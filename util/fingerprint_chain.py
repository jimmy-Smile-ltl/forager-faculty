#!/usr/bin/env python3
"""指纹轮换链 (2026-09-04 用户指令): 单一 chrome 指纹太老/太新都会被不同
WAF 拦 — 兰大/川大拦 chrome124/131 放 chrome110; 中大/兰大拦全部 curl_cffi
Chrome 指纹但放系统 curl。轮换顺序(直连优先, 失败才轮到下一个):

  chrome110(拦新不拦老型) → chrome136(新版) → firefox147(火狐) →
  safari18_0(苹果) → 上层兜底(系统 curl → nodenv → 浏览器)

跨平台说明: 本模块只用 curl_cffi(纯 Python, 全平台一致); 系统 curl 兜底在
smart_fetch._try_system_curl, macOS 自带 /usr/bin/curl、Windows 10 1803+
自带 System32\\curl.exe、Linux 几乎必有 — 但 TLS 指纹随平台而异(Windows
Schannel ≠ macOS SecureTransport), 换平台后需按实测调整链序(链序由本文件
IMPERSONATE_CHAIN 统一改, 一处生效)。
"""
from __future__ import annotations

# 轮换链: 全部为 curl_cffi 0.16.0 BrowserType 实测存在的模拟目标
IMPERSONATE_CHAIN = ["chrome110", "chrome136", "firefox147", "safari18_0"]


def try_impersonations(url: str, timeout: float, headers: dict,
                       proxies=None, checker=None):
    """按 IMPERSONATE_CHAIN 依次尝试 curl_cffi 各浏览器指纹。
    checker(resp) 返回 True 即成功; None=任何 HTTP 响应都算成功(预检用)。
    返回 (resp, profile) 或 (None, None)。所有异常吞掉交上层。"""
    import curl_cffi.requests as _cffi
    for profile in IMPERSONATE_CHAIN:
        try:
            resp = _cffi.get(url, headers=headers, impersonate=profile,
                             timeout=timeout, proxies=proxies)
            if checker is None or checker(resp):
                return resp, profile
        except Exception:
            continue
    return None, None
