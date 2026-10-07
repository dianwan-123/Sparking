# -*- coding: utf-8 -*-
"""v0.32.6 回归：表情包学习默认开 + pick 无描述回退 + 库存注入判定上下文。"""
from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"sticker-payload"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.stickers import StickerManager, StickerRecord  # noqa: E402


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class StickerPickFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_pick_falls_back_to_popular_without_description(self):
        """没有 vision 描述的库存 pick(query) 恒空 → 模型以为没货，
        是从不主动发表情的一环；无命中时必须回退热门库存。"""
        with tempfile.TemporaryDirectory() as directory:
            manager = StickerManager(directory)
            await manager.ingest(
                {"data": {"base64": base64.b64encode(PNG).decode("ascii")}},
                source_ref="m1",
            )
            picked = manager.pick("开心", 3)
            self.assertEqual(1, len(picked), "无描述时也要能挑到（回退热门）")
            catalog = manager.catalog(limit=0)
            self.assertEqual(catalog[0]["sticker_id"], picked[0]["sticker_id"])


class StickerLearningDefaultTests(unittest.TestCase):
    def test_sticker_learning_defaults_on(self):
        from src.config import PluginConfig

        config = PluginConfig.from_mapping({"enabled": True})
        self.assertTrue(config.sticker_learning, "表情学习默认开启（用户要求自主学表情）")


class StickerInventoryInjectionTests(unittest.IsolatedAsyncioTestCase):
    """端到端：库存里有表情时，判定上下文必须出现 sticker_inventory。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_inventory_reaches_judge_prompt(self):
        hooks = _hooks()
        responses = ['{"action": "ignore"}']
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext(responses), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        self.assertIsNotNone(plugin.stickers)
        await plugin.stickers.ingest(
            {"data": {"base64": base64.b64encode(PNG).decode("ascii")}},
            source_ref="seed",
        )

        event = hooks.FakeEvent(
            hooks._group_raw("在吗", 9001), "在吗", hooks.FakeBot(), wake=True)
        await plugin.on_onebot_event(event)
        while plugin._tasks:
            import asyncio

            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)

        prompts = [
            str(call.get("prompt", ""))
            for call in plugin.context.llm_calls if isinstance(call, dict)
        ]
        joined = "\n".join(prompts)
        self.assertIn("sticker_inventory", joined, "判定应知道有表情包库存可发")


if __name__ == "__main__":
    unittest.main()


class StickerSendSizeTests(unittest.TestCase):
    """实录："bot 发表情包时会直接发送图片，导致比例非常大"——原图常 1000px+，
    QQ 按原尺寸铺满聊天框。发送前缩到表情包规格（最长边 256），按 sha256 缓存。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = StickerManager(Path(self._tmp.name))

    @staticmethod
    def _make(path: Path, size: tuple[int, int], color="red"):
        from PIL import Image

        Image.new("RGB", size, color).save(path)

    def test_large_sticker_is_scaled_for_send(self):
        from PIL import Image

        big = Path(self._tmp.name) / ("a" * 64 + ".png")
        self._make(big, (1200, 900))
        record = StickerRecord("a" * 64, str(big), "image/png", big.stat().st_size)
        scaled = Path(self.manager.send_path(record, max_edge=256))
        self.assertTrue(scaled.is_file())
        with Image.open(scaled) as image:
            self.assertLessEqual(max(image.size), 256, "发送尺寸必须是表情包规格")
        with Image.open(big) as original:
            self.assertEqual((1200, 900), original.size, "原图不动")
        self.assertNotEqual(str(big), str(scaled), "发送的是缩放副本")

    def test_small_sticker_is_sent_as_is(self):
        from PIL import Image

        small = Path(self._tmp.name) / ("b" * 64 + ".png")
        self._make(small, (100, 100))
        record = StickerRecord("b" * 64, str(small), "image/png", small.stat().st_size)
        self.assertEqual(str(small), self.manager.send_path(record))

    def test_gif_keeps_frames(self):
        gif = Path(self._tmp.name) / ("c" * 64 + ".gif")
        gif.write_bytes(b"GIF89a" + b"\x00" * 200000)
        record = StickerRecord("c" * 64, str(gif), "image/gif", gif.stat().st_size)
        self.assertEqual(str(gif), self.manager.send_path(record), "动图不缩（怕丢帧）")

    def test_broken_image_falls_back(self):
        broken = Path(self._tmp.name) / ("d" * 64 + ".png")
        broken.write_bytes(b"not an image" * 20000)
        record = StickerRecord("d" * 64, str(broken), "image/png", broken.stat().st_size)
        self.assertEqual(str(broken), self.manager.send_path(record), "缩放失败要回退原图")


class StickerSendWiringTests(unittest.IsolatedAsyncioTestCase):
    """实录二连：①"表情包依然会发大图"——缩放没接到所有发送路径；
    ②"bot 会发 [sticker: <sha256>] 这样的消息"——模型把 sticker_id 当文本写出。
    这里盯**真实的发送链路**（不是单独测 send_path）。"""

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
        plugin.stickers.root.mkdir(parents=True, exist_ok=True)
        return plugin, hooks

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _big_sticker(self, plugin):
        from PIL import Image

        digest = "a" * 64
        path = plugin.stickers.root / f"{digest}.png"
        Image.new("RGB", (1200, 900), "red").save(path)
        return digest, path

    async def _send_and_capture(self, plugin, hooks, descriptor, message_id=9701):
        import sys

        chain_cls = sys.modules["astrbot.api.event"].MessageChain
        seen: list[str] = []
        original = chain_cls.file_image

        def spy(self, path):
            seen.append(str(path))
            return original(self, path)

        chain_cls.file_image = spy
        try:
            event = hooks.FakeEvent(hooks._group_raw("x", message_id), "x",
                                    hooks.FakeBot(), wake=True)
            await plugin._send_segment(event, descriptor)
        finally:
            chain_cls.file_image = original
        return seen

    async def test_sticker_segment_sends_scaled_image(self):
        from PIL import Image

        plugin, hooks = await self._plugin()
        digest, _path = await self._big_sticker(plugin)
        sent = await self._send_and_capture(plugin, hooks,
                                           {"action": "sticker", "sticker_id": digest})
        self.assertEqual(1, len(sent), "表情段要发出图片")
        with Image.open(sent[0]) as image:
            self.assertLessEqual(max(image.size), 256, "发送的必须是缩过的表情包尺寸")

    async def test_sticker_id_as_text_is_converted(self):
        """模型把 [sticker: <id>] 写成文本时，要变成真的表情发送，不能发这段字。"""
        from PIL import Image

        plugin, hooks = await self._plugin()
        digest, _path = await self._big_sticker(plugin)
        sent = await self._send_and_capture(
            plugin, hooks, {"action": "text", "text": f"[sticker: {digest}]"}, 9702)
        self.assertEqual(1, len(sent), "整段占位要转成真表情")
        with Image.open(sent[0]) as image:
            self.assertLessEqual(max(image.size), 256)

    async def test_placeholder_inside_text_is_stripped(self):
        plugin, hooks = await self._plugin()
        digest, _path = await self._big_sticker(plugin)
        import sys

        chain_cls = sys.modules["astrbot.api.event"].MessageChain
        texts: list[str] = []
        original_message = chain_cls.message

        def spy(self, text):
            texts.append(str(text))
            return original_message(self, text)

        chain_cls.message = spy
        try:
            event = hooks.FakeEvent(hooks._group_raw("x", 9703), "x", hooks.FakeBot(),
                                    wake=True)
            await plugin._send_segment(
                event, {"action": "text", "text": f"笑死 [sticker: {digest}] 行"})
        finally:
            chain_cls.message = original_message
        joined = " ".join(texts)
        self.assertNotIn("sticker:", joined, "占位符绝不能发出去")
        self.assertIn("笑死", joined)
        self.assertIn("行", joined)

    async def test_unknown_placeholder_is_dropped(self):
        plugin, hooks = await self._plugin()
        sent = await self._send_and_capture(
            plugin, hooks, {"action": "text", "text": "[sticker: deadbeefdeadbeef]"}, 9704)
        self.assertEqual([], sent, "未知表情 id 不能发图，也不该崩")
