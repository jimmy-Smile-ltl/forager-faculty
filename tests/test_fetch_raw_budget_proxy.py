#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器并发预算 + 及时关闭 + 快代理 回归测试（用户 2026-08-23 要求）。

覆盖:
- _fetch_raw Playwright 路径: 浏览器并发预算超限 → 立即降级(不排队)
- _fetch_raw Playwright 路径: goto 抛异常 → finally 关闭浏览器(不泄漏)
- _fetch_raw curl_cffi/requests 路径: 快代理配置透传; 熔断期直连
- 模板 spider_template._fetch_raw / detect_json_api: 同款代理+预算

运行: cd "college teacher" && uv run python -m unittest discover -s tests -v
"""
import inspect
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))
sys.path.insert(0, str(_PROJ / "department teachers" / "templates"))

import collect_teacher as ct  # noqa: E402
import collect_phases  # noqa: E402
import spider_template as tmpl  # noqa: E402
from util.browser_budget import (  # noqa: E402
    BrowserBudgetExceeded, MAX_EXTRA_BROWSERS, browser_budget)

_PROXY_DICT = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}


class _Resp:
    """最小响应模拟 (status_code/text/content)"""

    def __init__(self, status=200, text="x"):
        self.status_code = status
        self.text = text
        self.content = text.encode()


class _FakePW:
    """伪 sync_playwright() 上下文管理器: __enter__ 返回 mock pw 对象"""

    def __init__(self, pw):
        self._pw = pw

    def __enter__(self):
        return self._pw

    def __exit__(self, *a):
        return False


def _patch_smart_fetch_fail():
    return patch("util.smart_fetch.smart_fetch", return_value=("", "blocked"))


def _patch_curl_fail():
    return patch("curl_cffi.requests.get", side_effect=RuntimeError("conn"))


class TestPlaywrightBudgetExceeded(unittest.TestCase):
    """预算占满时 Playwright 路径必须立即抛 BrowserBudgetExceeded 并降级,
    而不是排队/无限新建浏览器 (browser_budget.py 拒绝语义)。"""

    def test_exceeded_degrades_to_requests(self):
        # 占满全部浏览器槽位 → _fetch_raw 内 browser_budget() 抛超限
        held = []
        for _ in range(MAX_EXTRA_BROWSERS):
            cm = browser_budget()
            cm.__enter__()
            held.append(cm)
        try:
            with _patch_smart_fetch_fail(), _patch_curl_fail(), \
                 patch("requests.Session", return_value=Mock(
                     get=Mock(return_value=_Resp(200, "ok" * 300)),
                     mount=Mock(), proxies=None)) as m_sess, \
                 patch("playwright.sync_api.sync_playwright") as m_pw:
                html = ct._fetch_raw("http://x.test/fac.htm", use_playwright=True)
            self.assertEqual(html, "ok" * 300)     # 降级到 requests 拿到结果
            m_pw.assert_not_called()               # 预算满时绝不新建浏览器
            self.assertEqual(m_sess.call_count, 1)  # 降级链走到了 requests
        finally:
            for c in held:
                c.__exit__(None, None, None)

    def test_exceeded_raises_when_no_lower_level(self):
        """全部层级都失败时返回 None, 预算异常不得吞成假成功"""
        held = []
        for _ in range(MAX_EXTRA_BROWSERS):
            cm = browser_budget()
            cm.__enter__()
            held.append(cm)
        try:
            with _patch_smart_fetch_fail(), _patch_curl_fail(), \
                 patch("requests.Session", side_effect=RuntimeError("sess")):
                self.assertIsNone(ct._fetch_raw("http://x.test/fac.htm", use_playwright=True))
        finally:
            for c in held:
                c.__exit__(None, None, None)


class TestPlaywrightClosesOnError(unittest.TestCase):
    """goto 抛异常时浏览器/上下文必须在 finally 中关闭 — 修复前
    ctx.close(); b.close() 在 try 外, goto 异常会泄漏整个浏览器进程。"""

    def test_goto_error_still_closes(self):
        b, c, p = Mock(), Mock(), Mock()
        p.goto.side_effect = RuntimeError("goto boom")
        c.new_page.return_value = p
        b.new_context.return_value = c
        pw = Mock()
        pw.chromium.launch.return_value = b
        with _patch_smart_fetch_fail(), _patch_curl_fail(), \
             patch("requests.Session", return_value=Mock(
                 get=Mock(return_value=_Resp(404, "")), mount=Mock())) as m_sess, \
             patch("playwright.sync_api.sync_playwright", return_value=_FakePW(pw)), \
             patch("socket.create_connection", side_effect=ConnectionRefusedError):
            self.assertIsNone(ct._fetch_raw("http://x.test/", use_playwright=True))
        b.close.assert_called_once()     # finally 关闭, 无泄漏
        c.close.assert_called_once()
        self.assertTrue(m_sess.called)   # 浏览器失败后继续降级, 不是吞异常

    def test_happy_path_closes_once(self):
        b, c, p = Mock(), Mock(), Mock()
        p.goto.return_value = None
        p.content.return_value = "x" * 600  # len>500 才返回, Mock 默认 len=0
        c.new_page.return_value = p
        b.new_context.return_value = c
        pw = Mock()
        pw.chromium.launch.return_value = b
        with _patch_smart_fetch_fail(), _patch_curl_fail(), \
             patch("playwright.sync_api.sync_playwright", return_value=_FakePW(pw)), \
             patch("socket.create_connection", side_effect=ConnectionRefusedError):
            html = ct._fetch_raw("http://x.test/", use_playwright=True)
        self.assertEqual(html, "x" * 600)
        b.close.assert_called_once()
        c.close.assert_called_once()


class TestCurlProxy(unittest.TestCase):
    """curl_cffi 级: 快代理配置透传; 熔断期直连 (proxies=None)"""

    def test_proxy_passed_when_enabled(self):
        with _patch_smart_fetch_fail(), \
             patch("util.http_client._build_proxies", return_value=_PROXY_DICT), \
             patch("util.http_client._proxy_fused", return_value=False), \
             patch("curl_cffi.requests.get", return_value=_Resp(200, "x" * 600)) as m_get:
            html = ct._fetch_raw("http://x.test/fac.htm")
        self.assertEqual(html, "x" * 600)
        self.assertEqual(m_get.call_args.kwargs.get("proxies"), _PROXY_DICT)

    def test_direct_when_fused(self):
        with _patch_smart_fetch_fail(), \
             patch("util.http_client._build_proxies", return_value=_PROXY_DICT), \
             patch("util.http_client._proxy_fused", return_value=True), \
             patch("curl_cffi.requests.get", return_value=_Resp(200, "x" * 600)) as m_get:
            ct._fetch_raw("http://x.test/fac.htm")
        self.assertIsNone(m_get.call_args.kwargs.get("proxies"))  # 熔断 → 直连


class TestRequestsProxy(unittest.TestCase):
    """requests 级: session 挂代理; 熔断期不挂"""

    def test_proxy_passed_when_enabled(self):
        with _patch_smart_fetch_fail(), \
             patch("curl_cffi.requests.get", side_effect=RuntimeError("conn")), \
             patch("util.http_client._build_proxies", return_value=_PROXY_DICT), \
             patch("util.http_client._proxy_fused", return_value=False), \
             patch("requests.Session", return_value=Mock(
                 get=Mock(return_value=_Resp(200, "y" * 600)), mount=Mock(), proxies={})) as m_sess:
            html = ct._fetch_raw("http://x.test/fac.htm")
        self.assertEqual(html, "y" * 600)
        self.assertEqual(m_sess.return_value.proxies, _PROXY_DICT)

    def test_no_proxy_when_fused(self):
        with _patch_smart_fetch_fail(), \
             patch("curl_cffi.requests.get", side_effect=RuntimeError("conn")), \
             patch("util.http_client._build_proxies", return_value=_PROXY_DICT), \
             patch("util.http_client._proxy_fused", return_value=True), \
             patch("requests.Session", return_value=Mock(
                 get=Mock(return_value=_Resp(200, "y" * 600)), mount=Mock(), proxies={})) as m_sess:
            ct._fetch_raw("http://x.test/fac.htm")
        # 熔断期 session 不挂死代理: 保持 requests 默认 {} (未赋值)
        self.assertEqual(m_sess.return_value.proxies, {})


class TestTemplateBudgetProxy(unittest.TestCase):
    """模板 spider_template 与主控同款修复 — LLM 生成 spider.py 的基底"""

    def test_template_fetch_raw_has_budget_and_finally(self):
        src = inspect.getsource(tmpl._fetch_raw)
        self.assertIn("browser_budget", src)   # 并发预算
        self.assertIn("BrowserBudgetExceeded", src)
        self.assertIn("finally", src)          # 异常也关闭
        self.assertIn("_proxy_fused", src)     # 代理+熔断
        self.assertIn("_build_proxies", src)
        self.assertIn("smart_fetch", src)      # P1-5: 统一请求层(R12 直连兜底)

    def test_template_fetch_raw_proxy_passed(self):
        # P1-5 后第一级是 smart_fetch: 短路它(返回空)以测 curl 分支的代理透传
        with patch("util.smart_fetch.smart_fetch", return_value=(None, "")), \
             patch("util.http_client._build_proxies", return_value=_PROXY_DICT), \
             patch("util.http_client._proxy_fused", return_value=False), \
             patch("curl_cffi.requests.get", return_value=_Resp(200, "z" * 600)) as m_get:
            html = tmpl._fetch_raw("http://x.test/")
        self.assertEqual(html, "z" * 600)
        self.assertEqual(m_get.call_args.kwargs.get("proxies"), _PROXY_DICT)

    def test_template_goto_error_still_closes(self):
        b, c, p = Mock(), Mock(), Mock()
        p.goto.side_effect = RuntimeError("boom")
        c.new_page.return_value = p
        b.new_context.return_value = c
        pw = Mock()
        pw.chromium.launch.return_value = b
        with patch("util.http_client._build_proxies", return_value=None), \
             patch("util.http_client._proxy_fused", return_value=False), \
             patch("curl_cffi.requests.get", side_effect=RuntimeError("conn")), \
             patch("requests.get", return_value=_Resp(404, "")), \
             patch("playwright.sync_api.sync_playwright", return_value=_FakePW(pw)), \
             patch("socket.create_connection", side_effect=ConnectionRefusedError):
            self.assertIsNone(tmpl._fetch_raw("http://x.test/"))
        b.close.assert_called_once()
        c.close.assert_called_once()

    def test_template_detect_json_api_uses_proxy(self):
        with patch("util.http_client._build_proxies", return_value=_PROXY_DICT), \
             patch("util.http_client._proxy_fused", return_value=False), \
             patch("requests.get", return_value=_Resp(404, "")) as m_get:
            self.assertIsNone(tmpl.detect_json_api(None, "http://x.test/"))
        for call in m_get.call_args_list:
            self.assertEqual(call.kwargs.get("proxies"), _PROXY_DICT)


class TestSourceSentinels(unittest.TestCase):
    """源码哨兵: 主控 _fetch_raw 及 prompt 示例必须含预算/关闭/代理,
    防止后续改动把修复退化掉(LLM 照示例生成, 示例回退=全链路回退)。"""

    def test_fetch_raw_source_sentinel(self):
        src = inspect.getsource(ct._fetch_raw)
        self.assertIn("browser_budget", src)
        self.assertIn("finally", src)
        self.assertIn("_proxy_fused", src)
        self.assertIn("_build_proxies", src)

    def test_prompt_example_sentinel(self):
        # prompt 中 LLM 照抄的 _fetch_raw 示例, 与真实实现同款
        # 2026-08-31 拆分后该提示词位于 collect_phases.py(phase2 生成提示)
        with open(Path(collect_phases.__file__).resolve(), encoding="utf-8") as f:
            src = f.read()
        i = src.find("spider.py 代码示例")
        self.assertGreater(i, 0)
        example = src[i:i + 6000]
        # 2026-08-28 起提示词改为紧凑规则式(网关长输出超时优化), 不再内嵌完整
        # _fetch_raw 示例; 契约相应改为: 必须点名关键防退化机制
        self.assertIn("browser_budget", example)   # 浏览器并发预算
        self.assertIn("用完即关", example)          # 浏览器生命周期
        self.assertIn("_build_proxies", example)   # 快代理
        self.assertIn("JS跳转跟随", example)       # 壳页跟随


if __name__ == "__main__":
    unittest.main(verbosity=2)
