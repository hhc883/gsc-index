"""流量数据客户端：GSC Search Analytics + GA4（Data API / Admin API）。

两边都是 Google 官方开放接口，有文档、有配额、完全合规——跟 webauto.py
那条灰色地带的路性质完全不同。

几个必须记住的事实（都是这两套 API 的固有特性，不是本模块的限制）：

1. **筛选只能在本地做。** 两边的接口都是"按单个站点/属性查询"，而且筛选条件
   只支持维度（页面、查询词、国家…），**不支持按指标筛**——没法跟 Google 说
   "把点击量大于 1000 的站点给我"。所以必须逐站点拉总量、缓存到本地，
   再在本地任意筛选排序。

2. **GSC 数据有 2~3 天延迟。** 这是 Google 自己的口径，"今天的流量"永远查不到。
   所以默认查询窗口都往前推几天，免得把还没就绪的日期算进来当成"零流量"。

3. **衡量 ID ≠ 媒体资源 ID。** 跟踪代码里那个 G-XXXXXXXXXX 是发送数据用的，
   调 Data API 读数据要的是纯数字的媒体资源 ID。两者不能混用——这是最常见的坑。
   本模块用 Admin API 自动把它们对应起来，不需要用户手抄。

4. **GA4 只有装了跟踪代码之后的数据**，没有历史回溯；免费版默认只保留 2 个月
   （可在 GA 后台改成 14 个月，且只对之后的数据生效）。想留更久要靠本地缓存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .api import _request, _err_message

SEARCH_ANALYTICS = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
GA_DATA = "https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport"
GA_ADMIN_ACCOUNTS = "https://analyticsadmin.googleapis.com/v1beta/accountSummaries"
GA_ADMIN_STREAMS = "https://analyticsadmin.googleapis.com/v1beta/properties/{prop}/dataStreams"

# GSC 数据的滞后天数。查到今天必然是空的，会被误当成"这天没流量"，
# 所以窗口末端一律往前推这么多天。
GSC_LAG_DAYS = 3


def window(days: int, *, lag: int = 0) -> tuple[str, str]:
    """返回 (开始日期, 结束日期) 字符串，结束日期往前推 lag 天。"""
    end = date.today() - timedelta(days=lag)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


# --------------------------------------------------------------------------
# GSC Search Analytics
# --------------------------------------------------------------------------


@dataclass
class GscTotals:
    site: str
    ok: bool
    clicks: int = 0
    impressions: int = 0
    ctr: float = 0.0
    position: float = 0.0
    message: str = ""


def gsc_totals(
    token: str, site_url: str, days: int = 28, *, timeout: int = 45
) -> GscTotals:
    """拿一个站点在窗口内的流量总量（不拆维度，响应很小、很快）。

    这是"全站点排行"的取数方式：79 个站点各来一次这样的轻量请求。
    """
    from urllib.parse import quote

    start, end = window(days, lag=GSC_LAG_DAYS)
    resp = _request(
        "POST",
        SEARCH_ANALYTICS.format(site=quote(site_url, safe="")),
        token,
        json_body={"startDate": start, "endDate": end},  # 不给 dimensions 就是总量
        timeout=timeout,
        max_retries=3,
    )
    if resp.status_code != 200:
        return GscTotals(site_url, False, message=_err_message(resp))
    rows = (resp.json() or {}).get("rows") or []
    if not rows:
        # 没有 rows 不是错误，是这个站点在这段时间里真的一次展现都没有
        return GscTotals(site_url, True, message="窗口内没有任何展现")
    r = rows[0]
    return GscTotals(
        site=site_url,
        ok=True,
        clicks=int(r.get("clicks") or 0),
        impressions=int(r.get("impressions") or 0),
        ctr=float(r.get("ctr") or 0.0),
        position=float(r.get("position") or 0.0),
    )


def gsc_breakdown(
    token: str,
    site_url: str,
    dimension: str = "page",
    days: int = 28,
    *,
    limit: int = 1000,
    timeout: int = 45,
) -> tuple[bool, list[dict], str]:
    """按维度拆分的明细。dimension 可传 page / query / country / device。

    page 用来找"已收录但零展现"的页面；query 用来看别人搜什么词找到你，
    这是 GSC 独有、GA 看不到的数据。
    """
    from urllib.parse import quote

    start, end = window(days, lag=GSC_LAG_DAYS)
    resp = _request(
        "POST",
        SEARCH_ANALYTICS.format(site=quote(site_url, safe="")),
        token,
        json_body={
            "startDate": start,
            "endDate": end,
            "dimensions": [dimension],
            "rowLimit": min(limit, 25000),
        },
        timeout=timeout,
        max_retries=3,
    )
    if resp.status_code != 200:
        return False, [], _err_message(resp)
    out = []
    for r in (resp.json() or {}).get("rows") or []:
        keys = r.get("keys") or [""]
        out.append(
            {
                "key": keys[0],
                "clicks": int(r.get("clicks") or 0),
                "impressions": int(r.get("impressions") or 0),
                "ctr": float(r.get("ctr") or 0.0),
                "position": float(r.get("position") or 0.0),
            }
        )
    return True, out, ""


def gsc_daily(
    token: str, site_url: str, days: int = 28, *, timeout: int = 45
) -> tuple[bool, list[dict], str]:
    """按天的趋势数据，用来画走势图。"""
    ok, rows, err = gsc_breakdown(
        token, site_url, dimension="date", days=days, limit=days + 10, timeout=timeout
    )
    if not ok:
        return False, [], err
    rows.sort(key=lambda r: r["key"])
    return True, [{**r, "date": r["key"]} for r in rows], ""


# --------------------------------------------------------------------------
# GA4 Admin API：自动发现属性并匹配站点
# --------------------------------------------------------------------------


@dataclass
class GaProperty:
    property_id: str          # 纯数字，调 Data API 用的就是这个
    display_name: str
    account_name: str = ""
    stream_uris: list[str] = field(default_factory=list)   # 数据流绑定的网站地址
    measurement_ids: list[str] = field(default_factory=list)  # G-XXXXXXXXXX


def ga_list_properties(token: str, *, timeout: int = 45) -> tuple[bool, list[GaProperty], str]:
    """列出这个账号能访问的全部 GA4 媒体资源。

    只拿属性列表和名称，还不含数据流——数据流要逐个属性查（见 ga_fill_streams），
    属性多的时候那一步比较慢，所以分开两步，界面上可以先把列表显示出来。
    """
    props: list[GaProperty] = []
    page_token = ""
    for _ in range(20):  # 翻页上限，防止异常情况下无限循环
        url = GA_ADMIN_ACCOUNTS + "?pageSize=200"
        if page_token:
            url += "&pageToken=" + page_token
        resp = _request("GET", url, token, timeout=timeout, max_retries=2)
        if resp.status_code != 200:
            return False, props, _err_message(resp)
        data = resp.json() or {}
        for acc in data.get("accountSummaries") or []:
            acc_name = acc.get("displayName", "")
            for ps in acc.get("propertySummaries") or []:
                # property 形如 "properties/123456789"，取后面的纯数字
                pid = (ps.get("property") or "").split("/")[-1]
                if pid:
                    props.append(
                        GaProperty(
                            property_id=pid,
                            display_name=ps.get("displayName", ""),
                            account_name=acc_name,
                        )
                    )
        page_token = data.get("nextPageToken") or ""
        if not page_token:
            break
    return True, props, ""


def ga_property_streams(
    token: str, property_id: str, *, timeout: int = 45
) -> tuple[bool, list[str], list[str], str]:
    """查一个属性的数据流，返回 (ok, 网站地址列表, 衡量ID列表, 错误)。

    数据流里存着这个属性实际绑定的网站地址，这是把 GA 属性和 GSC 站点
    自动对应起来最可靠的依据——比拿属性名称去猜准得多。
    """
    resp = _request(
        "GET",
        GA_ADMIN_STREAMS.format(prop=property_id) + "?pageSize=50",
        token,
        timeout=timeout,
        max_retries=2,
    )
    if resp.status_code != 200:
        return False, [], [], _err_message(resp)
    uris, mids = [], []
    for st in (resp.json() or {}).get("dataStreams") or []:
        web = st.get("webStreamData") or {}
        if web.get("defaultUri"):
            uris.append(web["defaultUri"])
        if web.get("measurementId"):
            mids.append(web["measurementId"])
    return True, uris, mids, ""


def _host(url: str) -> str:
    """从各种写法里取出主机名，用于比对。

    需要同时应对 GSC 的两种属性写法（sc-domain:example.com 和
    https://example.com/）以及 GA 数据流的 defaultUri。
    """
    from urllib.parse import urlparse

    u = (url or "").strip().lower()
    if u.startswith("sc-domain:"):
        u = u[len("sc-domain:") :]
    elif "//" in u:
        u = urlparse(u).netloc or u
    else:
        u = urlparse("//" + u).netloc or u
    u = u.split(":")[0].rstrip("/")
    return u[4:] if u.startswith("www.") else u


def ga_match_sites(
    props: list[GaProperty], site_urls: list[str]
) -> tuple[dict[str, str], list[GaProperty], list[str]]:
    """把 GA 属性和 GSC 站点对应起来。

    优先用数据流里的真实网站地址匹配（可靠）；地址对不上时退而用属性名称里
    是否包含域名来猜（不可靠，但比让用户手抄 79 个 ID 好）。

    返回 (站点->属性ID 的映射, 没配上的属性, 没配上的站点)。
    """
    by_host: dict[str, str] = {}
    for p in props:
        for uri in p.stream_uris:
            h = _host(uri)
            if h:
                by_host.setdefault(h, p.property_id)

    mapping: dict[str, str] = {}
    matched_props: set[str] = set()
    unmatched_sites: list[str] = []

    for site in site_urls:
        h = _host(site)
        pid = by_host.get(h)
        if not pid:
            # 退路：属性名称里带域名的也认，但这条不如数据流可靠
            for p in props:
                name = (p.display_name or "").lower()
                if h and h in name:
                    pid = p.property_id
                    break
        if pid:
            mapping[site] = pid
            matched_props.add(pid)
        else:
            unmatched_sites.append(site)

    unmatched_props = [p for p in props if p.property_id not in matched_props]
    return mapping, unmatched_props, unmatched_sites


# --------------------------------------------------------------------------
# GA4 Data API
# --------------------------------------------------------------------------


@dataclass
class GaTotals:
    property_id: str
    ok: bool
    sessions: int = 0
    users: int = 0
    views: int = 0
    bounce_rate: float = 0.0
    events: int = 0
    message: str = ""


# 顺序即 metricValues 的下标顺序，改动这里必须同步改下面取值的 num(i)。
# eventCount 是"事件数"：GA4 里连页面浏览、滚动、外链点击都算事件，
# 所以它天然比会话数、浏览量大一个量级，别拿它跟 GSC 的曝光量直接比。
GA_METRICS = ["sessions", "totalUsers", "screenPageViews", "bounceRate", "eventCount"]


def ga_totals(
    token: str, property_id: str, days: int = 28, *, timeout: int = 45
) -> GaTotals:
    """拿一个 GA4 属性在窗口内的总量。GA 数据接近实时，不用像 GSC 那样推迟窗口。"""
    start, end = window(days)
    resp = _request(
        "POST",
        GA_DATA.format(prop=property_id),
        token,
        json_body={
            "dateRanges": [{"startDate": start, "endDate": end}],
            "metrics": [{"name": m} for m in GA_METRICS],
        },
        timeout=timeout,
        max_retries=3,
    )
    if resp.status_code != 200:
        return GaTotals(property_id, False, message=_err_message(resp))
    rows = (resp.json() or {}).get("rows") or []
    if not rows:
        return GaTotals(property_id, True, message="窗口内没有任何会话")
    vals = rows[0].get("metricValues") or []

    def num(i: int) -> float:
        try:
            return float(vals[i].get("value") or 0)
        except (IndexError, AttributeError, ValueError):
            return 0.0

    return GaTotals(
        property_id=property_id,
        ok=True,
        sessions=int(num(0)),
        users=int(num(1)),
        views=int(num(2)),
        bounce_rate=num(3),
        events=int(num(4)),
    )


def ga_breakdown(
    token: str,
    property_id: str,
    dimension: str = "pagePath",
    days: int = 28,
    *,
    limit: int = 500,
    timeout: int = 45,
) -> tuple[bool, list[dict], str]:
    """按维度拆分。dimension 可传 pagePath / date / sessionSource / country / deviceCategory。"""
    start, end = window(days)
    resp = _request(
        "POST",
        GA_DATA.format(prop=property_id),
        token,
        json_body={
            "dateRanges": [{"startDate": start, "endDate": end}],
            "dimensions": [{"name": dimension}],
            "metrics": [{"name": m} for m in GA_METRICS],
            "limit": min(limit, 10000),
            "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
        },
        timeout=timeout,
        max_retries=3,
    )
    if resp.status_code != 200:
        return False, [], _err_message(resp)
    out = []
    for r in (resp.json() or {}).get("rows") or []:
        dims = r.get("dimensionValues") or [{}]
        vals = r.get("metricValues") or []

        def num(i: int) -> float:
            try:
                return float(vals[i].get("value") or 0)
            except (IndexError, AttributeError, ValueError):
                return 0.0

        out.append(
            {
                "key": dims[0].get("value", ""),
                "sessions": int(num(0)),
                "users": int(num(1)),
                "views": int(num(2)),
                "bounce_rate": num(3),
                "events": int(num(4)),
            }
        )
    return True, out, ""
