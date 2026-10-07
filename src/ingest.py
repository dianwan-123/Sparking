from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import NormalizedMessage, StoredMessage, utc_now
from .storage import Storage


class IngestService:
    """Normalizes host-independent event mappings into the replayable fact store."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    async def ingest(self, message: NormalizedMessage, dedupe_key: str | None = None) -> StoredMessage:
        return await self.storage.ingest_message(message, dedupe_key)

    async def ingest_mapping(self, event: Mapping[str, Any], *, platform: str, account_id: str, conversation_id: str) -> StoredMessage:
        message = self.normalize(event, platform=platform, account_id=account_id, conversation_id=conversation_id)
        event_id = _text(event.get("event_id") or event.get("post_id"))
        return await self.ingest(message, f"event:{event_id}" if event_id else None)

    @staticmethod
    def normalize(event: Mapping[str, Any], *, platform: str, account_id: str, conversation_id: str) -> NormalizedMessage:
        sender = event.get("sender") if isinstance(event.get("sender"), Mapping) else {}
        parts = event.get("parts") if isinstance(event.get("parts"), list) else event.get("message")
        normalized_parts = [dict(item) for item in parts if isinstance(item, Mapping)] if isinstance(parts, list) else []
        text = _text(event.get("text") or event.get("raw_message"))
        if not text and normalized_parts:
            text = "".join(_part_text(item) for item in normalized_parts)
        upstream_id = _text(event.get("message_id"))
        if not upstream_id:
            raise ValueError("event message_id is required")
        return NormalizedMessage(
            platform=platform,
            account_id=account_id,
            conversation_id=conversation_id,
            upstream_message_id=upstream_id,
            sender_id=_text(event.get("user_id") or sender.get("user_id")),
            sender_name=_text(sender.get("card") or sender.get("nickname") or event.get("sender_name")),
            text=text,
            occurred_at=_text(event.get("occurred_at") or event.get("time")) or utc_now(),
            raw_event=dict(event),
            parts=normalized_parts,
            reply_to=_optional_text(event.get("reply_to")),
            event_type=_text(event.get("event_type")) or "message.created",
        )

    async def revise(self, original: NormalizedMessage, *, text: str, raw_event: dict[str, Any], event_id: str | None = None) -> StoredMessage:
        changed = NormalizedMessage(
            platform=original.platform, account_id=original.account_id, conversation_id=original.conversation_id,
            upstream_message_id=original.upstream_message_id, sender_id=original.sender_id,
            sender_name=original.sender_name, text=text, occurred_at=original.occurred_at,
            raw_event=raw_event, parts=original.parts, reply_to=original.reply_to, event_type="message.edited",
        )
        return await self.ingest(changed, f"event:{event_id}" if event_id else None)

    async def recall(self, original: NormalizedMessage, raw_event: dict[str, Any], event_id: str | None = None) -> StoredMessage:
        changed = NormalizedMessage(
            platform=original.platform, account_id=original.account_id, conversation_id=original.conversation_id,
            upstream_message_id=original.upstream_message_id, sender_id=original.sender_id,
            sender_name=original.sender_name, text="", occurred_at=original.occurred_at,
            raw_event=raw_event, parts=[], reply_to=original.reply_to, event_type="message.recalled",
        )
        return await self.ingest(changed, f"event:{event_id}" if event_id else None)


def _part_text(item: Mapping[str, Any]) -> str:
    direct = item.get("text")
    if direct is not None:
        return _text(direct)
    data = item.get("data")
    return _text(data.get("text")) if isinstance(data, Mapping) else ""


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _optional_text(value: Any) -> str | None:
    text = _text(value).strip()
    return text or None
