# -*- coding: utf-8 -*-
"""v0.35.0/v0.36.0 回归：完全的子agent决策。

v0.35.0 曾用固定 kind 枚举（chat|draw|multi_step_task|…）路由——用户指出这
本质上还是关键词分类。v0.36.0 起需求梳理子agent输出自由形式
（understanding/plan/reply_strategy），代码零类别分支；是否需要梳理由判定器
（LLM）的 needs_plan 决定；画图等一切执行都是普通工具，由主agent自主调用。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class ConfigDefaultsTests(unittest.TestCase):
    def test_new_fields_default_on(self):
        from src.config import PluginConfig

        settings = PluginConfig.from_mapping({})
        self.assertTrue(settings.subagent_tools_enabled)
        self.assertTrue(settings.decision_subagent_enabled)
        self.assertEqual(6, settings.decision_subagent_max_steps)


class DecisionNeedsPlanTests(unittest.TestCase):
    def test_from_mapping_parses_needs_plan(self):
        from src.models import Decision

        self.assertTrue(Decision.from_mapping(
            {"action": "text", "needs_plan": True}, 5).needs_plan)
        self.assertFalse(Decision.from_mapping({"action": "text"}, 5).needs_plan)
        self.assertFalse(Decision.from_mapping(
            {"action": "text", "needs_plan": "yes"}, 5).needs_plan is None)

    def test_engine_parse_passes_needs_plan(self):
        hooks = _hooks()
        from src.decision import DecisionEngine

        engine = DecisionEngine(lambda _prompt: json.dumps({
            "action": "text", "intent": "画图", "needs_plan": True}))
        decision = engine.parse('{"action": "text", "intent": "画图", "needs_plan": true}')
        self.assertTrue(decision.needs_plan)


class DecisionAnalysisTests(unittest.IsolatedAsyncioTestCase):
    """需求梳理子agent：自由形式输出，唯一硬性要求 understanding 非空。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self, responses=None):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext(responses), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin, hooks

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_analyzer_returns_open_form(self):
        plugin, hooks = await self._plugin([json.dumps({
            "understanding": "重画刚才那张服务器状态图（指代：最近的绘制成果），并把它做成拓展",
            "constraints": ["指标必须真实"],
            "plan": [
                {"step": "重画服务器状态图", "how": "draw_picture（自动带真实指标）"},
                {"step": "固化成技能", "how": "learn_skill"},
            ],
            "reply_strategy": "画完说一句，拓展装好后确认",
            "confidence": 0.9,
        })])
        from src.models import Decision

        event = hooks.FakeEvent(hooks._group_raw("复用技能再画一次", 6701),
                                "复用技能再画一次", hooks.FakeBot(), wake=True)
        analysis = await plugin._decision_analysis(event, "{}", Decision(action="text"))
        self.assertIn("服务器状态图", analysis["understanding"])
        self.assertEqual(2, len(analysis["plan"]))
        # 只读工具集 + 受限步数
        call = plugin.context.llm_calls[0]
        self.assertEqual(6, call["max_steps"])
        names = {getattr(t, "name", "") for t in call["tools"]}
        self.assertNotIn("send_image", names)
        self.assertNotIn("browse", names)

    async def test_analyzer_garbage_returns_none(self):
        plugin, hooks = await self._plugin(["这不是JSON"])
        from src.models import Decision

        event = hooks.FakeEvent(hooks._group_raw("画只猫", 6702), "画只猫",
                                hooks.FakeBot(), wake=True)
        self.assertIsNone(await plugin._decision_analysis(event, "{}", Decision(action="text")))

    async def test_analyzer_missing_understanding_rejected(self):
        plugin, hooks = await self._plugin(['{"plan": [], "confidence": 0.5}'])
        from src.models import Decision

        event = hooks.FakeEvent(hooks._group_raw("画只猫", 6703), "画只猫",
                                hooks.FakeBot(), wake=True)
        self.assertIsNone(await plugin._decision_analysis(event, "{}", Decision(action="text")))

    async def test_analyzer_disabled_skips(self):
        plugin, hooks = await self._plugin()
        plugin.settings.decision_subagent_enabled = False
        from src.models import Decision

        event = hooks.FakeEvent(hooks._group_raw("画只猫", 6704), "画只猫",
                                hooks.FakeBot(), wake=True)
        self.assertIsNone(await plugin._decision_analysis(event, "{}", Decision(action="text")))
        self.assertEqual([], plugin.context.llm_calls)


class AnalysisGatingTests(unittest.IsolatedAsyncioTestCase):
    """是否梳理由判定器（LLM）的 needs_plan/agent 决定；梳理后没有任何固定路由。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self, responses):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext(responses), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123", "群")
        plugin._known_scopes["123"] = scope
        return plugin, hooks, scope

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    @staticmethod
    def _is_analyzer_call(call: dict) -> bool:
        return "你是需求梳理子agent" in str(call.get("system_prompt", ""))

    async def test_needs_plan_triggers_analysis_for_text(self):
        analysis_json = json.dumps({
            "understanding": "画服务器状态图", "plan": [{"step": "画图", "how": "draw_picture"}],
            "reply_strategy": "画完说一句", "confidence": 0.9})
        plugin, hooks, scope = await self._plugin([analysis_json, "喏 画好了"])
        text = "画个图看看服务器状态"
        event = hooks.FakeEvent(hooks._group_raw(text, 6711), text, hooks.FakeBot(),
                                wake=True)
        from src.models import Decision

        await plugin._execute_decision(event, scope, "6711", "{}",
                                       Decision(action="text", needs_plan=True),
                                       mock.MagicMock(), False, None)
        analyzers = [c for c in plugin.context.llm_calls if self._is_analyzer_call(c)]
        self.assertEqual(1, len(analyzers), "needs_plan=true 要触发需求梳理")
        reply_calls = [c for c in plugin.context.llm_calls if not self._is_analyzer_call(c)]
        self.assertTrue(reply_calls, "梳理后主agent要生成回复")
        self.assertIn("request_analysis", str(reply_calls[-1].get("prompt", "")))

    async def test_plain_text_skips_analysis(self):
        plugin, hooks, scope = await self._plugin([
            '{"thought": "好", "mode": "single", "message": {"action": "text", "text": "哈哈 是的"}}'])
        event = hooks.FakeEvent(hooks._group_raw("确实", 6712), "确实",
                                hooks.FakeBot(), wake=True)
        from src.models import Decision

        await plugin._execute_decision(event, scope, "6712", "{}",
                                       Decision(action="text", needs_plan=False),
                                       mock.MagicMock(), False, None)
        self.assertFalse(any(self._is_analyzer_call(c) for c in plugin.context.llm_calls),
                         "纯闲聊不进需求梳理")

    async def test_no_fixed_routing_even_for_draw_requests(self):
        """核心回归：即使消息明确是画图请求，也没有任何自动画图路径——
        是否调 draw_picture 由主agent在工具循环里自己决定。"""
        analysis_json = json.dumps({
            "understanding": "画服务器状态图", "plan": [{"step": "画图", "how": "draw_picture"}],
            "reply_strategy": "画完说一句", "confidence": 0.9})
        plugin, hooks, scope = await self._plugin([analysis_json, "喏 画好了"])
        draw_calls: list[str] = []

        async def recorder(event, subject, context=""):
            draw_calls.append(subject)
            return None

        plugin._draw_and_send = recorder
        text = "画个图看看服务器状态"
        event = hooks.FakeEvent(hooks._group_raw(text, 6713), text, hooks.FakeBot(),
                                wake=True)
        from src.models import Decision

        await plugin._execute_decision(event, scope, "6713", "{}",
                                       Decision(action="text", needs_plan=True),
                                       mock.MagicMock(), False, None)
        self.assertEqual([], draw_calls, "决策层不许自动触发画图——主agent自己决定")

    async def test_reply_plan_injects_request_analysis(self):
        plugin, hooks, scope = await self._plugin([
            '{"thought": "好", "mode": "single", '
            '"message": {"action": "text", "text": "来了"}}'])
        analysis = {"understanding": "读服务器指标并画图",
                    "plan": [{"step": "ssh 抓指标", "how": "ssh_exec"},
                             {"step": "画图发送", "how": "draw_picture"}],
                    "reply_strategy": "画完说一句", "confidence": 0.9}
        text = "看看服务器再画个图"
        event = hooks.FakeEvent(hooks._group_raw(text, 6714), text, hooks.FakeBot(),
                                wake=True)
        from src.models import Decision

        await plugin._reply_plan(event, scope, "{}", Decision(action="agent"),
                                 analysis=analysis)
        call = plugin.context.llm_calls[-1]
        self.assertIn("request_analysis", str(call.get("prompt", "")))
        self.assertIn("ssh 抓指标", str(call.get("prompt", "")))

    async def test_agent_prompt_has_dispatch_section(self):
        plugin, hooks, _scope = await self._plugin([])
        prompt = plugin._agent_reply_system_prompt("你是贴心的女仆。")
        self.assertIn("需求梳理与执行", prompt)
        self.assertIn("request_analysis", prompt)
        self.assertIn("dispatch_parallel_subagents", prompt)
        self.assertIn("draw_picture", prompt)
        self.assertIn("learn_skill", prompt)


if __name__ == "__main__":
    unittest.main()
