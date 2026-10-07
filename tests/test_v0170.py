# -*- coding: utf-8 -*-
"""v0.17.0 回归测试：md 清洗 / 原版兼容 / KB / 截图降级 / 调度通道。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


def _hooks():
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks

    return hooks


class MarkdownBubbleTests(unittest.TestCase):
    def test_bubble_strips_markdown_artifacts(self):
        import sys

        _hooks()
        main_mod = sys.modules["DFYChat.main"]
        # v0.17.1 起 markdown 语法有意保留（QQ 不渲染但语义在），
        # _bubble_text 只清手写 At 语法与全角标点
        cases = [
            ("**你好**呀", "**你好**呀"),
            ("### 标题行", "### 标题行"),
            ("`code`", "`code`"),
            ("[CQ:at,qq=123] 你好", "你好"),
            ("不行不行，这个真看不了。", "不行不行 这个真看不了"),
        ]
        for raw, expected in cases:
            self.assertEqual(expected, main_mod._bubble_text(raw))

    def test_bubble_keeps_normal_text(self):
        import sys

        _hooks()
        main_mod = sys.modules["DFYChat.main"]
        self.assertEqual("不行不行 这个真看不了",
                         main_mod._bubble_text("不行不行，这个真看不了。"))


class OriginCompatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def test_custom_prefix_bypasses_judging(self):
        """自定义前缀（小夜）的消息直通原版：不判定、不拦截、仍入库。"""
        hooks = _hooks()
        config = hooks._plugin_config()
        config["origin_wake_prefixes"] = "/,小夜"
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            bot = hooks.FakeBot()
            event = hooks.FakeEvent(hooks._group_raw("小夜 帮我签到", 20001),
                                    "小夜 帮我签到", bot, wake=True)
            await plugin.on_onebot_event(event)
            await __import__("asyncio").sleep(0.15)
            # 不进判定模型
            self.assertEqual(0, len(plugin.context.llm_calls))
            # 不拦截事件（AstrBot 原生命令系统可以接手）
            self.assertFalse(event._stopped)
            # 与 "/" 同语义：前缀消息不进记忆（命令不该污染记忆）
        finally:
            await plugin.terminate()

    async def test_default_slash_prefix_still_bypasses(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            bot = hooks.FakeBot()
            event = hooks.FakeEvent(hooks._group_raw("/help", 20002), "/help", bot)
            await plugin.on_onebot_event(event)
            await __import__("asyncio").sleep(0.1)
            self.assertEqual(0, len(plugin.context.llm_calls))
            self.assertFalse(event._stopped)
        finally:
            await plugin.terminate()

    async def test_origin_pack_disabled_falls_back_to_slash_only(self):
        """origin 拓展停用后，自定义前缀恢复判定，"/" 仍直通（保住管理命令）。"""
        hooks = _hooks()
        config = hooks._plugin_config()
        config["origin_wake_prefixes"] = "/,小夜"
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            self.assertTrue(plugin._extensions.disable("origin"))
            prefixes = plugin._origin_prefixes()
            self.assertEqual(["/"], prefixes)
        finally:
            await plugin.terminate()


class KnowledgePackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def test_kb_tools_degrade_gracefully_without_kb_module(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            bot = hooks.FakeBot()
            listed = await plugin.kb_list_tool(bot)
            self.assertIn("kbs", listed)
            searched = await plugin.kb_search_tool(bot, "量子")
            self.assertIn("知识库", searched)  # 未初始化的友好提示
        finally:
            await plugin.terminate()

    async def test_kb_pack_registered(self):
        from src.extensions import BUILTIN_PACKS

        self.assertIn("knowledge", BUILTIN_PACKS)
        self.assertIn("kb_search", BUILTIN_PACKS["knowledge"][2])
        self.assertIn("origin", BUILTIN_PACKS)
        self.assertIn("browser_screenshot", BUILTIN_PACKS["browser"][2])

    async def test_kb_tools_in_dispatch_channel(self):
        """闲时/定时的调度通道也要认识 kb_list（与 llm_tool 同级）。"""
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            result = await plugin._dispatch_task_action("kb_list", {}, None)
            self.assertIn("kbs", result)
        finally:
            await plugin.terminate()


class ScreenshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def test_screenshot_degrades_without_playwright(self):
        """无内核时截图给友好降级说明（v0.19.0 起走 PlaywrightDriver）。"""
        hooks = _hooks()
        config = hooks._plugin_config()
        config["enable_media_archive"] = True
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            bot = hooks.FakeBot()
            result = await plugin.browser_screenshot_tool(bot, "https://example.com")
            self.assertIn("playwright", result)
        finally:
            await plugin.terminate()

    async def test_dispatch_channel_knows_browse(self):
        """闲时 learning 指令里教的 browse 必须能走调度通道。"""
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            session = plugin._browser_session()

            def _fake(target, method="GET", payload=None):
                from src.browser import Page

                return (Page(url=target, status=200, title="示例页",
                             text="正文内容", links=[], forms=[]), "ok")

            session._fetch_sync = _fake  # type: ignore[assignment]
            result = await plugin._dispatch_task_action(
                "browse", {"url": "https://example.com/news"}, None)
            self.assertIn("示例页", result)
        finally:
            await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
