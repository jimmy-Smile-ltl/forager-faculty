# -*- coding: utf-8 -*-
"""高校教师采集项目 — 纯逻辑单元测试包（无网络 / 无 LLM / 无浏览器）。"""
# 2026-09-02 浏览器隔离守卫 (缺点#6 根治): 全量 unittest 任何代码路径
# 意外 Popen Chrome 都在此守卫处炸响 (宁红不静默), 杜绝测试拉起真实浏览器
# 劫持个人 Chrome 单例。合法测试 (如 browser_idle_reap) 自行 patch
# subprocess.Popen 会覆盖本守卫 — 行为不受影响。
import subprocess as _subprocess

_real_popen = _subprocess.Popen


def _guarded_popen(args, **kwargs):
    cmd = " ".join(args) if isinstance(args, (list, tuple)) else str(args)
    if "hrome" in cmd:  # Google Chrome / Chromium
        raise RuntimeError(
            f"[tests] 禁止真实拉起 Chrome, 请 mock: {cmd[:120]}")
    return _real_popen(args, **kwargs)


_subprocess.Popen = _guarded_popen
