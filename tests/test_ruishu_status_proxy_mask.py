#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第3轮抽样测试回归: 瑞数/风控状态码归因 + 代理凭证日志脱敏。

第3轮教训（2026-08-23, 8校抽样）:
- 兰大/西交/南师大 3 校 score=0 "异常: HTTP 412/452" —— 瑞数/风控拦截码
  被 HttpStatusError 当"URL 失效"确定性快速失败, 整院系异常。
- 5 个 spider_run.log 泄漏代理账号密码 —— http_client 打印完整 PROXY_URL。

运行: cd "college teacher" && uv run python -m unittest discover -s tests -v
"""
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "teachers"))
sys.path.insert(0, str(_PROJ / "util"))

from detail_scraper import (  # noqa: E402
    DetailScraper, HttpStatusError, _RUISHU_STATUS, classify_record)
from http_client import _mask_proxy_url, is_ruishu_challenge, ok_html  # noqa: E402


def _make_scraper(last_status: int):
    """构造一个绕过 __init__ 的 DetailScraper, 只挂 fetch_page 所需属性。"""
    import threading
    sc = object.__new__(DetailScraper)
    sc.rate_limit = 0.0
    sc._throttle_lock = threading.Lock()
    sc._next_request_at = 0.0
    sc._http = Mock()
    sc._http.last_status = last_status
    sc._http.fetch_text.return_value = None
    sc._log = lambda *a, **k: None
    return sc


def _patch_dns():
    """避免测试环境真实 DNS 解析（沙箱无网时 gaierror 会提前 return None）;
    2026-10-25 补 TCP 预检 mock——fetch_page 2026-09-28 新增 TCP 探测
    (socket.create_connection 3s 超时), 不 mock 会在真实连接超时后提前
    return None, 走不到 HttpStatusError 分支（404/500 测试挂 3s 假失败）。"""
    import contextlib
    stack = contextlib.ExitStack()
    stack.enter_context(patch(
        "socket.getaddrinfo",
        return_value=[(socket.AF_INET, 1, 6, "", ("93.184.216.34", 80))]))
    stack.enter_context(patch(
        "detail_scraper.socket.create_connection", return_value=Mock()))
    return stack


class TestRuishuStatusNotDeterministic(unittest.TestCase):
    """412/202/452 是反爬拦截, 不是 URL 失效 → fetch_page 必须返回 None,
    不得抛 HttpStatusError 让整院系异常（第3轮: 兰大/西交/南师大 score=0）。"""

    def test_412_not_raised(self):
        for code in (202, 412, 452):
            with self.subTest(code=code):
                sc = _make_scraper(code)
                with _patch_dns():
                    self.assertIsNone(sc.fetch_page("https://example.test/x.htm"))

    def test_404_still_raised(self):
        """404 仍是 URL 失效 → 确定性快速失败（北大哲学系教训, 不能回归）"""
        sc = _make_scraper(404)
        with _patch_dns():
            with self.assertRaises(HttpStatusError) as cm:
                sc.fetch_page("https://example.test/x.htm")
        self.assertEqual(cm.exception.status, 404)

    def test_500_still_raised(self):
        sc = _make_scraper(500)
        with _patch_dns():
            with self.assertRaises(HttpStatusError):
                sc.fetch_page("https://example.test/x.htm")

    def test_ruishu_status_set_contents(self):
        self.assertEqual(_RUISHU_STATUS, frozenset({202, 412, 452}))


class TestProxyUrlMasked(unittest.TestCase):
    """代理 URL 日志脱敏: user:pass 不得进日志（第3轮 5 个 spider_run.log
    泄漏快代理账号密码）。"""

    def test_credentials_stripped(self):
        masked = _mask_proxy_url("http://t123:passwd@tunnel.example-proxy.com:15818/")
        self.assertNotIn("t123", masked)
        self.assertNotIn("passwd", masked)
        self.assertEqual(masked, "http://tunnel.example-proxy.com:15818/")

    def test_no_credentials_unchanged(self):
        self.assertEqual(_mask_proxy_url("http://127.0.0.1:7890"),
                         "http://127.0.0.1:7890")

    def test_garbage_unchanged(self):
        self.assertEqual(_mask_proxy_url(""), "")
        self.assertEqual(_mask_proxy_url("不是URL"), "不是URL")


class TestInvalidUrlRejected(unittest.TestCase):
    """名单 URL 非 http → 提前判死, 不发起网络请求, 归因明确。
    （第3轮: 中南国家卓越工程师学院 410 人 URL 全为院系名"国家卓越工程师学院"）"""

    def _scraper(self):
        sc = object.__new__(DetailScraper)
        sc.resume = False
        sc._log = lambda *a, **k: None
        return sc

    def test_invalid_url_fetch_failed_with_reason(self):
        sc = self._scraper()
        r = sc.process_teacher({"name": "曹雁冰", "detail_url": "国家卓越工程师学院"})
        self.assertEqual(r["来源类型"], "fetch_failed")
        self.assertIn("名单URL无效", r["失败原因"])
        self.assertEqual(classify_record(r), "failed")

    def test_http_url_not_rejected(self):
        """有效 http URL 不得误伤: 走正常 fetch 路径(失败原因=fetch 类,
        不是"名单URL无效")——校验分支只拦非 http"""
        import tempfile
        sc = object.__new__(DetailScraper)
        sc.resume = False
        sc._log = lambda *a, **k: None
        tmp = Path(tempfile.mkdtemp())
        sc._raw_dir = tmp
        sc.raw_data_root = tmp.parent
        sc._make_filename = Mock(return_value="x.html")
        sc._pdf_file_ok = Mock(return_value=False)
        sc._retry = Mock(return_value=(None, 0, "fetch: mock失败"))
        r = sc.process_teacher({"name": "张三", "detail_url": "https://x.test/a.htm"})
        self.assertEqual(r["来源类型"], "fetch_failed")
        self.assertIn("fetch", r["失败原因"])
        self.assertNotIn("名单URL无效", r["失败原因"])


class TestRuishuChallengeNeedsRealTitle(unittest.TestCase):
    """2026-09-03 北邮 teacher.bupt.edu.cn 教训回归: 瑞数保护站解开后的
    真实页面(48KB, 含 <title>) 头部也引用 $_ts.nsd —— 仅凭标记出现即滤
    会把整站内容当挑战页丢弃(北邮卓越工程师学院 177 教师全失败)。
    判别升级: 挑战页纯混淆 JS 无真实 <title>, 解开页有真实 <title>。"""

    _CHALLENGE = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<script type="text/javascript" r=\'m\'>$_ts=window[\'$_ts\'];'
        'if(!$_ts)$_ts={};$_ts.nsd=129835;$_ts.cd="qEpxrrAlDaGqo..."'
        + "x" * 400
        + "</script>"
        "</head><body></body></html>"
    )
    _SOLVED = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<script>$_ts.nsd=1;/* 页面自带瑞数 JS 引用 */</script>'
        "<title>北京邮电大学主页平台管理系统 章洋--中文主页--首页</title>"
        "</head><body><p>真实正文内容</p>"
        + "正文内容" * 120
        + "</body></html>"
    )

    def test_challenge_page_without_title_is_challenge(self):
        self.assertTrue(is_ruishu_challenge(self._CHALLENGE))

    def test_solved_page_with_title_is_not_challenge(self):
        self.assertFalse(is_ruishu_challenge(self._SOLVED))

    def test_solved_page_passes_ok_html(self):
        self.assertIsNotNone(ok_html(self._SOLVED))

    def test_no_marker_page_not_challenge(self):
        self.assertFalse(is_ruishu_challenge(
            "<html><head><title>普通页</title></head><body>内容</body></html>"))

    def test_short_page_not_challenge(self):
        self.assertFalse(is_ruishu_challenge("<html></html>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
