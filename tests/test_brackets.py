# -*- coding: utf-8 -*-
"""括号存活回归：散文/计划文本里的 [] {} () 不再被清洗。"""
from __future__ import annotations

import unittest

from src.humanization import (
    HumanizationConfig,
    humanize_plan,
    parse_message_plan,
    salvage_message_plan,
)


class BracketSurvivalTests(unittest.TestCase):
    def test_prose_salvage_keeps_brackets(self):
        """模型没按 JSON 回复时，散文里的括号必须原样保留。"""
        raw = "大家好 这是列表 [1] [2] 和字典 {a: 1} 还有 (括号) 的说明"
        plan = salvage_message_plan(raw, HumanizationConfig())
        joined = "".join(s.text or "" for s in plan.segments)
        # 所有括号字符存活（可能在空格处被分成多条气泡，但括号不丢）
        for ch in "[]{}()":
            self.assertIn(ch, joined)
        self.assertIn("[1]", joined)
        self.assertIn("(括号)", joined)
        self.assertIn("字典", joined)
        # 计划关键字词不再被无差别删除
        self.assertIn("说明", joined)

    def test_markdown_link_brackets_survive_salvage(self):
        raw = "看这个 [链接](https://example.com) 和 {json} 例子"
        plan = salvage_message_plan(raw, HumanizationConfig())
        joined = "".join(s.text or "" for s in plan.segments)
        self.assertIn("[链接](https://example.com)", joined)
        self.assertIn("{json}", joined)

    def test_leaked_plan_json_still_gets_cleaned(self):
        """真的像泄漏计划 JSON 的散文仍然剥掉噪声。"""
        raw = 'mode: "sequence" segments: [{action: "text" text: "真的内容 好的"}]'
        plan = salvage_message_plan(raw, HumanizationConfig())
        joined = "".join(s.text or "" for s in plan.segments)
        self.assertIn("真的内容", joined)
        self.assertNotIn("{", joined)
        self.assertNotIn("mode", joined)

    def test_plan_text_field_keeps_brackets(self):
        """正常计划 JSON 的 text 字段带括号：整条管线原样通过。"""
        raw = ('{"thought": "回答", "mode": "single", "message": '
               '{"action": "text", "text": "数组写法 [1,2] 对象 {a:1} 括号 (ok)"}}')
        plan = parse_message_plan(raw, HumanizationConfig())
        plan = humanize_plan(plan, HumanizationConfig())
        joined = "".join(s.text or "" for s in plan.segments)
        self.assertIn("[1,2]", joined)
        self.assertIn("{a:1}", joined)
        self.assertIn("(ok)", joined)


if __name__ == "__main__":
    unittest.main()
