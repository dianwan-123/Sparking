# -*- coding: utf-8 -*-
"""v0.36.1 回归：唤醒消息合并窗口。

实录：用户连发三条 @，bot 机械地回了三次。真人会一起读完、回一句。
修法：@/私聊消息先进合并窗口（防抖：每条新消息把开口时机往后推；上限：从
第一条起最多等 wake_merge_max_seconds），窗口静默后把整批合并成一次判定
一次回复（batch_items 走既有攒批管线）。wake_merge_seconds=0 退回逐条即回。

注：窗口计时是真实 asyncio 定时器，测试一律用"大窗口 + 手动冲刷/改时间戳"
驱动，避免真实 IO 抖动造成 flaky。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class WakeMergeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _config(self, **extra):
        config = _hooks()._plugin_config()
        config.update(extra)
        return config

    async def _make_plugin(self, responses=None, config=None):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext(list(responses or [])), config or self._config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin, hooks

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    @staticmethod
    def _record_flow(plugin):
        recorded: list[dict] = []

        async def fake_flow(event, scope_id, message_id, image_only=False,
                            wake=False, image_urls=None, batch_items=None):
            recorded.append({
                "wake": wake, "batch": list(batch_items or []),
                "message_id": message_id,
            })

        plugin._handle_reply_flow = fake_flow
        return recorded

    async def _drain(self, plugin):
        for _ in range(200):
            if not plugin._tasks:
                await asyncio.sleep(0.01)
                if not plugin._tasks:
                    return
            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)

    async def _feed_wake(self, plugin, hooks, text, message_id):
        event = hooks.FakeEvent(hooks._group_raw(text, message_id), text,
                                hooks.FakeBot(), wake=True)
        await plugin.on_onebot_event(event)
        return event

    async def test_three_ats_merge_into_one_flow(self):
        plugin, hooks = await self._make_plugin(config=self._config(
            wake_merge_seconds=30.0, wake_merge_max_seconds=60.0))
        recorded = self._record_flow(plugin)
        events = []
        for index, text in enumerate(["在吗", "帮我看看服务器", "画个图"]):
            events.append(await self._feed_wake(plugin, hooks, text, 30001 + index))
        # 窗口内不判定、不回复，三条全部挂在同一缓冲里
        self.assertEqual([], plugin.context.llm_calls)
        self.assertEqual(3, len(plugin._wake_buffers["123"]))
        for event in events:
            self.assertTrue(event._stopped, "到达即拦默认管线，窗口期不许抢答")
        plugin._flush_wake("123", "测试手动")
        await self._drain(plugin)
        self.assertEqual(1, len(recorded), "三连@合并成一次回复")
        self.assertTrue(recorded[0]["wake"])
        self.assertEqual(3, len(recorded[0]["batch"]))
        self.assertEqual("画个图", recorded[0]["batch"][-1].text)
        self.assertEqual({}, plugin._wake_buffers, "冲刷后缓冲清空")

    async def test_debounce_replaces_timer(self):
        plugin, hooks = await self._make_plugin(config=self._config(
            wake_merge_seconds=30.0, wake_merge_max_seconds=60.0))
        recorded = self._record_flow(plugin)
        await self._feed_wake(plugin, hooks, "第一条", 30011)
        first_timer = plugin._wake_timers["123"]
        self.assertFalse(first_timer.done())
        await self._feed_wake(plugin, hooks, "第二条", 30012)
        second_timer = plugin._wake_timers["123"]
        self.assertIsNot(second_timer, first_timer, "防抖：新消息要重开窗口定时器")
        await asyncio.sleep(0.05)
        self.assertTrue(first_timer.done(),
                        "旧定时器提前结束（30s 的 sleep 在 50ms 内结束=被取消）")
        self.assertEqual([], recorded, "旧定时器结束时不触发冲刷")
        self.assertEqual(2, len(plugin._wake_buffers["123"]))

    async def test_merge_cap_flushes_on_next_message(self):
        plugin, hooks = await self._make_plugin(config=self._config(
            wake_merge_seconds=30.0, wake_merge_max_seconds=1.0))
        recorded = self._record_flow(plugin)
        await self._feed_wake(plugin, hooks, "一直说", 30021)
        # 把第一条的到达时间拨回 2 秒前，模拟"上限已到"
        plugin._wake_first_at["123"] = time.monotonic() - 2.0
        await self._feed_wake(plugin, hooks, "还在说", 30022)
        await asyncio.sleep(0.05)
        self.assertEqual(1, len(recorded), "到达合并上限立即开口，不再等窗口")
        self.assertEqual(2, len(recorded[0]["batch"]))

    async def test_batch_full_flushes_immediately(self):
        plugin, hooks = await self._make_plugin(config=self._config(
            wake_merge_seconds=30.0, batch_max_messages=2))
        recorded = self._record_flow(plugin)
        await self._feed_wake(plugin, hooks, "刷屏0", 30031)
        await self._feed_wake(plugin, hooks, "刷屏1", 30032)
        await asyncio.sleep(0.05)
        self.assertEqual(1, len(recorded), "攒满立即统一回复")
        self.assertEqual(2, len(recorded[0]["batch"]))

    async def test_pending_nonwake_batch_merges_into_wake(self):
        plugin, hooks = await self._make_plugin(config=self._config(
            autonomous_sample_rate=1.0,
            batch_window_min_seconds=30, batch_window_max_seconds=30,
            wake_merge_seconds=30.0))
        recorded = self._record_flow(plugin)
        plain = hooks.FakeEvent(hooks._group_raw("随便聊聊", 30041), "随便聊聊",
                                hooks.FakeBot(), wake=False)
        await plugin.on_onebot_event(plain)
        self.assertEqual(1, len(plugin._batch_buffers.get("123", [])),
                         "非唤醒消息先进攒批")
        await self._feed_wake(plugin, hooks, "@bot 帮我总结下", 30042)
        self.assertEqual({}, plugin._batch_buffers, "攒批被并入唤醒缓冲")
        self.assertEqual(2, len(plugin._wake_buffers["123"]))
        plugin._flush_wake("123", "测试手动")
        await self._drain(plugin)
        self.assertEqual(1, len(recorded))
        self.assertEqual(2, len(recorded[0]["batch"]),
                         "攒批里的消息并入唤醒合并，一次回复")

    async def test_disabled_keeps_old_immediate_path(self):
        plugin, hooks = await self._make_plugin(config=self._config(
            wake_merge_seconds=0.0,
            judge_provider_id="fake-provider"))
        plugin.context.responses = [
            json.dumps({"action": "text", "intent": "应答"}),
            '{"thought": "好", "mode": "single", "message": {"action": "text", "text": "在的"}}',
        ]
        await self._feed_wake(plugin, hooks, "在吗", 30051)
        self.assertEqual({}, plugin._wake_buffers, "关闭合并时不进缓冲")
        self.assertGreaterEqual(len(plugin.context.llm_calls), 1,
                                "逐条即回的老路径仍在")


if __name__ == "__main__":
    unittest.main()
