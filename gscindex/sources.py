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


def _norm_host(h: str) -> str:
    """比对用的主机名：去端口、去 www.、小写。"""
    h = (h or "").strip().lower().split(":")[0].rstrip("/")
    return h[4:] if h.startswith("www.") else h


def _host_distance(a: str, b: str, cap: int = 2) -> int:
    """两个主机名差几个字符，超过 cap 就不算准。

    用来识别"手打错了一个字母"——比如粘进来 exmaple.com 而属性里是
    example.com。只报"不属于任何站点"的话，用户会以为这个站没加进
    Search Console，实际只是打错了。
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _retarget(url: str, site_url: str) -> str | None:
    """把 URL 的协议和主机名换成某个 GSC 属性的，路径查询原样保留。

    专治"http:// 对 https:// 属性"和"www. 对非 www 属性"这两类：
    GSC 的网址前缀属性是**按字符串前缀**匹配的，https://example.com/ 这个属性
    压根不覆盖 http://example.com/page，www 和非 www 也是两回事。
    但用户粘一条 http 链接时，意图毫无疑问是他自己那个 https 站点——
    这种情况自动改写成属性的写法，比丢给他一句"不属于任何站点"有用得多。
    """
    if site_url.startswith("sc-domain:"):
        return None                      # 域名属性本来就不挑协议和子域，走不到这里
    try:
        pu, ps = urlparse(url), urlparse(site_url)
    except ValueError:
        return None
    if not ps.scheme or not ps.netloc:
        return None
    return urlunparse((ps.scheme, ps.netloc, pu.path or "/", pu.params, pu.query, ""))


def match_sites(
    urls: list[str], site_urls: list[str]
) -> tuple[dict[str, list[str]], list[dict], list[dict]]:
    """把一堆 URL 按所属 GSC 属性分组，返回 ({属性: [URL...]}, 判不出归属的)。

    这是"粘贴任意链接、不用先选站点"的基础：用户手里的链接可能横跨几十个站点，
    让他先选站点再粘贴是本末倒置。

    两种属性写法都要支持：
      sc-domain:example.com   → example.com 及其全部子域
      https://example.com/x/  → 按路径前缀

    一个 URL 可能同时落在两个属性里（https://x.com/ 和 https://x.com/blog/），
    取**前缀最长**的那个——更具体的属性才是这条 URL 真正归属的地方。

    判不出归属的**逐条给原因**，绝不静默丢掉。少提交一条是小事，
    但用户以为交上去了、实际被悄悄扔了，就会一直等一个不会来的结果。

    返回第三项是"自动改写过的"清单：主机名对得上、只是协议或 www 写法跟属性
    不一致的，会改写成属性的写法并记在这里，供界面明确告知——改写了用户粘进来的
    东西就必须说出来，不能悄悄换掉。
    """
    # (属性, 匹配函数, 前缀长度) —— 前缀长度用来在多个匹配里挑最具体的
    specs = []
    hosts: dict[str, str] = {}     # 归一化主机名 -> 属性（近似检测用）
    for site in site_urls:
        site = (site or "").strip()
        if not site:
            continue
        specs.append((site, site_matcher(site), len(site)))
        hosts.setdefault(_norm_host(host_of(site)), site)

    groups: dict[str, list[str]] = {}
    unknown: list[dict] = []
    fixed: list[dict] = []

    for raw in urls:
        u = normalize(raw)
        if not u:
            unknown.append({"input": str(raw)[:200], "reason": "bad_url"})
            continue

        hit = [(site, plen) for site, fn, plen in specs if fn(u)]
        if hit:
            hit.sort(key=lambda x: x[1], reverse=True)
            groups.setdefault(hit[0][0], []).append(u)
            continue

        h = _norm_host(urlparse(u).netloc)

        # 主机名对得上某个属性，却没匹配上 —— 这不是打错字，别往那边归因。
        # 三种可能：协议不同（http vs https）、www 写法不同、路径不在前缀范围内。
        # 前两种能自动改写，第三种改不了，得说清楚是路径的问题。
        same_host = [(site, plen) for site, fn, plen in specs
                     if _norm_host(host_of(site)) == h]
        if same_host:
            same_host.sort(key=lambda x: x[1], reverse=True)
            done = False
            for site, _plen in same_host:
                alt = _retarget(u, site)
                if not alt or alt == u:
                    continue
                fn = next(f for st, f, _pl in specs if st == site)
                if fn(alt):
                    groups.setdefault(site, []).append(alt)
                    fixed.append({"input": u, "fixed": alt, "site": site,
                                  "kind": ("scheme"
                                           if urlparse(u).scheme != urlparse(site).scheme
                                           else "www")})
                    done = True
                    break
            if done:
                continue
            unknown.append({"input": u, "host": h, "reason": "path_outside",
                            "near_site": same_host[0][0]})
            continue

        # 主机名也对不上。是打错了字，还是这个站真的没加进 Search Console？
        # 距离必须 >= 1：距离 0 说明主机名相同，上面那一支已经处理过，
        # 说"只差 0 个字符"是句废话，只会让人一头雾水。
        near, near_d = "", 99
        if len(h) >= 8:
            for cand in hosts:
                d = _host_distance(h, cand)
                if 1 <= d < near_d:
                    near, near_d = cand, d
        if near and near_d <= 2:
            unknown.append({"input": u, "host": h, "reason": "typo",
                            "near_host": near, "near_site": hosts[near],
                            "near_distance": near_d})
        else:
            unknown.append({"input": u, "host": h, "reason": "not_a_property"})

    return groups, unknown, fixed


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
