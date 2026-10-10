from __future__ import annotations

from astrbot.api import logger

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from .memory_ledger import MemoryLedger
from .models import StoredMessage, SummaryRecord
from .prompts import SUMMARY_SYSTEM_PROMPT
from .storage import Storage

# 市场规范：日志器必须取自 astrbot.api，插件不自建日志器
LLMCallback = Callable[..., Awaitable[str]]
_REQUIRED_ARRAYS = ("topics", "timeline", "facts", "decisions", "tasks", "open_questions", "conflicts", "citations", "memory_proposals")


class CompressionService:
    """Creates immutable L1/L2/L3 nodes from validated strict-JSON LLM output."""

    def __init__(self, storage: Storage, llm: LLMCallback, ledger: MemoryLedger | None = None, model: str = "", input_char_budget: int = 50000) -> None:
        self.storage = storage
        self.llm = llm
        self.ledger = ledger
        self.model = model
        self.input_char_budget = max(1000, int(input_char_budget))

    async def compress_l1(self, scope_id: str, messages: Sequence[StoredMessage]) -> SummaryRecord:
        if not messages:
            raise ValueError("L1 requires messages")
        self._check_messages(scope_id, messages)
        items = [{"message_id": x.message_id, "seq": x.scope_seq, "sender": x.sender_name, "occurred_at": x.occurred_at, "text": x.text} for x in messages]
        return await self._compress(scope_id, 1, messages[0].scope_seq, messages[-1].scope_seq, items, [x.message_id for x in messages], [])

    async def compress_l2(self, scope_id: str, summaries: Sequence[SummaryRecord]) -> SummaryRecord:
        return await self._compress_summaries(scope_id, 2, summaries, {1})

    async def compress_l3(self, scope_id: str, summaries: Sequence[SummaryRecord]) -> SummaryRecord:
        return await self._compress_summaries(scope_id, 3, summaries, {1, 2})

    async def compress_pending_l1(self, scope_id: str, batch_size: int = 40) -> SummaryRecord | None:
        messages = await self.storage.pending_l1_messages(scope_id, batch_size)
        return await self.compress_l1(scope_id, messages) if messages else None

    async def roll_up(self, scope_id: str, batch: int = 5) -> list[SummaryRecord]:
        """Aggregate unconsumed L1 chunks into L2 and L2s into L3 so memory forms continuously."""
        created: list[SummaryRecord] = []
        batch = max(2, int(batch))
        chunks = await self.storage.unconsumed_summaries(scope_id, 1, 2, batch)
        if len(chunks) >= batch:
            created.append(await self.compress_l2(scope_id, chunks))
        episodes = await self.storage.unconsumed_summaries(scope_id, 2, 3, batch)
        if len(episodes) >= batch:
            created.append(await self.compress_l3(scope_id, episodes))
        return created

    async def merge_summaries(self, scope_id: str,
                              summary_ids: "Sequence[str]") -> SummaryRecord:
        """把 bot **自己挑**的若干条同级摘要合并成一条更高层的（L1→L2、L2→L3）。

        这是"压缩"的落地方式（用户要求优先走它）：内容没丢，只是变精炼；
        层级规则跟自动 roll_up 一致——L1 合成 L2、L2 合成 L3；L3 已经是最顶层，
        合并不了（调用方应该改走遗忘）。
        """
        records = await self.storage.get_summaries_by_ids(scope_id, summary_ids)
        if len(records) < 2:
            raise ValueError("压缩至少要两条摘要（少于两条就没什么可压的）")
        levels = {int(item.level) for item in records}
        if len(levels) != 1:
            raise ValueError("只能合并同一层的摘要（L1 和 L2 不能混在一起压）")
        level = levels.pop()
        if level >= 3:
            raise ValueError("L3 已经是最顶层的长期记忆，没法再往上压——"
                             "真的没用就改用遗忘")
        target = level + 1
        return await self._compress_summaries(
            scope_id, target, records, {level})

    async def _compress_summaries(self, scope_id: str, level: int, summaries: Sequence[SummaryRecord], allowed: set[int]) -> SummaryRecord:
        if not summaries or any(x.scope_id != scope_id or x.level not in allowed for x in summaries):
            raise ValueError(f"L{level} inputs must be same-scope lower-level summaries")
        ordered = sorted(summaries, key=lambda x: x.start_seq)
        items = [{"summary_id": x.summary_id, "level": x.level, "range": [x.start_seq, x.end_seq], "title": x.title, "body": x.body, "citations": x.citations, "created_at": x.created_at} for x in ordered]
        citations = list(dict.fromkeys(item for summary in ordered for item in summary.citations))
        return await self._compress(scope_id, level, ordered[0].start_seq, ordered[-1].end_seq, items, citations, [x.summary_id for x in ordered])

    async def _compress(self, scope_id: str, level: int, start: int, end: int, items: Sequence[Mapping[str, Any]], citations: Sequence[str], input_summaries: Sequence[str]) -> SummaryRecord:
        packed_items = _pack_items(items, self.input_char_budget)
        # A summary's stored citations are always leaf message_ids (storage
        # enforces it). For L1 those are the item message_ids; for L2/L3 they
        # are the underlying message_ids each input summary already cites.
        allowed_ids = _ids_in_items(packed_items)
        packed_summary_ids = [str(item["summary_id"]) for item in packed_items if "summary_id" in item]
        packed_start, packed_end = _item_range(packed_items, start, end)
        prompt = f"{SUMMARY_SYSTEM_PROMPT}\nlevel=L{level}\ninput={json.dumps(packed_items, ensure_ascii=False, separators=(',', ':'))}"
        if level == 1:
            # L1 must never poison-pill the scope: a weak model that returns
            # bare-string facts or omits citations used to raise, leaving the
            # batch forever uncovered so no memory ever formed. Parse leniently
            # and fall back to a deterministic summary of the raw messages, so a
            # valid L1 node always lands and the watermark always advances.
            raw = await self._call_llm(prompt, allowed_ids=allowed_ids)
            data = _sanitize_l1(raw, allowed_ids, packed_items)
        else:
            # Aggregation must not hinge on a weak model quoting the right IDs:
            # the leaf citations are derivable, so sanitize leniently and fall
            # back to the deterministic union (fixes "summary citations must be
            # non-empty input IDs" / "evidence is outside the input manifest").
            raw = await self._call_llm(prompt)
            data = _sanitize_aggregate(raw, allowed_ids, sorted(allowed_ids))
        manifest = {"level": level, "messages": sorted(allowed_ids), "summaries": packed_summary_ids, "input_count": len(packed_items)}
        body = json.dumps({key: data[key] for key in _REQUIRED_ARRAYS if key not in {"topics", "citations", "memory_proposals"}}, ensure_ascii=False, separators=(",", ":"))
        # 「这段内容发生在什么时候」：L1 看消息时间，L2/L3 看子摘要覆盖的时间范围。
        # 不传的话 store_summary 会记成"写入时间"，导入的老记录就会被当成刚发生的事。
        happened_at = _happened_at(packed_items)
        record = await self.storage.store_summary(scope_id, level, packed_start, packed_end, str(data["title"])[:500], body, data["topics"], data["citations"], packed_summary_ids, manifest, self.model, occurred_at=happened_at)
        if level == 1 and data["topics"]:
            try:
                await self.storage.upsert_topics(scope_id, data["topics"], packed_end)
            except Exception as error:
                logger.warning("group topics upsert failed: %s", str(error)[:160])
        if self.ledger:
            for index, proposal in enumerate(data["memory_proposals"]):
                try:
                    await self.ledger.apply_proposal(
                        scope_id, proposal, f"summary:{record.summary_id}:{index}",
                        occurred_at=happened_at)
                except ValueError:
                    continue
        return record

    async def _call_llm(self, prompt: str, allowed_ids: set[str] | None = None) -> str:
        """Invoke the summary LLM; with allowed_ids, one validation retry on bad IDs."""
        try:
            parameters = inspect.signature(self.llm).parameters
        except (TypeError, ValueError):
            parameters = {}

        async def _invoke(text: str) -> str:
            return await self.llm(SUMMARY_SYSTEM_PROMPT, text) if len(parameters) >= 2 else await self.llm(text)

        first = await _invoke(prompt)
        if allowed_ids is None:
            return first
        # 首次输出常把引用 ID 弄错（引用了 manifest 外的 id，或干脆没给 citations）——
        # 把错误反馈给模型重试一次，提升 L1 引用质量；但即便重试仍不理想，下游
        # _sanitize_l1 也会回填，绝不再因此丢批。
        if _l1_citations_valid(first, allowed_ids):
            return first
        retry_prompt = (
            f"{prompt}\n\n注意：你上一次输出的 citations/evidence_ids 引用了输入中"
            "不存在的 ID，或没有给出任何有效引用。只能引用输入数据里出现的 message_id"
            "（看输入项的 message_id 字段），citations 必须非空，"
            "facts/decisions/tasks 每项都要是含 text 与 evidence_ids 的对象，请重新输出严格 JSON。"
        )
        return await _invoke(retry_prompt)

    @staticmethod
    def _check_messages(scope_id: str, messages: Sequence[StoredMessage]) -> None:
        if any(x.scope_id != scope_id for x in messages):
            raise ValueError("cross-scope compression is forbidden")


def _validate_response(raw: str, allowed_citations: set[str]) -> dict[str, Any]:
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError as error:
        raise ValueError("LLM must return only one strict JSON object") from error
    if not isinstance(data, dict) or not isinstance(data.get("title"), str):
        raise ValueError("LLM did not return a strict JSON object with title")
    for key in _REQUIRED_ARRAYS:
        if not isinstance(data.get(key), list):
            raise ValueError(f"LLM response field {key} must be an array")
    topics = [str(x)[:200] for x in data["topics"] if isinstance(x, (str, int, float))][:32]
    citations = _evidence_ids(data["citations"], "citations")
    if not citations or not set(citations).issubset(allowed_citations):
        raise ValueError("summary citations must be non-empty input IDs")
    for key in ("facts", "decisions", "tasks"):
        for item in data[key]:
            if not isinstance(item, dict):
                raise ValueError(f"{key} entries must be objects with evidence_ids")
            evidence = _evidence_ids(item.get("evidence_ids"), f"{key}.evidence_ids")
            if not set(evidence).issubset(allowed_citations):
                raise ValueError(f"{key} evidence is outside the input manifest")
    proposals: list[dict[str, Any]] = []
    for value in data["memory_proposals"]:
        if not isinstance(value, dict):
            raise ValueError("memory proposals must be objects")
        evidence = _evidence_ids(value.get("evidence_ids"), "memory_proposals.evidence_ids")
        if not set(evidence).issubset(allowed_citations):
            raise ValueError("memory proposal evidence is outside the input manifest")
        proposal = dict(value)
        proposal["evidence_ids"] = evidence
        proposals.append(proposal)
    data["topics"], data["citations"], data["memory_proposals"] = topics, citations, proposals
    return data


def _evidence_ids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    ids = list(dict.fromkeys(str(item) for item in value if str(item)))
    if not ids:
        raise ValueError(f"{field} must not be empty")
    return ids


def _clean_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item) for item in value if str(item)))


def _sanitize_aggregate(raw: str, allowed: set[str], inherited: Sequence[str]) -> dict[str, Any]:
    """Lenient L2/L3 validation. The prose comes from the model; the leaf
    citations are authoritative and derivable, so we never reject an aggregate
    just because a weak model quoted summary_ids instead of message_ids —
    invalid references are filtered and back-filled from ``inherited``."""
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError as error:
        raise ValueError("LLM must return only one strict JSON object") from error
    if not isinstance(data, dict) or not isinstance(data.get("title"), str):
        raise ValueError("LLM did not return a strict JSON object with title")
    for key in _REQUIRED_ARRAYS:
        if not isinstance(data.get(key), list):
            data[key] = []
    topics = [str(x)[:200] for x in data["topics"] if isinstance(x, (str, int, float))][:32]
    fallback = [str(x) for x in inherited if str(x) in allowed]
    citations = [c for c in _clean_ids(data["citations"]) if c in allowed] or fallback
    citations = list(dict.fromkeys(citations))
    if not citations:
        raise ValueError("summary citations must be non-empty input IDs")

    def _fix(items: Any) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            evidence = [e for e in _clean_ids(item.get("evidence_ids")) if e in allowed] or citations[:1]
            fixed = dict(item)
            fixed["evidence_ids"] = evidence
            cleaned.append(fixed)
        return cleaned

    data["facts"], data["decisions"], data["tasks"] = _fix(data["facts"]), _fix(data["decisions"]), _fix(data["tasks"])
    data["topics"], data["citations"], data["memory_proposals"] = topics, citations, _fix(data["memory_proposals"])
    return data


def _l1_citations_valid(raw: str, allowed: set[str]) -> bool:
    """Cheap gate for the one L1 retry: did the model cite at least one real
    message_id? Non-JSON or all-foreign citations trigger a single reprompt."""
    try:
        data = json.loads(raw.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or not isinstance(data.get("citations"), list):
        return False
    return any(str(item) in allowed for item in data["citations"])


def _entry_text(item: Any) -> str:
    """Pull a human-readable line out of a fact/decision/task entry that may be
    an object (various key names) or a bare string a weak model emitted."""
    if isinstance(item, dict):
        for key in ("text", "fact", "decision", "task", "value", "summary", "detail"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(item, (str, int, float)):
        return str(item).strip()
    return ""


def _fallback_title(items: Sequence[Mapping[str, Any]]) -> str:
    """Deterministic L1 title when the model gives none: never leave it blank."""
    senders: list[str] = []
    for item in items:
        name = str(item.get("sender") or "").strip()
        if name and name not in senders:
            senders.append(name)
        if len(senders) >= 3:
            break
    first_text = ""
    for item in items:
        text = str(item.get("text") or "").strip()
        if text:
            first_text = text[:40]
            break
    who = "、".join(senders) if senders else "群友"
    head = f"{who}的{len(items)}条消息"
    return (f"{head}：{first_text}" if first_text else head)[:200]


def _sanitize_l1(raw: str, allowed: set[str], items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Lenient L1 validation with a deterministic fallback. The prose is the
    model's; the citations are authoritative message_ids we own. We never raise:
    a broken or empty LLM response still yields a valid, storable L1 node (with
    a synthesized title and full-batch citations), so memory keeps forming and
    the scope's compression watermark always advances."""
    try:
        data = json.loads(raw.strip())
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    for key in _REQUIRED_ARRAYS:
        if not isinstance(data.get(key), list):
            data[key] = []
    topics = [str(x)[:200] for x in data["topics"] if isinstance(x, (str, int, float))][:32]
    batch_ids = [str(item.get("message_id")) for item in items if item.get("message_id")]
    # L1 summarizes every message in the batch, so citing all of them is always
    # correct — that is the back-fill when the model cites nothing valid.
    citations = [c for c in _clean_ids(data["citations"]) if c in allowed]
    citations = list(dict.fromkeys(citations)) or batch_ids
    if not citations:
        raise ValueError("L1 batch carried no message ids")

    def _fix(entries: Any) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for item in entries if isinstance(entries, list) else []:
            text = _entry_text(item)
            if not text:
                continue
            evidence = [
                e for e in _clean_ids(item.get("evidence_ids") if isinstance(item, dict) else None)
                if e in allowed
            ] or citations[:1]
            fixed = dict(item) if isinstance(item, dict) else {}
            fixed["text"] = text[:600]
            fixed["evidence_ids"] = evidence
            cleaned.append(fixed)
        return cleaned

    title = str(data.get("title") or "").strip() or _fallback_title(items)
    data["facts"], data["decisions"], data["tasks"] = _fix(data["facts"]), _fix(data["decisions"]), _fix(data["tasks"])
    proposals: list[dict[str, Any]] = []
    for value in data["memory_proposals"]:
        if not isinstance(value, dict):
            continue
        evidence = [e for e in _clean_ids(value.get("evidence_ids")) if e in allowed] or citations[:1]
        proposal = dict(value)
        proposal["evidence_ids"] = evidence
        proposals.append(proposal)
    data["title"] = title[:500]
    data["topics"], data["citations"], data["memory_proposals"] = topics, citations, proposals
    return data



def _pack_items(items: Sequence[Mapping[str, Any]], budget: int) -> list[dict[str, Any]]:
    packed: list[dict[str, Any]] = []
    for item in items:
        candidate = [*packed, dict(item)]
        if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) > budget:
            break
        packed = candidate
    if not packed:
        raise ValueError("first compression item exceeds input budget")
    return packed


def _ids_in_items(items: Sequence[Mapping[str, Any]]) -> set[str]:
    """Message IDs the aggregate may cite: L1 items carry them as `message_id`,
    L2/L3 items carry the underlying ones inside each summary's `citations`."""
    result: set[str] = set()
    for item in items:
        if item.get("message_id"):
            result.add(str(item["message_id"]))
        result.update(str(value) for value in item.get("citations", []) if str(value))
    return result


def _happened_at(items: Sequence[Mapping[str, Any]]) -> str:
    """这段内容**什么时候发生的**：取输入项里最早的时间（L1 是消息、L2/L3 是子摘要）。

    取"最早"而不是"最晚"：一条记忆说的是"从那时开始发生的事"，配上时间纪律里
    "先跟 now 比"，最早的锚点最不容易把旧事说成新事。
    """
    stamps: list[str] = []
    for item in items:
        for key in ("occurred_at", "at", "created_at"):
            value = str(item.get(key) or "").strip()
            if value:
                stamps.append(value)
                break
    if not stamps:
        return ""
    try:
        return min(stamps)
    except Exception:
        return stamps[0]


def _item_range(items: Sequence[Mapping[str, Any]], fallback_start: int, fallback_end: int) -> tuple[int, int]:
    first, last = items[0], items[-1]
    start = int(first.get("seq", first.get("range", [fallback_start])[0]))
    last_range = last.get("range", [fallback_end, fallback_end])
    end = int(last.get("seq", last_range[-1]))
    return start, end

