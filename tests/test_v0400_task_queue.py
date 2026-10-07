# -*- coding: utf-8 -*-
"""v0.40.0 回归：统一任务与日程系统（一切皆任务、队列式、可设区间、短期/长期）。

用户要求：把**所有事情都抽象成任务**（一次回复、一轮 agent、工具调用、主动通知、总结…）；
**队列式处理**；bot 可以自己给自己加任务/日程规划；分**短期**（跑 1 次或几次）与
**长期**（定时反复）；**都可以设时间区间，超出区间不执行**；记录要详细。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.task_queue import (  # noqa: E402
    KIND_AGENT,
    KIND_NOTIFY,
    KIND_REPLY,
    KIND_TOOL,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_EXPIRED,
    STATUS_PENDING,
    TaskQueue,
    in_window,
    next_window_start,
    window_expired,
)


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class WindowTests(unittest.TestCase):
    def test_absolute_window_expires(self):
        past = {"before": "2020-01-01T00:00:00+00:00"}
        self.assertTrue(window_expired(past), "截止时间已过 → 任务作废（超出区间不执行）")
        self.assertFalse(in_window(past))
        future = {"after": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()}
        self.assertFalse(in_window(future), "还没到起点 → 不执行")
        self.assertIsNotNone(next_window_start(future), "要能算出下一个可执行时刻")

    def test_daily_window(self):
        start = (datetime.now().astimezone() + timedelta(minutes=5)).strftime("%H:%M")
        end = (datetime.now().astimezone() + timedelta(minutes=10)).strftime("%H:%M")
        self.assertFalse(in_window({"daily": [start, end]}), "还没到今日时段")
        now_local = datetime.now().astimezone()
        inside = {"daily": [(now_local - timedelta(minutes=1)).strftime("%H:%M"),
                            (now_local + timedelta(minutes=1)).strftime("%H:%M")]}
        self.assertTrue(in_window(inside), "在时段内 → 可执行")


class TaskQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "群")
        plugin._known_scopes["123"] = scope
        sent: list[str] = []

        async def fake_send(scope_id, text):
            sent.append(str(text))

        plugin._task_send_text = fake_send
        return plugin, scope, sent

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_queue_is_wired_and_persistent(self):
        plugin, scope, _sent = await self._plugin()
        self.assertIsNotNone(plugin.task_queue, "任务队列要随插件初始化")
        self.assertIsNotNone(plugin.task_queue_runner)
        table = await plugin.storage._fetchone(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='tasks'")
        self.assertEqual(1, int(table["n"]), "tasks 表要建好（跨重启不丢）")

    async def test_short_term_once_and_n_times(self):
        plugin, scope, sent = await self._plugin()
        queue = plugin.task_queue
        await queue.add(KIND_NOTIFY, detail="一次性提醒", scope_id=scope, conversation="123")
        await queue.add(KIND_NOTIFY, detail="跑两次", scope_id=scope, conversation="123",
                        max_runs=2)
        await plugin.task_queue_runner.tick()
        await asyncio.sleep(0.4)
        stats = await queue.stats()
        self.assertEqual(1, stats[STATUS_DONE], "一次性任务跑完变 done")
        self.assertIn("一次性提醒", sent)

    async def test_expired_window_never_runs(self):
        plugin, scope, sent = await self._plugin()
        queue = plugin.task_queue
        await queue.add(KIND_NOTIFY, detail="过期任务", scope_id=scope, conversation="123",
                        window={"before": "2020-01-01T00:00:00+00:00"})
        await plugin.task_queue_runner.tick()
        await asyncio.sleep(0.3)
        stats = await queue.stats()
        self.assertEqual(1, stats[STATUS_EXPIRED], "超出区间的任务要作废且不执行")
        self.assertNotIn("过期任务", sent, "绝不能在区间外执行")

    async def test_long_term_reschedules_with_interval(self):
        plugin, scope, _sent = await self._plugin()
        queue = plugin.task_queue
        task = await queue.add(KIND_AGENT, detail="每小时巡群", scope_id=scope,
                               conversation="123", interval_seconds=3600, max_runs=-1)
        self.assertTrue(task.is_long_term)
        await plugin.task_queue_runner.tick()
        await asyncio.sleep(0.4)
        again = (await queue.list(limit=5))[0]
        self.assertEqual(STATUS_PENDING, again.status, "长期任务跑完要回到队列")
        self.assertGreaterEqual(again.runs_done, 1)
        self.assertTrue(again.next_run_at, "长期任务要排下一次")

    async def test_same_conversation_runs_serially(self):
        """队列式：同一会话同一时刻只跑一个。"""
        plugin, scope, _sent = await self._plugin()
        queue = plugin.task_queue
        for index in range(3):
            await queue.add(KIND_NOTIFY, detail=f"串行{index}", scope_id=scope,
                            conversation="123")
        claimed = await queue.claim_due()
        self.assertEqual(1, len(claimed), "同会话任务必须排队，一次只跑一个")

    async def test_priority_orders_the_queue(self):
        plugin, scope, _sent = await self._plugin()
        queue = plugin.task_queue
        await queue.add(KIND_NOTIFY, detail="低", scope_id=scope, conversation="123", priority=-2)
        await queue.add(KIND_NOTIFY, detail="高", scope_id=scope, conversation="123", priority=3)
        claimed = await queue.claim_due()
        self.assertEqual("高", claimed[0].detail, "优先级高的先跑")
        await queue.finish(claimed[0])

    async def test_failure_retries_then_marks_failed(self):
        plugin, scope, _sent = await self._plugin()
        queue = plugin.task_queue

        async def broken(_task):
            raise RuntimeError("boom")

        plugin.task_queue_runner.handlers[KIND_REPLY] = broken
        task = await queue.add(KIND_REPLY, detail="会失败", scope_id=scope, conversation="123")
        for _ in range(3):
            await queue.update(task.task_id, next_run_at=datetime.now(timezone.utc).isoformat())
            await plugin.task_queue_runner.tick()
            await asyncio.sleep(0.3)
        final = (await queue.list(limit=5))[0]
        self.assertEqual("failed", final.status, "连续失败要标记 failed")
        self.assertIn("boom", final.last_error, "失败原因要记下来")

    async def test_bot_tools_add_list_cancel(self):
        plugin, scope, _sent = await self._plugin()
        hooks = _hooks()
        event = hooks.FakeEvent(hooks._group_raw("排个任务", 9501), "排个任务",
                                hooks.FakeBot(), wake=True)
        added = await plugin.task_add_tool(
            event, task_kind=KIND_AGENT, detail="晚上总结群里话题",
            delay_minutes=30, max_runs=1)
        import json as _json

        payload = _json.loads(added)
        task_id = payload["task_id"]
        self.assertTrue(task_id)
        listed = await plugin.task_list_tool(event, "", 10)
        self.assertIn("晚上总结群里话题", listed)
        cancelled = await plugin.task_cancel_tool(event, task_id)
        self.assertIn("已撤销", cancelled)
        after = await plugin.task_list_tool(event, STATUS_CANCELLED, 10)
        self.assertIn(task_id[:8], after)

    async def test_bot_can_schedule_long_term_with_window(self):
        plugin, scope, _sent = await self._plugin()
        hooks = _hooks()
        event = hooks.FakeEvent(hooks._group_raw("排日程", 9502), "排日程",
                                hooks.FakeBot(), wake=True)
        out = await plugin.task_add_tool(
            event, task_kind=KIND_AGENT, detail="每天巡视群聊",
            interval_minutes=120, max_runs=-1, daily_window="09:00-22:00")
        self.assertIn("true", out.lower(), "长期任务要标记 long_term")
        task = (await plugin.task_queue.list(limit=3))[0]
        self.assertEqual(7200, task.interval_seconds)
        self.assertEqual(-1, task.max_runs)
        self.assertEqual(["09:00", "22:00"], task.window.get("daily"))

    async def test_tool_task_dispatches_through_channel(self):
        plugin, scope, sent = await self._plugin()
        await plugin.task_queue.add(
            KIND_TOOL, detail="发条消息", scope_id=scope, conversation="123",
            payload={"tool": "say_now", "args": {"text": "工具任务产物"}})
        await plugin.task_queue_runner.tick()
        await asyncio.sleep(0.4)
        task = (await plugin.task_queue.list(limit=3))[0]
        self.assertEqual(STATUS_DONE, task.status, f"工具任务应完成：{task.last_error}")

    async def test_webui_task_panel_and_actions(self):
        plugin, scope, _sent = await self._plugin()
        api = plugin._page_api()
        created = await api.write("task_add", {
            "group_id": "123", "kind": KIND_AGENT, "detail": "WebUI 排的任务",
            "delay_minutes": 5, "max_runs": 1})
        self.assertTrue(created.get("task_id"))
        panel = await api.read("tasks", {})
        titles = [row["title"] or row["detail"] for row in panel["tasks"]]
        self.assertTrue(any("WebUI 排的任务" in (title or "") for title in titles))
        self.assertIn("stats", panel)
        self.assertIn("kinds", panel)
        cancelled = await api.write("task_cancel", {"task_id": created["task_id"]})
        self.assertTrue(cancelled["ok"])


if __name__ == "__main__":
    unittest.main()
