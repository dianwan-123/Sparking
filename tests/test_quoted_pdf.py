# -*- coding: utf-8 -*-
"""v0.32.4 回归：引用消息(reply)解析/PDF 正文注入 + SnowLuma 图片 URL 归一化。"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _hooks():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks
    return hooks


def _main():
    import DFYChat.main as main_module
    return main_module


class ReplyRefsTests(unittest.TestCase):
    def test_reply_segment_id_extracted(self):
        hooks = _hooks()
        raw = {"message": [
            {"type": "reply", "data": {"id": "9002"}},
            {"type": "at", "data": {"qq": "1"}},
            {"type": "text", "data": {"text": "读一下这份试卷"}},
        ]}
        self.assertEqual(["9002"], hooks.LongMemoryAgentPlugin._reply_refs(raw))

    def test_reply_id_int_and_absent(self):
        hooks = _hooks()
        cls = hooks.LongMemoryAgentPlugin
        self.assertEqual(
            ["12345"],
            cls._reply_refs({"message": [{"type": "reply", "data": {"id": 12345}}]}),
        )
        self.assertEqual([], cls._reply_refs({"message": "plain text"}))
        self.assertEqual([], cls._reply_refs({"message": [{"type": "reply", "data": {}}]}))


class ImagePayloadNormalizeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _plugin(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin.__new__(hooks.LongMemoryAgentPlugin)
        plugin.settings = types.SimpleNamespace(sticker_max_bytes=1024 * 1024)
        return plugin

    async def test_base64_passthrough(self):
        plugin = self._plugin()
        out = await plugin._normalize_image_payload({"data": {"base64": "QUJD"}})
        self.assertEqual("QUJD", out["data"]["base64"])

    async def test_local_path_passthrough(self):
        plugin = self._plugin()
        path = Path(self._tmp.name) / "a.png"
        path.write_bytes(b"\x89PNG")
        raw = {"data": {"file": str(path)}}
        out = await plugin._normalize_image_payload(raw)
        self.assertIs(out, raw, "本地路径应原样放行（下游直接读文件）")

    async def test_url_downloaded_to_base64(self):
        """SnowLuma 的 get_image 返回远端 URL——必须下载转 base64，
        否则表情学习直接报 'URLs are not accepted'（实测日志）。"""
        plugin = self._plugin()
        main = _main()
        seen = []

        async def fake_fetch(url, limit):
            seen.append((url, limit))
            return b"IMGBYTES"

        original = main.fetch_bounded
        main.fetch_bounded = fake_fetch
        self.addCleanup(setattr, main, "fetch_bounded", original)

        out = await plugin._normalize_image_payload(
            {"data": {"file": "https://example.com/x.png"}})
        self.assertEqual([("https://example.com/x.png", 1024 * 1024)], seen)
        self.assertEqual(
            base64.b64encode(b"IMGBYTES").decode("ascii"), out["data"]["base64"])

    async def test_urlless_payload_is_none(self):
        plugin = self._plugin()
        self.assertIsNone(
            await plugin._normalize_image_payload({"data": {"url": "https://x"}}))


class QuotedResolutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _plugin(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin.__new__(hooks.LongMemoryAgentPlugin)
        plugin._quoted_cache = {}
        plugin._bind_gateway_client = lambda: None
        return plugin

    async def test_db_hit_with_file_excerpt(self):
        from DFYChat.src.models import StoredMessage

        plugin = self._plugin()
        doc = Path(self._tmp.name) / "试卷.txt"
        doc.write_text("选择题：1.A 2.B", encoding="utf-8")

        class _Storage:
            async def find_by_upstream(self, scope_id, upstream_id):
                return StoredMessage(
                    message_id="m-1", revision_id="r-1", scope_id=scope_id,
                    scope_seq=1, upstream_message_id=upstream_id,
                    sender_id="9", sender_name="指令师", text="[文件]",
                    occurred_at="2026-10-05T00:00:00+00:00",
                    parts=[{"type": "file", "data": {
                        "file": "试卷.txt", "file_id": "fid-1", "name": "试卷.txt"}}],
                )

        class _Media:
            async def find_by_message(self, scope_id, message_id, kind=None):
                if kind == "file":
                    return [types.SimpleNamespace(
                        item_id="m9", status="saved", kind="file")]
                return []

            async def get_path(self, item_id):
                return str(doc)

        plugin.storage = _Storage()
        plugin.media = _Media()
        plugin.gateway = None

        digest = await plugin._quoted_digest("scope-1", "9002")
        self.assertEqual("指令师", digest["sender"])
        self.assertEqual(1, len(digest["files"]))
        self.assertEqual("m9", digest["files"][0]["media_id"])
        self.assertIn("选择题", digest["files"][0]["excerpt"])

    async def test_gateway_fetch_when_not_stored(self):
        plugin = self._plugin()
        doc = Path(self._tmp.name) / "卷.txt"
        doc.write_text("题干A", encoding="utf-8")
        calls = []

        class _Gateway:
            async def execute(self, action, **params):
                calls.append((action, params))
                if action == "get_msg":
                    return {"status": "ok", "data": {
                        "message_id": "555",
                        "sender": {"nickname": "指令师"},
                        "message": [{"type": "file", "data": {
                            "file_id": "fid-9", "name": "卷.pdf"}}],
                    }}
                if action == "get_file":
                    return {"status": "ok", "data": {
                        "file": str(doc), "file_name": "卷.pdf"}}
                return {"status": "ok", "data": {}}

        saved = []

        class _Storage:
            async def find_by_upstream(self, scope_id, upstream_id):
                return None

        class _Media:
            max_file_bytes = 10 * 1024 * 1024

            async def find_by_message(self, scope_id, message_id, kind=None):
                return []

            async def save_bytes(self, data, **kwargs):
                saved.append((data, kwargs))
                return types.SimpleNamespace(item_id="f2", status="saved")

            async def get_path(self, item_id):
                return str(doc)

        plugin.storage = _Storage()
        plugin.media = _Media()
        plugin.gateway = _Gateway()

        digest = await plugin._quoted_digest("scope-1", "9002")
        self.assertIn(("get_msg", {"message_id": "9002"}), calls)
        self.assertTrue(any(
            action == "get_file" and params.get("file_id") == "fid-9"
            for action, params in calls), "get_file 要带 file_id")
        self.assertEqual(1, len(saved), "文件应归档一次")
        self.assertEqual("f2", digest["files"][0]["media_id"])
        self.assertIn("题干A", digest["files"][0]["excerpt"])

    async def test_cache_avoids_repeat(self):
        plugin = self._plugin()
        hits = []

        class _Storage:
            async def find_by_upstream(self, scope_id, upstream_id):
                hits.append(upstream_id)
                return None

        plugin.storage = _Storage()
        plugin.media = None
        plugin.gateway = None
        self.assertIsNone(await plugin._quoted_digest("s", "1"))
        self.assertIsNone(await plugin._quoted_digest("s", "1"))
        self.assertEqual(["1"], hits, "第二次应命中缓存不再解析")

    async def test_archive_file_segment_url_form(self):
        plugin = self._plugin()
        main = _main()
        calls = []

        class _Gateway:
            async def execute(self, action, **params):
                calls.append((action, params))
                return {"status": "ok", "data": {
                    "file": "https://example.com/卷.pdf", "file_name": "卷.pdf"}}

        saved = []

        class _Media:
            max_file_bytes = 1024 * 1024

            async def save_bytes(self, data, **kwargs):
                saved.append((data, kwargs))
                return types.SimpleNamespace(item_id="f3")

        async def fake_fetch(url, limit):
            return b"PDFBYTES"

        original = main.fetch_bounded
        main.fetch_bounded = fake_fetch
        self.addCleanup(setattr, main, "fetch_bounded", original)

        plugin.gateway = _Gateway()
        plugin.media = _Media()
        media_id = await plugin._archive_file_segment(
            "s", "mid", {"file_id": "fid-3", "name": "卷.pdf"})
        self.assertEqual("f3", media_id)
        self.assertEqual(b"PDFBYTES", saved[0][0])
        self.assertEqual("file", saved[0][1]["kind"])


class QuotedContextInjectionTests(unittest.IsolatedAsyncioTestCase):
    """端到端：被引用的 PDF 正文节选要出现在判定上下文里（实录"试卷呢"回归）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_quoted_pdf_excerpt_reaches_judge(self):
        hooks = _hooks()
        from DFYChat.src.models import StoredMessage

        doc = Path(self._tmp.name) / "试卷.txt"
        doc.write_text("选择题 1.A 2.B", encoding="utf-8")

        responses = ['{"action": "text", "segments": [{"text": "喏 看过了"}]}']
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext(responses), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        class _Media:
            max_file_bytes = 10 * 1024 * 1024

            async def find_by_message(self, scope_id, message_id, kind=None):
                if kind == "file":
                    return [types.SimpleNamespace(
                        item_id="m9", status="saved", kind="file")]
                return []

            async def get_path(self, item_id):
                return str(doc)

            async def save_bytes(self, data, **kwargs):
                return types.SimpleNamespace(item_id="mx")

            async def close(self):
                return None

        plugin.media = _Media()

        async def fake_find(scope_id, upstream_id):
            return StoredMessage(
                message_id="quoted-1", revision_id="r-1", scope_id=scope_id,
                scope_seq=1, upstream_message_id=upstream_id,
                sender_id="9", sender_name="指令师", text="[文件]",
                occurred_at="2026-10-05T00:00:00+00:00",
                parts=[{"type": "file", "data": {
                    "file_id": "fid-1", "name": "试卷.txt"}}],
            )

        plugin.storage.find_by_upstream = fake_find

        raw = hooks._group_raw("读一下这份试卷", 7001)
        raw["message"] = [
            {"type": "reply", "data": {"id": "6601"}},
            {"type": "at", "data": {"qq": "1"}},
            {"type": "text", "data": {"text": "读一下这份试卷"}},
        ]
        event = hooks.FakeEvent(raw, "读一下这份试卷", hooks.FakeBot(), wake=True)
        await plugin.on_onebot_event(event)
        while plugin._tasks:
            import asyncio

            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)

        prompts = [
            str(call.get("prompt", ""))
            for call in plugin.context.llm_calls if isinstance(call, dict)
        ]
        joined = "\n".join(prompts)
        self.assertIn("quoted_messages", joined, "判定上下文应带引用消息摘要")
        self.assertIn("选择题 1.A 2.B", joined, "PDF 正文节选应注入上下文")


class IdentityDisciplinePromptTests(unittest.TestCase):
    """回归：A 委托的试卷没到、B 发了张图，bot 把 B 的图当材料并回错人
    （实录"收到图了（这张要是就是那份天明一模）…之前光喊口令"）。"""

    def test_prompts_carry_identity_discipline(self):
        _hooks()
        from DFYChat.src.prompts import DECISION_SYSTEM_PROMPT, REPLY_SYSTEM_PROMPT

        for text in (DECISION_SYSTEM_PROMPT, REPLY_SYSTEM_PROMPT):
            self.assertIn("对象纪律", text)
            self.assertIn("材料", text)
        self.assertIn("收到图了", REPLY_SYSTEM_PROMPT, "必须点名禁止认领别人的图")
        self.assertIn("不许当成那份材料", REPLY_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
