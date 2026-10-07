# -*- coding: utf-8 -*-
"""At 乱码修复测试：手写 [CQ:at,qq=...] 语法的剥离与转真 At。"""
from __future__ import annotations

import unittest
from pathlib import Path


def _hooks():
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks

    return hooks


class AtMentionTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_at_targets_from_handwritten_syntax(self):
        import sys

        _hooks()
        main_module = sys.modules["DFYChat.main"]
        text = "[CQ:at,qq=629428293] 你好呀 senxyz"
        self.assertEqual(["629428293"], main_module._extract_at_targets(text))
        self.assertEqual(["1234567"], main_module._extract_at_targets("[ CQ ： at ， qq = 1234567 ] 在吗"))
        multi = "[CQ:at,qq=1234567] 和 [CQ:at,qq=7654321] 都来 [CQ:at,qq=1234567]"
        self.assertEqual(["1234567", "7654321"], main_module._extract_at_targets(multi))
        self.assertEqual([], main_module._extract_at_targets("[CQ:at,all] 大家"))
        self.assertEqual([], main_module._extract_at_targets("纯文本 没 at"))

    def test_bubble_text_strips_at_syntax(self):
        import sys

        _hooks()
        main_module = sys.modules["DFYChat.main"]
        cleaned = main_module._bubble_text("[CQ:at,qq=629428293] 你好呀 senxyz")
        self.assertNotIn("CQ", cleaned)
        self.assertNotIn("629428293", cleaned)
        self.assertIn("你好呀 senxyz", cleaned)
        for raw in (
            "[CQ:at,qq=629428293]hello",
            "（CQ:at,qq=629428293）hello",
            "[at:qq=629428293] hello",
        ):
            out = main_module._bubble_text(raw)
            self.assertNotIn("qq=", out)
            self.assertIn("hello", out)

    def test_bubble_text_keeps_plain_text(self):
        import sys

        _hooks()
        main_module = sys.modules["DFYChat.main"]
        self.assertEqual("不行不行 这个真看不了", main_module._bubble_text("不行不行，这个真看不了。"))
        self.assertEqual("你好", main_module._bubble_text("  你好  "))
        self.assertNotIn("\n", main_module._bubble_text("不行不行，这个真看不了。\n下一行"))


if __name__ == "__main__":
    unittest.main()
