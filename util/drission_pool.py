#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DrissionPage 浏览器窗口池 — 2026-09-02 从 smart_fetch 抽出整合到 util/。

本地兜底池: 每进程独立全局, _MAX_WINDOWS 窗口 × _MAX_TABS_PER_WINDOW tab
(默认 3×4=12)。租用 acquire_tab() / 归还 release_tab(tab); 池空闲超
IDLE_REAP(浏览器生命周期统一阈值, 见 browser_lifecycle) 自动整体回收,
下次按需重建; 进程退出 atexit 兜底关闭不残留。
另含 9222 可用性探测 can_cdp()(依赖 cdp_util.cdp_page)。
"""
import os as _os
import re as _re
import time as _time
import threading as _threading
import atexit as _atexit
import sys as _sys
from pathlib import Path as _Path

# ── 自身目录入 sys.path (2026-09-02 修复): 兄弟模块 browser_lifecycle 顶层
# 导入在 spider 子进程等仅把上级根放入 PYTHONPATH 的场景下无法解析
# → ModuleNotFoundError (同 smart_fetch 事故)。此处自举保证任何调用方可用。
_UTIL_DIR = str(_Path(__file__).resolve().parent)
if _UTIL_DIR not in _sys.path:
    _sys.path.insert(0, _UTIL_DIR)

from browser_lifecycle import (  # noqa: F401  统一阈值 + 活动时间戳
    IDLE_REAP, touch_browser_activity)

# ── 浏览器窗口池 ──────────────────────────────────
# 设计约束(2026-08-23): 窗口上限 _MAX_WINDOWS(3), 每窗口 tab 上限
# _MAX_TABS_PER_WINDOW(4) → 总 tab 上限 12 (tab:窗口 = 4:1)。
#   - tab 用完即关(_release_tab): 及时释放内存
#   - 窗口复用: 优先填满已有窗口, 满 4 才开新窗口, 不频繁新建窗口
#   - 池空闲超时整体回收; 进程退出 atexit 关闭 — 不留孤儿进程
# 2026-08-25 池化解耦(用户指令): 本池降级为"9222 不可用时的兜底"。
# _acquire_tab 优先连接 CDP 9222 常驻浏览器 (全局唯一窗口, tab 池化,
# 由 watchdog 守护, 跨进程共享), 连接失败才回退本进程独立窗口池。
import threading as _threading
import time as _time
import atexit as _atexit

_MAX_WINDOWS = int(_os.environ.get("DRISSION_MAX_WINDOWS", "3"))        # 窗口上限 (兜底池)
_MAX_TABS_PER_WINDOW = int(_os.environ.get("DRISSION_MAX_TABS_PER_WINDOW", "4"))  # 每窗口 tab 上限 (4:1)
_MAX_TABS = _MAX_WINDOWS * _MAX_TABS_PER_WINDOW  # 总 tab 上限 (兜底池)
# CDP 9222 统一池 (2026-08-25): 全局唯一浏览器, tab 池化, 跨进程共享。
# 窗口数恒为 1 — 不再出现"每 spider 子进程一个 3 窗口池 → 24 并发几十个窗口"。
_CDP_PORT = int(_os.environ.get("DRISSION_CDP_PORT", "9222"))
_CDP_MAX_TABS = int(_os.environ.get("DRISSION_CDP_MAX_TABS", "12"))   # 9222 浏览器内 tab 上限
# 窗口池端口基准 (P1-4, 2026-08-23): 按进程 PID 偏移 — ProcessPool 多
# worker 各进程内窗口池是独立全局, 都认为"没有窗口"→ 同时 _free_port(9340+)
# 杀掉别的 worker 刚启动的 Chrome → ws handshake 404 (第3轮兰大/南师大
# 主因修复后的放大器)。偏移粒度 = 窗口上限(_MAX_WINDOWS): 各 worker 的
# 端口块 [base, base+_MAX_WINDOWS) 互不重叠(相邻 PID 也安全), 不跨进程互杀。
# 环境变量可整体平移; 范围 ≤ 9340+999*3+2 ≈ 13k, 远小于 65535, 避开
# CDP 9222 / 调试 9333。
_DRISSION_PORT_BASE = int(_os.environ.get("DRISSION_PORT_BASE", "9340")) + (
    (_os.getpid() % 1000) * _MAX_WINDOWS)

_drission_pool: list = []          # 窗口池 [ChromiumPage, ...]
_drission_procs: dict = {}         # id(page) -> Popen (2026-09-10 预拉起模式: 池持有句柄, shutdown 精确收尸)
_drission_pool_available = None    # None=未探测 | True/False
_drission_fail_until = 0.0         # 2026-09-03: 创建失败冷却截止时间戳(冷却后自动重试)
_cdp_available = None              # CDP 桥接可用性缓存 (2026-08-23 重构漏删初始化, NameError 修复)
_drission_leases: dict = {}        # id(page) -> 租出未归还的 tab 数 (自维护计数,
                                   # 不依赖 page.tabs — headless 下不可靠)
_tab_to_page: dict = {}            # id(tab) -> page (归还时定位窗口减计数)
_drission_last_used = 0.0
_DRISSION_LOCK = _threading.Lock()  # 池内 tab 分配/回收串行化

import subprocess as _subprocess   # 端口清理用


def _free_port(port: int):
    """确保调试端口空闲 — DrissionPage 的 page.quit() 可能残留 Chrome 进程
    (僵尸进程仍占端口, 重建窗口时 ChromiumPage 会连到已断开的旧浏览器,
    首次请求侥幸成功、第二次必断 PageDisconnectedError)。必须持锁调用。"""
    try:
        out = _subprocess.run(
            ["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, timeout=5)
        for pid in out.stdout.split():
            try:
                os.kill(int(pid), 9)
            except Exception:
                pass
    except Exception:
        pass

# 池空闲回收阈值(秒), 环境变量可调
# 2026-08-24: 300→7200 窗口常驻 — 用户要求"窗口不要频繁开关, 维持几个窗口一直
# 开着"。原 5 分钟空闲即整体 quit → 下波请求重建 3 窗口, 形成 5 分钟一轮
# 开→关循环 (观察到的窗口频繁新建/关闭)。7200s 只在极长空闲时才回收。
# 2026-09-02 用户指令"按需启动+3分钟空闲关闭": 统一 BROWSER_IDLE_REAP
# (默认 180s) 三浏览器一致; DRISSION_IDLE_TIMEOUT 仍可独立覆盖。
_IDLE_TIMEOUT = float(_os.environ.get("DRISSION_IDLE_TIMEOUT") or IDLE_REAP)


def _shutdown_drission():
    """关闭池中全部窗口并复位 — 空闲回收/进程退出共用; 必须持 _DRISSION_LOCK 调用"""
    global _drission_pool, _drission_pool_available, _drission_leases, _tab_to_page
    global _drission_last_used, _drission_procs
    from browser_ops import browser_op
    browser_op("destroy", f"pool_shutdown 窗口数={len(_drission_pool)}")
    for i, page in enumerate(_drission_pool):
        try:
            page.quit()  # 关掉 Chromium 子进程, 不留孤儿
        except Exception:
            pass
        # 2026-09-10 预拉起模式: 直接 kill 持有的 Popen 句柄, 双保险收尸
        # (connect 模式 page.quit() 走 Browser.close, 个别挂死场景不生效)
        proc = _drission_procs.pop(id(page), None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _free_port(_DRISSION_PORT_BASE + i)  # quit 可能残留: 强制清端口
    _drission_pool = []
    _drission_procs = {}
    _drission_leases = {}
    _tab_to_page = {}
    _drission_pool_available = None
    _drission_last_used = 0.0


_atexit.register(_shutdown_drission)  # 进程退出兜底: 不随主进程残留


def _window_tab_count(page) -> int:
    """窗口内当前租用 tab 数 (自维护计数: 租出未归还数 = 实际打开的 tab 数,
    因为 tab 用完即关)"""
    return _drission_leases.get(id(page), 0)


def _pool_tab_count() -> int:
    return sum(_window_tab_count(p) for p in _drission_pool)


def _global_window_count() -> int:
    """全局 DrissionPage 窗口数: 唯一 user-data-dir 数(2026-09-08 用户指令)。

    注意 pgrep -f 会把 renderer/GPU helper 全算上(1 窗口≈10 进程) — 必须
    按 ps aux 的 user-data-dir 去重计数。独立函数便于 mock 单测。
    """
    try:
        import subprocess as _sp
        import re as _re
        _out = _sp.run(["ps", "aux"], capture_output=True, text=True, timeout=5).stdout
        return len(set(_re.findall(r"user-data-dir=(\S*DrissionPage/userData/\d+)", _out)))
    except Exception:
        return 0


def _new_window(index: int):
    """新建浏览器窗口 (仅池内窗口数 < 上限时调用, 持锁).
    关键: 每个窗口必须指定不同调试端口 — DrissionPage 的 ChromiumPage
    相同配置是单例的(返回同一浏览器实例), 否则 12 个 tab 会全部挤在
    同一个浏览器里, 窗口池形同虚设。

    2026-09-10 架构升级: 改用独立 Chromium(PROJECT_CHROME, bundle ID 与
    个人 Chrome 不同) — 不共享 macOS 单例, 个人 Chrome 开不开都可创建;
    原「个人 Chrome 未运行则拒绝创建」守卫整套删除。
    """
    from DrissionPage import ChromiumOptions, ChromiumPage
    # 2026-09-15 资源守卫(用户指令: 结合CPU动态管理防卡死): yellow/red 不新建
    # 窗口 — 渲染降级纯CPU路径, 高负载下拉浏览器是卡死主因(窗口池是内存大头)
    try:
        from util.resource_guard import allow_browser as _rg_allow
        if not _rg_allow():
            print("[drission-pool] 资源守卫拒绝建窗(高负载), 降级非浏览器路径",
                  file=__import__("sys").stderr, flush=True)
            return None
    except Exception:
        pass
    try:
        from util.browser_lifecycle import PROJECT_CHROME, _clean_profile_volatile
        if not PROJECT_CHROME:
            print("[drission-pool] 无可用独立 Chromium, 拒绝建窗 "
                  "(降级非浏览器路径)", file=__import__("sys").stderr,
                  flush=True)
            return None
    except Exception:
        PROJECT_CHROME = None
        _clean_profile_volatile = None
    # 2026-09-08 跨进程总窗口上限 + 并发加锁(用户指令): 池上限按进程独立
    # (spider 子进程各自建池), 总窗口数曾无界 — 实测 7 窗口 ~2GB 堆积。
    # 计数口径: 唯一 user-data-dir 数(pgrep -f 会把 renderer/GPU helper
    # 全算上, 1 窗口≈10 进程, 曾误判恒超限)。竞态根治: 检查+建窗全程持
    # flock — 后到者等前窗真正起来后再检查, 不再同建超限(6 spider 同建
    # 实锤); watchdog 每 60s 周期兜底仍保留(见 night_watchdog.sh)。
    port = _DRISSION_PORT_BASE + index
    _free_port(port)  # 防上一轮 quit 残留的僵尸 Chromium 占用端口
    profile = f"/tmp/DrissionPage/userData/{port}"
    if _clean_profile_volatile:
        _clean_profile_volatile(profile)  # OOM 后损坏缓存防启动即崩
    # 2026-09-10 预拉起+连接模式: DrissionPage 自带 launch 在 Chromium 145
    # 上存在 404 竞态 — 浏览器健康拉起但首连 /json/version 未就绪即连接
    # (实测 9400/9401/9405 三连败, 且拉起的浏览器残留成孤儿)。改为自行
    # Popen(参数全可控, 显式 --remote-debugging-address 与
    # --remote-allow-origins) + 等 DevTools 就绪 + set_address 连接。
    _launch_args = [PROJECT_CHROME,
                    f"--remote-debugging-port={port}",
                    "--remote-debugging-address=127.0.0.1",
                    "--headless=new", "--disable-gpu", "--no-sandbox",
                    "--no-first-run", "--no-default-browser-check",
                    "--disable-blink-features=AutomationControlled",
                    "--remote-allow-origins=*",
                    f"--user-data-dir={profile}", "about:blank"]
    # 本地代理转发器(util/local_proxy.py): Chromium 的 --proxy-server
    # 不支持 URL 内嵌认证, 瑞数站点需走代理防单IP高频被风控拦截;
    # 转发器(127.0.0.1:15819)不可用时回退直连, 不阻塞采集
    # 2026-09-03: PROXY_MODE=off(快代理失效期全直连)时不探测转发器,
    # 即使 15819 在跑也强制直连 — 否则窗口池浏览器带代理, 517 反复出现
    # (与 http_client/local_proxy/smart_fetch/fetch_chain 口径一致)。
    try:
        if _os.environ.get("PROXY_MODE", "off").strip() != "off":
            import socket as _socket
            with _socket.create_connection(("127.0.0.1", 15819), timeout=2):
                _launch_args.insert(-1, "--proxy-server=http://127.0.0.1:15819")
    except Exception:
        pass
    import subprocess as _sp
    from browser_ops import browser_op
    proc = None
    try:
        import fcntl as _fcntl
        import sys as _sys  # 函数内局部绑定(拒绝路径的 import 只在守卫分支执行)
        _lockf = open("/tmp/drission_window.lock", "w")
        try:
            _fcntl.flock(_lockf.fileno(), _fcntl.LOCK_EX)
            _gmax = int(_os.environ.get("DRISSION_GLOBAL_MAX_WINDOWS", "4"))
            if _global_window_count() >= _gmax:
                browser_op("refuse", f"window_cap {_gmax} reached (count={_global_window_count()})")
                print(f"[drission-pool] 全局窗口已达上限 {_gmax}, 拒绝新建"
                      f" (降级非浏览器路径)", file=_sys.stderr, flush=True)
                return None
            proc = _sp.Popen(_launch_args, stdout=_sp.DEVNULL,
                             stderr=_sp.DEVNULL)
            browser_op("launch", f"window port={port}")
            # 等 DevTools 就绪 (≤30s): 就绪前绝不连接 — 404 竞态根修
            ready = False
            for _ in range(60):
                if _port_devtools_ready(port):
                    ready = True
                    break
                if proc.poll() is not None:
                    break  # 浏览器启动即崩
                _time.sleep(0.5)
            if not ready:
                raise RuntimeError(f"window port={port} DevTools 未就绪")
            co = ChromiumOptions()
            co.set_address(f"127.0.0.1:{port}")
            page = ChromiumPage(co)
            _drission_procs[id(page)] = proc  # 池持有 Popen 句柄: shutdown 精确收尸
            browser_op("create", f"window port={port} connected")
            return page  # 锁内建窗: 后到者等本窗起来后再检查
        finally:
            try:
                _fcntl.flock(_lockf.fileno(), _fcntl.LOCK_UN)
            except Exception:
                pass
            _lockf.close()
    except Exception:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        return None


def _port_devtools_ready(port: int) -> bool:
    """/json/version 返回 200 才算 DevTools 就绪 (就绪前连接会 404)"""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


# CDP 9222 统一池状态 (2026-08-25 池化解耦): 连接全局唯一常驻浏览器。
# 注意: 连接模式下 page.quit() 会真的关掉 9222 — 本池只 new_tab/close,
# 绝不 quit; 浏览器生命周期由 watchdog 守护 (与 cdp_util 同约定)。
_cdp_drission_page = None        # 连接 9222 的 ChromiumPage (全局唯一, 不放入本地池)
_cdp_drission_tabs: set = set()  # 当前 CDP 池内租出的 tab (id 集合)
_cdp_drission_lock = _threading.Lock()
_cdp_drission_last_check = 0.0
_CDP_RECHECK_INTERVAL = 60.0     # 连接对象超过该秒数未使用 → 重连校验


def _cdp_port_listening() -> bool:
    """9222 CDP DevTools 是否就绪 — 2026-08-27 从"TCP 端口探测"升级为
    /json/version HTTP 探测(定案: 18:40 抓现行)。

    原 TCP 探测(2026-08-25)只验端口: Chrome 启动中(端口已开/DevTools ws
    未就绪)时探测通过 → ChromiumPage 连接失败 → 自动拉起 userData/9222
    非 headless 浏览器(用户"5 个窗口"投诉根因) → 父进程退出后孤儿化
    (ppid=1) → 被 watchdog 孤儿清理每 5 分钟 kill -9 → 9222 反复死。
    端点返回 200 才算就绪, 否则绝不调 ChromiumPage — 返回 None 走本地
    兜底池(headless, 无窗口)。"""
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{_CDP_PORT}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _acquire_cdp_tab():
    """[禁用] 从 CDP 9222 统一池租用一个 tab。

    ⚠️ 2026-08-27 18:54 定案禁用: DrissionPage ChromiumPage 连接模式析构
    (对象被 GC → __del__ → quit()) 会真关远程 9222 Chrome — 18:54:35 现场
    (批次连接后 1:58 被杀)。9222 仅由 playwright cdp_page 独占 (_try_cdp);
    本函数一律返回 None, 调用方回退本地窗口池。
    """
    return None
    # ── 以下为 2026-08-27 前实现, 已废弃 (保留供回溯) ──
    global _cdp_drission_page, _cdp_drission_last_check
    with _cdp_drission_lock:
        if len(_cdp_drission_tabs) >= _CDP_MAX_TABS:
            return None
        try:
            if (_cdp_drission_page is None
                    or _time.time() - _cdp_drission_last_check > _CDP_RECHECK_INTERVAL):
                # 防自启 (2026-08-25 用户报 5 窗口): 端口未监听 → 回退本地池
                if not _cdp_port_listening():
                    _cdp_drission_page = None
                    return None
                from DrissionPage import ChromiumOptions, ChromiumPage
                co = ChromiumOptions()
                co.set_address(f"127.0.0.1:{_CDP_PORT}")
                page = ChromiumPage(co)
                _cdp_drission_page = page
            tab = _cdp_drission_page.new_tab(background=True)  # 2026-08-26: 后台建 tab, 不抢焦点不激活
            _cdp_drission_tabs.add(id(tab))
            _cdp_drission_last_check = _time.time()
            return tab
        except Exception:
            _cdp_drission_page = None  # 连接失效, 下次重连
            return None


def _acquire_tab():
    """租用一个 tab — 本地窗口池 (2026-08-27 定案: DrissionPage 不再连 9222)。

    2026-08-27 18:54 现场: 批次连接 9222 后 Chrome 1 分 58 秒被杀 — 根因是
    DrissionPage ChromiumPage 连接模式的析构陷阱: _acquire_cdp_tab 把
    _cdp_drission_page 置 None (探测抖动/异常) 时, ChromiumPage 对象被 GC,
    __del__ → quit() → 真关掉远程 9222 浏览器 (与 18:40 定案的 auto-launch
    是同一个库的两条死路)。9222 从此只由 playwright cdp_page 独占访问
    (_try_cdp), 连接/归还/GC 均不杀浏览器; DrissionPage 只用本地自启
    headless 窗口池 (quit 杀的是自己的浏览器, 无害)。

    本地兜底池: 3 窗口 × 6 tab = 18 (每进程独立, 2026-08-26 tab 密度 4→6)。
    """
    return _acquire_local_tab()


def _acquire_local_tab():
    """本地窗口池租用 (9222 不可用时的兜底, 原每进程 3×4=12 逻辑):
    1. 池闲置超时 → 整体回收, 按需重建
    2. 总 tab 已达 12 → 拒绝 (调用方降级到 CDP/直连)
    3. 优先复用已有窗口 (窗口内 tab < 4 直接 new_tab, 不频繁新建窗口)
    4. 窗口数 < 3 → 新建窗口
    5. 全部满负荷 → 拒绝
    """
    global _drission_pool_available, _drission_last_used, _drission_fail_until
    with _DRISSION_LOCK:
        if _drission_pool_available is False:
            # 2026-09-03: 创建失败不永久锁死 — 冷却期后自动重置重试
            # (18:00 清理窗口池后某次 _new_window 失败置 False, 此后所有
            # DrissionPage 调用永久返回 None → 渲染型站点全 fetch_failed → 误判死系)
            if _time.time() < _drission_fail_until:
                return None
            _drission_pool_available = True  # 冷却到期, 重置允许重试
            _drission_fail_until = 0.0
        # 池空闲超时 → 整体回收, 下次按需重建
        if _drission_pool and _time.time() - _drission_last_used > _IDLE_TIMEOUT:
            _shutdown_drission()
        # 浏览器活动时间戳: watchdog 据此回收 9222/9333 常驻实例
        # (2026-09-02 按需启动+空闲回收)
        try:
            touch_browser_activity()
        except Exception:
            pass
        # tab 上限检查: 满 12 拒绝 (窗口池不无限扩张)
        if _pool_tab_count() >= _MAX_TABS:
            return None
        # 1) 复用已有窗口 (先填满, 不频繁新建窗口)
        for page in list(_drission_pool):
            if _window_tab_count(page) >= _MAX_TABS_PER_WINDOW:
                continue
            try:
                tab = page.new_tab(background=True)  # 2026-08-26: 后台建 tab, 不抢焦点(本地池 headless 亦统一)
                _drission_leases[id(page)] = _drission_leases.get(id(page), 0) + 1
                _tab_to_page[id(tab)] = page
                _drission_last_used = _time.time()
                from browser_ops import browser_op
                browser_op("tab_open", f"drission reuse win#{id(page) % 1000} tabs={_drission_leases[id(page)]}")
                return tab
            except Exception:
                # 窗口失效 → 剔除出池
                try:
                    page.quit()
                except Exception:
                    pass
                _drission_pool.remove(page)
                _drop_window_state(page)
        # 2) 新建窗口 (上限 3)
        if len(_drission_pool) < _MAX_WINDOWS:
            try:
                page = _new_window(len(_drission_pool))
                if page is None:  # 单例劫持守卫拒绝自启 → 冷却后重试
                    _drission_pool_available = False
                    _drission_fail_until = _time.time() + 60.0
                    return None
                _drission_pool.append(page)
                tab = page.new_tab(background=True)  # 2026-08-26: 后台建 tab
                _drission_leases[id(page)] = 1
                _tab_to_page[id(tab)] = page
                _drission_last_used = _time.time()
                from browser_ops import browser_op
                browser_op("tab_open", f"drission new-win tabs=1")
                return tab
            except Exception:
                _drission_pool_available = False
                _drission_fail_until = _time.time() + 60.0  # 2026-09-03: 60s 冷却后自动重试
                return None
        return None  # 满负荷: 窗口 3 个且 tab 全满


def _drop_window_state(page):
    """窗口剔除时清理其租用计数与 tab 映射 (持锁调用)"""
    pid = id(page)
    _drission_leases.pop(pid, None)
    for k in [k for k, v in _tab_to_page.items() if id(v) == pid]:
        _tab_to_page.pop(k, None)


def _release_tab(tab):
    """归还 tab: CDP 池只关 tab 不关浏览器 (9222 由 watchdog 守护);
    本地池 tab 用完即关, 窗口保留供复用。"""
    global _drission_last_used
    tid = id(tab)
    # CDP 统一池 tab: 只 close, 绝不 quit 浏览器
    with _cdp_drission_lock:
        if tid in _cdp_drission_tabs:
            _cdp_drission_tabs.discard(tid)
            try:
                tab.close()
            except Exception:
                pass
            return
    # 本地兜底池 tab: 原逻辑
    with _DRISSION_LOCK:
        page = _tab_to_page.pop(tid, None)
        if page is not None:
            pid = id(page)
            n = _drission_leases.get(pid, 0)
            if n > 1:
                _drission_leases[pid] = n - 1
            else:
                _drission_leases.pop(pid, None)
        _drission_last_used = _time.time()
    # 2026-08-23 治理: close 移出锁 — CDP 往返是网络操作, 锁内执行会串行化
    # 60 并发下的 tab 回收, 拖慢整个池; 先减计数再关, 计数一致性不受影响
    try:
        tab.close()
        from browser_ops import browser_op
        browser_op("tab_close", "drission local tab")
    except Exception:
        pass


def _can_cdp():
    global _cdp_available
    if _cdp_available is None:
        try:
            from cdp_util import cdp_page
            with cdp_page():
                pass
            _cdp_available = True
        except Exception:
            _cdp_available = False
    return _cdp_available


# ── 对外接口 ──────────────────────────────────────
acquire_tab = _acquire_tab      # 租用 tab, 无可用返回 None
release_tab = _release_tab      # 归还 tab(本地池 tab 用完即关)
can_cdp = _can_cdp              # 9222 CDP 可用性探测(惰性缓存)
shutdown = _shutdown_drission   # 整体关闭池(空闲回收/退出共用)
