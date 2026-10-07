from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .models import CatalogEntry, utc_now
from .storage import Storage, _json, _limit

_ALLOWED_KINDS = {"fact", "preference", "relationship", "rule", "decision", "task", "conflict", "topic", "joke"}
_ALLOWED_STATUS = {"active", "disputed", "superseded", "resolved", "invalid"}


class MemoryLedger:
    """Validates and persists LLM memory proposals; proposals never write unchecked."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    async def apply_proposal(self, scope_id: str, proposal: Mapping[str, Any], idempotency_key: str | None = None) -> CatalogEntry:
        kind = str(proposal.get("kind", "")).strip().lower()
        subject = str(proposal.get("subject", "")).strip()[:256]
        value = str(proposal.get("value", "")).strip()[:8000]
        status = str(proposal.get("status", "active")).strip().lower()
        evidence = list(dict.fromkeys(str(x) for x in proposal.get("evidence_ids", []) if str(x)))[:32]
        if kind not in _ALLOWED_KINDS or not subject or not value:
            raise ValueError("proposal requires an allowed kind, subject, and value")
        if status not in _ALLOWED_STATUS:
            raise ValueError("invalid memory status")
        try:
            confidence = min(max(float(proposal.get("confidence", 0.5)), 0.0), 1.0)
        except (TypeError, ValueError) as error:
            raise ValueError("confidence must be numeric") from error
        if not evidence:
            raise ValueError("memory proposal requires evidence")
        await self.storage._validate_owned(scope_id, "message_identities", "message_id", evidence)
        db = self.storage._conn()
        async with self.storage._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                existing = await self.storage._fetchone("SELECT memory_id FROM memory_items WHERE scope_id=? AND idempotency_key=?", (scope_id, idempotency_key)) if idempotency_key else None
                if existing:
                    await db.rollback()
                    found = await self.get(scope_id, str(existing["memory_id"]))
                    if found is None:
                        raise RuntimeError("memory projection missing")
                    return found
                memory_id, revision_id, entry_id, now = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex, utc_now()
                await db.execute("INSERT INTO memory_items VALUES(?,?,?,?,?,?,?,?,?,?)", (memory_id, scope_id, kind, subject, status, confidence, revision_id, idempotency_key, now, now))
                await db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?)", (revision_id, memory_id, 1, value, "create", now))
                await db.executemany("INSERT INTO memory_evidence VALUES(?,?)", [(memory_id, item) for item in evidence])
                await db.execute("INSERT INTO catalog_entries VALUES(?,?,?,?,?,?,?,?,?,?)", (entry_id, memory_id, scope_id, kind, subject, value, status, confidence, _json(evidence), now))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return CatalogEntry(entry_id, memory_id, scope_id, kind, subject, value, status, confidence, evidence, now)

    async def revise(self, scope_id: str, memory_id: str, value: str, evidence_ids: Sequence[str], status: str = "active") -> CatalogEntry:
        if status not in _ALLOWED_STATUS or not value.strip() or not evidence_ids:
            raise ValueError("revision requires valid value, status, and evidence")
        evidence = list(dict.fromkeys(evidence_ids))[:32]
        await self.storage._validate_owned(scope_id, "message_identities", "message_id", evidence)
        current = await self.storage._fetchone("SELECT * FROM memory_items WHERE scope_id=? AND memory_id=?", (scope_id, memory_id))
        if current is None:
            raise KeyError(memory_id)
        db, now = self.storage._conn(), utc_now()
        async with self.storage._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await self.storage._fetchone("SELECT COALESCE(MAX(revision_no),0)+1 AS n FROM memory_revisions WHERE memory_id=?", (memory_id,))
                revision_id = uuid.uuid4().hex
                await db.execute("INSERT INTO memory_revisions VALUES(?,?,?,?,?,?)", (revision_id, memory_id, int(row["n"]), value[:8000], "update", now))
                await db.execute("UPDATE memory_items SET status=?,current_revision_id=?,updated_at=? WHERE memory_id=?", (status, revision_id, now, memory_id))
                await db.execute("DELETE FROM memory_evidence WHERE memory_id=?", (memory_id,))
                await db.executemany("INSERT INTO memory_evidence VALUES(?,?)", [(memory_id, item) for item in evidence])
                await db.execute("UPDATE catalog_entries SET value=?,status=?,evidence_json=?,updated_at=? WHERE memory_id=?", (value[:8000], status, _json(evidence), now, memory_id))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        result = await self.get(scope_id, memory_id)
        if result is None:
            raise RuntimeError("memory projection missing")
        return result

    async def catalog(self, scope_id: str | Sequence[str], limit: int = 24, kinds: Sequence[str] | None = None, query: str | None = None) -> list[CatalogEntry]:
        scopes = [scope_id] if isinstance(scope_id, str) else [s for s in scope_id if s]
        if not scopes:
            return []
        clauses, args = [f"scope_id IN ({','.join('?' for _ in scopes)})", "status!='invalid'"], [*scopes]
        valid_kinds = [x for x in (kinds or ()) if x in _ALLOWED_KINDS]
        if valid_kinds:
            clauses.append("kind IN (" + ",".join("?" for _ in valid_kinds) + ")")
            args.extend(valid_kinds)
        if query:
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("(subject LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\')")
            args.extend([pattern, pattern])
        rows = await self.storage._fetchall("SELECT * FROM catalog_entries WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT ?", (*args, _limit(limit, 100)))
        return [_entry(row) for row in rows]

    async def get(self, scope_id: str, memory_id: str) -> CatalogEntry | None:
        row = await self.storage._fetchone("SELECT * FROM catalog_entries WHERE scope_id=? AND memory_id=?", (scope_id, memory_id))
        return _entry(row) if row else None


def _entry(row: Any) -> CatalogEntry:
    import json
    return CatalogEntry(str(row["entry_id"]), str(row["memory_id"]), str(row["scope_id"]), str(row["kind"]), str(row["subject"]), str(row["value"]), str(row["status"]), float(row["confidence"]), json.loads(row["evidence_json"]), str(row["updated_at"]))
