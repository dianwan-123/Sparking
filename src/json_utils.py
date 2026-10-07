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


def compact_json(value: Any, limit: int = 12000) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


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
