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
