from __future__ import annotations

import json
import re
from typing import Any


_UNPARSED = object()


def _try_loads(text: str) -> Any:
    """json.loads 的宽容版：严格失败时先修尾逗号（模型输出常见）再试。

    仍失败返回 _UNPARSED 哨兵，与合法的 null 区分。
    """
    for candidate in (text, re.sub(r",\s*([}\]])", r"\1", text)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return _UNPARSED


def parse_json_object(text: str) -> dict[str, Any] | None:
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    value = _try_loads(text)
    if value is not _UNPARSED:
        return value if isinstance(value, dict) else None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                extracted = _try_loads(text[start:index + 1])
                return extracted if isinstance(extracted, dict) else None
    return None


def iter_json_objects(text: str) -> "list[dict[str, Any]]":
    """把文本里所有能解析出来的平衡 `{…}` 块按出现顺序列出来。

    实录（deepseek-v4.1-flash 在 catapi 上）：模型把规则当耳旁风，先写一大段分析、
    再顺手把 JSON 塞在中间；原来的 `parse_json_object` 只认第一个 `{` 块，
    那块往往是它举例用的片段 → 解析失败 → 学习整段静默跳过。
    """
    blocks: list[dict[str, Any]] = []
    text = str(text or "")
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            return blocks
        depth = 0
        quoted = False
        escaped = False
        for cursor in range(start, len(text)):
            char = text[cursor]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    value = _try_loads(text[start:cursor + 1])
                    if isinstance(value, dict):
                        blocks.append(value)
                    index = cursor + 1
                    break
        else:                      # 没找到配对的收尾：后面的都不用看了
            return blocks


def parse_json_object_containing(text: str, keys: "tuple[str, ...]") -> dict[str, Any] | None:
    """解析出「含指定键」的那个 JSON 对象；一个都没有就退回顾有的解析结果。

    给"模型话多但终究给了 JSON"的场景用：整段解析 → 逐个块找 → 都没有才算失败。
    """
    direct = parse_json_object(text)
    if isinstance(direct, dict) and any(key in direct for key in keys):
        return direct
    for block in iter_json_objects(text):
        if any(key in block for key in keys):
            return block
    return direct if isinstance(direct, dict) else None


# 压缩时优先丢掉的键（按顺序）：都是"可再查"的冗余内容
_DROP_FIRST = ("chat_evidence", "summary_memory", "memory_catalog", "runtime_manifest",
               "conversation_overview", "other_scope_tags", "group_lexicon",
               "group_style", "known_person", "mood_events", "user_affinity",
               "sender_style", "sender_impression", "request_analysis", "style_hint")
# 压缩时**绝不**丢的键：丢了模型就没法正确回应（memory_context 里的内容可以缩，
# 但这个键本身必须留着）
_KEEP_ALWAYS = ("current_message", "memory_context", "my_recent_words", "now",
                "history_note", "type", "truncated", "truncated_note")


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_len(value: Any) -> int:
    """一个值序列化后大概多长（不真的拼字符串，用于增量计长）。"""
    if value is None:
        return 4
    if value is True or value is False:
        return 5 if value else 4
    if isinstance(value, (int, float)):
        return len(str(value))
    if isinstance(value, str):
        # 转义会略微变长，估个保守值
        return len(value) + 2 + value.count('"') + value.count("\\") * 2
    if isinstance(value, (dict, list)):
        return len(_dump_json(value))
    return len(str(value)) + 2


def _string_slots(data: Any, skip: set | None = None) -> list[tuple[Any, Any, int]]:
    """所有字符串槽位 → [(容器, 键或下标, 长度)]。

    `skip` 里的顶层键**整棵子树都不动**——"我刚说过的话"这类内容短但关键，
    被截一半就等于没记住。
    """
    slots: list[tuple[Any, Any, int]] = []
    skip = skip or set()
    stack: list[Any] = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in skip:
                    continue
                if isinstance(value, str):
                    slots.append((node, key, len(value)))
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                if isinstance(value, str):
                    slots.append((node, index, len(value)))
                elif isinstance(value, (dict, list)):
                    stack.append(value)
    return slots


def _list_slots(data: Any, skip: set | None = None) -> list[tuple[list, int]]:
    """所有列表 → [(列表, 序列化长度)]（用于从尾部删元素）。"""
    slots: list[tuple[list, int]] = []
    skip = skip or set()
    stack: list[Any] = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in skip:
                    continue
                if isinstance(value, list):
                    slots.append((value, _json_len(value)))
                    stack.append(value)
                elif isinstance(value, dict):
                    stack.append(value)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, list):
                    slots.append((value, _json_len(value)))
                    stack.append(value)
                elif isinstance(value, dict):
                    stack.append(value)
    return slots


def compact_json(value: Any, limit: int = 12000, *,
                 keep: "tuple[str, ...] | None" = None,
                 drop_first: "tuple[str, ...] | None" = None) -> str:
    """把结构序列化成 JSON，超预算时**按结构压缩**（永远输出合法 JSON）。

    与旧实现的关键区别：旧的是 `text[:limit] + "…"` —— 切出**半截 JSON**，调用方
    拿去 parse 必然失败、退回 `{}`，于是记忆整块丢失（实录：导入的大群历史长，
    判定/回复链路的 `memory_context` 几乎每轮都是空的，表现成"不认自己刚说的话、
    旧事当新事、答非所问"）。这里只做三种操作，且**增量计长**（大结构也快）：
      ① 丢掉可丢的键 → ② 截短最长的字符串 → ③ 从最长的列表尾部删元素
    每一步输出都仍是完整 JSON，**任何调用方拿去做 parse 都是安全的**。
    """
    text = _dump_json(value)
    if len(text) <= limit:
        return text
    try:
        data = json.loads(text)
    except Exception:
        return text[:limit]
    protected = set(_KEEP_ALWAYS) | set(keep or ())
    # 这些键的**内容**也不许截（短而关键：我刚说的话、当前这句、时间）
    never_cut = {"my_recent_words", "current_message", "now", "history_note",
                 "truncated_note"}
    order = tuple(drop_first or _DROP_FIRST)
    total = len(text)

    # ① 丢掉"可再查"的键（顶层的，最省事）
    if isinstance(data, dict):
        for name in order:
            if total <= limit:
                break
            if name in data and name not in protected:
                total -= _json_len(data[name]) + len(name) + 4
                data.pop(name, None)
    # ② 截短字符串（长的先截，每次砍一半）
    if total > limit:
        for container, key, size in sorted(_string_slots(data, never_cut), key=lambda x: -x[2]):
            if total <= limit:
                break
            if size < 2:
                continue
            half = size // 2
            container[key] = container[key][:half]
            total -= (size - half)
    # ③ 删列表尾部（长的先删）
    if total > limit:
        for items, size in sorted(_list_slots(data, never_cut), key=lambda x: -x[1]):
            while len(items) > 1 and total > limit:
                dropped = items.pop()
                total -= _json_len(dropped) + 1
    result = _dump_json(data)
    # 计长是估算，兜底再走一轮（最多几次，每次都在真长度上收敛）
    guard = 0
    while len(result) > limit and guard < 12:
        guard += 1
        if not _shrink_fallback(data, protected, order):
            break
        result = _dump_json(data)
    if len(result) > limit:
        # 实在压不下去（例如保护键里就有一个超长字符串）→ 给合法的最小对象
        result = _dump_json({"truncated": True, "memory_context": {},
                             "truncated_note": "上下文超出预算，已压到最小"})
    return result


def _shrink_fallback(data: Any, keep: set, drop_first: tuple) -> bool:
    """计长估算不准时的兜底：真长度上再压一次（每步都严格变小）。"""
    if isinstance(data, dict):
        for name in list(drop_first) + [k for k in data if k not in drop_first]:
            if name in data and name not in keep:
                data.pop(name, None)
                return True
    slots = _string_slots(data, keep)
    if slots:
        container, key, size = max(slots, key=lambda x: x[2])
        if size >= 2:
            container[key] = container[key][: size // 2]
            return True
    lists = _list_slots(data, keep)
    if lists:
        items = max(lists, key=lambda x: x[1])[0]
        if len(items) > 1:
            items.pop()
            return True
    return False


def parse_json_value(text: str) -> Any | None:
    """像 parse_json_object，但接受任何 JSON 顶层值（数组/标量等）。

    宽容模式：尾逗号（模型输出常见）会先剥掉再试。
    """
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    value = _try_loads(text)
    if value is not _UNPARSED:
        return value
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        quoted = False
        escaped = False
        end = -1
        for index in range(start, len(text)):
            char = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end > start:
            extracted = _try_loads(text[start:end])
            if extracted is not _UNPARSED:
                return extracted
    return None
