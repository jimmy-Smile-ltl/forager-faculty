#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器治理 09-08 新增机制回归测试 (用户指令: 核查浏览器窗口/tab 管理策略)。

覆盖(纯 mock, 不拉起真浏览器 — 测试守卫拦 Popen Chrome 约定):
  1. drission_pool 全局窗口上限: 唯一 user-data-dir 计数 + 超限拒绝
  2. 独立 Chromium: 个人 Chrome 未运行也允许拉起 9222(守卫已删)
  3. flock 并发建窗互斥: 后到者等前窗起来后再检查(计数口径验证)
  4. iv8 求解槽信号量: 槽满立即返回空串(拒绝不排队)
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))
sys.path.insert(0, str(_PROJ))
sys.path.insert(0, str(_PROJ / "util"))


class TestDrissionGlobalWindowCap(unittest.TestCase):
    """_new_window 全局上限: 按唯一 user-data-dir 计数(非进程数)"""

    @patch("drission_pool._os.environ.get", return_value="4")
    def test_refuse_when_at_cap(self, _env):
        import drission_pool
        # ps aux 输出 4 个不同窗口目录 → 已达上限 → 拒绝建窗
        _ps_out = "\n".join(
            f"chrome --user-data-dir=/tmp/DrissionPage/userData/{i} --headless"
            for i in (1001, 1002, 1003, 1004))
        # 另加 renderer helper(同目录多进程) 验证不会重复计数
        _ps_out += "\n" + "chrome-helper --user-data-dir=/tmp/DrissionPage/userData/1001"
        with patch("subprocess.run") as run, \
             patch("util.browser_lifecycle.personal_chrome_running", return_value=True):
            run.return_value.stdout = _ps_out
            self.assertIsNone(drission_pool._new_window(0))

    def test_count_unique_dirs_not_processes(self):
        import drission_pool
        # 1 个窗口目录 + 10 个 helper 进程(同目录) → 计数应为 1 不是 11
        _ps_out = "chrome --user-data-dir=/tmp/DrissionPage/userData/1001 --headless\n"
        _ps_out += "\n".join(
            f"chrome-helper --user-data-dir=/tmp/DrissionPage/userData/1001 --type=renderer"
            for _ in range(10))
        with patch("subprocess.run") as run:
            run.return_value.stdout = _ps_out
            self.assertEqual(drission_pool._global_window_count(), 1)

    def test_count_zero_when_ps_fails(self):
        import drission_pool
        with patch("subprocess.run", side_effect=Exception("ps broken")):
            self.assertEqual(drission_pool._global_window_count(), 0)


class TestPersonalChromePolicy(unittest.TestCase):
    """2026-09-10 架构升级: 项目浏览器改用独立 Chromium(不同 bundle ID,
    不共享 macOS 单例) — 「个人 Chrome 未运行拒绝启动」守卫整套删除,
    个人 Chrome 开不开都允许项目浏览器独立存在。"""

    def test_9222_launches_even_when_chrome_closed(self):
        import browser_lifecycle
        # 端口未监听 + Popen mock: 应直接拉起(守卫已删), 等端口即返回 True
        # side_effect: 入口检查 False → 锁内复查 False → 拉起后轮询 True
        # 2026-10-25 修: 测试环境无独立 Chromium(PROJECT_CHROME 空)会拒绝
        # 拉起——patch 为非空; _clean_profile_volatile patch 掉防误触真实
        # /tmp profile
        with patch.object(browser_lifecycle, "port_listening",
                          side_effect=[False, False, False, True]), \
             patch.object(browser_lifecycle, "PROJECT_CHROME",
                          "/fake/path/Chromium"), \
             patch.object(browser_lifecycle, "_clean_profile_volatile"), \
             patch("subprocess.Popen") as popen:
            self.assertTrue(browser_lifecycle.ensure_9222())
            popen.assert_called_once()


class TestIv8SolveSemaphore(unittest.TestCase):
    """iv8 求解槽: 并发上限 3, 槽满立即返回空串(拒绝不排队)"""

    def test_slot_exhausted_returns_empty(self):
        import iv8_rs
        # 占满 3 槽
        held = [iv8_rs._IV8_SEM.acquire(blocking=False) for _ in range(3)]
        self.assertTrue(all(held))
        try:
            self.assertEqual(iv8_rs.fetch_with_iv8("http://example.edu.cn/", 20), "")
        finally:
            for _ in held:
                iv8_rs._IV8_SEM.release()

    def test_slot_available_proceeds(self):
        import iv8_rs
        # 无竞争者时走求解(内部 _fetch 被 mock, 挑战页 → 交上层空串)
        with patch("iv8_rs._load_cookie", return_value={}), \
             patch("iv8_rs._fetch", return_value=("", 412, {})), \
             patch("iv8_rs._is_challenge", return_value=False):
            self.assertEqual(iv8_rs.fetch_with_iv8("http://example.edu.cn/", 20), "")


if __name__ == "__main__":
    unittest.main()
