"""引擎层：账号自检、全站扫描、收录预检、网页自动化提交调度。

所有耗时操作都通过 on_event 回调向外吐进度，Web 层据此推 SSE。

两条链路性质不同，别混淆：
* api.py     —— Google 官方开放接口（查站点、查收录、交站点地图），配额充裕
* webauto.py —— 自动化操作 GSC 网页点“请求编入索引”，灰色地带、名额稀缺
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from . import api, oauth, sources, traffic, webauto
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

        # 站点地图里的每一条都入库，包括查不出状态的——"全部"栏必须真的是
        # 这个站点的全部链接，不然用户没法拿它当清单用。
        # index_state 表达 Google 的判定，跟有没有通过本工具申请过完全无关。
        added = resolved = unknown = 0
        for row in res["rows"]:
            if row["state"] == STATE_INDEXED:
                state = self.store.INDEXED
                resolved += 1
            elif row["state"] == STATE_PENDING:
                state = self.store.NOT_INDEXED
                added += 1
            else:
                # 预检失败或没查（配额不足等），状态不明确——如实标 unknown，
                # 不猜成"未收录"，否则会误导用户去申请本来可能已经收录的页面
                state = self.store.UNKNOWN
                unknown += 1
            self.store.url_upsert(
                site_url, row["url"], state, row["coverage"], row["verdict"]
            )

        msg = f"扫描完成：共 {len(res['rows'])} 条 —— 已收录 {resolved} 条、未收录 {added} 条"
        if unknown:
            msg += f"、状态未查明 {unknown} 条"
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
            self.store.url_mark_requested(url, res.status)
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
    # 复查收录状态
    # ------------------------------------------------------------------

    def recheck(
        self,
        site_url: str,
        urls: list[str] | None = None,
        *,
        account_name: str = "",
        emit: Emit = _noop,
    ) -> dict:
        """重新查询这些链接现在到底收录了没，更新清单里的状态。

        典型用法：申请收录过几天之后回来复查，看 Google 到底收了没有。
        不传 urls 时默认复查"申请过、但目前还没确认收录"的那些。

        这个动作只花查询配额（2000 次/天/凭据，充裕），
        **不消耗申请名额**，所以想查多少次都行。
        """
        if urls is None:
            urls = self.store.urls_to_recheck(site_url)
            if not urls:
                emit({"type": "log", "level": "info",
                      "message": "没有需要复查的链接（申请过且还没确认收录的都查完了）"})
                return {"checked": 0, "newly_indexed": 0, "still_not": 0, "unknown": 0}
            emit({"type": "log", "level": "info",
                  "message": f"复查 {len(urls)} 条申请过但还没确认收录的链接"})
        else:
            emit({"type": "log", "level": "info", "message": f"复查指定的 {len(urls)} 条链接"})

        res = self.analyze(
            urls, site_url, do_inspect=True, force=True,
            account_name=account_name, emit=emit,
        )

        newly_indexed = still_not = unknown = 0
        for row in res["rows"]:
            before = self.store.url_row_state(row["url"])
            if row["state"] == STATE_INDEXED:
                state = self.store.INDEXED
                if before != self.store.INDEXED:
                    newly_indexed += 1
                    emit({"type": "log", "level": "success",
                          "message": "已收录：" + row["url"]})
            elif row["state"] == STATE_PENDING:
                state = self.store.NOT_INDEXED
                still_not += 1
            else:
                state = self.store.UNKNOWN
                unknown += 1
            self.store.url_upsert(
                site_url, row["url"], state, row["coverage"], row["verdict"]
            )

        emit({"type": "log", "level": "success",
              "message": f"复查完成：新确认收录 {newly_indexed} 条，"
                         f"仍未收录 {still_not} 条"
                         + (f"，未查明 {unknown} 条" if unknown else "")})
        return {
            "checked": len(res["rows"]), "newly_indexed": newly_indexed,
            "still_not": still_not, "unknown": unknown,
        }

    # ------------------------------------------------------------------
    # 流量数据（GSC Search Analytics + GA4）
    # ------------------------------------------------------------------

    def _traffic_token(self, account_name: str = "", need_analytics: bool = False):
        """挑一个能用的凭据取 token。

        need_analytics=True 时只认拿到过 analytics 权限的凭据——老凭据没有这个
        权限，硬拿去调 GA 会被 Google 拒，不如提前说清楚要重新授权。
        """
        cands = self.pool.accounts
        if account_name:
            acc = self.pool.by_name(account_name)
            cands = [acc] if acc else []
        for acc in cands:
            if acc.kind != KIND_OAUTH:
                continue  # 服务账号拿不到 GA 数据，GSC 流量也走 OAuth 更省事
            if need_analytics and not getattr(acc, "has_scope", lambda _s: False)(
                oauth.SCOPE_ANALYTICS
            ):
                continue
            try:
                return acc, acc.token()
            except AuthError:
                continue
        return None, None

    def analytics_ready(self) -> dict:
        """GA 是否可用。界面据此提示"需要重新授权"，而不是等调用失败才报错。"""
        oauth_accs = [a for a in self.pool.accounts if a.kind == KIND_OAUTH]
        with_ga = [
            a for a in oauth_accs
            if getattr(a, "has_scope", lambda _s: False)(oauth.SCOPE_ANALYTICS)
        ]
        return {
            "has_oauth": bool(oauth_accs),
            "ga_ready": bool(with_ga),
            "accounts_with_ga": [a.name for a in with_ga],
            "accounts_without_ga": [a.name for a in oauth_accs if a not in with_ga],
        }

    def traffic_realtime(self, sites: list[str], *, account_name: str = "",
                         top: int = 5) -> dict:
        """最近 30 分钟的实时活跃，按站点逐个查。

        同步返回、不走任务系统：实时数据的价值就在于立刻看到，
        丢进后台任务再等 SSE 推回来反而更慢，而且站点数一般只有几个。
        """
        _acc, token = self._traffic_token(account_name, need_analytics=True)
        if not token:
            return {"error": "没有拿到 GA 权限的凭据，请到「账号管理」重新授权一次。",
                    "rows": []}
        pmap = self.cfg.ga_property_map or {}
        todo, missing = [], []
        for site in sites:
            pid = pmap.get(site)
            (todo.append((site, pid)) if pid else missing.append(site))

        # 并发拿总数。串行的话 79 个站点要跑几分钟——实时数据的全部价值
        # 就在于立刻看到，慢了就没意义了。
        # 这一步刻意不要页面明细：绝大多数站此刻根本没人在线，
        # 给每个站都多要一次明细就是几十次白花的请求。
        def totals(item):
            site, pid = item
            rt = traffic.ga_realtime(
                token, pid, with_pages=False, timeout=self.cfg.request_timeout
            )
            return {
                "site": site, "property_id": pid, "ok": rt.ok,
                "active_users": rt.active_users, "views": rt.views,
                "events": rt.events, "top_pages": [], "message": rt.message,
            }

        rows: list[dict] = []
        if todo:
            with ThreadPoolExecutor(
                max_workers=min(max(1, self.cfg.concurrency * 2), len(todo))
            ) as ex:
                rows = [f.result() for f in as_completed(
                    [ex.submit(totals, it) for it in todo]
                )]

        # 第二趟：只给真的有人在线的站点要页面明细
        hot = [r for r in rows if r["ok"] and r["active_users"] > 0]
        if hot:
            def pages(row):
                row["top_pages"] = traffic.ga_realtime_pages(
                    token, row["property_id"], top=top,
                    timeout=self.cfg.request_timeout
                )

            with ThreadPoolExecutor(
                max_workers=min(max(1, self.cfg.concurrency * 2), len(hot))
            ) as ex:
                list(as_completed([ex.submit(pages, r) for r in hot]))

        # 按此刻在线人数排，最热的站点排最前
        rows.sort(key=lambda r: (r["ok"], r["active_users"]), reverse=True)
        return {"rows": rows, "unmapped": missing,
                "checked": len(rows), "live": len(hot)}

    def ga_discover(self, *, account_name: str = "", emit: Emit = _noop) -> dict:
        """列出账号下全部 GA4 属性，并自动跟 GSC 站点配对。

        为什么能自动配：GA4 的数据流里存着这个属性实际绑定的网站地址，
        拿它跟 GSC 属性比主机名就能对上，比让用户手抄几十个媒体资源 ID 靠谱得多。
        （注意跟踪代码里的 G-XXXXXXXXXX 是"衡量 ID"，不能用来调 Data API，
        调 API 要的是纯数字的"媒体资源 ID"——这是最常见的坑。）
        """
        acc, token = self._traffic_token(account_name, need_analytics=True)
        if not token:
            msg = ("没有拿到 GA 权限的凭据。请到「账号管理」重新授权一次——"
                   "新增了读取 GA 数据的权限，必须重新走一遍授权流程。")
            emit({"type": "log", "level": "error", "message": msg})
            return {"error": msg, "properties": [], "mapping": {}}

        emit({"type": "log", "level": "info", "message": "正在列出 GA4 媒体资源…"})
        ok, props, err = traffic.ga_list_properties(token, timeout=self.cfg.request_timeout)
        if not ok:
            emit({"type": "log", "level": "error", "message": "读取失败：" + err})
            return {"error": err, "properties": [], "mapping": {}}
        if not props:
            msg = "这个 Google 账号下没有任何 GA4 媒体资源"
            emit({"type": "log", "level": "warn", "message": msg})
            return {"error": msg, "properties": [], "mapping": {}}

        emit({"type": "log", "level": "success",
              "message": f"找到 {len(props)} 个 GA4 媒体资源，正在读取各自绑定的网站地址…"})
        emit({"type": "progress", "phase": "ga_discover", "done": 0, "total": len(props)})

        # 数据流要逐个属性查，属性多时较慢，所以并发拉
        def fill(prop):
            ok2, uris, mids, _err = traffic.ga_property_streams(
                token, prop.property_id, timeout=self.cfg.request_timeout
            )
            if ok2:
                prop.stream_uris = uris
                prop.measurement_ids = mids
            return prop

        done = 0
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.concurrency)) as ex:
            for _ in as_completed([ex.submit(fill, p) for p in props]):
                done += 1
                if done % 5 == 0 or done == len(props):
                    emit({"type": "progress", "phase": "ga_discover",
                          "done": done, "total": len(props)})

        sites = [s["site_url"] for s in self.all_sites(account_name=account_name)]
        mapping, unmatched_props, unmatched_sites = traffic.ga_match_sites(props, sites)

        emit({"type": "log", "level": "success",
              "message": f"自动配对成功 {len(mapping)} 个站点"
                         + (f"，{len(unmatched_sites)} 个站点没找到对应属性" if unmatched_sites else "")
                         + (f"，{len(unmatched_props)} 个属性没配上站点" if unmatched_props else "")})

        return {
            "properties": [
                {
                    "property_id": p.property_id,
                    "display_name": p.display_name,
                    "account_name": p.account_name,
                    "stream_uris": p.stream_uris,
                    "measurement_ids": p.measurement_ids,
                }
                for p in props
            ],
            "mapping": mapping,
            "unmatched_sites": unmatched_sites,
            "unmatched_properties": [p.property_id for p in unmatched_props],
        }

    def traffic_refresh(
        self,
        sites: list[str] | None = None,
        *,
        window_days: int = 28,
        account_name: str = "",
        with_detail: bool = False,
        emit: Emit = _noop,
    ) -> dict:
        """拉取流量数据并缓存到本地。

        为什么必须先拉后筛：GSC 和 GA 的接口都只支持按维度筛（页面、查询词…），
        **不支持按指标筛**——没法跟 Google 说"把点击量大于 1000 的站点给我"。
        所以只能逐站点拉总量、存本地，再在本地任意筛选排序。
        好处是筛选瞬时、阈值随便改、条件能任意组合。

        with_detail=True 时额外拉按页面/搜索词/按天的明细（请求量大得多，
        适合只对单个站点用）。
        """
        if sites is None:
            sites = [s["site_url"] for s in self.all_sites(account_name=account_name)]
        if not sites:
            emit({"type": "log", "level": "error", "message": "没有可用的站点"})
            return {"error": "没有可用的站点", "updated": 0}

        acc, token = self._traffic_token(account_name)
        if not token:
            msg = "没有可用的 OAuth 凭据，请先到「账号管理」完成 OAuth 登录"
            emit({"type": "log", "level": "error", "message": msg})
            return {"error": msg, "updated": 0}

        ga_ready = acc is not None and getattr(acc, "has_scope", lambda _s: False)(
            oauth.SCOPE_ANALYTICS
        )
        ga_map = self.cfg.ga_property_map or {}
        if not ga_ready:
            emit({"type": "log", "level": "warn",
                  "message": "当前凭据没有 GA 权限，本次只拉 GSC 流量。"
                             "想要 GA 数据请到「账号管理」重新授权一次。"})
        elif not ga_map:
            emit({"type": "log", "level": "warn",
                  "message": "还没配置站点与 GA4 属性的对应关系，本次只拉 GSC 流量。"
                             "可以到「流量数据」页点「自动匹配 GA 属性」。"})

        total = len(sites)
        emit({"type": "log", "level": "info",
              "message": f"开始拉取 {total} 个站点近 {window_days} 天的流量"
                         f"（GSC 数据有 2~3 天延迟，窗口已相应前移）"})
        emit({"type": "progress", "phase": "traffic", "done": 0, "total": total})

        stats = {"done": 0, "gsc_ok": 0, "ga_ok": 0, "failed": 0}

        def pull(site: str):
            gsc = traffic.gsc_totals(token, site, days=window_days,
                                     timeout=self.cfg.request_timeout)
            ga = None
            pid = ga_map.get(site, "")
            if ga_ready and pid:
                ga = traffic.ga_totals(token, pid, days=window_days,
                                       timeout=self.cfg.request_timeout)
            return site, gsc, ga, pid

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.concurrency)) as ex:
            for fut in as_completed([ex.submit(pull, s) for s in sites]):
                site, gsc, ga, pid = fut.result()
                self.store.traffic_upsert(site, window_days, gsc=gsc, ga=ga, ga_property=pid)
                stats["done"] += 1
                if gsc.ok:
                    stats["gsc_ok"] += 1
                else:
                    stats["failed"] += 1
                    emit({"type": "log", "level": "error",
                          "message": f"{site} GSC 拉取失败：{gsc.message[:90]}"})
                if ga is not None:
                    if ga.ok:
                        stats["ga_ok"] += 1
                    else:
                        emit({"type": "log", "level": "error",
                              "message": f"{site} GA 拉取失败：{ga.message[:90]}"})
                emit({"type": "progress", "phase": "traffic",
                      "done": stats["done"], "total": total})

        # 明细只对少量站点拉——按页面/搜索词拆分的响应比总量大得多
        if with_detail:
            for site in sites[:5]:
                self._traffic_detail(token, site, window_days, ga_map.get(site, ""),
                                     ga_ready, emit)

        emit({"type": "log", "level": "success",
              "message": f"完成：GSC 成功 {stats['gsc_ok']}/{total}"
                         + (f"，GA 成功 {stats['ga_ok']}" if ga_ready and ga_map else "")})
        return {"updated": stats["done"], "gsc_ok": stats["gsc_ok"],
                "ga_ok": stats["ga_ok"], "failed": stats["failed"],
                "window_days": window_days}

    def _traffic_detail(self, token, site, window_days, pid, ga_ready, emit) -> None:
        """拉单个站点的页面/搜索词/按天明细。"""
        ok, pages, err = traffic.gsc_breakdown(
            token, site, "page", window_days, timeout=self.cfg.request_timeout
        )
        if ok:
            for row in pages:
                self.store.page_traffic_upsert(site, row["key"], window_days, row)
            emit({"type": "log", "level": "info",
                  "message": f"{site} 页面明细 {len(pages)} 条"})
        elif err:
            emit({"type": "log", "level": "warn", "message": f"{site} 页面明细失败：{err[:80]}"})

        ok, queries, err = traffic.gsc_breakdown(
            token, site, "query", window_days, timeout=self.cfg.request_timeout
        )
        if ok:
            for row in queries:
                self.store.query_traffic_upsert(site, row["key"], window_days, row)
            emit({"type": "log", "level": "info",
                  "message": f"{site} 搜索词 {len(queries)} 条"})

        ok, daily, err = traffic.gsc_daily(
            token, site, window_days, timeout=self.cfg.request_timeout
        )
        if ok:
            for row in daily:
                self.store.daily_upsert(site, row["date"],
                                        impressions=row["impressions"], clicks=row["clicks"])

        if ga_ready and pid:
            ok, garows, err = traffic.ga_breakdown(
                token, pid, "date", window_days, timeout=self.cfg.request_timeout
            )
            if ok:
                for row in garows:
                    d = row["key"]
                    if len(d) == 8:   # GA 返回 YYYYMMDD，转成 YYYY-MM-DD 跟 GSC 对齐
                        d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                    self.store.daily_upsert(site, d,
                                            sessions=row["sessions"], users=row["users"])

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
