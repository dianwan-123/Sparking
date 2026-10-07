# -*- coding: utf-8 -*-
"""子代理派发（含图片批次）与文档阅读（PDF/txt）测试。"""
from __future__ import annotations

import asyncio
import json
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


class _FakeMedia:
    def __init__(self, files: dict[str, Path], images: dict[str, tuple[str, str]]):
        self._files = files
        self._images = images

    async def get_path(self, item_id: str) -> str:
        if item_id not in self._files:
            raise RuntimeError("item is not available")
        return str(self._files[item_id])

    async def get_base64(self, item_id: str) -> tuple[str, str]:
        if item_id not in self._images:
            raise RuntimeError("item is not available")
        return self._images[item_id]


class SubagentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _plugin(self, responses=None):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext(list(responses or [])), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin, hooks

    async def test_subagent_enabled_by_default_and_carries_images(self):
        plugin, hooks = await self._plugin(["图1是一张截图：豆包模型对话"])
        self.assertTrue(plugin.settings.subagent_enabled, "子代理默认开启")
        captured: dict = {}

        async def llm_generate(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(completion_text="图1内容是……")

        plugin.context.llm_generate = llm_generate
        plugin.media = _FakeMedia({}, {"m1": ("image/png", "AAAA")})
        result = await plugin.dispatch_subagent_tool(
            None, task="分析这张截图", media_ids_json='["m1"]')
        self.assertIn("图1内容是", result)
        self.assertEqual(
            ["data:image/png;base64,AAAA"], captured.get("image_urls"),
            "media_id 应转成 data URL 直传子agent模型")
        self.assertEqual(1, captured["prompt"].count("image_count"))

    async def test_parallel_tasks_with_mixed_media(self):
        plugin, hooks = await self._plugin()
        seen: list = []

        async def llm_generate(**kwargs):
            # 并行任务完成顺序不确定：按任务内容区分，不按调用顺序
            seen.append(kwargs)
            is_image = bool(kwargs.get("image_urls"))
            text = "B结论" if is_image else "A结论"
            return types.SimpleNamespace(completion_text=text)

        plugin.context.llm_generate = llm_generate
        plugin.media = _FakeMedia({}, {"m1": ("image/png", "BBBB")})
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123")
        plugin._known_scopes["123"] = scope
        raw = await plugin.dispatch_parallel_subagents_tool(
            None, tasks_json=json.dumps([
                "总结今天的话题",
                {"task": "分析截图", "media_ids": ["m1"]},
            ]))
        data = json.loads(raw)
        by_result = {item["result"] for item in data}
        self.assertEqual({"A结论", "B结论"}, by_result)
        # 一个任务带图、一个不带
        image_calls = [call for call in seen if call.get("image_urls")]
        self.assertEqual(1, len(image_calls))
        self.assertEqual(["data:image/png;base64,BBBB"], image_calls[0]["image_urls"])

    async def test_subagent_gated_when_disabled(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config() | {"subagent_enabled": False})
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        result = await plugin.dispatch_subagent_tool(None, task="x")
        self.assertIn("未启用", result)
        names = {str(getattr(t, "name", "")) for t in plugin._effective_tool_set(None).tools}
        self.assertNotIn("dispatch_subagent", names)


class DocumentReaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_extract_text_file(self):
        from src.pdf_reader import extract_document_text

        path = Path(self._tmp.name) / "notes.md"
        path.write_text("# 标题\n内容第一行", encoding="utf-8")
        result = extract_document_text(path)
        self.assertEqual("text", result["kind"])
        self.assertIn("内容第一行", result["text"])

    async def test_extract_unsupported_and_missing(self):
        from src.pdf_reader import extract_document_text

        result = extract_document_text(Path(self._tmp.name) / "nope.pdf")
        self.assertEqual("missing", result["kind"])
        binary = Path(self._tmp.name) / "x.bin"
        binary.write_bytes(b"\x00\x01")
        result = extract_document_text(binary)
        self.assertEqual("unsupported", result["kind"])

    async def test_extract_pdf_text_layer(self):
        from src.pdf_reader import extract_document_text, ensure_pypdf

        ok, _ = ensure_pypdf()
        if not ok:
            self.skipTest("pypdf 未安装且自动安装失败")
        pdf = Path(self._tmp.name) / "doc.pdf"
        pdf.write_bytes(_minimal_pdf("Hello PDF"))
        result = extract_document_text(pdf)
        self.assertEqual("pdf", result["kind"])
        self.assertIn("Hello PDF", result["text"])

    async def test_read_document_tool_with_fake_media(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        note = Path(self._tmp.name) / "file.txt"
        note.write_text("会议纪要：周五发版", encoding="utf-8")
        plugin.media = _FakeMedia({"f1": note}, {})
        result = await plugin.read_document_tool(None, media_id="f1")
        data = json.loads(result)
        self.assertIn("周五发版", data["text"])


def _minimal_pdf(text: str) -> bytes:
    """Build a valid single-page PDF with one text object (real xref)."""
    header = b"%PDF-1.4\n"
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    body = bytearray(header)
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{index} 0 obj".encode() + obj + b"endobj\n"
    xref_at = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode()
    body += b"0000000000 65535 f \n"
    for offset in offsets:
        body += f"{offset:010d} 00000 n \n".encode()
    body += (
        f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(body)


if __name__ == "__main__":
    unittest.main()
