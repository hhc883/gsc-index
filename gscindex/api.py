"""Google Indexing API / Search Console API 客户端。

只依赖 requests + google-auth，batch multipart 手工组包与拆包。
"""

from __future__ import annotations

import email
import json
import random
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

INDEXING_PUBLISH = "https://indexing.googleapis.com/v3/urlNotifications:publish"
INDEXING_BATCH = "https://indexing.googleapis.com/batch"
INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
WEBMASTERS = "https://www.googleapis.com/webmasters/v3"

BATCH_BOUNDARY = "gsc-index-batch-boundary"
RETRYABLE = {429, 500, 502, 503, 504}

_session_local = threading.local()


def _session() -> requests.Session:
    """每线程一个 Session，复用 TCP 连接。"""
    s = getattr(_session_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = "gsc-index/1.0"
        _session_local.s = s
    return s


@dataclass
class UrlResult:
    url: str
    ok: bool
    status: int
    message: str = ""


def _sleep_backoff(attempt: int, retry_after: str | None = None) -> None:
    if retry_after:
        try:
            time.sleep(min(60.0, float(retry_after)))
            return
        except ValueError:
            pass
    time.sleep(min(30.0, (2**attempt) + random.random()))


def _request(
    method: str,
    url: str,
    token: str,
    *,
    json_body: dict | None = None,
    data: str | None = None,
    content_type: str | None = None,
    timeout: int = 45,
    max_retries: int = 4,
) -> requests.Response:
    """带指数退避重试的请求。429/5xx 重试，其余直接返回。"""
    headers = {"Authorization": "Bearer " + token}
    if content_type:
        headers["Content-Type"] = content_type
    body = data.encode("utf-8") if isinstance(data, str) else data
    last: requests.Response | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = _session().request(
                method, url, headers=headers, json=json_body, data=body, timeout=timeout
            )
        except requests.RequestException:
            if attempt >= max_retries:
                raise
            _sleep_backoff(attempt)
            continue
        last = resp
        if resp.status_code in RETRYABLE and attempt < max_retries:
            _sleep_backoff(attempt, resp.headers.get("Retry-After"))
            continue
        return resp
    return last  # type: ignore[return-value]


# Google 的 API 报错同样需要翻译，否则用户不知道该去改什么
API_HINTS = [
    ("failed to verify the url ownership",
     "该服务账号不是这个站点的所有者。请到 Search Console 的「设置 → 用户和权限」把它的邮箱添加为「所有者」。"),
    ("has not been used in project",
     "该 GCP 项目还没有启用所需的 API，请按报错里的链接点开并启用，等 1~2 分钟后重试。"),
    ("service_disabled",
     "该 GCP 项目的 API 处于停用状态，请到 Google Cloud 控制台启用后重试。"),
    ("does not have sufficient permission",
     "服务账号对这个站点属性没有权限，或权限级别不够。注意 GSC 的权限是"
     "按属性逐个授予的：「域名属性」sc-domain:example.com 与「网址前缀属性」"
     "https://example.com/ 是两个互不相通的属性，加了一个不等于另一个也有权限。"
     "请到「账号管理」页看这个账号实际能看到哪些属性，并确认选的就是其中之一。"),
    ("requested entity was not found",
     "找不到该资源。请确认站点属性写法正确（域名属性形如 sc-domain:example.com）。"),
    ("resource_exhausted",
     "配额已耗尽。Indexing API 每个 GCP 项目每天 200 条，可新建项目再加一个服务账号来扩容。"),
    ("quota exceeded",
     "配额已耗尽。可以明天再试，或新建 GCP 项目增加服务账号。"),
    ("rate_limit_exceeded",
     "请求过于频繁，已触发限流。请降低设置里的并发数。"),
    ("invalid attribute",
     "URL 格式不被接受。请确认是完整的 http(s) 地址，且与站点属性匹配。"),
    ("permission denied",
     "权限被拒绝。请确认服务账号已在 Search Console 中添加为「所有者」，且相关 API 已启用。"),
]


def friendly_api_error(raw: str) -> str:
    """把 Google 的 API 报错转成可操作的中文提示，原文附后备查。"""
    if not raw:
        return "未知错误"
    low = raw.lower()
    for key, hint in API_HINTS:
        if key in low:
            return hint + "（原始信息：" + raw[:160] + "）"
    return raw[:240]


def _err_message(resp: requests.Response) -> str:
    """从 Google 的错误响应里抽出可读信息，并翻译成中文提示。"""
    try:
        data = resp.json()
    except ValueError:
        return friendly_api_error((resp.text or "")[:200])
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message", "")
        reasons = [
            d.get("reason", "")
            for d in err.get("details", [])
            if isinstance(d, dict) and d.get("reason")
        ]
        return friendly_api_error((msg + " " + " ".join(reasons)).strip())
    return friendly_api_error(json.dumps(data, ensure_ascii=False)[:200])


# --------------------------------------------------------------------------
# Indexing API
# --------------------------------------------------------------------------


def publish_one(
    token: str, url: str, notif_type: str = "URL_UPDATED", **kw
) -> UrlResult:
    resp = _request(
        "POST", INDEXING_PUBLISH, token, json_body={"url": url, "type": notif_type}, **kw
    )
    if resp.status_code == 200:
        return UrlResult(url, True, 200, "已提交")
    return UrlResult(url, False, resp.status_code, _err_message(resp))


def _build_batch_body(urls: list[str], notif_type: str) -> str:
    crlf = "\r\n"
    parts = []
    for i, url in enumerate(urls, 1):
        payload = json.dumps({"url": url, "type": notif_type}, ensure_ascii=False)
        length = len(payload.encode("utf-8"))
        parts.append(
            "--" + BATCH_BOUNDARY + crlf
            + "Content-Type: application/http" + crlf
            + "Content-ID: <item-" + str(i) + ">" + crlf
            + crlf
            + "POST /v3/urlNotifications:publish HTTP/1.1" + crlf
            + "Content-Type: application/json" + crlf
            + "accept: application/json" + crlf
            + "content-length: " + str(length) + crlf
            + crlf
            + payload + crlf
        )
    parts.append("--" + BATCH_BOUNDARY + "--" + crlf)
    return "".join(parts)


def _parse_http_part(raw: str) -> tuple[int, dict]:
    """解析 batch 子响应里内嵌的那段原始 HTTP 报文。"""
    text = raw.replace("\r\n", "\n")
    m = re.search(r"HTTP/\d\.\d\s+(\d+)", text)
    status = int(m.group(1)) if m else 0
    start = m.end() if m else 0
    idx = text.find("\n\n", start)
    body = text[idx + 2:].strip() if idx != -1 else ""
    if not body:
        return status, {}
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, {"_raw": body[:300]}


def _parse_batch_response(
    text: str, content_type: str, urls: list[str]
) -> list[UrlResult]:
    """按 Content-ID 把子响应映射回原始 URL，取不到时按顺序兜底。"""
    results: dict[int, UrlResult] = {}
    msg = email.message_from_string("Content-Type: " + content_type + "\n\n" + text)
    parts = msg.get_payload() if msg.is_multipart() else []
    for order, part in enumerate(parts, 1):
        if not hasattr(part, "get_payload"):
            continue
        payload = part.get_payload()
        if not isinstance(payload, str):
            continue
        cid = part.get("Content-ID") or ""
        m = re.search(r"item-(\d+)", cid)
        idx = int(m.group(1)) if m else order
        if not 1 <= idx <= len(urls):
            continue
        status, body = _parse_http_part(payload)
        url = urls[idx - 1]
        if status == 200:
            results[idx] = UrlResult(url, True, 200, "已提交")
        else:
            err = body.get("error") if isinstance(body, dict) else None
            message = err.get("message", "") if isinstance(err, dict) else ""
            results[idx] = UrlResult(url, False, status, friendly_api_error(message))
    return [
        results.get(i, UrlResult(u, False, 0, "batch 响应中未找到该条目"))
        for i, u in enumerate(urls, 1)
    ]


def publish_batch(
    token: str,
    urls: list[str],
    notif_type: str = "URL_UPDATED",
    *,
    timeout: int = 45,
    max_retries: int = 4,
) -> list[UrlResult]:
    """一次 HTTP 请求提交最多 100 个 URL；配额仍按每个 URL 单独计。"""
    if not urls:
        return []
    if len(urls) == 1:
        return [
            publish_one(
                token, urls[0], notif_type, timeout=timeout, max_retries=max_retries
            )
        ]

    resp = _request(
        "POST",
        INDEXING_BATCH,
        token,
        data=_build_batch_body(urls, notif_type),
        content_type="multipart/mixed; boundary=" + BATCH_BOUNDARY,
        timeout=timeout,
        max_retries=max_retries,
    )
    if resp.status_code != 200:
        msg = _err_message(resp)
        return [UrlResult(u, False, resp.status_code, msg) for u in urls]

    ctype = resp.headers.get("Content-Type", "")
    if "multipart" not in ctype:
        return [UrlResult(u, False, 0, "batch 响应格式异常") for u in urls]
    try:
        return _parse_batch_response(resp.text, ctype, urls)
    except Exception as exc:  # 解析失败不该拖垮整批
        return [UrlResult(u, False, 0, "batch 响应解析失败: " + str(exc)) for u in urls]


# --------------------------------------------------------------------------
# URL Inspection API
# --------------------------------------------------------------------------

COVERAGE_CN = {
    "Submitted and indexed": "已收录",
    "Indexed, not submitted in sitemap": "已收录（不在站点地图）",
    "Crawled - currently not indexed": "已抓取，未收录",
    "Discovered - currently not indexed": "已发现，未抓取",
    "URL is unknown to Google": "Google 未知此 URL",
    "Page with redirect": "重定向页面",
    "Duplicate without user-selected canonical": "重复内容（无规范标记）",
    "Duplicate, Google chose different canonical than user": "重复内容（规范标记被改）",
    "Alternate page with proper canonical tag": "备用页（已指向规范页）",
    "Excluded by 'noindex' tag": "被 noindex 排除",
    "Blocked by robots.txt": "被 robots.txt 拦截",
    "Not found (404)": "404 未找到",
    "Soft 404": "软 404",
    "Server error (5xx)": "服务器错误",
}


@dataclass
class InspectResult:
    url: str
    ok: bool
    verdict: str = ""       # PASS / PARTIAL / FAIL / NEUTRAL
    coverage: str = ""      # Google 原文
    coverage_cn: str = ""   # 中文
    last_crawl: str = ""
    robots: str = ""
    canonical: str = ""
    status: int = 0
    message: str = ""

    @property
    def indexed(self) -> bool:
        return self.verdict == "PASS"


def inspect_url(
    token: str, site_url: str, url: str, *, timeout: int = 45, max_retries: int = 4
) -> InspectResult:
    resp = _request(
        "POST",
        INSPECT_URL,
        token,
        json_body={"inspectionUrl": url, "siteUrl": site_url},
        timeout=timeout,
        max_retries=max_retries,
    )
    if resp.status_code != 200:
        return InspectResult(
            url, False, status=resp.status_code, message=_err_message(resp)
        )
    try:
        res = resp.json()["inspectionResult"]["indexStatusResult"]
    except (ValueError, KeyError):
        return InspectResult(url, False, status=200, message="响应缺少 indexStatusResult")
    coverage = res.get("coverageState", "")
    return InspectResult(
        url=url,
        ok=True,
        verdict=res.get("verdict", ""),
        coverage=coverage,
        coverage_cn=COVERAGE_CN.get(coverage, coverage or "未知"),
        last_crawl=res.get("lastCrawlTime", ""),
        robots=res.get("robotsTxtState", ""),
        canonical=res.get("googleCanonical", ""),
        status=200,
    )


# --------------------------------------------------------------------------
# Search Console：站点列表与站点地图
# --------------------------------------------------------------------------


def list_sites(token: str, *, timeout: int = 45) -> tuple[bool, list[dict], str]:
    """列出该服务账号在 GSC 里能看到的属性，同时用作权限自检。"""
    resp = _request("GET", WEBMASTERS + "/sites", token, timeout=timeout, max_retries=2)
    if resp.status_code != 200:
        return False, [], _err_message(resp)
    return True, resp.json().get("siteEntry") or [], ""


def list_sitemaps(
    token: str, site_url: str, *, timeout: int = 45
) -> tuple[bool, list[dict], str]:
    path = WEBMASTERS + "/sites/" + quote(site_url, safe="") + "/sitemaps"
    resp = _request("GET", path, token, timeout=timeout, max_retries=2)
    if resp.status_code != 200:
        return False, [], _err_message(resp)
    return True, resp.json().get("sitemap") or [], ""


def submit_sitemap(
    token: str, site_url: str, feedpath: str, *, timeout: int = 45
) -> tuple[bool, str]:
    path = (
        WEBMASTERS
        + "/sites/"
        + quote(site_url, safe="")
        + "/sitemaps/"
        + quote(feedpath, safe="")
    )
    resp = _request("PUT", path, token, timeout=timeout, max_retries=2)
    if resp.status_code in (200, 204):
        return True, "站点地图已提交"
    return False, _err_message(resp)
