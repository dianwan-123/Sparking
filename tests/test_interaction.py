from __future__ import annotations

import asyncio
import unittest
from datetime import datetime

from src.interaction import InteractionController


class FakeClock:
    def __init__(self):
        self.value = 10000.0

    def __call__(self):
        return self.value


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    def test_quiet_hours_support_normal_and_wrapped_ranges(self):
        normal = InteractionController(quiet_start_hour=1, quiet_end_hour=7)
        wrapped = InteractionController(quiet_start_hour=22, quiet_end_hour=6)
        self.assertTrue(normal.is_quiet(datetime(2026, 1, 1, 3)))
        self.assertFalse(normal.is_quiet(datetime(2026, 1, 1, 12)))
        self.assertTrue(wrapped.is_quiet(datetime(2026, 1, 1, 23)))
        self.assertTrue(wrapped.is_quiet(datetime(2026, 1, 1, 2)))
        self.assertFalse(wrapped.is_quiet(datetime(2026, 1, 1, 12)))

    async def test_group_user_cooldowns_and_hourly_limit(self):
        clock = FakeClock()
        controller = InteractionController(
            group_cooldown_seconds=10, user_cooldown_seconds=20,
            max_actions_per_hour=2, quiet_start_hour=0, quiet_end_hour=0,
            clock=clock,
        )
        first = await controller.begin("a", "g1", "u1")
        self.assertTrue(await controller.commit(first))
        self.assertIsNone(await controller.begin("b", "g1", "u2"))
        clock.value += 11
        second = await controller.begin("c", "g1", "u2")
        self.assertTrue(await controller.commit(second))
        clock.value += 20
        self.assertIsNone(await controller.begin("d", "g2", "u3"))
        clock.value += 3600
        third = await controller.begin("e", "g2", "u3")
        self.assertTrue(await controller.commit(third))

    async def test_generation_invalidates_stale_permit(self):
        controller = InteractionController(quiet_start_hour=0, quiet_end_hour=0)
        first = await controller.begin("scope", "g", "u")
        second = await controller.begin("scope", "g", "u")
        self.assertFalse(controller.is_current(first))
        self.assertTrue(controller.is_current(second))
        self.assertFalse(await controller.commit(first))

    async def test_scheduling_cancels_stale_waiting_action(self):
        called = []
        controller = InteractionController(
            group_cooldown_seconds=0, user_cooldown_seconds=0,
            quiet_start_hour=0, quiet_end_hour=0,
        )
        first = controller.schedule("scope", "g", "u", lambda: called.append("old"), wait_seconds=1)
        await asyncio.sleep(0)
        second = controller.schedule("scope", "g", "u", lambda: called.append("new"), wait_seconds=0)
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertTrue(await second)
        self.assertEqual(called, ["new"])
        await controller.terminate()

    async def test_done_callback_retrieves_error_and_invokes_handler(self):
        handled = []
        handled_event = asyncio.Event()

        async def handler(error):
            handled.append(error)
            handled_event.set()

        async def failing_action():
            raise RuntimeError("action failed")

        controller = InteractionController(
            group_cooldown_seconds=0, user_cooldown_seconds=0,
            quiet_start_hour=0, quiet_end_hour=0, error_handler=handler,
        )
        task = controller.schedule("scope", "g", "u", failing_action, wait_seconds=0)
        with self.assertRaisesRegex(RuntimeError, "action failed"):
            await task
        await asyncio.wait_for(handled_event.wait(), 1)
        self.assertEqual(str(handled[0]), "action failed")
        await controller.terminate()

    async def test_terminate_cleans_tasks_and_blocks_new_work(self):
        controller = InteractionController(quiet_start_hour=0, quiet_end_hour=0)
        task = controller.schedule("scope", "g", "u", lambda: None, wait_seconds=10)
        await asyncio.sleep(0)
        await controller.terminate()
        self.assertTrue(task.cancelled())
        self.assertIsNone(await controller.begin("new", "g", "u"))
        with self.assertRaises(RuntimeError):
            controller.schedule("new", "g", "u", lambda: None)


if __name__ == "__main__":
    unittest.main()
