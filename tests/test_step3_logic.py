#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 3 (department teachers) 纯逻辑单元测试 — 无网络、无 LLM。

运行:
  cd "college teacher" && uv run python -m unittest discover -s tests -v
或(本会话无 uv 环境时):
  PYTHONPATH=/tmp/testdeps python3 -m unittest tests.test_step3_logic -v
"""
import sys
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))
sys.path.insert(0, str(_PROJ / "teachers"))

from faculty_discovery import (  # noqa: E402
    FacultyDiscovery, clean_name, is_valid_name, _is_detail_url,
)
from extract_all_details import _is_person_name  # noqa: E402


class TestCleanName(unittest.TestCase):
    """名字与职称粘连拆解（faculty_discovery.clean_name）。"""

    def test_plain_name(self):
        self.assertEqual(clean_name("陈招斌"), ("陈招斌", ""))

    def test_name_with_space_title(self):
        self.assertEqual(clean_name("陈招斌 高级工程师"), ("陈招斌", "高级工程师"))

    def test_truncated_title_glue(self):
        # Step 3 截断粘连: 职称只截到前缀字
        # 2026-10-24 一致性优化: 单字前缀剥离彻底移除(收益全库仅 2 个
        # 可疑样本, 成本=误杀几百个真名)——单字尾名字一律保真, 判定
        # 不依赖 title 字段; 双字前缀(实验)仍剥
        self.assertEqual(clean_name("陈招斌高"), ("陈招斌高", ""))
        self.assertEqual(clean_name("李纯高"), ("李纯高", ""))
        self.assertEqual(clean_name("方雪明实"), ("方雪明实", ""))
        self.assertEqual(clean_name("潘蕊实验"), ("潘蕊", ""))

    def test_full_title_glue(self):
        # 完整职称直接粘连在名字尾部
        self.assertEqual(clean_name("林敏教授级高级实验师"), ("林敏", "教授级高级实验师"))
        self.assertEqual(clean_name("张三副教授"), ("张三", "副教授"))

    def test_fullwidth_space_inside_name(self):
        # 名字内部全角空格归一
        self.assertEqual(clean_name("汤　儆 教授"), ("汤儆", "教授"))
        self.assertEqual(clean_name("汤\xa0儆 教授"), ("汤儆", "教授"))

    def test_glue_must_leave_valid_name(self):
        # 剥离后必须剩 2-4 个纯汉字，否则不剥离
        r = clean_name("王教授")
        # "教授"剥离后剩 1 字 → 不剥离（名字本身可能就是 王教授? 保留原样）
        self.assertEqual(r[0], "王教授")

    def test_label_suffix_glue(self):
        # 真实案例教训(已化名): 名单页提取把姓名与下一列"职称"标签粘连
        # （"教师甲职称"/"教师乙职"）
        self.assertEqual(clean_name("包某红职"), ("包某红", ""))
        self.assertEqual(clean_name("曹某职称"), ("曹某", ""))
        self.assertEqual(clean_name("陈某理职"), ("陈某理", ""))
        self.assertEqual(clean_name("付某职称"), ("付某", ""))
        # 真实名字不受影响
        self.assertEqual(clean_name("范韦里克"), ("范韦里克", ""))
        self.assertEqual(clean_name("韩思成"), ("韩思成", ""))


class TestPersonNameFilter(unittest.TestCase):
    """人名过滤词表回归（_NON_PERSON_KEYWORDS 多轮扩充的教训锁死）。"""

    def test_title_words_rejected(self):
        # 吉大植物科学学院: "教授/副教授/讲师/知名专家" 是分类导航链接
        for n in ("教授", "副教授", "讲师", "知名专家", "名师"):
            self.assertFalse(_is_person_name(n, "https://zky.test/szdw1/js.htm"), n)

    def test_school_and_nav_words_rejected(self):
        # 吉大软件学院/上外国际教育学院教训
        for n in ("浙江大学", "国际交流", "网站首页", "网上学堂", "地图",
                  "慕课", "校历", "校友"):
            self.assertFalse(_is_person_name(n, "http://x/y.htm"), n)

    def test_real_names_pass(self):
        for n in ("张艳莉", "朱建军", "杨四平", "吴春相"):
            self.assertTrue(_is_person_name(n, "https://x.test/jszy/a.htm"), n)


class TestIsValidName(unittest.TestCase):
    def test_valid(self):
        for n in ("张三", "欧阳锋", "欧阳娜娜"):
            self.assertTrue(is_valid_name(n), n)

    def test_invalid(self):
        for n in ("", "李", "A3", "张三李四王五", "abc", "张3", "张·"):
            self.assertFalse(is_valid_name(n), n)


class TestIsDetailUrl(unittest.TestCase):
    def test_detail(self):
        for u in ("https://x.test/info/1234/5678.htm",
                  "https://x.test/c123a456/",
                  "https://x.test/c123456/",   # 十六进制 CMS 形式(与其它 DETAIL_HINT 对齐)
                  "https://x.test/2023/teacher.htm"):
            self.assertTrue(_is_detail_url(u), u)

    def test_not_detail(self):
        for u in ("https://x.test/szdw/zzjs.htm",
                  "https://x.test/list.htm",
                  "https://x.test/index.htm"):
            self.assertFalse(_is_detail_url(u), u)


class TestMerge(unittest.TestCase):
    def test_dedupe_by_url_and_clean_name(self):
        step3 = [
            {"name": "陈招斌高", "detail_url": "http://x/info/1"},
            {"name": "李四", "detail_url": "http://x/info/2"},
        ]
        discovered = [
            {"name": "陈招斌", "title": "高级工程师", "detail_url": "http://x/info/1"},  # 同URL → 只补职称
            {"name": "王五", "title": "教授", "detail_url": "http://x/info/3"},          # 新URL → 新增
        ]
        merged = FacultyDiscovery.merge(step3, discovered)
        by_url = {t["detail_url"]: t for t in merged}
        self.assertEqual(len(merged), 3)
        # 2026-10-23: 名字保真(陈招斌高 不剥高), discovered 的 title 仍补上
        self.assertEqual(by_url["http://x/info/1"]["name"], "陈招斌高")
        self.assertEqual(by_url["http://x/info/1"]["title"], "高级工程师")
        self.assertIn("http://x/info/3", by_url)


class TestExtractFromList(unittest.TestCase):
    HTML = """<html><body>
      <div class="content">
        <a href="/info/101/1001.htm">张三</a>
        <a href="/info/101/1002.htm">李四 副教授</a>
        <a href="/info/101/1003.htm">王五教授</a>
        <a href="/szdw/zzjs.htm">在职教师</a>
        <a href="/info/101/1004.htm">首页</a>
        <a href="javascript:void(0)">赵六</a>
      </div>
    </body></html>"""

    def test_extract(self):
        fd = FacultyDiscovery(scraper=None)
        teachers = fd.extract_teachers_from_list(self.HTML, "https://x.test/szdw.htm")
        names = [t["name"] for t in teachers]
        self.assertEqual(names, ["张三", "李四", "王五"])  # 导航词/JS链接被排除
        self.assertEqual(teachers[1]["title"], "副教授")
        self.assertTrue(teachers[0]["detail_url"].startswith("http"))


class TestExtractFromText(unittest.TestCase):
    def test_name_title_adjacent_lines(self):
        html = """<html><body><div class="content">
          <p>林敏教授级高级实验师</p>
          <p>吕鑫|教授</p>
          <p>员张剑荣</p>  <!-- 结构词开头 → 排除 -->
          <p>教务处</p>
        </div></body></html>"""
        fd = FacultyDiscovery(scraper=None)
        teachers = fd.extract_teachers_from_text(html)
        names = {t["name"] for t in teachers}
        self.assertIn("林敏", names)
        self.assertIn("吕鑫", names)
        self.assertNotIn("张剑荣", names)   # '员' 结构词开头
        self.assertNotIn("教务处", names)   # 无职称词不成对


class TestIsPersonName(unittest.TestCase):
    """Step 4 人名过滤（extract_all_details._is_person_name）。"""

    def test_person(self):
        self.assertTrue(_is_person_name("张三"))
        self.assertTrue(_is_person_name("欧阳锋"))

    def test_not_person(self):
        self.assertFalse(_is_person_name(""))
        self.assertFalse(_is_person_name("教务处"))
        self.assertFalse(_is_person_name("党建工作"))
        self.assertFalse(_is_person_name("abc"))
        self.assertFalse(_is_person_name("李"))          # 1 字
        self.assertFalse(_is_person_name("李四", "https://x/list.htm"))  # 导航页URL
        self.assertFalse(_is_person_name("张三", "https://x/list"))      # list 结尾


class TestEvaluateTeachersRegex(unittest.TestCase):
    """评估器正则行为。

    2026-10-25 修: 存在两个同名 evaluate_teachers(teachers/ 新版 analyze_dept
    与 department teachers/ 旧版 evaluate_dept, fix_loop 依赖后者)——sys.path
    顺序不可靠。本类直测正则真实定义处: NAME_RE/FOREIGN_RE/count_teacher_elements
    在 department teachers/eval_elements.py(旧版 evaluate_teachers re-export);
    SUBJECT_HINT/GLUE_HINT 单源在 util.teacher_extract(SUBJECT_RE/GLUE_RE)。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        _ee = _PROJ / "department teachers" / "eval_elements.py"
        spec = importlib.util.spec_from_file_location("eval_elements_dt", _ee)
        cls.ee = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ee)
        from util.teacher_extract import SUBJECT_RE, GLUE_RE
        cls.SUBJECT_HINT = SUBJECT_RE
        cls.GLUE_HINT = GLUE_RE

    def test_name_re(self):
        # NAME_RE 只校验"2-4 个汉字"; 组织词过滤由 SUBJECT_HINT/GLUE_HINT/NAV_WORDS 承担
        self.assertTrue(self.ee.NAME_RE.match("张三"))
        self.assertTrue(self.ee.NAME_RE.match("欧阳娜娜"))
        self.assertTrue(self.ee.NAME_RE.match("教务处"))
        self.assertFalse(self.ee.NAME_RE.match("A3"))

    def test_foreign_re(self):
        self.assertTrue(self.ee.FOREIGN_RE.match("John Smith"))
        self.assertTrue(self.ee.FOREIGN_RE.match("Ana Maria"))
        self.assertFalse(self.ee.FOREIGN_RE.match("张三"))

    def test_subject_hint(self):
        # 专业方向/栏目词: match 整词语义（与 util.teacher_extract.clean_name
        # 一致）。组合词(音乐理论/学科建设)由 GLUE_HINT 拦截或放行,
        # SUBJECT_HINT 只杀"整词即词表词"
        for s in ("建设", "理论", "教育", "研究", "管理"):
            self.assertTrue(self.SUBJECT_HINT.match(s), s)
        # 组合词不以整词命中词表 → 不误伤
        self.assertFalse(self.SUBJECT_HINT.match("学科建设"))
        self.assertFalse(self.SUBJECT_HINT.match("音乐理论"))

    def test_glue_hint(self):
        # 名字粘连栏目词(2-3字名 + 栏目后缀)
        for s in ("车某技术", "陈招斌职称", "张三中心", "李四学院"):
            self.assertTrue(self.GLUE_HINT.match(s), s)

    def test_count_teacher_elements(self):
        # 占位名须避开 NAV_WORDS 真名污染(张三/李四在词表中, 属独立复核清单)
        html = """<html><body>
          <a href="/info/1/1.htm">赵一</a>
          <a href="/info/1/2.htm">钱二</a>
          <a href="/szdw/zzjs.htm">在职教师</a>
          <a href="/index.htm">首页</a>
          <div class="pic-item"><a href="/info/1/3.htm">孙三</a></div>
        </body></html>"""
        n, clickable = self.ee.count_teacher_elements(html, "https://x.test/")
        self.assertEqual(n, 3)          # 3 个详情URL链接 + 1 卡片
        self.assertEqual(clickable, 3)


class TestFixLoopTemplate(unittest.TestCase):
    """fix_loop 确定性模板渲染产物（回归保护 H2/M1 修复）。"""

    def test_template_renders_and_has_anchor_evidence(self):
        # 2026-10-25 修: fix_loop 顶层 import evaluate_dept 来自同目录旧版
        # evaluate_teachers——测试进程 sys.path[0] 是 teachers(新版无
        # evaluate_dept)。import fix_loop 前把 department teachers 提到最前
        import sys as _sys
        _sys.path.insert(0, str(_PROJ / "department teachers"))
        import fix_loop
        # 2026-08-26 起模板占位符为 fac_urls/faculty_api(多入口/XHR接口),
        # 走 generate_spider 渲染(与 R0 实际路径一致)
        code = fix_loop.generate_spider(
            "测试大学", "测试学院", "https://x.test/", "https://x.test/szdw.htm",
        ).replace("__FETCH__", "def fetch(url): return None")
        compile(code, "<generated>", "exec")
        # 5 元组（含师资页 pg）→ 统一写盘入口（anchor 证据字段已移入 util 层）
        self.assertIn("teachers.append([t[0], t[1], t[2], t[3], pg])", code)
        self.assertIn("write_outputs(SCHOOL, DEPT, teachers)", code)
        self.assertIn("got = extract_teachers_multi(html, pg)", code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
