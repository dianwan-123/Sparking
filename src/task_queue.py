# -*- coding: utf-8 -*-
"""统一任务与日程系统（完全重写版）。

用户要求：把**所有事情都抽象成任务**——一次回复、一轮 agent 任务、一次工具调用、
一条主动通知、自我总结…；任务**队列式处理**；bot 可以自己给自己加任务/日程规划；
任务分**短期**（执行 1 次或几次）与**长期**（定时反复执行），**都可以设时间区间，
超出区间不执行**；一切都要有详细记录（谁加的、跑了多久、结果、错误、下次何时）。

设计：
- 一张表 `tasks` 承载一切（结构化字段见 `Task`），**队列语义**：按
  `priority DESC, next_run_at ASC, created_at ASC` 取件；
- 取件用 `BEGIN IMMEDIATE` 抢占（`status: pending → running`），多实例/多协程安全；
- **时间区间**两种：绝对区间 `{after, before}`（超出即过期不执行）与每日时段
  `{daily: ["09:00","22:00"]}`（不在时段内就把下次执行推到下一个窗口起点）；
- **短期/长期**统一由 `max_runs` + `interval_seconds` 表达：
  `max_runs=1` 一次性、`max_runs=3` 跑三次、`interval_seconds>0` 定时反复（`max_runs=-1` 无限）；
- 失败带**指数退避重试**（≤3 次）与 `last_error` 记录；一切变更落 `updated_at`；
- 执行体由宿主注入（`handlers: {kind: callable}`），本模块不依赖插件内部。

**自定义触发条件（v1.0.7 新增）**：任务除了"到点"，还能带一段**判定代码**
（`condition`）。到点后先把条件交给宿主求值，为真才执行、为假就顺延再试——
这样 bot 可以自己写"等某个群安静 30 分钟再说话""只在有人在线时执行"这类条件。
本模块只负责"什么时候问条件"，代码怎么跑由宿主注入（`condition_check`）。
"""
from __future__ import annotations

from .timeutil import zone as _zone

import asyncio
import inspect
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

# 任务类型（bot 与用户都能建）
KIND_REPLY = "reply"        # 一次回复：对着某条消息/某段指令说一句话
KIND_AGENT = "agent"        # 一轮 agent 任务：带工具循环自主完成
KIND_TOOL = "tool"          # 一次工具调用：tool + args
KIND_NOTIFY = "notify"      # 主动发一条消息到会话
KIND_REFLECT = "reflect"    # 自我总结
KIND_COMPRESS = "compress"  # 压缩记忆
KIND_CUSTOM = "custom"      # 拓展脚本自定义
ALL_KINDS = (KIND_REPLY, KIND_AGENT, KIND_TOOL, KIND_NOTIFY, KIND_REFLECT,
             KIND_COMPRESS, KIND_CUSTOM)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (60, 300, 900)

# 队列式：同一时刻最多跑几个（跨会话），以及每会话同时最多一个
DEFAULT_CONCURRENCY = 2
DEFAULT_TICK_SECONDS = 15


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    text = str(value or "").strip()
    if ":" not in text:
        return None
    try:
        hour, minute = (int(part) for part in text.split(":", 1))
    except ValueError:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


@dataclass
class Task:
    """一个任务（一次回复、一轮 agent、一条定时日程…统一如此）。"""

    task_id: str
    kind: str
    title: str = ""
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    scope_id: str = ""
    conversation: str = ""
    priority: int = 0
    status: str = STATUS_PENDING
    source: str = "bot"                    # bot / user / system
    created_at: str = ""
    updated_at: str = ""
    next_run_at: str = ""
    last_run_at: str = ""
    finished_at: str = ""
    interval_seconds: int = 0              # 0=不重复；>0=长期定时
    max_runs: int = 1                      # -1=无限
    runs_done: int = 0
    attempts: int = 0
    last_error: str = ""
    last_result: str = ""
    window: dict[str, Any] = field(default_factory=dict)
    # window: {"after": iso, "before": iso, "daily": ["09:00", "22:00"]}
    condition: str = ""                    # 自定义触发条件（宿主求值的判定代码）
    condition_checks: int = 0              # 条件判假顺延了多少次（面板可见）

    @property
    def is_long_term(self) -> bool:
        return self.interval_seconds > 0 or self.max_runs < 0 or self.max_runs > 1

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["long_term"] = self.is_long_term
        data["runs_left"] = ("∞" if self.max_runs < 0
                             else max(0, self.max_runs - self.runs_done))
        data["has_condition"] = bool(self.condition.strip())
        return data


def in_window(window: Mapping[str, Any] | None, moment: datetime | None = None) -> bool:
    """当前是否处在任务允许执行的**时间区间**内。"""
    if not window:
        return True
    now = moment or datetime.now(timezone.utc)
    after = _parse_dt(window.get("after"))
    before = _parse_dt(window.get("before"))
    if after and now < after:
        return False
    if before and now > before:
        return False
    daily = window.get("daily") or []
    if isinstance(daily, (list, tuple)) and len(daily) == 2:
        start = _parse_hhmm(str(daily[0]))
        end = _parse_hhmm(str(daily[1]))
        if start and end:
            local = now.astimezone(_zone())
            minutes = local.hour * 60 + local.minute
            lower = start[0] * 60 + start[1]
            upper = end[0] * 60 + end[1]
            if lower <= upper:
                if not (lower <= minutes <= upper):
                    return False
            elif not (minutes >= lower or minutes <= upper):  # 跨天时段
                return False
    return True


def next_window_start(window: Mapping[str, Any] | None,
                      moment: datetime | None = None) -> datetime | None:
    """不在区间内时，算出下一个可执行时刻（用于把 next_run_at 往后推）。"""
    if not window:
        return None
    now = moment or datetime.now(timezone.utc)
    after = _parse_dt(window.get("after"))
    if after and now < after:
        return after
    before = _parse_dt(window.get("before"))
    if before and now > before:
        return None  # 区间已过：任务作废
    daily = window.get("daily") or []
    if isinstance(daily, (list, tuple)) and len(daily) == 2:
        start = _parse_hhmm(str(daily[0]))
        if start:
            local = now.astimezone(_zone())
            target = local.replace(hour=start[0], minute=start[1], second=0, microsecond=0)
            if target <= local:
                target = target + timedelta(days=1)
            return target.astimezone(timezone.utc)
    return None


def window_expired(window: Mapping[str, Any] | None,
                   moment: datetime | None = None) -> bool:
    """绝对区间的截止时间已过 → 任务应当作废（不再执行）。"""
    if not window:
        return False
    before = _parse_dt(window.get("before"))
    return bool(before and (moment or datetime.now(timezone.utc)) > before)


class TaskQueue:
    """任务队列：建/取/完结/查询。全部落 SQLite（`tasks` 表），跨重启不丢。"""

    TABLE_SQL = (
        "CREATE TABLE IF NOT EXISTS tasks("
        "task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',"
        "detail TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',"
        "scope_id TEXT NOT NULL DEFAULT '', conversation TEXT NOT NULL DEFAULT '',"
        "priority INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',"
        "source TEXT NOT NULL DEFAULT 'bot', created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL, next_run_at TEXT NOT NULL DEFAULT '',"
        "last_run_at TEXT NOT NULL DEFAULT '', finished_at TEXT NOT NULL DEFAULT '',"
        "interval_seconds INTEGER NOT NULL DEFAULT 0, max_runs INTEGER NOT NULL DEFAULT 1,"
        "runs_done INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,"
        "last_error TEXT NOT NULL DEFAULT '', last_result TEXT NOT NULL DEFAULT '',"
        "window_json TEXT NOT NULL DEFAULT '{}',"
        "condition_text TEXT NOT NULL DEFAULT '',"
        "condition_checks INTEGER NOT NULL DEFAULT 0)"
    )
    # 老库补列（SQLite 的 ADD COLUMN 是幂等的：重复加会报错，忽略即可）
    MIGRATE_SQL = (
        "ALTER TABLE tasks ADD COLUMN condition_text TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tasks ADD COLUMN condition_checks INTEGER NOT NULL DEFAULT 0",
    )
    INDEX_SQL = ("CREATE INDEX IF NOT EXISTS idx_tasks_queue "
                 "ON tasks(status, next_run_at, priority)")

    def __init__(self, storage: Any, *, concurrency: int = DEFAULT_CONCURRENCY) -> None:
        self.storage = storage
        self.concurrency = max(1, int(concurrency))
        self._running: set[str] = set()

    async def ensure_table(self) -> None:
        db = self.storage._conn()
        await db.execute(self.TABLE_SQL)
        for statement in self.MIGRATE_SQL:
            try:
                await db.execute(statement)
            except Exception:
                pass          # 列已存在
        await db.execute(self.INDEX_SQL)
        await db.commit()

    # ------------------------------------------------------------ 写入
    async def add(
        self, kind: str, *, detail: str = "", title: str = "", scope_id: str = "",
        conversation: str = "", payload: Mapping[str, Any] | None = None,
        priority: int = 0, run_at: Any = None, delay_seconds: float = 0,
        interval_seconds: int = 0, max_runs: int = 1,
        window: Mapping[str, Any] | None = None, source: str = "bot",
        condition: str = "",
    ) -> Task:
        kind = str(kind or KIND_CUSTOM).strip().lower()
        if kind not in ALL_KINDS:
            kind = KIND_CUSTOM
        moment = _parse_dt(run_at)
        if moment is None:
            moment = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, delay_seconds))
        task = Task(
            task_id=f"t-{uuid.uuid4().hex[:16]}",
            kind=kind, title=str(title)[:200], detail=str(detail)[:2000],
            payload=dict(payload or {}), scope_id=str(scope_id), conversation=str(conversation),
            priority=max(-5, min(5, int(priority))), source=str(source)[:20],
            created_at=_now(), updated_at=_now(), next_run_at=moment.isoformat(),
            interval_seconds=max(0, int(interval_seconds)),
            max_runs=int(max_runs), window=dict(window or {}),
            condition=str(condition or "")[:2000],
        )
        db = self.storage._conn()
        async with self.storage._write_lock:
            await db.execute(
                "INSERT INTO tasks(task_id,kind,title,detail,payload_json,scope_id,"
                "conversation,priority,status,source,created_at,updated_at,next_run_at,"
                "interval_seconds,max_runs,window_json,condition_text) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task.task_id, task.kind, task.title, task.detail,
                 json.dumps(task.payload, ensure_ascii=False), task.scope_id,
                 task.conversation, task.priority, task.status, task.source,
                 task.created_at, task.updated_at, task.next_run_at,
                 task.interval_seconds, task.max_runs,
                 json.dumps(task.window, ensure_ascii=False), task.condition))
            await db.commit()
        return task

    async def claim_due(self, *, limit: int | None = None) -> list[Task]:
        """抢占到期任务（队列式：优先级高、到点早的在前）。"""
        budget = int(limit or self.concurrency)
        room = max(0, self.concurrency - len(self._running))
        budget = min(budget, room)
        if budget <= 0:
            return []
        now = _now()
        db = self.storage._conn()
        claimed: list[Task] = []
        async with self.storage._write_lock:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT * FROM tasks WHERE status=? AND next_run_at<=? "
                    "ORDER BY priority DESC, next_run_at ASC, created_at ASC LIMIT ?",
                    (STATUS_PENDING, now, budget * 3))
                rows = await cursor.fetchall()
                for row in rows:
                    if len(claimed) >= budget:
                        break
                    task = self._row_to_task(row)
                    if window_expired(task.window):
                        await db.execute(
                            "UPDATE tasks SET status=?, updated_at=?, finished_at=? "
                            "WHERE task_id=?",
                            (STATUS_EXPIRED, _now(), _now(), task.task_id))
                        continue
                    if not in_window(task.window):
                        target = next_window_start(task.window)
                        if target is None:
                            await db.execute(
                                "UPDATE tasks SET status=?, updated_at=?, finished_at=? "
                                "WHERE task_id=?",
                                (STATUS_EXPIRED, _now(), _now(), task.task_id))
                            continue
                        await db.execute(
                            "UPDATE tasks SET next_run_at=?, updated_at=? WHERE task_id=?",
                            (target.isoformat(), _now(), task.task_id))
                        continue
                    if task.scope_id and any(
                            item.scope_id == task.scope_id for item in claimed):
                        continue  # 同一会话一次只跑一个（队列语义）
                    if task.condition.strip():
                        # 自定义条件（bot 自己写的判定代码）：为真才执行，为假顺延再问
                        ready = await self._condition_ready(task)
                        if not ready:
                            await db.execute(
                                "UPDATE tasks SET next_run_at=?, condition_checks="
                                "condition_checks+1, updated_at=? WHERE task_id=?",
                                (self._condition_retry_at(task), _now(), task.task_id))
                            continue
                    await db.execute(
                        "UPDATE tasks SET status=?, updated_at=?, last_run_at=? "
                        "WHERE task_id=?",
                        (STATUS_RUNNING, _now(), _now(), task.task_id))
                    task.status = STATUS_RUNNING
                    claimed.append(task)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        for task in claimed:
            self._running.add(task.task_id)
        return claimed

    def _condition_retry_at(self, task: Task) -> str:
        """条件没满足时，下次什么时候再问：取 min(间隔, 15 分钟)，最少 1 分钟。"""
        gap = int(task.interval_seconds) if task.interval_seconds > 0 else 900
        gap = max(60, min(gap, 900))
        return (datetime.now(timezone.utc) + timedelta(seconds=gap)).isoformat()

    async def _condition_ready(self, task: Task) -> bool:
        """问宿主"这个自定义条件满足了吗"；没接条件求值器就当作满足（老行为）。"""
        checker = getattr(self, "condition_check", None)
        if checker is None:
            return True
        try:
            result = checker(task)
            if inspect.isawaitable(result):
                result = await result
        except Exception as error:
            self.condition_error = f"{type(error).__name__}: {error}"[:200]
            return False
        return bool(result)

    async def finish(self, task: Task, *, result: str = "") -> Task:
        """任务跑完：一次性→done；多次/定时→重排下一次（超次数或区间外→结束）。"""
        self._running.discard(task.task_id)
        runs_done = task.runs_done + 1
        finished = runs_done >= task.max_runs >= 0
        if not finished and task.interval_seconds > 0:
            nxt = datetime.now(timezone.utc) + timedelta(seconds=task.interval_seconds)
        elif not finished:
            nxt = None            # 多次任务但没给间隔：立刻可再跑（由调用方决定节奏）
        else:
            nxt = None
        next_iso = nxt.isoformat() if nxt else ""
        if next_iso and task.window and not in_window(task.window, nxt):
            shifted = next_window_start(task.window, nxt)
            if shifted is None:
                finished = True
                next_iso = ""
            else:
                next_iso = shifted.isoformat()
        status = STATUS_DONE if finished else STATUS_PENDING
        db = self.storage._conn()
        async with self.storage._write_lock:
            await db.execute(
                "UPDATE tasks SET status=?, updated_at=?, runs_done=?, next_run_at=?, "
                "attempts=0, last_error='', last_result=?, finished_at=? WHERE task_id=?",
                (status, _now(), runs_done, next_iso, str(result)[:500],
                 _now() if finished else "", task.task_id))
            await db.commit()
        task.status = status
        task.runs_done = runs_done
        task.next_run_at = next_iso
        task.last_result = str(result)[:500]
        return task

    async def fail(self, task: Task, error: str) -> Task:
        """失败：退避重试（≤3 次），仍失败则彻底失败并记原因。"""
        self._running.discard(task.task_id)
        attempts = task.attempts + 1
        fatal = attempts >= MAX_ATTEMPTS
        delay = RETRY_BACKOFF_SECONDS[min(attempts, len(RETRY_BACKOFF_SECONDS)) - 1]
        nxt = ("", datetime.now(timezone.utc) + timedelta(seconds=delay))
        status = STATUS_FAILED if fatal else STATUS_PENDING
        db = self.storage._conn()
        async with self.storage._write_lock:
            await db.execute(
                "UPDATE tasks SET status=?, updated_at=?, attempts=?, last_error=?, "
                "next_run_at=?, finished_at=? WHERE task_id=?",
                (status, _now(), attempts, str(error)[:500], nxt[1].isoformat(),
                 _now() if fatal else "", task.task_id))
            await db.commit()
        task.status = status
        task.attempts = attempts
        task.last_error = str(error)[:500]
        task.next_run_at = nxt[1].isoformat()
        return task

    async def cancel(self, task_id: str) -> bool:
        db = self.storage._conn()
        async with self.storage._write_lock:
            cursor = await db.execute(
                "UPDATE tasks SET status=?, updated_at=?, finished_at=? "
                "WHERE task_id=? AND status IN (?,?)",
                (STATUS_CANCELLED, _now(), _now(), str(task_id),
                 STATUS_PENDING, STATUS_RUNNING))
            await db.commit()
        self._running.discard(str(task_id))
        return bool(cursor.rowcount)

    async def update(self, task_id: str, **changes: Any) -> bool:
        allowed = {"title", "detail", "priority", "interval_seconds", "max_runs",
                   "next_run_at", "scope_id", "conversation", "source"}
        fields = []
        values: list[Any] = []
        for key, value in changes.items():
            if key not in allowed or value is None:
                continue
            fields.append(f"{key}=?")
            values.append(value)
        if "window" in changes and isinstance(changes["window"], Mapping):
            fields.append("window_json=?")
            values.append(json.dumps(changes["window"], ensure_ascii=False))
        if "condition" in changes and changes["condition"] is not None:
            fields.append("condition_text=?")
            values.append(str(changes["condition"])[:2000])
        if not fields:
            return False
        fields.append("updated_at=?")
        values.append(_now())
        values.append(str(task_id))
        db = self.storage._conn()
        async with self.storage._write_lock:
            cursor = await db.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE task_id=?", tuple(values))
            await db.commit()
        return bool(cursor.rowcount)

    # ------------------------------------------------------------ 读取
    async def list(self, *, statuses: Sequence[str] | None = None,
                   limit: int = 50, scope_id: str = "") -> list[Task]:
        clauses = []
        args: list[Any] = []
        if statuses:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            args.extend(statuses)
        if scope_id:
            clauses.append("scope_id=?")
            args.append(scope_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(max(1, min(int(limit), 200)))
        db = self.storage._conn()
        cursor = await db.execute(
            "SELECT * FROM tasks" + where +
            " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,"
            " next_run_at ASC, priority DESC LIMIT ?", tuple(args))
        rows = await cursor.fetchall()
        await cursor.close()
        return [self._row_to_task(row) for row in rows]

    async def stats(self) -> dict[str, int]:
        db = self.storage._conn()
        cursor = await db.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        rows = await cursor.fetchall()
        await cursor.close()
        out = {status: 0 for status in
               (STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED,
                STATUS_CANCELLED, STATUS_EXPIRED)}
        for row in rows:
            out[str(row["status"])] = int(row["n"])
        return out

    @staticmethod
    def _row_to_task(row: Any) -> Task:
        def get(name: str, default: Any = "") -> Any:
            try:
                return row[name]
            except (IndexError, KeyError):
                return default

        def load(raw: Any, fallback: Any) -> Any:
            try:
                value = json.loads(str(raw or ""))
                return value if isinstance(value, type(fallback)) else fallback
            except Exception:
                return fallback

        return Task(
            task_id=str(get("task_id")), kind=str(get("kind")),
            title=str(get("title")), detail=str(get("detail")),
            payload=load(get("payload_json"), {}),
            scope_id=str(get("scope_id")), conversation=str(get("conversation")),
            priority=int(get("priority", 0) or 0), status=str(get("status")),
            source=str(get("source")), created_at=str(get("created_at")),
            updated_at=str(get("updated_at")), next_run_at=str(get("next_run_at")),
            last_run_at=str(get("last_run_at")), finished_at=str(get("finished_at")),
            interval_seconds=int(get("interval_seconds", 0) or 0),
            max_runs=int(get("max_runs", 1) or 0), runs_done=int(get("runs_done", 0) or 0),
            attempts=int(get("attempts", 0) or 0), last_error=str(get("last_error")),
            last_result=str(get("last_result")), window=load(get("window_json"), {}),
            condition=str(get("condition_text")), 
            condition_checks=int(get("condition_checks", 0) or 0),
        )


class TaskRunner:
    """队列执行器：定期取件、按 kind 分发给宿主的处理器，串行度与并发都可控。"""

    def __init__(self, queue: TaskQueue, handlers: Mapping[str, Callable[[Task], Awaitable[Any]]],
                 *, tick_seconds: int = DEFAULT_TICK_SECONDS,
                 still_current: Callable[[], bool] | None = None) -> None:
        self.queue = queue
        self.handlers = dict(handlers)
        self.tick_seconds = max(5, int(tick_seconds))
        self._still_current = still_current or (lambda: True)
        self.tasks: set[asyncio.Task[Any]] = set()
        self.processed = 0
        self.last_error = ""

    def spawn(self, coroutine: Awaitable[Any]) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def run_forever(self) -> None:
        while self._still_current():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = f"{type(error).__name__}: {error}"[:200]
            await asyncio.sleep(self.tick_seconds)

    async def tick(self) -> int:
        """跑一轮取件（测试直接调它，不必等定时器）。"""
        claimed = await self.queue.claim_due()
        for task in claimed:
            self.spawn(self._execute(task))
        return len(claimed)

    async def _execute(self, task: Task) -> None:
        handler = self.handlers.get(task.kind)
        try:
            if handler is None:
                raise RuntimeError(f"没有 {task.kind} 类型的处理器")
            result = await handler(task)
            await self.queue.finish(task, result=str(result or ""))
            self.processed += 1
        except asyncio.CancelledError:
            await self.queue.fail(task, "cancelled")
            raise
        except Exception as error:
            await self.queue.fail(task, f"{type(error).__name__}: {error}")
