#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""教师提取新形态单元测试 (2026-08-31 清华样本发现的三类形态)。

P1 嵌套子元素姓名(清华集成电路 jyry.htm): <a> 文本为混合长串, 姓名在子元素内
P3 邮箱名单纯文本(清华哲学系 js.htm): 姓名行 + 邮箱/研究室行, 无职称行
导航剥离: 导航锚点链接("近期焦点")不得被当教师
"""
import sys
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))
sys.path.insert(0, str(_PROJ))
sys.path.insert(0, str(_PROJ / "util"))

from util.teacher_extract import extract_teachers  # noqa: E402


class TestNestedChildName(unittest.TestCase):
    """P1: <a> 混合长文本, 姓名在子元素(带"姓名："前缀)"""

    def test_nested_div_p_name(self):
        html = """<html><body>
          <a href="../info/1015/2267.htm">
            <div>姓名：白恩慧</div><span>姓名：</span>
            <p>白恩慧</p><div>研究所：集成电路制造研究所</div>
          </a>
        </body></html>"""
        ts = extract_teachers(html, "http://x.test/szdw/jyry.htm")
        names = [t[0] for t in ts]
        self.assertIn("白恩慧", names)
        self.assertEqual(ts[0][2], "http://x.test/info/1015/2267.htm")

    def test_label_division_not_name(self):
        # 研究所/学院 子元素不得被当姓名(全匹配拒绝长文本)
        html = """<html><body>
          <a href="../info/1/1.htm"><div>研究所：集成电路制造研究所</div></a>
        </body></html>"""
        ts = extract_teachers(html, "http://x.test/a.htm")
        self.assertEqual(ts, [])


class TestEmailPureText(unittest.TestCase):
    """P3: 纯文本姓名 + 邮箱/研究室行(无职称行)"""

    def test_name_plus_contact_pair(self):
        html = """<html><body>
          <div>陈某某</div><div>邮箱: chenxm@163.com 研究室：人文楼110</div>
          <div>李某某</div><div>邮箱: lixm@mail.example.edu.cn</div>
        </body></html>"""
        ts = extract_teachers(html, "http://x.test/js.htm")
        names = [t[0] for t in ts]
        self.assertIn("陈某某", names)
        self.assertIn("李某某", names)
        # 详情URL为空(纯文本名单)
        self.assertTrue(all(t[2] == "" for t in ts))


class TestNavExclusion(unittest.TestCase):
    """2026-08-31: 导航锚点链接不得被当教师(清华哲学系"近期焦点")"""

    def test_nav_anchor_not_extracted(self):
        html = """<html><body>
          <nav><a href="../../xydt.htm">近期焦点</a><a href="../../index.htm">首页</a></nav>
          <div>陈某某</div><div>邮箱: b@163.com</div>
        </body></html>"""
        ts = extract_teachers(html, "http://x.test/js.htm")
        names = [t[0] for t in ts]
        self.assertNotIn("近期焦点", names)
        self.assertIn("陈某某", names)


class TestLetterIndexPagination(unittest.TestCase):
    """2026-08-31 姓名首字母索引分页(西交数学 jsml/axmszmjs/A-Z.htm)"""

    def test_letter_pages_followed(self):
        from util.teacher_extract import pager_pages
        # 西交数学真实形态: 字母页在 jsml/axmszmjs/X.htm (相对师资页的 jsml/ 前缀)
        html = """<html><body>
          <a href="jsml/axmszmjs/A.htm">A</a>
          <a href="jsml/axmszmjs/B.htm">B</a>
          <a href="jsml/axmszmjs/C.htm">C</a>
          <a href="../index.htm">首页</a>
        </body></html>"""
        pages = pager_pages("http://x.test/szdw/jsml.htm", html)
        self.assertIn("http://x.test/szdw/jsml/axmszmjs/A.htm", pages)
        self.assertIn("http://x.test/szdw/jsml/axmszmjs/C.htm", pages)
        self.assertNotIn("http://x.test/index.htm", pages)  # 同目录外不跟随

    def test_letter_link_outside_dir_ignored(self):
        from util.teacher_extract import pager_pages
        html = """<html><body><a href="/other/B.htm">B</a></body></html>"""
        pages = pager_pages("http://x.test/szdw/jsml.htm", html)
        self.assertEqual(pages, ["http://x.test/szdw/jsml.htm"])


if __name__ == "__main__":
    unittest.main()


class TestFacultyApiDetect(unittest.TestCase):
    """2026-09-01 前端 AJAX/JSON 下拉加载适配(上交 airs/shss)"""

    def test_detect_json_infinite_scroll(self):
        from util.faculty_api import detect_faculty_api
        html = '''
        <script>window.__FACULTY_PAGE__ = {
            searchUrl: "https:\\/\\/airs.sjtu.edu.cn\\/faculty\\/search\\/byxz",
            detailUrlBase: "https:\\/\\/airs.sjtu.edu.cn\\/faculty\\/detail"};</script>
        <script>var limit = 12; var params = { offset: nextPage, limit: limit };
        $.getJSON(searchUrl, params)</script>'''
        cfg = detect_faculty_api(html, "https://airs.sjtu.edu.cn/faculty/byxz")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["kind"], "json")
        self.assertEqual(cfg["url"], "https://airs.sjtu.edu.cn/faculty/search/byxz")
        self.assertEqual(cfg["page_param"], "offset")
        self.assertIn("detail_base", cfg)

    def test_detect_ajax_html_fragment(self):
        from util.faculty_api import detect_faculty_api
        html = '''
        $.ajax({ url: "/Web/AjaxFaculty", type: "get",
                 data: { t: 1, d: 2, title: "教授", time: 0 }})'''
        cfg = detect_faculty_api(html, "https://shss.sjtu.edu.cn/Faculty/1-2")
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["kind"], "html")
        self.assertEqual(cfg["url"], "https://shss.sjtu.edu.cn/Web/AjaxFaculty")
        self.assertEqual(cfg["params"].get("t"), "1")
        # 最优路径: title 全部(空), 不职称逐个请求
        self.assertNotIn("title", cfg["params"])

    def test_no_api_returns_none(self):
        from util.faculty_api import detect_faculty_api
        self.assertIsNone(detect_faculty_api("<html><body>静态名单</body></html>",
                                             "http://x.test/a.htm"))
