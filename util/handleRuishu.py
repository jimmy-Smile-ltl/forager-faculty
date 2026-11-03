#!/usr/bin/env python3
"""
RsSpider — 瑞数反爬自动绕过网络请求类

基于 sdenv 补环境框架,自动管理瑞数 VMP Cookie 的生成、缓存与失效重试。

瑞数 WAF 三层对抗:

  层级 1（应用层瑞数 — RsSpider 可过）:
    特征: HTTP 412/202, 或 200 但响应体 <500字符且含 document.cookie / $_ts.nsd
    代表学校: 四川大学(202), 成都理工大学(412), 青海大学, 安徽大学(412)
    应对: Node.js sdenv 生成合法 Cookie → curl_cffi 携带 Cookie 重发

  层级 2（网络层瑞数 — RsSpider 也无法过）:
    特征: TCP broken pipe / connection reset, curl_cffi 直接断连
    代表学校: 兰州大学, 西安交通大学, 华中农业大学
    应对: CDP 桥接本地 Chrome（--remote-debugging-port=9222），复用真实浏览器 IP/指纹

  层级 3（IP 黑名单）:
    特征: connection refused, 任何工具都连不上
    代表学校: 南京师范大学, 南京医科大学
    应对: 换网络/等待解封

用法:
    from handleRuishu import RsSpider

    spider = RsSpider()
    resp = spider.get("https://www.scu.edu.cn")
    print(resp.status_code, len(resp.text))

特性:
    - 首次请求自动通过 Node.js sdenv 生成 Cookie
    - Cookie 按域名保存到本地 JSON (cookie_dir/www.example.com.json)
    - 请求前先检查已有 Cookie,有效则复用
    - 请求失败时自动重新生成 Cookie 并重试
    - 支持 curl_cffi 的 impersonate 参数模拟浏览器指纹
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
import sys as _sys

# 2026-10-25 用户指令「及时切换方法」: sdenv 生成超时的 host 负缓存(30min)
# — 命中直接跳过 sdenv 层直降其他方法, 不再每页白等 65s(12次/批实锤)
_SDENV_TIMEOUT_HOSTS: dict = {}
_SDENV_NEG_TTL = 1800

# ── 自身目录入 sys.path (2026-09-02 修复): 兄弟模块 browser_budget /
# browser_lifecycle 顶层导入在仅把上级根放入 PYTHONPATH 的场景下无法解析
# → ModuleNotFoundError (同 smart_fetch 事故)。此处自举保证任何调用方可用。
_UTIL_DIR = str(Path(__file__).resolve().parent)
if _UTIL_DIR not in _sys.path:
    _sys.path.insert(0, _UTIL_DIR)

from curl_cffi import requests as cffi_requests

from browser_budget import BrowserBudgetExceeded, browser_budget

logger = logging.getLogger("RsSpider")

# ── 常驻 Chrome (2026-08-24 池化) ──
# 9333 瑞数浏览器模块级单例: 首次 _launch_chrome_cdp 启动后缓存,
# 后续请求复用; 进程退出由 atexit 兜底关闭 (不再随请求 quit/杀端口)。
_CDP_RESIDENT = {"proc": None, "ws_url": None, "last_used": 0.0}

# 2026-08-25: 并发首启竞态防护 — 多 worker (process_batch workers=N) 同时
# 首调 _launch_chrome_cdp 时, 各自 pkill 会杀掉对方刚启动的 Chrome
# (实测 3 worker 并发 → 3 路「ws 连接失败」全灭)。启动全程持锁,
# 后到线程等锁 → 复用检查命中缓存, 不再重复 pkill/Popen。
_CDP_START_LOCK = threading.Lock()

# 2026-08-25: 连接级失败域记忆 — 瑞数 WAF 第 3 层 (IP 黑名单/TLS 指纹绑定,
# 如南师大 xinchuan: 80 拒绝 / 443 broken pipe / DrissionPage cookie 在 curl
# 层必 412) 对该域所有裸请求必然失败, 首个请求伪 412 后记入, 后续请求直通
# 浏览器链 (复用常驻 Chrome), 不再空跑 curl 缓存裸请求。
_CONNECTION_BLOCKED_HOSTS: set[str] = set()


def _shutdown_resident_chrome():
    """atexit: 2026-09-16 根治互搏循环(用户指令) — 多 worker 共享常驻,
    谁退谁杀 → 拉杀循环(实测 8-18 次/分)。常驻生命周期交 watchdog 空闲
    回收; 进程退出只清内存缓存。开发期无 watchdog 时仍杀(防残留)。"""
    from browser_ops import browser_op
    browser_op("destroy", "9333 resident chrome quit(缓存清理, 实例交watchdog)")
    _has_watchdog = False
    try:
        import subprocess as _sp
        _has_watchdog = _sp.run(["pgrep", "-f", "night_watchdog.sh"],
                                capture_output=True).returncode == 0
    except Exception:
        pass
    proc = _CDP_RESIDENT.get("proc")
    if proc and not _has_watchdog:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
    _CDP_RESIDENT["proc"], _CDP_RESIDENT["ws_url"] = None, None
    _CDP_RESIDENT["last_used"] = 0.0


def _touch_browser_activity():
    """浏览器活动时间戳 (2026-09-02): watchdog 据此空闲回收 9222/9333。"""
    try:
        from browser_lifecycle import touch_browser_activity
        touch_browser_activity()
    except Exception:
        pass


def _port_owner_is_google_chrome(port: int) -> bool:
    """9333 监听实例是否「非法」——个人 Chrome 或 DrissionPage 模板自启。

    2026-10-08 用户实锤: 个人 Chrome 打不开 — 根因是 9333 被 DrissionPage
    自启泄漏实例(userData/9333 + /Applications/Google Chrome.app)占用,
    与个人 Chrome 同 bundle ID, macOS 单例把用户窗口请求劫进 headless
    实例。所有复用 9333 的路径必须先验二进制: 非独立 Chromium 一律
    杀掉重拉。独立 Chromium 的 command 含 "Chromium.app"、playwright
    含 "Google Chrome for Testing.app", 均不匹配 "Google Chrome.app"。

    2026-10-16 扩展: DrissionPage 自启泄漏即便用了独立 Chromium(10-08
    修复生效), 模板实例仍无 --remote-allow-origins — Chrome 145 严格
    Origin 校验拒绝 DrissionPage ws 握手, 瑞数 cookie 链全线失败
    (成都理工 412 求解失败的根因)。合法常驻 = handleRuishu 拉起,
    必带 --remote-allow-origins + 固定 .chrome_profile; 模板自启实例
    (userData/9333 临时 profile) 无 allow-origins — 同样判非法杀掉。
    """
    try:
        import subprocess as _sp
        _r = _sp.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                     capture_output=True, text=True, timeout=8)
        pids = [p for p in _r.stdout.split() if p.strip().isdigit()]
    except Exception:
        return False
    for pid in pids[:4]:
        try:
            _c = _sp.run(["ps", "-p", pid, "-o", "command="],
                         capture_output=True, text=True, timeout=8)
            if "Google Chrome.app" in _c.stdout:
                return True  # 个人 Chrome: 单例劫持
            if "--remote-allow-origins" not in _c.stdout:
                return True  # DrissionPage 模板自启/坏实例: ws 必被拒
        except Exception:
            continue
    return False


def _kill_9333_google_chrome():
    """清掉占用 9333 的 Google Chrome 实例并等端口释放(最多 ~10s)。"""
    import subprocess as _sp
    import time as _t
    _sp.run(["pkill", "-9", "-f", "remote-debugging-port=9333"],
            capture_output=True, timeout=5)
    import socket as _sock
    for _ in range(20):
        busy = False
        for host in ("127.0.0.1", "::1"):
            try:
                with _sock.create_connection((host, 9333), timeout=0.3):
                    busy = True
            except OSError:
                pass
        if not busy:
            return True
        _t.sleep(0.5)
    return False


import atexit as _atexit
_atexit.register(_shutdown_resident_chrome)

# ── 默认配置 ────────────────────────────────────────
DEFAULT_COOKIE_DIR = Path(__file__).resolve().parent / "cookies"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
GENERATOR_SCRIPT = Path(__file__).resolve().parent / "generate_cookie.js"
COOKIE_MAX_AGE = 3600  # 缓存 cookie 最大有效期 (秒), 过期自动重新生成


class RsSpider:
    """瑞数自动绕过 HTTP 客户端。

    封装 curl_cffi + sdenv 补环境 Cookie 生成, 请求自动携带有效瑞数 Cookie。

    Parameters
    ----------
    cookie_dir: 本地 Cookie 缓存目录, 按域名存 JSON
    max_age: Cookie 缓存最大有效期 (秒), 默认 3600
    impersonate: curl_cffi 指纹模拟, 默认 "chrome131"
    timeout: 请求超时 (秒)
    """

    def __init__(
        self,
        cookie_dir: str | Path = DEFAULT_COOKIE_DIR,
        max_age: int = COOKIE_MAX_AGE,
        impersonate: str = "chrome131",
        timeout: int = 30,
        proxies: Optional[dict] = None,
    ):
        self.cookie_dir = Path(cookie_dir)
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        self.max_age = max_age
        self.impersonate = impersonate
        self.timeout = timeout
        self.proxies = proxies  # {"http": ..., "https": ...} 或 None
        self._ua = DEFAULT_USER_AGENT

        # 校验 Node.js 脚本存在
        if not GENERATOR_SCRIPT.exists():
            logger.warning(f"生成脚本不存在: {GENERATOR_SCRIPT}, Cookie 自动生成将不可用")

    # ── Cookie 文件管理 ──────────────────────────────

    def _domain_key(self, url: str) -> str:
        """从 URL 提取域名字符串, 兼容特殊字符做文件名"""
        parsed = urlparse(url)
        host = parsed.hostname or url
        # 如果是 IP 地址直接返回, 否则用后两级域名
        parts = host.split(".")
        if len(parts) >= 2:
            # 尝试取后两级: example.com / edu.cn
            if parts[-2] in ("edu", "gov", "ac", "org", "com", "net"):
                host = ".".join(parts[-3:]) if len(parts) >= 3 else host
            else:
                host = ".".join(parts[-2:])
        # 哈希降重 (同域名不同子域可能共享 cookie)
        return host

    def _cookie_file(self, url: str) -> Path:
        return self.cookie_dir / f"{self._domain_key(url)}.json"

    def _load_cookies(self, url: str) -> Optional[dict]:
        """加载已缓存的 Cookie, 过期返回 None"""
        f = self._cookie_file(url)
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text("utf-8"))
            age = time.time() - data.get("created_at", 0)
            if age > self.max_age:
                logger.debug(f"Cookie 已过期 (age={age:.0f}s): {f.name}")
                return None
            if not data.get("cookies"):
                return None
            # 兼容 Node 输出的 camelCase 和 Python 的 snake_case
            if not data.get("cookie_string"):
                data["cookie_string"] = data.get("cookieString", "")
            logger.info(f"✅ 加载缓存 Cookie: {f.name} ({len(data['cookies'])} 个, age={age:.0f}s)")
            return data
        except (json.JSONDecodeError, KeyError):
            return None

    def _save_cookies(self, url: str, cookies: list[dict], cookie_string: str,
                      method: str = "unknown") -> None:
        """保存 Cookie 到本地 JSON (记录成功方案, 下次优先)。

        原子写: 写唯一临时文件再 os.replace。同校多院系并发进程共享
        同一注册域缓存文件（_domain_key 只取后两段），直接 write_text
        会导致并发写坏缓存（torn write）。os.replace 在 POSIX 下原子，
        读者只会看到完整旧版或完整新版。
        临时名用 mkstemp（内核保证唯一）: 同进程并发线程共用 pid，
        <pid>.tmp 会互撞（教师级线程池引入后此路径被并发调用）。
        """
        import tempfile

        f = self._cookie_file(url)
        data = {
            "url": url,
            "cookies": cookies,
            "cookie_string": cookie_string,
            "created_at": time.time(),
            "count": len(cookies),
            "method": method,  # sdenv / DrissionPage / playwright / no-ruishu
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=str(f.parent), prefix=f"{f.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_name, f)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        logger.info(f"💾 保存 Cookie → {f.name} ({len(cookies)} 个, {method})")

    def _last_method(self, url: str) -> str:
        """上次成功生成该域名 Cookie 的方案"""
        f = self._cookie_file(url)
        if not f.exists():
            return ""
        try:
            data = json.loads(f.read_text("utf-8"))
            return data.get("method", "")
        except Exception:
            return ""

    def _forget_last_method(self, url: str) -> None:
        """清除该域名的方案记忆（2026-08-25, Chrome 151 兼容配套）。

        方案记忆失效但端口已死时（如 CDP 9222 常驻被 watchdog 之外的原因
        杀掉后, last=playwright 永远跳过可用的 DrissionPage 9333）, 失败
        路径调用本方法删除 method 字段, 下次请求自动重走
        sdenv → DrissionPage → playwright 完整链, 收敛到可用方案。
        """
        import tempfile
        f = self._cookie_file(url)
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text("utf-8"))
            if "method" not in data:
                return
            data.pop("method")
            data["method_cleared_at"] = time.time()
            fd, tmp_name = tempfile.mkstemp(
                dir=str(f.parent), prefix=f"{f.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                os.replace(tmp_name, f)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            logger.info(f"🧹 清除方案记忆 → {f.name}")
        except Exception:
            pass

    def _is_cdp_connection_error(self, e: Exception) -> bool:
        """是否为 CDP 常驻浏览器连接类失败 (9222 已死 → 方案记忆失效)。

        仅判定连接/进程类错误: 9222 无监听 (ECONNREFUSED)、常驻浏览器被
        杀 (Target closed / connection reset)、ws 端点失败。页面 404/超时
        等业务失败不清记忆 (站点可能只是暂时不可达, 下次仍可复用该方案)。
        """
        msg = f"{type(e).__name__}: {e}".lower()
        return any(k in msg for k in (
            "econnrefused", "connection refused", "cannot connect",
            "target closed", "browser has been closed",
            "connection reset", "websocket",
        ))

    # ── Cookie 生成 (调用 Node.js) ───────────────────

    def _launch_chrome_cdp(self, port: int = 9333):
        """启动/复用本机 Chrome 调试端口, 返回 (proc, ws_url)

        2026-08-24 池化改造: 模块级单例常驻 — 首次启动后缓存 (proc, ws_url),
        后续请求直接复用, 不再每次"起 Chrome → 用 → 杀进程"。ws 失活
        (端口无监听 / Chrome 退出) 时自动重建。持久 profile 复用瑞数信任状态,
        cookie 缓存跨请求命中。进程退出由模块级 atexit 关闭。

        2026-08-25 并发防护: 启动段持 _CDP_START_LOCK (双检锁) — 多 worker
        (process_batch workers=N) 同时首调时, 后到线程等锁后直接命中缓存,
        不再重复 pkill/Popen 互相杀 Chrome。
        """
        import urllib.request
        import time as _time

        # 复用检查: 端口仍监听且身份一致则直接返回缓存 (不再 Popen/杀端口)
        if _CDP_RESIDENT["ws_url"]:
            if self._resident_alive(port):
                _touch_browser_activity()
                return _CDP_RESIDENT["proc"], _CDP_RESIDENT["ws_url"]
            _CDP_RESIDENT["proc"], _CDP_RESIDENT["ws_url"] = None, None  # 失活, 重建

        # 启动段互斥 (双检锁: 前线程已启动完成 → 直接复用)
        with _CDP_START_LOCK:
            if _CDP_RESIDENT["ws_url"]:
                if self._resident_alive(port):
                    _touch_browser_activity()
                    return _CDP_RESIDENT["proc"], _CDP_RESIDENT["ws_url"]
                _CDP_RESIDENT["proc"], _CDP_RESIDENT["ws_url"] = None, None
            return self._launch_chrome_cdp_locked(port)

    def _resident_alive(self, port: int = 9333) -> bool:
        """常驻 Chrome 是否仍健康: 端口在听 + /json/version 的 ws 身份与
        缓存一致 (不一致 = 常驻被顶替/DrissionPage 自启实例占位 → 需重建,
        否则 ChromiumPage 会连到无 allow-origins 的实例被 Chrome 151 拒绝)。
        """
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=2) as _r:
                live_ws = json.loads(_r.read())["webSocketDebuggerUrl"]
            return live_ws == _CDP_RESIDENT["ws_url"]
        except Exception:
            return False

    def _launch_chrome_cdp_locked(self, port: int = 9333):
        """(已持 _CDP_START_LOCK) 启动段主体: pkill 清理 → Popen → 等 CDP 就绪。
        2026-09-16 根治互搏循环(用户指令): 多 worker 并发各自「检查→pkill→
        拉起」互相杀, 9333 每分 8-18 次 launch 循环 — 加跨进程 flock 启动锁:
        持锁者唯一执行启动; 后到者锁内看到活实例直接复用, 不再 pkill。"""
        import urllib.request
        import time as _time
        import subprocess
        import fcntl as _fcntl

        _lockf = open("/tmp/rs9333.launch.lock", "w")
        _fcntl.flock(_lockf, _fcntl.LOCK_EX)
        def _unlock():
            try:
                _fcntl.flock(_lockf, _fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                _lockf.close()
            except Exception:
                pass
        # 锁内复用检查: 并发窗口期其他 worker 已拉起 → 直接复用其 ws
        # (跨进程 ws_url 身份不缓存在本进程, 现读 /json/version 取活值)
        # 2026-10-08 身份校验: 9333 若被 DrissionPage 自启泄漏的 Google
        # Chrome(与个人 Chrome 同 bundle ID) 占用, 复用会单例劫持个人
        # Chrome — 校验监听进程二进制, 非独立 Chromium 杀之重拉
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=2) as _r:
                _live_ws = json.loads(_r.read())["webSocketDebuggerUrl"]
            if _live_ws:
                if _port_owner_is_google_chrome(port):
                    logger.warning("   9333 被 Google Chrome 占用(自启泄漏), "
                                   "杀之重拉独立 Chromium")
                    _kill_9333_google_chrome()
                else:
                    _CDP_RESIDENT["ws_url"] = _live_ws
                    _CDP_RESIDENT["last_used"] = _time.time()
                    _touch_browser_activity()
                    _unlock()
                    return None, _live_ws
        except Exception:
            pass

        # 2026-09-10 架构升级: 独立 Chromium(与个人 Chrome 不同 bundle ID,
        # 不共享 macOS 单例) — 个人 Chrome 开不开都可拉起 9333
        try:
            from util.browser_lifecycle import PROJECT_CHROME
            chrome_path = PROJECT_CHROME
        except Exception:
            chrome_path = ""
        if not chrome_path or not os.path.exists(chrome_path):
            _unlock()
            return None, None

        # 持久 profile (固定路径) → 二次访问直接命中缓存 cookie
        profile_dir = os.path.join(self.cookie_dir.parent, ".chrome_profile")
        os.makedirs(profile_dir, exist_ok=True)

        # 清除 profile 锁 (上次 Chrome 崩溃遗留)
        for lock in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
            lock_path = os.path.join(profile_dir, lock)
            try:
                if os.path.islink(lock_path):
                    os.unlink(lock_path)
                elif os.path.exists(lock_path):
                    os.remove(lock_path)
            except OSError:
                pass

        # 2026-08-25 防自启(第1层): 清掉占着 9333 的遗留实例 — 上批 DrissionPage
        # 自启的 userData/9333 非 headless 窗口若还占着端口, 新的 headless 常驻
        # bind 失败直接退出, 瑞数整批降级。pkill 只匹配调试端口参数, 不会碰到
        # 用户真实 Chrome (无 --remote-debugging-port)。
        # 2026-08-25 加强: -9 强杀 (SIGTERM 会被 Chrome 拖住) + 轮询等端口
        # 释放, 消除「旧实例半死 → 新 Chrome bind 失败 → /json/version 读到
        # 残留实例 → ChromiumPage 连错(无 allow-origins)」竞态。
        import socket as _rs_socket2
        if _CDP_RESIDENT["ws_url"] is None:
            # 2026-09-16 三杀循环根治: 冷启动实测 ~25s 才 CDP 就绪, 旧 15s
            # 等待超时 → rmtree → 并发 worker pkill 杀年轻实例 → 永不就绪。
            # pkill 只在端口确无监听时执行(启动中实例不杀)。
            _port_alive = True
            try:
                with _rs_socket2.create_connection(("127.0.0.1", port), timeout=0.3):
                    pass
            except OSError:
                _port_alive = False
            if not _port_alive:
                # 2026-08-25 修复: 模式不带前导 "--" — BSD pkill 会把
                # "--remote-debugging-port=9333" 当成选项解析, 匹配不到任何进程
                subprocess.run(["pkill", "-9", "-f", "remote-debugging-port=9333"],
                               capture_output=True, timeout=5)
            # 双栈轮询: 残留实例可能绑 IPv4 或 IPv6 (macOS Chrome 常 IPv4/IPv6
            # 分绑), 两者都无监听才认为端口释放
            for _ in range(20):
                v4_busy = True
                try:
                    with _rs_socket2.create_connection(("127.0.0.1", port), timeout=0.3):
                        pass
                except OSError:
                    v4_busy = False
                v6_busy = True
                try:
                    with _rs_socket2.create_connection(("::1", port), timeout=0.3):
                        pass
                except OSError:
                    v6_busy = False
                if not v4_busy and not v6_busy:
                    break
                _time.sleep(0.5)

        # 2026-09-10 架构升级: 原「个人 Chrome 预检守卫(未运行拒绝拉起)」
        # 删除 — 独立 Chromium 与个人 Chrome 不共享单例, 无需守卫
        from browser_ops import browser_op
        browser_op("launch", f"9333 Ruishu resident (port={port})")
        proc = subprocess.Popen(
            [chrome_path, "--headless=new",
             f"--remote-debugging-port={port}",
             # 2026-08-25 Chrome 151: 严格 Origin 校验 — 拒绝无 Origin 或
             # 非白名单 Origin 的 CDP ws 连接 (DrissionPage 4.1.1.4
             # suppress_origin=True 不发 Origin → BrowserConnectError)。
             # 本机调试端口, 允许任意 Origin 与 Playwright 等工具一致。
             "--remote-allow-origins=*",
             f"--user-data-dir={profile_dir}",
             "--no-sandbox", "--disable-blink-features=AutomationControlled",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # 等 CDP 就绪 (最多 90s, 2026-09-16 冷启动实测 25s 原 15s 不够);
        # IPv4 优先, 失败试 IPv6 (Chrome 可能只绑其一)
        ws_url = None
        for _ in range(180):
            _time.sleep(0.5)
            # Chrome 可能因 profile 锁/端口占用直接退出
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                    import json as _json
                    info = _json.loads(r.read())
                    ws_url = info["webSocketDebuggerUrl"]
                    break
            except Exception:
                try:
                    with urllib.request.urlopen(f"http://[::1]:{port}/json/version", timeout=2) as r:
                        import json as _json
                        info = _json.loads(r.read())
                        ws_url = info["webSocketDebuggerUrl"]
                        break
                except Exception:
                    continue
        if not ws_url:
            proc.terminate()
            # 2026-09-11 损坏 profile 启动即崩修复(机器强制重启遗留): 独立
            # Chromium 打头即死(SIGTRAP, DevTools 刚打印就退出) — 实测旧
            # .chrome_profile 启动必崩、全新 profile 存活。清 volatile 缓存
            # + 全量重建 profile 重试一次(与 ensure_9222 同款韧性), 否则
            # sdenv 求解链全断(Ruishu 学校大面积「师资页不可达」的直接原因)。
            try:
                from browser_lifecycle import _clean_profile_volatile
                _clean_profile_volatile(profile_dir)
            except Exception:
                pass
            try:
                import shutil as _sh
                _sh.rmtree(profile_dir, ignore_errors=True)
                os.makedirs(profile_dir, exist_ok=True)
            except Exception:
                pass
            browser_op("recover", f"9333 profile 重建重试 (port={port})")
            proc2 = subprocess.Popen(
                [chrome_path, "--headless=new",
                 f"--remote-debugging-port={port}",
                 "--remote-allow-origins=*",
                 f"--user-data-dir={profile_dir}",
                 "--no-sandbox", "--disable-blink-features=AutomationControlled",
                 "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(180):
                _time.sleep(0.5)
                if proc2.poll() is not None:
                    break
                try:
                    import json as _j2
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                        ws_url = _j2.loads(r.read())["webSocketDebuggerUrl"]
                        break
                except Exception:
                    continue
            proc = proc2
            if not ws_url:
                try:
                    proc.terminate()
                except Exception:
                    pass
                _unlock()
                return None, None
        # 2026-08-25 Chrome 151 headless=new: 启动时不创建 about:blank tab,
        # /json targets 为空 → DrissionPage connect_browser 的 test_connect
        # 找不到 page 类型 target → BrowserConnectError「浏览器连接失败」。
        # 主动 PUT /json/new 创建 tab, 确保 test_connect 通过。
        try:
            _req = urllib.request.Request(
                f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
            with urllib.request.urlopen(_req, timeout=3) as _r:
                _tab = json.loads(_r.read())
                logger.info(f"   CDP 就绪: 创建 tab ({_tab.get('type')})")
        except Exception as e:
            logger.warning(f"   创建 tab 失败(继续尝试连接): {e}")
        # 常驻缓存 (池化): 后续请求复用, 进程退出由 atexit 关闭
        _CDP_RESIDENT["proc"], _CDP_RESIDENT["ws_url"] = proc, ws_url
        _unlock()
        return proc, ws_url

    def _generate_cookies_drission(self, url: str, wait_sec: int = 15) -> Optional[dict]:
        """DrissionPage + 本机 Chrome 生成 Cookie (最强兜底, 指纹=真实 Chrome 151)

        ws 404 竞态 → 重试 3 次; 持久 profile 使 cookie 跨进程复用。
        并发预算: 自启 Chrome 也占浏览器槽位, 满则跳过 (降级下一方案)。
        """
        try:
            from DrissionPage import ChromiumPage
        except ImportError:
            return None

        logger.info(f"🦊 DrissionPage 生成 Cookie: {url}")
        try:
            with browser_budget():
                return self._generate_cookies_drission_locked(url, wait_sec)
        except BrowserBudgetExceeded:
            logger.warning("   ⛔ 浏览器并发预算满, 跳过 DrissionPage Cookie 生成")
            return None
        except Exception as e:
            logger.warning(f"   DrissionPage 失败: {e}")
            return None

    def _generate_cookies_drission_locked(self, url: str, wait_sec: int = 15) -> Optional[dict]:
        """(预算已占用) DrissionPage + 本机 Chrome 生成 Cookie 主体

        2026-08-24 池化: 浏览器常驻 (模块级单例), 用完只关 tab (close),
        不 quit 不杀端口; 常驻 Chrome 由 atexit 在进程退出时关闭。
        """
        try:
            from DrissionPage import ChromiumPage
        except ImportError:
            return None
        try:
            _, ws_url = self._launch_chrome_cdp()
            if not ws_url:
                logger.warning("   Chrome CDP 启动失败")
                return None

            # ws 404 竞态重试
            # 2026-08-25 防自启 (用户报 5 窗口): ChromiumPage(addr_or_opts)
            # 传入连接失败的地址时, DrissionPage 会自动启动默认模板浏览器
            # (userData/9333, 非 headless) → 每次重试都可能自启一个窗口。
            # 连接前 socket 验证端口仍监听, 失效直接放弃, 不让 ChromiumPage
            # 拿到可触发自启的输入。
            import socket as _rs_socket

            # 2026-10-28 渲染窗口冻结(tiers=off): 9333 构建级握手404 未解期间
            # 直接放弃浏览器路径 — 否则浏览器依赖户每户 3 连败+半死空烧 30min
            # (8:18 曾撤销致 1910 批 9333 问题 392 次复现)。修好 9333 后删本段
            import os as _rs_os
            if _rs_os.environ.get("BROWSER_TIERS", "").strip().lower() in ("off", "0"):
                logger.warning("   BROWSER_TIERS=off 渲染窗口冻结, 跳过 9333 浏览器路径")
                return None

            page = None
            for attempt in range(3):
                try:
                    with _rs_socket.create_connection(("127.0.0.1", 9333), timeout=2):
                        # 2026-08-25 防自启(第2层): 端口在听 ≠ 常驻还活着 —
                        # ChromiumPage 连接失败会自启默认模板浏览器 (userData/9333,
                        # 非 headless 窗口)。连接前核对 /json/version 的 ws 身份,
                        # 与缓存的常驻 ws_url 不一致说明常驻已死, 直接放弃,
                        # 不让 ChromiumPage 拿到可触发自启的输入。
                        import urllib.request as _rs_urllib
                        with _rs_urllib.urlopen(
                                f"http://127.0.0.1:9333/json/version", timeout=2) as _r:
                            live_ws = json.loads(_r.read())["webSocketDebuggerUrl"]
                        if live_ws != ws_url:
                            # 2026-10-27 漂移二分(凌晨实锤: 9333 重启换 UUID 后
                            # 批进程缓存过期 → 每户 3 连败+「疑似半死」39 次空烧):
                            # 非 Google Chrome = 项目 Chromium 重启换 UUID —
                            # 更新缓存继续用; Google Chrome = 自启泄漏, 清理放弃
                            if _port_owner_is_google_chrome(9333):
                                logger.warning("   漂移实例为 Google Chrome, 清理")
                                _kill_9333_google_chrome()
                                return None
                            logger.warning("   ws 身份漂移(常驻重启换UUID), 更新缓存继续")
                            _CDP_RESIDENT["ws_url"] = live_ws
                            ws_url = live_ws
                        # 2026-08-25 Chrome 151 兼容: 不能传 ws_url — DrissionPage
                        # 4.1.1.4 用 Target.getTargets 的 targetInfos[0] 取浏览器 id,
                        # Chrome 151 该元素是 service_worker/browser_ui(或空),
                        # browserContextId ≠ 浏览器 uuid → 拼出 /devtools/browser/xxx
                        # 端点 404 连接失败。改传 "地址:端口" 走 connect_browser 的
                        # HTTP /json/version 路径(真实 uuid), 端口在听时不会自启。
                        # 2026-08-25 再修: 必须显式 headless(True) — ChromiumOptions
                        # 默认 is_headless=False, 而 run_browser 检测到常驻是 headless
                        # → ChromiumPage.__init__ 86 行 quit()+connect_browser 重启 →
                        # DrissionPage 自启模板浏览器(userData/9333 非 headless 窗口,
                        # 无 allow-origins) → ws 被 Chrome 151 拒 → 404 + 窗口残留
                        # (用户反馈「频繁创建浏览器窗口」的直接来源)。
                        from DrissionPage._configs.chromium_options import (
                            ChromiumOptions as _RsChrOpt)
                        _opt = _RsChrOpt()
                        _opt.set_address("127.0.0.1:9333")
                        _opt.headless(True)
                        # 2026-09-16 红线兜底: 连接失败触发 DrissionPage 自启时,
                        # 默认模板浏览器是系统 Chrome(个人 Chrome 二进制) — 显式
                        # 绑 PROJECT_CHROME, 自启也只可能是项目独立 Chromium
                        try:
                            from util.browser_lifecycle import PROJECT_CHROME as _RS_PC
                            if _RS_PC:
                                _opt.set_browser_path(_RS_PC)
                        except Exception:
                            pass
                        page = ChromiumPage(addr_or_opts=_opt)
                        # 连接后复核: 竞态窗口内常驻若死, ChromiumPage 会自启 —
                        # 检测到漂移则 quit 掉自启实例, 不留窗口
                        with _rs_urllib.urlopen(
                                f"http://127.0.0.1:9333/json/version", timeout=2) as _r:
                            final_ws = json.loads(_r.read())["webSocketDebuggerUrl"]
                        if final_ws != ws_url:
                            logger.warning("   ws 连接后漂移(自启实例), 关闭防窗口残留")
                            page.quit()
                            return None
                        break
                except Exception as e:
                    logger.warning(f"   ws 连接失败(第{attempt+1}次): {e}")
                    import time as _t
                    _t.sleep(2)
            if page is None:
                # 2026-10-19 半死自愈(今天2次实锤): 3次ws连接全败=常驻半死
                # (进程活/端点死, _resident_alive 的 urlopen 仍通会误判复用,
                # 半死后一直躺到人工发现) — 失效 ws 缓存, 下次瑞数链
                # _launch_chrome_cdp 锁内 _resident_alive=False 自动重建
                logger.warning("   9333 疑似半死(3次连接全败), 失效缓存待重建")
                _CDP_RESIDENT["ws_url"] = ""
                return None

            page.get(url, timeout=30)
            # 等瑞数 VMP 执行 + 自动跳转
            for _ in range(wait_sec // 3 + 1):
                page.wait(3)
                if len(page.html) > 5000:
                    break

            html = page.html
            cookies = page.cookies()
            page.close()  # 只关 tab: 常驻浏览器保留 (池化)

            if not cookies:
                return None
            cs = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            cookie_list = [{"name": c["name"], "value": c["value"]} for c in cookies]
            has_rs = any(c["value"] and len(c["value"]) > 60 for c in cookie_list)
            logger.info(f"✅ DrissionPage: {len(cookie_list)} cookies, 页面{len(html)}B, 瑞数token={'有' if has_rs else '无'}")
            return {"cookies": cookie_list, "cookieString": cs, "cookie_string": cs,
                    "success": has_rs, "error": None, "html": html}
        except Exception as e:
            logger.warning(f"   DrissionPage 失败: {e}")
            return None

    def _generate_cookies_playwright(self, url: str, wait_sec: int = 15) -> Optional[dict]:
        """Playwright 真实浏览器生成 Cookie (sdenv 失败时的兜底)

        并发预算: playwright launch 也占浏览器槽位, 满则跳过。
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        logger.info(f"🌐 Playwright 生成 Cookie: {url}")
        try:
            with browser_budget():
                return self._generate_cookies_playwright_locked(url, wait_sec)
        except BrowserBudgetExceeded:
            logger.warning("   ⛔ 浏览器并发预算满, 跳过 Playwright Cookie 生成")
            return None
        except Exception as e:
            logger.warning(f"   Playwright 失败: {e}")
            if self._is_cdp_connection_error(e):
                self._forget_last_method(url)
            return None

    def _generate_cookies_playwright_locked(self, url: str, wait_sec: int = 15) -> Optional[dict]:
        """(预算已占用) Playwright 真实浏览器生成 Cookie 主体

        2026-08-24 池化: 不再 chromium.launch 新建浏览器 — 连接常驻 9222
        (cdp_util.cdp_page), 只租用 tab, 用完自动 page.close()。
        """
        try:
            from cdp_util import cdp_page
        except ImportError:
            return None
        try:
            with cdp_page() as (page, ctx):
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # 等待瑞数 VMP 执行
                page.wait_for_timeout(wait_sec * 1000)
                cookies = ctx.cookies()

            if not cookies:
                return None
            cs = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            cookie_list = [{"name": c["name"], "value": c["value"]} for c in cookies]
            # 判定是否含瑞数 token (值>60字符)
            has_rs = any(c["value"] and len(c["value"]) > 60 for c in cookie_list)
            logger.info(f"✅ Playwright(CDP): {len(cookie_list)} cookies, 瑞数token={'有' if has_rs else '无'}")
            return {"cookies": cookie_list, "cookieString": cs, "cookie_string": cs,
                    "success": has_rs, "error": None}
        except Exception as e:
            logger.warning(f"   Playwright 失败: {e}")
            if self._is_cdp_connection_error(e):
                self._forget_last_method(url)
            return None

    def _generate_cookies(self, url: str, timeout: int = 45) -> Optional[dict]:
        """调用 Node.js sdenv 脚本生成瑞数 Cookie, 返回 {cookies, cookie_string} 或 None"""
        if not GENERATOR_SCRIPT.exists():
            logger.error("生成脚本不存在, 无法生成 Cookie")
            return None

        output_file = self._cookie_file(url).with_suffix(".tmp.json")

        # 2026-10-27 node 路径兜底(凌晨实锤: 批进程 PATH 缺 /usr/local/bin →
        # FileNotFoundError「Node.js 未安装」, sdenv 纯CPU链全灭, 瑞数户裸请求
        # 3轮空烧30min): shutil.which 之外再试常见绝对路径
        import shutil as _sh
        _node = _sh.which("node")
        for _cand in (_node, "/usr/local/bin/node", "/opt/homebrew/bin/node", "/usr/bin/node"):
            if _cand and os.path.exists(_cand):
                _node = _cand
                break
        if not _node:
            logger.error("Node.js 未安装")
            return None

        cmd = [
            _node, str(GENERATOR_SCRIPT),
            "--url", url,
            "--wait", "10",
            "--output", str(output_file),
        ]

        logger.info(f"🔐 生成 Cookie: {url}")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout + 20, cwd=str(GENERATOR_SCRIPT.parent),
            )

            if output_file.exists():
                result = json.loads(output_file.read_text("utf-8"))
                output_file.unlink(missing_ok=True)
                if result.get("success") and result.get("cookies"):
                    if not result.get("cookie_string"):
                        result["cookie_string"] = result.get("cookieString", "")
                    logger.info(f"✅ 生成成功: {len(result['cookies'])} 个 Cookie")
                    return result

        except subprocess.TimeoutExpired:
            logger.warning("   生成超时")
            # 2026-10-25 负缓存: 超时 host 30min 内跳过 sdenv 层(及时切换方法)
            try:
                _h = urlparse(url).netloc
                if _h:
                    _SDENV_TIMEOUT_HOSTS[_h] = time.time()
            except Exception:
                pass
        except json.JSONDecodeError:
            logger.warning("   JSON 解析失败")
        except FileNotFoundError:
            logger.error("   Node.js 未安装")
        except Exception as e:
            logger.warning(f"   生成异常: {e}")

        return None

    def _get_cookies(self, url: str, skip_cache: bool = False) -> Optional[dict]:
        """获取 Cookie: 缓存 → 上次成功方案 → sdenv → DrissionPage → Playwright

        skip_cache: 连接级失败域直通 (request 层已跳过缓存裸请求, 这里再加载
        缓存会拿到无 html 的旧 dict, direct_browser 分支就无法用页面兜底)。
        """
        # 1. 尝试加载缓存
        if not skip_cache:
            cached = self._load_cookies(url)
            if cached:
                return cached

        # 1.5 记住上次成功方案, 失效后优先重试该方案 (避免每次都降级到底)
        last = self._last_method(url)

        # 2. sdenv (或上次 sdenv 成功) — 生成超时负缓存命中直接跳过(2026-10-25)
        if not last or last in ("sdenv", "unknown", ""):
            _h2 = urlparse(url).netloc
            _neg_t = _SDENV_TIMEOUT_HOSTS.get(_h2, 0) if _h2 else 0
            if _h2 and time.time() - _neg_t < _SDENV_NEG_TTL:
                logger.info("   sdenv 生成超时负缓存(30min内), 跳过 sdenv 层直降其他方法")
            else:
                result = self._generate_cookies(url)
                if result and result.get("cookies") and self._has_rs_token(result["cookies"]):
                    cs = result.get("cookieString") or result.get("cookie_string") or ""
                    self._save_cookies(url, result["cookies"], cs, "sdenv")
                    return result

        # 2026-10-25 tiers=off 只跳过重型浏览器层(Playwright 每用起实例)。
        # DrissionPage 走 9333 常驻单实例(~400MB, 2026-09-11 定案: sdenv 单实例
        # 9333 在 tiers 开关之前, 内存可控且是强瑞数校(兰大)唯一兜底) — 不门控;
        # 兰大 8 户 412 深冻实锤: 门控 DrissionPage = 强瑞数校断兜底。
        _browsers_off = (os.environ.get("BROWSER_TIERS", "off").strip().lower()
                         in ("off", "0", "no", "false"))

        # 3. DrissionPage 真实 Chrome (最强指纹; 上次 DrissionPage 成功的站点直接走这)
        if not last or last in ("DrissionPage", "sdenv", "unknown", ""):
            logger.info("   sdenv 无完整 token, 尝试 DrissionPage...")
            result = self._generate_cookies_drission(url)
            if result and result.get("cookies"):
                cs = result.get("cookieString") or result.get("cookie_string") or ""
                self._save_cookies(url, result["cookies"], cs, "DrissionPage")
                return result

        # 4. Playwright 兜底
        if not _browsers_off:
            logger.info("   DrissionPage 未成功, 尝试 Playwright...")
            result = self._generate_cookies_playwright(url)
            if result and result.get("cookies") and self._has_rs_token(result["cookies"]):
                cs = result.get("cookieString") or result.get("cookie_string") or ""
                self._save_cookies(url, result["cookies"], cs, "playwright")
                return result
        return None

    def _has_rs_token(self, cookies: list) -> bool:
        """完整瑞数 token: 必须含值 > 200 字符的 cookie (T token)
        只有 S token (88字符) 不完整, VMP 二次计算未完成
        """
        return any(c.get("value") and len(c["value"]) > 200 for c in cookies)

    # ── HTTP 请求 ────────────────────────────────────

    def _build_headers(self, extra: Optional[dict] = None) -> dict:
        headers = {"User-Agent": self._ua}
        if extra:
            headers.update(extra)
        return headers

    def _make_request(
        self, method: str, url: str, headers: Optional[dict] = None,
        cookie_string: str = "", **kwargs
    ) -> cffi_requests.Response:
        h = self._build_headers(headers)
        if cookie_string:
            h["Cookie"] = cookie_string

        # 兰大等站点 SSL 证书链异常, 关验证; 用 chrome110 更通用
        kwargs.setdefault("verify", False)
        # 统一代理（http_client 传入）
        if self.proxies and "proxies" not in kwargs:
            kwargs["proxies"] = self.proxies
        return cffi_requests.request(
            method=method, url=url, headers=h,
            impersonate="chrome110", timeout=self.timeout,
            **kwargs,
        )

    class _FakeRuishuBlock:
        """连接异常伪装成 412, 复用 request() 的 Cookie 链 + CDP 降级。

        2026-08-25: 瑞数层级 2/3 (broken pipe / connection refused) 此前
        直接抛异常透传 — Cookie 生成链与 CDP 路径从未触发, 降级链也因
        异常而非 412 走不到后续兜底。伪装 412 让现有「被拦截 → 生成
        Cookie → CDP 真实浏览器」逻辑自然接管。
        """
        status_code = 412
        text = ""
        headers = {}

    def _guarded_request(self, method, url, headers, cookie_string="", **kwargs):
        """_make_request + 连接类异常伪装 412 (瑞数断连/拒绝)。超时不伪装。

        2026-10-16 东南大学 webplus.seu.edu.cn NXDOMAIN 实锤: DNS 解析失败
        (curl 6) 同样抛 ConnectionError 被伪装 412 → Cookie 链+CDP 对死域名
        白烧(1615 批 39 次全同一域名)。DNS 死域名绝无 Cookie 可救 — 必须
        区分透传, 让上层 fix_loop「子站公网不可达 → 域名变体 + 7 天冻结
        账本」路径接管。真瑞数是 TCP 层断连/拒绝(域名可解析), 不受影响。"""
        try:
            return self._make_request(method, url, headers, cookie_string, **kwargs)
        except (cffi_requests.exceptions.ConnectionError,
                cffi_requests.exceptions.SSLError) as e:
            _msg = str(e)
            if re.search(r"Could not resolve host|Name or service not known|"
                         r"getaddrinfo failed|NXDOMAIN|curl: \(6\)", _msg):
                logger.warning(f"   ❌ DNS 解析失败({_msg[:100]}), 非瑞数拦截 — 透传(死域名无 Cookie 可救)")
                raise
            # 2026-10-28 合工大 hgxy.hfut 实锤: 站方详情页 302 到
            # http://127.0.0.1/cas/login(CMS 认证服务地址错配泄漏),
            # curl 跟随重定向连本地环回必拒 — 伪装 412 走 Cookie 链/CDP
            # 白烧(9333 互搏 60s+/请求, 整系 0 落盘)。环回拒绝=站侧
            # 配置问题, 与瑞数无关, 透传快速失败(上层降级链秒过)。
            if re.search(r"127\.0\.0\.1|\[::1\]", _msg):
                logger.warning(f"   ❌ 环回拒绝(站方重定向泄漏 {_msg[:80]}), 非瑞数 — 透传快速失败")
                raise
            logger.warning(f"   ⚠️ 连接被瑞数掐断/拒绝 ({e}), 伪装 412 走 Cookie 链/CDP")
            _CONNECTION_BLOCKED_HOSTS.add(self._domain_key(url))
            return self._FakeRuishuBlock()

    def _fetch_via_playwright(self, url: str) -> Optional["PlaywrightResponse"]:
        """Playwright 浏览器内请求 — 用于指纹绑定的 T token (curl 400 时)
        返回包装对象, 接口兼容 curl_cffi Response (status_code/text/headers)
        并发预算: playwright launch 也占浏览器槽位, 满则降级。
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None

        try:
            with browser_budget():
                return self._fetch_via_playwright_locked(url)
        except BrowserBudgetExceeded:
            logger.warning("   ⛔ 浏览器并发预算满, 跳过 Playwright 请求")
            return None
        except Exception as e:
            logger.warning(f"   Playwright 请求失败: {e}")
            return None

    def _fetch_via_playwright_locked(self, url: str) -> Optional["PlaywrightResponse"]:
        """(预算已占用) Playwright 浏览器内请求主体

        2026-08-24 池化: 连接常驻 9222 (cdp_util.cdp_page), 用完自动关 tab。
        """
        from types import SimpleNamespace

        try:
            from cdp_util import cdp_page
        except ImportError:
            return None
        try:
            with cdp_page() as (page, ctx):
                resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                # 若被瑞数二次拦截, 等 VMP 完成后 reload
                page.wait_for_timeout(3000)
                if resp and resp.status in (202, 412):
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(5000)
                content = page.content()
                status = resp.status if resp else 200
                headers = dict(resp.headers) if resp else {}
                cookies = ctx.cookies()

                result = SimpleNamespace(
                    status_code=status,
                    text=content,
                    headers=headers,
                    cookies={c["name"]: c["value"] for c in cookies},
                    url=page.url,
                )
                return result
        except Exception as e:
            logger.warning(f"   Playwright 请求失败: {e}")
            return None

    def _is_blocked(self, resp: cffi_requests.Response) -> bool:
        """判断响应是否为瑞数拦截"""
        if resp.status_code in (202, 412, 400):
            # 400: T token 指纹绑定站点 (北邮) 拒绝非浏览器请求
            return True
        if resp.status_code == 200 and len(resp.text) < 500:
            # 可能是空白 JS 页面
            if "<script" in resp.text and "document.cookie" in resp.text.lower():
                return True
        return False

    def request(
        self, method: str, url: str,
        headers: Optional[dict] = None,
        force_refresh: bool = False,
        **kwargs,
    ) -> cffi_requests.Response:
        """发送 HTTP 请求, 自动管理瑞数 Cookie。

        流程:
        1. 检查 Cookie 缓存 → 有效则复用
        2. 发起请求 → 成功返回
        3. 被拦截 (412/202/空响应) → 重新生成 Cookie → 再次请求
        4. 保存新 Cookie 供后续使用

        Parameters
        ----------
        method: "GET" | "POST"
        url: 目标 URL
        headers: 额外请求头
        force_refresh: 强制重新生成 Cookie (忽略缓存)
        **kwargs: 传给 curl_cffi.requests.request 的额外参数
        """
        resp = self._request_impl(method, url, headers=headers,
                                  force_refresh=force_refresh, **kwargs)
        # 2026-08-25: http:// 连接级失败 (80 端口被拒/断连 — 瑞数层级 3
        # IP 黑名单典型症状, 如南师大 xinchuan) → 升级 https 整链重跑。
        # https 通常返回挑战页 (412) 而非直接断连, Cookie 链/CDP 可过。
        if isinstance(resp, self._FakeRuishuBlock) and url.startswith("http://"):
            https_url = "https://" + url[len("http://"):]
            logger.info(f"   ⚠️ http 连接被拒, 升级 https 整链重跑: {https_url}")
            return self._request_impl(method, https_url, headers=headers,
                                      force_refresh=force_refresh, **kwargs)
        return resp

    def _request_impl(
        self, method: str, url: str,
        headers: Optional[dict] = None,
        force_refresh: bool = False,
        **kwargs,
    ) -> cffi_requests.Response:
        """请求主体 (http→https 升级由 request() 包装层处理)"""
        used_cache = False

        # 连接级失败域 (瑞数第 3 层黑名单, 如南师大 xinchuan): curl 层必败,
        # 缓存裸请求也是白跑, 直通生成链 (浏览器兜底)。
        direct_browser = self._domain_key(url) in _CONNECTION_BLOCKED_HOSTS

        if not force_refresh and not direct_browser:
            # 尝试用缓存 Cookie 发请求
            cached = self._load_cookies(url)
            if cached and cached.get("cookie_string"):
                resp = self._guarded_request(method, url, headers, cached["cookie_string"], **kwargs)
                if not self._is_blocked(resp) and resp.status_code < 500:
                    return resp
                # 连接级失败(伪 412) ≠ Cookie 失效: 保留缓存, 由包装层 request()
                # 升级 https 后用同一缓存重试 — 浏览器是保底方案, 缓存有效时
                # 不进浏览器链 (2026-08-25 用户反馈「不要动不动就用浏览器」)
                if isinstance(resp, self._FakeRuishuBlock):
                    return resp
                # 真拦截 (412/400 响应) → 缓存确实失效, 才走重新生成
                self._cookie_file(url).unlink(missing_ok=True)
                logger.info("🔄 缓存 Cookie 已失效, 重新生成...")

        # 生成新 Cookie (内部已按上次成功方案优先)
        # 连接级失败域: 跳过 _get_cookies 内部缓存加载, 强制走生成链拿页面
        result = self._get_cookies(url, skip_cache=direct_browser)
        if not result:
            # 生成失败, 不带 Cookie 发最后一次
            logger.warning("⚠️ 无 Cookie 可用, 裸请求")
            return self._guarded_request(method, url, headers, "", **kwargs)

        # 保存新 Cookie (method 已在 _get_cookies 中记录)
        cs = result.get("cookieString") or result.get("cookie_string") or ""

        # 用新 Cookie 发请求
        if direct_browser:
            # 连接级失败域: 裸请求必败, DrissionPage 已捕获完整页面 → 直接返回
            if result.get("html") and len(result["html"]) > 5000:
                from types import SimpleNamespace
                logger.info("   ✅ DrissionPage 已捕获页面, 直接返回 (连接级失败域)")
                return SimpleNamespace(
                    status_code=200, text=result["html"], headers={},
                    cookies={c["name"]: c["value"] for c in result["cookies"]},
                    url=url,
                )
            resp = self._FakeRuishuBlock()
        else:
            resp = self._guarded_request(method, url, headers, cs, **kwargs)

        # curl_cffi 被 400/412 拒绝 (T token 指纹绑定)
        if resp.status_code in (400, 412, 202) or (resp.status_code == 200 and len(resp.text) < 500):
            # DrissionPage 已抓到完整页面 → 直接包装返回
            if result.get("html") and len(result["html"]) > 5000:
                logger.info("   ✅ DrissionPage 已捕获页面, 直接返回")
                from types import SimpleNamespace
                return SimpleNamespace(
                    status_code=200, text=result["html"], headers={},
                    cookies={c["name"]: c["value"] for c in result["cookies"]},
                    url=url,
                )

            logger.info("   curl_cffi 被拒, 转 Playwright 浏览器内请求...")
            pw_resp = self._fetch_via_playwright(url)
            if pw_resp is not None:
                return pw_resp

        return resp

    def get(self, url: str, headers: Optional[dict] = None, **kwargs) -> cffi_requests.Response:
        """GET 请求"""
        return self.request("GET", url, headers=headers, **kwargs)

    def post(self, url: str, headers: Optional[dict] = None, **kwargs) -> cffi_requests.Response:
        """POST 请求"""
        return self.request("POST", url, headers=headers, **kwargs)

    # ── 便捷方法 ─────────────────────────────────────

    def clear_cache(self, url: Optional[str] = None):
        """清除 Cookie 缓存"""
        if url:
            f = self._cookie_file(url)
            f.unlink(missing_ok=True)
            logger.info(f"🗑️ 删除: {f.name}")
        else:
            for f in self.cookie_dir.glob("*.json"):
                f.unlink(missing_ok=True)
            logger.info(f"🗑️ 清空缓存目录")

    def cache_info(self) -> list[dict]:
        """查看缓存状态"""
        result = []
        for f in sorted(self.cookie_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text("utf-8"))
                age = time.time() - data.get("created_at", 0)
                result.append({
                    "file": f.name,
                    "url": data.get("url", "?"),
                    "cookies": data.get("count", len(data.get("cookies", []))),
                    "age_sec": int(age),
                    "expired": age > self.max_age,
                })
            except Exception:
                result.append({"file": f.name, "error": True})
        return result


# ── 便捷函数 ────────────────────────────────────────
_default_spider: Optional[RsSpider] = None


def get(url: str, **kwargs) -> cffi_requests.Response:
    """全局便捷 GET"""
    global _default_spider
    if _default_spider is None:
        _default_spider = RsSpider()
    return _default_spider.get(url, **kwargs)


def post(url: str, **kwargs) -> cffi_requests.Response:
    """全局便捷 POST"""
    global _default_spider
    if _default_spider is None:
        _default_spider = RsSpider()
    return _default_spider.post(url, **kwargs)


# ── 自测 ────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    spider = RsSpider()

    # 测试 1: 四川大学 (应用层瑞数, HTTP 202)
    print("\n" + "=" * 60)
    print("测试 1: 四川大学 (应用层瑞数, HTTP 202)")
    resp = spider.get("https://www.scu.edu.cn")
    print(f"  Status: {resp.status_code} | Length: {len(resp.text)}")
    if len(resp.text) > 500:
        print("  ✅ 通过")

    # 测试 2: 青海大学 (应用层瑞数)
    print("\n" + "=" * 60)
    print("测试 2: 青海大学 (应用层瑞数)")
    resp2 = spider.get("https://www.qhu.edu.cn")
    print(f"  Status: {resp2.status_code} | Length: {len(resp2.text)}")
    if len(resp2.text) > 500:
        print("  ✅ 通过")

    # 测试 3: 缓存复用
    print("\n" + "=" * 60)
    print("测试 3: 四川大学 (缓存复用)")
    resp3 = spider.get("https://www.scu.edu.cn")
    print(f"  Status: {resp3.status_code} | Length: {len(resp3.text)}")

    # 注意: 以下学校仅 RsSpider 不够,需要 CDP
    # 兰州大学 — TCP broken pipe (网络层瑞数)
    # 西安交通大学 — TCP broken pipe (网络层瑞数)
    # 华中农业大学 — TCP broken pipe (网络层瑞数)
    # → 需启动 Chrome: /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
