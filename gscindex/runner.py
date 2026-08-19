"""引擎层：账号自检、全站扫描、收录预检、网页自动化提交调度。

所有耗时操作都通过 on_event 回调向外吐进度，Web 层据此推 SSE。

两条链路性质不同，别混淆：
* api.py     —— Google 官方开放接口（查站点、查收录、交站点地图），配额充裕
* webauto.py —— 自动化操作 GSC 网页点“请求编入索引”，灰色地带、名额稀缺
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from . import api, sources, webauto
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
        self, site_url: str, require_owner: bool, account_name: str = ""
    ) -> tuple[list, list[tuple[str, str, str]]]:
        """只返回真正对 site_url 有权限的凭据，避免把配额浪费在必然失败的账号上。

        提交索引要求 siteOwner；收录预检 siteOwner 或 siteFullUser 均可（require_owner=False）。
        传 account_name 时只核实这一个凭据（用户在界面上手动指定了要用哪个账号的配额），
        不再让引擎自动跨全部凭据挑选——指定的账号如果没权限就直接报错，不会静默换用别的账号。
        返回 (合格凭据列表, 不合格凭据的诊断信息 [(名称, 邮箱, 原因), ...])，
        诊断信息用于在“一个合格账号都没有”时给出可操作的错误提示。
        """
        candidates = self.pool.accounts
        if account_name:
            acc = self.pool.by_name(account_name)
            if not acc:
                return [], [(account_name, "", "找不到这个账号，可能已被删除，请刷新账号列表")]
            candidates = [acc]

        if not site_url or not candidates:
            return list(candidates), []

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

        with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as ex:
            results = list(ex.map(check, candidates))

        eligible = [acc for acc, ok, _ in results if ok]
        diagnostics = [(acc.name, acc.email, msg) for acc, ok, msg in results if not ok]
        return eligible, diagnostics

    def all_sites(self, account_name: str = "") -> list[dict]:
        """汇总账号可见的 GSC 属性，供界面下拉框使用。

        传 account_name 时只看这一个账号能访问哪些属性，不再跨账号合并——
        用来实现"选定某个账号后，站点下拉框只显示这个账号名下的站点"。
        """
        accounts = self.pool.accounts
        if account_name:
            acc = self.pool.by_name(account_name)
            accounts = [acc] if acc else []

        merged: dict[str, dict] = {}
        for acc in accounts:
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
        account_name: str = "",
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
            self._inspect_many(candidates, site_url, rows, emit, account_name=account_name)
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
        self,
        urls: list[str],
        site_url: str,
        rows: dict[str, dict],
        emit: Emit,
        account_name: str = "",
    ) -> None:
        eligible, diag = self.eligible_accounts(site_url, require_owner=False, account_name=account_name)
        if not eligible:
            detail = "；".join(
                name + "（" + (email or "无邮箱") + "）：" + msg for name, email, msg in diag
            )
            scope = ("指定账号「" + account_name + "」") if account_name else "任何凭据"
            emit(
                {
                    "type": "log",
                    "level": "error",
                    "message": "没有" + scope + "对「" + site_url + "」拥有权限，跳过预检。" + detail,
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
    # 全站扫描：抓站点地图 -> 批量预检 -> 未收录的进待办池
    # ------------------------------------------------------------------

    def scan(
        self,
        site_url: str,
        sitemap_url: str,
        *,
        account_name: str = "",
        limit: int = 0,
        emit: Emit = _noop,
    ) -> dict:
        """一步走完“发现全站 URL -> 查哪些没收录 -> 存进待办池”。

        这是整个工具的核心动作。预检配额（2000/天/凭据）相对充裕，
        所以这一步可以放心扫几百上千条；真正稀缺的是后面网页点击的名额，
        所以这里的产出是一份待办清单，供之后分多天慢慢消化。
        """
        emit({"type": "log", "level": "info", "message": "开始抓取站点地图 " + sitemap_url})

        def progress(current, found):
            emit({"type": "log", "level": "info",
                  "message": f"读取 {current}（已收集 {found} 条）"})

        try:
            urls, visited, errors = sources.fetch_sitemap(sitemap_url, on_progress=progress)
        except sources.SourceError as exc:
            emit({"type": "log", "level": "error", "message": str(exc)})
            return {"error": str(exc), "found": 0, "pending": 0}

        for e in errors:
            emit({"type": "log", "level": "warn", "message": e})
        if not visited:
            emit({"type": "log", "level": "error",
                  "message": "一个站点地图文件都没读到，请检查上面的报错"})
            return {"error": "站点地图读取失败", "found": 0, "pending": 0}

        emit({"type": "log", "level": "success",
              "message": f"站点地图读到 {len(visited)} 个文件、{len(urls)} 条 URL"})

        # 只保留属于这个 GSC 属性的 URL，其余提交必然失败，没必要浪费预检配额
        match = sources.site_matcher(site_url)
        onsite = [u for u in urls if match(u)]
        if len(onsite) < len(urls):
            emit({"type": "log", "level": "warn",
                  "message": f"{len(urls) - len(onsite)} 条不属于所选站点，已排除"})
        if limit and len(onsite) > limit:
            emit({"type": "log", "level": "info",
                  "message": f"按设置只处理前 {limit} 条（共 {len(onsite)} 条）"})
            onsite = onsite[:limit]
        if not onsite:
            return {"error": "没有属于该站点的 URL", "found": 0, "pending": 0}

        # 借用已有的 analyze：它会做去重、预检、并把结果写进 urls 表
        res = self.analyze(
            onsite, site_url, do_inspect=True, force=True,
            account_name=account_name, emit=emit,
        )

        # 全部入库，用 done_at 区分已收录与否——这样界面上"全部/已收录/未收录"
        # 三种视图都有数据可看，而不是只留下未收录的那部分。
        added = resolved = unknown = 0
        for row in res["rows"]:
            if row["state"] == STATE_UNKNOWN:
                unknown += 1  # 预检没查成，状态不明，不入库免得给出误导性的结论
                continue
            # 先写入/更新收录状态，已收录的再标记完成
            self.store.pending_upsert(
                site_url, row["url"], row["coverage"], row["verdict"]
            )
            if row["state"] == STATE_INDEXED:
                self.store.pending_resolve(row["url"])
                resolved += 1
            elif row["state"] == STATE_PENDING:
                # 之前被判定已收录、现在又查出没收录的，要把完成标记撤掉重新进待办
                self.store.pending_reopen(row["url"])
                added += 1

        msg = f"扫描完成：未收录 {added} 条待处理，已收录 {resolved} 条"
        if unknown:
            msg += f"，{unknown} 条预检失败未记录"
        emit({"type": "log", "level": "success", "message": msg})
        return {
            "found": len(onsite), "pending": added,
            "resolved": resolved, "unknown": unknown,
            "site_url": site_url,
        }

    # ------------------------------------------------------------------
    # 网页自动化提交（点 GSC 网页上的“请求编入索引”）
    # ------------------------------------------------------------------

    def webauto_submit(
        self, site_url: str, urls: list[str], *, emit: Emit = _noop, should_stop=None
    ) -> dict:
        """逐个 URL 去 GSC 网页点"请求编入索引"。

        整批复用同一个浏览器会话——启动 Chrome 并加载 GSC 大约要十几秒，
        以前每条都重开一次，这部分开销被重复 N 遍。现在只启动一次。

        仍然是串行 + 随机间隔，不做并发：Google 那边的每日上限不明
        （撞到"超出了配额"就是到顶了），并发只会更快撞墙、更像机器人。
        """
        if not webauto.has_session(self.cfg.webauto_session_path):
            emit({"type": "log", "level": "error",
                  "message": "还没有浏览器登录，请先到「账号管理」完成一次浏览器登录"})
            return {"ok": 0, "failed": 0, "skipped": len(urls), "error": "未登录浏览器"}

        limit = self.cfg.webauto_daily_limit
        used = self.store.webauto_used(site_url)
        total = len(urls)
        emit({"type": "log", "level": "info",
              "message": f"开始处理 {total} 条。{site_url} 今日已用 {used}/{limit} 次。"
                         f"整批共用一个浏览器窗口，不再逐条重启"})
        emit({"type": "progress", "phase": "webauto", "done": 0, "total": total})

        stats = {"ok": 0, "failed": 0, "done": 0, "quota_stop": False}

        def take_quota(url: str) -> bool:
            if self.store.webauto_take(site_url, limit):
                return True
            emit({"type": "log", "level": "warn",
                  "message": f"本地每日上限 {limit} 已用完，剩余的留到明天"})
            return False

        def on_result(url: str, res) -> None:
            # 只有真的递交出去才占名额；其余情况一律退还，
            # 否则一次网络抖动就白吃掉一个本来就稀缺的名额
            if not res.ok:
                self.store.webauto_refund(site_url)

            self.store.log(
                "webauto", url, "webauto",
                200 if res.ok else 0, res.message, source="webauto",
            )
            self.store.pending_mark_requested(url, res.status)
            stats["done"] += 1

            if res.status == "challenge":
                emit({"type": "log", "level": "error",
                      "message": "触发 Google 安全验证，已停止整批：" + res.message})
                emit({"type": "log", "level": "error",
                      "message": "请重新走一遍浏览器登录（可能要手动过一次验证）再继续。"})
            elif res.status == "quota_exceeded":
                stats["quota_stop"] = True
                emit({"type": "log", "level": "warn",
                      "message": f"Google 侧已达上限，停止本批：{res.message}"})
            elif res.ok:
                stats["ok"] += 1
                emit({"type": "log", "level": "success", "message": f"{url} → {res.message}"})
            else:
                stats["failed"] += 1
                emit({"type": "log", "level": "error",
                      "message": f"{url} 失败（未占用名额）：{res.message}"})

            emit({"type": "progress", "phase": "webauto",
                  "done": stats["done"], "total": total})

        results = webauto.request_indexing_batch(
            self.cfg.webauto_session_path, site_url, urls,
            headless=self.cfg.webauto_headless,
            on_result=on_result,
            should_stop=should_stop,
            take_quota=take_quota,
            delay=lambda: webauto.random_delay(
                self.cfg.webauto_min_delay, self.cfg.webauto_max_delay
            ),
        )

        skipped = total - stats["done"]
        if skipped > 0 and not stats["quota_stop"]:
            emit({"type": "log", "level": "warn", "message": f"{skipped} 条未处理"})
        emit({"type": "log", "level": "success" if stats["ok"] else "warn",
              "message": f"结束：成功 {stats['ok']} 条，失败 {stats['failed']} 条，"
                         f"未处理 {skipped} 条"})
        return {
            "ok": stats["ok"], "failed": stats["failed"], "skipped": skipped,
            "results": [{"url": u, "status": r.status, "message": r.message}
                        for u, r in results],
            "used_today": self.store.webauto_used(site_url), "limit": limit,
        }

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
        """配额概览。

        只剩下收录预检这一种 API 配额了（Indexing API 已移除）。
        网页点击的名额是按站点算的、跟凭据无关，所以单独由 webauto_overview 提供。
        """
        accounts = []
        for acc in self.pool.accounts:
            accounts.append(
                {
                    "name": acc.name,
                    "email": acc.email,
                    "kind": acc.kind,
                    "kind_cn": acc.kind_cn,
                    "inspect_used": self.store.quota_used(acc.inspect_scope),
                    "inspect_limit": self.cfg.inspect_daily_quota,
                }
            )
        return {
            "accounts": accounts,
            "inspect_used": sum(a["inspect_used"] for a in accounts),
            "inspect_limit": len(accounts) * self.cfg.inspect_daily_quota,
        }

    def webauto_overview(self, site_url: str = "") -> dict:
        """网页点击名额概览：本地闸门用掉多少、还剩多少。

        注意这只是本地记账。Google 那边的真实上限不明，可能比这个更严，
        所以“还剩 N 次”是乐观估计，实际可能提前撞上“超出了配额”。
        """
        limit = self.cfg.webauto_daily_limit
        site = site_url or self.cfg.site_url
        used = self.store.webauto_used(site) if site else 0
        return {
            "site_url": site,
            "used": used,
            "limit": limit,
            "left": max(0, limit - used),
            "logged_in": webauto.has_session(self.cfg.webauto_session_path),
        }
