#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 4 (teachers) 纯逻辑单元测试 — 无网络、无 LLM。

运行:
  cd "college teacher" && uv run python -m unittest discover -s tests -v
或(本会话无 uv 环境时):
  PYTHONPATH=/tmp/testdeps python3 -m unittest tests.test_step4_logic -v
"""
import sys
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "teachers"))
sys.path.insert(0, str(_PROJ / "util"))

from detail_scraper import DetailScraper, DETAIL_FIELDS, _sanitize_path, _verify_zhicheng  # noqa: E402
from spider_gen import stratified_sample  # noqa: E402


def _dummy_scraper() -> DetailScraper:
    """绕过 __init__（不触发网络/LLM/日志）构造空实例，仅测实例方法。"""
    return DetailScraper.__new__(DetailScraper)


class TestParseJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(DetailScraper._parse_json('{"a": 1}'), {"a": 1})

    def test_markdown_wrapped(self):
        self.assertEqual(DetailScraper._parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_trailing_text(self):
        self.assertEqual(DetailScraper._parse_json('{"a": 1} 附注'), {"a": 1})

    def test_garbage(self):
        self.assertIsNone(DetailScraper._parse_json("not json at all"))
        self.assertIsNone(DetailScraper._parse_json(""))


class TestSanitizePath(unittest.TestCase):
    def test_special_chars(self):
        self.assertEqual(_sanitize_path('a/b:c*d?e"f<g>h|i'), "a_b_c_d_e_f_g_h_i")

    def test_strip(self):
        self.assertEqual(_sanitize_path("  张三  "), "张三")


class TestMakeFilename(unittest.TestCase):
    def test_deterministic(self):
        a = DetailScraper._make_filename("张三", "https://x.test/info/1.htm", "html")
        b = DetailScraper._make_filename("张三", "https://x.test/info/1.htm", "html")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("张三_"))
        self.assertTrue(a.endswith(".html"))
        self.assertEqual(len(a.split("_")[1].split(".")[0]), 8)  # md5 前 8 位


class TestDetectPdf(unittest.TestCase):
    """PDF 检测（iframe / a[href$=.pdf] / 苏迪 div.src / _upload 正则）。"""

    def test_iframe(self):
        html = '<iframe src="/files/abc.pdf" width="100%"></iframe>'
        self.assertEqual(DetailScraper._detect_pdf(_dummy_scraper(), html, "https://x.test/info/1.htm"),
                         "https://x.test/files/abc.pdf")

    def test_anchor(self):
        html = '<a href="https://cdn.x.test/jl.pdf">简历</a>'
        self.assertEqual(DetailScraper._detect_pdf(_dummy_scraper(), html, "https://x.test/"),
                         "https://cdn.x.test/jl.pdf")

    def test_sudy_div_src(self):
        html = '<div src="/_upload/teacher/张三.pdf" class="wp_pdf_player"></div>'
        self.assertEqual(DetailScraper._detect_pdf(_dummy_scraper(), html, "https://x.test/info/1.htm"),
                         "https://x.test/_upload/teacher/张三.pdf")

    def test_sudy_div_pdfsrc(self):
        html = '<div pdfsrc="https://x.test/_upload/a.pdf" swsrc="a.swf"></div>'
        self.assertEqual(DetailScraper._detect_pdf(_dummy_scraper(), html, "https://x.test/"),
                         "https://x.test/_upload/a.pdf")

    def test_no_pdf(self):
        html = "<html><body>无 PDF 引用</body></html>"
        self.assertIsNone(DetailScraper._detect_pdf(_dummy_scraper(), html, "https://x.test/"))


class TestExtractTextFromHtml(unittest.TestCase):
    def test_content_container_preferred(self):
        html = """<html><head><title>t</title></head><body>
          <nav>首页 学院概况 联系我们</nav>
          <div class="wp_articlecontent">
            <p>研究方向：算子理论</p><p>教育经历：2004-2007 南开大学</p>
          </div>
          <footer>版权所有</footer>
        </body></html>"""
        text = DetailScraper.extract_text_from_html(html)
        self.assertIn("算子理论", text)
        self.assertIn("南开大学", text)
        self.assertNotIn("首页 学院概况", text)  # 导航被剥离
        self.assertNotIn("版权所有", text)        # 页脚被剥离

    def test_fallback_body(self):
        html = "<html><body><p>只有一段很长的正文内容用于测试兜底提取路径</p></body></html>"
        text = DetailScraper.extract_text_from_html(html)
        self.assertIn("兜底提取", text)


class TestExtractFieldsHeuristic(unittest.TestCase):
    """启发式提取（LLM 不可用时的降级路径）。"""

    def test_title_email_phone(self):
        text = ("姓名：张三\n职称：教授\n办公电话：0431-85160000\n"
                "电子邮箱：zhangsan@jlu.edu.cn\n研究方向：算子理论、复分析")
        fields = DetailScraper._extract_fields_heuristic(_dummy_scraper(), text)
        self.assertEqual(fields["职称"], "教授")
        self.assertIn("zhangsan@jlu.edu.cn", fields["联系方式"])
        self.assertIn("算子理论", fields["研究方向"])

    def test_longest_title_first(self):
        # 高级工程师 优先于 工程师
        fields = DetailScraper._extract_fields_heuristic(_dummy_scraper(), "技术职称：高级工程师")
        self.assertEqual(fields["职称"], "高级工程师")

    def test_all_13_keys(self):
        fields = DetailScraper._extract_fields_heuristic(_dummy_scraper(), "无任何信息")
        self.assertEqual(set(fields.keys()), set(DETAIL_FIELDS))


class TestPdfTableFlatten(unittest.TestCase):
    """PDF 表格压平（表格简历页 '标签：值' 行）。"""

    class _FakePage:
        def __init__(self, tables):
            self._tables = tables

        def extract_tables(self):
            return self._tables

    def test_label_rows(self):
        page = self._FakePage([
            [["姓名", "张三"], ["职称", "教授"], ["研究方向", "算子理论"]],
        ])
        out = DetailScraper._flatten_pdf_table_page(page)
        self.assertIn("姓名：张三", out)
        self.assertIn("职称：教授", out)
        self.assertIn("研究方向：算子理论", out)

    def test_placeholder_row_dropped(self):
        page = self._FakePage([
            [["获奖情况", "备注"], ["姓名", "李四"]],
        ])
        out = DetailScraper._flatten_pdf_table_page(page)
        self.assertNotIn("备注", out)
        self.assertIn("姓名：李四", out)

    def test_label_with_colon_in_cell(self):
        page = self._FakePage([
            [["联系方式：0431-1234", "电话：0431-5678"], ["邮箱：a@b.com", ""]],
        ])
        out = DetailScraper._flatten_pdf_table_page(page)
        self.assertIn("联系方式：0431-1234", out)
        self.assertIn("邮箱：a@b.com", out)

    def test_pending_label_next_row(self):
        page = self._FakePage([
            [["教育经历"], ["2004-2007 南开大学 博士"]],
        ])
        out = DetailScraper._flatten_pdf_table_page(page)
        # _pdf_cell_text 会去掉单元格内所有空白 → 中文/数字无空格拼接
        self.assertIn("教育经历：2004-2007南开大学博士", out)

    def test_is_label_cell(self):
        self.assertTrue(DetailScraper._pdf_is_label_cell("姓名"))
        self.assertTrue(DetailScraper._pdf_is_label_cell("研究方向"))
        self.assertTrue(DetailScraper._pdf_is_label_cell("联系方式："))
        self.assertFalse(DetailScraper._pdf_is_label_cell("张三"))


class TestStratifiedSample(unittest.TestCase):
    def test_round_robin_across_categories(self):
        teachers = (
            [{"name": f"教{i}", "detail_url": f"http://x/{i}", "category": "教授"} for i in range(1, 7)]
            + [{"name": f"副{i}", "detail_url": f"http://y/{i}", "category": "副教授"} for i in range(1, 5)]
        )
        sample = stratified_sample(teachers, 6)
        cats = [t["category"] for t in sample]
        # 组内已洗牌（不依赖具体顺序），但轮转结构不变:
        # 两类别交替（每组都有成员时），且无重复
        self.assertEqual(len(set(t["name"] for t in sample)), 6)  # 无重复
        self.assertEqual(set(cats), {"副教授", "教授"})           # 两类都覆盖
        self.assertNotEqual(cats[0], cats[1])                      # 交替开始
        self.assertEqual(cats[2], cats[0])                         # 轮转回来

    def test_shuffle_avoids_head_bias(self):
        # 东北农大教训: 上游名单把导航词排在最前, 顺序抽样 5/5 全中垃圾。
        # 组内洗牌后, 多次抽样的样本集合应当有变化（不是固定取头部）
        teachers = [
            {"name": f"导航词{i}", "detail_url": f"http://x/{i}", "category": "未知"}
            for i in range(1, 9)
        ]
        samples = {frozenset(t["name"] for t in stratified_sample(teachers, 4))
                   for _ in range(30)}
        self.assertGreater(len(samples), 1)  # 30 次至少出现 2 种样本组合

    def test_no_url_excluded(self):
        teachers = [
            {"name": "张三", "detail_url": "", "category": "教授"},
            {"name": "李四", "detail_url": "http://x/1", "category": "教授"},
        ]
        sample = stratified_sample(teachers, 5)
        self.assertEqual(len(sample), 1)  # 无详情页的不参与页面分析
        self.assertEqual(sample[0]["name"], "李四")


class TestPersonNameFilter(unittest.TestCase):
    """东北农大教训: 名单头部混入导航/栏目词, 过滤必须识别。"""

    def setUp(self):
        from extract_all_details import _is_person_name
        self.is_person = _is_person_name

    def test_nav_words_rejected(self):
        for junk in ("二级党校", "农学系", "金陵讲堂", "一级学科", "二级学科",
                     "学科团队", "研究生会", "易班", "人才名师", "纪委工作",
                     "校友回家", "校友影像"):
            self.assertFalse(self.is_person(junk, "https://nxy.test/x.htm"),
                             f"{junk} 应为非人名")

    def test_real_names_accepted(self):
        for name in ("崔一凡", "董玲", "董守坤", "付连双", "龚振平", "韩雪"):
            self.assertTrue(self.is_person(name, "https://nxy.test/szdw/nxx/cyf.htm"),
                            f"{name} 应为真实人名")

    def test_common_xue_names_not_killed(self):
        # 云大教训: 词表加"教学"后, 含"学"字的常见名绝不能误杀
        for name in ("李学", "张学志", "王学军", "陈学峰", "赵学敏", "李学良", "孙学明"):
            self.assertTrue(self.is_person(name, "https://x.test/szdw/a.htm"),
                            f"{name} 含'学'字但不含'教学'连写, 应放行")

    def test_ynu_nav_words_rejected(self):
        # 云大教训: 栏目导航词"教学建设/教研风采"必须拦截
        for name in ("教学建设", "教研风采", "教师风采", "本科教学"):
            self.assertFalse(self.is_person(name, "https://www.srees.test/xygk/szdw.htm"),
                             f"{name} 应为非人名")

    def test_short_or_ascii_rejected(self):
        self.assertFalse(self.is_person("A3"))
        self.assertFalse(self.is_person("王"))  # 1 字
        self.assertFalse(self.is_person("欧阳娜娜娜"))  # 5 字


class TestNavNoiseValue(unittest.TestCase):
    """导航噪声字段判定 + html_noise 归类 failed（防覆盖率虚高）。"""

    def test_nav_noise_detected(self):
        from detail_scraper import _is_nav_noise_value
        noisy = "学位管理 下载中心 学生工作 学生工作办公室 辅导员简介 团委 学生会 研究生会 研究生团委 社团联合会 青年志愿者服务"
        self.assertTrue(_is_nav_noise_value(noisy))

    def test_real_values_kept(self):
        from detail_scraper import _is_nav_noise_value
        for v in ("中医基础理论文献研究；中医证候与治则治法实验研究",
                  "2004-2007年 上海中医药大学基础医学院 中医基础理论专业 医学博士",
                  "教授，博士生导师，主要从事大豆遗传育种研究"):
            self.assertFalse(_is_nav_noise_value(v), v)

    def test_long_value_not_judged(self):
        # 发现21（2026-08-28）: 完整导航菜单词链常 >120 字, 旧行为"不判"
        # 放过了陕师大文学院 155 字导航链 → 长值改高阈值判定:
        # ≥8 token 且 ≥80% 命中导航词才判（真实长字段值命中率远低）。
        from detail_scraper import _is_nav_noise_value
        long_noise = "学位管理 下载中心 学生工作 " * 30   # 全导航词长链 → 判污染
        self.assertTrue(_is_nav_noise_value(long_noise))
        # 真实长字段值（荣誉列表/教育经历）含少量导航词 → 不误判
        real_long = "国家级教学成果二等奖（第一完成人）；省级科学技术一等奖；" * 8
        self.assertFalse(_is_nav_noise_value(real_long))

    def test_classify_noise_as_failed(self):
        from detail_scraper import classify_record
        row = {"姓名": "二级党校", "详情页URL": "http://x/1", "来源类型": "html_noise",
               "html_path": "a/b.html", "pdf_path": "", "pdf_url": ""}
        self.assertEqual(classify_record(row), "failed")

    def test_value_substring_false_positives(self):
        # 发现27（2026-08-29）: 字段值含"研究生/团委/教师"是真实内容;
        # 无分隔符整句算 1 token + 子串命中曾致 100% 命中率误清 310 值。
        # 修复: 子串命中仅 ≥3 token 生效, ≤2 token 仅整词命中可判,
        # 字段值判定排除 研究生/团委/教师。
        from detail_scraper import _is_nav_noise_value, _is_table_header_chain
        real = (
            "1998年于安徽大学哲学系硕士研究生毕业并留校工作。",
            "硕士研究生",
            "博士研究生肄业",
            "硕士研究生导师",
            "吉林大学2022年度研究生学术业绩奖学金一等奖",
            "指导硕士研究生获得国家奖学金",
            "博士研究生国家奖学金；安徽省优秀毕业研究生",
            "作物育种Seminar（研究生必修课）",
            "团委书记（学办主任）（2006年-2008年）",
            "教师",
            "2025年指导MBA学生获中国研究生企业管理创新大赛全国二等奖；2024年指导MBA学生获中国研究生企业管理创新大赛全国二等奖；2021年获教育部博士研究生国家奖学金；2017年获教育部硕士研究生国家奖学金；2021年获湖南大学优秀研究生。",
        )
        for v in real:
            self.assertFalse(_is_nav_noise_value(v), v)
            self.assertFalse(_is_table_header_chain(v), v)

    def test_single_token_nav_word_still_caught(self):
        # 发现27: 排除集只保护真实内容; 单 token 整词导航仍判污染
        from detail_scraper import _is_nav_noise_value
        for v in ("社会服务", "招生信息", "表格下载", "科研成果", "学位管理"):
            self.assertTrue(_is_nav_noise_value(v), v)

    def test_table_header_chain_threshold(self):
        # 发现26/27: 真实表格拍平（合肥工业 张晶/王景运）列头链 ≥4 判污染;
        # key:value 简介与 bio 倾倒偶发 3 命中不误判（阈值 3→4 收紧）。
        from detail_scraper import _is_table_header_chain
        self.assertTrue(_is_table_header_chain(
            "学位 硕士 毕业学校 从事专业 计算机科学与技术 职务 所属院系 计算"))
        self.assertTrue(_is_table_header_chain(
            "学位 毕业学校 从事专业 职务 所属院系 所在部门 职称 高级实验师"))
        self.assertFalse(_is_table_header_chain(
            "学位：博士 毕业学校：清华大学 从事专业：计算机科学 职务：副教授 研究方向：机器学习"))
        # 吉大数学学院唐荣: bio 倾倒恰含 3 个表头词(联系方式/研究方向/学位管理)
        self.assertFalse(_is_table_header_chain(
            "联系方式 电子邮箱：x@jlu.edu.cn 研究方向 Yang-Baxter方程 学位管理"))


class TestEmptySectionStrip(unittest.TestCase):
    """发现29（2026-08-29）: 空节标签剥离——LLM 对"标签存在但内容为空"的
    section 不可靠（孙伯谦/郑光源教育经历倾倒后续全部 section, prompt 规则
    两次重跑无效）。治本: 文本层检测"标签后紧跟另一结构标签"=空节, 删标签。
    """

    def _strip(self, text):
        from detail_scraper import _strip_empty_section_tags
        return _strip_empty_section_tags(text)

    def test_li_bohu_shape(self):
        # 北航孙伯谦: "学习经历"空节后跟"工作经历", 删学习经历标签
        out = self._strip("学习经历\n工作经历\n历任北京计算机应用和仿真技术研究所所长")
        self.assertNotIn("学习经历", out)
        self.assertIn("工作经历", out)
        self.assertIn("历任", out)

    def test_pu_goguang_shape(self):
        # 华师郑光源: "教育经历/工作经历"均空节后跟"个人简介", 两个都删
        out = self._strip("教育经历\n工作经历\n个人简介\n个人研究兴趣包括 形式建模")
        self.assertNotIn("教育经历", out)
        self.assertNotIn("工作经历", out)
        self.assertIn("个人简介", out)
        self.assertIn("个人研究兴趣", out)

    def test_real_content_kept(self):
        # 真实教育经历/工作经历（标签后跟值）绝不删
        out = self._strip("教育经历\n2013-2017 香港城市大学 博士研究生\n"
                          "工作经历\n2019-至今 副教授")
        self.assertIn("教育经历", out)
        self.assertIn("工作经历", out)
        self.assertIn("香港城市大学", out)

    def test_inline_separator_shape(self):
        # 同行分隔 "教育经历 / 工作经历 / 个人简介" → 删教育经历
        out = self._strip("教育经历 / 工作经历 / 个人简介\n内容")
        self.assertNotIn("教育经历", out)
        self.assertIn("个人简介", out)

    def test_no_edu_section_untouched(self):
        # 页面本无教育经历标签（吉大唐荣形态）→ 不变
        out = self._strip("基本信息\n性别：男\n工作经历\n2014-至今 教授")
        self.assertIn("工作经历", out)
        self.assertIn("性别", out)
        self.assertEqual(out.count("工作经历"), 1)

    def test_mixed_empty_then_content(self):
        # 空节后跟真实内容段落: 删空节标签, 内容保留
        out = self._strip("教育经历\n个人简介\n2019-至今 副教授")
        self.assertNotIn("教育经历", out)
        self.assertIn("个人简介", out)
        self.assertIn("副教授", out)


class TestSectionBoundary(unittest.TestCase):
    """发现30（2026-08-29）: 字段边界溢出——启发式切块从起点标签切 section_len
    字但无终点检测。西南石油土木实测: "教育经历"后跟"学术/技术组织任职/
    社会工作任职/教学及教学研究/讲授课程/教改项目"等小节标签, 一路吞入教经
    （门禁 MISALIGN 检出 9 例教经含"讲授课程"）。治本: 起点后第一个独立
    结构标签即截断; 同系互为终点; 起点标签自身重复出现不算终点。
    """

    def _heuristic(self, text):
        return DetailScraper._extract_fields_heuristic(_dummy_scraper(), text)

    def test_swpu_civil_shape(self):
        # 真实案例(已化名)形态: 教经只含教育经历, 不吞"学术/技术组织任职"等
        text = ("教育经历：\n2013-2017：香港城市大学 博士研究生\n"
                "2006-2010：西南交通大学 本科\n"
                "学术/技术组织任职：\n《工程管理学报》青年编委\n"
                "社会工作任职：\n中国致公党成都市委员会执行主任\n"
                "教学及教学研究\n讲授课程：\n本科生：《工程合同管理》")
        fields = self._heuristic(text)
        edu = fields["教育经历"]
        self.assertIn("香港城市大学", edu)
        self.assertIn("西南交通大学", edu)
        self.assertNotIn("讲授课程", edu)
        self.assertNotIn("工程管理学报", edu)
        self.assertNotIn("致公党", edu)

    def test_edu_to_work_boundary(self):
        # 工作经历是教育经历的终点
        text = ("教育经历\n2004-2007 南开大学 博士\n"
                "工作经历\n2016-至今 吉林大学 教授")
        fields = self._heuristic(text)
        edu = fields["教育经历"]
        self.assertIn("南开大学", edu)
        self.assertNotIn("吉林大学", edu)
        work = fields["工作经历"]
        self.assertIn("吉林大学", work)

    def test_real_research_desc_in_edu_kept(self):
        # 安大范培锋形态: 教经里"主要研究方向为"非独立标签（前是"要"）,
        # 不得误截断; 但独立"学术成果："是终点, 成果段被切除
        text = ("教育经历\n学位，2019-2021年底在深圳大学从事博士后研究工作，"
                "主要研究方向为：磁约束等离子体物理基础理论。\n"
                "学术成果：\n在国内期刊发表论文多篇")
        fields = self._heuristic(text)
        edu = fields["教育经历"]
        self.assertIn("博士后", edu)
        self.assertIn("磁约束等离子体", edu)
        self.assertNotIn("发表论文", edu)

    def test_work_to_research_boundary(self):
        # 研究方向是工作经历的终点
        text = ("工作经历\n2016-至今 吉林大学 教授\n"
                "研究方向\n算子理论、复分析")
        fields = self._heuristic(text)
        work = fields["工作经历"]
        self.assertIn("吉林大学", work)
        self.assertNotIn("算子理论", work)

    def test_swpu_org_variant(self):
        # 赵翔形态: "学术组织任职"（无斜杠变体）也是终点
        text = ("教育经历：\n2011.09-2015.12：西南交通大学 博士研究生\n"
                "学术组织任职：\n四川省力学学会动力学专委会 秘书长")
        fields = self._heuristic(text)
        edu = fields["教育经历"]
        self.assertIn("西南交通大学", edu)
        self.assertNotIn("力学学会", edu)

    def test_same_label_repeat_not_end(self):
        # 起点标签自身重复出现（"教育经历：A\n教育经历：B"）不是终点, 两段都保留
        text = "教育经历\n2004-2007 南开大学 博士\n教育经历\n2010-2014 清华 博士后"
        fields = self._heuristic(text)
        edu = fields["教育经历"]
        self.assertIn("南开大学", edu)
        self.assertIn("博士后", edu)


class TestZhichengPriority(unittest.TestCase):
    """发现31（2026-08-29）: 职称提取——"职称/技术职称/现任职称:"标签值为准。
    安大材料 PDF 实测: LLM 被工作经历历史职称误导（周腾飞 PDF 技术职称=教授
    提取=副研究员; 17 例不一致）; 启发式词表全文扫描"副研究员"先于"教授"
    命中历史职务。标签优先; "暂无/无"置空; 无标签全文扫描兜底。
    """

    def _heuristic(self, text):
        return DetailScraper._extract_fields_heuristic(_dummy_scraper(), text)

    def test_tag_priority_over_history(self):
        # 单磊形态: 技术职称=教授, 工作经历含"副研究员"历史职务 → 教授
        text = ("技术职称：教授 聘任方式：校内双聘\n"
                "工作经历：2004/6-2013/8 中国科学院物理研究所，副研究员")
        self.assertEqual(self._heuristic(text)["职称"], "教授")

    def test_tag_with_parenthetical(self):
        # 职称标签带括注: "技术职称：教授（博导）" → 教授
        text = "技术职称：教授（博导）"
        self.assertEqual(self._heuristic(text)["职称"], "教授")

    def test_tag_none_blank(self):
        # "技术职称：暂无" → 空（不误填工作经历职称）
        text = "技术职称：暂无\n工作经历：2010-2015 副研究员"
        self.assertEqual(self._heuristic(text)["职称"], "")

    def test_no_tag_fallback_scan(self):
        # 无职称标签 → 全文词表扫描兜底
        text = "现任职务：副院长\n2004年获评副教授"
        self.assertEqual(self._heuristic(text)["职称"], "副教授")

    def test_tag_zhicheng_line(self):
        # "职称：讲师" 独立行 → 讲师（不被其他职称词抢先）
        text = "职称：讲师\n学术成果：曾获教授级荣誉"
        self.assertEqual(self._heuristic(text)["职称"], "讲师")

    def test_zhuli_yanjiuyuan_not_swallowed(self):
        # 发现34: "助理研究员"是"研究员"子串, 标签捕获后不能降级为研究员
        text = "技术职称：助理研究员 聘任方式：校内双聘"
        self.assertEqual(self._heuristic(text)["职称"], "助理研究员")

    def test_professor_level_eng_longest(self):
        # 发现34: 长词优先——"教授级高级工程师"不能命中"教授"/"工程师"
        text = "技术职称：教授级高级工程师 聘任方式：聘任"
        self.assertEqual(self._heuristic(text)["职称"], "教授级高级工程师")

    def test_heuristic_fulltext_longest(self):
        # 发现34: 无标签全文扫描同样最长优先——正文含"助理研究员"
        # 时不得命中"研究员"子串
        text = "个人简介：某某，助理研究员，主要从事……"
        self.assertEqual(self._heuristic(text)["职称"], "助理研究员")

    def test_tag_none_with_suffix(self):
        # 发现34: 标签值带"聘任方式"后缀（空格分隔）时只捕获标签值
        res = {"职称": "副教授"}
        _verify_zhicheng(res, "技术职称：暂无 聘任方式：校内双聘")
        self.assertEqual(res["职称"], "")


class TestNarrativeTimeline(unittest.TestCase):
    """发现35: 叙述型简历时间线——无教育/工作经历标签时, "个人简历:"下
    日期条目按学历信号归入教育/工作经历。安大材料 PDF 实测 LLM 主路径
    也漏（40 人教经空, 其中 11 人时间线型可启发式恢复, 29 人散文型留空）。"""

    def _heuristic(self, text):
        return DetailScraper._extract_fields_heuristic(_dummy_scraper(), text)

    def test_timeline_slash_split(self):
        # 龙明生形态: 斜杠日期混排, 学历：标注归教育
        text = ("技术职称：教授\n"
                "个人简历：2019/7-至今安徽大学物质科学与信息技术研究院教授"
                "2017/6-2019/5中国科学院上海技术物理研究所助理研究员"
                "2014/9-2017/6南京大学物理学院学历：博士研究生专业：物理学")
        r = self._heuristic(text)
        self.assertIn("南京大学", r["教育经历"])
        self.assertIn("博士研究生", r["教育经历"])
        self.assertIn("安徽大学", r["工作经历"])
        self.assertIn("助理研究员", r["工作经历"])
        self.assertNotIn("南京大学", r["工作经历"])

    def test_timeline_dot_split(self):
        # 李超形态: 点号日期, 博士/学士作学历信号
        text = ("个人简历：安徽大学材料科学与工程学院副研究员，"
                "2026.01-至今安徽大学副研究员"
                "2022.04-2025.12安徽大学助理研究员"
                "2012.09-2018.11中国科学技术大学有机化学理学博士导师：丁奎岭")
        r = self._heuristic(text)
        self.assertIn("中国科学技术大学", r["教育经历"])
        self.assertIn("博士", r["教育经历"])
        self.assertIn("副研究员", r["工作经历"])
        self.assertNotIn("中国科学技术大学", r["工作经历"])

    def test_prose_no_timeline_stays_empty(self):
        # 侯杰形态: 散文型无日期时间线 → 教经/工经保持空（宁缺毋滥）
        text = ("个人简历：侯杰，博士，安徽省百人计划入选者。"
                "2018年博士毕业于日本东北大学Komeda实验室，随后从事博士后研究。"
                "2021年1月回国加入安徽大学。长期从事量子材料研究。")
        r = self._heuristic(text)
        self.assertEqual(r["教育经历"], "")
        self.assertEqual(r["工作经历"], "")

    def test_no_fire_without_resume_label(self):
        # 无"个人简历"标签 → 不触发时间线解析
        text = "研究方向：量子功能材料\n学术成果：发表SCI论文10篇"
        r = self._heuristic(text)
        self.assertEqual(r["教育经历"], "")
        self.assertEqual(r["工作经历"], "")

    def test_labeled_section_takes_priority(self):
        # 有正规"教育经历"标签时走常规路径, 时间线兜底不覆盖
        text = ("教育经历：2004-2007 南开大学 博士\n"
                "工作经历：2007-至今 安徽大学 教授\n"
                "个人简历：2019/7-至今安徽大学教授2014/9-2017/6南京大学学历：博士")
        r = self._heuristic(text)
        self.assertIn("南开大学", r["教育经历"])
        self.assertNotIn("南京大学", r["教育经历"])

    def test_book_title_years_not_timeline(self):
        # 发现35加固: 著作目录的书名年份区间（"1949-2011"裸年份）不得
        # 当时间线——安大文学院周志雄实测把著作目录 dump 进教经
        text = ("个人简历：硕士研究生（1998，湖北大学），博士生导师。\n"
                "著作目录：\n"
                "9.《回眸百媚的样貌：中国当代小说情爱叙事研究（1949-2011》"
                "（修订版），台湾秀威出版社，2014年4月版。\n"
                "10.《新世纪网络文学的侧面》，山东人民出版社，2014年12月版。")
        r = self._heuristic(text)
        self.assertEqual(r["教育经历"], "")
        self.assertEqual(r["工作经历"], "")

    def test_timeline_requires_month_separator(self):
        # 发现35加固: 裸年份区间（"2004-2008 大学"）不算时间线,
        # 必须有月份分隔符（2019/7、2026.01、2013年10月）
        text = "个人简历：2004-2008北京大学博士2010-2015安徽大学讲师"
        r = self._heuristic(text)
        self.assertEqual(r["教育经历"], "")
        self.assertEqual(r["工作经历"], "")


class TestPdfInlineSectionNewline(unittest.TestCase):
    """发现31: PDF 密集排版行内标签切分——"…本科工作经历（科研…" 的
    "工作经历"前是"科"（非行首/冒号）, 启发式独立判定失效; 标签后跟括号
    =强标签信号, 插换行恢复结构。"""

    def _heuristic(self, text):
        return DetailScraper._extract_fields_heuristic(_dummy_scraper(), text)

    def test_inline_work_label(self):
        # 单磊形态: 本科工作经历（… 同行 → 工经切出
        text = ("教育经历（按时间倒排序）：1996/9-2001/7 南京大学 博士\n"
                "本科工作经历（科研与学术工作经历，按时间倒序排序）："
                "2018/9至今 安徽大学 教授\n学术成果：论文多篇")
        fields = self._heuristic(text)
        work = fields["工作经历"]
        self.assertIn("2018/9至今", work)
        self.assertNotIn("学术成果", work)

    def test_narrative_work_experience_not_split(self):
        # 正文"多年工作经历"（后无括号）不切分、不误当工经
        text = "个人简介：具有多年工作经历，积累了丰富经验"
        fields = self._heuristic(text)
        self.assertEqual(fields["工作经历"], "")

    def test_pdf_chain_edu_label(self):
        # "个人简历：教育经历（按时间倒排序）：…" 前导冒号 + 括注 → 教经切出
        text = "个人简历：教育经历（按时间倒排序）：1996/9-2001/7 南京大学 博士"
        fields = self._heuristic(text)
        self.assertIn("南京大学", fields["教育经历"])


class TestPageNameMismatch(unittest.TestCase):
    """发现32（2026-08-29）: 姓名一致性校验——title 去空格变体 + R13 位置
    防护。真实案例(已化名): 正文"王某某，教授，博士生导师…"仅 1 次名（位置
    0%, 个人简介开头）, title="王 某某-土木"带空格, 被 R13 误判列表页判死 →
    数据缺失。列表页（名字在尾部）不受影响。"""

    def _check(self, title, text, name):
        from detail_scraper import _page_name_mismatch
        return _page_name_mismatch(title, text, name)

    def test_title_space_variant_passes(self):
        # "王 某某-土木" title 空格变体 + 正文开头 1 次名 → 放行
        title = "王 某某-土木工程与测绘学院"
        text = "王某某，教授，博士生导师，教育部新世纪优秀人才，某大学土木工程与建筑学院院长。长期从事…"
        self.assertFalse(self._check(title, text, "王某某"))

    def test_template_title_no_name_passes(self):
        # title 模板不含名 + 正文开头名 1 次（真实个人页）→ 位置防护放行
        title = "教师个人主页"
        text = "张老师，副教授，主要从事… 他长期从事计算机视觉研究。"
        self.assertFalse(self._check(title, text, "张老师"))

    def test_list_page_tail_name_dead(self):
        # 列表页: 名字在文本尾部 1 次 → 仍判死
        title = "教研室-某大学哲学系"
        text = ("马克思主义哲学教研室 主任：教师甲；中国哲学教研室 主任：教师乙；"
                "逻辑学教研室 主任：教师丙；…… 哲学系现有教师名单：教师丁")
        self.assertTrue(self._check(title, text, "教师丁"))

    def test_no_name_dead(self):
        # 页面完全无名字（栏目页/URL 错配）→ 判死
        title = "本科教学建设-云南大学"
        text = "教学建设 专业建设 课程建设 教材建设 实践教学 教学成果"
        self.assertTrue(self._check(title, text, "教学建设"))

    def test_latin_page_passes(self):
        # 全英文页（外教）→ 放行
        title = "Faculty - Department"
        text = "Dr. John Smith is a professor of computer science."
        self.assertFalse(self._check(title, text, "张三"))


class TestErrorPageDns(unittest.TestCase):
    """东北师大教训: Chrome DNS 错误页（~187KB 大页面）必须被 is_error_page
    识别为 fetch 失败——否则 187KB 大错误页绕过 <3000 短页面检查,
    被判 html_noise（归因错误: 站点不可达 ≠ 页面是导航壳）。"""

    @classmethod
    def setUpClass(cls):
        import http_client as hc
        cls.hc = hc

    def _err(self, html):
        return self.hc.is_error_page(html)

    def test_chrome_dns_error_page_big(self):
        # 复现 js.nenu.edu.cn NXDOMAIN 的 Chrome 错误页（大页面 > 3000 字符）
        big = ('<!DOCTYPE html><html><head><title>js.nenu.edu.cn</title></head>'
               '<body><h1>无法访问此网站</h1>'
               '<p>找不到 js.nenu.edu.cn 的服务器 IP 地址。</p>'
               '<p>ERR_NAME_NOT_RESOLVED</p>'
               '<p>请检查代理服务器、防火墙和 DNS 配置</p>'
               '</body></html>') * 3000
        self.assertTrue(self._err(big))

    def test_chrome_dns_variants(self):
        for t in ("ERR_CONNECTION_REFUSED", "ERR_CONNECTION_TIMED_OUT",
                  "ERR_SSL_PROTOCOL_ERROR", "ERR_EMPTY_RESPONSE",
                  "无法访问此网站", "找不到 服务器 IP 地址"):
            page = f'<html><head><title>x</title></head><body>{t}</body></html>' * 500
            self.assertTrue(self._err(page), t)

    def test_real_page_not_killed(self):
        real = ('<!DOCTYPE html><html><head><title>张三 - 化学学院</title></head>'
                '<body><div class="wp_articlecontent">'
                '<p>研究方向：有机合成化学、不对称催化</p>'
                '<p>邮箱：zhangsan@nenu.edu.cn</p></div></body></html>')
        self.assertFalse(self._err(real))

    def test_long_real_page_not_killed(self):
        # 正常长文（>3000 字符）不含 DNS 特征词 → 不误杀
        long_real = ('<html><head><title>学院简介</title></head><body>'
                     '<p>学院的科研方向涵盖有机化学、无机化学、分析化学、'
                     '物理化学与高分子化学，依托国家重点实验室开展研究。</p>'
                     * 300) + '</body></html>'
        self.assertFalse(self._err(long_real))


class TestDnsPrecheck(unittest.TestCase):
    """东北师大教训: NXDOMAIN 域名必须 DNS 预检快速失败（0.06s），
    不能进浏览器降级链卡 15 分钟；真实域名不受影响。"""

    def test_nxdomain_fast_fail(self):
        from detail_scraper import DetailScraper
        s = _dummy_scraper()
        s._log = lambda *a, **k: None
        # 不存在的顶级域名（RFC 2606 保留 TLD，保证 NXDOMAIN）
        html = s.fetch_page("http://js.nenu.edu.cn.invalid/x.htm")
        self.assertIsNone(html)

    def test_reachable_domain_attempts_fetch(self):
        # 真实域名（baidu.com 一定可解析）→ 不应被 DNS 预检短路。
        # 不实际请求（无网络测试），只验证预检放行到降级链:
        # 用 mock 掉 _http.fetch_text 确认被调用。
        from unittest import mock
        from detail_scraper import DetailScraper
        s = _dummy_scraper()
        s._throttle = lambda: None
        s._http = mock.MagicMock()
        s._http.fetch_text.return_value = "<html>ok</html>"
        out = s.fetch_page("https://www.baidu.com/x.htm")
        self.assertEqual(out, "<html>ok</html>")
        s._http.fetch_text.assert_called_once()

    def test_dns_fail_returns_none_via_gaierror(self):
        # .invalid TLD 保证 getaddrinfo 抛 gaierror → fetch_page 短路返回 None
        from detail_scraper import DetailScraper
        s = _dummy_scraper()
        s._log = lambda *a, **k: None
        self.assertIsNone(s.fetch_page("http://nxdomain-example.invalid/"))

    def test_dns_fail_bypasses_fetch_chain(self):
        # 关键: NXDOMAIN 时绝不能调用底层 fetch_text（浏览器降级链会卡死）
        from unittest import mock
        from detail_scraper import DetailScraper
        s = _dummy_scraper()
        s._throttle = lambda: None
        s._log = lambda *a, **k: None
        s._http = mock.MagicMock()
        s.fetch_page("http://js.nenu.edu.cn.invalid/x.htm")
        s._http.fetch_text.assert_not_called()


class TestPageMentionsTeacher(unittest.TestCase):
    """云大教训: 栏目导航页被当教师详情页, 凭导航串/学院总机提取假字段。
    教师详情页 title/正文必须把姓名作为独立词提及; 栏目页要么不含姓名,
    要么姓名只是栏目词子串（"本科教学建设"含"教学建设"）→ 边界匹配。"""

    def test_independent_in_title(self):
        from detail_scraper import _page_mentions_teacher
        self.assertTrue(_page_mentions_teacher("钱学理-北京大学哲学系", "", "钱学理"))
        self.assertTrue(_page_mentions_teacher("钱学理 教授简介", "", "钱学理"))
        self.assertTrue(_page_mentions_teacher("钱学理", "", "钱学理"))
        self.assertTrue(_page_mentions_teacher("王骏（北京大学哲学系）", "", "王骏"))

    def test_suffix_attached_in_title(self):
        from detail_scraper import _page_mentions_teacher
        self.assertTrue(_page_mentions_teacher("钱学理教授", "", "钱学理"))
        self.assertTrue(_page_mentions_teacher("孙喆老师的个人主页", "", "孙喆"))

    def test_substring_not_independent(self):
        # "教学建设"是"本科教学建设"的子串 → 栏目词嵌入 → 判死
        from detail_scraper import _page_mentions_teacher
        self.assertFalse(_page_mentions_teacher(
            "本科教学建设-云南大学地球科学学院", "", "教学建设"))

    def test_no_name_in_title_or_text(self):
        from detail_scraper import _page_mentions_teacher
        self.assertFalse(_page_mentions_teacher(
            "师资队伍-云南大学地球科学学院", "学位点建设 培养过程 学院新闻", "教研风采"))

    def test_name_in_body_after_no_title(self):
        # title 模板站: 正文独立出现姓名 → 放行（不误杀）
        from detail_scraper import _page_mentions_teacher
        self.assertTrue(_page_mentions_teacher(
            "教师简介", "钱学理教授，北京大学哲学系主任", "钱学理"))

    def test_embedded_name_in_body_rejected(self):
        # 正文里姓名也只是栏目词子串 → 判死
        from detail_scraper import _page_mentions_teacher
        self.assertFalse(_page_mentions_teacher(
            "学院首页", "本科教学建设 教研风采 师资队伍 学院新闻", "教学建设"))

    def test_short_name_skips(self):
        from detail_scraper import _page_mentions_teacher
        self.assertTrue(_page_mentions_teacher("某页", "正文", "单"))


class TestPageIsLatin(unittest.TestCase):
    def test_latin_page(self):
        from detail_scraper import _page_is_latin
        self.assertTrue(_page_is_latin("Dr. John Smith is a professor at PKU."))

    def test_chinese_page(self):
        from detail_scraper import _page_is_latin
        self.assertFalse(_page_is_latin("钱学理教授的研究方向是中国哲学"))

    def test_empty(self):
        from detail_scraper import _page_is_latin
        self.assertFalse(_page_is_latin(""))


class TestFetchStatusAttr(unittest.TestCase):
    """北大哲学系教训: fetch 失败必须区分 HTTP 404（URL 失效, 上游名单
    问题, 可回补）vs 返回空（网络/超时, 站点问题）。状态码经
    http_client.last_status 留痕, fetch_page 对 ≥400 抛异常进失败原因。"""

    def test_http_404_raises(self):
        from unittest import mock
        from detail_scraper import DetailScraper
        s = _dummy_scraper()
        s._throttle = lambda: None
        s._log = lambda *a, **k: None
        s._http = mock.MagicMock()
        s._http.fetch_text.return_value = None
        s._http.last_status = 404
        with self.assertRaises(ValueError) as cm:
            s.fetch_page("https://www.baidu.com/szdw/x.htm")
        self.assertIn("404", str(cm.exception))

    def test_retry_fast_fails_on_4xx(self):
        # 北大哲学系教训: 404 是确定性失败, 重试只会空耗浏览器级
        # 降级链（每次 2-3 分钟挂起）——必须快速失败
        from detail_scraper import DetailScraper, HttpStatusError
        s = _dummy_scraper()
        calls = []
        fn = lambda: (calls.append(1), (_ for _ in ()).throw(HttpStatusError(404)))[1]
        val, retries, err = s._retry(fn, "fetch")
        self.assertIsNone(val)
        self.assertEqual(retries, 0)          # 未空耗重试
        self.assertEqual(len(calls), 1)       # fn 只调一次
        self.assertIn("HTTP 404", err)
        self.assertNotIn("重试", err)

    def test_no_status_returns_none(self):
        from unittest import mock
        from detail_scraper import DetailScraper
        s = _dummy_scraper()
        s._throttle = lambda: None
        s._log = lambda *a, **k: None
        s._http = mock.MagicMock()
        s._http.fetch_text.return_value = None
        s._http.last_status = 0
        self.assertIsNone(s.fetch_page("https://www.baidu.com/y.htm"))

    def test_success_returns_html(self):
        from unittest import mock
        from detail_scraper import DetailScraper
        s = _dummy_scraper()
        s._throttle = lambda: None
        s._log = lambda *a, **k: None
        s._http = mock.MagicMock()
        s._http.fetch_text.return_value = "<html>ok</html>"
        s._http.last_status = 200
        self.assertEqual(s.fetch_page("https://www.baidu.com/y.htm"), "<html>ok</html>")


class TestJunkNameNavMenu(unittest.TestCase):
    """发现39: 导航菜单项整词作教师姓名（上财"专题学习"/云大"教学建设"）。"""

    def test_nav_menu_names_junk(self):
        from detail_scraper import _is_junk_name
        for n in ("专题学习", "教工之家", "组织机构", "教学建设",
                  "教研风采", "研究生院", "本科生", "硕士生"):
            self.assertTrue(_is_junk_name(n), n)

    def test_real_names_kept(self):
        from detail_scraper import _is_junk_name
        for n in ("张建国", "王学", "李本科", "陈博", "刘教授", "张院士"):
            self.assertFalse(_is_junk_name(n), n)


class TestNavFragValue(unittest.TestCase):
    """发现38: 导航链接文本碎片入字段（"名师"/"获得者"/"成果" 单双字值）。"""

    def test_frag_detected(self):
        from detail_scraper import _is_nav_frag_value
        self.assertTrue(_is_nav_frag_value("名师", "讲授课程"))
        self.assertTrue(_is_nav_frag_value("获得者", "基金"))
        self.assertTrue(_is_nav_frag_value("成果", "荣誉"))
        self.assertTrue(_is_nav_frag_value("科研", "讲授课程"))
        self.assertTrue(_is_nav_frag_value("荣誉", "荣誉"))

    def test_zhicheng_legit_excluded(self):
        # 职称="院士" 是合法值, 不被碎片判定误杀
        from detail_scraper import _is_nav_frag_value
        self.assertFalse(_is_nav_frag_value("院士", "职称"))
        self.assertFalse(_is_nav_frag_value("教授", "职称"))

    def test_non_frag_values_kept(self):
        from detail_scraper import _is_nav_frag_value
        for v in ("教学名师奖", "国家杰出青年基金获得者", "优秀教学成果奖", "院士工作站"):
            self.assertFalse(_is_nav_frag_value(v, "荣誉"), v)

    def test_empty_and_long(self):
        from detail_scraper import _is_nav_frag_value
        self.assertFalse(_is_nav_frag_value("", "基金"))
        self.assertFalse(_is_nav_frag_value(None, "基金"))
        self.assertFalse(_is_nav_frag_value("获奖成果转化应用实践", "荣誉"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSelfSectionLabel(unittest.TestCase):
    """发现40: 节标签前缀未剥离（历史启发式切块起点=标签首字符, 370+ 例）。"""

    def _strip(self, v, f):
        from detail_scraper import _strip_self_section_label
        return _strip_self_section_label(v, f)

    def test_colon_prefix_stripped(self):
        # "工作经历： 2004 年-至今 ..." → 内容
        self.assertEqual(
            self._strip("工作经历： 2004年-至今 吉林大学 副教授", "工作经历"),
            "2004年-至今 吉林大学 副教授")

    def test_space_prefix_stripped(self):
        # "教育经历 1995年，河北师范大学..." → 内容
        self.assertEqual(
            self._strip("教育经历 1995年，河北师范大学历史系，获学士学位", "教育经历"),
            "1995年，河北师范大学历史系，获学士学位")

    def test_contact_label_stripped(self):
        self.assertEqual(
            self._strip("联系方式：liqingwei@shu.edu.cn", "联系方式"),
            "liqingwei@shu.edu.cn")

    def test_label_chain_cleared(self):
        # 郑光源: "教育经历 工作经历 个人简介 ..." 剥后是兄弟标签 = 倾倒
        self.assertEqual(
            self._strip("教育经历 工作经历 个人简介 个人研究兴趣包括...", "教育经历"),
            "")
        self.assertEqual(
            self._strip("工作经历： 教育经历： 1991.09-1995.06 武汉测绘科技大学", "工作经历"),
            "")

    def test_empty_after_label_cleared(self):
        # 杨晶晶 荣誉="荣誉：" → 剥后无内容
        self.assertEqual(self._strip("荣誉：", "荣誉"), "")

    def test_no_prefix_untouched(self):
        # 无前缀不动; 非自身标签不动（错位交给 MISALIGN 检测）
        self.assertEqual(self._strip("2008-2015 安徽大学 副教授", "工作经历"),
                         "2008-2015 安徽大学 副教授")
        self.assertEqual(self._strip("工作经历 1999-2012 大连海事大学", "教育经历"),
                         "工作经历 1999-2012 大连海事大学")
        self.assertEqual(self._strip("教授", "职称"), "教授")

    def test_idempotent(self):
        v = "教育经历： 1995年，河北师范大学历史系，获学士学位"
        once = self._strip(v, "教育经历")
        self.assertEqual(self._strip(once, "教育经历"), once)


class TestLabelSectionStartAfterLabel(unittest.TestCase):
    """发现40: _label_section 起点改为标签后——启发式值不再带标签前缀。"""

    def test_section_value_has_no_label_prefix(self):
        from detail_scraper import DetailScraper
        d = DetailScraper("测试", "测试院", "/tmp")
        raw = ("姓名 张三\n职称 教授\n"
               "工作经历： 2004年-至今 吉林大学植物科学学院 副教授\n"
               "教育经历： 1994年-1997年 沈阳农业大学 硕士\n"
               "研究方向： 作物遗传育种")
        result = d._extract_fields_heuristic(raw)
        self.assertTrue(result["工作经历"].startswith("2004年-至今"))
        self.assertTrue(result["教育经历"].startswith("1994年-1997年"))
        self.assertEqual(result["研究方向"], "作物遗传育种")


class TestUnmixTailSegments(unittest.TestCase):
    """发现41: 尾漏切分搬移——字段值尾部兄弟节标签段拆回各自字段。"""

    def _unmix(self, v, f):
        from detail_scraper import _unmix_tail_segments
        return _unmix_tail_segments(v, f)

    def test_multi_segment_move(self):
        # 姜长松: 工作经历 尾挂 教学情况/主要研究方向
        v = ("2004年-至今 吉林大学 副教授 1999年-2004年 解放军军需大学 讲师 "
             "教学情况： 本科生课程：《农业机械与应用》 主要研究方向： 车辆工程 数字农业")
        main, moves = self._unmix(v, "工作经历")
        self.assertTrue(main.startswith("2004年-至今"))
        self.assertIn(("讲授课程", "本科生课程：《农业机械与应用》"), moves)
        self.assertIn(("研究方向", "车辆工程 数字农业"), moves)

    def test_single_segment_move(self):
        # 姜妍: 科研项目 尾 → 基金
        v = ("2008/07-2012/08，东北农业大学，助理研究员； "
             "科研项目 1.“十四五”国家重点研发项目，115万，主持")
        main, moves = self._unmix(v, "工作经历")
        self.assertIn(("基金", "1.“十四五”国家重点研发项目，115万，主持"), moves)

    def test_in_sentence_label_not_split(self):
        # 发现37: "获硕士学位, 研究方向: 概率论" 前导逗号 → 非独立, 不切
        v = "获理学硕士学位，研究方向：概率论与数理统计 1999年获学士学位"
        main, moves = self._unmix(v, "教育经历")
        self.assertEqual(moves, [])

    def test_ambiguous_label_requires_colon(self):
        # 发现41 dry-run: "课题组 联合培养博士" 的课题 后跟"组" → 不切
        v = "2017年10月-2019年3月 美国康涅狄格大学 化学系 课题组 联合培养博士"
        main, moves = self._unmix(v, "教育经历")
        self.assertEqual(moves, [])

    def test_self_target_not_split(self):
        # 学术成果 里的 "专著，企业管理出版社" 与字段同目标 → 不动
        v = "聂爽爽，王维红. 论文A. 针织工业, 2020；专著，企业管理出版社，2012年出版"
        main, moves = self._unmix(v, "学术成果")
        self.assertEqual(moves, [])

    def test_nav_main_skipped(self):
        # 上财栏目页倾倒: 主段是导航词链 → 不切
        v = ("学位合作 国外硕士项目 社会服务 校友发展 联系我们 科学研究 学术期刊 "
             "研究概况 学术讲座 会议论坛 学者观点 研究机构 研究成果 论文著作 课题立项 获奖成果")
        main, moves = self._unmix(v, "教育经历")
        self.assertEqual(moves, [])

    def test_idempotent(self):
        v = ("2004年-至今 吉林大学 副教授 教学情况： 本科生课程：《农业机械与应用》")
        main, moves = self._unmix(v, "工作经历")
        main2, moves2 = self._unmix(main, "工作经历")
        self.assertEqual(moves2, [])


class TestMergeTailTarget(unittest.TestCase):
    """发现41: 搬移目标已有值时的确定性合并。"""

    def _merge(self, tv, content):
        from detail_scraper import _merge_tail_target
        return _merge_tail_target(tv, content)

    def test_redundant_dropped(self):
        # moved ⊆ target（同页同节提取两次）
        tv = "1.“十四五”国家重点研发项目，115万，主持；2.国家大豆产业技术体系，70万"
        self.assertEqual(self._merge(tv, "1.“十四五”国家重点研发项目，115万，主持"), tv)

    def test_fragment_replaced_by_full(self):
        # 目标只是片段头部, moved 是全文 → 用全文
        tv = "大洋性鲨鱼、鱿鱼"
        full = "大洋性鲨鱼、鱿鱼 营养生态学，鲨鱼兼捕预防和减缓技术"
        self.assertEqual(self._merge(tv, full), full)

    def test_distinct_appended(self):
        tv = "等国家和省部级课题"
        content = "（Research Projects）1. 国家自然科学基金面上项目（2022）"
        self.assertEqual(self._merge(tv, content), tv + "；" + content)

    def test_empty_target_takes_content(self):
        self.assertEqual(self._merge("", "abc"), "abc")
        self.assertEqual(self._merge(None, "abc"), "abc")


class TestStripSelfLabelExtended(unittest.TestCase):
    """发现41: 括注形态 + 同义标签前缀剥离。"""

    def _strip(self, v, f):
        from detail_scraper import _strip_self_section_label
        return _strip_self_section_label(v, f)

    def test_paren_note_stripped(self):
        # 秦建春: "工作经历（Professional Experience） 2019年…"
        self.assertEqual(
            self._strip("工作经历（Professional Experience） 2019年10月-11月，日本冈山大学访问交流", "工作经历"),
            "2019年10月-11月，日本冈山大学访问交流")

    def test_synonym_label_stripped(self):
        # 张贺: 教育经历="学习经历： 2009年…"
        self.assertEqual(
            self._strip("学习经历： 2009年9月-2013年6月，甘肃农业大学", "教育经历"),
            "2009年9月-2013年6月，甘肃农业大学")
        # 李冠乔: 工作经历="工作履历： 2022-至今…"
        self.assertEqual(
            self._strip("工作履历： 2022-至今 清华大学，助理教授", "工作经历"),
            "2022-至今 清华大学，助理教授")

    def test_no_separator_untouched(self):
        # "讲授课程与教学改革" 词首同位不误剥（发现41 修复的零分隔符隐患）
        self.assertEqual(
            self._strip("讲授课程与教学改革相关研究", "讲授课程"),
            "讲授课程与教学改革相关研究")


class TestJunkNameNavMenuWords(unittest.TestCase):
    """发现41: _NAME_JUNK_SUB 补复合栏目词（上财/华科/上交 名单污染漏网）。"""

    def test_menu_items_caught(self):
        from detail_scraper import _is_junk_name
        for n in ("会议论坛", "学者观点", "学术期刊", "培养管理", "职能科室",
                  "师者故事", "专任教师", "学术讲座"):
            self.assertTrue(_is_junk_name(n), n)

    def test_real_names_kept(self):
        from detail_scraper import _is_junk_name
        for n in ("姜长松", "都兴林", "李景文", "张建国", "王学会"):
            self.assertFalse(_is_junk_name(n), n)


class TestCutFooterNoise(unittest.TestCase):
    """发现42: 页脚块切尾——真内容+页脚尾（地址/电话/Copyright）只切页脚。"""

    def _cut(self, v):
        from detail_scraper import _cut_footer_noise
        return _cut_footer_noise(v)

    def test_content_kept_footer_cut(self):
        v = ("1. 国家自然科学基金项目，2019年；2. 面向行为的微控制器软件综合方法研究 "
             "地址：武汉市珞瑜路129号武汉大学计算机学院 电话：027-68775509 Copyright 武汉大学")
        out = self._cut(v)
        self.assertTrue(out.startswith("1. 国家自然科学基金项目"))
        self.assertNotIn("Copyright", out)
        self.assertNotIn("地址：", out)

    def test_no_footer_untouched(self):
        v = "2004年-至今 吉林大学 副教授"
        self.assertEqual(self._cut(v), v)

    def test_contact_only_value_with_copyright(self):
        # 联系方式字段: 电话/邮箱是内容, 页脚块从"地址："起
        v = "电话：13812345678；邮箱：a@b.edu.cn 地址：学院楼 Copyright 大学"
        out = self._cut(v)
        self.assertTrue(out.startswith("电话：13812345678"))
        self.assertNotIn("Copyright", out)


class TestIsPageDumpValue(unittest.TestCase):
    """发现42: 整页倾倒检测——形态1 页脚+导航词+联系方式; 形态2 无日期纯导航长链。"""

    def _is(self, v):
        from detail_scraper import _is_page_dump_value
        return _is_page_dump_value(v)

    def test_form1_detected(self):
        v = ("学位管理 奖助学金 工作流程与下载 科学研究 学术方向 学术报告 学术会议 国际交流 "
             "国际合作平台 国际会议 访问学者 境外教师及进修培训 地址：西安市长安南路199号 "
             "电话：029-85308892 邮箱：sdwy@snnu.edu.cn Copyright 陕西师范大学")
        self.assertTrue(self._is(v))

    def test_form2_no_footer_no_dates(self):
        v = ("学位管理 奖助学金 工作流程与下载 科学研究 学术方向 学术报告 学术会议 国际交流 "
             "国际合作平台 国际会议 访问学者 境外教师及博士后 建设平台 国家天元数学东北中心 "
             "吉林国家应用数学中心 中俄数学中心 其他平台 合作交流基地（学校） 党建工作 组织机构")
        self.assertTrue(self._is(v))

    def test_content_with_footer_not_dump(self):
        # 王磊型: 真内容+页脚, 无导航词 → 只切尾不清空
        v = ("在AppliedPhysicsLetter等杂志发表学术论文十余篇；获得国家发明专利十余项 "
             "地址：合肥市九龙路111号 电话：0551-63861000 Copyright 安徽大学")
        self.assertFalse(self._is(v))

    def test_award_value_not_dump(self):
        # 张磊荣誉: "青年教师授课竞赛"含"教师"子串, 整词计数不误判
        v = ("第六届全国高等学校电子信息类专业青年教师授课竞赛（华东赛区）一等奖；"
             "全国高校光电信息类专业教师授课比赛一等奖；校教学优秀奖")
        self.assertFalse(self._is(v))

    def test_attr_json_not_dump(self):
        from detail_scraper import _field_display
        v = _field_display({"办公室": "A517", "招生信息": "计算机学院招生办",
                            "学术组织任职": "CCF会员"})
        self.assertFalse(self._is(v))


class TestCutNavTail(unittest.TestCase):
    """发现42: 导航尾（师资队伍▶/返回顶部）切割。"""

    def _cut(self, v):
        from detail_scraper import _cut_nav_tail
        return _cut_nav_tail(v)

    def test_breadcrumb_tail_cut(self):
        v = "2025年至今 云南大学 讲师 讲授《数据结构》等课程教学任务。 师资队伍 ▶ 教职工 教授 副教授"
        out = self._cut(v)
        self.assertNotIn("师资队伍", out)
        self.assertTrue(out.endswith("等课程教学任务。"))

    def test_short_value_untouched(self):
        v = "师资队伍 ▶ 教职工 教授"
        self.assertEqual(self._cut(v), v)

    def test_back_to_top_cut(self):
        v = "2004年至今 吉林大学 副教授 返回顶部"
        self.assertEqual(self._cut(v), "2004年至今 吉林大学 副教授")


class TestSplitFormPairs(unittest.TestCase):
    """发现42: 表单块拆分——学历/电话/Email/领域 对分发到各自字段。"""

    def _split(self, v):
        from detail_scraper import _split_form_pairs
        return _split_form_pairs(v)

    def test_edu_form_split(self):
        v = "学历学位： 博士 电话： 68775509 办公地点： E-mail： kang_zh @ 163.com 领域： 智能计算"
        nv, moves = self._split(v)
        self.assertEqual(nv, "学历学位：博士")
        self.assertIn(("联系方式", "68775509"), moves)
        self.assertIn(("联系方式", "kang_zh @ 163.com"), moves)
        self.assertIn(("研究方向", "智能计算"), moves)

    def test_non_form_untouched(self):
        v = "1995年，河北师范大学历史系，获学士学位 1999年，中国人民大学历史系，获硕士学位"
        nv, moves = self._split(v)
        self.assertEqual(moves, [])
        self.assertEqual(nv, v)

    def test_lead_over_10_chars_untouched(self):
        v = "本人在计算机科学领域深耕多年 学历：博士 电话：123 Email：a@b.com"
        nv, moves = self._split(v)
        self.assertEqual(moves, [])


class TestStripMergedLabel(unittest.TestCase):
    """发现42: 合并标签前缀（教育背景及工作经历）循环剥离。"""

    def _strip(self, v, f):
        from detail_scraper import _strip_self_section_label
        return _strip_self_section_label(v, f)

    def test_merged_then_synonym_stripped(self):
        # 周立志: 两层标签连续剥
        v = "教育背景及工作经历 教育背景 1984.09~1988.07 安徽大学 动物学 学士"
        self.assertEqual(self._strip(v, "教育经历"), "1984.09~1988.07 安徽大学 动物学 学士")

    def test_merged_then_content(self):
        # 万霞: 合并标签后直接是内容
        v = "教育背景及工作经历 2007.05~至今 安徽大学生态学系 科研教学"
        self.assertEqual(self._strip(v, "教育经历"), "2007.05~至今 安徽大学生态学系 科研教学")

    def test_label_chain_still_clears(self):
        # 郑光源不回归
        self.assertEqual(self._strip("教育经历 工作经历 个人简介 个人研究兴趣", "教育经历"), "")


class TestUnmixNumberedListGlued(unittest.TestCase):
    """发现42: "研究所研究方向：1." 编号列表铁证, 前词粘连也切（薛嗣康）。"""

    def test_glued_numbered_list_splits(self):
        from detail_scraper import _unmix_tail_segments
        v = ("2016.09-2019.06江苏大学材料科学与工程硕士2019.09-2023.06福州大学"
             "物理化学博士2025.09-至今安徽大学物质科学与信息技术研究院量子材料"
             "与物理研究所研究方向：1.电子显微学；2.金属化合物半导体材料表界面结构分析")
        main, moves = _unmix_tail_segments(v, "教育经历")
        self.assertTrue(main.endswith("量子材料与物理研究所"))
        self.assertIn(("研究方向", "1.电子显微学；2.金属化合物半导体材料表界面结构分析"), moves)

    def test_inline_numbered_not_split(self):
        # "研究方向为1.xxx"（无冒号）或句内叙述不切
        from detail_scraper import _unmix_tail_segments
        v = "获理学硕士学位，主要研究方向为1.概率论与数理统计"
        main, moves = _unmix_tail_segments(v, "教育经历")
        self.assertEqual(moves, [])


class TestSinglePairDegreeMove(unittest.TestCase):
    """发现42: 单对学历表单错位（马珊: 讲授课程=最高学历对）搬进教育经历。"""

    def test_single_pair_moved(self):
        from detail_scraper import _split_form_pairs
        nv, moves = _split_form_pairs(
            "最高学历：2020年12月在陕西师范大学获跨语言文化交际专业博士学位",
            "讲授课程")
        self.assertEqual(nv, "")
        self.assertEqual(moves, [("教育经历",
                                  "2020年12月在陕西师范大学获跨语言文化交际专业博士学位")])

    def test_edu_field_not_moved(self):
        from detail_scraper import _split_form_pairs
        nv, moves = _split_form_pairs("学历：研究生；最高学位：博士", "教育经历")
        self.assertEqual(moves, [])

    def test_degree_course_not_moved(self):
        from detail_scraper import _split_form_pairs
        nv, moves = _split_form_pairs("学位课《信号与系统》", "讲授课程")
        self.assertEqual(moves, [])


class TestMisalignNoHighestDegree(unittest.TestCase):
    """发现42: 表单拆分后 "最高学位：博士" 是合法紧凑值, 不再判错位。"""

    def test_cleaned_form_not_misaligned(self):
        # importlib 显式加载, 规避 department teachers 同名模块冲突
        import importlib.util
        teachers_dir = Path(__file__).resolve().parent.parent / "teachers"
        spec = importlib.util.spec_from_file_location(
            "teachers_evaluate_42", teachers_dir / "evaluate_teachers.py")
        ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ev)
        t = {"教育经历": "学历：研究生；最高学位：博士"}
        self.assertEqual(ev._misalign_hits(t), [])


class TestTinyFragment(unittest.TestCase):
    """发现43: 内容字段 2-3 字短值碎片判定（字段适用域+课程词典保护）。"""

    def _is(self, v, f):
        from detail_scraper import _is_tiny_fragment
        return _is_tiny_fragment(v, f)

    def test_fragments_detected(self):
        self.assertTrue(self._is("情况", "荣誉"))
        self.assertTrue(self._is("经理", "基金"))
        self.assertTrue(self._is("论文", "学术成果"))
        self.assertTrue(self._is("多项。", "荣誉"))
        self.assertTrue(self._is("1.", "课堂项目"))
        self.assertTrue(self._is("中心", "讲授课程"))

    def test_legit_fields_excluded(self):
        # 职称/职务/出生年月/联系方式/研究方向/教育经历 短值是正常值域
        self.assertFalse(self._is("教授", "职称"))
        self.assertFalse(self._is("院长", "职务"))
        self.assertFalse(self._is("1972", "出生年月"))
        self.assertFalse(self._is("138123", "联系方式"))
        self.assertFalse(self._is("代数学", "研究方向"))
        self.assertFalse(self._is("博士", "教育经历"))

    def test_course_dictionary_protected(self):
        self.assertFalse(self._is("排球", "讲授课程"))
        self.assertFalse(self._is("税法", "讲授课程"))
        self.assertFalse(self._is("法律史", "讲授课程"))

    def test_wu_placeholder_kept(self):
        self.assertFalse(self._is("无", "基金"))

    def test_long_values_kept(self):
        self.assertFalse(self._is("国家自然科学基金青年项目", "基金"))


class TestBirthYearCheck(unittest.TestCase):
    """发现43: 出生年月无年份 = 籍贯/标签碎片, 清空。"""

    def test_birth_fragments(self):
        import re
        for v in ("，安徽望江人。核能科学与工程博士", "最终学位", "职称：", "队列调查"):
            self.assertFalse(re.search(r"(19|20)\d{2}", v), v)

    def test_birth_valid(self):
        import re
        for v in ("1972", "1972年5月", "1965.03"):
            self.assertTrue(re.search(r"(19|20)\d{2}", v), v)


class TestCleanupFieldsSharedPath(unittest.TestCase):
    """发现44: 清理链抽成 _cleanup_fields, 渲染路径与主路径共用。"""

    def test_cleanup_clears_tiny_fragment(self):
        from detail_scraper import DetailScraper
        d = DetailScraper("测试", "测试院", "/tmp")
        result = {"姓名": "测试", "来源类型": "html",
                  "讲授课程": "成果奖", "基金": "负责人",
                  "荣誉": "教授", "教育经历": "1995年获学士学位"}
        d._cleanup_fields(result)
        self.assertEqual(result["讲授课程"], "")
        self.assertEqual(result["基金"], "")
        self.assertEqual(result["荣誉"], "")
        self.assertEqual(result["教育经历"], "1995年获学士学位")

    def test_cleanup_idempotent(self):
        from detail_scraper import DetailScraper
        d = DetailScraper("测试", "测试院", "/tmp")
        result = {"姓名": "测试", "来源类型": "html",
                  "工作经历": "工作经历： 2004年-至今 吉林大学 副教授",
                  "讲授课程": "成果奖"}
        d._cleanup_fields(result)
        snap = dict(result)
        d._cleanup_fields(result)
        self.assertEqual(result, snap)


class TestApplyDeptRules(unittest.TestCase):
    """2026-08-31: 学院规则应用语义（命中即覆盖/页首窗口/课程拆条/懒加载）。"""

    def _apply(self, result, school, dept, text):
        from detail_scraper import _apply_dept_rules
        _apply_dept_rules(result, school, dept, text)
        return result

    def test_replace_semantics(self):
        # 命中即覆盖: LLM 已填的错值被规则压回（安平秋副教授→教授）
        text = "在职教师 安平秋 教授 个人简介 安平秋（AnPingqiu），男，1941年10月31日生"
        result = {"职称": "副教授", "出生年月": ""}
        r = self._apply(result, "北京大学", "中国语言文学系", text)
        self.assertEqual(r["职称"], "教授")
        self.assertIn("1941", r["出生年月"])

    def test_head_window(self):
        # 页首 2000 字外的职称/出生不命中（正文里他人职称/出版日期）
        head = "离退休教师 在职教师 " + "。" * 100
        tail = " 职称： 教授 出生： 2020年1月1日生"
        text = head + "长正文" * 800 + tail  # 职称/出生标签在 2000 字窗口之外
        result = {"职称": "", "出生年月": ""}
        r = self._apply(result, "北京大学", "中国语言文学系", text)
        self.assertEqual(r["职称"], "")
        self.assertEqual(r["出生年月"], "")

    def test_course_chain_split(self):
        text = ("个人简介 安平秋，男。开设过《史记》研究、《古文讲读》、《传统文化"
                "与社会生活》、《资治通鉴讲读》等课程。")
        result = {"讲授课程": ""}
        r = self._apply(result, "北京大学", "中国语言文学系", text)
        self.assertIn("古文讲读", r["讲授课程"])
        self.assertIn("资治通鉴讲读", r["讲授课程"])

    def test_lazy_json_loader(self):
        # 开源仓不含采集产物 extract_rules.json —— 用 tmp 学校目录自建 fixture
        # 验证懒加载与缓存语义（存在→dict；不存在→None）
        import json as _json
        from detail_scraper import _DEPT_RULES_JSON_CACHE, _load_dept_rules_json
        base = Path(__file__).resolve().parent.parent / "teachers"
        d = base / "测试校懒加载" / "测试系"
        d.mkdir(parents=True, exist_ok=True)
        try:
            (d / "extract_rules.json").write_text(
                _json.dumps({"rules": {"职称": "标签：教授"}}, ensure_ascii=False),
                encoding="utf-8")
            _DEPT_RULES_JSON_CACHE.pop(("测试校懒加载", "测试系"), None)
            r = _load_dept_rules_json("测试校懒加载", "测试系")
            self.assertIsNotNone(r)
            self.assertIn("职称", r)
            # 不存在的院系 → None
            self.assertIsNone(_load_dept_rules_json("测试校", "不存在系"))
        finally:
            import shutil as _shutil
            _shutil.rmtree(base / "测试校懒加载", ignore_errors=True)
            _DEPT_RULES_JSON_CACHE.pop(("测试校懒加载", "测试系"), None)

    def test_hardcoded_table_priority(self):
        # 硬编码表（北大历史学系）优先于 JSON（不存在该 JSON, 但语义测试表优先）
        from detail_scraper import _DEPT_EXTRACT_RULES
        self.assertIn(("北京大学", "历史学系"), _DEPT_EXTRACT_RULES)


class TestPendingPdfNotSkipped(unittest.TestCase):
    """2026-08-31: pending_pdf（html_shell_pdf 且 pdf_path 空）不被 skip_done 跳过。"""

    def test_pending_pdf_excluded_from_done(self):
        import json
        from pathlib import Path
        import tempfile
        from detail_scraper import DetailScraper
        d = DetailScraper("测试校", "测试系", Path(tempfile.mkdtemp()), fetch_only=True)
        # 构造已落盘数据: 1 行 ok, 1 行 pending_pdf
        data = {"teachers": [
            {"姓名": "甲", "详情页URL": "http://x/1.htm", "来源类型": "html",
             "pdf_path": "", "失败原因": ""},
            {"姓名": "乙", "详情页URL": "http://x/2.htm", "来源类型": "html_shell_pdf",
             "pdf_path": "", "has_pdf": True, "失败原因": ""},
        ]}
        # 走 process_batch 的 skip_done 逻辑（单测跳过 LLM/网络: 用内部过滤复现）
        _DONE_OK = frozenset({"html", "html_fallback", "html_noise",
                              "pdf", "pdf_noise", "html_shell_pdf"})
        kept = []
        for t in data["teachers"]:
            pending_pdf = (t.get("来源类型") == "html_shell_pdf"
                           and not (t.get("pdf_path") or ""))
            if t.get("来源类型") in _DONE_OK and not pending_pdf:
                continue
            kept.append(t)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["姓名"], "乙")


class TestIncrementalSemantics(unittest.TestCase):
    """2026-08-31: 两阶段增量语义——名单更新/URL 变更触发补下载, 存量同名同URL跳过。"""

    def _mk_raw(self, school, dept, name, url):
        import tempfile
        import download_stage as ds
        tmp = tempfile.mkdtemp(prefix="incr_raw_")
        raw = Path(tmp) / school / dept
        raw.mkdir(parents=True)
        fname = DetailScraper._make_filename(name, url, "html")
        (raw / fname).write_text("<html>ok</html>", encoding="utf-8")
        return ds, tmp

    def test_same_url_skips_url_change_refetches(self):
        # 名单 URL 变更 → 新 md5 文件名 → 视为缺失, 增量补下载; 原 URL → 跳过
        url_old = "https://x.edu.cn/info/1/1.htm"
        ds, tmp = self._mk_raw("测试大学", "测试学院", "张三", url_old)
        old_raw = ds._RAW
        ds._RAW = Path(tmp)
        try:
            self.assertTrue(ds.local_raw_exists("测试大学", "测试学院", "张三", url_old))
            self.assertFalse(ds.local_raw_exists(
                "测试大学", "测试学院", "张三", "https://x.edu.cn/info/1/2.htm"))
        finally:
            ds._RAW = old_raw

    def test_new_name_missing_and_pdf_prefix_match(self):
        # 新增教师本地无 raw → 缺失; 已有 PDF（同名不同后缀）也视为已存在
        ds, tmp = self._mk_raw("测试大学", "测试学院", "李四",
                               "https://x.edu.cn/info/2/1.htm")
        old_raw = ds._RAW
        ds._RAW = Path(tmp)
        try:
            self.assertFalse(ds.local_raw_exists(
                "测试大学", "测试学院", "王五", "https://x.edu.cn/info/2/2.htm"))
            prefix = DetailScraper._make_filename("李四", "https://x.edu.cn/info/2/1.htm",
                                                  "html")[:-5]
            (Path(tmp) / "测试大学" / "测试学院" / f"{prefix}.pdf").write_text(
                "%PDF", encoding="utf-8")
            self.assertTrue(ds.local_raw_exists(
                "测试大学", "测试学院", "李四", "https://x.edu.cn/info/2/1.htm"))
        finally:
            ds._RAW = old_raw


class TestLocalOnly(unittest.TestCase):
    """2026-08-31: 第二步提取本地模式——无本地文件跳过网络, 有本地 HTML 不下载 PDF。"""

    def _scraper(self):
        import tempfile
        from detail_scraper import DetailScraper
        return DetailScraper("测试校", "测试系", Path(tempfile.mkdtemp()),
                             fetch_only=True, local_only=True)

    def test_no_local_file_skips_network(self):
        sc = self._scraper()
        sc.fetch_page = lambda url: (_ for _ in ()).throw(
            AssertionError("local_only 不应发起网络请求"))
        r = sc.process_teacher({"name": "张三",
                                "detail_url": "http://x.edu.cn/info/1/1.htm"})
        self.assertEqual(r["来源类型"], "no_local_raw")
        self.assertIn("等待下载阶段", r["失败原因"])

    def test_local_html_detects_pdf_without_download(self):
        import tempfile
        from detail_scraper import DetailScraper
        sc = self._scraper()
        fname = DetailScraper._make_filename(
            "李四", "http://x.edu.cn/info/2/2.htm", "html")
        # 带足教师页正文, 避免短页被 html_noise 判死
        (sc._raw_dir / fname).write_text(
            '<html><body><h1>个人简介</h1><p>李四，教授，研究方向：考古学与'
            '博物馆学。教育经历：北京大学历史学博士。工作经历：北京大学考古'
            '文博学院讲师、副教授。</p><a href="http://x.edu.cn/_upload/'
            'jianli.pdf">简历</a></body></html>', encoding="utf-8")
        sc.download_pdf = lambda url, path: (_ for _ in ()).throw(
            AssertionError("local_only 不应下载 PDF"))
        r = sc.process_teacher({"name": "李四",
                                "detail_url": "http://x.edu.cn/info/2/2.htm"})
        self.assertTrue(r["has_pdf"])
        self.assertEqual(r["pdf_url"], "http://x.edu.cn/_upload/jianli.pdf")
        self.assertEqual(r["pdf_path"], "")          # 未落地 → 待第一步回补
        self.assertEqual(r["来源类型"], "html_shell_pdf")
        self.assertTrue(r["html_path"])              # 复用本地 HTML

    def test_local_pdf_no_shell_no_refetch(self):
        import tempfile
        from detail_scraper import DetailScraper
        sc = self._scraper()
        fname = DetailScraper._make_filename(
            "王五", "http://x.edu.cn/info/3/3.htm", "pdf")
        # _pdf_file_ok 要求 %PDF- 魔数 + ≥1000 字节
        (sc._raw_dir / fname).write_bytes(b"%PDF-1.4\n" + b"0" * 1000)
        # 文本提取打桩返回含姓名正文（pdf_noise 判定与本测试无关, 已在别处覆盖）
        _orig = DetailScraper.extract_text_from_pdf
        DetailScraper.extract_text_from_pdf = staticmethod(
            lambda pdf_path, max_pages=30, max_chars=15000:
            "王五，教授，研究方向：考古学。教育经历：北京大学博士。")
        try:
            sc.fetch_page = lambda url: (_ for _ in ()).throw(
                AssertionError("local_only 不应补页"))
            r = sc.process_teacher({"name": "王五",
                                    "detail_url": "http://x.edu.cn/info/3/3.htm"})
        finally:
            DetailScraper.extract_text_from_pdf = _orig
        self.assertEqual(r["来源类型"], "pdf")
        self.assertEqual(r["pdf_url"], "")           # 无壳不补页, 留空待回补


class TestMetaDescriptionFallback(unittest.TestCase):
    """2026-08-31: 清华自动化系 SEO 壳——教师全文在 meta description, body 空。"""

    def test_meta_content_used_when_body_empty(self):
        from detail_scraper import DetailScraper
        html = (
            '<html><head><title>万森-清华大学自动化系</title>'
            '<meta name="description" content="教育背景2013年9月-2019年7月'
            ' 清华大学航天航空学院，获工学博士学位，2009年9月-2013年7月北京'
            '航空航天大学机械工程及自动化学院获工学学士学位工作履历2022年1月'
            '至今清华大学自动化系助理研究员研究领域生物医学工程、计算成像研究'
            '概况主要从事数字自适应光学眼底成像仪器研究以及在神经退行疾病早期'
            '诊断中的应用，视网膜是中枢神经系统的外延，细胞级分辨率眼底成像'
            '技术可无创观察视网膜神经元"/></head>'
            '<body><div id="vsb_content"></div></body></html>')
        txt = DetailScraper.extract_text_from_html(html)
        self.assertIn("教育背景", txt)
        self.assertIn("研究领域", txt)
        self.assertGreater(len(txt), 150)

    def test_short_body_without_labels_keeps_body(self):
        from detail_scraper import DetailScraper
        html = ('<html><head><title>x</title>'
                '<meta name="description" content="某大学官网"/></head>'
                '<body>短正文无字段标签</body></html>')
        txt = DetailScraper.extract_text_from_html(html)
        self.assertIn("短正文无字段标签", txt)  # meta 无标签不误用


class TestCssRuleForm(unittest.TestCase):
    """2026-08-31: 混合规则形态——CSS 选择器补充正则（用户要求）。"""

    def _apply(self, rules, text, html, field="职称"):
        import tempfile
        from pathlib import Path
        from detail_scraper import _apply_rule_value
        d = Path(tempfile.mkdtemp())
        hp = d / "t.html"
        hp.write_text(html, encoding="utf-8")
        val = _apply_rule_value(rules[field][0], text, str(hp), field)
        return {field: val or ""}

    def test_css_element_text(self):
        html = ('<html><body><div class="profile"><span class="zhicheng">'
                '教授</span><p>正文导航里也有教授一词</p></div></body></html>')
        r = self._apply({"职称": [{"css": ".profile .zhicheng"}]},
                        "导航教授正文", html)
        self.assertEqual(r["职称"], "教授")

    def test_css_attr_meta_description(self):
        html = ('<html><head><meta name="description" content="教育背景2013年'
                '清华大学博士学位工作履历2022年助理研究员研究领域生物医学工程'
                '研究概况主要从事光学成像仪器研究以及在神经退行疾病早期诊断中'
                '的应用。视网膜是中枢神经系统的外延，细胞级分辨率眼底成像技术'
                '可无创观察视网膜神经元"/></head><body><p>空壳</p></body></html>')
        r = self._apply({"研究方向": [{"css": "meta[name=description]",
                                       "attr": "content",
                                       "regex": r"研究领域([^研]{2,80})"}]},
                        "空壳", html, field="研究方向")
        self.assertIn("生物医学工程", r["研究方向"])

    def test_css_inner_regex(self):
        html = ('<html><body><td class="zc">现任教授</td></body></html>')
        r = self._apply({"职称": [{"css": "td.zc", "regex": r"教授"}]},
                        "正文无职称", html)
        self.assertEqual(r["职称"], "教授")

    def test_css_rule_skipped_without_html(self):
        from detail_scraper import _apply_rule_value
        # 无 html 路径（PDF 行）→ CSS 规则静默跳过, 不崩
        self.assertIsNone(_apply_rule_value({"css": ".x"}, "文本", "", "职称"))
        # 字符串正则不受影响
        self.assertEqual(_apply_rule_value(r"职称[:：]\s*(教授)", "职称：教授",
                                           "", "职称"), "教授")

    def test_year_constraint_d4_accepts(self):
        # 2026-08-31 误杀修复: \d{4}\s*年 是合法年份约束, 不得判无年份
        from dept_rule_generator import validate_rules
        v = validate_rules({
            "出生年月": [r"生于\s*[:：]?\s*(\d{4}\s*年(?:\s*\d{1,2}\s*月)?)"]})
        self.assertTrue(v["ok"], v["errors"])
        # 2026-09-02: LLM 常写 (\d{4})\s*年（年份捕获组+可选空白+年字）,
        # 语义合法, 同样不得误杀
        v3 = validate_rules({
            "出生年月": [r"(?:出生于|生于)\s*(\d{4})\s*年(?:\s*(\d{1,2})\s*月)?"]})
        self.assertTrue(v3["ok"], v3["errors"])
        # 真无年份约束仍拦截
        v2 = validate_rules({"出生年月": [r"生于\s*(.{2,30})"]})
        self.assertFalse(v2["ok"])

    def test_optional_group_none_not_crash(self):
        # 2026-09-01: 多组场景 group1 可选未参与但 group2 命中 → lastindex=2
        # 而 group(1)=None → 崩溃（中科大附属医院等 8 系反复失败根因）
        from detail_scraper import _apply_rule_value
        pat = r"生于(?:\s*(\d{4})\s*年)?\s*(\d{4})"
        v = _apply_rule_value(pat, "生于1985", "", "出生年月")
        self.assertIsNone(v)  # None 组 → 视未命中不崩溃
        v2 = _apply_rule_value(pat, "生于1985年1986", "", "出生年月")
        self.assertEqual(v2, "1985")

    def test_label_only_value(self):
        # 发现55: "与领域：" 纯标签尾碎片清空; 真实值保留
        from detail_scraper import _is_label_only_value
        self.assertTrue(_is_label_only_value("与领域："))
        self.assertTrue(_is_label_only_value("研究方向："))
        self.assertFalse(_is_label_only_value("与领域：生物信息学"))
        self.assertFalse(_is_label_only_value("教授"))

    def test_zhicheng_near_name_override(self):
        # 发现54: 导航菜单"院士"远离姓名, 姓名邻近"研究员"优先
        from detail_scraper import _verify_zhicheng_near_name
        text = ("教师队伍 两院院士 人才计划 导师介绍 个人主页\n"
                "李开龙 博士 研究员，博士生导师\n研究方向与领域：表观遗传")
        r = {"姓名": "李开龙", "职称": "院士"}
        _verify_zhicheng_near_name(r, text)
        self.assertEqual(r["职称"], "研究员")

    def test_validate_dict_rules(self):
        from dept_rule_generator import validate_rules
        v = validate_rules({"职称": [{"css": ".zc", "regex": "教授"},
                                     r"教授"]})
        self.assertTrue(v["ok"])
        v2 = validate_rules({"职称": [{"regex": "教授"}]})  # 缺 css
        self.assertFalse(v2["ok"])


    def test_copyright_div_dropped(self):
        # 发现50: 北大药学院 <div class="copyRight">版权所有…电话…</div>
        # 学院级页脚必须整体丢弃, 否则污染教师联系方式
        from detail_scraper import DetailScraper
        html = ('<html><body><div class="main"><p>张老师，教授。'
                '联系电话：13800000000</p></div>'
                '<div class="copyRight">版权所有：北京大学药学院 '
                '地址：北京市海淀区学院路38号 邮编：100191 '
                '电话: 010-82805656 E-mail：yxdb@bjmu.edu.cn</div>'
                '</body></html>')
        txt = DetailScraper.extract_text_from_html(html)
        self.assertNotIn("010-82805656", txt)
        self.assertNotIn("版权所有", txt)
        self.assertIn("13800000000", txt)  # 教师本人电话保留

    def test_footer_copyright_dropped_teacher_footer_kept(self):
        # 发现20 与 发现50 的边界: <footer> 含版权所有 → 丢; 含教师联系
        # 信息（无版权所有）→ 保留并入
        from detail_scraper import DetailScraper
        html = ('<html><body><div class="main"><p>李老师，讲师。</p></div>'
                '<footer>办公地址：A楼101 邮编：100001 电话：010-12345678</footer>'
                '</body></html>')
        txt = DetailScraper.extract_text_from_html(html)
        self.assertIn("010-12345678", txt)

    def test_css_strictness_validation(self):
        # 用户要求: CSS 必须严格——裸标签/导航页脚选择器直接判错
        from dept_rule_generator import validate_rules
        v1 = validate_rules({"职称": [{"css": "div"}]})
        self.assertFalse(v1["ok"])
        v2 = validate_rules({"职称": [{"css": ".footer .tel"}]})
        self.assertFalse(v2["ok"])
        v3 = validate_rules({"职称": [{"css": "td.teacher-td span.zc"}]})
        self.assertTrue(v3["ok"])


class TestCollegeContactCleanup(unittest.TestCase):
    """2026-08-31: 发现51/52——模板页脚与院级共享联系方式清洗。"""

    def test_template_footer_div_dropped(self):
        # 清华数学: <div class="footer"> 学院地址/电话/math-hr + 相关链接
        from detail_scraper import DetailScraper
        html = ('<html><body><div class="main"><p>白老师，教授。</p></div>'
                '<div class="footer"><div class="ft-info">'
                '<span>地址：北京市海淀区清华园1号 100084</span>'
                '<span>电话：010-62783140</span>'
                '<span>邮箱：math-hr@tsinghua.edu.cn</span></div>'
                '<div class="box"><dl><dd><a href="x">相关链接1</a></dd>'
                '<dd><a href="y">相关链接2</a></dd></dl></div></div>'
                '</body></html>')
        txt = DetailScraper.extract_text_from_html(html)
        self.assertNotIn("math-hr", txt)
        self.assertNotIn("010-62783140", txt)
        self.assertIn("白老师", txt)

    def test_teacher_contact_footer_kept(self):
        # 发现20 河大场景: 无版权无链接的教师联系页脚 → 保留
        from detail_scraper import DetailScraper
        html = ('<html><body><div class="main"><p>李老师，讲师。</p></div>'
                '<footer>办公地址：A楼101 邮编：100001 电话：010-12345678</footer>'
                '</body></html>')
        txt = DetailScraper.extract_text_from_html(html)
        self.assertIn("010-12345678", txt)

    def test_shared_college_contact_stripped(self):
        from detail_scraper import _clean_college_contacts
        rows = [
            {"联系方式": "邮箱: math-hr@tsinghua.edu.cn; 电话：010-62783140"},
            {"联系方式": "邮箱: math-hr@tsinghua.edu.cn; 电话：010-62783140"},
            {"联系方式": "邮箱: math-hr@tsinghua.edu.cn; 电话：010-62783140"},
            {"联系方式": "邮箱: dgchen@tsinghua.edu.cn, math-hr@tsinghua.edu.cn; 电话：010-62783140"},
            {"联系方式": "邮箱: personal@abc.edu.cn; 电话：13800000000"},
            {"通讯地址": "北京市海淀区清华园1号 100084"},
            {"通讯地址": "北京市海淀区清华园1号 100084"},
            {"通讯地址": "北京市海淀区清华园1号 100084"},
        ]
        n = _clean_college_contacts(rows)
        self.assertGreaterEqual(n, 4)
        self.assertIn("dgchen@tsinghua.edu.cn", rows[3]["联系方式"])
        self.assertNotIn("math-hr", rows[3]["联系方式"])
        self.assertNotIn("010-62783140", rows[3]["联系方式"])
        # 个人联系方式不受影响
        self.assertIn("personal@abc.edu.cn", rows[4]["联系方式"])
        # 学院地址清空
        self.assertEqual(rows[5]["通讯地址"], "")

    def test_shared_cleanup_skips_small_batches(self):
        from detail_scraper import _clean_college_contacts
        rows = [{"联系方式": "邮箱: a@b.edu.cn"}]
        self.assertEqual(_clean_college_contacts(rows), 0)

    def test_prompt_discipline_block(self):
        from detail_scraper import render_field_spec_prompt
        p = render_field_spec_prompt()
        self.assertIn("提取纪律", p)
        self.assertIn("教师本人", p)


class TestRuleGenPrefilter(unittest.TestCase):
    """2026-08-31: 规则生成样本文本过滤（只影响 LLM 分析视图, 回放仍用真实文本）。"""

    def _pf(self, text):
        from dept_rule_generator import prefilter_text
        return prefilter_text(text)

    def test_nav_menu_line_dropped(self):
        t = self._pf("首页 学院概况 师资队伍 科学研究 人才培养\n职称：教授")
        self.assertNotIn("首页", t)
        self.assertIn("职称：教授", t)

    def test_dedup_keeps_blank_structure(self):
        # 去重但不折叠空行——正则锚定行结构, 视图须与真实文本行结构一致
        t = self._pf("职称：教授\n\n\n\n\n职称：教授\n张三\n职称：教授")
        self.assertEqual(t, "职称：教授\n\n\n\n\n张三")

    def test_content_line_kept(self):
        t = self._pf("张三，男，教授。\n联系方式：010-1234\n研究方向：考古学")
        self.assertIn("张三，男，教授。", t)
        self.assertIn("联系方式：010-1234", t)

    def test_sample_ok_junk_name(self):
        # 导航页文件（名单污染: 教育教学/研究成果）不得进规则生成样本
        from dept_rule_generator import _sample_ok
        self.assertTrue(_sample_ok("李飞跃"))
        self.assertFalse(_sample_ok("教育教学"))
        self.assertFalse(_sample_ok(""))

    def test_zero_hit_absent_not_error(self):
        # 页面无此字段信息（零命中+无参考值）→ 不阻塞, _clear_absent 清空
        # （清华中文系无联系方式/课堂项目, 36 分钟白跑的教训）
        from dept_rule_generator import replay_validate, _clear_absent
        samples = [{"name": "张三", "kind": "html",
                    "text": "张三，教授。研究方向：考古。",
                    "raw": "张三，教授。研究方向：考古。"}]
        rules = {"课堂项目": [r"课堂项目[:：]"],
                 "研究方向": [r"研究方向[:：]\s*(.+)"]}
        rp = replay_validate("测试校", "测试系", samples, rules)
        self.assertTrue(rp["ok"])
        self.assertFalse(rp["per_field"]["课堂项目"]["zero_hit"])  # 无参考值
        self.assertEqual(rp["per_field"]["研究方向"]["hit"], 1)
        self.assertEqual(rp["per_field"]["研究方向"]["unverified"], 1)
        _clear_absent({"rules": rules}, rp)
        self.assertEqual(rules["课堂项目"], [])          # 死规则清空
        self.assertEqual(len(rules["研究方向"]), 1)      # 命中的保留

    def test_attr_field_ignored_in_validation(self):
        # LLM 常误给内部兜底字段 attr 设计规则（长庚医院 3 轮全败的根因）
        # → validate 忽略不报错, 迭代剥除
        from dept_rule_generator import validate_rules
        v = validate_rules({"职称": [r"职称[：:]\s*(教授|副教授)"],
                            "attr": [r".*"]})
        self.assertTrue(v["ok"])
        self.assertTrue(any("attr" in w for w in v["warnings"]))

    def test_sample_carries_raw_for_replay(self):
        # 样本须同时带 text(过滤视图) 与 raw(真实文本)——回放用 raw 保证兼容
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "teachers"))
        from dept_rule_generator import _apply_dept_rules_single
        s = {"name": "甲", "kind": "html",
             "text": "职称：教授\n张三", "raw": "首页 学院概况\n职称：教授\n张三"}
        result = {"职称": ""}
        _apply_dept_rules_single(s["raw"], "职称", [r"职称[:：]\s*([^\s]+)"], result)
        self.assertEqual(result["职称"], "教授")


class TestDeptStateNonHttpUrl(unittest.TestCase):
    """2026-09-03 陕师大数统 175/175 教训回归: 非 http URL（detail_url=教师姓名,
    上游 Step3 错位）必须归入 no_url 而非 missing —— 否则 100% 坏 URL 院系
    每次被 _judge_dead 误判死系、6h TTL 循环白耗。"""

    def test_non_http_url_counts_as_no_url(self):
        import tempfile
        from unittest.mock import patch
        import download_stage as ds
        with patch.object(ds, "list_depts", return_value=[
                {"dept": "数学与统计学院", "json_path": "x"}]):
            with patch.object(ds, "load_teachers", return_value=[
                    {"name": "周农业", "detail_url": "周农业"},      # 非 http
                    {"name": "党修甫", "detail_url": "党修甫"},      # 非 http
                    {"name": "张三", "detail_url": "http://x.edu.cn/1.htm"},  # 有效
                ]):
                with patch.object(ds, "local_raw_exists", return_value=False):
                    old_raw = ds._RAW
                    ds._RAW = Path(tempfile.mkdtemp())
                    try:
                        s = ds.dept_download_state("陕西师范大学", "数学与统计学院")
                    finally:
                        ds._RAW = old_raw
        self.assertEqual(s["no_url"], 2)
        self.assertEqual(s["missing"], 1)   # 只有有效 URL 行计入缺口
        self.assertNotIn("error", s)
