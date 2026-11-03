#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collect_teacher.py 验证契约回归测试（第2轮北大哲学系教训）。

覆盖:
- _write_csv_from_stdout: spider.py stdout JSON 数组 → 落盘标准8列 CSV
  (LLM 修正可能丢掉 CSV 写入逻辑, 验证必须与输出形式解耦)
- _evaluate_final_output: URL 列精确匹配, "师资页URL"列不得被误计为详情URL
- sanitize_teachers_csv: 学校/学院传 None → 目录名推断, 占位符不污染数据

运行: cd "college teacher" && uv run python -m unittest discover -s tests -v
"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))

import collect_teacher as ct  # noqa: E402


class TestWriteCsvFromStdout(unittest.TestCase):
    """北大哲学系教训: 修正版 spider.py 丢了 CSV 写入, 只 print JSON。
    验证必须解析 stdout JSON 数组并落盘, 否则修正永远读旧 CSV 空转。"""

    def test_json_array_lands_csv(self):
        out = Path(tempfile.mkdtemp())
        stdout = json.dumps([
            {"name": "钱学理", "title": "教授",
             "detail_url": "https://phil.pku.edu.cn/szdw/szll/mkszyzxjys/69Religiousstudies274778.htm"},
            {"name": "孙春华", "title": "教授",
             "detail_url": "https://phil.pku.edu.cn/szdw/szll/mkszyzxjys/69Religiousstudies274706.htm"},
            {"name": "无URL教师", "title": "", "detail_url": ""},
        ])
        self.assertTrue(ct._write_csv_from_stdout(out, stdout))
        rows = list(csv.reader(open(out / "teachers.csv", encoding="utf-8")))
        self.assertEqual(rows[0], ["大学", "学院", "教师姓名", "职务职称", "所属院系",
                                   "个人详情URL", "师资页URL", "采集时间"])
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[1][5].startswith("https://phil.pku.edu.cn/szdw/szll/"))
        self.assertEqual(rows[3][5], "")  # 无URL行保留, 详情URL为空

    def test_non_json_stdout_not_overwrite(self):
        out = Path(tempfile.mkdtemp())
        (out / "teachers.csv").write_text("既有内容", encoding="utf-8")
        self.assertFalse(ct._write_csv_from_stdout(out, "成功采集 65 条教师数据"))
        self.assertEqual((out / "teachers.csv").read_text(encoding="utf-8"), "既有内容")

    def test_empty_or_dict_stdout_rejected(self):
        out = Path(tempfile.mkdtemp())
        self.assertFalse(ct._write_csv_from_stdout(out, ""))
        self.assertFalse(ct._write_csv_from_stdout(out, '{"name": "x"}'))  # dict 非数组
        self.assertFalse(ct._write_csv_from_stdout(out, "[1, 2]"))          # 元素非 dict
        self.assertFalse((out / "teachers.csv").exists())


class TestFinalOutputColumnMatch(unittest.TestCase):
    """师资页URL列含"URL"字样, 宽匹配会选中它 → 假 URL 覆盖率 100%
    (第2轮北大的 P4.5 门禁曾因此差点误判通过)。"""

    @staticmethod
    def _write(out, rows):
        with open(out / "teachers.csv", "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["大学", "学院", "教师姓名", "职务职称", "所属院系",
                        "个人详情URL", "师资页URL", "采集时间"])
            w.writerows(rows)

    def test_faculty_url_column_not_counted(self):
        out = Path(tempfile.mkdtemp())
        self._write(out, [
            ["北京大学", "哲学系", "钱学理", "教授", "学院",
             "https://x.test/a.htm", "https://x.test/szdw/szll/index.htm", "t"],
            ["北京大学", "哲学系", "孙春华", "教授", "学院",
             "", "https://x.test/szdw/szll/index.htm", "t"],
        ])
        q = ct._evaluate_final_output(out, "哲学系")
        self.assertEqual(q["url_ok"], 1)   # 只有详情URL算, 师资页URL不算
        self.assertEqual(q["url_rate"], 0.5)

    def test_detail_url_matched_first(self):
        out = Path(tempfile.mkdtemp())
        self._write(out, [
            ["北京大学", "哲学系", "钱学理", "教授", "学院",
             "https://x.test/detail.htm", "https://x.test/fac.htm", "t"],
        ])
        q = ct._evaluate_final_output(out, "哲学系")
        self.assertEqual(q["url_ok"], 1)
        self.assertEqual(q["url_rate"], 1.0)


class TestSanitizeDirNameFallback(unittest.TestCase):
    """sanitize_teachers_csv 的 school/dept 为 None 时用目录名推断,
    配合 stdout 兜底落盘(学校/学院列为空)不产出"学校"占位符。"""

    def test_none_falls_back_to_dir_names(self):
        tmp = Path(tempfile.mkdtemp())
        out = tmp / "北京大学" / "哲学系（宗教学系）"
        out.mkdir(parents=True)
        ct._write_csv_from_stdout(out, json.dumps([
            {"name": "钱学理", "title": "教授", "detail_url": "https://x.test/a.htm"}]))
        total, url_ok, nav, fake, dup = ct.sanitize_teachers_csv(out, None, None)
        self.assertEqual(total, 1)
        self.assertEqual(url_ok, 1)
        rows = list(csv.reader(open(out / "teachers.csv", encoding="utf-8")))
        self.assertEqual(rows[1][0], "北京大学")
        self.assertEqual(rows[1][1], "哲学系（宗教学系）")
        self.assertNotIn("学校", rows[1][0])  # 占位符不污染


if __name__ == "__main__":
    unittest.main(verbosity=2)
