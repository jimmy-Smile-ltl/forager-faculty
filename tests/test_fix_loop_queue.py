#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_loop 失败队列 + evaluate_teachers LLM 终审缓存 单元测试。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))

import fix_loop  # noqa: E402
import fix_queue  # noqa: E402
import evaluate_teachers  # noqa: E402


class TestFailQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "_fix_loop_queue.json"

    def test_add_remove_clear(self):
        # 2026-08-31 拆分后队列实现落在 fix_queue, 补丁目标随之更新
        with mock.patch.object(fix_queue, "FAIL_QUEUE_PATH", self.tmp):
            fix_loop.add_to_queue("苏州大学", "计算机学院", "http://x.test/")
            fix_loop.add_to_queue("苏州大学", "物理学院", "http://y.test/")
            fix_loop.add_to_queue("安徽大学", "化学学院", "http://z.test/")
            q = fix_loop.load_fail_queue()
            self.assertEqual(set(q["苏州大学"].keys()), {"计算机学院", "物理学院"})
            self.assertIn("化学学院", q["安徽大学"])

            fix_loop.remove_from_queue("苏州大学", "计算机学院")
            q = fix_loop.load_fail_queue()
            self.assertNotIn("计算机学院", q["苏州大学"])

            fix_loop.clear_fail_queue()
            self.assertEqual(fix_loop.load_fail_queue(), {})

    def test_remove_last_school_drops_key(self):
        with mock.patch.object(fix_queue, "FAIL_QUEUE_PATH", self.tmp):
            fix_loop.add_to_queue("苏州大学", "物理学院", "http://y.test/")
            fix_loop.remove_from_queue("苏州大学", "物理学院")
            self.assertNotIn("苏州大学", fix_loop.load_fail_queue())


class TestLlmCache(unittest.TestCase):
    def test_save_load_roundtrip(self):
        tmp = Path(tempfile.mkdtemp()) / ".llm_name_cache.json"
        with mock.patch.object(evaluate_teachers, "_LLM_CACHE_PATH", tmp):
            evaluate_teachers.save_llm_cache({"苏州大学/计算机学院": {"张三": True, "李四": False}})
            cache = evaluate_teachers.load_llm_cache()
            self.assertEqual(cache["苏州大学/计算机学院"]["张三"], True)
            self.assertEqual(cache["苏州大学/计算机学院"]["李四"], False)

    def test_missing_file_returns_empty(self):
        tmp = Path(tempfile.mkdtemp()) / "nonexistent.json"
        with mock.patch.object(evaluate_teachers, "_LLM_CACHE_PATH", tmp):
            self.assertEqual(evaluate_teachers.load_llm_cache(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
