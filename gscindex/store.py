"""SQLite 状态存储：站点链接清单、每日配额、操作日志。

表的职责划分：
* site_urls   —— 站点链接清单。扫描发现的**全部** URL 都在这里，
                 不管有没有被收录。两个维度严格分开、互不影响：
                   index_state  = Google 说这条收录了没（indexed/not_indexed/unknown）
                   requested_at = 我们有没有通过本工具申请过
                 「已收录」只看 index_state，跟有没有申请过完全无关——
                 一条从没申请过的页面完全可能本来就是收录的。
* urls        —— 更早期的 URL 历史表，保留用于统计
* quota       —— 按天计的 API 配额（收录查询）
* webauto_day —— 每个站点每天点了几次"请求编入索引"
* log         —— 操作流水，source 区分是旧的 Indexing API 还是网页自动化
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
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
CREATE TABLE IF NOT EXISTS site_urls (
    url           TEXT PRIMARY KEY,
    site          TEXT NOT NULL,
    -- Google 的收录判定。这是"已收录"唯一的依据，跟有没有通过本工具
    -- 申请过毫无关系。取值：indexed / not_indexed / unknown
    index_state   TEXT NOT NULL DEFAULT 'unknown',
    coverage      TEXT,          -- Google 给的原文描述
    verdict       TEXT,          -- PASS / NEUTRAL / FAIL
    last_checked  TEXT,          -- 上次查询收录状态的时间
    first_seen    TEXT NOT NULL,
    -- 下面是"我们做过什么"，跟收录状态是两码事
    requested_at  TEXT,
    request_count INTEGER NOT NULL DEFAULT 0,
    last_result   TEXT
);
CREATE TABLE IF NOT EXISTS webauto_day (
    site TEXT NOT NULL,
    day  TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (site, day)
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON log(ts);
CREATE INDEX IF NOT EXISTS idx_urls_submitted ON urls(last_submitted);
CREATE INDEX IF NOT EXISTS idx_site_urls_site ON site_urls(site, index_state);
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
        """轻量迁移：老库缺的列补上、老表数据搬过来，不丢数据。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(log)")}
        if "source" not in cols:
            # 老的 Indexing API 时代的记录没有来源标记，统一回填，
            # 这样历史统计里能把两种提交方式的实际效果分开看
            self.conn.execute("ALTER TABLE log ADD COLUMN source TEXT")
            self.conn.execute(
                "UPDATE log SET source = 'indexing_api' WHERE source IS NULL"
            )

        # 从旧的 pending 表迁到 site_urls。
        # 旧模型把"Google 的收录状态"和"这条待办办完了"混用同一个 done_at 字段，
        # 语义上讲不清"已收录"到底是本来就收录、还是申请之后才收录的。
        # 新模型用独立的 index_state 表达 Google 的判定，跟申请记录彻底解耦。
        tables = {
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "pending" in tables:
            moved = self.conn.execute(
                """INSERT OR IGNORE INTO site_urls
                     (url, site, index_state, coverage, verdict,
                      last_checked, first_seen, requested_at, request_count, last_result)
                   SELECT url, site,
                          CASE WHEN done_at IS NOT NULL THEN 'indexed'
                               ELSE 'not_indexed' END,
                          coverage, verdict,
                          COALESCE(done_at, found_at), found_at,
                          requested_at, request_count, last_result
                   FROM pending"""
            ).rowcount
            self.conn.execute("DROP TABLE pending")
            if moved:
                print(f"[迁移] 已把 {moved} 条记录从 pending 迁入 site_urls")

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

    # ---------- 站点链接清单 ----------

    INDEXED = "indexed"
    NOT_INDEXED = "not_indexed"
    UNKNOWN = "unknown"

    def url_upsert(
        self,
        site: str,
        url: str,
        index_state: str,
        coverage: str = "",
        verdict: str = "",
    ) -> None:
        """记录/更新一条链接的收录状态。

        只动收录相关的字段，申请记录（requested_at / request_count）保持不变——
        这两个维度是独立的，重新查一次收录状态不该影响申请历史。
        """
        now = _now()
        with self._lock:
            self.conn.execute(
                """INSERT INTO site_urls
                     (url, site, index_state, coverage, verdict, last_checked, first_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET
                     site         = excluded.site,
                     index_state  = excluded.index_state,
                     coverage     = excluded.coverage,
                     verdict      = excluded.verdict,
                     last_checked = excluded.last_checked""",
                (url, site, index_state, coverage, verdict, now, now),
            )
            self.conn.commit()

    def url_mark_requested(self, url: str, result: str) -> None:
        """记录一次"通过本工具申请收录"。不碰 index_state——
        申请了不等于就收录了，得等下次复查 Google 才知道。
        """
        with self._lock:
            self.conn.execute(
                """UPDATE site_urls
                   SET requested_at = ?, request_count = request_count + 1,
                       last_result = ?
                   WHERE url = ?""",
                (_now(), result, url),
            )
            self.conn.commit()

    def url_row_state(self, url: str) -> str:
        """取一条链接当前记录的收录状态，没有记录返回 unknown。

        复查时用来判断"这次是不是新确认收录的"。
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT index_state FROM site_urls WHERE url = ?", (url,)
            ).fetchone()
        return row["index_state"] if row else self.UNKNOWN

    def url_list(
        self, site: str = "", index_state: str = "", requested: str = ""
    ) -> list[dict]:
        """查链接清单。

        index_state 传 indexed / not_indexed / unknown 做筛选，空表示全部。
        requested 传 'yes' / 'no' 按有没有申请过筛选，空表示不限。
        """
        where, params = [], []
        if site:
            where.append("site = ?")
            params.append(site)
        if index_state:
            where.append("index_state = ?")
            params.append(index_state)
        if requested == "yes":
            where.append("requested_at IS NOT NULL")
        elif requested == "no":
            where.append("requested_at IS NULL")
        sql = "SELECT * FROM site_urls"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # 排序意图：需要处理的排前面——未收录且从没申请过的最优先，
        # 然后是未收录但申请过的，未查明的次之，已收录的沉到最后只作参考。
        sql += """ ORDER BY
                     CASE index_state
                       WHEN 'not_indexed' THEN 0
                       WHEN 'unknown' THEN 1
                       ELSE 2 END,
                     (requested_at IS NOT NULL),
                     first_seen"""
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def url_counts(self) -> list[dict]:
        """按站点汇总，给界面做概览。"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT site,
                          COUNT(*) AS total,
                          SUM(index_state = 'indexed')     AS indexed,
                          SUM(index_state = 'not_indexed') AS not_indexed,
                          SUM(index_state = 'unknown')     AS unknown,
                          SUM(index_state <> 'indexed' AND requested_at IS NULL)
                            AS never_requested,
                          SUM(index_state <> 'indexed' AND requested_at IS NOT NULL)
                            AS requested
                   FROM site_urls
                   GROUP BY site ORDER BY site"""
            ).fetchall()
        return [dict(r) for r in rows]

    def urls_to_recheck(self, site: str = "") -> list[str]:
        """需要复查的链接：申请过、但目前还没确认收录的。

        这是"复查我提交的链接收录了没"这个功能的取数逻辑。
        """
        where = ["requested_at IS NOT NULL", "index_state <> 'indexed'"]
        params: list = []
        if site:
            where.append("site = ?")
            params.append(site)
        with self._lock:
            rows = self.conn.execute(
                "SELECT url FROM site_urls WHERE " + " AND ".join(where)
                + " ORDER BY requested_at",
                params,
            ).fetchall()
        return [r["url"] for r in rows]

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
                "SELECT COUNT(*) c FROM site_urls WHERE index_state = 'not_indexed'"
            ).fetchone()["c"]
            pending_never = self.conn.execute(
                "SELECT COUNT(*) c FROM site_urls "
                "WHERE index_state = 'not_indexed' AND requested_at IS NULL"
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
