"""GSC 索引提交器 —— 命令行入口（用于定时任务/脚本自动化）。

界面操作请运行 server.py 或双击「启动.bat」。

  python gsc.py check                                     验证凭据与配额
  python gsc.py scan  --sitemap https://a.com/sitemap.xml  全站扫描，未收录的进待办池
  python gsc.py pending                                    看待办池
  python gsc.py request --limit 2                           从待办池取几条去网页申请收录
  python gsc.py inspect --file urls.txt                     只查收录状态
  python gsc.py sitemap --submit https://a.com/sitemap.xml  提交站点地图
  python gsc.py stats                                       看统计
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gscindex import config as config_mod
from gscindex import sources, webauto
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


def cmd_pending(args) -> None:
    cfg, store, engine = build(args)
    rows = store.pending_list(cfg.site_url if not args.all else "")
    counts = store.pending_counts()
    print("\n各站点待办概览：")
    for c in counts:
        print(f"  {c['site']}  待办 {c['total']}（从未提交 {c['never']} · 已交待观察 {c['waiting']}）")
    if not counts:
        print("  （空）先跑一次 scan")
    print(f"\n{'当前站点' if not args.all else '全部'}待办明细（前 {args.limit} 条）：")
    for r in rows[: args.limit]:
        mark = "未交" if not r["requested_at"] else f"已交{r['request_count']}次"
        print(f"  [{mark}] {r['coverage'] or '?'}  {r['url']}")
    if len(rows) > args.limit:
        print(f"  …… 其余 {len(rows) - args.limit} 条")


def cmd_request(args) -> None:
    """从待办池取前 N 条，去 GSC 网页申请收录。"""
    cfg, store, engine = build(args)
    if not cfg.site_url:
        sys.exit("请用 --site 指定站点")
    if not webauto.has_session(cfg.webauto_session_path):
        sys.exit("还没有浏览器登录。请先运行网页界面，在「账号管理」完成一次浏览器登录。")

    rows = store.pending_list(cfg.site_url)
    # 优先挑从没交过的
    targets = [r["url"] for r in rows if not r["requested_at"]][: args.limit]
    if not targets:
        targets = [r["url"] for r in rows][: args.limit]
    if not targets:
        sys.exit("待办池是空的，先跑一次 scan")

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
    if not cfg.site_url:
        sys.exit("请用 --site 指定站点")
    urls = collect_urls(args)
    if not urls:
        sys.exit("没有拿到任何 URL")
    res = engine.analyze(urls, cfg.site_url, do_inspect=True, force=True, emit=emit)
    print()
    for r in res["rows"]:
        print(f"[{r['verdict'] or '?':>7}] {r['coverage'] or r['state_cn']:<16} {r['url']}")


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

    pd = sub.add_parser("pending", help="查看未收录待办池")
    pd.add_argument("--all", action="store_true", help="显示全部站点，而非仅当前站点")
    pd.add_argument("--limit", type=int, default=30, help="明细显示条数")
    pd.set_defaults(fn=cmd_pending)

    rq = sub.add_parser("request", help="从待办池取几条，去 GSC 网页申请收录")
    rq.add_argument("--limit", type=int, default=1, help="本次提交几条（名额稀缺，默认 1）")
    rq.add_argument("--dry-run", action="store_true", help="只显示会交哪些，不实际提交")
    rq.set_defaults(fn=cmd_request)

    i = sub.add_parser("inspect", help="只查收录状态，不做任何提交")
    i.add_argument("--file")
    i.add_argument("--sitemap")
    i.set_defaults(fn=cmd_inspect)

    m = sub.add_parser("sitemap", help="站点地图管理")
    m.add_argument("--submit", help="提交站点地图给 GSC")
    m.set_defaults(fn=cmd_sitemap)

    sub.add_parser("stats", help="查看统计").set_defaults(fn=cmd_stats)
    sub.add_parser("serve", help="启动网页界面").set_defaults(fn=cmd_serve)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
