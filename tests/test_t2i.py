# -*- coding: utf-8 -*-
"""长文本自动转图的顺序回归测试。"""
from __future__ import annotations

import dataclasses
import os
import tempfile
import types
import unittest
from pathlib import Path


def _hooks():
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks

    return hooks


class TextToImageOrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin, hooks

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_long_text_is_rendered_before_split(self):
        """总字数超阈值 → 整条转图，而不是拆成多条短气泡后逐条漏判。"""
        plugin, hooks = await self._plugin()
        plugin._humanization = dataclasses.replace(plugin._humanization, typing_chars_per_second=1000.0)
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
        plugin._known_scopes["123"] = scope

        long_text = "这是一段很长的说明内容。" * 60  # ~600 字
        plugin.context.responses = [
            '{"thought": "长内容", "mode": "single", '
            '"message": {"action": "text", "text": "' + long_text + '"}}',
        ]

        calls = {"n": 0}

        async def fake_t2i(text, return_url=True):
            calls["n"] += 1
            return "https://example.com/render.png"

        plugin.text_to_image = fake_t2i  # 影子覆盖 Star 基类方法

        bot = hooks.FakeBot()
        event = hooks.FakeEvent(hooks._group_raw("讲讲那个", 10002), "讲讲那个", bot,
                                wake=True)
        from src.models import Decision

        decision = Decision(action="text")
        permit = types.SimpleNamespace()
        await plugin._execute_decision(
            event, scope, "10002", "{}", decision, permit, False, None)

        self.assertEqual(1, calls["n"], "应按总字数触发一次转图")
        self.assertEqual(1, len(event.sent), "转图成功后只发一条图片消息")

    async def test_t2i_failure_degrades_to_text(self):
        """渲染失败必须降级为普通文本发送，不能丢消息。"""
        plugin, hooks = await self._plugin()
        plugin._humanization = dataclasses.replace(plugin._humanization, typing_chars_per_second=1000.0)
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
        plugin._known_scopes["123"] = scope

        long_text = "这是一段很长的说明内容。" * 60
        plugin.context.responses = [
            '{"thought": "长内容", "mode": "single", '
            '"message": {"action": "text", "text": "' + long_text + '"}}',
        ]

        async def broken_t2i(text, return_url=True):
            raise RuntimeError("渲染内核缺失")

        plugin.text_to_image = broken_t2i

        bot = hooks.FakeBot()
        event = hooks.FakeEvent(hooks._group_raw("讲讲那个", 10003), "讲讲那个", bot,
                                wake=True)
        from src.models import Decision

        decision = Decision(action="text")
        permit = types.SimpleNamespace()
        await plugin._execute_decision(
            event, scope, "10003", "{}", decision, permit, False, None)

        self.assertGreaterEqual(len(event.sent), 1, "降级后消息仍要发出")
        # 降级路径发的是文本（无图片组件）
        for chain in event.sent:
            for comp in getattr(chain, "chain", []):
                self.assertNotIn("image", str(getattr(comp, "type", "")).lower())


if __name__ == "__main__":
    unittest.main()
