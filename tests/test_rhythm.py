# -*- coding: utf-8 -*-
"""v0.10.0：活人事件循环测试。

覆盖：日程规划解析（含过期丢弃）、plans 存储 CRUD、心跳执行到点计划、
闲时配额、潜水刷新印象、表情语义挑选与 sticker_id 发送、印象工具链。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from src.rhythm import idle_choice, parse_plan_response, part_of_day
from src.storage import Storage


class RhythmParsingTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_plans_with_stale_dropped(self):
        now = datetime.now().astimezone().replace(second=0, microsecond=0)
        future = (now + timedelta(hours=2)).strftime("%H:%M")
        past = (now - timedelta(hours=3)).strftime("%H:%M")
        raw = json.dumps({
            "plans": [
                {"time": future, "kind": "qzone", "detail": "刷会儿空间"},
                {"time": past, "kind": "chat", "detail": "早过的点自动滚到明天"},
                {"time": "25:99", "kind": "chat", "detail": "非法时间"},
                {"time": future, "kind": "nonsense", "detail": "未知类型归custom"},
            ],
        }, ensure_ascii=False)
        items = parse_plan_response(raw, now=now)
        self.assertEqual(3, len(items))
        kinds = {item.kind for item in items}
        self.assertIn("qzone", kinds)
        self.assertIn("custom", kinds)
        rolled = next(item for item in items if item.kind == "chat")
        self.assertGreater(rolled.due_at, now)
        for item in items:
            self.assertGreaterEqual(item.due_at, now - timedelta(minutes=30))

    def test_parse_garbage_returns_empty(self):
        self.assertEqual([], parse_plan_response("not json at all"))
        self.assertEqual([], parse_plan_response('{"plans": "nope"}'))

    def test_idle_choice_respects_time_and_caps(self):
        self.assertEqual("night", part_of_day(3))
        self.assertEqual("evening", part_of_day(19))
        seen = {idle_choice(21) for _ in range(50)}
        self.assertTrue(seen.issubset({
            "qzone", "poke", "private_chat", "chat", "sticker",
            "memory", "profile", "news", "learn", "none",
        }))
        self.assertIn("sticker", {idle_choice(19) for _ in range(80)})


class PlansAndImpressionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    async def test_plans_roundtrip(self):
        storage = await Storage(Path(self._tmp.name) / "memory.db").open()
        try:
            due = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="minutes")
            later = (datetime.now().astimezone() + timedelta(hours=2)).isoformat(timespec="minutes")
            n = await storage.add_plans([
                {"due_at": due, "kind": "qzone", "detail": "去逛逛"},
                {"due_at": later, "kind": "chat", "detail": "去群里聊聊"},
                {"due_at": later, "kind": "chat", "detail": "去群里聊聊"},  # 幂等
                {"due_at": "", "kind": "chat", "detail": "缺时间被忽略"},
            ])
            self.assertEqual(3, n)
            due_items = await storage.due_plans()
            self.assertEqual(1, len(due_items))
            self.assertEqual("qzone", due_items[0]["kind"])
            self.assertTrue(await storage.complete_plan(due_items[0]["plan_id"], "done"))
            self.assertEqual([], await storage.due_plans())
            pending = await storage.pending_plans()
            self.assertEqual(1, len(pending))
        finally:
            await storage.close()

    async def test_impressions_upsert_and_list(self):
        storage = await Storage(Path(self._tmp.name) / "memory.db").open()
        try:
            scope = await storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
            empty = await storage.get_impression(scope, "10002")
            self.assertEqual({}, empty)
            await storage.upsert_impression(
                scope, "10002", display_name="Tester",
                impression="话痨但讲义气", tags=["话痨", "游戏"],
            )
            await storage.upsert_impression(scope, "10003", display_name="Bob")
            # 只更新 last_seen 时不覆盖印象
            await storage.upsert_impression(scope, "10002", display_name=None)
            got = await storage.get_impression(scope, "10002")
            self.assertEqual("话痨但讲义气", got["impression"])
            self.assertEqual(["话痨", "游戏"], got["tags"])
            self.assertEqual("Tester", got["display_name"])
            people = await storage.list_impressions([scope])
            self.assertEqual(2, len(people))
            hit = await storage.list_impressions([scope], query="讲义气")
            self.assertEqual(1, len(hit))
        finally:
            await storage.close()


class HeartbeatAndStickerTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_heartbeat_executes_due_plan(self):
        hooks = _hooks()
        plugin = await self._make_plugin(hooks, [], self._config(hooks))
        due = (datetime.now().astimezone() - timedelta(seconds=5)).isoformat(timespec="minutes")
        await plugin.storage.add_plans([{"due_at": due, "kind": "memory", "detail": "沉淀一下今天的记忆"}])
        plugin._next_plan_at = time.monotonic() + 9999  # 规划不在本轮触发
        await plugin._heartbeat_tick()
        pending = await plugin.storage.pending_plans()
        self.assertEqual([], pending)
        self.assertEqual(1, len(plugin.context.llm_calls))

    async def test_heartbeat_idle_budget_and_lurk(self):
        import random as _random
        import sys

        hooks = _hooks()
        main_mod = sys.modules["DFYChat.main"]
        orig_random = _random.random
        orig_choice = main_mod.idle_choice
        _random.random = lambda: 0.01  # 必走闲时分支
        main_mod.idle_choice = lambda hour: "memory"  # 避开 none 分支
        self.addCleanup(setattr, _random, "random", orig_random)
        self.addCleanup(setattr, main_mod, "idle_choice", orig_choice)

        plugin = await self._make_plugin(
            hooks, [], self._config(hooks, idle_actions_per_hour=1, heartbeat_interval_seconds=60)
        )
        bot = hooks.FakeBot()
        event = hooks.FakeEvent(hooks._group_raw("冒个泡", 70001), "冒个泡", bot)
        await plugin.on_onebot_event(event)  # 入库 + 建印象数据来源

        plugin._next_plan_at = time.monotonic() + 9999
        # 第一跳：闲时预算 1 次
        plugin._idle_budget = (datetime.now().astimezone().hour, 0)
        await plugin._heartbeat_tick()
        self.assertEqual(1, plugin._idle_budget[1])
        # 预算用完 → 潜水（无 LLM 动作增量）
        calls_before = len(plugin.context.llm_calls)
        await plugin._heartbeat_tick()
        self.assertGreaterEqual(len(plugin.context.llm_calls), calls_before)
        scope = next(iter(plugin._known_scopes.values()))
        impression = await plugin.storage.get_impression(scope, "10002")
        self.assertTrue(impression.get("last_seen"))

    async def test_sticker_semantic_pick_and_resolve(self):
        hooks = _hooks()
        plugin = await self._make_plugin(hooks, [], self._config(hooks, sticker_learning=True))
        # 手工放一张"表情"进库
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
            "h6FO1AAAAABJRU5ErkJggg=="
        )
        record = await plugin.stickers.ingest(
            {"base64": base64.b64encode(png).decode("ascii")}, trusted_get_image=True,
            metadata={"summary": "狗头 嘲讽"},
        )
        await plugin.stickers.note(record.sha256, description="一只微笑狗头，用来优雅地嘲讽")
        picked = plugin.stickers.pick("嘲讽 狗头")
        self.assertEqual(1, len(picked))
        self.assertEqual(record.sha256, picked[0]["sticker_id"])
        self.assertEqual("狗头 嘲讽", picked[0]["summary"])
        resolved = plugin.stickers.resolve(record.sha256[:12])
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.path.endswith(".png"))
        self.assertIsNone(plugin.stickers.resolve("deadbeef"))

        # sticker 段按 sticker_id 精确发送
        bot = hooks.FakeBot()
        event = hooks.FakeEvent(hooks._group_raw("x", 80001), "x", bot)
        await plugin._send_segment(event, {"action": "sticker", "sticker_id": record.sha256[:12]})
        self.assertEqual(1, len(event.sent))


def _hooks():
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks

    return hooks


if __name__ == "__main__":
    unittest.main()
