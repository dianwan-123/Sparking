# -*- coding: utf-8 -*-
"""快速学习：导入导出的群聊天记录（QQChatExporter V5 / chunked-jsonl）。

用户给的语料形状：
    <群文件夹>/
        manifest.json          # chatInfo{name,type,selfUin,selfName} + statistics{senders,timeRange}
        chunks/chunk_0001.jsonl  # 每行一条消息

文件夹名形如 ``group_数学指令讨论群_1095747640_20260912_191113_chunked_jsonl``，
里面有群名与群号；**不是白名单群也要能导入**（导入只写记忆，不影响是否接管）。

解析尽量宽容：jsonl 里一行坏掉不影响其它行；elements 里的图文按人能读的方式拼成文本。
"""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

_FOLDER_RE = re.compile(
    r"^(?:group|friend)[_-](?P<name>.*?)[_-](?P<id>\d{5,})[_-]\d{8}[_-]\d{6}")
# elements 里这些类型只留一个占位符（真图/文件在导出里没有载荷）
_ELEMENT_PLACEHOLDER = {
    "image": "[图片]", "video": "[视频]", "audio": "[语音]", "record": "[语音]",
    "file": "[文件]", "json": "[卡片消息]", "xml": "[卡片消息]",
    "forward": "[合并转发]", "face": "[表情]", "market_face": "[表情]",
    "ark": "[卡片消息]", "markdown": "[卡片消息]", "gift": "[礼物]",
    "poke": "[戳一戳]", "type_17": "[图片]",
}


@dataclass(frozen=True, slots=True)
class ImportedMessage:
    message_id: str
    sender_id: str
    sender_name: str
    text: str
    occurred_at: str          # UTC ISO
    kind: str = "text"
    reply_to: str = ""        # 被引用消息的内容（导出里没有可用的 id）
    sender_uid: str = ""


@dataclass(slots=True)
class ImportedGroup:
    name: str
    group_id: str
    self_uin: str = ""
    self_name: str = ""
    messages: list[ImportedMessage] = field(default_factory=list)
    senders: dict[str, str] = field(default_factory=dict)   # uid/uin → 名字
    counts: dict[str, int] = field(default_factory=dict)    # uin → 条数（以解析出的消息为准）
    manifest_counts: dict[str, int] = field(default_factory=dict)  # 导出自带的统计（仅参考）
    source_folder: str = ""

    @property
    def people(self) -> list[tuple[str, str, int]]:
        """(uin, 名字, 条数) 按活跃度降序——建印象时按这个顺序取前几个。"""
        rows = [(uin, self.senders.get(uin, uin), count)
                for uin, count in self.counts.items() if uin]
        rows.sort(key=lambda item: -item[2])
        return rows


def folder_identity(folder_name: str) -> tuple[str, str]:
    """从文件夹名解析 (群名, 群号)；解析不出来就返回 (文件夹名, "")。"""
    match = _FOLDER_RE.match(str(folder_name or "").strip())
    if match:
        return match.group("name").strip() or folder_name, match.group("id")
    return str(folder_name or "").strip(), ""


def _iso_from_ms(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return datetime.now(timezone.utc).isoformat()


def _sender_of(raw: Mapping[str, Any]) -> tuple[str, str, str]:
    """→ (uin, uid, 名字)。名字优先群名片，其次导出给的显示名。

    实录：同一份导出里**同一个人有时只带 uid、没有 uin**（两条并列出现），
    直接拿它当 id 会把一个人算成两个。所以 uid 单独返回，交给上层回填。
    """
    sender = raw.get("sender") if isinstance(raw.get("sender"), Mapping) else {}
    uin = str(sender.get("uin") or "").strip()
    uid = str(sender.get("uid") or "").strip()
    name = (str(sender.get("groupCard") or "").strip()
            or str(sender.get("name") or "").strip()
            or str(sender.get("nickname") or "").strip()
            or str(sender.get("remark") or "").strip()
            or uin or uid)
    return uin, uid, name


def _elements_to_text(raw: Mapping[str, Any]) -> tuple[str, str, str]:
    """把一条消息的 elements 拼成人能读的文本。

    返回 (正文, 被引用内容, 类型标签)。导出里的 `content.text` 已经是可读形态，
    但 elements 更细，两个都试，谁完整用谁。
    """
    content = raw.get("content") if isinstance(raw.get("content"), Mapping) else {}
    elements = content.get("elements")
    reply_text = ""
    parts: list[str] = []
    kinds: list[str] = []
    if isinstance(elements, Iterable) and not isinstance(elements, (str, bytes)):
        for element in elements:
            if not isinstance(element, Mapping):
                continue
            kind = str(element.get("type") or "").strip()
            data = element.get("data") if isinstance(element.get("data"), Mapping) else {}
            if not kind:
                continue
            if kind == "text":
                text = str(data.get("text") or "")
                if text:
                    parts.append(text)
                    kinds.append("text")
            elif kind == "reply":
                reply_text = " ".join(str(data.get("content") or "").split())[:120]
            elif kind == "at":
                who = str(data.get("name") or data.get("uin") or "").strip()
                if who:
                    parts.append(f"@{who}")
            else:
                parts.append(_ELEMENT_PLACEHOLDER.get(kind, f"[{kind}]"))
                kinds.append(kind)
    body = "".join(parts).strip()
    if not body:
        body = " ".join(str(content.get("text") or "").split())
        # content.text 里带着 [回复消息] 前缀，剥掉它更干净
        body = body.replace("[回复消息]", " ")
        body = body.replace("[回复消息]", " ")
        for prefix in ("[图片]", "[视频]", "[语音]", "[文件]"):
            body = body.replace(prefix, prefix)
        body = " ".join(body.split())
    kind = "text" if kinds and set(kinds) == {"text"} else (kinds[0] if kinds else
                                                           str(raw.get("type") or "text"))
    return body.strip()[:2000], reply_text, str(kind)


def parse_jsonl_lines(lines: Iterable[str], *, limit: int = 0) -> list[ImportedMessage]:
    """逐行解析（一行坏掉只跳过这一行）。limit>0 时只取前 limit 条。"""
    out: list[ImportedMessage] = []
    for line in lines:
        text = str(line or "").strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, Mapping):
            continue
        if raw.get("system") or raw.get("recalled"):
            continue
        if str(raw.get("type") or "").strip().lower() in {"system", "recall"}:
            continue
        uin, uid, name = _sender_of(raw)
        body, reply_text, kind = _elements_to_text(raw)
        if not body or body.strip() in {"[system]", "[空]"}:     # 纯占位不算消息
            if not reply_text:
                continue
        out.append(ImportedMessage(
            message_id=str(raw.get("id") or raw.get("seq") or len(out)),
            sender_uid=uid,
            sender_id=uin or uid or "unknown",
            sender_name=name,
            text=body or f"（引用：{reply_text}）",
            occurred_at=_iso_from_ms(raw.get("timestamp")),
            kind=kind,
            reply_to=reply_text,
        ))
        if limit and len(out) >= limit:
            break
    return out


def _read_manifest(payload: Mapping[str, Any]) -> tuple[str, str, str, str, list[tuple[str, int]]]:
    """→ (群名, 群号, self_uin, self_name, [(uin, 条数)])"""
    chat = payload.get("chatInfo") if isinstance(payload.get("chatInfo"), Mapping) else {}
    name = str(chat.get("name") or "").strip()
    group_id = str(chat.get("uin") or chat.get("id") or "").strip()
    self_uin = str(chat.get("selfUin") or "").strip()
    self_name = str(chat.get("selfName") or "").strip()
    counts: list[tuple[str, int]] = []
    stats = payload.get("statistics") if isinstance(payload.get("statistics"), Mapping) else {}
    senders = stats.get("senders")
    if isinstance(senders, list):
        for item in senders:
            if not isinstance(item, Mapping):
                continue
            uin = str(item.get("uin") or item.get("uid") or "").strip()
            if uin:
                counts.append((uin, int(item.get("messageCount") or 0)))
    return name, group_id, self_uin, self_name, counts


def parse_export_folder(folder: Path, *, max_messages: int = 0) -> ImportedGroup | None:
    """解析一个导出文件夹（zip 解出来的那一层）。"""
    manifest_path = folder / "manifest.json"
    name, group_id = folder_identity(folder.name)
    self_uin = self_name = ""
    counts: list[tuple[str, int]] = []
    manifest_name = ""
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, Mapping):
            manifest_name, manifest_group, self_uin, self_name, counts = _read_manifest(payload)
            group_id = manifest_group or group_id
            name = manifest_name or name
    chunks_dir = folder / "chunks"
    chunk_files = sorted(chunks_dir.glob("*.jsonl")) if chunks_dir.is_dir() else []
    if not chunk_files:
        chunk_files = sorted(folder.glob("*.jsonl"))
    if not chunk_files:
        return None
    group = ImportedGroup(name=name or folder.name, group_id=group_id or folder.name,
                          self_uin=self_uin, self_name=self_name, source_folder=folder.name)
    for chunk in chunk_files:
        try:
            with chunk.open("r", encoding="utf-8", errors="replace") as handle:
                parsed = parse_jsonl_lines(handle, limit=0)
        except OSError:
            continue
        group.messages.extend(parsed)
        if max_messages and len(group.messages) >= max_messages:
            group.messages = group.messages[:max_messages]
            break
    # uid → uin 回填：同一个人只带 uid 的那几条，归到他真正的 QQ 号下面
    uid_to_uin: dict[str, str] = {}
    uid_to_name: dict[str, str] = {}
    for message in group.messages:
        if message.sender_uid and message.sender_id and message.sender_id != message.sender_uid:
            uid_to_uin.setdefault(message.sender_uid, message.sender_id)
            uid_to_name.setdefault(message.sender_uid, message.sender_name)
    manifest_names = {uin: group.senders.get(uin, uin) for uin, _count in counts}
    import dataclasses

    fixed: list[ImportedMessage] = []
    for message in group.messages:
        sender_id, sender_name = message.sender_id, message.sender_name
        if sender_id == message.sender_uid and message.sender_uid in uid_to_uin:
            sender_id = uid_to_uin[message.sender_uid]
            if not sender_name or sender_name.startswith("u_"):
                sender_name = uid_to_name.get(message.sender_uid) or sender_name
        if (not sender_name or sender_name.startswith("u_")) and manifest_names.get(sender_id):
            sender_name = manifest_names[sender_id]
        fixed.append(dataclasses.replace(
            message, sender_id=sender_id, sender_name=sender_name))
    group.messages = fixed
    group.counts = {}
    for message in group.messages:
        group.senders.setdefault(message.sender_id, message.sender_name)
        if message.sender_id and message.sender_id != "unknown":
            group.counts[message.sender_id] = group.counts.get(message.sender_id, 0) + 1
    # manifest 的 senders 只拿来补名字（它有时按 uid 列人，当成人数会与 uin 重复计数）
    for uin, count in counts:
        group.senders.setdefault(uin, group.senders.get(uin, uin))
        group.manifest_counts[uin] = count
    group.messages.sort(key=lambda item: item.occurred_at)
    return group


def discover_groups(root: Path, *, max_messages: int = 0) -> list[ImportedGroup]:
    """zip 根目录下每一层文件夹当作一个群（也容忍多包一层目录）。"""
    candidates: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "manifest.json").is_file() or (child / "chunks").is_dir() \
                or list(child.glob("*.jsonl")):
            candidates.append(child)
        else:                                        # 再往里看一层
            for inner in sorted(child.iterdir()):
                if inner.is_dir() and ((inner / "manifest.json").is_file()
                                       or (inner / "chunks").is_dir()):
                    candidates.append(inner)
    groups: list[ImportedGroup] = []
    for folder in candidates:
        try:
            parsed = parse_export_folder(folder, max_messages=max_messages)
        except Exception:
            parsed = None
        if parsed is not None:
            groups.append(parsed)
    return groups


def extract_zip(zip_path: Path, target_dir: Path) -> Path:
    """安全解压（拒绝绝对路径与 ../ 逃逸）。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as bundle:
        for info in bundle.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            bundle.extract(info, target_dir)
    return target_dir
