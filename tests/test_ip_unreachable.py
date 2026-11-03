#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IP级封禁前置检测单元测试 (2026-09-02 用户指令: 连接拒绝直接 FAIL 不重试)。

判定口径(2026-09-04 升级后):
  指纹轮换链(chrome110→chrome136→firefox147→safari18_0) 拿不到响应
  且 系统 curl 腿(000/超时) 不可达 且 代理腿(≥500 或 446/447/460/461/517
  代理CONNECT失败) 拿不到真实响应 → True(不可达)。
  任何腿拿到状态码(含 412 挑战页 = 可破解) → False 不误杀。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))
sys.path.insert(0, str(_PROJ))
sys.path.insert(0, str(_PROJ / "util"))

import fix_loop  # noqa: E402


class _Resp:
    def __init__(self, status=200, text="x" * 600):
        self.status_code = status
        self.text = text


def _sub_curl_000():
    """系统 curl 腿返回 000(传输层失败)。"""
    r = Mock()
    r.stdout = "000"
    return r


def _no_imps():
    """指纹轮换链全失败。"""
    return patch("util.fingerprint_chain.try_impersonations",
                 return_value=(None, None))


class TestIpUnreachable(unittest.TestCase):

    def test_all_transport_fail_marks_unreachable(self):
        # 指纹链失败 + 系统curl 000 + 代理异常 → True(封禁)
        with _no_imps(), \
             patch("subprocess.run", return_value=_sub_curl_000()), \
             patch("http_client._build_proxies",
                   side_effect=OSError("broken pipe")), \
             patch("http_client._PROXY_OK_HOSTS", set()):
            self.assertTrue(fix_loop._ip_unreachable("https://www.njnu.edu.cn/"))

    def test_challenge_status_means_reachable(self):
        # 指纹链拿到 412 挑战页 = 网络可达(可破解) → False
        with patch("util.fingerprint_chain.try_impersonations",
                   return_value=(_Resp(412), "chrome110")):
            self.assertFalse(fix_loop._ip_unreachable("https://www.lzu.edu.cn/"))

    def test_proxy_real_response_means_reachable(self):
        # 指纹链失败 + 系统curl 000, 但代理拿到 200 → False(仅代理可达不误杀)
        with _no_imps(), \
             patch("subprocess.run", return_value=_sub_curl_000()), \
             patch("http_client._build_proxies",
                   return_value={"https": "http://p:15818"}), \
             patch("http_client._PROXY_OK_HOSTS", set()), \
             patch("curl_cffi.requests.get", return_value=_Resp(200)):
            self.assertFalse(fix_loop._ip_unreachable("https://x.test.edu.cn/"))

    def test_proxy_502_means_unreachable(self):
        # 代理 502(代理侧也到不了目标) → True
        with _no_imps(), \
             patch("subprocess.run", return_value=_sub_curl_000()), \
             patch("http_client._build_proxies",
                   return_value={"https": "http://p:15818"}), \
             patch("http_client._PROXY_OK_HOSTS", set()), \
             patch("curl_cffi.requests.get", return_value=_Resp(502)):
            self.assertTrue(fix_loop._ip_unreachable("https://x.test.edu.cn/"))

    def test_proxy_446_connect_fail_means_unreachable(self):
        # 2026-10-24 dps 代理 CONNECT 失败(460 等) ≠ 真实响应 → True
        with _no_imps(), \
             patch("subprocess.run", return_value=_sub_curl_000()), \
             patch("http_client._build_proxies",
                   return_value={"https": "http://p:15818"}), \
             patch("http_client._PROXY_OK_HOSTS", set()), \
             patch("curl_cffi.requests.get", return_value=_Resp(460)):
            self.assertTrue(fix_loop._ip_unreachable("https://x.test.edu.cn/"))

    def test_proxy_ok_host_not_judged(self):
        # 已知代理可达域名 → 不判封禁, 交正常链路
        with _no_imps(), \
             patch("http_client._PROXY_OK_HOSTS", {"x.test.edu.cn"}):
            self.assertFalse(fix_loop._ip_unreachable("https://x.test.edu.cn/"))


if __name__ == "__main__":
    unittest.main()
