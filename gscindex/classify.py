"""站点分类：抓首页元信息，按关键词规则归入用户既定的分类。

为什么靠首页 title/description 而不是域名：域名经常看不出内容。
实测 nophoolery.com 光看域名完全猜不到是音频评测站，而它的首页标题
"Honest Audio Gear Reviews & Buying Guides" 一句话就说清了。

为什么必须人工确认：这里没有 AI，只能做关键词规则匹配，一定会有判错和
判不出的。拿一份看起来完整、实际错了几个的分类去做决策，比没有分类更糟。
所以本模块只产出**建议**，并且一并给出"命中了哪个词"，让人能判断该不该信。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_META = re.compile(
    r"""<meta[^>]+?(?:name|property)\s*=\s*["'](?:description|og:description)["']"""
    r"""[^>]+?content\s*=\s*["'](.*?)["']""",
    re.S | re.I,
)
_META_REV = re.compile(
    r"""<meta[^>]+?content\s*=\s*["'](.*?)["']"""
    r"""[^>]+?(?:name|property)\s*=\s*["'](?:description|og:description)["']""",
    re.S | re.I,
)
_TAG = re.compile(r"<[^>]+>")


def _unescape(s: str) -> str:
    import html
    return html.unescape(_TAG.sub("", s or "")).strip()


@dataclass
class SiteMeta:
    site: str
    ok: bool = False
    title: str = ""
    description: str = ""
    message: str = ""


def fetch_meta(site_url: str, *, timeout: int = 12) -> SiteMeta:
    """抓一个站点的首页标题和描述。访问的是用户自己的网站，不碰任何 Google 配额。"""
    url = site_url
    if url.startswith("sc-domain:"):
        url = "https://" + url[len("sc-domain:"):].strip().rstrip("/") + "/"
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA},
                         allow_redirects=True)
    except requests.exceptions.SSLError as exc:
        return SiteMeta(site_url, False, message="HTTPS 证书有问题：" + str(exc)[:80])
    except requests.exceptions.Timeout:
        return SiteMeta(site_url, False, message=f"{timeout} 秒内没有响应")
    except requests.exceptions.RequestException as exc:
        return SiteMeta(site_url, False, message=type(exc).__name__ + "：" + str(exc)[:80])
    if r.status_code != 200:
        return SiteMeta(site_url, False, message=f"首页返回 HTTP {r.status_code}")

    text = r.text or ""
    t = _TITLE.search(text)
    d = _META.search(text) or _META_REV.search(text)
    title, desc = _unescape(t.group(1)) if t else "", _unescape(d.group(1)) if d else ""
    if not title and not desc:
        return SiteMeta(site_url, False, message="首页里没有 title 也没有 description")
    return SiteMeta(site_url, True, title=title, description=desc)


def fetch_all(sites: list[str], *, concurrency: int = 8, timeout: int = 12,
              on_done=None) -> dict[str, SiteMeta]:
    """并发抓一批站点。并发度刻意压得低——这是在访问用户自己的服务器，
    没必要为了快几秒去打自己的站。"""
    out: dict[str, SiteMeta] = {}
    if not sites:
        return out
    with ThreadPoolExecutor(max_workers=min(concurrency, len(sites))) as ex:
        futs = {ex.submit(fetch_meta, s, timeout=timeout): s for s in sites}
        for f in as_completed(futs):
            m = f.result()
            out[m.site] = m
            if on_done:
                on_done(m, len(out), len(sites))
    return out


# --------------------------------------------------------------------------
# 分类体系
# --------------------------------------------------------------------------

# 前 8 个是用户目录里实际在用的，后 7 个来自他的分类标签栏但目前还没有站点。
# 保留空分类而不是删掉：一眼能看出哪些方向还没布局。
CATEGORIES = [
    "汽车", "家装", "服装", "游戏", "电子产品", "宠物", "美妆", "美食",
    "礼物", "母婴", "运动", "数字生活", "健康生活", "晚年生活", "节能与能源",
]

# 同一个分类在不同来源里的叫法。目录里叫「电子」，主表工作表里叫「电子产品」，
# 不归一的话同一批站会被拆成两个分类，后面的汇总就都是错的。
CATEGORY_ALIAS = {"电子": "电子产品", "数码": "电子产品"}


def canon_category(name: str) -> str:
    n = (name or "").strip()
    return CATEGORY_ALIAS.get(n, n)

# 关键词规则。**只用于给还没有权威分类的站点提建议**——用户自己维护的
# 主表（一个分类一个工作表、带域名列）才是权威来源，导入过的不走这里。
#
# 词表是拿 85 个真实站点的首页标题标定的，不是凭空写的。拿主表当答案跑过
# 一遍：85/85 全对。但要注意这里面有过拟合——其中一条规则就是为了修
# counterandspoon 的误判才调的，所以这个数字对**这批站**成立，对将来的新站
# 只能说"多半不错"。判不出的一律显示成未分类交给人，不硬猜。
# 顺序有意义：从上往下第一个命中的类胜出，所以把"更具体"的类排在前面。
RULES: list[tuple[str, list[str]]] = [
    # 童装/童鞋都归服装（用户的目录就是这么分的），所以 kids 相关的词
    # 要跟服装词一起判断，不能单独拉出来当母婴
    ("服装", [
        r"\bmenswear\b", r"\bwomen'?s? (fashion|style|wardrobe)\b", r"\bmen'?s? (style|clothing|fashion)\b",
        r"\bwardrobe\b", r"\boutfits?\b", r"\bcapsule\b", r"\bthrift\b", r"\bvintage (menswear|fashion)\b",
        r"\bfashion blog\b", r"\bplus-?size fashion\b", r"\bstyle advice\b", r"\bgarment\b",
        r"\bkids?'? (clothes|clothing|outfits?|style|wardrobe)\b", r"\bchildren'?s style\b",
        r"\bworkwear\b", r"\bslow fashion\b", r"\bquiet luxury\b", r"\bhow clothes.*fit\b",
        r"\bdress better\b", r"\bfit advice\b", r"\bboots?\b.*\bgear reviews?\b",
    ]),
    ("汽车", [
        r"\bcars?\b", r"\bautomotive\b", r"\bvehicles?\b", r"\bev\b", r"\belectric car\b",
        r"\bdriv(ers?|ing)\b", r"\bmechanic\b", r"\bgarage\b.*\b(car|mod|perform)", r"\bmileage\b",
        r"\bcar (culture|ownership|buying|reviews?)\b", r"\bused cars?\b", r"\bsuvs?\b",
    ]),
    ("游戏", [
        r"\bgames?\b", r"\bgaming\b", r"\bgamers?\b", r"\bindie game\b", r"\breplay\b",
        r"\bnew releases?\b.*\bplay\b",
    ]),
    ("电子", [
        r"\btech reviews?\b", r"\baudio\b", r"\bheadphones?\b", r"\bspeakers?\b",
        r"\btablets?\b", r"\bsmartwatch\b", r"\bwearables?\b", r"\bearbuds?\b",
        r"\bpc builds?\b", r"\bdesktops?\b", r"\bphones?\b.*\breviews?\b", r"\bgadget\b",
    ]),
    ("宠物", [r"\bpets?\b", r"\bdogs?\b.*\bcats?\b", r"\bveterinar", r"\bvet advice\b", r"\bdvm\b"]),
    ("美妆", [
        r"\bskincare\b", r"\bgrooming\b", r"\bmakeup\b", r"\bcosmetics?\b",
        r"\besthetician\b", r"\bingredients?\b.*\bskin\b",
    ]),
    ("美食", [
        # 刻意不收单独的 "cooking"：厨房装修站的描述里几乎必然出现它。
        # 实测 counterandspoon 的标题是 "Honest Kitchen Design, Tools & Remodel
        # Advice"，描述里带 "family cooking tips"，就被这个词误判成了美食。
        # 宁可判不出、显示成"未分类"由人来定，也不要悄悄归错——
        # 归错的分类会一路污染后面所有按分类做的筛选和汇总。
        r"\bbbq\b", r"\bbrisket\b", r"\brecipes?\b", r"\bpitmaster\b",
        r"\bhome cooks?\b", r"\bbaking\b", r"\bgrilling\b", r"\bmeal (prep|plan)",
    ]),
    # 家装放最后：它的词最杂（厨房/浴室/地板/照明/收纳/园艺都算），
    # 放前面会把"厨房设计"里的 cooking、"衣帽间设计"里的 closet 抢走
    ("家装", [
        r"\brenovation\b", r"\bremodel", r"\bhome (design|energy|maintenance|improvement)\b",
        r"\binterior design\b", r"\bbathrooms?\b", r"\bkitchens?\b", r"\bbasements?\b",
        r"\bflooring\b", r"\bcabinetry\b", r"\blighting design\b", r"\bfurniture\b",
        r"\bcontractor\b", r"\bsmart home\b", r"\bclosets?\b", r"\bbedroom\b", r"\bliving room\b",
        r"\bhome office\b", r"\bcurtains?\b", r"\bwindow treatments?\b", r"\blaundry room\b",
        r"\bgardening\b", r"\bzones? \d", r"\bair purifiers?\b", r"\bwater filters?\b",
        r"\bappliances?\b", r"\bfloor plans?\b", r"\bwaterproofing\b", r"\btile\b",
        r"\bnew build\b", r"\bhomeowners?\b", r"\bworkshop\b",
    ]),
]


def suggest(title: str, description: str = "", host: str = "") -> tuple[str, str]:
    """给一个站点建议分类，返回 (分类, 命中了哪些词)。

    命中的词一定要带出来。只给一个分类而不给依据的话，用户没法判断该不该信，
    只能全部重新核对一遍，那这个建议就白给了。
    """
    hay = " ".join(x for x in (title, description, host) if x).lower()
    if not hay.strip():
        return "", ""
    for cat, pats in RULES:
        hits = [p for p in pats if re.search(p, hay)]
        if hits:
            # \b 必须先整体去掉，再去掉其余元字符。反过来的话字符类会先吃掉
            # 那个反斜杠，只剩一个孤零零的 b，显示出来就成了 "bnew buildb"。
            # 括号和竖线保留：driv(ers?|ing) 剥掉括号会粘成 "driversing"，
            # 留着反而一眼看懂命中的是哪一族词
            words = [re.sub(r"[\\^$.*+?\[\]{}]", "",
                            re.sub(r"\\b", " ", h)).strip() for h in hits[:3]]
            words = [re.sub(r"\s+", " ", w) for w in words]
            # 归一化：规则表里写的是「电子」，对外统一成主表的「电子产品」
            return canon_category(cat), "、".join(w for w in words if w)
    return "", ""


# --------------------------------------------------------------------------
# 从目录导入分类
# --------------------------------------------------------------------------

_NUM_PREFIX = re.compile(r"^\s*\d+\s*[:：]\s*")
_KEY_SUFFIX = re.compile(r"[-\s]*key(word)?s?\d*\s*$", re.I)


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def clean_folder_name(raw: str) -> str:
    """把 "130：Connor's Key-Ready Home-key" 清成 "Connor's Key-Ready Home"。"""
    return _KEY_SUFFIX.sub("", _NUM_PREFIX.sub("", raw)).strip()


def import_from_dir(root, sites: list[str], titles: dict[str, str]) -> dict:
    """把「分类目录/站点文件夹」的结构读成 站点->分类 的映射。

    目录里的文件夹名是站点的**人类叫法**（有的是站名、有的是域名、还带编号和
    -key 后缀），跟域名不一定字面相同——实测有站改过名（文件夹叫
    "Connor's Key-Ready Home"，站点标题已经是 "Connor's New House"）。
    所以要三级匹配：域名精确 → 首页标题精确 → 近似。

    近似匹配的结果**必须单独标出来让人核对**，不能跟精确匹配混在一起报成功：
    近似匹配本质上是猜，猜错了就是把一个站归进了错误的分类，而错误的分类
    会一路影响后面所有按分类做的筛选和汇总。
    """
    import difflib
    from pathlib import Path

    root = Path(root)
    if not root.is_dir():
        return {"error": f"目录不存在：{root}", "mapping": {}}

    by_stem, by_title = {}, {}
    for s in sites:
        stem = s.replace("https://", "").replace("http://", "").replace(
            "sc-domain:", "").rstrip("/").rsplit(".", 1)[0]
        by_stem[_norm_name(stem)] = s
        head = re.split(r"[|—\-–]", titles.get(s, ""))[0]
        if _norm_name(head):
            by_title.setdefault(_norm_name(head), s)

    mapping: dict[str, str] = {}
    exact, fuzzy, unmatched = [], [], []
    seen_folders = 0

    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        category = d.name.strip()
        for f in sorted(d.iterdir()):
            if not f.is_dir() and not f.is_file():
                continue
            if f.name.lower() in ("backup", "备份", ".ds_store"):
                continue
            seen_folders += 1
            name = clean_folder_name(f.name)
            n = _norm_name(name)
            hit = how = None
            if n in by_stem:
                hit, how = by_stem[n], "域名精确"
            elif n in by_title:
                hit, how = by_title[n], "标题精确"
            else:
                c = difflib.get_close_matches(n, list(by_stem), 1, 0.82)
                if c:
                    hit, how = by_stem[c[0]], f"域名近似（{c[0]}）"
                else:
                    c = difflib.get_close_matches(n, list(by_title), 1, 0.80)
                    if c:
                        hit, how = by_title[c[0]], f"标题近似（{c[0]}）"
            rec = {"category": category, "folder": f.name, "name": name,
                   "site": hit or "", "how": how or ""}
            if not hit:
                unmatched.append(rec)
            else:
                mapping[hit] = category
                (fuzzy if "近似" in how else exact).append(rec)

    # 同一个站点被两个分类的文件夹认领 —— 必须报出来，不能让后写入的悄悄赢
    counts: dict[str, list[str]] = {}
    for rec in exact + fuzzy:
        counts.setdefault(rec["site"], []).append(rec["category"])
    conflicts = [{"site": s, "categories": cs} for s, cs in counts.items() if len(set(cs)) > 1]

    return {
        "mapping": mapping,
        "categories": sorted({d.name.strip() for d in root.iterdir() if d.is_dir()}),
        "exact": exact, "fuzzy": fuzzy, "unmatched": unmatched,
        "conflicts": conflicts,
        "folders_seen": seen_folders,
        # GSC 里有、但目录里没有任何文件夹认领的站点
        "sites_without_folder": [s for s in sites if s not in mapping],
    }


def import_from_xlsx(path, sites: list[str]) -> dict:
    """从「一个分类一个工作表」的主表导入分类。

    这是最可靠的来源：分类就是工作表名，域名是独立的一列，**逐字匹配即可**，
    不需要像目录那样拿站名去猜——目录那条路上已经出过一次站改名导致对不上的事。

    表里通常还有 GSC 里还没有的站点（域名买了、站还没建完）。这些也一并存下来：
    等它们进了 Search Console，分类已经是现成的，不用再导一次。
    """
    try:
        import openpyxl
    except ImportError:
        return {"error": "缺少 openpyxl，无法读取 xlsx。请运行 "
                         "pip install openpyxl 后重试。", "mapping": {}}
    from pathlib import Path

    path = Path(path)
    if not path.is_file():
        return {"error": f"文件不存在：{path}", "mapping": {}}
    try:
        # 刻意不用 read_only=True：那个模式依赖工作表自己声明的 dimension，
        # 实测这份表声明不准，表头行只能读出第一格，于是每个工作表都被判成
        # "没有域名列"而整个跳过。表就几十 KB，全量加载没有任何负担。
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:
        return {"error": f"打不开这个 xlsx：{type(exc).__name__} {exc}", "mapping": {}}

    known = {}
    for s in sites:
        d = re.sub(r"^https?://", "", s.replace("sc-domain:", "")).rstrip("/").lower()
        known[d] = s
        known[d[4:] if d.startswith("www.") else "www." + d] = s

    mapping: dict[str, str] = {}      # GSC 站点 -> 分类
    future: dict[str, str] = {}       # 表里有、GSC 还没有的域名 -> 分类
    per_sheet, skipped, dups = {}, [], []
    seen: dict[str, str] = {}

    for name in wb.sheetnames:
        if name.strip() in ("模板", "template", "Sheet1"):
            continue
        ws = wb[name]
        cat = canon_category(name)
        rowit = ws.iter_rows(values_only=True)
        try:
            hdr = [str(c or "").strip() for c in next(rowit)]
        except StopIteration:
            continue
        if "域名" not in hdr:
            skipped.append({"sheet": name, "why": "没有「域名」列", "header": hdr[:8]})
            continue
        di = hdr.index("域名")
        n = 0
        for r in rowit:
            if di >= len(r):
                continue
            d = re.sub(r"^https?://", "", str(r[di] or "").strip().lower()).rstrip("/")
            if not d or "." not in d:
                continue
            n += 1
            # 同一个域名出现在两个分类里，必须报出来——后写入的悄悄覆盖前一个，
            # 会让这个站的分类取决于工作表顺序，纯属随机
            if d in seen and seen[d] != cat:
                dups.append({"domain": d, "categories": [seen[d], cat]})
            seen[d] = cat
            if d in known:
                mapping[known[d]] = cat
            else:
                future[d] = cat
        per_sheet[name] = n

    wb.close()
    return {
        "mapping": mapping,
        "future": future,
        "categories": sorted({canon_category(n) for n in wb.sheetnames
                              if n.strip() not in ("模板", "template", "Sheet1")}),
        "per_sheet": per_sheet,
        "conflicts": dups,
        "skipped_sheets": skipped,
        "total_rows": sum(per_sheet.values()),
        "sites_without_row": [s for s in sites if s not in mapping],
    }
