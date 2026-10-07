from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class NormalizedMessage:
    platform: str
    account_id: str
    conversation_id: str
    upstream_message_id: str
    sender_id: str
    sender_name: str
    text: str
    occurred_at: str
    raw_event: dict[str, Any]
    parts: list[dict[str, Any]] = field(default_factory=list)
    reply_to: str | None = None
    event_type: str = "message.created"


@dataclass(slots=True)
class StoredMessage:
    message_id: str
    revision_id: str
    scope_id: str
    scope_seq: int
    upstream_message_id: str
    sender_id: str
    sender_name: str
    text: str
    occurred_at: str
    parts: list[dict[str, Any]] = field(default_factory=list)
    reply_to: str | None = None


@dataclass(slots=True)
class SearchHit:
    message_id: str
    revision_id: str
    scope_id: str
    sender_id: str
    sender_name: str
    occurred_at: str
    snippet: str
    score: float
    upstream_message_id: str = ""


@dataclass(slots=True)
class SummaryRecord:
    summary_id: str
    scope_id: str
    level: int
    start_seq: int
    end_seq: int
    title: str
    body: str
    topics: list[str]
    citations: list[str]
    created_at: str


@dataclass(slots=True)
class CatalogEntry:
    entry_id: str
    memory_id: str
    scope_id: str
    kind: str
    subject: str
    value: str
    status: str
    confidence: float
    evidence_ids: list[str]
    updated_at: str


Action = Literal[
    "ignore", "text", "sticker", "text_sticker", "reaction", "poke", "agent"
]


@dataclass(slots=True)
class Decision:
    action: Action = "ignore"
    intent: str = ""
    query: str = ""
    reply_to: str | None = None
    emoji_id: str | None = None
    wait_seconds: float = 0.0
    segments: list[dict[str, Any]] | None = None
    # 判定器（LLM）自己决定这条回复是否需要先做需求梳理——模型判断，不是关键词
    needs_plan: bool = False

    @classmethod
    def from_mapping(cls, value: Any, max_wait: float) -> "Decision":
        if not isinstance(value, dict):
            return cls()
        allowed = {"ignore", "text", "sticker", "text_sticker", "reaction", "poke", "agent"}
        action = str(value.get("action", "ignore"))
        if action not in allowed:
            action = "ignore"
        try:
            wait = min(max(float(value.get("wait_seconds", 0)), 0), max_wait)
        except (TypeError, ValueError):
            wait = 0.0
        segments = value.get("segments")
        return cls(
            action=action,
            intent=str(value.get("intent", ""))[:500],
            query=str(value.get("query", ""))[:256],
            reply_to=_optional_str(value.get("reply_to")),
            emoji_id=_optional_str(value.get("emoji_id")),
            wait_seconds=wait,
            segments=segments if isinstance(segments, list) else None,
            needs_plan=bool(value.get("needs_plan")),
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
