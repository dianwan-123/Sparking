# -*- coding: utf-8 -*-
"""MemoryWeb（记忆网）：把记忆"具体化 + 网状化"的存储层。

树状结构（会话 → 类目 → 条目 → 证据）里，一条记忆只属于一个会话、一个父节点；
网状结构把记忆拆成**节点**与**连线**：

- 节点：人物 / 会话 / 话题 / 事实 / 摘要——同一人物、同一话题在不同会话里是**同一个节点**，
  于是"谁在哪个群说过什么"天然连成一张网；
- 连线：包含（摘要→事实）、关于（事实→话题）、提到（事实→人物）、参与者、压缩自（高层摘要→子摘要）、
  同现（人物↔人物）——压缩时关系随摘要**上卷**而不是丢失，检索时沿连线**一跳扩展**。

写入是"拉取式"：同步器扫描还没入网的来源（分层摘要 / 记忆账本 / 群话题 / 人物印象），
逐个转换（幂等，按来源打标）。所以旧库不需要数据迁移脚本——第一次同步就是迁移；
导入的记忆包也一样，导入完成后同步器自动把新数据织进网里。

分支 / 回溯 / 剪枝（控制台发起）：
- **分支**：在某个节点上打一个"时间切口"；
- **回溯**：把切口之后的记忆压缩成一份归档（不进上下文），删除原件、封住对应消息区间
  （封口后压缩管线不会再从旧消息里重新长出记忆），记忆基本回到切口时刻；
- **剪枝**：在所选节点之间的路径上，丢弃低价值叶子节点、把必要摘要向上压缩，
  压缩程度跟随「上下文详细程度」档位（越低越狠）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from astrbot.api import logger

from . import timeutil
from .models import CatalogEntry, SummaryRecord, utc_now
from .storage import Storage, _json

# 节点种类：人物 / 会话 / 话题 / 事实 / 摘要
KINDS = ("person", "group", "topic", "fact", "summary")
# 关系：包含 / 关于 / 提到 / 参与者 / 压缩自 / 同现 / 属于
RELATIONS = ("includes", "about", "mentions", "has_participant",
             "derives_from", "co_occurs", "in", "member_of")

_EDGE_PUNCT = re.compile(
    r"^[\s,.;:!?'\"，。；：！？、（）()\[\]{}<>《》【】\-—~·]+"
    r"|[\s,.;:!?'\"，。；：！？、（）()\[\]{}<>《》【】\-—~·]+$")
_WHITESPACE = re.compile(r"\s+")
_LIKE_SPECIAL = ("\\", "%", "_")


def canonical(value: Any) -> str:
    """实体归一化（去首尾标点、合并空白、ASCII 转小写）——同名同实体。"""
    text = _EDGE_PUNCT.sub("", str(value or "").strip())
    text = _WHITESPACE.sub(" ", text)
    if text.isascii():
        text = text.lower()
    return text[:160]


def fact_key(text: str) -> str:
    digest = hashlib.sha1(canonical(text).encode("utf-8")).hexdigest()[:20]
    return f"fact:{digest}"


def topic_key(topic: str) -> str:
    return f"topic:{canonical(topic)}"


def person_key(user_id: str, name: str) -> str:
    uid = str(user_id or "").strip()
    if uid:
        return f"person:{uid}"
    return f"person:name:{canonical(name)}"


def group_key(conversation_id: str) -> str:
    return f"group:{str(conversation_id or '').strip()}"


def summary_key(summary_id: str) -> str:
    return f"summary:{summary_id}"


def _escape_like(value: str) -> str:
    out = str(value)
    for char in _LIKE_SPECIAL:
        out = out.replace(char, "\\" + char)
    return out


def _items_from_body(body: str) -> list[tuple[str, str]]:
    """从摘要正文 JSON 里取出要点：[(类别, 文本)]。

    摘要的 body 是严格 JSON（facts/decisions/tasks/open_questions/conflicts），
    解析失败就当没有要点（绝不因坏数据炸掉同步）。
    """
    try:
        data = json.loads(str(body or ""))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str]] = []
    for kind, key in (("fact", "facts"), ("decision", "decisions"),
                      ("task", "tasks"), ("question", "open_questions"),
                      ("conflict", "conflicts")):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            text = ""
            if isinstance(item, dict):
                for name in ("text", "fact", "decision", "task", "value",
                             "summary", "detail", "question", "conflict"):
                    value = item.get(name)
                    if isinstance(value, str) and value.strip():
                        text = value.strip()
                        break
            elif isinstance(item, (str, int, float)):
                text = str(item).strip()
            if text:
                out.append((kind, text[:300]))
    return out


def _digest_from_summary(row: Mapping[str, Any]) -> str:
    """回溯存档用的一行摘要：标题 + 前两条要点（机械压缩，不依赖模型）。"""
    points = "；".join(text[:80] for _kind, text in _items_from_body(str(row.get("body") or ""))[:2])
    title = str(row.get("title") or "").strip()[:100]
    return f"{title}（{points}）" if points else title


def _chunks(values: Sequence[Any], size: int = 180) -> list[list[Any]]:
    return [list(values[index:index + size]) for index in range(0, len(values), size)]


class MemoryWeb:
    """记忆网的存储、同步、检索与维护。所有写操作走 Storage 的写锁。"""

    def __init__(self, storage: Storage, compressor: Any = None) -> None:
        self.storage = storage
        self.compressor = compressor
        self._scope_cache: dict[str, tuple[str, str]] = {}

    def set_compressor(self, compressor: Any) -> None:
        self.compressor = compressor

    # ---------------------------------------------------------------- 基础
    async def _set_meta(self, key: str, value: str) -> None:
        db = self.storage._conn()
        async with self.storage._write_lock:
            await self.storage._begin_write(db)
            try:
                await db.execute(
                    "INSERT INTO web_meta(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at", (key, str(value), utc_now()))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _meta(self) -> dict[str, str]:
        rows = await self.storage._fetchall("SELECT key,value FROM web_meta")
        return {str(row["key"]): str(row["value"]) for row in rows}

    async def _scope_info(self, scope_id: str) -> tuple[str, str]:
        """(会话号, 显示名)——节点键用会话号（跨库稳定），展示用显示名。"""
        key = str(scope_id or "")
        if key in self._scope_cache:
            return self._scope_cache[key]
        row = await self.storage._fetchone(
            "SELECT conversation_id, display_name FROM scopes WHERE scope_id=?",
            (key,))
        info = (str(row["conversation_id"]), str(row["display_name"])) if row else ("", "")
        if key:
            self._scope_cache[key] = info
        return info

    async def _source_done(self, source_ref: str) -> bool:
        row = await self.storage._fetchone(
            "SELECT 1 FROM web_sources WHERE source_ref=?", (source_ref,))
        return row is not None

    async def _senders_of(self, summary_id: str, limit: int = 30) -> list[tuple[str, str]]:
        rows = await self.storage._fetchall(
            "SELECT DISTINCT mr.sender_id, mr.sender_name FROM summary_citations sc "
            "JOIN message_revisions mr ON mr.message_id=sc.message_id "
            "WHERE sc.summary_id=? LIMIT ?", (summary_id, int(limit)))
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            uid = str(row["sender_id"] or "").strip()
            name = str(row["sender_name"] or "").strip() or uid
            if not name or (uid or name) in seen:
                continue
            seen.add(uid or name)
            out.append((uid, name))
        return out

    # ---------------------------------------------------------------- 写入
    async def _persist(self, source_ref: str, kind: str, scope_id: str,
                       nodes: Sequence[Mapping[str, Any]],
                       edges: Sequence[Mapping[str, Any]],
                       *, node_status: str = "active",
                       edge_status: str = "active") -> dict[str, str]:
        """一个来源一个事务：节点 upsert（权重按来源计）、连线插入、来源打标。

        批量做：节点先一条 SELECT 取齐已有项、连线走 executemany——
        逐条往返在"首次转换几万条来源"时会慢到分钟级（迁移是后台任务，
        但没必要慢）。
        """
        db = self.storage._conn()
        now = utc_now()
        # 同键节点先去重（后出现的覆盖标签/元数据，与逐条 upsert 的净效果一致）
        unique_nodes: dict[str, Mapping[str, Any]] = {}
        for spec in nodes:
            key = str(spec.get("key") or "")
            if key:
                unique_nodes[key] = spec
        node_ids: dict[str, str] = {}
        async with self.storage._write_lock:
            await self.storage._begin_write(db)
            try:
                existing: dict[str, str] = {}
                for chunk in _chunks(list(unique_nodes), 300):
                    marks = ",".join("?" for _ in chunk)
                    rows = await self.storage._fetchall(
                        "SELECT node_id, node_key FROM web_nodes "
                        f"WHERE node_key IN ({marks})", tuple(chunk))
                    for row in rows:
                        existing[str(row["node_key"])] = str(row["node_id"])
                for key, spec in unique_nodes.items():
                    label = str(spec.get("label") or "")[:200]
                    meta = _json(dict(spec.get("meta") or {}))
                    node_id = existing.get(key)
                    if node_id:
                        await db.execute(
                            "UPDATE web_nodes SET label=CASE WHEN ?<>'' THEN ? ELSE label END, "
                            "last_seen=?, meta_json=?, scope_id=CASE WHEN scope_id='' AND ?<>'' "
                            "THEN ? ELSE scope_id END WHERE node_id=?",
                            (label, label, now, meta, str(scope_id or ""),
                             str(scope_id or ""), node_id))
                    else:
                        node_id = uuid.uuid4().hex
                        # 权重从 0 起：紧跟的 node_sources 插入会把它加到 1（"被几个来源提到"）。
                        # 若这里写 1，单来源节点会变成 2——权重全表偏大，剪枝/排序都会判错。
                        await db.execute(
                            "INSERT INTO web_nodes(node_id,node_key,kind,label,canonical,"
                            "scope_id,weight,status,meta_json,first_seen,last_seen) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (node_id, key, str(spec.get("kind") or "fact"), label,
                             str(spec.get("canonical") or "")[:200], str(scope_id or ""),
                             0.0, node_status, meta, now, now))
                        existing[key] = node_id
                    node_ids[key] = node_id
                    cursor = await db.execute(
                        "INSERT OR IGNORE INTO web_node_sources(node_id,source_ref) "
                        "VALUES(?,?)", (node_id, source_ref))
                    if cursor.rowcount:
                        await db.execute(
                            "UPDATE web_nodes SET weight=weight+1 WHERE node_id=?",
                            (node_id,))
                edge_rows: list[tuple[Any, ...]] = []
                for spec in edges:
                    src = node_ids.get(str(spec.get("src") or ""))
                    dst = node_ids.get(str(spec.get("dst") or ""))
                    if not src or not dst or src == dst:
                        continue
                    relation = str(spec.get("relation") or "related")[:40]
                    edge_key = f"{spec.get('src')}|{relation}|{spec.get('dst')}|{source_ref}"
                    try:
                        confidence = float(spec.get("confidence", 0.8))
                    except (TypeError, ValueError):
                        confidence = 0.8
                    edge_rows.append((
                        uuid.uuid4().hex, edge_key, src, dst, relation, 1.0,
                        max(0.0, min(1.0, confidence)), source_ref,
                        str(scope_id or ""), edge_status,
                        _json(dict(spec.get("meta") or {})), now, now))
                for chunk in _chunks(edge_rows, 200):
                    await db.executemany(
                        "INSERT OR IGNORE INTO web_edges(edge_id,edge_key,src_id,dst_id,"
                        "relation,weight,confidence,source_ref,scope_id,status,meta_json,"
                        "first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", chunk)
                await db.execute(
                    "INSERT OR IGNORE INTO web_sources(source_ref,kind,scope_id,"
                    "ingested_at,meta_json) VALUES(?,?,?,?,?)",
                    (source_ref, kind, str(scope_id or ""), now, "{}"))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return node_ids

    async def ingest_summary(self, record: SummaryRecord) -> dict[str, Any]:
        """把一条分层摘要织进网：摘要节点 + 要点事实 + 话题 + 参与者 + 压缩来源。"""
        source_ref = summary_key(record.summary_id)
        if await self._source_done(source_ref):
            return {"ok": True, "skipped": True}
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        conversation, display = await self._scope_info(record.scope_id)
        gkey = group_key(conversation or record.scope_id)
        at = str(record.created_at or "")
        nodes.append({"key": gkey, "kind": "group",
                      "canonical": conversation or record.scope_id,
                      "label": display or conversation or "会话",
                      "meta": {"conversation": conversation}})
        skey = summary_key(record.summary_id)
        nodes.append({"key": skey, "kind": "summary", "canonical": record.summary_id,
                      "label": record.title[:160],
                      "meta": {"level": record.level, "at": at,
                               "range": [record.start_seq, record.end_seq]}})
        edges.append({"src": skey, "dst": gkey, "relation": "in", "confidence": 1.0})

        fact_pairs: list[tuple[str, str]] = []
        for kind, text in _items_from_body(record.body)[:16]:
            fkey = fact_key(text)
            fact_pairs.append((fkey, text))
            nodes.append({"key": fkey, "kind": "fact", "canonical": canonical(text),
                          "label": text[:200], "meta": {"kind": kind, "at": at}})
            edges.append({"src": skey, "dst": fkey, "relation": "includes",
                          "confidence": 0.9})
            edges.append({"src": fkey, "dst": gkey, "relation": "in",
                          "confidence": 0.9})
            lowered = canonical(text)
            for topic in record.topics[:8]:
                tc = canonical(topic)
                if tc and tc in lowered:
                    tkey = topic_key(topic)
                    nodes.append({"key": tkey, "kind": "topic", "canonical": tc,
                                  "label": str(topic)[:80], "meta": {}})
                    edges.append({"src": fkey, "dst": tkey, "relation": "about",
                                  "confidence": 0.8})
        for topic in record.topics[:8]:
            tc = canonical(topic)
            if not tc:
                continue
            tkey = topic_key(topic)
            nodes.append({"key": tkey, "kind": "topic", "canonical": tc,
                          "label": str(topic)[:80], "meta": {}})
            edges.append({"src": tkey, "dst": gkey, "relation": "in",
                          "confidence": 0.7})
            edges.append({"src": skey, "dst": tkey, "relation": "about",
                          "confidence": 0.7})

        persons = await self._senders_of(record.summary_id)
        for uid, name in persons[:10]:
            pkey = person_key(uid, name)
            nodes.append({"key": pkey, "kind": "person",
                          "canonical": uid or canonical(name),
                          "label": name[:60], "meta": {"uid": uid}})
            edges.append({"src": skey, "dst": pkey, "relation": "has_participant",
                          "confidence": 0.8})
            edges.append({"src": pkey, "dst": gkey, "relation": "in",
                          "confidence": 0.6})
            for fkey, text in fact_pairs:
                if name and name in text:
                    edges.append({"src": fkey, "dst": pkey, "relation": "mentions",
                                  "confidence": 0.75})
        person_keys = [person_key(uid, name) for uid, name in persons[:6]]
        for index, first in enumerate(person_keys):
            for second in person_keys[index + 1:]:
                edges.append({"src": first, "dst": second, "relation": "co_occurs",
                              "confidence": 0.5})

        children = await self.storage._fetchall(
            "SELECT i.input_summary_id AS sid, n.title AS title FROM summary_inputs i "
            "LEFT JOIN summary_nodes n ON n.summary_id=i.input_summary_id "
            "WHERE i.summary_id=? AND i.input_summary_id IS NOT NULL LIMIT 12",
            (record.summary_id,))
        for row in children:
            child = str(row["sid"] or "")
            if not child:
                continue
            ckey = summary_key(child)
            nodes.append({"key": ckey, "kind": "summary", "canonical": child,
                          "label": str(row["title"] or child[:12])[:160],
                          "meta": {"level": max(1, record.level - 1)}})
            edges.append({"src": skey, "dst": ckey, "relation": "derives_from",
                          "confidence": 0.9})

        await self._persist(source_ref, "summary", record.scope_id, nodes, edges)
        return {"ok": True, "nodes": len(nodes), "edges": len(edges)}

    async def ingest_ledger(self, entry: CatalogEntry) -> dict[str, Any]:
        """把一条账本记忆织进网：事实节点 + 主题 + 所属会话。"""
        source_ref = f"ledger:{entry.entry_id}"
        if await self._source_done(source_ref):
            return {"ok": True, "skipped": True}
        text = str(entry.value or "").strip()
        if not text:
            return {"ok": True, "skipped": True}
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        conversation, display = await self._scope_info(entry.scope_id)
        gkey = group_key(conversation or entry.scope_id)
        nodes.append({"key": gkey, "kind": "group",
                      "canonical": conversation or entry.scope_id,
                      "label": display or conversation or "会话", "meta": {}})
        fkey = fact_key(text)
        nodes.append({"key": fkey, "kind": "fact", "canonical": canonical(text),
                      "label": text[:200],
                      "meta": {"kind": entry.kind, "subject": entry.subject,
                               "confidence": entry.confidence,
                               "at": entry.updated_at,
                               "evidence": len(entry.evidence_ids)}})
        edges.append({"src": fkey, "dst": gkey, "relation": "in", "confidence": 0.9})
        subject = str(entry.subject or "").strip()
        if subject:
            tkey = topic_key(subject)
            nodes.append({"key": tkey, "kind": "topic", "canonical": canonical(subject),
                          "label": subject[:80], "meta": {}})
            edges.append({"src": fkey, "dst": tkey, "relation": "about",
                          "confidence": 0.7})
            edges.append({"src": tkey, "dst": gkey, "relation": "in",
                          "confidence": 0.5})
        await self._persist(source_ref, "ledger", entry.scope_id, nodes, edges)
        return {"ok": True, "nodes": len(nodes), "edges": len(edges)}

    async def ingest_topic(self, row: Mapping[str, Any]) -> dict[str, Any]:
        topic_id = str(row.get("topic_id") or "")
        source_ref = f"topic:{topic_id}"
        if not topic_id or await self._source_done(source_ref):
            return {"ok": True, "skipped": True}
        title = str(row.get("title") or "").strip()
        if not title:
            return {"ok": True, "skipped": True}
        scope_id = str(row.get("scope_id") or "")
        conversation, display = await self._scope_info(scope_id)
        gkey = group_key(conversation or scope_id)
        tkey = topic_key(title)
        nodes = [
            {"key": gkey, "kind": "group", "canonical": conversation or scope_id,
             "label": display or conversation or "会话", "meta": {}},
            {"key": tkey, "kind": "topic", "canonical": canonical(title),
             "label": title[:80],
             "meta": {"hits": int(row.get("hits") or 1),
                      "at": str(row.get("created_at") or "")}},
        ]
        edges = [{"src": tkey, "dst": gkey, "relation": "in", "confidence": 0.6}]
        await self._persist(source_ref, "topic", scope_id, nodes, edges)
        return {"ok": True, "nodes": 2, "edges": 1}

    async def ingest_impression(self, row: Mapping[str, Any]) -> dict[str, Any]:
        scope_id = str(row.get("scope_id") or "")
        user_id = str(row.get("user_id") or "")
        source_ref = f"impression:{scope_id}:{user_id}"
        if not user_id or await self._source_done(source_ref):
            return {"ok": True, "skipped": True}
        display = str(row.get("display_name") or user_id)
        impression = str(row.get("impression") or "").strip()
        try:
            tags = json.loads(str(row.get("tags_json") or "[]"))
        except json.JSONDecodeError:
            tags = []
        conversation, scope_display = await self._scope_info(scope_id)
        gkey = group_key(conversation or scope_id)
        pkey = person_key(user_id, display)
        nodes = [
            {"key": gkey, "kind": "group", "canonical": conversation or scope_id,
             "label": scope_display or conversation or "会话", "meta": {}},
            {"key": pkey, "kind": "person", "canonical": user_id,
             "label": display[:60],
             "meta": {"uid": user_id, "tags": [str(x)[:20] for x in tags][:8],
                      "at": str(row.get("updated_at") or "")}},
        ]
        edges = [{"src": pkey, "dst": gkey, "relation": "member_of",
                  "confidence": 0.7}]
        if impression:
            fkey = fact_key(impression)
            nodes.append({"key": fkey, "kind": "fact", "canonical": canonical(impression),
                          "label": impression[:200],
                          "meta": {"kind": "impression", "at": str(row.get("updated_at") or "")}})
            edges.append({"src": fkey, "dst": pkey, "relation": "mentions",
                          "confidence": 0.8})
            edges.append({"src": fkey, "dst": gkey, "relation": "in",
                          "confidence": 0.7})
        await self._persist(source_ref, "impression", scope_id, nodes, edges)
        return {"ok": True, "nodes": len(nodes), "edges": len(edges)}

    # ---------------------------------------------------------------- 同步
    async def pending_count(self) -> dict[str, int]:
        async def _count(sql: str) -> int:
            row = await self.storage._fetchone(sql)
            return int(row["n"]) if row and row["n"] is not None else 0

        return {
            "summaries": await _count(
                "SELECT COUNT(*) AS n FROM summary_nodes s LEFT JOIN web_sources w "
                "ON w.source_ref='summary:'||s.summary_id "
                "WHERE w.source_ref IS NULL AND s.status='active'"),
            "ledger": await _count(
                "SELECT COUNT(*) AS n FROM catalog_entries c LEFT JOIN web_sources w "
                "ON w.source_ref='ledger:'||c.entry_id "
                "WHERE w.source_ref IS NULL AND c.status='active'"),
            "topics": await _count(
                "SELECT COUNT(*) AS n FROM group_topics t LEFT JOIN web_sources w "
                "ON w.source_ref='topic:'||t.topic_id WHERE w.source_ref IS NULL"),
            "impressions": await _count(
                "SELECT COUNT(*) AS n FROM user_impressions i LEFT JOIN web_sources w "
                "ON w.source_ref='impression:'||i.scope_id||':'||i.user_id "
                "WHERE w.source_ref IS NULL"),
        }

    async def sync_pending(self, limit: int = 200) -> dict[str, int]:
        """把还没入网的来源逐个转换进网（幂等；一次最多处理 limit 条/类）。"""
        limit = max(1, int(limit))
        report = {"summaries": 0, "ledger": 0, "topics": 0, "impressions": 0,
                  "total": 0, "failed": 0}
        rows = await self.storage._fetchall(
            "SELECT s.* FROM summary_nodes s LEFT JOIN web_sources w "
            "ON w.source_ref='summary:'||s.summary_id "
            "WHERE w.source_ref IS NULL AND s.status='active' "
            "ORDER BY s.created_at DESC LIMIT ?", (limit,))
        for row in rows:
            try:
                citations = await self.storage._fetchall(
                    "SELECT message_id FROM summary_citations WHERE summary_id=?",
                    (row["summary_id"],))
                record = SummaryRecord(
                    str(row["summary_id"]), str(row["scope_id"]), int(row["level"]),
                    int(row["start_seq"]), int(row["end_seq"]), str(row["title"]),
                    str(row["body"]), json.loads(str(row["topics_json"])),
                    [str(x["message_id"]) for x in citations], str(row["created_at"]))
                await self.ingest_summary(record)
                report["summaries"] += 1
            except Exception as error:
                report["failed"] += 1
                logger.warning("长程记忆：摘要入网失败（%s）：%s",
                               str(row["summary_id"])[:8], str(error)[:150])
        rows = await self.storage._fetchall(
            "SELECT c.* FROM catalog_entries c LEFT JOIN web_sources w "
            "ON w.source_ref='ledger:'||c.entry_id "
            "WHERE w.source_ref IS NULL AND c.status='active' "
            "ORDER BY c.updated_at DESC LIMIT ?", (limit,))
        for row in rows:
            try:
                entry = CatalogEntry(
                    str(row["entry_id"]), str(row["memory_id"]), str(row["scope_id"]),
                    str(row["kind"]), str(row["subject"]), str(row["value"]),
                    str(row["status"]), float(row["confidence"]),
                    json.loads(str(row["evidence_json"])), str(row["updated_at"]))
                await self.ingest_ledger(entry)
                report["ledger"] += 1
            except Exception as error:
                report["failed"] += 1
                logger.warning("长程记忆：账本入网失败（%s）：%s",
                               str(row["entry_id"])[:8], str(error)[:150])
        rows = await self.storage._fetchall(
            "SELECT t.* FROM group_topics t LEFT JOIN web_sources w "
            "ON w.source_ref='topic:'||t.topic_id WHERE w.source_ref IS NULL "
            "ORDER BY t.last_seq DESC LIMIT ?", (limit,))
        for row in rows:
            try:
                await self.ingest_topic(dict(row))
                report["topics"] += 1
            except Exception as error:
                report["failed"] += 1
                logger.warning("长程记忆：话题入网失败：%s", str(error)[:150])
        rows = await self.storage._fetchall(
            "SELECT i.* FROM user_impressions i LEFT JOIN web_sources w "
            "ON w.source_ref='impression:'||i.scope_id||':'||i.user_id "
            "WHERE w.source_ref IS NULL ORDER BY i.updated_at DESC LIMIT ?",
            (limit,))
        for row in rows:
            try:
                await self.ingest_impression(dict(row))
                report["impressions"] += 1
            except Exception as error:
                report["failed"] += 1
                logger.warning("长程记忆：印象入网失败：%s", str(error)[:150])
        report["total"] = (report["summaries"] + report["ledger"]
                           + report["topics"] + report["impressions"])
        if report["total"]:
            await self._set_meta("last_sync_at", utc_now())
        return report

    async def migration_state(self) -> dict[str, Any]:
        meta = await self._meta()
        row = await self.storage._fetchone(
            "SELECT (SELECT COUNT(*) FROM summary_nodes) + "
            "(SELECT COUNT(*) FROM catalog_entries) AS n")
        legacy_sources = int(row["n"]) if row and row["n"] is not None else 0
        pending = await self.pending_count()
        migrated = meta.get("legacy_migrated") == "1"
        return {
            "legacy_migrated": migrated,
            "migrated_at": meta.get("migrated_at", ""),
            "last_sync_at": meta.get("last_sync_at", ""),
            "legacy_sources": legacy_sources,
            "pending": pending,
            "needs_migration": bool(legacy_sources) and not migrated,
        }

    async def migrate_legacy(self, max_sources: int = 30000) -> dict[str, Any]:
        """旧版记忆 → 记忆网：循环同步直到没有待处理来源（首次即"转换流程"）。"""
        started = timeutil.now()
        total = 0
        rounds = 0
        while total < max_sources:
            report = await self.sync_pending(limit=400)
            if not report["total"]:
                break
            total += report["total"]
            rounds += 1
            await asyncio.sleep(0)
        await self._set_meta("legacy_migrated", "1")
        await self._set_meta("migrated_at", utc_now())
        stats = await self.stats()
        logger.info("长程记忆：记忆网转换完成 —— %s 条来源（%s 轮），节点 %s / 连线 %s",
                    total, rounds, stats["nodes"], stats["edges"])
        return {"sources": total, "rounds": rounds, "stats": stats,
                "seconds": round((timeutil.now() - started).total_seconds(), 2)}

    async def rebuild(self) -> dict[str, Any]:
        """清空网并按当前记忆重建（面板「重建记忆网」）。分支保留、按节点键重新挂接。"""
        db = self.storage._conn()
        async with self.storage._write_lock:
            await self.storage._begin_write(db)
            try:
                for table in ("web_edges", "web_node_sources", "web_sources",
                              "web_nodes"):
                    await db.execute(f"DELETE FROM {table}")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        await self._set_meta("legacy_migrated", "0")
        report = await self.migrate_legacy()
        # 重建后节点是新的 id：按节点键把分支重新挂接（键对摘要/事实是稳定的）
        db = self.storage._conn()
        async with self.storage._write_lock:
            await self.storage._begin_write(db)
            try:
                await db.execute(
                    "UPDATE web_branches SET node_id=(SELECT node_id FROM web_nodes "
                    "WHERE node_key=web_branches.node_key) WHERE node_key<>'' AND "
                    "EXISTS(SELECT 1 FROM web_nodes WHERE node_key=web_branches.node_key)")
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return report

    # ---------------------------------------------------------------- 检索
    @staticmethod
    def _tokens(query: str) -> list[str]:
        text = str(query or "").strip()
        if not text:
            return []
        tokens: list[str] = []
        # 整句（够短时）+ 按标点/空白切段 + 中文双字组（对中文无空格分词）
        if 2 <= len(text) <= 16:
            tokens.append(text)
        segments = [seg for seg in re.split(r"[\s,，。;；:：!！?？、~～/\\|\[\]（）()\"'“”]+", text)
                    if len(seg) >= 2]
        tokens.extend(segments)
        for seg in list(segments) + ([text] if len(text) <= 24 else []):
            chinese = re.findall(r"[\u4e00-\u9fff]+", seg)
            for run in chinese:
                for index in range(len(run) - 1):
                    tokens.append(run[index:index + 2])
            ascii_words = re.findall(r"[A-Za-z0-9]{2,}", seg)
            tokens.extend(word.lower() for word in ascii_words)
        seen: set[str] = set()
        unique: list[str] = []
        for token in tokens:
            token = token.strip()
            if token and token not in seen:
                seen.add(token)
                unique.append(token)
        if not unique:
            # 单字中文查询（"猫""茶"）：中文单字就是词，按单字查
            unique = list(dict.fromkeys(re.findall(r"[一-鿿]", text)))[:4]
        return unique[:24]

    @staticmethod
    def _score(text: str, tokens: Sequence[str]) -> float:
        lowered = str(text or "").lower()
        score = 0.0
        for token in tokens:
            if len(token) >= 2 and token in lowered:
                score += 1.0 + min(len(token), 6) * 0.2
            elif (len(token) == 1 and "一" <= token <= "鿿"
                  and token in lowered):
                score += 0.6
        return score

    @staticmethod
    def _recency_bonus(at: str) -> float:
        moment = timeutil.parse(at)
        if moment is None:
            return 0.0
        days = (timeutil.now() - moment).total_seconds() / 86400.0
        return max(0.0, 0.6 - days * 0.02)

    async def search(self, query: str, scopes: Sequence[str] = (),
                     labels: Mapping[str, str] | None = None,
                     limit: int = 8) -> list[dict[str, Any]]:
        """关键词检索 + 一跳关系扩展：经人物/话题找到相关事实。"""
        tokens = self._tokens(query)
        if not tokens:
            return []
        scope_set = {str(s) for s in scopes if str(s)}
        labels = dict(labels or {})
        rows = await self.storage._fetchall(
            "SELECT node_id,kind,label,canonical,scope_id,weight,meta_json,last_seen "
            "FROM web_nodes WHERE status='active' AND kind IN ('fact','topic','person') "
            "ORDER BY weight DESC, last_seen DESC LIMIT 3000")
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            node = {
                "node_id": str(row["node_id"]), "kind": str(row["kind"]),
                "label": str(row["label"]), "canonical": str(row["canonical"]),
                "scope_id": str(row["scope_id"]), "weight": float(row["weight"]),
                "meta": _safe_meta(row["meta_json"]),
            }
            score = self._score(node["label"], tokens)
            if score <= 0:
                continue
            scored.append((score, node))
        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        top_nodes = [node for _score, node in scored[:14]]
        matched_ids = [node["node_id"] for node in top_nodes]
        via_map: dict[str, list[str]] = {}
        fact_ids: list[str] = []
        for node in top_nodes:
            if node["kind"] == "fact":
                fact_ids.append(node["node_id"])
                via_map.setdefault(node["node_id"], [])
        # 一跳扩展：话题/人物 → 相关事实
        if matched_ids:
            marks = ",".join("?" for _ in matched_ids)
            neighbor_rows = await self.storage._fetchall(
                f"SELECT e.src_id AS fact_id, e.dst_id AS node_id FROM web_edges e "
                f"JOIN web_nodes f ON f.node_id=e.src_id AND f.kind='fact' "
                f"AND f.status='active' WHERE e.status='active' "
                f"AND e.relation IN ('about','mentions') AND e.dst_id IN ({marks}) "
                f"LIMIT 400", tuple(matched_ids))
            node_by_id = {node["node_id"]: node for node in top_nodes}
            for row in neighbor_rows:
                fact_id = str(row["fact_id"])
                node_id = str(row["node_id"])
                if fact_id not in fact_ids:
                    fact_ids.append(fact_id)
                via = via_map.setdefault(fact_id, [])
                label = node_by_id.get(node_id, {}).get("label")
                if label and label not in via:
                    via.append(label)
        if not fact_ids:
            return []
        fact_rows = await self.storage._fetchall(
            f"SELECT node_id,label,scope_id,weight,meta_json,last_seen FROM web_nodes "
            f"WHERE status='active' AND kind='fact' AND node_id IN "
            f"({','.join('?' for _ in fact_ids)})", tuple(fact_ids))
        out: list[dict[str, Any]] = []
        for row in fact_rows:
            scope_id = str(row["scope_id"])
            if scope_set and scope_id and scope_id not in scope_set:
                continue
            meta = _safe_meta(row["meta_json"])
            at = str(meta.get("at") or "")
            score = self._score(str(row["label"]), tokens)
            bonus = float(row["weight"]) * 0.5 + self._recency_bonus(at)
            out.append({
                "fact": str(row["label"])[:200],
                "kind": str(meta.get("kind") or "fact"),
                "at": timeutil.to_text(at, "%m-%d %H:%M") if at else "",
                "origin": str(labels.get(scope_id) or scope_id[:8] or "别处"),
                "scope_id": scope_id,
                "weight": float(row["weight"]),
                "via": via_map.get(str(row["node_id"]), [])[:4],
                "_rank": score * 2.0 + bonus,
            })
        out.sort(key=lambda item: item["_rank"], reverse=True)
        for item in out:
            item.pop("_rank", None)
        return out[:max(1, int(limit))]

    # ---------------------------------------------------------------- 面板
    async def stats(self) -> dict[str, Any]:
        rows = await self.storage._fetchall(
            "SELECT kind, COUNT(*) AS n FROM web_nodes WHERE status='active' "
            "GROUP BY kind")
        kinds = {str(row["kind"]): int(row["n"]) for row in rows}
        edge = await self.storage._fetchone(
            "SELECT COUNT(*) AS n FROM web_edges WHERE status='active'")
        sources = await self.storage._fetchone(
            "SELECT COUNT(*) AS n FROM web_sources")
        pruned = await self.storage._fetchone(
            "SELECT COUNT(*) AS n FROM web_nodes WHERE status='pruned'")
        meta = await self._meta()
        return {
            "nodes": sum(kinds.values()),
            "edges": int(edge["n"]) if edge else 0,
            "sources": int(sources["n"]) if sources else 0,
            "pruned": int(pruned["n"]) if pruned else 0,
            "kinds": kinds,
            "last_sync_at": meta.get("last_sync_at", ""),
            "migrated_at": meta.get("migrated_at", ""),
        }

    async def snapshot(self, *, scope_id: str = "", kinds: Sequence[str] = (),
                       include_archived: bool = False,
                       limit_nodes: int = 260,
                       limit_edges: int = 900) -> dict[str, Any]:
        """给面板用的图快照：节点（按权重取前 N）+ 聚合后的连线 + 统计。"""
        statuses = ["active"]
        if include_archived:
            statuses.append("archived")
        conditions = ["n.status IN (" + ",".join("?" for _ in statuses) + ")"]
        args: list[Any] = list(statuses)
        wanted = [str(kind) for kind in kinds if str(kind) in KINDS]
        if wanted:
            conditions.append("n.kind IN (" + ",".join("?" for _ in wanted) + ")")
            args.extend(wanted)
        if scope_id:
            conditions.append(
                "(n.scope_id=? OR n.node_id IN (SELECT src_id FROM web_edges "
                "WHERE scope_id=? UNION SELECT dst_id FROM web_edges WHERE scope_id=?))")
            args.extend([scope_id, scope_id, scope_id])
        rows = await self.storage._fetchall(
            "SELECT n.* FROM web_nodes n WHERE " + " AND ".join(conditions) +
            " ORDER BY n.weight DESC, n.last_seen DESC LIMIT ?",
            (*args, max(20, int(limit_nodes))))
        nodes = [_node_view(row) for row in rows]
        node_ids = {node["id"] for node in nodes}
        # 连线按"当前这批节点"取：每块先捞两端任一端命中的边，再过滤两端都在集合里，
        # 同一对节点按出现次数聚合（sources = 有多少条来源提到这条关系）。
        # 注意去重：跨块的一条边会被两个块各捞到一次。
        aggregated: dict[tuple[str, str, str], dict[str, Any]] = {}
        seen_edges: set[str] = set()
        connected: set[str] = set()
        if node_ids:
            status_marks = ",".join("?" for _ in statuses)
            for chunk in _chunks(list(node_ids), 200):
                marks = ",".join("?" for _ in chunk)
                edge_rows = await self.storage._fetchall(
                    "SELECT edge_id,src_id,dst_id,relation,weight,status FROM web_edges "
                    f"WHERE status IN ({status_marks}) AND (src_id IN ({marks}) "
                    f"OR dst_id IN ({marks})) ORDER BY rowid DESC LIMIT ?",
                    (*statuses, *chunk, *chunk, 6000))
                for row in edge_rows:
                    edge_id = str(row["edge_id"])
                    if edge_id in seen_edges:
                        continue
                    seen_edges.add(edge_id)
                    src, dst = str(row["src_id"]), str(row["dst_id"])
                    if src not in node_ids or dst not in node_ids:
                        continue
                    connected.add(src)
                    connected.add(dst)
                    key = (src, dst, str(row["relation"]))
                    slot = aggregated.setdefault(key, {
                        "source": src, "target": dst, "relation": str(row["relation"]),
                        "weight": 0.0, "sources": 0, "archived": True,
                    })
                    slot["weight"] += float(row["weight"])
                    slot["sources"] += 1
                    if str(row["status"]) == "active":
                        slot["archived"] = False
        # 这批节点里"一个连线都没有"的孤立点不画：满屏孤零零的圆点看着像"没连上"。
        # （用原始扫描的 connected 判断，不受 limit_edges 截断影响。）
        if connected:
            nodes = [node for node in nodes if node["id"] in connected]
        edges = sorted(aggregated.values(),
                       key=lambda item: (item["weight"], item["sources"]),
                       reverse=True)[:max(50, int(limit_edges))]
        scopes = await self.storage._fetchall(
            "SELECT scope_id,conversation_id,display_name FROM scopes "
            "ORDER BY display_name LIMIT 200")
        branches = await self.list_branches()
        stats = await self.stats()
        stats["pending"] = await self.pending_count()
        return {
            "nodes": nodes,
            "edges": edges,
            "stats": stats,
            "branches": branches,
            "scopes": [{"scope_id": str(row["scope_id"]),
                        "conversation_id": str(row["conversation_id"]),
                        "display_name": str(row["display_name"]) or str(row["conversation_id"])}
                       for row in scopes],
            "relations": list(RELATIONS),
        }

    async def node_detail(self, node_id: str) -> dict[str, Any]:
        row = await self.storage._fetchone(
            "SELECT * FROM web_nodes WHERE node_id=?", (str(node_id or ""),))
        if row is None:
            raise ValueError("节点不存在")
        node = _node_view(row)
        edge_rows = await self.storage._fetchall(
            "SELECT e.*, s.label AS src_label, s.kind AS src_kind, "
            "d.label AS dst_label, d.kind AS dst_kind FROM web_edges e "
            "JOIN web_nodes s ON s.node_id=e.src_id "
            "JOIN web_nodes d ON d.node_id=e.dst_id "
            "WHERE e.status='active' AND (e.src_id=? OR e.dst_id=?) LIMIT 80",
            (node["id"], node["id"]))
        edges = [{
            "relation": str(row["relation"]),
            "weight": float(row["weight"]),
            "direction": "out" if str(row["src_id"]) == node["id"] else "in",
            "other_id": str(row["dst_id"] if str(row["src_id"]) == node["id"]
                            else row["src_id"]),
            "other_label": str(row["dst_label"] if str(row["src_id"]) == node["id"]
                               else row["src_label"]),
            "other_kind": str(row["dst_kind"] if str(row["src_id"]) == node["id"]
                              else row["src_kind"]),
        } for row in edge_rows]
        source_rows = await self.storage._fetchall(
            "SELECT source_ref FROM web_node_sources WHERE node_id=? LIMIT 24",
            (node["id"],))
        sources = []
        for item in source_rows:
            ref = str(item["source_ref"])
            sources.append({"ref": ref, "title": await self._source_title(ref)})
        return {"node": node, "edges": edges, "sources": sources}

    async def _source_title(self, ref: str) -> str:
        kind, _, ident = ref.partition(":")
        try:
            if kind == "summary":
                row = await self.storage._fetchone(
                    "SELECT title FROM summary_nodes WHERE summary_id=?", (ident,))
                return str(row["title"])[:120] if row else ref
            if kind == "ledger":
                row = await self.storage._fetchone(
                    "SELECT subject, value FROM catalog_entries WHERE entry_id=?",
                    (ident,))
                return (f"{row['subject']}：{str(row['value'])[:80]}") if row else ref
            if kind == "topic":
                row = await self.storage._fetchone(
                    "SELECT title FROM group_topics WHERE topic_id=?", (ident,))
                return str(row["title"])[:120] if row else ref
            if kind == "impression":
                return f"人物印象 {ident[:24]}"
            if kind == "archive":
                row = await self.storage._fetchone(
                    "SELECT title FROM summary_nodes WHERE summary_id=?", (ident,))
                return str(row["title"])[:120] if row else ref
        except Exception:
            pass
        return ref

    # ---------------------------------------------------------------- 分支
    async def create_branch(self, node_id: str, note: str = "") -> dict[str, Any]:
        row = await self.storage._fetchone(
            "SELECT * FROM web_nodes WHERE node_id=?", (str(node_id or ""),))
        if row is None:
            raise ValueError("节点不存在")
        meta = _safe_meta(row["meta_json"])
        cut_at = str(meta.get("at") or "").strip() or str(row["last_seen"])
        branch_id = uuid.uuid4().hex[:12]
        now = utc_now()
        db = self.storage._conn()
        async with self.storage._write_lock:
            await self.storage._begin_write(db)
            try:
                await db.execute(
                    "INSERT INTO web_branches(branch_id,node_id,node_key,label,"
                    "scope_id,cut_at,note,status,archive_ref,created_at,reverted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (branch_id, str(row["node_id"]), str(row["node_key"]),
                     str(row["label"])[:120], str(row["scope_id"]), cut_at,
                     str(note or "")[:200], "open", "", now, ""))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return await self._branch(branch_id) or {}

    async def _branch(self, branch_id: str) -> dict[str, Any] | None:
        row = await self.storage._fetchone(
            "SELECT * FROM web_branches WHERE branch_id=?", (str(branch_id or ""),))
        return _branch_view(row) if row else None

    async def list_branches(self) -> list[dict[str, Any]]:
        rows = await self.storage._fetchall(
            "SELECT * FROM web_branches ORDER BY created_at DESC LIMIT 60")
        return [_branch_view(row) for row in rows]

    async def revert_branch(self, branch_id: str) -> dict[str, Any]:
        """回溯：压缩归档切口之后的记忆 → 删除原件 → 封住消息区间 → 回到切口。"""
        branch = await self._branch(branch_id)
        if branch is None:
            raise ValueError("分支不存在")
        if branch["status"] == "reverted":
            raise ValueError("这个分支已经回溯过了")
        cut = branch["cut_at"]
        report: dict[str, Any] = {"branch_id": branch["branch_id"],
                                  "label": branch["label"], "cut_at": cut,
                                  "archived": [], "summaries": 0, "ledger": 0,
                                  "sealed": [], "notes": []}
        summary_rows = [dict(row) for row in await self.storage._fetchall(
            "SELECT summary_id,scope_id,level,title,body,start_seq,end_seq "
            "FROM summary_nodes WHERE status='active' AND created_at > ? "
            "ORDER BY scope_id, start_seq", (cut,))]
        ledger_rows = [dict(row) for row in await self.storage._fetchall(
            "SELECT entry_id,memory_id,scope_id FROM catalog_entries "
            "WHERE status='active' AND updated_at > ?", (cut,))]
        # 1) 压缩 + 归档：每个会话一份"回溯存档"（机械摘要：标题 + 要点，不依赖模型）
        by_scope: dict[str, list[Mapping[str, Any]]] = {}
        for row in summary_rows:
            by_scope.setdefault(str(row["scope_id"]), []).append(row)
        for scope_id, rows in by_scope.items():
            try:
                start = min(int(row["start_seq"]) for row in rows)
                end = max(int(row["end_seq"]) for row in rows)
                items = [{"t": _digest_from_summary(row)} for row in rows[:60]]
                body = json.dumps({
                    "kind": "revert_archive",
                    "branch": branch["branch_id"],
                    "branch_label": branch["label"],
                    "note": branch["note"],
                    "count": len(rows),
                    "items": items,
                }, ensure_ascii=False)
                ids = [str(row["summary_id"]) for row in rows]
                cites: list[str] = []
                for chunk in _chunks(ids, 120):
                    marks = ",".join("?" for _ in chunk)
                    cite_rows = await self.storage._fetchall(
                        f"SELECT DISTINCT message_id FROM summary_citations "
                        f"WHERE summary_id IN ({marks}) LIMIT 64", tuple(chunk))
                    cites.extend(str(item["message_id"]) for item in cite_rows)
                cites = list(dict.fromkeys(cites))[:64]
                record = await self.storage.store_summary(
                    scope_id, 3, start, end,
                    f"回溯存档 · {branch['label'][:60]}", body, [], cites, [],
                    {"revert_archive": True, "branch": branch["branch_id"]},
                    model="", occurred_at=utc_now())
                await self.storage.archive_summary(scope_id, record.summary_id,
                                                   note="回溯存档")
                report["archived"].append(record.summary_id)
                await self._persist(
                    f"archive:{record.summary_id}", "archive", scope_id,
                    [{"key": summary_key(record.summary_id), "kind": "summary",
                      "canonical": record.summary_id,
                      "label": f"回溯存档 · {branch['label'][:60]}",
                      "meta": {"level": 3, "at": record.created_at,
                               "archive": True}}],
                    [], node_status="archived", edge_status="archived")
            except Exception as error:
                report["notes"].append(f"归档失败：{str(error)[:100]}")
        # 2) 删除原件（摘要 + 账本；级联会带走 inputs/citations/evidence）
        db = self.storage._conn()
        deleted_summaries = [str(row["summary_id"]) for row in summary_rows]
        deleted_ledger = [str(row["entry_id"]) for row in ledger_rows]
        async with self.storage._write_lock:
            await self.storage._begin_write(db)
            try:
                for chunk in _chunks(deleted_summaries):
                    marks = ",".join("?" for _ in chunk)
                    await db.execute(
                        f"DELETE FROM summary_nodes WHERE status='active' "
                        f"AND summary_id IN ({marks})", tuple(chunk))
                for chunk in _chunks([str(row["memory_id"]) for row in ledger_rows]):
                    marks = ",".join("?" for _ in chunk)
                    await db.execute(
                        f"DELETE FROM memory_items WHERE status='active' "
                        f"AND memory_id IN ({marks})", tuple(chunk))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        report["summaries"] = len(deleted_summaries)
        report["ledger"] = len(deleted_ledger)
        # 3) 网清理（删掉这些来源的连线与节点；共享节点保留）
        refs = [summary_key(sid) for sid in deleted_summaries] + \
               [f"ledger:{eid}" for eid in deleted_ledger]
        await self._forget_sources(refs)
        # 4) 封口：切口之后的消息区间不再参与压缩（防止回溯后重新长出记忆）
        seal_rows = await self.storage._fetchall(
            "SELECT mi.scope_id AS scope_id, MIN(mc.scope_seq) AS a, "
            "MAX(mc.scope_seq) AS b FROM message_current mc "
            "JOIN message_identities mi ON mi.message_id=mc.message_id "
            "JOIN message_revisions mr ON mr.revision_id=mc.revision_id "
            "WHERE mc.is_deleted=0 AND mr.occurred_at > ? GROUP BY mi.scope_id",
            (cut,))
        db = self.storage._conn()
        async with self.storage._write_lock:
            await self.storage._begin_write(db)
            try:
                for row in seal_rows:
                    await db.execute(
                        "INSERT OR IGNORE INTO memory_marks(mark_id,scope_id,"
                        "start_seq,end_seq,kind,ref,created_at) VALUES(?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, str(row["scope_id"]), int(row["a"]),
                         int(row["b"]), "reverted", branch["branch_id"], utc_now()))
                    report["sealed"].append({
                        "scope_id": str(row["scope_id"]),
                        "start_seq": int(row["a"]), "end_seq": int(row["b"])})
                await db.execute(
                    "UPDATE web_branches SET status='reverted', reverted_at=?, "
                    "archive_ref=? WHERE branch_id=?",
                    (utc_now(), ",".join(report["archived"])[:400],
                     branch["branch_id"]))
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        logger.info("长程记忆：回溯完成（分支 %s）—— 归档 %s / 删除摘要 %s 账本 %s / 封口 %s 个会话",
                    branch["branch_id"], len(report["archived"]),
                    report["summaries"], report["ledger"], len(report["sealed"]))
        return report

    async def _forget_sources(self, refs: Sequence[str]) -> int:
        """删掉这些来源的连线/来源标记；权重重算；孤儿节点清理。"""
        clean = [str(ref) for ref in dict.fromkeys(refs) if str(ref)]
        if not clean:
            return 0
        db = self.storage._conn()
        removed = 0
        async with self.storage._write_lock:
            await self.storage._begin_write(db)
            try:
                for chunk in _chunks(clean):
                    marks = ",".join("?" for _ in chunk)
                    rows = await self.storage._fetchall(
                        f"SELECT DISTINCT node_id FROM web_node_sources "
                        f"WHERE source_ref IN ({marks})", tuple(chunk))
                    affected = [str(row["node_id"]) for row in rows]
                    await db.execute(
                        f"DELETE FROM web_edges WHERE source_ref IN ({marks})",
                        tuple(chunk))
                    await db.execute(
                        f"DELETE FROM web_node_sources WHERE source_ref IN ({marks})",
                        tuple(chunk))
                    await db.execute(
                        f"DELETE FROM web_sources WHERE source_ref IN ({marks})",
                        tuple(chunk))
                    for node_id in affected:
                        await db.execute(
                            "UPDATE web_nodes SET weight=(SELECT COUNT(*) FROM "
                            "web_node_sources wns WHERE wns.node_id=web_nodes.node_id) "
                            "WHERE node_id=?", (node_id,))
                cursor = await db.execute(
                    "DELETE FROM web_nodes WHERE node_id NOT IN "
                    "(SELECT src_id FROM web_edges UNION SELECT dst_id FROM web_edges) "
                    "AND node_id NOT IN (SELECT node_id FROM web_node_sources)")
                removed = int(cursor.rowcount or 0)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        return removed

    # ---------------------------------------------------------------- 剪枝
    async def _adjacency(self, limit: int = 20000) -> dict[str, list[str]]:
        rows = await self.storage._fetchall(
            "SELECT src_id, dst_id FROM web_edges WHERE status='active' LIMIT ?",
            (int(limit),))
        adjacency: dict[str, list[str]] = {}
        for row in rows:
            src, dst = str(row["src_id"]), str(row["dst_id"])
            adjacency.setdefault(src, []).append(dst)
            adjacency.setdefault(dst, []).append(src)
        return adjacency

    @staticmethod
    def _bfs(start: str, goal: str, adjacency: Mapping[str, Sequence[str]],
             *, max_depth: int = 14, max_visit: int = 6000) -> list[str] | None:
        if start == goal:
            return [start]
        queue: list[str] = [start]
        parent: dict[str, str] = {start: ""}
        visited = 0
        depth = {start: 0}
        while queue:
            node = queue.pop(0)
            visited += 1
            if visited > max_visit or depth.get(node, 0) >= max_depth:
                continue
            for neighbor in adjacency.get(node, ()):
                if neighbor in parent:
                    continue
                parent[neighbor] = node
                depth[neighbor] = depth.get(node, 0) + 1
                if neighbor == goal:
                    path = [goal]
                    while path[-1] != start:
                        path.append(parent[path[-1]])
                    return list(reversed(path))
                queue.append(neighbor)
        return None

    async def prune(self, node_ids: Sequence[str], *, rounds: int = 1,
                    min_batch: int = 2) -> dict[str, Any]:
        """沿所选节点之间的路径：丢弃低价值节点、把必要摘要向上压缩。"""
        wanted = [str(item) for item in node_ids if str(item).strip()]
        if len(wanted) < 2:
            raise ValueError("剪枝至少要选择两个节点")
        rows = await self.storage._fetchall(
            "SELECT * FROM web_nodes WHERE node_id IN (" +
            ",".join("?" for _ in wanted) + ")", tuple(wanted))
        if len(rows) < 2:
            raise ValueError("选中的节点里有效的不足两个")
        nodes = {str(row["node_id"]): _node_view(row) for row in rows}

        def _when(node: Mapping[str, Any]) -> str:
            return str(node["meta"].get("at") or node["last_seen"])

        ordered = sorted(nodes.values(), key=_when)
        adjacency = await self._adjacency()
        path: list[str] = []
        unreachable: list[list[str]] = []
        for first, second in zip(ordered, ordered[1:]):
            segment = self._bfs(first["id"], second["id"], adjacency)
            if not segment:
                unreachable.append([first["label"], second["label"]])
                continue
            if path:
                segment = segment[1:]
            path.extend(segment)
        if not path:
            raise ValueError("所选节点之间没有连通的路径")
        unique_path = list(dict.fromkeys(path))
        path_rows = await self.storage._fetchall(
            "SELECT * FROM web_nodes WHERE node_id IN (" +
            ",".join("?" for _ in unique_path) + ")", tuple(unique_path))
        path_nodes = {str(row["node_id"]): _node_view(row) for row in path_rows}
        # "必要的" = 摘要/会话（承载内容与结构）、被多处引用（weight>=2）、
        # 或者连着不止一个非会话邻居（话题/人物被多条记忆共用）。
        # 其余是只挂在一处、只出现过一次的叶子——丢弃；会话边不算"连接"，
        # 否则每个节点都因为"属于某个群"而变成必要。
        neighbor_rows = await self.storage._fetchall(
            "SELECT e.node_id AS node_id, COUNT(DISTINCT e.other) AS n FROM ("
            "SELECT src_id AS node_id, dst_id AS other FROM web_edges WHERE status='active' "
            "UNION ALL SELECT dst_id AS node_id, src_id AS other FROM web_edges "
            "WHERE status='active') e JOIN web_nodes o ON o.node_id=e.other "
            "AND o.kind<>'group' WHERE e.node_id IN (" +
            ",".join("?" for _ in unique_path) + ") GROUP BY e.node_id",
            tuple(unique_path))
        neighbor_counts = {str(row["node_id"]): int(row["n"]) for row in neighbor_rows}

        necessary: list[dict[str, Any]] = []
        discard: list[dict[str, Any]] = []
        for node_id in unique_path:
            node = path_nodes.get(node_id)
            if node is None:
                continue
            links = neighbor_counts.get(node_id, 0)
            if (node["kind"] in ("summary", "group") or node["weight"] >= 2
                    or links >= 2):
                necessary.append(node)
            else:
                discard.append(node)
        pruned_ids = [node["id"] for node in discard if node["kind"] != "summary"]
        if pruned_ids:
            db = self.storage._conn()
            async with self.storage._write_lock:
                await self.storage._begin_write(db)
                try:
                    for chunk in _chunks(pruned_ids):
                        marks = ",".join("?" for _ in chunk)
                        await db.execute(
                            f"UPDATE web_nodes SET status='pruned' WHERE node_id "
                            f"IN ({marks})", tuple(chunk))
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise

        merged: list[str] = []
        notes: list[str] = []
        frontier: list[tuple[str, int, str]] = [
            (node["scope_id"], int(node["meta"].get("level") or 1), node["id"])
            for node in necessary if node["kind"] == "summary"]
        for _round in range(max(1, int(rounds))):
            groups: dict[tuple[str, int], list[str]] = {}
            for scope_id, level, node_id in frontier:
                if level >= 3:
                    continue
                groups.setdefault((scope_id, level), []).append(node_id)
            next_frontier: list[tuple[str, int, str]] = []
            for (scope_id, level), ids in groups.items():
                if len(ids) < max(2, int(min_batch)):
                    continue
                if self.compressor is None:
                    notes.append("压缩服务未就绪，跳过压缩")
                    break
                summary_ids: list[str] = []
                for node_id in ids:
                    ref_rows = await self.storage._fetchall(
                        "SELECT source_ref FROM web_node_sources WHERE node_id=? "
                        "AND source_ref LIKE 'summary:%' LIMIT 1", (node_id,))
                    if ref_rows:
                        summary_ids.append(str(ref_rows[0]["source_ref"])[8:])
                if len(summary_ids) < 2:
                    continue
                try:
                    parent = await self.compressor.merge_summaries(scope_id, summary_ids)
                except Exception as error:
                    notes.append(f"合并失败：{str(error)[:100]}")
                    continue
                merged.append(parent.summary_id)
                next_frontier.append((scope_id, int(parent.level), summary_key(parent.summary_id)))
            if not next_frontier:
                break
            frontier = next_frontier
        # 新合并的摘要立即入网（面板刷新即可见）
        for summary_id in merged:
            try:
                row = await self.storage._fetchone(
                    "SELECT * FROM summary_nodes WHERE summary_id=?", (summary_id,))
                if row is None:
                    continue
                citations = await self.storage._fetchall(
                    "SELECT message_id FROM summary_citations WHERE summary_id=?",
                    (summary_id,))
                await self.ingest_summary(SummaryRecord(
                    str(row["summary_id"]), str(row["scope_id"]), int(row["level"]),
                    int(row["start_seq"]), int(row["end_seq"]), str(row["title"]),
                    str(row["body"]), json.loads(str(row["topics_json"])),
                    [str(x["message_id"]) for x in citations], str(row["created_at"])))
            except Exception as error:
                notes.append(f"入网失败：{str(error)[:100]}")
        return {
            "path": [path_nodes[nid]["label"] for nid in unique_path if nid in path_nodes],
            "path_count": len(unique_path),
            "pruned": len(pruned_ids),
            "merged": merged,
            "unreachable": unreachable,
            "notes": notes,
        }


def _safe_meta(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _node_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["node_id"]),
        "key": str(row["node_key"]),
        "kind": str(row["kind"]),
        "label": str(row["label"]),
        "canonical": str(row["canonical"]),
        "scope_id": str(row["scope_id"]),
        "weight": float(row["weight"]),
        "status": str(row["status"]),
        "meta": _safe_meta(row["meta_json"]),
        "first_seen": str(row["first_seen"]),
        "last_seen": str(row["last_seen"]),
    }


def _branch_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "branch_id": str(row["branch_id"]),
        "node_id": str(row["node_id"]),
        "node_key": str(row["node_key"]),
        "label": str(row["label"]),
        "scope_id": str(row["scope_id"]),
        "cut_at": str(row["cut_at"]),
        "note": str(row["note"]),
        "status": str(row["status"]),
        "archive_ref": str(row["archive_ref"]),
        "created_at": str(row["created_at"]),
        "reverted_at": str(row["reverted_at"]),
    }
