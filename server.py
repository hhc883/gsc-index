"""GSC 索引提交器 —— 本地 Web 服务。

启动后浏览器访问 http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import queue
import secrets
import sys
import threading
import uuid
import webbrowser
from datetime import date
from pathlib import Path

from fastapi import Body, FastAPI, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from gscindex import config as config_mod
from gscindex import oauth, sources, traffic, webauto
from gscindex.auth import KIND_OAUTH, AccountPool
from gscindex.runner import Engine
from gscindex.store import Store

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

cfg = config_mod.load()
store = Store(cfg.db_path)
pool = AccountPool(cfg.accounts_path, store)
engine = Engine(cfg, store, pool)

app = FastAPI(title="GSC 索引提交器", docs_url=None, redoc_url=None)

# 实际监听端口（--port 可覆盖 config），OAuth 回调地址要用它拼
RUNTIME = {"port": cfg.port}
# 待完成的授权请求：state -> redirect_uri
PENDING_OAUTH: dict[str, str] = {}


def redirect_uri() -> str:
    return "http://127.0.0.1:" + str(RUNTIME["port"]) + "/oauth/callback"


# --------------------------------------------------------------------------
# 任务管理：后台线程跑，前端用 SSE 订阅进度
# --------------------------------------------------------------------------


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.status = "running"      # running / done / error
        self.events: list[dict] = []
        self.result: dict | None = None
        self.error = ""
        self.progress = {"phase": "", "done": 0, "total": 0}
        # 网页自动化任务可能跑很久（每条都要开浏览器），需要能中途停下
        self.stop_flag = threading.Event()
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []

    def emit(self, evt: dict) -> None:
        with self._lock:
            if evt.get("type") == "progress":
                self.progress = {
                    "phase": evt.get("phase", ""),
                    "done": evt.get("done", 0),
                    "total": evt.get("total", 0),
                }
            if evt.get("type") == "log":
                self.events.append(evt)
                if len(self.events) > 600:
                    del self.events[:200]
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(evt)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            for evt in self.events:
                try:
                    q.put_nowait(evt)
                except queue.Full:
                    break
            q.put_nowait({"type": "progress", **self.progress})
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "progress": dict(self.progress),
                "error": self.error,
                "result": self.result,
                "events": list(self.events[-80:]),
            }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def start_job(kind: str, fn) -> Job:
    job = Job(kind)
    with JOBS_LOCK:
        JOBS[job.id] = job
        if len(JOBS) > 40:  # 只保留最近的任务
            for old in sorted(JOBS.values(), key=lambda j: j.id)[:10]:
                if old.status != "running":
                    JOBS.pop(old.id, None)

    def run():
        try:
            # 统一成 fn(emit, stop_flag)。老的只收 emit 的回调也照样能用，
            # 免得为了这一个参数把所有既有端点都改一遍。
            try:
                job.result = fn(job.emit, job.stop_flag)
            except TypeError as exc:
                if "positional argument" not in str(exc):
                    raise
                job.result = fn(job.emit)
            job.status = "done"
        except Exception as exc:  # 任何异常都要让前端看见
            job.status = "error"
            job.error = str(exc)
            job.emit({"type": "log", "level": "error", "message": "任务失败：" + str(exc)})
        finally:
            job.emit({"type": "done", "status": job.status})

    threading.Thread(target=run, daemon=True, name="job-" + job.id).start()
    return job


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/favicon.ico")
def favicon():
    # 204 表示"无内容"，响应体必须为空。
    # 之前返回 JSONResponse({}) 会带上 "{}" 作为响应体，与 Content-Length 声明冲突，
    # 浏览器每次请求 favicon 都会在服务端抛 LocalProtocolError。
    return Response(status_code=204)


# --------------------------------------------------------------------------
# 配置与概览
# --------------------------------------------------------------------------


@app.get("/api/state")
def api_state():
    return {
        "config": cfg.to_dict(),
        "quota": engine.quota_overview(),
        "account_count": len(pool),
        "account_errors": [{"file": f, "error": e} for f, e in pool.errors],
        "stats": store.stats(),
    }


@app.post("/api/config")
def api_config(patch: dict = Body(...)):
    cfg.update(patch)
    cfg.save()
    return {"ok": True, "config": cfg.to_dict()}


@app.get("/api/stats")
def api_stats():
    return {
        **store.stats(),
        "daily": store.daily_series(14),
        "failures": store.failure_reasons(8),
        # 分来源统计，用来对比"网页自动化"和已移除的"旧 Indexing API"的实际效果
        "sources": store.source_breakdown(),
    }


@app.get("/api/logs")
def api_logs(limit: int = 100, offset: int = 0, q: str = ""):
    return store.log_page(limit=min(limit, 500), offset=offset, keyword=q.strip())


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------


@app.get("/api/accounts")
def api_accounts(site_url: str = ""):
    return {"accounts": engine.check_accounts(site_url or cfg.site_url)}


@app.post("/api/accounts/upload")
async def api_accounts_upload(files: list[UploadFile]):
    saved, failed = [], []
    cfg.accounts_path.mkdir(parents=True, exist_ok=True)
    for f in files:
        raw = await f.read()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            failed.append({"file": f.filename, "error": "不是合法的 JSON 文件"})
            continue
        # 拖进来的也可能是 OAuth 客户端配置，直接识别并收下，不要报错赶人走
        if data.get("installed") or data.get("web"):
            try:
                info = oauth.save_client(data)
            except oauth.OAuthError as exc:
                failed.append({"file": f.filename, "error": str(exc)})
                continue
            saved.append(
                {
                    "file": f.filename,
                    "email": "OAuth 客户端",
                    "oauth_client": True,
                    "project_id": info.get("project_id", ""),
                }
            )
            continue
        if data.get("type") != "service_account" or not data.get("client_email"):
            failed.append(
                {
                    "file": f.filename,
                    "error": "既不是服务账号密钥，也不是 OAuth 客户端配置文件",
                }
            )
            continue
        stem = data["client_email"].split("@")[0][:60] or Path(f.filename or "key").stem
        target = cfg.accounts_path / (stem + ".json")
        n = 1
        while target.exists() and json.loads(target.read_text("utf-8")).get(
            "client_email"
        ) != data["client_email"]:
            target = cfg.accounts_path / (stem + "-" + str(n) + ".json")
            n += 1
        target.write_bytes(raw)
        saved.append({"file": target.name, "email": data["client_email"]})
    pool.reload()
    return {"saved": saved, "failed": failed, "count": len(pool)}


@app.delete("/api/accounts/{name}")
def api_accounts_delete(name: str):
    target = cfg.accounts_path / (Path(name).name + ".json")
    if not target.exists() or target.parent.resolve() != cfg.accounts_path.resolve():
        return JSONResponse({"ok": False, "error": "找不到该账号"}, status_code=404)
    # OAuth 凭据顺手通知 Google 撤销，别只删本地文件留个悬空授权
    acc = pool.by_name(Path(name).name)
    if acc is not None and acc.kind == KIND_OAUTH and getattr(acc, "refresh_token", ""):
        oauth.revoke(acc.refresh_token)
    target.unlink()
    pool.reload()
    return {"ok": True, "count": len(pool)}


@app.get("/api/sites")
def api_sites(account: str = ""):
    return {"sites": engine.all_sites(account_name=account)}


# --------------------------------------------------------------------------
# OAuth：以用户本人身份授权
# --------------------------------------------------------------------------


@app.get("/api/oauth/status")
def api_oauth_status():
    client = oauth.load_client()
    return {
        "has_client": bool(client),
        "client_id": (client or {}).get("client_id", "")[:32],
        "client_project": (client or {}).get("project_id", ""),
        "redirect_uri": redirect_uri(),
        "accounts": [
            {
                "name": a.name,
                "email": a.email,
                "project_id": a.project_id,
                "inspect_used": store.quota_used(a.inspect_scope),
                "inspect_limit": cfg.inspect_daily_quota,
            }
            for a in pool.accounts
            if a.kind == KIND_OAUTH
        ],
    }


@app.post("/api/oauth/start")
def api_oauth_start():
    if not oauth.load_client():
        return JSONResponse(
            {"error": "请先上传 OAuth 客户端配置文件（在 GCP 创建「桌面应用」类型的客户端 ID）"},
            status_code=400,
        )
    state = secrets.token_urlsafe(24)
    uri = redirect_uri()
    PENDING_OAUTH.clear()  # 同时只允许一个授权流程，避免状态混乱
    PENDING_OAUTH[state] = uri
    try:
        return {"auth_url": oauth.build_auth_url(uri, state), "redirect_uri": uri}
    except oauth.OAuthError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def _callback_page(title: str, body: str, ok: bool) -> HTMLResponse:
    color = "#15803d" if ok else "#c02626"
    return HTMLResponse(
        "<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
        "<title>" + title + "</title><style>"
        "body{font:15px/1.7 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
        "background:#f5f6f8;color:#1a1d23;display:flex;align-items:center;"
        "justify-content:center;min-height:100vh;margin:0;padding:24px}"
        ".b{background:#fff;border:1px solid #e2e5ea;border-radius:12px;padding:32px 36px;"
        "max-width:560px;box-shadow:0 2px 8px rgba(16,24,40,.07)}"
        "h1{font-size:19px;margin:0 0 12px;color:" + color + "}"
        "p{margin:0 0 10px;color:#4b5563}code{background:#f1f3f5;padding:2px 6px;border-radius:4px;"
        "font-size:13px;word-break:break-all}</style></head><body><div class=b>"
        "<h1>" + title + "</h1>" + body + "</div></body></html>",
        status_code=200 if ok else 400,
    )


@app.get("/oauth/callback")
def oauth_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return _callback_page(
            "授权被取消",
            "<p>Google 返回：<code>" + error + "</code></p>"
            "<p>关掉本页回到工具，可以重新点一次授权。</p>",
            False,
        )
    uri = PENDING_OAUTH.pop(state, None)
    if not code or not uri:
        return _callback_page(
            "授权链接已失效",
            "<p>这个授权链接已经用过或已过期。请关掉本页，回到工具重新点一次「开始授权」。</p>",
            False,
        )
    try:
        creds = oauth.exchange_code(code, uri)
    except oauth.OAuthError as exc:
        return _callback_page("授权失败", "<p>" + str(exc) + "</p>", False)

    cfg.accounts_path.mkdir(parents=True, exist_ok=True)
    target = cfg.accounts_path / oauth.account_filename(creds)
    target.write_text(json.dumps(creds, indent=2, ensure_ascii=False), encoding="utf-8")
    pool.reload()
    who = creds.get("email") or "你的 Google 账号"
    return _callback_page(
        "授权成功",
        "<p>已连接 <code>" + who + "</code>。</p>"
        "<p>你名下这个账号的全部 GSC 属性现在都可以直接使用，"
        "不需要在任何站点里添加权限。</p>"
        "<p>关掉本页回到工具，点「刷新」就能看到站点列表。</p>",
        True,
    )


@app.delete("/api/oauth/client")
def api_oauth_client_clear():
    oauth.clear_client()
    return {"ok": True}


# --------------------------------------------------------------------------
# 站点地图
# --------------------------------------------------------------------------


@app.post("/api/sitemap/fetch")
def api_sitemap_fetch(body: dict = Body(...)):
    url = (body.get("url") or "").strip()

    def run(emit):
        emit({"type": "log", "level": "info", "message": "开始抓取站点地图 " + url})

        def progress(current, found):
            emit({"type": "log", "level": "info", "message": f"读取 {current}（已收集 {found} 条）"})

        urls, visited, errors = sources.fetch_sitemap(url, on_progress=progress)
        for e in errors:
            emit({"type": "log", "level": "warn", "message": e})
        if not visited:
            emit({"type": "log", "level": "error", "message": "一个站点地图文件都没读到，请检查上面的报错"})
        elif not urls:
            emit({"type": "log", "level": "warn",
                  "message": f"读到 {len(visited)} 个文件，但里面没有任何 URL"})
        else:
            emit(
                {
                    "type": "log",
                    "level": "success",
                    "message": f"抓取完成：{len(visited)} 个站点地图文件，共 {len(urls)} 条 URL",
                }
            )
        return {"urls": urls, "visited": visited, "errors": errors}

    return {"job_id": start_job("sitemap_fetch", run).id}


@app.post("/api/sitemap/submit")
def api_sitemap_submit(body: dict = Body(...)):
    site = (body.get("site_url") or cfg.site_url).strip()
    url = (body.get("url") or "").strip()
    ok, msg = engine.sitemap_submit(site, url)
    return {"ok": ok, "message": msg}


@app.get("/api/sitemap/list")
def api_sitemap_list(site_url: str = ""):
    ok, items, err = engine.sitemap_list(site_url or cfg.site_url)
    return {"ok": ok, "sitemaps": items, "error": err}


# --------------------------------------------------------------------------
# 分析与提交
# --------------------------------------------------------------------------


@app.post("/api/analyze")
def api_analyze(body: dict = Body(...)):
    text = body.get("text") or ""
    urls = body.get("urls") or []
    site = (body.get("site_url") or cfg.site_url).strip()
    do_inspect = bool(body.get("inspect", True))
    force = bool(body.get("force", False))
    account = (body.get("account") or "").strip()

    if text:
        parsed, bad = sources.parse_text(text)
        urls = list(urls) + parsed
    else:
        bad = 0

    def run(emit):
        if bad:
            emit({"type": "log", "level": "warn", "message": f"{bad} 行无法识别为 URL，已忽略"})
        return engine.analyze(
            urls, site, do_inspect=do_inspect, force=force, account_name=account, emit=emit
        )

    return {"job_id": start_job("analyze", run).id}


@app.post("/api/scan")
def api_scan(body: dict = Body(...)):
    """全站扫描：抓站点地图 -> 批量预检 -> 未收录的进待办池。"""
    site = (body.get("site_url") or cfg.site_url).strip()
    sitemap = (body.get("sitemap_url") or "").strip()
    account = (body.get("account") or "").strip()
    limit = int(body.get("limit") or cfg.scan_limit or 0)
    if not site:
        return JSONResponse({"error": "请先选择站点"}, status_code=400)
    if not sitemap:
        return JSONResponse({"error": "请填写站点地图地址"}, status_code=400)

    def run(emit, _stop):
        return engine.scan(site, sitemap, account_name=account, limit=limit, emit=emit)

    return {"job_id": start_job("scan", run).id}


@app.get("/api/links")
def api_links(
    site_url: str = "",
    index_state: str = "",
    requested: str = "",
    all_sites: bool = False,
):
    """站点链接清单：扫描发现的全部 URL，不管有没有收录。

    index_state 传 indexed / not_indexed / unknown 做筛选，空表示全部。
    requested 传 yes / no 按有没有通过本工具申请过筛选。
    all_sites=True 返回全部站点——不能用"site_url 为空就回退到 cfg.site_url"，
    那样"全站点明细"永远只能拿到当前站点的数据。
    """
    site = "" if all_sites else (site_url or cfg.site_url)
    return {
        "rows": store.url_list(site, index_state=index_state, requested=requested),
        "counts": store.url_counts(),
        "webauto": engine.webauto_overview(site or cfg.site_url),
        "recheck_pending": len(store.urls_to_recheck(site)),
    }


def _window_dates(w: int) -> dict:
    """这个窗口下 GSC 和 GA 各自实际查询的日期区间。

    两边不一样，而且差得不小：GSC 末端要往前推 GSC_LAG_DAYS 天（它没有更新的数据），
    GA 可以一直查到今天。窗口为 1 天时这个差别最刺眼——GA 是今天，GSC 是三天前，
    界面必须把两个日期分别标出来，否则同一行里的数字看起来是同一天的。
    """
    gs, ge = traffic.window(w, lag=traffic.GSC_LAG_DAYS)
    as_, ae = traffic.window(w)
    return {"gsc": {"start": gs, "end": ge}, "ga": {"start": as_, "end": ae},
            "today": date.today().isoformat(), "gsc_lag_days": traffic.GSC_LAG_DAYS}


@app.get("/api/traffic")
def api_traffic(
    window_days: int = 0,
    metric: str = "impressions",
    op: str = "gte",
    value: float | None = None,
    value2: float | None = None,
    include_unfetched: bool = False,
):
    """流量排行与筛选。

    「哪些站流量过 1000」就是这个接口：metric=impressions&op=gte&value=1000。
    两边的 Google 接口都不支持按指标筛（只支持按维度），所以筛选在本地做——
    数据缓存下来之后筛选是瞬时的，阈值随便改、条件能任意组合。
    """
    w = window_days or cfg.traffic_window_days
    try:
        rows = store.traffic_rank(
            w, metric=metric, op=op, value=value, value2=value2,
            only_fetched=not include_unfetched,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {
        "rows": rows,
        "window_days": w,
        # 这个窗口两边各自实际查询的日期区间。GSC 有 2~3 天延迟，"今日"窗口下
        # 它给的是三天前那一天而不是今天——不把真实日期交给界面标出来，
        # 用户会把 GSC 那几列当成今天的数字看。
        "dates": _window_dates(w),
        # 每个窗口各有多少数据。流量按窗口分别缓存，在「近 7 天」拉的数据切到
        # 「近 28 天」是看不到的；把这份汇总交给界面，才能把"数据在另一个窗口"
        # 和"真的没数据"区分开，不然空表看起来就像功能坏了。
        "windows": store.traffic_windows(),
        "metric": metric,
        "analytics": engine.analytics_ready(),
        "ga_mapped": len(cfg.ga_property_map or {}),
    }


@app.get("/api/traffic/site")
def api_traffic_site(site_url: str = "", window_days: int = 0):
    """单站点详情：总量 + 按天趋势 + 页面明细 + 搜索词。"""
    site = site_url or cfg.site_url
    w = window_days or cfg.traffic_window_days
    return {
        "site_url": site,
        "window_days": w,
        "totals": store.traffic_row(site, w),
        "daily": store.daily_series_traffic(site, w),
        "pages": store.page_traffic_list(site, w)[:300],
        "queries": store.query_traffic_list(site, w, limit=200),
    }


@app.get("/api/traffic/links")
def api_traffic_links(site_url: str = "", window_days: int = 0):
    """链接清单 + 流量，用来找"已收录但零展现"的页面。"""
    site = site_url or cfg.site_url
    w = window_days or cfg.traffic_window_days
    return {"rows": store.links_with_traffic(site, w), "window_days": w}


@app.post("/api/traffic/refresh")
def api_traffic_refresh(body: dict = Body(...)):
    """拉取流量数据。不传 sites 就拉全部站点（79 个约需几分钟）。"""
    sites = body.get("sites")
    w = int(body.get("window_days") or cfg.traffic_window_days)
    account = (body.get("account") or "").strip()
    detail = bool(body.get("with_detail"))

    def run(emit, _stop):
        return engine.traffic_refresh(
            sites, window_days=w, account_name=account, with_detail=detail, emit=emit
        )

    return {"job_id": start_job("traffic", run).id}


@app.post("/api/traffic/realtime")
def api_traffic_realtime(body: dict = Body(...)):
    """最近 30 分钟的实时活跃。只有 GA 有这个，GSC 完全给不了。

    同步返回而不是丢后台任务：实时数据的意义就在于立刻看到。
    """
    sites = body.get("sites") or ([cfg.site_url] if cfg.site_url else [])
    account = (body.get("account") or "").strip()
    top = max(1, min(int(body.get("top") or 5), 20))
    return engine.traffic_realtime(list(sites), account_name=account, top=top)


@app.post("/api/ga/discover")
def api_ga_discover(body: dict = Body(...)):
    """自动发现 GA4 媒体资源并跟站点配对。

    靠数据流里绑定的真实网站地址来配，不用手抄几十个媒体资源 ID。
    """
    account = (body.get("account") or "").strip()

    def run(emit, _stop):
        return engine.ga_discover(account_name=account, emit=emit)

    return {"job_id": start_job("ga_discover", run).id}


@app.post("/api/ga/mapping")
def api_ga_mapping(body: dict = Body(...)):
    """保存站点与 GA4 属性的对应关系。"""
    mapping = body.get("mapping")
    if not isinstance(mapping, dict):
        return JSONResponse({"error": "mapping 必须是对象"}, status_code=400)
    # 只留纯数字的媒体资源 ID。跟踪代码里的 G-XXXXXXXXXX 是"衡量 ID"，
    # 调 Data API 用它必然失败，这里直接拦掉免得用户白等一轮报错。
    clean, rejected = {}, []
    for site, pid in mapping.items():
        pid = str(pid).strip()
        if not pid:
            continue
        if pid.isdigit():
            clean[site] = pid
        else:
            rejected.append({"site": site, "value": pid})
    cfg.ga_property_map = clean
    cfg.save()
    return {"ok": True, "saved": len(clean), "rejected": rejected}


@app.post("/api/recheck")
def api_recheck(body: dict = Body(...)):
    """复查收录状态：重新查询这些链接现在收录了没。

    不传 urls 时默认复查"申请过、但还没确认收录"的那些。
    只花查询配额，不消耗申请名额。
    """
    site = (body.get("site_url") or cfg.site_url).strip()
    urls = body.get("urls")
    account = (body.get("account") or "").strip()
    if not site:
        return JSONResponse({"error": "请先选择站点"}, status_code=400)

    def run(emit, _stop):
        return engine.recheck(site, urls, account_name=account, emit=emit)

    return {"job_id": start_job("recheck", run).id}


@app.post("/api/webauto/submit")
def api_webauto_submit(body: dict = Body(...)):
    """把选中的 URL 交给浏览器自动化，逐个去 GSC 网页点“请求编入索引”。"""
    urls = body.get("urls") or []
    site = (body.get("site_url") or cfg.site_url).strip()
    if not site:
        return JSONResponse({"error": "请先选择站点"}, status_code=400)
    if not urls:
        return JSONResponse({"error": "没有要提交的 URL"}, status_code=400)
    if not webauto.has_session(cfg.webauto_session_path):
        return JSONResponse(
            {"error": "还没有浏览器登录，请先到「账号管理」完成一次浏览器登录"},
            status_code=400,
        )

    def run(emit, stop_flag):
        return engine.webauto_submit(
            site, urls, emit=emit, should_stop=stop_flag.is_set
        )

    return {"job_id": start_job("webauto", run).id}


@app.get("/api/webauto/status")
def api_webauto_status(site_url: str = ""):
    return engine.webauto_overview(site_url or cfg.site_url)


@app.post("/api/webauto/login")
def api_webauto_login():
    """打开本机真实 Chrome，由用户本人手动登录 Google 账号。"""
    def run(emit, _stop):
        emit({"type": "log", "level": "info",
              "message": "已打开浏览器窗口，请在里面用你自己的 Google 账号登录……"})
        ok, msg = webauto.bootstrap_login(cfg.webauto_session_path)
        emit({"type": "log", "level": "success" if ok else "error", "message": msg})
        return {"ok": ok, "message": msg}

    return {"job_id": start_job("webauto_login", run).id}


@app.delete("/api/webauto/login")
def api_webauto_logout():
    """清除浏览器登录状态。整个配置目录删掉才算真正登出。"""
    import shutil

    cfg.webauto_session_path.unlink(missing_ok=True)
    prof = webauto.profile_dir(cfg.webauto_session_path)
    if prof.exists():
        webauto.kill_stale_browsers(prof)  # 先清进程，否则目录被占着删不掉
        shutil.rmtree(prof, ignore_errors=True)
    return {"ok": True}


# --------------------------------------------------------------------------
# 任务查询与 SSE
# --------------------------------------------------------------------------


@app.post("/api/job/{job_id}/stop")
def api_job_stop(job_id: str):
    """请求停止任务。网页自动化一条条跑、可能很久，必须能中途叫停。"""
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    job.stop_flag.set()
    return {"ok": True}


@app.get("/api/job/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    return job.snapshot()


@app.get("/api/job/{job_id}/events")
def api_job_events(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "任务不存在"}, status_code=404)

    def stream():
        q = job.subscribe()
        try:
            while True:
                try:
                    evt = q.get(timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"          # 心跳，防代理断连
                    if job.status != "running":
                        break
                    continue
                yield "data: " + json.dumps(evt, ensure_ascii=False) + "\n\n"
                if evt.get("type") == "done":
                    break
        finally:
            job.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------


def main() -> None:
    import uvicorn

    port = cfg.port
    # 允许 --port 覆盖，方便同时开多个实例或临时避开端口冲突
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            sys.exit("--port 后面要跟一个端口号，例如 --port 8790")
    RUNTIME["port"] = port  # OAuth 回调地址要用真实端口
    url = "http://127.0.0.1:" + str(port)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print("=" * 56)
    print("  GSC 索引提交器已启动")
    print("  请在浏览器打开： " + url)
    print("  关闭本窗口即停止服务")
    print("=" * 56)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
