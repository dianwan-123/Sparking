from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return min(max(int(value), low), high)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float, low: float, high: float) -> float:
    try:
        return min(max(float(value), low), high)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class PluginConfig:
    enabled: bool
    group_whitelist: frozenset[str]
    judge_provider_id: str
    reply_provider_id: str
    summary_provider_id: str
    embedding_provider_id: str
    persona_id: str
    vision_provider_id: str
    recent_message_limit: int
    catalog_limit: int
    summary_limit: int
    evidence_limit: int
    context_char_budget: int
    compression_batch_size: int
    autonomous_sample_rate: float
    batch_window_min_seconds: int
    batch_window_max_seconds: int
    batch_max_messages: int
    wake_merge_seconds: float
    wake_merge_max_seconds: float
    group_cooldown_seconds: int
    user_cooldown_seconds: int
    max_actions_per_hour: int
    max_random_wait_seconds: int
    quiet_start_hour: int
    quiet_end_hour: int
    sticker_learning: bool
    sticker_max_bytes: int
    sticker_max_count: int
    reactions_enabled: bool
    pokes_enabled: bool
    allowed_emoji_ids: frozenset[str]
    dashboard_base_url: str
    dashboard_api_key: str
    allow_arbitrary_plugin_urls: bool
    agent_max_steps: int
    tool_timeout_seconds: int
    llm_timeout_seconds: int
    enable_qq_tools: bool
    enable_qzone: bool
    enable_media_archive: bool
    enable_scheduler: bool
    max_sequence_segments: int
    sequence_char_limit: int
    typing_chars_per_second: float
    media_max_file_mb: int
    media_total_mb: int
    web_fetch_max_mb: int
    gateway_max_per_hour: int
    self_reflect_interval_hours: int
    mood_update_minutes: int
    enable_private_memory: bool
    auto_handle_requests: bool
    proactive_interval_minutes: int
    heartbeat_interval_seconds: int
    idle_actions_per_hour: int
    plan_interval_hours: int
    origin_wake_prefixes: str
    style_learn_hours: int
    auto_install_deps: bool
    browser_auto_install: bool
    browser_persist: bool
    browser_executable_path: str
    subagent_enabled: bool
    subagent_provider_id: str
    subagent_tools_enabled: bool
    decision_subagent_enabled: bool
    decision_subagent_max_steps: int
    text_to_image_threshold: int
    ssh_host: str
    ssh_port: int
    ssh_user: str
    ssh_password: str
    ssh_notes: str
    ssh_command_timeout: int
    ssh_agent_max_steps: int
    evolution_iterations: int
    task_queue_enabled: bool
    task_concurrency: int
    task_tick_seconds: int
    standing_orders: str
    program_port: int
    program_ttl_minutes: int
    self_learning_enabled: bool
    self_learning_min_seconds: int

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "PluginConfig":
        d = dict(data or {})
        # WebUI set_conf 会把 None 塞进配置（用户清空输入框）：None 统一转 ""，
        # 否则 str(None)='None' 把字面 "None" 当配置值（实录：persona_id='None'
        # → 人格对象的 system_prompt=None → prompt.strip() 崩掉主动参与与回复）。
        for key, value in list(d.items()):
            if value is None:
                d[key] = ""
        groups = frozenset(str(x).strip() for x in d.get("group_whitelist", []) if str(x).strip())
        emojis = frozenset(str(x).strip() for x in d.get("allowed_emoji_ids", ["76", "124", "144"]) if str(x).strip())
        bw_min = _int(d.get("batch_window_min_seconds"), 15, 0, 3600)
        bw_max = max(_int(d.get("batch_window_max_seconds"), 60, 0, 3600), bw_min)
        wake_merge = _float(d.get("wake_merge_seconds"), 4.0, 0, 60)
        # 上限允许小于防抖窗口：连续刷屏时上限会先触发强制开口（语义自洽）
        wake_merge_max = _float(d.get("wake_merge_max_seconds"), 15.0, 0, 120)
        return cls(
            enabled=bool(d.get("enabled", True)),
            group_whitelist=groups,
            judge_provider_id=str(d.get("judge_provider_id", "")).strip(),
            reply_provider_id=str(d.get("reply_provider_id", "")).strip(),
            summary_provider_id=str(d.get("summary_provider_id", "")).strip(),
            embedding_provider_id=str(d.get("embedding_provider_id", "")).strip(),
            persona_id=str(d.get("persona_id", "")).strip(),
            vision_provider_id=str(d.get("vision_provider_id", "")).strip(),
            recent_message_limit=_int(d.get("recent_message_limit"), 30, 5, 200),
            catalog_limit=_int(d.get("catalog_limit"), 24, 5, 100),
            summary_limit=_int(d.get("summary_limit"), 14, 1, 40),
            evidence_limit=_int(d.get("evidence_limit"), 12, 1, 50),
            context_char_budget=_int(d.get("context_char_budget"), 24000, 4000, 120000),
            compression_batch_size=_int(d.get("compression_batch_size"), 40, 10, 200),
            autonomous_sample_rate=_float(d.get("autonomous_sample_rate"), 0.35, 0, 1),
            batch_window_min_seconds=bw_min,
            batch_window_max_seconds=bw_max,
            batch_max_messages=_int(d.get("batch_max_messages"), 10, 1, 50),
            wake_merge_seconds=wake_merge,
            wake_merge_max_seconds=wake_merge_max,
            group_cooldown_seconds=_int(d.get("group_cooldown_seconds"), 180, 0, 86400),
            user_cooldown_seconds=_int(d.get("user_cooldown_seconds"), 300, 0, 86400),
            max_actions_per_hour=_int(d.get("max_actions_per_hour"), 8, 1, 100),
            max_random_wait_seconds=_int(d.get("max_random_wait_seconds"), 15, 0, 120),
            quiet_start_hour=_int(d.get("quiet_start_hour"), 1, 0, 23),
            quiet_end_hour=_int(d.get("quiet_end_hour"), 7, 0, 23),
            sticker_learning=bool(d.get("sticker_learning", True)),
            sticker_max_bytes=_int(d.get("sticker_max_bytes"), 8_000_000, 10_000, 30_000_000),
            sticker_max_count=_int(d.get("sticker_max_count"), 2000, 10, 20000),
            reactions_enabled=bool(d.get("reactions_enabled", True)),
            pokes_enabled=bool(d.get("pokes_enabled", True)),
            allowed_emoji_ids=emojis,
            dashboard_base_url=str(d.get("dashboard_base_url", "http://127.0.0.1:6185")).rstrip("/"),
            dashboard_api_key=str(d.get("dashboard_api_key", "")),
            allow_arbitrary_plugin_urls=bool(d.get("allow_arbitrary_plugin_urls", False)),
            agent_max_steps=_int(d.get("agent_max_steps"), 40, 1, 80),
            tool_timeout_seconds=_int(d.get("tool_timeout_seconds"), 60, 5, 300),
            llm_timeout_seconds=_int(d.get("llm_timeout_seconds"), 180, 10, 600),
            enable_qq_tools=bool(d.get("enable_qq_tools", True)),
            enable_qzone=bool(d.get("enable_qzone", True)),
            enable_media_archive=bool(d.get("enable_media_archive", True)),
            enable_scheduler=bool(d.get("enable_scheduler", True)),
            max_sequence_segments=_int(d.get("max_sequence_segments"), 5, 1, 5),
            sequence_char_limit=_int(d.get("sequence_char_limit"), 2000, 100, 8000),
            typing_chars_per_second=_float(d.get("typing_chars_per_second"), 8.0, 1.0, 60.0),
            media_max_file_mb=_int(d.get("media_max_file_mb"), 15, 1, 200),
            media_total_mb=_int(d.get("media_total_mb"), 1024, 16, 51200),
            web_fetch_max_mb=_int(d.get("web_fetch_max_mb"), 5, 1, 50),
            gateway_max_per_hour=_int(d.get("gateway_max_per_hour"), 40, 1, 500),
            self_reflect_interval_hours=_int(d.get("self_reflect_interval_hours"), 6, 0, 72),
            mood_update_minutes=_int(d.get("mood_update_minutes"), 60, 0, 1440),
            enable_private_memory=bool(d.get("enable_private_memory", True)),
            auto_handle_requests=bool(d.get("auto_handle_requests", True)),
            proactive_interval_minutes=_int(d.get("proactive_interval_minutes"), 15, 0, 720),
            heartbeat_interval_seconds=_int(d.get("heartbeat_interval_seconds"), 120, 0, 3600),
            idle_actions_per_hour=_int(d.get("idle_actions_per_hour"), 4, 0, 30),
            plan_interval_hours=_int(d.get("plan_interval_hours"), 6, 1, 24),
            origin_wake_prefixes=str(d.get("origin_wake_prefixes", "/") or "/"),
            style_learn_hours=_int(d.get("style_learn_hours"), 3, 0, 168),
            auto_install_deps=bool(d.get("auto_install_deps", True)),
            browser_auto_install=bool(d.get("browser_auto_install", True)),
            browser_persist=bool(d.get("browser_persist", True)),
            browser_executable_path=str(d.get("browser_executable_path", "")).strip(),
            subagent_enabled=bool(d.get("subagent_enabled", True)),
            subagent_provider_id=str(d.get("subagent_provider_id", "")).strip(),
            subagent_tools_enabled=bool(d.get("subagent_tools_enabled", True)),
            decision_subagent_enabled=bool(d.get("decision_subagent_enabled", True)),
            decision_subagent_max_steps=_int(
                d.get("decision_subagent_max_steps"), 6, 2, 20),
            text_to_image_threshold=_int(d.get("text_to_image_threshold"), 500, 0, 200000),
            ssh_host=str(d.get("ssh_host", "")).strip(),
            ssh_port=_int(d.get("ssh_port"), 22, 1, 65535),
            ssh_user=str(d.get("ssh_user", "")).strip(),
            ssh_password=str(d.get("ssh_password", "")),
            ssh_notes=str(d.get("ssh_notes", "")),
            ssh_command_timeout=_int(d.get("ssh_command_timeout"), 60, 5, 1800),
            ssh_agent_max_steps=_int(d.get("ssh_agent_max_steps"), 30, 1, 200),
            evolution_iterations=_int(d.get("evolution_iterations"), 4, 1, 12),
            task_queue_enabled=bool(d.get("task_queue_enabled", True)),
            task_concurrency=_int(d.get("task_concurrency"), 2, 1, 8),
            task_tick_seconds=_int(d.get("task_tick_seconds"), 15, 5, 300),
            standing_orders=str(d.get("standing_orders", "")).strip(),
            program_port=_int(d.get("program_port"), 8765, 1024, 65535),
            program_ttl_minutes=_int(d.get("program_ttl_minutes"), 10, 1, 120),
            self_learning_enabled=bool(d.get("self_learning_enabled", True)),
            self_learning_min_seconds=_int(d.get("self_learning_min_seconds"), 60, 10, 3600),
        )

    def allows_group(self, group_id: str | int | None) -> bool:
        return self.enabled and bool(group_id) and str(group_id) in self.group_whitelist
