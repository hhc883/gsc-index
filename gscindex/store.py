"""SQLite 状态存储：URL 历史、待办池、每日配额、操作日志。

表的职责划分：
* urls        —— 每个 URL 的历史（何时预检过、收录状态、提交过几次）
* pending     —— 未收录待办池，跨天持久化。真实的网页提交名额很有限，
                 一次扫描出的几十上百条未收录 URL 要分好几天才交得完，
                 所以必须存下来，而不是每次扫完就丢。
* quota       —— 按天计的 API 配额（现在只剩收录预检，Indexing API 已移除）
* webauto_day —— 网页自动化每个站点每天点了几次"请求编入索引"
* log         —— 操作流水，source 区分是旧的 Indexing API 还是网页自动化
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    url             TEXT PRIMARY KEY,
    site            TEXT,
    first_seen      TEXT NOT NULL,
    last_submitted  TEXT,
    submit_count    INTEGER NOT NULL DEFAULT 0,
    last_status     TEXT,
    coverage_state  TEXT,
    verdict         TEXT,
    last_inspected  TEXT
);
CREATE TABLE IF NOT EXISTS quota (
    scope   TEXT NOT NULL,
    day     TEXT NOT NULL,
    used    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, day)
);
CREATE TABLE IF NOT EXISTS log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    account TEXT,
    url     TEXT,
    action  TEXT,
    status  INTEGER,
    message TEXT
);
CREATE TABLE IF NOT EXISTS pending (
    url          TEXT PRIMARY KEY,
    site         TEXT NOT NULL,
    coverage     TEXT,
    verdict      TEXT,
    found_at     TEXT NOT NULL,
    requested_at TEXT,
    request_count INTEGER NOT NULL DEFAULT 0,
    last_result  TEXT,
    done_at      TEXT
);
CREATE TABLE IF NOT EXISTS webauto_day (
    site TEXT NOT NULL,
    day  TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (site, day)
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON log(ts);
CREATE INDEX IF NOT EXISTS idx_urls_submitted ON urls(last_submitted);
CREATE INDEX IF NOT EXISTS idx_pending_site ON pending(site, done_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_key() -> str:
    """配额按太平洋时间重置（Google 配额口径），这里用 UTC-8 近似。"""
    return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """轻量迁移：老库缺的列补上，不动已有数据。

        source 列用来区分历史记录的来源：老的 Indexing API 时代的记录没有这个值，
        统一回填成 indexing_api；之后网页自动化产生的记录标 webauto。
        这样历史统计里能把两种方式的实际效果分开看。
        """
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(log)")}
        if "source" not in cols:
            self.conn.execute("ALTER TABLE log ADD COLUMN source TEXT")
            self.conn.execute(
                "UPDATE log SET source = 'indexing_api' WHERE source IS NULL"
            )

    # ---------- URL ----------

    def seen(self, url: str, site: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO urls (url, site, first_seen) VALUES (?, ?, ?)",
                (url, site, _now()),
            )
            self.conn.commit()

    def mark_submitted(self, url: str, status: str) -> None:
        with self._lock:
            self.conn.execute(
                """UPDATE urls
                   SET last_submitted = ?, submit_count = submit_count + 1, last_status = ?
                   WHERE url = ?""",
                (_now(), status, url),
            )
            self.conn.commit()

    def mark_inspected(self, url: str, verdict: str, coverage: str) -> None:
        with self._lock:
            self.conn.execute(
                """UPDATE urls
                   SET last_inspected = ?, verdict = ?, coverage_state = ?
                   WHERE url = ?""",
                (_now(), verdict, coverage, url),
            )
            self.conn.commit()

    def recently_submitted(self, urls: list[str], within_days: int) -> set[str]:
        """返回 within_days 天内已提交过的 URL，用于跳过重复提交。"""
        if within_days <= 0 or not urls:
            return set()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
        out: set[str] = set()
        with self._lock:
            for i in range(0, len(urls), 500):
                chunk = urls[i : i + 500]
                q = ",".join("?" * len(chunk))
                rows = self.conn.execute(
                    f"SELECT url FROM urls WHERE url IN ({q}) "
                    f"AND last_submitted IS NOT NULL AND last_submitted > ?",
                    (*chunk, cutoff),
                ).fetchall()
                out.update(r["url"] for r in rows)
        return out

    def url_row(self, url: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM urls WHERE url = ?", (url,)
            ).fetchone()

    # ---------- 配额 ----------

    def quota_used(self, scope: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT used FROM quota WHERE scope = ? AND day = ?",
                (scope, today_key()),
            ).fetchone()
            return row["used"] if row else 0

    def quota_take(self, scope: str, limit: int, want: int) -> int:
        """原子地申请配额，返回实际拿到的数量（可能少于 want）。"""
        with self._lock:
            day = today_key()
            row = self.conn.execute(
                "SELECT used FROM quota WHERE scope = ? AND day = ?", (scope, day)
            ).fetchone()
            used = row["used"] if row else 0
            grant = max(0, min(want, limit - used))
            if grant:
                self.conn.execute(
                    """INSERT INTO quota (scope, day, used) VALUES (?, ?, ?)
                       ON CONFLICT(scope, day) DO UPDATE SET used = used + ?""",
                    (scope, day, grant, grant),
                )
                self.conn.commit()
            return grant

    def quota_refund(self, scope: str, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self.conn.execute(
                "UPDATE quota SET used = MAX(0, used - ?) WHERE scope = ? AND day = ?",
                (n, scope, today_key()),
            )
            self.conn.commit()

    # ---------- 日志 ----------

    def log(
        self,
        account: str,
        url: str,
        action: str,
        status: int | None,
        message: str = "",
        source: str = "webauto",
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO log (ts, account, url, action, status, message, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), account, url, action, status, message[:500], source),
            )
            self.conn.commit()

    # ---------- 待办池（未收录清单，跨天持久化） ----------

    def pending_upsert(self, site: str, url: str, coverage: str, verdict: str) -> None:
        """把一条未收录 URL 放进待办池；已存在的只刷新收录状态，保留提交历史。"""
        with self._lock:
            self.conn.execute(
                """INSERT INTO pending (url, site, coverage, verdict, found_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                     coverage = excluded.coverage,
                     verdict  = excluded.verdict,
                     site     = excluded.site""",
                (url, site, coverage, verdict, _now()),
            )
            self.conn.commit()

    def pending_resolve(self, url: str) -> None:
        """这条 URL 已经确认收录了，标记完成（不再出现在待办里，但记录保留）。"""
        with self._lock:
            self.conn.execute(
                "UPDATE pending SET done_at = ? WHERE url = ? AND done_at IS NULL",
                (_now(), url),
            )
            self.conn.commit()

    def pending_reopen(self, url: str) -> None:
        """重新打开：之前判定已收录、现在又查出没收录了。

        Google 的收录状态是会变的（页面被移出索引、规范网址改变等），
        所以不能假设"标记完成"就是终态——每次扫描都要按最新结果校正。
        """
        with self._lock:
            self.conn.execute(
                "UPDATE pending SET done_at = NULL WHERE url = ? AND done_at IS NOT NULL",
                (url,),
            )
            self.conn.commit()

    def pending_mark_requested(self, url: str, result: str) -> None:
        with self._lock:
            self.conn.execute(
                """UPDATE pending
                   SET requested_at = ?, request_count = request_count + 1,
                       last_result = ?
                   WHERE url = ?""",
                (_now(), result, url),
            )
            self.conn.commit()

    def pending_list(self, site: str = "", include_done: bool = False) -> list[dict]:
        where, params = [], []
        if site:
            where.append("site = ?")
            params.append(site)
        if not include_done:
            where.append("done_at IS NULL")
        sql = "SELECT * FROM pending"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # 排序意图：还没收录的排在前面（这些才需要处理），其中从没申请过的最优先，
        # 因为名额有限、优先花在这些上；已收录的沉到最后，只作为参考。
        sql += " ORDER BY (done_at IS NOT NULL), (requested_at IS NOT NULL), found_at"
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def pending_counts(self) -> list[dict]:
        """按站点汇总，给界面做概览。

        total 是这个站点扫到过的全部 URL；pending 才是还没收录、需要处理的。
        indexed 单独给出来，让用户能看到"已经收录了多少"这个正向进展。
        """
        with self._lock:
            rows = self.conn.execute(
                """SELECT site,
                          COUNT(*) AS total,
                          SUM(CASE WHEN done_at IS NOT NULL THEN 1 ELSE 0 END) AS indexed,
                          SUM(CASE WHEN done_at IS NULL THEN 1 ELSE 0 END) AS pending,
                          SUM(CASE WHEN done_at IS NULL AND requested_at IS NULL
                                   THEN 1 ELSE 0 END) AS never,
                          SUM(CASE WHEN done_at IS NULL AND requested_at IS NOT NULL
                                   THEN 1 ELSE 0 END) AS waiting
                   FROM pending
                   GROUP BY site ORDER BY site"""
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 网页自动化的每日点击计数 ----------

    def webauto_used(self, site: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT used FROM webauto_day WHERE site = ? AND day = ?",
                (site, today_key()),
            ).fetchone()
            return row["used"] if row else 0

    def webauto_take(self, site: str, limit: int) -> bool:
        """原子地申请一次点击名额，成功返回 True。"""
        with self._lock:
            day = today_key()
            row = self.conn.execute(
                "SELECT used FROM webauto_day WHERE site = ? AND day = ?", (site, day)
            ).fetchone()
            if (row["used"] if row else 0) >= limit:
                return False
            self.conn.execute(
                """INSERT INTO webauto_day (site, day, used) VALUES (?, ?, 1)
                   ON CONFLICT(site, day) DO UPDATE SET used = used + 1""",
                (site, day),
            )
            self.conn.commit()
            return True

    def webauto_refund(self, site: str) -> None:
        """这次没真正交出去，把名额退回来。"""
        with self._lock:
            self.conn.execute(
                "UPDATE webauto_day SET used = MAX(0, used - 1) "
                "WHERE site = ? AND day = ?",
                (site, today_key()),
            )
            self.conn.commit()

    # ---------- 统计 ----------

    def stats(self) -> dict:
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) c FROM urls").fetchone()["c"]
            submitted = self.conn.execute(
                "SELECT COUNT(*) c FROM urls WHERE submit_count > 0"
            ).fetchone()["c"]
            indexed = self.conn.execute(
                "SELECT COUNT(*) c FROM urls WHERE verdict = 'PASS'"
            ).fetchone()["c"]
            today = self.conn.execute(
                "SELECT scope, used FROM quota WHERE day = ? ORDER BY scope",
                (today_key(),),
            ).fetchall()
            recent = self.conn.execute(
                "SELECT ts, url, action, status, message FROM log "
                "ORDER BY id DESC LIMIT 15"
            ).fetchall()
        with self._lock:
            pending_open = self.conn.execute(
                "SELECT COUNT(*) c FROM pending WHERE done_at IS NULL"
            ).fetchone()["c"]
            pending_never = self.conn.execute(
                "SELECT COUNT(*) c FROM pending "
                "WHERE done_at IS NULL AND requested_at IS NULL"
            ).fetchone()["c"]
        return {
            "total": total,
            "submitted": submitted,
            "indexed": indexed,
            "pending_open": pending_open,
            "pending_never": pending_never,
            "quota_today": [dict(r) for r in today],
            "recent": [dict(r) for r in recent],
        }

    def daily_series(self, days: int = 14) -> list[dict]:
        """最近 N 天的提交量（成功/失败），用于趋势图。

        同时把两种来源分开：webauto 是现在真正在用的网页自动化，
        indexing_api 是已移除的旧通道的历史遗留，放在一起看会误导。
        """
        with self._lock:
            rows = self.conn.execute(
                """SELECT substr(ts, 1, 10) AS d,
                          SUM(CASE WHEN status = 200 THEN 1 ELSE 0 END) AS ok,
                          SUM(CASE WHEN status <> 200 THEN 1 ELSE 0 END) AS fail,
                          SUM(CASE WHEN source = 'webauto' THEN 1 ELSE 0 END) AS webauto,
                          SUM(CASE WHEN source = 'indexing_api' THEN 1 ELSE 0 END) AS legacy
                   FROM log WHERE action IN ('publish', 'webauto')
                   GROUP BY d ORDER BY d DESC LIMIT ?""",
                (days,),
            ).fetchall()
        series = [dict(r) for r in rows]
        series.reverse()
        return series

    def source_breakdown(self) -> list[dict]:
        """按来源统计成败，用来对比两种方式的实际效果。"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT COALESCE(source, 'indexing_api') AS source,
                          COUNT(*) AS total,
                          SUM(CASE WHEN status = 200 THEN 1 ELSE 0 END) AS ok
                   FROM log WHERE action IN ('publish', 'webauto')
                   GROUP BY COALESCE(source, 'indexing_api')"""
            ).fetchall()
        return [dict(r) for r in rows]

    def failure_reasons(self, limit: int = 8) -> list[dict]:
        """失败原因分布，按 HTTP 状态码归类。"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT status, COUNT(*) AS n, MIN(message) AS sample
                   FROM log WHERE action = 'publish' AND status <> 200
                   GROUP BY status ORDER BY n DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def log_page(self, limit: int = 100, offset: int = 0, keyword: str = "") -> dict:
        """分页读取操作日志，支持按 URL / 信息关键词过滤。"""
        where, params = "", []
        if keyword:
            where = "WHERE url LIKE ? OR message LIKE ? OR account LIKE ?"
            kw = "%" + keyword + "%"
            params = [kw, kw, kw]
        with self._lock:
            total = self.conn.execute(
                "SELECT COUNT(*) AS c FROM log " + where, params
            ).fetchone()["c"]
            rows = self.conn.execute(
                "SELECT ts, account, url, action, status, message FROM log "
                + where
                + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return {"total": total, "rows": [dict(r) for r in rows]}

    def close(self) -> None:
        self.conn.close()
