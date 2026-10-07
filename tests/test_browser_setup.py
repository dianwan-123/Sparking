# -*- coding: utf-8 -*-
"""浏览器环境自动安装与页面操作工具的测试（全程不真装）。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.browser import BrowserError
from src.browser_setup import ensure_browser_environment


class ChromiumCacheTests(unittest.TestCase):
    def test_detects_chromium_kernel_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(__import__("src.browser_setup", fromlist=["chromium_cached"]).chromium_cached(root))
            (root / "chromium-1148").mkdir()
            self.assertTrue(
                __import__("src.browser_setup", fromlist=["chromium_cached"]).chromium_cached(root))


def _fake_runner(calls: list, results: list[tuple[int, str]] | None = None):
    """Runner that records commands and pops canned results."""
    async def run(cmd, timeout=60, env=None):
        calls.append({"cmd": cmd, "env": env})
        if results:
            return results.pop(0)
        return 0, ""
    return run


class EnsureEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_ready_reports_without_steps(self):
        status = await ensure_browser_environment(
            auto_install=True,
            import_check=lambda name: True,
            chromium_check=lambda: True,
            runner=_fake_runner([]),
        )
        self.assertTrue(status["playwright"])
        self.assertTrue(status["chromium"])
        self.assertEqual([], status["steps"])
        self.assertEqual("", status["error"])

    async def test_missing_playwright_no_install_reports_error(self):
        calls: list = []
        status = await ensure_browser_environment(
            auto_install=False,
            import_check=lambda name: False,
            chromium_check=lambda: False,
            runner=_fake_runner(calls),
        )
        self.assertEqual([], calls, "auto_install 关闭时不许执行安装命令")
        self.assertFalse(status["playwright"])
        self.assertIn("browser_auto_install", status["error"])

    async def test_auto_install_uses_china_mirrors(self):
        calls: list = []
        import_state = {"installed": False}
        state = {"kernel": False}

        def import_check(name: str) -> bool:
            return import_state["installed"]

        async def runner(cmd, timeout=60, env=None):
            calls.append({"cmd": cmd, "env": env})
            if "chromium" in cmd:
                state["kernel"] = True  # 内核装完 → 缓存检测通过
            else:
                import_state["installed"] = True  # pip 装完 → import 通过
            return 0, ""

        status = await ensure_browser_environment(
            auto_install=True,
            import_check=import_check,
            chromium_check=lambda: state["kernel"],
            runner=runner,
        )
        self.assertTrue(status["playwright"])
        pip_cmd = calls[0]["cmd"]
        self.assertIn("install", pip_cmd)
        self.assertIn("playwright", pip_cmd)
        self.assertTrue(any("tuna.tsinghua" in part for part in pip_cmd),
                        "pip 必须走国内镜像")
        install_cmd = calls[1]["cmd"]
        self.assertIn("playwright", install_cmd)
        self.assertIn("install", install_cmd)
        self.assertIn("chromium", install_cmd)
        self.assertEqual(
            "https://npmmirror.com/mirrors/playwright/",
            calls[1]["env"].get("PLAYWRIGHT_DOWNLOAD_HOST"),
            "内核下载首选 npmmirror 国内镜像")
        self.assertTrue(status["chromium"])

    async def test_pip_failure_surfaces_error(self):
        status = await ensure_browser_environment(
            auto_install=True,
            import_check=lambda name: False,
            chromium_check=lambda: False,
            runner=_fake_runner([], results=[(1, "mirror unreachable")]),
        )
        self.assertFalse(status["playwright"])
        self.assertIn("安装失败", status["error"])


def _hooks():
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks

    return hooks


class ToolDegradeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def test_page_ops_degrade_without_kernel(self):
        """桩环境没有 playwright：新工具给友好提示而不是崩溃。"""
        hooks = _hooks()
        config = hooks._plugin_config()
        config["enable_media_archive"] = True
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), config
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            bot = hooks.FakeBot()
            dom = await plugin.browser_dom_tool(bot, "https://example.com")
            self.assertIn("playwright", dom)
            click = await plugin.browser_click_element_tool(bot, 0)
            self.assertIn("playwright", click)
            typed = await plugin.browser_type_text_tool(bot, 0, "x")
            self.assertIn("playwright", typed)
            shot = await plugin.browser_screenshot_tool(bot, "https://example.com")
            self.assertIn("截图失败", shot)
            # 过验证工具在没内核时也要优雅降级，不能崩
            slid = await plugin.browser_slide_tool(bot, 100, 500, 300, 500)
            self.assertIn("失败", slid)
            clicked = await plugin.browser_click_xy_tool(bot, 10, 20)
            self.assertIn("失败", clicked)
            grid = await plugin.browser_grid_shot_tool(bot)
            self.assertIn("截图失败", grid)
        finally:
            await plugin.terminate()

    async def test_overview_exposes_browser_env(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            import sys

            web = sys.modules["astrbot.api.web"]
            saved = getattr(web, "request", None)
            web.request.username = "admin"
            try:
                data = await plugin._page_api().read("overview", {})
                self.assertIn("browser_env", data)
                self.assertIn("checked", data["browser_env"])
            finally:
                if saved is not None:
                    web.request = saved
        finally:
            await plugin.terminate()


    async def test_download_falls_back_to_next_mirror(self):
        """第一个镜像失败 → 自动换下一个源；成功即停。"""
        calls: list = []
        state = {"kernel": False}

        async def runner(cmd, timeout=60, env=None):
            calls.append({"cmd": cmd, "env": env})
            if "chromium" in cmd:
                if len([c for c in calls if "chromium" in c["cmd"]]) == 1:
                    return 1, "Error: 404 download failed"
                state["kernel"] = True
                return 0, ""
            return 0, ""

        log_file = Path(self._tmp.name) / "browser_setup.log"
        status = await ensure_browser_environment(
            auto_install=True,
            import_check=lambda name: True,
            chromium_check=lambda: state["kernel"],
            runner=runner,
            log_file=log_file,
        )
        self.assertTrue(status["chromium"])
        chromium_calls = [c for c in calls if "chromium" in c["cmd"]]
        self.assertEqual(2, len(chromium_calls))
        self.assertEqual(
            "https://cdn.npmmirror.com/binaries/playwright/",
            chromium_calls[1]["env"].get("PLAYWRIGHT_DOWNLOAD_HOST"))
        # 失败那次完整输出已落盘
        self.assertIn("404", log_file.read_text(encoding="utf-8"))

    async def test_all_mirrors_failed_reports_error(self):
        calls: list = []

        async def runner(cmd, timeout=60, env=None):
            calls.append({"cmd": cmd, "env": env})
            return 1, "Error: connection reset"

        log_file = Path(self._tmp.name) / "browser_setup.log"
        status = await ensure_browser_environment(
            auto_install=True,
            import_check=lambda name: True,
            chromium_check=lambda: False,
            runner=runner,
            log_file=log_file,
        )
        self.assertFalse(status["chromium"])
        # npmmirror×2 + registry + 官方 = 4 个下载源
        self.assertEqual(4, len([c for c in calls if "chromium" in c["cmd"]]))
        self.assertIn("已尝试4个下载源", status["error"])
        self.assertIn("connection reset", log_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
