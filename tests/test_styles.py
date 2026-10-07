# -*- coding: utf-8 -*-
"""风格档案学习与模仿的回归测试。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src.models import NormalizedMessage
from src.storage import Storage


class StyleStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_style_roundtrip_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = await Storage(Path(tmp) / "memory.db").open()
            try:
                scope = await storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
                self.assertEqual({}, await storage.get_style(scope, "10002"))
                await storage.upsert_style(
                    scope, "10002", display_name="甲",
                    style_summary="短句 爱用草和问号",
                    catchphrases=["草", "确实"], sample_lines=["草 这也太离谱了"],
                    msg_count=12,
                )
                got = await storage.get_style(scope, "10002")
                self.assertEqual("短句 爱用草和问号", got["style_summary"])
                self.assertEqual(["草", "确实"], got["catchphrases"])
                self.assertEqual(12, got["msg_count"])
                # 只刷 last_seen 类字段时不清空摘要
                await storage.upsert_style(scope, "10002", display_name="甲二号")
                got = await storage.get_style(scope, "10002")
                self.assertEqual("甲二号", got["display_name"])
                self.assertEqual("短句 爱用草和问号", got["style_summary"])
                self.assertEqual(1, len(await storage.list_styles([scope])))
            finally:
                await storage.close()


def _hooks():
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks

    return hooks


class StyleLearningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def test_learn_round_writes_style_profiles(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
            plugin._known_scopes["123"] = scope
            for i in range(6):
                await plugin.ingest.ingest(NormalizedMessage(
                    platform="aiocqhttp", account_id="1", conversation_id="123",
                    upstream_message_id=f"m-{i}", sender_id="10002",
                    sender_name="甲", text=f"草 这也太离谱了 {i}",
                    occurred_at=f"2026-09-19T10:{i:02d}:00+00:00",
                    raw_event={"message_id": f"m-{i}"}, parts=[],
                    event_type="message.created",
                ))
            plugin.context.responses = [json.dumps({
                "styles": [{
                    "user_id": "10002",
                    "summary": "短句 爱用草和问号",
                    "catchphrases": ["草", "离谱"],
                    "samples": ["草 这也太离谱了 3"],
                }],
            }, ensure_ascii=False)]
            learned = await plugin._learn_styles_round()
            self.assertEqual(1, learned)
            style = await plugin.storage.get_style(scope, "10002")
            self.assertEqual("短句 爱用草和问号", style["style_summary"])
            self.assertIn("草", style["catchphrases"])
            self.assertEqual(6, style["msg_count"])
        finally:
            await plugin.terminate()

    async def test_sender_style_injected_into_reply_context(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
            plugin._known_scopes["123"] = scope
            await plugin.storage.upsert_style(
                scope, "10002", style_summary="短句 爱用问号", catchphrases=["？"],
            )
            bot = hooks.FakeBot()
            plugin.context.responses = [
                '{"action": "ignore", "intent": "", "query": "", "wait_seconds": 0}',
            ]
            event = hooks.FakeEvent(hooks._group_raw("在吗", 10002), "在吗", bot, wake=True)
            await plugin.on_onebot_event(event)
            await __import__("asyncio").sleep(0.15)
            judge_prompt = str(plugin.context.llm_calls[0].get("prompt", ""))
            self.assertIn("sender_style", judge_prompt)
            self.assertIn("短句 爱用问号", judge_prompt)
        finally:
            await plugin.terminate()


class DispatchCompletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def test_dispatch_runs_python_exec(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            result = await plugin._dispatch_task_action(
                "python_exec", {"code": "print(1 + 1)"}, None)
            self.assertIn("2", result)
        finally:
            await plugin.terminate()

    async def test_dispatch_napcat_catalog_and_affinity(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
            plugin._known_scopes["123"] = scope
            # v0.38.0：不带参数列分类；给 category 列该分类动作；给 action 查参数表
            overview = await plugin._dispatch_task_action("napcat_catalog", {}, scope)
            self.assertIn("categories", overview)
            catalog = await plugin._dispatch_task_action(
                "napcat_catalog", {"category": "消息"}, scope)
            self.assertIn("send_group_msg", catalog)
            single = await plugin._dispatch_task_action(
                "napcat_catalog", {"action": "get_forward_msg"}, scope)
            self.assertIn("message_id", single)
            await plugin._dispatch_task_action(
                "adjust_affinity",
                {"user_id": "10002", "delta": 5, "note": "测试"}, scope)
            result = await plugin._dispatch_task_action(
                "get_user_style", {"user_id": "10002"}, scope)
            self.assertIn("没有TA的风格档案", result)  # 没学过 → 友好提示
        finally:
            await plugin.terminate()

    async def test_new_tools_listed_in_prompts(self):
        root = Path(__file__).resolve().parents[1]
        prompts = (root / "src" / "prompts.py").read_text(encoding="utf-8")
        self.assertIn("get_user_style", prompts)
        self.assertIn("语气模仿", prompts)
        self.assertIn("batch_styles", prompts)
