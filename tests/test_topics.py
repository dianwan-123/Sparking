from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.compression import CompressionService
from src.ingest import IngestService
from src.models import NormalizedMessage, utc_now
from src.storage import Storage


class GroupTopicsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    async def test_topics_recorded_per_scope_and_queried(self):
        storage = await Storage(Path(self._tmp.name) / "m.db").open()
        try:
            ingest = IngestService(storage)
            scope_a = await storage.get_or_create_scope("aiocqhttp", "1", "groupA")
            scope_b = await storage.get_or_create_scope("aiocqhttp", "1", "groupB")
            for index in range(4):
                await ingest.ingest(NormalizedMessage(
                    platform="aiocqhttp", account_id="1", conversation_id="groupA",
                    upstream_message_id=str(50000 + index), sender_id="u", sender_name="T",
                    text=f"我们在聊音游定数 {index}", occurred_at=utc_now(),
                    raw_event={"message_id": 50000 + index},
                ))

            async def llm(prompt: str) -> str:
                import re

                cite = re.findall(r'"message_id":\s*"([0-9a-f]+)"', prompt)[0]
                return json.dumps({
                    "title": "音游讨论", "topics": ["音游", "定数"],
                    "timeline": [], "facts": [{"text": "聊音游", "evidence_ids": [cite]}],
                    "decisions": [], "tasks": [], "open_questions": [],
                    "conflicts": [], "memory_proposals": [],
                    "citations": [cite],
                }, ensure_ascii=False)

            service = CompressionService(storage, llm)
            messages = await storage.recent_messages(scope_a, 10)
            await service.compress_l1(scope_a, messages)
            topics_a = await storage.recent_topics(scope_a, 10)
            self.assertTrue(any(t["topic"] == "音游" for t in topics_a))
            topics_b = await storage.recent_topics(scope_b, 10)
            self.assertEqual([], topics_b)
        finally:
            await storage.close()


if __name__ == "__main__":
    unittest.main()
