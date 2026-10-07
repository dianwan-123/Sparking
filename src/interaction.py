from __future__ import annotations

import asyncio
import inspect
import random
import time
from collections import defaultdict, deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class InteractionPermit:
    scope: Hashable
    generation: int
    group_id: str
    user_id: str
    bypass_quiet: bool = False


class InteractionController:
    """Coordinates autonomous interaction rate limits and stale-work cancellation."""

    def __init__(self, *, debounce_seconds: float = 0,
                 group_cooldown_seconds: float = 180,
                 user_cooldown_seconds: float = 300,
                 max_actions_per_hour: int = 8,
                 quiet_start_hour: int = 1,
                 quiet_end_hour: int = 7,
                 max_random_wait_seconds: float = 15,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] = datetime.now,
                 random_uniform: Callable[[float, float], float] = random.uniform,
                 error_handler: Callable[[BaseException], Any] | None = None) -> None:
        if min(debounce_seconds, group_cooldown_seconds, user_cooldown_seconds,
               max_random_wait_seconds) < 0 or max_actions_per_hour <= 0:
            raise ValueError("invalid interaction limits")
        if not 0 <= quiet_start_hour <= 23 or not 0 <= quiet_end_hour <= 23:
            raise ValueError("quiet hours must be between 0 and 23")
        self.debounce_seconds = debounce_seconds
        self.group_cooldown_seconds = group_cooldown_seconds
        self.user_cooldown_seconds = user_cooldown_seconds
        self.max_actions_per_hour = max_actions_per_hour
        self.quiet_start_hour = quiet_start_hour
        self.quiet_end_hour = quiet_end_hour
        self.max_random_wait_seconds = max_random_wait_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._random_uniform = random_uniform
        self._error_handler = error_handler
        self._generation: dict[Hashable, int] = defaultdict(int)
        self._last_group: dict[str, float] = {}
        self._last_user: dict[str, float] = {}
        self._hourly: deque[float] = deque()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._scope_tasks: dict[Hashable, asyncio.Task[Any]] = {}
        self._terminated = False
        self._lock = asyncio.Lock()

    def is_quiet(self, at: datetime | None = None) -> bool:
        hour = (at or self._wall_clock()).hour
        start, end = self.quiet_start_hour, self.quiet_end_hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    async def begin(self, scope: Hashable, group_id: str | int, user_id: str | int,
                    *, bypass_quiet: bool = False, supersede: bool = True) -> InteractionPermit | None:
        """Create a permit if current limits allow evaluation.

        With `supersede=False` the permit keeps the current generation, so
        in-flight replies are queued and sent instead of cancelled by newer
        messages.
        """
        if self._terminated or (not bypass_quiet and self.is_quiet()):
            return None
        group, user = str(group_id), str(user_id)
        async with self._lock:
            now = self._clock()
            self._prune(now)
            if self._on_cooldown(group, user, now) or len(self._hourly) >= self.max_actions_per_hour:
                return None
            if supersede:
                self._generation[scope] += 1
                generation = self._generation[scope]
            else:
                generation = self._generation[scope]
        if self.debounce_seconds:
            await asyncio.sleep(self.debounce_seconds)
        permit = InteractionPermit(scope, generation, group, user, bypass_quiet)
        return permit if self.is_current(permit) else None

    def is_current(self, permit: InteractionPermit) -> bool:
        return not self._terminated and self._generation.get(permit.scope) == permit.generation

    def invalidate(self, scope: Hashable) -> None:
        self._generation[scope] += 1
        task = self._scope_tasks.pop(scope, None)
        if task is not None and not task.done():
            task.cancel()

    async def commit(self, permit: InteractionPermit) -> bool:
        """Atomically consume cooldown/hourly quota immediately before an action."""
        async with self._lock:
            if not self.is_current(permit) or (not permit.bypass_quiet and self.is_quiet()):
                return False
            now = self._clock()
            self._prune(now)
            if self._on_cooldown(permit.group_id, permit.user_id, now):
                return False
            if len(self._hourly) >= self.max_actions_per_hour:
                return False
            self._last_group[permit.group_id] = now
            self._last_user[permit.user_id] = now
            self._hourly.append(now)
            return True

    async def wait_until_current(self, permit: InteractionPermit,
                                 wait_seconds: float | None = None) -> bool:
        if not self.is_current(permit):
            return False
        delay = wait_seconds
        if delay is None:
            delay = self._random_uniform(0, self.max_random_wait_seconds)
        delay = min(max(float(delay), 0), self.max_random_wait_seconds)
        if delay:
            await asyncio.sleep(delay)
        return self.is_current(permit)

    def schedule(self, scope: Hashable, group_id: str | int, user_id: str | int,
                 action: Callable[[], Any], *, wait_seconds: float | None = None,
                 bypass_quiet: bool = False) -> asyncio.Task[bool]:
        """Debounce, wait, recheck limits, then run the latest action for a scope."""
        if self._terminated:
            raise RuntimeError("interaction controller is terminated")

        async def runner() -> bool:
            permit = await self.begin(scope, group_id, user_id, bypass_quiet=bypass_quiet)
            if permit is None or not await self.wait_until_current(permit, wait_seconds):
                return False
            if not await self.commit(permit):
                return False
            result = action()
            if inspect.isawaitable(result):
                await result
            return True

        task = asyncio.create_task(runner())
        previous = self._scope_tasks.get(scope)
        if previous is not None and not previous.done():
            previous.cancel()
        self._scope_tasks[scope] = task
        self._tasks.add(task)
        task.add_done_callback(lambda finished: self._discard(scope, finished))
        return task

    def _discard(self, scope: Hashable, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if self._scope_tasks.get(scope) is task:
            self._scope_tasks.pop(scope, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is None or self._error_handler is None:
            return
        try:
            handled = self._error_handler(error)
            if inspect.isawaitable(handled):
                handler_task = asyncio.create_task(handled)
                self._tasks.add(handler_task)
                handler_task.add_done_callback(self._discard_handler)
        except Exception as handler_error:
            loop = asyncio.get_running_loop()
            loop.call_exception_handler({
                "message": "InteractionController error handler failed",
                "exception": handler_error,
            })

    def _discard_handler(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            asyncio.get_running_loop().call_exception_handler({
                "message": "InteractionController async error handler failed",
                "exception": error,
            })

    def _on_cooldown(self, group: str, user: str, now: float) -> bool:
        group_last = self._last_group.get(group, float("-inf"))
        user_last = self._last_user.get(user, float("-inf"))
        return (now - group_last < self.group_cooldown_seconds
                or now - user_last < self.user_cooldown_seconds)

    def _prune(self, now: float) -> None:
        cutoff = now - 3600
        while self._hourly and self._hourly[0] <= cutoff:
            self._hourly.popleft()

    async def terminate(self) -> None:
        self._terminated = True
        self._generation.clear()
        tasks = list(self._tasks)
        self._tasks.clear()
        self._scope_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def __aenter__(self) -> "InteractionController":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.terminate()
