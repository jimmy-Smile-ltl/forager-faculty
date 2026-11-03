#!/usr/bin/env python3
"""浏览器操作审计日志 (2026-09-10 用户指令): 创建/销毁/开关标签页等浏览器
操作统一写日志, 便于排查「创建销毁是否太频繁」与劫持/内存问题。

输出: department teachers/logs/browser_ops.log
每行: [时间] [pid] [操作] 详情 — 无敏感信息, 仅操作与端口/窗口标识。
"""
import os
import threading
from datetime import datetime
from pathlib import Path

_LOG = Path(__file__).resolve().parent.parent / "department teachers" / "logs" / "browser_ops.log"
_LOCK = threading.Lock()


def browser_op(action: str, detail: str = ""):
    """记录一次浏览器操作。action: create/destroy/tab_open/tab_close/launch/
    kill/refuse/reap/quit 等; detail: 端口/窗口标识/原因。"""
    try:
        line = (f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] "
                f"[{os.getpid():>7d}] [{action}] {detail}".rstrip())
        with _LOCK:
            with open(_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def browser_op_shell(action: str, detail: str = ""):
    """shell 侧(watchdog)调用的同格式日志入口。"""
    try:
        line = (f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"[   shell] [{action}] {detail}".rstrip())
        with _LOCK:
            with open(_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
