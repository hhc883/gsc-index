"""Google Search Console API 客户端（官方开放接口部分）。

包含：URL Inspection（查收录状态）、站点属性列表、站点地图提交与查询。
只依赖 requests + google-auth。

注意：本项目已经**移除** Indexing API（urlNotifications:publish）。
原因是 Google 官方只声明它支持 JobPosting / BroadcastEvent 两类结构化数据，
普通网页调用虽然返回 200 但实测对收录没有可靠作用。
"请求编入索引"改由 webauto.py 自动化操作 GSC 网页完成。
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
WEBMASTERS = "https://www.googleapis.com/webmasters/v3"

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
     "当前凭据不是这个站点的所有者。服务账号需要到 Search Console 的"
     "「设置 → 用户和权限」把邮箱添加为「所有者」；OAuth 凭据请确认"
     "授权的是拥有该站点的那个 Google 账号。"),
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
     "配额已耗尽。收录查询（URL Inspection）每个凭据每天 2000 次，"
     "可新建 GCP 项目再加一个凭据来扩容。"),
    ("quota exceeded",
     "配额已耗尽。可以明天再试，或新建 GCP 项目增加凭据。"),
    ("rate_limit_exceeded",
     "请求过于频繁，已触发限流。请降低设置里的并发数。"),
    ("invalid attribute",
     "URL 格式不被接受。请确认是完整的 http(s) 地址，且与站点属性匹配。"),
    ("permission denied",
     "权限被拒绝。请确认当前凭据对该站点有权限，且 Search Console API 已启用。"),
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
