from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.models import NormalizedMessage
from src.storage import Storage


def message(group: str, upstream: str, text: str, event_id: str, sender: str = "u1", kind: str = "message.created") -> NormalizedMessage:
    return NormalizedMessage("onebot", "bot", group, upstream, sender, sender, text, "2026-01-01T00:00:00+00:00", {"event_id": event_id}, event_type=kind)


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.db"
        self.store = await Storage(self.path).open()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.temp.cleanup()

    async def test_pragmas_idempotency_and_revision(self) -> None:
        first = await self.store.ingest_message(message("g1", "m1", "alpha", "e1"))
        duplicate = await self.store.ingest_message(message("g1", "m1", "alpha", "e1"))
        revised = await self.store.ingest_message(message("g1", "m1", "beta", "e2", kind="message.edited"))
        self.assertEqual(first.revision_id, duplicate.revision_id)
        self.assertEqual(first.message_id, revised.message_id)
        recent = await self.store.recent_messages(first.scope_id)
        self.assertEqual([x.text for x in recent], ["beta"])
        foreign_keys = await self.store._fetchone("PRAGMA foreign_keys")
        journal = await self.store._fetchone("PRAGMA journal_mode")
        self.assertEqual(foreign_keys[0], 1)
        self.assertEqual(str(journal[0]).lower(), "wal")

    async def test_delete_user_all_revisions_purges_derived_and_media(self) -> None:
        item = await self.store.ingest_message(message("g1", "m1", "source body", "e1", "target"))
        await self.store.ingest_message(message("g1", "m1", "edited by projection", "e2", "other", "message.edited"))
        summary = await self.store.store_summary(item.scope_id, 1, 1, 2, "derived title", "derived body", ["topic"], [item.message_id])
        from src.memory_ledger import MemoryLedger
        memory = await MemoryLedger(self.store).apply_proposal(item.scope_id, {"kind": "fact", "subject": "derived", "value": "private", "evidence_ids": [item.message_id]})
        media = Path(self.temp.name) / "sticker.webp"
        media.write_bytes(b"x")
        await self.store.save_sticker("a" * 64, media, "image/webp", 1, message_id=item.message_id, scope_id=item.scope_id)
        await self.store.enqueue_job("old", {"body": "source body"}, item.scope_id)
        result = await self.store.delete_user("target", item.scope_id)
        self.assertEqual(1, result)
        self.assertEqual((media,), result.media_paths)
        self.assertEqual([], await self.store.get_messages(item.scope_id, [item.message_id]))
        self.assertIsNone(await self.store._fetchone("SELECT 1 FROM summary_nodes WHERE summary_id=?", (summary.summary_id,)))
        self.assertIsNone(await self.store._fetchone("SELECT 1 FROM memory_items WHERE memory_id=?", (memory.memory_id,)))
        jobs = await self.store._fetchall("SELECT kind,payload_json FROM jobs WHERE scope_id=?", (item.scope_id,))
        self.assertEqual(["rebuild"], [row["kind"] for row in jobs])
        self.assertNotIn("source body", jobs[0]["payload_json"])
        await self.store.cleanup_media(result.media_paths)
        self.assertFalse(media.exists())

    async def test_jobs_audit_stickers_export_and_rebuild(self) -> None:
        item = await self.store.ingest_message(message("g1", "m1", "alpha", "e1"))
        job_id = await self.store.enqueue_job("compress", {"from": 1}, item.scope_id, "once")
        self.assertEqual(job_id, await self.store.enqueue_job("compress", {"from": 1}, item.scope_id, "once"))
        leased = await self.store.lease_job("worker")
        self.assertEqual(job_id, leased["job_id"])
        self.assertTrue(await self.store.fail_job(job_id, "worker", "retry", retry=True))
        leased = await self.store.lease_job("worker")
        self.assertTrue(await self.store.complete_job(leased["job_id"], "worker"))
        self.assertTrue(await self.store.write_audit("admin", "export", item.scope_id, "ok", scope_id=item.scope_id))
        orphan = Path(self.temp.name) / "orphan.png"
        await self.store.save_sticker("b" * 64, orphan, "image/png", 3)
        self.assertEqual([orphan], await self.store.orphan_sticker_paths(remove_records=True))
        exported = await self.store.export_scope(item.scope_id)
        self.assertEqual("alpha", exported["messages"][0]["text"])
        self.assertTrue(await self.store.request_rebuild(item.scope_id))

    async def test_scope_isolation_deletion_backup_and_stats(self) -> None:
        one = await self.store.ingest_message(message("g1", "same", "secret one", "e1"))
        two = await self.store.ingest_message(message("g2", "same", "secret two", "e2"))
        self.assertEqual([], await self.store.get_messages(one.scope_id, [two.message_id]))
        await self.store.store_summary(one.scope_id, 1, 1, 1, "digest", "body", ["topic"], [one.message_id])
        self.assertEqual(1, await self.store.delete_message(one.scope_id, one.message_id))
        self.assertEqual([], await self.store.list_summaries(one.scope_id))
        self.assertEqual("secret two", (await self.store.get_messages(two.scope_id, [two.message_id]))[0].text)
        backup = await self.store.backup(Path(self.temp.name) / "backup.db")
        self.assertTrue(backup.exists())
        stats = await self.store.stats()
        self.assertEqual(1, stats["message_identities"])
