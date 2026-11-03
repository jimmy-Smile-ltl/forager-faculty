#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器按需启动+空闲回收回归测试 (2026-09-02 用户指令)。

覆盖: 9222 自启兜底(_ensure_9222) / 活动时间戳 touch / handleRuishu 9333
空闲回收 / smart_fetch 池阈值默认 180s。
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJ / "department teachers"))
sys.path.insert(0, str(_PROJ))
sys.path.insert(0, str(_PROJ / "util"))


class TestEnsure9222(unittest.TestCase):
    """9222 被空闲回收后, 租用方自启兜底"""

    def test_already_listening_no_launch(self):
        import browser_lifecycle
        with patch.object(browser_lifecycle, "port_listening", return_value=True), \
             patch("subprocess.Popen") as popen:
            self.assertTrue(browser_lifecycle.ensure_9222())
            popen.assert_not_called()

    def test_down_then_self_launch(self):
        import browser_lifecycle
        # 首次探测不通 → Popen 拉起 → 第二次探测通
        # (2026-09-08 守卫预检 mock: personal_chrome_running 的 pgrep 走
        # subprocess.run → py3.14 内部 Popen, 不 mock 会占 Popen 计数)
        # 2026-10-25 补: PROJECT_CHROME 空会拒绝拉起(测试环境无独立
        # Chromium), _clean_profile_volatile 防误触真实 /tmp profile
        with patch.object(browser_lifecycle, "port_listening",
                          side_effect=[False, False, False, True]), \
             patch.object(browser_lifecycle, "personal_chrome_running",
                          return_value=True), \
             patch.object(browser_lifecycle, "PROJECT_CHROME",
                          "/fake/path/Chromium"), \
             patch.object(browser_lifecycle, "_clean_profile_volatile"), \
             patch("subprocess.Popen") as popen:
            self.assertTrue(browser_lifecycle.ensure_9222())
            popen.assert_called_once()

    def test_touch_activity_creates_file(self):
        import os
        import browser_lifecycle
        with patch.object(browser_lifecycle, "_BROWSER_ACTIVITY",
                          "/tmp/_test_browser_activity.ts"):
            browser_lifecycle.touch_browser_activity()
            self.assertTrue(os.path.exists("/tmp/_test_browser_activity.ts"))
            os.unlink("/tmp/_test_browser_activity.ts")


class TestRsResidentIdleReap(unittest.TestCase):
    """handleRuishu 9333 常驻实例空闲回收。

    2026-10-25 功能迁移: 独立 Chromium 架构(2026-09-10)后 9333 空闲回收
    整体移交 watchdog 统一管理, handleRuishu 不再有 _RS_IDLE_REAP 阈值与
    idle 判断逻辑(常驻生命周期唯一归 watchdog)——原测试功能不存在, skip。
    """

    @unittest.skip("9333 空闲回收 2026-09-10 迁移 watchdog, handleRuishu 已无 idle 逻辑")
    def test_idle_over_threshold_shuts_down_and_relaunches(self):
        import handleRuishu as hr
        hr._CDP_RESIDENT["ws_url"] = "ws://x"
        hr._CDP_RESIDENT["proc"] = "fake-proc"
        hr._CDP_RESIDENT["last_used"] = time.time() - 99999  # 空闲远超阈值
        spider = hr.RsSpider.__new__(hr.RsSpider)
        with patch.object(hr, "_RS_IDLE_REAP", 180), \
             patch.object(hr, "_shutdown_resident_chrome",
                          side_effect=lambda: hr._CDP_RESIDENT.update(
                              ws_url=None, proc=None, last_used=0.0)), \
             patch.object(spider, "_launch_chrome_cdp_locked",
                          return_value=("p2", "ws://y")) as launch:
            spider._launch_chrome_cdp(9333)
            launch.assert_called_once()  # 旧实例被回收后按需重建

    @unittest.skip("9333 空闲回收 2026-09-10 迁移 watchdog, handleRuishu 已无 idle 逻辑")
    def test_recent_use_reuses_resident(self):
        import handleRuishu as hr
        hr._CDP_RESIDENT["ws_url"] = "ws://x"
        hr._CDP_RESIDENT["proc"] = "fake-proc"
        hr._CDP_RESIDENT["last_used"] = time.time()  # 刚用过
        spider = hr.RsSpider.__new__(hr.RsSpider)
        with patch.object(hr, "_RS_IDLE_REAP", 180), \
             patch.object(spider, "_resident_alive", return_value=True), \
             patch.object(hr, "_shutdown_resident_chrome") as sd, \
             patch.object(spider, "_launch_chrome_cdp_locked") as launch:
            proc, ws = spider._launch_chrome_cdp(9333)
            self.assertEqual(ws, "ws://x")
            sd.assert_not_called()
            launch.assert_not_called()


class TestPoolIdleDefault(unittest.TestCase):
    def test_idle_timeout_is_reap_default(self):
        # 统一阈值: BROWSER_IDLE_REAP 默认 1800 (2026-09-10 由 180 调长 —
        # 渲染需求断续时 180s 造成「拉起→空闲被杀→再拉起」冷启动循环)
        # 2026-10-25 修: 项目 .env 设了 300 会覆盖默认——测试须隔离环境
        # 变量后 reload 模块, 测的是代码默认值而非 .env 配置
        import importlib
        import os
        import browser_lifecycle
        saved = os.environ.pop("BROWSER_IDLE_REAP", None)
        try:
            importlib.reload(browser_lifecycle)
            self.assertEqual(browser_lifecycle.IDLE_REAP, 1800.0)
        finally:
            if saved is not None:
                os.environ["BROWSER_IDLE_REAP"] = saved
            importlib.reload(browser_lifecycle)  # 恢复 .env 读值, 不影响后续测试

    def test_drission_pool_uses_unified_reap(self):
        # 窗口池阈值来自统一模块(2026-09-02 策略整合)
        import drission_pool
        import browser_lifecycle
        self.assertEqual(drission_pool._IDLE_TIMEOUT, browser_lifecycle.IDLE_REAP)


if __name__ == "__main__":
    unittest.main()
