from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.humanization import HumanizationConfig, parse_message_plan
from src.storage import Storage


class AffinityAndThoughtTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    async def test_affinity_defaults_adjust_and_clamp(self):
        storage = await Storage(Path(self._tmp.name) / "m.db").open()
        try:
            scope = await storage.get_or_create_scope("aiocqhttp", "1", "g")
            base = await storage.get_affinity(scope, "u1")
            self.assertEqual(50.0, base["warmth"])
            state = await storage.adjust_affinity(scope, "u1", 15, "聊得来")
            self.assertEqual(65.0, state["warmth"])
            self.assertEqual("聊得来", state["note"])
            state = await storage.adjust_affinity(scope, "u1", -200)
            self.assertEqual(0.0, state["warmth"])
            self.assertEqual("聊得来", state["note"])  # empty note keeps old
            other = await storage.get_affinity(scope, "u2")
            self.assertEqual(50.0, other["warmth"])
        finally:
            await storage.close()

    def test_thought_field_allowed_and_ignored(self):
        config = HumanizationConfig(max_sequences_per_hour=100)
        raw = json.dumps({
            "thought": "他在问天气，我查一下再回",
            "mode": "single",
            "message": {"action": "text", "text": "今天晴（"},
        }, ensure_ascii=False)
        plan = parse_message_plan(raw, config)
        self.assertEqual("今天晴（", plan.segments[0].text)

        raw_seq = json.dumps({
            "thought": "连发两条",
            "mode": "sequence",
            "segments": [
                {"action": "text", "text": "笑死"},
                {"action": "text", "text": "真的假的（"},
            ],
        }, ensure_ascii=False)
        plan = parse_message_plan(raw_seq, config)
        self.assertEqual(2, len(plan.segments))


if __name__ == "__main__":
    unittest.main()
