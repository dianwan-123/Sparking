from __future__ import annotations

import base64
import functools
import inspect
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .models import NormalizedMessage


class OneBotActionError(RuntimeError):
    """A transport-independent OneBot action failure."""

    def __init__(self, action: str, message: str, response: Any = None) -> None:
        super().__init__(f"{action}: {message}")
        self.action = action
        self.response = safe_serialize(response)


def safe_serialize(value: Any, *, max_depth: int = 8, max_items: int = 200,
                   max_bytes: int = 64_000, max_string_bytes: int = 8_000,
                   max_binary_bytes: int = 8_000) -> Any:
    """Convert untrusted values to JSON data bounded by per-value and global budgets."""
    if max_depth < 0 or max_items < 0 or max_bytes < 2 or max_string_bytes < 0 or max_binary_bytes < 0:
        raise ValueError("invalid serialization budget")
    seen: set[int] = set()
    remaining = max_bytes

    def take_text(text: str, limit: int) -> str:
        nonlocal remaining
        allowance = min(limit, max(remaining - 2, 0))
        encoded = text.encode("utf-8", "replace")
        if len(encoded) > allowance:
            marker = "…".encode("utf-8")
            prefix_limit = max(allowance - len(marker), 0)
            encoded = encoded[:prefix_limit]
            while encoded:
                try:
                    text = encoded.decode("utf-8") + ("…" if allowance >= len(marker) else "")
                    break
                except UnicodeDecodeError:
                    encoded = encoded[:-1]
            else:
                text = "…" if allowance >= len(marker) else ""
        remaining = max(remaining - len(text.encode("utf-8", "replace")) - 2, 0)
        return text

    def convert(item: Any, depth: int) -> Any:
        nonlocal remaining
        if remaining <= 0:
            return ""
        if item is None or isinstance(item, bool):
            remaining = max(remaining - 5, 0)
            return item
        if isinstance(item, int):
            remaining = max(remaining - len(str(item)), 0)
            return item
        if isinstance(item, str):
            return take_text(item, max_string_bytes)
        if isinstance(item, float):
            result: float | str = item if math.isfinite(item) else str(item)
            remaining = max(remaining - len(str(result)), 0)
            return result
        if isinstance(item, (bytes, bytearray, memoryview)):
            raw = bytes(item)
            truncated = len(raw) > max_binary_bytes
            raw = raw[:max_binary_bytes]
            encoded = base64.b64encode(raw).decode("ascii")
            result = {
                "type": take_text("bytes", max_string_bytes),
                "base64": take_text(encoded, max_string_bytes),
            }
            if truncated:
                result["truncated"] = True
            return result
        if depth >= max_depth:
            return take_text("<max-depth>", max_string_bytes)
        identity = id(item)
        if identity in seen:
            return take_text("<cycle>", max_string_bytes)
        if isinstance(item, Mapping):
            seen.add(identity)
            result: dict[str, Any] = {}
            try:
                for index, (key, child) in enumerate(item.items()):
                    if remaining <= 32:
                        result["<truncated>"] = True
                        break
                    if index >= max_items:
                        result["<truncated>"] = True
                        break
                    safe_key = take_text(str(key), min(max_string_bytes, 256))
                    result[safe_key] = convert(child, depth + 1)
            finally:
                seen.discard(identity)
            return result
        if isinstance(item, Sequence) and not isinstance(item, str):
            seen.add(identity)
            values: list[Any] = []
            try:
                for index, child in enumerate(item):
                    if remaining <= 16 or index >= max_items:
                        values.append("<truncated>")
                        break
                    values.append(convert(child, depth + 1))
                return values
            except (TypeError, AttributeError):
                return ["<unserializable-sequence>"]
            finally:
                seen.discard(identity)
        try:
            return take_text(str(item), max_string_bytes)
        except Exception:
            return take_text(f"<{type(item).__name__}>", max_string_bytes)

    result = convert(value, 0)
    # Structural punctuation can exceed the accounting estimate slightly; enforce hard UTF-8 size.
    while len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > max_bytes:
        if isinstance(result, dict) and result:
            result.pop(next(reversed(result)))
        elif isinstance(result, list) and result:
            result.pop()
        else:
            return ""
    return result


def extract_raw_event(event: Any) -> dict[str, Any]:
    """Extract an event mapping from aiocqhttp/AstrBot-shaped objects by duck typing."""
    if isinstance(event, Mapping):
        return safe_serialize(event)
    candidates = [event]
    for name in ("raw_event", "raw", "message_obj", "event"):
        try:
            candidate = getattr(event, name)
        except (AttributeError, Exception):
            continue
        candidates.append(candidate)
        if name == "message_obj":
            for nested_name in ("raw_event", "raw_message", "raw"):
                try:
                    candidates.append(getattr(candidate, nested_name))
                except (AttributeError, Exception):
                    pass
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return safe_serialize(candidate)
    fields = {}
    for name in ("post_type", "message_type", "notice_type", "sub_type", "self_id",
                 "group_id", "user_id", "target_id", "message_id", "message", "time", "sender"):
        try:
            fields[name] = getattr(event, name)
        except (AttributeError, Exception):
            pass
    return safe_serialize(fields)


def normalize_raw_event(event: Any) -> dict[str, Any]:
    raw = extract_raw_event(event)
    if is_group_message(raw):
        raw["event_kind"] = "group_message"
    elif is_poke_notice(raw):
        raw["event_kind"] = "poke"
    elif raw.get("post_type") == "message" and raw.get("message_type") == "private":
        raw["event_kind"] = "private_message"
    elif raw.get("post_type") == "request":
        raw["event_kind"] = "request"
    elif raw.get("post_type") == "notice":
        raw["event_kind"] = "notice"
    else:
        raw["event_kind"] = "unsupported"
    return raw


def is_request_event(event: Any) -> bool:
    raw = event if isinstance(event, Mapping) else extract_raw_event(event)
    return raw.get("post_type") == "request" and bool(raw.get("request_type"))


def extract_request_info(event: Any) -> dict[str, Any]:
    raw = event if isinstance(event, Mapping) else extract_raw_event(event)
    return {
        "request_type": str(raw.get("request_type", "")),
        "sub_type": str(raw.get("sub_type", "")),
        "flag": str(raw.get("flag", "")),
        "user_id": str(raw.get("user_id", "")),
        "group_id": str(raw.get("group_id", "") or ""),
        "comment": str(raw.get("comment", "") or ""),
    }


def _notice_text(raw: dict[str, Any]) -> str:
    notice_type = str(raw.get("notice_type", ""))
    sub_type = str(raw.get("sub_type", "") or "")
    user = str(raw.get("user_id", ""))
    operator = str(raw.get("operator_id", "") or user)
    if notice_type == "group_increase":
        return f"[{user} 加入了本群（{sub_type or '同意'}）]" if sub_type != "invite" else f"[{user} 被邀请加入了本群]"
    if notice_type == "group_decrease":
        if sub_type == "kick_me":
            return f"[{operator} 把机器人踢出了群聊]"
        if sub_type == "kick":
            return f"[{operator} 把 {user} 踢出了群聊]"
        return f"[{user} 离开了本群]"
    if notice_type == "friend_add":
        return f"[新好友 {user} 已添加]"
    if notice_type == "friend_recall":
        return f"[{user} 撤回了一条私聊消息]"
    if notice_type == "group_recall":
        return f"[{operator} 撤回了一条群消息]"
    if notice_type == "group_upload":
        file = raw.get("file")
        name = file.get("name", "") if isinstance(file, Mapping) else ""
        return f"[{user} 上传了文件 {name}]"
    if notice_type == "essence":
        return f"[消息被设为精华]" if sub_type == "add" else "[消息被移出精华]"
    if notice_type == "group_admin":
        return (f"[{user} 被设为管理员]" if sub_type == "set"
                else f"[{user} 被取消管理员]")
    if notice_type == "group_card":
        return (f"[{user} 的群名片改为 {raw.get('card_new', '')}"
                f"（原 {raw.get('card_old', '')}）]")
    if notice_type == "notify":
        target = str(raw.get("target_id", "") or "")
        if sub_type == "poke":
            return f"[{user} 戳了戳 {target}]"
        if sub_type == "lucky_king":
            return f"[{user} 成为群聊运气王（红包）]"
        if sub_type == "honor":
            return f"[{user} 获得群荣誉 {raw.get('honor_type', '')}]"
        return f"[群通知 notify/{sub_type}：{user}→{target}]"
    if notice_type == "offline_file":
        file = raw.get("file") if isinstance(raw.get("file"), Mapping) else {}
        return f"[{user} 发送了离线文件 {file.get('name', '')} {file.get('url', '')}]"
    if notice_type in {"group_msg_emoji_like", "message_reaction"}:
        likes = raw.get("likes") if isinstance(raw.get("likes"), list) else []
        emoji = ""
        if likes and isinstance(likes[0], Mapping):
            emoji = str(likes[0].get("emoji_id", ""))
        return f"[{user} 对消息做出了表情回应 {emoji}]"
    # 未知/新增事件类型：带上有意义的字段细节，别只留一句占位
    detail = {
        key: value for key, value in raw.items()
        if key not in {"post_type", "self_id", "time", "notice_type", "sub_type",
                       "group_id", "user_id", "operator_id"}
        and isinstance(value, (str, int, float, bool))
    }
    tail = ""
    if detail:
        try:
            tail = " " + json.dumps(detail, ensure_ascii=False)[:200]
        except (TypeError, ValueError):
            tail = ""
    return f"[群通知 {notice_type}/{sub_type}{tail}]"


def normalize_request_record(event: Any) -> NormalizedMessage | None:
    """Turn friend/group requests into query-only memory records (flag included)."""
    raw = event if isinstance(event, Mapping) else extract_raw_event(event)
    if not is_request_event(raw):
        return None
    info = extract_request_info(raw)
    timestamp = raw.get("time")
    try:
        occurred = datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        occurred = datetime.now(timezone.utc).isoformat()
    comment = info["comment"] or "无"
    if info["request_type"] == "friend":
        conversation = f"private:{info['user_id']}"
        text = f"[收到好友申请 user_id={info['user_id']} flag={info['flag']} 说明：{comment}]"
        event_type = "request.friend"
    else:
        conversation = info["group_id"]
        if info["sub_type"] == "invite":
            text = (f"[收到群邀请 group_id={info['group_id']} 来自 {info['user_id']} "
                    f"sub_type=invite flag={info['flag']} 说明：{comment}]")
        else:
            text = (f"[群 {info['group_id']} 收到入群申请 user_id={info['user_id']} "
                    f"sub_type=add flag={info['flag']} 说明：{comment}]")
        event_type = "request.group"
    upstream = f"request:{timestamp}:{info['request_type']}:{info['user_id']}:{info['flag'][:24]}"
    return NormalizedMessage(
        platform="aiocqhttp", account_id=str(raw.get("self_id", "")),
        conversation_id=conversation, upstream_message_id=upstream,
        sender_id=info["user_id"] or "system", sender_name="system",
        text=text, occurred_at=occurred, raw_event=raw, parts=[],
        event_type=event_type,
    )


def normalize_notice_event(event: Any) -> NormalizedMessage | None:
    """Turn non-poke OneBot notices into synthetic memory messages."""
    raw = event if isinstance(event, Mapping) else extract_raw_event(event)
    if raw.get("post_type") != "notice" or is_poke_notice(raw):
        return None
    notice_type = str(raw.get("notice_type", ""))
    if not notice_type:
        return None
    sender = raw.get("sender") if isinstance(raw.get("sender"), Mapping) else {}
    timestamp = raw.get("time")
    try:
        occurred = datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        occurred = datetime.now(timezone.utc).isoformat()
    group_id = str(raw.get("group_id", "") or "")
    user_id = str(raw.get("user_id", ""))
    conversation = group_id if group_id else f"private:{user_id}"
    upstream = f"notice:{timestamp}:{notice_type}:{user_id}:{raw.get('sub_type', '')}"
    return NormalizedMessage(
        platform="aiocqhttp", account_id=str(raw.get("self_id", "")),
        conversation_id=conversation, upstream_message_id=upstream,
        sender_id=user_id or "system", sender_name=str(sender.get("card") or sender.get("nickname") or "system"),
        text=_notice_text(raw), occurred_at=occurred, raw_event=raw, parts=[],
        event_type=f"notice.{notice_type}",
    )


def is_group_message(event: Any) -> bool:
    raw = event if isinstance(event, Mapping) else extract_raw_event(event)
    return (raw.get("post_type") == "message" and raw.get("message_type") == "group"
            and raw.get("sub_type") in (None, "normal"))


def is_poke_notice(event: Any) -> bool:
    raw = event if isinstance(event, Mapping) else extract_raw_event(event)
    return (raw.get("post_type") == "notice" and raw.get("notice_type") == "notify"
            and raw.get("sub_type") == "poke" and raw.get("group_id") is not None)


def _parts(raw_message: Any) -> list[dict[str, Any]]:
    if isinstance(raw_message, str):
        return [{"type": "text", "data": {"text": raw_message}}]
    if not isinstance(raw_message, Sequence) or isinstance(raw_message, (str, bytes)):
        return []
    result = safe_serialize(raw_message)
    return [part for part in result if isinstance(part, dict)]


def _text_and_reply(parts: list[dict[str, Any]], fallback: Any) -> tuple[str, str | None]:
    texts: list[str] = []
    reply_to = None
    for part in parts:
        data = part.get("data")
        if not isinstance(data, Mapping):
            continue
        if part.get("type") == "text" and isinstance(data.get("text"), str):
            texts.append(data["text"])
        elif part.get("type") == "reply" and data.get("id") is not None:
            reply_to = str(data["id"])
    if not texts and isinstance(fallback, str):
        texts.append(fallback)
    return "".join(texts), reply_to


def normalize_event(event: Any) -> NormalizedMessage | None:
    """Normalize supported group/private messages and group poke notices."""
    raw = normalize_raw_event(event)
    is_private = (
        raw.get("post_type") == "message" and raw.get("message_type") == "private"
        and raw.get("sub_type") in (None, "normal", "friend", "group")
    )
    if not (is_group_message(raw) or is_poke_notice(raw) or is_private):
        return None
    sender = raw.get("sender") if isinstance(raw.get("sender"), Mapping) else {}
    timestamp = raw.get("time")
    try:
        occurred = datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        occurred = datetime.now(timezone.utc).isoformat()
    poke = is_poke_notice(raw)
    if not poke and raw.get("message_id") is None:
        return None
    parts = [] if poke else _parts(raw.get("message"))
    text, reply_to = _text_and_reply(parts, raw.get("raw_message"))
    sender_id = str(raw.get("user_id", ""))
    message_id = raw.get("message_id")
    if message_id is None and poke:
        message_id = f"poke:{raw.get('time', '')}:{sender_id}:{raw.get('target_id', '')}"
    if poke:
        conversation = str(raw.get("group_id", ""))
    elif is_private:
        conversation = f"private:{sender_id}"
    else:
        conversation = str(raw.get("group_id", ""))
    return NormalizedMessage(
        platform="aiocqhttp", account_id=str(raw.get("self_id", "")),
        conversation_id=conversation, upstream_message_id=str(message_id or ""),
        sender_id=sender_id, sender_name=str(sender.get("card") or sender.get("nickname") or sender_id),
        text=text, occurred_at=occurred, raw_event=raw, parts=parts, reply_to=reply_to,
        event_type="notice.poke" if poke else "message.created",
    )


_ALLOWED_ACTIONS = {"set_msg_emoji_like", "send_poke", "get_msg", "get_image"}


def _is_action_bound_caller(candidate: Any, action: str) -> bool:
    """True when aiocqhttp's `Api.__getattr__` already baked this action in.

    `bot.get_image` yields `functools.partial(self.call_action, "get_image")`;
    the bound name must equal the action we are about to run — arbitrary
    attribute probes (e.g. `bot.bot`) must never be mistaken for a transport.
    """
    if not isinstance(candidate, functools.partial):
        return False
    if getattr(candidate.func, "__name__", "") != "call_action":
        return False
    return bool(candidate.args) and candidate.args[0] == action


def _is_instance_bound_caller(candidate: Any) -> bool:
    """True for `functools.partial(CQHttp.call_action, bot)` shapes.

    Here the bound first argument is the transport instance, not an action
    name, so the caller still expects the action as its first argument.
    """
    if not isinstance(candidate, functools.partial):
        return False
    if getattr(candidate.func, "__name__", "") != "call_action":
        return False
    return bool(candidate.args) and not isinstance(candidate.args[0], str)


def _resolve_caller(target: Any, action: str) -> tuple[Any, bool] | None:
    """Probe every plausible OneBot transport shape; property errors are skipped.

    Returns (callable, takes_action). takes_action is False when the action is
    already bound (aiocqhttp `Api.__getattr__` partials and dynamic methods).
    """
    if _is_action_bound_caller(target, action):
        return target, False
    if _is_instance_bound_caller(target):
        return target, True
    direct_bot = None
    direct_api = None
    try:
        direct_bot = getattr(target, "bot", None)
    except Exception:
        direct_bot = None
    try:
        direct_api = getattr(target, "api", None)
    except Exception:
        direct_api = None
    for candidate in (direct_bot, direct_api, getattr(direct_bot, "api", None)
                      if direct_bot is not None else None):
        if _is_action_bound_caller(candidate, action):
            return candidate, False
        if _is_instance_bound_caller(candidate):
            return candidate, True
    candidates = (
        (lambda: getattr(target, "call_action", None), True),
        (lambda: getattr(direct_api, "call_action", None) if direct_api is not None else None, True),
        (lambda: getattr(direct_bot, "call_action", None) if direct_bot is not None else None, True),
        (lambda: getattr(getattr(direct_bot, "api", None), "call_action", None)
         if direct_bot is not None else None, True),
        # AstrBot builds may expose event.bot as a callable (e.g. functools.partial of call_action)
        (lambda: target if callable(target) and not inspect.isclass(target) else None, True),
        (lambda: getattr(target, action, None), False),
    )
    for getter, takes_action in candidates:
        try:
            candidate = getter()
        except Exception:
            continue
        if candidate is None:
            continue
        if _is_action_bound_caller(candidate, action):
            return candidate, False
        if callable(candidate) and getattr(candidate, "__name__", "") in {
            "call_action", "__call__",
        }:
            return candidate, takes_action
    return None


async def call_action(client: Any, action: str, **params: Any) -> Any:
    """Call a small allowlisted OneBot API surface without importing its SDK."""
    if action not in _ALLOWED_ACTIONS:
        raise OneBotActionError(action, "action is not allowlisted")
    # Never resolve the target through `getattr(client, "bot", client)`: on
    # aiocqhttp that attribute probe itself goes through `Api.__getattr__` and
    # hands back a partial whose bound action is the literal "bot", which the
    # API then rejects with retcode 1404 (不支持的Api bot). `_resolve_caller`
    # probes transports explicitly and only accepts partials whose bound action
    # or instance is genuine.
    resolved = _resolve_caller(client, action)
    if resolved is None:
        raise OneBotActionError(
            action, f"no callable transport on {type(client).__name__}"
        )
    caller, takes_action = resolved
    try:
        response = caller(**params) if not takes_action else caller(action, **params)
        if inspect.isawaitable(response):
            response = await response
    except OneBotActionError:
        raise
    except Exception as exc:
        raise OneBotActionError(action, f"{type(exc).__name__}: {exc}") from exc
    if isinstance(response, Mapping):
        failed = response.get("status") in {"failed", "error"}
        retcode = response.get("retcode", 0)
        if failed or (isinstance(retcode, int) and retcode != 0):
            raise OneBotActionError(action, "OneBot returned failure", response)
    return response


async def set_msg_emoji_like(client: Any, message_id: str | int, emoji_id: str | int) -> Any:
    return await call_action(client, "set_msg_emoji_like", message_id=message_id, emoji_id=str(emoji_id))


async def send_poke(client: Any, user_id: str | int, group_id: str | int) -> Any:
    return await call_action(client, "send_poke", user_id=user_id, group_id=group_id)


async def get_msg(client: Any, message_id: str | int) -> Any:
    return await call_action(client, "get_msg", message_id=message_id)


async def get_image(client: Any, file_ref: str) -> Any:
    """Fetch an image by segment file reference.

    NapCat's /get_image accepts the reference as `file` (path, URL or base64)
    or as `file_id`; callers may also hand us a whole segment mapping. The first
    form that the transport accepts wins, and only allowlist failures are
    retried so real API errors surface instead of being masked.
    """
    reference = file_ref
    if isinstance(file_ref, Mapping):
        data = file_ref.get("data") if isinstance(file_ref.get("data"), Mapping) else file_ref
        reference = str(
            data.get("file") or data.get("file_id") or data.get("url") or ""
        ).strip()
    reference = str(reference or "").strip()
    if not reference:
        raise OneBotActionError("get_image", "empty image reference")
    attempts = [("file", reference)]
    if "://" not in reference and not reference.startswith("base64:"):
        attempts.append(("file_id", reference))
    last: Exception | None = None
    for key, value in attempts:
        try:
            return await call_action(client, "get_image", **{key: value})
        except OneBotActionError as exc:
            last = exc
            if "allowlisted" in str(exc):
                raise
    raise last if last is not None else OneBotActionError("get_image", "no attempt made")
