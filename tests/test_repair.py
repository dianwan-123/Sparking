from __future__ import annotations

import unittest

from src.humanization import (
    HumanizationConfig,
    PlanValidationError,
    parse_message_plan,
    salvage_message_plan,
)


class RepairAndSalvageTests(unittest.TestCase):
    def setUp(self):
        self.config = HumanizationConfig(max_sequences_per_hour=100)

    def test_delay_only_fragments_merge_into_next_segment(self):
        raw = (
            '{"mode": "sequence", "segments": ['
            '{"action": "text", "text": "笑死 这组突然开始比惨大会（"},'
            '{"delay_seconds": 0.8},'
            '{"action": "text", "text": "一天88确实连薯条都点不起大份的"}]}'
        )
        plan = parse_message_plan(raw, self.config)
        self.assertEqual(2, len(plan.segments))
        self.assertEqual(0.8, plan.segments[1].delay_seconds)

    def test_nested_text_object_is_unwrapped(self):
        raw = (
            '{"mode": "sequence", "segments": ['
            '{"action": "text", "text": {"text": "能看是能看 但B站反手甩了个412（"}, '
            '"reply_to_message_id": "1138123772", "delay_seconds": 1}]}'
        )
        plan = parse_message_plan(raw, self.config)
        self.assertEqual(1, len(plan.segments))
        self.assertIn("412", plan.segments[0].text)
        self.assertEqual("1138123772", plan.segments[0].reply_to_message_id)

    def test_salvage_extracts_text_from_malformed_json(self):
        raw = (
            '{"mode": "sequence", "segments": [{"action": "text", "text": {"text": "第一条（"}, '
            '"delay_seconds": 1}, {"action": "text", "text": {"text": "第二条 awa"}, '
            '"delay_seconds": 3}]}'
        )
        plan = salvage_message_plan(raw, self.config)
        texts = [s.text for s in plan.segments]
        self.assertIn("第一条（", texts)
        self.assertIn("第二条 awa", texts)

    def test_salvage_plain_prose(self):
        plan = salvage_message_plan("就是一句普通的话（", self.config)
        self.assertEqual(1, len(plan.segments))
        self.assertIn("普通的话", plan.segments[0].text)

    def test_salvage_never_returns_raw_json(self):
        raw = '{"mode": "sequence", "segments": [{"delay_seconds": 1}]}'
        with self.assertRaises(PlanValidationError):
            salvage_message_plan(raw, self.config)


if __name__ == "__main__":
    unittest.main()
