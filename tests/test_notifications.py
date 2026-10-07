from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from src.onebot import normalize_request_record
from src.storage import Storage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_state: dict = {}


class NotificationFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        if not _state:
            import sys

            sys.path.insert(0, str(PROJECT_ROOT))
            import tests.test_plugin_hooks as hooks  # installs astrbot stub

            _state["hooks"] = hooks
        self.hooks = _state["hooks"]

    def test_request_record_contains_flag(self):
        raw = {
            "post_type": "request", "request_type": "friend", "sub_type": "add",
            "flag": "abc123flag", "user_id": 777, "self_id": 1, "time": 1700000000,
            "comment": "我是群里的人",
        }
        record = normalize_request_record(raw)
        self.assertIsNotNone(record)
        self.assertEqual("private:777", record.conversation_id)
        self.assertIn("flag=abc123flag", record.text)
        self.assertEqual("request.friend", record.event_type)

    async def test_pending_events_processed_and_compacted(self):
        hooks = self.hooks
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        import os

        os.environ["ASTRBOT_STUB_DATA"] = tmp.name
        self.addCleanup(os.environ.pop, "ASTRBOT_STUB_DATA", None)
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        raw = {
            "post_type": "request", "request_type": "friend", "sub_type": "add",
            "flag": "flag-xyz", "user_id": 888, "self_id": 1, "time": int(__import__("time").time()),
            "comment": "加个好友",
        }
        record = normalize_request_record(raw)
        stored = await plugin.ingest.ingest(record)
        plugin._known_scopes[record.conversation_id] = stored.scope_id
        plugin.gateway.bot = hooks.FakeBot()

        plugin.context.responses = [
            json.dumps({
                "actions": [
                    {"tool": "qq_handle_friend_request",
                     "args": {"flag": "flag-xyz", "approve": True, "remark": "群友"}}
                ],
                "done": True,
            }, ensure_ascii=False)
        ]
        await plugin._process_pending_events()

        summaries = await plugin.storage.list_summaries(stored.scope_id, 5, levels=(3,))
        self.assertTrue(any("通知处理" in s.title for s in summaries))
        approvals = [c for c in plugin.gateway.bot.calls if c[0] == "set_friend_add_request"]
        self.assertEqual(1, len(approvals))
        self.assertTrue(approvals[0][1]["approve"])
        before = len(plugin.context.llm_calls)
        await plugin._process_pending_events()
        self.assertEqual(before, len(plugin.context.llm_calls))

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
