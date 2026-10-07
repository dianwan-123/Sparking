from __future__ import annotations

import asyncio
import unittest


class PerConversationQueueTests(unittest.IsolatedAsyncioTestCase):
    def _make_plugin(self):
        import sys
        from pathlib import Path as P

        sys.path.insert(0, str(P(__file__).resolve().parents[1]))
        import tests.test_plugin_hooks as hooks

        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        return plugin, hooks

    async def _wait_for(self, condition, timeout: float = 5.0) -> bool:
        for _ in range(int(timeout / 0.05)):
            if condition():
                return True
            await asyncio.sleep(0.05)
        return condition()

    async def test_cross_conversation_parallelism(self):
        plugin, hooks = self._make_plugin()
        await plugin.initialize()
        try:
            active: set[str] = set()
            overlap = {"value": False}
            done = {"count": 0}

            async def flow(key: str) -> None:
                active.add(key)
                await asyncio.sleep(0.2)
                if len(active) > 1:
                    overlap["value"] = True
                active.discard(key)
                done["count"] += 1

            plugin._enqueue_reply("scope-A", flow("A"))
            plugin._enqueue_reply("scope-B", flow("B"))
            self.assertTrue(await self._wait_for(lambda: done["count"] == 2))
            self.assertTrue(overlap["value"], "different conversations should run in parallel")
        finally:
            await plugin.terminate()

    async def test_same_conversation_runs_sequentially(self):
        plugin, hooks = self._make_plugin()
        await plugin.initialize()
        try:
            order: list[str] = []
            running = {"flag": False}

            async def flow(name: str) -> None:
                if running["flag"]:
                    order.append("overlap")
                running["flag"] = True
                await asyncio.sleep(0.15)
                order.append(name)
                running["flag"] = False

            plugin._enqueue_reply("scope-A", flow("first"))
            plugin._enqueue_reply("scope-A", flow("second"))
            self.assertTrue(await self._wait_for(lambda: len(order) >= 2))
            self.assertNotIn("overlap", order)
            self.assertEqual(["first", "second"], order)
        finally:
            await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
