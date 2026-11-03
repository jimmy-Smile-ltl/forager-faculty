#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IP 封禁账本写入 单元测试 (2026-09-02 缺点#4 修复, fix_loop 侧)。

覆盖: 首次封禁写 ts+1 / 重复封禁累计次数 / 损坏账本兜底重写。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))

import fix_loop  # noqa: E402


class TestIpBlockMark(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def _read(self):
        lines = (self.d / ".ip_block_mark").read_text(encoding="utf-8").strip().splitlines()
        return int(lines[0]), int(lines[1]) if len(lines) > 1 else 1

    def test_first_fail_writes_ts_and_attempt_1(self):
        fix_loop._write_ip_block_mark(self.d)
        ts, att = self._read()
        self.assertEqual(att, 1)
        self.assertLess(abs(ts - int(time.time())), 10)  # 时间戳为当前

    def test_repeat_fail_increments_attempts(self):
        fix_loop._write_ip_block_mark(self.d)
        fix_loop._write_ip_block_mark(self.d)
        ts, att = self._read()
        self.assertEqual(att, 2)

    def test_corrupt_ledger_resets_to_1(self):
        (self.d / ".ip_block_mark").write_text("nonsense", encoding="utf-8")
        fix_loop._write_ip_block_mark(self.d)
        ts, att = self._read()
        self.assertEqual(att, 1)


if __name__ == "__main__":
    unittest.main()
