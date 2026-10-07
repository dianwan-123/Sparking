from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import SearchHit, StoredMessage
from .storage import Storage, _escape_like, _limit


class EmbeddingAdapter(Protocol):
    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


@dataclass(slots=True)
class RankedId:
    item_id: str
    score: float


class RetrievalService:
    def __init__(self, storage: Storage, embedding: EmbeddingAdapter | Callable[[Sequence[str]], Awaitable[Sequence[Sequence[float]]]] | None = None, embedding_model: str = "default") -> None:
        self.storage = storage
        self.embedding = embedding
        self.embedding_model = embedding_model

    async def recent(self, scope_id: str, limit: int = 30, before_seq: int | None = None, include_events: bool = False) -> list[StoredMessage]:
        return await self.storage.recent_messages(scope_id, limit, before_seq, include_events=include_events)

    async def by_ids(self, scope_id: str, message_ids: Sequence[str], limit: int = 8) -> list[StoredMessage]:
        return await self.storage.get_messages(scope_id, message_ids, limit)

    async def context(self, scope_id: str, message_id: str, radius: int = 5) -> list[StoredMessage]:
        return await self.storage.chat_context(scope_id, message_id, radius)

    async def search(self, scope_id: str | Sequence[str], query: str, limit: int = 20, sender_id: str | None = None, start: str | None = None, end: str | None = None, semantic: bool = False) -> list[SearchHit]:
        query = query.strip()[:512]
        if not query:
            return []
        scopes = tuple(dict.fromkeys([scope_id] if isinstance(scope_id, str) else [s for s in scope_id if s]))
        if not scopes:
            return []
        lexical = await self._lexical(scopes, query, min(_limit(limit, 50) * 3, 100), sender_id, start, end)
        if not semantic or self.embedding is None:
            return lexical[: _limit(limit, 50)]
        try:
            semantic_hits = await self._semantic(scopes, query, min(_limit(limit, 50) * 3, 100), sender_id, start, end)
            ranking = reciprocal_rank_fusion([[RankedId(x.message_id, x.score) for x in lexical], [RankedId(x.message_id, x.score) for x in semantic_hits]])
            by_id = {x.message_id: x for x in lexical + semantic_hits}
            return [SearchHit(by_id[x.item_id].message_id, by_id[x.item_id].revision_id, by_id[x.item_id].scope_id, by_id[x.item_id].sender_id, by_id[x.item_id].sender_name, by_id[x.item_id].occurred_at, by_id[x.item_id].snippet, x.score, by_id[x.item_id].upstream_message_id) for x in ranking[: _limit(limit, 50)]]
        except Exception:
            return lexical[: _limit(limit, 50)]

    async def index_embedding(self, scope_id: str, message: StoredMessage) -> bool:
        if message.scope_id != scope_id:
            raise ValueError("message does not belong to scope")
        if self.embedding is None:
            return False
        vectors = await self._embed([message.text])
        if not vectors or not vectors[0]:
            return False
        await self.storage.save_embedding(scope_id, message.message_id, self.embedding_model, vectors[0])
        return True

    async def _lexical(self, scope_id: str | Sequence[str], query: str, limit: int, sender_id: str | None, start: str | None, end: str | None) -> list[SearchHit]:
        scopes = tuple(dict.fromkeys([scope_id] if isinstance(scope_id, str) else [s for s in scope_id if s]))
        filters, args = [f"mi.scope_id IN ({','.join('?' for _ in scopes)})", "mc.is_deleted=0"], [*scopes]
        if sender_id:
            filters.append("mr.sender_id=?")
            args.append(sender_id)
        if start:
            filters.append("mr.occurred_at>=?")
            args.append(start)
        if end:
            filters.append("mr.occurred_at<=?")
            args.append(end)
        where = " AND ".join(filters)
        rows = []
        if self.storage.fts5_available:
            tokens = _fts_query(query)
            if tokens:
                sql = "SELECT mi.message_id,mi.upstream_message_id,mr.revision_id,mr.sender_id,mr.sender_name,mr.occurred_at,mr.text,bm25(message_fts) AS rank FROM message_fts JOIN message_identities mi ON mi.message_id=message_fts.message_id JOIN message_current mc ON mc.message_id=mi.message_id JOIN message_revisions mr ON mr.revision_id=mc.revision_id WHERE message_fts MATCH ? AND " + where + " ORDER BY rank LIMIT ?"
                try:
                    rows = await self.storage._fetchall(sql, (tokens, *args, limit))
                except Exception:
                    rows = []
        if not rows:
            patterns = ["%" + _escape_like(token) + "%" for token in _keyword_terms(query)]
            clauses = " OR ".join("mr.text LIKE ? ESCAPE '\\'" for _ in patterns)
            sql = "SELECT mi.message_id,mi.upstream_message_id,mr.revision_id,mr.sender_id,mr.sender_name,mr.occurred_at,mr.text,0.0 AS rank FROM message_current mc JOIN message_identities mi ON mi.message_id=mc.message_id JOIN message_revisions mr ON mr.revision_id=mc.revision_id WHERE " + where + " AND (" + clauses + ") ORDER BY mc.scope_seq DESC LIMIT ?"
            rows = await self.storage._fetchall(sql, (*args, *patterns, limit))
        return [SearchHit(str(row["message_id"]), str(row["revision_id"]), scope_id, str(row["sender_id"]), str(row["sender_name"]), str(row["occurred_at"]), _snippet(str(row["text"]), query), 1.0 / (index + 1), str(row["upstream_message_id"])) for index, row in enumerate(rows)]

    async def _semantic(self, scope_id: str | Sequence[str], query: str, limit: int, sender_id: str | None, start: str | None, end: str | None) -> list[SearchHit]:
        scopes = tuple(dict.fromkeys([scope_id] if isinstance(scope_id, str) else [s for s in scope_id if s]))
        vectors = await self._embed([query])
        if not vectors or not vectors[0]:
            return []
        target = [float(x) for x in vectors[0]]
        conditions, args = [f"e.scope_id IN ({','.join('?' for _ in scopes)})", "e.owner_type='message'", "e.model=?", "mc.is_deleted=0"], [*scopes, self.embedding_model]
        if sender_id:
            conditions.append("mr.sender_id=?")
            args.append(sender_id)
        if start:
            conditions.append("mr.occurred_at>=?")
            args.append(start)
        if end:
            conditions.append("mr.occurred_at<=?")
            args.append(end)
        rows = await self.storage._fetchall("SELECT e.owner_id AS message_id,e.scope_id AS hit_scope,e.dimensions,e.vector_json,mr.revision_id,mr.sender_id,mr.sender_name,mr.occurred_at,mr.text FROM embeddings e JOIN message_identities mi ON mi.message_id=e.owner_id AND mi.scope_id=e.scope_id JOIN message_current mc ON mc.message_id=mi.message_id JOIN message_revisions mr ON mr.revision_id=mc.revision_id WHERE " + " AND ".join(conditions), args)
        scored = []
        for row in rows:
            vector = json.loads(row["vector_json"])
            if len(vector) != len(target):
                continue
            score = _cosine(target, vector)
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [SearchHit(str(row["message_id"]), str(row["revision_id"]), str(row["hit_scope"]), str(row["sender_id"]), str(row["sender_name"]), str(row["occurred_at"]), _snippet(str(row["text"]), query), score, str(row["message_id"])) for score, row in scored[:limit]]

    async def _embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if self.embedding is None:
            return []
        method = getattr(self.embedding, "embed", None)
        return await method(texts) if method else await self.embedding(texts)  # type: ignore[misc]


def reciprocal_rank_fusion(rankings: Sequence[Sequence[RankedId]], k: int = 60) -> list[RankedId]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item.item_id] = scores.get(item.item_id, 0.0) + 1.0 / (k + rank)
    return [RankedId(item_id, score) for item_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def _fts_query(query: str) -> str:
    cleaned = query.replace('"', " ").strip()
    if not cleaned:
        return ""
    tokens = [part for part in cleaned.split() if part]
    return " AND ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:20])


def _keyword_terms(query: str) -> list[str]:
    cleaned = query.replace('"', " ").strip()
    words = [part for part in cleaned.split() if part]
    if len(words) > 1 or not cleaned:
        return words[:20]
    word = words[0]
    if any("\u4e00" <= char <= "\u9fff" for char in word) and len(word) > 2:
        return list(dict.fromkeys([word, *(word[index:index + 2] for index in range(len(word) - 1))]))[:20]
    return [word]


def _snippet(text: str, query: str, width: int = 240) -> str:
    index = text.casefold().find(query.casefold())
    start = max(index - width // 3, 0) if index >= 0 else 0
    value = text[start:start + width]
    return ("…" if start else "") + value + ("…" if start + width < len(text) else "")


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    return dot / denominator if denominator else 0.0
