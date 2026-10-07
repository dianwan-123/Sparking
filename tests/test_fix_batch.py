from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.compression import CompressionService
from src.ingest import IngestService
from src.models import NormalizedMessage, utc_now
from src.qq_gateway import QQGateway
from src.onebot import normalize_event
from src.qq_services import QzoneService
from src.storage import Storage


class _CookieBot:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action == "get_cookies":
            return {"cookies": " p_skey = abc123 ;  uin =o12345 ;", "bkn": "12345"}
        if action == "get_login_info":
            return {"user_id": 12345}
        return {"status": "ok", "retcode": 0, "data": None}


class FixBatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    async def test_cookie_keys_are_stripped_and_bkn_used(self):
        fetched = []

        async def fake_getter(path, cookie, params):
            fetched.append((path, params))
            return {"msglist": []}

        qzone = QzoneService(
            QQGateway(_CookieBot()), getter=fake_getter, ttl_seconds=600,
        )
        await qzone.list_feeds(1)
        self.assertEqual(1, len(fetched))
        self.assertEqual(12345, fetched[0][1]["g_tk"])

    def test_normalize_private_message(self):
        raw = {
            "post_type": "message", "message_type": "private", "sub_type": "friend",
            "message_id": 99, "user_id": 555, "self_id": 1, "time": 1700000000,
            "message": [{"type": "text", "data": {"text": "在吗"}}], "raw_message": "在吗",
        }
        message = normalize_event(raw)
        self.assertIsNotNone(message)
        self.assertEqual("private:555", message.conversation_id)
        self.assertEqual("在吗", message.text)

    async def test_roll_up_builds_l2_from_l1_chunks(self):
        import re

        path = Path(self._tmp.name) / "m.db"
        storage = await Storage(path).open()
        try:
            ingest = IngestService(storage)
            scope = await storage.get_or_create_scope("aiocqhttp", "1", "g")
            for index in range(6):
                await ingest.ingest(NormalizedMessage(
                    platform="aiocqhttp", account_id="1", conversation_id="g",
                    upstream_message_id=str(40000 + index), sender_id="u", sender_name="T",
                    text=f"聊天内容{index}", occurred_at=utc_now(),
                    raw_event={"message_id": 40000 + index},
                ))

            async def llm(prompt: str) -> str:
                if '"summary_id"' in prompt:
                    cite = re.findall(r'"citations":\s*\["([0-9a-f]+)"\]', prompt)[0]
                else:
                    cite = re.findall(r'"message_id":\s*"([0-9a-f]+)"', prompt)[0]
                return json.dumps({
                    "title": "T", "topics": [], "timeline": [],
                    "facts": [{"text": "f", "evidence_ids": [cite]}],
                    "decisions": [], "tasks": [], "open_questions": [],
                    "conflicts": [], "citations": [cite], "memory_proposals": [],
                }, ensure_ascii=False)

            service = CompressionService(storage, llm)
            for _ in range(3):
                await service.compress_pending_l1(scope, 2)
            created = await service.roll_up(scope, 3)
            self.assertTrue(any(s.level == 2 for s in created))
            pending_l2 = await storage.unconsumed_summaries(scope, 2, 3, 5)
            self.assertEqual(1, len(pending_l2))
        finally:
            await storage.close()


class HumanizationAtTests(unittest.IsolatedAsyncioTestCase):
    async def test_at_field_parsed_and_validated(self):
        from src.humanization import PlanValidationError, parse_message_plan

        plan = parse_message_plan(
            '{"mode": "single", "message": {"action": "text", "text": "看这个", "at": ["123", 456]}}'
        )
        self.assertEqual(("123", "456"), plan.segments[0].at)
        plan = parse_message_plan(
            '{"mode": "single", "message": {"action": "text", "text": "hi", "at": 789}}'
        )
        self.assertEqual(("789",), plan.segments[0].at)
        with self.assertRaises(PlanValidationError):
            parse_message_plan(
                '{"mode": "single", "message": {"action": "text", "text": "x", "at": ' + str([1] * 6) + '}}'
            )


if __name__ == "__main__":
    unittest.main()
