# -*- coding: utf-8 -*-
"""v0.33.0 回归：hermes/openclaw agent 能力补齐——todo 任务表、技能自学习
（体验复盘→固化→热加载→修补）、python 沙箱 bot RPC、循环检测、用量统计。"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.script_ext import ScriptExtensionManager  # noqa: E402
from src.self_learning import (  # noqa: E402
    apply_skill,
    build_evidence,
    normalize_skill,
    parse_skill_proposal,
)


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class SelfLearningPureTests(unittest.TestCase):
    def test_normalize_skill_validates(self):
        good = normalize_skill({
            "id": "exam-hint",
            "description": "读试卷给答案",
            "prompts": ["先 read_document", "  ", "再逐题作答"],
            "tools": [{"name": "answer", "description": "答题"}],
            "code": "def call_tool(api, name, params):\n    return 'ok'\n",
        })
        self.assertIsNotNone(good)
        self.assertEqual(["先 read_document", "再逐题作答"], good["prompts"])
        self.assertIsNone(normalize_skill({"id": "Bad Name!", "description": "x"}))
        self.assertIsNone(normalize_skill({"id": "ok-name", "description": ""}))
        self.assertIsNone(normalize_skill({
            "id": "ok-name", "description": "x", "code": "def broken(:\n"}))
        self.assertIsNone(normalize_skill({
            "id": "ok-name", "description": "x",
            "tools": [{"name": "t"}], "code": "x = 1\n"}))

    def test_parse_proposal_null_and_fenced(self):
        self.assertIsNone(parse_skill_proposal('{"skill": null, "reason": "无"}'))
        self.assertIsNone(parse_skill_proposal("不是JSON"))
        fenced = "```json\n" + json.dumps({
            "skill": {"id": "weibo-flow", "description": "发微博流程"}}) + "\n```"
        parsed = parse_skill_proposal(fenced)
        self.assertEqual("weibo-flow", parsed["id"])

    def test_build_evidence(self):
        rows = [
            types.SimpleNamespace(sender_name="A", text="帮我读试卷", sender_id="1"),
            types.SimpleNamespace(sender_name="B", text="好", sender_id="2"),
        ]
        text = build_evidence(rows, elapsed_seconds=120.5, known_skills=["x（旧技能）"])
        self.assertIn("120", text)
        self.assertIn("A: 帮我读试卷", text)
        self.assertIn("x（旧技能）", text)


class LearnedExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_extension_hotloads_and_protects_builtin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builtin = root / "scripts"
            (builtin / "example_dice").mkdir(parents=True)
            (builtin / "example_dice" / "extension.json").write_text(
                json.dumps({"name": "example_dice", "api_version": 1,
                            "prompts": ["内置提示"]}), encoding="utf-8")
            learned = root / "learned"
            manager = ScriptExtensionManager(builtin, root / "data", learned_root=learned)
            await manager.load_all()
            self.assertIn("example_dice", manager.extensions)

            result = await manager.create_extension("my-skill", {
                "display_name": "我的技能", "description": "演示",
                "prompts": ["记住这个流程"],
            }, "def call_tool(api, name, params):\n    return 'ok'\n")
            self.assertTrue(result["ok"], result)
            ext = manager.get("my-skill")
            self.assertIsNotNone(ext)
            self.assertEqual("learned", ext.origin)
            self.assertTrue((learned / "my-skill" / "extension.json").exists())
            self.assertIn("记住这个流程", manager.prompts())

            # 坏代码：写盘但不通过加载 → ok False，且不影响其他拓展
            bad = await manager.create_extension("broken-skill", {
                "description": "坏代码"}, "def broken(:\n")
            self.assertFalse(bad["ok"])
            self.assertIn("example_dice", manager.extensions)

            # 内置保护：不能覆盖 builtin
            protected = await manager.create_extension("example_dice", {
                "description": "试图覆盖"}, "")
            self.assertFalse(protected["ok"])


class TodoStorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def test_todo_lifecycle(self):
        from src.storage import Storage

        storage = await Storage(Path(self._tmp.name) / "t.db").open()
        try:
            rows = await storage.add_todos("s1", ["查资料", "写总结"])
            self.assertEqual(2, len(rows))
            todo_id = rows[0]["todo_id"]
            updated = await storage.update_todo("s1", todo_id, status="completed")
            self.assertEqual("completed", updated["status"])
            self.assertIsNone(await storage.update_todo("s1", "nope", status="completed"))
            active = await storage.list_todos("s1", statuses=["pending"])
            self.assertEqual(["写总结"], [x["content"] for x in active])
            removed = await storage.clear_todos("s1", done_only=True)
            self.assertEqual(1, removed)
            self.assertEqual(1, len(await storage.list_todos("s1")))
        finally:
            await storage.close()

    async def test_usage_accumulates(self):
        from src.storage import Storage

        storage = await Storage(Path(self._tmp.name) / "u.db").open()
        try:
            await storage.add_usage(1, 100, 20)
            await storage.add_usage(1, 50, 10)
            summary = await storage.usage_summary(7)
            self.assertEqual(2, summary["totals"]["calls"])
            self.assertEqual(150, summary["totals"]["prompt_tokens"])
            self.assertEqual(30, summary["totals"]["completion_tokens"])
        finally:
            await storage.close()


class PluginGrowthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _make_plugin(self, responses=None):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext(responses or []), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_learn_and_update_skill_via_action(self):
        plugin = await self._make_plugin()
        hooks = _hooks()
        event = hooks.FakeEvent(
            hooks._group_raw("随便", 4242), "随便", hooks.FakeBot(), wake=False)
        scope = await plugin._scope_for_event(event, create=True)

        result = json.loads(await plugin._skill_tool_action("learn_skill", {
            "skill_id": "demo-flow", "description": "演示流程",
            "prompts": "第一步 打开\n第二步 收工",
            "code": "def call_tool(api, name, params):\n    return 'demo-ok'\n",
            "tools_json": json.dumps([{"name": "demo_tool", "description": "演示"}]),
        }))
        self.assertTrue(result["ok"], result)
        ext = plugin._script_manager().get("demo-flow")
        self.assertIsNotNone(ext)
        self.assertEqual("learned", ext.origin)
        self.assertEqual("demo-ok", await plugin._script_manager().call(
            "demo-flow", "demo_tool", {}))
        # 自学习技能立刻参与提示词注入
        self.assertTrue(any("第一步 打开" in p for p in plugin._script_manager().prompts()))

        updated = json.loads(await plugin._skill_tool_action("update_skill", {
            "skill_id": "demo-flow", "prompts": "第三步 复盘",
        }))
        self.assertTrue(updated["ok"], updated)
        self.assertTrue(any("第三步 复盘" in p for p in plugin._script_manager().prompts()))
        self.assertIsNotNone(scope)

    async def test_builtin_skill_cannot_be_updated(self):
        """内置（随包）拓展不能被 update_skill 改——用临时目录造一个"内置"来验。"""
        plugin = await self._make_plugin()
        manager = plugin._script_manager()
        builtin = Path(self._tmp.name) / "builtin_scripts"
        (builtin / "frozen_dice").mkdir(parents=True, exist_ok=True)
        (builtin / "frozen_dice" / "extension.json").write_text(json.dumps({
            "name": "frozen-dice", "api_version": 1, "version": "1.0.0",
            "description": "假内置拓展", "tools": [], "prompts": [],
        }, ensure_ascii=False), encoding="utf-8")
        manager.scripts_root = builtin
        await manager.load_all()
        self.assertIn("frozen-dice", manager.extensions)
        answer = await plugin._skill_tool_action("update_skill", {
            "skill_id": "frozen-dice", "prompts": "偷偷改内置",
        })
        self.assertIn("不是自学习技能", answer)

    async def test_todo_tool_and_context_injection(self):
        plugin = await self._make_plugin(['{"action": "ignore"}'])
        hooks = _hooks()
        event = hooks.FakeEvent(
            hooks._group_raw("安排一下", 4243), "安排一下", hooks.FakeBot(), wake=True)
        # 与 ingest 相同的 (platform, account, conversation)：FakeEvent 的
        # get_self_id() 与 raw self_id 不一致，直接用 raw 的 self_id=1
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123")

        added = json.loads(await plugin._todo_action(
            scope, "add", items_json='["查资料", "写总结"]'))
        self.assertEqual(2, len(added["todos"]))
        listed = json.loads(await plugin._todo_action(scope, "list"))
        self.assertEqual(2, len(listed["todos"]))

        # 未完成任务在判定上下文里出现（跨轮不忘）
        await plugin.on_onebot_event(event)
        while plugin._tasks:
            import asyncio

            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)
        prompt = "\n".join(
            str(call.get("prompt", "")) for call in plugin.context.llm_calls
            if isinstance(call, dict))
        self.assertIn("active_todos", prompt)
        self.assertIn("查资料", prompt)

    async def test_ask_user_sends_question(self):
        plugin = await self._make_plugin()
        hooks = _hooks()
        event = hooks.FakeEvent(
            hooks._group_raw("做吧", 4244), "做吧", hooks.FakeBot(), wake=True)
        answer = await plugin.ask_user_tool(event, "用哪份材料？")
        self.assertIn("等待对方回复", answer)
        self.assertEqual(1, len(event.sent))
        chain = event.sent[0]
        texts = [
            part[1] if isinstance(part, tuple) else getattr(part, "text", "")
            for part in getattr(chain, "chain", [])
        ]
        self.assertTrue(any("用哪份材料" in str(t) for t in texts))

    async def test_python_sandbox_bot_rpc(self):
        plugin = await self._make_plugin()
        hooks = _hooks()
        event = hooks.FakeEvent(
            hooks._group_raw("跑", 4245), "跑", hooks.FakeBot(), wake=True)
        scope = await plugin._scope_for_event(event, create=True)
        code = (
            "print('NOW:', bot.now())\n"
            "print('NOGROUP:', bot.send_group('999999', 'hi'))\n"
            "print('MEM:', bot.search_memory('你好'))\n"
        )
        output = await plugin._run_python_sandbox(code, event, scope)
        self.assertIn("NOW:", output)
        self.assertIn("不在白名单", output)
        self.assertIn("MEM:", output)

    async def test_loop_guard_blocks_repeats(self):
        repeated = json.dumps({
            "actions": [{"tool": "python_exec", "args": {"code": "print('x')"}}],
            "done": False,
        })
        plugin = await self._make_plugin([repeated, repeated, repeated])
        hooks = _hooks()
        event = hooks.FakeEvent(
            hooks._group_raw("干活", 4246), "干活", hooks.FakeBot(), wake=True)
        scope = await plugin._scope_for_event(event, create=True)
        result = await plugin._autonomous_action_loop(scope, "测试任务", "重复动作")
        blocked = [
            item for item in result["results"]
            if "已阻止" in str(item.get("result", ""))
        ]
        self.assertTrue(blocked, result)

    async def test_usage_recorded_from_llm(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        class _Usage:
            prompt_tokens = 123
            completion_tokens = 45

        class _Raw:
            usage = _Usage()

        class _Response:
            completion_text = "ok"
            raw_completion = _Raw()

        async def llm_generate(**kwargs):
            return _Response()

        async def current(umo=""):
            return "p1"

        plugin.context.llm_generate = llm_generate
        plugin._current_provider = current
        text = await plugin._llm_text("p1", prompt="hi", system_prompt="sys")
        self.assertEqual("ok", text)
        while plugin._tasks:
            import asyncio

            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)
        summary = await plugin.storage.usage_summary(7)
        self.assertEqual(1, summary["totals"]["calls"])
        self.assertEqual(123, summary["totals"]["prompt_tokens"])

    async def test_self_learning_defaults_and_review_creates_skill(self):
        hooks = _hooks()
        from src.config import PluginConfig

        config = PluginConfig.from_mapping({"enabled": True})
        self.assertTrue(config.self_learning_enabled)
        self.assertEqual(60, config.self_learning_min_seconds)

        proposal = {
            "id": "review-made", "display_name": "复盘产物",
            "description": "来自体验复盘", "prompts": ["照着做"],
        }
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([json.dumps({"skill": proposal, "reason": "可复用"})]),
            hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        event = hooks.FakeEvent(
            hooks._group_raw("参考", 4247), "参考", hooks.FakeBot(), wake=True)
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123")
        from DFYChat.src.models import NormalizedMessage

        await plugin.ingest.ingest(NormalizedMessage(
            platform="aiocqhttp", account_id="1", conversation_id="123",
            upstream_message_id="m-review-1", sender_id="9", sender_name="指令师",
            text="帮我读试卷并给出选择题答案，先下载再逐题作答",
            occurred_at="2026-10-05T00:00:00+00:00",
            raw_event={"message_id": "m-review-1"}, parts=[],
        ))
        import unittest.mock as mock

        with mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            await plugin._skill_review(scope, 200.0)
        while plugin._tasks:
            import asyncio

            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)
        self.assertIsNotNone(plugin._script_manager().get("review-made"))


if __name__ == "__main__":
    unittest.main()
