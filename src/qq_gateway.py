# -*- coding: utf-8 -*-
"""QQ 动作网关（完全重写版，取代旧的 napcat_gateway / napcat_actions / snowluma 三件套）。

设计（对照 SnowLuma 文档重写）：
- **动作表来自 `src/qq_actions.py`**（由 `tools/gen_qq_actions.py` 从文档站 catalog
  生成，193 个动作全覆盖）——不再手抄动作目录，文档更新即重跑生成器；
- **一个动作一个 spec**：分类、中文说明、只读标记、风险级别、参数表（类型/必填/
  默认/说明）。入参做宽松类型转换（LLM 常把数字/布尔写成字符串）；
- **必填校验按 schema 走**：缺必填就明确报缺哪个（旧实现里参数名与校验表不一致，
  导致 `get_forward_msg` 传 `id` 直接被拒——"合并转发点不开"的真凶之一）；
- **返回值形状统一解包**：aiocqhttp/SnowLuma 把 OneBot 信封的 `data` 直接返回，
  这里统一成 `{"ok":bool,"data":...}` 风格的辅助函数供上层取字段；
- **凭证类动作永不外露**（cookies/credentials/rkey/解密密钥/send_packet），只允许
  插件内部调用；
- 风险分级配额 + 每动作最小间隔，防失控刷屏。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .qq_actions import (
    ACTIONS,
    CATEGORIES,
    ACTION_ALIASES,
    QQAction,
    describe,
    normalize_params,
    resolve,
    stats,
)

RISK_READ = "read"
RISK_SEND = "send"
RISK_SOCIAL = "social"
RISK_MODERATION = "destructive"
RISK_CREDENTIAL = "credential"

# 每风险级别的默认小时配额（可被插件配置覆盖）
DEFAULT_HOURLY = {
    RISK_READ: 600,
    RISK_SEND: 60,
    RISK_SOCIAL: 30,
    RISK_MODERATION: 12,
    RISK_CREDENTIAL: 30,
}
# 每个动作的最小调用间隔（秒）：按风险级别给默认，个别动作单独收紧
MIN_INTERVAL = {
    RISK_READ: 0.2,
    RISK_SEND: 1.5,
    RISK_SOCIAL: 2.0,
    RISK_MODERATION: 2.0,
    RISK_CREDENTIAL: 0.5,
}
SLOW_ACTIONS = {"send_group_msg", "send_private_msg", "send_msg",
                "send_forward_msg", "send_group_forward_msg",
                "send_private_forward_msg", "set_qq_avatar",
                "set_self_longnick", "set_online_status", "upload_group_file"}


class QQGatewayError(RuntimeError):
    """网关调用失败（动作未注册 / 参数缺失 / 上游报错 / 配额耗尽）。"""

    def __init__(self, action: str, message: str, response: Any = None) -> None:
        super().__init__(f"{action}: {message}")
        self.action = action
        self.message = message
        self.response = response


@dataclass
class QQGateway:
    """所有 OneBot/NapCat/SnowLuma 动作的唯一出口。"""

    bot: Any = None
    hourly: Mapping[str, int] = field(default_factory=dict)
    audit_callback: Callable[[dict[str, Any]], Any] | None = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        self._last_call: dict[str, float] = {}
        self._buckets: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()
        self.hourly = dict(DEFAULT_HOURLY) | dict(self.hourly or {})

    # ------------------------------------------------------------ 目录
    @staticmethod
    def spec(action: str) -> QQAction | None:
        return resolve(action)

    @staticmethod
    def catalog(category: str = "") -> list[dict[str, Any]]:
        """给 LLM 看的动作目录（凭证类剔除，可按分类过滤）。"""
        out: list[dict[str, Any]] = []
        wanted = str(category or "").strip()
        for action in ACTIONS.values():
            if action.risk == RISK_CREDENTIAL:
                continue
            if wanted and action.category != wanted:
                continue
            out.append({
                "action": action.name,
                "category": action.category,
                "read_only": action.read_only,
                "summary": action.summary,
                "params": [
                    {"name": p.name, "type": p.type, "required": p.required,
                     "desc": p.desc, **({"default": p.default} if p.default is not None else {})}
                    for p in action.params
                ],
                **({"aliases": list(action.aliases)} if action.aliases else {}),
            })
        return out

    @staticmethod
    def categories() -> list[dict[str, Any]]:
        counts = stats()
        return [{"category": name, "count": counts.get(name, 0)}
                for name in CATEGORIES if counts.get(name)]

    def resolve_action(self, action: str) -> QQAction:
        spec = resolve(action)
        if spec is None:
            close = [name for name in ACTIONS if str(action or "")[:4] and
                     str(action or "").lower()[:4] in name.lower()][:6]
            hint = f"；相近动作：{', '.join(close)}" if close else ""
            raise QQGatewayError(str(action or ""), f"未注册的动作名{hint}")
        return spec

    # ------------------------------------------------------------ 执行
    async def execute(self, action: str, **params: Any) -> Any:
        spec = self.resolve_action(action)
        clean, missing = normalize_params(spec, params)
        if missing:
            raise QQGatewayError(
                spec.name,
                f"缺少必填参数 {missing}；该动作参数："
                + "、".join(f"{p.name}({p.type}{'，必填' if p.required else ''})"
                            for p in spec.params),
            )
        interval = MIN_INTERVAL.get(spec.risk, 2.0)
        if spec.name in SLOW_ACTIONS:
            interval = max(interval, 2.0)
        async with self._lock:
            now = time.monotonic()
            last = self._last_call.get(spec.name, float("-inf"))
            if now - last < interval:
                await asyncio.sleep(interval - (now - last))
            bucket = self._buckets.setdefault(spec.risk, deque())
            cutoff = time.monotonic() - 3600
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            limit = int(self.hourly.get(spec.risk, DEFAULT_HOURLY.get(spec.risk, 60)))
            if len(bucket) >= limit:
                raise QQGatewayError(
                    spec.name, f"{spec.risk} 类动作每小时上限 {limit} 已用尽")
            self._last_call[spec.name] = time.monotonic()
            bucket.append(time.monotonic())
        result = await self._call(spec.name, **clean)
        if self.audit_callback:
            value = self.audit_callback(
                {"action": spec.name, "risk": spec.risk, "category": spec.category,
                 "ok": True})
            if inspect.isawaitable(value):
                await value
        return result

    async def execute_internal(self, action: str, **params: Any) -> Any:
        """内部通道：允许凭证类动作（取 cookies/rkey 等），不经 LLM 门。"""
        spec = self.resolve_action(action)
        clean, missing = normalize_params(spec, params, strip_unknown=False)
        if missing:
            raise QQGatewayError(spec.name, f"缺少必填参数 {missing}")
        return await self._call(spec.name, **clean)

    async def _call(self, action: str, **params: Any) -> Any:
        from .onebot import _resolve_caller

        if self.bot is None:
            raise QQGatewayError(action, "网关未绑定客户端（无可用 OneBot 连接）")
        resolved = _resolve_caller(self.bot, action)
        if resolved is None:
            raise QQGatewayError(
                action, f"没有可用的 OneBot 传输层（{type(self.bot).__name__}）")
        caller, takes_action = resolved
        try:
            response = caller(**params) if not takes_action else caller(action, **params)
        except TypeError as exc:
            if not takes_action or "positional" not in str(exc):
                raise QQGatewayError(action, f"参数不被接受：{str(exc)[:160]}") from exc
            response = caller(**params)
        if inspect.isawaitable(response):
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await response
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise QQGatewayError(action, "调用超时") from None
        if isinstance(response, dict) and "retcode" in response:
            retcode = response.get("retcode")
            if response.get("status") in {"failed", "error"} or (
                isinstance(retcode, int) and retcode not in (0, 1)
            ):
                raise QQGatewayError(
                    action,
                    f"上游返回失败 retcode={retcode} "
                    f"{str(response.get('message') or response.get('wording') or '')[:120]}",
                    response)
        return response

    async def run_sequence(
        self,
        actions: Sequence[tuple[str, dict[str, Any]]],
        *,
        between_delay: float = 1.5,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> list[Any]:
        results: list[Any] = []
        for index, (action, params) in enumerate(actions):
            if is_cancelled and is_cancelled():
                break
            if index and between_delay > 0:
                await asyncio.sleep(between_delay)
            results.append(await self.execute(action, **(params or {})))
        return results


def payload(result: Any) -> dict[str, Any]:
    """把各种返回形状归一成"能取字段的 dict"。

    实测：aiocqhttp/SnowLuma 的 `call_action` 直接返回 OneBot 信封里的 `data`
    （例如 `{"messages":[...]}`、`{"message_id":123}`）；但也有实现/旧版本返回
    整个信封（`{"status":"ok","retcode":0,"data":{...}}`）。这里两者都吃。
    """
    if isinstance(result, dict):
        inner = result.get("data")
        if isinstance(inner, dict) and not set(result).intersection(
                {"messages", "message_id", "res_id", "forward_id"}):
            return inner
        return result
    return {}


def messages_of(result: Any) -> list[dict[str, Any]]:
    """从 get_forward_msg 的返回里取节点数组（兼容信封/解包两种形状）。"""
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    data = payload(result)
    nodes = data.get("messages") or data.get("message")
    if isinstance(nodes, list):
        return [item for item in nodes if isinstance(item, dict)]
    return []


class GatewayFacade:
    """LLM 面：非凭证动作 + JSON 安全输出（写动作仍走同一个配额与审计）。"""

    def __init__(self, gateway: QQGateway) -> None:
        self.gateway = gateway

    async def call(self, action: str, **params: Any) -> Any:
        spec = self.gateway.resolve_action(action)
        if spec.risk == RISK_CREDENTIAL:
            raise QQGatewayError(spec.name, "凭证类动作仅供插件内部使用")
        result = await self.gateway.execute(action, **params)
        return _json_safe(result)

    def catalog(self, category: str = "") -> list[dict[str, Any]]:
        return self.gateway.catalog(category)

    def categories(self) -> list[dict[str, Any]]:
        return self.gateway.categories()

    def describe(self, action: str) -> str:
        spec = self.gateway.resolve_action(action)
        return describe(spec)

    async def execute(self, action: str, **params: Any) -> Any:
        """与 gateway.execute 同名同义（旧代码里 facade.execute 被直接调用）。"""
        return await self.call(action, **params)


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item, depth + 1)
                for key, item in list(value.items())[:60]}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in list(value)[:60]]
    return str(value)[:500]


# 兼容别名：旧代码里用的是 NapCatGateway/NapCatGatewayError 这两个名字
NapCatGateway = QQGateway
NapCatGatewayError = QQGatewayError
