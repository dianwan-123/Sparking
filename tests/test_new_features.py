from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from src.emotions import MoodStore
from src.humanization import (
    HumanizationConfig,
    HumanizedSender,
    PlanValidationError,
    analyze_recent_style,
    conservative_split_text,
    humanize_plan,
    parse_message_plan,
)
from src.media_archive import MediaArchive, extract_urls, is_blocked_host
from src.qq_actions import ACTIONS
from src.qq_gateway import GatewayFacade, QQGateway, QQGatewayError
from src.qq_services import QQService, QzoneService
from src.scheduler import TaskScheduler


class _Bot:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action == "get_cookies":
            return {"cookies": "p_skey=abc123; uin=o12345"}
        if action == "get_login_info":
            return {"user_id": 12345}
        if action == "fail_action":
            return {"status": "failed", "retcode": 100, "data": None}
        return {"status": "ok", "retcode": 0, "data": None}


class QQGatawayTests(unittest.IsolatedAsyncioTestCase):
    """v0.38.0：QQ 工具层完全重写（catalog 驱动，193 动作全量）。"""

    async def test_catalog_covers_reference_actions(self):
        expected = {
            "send_msg", "send_private_msg", "send_group_msg", "send_forward_msg",
            "send_private_forward_msg", "send_group_forward_msg", "delete_msg",
            "get_group_msg_history", "get_friend_msg_history", "get_forward_msg",
            "get_group_member_info", "get_group_member_list", "get_friend_list",
            "get_group_list", "get_stranger_info", "get_login_info", "get_cookies",
            "get_status", "send_poke", "group_poke", "friend_poke",
            "set_msg_emoji_like", "set_group_ban", "set_group_leave", "delete_friend",
            "set_friend_add_request", "set_group_add_request", "set_group_card",
            "set_group_name", "set_group_special_title", "set_qq_avatar",
            "set_self_longnick", "set_online_status", "set_qq_profile",
            "forward_friend_single_msg", "forward_group_single_msg", "get_file",
            # SnowLuma 独有：闪传/群相册/收藏/AI 语音/精华/已读/凭证
            "get_qun_album_list", "upload_image_to_qun_album", "delete_flash_file",
            "send_group_ai_record", "get_recent_contact", "send_like",
        }
        self.assertTrue(expected.issubset(set(ACTIONS)), expected - set(ACTIONS))

    async def test_catalog_has_full_193_actions(self):
        self.assertEqual(193, len(ACTIONS))
        categories = {a.category for a in ACTIONS.values()}
        for name in ("信息", "消息", "好友", "群信息", "群管理", "群文件",
                     "请求", "扩展", "群相册", "空间", "系统表情", "流式接口"):
            self.assertIn(name, categories)

    async def test_execute_validates_required_params(self):
        gateway = QQGateway(_Bot(), hourly={"send": 100})
        # 必填缺失 → 明确报缺哪个，而不是含糊失败
        with self.assertRaises(QQGatewayError) as ctx:
            await gateway.execute("send_group_msg", group_id=1)
        self.assertIn("message", str(ctx.exception))
        # 未注册动作 → 报未注册（可给相近动作提示）
        with self.assertRaises(QQGatewayError):
            await gateway.execute("made_up_action")
            await gateway.execute("get_group_inf")

    async def test_type_coercion_for_llm_inputs(self):
        bot = _Bot()
        gateway = QQGateway(bot, hourly={"read": 50})
        await gateway.execute("get_group_info", group_id="123")
        self.assertEqual(123, bot.calls[-1][1]["group_id"], "字符串数字要转 int")

    async def test_facade_blocks_credentials(self):
        facade = GatewayFacade(QQGateway(_Bot()))
        with self.assertRaises(QQGatewayError):
            await facade.call("get_cookies", domain="x")
        result = await facade.call("get_status")
        self.assertEqual(result["status"], "ok")

    async def test_hourly_budget_and_min_interval(self):
        gateway = QQGateway(_Bot(), hourly={"send": 2})
        await gateway.execute("send_group_msg", group_id=1, message=[])
        await gateway.execute("send_group_msg", group_id=1, message=[])
        with self.assertRaises(QQGatewayError):
            await gateway.execute("send_group_msg", group_id=1, message=[])

    async def test_sequence_with_cancel(self):
        bot = _Bot()
        gateway = QQGateway(bot)
        actions = [("send_group_msg", {"group_id": 1, "message": []})] * 3
        await gateway.run_sequence(actions, between_delay=0)
        self.assertEqual(3, len(bot.calls))
        cancelled = await gateway.run_sequence(
            actions, between_delay=0, is_cancelled=lambda: True)
        self.assertEqual([], cancelled)

    async def test_catalog_listing_and_describe(self):
        gateway = QQGateway(_Bot())
        items = gateway.catalog("消息")
        names = {item["action"] for item in items}
        self.assertIn("send_group_msg", names)
        self.assertTrue(all(item["params"] is not None for item in items))
        # 凭证类不出现在 LLM 目录里
        every = {item["action"] for item in gateway.catalog()}
        self.assertNotIn("get_cookies", every)
        text = gateway.spec("get_forward_msg")
        self.assertIsNotNone(text)
        self.assertEqual("get_forward_msg", text.name)

    async def test_messages_of_unwraps_both_shapes(self):
        from src.qq_gateway import messages_of

        self.assertEqual([{"a": 1}], messages_of({"messages": [{"a": 1}]}))
        self.assertEqual([{"a": 1}], messages_of(
            {"status": "ok", "retcode": 0, "data": {"messages": [{"a": 1}]}}))
        self.assertEqual([], messages_of({}))


class HumanizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_strict_single_and_sequence(self):
        plan = parse_message_plan('{"mode": "single", "message": {"action": "text", "text": "hi"}}')
        self.assertEqual(1, len(plan.segments))
        plan = parse_message_plan(
            '{"mode": "sequence", "segments": ['
            '{"action": "text", "text": "a"}, {"action": "face", "face_id": 14}]}')
        self.assertEqual(2, len(plan.segments))
        with self.assertRaises(PlanValidationError):
            parse_message_plan('{"mode": "sequence", "segments": [{"action": "bomb"}]}')
        with self.assertRaises(PlanValidationError):
            parse_message_plan('not json')

    async def test_split_protects_urls_and_code(self):
        text = "看这个 https://example.com/a?b=1&c=2 非常有意思。真的。"
        parts = conservative_split_text(text, max_segments=3, min_chars=4, target_chars=10)
        self.assertTrue(any("https://example.com/a?b=1&c=2" in part for part in parts))
        code = "```\ndef a():\n    pass\n```\n后面的话。"
        parts = conservative_split_text(code, max_segments=3, min_chars=4, target_chars=8)
        self.assertTrue(any("def a():" in part for part in parts))

    async def test_sender_cancels_and_counts(self):
        sent = []
        config = HumanizationConfig(max_sequences_per_hour=100)

        async def send(descriptor):
            sent.append(descriptor)
            if descriptor.get("text") == "two":
                raise RuntimeError("boom")

        sender = HumanizedSender(send, config=config)
        plan = parse_message_plan(
            '{"mode": "sequence", "segments": ['
            '{"action": "text", "text": "one"}, {"action": "text", "text": "two"}, '
            '{"action": "text", "text": "three"}]}', config)
        plan = humanize_plan(plan, config)
        result = await sender.send(plan)
        self.assertEqual("failed", result.status)
        self.assertEqual(1, result.sent_count)
        result = await sender.send(
            parse_message_plan('{"mode": "sequence", "segments": ['
                               '{"action": "text", "text": "a"}, {"action": "text", "text": "b"}]}', config),
            is_cancelled=lambda: True)
        self.assertEqual("cancelled", result.status)
        self.assertEqual(0, result.sent_count)

    async def test_style_analysis(self):
        style = analyze_recent_style([
            {"text": "哈哈哈哈太搞笑了", "occurred_at": "2026-09-12T10:00:00"},
            {"text": "真的假的？", "occurred_at": "2026-09-12T10:00:40"},
        ])
        self.assertEqual(2, style.sample_size)
        self.assertTrue(style.suggested_segment_chars > 0)


class MediaArchiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    async def test_save_dedupe_limit_and_recent(self):
        archive = await MediaArchive(self._tmp.name, max_file_bytes=1024).open()
        try:
            png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
            first = await archive.save_bytes(png, scope_id="s1", kind="image", mime="image/png")
            self.assertEqual("saved", first.status)
            duplicate = await archive.save_bytes(png, scope_id="s1", kind="image", mime="image/png")
            self.assertEqual("duplicate", duplicate.status)
            skipped = await archive.save_bytes(b"x" * 2048, scope_id="s1", kind="file")
            self.assertEqual("skipped", skipped.status)
            self.assertEqual(1, len([x for x in await archive.recent("s1") if x["status"] == "saved"]))
            path = await archive.get_path(first.item_id)
            self.assertTrue(Path(path).is_file())
        finally:
            await archive.close()

    async def test_url_record_and_blocked_host(self):
        archive = await MediaArchive(self._tmp.name).open()
        try:
            record = await archive.record_url("https://example.com/x", scope_id="s1")
            self.assertEqual("pending", record.status)
            blocked = await archive.record_url("http://127.0.0.1:6185/x", scope_id="s1")
            self.assertEqual("blocked", blocked.status)
            self.assertTrue(is_blocked_host("192.168.1.5"))
            self.assertFalse(is_blocked_host("example.com"))
            self.assertEqual(["https://a.b/c"], extract_urls("看 https://a.b/c 好东西"))
        finally:
            await archive.close()

    async def test_ingest_url_with_fake_fetcher(self):
        async def fake_stream(url, max_bytes):
            yield b"\x89PNG\r\n\x1a\n" + b"data" * 10, None

        archive = await MediaArchive(self._tmp.name, fetch_stream=fake_stream).open()
        try:
            record = await archive.ingest_url("https://example.com/pic.png", scope_id="s1")
            self.assertEqual("saved", record.status)
            self.assertTrue(record.path.endswith(".png"))
        finally:
            await archive.close()

    async def test_fetch_bounded_happy_and_blocked(self):
        from src.media_archive import MediaArchiveError, fetch_bounded

        async def fake_stream(url, max_bytes):
            yield b"\x89PNG\r\n\x1a\n", None
            yield b"rest", None

        data = await fetch_bounded(
            "https://example.com/pic.png", 1024, fetcher=fake_stream)
        self.assertTrue(data.startswith(b"\x89PNG"))
        with self.assertRaises(MediaArchiveError):
            await fetch_bounded("http://127.0.0.1/x", 1024, fetcher=fake_stream)
        with self.assertRaises(MediaArchiveError):
            await fetch_bounded("ftp://example.com/x", 1024, fetcher=fake_stream)


class QQServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_social_actions_route_through_gateway(self):
        bot = _Bot()
        gateway = QQGateway(bot, hourly={"send": 100, "social": 100, "destructive": 100})
        service = QQService(gateway)
        await service.friend_list()
        await service.group_members(456)
        await service.poke(789, 456)
        await service.send_group(456, [{"type": "text", "data": {"text": "hi"}}])
        actions = [call[0] for call in bot.calls]
        self.assertIn("get_friend_list", actions)
        self.assertIn("get_group_member_list", actions)
        self.assertIn("group_poke", actions)
        self.assertIn("send_group_msg", actions)

    async def test_qzone_session_and_publish(self):
        bot = _Bot()
        gateway = QQGateway(bot, hourly={"credential": 100, "send": 100})
        posted = []
        fetched = []

        async def fake_poster(path, cookie, data):
            posted.append((path, data))
            return {"tid": "t1"}

        async def fake_getter(path, cookie, params):
            fetched.append((path, params))
            return {"msglist": []}

        qzone = QzoneService(gateway, poster=fake_poster, getter=fake_getter, ttl_seconds=3600)
        result = await qzone.publish("今天天气不错")
        # native NapCat action used for publish
        native = [call for call in bot.calls if call[0] == "send_qzone_msg"]
        self.assertEqual(1, len(native))
        self.assertEqual("今天天气不错", native[0][1]["content"])
        self.assertEqual("napcat", result["via"])
        # list falls back to HTTP GET because the fake bot returns no data
        feeds = await qzone.list_feeds(1)
        self.assertEqual({"msglist": []}, feeds)
        self.assertEqual(1, len(fetched))
        self.assertIn("msglist_v6", fetched[0][0])
        self.assertIn("g_tk", fetched[0][1])


class SchedulerMoodTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    async def test_task_lifecycle(self):
        path = Path(self._tmp.name) / "tasks.json"
        scheduler = TaskScheduler(path)
        task = await scheduler.add("新闻", "看看新闻", group_id="123", delay_minutes=0)
        later = await scheduler.add("晚报", "看看晚报", group_id="123", delay_minutes=30)
        due = await scheduler.due()
        self.assertEqual([task.task_id], [t.task_id for t in due])
        # one-shot tasks disappear after running; the delayed one stays
        self.assertEqual([], await scheduler.due())
        self.assertTrue(await scheduler.cancel(later.task_id))
        reloaded = TaskScheduler(path)
        self.assertEqual([], reloaded.list())

    async def test_daily_task_reschedules(self):
        scheduler = TaskScheduler(Path(self._tmp.name) / "t2.json")
        await scheduler.add("早报", "发早报", group_id="1", daily_hour=9)
        due = await scheduler.due(now=scheduler.list()[0]["next_run"] + 1)
        self.assertEqual(1, len(due))
        again = await scheduler.due()
        self.assertEqual([], again)

    async def test_mood_store(self):
        store = MoodStore(Path(self._tmp.name) / "mood.json")
        state = store.set("开心", 0.8, "群里在聊有趣的事")
        self.assertEqual("开心", state.mood)
        reloaded = MoodStore(Path(self._tmp.name) / "mood.json")
        self.assertEqual("开心", reloaded.get().mood)
        with self.assertRaises(ValueError):
            store.set("", 0.5)


class DecisionSegmentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_segments_pass_validation(self):
        from src.decision import DecisionEngine

        async def judge(prompt):
            return json.dumps({
                "action": "text", "intent": "聊天",
                "segments": [{"action": "text", "text": "哈哈"}, {"action": "reaction", "emoji_id": "76"}],
            }, ensure_ascii=False)

        engine = DecisionEngine(judge, allowed_emoji_ids={"76"})
        decision = await engine.decide("ctx")
        self.assertEqual(2, len(decision.segments or []))

    async def test_segments_rejected_over_limit(self):
        from src.decision import DecisionEngine, DecisionValidationError

        async def judge(prompt):
            return json.dumps({
                "action": "text",
                "segments": [{"action": "text", "text": "x"}] * 6,
            })

        engine = DecisionEngine(judge)
        with self.assertRaises(DecisionValidationError):
            engine.parse(await judge("x"))


if __name__ == "__main__":
    unittest.main()
