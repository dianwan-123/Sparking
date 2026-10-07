# -*- coding: utf-8 -*-
"""媒体归档自愈回归：'saved' 记录的文件被删后，再次保存要重新落盘，
避免 send_image 拿到死路径导致 NapCat ENOENT（用户日志 retcode=1200）。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.media_archive import MediaArchive, MediaArchiveError

_PNG = b"\x89PNG\r\n\x1a\n" + b"selfheal-test-bytes" * 8


class MediaSelfHealTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.archive = await MediaArchive(Path(self._tmp.name) / "media").open()

    async def asyncTearDown(self):
        await self.archive.close()

    async def test_missing_file_is_rematerialised_on_resave(self):
        first = await self.archive.save_bytes(_PNG, scope_id="s", kind="image", mime="image/png")
        self.assertEqual("saved", first.status)
        path = await self.archive.get_path(first.item_id)
        self.assertTrue(Path(path).exists())

        # simulate the file vanishing (reinstall wiped media/, AV, manual clean…)
        os.remove(path)
        with self.assertRaises(MediaArchiveError):
            await self.archive.get_path(first.item_id)

        # saving the same bytes must NOT just say "duplicate" and leave it gone —
        # it re-writes the content-addressed file so the id is usable again
        healed = await self.archive.save_bytes(_PNG, scope_id="s", kind="image", mime="image/png")
        self.assertEqual(first.item_id, healed.item_id, "同内容仍复用同一条记录")
        self.assertEqual("saved", healed.status)
        self.assertTrue(Path(await self.archive.get_path(first.item_id)).exists())

    async def test_present_file_is_reported_duplicate(self):
        first = await self.archive.save_bytes(_PNG, scope_id="s", kind="image", mime="image/png")
        again = await self.archive.save_bytes(_PNG, scope_id="s", kind="image", mime="image/png")
        self.assertEqual(first.item_id, again.item_id)
        self.assertEqual("duplicate", again.status)
        self.assertTrue(Path(await self.archive.get_path(first.item_id)).exists())


if __name__ == "__main__":
    unittest.main()
