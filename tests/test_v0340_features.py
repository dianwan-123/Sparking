# -*- coding: utf-8 -*-
"""v0.34.0 回归：计划 JSON 泄漏拦截、媒体段（image/file/record/video）与本地直发、
工作区文件增删改查+脚本运行、SnowLuma 通知文本覆盖。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"workspace-png"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.humanization import (  # noqa: E402
    PlanValidationError,
    is_plan_json_leak,
    parse_message_plan,
    salvage_message_plan,
)
from src.onebot import normalize_notice_event  # noqa: E402
from src.workspace import Workspace, WorkspaceError  # noqa: E402


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class PlanJsonLeakTests(unittest.TestCase):
    LEAK = ('{"thought":"反问好感度梗 疲惫看戏语气吐槽 配无奈表情","mode":"sequence",'
            '"segments":[{"action":"text","text":"反客为主是吧（",'
            '"reply_to_message_id":"269818f4a5214f49aa0ba0d23bac20de"}]}')

    def test_detects_thought_prefixed_plan(self):
        # 实录：旧守卫只查 {"mode"/{"action"，`{"thought":…}` 整包漏网被发进群
        self.assertTrue(is_plan_json_leak(self.LEAK))
        self.assertTrue(is_plan_json_leak('{"mode": "single", "message": {}}'))
        self.assertTrue(is_plan_json_leak(
            '```json\n{"thought":"x","mode":"sequence","segments":[]}\n```'))

    def test_normal_text_is_not_flagged(self):
        for text in ("{加个栗子}", "这波啊 这波是肉蛋葱鸡", "awa {  }",
                     "【图】{1,2,3}"):
            self.assertFalse(is_plan_json_leak(text), text)

    def test_send_segment_intercepts_leak(self):
        hooks = _hooks()
        tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = tmp.name
        self.addCleanup(tmp.cleanup)
        import types as _types

        plugin = hooks.LongMemoryAgentPlugin.__new__(hooks.LongMemoryAgentPlugin)
        plugin.settings = _types.SimpleNamespace(text_to_image_threshold=0)
        event = hooks.FakeEvent(
            hooks._group_raw("x", 4701), "x", hooks.FakeBot(), wake=True)
        event.sent = []

        import asyncio

        async def run():
            await plugin._send_segment(event, {"action": "text", "text": self.LEAK})
            self.assertEqual([], event.sent, "整包计划JSON必须被拦截")
            await plugin._send_segment(event, {"action": "text", "text": "正常一句话"})
            self.assertEqual(1, len(event.sent), "正常文本照常发送")

        asyncio.run(run())

    def test_salvage_drops_unparseable_plan_json(self):
        # 坏的整包 JSON：无从抢救必须报错，绝不能原样当文本发出去
        with self.assertRaises(PlanValidationError):
            salvage_message_plan('{"thought": "x", "mode": "sequence", "segments": [')


class MediaSegmentParseTests(unittest.TestCase):
    def test_media_segments_parse_with_source(self):
        plan = parse_message_plan(json.dumps({
            "mode": "sequence",
            "segments": [
                {"action": "text", "text": "喏"},
                {"action": "image", "media_id": "mid-1"},
                {"action": "file", "path": "docs/报告.pdf", "file_name": "报告.pdf"},
                {"action": "record", "media_id": "voice-1"},
                {"action": "video", "path": "clips/demo.mp4"},
            ],
            "thought": "发东西",
        }))
        actions = [seg.action for seg in plan.segments]
        self.assertEqual(["text", "image", "file", "record", "video"], actions)
        descriptor = plan.segments[2].to_descriptor()
        self.assertEqual("docs/报告.pdf", descriptor["path"])
        self.assertEqual("报告.pdf", descriptor["file_name"])

    def test_media_segment_without_source_rejected(self):
        for action in ("image", "file", "record", "video"):
            with self.assertRaises(PlanValidationError):
                parse_message_plan(json.dumps({
                    "mode": "single",
                    "message": {"action": action},
                }))

    def test_plain_text_cannot_carry_media_fields(self):
        with self.assertRaises(PlanValidationError):
            parse_message_plan(json.dumps({
                "mode": "single",
                "message": {"action": "text", "text": "hi", "media_id": "x"},
            }))


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.ws = Workspace([base / "data", base / "data" / "plugin_data" / "app" / "workspace"])

    def test_crud_and_search(self):
        saved = self.ws.write("notes/a.txt", "hello 工作区")
        self.assertTrue(Path(saved["path"]).is_file())
        read = self.ws.read("notes/a.txt")
        self.assertIn("工作区", read["text"])
        listing = self.ws.list("notes")
        self.assertEqual(["a.txt"], [e["name"] for e in listing["entries"]])
        found = self.ws.search("**/*.txt", root=".")
        self.assertEqual(1, len(found["matches"]))
        moved = self.ws.move("notes/a.txt", "notes/b.txt")
        self.assertTrue(Path(moved["to"]).is_file())
        removed = self.ws.delete("notes/b.txt")
        self.assertEqual(1, removed["removed"])

    def test_escape_is_rejected(self):
        external = Path(self._tmp.name).parent / "outside.txt"
        with self.assertRaises(WorkspaceError):
            self.ws.write(str(external), "nope")
        with self.assertRaises(WorkspaceError):
            self.ws.read(str(external))
        with self.assertRaises(WorkspaceError):
            self.ws.resolve("C:/Windows/System32/drivers/etc/hosts")
        # 注意：根是 AstrBot 数据目录，workspace 是其子集——workspace 里的
        # "../" 只要还在数据目录内就算合法（各插件数据目录都允许访问）

    async def test_run_script_in_workspace(self):
        import sys as _sys

        self.ws.write(
            "tools/hello.py",
            "import sys\nprint('MARK', sys.argv[1] if len(sys.argv) > 1 else '')\n",
        )
        result = await self.ws.run_script("tools/hello.py", args="参数A", timeout=30)
        self.assertEqual(0, result["exit_code"], result)
        self.assertIn("MARK", result["stdout"])
        self.assertEqual(_sys.executable, result["command"])
        with self.assertRaises(WorkspaceError):
            await self.ws.run_script("../../evil.py")

    async def test_unsupported_script_type(self):
        self.ws.write("x.rb", "puts 1")
        with self.assertRaises(WorkspaceError):
            await self.ws.run_script("x.rb")


class NoticeCoverageTests(unittest.TestCase):
    def _raw(self, notice_type, **extra):
        raw = {"post_type": "notice", "notice_type": notice_type, "time": 1791000000,
               "self_id": 1, "user_id": 42}
        raw.update(extra)
        return raw

    def test_admin_card_and_notify(self):
        admin = normalize_notice_event(self._raw(
            "group_admin", group_id=999, sub_type="set"))
        self.assertIn("管理员", admin.text)
        card = normalize_notice_event(self._raw(
            "group_card", group_id=999, card_new="新名", card_old="旧名"))
        self.assertIn("新名", card.text)
        honor = normalize_notice_event(self._raw(
            "notify", group_id=999, sub_type="honor", honor_type="talkative"))
        self.assertIn("talkative", honor.text)

    def test_unknown_notice_carries_details(self):
        unknown = normalize_notice_event(self._raw(
            "group_todo", group_id=999, msg_id="abc123", action="update"))
        self.assertIsNotNone(unknown)
        self.assertIn("group_todo", unknown.text)
        self.assertIn("abc123", unknown.text, "未知事件也要带上有意义字段")


class MediaKindAndSendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _make_plugin(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin

    def test_kind_by_extension(self):
        cls = _hooks().LongMemoryAgentPlugin
        self.assertEqual("image", cls._media_kind_by_name("a/b.PNG"))
        self.assertEqual("video", cls._media_kind_by_name("demo.mp4"))
        self.assertEqual("record", cls._media_kind_by_name("voice.amr"))
        self.assertEqual("file", cls._media_kind_by_name("报告.pdf"))
        self.assertEqual("file", cls._media_kind_by_name("未知.bin"))

    async def test_send_local_file_uses_workspace_relative_path(self):
        hooks = _hooks()
        plugin = await self._make_plugin()
        bot = hooks.FakeBot()
        workspace = plugin.storage.path.parent / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "shot.png").write_bytes(PNG)
        event = hooks.FakeEvent(
            hooks._group_raw("发给我", 4702), "发给我", bot, wake=True)
        result = json.loads(await plugin.send_local_file_tool(
            event, path="shot.png"))
        self.assertTrue(result["ok"], result)
        self.assertEqual("image", result["type"])
        sent = [call for call in bot.calls if call[0] == "send_group_msg"]
        self.assertTrue(sent, "应经网关发出群消息")
        segment = sent[-1][1]["message"][0]
        self.assertEqual("image", segment["type"])
        self.assertTrue(segment["data"]["file"].startswith("base://"))


if __name__ == "__main__":
    unittest.main()
