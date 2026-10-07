# -*- coding: utf-8 -*-
"""v0.16.1 回归测试：孤儿代码/识图拒绝/自身回复入记忆/历史通知基线。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


def _hooks():
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks

    return hooks


class OrphanTailTests(unittest.TestCase):
    def test_store_image_note_has_no_archive_orphan(self):
        """孤儿旧归档体（引用未定义 raw/text）必须已从 _store_image_note 移除。"""
        root = Path(__file__).resolve().parents[1]
        main_text = (root / "main.py").read_text(encoding="utf-8")
        start = main_text.find("async def _store_image_note")
        end = main_text.find("async def _run_scheduled_task", start)
        body = main_text[start:end]
        self.assertNotIn("媒体归档异常", body)
        self.assertNotIn("raw.get(", body)
        self.assertNotIn("extract_urls(", body)


class VisionRefusalTests(unittest.TestCase):
    def test_refusal_regex_catches_the_real_reply(self):
        root = Path(__file__).resolve().parents[1]
        import sys

        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import tests.test_plugin_hooks  # stub install
        main_mod = sys.modules["DFYChat.main"]

        text = ("我这边无法查看或识别这张图片（当前模型不支持视觉），"
                "因此无法客观描述其内容和文字。请重新上传可访问的图片")
        self.assertTrue(main_mod.LongMemoryAgentPlugin._is_vision_refusal(text))
        self.assertFalse(main_mod.LongMemoryAgentPlugin._is_vision_refusal(
            "一只橘猫趴在键盘上睡觉"))


class SelfReplyMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def test_wake_reply_is_ingested_as_own_message(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            bot = hooks.FakeBot()
            plugin.context.responses = [
                # 1) 判定模型（decision.decide）
                '{"action": "text", "intent": "问候", "query": "", "wait_seconds": 0}',
                # 2) 回复模型（_reply_plan, agent）
                '{"thought": "问候", "mode": "single", '
                '"message": {"action": "text", "text": "在的 怎么了"}}',
            ]
            event = hooks.FakeEvent(hooks._group_raw("在吗", 10002), "在吗", bot,
                                    wake=True)
            await plugin.on_onebot_event(event)
            import asyncio as _aio

            for _ in range(50):
                if event.sent:
                    break
                await _aio.sleep(0.05)
            scope = plugin._known_scopes.get("123")
            self.assertIsNotNone(scope)
            own = []
            for _ in range(40):
                messages = await plugin.storage.recent_messages(scope, 20)
                own = [m for m in messages
                       if m.sender_id == "bot1" and "在的" in m.text]
                if own:
                    break
                await _aio.sleep(0.05)
            self.assertTrue(own, "bot 自己的回复应入记忆")
        finally:
            await plugin.terminate()

    async def test_old_notice_is_skipped_by_baseline(self):
        """基线之前的历史通知（重装回放）不再触发处理。"""
        import json as _json
        import time as _time

        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()  # baseline = now
        try:
            old_raw = {
                "post_type": "request", "request_type": "friend",
                "sub_type": "add", "flag": "old-flag", "user_id": 888,
                "self_id": 1,
                "time": _time.time() - 90 * 24 * 3600,  # 90 天前
                "comment": "很久以前的申请",
            }
            from src.onebot import normalize_request_record

            record = normalize_request_record(old_raw)
            stored = await plugin.ingest.ingest(record)
            plugin._known_scopes[record.conversation_id] = stored.scope_id
            await plugin._process_pending_events()
            # 基线过滤：未调用判定模型
            self.assertEqual(0, len(plugin.context.llm_calls))
            # 且不记入 handled（下轮基线同样拦住）
            self.assertNotIn(stored.message_id, plugin._handled_event_ids)
        finally:
            await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
