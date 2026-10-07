# -*- coding: utf-8 -*-
"""v0.34.2/v0.36.0 回归：画图能力从"自动直通管线"改为"普通工具 draw_picture"。

v0.34.2 实录：直通画图提前 return 吞掉"做成拓展"条款、指代式请求画成猫。
v0.36.0 按用户要求彻底去固定路由：要不要画、画什么全部由主agent决定——
这里只测 draw_picture 工具本体（可靠管线：SVG→渲染→截图→发送）与
`_looks_like_server_status_request`（管线内部的数据增强判定，非路由）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class ServerStatusDetectionTests(unittest.TestCase):
    """`_looks_like_server_status_request` 只服务于 draw_picture 内部抓真指标。"""

    def test_positive_and_negative(self):
        _hooks()
        from DFYChat import main as plugin_main

        self.assertTrue(plugin_main._looks_like_server_status_request(
            "画个图给我看看ssh到的服务器状态"))
        self.assertTrue(plugin_main._looks_like_server_status_request(
            "给我画个服务器的资源监控面板"))
        self.assertFalse(plugin_main._looks_like_server_status_request("画只猫"))
        self.assertFalse(plugin_main._looks_like_server_status_request(
            "服务器上的代码怎么改"))


class DrawPictureToolTests(unittest.IsolatedAsyncioTestCase):
    """draw_picture：主agent决定调不调；调了就走可靠管线出图发送。"""

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

    async def test_empty_subject_friendly_error(self):
        plugin, hooks = await self._plugin()
        event = hooks.FakeEvent(hooks._group_raw("画", 6801), "画", hooks.FakeBot(),
                                wake=True)
        result = await plugin.draw_picture_tool(event, "  ")
        self.assertIn("需要 subject", result)

    async def test_tool_draws_and_sends(self):
        plugin, hooks = await self._plugin()
        svg_json = json.dumps({"title": "戴眼镜的猫", "svg": "<svg viewBox='0 0 800 600'><rect width='800' height='600' fill='#fff'/></svg>"})

        async def fake_llm(provider, *, prompt, system_prompt="", **kwargs):
            return svg_json

        plugin._llm_text = fake_llm
        plugin._studio_host = lambda: types.SimpleNamespace(
            put_design=lambda content: {"url": "http://127.0.0.1:1/d/x"},
            list_design_projects=lambda: [])
        plugin._design_screenshot = None  # 防呆：下面必须用 stub

        async def fake_shot(content):
            return "media-1"

        plugin._design_screenshot = fake_shot
        plugin.media = types.SimpleNamespace(
            get_path=lambda media_id: _async(str(Path(plugin.storage.path.parent)
                                                / "media-1.png")))
        event = hooks.FakeEvent(hooks._group_raw("画只戴眼镜的猫", 6802),
                                "画只戴眼镜的猫", hooks.FakeBot(), wake=True)
        result = json.loads(await plugin.draw_picture_tool(event, "戴眼镜的猫"))
        self.assertTrue(result["ok"])
        self.assertEqual("戴眼镜的猫", result["title"])
        self.assertFalse(result["real_metrics"])
        self.assertEqual(1, len(event.sent), "图要真正发出去")

    async def test_llm_garbage_reports_instead_of_silence(self):
        plugin, hooks = await self._plugin()

        async def fake_llm(provider, *, prompt, system_prompt="", **kwargs):
            return "我不会画"

        plugin._llm_text = fake_llm
        event = hooks.FakeEvent(hooks._group_raw("画只猫", 6803), "画只猫",
                                hooks.FakeBot(), wake=True)
        result = await plugin.draw_picture_tool(event, "猫")
        self.assertIn("没走通", result)


class DrawCorePayloadTests(unittest.IsolatedAsyncioTestCase):
    """`_draw_and_send` 的提示词装配：recent_designs/context 注入、真指标增强。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin, hooks

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_payload_carries_context_and_recent_designs(self):
        plugin, hooks = await self._plugin()
        captured: dict[str, str] = {}

        async def fake_llm(provider, *, prompt, system_prompt="", **kwargs):
            captured["prompt"] = prompt
            return None  # svg 解析为空 → 返回 None（够测 payload 了）

        plugin._llm_text = fake_llm
        plugin._studio_host = lambda: types.SimpleNamespace(
            list_design_projects=lambda: [
                {"id": "d1", "title": "目标服务器状态", "kind": "design"},
                {"id": "d2", "title": "鹈鹕骑车", "kind": "design"},
            ])
        event = hooks.FakeEvent(hooks._group_raw("x", 6811), "x", hooks.FakeBot(),
                                wake=True)
        result = await plugin._draw_and_send(
            event, "复用技能再画一次", context='{"memory_context": {"last_topic": "服务器状态"}}')
        self.assertIsNone(result)
        payload = json.loads(captured["prompt"])
        self.assertEqual(["目标服务器状态", "鹈鹕骑车"], payload["recent_designs"])
        self.assertIn("服务器状态", payload["context"])

    async def test_server_subject_fetches_real_metrics(self):
        plugin, hooks = await self._plugin()
        captured: dict[str, str] = {}
        ssh_calls: list[str] = []

        async def fake_llm(provider, *, prompt, system_prompt="", **kwargs):
            captured["prompt"] = prompt
            return None

        plugin._llm_text = fake_llm
        plugin._studio_host = lambda: types.SimpleNamespace(
            list_design_projects=lambda: [])
        plugin._ssh_configured = lambda: True

        async def fake_ssh(tool, args):
            ssh_calls.append(tool)
            return json.dumps({"exit_code": 0, "stdout": "CPU=2核 负载0.19", "stderr": ""})

        plugin._dispatch_ssh_action = fake_ssh
        event = hooks.FakeEvent(hooks._group_raw("x", 6812), "x", hooks.FakeBot(),
                                wake=True)
        await plugin._draw_and_send(event, "目标服务器状态仪表盘")
        self.assertEqual(["ssh_exec"], ssh_calls, "服务器状态主题要抓真指标")
        payload = json.loads(captured["prompt"])
        self.assertIn("CPU=2核", payload["real_metrics"])

    async def test_plain_subject_does_not_fetch_metrics(self):
        plugin, hooks = await self._plugin()
        ssh_calls: list[str] = []

        async def fake_llm(provider, *, prompt, system_prompt="", **kwargs):
            return None

        plugin._llm_text = fake_llm
        plugin._studio_host = lambda: types.SimpleNamespace(
            list_design_projects=lambda: [])
        plugin._ssh_configured = lambda: True

        async def fake_ssh(tool, args):
            ssh_calls.append(tool)
            return "{}"

        plugin._dispatch_ssh_action = fake_ssh
        event = hooks.FakeEvent(hooks._group_raw("x", 6813), "x", hooks.FakeBot(),
                                wake=True)
        await plugin._draw_and_send(event, "戴眼镜的猫")
        self.assertEqual([], ssh_calls, "画猫不该抓服务器指标")


def _async(value: str):
    async def _inner():
        return value
    return _inner()


if __name__ == "__main__":
    unittest.main()
