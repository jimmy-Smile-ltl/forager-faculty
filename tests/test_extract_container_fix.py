#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R13 回归测试: 提取器容器补全 + 字段全空自动降级（P0-3）。

第3轮 bad case (2026-08-23, 已化名):
- 教师甲/教师乙: 博达 CMS 正文容器 #vsb_content 不在 content_selectors →
  44/65 字真实正文被丢弃, raw_text 只剩导航 → LLM 提取假字段("教授")
  污染 nonempty → 假成功记录(字段全空却算成功)。
- 教师丙: 页面是教研室列表页(正文全是"XX教研室 主任: XX", 教师名只
  出现在列表尾部) → 启发式把"原理教研室 主任: 教师丁"当"讲授课程"。

修复三件套:
1. content_selectors 补 #vsb_content/.v_news_content 等 CMS 容器;
   id 选择器置信度高, 长度下限放宽 100→20(短正文不被误杀)
2. _is_nav_noise_page 放宽 2000→5000(更长导航壳也判噪声)
3. 判死段 meaningful 净化(剔除职称占位值) + 列表页主体性判定
   (名字只出现 1 次且 title 不含名 → 页面主体不是该教师)

运行: cd "college teacher" && uv run python -m unittest discover -s tests -v
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "teachers"))
sys.path.insert(0, str(_PROJ / "util"))

from detail_scraper import (  # noqa: E402
    DetailScraper, _is_nav_noise_page, _is_junk_name, _is_retryable_failure,
    classify_record)

# 博达 CMS 结构: #vsb_content 短正文 + 长导航（真实案例结构, 已化名）
_BODA_HTML = """<html><head><title>教师甲-某师范大学教育科学学院</title></head>
<body>
<nav>首页 新闻中心 通知公告 学院概况 学院简介 组织机构 现任领导 联系方式
师资队伍 教育学系 小学教育系 教育管理与政策系 学前教育学系 教育技术学系
教育科学研究院 道德教育研究所 学工管理 行政管理 人事动态 科学研究 科研通知
学术动态 管理文件 人才培养 本科教育 研究生教育 党建工作 组织架构 党建动态
学生工作 通知公告 国际交流 合作项目 社会服务 合作办学 师资培训 成人教育
校友风采 校友动态 毕业相册 教师通道 事务公开 公告公示 文件公布 English</nav>
<div id="vsb_content" class="article-p wow fadeInUp">
  <div class="v_news_content">
    <p>科研助理，办公电话：025—00000000，邮箱：teacher_a@example.edu.cn</p>
  </div>
</div>
</body></html>"""

# 列表页（列表壳型）: 正文全是教研室名单, 教师名只在列表尾部出现 1 次。
# 含"宗教与宗教学原理教研室"(label"教学"命中) → 启发式必提取假字段
# "讲授课程=原理教研室 主任: 教师丁" → meaningful 非空 → 走主体性判定
_LIST_SHELL_HTML = """<html><head><title>某大学哲学系宗教学系</title></head>
<body><div class="content">
首页 通知 公告 师资队伍 师资力量 组织架构 离退休人员 博士后
马克思主义哲学教研室 主任: 教师一
中国哲学教研室 主任: 教师二
外国哲学教研室 主任: 教师三
伦理学教研室 主任: 教师四
美学教研室 主任: 教师五
佛教与道教教研室 主任: 教师六
宗教与宗教学原理教研室 主任: 教师丁
教师丙 来源: 作者: 时间:
</div></body></html>"""

# 渲染成功救回型: 静态壳只有职称占位, 渲染后才有真实字段
_SHELL_ONLY_TITLE = """<html><head><title>某某-某大学</title></head>
<body><div class="content">首页 通知 公告 学院简介 师资队伍 人才培养 荣誉教授
客座教授 杰出人才 组织机构 现任领导 教师名录 招生就业 校友工作 更多</div></body></html>"""
_RENDERED_HTML = """<html><head><title>某某-某大学</title></head>
<body><div id="vsb_content"><p>某某，教授。</p>
<p>研究方向：人工智能与机器学习。</p>
<p>教育经历：某大学博士。</p>
<p>邮箱：moumou@example.edu.cn</p></div></body></html>"""


def _make_scraper():
    """绕过 __init__ 的 DetailScraper: 启发式提取(无 LLM) + mock 抓取"""
    sc = object.__new__(DetailScraper)
    sc.resume = False
    sc.rate_limit = 0.0
    sc._throttle_lock = threading.Lock()
    sc._next_request_at = 0.0
    sc._csv_dir = Path(tempfile.mkdtemp())
    tmp = Path(tempfile.mkdtemp())
    sc._raw_dir = tmp
    sc.raw_data_root = tmp.parent
    sc._llm = None  # has_llm=False → 启发式提取
    sc._render_blackout = False
    sc._render_empty_streak = 0
    sc._log = lambda *a, **k: None
    return sc


def _process(sc, html, name, rendered=None):
    with patch.object(DetailScraper, "fetch_page", return_value=html), \
         patch.object(DetailScraper, "fetch_page_rendered", return_value=rendered), \
         patch.object(DetailScraper, "_detect_pdf", return_value=None), \
         patch.object(DetailScraper, "_extract_avatar_url", return_value=None):
        return sc.process_teacher({"name": name,
                                   "detail_url": f"http://test.example.test/{name}.htm"})


class TestVsbContentContainer(unittest.TestCase):
    """提取器容器补全: 博达 CMS #vsb_content 短正文必须命中"""

    def test_vsb_content_short_body_extracted(self):
        text = DetailScraper.extract_text_from_html(_BODA_HTML)
        self.assertIn("科研助理", text)          # 正文进入
        self.assertIn("teacher_a@example.edu.cn", text)
        self.assertNotIn("新闻中心", text)       # 导航不进正文

    def test_id_selector_accepts_short_body(self):
        """id 容器 20-100 字: 接受(真实短正文, 案例 44 字)"""
        html = ('<html><body><div id="vsb_content">科研助理，电话：123</div>'
                '<nav>首页 新闻 通知 公告 简介 概况 师资 招生 就业 更多</nav></body></html>')
        text = DetailScraper.extract_text_from_html(html)
        self.assertIn("科研助理", text)

    def test_class_selector_short_body_still_skipped(self):
        """class 容器 <100 字: 仍跳过(导航容器长度下限不变)——短 class
        容器不命中, 走 body 兜底"""
        html = ('<html><body><div class="content">首页 新闻 通知 公告 简介</div>'
                '<div id="vsb_content">真实正文 电话：123</div></body></html>')
        text = DetailScraper.extract_text_from_html(html)
        self.assertIn("真实正文", text)

    def test_v_news_content_hit(self):
        """博达次容器 .v_news_content 同样命中"""
        html = ('<html><body><div class="v_news_content">'
                '研究方向：机器学习。<p>邮箱：a@b.c</p></div></body></html>')
        text = DetailScraper.extract_text_from_html(html)
        self.assertIn("研究方向", text)
        self.assertIn("a@b.c", text)


class TestNavNoisePageWidened(unittest.TestCase):
    """_is_nav_noise_page 放宽 2000→5000: 长导航壳也判噪声, 真正文不误杀"""

    _NAV = " 首页 新闻 通知 公告 简介 概况 师资队伍 人才培养 科学研究 "

    def test_3000_char_nav_shell_detected(self):
        """R13 前 2000 字上限挡掉的长导航壳, 现在必须判为噪声页"""
        text = self._NAV * 88  # ~3000 字
        self.assertLess(len(text), 5000)
        self.assertTrue(_is_nav_noise_page(text))

    def test_over_5000_not_judged(self):
        text = self._NAV * 700  # >5000 字
        self.assertFalse(_is_nav_noise_page(text))

    def test_real_long_body_not_judged(self):
        """真实长正文(导航词 token 占比低)不得误判"""
        text = ("该教师长期从事人工智能与机器学习方向的研究，发表论文多篇。" * 60) + self._NAV * 10
        self.assertLess(len(text), 5000)
        self.assertFalse(_is_nav_noise_page(text))

    def test_short_nav_shell_still_detected(self):
        """原范围内(≤2000)的导航壳行为不变"""
        self.assertTrue(_is_nav_noise_page(self._NAV * 10))


class TestProcessTeacherR13(unittest.TestCase):
    """端到端: 博达正文救回 / 列表壳正确判死 / 渲染救回不误判"""

    def test_boda_body_extracted_ok(self):
        """博达型: 44 字正文容器命中 → 真实字段提取 → 成功"""
        sc = _make_scraper()
        r = _process(sc, _BODA_HTML, "教师甲")
        self.assertEqual(r["来源类型"], "html")
        self.assertEqual(classify_record(r), "ok")
        self.assertIn("teacher_a@example.edu.cn", r.get("联系方式") or "")

    def test_list_shell_page_judged_noise(self):
        """列表壳型: 列表页(名字 1 次 + title 不含名) → 正确判死 html_noise"""
        sc = _make_scraper()
        r = _process(sc, _LIST_SHELL_HTML, "教师丙")
        self.assertEqual(r["来源类型"], "html_noise")
        self.assertEqual(classify_record(r), "failed")
        self.assertIn("页面不含教师姓名", r.get("失败原因") or "")

    def test_title_only_placeholder_dead_without_render(self):
        """静态壳只有职称占位 + 渲染无果 → 判死(meaningful 净化)"""
        sc = _make_scraper()
        r = _process(sc, _SHELL_ONLY_TITLE, "某某")
        self.assertEqual(r["来源类型"], "html_noise")
        self.assertEqual(classify_record(r), "failed")

    def test_render_rescue_not_judged_dead(self):
        """占位 + 渲染成功提取真实字段 → 不得按渲染前快照判死
        (title_only_now 修复: 原逻辑在"占位+渲染成功"边界误杀)"""
        sc = _make_scraper()
        r = _process(sc, _SHELL_ONLY_TITLE, "某某", rendered=_RENDERED_HTML)
        self.assertEqual(r["来源类型"], "html")
        self.assertEqual(classify_record(r), "ok")
        self.assertIn("人工智能", r.get("研究方向") or "")

    def test_render_failed_still_dead(self):
        """占位 + 渲染失败 → 判死(渲染尝试过仍无正文)"""
        sc = _make_scraper()
        r = _process(sc, _SHELL_ONLY_TITLE, "某某", rendered=_SHELL_ONLY_TITLE)
        self.assertEqual(r["来源类型"], "html_noise")


class TestJunkNameRejected(unittest.TestCase):
    """P0-1: 名单垃圾词(非人名) → 不发起请求, 立即判死归因"名单垃圾词"。

    第3轮: 西交"正版软件"(URL 指向学校主页 ms.xjtu.test)、上财
    "联系电话"; 东北农大"二级党校"。都是上游名单数据问题, agent 不应
    对垃圾词发起真实请求(R11 无效 URL 同款设计)。"""

    def test_junk_names_flagged(self):
        for n in ("正版软件", "联系电话", "二级党校", "本科教学",
                  "教师名录", "师资队伍", "研究生", "报名系统",
                  "党校", "讲堂"):
            self.assertTrue(_is_junk_name(n), n)

    def test_real_names_not_flagged(self):
        """防误杀: 含"建设/中心/工作/管理"等字的真实姓名必须放行
        （这些词刻意排除在人名校验子串表外, R1 教训）"""
        for n in ("张建国", "王中心", "李管理", "王海洋", "张伟",
                  "欧阳修", "刘静宜", "郑光源", "陈志远", "周文瀚"):
            self.assertFalse(_is_junk_name(n), n)

    def test_empty_not_flagged(self):
        self.assertFalse(_is_junk_name(""))
        self.assertFalse(_is_junk_name("未知"))

    def test_process_teacher_rejects_without_request(self):
        """端到端: 垃圾词判死 fetch_failed, 不触发 URL 校验也不请求"""
        sc = _make_scraper()
        with patch.object(DetailScraper, "fetch_page", side_effect=AssertionError("不应发起请求")):
            r = sc.process_teacher({"name": "正版软件", "detail_url": "http://ms.xjtu.test/"})
        self.assertEqual(r["来源类型"], "fetch_failed")
        self.assertIn("名单垃圾词", r["失败原因"])
        self.assertEqual(classify_record(r), "failed")

    def test_real_name_still_goes_to_url_check(self):
        """真实姓名不受影响: 走 URL 校验分支(R11 已有测试覆盖)"""
        sc = _make_scraper()
        r = sc.process_teacher({"name": "张伟", "detail_url": "国家卓越工程师学院"})
        self.assertIn("名单URL无效", r["失败原因"])


class TestRequeueFailed(unittest.TestCase):
    """P1-6: 失败重跑队列——瞬时故障重试 1 次, 确定性失败不重试"""

    def _rec(self, stype="fetch_failed", reason=""):
        return {"姓名": "x", "来源类型": stype, "失败原因": reason}

    def test_retryable_timeout(self):
        self.assertTrue(_is_retryable_failure(self._rec(reason="fetch: 超时(重试2次后仍失败)")))

    def test_retryable_empty(self):
        self.assertTrue(_is_retryable_failure(self._rec(reason="fetch: 返回空/失败")))

    def test_retryable_conn_error(self):
        self.assertTrue(_is_retryable_failure(self._rec(reason="fetch: ConnectionError(重试2次后仍失败)")))

    def test_not_retryable_http_status(self):
        """4xx/5xx 确定性失败(HttpStatusError 快速失败)——重试不会改变结果"""
        for code in (404, 410, 500, 502):
            self.assertFalse(_is_retryable_failure(self._rec(reason=f"fetch: HTTP {code}")))

    def test_not_retryable_ruishu_status(self):
        """412 是反爬拦截非 URL 失效, fetch_page 返回 None 走"返回空/失败"
        路径(可重试); 但若已归因为 HTTP 4xx 则不重试"""
        self.assertFalse(_is_retryable_failure(self._rec(reason="fetch: HTTP 412")))

    def test_not_retryable_list_issues(self):
        """名单数据问题(无效 URL/垃圾词)——重试同样失败"""
        self.assertFalse(_is_retryable_failure(self._rec(reason="名单URL无效(非http): xxx")))
        self.assertFalse(_is_retryable_failure(self._rec(reason="名单垃圾词(非人名): 正版软件")))

    def test_not_retryable_non_fetch(self):
        self.assertFalse(_is_retryable_failure(self._rec(stype="html_noise")))
        self.assertFalse(_is_retryable_failure(self._rec(stype="ok", reason="")))
        self.assertFalse(_is_retryable_failure(self._rec(stype="no_url")))

    def test_requeue_replaces_and_retries(self):
        """process_batch: 瞬时失败重跑成功 → 结果替换; 确定性失败不重跑"""
        sc = _make_scraper()
        teachers = [
            {"name": "瞬时", "detail_url": "http://x.test/a.htm"},
            {"name": "404", "detail_url": "http://x.test/b.htm"},
        ]
        first = [
            self._rec(reason="fetch: 超时(重试2次后仍失败)"),
            self._rec(reason="fetch: HTTP 404"),
        ]
        # 重跑成功 → 替换为 ok; process_teacher 用 _BODA_HTML(博达型)
        with patch.object(DetailScraper, "process_teacher", return_value=self._rec(stype="html", reason="")) as m_pt:
            results = sc._requeue_failed(teachers, list(first))
        self.assertEqual(results[0]["来源类型"], "html")   # 替换成功
        self.assertEqual(results[1]["来源类型"], "fetch_failed")  # 404 未重跑
        self.assertEqual(m_pt.call_count, 1)              # 只重试 1 个


if __name__ == "__main__":
    unittest.main(verbosity=2)
