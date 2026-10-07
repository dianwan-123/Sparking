# -*- coding: utf-8 -*-
"""只读子agent的回归测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.models import NormalizedMessage


def _hooks():
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks

    return hooks


class SubagentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self, **config_extra):
        hooks = _hooks()
        config = hooks._plugin_config()
        config.update(config_extra)
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin, hooks

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _seed(self, plugin):
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
        plugin._known_scopes["123"] = scope
        for i in range(6):
            await plugin.ingest.ingest(NormalizedMessage(
                platform="aiocqhttp", account_id="1", conversation_id="123",
                upstream_message_id=f"m-{i}", sender_id="10002",
                sender_name="甲", text=f"今天大家都在聊新游戏 {i}",
                occurred_at=f"2026-09-19T11:{i:02d}:00+00:00",
                raw_event={"message_id": f"m-{i}"}, parts=[],
                event_type="message.created",
            ))
        return scope

    async def test_disabled_by_default_tool_reports_and_filtered(self):
        """v0.27.0 起子代理默认开启；此用例改为验证显式关闭时的门控行为。"""
        plugin, hooks = await self._plugin(subagent_enabled=False)
        event = hooks.FakeEvent(hooks._group_raw("聊什么", 10002), "聊什么",
                                hooks.FakeBot(), wake=True)
        result = await plugin.dispatch_subagent_tool(event, "总结")
        self.assertIn("未启用", result)
        names = {str(getattr(tool, "name", ""))
                 for tool in plugin._effective_tool_set(None).tools}
        self.assertNotIn("dispatch_subagent", names)
        self.assertNotIn("dispatch_parallel_subagents", names)

    async def test_enabled_tool_runs_readonly_analysis(self):
        plugin, hooks = await self._plugin(
            subagent_enabled=True, subagent_provider_id="fake-provider")
        scope = await self._seed(plugin)
        plugin.context.responses = ["分析结论：群里在聊新游戏，情绪高涨。"]
        event = hooks.FakeEvent(hooks._group_raw("聊什么", 10002), "聊什么",
                                hooks.FakeBot(), wake=True)
        result = await plugin.dispatch_subagent_tool(event, "总结今天的话题")
        self.assertIn("分析结论", result)
        # v0.35.0 起子agent是执行型：带事件上下文走 tool_loop_agent 工具循环，
        # 工具集=除浏览器+派发递归外全部
        call = plugin.context.llm_calls[0]
        self.assertTrue(call.get("agent"), call)
        self.assertIn("执行型子agent", str(call.get("system_prompt", "")))
        tool_names = {getattr(t, "name", "") for t in (call.get("tools") or [])}
        self.assertNotIn("browse", tool_names)
        self.assertNotIn("dispatch_subagent", tool_names)
        self.assertIn("python_exec", tool_names)
        # 抓取的数据里包含聊天样本
        self.assertIn("新游戏", str(call.get("prompt", "")))
        # 结果未自动入记忆（由主agent决定）
        summaries = await plugin.storage.list_summaries(scope, 10)
        self.assertEqual([], summaries)

    async def test_subagent_tool_set_analyzer_is_readonly(self):
        """决策层分析子agent：只读——发送/写入/执行类工具全部剔除。"""
        plugin, hooks = await self._plugin(subagent_enabled=True)
        event = hooks.FakeEvent(hooks._group_raw("聊什么", 10003), "聊什么",
                                hooks.FakeBot(), wake=True)
        names = {str(getattr(tool, "name", ""))
                 for tool in plugin._subagent_tool_set(event, analyzer=True).tools}
        for blocked in ("send_image", "send_local_file", "ssh_exec", "python_exec",
                        "learn_skill", "napcat_call", "browse", "dispatch_subagent",
                        "todo", "say_now", "ask_user",
                        "forward_messages", "screenshot_messages"):
            self.assertNotIn(blocked, names)
        for allowed in ("memory_catalog", "web_search", "design_list", "fs_list",
                        "fs_read", "search_chat_history"):
            self.assertIn(allowed, names)

    async def test_parallel_dispatch_runs_all(self):
        plugin, hooks = await self._plugin(
            subagent_enabled=True, subagent_provider_id="fake-provider")
        scope = await self._seed(plugin)
        plugin.context.responses = ["话题总结：聊游戏。", "语气分析：大家用语随意。"]
        event = hooks.FakeEvent(hooks._group_raw("聊什么", 10002), "聊什么",
                                hooks.FakeBot(), wake=True)
        result = await plugin.dispatch_parallel_subagents_tool(
            event, '["总结话题", "分析语气"]')
        self.assertIn("话题总结", result)
        self.assertIn("语气分析", result)
        self.assertEqual(2, len(plugin.context.llm_calls))

    async def test_dispatch_channel_respects_toggle(self):
        plugin, hooks = await self._plugin(
            subagent_enabled=True, subagent_provider_id="fake-provider")
        scope = await self._seed(plugin)
        plugin.context.responses = ["调度通道子agent结论。"]
        result = await plugin._dispatch_task_action(
            "dispatch_subagent", {"task": "总结"}, scope)
        self.assertIn("结论", result)
        # 关闭后拒绝
        plugin.settings.subagent_enabled = False
        blocked = await plugin._dispatch_task_action(
            "dispatch_subagent", {"task": "总结"}, scope)
        self.assertIn("未在 WebUI 启用", blocked)

    async def test_subagent_has_no_tools_and_no_writes(self):
        """子agent调用不得带工具（构造上只读），也不产生任何写入。"""
        plugin, hooks = await self._plugin(
            subagent_enabled=True, subagent_provider_id="fake-provider")
        scope = await self._seed(plugin)
        plugin.context.responses = ["只读分析结果。"]
        await plugin._run_subagent_task("总结聊天", scope)
        call = plugin.context.llm_calls[0]
        self.assertIsNone(call.get("tools"), "子agent不得携带任何工具")
        summaries = await plugin.storage.list_summaries(scope, 10)
        self.assertEqual([], summaries)
        impressions = await plugin.storage.list_impressions([scope])
        self.assertEqual([], impressions)


if __name__ == "__main__":
    unittest.main()
