#!/usr/bin/env python3
"""
浏览器并发预算 — 限制"另开浏览器"路径的并发数量。

背景: DrissionPage 窗口池 (3 窗口 × 4 tab = 12) 已有自身上限 (smart_fetch),
但 handleRuishu 的 playwright launch / 自启 Chrome CDP、smart_fetch 的
CDP new_page、spider 的 playwright 等路径, 每次请求都会新建浏览器实例
(或页面), 多线程并发时可同时拉起多个 Chromium, 内存打爆。
本模块提供全局并发信号量, 超限立即拒绝 (与窗口池满负荷拒绝语义一致,
调用方自行降级 / 返回 None)。

配置 (college teacher/.env):
    DRISSION_EXTRA_BROWSERS=3   # 窗口池之外同时打开的浏览器实例/页面上限

用法:
    from browser_budget import browser_budget, BrowserBudgetExceeded

    try:
        with browser_budget():
            browser = p.chromium.launch(headless=True)   # 占用一个槽位
            ...
    except BrowserBudgetExceeded:
        return None    # 并发超限, 降级
"""

import os as _os
import threading as _threading
from contextlib import contextmanager
from typing import Optional

try:
    from dotenv import load_dotenv
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)  # .env 强制覆盖
except Exception:
    pass

MAX_EXTRA_BROWSERS = max(1, int(_os.environ.get("DRISSION_EXTRA_BROWSERS", "3")))
_semaphore = _threading.BoundedSemaphore(MAX_EXTRA_BROWSERS)


class BrowserBudgetExceeded(Exception):
    """浏览器并发预算耗尽 — 立即拒绝, 调用方应降级"""


@contextmanager
def browser_budget(timeout: Optional[float] = 0.0):
    """占用一个浏览器槽位; 预算满则抛 BrowserBudgetExceeded (默认立即拒绝)。

    与窗口池满负荷拒绝语义一致 — 不做排队等待, 调用方降级到下一级
    (CDP 页面 / 直连 / 返回 None), 避免线程堆积把内存拖垮。
    """
    if not _semaphore.acquire(timeout=timeout):
        raise BrowserBudgetExceeded(
            f"浏览器并发超限({MAX_EXTRA_BROWSERS}), 拒绝新建浏览器")
    try:
        yield
    finally:
        _semaphore.release()
