#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_test_loop（Step 3 测试-评估-改进循环）纯逻辑单元测试。
不碰网络/LLM：候选院系列表、报告渲染、导航词/模式落地、改进日志。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))

import batch_test_loop as bt  # noqa: E402


class TestListDeptDirs(unittest.TestCase):
    def test_only_dirs_with_artifacts(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "苏州大学" / "传媒学院").mkdir(parents=True)
        (tmp / "苏州大学" / "传媒学院" / "teachers.csv").write_text("a\n", encoding="utf-8")
        (tmp / "苏州大学" / "物理学院").mkdir(parents=True)
        (tmp / "苏州大学" / "物理学院" / "spider.py").write_text("x", encoding="utf-8")
        (tmp / "苏州大学" / "空目录").mkdir(parents=True)
        (tmp / "苏州大学" / "_隐藏").mkdir(parents=True)
        with mock.patch.object(bt, "OUT", tmp):
            depts = bt.list_dept_dirs("苏州大学")
        self.assertEqual(depts, ["传媒学院", "物理学院"])


class TestRenderReport(unittest.TestCase):
    def test_output_contains_results_and_issues(self):
        results = [
            {"dept": "传媒学院", "status": "PASS", "total": 42, "url_ok": 40, "reasons": []},
            {"dept": "物理学院", "status": "FAIL", "total": 8, "url_ok": 0,
             "reasons": ["详情URL全空", "数量偏少"]},
        ]
        retro = {
            "issues": [{"dimension": "提取", "problem": "名字粘连", "evidence": "物理学院"}],
            "nav_words": ["教师风采"], "patterns": [{"name": "卡片式", "structure": "li.doctor-item",
                                                     "extract": "卡片内a"}],
            "suggestions": [{"target": "prompt", "action": "加锚点证据"}],
        }
        md = bt.render_report("苏州大学", 1, results, retro)
        self.assertIn("# 第 1 批测试回顾 — 苏州大学", md)
        self.assertIn("传媒学院", md)
        self.assertIn("物理学院", md)
        self.assertIn("名字粘连", md)
        self.assertIn("教师风采", md)
        self.assertIn("卡片式", md)
        self.assertIn("加锚点证据", md)


class TestApplyNavWords(unittest.TestCase):
    def test_merge_and_dedupe(self):
        tmp = Path(tempfile.mkdtemp()) / "spider_template.py"
        tmp.write_text("NAV_WORDS = {'首页', '师资'}\n", encoding="utf-8")
        with mock.patch.object(bt, "NAV_WORDS_FILE", tmp):
            n = bt.apply_nav_words(["教师风采", "首页"])  # 首页 已在源码 → 跳过
        self.assertEqual(n, 1)
        src = tmp.read_text(encoding="utf-8")
        self.assertIn("教师风采", src)
        self.assertIn("NAV_WORDS.update", src)

    def test_empty_returns_zero(self):
        tmp = Path(tempfile.mkdtemp()) / "spider_template.py"
        tmp.write_text("NAV_WORDS = {}\n", encoding="utf-8")
        with mock.patch.object(bt, "NAV_WORDS_FILE", tmp):
            self.assertEqual(bt.apply_nav_words([]), 0)


class TestApplyPatterns(unittest.TestCase):
    def test_patterns_saved_to_kb(self):
        kb_path = Path(tempfile.mkdtemp()) / "teacher_patterns.json"
        # 2026-08-31 拆分后 load_kb/save_kb 实现在 collect_kb, 补丁目标随之更新
        with mock.patch("collect_kb.KB_PATH", kb_path):
            n = bt.apply_patterns([{"name": "卡片式", "structure": "li.doctor-item",
                                    "extract": "卡片内a"}])
            self.assertEqual(n, 1)
            # 断言必须在 patch 上下文内(KB_PATH 才指向 tmp)
            from collect_teacher import load_kb
            self.assertEqual(load_kb()["page_structure_patterns"]["卡片式"]["structure"],
                             "li.doctor-item")
            # 重复沉淀 → 不重复加
            self.assertEqual(bt.apply_patterns([{"name": "卡片式", "structure": "x"}]),
                             0)


class TestAppendImprovementsLog(unittest.TestCase):
    def test_appends_line(self):
        tmp = Path(tempfile.mkdtemp()) / "improvements.md"
        with mock.patch.object(bt, "IMPROVE_LOG", tmp):
            bt.append_improvements_log("苏州大学", 1,
                                       {"issues": [{"dimension": "评估", "problem": "盲区"}],
                                        "nav_words": ["a"], "patterns": []},
                                       "batch_001.md")
            bt.append_improvements_log("苏州大学", 2,
                                       {"issues": [], "nav_words": [], "patterns": []},
                                       "batch_002.md")
        text = tmp.read_text(encoding="utf-8")
        self.assertIn("# Step 3 Agent 改进日志", text)
        self.assertIn("第1批(苏州大学)", text)
        self.assertIn("第2批(苏州大学)", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
