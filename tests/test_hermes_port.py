# -*- coding: utf-8 -*-
"""Hermes 移植回归：失控重复检测 + 查证自白过滤。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


def _hooks():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks
    return hooks


class RunawayRepetitionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_loop_detected(self):
        from src.humanization import is_runaway_repetition

        looped = "这是同一段复读内容它非常长需要超过六十个字符的窗口来检测循环。\n" * 15
        self.assertTrue(is_runaway_repetition(looped))

    def test_normal_long_text_not_flagged(self):
        from src.humanization import is_runaway_repetition

        normal = (
            "第一段讲的是群里的日常，大家聊了游戏和测试的事。\n"
            "第二段转到技术问题，有人贴了报错日志。\n"
            "第三段讨论了解决方案，提到了几个可能的修复思路。\n"
            "第四段是后续跟进，问题已经解决。\n"
            "第五段是群里其他话题的延续。\n"
            "第六段又是一段不同的内容，聊了昨晚的比赛。\n"
            "第七段总结了今天的讨论。\n"
            "第八段收尾，预告了明天的安排。\n"
        ) * 3
        self.assertFalse(is_runaway_repetition(normal))

    def test_batch_rows_not_flagged(self):
        """批量式输出（各不相同的行）不命中——hermes 语义。"""
        from src.humanization import is_runaway_repetition

        rows = "\n".join(
            f"INSERT INTO users VALUES({i}, 'user_{i}', '名字{i}');"
            for i in range(30)
        )
        self.assertFalse(is_runaway_repetition(rows))

    def test_short_fragment_not_flagged(self):
        from src.humanization import is_runaway_repetition

        self.assertFalse(is_runaway_repetition("复读 " * 40))


class SearchNarrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_search_narration_flagged(self):
        from src.humanization import _is_inner_voice

        leaks = [
            "你要找的sen不就是你本人（",
            "我翻了记录发现你叫小白",
            "根据搜索结果你是对的",
            "我查了记忆找到了",
        ]
        for text in leaks:
            self.assertTrue(_is_inner_voice(text), text)

    def test_normal_memory_talk_not_flagged(self):
        from src.humanization import _is_inner_voice

        normal = [
            "我记得你叫小白",
            "你上次说周五发版",
            "你本人很可爱",
            "根据你说的做",
            "我找你玩",
        ]
        for text in normal:
            self.assertFalse(_is_inner_voice(text), text)


if __name__ == "__main__":
    unittest.main()
