# -*- coding: utf-8 -*-
"""JSON 宽容解析测试：模型输出常见瑕疵（尾逗号、前后杂文、代码块）不应让工具空参。"""
from __future__ import annotations

import unittest

from src.json_utils import compact_json, parse_json_object, parse_json_value


class ParseJsonObjectTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual({"a": 1}, parse_json_object('{"a": 1}'))

    def test_trailing_comma(self):
        # 模型常见输出：{"a":1,} —— AstrBot 侧 json.loads 会失败并丢弃参数，插件侧必须兜住
        self.assertEqual({"a": 1}, parse_json_object('{"a": 1,}'))

    def test_nested_trailing_commas(self):
        raw = '前言 {"a": [1, 2,], "b": {"c": 3,},} 后记'
        self.assertEqual({"a": [1, 2], "b": {"c": 3}}, parse_json_object(raw))

    def test_text_around_object(self):
        self.assertEqual({"x": "y"}, parse_json_object('这是参数：{"x": "y"} 完毕'))

    def test_non_dict_top_is_none(self):
        self.assertIsNone(parse_json_object("[1, 2]"))
        self.assertIsNone(parse_json_object("garbage"))

    def test_string_with_brace(self):
        # 字符串值里出现 }: 不能让花括号配对提前退出
        self.assertEqual(
            {"t": "a}b"}, parse_json_object('{"t": "a}b"}')
        )


class ParseJsonValueTests(unittest.TestCase):
    def test_array_trailing_comma(self):
        self.assertEqual(["m1", "m2"], parse_json_value('["m1", "m2",]'))

    def test_scalar_and_null(self):
        self.assertEqual("just text", parse_json_value('"just text"'))
        self.assertIsNone(parse_json_value("null"))
        self.assertIsNone(parse_json_value("garbage"))

    def test_code_fence(self):
        raw = "```json\n{\"id\": \"abc\",}\n```"
        self.assertEqual({"id": "abc"}, parse_json_value(raw))

    def test_extract_from_prose(self):
        self.assertEqual(["a"], parse_json_value("media: [\"a\",] 就这些"))


    def test_none_and_empty_input(self):
        # 实录：模型空输出 → _llm_text 返回 None → parse_json_object(None) 崩 strip
        self.assertIsNone(parse_json_object(None))
        self.assertIsNone(parse_json_object(""))
        self.assertIsNone(parse_json_object("   "))
        self.assertIsNone(parse_json_value(None))
        self.assertIsNone(parse_json_value(""))


class CompactJsonTests(unittest.TestCase):
    def test_truncation(self):
        out = compact_json({"k": "x" * 100}, limit=30)
        self.assertLessEqual(len(out), 31)
        self.assertTrue(out.endswith("…"))


if __name__ == "__main__":
    unittest.main()
