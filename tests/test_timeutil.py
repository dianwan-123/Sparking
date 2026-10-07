# -*- coding: utf-8 -*-
"""时间基准：**存 UTC、看本地**。

实录：用户下午两点发消息，bot 说"现在是凌晨六点"——库里时间存的是 UTC，
而回复上下文/工具结果/聊天卡片都把 UTC 串原样递出去，模型只能按最新消息的 05:xx
猜"现在"。这里锁住：①配置时区（AstrBot 的 timezone）②所有展示路径都换算。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import timeutil  # noqa: E402


class TimeutilUnitTests(unittest.TestCase):
    def tearDown(self):
        timeutil.configure(None)

    def test_utc_iso_becomes_local(self):
        timeutil.configure("Asia/Shanghai")
        # 06:00Z == 14:00 +08:00：用户在下午两点发的那条
        self.assertEqual("2026-10-07 14:00",
                         timeutil.to_text("2026-10-07T06:00:00+00:00"))
        self.assertEqual("14:00", timeutil.to_text("2026-10-07T06:00:00Z", "%H:%M"))

    def test_epoch_and_naive_iso(self):
        timeutil.configure("Asia/Shanghai")
        from datetime import datetime, timezone

        epoch = int(datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual("2026-10-07 14:00", timeutil.to_text(epoch))
        self.assertEqual("2026-10-07 14:00", timeutil.to_text(str(epoch)))
        # 裸 ISO（无时区）按 UTC 处理——库里就是这个约定
        self.assertEqual("2026-10-07 14:00", timeutil.to_text("2026-10-07T06:00:00"))

    def test_garbage_passes_through(self):
        timeutil.configure("Asia/Shanghai")
        self.assertEqual("", timeutil.to_text(""))
        self.assertEqual("前天", timeutil.to_text("前天"))
        self.assertEqual("", timeutil.to_text(None))

    def test_zone_falls_back_to_host(self):
        name = timeutil.configure("不是时区")
        self.assertEqual("", name)
        self.assertIsNotNone(timeutil.zone())
        self.assertIsInstance(timeutil.text("%H:%M"), str)

    def test_localize_fields_only_touches_time_keys(self):
        timeutil.configure("Asia/Shanghai")
        row = timeutil.localize_fields({
            "occurred_at": "2026-10-07T06:00:00+00:00",
            "text": "2026-10-07T06:00:00+00:00",   # 普通字段不动
        })
        self.assertEqual("2026-10-07 14:00", row["occurred_at"])
        self.assertEqual("2026-10-07T06:00:00+00:00", row["text"])


class ContextTimeTests(unittest.IsolatedAsyncioTestCase):
    """回复上下文：既有 now（本地），消息时间也换算过。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self):
        import tests.test_plugin_hooks as hooks

        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._terminate, plugin)
        return plugin

    async def _terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_context_carries_local_now_and_times(self):
        timeutil.configure("Asia/Shanghai")
        self.addCleanup(timeutil.configure, None)
        plugin = await self._plugin()
        scope = await plugin.storage.get_or_create_scope(
            "aiocqhttp", "bot1", "123", "数学指令讨论群")
        plugin._known_scopes["123"] = scope
        from src.models import NormalizedMessage

        await plugin.ingest.ingest(NormalizedMessage(
            platform="aiocqhttp", account_id="bot1", conversation_id="123",
            upstream_message_id="up-1", sender_id="10002", sender_name="Tester",
            text="下午两点的消息", occurred_at="2026-10-07T06:00:00+00:00",
            raw_event={"message_id": "up-1"}, parts=[], event_type="message.created",
        ))
        envelope = json.loads(await plugin.context_builder.build(scope, "下午"))
        self.assertIn("now", envelope, "上下文必须给出当前本地时间")
        self.assertTrue(str(envelope["now"]).startswith("2026-") or envelope["now"])
        times = [row.get("occurred_at", "") for row in envelope.get("recent_messages", [])]
        self.assertTrue(times, "应该有最近消息")
        self.assertIn("2026-10-07 14:00", times, "消息时间要换算成本地（06:00Z → 14:00）")
        self.assertNotIn("2026-10-07T06:00:00+00:00", json.dumps(envelope))

    async def test_public_value_localizes(self):
        timeutil.configure("Asia/Shanghai")
        self.addCleanup(timeutil.configure, None)
        import DFYChat.main as main_module

        from src.models import SearchHit

        hit = SearchHit("m1", "r1", "s1", "10002", "Tester",
                        "2026-10-07T06:00:00+00:00", "四张卷子", 0.9, "qq-1")
        row = main_module._public_value(hit)
        self.assertEqual("2026-10-07 14:00", row["occurred_at"])
        self.assertEqual("qq-1", row["upstream_message_id"], "其它字段照旧")


if __name__ == "__main__":
    unittest.main()
