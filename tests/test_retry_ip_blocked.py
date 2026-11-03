#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IP 封禁错峰复查 单元测试 (2026-09-02 缺点#4 修复)。

覆盖: 冷却已过且未达上限 → 复活(删 .fail_mark, 留账本) / 未到冷却期保持 /
已达上限保持 / dry-run 不动文件 / 账本损坏兜底。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))
sys.path.insert(0, str(_PROJ / "department teachers" / "script"))

import retry_ip_blocked as rib  # noqa: E402


class TestIpBlockSweep(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.dept = self.out / "南京师范大学" / "数学科学学院"
        self.dept.mkdir(parents=True)
        (self.dept / ".fail_mark").write_text("x", encoding="utf-8")

    def _mark(self, ts, att):
        (self.dept / ".ip_block_mark").write_text(f"{int(ts)}\n{att}",
                                                  encoding="utf-8")

    def test_cooled_down_revives_and_keeps_ledger(self):
        self._mark(time.time() - 3600 * 10, 1)  # 10h 前封禁, 第 1 次
        with mock.patch.object(rib, "OUT", self.out):
            r, k = rib.sweep(apply=True)
        self.assertEqual(r, 1)
        self.assertFalse((self.dept / ".fail_mark").exists())  # 复活
        self.assertTrue((self.dept / ".ip_block_mark").exists())  # 账本保留

    def test_within_cooldown_kept(self):
        self._mark(time.time() - 60, 1)  # 1 分钟前, 冷却未过
        with mock.patch.object(rib, "OUT", self.out):
            r, k = rib.sweep(apply=True)
        self.assertEqual(r, 0)
        self.assertTrue((self.dept / ".fail_mark").exists())

    def test_max_attempts_kept(self):
        self._mark(time.time() - 3600 * 10, 3)  # 已达上限 3
        with mock.patch.object(rib, "OUT", self.out):
            r, k = rib.sweep(apply=True)
        self.assertEqual(r, 0)
        self.assertTrue((self.dept / ".fail_mark").exists())

    def test_dry_run_does_not_touch(self):
        self._mark(time.time() - 3600 * 10, 1)
        with mock.patch.object(rib, "OUT", self.out):
            r, k = rib.sweep(apply=False)
        self.assertEqual(r, 1)
        self.assertTrue((self.dept / ".fail_mark").exists())  # dry-run 未落盘

    def test_corrupt_ledger_parses_as_attempt_1(self):
        (self.dept / ".ip_block_mark").write_text("garbage", encoding="utf-8")
        self.assertEqual(rib._parse(self.dept / ".ip_block_mark"), (0, 1))

    def test_already_revived_no_fail_mark_counted_kept(self):
        self._mark(time.time() - 3600 * 10, 1)
        (self.dept / ".fail_mark").unlink()  # 上轮已复活, 本轮无 fail_mark
        with mock.patch.object(rib, "OUT", self.out):
            r, k = rib.sweep(apply=True)
        self.assertEqual(r, 0)


if __name__ == "__main__":
    unittest.main()
