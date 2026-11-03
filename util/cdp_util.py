#!/usr/bin/env python3
"""
CDP 9222 常驻浏览器 tab 租用 — playwright 各路径统一入口

背景 (2026-08-24 池化改造): handleRuishu 的 playwright launch 与 spider.py 的
playwright launch 每次请求都 chromium.launch() 新建浏览器, 60 并发下频繁
"创建-关闭", 大量时间耗在浏览器启停。统一改为连接常驻 9222 Chrome
(由 watchdog 守护), 只租用 tab (new_page), 用完 page.close() 不关浏览器。

约定:
- 9222 按需启动 + 空闲回收 (2026-09-02 用户指令): watchdog 只在空闲
  BROWSER_IDLE_REAP 秒后回收; 本模块租用时连接失败会**自启**(与 watchdog
  同参数), 冷启动首请求慢 2-5s
- 调用方**不得**调用 browser.close() (CDP 模式下会真的关闭远程 Chrome)
- 用完只关 tab (page.close()); context 保持存活 (cookies 复用)
"""
import os
import sys as _sys
from contextlib import contextmanager
from pathlib import Path as _Path

# ── 自身目录入 sys.path (2026-09-02 修复): 兄弟模块 browser_lifecycle 顶层
# 导入在仅把上级根放入 PYTHONPATH 的场景下无法解析 → ModuleNotFoundError
# (同 smart_fetch 事故)。此处自举保证任何调用方可用。
_UTIL_DIR = str(_Path(__file__).resolve().parent)
if _UTIL_DIR not in _sys.path:
    _sys.path.insert(0, _UTIL_DIR)

from browser_lifecycle import (  # noqa: F401  策略整合到 util/
    touch_browser_activity, port_listening, ensure_9222)
_port_listening = port_listening
_ensure_9222 = ensure_9222

_CDP_URL = os.environ.get("CDP_BASE_URL", "http://127.0.0.1:9222")

_STEALTH_FULL = ""  # 2026-09-22 用户指令「默认都注入」: puppeteer-extra stealth 完整版
try:
    from pathlib import Path as _P
    _sf = _P(__file__).resolve().parent / "stealth.min.js"
    if _sf.exists():
        _STEALTH_FULL = _sf.read_text(encoding="utf-8")
except Exception:
    pass

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'language', {get: () => 'zh-CN'});
Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
Object.defineProperty(navigator, 'appVersion', {get: () => '5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'});
Object.defineProperty(navigator, 'plugins', {get: () => [
    {name: 'PDF Viewer', description: 'Portable Document Format', filename: 'internal-pdf-viewer'},
    {name: 'Chrome PDF Viewer', description: '', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
    {name: 'Chromium PDF Viewer', description: '', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'}]});
window.chrome = window.chrome || {};
window.chrome.app = window.chrome.app || {};
window.chrome.runtime = window.chrome.runtime || {};
Object.defineProperty(window.chrome, 'loadTimes', {get: () => () => ({commitLoadTime: 500.0, finishDocumentLoadTime: 800.0})});
Object.defineProperty(window.chrome, 'csi', {get: () => () => ({startE: 1, onloadT: 300, pageT: 400.0})});
// canvas 指纹噪声: toDataURL 前随机扰动 1-3 像素值(每次调用输出不同指纹)
(function () {
  const _orig = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function (type) {
    try {
      const ctx = this.getContext('2d');
      if (ctx && !this.__noised && this.width > 0 && this.height > 0) {
        const img = ctx.getImageData(0, 0, this.width, this.height);
        for (let i = 0; i < img.data.length; i += 4) {
          const r = Math.floor(Math.random() * 3);
          if (img.data[i] + r <= 255) img.data[i] += r;
        }
        ctx.putImageData(img, 0, 0);
        this.__noised = true;
      }
    } catch (e) {}
    return _orig.apply(this, arguments);
  };
})();
"""
_STEALTH_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


@contextmanager
def cdp_page():
    """租用一个 tab (连接 9222 常驻 Chrome)。

    Yields: (page, ctx) — ctx 用于读 cookies。
    用完自动 page.close() (只关 tab, 不关浏览器)。

    2026-08-26 不聚焦改造: playwright 无 new_page 的 background 参数
    (DrissionPage 侧 new_tab(background=True) 直接走 CDP 后台建 tab),
    这里用 CDP 差集归还焦点 — 新 tab 创建后把活动 tab 切回创建前的
    原 tab, 采集页不抢用户正在看的 tab; 窗口本身 playwright 从不置顶。
    """
    from playwright.sync_api import sync_playwright
    touch_browser_activity()
    with sync_playwright() as p:
        # 2026-08-31: connect 必须带超时 — 9222 积压陈旧 tab/连接时 connect
        # 会挂满 playwright 默认 180s(实测), 加 15s 快速失败走兜底链
        try:
            browser = p.chromium.connect_over_cdp(_CDP_URL, timeout=15000)
        except Exception:
            # 9222 被空闲回收/崩溃 → 自启后重连 (2026-09-02 按需启动)
            # 2026-09-15 资源守卫(用户指令: 结合CPU动态管理防卡死): yellow/red
            # 不自启 — 高负载下拉起浏览器是卡死主因, 直接失败走纯CPU兜底
            from util.resource_guard import allow_browser as _rg_allow
            if not _rg_allow():
                raise
            if _ensure_9222():
                browser = p.chromium.connect_over_cdp(_CDP_URL, timeout=15000)
            else:
                raise
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        # 创建前: 记录已有 page target (保持顺序, 首条即原活动 tab)
        # 2026-08-27: browser.pages 在部分 playwright 版本/连接模式不存在
        # ('Browser' object has no attribute 'pages' — 实测 19:49 导致 CDP 路径
        # 全异常降级裸请求), 改走 contexts[0].pages 防御性取值。
        _sess, _prev = [], None
        try:
            _pages = ctx.pages if browser.contexts else []
            if _pages:
                _sess = ctx.new_cdp_session(_pages[0])
                _prev = [t["targetId"] for t in _sess.send("Target.getTargets")["targetInfos"]
                         if t.get("type") == "page"]
            else:
                _sess = None
        except Exception:
            _sess = None
        page = ctx.new_page()
        from browser_ops import browser_op
        browser_op("tab_open", "9222 CDP new tab")
        # 瑞数伪装: webdriver 隐藏 + UA 覆盖 (CDP session 级, 每个 tab 生效)
        page.add_init_script(_STEALTH_JS)
        if _STEALTH_FULL:
            try:
                page.add_init_script(_STEALTH_FULL)
            except Exception:
                pass
        try:
            _s = ctx.new_cdp_session(page)
            _s.send("Network.setUserAgentOverride", {"userAgent": _STEALTH_UA})
        except Exception:
            pass
        # 归还焦点: 新 tab 默认成为活动 tab, 切回创建前的原 tab (全 try 包裹, 失败无害)
        if _sess:
            for _tid in _prev:
                try:
                    _sess.send("Target.activateTarget", {"targetId": _tid})
                    break
                except Exception:
                    continue
        try:
            yield page, ctx
        finally:
            try:
                page.close()  # 只关 tab: 常驻浏览器不受影响 (closeTarget 不激活窗口)
                browser_op("tab_close", "9222 CDP tab")
            except Exception:
                pass
