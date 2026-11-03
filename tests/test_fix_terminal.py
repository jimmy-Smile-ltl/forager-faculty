#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_loop 3轮终态分流单元测试 (2026-08-31 用户指令)。

评估→改进最多3轮, 3轮未通过即终态不再入队无限重试:
  no_data_skip   = 确实没有数据(页面不可达/无师资页) → .skip_mark
  fail_extraction = 有数据但提取失败(选择器/提取规则错误) → .fail_mark
分析文档 analysis.md 始终保留, 结论写 test_report.json。

运行: cd "college teacher" && uv run python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))
sys.path.insert(0, str(_PROJ / "department teachers" / "templates"))

from fix_loop import _classify_terminal  # noqa: E402

_HTML = "<html><body>" + "y" * 2000 + "</body></html>"


class TestClassifyTerminal(unittest.TestCase):
    """_classify_terminal(fac_html, reasons) -> no_data_skip | fail_extraction"""

    def test_empty_or_tiny_page_is_skip(self):
        # 页面没抓到/过小 = 无法确定有数据 → skip
        self.assertEqual(_classify_terminal("", ["URL缺失率100%"]), "no_data_skip")
        self.assertEqual(_classify_terminal(None, []), "no_data_skip")
        self.assertEqual(_classify_terminal("x" * 100, []), "no_data_skip")

    def test_no_data_hints_are_skip(self):
        for r in ("页面不可达", "未发现师资页", "no_faculty_page", "连接超时",
                  "DNS解析失败", "404"):
            self.assertEqual(_classify_terminal(_HTML, [r]), "no_data_skip", r)

    def test_extraction_failures_are_fail(self):
        # 有数据但提取失败: URL缺失/漏采/非人名混入 — 选择器或规则错误
        for r in ("URL缺失率85%", "详情URL全空", "数量硬规则: 页面52元素 vs CSV 4人",
                  "非人名混入 12个", "机构页URL占比过高"):
            self.assertEqual(_classify_terminal(_HTML, [r]), "fail_extraction", r)

    def test_empty_reasons_are_fail(self):
        # 页面可抓但没有任何评估原因(异常吞没等) → 保守归 fail(有数据侧)
        self.assertEqual(_classify_terminal(_HTML, []), "fail_extraction")

    def test_no_data_hint_wins_over_extraction(self):
        # 混合原因: 只要出现无数据信号(页面确实没数据) → skip
        self.assertEqual(_classify_terminal(_HTML, ["URL缺失率90%", "无师资页"]),
                         "no_data_skip")


if __name__ == "__main__":
    unittest.main()
