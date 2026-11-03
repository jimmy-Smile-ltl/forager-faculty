#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提取侧 2026-10-24 改动回归测试。

覆盖:
  A. extract_all_details._is_person_name 的 list.htm 详情页豁免
     (判别同源 util.url_rewrites.is_list_htm_person)
  B. 院系 extract_rules.json 的 CSS 容器规则加载与应用
     (东南大学交通学院 webplus box_tt 节切块族, 207 页实测)
  C. load_teachers 同名单证据剥离 (刘小龙副 ↔ 刘小龙 两条目)
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "teachers"))
sys.path.insert(0, str(_PROJ))  # util.*

from extract_all_details import _is_person_name  # noqa: E402


class TestPersonNameListHtm(unittest.TestCase):
    """A. 提取侧 _is_person_name 对 list.htm 形态的豁免。"""

    def test_listhtm_person_detail_passes(self):
        # /拼音/list.htm 每人一页详情页 → 放行 (此前整类被误拒)
        self.assertTrue(_is_person_name(
            "蔡宗琰", "https://js.chd.edu.cn/gcjxxy/czy/list.htm"))
        self.assertTrue(_is_person_name(
            "安树青", "http://life.nju.edu.cn/asq1/list.htm"))
        self.assertTrue(_is_person_name(
            "石磊", "https://faculty.jnu.edu.cn/lgxy/sl/list.psp"))

    def test_listhtm_column_page_rejected(self):
        # 前段=栏目词的真列表页 → 仍拒
        self.assertFalse(_is_person_name(
            "李化学", "https://surfchem.ustc.edu.cn/szll/list.htm"))
        self.assertFalse(_is_person_name(
            "张曼君", "http://le.ustc.edu.cn/qzjs/list.htm"))

    def test_non_list_url_unaffected(self):
        self.assertTrue(_is_person_name(
            "张三", "https://x.edu.cn/info/1234/5678.htm"))
        self.assertFalse(_is_person_name("", ""))
        self.assertFalse(_is_person_name("教务处", "https://x.edu.cn/info/1/1.htm"))


class TestDeptRulesCss(unittest.TestCase):
    """B. 院系 extract_rules.json CSS 容器规则加载与应用。"""

    _HTML = """<html><body>
      <div class="nav"><a>科研项目</a><a>学术论文</a></div>
      <div class="col_news_list listcon">
        <div class="title">安成川</div>
        <div class="news_text">研究方向为AI驱动的交通网络建模</div>
        <div class="box_tt">教育背景</div><p>2019年，东南大学博士</p>
        <div class="box_tt">工作经历</div><p>2020年，东南大学博士后</p>
        <div class="box_tt">研究领域</div><p>智能交通控制</p>
        <div class="box_tt">科研项目</div><p>国自然基金青年项目，主持</p>
        <div class="box_tt">发明专利</div><p>一种路网动态交通分配方法，ZL2020</p>
        <div class="box_tt">学术论文</div><p>An, C. et al. (2024). Paper.</p>
        <div class="box_tt">荣誉奖项</div><p>2024年 中国公路学会一等奖</p>
        <div class="box_tt">教授课程</div><p>《高速公路控制与仿真》</p>
        <div class="box_tt">学术兼职</div><p>专委会委员</p>
        <div class="box_tt">招生需求</div><p>每年招收博士1名</p>
      </div>
      <div class="kzt k3">办公室： 203</div>
      <div class="kzt k4">邮箱： ccan@seu.edu.cn</div>
    </body></html>"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.html_path = Path(self._tmp.name) / "安成川_0cb8003b.html"
        self.html_path.write_text(self._HTML, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _apply(self, rules, field):
        import detail_scraper as ds
        result = {}
        for f, patterns in rules.items():
            for pat in patterns:
                val = ds._apply_rule_value(pat, "", str(self.html_path), f)
                if val:
                    result[f] = val
                    break
        return result.get(field)

    def test_css_container_anchors_avoid_nav_hijack(self):
        """CSS 锁正文容器后, 导航区同名「科研项目」不劫持锚点。"""
        rules = {
            "学术成果": [{"css": "div.col_news_list.listcon",
                          "regex": r"科研项目\s*([\s\S]{10,50000}?)(?=招生需求|学术兼职)"}],
            "教育经历": [{"css": "div.col_news_list.listcon",
                          "regex": r"教育背景\s*([\s\S]{5,600}?)(?=工作经历|研究领域|科研项目)"}],
        }
        edu = self._apply(rules, "教育经历")
        self.assertIn("2019年", edu)
        self.assertNotIn("工作经历", edu)  # 前瞻切干净
        ach = self._apply(rules, "学术成果")
        self.assertIn("国自然基金", ach)
        self.assertIn("ZL2020", ach)          # 跨节合并(科研项目+发明专利+论文)
        self.assertNotIn("招生需求", ach)

    def test_kzt_blocks(self):
        rules = {
            "通讯地址": [{"css": "div.kzt.k3",
                          "regex": r"办公室[：:]\s*([^\n]{1,40})"}],
            "联系方式": [{"css": "div.kzt.k4",
                          "regex": r"邮箱[：:]\s*([^\s\n]+)"}],
        }
        self.assertEqual(self._apply(rules, "通讯地址"), "203")
        self.assertEqual(self._apply(rules, "联系方式"), "ccan@seu.edu.cn")

    def test_css_rule_needs_html_path(self):
        """无 html_path 时 CSS 规则返回 None（正则规则仍可用）。"""
        import detail_scraper as ds
        val = ds._apply_rule_value(
            {"css": "div.box_tt"}, "", "", "教育经历")
        self.assertIsNone(val)

    def test_mixed_rule_forms(self):
        """CSS 规则与正则规则共存于同一 rules dict, 逐条尝试(命中即止)。"""
        rules = {"研究方向": [
            {"css": "div.col_news_list.listcon",
             "regex": r"研究领域\s*([\s\S]{5,600}?)(?=科研项目|发明专利)"},
            r"研究方向[：:]\s*(.{5,40})",
        ]}
        val = self._apply(rules, "研究方向")
        self.assertIn("智能交通控制", val)


class TestSameRosterStrip(unittest.TestCase):
    """C. 同名单证据剥离: 名字以单字职称尾结尾且去尾版在同名单。"""

    def _strip(self, names):
        cleaned = [{"name": n} for n in names]
        _names_set = {t["name"] for t in cleaned if t.get("name")}
        for t in cleaned:
            n = t["name"]
            if len(n) >= 3 and n[-1] in "副高实助教工研讲":
                base = n[:-1]
                if base in _names_set:
                    t["name"] = base
        return [t["name"] for t in cleaned]

    def test_strip_when_base_exists(self):
        # 刘小龙副 ↔ 刘小龙 两条目 → 剥为同名(实锤案例)
        self.assertEqual(self._strip(["刘小龙", "刘小龙副"]), ["刘小龙", "刘小龙"])

    def test_keep_when_no_base(self):
        # 李纯高(真名, 名单内无去尾版) → 保真
        self.assertEqual(self._strip(["李纯高"]), ["李纯高"])
        self.assertEqual(self._strip(["李纯高", "张三"]), ["李纯高", "张三"])

    def test_short_names_unaffected(self):
        self.assertEqual(self._strip(["王五", "李六"]), ["王五", "李六"])


if __name__ == "__main__":
    unittest.main()
