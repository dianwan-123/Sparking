from __future__ import annotations

import unittest

from src.humanization import (
    HumanizationConfig,
    PlanValidationError,
    _is_tool_garbage,
    salvage_message_plan,
)


class ToolGarbageTests(unittest.TestCase):
    def setUp(self):
        self.config = HumanizationConfig(max_sequences_per_hour=100)

    def test_garbage_patterns_detected(self):
        for piece in (
            ": send_ _ to_user , args :",
            "reply , : send_ _ to_user , args : 是一百个小作文",
            "tool : reply",
            "args : {}",
            "action : text",
        ):
            self.assertTrue(_is_tool_garbage(piece), piece)

    def test_normal_text_not_flagged(self):
        for piece in (
            "笑死 这组突然开始比惨大会（",
            "一天88确实连薯条都点不起大份的",
            "本姬在月球都收到你们的穷气了 awa",
        ):
            self.assertFalse(_is_tool_garbage(piece), piece)

    def test_salvage_drops_garbage_pieces(self):
        raw = ": reply , : send_ _ to_user , args : 本姬月读卷轴差点被他干烧（ , tool : reply"
        with self.assertRaises(PlanValidationError):
            salvage_message_plan(raw, self.config)

    def test_salvage_keeps_clean_parts_of_mixed_reply(self):
        raw = ": send_ _ to_user , args : 本姬月读卷轴差点被他干烧（ awa"
        plan = salvage_message_plan(raw, self.config)
        texts = [s.text for s in plan.segments]
        self.assertTrue(any("月读卷轴" in t for t in texts))
        self.assertFalse(any(_is_tool_garbage(t) for t in texts))


if __name__ == "__main__":
    unittest.main()
