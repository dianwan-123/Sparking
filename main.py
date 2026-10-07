from __future__ import annotations

import asyncio
import base64
import inspect
import json
import random
import uuid
import re
import shlex
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any, Awaitable

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import ToolSet
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .src.agent_tools import AgentToolService, AuthorizationError
from .src.astrbot_runtime import build_runtime_manifest
from .src.browser import BrowserError, BrowserSession, allow_local_port, page_text
from .src.browser_setup import ensure_browser_environment
from .src.pw_driver import PlaywrightDriver
from .src.ssh_ext import (
    SSHAgentManager,
    SSHClient,
    SSHConfig,
    SSHError,
    ensure_paramiko,
)
from .src.compression import CompressionService
from .src.config import PluginConfig
from .src.context_builder import ContextBuilder, ScopedMemoryFacade
from .src.dashboard_client import DashboardClient
from .src.decision import DecisionEngine
from .src.emotions import MoodStore
from .src.extensions import ExtensionError, ExtensionRegistry
from .src.humanization import (
    HumanizationConfig,
    HumanizedSender,
    MessagePlan,
    PlanValidationError,
    analyze_recent_style,
    humanize_plan,
    is_local_path_leak,
    is_tool_markup_leak,
    is_tool_status_narration,
    is_plan_json_leak,
    parse_message_plan,
    salvage_message_plan,
)
from .src.ingest import IngestService
from .src.interaction import InteractionController, InteractionPermit
from .src.json_utils import compact_json, parse_json_object, parse_json_value
from .src.media_archive import MediaArchive, MediaArchiveError, default_aiohttp_fetch, extract_urls, fetch_bounded
from .src.memory_ledger import MemoryLedger
from .src.models import Decision
from .src.task_queue import (
    ALL_KINDS,
    KIND_AGENT,
    KIND_COMPRESS,
    KIND_CUSTOM,
    KIND_NOTIFY,
    KIND_REFLECT,
    KIND_REPLY,
    KIND_TOOL,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    Task,
    TaskQueue,
    TaskRunner as TaskQueueRunner,
)
from .src.qq_gateway import (
    GatewayFacade,
    QQGateway,
    QQGatewayError,
    messages_of,
    payload as qq_payload,
)
from .src.program_host import ProgramHost, slugify
from .src.evolution import GEPAOptimizer, EvalDataset
from .src.script_ext import ScriptExtensionError, ScriptExtensionManager
from .src.self_learning import (
    SKILL_REVIEW_SYSTEM_PROMPT,
    apply_skill,
    build_evidence,
    normalize_skill,
    parse_skill_proposal,
)
from .src.onebot import (
    _text_and_reply,
    get_image,
    is_poke_notice,
    is_request_event,
    normalize_event,
    normalize_notice_event,
    normalize_raw_event,
    normalize_request_record,
    safe_serialize,
    send_poke,
    set_msg_emoji_like,
)
from .src.prompts import (
    DECISION_SYSTEM_PROMPT,
    MEMORY_TRUST_POLICY,
    MOOD_UPDATE_PROMPT,
    REFLECTION_PROMPT,
    REQUEST_ANALYSIS_SYSTEM_PROMPT,
    REPLY_SYSTEM_PROMPT,
    SCHEDULED_TASK_PROMPT,
    AGENT_FINAL_OUTPUT_PROMPT,
    SSH_AGENT_SYSTEM_PROMPT,
    SUBAGENT_SYSTEM_PROMPT,
    TASK_DISPATCH_PROMPT,
    QQMSG_STYLE_PROMPT,
    GROUP_LIFE_BEHAVIORS_PROMPT,
)
from .src.qq_services import QQService, QzoneError, QzoneService
from .src.retrieval import RetrievalService
from .src.rhythm import build_plan_prompt, idle_choice, parse_plan_response
from .src.scheduler import ScheduledTask, TaskRunner, TaskScheduler
from .src.skill_manager import SkillManager, SkillValidationError
from .src.stickers import StickerError, StickerManager
from .src.storage import Storage
from .src import timeutil
from .src import injections as prompt_injections
from .src import data_tools, pdf_reader, program_host
from .src.workspace import Workspace, WorkspaceError
from .src.web_tools import WebToolError, fetch_text, search_web


_PLUGIN_NAME = "astrbot_plugin_long_memory_agent"
_PAGE_PREFIX = f"/{_PLUGIN_NAME}/page"

_PREFETCH_KEY = "long_memory_agent.prefetched_context"
_SKIP_KEY = "long_memory_agent.skip_injection"


@dataclass(slots=True)
class _BatchedMessage:
    """A non-wake group message held for batch judging."""

    event: AstrMessageEvent
    scope_id: str
    message_id: str
    image_only: bool
    text: str
    sender_name: str
    occurred_at: str


def _provider_id(configured: str, current: str | None) -> str:
    return configured or (current or "")


def _plain_result(event: AstrMessageEvent, text: str):
    return event.plain_result(text)


_AT_RE = re.compile(r"\[\s*CQ\s*[:：]\s*at\s*[,，]\s*qq\s*=\s*(\d{5,})\s*\]")


def _extract_at_targets(text: str) -> list[str]:
    """Pull real QQ ids out of any hand-written `[CQ:at,qq=NNN]` syntax."""
    seen: list[str] = []
    for found in _AT_RE.findall(str(text) + " " + str(text)):
        qq = str(found).strip()
        if qq and qq not in seen:
            seen.append(qq)
    return seen[:5]


# 技术类问题（数学/信息学）：命中就强制先检索知识库再回答。中文词直接匹配；
# ASCII 缩写用 ASCII 字母数字的 lookaround 定界（\b 对汉字无效——汉字也算
# \w，"这道DP题"里 DP 前后都不存在 \b）。
_TECH_QUESTION_RE = re.compile(
    r"数学|微积分|导数|积分|极限|线性代数|矩阵|行列式|概率论|组合数|几何|三角函数|方程|不等式"
    r"|数论|质数|素数|同余|对数|向量|信息学|算法|数据结构|复杂度|动态规划|递归|递推|贪心|二分"
    r"|图论|最短路|生成树|线段树|树状数组|并查集|哈希|链表|单调栈|快排|归并|排序算法|搜索算法"
    r"|编程|代码|编译|报错|调试|指针|数组|溢出|正则|题解|题面|评测机|洛谷|蓝桥|天梯"
    r"|(?<![A-Za-z0-9])(?:DP|OI|NOIP|NOI|CSP|CSES?|TLE|MLE|WA|RE|CE|bug|hash|python"
    r"|java|linux|git|codeforces|leetcode|atcoder)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _within_hours(stamp: Any, hours: float) -> bool:
    """时间戳（ISO 字符串）距今是否在 hours 内；解析失败按"算新鲜"处理。"""
    try:
        then = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return True
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() <= hours * 3600


def _bubble_text(text: str, limit: int = 1200) -> str:
    """QQ bubble style: commas/periods become spaces, no trailing period.
    
    Markdown syntax is now allowed (QQ won't render but semantics preserved).
    Hand-written At syntax is stripped: real at-mentions must go through
    the structured `at` field so the transport renders a true @ mention.
    """
    cleaned = str(text)[:limit]
    # 只清理手写 At 语法（模拟 CQ 码），markdown 保留原样
    cleaned = _AT_RE.sub(" ", cleaned)
    for pattern in (
        r"\[\s*CQ\s*[:：]\s*at\s*[,，]\s*(?:qq\s*=\s*)?\s*[a-zA-Z0-9_]+\s*\]",
        r"[（(]?\s*CQ\s*[:：]\s*at\s*[,，]\s*qq\s*=\s*\d+\s*[)）]?",
        r"\[\s*at\s*[:：]?\s*qq\s*=\s*\d+\s*\]",
    ):
        cleaned = re.sub(pattern, " ", cleaned)
    for mark in ("，", "。", "、", "；"):
        cleaned = cleaned.replace(mark, " ")
    return " ".join(cleaned.split()).rstrip(". ")


@register(
    "astrbot_plugin_long_memory_agent",
    "Rikka0612",
    "星火 Sparking：让你的 Bot 像真人一样聊天、记事与自主行动（OneBot v11）",
    "1.0.2",
)
class LongMemoryAgentPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config)
        self.raw_config = config or {}
        self.settings = PluginConfig.from_mapping(self.raw_config)
        self._config_signature_seen = self._config_signature()
        self.storage: Storage | None = None
        self.ingest: IngestService | None = None
        self.ledger: MemoryLedger | None = None
        self.retrieval: RetrievalService | None = None
        self.context_builder: ContextBuilder | None = None
        self.compression: CompressionService | None = None
        self.decision: DecisionEngine | None = None
        self.interactions: InteractionController | None = None
        self.dashboard: DashboardClient | None = None
        self.skills: SkillManager | None = None
        self.agent_tools: AgentToolService | None = None
        self.stickers: StickerManager | None = None
        self.gateway: QQGateway | None = None
        self.task_queue: TaskQueue | None = None
        self.task_queue_runner: Any | None = None
        self.qq: QQService | None = None
        self.qzone: QzoneService | None = None
        self.media: MediaArchive | None = None
        self.mood: MoodStore | None = None
        self.scheduler: TaskScheduler | None = None
        self.task_runner: TaskRunner | None = None
        self._humanization: HumanizationConfig | None = None
        self._known_scopes: dict[str, str] = {}
        self._send_locks: dict[str, asyncio.Lock] = {}
        self._pending_counts: dict[str, int] = {}
        self._handled_event_ids: set[str] = set()
        self._handled_event_keys: set[str] = set()
        self._handled_since: str = ""
        self._event_timer: asyncio.Task[Any] | None = None
        self._send_blockade: dict[str, float] = {}
        self._processed_messages: set[str] = set()
        # 引用消息解析缓存（wake 与攒批路径都会查同一引用，避免重复 get_msg）
        self._quoted_cache: dict[str, dict[str, Any]] = {}
        # 技能自学习：每 scope 最近一次复盘时间（monotonic）与进行中标记
        self._last_skill_review: dict[str, float] = {}
        self._skill_review_inflight: set[str] = set()
        # 上游已删/不可用的模型（运行期拉黑，别再重试烧满 5 次）
        self._dead_providers: set[str] = set()
        # 待展开的合并转发：按会话累积（攒批时逐条覆盖会丢掉先前的转发）
        self._pending_forwards: dict[str, list[str]] = {}
        # 单实例守卫：重装/重载可能留下旧实例的循环在跑（实录：v0.32.6 旧实例与
        # v0.33.x 新实例并存，日志两套行号、旧实例抱死配置烧 5/5、抢日程）
        self._instance_id = ""
        self._instance_checked_at = 0.0
        self._instance_ok = True
        # 受限工作区（惰性构建：需要 storage 决定 workspace 路径）
        self._workspace_obj: Workspace | None = None
        self._batch_buffers: dict[str, list[_BatchedMessage]] = {}
        self._batch_timers: dict[str, asyncio.Task[Any]] = {}
        # 唤醒合并窗口：连发的 @/私聊一起读完一次回（v0.36.1 实录三连@回三次太机械）
        self._wake_buffers: dict[str, list[_BatchedMessage]] = {}
        self._wake_timers: dict[str, asyncio.Task[Any]] = {}
        self._wake_first_at: dict[str, float] = {}
        self._next_plan_at = float("inf")
        self._idle_budget: tuple[int, int] = (0, 0)
        self._lurk_cursor: int = 0
        self._pending_forward_ids: list[str] = []
        self._provider_fallback_warned: set[str] = set()
        self._last_signature_at = -6 * 3600.0  # cooldown long expired at boot
        self._last_signature_text = ""
        self._browser: BrowserSession | None = None
        self._studio: ProgramHost | None = None
        self._scripts: ScriptExtensionManager | None = None
        self._extensions: ExtensionRegistry | None = None
        self._browser_env: dict[str, Any] = {
            "playwright": False, "chromium": False,
            "checked": False, "error": "",
        }
        self._pw: PlaywrightDriver | None = None
        self._ssh: SSHClient | None = None
        self._ssh_agents: SSHAgentManager | None = None
        self._ssh_env: dict[str, Any] = {"paramiko": False, "checked": False, "error": ""}
        self._reply_queues: dict[str, asyncio.Queue] = {}
        self._reply_workers: dict[str, asyncio.Task] = {}
        self._reply_queue_enabled = True
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopping = False

    def _config_signature(self) -> str:
        try:
            return json.dumps(
                dict(self.raw_config or {}), sort_keys=True, default=str)
        except Exception:
            return ""

    def _refresh_settings_if_changed(self) -> bool:
        """配置被外部改动后让运行实例立刻生效（不依赖热重载）。

        AstrBotConfig 是普通 dict 子类：面板 save_config 会 merge 进同一对象，
        但运行中的插件只在 __init__ 构建过一次 settings，不刷新就永远是旧值——
        实录：面板里填好了 SSH，bot 仍说"未配置"。
        """
        signature = self._config_signature()
        if not signature or signature == self._config_signature_seen:
            return False
        self._config_signature_seen = signature
        try:
            self.settings = PluginConfig.from_mapping(self.raw_config)
            logger.info("长程记忆：检测到配置热更新，已重建运行参数")
            self._apply_auto_install_setting()
            self._invalidate_injections()
            # 后补的 SSH 配置：把 paramiko 安装/环境探测补跑一次
            try:
                if self._ssh_config().configured and not self._ssh_env.get("checked"):
                    self._spawn(self._ssh_setup_task())
                    logger.info("长程记忆：SSH 配置就绪，远程运维工具启用")
            except Exception:
                pass
        except Exception as error:
            logger.warning("长程记忆：配置热更新失败：%s", str(error)[:150])
        return True

    def _apply_auto_install_setting(self) -> None:
        """把"允许按需 pip 安装"的主人授权项同步给各模块（市场审查要求显式可关）。"""
        allow = bool(getattr(self.settings, "auto_install_deps", True))
        for module in (data_tools, pdf_reader, program_host):
            try:
                module.set_auto_install(allow)
            except Exception:
                pass
        logger.info("长程记忆：运行时自动安装依赖 = %s", "允许" if allow else "已关闭")

    def _persist_raw_config(self) -> None:
        """把内存里的 raw_config 落盘——AstrBotConfig 不会自动保存 setitem 的改动，
        我们 WebUI 的 set_conf 若只改内存，插件重载/重启后改动就丢了。"""
        saver = getattr(self.raw_config, "save_config", None)
        if not callable(saver):
            return
        try:
            saver()
        except Exception as error:
            logger.warning("长程记忆：配置落盘失败：%s", str(error)[:150])

    # ------------------------------------------------------------ 单实例守卫
    def _instance_stamp_path(self) -> Path | None:
        if self.storage is None:
            return None
        return self.storage.path.parent / "runtime" / "instance.json"

    def _claim_instance_slot(self) -> None:
        """声明本实例为最新一代：后台循环每跳自查，被顶替的旧实例自动下线。"""
        self._instance_id = uuid.uuid4().hex
        self._instance_checked_at = 0.0
        self._instance_ok = True
        path = self._instance_stamp_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "id": self._instance_id,
                "at": datetime.now(timezone.utc).isoformat(),
            }), encoding="utf-8")
        except OSError as error:
            logger.warning("长程记忆：实例标记写入失败（忽略）：%s", str(error)[:120])

    def _still_current_instance(self) -> bool:
        """单实例守卫：不是最新一代就把自己停掉（置位 _stopping，所有循环退出）。

        每 15 秒才真正读一次标记文件，热路径开销可忽略。
        """
        if not self._instance_id:
            return True
        now = time.monotonic()
        if now - self._instance_checked_at < 15:
            return self._instance_ok
        self._instance_checked_at = now
        ok = True
        path = self._instance_stamp_path()
        if path is not None:
            try:
                current = json.loads(path.read_text(encoding="utf-8")).get("id")
                ok = str(current or "") == self._instance_id
            except (OSError, ValueError):
                ok = True  # 读不到就当自己是最新，别误停
        self._instance_ok = ok
        if not ok:
            self._stopping = True
            logger.warning("长程记忆：检测到更新实例接管，旧实例停止后台循环与事件处理")
        return ok

    def _reap_zombie_instances(self) -> int:
        """清理同插件旧实例残留的后台任务。

        实录：AstrBot 重装/重载未终止旧实例时，旧循环会与新实例并存——旧实例
        抱着过期配置继续跑（两套行号、死模型 5/5 重试、和新实例抢日程）。这里在
        同一事件循环上找到"属于本插件类、但不是本实例"的循环任务并取消。
        """
        killed = 0
        own = asyncio.current_task()
        markers = ("_loop", "_tick", "_round", "_worker", "_review", "_reply",
                   "_digest")
        for task in list(asyncio.all_tasks()):
            if task is own or task.done():
                continue
            try:
                coro = task.get_coro()
            except Exception:
                continue
            name = str(getattr(coro, "__qualname__", "") or "")
            if not any(marker in name for marker in markers):
                continue
            frame = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
            if frame is None:
                continue
            try:
                owner = frame.f_locals.get("self")
            except Exception:
                owner = None
            if owner is None or owner is self:
                continue
            # 关键：按类名比较，不能用 type(owner) is type(self)——热重载会生成
            # 同名的新类对象，旧实例的类与新类 is 比较为 False，僵尸正好被漏掉
            # （实录：v0.32.6 僵尸因此逃过两轮清理，继续刷 deepseek 重试）
            if type(owner).__name__ != type(self).__name__:
                continue
            task.cancel()
            killed += 1
        if killed:
            logger.warning("长程记忆：清理了 %d 个旧实例残留的后台任务", killed)
        return killed

    async def initialize(self) -> None:
        # 时间基准：库里的时间存 UTC，凡是要给模型/给人看的都按这个时区换算
        # （实录：下午两点的消息被说成"凌晨六点"——UTC 串被原样递出去了）
        try:
            zone = timeutil.configure(self.context.get_config().get("timezone"))
        except Exception:
            zone = timeutil.configure(None)
        logger.info("长程记忆：时间基准 = %s（存储 UTC / 展示本地）",
                    zone or timeutil.zone_name() or "本机时区")
        self._apply_auto_install_setting()
        self._injections_cache = None
        root = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_long_memory_agent"
        root.mkdir(parents=True, exist_ok=True)
        (root / "backups").mkdir(exist_ok=True)
        self.storage = await Storage(root / "memory.db").open()
        self._claim_instance_slot()
        self._reap_zombie_instances()
        self.ingest = IngestService(self.storage)
        await self._seed_prompt_injections()   # 自带注入预设入库（用户改过的不覆盖）
        self.ledger = MemoryLedger(self.storage)
        self.retrieval = RetrievalService(self.storage, embedding=await self._embedding_adapter())
        self.context_builder = ContextBuilder(
            self.storage, self.retrieval, self.ledger, self.settings.context_char_budget
        )
        self.compression = CompressionService(
            self.storage, self._summary_llm, self.ledger, self.settings.summary_provider_id
        )
        self.decision = DecisionEngine(
            self._judge_llm,
            timeout_seconds=self.settings.llm_timeout_seconds,
            max_wait_seconds=self.settings.max_random_wait_seconds,
            allowed_emoji_ids=self.settings.allowed_emoji_ids,
            error_logger=lambda message: logger.info("长程记忆：%s", message),
        )
        self.interactions = InteractionController(
            group_cooldown_seconds=self.settings.group_cooldown_seconds,
            user_cooldown_seconds=self.settings.user_cooldown_seconds,
            max_actions_per_hour=self.settings.max_actions_per_hour,
            quiet_start_hour=self.settings.quiet_start_hour,
            quiet_end_hour=self.settings.quiet_end_hour,
            max_random_wait_seconds=self.settings.max_random_wait_seconds,
            error_handler=self._task_error,
        )
        if self.settings.dashboard_api_key:
            self.dashboard = DashboardClient(
                self.settings.dashboard_base_url,
                self.settings.dashboard_api_key,
                allow_arbitrary_plugin_urls=self.settings.allow_arbitrary_plugin_urls,
            )
            self.skills = SkillManager(self.dashboard)
            self.agent_tools = AgentToolService(
                self.context,
                self.dashboard,
                group_authorizer=lambda group_id: self.settings.allows_group(group_id),
                audit_callback=self._audit_callback,
            )
        self.stickers = StickerManager(
            root / "stickers",
            max_bytes=self.settings.sticker_max_bytes,
            max_count=self.settings.sticker_max_count,
        )
        self._humanization = HumanizationConfig(
            max_segments=self.settings.max_sequence_segments,
            max_total_chars=self.settings.sequence_char_limit,
            typing_chars_per_second=self.settings.typing_chars_per_second,
            max_sequences_per_hour=12,
        )
        self.mood = MoodStore(root / "mood.json")
        if self.settings.enable_media_archive:
            self.media = await MediaArchive(
                root / "media",
                max_file_bytes=self.settings.media_max_file_mb * 1024 * 1024,
                max_total_bytes=self.settings.media_total_mb * 1024 * 1024,
                fetch_stream=default_aiohttp_fetch,
            ).open()
        # 统一任务队列（一次回复 / 一轮 agent / 工具调用 / 主动通知 / 总结 全是任务）
        try:
            self.task_queue = TaskQueue(
                self.storage,
                concurrency=max(1, int(getattr(self.settings, "task_concurrency", 2) or 2)))
            await self.task_queue.ensure_table()
            self.task_queue_runner = TaskQueueRunner(
                self.task_queue, self._task_handlers(),
                tick_seconds=int(getattr(self.settings, "task_tick_seconds", 15) or 15),
                still_current=self._still_current_instance)
            if getattr(self.settings, "task_queue_enabled", True):
                self._spawn(self.task_queue_runner.run_forever())
            logger.info(
                "长程记忆：任务队列已就绪（并发 %d，常驻执行器 %s）",
                self.task_queue.concurrency,
                "开启" if getattr(self.settings, "task_queue_enabled", True) else "关闭")
        except Exception as error:
            logger.warning("长程记忆：任务队列初始化失败：%s", str(error)[:160])
            self.task_queue = None
            self.task_queue_runner = None
        if self._ready_qq():
            self.gateway = QQGateway(
                bot=None,
                hourly={"send": self.settings.gateway_max_per_hour},
            )
            self.qq = QQService(self.gateway)
            self.qzone = QzoneService(self.gateway) if self.settings.enable_qzone else None
            # Wire the live client now so timer-driven actions work on a cold
            # start; if the adapter is not loaded yet each tick self-heals.
            self._bind_gateway_client()
        # Rebuild the scope map from disk so autonomous loops act on every known
        # chat immediately instead of waiting for each chat's first message.
        await self._hydrate_known_scopes()
        if not self.settings.group_whitelist:
            logger.warning(
                "长程记忆：群白名单为空——不会完整记忆任何群，也不会在群里主动发言。"
                "如需启用，请在插件配置 group_whitelist 填入群号。"
            )
        if self.settings.enable_scheduler:
            self.scheduler = TaskScheduler(root / "scheduled_tasks.json")
            self.task_runner = TaskRunner(self.scheduler, self._run_scheduled_task)
            self._spawn(self.task_runner.loop())
        if self.settings.self_reflect_interval_hours > 0:
            self._spawn(self._reflection_loop())
        if self.settings.mood_update_minutes > 0:
            self._spawn(self._mood_status_loop())
        if self.settings.proactive_interval_minutes > 0:
            self._spawn(self._proactive_loop())
        if self.settings.heartbeat_interval_seconds > 0:
            self._next_plan_at = time.monotonic() + 75
            self._spawn(self._heartbeat_loop())
        if self.settings.style_learn_hours > 0:
            self._spawn(self._style_learn_loop())
        self._spawn(self._browser_setup_task())
        self._extensions = ExtensionRegistry(root / "extensions.json")
        self._ssh = SSHClient(
            self._ssh_config, connect_timeout=15.0)
        self._ssh_agents = SSHAgentManager(
            ssh=self._ssh,
            llm=self._ssh_agent_llm,
            provider_getter=self._ssh_provider,
            notes_getter=lambda: self.settings.ssh_notes,
            system_prompt=SSH_AGENT_SYSTEM_PROMPT,
            max_steps=self.settings.ssh_agent_max_steps,
            cmd_timeout=float(self.settings.ssh_command_timeout),
            char_budget=self.settings.context_char_budget,
            max_sessions=3,
            logger=lambda msg: logger.info("长程记忆：%s", str(msg)[:200]),
        )
        if self._ssh_configured():
            logger.info(
                "长程记忆：SSH 已配置（%s），远程运维工具就绪",
                self._ssh_config().target())
            self._spawn(self._ssh_setup_task())
        else:
            ssh_keys = sorted(
                str(k) for k in (self.raw_config or {}) if str(k).startswith("ssh_"))
            lacking = [
                name for name, value in (
                    ("ssh_host", self.settings.ssh_host),
                    ("ssh_user", self.settings.ssh_user),
                    ("ssh_password", self.settings.ssh_password),
                ) if not value
            ]
            if ssh_keys:
                # 配置里有 ssh_* 却没生效——下次日志直接能看到缺哪项
                logger.warning(
                    "长程记忆：配置里存在 %s 但 SSH 未就绪（缺少：%s）；"
                    "面板保存后应看到「成功~ 机器人正在热重载插件」提示",
                    "、".join(ssh_keys), "、".join(lacking) or "无")
            else:
                logger.info("长程记忆：SSH 未配置（可选功能）")
        self._load_event_watermark(root / "event_watermark.json")
        if not self._handled_since:
            # First ever start of this data dir: record the baseline so a
            # fresh install never walks back through months of history.
            # Truncated to whole seconds: OneBot event timestamps are
            # second-precision, and a microsecond-earlier event must not be
            # mistaken for pre-install history.
            self._handled_since = (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            )
            self._save_event_watermark()
        self._register_page_routes()
        # scripts/ 万能拓展：加载失败只影响该拓展自身，绝不阻塞插件启动
        try:
            await self.load_script_extensions()
        except Exception as error:
            logger.warning("长程记忆：scripts 拓展加载异常（忽略）：%s", str(error)[:200])
        if self.settings.persona_id:
            resolved = await self._persona_prompt()
            logger.info(
                "长程记忆：人格 %s %s",
                self.settings.persona_id,
                "已加载（自主发言将带该人设）" if resolved else "未加载，请对照启动日志检查ID",
            )
        logger.info("长程记忆插件已初始化；仅白名单群会被完整存储")

    async def terminate(self) -> None:
        """Shut down bulletproof: a failing step must never strand the database.

        AstrBot deletes data/plugin_data/<plugin> when the user picks "delete
        persistent data", but on Windows a still-open memory.db makes that
        fail and the group memory silently survives.
        """
        self._stopping = True
        try:
            if self._ssh_agents is not None:
                await self._ssh_agents.stop_all()
        except Exception as error:
            logger.warning("长程记忆：结束 SSH 子agent失败：%s", str(error)[:120])
        try:
            if self._ssh is not None:
                await self._ssh.close()
                self._ssh = None
        except Exception as error:
            logger.warning("长程记忆：关闭 SSH 连接失败：%s", str(error)[:120])
        try:
            if self._studio is not None:
                self._studio.close()
                self._studio = None
        except Exception as error:
            logger.warning("长程记忆：关闭 studio 宿主失败：%s", str(error)[:120])
        try:
            if self._scripts is not None:
                await self._scripts.unload_all()
                self._scripts = None
        except Exception as error:
            logger.warning("长程记忆：卸载 scripts 拓展失败：%s", str(error)[:120])
        try:
            if self._pw is not None:
                await self._pw.close()
                self._pw = None
        except Exception as error:
            logger.warning("长程记忆：关闭浏览器内核失败：%s", str(error)[:120])
        try:
            self._unregister_page_routes()
        except Exception as error:
            logger.warning("长程记忆：注销页面路由失败：%s", str(error)[:120])
        try:
            if self.interactions:
                await self.interactions.terminate()
        except Exception as error:
            logger.warning("长程记忆：关闭限流器失败：%s", str(error)[:120])
        # stop the batch timers / workers first so they do not touch storage next
        try:
            for key in list(getattr(self, "_batch_timers", {}) or {}):
                self._drop_batch(key)
        except Exception:
            pass
        try:
            for key in list(getattr(self, "_wake_timers", {}) or {}):
                timer = self._wake_timers.pop(key, None)
                if timer is not None and not timer.done():
                    timer.cancel()
            self._wake_buffers.clear()
            self._wake_first_at.clear()
        except Exception:
            pass
        try:
            for task in tuple(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        except Exception as error:
            logger.warning("长程记忆：取消后台任务失败：%s", str(error)[:120])
        try:
            if self.media:
                await self.media.close()
        except Exception as error:
            logger.warning("长程记忆：关闭媒体归档失败：%s", str(error)[:120])
        try:
            if self.compression and self.storage:
                for scope_id in list(dict.fromkeys((getattr(self, "_known_scopes", {}) or {}).values())):
                    if scope_id:
                        await self._compress_scope(scope_id, rounds=1)
        except Exception as error:
            logger.warning("长程记忆：退出前压缩失败：%s", str(error)[:120])
        # The database MUST close: this is what lets the host delete the data dir.
        try:
            if self._extensions is not None:
                self._extensions.save()
        except Exception as error:
            logger.warning("长程记忆：保存拓展状态失败：%s", str(error)[:120])
        try:
            if self.mood:
                self.mood.save()
                logger.info("长程记忆：情绪已落盘")
        except Exception as error:
            logger.warning("长程记忆：保存情绪失败：%s", str(error)[:120])
        if self.storage:
            try:
                await self.storage.close()
                logger.info("长程记忆：数据库已关闭")
            except Exception as error:
                logger.warning("长程记忆：关闭数据库失败：%s", str(error)[:200])

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        if self._stopping:
            return
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error:
            # include the message: a bare exception name is unactionable
            logger.warning(
                "长程记忆后台任务失败: %s: %s",
                type(error).__name__, str(error)[:400],
            )

    async def _task_error(self, error: BaseException) -> None:
        logger.warning(
            "自主互动任务失败: %s: %s", type(error).__name__, str(error)[:400]
        )

    def _ready(self) -> bool:
        return all((self.storage, self.ingest, self.ledger, self.retrieval, self.context_builder))

    def _ready_qq(self) -> bool:
        return self.settings.enable_qq_tools

    def _target_group(self, event: AstrMessageEvent) -> bool:
        return (
            event.get_platform_name() == "aiocqhttp"
            and self.settings.allows_group(event.get_group_id())
        )

    def _conversation_key(self, event: AstrMessageEvent) -> str:
        group = str(event.get_group_id() or "")
        return group if group else f"private:{event.get_sender_id()}"

    def _is_private_chat(self, event: AstrMessageEvent) -> bool:
        return not str(event.get_group_id() or "")

    def _is_managed_conversation(self, event: AstrMessageEvent) -> bool:
        if event.get_platform_name() != "aiocqhttp":
            return False
        if self._is_private_chat(event):
            return self.settings.enable_private_memory
        return self.settings.allows_group(event.get_group_id())

    def _send_lock(self, scope_id: str) -> asyncio.Lock:
        lock = self._send_locks.get(scope_id)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[scope_id] = lock
        return lock

    def _run_reply_job(
        self,
        event: AstrMessageEvent,
        scope_id: str,
        message_id: str,
        permit: InteractionPermit,
        context: str,
        decision: Decision,
        image_only: bool,
        image_urls: list[str] | None,
    ):
        async def job() -> None:
            await self._run_reply(
                event, scope_id, message_id, permit, context, decision,
                image_only, image_urls,
            )

        return job()

    def _buffer_batch_message(
        self, key: str, event: AstrMessageEvent, stored: Any, image_only: bool
    ) -> None:
        """Hold a non-wake group message; judge the whole batch once later."""
        item = _BatchedMessage(
            event=event,
            scope_id=stored.scope_id,
            message_id=stored.message_id,
            image_only=image_only,
            text=str(stored.text or ""),
            sender_name=str(stored.sender_name or ""),
            occurred_at=str(stored.occurred_at or ""),
        )
        buffer = self._batch_buffers.setdefault(key, [])
        buffer.append(item)
        if len(buffer) >= self.settings.batch_max_messages:
            self._flush_batch(key, "攒满")
            return
        timer = self._batch_timers.get(key)
        if timer is None or timer.done():
            low = self.settings.batch_window_min_seconds
            high = max(self.settings.batch_window_max_seconds, low)
            task = asyncio.create_task(self._batch_timer(key, random.uniform(low, high)))
            self._batch_timers[key] = task
            self._tasks.add(task)
            task.add_done_callback(self._task_done)

    async def _batch_timer(self, key: str, window: float) -> None:
        try:
            await asyncio.sleep(window)
        except asyncio.CancelledError:
            return
        self._flush_batch(key, "窗口到期")

    def _flush_batch(self, key: str, reason: str) -> None:
        timer = self._batch_timers.pop(key, None)
        if timer is not None and not timer.done() and timer is not asyncio.current_task():
            timer.cancel()
        items = self._batch_buffers.pop(key, None)
        if not items:
            return
        logger.info("长程记忆：%s攒批%d条消息统一判定（%s）", key, len(items), reason)
        last = items[-1]
        self._enqueue_reply(
            key,
            self._handle_reply_flow(
                last.event, last.scope_id, last.message_id, last.image_only,
                wake=False, batch_items=items,
            ),
        )

    def _drop_batch(self, key: str) -> None:
        """A wake judgment sees the full latest context; the pending batch is redundant."""
        timer = self._batch_timers.pop(key, None)
        if timer is not None and not timer.done():
            timer.cancel()
        self._batch_buffers.pop(key, None)

    def _buffer_wake_message(
        self, key: str, event: AstrMessageEvent, stored: Any, image_only: bool
    ) -> None:
        """@/私聊连发先并入合并窗口：像真人一样一起读完、一次回。

        防抖语义——每条新消息把开口时机往后推（对方可能还在打字）；
        上限语义——从第一条唤醒消息起最多等 wake_merge_max_seconds，防止
        一直连发导致永远不回。攒满 batch_max_messages 立即开口。
        """
        # 已有非唤醒攒批 → 整体并入唤醒缓冲（唤醒判定本就覆盖全部最新上下文）
        pending = self._batch_buffers.pop(key, None) or []
        if pending:
            timer = self._batch_timers.pop(key, None)
            if timer is not None and not timer.done():
                timer.cancel()
        item = _BatchedMessage(
            event=event,
            scope_id=stored.scope_id,
            message_id=stored.message_id,
            image_only=image_only,
            text=str(stored.text or ""),
            sender_name=str(stored.sender_name or ""),
            occurred_at=str(stored.occurred_at or ""),
        )
        buffer = self._wake_buffers.setdefault(key, [])
        buffer.extend(pending)
        buffer.append(item)
        if len(buffer) >= self.settings.batch_max_messages:
            self._flush_wake(key, "攒满")
            return
        now = time.monotonic()
        first = self._wake_first_at.setdefault(key, now)
        if now - first >= self.settings.wake_merge_max_seconds:
            self._flush_wake(key, "合并窗口到上限")
            return
        timer = self._wake_timers.get(key)
        if timer is not None and not timer.done():
            timer.cancel()
        task = asyncio.create_task(
            self._wake_timer(key, self.settings.wake_merge_seconds))
        self._wake_timers[key] = task
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    async def _wake_timer(self, key: str, window: float) -> None:
        try:
            await asyncio.sleep(window)
        except asyncio.CancelledError:
            return
        self._flush_wake(key, "合并窗口静默")

    def _flush_wake(self, key: str, reason: str) -> None:
        timer = self._wake_timers.pop(key, None)
        if timer is not None and not timer.done() and timer is not asyncio.current_task():
            timer.cancel()
        self._wake_first_at.pop(key, None)
        items = self._wake_buffers.pop(key, None)
        if not items:
            return
        logger.info(
            "长程记忆：%s合并%d条唤醒消息统一回复（%s）", key, len(items), reason)
        last = items[-1]
        self._enqueue_reply(
            key,
            self._handle_reply_flow(
                last.event, last.scope_id, last.message_id, last.image_only,
                wake=True, batch_items=items,
            ),
        )

    def _enqueue_reply(self, key: str, job: Any) -> None:
        if not self._reply_queue_enabled:
            self._spawn(job)
            return
        queue = self._reply_queues.setdefault(key, asyncio.Queue())
        queue.put_nowait(job)
        worker = self._reply_workers.get(key)
        if worker is None or worker.done():
            task = asyncio.create_task(self._reply_worker(key, queue))
            self._reply_workers[key] = task
            self._tasks.add(task)
            task.add_done_callback(self._task_done)

    async def _reply_worker(self, key: str, queue: asyncio.Queue) -> None:
        while True:
            job = await queue.get()
            try:
                await job
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("长程记忆：%s回复队列任务失败：%s", key, str(error)[:200])
            finally:
                queue.task_done()

    def _shared_scope_ids(self, current: str | None = None) -> tuple[str, ...]:
        """Memory-sharing set: whitelisted-group and private scopes share one
        memory space (摘要/账本/印象跨群，每条带 origin 出处标注). Raw chat
        records stay per-scope — ContextBuilder never injects other groups'
        messages, only their distilled memory with origin labels. The plugin's
        own traces (web browsing / studio designs & programs) count as the
        bot's personal experience and always participate."""
        groups: list[str] = []
        privates: list[str] = []
        others: list[str] = []
        for key, scope_id in self._known_scopes.items():
            if key.startswith("private:"):
                if self.settings.enable_private_memory:
                    privates.append(scope_id)
                continue
            if key in {"web", "studio"}:
                others.append(scope_id)
                continue
            if self.settings.allows_group(key):
                groups.append(scope_id)
        # 顺序即优先级：当前会话 → 白名单群（群聊是主场景）→ 私聊 → 插件自身痕迹。
        # 原来按"首次出现顺序"截前 9 个，群一多就会有群被无声挤出去（实录：记忆"没互通"）。
        ordered = ([current] if current else []) + groups + privates + others
        unique: list[str] = []
        for scope_id in ordered:
            if scope_id and scope_id not in unique:
                unique.append(scope_id)
        return tuple(unique[:16])

    async def _scope_for_event(self, event: AstrMessageEvent, create: bool = False) -> str | None:
        if not self.storage or not self._is_managed_conversation(event):
            return None
        account = str(event.get_self_id() or "")
        conversation = self._conversation_key(event)
        if create:
            return await self.storage.get_or_create_scope("aiocqhttp", account, conversation)
        return await self.storage.resolve_scope("aiocqhttp", account, conversation)

    def _known_providers(self) -> list[Any]:
        """Currently registered chat providers (for dead-config detection)."""
        getter = getattr(self.context, "get_all_providers", None)
        if callable(getter):
            try:
                return list(getter() or [])
            except Exception:
                return []
        return []

    async def _resolve_provider(self, provider_id: str, event: AstrMessageEvent | None) -> str:
        """解析真实可用的 provider id：候选链（调用方指定→会话偏好→全局默认）
        逐一对照 AstrBot 注册表，指向已删除模型时自动落到还活着的候选。

        实录：配置/会话偏好/全局默认都还指向已删除的 deepseek 时，旧的单步回退
        （回退目标="当前主模型"）会被 provider_id == live_id 短路，永远重试死模型。
        """
        known = {
            str((getattr(t, "provider_config", None) or {}).get("id", ""))
            for t in self._known_providers()
        }
        known.discard("")
        provider_id = str(provider_id or "").strip()
        if not known:
            return provider_id  # 注册表不可用时保持原样（交由调用报错）
        candidates: list[str] = [provider_id]
        try:
            if event is not None:
                candidates.append(await self._current_provider(event))
        except Exception:
            pass
        # 无事件的自主调用（闲时/日程/心跳）没有会话偏好层：插件配置的回复模型
        # 应优先于 AstrBot 全局默认——实录：全局默认停在被删的旧模型上，闲时动作
        # 全部 5/5 重试失败（"Provider catapi/deepseek-v4.1-flash not found"）
        candidates.append(str(self.settings.reply_provider_id or ""))
        try:
            provider = await self.context.get_using_provider_async()
            if provider:
                candidates.append(str(getattr(provider.meta(), "id", "") or ""))
        except Exception:
            pass

        async def _resolvable(candidate: str) -> bool:
            """候选是否真能被 AstrBot 取到实例——注册表(get_all_providers)可能仍列出
            已失效条目（实录：全局默认指向的旧模型在注册表里还在，llm_generate 查找时
            却报 "Provider not found"）。取不到/查询异常都不额外否决。"""
            if not candidate:
                return False
            manager = getattr(self.context, "provider_manager", None)
            getter = getattr(manager, "get_provider_by_id", None) if manager else None
            if callable(getter):
                try:
                    result = getter(candidate)
                    if inspect.isawaitable(result):
                        result = await result
                    return result is not None
                except Exception:
                    return True
            sync_getter = getattr(self.context, "get_provider_by_id", None)
            if callable(sync_getter):
                try:
                    return sync_getter(candidate) is not None
                except Exception:
                    return True
            return True

        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if (candidate and candidate in known
                    and candidate not in self._dead_providers
                    and await _resolvable(candidate)):
                if candidate != provider_id and candidate not in self._provider_fallback_warned:
                    logger.warning(
                        "长程记忆：模型 %s 已不存在，自动改用可用模型 %s",
                        provider_id[:40], candidate[:40],
                    )
                    self._provider_fallback_warned.add(provider_id)
                return candidate
        live = sorted(known - self._dead_providers) or sorted(known)
        fallback = live[0]
        for option in live:
            if await _resolvable(option):
                fallback = option
                break
        if provider_id not in self._provider_fallback_warned:
            logger.warning(
                "长程记忆：模型 %s 已不存在且候选均不可用，改用注册模型 %s",
                provider_id[:40], fallback[:40],
            )
            self._provider_fallback_warned.add(provider_id)
        return fallback

    @staticmethod
    def _looks_like_prompt_blocked(error: BaseException) -> bool:
        """模型侧内容审核拦截：重试也不会通过，必须立刻停。"""
        text = str(error).lower()
        return ("prompt_blocked" in text or "prohibited_content" in text
                or "content_filter" in text or "内容审核" in text
                or "request blocked by model service" in text)

    @staticmethod
    def _looks_like_provider_missing(error: Exception) -> bool:
        """上游通道已删除/未注册/凭据失效类错误——重试同模型没有意义。"""
        text = str(error).lower()
        return any(token in text for token in (
            "not found", "不存在", "no such model", "unknown model",
            "no active credentials",
        ))

    async def _alternate_provider(self, current: str) -> str:
        """换一个与 current 不同、且不在拉黑名单里的候选模型。"""
        candidates: list[str] = []
        try:
            provider = await self.context.get_using_provider_async()
            if provider:
                candidates.append(str(getattr(provider.meta(), "id", "") or ""))
        except Exception:
            pass
        for field_name in (
            "reply_provider_id", "judge_provider_id", "summary_provider_id",
            "vision_provider_id", "subagent_provider_id",
        ):
            candidates.append(str(getattr(self.settings, field_name, "") or ""))
        for candidate in candidates:
            candidate = candidate.strip()
            if candidate and candidate != current and candidate not in self._dead_providers:
                return candidate
        return ""

    async def _current_provider(self, event: AstrMessageEvent) -> str:
        return await self.context.get_current_chat_provider_id(
            event.unified_msg_origin or ""
        )

    async def _llm_text(
        self,
        provider_id: str,
        *,
        prompt: str,
        system_prompt: str,
        event: AstrMessageEvent | None = None,
        tools: ToolSet | None = None,
        agent: bool = False,
        image_urls: list[str] | None = None,
        max_steps: int | None = None,
    ) -> str:
        if not provider_id:
            if event is not None:
                provider_id = await self._current_provider(event)
            else:
                provider = await self.context.get_using_provider_async()
                provider_id = str(getattr(provider.meta(), "id", "") or "") if provider else ""
        provider_id = await self._resolve_provider(provider_id, event)
        if not provider_id:
            logger.warning(
                "长程记忆：没有可用的聊天Provider（请在插件配置选择判定/回复模型，或在AstrBot启用默认模型）"
            )
            raise RuntimeError("没有可用的聊天 Provider")
        max_attempts = 5
        deadline = asyncio.get_running_loop().time() + self.settings.llm_timeout_seconds
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0.5:
                break
            try:
                async with asyncio.timeout(remaining):
                    if agent:
                        if event is None:
                            raise RuntimeError("Agent 调用需要当前事件")
                        response = await self.context.tool_loop_agent(
                            event=event,
                            chat_provider_id=provider_id,
                            prompt=prompt,
                            system_prompt=system_prompt,
                            tools=tools,
                            image_urls=image_urls,
                            max_steps=max_steps or self.settings.agent_max_steps,
                            tool_call_timeout=self.settings.tool_timeout_seconds,
                        )
                    else:
                        response = await self.context.llm_generate(
                            chat_provider_id=provider_id,
                            prompt=prompt,
                            system_prompt=system_prompt,
                            tools=tools,
                            image_urls=image_urls,
                        )
                raw_text = getattr(response, "completion_text", None)
                text = raw_text if isinstance(raw_text, str) else (
                    "" if raw_text is None else str(raw_text))
                if text.strip():
                    self._record_usage(response)
                    return text
                # 空输出（实录：mimo-v2.6-flash 偶发 content=None、0 completion tokens，
                # AstrBot 日志"returned empty output"）——非 agent 路径无副作用，可重试；
                # agent 路径绝不能重跑（会重复执行工具副作用，比如重复发消息），
                # 空文本直接交调用方兜底（parse_json_object("")→None→{}）。
                if agent:
                    logger.warning("长程记忆：Agent 最终输出为空，交调用方兜底")
                    return text
                last_error = RuntimeError("LLM返回空输出")
                logger.warning(
                    "长程记忆：LLM 返回空输出（第%d/%d次尝试）", attempt, max_attempts,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** (attempt - 1), 8))
                    continue
                return text
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise TimeoutError(
                    f"LLM总超时（{self.settings.llm_timeout_seconds}s，含全部重试）"
                ) from None
            except Exception as error:
                last_error = error
                # 内容审核拦截（400 prompt_blocked / PROHIBITED_CONTENT）：重试毫无意义，
                # 只会连着烧 5 轮 token（实录：同一条被拦 3 次以上）。直接放弃这一轮。
                if self._looks_like_prompt_blocked(error):
                    logger.warning(
                        "长程记忆：请求被模型侧内容审核拦截，本轮不再重试：%s",
                        str(error)[:160],
                    )
                    raise RuntimeError(f"内容审核拦截：{str(error)[:120]}") from error
                # 上游已删/未注册的模型（实录 "Provider catapi/xxx not found" 烧满
                # 5 次重试）：拉黑 + 立刻换候选模型重试
                if self._looks_like_provider_missing(error):
                    self._dead_providers.add(provider_id)
                    alternative = await self._alternate_provider(provider_id)
                    if alternative:
                        logger.warning(
                            "长程记忆：模型 %s 不可用（%s），改用 %s 重试",
                            provider_id[:40], str(error)[:80], alternative[:40],
                        )
                        provider_id = alternative
                        continue
                    logger.warning(
                        "长程记忆：模型 %s 不可用且没有别的候选（%s）",
                        provider_id[:40], str(error)[:80],
                    )
                logger.warning(
                    "长程记忆：LLM调用失败（第%d/%d次尝试）：%s",
                    attempt, max_attempts, str(error)[:200],
                )
                if attempt < max_attempts:
                    await asyncio.sleep(min(2 ** (attempt - 1), 8))
        raise RuntimeError(f"LLM重试{max_attempts}次后仍失败：{last_error}")

    async def _summary_llm(self, system_prompt: str, prompt: str) -> str:
        provider = self.settings.summary_provider_id or self.settings.reply_provider_id
        return await self._llm_text(provider, prompt=prompt, system_prompt=system_prompt)

    def _record_usage(self, response: Any) -> None:
        """记录一次 LLM 调用（openclaw usage-tracking 移植）：调用数 + token 数。"""
        if self.storage is None or self._stopping:
            return
        raw = getattr(response, "raw_completion", None)
        usage = getattr(raw, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        try:
            self._spawn(self.storage.add_usage(1, prompt_tokens, completion_tokens))
        except Exception:
            pass

    async def _judge_llm(self, prompt: str) -> str:
        provider = self.settings.judge_provider_id or self.settings.reply_provider_id
        return await self._llm_text(
            provider, prompt=prompt, system_prompt=DECISION_SYSTEM_PROMPT
        )

    async def _embedding_adapter(self) -> Any | None:
        wanted = self.settings.embedding_provider_id
        if not wanted:
            return None
        getter = getattr(self.context, "get_all_embedding_providers", None)
        if not callable(getter):
            return None
        for provider in getter():
            meta = provider.meta()
            if str(getattr(meta, "id", "")) == wanted:
                return _AstrBotEmbeddingAdapter(provider)
        logger.warning("配置的 Embedding Provider 不可用，已降级为全文检索")
        return None

    async def _audit_callback(self, record: dict[str, Any]) -> None:
        if self.storage and hasattr(self.storage, "write_audit"):
            await self.storage.write_audit(record)

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.AIOCQHTTP,
        priority=20,
    )
    async def on_onebot_event(self, event: AstrMessageEvent):
        if not self._still_current_instance():
            return
        self._refresh_settings_if_changed()
        raw = normalize_raw_event(event)
        if is_request_event(raw):
            if self.settings.auto_handle_requests and self.storage and self._ready():
                record = normalize_request_record(raw)
                if record is not None:
                    stored = await self.ingest.ingest(record)
                    self._known_scopes.setdefault(record.conversation_id, stored.scope_id)
                    logger.info("长程记忆：%s已记录，稍后唤醒LLM处理", record.text[:60])
                    self._schedule_event_processing()
            return
        if not self._is_managed_conversation(event) or not self._ready():
            return
        if raw.get("post_type") == "notice" and not is_poke_notice(raw):
            notice = normalize_notice_event(raw)
            if notice is not None and self.ingest:
                if notice.conversation_id.startswith("private:") and not self.settings.enable_private_memory:
                    return
                if not notice.conversation_id.startswith("private:") and not self.settings.allows_group(notice.conversation_id):
                    return
                stored = await self.ingest.ingest(notice)
                self._known_scopes.setdefault(notice.conversation_id, stored.scope_id)
                self._spawn(self._compress_if_ready(stored.scope_id))
                self._schedule_event_processing()
            return
        if is_poke_notice(raw):
            if not self._is_private_chat(event) and self.settings.allows_group(event.get_group_id()):
                await self._handle_poke(event, raw)
            return
        normalized = normalize_event(event)
        if normalized is None or normalized.event_type != "message.created":
            return
        text = normalized.text.strip()
        # 原版兼容（origin 拓展）：这些前缀的消息不判定、不拦截，
        # 直接交给 AstrBot 的原生命令系统（含全部已装插件的命令处理器）
        if any(text.startswith(prefix) for prefix in self._origin_prefixes() if prefix):
            return
        assert self.ingest and self.retrieval and self.interactions
        stored = await self.ingest.ingest(normalized)
        self._known_scopes[self._conversation_key(event)] = stored.scope_id
        is_self_message = str(stored.sender_id) == str(event.get_self_id())
        # 情绪系统 v2：每条用户消息产生一个确定性情绪增量（效价/唤醒轴）
        if self.mood is not None and not is_self_message and stored.text:
            try:
                from .src.emotions import classify_interaction

                delta = classify_interaction(stored.text)
                if delta is not None:
                    self.mood.observe(delta)
            except Exception:
                pass
        if self.retrieval.embedding and stored.text:
            self._spawn(self.retrieval.index_embedding(stored.scope_id, stored))
        self._spawn(self._compress_if_ready(stored.scope_id))
        if self.settings.sticker_learning:
            self._spawn(self._learn_stickers(event, raw, stored.message_id))
        self._ensure_gateway(event)
        # 合并转发：消息本体带 forward 段时（NapCat 段类型有 forward/forwardtransfer/
        # flashtransfer 变体），立刻展开内容入同一 scope 的查询记忆，并留出判定同步展开。
        # 按会话累积——攒批时逐条覆盖会丢掉先到的那条转发（实录"读不了合并转发"）。
        forward_ids = self._forward_ids(raw)
        if forward_ids:
            self._spawn(self._ingest_forward_notes(raw, stored.scope_id))
            bucket = self._pending_forwards.setdefault(
                self._conversation_key(event), [])
            for fid in forward_ids:
                if fid not in bucket:
                    bucket.append(fid)
            del bucket[3:]  # 只留最近的 3 个，防止老转发无限堆积
        if self.media:
            self._spawn(self._archive_media_then_note(
                event, raw, stored.scope_id, stored.message_id, stored.text
            ))
        image_only = _is_image_only(raw, text)
        if is_self_message:
            return
        is_wake = bool(getattr(event, "is_at_or_wake_command", False)) or self._is_private_chat(event)
        if is_wake:
            logger.info(
                "长程记忆：%s收到@/唤醒/私聊消息，进入统一判定",
                self._conversation_key(event),
            )
            # 合并窗口期间必须立刻拦下默认管线：否则每条@都会被 AstrBot 原生
            # LLM 再回一遍（窗口期双回复）。关闭合并时保持老契约——判定失败
            # 不拦截，交给 AstrBot 默认管线兜底。
            if self.settings.wake_merge_seconds > 0:
                event.stop_event()
                # 真人节奏：连发的@/私聊一起读完、一次回（v0.36.1 实录三连@回三次太机械）
                self._buffer_wake_message(
                    self._conversation_key(event), event, stored, image_only)
                return
            self._drop_batch(self._conversation_key(event))
            # Judge once here: `_handle_reply_flow` would judge a second time,
            # doubling the LLM cost on every private/@ message.
            prepared = await self._prepare_reply(
                event, stored.scope_id, stored.message_id, image_only, wake=True
            )
            if prepared is None:
                return
            permit, context, decision, urls = prepared
            self._enqueue_reply(
                self._conversation_key(event),
                self._run_reply(
                    event, stored.scope_id, stored.message_id, permit, context,
                    decision, image_only, urls,
                ),
            )
            return
        if not text:
            return
        if random.random() > self.settings.autonomous_sample_rate:
            logger.info(
                "长程记忆：%s消息未采样进入判定（autonomous_sample_rate=%.2f）",
                self._conversation_key(event), self.settings.autonomous_sample_rate,
            )
            return
        logger.info("长程记忆：%s消息进入攒批判定缓冲", self._conversation_key(event))
        dedupe_key = f"{stored.scope_id}:{stored.message_id}"
        if dedupe_key in self._processed_messages:
            return
        self._processed_messages.add(dedupe_key)
        self._buffer_batch_message(
            self._conversation_key(event), event, stored, image_only
        )

    @filter.on_waiting_llm_request(priority=20)
    async def prefetch_default_llm_context(self, event: AstrMessageEvent) -> None:
        if not self._target_group(event) or not self.context_builder:
            return
        scope = await self._scope_for_event(event)
        if not scope:
            return
        try:
            async with asyncio.timeout(2.0):
                memory = await self.context_builder.build(
                    scope,
                    event.get_message_str(),
                    recent_limit=self.settings.recent_message_limit,
                    catalog_limit=self.settings.catalog_limit,
                    summary_limit=self.settings.summary_limit,
                    evidence_limit=self.settings.evidence_limit,
                    cross_scope_ids=self._shared_scope_ids(scope),
                    cross_labels={v: k for k, v in self._known_scopes.items()},
                )
            event.set_extra(_PREFETCH_KEY, memory)
        except Exception as error:
            logger.warning("长程记忆预取失败，默认对话继续: %s", type(error).__name__)

    @filter.on_llm_request(priority=20)
    async def inject_default_llm_context(self, event: AstrMessageEvent, req: Any) -> None:
        if not self._target_group(event) or event.get_extra(_SKIP_KEY):
            return
        manifest = await build_runtime_manifest(self.context, req, event=event)
        memory = event.get_extra(_PREFETCH_KEY)
        if not memory and self.context_builder:
            scope = await self._scope_for_event(event)
            if scope:
                try:
                    async with asyncio.timeout(1.5):
                        memory = await self.context_builder.build(
                            scope, event.get_message_str(), runtime_manifest=manifest,
                            cross_scope_ids=self._shared_scope_ids(scope),
                            cross_labels={v: k for k, v in self._known_scopes.items()},
                        )
                except Exception:
                    memory = None
        payload = compact_json(
            {"runtime_manifest": manifest, "retrieved_memory": parse_json_object(memory or "{}") or {}},
            self.settings.context_char_budget,
        )
        if MEMORY_TRUST_POLICY not in req.system_prompt:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + MEMORY_TRUST_POLICY
        req.extra_user_content_parts.append(TextPart(text=payload).mark_as_temp())

    async def _compress_if_ready(self, scope_id: str) -> None:
        """Cheap gate: only reacts every `compression_batch_size` messages.

        The real work happens in `_compress_scope` so a restart never strands
        unwritten L1 chunks (the counter is just a throttle, not a threshold).
        """
        if not self.compression or not self.storage:
            return
        self._pending_counts[scope_id] = self._pending_counts.get(scope_id, 0) + 1
        if self._pending_counts[scope_id] < self.settings.compression_batch_size:
            return
        self._pending_counts[scope_id] = 0
        await self._compress_scope(scope_id)

    async def _compress_scope(self, scope_id: str, rounds: int = 3) -> int:
        """Drain pending messages into L1 chunks and roll them up.

        Runs until nothing is pending (bounded by `rounds`), so memory keeps
        forming even when a scope never accumulates a full batch.
        """
        if not self.compression or not self.storage:
            return 0
        created = 0
        batch = max(4, self.settings.compression_batch_size // 2)
        for _ in range(max(1, rounds)):
            try:
                pending = await self.storage.pending_l1_messages(scope_id, batch)
            except Exception as error:
                logger.warning("长程记忆：读取待压缩消息失败：%s", error)
                break
            if not pending:
                break
            try:
                await self.compression.compress_pending_l1(scope_id, batch)
                created += 1
            except Exception as error:
                logger.warning("长程记忆：L1 压缩失败：%s", error)
                break
        try:
            await self.compression.roll_up(scope_id, 2)
        except Exception as error:
            logger.warning("长程记忆：L2/L3 聚合失败：%s", error)
        if created:
            logger.info("长程记忆：%s 分层压缩产出%d个 L1", scope_id[:8], created)
        return created

    async def _compress_all_scopes(self) -> None:
        """Heartbeat hook: keep every known scope's memory layers current."""
        if not self.compression or not self.storage:
            return
        for scope_id in list(dict.fromkeys(self._known_scopes.values())):
            if not scope_id:
                continue
            try:
                await self._compress_scope(scope_id, rounds=2)
            except Exception as error:
                logger.warning("长程记忆：记忆压缩巡检失败：%s", str(error)[:150])

    async def _handle_reply_flow(
        self,
        event: AstrMessageEvent,
        scope_id: str,
        message_id: str,
        image_only: bool = False,
        wake: bool = False,
        image_urls: list[str] | None = None,
        batch_items: list[_BatchedMessage] | None = None,
    ) -> None:
        """One shared pipeline for @/wake and autonomous messages."""
        prepared = await self._prepare_reply(
            event, scope_id, message_id, image_only, wake, batch_items=batch_items
        )
        if prepared is None:
            return
        permit, context, decision, urls = prepared
        await self._run_reply(
            event, scope_id, message_id, permit, context, decision, image_only, urls,
            batch_mode=bool(batch_items),
        )

    async def _prepare_reply(
        self,
        event: AstrMessageEvent,
        scope_id: str,
        message_id: str,
        image_only: bool,
        wake: bool,
        batch_items: list[_BatchedMessage] | None = None,
    ) -> tuple[Any, str, Decision, list[str] | None] | None:
        """Pre-lock phase: limits, context building, judging."""
        if batch_items and any(item.text for item in batch_items):
            image_only = False
        if not self.context_builder or not self.decision or not self.interactions:
            if wake:
                event.stop_event()
            return None
        if time.monotonic() < self._send_blockade.get(scope_id, 0.0):
            logger.info(
                "长程记忆：%s处于风控封锁期，跳过本次回复", self._conversation_key(event)
            )
            if wake:
                event.stop_event()
            return None
        permit = await self.interactions.begin(
            scope_id, str(event.get_group_id()), str(event.get_sender_id()),
            bypass_quiet=wake, supersede=False,
        )
        if permit is None:
            logger.info(
                "长程记忆：群%s消息被限流跳过（静默时段/群或用户冷却/小时限额）",
                event.get_group_id(),
            )
            if wake:
                event.stop_event()
            return None
        mood_data = self.mood.get().as_dict() if self.mood else {}
        try:
            affinity = await self.storage.get_affinity(
                scope_id, str(event.get_sender_id())
            )
        except Exception:
            affinity = {}
        context = await self.context_builder.build(
            scope_id,
            event.get_message_str(),
            recent_limit=self.settings.recent_message_limit,
            runtime_manifest={
                "platform": "aiocqhttp",
                "group_id": str(event.get_group_id()),
                "mood": mood_data,
            },
            cross_scope_ids=self._shared_scope_ids(scope_id),
            cross_labels={v: k for k, v in self._known_scopes.items()},
            affinity=affinity,
        )
        context_data = parse_json_object(context) or {}
        merged_affinity = affinity
        try:
            # 一个人的好感度跨群只有一份（写仍分群，读一律合并）
            merged_affinity = await self.storage.get_affinity_any(
                self._shared_scope_ids(scope_id), str(event.get_sender_id()))
        except Exception:
            merged_affinity = affinity
        context_data["sender_affinity"] = {
            "user_id": str(event.get_sender_id()),
            **merged_affinity,
            "hint": "warmth是你对TA的好感度（0-100），影响你的态度和回复意愿，但你仍可自主决定",
        }
        try:
            # 同一个人只有一个印象：跨群合并（实录"人物印象每个群不互通"）
            sender_impression = await self.storage.get_impression_any(
                self._shared_scope_ids(scope_id), str(event.get_sender_id())
            )
        except Exception:
            sender_impression = {}
        if sender_impression:
            sender_impression["hint"] = (
                "impression是TA给你留下的印象、tags是TA的特点，用来自然地调整称呼和语气；"
                "觉得过时了就 update_impression"
            )
            context_data["sender_impression"] = sender_impression
        try:
            sender_style = await self.storage.get_style(
                scope_id, str(event.get_sender_id())
            )
        except Exception:
            sender_style = {}
        if sender_style and sender_style.get("style_summary"):
            sender_style["hint"] = (
                "模仿TA的惯用词、句长、语气，自然融入回复；"
                "别逐句照搬、别夸张成模仿秀"
            )
            context_data["sender_style"] = sender_style
        # 技术类问题（数学/信息学）：强制先查知识库，把结果注进上下文
        tech_enriched = False
        for candidate in (
            [event.get_message_str()]
            + [item.text for item in (batch_items or [])]
        ):
            enrichment = await self._knowledge_enrichment(candidate or "")
            if enrichment is not None:
                context_data.update(enrichment)
                tech_enriched = True
                break
        # 合并转发：判定前同步展开内容，否则模型只看到"[合并转发]"占位符。
        # 来源三处：当前消息本体、攒批里各条消息的事件、到达时登记的待展开表
        # （非唤醒消息的转发先缓着，判定时才展开——逐条覆盖会丢先到的那条）
        forward_ids: list[str] = []
        for candidate_raw in (
            [normalize_raw_event(event)]
            + [normalize_raw_event(item.event) for item in (batch_items or [])[:5]]
        ):
            try:
                for fid in self._forward_ids(candidate_raw):
                    if fid not in forward_ids:
                        forward_ids.append(fid)
            except Exception:
                continue
        conv_key = self._conversation_key(event)
        for fid in self._pending_forwards.get(conv_key, []):
            if fid not in forward_ids:
                forward_ids.append(fid)
        self._pending_forwards.pop(conv_key, None)
        # 承载消息自身的 id 也当候选：不同网关实现认的转发 id 形态不同
        # （res_id vs 消息 id），带上能显著提高老转发的展开成功率
        carrying_ids = [str(getattr(event.message_obj, "message_id", "") or "")]
        carrying_ids += [str(getattr(item.event.message_obj, "message_id", "") or "")
                         for item in (batch_items or [])[:3]]
        for fid in forward_ids[:3]:
            flattened = await self._fetch_forward_text(
                fid, extra_ids=carrying_ids, scope_id=scope_id)
            if flattened:
                logger.info(
                    "长程记忆：合并转发已展开（%s，%d 字）", fid[:12], len(flattened))
                self._spawn(self._remember_forward_text(scope_id, fid, flattened))
                context_data["forward_content"] = {
                    "forward_id": fid[:16],
                    "content": flattened,
                    "note": "这是消息/批里合并转发的完整内容，已按 谁说的：内容 排列",
                }
                tech_enriched = True
                break
            else:
                context_data["forward_failed"] = (
                    f"合并转发 {fid[:12]} 的正文这次没取到：先用 read_forward 工具"
                    "（传这一条的 message_id 或 forward_id）再试一次；还不行就如实"
                    "告诉对方这份转发读不出来，别编造内容，也别拿'过期'当借口反复说")
                tech_enriched = True
        # 引用消息（reply 段）：把被引用消息的文本/文件带进上下文。文件（PDF
        # 试卷等）自动读出正文节选——否则模型只看到 "[引用消息]" 占位，会对
        # 着被引用的试卷回"试卷呢，你又复读上了"（实测）。
        quoted_notes: list[dict[str, Any]] = []
        seen_refs: set[str] = set()
        for item_event in [event] + [item.event for item in (batch_items or [])][:3]:
            try:
                refs = self._reply_refs(normalize_raw_event(item_event))
            except Exception:
                refs = []
            for ref in refs[:2]:
                if ref in seen_refs or len(quoted_notes) >= 2:
                    continue
                seen_refs.add(ref)
                note = await self._quoted_digest(scope_id, ref)
                if note is not None:
                    quoted_notes.append(note)
            if len(quoted_notes) >= 2:
                break
        if quoted_notes:
            context_data["quoted_messages"] = quoted_notes
            forwarded = next(
                (note["forward"] for note in quoted_notes if note.get("forward")), "")
            if forwarded:
                context_data["forward_content"] = {
                    "content": forwarded,
                    "note": "这是被引用消息里的合并转发全文，已按 谁说的：内容 排列",
                }
            context_data["quoted_note"] = (
                "上面是用户这条消息引用的原消息：files[].excerpt 是文件（PDF/文档）"
                "已经读出来的正文节选，需要更多细节时用 read_document(media_id) 读全文；"
                "image_media_ids 是图片，可用 look_at_image 查看；forward 是被引用消息里"
                "合并转发的全文。直接基于这些内容回应——不要说看不到文件/转发、也不要让对方重发。"
            )
            tech_enriched = True
        # Standing intents（事件条件意图）：命中即注入，像 OpenClaw 一样
        # "当有人提到X时就做Y" 的前瞻记忆
        hits = await self._match_standing_intents(
            scope_id,
            [event.get_message_str()] + [item.text for item in (batch_items or [])],
        )
        if hits:
            context_data["standing_intents_hit"] = {
                "intents": hits,
                "note": ("这些是你之前立下的事件条件指令。判断：当前消息确实是在说这"
                         "件事→按指令行动并把结果告诉对方；只是字面撞词、语境无关→"
                         "忽略，别硬套。"),
            }
            tech_enriched = True
        if batch_items:
            # 批次以**聊天记录形态**给出（"昵称：内容"逐行），不再用对象数组——
            # 数组会诱导模型逐项作答（实录："窗口内一条一条详细回复"）
            lines = [
                f"{item.sender_name or item.event.get_sender_id()}：{item.text}"
                for item in batch_items if item.text
            ]
            context_data["batch_transcript"] = "\n".join(lines)
            context_data["batch_count"] = len(lines)
            context_data["batch_note"] = (
                "这是攒批判定：batch_transcript 是自你上次查看以来群里连着的几句话"
                "（聊天记录形态，旧→新），recent_messages 含它们与更早的证据和记忆。"
                "【批次纪律·硬性】整批**只回一次、只接一件事**：挑最值得接的那一句、"
                "或对整批做一个反应，其余全部略过。严禁逐条点评、严禁按顺序一一作答、"
                "严禁写「回复某某：…」式的清单，也不要总结这批消息——真人扫一眼聊天记录"
                "只会插一句话，不会给每条都补一句。没有值得接的话、纯刷屏寒暄、或你最近"
                "已回复过同样内容时直接 ignore。【对象纪律】消息按发送者区分，每个人只对"
                "自己的话负责——别把某位群友的话/委托安到另一位群友头上；你此前请某人发来"
                "的材料只认那个人发来的，其他人随后发的图/表情是TA们自己的消息，不得当成"
                "那份材料。未完成的旧任务也别硬接：这条消息与它无关就当它不存在，"
                "不许说「图糊了看不清题干」这类硬套旧任务的话。"
            )
        if batch_items:
            style_rows: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for item in batch_items:
                sid = str(item.event.get_sender_id())
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                try:
                    st = await self.storage.get_style(item.scope_id, sid)
                except Exception:
                    st = {}
                if st.get("style_summary"):
                    style_rows.append({
                        "user_id": sid,
                        "display_name": st.get("display_name", ""),
                        "style": st.get("style_summary"),
                        "catchphrases": st.get("catchphrases", []),
                    })
                if len(style_rows) >= 3:
                    break
            if style_rows:
                context_data["batch_styles"] = style_rows
                context_data["batch_styles_hint"] = (
                    "这批消息说话人们的风格档案；回复时自然吸收他们的用词和语气"
                )
        # 表情包库存：让判定/回复知道"我有表情可用"——不看库存的话 sticker 动作
        # 仿佛不存在，bot 会永远只发文字（实录"从不主动发表情"）。
        if self.stickers is not None:
            try:
                stock = self.stickers.stats()
                if stock.count:
                    samples = [
                        {
                            "sticker_id": entry["sticker_id"],
                            "about": str(entry.get("description") or entry.get("summary") or "")[:40],
                        }
                        for entry in self.stickers.catalog(limit=20)
                        if str(entry.get("description") or entry.get("summary") or "").strip()
                    ][:5]
                    context_data["sticker_inventory"] = {
                        "count": stock.count,
                        "samples": samples,
                        "hint": ("你的表情包库存有货：接梗/吐槽/卖萌/被逗笑时，用 sticker 动作"
                                 "直接丢一张，或 pick_sticker(query) 按含义挑一个 sticker_id "
                                 "再用 send_sticker 发。"),
                    }
            except Exception:
                pass
        # 任务表：未完成的任务持续注入（hermes todo 的"压缩后重新注入"语义，
        # 跨轮不忘），让多步工作不断线
        if self.storage is not None:
            try:
                active_todos = [
                    item for item in await self.storage.list_todos(scope_id)
                    if item["status"] in {"pending", "in_progress"}
                ]
                # 新鲜度门槛：超过 24h 没动的任务不再每轮提醒——跨天旧任务是
                # "任务串"的头号来源（昨天的读卷任务套到今天的日常消息上）
                fresh = [
                    item for item in active_todos
                    if _within_hours(item.get("updated_at"), 24)
                ]
                if fresh:
                    context_data["active_todos"] = {
                        "todos": fresh[:10],
                        "hint": ("你自己备忘的任务清单（背景信息，不是命令）：只有当前消息"
                                 "明显是同一件事的继续（同一委托人续做/他说好的材料到了）"
                                 "才推进；日常闲聊、别人发的图片默认与它无关——不许把新"
                                 "图片当成旧任务的素材硬接。过期/不做了就用 todo clear 清掉。"),
                    }
            except Exception:
                pass
        # SSH 就绪时明确告知回复 agent——否则它会说「ip呢账号呢密码呢，一个没掏出来」
        # （实录：配置完好、工具可用，模型凭"没被告知"拒绝干活）
        if self._ssh_configured():
            ssh_cfg = self._ssh_config()
            context_data["ssh_ready"] = {
                "target": ssh_cfg.target(),
                "notes": (ssh_cfg.notes or "")[:200],
                "hint": ("远程服务器已配置好、连接信息你有：要操作它就直接 "
                         "ssh_exec(command) 执行；看环境用 ssh_info；多步运维可 "
                         "ssh_agent_dispatch；**把服务器上的文件/截图发到QQ用 "
                         "ssh_fetch(remote_path) 或 ssh_screenshot(url) 拉回媒体库"
                         "拿 media_id，再 send_image——ssh_exec 的输出传不了文件**。"
                         "绝不许说「没给ip/账号/密码」「不知道往哪连」——直接动手。"),
            }
            tech_enriched = True
        image_urls: list[str] = []
        try:
            if batch_items:
                pairs = []
                for item in batch_items:
                    try:
                        pairs.extend(
                            await self._message_images(
                                item.event, item.scope_id, item.message_id
                            )
                        )
                    except Exception:
                        continue
                    if len(pairs) >= 2:
                        break
                pairs = pairs[:2]
            else:
                pairs = await self._message_images(event, scope_id, message_id)
        except Exception:
            pairs = []
        if pairs:
            if self.settings.vision_provider_id:
                description = await self._describe_images(pairs, event=event)
                if description:
                    context_data["image_description"] = description
            else:
                image_urls = [f"data:{mime};base64,{b64}" for mime, b64 in pairs]
                context_data["note"] = (
                    context_data.get("note", "")
                    + " 本次消息附带图片，已直接提供给你查看。"
                ).strip()
        if image_only:
            context_data["note"] = (
                "当前消息是图片或表情，你看不到它的内容。除非最近对话强烈需要回应，"
                "否则选择 ignore 或 reaction（emoji_id 必须在白名单内）。"
            )
        if wake:
            context_data["attention"] = (
                "用户明确@/唤醒了你，正在直接和你说话。除非明显不适合，应当回应。"
            )
        if batch_items or image_only or wake or tech_enriched:
            context = compact_json(context_data, self.settings.context_char_budget)
        direct = not any(token in event.get_message_str() for token in ("?", "？", "他说", "建议"))
        admin = event.is_admin()
        if asyncio.iscoroutine(admin):
            admin = await admin
        decision = await self.decision.decide(
            context, is_admin=bool(admin), direct_request=direct
        )
        logger.info(
            "长程记忆：%s判定结果=%s",
            self._conversation_key(event), decision.action,
        )
        if wake:
            if decision.action == "ignore" and self.decision.last_failure:
                logger.warning(
                    "长程记忆：判定失败，回退AstrBot默认对话：%s",
                    self.decision.last_failure,
                )
                return
            event.stop_event()
        if decision.action == "ignore":
            return None
        return permit, context, decision, image_urls

    async def _run_reply(
        self,
        event: AstrMessageEvent,
        scope_id: str,
        message_id: str,
        permit: InteractionPermit,
        context: str,
        decision: Decision,
        image_only: bool,
        image_urls: list[str] | None,
        batch_mode: bool = False,
    ) -> None:
        lock = self._send_lock(scope_id)
        started = time.monotonic()
        async with lock:
            if not await self.interactions.wait_until_current(permit, decision.wait_seconds):
                logger.info(
                    "长程记忆：%s等待期间被终止，丢弃本次回复",
                    self._conversation_key(event),
                )
                return
            if not await self.interactions.commit(permit):
                logger.info("长程记忆：%s提交动作时未通过限额检查", self._conversation_key(event))
                return
            await self._execute_decision(
                event, scope_id, message_id, context, decision, permit, image_only,
                image_urls=image_urls, batch_mode=batch_mode,
            )
            try:
                nudge, why = self._affinity_nudge(text=str(stored.text or ""))
                if nudge:
                    await self.storage.adjust_affinity(
                        scope_id, str(event.get_sender_id()), nudge, note=why or None
                    )
            except Exception:
                pass
            try:
                await self.storage.upsert_impression(
                    scope_id, str(event.get_sender_id()),
                    display_name=event.get_sender_name() or None,
                )
            except Exception:
                pass
        # 技能自学习：本轮工作够重就安静复盘一次（hermes/openclaw 的学习闭环）
        try:
            self._maybe_schedule_skill_review(scope_id, time.monotonic() - started)
        except Exception:
            pass

    # ------------------------------------------------------- 技能自学习
    def _maybe_schedule_skill_review(self, scope_id: str, elapsed: float) -> None:
        """一段够重的会话工作结束后安排一次安静复盘（openclaw 的 experience review）。"""
        if not getattr(self.settings, "self_learning_enabled", False):
            return
        if self._scripts is None or self.storage is None:
            return
        if elapsed < max(10, int(self.settings.self_learning_min_seconds)):
            return
        now = time.monotonic()
        if now - self._last_skill_review.get(scope_id, -1e9) < 600:
            return
        if scope_id in self._skill_review_inflight:
            return
        self._last_skill_review[scope_id] = now
        self._skill_review_inflight.add(scope_id)
        self._spawn(self._skill_review(scope_id, elapsed))

    async def _skill_review(self, scope_id: str, elapsed: float) -> None:
        """体验复盘：让模型带证据找可复用流程 → 值得就固化成技能（写盘并热加载）。"""
        try:
            if not self._still_current_instance():
                return
            await asyncio.sleep(10)  # 安静期：别打断紧接着的对话
            if self.storage is None:
                return
            messages = await self.storage.recent_messages(scope_id, 40, include_events=True)
            if not messages:
                return
            known = [
                f"{ext.id}（{str(ext.manifest.get('description', ''))[:40]}）"
                for ext in self._script_manager().extensions.values()
                if getattr(ext, "origin", "") == "learned"
            ]
            evidence = build_evidence(
                messages, elapsed_seconds=elapsed, known_skills=known)
            provider = self.settings.summary_provider_id or self.settings.reply_provider_id
            raw = await self._llm_text(
                provider, prompt=evidence, system_prompt=SKILL_REVIEW_SYSTEM_PROMPT)
            proposal = parse_skill_proposal(raw)
            if proposal is None:
                return
            result = await apply_skill(self._script_manager(), proposal)
            if result.get("ok"):
                logger.info(
                    "长程记忆：自学习固化技能 %s（%d 个工具）",
                    result.get("id"), len(result.get("tools") or []))
                await self._studio_note(
                    f"[技能学习] {proposal['id']}：{proposal['description']}", "skill")
            else:
                logger.info("长程记忆：技能固化未通过：%s", str(result.get("error"))[:150])
        except Exception as error:
            logger.info("长程记忆：技能复盘跳过：%s", str(error)[:150])
        finally:
            self._skill_review_inflight.discard(scope_id)

    @staticmethod
    def _persona_fields(item: Any) -> tuple[str, str, str]:
        """Return (id, name, system_prompt) from a Persona object or mapping.

        system_prompt/prompt 字段可能是 None（Persona v3 对象/TypedDict）——
        str(None)='None' 会把字面 'None' 当提示词，必须先 or "" 再 str。
        """
        def _text(value: Any) -> str:
            if value is None or not isinstance(value, str):
                return ""
            return value

        if isinstance(item, Mapping):
            return (
                str(item.get("persona_id", "") or item.get("id", "") or item.get("name", "") or ""),
                _text(item.get("name")),
                _text(item.get("system_prompt") or item.get("prompt")),
            )
        return (
            str(getattr(item, "persona_id", "") or getattr(item, "id", "") or getattr(item, "name", "") or ""),
            _text(getattr(item, "name", None)),
            _text(getattr(item, "system_prompt", None) or getattr(item, "prompt", None)),
        )

    async def _persona_prompt(self) -> str:
        """Return the configured AstrBot persona's system prompt, or empty string."""
        wanted = str(self.settings.persona_id or "").strip()
        if not wanted:
            return ""
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            logger.warning("找不到人格管理器，自主回复使用内置拟人风格")
            return ""

        async def _all() -> list[Any]:
            for name in ("get_all_personas", "get_personas"):
                getter = getattr(manager, name, None)
                if callable(getter):
                    result = getter()
                    if inspect.isawaitable(result):
                        result = await result
                    return list(result or [])
            return []

        direct = getattr(manager, "get_persona", None)
        if callable(direct):
            try:
                persona = direct(wanted)
                if inspect.isawaitable(persona):
                    persona = await persona
                if persona is not None:
                    _, _, prompt = self._persona_fields(persona)
                    if prompt.strip():
                        return prompt.strip()[:3000]
            except Exception:
                pass

        try:
            personas = await _all()
        except Exception as error:
            logger.warning("读取人格列表失败：%s（自主回复使用内置拟人风格）", str(error)[:150])
            return ""

        available: list[str] = []
        for persona in personas:
            persona_id, name, prompt = self._persona_fields(persona)
            available.append(persona_id or name)
            if wanted in {persona_id, name} and prompt.strip():
                return prompt.strip()[:3000]
        logger.warning(
            "长程记忆：人格 %s 未找到或提示词为空；可用人格：%s（自主回复使用内置拟人风格）",
            wanted, ", ".join(a for a in available if a)[:300] or "（无）",
        )
        return ""

    def _invalidate_injections(self) -> None:
        self._injections_cache = None

    async def _seed_prompt_injections(self) -> None:
        """把自带预设补进库（用户改过的不覆盖，只补缺的）。"""
        if self.storage is None:
            return
        try:
            added = await self.storage.seed_prompt_injections(prompt_injections.PRESETS)
            if added:
                logger.info("长程记忆：已补入 %d 条自带提示词注入预设", added)
            self._invalidate_injections()
        except Exception as error:
            logger.warning("长程记忆：提示词注入预设播种失败：%s", str(error)[:140])

    async def _injections_block(self) -> str:
        """启用中的注入 → 一段"强制规则"文本（回复与自主行动都用它）。

        缓存一份：每条消息都查库没必要；控制台/工具改动会调用 _invalidate_injections。
        """
        cached = getattr(self, "_injections_cache", None)
        if cached is not None:
            return cached
        text = ""
        if self.storage is not None:
            try:
                rows = await self.storage.list_prompt_injections()
                text = prompt_injections.render_block(rows)
            except Exception as error:
                logger.info("长程记忆：读取提示词注入失败：%s", str(error)[:120])
        self._injections_cache = text
        return text

    async def _compose_reply_prompt(self) -> str:
        persona = await self._persona_prompt()
        parts = [part for part in (persona, REPLY_SYSTEM_PROMPT.strip()) if part]
        injected = await self._injections_block()
        if injected:
            parts.append(injected)
        inventory = self._tool_inventory_prompt()
        if inventory:
            parts.append(inventory)
        script_block = self._script_prompts_block()
        if script_block:
            parts.append(script_block)
        orders = self._standing_orders_block()
        if orders:
            parts.append(orders)
        return "\n\n".join(parts) + "\n\n" + MEMORY_TRUST_POLICY

    _VISION_REFUSAL_RE = re.compile(
        r"无法查看|无法识别|不支持视觉|看不到图|不能查看图片|无法查看或识别|"
        r"cannot see|no vision|vision.*not support|as a text",
        re.I,
    )

    @staticmethod
    def _is_vision_refusal(text: str) -> bool:
        """模型（或通道）拿不到图时会客套拒绝；这句垃圾绝不能入记忆。"""
        return bool(text) and bool(
            LongMemoryAgentPlugin._VISION_REFUSAL_RE.search(text[:200])
        )

    async def _describe_images(
        self, images: list[tuple[str, str]], event: AstrMessageEvent | None = None,
        prompt: str | None = None,
    ) -> str:
        """Describe images via the proven vision path; refusals yield "".

        When an event is available we go through `tool_loop_agent` — the same
        channel that already sees images in chat — because `llm_generate` on
        some providers drops the attachments and the model then answers
        "不支持视觉"（这句会被 refusal 过滤拦下，不入记忆）.
        """
        if not images:
            return ""
        provider = self.settings.vision_provider_id or self.settings.reply_provider_id
        urls = [f"data:{mime};base64,{b64}" for mime, b64 in images[:2]]
        prompt = prompt or "用一两句话客观描述这张图片里的内容和文字（如果是表情包，描述其情绪和含义）。直接给描述本身。"
        try:
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                if event is not None:
                    response = await self._llm_text(
                        provider,
                        event=event,
                        agent=True,
                        prompt=prompt,
                        system_prompt="你是识图器，只输出图片描述本身。",
                        image_urls=urls,
                    )
                else:
                    live_provider = await self._resolve_provider(provider, None)
                    response = await self.context.llm_generate(
                        chat_provider_id=live_provider,
                        prompt=prompt,
                        image_urls=urls,
                    )
                    response = str(getattr(response, "completion_text", "") or "")
            text = str(response or "").strip()[:600]
            if self._is_vision_refusal(text):
                logger.info("长程记忆：识图被拒绝（模型未收到图），丢弃该描述")
                return ""
            return text
        except Exception as error:
            logger.warning("长程记忆：图片识别失败：%s", str(error)[:200])
            return ""

    async def _message_images(self, event: AstrMessageEvent, scope_id: str, message_id: str) -> list[tuple[str, str]]:
        """Collect base64 images archived for the triggering message."""
        if not self.media:
            return []
        try:
            records = await self.media.find_by_message(scope_id, message_id, kind="image")
            pairs = []
            for record in records:
                if record.status != "saved":
                    continue
                mime, b64 = await self.media.get_base64(record.item_id)
                if not mime.startswith("image/"):
                    continue
                pairs.append((mime, b64))
                if len(pairs) >= 2:
                    break
            return pairs
        except Exception as error:
            logger.warning("长程记忆：读取已归档图片失败：%s", str(error)[:150])
            return []

    def _agent_reply_system_prompt(self, persona: str = "") -> str:
        """agent 循环（tool_loop_agent）专用系统提示词：最终回应不再强制 JSON。

        旧【最终输出】的严格 JSON 格式（thought/mode/message）是给无工具两段式
        设计的——在多轮工具循环里教模型"只输出一个 JSON"会让它跳过工具直接
        给计划文本（实录：判定=agent 却一次工具都没调、直接出 4 段纯文本）。
        工具 schema 已经在请求里传给模型，这里也不再重复注入工具清单文本
        （50+ 工具名列两遍只会挤劣化行为）。"""
        parts = [part for part in (persona, REPLY_SYSTEM_PROMPT.strip()) if part]
        script_block = self._script_prompts_block()
        if script_block:
            parts.append(script_block)
        orders = self._standing_orders_block()
        if orders:
            parts.append(orders)
        digest = self._capability_digest()
        external = digest.get("external") or []
        if external:
            listed = "、".join(external[:40])
            parts.append(
                "【可用能力】这次会话里你能调用的工具共 %d 个（含 AstrBot 内置与其它插件）：%s。"
                "不确定工具名或用法时，先调 find_tools 搜关键词（如 知识库/搜索/图片/文件/"
                "群相册/定时）——比凭记忆猜工具名可靠；搜到的工具与你自己的一样可以直接调用。"
                % (digest["total"], listed))
        parts.append(QQMSG_STYLE_PROMPT)
        parts.append(GROUP_LIFE_BEHAVIORS_PROMPT)
        parts.append(TASK_DISPATCH_PROMPT)
        parts.append(AGENT_FINAL_OUTPUT_PROMPT)
        return "\n\n".join(parts) + "\n\n" + MEMORY_TRUST_POLICY

    async def _reply_plan(
        self,
        event: AstrMessageEvent,
        scope_id: str,
        context: str,
        decision: Decision,
        image_only: bool = False,
        image_urls: list[str] | None = None,
        analysis: dict[str, Any] | None = None,
    ) -> MessagePlan:
        assert self.retrieval and self.storage
        style = analyze_recent_style(await self.retrieval.recent(scope_id, 20))
        provider = _provider_id(self.settings.reply_provider_id, await self._current_provider(event))
        payload: dict[str, Any] = {
            "current_message": event.get_message_str(),
            "decision_intent": decision.intent,
            "style_hint": style.as_prompt_data(),
            "memory_context": parse_json_object(context) or {},
        }
        if analysis:
            payload["request_analysis"] = analysis
        if decision.action == "text_sticker":
            payload["sticker_directive"] = (
                "本回合请配一张表情包：先 pick_sticker(按你想表达的情绪/梗) 挑一个 "
                "sticker_id，再在最终计划里加一段 sticker（或用 send_sticker 发）。"
            )
        if image_only:
            payload["message_type"] = "image"
            payload["note"] = "你看不到这张图片的内容；只做符合当前情绪的最简短回应，或改用表情/戳一戳表达。"
        prompt = compact_json(payload, self.settings.context_char_budget)
        # agent 循环走专用系统提示词（不强制 JSON、不重复注入工具清单）；
        # 旧 _compose_reply_prompt 的【最终输出】JSON 只服务无工具两段式。
        # 最终回应的形状：模型循环里可以直接说话，也可能带计划 JSON——
        # 解析时优先按计划 JSON 解析，解析不出就整段正文当单段回复（自然对话）。
        persona = await self._persona_prompt()
        raw = await self._llm_text(
            provider,
            event=event,
            prompt=prompt,
            system_prompt=self._agent_reply_system_prompt(persona),
            tools=self._effective_tool_set(event),
            agent=True,
            image_urls=image_urls if image_urls else None,
        )
        # 长文本转图前置拦截：按原始输出的总字数判断（解析+校验+salvage 都可能
        # 截短文本，到 _execute_decision 再看就永远低于阈值了）。超阈值时跳过
        # 拟人拆条，直接构造单段计划，由 _execute_decision 整条渲染成图片。
        t2i_threshold = self.settings.text_to_image_threshold
        if t2i_threshold > 0:
            raw_obj = parse_json_object(raw) or {}
            raw_texts: list[str] = []
            if raw_obj.get("mode") == "sequence" and isinstance(raw_obj.get("segments"), list):
                raw_texts = [str((s or {}).get("text", ""))
                             for s in raw_obj["segments"] if isinstance(s, dict)]
            elif isinstance(raw_obj.get("message"), dict):
                raw_texts = [str(raw_obj["message"].get("text", ""))]
            if raw_texts and sum(len(x) for x in raw_texts) > t2i_threshold:
                from .src.humanization import PlannedAction

                logger.info(
                    "长程记忆：%s原始输出%d字≥转图阈值，跳过拆条直接渲染",
                    self._conversation_key(event), sum(len(x) for x in raw_texts),
                )
                return MessagePlan(
                    "single",
                    (PlannedAction(action="text", text="\n".join(raw_texts)),),
                    False,
                )
        try:
            plan = parse_message_plan(raw, self._humanization)
        except (PlanValidationError, ValueError):
            # agent 循环的最终回应是自然对话文本（不再强制 JSON）：整段当单段
            # 回复，按行拆开成短气泡；salvage 只负责从坏 JSON 里抢救文本
            plan = self._plain_text_plan(raw) or salvage_message_plan(raw, self._humanization)
        from .src.humanization import _is_tool_garbage, _is_inner_voice, is_runaway_repetition

        joined_text = "\n".join(
            s.text or "" for s in plan.segments if s.action == "text")
        if joined_text.strip() and is_runaway_repetition(joined_text):
            # Hermes 移植：失控重复循环的回复整体丢弃（触发重试/兜底）
            raise PlanValidationError("reply degenerated into a repetition loop")
        plan_segments = [
            s for s in plan.segments
            if s.action != "text" or (s.text and not _is_tool_garbage(s.text)
                                      and not _is_inner_voice(s.text))
        ]
        if not plan_segments:
            raise PlanValidationError("reply is entirely tool-call garbage")
        plan = MessagePlan(plan.mode, tuple(plan_segments), plan.explicit_segments)
        return humanize_plan(plan, self._humanization, style)

    async def _draw_and_send(self, event: AstrMessageEvent, subject: str,
                             context: str = "") -> dict[str, Any] | None:
        """画图核心管线（draw_picture 工具的实现体）：LLM 出 SVG → 渲染 → 截图 → 发送。

        v0.36.0 起没有自动路由：要不要画、画什么，全部由主agent自己决定并传
        subject 进来；这里只负责把一张图可靠地画出来发出去。服务器状态类主题
        会自动先抓真实指标（数据丰富是管线内部的事，不是路由）。
        """
        subject = " ".join(str(subject or "").split())[:200]
        if not subject:
            return None
        # 近期画过的项目标题进提示词：帮模型对齐"还是那个/换一版"的语境
        recent_designs: list[str] = []
        try:
            for row in list(self._studio_host().list_design_projects())[:5]:
                title = str((row or {}).get("title") or "").strip()
                if title:
                    recent_designs.append(title[:40])
        except Exception:
            recent_designs = []
        # 服务器状态类主题：先抓真实指标塞进提示词，画出来的是真数据仪表盘
        # （实录机器人被要求"画图看看服务器状态"，只剩空谈——要么真数据，要么别画）
        metrics = ""
        status_haystack = " ".join(
            part for part in (subject, " ".join(recent_designs)) if part)
        if _looks_like_server_status_request(status_haystack) and self._ssh_configured():
            try:
                raw_metrics = await self._dispatch_ssh_action("ssh_exec", {
                    "command": (
                        "echo CPU=$(nproc)核 负载$(cut -d' ' -f1-3 /proc/loadavg); "
                        "free -m | awk 'NR==2{printf \"内存 %s/%sMB\\n\", $3, $2}'; "
                        "df -h / | awk 'NR==2{printf \"磁盘 %s/%s (%s)\\n\", $3, $2, $5}'; "
                        "uptime -p"),
                    "timeout": 25,
                })
                parsed_metrics = parse_json_object(raw_metrics) or {}
                metrics = str(parsed_metrics.get("stdout") or "").strip()[:600]
            except Exception as error:
                logger.info("长程记忆：画图取服务器指标失败：%s", str(error)[:120])
        try:
            provider = _provider_id(
                self.settings.reply_provider_id, await self._current_provider(event))
        except Exception:
            provider = self.settings.reply_provider_id or ""
        if not provider:
            return None
        payload: dict[str, Any] = {
            "request": subject,
            "ask": ("请把上面的画图请求画成一张 SVG 插画。只输出一个 JSON 对象："
                    '{"title": "一句话标题", "svg": "<svg ...>...</svg>"}。'
                    "SVG 要求：宽 800 高 600、viewBox='0 0 800 600'、白底或浅色底；"
                    "用基本形状（椭圆/矩形/路径/圆）把主体拼出来，主体居中、比例合理，"
                    "加少量背景元素；必须是合法可渲染的 SVG 文本，不要 markdown、不要解释。"),
        }
        if recent_designs:
            payload["recent_designs"] = recent_designs
        if context:
            payload["context"] = str(context)[:1500]
        if metrics:
            payload["real_metrics"] = metrics
            payload["metrics_rule"] = (
                "real_metrics 是真实抓到的服务器指标：仪表盘/状态面板类画面必须把"
                "里面的数字原样标在图上（绿黄红分档），不许自己编数字。")
        prompt = compact_json(payload, 4000)
        try:
            async with asyncio.timeout(90):
                raw = await self._llm_text(
                    provider, event=event, prompt=prompt,
                    system_prompt=("你是插画生成器。把用户的画图请求变成一张简洁可爱的"
                                   " SVG 矢量插画。只输出严格 JSON。"),
                )
        except Exception as error:
            logger.warning("长程记忆：画图 LLM 失败：%s", str(error)[:160])
            return None
        data = parse_json_object(raw) or {}
        svg = str(data.get("svg") or "").strip()
        title = str(data.get("title") or "").strip() or "画图"
        if not svg.lower().startswith("<svg"):
            return None
        try:
            window = self._studio_host().put_design(self._wrap_svg(svg))
            media_id = await self._design_screenshot(self._wrap_svg(svg))
        except Exception as error:
            logger.warning("长程记忆：画图渲染失败：%s", str(error)[:160])
            return None
        if not media_id or self.media is None:
            return None
        try:
            # get_path 也是 async，不能经 to_thread 同步调用（又一个 coroutine 陷阱）
            await event.send(MessageChain().file_image(
                str(await self.media.get_path(media_id))))
            logger.info("长程记忆：画图已发送（%s，窗口 %s）", title[:20], window["url"])
        except Exception as error:
            logger.warning("长程记忆：画图发送失败：%s", str(error)[:160])
            return None
        return {"ok": True, "title": title, "window_url": window["url"],
                "real_metrics": bool(metrics)}

    @filter.llm_tool(name="draw_picture")
    async def draw_picture_tool(self, event: AstrMessageEvent, subject: str = ""):
        """画一张图并立刻发到当前会话（SVG→浏览器渲染→截图→发送的可靠管线）。
        画图优先用它：不用你再 design_render+send_image 手动拼。主题是服务器状态/
        监控类时会自动先 ssh 抓真实指标并标在图上。收尾的话你自己说——工具只负责
        把图发出去，返回后你接着正常回复即可。

        Args:
            subject(string): 画什么——具体主题（如"目标服务器状态仪表盘"、"戴眼镜的猫"）。
                "再画一次/复用技能画"这类指代式说法要先解出具体主题再传，别原样传进来。
        """
        subject = str(subject or "").strip()
        if not subject:
            return "需要 subject（画什么——具体主题，指代式说法先解出指代）"
        try:
            result = await self._draw_and_send(event, subject)
        except Exception as error:
            return f"画图失败：{type(error).__name__}: {str(error)[:140]}"
        if result is None:
            return ("画图管线没走通（模型没产出合法 SVG 或渲染/发送失败），"
                    "可改用 design_render 自己拼，或如实告诉用户画不成")
        return compact_json(result, 1200)

    async def _recent_message_ids(self, scope_id: str, count: int) -> list[str]:
        """取某会话最近 N 条消息的内部 id（旧→新，含 bot 自己发的）。

        实录：让 bot "把你自己的最后一条消息做成卡片"，它手里没有任何 id（卡片/转发
        工具当时只认 id），于是绕去 design_render 手搓 HTML 画了个丑页面。
        有了这个，模型只要给条数就能干活——顺手的路不该比歪路更难走。
        """
        if self.storage is None or not str(scope_id or "").strip():
            return []
        limit = max(1, min(int(count or 1), 20))
        try:
            rows = await self.storage.recent_messages(str(scope_id), limit)
        except Exception as error:
            logger.info("长程记忆：取最近消息失败：%s", str(error)[:120])
            return []
        return [str(row.message_id) for row in rows if str(row.message_id or "")]

    async def _fetch_message_details(
        self, message_ids: list[str], scope_id: str | None, *,
        enrich: bool = True, depth: int = 0,
    ) -> list[dict[str, Any]]:
        """逐条拉取消息原文（网关 get_msg 优先，回落本地库存）——转发与卡片共用。

        SnowLuma/NapCat 的 messageFormat 可能给段数组或字符串，这里归一成段数组。
        """
        details: list[dict[str, Any]] = []
        for mid in [str(x).strip() for x in message_ids if str(x).strip()][:20]:
            detail: dict[str, Any] | None = None
            try:
                self._bind_gateway_client()
                response = await self.gateway.execute("get_msg", message_id=mid)
                data = qq_payload(response)
                # 形状校验：网关偶尔回一个非空但没有消息字段的壳（错误对象/空信封）。
                # 那种"数据"过不了下面，会把 detail 建成空署名+空内容（实录：转发节点署名
                # 全变"群友"）——形状不对就当没取到，回落本地库存。
                if data and not ({"message", "raw_message", "sender"} & set(data)):
                    logger.info("长程记忆：get_msg %s 返回的形状不是消息，回落库存", mid[:16])
                    data = {}
                if data:
                    sender = data.get("sender") or {}
                    message = data.get("message")
                    if isinstance(message, str) or message is None:
                        text = str(message or data.get("raw_message") or "")
                        segments = ([{"type": "text", "data": {"text": text}}]
                                    if text else [])
                    else:
                        segments = list(message)
                    detail = {
                        "id": mid,
                        "uin": str(sender.get("user_id") or ""),
                        "name": str(sender.get("card") or sender.get("nickname") or ""),
                        # QQ 事件给的是 epoch；本地化后再进卡片/转发（否则卡片上是 UTC 钟点）
                        "time": timeutil.to_text(data.get("time"), "%H:%M"),
                        "segments": segments,
                    }
            except Exception as error:
                logger.info("长程记忆：get_msg %s 失败（回落库存）：%s",
                            mid[:16], str(error)[:120])
            if detail is None and self.storage is not None:
                # 先当前会话，再兜底扫其它会话——录实录：跨群引用消息 id 时，
                # 只认当前会话会「一条都取不到」，模型只好改拿手边的消息凑。
                tried: list[str] = []
                for candidate in [str(scope_id or "")] + [
                        str(x) for x in (self._known_scopes or {}).values()][:20]:
                    if not candidate or candidate in tried:
                        continue
                    tried.append(candidate)
                    try:
                        row = await self.storage.find_message_any(candidate, mid)
                    except Exception:
                        row = None
                    if row is not None:
                        break
                else:
                    row = None
                if row is not None:
                    stored_parts = [dict(part) for part in (getattr(row, "parts", None) or [])
                                    if isinstance(part, Mapping)]
                    detail = {
                        "id": mid, "uin": str(row.sender_id),
                        "name": str(row.sender_name),
                        "time": timeutil.to_text(row.occurred_at, "%Y-%m-%d %H:%M"),
                        # 入库时存的是原始段（@/图片/表情/引用都在），别再压成纯文本——
                        # 实录：卡片里 @、表情包、引用全丢了，就是这么丢的
                        "segments": stored_parts or [{"type": "text",
                                                      "data": {"text": str(row.text or "")}}],
                        "reply_to": str(getattr(row, "reply_to", "") or ""),
                    }
            if detail is not None:
                details.append(detail)
        if enrich and details:
            for item in details:
                await self._enrich_card_detail(item, scope_id, depth=depth)
        return details

    @staticmethod
    def _reply_id_of(item: Mapping[str, Any]) -> str:
        """这条消息在引用谁：库里存的 reply_to，或段里的 reply.id。"""
        ref = str(item.get("reply_to") or "").strip()
        if ref:
            return ref
        for segment in list(item.get("segments") or []):
            if str(segment.get("type")) == "reply":
                return str((segment.get("data") or {}).get("id") or "").strip()
        return ""

    async def _enrich_card_detail(self, item: dict[str, Any],
                                  scope_id: str | None, *, depth: int = 0) -> None:
        """把一条消息补齐成"能画成卡片"的样子：引用源消息、图片内联、转发子消息。

        实录（用户两张截图）：卡片里引用、表情包、@、转发全丢了——因为旧代码只把
        `text` 交给渲染器，而原始段（parts）、引用目标（reply_to）都没用上。
        """
        segments = [seg for seg in list(item.get("segments") or [])
                    if isinstance(seg, Mapping)]
        # ① 图片/表情：抓成本地 base64，画卡片与转发都不再依赖会过期的 CDN 链接
        for index, segment in enumerate(segments):
            kind = str(segment.get("type") or "")
            if kind not in {"image", "mface", "marketface"}:
                continue
            data = dict(segment.get("data") or {})
            if self._segment_already_inline(data):
                continue
            payload = await self._fetch_media_bytes(data, "image")
            if not payload:
                continue
            encoded = base64.b64encode(payload).decode("ascii")
            segments[index] = {"type": "image",
                               "data": {**data, "file": "base64://" + encoded, "url": ""}}
        # ② 引用的源消息：把被引用的那条也取出来（只一层，防递归）
        reply_id = self._reply_id_of(item)
        if reply_id and depth < 1:
            quoted = await self._fetch_message_details(
                [reply_id], scope_id, enrich=True, depth=depth + 1)
            if quoted:
                source = quoted[0]
                item["reply_name"] = str(source.get("name") or source.get("uin") or "")
                item["reply_segments"] = list(source.get("segments") or [])
                item["reply_text"] = str(source.get("text") or "")
        # ③ 合并转发段：展开成子消息（一层），卡片里渲染成嵌套块
        forward_ids: list[str] = []
        for segment in segments:
            if str(segment.get("type")) in {"forward", "node", "forwardtransfer", "flashtransfer"}:
                found = self._forward_ids({"message": [dict(segment)]})
                forward_ids.extend(fid for fid in found if fid not in forward_ids)
        if forward_ids and depth < 1 and self.gateway is not None:
            children = await self._forward_children(forward_ids[0], scope_id)
            if children:
                item["children"] = children
        item["segments"] = segments

    async def _forward_children(self, forward_id: str,
                                scope_id: str | None) -> list[dict[str, Any]]:
        """把一份合并转发展开成卡片能画的子消息（名字 + 段），图片顺带内联。"""
        nodes: list[dict[str, Any]] = []
        self._bind_gateway_client()
        for params in ({"message_id": forward_id}, {"id": forward_id, "message_id": forward_id},
                       {"id": forward_id}):
            try:
                response = await self.gateway.execute("get_forward_msg", **params)
                nodes = messages_of(response)
            except Exception as error:
                logger.info("长程记忆：卡片展开转发失败：%s", str(error)[:120])
                nodes = []
            if nodes:
                break
        children: list[dict[str, Any]] = []
        for node in nodes[:20]:
            if not isinstance(node, Mapping):
                continue
            raw_segments = node.get("message") if isinstance(node.get("message"), list) else None
            if raw_segments is None:
                inner = node.get("content")
                if isinstance(inner, Mapping) and isinstance(inner.get("content"), list):
                    raw_segments = inner["content"]
                elif isinstance(node.get("data"), Mapping):
                    candidate = (node["data"] or {}).get("content")
                    raw_segments = candidate if isinstance(candidate, list) else None
            segments: list[dict[str, Any]] = []
            for segment in list(raw_segments or []):
                if not isinstance(segment, Mapping):
                    continue
                kind = str(segment.get("type") or "")
                data = dict(segment.get("data") or {})
                if kind in {"image", "mface", "marketface"}:
                    payload = await self._fetch_media_bytes(data, "image")
                    if payload:
                        segments.append({"type": "image", "data": {
                            **data, "file": "base64://" + base64.b64encode(payload).decode("ascii"),
                            "url": ""}})
                        continue
                if kind in {"text", "at", "face", "image"}:
                    segments.append({"type": kind, "data": data})
            if not segments:
                continue
            children.append({"name": _forward_node_name(node) or "群友",
                             "segments": segments})
        return children

    @staticmethod
    def _parse_forward_nodes(raw: str) -> list[dict[str, Any]]:
        """把模型自拼的节点数组解析成内部节点结构。

        形如 ``[{"name": "长公主", "uin": "364962863", "text": "原话"}]``；
        也可以给 ``segments`` 直接放 OneBot 段数组。署名按"名字(QQ号)"拼
        （用户要的"包含qq号和名字"就是这种客户端里能一眼看到的形式），
        name/uin 缺一个就用另一个。
        """
        text = str(raw or "").strip()
        if not text:
            return []
        data = parse_json_value(text)
        if not isinstance(data, list):
            return []
        nodes: list[dict[str, Any]] = []
        for item in data[:20]:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or item.get("nickname") or "").strip()
            uin = str(item.get("uin") or item.get("user_id") or "").strip()
            label = f"{name}({uin})" if name and uin else (name or uin or "某人")
            segments = item.get("segments")
            if not isinstance(segments, list) or not segments:
                body = str(item.get("text") or item.get("content") or "").strip()
                if not body:
                    continue
                segments = [{"type": "text", "data": {"text": body}}]
            nodes.append({"uin": uin, "name": label,
                          "segments": [dict(s) for s in segments if isinstance(s, Mapping)]})
        return nodes

    async def _inline_media_segment(self, segment: dict[str, Any]) -> dict[str, Any]:
        """把一段媒体内联成 base64；抓不到就降级成文字占位。

        实录（合并转发发不出去的真因）：SnowLuma 打包转发时会把节点里的图片**下载**
        下来内联（makeImageElem → downloadHttp），**任何一张图下载失败都会让整条
        转发发送失败**（`TypeError: fetch failed`）——实测塞一个失效 URL 就复现。
        所以这里先自己把图抓成 base64：SnowLuma 不再需要联网，坏图只丢那一段。
        """
        data = segment.get("data") or {}
        kind = str(segment.get("type") or "media")
        label = {"image": "图片", "record": "语音", "video": "视频"}.get(kind, kind)
        if self._segment_already_inline(data):
            return segment  # 已经是内联内容，原样
        payload = await self._fetch_media_bytes(data, kind)
        if not payload:
            return {"type": "text", "data": {"text": f"[{label}已失效]"}}
        encoded = base64.b64encode(payload).decode("ascii")
        return {"type": kind,
                "data": {**data, "file": "base64://" + encoded, "url": ""}}

    @staticmethod
    def _segment_already_inline(data: Mapping[str, Any]) -> bool:
        """段里已经带 base64（或 base64:// file）就不用再抓。"""
        if str(data.get("base64") or "").strip():
            return True
        return str(data.get("file") or "").lower().startswith(
            ("base64://", "base://", "data:"))

    @staticmethod
    def _looks_like_image(payload: bytes) -> bool:
        """魔数校验：图片抓回来必须真是图片。

        实录：CDN 链接过期后常返回一个 404 的 HTML 页，旧代码照样当图片内联，
        卡片上就是一块乱码/空白。不是图片就当抓取失败（卡片回退原 URL、转发走占位）。
        """
        head = bytes(payload[:16])
        return (head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8")
                or head.startswith(b"GIF8") or head.startswith(b"BM")
                or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
                or head.lstrip()[:5] in {b"<svg ", b"<?xml"})

    async def _fetch_media_bytes(self, data: Mapping[str, Any],
                                 kind: str = "image") -> bytes:
        """媒体段 → 原始字节。依次试：远端 URL → 本地文件 → 网关 get_image。

        实录（转发里图片全变"[图片已失效]"的真因）：get_msg 回里的 `file` 往往是
        **裸文件名**（比如 "a1b2c3.jpg"，既不是 URL 也不是本地路径），而旧代码
        `file or url` 优先拿它 → 读不到就当坏图。现在 URL 优先，裸文件名交给
        get_image 去换真内容（SnowLuma 会给新 URL，NapCat 会给本地路径）。
        """
        cap = int(getattr(self.settings, "media_max_file_mb", 32) or 32) * 1024 * 1024
        refs: list[str] = []
        for key in ("url", "file", "path"):
            value = str(data.get(key) or "").strip()
            if value and value not in refs:
                refs.append(value)
        for source in refs:
            lowered = source.lower()
            if lowered.startswith(("base64://", "base://", "data:")):
                return b""
            if lowered.startswith("file://"):
                source = source[7:]
                lowered = source.lower()
            if lowered.startswith(("http://", "https://")):
                try:
                    payload = await fetch_bounded(source, cap)
                except Exception as error:
                    logger.info("长程记忆：转发媒体下载失败（试下一来源）：%s",
                                str(error)[:120])
                    payload = b""
                if payload and (kind != "image" or self._looks_like_image(payload)):
                    return payload
                if payload:
                    logger.info("长程记忆：抓回的内容不是图片（%s 字节），改用下一来源",
                                len(payload))
                continue
            try:
                path = Path(source)
                if path.is_file():
                    payload = await asyncio.to_thread(path.read_bytes)
                    if kind != "image" or self._looks_like_image(payload):
                        return payload
            except Exception:
                continue
        # 网关兜底：把裸文件名换成真内容（图片走 get_image，语音走 get_record 转码）
        ref = str(data.get("file") or data.get("file_id") or "").strip()
        if not ref or self.gateway is None or kind not in {"image", "record"}:
            return b""
        try:
            self._bind_gateway_client()
            if kind == "image":
                response = await self.gateway.execute("get_image", file=ref)
                normalized = await self._normalize_image_payload(
                    qq_payload(response), limit=cap)
                encoded = str(((normalized or {}).get("data") or {}).get("base64") or "")
            else:
                response = await self.gateway.execute(
                    "get_record", file=ref, out_format="mp3")
                inner = qq_payload(response) or {}
                encoded = str(inner.get("base64") or "")
            if encoded:
                return base64.b64decode(encoded.split(",", 1)[-1])
        except Exception as error:
            logger.info("长程记忆：转发媒体经网关取回失败：%s", str(error)[:120])
        return b""

    @staticmethod
    def _forward_preview(details: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
        """卡片上的逐条预览（QQ 显示"名字：内容"那种）。"""
        preview: list[dict[str, str]] = []
        for item in list(details)[:4]:
            name = str(item.get("name") or item.get("uin") or "群友")
            text = ""
            for segment in list(item.get("segments") or []):
                if str(segment.get("type")) == "text":
                    text = str((segment.get("data") or {}).get("text") or "").strip()
                    if text:
                        break
            if not text:
                kinds = {str(segment.get("type")) for segment in list(item.get("segments") or [])}
                text = "[图片]" if "image" in kinds else "[消息]"
            preview.append({"text": f"{name}：{text[:28]}"})
        return preview

    @staticmethod
    def _clean_forward_name(value: Any) -> str:
        """节点署名清洗：去控制字符/代理对残渣，压掉多余空白并限长。

        实录：转发窗口标题出现过 "&ÿÿÆê ◆◆◆◆" 这类乱码——署名里混进了坏字符，
        QQ 自己拼 "<甲>和<乙>的聊天记录" 就花了。
        """
        text = str(value or "")
        cleaned = "".join(ch for ch in text
                          if ch.isprintable() and not (0xDC80 <= ord(ch) <= 0xDCFF)
                          and ord(ch) != 0xFFFD)
        cleaned = " ".join(cleaned.split()).strip()
        return cleaned[:24]

    async def _build_forward_nodes(
        self, details: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """构造合并转发节点：媒体段先内联成 base64，坏媒体降级为占位文字。"""
        nodes: list[dict[str, Any]] = []
        for item in list(details)[:20]:
            segments: list[dict[str, Any]] = []
            for segment in list(item.get("segments") or []):
                if not isinstance(segment, Mapping):
                    continue
                if str(segment.get("type")) in {"image", "record", "video"}:
                    segments.append(await self._inline_media_segment(dict(segment)))
                else:
                    segments.append(dict(segment))
            if not segments:
                segments = [{"type": "text", "data": {"text": "[空消息]"}}]
            nodes.append({"type": "node", "data": {
                "user_id": str(item.get("uin") or "10000"),
                "nickname": self._clean_forward_name(item.get("name")) or "群友",
                "content": segments,
            }})
        return nodes

    @filter.llm_tool(name="forward_messages")
    async def forward_messages_tool(
        self, event: AstrMessageEvent, message_ids_json: str = "",
        nodes_json: str = "", target_group_id: str = "", target_user_id: str = "",
        summary: str = "", latest_count: int = 0,
    ):
        """把一批消息做成**合并聊天记录**发出去（挂人、留证据、把神人发言拼成一册）。
        两种用法，任选其一：
        ① `message_ids_json`：转发**已存在的消息**——先用 search_chat_history /
           qq_recent_history 找到它们，把结果里的 id（内部编号或 `qq_id` 都收）填进来；
        ② `nodes_json`：**自己拼**一条记录——直接给每条的 名字/QQ号/原话，
           不用先有消息 id（"把这些人的发言拼成合并记录，包含QQ号和名字"就用这个）。
        目标留空=发到当前会话；群必须在白名单。

        Args:
            message_ids_json(string): 消息 id 数组 JSON，如 ["abc123", "7654321"]。
            nodes_json(string): 自拼节点数组 JSON，如
                [{"name": "长公主", "uin": "364962863", "text": "不是我这bug怎么这么多"}]
                （name/uin 会被拼成发送者署名"长公主(364962863)"）。
            target_group_id(string): 目标群号（留空=当前会话）。
            target_user_id(string): 目标 QQ（与群号二选一）。
            summary(string): 外层摘要，留空自动取第一条内容。
            latest_count(number): 不用 id：直接取当前会话（或 target 指定的群）最近 N 条
                消息打包（含 bot 自己发的，最多 20）。"把最近 5 条打包"就填 5。
        """
        ids = self._parse_media_ids(message_ids_json)
        custom_nodes = self._parse_forward_nodes(nodes_json)
        if not ids and not custom_nodes and int(latest_count or 0) > 0:
            group_ref = str(target_group_id or "").strip()
            source_scope = None
            if group_ref:
                resolved, _key, hint = await self._resolve_scope_ref(group_ref)
                if not resolved:
                    return f"没能定位目标群：{hint}"
                source_scope = resolved
            else:
                source_scope = await self._scope_for_event(event) if event is not None else None
            ids = await self._recent_message_ids(source_scope or "", latest_count)
            if not ids:
                return "这个会话最近没有可用消息（可用 message_ids_json 指定消息 id）"
        if not ids and not custom_nodes:
            return ("需要 message_ids_json（转发已有消息）/ latest_count（取最近几条）"
                    "或 nodes_json（自己拼一条记录：名字/QQ号/原话）——见工具说明")
        for node in custom_nodes[:20]:
            node["custom"] = True
        target_group = str(target_group_id or "").strip()
        target_user = str(target_user_id or "").strip()
        if target_group and not target_group.isdigit():
            # 群名也能用（实录：模型手里只有"数学指令讨论群"这种名字）
            resolved, key, hint = await self._resolve_scope_ref(target_group)
            if not resolved:
                return f"没能定位目标群：{hint}"
            target_group = key
        if target_user and not target_user.isdigit():
            resolved, key, hint = await self._resolve_scope_ref(target_user)
            if resolved:
                target_user = key
        if not target_group and not target_user:
            current_group = str(event.get_group_id() or "")
            if current_group:
                target_group = current_group
            else:
                target_user = str(event.get_sender_id() or "")
        if target_group and not self.settings.allows_group(target_group):
            return f"拒绝转发：群 {target_group} 不在白名单"
        if self.gateway is None:
            return "QQ 网关未就绪"
        scope_id = await self._scope_for_event(event) if event is not None else None
        details = await self._fetch_message_details(ids, scope_id) if ids else []
        missing = [str(x) for x in ids] if ids and not details else []
        details = list(custom_nodes) + details
        if not details:
            return (f"一条消息都没取到（可能已删除或 id 无效）：{[x[:16] for x in ids][:5]}。"
                    "改用 nodes_json 自拼，或先 search_chat_history 拿到有效 id")
        first_text = ""
        for seg in (details[0].get("segments") or []):
            if seg.get("type") == "text":
                first_text = str((seg.get("data") or {}).get("text") or "")
                break
        outer = str(summary or "").strip() or (first_text[:50] or
                                               f"{details[0]['name'] or '群友'} 的聊天记录")
        self._bind_gateway_client()
        try:
            if len(details) == 1 and not details[0].get("custom") and target_group:
                await self.gateway.execute(
                    "forward_group_single_msg", message_id=details[0]["id"],
                    group_id=int(target_group) if target_group.isdigit() else target_group)
            elif len(details) == 1 and not details[0].get("custom") and target_user:
                await self.gateway.execute(
                    "forward_friend_single_msg", message_id=details[0]["id"],
                    user_id=int(target_user) if target_user.isdigit() else target_user)
            else:
                nodes = await self._build_forward_nodes(details)
                # 卡片文案各就其位：summary=标题、prompt=预览前缀、news=逐条预览。
                # 实录：三个字段塞同一句话，QQ 卡片上就出现两行一样的文字。
                preview = self._forward_preview(details)
                if target_group:
                    await self.gateway.execute(
                        "send_group_forward_msg",
                        group_id=int(target_group) if target_group.isdigit() else target_group,
                        message=nodes, summary=outer[:50], prompt="[聊天记录]",
                        news=preview)
                else:
                    await self.gateway.execute(
                        "send_private_forward_msg",
                        user_id=int(target_user) if target_user.isdigit() else target_user,
                        message=nodes, summary=outer[:50], prompt="[聊天记录]",
                        news=preview)
        except Exception as error:
            return f"转发失败：{type(error).__name__}: {str(error)[:140]}"
        logger.info("长程记忆：%s已转发%d条消息（→%s）",
                    self._conversation_key(event), len(details),
                    target_group or target_user)
        payload_out: dict[str, Any] = {"ok": True, "forwarded": len(details),
                                       "target": target_group or target_user,
                                       "summary": outer[:50]}
        if missing:
            payload_out["missing_ids"] = missing[:5]
        return compact_json(payload_out, 800)

    @filter.llm_tool(name="screenshot_messages")
    async def screenshot_messages_tool(
        self, event: AstrMessageEvent, message_ids_json: str = "", title: str = "",
        group_id: str = "", latest_count: int = 0,
    ):
        """把一批消息做成 QQ 风格的聊天卡片图片发到当前会话（伪截图：头像+昵称+
        内容气泡，支持图文混排、合并转发占位）。比真实转发更直观，任何会话都能发；
        "挂人"、留档、展示聊天记录都用它。

        **要做聊天记录样子的卡片一律用它**——不要用 design_render 自己写 HTML 画聊天界面
        （实录：那样会画成大字报，字号溢出、也不像聊天记录）。
        不知道消息 id 时：latest_count=1 就是"我最近一条"，5 就是最近五条。

        Args:
            message_ids_json(string): 消息 id 数组 JSON，按时间顺序（与 latest_count 二选一）。
            title(string): 卡片标题，留空默认"聊天记录"。
            group_id(string): 这些消息属于哪个群（群号或群名，如"数学指令讨论群"）；
                消息来自别的群/会话时必须给，留空按当前会话找。
            latest_count(number): 不用 id：直接取该会话最近 N 条消息（含 bot 自己发的，
                最多 20）。想把自己刚说的一句做成卡片就填 1。
        """
        ids = self._parse_media_ids(message_ids_json)
        scope_id = await self._scope_for_event(event) if event is not None else None
        if str(group_id or "").strip():
            resolved, _key, hint = await self._resolve_scope_ref(group_id)
            if not resolved:
                return f"没能定位这个群：{hint}"
            scope_id = resolved
        if not ids and int(latest_count or 0) > 0:
            ids = await self._recent_message_ids(scope_id or "", latest_count)
            if not ids:
                return f"这个会话最近没有可用消息（group_id={group_id or '当前会话'}）"
        if not ids:
            return "需要 message_ids_json（消息 id 数组）或 latest_count（取最近几条）"
        details = await self._fetch_message_details(ids, scope_id)
        if not details:
            return "一条消息都没取到（可能已删除或 id 无效）"
        from .src.chat_card import build_card_html

        card_html = build_card_html(details, title=str(title or "").strip() or "聊天记录")
        try:
            media_id = await self._design_screenshot(card_html)
        except Exception as error:
            return f"卡片渲染失败：{type(error).__name__}: {str(error)[:140]}"
        if not media_id or self.media is None:
            return "卡片渲染失败（没有产出图片）"
        try:
            await event.send(MessageChain().file_image(
                str(await self.media.get_path(media_id))))
        except Exception as error:
            return f"卡片已生成但发送失败：{str(error)[:140]}"
        return compact_json({"ok": True, "messages": len(details)}, 500)

    @filter.llm_tool(name="read_forward")
    async def read_forward_tool(
        self, event: AstrMessageEvent, message_id: str = "",
        forward_id: str = "", count: int = 1,
    ):
        """读合并转发（聊天记录卡片）的完整内容，按"谁说的：内容"排列。
        优先读已入库的展开结果——所以**能读出来就一定能读出来**，不会因为
        QQ 侧资源过期而失败；真读不到就如实说读不到。
        一条转发读不全时换另一个参数再试（message_id 是那条转发消息本身的 id，
        forward_id 是卡片里的资源 id）。

        Args:
            message_id(string): 承载这条转发的消息 id（最常见、最有效）。
            forward_id(string): 转发资源 id（res_id），message_id 不灵时用。
            count(number): message_id 本身不是转发时，往前找最近几条消息里的转发（默认1）。
        """
        conv_key = self._conversation_key(event)
        current_id = str(getattr(event.message_obj, "message_id", "") or "")
        scope_id = await self._scope_for_event(event) if event is not None else None
        candidates: list[str] = []
        for raw in (forward_id, message_id, current_id):
            text = str(raw or "").strip()
            if text and text not in candidates:
                candidates.append(text)
        # 登记的待展开表（到达时抓到的转发 id）也算候选
        for fid in self._pending_forwards.get(conv_key, []):
            if fid not in candidates:
                candidates.append(fid)
        if not candidates:
            return "需要 message_id（那条转发消息的 id）或 forward_id"
        # message_id 指向普通消息时，先把它的转发段 id 解析出来
        for mid in list(candidates[:2]):
            if scope_id and str(mid) not in (str(forward_id or ""),):
                try:
                    self._bind_gateway_client()
                    response = await self.gateway.execute("get_msg", message_id=mid)
                    data = qq_payload(response)
                    message = data.get("message")
                    raw = {"message": message if isinstance(message, list) else []}
                    for fid in self._forward_ids(raw):
                        if fid not in candidates:
                            candidates.append(fid)
                except Exception:
                    pass
        extra = [current_id, message_id, forward_id]
        tried = 0
        for candidate in candidates[:6]:
            tried += 1
            text = await self._fetch_forward_text(
                candidate, extra_ids=extra, scope_id=scope_id)
            if text:
                await self._remember_forward_text(scope_id, candidate, text)
                return text[:5000]
        return (f"读了 {tried} 个候选 id 都没取到这份转发的内容"
                "（QQ 侧可能确实没缓存了，且机器人到达时也没能抓到）——"
                "如实告诉对方读不出来，别编造；可以请对方重新转发一次")

    async def _remember_forward_text(self, scope_id: str | None, forward_id: str,
                                     text: str) -> None:
        """把刚成功展开的转发正文补写进库——"能读出来就一定读得出来"：
        入库时没抓到的那份（比如运行时才展开的引用转发），后续读取也能命中。"""
        if not scope_id or not str(text or "").strip():
            return
        # 注意：这里不设最小长度——嵌套转发可能只有一两句（实录 11 字的接龙），
        # 长度门槛会把它们悄悄丢掉，之后资源失效就永久读不到了。
        if self.storage is None or not self.ingest:
            return
        try:
            existing = await self.storage.find_forward_note(scope_id, forward_id)
        except Exception:
            existing = ""
        if existing:
            return
        try:
            from .src.models import NormalizedMessage

            scopes = await self.storage.all_scopes("aiocqhttp")
            row = next((s for s in scopes if str(s.get("scope_id")) == str(scope_id)), {})
            stem = f"forward:{forward_id[:16]}:{hash(text) & 0xFFFFFFFF:08x}"
            await self.ingest.ingest(NormalizedMessage(
                platform="aiocqhttp",
                account_id=str(row.get("account_id") or "0"),
                conversation_id=str(row.get("conversation_id") or scope_id),
                upstream_message_id=stem, sender_id="self", sender_name="self",
                text=f"[合并转发内容 {forward_id[:12]}]\n{text}",
                occurred_at=datetime.now(timezone.utc).isoformat(),
                raw_event={"message_id": stem, "derived": True, "kind": "forward"},
                parts=[], event_type="notice.forward",
            ), f"forward:{scope_id}:{stem}")
            logger.info("长程记忆：转发正文已补写入库（%s）", forward_id[:12])
        except Exception as error:
            logger.info("长程记忆：转发正文补写失败：%s", str(error)[:120])

    async def _reply_id_for(self, scope_id: str | None, candidate: Any) -> str:
        """引用目标校验：内部编号/QQ 号都接受 → 翻成 QQ 消息号；不确定就返回 ""。

        宁可少一个引用，也不许引用错消息（实录：模型拿内部编号当 QQ 号，
        结果引用了"戳一戳"通知那条消息，看起来像"莫名其妙引用错消息"）。
        """
        value = str(candidate or "").strip()
        if not value or not scope_id or self.storage is None:
            return ""
        try:
            return await self.storage.resolve_reply_target(scope_id, value)
        except Exception as error:
            logger.info("长程记忆：引用目标校验失败（本次不引用）：%s", str(error)[:120])
            return ""

    # ------------------------------------------------------------ 任务队列（统一）
    def _task_handlers(self) -> dict[str, Any]:
        """任务类型 → 执行体。所有"要做事"的路径最终都落在这里。"""
        return {
            KIND_REPLY: self._task_reply,
            KIND_AGENT: self._task_agent,
            KIND_TOOL: self._task_tool,
            KIND_NOTIFY: self._task_notify,
            KIND_REFLECT: self._task_reflect,
            KIND_COMPRESS: self._task_compress,
            KIND_CUSTOM: self._task_custom,
        }

    def _task_scope(self, task: "Task") -> str | None:
        if task.scope_id:
            return task.scope_id
        conversation = str(task.conversation or "").strip()
        if conversation:
            return self._known_scopes.get(conversation)
        return next(iter(self._known_scopes.values()), None)

    async def _task_reply(self, task: "Task") -> str:
        """一次回复：对着指令说一句话（不挂工具，纯拟人短句）。"""
        scope = self._task_scope(task)
        text = f"{task.title} {task.detail}".strip() or "说点什么"
        provider = self.settings.reply_provider_id or ""
        answer = await self._llm_text(
            provider,
            prompt=f"当前会话氛围与最近聊天：见下。要你回应的事：{text}",
            system_prompt=self._agent_reply_system_prompt(await self._persona_prompt()),
            event=None,
        )
        answer = str(answer or "").strip()
        if not answer:
            raise RuntimeError("模型没有产出回复")
        await self._task_send_text(scope, answer)
        return answer[:200]

    async def _task_agent(self, task: "Task") -> str:
        """一轮 agent 任务：带工具循环自主完成，结果发回会话。"""
        scope = self._task_scope(task)
        if not scope:
            raise RuntimeError("没有可用的会话")
        instruction = f"{task.title} {task.detail}".strip()
        result = await self._autonomous_action_loop(
            scope, instruction, f"任务队列（{task.source}）")
        summary = str((result or {}).get("summary") or "").strip()
        if summary:
            await self._task_send_text(scope, summary)
        return summary[:300] or "已完成"

    async def _task_tool(self, task: "Task") -> str:
        """一次工具调用：payload 里给 tool/args。"""
        tool = str(task.payload.get("tool") or task.title or "").strip()
        args = task.payload.get("args") if isinstance(task.payload.get("args"), dict) else {}
        if not tool:
            raise RuntimeError("工具任务缺少 tool")
        return str(await self._dispatch_task_action(tool, args, self._task_scope(task)))[:400]

    async def _task_notify(self, task: "Task") -> str:
        """主动发一条消息到会话。"""
        scope = self._task_scope(task)
        text = f"{task.title} {task.detail}".strip()
        if not text:
            raise RuntimeError("通知任务没有内容")
        await self._task_send_text(scope, text)
        return text[:200]

    async def _task_reflect(self, task: "Task") -> str:
        scope = self._task_scope(task)
        if not scope:
            raise RuntimeError("没有可总结的会话")
        result = await self._self_reflect(scope, str(task.conversation or ""))
        return str(result)[:300]

    async def _task_compress(self, task: "Task") -> str:
        scope = self._task_scope(task)
        if not scope:
            raise RuntimeError("没有可压缩的会话")
        created = await self._compress_scope(scope, rounds=6)
        return f"生成 {created} 条摘要"

    async def _task_custom(self, task: "Task") -> str:
        """拓展脚本自定义任务：payload 给 script/tool/params。"""
        script = str(task.payload.get("script") or "").strip()
        tool = str(task.payload.get("tool") or "").strip()
        if not script or not tool:
            raise RuntimeError("自定义任务需要 script 与 tool")
        params = task.payload.get("params") if isinstance(task.payload.get("params"), dict) else {}
        return str(await self._script_manager().call(script, tool, params))[:400]

    async def _task_send_text(self, scope: str | None, text: str) -> None:
        """把任务产出发到会话（走冻结的发送通道：QQ 网关优先，非 QQ 会话走原生）。"""
        content = " ".join(str(text or "").split())
        if not scope or not content:
            return
        conversation = next((key for key, value in self._known_scopes.items()
                             if value == scope), "")
        if not conversation:
            return
        if conversation.isdigit() and self.gateway is not None:
            self._bind_gateway_client()
            if self.gateway.bot is not None:
                await self.gateway.execute(
                    "send_group_msg", group_id=int(conversation),
                    message=[{"type": "text", "data": {"text": content[:1000]}}])
                return
        if conversation.startswith("private:") and self.gateway is not None:
            user_id = conversation.split("private:", 1)[1]
            if user_id.isdigit():
                self._bind_gateway_client()
                if self.gateway.bot is not None:
                    await self.gateway.execute(
                        "send_private_msg", user_id=int(user_id),
                        message=[{"type": "text", "data": {"text": content[:1000]}}])
                    return
        raise RuntimeError(f"会话 {conversation} 不可发送（不在白名单或通道未就绪）")

    @filter.llm_tool(name="task_add")
    async def task_add_tool(self, event: AstrMessageEvent, task_kind: str = "agent",
                            detail: str = "", title: str = "", args_json: str = "",
                            delay_minutes: int = 0, interval_minutes: int = 0,
                            max_runs: int = 1, daily_window: str = "",
                            not_before: str = "", not_after: str = "",
                            priority: int = 0):
        """给自己排一个任务/日程——**这是你自主行动的主入口**（一切皆任务）。
        kind 说明：reply=到点说一句话；agent=到点带工具自主干一轮（最常用）；
        tool=到点调某个工具（args_json 给参数）；notify=到点发一条消息；reflect=自我总结；
        compress=压缩记忆。
        短期任务：max_runs=1（跑一次）或 2~N（跑几次）；长期任务：给 interval_minutes（定时反复），
        max_runs=-1 表示不限次数。时间区间：not_before/not_after 是绝对有效期（超出不执行），
        daily_window 形如 "09:00-22:00" 表示只在每天的这段时间内执行。

        Args:
            task_kind(string): reply/agent/tool/notify/reflect/compress。
            detail(string): 要做什么（agent 任务就是给它的一段指令）。
            title(string): 任务短标题（列表里显示）。
            args_json(string): kind=tool 时的参数 JSON。
            delay_minutes(number): 多少分钟后第一次执行（默认 0=尽快）。
            interval_minutes(number): 长期任务的执行间隔（分钟；0=不重复）。
            max_runs(number): 最多执行几次；-1 不限（长期定时用 -1）。
            daily_window(string): 每日时段，如 "09:00-22:00"（超出不执行）。
            not_before(string): 有效期起点（ISO 时间，可留空）。
            not_after(string): 有效期终点（ISO 时间，超出即作废）。
            priority(number): -5~5，越大越先执行。
        """
        if self.task_queue is None:
            return "任务队列未启用"
        window: dict[str, Any] = {}
        if not_before.strip():
            window["after"] = not_before.strip()
        if not_after.strip():
            window["before"] = not_after.strip()
        if daily_window.strip() and "-" in daily_window:
            start, end = daily_window.split("-", 1)
            window["daily"] = [start.strip(), end.strip()]
        scope = await self._scope_for_event(event) if event is not None else None
        conversation = self._conversation_key(event) if event is not None else ""
        payload = parse_json_value(args_json) if str(args_json or "").strip() else {}
        task = await self.task_queue.add(
            task_kind, detail=detail, title=title, scope_id=scope or "",
            conversation=conversation, payload=payload if isinstance(payload, dict) else {},
            priority=int(priority or 0), delay_seconds=float(delay_minutes or 0) * 60,
            interval_seconds=int(interval_minutes or 0) * 60, max_runs=int(max_runs),
            window=window, source="bot")
        logger.info("长程记忆：新增任务 %s（%s，%s）", task.task_id[:10], task.kind,
                    "长期" if task.is_long_term else "短期")
        return compact_json({
            "ok": True, "task_id": task.task_id, "kind": task.kind,
            "first_run_at": task.next_run_at,
            "long_term": task.is_long_term, "max_runs": task.max_runs,
            "window": window, "hint": "task_list 可查队列，task_cancel 可撤销",
        }, 1200)

    @filter.llm_tool(name="task_list")
    async def task_list_tool(self, event: AstrMessageEvent, status: str = "",
                             limit: int = 20):
        """查看你的任务队列（谁在排队、跑到第几次、下次什么时候、上次结果/错误）。

        Args:
            status(string): 可选过滤：pending/running/done/failed/cancelled/expired。
            limit(number): 条数，默认 20。
        """
        if self.task_queue is None:
            return "任务队列未启用"
        statuses = [status.strip()] if status.strip() else None
        tasks = await self.task_queue.list(statuses=statuses,
                                           limit=min(max(int(limit), 1), 100))
        stats = await self.task_queue.stats()
        return compact_json({
            "stats": stats,
            "tasks": [{
                "task_id": t.task_id, "kind": t.kind, "title": t.title or t.detail[:40],
                "status": t.status, "next_run_at": t.next_run_at,
                "runs": f"{t.runs_done}/{('∞' if t.max_runs < 0 else t.max_runs)}",
                "interval_minutes": t.interval_seconds // 60,
                "window": t.window, "source": t.source,
                "last_error": t.last_error[:80], "last_result": t.last_result[:80],
            } for t in tasks],
        }, 6000)

    @filter.llm_tool(name="task_cancel")
    async def task_cancel_tool(self, event: AstrMessageEvent, task_id: str):
        """撤销一个任务（暂停排队中的或正在跑的）。

        Args:
            task_id(string): 任务 ID（task_list 里能看到）。
        """
        if self.task_queue is None:
            return "任务队列未启用"
        okay = await self.task_queue.cancel(str(task_id or "").strip())
        return "已撤销" if okay else "没找到这个任务（或它已经结束）"

    @filter.llm_tool(name="task_update")
    async def task_update_tool(self, event: AstrMessageEvent, task_id: str,
                               detail: str = "", interval_minutes: int = -1,
                               max_runs: int = -2, priority: int = -99,
                               delay_minutes: int = -1, daily_window: str = ""):
        """改一个任务（内容/间隔/次数/优先级/下次时间/每日时段）。

        Args:
            task_id(string): 任务 ID。
            detail(string): 新内容（留空不改）。
            interval_minutes(number): 新间隔分钟（-1 不改）。
            max_runs(number): 新次数上限（-2 不改；-1 不限）。
            priority(number): 新优先级（-99 不改）。
            delay_minutes(number): 从现在起多少分钟后执行（-1 不改）。
            daily_window(string): "09:00-22:00"（留空不改）。
        """
        if self.task_queue is None:
            return "任务队列未启用"
        changes: dict[str, Any] = {}
        if detail.strip():
            changes["detail"] = detail.strip()[:2000]
        if int(interval_minutes) >= 0:
            changes["interval_seconds"] = int(interval_minutes) * 60
        if int(max_runs) != -2:
            changes["max_runs"] = int(max_runs)
        if int(priority) != -99:
            changes["priority"] = int(priority)
        if int(delay_minutes) >= 0:
            changes["next_run_at"] = (
                datetime.now(timezone.utc)
                + timedelta(minutes=int(delay_minutes))).isoformat()
        if daily_window.strip() and "-" in daily_window:
            start, end = daily_window.split("-", 1)
            changes["window"] = {"daily": [start.strip(), end.strip()]}
        if not changes:
            return "没有要改的字段"
        okay = await self.task_queue.update(str(task_id or "").strip(), **changes)
        return "已更新" if okay else "没找到这个任务"

    def _plain_text_plan(self, raw: str) -> MessagePlan | None:
        """把 agent 循环的自然对话最终回应变成单段计划（按行拆短气泡）。"""
        text = str(raw or "").strip()
        if not text:
            return None
        # JSON 计划交给 parse_message_plan/salvage 处理，这里只收纯文本
        if text.lstrip().startswith(("{", "```")):
            return None
        from .src.humanization import PlannedAction

        lines = [" ".join(line.strip().split())
                 for line in text.splitlines() if line.strip()][:8]
        lines = [line for line in lines
                 if line and not is_plan_json_leak(line)
                 and not is_local_path_leak(line)
                 and not is_tool_markup_leak(line)
                 and not is_tool_status_narration(line)]
        if not lines:
            return None
        actions = tuple(
            PlannedAction(action="text", text=line) for line in lines
        )
        return MessagePlan("sequence", actions, False)

    async def _fallback_plain_reply(
        self, event: AstrMessageEvent, decision: Decision, image_only: bool
    ) -> MessagePlan | None:
        """Agent 回复失败（超时/坏计划）时的兜底：一次无工具的普通对话直接拿
        一句话正文，避免被@后彻底沉默。仍失败返回 None。"""
        try:
            provider = _provider_id(
                self.settings.reply_provider_id, await self._current_provider(event)
            )
            persona = await self._persona_prompt()
            system = "\n\n".join(
                part for part in (
                    persona,
                    "刚才的多步回复生成失败了。直接输出一句正文回应当前消息："
                    "3~20字、口语、只有一句话；禁止 JSON、禁止解释、禁止换行。",
                ) if part
            )
            payload: dict[str, Any] = {
                "current_message": event.get_message_str(),
                "decision_intent": decision.intent,
            }
            if image_only:
                payload["note"] = "你看不到图片内容，用表情式短语回应即可。"
            prompt = compact_json(payload, 4000)
            async with asyncio.timeout(30):
                raw = await self._llm_text(
                    provider, event=event, prompt=prompt, system_prompt=system
                )
        except Exception as error:
            logger.warning(
                "长程记忆：%s兜底回复也失败：%s",
                self._conversation_key(event),
                f"{type(error).__name__}: {error}"[:200],
            )
            return None
        text = " ".join(str(raw or "").split())[:300].strip()
        if not text:
            return None
        from .src.humanization import PlannedAction

        return MessagePlan("single", (PlannedAction(action="text", text=text),), False)

    async def _decision_analysis(self, event: AstrMessageEvent, context: str,
                                 decision: Decision) -> dict[str, Any] | None:
        """需求梳理子agent（决策层）：自由形式的需求理解与执行建议。

        设计原则（用户定的）：完全的子agent决策，不做固定任务分类——产出是
        understanding/plan/reply_strategy 的自由 JSON，代码里没有任何按类别
        分支的路由；plan 是给主agent的建议，做不做、怎么做由主agent定。
        是否走到这里由判定器（LLM）的 needs_plan / action=agent 决定。
        """
        if not self.settings.decision_subagent_enabled:
            return None
        message = " ".join(str(event.get_message_str() or "").split())
        if not message:
            return None
        provider = _provider_id(
            self.settings.judge_provider_id or self.settings.reply_provider_id,
            await self._current_provider(event))
        if not provider:
            return None
        payload = compact_json({
            "message": message[:600],
            "judge_action": decision.action,
            "judge_intent": decision.intent,
            "context": str(context)[:1200],
            "ssh_ready": self._ssh_configured(),
        }, 3000)
        try:
            async with asyncio.timeout(120):
                raw = await self._llm_text(
                    provider,
                    event=event,
                    prompt=payload,
                    system_prompt=REQUEST_ANALYSIS_SYSTEM_PROMPT,
                    tools=self._subagent_tool_set(event, analyzer=True),
                    agent=True,
                    max_steps=self.settings.decision_subagent_max_steps,
                )
        except Exception as error:
            logger.info(
                "长程记忆：%s需求梳理子agent失败（主agent自行判断）：%s",
                self._conversation_key(event),
                f"{type(error).__name__}: {str(error)[:140]}")
            return None
        data = parse_json_object(raw or "")
        # 唯一的硬性要求：understanding 非空——其余全是自由形式
        if not isinstance(data, dict) or not str(data.get("understanding") or "").strip():
            return None
        return data

    async def _execute_decision(
        self,
        event: AstrMessageEvent,
        scope_id: str,
        message_id: str,
        context: str,
        decision: Decision,
        permit: InteractionPermit,
        image_only: bool = False,
        image_urls: list[str] | None = None,
        batch_mode: bool = False,
    ) -> None:
        # 需求梳理子agent（决策层）：要不要梳理由判定器（LLM）的 needs_plan 决定
        # ——不是关键词。梳理产出自由形式的 understanding/plan/reply_strategy，
        # 全部注入回复上下文，由主agent自己决定怎么做（没有固定类别路由）。
        analysis: dict[str, Any] | None = None
        if decision.action in {"text", "agent", "text_sticker"} and not image_only \
                and (decision.action == "agent" or decision.needs_plan):
            analysis = await self._decision_analysis(event, context, decision)
            if analysis is not None:
                logger.info(
                    "长程记忆：%s需求梳理完成（%d 步建议）",
                    self._conversation_key(event),
                    len(analysis.get("plan") or []))
        if decision.action == "reaction" and self.settings.reactions_enabled:
            emoji = decision.emoji_id or next(iter(self.settings.allowed_emoji_ids), "76")
            await set_msg_emoji_like(event.bot, event.message_obj.message_id, emoji)
            return
        if decision.action == "poke" and self.settings.pokes_enabled:
            await self._poke_back(event, event.get_sender_id(), event.get_group_id())
            return
        if decision.action == "sticker" and self.stickers:
            sticker = None
            wanted = str(getattr(decision, "query", "") or "").strip()
            if wanted:
                picked = self.stickers.pick(wanted, 1)
                if picked:
                    sticker = self.stickers.resolve(str(picked[0]["sticker_id"]))
            if sticker is None:
                recent = self.stickers.select(limit=1)
                sticker = recent[0] if recent else None
            if sticker is not None:
                await event.send(MessageChain().file_image(
                    self.stickers.send_path(sticker)))
                try:
                    await self.stickers.note(sticker.sha256, bump_use=True)
                except Exception:
                    pass
            return

        plan: MessagePlan | None = None
        if decision.segments:
            try:
                plan = parse_message_plan(
                    compact_json({"mode": "sequence", "segments": decision.segments}, 4000),
                    self._humanization,
                )
            except (PlanValidationError, ValueError):
                plan = None
        if plan is None:
            try:
                plan = await self._reply_plan(event, scope_id, context, decision,
                                              image_only, image_urls, analysis=analysis)
            except Exception as error:
                logger.warning(
                    "长程记忆：%s拟人回复生成失败：%s",
                    self._conversation_key(event),
                    f"{type(error).__name__}: {error}"[:300],
                )
                plan = await self._fallback_plain_reply(event, decision, image_only)
                if plan is None:
                    return
        if (
            not self._is_private_chat(event)
            and plan.segments
            and plan.segments[0].action == "text"
            and not any(s.reply_to_message_id for s in plan.segments)
        ):
            from .src.humanization import PlannedAction

            first = plan.segments[0]
            plan = MessagePlan(
                plan.mode,
                (
                    PlannedAction(
                        action=first.action, text=first.text,
                        sticker_id=first.sticker_id, face_id=first.face_id,
                        target_id=first.target_id, emoji_id=first.emoji_id,
                        query=first.query, delay_seconds=first.delay_seconds,
                        typing_delay_seconds=first.typing_delay_seconds,
                        reply_to_message_id=await self._reply_id_for(
                            scope_id, event.message_obj.message_id),
                        at=first.at,
                    ),
                    *plan.segments[1:],
                ),
                plan.explicit_segments,
            )
        # 攒批回复的段数上限：真人扫一眼聊天记录只说一两句，不会逐条作答。
        # 提示词之外再加一道程序兜底（实录："窗口内一条一条详细回复"）。
        if batch_mode and plan.segments:
            text_segments = [s for s in plan.segments if s.action == "text"]
            if len(text_segments) > 3:
                logger.info(
                    "长程记忆：攒批回复%d段超出上限，收敛为3段（只留最值得接的）",
                    len(text_segments))
                kept = 0
                trimmed = []
                for segment in plan.segments:
                    if segment.action == "text":
                        kept += 1
                        if kept > 3:
                            continue
                    trimmed.append(segment)
                # 注意：这里不能局部 import MessagePlan——函数内 import 会把它变成
                # 局部名，遮蔽模块级导入，导致前面用到它的分支 UnboundLocalError
                plan = MessagePlan(plan.mode, tuple(trimmed), plan.explicit_segments)

        # 长文本自动转图：必须在 humanize 拆条之前按"总字数"判断——
        # 拆条后每条都低于阈值，逐条检查永远不触发（v0.17.1 的顺序 bug）。
        image_sent = False
        t2i_threshold = self.settings.text_to_image_threshold
        text_total = sum(len(s.text or "") for s in plan.segments if s.action == "text")
        if t2i_threshold > 0 and text_total > t2i_threshold:
            logger.info(
                "长程记忆：%s文本%d字≥阈值%d，尝试整条渲染为图片",
                self._conversation_key(event), text_total, t2i_threshold,
            )
            try:
                joined = "\n".join(
                    (s.text or "") for s in plan.segments if s.action == "text"
                )
                rendered = str(
                    await self.text_to_image(joined, return_url=True) or ""
                ).strip()
                if rendered:
                    import astrbot.api.message_components as Comp

                    chain = MessageChain()
                    first_segment = plan.segments[0]
                    if first_segment.reply_to_message_id:
                        reply_id = await self._reply_id_for(
                            scope_id, first_segment.reply_to_message_id)
                        if reply_id:
                            try:
                                chain.chain.append(Comp.Reply(id=reply_id))
                            except Exception:
                                pass
                    if rendered.startswith(("http://", "https://")):
                        chain.url_image(rendered)
                    else:
                        chain.file_image(rendered)
                    await event.send(chain)
                    logger.info(
                        "长程记忆：长文本已渲染为图片发送（%d字符）", text_total
                    )
                    await self._remember_own_reply(event, scope_id, message_id, plan)
                    image_sent = True
                else:
                    logger.info("长程记忆：text_to_image 返回空，降级分条文本")
            except Exception as error:
                logger.info(
                    "长程记忆：长文本转图失败，降级为分条文本：%s", str(error)[:160]
                )
        if image_sent:
            return
        logger.info(
            "长程记忆：%s拟人回复计划=%d段", self._conversation_key(event), len(plan.segments)
        )
        plan = self._text_first_plan(self._merge_split_sticker_plan(plan))
        sender = HumanizedSender(
            lambda descriptor: self._send_segment(event, descriptor),
            config=self._humanization,
        )
        send_result = await sender.send(
            plan,
            is_cancelled=lambda: self._stopping
            or time.monotonic() < self._send_blockade.get(scope_id, 0.0),
        )
        if send_result.status == "failed" and send_result.failed_index is not None:
            failed_outcome = next(
                (o for o in send_result.outcomes if o.status == "failed"), None
            )
            if failed_outcome and self._looks_like_risk_control(
                f"{failed_outcome.error_type} {failed_outcome.error_message}"
            ):
                self._send_blockade[scope_id] = time.monotonic() + 600
                logger.warning(
                    "长程记忆：%s触发QQ风控（retcode 1200），暂停向该会话发送10分钟",
                    self._conversation_key(event),
                )
        if send_result.status == "completed":
            logger.info("长程记忆：%s已发送%d段回复", self._conversation_key(event), send_result.sent_count)
            # 把自己的发言写进记忆，否则"刚说的话"永远不在上下文里
            await self._remember_own_reply(event, scope_id, message_id, plan)
        else:
            failed_outcome = next(
                (o for o in send_result.outcomes if o.status == "failed"), None
            )
            logger.warning(
                "长程记忆：%s发送未完成 status=%s sent=%d failed_index=%s reason=%s detail=%s",
                self._conversation_key(event), send_result.status, send_result.sent_count,
                send_result.failed_index, send_result.reason,
                f"{failed_outcome.error_type}: {failed_outcome.error_message}"
                if failed_outcome else "无",
            )

    async def _remember_own_reply(
        self, event: AstrMessageEvent, scope_id: str, message_id: str,
        plan: MessagePlan,
    ) -> None:
        """Ingest the bot's own reply so later turns can recall it."""
        if not self.ingest or not self.storage:
            return
        try:
            from .src.models import NormalizedMessage

            reply_text = " ".join(
                piece for piece in (
                    segment.text for segment in plan.segments if segment.action == "text"
                ) if piece
            ).strip()
            if not reply_text:
                return
            upstream = f"self:{message_id}"
            raw_self = normalize_raw_event(event) or {}
            await self.ingest.ingest(NormalizedMessage(
                platform="aiocqhttp",
                account_id=str(raw_self.get("self_id") or event.get_self_id() or ""),
                conversation_id=self._conversation_key(event),
                upstream_message_id=upstream,
                sender_id=str(event.get_self_id() or "self"),
                sender_name="我",
                text=reply_text[:2000],
                occurred_at=datetime.now(timezone.utc).isoformat(),
                raw_event={"message_id": upstream, "derived": True, "self": True},
                parts=[], event_type="message.created",
            ), f"self:{scope_id}:{message_id}")
        except Exception as error:
            logger.info("长程记忆：自身回复入记忆跳过：%s", str(error)[:120])

    @staticmethod
    def _looks_like_risk_control(error_text: str) -> bool:
        return (
            "1200" in error_text
            or 'result": 120' in error_text
            or "result: 120" in error_text
            or "风控" in error_text
        )

    _STICKER_PLACEHOLDER_RE = re.compile(
        r"\[\s*(?:sticker|表情|表情包)\s*[:：]?\s*([0-9a-fA-F]{16,64})\s*\]?",
        re.IGNORECASE)
    # "悬挂的占位开头"：实录模型把 [sticker: 和 <sha>] 拆成两条消息发出去，
    # 任何一半单独看都不是完整占位 → 发送前要把相邻两段拼起来再判断
    _STICKER_DANGLING_RE = re.compile(
        r"\[\s*(?:sticker|表情|表情包)\s*[:：]?\s*$", re.IGNORECASE)
    _STICKER_TAIL_RE = re.compile(r"^\s*([0-9a-fA-F]{16,64})\s*\]?\s*$")

    async def _send_sticker_placeholder(self, event: AstrMessageEvent,
                                        sticker_id: str) -> bool:
        """把模型写出的 [sticker: <id>] 变成真的表情包发送。"""
        stickers = getattr(self, "stickers", None)
        if stickers is None:
            return False
        record = None
        try:
            record = stickers.resolve(sticker_id)
        except Exception:
            for item in stickers.select(limit=200):
                if str(getattr(item, "sha256", "")).startswith(sticker_id[:16]):
                    record = item
                    break
        if record is None:
            return False
        try:
            await event.send(MessageChain().file_image(stickers.send_path(record)))
        except Exception as error:
            logger.info("长程记忆：表情占位转真发送失败：%s", str(error)[:120])
            return False
        try:
            await stickers.note(record.sha256, bump_use=True)
        except Exception:
            pass
        return True

    _MEDIA_ACTIONS = {"sticker", "image", "file", "record", "video"}

    def _text_first_plan(self, plan: Any) -> Any:
        """把所有媒体段挪到文字之后，媒体之间、文字之间各自保持原顺序。

        实录（用户截图）：回复是「文字 → 表情 → 文字 → 表情」，表情被夹在两句中间。
        用户要的是**别在文字之间夹图片/表情**：先说完整段话，再发图/表情。
        稳定分区，不改任何一段的内容与延迟。
        """
        segments = list(getattr(plan, "segments", ()) or [])
        if len(segments) < 2:
            return plan
        texts: list[Any] = []
        media: list[Any] = []
        for item in segments:
            kind = str(getattr(item, "action", ""))
            (media if kind in self._MEDIA_ACTIONS else texts).append(item)
        if not media or not texts:
            return plan
        ordered = texts + media
        if ordered == segments:
            return plan
        try:
            import dataclasses

            return dataclasses.replace(plan, segments=tuple(ordered))
        except Exception:
            return plan

    def _merge_split_sticker_plan(self, plan: Any) -> Any:
        """把被拆成两段的 [sticker: … ] 占位拼回一段（计划段级）。

        实录（用户截图）：一个气泡是"……你嘴硬 [sticker:"、下一个气泡是
        "9dc80a0b…218]"。分开发出去就是两行源码，必须拼起来当成一次表情发送。
        """
        segments = list(getattr(plan, "segments", ()) or [])
        if not segments:
            return plan
        merged: list[Any] = []
        index = 0
        changed = False
        while index < len(segments):
            item = segments[index]
            text = str(getattr(item, "text", "") or "")
            if (str(getattr(item, "action", "")) == "text"
                    and self._STICKER_DANGLING_RE.search(text)
                    and index + 1 < len(segments)
                    and str(getattr(segments[index + 1], "action", "")) == "text"):
                tail = str(getattr(segments[index + 1], "text", "") or "")
                if self._STICKER_TAIL_RE.match(tail):
                    import dataclasses

                    merged.append(dataclasses.replace(
                        item, text=(text + tail.strip())[:2000]))
                    index += 2
                    changed = True
                    continue
            merged.append(item)
            index += 1
        if not changed:
            return plan
        try:
            import dataclasses

            return dataclasses.replace(plan, segments=tuple(merged))
        except Exception:
            return plan

    async def _send_segment(self, event: AstrMessageEvent, descriptor: dict[str, Any]) -> None:
        action = descriptor.get("action")
        if action == "text":
            raw_text = str(descriptor.get("text", ""))
            # 实录：模型把 sticker_id 当文本发了出来（"[sticker: 36ae…]"）。
            # 整段就是一个占位 → 直接改发真表情；混在正文里 → 摘掉占位再发文字。
            placeholder = self._STICKER_PLACEHOLDER_RE.search(raw_text)
            if placeholder:
                whole = self._STICKER_PLACEHOLDER_RE.fullmatch(raw_text.strip())
                if whole:
                    if await self._send_sticker_placeholder(event, whole.group(1)):
                        return
                raw_text = self._STICKER_PLACEHOLDER_RE.sub("", raw_text).strip()
                if not raw_text:
                    return
                descriptor["text"] = raw_text   # 必须写回：后面按 descriptor 取正文
            # 整包计划/工具 JSON（{"thought":…}/{"mode":…} 等）当正文发出去就是严重
            # 穿帮——旧守卫只查 {"mode"/{"action" 两个前缀，{"thought" 直接漏网（实录）
            if is_plan_json_leak(raw_text):
                logger.info("长程记忆：拦截疑似未解析的计划JSON，跳过发送")
                return
            import astrbot.api.message_components as Comp

            chain = MessageChain()
            if descriptor.get("reply_to_message_id"):
                reply_id = await self._reply_id_for(
                    await self._scope_for_event(event), descriptor["reply_to_message_id"])
                if reply_id:
                    try:
                        chain.chain.append(Comp.Reply(id=reply_id))
                    except Exception:
                        pass
                else:
                    logger.info("长程记忆：引用目标无法确认，取消本条引用（%s）",
                                str(descriptor["reply_to_message_id"])[:24])
            at_targets: list[str] = [str(t) for t in (descriptor.get("at") or []) if str(t).strip()]
            for target in _extract_at_targets(str(descriptor.get("text", ""))):
                if target not in at_targets:
                    at_targets.append(target)
            for target in at_targets[:5]:
                try:
                    chain.chain.append(Comp.At(qq=str(target)))
                except Exception:
                    pass
            bubble = _bubble_text(descriptor.get("text", ""))
            if not bubble:
                logger.info("长程记忆：跳过空文本段")
                return
            # 长文本转图：超过阈值时调用 text_to_image 渲染成图片
            threshold = self.settings.text_to_image_threshold
            if threshold > 0 and len(bubble) > threshold:
                try:
                    img_url = await self.text_to_image(bubble, return_url=True)
                    if img_url:
                        await event.send(chain.image(img_url))
                        logger.info("长程记忆：长文本已转图 (%d chars -> image)", len(bubble))
                        return
                except Exception as error:
                    logger.info("长程记忆：text_to_image 失败，降级发送纯文本：%s", str(error)[:120])
            await event.send(chain.message(bubble))
        elif action == "sticker" and self.stickers:
            sticker = self.stickers.resolve(str(descriptor.get("sticker_id") or ""))
            if sticker is None:
                recent = self.stickers.select(limit=1)
                sticker = recent[0] if recent else None
            if sticker is not None:
                await event.send(MessageChain().file_image(
                    self.stickers.send_path(sticker)))
                try:
                    await self.stickers.note(sticker.sha256, bump_use=True)
                except Exception:
                    pass
        elif action in {"image", "file", "record", "video"}:
            await self._send_media_segment(event, descriptor)
        elif action == "face":
            import astrbot.api.message_components as Comp

            chain = MessageChain()
            chain.chain.append(Comp.Face(id=int(descriptor.get("face_id") or 14)))
            await event.send(chain)
        elif action == "poke":
            target = str(descriptor.get("target_id") or event.get_sender_id())
            await send_poke(event.bot, target, event.get_group_id())
        elif action == "reaction":
            emoji = descriptor.get("emoji_id") or next(iter(self.settings.allowed_emoji_ids), "76")
            await set_msg_emoji_like(event.bot, event.message_obj.message_id, str(emoji))

    def _origin_prefixes(self) -> list[str]:
        """前缀直通名单；origin 拓展停用时仅保留 "/"（别封死管理命令）。"""
        raw = str(getattr(self.settings, "origin_wake_prefixes", "") or "/")
        if self._extensions is not None and not self._extensions.is_enabled("origin"):
            raw = "/"
        return [prefix.strip() for prefix in raw.split(",") if prefix.strip()]

    def _effective_tool_set(self, event: AstrMessageEvent) -> ToolSet:
        """All active AstrBot/plugin tools, minus tools of disabled packs.

        `get_full_tool_set()` already contains AstrBot's built-in tools and
        every installed plugin's tools; the extension registry only gates
        which of this plugin's own packs are enabled right now.
        """
        # 面板刚改的配置（如 SSH）要能立刻影响工具门控，不必等插件重载
        self._refresh_settings_if_changed()
        manager = self.context.get_llm_tool_manager()
        source = manager.get_full_tool_set()
        sub_tools = {"dispatch_subagent", "dispatch_parallel_subagents"}
        sub_ok = bool(getattr(self.settings, "subagent_enabled", False))
        ssh_tools = {"ssh_exec", "ssh_info", "ssh_agent_dispatch",
                     "ssh_agent_status", "ssh_agent_control",
                     "ssh_fetch", "ssh_screenshot"}
        ssh_ok = self._ssh_configured()
        script_tools = {"script_call", "script_list"}
        script_ok = bool(self._scripts and self._scripts.extensions)
        active = [
            tool for tool in source
            if getattr(tool, "active", True)
            and (self._extensions is None
                 or self._extensions.is_tool_enabled(str(getattr(tool, "name", ""))))
            and (sub_ok or str(getattr(tool, "name", "")) not in sub_tools)
            and (ssh_ok or str(getattr(tool, "name", "")) not in ssh_tools)
            and (script_ok or str(getattr(tool, "name", "")) not in script_tools)
        ]
        try:
            return ToolSet(tools=active)
        except TypeError:
            return ToolSet(active)

    def _all_active_tools(self) -> list[Any]:
        """当前真正可调用的全部工具（AstrBot 内置 + 其它插件 + 本插件）。"""
        try:
            return list(getattr(self._effective_tool_set(None), "tools", []) or [])
        except Exception:
            return []

    def _capability_digest(self) -> dict[str, Any]:
        """工具清单摘要：分来源给出名字（描述太占预算，细节交给 find_tools）。"""
        from .src.extensions import known_tool_names

        own = known_tool_names()
        astrbot: list[str] = []
        plugins: list[str] = []
        for tool in self._all_active_tools():
            name = str(getattr(tool, "name", ""))
            if not name:
                continue
            (astrbot if name in own else plugins).append(name)
        return {
            "own": sorted(astrbot),
            "external": sorted(plugins),
            "total": len(astrbot) + len(plugins),
        }

    @filter.llm_tool(name="find_tools")
    async def find_tools_tool(self, event: AstrMessageEvent, query: str = "",
                              source: str = ""):
        """按关键词搜"你现在能调用的工具"——包括 AstrBot 内置能力、其它插件的工具、
        本插件工具与自学习技能（知识库/联网搜索/文档处理/图片/群文件/空间 等都在里面）。
        拿不准该用什么工具时先搜一下，比凭记忆猜工具名可靠。

        Args:
            query(string): 关键词，如 知识库、搜索、图片、文件、群相册、定时。
                留空则按来源列出全部工具名。
            source(string): 可选过滤：astrbot（内置）/ plugin（本插件）/ other（其他插件）。
        """
        from .src.extensions import known_tool_names

        tools = self._all_active_tools()
        own = known_tool_names()
        tokens = _tool_query_tokens(query)
        wanted = str(source or "").strip().lower()

        def _bucket(name: str) -> str:
            if name in own:
                return "astrbot"
            return "other"

        rows: list[dict[str, Any]] = []
        for tool in tools:
            name = str(getattr(tool, "name", ""))
            if not name:
                continue
            bucket = _bucket(name)
            if wanted and bucket != wanted:
                continue
            description = str(getattr(tool, "description", "") or "")
            haystack = (name + " " + description).lower()
            if tokens and not any(token in haystack for token in tokens):
                continue
            params: list[str] = []
            raw_args = getattr(tool, "parameters", None) or getattr(tool, "args", None)
            if isinstance(raw_args, Mapping):
                params = list(raw_args)[:12]
            elif isinstance(raw_args, (list, tuple)):
                params = [str(getattr(item, "name", item)) for item in raw_args][:12]
            rows.append({"name": name, "source": bucket, "description": description[:200],
                         "params": params})
        if not tokens:
            digest = self._capability_digest()
            own = digest["own"] if wanted in ("", "astrbot", "plugin") else []
            other = digest["external"] if wanted in ("", "other") else []
            return compact_json({"count": len(own) + len(other),
                                 "astrbot": own, "other": other,
                                 "hint": "给 query 关键词可查具体工具的参数与说明"}, 6000)
        rows.sort(key=lambda item: (item["source"], item["name"]))
        return compact_json({"matched": len(rows), "tools": rows[:40]}, 6000)

    def _subagent_tool_set(self, event: AstrMessageEvent | None,
                           *, analyzer: bool = False) -> ToolSet:
        """子agent工具集：执行型=除浏览器+派发递归外全部；analyzer（决策层）只读。

        实录驱动的两个原则：①子agent要能真正干活（用户明确要求扩大到除 browser
        use 外全部操作）；②需求分析层绝不发消息/改状态——它只产出结构化结论。
        """
        self._refresh_settings_if_changed()
        base = list(getattr(self._effective_tool_set(event), "tools", []) or [])
        blocked = _ANALYZER_BLOCKED_TOOLS if analyzer else _SUBAGENT_BLOCKED_TOOLS
        active = [
            tool for tool in base
            if str(getattr(tool, "name", "")) not in blocked
        ]
        try:
            return ToolSet(tools=active)
        except TypeError:
            return ToolSet(active)

    def _tool_inventory_prompt(self) -> str:
        """Tell the model which tools exist so it actually uses them.

        Weak models ignore bare tool schemas; an explicit inventory in the
        system prompt is what makes them reach for AstrBot/plugin tools.
        """
        try:
            tool_set = self._effective_tool_set(None)
        except Exception:
            return ""
        tools = list(getattr(tool_set, "tools", []) or [])
        if not tools:
            return ""
        # 省 token：清单只留"名字 + 一句话"，并设总长上限。
        # 实录：清单越滚越长（AstrBot 内置 + 各插件 + 拓展），每轮都重复灌进上下文，
        # 模型反而抓不住重点、还挤掉了记忆与任务说明。
        lines: list[str] = []
        budget = 1600
        used = 0
        for tool in tools[:60]:
            name = str(getattr(tool, "name", "") or "")
            if not name:
                continue
            description = str(getattr(tool, "description", "") or "").strip()
            description = re.sub(r"\s+", " ", description)[:42]
            line = f"- {name}：{description}" if description else f"- {name}"
            if used + len(line) > budget:
                break
            used += len(line)
            lines.append(line)
        # 已装自定义拓展（HTTP 工具经 extension_call 调用）
        if self._extensions is not None:
            pairs = self._extensions.dynamic_tools()
            if pairs:
                lines.append("【已装拓展（用 extension_call 调用）】")
                for extension, tool in pairs[:6]:
                    lines.append(
                        f"- {extension.id}.{tool.name}：{tool.description[:60]}"
                    )
        # scripts/ 拓展工具（经 script_call 调用）
        script_tools = self._script_manager().tool_catalog()
        if script_tools:
            lines.append("【scripts 拓展工具（用 script_call(script, tool, params_json) 调用）】")
            for row in script_tools[:8]:
                params = "、".join(
                    f"{key}:{value}" for key, value in
                    (row.get("params") or {}).items()) if row.get("params") else "无参数"
                lines.append(
                    f"- {row['name']}（{row['script']}）：{row['description'][:70]}"
                    f"  参数：{params}"
                )
        if not lines:
            return ""
        return (
            "【可用工具（本回合真实可调用；名字+一句话）】\n"
            + "\n".join(lines)
            + "\n需要细节用 find_tools(query) 查；要用就直接发起调用，别在文本里描述调用。"
        )

    # ---------------------------------------------------- 媒体段直发（bot 自主选择）
    _MEDIA_SEND_LIMIT = 8 * 1024 * 1024
    _MEDIA_KIND_EXT = {
        "image": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "ico"},
        "video": {"mp4", "mkv", "mov", "avi", "webm", "flv", "wmv"},
        "record": {"amr", "silk", "mp3", "wav", "m4a", "ogg", "aac", "flac", "wma"},
        "file": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md",
                 "zip", "rar", "7z", "tar", "gz", "json", "csv", "py", "js", "html",
                 "xml", "yaml", "yml", "log", "exe", "apk", "ipa"},
    }

    @classmethod
    def _media_kind_by_name(cls, name: str, fallback: str = "file") -> str:
        suffix = Path(str(name or "")).suffix.lower().lstrip(".")
        for kind, extensions in cls._MEDIA_KIND_EXT.items():
            if suffix in extensions:
                return kind
        return fallback

    async def _resolve_send_path(self, descriptor: Mapping[str, Any]) -> str:
        """把媒体段描述符（media_id 或 path）解析为本地可读文件路径。"""
        media_id = str(descriptor.get("media_id") or "").strip()
        if media_id and self.media is not None:
            try:
                return await self.media.get_path(media_id)
            except Exception as error:
                logger.info("长程记忆：媒体段取文件失败（%s）：%s",
                            media_id[:12], str(error)[:120])
                return ""
        raw_path = str(descriptor.get("path") or "").strip()
        if not raw_path:
            return ""
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            # 相对路径不只认插件 workspace：AstrBot 自带工具写进 data/workspaces/… 的文件
            # 也要找得到（实录：run_script("pelican_gif.py") 报"脚本不存在"、其实在别处）
            try:
                found = self._workspace().resolve_existing(candidate)
                if found.is_file():
                    return str(found)
            except Exception:
                pass
            candidate = self._workspace().root / candidate
        try:
            candidate = candidate.resolve()
        except OSError:
            return ""
        if candidate.is_file():
            return str(candidate)
        logger.info("长程记忆：媒体段路径不存在：%s", str(candidate)[:120])
        return ""

    def _sticker_scaled_path(self, path: "Path") -> "Path | None":
        """如果这张图是表情包库存里的，返回缩放后的发送路径（否则 None）。"""
        stickers = getattr(self, "stickers", None)
        if stickers is None:
            return None
        try:
            root = Path(getattr(stickers, "root", "")).resolve()
        except Exception:
            return None
        try:
            target = Path(path).resolve()
        except Exception:
            return None
        try:
            target.relative_to(root)
        except ValueError:
            return None
        if target.parent.name == "_send":
            return None
        try:
            from .src.stickers import StickerRecord

            record = StickerRecord(target.stem, str(target), "image/png",
                                   target.stat().st_size)
            scaled = Path(stickers.send_path(record))
        except Exception:
            return None
        return scaled if str(scaled) != str(target) else None

    async def _send_media_file(self, path: str, kind: str, *, group_id: str = "",
                               user_id: str = "", file_name: str = "") -> dict[str, Any]:
        """把本地文件按类型（image/file/record/video）base64 直发到指定目标。"""
        if self.gateway is None:
            raise RuntimeError("QQ 网关未就绪")
        p = Path(path)
        # 表情包无论从哪个入口发（sticker 段/图片段/send_local_file），
        # 都先缩到表情包规格——实录"发表情包直接发大图，比例非常大"
        scaled = self._sticker_scaled_path(p)
        if scaled is not None:
            p = scaled
        data = await asyncio.to_thread(p.read_bytes)
        if len(data) > self._MEDIA_SEND_LIMIT:
            raise RuntimeError(
                f"文件 {len(data)} 字节，超过发送上限 "
                f"{self._MEDIA_SEND_LIMIT // (1024 * 1024)}MB")
        encoded = base64.b64encode(data).decode("ascii")
        name = str(file_name or "").strip() or p.name
        if kind == "image":
            segment = {"type": "image", "data": {"file": "base://" + encoded}}
        elif kind == "record":
            segment = {"type": "record", "data": {"file": "base://" + encoded}}
        elif kind == "video":
            segment = {"type": "video", "data": {"file": "base://" + encoded}}
        else:
            segment = {"type": "file",
                       "data": {"file": "base://" + encoded, "name": name}}
        self._bind_gateway_client()
        group = str(group_id or "").strip()
        user = str(user_id or "").strip()
        if group:
            await self.gateway.execute(
                "send_group_msg", group_id=int(group), message=[segment])
        elif user:
            await self.gateway.execute(
                "send_private_msg", user_id=int(user), message=[segment])
        else:
            raise ValueError("需要 group_id 或 user_id")
        logger.info("长程记忆：媒体已发送（%s，%d 字节，type=%s → %s）",
                    p.name, len(data), segment["type"], group or user)
        return {"ok": True, "type": segment["type"], "bytes": len(data),
                "target": group or user, "via": "gateway", "file": name}

    @staticmethod
    def _as_qq_number(value: Any) -> int | None:
        """群号/QQ 号必须是纯数字；webchat 的会话 id（如 Rikka0612）不是。"""
        text = str(value or "").strip()
        return int(text) if text.isdigit() and len(text) <= 12 else None

    async def _send_media_native(self, event: AstrMessageEvent, path: str,
                                 kind: str, file_name: str = "") -> None:
        """用 AstrBot 原生发送把媒体发进当前会话（webchat/qqofficial 等非 OneBot 平台）。

        NapCat 网关只认数字 QQ 目标——实录 webchat 会话 id 是 `Rikka0612`，
        `int()` 转换当场炸掉（"发送失败：invalid literal for int() with base 10"），
        所以当前会话一律优先走平台原生通道。
        """
        import astrbot.api.message_components as Comp

        p = Path(path)
        scaled = self._sticker_scaled_path(p) if kind == "image" else None
        if scaled is not None:
            p = scaled
        name = str(file_name or "").strip() or p.name
        chain = MessageChain()
        if kind == "image":
            chain.chain.append(Comp.Image.fromFileSystem(str(p)))
        elif kind == "record":
            chain.chain.append(Comp.Record.fromFileSystem(str(p)))
        elif kind == "video":
            chain.chain.append(Comp.Video.fromFileSystem(str(p)))
        else:
            chain.chain.append(Comp.File(name=name, file=str(p)))
        await event.send(chain)
        logger.info("长程记忆：媒体已发送（%s，type=%s，原生通道）", name, kind)

    async def _send_media_segment(self, event: AstrMessageEvent,
                                  descriptor: Mapping[str, Any]) -> None:
        """发送 图片/文件/语音/视频 段：media_id 或本地 path，base64 直发当前会话。"""
        action = str(descriptor.get("action") or "file")
        path = await self._resolve_send_path(descriptor)
        if not path:
            return
        kind = action if action in self._MEDIA_KIND_EXT else self._media_kind_by_name(path)
        file_name = str(descriptor.get("file_name") or "")
        group_id = str(event.get_group_id() or "")
        user_id = "" if group_id else str(event.get_sender_id() or "")
        # 非 OneBot 会话（webchat/qqofficial）没有数字 QQ 目标 → 走平台原生发送
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:
            platform = ""
        if not (platform == "aiocqhttp" and self._as_qq_number(group_id or user_id)):
            try:
                await self._send_media_native(event, path, kind, file_name)
            except Exception as error:
                logger.warning("长程记忆：媒体段原生发送失败（%s）：%s",
                               kind, str(error)[:150])
            return
        if self.gateway is None:
            return
        self._ensure_gateway(event)
        try:
            await self._send_media_file(
                path, kind, group_id=group_id, user_id=user_id,
                file_name=file_name)
        except Exception as error:
            logger.warning("长程记忆：媒体段发送失败（%s）：%s", kind, str(error)[:150])

    # ---------------------------------------------------- 工作区（OpenClaw 文件/exec 移植）
    def _workspace(self) -> Workspace:
        """受限工作区：AstrBot 数据目录（含各插件数据目录）+ 插件 workspace 子目录。
        相对路径以 workspace 为基准；所有操作 resolve 后必须在允许根内。"""
        if self._workspace_obj is None:
            data_root = Path(get_astrbot_data_path())
            if self.storage is not None:
                workspace_dir = self.storage.path.parent / "workspace"
            else:
                workspace_dir = (data_root / "plugin_data"
                                 / "astrbot_plugin_long_memory_agent" / "workspace")
            self._workspace_obj = Workspace([data_root, workspace_dir])
        return self._workspace_obj

    async def _dispatch_workspace_action(self, tool: str,
                                         args: Mapping[str, Any]) -> str:
        """工作区工具的共享实现（llm_tool 与调度通道共用）。"""
        ws = self._workspace()
        try:
            if tool == "fs_list":
                return compact_json(ws.list(str(args.get("path", "."))), 6000)
            if tool == "fs_read":
                return compact_json(ws.read(
                    str(args.get("path", "")),
                    offset=int(args.get("offset", 0) or 0),
                    max_chars=int(args.get("max_chars", 16000) or 16000)), 20000)
            if tool == "fs_write":
                return compact_json(ws.write(
                    str(args.get("path", "")), str(args.get("content", "")),
                    append=bool(args.get("append", False))), 1500)
            if tool == "fs_delete":
                return compact_json(ws.delete(
                    str(args.get("path", "")),
                    recursive=bool(args.get("recursive", False))), 1500)
            if tool == "fs_move":
                return compact_json(ws.move(
                    str(args.get("src", "")), str(args.get("dst", ""))), 1500)
            if tool == "fs_search":
                return compact_json(ws.search(
                    str(args.get("pattern", "")), root=str(args.get("root", ".")),
                    limit=int(args.get("limit", 50) or 50)), 8000)
            if tool == "run_script":
                return compact_json(await ws.run_script(
                    str(args.get("path", "")), args=str(args.get("args", "")),
                    timeout=int(args.get("timeout", 60) or 60)), 14000)
        except WorkspaceError as error:
            return f"{tool} 失败：{error}"
        except Exception as error:
            return f"{tool} 失败：{type(error).__name__}: {str(error)[:150]}"
        return f"unknown workspace tool: {tool}"

    @filter.llm_tool(name="fs_list")
    async def fs_list_tool(self, event: AstrMessageEvent, path: str = "."):
        """列出工作区里的文件与子目录（AstrBot 数据目录/各插件数据目录/workspace）。
        相对路径以插件 workspace 目录为基准。

        Args:
            path(string): 目录或文件路径，默认工作区根。
        """
        return await self._dispatch_workspace_action("fs_list", {"path": path})

    @filter.llm_tool(name="fs_read")
    async def fs_read_tool(self, event: AstrMessageEvent, path: str,
                           offset: int = 0, max_chars: int = 16000):
        """读取工作区里的一个文本文件（大文件用 offset/max_chars 分页读）。

        Args:
            path(string): 文件路径（相对 workspace 或绝对，须在允许目录内）。
            offset(number): 起始字符偏移，默认 0。
            max_chars(number): 本次最多读多少字符，默认 16000。
        """
        return await self._dispatch_workspace_action(
            "fs_read", {"path": path, "offset": offset, "max_chars": max_chars})

    @filter.llm_tool(name="fs_write")
    async def fs_write_tool(self, event: AstrMessageEvent, path: str,
                            content: str, append: bool = False):
        """写入（或追加）工作区里的一个文件，父目录自动创建。

        Args:
            path(string): 文件路径。
            content(string): 要写入的完整内容。
            append(boolean): true 表示追加而不是覆盖。
        """
        return await self._dispatch_workspace_action(
            "fs_write", {"path": path, "content": content, "append": append})

    @filter.llm_tool(name="fs_delete")
    async def fs_delete_tool(self, event: AstrMessageEvent, path: str,
                             recursive: bool = False):
        """删除工作区里的文件或目录（非空目录要 recursive=true）。

        Args:
            path(string): 要删除的路径。
            recursive(boolean): 目录非空时必须为 true。
        """
        return await self._dispatch_workspace_action(
            "fs_delete", {"path": path, "recursive": recursive})

    @filter.llm_tool(name="fs_move")
    async def fs_move_tool(self, event: AstrMessageEvent, src: str, dst: str):
        """移动/重命名工作区里的文件或目录。

        Args:
            src(string): 源路径。
            dst(string): 目标路径（相对路径以 workspace 为基准）。
        """
        return await self._dispatch_workspace_action("fs_move", {"src": src, "dst": dst})

    @filter.llm_tool(name="fs_search")
    async def fs_search_tool(self, event: AstrMessageEvent, pattern: str,
                             root: str = ".", limit: int = 50):
        """按 glob 模式搜索工作区里的文件（如 *.py、**/*.json、日志*）。

        Args:
            pattern(string): glob 模式。
            root(string): 搜索起点目录，默认工作区根。
            limit(number): 最多返回条数（默认50）。
        """
        return await self._dispatch_workspace_action(
            "fs_search", {"pattern": pattern, "root": root, "limit": limit})

    @filter.llm_tool(name="run_script")
    async def run_script_tool(self, event: AstrMessageEvent, path: str,
                              args: str = "", timeout: int = 60):
        """在工作区里运行一个脚本文件（.py/.js/.sh/.bat/.ps1），返回 exit_code 与输出。
        脚本必须位于允许目录内；超时会被掐断（最长 10 分钟）。

        Args:
            path(string): 脚本文件路径。
            args(string): 传给脚本的命令行参数（空格分隔）。
            timeout(number): 超时秒数，默认 60。
        """
        return await self._dispatch_workspace_action(
            "run_script", {"path": path, "args": args, "timeout": timeout})

    @filter.llm_tool(name="read_tabular")
    async def read_tabular_tool(
        self, event: AstrMessageEvent, pandas_operations: str, file_path: str = "",
    ):
        """读取表格数据（CSV/Excel/JSON/TSV）并用 pandas 处理；也可以把它当"带 pandas +
        matplotlib 的 Python 沙箱"用（沙箱里同样有 bot.* 接口）。读文件时表格在变量 df 里，
        最终结果赋给 result 变量或直接 print。要画数据图就在代码里用 plt（已装好中文字体），
        存图用 result = save_chart("图.png")（存进工作区并返回路径），再用 send_local_file 发出。

        Args:
            pandas_operations(string): 要执行的 pandas 代码（结果赋给 result 或 print 出来；
                画图用 plt 并把 save_chart(...) 的返回值赋给 result）。
            file_path(string): 表格文件路径（工作区内可给相对路径）；只想跑 pandas/画图
                代码时留空，不要填 __noop__ 之类的占位符。
        """
        scope = ""
        try:
            scope = await self._scope_for_event(event) or ""
        except Exception:
            scope = ""
        from .src.data_tools import run_tabular

        return await run_tabular(
            pandas_operations,
            file_path=file_path,
            resolve_path=self._workspace().resolve,
            extra_globals=self._sandbox_globals(event, scope),
        )

    @filter.llm_tool(name="send_local_file")
    async def send_local_file_tool(
        self, event: AstrMessageEvent, path: str = "", media_id: str = "",
        kind: str = "", group_id: str = "", user_id: str = "", file_name: str = "",
    ):
        """把本地文件/图片/视频/音频发到聊天（默认发当前会话；群必须在白名单）。
        kind 留空按扩展名自动判断（image/file/record/video）；也可用 media_id 转发
        媒体库里的东西。

        Args:
            path(string): 本地文件路径（工作区内可给相对路径）。
            media_id(string): 媒体库 ID（与 path 二选一）。
            kind(string): 可选 image/file/record/video，留空自动判断。
            group_id(string): 目标群号（默认当前群）。
            user_id(string): 目标QQ（私聊，默认当前对话对象）。
            file_name(string): 发送文件时的显示名。
        """
        self._ensure_gateway(event)
        descriptor: dict[str, Any] = {"path": path, "media_id": media_id}
        resolved = await self._resolve_send_path(descriptor)
        if not resolved:
            return "找不到要发送的文件（path 不存在且 media_id 无效）"
        given_group = str(group_id or "").strip()
        given_user = str(user_id or "").strip()
        if given_group and not self.settings.allows_group(given_group):
            return f"拒绝发送：群 {given_group} 不在白名单"
        chosen = str(kind or "").strip()
        if chosen not in self._MEDIA_KIND_EXT:
            chosen = self._media_kind_by_name(resolved)
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:
            platform = ""
        current_target = str(event.get_group_id() or event.get_sender_id() or "")
        # 当前会话 + 不是 OneBot 的 QQ 目标（webchat 的 "Rikka0612" 之类）→ 平台原生发送；
        # 明确给了 QQ 群/人，或当前就是 OneBot 会话 → NapCat 网关 base64 直发
        explicit = bool(given_group or given_user)
        use_native = not explicit and not (
            platform == "aiocqhttp" and self._as_qq_number(current_target) is not None)
        if use_native:
            try:
                await self._send_media_native(event, resolved, chosen,
                                              str(file_name or ""))
            except Exception as error:
                return f"发送失败：{str(error)[:160]}"
            return compact_json({"ok": True, "via": "astrbot", "type": chosen,
                                 "file": Path(resolved).name}, 1200)
        target_group = given_group
        target_user = given_user
        if not target_group and not target_user:
            current_group = str(event.get_group_id() or "")
            if current_group:
                target_group = current_group
            else:
                target_user = str(event.get_sender_id() or "")
        if self._as_qq_number(target_group or target_user) is None:
            return (f"目标 {target_group or target_user!r} 不是 QQ 号/群号，"
                    "无法经 QQ 网关发送")
        try:
            result = await self._send_media_file(
                resolved, chosen, group_id=target_group, user_id=target_user,
                file_name=str(file_name or ""))
        except Exception as error:
            return f"发送失败：{str(error)[:160]}"
        return compact_json(result, 1200)

    async def _poke_back(self, event: AstrMessageEvent, user_id: Any, group_id: Any) -> None:
        attempts: list[tuple[str, dict[str, Any]]] = []
        if group_id:
            attempts.append((
                "group_poke",
                {"group_id": int(group_id), "user_id": int(user_id), "target_id": int(user_id)},
            ))
        else:
            attempts.append(("friend_poke", {"user_id": int(user_id)}))
        attempts.append((
            "send_poke",
            {"user_id": int(user_id), "target_id": int(user_id),
             **({"group_id": int(group_id)} if group_id else {})},
        ))
        last_error: Exception | None = None
        for action, params in attempts:
            if self.gateway is not None:
                try:
                    await self.gateway.execute(action, **params)
                    return
                except Exception as error:
                    last_error = error
            try:
                if action in {"group_poke", "send_poke"} and group_id:
                    await send_poke(event.bot, user_id, group_id)
                else:
                    await send_poke(event.bot, user_id, event.get_group_id())
                return
            except Exception as error:
                last_error = error
        logger.warning("长程记忆：戳一戳回应失败：%s", last_error)

    async def _handle_poke(self, event: AstrMessageEvent, raw: dict[str, Any]) -> None:
        if not self.settings.pokes_enabled:
            return
        if str(raw.get("target_id")) != str(event.get_self_id()):
            return
        if random.random() < 0.35:
            await self._poke_back(event, raw.get("user_id"), raw.get("group_id"))

    async def _normalize_image_payload(
        self, result: Any, *, limit: int | None = None,
    ) -> dict[str, Any] | None:
        """把 get_image 结果归一成下游能吃的形态（base64 优先）。

        SnowLuma 的 get_image 返回远端 URL（没有本地路径也没有 base64），NapCat
        返回本地路径；而表情学习/图片描述等下游只收本地文件或 base64（拒绝 URL
        是刻意的防 SSRF 设计，实录报错 "URLs are not accepted"）。这里把 URL 按
        需有界下载转成 base64，本地路径原样放行，两者都拿不到时返回 None。
        """
        inner = result.get("data", result) if isinstance(result, Mapping) else None
        if not isinstance(inner, Mapping):
            return None
        encoded = str(inner.get("base64") or "").strip()
        if encoded:
            return {"data": {"base64": encoded.split(",", 1)[-1]}}
        cap = int(limit or self.settings.sticker_max_bytes)
        for key in ("file", "path"):
            value = str(inner.get(key) or "").strip()
            if not value:
                continue
            if value.lower().startswith("file://"):
                value = value[7:]
            if value.lower().startswith(("http://", "https://")):
                try:
                    data_bytes = await fetch_bounded(value, cap)
                except Exception as error:
                    logger.info("长程记忆：get_image 远端内容下载失败：%s", str(error)[:120])
                    return None
                import base64 as _b64

                return {"data": {"base64": _b64.b64encode(data_bytes).decode("ascii")}}
            if Path(value).is_file():
                return result
            return None
        return None

    async def _fetch_image_payload(
        self,
        event: AstrMessageEvent,
        data: dict[str, Any],
        scope_id: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        """get_image first; on failure fall back to the segment's QQ CDN URL."""
        try:
            result = await get_image(event.bot, str(data.get("file", "")))
            normalized = await self._normalize_image_payload(result)
            if normalized is not None:
                return normalized
            logger.info("长程记忆：get_image 结果不可用（无本地/可下载内容），尝试URL回退")
        except Exception as error:
            logger.info("长程记忆：get_image失败，尝试URL回退：%s", error)
        url = str(data.get("url") or "")
        if not url:
            return None
        try:
            data_bytes = await fetch_bounded(url, self.settings.sticker_max_bytes)
        except Exception as error:
            logger.info("长程记忆：图片URL下载失败：%s", error)
            return None
        import base64 as _b64

        return {"data": {"base64": _b64.b64encode(data_bytes).decode("ascii")}}

    async def _describe_sticker(self, record: Any) -> None:
        """Best-effort: store a one-line vision description for semantic picking."""
        if not self.stickers:
            return
        try:
            path = Path(record.path)
            data = path.read_bytes()
            description = await self._describe_images(
                [(record.media_type, base64.b64encode(data).decode("ascii"))]
            )
            if description:
                await self.stickers.note(record.sha256, description=description)
        except Exception as error:
            logger.info("长程记忆：表情描述生成跳过：%s", str(error)[:120])

    async def _learn_stickers(self, event: AstrMessageEvent, raw: dict[str, Any], source_id: str) -> None:
        if not self.stickers:
            return
        message = raw.get("message")
        if not isinstance(message, list):
            return
        for part in message:
            if not isinstance(part, dict) or part.get("type") not in {"image", "mface"}:
                continue
            data = part.get("data") or {}
            summary = str(data.get("summary", ""))
            if "表情" not in summary and data.get("sub_type") not in {1, "1"}:
                continue
            payload = await self._fetch_image_payload(event, data, "", source_id)
            if payload is None:
                logger.warning("长程记忆：表情学习跳过（无法获取图片内容）")
                continue
            try:
                record = await self.stickers.ingest(
                    payload, source_ref=source_id, trusted_get_image=True,
                    metadata={"summary": summary} if summary else None,
                )
                if not summary and self.settings.vision_provider_id:
                    self._spawn(self._describe_sticker(record))
                logger.info("长程记忆：已学习一张表情%s", f"（{summary[:30]}）" if summary else "")
            except Exception as error:
                logger.warning("表情学习失败：%s", error)



    async def _archive_media(
        self,
        event: AstrMessageEvent,
        raw: dict[str, Any],
        scope_id: str,
        message_id: str,
        text: str,
    ) -> None:
        """Archive this message's image/file segments into the media store."""
        if self.media is None:
            return
        message = raw.get("message")
        segments = message if isinstance(message, list) else []
        has_file_segment = any(
            isinstance(part, dict) and part.get("type") in {"file", "record", "video"}
            for part in segments[:10]
        )
        # AstrBot 把文件消息转成 File 组件（raw 段可能缺失，或 raw 段的
        # get_file 失败——SnowLuma 的 file_id 不在图片缓存）——组件归档兜底
        self._file_component_fallback = not has_file_segment
        for part in segments[:10]:
            if not isinstance(part, dict):
                continue
            data = part.get("data") or {}
            if part.get("type") == "image":
                try:
                    result = await get_image(event.bot, str(data.get("file", "")))
                    result = await self._normalize_image_payload(result)
                except Exception as error:
                    logger.info("长程记忆：get_image失败（归档改走URL）：%s", str(error)[:150])
                    result = None
                if result is not None:
                    try:
                        await self.media.ingest_onebot_image(
                            result, scope_id=scope_id, source_message_id=message_id
                        )
                    except Exception as error:
                        logger.warning("长程记忆：图片归档失败：%s", str(error)[:150])
                else:
                    url = str(data.get("url") or "")
                    if url:
                        try:
                            record = await self.media.ingest_url(
                                url, scope_id=scope_id,
                                source_message_id=message_id, note="image fallback",
                            )
                            logger.info("长程记忆：图片归档（URL回退）=%s", record.status)
                        except Exception as error:
                            logger.warning("长程记忆：图片URL归档失败：%s", str(error)[:150])
                    else:
                        logger.warning("长程记忆：图片归档失败：无get_image结果且消息段无URL")
            elif part.get("type") in {"file", "record", "video"} and self.gateway is not None:
                part_kind = str(part.get("type"))
                try:
                    # get_file 的动作规范是 ['file', 'file_id'] 且严格校验要求
                    # 两个参数都在，只传其一会报 missing required params
                    file_ref = str(data.get("file_id") or data.get("file", ""))
                    file_result = await self.gateway.execute(
                        "get_file", file=file_ref, file_id=file_ref
                    )
                    payload = file_result.get("data", file_result) if isinstance(
                        file_result, Mapping) else {}
                    local = str(payload.get("file") or payload.get("path") or "")
                    encoded = str(payload.get("base64") or "")
                    data_bytes: bytes | None = None
                    if local.lower().startswith(("http://", "https://")):
                        try:
                            data_bytes = await fetch_bounded(
                                local, self.media.max_file_bytes)
                        except Exception as error:
                            logger.info("长程记忆：%s 下载失败：%s",
                                        part_kind, str(error)[:120])
                    elif local:
                        path = Path(local).resolve()
                        if path.is_file() and path.stat().st_size <= self.media.max_file_bytes:
                            data_bytes = await asyncio.to_thread(path.read_bytes)
                    elif encoded:
                        import base64 as _b64

                        data_bytes = _b64.b64decode(encoded.split(",", 1)[-1])
                    if data_bytes is None and str(data.get("url") or "").lower().startswith(
                            ("http://", "https://")):
                        # 段自带下载地址（SnowLuma 的 record/video/file 常见）
                        try:
                            data_bytes = await fetch_bounded(
                                str(data.get("url")), self.media.max_file_bytes)
                        except Exception as error:
                            logger.info("长程记忆：%s 段 URL 下载失败：%s",
                                        part_kind, str(error)[:120])
                    if data_bytes is not None:
                        await self.media.save_bytes(
                            data_bytes, scope_id=scope_id, kind=part_kind,
                            source_message_id=message_id,
                            mime=str(payload.get("file_name", "")),
                            note=str(data.get("name", "")),
                        )
                        logger.info("长程记忆：%s已归档（%d 字节）",
                                    {"record": "语音", "video": "视频"}.get(
                                        part_kind, "文件"), len(data_bytes))
                    else:
                        logger.info("长程记忆：%s归档跳过（无本地内容）", part_kind)
                        self._file_component_fallback = True
                except Exception as error:
                    logger.warning("长程记忆：%s归档失败：%s", part_kind, str(error)[:150])
                    self._file_component_fallback = True
        if getattr(self, "_file_component_fallback", False):
            # raw 段缺失或 get_file 失败：从 AstrBot File 组件兜底归档
            try:
                await self._archive_file_components(event, scope_id, message_id)
            except Exception as error:
                logger.warning("长程记忆：文件组件归档失败：%s", str(error)[:150])

    async def _archive_file_components(
        self, event: AstrMessageEvent, scope_id: str, message_id: str,
    ) -> None:
        """从 AstrBot File 组件归档文件（用户发的 PDF/文档等）。"""
        comps = getattr(getattr(event, "message_obj", None), "message", None) or []
        for comp in comps[:6]:
            get_file = getattr(comp, "get_file", None)
            name = str(getattr(comp, "name", "") or "")
            if not callable(get_file) and not name:
                continue
            try:
                file_path = str(await get_file()) if callable(get_file) else ""
            except Exception as error:
                logger.info("长程记忆：File 组件取文件失败：%s", str(error)[:120])
                continue
            if not file_path:
                continue
            path = Path(file_path)
            if not path.is_file():
                continue
            data = await asyncio.to_thread(path.read_bytes)
            await self.media.save_bytes(
                data, scope_id=scope_id, kind="file",
                source_message_id=message_id,
                mime=path.suffix.lower().lstrip("."),
                note=name or path.name,
            )
            logger.info("长程记忆：文件已归档（%s，%d 字节）", path.name, len(data))

    def _ensure_gateway(self, event: AstrMessageEvent) -> None:
        if self.gateway is None:
            return
        bot = getattr(event, "bot", None)
        if bot is not None:
            self.gateway.bot = bot
        elif self.gateway.bot is None:
            self._bind_gateway_client()

    def _resolve_platform_client(self) -> Any | None:
        """The live OneBot/NapCat client, resolved from the loaded aiocqhttp
        adapter. Stable across the process, unlike per-event ``event.bot``."""
        platform = None
        getter = getattr(self.context, "get_platform", None)
        if callable(getter):
            try:
                platform = getter("aiocqhttp")
            except Exception:
                platform = None
        if platform is None:
            try:
                for inst in self.context.platform_manager.platform_insts:
                    if inst.meta().name == "aiocqhttp":
                        platform = inst
                        break
            except Exception:
                platform = None
        if platform is None:
            return None
        try:
            return platform.get_client()
        except Exception:
            return None

    def _bind_gateway_client(self) -> bool:
        """Point the gateway at the live client so timer-driven actions (发说说、
        主动发言、戳一戳) work on a cold start. The gateway is created with
        bot=None and used to be wired only from inbound events, so before the
        first message every autonomous send failed with 'no callable OneBot
        transport on NoneType'. Idempotent self-heal: safe to call each tick."""
        if self.gateway is None:
            return False
        if self.gateway.bot is not None:
            return True
        client = self._resolve_platform_client()
        if client is not None:
            self.gateway.bot = client
            logger.info("长程记忆：已绑定 OneBot 客户端，自主动作通道就绪")
            return True
        return False

    async def _hydrate_known_scopes(self) -> None:
        """Rebuild the conversation→scope map from disk at startup so the timer
        loops (压缩/反思/主动/规划) act on every known chat immediately, instead
        of staying idle until each chat sends its first message this session."""
        if not self.storage:
            return
        try:
            scopes = await self.storage.all_scopes("aiocqhttp")
        except Exception as error:
            logger.warning("长程记忆：加载历史会话映射失败：%s", str(error)[:160])
            return
        added = 0
        for row in scopes:
            conversation = row.get("conversation_id") or ""
            scope_id = row.get("scope_id") or ""
            if not conversation or not scope_id:
                continue
            if conversation not in self._known_scopes:
                self._known_scopes[conversation] = scope_id
                added += 1
        if added:
            logger.info("长程记忆：已从磁盘恢复%d个会话映射", added)

    async def _archive_media_then_note(
        self,
        event: AstrMessageEvent,
        raw: dict[str, Any],
        scope_id: str,
        message_id: str,
        text: str,
    ) -> None:
        """Store media, then turn every image into searchable memory text."""
        await self._archive_media(event, raw, scope_id, message_id, text)
        try:
            await self._ingest_image_notes(event, raw, scope_id, message_id)
        except Exception as error:
            logger.warning("长程记忆：图片转述入库失败：%s", str(error)[:200])
        try:
            await self._ingest_forward_notes(raw, scope_id)
        except Exception as error:
            logger.warning("长程记忆：合并转发入库失败：%s", str(error)[:200])

    @staticmethod
    def _forward_ids(raw: dict[str, Any]) -> list[str]:
        """Pull `forward` segment ids (合并转发的资源ID) out of a message."""
        message = raw.get("message")
        segments = message if isinstance(message, list) else []
        ids: list[str] = []
        for part in segments[:10]:
            if not isinstance(part, dict):
                continue
            # NapCat 段类型变体：forward（OneBot 标准）/forwardtransfer/flashtransfer
            # （QQ 新客户端）/node（转发节点嵌套）
            ptype = str(part.get("type", "")).lower()
            data = part.get("data") or {}
            fid = ""
            if "forward" in ptype or ptype == "node":
                fid = str(data.get("id") or data.get("res_id")
                          or data.get("fileSetId") or "").strip()
            elif ptype == "xml":
                # QQ 合并转发常以 XML 卡片下发，res_id 藏在 m_resid 属性里
                xml_text = str(data.get("data") or data.get("content") or "")
                match = re.search(r'm_resid="([^"]+)"', xml_text) or                     re.search(r"m_resid='([^']+)'", xml_text)
                if match:
                    fid = match.group(1).strip()
            if fid and fid not in ids:
                ids.append(fid)
                logger.info("长程记忆：发现合并转发段（%s），id=%s", ptype, fid[:16])
        return ids

    async def _fetch_forward_text(self, forward_id: str, *,
                                  depth: int = 0,
                                  visited: set[str] | None = None,
                                  extra_ids: Sequence[str] = (),
                                  scope_id: str | None = None) -> str:
        """拉取合并转发的节点内容并压成人读的文本。

        SnowLuma：get_forward_msg 接受 id 或 message_id（**string**，不能 int），
        返回 { messages: [...] }；NapCat：id+message_id 双参数。别名分开试：一个失败换下一个。

        实测要点（"合并聊天记录点不开/老说已过期"的修复）：QQ 只在服务端缓存
        转发资源一小段时间，隔一会儿再拉就真没了。所以：①**先查入库时抓下的
        展开结果**（消息到达时 `_ingest_forward_notes` 就存了一份，见
        `storage.find_forward_note`）；②库存没有再现场拉，并把 `extra_ids`
        （承载这条转发的消息 id 等）一起当候选。

        **参数形态是实测出来的（不是猜的）**：SnowLuma 的 get_forward_msg 只认
        `message_id`（传 `id` 会被服务端判 missing required params），而
        `message_id` 的**值**既可以是转发资源 id（res_id）也可以是承载消息的
        message_id——所以候选顺序必须 message_id 优先。返回值形状也实测过：
        传输层直接把 OneBot 信封的 `data` 交回来（`{"messages":[...]}`），
        用 `messages_of()` 统一解包（信封/解包两种形状都吃）。

        嵌套转发（转发里再套转发）递归展开：深度≤2，visited 防环，内层按缩进块
        接在所属节点下面（实录：套娃转发只显示 [forward] 占位等于没读到）。
        """
        self._bind_gateway_client()
        if self.gateway is None:
            return ""
        visited = visited if visited is not None else set()
        if forward_id in visited or depth > 2:
            return ""
        visited.add(forward_id)
        # ① 入库时抓下来的展开结果（QQ 侧资源过期后唯一可靠来源）
        if scope_id and self.storage is not None:
            try:
                cached = await self.storage.find_forward_note(scope_id, forward_id)
            except Exception:
                cached = ""
            if cached:
                logger.info("长程记忆：合并转发命中入库内容（%s）", forward_id[:12])
                return cached
        last_error = ""
        candidates: list[str] = [forward_id]
        for extra in extra_ids:
            text = str(extra or "").strip()
            if text and text not in candidates:
                candidates.append(text)
        nodes: list[dict[str, Any]] = []
        for candidate in candidates[:4]:
            for params in (
                # message_id 优先：SnowLuma 服务端硬性要求这个键名
                {"message_id": candidate},
                {"id": candidate, "message_id": candidate},
                {"id": candidate},
            ):
                try:
                    response = await self.gateway.execute("get_forward_msg", **params)
                    nodes = messages_of(response)
                    if nodes:
                        break
                except Exception as error:
                    last_error = str(error)[:160]
            if nodes:
                break
        if not nodes:
            logger.info("长程记忆：get_forward_msg 失败：%s", last_error)
            return ""
        prefix = "  " * depth
        lines: list[str] = []
        for node in nodes[:40]:
            if not isinstance(node, dict):
                continue
            name = _forward_node_name(node)
            # 内容有两种真实形态（都实测过）：
            #  ① 发送侧 node：{"data": {"nickname","content": [...]}}（我们构造出去的）
            #  ② 事件侧：OneBot 消息事件本身 —— 内容在 `message`、发送者在 `sender`
            # 旧代码只认 `content`，于是事件侧全被渲染成 "[空]"（实录读转发只出昵称）。
            nested: list[str] = []
            text = ""
            for key in ("content", "message"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    text = value
                    for match in re.findall(r'm_resid="([^"]+)"', value):
                        if match and match not in nested:
                            nested.append(match)
                    break
                if isinstance(value, list) and value:
                    serialized = safe_serialize(value)
                    text, _ = _text_and_reply(serialized, "")
                    if serialized:
                        nested = self._forward_ids({"message": serialized})
                    if not text:
                        kinds = sorted({str(p.get("type")) for p in value
                                        if isinstance(p, dict)})
                        text = f"[{'、'.join(kinds) or '空'}]"
                    break
            if not text:
                # 有的实现把段落放在 data.content 里
                data = node.get("data")
                inner = data.get("content") if isinstance(data, dict) else None
                if isinstance(inner, list) and inner:
                    text, _ = _text_and_reply(safe_serialize(inner), "")
                elif isinstance(inner, str):
                    text = inner
            text = " ".join(str(text or "").split())[:160]
            if text:
                lines.append(f"{prefix}{name}：{text}")
            for nid in nested[:1]:
                inner = await self._fetch_forward_text(
                    nid, depth=depth + 1, visited=visited, scope_id=scope_id)
                if inner:
                    # 嵌套层单独入库：这一层往往比外层先过期（实录：外层还能读、
                    # 内层 SnowLuma 已报 "download forward message payload is empty"），
                    # 抓到的这一刻就存下来，之后读内层 id 永远命中缓存。
                    if depth == 0:
                        await self._remember_forward_text(scope_id, nid, inner)
                    indented = "\n".join("  " + line for line in inner.splitlines())
                    lines.append(f"{prefix}[嵌套转发] ↓\n{indented}")
                else:
                    # 文案要"不可脑补"：实录模型看到简称会把这一层猜成
                    # "他也跟着接龙了一句"（其实这层是另一条合并转发卡）
                    lines.append(
                        f"{prefix}[嵌套转发未能展开：这层本身又是一条合并转发，"
                        "其内容在 QQ 服务器上已取不到——如实说明取不到，"
                        "绝不许猜它说了什么]")
        if not lines:
            return ""
        budget = 3600 if depth == 0 else 1600
        return "\n".join(lines)[:budget]

    @staticmethod
    def _reply_refs(raw: dict[str, Any]) -> list[str]:
        """引用消息（reply 段）指向的上游消息 id 列表。"""
        message = raw.get("message")
        segments = message if isinstance(message, list) else []
        refs: list[str] = []
        for part in segments[:10]:
            if not isinstance(part, dict):
                continue
            if str(part.get("type", "")).lower() != "reply":
                continue
            data = part.get("data") or {}
            ref = str(data.get("id") or "").strip()
            if ref and ref not in refs:
                refs.append(ref)
        return refs

    async def _file_media_id(self, scope_id: str, message_id: str) -> str:
        """同一消息先前归档过的文件记录 media_id（有就直接复用）。"""
        if self.media is None or not message_id:
            return ""
        try:
            records = await self.media.find_by_message(scope_id, message_id, kind="file")
        except Exception:
            return ""
        for record in records:
            if record.status == "saved" and record.item_id:
                return record.item_id
        return ""

    async def _archive_file_segment(
        self, scope_id: str, message_id: str, data: Mapping[str, Any],
    ) -> str:
        """按 file 段内容归档文件并返回 media_id（失败返回 ""）。"""
        if self.media is None or self.gateway is None:
            return ""
        ref = str(data.get("file_id") or data.get("file") or "").strip()
        if not ref:
            return ""
        try:
            response = await self.gateway.execute("get_file", file=ref, file_id=ref)
        except Exception as error:
            logger.info("长程记忆：引用文件 get_file 失败：%s", str(error)[:150])
            return ""
        inner = response.get("data", response) if isinstance(response, Mapping) else {}
        if not isinstance(inner, Mapping):
            inner = {}
        data_bytes: bytes | None = None
        local = str(inner.get("file") or inner.get("path") or "")
        encoded = str(inner.get("base64") or "")
        if local.lower().startswith(("http://", "https://")):
            try:
                data_bytes = await fetch_bounded(local, self.media.max_file_bytes)
            except Exception as error:
                logger.info("长程记忆：引用文件下载失败：%s", str(error)[:150])
        elif local:
            path = Path(local)
            if path.is_file():
                data_bytes = await asyncio.to_thread(path.read_bytes)
        elif encoded:
            import base64 as _b64

            try:
                data_bytes = _b64.b64decode(encoded.split(",", 1)[-1])
            except Exception:
                data_bytes = None
        if data_bytes is None and str(data.get("url") or "").lower().startswith(
                ("http://", "https://")):
            # 段里自带下载地址（SnowLuma 的 file 段可能直接给 URL）
            try:
                data_bytes = await fetch_bounded(
                    str(data.get("url")), self.media.max_file_bytes)
            except Exception as error:
                logger.info("长程记忆：引用文件段 URL 下载失败：%s", str(error)[:150])
        if data_bytes is None:
            logger.info("长程记忆：引用文件归档跳过（无本地/可下载内容）")
            return ""
        try:
            record = await self.media.save_bytes(
                data_bytes, scope_id=scope_id, kind="file",
                source_message_id=message_id,
                mime=str(inner.get("file_name", "")),
                note=str(data.get("name") or data.get("file_name") or ""),
            )
        except Exception as error:
            logger.warning("长程记忆：引用文件归档失败：%s", str(error)[:150])
            return ""
        logger.info(
            "长程记忆：引用文件已归档（%s，%d 字节）",
            str(data.get("name") or data.get("file_name") or local)[-40:], len(data_bytes),
        )
        return str(getattr(record, "item_id", "") or "")

    async def _document_excerpt(self, media_id: str, limit: int = 4000) -> str:
        """把归档文件按文档抽正文（PDF/文本等），抽不出返回 ""。"""
        if self.media is None or not media_id:
            return ""
        try:
            path = await self.media.get_path(media_id)
        except Exception:
            return ""
        try:
            from .src.pdf_reader import extract_document_text

            doc = await asyncio.to_thread(
                extract_document_text, path, max_chars=limit)
        except Exception as error:
            logger.info("长程记忆：文档正文抽取失败：%s", str(error)[:120])
            return ""
        text = str((doc or {}).get("text") or "").strip()
        return text[:limit]

    async def _quoted_digest(self, scope_id: str, ref_id: str) -> dict[str, Any] | None:
        """解析一条被引用消息，产出注入上下文的摘要（带缓存）。

        没有这一步，模型只看得到 "[引用消息]" 占位——实测它会对着一张被引用的
        PDF 试卷回"试卷呢，你又复读上了"。文本、文件（正文自动读出）、图片
        media_id 都在这里带给它。
        """
        key = f"{scope_id}:{ref_id}"
        if key in self._quoted_cache:
            return self._quoted_cache[key] or None
        digest: dict[str, Any] | None = None
        try:
            digest = await self._resolve_quoted(scope_id, ref_id)
        except Exception as error:
            logger.info("长程记忆：引用消息解析失败：%s", str(error)[:150])
        self._quoted_cache[key] = digest or {}
        while len(self._quoted_cache) > 64:
            self._quoted_cache.pop(next(iter(self._quoted_cache)))
        return digest

    async def _resolve_quoted(self, scope_id: str, ref_id: str) -> dict[str, Any] | None:
        self._bind_gateway_client()
        stored = None
        if self.storage is not None:
            try:
                stored = await self.storage.find_by_upstream(scope_id, ref_id)
            except Exception:
                stored = None
        sender = ""
        text = ""
        inner_id = ""
        segments: list[dict[str, Any]] = []
        if stored is not None:
            sender = str(stored.sender_name or stored.sender_id or "")
            text = str(stored.text or "")
            inner_id = stored.message_id
            if isinstance(stored.parts, list):
                segments = [p for p in safe_serialize(stored.parts)[:8] if isinstance(p, Mapping)]
        elif self.gateway is not None:
            # 库里没有（不在白名单历史/已被清理）：直接向网关要原消息
            response = await self.gateway.execute("get_msg", message_id=ref_id)
            payload = qq_payload(response)
            if not payload:
                return None
            value = payload.get("message")
            if isinstance(value, str):
                text = value
            elif isinstance(value, list):
                segments = [p for p in safe_serialize(value)[:8] if isinstance(p, Mapping)]
                text, _ = _text_and_reply(safe_serialize(value), "")
            else:
                text = str(value or "")
            sender_data = payload.get("sender") or {}
            if isinstance(sender_data, Mapping):
                sender = str(sender_data.get("card") or sender_data.get("nickname") or "")
            else:
                sender = str(getattr(sender_data, "card", "")
                             or getattr(sender_data, "nickname", "") or "")
            inner_id = str(payload.get("message_id") or "")
        files: list[dict[str, Any]] = []
        for part in segments:
            if str(part.get("type", "")).lower() != "file":
                continue
            data = part.get("data") or {}
            if not isinstance(data, Mapping):
                continue
            name = str(data.get("name") or data.get("file_name")
                       or data.get("file") or "文件")[:80]
            media_id = await self._file_media_id(scope_id, inner_id)
            if not media_id:
                media_id = await self._archive_file_segment(
                    scope_id, inner_id or f"quoted:{ref_id}", data)
            entry: dict[str, Any] = {"name": name}
            if media_id:
                entry["media_id"] = media_id
                excerpt = await self._document_excerpt(media_id)
                if excerpt:
                    entry["excerpt"] = excerpt
            files.append(entry)
        images: list[str] = []
        if self.media is not None and inner_id:
            try:
                records = await self.media.find_by_message(scope_id, inner_id, kind="image")
                images = [r.item_id for r in records[:4] if r.status == "saved"]
            except Exception:
                images = []
        # 被引用的消息本身可能是个合并转发（"引用转发+@bot 看下"是常见用法）
        forward_id = ""
        for part in segments:
            ptype = str(part.get("type", "")).lower()
            data = part.get("data") or {}
            if not isinstance(data, Mapping):
                continue
            if "forward" in ptype or ptype == "node":
                forward_id = str(data.get("id") or data.get("res_id")
                                 or data.get("fileSetId") or "").strip() or forward_id
            elif ptype == "xml":
                xml_text = str(data.get("data") or data.get("content") or "")
                match = re.search(r'm_resid="([^"]+)"', xml_text) or \
                    re.search(r"m_resid='([^']+)'", xml_text)
                if match:
                    forward_id = match.group(1).strip() or forward_id
        forward_text = ""
        if forward_id:
            try:
                forward_text = await self._fetch_forward_text(forward_id)
            except Exception:
                forward_text = ""
        if not text and not files and not images and not forward_text:
            return None
        digest: dict[str, Any] = {}
        if sender:
            digest["sender"] = sender
        if text:
            digest["text"] = text[:300]
        if files:
            digest["files"] = files
        if images:
            digest["image_media_ids"] = images
        if forward_text:
            digest["forward"] = forward_text[:2400]
        return digest

    async def _match_standing_intents(
        self, scope_id: str | None, texts: list[str],
    ) -> list[dict[str, Any]]:
        """Match current message texts against active standing intents."""
        if not self.storage:
            return []
        haystack = " ".join(str(t or "") for t in texts).casefold()
        if not haystack.strip():
            return []
        try:
            intents = await self.storage.list_standing_intents(
                self._shared_scope_ids(scope_id or ""))
        except Exception:
            return []
        now = time.time()
        hits: list[dict[str, Any]] = []
        for intent in intents:
            if any(kw and kw in haystack for kw in intent["keywords"]):
                last = intent.get("last_fired") or ""
                if last:
                    try:
                        elapsed = datetime.now(timezone.utc).timestamp() -                             datetime.fromisoformat(last).timestamp()
                        if elapsed < max(0, int(intent.get("cooldown_seconds", 0))):
                            continue
                    except (TypeError, ValueError):
                        pass
                hits.append({
                    "intent_id": intent["intent_id"],
                    "instruction": intent["instruction"],
                    "keywords": intent["keywords"],
                    "remaining": intent["remaining"],
                })
                try:
                    await self.storage.fire_standing_intent(intent["intent_id"])
                except Exception:
                    pass
        return hits[:3]

    async def _ingest_forward_notes(self, raw: dict[str, Any], scope_id: str) -> None:
        """把合并转发的内容存成可查询记忆（notice.forward，不占主线上下文）。"""
        # 必须用 raw 还原 account/conversation 才能落进同一个 scope
        # （拿 scope_id 当 conversation_id 会造出孤儿 scope——v0.16.1 的老坑）
        account = str(raw.get("self_id") or "")
        conversation = str(raw.get("group_id") or "") \
            or f"private:{raw.get('user_id')}"
        for fid in self._forward_ids(raw):
            flattened = await self._fetch_forward_text(fid, scope_id=scope_id)
            if not flattened or not self.ingest:
                continue
            from .src.models import NormalizedMessage

            stem = f"forward:{hash(flattened) & 0xFFFFFFFF:08x}"
            stored = await self.ingest.ingest(NormalizedMessage(
                platform="aiocqhttp", account_id=account,
                conversation_id=conversation, upstream_message_id=stem,
                sender_id="self", sender_name="self",
                text=f"[合并转发内容 {fid[:12]}]\n{flattened}",
                occurred_at=datetime.now(timezone.utc).isoformat(),
                raw_event={"message_id": stem, "derived": True, "kind": "forward"},
                parts=[], event_type="notice.forward",
            ), f"forward:{scope_id}:{stem}")
            logger.info("长程记忆：合并转发已展开入库（%d 字）", len(flattened))

    async def _ingest_image_notes(
        self, event: AstrMessageEvent, raw: dict[str, Any], scope_id: str, message_id: str
    ) -> None:
        """Describe each image of this message and write it back as memory.

        The description lands as a normal (searchable) memory message so the
        LLM can recall "what that picture was about" later. When the picture
        looks like a sticker it is additionally learned into the sticker pack,
        with the description stored as its semantic cue.
        """
        if not self.storage:
            return
        message = raw.get("message")
        segments = message if isinstance(message, list) else []
        for part in segments[:6]:
            if not isinstance(part, dict) or part.get("type") not in {"image", "mface"}:
                continue
            data = part.get("data") or {}
            summary = str(data.get("summary", ""))
            is_sticker = (
                "表情" in summary or "sticker" in str(data.get("sub_type", "")).lower()
                or data.get("sub_type") in {1, "1"}
            )
            description = ""
            payload = await self._fetch_image_payload(event, data, scope_id, message_id)
            if payload is not None:
                description = await self._describe_payload(payload, event=event)
            if not description and summary:
                description = summary
            if not description:
                continue
            if is_sticker and self.stickers and payload is not None:
                try:
                    record = await self.stickers.ingest(
                        payload, source_ref=f"{scope_id}:{message_id}",
                        trusted_get_image=True,
                        metadata={"summary": summary or description[:120]},
                    )
                    await self.stickers.note(record.sha256, description=description)
                    logger.info("长程记忆：表情包已学习并记录描述（%s）", description[:40])
                except Exception as error:
                    logger.warning("长程记忆：表情包学习失败：%s", str(error)[:150])
            try:
                await self._store_image_note(event, scope_id, message_id, description, is_sticker)
            except Exception as error:
                logger.warning("长程记忆：图片描述入记忆失败：%s", str(error)[:150])

    async def _describe_payload(
        self, payload: Any, event: AstrMessageEvent | None = None
    ) -> str:
        """Vision-describe a fetched image payload (base64 or local path)."""
        try:
            data = payload.get("data", payload) if isinstance(payload, Mapping) else {}
            encoded = str(data.get("base64") or "")
            local = str(data.get("file") or data.get("path") or "")
            pairs: list[tuple[str, str]] = []
            if encoded:
                pairs = [("image/png", encoded.split(",", 1)[-1])]
            elif local:
                import base64 as _b64

                path = Path(local).resolve()
                if path.is_file():
                    pairs = [("image/png", _b64.b64encode(path.read_bytes()).decode("ascii"))]
            if not pairs:
                return ""
            return await self._describe_images(pairs, event=event)
        except Exception as error:
            logger.info("长程记忆：图片转述跳过：%s", str(error)[:150])
            return ""

    async def _store_image_note(
        self, event: AstrMessageEvent, scope_id: str, message_id: str,
        description: str, is_sticker: bool,
    ) -> None:
        """Write an image description into the message store as retrievable text."""
        assert self.storage
        label = "[表情包]" if is_sticker else "[图片]"
        note = f"{label} {description[:400]}"
        # one derived message per source message keeps re-runs idempotent
        upstream = f"image-note:{message_id}"
        try:
            from .src.models import NormalizedMessage

            await self.ingest.ingest(NormalizedMessage(
                platform="aiocqhttp", account_id=str(event.get_self_id() or ""),
                conversation_id=self._conversation_key(event),
                upstream_message_id=upstream,
                sender_id=str(event.get_self_id() or "system"),
                sender_name=str(event.get_sender_name() or "system"),
                text=note, occurred_at=datetime.now(timezone.utc).isoformat(),
                raw_event={"message_id": upstream, "derived": True}, parts=[],
                event_type="message.created",
            ), f"derived:{scope_id}:{message_id}")
            logger.info("长程记忆：图片已转述入记忆：%s", note[:60])
        except Exception as error:
            logger.info("长程记忆：图片描述落库跳过：%s", str(error)[:120])

    async def _run_scheduled_task(self, task: ScheduledTask) -> dict[str, Any]:
        if not self.storage or not self.context_builder:
            return {"ok": False, "error": "not ready"}
        scope = await self.storage.resolve_scope(
            "aiocqhttp", task.scope_key or str(getattr(self.context, "self_id", "")), task.group_id
        )
        return await self._autonomous_action_loop(scope, task.name, task.prompt)

    async def _autonomous_action_loop(
        self, scope: str | None, task_name: str, instruction: str
    ) -> dict[str, Any]:
        self._bind_gateway_client()
        transcript: list[dict[str, Any]] = []
        results: list[Any] = []
        seen_calls: dict[str, int] = {}
        loop_warning = ""
        forced_done = False
        last_thought = ""
        watermark = 0
        if scope and self.storage is not None:
            try:
                tail = await self.storage.recent_messages(scope, 1)
                watermark = tail[-1].scope_seq if tail else 0
            except Exception:
                watermark = 0
        for round_index in range(3):
            mood = self.mood.get().as_prompt() if self.mood else ""
            memory = "{}"
            if scope and self.context_builder:
                memory = await self.context_builder.build(
                    scope, instruction, runtime_manifest={"mood": mood},
                    cross_scope_ids=self._shared_scope_ids(scope),
                    cross_labels={v: k for k, v in self._known_scopes.items()},
                )
            payload_data: dict[str, Any] = {
                "task": task_name, "instruction": instruction,
                "previous_results": results[-6:], "transcript": transcript[-4:],
            }
            # steering（openclaw 移植）：任务开始后群里的新消息可能含新指示，
            # 每轮带上让 agent 及时调整，而不是做完已经过时的步骤
            if scope and self.storage is not None and round_index > 0:
                try:
                    fresh = [
                        m for m in await self.storage.recent_messages(scope, 12)
                        if m.scope_seq > watermark
                    ]
                    if fresh:
                        payload_data["new_messages_since_start"] = [
                            {"sender": m.sender_name, "text": str(m.text or "")[:200]}
                            for m in fresh[-6:]
                        ]
                        payload_data["steering_note"] = (
                            "任务开始后又有新消息（可能含新指示）：据此调整行动，"
                            "别再做已经过时的步骤。"
                        )
                        watermark = max(m.scope_seq for m in fresh)
                except Exception:
                    pass
            if loop_warning:
                payload_data["loop_warning"] = loop_warning
                loop_warning = ""
            payload = compact_json(payload_data, self.settings.context_char_budget)
            provider = self.settings.reply_provider_id or ""
            persona = await self._persona_prompt()
            script_block = self._script_prompts_block()
            task_prompt = (
                "\n\n".join(
                    part for part in
                    (persona, SCHEDULED_TASK_PROMPT.strip(), script_block,
                     self._standing_orders_block(),
                     await self._injections_block()) if part
                )
            )
            raw = await self._llm_text(provider, prompt=payload, system_prompt=task_prompt)
            plan = parse_json_object(raw) or {}
            actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
            for item in actions[:5]:
                if not isinstance(item, dict):
                    continue
                tool_name = str(item.get("tool", ""))
                call_args = item.get("args") or {}
                # 循环检测（openclaw loop-detection 移植）：同一动作重复且无进展
                call_key = f"{tool_name}|{compact_json(call_args, 500)}"
                seen_calls[call_key] = seen_calls.get(call_key, 0) + 1
                repeats = seen_calls[call_key]
                if repeats >= 5:
                    forced_done = True
                if repeats >= 3:
                    outcome = (
                        f"已阻止：同一个动作（{tool_name}）连续第{repeats}次执行、没有新进展。"
                        "换个方法或直接收尾。")
                    results.append({"tool": tool_name, "result": outcome})
                    transcript.append({"tool": tool_name})
                    loop_warning = outcome
                    continue
                outcome = await self._dispatch_task_action(tool_name, call_args, scope)
                results.append({"tool": tool_name, "result": outcome})
                transcript.append({"tool": tool_name})
            if isinstance(plan.get("thought"), str) and plan["thought"].strip():
                last_thought = plan["thought"].strip()
            if forced_done or plan.get("done") or not actions:
                break
        return {"ok": True, "actions": len(results), "results": results,
                "last_thought": last_thought}

    async def _heartbeat_loop(self) -> None:
        """Human-like tick: scheduled plans first, idle pastimes, silent lurking."""
        await asyncio.sleep(90)
        while not self._stopping:
            interval = max(30.0, self.settings.heartbeat_interval_seconds)
            interval *= random.uniform(0.75, 1.35)
            try:
                await self._heartbeat_tick()
            except Exception as error:
                logger.warning("长程记忆：心跳异常：%s\n%s", str(error)[:200], traceback.format_exc())
            await asyncio.sleep(interval)

    async def _heartbeat_tick(self) -> None:
        if not self._still_current_instance():
            return
        if not self._ready() or self.interactions.is_quiet():
            return
        self._bind_gateway_client()
        # 1) 自我规划到期：把未来几小时排成日程
        if time.monotonic() >= self._next_plan_at:
            self._next_plan_at = time.monotonic() + max(1, self.settings.plan_interval_hours) * 3600
            await self._planning_round()
            return
        # 2) 到点的计划事项
        if self.storage:
            due = await self.storage.due_plans(limit=1)
            if due:
                await self._run_plan_item(due[0])
                return
        # 3) 闲时消遣（每小时限额，概率触发，别让人看起来像挂机脚本）
        now = timeutil.now()
        hour_key, used = self._idle_budget
        if hour_key != now.hour:
            self._idle_budget = (now.hour, 0)
            used = 0
        if used >= self.settings.idle_actions_per_hour:
            await self._lurk_round()
            return
        if random.random() < 0.35:
            kind = idle_choice(now.hour)
            if kind == "none":
                await self._lurk_round()
                return
            self._idle_budget = (now.hour, used + 1)
            await self._run_idle_action(kind)
            return
        # 4) 记忆维护：把新消息压成 L1/L2/L3（不依赖攒满一整批）
        await self._compress_all_scopes()
        # 5) 默认安静潜水：看一眼群里在聊什么，不说话
        await self._lurk_round()

    async def _planning_round(self) -> None:
        """Ask the LLM to lay out the next few hours as a concrete schedule."""
        if not self.storage or not self.context_builder:
            return
        groups: list[dict[str, Any]] = []
        for key, scope_id in list(self._known_scopes.items()):
            if key.startswith("private:") or not self.settings.allows_group(key):
                continue
            topics = await self.storage.recent_topics(scope_id, 4)
            groups.append({
                "group_id": key,
                "recent_topics": [str(t.get("topic", "")) for t in topics],
            })
        acquaintances = await self.storage.list_impressions(
            self._shared_scope_ids(next(iter(self._known_scopes.values()), "")), limit=12
        )
        pending = await self.storage.pending_plans(limit=15)
        payload = build_plan_prompt(
            now=timeutil.now(),
            horizon_hours=max(1, self.settings.plan_interval_hours),
            groups=groups,
            acquaintances=[
                {"user_id": a["user_id"], "name": a["display_name"],
                 "impression": a["impression"][:80], "tags": a["tags"]}
                for a in acquaintances
            ],
            mood=self.mood.get().as_prompt() if self.mood else "",
            pending_plans=[
                {"due_at": p["due_at"], "kind": p["kind"], "detail": p["detail"][:60]}
                for p in pending
            ],
        )
        provider = self.settings.reply_provider_id or ""
        persona = await self._persona_prompt()
        system = "\n\n".join(
            part for part in (
                persona,
                "你是自己的日程规划器。只输出严格 JSON："
                '{"plans": [{"time": "HH:MM", "kind": "...", "detail": "..."}]}。'
                "time 是 24 小时制。像真人一样排：不要每件事都重要，可以只排 2-4 件事，"
                "留大把空白什么都不干；深夜不安排社交；同一群别排太密。",
            ) if part
        )
        raw = await self._llm_text(provider, prompt=payload, system_prompt=system)
        items = parse_plan_response(raw)
        if not items:
            logger.info("长程记忆：本轮规划没有产出日程")
            return
        stored = await self.storage.add_plans([
            {"due_at": item.due_at.isoformat(timespec="minutes"),
             "kind": item.kind, "detail": item.detail}
            for item in items
        ])
        logger.info("长程记忆：规划出%d项日程（入库%d）", len(items), stored)

    async def _run_plan_item(self, plan: dict[str, Any]) -> None:
        if not self._still_current_instance():
            return
        kind = str(plan.get("kind", "custom"))
        detail = str(plan.get("detail", ""))
        # Claim the plan first so a crash mid-run never re-fires the same item.
        if self.storage and not await self.storage.claim_plan(str(plan.get("plan_id", ""))):
            return
        scope, group_key = self._plan_target_group(detail)
        done = "done"
        try:
            if self.interactions.is_quiet():
                done = "skipped"
                return
            instruction = (
                f"现在是{timeutil.text('%H:%M')}，"
                f"这是你日程里的一项[{kind}]：{detail}\n"
                "像真人一样完成它：能做就做，觉得此刻不合适也可以放弃（输出 done 并"
                "在 thought 里说明）；所有 QQ/Qzone 工具随意用，发言遵守语言铁律。"
            )
            if scope and group_key:
                instruction += f"（涉及群发言时 group_id 只能是 {group_key}）"
            result = await self._autonomous_action_loop(
                scope or None, f"日程:{kind}", instruction
            )
            done = "done" if result.get("actions") else "skipped"
        except Exception as error:
            logger.warning("长程记忆：日程[%s]执行失败：%s", kind, str(error)[:200])
            # 瞬时故障（上游 503/模型通道抖动/超时）不该让日程直接判死：
            # 顺延重试，超过上限才标记 failed（实录：opus 通道瞬时 503 烧穿所有日程）
            text = str(error)
            transient = (
                self._looks_like_provider_missing(error)
                or "503" in text or "502" in text or "429" in text
                or "超时" in text or "timeout" in text.lower()
                or "temporarily" in text.lower()
            )
            if (transient and self.storage and await self.storage.postpone_plan(
                    str(plan.get("plan_id", "")))):
                done = ""  # 已改回 pending 并顺延，别再 complete
                logger.info("长程记忆：日程[%s]判定为瞬时故障，已顺延重试", kind)
            else:
                done = "failed"
        finally:
            if self.storage and done:
                await self.storage.complete_plan(str(plan.get("plan_id", "")), done)

    async def _scope_labels(self) -> dict[str, str]:
        """scope_id → "群名（群号）"：跨会话记忆与各会话近况都靠它标注出处。"""
        labels: dict[str, str] = {}
        for row in await self._scope_directory():
            scope_id = str(row.get("scope_id") or "")
            key = str(row.get("key") or "")
            if not scope_id or not key:
                continue
            name = str(row.get("name") or "").strip()
            labels[scope_id] = f"{name}（{key}）" if name else key
        for key, scope_id in (self._known_scopes or {}).items():
            labels.setdefault(str(scope_id), str(key))
        return labels

    @staticmethod
    def _affinity_nudge(*, text: str) -> tuple[float, str]:
        """按这条消息的语气决定好感度涨跌（确定性规则，不指望模型自觉）。

        实录：旧代码每回复一次就 `adjust_affinity(+1.0)`，**好感只涨不跌**——
        用户做了次冒犯性测试（"你当我爸爸要不要"），好感度反而升了。
        现在负面明显重于正面：好感涨得慢、掉得快，才像真人。
        """
        from .src.emotions import classify_interaction

        delta = classify_interaction(text)
        if delta is None:
            return 0.0, ""
        if delta.valence > 0:
            return 0.5, ""
        if delta.valence < 0:
            return -1.5, "这条话说得不太客气"
        return 0.0, ""

    def _plan_target_group(self, detail: str) -> tuple[str, str]:
        """Find a whitelist group mentioned in a plan detail (id or known key)."""
        for key in self._known_scopes:
            if key and key in detail:
                return self._known_scopes[key], key
        for key in self.settings.group_whitelist:
            if key and key in detail:
                return self._known_scopes.get(key, ""), key
        return "", ""

    async def _scope_directory(self) -> list[dict[str, str]]:
        """全部已知会话：群号/QQ号、scope_id、名字（含白名单里还没说过话的群）。"""
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        if self.storage is not None:
            try:
                for item in await self.storage.all_scopes("aiocqhttp"):
                    key = str(item.get("conversation_id") or "")
                    scope_id = str(item.get("scope_id") or "")
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    rows.append({"key": key, "scope_id": scope_id,
                                 "name": str(item.get("display_name") or "")})
            except Exception as error:
                logger.info("长程记忆：列举会话失败：%s", str(error)[:120])
        for key, scope_id in (self._known_scopes or {}).items():
            if key in seen:
                continue
            seen.add(key)
            rows.append({"key": str(key), "scope_id": str(scope_id), "name": ""})
        for key in self.settings.group_whitelist or []:
            if str(key) in seen:
                continue
            seen.add(str(key))
            rows.append({"key": str(key), "scope_id": str(self._known_scopes.get(str(key), "")),
                         "name": ""})
        return rows

    async def _resolve_scope_ref(self, ref: str) -> tuple[str, str, str]:
        """把"群号 / QQ号 / 群名（或名字片段）"解析成 (scope_id, 会话号, 提示)。

        实录：用户要"数学指令讨论群"的记录，模型手里只有名字，而检索工具只认当前
        会话 → 它只好拿私聊的消息凑数。这里让带名字的目标都能解析出来；解析不了
        就返回候选清单（宁可让它再问一句，也不要它默默换群）。
        """
        text = str(ref or "").strip()
        if not text:
            return "", "", ""
        directory = await self._scope_directory()
        lowered = text.lower()
        exact = [row for row in directory if row["key"] == text]
        if not exact:
            exact = [row for row in directory
                     if row["name"] and row["name"].lower() == lowered]
        if not exact:
            exact = [row for row in directory
                     if row["name"] and lowered in row["name"].lower()]
        if not exact:
            exact = [row for row in directory if row["key"] == text.replace("群", "")]
        if len(exact) == 1:
            row = exact[0]
            scope_id = row["scope_id"]
            if not scope_id and self.storage is not None:
                scope_id = str(self._known_scopes.get(row["key"], ""))
            label = f"{row['name']}（{row['key']}）" if row["name"] else row["key"]
            return scope_id, row["key"], f"已定位到 {label}"
        names = [f"{row['name']}（{row['key']}）" if row["name"] else row["key"]
                 for row in directory][:12]
        if len(exact) > 1:
            matched = [f"{row['name']}（{row['key']}）" if row["name"] else row["key"]
                       for row in exact][:8]
            return "", "", f"「{text}」匹配到多个会话：{'、'.join(matched)}——请指明群号"
        return "", "", (f"没找到「{text}」这个会话；已知会话：{'、'.join(names) or '（暂无）'}")

    async def _run_idle_action(self, kind: str) -> None:
        scope, group_key = "", ""
        active = self._most_active_group()
        if active:
            scope, group_key = active
        if kind == "chat" and not group_key:
            kind = "memory"  # 没有活跃群，改为整理记忆
        friend = await self._friend_to_talk()
        friend_line = (
            f"可考虑的对象：{friend['display_name'] or friend['user_id']}"
            f"（{friend['user_id']}，印象：{friend['impression'][:60]}）"
            if friend else "暂无熟人记录"
        )
        menus = {
            "qzone": "去 Qzone 逛逛：qzone_list 看看朋友动态，给一两条点赞或评论；"
                     "有感触也可以 qzone_publish 发条说说。不想发也没关系。",
            "news": (
                "关心一下外面的事：先用 web_search 搜今天的热点新闻，"
                "挑一条你好奇的用 browse 打开读正文（可以 browser_click 继续翻），"
                "读完后用 remember 写一条你自己的看法当谈资；"
                "relate 到群里的话题就用 add_plan 安排稍后去聊。"
            ),
            "learn": (
                "自学点东西：web_search 搜一个你感兴趣的主题（或者上次没弄明白的问题），"
                "用 browse 打开几页认真读，browser_find 找细节；"
                "读明白了就用 remember 记下关键结论。学到新东西本身就是收获。"
            ),
            "poke": f"用 napcat_call(friend_poke) 戳一位朋友打招呼。{friend_line}",
            "private_chat": f"主动私聊一位朋友聊两句：先 get_user_style 看看TA的说话风格，"
                             f"模仿着TA习惯的语气聊（qq_send_private，1-3条短气泡就收）。{friend_line}",
            "chat": f"看看群{group_key or '（无活跃群）'}里在聊什么，值得接就 qq_send_group "
                    f"插1-2句（group_id 只能是 {group_key}），不值得就 done。",
            "memory": "翻翻最近的聊天记忆，用 remember 沉淀一条总结，"
                      "给聊得多的人 update_impression 更新印象。",
            "profile": "打理一下账号：换个性签名/在线状态（qq_set_profile/qq_set_status），"
                       "或者去常待的群打个卡（napcat_call send_group_sign）。",
            "sticker": (
                "看心情发一张表情包活跃气氛：先 list_stickers 看看库存，"
                f"再挑一张贴合当前心情的用 send_sticker(sticker_id) 发到群{group_key or '（无活跃群）'}"
                "（没有活跃群或库存为空就 done）。只发一张，别连发。"
            ),
        }
        instruction = menus.get(kind, str(kind))
        instruction += "\n（闲时消遣：一件小事就收手，绝不刷屏。）"
        if kind in {"news", "learn"}:
            instruction += (
                "\n读完记得用 remember 记下结论；插件还会把你读到的内容"
                "自动存进可查询记忆。"
            )
        try:
            result = await self._autonomous_action_loop(
                scope or None, f"闲时:{kind}", instruction
            )
            if result.get("actions"):
                logger.info("长程记忆：闲时动作[%s]完成", kind)
            if kind in {"news", "learn"}:
                # 把这次浏览留下的材料概括进长程记忆
                material = self._recent_web_material()
                if material:
                    self._spawn(self._digest_web_learning(scope or None, material))
        except Exception as error:
            logger.warning("长程记忆：闲时动作[%s]失败：%s", kind, str(error)[:150])

    async def _digest_web_learning(self, scope: str | None, note: str) -> str:
        """Condense freshly learned web material into long-term memory.

        The raw pages already sit in the queryable store (see `_browser_note`);
        this writes the *high-level* takeaway as an L3 summary so it shows up in
        normal memory recall, plus one ledger fact for the catalog.
        """
        if not self.storage or not note.strip():
            return ""
        target = scope or next(iter(dict.fromkeys(self._known_scopes.values())), None)
        if not target:
            return ""
        provider = self.settings.summary_provider_id or self.settings.reply_provider_id
        digest = ""
        if provider:
            try:
                raw = await self._llm_text(
                    provider,
                    prompt=compact_json({
                        "material": note[:6000],
                        "ask": "请把上面的资料高度概括成三到五句可直接记住的结论，"
                               "保留关键事实与数字，不要评价不要客套。",
                    }, 8000),
                    system_prompt=(
                        "你是自己的学习整理器。只输出概括本身，不要前言后语，"
                        "不要 Markdown，不超过 300 字。"
                    ),
                )
                digest = str(raw or "").strip()[:600]
            except Exception as error:
                logger.info("长程记忆：学习概括失败，改用原文摘要：%s", str(error)[:120])
        if not digest:
            digest = note[:400]
        try:
            tail = await self.storage.recent_messages(target, 1, include_events=True)
            end_seq = tail[-1].scope_seq if tail else 0
            stamp = timeutil.text("%m-%d %H:%M")
            record = await self.storage.store_summary(
                target, 3, end_seq, end_seq,
                f"网络学习 {stamp}", digest, ["网络学习"], [],
            )
            if self.ledger:
                try:
                    await self.ledger.apply_proposal(
                        target,
                        {"kind": "fact", "subject": f"网络学习 {stamp}",
                         "value": digest[:400], "confidence": 0.8,
                         "evidence_ids": [record.summary_id]},
                        f"weblearn:{record.summary_id}",
                    )
                except Exception:
                    pass
            logger.info("长程记忆：网络学习已概括进长程记忆（%s）", digest[:60])
            return digest
        except Exception as error:
            logger.warning("长程记忆：学习概括入库失败：%s", str(error)[:150])
            return ""

    def _recent_web_material(self, limit: int = 6) -> str:
        """Collect what the browser just read (for the learning digest)."""
        if self._browser is None:
            return ""
        snapshot = self._browser.snapshot(text_limit=40000)
        text = str(snapshot.get("text", "")).strip()
        title = str(snapshot.get("title", "")).strip()
        url = str(snapshot.get("url", "")).strip()
        if not text:
            return ""
        head = f"{title}（{url}）" if title else url
        return f"{head}\n{text[:limit * 1000]}"

    async def _browser_setup_task(self) -> None:
        """Install-time auto-detection: pip mirror + npmmirror kernel download."""
        log_file = None
        if self.storage is not None:
            log_file = self.storage.path.parent / "browser_setup.log"
        self._browser_env = await ensure_browser_environment(
            # 浏览器环境安装也归总开关管：关掉 auto_install_deps 就一律不自动装
            auto_install=bool(self.settings.browser_auto_install
                              and self.settings.auto_install_deps),
            logger=lambda msg: logger.info("长程记忆：%s", str(msg)[:200]),
            log_file=log_file,
        )
        self._browser_env["checked"] = True
        if self._browser_env.get("error"):
            logger.warning("长程记忆：浏览器环境：%s", self._browser_env["error"])

    def _pw_driver(self) -> PlaywrightDriver:
        if self._pw is None:
            profile = None
            if getattr(self.settings, "browser_persist", True) and self.storage is not None:
                profile = self.storage.path.parent / "browser_profile"
            self._pw = PlaywrightDriver(
                user_data_dir=profile,
                executable_path=self.settings.browser_executable_path or None)
        return self._pw

    # ------------------------------------------------------------------ studio（designer / programmer）
    def _studio_host(self) -> ProgramHost:
        """Loopback studio host: designer windows + programmer programs."""
        if self._studio is None:
            root = (
                self.storage.path.parent if self.storage is not None
                else Path(get_astrbot_data_path()) / "plugin_data"
                / "astrbot_plugin_long_memory_agent"
            ) / "studio"
            self._studio = ProgramHost(
                root,
                port=self.settings.program_port,
                program_ttl_seconds=self.settings.program_ttl_minutes * 60,
                logger=lambda message: logger.info("长程记忆：%s", str(message)[:200]),
            )
            port = self._studio.start()
            allow_local_port(port)
        return self._studio

    # ------------------------------------------------------------------ scripts 拓展
    def _script_manager(self) -> ScriptExtensionManager:
        """scripts/ 万能拓展：加载、工具分发、提示词注入（长期兼容接口）。"""
        if self._scripts is None:
            root = Path(__file__).resolve().parent / "scripts"
            base = (
                self.storage.path.parent if self.storage is not None
                else Path(get_astrbot_data_path()) / "plugin_data"
                / "astrbot_plugin_long_memory_agent"
            )
            data_root = base / "script_data"
            self._scripts = ScriptExtensionManager(
                root, data_root, learned_root=base / "learned_skills",
                llm=self._script_llm,
                memory_note=self._script_memory_note,
                qq_send_group=self._script_send_group,
                qq_send_private=self._script_send_private,
                run_program=self._script_run_program,
                save_image=self._script_save_image,
                ssh_exec=self._script_ssh_exec,
                media_from_remote=self._script_media_from_remote,
                logger=lambda message: logger.info("长程记忆：%s", str(message)[:250]),
            )
        return self._scripts

    async def _script_llm(self, prompt: str, system_prompt: str, provider_id: str) -> str:
        provider = provider_id or self.settings.reply_provider_id \
            or self.settings.summary_provider_id
        return await self._llm_text(
            provider, prompt=prompt, system_prompt=system_prompt or "你是助手。")

    async def _script_memory_note(self, text: str) -> None:
        await self._studio_note(text, "script")

    async def _script_send_group(self, group_id: str, text: str) -> None:
        if not self.settings.allows_group(group_id):
            raise ScriptExtensionError(f"群 {group_id} 不在白名单，拒绝发送")
        if self.gateway is None:
            raise ScriptExtensionError("QQ 通道未就绪")
        self._bind_gateway_client()
        await self.gateway.execute(
            "send_group_msg", group_id=int(group_id),
            message=[{"type": "text", "data": {"text": _bubble_text(text)}}])

    async def _script_run_program(self, program_id: str, code: str,
                                  title: str, description: str) -> dict[str, Any]:
        """拓展启动自己的 Flask 小程序：进 studio 宿主 + 注册表（与 programmer 同一套）。"""
        host = self._studio_host()
        host.upsert_program(program_id, code, title, description)
        result = await asyncio.to_thread(host.run_program, program_id, code, title)
        if result.get("ok"):
            self._spawn(self._studio_note(
                f"[程序] {title or program_id}：{description}（访问 {result['url']}）",
                "program"))
        return result

    async def _script_save_image(self, png: bytes, note: str) -> str:
        if self.media is None:
            raise ScriptExtensionError("媒体归档未启用（enable_media_archive）")
        record = await self.media.save_bytes(
            png, scope_id="studio", kind="image", mime="image/png", note=note)
        return str(record.item_id)

    async def _script_ssh_exec(self, command: str, timeout: int = 60) -> dict[str, Any]:
        """拓展的 SSH 能力：与插件共用同一份 SSH 配置与连接。"""
        if self._ssh is None or not self._ssh_configured():
            raise ScriptExtensionError(
                "SSH 未配置：请在插件配置填 ssh_host / ssh_user / ssh_password")
        seconds = min(max(int(timeout or 60), 5), 600)
        return await self._ssh.exec(str(command), timeout=float(seconds))

    async def _script_media_from_remote(self, remote_path: str,
                                        note: str = "") -> str:
        """拓展的远程文件回传：拉进媒体归档并返回 media_id。"""
        result = await self._pull_remote_file(str(remote_path), str(note or ""))
        if not result.get("ok") or not result.get("media_id"):
            raise ScriptExtensionError("远程文件归档失败（类型不受支持或超出大小限制）")
        return str(result["media_id"])

    async def _script_send_private(self, user_id: str, text: str) -> None:
        if self.gateway is None:
            raise ScriptExtensionError("QQ 通道未就绪")
        self._bind_gateway_client()
        await self.gateway.execute(
            "send_private_msg", user_id=int(user_id),
            message=[{"type": "text", "data": {"text": _bubble_text(text)}}])

    def _standing_orders_block(self) -> str:
        """常备指令（OpenClaw standing orders）：每轮注入的持久行动授权。"""
        text = str(getattr(self.settings, "standing_orders", "") or "").strip()
        if not text:
            return ""
        return "【常备指令（主人授予你的长期行动授权，每次对话都生效）】" + text[:2000]

    def _script_prompts_block(self) -> str:
        """拓展提示词段落：注入回复/自主系统提示词。"""
        return "\n\n".join(
            text.strip() for text in self._script_manager().prompts() if text.strip()
        )

    async def load_script_extensions(self) -> None:
        problems = await self._script_manager().load_all()
        catalog = self._script_manager().tool_catalog()
        if problems:
            for item in problems:
                logger.warning("长程记忆：scripts 拓展加载问题：%s", item)
        logger.info(
            "长程记忆：scripts 拓展已加载 %d 个（工具 %d 个）",
            len(self._script_manager().extensions), len(catalog),
        )

    async def _studio_note(self, summary: str, kind: str) -> None:
        """Archive trace (design/program) into the queryable event memory."""
        if not self.storage or not self.ingest or not summary.strip():
            return
        try:
            from .src.models import NormalizedMessage

            stem = f"studio:{kind}:{hash(summary) & 0xFFFFFFFF:08x}"
            stored = await self.ingest.ingest(NormalizedMessage(
                platform="aiocqhttp", account_id="",
                conversation_id="studio", upstream_message_id=stem,
                sender_id="self", sender_name="self",
                text=summary[:800], occurred_at=datetime.now(timezone.utc).isoformat(),
                raw_event={"message_id": stem, "derived": True, "kind": kind},
                parts=[], event_type=f"notice.{kind}",
            ), f"studio:{stem}")
            # 让 studio 留痕立刻进入共享记忆集合（否则要等重启才被 hydrate）
            self._known_scopes.setdefault("studio", stored.scope_id)
        except Exception as error:
            logger.info("长程记忆：%s留痕跳过：%s", kind, str(error)[:120])

    def _wrap_svg(self, svg: str) -> str:
        return (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<style>html,body{margin:0;padding:0;background:#fff;}'
            'svg{max-width:100%;height:auto;display:block;margin:0 auto;}</style>'
            "</head><body>" + svg + "</body></html>"
        )

    # ------------------------------------------------------------------ SSH
    def _ssh_config(self) -> SSHConfig:
        return SSHConfig(
            host=self.settings.ssh_host,
            port=self.settings.ssh_port,
            user=self.settings.ssh_user,
            password=self.settings.ssh_password,
            notes=self.settings.ssh_notes,
        )

    def _ssh_configured(self) -> bool:
        # 面板配置可能刚存过：先热刷新再判定，工具门控不依赖重启
        self._refresh_settings_if_changed()
        return self._ssh_config().configured

    def _ssh_provider(self) -> str:
        return (self.settings.subagent_provider_id.strip()
                or self.settings.reply_provider_id.strip()
                or self.settings.judge_provider_id.strip())

    async def _ssh_agent_llm(self, provider: str, prompt: str, system_prompt: str) -> str:
        return await self._llm_text(provider, prompt=prompt, system_prompt=system_prompt)

    async def _ssh_setup_task(self) -> None:
        """Install-time paramiko provisioning (only when SSH is configured)."""
        self._ssh_env = await ensure_paramiko(
            auto_install=bool(self.settings.auto_install_deps),
            logger=lambda msg: logger.info("长程记忆：%s", str(msg)[:200]),
        )
        self._ssh_env["checked"] = True
        if self._ssh_env.get("error"):
            logger.warning("长程记忆：SSH 环境：%s", self._ssh_env["error"])

    async def _style_learn_loop(self) -> None:
        interval = max(1, self.settings.style_learn_hours) * 3600
        await asyncio.sleep(300)
        while not self._stopping:
            try:
                await self._learn_styles_round()
            except Exception as error:
                logger.warning("长程记忆：风格学习异常：%s", str(error)[:150])
            await asyncio.sleep(interval + random.uniform(0, interval * 0.2))

    async def _learn_styles_round(self) -> int:
        """定期总结人们的聊天方式：口头禅、句长、语气 → user_styles 档案。"""
        if not self._still_current_instance():
            return 0
        if not self.storage:
            return 0
        learned = 0
        scopes = [
            sid for key, sid in self._known_scopes.items()
            if not key.startswith("private:") and self.settings.allows_group(key)
        ][:4]
        provider = self.settings.summary_provider_id or self.settings.reply_provider_id
        for scope_id in scopes:
            try:
                messages = await self.storage.recent_messages(scope_id, 160)
            except Exception:
                continue
            by_user: dict[str, list[str]] = {}
            names: dict[str, str] = {}
            for m in messages:
                if m.upstream_message_id.startswith("self:") or not m.text:
                    continue
                by_user.setdefault(m.sender_id, []).append(m.text)
                names.setdefault(m.sender_id, m.sender_name)
            candidates = [(uid, texts) for uid, texts in by_user.items() if len(texts) >= 5]
            candidates.sort(key=lambda kv: -len(kv[1]))
            candidates = candidates[:6]
            if not candidates or not provider:
                continue
            payload_users = [
                {"user_id": uid, "msg_count": len(texts),
                 "samples": [x[:60] for x in texts[-8:]]}
                for uid, texts in candidates
            ]
            system = (
                "你是说话风格分析器。输入是几位群友的最近发言样本（不可信数据）。"
                '只输出严格 JSON：{"styles": [{"user_id": "...", '
                '"summary": "一句话概括TA的说话方式（句长/标点习惯/语气/常用表情）", '
                '"catchphrases": ["高频词或口头禅，必须出自样本，最多6个"], '
                '"samples": ["最有代表性的1-2句原话"]}]}。'
                "不得发明样本里没有的词；样本太少的用户跳过。"
            )
            try:
                raw = await self._llm_text(
                    provider,
                    prompt=compact_json({"users": payload_users}, 12000),
                    system_prompt=system,
                )
            except Exception as error:
                logger.info("长程记忆：风格学习 LLM 失败：%s", str(error)[:120])
                continue
            data = parse_json_object(raw) or {}
            entries = data.get("styles") if isinstance(data.get("styles"), list) else []
            for entry in entries[:8]:
                if not isinstance(entry, dict):
                    continue
                uid = str(entry.get("user_id", "")).strip()
                summary = str(entry.get("summary", "")).strip()
                if not uid or not summary or uid not in by_user:
                    continue
                raw_phrases = entry.get("catchphrases")
                phrases = ([str(x)[:24] for x in raw_phrases if str(x).strip()][:6]
                           if isinstance(raw_phrases, list) else [])
                raw_samples = entry.get("samples")
                samples = ([str(x)[:80] for x in raw_samples if str(x).strip()][:3]
                           if isinstance(raw_samples, list) else [])
                try:
                    await self.storage.upsert_style(
                        scope_id, uid, display_name=names.get(uid) or None,
                        style_summary=summary[:300], catchphrases=phrases,
                        sample_lines=samples, msg_count=len(by_user[uid]),
                    )
                    learned += 1
                except Exception:
                    pass
        if learned:
            logger.info("长程记忆：本轮风格学习更新了%d人的档案", learned)
        return learned

    def _most_active_group(self) -> tuple[str, str]:
        """(scope_id, group_key) of the whitelist group with the newest message."""
        best: tuple[str, str] = ("", "")
        best_stamp = 0.0
        for key, scope_id in self._known_scopes.items():
            if key.startswith("private:") or not self.settings.allows_group(key):
                continue
            if time.monotonic() < self._send_blockade.get(scope_id, 0.0):
                continue
            best = best if best[0] else (scope_id, key)
        return best

    async def _friend_to_talk(self) -> dict[str, Any] | None:
        if not self.storage:
            return None
        scopes = self._shared_scope_ids(next(iter(self._known_scopes.values()), ""))
        people = await self.storage.list_impressions(scopes, limit=15)
        now = datetime.now(timezone.utc)
        for person in people:
            try:
                last = datetime.fromisoformat(person["last_seen"])
            except (TypeError, ValueError):
                continue
            gap = (now - last).total_seconds()
            if gap > 2 * 3600:
                return person  # someone you have not touched for a while
        return people[0] if people else None

    async def _lurk_round(self) -> None:
        """Silent lurking: read recent chatter, refresh who was seen, say nothing."""
        if not self.storage:
            return
        candidates = [
            (key, scope_id)
            for key, scope_id in self._known_scopes.items()
            if not key.startswith("private:") and self.settings.allows_group(key)
        ]
        if not candidates:
            return
        key, scope_id = candidates[self._lurk_cursor % len(candidates)]
        self._lurk_cursor += 1
        try:
            recent = await self.storage.recent_messages(scope_id, 15)
        except Exception:
            return
        for message in recent[-5:]:
            if not message.sender_id or message.sender_id == str(self._self_id() or ""):
                continue
            try:
                await self.storage.upsert_impression(
                    scope_id, str(message.sender_id),
                    display_name=message.sender_name or None,
                )
            except Exception:
                pass
        if recent:
            logger.info("长程记忆：潜水看了眼群%s（%d条，未发言）", key, len(recent))

    def _self_id(self) -> str:
        value = getattr(self.context, "bot_id", None)
        return str(value) if value else ""

    async def _mood_status_loop(self) -> None:
        """Deterministic self-maintenance: mood used to move only at the 6h
        reflection (and only when a group was active), and the signature only
        via a ~4% idle lottery — in practice both never moved on their own."""
        await asyncio.sleep(240)
        while not self._stopping:
            interval = max(1, self.settings.mood_update_minutes) * 60
            try:
                await self._mood_status_tick()
            except Exception as error:
                logger.warning("长程记忆：情绪/状态自更新异常：%s", str(error)[:200])
            await asyncio.sleep(interval * random.uniform(0.8, 1.3))

    async def _mood_status_tick(self) -> None:
        """Refresh mood from recent life; swap the personal signature when the
        mood outgrew it. Direct gateway call — no LLM dispatch lottery."""
        if not self._still_current_instance():
            return
        if not self._ready() or not self.mood or self.interactions.is_quiet():
            return
        self._bind_gateway_client()
        scope = next(iter(dict.fromkeys(self._known_scopes.values())), None)
        recent = await self.storage.recent_messages(scope, 8) if scope else []
        events = (
            await self.storage.recent_events(self._shared_scope_ids(scope), 5)
            if scope else []
        )
        if not recent and not events:
            return
        mood = self.mood.get()
        payload = compact_json(
            {
                "time": timeutil.text("%Y-%m-%d %H:%M %A"),
                "current_mood": mood.as_dict(),
                "recent_life": [
                    {"sender": m.sender_name, "text": m.text[:120]} for m in recent
                ],
                "pending_events": [e.text[:100] for e in events],
            },
            6000,
        )
        provider = self.settings.reply_provider_id or self.settings.summary_provider_id
        raw = await self._llm_text(
            provider, prompt=payload, system_prompt=MOOD_UPDATE_PROMPT
        )
        data = parse_json_object(raw) or {}
        new_mood = str(data.get("mood", "")).strip()
        if new_mood and new_mood != mood.mood:
            try:
                self.mood.set(
                    new_mood,
                    float(data.get("mood_intensity", mood.intensity)),
                    str(data.get("mood_note", "")),
                )
                logger.info("长程记忆：情绪已自更新为「%s」", new_mood[:20])
            except (TypeError, ValueError) as error:
                logger.info("长程记忆：情绪自更新被拒：%s", error)
        signature = str(data.get("new_signature", "")).strip()
        if (
            data.get("update_signature") and signature
            and self.gateway is not None and self.settings.enable_qq_tools
            and time.monotonic() - self._last_signature_at >= 6 * 3600
            and signature != self._last_signature_text
        ):
            try:
                await self.gateway.execute("set_self_longnick", longNick=signature[:80])
                self._last_signature_at = time.monotonic()
                self._last_signature_text = signature
                logger.info("长程记忆：个性签名已更新：%s", signature[:40])
            except Exception as error:
                logger.info("长程记忆：签名更新失败：%s", str(error)[:150])

    async def _reflection_loop(self) -> None:
        interval = max(self.settings.self_reflect_interval_hours, 1) * 3600
        await asyncio.sleep(120)
        while not self._stopping:
            try:
                await self._self_reflect_all()
            except Exception as error:
                logger.warning("长程记忆：自我总结异常：%s\n%s", error, traceback.format_exc())
            await asyncio.sleep(interval)

    async def _self_reflect_all(self) -> None:
        if not self._still_current_instance():
            return
        if not self.storage or not self.context_builder:
            return
        self._bind_gateway_client()
        for group_id, scope in list(self._known_scopes.items()):
            recent = await self.storage.recent_messages(scope, 5)
            if not recent:
                continue
            try:
                result = await self._self_reflect(scope, group_id)
                logger.info(
                    "长程记忆：群%s自我总结完成，随后行动%s个",
                    group_id, result.get("actions", 0),
                )
            except Exception as error:
                logger.warning("长程记忆：群%s自我总结失败：%s", group_id, error)

    async def _self_reflect(self, scope: str, group_id: str) -> dict[str, Any]:
        """Summarize recent behavior into memory, then run an autonomous action round."""
        assert self.storage and self.mood
        recent = await self.storage.recent_messages(scope, 40, include_events=True)
        pending_events = await self.storage.recent_events(self._shared_scope_ids(scope), 10)
        summaries = await self.storage.list_summaries(scope, 5)
        mood = self.mood.get()
        payload = compact_json(
            {
                "date": timeutil.text("%Y-%m-%d %H:%M %A"),
                "mood": mood.as_dict(),
                "recent_messages": [
                    {"sender": m.sender_name, "user_id": m.sender_id, "text": m.text[:200]}
                    for m in recent
                ],
                "pending_events": [
                    {"event": e.text[:200], "message_id": e.message_id} for e in pending_events
                ],
                "recent_summaries": [
                    {"title": s.title, "body": s.body[:400]} for s in summaries
                ],
            },
            self.settings.context_char_budget,
        )
        provider = self.settings.reply_provider_id or ""
        persona = await self._persona_prompt()
        prompt_system = "\n\n".join(
            part for part in (persona, REFLECTION_PROMPT.strip()) if part
        )
        raw = await self._llm_text(provider, prompt=payload, system_prompt=prompt_system)
        data = parse_json_object(raw) or {}
        summary_text = str(data.get("summary", "")).strip()
        if summary_text and recent:
            await self.storage.store_summary(
                scope, 3, recent[0].scope_seq, recent[-1].scope_seq,
                f"自我总结 {timeutil.text('%Y-%m-%d %H:%M')}",
                summary_text[:4000],
                [str(x) for x in data.get("topics", [])][:8] if isinstance(data.get("topics"), list) else [],
                [m.message_id for m in recent],
            )
        if data.get("mood"):
            try:
                self.mood.set(
                    str(data["mood"]), float(data.get("mood_intensity", 0.5)),
                    str(data.get("mood_note", "")),
                )
            except (TypeError, ValueError):
                pass
        impressions = data.get("impressions")
        if isinstance(impressions, list) and self.storage:
            for entry in impressions[:12]:
                if not isinstance(entry, dict):
                    continue
                user_id = str(entry.get("user_id", "")).strip()
                text = str(entry.get("impression", "")).strip()
                if not user_id or not text:
                    continue
                tags = entry.get("tags")
                tag_list = [str(t)[:20] for t in tags][:8] if isinstance(tags, list) else None
                try:
                    await self.storage.upsert_impression(
                        scope, user_id, impression=text[:600], tags=tag_list,
                    )
                except Exception as error:
                    logger.warning(
                        "长程记忆：写入对 %s 的印象失败：%s", user_id, str(error)[:160]
                    )
        thoughts = str(data.get("thoughts", "")).strip()
        if not thoughts or "什么都不做" in thoughts:
            return {"ok": True, "actions": 0, "summary": bool(summary_text)}
        outcome = await self._autonomous_action_loop(scope, "自我总结后的行动", thoughts)
        outcome["summary"] = bool(summary_text)
        return outcome

    async def _dispatch_task_action(self, tool: str, args: Any, scope: str | None) -> Any:
        args = args if isinstance(args, dict) else {}
        self._bind_gateway_client()
        self._refresh_settings_if_changed()
        if not self._still_current_instance():
            return "本实例已被新实例接管，动作取消"
        try:
            if tool == "web_search":
                return await search_web(str(args.get("query", "")))
            if tool == "python_exec":
                return await self._run_python_sandbox(
                    str(args.get("code", "")), None, scope or "")
            if tool in {"task_add", "task_list", "task_cancel", "task_update"} and self.task_queue:
                if tool == "task_list":
                    tasks = await self.task_queue.list(limit=int(args.get("limit", 20) or 20))
                    return compact_json({"tasks": [t.as_dict() for t in tasks]}, 6000)
                if tool == "task_cancel":
                    okay = await self.task_queue.cancel(str(args.get("task_id", "")))
                    return "已撤销" if okay else "没找到这个任务"
                if tool == "task_update":
                    changes = {k: v for k, v in args.items() if k != "task_id"}
                    okay = await self.task_queue.update(str(args.get("task_id", "")), **changes)
                    return "已更新" if okay else "没找到这个任务"
                task = await self.task_queue.add(
                    str(args.get("task_kind") or args.get("kind") or KIND_AGENT),
                    detail=str(args.get("detail", "")), title=str(args.get("title", "")),
                    scope_id=scope or "", payload=args.get("payload") or {},
                    delay_seconds=float(args.get("delay_minutes", 0) or 0) * 60,
                    interval_seconds=int(args.get("interval_minutes", 0) or 0) * 60,
                    max_runs=int(args.get("max_runs", 1) or 1))
                return compact_json({"ok": True, "task_id": task.task_id}, 800)
            if tool == "napcat_catalog" and self.gateway is not None:
                category = str(args.get("category", "") or "").strip()
                action = str(args.get("action", "") or "").strip()
                if action:
                    try:
                        spec = self.gateway.resolve_action(action)
                    except QQGatewayError as error:
                        return str(error)
                    return compact_json({
                        "action": spec.name, "category": spec.category,
                        "read_only": spec.read_only, "risk": spec.risk,
                        "summary": spec.summary,
                        "params": [
                            {"name": p.name, "type": p.type, "required": p.required,
                             "desc": p.desc,
                             **({"default": p.default} if p.default is not None else {})}
                            for p in spec.params],
                        **({"returns": spec.returns} if spec.returns else {}),
                    }, 6000)
                if not category:
                    return compact_json({
                        "categories": self.gateway.categories(),
                        "hint": "给 category 列出一类动作，或给 action 查单个动作参数",
                    }, 4000)
                items = self.gateway.catalog(category)
                if not items:
                    return ("没有分类 " + repr(category) + "；可用分类："
                            + "、".join(str(c["category"])
                                        for c in self.gateway.categories()))
                return compact_json({"category": category, "actions": items}, 16000)
            if tool == "napcat_call" and self.gateway is not None:
                params = args.get("params")
                params = params if isinstance(params, dict) else {}
                facade = GatewayFacade(self.gateway)
                try:
                    return compact_json(
                        await facade.call(
                            str(args.get("action", "")),
                            **{str(k): v for k, v in params.items()}), 6000)
                except QQGatewayError as error:
                    return f"动作失败：{error}"
            if tool == "qq_set_profile" and self.qq:
                return compact_json(await self.qq.set_profile(
                    nickname=str(args.get("nickname", "")) or None,
                    longnick=str(args.get("longnick", "")) or None), 3000)
            if tool == "qq_set_status" and self.qq:
                # 模型会把签名文本传给 status（实录：'月读渲染中勿扰'）——
                # 非数字一律当 0（在线），不能让 ValueError 炸掉动作循环
                try:
                    status_value = int(str(args.get("status", 0)).strip() or 0)
                except (TypeError, ValueError):
                    status_value = 0
                return compact_json(await self.qq.set_online_status(status_value), 3000)
            if tool == "qq_set_avatar" and self.qq and self.media:
                path = await self.media.get_path(str(args.get("media_id", "")))
                return compact_json(await self.qq.set_avatar("file://" + str(path)), 3000)
            if tool == "adjust_affinity" and self.storage:
                target_scope = scope or next(iter(dict.fromkeys(self._known_scopes.values())), None)
                if not target_scope:
                    return "还没有任何会话"
                return compact_json(await self.storage.adjust_affinity(
                    target_scope, str(args.get("user_id", "")),
                    float(args.get("delta", 0) or 0),
                    str(args.get("note", "") or "") or None), 1200)
            if tool == "get_affinity" and self.storage:
                target_scope = scope or next(iter(dict.fromkeys(self._known_scopes.values())), None)
                if not target_scope:
                    return "还没有任何会话"
                return compact_json(await self.storage.get_affinity(
                    target_scope, str(args.get("user_id", ""))), 1200)
            if tool == "get_user_style" and self.storage:
                target_scope = scope or next(iter(dict.fromkeys(self._known_scopes.values())), None)
                if not target_scope:
                    return "还没有任何会话"
                return compact_json(
                    await self.storage.get_style(target_scope, str(args.get("user_id", "")))
                    or {"note": "还没有TA的风格档案"}, 1500)
            if tool == "group_topics" and self.storage:
                target_scope = scope or next(iter(dict.fromkeys(self._known_scopes.values())), None)
                if not target_scope:
                    return compact_json([], 100)
                return compact_json(await self.storage.recent_topics(
                    target_scope, min(max(int(args.get("limit", 10) or 10), 1), 30)), 4000)
            if tool == "list_stickers" and self.stickers:
                return compact_json(self.stickers.catalog(
                    min(max(int(args.get("limit", 20) or 20), 1), 50)), 5000)
            if tool == "pick_sticker" and self.stickers:
                return compact_json(self.stickers.pick(
                    str(args.get("query", "")),
                    min(max(int(args.get("limit", 5) or 5), 1), 10)), 3000)
            if tool == "send_image" and self.media is not None and self.gateway is not None:
                path = await self.media.get_path(str(args.get("media_id", "")))
                segment = {"type": "image", "data": {"file": "file://" + str(path)}}
                target_group = str(args.get("group_id", "")).strip()
                target_user = str(args.get("user_id", "")).strip()
                if target_group:
                    if not self.settings.allows_group(target_group):
                        return f"拒绝发送：群 {target_group} 不在白名单"
                    return compact_json(await self.gateway.execute(
                        "send_group_msg", group_id=int(target_group), message=[segment]), 3000)
                if target_user:
                    return compact_json(await self.gateway.execute(
                        "send_private_msg", user_id=int(target_user), message=[segment]), 3000)
                return "需要 group_id 或 user_id"
            if tool == "look_at_image" and self.media:
                mime, b64 = await self.media.get_base64(str(args.get("media_id", "")))
                description = await self._describe_images([(mime, b64)])
                return description or "图片识别失败（未配置可用的视觉模型？）"
            if tool == "read_document" and self.media:
                from .src.pdf_reader import extract_document_text

                try:
                    doc_path = await self.media.get_path(str(args.get("media_id", "")))
                except Exception as error:
                    return f"取文件失败：{str(error)[:140]}"
                doc = await asyncio.to_thread(
                    extract_document_text, doc_path,
                    max_chars=min(max(int(args.get("max_chars", 12000) or 12000), 500), 30000))
                return compact_json(doc, 16000)
            if tool == "media_recent" and self.media:
                target_scope = scope or next(iter(dict.fromkeys(self._known_scopes.values())), None)
                return compact_json(await self.media.recent(
                    target_scope, min(max(int(args.get("limit", 15) or 15), 1), 100)), 8000)
            if tool == "list_known_users" and self.storage:
                target_scope = scope or next(iter(dict.fromkeys(self._known_scopes.values())), None)
                people = await self.storage.list_impressions(
                    self._shared_scope_ids(target_scope or ""),
                    min(max(int(args.get("limit", 20) or 20), 1), 40),
                    query=str(args.get("query", "")) or None)
                return compact_json(people, 5000)
            if tool == "extension_list" and self._extensions is not None:
                return compact_json({"extensions": self._extensions.list()}, 6000)
            if tool in {"dispatch_subagent", "dispatch_parallel_subagents"}:
                if not self.settings.subagent_enabled:
                    return "子agent未在 WebUI 启用"
                if tool == "dispatch_subagent":
                    return await self._run_subagent_task(
                        str(args.get("task", "")), scope,
                        self._parse_media_ids(str(args.get("media_ids_json", ""))))
                raw_tasks = args.get("tasks")
                parsed_tasks: list[tuple[str, list[str]]] = []
                if isinstance(raw_tasks, list):
                    for item in raw_tasks[:4]:
                        if isinstance(item, str) and item.strip():
                            parsed_tasks.append((item.strip()[:400], []))
                        elif isinstance(item, dict) and str(item.get("task", "")).strip():
                            parsed_tasks.append((
                                str(item.get("task")).strip()[:400],
                                self._parse_media_ids(json.dumps(
                                    item.get("media_ids") or [], ensure_ascii=False)),
                            ))
                if not parsed_tasks:
                    return "需要 tasks 任务数组"
                results = await asyncio.gather(
                    *(self._run_subagent_task(task, scope, media_ids)
                      for task, media_ids in parsed_tasks),
                    return_exceptions=True,
                )
                labeled = [
                    {"task": task,
                     "result": result if isinstance(result, str)
                     else f"失败：{type(result).__name__}"}
                    for (task, _media), result in zip(parsed_tasks, results)
                ]
                return compact_json(labeled, 8000)
            if tool in {"ssh_exec", "ssh_info", "ssh_agent_dispatch",
                        "ssh_agent_status", "ssh_agent_control",
                        "ssh_fetch", "ssh_screenshot"}:
                return await self._dispatch_ssh_action(tool, args)
            if tool in {"fs_list", "fs_read", "fs_write", "fs_delete", "fs_move",
                        "fs_search", "run_script"}:
                return await self._dispatch_workspace_action(tool, args)
            if tool == "read_tabular":
                from .src.data_tools import run_tabular

                return await run_tabular(
                    str(args.get("pandas_operations", "")),
                    file_path=str(args.get("file_path", "")),
                    resolve_path=self._workspace().resolve,
                    extra_globals=self._sandbox_globals(None, scope or ""),
                )
            if tool == "send_local_file":
                path = await self._resolve_send_path(args)
                if not path:
                    return "找不到要发送的文件（path 不存在且 media_id 无效）"
                kind = str(args.get("kind") or "").strip()
                if kind not in self._MEDIA_KIND_EXT:
                    kind = self._media_kind_by_name(path)
                group_id = str(args.get("group_id", "")).strip()
                user_id = str(args.get("user_id", "")).strip()
                if group_id and not self.settings.allows_group(group_id):
                    return f"拒绝发送：群 {group_id} 不在白名单"
                if not group_id and not user_id:
                    return "需要 group_id 或 user_id（自主任务没有默认会话）"
                if self._as_qq_number(group_id or user_id) is None:
                    return (f"目标 {group_id or user_id!r} 不是 QQ 号/群号，"
                            "无法经 QQ 网关发送")
                try:
                    return compact_json(await self._send_media_file(
                        path, kind, group_id=group_id, user_id=user_id,
                        file_name=str(args.get("file_name", ""))), 1200)
                except Exception as error:
                    return f"发送失败：{str(error)[:150]}"
            if tool in {"design_render", "design_list", "design_load",
                        "program_write", "program_list", "program_read",
                        "program_view", "program_screenshot", "program_archive"}:
                return await self._dispatch_studio_action(tool, args)
            if tool == "script_call":
                try:
                    params = args if isinstance(args, dict) else {}
                    return await self._script_manager().call(
                        str(args.get("script", "")) if isinstance(args, dict) else "",
                        str(args.get("tool", "")) if isinstance(args, dict) else "",
                        params.get("params") if isinstance(params.get("params"), dict)
                        else {},
                    )
                except ScriptExtensionError as error:
                    return f"拓展调用失败：{error}"
                except Exception as error:
                    return f"拓展调用失败：{type(error).__name__}: {str(error)[:200]}"
            if tool == "evolve_prompt":
                target = str(args.get("target", "")).strip()
                text = self._evolution_target_text(target)
                if text is None:
                    return "未知进化目标：designer / program / reply"
                optimizer = self._evolution_optimizer(target)
                self._spawn(self._run_evolution(
                    optimizer, target, text, int(args.get("iterations", 4) or 4)))
                return "进化任务已在后台派出"
            if tool == "manage_intent":
                return await self.manage_intent_tool(
                    None, str(args.get("action", "")),
                    keywords=str(args.get("keywords", "")),
                    instruction=str(args.get("instruction", "")),
                    intent_id=str(args.get("intent_id", "")))
            if tool == "todo":
                return await self._todo_action(
                    scope, str(args.get("action", "")),
                    items_json=str(args.get("items_json", "")),
                    todo_id=str(args.get("todo_id", "")),
                    status=str(args.get("status", "")),
                    content=str(args.get("content", "")))
            if tool in {"learn_skill", "update_skill"}:
                return await self._skill_tool_action(tool, dict(args))
            if tool == "script_list":
                manager = self._script_manager()
                return compact_json({
                    "scripts": manager.list(), "tools": manager.tool_catalog()}, 6000)
            if tool == "render_code":
                from .src.code_render import code_to_page

                code_text = str(args.get("code", ""))
                if not code_text.strip():
                    return "需要 code"
                page = code_to_page(
                    code_text, str(args.get("language", "")),
                    str(args.get("filename", "")))
                try:
                    media = await self._design_screenshot(page)
                except Exception as error:
                    return f"代码渲染失败：{str(error)[:160]}"
                return media and compact_json(
                    {"ok": True, "media_id": media,
                     "hint": "send_image(media_id) 发给用户"}, 1500) or "媒体归档未启用"
            if tool == "browser_dom":
                try:
                    dom_url = str(args.get("url", "")).strip()
                    driver = self._pw_driver()
                    if dom_url:
                        await driver.goto(dom_url)
                    snapshot = await driver.dom_snapshot()
                except BrowserError as error:
                    return f"browser_dom 失败：{error}"
                return compact_json(snapshot, 9000)
            if tool == "browser_scroll":
                try:
                    element_index = int(args.get("element_index", -1) or -1)
                    state = await self._pw_driver().scroll(
                        amount=int(args.get("amount", 600) or 0),
                        to_bottom=bool(args.get("to_bottom", False)),
                        index=element_index if element_index >= 0 else None,
                    )
                except BrowserError as error:
                    return f"滚动失败：{error}"
                return compact_json(state, 2000)
            if tool == "browser_screenshot":
                try:
                    shot_url = str(args.get("url", "")).strip()
                    png = await self._pw_driver().screenshot(shot_url or None)
                except BrowserError as error:
                    return f"截图失败：{error}"
                if self.media is None:
                    return "媒体归档未启用，无法生成图片"
                record = await self.media.save_bytes(
                    png, scope_id="studio", kind="image", mime="image/png",
                    note="autonomous-shot")
                return compact_json({"ok": True, "media_id": record.item_id}, 1000)
            if tool == "browse" and self._browser is not None:
                page = await self._browser_session().fetch(str(args.get("url", "")))
                return compact_json(self._browser_session().snapshot(), 8000)
            if tool == "browser_click" and self._browser is not None:
                link = self._browser_session().link(int(args.get("index", 0)))
                await self._browser_session().fetch(link.href)
                return compact_json(self._browser_session().snapshot(), 8000)
            if tool == "browser_links" and self._browser is not None:
                return compact_json(
                    self._browser_session().snapshot(link_limit=30).get("links", []),
                    6000)
            if tool == "browser_find" and self._browser is not None:
                return self._browser_find_text(
                    str(args.get("keyword", "")), int(args.get("limit", 5)))
            if tool == "kb_list":
                return compact_json({"kbs": await self._kb_list_data()}, 4000)
            if tool == "kb_search":
                return await self._kb_search_data(
                    str(args.get("query", "")), str(args.get("kb_id", "")),
                    int(args.get("limit", 5)))
            if tool == "extension_call" and self._extensions is not None:
                call_args = args.get("args")
                call_args = call_args if isinstance(call_args, dict) else {}
                return await self._extensions.call(
                    str(args.get("extension", "")), str(args.get("tool", "")),
                    {str(k): v for k, v in call_args.items()})
            if tool == "web_fetch":
                return await fetch_text(
                    str(args.get("url", "")),
                    max_bytes=self.settings.web_fetch_max_mb * 1024 * 1024,
                )
            if tool == "qzone_publish" and self.qzone:
                return await self.qzone.publish(str(args.get("text", "")))
            if tool == "qzone_list" and self.qzone:
                return await self.qzone.list_feeds(int(args.get("count", 10)))
            if tool == "qzone_like" and self.qzone:
                return await self.qzone.like(
                    str(args.get("unikey", "")), str(args.get("curkey", "")), str(args.get("owner_uin", "")))
            if tool == "qzone_comment" and self.qzone:
                return await self.qzone.comment(str(args.get("topic_id", "")), str(args.get("content", "")))
            if tool == "qq_send_group" and self.gateway:
                target_group = str(args.get("group_id", "")).strip()
                if not self.settings.allows_group(target_group):
                    return f"拒绝发送：群 {target_group} 不在白名单，不能跨群发言"
                return await self.gateway.execute(
                    "send_group_msg", group_id=int(target_group or 0),
                    message=[{"type": "text", "data": {"text": _bubble_text(args.get("text", ""))}}])
            if tool == "qq_send_private" and self.gateway:
                return await self.gateway.execute(
                    "send_private_msg", user_id=int(args.get("user_id", 0)),
                    message=[{"type": "text", "data": {"text": _bubble_text(args.get("text", ""))}}])
            if tool == "qq_handle_friend_request" and self.qq:
                return await self.qq.handle_friend_request(
                    str(args.get("flag", "")), bool(args.get("approve", True)),
                    str(args.get("remark", "")))
            if tool == "qq_handle_group_invite" and self.qq:
                return await self.qq.handle_group_invite(
                    str(args.get("flag", "")), bool(args.get("approve", True)),
                    str(args.get("reason", "")),
                    sub_type=str(args.get("sub_type", "") or "invite"))
            if tool == "recent_events" and self.storage:
                return await self.storage.recent_events(
                    list(self._known_scopes.values()) or None, int(args.get("limit", 10)))
            if tool == "set_mood" and self.mood:
                return self.mood.set(
                    str(args.get("mood", "")), float(args.get("intensity", 0.3)),
                    str(args.get("note", ""))).as_dict()
            if tool == "remember" and self.storage and scope:
                recent = await self.storage.recent_messages(scope, 5)
                ids = [m.message_id for m in recent]
                start = recent[0].scope_seq if recent else 0
                end = recent[-1].scope_seq if recent else 0
                return await self.storage.store_summary(
                    scope, 3, start, end, str(args.get("title", "记忆"))[:120],
                    str(args.get("summary", ""))[:4000], [], ids)
            if tool == "add_plan" and self.storage:
                detail = str(args.get("detail", "")).strip()[:400]
                if not detail:
                    return "add_plan 需要 detail"
                hours = float(args.get("hours_ahead", 1) or 1)
                hours = min(max(hours, 0.05), 72)
                due = datetime.now(timezone.utc) + timedelta(hours=hours)
                count = await self.storage.add_plans([{
                    "due_at": due.isoformat(timespec="minutes"),
                    "kind": str(args.get("kind", "custom"))[:30] or "custom",
                    "detail": detail,
                }])
                return f"已排入日程（约{hours:.1f}小时后）"
            if tool == "list_plans" and self.storage:
                return compact_json(await self.storage.pending_plans(15), 3000)
            if tool == "drop_plan" and self.storage:
                ok = await self.storage.complete_plan(
                    str(args.get("plan_id", "")), "skipped", "LLM主动取消")
                return "已取消" if ok else "没有这项计划"
            if tool == "update_impression" and self.storage and scope:
                return compact_json(await self.storage.upsert_impression(
                    scope, str(args.get("user_id", "")),
                    impression=str(args.get("impression", "")) or None,
                    tags=[str(t) for t in (args.get("tags") or [])]
                    if isinstance(args.get("tags"), list) else None,
                ), 1200)
            if tool == "get_user_profile" and self.storage and scope:
                user = str(args.get("user_id", ""))
                affinity = await self.storage.get_affinity(scope, user)
                impression = await self.storage.get_impression(scope, user)
                return compact_json({"affinity": affinity, "impression": impression}, 2000)
            if tool == "send_sticker" and self.stickers:
                return await self._send_sticker_action(args)
            if tool == "done":
                return "done"
            return f"unknown tool: {tool}"
        except Exception as error:
            logger.warning(
                "长程记忆：自主动作 %s 执行失败：%s: %s",
                tool, type(error).__name__, str(error)[:200],
            )
            return f"{type(error).__name__}: {error}"[:300]

    async def _dispatch_studio_action(self, tool: str, args: dict[str, Any]) -> Any:
        """designer/programmer tools reachable from the scheduler/heartbeat channel."""
        args = args if isinstance(args, dict) else {}
        if tool == "design_render":
            return await self.design_render_tool(
                None, html=str(args.get("html", "")), svg=str(args.get("svg", "")),
                title=str(args.get("title", "")),
                description=str(args.get("description", "")),
                project_id=str(args.get("project_id", "")),
                save=bool(args.get("save", False)),
            )
        if tool == "design_list":
            return await self.design_list_tool(None)
        if tool == "design_load":
            return await self.design_load_tool(None, str(args.get("project_id", "")))
        if tool == "program_write":
            return await self.program_write_tool(
                None, code=str(args.get("code", "")),
                title=str(args.get("title", "")),
                description=str(args.get("description", "")),
                program_id=str(args.get("program_id", "")),
            )
        if tool == "program_list":
            return await self.program_list_tool(None)
        if tool == "program_read":
            return await self.program_read_tool(None, str(args.get("program_id", "")))
        if tool == "program_view":
            return await self.program_view_tool(
                None, str(args.get("program_id", "")), str(args.get("path", "/")))
        if tool == "program_screenshot":
            return await self.program_screenshot_tool(
                None, str(args.get("program_id", "")), str(args.get("path", "/")))
        if tool == "program_archive":
            return await self.program_archive_tool(
                None, str(args.get("program_id", "")),
                title=str(args.get("title", "")),
                description=str(args.get("description", "")),
            )
        return f"unknown studio tool: {tool}"

    async def _dispatch_ssh_action(self, tool: str, args: dict[str, Any]) -> Any:
        """SSH tools reachable from the scheduler/heartbeat dispatch channel."""
        if self._ssh is None or self._ssh_agents is None or not self._ssh_configured():
            return "SSH 未配置：请在插件配置填 ssh_host / ssh_user / ssh_password（目前仅支持 Ubuntu）"
        if tool == "ssh_exec":
            seconds = int(args.get("timeout", 0) or 0) or self.settings.ssh_command_timeout
            try:
                return compact_json(await self._ssh.exec(
                    str(args.get("command", "")), timeout=float(seconds)),
                    self.settings.context_char_budget // 2)
            except SSHError as error:
                return f"SSH 执行失败：{error}"
        if tool == "ssh_fetch":
            return await self._ssh_fetch_core(
                str(args.get("remote_path", "")), str(args.get("note", "")))
        if tool == "ssh_screenshot":
            return await self._ssh_shot_core(
                str(args.get("url", "")),
                int(args.get("width", 1280) or 1280),
                int(args.get("height", 900) or 900))
        if tool == "ssh_info":
            cfg = self._ssh_config()
            info: dict[str, Any] = {"target": cfg.target(),
                                    "notes": cfg.notes or "（未填写机器注释）"}
            try:
                info["probe"] = await self._ssh.check()
            except SSHError as error:
                info["probe_error"] = str(error)
            return compact_json(info, 6000)
        if tool == "ssh_agent_dispatch":
            try:
                session = self._ssh_agents.dispatch(str(args.get("task", "")))
            except SSHError as error:
                return f"派发失败：{error}"
            return compact_json({"ok": True, "session_id": session.id,
                                 "state": session.state}, 2000)
        if tool == "ssh_agent_status":
            status = self._ssh_agents.status(str(args.get("session_id", "")))
            return compact_json(status if status is not None else {"error": "没有这个会话"},
                                self.settings.context_char_budget // 2)
        if tool == "ssh_agent_control":
            return compact_json(self._ssh_agents.control(
                str(args.get("session_id", "")), str(args.get("action", "")),
                str(args.get("message", ""))), self.settings.context_char_budget // 2)
        return f"unknown ssh tool: {tool}"

    def _page_owner(self) -> str | None:
        username = getattr(request, "username", None)
        if callable(username):
            try:
                username = username()
            except Exception:
                return None
        return str(username) if username else None

    # ------------------------------------------------------------ WebUI（重写版）
    def _page_api(self) -> Any:
        """WebUI 数据面/动作面（src/webui.py）；惰性创建。"""
        api = getattr(self, "_page_api_obj", None)
        if api is None:
            from .src.webui import PageAPI

            api = PageAPI(self)
            self._page_api_obj = api
        return api

    def _register_page_routes(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            return
        from .src.webui import READ_ROUTES

        async def _read(route: str):
            guard = self._page_gate()
            if guard is not None:
                return guard
            try:
                data = await self._page_api().read(route, self._page_query())
            except Exception as error:
                logger.warning("长程记忆：WebUI 读取 %s 失败：%s", route, error)
                return error_response(f"{type(error).__name__}: {error}"[:250],
                                      status_code=400)
            return self._page_ok(data)

        for route, desc in READ_ROUTES:
            register(f"{_PAGE_PREFIX}/{route}", (lambda r=route: _read(r)),
                     ["GET"], desc)

        async def _write():
            guard = self._page_gate()
            if guard is not None:
                return guard
            body = await self._page_body()
            action = str(body.get("action", "")).strip()
            if not action:
                return error_response("缺少 action", status_code=400)
            try:
                data = await self._page_api().write(action, body)
            except Exception as error:
                logger.warning("长程记忆：WebUI 操作 %s 失败：%s", action, error)
                return error_response(f"{type(error).__name__}: {error}"[:250],
                                      status_code=400)
            if isinstance(data, dict) and data.get("ok") is False:
                return error_response(str(data.get("error") or "操作未完成")[:250],
                                      status_code=400)
            return self._page_ok(data)

        register(f"{_PAGE_PREFIX}/action", _write, ["POST"], "执行操作")

    def _unregister_page_routes(self) -> None:
        routes = getattr(self.context, "registered_web_apis", None)
        if routes is None:
            return
        try:
            routes[:] = [item for item in routes
                         if not str(item[0] if isinstance(item, (tuple, list)) else item)
                         .startswith(_PAGE_PREFIX)]
        except Exception as error:
            logger.warning("长程记忆：注销页面路由失败：%s", str(error)[:120])

    def _page_gate(self):
        """页面访问闸：仅 Dashboard 登录用户；插件未就绪时给出明确提示。"""
        if not self._page_owner():
            return error_response("仅 Dashboard 登录用户可访问", status_code=403)
        if not self._ready():
            return error_response("插件尚未初始化完成", status_code=503)
        return None

    @staticmethod
    def _page_ok(data: Any, message: str = "") -> Any:
        """页面信封（参照 qqwebui）：{"ok": True, "message": ..., "data": ...}。"""
        return json_response({
            "ok": True,
            "message": message,
            "data": _public_value(data) if data is not None else {},
        })

    @staticmethod
    def _page_owner() -> str | None:
        username = getattr(request, "username", None)
        if callable(username):
            try:
                username = username()
            except Exception:
                return None
        return str(username) if username else None

    @staticmethod
    def _page_query() -> Mapping[str, Any]:
        query = getattr(request, "query", None)
        if query is None:
            return {}
        try:
            return {str(k): v for k, v in dict(query).items()}
        except Exception:
            return {}

    @staticmethod
    async def _page_body() -> dict[str, Any]:
        """POST body：`await request.json(default={})`（参照 qqwebui）。"""
        getter = getattr(request, "json", None)
        if not callable(getter):
            return {}
        value = None
        try:
            value = getter(default={})
        except TypeError:
            try:
                value = getter()
            except Exception:
                return {}
        except Exception:
            return {}
        if hasattr(value, "__await__"):
            try:
                value = await value
            except Exception:
                return {}
        return dict(value) if isinstance(value, Mapping) else {}

    def _load_event_watermark(self, path: Path) -> None:
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            self._handled_event_ids = {str(x) for x in data.get("handled", [])}
            self._handled_event_keys = {str(x) for x in data.get("handled_keys", [])}
            try:
                self._handled_since = str(data.get("since", "") or "")
            except Exception:
                self._handled_since = ""
            self._event_watermark_path = path
        except (OSError, ValueError):
            self._event_watermark_path = path

    def _event_watermark_key(self, event: Any) -> str:
        """Stable key for an event, independent of the per-run message_id.

        Replaying old notices mints fresh message_ids, so a message_id set
        cannot stop a re-installed plugin from re-handling history; the
        upstream id + scope + occurred_at can.
        """
        return "\0".join((
            str(getattr(event, "scope_id", "")),
            str(getattr(event, "upstream_message_id", "") or getattr(event, "upstream_message_id", "")),
            str(getattr(event, "occurred_at", "")),
        ))

    def _save_event_watermark(self) -> None:
        import json

        try:
            self._event_watermark_path.write_text(
                json.dumps({
                    "handled": list(self._handled_event_ids)[-300:],
                    "handled_keys": list(self._handled_event_keys)[-600:],
                    "since": str(getattr(self, "_handled_since", "") or "")[:40],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _schedule_event_processing(self) -> None:
        if self._stopping or not self.settings.auto_handle_requests:
            return
        if self._event_timer is not None and not self._event_timer.done():
            return
        self._event_timer = asyncio.create_task(self._process_pending_events_later())

    async def _process_pending_events_later(self) -> None:
        await asyncio.sleep(20)
        try:
            await self._process_pending_events()
        except Exception as error:
            logger.warning("长程记忆：通知处理异常：%s", error)

    async def _process_pending_events(self) -> None:
        """Wake the LLM to view and handle pending events; details stay in the
        queryable event store, only a compact line lands in main memory."""
        if not self.storage or not self.context_builder or self._stopping:
            return
        scopes = list(self._known_scopes.values())
        if not scopes:
            return
        self._bind_gateway_client()
        events = await self.storage.recent_events(scopes, 12)
        since = str(getattr(self, "_handled_since", "") or "")
        pending = []
        for e in events:
            # 重装/清库后 handled 集合为空，基线之前的历史通知一律不再处理
            if since and str(getattr(e, "occurred_at", "") or "") < since:
                continue
            if e.message_id in self._handled_event_ids:
                continue
            if self._event_watermark_key(e) in self._handled_event_keys:
                # history replayed by a fresh install: already handled once
                self._handled_event_ids.add(e.message_id)
                continue
            pending.append(e)
        if not pending:
            self._save_event_watermark()
            return
        labels = await self._scope_labels()
        listing = compact_json(
            [
                {"message_id": e.message_id, "origin": labels.get(e.scope_id, e.scope_id[:8]),
                 "event": e.text[:220]}
                for e in pending
            ],
            6000,
        )
        instruction = (
            f"有 {len(pending)} 条待处理通知，逐条处理。**默认态度**：好友申请同意（remark 写对方"
            "来自的群或能认人的备注），群邀请接受，别人申请进你的群也同意；只有明显是广告/骚扰/"
            "可疑小号才拒绝，并在 reason 里写明。\n"
            "动作写成 JSON：{\"actions\":[{\"tool\":\"qq_handle_friend_request\","
            "\"args\":{\"flag\":\"<从事件文本里抄>\",\"approve\":true,\"remark\":\"群友\"}}],"
            "\"thought\":\"为什么这么处理\",\"done\":true}；群相关申请改用 "
            "qq_handle_group_invite(flag,approve,reason,sub_type)，"
            "**sub_type 照抄事件文本里的值**（invite=邀请你进群，add=别人申请进你的群），"
            "抄错会导致申请一直挂着；其他通知如需回应自行决定。"
            "若决定不处理，thought 里必须写明理由（会记进日志）。\n"
            f"待处理列表：{listing}"
        )
        result = await self._autonomous_action_loop(pending[0].scope_id, "通知处理", instruction)
        details = [
            item for item in result.get("results", [])
            if isinstance(item, dict) and item.get("tool") in {
                "qq_handle_friend_request", "qq_handle_group_invite", "qq_send_private",
                "qq_send_group", "set_mood",
            }
        ]
        thought = str(result.get("last_thought", "") or "").strip()[:120]
        brief = "；".join(
            f"{item['tool']}→{str(item.get('result', ''))[:40]}" for item in details[:6]
        ) or (f"未执行处理动作；理由是：{thought}" if thought else "未执行处理动作")
        compact = f"[通知处理] 处理了{len(pending)}条事件：{brief}"
        now_stamp = timeutil.text("%Y-%m-%d %H:%M")
        for scope_id in {e.scope_id for e in pending}:
            try:
                tail = await self.storage.recent_messages(scope_id, 1, include_events=True)
                end_seq = tail[-1].scope_seq if tail else 0
                await self.storage.store_summary(
                    scope_id, 3, end_seq, end_seq,
                    f"通知处理 {now_stamp}", compact[:500], [],
                    [e.message_id for e in pending if e.scope_id == scope_id],
                )
            except Exception as error:
                logger.warning("长程记忆：通知主线摘要写入失败：%s", error)
        self._handled_event_ids.update(e.message_id for e in pending)
        self._handled_event_keys.update(self._event_watermark_key(e) for e in pending)
        self._save_event_watermark()
        logger.info("长程记忆：%s", compact)

    async def _proactive_loop(self) -> None:
        interval = max(self.settings.proactive_interval_minutes, 1) * 60
        await asyncio.sleep(120)
        while not self._stopping:
            try:
                await self._proactive_round()
            except Exception as error:
                logger.warning("长程记忆：主动参与异常：%s\n%s", error, traceback.format_exc())
            await asyncio.sleep(interval + random.uniform(0, interval * 0.3))

    async def _proactive_round(self) -> None:
        if not self._still_current_instance():
            return
        self._bind_gateway_client()
        if not self.storage or not self.context_builder or self.gateway is None:
            return
        if self.interactions.is_quiet():
            return
        now_ts = datetime.now(timezone.utc).timestamp()
        for key, scope_id in list(self._known_scopes.items()):
            if key.startswith("private:") or not self.settings.allows_group(key):
                continue
            recent = await self.storage.recent_messages(scope_id, 5)
            if not recent:
                continue
            try:
                last_seen = datetime.fromisoformat(recent[-1].occurred_at).timestamp()
            except (TypeError, ValueError):
                last_seen = 0
            if now_ts - last_seen > 3600:
                continue
            if time.monotonic() < self._send_blockade.get(scope_id, 0.0):
                continue
            permit = await self.interactions.begin(
                scope_id, key, "proactive", supersede=False,
            )
            if permit is None or not await self.interactions.commit(permit):
                continue
            topics = await self.storage.recent_topics(scope_id, 6)
            temperature = self.mood.reply_temperature() if self.mood else {"tier": "neutral"}
            context = await self.context_builder.build(
                scope_id, "", recent_limit=self.settings.recent_message_limit,
                runtime_manifest={
                    "mood": self.mood.get().as_prompt() if self.mood else "",
                    "reply_temperature": temperature,
                },
                cross_scope_ids=self._shared_scope_ids(scope_id),
                cross_labels={v: k for k, v in self._known_scopes.items()},
            )
            try:
                pending_events = await self.storage.recent_events(scope_id, 8)
            except Exception:
                pending_events = []
            instruction = (
                f"群{key}的近期讨论话题（按新到旧）：{compact_json(topics, 1200) or '暂无记录'}。"
                f"待处理事件：{compact_json([e.text[:120] for e in pending_events], 1500) or '无'}。"
                f"当前表达温度：{temperature['tier']}——"
                f"{'克制少说' if temperature['tier'] == 'guarded' else '正常' if temperature['tier'] == 'neutral' else '可以主动一点'}。"
                "这是你的例行巡查，像真人打理自己的QQ一样自主决定（不限于聊天）："
                "有好友申请/群邀请就明确处理；群里有值得接的话题就 qq_send_group 发1-2句短气泡"
                f"（group_id 只能是 {key}，其他群的内容只是背景，绝不能发到别的群）；"
                "发现值得私聊的群友可以用 qq_send_private；"
                "有值得记住的就用 remember；顺手 adjust_affinity、set_mood；"
                "氛围合适就用 pick_sticker 挑一张 + send_sticker 发群里活跃气氛；"
                "实在不想说话就只输出 done。禁止刷屏，禁止客服腔。"
            )
            result = await self._autonomous_action_loop(
                scope_id, f"主动参与群{key}", instruction
            )
            if result.get("actions"):
                logger.info(
                    "长程记忆：群%s主动参与，执行%s个动作", key, result["actions"]
                )
            await asyncio.sleep(random.uniform(3, 8))

    async def _memory_facade(self, event: AstrMessageEvent,
                             scope_id: str | None = None) -> ScopedMemoryFacade:
        if not self.storage or not self.retrieval or not self.ledger:
            raise RuntimeError("记忆系统尚未就绪")
        scope = str(scope_id or "").strip() or await self._scope_for_event(event, create=True)
        if not scope:
            raise PermissionError("当前会话未启用记忆")
        return ScopedMemoryFacade(
            scope, self.storage, self.retrieval, self.ledger,
            scope_ids=self._shared_scope_ids(scope),
        )

    @filter.llm_tool(name="memory_catalog")
    async def memory_catalog_tool(
        self, event: AstrMessageEvent, query: str = "", kind: str = "", limit: int = 20
    ):
        """浏览当前群的总体记忆目录。

        Args:
            query(string): 可选主题或实体关键词。
            kind(string): 可选类型，如 fact、preference、decision、task、conflict。
            limit(number): 返回条数，最大20。
        """
        facade = await self._memory_facade(event)
        kinds = [kind] if kind else None
        entries = await facade.memory_catalog(query=query or None, kinds=kinds, limit=min(max(limit, 1), 20))
        return (compact_json([_public_value(item) for item in entries], 10000))

    @filter.llm_tool(name="search_memory_summaries")
    async def search_memory_summaries_tool(
        self, event: AstrMessageEvent, query: str = "", level: int = 0, limit: int = 8
    ):
        """搜索当前群的分层压缩摘要。

        Args:
            query(string): 主题关键词，可为空。
            level(number): 0表示全部，或1、2、3。
            limit(number): 返回条数，最大20。
        """
        facade = await self._memory_facade(event)
        levels = [level] if level in {1, 2, 3} else None
        items = await facade.search_memory_summaries(query=query or None, levels=levels, limit=min(max(limit, 1), 20))
        return (compact_json([_public_value(item) for item in items], 12000))

    @filter.llm_tool(name="search_chat_history")
    async def search_chat_history_tool(
        self, event: AstrMessageEvent, query: str, sender_id: str = "",
        group_id: str = "", limit: int = 12
    ):
        """在聊天事实层中搜索消息（默认当前会话，也可以指定别的群）。

        Args:
            query(string): 关键词或精确细节查询。
            sender_id(string): 可选发送者QQ号。
            group_id(string): 要搜的群号**或群名**（如"数学指令讨论群"）；留空=当前会话。
            limit(number): 返回片段数，最大20。
        """
        scope_id = ""
        if str(group_id or "").strip():
            scope_id, _key, hint = await self._resolve_scope_ref(group_id)
            if not scope_id:
                return f"没能定位这个群：{hint}"
        facade = await self._memory_facade(event, scope_id=scope_id or None)
        items = await facade.search_chat_history(
            query, sender_id=sender_id or None, limit=min(max(limit, 1), 20)
        )
        payload = [_public_value(item) for item in items]
        if str(group_id or "").strip() and not payload:
            return f"（{group_id} 里没有匹配「{query}」的记录）"
        return (compact_json(payload, 12000))

    @filter.llm_tool(name="get_chat_messages")
    async def get_chat_messages_tool(self, event: AstrMessageEvent, message_ids: list[str]):
        """按搜索结果中的内部消息ID读取当前群少量精确原文。

        Args:
            message_ids(array[string]): 内部消息ID，最多8个。
        """
        facade = await self._memory_facade(event)
        items = await facade.get_chat_messages(list(message_ids)[:8], limit=8)
        return (compact_json([_public_value(item) for item in items], 14000))

    @filter.llm_tool(name="get_chat_context")
    async def get_chat_context_tool(self, event: AstrMessageEvent, message_id: str, radius: int = 3):
        """读取当前群某条消息前后的原文上下文。

        Args:
            message_id(string): 内部消息ID。
            radius(number): 前后消息数，最大5。
        """
        facade = await self._memory_facade(event)
        items = await facade.get_chat_context(message_id, min(max(radius, 0), 5))
        return (compact_json([_public_value(item) for item in items], 16000))

    @filter.llm_tool(name="propose_memory_update")
    async def propose_memory_update_tool(
        self,
        event: AstrMessageEvent,
        kind: str,
        subject: str,
        value: str,
        evidence_ids: list[str],
        confidence: float = 0.5,
    ):
        """依据当前群原文证据提出一条结构化长期记忆。

        Args:
            kind(string): fact、preference、relationship、rule、decision、task、conflict、topic或joke。
            subject(string): 记忆主体。
            value(string): 有证据支持的内容。
            evidence_ids(array[string]): 当前群内部消息ID。
            confidence(number): 0到1的置信度。
        """
        facade = await self._memory_facade(event)
        proposal = {"kind": kind, "subject": subject, "value": value, "evidence_ids": list(evidence_ids)[:16], "confidence": confidence}
        result = await facade.propose_memory_update(proposal)
        return (compact_json(_public_value(result), 8000))

    @filter.llm_tool(name="astrbot_runtime_inventory")
    async def runtime_inventory_tool(self, event: AstrMessageEvent):
        """读取当前AstrBot版本、平台、已装插件和本轮实际可用工具。"""
        manifest = await build_runtime_manifest(self.context, _RequestView(self.context), event=event)
        return (compact_json(manifest, 16000))

    @filter.llm_tool(name="astrbot_market_search")
    async def market_search_tool(self, event: AstrMessageEvent, query: str):
        """只读搜索AstrBot官方插件市场。

        Args:
            query(string): 插件名、作者或功能关键词。
        """
        if not self.dashboard:
            raise RuntimeError("Dashboard未配置")
        result = await self.dashboard.search_market(query)
        return (compact_json(result[:20], 14000))



    @filter.llm_tool(name="astrbot_manage_plugin")
    async def manage_plugin_tool(self, event: AstrMessageEvent, action: str, plugin_id: str):
        """执行当前管理员消息明确授权的插件管理动作。

        Args:
            action(string): install、update、enable、disable或reload。
            plugin_id(string): 官方market_plugin_id或已安装插件ID。
        """
        if not self.agent_tools:
            raise RuntimeError("管理服务未配置")
        try:
            result = await self.agent_tools.manage_plugin(event, action, plugin_id)
        except AuthorizationError as error:
            return ("拒绝执行：" + error.authorization.reason)
            return
        return (compact_json(result, 12000) + "\n新能力将在下一轮Agent自然生效。")

    @filter.llm_tool(name="astrbot_skill_list")
    async def skill_list_tool(self, event: AstrMessageEvent):
        """只读列出AstrBot本地和插件Skills。"""
        if not self.dashboard:
            raise RuntimeError("Dashboard未配置")
        return (compact_json(await self.dashboard.list_skills(), 14000))

    @filter.llm_tool(name="astrbot_manage_skill")
    async def manage_skill_tool(
        self, event: AstrMessageEvent, action: str, skill_name: str, skill_md: str = ""
    ):
        """执行当前管理员消息明确授权的本地Skill管理动作。

        Args:
            action(string): create、update、enable或disable。
            skill_name(string): 小写kebab-case Skill名称。
            skill_md(string): create/update时完整SKILL.md，其余动作留空。
        """
        if not self.agent_tools:
            raise RuntimeError("管理服务未配置")
        try:
            result = await self.agent_tools.manage_skill(
                event, action, skill_name, skill_md or None
            )
        except (AuthorizationError, SkillValidationError) as error:
            reason = getattr(getattr(error, "authorization", None), "reason", str(error))
            return ("拒绝执行：" + reason)
            return
        return (compact_json(result, 12000) + "\n新Skill将在下一轮Agent自然生效。")

    def _require_qq(self, event: Any = None) -> None:
        if not self.settings.enable_qq_tools or self.gateway is None or self.qq is None:
            raise RuntimeError("QQ工具未启用或未就绪")
        if event is not None:
            self._ensure_gateway(event)

    def _require_qzone(self, event: Any = None) -> None:
        if not self.settings.enable_qzone or self.qzone is None or self.gateway is None:
            raise RuntimeError("Qzone工具未启用")
        if event is not None:
            self._ensure_gateway(event)

    @filter.llm_tool(name="qq_friend_list")
    async def qq_friend_list_tool(self, event: AstrMessageEvent):
        """列出Bot的好友列表。"""
        self._require_qq(event)
        return (compact_json(await self.qq.friend_list(), 12000))

    @filter.llm_tool(name="qq_group_list")
    async def qq_group_list_tool(self, event: AstrMessageEvent):
        """列出Bot加入的群列表。"""
        self._require_qq(event)
        return (compact_json(await self.qq.group_list(), 12000))

    @filter.llm_tool(name="qq_group_members")
    async def qq_group_members_tool(self, event: AstrMessageEvent, group_id: str):
        """查看某个群的成员列表。

        Args:
            group_id(string): 群号。
        """
        self._require_qq(event)
        return (compact_json(await self.qq.group_members(group_id), 14000))

    @filter.llm_tool(name="qq_stranger_info")
    async def qq_stranger_info_tool(self, event: AstrMessageEvent, user_id: str):
        """查询某个QQ用户的资料。

        Args:
            user_id(string): 对方QQ号。
        """
        self._require_qq(event)
        return (compact_json(await self.qq.stranger_info(user_id), 8000))

    @filter.llm_tool(name="qq_recent_history")
    async def qq_recent_history_tool(self, event: AstrMessageEvent, group_id: str = "", count: int = 20):
        """从NapCat读取群最近聊天记录（与本地完整存储互补）。

        Args:
            group_id(string): 群号**或群名**（如"数学指令讨论群"），留空使用当前群。
            count(number): 条数，最大60。
        """
        self._require_qq(event)
        target = str(group_id or "").strip()
        if target and not target.isdigit():
            resolved, key, hint = await self._resolve_scope_ref(target)
            if not key:
                return f"没能定位这个群：{hint}"
            target = key
        target = target or event.get_group_id()
        return (
            compact_json(await self.qq.group_history(target, min(max(int(count), 1), 60)), 14000))

    @filter.llm_tool(name="qq_send_group")
    async def qq_send_group_tool(self, event: AstrMessageEvent, group_id: str, text: str):
        """主动向某个群发送一条文本消息。

        Args:
            group_id(string): 群号。
            text(string): 消息文本，最长1200字。
        """
        self._require_qq(event)
        current_group = str(event.get_group_id() or "")
        if current_group:
            group_id = current_group
        return (compact_json(await self.qq.send_group(
            group_id, [{"type": "text", "data": {"text": _bubble_text(text)}}]), 4000))

    @filter.llm_tool(name="qq_send_private")
    async def qq_send_private_tool(self, event: AstrMessageEvent, user_id: str, text: str):
        """主动私信某个好友。

        Args:
            user_id(string): 对方QQ号。
            text(string): 消息文本，最长1200字。
        """
        self._require_qq(event)
        return (compact_json(await self.qq.send_private(
            user_id, [{"type": "text", "data": {"text": _bubble_text(text)}}]), 4000))

    @filter.llm_tool(name="qq_handle_friend_request")
    async def qq_friend_request_tool(self, event: AstrMessageEvent, flag: str, approve: bool = True, remark: str = ""):
        """处理收到的好友申请（LLM自主决定是否同意）。

        Args:
            flag(string): 好友申请的flag，可从通知中获取。
            approve(bool): 是否同意。
            remark(string): 备注名，可留空。
        """
        self._require_qq(event)
        return (compact_json(
            await self.qq.handle_friend_request(flag, bool(approve), remark), 4000))

    @filter.llm_tool(name="qq_handle_group_invite")
    async def qq_group_invite_tool(self, event: AstrMessageEvent, flag: str,
                                   approve: bool = True, reason: str = "",
                                   sub_type: str = ""):
        """处理群相关申请（LLM自主决定是否接受）：邀请你进群、或别人申请进你的群。

        Args:
            flag(string): 申请/邀请的 flag（事件文本里有）。
            approve(bool): 是否接受。
            reason(string): 拒绝理由，可留空。
            sub_type(string): **必须与事件文本里的一致**：invite=邀请你进群，
                add=别人申请进你的群（文本里写着 sub_type=...，照抄；抄错申请会一直挂着）。
        """
        self._require_qq(event)
        return (compact_json(
            await self.qq.handle_group_invite(
                flag, bool(approve), reason, sub_type=sub_type or "invite"), 4000))

    @filter.llm_tool(name="qq_delete_friend")
    async def qq_delete_friend_tool(self, event: AstrMessageEvent, user_id: str):
        """删除一个好友（谨慎使用，由LLM自主决定）。

        Args:
            user_id(string): 对方QQ号。
        """
        self._require_qq(event)
        return (compact_json(await self.qq.delete_friend(user_id), 4000))

    @filter.llm_tool(name="qq_leave_group")
    async def qq_leave_group_tool(self, event: AstrMessageEvent, group_id: str):
        """退出某个群（谨慎使用，由LLM自主决定）。

        Args:
            group_id(string): 群号。
        """
        self._require_qq(event)
        return (compact_json(await self.qq.leave_group(group_id), 4000))

    @filter.llm_tool(name="qq_set_group_card")
    async def qq_group_card_tool(self, event: AstrMessageEvent, group_id: str, user_id: str, card: str):
        """设置群成员的群名片。

        Args:
            group_id(string): 群号。
            user_id(string): 成员QQ号。
            card(string): 名片内容。
        """
        self._require_qq(event)
        return (compact_json(await self.qq.set_group_card(group_id, user_id, card), 4000))

    @filter.llm_tool(name="qq_set_group_name")
    async def qq_group_name_tool(self, event: AstrMessageEvent, group_id: str, group_name: str):
        """修改群名称（需要Bot是群管理员）。

        Args:
            group_id(string): 群号。
            group_name(string): 新群名。
        """
        self._require_qq(event)
        return (compact_json(await self.qq.set_group_name(group_id, group_name), 4000))

    @filter.llm_tool(name="qq_set_special_title")
    async def qq_special_title_tool(self, event: AstrMessageEvent, group_id: str, user_id: str, title: str):
        """给成员设置专属头衔（需要Bot是群主）。

        Args:
            group_id(string): 群号。
            user_id(string): 成员QQ号。
            title(string): 头衔。
        """
        self._require_qq(event)
        return (compact_json(await self.qq.set_special_title(group_id, user_id, title), 4000))

    @filter.llm_tool(name="qq_set_profile")
    async def qq_profile_tool(self, event: AstrMessageEvent, nickname: str = "", longnick: str = ""):
        """修改Bot自己的昵称和签名。

        Args:
            nickname(string): 新昵称，留空不修改。
            longnick(string): 新签名，留空不修改。
        """
        self._require_qq(event)
        return (compact_json(
            await self.qq.set_profile(nickname=nickname or None, longnick=longnick or None), 4000))

    @filter.llm_tool(name="qq_set_status")
    async def qq_status_tool(self, event: AstrMessageEvent, status: int = 0):
        """设置Bot在线状态。

        Args:
            status(number): 在线状态代码。
        """
        self._require_qq(event)
        try:
            status_value = int(str(status).strip() or 0)
        except (TypeError, ValueError):
            status_value = 0
        return (compact_json(await self.qq.set_online_status(status_value), 4000))

    @filter.llm_tool(name="qq_set_avatar")
    async def qq_avatar_tool(self, event: AstrMessageEvent, media_id: str):
        """用归档媒体库中的图片设置Bot头像。

        Args:
            media_id(string): 媒体归档中的图片ID（用media_recent查询）。
        """
        self._require_qq(event)
        if not self.media:
            raise RuntimeError("媒体归档未启用")
        path = await self.media.get_path(media_id)
        return (compact_json(await self.qq.set_avatar("file://" + path), 4000))

    @filter.llm_tool(name="qq_recall")
    async def qq_recall_tool(self, event: AstrMessageEvent, message_id: str):
        """撤回Bot发出的一条消息。

        Args:
            message_id(string): 消息ID。
        """
        self._require_qq(event)
        return (compact_json(await self.qq.recall(message_id), 4000))

    @filter.llm_tool(name="qq_poke")
    async def qq_poke_tool(self, event: AstrMessageEvent, user_id: str, group_id: str = ""):
        """戳一戳某个用户。

        Args:
            user_id(string): 对方QQ号。
            group_id(string): 群号，留空为私聊戳。
        """
        self._require_qq(event)
        return (compact_json(await self.qq.poke(user_id, group_id or None), 4000))

    @filter.llm_tool(name="napcat_catalog")
    async def napcat_catalog_tool(self, event: AstrMessageEvent, category: str = "",
                                  action: str = ""):
        """查阅 QQ 动作表（193 个 OneBot/SnowLuma 动作，含中文说明与参数 schema）。
        两种用法：给 category 列出一类动作；给 action 查这一个动作的完整参数说明。
        拿不准参数名时先查这里，再 napcat_call。

        Args:
            category(string): 可选分类：信息/消息/好友/群信息/群管理/群文件/请求/
                扩展/群相册/空间/系统表情/流式接口。留空列出全部分类与数量。
            action(string): 可选，查单个动作的参数表与返回说明（如 get_forward_msg）。
        """
        self._require_qq(event)
        wanted = str(action or "").strip()
        if wanted:
            try:
                spec = self.gateway.resolve_action(wanted)
            except QQGatewayError as error:
                return str(error)
            return compact_json({
                "action": spec.name,
                "category": spec.category,
                "read_only": spec.read_only,
                "risk": spec.risk,
                "summary": spec.summary,
                "params": [
                    {"name": p.name, "type": p.type, "required": p.required,
                     "desc": p.desc,
                     **({"default": p.default} if p.default is not None else {})}
                    for p in spec.params
                ],
                **({"aliases": list(spec.aliases)} if spec.aliases else {}),
                **({"returns": spec.returns} if spec.returns else {}),
            }, 6000)
        category = str(category or "").strip()
        if not category:
            return compact_json({
                "categories": self.gateway.categories(),
                "hint": "给 category 列出一类动作，或给 action 查单个动作的参数表",
            }, 4000)
        items = self.gateway.catalog(category)
        if not items:
            return (f"没有分类 {category!r}；可用分类："
                    + "、".join(str(c["category"]) for c in self.gateway.categories()))
        return compact_json({"category": category, "actions": items}, 16000)

    @filter.llm_tool(name="napcat_call")
    async def napcat_call_tool(self, event: AstrMessageEvent, action: str = "",
                               params_json: str = "{}"):
        """调用任意 QQ 动作（OneBot/SnowLuma 全量 193 个，先用 napcat_catalog 查参数表）。
        参数名必须用动作表里的名字；数字/布尔写成字符串也会被自动转换。

        Args:
            action(string): 动作名，如 get_group_info、send_group_msg、send_poke。
            params_json(string): 参数对象 JSON，如 {"group_id": 123, "message": [...]}。
        """
        self._require_qq(event)
        action = str(action or "").strip()
        if not action:
            return ("需要 action（动作名）。先调 napcat_catalog 查动作与参数："
                    "给 category 列一类，给 action 查单个。")
        params = parse_json_value(params_json or "{}")
        if params is None:
            return (f"params_json 无法解析为 JSON（原文 {params_json[:80]!r}）。"
                    "请用双引号重新构造参数 JSON 再调用一次。")
        if not isinstance(params, dict):
            params = {"params": params}
        facade = GatewayFacade(self.gateway)
        try:
            result = await facade.call(action, **params)
        except QQGatewayError as error:
            return f"动作失败：{error}"
        return (compact_json(result, 12000))

    @filter.llm_tool(name="group_topics")
    async def group_topics_tool(self, event: AstrMessageEvent, group_id: str = "", limit: int = 10):
        """查看某个群最近记录的讨论话题（按新到旧）。

        Args:
            group_id(string): 群号，留空为当前群。
            limit(number): 条数，最大30。
        """
        if not self.storage:
            raise RuntimeError("存储未就绪")
        if group_id:
            scope = await self.storage.resolve_scope(
                "aiocqhttp", str(event.get_self_id() or ""), str(group_id)
            )
        else:
            scope = await self._scope_for_event(event, create=True)
        if not scope:
            return compact_json([])
        return compact_json(await self.storage.recent_topics(scope, min(max(int(limit), 1), 30)), 8000)

    @filter.llm_tool(name="look_at_image")
    async def look_at_image_tool(self, event: AstrMessageEvent, media_id: str, question: str = ""):
        """查看一张归档图片（用视觉模型描述；media_id 从 media_recent/截图工具获取）。

        截图后**先用它自检**：截的是不是人机验证/滑块/CAPTCHA？定位滑块/按钮时可带 question
        问具体坐标，比如"这是不是验证页？滑块手柄中心大概在什么像素坐标、要往右拖到哪（图约1280×860）"。

        Args:
            media_id(string): 媒体归档ID。
            question(string): 可选，要问视觉模型的具体问题（如判断是否验证页、估算滑块坐标）。
        """
        if not self.media:
            raise RuntimeError("媒体归档未启用")
        mime, b64 = await self.media.get_base64(media_id)
        ask = str(question or "").strip()
        prompt = (f"{ask}\n只根据图片作答，简明直接。" if ask else None)
        description = await self._describe_images([(mime, b64)], prompt=prompt)
        if not description:
            raise RuntimeError("图片识别失败（未配置可用的视觉模型？）")
        return description

    @filter.llm_tool(name="read_document")
    async def read_document_tool(
        self, event: AstrMessageEvent, media_id: str, max_chars: int = 12000,
    ):
        """读取归档文档的文字内容：PDF（含逐页）、txt/md/代码/json 等。
        用户发来的文件会进媒体归档（media_recent 可见），先用这个读全文再处理。

        Args:
            media_id(string): 媒体归档ID。
            max_chars(number): 返回文本上限字符数，默认12000。
        """
        if not self.media:
            raise RuntimeError("媒体归档未启用")
        try:
            path = await self.media.get_path(str(media_id or "").strip())
        except Exception as error:
            return f"取文件失败：{str(error)[:140]}"
        from .src.pdf_reader import extract_document_text

        result = await asyncio.to_thread(
            extract_document_text, path,
            max_chars=min(max(int(max_chars or 12000), 500), 30000))
        if result.get("kind") in {"missing", "error", "unsupported"}:
            return f"读取失败：{result.get('note')}"
        return compact_json({
            "kind": result.get("kind"),
            "pages": result.get("pages"),
            "truncated": result.get("truncated"),
            "note": result.get("note") or None,
            "text": result.get("text", ""),
        }, int(max_chars) + 800)

    # ------------------------------------------------------- python 沙箱（bot RPC）
    def _sandbox_globals(
        self, event: AstrMessageEvent | None, scope_id: str,
    ) -> dict[str, Any]:
        """python_exec 的沙箱命名空间：注入 bot 帮助对象（hermes code_execution_rpc /
        openclaw code-mode 的轻量移植）——一段脚本就能跑完"搜索→整理→发送"多步流水线，
        不用一轮轮来回调工具。全部同步接口，内部把协程送回主事件循环执行。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        def _call(coro: Any, timeout: float = 25.0) -> Any:
            if loop is None or not loop.is_running():
                raise RuntimeError("宿主事件循环不可用")
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

        async def _do_web_search(query: str, limit: int) -> Any:
            from .src.web_tools import search_web

            return await search_web(str(query), limit=max(1, min(int(limit), 10)))

        async def _do_fetch(url: str, max_chars: int) -> str:
            from .src.web_tools import fetch_text

            data = await fetch_text(
                str(url), max_bytes=self.settings.web_fetch_max_mb * 1024 * 1024)
            text = ""
            if isinstance(data, Mapping):
                text = str(data.get("text") or data.get("body") or "")
            else:
                text = str(data)
            return text[:max(200, min(int(max_chars), 8000))]

        async def _do_memory_search(query: str, limit: int) -> Any:
            if self.retrieval is None:
                raise RuntimeError("检索未就绪")
            hits = await self.retrieval.search(
                self._shared_scope_ids(scope_id or ""), str(query),
                max(1, min(int(limit), 20)))
            return [
                {"sender": h.sender_name, "when": timeutil.to_text(h.occurred_at),
                 "snippet": h.snippet[:200]}
                for h in hits
            ]

        async def _do_recent(limit: int) -> Any:
            if self.storage is None or not scope_id:
                raise RuntimeError("记忆未就绪")
            rows = await self.storage.recent_messages(
                scope_id, max(1, min(int(limit), 50)))
            return [
                {"sender": m.sender_name, "text": str(m.text or "")[:200]}
                for m in rows
            ]

        async def _do_send_group(group_id: str, text: str) -> str:
            self._bind_gateway_client()
            group = str(group_id or "").strip()
            if not group or not self.settings.allows_group(group):
                raise RuntimeError(f"群 {group} 不在白名单，拒绝发送")
            if self.gateway is None:
                raise RuntimeError("QQ 网关未就绪")
            await self.gateway.execute(
                "send_group_msg", group_id=int(group),
                message=[{"type": "text", "data": {"text": _bubble_text(text)}}])
            return "sent"

        async def _do_send_private(user_id: str, text: str) -> str:
            self._bind_gateway_client()
            user = str(user_id or "").strip()
            if not user or self.gateway is None:
                raise RuntimeError("目标或网关不可用")
            await self.gateway.execute(
                "send_private_msg", user_id=int(user),
                message=[{"type": "text", "data": {"text": _bubble_text(text)}}])
            return "sent"

        async def _do_say(text: str) -> str:
            if event is None:
                raise RuntimeError("当前任务没有会话上下文，改用 send_group（需群号）")
            await self._send_segment(event, {"action": "text", "text": _bubble_text(text)})
            return "sent"

        class _BotSandbox:
            """沙箱脚本用的同步便捷接口；调用失败不抛异常，返回 'ERROR: ...' 字符串。"""

            @staticmethod
            def _wrap(func: Any) -> Any:
                try:
                    return func()
                except Exception as error:
                    return f"ERROR: {type(error).__name__}: {str(error)[:160]}"

            def web_search(self, query: str, limit: int = 5):
                return self._wrap(lambda: compact_json(
                    _call(_do_web_search(query, limit)), 4000))

            def fetch(self, url: str, max_chars: int = 4000):
                return self._wrap(lambda: _call(_do_fetch(url, max_chars)))

            def kb_search(self, query: str, limit: int = 5):
                return self._wrap(lambda: _call(self._kb_search_data(
                    str(query), "", max(1, min(int(limit), 10)))))

            def search_memory(self, query: str, limit: int = 8):
                return self._wrap(lambda: compact_json(
                    _call(_do_memory_search(query, limit)), 6000))

            def recent_messages(self, limit: int = 20):
                return self._wrap(lambda: compact_json(
                    _call(_do_recent(limit)), 6000))

            def send_group(self, group_id: str, text: str):
                return self._wrap(lambda: _call(_do_send_group(group_id, text)))

            def send_private(self, user_id: str, text: str):
                return self._wrap(lambda: _call(_do_send_private(user_id, text)))

            def say(self, text: str):
                return self._wrap(lambda: _call(_do_say(text)))

            def now(self) -> str:
                return timeutil.text("%Y-%m-%d %H:%M %A")

            def sleep(self, seconds: float) -> None:
                time.sleep(min(max(float(seconds), 0.0), 20.0))

        return {"bot": _BotSandbox()}

    async def _run_python_sandbox(
        self, code: str, event: AstrMessageEvent | None, scope_id: str,
    ) -> str:
        import contextlib
        import io as _io

        from .src import data_tools

        sandbox = self._sandbox_globals(event, scope_id)
        # 代码里出现 matplotlib 就补齐画图环境：Agg 后端 + 中文字体 + save_chart
        # （read_tabular 同款；模型说"本机 matplotlib 直接画"时走的就是这条路）
        if any(token in code for token in ("matplotlib", "pyplot", "plt.", "save_chart")):
            plot_ok, plot_note = await asyncio.to_thread(data_tools.ensure_matplotlib)
            if not plot_ok:
                return plot_note
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            matplotlib.rcParams["axes.unicode_minus"] = False
            await asyncio.to_thread(data_tools._apply_cjk_font, matplotlib)
            sandbox["matplotlib"] = matplotlib
            sandbox["plt"] = plt
            try:
                sandbox["save_chart"] = data_tools._make_save_chart(
                    plt, self._workspace().resolve)
            except Exception:
                pass

        def _run() -> str:
            buffer = _io.StringIO()
            globals_ns: dict[str, Any] = {"__name__": "sandbox", **sandbox}
            try:
                with contextlib.redirect_stdout(buffer):
                    exec(compile(code, "<python_exec>", "exec"), globals_ns)  # noqa: S102
                return buffer.getvalue() or "（无输出）"
            except Exception as error:
                return f"执行出错：{type(error).__name__}: {error}\n部分输出：{buffer.getvalue()[:1500]}"

        try:
            async with asyncio.timeout(30):
                output = await asyncio.to_thread(_run)
        except TimeoutError:
            return "执行超时（30秒）"
        return output[:4000]

    @filter.llm_tool(name="say_now")
    async def say_now_tool(self, event: AstrMessageEvent, text: str):
        """任务执行途中，立刻给【当前对话】发一条简短消息（进度/让对方稍等等）。

        这条是即时发出的，和你最终的正常回复相互独立、条数互不影响；可按需发 0 到多条。
        【克制】默认别播报：没人要求就保持安静，最终结果用正常回复给。只有任务较长、
        用户明确要进度、或需要让对方等一下时，才发一两条很短的（如"稍等 我去过下验证"）。
        若过程里已经把要说的都说完了，最终回复可以用 noop。

        Args:
            text(string): 要立刻发出的一句短话（一个气泡，别换行、别长段）。
        """
        bubble = _bubble_text(text)
        if not bubble:
            return "空消息，未发送"
        try:
            await self._send_segment(event, {"action": "text", "text": bubble})
        except Exception as error:
            return f"发送失败：{str(error)[:120]}"
        return "已发送一条过程消息"

    @filter.llm_tool(name="ask_user")
    async def ask_user_tool(self, event: AstrMessageEvent, question: str):
        """对方的要求模糊、缺关键信息、或动手前需要确认时，向对方发一句短的澄清/确认问题，
        然后本轮先停下等TA回答（比硬猜着做、或一句不问更好）。

        Args:
            question(string): 一句口语化的短问题（一个气泡，别换行）。
        """
        bubble = _bubble_text(question)
        if not bubble:
            return "空问题，未发送"
        try:
            await self._send_segment(event, {"action": "text", "text": bubble})
        except Exception as error:
            return f"发送失败：{str(error)[:120]}"
        return "问题已发出，等待对方回复——本轮任务先到这里，不要再继续后面的步骤。"

    @filter.llm_tool(name="python_exec")
    async def python_exec_tool(self, event: AstrMessageEvent, code: str):
        """执行一段Python代码（本机运行，30秒超时）。沙箱里有现成的 bot 对象可一次跑完多步流水线
        （同步调用，失败返回 'ERROR: ...' 字符串）：bot.web_search(query) 搜网页、
        bot.fetch(url) 抓正文、bot.kb_search(q) 查知识库、bot.search_memory(q) 搜你的记忆、
        bot.recent_messages(n) 看本会话近况、bot.send_group(gid,text)/bot.send_private(uid,text)
        发消息（群必须在白名单）、bot.say(text) 给当前对话发一条、bot.now()、bot.sleep(秒)。
        适合"搜一批资料→筛选→汇总发出"这类批量/流水线活。
        画数据图也行：代码里用到 plt 时会自动备好 matplotlib（Agg 后端+中文字体），
        存图调 save_chart("图.png")（存进工作区并返回路径，print 出来即可看见），
        再用 send_local_file 把图发出去。

        Args:
            code(string): 完整的Python代码。
        """
        scope = ""
        try:
            scope = await self._scope_for_event(event) or ""
        except Exception:
            scope = ""
        return await self._run_python_sandbox(code, event, scope)

    @filter.llm_tool(name="send_image")
    async def send_image_tool(
        self, event: AstrMessageEvent, media_id: str, group_id: str = "", user_id: str = ""
    ):
        """把媒体归档里的一张图片发送到群或私聊（二选一填写目标）。

        Args:
            media_id(string): 媒体归档ID（media_recent可查）。
            group_id(string): 目标群号，私聊则留空。
            user_id(string): 目标QQ号，发群则留空。
        """
        self._require_qq(event)
        # 默认发到当前会话：群=本群，私聊=对方QQ号（实录：模型只传 media_id
        # 时报 "group_id 和 user_id 至少填一个"，图就发不出去）
        current_group = str(event.get_group_id() or "")
        if current_group:
            group_id = group_id or current_group
        else:
            user_id = user_id or str(event.get_sender_id() or "")
        if not self.media:
            raise RuntimeError("媒体归档未启用")
        path = await self.media.get_path(media_id)
        # QQ 与 NapCat 可能不同机：file:// 本地路径读不到，用 base64 段
        data = await asyncio.to_thread(Path(path).read_bytes)
        segment = {"type": "image",
                   "data": {"file": "base://" + base64.b64encode(data).decode("ascii")}}
        if group_id:
            result = await self.gateway.execute("send_group_msg", group_id=int(group_id),
                                                message=[segment])
        elif user_id:
            result = await self.gateway.execute("send_private_msg", user_id=int(user_id),
                                                message=[segment])
        else:
            raise ValueError("group_id 和 user_id 至少填一个")
        return compact_json({"status": result.get("status") if isinstance(result, dict) else "ok",
                             "target": group_id or user_id}, 1000)

    @filter.llm_tool(name="get_user_profile")
    async def get_user_profile_tool(self, event: AstrMessageEvent, user_id: str):
        """查看你对某个人的完整印象：好感度+印象描述+特点标签。

        Args:
            user_id(string): 对方QQ号。
        """
        if not self.storage:
            raise RuntimeError("记忆系统未就绪")
        scope = await self._scope_for_event(event)
        if not scope:
            return "当前会话未启用记忆"
        affinity = await self.storage.get_affinity(scope, user_id)
        impression = await self.storage.get_impression_any(
            self._shared_scope_ids(scope), str(user_id))
        return compact_json({"user_id": user_id, "affinity": affinity,
                             "impression": impression}, 2000)

    @filter.llm_tool(name="update_impression")
    async def update_impression_tool(
        self, event: AstrMessageEvent, user_id: str, impression: str, tags: str = ""
    ):
        """更新你对某个人的印象和特点标签（像真人记住一个人的感觉）。

        Args:
            user_id(string): 对方QQ号。
            impression(string): 印象描述，一两句话（例如"话痨但讲义气，爱聊游戏"）。
            tags(string): 特点标签，逗号分隔，最多8个（例如"话痨,游戏,夜猫子"）。
        """
        if not self.storage:
            raise RuntimeError("记忆系统未就绪")
        scope = await self._scope_for_event(event)
        if not scope:
            return "当前会话未启用记忆"
        tag_list = [t.strip() for t in str(tags or "").replace("，", ",").split(",") if t.strip()][:8]
        result = await self.storage.upsert_impression(
            scope, user_id, impression=impression or None,
            tags=tag_list or None,
        )
        return compact_json(result, 1200)

    @filter.llm_tool(name="list_known_users")
    async def list_known_users_tool(
        self, event: AstrMessageEvent, query: str = "", limit: int = 20
    ):
        """列出你认识的人（含印象与标签），可按关键词筛选。

        Args:
            query(string): 可选关键词（昵称/印象内容）。
            limit(number): 最多返回多少条。
        """
        if not self.storage:
            raise RuntimeError("记忆系统未就绪")
        scope = await self._scope_for_event(event)
        if not scope:
            return "当前会话未启用记忆"
        people = await self.storage.list_impressions(
            self._shared_scope_ids(scope), min(max(int(limit or 20), 1), 40),
            query=query or None,
        )
        return compact_json(people, 5000)

    @filter.llm_tool(name="list_stickers")
    async def list_stickers_tool(self, event: AstrMessageEvent, limit: int = 20):
        """浏览表情包库存（含每张的含义描述，按使用频次排序）。

        Args:
            limit(number): 最多返回多少张。
        """
        if not self.stickers:
            return "表情包库未启用"
        entries = self.stickers.catalog(min(max(int(limit or 20), 1), 50))
        if not entries:
            return "表情包库还是空的"
        return compact_json(entries, 5000)

    @filter.llm_tool(name="pick_sticker")
    async def pick_sticker_tool(self, event: AstrMessageEvent, query: str, limit: int = 5):
        """按含义/情绪关键词挑几张合适的表情包（返回 sticker_id 供发送用）。

        Args:
            query(string): 想要的表情含义关键词，如"开心""无语""狗头""赞"。
            limit(number): 最多返回几张候选。
        """
        if not self.stickers:
            return "表情包库未启用"
        entries = self.stickers.pick(query, min(max(int(limit or 5), 1), 10))
        if not entries:
            return "没有匹配的表情"
        return compact_json(entries, 3000)

    async def _send_sticker_action(self, args: dict[str, Any]) -> str:
        """往群里发一张库存表情包（主动发/被要求发都走这里）。

        实录：提示词一直承诺有 send_sticker，但它只挂在内部动作分发上、**没有注册成工具**，
        模型在主循环里按名字调用不到 → 跑去别家插件的 run_wyc_tool 包装器里试，白烧一轮
        （日志：无效的工具名称或工具未启用: send_sticker）。现在补成真工具，两处共用这一段。
        """
        if self.stickers is None:
            return "表情包库未启用"
        payload = args if isinstance(args, dict) else {}
        sticker = self.stickers.resolve(str(payload.get("sticker_id", "")))
        if sticker is None:
            picked = self.stickers.pick(str(payload.get("query", "")), 1)
            sticker = self.stickers.resolve(
                str(picked[0]["sticker_id"])) if picked else None
        if sticker is None:
            return "没有可发送的表情包（可用 list_stickers 翻库存）"
        target_group = str(payload.get("group_id", "")).strip()
        if target_group and not self.settings.allows_group(target_group):
            return f"拒绝发送：群 {target_group} 不在白名单"
        if not target_group:
            active = self._most_active_group()
            target_group = active[1] if active else ""
        if not target_group:
            return "没有可发送的活跃群"
        if self.gateway is None:
            return "QQ 网关未就绪"
        self._bind_gateway_client()
        await self.gateway.execute(
            "send_group_msg", group_id=int(target_group),
            message=[{"type": "image",
                      "data": {"file": "file://" + self.stickers.send_path(sticker)}}],
        )
        try:
            await self.stickers.note(sticker.sha256, bump_use=True)
        except Exception:
            pass
        return f"已发表情包到群{target_group}"

    @filter.llm_tool(name="send_sticker")
    async def send_sticker_tool(
        self, event: AstrMessageEvent, sticker_id: str = "", query: str = "",
        group_id: str = "",
    ):
        """主动往群里发一张表情包（库存里的）。想接梗/吐槽/卖萌又不想只发文字时用它；
        在群里回话时也可以在计划里直接加 sticker 段，不必调这个工具。

        Args:
            sticker_id(string): 库存里的表情 id（pick_sticker/list_stickers 拿）；与 query 二选一。
            query(string): 没有 id 时按含义挑一张，如"无语""狗头""赞"。
            group_id(string): 发到哪个群（群号或群名）；留空=最近最活跃的白名单群。
        """
        resolved = str(group_id or "").strip()
        if resolved and not resolved.isdigit():
            scope_ref, key, hint = await self._resolve_scope_ref(resolved)
            if not key:
                return f"没能定位这个群：{hint}"
            resolved = key
        return await self._send_sticker_action({
            "sticker_id": sticker_id, "query": query, "group_id": resolved,
        })

    @filter.llm_tool(name="group_memory")
    async def group_memory_tool(
        self, event: AstrMessageEvent, group_id: str = "", level: int = 0,
        limit: int = 6,
    ):
        """深入看**某个会话**的分层记忆：L1（分钟级，刚聊的）/ L2（天级）/ L3（月级），
        外加该群话题与记忆账本。上下文里的 conversation_overview 只给一句话近况，
        想知道"那个群具体在聊什么"就用这个工具查。

        Args:
            group_id(string): 群号或群名（如"数学指令讨论群"）；留空=当前会话。
            level(number): 只看某一层：1/2/3；0=全部层。
            limit(number): 每层返回条数，最大 20。
        """
        scope_id = await self._scope_for_event(event) if event is not None else None
        if str(group_id or "").strip():
            resolved, _key, hint = await self._resolve_scope_ref(group_id)
            if not resolved:
                return f"没能定位这个会话：{hint}"
            scope_id = resolved
        if not scope_id or self.storage is None:
            return "记忆系统未就绪或会话未记录"
        levels = [level] if int(level or 0) in {1, 2, 3} else [1, 2, 3]
        rows = await self.storage.list_summaries(
            [scope_id], max(1, min(int(limit or 6), 20)), levels=levels)
        labels = await self._scope_labels()
        payload = {
            "origin": labels.get(scope_id, scope_id[:8]),
            "layers": {},
            "topics": [row.get("topic") for row in await self.storage.recent_topics(scope_id, 10)],
        }
        for row in rows:
            payload["layers"].setdefault(f"L{row.level}", []).append({
                "time": timeutil.to_text(row.created_at, "%m-%d %H:%M"),
                "title": row.title, "body": " ".join(str(row.body).split())[:400],
            })
        if self.ledger is not None:
            entries = await self.ledger.catalog([scope_id], 8)
            payload["memory_catalog"] = [
                {"kind": item.kind, "subject": item.subject,
                 "value": " ".join(str(item.value).split())[:160]}
                for item in entries
            ]
        if not payload["layers"] and not payload.get("memory_catalog"):
            return (f"{payload['origin']} 还没有可用的分层记忆（可能刚进群/刚被清空，"
                    "继续聊几句就会攒出来）")
        return compact_json(payload, 12000)

    @filter.llm_tool(name="get_affinity")
    async def get_affinity_tool(self, event: AstrMessageEvent, user_id: str = ""):
        """查看你对某个用户的好感度和情绪备注（留空则查当前说话的人）。

        Args:
            user_id(string): 对方QQ号，留空为当前用户。
        """
        if not self.storage:
            raise RuntimeError("存储未就绪")
        scope = await self._scope_for_event(event, create=True)
        target = str(user_id or event.get_sender_id())
        merged = await self.storage.get_affinity_any(
            self._shared_scope_ids(scope), target)
        return compact_json(merged, 2000)

    @filter.llm_tool(name="adjust_affinity")
    async def adjust_affinity_tool(
        self, event: AstrMessageEvent, user_id: str, delta: float, note: str = ""
    ):
        """调整你对某个用户的好感度（-100到100；聊得来就加，被惹毛了就减）。

        Args:
            user_id(string): 对方QQ号。
            delta(number): 好感度变化量，正加负减。
            note(string): 一句话原因，会记住。
        """
        if not self.storage:
            raise RuntimeError("存储未就绪")
        scope = await self._scope_for_event(event, create=True)
        state = await self.storage.adjust_affinity(
            scope, str(user_id), float(delta), note
        )
        return compact_json(state, 2000)

    @filter.llm_tool(name="recent_events")
    async def recent_events_tool(self, event: AstrMessageEvent, limit: int = 15):
        """查询最近的好友申请、群邀请、入群退群、撤回等QQ事件记录（含flag，可用于批准）。

        Args:
            limit(number): 条数，最大50。
        """
        if not self.storage:
            raise RuntimeError("存储未就绪")
        scopes = list(self._known_scopes.values())
        current = None
        try:
            current = await self._scope_for_event(event, create=True)
        except Exception:
            current = None
        if current and current not in scopes:
            scopes.insert(0, current)
        items = await self.storage.recent_events(scopes or [current] if current else scopes, min(max(int(limit), 1), 50))
        payload = [
            {
                "message_id": item.message_id,
                "event_text": item.text,
                "time": item.occurred_at,
                "origin": next((k for k, v in self._known_scopes.items() if v == item.scope_id), item.scope_id[:8]),
            }
            for item in items
        ]
        return compact_json(payload, 12000)

    @filter.llm_tool(name="qzone_list")
    async def qzone_list_tool(self, event: AstrMessageEvent, count: int = 10):
        """读取QQ空间说说列表。

        Args:
            count(number): 条数，最大20。
        """
        self._require_qzone(event)
        return (compact_json(await self.qzone.list_feeds(min(max(int(count), 1), 20)), 14000))

    @filter.llm_tool(name="qzone_publish")
    async def qzone_publish_tool(self, event: AstrMessageEvent, text: str):
        """以Bot自己的名义发布一条说说（LLM自主决定内容）。

        Args:
            text(string): 说说内容，1-1000字。
        """
        self._require_qzone(event)
        return (compact_json(await self.qzone.publish(text), 6000))

    @filter.llm_tool(name="qzone_like")
    async def qzone_like_tool(self, event: AstrMessageEvent, unikey: str, curkey: str, owner_uin: str):
        """给一条空间动态点赞。

        Args:
            unikey(string): 动态unikey。
            curkey(string): 动态curkey。
            owner_uin(string): 动态主人QQ号。
        """
        self._require_qzone(event)
        return (compact_json(await self.qzone.like(unikey, curkey, owner_uin), 6000))

    @filter.llm_tool(name="qzone_comment")
    async def qzone_comment_tool(self, event: AstrMessageEvent, topic_id: str, content: str):
        """评论一条空间动态。

        Args:
            topic_id(string): 动态ID。
            content(string): 评论内容，1-500字。
        """
        self._require_qzone(event)
        return (compact_json(await self.qzone.comment(topic_id, content), 6000))

    @filter.llm_tool(name="qzone_delete")
    async def qzone_delete_tool(self, event: AstrMessageEvent, topic_id: str):
        """删除Bot自己发布的一条说说。

        Args:
            topic_id(string): 动态ID。
        """
        self._require_qzone(event)
        return (compact_json(await self.qzone.delete(topic_id), 6000))

    def _browser_session(self) -> BrowserSession:
        """Lazy per-plugin browser session (tabs + cookies + cache)."""
        if self._browser is None:
            self._browser = BrowserSession(
                timeout=float(self.settings.web_fetch_max_mb and 20 or 20),
                max_bytes=self.settings.web_fetch_max_mb * 1024 * 1024,
            )
        return self._browser

    async def _browser_note(self, summary: str, scope: str | None = None) -> None:
        """Leave a queryable trace of what was browsed."""
        if not self.storage or not summary.strip():
            return

        target = scope or next(iter(dict.fromkeys(self._known_scopes.values())), None)
        if not target:
            return
        try:
            from .src.models import NormalizedMessage

            stem = f"web-note:{hash(summary) & 0xFFFFFFFF:08x}"
            stored = await self.ingest.ingest(NormalizedMessage(
                platform="aiocqhttp", account_id="",
                conversation_id="web", upstream_message_id=stem,
                sender_id="self", sender_name="self",
                text=summary[:800], occurred_at=datetime.now(timezone.utc).isoformat(),
                raw_event={"message_id": stem, "derived": True, "kind": "web"},
                parts=[], event_type="notice.web",
            ), f"web:{stem}")
            self._known_scopes.setdefault("web", stored.scope_id)
        except Exception as error:
            logger.info("长程记忆：浏览留痕跳过：%s", str(error)[:120])

    @filter.llm_tool(name="browse")
    async def browse_tool(self, event: AstrMessageEvent, url: str, tab_id: str = ""):
        """用内置浏览器打开一个网页，返回标题、正文、可点击链接与表单。

        Args:
            url(string): 要打开的 http/https 网址。
            tab_id(string): 可选，指定标签页；留空用当前标签页。
        """
        session = self._browser_session()
        wanted = int(tab_id) if str(tab_id).strip().isdigit() else None
        try:
            page = await session.fetch(url, tab_id=wanted)
        except BrowserError as error:
            return f"打开失败：{error}"
        snapshot = session.snapshot(wanted)
        if isinstance(snapshot, dict):
            snapshot["engine_hint"] = (
                "browse 是轻量HTTP引擎（不渲染）；本页面要截图或点击输入时，把同一网址"
                "传给 browser_dom(url=...) / browser_screenshot(url=...) 走真实浏览器内核"
            )
        self._spawn(self._browser_note(
            f"[浏览] {page.title or page.url}\n{page.text[:400]}"
        ))
        return compact_json(snapshot, self.settings.context_char_budget // 2)

    @filter.llm_tool(name="browser_links")
    async def browser_links_tool(self, event: AstrMessageEvent, limit: int = 30):
        """列出当前网页上可点击的链接（带序号，供 browser_click 使用）。

        Args:
            limit(number): 最多返回多少条。
        """
        snapshot = self._browser_session().snapshot(link_limit=int(limit or 30))
        if not snapshot.get("links"):
            return "当前页面没有链接，或还没有打开页面"
        return compact_json({
            "url": snapshot.get("url"), "title": snapshot.get("title"),
            "links": snapshot.get("links"),
        }, 8000)

    @filter.llm_tool(name="browser_click")
    async def browser_click_tool(self, event: AstrMessageEvent, index: int):
        """点击当前页面上第 N 号链接（序号来自 browser_links/browse）。

        Args:
            index(number): 链接序号。
        """
        session = self._browser_session()
        try:
            link = session.link(int(index))
            page = await session.fetch(link.href)
        except (BrowserError, ValueError) as error:
            return f"点击失败：{error}"
        self._spawn(self._browser_note(
            f"[浏览] 点击「{link.text}」→ {page.title or page.url}\n{page.text[:400]}"
        ))
        return compact_json(session.snapshot(), self.settings.context_char_budget // 2)

    @filter.llm_tool(name="browser_form")
    async def browser_form_tool(
        self, event: AstrMessageEvent, fields: str, index: int = 0
    ):
        """填写并提交当前页面上的表单（自动携带该会话的 cookie）。

        Args:
            fields(string): JSON 对象，形如 {"q": "搜索词"}，键为表单字段名。
            index(number): 表单序号，默认 0（第一个表单）。
        """
        session = self._browser_session()
        try:
            form = session.form(int(index))
        except BrowserError as error:
            return f"提交失败：{error}"
        try:
            payload = parse_json_object(fields) or {}
        except Exception:
            payload = {}
        if not isinstance(payload, Mapping) or not payload:
            return "需要 fields，例如 {\"q\": \"搜索词\"}"
        values = {str(k): str(v) for k, v in payload.items()}
        method = "POST" if form.method == "post" else "GET"
        try:
            page = await session.fetch(form.action or session.snapshot()["url"],
                                       method=method, payload=values)
        except BrowserError as error:
            return f"提交失败：{error}"
        self._spawn(self._browser_note(
            f"[浏览] 提交表单 {values} → {page.title or page.url}\n{page.text[:400]}"
        ))
        return compact_json(session.snapshot(), self.settings.context_char_budget // 2)

    @filter.llm_tool(name="browser_back")
    async def browser_back_tool(self, event: AstrMessageEvent):
        """返回浏览历史里的上一页。"""
        session = self._browser_session()
        try:
            url = session.back()
            await session.fetch(url, remember=False)
        except BrowserError as error:
            return f"返回失败：{error}"
        return compact_json(session.snapshot(), self.settings.context_char_budget // 2)

    @filter.llm_tool(name="browser_forward")
    async def browser_forward_tool(self, event: AstrMessageEvent):
        """前进到浏览历史里的下一页。"""
        session = self._browser_session()
        try:
            url = session.forward()
            await session.fetch(url, remember=False)
        except BrowserError as error:
            return f"前进失败：{error}"
        return compact_json(session.snapshot(), self.settings.context_char_budget // 2)

    @filter.llm_tool(name="browser_tabs")
    async def browser_tabs_tool(self, event: AstrMessageEvent):
        """查看所有标签页及其网址（像真人开多个页面）。"""
        return compact_json(self._browser_session().tabs(), 4000)

    @filter.llm_tool(name="browser_new_tab")
    async def browser_new_tab_tool(self, event: AstrMessageEvent, url: str = ""):
        """新开一个标签页，可顺带打开一个网址。

        Args:
            url(string): 可选网址。
        """
        session = self._browser_session()
        tab = session.new_tab()
        session.select_tab(tab.tab_id)
        if str(url).strip():
            try:
                await session.fetch(url, tab_id=tab.tab_id)
            except BrowserError as error:
                return f"新标签页已开，但打开失败：{error}"
        return compact_json(session.tabs(), 4000)

    @filter.llm_tool(name="browser_switch_tab")
    async def browser_switch_tab_tool(self, event: AstrMessageEvent, tab_id: int):
        """切换到某个标签页。

        Args:
            tab_id(number): 标签页编号（browser_tabs 可查）。
        """
        try:
            return compact_json(self._browser_session().select_tab(int(tab_id)), 4000)
        except BrowserError as error:
            return str(error)

    @filter.llm_tool(name="browser_close_tab")
    async def browser_close_tab_tool(self, event: AstrMessageEvent, tab_id: int):
        """关闭某个标签页。

        Args:
            tab_id(number): 标签页编号。
        """
        return compact_json(self._browser_session().close_tab(int(tab_id)), 4000)

    def _browser_find_text(self, keyword: str, limit: int = 5):
        """Page-text keyword search shared by the llm_tool and the scheduler."""
        section = self._browser_session().snapshot(text_limit=200000)
        text = str(section.get("text", ""))
        word = str(keyword or "").strip()
        if not word:
            return "需要 keyword"
        found: list[str] = []
        start = 0
        while len(found) < max(1, min(int(limit or 5), 10)):
            index = text.find(word, start)
            if index < 0:
                break
            found.append(text[max(0, index - 120):index + 180].strip())
            start = index + len(word)
        if not found:
            return f"页面里没有找到「{word}」"
        return compact_json({"url": section.get("url"), "matches": found}, 6000)

    @filter.llm_tool(name="browser_find")
    async def browser_find_tool(self, event: AstrMessageEvent, keyword: str, limit: int = 5):
        """在当前网页的正文里查找关键词，返回上下文片段。

        Args:
            keyword(string): 关键词。
            limit(number): 最多返回几段。
        """
        return self._browser_find_text(keyword, limit)

    @filter.llm_tool(name="browser_cookies")
    async def browser_cookies_tool(self, event: AstrMessageEvent):
        """查看内置浏览器的会话 cookie（用于确认登录态）。"""
        cookies = self._browser_session().cookies()
        return compact_json({"count": len(cookies), "cookies": cookies[:20]}, 4000)

    async def _kb_list_data(self) -> list[dict[str, Any]]:
        """AstrBot 知识库清单；KB 模块未初始化时返回空。"""
        manager = getattr(self.context, "kb_manager", None)
        if manager is None:
            return []
        try:
            kbs = await manager.list_kbs()
        except Exception:
            return []
        return [
            {
                "kb_id": str(getattr(kb, "kb_id", "")),
                "name": str(getattr(kb, "kb_name", "")),
                "description": str(getattr(kb, "description", "") or "")[:140],
            }
            for kb in kbs
        ]

    async def _knowledge_enrichment(self, question: str) -> dict[str, Any] | None:
        """技术类问题（数学/信息学）强制先查知识库：由插件主动检索并把结果
        与指令注入上下文。不指望模型自觉调用 kb_search——弱模型经常不查就编。"""
        question = str(question or "").strip()
        if not question or not _TECH_QUESTION_RE.search(question):
            return None
        hits: Any = []
        try:
            raw = await self._kb_search_data(question[:120], limit=3)
            parsed = parse_json_object(raw) if isinstance(raw, str) else None
            if isinstance(parsed, dict) and isinstance(parsed.get("hits"), list):
                hits = parsed["hits"]
        except Exception as error:
            logger.info("长程记忆：知识库预检索失败：%s", str(error)[:120])
        return {
            "knowledge_directive": (
                "这是技术类问题（数学/信息学），已强制检索知识库：回答前必须先依据 "
                "knowledge_hits 核对结论；hits 为空或与本题无关时，先说明知识库没有"
                "相关内容，再按你自己的知识作答，禁止谎称来自知识库。"
            ),
            "knowledge_hits": hits,
        }

    async def _kb_search_data(self, query: str, kb_id: str = "", limit: int = 5):
        """混合检索 AstrBot 知识库（稠密+BM25+RRF，与可查询记忆同级）。"""
        manager = getattr(self.context, "kb_manager", None)
        retrieval = getattr(manager, "retrieval_manager", None) if manager else None
        if retrieval is None:
            return "知识库模块未初始化（检查 AstrBot 知识库依赖与配置）"
        query = str(query or "").strip()
        if not query:
            return "需要 query"
        try:
            kbs = await manager.list_kbs()
        except Exception as error:
            return f"读取知识库清单失败：{str(error)[:120]}"
        wanted = str(kb_id or "").strip()
        targets = [
            kb for kb in kbs
            if not wanted or str(getattr(kb, "kb_id", "")) == wanted
            or str(getattr(kb, "kb_name", "")) == wanted
        ]
        if not targets:
            return f"没有匹配的知识库：{wanted}（kb_list 可查全部）"
        ids = [str(getattr(kb, "kb_id", "")) for kb in targets]
        try:
            results = await retrieval.retrieve(
                query, ids, getattr(manager, "kb_insts", {}),
                top_m_final=min(max(int(limit or 5), 1), 10),
            )
        except Exception as error:
            return f"检索失败：{type(error).__name__}: {str(error)[:160]}"
        payload = [
            {
                "kb": str(getattr(r, "kb_name", "")),
                "doc": str(getattr(r, "doc_name", "")),
                "score": round(float(getattr(r, "score", 0) or 0), 4),
                "content": str(getattr(r, "content", ""))[:500],
            }
            for r in results
        ]
        return compact_json({"query": query, "hits": payload}, 9000)

    @filter.llm_tool(name="browser_dom")
    async def browser_dom_tool(self, event: AstrMessageEvent, url: str = ""):
        """用真实浏览器渲染网页并返回可交互元素清单（按钮/输入框/链接，带序号）。

        序号供 browser_click_element / browser_type_text 使用。支持 JS 动态页面。

        Args:
            url(string): 要打开的网址；留空则刷新当前页面。
        """
        remembered = self._nav_failure_note(str(url or "").strip())
        if remembered:
            return f"这个站点刚试过、打不开（{remembered}）。别再换工具重试同一站点。"
        driver = self._pw_driver()
        try:
            if str(url or "").strip():
                await driver.goto(str(url).strip())
            snapshot = await driver.dom_snapshot()
        except BrowserError as error:
            return f"页面操作失败：{error}"
        self._spawn(self._browser_note(
            f"[浏览] DOM快照 {snapshot.get('title') or snapshot.get('url')}"))
        return compact_json(snapshot, 9000)

    @filter.llm_tool(name="browser_click_element")
    async def browser_click_element_tool(self, event: AstrMessageEvent, index: int):
        """点击网页上的第 N 号可交互元素（序号来自 browser_dom），返回结果页内容。

        Args:
            index(number): browser_dom 返回的元素序号。
        """
        driver = self._pw_driver()
        try:
            result = await driver.click(int(index))
        except BrowserError as error:
            return f"点击失败：{error}"
        self._spawn(self._browser_note(
            f"[浏览] 点击元素{index} → {result.get('title') or result.get('url')}"))
        return compact_json(result, self.settings.context_char_budget // 2)

    @filter.llm_tool(name="browser_type_text")
    async def browser_type_text_tool(
        self, event: AstrMessageEvent, index: int, text: str, submit: bool = False
    ):
        """向网页上第 N 号输入框输入文字（可选回车提交），返回结果页内容。

        Args:
            index(number): browser_dom 返回的输入框序号。
            text(string): 要输入的内容。
            submit(bool): 输入后是否按回车提交（搜索框等）。
        """
        driver = self._pw_driver()
        try:
            result = await driver.fill(int(index), str(text), submit=bool(submit))
        except BrowserError as error:
            return f"输入失败：{error}"
        self._spawn(self._browser_note(
            f"[浏览] 输入 → {result.get('title') or result.get('url')}"))
        return compact_json(result, self.settings.context_char_budget // 2)

    @filter.llm_tool(name="browser_screenshot")
    async def browser_screenshot_tool(
        self, event: AstrMessageEvent, url: str = "", full_page: bool = False
    ):
        """给网页截图（真实浏览器渲染，支持 JS 动态页面；内核首次使用自动下载）。

        【务必先自检再发送】截图拿到后先用 look_at_image(media_id) 看一眼：如果是人机验证/
        滑块/CAPTCHA/Cloudflare 等验证页（而不是你要的真实内容），**不要 send_image 发给用户**，
        先想办法过验证——用 browser_dom 看元素、browser_grid_shot 看带坐标网格的截图、
        browser_slide 拖动滑块（可多拖几次微调）、browser_click_xy 点击、browser_type_text 输入；
        过验证后**再用本工具（url 留空=对当前页重新截图，不要重新导航，否则验证会重来）**复查，
        确认是真正内容了才 send_image。

        Args:
            url(string): 要截图的 http/https 网址；**留空=对当前已打开的页面截图**（做完验证操作后复查用）。
            full_page(bool): 是否整页截图（默认只截首屏）。
        """
        if not self.media:
            return "媒体归档未启用，无法保存截图"
        target = str(url or "").strip()
        if target:
            remembered = self._nav_failure_note(target)
            if remembered:
                return (f"这个站点刚试过、打不开（{remembered}）。别再换工具重试同一站点——"
                        "要么换一个能打开的网址，要么直接告诉用户这个站这边访问不了。")
        driver = self._pw_driver()
        if not target:
            # screenshot the CURRENT page (no navigation) — used to re-check after
            # solving a captcha; re-navigating would reset the verification.
            try:
                png = await driver.screenshot(None, full_page=bool(full_page))
            except BrowserError as error:
                return f"截图失败：{error}"
            return await self._save_and_report_shot(png, "当前页面", "")
        # Pre-fetch the HTML over the stdlib engine (fast, and reachable on
        # networks where the browser's own navigation stalls). If the live
        # render can't connect, the driver falls back to rendering this.
        fallback_html: str | None = None
        base_url = target
        try:
            base_url, fallback_html = await asyncio.wait_for(
                self._browser_session().fetch_html(target), timeout=8)
        except Exception:
            fallback_html = None
            base_url = target
        try:
            png = await driver.screenshot(
                target, full_page=bool(full_page),
                fallback_html=fallback_html, base_url=base_url)
        except BrowserError as error:
            if fallback_html:
                return (f"截图失败：{error}（且降级渲染也未成功）")
            return f"截图失败：{error}"
        nav_error = str(getattr(driver, "last_nav_error", "") or "")
        if nav_error:
            logger.info("长程记忆：浏览器实时导航失败(%s)，已用抓取的HTML降级渲染 %s",
                        nav_error, target)
            self._remember_nav_failure(target, nav_error)
        elif len(png) < 8000:
            # 降级渲染的空白页：抓回来的 HTML 是个 JS 壳（bilibili 这类），渲染出来啥也没有
            logger.info("长程记忆：截图疑似空白（%d 字节），提示模型别当内容用", len(png))
            return compact_json({
                "ok": False, "media_id": "",
                "note": ("这张图基本是空白的——该站是 JS 渲染的单页应用，"
                         "浏览器这边拿不到内容；别把空白图发给用户，"
                         "换 browse 读文字或直接说明打不开。"),
            }, 1200)
        return await self._save_and_report_shot(png, target, nav_error)

    _NAV_FAIL_TTL = 300.0        # 同一个站点失败后 5 分钟内不再硬试

    def _nav_failure_note(self, url: str) -> str:
        """这个站点最近失败过吗？返回当时的失败原因（否则空串）。"""
        cache = getattr(self, "_nav_fail_cache", None)
        if not cache:
            return ""
        try:
            from urllib.parse import urlparse

            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return ""
        if not host:
            return ""
        hit = cache.get(host)
        if not hit:
            return ""
        when, reason = hit
        if time.monotonic() - when > self._NAV_FAIL_TTL:
            cache.pop(host, None)
            return ""
        return str(reason)

    def _remember_nav_failure(self, url: str, reason: str) -> None:
        """记下失败：实录里模型在同一个站点上连着试 browser_screenshot→browser_dom→
        ssh_screenshot→python_exec，四条路全废还烧了十几轮 token。"""
        if not url or not reason:
            return
        try:
            from urllib.parse import urlparse

            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return
        if not host:
            return
        cache = getattr(self, "_nav_fail_cache", None)
        if cache is None:
            cache = self._nav_fail_cache = {}
        cache[host] = (time.monotonic(), str(reason)[:120])

    async def _save_and_report_shot(self, png: bytes, label: str, nav_error: str):
        scope_id = next(iter(dict.fromkeys(self._known_scopes.values())), "") or ""
        try:
            record = await self.media.save_bytes(
                png, scope_id=scope_id, kind="image", mime="image/png",
                note=f"screenshot {label}",
            )
        except Exception as error:
            return f"截图保存失败：{str(error)[:160]}"
        self._spawn(self._browser_note(f"[截图] {label}"))
        result: dict[str, Any] = {
            "ok": record.status == "saved",
            "media_id": record.item_id,
            "hint": "先 look_at_image 自检；是验证页就先过验证再复查，确认无误才 send_image",
        }
        if nav_error:
            result["note"] = (
                f"浏览器直连该站失败（{nav_error}），这张图是用抓取到的HTML降级渲染的，"
                "动态/JS 内容可能缺失")
        return compact_json(result, 3000)

    @filter.llm_tool(name="browser_grid_shot")
    async def browser_grid_shot_tool(self, event: AstrMessageEvent):
        """给当前页面截图并叠加坐标网格（每100px一条线+坐标数字），存档返回 media_id。

        再用 look_at_image(media_id) 看这张带网格的图，就能估出滑块手柄/按钮/验证目标的
        像素坐标（视口约 1280×860），供 browser_slide / browser_click_xy 精确操作。这张带网格的
        图只给你自己看定位用，别 send_image 发给用户。
        """
        if not self.media:
            return "媒体归档未启用，无法保存截图"
        try:
            png = await self._pw_driver().screenshot(None, grid=True)
        except BrowserError as error:
            return f"截图失败：{error}"
        return await self._save_and_report_shot(png, "当前页面(带坐标网格,仅供你定位)", "")

    @filter.llm_tool(name="browser_slide")
    async def browser_slide_tool(
        self, event: AstrMessageEvent,
        from_x: int, from_y: int, to_x: int, to_y: int, steps: int = 30
    ):
        """按住 (from_x,from_y) 一路拖到 (to_x,to_y) 再松手——过滑块验证用（拟人化持续拖动）。

        坐标是视口像素（约 1280×860）；先用 browser_grid_shot + look_at_image 估好滑块手柄
        起点与目标位置。滑块常需多次微调：拖完用 browser_screenshot（url 留空=复查当前页），
        没对齐/没通过就再拖一次（调整 to_x）。

        Args:
            from_x(number): 起点X（滑块手柄中心）。
            from_y(number): 起点Y。
            to_x(number): 终点X（拖到的目标位置）。
            to_y(number): 终点Y（一般与起点Y相近）。
            steps(number): 拖动步数，默认30；越大越慢越像人手。
        """
        try:
            result = await self._pw_driver().slide(
                from_x, from_y, to_x, to_y, steps=int(steps or 30))
        except BrowserError as error:
            return f"滑动失败：{error}"
        self._spawn(self._browser_note(f"[浏览] 滑动 {from_x}->{to_x}"))
        return compact_json({**result, "hint": "用 browser_screenshot(留空url) 复查是否过验证"}, 2000)

    @filter.llm_tool(name="browser_click_xy")
    async def browser_click_xy_tool(self, event: AstrMessageEvent, x: int, y: int):
        """在页面像素坐标 (x,y) 处点击（视口约 1280×860）——点验证图片/按钮/选项用。

        Args:
            x(number): 视口X像素。
            y(number): 视口Y像素。
        """
        try:
            result = await self._pw_driver().click_xy(x, y)
        except BrowserError as error:
            return f"点击失败：{error}"
        self._spawn(self._browser_note(f"[浏览] 点击坐标({x},{y})"))
        return compact_json({**result, "hint": "用 browser_screenshot(留空url) 复查"}, 2000)

    @filter.llm_tool(name="browser_drag_element")
    async def browser_drag_element_tool(
        self, event: AstrMessageEvent, index: int, dx: int, dy: int = 0
    ):
        """按住 browser_dom 里第 index 号元素的中心，拖动 (dx,dy) 像素——滑块手柄在DOM里时用。

        Args:
            index(number): browser_dom 返回的元素序号（滑块手柄/可拖动按钮）。
            dx(number): 水平拖动像素（向右为正）。
            dy(number): 垂直拖动像素（默认0）。
        """
        try:
            result = await self._pw_driver().drag_element(int(index), dx, dy)
        except BrowserError as error:
            return f"拖动失败：{error}"
        self._spawn(self._browser_note(f"[浏览] 拖动元素{index} dx={dx}"))
        return compact_json({**result, "hint": "用 browser_screenshot(留空url) 复查"}, 2000)

    @filter.llm_tool(name="browser_scroll")
    async def browser_scroll_tool(
        self, event: AstrMessageEvent, amount: int = 600, to_bottom: bool = False,
        element_index: int = -1,
    ):
        """滚动网页，让长页面下方的内容进入视野（DOM 快照只列当前视口可见元素，先滚动再刷新）。

        Args:
            amount(number): 向下滚动的像素（负数向上），默认600。
            to_bottom(boolean): true 时直接滚到页面底部（忽略 amount）。
            element_index(number): 滚到 browser_dom 里第 N 号元素并居中（≥0 时生效）。
        """
        try:
            state = await self._pw_driver().scroll(
                amount=int(amount or 0), to_bottom=bool(to_bottom),
                index=int(element_index) if int(element_index) >= 0 else None,
            )
        except BrowserError as error:
            return f"滚动失败：{error}"
        self._spawn(self._browser_note(f"[浏览] 滚动页面 to_bottom={to_bottom} amount={amount}"))
        return compact_json(
            {**state, "hint": "滚动后用 browser_dom(留空url) 刷新可见元素"}, 2000)

    @filter.llm_tool(name="kb_list")
    async def kb_list_tool(self, event: AstrMessageEvent):
        """列出 AstrBot 知识库（知识渠道，与你的可查询记忆同级）。"""
        return compact_json({"kbs": await self._kb_list_data()}, 5000)

    @filter.llm_tool(name="kb_search")
    async def kb_search_tool(
        self, event: AstrMessageEvent, query: str, kb_id: str = "", limit: int = 5
    ):
        """在 AstrBot 知识库里检索资料（混合检索：向量+BM25+融合）。

        Args:
            query(string): 要查的问题或关键词。
            kb_id(string): 可选，限定某个知识库（ID 或名称）；留空查全部。
            limit(number): 返回条数，最大10。
        """
        return await self._kb_search_data(query, kb_id, limit)

    async def _run_subagent_task(
        self, task: str, scope: str | None, media_ids: list[str] | None = None,
        event: AstrMessageEvent | None = None,
    ) -> str:
        """执行型子agent：自带除浏览器外的全部工具（ssh/代码/文件/发媒体…），
        在 tool_loop_agent 循环里真正把任务做完，而不只是出分析。

        有事件上下文（前台回复派发）→ 工具循环；无事件（定时/调度通道）→
        退回单次分析（老只读路径）。带 media_ids 时图片直接附在请求里。
        """
        task = str(task or "").strip()
        if not task:
            return "需要 task"
        if not self.storage:
            return "存储未就绪"
        provider = (self.settings.subagent_provider_id.strip()
                    or self.settings.reply_provider_id or "")
        if not provider:
            return "没有可用的子agent模型（请在 WebUI 配置 subagent_provider_id）"
        image_urls: list[str] = []
        wanted = [str(x).strip() for x in (media_ids or []) if str(x).strip()][:6]
        if wanted:
            if self.media is None:
                return "媒体归档未启用，无法附带图片"
            for media_id in wanted:
                try:
                    mime, b64 = await self.media.get_base64(media_id)
                    image_urls.append(f"data:{mime};base64,{b64}")
                except Exception as error:
                    return f"取媒体 {media_id[:8]} 失败：{str(error)[:120]}"
        payload_obj: dict[str, Any] = {"task": task}
        if image_urls:
            payload_obj["image_count"] = len(image_urls)
            payload_obj["note"] = "图片已直接附在本请求里，逐张分析后再汇总结论"
        else:
            target = scope or next(iter(dict.fromkeys(self._known_scopes.values())), None)
            if not target:
                return "还没有任何会话数据"
            try:
                recent = await self.storage.recent_messages(target, 120)
            except Exception as error:
                return f"读取聊天记录失败：{str(error)[:120]}"
            payload_obj["recent_chat"] = [
                {"sender": m.sender_name or m.sender_id, "text": m.text[:120]}
                for m in recent
                if m.text and not m.upstream_message_id.startswith("self:")
            ][-80:]
            by_user: dict[str, list[str]] = {}
            for m in recent:
                if m.upstream_message_id.startswith("self:") or not m.text:
                    continue
                by_user.setdefault(m.sender_id, []).append(m.text)
            payload_obj["style_samples"] = [
                {"user_id": uid, "samples": [x[:60] for x in texts[-5:]]}
                for uid, texts in sorted(by_user.items(), key=lambda kv: -len(kv[1]))[:8]
            ]
            try:
                payload_obj["recent_topics"] = [
                    str(t.get("topic", ""))
                    for t in await self.storage.recent_topics(target, 8)]
            except Exception:
                pass
        payload = compact_json(payload_obj, self.settings.context_char_budget)
        if event is not None and self.settings.subagent_tools_enabled:
            try:
                raw = await self._llm_text(
                    provider,
                    event=event,
                    prompt=payload,
                    system_prompt=SUBAGENT_SYSTEM_PROMPT,
                    tools=self._subagent_tool_set(event),
                    agent=True,
                    image_urls=image_urls or None,
                )
                return (raw or "").strip()[:8000] or "子agent没有产出结论"
            except Exception as error:
                logger.info(
                    "长程记忆：子agent工具循环失败，退回单次分析：%s",
                    f"{type(error).__name__}: {str(error)[:140]}")
        try:
            return (await self._llm_text(
                provider,
                prompt=payload,
                system_prompt=SUBAGENT_SYSTEM_PROMPT,
                image_urls=image_urls or None,
            ) or "").strip()[:6000] or "子agent没有产出结论"
        except Exception as error:
            return f"子agent执行失败：{type(error).__name__}: {str(error)[:160]}"

    @filter.llm_tool(name="dispatch_subagent")
    async def dispatch_subagent_tool(
        self, event: AstrMessageEvent, task: str = "", media_ids_json: str = "",
    ):
        """派发一个执行型子agent完成子任务。子agent有除浏览器外的全部工具
        （ssh、python_exec、read_tabular、fs_* 文件读写、read_document、
        send_image/send_local_file、查记忆搜记录等），会真正动手做完并汇报结果，
        比你在主循环里一步步做省步数；一堆图片的分析也优先派它（media_ids 直读）。
        子agent没有派发子agent的工具（防递归）。

        Args:
            task(string): 给子agent的任务描述，如"逐张分析这些截图里的数据"。
            media_ids_json(string): 可选，要附给子agent的图片 media_id 数组 JSON，
                如 ["abc123","def456"]（media_recent / 消息里的图片都有 media_id）。
        """
        if not self.settings.subagent_enabled:
            return "子agent未启用：请在 WebUI 配置里开启 subagent_enabled"
        task = str(task or "").strip()
        if not task:
            return "需要 task（给子agent的任务描述）。分析图片时同时传 media_ids_json"
        media_ids = self._parse_media_ids(media_ids_json)
        scope = await self._scope_for_event(event) if event is not None else None
        return await self._run_subagent_task(task, scope, media_ids, event=event)

    @staticmethod
    def _parse_media_ids(raw: str) -> list[str]:
        text = str(raw or "").strip()
        if not text:
            return []
        data = parse_json_value(text)
        if data is None:
            return [part.strip() for part in text.split(",") if part.strip()]
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
        if isinstance(data, str):
            return [data.strip()] if data.strip() else []
        return []

    @filter.llm_tool(name="dispatch_parallel_subagents")
    async def dispatch_parallel_subagents_tool(
        self, event: AstrMessageEvent, tasks_json: str
    ):
        """并行派发最多4个执行型子agent（互不阻塞），返回各自的结果。
        子agent有除浏览器外的全部工具，能真正执行任务（跑代码/SSH/读文件/发媒体）。
        任务可以是纯文本，也可以带图片：{"task":"分析这张图","media_ids":["..."]}。
        批量图片分析、多文档整理、互相独立的子任务时用它，别在主循环里串行慢慢做。

        Args:
            tasks_json(string): 任务数组 JSON，如 ["总结今天的话题",
                {"task":"分析截图","media_ids":["media_id1","media_id2"]}]。
        """
        if not self.settings.subagent_enabled:
            return "子agent未启用：请在 WebUI 配置里开启 subagent_enabled"
        try:
            import json as _json

            tasks = _json.loads(str(tasks_json or "[]"))
        except Exception:
            return "tasks_json 不是合法 JSON 数组"
        if not isinstance(tasks, list) or not tasks:
            return "需要非空任务数组"
        parsed: list[tuple[str, list[str]]] = []
        for item in tasks[:4]:
            if isinstance(item, str) and item.strip():
                parsed.append((item.strip()[:400], []))
            elif isinstance(item, dict) and str(item.get("task", "")).strip():
                parsed.append((
                    str(item.get("task")).strip()[:400],
                    self._parse_media_ids(_json.dumps(item.get("media_ids") or [], ensure_ascii=False)),
                ))
        if not parsed:
            return "需要非空任务数组"
        scope = await self._scope_for_event(event) if event is not None else None
        results = await asyncio.gather(
            *(self._run_subagent_task(task, scope, media_ids, event=event)
              for task, media_ids in parsed),
            return_exceptions=True,
        )
        labeled = [
            {"task": task,
             "result": result if isinstance(result, str)
             else f"失败：{type(result).__name__}"}
            for (task, _media), result in zip(parsed, results)
        ]
        return compact_json(labeled, 8000)

    @filter.llm_tool(name="ssh_exec")
    async def ssh_exec_tool(self, event: AstrMessageEvent, command: str, timeout: int = 0):
        """在已配置的远程 Ubuntu 机器上执行一条 shell 命令，返回 stdout/stderr/退出码。

        每次是独立的一条命令（不保留 cd/环境变量/venv 状态），需要上下文就用 && 串联或用绝对路径。
        仅当插件配置里填了 ssh_host/ssh_user/ssh_password 时可用（目前仅支持 Ubuntu）。

        Args:
            command(string): 要在远程执行的 shell 命令。
            timeout(number): 超时秒数，留空用默认（ssh_command_timeout）。
        """
        if self._ssh is None or not self._ssh_configured():
            return "SSH 未配置：请在插件配置填 ssh_host / ssh_user / ssh_password（目前仅支持 Ubuntu）"
        seconds = int(timeout) if timeout and int(timeout) > 0 else self.settings.ssh_command_timeout
        try:
            result = await self._ssh.exec(str(command or ""), timeout=float(seconds))
        except SSHError as error:
            return f"SSH 执行失败：{error}"
        return compact_json(result, self.settings.context_char_budget // 2)

    # ---------------------------------------------- 远程文件回传（截图/文件发QQ）
    async def _pull_remote_file(self, remote_path: str, note: str = "") -> dict[str, Any]:
        """把远程服务器上的文件拉进本地媒体库（整块 base64 回传）。

        这是"把服务器上的图发我"的专用通道——走 ssh_exec 的常规输出会被 12k
        上限截断成坏文件（实录：bot 说"图送上来了"却怎么也发不出来）。
        """
        if self._ssh is None or not self._ssh_configured():
            raise SSHError("SSH 未配置：请在插件配置填 ssh_host / ssh_user / ssh_password")
        if self.media is None:
            raise SSHError("媒体归档未启用")
        path = str(remote_path or "").strip()
        if not path:
            raise SSHError("需要 remote_path")
        if any(ch in path for ch in "\n\r'\"`$"):
            raise SSHError("路径包含非法字符")
        probe = await self._ssh.exec(
            f"test -f '{path}' && stat -c %s '{path}' || echo MISSING", timeout=20)
        out = str(probe.get("stdout") or "").strip().splitlines()
        size_line = out[-1].strip() if out else ""
        if size_line.endswith("MISSING") or not size_line.isdigit():
            raise SSHError(f"远程文件不存在：{path}")
        size = int(size_line)
        cap = int(getattr(self.media, "max_file_bytes", 15 * 1024 * 1024))
        if size <= 0:
            raise SSHError("远程文件为空")
        if size > cap:
            raise SSHError(f"文件 {size} 字节，超过上限 {cap}")
        fetched = await self._ssh.exec(
            f"base64 -w0 '{path}'",
            timeout=max(60.0, min(300.0, size / 1024)),  # 10mbps ≈ 128KB/s
            max_out=size * 2 + 8192,
        )
        if fetched.get("truncated"):
            raise SSHError("回传被截断（文件过大）")
        encoded = "".join(str(fetched.get("stdout") or "").split())
        import base64 as _b64

        try:
            data = _b64.b64decode(encoded, validate=True)
        except Exception as error:
            raise SSHError(f"远程内容解码失败：{str(error)[:80]}") from error
        if len(data) != size:
            raise SSHError(f"字节数不符（{len(data)} != {size}），回传可能损坏")
        from .src.media_archive import _magic_mime

        mime = _magic_mime(data)
        kind = "image" if mime.startswith("image/") else "file"
        record = await self.media.save_bytes(
            data, scope_id="studio", kind=kind, mime=mime,
            note=note or f"ssh:{path}")
        ok = getattr(record, "status", "") == "saved"
        logger.info(
            "长程记忆：远程文件已回传（%s，%d 字节 → %s）", path[-40:], len(data),
            "已归档" if ok else "归档跳过")
        return {"ok": ok, "media_id": getattr(record, "item_id", ""),
                "kind": kind, "mime": mime, "bytes": len(data),
                "remote_path": path}

    async def _ssh_fetch_core(self, remote_path: str, note: str = "") -> str:
        try:
            result = await self._pull_remote_file(remote_path, note)
        except SSHError as error:
            return f"拉取失败：{error}"
        except Exception as error:
            return f"拉取失败：{type(error).__name__}: {str(error)[:150]}"
        return compact_json({**result, "hint": "用 send_image(media_id) 发出去"}, 2000)

    async def _ssh_shot_core(self, url: str, width: int = 1280,
                             height: int = 900) -> str:
        """在远程服务器上用 chromium 截图并拉回媒体库。"""
        url = str(url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return "需要 http(s) 的 url"
        if any(ch in url for ch in "\n\r'\"`$;|"):
            return "url 包含非法字符"
        w = min(max(int(width or 1280), 320), 2560)
        h = min(max(int(height or 900), 240), 2000)
        script = (
            'CHROME=$(command -v chromium || command -v chromium-browser || '
            'command -v google-chrome || command -v google-chrome-stable); '
            '[ -n "$CHROME" ] || { echo NO_CHROME; exit 3; }; '
            'for MODE in --headless=new --headless; do '
            'rm -f /tmp/zcode_shot.png; '
            f'"$CHROME" $MODE --no-sandbox --disable-gpu --hide-scrollbars '
            f"--window-size={w},{h} --screenshot=/tmp/zcode_shot.png '{url}' "
            '2>/tmp/zcode_shot.err; '
            'test -s /tmp/zcode_shot.png && break; done; '
            'test -s /tmp/zcode_shot.png && echo SHOT_OK || '
            '{ echo SHOT_FAIL; tail -3 /tmp/zcode_shot.err; }'
        )
        try:
            probe = await self._ssh.exec(script, timeout=120.0, max_out=4000)
        except SSHError as error:
            return f"远程截图失败：{error}"
        out = str(probe.get("stdout") or "")
        if "SHOT_OK" not in out:
            detail = "；".join(out.strip().splitlines()[-3:])[:200]
            if "NO_CHROME" in out:
                return "远程没装 chromium：先 ssh_exec 装 chromium-browser 再截图"
            return f"远程截图未产出图片：{detail}"
        result = await self._ssh_fetch_core(
            "/tmp/zcode_shot.png", note=f"ssh截图 {url[:80]}")
        return result

    @filter.llm_tool(name="ssh_fetch")
    async def ssh_fetch_tool(self, event: AstrMessageEvent, remote_path: str,
                             note: str = ""):
        """把远程服务器上的文件（图片/日志/任何文件）拉回媒体库，返回 media_id，
        再用 send_image(media_id) 发给用户。用户说"把服务器上那张图发我/把日志发来"
        就用它——这是把远程文件发到 QQ 的正确通道（ssh_exec 的输出上限传不了文件）。

        Args:
            remote_path(string): 远程机上的文件路径，如 /root/shot.png。
            note(string): 可选备注。
        """
        return await self._ssh_fetch_core(remote_path, note)

    @filter.llm_tool(name="ssh_screenshot")
    async def ssh_screenshot_tool(
        self, event: AstrMessageEvent, url: str, width: int = 1280, height: int = 900,
    ):
        """在远程服务器上用 chromium 对网页截图，并把图拉回媒体库（返回 media_id，
        再用 send_image 发出去）。用户要"看看服务器上打开某网页的样子"就用它。

        Args:
            url(string): 要截图的网址（http/https）。
            width(number): 视口宽度（默认1280）。
            height(number): 视口高度（默认900）。
        """
        return await self._ssh_shot_core(url, int(width or 1280), int(height or 900))

    @filter.llm_tool(name="ssh_info")
    async def ssh_info_tool(self, event: AstrMessageEvent):
        """查看远程 SSH 机器信息：配置注释（机器规格/网络限制/开放端口）+ 连通性探测。
        用户提到 ssh、服务器、远程机器、部署时先调用它确认可用——不要说"没有连接信息"，
        信息都在配置里、直接调就能连。"""
        if self._ssh is None or not self._ssh_configured():
            return "SSH 未配置：请在插件配置填 ssh_host / ssh_user / ssh_password（目前仅支持 Ubuntu）"
        cfg = self._ssh_config()
        info: dict[str, Any] = {"target": cfg.target(),
                                "notes": cfg.notes or "（未填写机器注释）"}
        try:
            info["probe"] = await self._ssh.check()
        except SSHError as error:
            info["probe_error"] = str(error)
        return compact_json(info, 6000)

    @filter.llm_tool(name="ssh_agent_dispatch")
    async def ssh_agent_dispatch_tool(self, event: AstrMessageEvent, task: str):
        """派一个运维子agent自主操作远程 Ubuntu 机器完成任务（装环境、部署、排查等）。

        子agent会一步步在远程跑命令直到完成；派出后用 ssh_agent_status 看进度，
        用 ssh_agent_control 暂停/继续/追问/改目标/结束。

        Args:
            task(string): 交给子agent的运维任务，如"给这台机器装好 docker 和 docker-compose"。
        """
        if self._ssh is None or self._ssh_agents is None or not self._ssh_configured():
            return "SSH 未配置：请在插件配置填 ssh_host / ssh_user / ssh_password（目前仅支持 Ubuntu）"
        try:
            session = self._ssh_agents.dispatch(str(task or ""))
        except SSHError as error:
            return f"派发失败：{error}"
        return compact_json({
            "ok": True, "session_id": session.id, "state": session.state,
            "hint": "用 ssh_agent_status(session_id) 看进度，ssh_agent_control 控制它",
        }, 2000)

    @filter.llm_tool(name="ssh_agent_status")
    async def ssh_agent_status_tool(self, event: AstrMessageEvent, session_id: str = ""):
        """查看 SSH 运维子agent的状态与执行日志（留空 session_id 则列出全部会话概览）。

        Args:
            session_id(string): 子agent会话ID；留空看所有会话概览。
        """
        if self._ssh_agents is None:
            return "SSH 未就绪"
        status = self._ssh_agents.status(str(session_id or ""))
        if status is None:
            return "没有这个会话（session_id 不对，先不带参数看全部）"
        return compact_json(status, self.settings.context_char_budget // 2)

    @filter.llm_tool(name="ssh_agent_control")
    async def ssh_agent_control_tool(
        self, event: AstrMessageEvent, session_id: str, action: str, message: str = ""
    ):
        """控制正在运行的 SSH 运维子agent：查看/暂停/继续/追问或改目标/结束。

        Args:
            session_id(string): 子agent会话ID。
            action(string): status(状态)/pause(暂停)/resume(继续)/update(更新任务目标)/ask(给它留言或答复它的提问)/stop(结束)。
            message(string): update/ask 时要传达的内容（新目标、答复或补充指示）。
        """
        if self._ssh_agents is None:
            return "SSH 未就绪"
        result = self._ssh_agents.control(
            str(session_id or ""), str(action or ""), str(message or ""))
        return compact_json(result, self.settings.context_char_budget // 2)

    # ------------------------------------------------------------------ studio tools（designer / programmer）
    async def _design_screenshot(self, content: str) -> str:
        """Render HTML content in a fresh browser page and archive the PNG."""
        # screenshot 是 async（_pw_driver 的方法），不能塞进 to_thread 的同步
        # lambda——那只会拿到 coroutine（实录：object of type 'coroutine' has no len()）
        png = await self._pw_driver().screenshot(None, fallback_html=content)
        if not png:
            raise BrowserError("渲染截图为空")
        if self.media is None:
            return ""
        record = await self.media.save_bytes(
            png, scope_id="studio", kind="image", mime="image/png",
            note="design-render")
        return str(record.item_id)

    @filter.llm_tool(name="design_render")
    async def design_render_tool(
        self, event: AstrMessageEvent, html: str = "", svg: str = "",
        title: str = "", description: str = "", project_id: str = "",
        save: bool = False,
    ):
        """绘图/设计：用 HTML 或 SVG 画一张图，渲染成图片（拿 media_id 用 send_image 发）。
        渲染窗口最多存在 60 秒，期间可用 browser_dom/browser_screenshot(url=返回的url) 访问。
        save=true 时存为项目（必须给 title 和 description，会加入记忆）。
        **聊天记录样子的卡片不要用它**——那种一律 screenshot_messages（latest_count 可以直接
        给条数）；转发聊天记录用 forward_messages。手搓 HTML 画聊天界面会做成大字报。

        Args:
            html(string): 完整 HTML 页面内容（含 CSS/JS）；与 svg 二选一。
            svg(string): 纯 SVG 图形代码；与 html 二选一。
            title(string): 存项目时的标题（save=true 必填）。
            description(string): 存项目时的简介（save=true 必填）。
            project_id(string): 要重新渲染的已存项目 ID（design_list 里选）。
            save(boolean): true=把这次内容存为项目。
        """
        host = self._studio_host()
        content = ""
        if str(project_id or "").strip():
            row = host.get_design_project(project_id.strip())
            if row is None:
                return f"没有这个设计项目：{project_id}（design_list 可查全部）"
            content = str(row.get("content", ""))
            title = title or str(row.get("title", ""))
            description = description or str(row.get("description", ""))
        elif str(svg or "").strip():
            content = self._wrap_svg(svg)
        elif str(html or "").strip():
            content = html
        else:
            return "需要提供 html 或 svg 内容（或 project_id 重渲染已存项目）"
        window = host.put_design(content)
        try:
            media_id = await self._design_screenshot(content)
        except Exception as error:
            media_id = ""
            logger.info("长程记忆：设计渲染截图失败：%s", str(error)[:140])
        saved_as = ""
        if save or (project_id and title):
            if not title.strip() or not description.strip():
                return (f"已渲染（media_id={media_id or '无'}，窗口 {window['url']} 60秒）。"
                        "但保存项目需要 title 和 description，请补齐后重试 save=true。")
            row = host.save_design_project(title, description, content)
            saved_as = str(row["id"])
            self._spawn(self._studio_note(
                f"[设计] {title}：{description}", "design"))
        return compact_json({
            "ok": True, "media_id": media_id, "url": window["url"],
            "expires_in_seconds": window["expires_in_seconds"],
            "project_id": saved_as,
            "hint": "send_image(media_id) 发送图片；url 是本机渲染窗口（60秒内可访问）",
        }, 3000)

    @filter.llm_tool(name="design_list")
    async def design_list_tool(self, event: AstrMessageEvent):
        """列出已保存的绘图/设计项目（ID/标题/简介）。"""
        return compact_json({"projects": self._studio_host().list_design_projects()}, 4000)

    @filter.llm_tool(name="design_load")
    async def design_load_tool(self, event: AstrMessageEvent, project_id: str):
        """读取一个已存设计项目的完整源码（HTML/SVG），便于修改后重新 design_render。

        Args:
            project_id(string): 设计项目 ID。
        """
        row = self._studio_host().get_design_project(str(project_id or "").strip())
        if row is None:
            return f"没有这个设计项目：{project_id}（design_list 可查全部）"
        return compact_json({
            "project_id": row["id"], "title": row.get("title", ""),
            "description": row.get("description", ""),
            "content": row.get("content", ""),
            "hint": "修改后用 design_render(html=.../svg=..., save=true, title=..., description=...) 重渲染并保存",
        }, self.settings.context_char_budget // 2)

    @filter.llm_tool(name="render_code")
    async def render_code_tool(
        self, event: AstrMessageEvent, code: str, language: str = "",
        filename: str = "",
    ):
        """把一段源代码渲染成 VSCode 风格的语法高亮图片（拿 media_id 用 send_image 发给用户）。
        展示你写的 designer/program/脚本代码时用它，比直接发一大段文字好看得多。

        Args:
            code(string): 源代码全文。
            language(string): 语言：python/javascript/typescript/json/html/css/c/java/go/rust/sql/bash（留空自动通用高亮）。
            filename(string): 显示在窗口标签上的文件名，如 main.py。
        """
        if not str(code or "").strip():
            return "需要 code"
        from .src.code_render import code_to_page

        page = code_to_page(code, str(language or ""), str(filename or ""))
        try:
            media_id = await self._design_screenshot(page)
        except Exception as error:
            return f"代码渲染失败：{str(error)[:160]}"
        if not media_id:
            return "媒体归档未启用，无法生成图片"
        return compact_json({
            "ok": True, "media_id": media_id,
            "hint": "send_image(media_id) 发给用户",
        }, 1500)

    @filter.llm_tool(name="program_write")
    async def program_write_tool(
        self, event: AstrMessageEvent, code: str, title: str = "",
        description: str = "", program_id: str = "",
    ):
        """programmer：写一个 Python+Flask 小程序给自己跑（单端口多页面，网页可访问）。
        代码里要定义 app = Flask(__name__) 并挂 @app.route 路由；可用注入变量
        PROGRAM_ID 与 DATA_DIR（把游戏数据实时写进 DATA_DIR 下的 json 文件即实时保存）。
        进程存活 program_ttl_minutes 分钟，可随时再次调用本工具热重启/改代码。
        创建新程序时建议给 title 和 description（存档会加入记忆）。

        Args:
            code(string): 完整的 Python 代码（Flask 应用）。
            title(string): 程序标题（如"Wordle 猜词"）。
            description(string): 程序简介：玩法/路由说明。
            program_id(string): 更新已有程序时填它的 ID；留空自动生成。
        """
        if not str(code or "").strip():
            return "需要 code（完整 Python 代码）"
        host = self._studio_host()
        pid = str(program_id or "").strip() or slugify(
            title, f"prog-{uuid.uuid4().hex[:6]}")
        stored = host.upsert_program(pid, code, title, description)
        result = await asyncio.to_thread(host.run_program, pid, code,
                                         str(title or ""))
        if not result.get("ok"):
            return compact_json({
                "ok": False, "error": result.get("error"),
                "hint": "修好代码后再次 program_write 重启；数据目录不受影响",
            }, 3000)
        if stored["created"] and title.strip() and description.strip():
            self._spawn(self._studio_note(
                f"[程序] {title}：{description}（访问 {result['url']}）", "program"))
        return compact_json({**result, "program_id": pid,
                             "updated": not stored["created"],
                             "hint": "program_view 看页面文本、program_screenshot 截图发给用户；"
                                     "program_write 同 id 可热改代码"}, 3000)

    @filter.llm_tool(name="program_list")
    async def program_list_tool(self, event: AstrMessageEvent):
        """列出运行中的程序（含剩余时间）与已存档的程序项目。"""
        host = self._studio_host()
        return compact_json({
            "running": host.running(),
            "projects": host.list_programs(),
        }, 5000)

    @filter.llm_tool(name="program_read")
    async def program_read_tool(self, event: AstrMessageEvent, program_id: str):
        """读取某个程序的完整 Python 源码（改代码前先读）。

        Args:
            program_id(string): 程序 ID。
        """
        row = self._studio_host().get_program(str(program_id or "").strip())
        if row is None:
            return f"没有这个程序：{program_id}（program_list 可查全部）"
        return compact_json({
            "program_id": row["id"], "title": row.get("title", ""),
            "description": row.get("description", ""),
            "code": row.get("code", ""),
            "hint": "改完用 program_write(program_id=..., code=...) 热重启",
        }, self.settings.context_char_budget // 2)

    @filter.llm_tool(name="program_view")
    async def program_view_tool(self, event: AstrMessageEvent, program_id: str, path: str = "/"):
        """获取程序某个页面的文本内容（相当于替用户看一眼网页）。

        Args:
            program_id(string): 程序 ID。
            path(string): 页面路径，默认 /。
        """
        host = self._studio_host()
        if host.get_program(str(program_id or "").strip()) is None \
                and str(program_id) not in {p["program_id"] for p in host.running()}:
            return f"程序 {program_id} 未注册或未运行（program_list 可查）"
        url = f"http://127.0.0.1:{host.port}/p/{str(program_id).strip('/')}" \
            f"/{str(path or '/').lstrip('/')}"
        def _fetch() -> tuple[int, str]:
            import urllib.request
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.status, response.read().decode("utf-8", "replace")
        try:
            status, text = await asyncio.to_thread(_fetch)
        except Exception as error:
            return f"访问失败：{type(error).__name__}: {str(error)[:160]}"
        return compact_json({"url": url, "status": status, "text": text[:4000]}, 5000)

    @filter.llm_tool(name="program_screenshot")
    async def program_screenshot_tool(
        self, event: AstrMessageEvent, program_id: str, path: str = "/",
    ):
        """给程序页面截图（拿 media_id 用 send_image 发给用户）。

        Args:
            program_id(string): 程序 ID。
            path(string): 页面路径，默认 /。
        """
        host = self._studio_host()
        url = f"http://127.0.0.1:{host.port}/p/{str(program_id).strip('/')}" \
            f"/{str(path or '/').lstrip('/')}"
        try:
            png = await self._pw_driver().screenshot(url)
        except BrowserError as error:
            return f"截图失败：{error}"
        if self.media is None:
            return "媒体归档未启用，无法生成可发送的图片"
        record = await self.media.save_bytes(
            png, scope_id="studio", kind="image", mime="image/png",
            note=f"program:{program_id}")
        return compact_json({
            "ok": True, "media_id": record.item_id, "url": url,
            "hint": "先 look_at_image 自检内容，再 send_image 发给用户",
        }, 2000)

    @filter.llm_tool(name="program_archive")
    async def program_archive_tool(
        self, event: AstrMessageEvent, program_id: str,
        title: str = "", description: str = "",
    ):
        """存档并停止一个程序（代码与数据保留，加入记忆，以后可 program_write 重启）。

        Args:
            program_id(string): 程序 ID。
            title(string): 存档标题（没有时用程序已有的）。
            description(string): 存档简介：玩法与路由说明（会加入记忆）。
        """
        host = self._studio_host()
        row = host.archive_program(str(program_id or "").strip(), title, description)
        if not row:
            return f"没有这个程序：{program_id}（program_list 可查全部）"
        final_title = str(row.get("title") or title or program_id)
        final_desc = str(row.get("description") or description or "")
        self._spawn(self._studio_note(f"[程序存档] {final_title}：{final_desc}", "program"))
        return compact_json({
            "ok": True, "program_id": row["id"], "archived": True,
            "hint": "已停止并存档；想再跑用 program_write(program_id=..., code=...) 重启",
        }, 2000)

    @filter.llm_tool(name="evolve_prompt")
    async def evolve_prompt_tool(
        self, event: AstrMessageEvent, target: str, iterations: int = 4,
    ):
        """进化自己的一段提示词（GEPA 反思式变异：生成评测集→变异→约束过滤→评分→择优）。
        只在 holdout 上严格优于基线时才采纳；全程落盘可审计（evolution/<target>/）。
        进化是后台任务：派出后用 script_list 之外的提示查询进度即可，本轮先正常回复。

        Args:
            target(string): 要进化的目标：designer(画图提示词)/program(写程序提示词)/reply(回复语言铁律)。
            iterations(number): 进化代数，默认4（每代变异4个候选，消耗较多token）。
        """
        text = self._evolution_target_text(str(target or "").strip())
        if text is None:
            return ("未知进化目标：只支持 designer / program / reply。"
                    "进化是重操作，一次只跑一个。")
        optimizer = self._evolution_optimizer(str(target or "").strip())
        self._spawn(self._run_evolution(optimizer, str(target or "").strip(),
                                        text, int(iterations or 4)))
        return compact_json({
            "ok": True, "target": str(target or "").strip(),
            "hint": "进化在后台运行（约几分钟）；完成后结果落盘 evolution/<target>/report.json，"
                    "采纳与否看 report 的 adopted 字段；进化成功会自动生效",
        }, 1200)

    def _evolution_target_text(self, target: str) -> str | None:
        """进化目标的当前文本：优先用已采纳的进化冠军（evolution/<t>/champion.md，
        附 adopted 标记），否则用内置文本。"""
        champion = self._evolution_champion(target)
        if champion is not None:
            return champion
        if target == "designer":
            return "你是插画生成器。把用户的画图请求变成一张简洁可爱的 SVG 矢量插画。" \
                   "只输出严格 JSON：{\"title\": \"标题\", \"svg\": \"<svg ...>...</svg>\"}。" \
                   "SVG 要求：宽 800 高 600、viewBox 合理、白底或浅色底；用基本形状把主体" \
                   "拼出来，主体居中、比例合理，加少量背景元素；必须是合法可渲染的 SVG。"
        if target == "program":
            return str(self.settings.standing_orders or "").strip() or (
                "你是 Flask 网页程序生成器。为用户的小游戏/小工具需求生成单文件 Flask 应用。"
                "代码里定义 app = Flask(__name__) 并挂 @app.route 路由；数据写入注入的 "
                "DATA_DIR 目录（json 文件实时保存）；代码风格清晰、有注释。")
        if target == "reply":
            return "回复语言铁律（摘要）：每条消息一句话 3~20 字；句间用空格不用逗号；句尾" \
                   "不加句号；口语词与颜文字点缀（草/乐/awa/qwq）；吐槽用半开括号结尾；" \
                   "严禁AI腔与公文结构；严禁解释自己的思考过程。" \
                   "（当前表达温度按情绪系统动态调整。）"
        return None

    def _evolution_champion(self, target: str) -> str | None:
        """读取已采纳的进化冠军文本（report.json 的 adopted=true 才生效）。"""
        root = (
            self.storage.path.parent if self.storage is not None
            else Path(get_astrbot_data_path()) / "plugin_data"
            / "astrbot_plugin_long_memory_agent"
        ) / "evolution" / str(target or "").strip()
        report_path = root / "report.json"
        champion_path = root / "champion.md"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if not report.get("adopted"):
                return None
            text = champion_path.read_text(encoding="utf-8").strip()
            return text or None
        except (OSError, json.JSONDecodeError):
            return None

    def _evolution_optimizer(self, target: str) -> GEPAOptimizer:
        root = (
            self.storage.path.parent if self.storage is not None
            else Path(get_astrbot_data_path()) / "plugin_data"
            / "astrbot_plugin_long_memory_agent"
        ) / "evolution" / target
        return GEPAOptimizer(
            llm=self._evolution_llm,
            output_dir=root,
            iterations=self.settings.evolution_iterations,
        )

    async def _evolution_llm(self, prompt: str, system_prompt: str = "") -> str:
        provider = self.settings.reply_provider_id
        return await self._llm_text(provider, prompt=prompt,
                                    system_prompt=system_prompt or "你是助手。")

    async def _run_evolution(self, optimizer: GEPAOptimizer, target: str,
                             text: str, iterations: int) -> None:
        optimizer.iterations = max(1, int(iterations))
        try:
            report = await optimizer.evolve(target, text)
        except Exception as error:
            logger.warning("长程记忆：提示词进化[%s]异常：%s", target, str(error)[:200])
            return
        logger.info(
            "长程记忆：提示词进化[%s]完成：holdout %.3f → %.3f（%+.3f，%s）",
            target, report.baseline_holdout, report.champion_holdout,
            report.improvement, "已采纳" if report.adopted else "保留基线",
        )

    @filter.llm_tool(name="script_call")
    async def script_call_tool(
        self, event: AstrMessageEvent, script: str, tool: str, params_json: str = "{}",
    ):
        """调用某个 scripts 拓展的工具（工具清单见系统提示词与 script_list）。

        Args:
            script(string): 拓展 ID（scripts 文件夹名）。
            tool(string): 工具名。
            params_json(string): 参数 JSON 对象，如 {"city":"北京"}。
        """
        try:
            params = parse_json_object(str(params_json or "").strip() or "{}") or {}
        except Exception:
            params = {}
        try:
            result = await self._script_manager().call(str(script), str(tool), params)
        except ScriptExtensionError as error:
            return f"拓展调用失败：{error}"
        except Exception as error:
            return f"拓展调用失败：{type(error).__name__}: {str(error)[:200]}"
        return str(result)[:4000]

    @filter.llm_tool(name="script_list")
    async def script_list_tool(self, event: AstrMessageEvent):
        """列出已加载的 scripts 拓展与它们提供的工具（含参数说明）。"""
        manager = self._script_manager()
        return compact_json({
            "scripts": manager.list(),
            "tools": manager.tool_catalog(),
        }, self.settings.context_char_budget // 2)

    async def _todo_action(
        self, scope: str | None, action: str, items_json: str = "",
        todo_id: str = "", status: str = "", content: str = "",
    ) -> str:
        """任务表核心（供 llm_tool 与调度通道共用）。"""
        if self.storage is None or not scope:
            return "任务表需要记忆系统就绪"
        act = str(action or "").strip().lower()
        if act == "add":
            items = parse_json_value(items_json or "")
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, list):
                items = [part.strip() for part in str(items_json or "").split(",") if part.strip()]
            items = [str(x).strip() for x in items if str(x).strip()]
            if not items:
                return "add 需要 items_json（如 [\"查资料\",\"写总结\"]）"
            rows = await self.storage.add_todos(scope, items)
            return compact_json({"ok": True, "todos": rows}, 3000)
        if act == "list":
            return compact_json({"todos": await self.storage.list_todos(scope)}, 4000)
        if act == "update":
            row = await self.storage.update_todo(
                scope, str(todo_id or "").strip(), status=str(status or "").strip(),
                content=str(content or ""))
            if row is None:
                return f"没找到任务 {todo_id}（用 list 看 id；update 至少给 status 或 content）"
            return compact_json({"ok": True, "todo": row}, 1500)
        if act == "clear":
            removed = await self.storage.clear_todos(
                scope, done_only=str(status or "").strip().lower() in {"done", "completed"})
            return f"已清掉 {removed} 条任务"
        return "action 只能是 add / list / update / clear"

    async def _skill_tool_action(self, tool: str, args: Mapping[str, Any]) -> str:
        """learn_skill / update_skill 核心（供 llm_tool 与调度通道共用）。"""
        if self._scripts is None:
            return "拓展系统未就绪"
        if tool == "learn_skill":
            proposal = normalize_skill({
                "id": str(args.get("skill_id", "")),
                "display_name": str(args.get("display_name", "")) or str(args.get("skill_id", "")),
                "description": str(args.get("description", "")),
                "prompts": [
                    line.strip() for line in str(args.get("prompts", "")).splitlines()
                    if line.strip()
                ],
                "tools": parse_json_value(str(args.get("tools_json", "")) or "[]") or [],
                "code": str(args.get("code", "")),
                "permissions": parse_json_value(
                    str(args.get("permissions_json", "")) or "[]") or [],
            })
            if proposal is None:
                return ("技能参数不合法：检查 skill_id（小写字母/数字/中划线、2位以上）、"
                        "description 非空；code 必须能编译；声明了工具就必须实现 def call_tool")
            result = await apply_skill(self._script_manager(), proposal)
            if not result.get("ok"):
                return f"固化失败：{str(result.get('error'))[:200]}"
            await self._studio_note(
                f"[技能学习] {proposal['id']}：{proposal['description']}", "skill")
            return compact_json({
                "ok": True, "skill": result["id"], "tools": result.get("tools"),
                "hint": "已热加载并写入长期记忆，下次同类任务直接可用",
            }, 1200)
        # update_skill：只修自学习技能（内置/手写拓展受保护）
        skill_id = str(args.get("skill_id", "")).strip().lower()
        manager = self._script_manager()
        ext = manager.get(skill_id)
        if ext is None:
            return f"没有叫 {skill_id} 的技能（script_list 可看全部）"
        if getattr(ext, "origin", "builtin") != "learned":
            return f"{skill_id} 不是自学习技能（{ext.origin}），不能这样改"
        manifest = dict(ext.manifest)
        if str(args.get("description", "")).strip():
            manifest["description"] = str(args.get("description")).strip()[:300]
        add_prompts = [
            line.strip() for line in str(args.get("prompts", "")).splitlines()
            if line.strip()
        ]
        if add_prompts:
            manifest["prompts"] = (list(manifest.get("prompts") or []) + add_prompts)[:8]
        perms = parse_json_value(str(args.get("permissions_json", "")) or "[]")
        if isinstance(perms, list) and perms:
            manifest["permissions"] = [
                str(p) for p in perms
                if str(p) in {"llm", "memory", "net", "send", "program", "media", "ssh"}
            ]
        code = str(args.get("code", ""))
        if not code.strip():
            code_path = ext.folder / "extension.py"
            if code_path.exists():
                try:
                    code = await asyncio.to_thread(code_path.read_text, encoding="utf-8")
                except OSError:
                    code = ""
        if code.strip():
            try:
                compile(code, "<learned-skill-update>", "exec")
            except SyntaxError as error:
                return f"新版代码编译不过：{error}"
        result = await manager.create_extension(skill_id, manifest, code)
        if not result.get("ok"):
            return f"更新失败：{str(result.get('error'))[:200]}"
        return compact_json({"ok": True, "skill": skill_id,
                             "hint": "已更新并热加载"}, 800)

    @filter.llm_tool(name="manage_intent")
    async def manage_intent_tool(
        self, event: AstrMessageEvent, action: str, keywords: str = "",
        instruction: str = "", intent_id: str = "",
    ):
        """管理你的"事件条件意图"（standing intent）：像"当有人提到X时就做Y"。
        命中时系统会把指令注入你的上下文，你照做即可；适合许下"下次见到/提到某事就……"的承诺。

        Args:
            action(string): add(立一条)/list(列出)/remove(取消)。
            keywords(string): add 时触发关键词，逗号分隔（命中任意一个就触发）。
            instruction(string): add 时命中后要做的事（一句话指令）。
            intent_id(string): remove 时的意图 ID。
        """
        if not self.storage:
            raise RuntimeError("记忆系统未就绪")
        act = str(action or "").strip().lower()
        scope = await self._scope_for_event(event) if event is not None else None
        if act == "add":
            words = [w.strip() for w in str(keywords or "").replace("，", ",").split(",") if w.strip()]
            if not words or not str(instruction or "").strip():
                return "add 需要 keywords（逗号分隔）与 instruction"
            try:
                row = await self.storage.add_standing_intent(
                    scope, str(instruction), words)
            except ValueError as error:
                return f"创建失败：{error}"
            return compact_json({"ok": True, **row,
                                 "hint": "命中时指令会自动注入你的上下文"}, 1200)
        if act == "list":
            rows = await self.storage.list_standing_intents(
                self._shared_scope_ids(scope) if scope else None)
            return compact_json({"intents": rows}, 3000)
        if act == "remove":
            ok = await self.storage.cancel_standing_intent(str(intent_id or "").strip())
            return "已取消" if ok else f"没找到活跃的意图 {intent_id}"
        return "action 只能是 add/list/remove"

    @filter.llm_tool(name="todo")
    async def todo_tool(
        self, event: AstrMessageEvent, action: str, items_json: str = "",
        todo_id: str = "", status: str = "", content: str = "",
    ):
        """多步任务的显式任务表：活儿一多就先列出来，做一步标一步，跨轮也不会忘
        （未完成项每轮会注入你的上下文提醒你接着做）。

        Args:
            action(string): add 加任务 / list 查看 / update 改状态或内容 / clear 清理。
            items_json(string): add 时的任务数组JSON，如 ["查资料","写总结"]。
            todo_id(string): update 时的任务id（list 能看到）。
            status(string): update 时的新状态：pending/in_progress/completed/cancelled。
            content(string): update 时改写任务内容（可选）。
        """
        scope = await self._scope_for_event(event) if event is not None else None
        return await self._todo_action(scope, action, items_json, todo_id, status, content)

    @filter.llm_tool(name="learn_skill")
    async def learn_skill_tool(
        self, event: AstrMessageEvent, skill_id: str, description: str,
        prompts: str = "", code: str = "", tools_json: str = "",
        permissions_json: str = "",
    ):
        """把刚验证有效的一套流程固化成你自己的新技能（写完立刻热加载，下次同类任务直接可用）。
        复杂多步流程收尾、或用户纠正过你的做法时，值得固化；一件小事不值得。

        Args:
            skill_id(string): 技能英文短名（小写字母/数字/中划线），如 exam-paper-answers。
            description(string): 一句话：做什么 + 什么时候用。
            prompts(string): 给未来的你的操作说明（会注入你的上下文），一行一条。
            code(string): 可选Python代码；声明工具时必须实现 def call_tool(api, name, params)。
            tools_json(string): 可选工具声明JSON，如 [{"name":"xx","description":"...","params":{}}]。
            permissions_json(string): 可选权限数组JSON，如 ["ssh","media"]——代码里用到
                哪些宿主能力就必须声明（llm/memory/net/send/program/media/ssh），否则调用时报未声明权限。
        """
        return await self._skill_tool_action("learn_skill", {
            "skill_id": skill_id, "display_name": skill_id,
            "description": description, "prompts": prompts,
            "code": code, "tools_json": tools_json,
            "permissions_json": permissions_json,
        })

    @filter.llm_tool(name="update_skill")
    async def update_skill_tool(
        self, event: AstrMessageEvent, skill_id: str, code: str = "",
        description: str = "", prompts: str = "", permissions_json: str = "",
    ):
        """修补一个自学习技能（发现不好用/不完整时当回合改掉，立刻热加载生效）。
        只对 self-learned 技能有效（内置/手写拓展不能这样改）。

        Args:
            skill_id(string): 技能id（script_list 可见）。
            code(string): 新的完整 extension.py 源码（留空保留原代码）。
            description(string): 新的一句话说明（留空保留）。
            prompts(string): 追加的操作说明，一行一条。
            permissions_json(string): 可选权限数组JSON，如 ["ssh","media"]（给出则整体替换权限声明）。
        """
        return await self._skill_tool_action("update_skill", {
            "skill_id": skill_id, "code": code,
            "description": description, "prompts": prompts,
            "permissions_json": permissions_json,
        })

    @filter.llm_tool(name="get_user_style")
    async def get_user_style_tool(self, event: AstrMessageEvent, user_id: str):
        """查看某个人的说话风格档案（惯用词、句长、语气），便于模仿着跟TA聊天。

        Args:
            user_id(string): 对方QQ号。
        """
        if not self.storage:
            raise RuntimeError("记忆系统未就绪")
        scope = await self._scope_for_event(event)
        if not scope:
            return "当前会话未启用记忆"
        style = await self.storage.get_style(scope, user_id)
        return compact_json(style or {"note": "还没有TA的风格档案，聊几轮后自动学习"}, 1500)

    @filter.llm_tool(name="extension_list")
    async def extension_list_tool(self, event: AstrMessageEvent):
        """列出全部拓展（内置能力包 + 已装自定义拓展）及其工具与启停状态。"""
        if self._extensions is None:
            return "拓展系统未就绪"
        return compact_json({"extensions": self._extensions.list()}, 6000)

    @filter.llm_tool(name="extension_install")
    async def extension_install_tool(self, event: AstrMessageEvent, source: str):
        """安装一个自定义拓展：给一个 http(s) JSON 清单地址，或直接内联 JSON。

        清单格式：{"id": "weather", "name": "天气", "description": "...",
        "tools": [{"name": "get_weather", "description": "查天气",
        "method": "GET", "url": "https://api.example.com/w?city={city}",
        "params": ["city"]}]}。工具是声明式 HTTP 调用，不会执行任意代码。

        Args:
            source(string): JSON 清单的网址，或内联 JSON 文本。
        """
        if self._extensions is None:
            return "拓展系统未就绪"
        raw = str(source or "").strip()
        try:
            if raw.startswith("{"):
                data = parse_json_object(raw)
                origin = "inline"
            else:
                page = await self._browser_session().fetch(raw)
                data = parse_json_object(page.text)
                origin = page.url
            if not isinstance(data, dict):
                return "清单不是合法 JSON 对象"
            extension = self._extensions.install(data, source=origin)
            return compact_json({
                "ok": True, "id": extension.id, "name": extension.name,
                "tools": [t.name for t in extension.http_tools],
                "hint": "用 extension_call(extension, tool, args_json) 调用",
            }, 3000)
        except ExtensionError as error:
            return f"安装失败：{error}"
        except BrowserError as error:
            return f"下载清单失败：{error}"

    @filter.llm_tool(name="extension_enable")
    async def extension_enable_tool(self, event: AstrMessageEvent, extension_id: str):
        """启用一个拓展（内置能力包或自定义拓展）。

        Args:
            extension_id(string): 拓展 ID（extension_list 可查）。
        """
        if self._extensions is None:
            return "拓展系统未就绪"
        return ("已启用" if self._extensions.enable(str(extension_id))
                else f"没有拓展 {extension_id}")

    @filter.llm_tool(name="extension_disable")
    async def extension_disable_tool(self, event: AstrMessageEvent, extension_id: str):
        """停用一个拓展（其工具会从你的工具集移除）。

        Args:
            extension_id(string): 拓展 ID。
        """
        if self._extensions is None:
            return "拓展系统未就绪"
        return ("已停用" if self._extensions.disable(str(extension_id))
                else f"没有拓展 {extension_id}，或它是核心能力不可停用")

    @filter.llm_tool(name="extension_uninstall")
    async def extension_uninstall_tool(self, event: AstrMessageEvent, extension_id: str):
        """卸载一个自定义拓展（内置能力包不可卸载）。

        Args:
            extension_id(string): 拓展 ID。
        """
        if self._extensions is None:
            return "拓展系统未就绪"
        return ("已卸载" if self._extensions.uninstall(str(extension_id))
                else f"没有可卸载的自定义拓展 {extension_id}")

    @filter.llm_tool(name="extension_call")
    async def extension_call_tool(
        self, event: AstrMessageEvent, extension_id: str, tool: str, args_json: str = "{}"
    ):
        """调用某个已装自定义拓展的工具（URL 模板里的 {参数} 用 args 填）。

        Args:
            extension_id(string): 拓展 ID。
            tool(string): 工具名。
            args_json(string): 参数 JSON，例如 {"city": "北京"}。
        """
        if self._extensions is None:
            return "拓展系统未就绪"
        args = parse_json_object(args_json) or {}
        if not isinstance(args, dict):
            args = {}
        try:
            result = await self._extensions.call(
                str(extension_id), str(tool), {str(k): v for k, v in args.items()})
        except ExtensionError as error:
            return f"调用失败：{error}"
        except Exception as error:
            return f"调用失败：{type(error).__name__}: {str(error)[:160]}"
        return compact_json({"ok": True, "result": result[:6000]}, 8000)

    @filter.llm_tool(name="web_search")
    async def web_search_tool(self, event: AstrMessageEvent, query: str):
        """搜索互联网（好奇、查新闻、查资料）。

        Args:
            query(string): 搜索关键词。
        """
        results = await search_web(query)
        return (compact_json(results, 10000))

    @filter.llm_tool(name="web_fetch")
    async def web_fetch_tool(self, event: AstrMessageEvent, url: str):
        """抓取一个公开网页的正文文本；文件过大会拒绝。

        Args:
            url(string): 网页URL，仅http(s)。
        """
        result = await fetch_text(url, max_bytes=self.settings.web_fetch_max_mb * 1024 * 1024)
        return (compact_json(result, 16000))

    @filter.llm_tool(name="media_recent")
    async def media_recent_tool(self, event: AstrMessageEvent, limit: int = 15):
        """查看本地媒体归档（图片、文件、链接），返回ID和路径。

        Args:
            limit(number): 条数，最大100。
        """
        if not self.media:
            raise RuntimeError("媒体归档未启用")
        scope = await self._scope_for_event(event)
        items = await self.media.recent(scope, min(max(int(limit), 1), 100))
        return (compact_json(items, 14000))

    @filter.llm_tool(name="media_fetch_url")
    async def media_fetch_url_tool(self, event: AstrMessageEvent, url: str):
        """好奇下载用户发过的链接到本地归档（文件过大则只记录不下载）。

        Args:
            url(string): 要下载的URL。
        """
        if not self.media:
            raise RuntimeError("媒体归档未启用")
        scope = await self._scope_for_event(event)
        record = await self.media.ingest_url(url, scope_id=scope or "", note="llm curiosity")
        return (compact_json(record.to_dict(), 4000))

    @filter.llm_tool(name="schedule_task")
    async def schedule_task_tool(
        self,
        event: AstrMessageEvent,
        name: str,
        prompt: str,
        delay_minutes: int = 0,
        daily_hour: int = -1,
        interval_minutes: int = 0,
    ):
        """创建一个由自己执行的定时任务（如每天看新闻、定时发说说）。

        Args:
            name(string): 任务名。
            prompt(string): 任务说明，告诉未来的自己要做什么。
            delay_minutes(number): 一次性的延迟分钟数。
            daily_hour(number): 每天执行的小时(0-23)。
            interval_minutes(number): 每隔多少分钟循环执行（最小5）。
        """
        if not self.scheduler:
            raise RuntimeError("定时任务未启用")
        group_id = event.get_group_id() or ""
        task = await self.scheduler.add(
            name, prompt, group_id=group_id, scope_key=str(event.get_self_id()),
            delay_minutes=int(delay_minutes) if delay_minutes else None,
            daily_hour=int(daily_hour) if daily_hour is not None and daily_hour >= 0 else None,
            interval_minutes=int(interval_minutes) if interval_minutes else None,
        )
        if self.task_runner:
            self.task_runner.poke()
        return (compact_json(task.to_dict(), 4000))

    @filter.llm_tool(name="list_scheduled")
    async def list_scheduled_tool(self, event: AstrMessageEvent):
        """列出自己创建的定时任务。"""
        if not self.scheduler:
            raise RuntimeError("定时任务未启用")
        return (compact_json(self.scheduler.list(), 8000))

    @filter.llm_tool(name="cancel_scheduled")
    async def cancel_scheduled_tool(self, event: AstrMessageEvent, task_id: str):
        """取消一个定时任务。

        Args:
            task_id(string): 任务ID。
        """
        if not self.scheduler:
            raise RuntimeError("定时任务未启用")
        ok = await self.scheduler.cancel(task_id)
        return ("已取消" if ok else "任务不存在")

    @filter.llm_tool(name="set_mood")
    async def set_mood_tool(self, event: AstrMessageEvent, mood: str, intensity: float = 0.3, note: str = ""):
        """更新自己的当前情绪状态，会影响后续语气。

        Args:
            mood(string): 情绪词，如开心、烦躁、平静。
            intensity(number): 强度0-1。
            note(string): 一句话说明原因。
        """
        if not self.mood:
            raise RuntimeError("情绪模块未就绪")
        return (compact_json(
            self.mood.set(mood, float(intensity), note).as_dict(), 2000))

    @filter.command_group("longmem")
    def longmem(self):
        """长程记忆管理。"""

    @longmem.command("status")
    async def command_status(self, event: AstrMessageEvent):
        scope = await self._scope_for_event(event)
        payload = {
            "enabled": self.settings.enabled,
            "group_allowed": self._target_group(event),
            "scope_ready": bool(scope),
            "sticker_learning": self.settings.sticker_learning,
            "dashboard_management": bool(self.settings.dashboard_api_key),
            "background_tasks": len(self._tasks),
        }
        yield _plain_result(event, compact_json(payload))

    @longmem.command("stats")
    async def command_stats(self, event: AstrMessageEvent):
        await self._require_admin_group(event)
        scope = await self._scope_for_event(event)
        if not self.storage or not scope:
            yield _plain_result(event, "当前群还没有记忆数据。")
            return
        yield _plain_result(event, compact_json(await self.storage.stats(scope)))

    @longmem.command("compress")
    async def command_compress(self, event: AstrMessageEvent):
        await self._require_admin_group(event)
        scope = await self._scope_for_event(event)
        if not self.compression or not scope:
            yield _plain_result(event, "当前群没有可压缩数据。")
            return
        record = await self.compression.compress_pending_l1(
            scope, self.settings.compression_batch_size
        )
        yield _plain_result(event, "没有待压缩消息。" if not record else f"已生成 L1 摘要 {record.summary_id}")

    @longmem.command("backup")
    async def command_backup(self, event: AstrMessageEvent):
        await self._require_admin_group(event)
        if not self.storage:
            raise RuntimeError("存储尚未就绪")
        directory = self.storage.path.parent / "backups"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = await self.storage.backup(directory / f"memory-{stamp}.db")
        yield _plain_result(event, f"备份已写入 {path.name}")

    @longmem.command("delete")
    async def command_delete(self, event: AstrMessageEvent, arguments: GreedyStr):
        await self._require_admin_group(event)
        if not self.storage:
            raise RuntimeError("存储尚未就绪")
        try:
            args = shlex.split(str(arguments))
        except ValueError:
            args = str(arguments).split()
        if len(args) < 1:
            yield _plain_result(event, "用法: /longmem delete message|user|range|group|all ...")
            return
        scope = await self._scope_for_event(event)
        kind = args[0].lower()
        if kind == "message" and len(args) == 2 and scope:
            count = await self.storage.delete_message(scope, args[1])
        elif kind == "user" and len(args) == 2 and scope:
            count = await self.storage.delete_user(args[1], scope)
        elif kind == "range" and len(args) == 3 and scope:
            count = await self.storage.delete_range(args[1], args[2], scope)
        elif kind == "group" and len(args) == 2:
            target = await self.storage.resolve_scope("aiocqhttp", str(event.get_self_id()), args[1])
            count = await self.storage.delete_group(target) if target else 0
        elif kind == "all" and len(args) == 1:
            count = await self.storage.delete_all()
        else:
            yield _plain_result(event, "删除参数无效；为避免误删，本次未执行。")
            return
        yield _plain_result(event, f"删除完成，受影响消息数：{count}")

    @longmem.command("plugins")
    async def command_plugins(self, event: AstrMessageEvent, arguments: GreedyStr):
        await self._require_admin_group(event)
        if not self.dashboard:
            raise RuntimeError("Dashboard未配置")
        try:
            args = shlex.split(str(arguments))
        except ValueError:
            args = str(arguments).split()
        if not args or args[0] == "list":
            result = await self.dashboard.list_plugins()
        elif args[0] == "search" and len(args) > 1:
            result = await self.dashboard.search_market(" ".join(args[1:]))
        elif len(args) == 2 and args[0] in {"install", "update", "enable", "disable", "reload"}:
            methods = {
                "install": self.dashboard.install_market_plugin,
                "update": self.dashboard.update_plugin,
                "enable": self.dashboard.enable_plugin,
                "disable": self.dashboard.disable_plugin,
                "reload": self.dashboard.reload_plugin,
            }
            result = await methods[args[0]](args[1])
        else:
            yield _plain_result(event, "插件子命令参数无效。")
            return
        yield _plain_result(event, compact_json(result, 16000))



    @longmem.command("skills")
    async def command_skills(self, event: AstrMessageEvent, arguments: GreedyStr):
        await self._require_admin_group(event)
        if not self.dashboard or not self.skills:
            raise RuntimeError("Dashboard未配置")
        try:
            args = shlex.split(str(arguments))
        except ValueError:
            args = str(arguments).split()
        if not args or args[0] == "list":
            result = await self.dashboard.list_skills()
        elif args[0] == "show" and len(args) == 2:
            result = await self.skills.read(args[1])
        elif args[0] in {"enable", "disable"} and len(args) == 2:
            result = await getattr(self.skills, args[0])(args[1])
        else:
            yield _plain_result(event, "Skill子命令参数无效。创建和更新请通过管理员直接指令调用Agent工具。")
            return
        yield _plain_result(event, compact_json(result, 16000) if not isinstance(result, str) else result[:16000])

    async def _require_admin_group(self, event: AstrMessageEvent) -> None:
        if not self._target_group(event):
            raise PermissionError("仅可在已启用的aiocqhttp白名单群中使用")
        admin = event.is_admin()
        if asyncio.iscoroutine(admin):
            admin = await admin
        if not admin:
            raise PermissionError("此操作仅限AstrBot管理员")


class _AstrBotEmbeddingAdapter:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self.provider.get_embeddings(texts)


def _is_image_only(raw: dict[str, Any], text: str) -> bool:
    """True when the message is an image/sticker without meaningful text."""
    if text not in ("", "[图片]", "[动画表情]", "[表情]"):
        return False
    message = raw.get("message")
    if not isinstance(message, list):
        return False
    return any(
        isinstance(part, dict) and part.get("type") in {"image", "mface"}
        for part in message
    )


# 画图/设计请求（确定性直通）：命中就不再指望 agent 自觉调 design_render——
# 巨大工具集+超长提示词下弱模型/强模型的工具调用都会失效（实录：判定=agent
# 却一个工具都没调、直接出了纯文本回复）。走"LLM 出 SVG→渲染→截图→发送"。
_DRAW_REQUEST_RE = re.compile(
    r"画(一?张|个|一下)?|绘制|来(一?张|个)?图|设计(一?张|个|一下)?(海报|头像|图标|logo|表情)"
    r"|生成(一?张|个)?(图片|图像|图)|鹦鹉|鹈鹕|骑士|骑(着|个)?",
)


_BROWSER_TOOL_NAMES = frozenset({
    "browse", "browser_links", "browser_click", "browser_form", "browser_back",
    "browser_forward", "browser_tabs", "browser_new_tab", "browser_switch_tab",
    "browser_close_tab", "browser_find", "browser_cookies", "browser_dom",
    "browser_click_element", "browser_type_text", "browser_screenshot",
    "browser_grid_shot", "browser_slide", "browser_click_xy",
    "browser_drag_element", "browser_scroll",
})
# 子agent（执行型）：除浏览器外全部可用；唯一结构性例外是不许再派子agent（防递归爆炸）
_SUBAGENT_BLOCKED_TOOLS = _BROWSER_TOOL_NAMES | {
    "dispatch_subagent", "dispatch_parallel_subagents"}
# 需求分析子agent（决策层）：只读核查——一切发送/写入/执行类工具都不给
_ANALYZER_BLOCKED_TOOLS = _SUBAGENT_BLOCKED_TOOLS | {
    "send_image", "send_local_file", "send_sticker", "say_now", "ask_user",
    "forward_messages", "screenshot_messages",  # read_forward 是只读，放行
    "qq_send_group", "qq_send_private", "qzone_publish", "napcat_call",
    "ssh_exec", "ssh_fetch", "ssh_screenshot", "ssh_agent_dispatch",
    "ssh_agent_control", "python_exec", "read_tabular", "run_script",
    "fs_write", "fs_delete", "fs_move", "learn_skill", "update_skill",
    "schedule_task", "cancel_scheduled", "todo", "set_mood", "manage_intent",
    "program_write", "program_archive", "extension_call", "script_call",
    "adjust_affinity", "update_impression", "remember", "add_plan", "drop_plan",
    "render_code",
}

# find_tools 的中英关键词映射：模型多用中文问，而工具名/描述常是英文
_TOOL_QUERY_ALIASES: dict[str, tuple[str, ...]] = {
    "知识库": ("kb", "knowledge", "rag", "corpus"),
    "搜索": ("search", "web", "query"),
    "联网": ("web", "search", "fetch", "http"),
    "网页": ("web", "browse", "page", "fetch", "url"),
    "图片": ("image", "img", "photo", "picture", "vision"),
    "看图": ("image", "vision", "look", "describe"),
    "文件": ("file", "document", "attachment"),
    "文档": ("document", "pdf", "read", "extract"),
    "群": ("group",),
    "好友": ("friend", "stranger"),
    "记忆": ("memory", "recall", "summary", "catalog", "history"),
    "定时": ("schedule", "cron", "task", "plan", "remind"),
    "发消息": ("send", "message", "say"),
    "空间": ("qzone", "feed"),
    "表情": ("sticker", "face", "emoji", "meme"),
    "服务器": ("ssh", "server", "remote", "shell"),
    "浏览器": ("browser", "browse", "page"),
    "代码": ("code", "python", "exec", "script", "run"),
    "画图": ("draw", "design", "svg", "chart", "image"),
    "表格": ("table", "csv", "excel", "pandas", "tabular"),
    "转发": ("forward", "share"),
    "拓展": ("extension", "script", "plugin"),
}


def _tool_query_tokens(query: str) -> list[str]:
    """把查询词展开成匹配标记（原词 + 中英别名）。"""
    keyword = str(query or "").strip().lower()
    if not keyword:
        return []
    tokens = [keyword]
    for chinese, aliases in _TOOL_QUERY_ALIASES.items():
        if chinese in keyword or keyword in chinese:
            tokens.extend(aliases)
    return tokens


def _forward_node_name(node: dict[str, Any]) -> str:
    """从转发节点里取发送者昵称（兼容发送侧 node 与事件侧 message 两种形态）。"""
    sender = node.get("sender")
    if isinstance(sender, dict):
        for key in ("card", "nickname", "name"):
            value = str(sender.get(key) or "").strip()
            if value:
                return value
    for key in ("nickname", "name"):
        value = str(node.get(key) or "").strip()
        if value:
            return value
    data = node.get("data")
    if isinstance(data, dict):
        for key in ("nickname", "name"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    for key in ("user_id", "uin"):
        value = str(node.get(key) or "").strip()
        if value:
            return value
    return "某人"


def _looks_like_server_status_request(text: str) -> bool:
    """服务器状态类主题识别（draw_picture 内部用它决定要不要抓真指标——
    是数据增强不是路由；v0.36.0 起任务类别路由已全部移除）。"""
    cleaned = str(text or "")
    if not re.search(r"服务器|服务端|主机|机器|vps|server|ssh|监控|cpu|内存|磁盘|硬盘",
                     cleaned, re.IGNORECASE):
        return False
    return bool(re.search(r"状态|情况|监控|健康|负载|性能|资源|占用|dashboard|面板",
                          cleaned, re.IGNORECASE))


def _deletion_payload(result: Any, extra_files: int = 0) -> dict[str, Any]:
    """JSON view of a DeletionResult: the removed count plus media files cleaned."""
    return {
        "removed": int(result),
        "media_files": len(tuple(getattr(result, "media_paths", ()) or ())) + extra_files,
    }


def _public_value(value: Any) -> Any:
    """Plain-JSON-able view of adataclass / dict-like values.

    Ordinary mappings must pass through untouched: rewriting them through
    `__dict__` drops their keys (which broke the plugin page payloads).
    """
    if isinstance(value, Mapping):
        return timeutil.localize_fields(dict(value))
    if isinstance(value, (list, tuple, set)):
        return [_public_value(item) for item in value]
    slots = getattr(type(value), "__slots__", ())
    if slots:
        row = {name: _public_value(getattr(value, name))
               for name in slots if hasattr(value, name)}
        return timeutil.localize_fields(row)
    if hasattr(value, "__dict__"):
        return _public_value(dict(value.__dict__))
    return value


class _RequestView:
    def __init__(self, context: Context) -> None:
        manager = context.get_llm_tool_manager()
        self.func_tool = manager.get_full_tool_set()


__MAIN_CONTINUED__ = True
