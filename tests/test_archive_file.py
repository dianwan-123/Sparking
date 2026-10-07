# -*- coding: utf-8 -*-
"""文件归档 get_file 参数回归测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


def _hooks():
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks

    return hooks


class ArchiveFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def test_file_segment_sends_both_get_file_params(self):
        """file 段归档必须同时传 file 与 file_id（严格校验要求全给）。"""
        hooks = _hooks()
        config = hooks._plugin_config()
        config["enable_media_archive"] = True
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
            plugin._known_scopes["123"] = scope

            calls: list[tuple[str, dict]] = []

            async def fake_execute(action, **params):
                calls.append((action, params))
                return {"data": {}}  # 无本地路径 → 走 record_url 兜底

            plugin.gateway.execute = fake_execute
            bot = hooks.FakeBot()
            event = hooks.FakeEvent(hooks._group_raw("发个文件", 10002), "发个文件", bot)
            raw = {
                "message": [
                    {"type": "file", "data": {"file_id": "FTHEFILE", "name": "资料.zip"}},
                ],
            }
            await plugin._archive_media(event, raw, scope, "10002", "")

            get_file_calls = [c for c in calls if c[0] == "get_file"]
            self.assertEqual(1, len(get_file_calls))
            params = get_file_calls[0][1]
            self.assertEqual("FTHEFILE", params.get("file_id"))
            self.assertEqual("FTHEFILE", params.get("file"))
        finally:
            await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
