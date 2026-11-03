#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 总闸 / 学院亲和 / degraded 退避 / 全局节流 回归测试（2026-08-25）。

借鉴 department teachers 范式应用（concurrency_playbook.md §三, 用户批准全量）:
- P0: begin_task 学院亲和 — 院系内连续 ask 固定 token 命中网关 prompt 缓存;
      跨批次/重启稳定; 多学院并发按院分散负载
- P1-1: _LLM_SEM 全局总闸 — 进程内单一共享实例(动态容量), 防多模块信号量
      叠加打爆网关(department 侧"三处 Semaphore(4)=12 路"教训)
- P1-2: degraded/空响应 3s/8s 退避 + 坏缓存时间戳扰动 + shutdown(wait=False)
      (wait=True 会等卡死的 SSL read 永远结束)
- P1-3: _throttle 进程级全局 — 跨 scraper 实例共享节流门, 并发 N 校时
      请求速率不叠加成 N×rate_limit

运行: cd "college teacher" && uv run python -m unittest discover -s tests -v
"""
import inspect
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "teachers"))
sys.path.insert(0, str(_PROJ / "util"))

import detail_scraper as ds  # noqa: E402
from detail_scraper import (  # noqa: E402
    DetailScraper, _LLMDynamicSem, _LLM_SEM, _NEXT_REQUEST_AT,
    _THROTTLE_LOCK, _init_llm_sem_cap)


def _make_scraper(llm=None):
    """绕过 __init__ 的最小 DetailScraper（启发式路径无 LLM 时安全）"""
    sc = object.__new__(DetailScraper)
    sc.school_name = "测试大学"
    sc.dept_name = "测试学院"
    sc.resume = False
    sc.rate_limit = 0.0
    sc._llm = llm
    sc._llm_blackout_until = 0.0
    sc._llm_consecutive_timeouts = 0
    sc._render_blackout = False
    sc._render_empty_streak = 0
    sc._log_lines = []
    sc._log_lock = threading.Lock()
    sc._log = lambda *a, **k: None
    return sc


class TestBeginTaskCollegeAffinity(unittest.TestCase):
    """P0: 学院亲和 — begin_task 以学院名调用 SimpleLLM, 固定线程到 hash 槽位"""

    def test_begin_task_calls_llm_with_school_name(self):
        llm = Mock()
        sc = _make_scraper(llm)
        sc.begin_task()
        llm.begin_task.assert_called_once_with("测试大学")

    def test_begin_task_no_llm_is_noop(self):
        sc = _make_scraper(None)  # 启发式模式
        sc.begin_task()  # 不抛

    def test_process_dept_injects_begin_task(self):
        """process_dept 创建 scraper 后必须调用 begin_task（任务边界）"""
        sys.path.insert(0, str(_PROJ / "teachers"))
        import extract_all_details as ead
        with patch("detail_scraper.DetailScraper") as m_cls, \
             patch("extract_all_details.save_state"):
            inst = m_cls.return_value
            inst.begin_task = Mock()
            inst.process_batch = Mock(return_value=[])
            inst.save_results = Mock()
            ead.process_dept("测试大学", "测试学院", [], rate_limit=0.0)
        inst.begin_task.assert_called_once_with()
        inst.process_batch.assert_called_once_with([], workers=1, skip_done=False)

    def test_process_dept_pass_kwargs(self):
        """begin_task 必须在 process_batch 之前调用（任务开始即固定）"""
        sys.path.insert(0, str(_PROJ / "teachers"))
        import extract_all_details as ead
        order = []
        with patch("detail_scraper.DetailScraper") as m_cls, \
             patch("extract_all_details.save_state"):
            inst = m_cls.return_value
            inst.begin_task = Mock(side_effect=lambda: order.append("begin"))
            inst.process_batch = Mock(
                side_effect=lambda *a, **k: order.append("batch") or [])
            inst.save_results = Mock()
            ead.process_dept("测试大学", "测试学院", [], rate_limit=0.0)
        self.assertEqual(order, ["begin", "batch"])


class TestLLMDynamicSem(unittest.TestCase):
    """P1-1: 全局 LLM 总闸 — 动态容量信号量语义"""

    def test_sem_caps_concurrency(self):
        sem = _LLMDynamicSem(2)
        sem.acquire()
        sem.acquire()
        # 第 3 个 acquire 必须阻塞（容量 2）
        got = threading.Event()

        def _try():
            sem.acquire()
            got.set()

        t = threading.Thread(target=_try)
        t.start()
        t.join(0.2)
        self.assertFalse(got.is_set())   # 阻塞中
        sem.release()                    # 让出 1 槽
        t.join(1.0)
        self.assertTrue(got.is_set())    # 放行
        sem.release()
        sem.release()

    def test_set_cap_widens(self):
        sem = _LLMDynamicSem(1)
        sem.acquire()
        sem.set_cap(2)
        sem.acquire()  # 扩容后立即可用
        sem.release()
        sem.release()

    def test_global_sem_is_module_singleton(self):
        self.assertIs(ds._LLM_SEM, _LLM_SEM)      # 模块级单例
        self.assertIsInstance(_LLM_SEM, _LLMDynamicSem)

    def test_manual_env_override(self):
        with patch.dict("os.environ", {"LLM_SEM": "5"}, clear=False):
            cap, auto = _init_llm_sem_cap()
        self.assertEqual(cap, 5)
        self.assertFalse(auto)                     # 手动模式不跟随池

    def test_auto_mode_bounds(self):
        with patch.dict("os.environ", {}, clear=False):
            with patch.dict("os.environ", {"LLM_SEM": ""}):
                cap, auto = _init_llm_sem_cap()
        self.assertTrue(auto)
        self.assertGreaterEqual(cap, 2)
        self.assertLessEqual(cap, ds.LLM_CONCURRENCY_CAP)


class TestAskLlmDegradedRetry(unittest.TestCase):
    """P1-2: degraded/空响应退避重试 + 坏缓存扰动 + 熔断语义"""

    def test_degraded_then_success_retries(self):
        llm = Mock()
        llm.ask.side_effect = ['{"error": "degraded"}', '{"职称": "教授"}']
        sc = _make_scraper(llm)
        with patch.object(ds.time, "sleep") as m_sleep:  # 跳过真实退避 sleep
            r = sc._ask_llm("sys", "p")
        self.assertEqual(r, '{"职称": "教授"}')
        self.assertEqual(llm.ask.call_count, 2)
        m_sleep.assert_called_once_with(3)

    def test_empty_then_success(self):
        llm = Mock()
        llm.ask.side_effect = ["", '{"职称": "教授"}']
        sc = _make_scraper(llm)
        with patch.object(ds.time, "sleep"):
            r = sc._ask_llm("sys", "p")
        self.assertEqual(r, '{"职称": "教授"}')
        self.assertEqual(llm.ask.call_count, 2)

    def test_all_bad_falls_back_none_with_perturbation(self):
        """2 次退避全 bad → 时间戳扰动重试 → 仍 bad → None（调用方走启发式）"""
        llm = Mock()
        llm.ask.side_effect = ["", '{"error": "degraded"}', '{"error": "x"}']
        sc = _make_scraper(llm)
        with patch.object(ds.time, "sleep"):
            r = sc._ask_llm("sys", "p")
        self.assertIsNone(r)
        self.assertEqual(llm.ask.call_count, 3)
        # 第 3 次带扰动（坏缓存秒回空时强制真实生成, fix_loop 教训）
        self.assertIn("[调用扰动", llm.ask.call_args_list[2][0][1])

    def test_timeout_counts_and_returns_none(self):
        """ask 挂起 → fut.result(timeout) 抛 TimeoutError → 计数+1。
        注意: TimeoutError 不能经 side_effect 真实 sleep 模拟 —— patch
        ds.time.sleep 会连测试模块的 time（同一模块对象）一起打掉; 直接用
        side_effect 抛 builtin TimeoutError（3.11+ 与
        concurrent.futures.TimeoutError 同类型）, fut.result 立即重抛。"""
        llm = Mock()
        llm.ask.side_effect = TimeoutError
        sc = _make_scraper(llm)
        with patch.object(ds, "LLM_TIMEOUT", 0.05):
            with patch.object(ds.time, "sleep"):
                r = sc._ask_llm("sys", "p")
        self.assertIsNone(r)
        self.assertEqual(sc._llm_consecutive_timeouts, 3)  # 循环 2 次 + 扰动 1 次
        self.assertEqual(llm.ask.call_count, 3)

    def test_blackout_after_three_timeouts(self):
        llm = Mock()
        llm.ask.side_effect = TimeoutError
        sc = _make_scraper(llm)
        sc._llm_consecutive_timeouts = 2  # 预置: 本轮再 1 次超时 → 3
        with patch.object(ds, "LLM_TIMEOUT", 0.05):
            with patch.object(ds.time, "sleep"):
                sc._ask_llm("sys", "p")
        self.assertGreater(sc._llm_blackout_until, time.time())
        # 熔断期 _ask_llm 内 3 次调用已发生（熔断作用于后续 _extract_fields_llm）
        self.assertEqual(llm.ask.call_count, 3)
        # 熔断期内 _extract_fields_llm 快速失败, ask 不再增加
        with patch.object(ds, "LLM_TIMEOUT", 0.05):
            out = sc._extract_fields_llm("x", "http://x.test/")
        self.assertEqual(out, {})
        self.assertEqual(llm.ask.call_count, 3)

    def test_extract_fields_llm_uses_ask_llm_success(self):
        """成功路径: 单次正常响应 → 14 字段解析（2026-08-26 新增 attr 兜底字段）"""
        llm = Mock()
        llm.ask.side_effect = [
            '{"职称": "教授", "联系方式": "a@b.c", "研究方向": "AI", '
            '"attr": {"兼职": "中国数学会理事"}}',
        ]
        sc = _make_scraper(llm)
        out = sc._extract_fields_llm("正文", "http://x.test/")
        self.assertEqual(out["职称"], "教授")
        self.assertEqual(out["联系方式"], "a@b.c")
        # attr 兜底字段: dict 原样保留
        self.assertEqual(out["attr"], {"兼职": "中国数学会理事"})
        # 其余字段补空
        self.assertEqual(len(out), 14)
        self.assertEqual(out["职务"], "")

    def test_extract_fields_llm_attr_defaults_empty_dict(self):
        """LLM 未返回 attr 时, 兜底字段必须初始化为空 dict 而非字符串。"""
        llm = Mock()
        llm.ask.side_effect = ['{"职称": "副教授"}']
        sc = _make_scraper(llm)
        out = sc._extract_fields_llm("正文", "http://x.test/")
        self.assertEqual(out["attr"], {})
        self.assertIsInstance(out["attr"], dict)


class TestCallTimeoutShutdown(unittest.TestCase):
    """P1-2: shutdown(wait=False) 源码哨兵 — 防回归 with 块（wait=True 卡死）"""

    def test_shutdown_wait_false_sentinel(self):
        src = inspect.getsource(DetailScraper._call_llm_with_timeout)
        self.assertIn("shutdown(wait=False, cancel_futures=True)", src)
        self.assertNotIn("with _cf.ThreadPoolExecutor", src)

    def test_ask_llm_uses_global_sem(self):
        src = inspect.getsource(DetailScraper._ask_llm)
        self.assertIn("with _LLM_SEM", src)      # 全局总闸接线
        self.assertIn("[调用扰动", src)           # 坏缓存扰动


class TestThrottleGlobal(unittest.TestCase):
    """P1-3: 节流门进程级全局 — 跨实例共享, 速率不叠加"""

    def tearDown(self):
        ds._NEXT_REQUEST_AT = 0.0  # 恢复模块全局, 不影响其他测试

    def test_throttle_shared_across_instances(self):
        """A 实例节流后 B 实例必须等待（共享模块级时间戳）"""
        a = _make_scraper(None)
        b = _make_scraper(None)
        a.rate_limit = 0.25
        b.rate_limit = 0.25
        a._throttle()  # t=0.25
        t0 = time.monotonic()
        b._throttle()  # 必须等到 ≥0.25
        self.assertGreaterEqual(time.monotonic() - t0, 0.2)

    def test_global_variables_are_module_level(self):
        self.assertIsInstance(_THROTTLE_LOCK, threading.Lock)
        self.assertEqual(_NEXT_REQUEST_AT, 0.0)

    def test_throttle_source_uses_global(self):
        src = inspect.getsource(DetailScraper._throttle)
        self.assertIn("global _NEXT_REQUEST_AT", src)
        self.assertIn("_THROTTLE_LOCK", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
