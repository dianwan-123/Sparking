from __future__ import annotations

import unittest


class LlmRetryTests(unittest.IsolatedAsyncioTestCase):
    def _make_plugin(self, provider):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import tests.test_plugin_hooks as hooks

        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin.settings.llm_timeout_seconds = 30

        async def get_current(umo):
            return "p1"

        plugin.context.get_current_chat_provider_id = get_current
        plugin._current_provider = get_current

        class _Response:
            completion_text = "ok"

        state = {"calls": 0}

        async def llm_generate(**kwargs):
            state["calls"] += 1
            if state["calls"] < provider["fail_times"]:
                raise RuntimeError(
                    "Error code: 404 - No active credentials for provider: codebuddy-intl"
                )
            return _Response()

        plugin.context.llm_generate = llm_generate
        return plugin, state

    async def test_retries_transient_provider_errors(self):
        plugin, state = self._make_plugin({"fail_times": 3})
        text = await plugin._llm_text("p1", prompt="hi", system_prompt="sys")
        self.assertEqual("ok", text)
        self.assertEqual(3, state["calls"])

    async def test_gives_up_after_five_attempts(self):
        plugin, state = self._make_plugin({"fail_times": 99})
        with self.assertRaises(RuntimeError):
            await plugin._llm_text("p1", prompt="hi", system_prompt="sys")
        self.assertEqual(5, state["calls"])

    async def test_timeout_errors_are_not_retried(self):
        import asyncio

        plugin, state = self._make_plugin({"fail_times": 99})

        async def llm_generate(**kwargs):
            state["calls"] += 1
            await asyncio.sleep(999)

        plugin.context.llm_generate = llm_generate
        plugin.settings.llm_timeout_seconds = 1
        with self.assertRaises(TimeoutError) as caught:
            await plugin._llm_text("p1", prompt="hi", system_prompt="sys")
        self.assertEqual(1, state["calls"])
        # 超时错误必须带说明（否则"回复生成失败："后面是空的，没法排查）
        self.assertTrue(str(caught.exception).strip())


    async def test_empty_output_is_retried_then_succeeds(self):
        """实录：mimo-v2.6-flash 偶发 content=None、0 completion tokens——
        空输出必须重试，而不是把 None 传给 parse_json_object 崩 strip。"""
        import types
        import unittest.mock as mock

        plugin, state = self._make_plugin({"fail_times": 1})
        responses = iter([
            types.SimpleNamespace(completion_text=None),
            types.SimpleNamespace(completion_text="ok"),
        ])

        async def llm_generate(**kwargs):
            state["calls"] += 1
            return next(responses)

        plugin.context.llm_generate = llm_generate
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            text = await plugin._llm_text("p1", prompt="hi", system_prompt="sys")
        self.assertEqual("ok", text)
        self.assertEqual(2, state["calls"])

    async def test_persistent_empty_output_returns_blank(self):
        import types
        import unittest.mock as mock

        plugin, state = self._make_plugin({"fail_times": 1})

        async def llm_generate(**kwargs):
            state["calls"] += 1
            return types.SimpleNamespace(completion_text=None)

        plugin.context.llm_generate = llm_generate
        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            text = await plugin._llm_text("p1", prompt="hi", system_prompt="sys")
        self.assertEqual("", text, "重试耗尽返回空串，绝不能返回 None")
        self.assertEqual(5, state["calls"])

    async def test_agent_empty_output_not_restarted(self):
        """agent 循环重跑会重复执行工具副作用（重复发消息），空输出只能兜底。"""
        import types

        plugin, state = self._make_plugin({"fail_times": 1})

        async def tool_loop_agent(**kwargs):
            state["calls"] += 1
            return types.SimpleNamespace(completion_text=None)

        plugin.context.tool_loop_agent = tool_loop_agent
        text = await plugin._llm_text(
            "p1", prompt="hi", system_prompt="sys", agent=True, event=object())
        self.assertEqual("", text)
        self.assertEqual(1, state["calls"], "agent 空输出不重跑")


if __name__ == "__main__":
    unittest.main()
