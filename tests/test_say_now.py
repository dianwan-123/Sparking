# -*- coding: utf-8 -*-
"""say_now：任务执行途中即时给当前对话发消息，与最终回复独立。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


def _hooks():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks
    return hooks


class SayNowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin, hooks

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_say_now_sends_one_bubble_to_current_conversation(self):
        plugin, hooks = await self._plugin()
        event = hooks.FakeEvent(hooks._group_raw("在吗", 10002), "在吗",
                                hooks.FakeBot(), wake=True)
        result = await plugin.say_now_tool(event, "稍等 我去过下验证")
        self.assertIn("已发送", result)
        self.assertEqual(1, len(event.sent))

    async def test_say_now_can_fire_multiple_times(self):
        plugin, hooks = await self._plugin()
        event = hooks.FakeEvent(hooks._group_raw("hi", 10002), "hi",
                                hooks.FakeBot(), wake=True)
        await plugin.say_now_tool(event, "第一步好了")
        await plugin.say_now_tool(event, "第二步好了")
        self.assertEqual(2, len(event.sent))

    async def test_empty_text_is_not_sent(self):
        plugin, hooks = await self._plugin()
        event = hooks.FakeEvent(hooks._group_raw("hi", 10002), "hi",
                                hooks.FakeBot(), wake=True)
        result = await plugin.say_now_tool(event, "   ")
        self.assertIn("空消息", result)
        self.assertEqual(0, len(event.sent))


if __name__ == "__main__":
    unittest.main()
