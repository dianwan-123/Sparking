# -*- coding: utf-8 -*-
"""v0.33.5 回归：嵌套合并转发递归展开、拓展 SSH 能力（api.ssh_exec/
api.media_from_remote）+ ssh 权限門控、自学习权限白名单含 ssh。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.script_ext import (  # noqa: E402
    ScriptExtensionError,
    ScriptExtensionManager,
)
from src.self_learning import normalize_skill  # noqa: E402


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class NestedForwardTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, gateway):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin.__new__(hooks.LongMemoryAgentPlugin)
        plugin._bind_gateway_client = lambda: None
        plugin.gateway = gateway
        return plugin

    async def test_nested_forward_expanded(self):
        class _Gateway:
            async def execute(self, action, **params):
                fid = params.get("id") or params.get("message_id")
                if fid == "outer":
                    return {"data": {"messages": [
                        {"sender": {"nickname": "甲"}, "content": [
                            {"type": "text", "data": {"text": "外层正文"}},
                            {"type": "forward", "data": {"id": "inner"}},
                        ]},
                    ]}}
                if fid == "inner":
                    return {"data": {"messages": [
                        {"sender": {"nickname": "乙"}, "content": [
                            {"type": "text", "data": {"text": "内层正文"}}]}]}}
                raise RuntimeError("unknown id")

        plugin = self._plugin(_Gateway())
        text = await plugin._fetch_forward_text("outer")
        self.assertIn("外层正文", text)
        self.assertIn("内层正文", text, "嵌套转发必须递归展开")
        self.assertIn("[嵌套转发]", text)

    async def test_forward_cycle_is_guarded(self):
        class _Gateway:
            async def execute(self, action, **params):
                return {"data": {"messages": [
                    {"sender": {"nickname": "甲"}, "content": [
                        {"type": "text", "data": {"text": "循环层"}},
                        {"type": "forward", "data": {"id": "self-ref"}},
                    ]},
                ]}}

        plugin = self._plugin(_Gateway())
        text = await plugin._fetch_forward_text("self-ref")
        self.assertIn("循环层", text)
        self.assertIn("嵌套转发未能展开", text, "自引用必须被防环拦住")

    async def test_xml_nested_forward_id_extracted(self):
        xml = ('<msg service="QQ"><item title="聊天记录"/>'
               '<source m_resid="xml-inner"/></msg>')

        class _Gateway:
            async def execute(self, action, **params):
                fid = params.get("id") or params.get("message_id")
                if fid == "outer-xml":
                    return {"data": {"messages": [
                        {"sender": {"nickname": "甲"},
                         "content": xml}]}}
                if fid == "xml-inner":
                    return {"data": {"messages": [
                        {"sender": {"nickname": "乙"}, "content": [
                            {"type": "text", "data": {"text": "XML内层"}}]}]}}
                raise RuntimeError("unknown")

        plugin = self._plugin(_Gateway())
        text = await plugin._fetch_forward_text("outer-xml")
        self.assertIn("XML内层", text)


class ExtensionSshApiTests(unittest.IsolatedAsyncioTestCase):
    async def _manager(self, directory, permissions, calls):
        root = Path(directory)
        ext_dir = root / "scripts" / "ssh-kit"
        ext_dir.mkdir(parents=True)
        (ext_dir / "extension.json").write_text(json.dumps({
            "name": "ssh-kit", "api_version": 1,
            "tools": [{"name": "shot", "description": "截个图"}],
            "permissions": permissions,
        }, ensure_ascii=False), encoding="utf-8")
        (ext_dir / "extension.py").write_text(
            "async def call_tool(api, name, params):\n"
            "    r = await api.ssh_exec('uname -a', 10)\n"
            "    mid = await api.media_from_remote('/tmp/x.png', 'note')\n"
            "    return f\"{r['exit_code']}|{mid}\"\n",
            encoding="utf-8")

        async def ssh_exec(command, timeout=60):
            calls.append(("ssh", command, timeout))
            return {"exit_code": 0, "stdout": "Linux", "stderr": "",
                    "truncated": False}

        async def media_from_remote(remote_path, note=""):
            calls.append(("media", remote_path, note))
            return "media-123"

        manager = ScriptExtensionManager(
            root / "scripts", root / "data",
            ssh_exec=ssh_exec, media_from_remote=media_from_remote)
        await manager.load_all()
        return manager

    async def test_extension_can_ssh_and_pull_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            calls: list = []
            manager = await self._manager(directory, ["ssh", "media"], calls)
            out = await manager.call("ssh-kit", "shot", {})
            self.assertEqual("0|media-123", out)
            self.assertIn(("ssh", "uname -a", 10), calls)
            self.assertIn(("media", "/tmp/x.png", "note"), calls)

    async def test_extension_without_ssh_permission_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            calls: list = []
            manager = await self._manager(directory, ["media"], calls)
            with self.assertRaises(ScriptExtensionError) as ctx:
                await manager.call("ssh-kit", "shot", {})
            self.assertIn("ssh", str(ctx.exception))
            self.assertEqual([], calls, "未声明权限不能触达宿主能力")


class SelfLearningSshPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_learn_skill_tool_passes_permissions(self):
        """工具链缺口回归：learn_skill 必须能声明 permissions，否则带 ssh 代码的技能
        一跑就撞"未声明权限"。"""
        hooks = _hooks()
        tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = tmp.name
        self.addCleanup(tmp.cleanup)
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()

        async def _safe_terminate():
            try:
                await plugin.terminate()
            except Exception:
                pass

        self.addCleanup(_safe_terminate)
        result = json.loads(await plugin._skill_tool_action("learn_skill", {
            "skill_id": "server-shot",
            "description": "远程截图回传",
            "prompts": "一句话截图",
            "code": (
                "async def call_tool(api, name, params):\n"
                "    await api.ssh_exec('echo hi')\n"
                "    return await api.media_from_remote('/tmp/s.png')\n"
            ),
            "tools_json": json.dumps([{"name": "server_shoot", "description": "截图"}]),
            "permissions_json": json.dumps(["ssh", "media"]),
        }))
        self.assertTrue(result["ok"], result)
        ext = plugin._script_manager().get("server-shot")
        self.assertIsNotNone(ext)
        self.assertIn("ssh", set(ext.manifest.get("permissions") or []))
        self.assertIn("media", set(ext.manifest.get("permissions") or []))


class SelfLearningSshPermissionTests2(unittest.TestCase):
    def test_normalize_skill_accepts_ssh_permission(self):
        _hooks()
        skill = normalize_skill({
            "id": "server-shot",
            "description": "远程截图回传",
            "prompts": ["用 api.ssh_exec 截图再 api.media_from_remote 拉回"],
            "code": (
                "async def call_tool(api, name, params):\n"
                "    await api.ssh_exec('chromium --headless --screenshot=/tmp/s.png URL')\n"
                "    return await api.media_from_remote('/tmp/s.png')\n"
            ),
            "permissions": ["ssh", "media", "bogus-perm"],
        })
        self.assertIsNotNone(skill)
        self.assertEqual(["ssh", "media"], skill["permissions"],
                         "ssh 权限要保留、未知权限要丢弃")


if __name__ == "__main__":
    unittest.main()
