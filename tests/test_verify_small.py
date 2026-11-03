#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_small_department 严谨核实严格化回归测试 (2026-09-01)。

用户反馈「假阳性, 复核逻辑需要改进。提高门槛, 严格化, 对网站更多的网页结构
进行分析评估」——三类历史误报形态:
  T1 教学组织静态走查元素太少即确认(电院: CSV2 走查3 误确认)
  T2 壳/渲染失败页 0 元素与 CSV≈0 假自洽误确认(碳中和发展研究院)
  T3 前端API(JSON下拉/AjaxFragment)驱动页静态走查少数卡片误确认
     (区域国别研究院: 静态4元素/API 30人)

新增门槛: ①壳页不计元素 ②前端API检出即权威(大→漏采不确认) ③教学组织
static≥5元素/其他≥1, 且 csv_n<=total<=csv_n+5, 0元素绝不确认。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))
sys.path.insert(0, str(_PROJ))
sys.path.insert(0, str(_PROJ / "util"))

import eval_small  # noqa: E402

FAC = "http://x.test/szdw/jsml.htm"
_NAMES = ["张伟", "李娜", "王强", "刘洋", "陈静", "杨帆",
          "赵磊", "黄敏", "周杰", "吴倩", "徐刚", "孙丽"]


def _rows(csv_names):
    """构造 CSV 行: [0]院系名 [3]职称 [6]师资页URL。"""
    return [["测试院系", "", "", "教授", "", "", ""]] + [
        ["测试院系", "", "", "教授", "", "", FAC] for _ in csv_names]


def _page(names, extra=""):
    """静态师资名单页: 详情URL命中文档型 /info/数字 才被 teacher_name_set 计入。"""
    links = "".join(f'<a href="info/1015/{i}.htm">{n}</a>'
                    for i, n in enumerate(names, 1))
    body = (links + extra + "<div style='display:none'>" + "填充" * 300 + "</div>")
    return f"<html><body>{body}</body></html>"


def _fetch_side(urls):
    return lambda u: urls.get(u, "")


def _verify(csv_names, fac_html, dept_type=""):
    with patch.object(eval_small, "fetch", side_effect=_fetch_side({FAC: fac_html})), \
         patch.object(eval_small, "find_faculty_page", return_value=""):
        return eval_small.verify_small_department(None, _rows(csv_names), dept_type)


class TestTeachingOrgMinElements(unittest.TestCase):
    """T1: 教学组织(学院/学部/系)静态走查 <5 元素不得确认(电院假阳性)。"""

    def test_csv2_walked3_not_confirmed(self):
        v = _verify(["张伟", "李娜"], _page(_NAMES[:3]), dept_type="学院")
        self.assertFalse(v["confirmed"])
        self.assertIn("低于确认门槛5", v["evidence"])

    def test_csv10_walked12_confirmed(self):
        v = _verify(_NAMES[:10], _page(_NAMES[:12]), dept_type="学院")
        self.assertTrue(v["confirmed"])

    def test_csv2_walked5_confirmed(self):
        v = _verify(["张伟", "李娜"], _page(_NAMES[:5]), dept_type="学部")
        self.assertTrue(v["confirmed"])


class TestShellPageGate(unittest.TestCase):
    """T2: 壳/渲染失败页不计元素 — 0元素绝不确认(碳中和发展教训)。"""

    def test_shell_page_not_confirmed(self):
        shell = "<html><body>网站正在加载中" + "<p>loading</p>" * 200 + "</body></html>"
        v = _verify(["张伟"], shell, dept_type="研究院")
        self.assertFalse(v["confirmed"])
        self.assertIn("低质量", v["evidence"])

    def test_zero_elements_not_confirmed(self):
        # 普通页面但无教师级元素(新闻站/错误页形态)
        blank = "<html><body><a href='news/1.htm'>学院新闻</a>" + "<p>x</p>" * 200 + "</body></html>"
        v = _verify(["张伟"], blank, dept_type="研究院")
        self.assertFalse(v["confirmed"])
        self.assertIn("未找到教师级元素", v["evidence"])


class TestFacultyApiAuthority(unittest.TestCase):
    """T3: 前端API(JSON下拉/AjaxFragment)检出即权威(2026-09-01)。"""

    def _api(self, n):
        return [["王老师%d" % i, "教授", f"http://x.test/faculty/detail/{i}", ""]
                for i in range(1, n + 1)]

    def test_api_big_roster_not_confirmed(self):
        with patch("util.faculty_api.detect_faculty_api", return_value={"kind": "json"}):
            with patch("util.faculty_api.fetch_faculty_api", return_value=self._api(30)):
                v = _verify(["张伟", "李娜"], _page(_NAMES[:3]), dept_type="学院")
        self.assertFalse(v["confirmed"])
        self.assertIn("API名单30人", v["evidence"])

    def test_api_small_roster_authoritative_confirmed(self):
        with patch("util.faculty_api.detect_faculty_api", return_value={"kind": "html"}):
            with patch("util.faculty_api.fetch_faculty_api", return_value=self._api(5)):
                v = _verify(["张伟", "李娜", "王强", "刘洋"], _page([]), dept_type="学院")
        self.assertTrue(v["confirmed"])

    def test_api_roster_far_smaller_than_csv_not_confirmed(self):
        # API名单远小于CSV → 端点非全量名单, 静态走查不可信 → 不确认
        with patch("util.faculty_api.detect_faculty_api", return_value={"kind": "json"}):
            with patch("util.faculty_api.fetch_faculty_api", return_value=self._api(5)):
                v = _verify(_NAMES[:12], _page(_NAMES[:3]), dept_type="学院")
        self.assertFalse(v["confirmed"])
        self.assertIn("端点非全量名单", v["evidence"])

    def test_api_endpoint_but_fetch_failed_not_confirmed(self):
        # 检出端点但拉取失败(<3人): JS驱动页, 静态走查不可信 → 不确认
        with patch("util.faculty_api.detect_faculty_api", return_value={"kind": "json"}):
            with patch("util.faculty_api.fetch_faculty_api", return_value=[]):
                v = _verify(["张伟"], _page(_NAMES[:2]), dept_type="学院")
        self.assertFalse(v["confirmed"])
        self.assertIn("名单拉取失败", v["evidence"])

    def test_no_api_static_path_unchanged(self):
        with patch("util.faculty_api.detect_faculty_api", return_value=None):
            v = _verify(_NAMES[:10], _page(_NAMES[:12]), dept_type="学院")
        self.assertTrue(v["confirmed"])


class TestNonTeachingSmallOrg(unittest.TestCase):
    """研究院/中心/实验室: 走查≥1且与CSV一致可确认。"""

    def test_two_person_lab_confirmed(self):
        v = _verify(["张伟", "李娜"], _page(["张伟", "李娜"]), dept_type="研究院")
        self.assertTrue(v["confirmed"])

    def test_walked_fewer_than_csv_not_confirmed(self):
        # 走查比CSV还少 = 没走到真名单页 → 不确认(差≤5 的负数方向补洞)
        v = _verify(_NAMES[:5], _page(_NAMES[:2]), dept_type="研究院")
        self.assertFalse(v["confirmed"])

    def test_walked_more_than_csv_plus5_not_confirmed(self):
        # 走查比CSV多5以上 = 漏采 → 不确认
        v = _verify(["张伟"], _page(_NAMES[:8]), dept_type="研究院")
        self.assertFalse(v["confirmed"])


if __name__ == "__main__":
    unittest.main()
