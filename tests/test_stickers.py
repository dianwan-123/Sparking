from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from src.stickers import StickerError, StickerManager


PNG = b"\x89PNG\r\n\x1a\n" + b"payload"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg" + b"\xff\xd9"
GIF = b"GIF89a" + b"gif"
WEBP_PAYLOAD = b"VP8 " + b"webp"
WEBP = b"RIFF" + (len(WEBP_PAYLOAD) + 4).to_bytes(4, "little") + b"WEBP" + WEBP_PAYLOAD


class StickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingests_base64_validates_magic_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = []
            manager = StickerManager(directory, metadata_callback=saved.append)
            encoded = base64.b64encode(PNG).decode("ascii")
            first = await manager.ingest({"data": {"base64": encoded}}, source_ref="msg:1")
            second = await manager.ingest({"data": {"base64": encoded}}, source_ref="msg:2")
            self.assertEqual(first.sha256, hashlib.sha256(PNG).hexdigest())
            self.assertEqual(first.path, second.path)
            self.assertEqual(first.media_type, "image/png")
            files = [p for p in Path(directory).iterdir() if p.name != "index.json"]
            self.assertEqual(len(list(files)), 1)
            self.assertEqual(len(saved), 2)

    async def test_accepts_only_supported_signatures_from_local_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "source"
            source_dir.mkdir()
            store = Path(directory) / "store"
            manager = StickerManager(store)
            for index, (data, media) in enumerate(((JPEG, "image/jpeg"), (GIF, "image/gif"),
                                                   (WEBP, "image/webp"))):
                path = source_dir / f"image-{index}.bin"
                path.write_bytes(data)
                record = await manager.ingest({"data": {"file": str(path)}}, trusted_get_image=True)
                self.assertEqual(record.media_type, media)
                self.assertEqual(Path(record.path).parent, store.resolve())

    async def test_rejects_urls_invalid_magic_and_oversized_data(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = StickerManager(directory, max_bytes=12)
            for value in (
                {"data": {"url": "https://example.invalid/a.png"}},
                {"data": {"file": "https://example.invalid/a.png"}},
                {"data": {"base64": base64.b64encode(b"not-image").decode("ascii")}},
                {"data": {"base64": base64.b64encode(PNG + b"too much").decode("ascii")}},
            ):
                with self.subTest(value=value), self.assertRaises(StickerError):
                    await manager.ingest(value)

    async def test_local_paths_need_trust_or_allowed_root_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            image = source / "image.png"
            image.write_bytes(PNG)
            manager = StickerManager(root / "store")
            with self.assertRaises(StickerError):
                await manager.ingest({"data": {"file": str(image)}})
            allowed = StickerManager(root / "allowed-store", allowed_roots=[source])
            self.assertEqual((await allowed.ingest({"data": {"file": str(image)}})).media_type,
                             "image/png")
            outside = root / "outside.png"
            outside.write_bytes(PNG)
            with self.assertRaises(StickerError):
                await allowed.ingest({"data": {"file": str(outside)}})
            link = source / "link.png"
            try:
                link.symlink_to(image)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(StickerError):
                await allowed.ingest({"data": {"file": str(link)}})

    async def test_rejects_malformed_jpeg_and_webp_structures(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = StickerManager(directory)
            malformed = (b"\xff\xd8\xff\xe0missing-eoi", b"RIFF\x04\x00\x00\x00WEBPxxxx")
            for data in malformed:
                encoded = base64.b64encode(data).decode("ascii")
                with self.assertRaises(StickerError):
                    await manager.ingest({"data": {"base64": encoded}})

    async def test_management_apis_capacity_eviction_and_storage_field_adapter(self):
        class Storage:
            def __init__(self):
                self.values = []

            def save_sticker(self, sha256, path, media_type, size, source_ref):
                self.values.append((sha256, path, media_type, size, source_ref))

        with tempfile.TemporaryDirectory() as directory:
            storage = Storage()
            manager = StickerManager(directory, capacity_bytes=len(PNG) + len(JPEG) - 1,
                                     metadata_store=storage)
            encode = lambda data: {"data": {"base64": base64.b64encode(data).decode("ascii")}}
            first = await manager.ingest(encode(PNG), source_ref="one")
            second = await manager.ingest(encode(JPEG), source_ref="two")
            stats = manager.stats()
            self.assertEqual(stats.count, 1)
            self.assertEqual(stats.total_bytes, len(JPEG))
            self.assertEqual(manager.select()[0].sha256, second.sha256)
            self.assertFalse(Path(first.path).exists())
            self.assertEqual(len(storage.values), 2)
            self.assertEqual(len(manager.catalog()), 1)  # index.json 索引仍在
            self.assertTrue(manager.delete(second.sha256))
            self.assertFalse(manager.delete(second.sha256))
            self.assertEqual(manager.stats().count, 0)

    async def test_adapter_supports_existing_storage_field_names(self):
        class ExistingStorage:
            def __init__(self):
                self.value = None

            async def save_sticker(self, sha256, path, mime_type, size_bytes, metadata=None,
                                   message_id=None, scope_id=None):
                self.value = (sha256, path, mime_type, size_bytes, metadata)

        with tempfile.TemporaryDirectory() as directory:
            storage = ExistingStorage()
            manager = StickerManager(directory, metadata_store=storage)
            encoded = base64.b64encode(PNG).decode("ascii")
            record = await manager.ingest({"data": {"base64": encoded}}, source_ref="msg:7")
            self.assertEqual(storage.value[0], record.sha256)
            self.assertEqual(storage.value[2], "image/png")
            self.assertEqual(storage.value[3], len(PNG))
            self.assertEqual(storage.value[4], {"source_ref": "msg:7"})

    async def test_enforces_count_limit_but_allows_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = StickerManager(directory, max_count=1)
            encoded = lambda data: {"data": {"base64": base64.b64encode(data).decode("ascii")}}
            await manager.ingest(encoded(PNG))
            await manager.ingest(encoded(PNG))
            with self.assertRaises(StickerError):
                await manager.ingest(encoded(JPEG))


if __name__ == "__main__":
    unittest.main()
