# -*- coding: utf-8 -*-
"""从 SnowLuma 文档站的 catalog.json 生成 `src/qq_actions.py`。

数据源：`.snowluma_docs/public/api/catalog.json`（由 `tools/catalog-to-openapi.mjs`
同源生成，含每个动作的入参/返回 schema 与中文说明）。

用法：
    python tools/gen_qq_actions.py [catalog.json 路径]

生成物是**自包含**的纯数据模块（不依赖文档目录），插件运行期只用生成物。
重跑即可在 SnowLuma 更新动作表后同步——这正是"完全按文档重写 QQ 工具层"的
可持续做法（对照脚本，不是手抄）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOGS = (
    ROOT / ".snowluma_docs" / "public" / "api" / "catalog.json",
    ROOT / ".snowluma_docs" / "docs" / "public" / "api" / "catalog.json",
)

# 凭证类动作：只给插件内部用（HTTP 回退取 cookies/bkn 等），绝不暴露给 LLM
CREDENTIAL_ACTIONS = {
    "get_cookies", "get_credentials", "get_csrf_token", "get_clientkey",
    "get_rkey", "get_rkey_server", "request_decrypt_key", "send_packet",
}

# 破坏性/管理类：小时配额最低，且提示词要求先确认
DESTRUCTIVE_KEYWORDS = (
    "delete", "del_", "kick", "ban", "exit", "leave", "dismiss", "withdraw",
    "remove", "clear", "clean",
)

# 社交动作（点赞/评论/打卡/好友申请/空间）：单独配额
SOCIAL_KEYWORDS = (
    "like", "praise", "comment", "qzone", "sign", "poke", "feed", "album",
    "request", "essence", "todo", "notice", "face",
)


def risk_of(action: dict) -> str:
    name = str(action.get("name", ""))
    lowered = name.lower()
    if lowered in CREDENTIAL_ACTIONS or name in CREDENTIAL_ACTIONS:
        return "credential"
    if action.get("readOnly"):
        return "read"
    if any(token in lowered for token in DESTRUCTIVE_KEYWORDS):
        return "destructive"
    if any(token in lowered for token in SOCIAL_KEYWORDS):
        return "social"
    return "send"


def py_literal(value) -> str:
    return repr(value)


def render(catalog: dict) -> str:
    actions = catalog["actions"]
    lines: list[str] = [
        '# -*- coding: utf-8 -*-',
        '"""SnowLuma / OneBot v11 全量动作表（由 tools/gen_qq_actions.py 生成，勿手改）。',
        '',
        f'来源：SnowLuma 文档站 catalog.json，共 {len(actions)} 个动作。',
        '每个动作带：分类、中文说明、只读标记、风险级别、参数表（含类型/必填/默认/说明）。',
        '"""',
        'from __future__ import annotations',
        '',
        'from dataclasses import dataclass, field',
        'from typing import Any, Iterable, Mapping',
        '',
        '',
        '@dataclass(frozen=True, slots=True)',
        'class QQParam:',
        '    """一个动作参数（类型用于入参强制转换，role 用于网关侧的安全归类）。"""',
        '',
        '    name: str',
        '    type: str',
        '    required: bool = False',
        '    desc: str = ""',
        '    default: Any = None',
        '    role: str = ""',
        '',
        '',
        '@dataclass(frozen=True, slots=True)',
        'class QQAction:',
        '    """一个 OneBot/SnowLuma 动作的完整描述。"""',
        '',
        '    name: str',
        '    summary: str',
        '    category: str',
        '    read_only: bool',
        '    risk: str',
        '    params: tuple[QQParam, ...] = ()',
        '    aliases: tuple[str, ...] = ()',
        '    returns: str = ""',
        '    invariants: tuple[str, ...] = ()',
        '    accepts_extra: bool = True',
        '',
        '    @property',
        '    def required(self) -> tuple[str, ...]:',
        '        return tuple(p.name for p in self.params if p.required)',
        '',
        '    def param(self, name: str) -> QQParam | None:',
        '        for item in self.params:',
        '            if item.name == name:',
        '                return item',
        '        return None',
        '',
        '',
        'CATEGORIES: tuple[str, ...] = (',
    ]
    categories = catalog.get("categories") or []
    order = [c["category"] for c in categories]
    for name in order:
        lines.append(f'    {py_literal(name)},')
    lines += [
        ')',
        '',
        'ACTIONS: dict[str, QQAction] = {}',
        '',
        '',
        'def _register(action: QQAction) -> None:',
        '    ACTIONS[action.name] = action',
        '',
        '',
    ]
    for action in sorted(actions, key=lambda item: (item.get("category", ""), item["name"])):
        params = action.get("params") or []
        lines.append('_register(QQAction(')
        lines.append(f'    name={py_literal(action["name"])},')
        lines.append(f'    summary={py_literal(str(action.get("summary") or "")[:200])},')
        lines.append(f'    category={py_literal(action.get("category") or "扩展")},')
        lines.append(f'    read_only={bool(action.get("readOnly"))},')
        lines.append(f'    risk={py_literal(risk_of(action))},')
        if params:
            lines.append('    params=(')
            for param in params:
                default = param.get("default")
                if default is None:
                    default = param.get("schema", {}).get("default")
                lines.append(
                    '        QQParam('
                    f'name={py_literal(str(param.get("name") or ""))}, '
                    f'type={py_literal(str(param.get("type") or "string"))}, '
                    f'required={bool(param.get("required"))}, '
                    f'desc={py_literal(str(param.get("desc") or "")[:160])}, '
                    f'default={py_literal(default)}, '
                    f'role={py_literal(str(param.get("role") or ""))}),')
            lines.append('    ),')
        aliases = action.get("aliases") or []
        if aliases:
            lines.append('    aliases=(' + ", ".join(py_literal(a) for a in aliases) + ',),')
        returns = str(action.get("returns") or "")[:400]
        if returns:
            lines.append(f'    returns={py_literal(returns)},')
        invariants = action.get("invariants") or []
        if invariants:
            lines.append('    invariants=(' + ", ".join(
                py_literal(str(x)[:160]) for x in invariants) + ',),')
        if action.get("inputSchema", {}).get("additionalProperties", True) is False:
            lines.append('    accepts_extra=False,')
        lines.append('))')
    lines += [
        '',
        'ACTION_ALIASES: dict[str, str] = {}',
        'for _action in ACTIONS.values():',
        '    for _alias in _action.aliases:',
        '        ACTION_ALIASES.setdefault(_alias, _action.name)',
        '',
        '',
        'def resolve(name: str) -> QQAction | None:',
        '    """按动作名（含别名）取描述；未知动作返回 None。"""',
        '    key = str(name or "").strip()',
        '    if not key:',
        '        return None',
        '    if key in ACTIONS:',
        '        return ACTIONS[key]',
        '    canonical = ACTION_ALIASES.get(key)',
        '    return ACTIONS.get(canonical) if canonical else None',
        '',
        '',
        '# 类型转换表：把 LLM 送来的松散值转成动作期望的类型（尽力而为，转不了就原样）',
        '_INT_TYPES = {"uint", "int", "messageId", "number"}',
        '_BOOL_TYPES = {"bool"}',
        '',
        '',
        'def coerce(value: Any, type_name: str) -> Any:',
        '    """按动作参数类型做一次宽松转换——LLM 常把数字/布尔写成字符串。"""',
        '    if not isinstance(value, str):',
        '        if type_name in _INT_TYPES and isinstance(value, bool):',
        '            return int(value)',
        '        return value',
        '    text = value.strip()',
        '    if not text:',
        '        return value',
        '    if type_name in _INT_TYPES:',
        '        try:',
        '            return int(text)',
        '        except ValueError:',
        '            return value',
        '    if type_name in _BOOL_TYPES:',
        '        lowered = text.lower()',
        '        if lowered in {"true", "1", "yes", "y", "on"}:',
        '            return True',
        '        if lowered in {"false", "0", "no", "n", "off"}:',
        '            return False',
        '    if type_name.endswith("[]") and "," in text:',
        '        inner = type_name[:-2]',
        '        return [coerce(part, inner) for part in text.split(",") if part.strip()]',
        '    return value',
        '',
        '',
        'def normalize_params(action: QQAction, params: Mapping[str, Any],',
        '                     *, strip_unknown: bool = True) -> tuple[dict[str, Any], list[str]]:',
        '    """校验并归一化入参：返回 (clean, missing)。',
        '',
        '    - 已知参数：按声明类型做宽松转换；',
        '    - 未声明参数：动作 `accepts_extra` 为真时原样保留（SnowLuma 允许透传），',
        '      否则丢弃（strip_unknown=False 时保留，供内部调用方用）；',
        '    - `missing`：必填缺失列表（调用方决定是报错还是提醒）。',
        '    """',
        '    clean: dict[str, Any] = {}',
        '    missing: list[str] = []',
        '    known = {p.name: p for p in action.params}',
        '    for name, spec in known.items():',
        '        if name in params and params[name] is not None:',
        '            clean[name] = coerce(params[name], spec.type)',
        '        elif spec.required:',
        '            missing.append(name)',
        '    for name, value in (params or {}).items():',
        '        if name in known or value is None:',
        '            continue',
        '        if action.accepts_extra or not strip_unknown:',
        '            clean[name] = value',
        '    return clean, missing',
        '',
        '',
        'def describe(action: QQAction, *, with_params: bool = True) -> str:',
        '    """一行动作说明（catalog 展示用）。"""',
        '    bits = [f"{action.name} [{action.category}]"]',
        '    if action.read_only:',
        '        bits.append("(只读)")',
        '    if action.summary:',
        '        bits.append(action.summary)',
        '    if with_params and action.params:',
        '        parts = []',
        '        for p in action.params:',
        '            mark = "必填" if p.required else "可选"',
        '            extra = f" 默认{p.default}" if p.default is not None else ""',
        '            desc = f" {p.desc}" if p.desc else ""',
        '            parts.append(f"{p.name}({p.type},{mark}{extra}){desc}")',
        '        bits.append("参数：" + "；".join(parts))',
        '    if action.returns:',
        '        bits.append("返回：" + action.returns)',
        '    return " | ".join(bits)',
        '',
        '',
        'def names_in(category: str) -> list[str]:',
        '    return [a.name for a in ACTIONS.values() if a.category == category]',
        '',
        '',
        'def stats() -> dict[str, int]:',
        '    out: dict[str, int] = {}',
        '    for action in ACTIONS.values():',
        '        out[action.category] = out.get(action.category, 0) + 1',
        '    return out',
        '',
    ]
    return "\n".join(lines)


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if target is None:
        for candidate in DEFAULT_CATALOGS:
            if candidate.is_file():
                target = candidate
                break
    if target is None or not target.is_file():
        print("找不到 catalog.json（用参数指定路径）")
        return 2
    catalog = json.loads(target.read_text(encoding="utf-8"))
    output = ROOT / "src" / "qq_actions.py"
    output.write_text(render(catalog), encoding="utf-8")
    actions = catalog["actions"]
    by_risk: dict[str, int] = {}
    for action in actions:
        by_risk[risk_of(action)] = by_risk.get(risk_of(action), 0) + 1
    print(f"写入 {output}：{len(actions)} 个动作 | 风险分布 {by_risk}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
