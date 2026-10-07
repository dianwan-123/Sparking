from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from src.media_archive import MediaArchive


class VisionSupportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    async def test_find_by_message_and_get_base64(self):
        archive = await MediaArchive(self._tmp.name).open()
        try:
            png = b"\x89PNG\r\n\x1a\n" + b"imagedata" * 20
            record = await archive.save_bytes(
                png, scope_id="s1", kind="image", mime="image/png",
                source_message_id="msg-1",
            )
            await archive.record_url("https://example.com/x", scope_id="s1",
                                     source_message_id="msg-1")
            found = await archive.find_by_message("s1", "msg-1", kind="image")
            self.assertEqual(1, len(found))
            self.assertEqual(record.item_id, found[0].item_id)
            mime, b64 = await archive.get_base64(record.item_id)
            self.assertEqual("image/png", mime)
            self.assertTrue(b64.startswith("iVBOR"))
        finally:
            await archive.close()

    async def test_describe_uses_vision_provider_and_records_image_urls(self):
        import sys
        from pathlib import Path as P

        sys.path.insert(0, str(P(__file__).resolve().parents[1]))
        import tests.test_plugin_hooks as hooks

        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config() | {"vision_provider_id": "vision-p"}
        )
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        recorded = {}

        async def llm_generate(**kwargs):
            recorded.update(kwargs)
            import types

            return types.SimpleNamespace(completion_text=" 一只猫坐在键盘上 ")

        plugin.context.llm_generate = llm_generate
        description = await plugin._describe_images([("image/png", "QUJD")])
        self.assertEqual("一只猫坐在键盘上", description)
        self.assertEqual("vision-p", recorded["chat_provider_id"])
        self.assertEqual(["data:image/png;base64,QUJD"], recorded["image_urls"])

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
