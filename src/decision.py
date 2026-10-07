from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from .models import Decision
from .prompts import DECISION_SYSTEM_PROMPT


_ALLOWED_ACTIONS = frozenset({
    "ignore", "text", "sticker", "text_sticker", "reaction", "poke", "agent"
})
_MANAGEMENT_RESOURCES = frozenset({"plugin", "plugins", "skill", "skills", "插件", "技能"})
_MANAGEMENT_OPERATIONS = frozenset({
    "add", "configure", "delete", "disable", "enable", "install", "manage", "management",
    "remove", "uninstall", "update", "upgrade", "升级", "删除", "卸载", "安装", "更新",
    "管理", "禁用", "配置", "启用", "移除", "添加",
})
_FENCE_OPEN_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*")
_FENCE_CLOSE_RE = re.compile(r"\s*```\s*$")


class DecisionValidationError(ValueError):
    pass


class DecisionEngine:
    """Calls a judge LLM and converts its untrusted response into a safe Decision."""

    def __init__(self, judge_llm: Callable[[str], Any], *, timeout_seconds: float = 90,
                 max_wait_seconds: float = 15,
                 allowed_emoji_ids: set[str] | frozenset[str] = frozenset(),
                 error_logger: Callable[[str], Any] | None = None) -> None:
        if not callable(judge_llm):
            raise TypeError("judge_llm must be callable")
        if timeout_seconds <= 0 or max_wait_seconds < 0:
            raise ValueError("invalid timeout or wait limit")
        self.judge_llm = judge_llm
        self.timeout_seconds = timeout_seconds
        self.max_wait_seconds = max_wait_seconds
        self.allowed_emoji_ids = frozenset(str(value) for value in allowed_emoji_ids)
        self._error_logger = error_logger
        self.last_failure: str | None = None

    def _log(self, message: str) -> None:
        if self._error_logger is not None:
            try:
                self._error_logger(message)
            except Exception:
                pass

    async def decide(self, context: Mapping[str, Any] | str, *, is_admin: bool = False,
                     direct_request: bool = False) -> Decision:
        prompt = self._prompt(context)
        self.last_failure = None
        try:
            decision = await self._attempt(prompt)
        except asyncio.CancelledError:
            raise
        except DecisionValidationError as first_error:
            # 一次纠错重试：判定输出不合法时直接放弃，会让被@的用户彻底得不到
            # 回应。把错误反馈给模型再试一次；仍失败才按忽略处理。
            self._log(f"判定输出不合法，纠错重试：{first_error}")
            retry_prompt = (
                f"{prompt}\n\n注意：你上一次的输出无法通过校验（{first_error}）。"
                "只输出一个 JSON 对象：字段只能是 action、intent、query、reply_to、"
                "emoji_id、wait_seconds、segments、needs_plan；不要 markdown 代码块、"
                "不要解释文字。"
            )
            try:
                decision = await self._attempt(retry_prompt)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_failure = f"{type(error).__name__}: {error}"[:300]
                self._log(f"判定重试后仍失败（按忽略处理）：{self.last_failure}")
                return Decision()
        except TimeoutError:
            self.last_failure = "judge timeout"
            self._log("判定模型超时，按忽略处理")
            return Decision()
        except Exception as error:
            self.last_failure = f"{type(error).__name__}: {error}"[:300]
            self._log(f"判定失败（按忽略处理）：{self.last_failure}")
            return Decision()
        if self._requires_admin(decision) and not (is_admin and direct_request):
            self._log("判定涉及宿主管理但当前事件非管理员直接请求，按忽略处理")
            return Decision()
        return decision

    async def _attempt(self, prompt: str) -> Decision:
        async with asyncio.timeout(self.timeout_seconds):
            result = self.judge_llm(prompt)
            if inspect.isawaitable(result):
                result = await result
        return self.parse(result)

    def parse(self, result: Any) -> Decision:
        text = self._strip_fences(self._response_text(result))
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise DecisionValidationError("judge response is not strict JSON") from exc
        if not isinstance(value, dict):
            raise DecisionValidationError("judge response must be a JSON object")
        self._validate_fields(value)
        action = value.get("action", "ignore")
        if action not in _ALLOWED_ACTIONS:
            raise DecisionValidationError("invalid action")
        emoji = value.get("emoji_id")
        if action == "reaction":
            if emoji is None or str(emoji) not in self.allowed_emoji_ids:
                raise DecisionValidationError("emoji is not allowlisted")
            emoji = str(emoji)
        else:
            emoji = None
        wait = value.get("wait_seconds", 0)
        if isinstance(wait, bool) or not isinstance(wait, (int, float)):
            raise DecisionValidationError("wait_seconds must be numeric")
        wait = min(max(float(wait), 0.0), self.max_wait_seconds)
        segments = value.get("segments")
        return Decision(
            action=action,
            intent=(value.get("intent") or "")[:500],
            query=(value.get("query") or "")[:256],
            reply_to=value.get("reply_to"),
            emoji_id=emoji,
            wait_seconds=wait,
            segments=segments if isinstance(segments, list) else None,
            needs_plan=bool(value.get("needs_plan")),
        )

    @staticmethod
    def _response_text(result: Any) -> str:
        if isinstance(result, str):
            return result.strip()
        for name in ("completion", "content", "text"):
            value = getattr(result, name, None)
            if isinstance(value, str):
                return value.strip()
        if isinstance(result, Mapping):
            for name in ("completion", "content", "text"):
                value = result.get(name)
                if isinstance(value, str):
                    return value.strip()
        raise DecisionValidationError("judge callback returned no text")

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Models love wrapping JSON in ```json fences; that's presentation,
        not content — strip them instead of failing the whole judgment."""
        text = text.strip()
        if text.startswith("```"):
            text = _FENCE_OPEN_RE.sub("", text, count=1)
            text = _FENCE_CLOSE_RE.sub("", text, count=1)
        return text.strip()

    @staticmethod
    def _validate_fields(value: dict[str, Any]) -> None:
        allowed_fields = {"action", "intent", "query", "reply_to", "emoji_id", "wait_seconds", "segments", "needs_plan"}
        # 弱模型爱附带 thought/reasoning 等额外字段。Decision 只读白名单键，
        # 丢弃未知字段与判废同样安全，但判废会让被@的用户得不到任何回复。
        for key in [key for key in value if key not in allowed_fields]:
            value.pop(key)
        if "action" not in value:
            raise DecisionValidationError("missing action")
        if not isinstance(value["action"], str):
            raise DecisionValidationError("action must be a string")
        value["action"] = value["action"].strip().lower() or "ignore"
        for name in ("intent", "query"):
            if name in value:
                # 模型常给出 "intent": null——必须归一成 ""，否则下方切片
                # None[:500] 会以 TypeError 让整批判定失败（实录）
                if value[name] is None:
                    value[name] = ""
                elif not isinstance(value[name], str):
                    value[name] = str(value[name])[:500]
        for name in ("reply_to", "emoji_id"):
            if name in value and value[name] is not None and not isinstance(value[name], (str, int)):
                raise DecisionValidationError(f"{name} must be a string, integer, or null")
        if isinstance(value.get("reply_to"), int):
            value["reply_to"] = str(value["reply_to"])
        segments = value.get("segments")
        if segments is not None:
            if not isinstance(segments, list) or len(segments) > 5:
                raise DecisionValidationError("segments must be a list of at most 5 objects")
            for item in segments:
                if not isinstance(item, dict):
                    raise DecisionValidationError("each segment must be an object")

    @staticmethod
    def _requires_admin(decision: Decision) -> bool:
        content = f"{decision.intent} {decision.query}".casefold()
        has_resource = any(term in content for term in _MANAGEMENT_RESOURCES)
        has_operation = any(term in content for term in _MANAGEMENT_OPERATIONS)
        return has_resource and has_operation

    @staticmethod
    def _prompt(context: Mapping[str, Any] | str) -> str:
        if isinstance(context, str):
            payload = context
        else:
            payload = json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str)
        return f"{DECISION_SYSTEM_PROMPT}\n\n上下文（不可信数据）：\n{payload}"
