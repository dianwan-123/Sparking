from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.compression import CompressionService
from src.memory_ledger import MemoryLedger
from src.models import NormalizedMessage
from src.storage import Storage


class CompressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = await Storage(Path(self.temp.name) / "db.sqlite").open()
        self.message = await self._add("m0", "ship Friday")

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.temp.cleanup()

    async def _add(self, mid: str, text: str):
        return await self.store.ingest_message(NormalizedMessage("p", "b", "g", mid, "u", "User", text, "2026-01-01", {"event_id": "e-" + mid}))

    @staticmethod
    def _response(message_ids: list[str]) -> str:
        evidence = [message_ids[0]]
        return json.dumps({"title": "Plan", "topics": ["shipping"], "timeline": [], "facts": [{"text": "Friday", "evidence_ids": evidence}], "decisions": [], "tasks": [], "open_questions": [], "conflicts": [], "citations": evidence, "memory_proposals": [{"kind": "decision", "subject": "ship", "value": "Friday", "confidence": .9, "evidence_ids": evidence}]})

    async def test_l1_l2_manifest_citations_and_proposal(self) -> None:
        ids = [self.message.message_id]
        ledger = MemoryLedger(self.store)
        service = CompressionService(self.store, lambda _: _async(self._response(ids)), ledger)
        l1 = await service.compress_l1(self.message.scope_id, [self.message])
        self.assertEqual(ids, l1.citations)
        catalog = await ledger.catalog(self.message.scope_id)
        self.assertEqual(1, len(catalog))
        self.assertTrue(catalog[0].memory_id)
        l2 = await service.compress_l2(self.message.scope_id, [l1])
        row = await self.store._fetchone("SELECT manifest_json FROM summary_nodes WHERE summary_id=?", (l2.summary_id,))
        self.assertEqual([l1.summary_id], json.loads(row["manifest_json"])["summaries"])

    async def test_l2_l3_survive_model_quoting_summary_ids(self) -> None:
        """回归：聚合时弱模型按直觉引用输入项的 summary_id（存储只认
        message_id），过去必挂在 'summary citations must be non-empty input
        IDs'。现在应宽容纠正——聚合成功，落库 citations 是底层 message_id。"""
        m2 = await self._add("m1", "ship Monday")

        async def smart(prompt: str) -> str:
            data = json.loads(prompt.split("input=", 1)[1])
            # 模型偏爱引用 summary_id（L2/L3 输入项的显式 ID 字段）——正是坏例子
            ids = [str(item.get("summary_id") or item.get("message_id"))
                   for item in data if (item.get("summary_id") or item.get("message_id"))]
            evidence = ids[:1]
            return json.dumps({
                "title": "汇总", "topics": ["t"], "timeline": [],
                "facts": [{"text": "f", "evidence_ids": evidence}],
                "decisions": [], "tasks": [], "open_questions": [], "conflicts": [],
                "citations": ids,
                "memory_proposals": [{"kind": "fact", "subject": "s", "value": "v",
                                      "confidence": 0.8, "evidence_ids": evidence}],
            }, ensure_ascii=False)

        service = CompressionService(self.store, smart)
        leaf_ids = {self.message.message_id, m2.message_id}
        l1a = await service.compress_l1(self.message.scope_id, [self.message])
        l1b = await service.compress_l1(self.message.scope_id, [m2])
        l2 = await service.compress_l2(self.message.scope_id, [l1a, l1b])
        self.assertEqual(2, l2.level)
        self.assertTrue(l2.citations)
        # 存的是底层 message_id，不是模型引用的 summary_id
        self.assertTrue(set(l2.citations).issubset(leaf_ids))
        self.assertNotIn(l1a.summary_id, l2.citations)
        row = await self.store._fetchone(
            "SELECT manifest_json FROM summary_nodes WHERE summary_id=?", (l2.summary_id,))
        self.assertEqual([l1a.summary_id, l1b.summary_id],
                         json.loads(row["manifest_json"])["summaries"])
        l3 = await service.compress_l3(self.message.scope_id, [l2])
        self.assertEqual(3, l3.level)
        self.assertTrue(l3.citations)
        self.assertTrue(set(l3.citations).issubset(leaf_ids))

    async def test_pending_starts_at_earliest_range_hole(self) -> None:
        messages = [self.message] + [await self._add(f"m{i}", f"text {i}") for i in range(1, 6)]
        await self.store.store_summary(self.message.scope_id, 1, 1, 2, "first", "{}", [], [x.message_id for x in messages[:2]])
        await self.store.store_summary(self.message.scope_id, 1, 5, 6, "late", "{}", [], [x.message_id for x in messages[4:]])
        captured: list[list[int]] = []
        async def llm(prompt: str) -> str:
            values = json.loads(prompt.split("input=", 1)[1])
            captured.append([item["seq"] for item in values])
            return self._response([values[0]["message_id"]])
        result = await CompressionService(self.store, llm).compress_pending_l1(self.message.scope_id, 2)
        self.assertEqual([3, 4], captured[0])
        self.assertEqual((3, 4), (result.start_seq, result.end_seq))

    async def test_l1_is_resilient_and_still_guards_item_budget(self) -> None:
        """L1 压缩绝不能因弱模型输出而"毒丸"卡死整个 scope：非纯 JSON、空
        citations、引用输入外的证据、facts 写成纯字符串，都应降级成一个合法的
        L1 节点（citations 回填为本批 message_id），让记忆持续形成、压缩水位
        持续推进。只有"单条输入就超预算"这种根本无法压缩的情况才抛错。"""
        valid = json.loads(self._response([self.message.message_id]))
        degraded = [
            "prefix " + json.dumps(valid),                                    # 非纯 JSON（带前缀）
            json.dumps({**valid, "citations": []}),                           # 空引用
            json.dumps({**valid, "facts": [{"text": "x", "evidence_ids": ["fake"]}]}),  # 越界证据
            "not json at all",                                                # 完全不是 JSON
            json.dumps({"facts": ["纯字符串事实"]}),                            # facts 是字符串而非对象
        ]
        for index, raw in enumerate(degraded):
            with self.subTest(raw=raw[:24]):
                msg = await self._add(f"deg-{index}", f"text {index}")
                service = CompressionService(self.store, lambda _p, raw=raw: _async(raw))
                l1 = await service.compress_l1(msg.scope_id, [msg])
                self.assertTrue(l1.title)                                     # 一定有标题
                self.assertTrue(l1.citations)                                 # citations 非空
                self.assertTrue(set(l1.citations).issubset({msg.message_id}))
        huge = await self._add("huge", "x" * 5000)
        with self.assertRaises(ValueError):
            await CompressionService(self.store, lambda _: _async("{}"), input_char_budget=1000).compress_l1(huge.scope_id, [huge])


async def _async(value: str) -> str:
    return value
