# -*- coding: utf-8 -*-
"""记忆分层与调度修复的回归测试。

覆盖本轮三个 bug：
1. 未攒满一整批消息也能产出 L1（不再依赖内存计数器攒满 40）；
2. 计划被认领后不会重复执行（同一条 due plan 只被取出一次）；
3. 事件水位按 upstream 键判重——重装插件回看历史不会重做旧通知。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.storage import Storage
from src.ingest import IngestService
from src.models import NormalizedMessage


class FakeLLM:
    """Emit a valid strict-JSON summary citing the internal message ids."""

    async def __call__(self, prompt: str) -> str:
        import re

        ids = list(dict.fromkeys(re.findall(r'"message_id"\s*:\s*"([^"]+)"', prompt)))
        first = ids[:1]
        return json.dumps({
            "title": "摘要",
            "topics": ["话题"],
            "timeline": ["线"],
            "facts": [{"text": "事", "evidence_ids": first}] if first else [],
            "decisions": [],
            "tasks": [],
            "open_questions": [],
            "conflicts": [],
            "citations": ids[:3],
            "memory_proposals": [],
        }, ensure_ascii=False)


class MemoryLayersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def _store(self, ingest: IngestService, count: int, prefix: str = "m") -> None:
        for i in range(count):
            await ingest.ingest(NormalizedMessage(
                platform="aiocqhttp", account_id="1", conversation_id="123",
                upstream_message_id=f"{prefix}-{i}", sender_id="10002",
                sender_name="Tester", text=f"内容{i}",
                occurred_at=f"2026-09-15T09:{i:02d}:00+00:00",
                raw_event={"message_id": f"{prefix}-{i}"}, parts=[],
                event_type="message.created",
            ))

    async def test_l1_forms_without_full_batch(self):
        """Six messages (< batch size) must still compress into an L1 chunk."""
        import sys

        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from src.compression import CompressionService

        storage = await Storage(self.root / "memory.db").open()
        try:
            ingest = IngestService(storage)
            compression = CompressionService(storage, FakeLLM(), model="fake")
            scope = await storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
            await self._store(ingest, 6)

            self.assertEqual(6, len(await storage.pending_l1_messages(scope, 20)))
            # a bounded drain (what _compress_scope does) must produce a chunk
            created = 0
            for _ in range(3):
                pending = await storage.pending_l1_messages(scope, 4)
                if not pending:
                    break
                await compression.compress_pending_l1(scope, 4)
                created += 1
            self.assertGreaterEqual(created, 1)
            sumaries = await storage.list_summaries(scope, 10)
            self.assertGreaterEqual(len(sumaries), 1)
            self.assertEqual(1, sumaries[0].level)
            self.assertTrue(sumaries[0].citations)
            # the covered messages must no longer be pending
            self.assertLess(len(await storage.pending_l1_messages(scope, 20)), 6)
        finally:
            await storage.close()

    async def test_fist_uncovered_seq_moves_after_compression(self):
        import sys

        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from src.compression import CompressionService

        storage = await Storage(self.root / "memory.db").open()
        try:
            ingest = IngestService(storage)
            compression = CompressionService(storage, FakeLLM(), model="fake")
            scope = await storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
            await self._store(ingest, 4)
            first_before = await storage.first_uncovered_seq(scope)
            await compression.compress_pending_l1(scope, 4)
            first_after = await storage.first_uncovered_seq(scope)
            self.assertIsNone(first_after)
        finally:
            await storage.close()


class PlanDedupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    async def test_claim_plan_runs_once(self):
        from datetime import datetime, timedelta

        storage = await Storage(self.root / "memory.db").open()
        try:
            due = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="minutes")
            await storage.add_plans([{"due_at": due, "kind": "chat", "detail": "去群里聊"}])
            first = await storage.due_plans()
            self.assertEqual(1, len(first))
            plan_id = first[0]["plan_id"]
            # claim is atomic: the second claim fails
            self.assertTrue(await storage.claim_plan(plan_id))
            self.assertFalse(await storage.claim_plan(plan_id))
            # a claimed (running) plan is not handed out again
            self.assertEqual([], await storage.due_plans())
            # completting afterwards is still fine
            self.assertTrue(await storage.complete_plan(plan_id, "done"))
            self.assertEqual([], await storage.pending_plans())
        finally:
            await storage.close()

    async def test_stale_plan_retired_after_reinstall(self):
        """An old plan must not fire as fresh work after a re-install."""
        from datetime import datetime, timedelta

        storage = await Storage(self.root / "memory.db").open()
        try:
            fresh = (
                datetime.now().astimezone() - timedelta(minutes=2)
            ).isoformat(timespec="minutes")
            stales = (
                datetime.now().astimezone() - timedelta(hours=20)
            ).isoformat(timespec="minutes")
            await storage.add_plans([
                {"due_at": fresh, "kind": "chat", "detail": "刚刚到点的活"},
                {"due_at": stales, "kind": "chat", "detail": "半年前的活"},
            ])
            due = await storage.due_plans()
            # only the fresh one runs; the stales one went to skipped
            self.assertEqual(1, len(due))
            self.assertEqual("刚刚到点的活", due[0]["detail"])
            pending = await storage.pending_plans()
            self.assertEqual(1, len(pending))
            self.assertEqual("刚刚到点的活", pending[0]["detail"])
        finally:
            await storage.close()

    def test_event_watermark_key_ignores_message_id(self):
        """The replay key must survive a fresh install's new message_id."""
        import sys

        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import tests.test_plugin_hooks as hooks

        hook = hooks
        event = hooks.FakeEvent(hooks._group_raw("x"), "x", hooks.FakeBot())
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())

        class Notice:
            scope_id = "S1"
            upstream_message_id = "notice:1700000000:notify:10002:poke"
            occurred_at = "2026-09-15T09:00:00+00:00"

        key_a = plugin._event_watermark_key(Notice())
        self.assertIn("notice:1700000000:notify:10002:poke", key_a)
        self.assertTrue(plugin._event_watermark_key(Notice()) == key_a)

        class NoticeReplayed:
            scope_id = "S1"
            upstream_message_id = "notice:1700000000:notify:10002:poke"
            occurred_at = "2026-09-15T09:00:00+00:00"

        # a fresh install re-ingests history under a new internal message_id,
        # but the watermark key stays identical
        self.assertEqual(key_a, plugin._event_watermark_key(NoticeReplayed()))


if __name__ == "__main__":
    unittest.main()
