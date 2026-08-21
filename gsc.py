"""GSC 索引提交器 —— 命令行入口（用于定时任务/脚本自动化）。

界面操作请运行 server.py 或双击「启动.bat」。

  python gsc.py check                                     验证凭据与配额
  python gsc.py scan  --sitemap https://a.com/sitemap.xml  全站扫描，未收录的进待办池
  python gsc.py links                                      看链接清单（默认只看未收录）
  python gsc.py links --state all                           看全部链接
  python gsc.py links --state indexed                       只看已收录的
  python gsc.py request --limit 2                           挑几条未收录的去网页申请
  python gsc.py recheck                                     复查申请过的现在收录了没
  python gsc.py traffic --refresh                           拉取并显示流量排行
  python gsc.py traffic --metric impressions --min 1000     筛出展现量过 1000 的站点
  python gsc.py inspect --file urls.txt                     只查收录状态
  python gsc.py sitemap --submit https://a.com/sitemap.xml  提交站点地图
  python gsc.py stats                                       看统计
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gscindex import config as config_mod
from gscindex import sources, traffic, webauto
from gscindex.auth import AccountPool
from gscindex.runner import Engine
from gscindex.store import Store

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LEVEL_MARK = {"info": "  ", "success": "OK", "warn": "!!", "error": "XX"}


def emit(evt: dict) -> None:
    if evt.get("type") == "log":
        print(LEVEL_MARK.get(evt.get("level", "info"), "  "), evt.get("message", ""))
    elif evt.get("type") == "progress" and evt.get("total"):
        done, total = evt["done"], evt["total"]
        bar = int(done / total * 30)
        end = "\n" if done >= total else ""
        print(f"\r   [{'#' * bar}{'.' * (30 - bar)}] {done}/{total}", end=end, flush=True)


def build(args) -> tuple:
    cfg = config_mod.load()
    if getattr(args, "site", None):
        cfg.site_url = args.site
    store = Store(cfg.db_path)
    pool = AccountPool(cfg.accounts_path, store)
    return cfg, store, Engine(cfg, store, pool)


def collect_urls(args) -> list[str]:
    urls: list[str] = []
    if getattr(args, "file", None):
        path = Path(args.file)
        if not path.exists():
            sys.exit("找不到文件：" + str(path))
        parsed, bad = sources.parse_text(path.read_text(encoding="utf-8", errors="ignore"))
        urls += parsed
        print(f"   从 {path} 读取 {len(parsed)} 条" + (f"，{bad} 行无法识别" if bad else ""))
    if getattr(args, "sitemap", None):
        found, visited, errors = sources.fetch_sitemap(args.sitemap)
        for e in errors:
            print("!! ", e)
        print(f"   站点地图 {len(visited)} 个文件，得到 {len(found)} 条 URL")
        urls += found
    return sources.dedupe(urls)


def cmd_check(args) -> None:
    cfg, _, engine = build(args)
    rows = engine.check_accounts(cfg.site_url)
    if not rows:
        sys.exit("accounts/ 目录里没有任何凭据")
    print(f"\n站点：{cfg.site_url or '（未设置）'}\n")
    print("— API 凭据（查站点 / 查收录，官方接口）—")
    for r in rows:
        flag = "OK" if r["ok"] and not r["error"] else "XX"
        print(f"[{flag}] {r['name']}  {r['email']}  ({r.get('kind_cn', '')})")
        if r["error"]:
            print("      " + r["error"])
        print(f"      预检 {r['inspect_used']}/{r['inspect_limit']}")
    q = engine.quota_overview()
    print(f"\n今日预检剩余：{q['inspect_limit'] - q['inspect_used']} 次")

    w = engine.webauto_overview(cfg.site_url)
    print("\n— 浏览器登录（点网页上的「请求编入索引」）—")
    print("  登录状态：" + ("已登录" if w["logged_in"] else "未登录（请到网页界面完成一次登录）"))
    if cfg.site_url:
        print(f"  {cfg.site_url} 今日已用 {w['used']}/{w['limit']}，本地还剩 {w['left']} 次")
        print("  注意：Google 侧真实上限不明，可能提前撞上「超出了配额」")


def cmd_scan(args) -> None:
    cfg, _, engine = build(args)
    if not cfg.site_url:
        sys.exit("请用 --site 指定站点，或先在设置里配置")
    sitemap = args.sitemap or _guess_sitemap(cfg.site_url)
    print(f"\n站点：{cfg.site_url}\n站点地图：{sitemap}\n")
    res = engine.scan(cfg.site_url, sitemap, limit=args.limit or 0, emit=emit)
    if res.get("error"):
        sys.exit("\n扫描失败：" + res["error"])
    print(f"\n发现 {res['found']} 条 · 未收录入池 {res['pending']} 条 · 已收录 {res['resolved']} 条")


def _guess_sitemap(site_url: str) -> str:
    if site_url.startswith("sc-domain:"):
        return "https://" + site_url[len("sc-domain:"):].rstrip("/") + "/sitemap.xml"
    return (site_url if site_url.endswith("/") else site_url + "/") + "sitemap.xml"


STATE_MARK = {"indexed": "已收录", "not_indexed": "未收录", "unknown": "未查明"}


def cmd_links(args) -> None:
    """站点链接清单。默认只列未收录的，--state 可切换。"""
    cfg, store, engine = build(args)
    rows = store.url_list(
        "" if args.all else cfg.site_url,
        index_state="" if args.state == "all" else args.state,
    )
    counts = store.url_counts()
    print("\n各站点概览：")
    for c in counts:
        print(
            f"  {c['site']}"
            f"  共 {c['total']} · 已收录 {c['indexed']} · 未收录 {c['not_indexed']}"
            + (f" · 未查明 {c['unknown']}" if c["unknown"] else "")
            + f"（未收录中：从未申请 {c['never_requested']} · 已申请 {c['requested']}）"
        )
    if not counts:
        print("  （空）先跑一次 scan")

    scope = "全部站点" if args.all else "当前站点"
    kind = {"all": "全部", "indexed": "已收录", "not_indexed": "未收录",
            "unknown": "未查明"}[args.state]
    print(f"\n{scope}{kind}明细（前 {args.limit} 条）：")
    for r in rows[: args.limit]:
        mark = STATE_MARK.get(r["index_state"], r["index_state"])
        req = f"已申请{r['request_count']}次" if r["requested_at"] else "未申请"
        print(f"  [{mark:>6}|{req:>9}] {r['coverage'] or '?':<16} {r['url']}")
    if len(rows) > args.limit:
        print(f"  …… 其余 {len(rows) - args.limit} 条")


def cmd_recheck(args) -> None:
    """复查收录状态：看之前申请的链接现在收录了没。"""
    cfg, _, engine = build(args)
    if not cfg.site_url:
        sys.exit("请用 --site 指定站点")
    res = engine.recheck(cfg.site_url, emit=emit)
    print(f"\n复查 {res['checked']} 条：新确认收录 {res['newly_indexed']} 条，"
          f"仍未收录 {res['still_not']} 条")


def cmd_request(args) -> None:
    """从待办池取前 N 条，去 GSC 网页申请收录。"""
    cfg, store, engine = build(args)
    if not cfg.site_url:
        sys.exit("请用 --site 指定站点")
    if not webauto.has_session(cfg.webauto_session_path):
        sys.exit("还没有浏览器登录。请先运行网页界面，在「账号管理」完成一次浏览器登录。")

    # 只从"未收录"里挑——已收录的没必要再申请，白费稀缺名额
    rows = store.url_list(cfg.site_url, index_state=store.NOT_INDEXED)
    # 优先挑从没申请过的
    targets = [r["url"] for r in rows if not r["requested_at"]][: args.limit]
    if not targets:
        targets = [r["url"] for r in rows][: args.limit]
    if not targets:
        sys.exit("没有未收录的链接可申请，先跑一次 scan")

    w = engine.webauto_overview(cfg.site_url)
    print(f"\n{cfg.site_url} 今日已用 {w['used']}/{w['limit']}，准备提交 {len(targets)} 条：")
    for u in targets:
        print("   " + u)
    if args.dry_run:
        print("\n演算模式，未实际提交")
        return
    print()
    out = engine.webauto_submit(cfg.site_url, targets, emit=emit)
    print(f"\n成功 {out['ok']} · 失败 {out['failed']} · 跳过 {out['skipped']}")


def cmd_inspect(args) -> None:
    cfg, _, engine = build(args)
    urls = collect_urls(args)
    if not urls:
        sys.exit("没有拿到任何 URL")
    # --auto（或压根没设站点）时让引擎逐条判定归属：清单文件里的链接
    # 完全可能横跨好几个站点，强迫用户先 --site 一个再跑是本末倒置
    auto = getattr(args, "auto", False) or not cfg.site_url
    res = engine.analyze(urls, "" if auto else cfg.site_url,
                         do_inspect=True, force=True, emit=emit)
    if res.get("error"):
        sys.exit(res["error"])
    print()
    for r in res["rows"]:
        site = _short_site(r.get("site")) if auto else ""
        print(f"[{r['verdict'] or '?':>7}] {r['coverage'] or r['state_cn']:<16} "
              + (f"{site:<24} " if auto else "") + r["url"])
    unk = res.get("unknown") or []
    if unk:
        # 判不出归属的单独列在最后并说明原因，不能混在上面一大片里滚过去
        print()
        print(f"!! {len(unk)} 条判不出属于哪个站点，不会提交：")
        for x in unk:
            print("   " + (x.get("input") or "") + " —— " + _unknown_cn(x))


def _short_site(s: str) -> str:
    s = str(s or "")
    return (s.replace("sc-domain:", "").replace("https://", "")
            .replace("http://", "").rstrip("/")) or "?"


def _unknown_cn(x: dict) -> str:
    if x.get("reason") == "typo":
        return (f"你的属性里没有 {x.get('host')}，但有 {x.get('near_host')}"
                f"（差 {x.get('near_distance')} 个字符），很可能打错了")
    if x.get("reason") == "not_a_property":
        return f"{x.get('host')} 不在你的任何 GSC 属性里"
    return "解析不出合法网址"


def cmd_sitemap(args) -> None:
    cfg, _, engine = build(args)
    if args.submit:
        ok, msg = engine.sitemap_submit(cfg.site_url, args.submit)
        print(("OK  " if ok else "XX  ") + msg)
        return
    ok, items, err = engine.sitemap_list(cfg.site_url)
    if not ok:
        sys.exit(err)
    for s in items:
        print(f"{s.get('lastSubmitted', '')[:10]}  {s.get('path', '')}")


def cmd_stats(args) -> None:
    _, store, engine = build(args)
    s = store.stats()
    q = engine.quota_overview()
    print(f"\n累计 URL {s['total']} · 预检已收录 {s['indexed']}")
    print(f"待办池：{s['pending_open']} 条（其中从未提交 {s['pending_never']} 条）")
    print(f"今日预检 {q['inspect_used']}/{q['inspect_limit']}")
    print("\n按来源统计（webauto = 网页自动化，indexing_api = 已移除的旧通道）：")
    for b in store.source_breakdown():
        print(f"  {b['source']:<14} 共 {b['total']:>4} 次，成功 {b['ok']:>4} 次")
    print("\n最近 14 天：")
    for d in store.daily_series(14):
        print(f"  {d['d']}  成功 {d['ok']:>4}  失败 {d['fail']:>4}")


def cmd_traffic(args) -> None:
    """流量排行与筛选。--min/--max 就是"筛出流量达标的站点"。"""
    cfg, store, engine = build(args)
    w = args.window or cfg.traffic_window_days

    if args.refresh:
        sites = [args.site] if args.site else None
        res = engine.traffic_refresh(sites, window_days=w, emit=emit)
        if res.get("error"):
            sys.exit("拉取失败：" + res["error"])
        print()

    op, val, val2 = "gte", None, None
    if args.min is not None and args.max is not None:
        op, val, val2 = "between", args.min, args.max
    elif args.min is not None:
        op, val = "gte", args.min
    elif args.max is not None:
        op, val = "lte", args.max

    try:
        rows = store.traffic_rank(w, metric=args.metric, op=op, value=val, value2=val2)
    except ValueError as exc:
        sys.exit(str(exc))

    cond = ""
    if val is not None:
        cond = f"（{args.metric} " + (f"{val}~{val2}" if val2 is not None
                                     else ("≥" if op == "gte" else "≤") + str(val)) + "）"
    gs, ge = traffic.window(w, lag=traffic.GSC_LAG_DAYS)
    as_, ae = traffic.window(w)
    win_cn = "今日" if w == 1 else f"近 {w} 天"

    print()
    print(f"{win_cn}流量排行{cond}，命中 {len(rows)} 个站点：")
    print()
    print(f"  {'站点':<36} {'曝光':>7} {'点击':>6} {'排名':>7} {'GA会话':>8} {'GA事件':>8}")
    for r in rows[: args.limit]:
        ga = str(r["sessions"]) if r["ga_ok"] else "-"
        ev = str(r["events"]) if (r["ga_ok"] and r["events"] is not None) else "-"
        print(f"  {r['site']:<36} {r['impressions'] or 0:>7} {r['clicks'] or 0:>6}"
              f" {(r['position'] or 0):>7.1f} {ga:>8} {ev:>8}")
    if len(rows) > args.limit:
        print(f"  …… 其余 {len(rows) - args.limit} 个")
    print()
    # 两边查的不是同一段日期，必须写清楚。窗口为 1 天时差得最远：
    # GA 是今天，GSC 是三天前——不说明的话同一行的数字看着像同一天的。
    gsc_span = gs if gs == ge else f"{gs}~{ge}"
    ga_span = as_ if as_ == ae else f"{as_}~{ae}"
    print(f"注：GSC 列的日期是 {gsc_span}（Google 自身延迟 {traffic.GSC_LAG_DAYS} 天，"
          f"没有今天的数据）；GA 列是 {ga_span}。")
    print("    两边不是同一段日期，不要横向对照。GA 列为 - 表示还没授权或还没配属性。")


def cmd_serve(args) -> None:
    import server

    server.main()


def main() -> None:
    p = argparse.ArgumentParser(prog="gsc", description="GSC 索引工具")
    p.add_argument("--site", help="GSC 属性，如 sc-domain:example.com")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="验证凭据、登录状态与配额").set_defaults(fn=cmd_check)

    sc = sub.add_parser("scan", help="全站扫描：抓站点地图并查收录，未收录的进待办池")
    sc.add_argument("--sitemap", help="站点地图地址，默认猜 {站点}/sitemap.xml")
    sc.add_argument("--limit", type=int, help="本次最多处理多少条 URL")
    sc.set_defaults(fn=cmd_scan)

    ln = sub.add_parser("links", help="查看站点链接清单及收录状态")
    ln.add_argument("--all", action="store_true", help="显示全部站点，而非仅当前站点")
    ln.add_argument("--state", default="not_indexed",
                    choices=["all", "indexed", "not_indexed", "unknown"],
                    help="按收录状态筛选，默认只看未收录的")
    ln.add_argument("--limit", type=int, default=30, help="明细显示条数")
    ln.set_defaults(fn=cmd_links)

    rc = sub.add_parser("recheck", help="复查之前申请的链接现在收录了没")
    rc.set_defaults(fn=cmd_recheck)

    rq = sub.add_parser("request", help="从待办池取几条，去 GSC 网页申请收录")
    rq.add_argument("--limit", type=int, default=1, help="本次提交几条（名额稀缺，默认 1）")
    rq.add_argument("--dry-run", action="store_true", help="只显示会交哪些，不实际提交")
    rq.set_defaults(fn=cmd_request)

    i = sub.add_parser("inspect", help="只查收录状态，不做任何提交")
    i.add_argument("--file", help="URL 清单文件，一行一条，# 开头为注释")
    i.add_argument("--sitemap")
    i.add_argument("--auto", action="store_true",
                   help="不指定站点，逐条自动判定每个 URL 属于哪个 GSC 属性"
                        "（清单里可以混着多个站点的链接）")
    i.set_defaults(fn=cmd_inspect)

    m = sub.add_parser("sitemap", help="站点地图管理")
    m.add_argument("--submit", help="提交站点地图给 GSC")
    m.set_defaults(fn=cmd_sitemap)

    tr = sub.add_parser("traffic", help="流量排行与筛选（GSC 曝光/点击 + GA 会话/事件）")
    tr.add_argument("--refresh", action="store_true", help="先拉取最新数据再显示")
    tr.add_argument("--window", type=int,
                    help="统计窗口天数，默认取配置值；--window 1 就是「今日」"
                         "（GA 是今天，GSC 是它最新可用的那一天）")
    # choices 直接取自后端白名单，不再另抄一份——上次加 events 时这里就漏了，
    # 后端支持而 CLI 报"invalid choice"。
    tr.add_argument("--metric", default="impressions",
                    choices=sorted(Store.TRAFFIC_METRICS),
                    help="排序/筛选用的指标")
    tr.add_argument("--min", type=float, help="下限，比如 --min 1000")
    tr.add_argument("--max", type=float, help="上限；跟 --min 同时给就是区间")
    tr.add_argument("--limit", type=int, default=40, help="显示条数")
    tr.set_defaults(fn=cmd_traffic)

    sub.add_parser("stats", help="查看统计").set_defaults(fn=cmd_stats)
    sub.add_parser("serve", help="启动网页界面").set_defaults(fn=cmd_serve)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
