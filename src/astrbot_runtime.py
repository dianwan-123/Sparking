from __future__ import annotations

import inspect
import platform
import sys
from collections.abc import Mapping
from copy import deepcopy
from typing import Any


def _value(obj: object, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _safe_text(value: Any, limit: int = 4096) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def _marked_schema(value: Any) -> Any:
    """Copy a schema while explicitly marking every description as untrusted."""
    if isinstance(value, Mapping):
        result = {str(k): _marked_schema(v) for k, v in value.items()}
        if "description" in result:
            result["description_trusted"] = False
        return result
    if isinstance(value, (list, tuple)):
        return [_marked_schema(item) for item in value]
    try:
        return deepcopy(value)
    except Exception:
        return _safe_text(value)


def _iter_tools(tool_set: Any) -> list[Any]:
    if tool_set is None:
        return []
    for attr in ("tools", "func_list"):
        tools = _value(tool_set, attr)
        if tools is not None:
            try:
                return list(tools)
            except TypeError:
                return []
    if isinstance(tool_set, Mapping):
        return list(tool_set.values())
    try:
        return list(tool_set)
    except TypeError:
        return []


def _plugin_id(star: Any) -> str:
    return _safe_text(
        _value(star, "plugin_id")
        or _value(star, "name")
        or _value(star, "module_path"),
        256,
    )


def _owner_for_tool(tool: Any, stars: list[Any]) -> str | None:
    module = _safe_text(_value(tool, "handler_module_path"), 512)
    if not module:
        handler = _value(tool, "handler")
        module = _safe_text(getattr(handler, "__module__", ""), 512)
    best: tuple[int, str] | None = None
    for star in stars:
        path = _safe_text(_value(star, "module_path"), 512)
        if path and (module == path or module.startswith(path + ".")):
            candidate = (len(path), _plugin_id(star))
            if best is None or candidate[0] > best[0]:
                best = candidate
    return best[1] if best else None


def _call_value(obj: Any, *names: str) -> Any:
    for name in names:
        value = _value(obj, name)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if value is not None and not inspect.isawaitable(value):
            return value
    return None


def _skill_entries(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        for key in ("skills", "items", "data"):
            if key in value:
                return _skill_entries(value[key])
        return list(value.values())
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


async def _available_skills(context: Any) -> list[dict[str, Any]]:
    candidates = []
    for owner in (context, _value(context, "skill_manager"), _value(context, "skills")):
        if owner is None:
            continue
        getter = getattr(owner, "list_skills", None) or getattr(owner, "get_skills", None)
        if callable(getter):
            try:
                candidates = _skill_entries(await _resolve(getter()))
            except Exception:
                candidates = []
            if candidates:
                break
    result = []
    for skill in candidates:
        active = bool(_value(skill, "active", _value(skill, "enabled", True)))
        if not active:
            continue
        result.append(
            {
                "name": _safe_text(_value(skill, "name"), 128),
                "description": _safe_text(_value(skill, "description"), 2048),
                "description_trusted": False,
                "source_type": _safe_text(_value(skill, "source_type"), 64),
                "readonly": bool(_value(skill, "readonly", False)),
            }
        )
    return result


async def build_runtime_manifest(context: Any, req: Any, event: Any = None) -> dict[str, Any]:
    """Build runtime facts without importing AstrBot or exposing plugin config."""
    getter = getattr(context, "get_all_stars", None)
    stars = list(await _resolve(getter())) if callable(getter) else []
    plugins = []
    for star in stars:
        description = _safe_text(_value(star, "desc"), 4096)
        short_description = _safe_text(_value(star, "short_desc"), 1024)
        plugins.append(
            {
                "id": _plugin_id(star),
                "name": _safe_text(_value(star, "name"), 256),
                "display_name": _safe_text(_value(star, "display_name"), 256),
                "author": _safe_text(_value(star, "author"), 256),
                "version": _safe_text(_value(star, "version"), 128),
                "active": bool(_value(star, "activated", False)),
                "description": description,
                "description_trusted": False,
                "short_description": short_description,
                "short_description_trusted": False,
            }
        )
    effective_tools = []
    for tool in _iter_tools(_value(req, "func_tool")):
        if not bool(_value(tool, "active", True)):
            continue
        effective_tools.append(
            {
                "name": _safe_text(_value(tool, "name"), 256),
                "description": _safe_text(_value(tool, "description"), 4096),
                "description_trusted": False,
                "parameters": _marked_schema(_value(tool, "parameters", {})),
                "plugin_id": _owner_for_tool(tool, stars),
            }
        )
    runtime = {
        "astrbot_version": _safe_text(
            _call_value(context, "astrbot_version", "version", "get_version"), 128
        ),
        "python_version": platform.python_version(),
        "implementation": sys.implementation.name,
    }
    event_facts = {
        "platform": _safe_text(
            _call_value(event, "get_platform_name", "platform_name"), 128
        ),
        "bot_id": _safe_text(_call_value(event, "get_self_id", "self_id"), 256),
        "group_id": _safe_text(_call_value(event, "get_group_id", "group_id"), 256),
        "session_id": _safe_text(
            _call_value(event, "get_session_id", "session_id"), 512
        ),
        "unified_session": _safe_text(
            _call_value(event, "unified_msg_origin"), 1024
        ),
    }
    return {
        "runtime": runtime,
        "event": event_facts,
        "plugins": plugins,
        "effective_tools": effective_tools,
        "skills": await _available_skills(context),
        "trust_policy": {"all_descriptions_untrusted": True},
    }


collect_runtime_manifest = build_runtime_manifest
