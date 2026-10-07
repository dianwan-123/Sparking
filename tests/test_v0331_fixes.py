# -*- coding: utf-8 -*-
"""v0.33.1 回归：判定 null 字段、上游已删模型的运行期换模型、合并转发累积、
引用转发、"旧任务不自动续"纪律与 todo 新鲜度。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.decision import DecisionEngine  # noqa: E402


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class DecisionNullFieldTests(unittest.IsolatedAsyncioTestCase):
    async def test_null_intent_and_query_do_not_crash(self):
        """实录：judge 回了 {"action":"ignore","intent":null} →
        value.get("intent","")[:500] 对 None 切片 → TypeError 让整批判定失败。"""
        async def judge(_prompt):
            return '{"action": "ignore", "intent": null, "query": null, "wait_seconds": 0}'

        engine = DecisionEngine(judge)
        decision = await engine.decide("x")
        self.assertEqual("ignore", decision.action)
        self.assertEqual("", decision.intent)
        self.assertEqual("", decision.query)

    async def test_parse_directly(self):
        engine = DecisionEngine(lambda _p: "")
        decision = engine.parse('{"action":"text","intent":null}')
        self.assertEqual("", decision.intent)


class ProviderHealTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_missing_provider_switches_and_blacklists(self):
        """实录：闲时/日程动作在早已被删的 deepseek 上烧满 5/5 次重试——
        上游 'Provider X not found' 必须拉黑并立刻换模型。"""
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        plugin.settings.reply_provider_id = "good-model"
        seen = []

        class _Response:
            completion_text = "ok"

        async def llm_generate(**kwargs):
            seen.append(kwargs.get("chat_provider_id"))
            if kwargs.get("chat_provider_id") == "catapi/deepseek-v4.1-flash":
                raise RuntimeError("Provider catapi/deepseek-v4.1-flash not found")
            return _Response()

        plugin.context.llm_generate = llm_generate
        text = await plugin._llm_text(
            "catapi/deepseek-v4.1-flash", prompt="hi", system_prompt="sys")
        self.assertEqual("ok", text)
        self.assertIn("catapi/deepseek-v4.1-flash", plugin._dead_providers)
        self.assertIn("good-model", seen, "换到可用模型重试")

    def test_looks_like_provider_missing(self):
        cls = _hooks().LongMemoryAgentPlugin
        self.assertTrue(cls._looks_like_provider_missing(
            RuntimeError("Provider catapi/deepseek-v4.1-flash not found")))
        self.assertTrue(cls._looks_like_provider_missing(
            RuntimeError("Error code: 404 - No active credentials for provider: x")))
        self.assertFalse(cls._looks_like_provider_missing(
            RuntimeError("Connection reset by peer")))


class ForwardHandlingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _make_plugin(self, config_overrides=None):
        hooks = _hooks()
        config = hooks._plugin_config()
        config.update(config_overrides or {})
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin

    async def test_pending_forwards_accumulate_per_conversation(self):
        """攒批场景：逐条覆盖 _pending_forward_ids 会丢先到的那条转发。
        用长攒批窗口让两条转发都留在缓冲里（0 秒窗口会立刻消费掉待展开表）。"""
        hooks = _hooks()
        plugin = await self._make_plugin({
            "batch_window_min_seconds": 3600, "batch_window_max_seconds": 3600,
        })
        first = hooks._group_raw("[转发消息]", 4301)
        first["message"] = [{"type": "forward", "data": {"id": "fwd-1"}}]
        second = hooks._group_raw("[转发消息二]", 4302)
        second["message"] = [{"type": "forward", "data": {"id": "fwd-2"}}]
        await plugin.on_onebot_event(
            hooks.FakeEvent(first, "[转发消息]", hooks.FakeBot()))
        await plugin.on_onebot_event(
            hooks.FakeEvent(second, "[转发消息二]", hooks.FakeBot()))
        bucket = plugin._pending_forwards.get("123", [])
        self.assertIn("fwd-1", bucket)
        self.assertIn("fwd-2", bucket)

    async def test_quoted_forward_digest(self):
        """引用一条合并转发（'引用转发+@bot 看下'）也要能展开。"""
        from DFYChat.src.models import StoredMessage

        plugin = await self._make_plugin()

        async def fake_execute(action, **params):
            if action == "get_forward_msg":
                return {"status": "ok", "data": {"messages": [
                    {"sender": {"nickname": "甲"}, "content": [
                        {"type": "text", "data": {"text": "第一句"}}]},
                    {"sender": {"nickname": "乙"}, "content": [
                        {"type": "text", "data": {"text": "第二句"}}]},
                ]}}
            return {"status": "ok", "data": {}}

        plugin.gateway.execute = fake_execute

        async def fake_find(scope_id, upstream_id):
            return StoredMessage(
                message_id="quoted-fwd", revision_id="r", scope_id=scope_id,
                scope_seq=1, upstream_message_id=upstream_id,
                sender_id="9", sender_name="甲", text="[转发消息]",
                occurred_at="2026-10-05T00:00:00+00:00",
                parts=[{"type": "forward", "data": {"id": "fwd-9"}}],
            )

        plugin.storage.find_by_upstream = fake_find
        digest = await plugin._quoted_digest("scope-1", "8001")
        self.assertIn("forward", digest)
        self.assertIn("第一句", digest["forward"])
        self.assertIn("第二句", digest["forward"])


class TaskCrossoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_stale_todos_not_injected(self):
        """超过 24h 没动的任务不再每轮注入——跨天旧任务是"任务串"的头号来源。"""
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext(['{"action": "ignore"}']), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123")
        rows = await plugin.storage.add_todos(scope, ["读昨天的试卷"])
        await plugin.storage._conn().execute(
            "UPDATE agent_todos SET updated_at=? WHERE todo_id=?",
            ("2020-01-01T00:00:00+00:00", rows[0]["todo_id"]))
        await plugin.storage._conn().commit()

        event = hooks.FakeEvent(
            hooks._group_raw("今天天气不错", 4303), "今天天气不错",
            hooks.FakeBot(), wake=True)
        await plugin.on_onebot_event(event)
        while plugin._tasks:
            import asyncio

            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)
        prompt = "\n".join(
            str(call.get("prompt", "")) for call in plugin.context.llm_calls
            if isinstance(call, dict))
        self.assertNotIn("active_todos", prompt)

        # 新鲜任务仍然注入
        await plugin.storage.add_todos(scope, ["今天要做的活"])
        await plugin.storage._conn().execute(
            "UPDATE agent_todos SET updated_at=? WHERE todo_id=?",
            ("2020-01-01T00:00:00+00:00", rows[0]["todo_id"]))
        await plugin.storage._conn().commit()
        event2 = hooks.FakeEvent(
            hooks._group_raw("在吗", 4304), "在吗", hooks.FakeBot(), wake=True)
        await plugin.on_onebot_event(event2)
        while plugin._tasks:
            import asyncio

            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)
        prompt2 = "\n".join(
            str(call.get("prompt", "")) for call in plugin.context.llm_calls
            if isinstance(call, dict))
        self.assertIn("active_todos", prompt2)
        self.assertIn("今天要做的活", prompt2)


class DisciplinePromptTests(unittest.TestCase):
    def test_task_and_tool_discipline_present(self):
        _hooks()
        from DFYChat.src.prompts import DECISION_SYSTEM_PROMPT, REPLY_SYSTEM_PROMPT

        self.assertIn("旧任务不自动续", REPLY_SYSTEM_PROMPT)
        self.assertIn("工具名纪律", REPLY_SYSTEM_PROMPT)
        self.assertIn("active_todos", REPLY_SYSTEM_PROMPT)
        self.assertIn("不自动续", DECISION_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
