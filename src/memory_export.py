# -*- coding: utf-8 -*-
"""记忆导出 / 导入：把 bot 的记忆打成一个可下载的 zip，也能再喂回来。

用户诉求（实录）："加入导出记忆功能，可以下载 bot 的记忆（注意：重新导入时需要匹配的
嵌入模型），也可以导入记忆。"

包结构（zip）：
    manifest.json   版本、导出时间、各表条数、**导出时的嵌入模型**、提示与注意事项
    data.json.gz    全部记忆行（按表分组，gzip 压缩）

为什么带嵌入模型：向量只在自己那个模型的向量空间里有意义——换了嵌入模型，
旧向量与新向量算余弦相似度等于随机数。所以导入时**模型对不上就丢向量、留文本**，
并在面板上写明原因（绝不悄悄导入一堆没用的向量）。

哪些东西进包：会话、消息、分层摘要、记忆账本、人物印象/档案/好感度、说话风格、
群内黑话、群话题、情绪记忆、注入与立指令/待办/日程等"理解层"数据。
不进包：媒体与表情包文件（体积大、在磁盘上）、用量统计与审计流水（本机运行数据）。
"""
from __future__ import annotations

import gzip
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

MANIFEST_NAME = "manifest.json"
DATA_NAME = "data.json.gz"
PACKAGE_KIND = "sparking-memory"
PACKAGE_VERSION = 1

# 导出/导入的表清单：顺序=导入顺序（外键依赖在前）。
# 每项：(表名, 主键列, 是否需要按 scope 重映射)。
TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("scopes", ("scope_id",)),
    ("event_headers", ("event_id",)),
    ("message_identities", ("message_id",)),
    ("message_revisions", ("revision_id",)),
    ("message_current", ("message_id",)),
    ("summary_nodes", ("summary_id",)),
    ("summary_inputs", ("summary_id", "input_message_id", "input_summary_id")),
    ("summary_citations", ("summary_id", "message_id")),
    ("memory_items", ("memory_id",)),
    ("memory_revisions", ("memory_revision_id",)),
    ("memory_evidence", ("memory_id", "message_id")),
    ("catalog_entries", ("entry_id",)),
    ("user_impressions", ("scope_id", "user_id")),
    ("user_styles", ("scope_id", "user_id")),
    ("user_affinity", ("scope_id", "user_id")),
    ("person_profiles", ("person_id",)),
    ("group_style_rules", ("rule_id",)),
    ("group_lexicon", ("term_id",)),
    ("group_topics", ("topic_id",)),
    ("mood_events", ("event_id",)),
    ("embeddings", ("embedding_id",)),
    ("prompt_injections", ("injection_id",)),
    ("standing_intents", ("intent_id",)),
    ("agent_todos", ("todo_id",)),
    ("agent_plans", ("plan_id",)),
)

# 必须按新 scope_id 改写的列
_SCOPE_COLUMNS = ("scope_id", "input_scope_id")
# 必须按新 message_id 改写的列
_MESSAGE_COLUMNS = ("message_id", "owner_id", "evidence_message_id", "root_message_id",
                    "input_message_id")


def package_name(stamp: datetime | None = None) -> str:
    moment = stamp or datetime.now(timezone.utc)
    return f"sparking-memory-{moment.strftime('%Y%m%dT%H%M%S')}Z.zip"


def build_package(tables: Mapping[str, Iterable[Mapping[str, Any]]],
                  *, plugin_version: str = "", embedding_model: str = "",
                  platform: str = "", extra: Mapping[str, Any] | None = None) -> bytes:
    """把记忆行打成 zip 字节。tables 里没有的表按空处理。"""
    normalised: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for name, _keys in TABLES:
        rows = [dict(row) for row in (tables.get(name) or [])]
        normalised[name] = rows
        counts[name] = len(rows)
    manifest = {
        "kind": PACKAGE_KIND,
        "package_version": PACKAGE_VERSION,
        "plugin_version": str(plugin_version or ""),
        "platform": str(platform or ""),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        # 导入时要匹配的就是它：换了嵌入模型，旧向量就作废
        "embedding_model": str(embedding_model or ""),
        "counts": counts,
        "total_rows": sum(counts.values()),
        "messages": counts.get("message_revisions", 0),
        "notes": [
            "重新导入时需要匹配的嵌入模型：模型不一致时只导入文本，向量会被跳过（会写明）。",
            "不进包的东西：媒体/表情包文件、用量统计、审计流水（它们属于本机运行数据）。",
        ],
    }
    if extra:
        manifest.update(dict(extra))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(MANIFEST_NAME,
                        json.dumps(manifest, ensure_ascii=False, indent=2))
        payload = json.dumps(normalised, ensure_ascii=False, default=str).encode("utf-8")
        bundle.writestr(DATA_NAME, gzip.compress(payload, compresslevel=6))
    return buffer.getvalue()


def read_package(raw: bytes) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """读出 (manifest, tables)；不是本插件的包就报错说清楚。"""
    try:
        bundle = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as error:
        raise ValueError("这不是一个 zip 文件（或者文件坏了）") from error
    names = set(bundle.namelist())
    if MANIFEST_NAME not in names:
        raise ValueError("包里没有 manifest.json：不是本插件导出的记忆包")
    manifest = json.loads(bundle.read(MANIFEST_NAME).decode("utf-8", "replace"))
    if str(manifest.get("kind") or "") != PACKAGE_KIND:
        raise ValueError(f"包的 kind 是 {manifest.get('kind')!r}，不是 {PACKAGE_KIND}")
    if DATA_NAME not in names:
        raise ValueError("包里没有 data.json.gz")
    blob = bundle.read(DATA_NAME)
    try:
        text = gzip.decompress(blob).decode("utf-8", "replace")
    except OSError:                                # 没压缩的包也认
        text = blob.decode("utf-8", "replace")
    tables = json.loads(text)
    if not isinstance(tables, dict):
        raise ValueError("data.json.gz 里不是按表分组的对象")
    return manifest, {str(k): list(v or []) for k, v in tables.items()}


def embedding_verdict(manifest: Mapping[str, Any], current_model: str) -> tuple[bool, str]:
    """向量能不能用：(是否导入, 给用户看的一句话)。"""
    packed = str(manifest.get("embedding_model") or "").strip()
    current = str(current_model or "").strip()
    if not packed and not current:
        return True, "导出与当前都没配嵌入模型：向量按原样导入"
    if not packed:
        return False, "这个包里没有嵌入模型信息（旧版本导出的），向量跳过、文本照常导入"
    if not current:
        return False, (f"当前没配嵌入模型，而包里是 {packed}：向量跳过（配好同一个模型再导一次就能带上）")
    if packed != current:
        return False, (f"嵌入模型不匹配：包里是 {packed}、当前是 {current} —— "
                       "向量跳过（向量空间不同，混用等于随机数），文本与记忆照常导入")
    return True, f"嵌入模型一致（{packed}）：向量一起导入"


def remap_row(row: Mapping[str, Any], *, scope_map: Mapping[str, str],
              message_map: Mapping[str, str], seq_shift: Mapping[str, int] | None = None,
              seq_columns: tuple[str, ...] = (), self_keys: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """把一行按映射改写；引用了不存在的对象（说明它被跳过了）就返回 None。

    `self_keys` 是"这一行自己的身份列"（如 message_identities.message_id）：
    它们不参与映射，否则自己会被当成"引用了不存在的消息"而整行被丢。
    """
    out = dict(row)
    for column in _SCOPE_COLUMNS:
        if column in out and out[column] not in (None, ""):
            mapped = scope_map.get(str(out[column]))
            if mapped is None:
                return None
            out[column] = mapped
    for column in _MESSAGE_COLUMNS:
        if column in self_keys:
            continue
        if column in out and out[column] not in (None, ""):
            mapped = message_map.get(str(out[column]))
            if mapped is None:
                # 摘要引用的一条消息没进库（去重跳过/已被删）→ 这两张关联表整行不要
                if column in ("input_message_id", "message_id"):
                    return None
                out[column] = ""
            else:
                out[column] = mapped
    if seq_shift:
        for column in seq_columns:
            if column in out and out[column] is not None:
                try:
                    shift = seq_shift.get(str(out.get("scope_id", "")), 0)
                    out[column] = int(out[column]) + int(shift)
                except (TypeError, ValueError):
                    pass
    return out
