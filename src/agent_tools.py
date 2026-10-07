from __future__ import annotations

import hashlib
import inspect
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .astrbot_runtime import build_runtime_manifest
from .skill_manager import SkillManager


_PLUGIN_ACTIONS = {
    "安装": "install", "更新": "update", "启用": "enable",
    "禁用": "disable", "重载": "reload",
    "install": "install", "update": "update", "enable": "enable",
    "disable": "disable", "reload": "reload",
}
_SKILL_ACTIONS = {
    "创建": "create", "修改": "update", "启用": "enable", "禁用": "disable",
    "create": "create", "modify": "update", "update": "update",
    "enable": "enable", "disable": "disable",
}
_PLUGIN_RE = re.compile(
    r"^(?:请\s*)?(安装|更新|启用|禁用|重载|install|update|enable|disable|reload)"
    r"\s*(?:插件|plugin)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_.\-/]{0,254})\s*[。！!]?\s*$",
    re.IGNORECASE,
)
_SKILL_RE = re.compile(
    r"^(?:请\s*)?(创建|修改|启用|禁用|create|modify|update|enable|disable)"
    r"\s*(?:skill|技能)\s*[:：]?\s*([a-z0-9]+(?:-[a-z0-9]+)*)"
    r"(?:[ \t]*\n([\s\S]+))?[ \t]*$",
    re.IGNORECASE,
)
_INDIRECT_OR_QUESTION = re.compile(
    r"[?？]|(?:^|\s|，|,)(?:他说|她说|转述|引用|如果|假如|模型|助手|建议|提议|是否|能否|可否|要不要|怎么办)"
    r"|(?:吗|么|呢)[。！!\s]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Authorization:
    authorized: bool
    action: str | None
    resource: str | None
    target: str | None
    actor_id: str
    event_id: str
    command_digest: str
    issued_at: str
    reason: str
    payload_digest: str | None = None
    source: str = "text"

    def to_audit_dict(self) -> dict[str, Any]:
        return asdict(self)


def _event_value(event: Any, *names: str) -> str:
    for name in names:
        value = getattr(event, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if value is not None and not inspect.isawaitable(value):
            text = str(value).strip()
            if text:
                return text[:512]
        elif inspect.iscoroutine(value):
            value.close()
    return ""


def _component_text(component: Any) -> str | None:
    if isinstance(component, str):
        return component
    if isinstance(component, dict):
        kind = str(component.get("type", "")).lower()
        if kind not in {"text", "plain"}:
            return None
        value = component.get("text", component.get("content"))
        return value if isinstance(value, str) else None
    kind = type(component).__name__.lower()
    if kind not in {"plain", "text", "textcomponent"}:
        return None
    value = getattr(component, "text", getattr(component, "content", None))
    return value if isinstance(value, str) else None


def _message_components(event: Any) -> tuple[str, list[Any] | None]:
    """Return (state, components); async component getters fail closed."""
    found_getter = False
    for name in ("get_messages", "get_message_chain"):
        getter = getattr(event, name, None)
        if callable(getter):
            found_getter = True
            try:
                value = getter()
            except Exception:
                return "invalid", None
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                return "async", None
            if hasattr(value, "chain"):
                value = value.chain
            if isinstance(value, (list, tuple)):
                return "components", list(value)
            return "invalid", None
    message_obj = getattr(event, "message_obj", None)
    if message_obj is not None:
        value = getattr(message_obj, "message", None)
        if hasattr(value, "chain"):
            value = value.chain
        if isinstance(value, (list, tuple)):
            return "components", list(value)
        return "invalid", None
    return ("invalid" if found_getter else "absent"), None


def current_plain_text(event: Any) -> str | None:
    """Return text only when authoritative current components are all plain text."""
    state, components = _message_components(event)
    if state == "components":
        if not components:
            return None
        parts = []
        for component in components:
            text = _component_text(component)
            if text is None:
                return None
            parts.append(text)
        return "".join(parts).strip() or None
    if state != "absent":
        return None
    for name in ("get_message_str", "get_plain_text"):
        getter = getattr(event, name, None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                return None
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                return None
            return value.strip() if isinstance(value, str) and value.strip() else None
    for name in ("message_str", "text"):
        value = getattr(event, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def authorize_admin_instruction(event: Any) -> Authorization:
    now = datetime.now(timezone.utc).isoformat()
    actor = _event_value(event, "get_sender_id", "sender_id", "user_id")
    event_id = _event_value(event, "get_message_id", "message_id", "id")
    text = current_plain_text(event)
    digest = _digest(text or "")

    def result(
        authorized: bool,
        reason: str,
        action: str | None = None,
        resource: str | None = None,
        target: str | None = None,
        payload: str | None = None,
    ) -> Authorization:
        return Authorization(
            authorized, action, resource, target, actor, event_id, digest, now,
            reason, _digest(payload) if payload is not None else None, "text",
        )

    checker = getattr(event, "is_admin", None)
    if not callable(checker):
        return result(False, "event has no administrator check")
    try:
        admin = checker()
        if inspect.isawaitable(admin):
            admin = await admin
    except Exception:
        return result(False, "administrator check failed")
    if not bool(admin):
        return result(False, "sender is not an administrator")
    if text is None:
        return result(False, "current event is not pure text")
    if len(text.encode("utf-8")) > 512 * 1024:
        return result(False, "instruction is too large")
    if text.startswith((">", "“", '"', "'", "「", "『", "```")):
        return result(False, "quoted text cannot authorize an operation")
    first_line = text.splitlines()[0]
    if _INDIRECT_OR_QUESTION.search(first_line):
        return result(False, "indirect, suggested, or interrogative text is not authorized")
    plugin_match = _PLUGIN_RE.fullmatch(text)
    if plugin_match:
        action, target = plugin_match.groups()
        return result(True, "direct administrator instruction", _PLUGIN_ACTIONS[action.lower()], "plugin", target)
    skill_match = _SKILL_RE.fullmatch(text)
    if skill_match:
        action_text, target, payload = skill_match.groups()
        action = _SKILL_ACTIONS[action_text.lower()]
        if action in {"create", "update"} and not payload:
            return result(False, "skill content must be present in the direct instruction")
        if action in {"enable", "disable"} and payload:
            return result(False, "unexpected skill content")
        return result(True, "direct administrator instruction", action, "skill", target, payload)
    return result(False, "text is not a supported direct imperative")


parse_authorization = authorize_admin_instruction


def _supported_operation(action: str, resource: str) -> bool:
    return (
        resource == "plugin" and action in {"install", "update", "enable", "disable", "reload"}
    ) or (
        resource == "skill" and action in {"create", "update", "enable", "disable"}
    )


async def authorize_structured_command(
    event: Any,
    *,
    action: str,
    resource: str,
    target: str,
    payload: str | None = None,
    source: str = "slash",
) -> Authorization:
    """Authorize slash/structured commands without interpreting flattened message text."""
    now = datetime.now(timezone.utc).isoformat()
    action = str(action).strip().lower()
    resource = str(resource).strip().lower()
    target = str(target).strip()
    canonical = "\0".join((source, resource, action, target, payload or ""))
    authorization = Authorization(
        False,
        action or None,
        resource or None,
        target or None,
        _event_value(event, "get_sender_id", "sender_id", "user_id"),
        _event_value(event, "get_message_id", "message_id", "id"),
        _digest(canonical),
        now,
        "structured command rejected",
        _digest(payload) if payload is not None else None,
        source,
    )
    checker = getattr(event, "is_admin", None)
    if not callable(checker):
        return authorization
    try:
        admin = checker()
        if inspect.isawaitable(admin):
            admin = await admin
    except Exception:
        return authorization
    if not admin:
        return authorization
    if not target or not _supported_operation(action, resource):
        return authorization
    return Authorization(
        True, action, resource, target, authorization.actor_id,
        authorization.event_id, authorization.command_digest,
        authorization.issued_at, "structured administrator command",
        authorization.payload_digest, source,
    )


class AuthorizationError(PermissionError):
    def __init__(self, authorization: Authorization):
        super().__init__(authorization.reason)
        self.authorization = authorization


class ScopeBoundMemoryTools:
    """A bounded callback facade that never accepts a caller-provided scope."""

    def __init__(
        self,
        scope_id: str,
        *,
        search_callback: Any = None,
        recent_callback: Any = None,
        get_callback: Any = None,
        max_limit: int = 50,
        max_output_chars: int = 24_000,
    ) -> None:
        if not scope_id:
            raise ValueError("scope_id is required")
        self._scope_id = str(scope_id)
        self._search = search_callback
        self._recent = recent_callback
        self._get = get_callback
        self.max_limit = max(1, min(int(max_limit), 100))
        self.max_output_chars = max(256, min(int(max_output_chars), 120_000))

    def _limit(self, value: int) -> int:
        try:
            return max(1, min(int(value), self.max_limit))
        except (TypeError, ValueError):
            return min(20, self.max_limit)

    async def _call(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        if not callable(callback):
            raise RuntimeError("Memory callback is not configured")
        kwargs.pop("scope_id", None)
        value = callback(self._scope_id, *args, **kwargs)
        value = await value if inspect.isawaitable(value) else value
        return self._bounded(value)

    def _bounded(self, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            result, used = [], 0
            for item in value[: self.max_limit]:
                size = len(str(item))
                if result and used + size > self.max_output_chars:
                    break
                result.append(item)
                used += size
            return result
        text = str(value)
        return value if len(text) <= self.max_output_chars else text[: self.max_output_chars]

    async def search(self, query: str, limit: int = 20, **filters: Any) -> Any:
        filters.pop("scope_id", None)
        return await self._call(
            self._search, str(query)[:1000], limit=self._limit(limit), **filters
        )

    async def recent(self, limit: int = 20) -> Any:
        return await self._call(self._recent, limit=self._limit(limit))

    async def get(self, message_ids: Any, limit: int = 8) -> Any:
        ids = [str(item)[:256] for item in list(message_ids)[: self.max_limit]]
        return await self._call(self._get, ids, limit=self._limit(limit))


class AgentToolService:
    """Framework-neutral async tools; it never reaches into AstrBot's StarManager."""

    def __init__(
        self,
        context: Any,
        dashboard: Any,
        search_service: Any = None,
        *,
        group_authorizer: Any = None,
        audit_callback: Any = None,
        memory_scope_resolver: Any = None,
        memory_callbacks: dict[str, Any] | None = None,
    ):
        self.context = context
        self.dashboard = dashboard
        self.search_service = search_service
        self.skills = SkillManager(dashboard)
        self.group_authorizer = group_authorizer
        self.audit_callback = audit_callback
        self.memory_scope_resolver = memory_scope_resolver
        self.memory_callbacks = memory_callbacks or {}

    @staticmethod
    def _require_event(event: Any) -> None:
        if event is None:
            raise PermissionError("A current event is required")

    @staticmethod
    def _event_fact(event: Any, *names: str) -> str:
        return _event_value(event, *names)

    async def _audit(self, record: dict[str, Any]) -> None:
        if not callable(self.audit_callback):
            return
        value = self.audit_callback(record)
        if inspect.isawaitable(value):
            await value

    async def _management_context(self, event: Any) -> tuple[str, str]:
        self._require_event(event)
        platform = self._event_fact(event, "get_platform_name", "platform_name")
        group_id = self._event_fact(event, "get_group_id", "group_id")
        if platform != "aiocqhttp":
            raise PermissionError("Management is restricted to aiocqhttp")
        if not group_id:
            raise PermissionError("Management requires a group event")
        if not callable(self.group_authorizer):
            raise PermissionError("Group authorizer is not configured")
        allowed = self.group_authorizer(group_id)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if not allowed:
            raise PermissionError("Group is not authorized for management")
        return platform, group_id

    async def runtime_manifest(self, event: Any, req: Any) -> dict[str, Any]:
        self._require_event(event)
        return await build_runtime_manifest(self.context, req, event)

    async def get_runtime_manifest(self, event: Any, req: Any) -> dict[str, Any]:
        return await self.runtime_manifest(event, req)

    async def search(self, event: Any, query: str, **kwargs: Any) -> Any:
        self._require_event(event)
        if self.search_service is None:
            raise RuntimeError("Search service is not configured")
        method = getattr(self.search_service, "search", self.search_service)
        if not callable(method):
            raise RuntimeError("Search service has no search method")
        value = method(query, **kwargs)
        return await value if inspect.isawaitable(value) else value

    async def search_history(self, event: Any, query: str, **kwargs: Any) -> Any:
        return await self.search(event, query, **kwargs)

    async def search_plugins(self, event: Any, query: str) -> Any:
        self._require_event(event)
        return await self.dashboard.search_market(query)

    async def memory_tools(self, event: Any) -> ScopeBoundMemoryTools:
        self._require_event(event)
        if not callable(self.memory_scope_resolver):
            raise RuntimeError("Memory scope resolver is not configured")
        scope_id = self.memory_scope_resolver(event)
        if inspect.isawaitable(scope_id):
            scope_id = await scope_id
        return ScopeBoundMemoryTools(
            str(scope_id or ""),
            search_callback=self.memory_callbacks.get("search"),
            recent_callback=self.memory_callbacks.get("recent"),
            get_callback=self.memory_callbacks.get("get"),
        )

    async def _authorization(
        self,
        event: Any,
        action: str,
        resource: str,
        target: str,
        payload: str | None = None,
        structured: bool = False,
    ) -> Authorization:
        authorization = (
            await authorize_structured_command(
                event,
                action=action,
                resource=resource,
                target=target,
                payload=payload,
            )
            if structured
            else await authorize_admin_instruction(event)
        )
        matches = (
            authorization.authorized
            and authorization.action == action
            and authorization.resource == resource
            and authorization.target == target
            and (payload is None or authorization.payload_digest == _digest(payload))
        )
        if not matches:
            if authorization.authorized:
                authorization = Authorization(
                    False, authorization.action, authorization.resource,
                    authorization.target, authorization.actor_id,
                    authorization.event_id, authorization.command_digest,
                    authorization.issued_at,
                    "instruction does not match the requested operation",
                    authorization.payload_digest,
                    authorization.source,
                )
            raise AuthorizationError(authorization)
        return authorization

    async def _manage(
        self,
        event: Any,
        action: str,
        resource: str,
        target: str,
        operation: Any,
        *,
        payload: str | None = None,
        structured: bool = False,
    ) -> dict[str, Any]:
        authorization: Authorization | None = None
        platform = ""
        group_id = ""
        try:
            self._require_event(event)
            platform = self._event_fact(event, "get_platform_name", "platform_name")
            group_id = self._event_fact(event, "get_group_id", "group_id")
            platform, group_id = await self._management_context(event)
            authorization = await self._authorization(
                event, action, resource, target, payload, structured
            )
            data = await operation()
        except Exception as exc:
            await self._audit(
                {
                    "outcome": "rejected" if isinstance(exc, (PermissionError, AuthorizationError)) else "failed",
                    "platform": platform,
                    "group_id": group_id,
                    "resource": resource,
                    "action": action,
                    "target": target,
                    "authorization": authorization.to_audit_dict() if authorization else None,
                    "error_type": type(exc).__name__,
                }
            )
            raise
        record = {
            "outcome": "success",
            "platform": platform,
            "group_id": group_id,
            "resource": resource,
            "action": action,
            "target": target,
            "authorization": authorization.to_audit_dict(),
        }
        await self._audit(record)
        return {"authorization": authorization.to_audit_dict(), "data": data}

    async def manage_plugin(
        self, event: Any, action: str, plugin_id: str, *, structured: bool = False
    ) -> dict[str, Any]:
        action = action.lower()
        methods = {
            "install": self.dashboard.install_market_plugin,
            "update": self.dashboard.update_plugin,
            "enable": self.dashboard.enable_plugin,
            "disable": self.dashboard.disable_plugin,
            "reload": self.dashboard.reload_plugin,
        }
        if action not in methods:
            raise ValueError("Unsupported plugin action")
        return await self._manage(
            event, action, "plugin", plugin_id,
            lambda: methods[action](plugin_id), structured=structured,
        )

    async def install_plugin(self, event: Any, plugin_id: str) -> dict[str, Any]:
        return await self.manage_plugin(event, "install", plugin_id)

    async def update_plugin(self, event: Any, plugin_id: str) -> dict[str, Any]:
        return await self.manage_plugin(event, "update", plugin_id)

    async def enable_plugin(self, event: Any, plugin_id: str) -> dict[str, Any]:
        return await self.manage_plugin(event, "enable", plugin_id)

    async def disable_plugin(self, event: Any, plugin_id: str) -> dict[str, Any]:
        return await self.manage_plugin(event, "disable", plugin_id)

    async def reload_plugin(self, event: Any, plugin_id: str) -> dict[str, Any]:
        return await self.manage_plugin(event, "reload", plugin_id)

    async def manage_skill(
        self,
        event: Any,
        action: str,
        skill_name: str,
        skill_md: str | None = None,
        *,
        structured: bool = False,
    ) -> dict[str, Any]:
        action = action.lower()
        if action == "create":
            if skill_md is None:
                raise ValueError("skill_md is required")
            operation = lambda: self.skills.create(skill_name, skill_md)
        elif action == "update":
            if skill_md is None:
                raise ValueError("skill_md is required")
            operation = lambda: self.skills.update(skill_name, skill_md)
        elif action == "enable":
            operation = lambda: self.skills.enable(skill_name)
        elif action == "disable":
            operation = lambda: self.skills.disable(skill_name)
        else:
            raise ValueError("Unsupported skill action")
        return await self._manage(
            event,
            action,
            "skill",
            skill_name,
            operation,
            payload=skill_md if action in {"create", "update"} else None,
            structured=structured,
        )

    async def manage_structured_plugin(
        self, event: Any, action: str, plugin_id: str
    ) -> dict[str, Any]:
        return await self.manage_plugin(event, action, plugin_id, structured=True)

    async def manage_structured_skill(
        self,
        event: Any,
        action: str,
        skill_name: str,
        skill_md: str | None = None,
    ) -> dict[str, Any]:
        return await self.manage_skill(
            event, action, skill_name, skill_md, structured=True
        )

    async def create_skill(self, event: Any, name: str, skill_md: str) -> dict[str, Any]:
        return await self.manage_skill(event, "create", name, skill_md)

    async def update_skill(self, event: Any, name: str, skill_md: str) -> dict[str, Any]:
        return await self.manage_skill(event, "update", name, skill_md)

    async def enable_skill(self, event: Any, name: str) -> dict[str, Any]:
        return await self.manage_skill(event, "enable", name)

    async def disable_skill(self, event: Any, name: str) -> dict[str, Any]:
        return await self.manage_skill(event, "disable", name)
