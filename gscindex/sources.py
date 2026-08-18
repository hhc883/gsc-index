"""URL 来源：文本清单、站点地图抓取，以及归一化与站点归属校验。"""

from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urlunparse

import requests

MAX_SITEMAP_DEPTH = 3
MAX_SITEMAP_FILES = 60
SITEMAP_TIMEOUT = 40


class SourceError(Exception):
    pass


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------


def normalize(raw: str) -> str | None:
    """去掉锚点、统一小写域名、补全协议。无法识别的返回 None。"""
    s = (raw or "").strip().strip('"').strip("'")
    if not s or s.startswith("#"):
        return None
    if s.startswith("//"):
        s = "https:" + s
    elif not re.match(r"^https?://", s, re.I):
        if "." not in s.split("/")[0]:
            return None
        s = "https://" + s
    try:
        p = urlparse(s)
    except ValueError:
        return None
    if not p.netloc:
        return None
    return urlunparse(
        (p.scheme.lower(), p.netloc.lower(), p.path or "/", p.params, p.query, "")
    )


def parse_text(text: str) -> tuple[list[str], int]:
    """从粘贴的文本或 .txt 文件解析 URL，返回 (URL 列表, 无法识别的条目数)。

    先按行处理再切分，这样整行注释和行尾注释都能整段丢掉，
    不会把注释里的文字误当成无效 URL 统计。
    """
    urls: list[str] = []
    bad = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for token in re.split(r"[,\s]+", line):
            if not token:
                continue
            if token.startswith("#"):
                break  # 行尾注释，本行剩下的一并忽略
            u = normalize(token)
            if u:
                urls.append(u)
            else:
                bad += 1
    return urls, bad


def dedupe(urls: list[str]) -> list[str]:
    """保持原顺序去重。"""
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# --------------------------------------------------------------------------
# 站点归属
# --------------------------------------------------------------------------


def site_matcher(site_url: str):
    """返回一个判断 URL 是否属于该 GSC 属性的函数。

    域名属性 sc-domain:example.com 匹配 example.com 及其全部子域；
    网址前缀属性 https://example.com/path/ 按前缀匹配。
    """
    site_url = (site_url or "").strip()
    if not site_url:
        return lambda _u: True

    if site_url.startswith("sc-domain:"):
        domain = site_url[len("sc-domain:") :].strip().lower().rstrip("/")

        def match_domain(u: str) -> bool:
            host = urlparse(u).netloc.lower().split(":")[0]
            return host == domain or host.endswith("." + domain)

        return match_domain

    prefix = site_url.lower()
    if not prefix.endswith("/"):
        prefix += "/"

    def match_prefix(u: str) -> bool:
        return u.lower().startswith(prefix)

    return match_prefix


def host_of(site_url: str) -> str:
    if site_url.startswith("sc-domain:"):
        return site_url[len("sc-domain:") :].strip()
    return urlparse(site_url).netloc


# --------------------------------------------------------------------------
# 站点地图
# --------------------------------------------------------------------------

NS_RE = re.compile(r"\{[^}]+\}")


def _strip_ns(tag: str) -> str:
    return NS_RE.sub("", tag).lower()


def _fetch(url: str) -> bytes:
    try:
        resp = requests.get(
            url,
            timeout=SITEMAP_TIMEOUT,
            headers={"User-Agent": "gsc-index/1.0 (sitemap reader)"},
            allow_redirects=True,
        )
    except requests.Timeout as exc:
        raise SourceError("读取超时：" + url) from exc
    except requests.ConnectionError as exc:
        raise SourceError("连不上服务器：" + url + "（请检查域名是否正确、网络是否通畅）") from exc
    except requests.RequestException as exc:
        raise SourceError("读取失败：" + url + " —— " + str(exc)[:120]) from exc
    if resp.status_code == 404:
        raise SourceError("站点地图不存在（404）：" + url)
    if resp.status_code != 200:
        raise SourceError("读取失败：" + url + " 返回 HTTP " + str(resp.status_code))
    data = resp.content
    if url.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError as exc:
            raise SourceError("gzip 解压失败 " + url + " : " + str(exc)) from exc
    return data


def fetch_sitemap(
    url: str, *, on_progress=None
) -> tuple[list[str], list[str], list[str]]:
    """递归抓取站点地图（支持 sitemap index 与 .gz）。

    返回 (URL 列表, 抓过的站点地图列表, 出错信息列表)。
    """
    start = normalize(url)
    if not start:
        raise SourceError("站点地图地址无法识别: " + str(url))

    urls: list[str] = []
    visited: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(start, 0)]

    while queue:
        current, depth = queue.pop(0)
        if current in seen or len(visited) >= MAX_SITEMAP_FILES:
            continue
        seen.add(current)
        if on_progress:
            on_progress(current, len(urls))
        try:
            data = _fetch(current)
        except SourceError as exc:
            errors.append(str(exc))
            continue
        visited.append(current)

        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            # 不是 XML，按纯文本站点地图处理
            text_urls, _ = parse_text(data.decode("utf-8", "ignore"))
            if text_urls:
                urls.extend(text_urls)
            else:
                errors.append("无法解析（既不是 XML 也不是文本清单）: " + current)
            continue

        kind = _strip_ns(root.tag)
        if kind == "sitemapindex":
            if depth >= MAX_SITEMAP_DEPTH:
                errors.append("嵌套层级超限，已跳过: " + current)
                continue
            for child in root:
                for loc in child:
                    if _strip_ns(loc.tag) == "loc" and (loc.text or "").strip():
                        nxt = normalize(loc.text)
                        if nxt:
                            queue.append((nxt, depth + 1))
        else:
            for child in root:
                for loc in child:
                    if _strip_ns(loc.tag) == "loc" and (loc.text or "").strip():
                        u = normalize(loc.text)
                        if u:
                            urls.append(u)

    return dedupe(urls), visited, errors


def guess_sitemap_feedpath(site_url: str, sitemap_url: str) -> str:
    """把站点地图地址整理成提交给 GSC 的完整 URL。"""
    u = normalize(sitemap_url)
    if u:
        return u
    host = host_of(site_url)
    path = sitemap_url if sitemap_url.startswith("/") else "/" + sitemap_url
    return "https://" + host + path
