# -*- coding: utf-8 -*-
"""OpenClaw 移植特性测试：standing intents（事件条件意图）+ standing orders（常备指令）
+ 合并转发读取 + VSCode 代码渲染。"""
from __future__ import annotations

import json
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


class StandingIntentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _plugin(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin, hooks

    async def test_intent_add_match_fire_and_cooldown(self):
        plugin, hooks = await self._plugin()
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123")
        plugin._known_scopes["123"] = scope
        created = await plugin.manage_intent_tool(
            None, action="add", keywords="上线,发布",
            instruction="提醒大家看更新日志并记录反馈")
        data = json.loads(created)
        self.assertTrue(data["ok"])
        # 命中："发布" 出现在消息里
        hits = await plugin._match_standing_intents(scope, ["今天新版本发布啦"])
        self.assertEqual(1, len(hits))
        self.assertIn("更新日志", hits[0]["instruction"])
        # 无限次意图冷却 0 → 再查仍命中
        hits = await plugin._match_standing_intents(scope, ["上线测试"])
        self.assertEqual(1, len(hits))
        # 不命中
        self.assertEqual([], await plugin._match_standing_intents(scope, ["今天天气不错"]))

    async def test_intent_finite_budget_exhausts(self):
        plugin, hooks = await self._plugin()
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123")
        plugin._known_scopes["123"] = scope
        await plugin.storage.add_standing_intent(
            scope, "问一次反馈", ["问卷"], remaining=1)
        self.assertEqual(1, len(await plugin._match_standing_intents(scope, ["填问卷"])))
        self.assertEqual([], await plugin._match_standing_intents(scope, ["再填问卷"]))
        rows = await plugin.storage.list_standing_intents(active_only=False)
        self.assertEqual("spent", rows[0]["status"])

    async def test_intent_remove(self):
        plugin, hooks = await self._plugin()
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123")
        plugin._known_scopes["123"] = scope
        data = json.loads(await plugin.manage_intent_tool(
            None, action="add", keywords="周末", instruction="提议拼饭"))
        result = await plugin.manage_intent_tool(
            None, action="remove", intent_id=data["intent_id"])
        self.assertIn("已取消", result)
        rows = await plugin.storage.list_standing_intents()
        self.assertEqual([], rows)

    async def test_standing_orders_injected(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]),
            hooks._plugin_config() | {"standing_orders": "每周五把群里聊过的话题整理成一条总结发出来"})
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        prompt = await plugin._compose_reply_prompt()
        self.assertIn("常备指令", prompt)
        self.assertIn("每周五", prompt)


class ForwardMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_forward_ids_and_fetch_flatten(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        raw = {"self_id": "bot1", "group_id": "123", "message": [
            {"type": "text", "data": {"text": "[合并转发]"}},
            {"type": "forward", "data": {"id": "fwd-123"}},
        ]}
        self.assertEqual(["fwd-123"], plugin._forward_ids(raw))
        self.assertEqual([], plugin._forward_ids({"message": []}))

        class _Bot:
            async def call_action(self, action, **params):
                assert action == "get_forward_msg"
                assert params["id"] == "fwd-123"
                return {"status": "ok", "retcode": 0, "data": {"messages": [
                    {"sender": {"nickname": "小明"}, "content": [
                        {"type": "text", "data": {"text": "明天下午三点开会"}}]},
                    {"sender": {"nickname": "小红"}, "content": [
                        {"type": "image", "data": {"file": "x.jpg"}}]},
                ]}}

        plugin.gateway.bot = _Bot()
        flattened = await plugin._fetch_forward_text("fwd-123")
        self.assertIn("小明：明天下午三点开会", flattened)
        self.assertIn("小红：[image]", flattened)
        # 入库为可查询记忆
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123")
        await plugin._ingest_forward_notes(raw, scope)
        events = await plugin.storage.recent_events([scope], 5)
        self.assertTrue(any("合并转发内容" in e.text for e in events))


class CodeRenderTests(unittest.TestCase):
    def test_highlight_and_page(self):
        from src.code_render import code_to_page, highlight

        snippet = 'def greet(name):\n    # 打招呼\n    return f"hi {name}"\n'
        highlighted = highlight(snippet, "python")
        self.assertIn('class="kw">def', highlighted)
        self.assertIn('class="cm"># 打招呼', highlighted)
        self.assertIn('class="st">', highlighted)
        page = code_to_page(snippet, "python", "main.py")
        self.assertIn("main.py", page)
        self.assertIn("greet", page)
        self.assertNotIn("<script", page, "无外链无脚本，离线可渲染")
        # 未知语言退化为通用高亮（字符串/注释仍着色）
        generic = highlight("'hello'", "kotlin")
        self.assertIn('class="st"', generic)

    def test_html_escaped(self):
        from src.code_render import highlight

        out = highlight('<img src="x"> <script>', "plain")
        self.assertNotIn("<img", out)
        self.assertNotIn("<script>", out)


if __name__ == "__main__":
    unittest.main()
