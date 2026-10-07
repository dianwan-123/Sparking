from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

_TASK_FIELDS = {"task_id", "name", "prompt", "scope_key", "group_id", "created_at",
                "next_run", "interval_kind", "interval_minutes", "last_run", "last_error", "runs"}


@dataclass(slots=True)
class ScheduledTask:
    task_id: str
    name: str
    prompt: str
    scope_key: str
    group_id: str
    created_at: float
    next_run: float
    interval_kind: str  # "once" | "minutes" | "daily"
    interval_minutes: int = 0
    last_run: float = 0.0
    last_error: str = ""
    runs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "name": self.name, "prompt": self.prompt,
            "group_id": self.group_id, "interval_kind": self.interval_kind,
            "interval_minutes": self.interval_minutes, "runs": self.runs,
            "next_run": self.next_run, "last_error": self.last_error,
        }


class TaskScheduler:
    """LLM-managed persisted task store; timing/execution lives in main."""

    def __init__(self, path: str | Path, *, max_tasks: int = 32) -> None:
        self.path = Path(path)
        self.max_tasks = max_tasks
        self._tasks: dict[str, ScheduledTask] = {}
        self._lock = asyncio.Lock()
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._tasks = {
                item["task_id"]: ScheduledTask(**{k: v for k, v in item.items() if k in _TASK_FIELDS})
                for item in raw.get("tasks", [])
                if isinstance(item, dict) and item.get("task_id")
            }
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            self._tasks = {}

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps({"tasks": [asdict(t) for t in self._tasks.values()]},
                           ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            pass

    async def add(
        self,
        name: str,
        prompt: str,
        *,
        group_id: str,
        delay_minutes: int | None = None,
        daily_hour: int | None = None,
        interval_minutes: int | None = None,
        scope_key: str = "",
    ) -> ScheduledTask:
        name = str(name).strip()[:60]
        prompt = str(prompt).strip()[:4000]
        if not name or not prompt:
            raise ValueError("task name and prompt are required")
        if delay_minutes is None and daily_hour is None and interval_minutes is None:
            raise ValueError("choose delay_minutes, interval_minutes, or daily_hour")
        now = time.time()
        if delay_minutes is not None:
            interval_kind, interval = "once", 0
            next_run = now + max(int(delay_minutes), 0) * 60
        elif daily_hour is not None:
            hour = int(daily_hour)
            if not 0 <= hour <= 23:
                raise ValueError("daily_hour must be 0-23")
            interval_kind, interval = "daily", hour
            next_run = _next_daily(hour)
        else:
            interval_kind = "minutes"
            interval = max(int(interval_minutes or 0), 5)
            next_run = now + interval * 60
        async with self._lock:
            if len(self._tasks) >= self.max_tasks:
                raise ValueError("scheduler is full; cancel a task first")
            task = ScheduledTask(
                task_id=uuid.uuid4().hex[:12], name=name, prompt=prompt,
                scope_key=scope_key, group_id=str(group_id), created_at=now,
                next_run=next_run, interval_kind=interval_kind, interval_minutes=interval,
            )
            self._tasks[task.task_id] = task
            self.save()
        return task

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            task = self._tasks.pop(str(task_id), None)
            self.save()
        return task is not None

    def list(self) -> list[dict[str, Any]]:
        return sorted(
            (t.to_dict() for t in self._tasks.values()),
            key=lambda item: item["next_run"],
        )

    async def due(self, now: float | None = None) -> list[ScheduledTask]:
        now = now if now is not None else time.time()
        async with self._lock:
            ready = [t for t in self._tasks.values() if t.next_run <= now]
            for task in ready:
                if task.interval_kind == "minutes":
                    task.next_run = now + task.interval_minutes * 60
                elif task.interval_kind == "daily":
                    task.next_run = _next_daily(task.interval_minutes)
                else:
                    self._tasks.pop(task.task_id, None)
                task.last_run = now
                task.runs += 1
            self.save()
        return ready

    async def mark_error(self, task_id: str, error: str) -> None:
        task = self._tasks.get(str(task_id))
        if task is not None:
            task.last_error = str(error)[:300]
            self.save()


def _next_daily(hour: int) -> float:
    now = time.time()
    import datetime as _dt

    local_now = _dt.datetime.now()
    target = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= local_now:
        target += _dt.timedelta(days=1)
    return now + (target - local_now).total_seconds()


class TaskRunner:
    """Runs due tasks through a two-round autonomous action loop."""

    def __init__(
        self,
        scheduler: TaskScheduler,
        run_callback: Callable[[ScheduledTask], Awaitable[dict[str, Any]]],
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        self.scheduler = scheduler
        self.run_callback = run_callback
        self.interval_seconds = max(5.0, interval_seconds)
        self._wakeup = asyncio.Event()

    def poke(self) -> None:
        self._wakeup.set()

    async def loop(self, *, max_tasks_per_tick: int = 3) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
            self._wakeup.clear()
            try:
                due = await self.scheduler.due()
            except Exception:
                continue
            for task in due[:max_tasks_per_tick]:
                try:
                    await self.run_callback(task)
                except Exception as error:
                    await self.scheduler.mark_error(task.task_id, f"{type(error).__name__}: {error}")
