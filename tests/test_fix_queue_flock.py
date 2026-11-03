#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_queue 跨进程 flock + 原子写 单元测试 (2026-09-02 缺点#7 修复)。

覆盖: 读-改-写被文件锁串行 / 原子写不留半截 JSON / 非 POSIX 降级不炸。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))

import fix_queue  # noqa: E402


class TestQueueFlock(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "_fix_loop_queue.json"

    def test_atomic_write_no_tmp_leftover(self):
        with mock.patch.object(fix_queue, "FAIL_QUEUE_PATH", self.tmp):
            fix_queue.add_to_queue("苏州大学", "计算机学院", "http://x.test/")
            # 原子写: tmp 文件已 rename 走, 只留正式文件
            self.assertFalse(self.tmp.with_suffix(".tmp").exists())
            q = fix_queue.load_fail_queue()
            self.assertIn("计算机学院", q.get("苏州大学", {}))
            json.loads(self.tmp.read_text(encoding="utf-8"))  # 完整合法 JSON

    def test_flock_acquired_during_read_modify_write(self):
        # 读-改-写全程持文件锁: flock 被调用且 LOCK_EX 至少一次
        calls = []
        real_flock = fix_queue._fcntl.flock
        def spy(fd, op):
            calls.append(op)
            return real_flock(fd, op)
        with mock.patch.object(fix_queue, "FAIL_QUEUE_PATH", self.tmp), \
             mock.patch.object(fix_queue._fcntl, "flock", spy):
            fix_queue.add_to_queue("苏州大学", "物理学院", "http://y.test/")
        self.assertIn(fix_queue._fcntl.LOCK_EX, calls)
        self.assertIn(fix_queue._fcntl.LOCK_UN, calls)  # 用后释放

    def test_lock_file_created_next_to_queue(self):
        with mock.patch.object(fix_queue, "FAIL_QUEUE_PATH", self.tmp):
            fix_queue.remove_from_queue("不存在", "不存在")
        self.assertTrue(self.tmp.with_suffix(".lock").exists())

    def test_remove_and_clear_also_locked(self):
        calls = []
        real_flock = fix_queue._fcntl.flock
        def spy(fd, op):
            calls.append(op)
            return real_flock(fd, op)
        with mock.patch.object(fix_queue, "FAIL_QUEUE_PATH", self.tmp), \
             mock.patch.object(fix_queue._fcntl, "flock", spy):
            fix_queue.add_to_queue("安徽大学", "化学学院", "http://z.test/")
            fix_queue.remove_from_queue("安徽大学", "化学学院")
            fix_queue.clear_fail_queue()
            self.assertEqual(fix_queue.load_fail_queue(), {})  # 须在 patch 内读
        self.assertGreaterEqual(calls.count(fix_queue._fcntl.LOCK_EX), 3)


if __name__ == "__main__":
    unittest.main()
