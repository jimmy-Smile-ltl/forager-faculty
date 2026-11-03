#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器生命周期统一管理 (2026-09-02 用户指令: 策略整合到 util/)。

策略总纲 — 独立浏览器 + 按需启动 + 空闲回收:
  - 2026-09-10 架构升级(用户指令): 项目浏览器一律用**独立 Chromium**
    (CloakBrowser Chromium 或 playwright Chrome for Testing, bundle ID
    与个人 Chrome 的 com.google.Chrome 不同) — macOS 单例/Dock 转发按
    bundle ID 路由, 个人 Chrome 与项目浏览器从此互不干扰: 个人 Chrome
    关闭时项目浏览器照常可用, 也绝不会把用户的窗口/数据劫进项目实例。
    「个人 Chrome 在跑才允许项目浏览器」的守卫与 watchdog 反向清理
    整套机制删除 (守卫曾造成 609 次/天的建窗↔杀窗互搏)。
  - 三浏览器(9222 CDP 常驻 / 9333 瑞数常驻 / DrissionPage 窗口池)统一
    空闲阈值 BROWSER_IDLE_REAP(默认 1800s, 0=关闭回收)自动关闭 —
    1800s 避免渲染需求断续时「拉起→空闲被杀→再拉起」的冷启动循环
    (180s 时代一天 7~20 轮, 实测 2026-09-10);
  - 租用方连接失败自启(ensure_9222), 冷启动首请求慢 2-5s;
  - 活动时间戳: 各浏览器路径每次使用 touch_browser_activity(),
    watchdog(night_watchdog.sh) 据此空闲回收 9222/9333, 池由本进程惰性回收;
  - 采集连续运行时浏览器保持热态零影响; 停顿/停止后 30 分钟全部自动退出。

调用方:
  - util/cdp_util.py      (9222 tab 租用: ensure_9222 + touch)
  - util/drission_pool.py (DrissionPage 窗口池: IDLE_REAP + touch)
  - util/handleRuishu.py  (9333 瑞数常驻: IDLE_REAP + touch)
  - department teachers/script/night_watchdog.sh (bash 侧空闲回收,
    读同一 BROWSER_IDLE_REAP/BROWSER_ACTIVITY_FILE 环境变量与默认值)
"""
import glob
import os
import socket
import subprocess
import threading
import time

IDLE_REAP = float(os.environ.get("BROWSER_IDLE_REAP", "1800"))  # 秒, 0=不回收
_BROWSER_ACTIVITY = os.environ.get("BROWSER_ACTIVITY_FILE",
                                   "/tmp/browser_activity.ts")
_LAUNCH_LOCK = threading.Lock()


def _resolve_project_chrome() -> str:
    """独立 Chromium 可执行文件 (不与个人 Chrome 共享单例的关键)。

    候选顺序: 环境变量 PROJECT_CHROME_BIN → CloakBrowser Chromium
    (org.chromium.Chromium) → playwright Chrome for Testing
    (com.google.Chrome.forTesting) → 系统 Chrome(旧路径兜底, 会在 stderr
    提示有单例劫持风险)。全部不存在返回 ""。
    """
    cand = os.environ.get("PROJECT_CHROME_BIN", "").strip()
    if cand and os.path.exists(cand):
        return cand
    for pat in (
        os.path.expanduser("~/.cloakbrowser/chromium-*/Chromium.app/Contents/MacOS/Chromium"),
        os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/"
                           "Google Chrome for Testing.app/Contents/MacOS/"
                           "Google Chrome for Testing"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ):
        for p in sorted(glob.glob(pat)):
            if os.path.exists(p):
                if p.endswith("Google Chrome"):
                    print("[browser-lifecycle] 警告: 独立 Chromium 未找到, "
                          "回退系统 Chrome (有单例劫持风险, 建议安装 playwright "
                          "chromium 或设置 PROJECT_CHROME_BIN)",
                          file=__import__("sys").stderr, flush=True)
                return p
    return ""


PROJECT_CHROME = _resolve_project_chrome()

_9222_ARGS = [
    PROJECT_CHROME,
    "--remote-debugging-port=9222",
    "--remote-debugging-address=127.0.0.1",
    "--headless=new", "--disable-gpu", "--no-first-run",
    "--no-default-browser-check",
    "--user-data-dir=/tmp/chrome_cdp_9222",
    "--remote-allow-origins=*",
    "--disable-background-networking", "--disable-sync",
    "--disable-extensions", "--no-sandbox", "about:blank",
]


def touch_browser_activity():
    """任一浏览器路径使用时调用 — watchdog 据此空闲回收(3分钟无使用即关)。"""
    try:
        os.utime(_BROWSER_ACTIVITY, None)
    except FileNotFoundError:
        try:
            open(_BROWSER_ACTIVITY, "a").close()
        except Exception:
            pass
    except Exception:
        pass


def browser_idle_sec() -> float:
    """距上次浏览器使用秒数 (无记录视为 ∞)。"""
    try:
        return time.time() - os.path.getmtime(_BROWSER_ACTIVITY)
    except Exception:
        return float("inf")


def port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except Exception:
        return False


_PERSONAL_CC_TTL = 5.0          # 个人 Chrome 探测缓存 TTL(秒) — 仅保留供外部兼容
_personal_cc_cache = (0.0, True)


def personal_chrome_running() -> bool:
    """个人 Chrome 是否在运行 — 兼容存根 (2026-09-10 架构升级后无调用方)。

    独立 Chromium 架构下项目浏览器与个人 Chrome 互不干扰 (不同 bundle ID,
    不共享 macOS 单例), 「个人 Chrome 在跑才允许项目浏览器」的守卫整套
    删除; 函数保留为兼容存根 (永远 True = 永不拒绝)。
    """
    return True


def _clean_profile_volatile(profile_dir: str):
    """清理 profile 内的易损坏缓存目录 (2026-09-10): 机器 OOM 挤爆时 Chromium
    被强杀, GrShaderCache/Code Cache 等文件损坏 → 下次启动报 disk_cache
    错误后崩溃 (实测旧 /tmp/chrome_cdp_9222 profile 即如此, 启动即死)。
    只清缓存类目录, Cookies/Login Data 等状态文件保留。"""
    import shutil
    for sub in ("GrShaderCache", "ShaderCache", "GraphiteDawnCache",
                "Code Cache", "Crashpad", "cache"):
        p = os.path.join(profile_dir, sub)
        if os.path.isdir(p) and os.path.exists(p):
            try:
                shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass


def ensure_9222() -> bool:
    """9222 无监听时自启常驻实例 (按需启动: watchdog 不再保活, 租用方
    连接失败自启; 参数与 night_watchdog.sh 完全一致)。

    2026-09-10 架构升级: 改用独立 Chromium(PROJECT_CHROME)启动, 与个人
    Chrome 不共享单例 — 个人 Chrome 开不开都不影响项目浏览器, 反之亦然。
    启动前清 profile 易损缓存(防 OOM 后损坏 profile 启动即崩), 拉起失败
    则整目录重来一次再试。无可用二进制则拒绝拉起, 调用方降级。
    """
    if port_listening(9222):
        return True
    if not PROJECT_CHROME:
        print("[browser-lifecycle] 无可用独立 Chromium (PROJECT_CHROME 为空), "
              "拒绝拉起 9222, 渲染链降级", file=__import__("sys").stderr,
              flush=True)
        from browser_ops import browser_op
        browser_op("refuse", "9222 无独立Chromium二进制")
        return False
    with _LAUNCH_LOCK:
        if port_listening(9222):
            return True
        # 2026-09-16 跨进程启动锁(9333 互搏循环教训同源): _LAUNCH_LOCK 是
        # 进程内锁, 多 worker 子进程并发双 Popen 竞争同一 profile/端口 —
        # 失败方自退无害但双写 profile 是历史「profile 损坏启动即崩」根因
        # 之一; flock 保证只有一进程执行启动段
        import fcntl as _fcntl
        _lockf = open("/tmp/chrome9222.launch.lock", "w")
        _fcntl.flock(_lockf, _fcntl.LOCK_EX)
        try:
            if port_listening(9222):
                return True
            profile = "/tmp/chrome_cdp_9222"
            _clean_profile_volatile(profile)
            for attempt in (1, 2):
                try:
                    subprocess.Popen(_9222_ARGS,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    from browser_ops import browser_op
                    browser_op("launch", f"9222 CDP 常驻实例 (独立Chromium, 第{attempt}次)")
                except Exception:
                    return False
                for _ in range(60):  # 等待绑定 ≤30s
                    if port_listening(9222):
                        return True
                    time.sleep(0.5)
                if attempt == 1:
                    # 首拉失败: 全量清 profile 重试一次 (损坏 profile 启动即崩场景)
                    import subprocess as _sp
                    import shutil
                    _sp.run(["pkill", "-9", "-f", "user-data-dir=/tmp/chrome_cdp_9222"],
                            capture_output=True)
                    shutil.rmtree(profile, ignore_errors=True)
        finally:
            _fcntl.flock(_lockf, _fcntl.LOCK_UN)
            _lockf.close()
    return port_listening(9222)
