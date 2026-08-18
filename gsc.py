"""GSC 索引提交器 —— 命令行入口（用于定时任务/脚本自动化）。

界面操作请运行 server.py 或双击「启动.bat」。

  python gsc.py check
  python gsc.py submit --file urls.txt
  python gsc.py submit --sitemap https://example.com/sitemap.xml
  python gsc.py inspect --file urls.txt
  python gsc.py sitemap --submit https://example.com/sitemap.xml
  python gsc.py stats
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gscindex import config as config_mod
from gscindex import sources
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


def collect_urls(args, emit_fn) -> list[str]:
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
        sys.exit("accounts/ 目录里没有服务账号密钥")
    print(f"\n站点：{cfg.site_url or '（未设置）'}\n")
    for r in rows:
        flag = "OK" if r["ok"] and not r["error"] else "XX"
        print(f"[{flag}] {r['name']}  {r['email']}")
        if r["error"]:
            print("      " + r["error"])
        print(f"      提交 {r['submit_used']}/{r['submit_limit']}   "
              f"预检 {r['inspect_used']}/{r['inspect_limit']}")
    q = engine.quota_overview()
    print(f"\n今日剩余：提交 {q['submit_limit'] - q['submit_used']} 条，"
          f"预检 {q['inspect_limit'] - q['inspect_used']} 次")


def cmd_submit(args) -> None:
    cfg, _, engine = build(args)
    if not cfg.site_url:
        sys.exit("请用 --site 指定站点，或先在设置里配置")
    urls = collect_urls(args, emit)
    if not urls:
        sys.exit("没有拿到任何 URL")

    res = engine.analyze(
        urls, cfg.site_url,
        do_inspect=not args.no_inspect and cfg.inspect_before_submit,
        force=args.force, emit=emit,
    )
    targets = [r["url"] for r in res["rows"] if r["selected"]]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("\n没有需要提交的 URL")
        return
    if args.dry_run:
        print(f"\n演算模式：本应提交 {len(targets)} 条")
        for u in targets[:20]:
            print("   " + u)
        if len(targets) > 20:
            print(f"   …… 其余 {len(targets) - 20} 条")
        return

    out = engine.submit(
        targets, cfg.site_url,
        notif_type="URL_DELETED" if args.delete else "URL_UPDATED", emit=emit,
    )
    print(f"\n成功 {out['ok']} 条，失败 {out['failed']} 条")
    for r in out["results"]:
        if not r["ok"]:
            print(f"   XX {r['url']}  {r['message'][:120]}")


def cmd_inspect(args) -> None:
    cfg, _, engine = build(args)
    if not cfg.site_url:
        sys.exit("请用 --site 指定站点")
    urls = collect_urls(args, emit)
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
    print(f"\n累计 URL {s['total']} · 已提交 {s['submitted']} · 预检已收录 {s['indexed']}")
    print(f"今日提交 {q['submit_used']}/{q['submit_limit']} · "
          f"预检 {q['inspect_used']}/{q['inspect_limit']}\n")
    for d in store.daily_series(14):
        print(f"  {d['d']}  成功 {d['ok']:>4}  失败 {d['fail']:>4}")


def cmd_serve(args) -> None:
    import server
    server.main()


def main() -> None:
    p = argparse.ArgumentParser(prog="gsc", description="GSC 批量索引提交工具")
    p.add_argument("--site", help="GSC 属性，如 sc-domain:example.com")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="验证服务账号与配额").set_defaults(fn=cmd_check)

    s = sub.add_parser("submit", help="提交 URL 到索引")
    s.add_argument("--file", help="URL 清单文件")
    s.add_argument("--sitemap", help="从站点地图抓取 URL")
    s.add_argument("--limit", type=int, help="本次最多提交多少条")
    s.add_argument("--no-inspect", action="store_true", help="跳过收录预检")
    s.add_argument("--force", action="store_true", help="忽略重复提交保护")
    s.add_argument("--delete", action="store_true", help="发送删除通知而非更新")
    s.add_argument("--dry-run", action="store_true", help="只演算不发请求")
    s.set_defaults(fn=cmd_submit)

    i = sub.add_parser("inspect", help="查询收录状态")
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
