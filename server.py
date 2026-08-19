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
from pathlib import Path

from fastapi import Body, FastAPI, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from gscindex import config as config_mod
from gscindex import oauth, sources
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
    return JSONResponse({}, status_code=204)


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
                "submit_used": store.quota_used(a.submit_scope),
                "submit_limit": cfg.daily_quota_per_account,
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
    do_inspect = bool(body.get("inspect", cfg.inspect_before_submit))
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


@app.post("/api/submit")
def api_submit(body: dict = Body(...)):
    urls = body.get("urls") or []
    site = (body.get("site_url") or cfg.site_url).strip()
    notif = "URL_DELETED" if body.get("delete") else "URL_UPDATED"
    account = (body.get("account") or "").strip()

    def run(emit):
        return engine.submit(urls, site, notif_type=notif, account_name=account, emit=emit)

    return {"job_id": start_job("submit", run).id}


# --------------------------------------------------------------------------
# 任务查询与 SSE
# --------------------------------------------------------------------------


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
