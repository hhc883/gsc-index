"""引擎层：账号自检、提交前分析、批量提交调度。

所有耗时操作都通过 on_event 回调向外吐进度，Web 层据此推 SSE。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from . import api, sources
from .auth import KIND_OAUTH, AccountPool, AuthError
from .config import Config
from .store import Store

Emit = Callable[[dict], None]

# 分析后的分类
STATE_PENDING = "pending"    # 待提交
STATE_INDEXED = "indexed"    # 已收录
STATE_RECENT = "recent"      # 近期已提交过
STATE_OFFSITE = "offsite"    # 不属于当前站点
STATE_UNKNOWN = "unknown"    # 未预检 / 预检失败

# GSC 的权限级别。提交索引必须是 siteOwner；收录预检 siteOwner / siteFullUser 均可
PERMISSION_CN = {
    "siteOwner": "所有者",
    "siteFullUser": "完全",
    "siteRestrictedUser": "受限",
    "siteUnverifiedUser": "未验证",
}

STATE_CN = {
    STATE_PENDING: "待提交",
    STATE_INDEXED: "已收录",
    STATE_RECENT: "近期已提交",
    STATE_OFFSITE: "不属于本站点",
    STATE_UNKNOWN: "未预检",
}


def _noop(_evt: dict) -> None:
    pass


class Engine:
    def __init__(self, cfg: Config, store: Store, pool: AccountPool):
        self.cfg = cfg
        self.store = store
        self.pool = pool

    # ------------------------------------------------------------------
    # 账号自检
    # ------------------------------------------------------------------

    def check_accounts(self, site_url: str = "", emit: Emit = _noop) -> list[dict]:
        """逐个验证服务账号：能否取 token、能看到哪些 GSC 属性、是否为所有者。"""
        self.pool.reload()
        rows: list[dict] = []

        for fname, err in self.pool.errors:
            rows.append(
                {
                    "name": fname,
                    "email": "",
                    "project_id": "",
                    "kind": "",
                    "kind_cn": "无法识别",
                    "ok": False,
                    "error": err,
                    "sites": [],
                    "permission": "",
                    "is_owner": False,
                    "submit_used": 0,
                    "submit_limit": self.cfg.daily_quota_per_account,
                    "inspect_used": 0,
                    "inspect_limit": self.cfg.inspect_daily_quota,
                }
            )

        def check(acc):
            row = {
                "name": acc.name,
                "email": acc.email,
                "project_id": acc.project_id,
                "kind": acc.kind,
                "kind_cn": acc.kind_cn,
                "ok": False,
                "error": "",
                "sites": [],
                "permission": "",
                "is_owner": False,
                "submit_used": self.store.quota_used(acc.submit_scope),
                "submit_limit": self.cfg.daily_quota_per_account,
                "inspect_used": self.store.quota_used(acc.inspect_scope),
                "inspect_limit": self.cfg.inspect_daily_quota,
            }
            try:
                token = acc.token()
            except AuthError as exc:
                row["error"] = str(exc)
                return row
            ok, entries, err = api.list_sites(token, timeout=self.cfg.request_timeout)
            if not ok:
                row["error"] = err or "无法读取 GSC 属性列表"
                return row
            row["ok"] = True
            row["sites"] = [
                {
                    "site_url": e.get("siteUrl", ""),
                    "permission": e.get("permissionLevel", ""),
                    "permission_cn": PERMISSION_CN.get(
                        e.get("permissionLevel", ""), e.get("permissionLevel", "未知")
                    ),
                }
                for e in entries
            ]
            if site_url:
                hit = next((s for s in row["sites"] if s["site_url"] == site_url), None)
                if hit:
                    row["permission"] = hit["permission"]
                    row["permission_cn"] = hit["permission_cn"]
                    row["is_owner"] = hit["permission"] == "siteOwner"
                    if not row["is_owner"]:
                        row["error"] = (
                            "权限级别不够：当前是「" + hit["permission_cn"] + "」。"
                            "提交索引要求「所有者」，收录预检要求「所有者」或「完全」。"
                            "请到 GSC 的「设置 → 用户和权限」把级别改成所有者。"
                        )
                elif row["sites"]:
                    tail = (
                        "。域名属性与网址前缀属性在 GSC 里互不相通，"
                        + (
                            "请确认你授权的是拥有该属性的那个 Google 账号。"
                            if acc.kind == KIND_OAUTH
                            else "GSC 权限按属性单独授予，需要在这个属性里也把邮箱加为所有者。"
                        )
                    )
                    row["error"] = (
                        "看不到属性「" + site_url + "」。能看到的是："
                        + "、".join(s["site_url"] for s in row["sites"][:6])
                        + ("…" if len(row["sites"]) > 6 else "")
                        + tail
                    )
                elif acc.kind == KIND_OAUTH:
                    row["error"] = (
                        "这个 Google 账号（" + (acc.email or "未知邮箱")
                        + "）在 GSC 里没有任何属性。请确认授权时登录的是拥有你那些站点的账号。"
                    )
                else:
                    row["error"] = (
                        "这个服务账号在 GSC 里看不到任何属性。请到 Search Console 的"
                        "「设置 → 用户和权限 → 添加用户」，把 " + acc.email + " 加为「所有者」。"
                    )
            return row

        if self.pool.accounts:
            with ThreadPoolExecutor(max_workers=min(8, len(self.pool.accounts))) as ex:
                futures = {ex.submit(check, a): a for a in self.pool.accounts}
                for fut in as_completed(futures):
                    row = fut.result()
                    rows.append(row)
                    emit(
                        {
                            "type": "log",
                            "level": "success" if row["ok"] and not row["error"] else "error",
                            "message": row["name"] + " · " + (row["error"] or "正常"),
                        }
                    )

        rows.sort(key=lambda r: r["name"])
        return rows

    def eligible_accounts(
        self, site_url: str, require_owner: bool
    ) -> tuple[list, list[tuple[str, str, str]]]:
        """只返回真正对 site_url 有权限的凭据，避免把配额浪费在必然失败的账号上。

        提交索引要求 siteOwner；收录预检 siteOwner 或 siteFullUser 均可（require_owner=False）。
        返回 (合格凭据列表, 不合格凭据的诊断信息 [(名称, 邮箱, 原因), ...])，
        诊断信息用于在“一个合格账号都没有”时给出可操作的错误提示。
        """
        if not site_url or not self.pool.accounts:
            return list(self.pool.accounts), []

        def check(acc):
            try:
                token = acc.token()
            except AuthError as exc:
                return acc, False, str(exc)
            ok, entries, err = api.list_sites(token, timeout=self.cfg.request_timeout)
            if not ok:
                return acc, False, err or "无法读取 GSC 属性列表"
            hit = next((e for e in entries if e.get("siteUrl") == site_url), None)
            if not hit:
                return acc, False, "这个账号看不到该属性"
            perm = hit.get("permissionLevel", "")
            allowed = perm == "siteOwner" if require_owner else perm in ("siteOwner", "siteFullUser")
            if allowed:
                return acc, True, ""
            return acc, False, "权限级别是「" + PERMISSION_CN.get(perm, perm or "未知") + "」，不够"

        with ThreadPoolExecutor(max_workers=min(8, len(self.pool.accounts))) as ex:
            results = list(ex.map(check, self.pool.accounts))

        eligible = [acc for acc, ok, _ in results if ok]
        diagnostics = [(acc.name, acc.email, msg) for acc, ok, msg in results if not ok]
        return eligible, diagnostics

    def all_sites(self) -> list[dict]:
        """汇总全部账号可见的 GSC 属性，供界面下拉框使用。"""
        merged: dict[str, dict] = {}
        for acc in self.pool.accounts:
            try:
                token = acc.token()
            except AuthError:
                continue
            ok, entries, _ = api.list_sites(token, timeout=self.cfg.request_timeout)
            if not ok:
                continue
            for e in entries:
                url = e.get("siteUrl", "")
                if not url:
                    continue
                cur = merged.setdefault(
                    url, {"site_url": url, "permission": e.get("permissionLevel", ""), "owners": 0}
                )
                if e.get("permissionLevel") == "siteOwner":
                    cur["owners"] += 1
                    cur["permission"] = "siteOwner"
                cur["permission_cn"] = PERMISSION_CN.get(
                    cur["permission"], cur["permission"] or "未知"
                )
        return sorted(merged.values(), key=lambda s: s["site_url"])

    # ------------------------------------------------------------------
    # 分析
    # ------------------------------------------------------------------

    def analyze(
        self,
        urls: list[str],
        site_url: str,
        *,
        do_inspect: bool = True,
        force: bool = False,
        emit: Emit = _noop,
    ) -> dict:
        """去重 → 站点归属过滤 → 重复提交过滤 → 收录预检，返回逐条结果。"""
        raw_count = len(urls)
        urls = sources.dedupe([u for u in (sources.normalize(u) for u in urls) if u])
        emit({"type": "log", "level": "info", "message": f"输入 {raw_count} 条，去重后 {len(urls)} 条"})

        match = sources.site_matcher(site_url)
        rows: dict[str, dict] = {}
        candidates: list[str] = []

        for u in urls:
            self.store.seen(u, site_url)
            hist = self.store.url_row(u)
            row = {
                "url": u,
                "state": STATE_PENDING,
                "state_cn": STATE_CN[STATE_PENDING],
                "coverage": "",
                "verdict": "",
                "last_crawl": "",
                "last_submitted": hist["last_submitted"] if hist else "",
                "submit_count": hist["submit_count"] if hist else 0,
                "selected": True,
                "note": "",
            }
            if not match(u):
                row.update(
                    state=STATE_OFFSITE,
                    state_cn=STATE_CN[STATE_OFFSITE],
                    selected=False,
                    note="与所选 GSC 属性不匹配，提交必定失败",
                )
            else:
                candidates.append(u)
            rows[u] = row

        offsite = sum(1 for r in rows.values() if r["state"] == STATE_OFFSITE)
        if offsite:
            emit({"type": "log", "level": "warn", "message": f"{offsite} 条不属于所选站点，已排除"})

        # 近期已提交过的
        if not force and self.cfg.resubmit_after_days > 0:
            recent = self.store.recently_submitted(candidates, self.cfg.resubmit_after_days)
            for u in recent:
                rows[u].update(
                    state=STATE_RECENT,
                    state_cn=STATE_CN[STATE_RECENT],
                    selected=False,
                    note=f"{self.cfg.resubmit_after_days} 天内已提交过",
                )
            candidates = [u for u in candidates if u not in recent]
            if recent:
                emit({"type": "log", "level": "info", "message": f"{len(recent)} 条近期已提交，已排除"})

        # 收录预检
        if do_inspect and candidates:
            self._inspect_many(candidates, site_url, rows, emit)
        elif candidates:
            # 跳过预检时仍归类为待提交，只在文案上标明没查过
            for u in candidates:
                rows[u].update(state_cn=STATE_CN[STATE_PENDING] + "（未预检）")

        ordered = [rows[u] for u in urls]
        summary = {
            "raw": raw_count,
            "deduped": len(urls),
            "pending": sum(1 for r in ordered if r["state"] == STATE_PENDING),
            "indexed": sum(1 for r in ordered if r["state"] == STATE_INDEXED),
            "recent": sum(1 for r in ordered if r["state"] == STATE_RECENT),
            "offsite": offsite,
            "unknown": sum(1 for r in ordered if r["state"] == STATE_UNKNOWN),
        }
        emit(
            {
                "type": "log",
                "level": "success",
                "message": f"分析完成：待提交 {summary['pending']} · 已收录 {summary['indexed']} "
                f"· 近期已交 {summary['recent']} · 站外 {summary['offsite']}",
            }
        )
        return {"summary": summary, "rows": ordered, "site_url": site_url}

    def _inspect_many(
        self, urls: list[str], site_url: str, rows: dict[str, dict], emit: Emit
    ) -> None:
        eligible, diag = self.eligible_accounts(site_url, require_owner=False)
        if not eligible:
            detail = "；".join(
                name + "（" + (email or "无邮箱") + "）：" + msg for name, email, msg in diag
            )
            emit(
                {
                    "type": "log",
                    "level": "error",
                    "message": "没有任何凭据对「" + site_url + "」拥有权限，跳过预检。" + detail,
                }
            )
            for u in urls:
                rows[u].update(state=STATE_UNKNOWN, state_cn=STATE_CN[STATE_UNKNOWN], selected=True)
            return

        plan = self.pool.plan_inspect(len(urls), self.cfg.inspect_daily_quota, eligible)
        granted = sum(n for _, n in plan)
        if granted < len(urls):
            emit(
                {
                    "type": "log",
                    "level": "warn",
                    "message": f"预检配额只够 {granted} 条（共 {len(urls)} 条），其余标记为未预检",
                }
            )
        if not granted:
            for u in urls:
                rows[u].update(state=STATE_UNKNOWN, state_cn=STATE_CN[STATE_UNKNOWN], selected=True)
            return

        # 按配额把 URL 分给各账号
        assignments: list[tuple] = []
        cursor = 0
        for acc, n in plan:
            for u in urls[cursor : cursor + n]:
                assignments.append((acc, u))
            cursor += n
        for u in urls[cursor:]:
            rows[u].update(
                state=STATE_UNKNOWN,
                state_cn=STATE_CN[STATE_UNKNOWN],
                selected=True,
                note="预检配额不足，未检查",
            )

        total = len(assignments)
        emit({"type": "progress", "phase": "inspect", "done": 0, "total": total})
        done = 0

        def work(item):
            acc, url = item
            try:
                token = acc.token()
            except AuthError as exc:
                return acc, api.InspectResult(url, False, message=str(exc))
            return acc, api.inspect_url(
                token,
                site_url,
                url,
                timeout=self.cfg.request_timeout,
                max_retries=self.cfg.max_retries,
            )

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.concurrency)) as ex:
            for fut in as_completed([ex.submit(work, a) for a in assignments]):
                acc, res = fut.result()
                done += 1
                row = rows[res.url]
                if res.ok:
                    self.store.mark_inspected(res.url, res.verdict, res.coverage)
                    row.update(
                        coverage=res.coverage_cn,
                        verdict=res.verdict,
                        last_crawl=res.last_crawl,
                    )
                    if res.indexed:
                        row.update(
                            state=STATE_INDEXED,
                            state_cn=STATE_CN[STATE_INDEXED],
                            selected=False,
                            note="已在 Google 索引中，无需重复提交",
                        )
                    else:
                        row.update(state=STATE_PENDING, state_cn=STATE_CN[STATE_PENDING], selected=True)
                else:
                    # 预检没拿到结果，退还这一次配额
                    if res.status in (0, 401, 403, 429):
                        self.store.quota_refund(acc.inspect_scope, 1)
                    row.update(
                        state=STATE_UNKNOWN,
                        state_cn=STATE_CN[STATE_UNKNOWN],
                        selected=True,
                        note="预检失败：" + res.message[:120],
                    )
                    self.store.log(acc.name, res.url, "inspect", res.status, res.message)
                if done % 5 == 0 or done == total:
                    emit({"type": "progress", "phase": "inspect", "done": done, "total": total})

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------

    def submit(
        self,
        urls: list[str],
        site_url: str,
        *,
        notif_type: str = "URL_UPDATED",
        emit: Emit = _noop,
    ) -> dict:
        urls = sources.dedupe([u for u in (sources.normalize(u) for u in urls) if u])
        if not urls:
            return {"ok": 0, "failed": 0, "skipped": 0, "results": []}

        # 直接提交（跳过分析）的 URL 也要入库，否则重复提交保护和统计都会漏掉它们
        for u in urls:
            self.store.seen(u, site_url)

        if not self.pool.accounts:
            emit({"type": "log", "level": "error", "message": "accounts/ 里没有可用的服务账号密钥"})
            return {"ok": 0, "failed": 0, "skipped": len(urls), "results": [], "error": "无可用账号"}

        eligible, diag = self.eligible_accounts(site_url, require_owner=True)
        if not eligible:
            detail = "；".join(
                name + "（" + (email or "无邮箱") + "）：" + msg for name, email, msg in diag
            )
            emit(
                {
                    "type": "log",
                    "level": "error",
                    "message": "没有任何凭据对「" + site_url + "」拥有所有者权限，无法提交。" + detail,
                }
            )
            return {
                "ok": 0,
                "failed": 0,
                "skipped": len(urls),
                "results": [],
                "error": "没有凭据对该站点拥有所有者权限",
            }

        plan = self.pool.plan_submit(len(urls), self.cfg.daily_quota_per_account, eligible)
        granted = sum(n for _, n in plan)
        results: list[dict] = []

        if granted < len(urls):
            emit(
                {
                    "type": "log",
                    "level": "warn",
                    "message": f"今日提交配额只剩 {granted} 条，本次提交前 {granted} 条，"
                    f"其余 {len(urls) - granted} 条请明天再来",
                }
            )
            for u in urls[granted:]:
                results.append({"url": u, "ok": False, "status": 0, "message": "今日配额不足，未提交", "account": ""})
        if not granted:
            return {
                "ok": 0,
                "failed": 0,
                "skipped": len(urls),
                "results": results,
                "error": "今日配额已用尽",
            }

        # 按账号切分，再按 batch_size 分片
        chunks: list[tuple] = []
        cursor = 0
        for acc, n in plan:
            mine = urls[cursor : cursor + n]
            cursor += n
            size = max(1, min(100, self.cfg.batch_size))
            for i in range(0, len(mine), size):
                chunks.append((acc, mine[i : i + size]))

        total = granted
        emit({"type": "progress", "phase": "submit", "done": 0, "total": total})
        emit(
            {
                "type": "log",
                "level": "info",
                "message": f"开始提交 {total} 条，分 {len(chunks)} 批，动用 {len(plan)} 个账号",
            }
        )
        done = 0

        def work(item):
            acc, batch = item
            try:
                token = acc.token()
            except AuthError as exc:
                return acc, batch, [api.UrlResult(u, False, 0, str(exc)) for u in batch]
            return acc, batch, api.publish_batch(
                token,
                batch,
                notif_type,
                timeout=self.cfg.request_timeout,
                max_retries=self.cfg.max_retries,
            )

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.concurrency)) as ex:
            for fut in as_completed([ex.submit(work, c) for c in chunks]):
                acc, batch, batch_results = fut.result()
                refund = 0
                for res in batch_results:
                    done += 1
                    self.store.mark_submitted(res.url, "OK" if res.ok else "ERR " + str(res.status))
                    # 成功也记一笔，历史统计的趋势图依赖完整日志
                    self.store.log(acc.name, res.url, "publish", res.status, res.message)
                    if not res.ok and res.status in (0, 401, 403, 429):
                        # 这几类失败 Google 并未消耗配额，退还
                        refund += 1
                    results.append(
                        {
                            "url": res.url,
                            "ok": res.ok,
                            "status": res.status,
                            "message": res.message,
                            "account": acc.name,
                        }
                    )
                if refund:
                    self.store.quota_refund(acc.submit_scope, refund)
                bad = [r for r in batch_results if not r.ok]
                if bad:
                    emit(
                        {
                            "type": "log",
                            "level": "error",
                            "message": f"{acc.name} 批次 {len(batch)} 条中 {len(bad)} 条失败："
                            + bad[0].message[:140],
                        }
                    )
                else:
                    emit(
                        {
                            "type": "log",
                            "level": "success",
                            "message": f"{acc.name} 成功提交 {len(batch)} 条",
                        }
                    )
                emit({"type": "progress", "phase": "submit", "done": done, "total": total})

        ok = sum(1 for r in results if r["ok"])
        failed = sum(1 for r in results if not r["ok"])
        emit(
            {
                "type": "log",
                "level": "success" if failed == 0 else "warn",
                "message": f"提交完成：成功 {ok} 条，失败 {failed} 条",
            }
        )
        return {"ok": ok, "failed": failed, "skipped": 0, "results": results}

    # ------------------------------------------------------------------
    # 站点地图
    # ------------------------------------------------------------------

    def sitemap_submit(self, site_url: str, sitemap_url: str) -> tuple[bool, str]:
        feed = sources.guess_sitemap_feedpath(site_url, sitemap_url)
        last = "没有可用的服务账号"
        for acc in self.pool.accounts:
            try:
                token = acc.token()
            except AuthError as exc:
                last = str(exc)
                continue
            ok, msg = api.submit_sitemap(
                token, site_url, feed, timeout=self.cfg.request_timeout
            )
            self.store.log(acc.name, feed, "sitemap", 200 if ok else 0, msg)
            if ok:
                return True, msg + "：" + feed
            last = msg
        return False, last

    def sitemap_list(self, site_url: str) -> tuple[bool, list[dict], str]:
        last = "没有可用的服务账号"
        for acc in self.pool.accounts:
            try:
                token = acc.token()
            except AuthError as exc:
                last = str(exc)
                continue
            ok, items, msg = api.list_sitemaps(
                token, site_url, timeout=self.cfg.request_timeout
            )
            if ok:
                return True, items, ""
            last = msg
        return False, [], last

    # ------------------------------------------------------------------
    # 配额概览
    # ------------------------------------------------------------------

    def quota_overview(self) -> dict:
        accounts = []
        for acc in self.pool.accounts:
            accounts.append(
                {
                    "name": acc.name,
                    "email": acc.email,
                    "kind": acc.kind,
                    "kind_cn": acc.kind_cn,
                    "submit_used": self.store.quota_used(acc.submit_scope),
                    "submit_limit": self.cfg.daily_quota_per_account,
                    "inspect_used": self.store.quota_used(acc.inspect_scope),
                    "inspect_limit": self.cfg.inspect_daily_quota,
                }
            )
        return {
            "accounts": accounts,
            "submit_used": sum(a["submit_used"] for a in accounts),
            "submit_limit": len(accounts) * self.cfg.daily_quota_per_account,
            "inspect_used": sum(a["inspect_used"] for a in accounts),
            "inspect_limit": len(accounts) * self.cfg.inspect_daily_quota,
        }
