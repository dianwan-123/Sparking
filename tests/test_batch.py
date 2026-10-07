# -*- coding: utf-8 -*-
"""攒批判定（batch judging）行为测试。

窗口计时用真实 asyncio.sleep，窗口设得很小（0.05s）保持测试速度。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import types
import time
import unittest
from pathlib import Path


def _load_hooks():
    sys_root = Path(__file__).resolve().parents[1]
    import sys

    if str(sys_root) not in sys.path:
        sys.path.insert(0, str(sys_root))
    import tests.test_plugin_hooks as hooks

    return hooks


class BatchJudgingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _config(self, hooks, **extra):
        config = hooks._plugin_config()
        config.update(extra)
        return config

    async def _make_plugin(self, hooks, responses=None, config=None):
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext(list(responses or [])), config
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _drain(self, plugin):
        for _ in range(200):
            if not plugin._tasks:
                await asyncio.sleep(0.01)
                if not plugin._tasks:
                    return
            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)

    async def test_window_flush_judges_batch_once(self):
        hooks = _load_hooks()
        config = self._config(
            hooks,
            batch_window_min_seconds=1,
            batch_window_max_seconds=1,
            batch_max_messages=10,
        )
        plugin = await self._make_plugin(hooks, [], config)
        bot = hooks.FakeBot()

        for index, text in enumerate(["你们觉得呢", "我选B", "A也还行"]):
            event = hooks.FakeEvent(hooks._group_raw(text, 20001 + index), text, bot)
            await plugin.on_onebot_event(event)

        # buffered, nothing judged yet: all 3 landed within the 1s window
        self.assertEqual(0, len(plugin.context.llm_calls))
        key = "123"
        self.assertEqual(3, len(plugin._batch_buffers[key]))
        plugin._batch_timers[key].cancel()  # skip the 1s wait
        plugin._flush_batch(key, "test")
        await self._drain(plugin)

        # one judgment for the whole batch of 3 messages, prompt carries the batch
        self.assertEqual(1, len(plugin.context.llm_calls))
        prompt = str(plugin.context.llm_calls[0].get("prompt", ""))
        self.assertIn("batch_transcript", prompt)
        self.assertIn("批次纪律", prompt, "批次纪律要进判定载荷")
        for text in ("你们觉得呢", "我选B", "A也还行"):
            self.assertIn(text, prompt)
        self.assertEqual([], plugin._batch_buffers.get(key, []))

    async def test_full_buffer_flushes_immediately(self):
        hooks = _load_hooks()
        config = self._config(
            hooks,
            batch_window_min_seconds=30,
            batch_window_max_seconds=30,
            batch_max_messages=2,
        )
        plugin = await self._make_plugin(hooks, [], config)
        bot = hooks.FakeBot()

        await plugin.on_onebot_event(
            hooks.FakeEvent(hooks._group_raw("第一条", 30001), "第一条", bot)
        )
        self.assertEqual(1, len(plugin._batch_buffers["123"]))
        # not judged yet: only one buffered message, window is 30s
        self.assertEqual(0, len(plugin.context.llm_calls))

        await plugin.on_onebot_event(
            hooks.FakeEvent(hooks._group_raw("第二条", 30002), "第二条", bot)
        )
        await self._drain(plugin)

        # buffer full (2/2) -> flush without waiting the 30s window
        self.assertEqual(1, len(plugin.context.llm_calls))
        self.assertEqual([], plugin._batch_buffers.get("123", []))

    async def test_wake_drops_pending_batch(self):
        hooks = _load_hooks()
        config = self._config(
            hooks,
            batch_window_min_seconds=30,
            batch_window_max_seconds=30,
            batch_max_messages=10,
        )
        plugin = await self._make_plugin(hooks, [], config)
        bot = hooks.FakeBot()

        await plugin.on_onebot_event(
            hooks.FakeEvent(hooks._group_raw("闲聊一句", 40001), "闲聊一句", bot)
        )
        self.assertEqual(1, len(plugin._batch_buffers["123"]))

        wake = hooks.FakeEvent(hooks._group_raw("在吗", 40002), "在吗", bot, wake=True)
        await plugin.on_onebot_event(wake)
        await self._drain(plugin)

        # the wake judgment covers the buffered message via recent context;
        # the pending batch is dropped instead of being judged a second time
        self.assertEqual([], plugin._batch_buffers.get("123", []))
        self.assertEqual(1, len(plugin.context.llm_calls))
        self.assertTrue(wake._stopped)

    async def test_timer_expires_flushes_automatically(self):
        hooks = _load_hooks()
        config = self._config(
            hooks,
            batch_window_min_seconds=1,
            batch_window_max_seconds=1,
            batch_max_messages=10,
        )
        plugin = await self._make_plugin(hooks, [], config)
        bot = hooks.FakeBot()

        await plugin.on_onebot_event(
            hooks.FakeEvent(hooks._group_raw("等等再看", 45001), "等等再看", bot)
        )
        self.assertEqual(1, len(plugin._batch_buffers["123"]))
        # run the timer coroutine with a tiny window instead of 1s
        plugin._batch_timers.pop("123").cancel()
        await plugin._batch_timer("123", 0.01)
        await self._drain(plugin)

        self.assertEqual(1, len(plugin.context.llm_calls))
        self.assertEqual([], plugin._batch_buffers.get("123", []))

    async def test_same_message_not_buffered_twice(self):
        hooks = _load_hooks()
        config = self._config(
            hooks,
            batch_window_min_seconds=0,
            batch_window_max_seconds=0,
            batch_max_messages=10,
        )
        plugin = await self._make_plugin(hooks, [], config)
        bot = hooks.FakeBot()

        raw = hooks._group_raw("重复消息", 50001)
        await plugin.on_onebot_event(hooks.FakeEvent(raw, "重复消息", bot))
        await plugin.on_onebot_event(hooks.FakeEvent(raw, "重复消息", bot))
        await self._drain(plugin)

        self.assertEqual(1, len(plugin.context.llm_calls))
        prompt = str(plugin.context.llm_calls[0].get("prompt", ""))
        # 同一条消息在批次转录里只能出现一次（重复事件不重复入批）
        # 转录行形如「昵称：内容」，用全角冒号前缀计数可避开 recent_messages 里的 JSON 文本
        self.assertEqual(1, prompt.count("：重复消息"))
        self.assertEqual(1, prompt.count('"batch_count":1'))

    async def test_batch_reply_still_respects_limits(self):
        hooks = _load_hooks()
        config = self._config(
            hooks,
            batch_window_min_seconds=1,
            batch_window_max_seconds=1,
            batch_max_messages=10,
        )
        plugin = await self._make_plugin(hooks, [], config)
        bot = hooks.FakeBot()

        await plugin.on_onebot_event(
            hooks.FakeEvent(hooks._group_raw("话题一", 60001), "话题一", bot)
        )
        # simulate QQ risk-control blockade on this scope
        scope = plugin._scope_for_event and None
        key = "123"
        for scope_id in plugin._known_scopes.values():
            plugin._send_blockade[scope_id] = time.monotonic() + 60
        plugin._flush_batch(key, "test")
        await self._drain(plugin)

        # blockade skips the batch judgment entirely
        self.assertEqual(0, len(plugin.context.llm_calls))


if __name__ == "__main__":
    unittest.main()


class BatchReplyShapeTests(unittest.IsolatedAsyncioTestCase):
    """实录："窗口内的一条一条详细回复"——攒批成立但回复仍逐条作答。

    三层修复：①批次改**聊天记录形态**（batch_transcript 逐行"昵称：内容"），
    不再给对象数组（数组本身诱导逐项作答）；②判定与回复两处提示词加【批次纪律】；
    ③攒批回复**段数上限 3**（程序兜底，真人扫一眼只会插一句）。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    @staticmethod
    def _config(hooks, **extra):
        config = hooks._plugin_config()
        config.update(extra)
        return config

    async def _plugin(self, responses=None):
        hooks = _load_hooks()
        config = self._config(hooks, batch_window_min_seconds=0,
                              batch_window_max_seconds=0)
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext(list(responses or [])), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
        plugin._known_scopes["123"] = scope
        return plugin, hooks, scope

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    def test_prompts_carry_batch_discipline(self):
        from src.prompts import DECISION_SYSTEM_PROMPT, REPLY_SYSTEM_PROMPT

        self.assertIn("批次纪律", DECISION_SYSTEM_PROMPT)
        self.assertIn("最多回一次", DECISION_SYSTEM_PROMPT)
        self.assertIn("批次纪律", REPLY_SYSTEM_PROMPT)
        self.assertIn("严禁逐条点评", REPLY_SYSTEM_PROMPT)

    async def test_batch_reply_segments_are_capped(self):
        """模型硬给 6 段逐条回复时，攒批场景只发前 3 段。"""
        import json as _json
        from src.models import Decision

        segments = [{"action": "text", "text": f"回复第{i}条"} for i in range(6)]
        plan_json = _json.dumps({"thought": "逐条", "mode": "sequence",
                                 "segments": segments}, ensure_ascii=False)
        plugin, hooks, scope = await self._plugin([plan_json])
        event = hooks.FakeEvent(hooks._group_raw("一批", 9301), "一批",
                                hooks.FakeBot(), wake=True)
        await plugin._execute_decision(
            event, scope, "9301", "{}", Decision(action="text"),
            types.SimpleNamespace(), False, None, batch_mode=True)
        sent_texts = []
        for chain in event.sent:
            for part in getattr(chain, "chain", []):
                text = part[1] if isinstance(part, tuple) else getattr(part, "text", "")
                if text:
                    sent_texts.append(str(text))
        self.assertEqual(3, len(sent_texts), f"攒批回复要收敛到 3 段，实际 {sent_texts}")

    async def test_non_batch_reply_is_not_capped(self):
        import json as _json
        from src.models import Decision

        segments = [{"action": "text", "text": f"正常第{i}条"} for i in range(5)]
        plan_json = _json.dumps({"thought": "正常", "mode": "sequence",
                                 "segments": segments}, ensure_ascii=False)
        plugin, hooks, scope = await self._plugin([plan_json])
        event = hooks.FakeEvent(hooks._group_raw("单独一条", 9302), "单独一条",
                                hooks.FakeBot(), wake=True)
        await plugin._execute_decision(
            event, scope, "9302", "{}", Decision(action="text"),
            types.SimpleNamespace(), False, None)
        sent_texts = []
        for chain in event.sent:
            for part in getattr(chain, "chain", []):
                text = part[1] if isinstance(part, tuple) else getattr(part, "text", "")
                if text:
                    sent_texts.append(str(text))
        self.assertGreaterEqual(len(sent_texts), 4, "非攒批场景不受 3 段上限影响")
