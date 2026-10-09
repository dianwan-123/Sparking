from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .models import CatalogEntry, NormalizedMessage, StoredMessage, SummaryRecord, utc_now

SCHEMA_VERSION = 1

# 各表的真实列名（导入记忆时用来过滤包里多余的字段；open() 时用 PRAGMA 预热）
_TABLE_COLUMNS: dict[str, frozenset] = {}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scopes(scope_id TEXT PRIMARY KEY,platform TEXT NOT NULL,account_id TEXT NOT NULL,conversation_id TEXT NOT NULL,display_name TEXT NOT NULL DEFAULT '',tag TEXT NOT NULL DEFAULT '',next_seq INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,UNIQUE(platform,account_id,conversation_id));
CREATE TABLE IF NOT EXISTS event_headers(event_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,scope_seq INTEGER NOT NULL,event_type TEXT NOT NULL,upstream_message_id TEXT NOT NULL,sender_id TEXT NOT NULL,occurred_at TEXT NOT NULL,dedupe_key TEXT NOT NULL,payload_hash TEXT NOT NULL,schema_version INTEGER NOT NULL DEFAULT 1,ingested_at TEXT NOT NULL,UNIQUE(scope_id,scope_seq),UNIQUE(scope_id,dedupe_key));
CREATE TABLE IF NOT EXISTS event_payloads(event_id TEXT PRIMARY KEY REFERENCES event_headers(event_id) ON DELETE CASCADE,payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS message_identities(message_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,upstream_message_id TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(scope_id,upstream_message_id));
CREATE TABLE IF NOT EXISTS message_revisions(revision_id TEXT PRIMARY KEY,message_id TEXT NOT NULL REFERENCES message_identities(message_id) ON DELETE CASCADE,event_id TEXT NOT NULL REFERENCES event_headers(event_id) ON DELETE CASCADE,revision_no INTEGER NOT NULL,sender_id TEXT NOT NULL,sender_name TEXT NOT NULL,text TEXT NOT NULL,parts_json TEXT NOT NULL,reply_to TEXT,occurred_at TEXT NOT NULL,is_deleted INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,UNIQUE(message_id,revision_no));
CREATE TABLE IF NOT EXISTS message_current(message_id TEXT PRIMARY KEY REFERENCES message_identities(message_id) ON DELETE CASCADE,revision_id TEXT NOT NULL REFERENCES message_revisions(revision_id) ON DELETE CASCADE,scope_seq INTEGER NOT NULL,is_deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS threads(thread_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,root_message_id TEXT REFERENCES message_identities(message_id) ON DELETE SET NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS episodes(episode_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,start_seq INTEGER NOT NULL,end_seq INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'open',topic TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS summary_nodes(summary_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),start_seq INTEGER NOT NULL,end_seq INTEGER NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,topics_json TEXT NOT NULL,manifest_json TEXT NOT NULL,model TEXT NOT NULL DEFAULT '',prompt_version TEXT NOT NULL DEFAULT '1',status TEXT NOT NULL DEFAULT 'active',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS summary_inputs(summary_id TEXT NOT NULL REFERENCES summary_nodes(summary_id) ON DELETE CASCADE,input_message_id TEXT REFERENCES message_identities(message_id) ON DELETE CASCADE,input_summary_id TEXT REFERENCES summary_nodes(summary_id) ON DELETE CASCADE,CHECK((input_message_id IS NOT NULL)!=(input_summary_id IS NOT NULL)),UNIQUE(summary_id,input_message_id,input_summary_id));
CREATE TABLE IF NOT EXISTS summary_citations(summary_id TEXT NOT NULL REFERENCES summary_nodes(summary_id) ON DELETE CASCADE,message_id TEXT NOT NULL REFERENCES message_identities(message_id) ON DELETE CASCADE,quote TEXT NOT NULL DEFAULT '',PRIMARY KEY(summary_id,message_id));
CREATE TABLE IF NOT EXISTS memory_items(memory_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,kind TEXT NOT NULL,subject TEXT NOT NULL,status TEXT NOT NULL,confidence REAL NOT NULL,current_revision_id TEXT,idempotency_key TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(scope_id,idempotency_key));
CREATE TABLE IF NOT EXISTS memory_revisions(memory_revision_id TEXT PRIMARY KEY,memory_id TEXT NOT NULL REFERENCES memory_items(memory_id) ON DELETE CASCADE,revision_no INTEGER NOT NULL,value TEXT NOT NULL,operation TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(memory_id,revision_no));
CREATE TABLE IF NOT EXISTS memory_evidence(memory_id TEXT NOT NULL REFERENCES memory_items(memory_id) ON DELETE CASCADE,message_id TEXT NOT NULL REFERENCES message_identities(message_id) ON DELETE CASCADE,PRIMARY KEY(memory_id,message_id));
CREATE TABLE IF NOT EXISTS catalog_entries(entry_id TEXT PRIMARY KEY,memory_id TEXT NOT NULL UNIQUE REFERENCES memory_items(memory_id) ON DELETE CASCADE,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,kind TEXT NOT NULL,subject TEXT NOT NULL,value TEXT NOT NULL,status TEXT NOT NULL,confidence REAL NOT NULL,evidence_json TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY,scope_id TEXT REFERENCES scopes(scope_id) ON DELETE CASCADE,kind TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,lease_owner TEXT,lease_until TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS embeddings(embedding_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,owner_type TEXT NOT NULL,owner_id TEXT NOT NULL,model TEXT NOT NULL,dimensions INTEGER NOT NULL,vector_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(scope_id,owner_type,owner_id,model));
CREATE TABLE IF NOT EXISTS stickers(sticker_id TEXT PRIMARY KEY,sha256 TEXT NOT NULL UNIQUE,path TEXT NOT NULL,mime_type TEXT NOT NULL,size_bytes INTEGER NOT NULL,metadata_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sticker_sources(sticker_id TEXT NOT NULL REFERENCES stickers(sticker_id) ON DELETE CASCADE,message_id TEXT NOT NULL REFERENCES message_identities(message_id) ON DELETE CASCADE,PRIMARY KEY(sticker_id,message_id));
CREATE TABLE IF NOT EXISTS interactions(interaction_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,user_id TEXT,action TEXT NOT NULL,message_id TEXT,occurred_at TEXT NOT NULL,metadata_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_logs(audit_id TEXT PRIMARY KEY,scope_id TEXT REFERENCES scopes(scope_id) ON DELETE SET NULL,actor_id TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,result TEXT NOT NULL,metadata_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS group_style_rules(rule_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,situation TEXT NOT NULL,style TEXT NOT NULL,evidence_message_id TEXT NOT NULL DEFAULT '',hits INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(scope_id,situation,style));
CREATE TABLE IF NOT EXISTS group_lexicon(term_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,term TEXT NOT NULL,meaning TEXT NOT NULL DEFAULT '',confidence REAL NOT NULL DEFAULT 0.5,evidence_message_id TEXT NOT NULL DEFAULT '',first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,uses INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL,UNIQUE(scope_id,term));
CREATE TABLE IF NOT EXISTS person_profiles(person_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,display_name TEXT NOT NULL DEFAULT '',points_json TEXT NOT NULL DEFAULT '[]',know_counts INTEGER NOT NULL DEFAULT 0,first_known TEXT NOT NULL,last_known TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mood_events(event_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,user_id TEXT NOT NULL DEFAULT '',valence REAL NOT NULL DEFAULT 0,arousal REAL NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '',message_id TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prompt_injections(injection_id TEXT PRIMARY KEY,name TEXT NOT NULL,content TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,builtin INTEGER NOT NULL DEFAULT 0,sort_order INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_affinity(scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,user_id TEXT NOT NULL,warmth REAL NOT NULL DEFAULT 50,note TEXT NOT NULL DEFAULT '',interactions INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,PRIMARY KEY(scope_id,user_id));
CREATE TABLE IF NOT EXISTS group_topics(topic_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,title TEXT NOT NULL,hits INTEGER NOT NULL DEFAULT 1,last_seq INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,UNIQUE(scope_id,title));
CREATE INDEX IF NOT EXISTS idx_current_scope_seq ON message_current(scope_seq);
CREATE TABLE IF NOT EXISTS agent_plans(plan_id TEXT PRIMARY KEY,due_at TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'custom',detail TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL,executed_at TEXT NOT NULL DEFAULT '',note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_agent_plans_due ON agent_plans(status,due_at);
CREATE TABLE IF NOT EXISTS user_impressions(scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,user_id TEXT NOT NULL,display_name TEXT NOT NULL DEFAULT '',impression TEXT NOT NULL DEFAULT '',tags_json TEXT NOT NULL DEFAULT '[]',first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(scope_id,user_id));
CREATE TABLE IF NOT EXISTS user_styles(scope_id TEXT NOT NULL REFERENCES scopes(scope_id) ON DELETE CASCADE,user_id TEXT NOT NULL,display_name TEXT NOT NULL DEFAULT '',style_summary TEXT NOT NULL DEFAULT '',catchphrases_json TEXT NOT NULL DEFAULT '[]',sample_lines_json TEXT NOT NULL DEFAULT '[]',msg_count INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,PRIMARY KEY(scope_id,user_id));
CREATE TABLE IF NOT EXISTS standing_intents(intent_id TEXT PRIMARY KEY,scope_id TEXT REFERENCES scopes(scope_id) ON DELETE CASCADE,instruction TEXT NOT NULL,keywords_json TEXT NOT NULL,remaining INTEGER NOT NULL DEFAULT -1,cooldown_seconds INTEGER NOT NULL DEFAULT 0,last_fired TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'active',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_todos(todo_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,content TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',position INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_agent_todos_scope ON agent_todos(scope_id,status,position);
CREATE TABLE IF NOT EXISTS usage_stats(day TEXT NOT NULL,scope_id TEXT NOT NULL DEFAULT '',calls INTEGER NOT NULL DEFAULT 0,prompt_tokens INTEGER NOT NULL DEFAULT 0,completion_tokens INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,PRIMARY KEY(day,scope_id));
CREATE INDEX IF NOT EXISTS idx_revision_sender_time ON message_revisions(sender_id,occurred_at);
CREATE INDEX IF NOT EXISTS idx_summary_scope_level ON summary_nodes(scope_id,level,status,start_seq,end_seq);
CREATE INDEX IF NOT EXISTS idx_catalog_scope_updated ON catalog_entries(scope_id,updated_at);
"""


class DeletionResult(int):
    """Deleted message count with media files that the caller should unlink."""

    media_paths: tuple[Path, ...]

    def __new__(cls, count: int, media_paths: Sequence[str | Path] = ()) -> "DeletionResult":
        value = int.__new__(cls, count)
        value.media_paths = tuple(Path(item) for item in media_paths)
        return value


class Storage:
    """Async SQLite fact store. Scope checks are mandatory on all read paths."""

    def __init__(self, path: str | Path, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.db: aiosqlite.Connection | None = None
        self.fts5_available = False
        self._write_lock = asyncio.Lock()

    async def open(self) -> "Storage":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None (autocommit): implicit transactions never linger
        # on the shared connection. In legacy mode a stray DML outside the write
        # lock left an open transaction and every later `BEGIN IMMEDIATE`
        # failed with "cannot start a transaction within a transaction" —
        # which silently killed compression AND every WebUI delete. All
        # multi-statement paths here use explicit BEGIN IMMEDIATE; single
        # statements become individually atomic, which is what they always
        # meant. `commit()` after that is a harmless no-op.
        self.db = await aiosqlite.connect(self.path, isolation_level=None)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute(f"PRAGMA busy_timeout={max(0, int(self.busy_timeout_ms))}")
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")
        await self._migrate()
        await self._add_scope_tag_column()
        await self._load_table_columns()
        return self

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    async def __aenter__(self) -> "Storage":
        return await self.open()

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _begin_write(self, db: aiosqlite.Connection) -> None:
        """开一个写事务（调用方必须已持有 self._write_lock）。

        防御：若上一个路径在异常分支漏了收尾、连接上还挂着事务，先回滚掉——
        否则后面每一次写都会以 "cannot start a transaction within a transaction"
        连环失败（实录：用户日志里 ingest_message 整条炸穿，消息直接不入库）。
        """
        if getattr(db, "in_transaction", False):
            await db.rollback()
        await db.execute("BEGIN IMMEDIATE")

    def _conn(self) -> aiosqlite.Connection:
        if self.db is None:
            raise RuntimeError("storage is not open")
        return self.db

    async def _migrate(self) -> None:
        db = self._conn()
        version_row = await self._fetchone("PRAGMA user_version")
        current_version = int(version_row[0]) if version_row else 0
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {current_version} is newer than supported {SCHEMA_VERSION}")
        async with self._write_lock:
            await self._begin_write(db)
            try:
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        await db.execute(statement)
                try:
                    await db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(message_id UNINDEXED,scope_id UNINDEXED,text,tokenize='unicode61')")
                    self.fts5_available = True
                except (aiosqlite.OperationalError, sqlite3.OperationalError):
                    self.fts5_available = False
                    await db.execute("CREATE TABLE IF NOT EXISTS message_search(message_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,text TEXT NOT NULL)")
                await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def get_or_create_scope(self, platform: str, account_id: str, conversation_id: str, display_name: str = "") -> str:
        async with self._write_lock:
            scope_id = await self._ensure_scope(platform, account_id, conversation_id, display_name)
            await self._conn().commit()
            return scope_id

    async def _ensure_scope(self, platform: str, account_id: str, conversation_id: str, display_name: str = "") -> str:
        db = self._conn()
        scope_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{platform}\0{account_id}\0{conversation_id}").hex
        await db.execute(
            # 名字只在拿得到时更新：入库链路常带空 display_name，直接覆盖会把
            # 群名抹掉（实录：控制台会话列表只剩号码、按群名找不到会话）。
            "INSERT INTO scopes(scope_id,platform,account_id,conversation_id,display_name,created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(platform,account_id,conversation_id) DO UPDATE SET display_name="
            "CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE scopes.display_name END",
            (scope_id, platform, account_id, conversation_id, display_name, utc_now()),
        )
        row = await self._fetchone("SELECT scope_id FROM scopes WHERE platform=? AND account_id=? AND conversation_id=?", (platform, account_id, conversation_id))
        return str(row["scope_id"])

    async def resolve_scope(self, platform: str, account_id: str, conversation_id: str) -> str | None:
        row = await self._fetchone("SELECT scope_id FROM scopes WHERE platform=? AND account_id=? AND conversation_id=?", (platform, account_id, conversation_id))
        return str(row["scope_id"]) if row else None

    async def all_scopes(self, platform: str | None = None) -> list[dict[str, str]]:
        """Every known scope, so a cold start can rehydrate its scope map from
        disk instead of waiting for the first inbound message of each chat."""
        if platform:
            rows = await self._fetchall("SELECT scope_id,platform,account_id,conversation_id,display_name,tag FROM scopes WHERE platform=?", (platform,))
        else:
            rows = await self._fetchall("SELECT scope_id,platform,account_id,conversation_id,display_name,tag FROM scopes", ())
        return [
            {"scope_id": str(row["scope_id"]), "platform": str(row["platform"]),
             "account_id": str(row["account_id"]), "conversation_id": str(row["conversation_id"]),
             "display_name": str(row["display_name"])}
            for row in rows
        ]

    async def add_standing_intent(self, scope_id: str | None, instruction: str,
                                  keywords: Sequence[str], *, remaining: int = -1,
                                  cooldown_seconds: int = 0) -> dict[str, Any]:
        """Event-conditioned intent (OpenClaw-style standing intent)."""
        clean = [str(k).strip().casefold()[:40] for k in keywords if str(k).strip()][:8]
        if not instruction.strip() or not clean:
            raise ValueError("standing intent requires instruction and keywords")
        intent_id, now = uuid.uuid4().hex[:12], utc_now()
        async with self._write_lock:
            await self._conn().execute(
                "INSERT INTO standing_intents(intent_id,scope_id,instruction,keywords_json,"
                "remaining,cooldown_seconds,last_fired,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (intent_id, scope_id, instruction.strip()[:600], _json(clean),
                 int(remaining), max(0, int(cooldown_seconds)), "", "active", now))
            await self._conn().commit()
        return {"intent_id": intent_id, "instruction": instruction.strip()[:600],
                "keywords": clean, "remaining": int(remaining)}

    async def list_standing_intents(self, scope_ids: Sequence[str] | None = None,
                                    active_only: bool = True) -> list[dict[str, Any]]:
        clauses, args = [], []
        if active_only:
            clauses.append("status='active'")
        if scope_ids:
            marks = ",".join("?" for _ in scope_ids)
            clauses.append(f"(scope_id IS NULL OR scope_id IN ({marks}))")
            args.extend(scope_ids)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await self._fetchall(
            "SELECT * FROM standing_intents" + where + " ORDER BY created_at DESC LIMIT 50",
            tuple(args))
        result = []
        for row in rows:
            try:
                keywords = json.loads(row["keywords_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                keywords = []
            result.append({
                "intent_id": str(row["intent_id"]), "scope_id": str(row["scope_id"] or ""),
                "instruction": str(row["instruction"]), "keywords": [str(k) for k in keywords],
                "remaining": int(row["remaining"]), "cooldown_seconds": int(row["cooldown_seconds"]),
                "last_fired": str(row["last_fired"]), "status": str(row["status"]),
            })
        return result

    async def fire_standing_intent(self, intent_id: str) -> bool:
        """Mark fired: stamp time, decrement finite budgets, retire exhausted."""
        now = utc_now()
        async with self._write_lock:
            row = await self._fetchone(
                "SELECT remaining FROM standing_intents WHERE intent_id=? AND status='active'",
                (intent_id,))
            if row is None:
                return False
            remaining = int(row["remaining"])
            if remaining > 0:
                remaining -= 1
                await self._conn().execute(
                    "UPDATE standing_intents SET last_fired=?,remaining=?,status=? WHERE intent_id=?",
                    (now, remaining, "spent" if remaining <= 0 else "active", intent_id))
            else:
                await self._conn().execute(
                    "UPDATE standing_intents SET last_fired=? WHERE intent_id=?",
                    (now, intent_id))
            await self._conn().commit()
        return True

    async def cancel_standing_intent(self, intent_id: str) -> bool:
        async with self._write_lock:
            cursor = await self._conn().execute(
                "UPDATE standing_intents SET status='cancelled' WHERE intent_id=? AND status='active'",
                (intent_id,))
            await self._conn().commit()
        return bool(cursor.rowcount)

    # ---- 任务表（hermes todo_tool 移植：多步工作的显式任务清单） ----
    async def add_todos(self, scope_id: str, items: Sequence[str]) -> list[dict[str, Any]]:
        now = utc_now()
        clean = [str(x).strip()[:300] for x in items if str(x).strip()][:20]
        async with self._write_lock:
            row = await self._fetchone(
                "SELECT COALESCE(MAX(position), -1) AS n FROM agent_todos WHERE scope_id=?",
                (scope_id,))
            position = int(row["n"]) + 1 if row else 0
            for content in clean:
                await self._conn().execute(
                    "INSERT INTO agent_todos(todo_id,scope_id,content,status,position,created_at,updated_at) "
                    "VALUES(?,?,?,'pending',?,?,?)",
                    (uuid.uuid4().hex[:12], scope_id, content, position, now, now))
                position += 1
            await self._conn().commit()
        return await self.list_todos(scope_id)

    async def list_todos(self, scope_id: str, statuses: Sequence[str] | None = None) -> list[dict[str, Any]]:
        clauses, args = ["scope_id=?"], [scope_id]
        wanted = [s for s in (statuses or ()) if s in {"pending", "in_progress", "completed", "cancelled"}]
        if wanted:
            clauses.append("status IN (" + ",".join("?" for _ in wanted) + ")")
            args.extend(wanted)
        rows = await self._fetchall(
            "SELECT * FROM agent_todos WHERE " + " AND ".join(clauses)
            + " ORDER BY position ASC LIMIT 200", tuple(args))
        return [
            {"todo_id": str(row["todo_id"]), "content": str(row["content"]),
             "status": str(row["status"]), "position": int(row["position"]),
             "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"])}
            for row in rows
        ]

    async def update_todo(self, scope_id: str, todo_id: str,
                          status: str = "", content: str = "") -> dict[str, Any] | None:
        fields, args = [], []
        if status in {"pending", "in_progress", "completed", "cancelled"}:
            fields.append("status=?")
            args.append(status)
        if str(content or "").strip():
            fields.append("content=?")
            args.append(str(content).strip()[:300])
        if not fields:
            return None
        fields.append("updated_at=?")
        args.append(utc_now())
        async with self._write_lock:
            cursor = await self._conn().execute(
                "UPDATE agent_todos SET " + ",".join(fields)
                + " WHERE scope_id=? AND todo_id=?",
                (*args, scope_id, str(todo_id)))
            await self._conn().commit()
        if not cursor.rowcount:
            return None
        rows = await self.list_todos(scope_id)
        return next((item for item in rows if item["todo_id"] == str(todo_id)), None)

    async def clear_todos(self, scope_id: str, done_only: bool = False) -> int:
        async with self._write_lock:
            if done_only:
                cursor = await self._conn().execute(
                    "DELETE FROM agent_todos WHERE scope_id=? AND status IN ('completed','cancelled')",
                    (scope_id,))
            else:
                cursor = await self._conn().execute(
                    "DELETE FROM agent_todos WHERE scope_id=?", (scope_id,))
            await self._conn().commit()
        return int(cursor.rowcount or 0)

    # ---- 用量统计（openclaw usage-tracking 移植） ----
    async def add_usage(self, calls: int = 1, prompt_tokens: int = 0,
                        completion_tokens: int = 0, scope_id: str = "") -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        async with self._write_lock:
            await self._conn().execute(
                "INSERT INTO usage_stats(day,scope_id,calls,prompt_tokens,completion_tokens,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(day,scope_id) DO UPDATE SET "
                "calls=usage_stats.calls+excluded.calls,"
                "prompt_tokens=usage_stats.prompt_tokens+excluded.prompt_tokens,"
                "completion_tokens=usage_stats.completion_tokens+excluded.completion_tokens,"
                "updated_at=excluded.updated_at",
                (day, scope_id, max(0, int(calls)), max(0, int(prompt_tokens)),
                 max(0, int(completion_tokens)), utc_now()))
            await self._conn().commit()

    async def usage_summary(self, days: int = 7) -> dict[str, Any]:
        limit = min(max(int(days), 1), 90)
        rows = await self._fetchall(
            "SELECT day,SUM(calls) AS calls,SUM(prompt_tokens) AS pt,"
            "SUM(completion_tokens) AS ct FROM usage_stats GROUP BY day "
            "ORDER BY day DESC LIMIT ?", (limit,))
        daily = [
            {"day": str(row["day"]), "calls": int(row["calls"] or 0),
             "prompt_tokens": int(row["pt"] or 0), "completion_tokens": int(row["ct"] or 0)}
            for row in rows
        ]
        return {
            "daily": daily,
            "totals": {
                "calls": sum(d["calls"] for d in daily),
                "prompt_tokens": sum(d["prompt_tokens"] for d in daily),
                "completion_tokens": sum(d["completion_tokens"] for d in daily),
            },
        }

    async def postpone_plan(self, plan_id: str, *, minutes: int = 15,
                            max_retries: int = 2) -> bool:
        """执行失败后顺延重试：改回 pending 并把 due_at 推后；超过上限返回 False。

        实录：上游瞬时 503 / 模型通道抖动会让日程直接 failed 永不重来——
        瞬时故障应该顺延，只有真正做不了（或重试超限）才判死。
        """
        row = await self._fetchone(
            "SELECT note FROM agent_plans WHERE plan_id=?", (plan_id,))
        note = str(row["note"] or "") if row is not None else ""
        tries = 0
        marker = "自动重试"
        if marker in note:
            try:
                tries = int(note.split(marker, 1)[1].split("次", 1)[0])
            except (ValueError, IndexError):
                tries = 0
        if tries >= max(1, int(max_retries)):
            return False
        due = (datetime.now(timezone.utc) + timedelta(minutes=max(1, int(minutes)))
               ).isoformat(timespec="minutes")
        async with self._write_lock:
            await self._conn().execute(
                "UPDATE agent_plans SET status='pending', due_at=?, note=? WHERE plan_id=?",
                (due, f"{marker}{tries + 1}次", plan_id))
            await self._conn().commit()
        return True

    async def ingest_message(self, message: NormalizedMessage, dedupe_key: str | None = None) -> StoredMessage:
        db = self._conn()
        payload = _json(message.raw_event)
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()
        key = dedupe_key or _event_dedupe_key(message, payload_hash)
        async with self._write_lock:
            await self._begin_write(db)
            try:
                scope_id = await self._ensure_scope(message.platform, message.account_id, message.conversation_id)
                existing = await self._fetchone("SELECT event_id FROM event_headers WHERE scope_id=? AND dedupe_key=?", (scope_id, key))
                if existing:
                    result = await self._stored_for_event(str(existing["event_id"]))
                    await db.rollback()
                    return result
                scope = await self._fetchone("SELECT next_seq FROM scopes WHERE scope_id=?", (scope_id,))
                seq = int(scope["next_seq"])
                await db.execute("UPDATE scopes SET next_seq=next_seq+1 WHERE scope_id=?", (scope_id,))
                now, event_id = utc_now(), uuid.uuid4().hex
                await db.execute("INSERT INTO event_headers VALUES(?,?,?,?,?,?,?,?,?,?,?)", (event_id, scope_id, seq, message.event_type, message.upstream_message_id, message.sender_id, message.occurred_at, key, payload_hash, 1, now))
                await db.execute("INSERT INTO event_payloads VALUES(?,?)", (event_id, payload))
                identity = await self._fetchone("SELECT message_id FROM message_identities WHERE scope_id=? AND upstream_message_id=?", (scope_id, message.upstream_message_id))
                message_id = str(identity["message_id"]) if identity else uuid.uuid4().hex
                if identity is None:
                    await db.execute("INSERT INTO message_identities VALUES(?,?,?,?)", (message_id, scope_id, message.upstream_message_id, now))
                rev = await self._fetchone("SELECT COALESCE(MAX(revision_no),0)+1 AS n FROM message_revisions WHERE message_id=?", (message_id,))
                revision_id, revision_no = uuid.uuid4().hex, int(rev["n"])
                deleted = int(message.event_type in {"message.deleted", "message.recalled"})
                await db.execute("INSERT INTO message_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (revision_id, message_id, event_id, revision_no, message.sender_id, message.sender_name, message.text, _json(message.parts), message.reply_to, message.occurred_at, deleted, now))
                await db.execute("INSERT INTO message_current VALUES(?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET revision_id=excluded.revision_id,scope_seq=excluded.scope_seq,is_deleted=excluded.is_deleted", (message_id, revision_id, seq, deleted))
                await self._sync_search(message_id, scope_id, message.text, bool(deleted))
                if revision_no > 1:
                    await self._invalidate_derived(scope_id, [message_id])
                await db.commit()
                return StoredMessage(message_id, revision_id, scope_id, seq, message.upstream_message_id, message.sender_id, message.sender_name, message.text, message.occurred_at, list(message.parts), message.reply_to)
            except BaseException:
                await db.rollback()
                raise

    async def _stored_for_event(self, event_id: str) -> StoredMessage:
        row = await self._fetchone("SELECT mi.message_id,mr.revision_id,mi.scope_id,eh.scope_seq,mi.upstream_message_id,mr.sender_id,mr.sender_name,mr.text,mr.occurred_at,mr.parts_json,mr.reply_to FROM message_revisions mr JOIN message_identities mi ON mi.message_id=mr.message_id JOIN event_headers eh ON eh.event_id=mr.event_id WHERE mr.event_id=?", (event_id,))
        if row is None:
            raise RuntimeError("event has no message projection")
        return _stored(row)

    async def messages_from_seq(self, scope_id: str, start_seq: int, limit: int = 40) -> list[StoredMessage]:
        rows = await self._fetchall(
            "SELECT mi.message_id,mr.revision_id,mi.scope_id,mc.scope_seq,mi.upstream_message_id,mr.sender_id,mr.sender_name,mr.text,mr.occurred_at,mr.parts_json,mr.reply_to FROM message_current mc JOIN message_identities mi ON mi.message_id=mc.message_id JOIN message_revisions mr ON mr.revision_id=mc.revision_id WHERE mi.scope_id=? AND mc.is_deleted=0 AND mc.scope_seq>=? ORDER BY mc.scope_seq LIMIT ?",
            (scope_id, max(int(start_seq), 1), _limit(limit, 200)),
        )
        return [_stored(row) for row in rows]

    async def first_uncovered_seq(self, scope_id: str) -> int | None:
        row = await self._fetchone(
            "SELECT MIN(mc.scope_seq) AS n FROM message_current mc JOIN message_identities mi ON mi.message_id=mc.message_id WHERE mi.scope_id=? AND mc.is_deleted=0 AND NOT EXISTS(SELECT 1 FROM summary_nodes sn WHERE sn.scope_id=mi.scope_id AND sn.level=1 AND sn.status='active' AND mc.scope_seq BETWEEN sn.start_seq AND sn.end_seq)",
            (scope_id,),
        )
        return int(row["n"]) if row and row["n"] is not None else None

    async def pending_l1_messages(self, scope_id: str, limit: int = 40) -> list[StoredMessage]:
        start = await self.first_uncovered_seq(scope_id)
        if start is None:
            return []
        boundary = await self._fetchone("SELECT MIN(start_seq) AS n FROM summary_nodes WHERE scope_id=? AND level=1 AND status='active' AND start_seq>?", (scope_id, start))
        end = int(boundary["n"]) if boundary and boundary["n"] is not None else None
        rows = await self._fetchall(
            "SELECT mi.message_id,mr.revision_id,mi.scope_id,mc.scope_seq,mi.upstream_message_id,mr.sender_id,mr.sender_name,mr.text,mr.occurred_at,mr.parts_json,mr.reply_to FROM message_current mc JOIN message_identities mi ON mi.message_id=mc.message_id JOIN message_revisions mr ON mr.revision_id=mc.revision_id WHERE mi.scope_id=? AND mc.is_deleted=0 AND mc.scope_seq>=? AND (? IS NULL OR mc.scope_seq<?) ORDER BY mc.scope_seq LIMIT ?",
            (scope_id, start, end, end, _limit(limit, 200)),
        )
        return [_stored(row) for row in rows]

    async def save_embedding(self, scope_id: str, message_id: str, model: str, vector: Sequence[float]) -> None:
        values = [float(item) for item in vector]
        if not values or not all(math.isfinite(item) for item in values):
            raise ValueError("embedding vector must contain finite values")
        async with self._write_lock:
            await self._validate_owned(scope_id, "message_identities", "message_id", [message_id])
            await self._conn().execute("INSERT INTO embeddings(embedding_id,scope_id,owner_type,owner_id,model,dimensions,vector_json,created_at) VALUES(?,?,'message',?,?,?,?,?) ON CONFLICT(scope_id,owner_type,owner_id,model) DO UPDATE SET dimensions=excluded.dimensions,vector_json=excluded.vector_json,created_at=excluded.created_at", (uuid.uuid4().hex, scope_id, message_id, model, len(values), _json(values), utc_now()))
            await self._conn().commit()

    async def recent_messages(self, scope_id: str, limit: int = 30, before_seq: int | None = None, include_events: bool = False) -> list[StoredMessage]:
        limit = _limit(limit, 200)
        clause, values = (" AND mc.scope_seq<?", [before_seq]) if before_seq is not None else ("", [])
        gate = "" if include_events else " AND (eh.event_type NOT LIKE 'notice.%' AND eh.event_type NOT LIKE 'request.%')"
        rows = await self._fetchall(
            "SELECT mi.message_id,mr.revision_id,mi.scope_id,mc.scope_seq,mi.upstream_message_id,mr.sender_id,mr.sender_name,mr.text,mr.occurred_at,mr.parts_json,mr.reply_to FROM message_current mc JOIN message_identities mi ON mi.message_id=mc.message_id JOIN message_revisions mr ON mr.revision_id=mc.revision_id JOIN event_headers eh ON eh.event_id=mr.event_id WHERE mi.scope_id=? AND mc.is_deleted=0" + gate + clause + " ORDER BY mc.scope_seq DESC LIMIT ?",
            (scope_id, *values, limit),
        )
        return [_stored(row) for row in reversed(rows)]

    async def recent_events(self, scope_ids: str | Sequence[str], limit: int = 20) -> list[StoredMessage]:
        """Latest notice/request records; these are query-only, never auto-injected."""
        scopes = [scope_ids] if isinstance(scope_ids, str) else [s for s in scope_ids if s]
        if not scopes:
            return []
        marks = ",".join("?" for _ in scopes)
        rows = await self._fetchall(
            "SELECT mi.message_id,mr.revision_id,mi.scope_id,mc.scope_seq,mi.upstream_message_id,mr.sender_id,mr.sender_name,mr.text,mr.occurred_at,mr.parts_json,mr.reply_to FROM message_current mc JOIN message_identities mi ON mi.message_id=mc.message_id JOIN message_revisions mr ON mr.revision_id=mc.revision_id JOIN event_headers eh ON eh.event_id=mr.event_id WHERE mi.scope_id IN (" + marks + ") AND mc.is_deleted=0 AND (eh.event_type LIKE 'notice.%' OR eh.event_type LIKE 'request.%') ORDER BY mr.occurred_at DESC LIMIT ?",
            (*scopes, _limit(limit, 100)),
        )
        return [_stored(row) for row in reversed(rows)]

    async def get_messages(self, scope_id: str, message_ids: Sequence[str], limit: int = 8) -> list[StoredMessage]:
        ids = list(dict.fromkeys(str(x) for x in message_ids))[: _limit(limit, 8)]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = await self._fetchall(
            f"SELECT mi.message_id,mr.revision_id,mi.scope_id,mc.scope_seq,mi.upstream_message_id,mr.sender_id,mr.sender_name,mr.text,mr.occurred_at,mr.parts_json,mr.reply_to FROM message_current mc JOIN message_identities mi ON mi.message_id=mc.message_id JOIN message_revisions mr ON mr.revision_id=mc.revision_id WHERE mi.scope_id=? AND mc.is_deleted=0 AND mi.message_id IN ({marks}) ORDER BY mc.scope_seq",
            (scope_id, *ids),
        )
        return [_stored(row) for row in rows]

    async def chat_context(self, scope_id: str, message_id: str, radius: int = 5) -> list[StoredMessage]:
        radius = min(max(int(radius), 0), 5)
        row = await self._fetchone("SELECT mc.scope_seq FROM message_current mc JOIN message_identities mi ON mi.message_id=mc.message_id WHERE mi.scope_id=? AND mi.message_id=?", (scope_id, message_id))
        if row is None:
            return []
        seq = int(row["scope_seq"])
        rows = await self._fetchall(
            "SELECT mi.message_id,mr.revision_id,mi.scope_id,mc.scope_seq,mi.upstream_message_id,mr.sender_id,mr.sender_name,mr.text,mr.occurred_at,mr.parts_json,mr.reply_to FROM message_current mc JOIN message_identities mi ON mi.message_id=mc.message_id JOIN message_revisions mr ON mr.revision_id=mc.revision_id WHERE mi.scope_id=? AND mc.is_deleted=0 AND mc.scope_seq BETWEEN ? AND ? ORDER BY mc.scope_seq",
            (scope_id, seq - radius, seq + radius),
        )
        return [_stored(item) for item in rows]

    async def find_by_upstream(self, scope_id: str, upstream_message_id: str) -> StoredMessage | None:
        """按上游（QQ/NapCat）消息 id 查已存消息——引用(reply)解析用。"""
        row = await self._fetchone(
            "SELECT mi.message_id,mr.revision_id,mi.scope_id,mc.scope_seq,mi.upstream_message_id,mr.sender_id,mr.sender_name,mr.text,mr.occurred_at,mr.parts_json,mr.reply_to FROM message_current mc JOIN message_identities mi ON mi.message_id=mc.message_id JOIN message_revisions mr ON mr.revision_id=mc.revision_id WHERE mi.scope_id=? AND mi.upstream_message_id=? AND mc.is_deleted=0 LIMIT 1",
            (scope_id, str(upstream_message_id)),
        )
        return _stored(row) if row is not None else None

    async def find_message_any(self, scope_id: str, ref: str) -> StoredMessage | None:
        """按**内部编号或 QQ 上游 id** 任一取消息。

        实录（bot"拼不出合并转发"）：`forward_messages` 拿到的 id 可能来自
        `search_chat_history`（那里叫 message_id=内部编号）也可能来自上下文
        （qq_id=上游编号），而旧的回落只认上游 id → 模型传内部编号时两条路都
        查不到，于是回一句"没取到"，模型就以为"拼不了转发"。
        """
        value = str(ref or "").strip()
        if not value:
            return None
        row = await self._fetchone(
            "SELECT mi.message_id,mr.revision_id,mi.scope_id,mc.scope_seq,"
            "mi.upstream_message_id,mr.sender_id,mr.sender_name,mr.text,"
            "mr.occurred_at,mr.parts_json,mr.reply_to "
            "FROM message_current mc "
            "JOIN message_identities mi ON mi.message_id=mc.message_id "
            "JOIN message_revisions mr ON mr.revision_id=mc.revision_id "
            "WHERE mi.scope_id=? AND mc.is_deleted=0 "
            "AND (mi.message_id=? OR mi.upstream_message_id=?) LIMIT 1",
            (scope_id, value, value),
        )
        return _stored(row) if row is not None else None

    async def resolve_reply_target(self, scope_id: str, candidate: str) -> str:
        """把引用目标校验并翻成 **QQ 上游消息号**；确认不了返回 ""（调用方就别引用）。

        实录（"bot 莫名其妙引用错消息"）：上下文里同时给了内部编号 message_id 和
        QQ 消息号 qq_id，模型时常拿内部编号去填 reply_to_message_id —— QQ 端按号
        一找，引用到的是**另一条完全不相干的消息**（实录：回复的是群友的感慨，
        引用条却是"戳一戳"通知那条）。所以发送前必须：①两种 id 都接受并翻译；
        ②系统通知类消息（notice./request.）一律不许被引用。
        """
        value = str(candidate or "").strip()
        if not value:
            return ""
        row = await self._fetchone(
            "SELECT mi.upstream_message_id AS up, eh.event_type AS etype "
            "FROM message_current mc "
            "JOIN message_identities mi ON mi.message_id=mc.message_id "
            "JOIN message_revisions mr ON mr.revision_id=mc.revision_id "
            "LEFT JOIN event_headers eh ON eh.event_id=mr.event_id "
            "WHERE mi.scope_id=? AND mc.is_deleted=0 "
            "AND (mi.message_id=? OR mi.upstream_message_id=?) LIMIT 1",
            (scope_id, value, value),
        )
        if row is None:
            return ""
        event_type = str(row["etype"] or "")
        if event_type.startswith(("notice.", "request.")):
            return ""
        return str(row["up"] or "")

    async def find_forward_note(self, scope_id: str, forward_id: str) -> str:
        """按转发资源 id 找已展开入库的正文（QQ 侧转发资源有缓存时限，过期后
        只能靠入库时抓下来的这份——"合并聊天记录点不开/已过期"的正解）。

        `_ingest_forward_notes` 写入的文本形如 ``[合并转发内容 <fid前12位>]\\n正文``，
        所以用前缀 LIKE 找。
        """
        prefix = f"[合并转发内容 {str(forward_id or '')[:12]}"
        if len(prefix) < 12:
            return ""
        rows = await self._fetchall(
            "SELECT mr.text FROM message_current mc "
            "JOIN message_identities mi ON mi.message_id=mc.message_id "
            "JOIN message_revisions mr ON mr.revision_id=mc.revision_id "
            "WHERE mi.scope_id=? AND mc.is_deleted=0 AND mr.text LIKE ? "
            "ORDER BY mc.scope_seq DESC LIMIT 1",
            (scope_id, prefix + "%"),
        )
        if not rows:
            return ""
        text = str(rows[0]["text"] or "")
        # 去掉首行标记，只留正文
        return text.split("\n", 1)[1].strip() if "\n" in text else text.strip()

    async def store_summary(self, scope_id: str, level: int, start_seq: int, end_seq: int, title: str, body: str, topics: Sequence[str], message_ids: Sequence[str] = (), input_summary_ids: Sequence[str] = (), manifest: dict[str, Any] | None = None, model: str = "", prompt_version: str = "1") -> SummaryRecord:
        if level not in {1, 2, 3}:
            raise ValueError("summary level must be 1, 2, or 3")
        message_ids = list(dict.fromkeys(message_ids))
        input_summary_ids = list(dict.fromkeys(input_summary_ids))
        await self._validate_owned(scope_id, "message_identities", "message_id", message_ids)
        await self._validate_owned(scope_id, "summary_nodes", "summary_id", input_summary_ids)
        db, summary_id, now = self._conn(), uuid.uuid4().hex, utc_now()
        data = manifest or {"messages": message_ids, "summaries": input_summary_ids}
        async with self._write_lock:
            await self._begin_write(db)
            try:
                await db.execute("INSERT INTO summary_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (summary_id, scope_id, level, int(start_seq), int(end_seq), title, body, _json(list(topics)), _json(data), model, prompt_version, "active", now))
                await db.executemany("INSERT INTO summary_inputs(summary_id,input_message_id) VALUES(?,?)", [(summary_id, item) for item in message_ids])
                await db.executemany("INSERT INTO summary_inputs(summary_id,input_summary_id) VALUES(?,?)", [(summary_id, item) for item in input_summary_ids])
                await db.executemany("INSERT INTO summary_citations VALUES(?,?,?)", [(summary_id, item, "") for item in message_ids])
                # 群话题：每个话题按 (scope,title) 唯一，命中就 hits+1 并推进 last_seq
                # （实录：v0.40.0 我改 list_impressions 时误删了这段，导致话题不再记录）
                for topic in list(dict.fromkeys(str(x).strip() for x in topics))[:12]:
                    if not topic:
                        continue
                    await db.execute(
                        "INSERT INTO group_topics(topic_id,scope_id,title,hits,last_seq,"
                        "created_at) VALUES(?,?,?,1,?,?) "
                        "ON CONFLICT(scope_id,title) DO UPDATE SET hits=group_topics.hits+1, "
                        "last_seq=excluded.last_seq",
                        (uuid.uuid4().hex, scope_id, topic[:80], int(end_seq), now))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return SummaryRecord(summary_id, scope_id, level, int(start_seq), int(end_seq), title, body, list(topics), message_ids, now)

    async def upsert_topics(self, scope_id: str, topics: Sequence[str], last_seq: int) -> int:
        """把一批话题写进 group_topics（同 scope 同标题 hits+1 并推进 last_seq）。

        compression 侧一直在调它（实录日志：'Storage' object has no attribute 'upsert_topics'），
        但方法从来没被实现过——话题表因此永远是空的。与 store_summary 里那段内联 SQL 同语义。
        """
        cleaned = [str(item).strip()[:80] for item in (topics or [])]
        cleaned = [item for item in dict.fromkeys(cleaned) if item][:12]
        if not cleaned:
            return 0
        db = self._conn()
        now = utc_now()
        # 必须持写锁：否则和 ingest/压缩并发时，两边各自 BEGIN IMMEDIATE 会互相踩
        # （实录：'cannot start a transaction within a transaction'）
        async with self._write_lock:
            await self._begin_write(db)
            try:
                for topic in cleaned:
                    await db.execute(
                        "INSERT INTO group_topics(topic_id,scope_id,title,hits,last_seq,"
                        "created_at) VALUES(?,?,?,1,?,?) "
                        "ON CONFLICT(scope_id,title) DO UPDATE SET hits=group_topics.hits+1, "
                        "last_seq=excluded.last_seq",
                        (uuid.uuid4().hex, scope_id, topic, int(last_seq), now))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return len(cleaned)

    async def list_summaries(self, scope_id: str | Sequence[str], limit: int = 8, levels: Sequence[int] | None = None, query: str | None = None) -> list[SummaryRecord]:
        scopes = [scope_id] if isinstance(scope_id, str) else [s for s in scope_id if s]
        if not scopes:
            return []
        conditions, args = [f"scope_id IN ({','.join('?' for _ in scopes)})", "status='active'"], [*scopes]
        if levels:
            valid = [x for x in levels if x in {1, 2, 3}]
            if valid:
                conditions.append("level IN (" + ",".join("?" for _ in valid) + ")")
                args.extend(valid)
        if query:
            conditions.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
            pattern = "%" + _escape_like(query) + "%"
            args.extend([pattern, pattern])
        rows = await self._fetchall("SELECT * FROM summary_nodes WHERE " + " AND ".join(conditions) + " ORDER BY level DESC,end_seq DESC LIMIT ?", (*args, _limit(limit, 30)))
        result: list[SummaryRecord] = []
        for row in rows:
            citations = await self._fetchall("SELECT message_id FROM summary_citations WHERE summary_id=? ORDER BY message_id", (row["summary_id"],))
            result.append(SummaryRecord(str(row["summary_id"]), str(row["scope_id"]), int(row["level"]), int(row["start_seq"]), int(row["end_seq"]), str(row["title"]), str(row["body"]), json.loads(row["topics_json"]), [str(x["message_id"]) for x in citations], str(row["created_at"])))
        return result

    async def get_affinity(self, scope_id: str, user_id: str) -> dict[str, Any]:
        row = await self._fetchone(
            "SELECT warmth,note,interactions,updated_at FROM user_affinity WHERE scope_id=? AND user_id=?",
            (scope_id, user_id),
        )
        if row is None:
            return {"warmth": 50.0, "note": "", "interactions": 0, "updated_at": ""}
        return {
            "warmth": round(float(row["warmth"]), 1),
            "note": str(row["note"]),
            "interactions": int(row["interactions"]),
            "updated_at": str(row["updated_at"]),
        }

    async def adjust_affinity(self, scope_id: str, user_id: str, delta: float, note: str | None = None) -> dict[str, Any]:
        current = await self.get_affinity(scope_id, user_id)
        warmth = min(max(current["warmth"] + float(delta), 0.0), 100.0)
        db = self._conn()
        async with self._write_lock:
            await db.execute(
                "INSERT INTO user_affinity(scope_id,user_id,warmth,note,interactions,updated_at) VALUES(?,?,?,?,1,?) "
                "ON CONFLICT(scope_id,user_id) DO UPDATE SET warmth=excluded.warmth,"
                "note=CASE WHEN excluded.note!='' THEN excluded.note ELSE user_affinity.note END,"
                "interactions=user_affinity.interactions+1,updated_at=excluded.updated_at",
                (scope_id, user_id, warmth, (note or "")[:200], utc_now()),
            )
            await db.commit()
        return await self.get_affinity(scope_id, user_id)

    async def add_plans(self, items: Sequence[Mapping[str, Any]]) -> int:
        """Insert scheduled plan items; identical (due_at,kind,detail) dedupes.

        due_at is normalized to UTC so due-queries can compare plain strings.
        """
        rows: list[tuple[str, str, str, str, str]] = []
        for item in items:
            due_raw = str(item.get("due_at", "")).strip()
            detail = str(item.get("detail", "")).strip()[:400]
            if not due_raw or not detail:
                continue
            try:
                due = datetime.fromisoformat(due_raw).astimezone(timezone.utc).isoformat(timespec="minutes")
            except (TypeError, ValueError):
                continue
            kind = str(item.get("kind", "custom")).strip()[:30] or "custom"
            pending = await self._fetchone(
                "SELECT 1 AS n FROM agent_plans WHERE status IN ('pending','running') "
                "AND kind=? AND detail=? LIMIT 1",
                (kind, detail),
            )
            if pending:
                continue  # the same errand is already on the board
            plan_id = uuid.uuid5(uuid.NAMESPACE_URL, f"plan\0{due}\0{kind}\0{detail}").hex
            rows.append((plan_id, due, kind, detail, utc_now()))
        if not rows:
            return 0
        db = self._conn()
        async with self._write_lock:
            await db.executemany(
                "INSERT OR IGNORE INTO agent_plans(plan_id,due_at,kind,detail,status,created_at) "
                "VALUES(?,?,?,?,'pending',?)",
                rows,
            )
            await db.commit()
        return len(rows)

    async def due_plans(self, limit: int = 5, stale_hours: int = 6) -> list[dict[str, Any]]:
        """Plans that came due and are not yet stale.

        Anything older than `stale_hours` is retired as `skipped` first: after
        a re-install an old plan must not suddenly fire as if it were new work.
        """
        db, cutoff = self._conn(), utc_now()
        stale_before = (
            datetime.fromisoformat(cutoff)
            - timedelta(hours=max(1, int(stale_hours)))
        ).astimezone(timezone.utc).isoformat(timespec="minutes")
        async with self._write_lock:
            await db.execute(
                "UPDATE agent_plans SET status='skipped',executed_at=?,note='stale' "
                "WHERE status='pending' AND due_at<?",
                (cutoff, stale_before),
            )
            await db.commit()
        rows = await self._fetchall(
            "SELECT plan_id,due_at,kind,detail,created_at FROM agent_plans "
            "WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT ?",
            (cutoff, int(limit)),
        )
        return [dict(row) for row in rows]

    async def pending_plans(self, limit: int = 20) -> list[dict[str, Any]]:
        db = self._conn()
        rows = await self._fetchall(
            "SELECT plan_id,due_at,kind,detail,status FROM agent_plans "
            "WHERE status='pending' ORDER BY due_at LIMIT ?",
            (int(limit),),
        )
        return [dict(row) for row in rows]

    async def claim_plan(self, plan_id: str) -> bool:
        """Atomically take ownership of a plan so it never runs twice."""
        if not plan_id:
            return False
        db = self._conn()
        async with self._write_lock:
            cursor = await db.execute(
                "UPDATE agent_plans SET status='running',executed_at=? "
                "WHERE plan_id=? AND status='pending'",
                (utc_now(), plan_id),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def drop_plan(self, plan_id: str) -> bool:
        """撤销一条日程（WebUI 的「撤销任务」用）。

        实录：这个方法在重构里被删了，但 WebUI 两处还在调 → 点撤销直接
        `AttributeError: 'Storage' object has no attribute 'drop_plan'`。
        """
        plan_id = str(plan_id or "").strip()
        if not plan_id:
            return False
        db = self._conn()
        async with self._write_lock:
            cursor = await db.execute("DELETE FROM agent_plans WHERE plan_id=?", (plan_id,))
            await db.commit()
            return bool(cursor.rowcount)

    async def complete_plan(self, plan_id: str, status: str, note: str = "") -> bool:
        if status not in {"done", "skipped", "failed"}:
            status = "done"
        db = self._conn()
        async with self._write_lock:
            cursor = await db.execute(
                "UPDATE agent_plans SET status=?,executed_at=?,note=? "
                "WHERE plan_id=? AND status IN ('pending','running')",
                (status, utc_now(), note[:200], plan_id),
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def upsert_style(self, scope_id: str, user_id: str, *, display_name: str | None = None,
                           style_summary: str | None = None, catchphrases: Sequence[str] | None = None,
                           sample_lines: Sequence[str] | None = None, msg_count: int = 0) -> dict[str, Any]:
        db = self._conn()
        now = utc_now()
        phrases = [str(x).strip()[:24] for x in (catchphrases or []) if str(x).strip()][:6]
        samples = [str(x).strip()[:80] for x in (sample_lines or []) if str(x).strip()][:3]
        async with self._write_lock:
            await db.execute(
                "INSERT INTO user_styles(scope_id,user_id,display_name,style_summary,"
                "catchphrases_json,sample_lines_json,msg_count,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope_id,user_id) DO UPDATE SET "
                "display_name=CASE WHEN excluded.display_name!='' THEN excluded.display_name ELSE user_styles.display_name END,"
                "style_summary=CASE WHEN excluded.style_summary!='' THEN excluded.style_summary ELSE user_styles.style_summary END,"
                "catchphrases_json=CASE WHEN excluded.catchphrases_json!='[]' THEN excluded.catchphrases_json ELSE user_styles.catchphrases_json END,"
                "sample_lines_json=CASE WHEN excluded.sample_lines_json!='[]' THEN excluded.sample_lines_json ELSE user_styles.sample_lines_json END,"
                "msg_count=CASE WHEN excluded.msg_count>0 THEN excluded.msg_count ELSE user_styles.msg_count END,"
                "updated_at=excluded.updated_at",
                (scope_id, user_id, (display_name or "")[:60], (style_summary or "")[:300],
                 json.dumps(phrases, ensure_ascii=False), json.dumps(samples, ensure_ascii=False),
                 int(msg_count or 0), now),
            )
            await db.commit()
        return await self.get_style(scope_id, user_id)

    async def get_style(self, scope_id: str, user_id: str) -> dict[str, Any]:
        row = await self._fetchone(
            "SELECT display_name,style_summary,catchphrases_json,sample_lines_json,msg_count,updated_at "
            "FROM user_styles WHERE scope_id=? AND user_id=?", (scope_id, user_id))
        if row is None:
            return {}
        try:
            phrases = json.loads(row["catchphrases_json"])
        except (TypeError, ValueError):
            phrases = []
        try:
            samples = json.loads(row["sample_lines_json"])
        except (TypeError, ValueError):
            samples = []
        return {
            "user_id": str(user_id),
            "display_name": str(row["display_name"]),
            "style_summary": str(row["style_summary"]),
            "catchphrases": [str(x) for x in phrases][:6],
            "samples": [str(x) for x in samples][:3],
            "msg_count": int(row["msg_count"]),
            "updated_at": str(row["updated_at"]),
        }

    async def list_styles(self, scope_ids: Sequence[str], limit: int = 30) -> list[dict[str, Any]]:
        if not scope_ids:
            return []
        marks = ",".join("?" for _ in scope_ids)
        rows = await self._fetchall(
            "SELECT scope_id,user_id,display_name,style_summary,catchphrases_json,msg_count,updated_at "
            "FROM user_styles WHERE scope_id IN (" + marks + ") "
            "ORDER BY msg_count DESC LIMIT ?", (*scope_ids, _limit(limit, 100)))
        result = []
        for row in rows:
            try:
                phrases = json.loads(row["catchphrases_json"])
            except (TypeError, ValueError):
                phrases = []
            result.append({
                "scope_id": str(row["scope_id"]), "user_id": str(row["user_id"]),
                "display_name": str(row["display_name"]),
                "style_summary": str(row["style_summary"]),
                "catchphrases": [str(x) for x in phrases][:6],
                "msg_count": int(row["msg_count"]), "updated_at": str(row["updated_at"]),
            })
        return result

    async def get_impression(self, scope_id: str, user_id: str) -> dict[str, Any]:
        row = await self._fetchone(
            "SELECT display_name,impression,tags_json,first_seen,last_seen,updated_at "
            "FROM user_impressions WHERE scope_id=? AND user_id=?",
            (scope_id, user_id),
        )
        if row is None:
            return {}
        try:
            tags = json.loads(row["tags_json"])
        except (TypeError, ValueError):
            tags = []
        return {
            "user_id": str(user_id),
            "display_name": str(row["display_name"]),
            "impression": str(row["impression"]),
            "tags": [str(t) for t in tags][:8] if isinstance(tags, list) else [],
            "first_seen": str(row["first_seen"]),
            "last_seen": str(row["last_seen"]),
            "updated_at": str(row["updated_at"]),
        }

    async def get_affinity_any(self, scope_ids: Sequence[str], user_id: str) -> dict[str, Any]:
        """跨会话合并读好感度：同一个人在哪都只算一份。

        取"最近更新的那条"的 warmth（避免把不同群的好感平均成一个没意义的数），
        互动数累加，备注取最新非空，并附 groups 说明在哪见过。
        """
        scopes = [s for s in dict.fromkeys(scope_ids) if s]
        if not scopes:
            return {"warmth": 50.0, "note": "", "interactions": 0, "updated_at": ""}
        marks = ",".join("?" for _ in scopes)
        rows = await self._fetchall(
            "SELECT scope_id,warmth,note,interactions,updated_at FROM user_affinity "
            f"WHERE scope_id IN ({marks}) AND user_id=? ORDER BY updated_at DESC",
            (*scopes, str(user_id)),
        )
        if not rows:
            return {"warmth": 50.0, "note": "", "interactions": 0, "updated_at": ""}
        merged = {
            "user_id": str(user_id),
            "warmth": round(float(rows[0]["warmth"]), 1),
            "note": next((str(r["note"]) for r in rows if str(r["note"] or "").strip()), ""),
            "interactions": sum(int(r["interactions"] or 0) for r in rows),
            "updated_at": str(rows[0]["updated_at"] or ""),
        }
        label_by_scope = {}
        try:
            for item in await self.all_scopes("aiocqhttp"):
                label_by_scope[str(item.get("scope_id"))] = str(item.get("display_name") or "")
        except Exception:
            label_by_scope = {}
        groups = [label_by_scope.get(str(r["scope_id"])) or str(r["scope_id"])[:8]
                  for r in rows]
        merged["groups"] = list(dict.fromkeys(groups))[:8]
        return merged

    async def get_impression_any(self, scope_ids: Sequence[str], user_id: str) -> dict[str, Any]:
        """跨会话合并读人物印象（"一个人只有一份印象"，附 groups 出处）。"""
        labels: dict[str, str] = {}
        try:
            for item in await self.all_scopes("aiocqhttp"):
                name = str(item.get("display_name") or "").strip()
                if name:
                    labels[str(item.get("scope_id"))] = name
        except Exception:
            labels = {}
        merged = await self.list_impressions(
            [s for s in dict.fromkeys(scope_ids) if s], limit=200, merge=True,
            labels=labels or None)
        for item in merged:
            if str(item.get("user_id")) == str(user_id):
                return item
        return {}

    async def upsert_impression(
        self, scope_id: str, user_id: str, *, display_name: str | None = None,
        impression: str | None = None, tags: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        # 自动标名：名字留空时继承这个人在**任意群**已有的名字（同一个人只该有一个称呼）
        if not display_name:
            try:
                row = await self._fetchone(
                    "SELECT display_name FROM user_impressions WHERE user_id=? "
                    "AND display_name!='' ORDER BY updated_at DESC LIMIT 1", (user_id,))
                if row is not None:
                    display_name = str(row["display_name"] or "") or None
            except Exception:
                pass
        db = self._conn()
        now = utc_now()
        clean_tags = [str(t).strip()[:20] for t in (tags or []) if str(t).strip()][:8]
        async with self._write_lock:
            await db.execute(
                "INSERT INTO user_impressions(scope_id,user_id,display_name,impression,tags_json,"
                "first_seen,last_seen,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope_id,user_id) DO UPDATE SET "
                "display_name=CASE WHEN excluded.display_name!='' THEN excluded.display_name ELSE user_impressions.display_name END,"
                "impression=CASE WHEN excluded.impression!='' THEN excluded.impression ELSE user_impressions.impression END,"
                "tags_json=CASE WHEN excluded.tags_json!='[]' THEN excluded.tags_json ELSE user_impressions.tags_json END,"
                "last_seen=excluded.last_seen,updated_at=excluded.updated_at",
                (
                    scope_id, user_id, (display_name or "")[:60], (impression or "")[:600],
                    json.dumps(clean_tags, ensure_ascii=False), now, now, now,
                ),
            )
            await db.commit()
        return await self.get_impression(scope_id, user_id)

    async def list_impressions(
        self, scope_ids: Sequence[str], limit: int = 30, query: str | None = None,
        *, merge: bool = True, labels: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """读人物印象；`merge=True` 会把**同一个人的多条印象合并成一条**。

        实录："bot 的人物印象每个群不互通，一个人在两个群会有不同的印象"。
        合并策略：正文取最新一条非空、标签取并集、名字取最新非空、时间取最新，
        并附带 `groups` 说明这个人出现在哪些群。写入仍按群存（保留各自的细节），
        读取与注入一律走合并视图 —— 所以对 bot 来说"一个人只有一份印象"。
        """
        if not scope_ids:
            return []
        marks = ",".join("?" for _ in scope_ids)
        sql = (
            "SELECT scope_id,user_id,display_name,impression,tags_json,last_seen,updated_at "
            "FROM user_impressions WHERE scope_id IN (" + marks + ")"
        )
        params: list[Any] = [*scope_ids]
        if query and query.strip():
            sql += " AND (impression LIKE ? OR display_name LIKE ? OR user_id LIKE ?)"
            like = f"%{query.strip()[:60]}%"
            params += [like, like, like]
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, int(limit) * 8))
        rows = await self._fetchall(sql, tuple(params))
        if not merge:
            result = []
            for row in rows[:limit]:
                try:
                    tags = json.loads(row["tags_json"])
                except Exception:
                    tags = []
                result.append({
                    "scope_id": str(row["scope_id"]), "user_id": str(row["user_id"]),
                    "display_name": str(row["display_name"]),
                    "impression": str(row["impression"]),
                    "tags": [str(x) for x in tags],
                    "last_seen": str(row["last_seen"]),
                    "updated_at": str(row["updated_at"]),
                })
            return result

        merged: dict[str, dict[str, Any]] = {}
        for row in rows:
            user_id = str(row["user_id"] or "")
            if not user_id:
                continue
            try:
                tags = [str(x) for x in json.loads(row["tags_json"] or "[]")]
            except Exception:
                tags = []
            item = merged.get(user_id)
            if item is None:
                merged[user_id] = {
                    "user_id": user_id,
                    "display_name": str(row["display_name"] or ""),
                    "impression": str(row["impression"] or ""),
                    "tags": tags,
                    "last_seen": str(row["last_seen"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                    "scope_id": str(row["scope_id"] or ""),
                    "groups": [str(row["scope_id"] or "")],
                }
                continue
            if not item["display_name"] and row["display_name"]:
                item["display_name"] = str(row["display_name"])
            if not item["impression"] and row["impression"]:
                item["impression"] = str(row["impression"])
            for tag in tags:
                if tag not in item["tags"]:
                    item["tags"].append(tag)
            if str(row["updated_at"] or "") > item["updated_at"]:
                item["updated_at"] = str(row["updated_at"] or "")
                if row["impression"]:
                    item["impression"] = str(row["impression"])
                if row["display_name"]:
                    item["display_name"] = str(row["display_name"])
            if str(row["last_seen"] or "") > item["last_seen"]:
                item["last_seen"] = str(row["last_seen"] or "")
            scope_key = str(row["scope_id"] or "")
            if scope_key and scope_key not in item["groups"]:
                item["groups"].append(scope_key)
        if labels:
            for item in merged.values():
                item["groups"] = [str(labels.get(scope_id, scope_id)) or scope_id
                                  for scope_id in item.get("groups", [])]
        ordered = sorted(merged.values(), key=lambda x: x.get("updated_at") or "", reverse=True)
        return ordered[:limit]

    async def latest_summaries_by_scope(
        self, scope_ids: Sequence[str], *, levels: Sequence[int] = (2, 3),
        per_scope: int = 1,
    ) -> dict[str, list[SummaryRecord]]:
        """每个会话最新的高层摘要（默认 L2/L3）——给"各群一句话近况"用。

        一次查询搞定所有会话（窗口函数），避免 N 个会话 N 次查询。
        """
        scopes = [s for s in dict.fromkeys(scope_ids) if s]
        valid = [x for x in (levels or ()) if x in {1, 2, 3}]
        if not scopes or not valid:
            return {}
        marks = ",".join("?" for _ in scopes)
        level_marks = ",".join("?" for _ in valid)
        rows = await self._fetchall(
            "SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY scope_id "
            "ORDER BY level DESC, end_seq DESC) AS rn FROM summary_nodes "
            f"WHERE scope_id IN ({marks}) AND status='active' AND level IN ({level_marks})) "
            "WHERE rn <= ?",
            (*scopes, *valid, max(1, int(per_scope))),
        )
        grouped: dict[str, list[SummaryRecord]] = {}
        for row in rows:
            record = SummaryRecord(
                str(row["summary_id"]), str(row["scope_id"]), int(row["level"]),
                int(row["start_seq"]), int(row["end_seq"]), str(row["title"]),
                str(row["body"]), json.loads(row["topics_json"]), [],
                str(row["created_at"]))
            grouped.setdefault(record.scope_id, []).append(record)
        return grouped

    async def scope_activity(self, scope_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """每个会话的活跃度：最后一条消息时间 + 总条数。"""
        scopes = [s for s in dict.fromkeys(scope_ids) if s]
        if not scopes:
            return {}
        marks = ",".join("?" for _ in scopes)
        rows = await self._fetchall(
            "SELECT mi.scope_id AS sid, MAX(mr.occurred_at) AS last_at, COUNT(*) AS total "
            "FROM message_current mc JOIN message_identities mi ON mi.message_id=mc.message_id "
            "JOIN message_revisions mr ON mr.revision_id=mc.revision_id "
            f"WHERE mi.scope_id IN ({marks}) AND mc.is_deleted=0 GROUP BY mi.scope_id",
            (*scopes,),
        )
        return {str(row["sid"]): {"last_active": str(row["last_at"] or ""),
                                  "messages": int(row["total"] or 0)} for row in rows}

    # -------------------------------------------- 群风格 / 黑话 / 人物档案 / 情绪记忆
    async def upsert_style_rule(self, scope_id: str, situation: str, style: str, *,
                                evidence_message_id: str = "") -> dict[str, Any]:
        """记一条群说话规律（同群同「情境+说法」命中就 hits+1）。"""
        situation, style = situation.strip()[:40], style.strip()[:40]
        if not situation or not style:
            raise ValueError("situation/style 不能为空")
        now = utc_now()
        rule_id = uuid.uuid5(uuid.NAMESPACE_URL,
                             f"style\0{scope_id}\0{situation}\0{style}").hex
        async with self._write_lock:
            await self._conn().execute(
                "INSERT INTO group_style_rules(rule_id,scope_id,situation,style,"
                "evidence_message_id,hits,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?) "
                "ON CONFLICT(scope_id,situation,style) DO UPDATE SET hits=group_style_rules.hits+1,"
                "evidence_message_id=CASE WHEN excluded.evidence_message_id!='' "
                "THEN excluded.evidence_message_id ELSE group_style_rules.evidence_message_id END,"
                "updated_at=excluded.updated_at",
                (rule_id, scope_id, situation, style, evidence_message_id[:64], now, now))
        return {"rule_id": rule_id, "situation": situation, "style": style}

    async def list_style_rules(self, scope_ids: "str | Sequence[str]",
                               limit: int = 12) -> list[dict[str, Any]]:
        scopes = [scope_ids] if isinstance(scope_ids, str) else [s for s in scope_ids if s]
        if not scopes:
            return []
        marks = ",".join("?" for _ in scopes)
        rows = await self._fetchall(
            "SELECT rule_id,scope_id,situation,style,evidence_message_id,hits,updated_at "
            f"FROM group_style_rules WHERE scope_id IN ({marks}) "
            "ORDER BY hits DESC, updated_at DESC LIMIT ?",
            (*scopes, _limit(limit, 60)))
        return [{"rule_id": str(r["rule_id"]), "scope_id": str(r["scope_id"]),
                 "situation": str(r["situation"]), "style": str(r["style"]),
                 "evidence_message_id": str(r["evidence_message_id"]),
                 "hits": int(r["hits"] or 1), "updated_at": str(r["updated_at"])}
                for r in rows]

    async def delete_style_rule(self, rule_id: str) -> int:
        async with self._write_lock:
            cursor = await self._conn().execute(
                "DELETE FROM group_style_rules WHERE rule_id=?", (str(rule_id),))
            return int(cursor.rowcount or 0)

    async def upsert_lexicon(self, scope_id: str, term: str, meaning: str, *,
                             confidence: float = 0.5, evidence_message_id: str = "",
                             bump_use: bool = False) -> dict[str, Any]:
        """记/更新一个群内词条（黑话）。meaning 为空表示"还没搞懂"。"""
        term = term.strip()[:24]
        if not term:
            raise ValueError("term 不能为空")
        now = utc_now()
        term_id = uuid.uuid5(uuid.NAMESPACE_URL, f"lex\0{scope_id}\0{term}").hex
        async with self._write_lock:
            await self._conn().execute(
                "INSERT INTO group_lexicon(term_id,scope_id,term,meaning,confidence,"
                "evidence_message_id,first_seen,last_seen,uses,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,1,?) "
                "ON CONFLICT(scope_id,term) DO UPDATE SET "
                "meaning=CASE WHEN excluded.meaning!='' THEN excluded.meaning "
                "ELSE group_lexicon.meaning END,"
                "confidence=CASE WHEN excluded.confidence>0 THEN excluded.confidence "
                "ELSE group_lexicon.confidence END,"
                "evidence_message_id=CASE WHEN excluded.evidence_message_id!='' "
                "THEN excluded.evidence_message_id ELSE group_lexicon.evidence_message_id END,"
                "last_seen=excluded.last_seen,"
                "uses=group_lexicon.uses+?, updated_at=excluded.updated_at",
                (term_id, scope_id, term, meaning.strip()[:200], float(confidence),
                 evidence_message_id[:64], now, now, now, 1 if bump_use else 0))
        return {"term_id": term_id, "term": term, "meaning": meaning}

    async def list_lexicon(self, scope_ids: "str | Sequence[str]",
                           limit: int = 20, *, known_only: bool = True) -> list[dict[str, Any]]:
        scopes = [scope_ids] if isinstance(scope_ids, str) else [s for s in scope_ids if s]
        if not scopes:
            return []
        marks = ",".join("?" for _ in scopes)
        clause = " AND meaning!=''" if known_only else ""
        rows = await self._fetchall(
            "SELECT term_id,scope_id,term,meaning,confidence,uses,first_seen,last_seen "
            f"FROM group_lexicon WHERE scope_id IN ({marks}){clause} "
            "ORDER BY uses DESC, last_seen DESC LIMIT ?",
            (*scopes, _limit(limit, 100)))
        return [{"term_id": str(r["term_id"]), "scope_id": str(r["scope_id"]),
                 "term": str(r["term"]), "meaning": str(r["meaning"]),
                 "confidence": float(r["confidence"] or 0), "uses": int(r["uses"] or 1),
                 "first_seen": str(r["first_seen"]), "last_seen": str(r["last_seen"])}
                for r in rows]

    async def get_lexicon_term(self, scope_id: str, term: str) -> dict[str, Any] | None:
        row = await self._fetchone(
            "SELECT term_id,scope_id,term,meaning,confidence,uses FROM group_lexicon "
            "WHERE scope_id=? AND term=?", (scope_id, str(term).strip()[:24]))
        if row is None:
            return None
        return {"term_id": str(row["term_id"]), "scope_id": str(row["scope_id"]),
                "term": str(row["term"]), "meaning": str(row["meaning"]),
                "confidence": float(row["confidence"] or 0), "uses": int(row["uses"] or 1)}

    async def delete_lexicon(self, term_id: str) -> int:
        async with self._write_lock:
            cursor = await self._conn().execute(
                "DELETE FROM group_lexicon WHERE term_id=?", (str(term_id),))
            return int(cursor.rowcount or 0)

    async def upsert_person_profile(self, user_id: str, *, display_name: str = "",
                                    points: "Sequence[str] | None" = None) -> dict[str, Any]:
        """人物心理档案（跨群一份）：要点按「分类:内容:权重」存，认识次数累加。"""
        user_id = str(user_id).strip()
        if not user_id:
            raise ValueError("user_id 不能为空")
        now = utc_now()
        person_id = uuid.uuid5(uuid.NAMESPACE_URL, f"person\0{user_id}").hex
        existing = await self.get_person_profile(user_id)
        merged = list(existing.get("points") or [])
        if points:
            from .style_learning import merge_points

            merged = merge_points(merged, points)
        async with self._write_lock:
            await self._conn().execute(
                "INSERT INTO person_profiles(person_id,user_id,display_name,points_json,"
                "know_counts,first_known,last_known,updated_at) VALUES(?,?,?,?,1,?,?,?) "
                "ON CONFLICT(person_id) DO UPDATE SET "
                "display_name=CASE WHEN excluded.display_name!='' THEN excluded.display_name "
                "ELSE person_profiles.display_name END,"
                "points_json=excluded.points_json, know_counts=person_profiles.know_counts+1,"
                "last_known=excluded.last_known, updated_at=excluded.updated_at",
                (person_id, user_id, str(display_name or "")[:60],
                 json.dumps(merged, ensure_ascii=False), now, now, now))
        return {"person_id": person_id, "user_id": user_id, "points": merged}

    async def get_person_profile(self, user_id: str) -> dict[str, Any]:
        person_id = uuid.uuid5(uuid.NAMESPACE_URL, f"person\0{str(user_id).strip()}").hex
        row = await self._fetchone(
            "SELECT person_id,user_id,display_name,points_json,know_counts,first_known,"
            "last_known,updated_at FROM person_profiles WHERE person_id=?", (person_id,))
        if row is None:
            return {}
        try:
            points = json.loads(row["points_json"] or "[]")
        except Exception:
            points = []
        return {"person_id": str(row["person_id"]), "user_id": str(row["user_id"]),
                "display_name": str(row["display_name"]), "points": list(points),
                "know_counts": int(row["know_counts"] or 0),
                "first_known": str(row["first_known"]), "last_known": str(row["last_known"]),
                "updated_at": str(row["updated_at"])}

    async def list_person_profiles(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT person_id,user_id,display_name,points_json,know_counts,first_known,"
            "last_known,updated_at FROM person_profiles ORDER BY last_known DESC LIMIT ?",
            (_limit(limit, 100),))
        out = []
        for row in rows:
            try:
                points = json.loads(row["points_json"] or "[]")
            except Exception:
                points = []
            out.append({"person_id": str(row["person_id"]), "user_id": str(row["user_id"]),
                        "display_name": str(row["display_name"]), "points": list(points),
                        "know_counts": int(row["know_counts"] or 0),
                        "last_known": str(row["last_known"])})
        return out

    async def delete_person_profile(self, person_id: str) -> int:
        async with self._write_lock:
            cursor = await self._conn().execute(
                "DELETE FROM person_profiles WHERE person_id=?", (str(person_id),))
            return int(cursor.rowcount or 0)

    async def add_mood_event(self, scope_id: str, user_id: str, valence: float,
                             arousal: float, reason: str, message_id: str = "") -> str:
        event_id = uuid.uuid4().hex
        async with self._write_lock:
            await self._conn().execute(
                "INSERT INTO mood_events(event_id,scope_id,user_id,valence,arousal,reason,"
                "message_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (event_id, scope_id, str(user_id), float(valence), float(arousal),
                 str(reason)[:120], str(message_id)[:64], utc_now()))
        return event_id

    async def recent_mood_events(self, scope_ids: "str | Sequence[str] | None" = None,
                                 limit: int = 8) -> list[dict[str, Any]]:
        scopes = None
        if isinstance(scope_ids, str):
            scopes = [scope_ids]
        elif scope_ids:
            scopes = [s for s in scope_ids if s]
        args: list[Any] = []
        clause = ""
        if scopes:
            clause = " WHERE scope_id IN (" + ",".join("?" for _ in scopes) + ")"
            args.extend(scopes)
        rows = await self._fetchall(
            "SELECT event_id,scope_id,user_id,valence,arousal,reason,message_id,created_at "
            "FROM mood_events" + clause + " ORDER BY created_at DESC LIMIT ?",
            (*args, _limit(limit, 60)))
        return [{"event_id": str(r["event_id"]), "scope_id": str(r["scope_id"]),
                 "user_id": str(r["user_id"]), "valence": float(r["valence"] or 0),
                 "arousal": float(r["arousal"] or 0), "reason": str(r["reason"]),
                 "created_at": str(r["created_at"])} for r in rows]

    async def delete_mood_event(self, event_id: str) -> int:
        async with self._write_lock:
            cursor = await self._conn().execute(
                "DELETE FROM mood_events WHERE event_id=?", (str(event_id),))
            return int(cursor.rowcount or 0)

    # ---------------------------------------------------------- 提示词注入
    async def list_prompt_injections(self) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT injection_id,name,content,enabled,builtin,sort_order,created_at,updated_at "
            "FROM prompt_injections ORDER BY sort_order, created_at")
        return [
            {"injection_id": str(row["injection_id"]), "name": str(row["name"]),
             "content": str(row["content"]), "enabled": bool(row["enabled"]),
             "builtin": bool(row["builtin"]), "sort_order": int(row["sort_order"]),
             "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"])}
            for row in rows
        ]

    async def upsert_prompt_injection(
        self, injection_id: str, name: str, content: str, *,
        enabled: bool = True, builtin: bool = False, sort_order: int = 0,
    ) -> dict[str, Any]:
        key = str(injection_id or "").strip()
        if not key:
            raise ValueError("injection_id 不能为空")
        now = utc_now()
        async with self._write_lock:
            await self._conn().execute(
                "INSERT INTO prompt_injections(injection_id,name,content,enabled,builtin,"
                "sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(injection_id) DO UPDATE SET name=excluded.name,"
                "content=excluded.content,enabled=excluded.enabled,"
                "sort_order=excluded.sort_order,updated_at=excluded.updated_at",
                (key, str(name)[:60], str(content)[:4000], 1 if enabled else 0,
                 1 if builtin else 0, int(sort_order), now, now))
        return {"injection_id": key, "name": str(name)[:60],
                "content": str(content)[:4000], "enabled": bool(enabled),
                "builtin": bool(builtin), "sort_order": int(sort_order)}

    async def delete_prompt_injection(self, injection_id: str) -> int:
        async with self._write_lock:
            cursor = await self._conn().execute(
                "DELETE FROM prompt_injections WHERE injection_id=?", (str(injection_id),))
            return int(cursor.rowcount or 0)

    async def seed_prompt_injections(self, presets: "Sequence[Mapping[str, Any]]") -> int:
        """把自带预设补进库（已存在的不动——用户改过的内容不该被覆盖）。"""
        existing = {row["injection_id"] for row in await self.list_prompt_injections()}
        added = 0
        for order, item in enumerate(presets):
            key = str(item.get("id", "")).strip()
            if not key or key in existing:
                continue
            await self.upsert_prompt_injection(
                key, str(item.get("name", key)), str(item.get("content", "")),
                enabled=bool(item.get("enabled", False)),
                builtin=True, sort_order=order)
            added += 1
        return added

    async def recent_topics(self, scope_id: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT title,hits,last_seq,created_at FROM group_topics WHERE scope_id=? ORDER BY last_seq DESC LIMIT ?",
            (scope_id, _limit(limit, 30)),
        )
        return [
            {"topic": str(row["title"]), "hits": int(row["hits"]),
             "last_seq": int(row["last_seq"]), "first_seen": str(row["created_at"])}
            for row in rows
        ]

    async def unconsumed_summaries(self, scope_id: str, level: int, consumed_by_level: int, limit: int = 5) -> list[SummaryRecord]:
        """Active summaries of `level` not yet used as input by `consumed_by_level`."""
        rows = await self._fetchall(
            "SELECT n.* FROM summary_nodes n WHERE n.scope_id=? AND n.level=? AND n.status='active' "
            "AND NOT EXISTS(SELECT 1 FROM summary_inputs i JOIN summary_nodes p ON p.summary_id=i.summary_id "
            "WHERE i.input_summary_id=n.summary_id AND p.level=? AND p.status='active') "
            "ORDER BY n.end_seq ASC LIMIT ?",
            (scope_id, int(level), int(consumed_by_level), _limit(limit, 30)),
        )
        result: list[SummaryRecord] = []
        for row in rows:
            citations = await self._fetchall("SELECT message_id FROM summary_citations WHERE summary_id=? ORDER BY message_id", (row["summary_id"],))
            result.append(SummaryRecord(str(row["summary_id"]), str(row["scope_id"]), int(row["level"]), int(row["start_seq"]), int(row["end_seq"]), str(row["title"]), str(row["body"]), json.loads(row["topics_json"]), [str(x["message_id"]) for x in citations], str(row["created_at"])))
        return result

    async def delete_message(self, scope_id: str, message_id: str) -> DeletionResult:
        return await self._delete_where(scope_id, "mi.message_id=?", (message_id,))

    async def delete_user(self, sender_id: str, scope_id: str | None = None) -> DeletionResult:
        return await self._delete_where(scope_id, "EXISTS(SELECT 1 FROM message_revisions owned WHERE owned.message_id=mi.message_id AND owned.sender_id=?)", (sender_id,))

    async def delete_range(self, start: str, end: str, scope_id: str | None = None) -> DeletionResult:
        return await self._delete_where(scope_id, "EXISTS(SELECT 1 FROM message_revisions ranged WHERE ranged.message_id=mi.message_id AND ranged.occurred_at>=? AND ranged.occurred_at<=?)", (start, end))

    async def delete_group(self, scope_id: str) -> DeletionResult:
        scope = await self._fetchone("SELECT 1 FROM scopes WHERE scope_id=?", (scope_id,))
        if scope is None:
            return DeletionResult(0)
        rows = await self._fetchall("SELECT message_id FROM message_identities WHERE scope_id=?", (scope_id,))
        if rows:
            return await self._delete_where(scope_id, "mi.scope_id=?", (scope_id,), remove_scope=True, enqueue_rebuild=False)
        async with self._write_lock:
            await self._conn().execute("DELETE FROM scopes WHERE scope_id=?", (scope_id,))
            await self._conn().commit()
        return DeletionResult(0)

    async def delete_all(self) -> DeletionResult:
        rows = await self._fetchall("SELECT message_id FROM message_identities")
        media = await self._fetchall("SELECT path FROM stickers")
        async with self._write_lock:
            db = self._conn()
            await self._begin_write(db)
            try:
                await db.execute("DELETE FROM scopes")
                await db.execute("DELETE FROM stickers")
                await db.execute("DELETE FROM jobs")
                await db.execute("DELETE FROM audit_logs")
                # 实录（用户）：点了"清空全部记忆"，情绪/任务/日程还留着。
                # 记忆相关的一律清干净——scopes 靠外键级联带走消息/摘要/话题/印象/好感/风格，
                # 下面这些是各自独立的表，要显式删。
                for table in ("agent_plans", "tasks", "mood_events", "person_profiles",
                              "group_style_rules", "group_lexicon"):
                    try:
                        await db.execute(f"DELETE FROM {table}")
                    except Exception:
                        pass          # 表可能还没建（老库/功能关着）
                if self.fts5_available:
                    await db.execute("DELETE FROM message_fts")
                else:
                    await db.execute("DELETE FROM message_search")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return DeletionResult(len(rows), [row["path"] for row in media])

    async def _delete_where(self, scope_id: str | None, clause: str, params: Sequence[Any], remove_scope: bool = False, enqueue_rebuild: bool = True) -> DeletionResult:
        conditions = [clause]
        args: list[Any] = list(params)
        if scope_id is not None:
            conditions.append("mi.scope_id=?")
            args.append(scope_id)
        rows = await self._fetchall("SELECT DISTINCT mi.message_id,mi.scope_id FROM message_identities mi WHERE " + " AND ".join(conditions), args)
        if not rows:
            return DeletionResult(0)
        by_scope: dict[str, list[str]] = {}
        for row in rows:
            by_scope.setdefault(str(row["scope_id"]), []).append(str(row["message_id"]))
        db = self._conn()
        media_paths: list[str] = []
        async with self._write_lock:
            await self._begin_write(db)
            try:
                for owner_scope, ids in by_scope.items():
                    marks = ",".join("?" for _ in ids)
                    media = await self._fetchall(f"SELECT DISTINCT s.path FROM stickers s JOIN sticker_sources ss ON ss.sticker_id=s.sticker_id WHERE ss.message_id IN ({marks}) AND NOT EXISTS(SELECT 1 FROM sticker_sources keep WHERE keep.sticker_id=s.sticker_id AND keep.message_id NOT IN ({marks}))", (*ids, *ids))
                    media_paths.extend(str(row["path"]) for row in media)
                    await self._purge_derived(owner_scope, ids, enqueue_rebuild=enqueue_rebuild)
                    event_rows = await self._fetchall(f"SELECT event_id FROM message_revisions WHERE message_id IN ({marks})", ids)
                    event_ids = [str(row["event_id"]) for row in event_rows]
                    if self.fts5_available:
                        await db.execute(f"DELETE FROM message_fts WHERE message_id IN ({marks})", ids)
                    else:
                        await db.execute(f"DELETE FROM message_search WHERE message_id IN ({marks})", ids)
                    await db.execute(f"DELETE FROM message_identities WHERE message_id IN ({marks})", ids)
                    if event_ids:
                        event_marks = ",".join("?" for _ in event_ids)
                        await db.execute(f"DELETE FROM event_headers WHERE event_id IN ({event_marks})", event_ids)
                    if remove_scope:
                        await db.execute("DELETE FROM scopes WHERE scope_id=?", (owner_scope,))
                await db.execute("DELETE FROM stickers WHERE NOT EXISTS(SELECT 1 FROM sticker_sources ss WHERE ss.sticker_id=stickers.sticker_id)")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return DeletionResult(len(rows), media_paths)

    async def _purge_derived(self, scope_id: str, message_ids: Sequence[str], enqueue_rebuild: bool = True) -> None:
        if not message_ids:
            return
        db = self._conn()
        marks = ",".join("?" for _ in message_ids)
        roots = await self._fetchall(f"SELECT DISTINCT summary_id FROM summary_inputs WHERE input_message_id IN ({marks}) UNION SELECT DISTINCT summary_id FROM summary_citations WHERE message_id IN ({marks})", (*message_ids, *message_ids))
        invalid = {str(row["summary_id"]) for row in roots}
        frontier = list(invalid)
        while frontier:
            marks2 = ",".join("?" for _ in frontier)
            rows = await self._fetchall(f"SELECT DISTINCT summary_id FROM summary_inputs WHERE input_summary_id IN ({marks2})", frontier)
            frontier = [str(row["summary_id"]) for row in rows if str(row["summary_id"]) not in invalid]
            invalid.update(frontier)
        affected_memory = await self._fetchall(f"SELECT DISTINCT memory_id FROM memory_evidence WHERE message_id IN ({marks})", message_ids)
        memory_ids = [str(row["memory_id"]) for row in affected_memory]
        if invalid:
            await db.execute("DELETE FROM embeddings WHERE scope_id=? AND owner_type='summary' AND owner_id IN (" + ",".join("?" for _ in invalid) + ")", (scope_id, *invalid))
            await db.execute("DELETE FROM summary_nodes WHERE scope_id=? AND summary_id IN (" + ",".join("?" for _ in invalid) + ")", (scope_id, *invalid))
        if memory_ids:
            memory_marks = ",".join("?" for _ in memory_ids)
            await db.execute(f"DELETE FROM embeddings WHERE scope_id=? AND owner_type='memory' AND owner_id IN ({memory_marks})", (scope_id, *memory_ids))
            await db.execute(f"DELETE FROM memory_items WHERE scope_id=? AND memory_id IN ({memory_marks})", (scope_id, *memory_ids))
        await db.execute(f"DELETE FROM embeddings WHERE scope_id=? AND owner_type='message' AND owner_id IN ({marks})", (scope_id, *message_ids))
        await db.execute("DELETE FROM jobs WHERE scope_id=?", (scope_id,))
        if enqueue_rebuild:
            await self._enqueue_job_tx(scope_id, "rebuild", {"reason": "source_deleted", "message_ids": list(message_ids)}, f"rebuild:{scope_id}:{uuid.uuid4().hex}")

    async def _invalidate_derived(self, scope_id: str, message_ids: Sequence[str]) -> None:
        if not message_ids:
            return
        db = self._conn()
        marks = ",".join("?" for _ in message_ids)
        roots = await self._fetchall(f"SELECT DISTINCT summary_id FROM summary_inputs WHERE input_message_id IN ({marks})", message_ids)
        invalid = {str(row["summary_id"]) for row in roots}
        frontier = list(invalid)
        while frontier:
            marks2 = ",".join("?" for _ in frontier)
            rows = await self._fetchall(f"SELECT DISTINCT summary_id FROM summary_inputs WHERE input_summary_id IN ({marks2})", frontier)
            frontier = [str(row["summary_id"]) for row in rows if str(row["summary_id"]) not in invalid]
            invalid.update(frontier)
        if invalid:
            await db.execute("UPDATE summary_nodes SET status='invalid',body='',title='',topics_json='[]',manifest_json='{}' WHERE scope_id=? AND summary_id IN (" + ",".join("?" for _ in invalid) + ")", (scope_id, *invalid))
            await db.execute("DELETE FROM summary_inputs WHERE summary_id IN (" + ",".join("?" for _ in invalid) + ")", tuple(invalid))
            await db.execute("DELETE FROM summary_citations WHERE summary_id IN (" + ",".join("?" for _ in invalid) + ")", tuple(invalid))
        await db.execute(f"DELETE FROM embeddings WHERE scope_id=? AND owner_type='message' AND owner_id IN ({marks})", (scope_id, *message_ids))
        memory_rows = await self._fetchall(f"SELECT DISTINCT memory_id FROM memory_evidence WHERE message_id IN ({marks})", message_ids)
        memory_ids = [str(row["memory_id"]) for row in memory_rows]
        if memory_ids:
            memory_marks = ",".join("?" for _ in memory_ids)
            await db.execute(f"DELETE FROM catalog_entries WHERE memory_id IN ({memory_marks})", memory_ids)
            await db.execute(f"DELETE FROM memory_revisions WHERE memory_id IN ({memory_marks})", memory_ids)
            await db.execute(f"UPDATE memory_items SET status='invalid',current_revision_id=NULL,updated_at=? WHERE scope_id=? AND memory_id IN ({memory_marks})", (utc_now(), scope_id, *memory_ids))
        await db.execute(f"DELETE FROM memory_evidence WHERE message_id IN ({marks})", message_ids)
        await db.execute("DELETE FROM jobs WHERE scope_id=?", (scope_id,))
        await self._enqueue_job_tx(scope_id, "rebuild", {"reason": "source_changed", "message_ids": list(message_ids)}, f"rebuild:{scope_id}:{uuid.uuid4().hex}")

    async def enqueue_job(self, kind: str, payload: Mapping[str, Any], scope_id: str | None = None, idempotency_key: str | None = None) -> str:
        async with self._write_lock:
            job_id = await self._enqueue_job_tx(scope_id, kind, payload, idempotency_key)
            await self._conn().commit()
            return job_id

    async def _enqueue_job_tx(self, scope_id: str | None, kind: str, payload: Mapping[str, Any], idempotency_key: str | None = None) -> str:
        if scope_id is not None:
            row = await self._fetchone("SELECT 1 FROM scopes WHERE scope_id=?", (scope_id,))
            if row is None:
                raise ValueError("unknown scope")
        job_id = uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key).hex if idempotency_key else uuid.uuid4().hex
        now = utc_now()
        await self._conn().execute("INSERT INTO jobs(job_id,scope_id,kind,payload_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(job_id) DO NOTHING", (job_id, scope_id, kind[:100], _json(dict(payload)), "pending", now, now))
        return job_id

    async def lease_job(self, worker: str, lease_seconds: int = 60, kinds: Sequence[str] | None = None) -> dict[str, Any] | None:
        db, now = self._conn(), datetime.now(timezone.utc)
        clauses, args = ["(status='pending' OR (status='leased' AND lease_until<?))"], [now.isoformat()]
        if kinds:
            clauses.append("kind IN (" + ",".join("?" for _ in kinds) + ")")
            args.extend(kinds)
        async with self._write_lock:
            await self._begin_write(db)
            try:
                row = await self._fetchone("SELECT * FROM jobs WHERE " + " AND ".join(clauses) + " ORDER BY created_at LIMIT 1", args)
                if row is None:
                    await db.rollback()
                    return None
                lease_until = (now + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
                await db.execute("UPDATE jobs SET status='leased',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE job_id=?", (worker, lease_until, now.isoformat(), row["job_id"]))
                await db.commit()
                result = dict(row)
                result.update({"status": "leased", "lease_owner": worker, "lease_until": lease_until, "payload": json.loads(row["payload_json"])})
                return result
            except BaseException:
                await db.rollback()
                raise

    async def complete_job(self, job_id: str, worker: str) -> bool:
        return await self._finish_job(job_id, worker, "completed", None)

    async def fail_job(self, job_id: str, worker: str, error: str, retry: bool = True) -> bool:
        return await self._finish_job(job_id, worker, "pending" if retry else "failed", error[:2000])

    async def _finish_job(self, job_id: str, worker: str, status: str, error: str | None) -> bool:
        async with self._write_lock:
            cursor = await self._conn().execute("UPDATE jobs SET status=?,lease_owner=NULL,lease_until=NULL,last_error=?,updated_at=? WHERE job_id=? AND status='leased' AND lease_owner=?", (status, error, utc_now(), job_id, worker))
            await self._conn().commit()
            return cursor.rowcount == 1

    async def write_audit(self, actor_id: str, action: str, target: str, result: str, metadata: Mapping[str, Any] | None = None, scope_id: str | None = None) -> str:
        audit_id = uuid.uuid4().hex
        async with self._write_lock:
            await self._conn().execute("INSERT INTO audit_logs VALUES(?,?,?,?,?,?,?,?)", (audit_id, scope_id, actor_id, action, target, result, _json(dict(metadata or {})), utc_now()))
            await self._conn().commit()
        return audit_id

    async def cleanup_media(self, paths: Sequence[str | Path]) -> list[Path]:
        removed: list[Path] = []
        for value in dict.fromkeys(Path(item) for item in paths):
            try:
                value.unlink()
                removed.append(value)
            except FileNotFoundError:
                continue
        return removed

    async def save_sticker(self, sha256: str, path: str | Path, mime_type: str, size_bytes: int, metadata: Mapping[str, Any] | None = None, message_id: str | None = None, scope_id: str | None = None) -> str:
        if message_id:
            if not scope_id:
                raise ValueError("scope_id is required for a sticker source")
            await self._validate_owned(scope_id, "message_identities", "message_id", [message_id])
        db, sticker_id = self._conn(), uuid.uuid4().hex
        async with self._write_lock:
            await db.execute("INSERT INTO stickers VALUES(?,?,?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET path=excluded.path,mime_type=excluded.mime_type,size_bytes=excluded.size_bytes,metadata_json=excluded.metadata_json", (sticker_id, sha256, str(path), mime_type, int(size_bytes), _json(dict(metadata or {})), utc_now()))
            row = await self._fetchone("SELECT sticker_id FROM stickers WHERE sha256=?", (sha256,))
            sticker_id = str(row["sticker_id"])
            if message_id:
                await db.execute("INSERT OR IGNORE INTO sticker_sources VALUES(?,?)", (sticker_id, message_id))
            await db.commit()
        return sticker_id

    async def list_stickers(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self._fetchall("SELECT * FROM stickers ORDER BY created_at DESC LIMIT ?", (_limit(limit, 1000),))
        return [{**dict(row), "metadata": json.loads(row["metadata_json"])} for row in rows]

    async def remove_sticker(self, sticker_id: str) -> Path | None:
        row = await self._fetchone("SELECT path FROM stickers WHERE sticker_id=?", (sticker_id,))
        if row is None:
            return None
        async with self._write_lock:
            await self._conn().execute("DELETE FROM stickers WHERE sticker_id=?", (sticker_id,))
            await self._conn().commit()
        return Path(str(row["path"]))

    async def orphan_sticker_paths(self, remove_records: bool = False) -> list[Path]:
        rows = await self._fetchall("SELECT sticker_id,path FROM stickers WHERE NOT EXISTS(SELECT 1 FROM sticker_sources ss WHERE ss.sticker_id=stickers.sticker_id)")
        if rows and remove_records:
            async with self._write_lock:
                await self._conn().executemany("DELETE FROM stickers WHERE sticker_id=?", [(row["sticker_id"],) for row in rows])
                await self._conn().commit()
        return [Path(str(row["path"])) for row in rows]

    async def export_scope(self, scope_id: str) -> dict[str, Any]:
        scope = await self._fetchone("SELECT * FROM scopes WHERE scope_id=?", (scope_id,))
        if scope is None:
            raise KeyError(scope_id)
        messages = await self.recent_messages(scope_id, 200)
        summaries = await self.list_summaries(scope_id, 30)
        catalog = await self._fetchall("SELECT * FROM catalog_entries WHERE scope_id=? ORDER BY updated_at", (scope_id,))
        return {"scope": dict(scope), "messages": [_stored_dict(item) for item in messages], "summaries": [dict(summary_id=item.summary_id, level=item.level, start_seq=item.start_seq, end_seq=item.end_seq, title=item.title, body=item.body, topics=item.topics, citations=item.citations) for item in summaries], "catalog": [dict(row) for row in catalog]}

    async def request_rebuild(self, scope_id: str, reason: str = "manual") -> str:
        return await self.enqueue_job("rebuild", {"reason": reason[:500]}, scope_id, f"rebuild:{scope_id}:{reason}")

    # ---------------------------------------------------------- 记忆条目：人工 / bot 自改
    async def recent_catalog(self, scope_ids: "Sequence[str]",
                             limit: int = 20) -> list[Any]:
        """最近的记忆账本条目（给 bot 的 memory_edit list 用）。"""
        scopes = [s for s in dict.fromkeys(scope_ids or []) if s]
        if not scopes:
            return []
        marks = ",".join("?" for _ in scopes)
        rows = await self._fetchall(
            f"SELECT * FROM catalog_entries WHERE scope_id IN ({marks}) "
            "AND status='active' ORDER BY updated_at DESC LIMIT ?",
            (*scopes, _limit(limit, 100)))
        return [_ledger_entry(row) for row in rows]

    async def search_catalog(self, scope_ids: "Sequence[str]", query: str,
                             limit: int = 20) -> list[Any]:
        """按关键词搜记忆条目（标题或内容命中）。"""
        scopes = [s for s in dict.fromkeys(scope_ids or []) if s]
        keyword = str(query or "").strip()
        if not scopes or not keyword:
            return []
        marks = ",".join("?" for _ in scopes)
        like = f"%{_escape_like(keyword)}%"
        rows = await self._fetchall(
            f"SELECT * FROM catalog_entries WHERE scope_id IN ({marks}) "
            "AND status='active' AND (subject LIKE ? ESCAPE '\\' "
            "OR value LIKE ? ESCAPE '\\') ORDER BY updated_at DESC LIMIT ?",
            (*scopes, like, like, _limit(limit, 100)))
        return [_ledger_entry(row) for row in rows]

    async def edit_catalog_entry(self, entry_id: str, *, subject: str | None = None,
                                 value: str | None = None, note: str = "") -> bool:
        """改一条记忆（写修订记录，审计可追）。subject/value 给 None 表示不动那项。"""
        entry_id = str(entry_id or "").strip()
        if not entry_id:
            return False
        row = await self._fetchone(
            "SELECT memory_id, subject, value FROM catalog_entries WHERE entry_id=?",
            (entry_id,))
        if row is None:
            return False
        memory_id = str(row["memory_id"])
        new_subject = str(row["subject"]) if subject is None else str(subject)[:200]
        new_value = str(row["value"]) if value is None else str(value)[:2000]
        now = utc_now()
        db = self._conn()
        async with self._write_lock:
            await self._begin_write(db)
            try:
                await db.execute(
                    "UPDATE catalog_entries SET subject=?, value=?, updated_at=? "
                    "WHERE entry_id=?", (new_subject, new_value, now, entry_id))
                await db.execute(
                    "UPDATE memory_items SET subject=?, updated_at=? WHERE memory_id=?",
                    (new_subject, now, memory_id))
                await db.execute(
                    "INSERT INTO memory_revisions(memory_revision_id, memory_id, revision_no, "
                    "value, operation, created_at) SELECT ?, ?, COALESCE(MAX(revision_no),0)+1, "
                    "?, ?, ? FROM memory_revisions WHERE memory_id=?",
                    (f"rev-{uuid.uuid4().hex[:16]}", memory_id, new_value,
                     (f"edit:{note}" if note else "edit")[:120], now, memory_id))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return True

    async def forget_catalog_entry(self, entry_id: str, *, note: str = "") -> bool:
        """"忘掉"一条记忆：条目标记 archived（不再进上下文），但保留可追溯的修订记录。

        不直接 DELETE：删掉就查不到"它曾经记过什么"，而记忆错的场景恰恰需要回溯。
        """
        entry_id = str(entry_id or "").strip()
        if not entry_id:
            return False
        row = await self._fetchone(
            "SELECT memory_id FROM catalog_entries WHERE entry_id=?", (entry_id,))
        if row is None:
            return False
        memory_id = str(row["memory_id"])
        now = utc_now()
        db = self._conn()
        async with self._write_lock:
            await self._begin_write(db)
            try:
                await db.execute(
                    "UPDATE catalog_entries SET status='archived', updated_at=? "
                    "WHERE entry_id=?", (now, entry_id))
                await db.execute(
                    "UPDATE memory_items SET status='archived', updated_at=? "
                    "WHERE memory_id=?", (now, memory_id))
                await db.execute(
                    "INSERT INTO memory_revisions(memory_revision_id, memory_id, revision_no, "
                    "value, operation, created_at) SELECT ?, ?, COALESCE(MAX(revision_no),0)+1, "
                    "'', ?, ? FROM memory_revisions WHERE memory_id=?",
                    (f"rev-{uuid.uuid4().hex[:16]}", memory_id,
                     (f"forget:{note}" if note else "forget")[:120], now, memory_id))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return True

    # ---------------------------------------------------------- 记忆导出 / 导入
    async def export_memory(self, *, scope_ids: "Sequence[str] | None" = None) -> dict[str, list[dict[str, Any]]]:
        """按表导出记忆行（打包在 src/memory_export.py，那里也定义了表清单）。

        scope_ids 给了就只导这些会话（按会话导；不给=全导）。
        """
        from .memory_export import TABLES

        wanted = {str(item) for item in scope_ids} if scope_ids else None
        tables: dict[str, list[dict[str, Any]]] = {}
        for name, _keys in TABLES:
            try:
                rows = await self._fetchall(f"SELECT * FROM {name}")
            except Exception:
                rows = []
            out: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                if wanted is not None:
                    scope = item.get("scope_id")
                    if scope is not None and str(scope) not in wanted:
                        continue
                out.append(item)
            tables[name] = out
        if wanted is not None:
            # 全局表（不属于任何会话）按"跟着包走"处理：注入与立指令是用户自己的东西
            for name in ("prompt_injections", "standing_intents", "agent_todos", "agent_plans"):
                keep = []
                for row in tables.get(name, []):
                    scope = row.get("scope_id")
                    if name in ("prompt_injections",) or scope in (None, "") \
                            or str(scope) in wanted:
                        keep.append(row)
                tables[name] = keep
        return tables

    async def import_memory(self, tables: Mapping[str, Any], *,
                            embed_ok: bool = False,
                            embed_note: str = "") -> dict[str, Any]:
        """把导出的记忆行合并进库（去重、按会话平移 scope_seq）。

        - 会话按 (平台/账号/会话) 认领：同一条会话在目标库里是同一个 scope；
        - 消息按 (scope, 上游消息 id) 去重：重复导入同一份不会重复计数；
         - `scope_seq` 按目标库的 next_seq 整体平移（摘要的起止序号一起平移，
          所以摘要与消息的对应关系保持），源库里已删消息留下的空洞不影响；
        - `embed_ok=False`（嵌入模型不匹配）时**不导向量**，只导文本与理解层数据。
        """
        from .memory_export import TABLES, remap_row

        source = {str(name): [dict(row) for row in (tables.get(name) or [])]
                  for name, _keys in TABLES}
        report: dict[str, Any] = {"tables": {}, "messages": 0, "reused_messages": 0,
                                  "scopes": 0, "embeddings_skipped": 0,
                                  "embeddings": 0, "embed_note": str(embed_note or "")}
        db = self._conn()

        def bump(table: str, *, added: int = 0, skipped: int = 0) -> None:
            slot = report["tables"].setdefault(table, {"added": 0, "skipped": 0})
            slot["added"] += added
            slot["skipped"] += skipped

        async with self._write_lock:
            await self._begin_write(db)
            try:
                # ---- 1) 会话：按自然键认领（scope_id 是 uuid5，同一会话天然同一个 id）
                scope_map: dict[str, str] = {}
                for row in source.get("scopes", []):
                    platform = str(row.get("platform") or "")
                    account = str(row.get("account_id") or "")
                    conversation = str(row.get("conversation_id") or "")
                    if not (platform and account and conversation):
                        continue
                    await self._ensure_scope(platform, account, conversation,
                                             str(row.get("display_name") or ""))
                    resolved = await self._fetchone(
                        "SELECT scope_id FROM scopes WHERE platform=? AND account_id=? "
                        "AND conversation_id=?", (platform, account, conversation))
                    if resolved is None:
                        continue
                    scope_map[str(row.get("scope_id") or "")] = str(resolved["scope_id"])
                    report["scopes"] += 1
                # ---- 2) 序号平移：目标 next_seq - 源最小 seq（同一条会话整体搬）
                seq_shift: dict[str, int] = {}
                for old_id, new_id in scope_map.items():
                    seqs = [int(row["scope_seq"]) for row in source.get("event_headers", [])
                            if str(row.get("scope_id")) == old_id
                            and row.get("scope_seq") is not None]
                    if not seqs:
                        seq_shift[new_id] = 0
                        continue
                    current = await self._fetchone(
                        "SELECT next_seq FROM scopes WHERE scope_id=?", (new_id,))
                    next_seq = int(current["next_seq"]) if current else 1
                    seq_shift[new_id] = max(0, next_seq - min(seqs))
                # ---- 3) 事件头 → 消息身份 → 修订/当前（顺序即外键顺序）
                live_events: set[str] = set()
                for row in source.get("event_headers", []):
                    mapped = remap_row(row, scope_map=scope_map, message_map={},
                                       seq_shift=seq_shift, seq_columns=("scope_seq",))
                    if mapped is None:
                        bump("event_headers", skipped=1)
                        continue
                    mapped = self._drop_unknown_columns("event_headers", mapped)
                    cursor = await db.execute(
                        self._insert_sql("event_headers", mapped, ignore=True),
                        tuple(mapped.values()))
                    if cursor.rowcount:
                        live_events.add(str(mapped.get("event_id")))
                        bump("event_headers", added=1)
                    else:
                        bump("event_headers", skipped=1)
                message_map: dict[str, str] = {}
                fresh_messages: list[dict[str, Any]] = []
                fresh_text: dict[str, str] = {}
                # message_current/revisions 没有 scope_id 列 → 平移量按"这条消息
                # 属于哪个会话"记一份（按旧 message_id 索引）
                message_shift: dict[str, int] = {}
                for row in source.get("message_identities", []):
                    old_id = str(row.get("message_id") or "")
                    # message_id 在这里是这行自己的身份，不是引用 → 不参与映射
                    mapped = remap_row(row, scope_map=scope_map, message_map={},
                                       self_keys=("message_id",))
                    if mapped is None:
                        bump("message_identities", skipped=1)
                        continue
                    mapped = self._drop_unknown_columns("message_identities", mapped)
                    cursor = await db.execute(
                        self._insert_sql("message_identities", mapped, ignore=True),
                        tuple(mapped.values()))
                    scope_id = str(mapped.get("scope_id") or "")
                    message_shift[old_id] = int(seq_shift.get(scope_id, 0))
                    if cursor.rowcount:
                        message_map[old_id] = old_id
                        fresh_messages.append(mapped)
                        report["messages"] += 1
                        bump("message_identities", added=1)
                    else:
                        existing = await self._fetchone(
                            "SELECT message_id FROM message_identities "
                            "WHERE scope_id=? AND upstream_message_id=?",
                            (scope_id, str(mapped.get("upstream_message_id") or "")))
                        if existing is not None:
                            message_map[old_id] = str(existing["message_id"])
                        report["reused_messages"] += 1
                        bump("message_identities", skipped=1)
                for table in ("message_revisions", "message_current"):
                    for row in source.get(table, []):
                        if str(row.get("event_id") or "") not in live_events \
                                and table == "message_revisions":
                            bump(table, skipped=1)
                            continue
                        old_message_id = str(row.get("message_id") or "")
                        mapped = remap_row(row, scope_map=scope_map, message_map=message_map)
                        if mapped is None or str(mapped.get("message_id")) not in message_map.values():
                            bump(table, skipped=1)
                            continue
                        # 这两张表没有 scope_id 列：平移量按"这条消息属于哪个会话"取
                        if mapped.get("scope_seq") is not None:
                            mapped["scope_seq"] = (int(mapped["scope_seq"])
                                                   + message_shift.get(old_message_id, 0))
                        mapped = self._drop_unknown_columns(table, mapped)
                        cursor = await db.execute(
                            self._insert_sql(table, mapped, ignore=True), tuple(mapped.values()))
                        if cursor.rowcount and table == "message_revisions":
                            fresh_text[str(mapped.get("message_id") or "")] =                                 str(mapped.get("text") or "")
                        bump(table, added=1 if cursor.rowcount else 0,
                             skipped=0 if cursor.rowcount else 1)
                # ---- 4) 其余表：按映射改写后插入（主键冲突=已有，跳过）
                for name, keys in TABLES:
                    if name in ("scopes", "event_headers", "message_identities",
                                "message_revisions", "message_current", "embeddings"):
                        continue
                    for row in source.get(name, []):
                        mapped = remap_row(row, scope_map=scope_map, message_map=message_map,
                                           seq_shift=seq_shift, seq_columns=("last_seq",))
                        if mapped is None:
                            bump(name, skipped=1)
                            continue
                        mapped = self._drop_unknown_columns(name, mapped)
                        cursor = await db.execute(
                            self._insert_sql(name, mapped, ignore=True), tuple(mapped.values()))
                        bump(name, added=1 if cursor.rowcount else 0,
                             skipped=0 if cursor.rowcount else 1)
                # ---- 5) 向量（只有嵌入模型一致才导；否则明说跳过了多少条）
                embedding_rows = source.get("embeddings", [])
                if not embed_ok:
                    report["embeddings_skipped"] = len(embedding_rows)
                else:
                    for row in embedding_rows:
                        mapped = remap_row(row, scope_map=scope_map, message_map=message_map)
                        if mapped is None:
                            report["embeddings_skipped"] += 1
                            continue
                        mapped = self._drop_unknown_columns("embeddings", mapped)
                        cursor = await db.execute(
                            self._insert_sql("embeddings", mapped, ignore=True),
                            tuple(mapped.values()))
                        if cursor.rowcount:
                            report["embeddings"] += 1
                        else:
                            report["embeddings_skipped"] += 1
                # ---- 6) 搜索索引 + 会话游标
                for mapped in fresh_messages:
                    message_id = str(mapped.get("message_id") or "")
                    await self._sync_search(message_id, str(mapped.get("scope_id") or ""),
                                            fresh_text.get(message_id, ""), False)
                for old_id, new_id in scope_map.items():
                    rows = [int(row["scope_seq"]) + seq_shift.get(new_id, 0)
                            for row in source.get("event_headers", [])
                            if str(row.get("scope_id")) == old_id
                            and row.get("scope_seq") is not None]
                    if not rows:
                        continue
                    await db.execute(
                        "UPDATE scopes SET next_seq=? WHERE scope_id=? AND next_seq<?",
                        (max(rows) + 1, new_id, max(rows) + 1))
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return report

    @staticmethod
    def _insert_sql(table: str, row: Mapping[str, Any], *, ignore: bool = False) -> str:
        columns = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        verb = "INSERT OR IGNORE" if ignore else "INSERT"
        return f"{verb} INTO {table}({columns}) VALUES({marks})"

    def _drop_unknown_columns(self, table: str, row: Mapping[str, Any]) -> dict[str, Any]:
        """只保留目标表真实存在的列（包与库版本不一致时不至于整行失败）。"""
        allowed = _TABLE_COLUMNS.get(table)
        if not allowed:
            return dict(row)
        return {key: value for key, value in row.items() if key in allowed}

    async def _add_scope_tag_column(self) -> None:
        """老库补 `scopes.tag`（群标签：这个群是干什么的）。"""
        try:
            await self._conn().execute("ALTER TABLE scopes ADD COLUMN tag TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass          # 列已存在
        try:
            await self._conn().commit()
        except Exception:
            pass

    async def set_scope_tag(self, scope_id: str, tag: str) -> bool:
        """写一个会话的标签（控制台用）。"""
        if not str(scope_id or "").strip():
            return False
        async with self._write_lock:
            cursor = await self._conn().execute(
                "UPDATE scopes SET tag=? WHERE scope_id=?",
                (str(tag or "").strip()[:200], str(scope_id)))
            await self._conn().commit()
        return bool(cursor.rowcount)

    async def get_scope_tag(self, scope_id: str) -> str:
        row = await self._fetchone("SELECT tag FROM scopes WHERE scope_id=?", (str(scope_id),))
        return str(row["tag"] or "") if row is not None else ""

    async def all_scope_tags(self, platform: str | None = None) -> dict[str, str]:
        """scope_id → 标签（只返回有标签的）。"""
        if platform:
            rows = await self._fetchall(
                "SELECT scope_id, tag FROM scopes WHERE platform=? AND tag != ''", (platform,))
        else:
            rows = await self._fetchall("SELECT scope_id, tag FROM scopes WHERE tag != ''")
        return {str(row["scope_id"]): str(row["tag"]) for row in rows}

    async def _load_table_columns(self) -> None:
        """把各表的真实列名缓存下来（导入时用来过滤多余字段）。"""
        for name in ("scopes", "event_headers", "message_identities", "message_revisions",
                     "message_current", "summary_nodes", "summary_inputs",
                     "summary_citations", "memory_items", "memory_revisions",
                     "memory_evidence", "catalog_entries", "user_impressions",
                     "user_styles", "user_affinity", "person_profiles",
                     "group_style_rules", "group_lexicon", "group_topics",
                     "mood_events", "embeddings", "prompt_injections",
                     "standing_intents", "agent_todos", "agent_plans"):
            try:
                rows = await self._fetchall(f"PRAGMA table_info({name})")
            except Exception:
                continue
            _TABLE_COLUMNS[name] = frozenset(str(row["name"]) for row in rows)


    async def backup(self, destination: str | Path) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        source = self._conn()
        async with self._write_lock:
            await source.commit()
            backup_db = await aiosqlite.connect(target)
            try:
                await source.backup(backup_db)
            finally:
                await backup_db.close()
        return target

    async def stats(self, scope_id: str | None = None) -> dict[str, Any]:
        suffix, params = (" WHERE scope_id=?", (scope_id,)) if scope_id else ("", ())
        result: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "fts5": self.fts5_available}
        for name in ("scopes", "message_identities", "summary_nodes", "memory_items", "catalog_entries", "jobs", "embeddings"):
            where = suffix if name != "scopes" else ((" WHERE scope_id=?") if scope_id else "")
            row = await self._fetchone(f"SELECT COUNT(*) AS n FROM {name}{where}", params)
            result[name] = int(row["n"])
        return result

    async def _sync_search(self, message_id: str, scope_id: str, text: str, deleted: bool) -> None:
        db = self._conn()
        if self.fts5_available:
            await db.execute("DELETE FROM message_fts WHERE message_id=?", (message_id,))
            if not deleted:
                await db.execute("INSERT INTO message_fts(message_id,scope_id,text) VALUES(?,?,?)", (message_id, scope_id, text))
        else:
            if deleted:
                await db.execute("DELETE FROM message_search WHERE message_id=?", (message_id,))
            else:
                await db.execute("INSERT INTO message_search VALUES(?,?,?) ON CONFLICT(message_id) DO UPDATE SET scope_id=excluded.scope_id,text=excluded.text", (message_id, scope_id, text))

    async def _validate_owned(self, scope_id: str, table: str, key: str, ids: Sequence[str]) -> None:
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        row = await self._fetchone(f"SELECT COUNT(*) AS n FROM {table} WHERE scope_id=? AND {key} IN ({marks})", (scope_id, *ids))
        if int(row["n"]) != len(set(ids)):
            raise ValueError("input references do not belong to scope")

    async def _fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        cursor = await self._conn().execute(sql, tuple(params))
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def _fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        cursor = await self._conn().execute(sql, tuple(params))
        try:
            return await cursor.fetchall()
        finally:
            await cursor.close()


def _stored_dict(value: StoredMessage) -> dict[str, Any]:
    return {"message_id": value.message_id, "revision_id": value.revision_id, "scope_seq": value.scope_seq, "upstream_message_id": value.upstream_message_id, "sender_id": value.sender_id, "sender_name": value.sender_name, "text": value.text, "occurred_at": value.occurred_at, "parts": value.parts, "reply_to": value.reply_to}


def _stored(row: aiosqlite.Row) -> StoredMessage:
    return StoredMessage(
        message_id=str(row["message_id"]), revision_id=str(row["revision_id"]), scope_id=str(row["scope_id"]),
        scope_seq=int(row["scope_seq"]), upstream_message_id=str(row["upstream_message_id"]), sender_id=str(row["sender_id"]),
        sender_name=str(row["sender_name"]), text=str(row["text"]), occurred_at=str(row["occurred_at"]),
        parts=json.loads(row["parts_json"]), reply_to=row["reply_to"],
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _event_dedupe_key(message: NormalizedMessage, payload_hash: str) -> str:
    raw_id = message.raw_event.get("event_id") or message.raw_event.get("post_id")
    if raw_id:
        return f"event:{raw_id}"
    stable = "\0".join((message.event_type, message.upstream_message_id, message.sender_id, message.occurred_at, payload_hash))
    return hashlib.sha256(stable.encode()).hexdigest()


def _ledger_entry(row: Any) -> Any:
    """catalog_entries 行 → CatalogEntry（复用 memory_ledger 的转换，保持形状一致）。"""
    from .memory_ledger import _entry

    return _entry(row)


def _limit(value: int, high: int) -> int:
    return min(max(int(value), 1), high)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
