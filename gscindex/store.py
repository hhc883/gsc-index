"""SQLite 状态存储：URL 历史、每日配额、操作日志。"""

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
CREATE INDEX IF NOT EXISTS idx_log_ts ON log(ts);
CREATE INDEX IF NOT EXISTS idx_urls_submitted ON urls(last_submitted);
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
        self.conn.commit()

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
    ) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO log (ts, account, url, action, status, message) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), account, url, action, status, message[:500]),
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
        return {
            "total": total,
            "submitted": submitted,
            "indexed": indexed,
            "quota_today": [dict(r) for r in today],
            "recent": [dict(r) for r in recent],
        }

    def daily_series(self, days: int = 14) -> list[dict]:
        """最近 N 天的提交量（成功/失败），用于趋势图。"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT substr(ts, 1, 10) AS d,
                          SUM(CASE WHEN status = 200 THEN 1 ELSE 0 END) AS ok,
                          SUM(CASE WHEN status <> 200 THEN 1 ELSE 0 END) AS fail
                   FROM log WHERE action = 'publish'
                   GROUP BY d ORDER BY d DESC LIMIT ?""",
                (days,),
            ).fetchall()
        series = [dict(r) for r in rows]
        series.reverse()
        return series

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
