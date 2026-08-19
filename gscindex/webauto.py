"""自动化操作 GSC 网页的"请求编入索引"按钮。

**这个模块跟本项目其他部分的性质完全不同**，改动前务必读完：

其余模块（api.py / oauth.py / auth.py）走的都是 Google 官方开放的 API，
有文档、有配额、Google 认可这么用。这个模块没有 API 可用——Google 从未把
"请求编入索引"这个功能开放成接口，所以只能自动化操作网页本身，属于灰色地带，
且依赖 Google 页面当前的结构，对方改版就会失效。

几条红线，任何改动都不能突破：

1. 登录必须由用户本人在真实、可见的浏览器窗口里完成——脚本不经手密码，
   也不会替用户点登录相关的任何按钮。
2. 一旦页面出现验证码或"异常流量"之类的安全验证，立刻停止并如实上报，
   绝不尝试识别、绕过或自动处理验证码。这是硬性红线，不是待办事项。
3. 每个站点每天的点击次数在本地强制设上限，判断在发出下一次点击之前完成。
   注意：Google 那边真实的每日上限是多少并不清楚（官方没公开），
   本地这个上限只是个保守闸门，真正的天花板以 Google 返回"超出了配额"为准。

模块内的按钮定位方式和结果文案识别规则，都是 2026-08-19 用真实登录态实测
GSC 网页得到的第一手数据，不是猜的——改这些常量前请先实测确认。
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

OVERVIEW_URL = "https://search.google.com/search-console"
LOGIN_LANDING = "https://search.google.com/search-console/welcome"

# Playwright 自带的 Chromium 会被 Google 判定为"此浏览器或应用可能不安全"而拒绝登录，
# 所以必须调用用户本机真实安装的 Chrome / Edge。
CHROME_CANDIDATES = [
    (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome"),
    (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "chrome"),
    (str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"), "chrome"),
    (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "msedge"),
    (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "msedge"),
]

# 去掉最显眼的自动化特征。这不是为了"伪装成人类去绕过验证"——
# 验证码一旦真的出现，代码依然会立刻停止（见 _find_challenge）；
# 这里只是让正常的人工登录不至于被误判成机器人而无法进行。
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]
IGNORE_DEFAULT_ARGS = ["--enable-automation"]


def find_browser() -> tuple[str, str] | None:
    """返回 (可执行文件路径, 渠道名)，找不到返回 None。"""
    for path, channel in CHROME_CANDIDATES:
        if Path(path).exists():
            return path, channel
    return None


def kill_stale_browsers(prof: Path) -> int:
    """清掉还占着这个配置目录的残留浏览器进程，返回清理数量。

    为什么需要这个：Windows 上 Chrome 的"同一配置目录只允许一个实例"是靠进程持有的
    窗口消息实现的，不是靠 lockfile。Chrome 启动时经常会自我重启一次（换一个 PID），
    Playwright 只记得最初那个 PID，收尾时杀不掉重启后的进程，于是残留下来。
    下次再启动，新进程发现"这个目录已经有实例"，就把请求转交过去然后自己退出——
    Playwright 那头就表现为 "Target page, context or browser has been closed"。

    只杀命令行里带着这个配置目录路径的进程，不会碰用户日常的浏览器。
    """
    import subprocess

    target = str(prof.resolve()).lower()
    killed = 0
    try:
        # 用 WMIC 的替代方案 PowerShell 查命令行，逐个匹配 user-data-dir
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" "
             "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=25,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return 0
        import json as _json

        data = _json.loads(out.stdout)
        if isinstance(data, dict):
            data = [data]
        for proc in data:
            cmd = (proc.get("CommandLine") or "").lower()
            if target in cmd:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(proc["ProcessId"])],
                        capture_output=True, timeout=10,
                    )
                    killed += 1
                except Exception:
                    pass
    except Exception:
        return killed  # 清理失败不该阻断主流程，后面启动失败会有明确报错
    if killed:
        time.sleep(1.5)  # 给 Chrome 释放单例锁留点时间
    return killed


def profile_dir(session_path: Path) -> Path:
    """给自动化用的独立浏览器配置目录。

    刻意不使用用户日常那个 Chrome 配置目录：一是 Chrome 正在运行时配置目录被锁、
    根本起不来；二是不该让自动化操作碰用户的日常浏览数据。
    这里用项目自己的目录，用户在里面登录一次，之后长期复用。
    """
    return session_path.parent / "browser_profile"

# 检测"需要人工介入"的信号：验证码、异常流量提示、被要求重新登录。
# 命中任何一条都必须整体停止，不能有下一步自动操作。
CHALLENGE_URL_HINTS = ("google.com/sorry", "accounts.google.com/v3/signin/challenge", "/signin/rejected")
CHALLENGE_TEXT_HINTS = (
    "unusual traffic", "异常流量", "我们的系统检测到", "verify you're not a robot",
    "此浏览器或应用可能不安全", "this browser or app may not be secure",
)

# 下面这几组文案是 2026-08-19 用真实登录态实测 wristtruth.com 拿到的第一手数据，
# 不再是猜测：
#   真实按钮是 role=button、aria-label 含"请求编入索引"（不是文字节点本身可点，
#   文字往上数第 6 层父元素才是真正的 <div role="button">）。
#   点击后依次经过两段处理文案，最终成功态是"已请求编入索引"+"已将网址添加到...队列"。
# QUOTA/ALREADY 两组还是没能触发验证的猜测（没有真的把配额用尽、也没有再次提交
# 同一个刚成功的 URL 去看会不会有不同文案），第一次真撞见了把日志发回来再核对。
REQUEST_BUTTON_PATTERNS = [r"请求编入索引", r"申请编入索引", r"REQUEST\s+INDEXING"]

# 处理中的过渡文案——命中就该继续等，不是最终结果
PROCESSING_PATTERNS = [r"正在测试实际网址可否编入索引", r"正在提交请求", r"testing if"]

RESULT_SUCCESS_PATTERNS = [
    r"已请求编入索引", r"已将.*网址.*添加.*(优先)?抓取队列",
    r"已提交.*索引请求", r"indexing requested", r"has been added to.*queue",
]
# "配额超限"是实测真实拿到的文案（对同一个网址两分钟内二次提交就触发了，
# 说明这个每日配额比社区流传的"十来次"要严格得多，可能是按网址而不是按站点算）。
RESULT_QUOTA_PATTERNS = [r"超出了配额", r"超出了.*每日配额", r"quota", r"exceeded.*quota"]
# 这组仍然是没能触发验证的猜测——实测中"重复提交同一网址"命中的是配额超限，
# 而不是某种"最近申请过"的独立提示，所以这组模式的真实存在性存疑，优先级放最后。
RESULT_ALREADY_PATTERNS = [r"最近.*(检查|请求)过", r"recently (checked|requested)"]


class ChallengeDetected(Exception):
    """页面出现验证码/安全验证，必须整体停止，不能继续自动化。"""


@dataclass
class RequestResult:
    ok: bool
    status: str  # success / already_done / quota_exceeded / challenge / no_session / error
    message: str = ""


def has_session(session_path: Path) -> bool:
    """登录状态现在体现为"持久化配置目录里有 Cookie 数据"，不再是单独的 state 文件。"""
    prof = profile_dir(session_path)
    return (prof / "Default" / "Cookies").exists() or (prof / "Default" / "Network" / "Cookies").exists()


def _find_challenge(page: Page) -> str | None:
    url = page.url.lower()
    for hint in CHALLENGE_URL_HINTS:
        if hint in url:
            return "页面跳转到了验证/挑战地址：" + page.url
    try:
        body_text = page.locator("body").inner_text(timeout=2000).lower()
    except Exception:
        body_text = ""
    for hint in CHALLENGE_TEXT_HINTS:
        if hint.lower() in body_text:
            return "页面出现安全验证提示：" + hint
    return None


def bootstrap_login(session_path: Path, *, timeout_seconds: int = 900) -> tuple[bool, str]:
    """打开用户本机真实的 Chrome，用户在里面手动登录自己的 Google 账号。

    必须用真实 Chrome：Playwright 自带的 Chromium 会被 Google 判定为
    "此浏览器或应用可能不安全"，登录页直接不让往下走。

    脚本只负责打开窗口、等待、保存登录状态；全程不触碰密码输入框，
    也不代替用户完成登录的任何一步。用户可以慢慢来，包括处理两步验证。
    """
    found = find_browser()
    if not found:
        return False, (
            "没找到本机安装的 Chrome 或 Edge。请先安装 Chrome："
            "https://www.google.cn/chrome/"
        )
    exe_path, channel = found
    prof = profile_dir(session_path)
    prof.mkdir(parents=True, exist_ok=True)
    kill_stale_browsers(prof)  # 清掉上次没退干净的进程，否则新窗口会被转交后立刻退出

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(prof),
                executable_path=exe_path,
                headless=False,
                args=STEALTH_ARGS,
                ignore_default_args=IGNORE_DEFAULT_ARGS,
                viewport=None,
            )
        except Exception as exc:
            return False, "启动本机浏览器失败：" + str(exc)[:200]

        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(LOGIN_LANDING, wait_until="domcontentloaded", timeout=60000)
        except PWTimeout:
            pass  # 网络慢不算失败，让用户自己在窗口里继续
        except Exception:
            pass  # 同样可能是 Chrome 自我重启导致的连接抖动，交给下面的轮询循环处理

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            time.sleep(2)
            try:
                url = page.url
            except Exception:
                try:
                    context.close()
                except Exception:
                    pass
                # 持久化目录已经落盘，窗口被关掉也可能已经登录成功了
                if has_session(session_path):
                    return True, "浏览器已关闭，检测到登录状态已保存"
                return False, "浏览器窗口被关闭，登录未完成"
            if "accounts.google.com" not in url:
                try:
                    page.wait_for_selector(
                        "text=/Search Console|搜索资源报告|资源|属性/i", timeout=3000
                    )
                    context.close()
                    kill_stale_browsers(prof)  # Playwright 杀不掉自我重启后的 Chrome，这里补一刀
                    return True, "登录成功，登录状态已保存到独立的浏览器配置目录"
                except PWTimeout:
                    continue
                except Exception:
                    # Chrome 登录过程中常会自我重启一次进程（跟 kill_stale_browsers
                    # 要处理的是同一个根因），这一刻正好在等选择器，Playwright 手里
                    # 那个 page 引用就失效了，抛的不是 PWTimeout 而是"页面已关闭"。
                    # 但持久化配置目录下 Cookie 是 Chrome 自己实时写盘的，不依赖
                    # Playwright 这边活着——只要选择器命中过，大概率已经真登录成功了，
                    # 用 has_session 这个磁盘上的事实做准判断，而不是让异常直接炸穿任务。
                    try:
                        context.close()
                    except Exception:
                        pass
                    kill_stale_browsers(prof)
                    if has_session(session_path):
                        return True, "浏览器进程重启导致连接中断，但检测到登录状态已保存"
                    return False, "浏览器连接意外中断，且未检测到登录状态，请重新点「开始登录」"
        try:
            context.close()
        except Exception:
            pass
        kill_stale_browsers(prof)
        return False, f"等待 {timeout_seconds // 60} 分钟仍未检测到登录完成，请重试"


def _do_one(page, site_url: str, url: str, *, nav_timeout_ms: int = 30000) -> RequestResult:
    """在一个已经打开的页面上，对单个 URL 走完一次"请求编入索引"。

    抽出来是为了批量处理时能复用同一个浏览器：启动 Chrome 加载 GSC 大约要十几秒，
    每条 URL 都重开一次的话这部分开销会被重复 N 遍。这个函数假定调用方已经
    准备好了 page，只负责"输入网址 -> 点按钮 -> 等结果"这段。
    """
    from urllib.parse import quote

    # GSC 的网址检查结果页地址里的 id 参数是 Google 生成的不透明哈希，不是网址本身，
    # 没法直接拼 URL 跳转到检查结果——必须回到属性概览页，在顶部搜索框输入网址回车，
    # 这是唯一能真正走到检查结果的路径（已用真实登录态验证过）。
    overview_url = f"{OVERVIEW_URL}?resource_id={quote(site_url, safe='')}"

    # 批量处理时每条都要回到概览页重新搜索，否则搜索框里还是上一条的结果
    page.goto(overview_url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
    page.wait_for_timeout(2500)

    challenge = _find_challenge(page)
    if challenge:
        return RequestResult(False, "challenge", challenge)

    try:
        box = page.locator("input[type=text]").first
        box.wait_for(state="visible", timeout=10000)
    except PWTimeout:
        return RequestResult(
            False, "error",
            "没找到顶部的网址检查搜索框，可能是这个站点属性打不开、"
            "或者 Google 改了页面结构。当前页面标题：" + (page.title() or "(无)"),
        )
    box.click()
    page.wait_for_timeout(300)
    box.fill(url)
    page.wait_for_timeout(300)
    page.keyboard.press("Enter")
    page.wait_for_timeout(4000)

    challenge = _find_challenge(page)
    if challenge:
        return RequestResult(False, "challenge", challenge)

    btn = page.get_by_role("button", name=re.compile(_pattern_or(REQUEST_BUTTON_PATTERNS), re.I))
    try:
        btn.first.wait_for(state="visible", timeout=10000)
    except PWTimeout:
        return RequestResult(
            False, "error",
            "没找到「请求编入索引」按钮，可能是这个网址检查结果本身有问题"
            "（比如不属于这个站点属性），或者 Google 改了页面结构。"
            "当前页面标题：" + (page.title() or "(无)"),
        )
    btn.first.click()

    # 点击后会先后经过两段处理文案（"正在测试实际网址可否编入索引"
    # -> "正在提交请求"），中间偶尔还有一小段两者都不匹配的空档期，
    # 所以不能"文案不再是处理中就当作有结果"——必须一直等到真正出现
    # 已知的最终结果文案（成功/配额超限）才停，否则会在空档期误判成
    # "无法识别"。官方提示这一步可能要 1~2 分钟。
    deadline = time.time() + 150
    body_text = ""
    while time.time() < deadline:
        time.sleep(2)
        challenge = _find_challenge(page)
        if challenge:
            return RequestResult(False, "challenge", challenge)
        try:
            body_text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            continue

        if any(re.search(pat, body_text, re.I) for pat in RESULT_QUOTA_PATTERNS):
            return RequestResult(False, "quota_exceeded", "今日配额已用尽（Google 侧）")
        if any(re.search(pat, body_text, re.I) for pat in RESULT_SUCCESS_PATTERNS):
            return RequestResult(True, "success", "已请求编入索引")
        if any(re.search(pat, body_text, re.I) for pat in RESULT_ALREADY_PATTERNS):
            return RequestResult(True, "already_done", "最近已经申请过，本次视为已处理")
        # 既不是已知的最终结果，也判断不出是不是还在处理中过渡态——
        # 不管是不是，只要还没超时就继续等，避免在两段处理文案之间的
        # 空档期被误判成"无法识别"。

    return RequestResult(
        False, "error",
        "等了 150 秒也没等到已知的结果文案，无法确认是否成功。"
        "把这条反馈回去，我需要根据实际弹出的文字调整识别规则。当前页面片段："
        + body_text[-200:],
    )


def request_indexing_batch(
    session_path: Path,
    site_url: str,
    urls: list[str],
    *,
    headless: bool = False,
    nav_timeout_ms: int = 30000,
    on_result=None,
    should_stop=None,
    take_quota=None,
    delay=None,
) -> list[tuple[str, RequestResult]]:
    """一个浏览器会话跑完整批 URL，避免每条都重启浏览器。

    启动 Chrome 并加载 GSC 大约要十几秒，逐条重开的话这部分开销会被重复 N 遍。
    这里只启动一次、只清理一次残留进程，然后循环处理。

    几个回调让调用方能在不侵入浏览器逻辑的前提下控制流程：
      on_result(url, result) —— 每条出结果就回调，界面可以实时更新
      should_stop()          —— 返回 True 就停止（用户点了停止按钮）
      take_quota(url)        —— 返回 False 表示名额不够，跳过剩下的
      delay()                —— 两条之间的随机间隔

    遇到验证码或 Google 侧配额用尽会立即中断整批——继续点下去没有意义，
    而且在已经被限流的情况下继续操作只会让情况更糟。
    """
    if not has_session(session_path):
        return [(u, RequestResult(False, "no_session", "还没有登录会话，请先完成一次登录")) for u in urls]

    found = find_browser()
    if not found:
        return [(u, RequestResult(False, "error", "没找到本机安装的 Chrome 或 Edge")) for u in urls]
    exe_path, _channel = found

    prof = profile_dir(session_path)
    kill_stale_browsers(prof)

    out: list[tuple[str, RequestResult]] = []
    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(prof),
                executable_path=exe_path,
                headless=headless,
                args=STEALTH_ARGS,
                ignore_default_args=IGNORE_DEFAULT_ARGS,
                viewport=None,
            )
        except Exception as exc:
            res = RequestResult(False, "error", "启动浏览器失败：" + str(exc)[:200])
            return [(u, res) for u in urls]

        page = context.pages[0] if context.pages else context.new_page()
        try:
            for i, url in enumerate(urls):
                if should_stop and should_stop():
                    break
                if take_quota and not take_quota(url):
                    break

                try:
                    res = _do_one(page, site_url, url, nav_timeout_ms=nav_timeout_ms)
                except PWTimeout as exc:
                    res = RequestResult(False, "error", "页面加载超时：" + str(exc)[:160])
                except Exception as exc:
                    # Chrome 中途自我重启会让 page 引用失效，抛的是"页面已关闭"。
                    # 这一条没法判断成败，如实报错并中断整批——page 已经废了，
                    # 后面的循环也做不了什么。
                    res = RequestResult(False, "error", "浏览器连接意外中断：" + str(exc)[:160])
                    out.append((url, res))
                    if on_result:
                        on_result(url, res)
                    break

                out.append((url, res))
                if on_result:
                    on_result(url, res)

                # 验证码和 Google 侧配额用尽都必须立刻停整批
                if res.status in ("challenge", "quota_exceeded"):
                    break
                if i < len(urls) - 1 and delay and not (should_stop and should_stop()):
                    delay()
        finally:
            try:
                context.close()
            except Exception:
                pass
            kill_stale_browsers(prof)  # 不清的话残留进程会越积越多，下次启动必然失败
    return out


def request_indexing(
    session_path: Path,
    site_url: str,
    url: str,
    *,
    headless: bool = False,
    nav_timeout_ms: int = 30000,
) -> RequestResult:
    """单条便捷入口（CLI 和只交一条时用），内部走批量实现。"""
    res = request_indexing_batch(
        session_path, site_url, [url], headless=headless, nav_timeout_ms=nav_timeout_ms
    )
    return res[0][1] if res else RequestResult(False, "error", "没有返回结果")


def random_delay(min_s: int, max_s: int) -> None:
    time.sleep(random.uniform(min_s, max_s))
