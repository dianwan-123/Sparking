# -*- coding: utf-8 -*-
"""WebUI 数据面与动作面（完全重写版）。

用户诉求（实录）：①所有删除功能点了没反应；②界面有错别字（"LLLM"）；③很卡。
根因（本地实测）：删除入口只认**内部编号**，前端拿到的是 QQ 消息号 → 静默删 0 条；
`storage.stats()` 的键是 `message_identities` 而前端读 `messages` → 计数全空；
前端一次性拉全量数据、无分页。

本模块的设计约定（前端 `pages/memory/app.js` 与之严格对应）：
- **读**：`read(route, query) -> dict`；**写**：`write(action, body) -> dict`，
  统一 `{"ok": bool, "data": ..., "message": str}` 信封（由 main 包装成 json_response）。
- **删除**：一律接受「内部编号 / QQ 上游 id」两种 id（`storage.resolve_reply_target`
  之外再补 `find_message_any`），返回 `removed` 供前端提示与刷新。
- **分片**：每个面板只取自己的数据；列表统一带 `limit`，消息带游标分页。
- **能力总表**：AstrBot 内置工具 + 其它插件工具 + 本插件工具按来源列出，
  「bot 能用什么」在界面可见（对应"bot 必须能自由调用 astrbot / 插件工具"）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

# 路由表：(路径, 方法组, 说明)。方法组 "GET" 走 read()，"POST" 走 write()。
READ_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("overview", "概览"),
    ("scopes", "群与会话"),
    ("messages", "消息浏览"),
    ("summaries", "分层摘要"),
    ("catalog", "记忆账本"),
    ("impressions", "人物印象"),
    ("affinity", "好感度"),
    ("styles", "说话风格"),
    ("topics", "群话题"),
    ("events", "通知与请求"),
    ("plans", "自主日程"),
    ("todos", "任务表"),
    ("scheduled", "定时任务"),
    ("intents", "事件条件指令"),
    ("usage", "用量统计"),
    ("media", "媒体归档"),
    ("stickers", "表情包库"),
    ("extensions", "拓展与技能"),
    ("capabilities", "能力总表"),
    ("config", "配置"),
    ("personas", "可用人格"),
    ("backups", "备份"),
    ("memory_tree", "记忆森林"),
    ("tasks", "任务队列"),
    ("injections", "提示词注入"),
    ("culture", "群风格与心理"),
    ("imports", "快速学习"),
    ("backup_memory", "记忆导出与导入"),
)

WRITE_ACTIONS: tuple[tuple[str, str], ...] = (
    ("config_set", "改单项配置"),
    ("whitelist_set", "改白名单"),
    ("message_delete", "删单条消息"),
    ("user_delete", "删某人在本群的消息"),
    ("group_wipe", "清空某群记忆"),
    ("wipe_all", "清空全部记忆"),
    ("media_clear", "清空媒体归档"),
    ("sticker_clear", "清空表情包"),
    ("impression_save", "写人物印象"),
    ("impression_delete", "删人物印象"),
    ("affinity_adjust", "调好感度"),
    ("import_chatlog_upload", "上传聊天记录分片"),
    ("import_chatlog_finish", "解析导入聊天记录"),
    ("import_chatlog_abort", "取消导入"),
    ("memory_import_upload", "上传记忆包分片"),
    ("memory_import_finish", "导入记忆包"),
    ("memory_export_save", "导出一份记忆包到服务器"),
    ("style_rule_delete", "删一条群说话风格"),
    ("lexicon_save", "改/加一条群内词条"),
    ("lexicon_delete", "删一条群内词条"),
    ("profile_delete", "删一份人物档案"),
    ("mood_event_delete", "删一条情绪记忆"),
    ("injection_save", "存提示词注入（新增/改内容）"),
    ("injection_toggle", "开关提示词注入"),
    ("injection_delete", "删除自定义注入"),
    ("mood_set", "设置情绪"),
    ("plan_add", "加日程"),
    ("plan_drop", "撤日程"),
    ("todo_add", "加任务"),
    ("todo_update", "改任务"),
    ("todo_clear", "清任务"),
    ("intent_add", "立事件条件指令"),
    ("intent_remove", "撤事件条件指令"),
    ("extension_toggle", "开关拓展"),
    ("extension_reload", "重载拓展"),
    ("extension_config_save", "保存拓展配置"),
    ("extension_config_reset", "恢复拓展默认配置"),
    ("compress", "压缩记忆"),
    ("reflect", "让 bot 自我总结"),
    ("backup", "备份"),
    ("restore", "从备份恢复"),
    ("order", "给 bot 下令"),
    ("ask", "问 bot 一句"),
    ("say", "以 bot 身份发言"),
    ("memory_node_save", "新增/编辑记忆节点"),
    ("memory_node_delete", "删除记忆节点"),
    ("task_add", "新增任务"),
    ("task_update", "修改任务"),
    ("task_cancel", "撤销任务"),
)


# 敏感配置：读接口一律脱敏（页面不回显口令；写入仍需填新值才生效）
_SECRET_HINTS = ("password", "passwd", "secret", "api_key", "apikey", "token",
                 "cookie", "credential")


def _is_secret_key(key: str) -> bool:
    lowered = str(key or "").lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _unlink_media_files(result: Any) -> int:
    """删除存储记录时把落盘的媒体文件一起清掉（旧实现删行不删文件）。"""
    removed = 0
    for raw in (getattr(result, "media_paths", ()) or ()):
        try:
            path = Path(str(raw))
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


class WebUIError(RuntimeError):
    """动作失败（前端据此弹提示，不吞错）。"""


def _iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


class PageAPI:
    """所有 WebUI 读写的唯一入口；host 是插件实例（弱耦合，只取用到的属性）。"""

    def __init__(self, host: Any) -> None:
        self.host = host

    # ------------------------------------------------------------ 基础
    @property
    def storage(self):
        storage = getattr(self.host, "storage", None)
        if storage is None:
            raise WebUIError("存储未就绪")
        return storage

    def _known_scopes(self) -> dict[str, str]:
        return dict(getattr(self.host, "_known_scopes", {}) or {})

    def _scope_or_raise(self, group_id: str) -> str:
        scope = self._known_scopes().get(str(group_id or "").strip())
        if not scope:
            raise WebUIError(f"未知的群/会话：{group_id!r}（先在群里发一条消息让它入库）")
        return scope

    def _scope_of(self, body: Mapping[str, Any]) -> tuple[str, str]:
        group = str(body.get("group_id") or body.get("scope") or "").strip()
        known = self._known_scopes()
        if group and group in known:
            return group, known[group]
        if group:  # 也可能是直接给了 scope_id
            for conv, scope in known.items():
                if scope == group:
                    return conv, scope
            raise WebUIError(f"未知的群/会话：{group!r}")
        if not known:
            raise WebUIError("还没有任何会话数据")
        conv = next(iter(known))
        return conv, known[conv]

    # ------------------------------------------------------------ 读
    async def read(self, route: str, query: Mapping[str, Any]) -> Any:
        handler = getattr(self, f"_read_{route}", None)
        if handler is None:
            raise WebUIError(f"未知的数据面：{route}")
        return await handler(query)

    async def _read_overview(self, query: Mapping[str, Any]) -> dict[str, Any]:
        settings = getattr(self.host, "settings", None)
        storage = self.storage
        stats = await storage.stats(None)
        scopes = await storage.all_scopes("aiocqhttp")
        usage = await storage.usage_summary(7)
        mood = getattr(getattr(self.host, "mood", None), "get", lambda: None)()
        plans = await storage.pending_plans(10)
        scripts = None
        manager = getattr(self.host, "_scripts", None)
        if manager is not None:
            try:
                scripts = {
                    "custom": len([e for e in manager.extensions.values()
                                   if getattr(e, "origin", "") != "learned"]),
                    "learned": len([e for e in manager.extensions.values()
                                    if getattr(e, "origin", "") == "learned"]),
                }
            except Exception:
                scripts = None
        return {
            "version": getattr(self.host, "_version", "") or "",
            "enabled": bool(getattr(settings, "enabled", False)),
            "groups": sorted(str(x) for x in (getattr(settings, "group_whitelist", ()) or ()))[:200],
            "counts": {
                "scopes": stats.get("scopes", 0),
                "messages": stats.get("message_identities", 0),
                "summaries": stats.get("summary_nodes", 0),
                "memories": stats.get("memory_items", 0),
                "catalog": stats.get("catalog_entries", 0),
                "jobs": stats.get("jobs", 0),
            },
            "models": {
                "judge": getattr(settings, "judge_provider_id", "") or "（跟随当前模型）",
                "reply": getattr(settings, "reply_provider_id", "") or "（跟随当前模型）",
                "summary": getattr(settings, "summary_provider_id", "") or "（跟随回复模型）",
                "persona": getattr(settings, "persona_id", "") or "（插件内置风格）",
            },
            "mood": mood.as_dict() if mood is not None else None,
            "usage": usage,
            "plans": plans[:5],
            "scripts": scripts,
            "capability_enabled": bool(getattr(settings, "enable_qq_tools", False)),
            "browser_env": dict(getattr(self.host, "_browser_env", {}) or {}),
        }

    async def _read_scopes(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows = await self.storage.all_scopes("aiocqhttp")
        out: list[dict[str, Any]] = []
        for row in rows:
            conversation = str(row.get("conversation_id") or "")
            scope = str(row.get("scope_id") or "")
            if not conversation or not scope:
                continue
            stats = await self.storage.stats(scope)
            tail = await self.storage.recent_messages(scope, 1)
            out.append({
                "group_id": conversation,
                "scope_id": scope,
                "display_name": str(row.get("display_name") or ""),
                "messages": stats.get("message_identities", 0),
                "summaries": stats.get("summary_nodes", 0),
                "memories": stats.get("memory_items", 0),
                "last_at": str(tail[0].occurred_at)[:19] if tail else "",
            })
        out.sort(key=lambda item: item["last_at"], reverse=True)
        return out

    async def _read_messages(self, query: Mapping[str, Any]) -> dict[str, Any]:
        group = str(query.get("group_id") or "").strip()
        scope = self._scope_or_raise(group) if group else (
            next(iter(self._known_scopes().values()), None))
        if not scope:
            return {"items": [], "total": 0}
        limit = min(max(int(query.get("limit", 40) or 40), 1), 200)
        before = query.get("before_seq")
        before_seq = int(before) if str(before or "").strip().isdigit() else None
        keyword = str(query.get("q") or "").strip()
        include_events = bool(query.get("events"))
        rows = await self.storage.recent_messages(
            scope, limit, before_seq, include_events=include_events)
        items = [self._message_view(row) for row in rows]
        if keyword:
            items = [item for item in items if keyword in item["text"]]
        stats = await self.storage.stats(scope)
        return {"items": items, "total": stats.get("message_identities", 0)}

    @staticmethod
    def _message_view(row: Any) -> dict[str, Any]:
        return {
            "message_id": str(getattr(row, "message_id", "")),
            "qq_id": str(getattr(row, "upstream_message_id", "")),
            "seq": int(getattr(row, "scope_seq", 0) or 0),
            "sender_id": str(getattr(row, "sender_id", "")),
            "sender_name": str(getattr(row, "sender_name", "")),
            "occurred_at": str(getattr(row, "occurred_at", ""))[:19],
            "text": str(getattr(row, "text", "") or ""),
        }

    async def _read_summaries(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        group = str(query.get("group_id") or "").strip()
        scope = self._scope_or_raise(group) if group else (
            next(iter(self._known_scopes().values()), None))
        if not scope:
            return []
        limit = min(max(int(query.get("limit", 20) or 20), 1), 100)
        rows = await self.storage.list_summaries(scope, limit)
        out: list[dict[str, Any]] = []
        for row in rows:
            body = getattr(row, "body", "") or ""
            out.append({
                "summary_id": str(getattr(row, "summary_id", "")),
                "level": int(getattr(row, "level", 0) or 0),
                "title": str(getattr(row, "title", "")),
                "range": [int(getattr(row, "start_seq", 0) or 0),
                          int(getattr(row, "end_seq", 0) or 0)],
                "topics": list(getattr(row, "topics", ()) or ()),
                "body": body[:1200],
                "at": str(getattr(row, "created_at", ""))[:19],
            })
        return out

    async def _read_catalog(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        ledger = getattr(self.host, "ledger", None)
        if ledger is None:
            return []
        group = str(query.get("group_id") or "").strip()
        scope_ids: Sequence[str] = ()
        if group:
            scope_ids = (self._scope_or_raise(group),)
        limit = min(max(int(query.get("limit", 40) or 40), 1), 200)
        entries = await ledger.catalog(scope_ids, limit)
        out: list[dict[str, Any]] = []
        for item in entries:
            # 注意：CatalogEntry 是 dataclass(slots=True)——**没有 __dict__**，
            # 用 getattr(item,"__dict__",{}) 会拿到 {}（实录：账本全是"记忆 — —"）
            pick = (lambda name, default="": item.get(name, default)
                    if isinstance(item, Mapping) else getattr(item, name, default))
            out.append({
                "entry_id": str(pick("entry_id")),
                "memory_id": str(pick("memory_id")),
                "kind": str(pick("kind")),
                "subject": str(pick("subject")),
                "value": str(pick("value"))[:1200],
                "status": str(pick("status")),
                "confidence": pick("confidence", 0),
                "evidence": list(pick("evidence_ids", []) or []),
                "updated_at": str(pick("updated_at"))[:19],
            })
        return out

    async def _read_impressions(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        group = str(query.get("group_id") or "").strip()
        scope = self._scope_or_raise(group) if group else (
            next(iter(self._known_scopes().values()), None))
        if not scope:
            return []
        limit = min(max(int(query.get("limit", 40) or 40), 1), 200)
        # 印象跨群互通：读的时候合并同一个人的多条（界面上一人一行），
        # 并且带上 groups 说明这人出现在哪些群
        all_scopes = tuple(dict.fromkeys(self._known_scopes().values()))
        rows = await self.storage.list_impressions(
            all_scopes or (scope,), limit, query=str(query.get("q") or "") or None)
        return [dict(row) if isinstance(row, Mapping) else row for row in rows]

    async def _read_affinity(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        group = str(query.get("group_id") or "").strip()
        scope = self._scope_or_raise(group) if group else (
            next(iter(self._known_scopes().values()), None))
        if not scope:
            return []
        limit = min(max(int(query.get("limit", 50) or 50), 1), 200)
        rows = await self.storage._fetchall(
            "SELECT user_id,warmth,note,interactions,updated_at FROM user_affinity "
            "WHERE scope_id=? ORDER BY warmth DESC LIMIT ?", (scope, limit))
        return [dict(row) for row in rows]

    async def _read_styles(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        group = str(query.get("group_id") or "").strip()
        scope = self._scope_or_raise(group) if group else (
            next(iter(self._known_scopes().values()), None))
        if not scope:
            return []
        limit = min(max(int(query.get("limit", 40) or 40), 1), 200)
        return list(await self.storage.list_styles((scope,), limit))

    async def _read_topics(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        group = str(query.get("group_id") or "").strip()
        scope = self._scope_or_raise(group) if group else (
            next(iter(self._known_scopes().values()), None))
        if not scope:
            return []
        limit = min(max(int(query.get("limit", 30) or 30), 1), 200)
        return list(await self.storage.recent_topics(scope, limit))

    async def _read_events(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        group = str(query.get("group_id") or "").strip()
        scope = self._scope_or_raise(group) if group else (
            next(iter(self._known_scopes().values()), None))
        if not scope:
            return []
        limit = min(max(int(query.get("limit", 40) or 40), 1), 200)
        rows = await self.storage.recent_events((scope,), limit)
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append({
                "message_id": str(getattr(row, "message_id", "")),
                "qq_id": str(getattr(row, "upstream_message_id", "")),
                "sender_name": str(getattr(row, "sender_name", "")),
                "text": str(getattr(row, "text", ""))[:400],
                "at": str(getattr(row, "occurred_at", ""))[:19],
            })
        return out

    async def _read_plans(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        limit = min(max(int(query.get("limit", 30) or 30), 1), 200)
        return list(await self.storage.pending_plans(limit))

    async def _read_backup_memory(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """记忆导出/导入面板：能导出什么、之前导出过哪些包。"""
        directory = self.host._exports_dir()
        files = []
        try:
            for item in sorted(directory.iterdir(), key=lambda f: f.name, reverse=True)[:20]:
                if item.is_file() and item.suffix == ".zip":
                    files.append({"name": item.name, "size": item.stat().st_size})
        except Exception:
            pass
        stats = await self.storage.stats()
        embed = ""
        try:
            embed = self.host._embedding_model_name()
        except Exception:
            pass
        last = getattr(self.host, "_last_export", None) or {}
        return {
            "current_embedding_model": embed,
            "exports": files,
            "last": {"name": str(last.get("name") or "")},
            "counts": {
                "scopes": int(stats.get("scopes", 0) or 0),
                "messages": int(stats.get("message_identities", 0) or 0),
                "summaries": int(stats.get("summary_nodes", 0) or 0),
                "memories": int(stats.get("memory_items", 0) or 0),
                "catalog": int(stats.get("catalog_entries", 0) or 0),
                "embeddings": int(stats.get("embeddings", 0) or 0),
            },
            "note": ("导出的包可以在别的 bot 上导入（会话、消息、分层摘要、记忆账本、人物印象、"
                     "群文化、情绪记忆、注入与日程）。**重新导入时要匹配同一个嵌入模型**："
                     "对不上就只导文本与记忆、跳过向量，面板会写明。"),
        }

    async def _read_imports(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """快速学习面板：能导入什么格式、已导入过哪些群。"""
        rows = await self.storage.all_scopes("aiocqhttp")
        imported = []
        for row in rows:
            scope_id = str(row.get("scope_id") or "")
            if not scope_id:
                continue
            counts = await self.storage.scope_activity([scope_id])
            stat = counts.get(scope_id) or {}
            imported.append({
                "group_id": str(row.get("conversation_id") or ""),
                "name": str(row.get("display_name") or ""),
                "messages": int(stat.get("messages", 0) or 0),
                "last_active": str(stat.get("last_active") or ""),
                "whitelisted": bool(self.host.settings.allows_group(
                    str(row.get("conversation_id") or ""))),
            })
        imported.sort(key=lambda item: -item["messages"])
        return {
            "scopes": imported[:50],
            "note": ("支持 QQChatExporter V5 导出的 chunked-jsonl：zip 根目录下每个文件夹是一个群"
                     "（含 manifest.json 与 chunks/*.jsonl）。**不在白名单里的群也能导入**，"
                     "只是不会建人物印象——印象只给白名单群建。"),
        }

    async def _read_culture(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """群说话风格 / 群内黑话 / 人物心理档案 / 情绪记忆（MaiBot 式的那两块）。"""
        group = str(query.get("group_id") or "").strip()
        scope = self._scope_or_raise(group) if group else (
            next(iter(self._known_scopes().values()), None))
        scope_ids = [scope] if scope else []
        return {
            "scope": group or "",
            "style_rules": await self.storage.list_style_rules(scope_ids, 30),
            "lexicon": await self.storage.list_lexicon(scope_ids, 40, known_only=False),
            "profiles": await self.storage.list_person_profiles(30),
            "mood_events": await self.storage.recent_mood_events(scope_ids, 20),
            "note": "说话风格与黑话是按群学的；人物档案与情绪记忆跨群共用一份。",
        }

    async def _read_injections(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """提示词注入列表（自带 vs 自定义、开关状态）。"""
        rows = await self.storage.list_prompt_injections()
        return {
            "items": rows,
            "enabled": [row["injection_id"] for row in rows if row.get("enabled")],
            "note": "注入会以【强制规则】的形式追加到系统提示；自带预设立即可用，可改内容。",
        }

    async def _read_todos(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        group = str(query.get("group_id") or "").strip()
        scope = self._scope_or_raise(group) if group else (
            next(iter(self._known_scopes().values()), None))
        if not scope:
            return []
        return list(await self.storage.list_todos(scope))

    async def _read_scheduled(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        scheduler = getattr(self.host, "scheduler", None)
        if scheduler is None:
            return []
        try:
            tasks = await scheduler.list_tasks() if hasattr(scheduler, "list_tasks") \
                else list(getattr(scheduler, "tasks", []) or [])
        except Exception:
            tasks = []
        out: list[dict[str, Any]] = []
        for task in tasks:
            out.append({
                "task_id": str(getattr(task, "task_id", "") or (task.get("task_id") if isinstance(task, Mapping) else "")),
                "kind": str(getattr(task, "kind", "") or (task.get("kind") if isinstance(task, Mapping) else "")),
                "instruction": str(getattr(task, "instruction", "") or (task.get("instruction") if isinstance(task, Mapping) else ""))[:300],
                "due_at": str(getattr(task, "due_at", "") or (task.get("due_at") if isinstance(task, Mapping) else ""))[:19],
            })
        return out

    async def _read_intents(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        group = str(query.get("group_id") or "").strip()
        scope = self._scope_or_raise(group) if group else None
        rows = await self.storage._fetchall(
            "SELECT intent_id,scope_id,instruction,keywords_json,remaining,"
            "cooldown_seconds,status,created_at FROM standing_intents"
            + (" WHERE scope_id=?" if scope else "") + " ORDER BY created_at DESC LIMIT 50",
            (scope,) if scope else ())
        out: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            try:
                data["keywords"] = json.loads(data.pop("keywords_json") or "[]")
            except Exception:
                data["keywords"] = []
            out.append(data)
        return out

    async def _read_usage(self, query: Mapping[str, Any]) -> dict[str, Any]:
        days = min(max(int(query.get("days", 7) or 7), 1), 60)
        return await self.storage.usage_summary(days)

    async def _read_media(self, query: Mapping[str, Any]) -> dict[str, Any]:
        media = getattr(self.host, "media", None)
        group = str(query.get("group_id") or "").strip()
        scope = self._scope_or_raise(group) if group else (
            next(iter(self._known_scopes().values()), None))
        if media is None or not scope:
            return {"items": [], "count": 0}
        limit = min(max(int(query.get("limit", 24) or 24), 1), 100)
        rows = await media.recent(scope, limit)
        items = []
        for row in rows:
            data = dict(row) if isinstance(row, Mapping) else {}
            items.append({
                "media_id": str(data.get("item_id") or ""),
                "kind": str(data.get("kind") or ""),
                "mime": str(data.get("mime") or ""),
                "size": int(data.get("size") or 0),
                "note": str(data.get("note") or "")[:80],
                "at": str(data.get("created_at") or "")[:19],
            })
        return {"items": items, "count": len(items)}

    async def _read_stickers(self, query: Mapping[str, Any]) -> dict[str, Any]:
        stickers = getattr(self.host, "stickers", None)
        if stickers is None:
            return {"count": 0, "items": []}
        try:
            items = stickers.select(limit=24)
        except TypeError:
            items = stickers.select(24)
        except Exception:
            items = []
        out = []
        for item in items or []:
            pick = (lambda name, default="": item.get(name, default)
                    if isinstance(item, Mapping) else getattr(item, name, default))
            path = str(pick("path"))
            out.append({
                "sha256": str(pick("sha256"))[:16],
                "media_type": str(pick("media_type")),
                "size": int(pick("size", 0) or 0),
                "file": path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
            })
        total = 0
        try:
            total = int(getattr(stickers.stats(), "count", 0) or 0)
        except Exception:
            try:
                total = int(stickers.count())
            except Exception:
                total = len(out)
        return {"count": total, "items": out}

    async def _read_extensions(self, query: Mapping[str, Any]) -> dict[str, Any]:
        manager = getattr(self.host, "_scripts", None)
        registry = getattr(self.host, "_extensions", None)
        bundles = []
        if registry is not None:
            try:
                bundles = [dict(item) if isinstance(item, Mapping) else item
                           for item in registry.list()]
            except Exception:
                bundles = []
        scripts = []
        if manager is not None:
            try:
                scripts = list(manager.list())
            except Exception:
                scripts = []
        return {"bundles": bundles, "scripts": scripts}

    async def _read_capabilities(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """能力总表：AstrBot 内置 / 其它插件 / 本插件（含拓展）——bot 能用什么，一眼可见。"""
        host = self.host
        rows: list[dict[str, Any]] = []
        try:
            manager = host.context.get_llm_tool_manager()
            tools = list(manager.get_full_tool_set())
        except Exception:
            tools = []
        from .extensions import known_tool_names

        own = known_tool_names()
        for tool in tools:
            name = str(getattr(tool, "name", ""))
            if not name:
                continue
            active = bool(getattr(tool, "active", True))
            source = "本插件" if name in own else "AstrBot/其它插件"
            rows.append({
                "name": name,
                "source": source,
                "active": active,
                "description": str(getattr(tool, "description", "") or "")[:160],
            })
        gated = self._gated_own_tools()
        for name in gated:
            rows.append({"name": name, "source": "本插件(被拓展开关关闭)", "active": False,
                         "description": "在「拓展」页打开对应包即可恢复"})
        rows.sort(key=lambda item: (item["source"], item["name"]))
        return {"total": len(rows), "items": rows}

    def _gated_own_tools(self) -> list[str]:
        registry = getattr(self.host, "_extensions", None)
        if registry is None:
            return []
        try:
            from .extensions import BUILTIN_PACKS

            missing: list[str] = []
            for pack_id, (_name, _desc, tools) in BUILTIN_PACKS.items():
                for tool in tools:
                    if not registry.is_tool_enabled(tool):
                        missing.append(tool)
            return missing
        except Exception:
            return []

    async def _read_config(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        host = self.host
        schema: dict[str, Any] = {}
        try:
            path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
            schema = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            schema = {}
        raw = dict(getattr(host, "raw_config", {}) or {})
        out: list[dict[str, Any]] = []
        for key, meta in schema.items():
            info = meta if isinstance(meta, Mapping) else {}
            value = raw.get(key, info.get("default"))
            masked = _is_secret_key(key)
            out.append({
                "key": key,
                "type": str(info.get("type") or "string"),
                "description": str(info.get("description") or ""),
                "default": "********" if masked and info.get("default") else info.get("default"),
                "value": ("********" if value else "") if masked else value,
                "secret": masked,
                "group": self._config_group(key),
            })
        for key, value in raw.items():
            if key not in schema:
                out.append({"key": key, "type": "string", "description": "（无 schema 的自定义项）",
                            "default": None, "value": value, "group": "其它"})
        return out

    @staticmethod
    def _config_group(key: str) -> str:
        if any(token in key for token in ("provider", "persona", "agent_max", "tool_timeout", "llm_timeout")):
            return "模型与人格"
        if any(token in key for token in ("whitelist", "group", "quiet", "cooldown", "batch", "wake", "sample", "actions_per_hour", "random_wait")):
            return "群与节奏"
        if any(token in key for token in ("memory", "summary", "catalog", "recent_message", "evidence", "context_char", "compression", "limit", "embedding")):
            return "记忆与检索"
        if any(token in key for token in ("mood", "sticker", "emoji", "poke", "reaction", "style", "affinity")):
            return "情绪与表情"
        if any(token in key for token in ("ssh", "browser", "program", "self_learning", "evolution", "subagent", "decision", "standing", "workspace")):
            return "能力与拓展"
        if any(token in key for token in ("qzone", "qq_tools", "media", "gateway", "scheduler", "proactive", "heartbeat", "plan_interval", "reflect", "request")):
            return "自动化与渠道"
        return "其它"

    async def _read_personas(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        context = getattr(self.host, "context", None)
        manager = getattr(context, "persona_manager", None) if context else None
        if manager is None:
            return []
        try:
            items = manager.get_all_personas()
            if hasattr(items, "__await__"):
                items = await items
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for item in items or []:
            if isinstance(item, Mapping):
                out.append({"persona_id": str(item.get("persona_id") or item.get("id") or ""),
                            "name": str(item.get("name") or "")})
            else:
                out.append({"persona_id": str(getattr(item, "persona_id", "") or getattr(item, "id", "")),
                            "name": str(getattr(item, "name", "") or "")})
        return out

    # ------------------------------------------------------------ 记忆森林
    @staticmethod
    def _time_text(value: Any, granularity: str) -> str:
        """按层级给时间粒度：越具体的记忆显示得越精确（用户明确要求）。"""
        raw = str(value or "").strip()
        if not raw:
            return ""
        text = raw.replace("T", " ").replace("+00:00", "")
        if granularity == "minute":
            return text[:16]
        if granularity == "day":
            return text[:10]
        if granularity == "month":
            return text[:7]
        if granularity == "relative":
            try:
                stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                seconds = (datetime.now(timezone.utc) - stamp).total_seconds()
                if seconds < 3600:
                    return f"{int(seconds // 60)} 分钟前"
                if seconds < 86400:
                    return f"{int(seconds // 3600)} 小时前"
                if seconds < 86400 * 30:
                    return f"{int(seconds // 86400)} 天前"
                return text[:10]
            except Exception:
                return text[:10]
        return text[:19]

    async def _read_memory_tree(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """记忆森林：群 → 类目 → 条目 → 证据；`node=` 懒加载某一层的子节点。"""
        node = str(query.get("node") or "").strip()
        if node:
            return {"children": await self._tree_children(node)}
        scopes = await self.storage.all_scopes("aiocqhttp")
        known = {str(v): str(k) for k, v in self._known_scopes().items()}
        roots: list[dict[str, Any]] = []
        for row in scopes[:60]:
            scope = str(row.get("scope_id") or "")
            conversation = str(row.get("conversation_id") or "")
            if not scope:
                continue
            stats = await self.storage.stats(scope)
            roots.append({
                "id": f"scope:{scope}",
                "type": "scope",
                "label": conversation,
                "hint": str(row.get("display_name") or "") or known.get(scope, ""),
                "count": (stats.get("summary_nodes", 0) + stats.get("catalog_entries", 0)
                          + stats.get("memory_items", 0)),
                "time": "",
                "time_granularity": "",
                "children_loaded": False,
            })
        return {"roots": roots, "total_nodes": sum(item["count"] for item in roots)}

    async def _tree_children(self, node: str) -> list[dict[str, Any]]:
        parts = node.split(":")
        if parts[0] == "scope" and len(parts) >= 2:
            return await self._tree_scope_children(parts[1])
        if parts[0] == "cat" and len(parts) >= 3:
            return await self._tree_category_children(parts[1], parts[2])
        if parts[0] == "item" and len(parts) >= 3:
            return await self._tree_item_children(parts[1], ":".join(parts[2:]))
        raise WebUIError(f"未知的树节点：{node}")

    async def _tree_scope_children(self, scope: str) -> list[dict[str, Any]]:
        stats = await self.storage.stats(scope)
        impressions = await self.storage.list_impressions(
            tuple(dict.fromkeys(self._known_scopes().values())) or (scope,), 200)
        topics = await self.storage.recent_topics(scope, 200)
        rows = (
            ("summaries", "分层摘要", stats.get("summary_nodes", 0),
             "按批次/情节/主题压缩的长期记忆", "none"),
            ("catalog", "记忆账本", stats.get("catalog_entries", 0),
             "带证据的事实·偏好·约定", "relative"),
            ("impressions", "人物印象", len(impressions), "对某个人的长期判断", "relative"),
            ("topics", "群话题", len(topics), "这个群聊过什么", "day"),
        )
        return [{
            "id": f"cat:{scope}:{category}",
            "type": "category",
            "label": label,
            "hint": hint,
            "count": count,
            "time": "",
            "time_granularity": "",
            "children_loaded": False,
        } for category, label, count, hint, _gran in rows]

    async def _tree_category_children(self, scope: str, category: str) -> list[dict[str, Any]]:
        if category == "summaries":
            rows = await self.storage.list_summaries(scope, 60)
            out = []
            for row in rows:
                level = int(getattr(row, "level", 1) or 1)
                out.append({
                    "id": f"item:summary:{getattr(row, 'summary_id', '')}",
                    "type": "summary",
                    "label": f"L{level} · {getattr(row, 'title', '') or '（无标题）'}",
                    "hint": str(getattr(row, "body", "") or "")[:200],
                    "count": len(getattr(row, "citations", ()) or ()),
                    "time": self._time_text(getattr(row, "created_at", ""),
                                            {1: "minute", 2: "day", 3: "month"}.get(level, "day")),
                    "time_granularity": {1: "精确到分钟", 2: "精确到天",
                                         3: "精确到月"}.get(level, "精确到天"),
                    "children_loaded": False,
                    "editable": False,
                })
            return out
        if category == "catalog":
            ledger = getattr(self.host, "ledger", None)
            if ledger is None:
                return []
            rows = await ledger.catalog((scope,), 100)
            out = []
            for item in rows:
                pick = (lambda name, default="": item.get(name, default)
                        if isinstance(item, Mapping) else getattr(item, name, default))
                out.append({
                    "id": f"item:catalog:{pick('entry_id')}",
                    "type": "catalog",
                    "label": str(pick("subject")) or "记忆",
                    "hint": str(pick("value"))[:200],
                    "count": len(pick("evidence_ids", []) or []),
                    "time": self._time_text(pick("updated_at"), "relative"),
                    "time_granularity": "相对时间",
                    "kind": str(pick("kind")),
                    "status": str(pick("status")),
                    "children_loaded": False,
                    "editable": True,
                })
            return out
        if category == "impressions":
            rows = await self.storage.list_impressions(
                tuple(dict.fromkeys(self._known_scopes().values())) or (scope,), 100)
            return [{
                "id": f"item:impression:{row.get('user_id', '')}",
                "type": "impression",
                "label": str(row.get("display_name") or row.get("user_id") or ""),
                "hint": (str(row.get("impression") or "")
                         + (f"（出现在 {len(row.get('groups') or [])} 个会话）"
                            if row.get("groups") else "")),
                "count": len(row.get("tags") or []),
                "time": self._time_text(row.get("updated_at"), "relative"),
                "time_granularity": "相对时间",
                "children_loaded": False,
                "editable": True,
            } for row in rows]
        if category == "topics":
            rows = await self.storage.recent_topics(scope, 100)
            return [{
                "id": f"item:topic:{row.get('topic_id', '')}",
                "type": "topic",
                "label": str(row.get("title") or "话题"),
                "hint": f"出现过 {row.get('hits', 0)} 次",
                "count": 0,
                "time": self._time_text(row.get("created_at"), "day"),
                "time_granularity": "精确到天",
                "children_loaded": False,
                "editable": False,
            } for row in rows]
        raise WebUIError(f"未知的类目：{category}")

    async def _tree_item_children(self, item_kind: str, item_id: str) -> list[dict[str, Any]]:
        """条目 → 证据（摘要的引用消息 / 账本的证据消息），时间精确到分钟。"""
        if item_kind == "summary":
            row = await self.storage._fetchone(
                "SELECT scope_id FROM summary_nodes WHERE summary_id=?", (item_id,))
            if row is None:
                return []
            cites = await self.storage._fetchall(
                "SELECT message_id, quote FROM summary_citations "
                "WHERE summary_id=? LIMIT 30", (item_id,))
            pairs = [(str(c["message_id"]), str(c["quote"] or "")) for c in cites]
            scope = str(row["scope_id"])
        elif item_kind == "catalog":
            row = await self.storage._fetchone(
                "SELECT scope_id, evidence_json FROM catalog_entries WHERE entry_id=?",
                (item_id,))
            if row is None:
                return []
            try:
                ids = json.loads(str(row["evidence_json"] or "[]"))
            except Exception:
                ids = []
            pairs = [(str(x), "") for x in ids[:30]]
            scope = str(row["scope_id"])
        else:
            return []
        out = []
        for message_id, quote in pairs:
            message = await self.storage.find_message_any(scope, message_id)
            if message is None:
                continue
            out.append({
                "id": f"item:message:{message.message_id}",
                "type": "message",
                "label": str(message.sender_name or message.sender_id),
                "hint": str(message.text or quote or "")[:200],
                "count": 0,
                "time": self._time_text(message.occurred_at, "minute"),
                "time_granularity": "精确到分钟",
                "children_loaded": True,
                "editable": False,
            })
        return out

    async def _read_backups(self, query: Mapping[str, Any]) -> list[dict[str, Any]]:
        directory = self.storage.path.parent / "backups"
        if not directory.is_dir():
            return []
        files = sorted((f for f in directory.iterdir() if f.is_file()),
                       key=lambda f: f.name, reverse=True)[:30]
        return [{"name": f.name, "size": f.stat().st_size} for f in files]

    # ------------------------------------------------------------ 写
    async def write(self, action: str, body: Mapping[str, Any]) -> Any:
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            raise WebUIError(f"未知的操作：{action}")
        return await handler(body)

    async def _do_config_set(self, body: Mapping[str, Any]) -> dict[str, Any]:
        host = self.host
        key = str(body.get("key") or "").strip()
        if not key:
            raise WebUIError("缺少 key")
        value = body.get("value")
        if isinstance(value, str) and value.strip() == "":
            value = ""
        if _is_secret_key(key) and str(value).strip() == "********":
            return {"ok": True, "key": key, "value": "（保持原值）", "unchanged": True}
        host.raw_config[key] = value
        from .config import PluginConfig

        host.settings = PluginConfig.from_mapping(host.raw_config)
        host._config_signature_seen = host._config_signature()
        host._persist_raw_config()
        return {"ok": True, "key": key, "value": value}

    async def _do_whitelist_set(self, body: Mapping[str, Any]) -> dict[str, Any]:
        host = self.host
        groups = body.get("groups")
        if not isinstance(groups, list):
            raise WebUIError("groups 必须是数组")
        clean = [str(x).strip() for x in groups if str(x).strip()]
        host.raw_config["group_whitelist"] = clean
        from .config import PluginConfig

        host.settings = PluginConfig.from_mapping(host.raw_config)
        host._config_signature_seen = host._config_signature()
        host._persist_raw_config()
        return {"ok": True, "groups": clean}

    async def _do_message_delete(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """删单条消息：内部编号 / QQ 消息号都收（录实：只认内部编号 → 点了没反应）。"""
        _group, scope = self._scope_of(body)
        ref = str(body.get("message_id") or body.get("qq_id") or "").strip()
        if not ref:
            raise WebUIError("缺少 message_id")
        row = await self.storage.find_message_any(scope, ref)
        if row is None:
            raise WebUIError(f"这条消息不在本会话记录里：{ref[:24]}")
        result = await self.storage.delete_message(scope, row.message_id)
        removed = int(result)
        files = _unlink_media_files(result)
        return {"ok": True, "removed": removed, "media_files": files,
                "message_id": row.message_id, "qq_id": row.upstream_message_id}

    async def _do_user_delete(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _group, scope = self._scope_of(body)
        user_id = str(body.get("user_id") or "").strip()
        if not user_id:
            raise WebUIError("缺少 user_id")
        result = await self.storage.delete_user(user_id, scope)
        files = _unlink_media_files(result)
        return {"ok": True, "removed": int(result), "media_files": files}

    async def _do_group_wipe(self, body: Mapping[str, Any]) -> dict[str, Any]:
        group, scope = self._scope_of(body)
        result = await self.storage.delete_group(scope)
        files = _unlink_media_files(result)
        media = getattr(self.host, "media", None)
        if media is not None:
            try:
                files += await media.delete_scope(scope)
            except Exception:
                pass
        return {"ok": True, "group_id": group, "removed": int(result), "media_files": files}

    async def _do_wipe_all(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if str(body.get("confirm") or "") != "确认清空":
            raise WebUIError("危险操作：请把 confirm 填成「确认清空」")
        result = await self.storage.delete_all()
        files = _unlink_media_files(result)
        stickers = getattr(self.host, "stickers", None)
        if stickers is not None:
            try:
                stickers.clear()
            except Exception:
                pass
        mood = getattr(self.host, "mood", None)
        if mood is not None:
            try:
                mood.load_dict({})          # 情绪复位（mood.json 里那份也一起清）
            except Exception:
                pass
        queue = getattr(self.host, "task_queue", None)
        if queue is not None:
            try:
                await queue.ensure_table()   # 刚才删过 tasks 表，把表补回来
            except Exception:
                pass
        media = getattr(self.host, "media", None)
        if media is not None:
            try:
                files += await media.delete_all()
            except Exception:
                pass
        for attr in ("_handled_event_ids", "_handled_event_keys"):
            if hasattr(self.host, attr):
                setattr(self.host, attr, set())
        if hasattr(self.host, "_save_event_watermark"):
            try:
                self.host._save_event_watermark()
            except Exception:
                pass
        return {"ok": True, "removed": int(result), "media_files": files}

    async def _do_media_clear(self, body: Mapping[str, Any]) -> dict[str, Any]:
        media = getattr(self.host, "media", None)
        if media is None:
            raise WebUIError("媒体归档未启用")
        removed = await media.delete_all()
        return {"ok": True, "removed_files": removed}

    async def _do_sticker_clear(self, body: Mapping[str, Any]) -> dict[str, Any]:
        stickers = getattr(self.host, "stickers", None)
        if stickers is None:
            raise WebUIError("表情包未启用")
        try:
            count = await stickers.clear()
        except TypeError:
            count = stickers.clear()
        return {"ok": True, "removed": count}

    async def _do_impression_save(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _group, scope = self._scope_of(body)
        user_id = str(body.get("user_id") or "").strip()
        if not user_id:
            raise WebUIError("缺少 user_id")
        tags = body.get("tags")
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
        result = await self.storage.upsert_impression(
            scope, user_id,
            display_name=str(body.get("display_name") or "") or None,
            impression=str(body.get("impression") or "") or None,
            tags=[str(t) for t in tags] if isinstance(tags, list) else None,
        )
        return {"ok": True, "impression": dict(result) if isinstance(result, Mapping) else result}

    async def _do_impression_delete(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _group, scope = self._scope_of(body)
        user_id = str(body.get("user_id") or "").strip()
        if not user_id:
            raise WebUIError("缺少 user_id")
        async with self.storage._write_lock:
            cursor = await self.storage._conn().execute(
                "DELETE FROM user_impressions WHERE scope_id=? AND user_id=?", (scope, user_id))
            await self.storage._conn().commit()
        return {"ok": True, "removed": int(cursor.rowcount or 0)}

    async def _do_affinity_adjust(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _group, scope = self._scope_of(body)
        user_id = str(body.get("user_id") or "").strip()
        if not user_id:
            raise WebUIError("缺少 user_id")
        result = await self.storage.adjust_affinity(
            scope, user_id, float(body.get("delta") or 0),
            str(body.get("note") or "") or None)
        return {"ok": True, "affinity": dict(result) if isinstance(result, Mapping) else result}

    async def _do_mood_set(self, body: Mapping[str, Any]) -> dict[str, Any]:
        mood = getattr(self.host, "mood", None)
        if mood is None:
            raise WebUIError("情绪系统未启用")
        result = mood.set(str(body.get("mood") or "平静"),
                          float(body.get("intensity") or 0.3),
                          str(body.get("note") or ""))
        return {"ok": True, "mood": result.as_dict()}

    async def _do_plan_add(self, body: Mapping[str, Any]) -> dict[str, Any]:
        detail = str(body.get("detail") or "").strip()
        if not detail:
            raise WebUIError("缺少 detail（要做什么）")
        hours = float(body.get("hours_ahead") or 1)
        due = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        count = await self.storage.add_plans([{
            "due_at": due, "kind": str(body.get("kind") or "custom"),
            "detail": detail[:400]}])
        return {"ok": True, "added": int(count), "due_at": due}

    async def _do_plan_drop(self, body: Mapping[str, Any]) -> dict[str, Any]:
        plan_id = str(body.get("plan_id") or "").strip()
        if not plan_id:
            raise WebUIError("缺少 plan_id")
        ok = await self.storage.drop_plan(plan_id)
        return {"ok": bool(ok), "plan_id": plan_id}

    async def _do_import_chatlog_upload(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self.host.import_chatlog_chunk(
            str(body.get("upload_id") or ""), int(body.get("index") or 0),
            str(body.get("data") or ""))

    async def _do_import_chatlog_finish(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self.host.import_chatlog_finish(
            str(body.get("upload_id") or ""), str(body.get("filename") or ""),
            bool(body.get("learn", True)))

    async def _do_import_chatlog_abort(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self.host.import_chatlog_abort(str(body.get("upload_id") or ""))

    async def _do_memory_export_save(self, body: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.host.export_memory(save=True)
        return {"ok": True, "name": result.get("name"), "size": result.get("size"),
                "saved_to": result.get("saved_to"), "counts": result.get("counts"),
                "embedding_model": result.get("embedding_model")}

    async def _do_memory_import_upload(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self.host.memory_import_upload(
            str(body.get("upload_id") or ""), int(body.get("index") or 0),
            str(body.get("data") or ""))

    async def _do_memory_import_finish(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return await self.host.memory_import_finish(
            str(body.get("upload_id") or ""), str(body.get("filename") or ""))

    async def _do_style_rule_delete(self, body: Mapping[str, Any]) -> dict[str, Any]:
        rule_id = str(body.get("rule_id") or "").strip()
        if not rule_id:
            raise WebUIError("缺少 rule_id")
        return {"ok": True, "removed": await self.storage.delete_style_rule(rule_id)}

    async def _do_lexicon_save(self, body: Mapping[str, Any]) -> dict[str, Any]:
        scope = self._scope_or_raise(str(body.get("group_id") or ""))
        term = str(body.get("term") or "").strip()
        if not term:
            raise WebUIError("缺少 term")
        meaning = str(body.get("meaning") or "").strip()
        await self.storage.upsert_lexicon(scope, term, meaning, confidence=0.9)
        return {"ok": True, "term": term}

    async def _do_lexicon_delete(self, body: Mapping[str, Any]) -> dict[str, Any]:
        term_id = str(body.get("term_id") or "").strip()
        if not term_id:
            raise WebUIError("缺少 term_id")
        return {"ok": True, "removed": await self.storage.delete_lexicon(term_id)}

    async def _do_profile_delete(self, body: Mapping[str, Any]) -> dict[str, Any]:
        person_id = str(body.get("person_id") or "").strip()
        if not person_id:
            raise WebUIError("缺少 person_id")
        return {"ok": True, "removed": await self.storage.delete_person_profile(person_id)}

    async def _do_mood_event_delete(self, body: Mapping[str, Any]) -> dict[str, Any]:
        event_id = str(body.get("event_id") or "").strip()
        if not event_id:
            raise WebUIError("缺少 event_id")
        return {"ok": True, "removed": await self.storage.delete_mood_event(event_id)}

    async def _do_injection_save(self, body: Mapping[str, Any]) -> dict[str, Any]:
        injection_id = str(body.get("injection_id") or "").strip()
        name = str(body.get("name") or "").strip()
        content = str(body.get("content") or "").strip()
        if not content:
            raise WebUIError("注入内容不能为空")
        if not injection_id:
            import re as _re

            slug = _re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", name or content[:12]).strip("-")
            injection_id = f"custom-{slug[:24] or 'item'}"
        existing = {row["injection_id"]: row
                    for row in await self.storage.list_prompt_injections()}
        enabled = bool(body.get("enabled", existing.get(injection_id, {}).get("enabled", True)))
        item = await self.storage.upsert_prompt_injection(
            injection_id, name or injection_id, content, enabled=enabled,
            builtin=bool(existing.get(injection_id, {}).get("builtin", False)),
            sort_order=int(existing.get(injection_id, {}).get("sort_order", 50) or 50))
        self._invalidate_injections()
        return {"ok": True, "injection": item}

    async def _do_injection_toggle(self, body: Mapping[str, Any]) -> dict[str, Any]:
        injection_id = str(body.get("injection_id") or "").strip()
        if not injection_id:
            raise WebUIError("缺少 injection_id")
        rows = {row["injection_id"]: row for row in await self.storage.list_prompt_injections()}
        row = rows.get(injection_id)
        if row is None:
            raise WebUIError(f"没有这条注入：{injection_id}")
        enabled = bool(body.get("enabled", not row.get("enabled")))
        await self.storage.upsert_prompt_injection(
            injection_id, str(row.get("name") or injection_id),
            str(row.get("content") or ""), enabled=enabled,
            builtin=bool(row.get("builtin")), sort_order=int(row.get("sort_order") or 0))
        self._invalidate_injections()
        return {"ok": True, "injection_id": injection_id, "enabled": enabled}

    async def _do_injection_delete(self, body: Mapping[str, Any]) -> dict[str, Any]:
        injection_id = str(body.get("injection_id") or "").strip()
        rows = {row["injection_id"]: row for row in await self.storage.list_prompt_injections()}
        row = rows.get(injection_id)
        if row is None:
            raise WebUIError(f"没有这条注入：{injection_id}")
        if row.get("builtin"):
            raise WebUIError("自带预设不能删，直接关掉即可（改内容也行）")
        removed = await self.storage.delete_prompt_injection(injection_id)
        self._invalidate_injections()
        return {"ok": True, "removed": removed}

    def _invalidate_injections(self) -> None:
        """让插件重新读注入（宿主有缓存）。"""
        try:
            host = self.host
            invalidate = getattr(host, "_invalidate_injections", None)
            if callable(invalidate):
                invalidate()
        except Exception:
            pass

    async def _do_todo_add(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _group, scope = self._scope_of(body)
        content = str(body.get("content") or "").strip()
        if not content:
            raise WebUIError("缺少 content")
        item = await self.storage.add_todos(scope, [content[:400]])
        return {"ok": True, "todo": item}

    async def _do_todo_update(self, body: Mapping[str, Any]) -> dict[str, Any]:
        todo_id = str(body.get("todo_id") or "").strip()
        if not todo_id:
            raise WebUIError("缺少 todo_id")
        ok = await self.storage.update_todo(
            todo_id, status=str(body.get("status") or "completed"),
            content=str(body.get("content") or "") or None)
        return {"ok": bool(ok), "todo_id": todo_id}

    async def _do_todo_clear(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _group, scope = self._scope_of(body)
        removed = await self.storage.clear_todos(scope)
        return {"ok": True, "removed": int(removed or 0)}

    async def _do_intent_add(self, body: Mapping[str, Any]) -> dict[str, Any]:
        handler = getattr(self.host, "_intent_action", None)
        if handler is None:
            raise WebUIError("事件条件指令未启用")
        result = await handler("add", {
            "instruction": str(body.get("instruction") or ""),
            "keywords_json": json.dumps(body.get("keywords") or [], ensure_ascii=False),
        }, None)
        return {"ok": True, "result": result}

    async def _do_intent_remove(self, body: Mapping[str, Any]) -> dict[str, Any]:
        handler = getattr(self.host, "_intent_action", None)
        if handler is None:
            raise WebUIError("事件条件指令未启用")
        result = await handler("remove", {"intent_id": str(body.get("intent_id") or "")}, None)
        return {"ok": True, "result": result}

    async def _do_extension_toggle(self, body: Mapping[str, Any]) -> dict[str, Any]:
        extension_id = str(body.get("id") or "").strip()
        if not extension_id:
            raise WebUIError("缺少 id")
        want_enabled = bool(body.get("enabled", True))
        manager = getattr(self.host, "_scripts", None)
        # 先看 scripts 拓展（它们有自己的启用状态，能声明默认关闭）
        if manager is not None and manager.get(extension_id) is not None:
            manager.set_enabled(extension_id, want_enabled)
            return {"ok": True, "id": extension_id, "enabled": want_enabled,
                    "kind": "script"}
        registry = getattr(self.host, "_extensions", None)
        if registry is None:
            raise WebUIError("拓展系统未启用")
        if want_enabled:
            registry.enable(extension_id)
        else:
            registry.disable(extension_id)
        return {"ok": True, "id": extension_id, "enabled": want_enabled, "kind": "bundle"}

    async def _do_extension_config_save(self, body: Mapping[str, Any]) -> dict[str, Any]:
        manager = getattr(self.host, "_scripts", None)
        if manager is None:
            raise WebUIError("脚本拓展未启用")
        ext_id = str(body.get("id") or "").strip()
        if not ext_id:
            raise WebUIError("缺少 id")
        values = body.get("config")
        if not isinstance(values, Mapping):
            raise WebUIError("config 必须是对象")
        try:
            saved = manager.save_config(ext_id, values)
        except Exception as error:
            raise WebUIError(f"{type(error).__name__}: {error}"[:200]) from error
        return {"ok": True, "id": ext_id, "config": saved}

    async def _do_extension_config_reset(self, body: Mapping[str, Any]) -> dict[str, Any]:
        manager = getattr(self.host, "_scripts", None)
        if manager is None:
            raise WebUIError("脚本拓展未启用")
        ext_id = str(body.get("id") or "").strip()
        if not ext_id:
            raise WebUIError("缺少 id")
        try:
            manager.config_path(ext_id).unlink(missing_ok=True)
        except OSError as error:
            raise WebUIError(f"删除配置失败：{error}") from error
        return {"ok": True, "id": ext_id, "config": manager.effective_config(ext_id)}

    async def _do_extension_reload(self, body: Mapping[str, Any]) -> dict[str, Any]:
        manager = getattr(self.host, "_scripts", None)
        if manager is None:
            raise WebUIError("脚本拓展未启用")
        problems = await manager.reload(str(body.get("id") or "").strip() or None)
        return {"ok": True, "problems": list(problems or [])}

    async def _do_compress(self, body: Mapping[str, Any]) -> dict[str, Any]:
        _group, scope = self._scope_of(body)
        created = await self.host._compress_scope(scope, rounds=6)
        return {"ok": True, "created": created}

    async def _do_reflect(self, body: Mapping[str, Any]) -> dict[str, Any]:
        group, scope = self._scope_of(body)
        result = await self.host._self_reflect(scope, group)
        return {"ok": True, "result": result}

    async def _do_backup(self, body: Mapping[str, Any]) -> dict[str, Any]:
        directory = self.storage.path.parent / "backups"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = await self.storage.backup(directory / f"memory-{stamp}.db")
        return {"ok": True, "backup": Path(path).name}

    async def _do_restore(self, body: Mapping[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        directory = self.storage.path.parent / "backups"
        source = directory / name
        if not name or not source.is_file():
            raise WebUIError("找不到该备份")
        await self.storage.close()
        from shutil import copy2

        copy2(source, self.storage.path)
        await self.storage.open()
        return {"ok": True, "restored": name}

    async def _do_order(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """给 bot 下令：写进任务表 + 立刻跑一轮自主行动（工具循环，可自己发消息汇报）。"""
        group, scope = self._scope_of(body)
        text = str(body.get("text") or "").strip()
        if not text:
            raise WebUIError("缺少 text（要它做什么）")
        await self.storage.add_todos(scope, [text[:400]])
        spawned = False
        runner = getattr(self.host, "_autonomous_action_loop", None)
        spawn = getattr(self.host, "_spawn", None)
        if callable(runner) and callable(spawn):
            spawn(runner(scope, text, "WebUI 指令"))
            spawned = True
        return {"ok": True, "group_id": group, "dispatched": spawned, "order": text[:200]}

    async def _do_ask(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """问 bot 一句：单次模型调用（不带工具），答案会写进该会话记忆。"""
        group, scope = self._scope_of(body)
        question = str(body.get("question") or body.get("text") or "").strip()
        if not question:
            raise WebUIError("缺少 question")
        provider = getattr(self.host.settings, "reply_provider_id", "") or ""
        answer = await self.host._llm_text(
            provider, prompt=question,
            system_prompt=("你是这个群里的成员，用短句口语回答（3~30字），"
                           "不要 Markdown、不要解释。"))
        answer = str(answer or "").strip()
        if answer:
            try:
                from .models import NormalizedMessage

                await self.host.ingest.ingest(NormalizedMessage(
                    platform="aiocqhttp",
                    account_id=self._account_of(scope) or "0",
                    conversation_id=group, upstream_message_id=f"webui:{int(datetime.now().timestamp())}",
                    sender_id=str(self.host.context.get_self_id() if hasattr(self.host.context, "get_self_id") else "self"),
                    sender_name="self", text=f"[WebUI 问答] 问：{question} 答：{answer}",
                    occurred_at=_iso(), raw_event={"derived": True, "kind": "webui-ask"},
                    parts=[], event_type="notice.webui"), f"webui-ask:{scope}")
            except Exception:
                pass
        return {"ok": True, "answer": answer}

    async def _do_say(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """以 bot 身份在指定群/私聊说一句（白名单内）。"""
        text = str(body.get("text") or "").strip()
        if not text:
            raise WebUIError("缺少 text")
        gateway = getattr(self.host, "gateway", None)
        if gateway is None:
            raise WebUIError("QQ 网关未就绪")
        if hasattr(self.host, "_bind_gateway_client"):
            self.host._bind_gateway_client()
        target_group = str(body.get("group_id") or "").strip()
        target_user = str(body.get("user_id") or "").strip()
        if not target_group and not target_user:
            raise WebUIError("需要 group_id 或 user_id")
        if target_group and hasattr(self.host.settings, "allows_group") \
                and not self.host.settings.allows_group(target_group):
            raise WebUIError(f"群 {target_group} 不在白名单")
        segment = {"type": "text", "data": {"text": text[:1000]}}
        if target_group:
            await gateway.execute("send_group_msg",
                                  group_id=int(target_group), message=[segment])
        else:
            await gateway.execute("send_private_msg",
                                  user_id=int(target_user), message=[segment])
        return {"ok": True, "target": target_group or target_user}

    # ------------------------------------------------------------ 任务队列
    async def _read_tasks(self, query: Mapping[str, Any]) -> dict[str, Any]:
        """任务队列：排队中的、跑着的、完成/失败的，含长期任务的次数与区间。"""
        queue = getattr(self.host, "task_queue", None)
        rows: list[dict[str, Any]] = []
        stats: dict[str, int] = {}
        if queue is not None:
            status = str(query.get("status") or "").strip()
            limit = min(max(int(query.get("limit", 60) or 60), 1), 200)
            tasks = await queue.list(statuses=[status] if status else None, limit=limit)
            stats = await queue.stats()
            for task in tasks:
                rows.append({
                    "task_id": task.task_id, "kind": task.kind,
                    "title": task.title or task.detail[:60],
                    "detail": task.detail[:300], "status": task.status,
                    "source": task.source, "priority": task.priority,
                    "conversation": task.conversation,
                    "next_run_at": task.next_run_at, "last_run_at": task.last_run_at,
                    "runs": task.runs_done,
                    "max_runs": task.max_runs,
                    "runs_left": ("∞" if task.max_runs < 0
                                  else max(0, task.max_runs - task.runs_done)),
                    "interval_minutes": task.interval_seconds // 60,
                    "long_term": task.is_long_term,
                    "window": task.window,
                    "last_error": task.last_error[:200],
                    "last_result": task.last_result[:200],
                    "created_at": task.created_at,
                })
        legacy = [
            {"task_id": f"plan:{row.get('plan_id', '')}", "kind": "日程(旧)",
             "title": str(row.get("detail") or ""), "status": "pending",
             "next_run_at": str(row.get("due_at") or ""), "legacy": True,
             "source": "旧日程表"}
            for row in await self.storage.pending_plans(20)
        ]
        return {"stats": stats, "tasks": rows, "legacy": legacy,
                "kinds": ["reply", "agent", "tool", "notify", "reflect", "compress"]}

    async def _do_task_add(self, body: Mapping[str, Any]) -> dict[str, Any]:
        queue = getattr(self.host, "task_queue", None)
        if queue is None:
            raise WebUIError("任务队列未启用")
        _group, scope = self._scope_of(body)
        conversation = next((key for key, value in self._known_scopes().items()
                             if value == scope), "")
        window: dict[str, Any] = {}
        if str(body.get("not_before") or "").strip():
            window["after"] = str(body["not_before"]).strip()
        if str(body.get("not_after") or "").strip():
            window["before"] = str(body["not_after"]).strip()
        daily = str(body.get("daily_window") or "").strip()
        if daily and "-" in daily:
            start, end = daily.split("-", 1)
            window["daily"] = [start.strip(), end.strip()]
        task = await queue.add(
            str(body.get("kind") or "agent"), detail=str(body.get("detail") or ""),
            title=str(body.get("title") or ""), scope_id=scope, conversation=conversation,
            payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
            priority=int(body.get("priority") or 0),
            delay_seconds=float(body.get("delay_minutes") or 0) * 60,
            interval_seconds=int(body.get("interval_minutes") or 0) * 60,
            max_runs=int(body.get("max_runs") or 1), window=window, source="user")
        return {"ok": True, "task_id": task.task_id, "first_run_at": task.next_run_at,
                "long_term": task.is_long_term}

    async def _do_task_cancel(self, body: Mapping[str, Any]) -> dict[str, Any]:
        queue = getattr(self.host, "task_queue", None)
        if queue is None:
            raise WebUIError("任务队列未启用")
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            raise WebUIError("缺少 task_id")
        okay = await queue.cancel(task_id)
        if not okay and task_id.startswith("plan:"):
            legacy = await self.storage.drop_plan(task_id.split("plan:", 1)[1])
            return {"ok": bool(legacy), "legacy": True}
        if not okay:
            raise WebUIError("没找到这个任务（或它已经结束）")
        return {"ok": True, "task_id": task_id}

    async def _do_task_update(self, body: Mapping[str, Any]) -> dict[str, Any]:
        queue = getattr(self.host, "task_queue", None)
        if queue is None:
            raise WebUIError("任务队列未启用")
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            raise WebUIError("缺少 task_id")
        changes: dict[str, Any] = {}
        if str(body.get("detail") or "").strip():
            changes["detail"] = str(body["detail"]).strip()[:2000]
        if body.get("interval_minutes") not in (None, "", -1):
            changes["interval_seconds"] = int(body["interval_minutes"]) * 60
        if body.get("max_runs") not in (None, "", -2):
            changes["max_runs"] = int(body["max_runs"])
        if body.get("priority") not in (None, "", -99):
            changes["priority"] = int(body["priority"])
        daily = str(body.get("daily_window") or "").strip()
        if daily and "-" in daily:
            start, end = daily.split("-", 1)
            changes["window"] = {"daily": [start.strip(), end.strip()]}
        if not changes:
            raise WebUIError("没有要改的字段")
        okay = await queue.update(task_id, **changes)
        if not okay:
            raise WebUIError("没找到这个任务")
        return {"ok": True, "task_id": task_id}

    # ------------------------------------------------------------ 森林写操作
    async def _do_memory_node_save(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """新增/编辑一个记忆节点（账本条目或人物印象）。

        `node_id` 为空=新增；`kind` 决定写哪张表（catalog/impression）。
        新增的账本条目落进 `catalog_entries`（kind 支持 fact/preference/agreement/…），
        与 bot 自己学到的记忆同一张表 —— 这样"手动记的知识"也会进上下文。
        """
        _group, scope = self._scope_of(body)
        node_id = str(body.get("node_id") or "").strip()
        node_type = str(body.get("type") or "").strip().lower()
        subject = str(body.get("subject") or body.get("label") or "").strip()
        value = str(body.get("value") or body.get("hint") or "").strip()
        if not subject and not value:
            raise WebUIError("标题和内容至少要填一个")
        kind = str(body.get("kind") or "fact").strip().lower()
        try:
            confidence = float(body.get("confidence") or 0.9)
        except (TypeError, ValueError):
            confidence = 0.9

        if node_type == "impression" or node_id.startswith("item:impression:"):
            user_id = str(body.get("user_id") or "").strip()
            if node_id.startswith("item:impression:"):
                user_id = node_id.split("item:impression:", 1)[1]
            if not user_id:
                raise WebUIError("人物印象需要 user_id")
            tags = body.get("tags")
            if isinstance(tags, str):
                tags = [x.strip() for x in tags.replace("，", ",").split(",") if x.strip()]
            result = await self.storage.upsert_impression(
                scope, user_id, display_name=subject or None,
                impression=value or None,
                tags=[str(x) for x in tags] if isinstance(tags, list) else None)
            return {"ok": True, "saved": "impression", "user_id": user_id,
                    "impression": dict(result) if isinstance(result, Mapping) else result}

        ledger = getattr(self.host, "ledger", None)
        if ledger is None:
            raise WebUIError("记忆账本未启用")
        entry_id = ""
        if node_id.startswith("item:catalog:"):
            entry_id = node_id.split("item:catalog:", 1)[1]
        now = datetime.now(timezone.utc).isoformat()
        db = self.storage._conn()
        if entry_id:
            row = await self.storage._fetchone(
                "SELECT memory_id FROM catalog_entries WHERE entry_id=?", (entry_id,))
            if row is None:
                raise WebUIError("这条记忆已经不在了")
            memory_id = str(row["memory_id"])
            async with self.storage._write_lock:
                await db.execute(
                    "UPDATE memory_items SET subject=?, status='active', confidence=?, "
                    "updated_at=? WHERE memory_id=?", (subject, confidence, now, memory_id))
                await db.execute(
                    "UPDATE catalog_entries SET subject=?, value=?, confidence=?, "
                    "updated_at=? WHERE entry_id=?",
                    (subject, value, confidence, now, entry_id))
                await db.execute(
                    "INSERT INTO memory_revisions(memory_revision_id, memory_id, revision_no, "
                    "value, operation, created_at) SELECT ?, ?, COALESCE(MAX(revision_no),0)+1, "
                    "?, 'update', ? FROM memory_revisions WHERE memory_id=?",
                    (f"rev-{entry_id[:12]}-{int(datetime.now().timestamp())}", memory_id,
                     value, now, memory_id))
                await db.commit()
            return {"ok": True, "saved": "catalog", "entry_id": entry_id, "updated": True}

        memory_id = f"mem-webui-{int(datetime.now().timestamp() * 1000)}"
        entry_id = f"entry-webui-{int(datetime.now().timestamp() * 1000)}"
        async with self.storage._write_lock:
            await db.execute(
                "INSERT INTO memory_items(memory_id, scope_id, kind, subject, status, "
                "confidence, idempotency_key, created_at, updated_at) "
                "VALUES(?,?,?,?,'active',?,?,?,?)",
                (memory_id, scope, kind, subject, confidence, f"webui:{entry_id}", now, now))
            await db.execute(
                "INSERT INTO memory_revisions(memory_revision_id, memory_id, revision_no, "
                "value, operation, created_at) VALUES(?,?,1,?,'create',?)",
                (f"rev-{entry_id[:12]}", memory_id, value, now))
            await db.execute(
                "INSERT INTO catalog_entries(entry_id, memory_id, scope_id, kind, subject, "
                "value, status, confidence, evidence_json, updated_at) "
                "VALUES(?,?,?,?,?,?,'active',?,'[]',?)",
                (entry_id, memory_id, scope, kind, subject, value, confidence, now))
            await db.commit()
        return {"ok": True, "saved": "catalog", "entry_id": entry_id, "created": True}

    async def _do_memory_node_delete(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """删除一个记忆节点：账本条目 / 人物印象 / 群话题 / 摘要 / 单条证据消息。"""
        node = str(body.get("node_id") or body.get("id") or "").strip()
        if not node:
            raise WebUIError("缺少 node_id")
        parts = node.split(":")
        kind = parts[0]
        item_kind = parts[1] if len(parts) > 1 else ""
        item_id = ":".join(parts[2:]) if len(parts) > 2 else ""
        db = self.storage._conn()

        if kind == "item" and item_kind == "catalog":
            row = await self.storage._fetchone(
                "SELECT memory_id, scope_id FROM catalog_entries WHERE entry_id=?", (item_id,))
            if row is None:
                raise WebUIError("这条记忆已经不在了")
            async with self.storage._write_lock:
                await db.execute("DELETE FROM catalog_entries WHERE entry_id=?", (item_id,))
                await db.execute("DELETE FROM memory_evidence WHERE memory_id=?",
                                 (str(row["memory_id"]),))
                await db.execute("DELETE FROM memory_revisions WHERE memory_id=?",
                                 (str(row["memory_id"]),))
                await db.execute("DELETE FROM memory_items WHERE memory_id=?",
                                 (str(row["memory_id"]),))
                await db.commit()
            return {"ok": True, "removed": 1, "type": "catalog"}

        if kind == "item" and item_kind == "impression":
            scope = str(body.get("group_id") or "").strip()
            scope_id = self._scope_or_raise(scope) if scope else None
            async with self.storage._write_lock:
                if scope_id:
                    cursor = await db.execute(
                        "DELETE FROM user_impressions WHERE scope_id=? AND user_id=?",
                        (scope_id, item_id))
                else:
                    cursor = await db.execute(
                        "DELETE FROM user_impressions WHERE user_id=?", (item_id,))
                await db.commit()
            return {"ok": True, "removed": int(cursor.rowcount or 0), "type": "impression"}

        if kind == "item" and item_kind == "topic":
            async with self.storage._write_lock:
                cursor = await db.execute(
                    "DELETE FROM group_topics WHERE topic_id=?", (item_id,))
                await db.commit()
            return {"ok": True, "removed": int(cursor.rowcount or 0), "type": "topic"}

        if kind == "item" and item_kind == "summary":
            async with self.storage._write_lock:
                await db.execute("DELETE FROM summary_citations WHERE summary_id=?", (item_id,))
                await db.execute("DELETE FROM summary_inputs WHERE summary_id=?", (item_id,))
                cursor = await db.execute(
                    "DELETE FROM summary_nodes WHERE summary_id=?", (item_id,))
                await db.commit()
            return {"ok": True, "removed": int(cursor.rowcount or 0), "type": "summary"}

        if kind == "item" and item_kind == "message":
            group, scope = self._scope_of(body)
            message = await self.storage.find_message_any(scope, item_id)
            if message is None:
                raise WebUIError("这条消息已经不在了")
            result = await self.storage.delete_message(scope, message.message_id)
            files = _unlink_media_files(result)
            return {"ok": True, "removed": int(result), "media_files": files,
                    "type": "message", "group_id": group}

        raise WebUIError(f"这类节点不支持直接删除：{node}")

    def _account_of(self, scope: str) -> str:
        for attr in ("_known_scopes",):
            pass
        storage = getattr(self.host, "storage", None)
        return str(getattr(storage, "account_id", "") or "")
