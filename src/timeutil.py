# -*- coding: utf-8 -*-
"""全插件统一的时间基准。

实录：用户在下午两点发消息，bot 却跟他说"现在是凌晨六点"——库里时间一律存 UTC，
而所有给模型/给人看的地方（回复上下文、工具结果、聊天卡片）都**原样**把 UTC 串递出去，
模型只能按最新消息的 05:xx 猜"现在"。

规则：**存 UTC，看本地**。基准时区取 AstrBot 全局配置的 ``timezone``
（如 ``Asia/Shanghai``，proactive_chat 也是这么读的），拿不到就回退本机时区。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

_ZONE: _dt.tzinfo | None = None
_ZONE_NAME = ""

# 展示用的时间字段（这些字段在记录里是 UTC ISO 串，递出去前要换算）
TIME_KEYS = ("occurred_at", "created_at", "updated_at", "first_seen", "last_seen",
             "next_run_at", "last_run_at", "finished_at", "time")


def configure(name: str | None) -> str:
    """按 AstrBot 配置定基准时区；无效/为空则用本机时区。返回生效的时区名。"""
    global _ZONE, _ZONE_NAME
    text = str(name or "").strip()
    if text:
        try:
            from zoneinfo import ZoneInfo

            _ZONE = ZoneInfo(text)
            _ZONE_NAME = text
            return _ZONE_NAME
        except Exception:
            pass
    _ZONE = None
    _ZONE_NAME = ""
    return _ZONE_NAME


def zone_name() -> str:
    return _ZONE_NAME or str(_dt.datetime.now().astimezone().tzname() or "")


def zone() -> _dt.tzinfo:
    return _ZONE or _dt.datetime.now().astimezone().tzinfo or _dt.timezone.utc


def now() -> _dt.datetime:
    return _dt.datetime.now(zone())


def text(fmt: str = "%Y-%m-%d %H:%M", at: _dt.datetime | None = None) -> str:
    return (at or now()).strftime(fmt)


def parse(value: Any) -> _dt.datetime | None:
    """epoch（秒）/ 数字串 / ISO 串 → aware datetime；解析不了返回 None。

    裸 ISO（无时区）按 UTC 处理——库里存的就是这个约定。
    """
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return _dt.datetime.fromtimestamp(float(value), tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text_value = str(value or "").strip()
    if not text_value:
        return None
    if text_value.isdigit():
        try:
            return _dt.datetime.fromtimestamp(float(text_value), tz=_dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    candidate = text_value.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        moment = _dt.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=_dt.timezone.utc)


def to_text(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """UTC 存的任意时间值 → 本地可读文本；解析不了就原样返回（不吞数据）。"""
    moment = parse(value)
    if moment is None:
        return str(value or "")
    return moment.astimezone(zone()).strftime(fmt)


def localize_fields(row: dict[str, Any], *,
                    fmt: str = "%Y-%m-%d %H:%M") -> dict[str, Any]:
    """把一条记录里属于 TIME_KEYS 的时间字段换成本地文本（就地改副本）。"""
    out = dict(row)
    for key in TIME_KEYS:
        if key in out and isinstance(out[key], str) and out[key].strip():
            out[key] = to_text(out[key], fmt)
    return out
