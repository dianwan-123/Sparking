from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.models import NormalizedMessage
from src.retrieval import RankedId, RetrievalService, reciprocal_rank_fusion
from src.storage import Storage


class FakeEmbedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float("apple" in text), float("banana" in text)] for text in texts]


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = await Storage(Path(self.temp.name) / "db.sqlite").open()
        self.a = await self._add("g1", "1", "apple red", "u1")
        self.b = await self._add("g1", "2", "banana yellow", "u2")
        self.foreign = await self._add("g2", "3", "apple private", "u3")
        self.chinese = await self._add("g1", "4", "明天讨论数据库迁移方案", "u4")

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.temp.cleanup()

    async def _add(self, group: str, mid: str, text: str, sender: str):
        return await self.store.ingest_message(NormalizedMessage("p", "bot", group, mid, sender, sender, text, f"2026-01-0{mid}T00:00:00+00:00", {"event_id": "e" + mid}))

    async def test_lexical_scope_filters_and_context(self) -> None:
        service = RetrievalService(self.store)
        hits = await service.search(self.a.scope_id, "apple")
        self.assertEqual([self.a.message_id], [x.message_id for x in hits])
        self.assertEqual([], await service.by_ids(self.a.scope_id, [self.foreign.message_id]))
        context = await service.context(self.a.scope_id, self.a.message_id, 5)
        self.assertEqual([self.a.message_id, self.b.message_id, self.chinese.message_id], [x.message_id for x in context])

    async def test_embedding_scope_validation_and_chinese_search(self) -> None:
        service = RetrievalService(self.store, FakeEmbedding())
        with self.assertRaises(ValueError):
            await service.index_embedding(self.foreign.scope_id, self.a)
        hits = await service.search(self.a.scope_id, "数据库迁移")
        self.assertEqual(self.chinese.message_id, hits[0].message_id)

    async def test_embedding_and_rrf(self) -> None:
        service = RetrievalService(self.store, FakeEmbedding())
        self.assertTrue(await service.index_embedding(self.a.scope_id, self.a))
        self.assertTrue(await service.index_embedding(self.b.scope_id, self.b))
        hits = await service.search(self.a.scope_id, "apple", semantic=True)
        self.assertEqual(self.a.message_id, hits[0].message_id)
        fused = reciprocal_rank_fusion([[RankedId("a", 1), RankedId("b", 0)], [RankedId("b", 1)]])
        self.assertEqual("b", fused[0].item_id)
