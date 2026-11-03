#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nodenv 形态不支持跳过账本 单元测试 (2026-09-02 缺点#5 改进)。

njust/scu 类形态 VM 跑满 13-16s 无 cookie — 失败记账, TTL 内直跳 sdenv;
覆盖: 失败记账本 / 二次请求短路不发网络 / TTL 过期自愈重试。
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "util"))

import rs_cookie  # noqa: E402


class _Resp:
    def __init__(self, status=412, text="<html>$_ts.nsd=1;</html>" * 10):
        self.status_code = status
        self.text = text


class TestNodenvSkip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _patched_fetch(self):
        return (mock.patch.object(rs_cookie, "_STORE", self.tmp),
                mock.patch.object(rs_cookie, "_nodenv_ready", return_value=True),
                mock.patch.object(rs_cookie, "nodenv_cookie", return_value=""))

    def test_unsupported_form_marks_skip(self):
        with self._patched_fetch()[0], self._patched_fetch()[1], \
             self._patched_fetch()[2], \
             mock.patch.object(rs_cookie, "_chain_get",
                               return_value=_Resp(412)):
            self.assertIsNone(rs_cookie.fetch_with_nodenv(
                "https://cs.njust.edu.cn/1735/list.htm"))
        # 账本已写: 同域第二次请求直接短路
        self.assertTrue(rs_cookie._host_skipped("cs.njust.edu.cn"))

    def test_second_call_short_circuits_no_network(self):
        with mock.patch.object(rs_cookie, "_STORE", self.tmp), \
             mock.patch.object(rs_cookie, "_nodenv_ready", return_value=True), \
             mock.patch.object(rs_cookie, "nodenv_cookie", return_value=""), \
             mock.patch.object(rs_cookie, "_chain_get",
                               return_value=_Resp(412)):
            rs_cookie.fetch_with_nodenv("https://cs.njust.edu.cn/x")
        # 第二次: 账本命中 → 不发任何请求直接 None
        with mock.patch.object(rs_cookie, "_STORE", self.tmp), \
             mock.patch.object(rs_cookie, "_nodenv_ready", return_value=True), \
             mock.patch.object(rs_cookie, "_chain_get",
                               side_effect=AssertionError("账本命中后不得再发请求")):
            self.assertIsNone(rs_cookie.fetch_with_nodenv(
                "https://cs.njust.edu.cn/x"))

    def test_ttl_expiry_self_heals(self):
        # 账本 25h 前 → 过期 → 不再跳过, nodenv 重新尝试
        with mock.patch.object(rs_cookie, "_STORE", self.tmp):
            rs_cookie._mark_skipped("cs.njust.edu.cn")
            skip = rs_cookie._SKIP_STORE
            j = json.load(open(skip, encoding="utf-8"))
            j["cs.njust.edu.cn"] = time.time() - 25 * 3600
            skip.write_text(json.dumps(j), encoding="utf-8")
            self.assertFalse(rs_cookie._host_skipped("cs.njust.edu.cn"))

    def test_supported_host_never_marked(self):
        # 成功路径(200 页面)不记账本
        with mock.patch.object(rs_cookie, "_STORE", self.tmp), \
             mock.patch.object(rs_cookie, "_nodenv_ready", return_value=True), \
             mock.patch.object(rs_cookie, "_chain_get",
                               return_value=_Resp(200, "x" * 600)):
            text = rs_cookie.fetch_with_nodenv("https://www.lzu.edu.cn/x")
        self.assertIsNotNone(text)
        self.assertFalse(rs_cookie._host_skipped("www.lzu.edu.cn"))


if __name__ == "__main__":
    unittest.main()
