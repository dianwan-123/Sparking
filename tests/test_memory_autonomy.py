# -*- coding: utf-8 -*-
"""记忆系统与自主行为的地基修复回归：

1. 冷启动能从磁盘恢复会话映射（否则所有定时循环空转）。
2. gateway 在无入站事件时也能绑定到平台客户端（否则发说说等自主动作必挂
   "no callable OneBot transport on NoneType"）。
3. L1 压缩不再被弱模型输出"毒丸"卡死——垃圾输出也能形成 L1 并推进水位。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.models import NormalizedMessage


def _hooks():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks
    return hooks


def _norm(hooks, mid: str, text: str):
    from src.models import NormalizedMessage
    return NormalizedMessage(
        platform="aiocqhttp", account_id="bot1", conversation_id="123",
        upstream_message_id=mid, sender_id="10002", sender_name="Tester",
        text=text, occurred_at=datetime.now(timezone.utc).isoformat(),
        raw_event={"message_id": mid}, parts=[], event_type="message.created",
    )


class _Adapter:
    def __init__(self, client):
        self._client = client

    def meta(self):
        return types.SimpleNamespace(name="aiocqhttp", id="aiocqhttp")

    def get_client(self):
        return self._client


def _platform_context(hooks, client, responses=None):
    class _PlatformContext(hooks.FakeContext):
        def get_platform(self, name):
            return _Adapter(client) if name == "aiocqhttp" else None

    return _PlatformContext(responses or [])


class MemoryAutonomyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_hydrate_known_scopes_from_disk(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        # 造一个已存在的会话，再清空内存映射，模拟冷启动
        scope_id = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123")
        plugin._known_scopes.clear()
        await plugin._hydrate_known_scopes()
        self.assertEqual(scope_id, plugin._known_scopes.get("123"))

    async def test_bind_gateway_client_without_event(self):
        hooks = _hooks()
        client = hooks.FakeBot()
        plugin = hooks.LongMemoryAgentPlugin(
            _platform_context(hooks, client), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        # 初始化里已应绑定到平台客户端（无需任何入站事件）
        self.assertIsNotNone(plugin.gateway)
        self.assertIs(client, plugin.gateway.bot)
        # 真正走一遍传输：读动作应命中 fake 客户端的 call_action
        await plugin.gateway.execute("get_login_info")
        self.assertTrue(any(a == "get_login_info" for a, _ in client.calls))

    async def test_bind_gateway_self_heals_when_bot_lost(self):
        hooks = _hooks()
        client = hooks.FakeBot()
        plugin = hooks.LongMemoryAgentPlugin(
            _platform_context(hooks, client), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        plugin.gateway.bot = None  # 模拟从未被事件填充的冷启动状态
        self.assertTrue(plugin._bind_gateway_client())
        self.assertIs(client, plugin.gateway.bot)

    async def test_compress_scope_survives_garbage_llm(self):
        hooks = _hooks()
        # 不排任何 LLM 响应：FakeContext 会返回默认的 {"action":"ignore"}，
        # 这正是"弱模型给了不符合 schema 的输出"的场景。
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        scope_id = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123")
        for i in range(6):
            await plugin.storage.ingest_message(_norm(hooks, f"m{i}", f"消息内容 {i}"))
        pending_before = await plugin.storage.pending_l1_messages(scope_id, 40)
        self.assertTrue(pending_before)
        created = await plugin._compress_scope(scope_id, rounds=3)
        self.assertGreaterEqual(created, 1)
        # 水位必须推进：垃圾输出也形成了 L1，不再毒丸
        summaries = await plugin.storage.list_summaries(scope_id, 10, levels=[1])
        self.assertTrue(summaries)
        self.assertTrue(summaries[0].citations)
        pending_after = await plugin.storage.pending_l1_messages(scope_id, 40)
        self.assertLess(len(pending_after), len(pending_before))

    async def test_reply_fallback_when_plan_generation_fails(self):
        """回归：拟人回复生成失败（超时/坏计划）时不能再默默 return 让被@
        的用户彻底沉默——应走一次无工具的兜底对话发一句话。"""
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "群")
        plugin._known_scopes["123"] = scope
        # 响应1：计划合法但整段是工具语法垃圾 → _reply_plan 抛
        # "reply is entirely tool-call garbage"；响应2：兜底普通对话的正文。
        plugin.context.responses = [
            '{"mode": "single", "message": {"action": "text", "text": "args: reply tool: x"}}',
            "在的 怎么了",
        ]
        bot = hooks.FakeBot()
        event = hooks.FakeEvent(hooks._group_raw("在吗", 10002), "在吗", bot, wake=True)
        from src.models import Decision

        decision = Decision(action="text")
        await plugin._execute_decision(
            event, scope, "10002", "{}", decision, types.SimpleNamespace(), False, None
        )
        bubbles = [
            part[1]
            for chain in event.sent
            for part in getattr(chain, "chain", [])
            if part[0] == "text"
        ]
        self.assertTrue(bubbles, "兜底回复必须真的发出去")
        self.assertEqual("在的 怎么了", bubbles[-1])


class KnowledgeMoodTests(unittest.IsolatedAsyncioTestCase):
    """技术题强制查知识库 + 情绪/签名自更新。"""

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

    async def test_tech_question_detection(self):
        import sys
        plugin, hooks = await self._plugin()
        regex = sys.modules[type(plugin).__module__]._TECH_QUESTION_RE
        # 命中：数学/算法/编程各种表述
        for text in (
            "帮我看下这道数学题",
            "这道DP题怎么写",
            "我的python代码报错了",
            "NOIP的图论题有什么好算法",
            "二分查找的边界老写错",
        ):
            self.assertIsNotNone(regex.search(text), text)
        # 不命中：日常聊天
        for text in ("今天天气真好", "晚上吃啥", "一堆人排队", "哈哈哈哈"):
            self.assertIsNone(regex.search(text), text)

    async def test_knowledge_enrichment_injects_hits_and_directive(self):
        plugin, hooks = await self._plugin()
        # FakeContext 没有 kb_manager → _kb_search_data 返回"未初始化"提示，
        # 此时也应注入指令与空 hits（让模型明说知识库没有）
        enrichment = await plugin._knowledge_enrichment("这道动态规划题状态怎么设计")
        self.assertIsNotNone(enrichment)
        self.assertIn("knowledge_directive", enrichment)
        self.assertEqual([], enrichment["knowledge_hits"])
        # 非技术问题不注入
        self.assertIsNone(await plugin._knowledge_enrichment("今天吃啥好"))

    async def test_mood_status_tick_updates_mood_and_signature(self):
        plugin, hooks = await self._plugin()
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "群")
        plugin._known_scopes["123"] = scope
        for i in range(3):
            await plugin.storage.ingest_message(_norm(hooks, f"km{i}", f"今天群里聊得特别开心 {i}"))
        bot = hooks.FakeBot()
        plugin.gateway.bot = bot
        plugin.context.responses = [
            '{"mood": "兴奋", "mood_intensity": 0.8, "mood_note": "群里聊得火热", '
            '"update_signature": true, "new_signature": "今天有点嗨"}',
        ]
        await plugin._mood_status_tick()
        self.assertEqual("兴奋", plugin.mood.get().mood)
        signature_calls = [
            params.get("longNick") for action, params in bot.calls
            if action == "set_self_longnick"
        ]
        self.assertEqual(["今天有点嗨"], signature_calls)
        # 冷却期内第二次要求换签名：不再真的调用
        plugin.context.responses = [
            '{"mood": "平静", "mood_intensity": 0.4, "mood_note": "散场了", '
            '"update_signature": true, "new_signature": "换个签名"}',
        ]
        await plugin._mood_status_tick()
        self.assertEqual("平静", plugin.mood.get().mood)
        signature_calls = [
            params.get("longNick") for action, params in bot.calls
            if action == "set_self_longnick"
        ]
        self.assertEqual(1, len(signature_calls), "6小时冷却内不能反复改签名")

    async def test_mood_status_tick_skips_when_no_life(self):
        plugin, hooks = await self._plugin()
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "999", "空群")
        plugin._known_scopes["999"] = scope
        plugin.context.responses = []  # 若误调 LLM 会拿到默认 {"action":"ignore"}
        before = plugin.mood.get().mood
        await plugin._mood_status_tick()
        self.assertEqual(before, plugin.mood.get().mood)
        self.assertEqual([], plugin.context.llm_calls)


class ScopeIsolationTests(unittest.IsolatedAsyncioTestCase):
    """记忆跨群共享但带出处标注；原始聊天记录严格按群隔离。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _two_scope_plugin(self):
        hooks = _hooks()
        # 两个群都进白名单（生产里只有白名单群会有记忆，跨群共享也在它们之间）
        config = hooks._plugin_config() | {"group_whitelist": ["123", "222"]}
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        # A群用白名单内的 123（facade 测试要用事件走 create 路径）；B群 222 是别的群
        scope_a = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "A群")
        scope_b = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "222", "B群")
        for i in range(3):
            await plugin.storage.ingest_message(NormalizedMessage(
                platform="aiocqhttp", account_id="bot1", conversation_id="222",
                upstream_message_id=f"b{i}", sender_id="20002", sender_name="B友",
                text=f"B群的秘密话题{i}", occurred_at=datetime.now(timezone.utc).isoformat(),
                raw_event={"message_id": f"b{i}"}, parts=[], event_type="message.created",
            ))
        rows = await plugin.storage.recent_messages(scope_b, 1)
        return plugin, hooks, scope_a, scope_b, rows[0].message_id

    async def test_context_shares_memory_with_origin_but_not_chat(self):
        plugin, hooks, scope_a, scope_b, b_msg = await self._two_scope_plugin()
        # B 群沉淀一条记忆账本条目（模拟跨群记忆）
        await plugin.ledger.apply_proposal(
            scope_b,
            {"kind": "fact", "subject": "B群梗", "value": "B群的秘密话题",
             "confidence": 0.9, "evidence_ids": [b_msg]},
            "test-b-entry",
        )
        context = await plugin.context_builder.build(
            scope_a, "", recent_limit=10,
            cross_scope_ids=(scope_b,), cross_labels={scope_b: "222"},
        )
        data = json.loads(context)
        # 原始聊天绝不跨群
        self.assertNotIn("cross_scope_recent", data)
        self.assertEqual([], data["recent_messages"])
        self.assertEqual([], data["chat_evidence"])
        # 记忆跨群共享且带出处：B 的条目 origin=222，本群条目 origin=本群
        self.assertIn("memory_scope_note", data)
        origins = {item["subject"]: item["origin"] for item in data["memory_catalog"]}
        self.assertEqual("222", origins.get("B群梗"))
        all_origin = [item["origin"] for item in data["memory_catalog"]]
        self.assertTrue(all_origin)

    async def test_context_current_scope_items_are_marked_ben_group(self):
        plugin, hooks, scope_a, scope_b, b_msg = await self._two_scope_plugin()
        b_row = await plugin.storage.ingest_message(NormalizedMessage(
            platform="aiocqhttp", account_id="bot1", conversation_id="123",
            upstream_message_id="a9", sender_id="10002", sender_name="A友",
            text="A群的事", occurred_at=datetime.now(timezone.utc).isoformat(),
            raw_event={"message_id": "a9"}, parts=[], event_type="message.created",
        ))
        await plugin.ledger.apply_proposal(
            scope_a,
            {"kind": "fact", "subject": "A群梗", "value": "A群的事",
             "confidence": 0.9, "evidence_ids": [b_row.message_id]},
            "test-a-entry",
        )
        context = await plugin.context_builder.build(
            scope_a, "", recent_limit=10,
            cross_scope_ids=(scope_b,), cross_labels={scope_b: "222"},
        )
        data = json.loads(context)
        origins = {item["subject"]: item["origin"] for item in data["memory_catalog"]}
        self.assertEqual("本群", origins.get("A群梗"))

    async def test_memory_facade_memory_spans_scopes_chat_does_not(self):
        plugin, hooks, scope_a, scope_b, b_msg = await self._two_scope_plugin()
        await plugin.ledger.apply_proposal(
            scope_b,
            {"kind": "fact", "subject": "B群梗", "value": "B群的秘密话题",
             "confidence": 0.9, "evidence_ids": [b_msg]},
            "test-b-entry2",
        )
        plugin._known_scopes["123"] = scope_a
        plugin._known_scopes["222"] = scope_b  # 生产里由 _hydrate_known_scopes 恢复
        event = hooks.FakeEvent(hooks._group_raw("查一下", 10002), "查一下",
                                hooks.FakeBot(), wake=True)
        facade = await plugin._memory_facade(event)
        # 记忆检索跨群
        catalog = await facade.memory_catalog()
        self.assertTrue(any(item.subject == "B群梗" for item in catalog))
        # 原始聊天检索只在本群
        hits = await facade.search_chat_history("B群的秘密话题")
        self.assertEqual([], hits)

    async def test_l1_summaries_are_shared_across_scopes(self):
        """回归：只共享 L2/L3 时，群A刚聊完的内容（还没滚成 L2）在私信里完全
        不可见——bot 会说"我没聊天"。L1 也必须跨会话进上下文（带出处）。"""
        plugin, hooks, scope_a, scope_b, b_msg = await self._two_scope_plugin()
        await plugin.storage.store_summary(
            scope_b, 1, 1, 3, "B群刚才聊得火热", '[{"timeline":["聊了表情包新玩法"]}]',
            ["表情包"], [b_msg])
        context = await plugin.context_builder.build(
            scope_a, "", recent_limit=10,
            cross_scope_ids=(scope_b,), cross_labels={scope_b: "222"},
        )
        data = json.loads(context)
        l1 = [item for item in data["summary_memory"] if item["level"] == 1]
        self.assertTrue(l1, "L1 摘要必须出现在跨会话记忆里")
        self.assertEqual("222", l1[0]["origin"])
        self.assertIn("表情包", json.dumps(l1[0], ensure_ascii=False))


class MediaWipeTests(unittest.IsolatedAsyncioTestCase):
    """删除动作要真的删掉媒体文件，而不是只删数据库行。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_media_delete_all_removes_files(self):
        from src.media_archive import MediaArchive
        root = Path(self._tmp.name) / "media"
        archive = await MediaArchive(root).open()
        record = await archive.save_bytes(
            b"pngbytes", kind="image", mime="image/png", scope_id="123")
        stored = Path(await archive.get_path(record.item_id))
        self.assertTrue(stored.is_file())
        removed = await archive.delete_all()
        self.assertEqual(1, removed)
        self.assertFalse(stored.exists())
        self.assertEqual([], await archive.recent(None, 10))
        await archive.close()

    async def test_delete_all_action_purges_media_and_watermark(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "群")
        plugin._known_scopes["123"] = scope
        await plugin.storage.ingest_message(NormalizedMessage(
            platform="aiocqhttp", account_id="bot1", conversation_id="123",
            upstream_message_id="w1", sender_id="10002", sender_name="Tester",
            text="带媒体的删除测试", occurred_at=datetime.now(timezone.utc).isoformat(),
            raw_event={"message_id": "w1"}, parts=[], event_type="message.created",
        ))
        plugin._handled_event_ids.add("some-event")
        plugin.media = None  # 测试环境未启用媒体归档，delete_all 必须照样工作
        result = await plugin._page_api().write(
            "wipe_all", {"confirm": "确认清空"})
        payload = result.body if hasattr(result, "body") else result
        self.assertIn("ok", str(payload))
        stats = await plugin.storage.stats()
        self.assertEqual(0, stats["scopes"])
        self.assertEqual(0, stats["message_identities"])
        self.assertEqual(set(), plugin._handled_event_ids)


if __name__ == "__main__":
    unittest.main()
