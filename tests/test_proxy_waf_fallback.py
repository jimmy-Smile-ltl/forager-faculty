#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R12 回归测试: 降级链 直连优先 → 代理兜底 + 域名双向记忆。

第3轮教训 (2026-08-23, 兰大/南师大 5/5 抓取失败):
- 快代理出口是数据中心 IP, 瑞数 WAF 直接 412 拦截(JS 挑战页);
- 整条降级链带同一代理 → 全链白跑 → 全部 fetch_failed;
- 实测: 代理 412 / 直连(本地宽带 IP) 200 → 应直连重试一次并缓存域名。

2026-08-24 链反转 (用户指令): 未知域名 本地直连优先(DIRECT_TIMEOUT 快超时)
→ 代理兜底(重试≤2) → 直连兜底。双向记忆: 直连成功 → _DIRECT_OK_HOSTS
(同域永久直连); 代理成功 → _PROXY_OK_HOSTS(同域直接走代理); 直连连续失败
DIRECT_FAIL_LIMIT 次 → 该域名自主转代理优先。

2026-08-25: 测试 URL 全部改用保留域名(.test, RFC 2606 永不解析), 不指向
真实/虚构的学校站点。

运行: cd "college teacher" && uv run python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "teachers"))
sys.path.insert(0, str(_PROJ / "util"))

import http_client  # noqa: E402
import drission_pool  # noqa: E402  2026-09-02 池从 smart_fetch 抽出
import smart_fetch  # noqa: E402
from http_client import (  # noqa: E402
    UnifiedClient, _DIRECT_OK_HOSTS, _PROXY_OK_HOSTS, _DIRECT_FAILS,
    _RUISHU_BLOCK_CODES, _host_of_url)


_PROXY = {"http": "http://u:p@proxy.test:1", "https": "http://u:p@proxy.test:1"}


def _client():
    """fetch_text 降级链除 curl 快路径外全部 Mock — 失败用例不得落进
    真实 RsSpider(30s+) / smart_fetch / 窗口池, 否则单测跑数分钟。"""
    c = object.__new__(UnifiedClient)
    c.proxies = dict(_PROXY)
    c.last_status = 0
    c._curl_requests = Mock()
    c._session = Mock()
    c._session.headers = {}
    c._session.get = Mock(return_value=_r(412, _RUISHU_HTML))  # requests 兜底层
    spider = Mock()
    spider.get = Mock(return_value=_r(412, _RUISHU_HTML))      # RsSpider 层
    c._get_rs_spider = Mock(return_value=spider)
    return c


def _fetch(c, url):
    """走 fetch_text 但短路 smart_fetch 四级降级 (该层在本测试范围外)"""
    with patch("smart_fetch.smart_fetch", return_value=(None, "")):
        return c.fetch_text(url, timeout=10)


def _r(status, text="", content=b""):
    m = Mock()
    m.status_code = status
    m.text = text
    m.content = content
    return m


_RUISHU_HTML = "<html>" + "$_ts.nsd" + "=123;document.cookie=..." + "</html>" * 30
_OK_HTML = "x" * 600
_OK_BIN = b"%PDF-1.4" + b"x" * 300


class TestFetchTextWafFallback(unittest.TestCase):
    """直连优先: 未知域名先本地直连, 失败切代理; 代理成功记 _PROXY_OK_HOSTS"""

    def setUp(self):
        _DIRECT_OK_HOSTS.clear()
        _PROXY_OK_HOSTS.clear()
        _DIRECT_FAILS.clear()

    def test_direct_412_then_proxy_200_memories_proxy(self):
        """直连 412(瑞数挑战) → 代理 200: 记代理成功, 第二次同域直走代理"""
        c = _client()
        c._curl_requests.get.side_effect = [_r(412, _RUISHU_HTML), _r(200, _OK_HTML)]
        html = _fetch(c, "http://waf.test/a.htm")
        self.assertEqual(html, _OK_HTML)
        # 第二次调用是代理(proxies=self.proxies), 不是直连
        self.assertIsNotNone(c._curl_requests.get.call_args_list[1].kwargs["proxies"])
        self.assertEqual(c.last_status, 200)
        self.assertIn("waf.test", _PROXY_OK_HOSTS)
        self.assertNotIn("waf.test", _DIRECT_OK_HOSTS)

    def test_cached_host_skips_proxy(self):
        _DIRECT_OK_HOSTS.add("waf.test")
        c = _client()
        c._curl_requests.get.side_effect = [_r(200, _OK_HTML)]
        html = _fetch(c, "http://waf.test/other.htm")
        self.assertEqual(html, _OK_HTML)
        self.assertIsNone(c._curl_requests.get.call_args.kwargs["proxies"])
        # 缓存命中时不应出现 412 → 直连的两段式
        self.assertEqual(c._curl_requests.get.call_count, 1)

    def test_proxy_412_direct_also_fails_no_cache(self):
        c = _client()
        c._curl_requests.get.side_effect = [_r(412, _RUISHU_HTML), _r(412, _RUISHU_HTML)]
        html = _fetch(c, "http://waf.test/a.htm")
        self.assertIsNone(html)
        self.assertNotIn("waf.test", _DIRECT_OK_HOSTS)

    def test_404_not_cached_no_proxy_retry(self):
        """2026-09-28 死链早退: 业务 4xx 不是 WAF 拦截 → 直连 404 直接 return,
        不缓存、不白烧代理/直连兜底降级链（地大北京 scholarDetail 旧路径、
        中南老系统实测 3 后端结果一致）。总调用 1 次。"""
        c = _client()
        c._curl_requests.get.side_effect = [_r(404, "<html>not found</html>")]
        html = _fetch(c, "http://waf.test/a.htm")
        self.assertIsNone(html)
        self.assertNotIn("waf.test", _DIRECT_OK_HOSTS)
        self.assertNotIn("waf.test", _PROXY_OK_HOSTS)
        self.assertEqual(c._curl_requests.get.call_count, 1)

    def test_no_proxy_direct_412_no_cache(self):
        """代理未启用 → 直连 412 后兜底段再试一次仍失败, 不缓存域名"""
        c = _client()
        c.proxies = None
        c._curl_requests.get.side_effect = [_r(412, _RUISHU_HTML), _r(412, _RUISHU_HTML)]
        html = _fetch(c, "http://waf.test/a.htm")
        self.assertIsNone(html)
        self.assertNotIn("waf.test", _DIRECT_OK_HOSTS)
        self.assertEqual(c._curl_requests.get.call_count, 2)

    def test_download_binary_412_falls_back(self):
        c = _client()
        c._curl_requests.get.side_effect = [_r(412, _RUISHU_HTML), _r(200, content=_OK_BIN)]
        c._atomic_write = Mock()
        ok = c.download_binary("http://waf.test/a.pdf", Path(tempfile.mkdtemp()) / "a.pdf", timeout=10)
        self.assertTrue(ok)
        self.assertIn("waf.test", _DIRECT_OK_HOSTS)


class TestSmartFetchWafFallback(unittest.TestCase):
    """smart_fetch._try_curl_cffi 同款直连优先链, 与 http_client 共享缓存"""

    def setUp(self):
        _DIRECT_OK_HOSTS.clear()
        _PROXY_OK_HOSTS.clear()
        _DIRECT_FAILS.clear()

    def test_direct_412_then_proxy_200_memories_proxy(self):
        """直连 412 → 代理 200: 记 _PROXY_OK_HOSTS(代理成功), 不记直连。
        2026-09-15 起 _try_curl_cffi 开头有无条件 AES 壳预探测 GET;
        直连段 2026-09-04 起走 fingerprint_chain(内部独立 curl_cffi 引用,
        patch smart_fetch._cffi 拦不到)——须 patch fingerprint_chain。
        _cffi.get 调用序: [AES探测412, 代理段200]。"""
        with patch("util.fingerprint_chain.try_impersonations",
                   return_value=(None, None)), \
             patch("smart_fetch._cffi.get",
                   side_effect=[_r(412, _RUISHU_HTML), _r(200, _OK_HTML)]), \
             patch("smart_fetch._PROXIES", _PROXY):
            html, method = smart_fetch._try_curl_cffi("http://waf.test/a.htm", 10)
        self.assertEqual(html, _OK_HTML)
        self.assertEqual(method, "curl_cffi")
        self.assertIn("waf.test", _PROXY_OK_HOSTS)
        self.assertNotIn("waf.test", _DIRECT_OK_HOSTS)

    def test_shared_cache_with_http_client(self):
        """http_client 直连成功后 smart_fetch 也应直接直连 (共享 _DIRECT_OK_HOSTS)。
        AES 壳预探测 1 次 + 直连段 1 次 = side_effect 2 次。"""
        _DIRECT_OK_HOSTS.add("waf.test")
        with patch("smart_fetch._cffi.get",
                   side_effect=[_r(200, _OK_HTML), _r(200, _OK_HTML)]) as mocked_get, \
             patch("smart_fetch._PROXIES", _PROXY):
            html, _ = smart_fetch._try_curl_cffi("http://waf.test/a.htm", 10)
        self.assertEqual(html, _OK_HTML)
        self.assertIsNone(mocked_get.call_args.kwargs["proxies"])


class TestHostUtil(unittest.TestCase):
    def test_host_of_url(self):
        self.assertEqual(_host_of_url("http://waf.test/a.htm"), "waf.test")
        self.assertEqual(_host_of_url("https://a.b.test:8080/x"), "a.b.test")
        self.assertEqual(_host_of_url("不是URL"), "")

    def test_block_codes_set(self):
        self.assertEqual(_RUISHU_BLOCK_CODES, frozenset({202, 412, 452}))


class TestDrissionPortPidOffset(unittest.TestCase):
    """P1-4: 窗口池端口按进程 PID 偏移——ProcessPool 各 worker 端口互不
    重叠, 不再跨进程互杀（第3轮 R12 从属问题, 2026-08-23 修复）。"""

    def test_port_base_has_pid_offset(self):
        base = drission_pool._DRISSION_PORT_BASE
        pid_mod = int(__import__("os").getpid()) % 1000
        # 偏移粒度 = 窗口上限: 相邻 PID 的端口块也不重叠
        self.assertEqual(base, 9340 + pid_mod * drission_pool._MAX_WINDOWS)

    def test_windows_ports_within_range(self):
        base = drission_pool._DRISSION_PORT_BASE
        # 窗口端口 = base + 0..2 (窗口池上限 3)
        ports = range(base, base + 3)
        self.assertLess(base, 65535)
        # 避开 CDP 桥接 9222 与独立调试 9333
        self.assertNotIn(9222, ports)
        self.assertNotIn(9333, ports)

    def test_different_pids_no_overlap(self):
        """不同 PID 的 worker 端口块互不重叠（偏移粒度 = 窗口上限;
        仅 PID%1000 相同才重叠, 概率 1/1000, 可接受）"""
        w = drission_pool._MAX_WINDOWS
        for p1, p2 in ((12345, 34567), (10001, 10002), (93401, 93402)):
            b1 = 9340 + (p1 % 1000) * w
            b2 = 9340 + (p2 % 1000) * w
            self.assertTrue(
                set(range(b1, b1 + w)).isdisjoint(range(b2, b2 + w)) or b1 == b2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
