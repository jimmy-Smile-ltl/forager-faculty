"""filter_roster 下载侧过滤规则测试（2026-10-24 豁免/共享/映射三族）。

覆盖:
  A. list.htm/list.psp 详情页豁免（前段=拼音放行/栏目词拒/per-host 拦）
  B. 医院 CMS 文章页 /list/数字/数字.html 豁免
  C. 纯数字段共享名单页拒绝（mphy.ustc 111 人共享 2841）
  D. 名录重建映射提前应用（E 类名单页 URL → 映射真实详情 URL）
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_BASE = Path(__file__).resolve().parent.parent / "teachers"
sys.path.insert(0, str(_BASE))
sys.path.insert(0, str(_BASE / "scripts"))
sys.path.insert(0, str(_BASE.parent))  # util.*

from util.url_rewrites import (  # noqa: E402
    is_list_htm_person, is_list_htm_numeric, is_list_article_url)
from scripts.upstream_refresh import filter_roster  # noqa: E402

# 2026-10-26 LLM 人名终审: 单元测试不依赖 LLM 网关——全局 mock 返回
# 空集(LLM 行为由 test_llm_name_judge 单独测), 否则测试名(独享者/
# 垃圾名等占位名)会被 LLM 判非人名干扰断言。
_LLM_JUDGE_PATCHER = patch("scripts.upstream_refresh.llm_judge_nonperson",
                           return_value=set())
_LLM_JUDGE_PATCHER.start()


def tearDownModule():
    _LLM_JUDGE_PATCHER.stop()


class TestListHtmExemption(unittest.TestCase):
    """A. list.htm/list.psp 详情页豁免判别。"""

    def test_pinyin_segment_passes(self):
        self.assertTrue(is_list_htm_person(
            "https://js.chd.edu.cn/gcjxxy/czy/list.htm"))  # 蔡宗琰
        self.assertTrue(is_list_htm_person(
            "http://edu.nju.edu.cn/cts/list.htm"))        # 操太圣
        self.assertTrue(is_list_htm_person(
            "http://life.nju.edu.cn/asq1/list.htm"))      # 拼音+数字尾
        self.assertTrue(is_list_htm_person(
            "http://em.seu.edu.cn/cfl_36350/list.htm"))   # 下划线形态
        self.assertTrue(is_list_htm_person(
            "https://faculty.jnu.edu.cn/lgxy/sl/list.psp"))  # psp 变体

    def test_column_segments_rejected(self):
        # 全局栏目词
        self.assertFalse(is_list_htm_person(
            "http://ssrc.gzu.edu.cn/szdw/list.htm"))
        self.assertFalse(is_list_htm_person(
            "https://surfchem.ustc.edu.cn/szll/list.htm"))
        self.assertFalse(is_list_htm_person(
            "https://lxafs.ustc.edu.cn/Members/list.htm"))
        # per-host 栏目（qzjs 在别的 host 是拼音, 只在 le.ustc 拦）
        self.assertFalse(is_list_htm_person(
            "http://le.ustc.edu.cn/qzjs/list.htm"))
        self.assertFalse(is_list_htm_person(
            "http://sist.shanghaitech.edu.cn/szdwx/list.htm?col=0"))
        self.assertFalse(is_list_htm_person(
            "http://math.shufe.edu.cn/xz/list.htm#"))

    def test_non_list_urls_unaffected(self):
        # 非 list.htm 形态一律 False（不改变既有判定）
        self.assertFalse(is_list_htm_person(
            "https://x.edu.cn/info/1234/5678.htm"))
        self.assertFalse(is_list_htm_person(
            "http://www.fzsdeyy.com/jyzn/zjfc/zjfcList.html"))  # .html 非 .htm
        self.assertFalse(is_list_htm_person(""))
        self.assertFalse(is_list_htm_person("not a url"))


class TestArticleUrlExemption(unittest.TestCase):
    """B. 医院 CMS 文章页 /list/数字/数字.html。"""

    def test_article_detail_passes(self):
        self.assertTrue(is_list_article_url(
            "http://www.czfph.com/list/5931/20242.html"))
        self.assertTrue(is_list_article_url(
            "https://www.ncsdyyy.com/list/464/13466.html"))
        self.assertTrue(is_list_article_url(
            "http://www.pumcskin.cn/list/33/890.html"))

    def test_other_paths_rejected(self):
        self.assertFalse(is_list_article_url(
            "http://x.edu.cn/list/5931.htm"))          # 单级数字
        self.assertFalse(is_list_article_url(
            "http://x.edu.cn/szdw/list.htm"))
        self.assertFalse(is_list_article_url(""))


class TestNumericSharedSegments(unittest.TestCase):
    """C. 纯数字段 /NNNN/list.htm 两可——名单级共享度判定。"""

    def test_numeric_detection(self):
        self.assertTrue(is_list_htm_numeric(
            "https://mphy.ustc.edu.cn/2841/list.htm"))
        self.assertTrue(is_list_htm_numeric(
            "https://lsi.zju.edu.cn/25413/list.htm"))
        self.assertFalse(is_list_htm_numeric(
            "http://life.nju.edu.cn/asq1/list.htm"))   # 拼音段
        self.assertFalse(is_list_htm_numeric(
            "http://x.edu.cn/info/1/2.htm"))

    def test_shared_numeric_roster_rejected(self):
        names = ["卢建新", "完绍龙", "王安民", "卢明旺", "王小飞"]
        roster = [{"name": n, "detail_url": "https://mphy.ustc.edu.cn/2841/list.htm"}
                  for n in names]
        roster.append({"name": "独享者",
                       "detail_url": "https://mphy.ustc.edu.cn/2850/list.htm"})
        ok, rej = filter_roster(roster)
        self.assertEqual([t["name"] for t in ok], ["独享者"])
        self.assertTrue(all(r[2] == "列表页URL(数字段共享)" for r in rej))
        self.assertEqual(len(rej), 5)

    def test_unique_numeric_passes(self):
        roster = [
            {"name": "张三", "detail_url": "https://lsi.zju.edu.cn/25413/list.htm"},
            {"name": "李四", "detail_url": "https://lsi.zju.edu.cn/25421/list.htm"},
        ]
        ok, rej = filter_roster(roster)
        self.assertEqual(len(ok), 2)
        self.assertEqual(len(rej), 0)


class TestNameMapEarlyApply(unittest.TestCase):
    """D. 名录映射提前应用: 名单页 URL 被拒前用映射 URL 判定。"""

    def _mock_maps(self, maps):
        return patch("scripts.upstream_refresh._load_name_url_maps",
                     return_value=maps)

    def test_listpage_url_with_map_passes(self):
        # 地大资源: 全员 szdw.htm(列表页URL 必拒), 映射到真实详情页
        maps = {"zyxy.cug.edu.cn": {"王地质": "http://zyxy.cug.edu.cn/info/1064/2274.htm"}}
        roster = [{"name": "王地质", "detail_url": "http://zyxy.cug.edu.cn/szdw.htm"}]
        with self._mock_maps(maps):
            ok, rej = filter_roster(roster)
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["detail_url"],
                         "http://zyxy.cug.edu.cn/info/1064/2274.htm")
        self.assertEqual(len(rej), 0)

    def test_listpage_url_without_map_rejected(self):
        roster = [{"name": "王地质", "detail_url": "http://zyxy.cug.edu.cn/szdw.htm"}]
        with self._mock_maps({}):
            ok, rej = filter_roster(roster)
        self.assertEqual(len(ok), 0)
        self.assertEqual(rej[0][2], "列表页URL")

    def test_map_to_listhtm_form_passes(self):
        # 河海历史场景: 映射后 URL 是 /xxx/list.htm 详情页形态——豁免放行
        maps = {"shxy.hhu.edu.cn": {"张三": "http://shxy.hhu.edu.cn/zhangsan/list.htm"}}
        roster = [{"name": "张三", "detail_url": "http://shxy.hhu.edu.cn/szdw.htm"}]
        with self._mock_maps(maps):
            ok, rej = filter_roster(roster)
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["detail_url"],
                         "http://shxy.hhu.edu.cn/zhangsan/list.htm")

    def test_mixed_family_roster(self):
        """混合名单: 豁免/拒绝/映射三类同批。"""
        maps = {"zyxy.cug.edu.cn": {"王地质": "http://zyxy.cug.edu.cn/info/1064/2274.htm"}}
        roster = [
            {"name": "蔡宗琰", "detail_url": "https://js.chd.edu.cn/gcjxxy/czy/list.htm"},
            {"name": "李化学", "detail_url": "https://surfchem.ustc.edu.cn/szll/list.htm"},
            {"name": "王地质", "detail_url": "http://zyxy.cug.edu.cn/szdw.htm"},
            {"name": "石欢", "detail_url": "http://www.czfph.com/list/217/22445.html"},
            {"name": "普通教师", "detail_url": "https://x.edu.cn/info/1234/5678.htm"},
            {"name": "垃圾名", "detail_url": "https://x.edu.cn/info/1/2.htm"},
        ]
        with self._mock_maps(maps):
            ok, rej = filter_roster(roster)
        self.assertEqual(len(ok), 5)  # 垃圾名 被非人名拒
        self.assertEqual(len(rej), 1)


if __name__ == "__main__":
    unittest.main()
