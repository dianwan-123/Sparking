# -*- coding: utf-8 -*-
"""SSH 扩展测试：配置、客户端（注入假连接）、运维子agent受控循环、插件门控。"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from src.ssh_ext import (
    SSHAgentManager,
    SSHClient,
    SSHConfig,
    SSHError,
    ensure_paramiko,
)


class FakeSSH:
    """Duck-typed stand-in for SSHClient used by the agent-loop tests."""

    def __init__(self, configured: bool = True, results=None):
        self._configured = configured
        self.commands: list[str] = []
        self.results = results or {}

    @property
    def configured(self) -> bool:
        return self._configured

    def config(self) -> SSHConfig:
        return (SSHConfig(host="h", port=22, user="u", password="p", notes="4C8G")
                if self._configured else SSHConfig())

    async def exec(self, command, *, timeout=60.0):
        self.commands.append(command)
        return self.results.get(
            command, {"exit_code": 0, "stdout": f"ok:{command}", "stderr": "",
                      "truncated": False})

    async def check(self):
        return {"exit_code": 0, "stdout": "root\nLinux ubuntu", "stderr": ""}

    async def close(self):
        return None


class ScriptedLLM:
    def __init__(self, script, default='{"done": true, "conclusion": "完成"}'):
        self.script = list(script)
        self.default = default
        self.calls: list[dict] = []

    async def __call__(self, provider, prompt, system_prompt):
        self.calls.append({"provider": provider, "prompt": prompt,
                           "system": system_prompt})
        return self.script.pop(0) if self.script else self.default


async def _wait_state(session, states, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if session.state in states:
            return True
        await asyncio.sleep(0.01)
    return session.state in states


class SSHConfigTests(unittest.TestCase):
    def test_requires_host_user_password(self):
        self.assertFalse(SSHConfig(host="h", user="u").configured)
        self.assertFalse(SSHConfig(host="h", password="p").configured)
        self.assertTrue(SSHConfig(host="h", user="u", password="p").configured)

    def test_public_masks_password(self):
        pub = SSHConfig(host="1.2.3.4", port=2222, user="ubuntu",
                        password="secret", notes="仅开放22端口").public()
        self.assertTrue(pub["configured"])
        self.assertTrue(pub["password_set"])
        self.assertNotIn("secret", str(pub))
        self.assertEqual("仅开放22端口", pub["notes"])
        self.assertEqual("ubuntu@1.2.3.4:2222",
                         SSHConfig(host="1.2.3.4", port=2222, user="ubuntu",
                                   password="x").target())


class EnsureParamikoTests(unittest.IsolatedAsyncioTestCase):
    async def test_already_installed_no_install(self):
        calls = []

        async def runner(cmd, timeout=60):
            calls.append(cmd)
            return 0, ""

        status = await ensure_paramiko(import_check=lambda: True, runner=runner)
        self.assertTrue(status["paramiko"])
        self.assertEqual([], calls)

    async def test_auto_install_uses_mirror(self):
        state = {"installed": False}
        calls = []

        async def runner(cmd, timeout=60):
            calls.append(cmd)
            state["installed"] = True
            return 0, ""

        status = await ensure_paramiko(
            import_check=lambda: state["installed"], runner=runner)
        self.assertTrue(status["paramiko"])
        self.assertIn("paramiko", calls[0])
        self.assertTrue(any("tuna.tsinghua" in part for part in calls[0]),
                        "paramiko 安装必须走国内镜像")

    async def test_no_auto_install_reports_error(self):
        status = await ensure_paramiko(auto_install=False, import_check=lambda: False)
        self.assertFalse(status["paramiko"])
        self.assertIn("未安装", status["error"])

    async def test_install_failure_surfaces(self):
        async def runner(cmd, timeout=60):
            return 1, "mirror down"

        status = await ensure_paramiko(import_check=lambda: False, runner=runner)
        self.assertFalse(status["paramiko"])
        self.assertIn("安装失败", status["error"])


class SSHClientTests(unittest.IsolatedAsyncioTestCase):
    def _cfg(self):
        return SSHConfig(host="h", port=22, user="u", password="p")

    async def test_not_configured_raises(self):
        client = SSHClient(lambda: SSHConfig(),
                           opener=lambda cfg, t: object(),
                           runner=lambda c, cmd, t: (0, "", ""))
        with self.assertRaises(SSHError):
            await client.exec("ls")

    async def test_exec_returns_streams_and_reuses_connection(self):
        opened = []

        def opener(cfg, timeout):
            opened.append(cfg)
            return object()

        def runner(client, command, timeout):
            return 0, f"out:{command}", ""

        client = SSHClient(self._cfg, opener=opener, runner=runner)
        first = await client.exec("whoami")
        second = await client.exec("uname -a")
        self.assertEqual(0, first["exit_code"])
        self.assertEqual("out:whoami", first["stdout"])
        self.assertEqual("out:uname -a", second["stdout"])
        self.assertEqual(1, len(opened), "同一连接应被复用，不该每条命令重连")

    async def test_stale_connection_reconnects_once(self):
        opened = []
        attempts = {"n": 0}

        def opener(cfg, timeout):
            opened.append(cfg)
            return object()

        def runner(client, command, timeout):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("socket closed")
            return 0, "recovered", ""

        client = SSHClient(self._cfg, opener=opener, runner=runner)
        result = await client.exec("echo hi")
        self.assertEqual("recovered", result["stdout"])
        self.assertEqual(2, len(opened), "断线后应重连一次")

    async def test_missing_paramiko_default_opener_raises(self):
        # 机器上可能真的装着 paramiko（插件会自动装）：把它模拟成"未安装"，
        # 测试不依赖环境——默认 opener 必须给出可执行的中文提示而不是崩溃
        import unittest.mock as mock

        import src.ssh_ext as ssh_ext

        client = SSHClient(self._cfg)
        with mock.patch.object(ssh_ext, "paramiko_installed", return_value=False):
            with self.assertRaises(SSHError) as ctx:
                await client.exec("ls")
        self.assertIn("paramiko", str(ctx.exception))


class SSHAgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, ssh, llm, *, max_steps=10):
        return SSHAgentManager(
            ssh=ssh, llm=llm, provider_getter=lambda: "fake-provider",
            notes_getter=lambda: "4C8G Ubuntu，仅开放22端口",
            system_prompt="SSH运维子agent", max_steps=max_steps,
            cmd_timeout=5.0, char_budget=8000, max_sessions=3)

    async def test_runs_commands_until_done(self):
        ssh = FakeSSH()
        llm = ScriptedLLM([
            '{"thought": "先看系统", "command": "uname -a"}',
            '{"thought": "装docker", "command": "apt-get install -y docker.io"}',
            '{"done": true, "conclusion": "docker已装好"}',
        ])
        manager = self._manager(ssh, llm)
        session = manager.dispatch("装好docker")
        self.assertTrue(await _wait_state(session, {"done"}))
        self.assertEqual(["uname -a", "apt-get install -y docker.io"], ssh.commands)
        self.assertIn("docker已装好", session.conclusion)
        status = manager.status(session.id)
        self.assertEqual("done", status["state"])
        self.assertTrue(status["recent_steps"])
        # machine_notes 被喂给了子agent
        self.assertIn("开放22端口", str(llm.calls[0]["prompt"]))

    async def test_pause_then_resume(self):
        ssh = FakeSSH()
        llm = ScriptedLLM([], default='{"command": "echo loop"}')
        manager = self._manager(ssh, llm, max_steps=3)
        session = manager.dispatch("循环任务")
        # dispatch 后后台任务尚未运行，立即暂停 → 首个 turn 前就停住
        manager.control(session.id, "pause")
        self.assertTrue(await _wait_state(session, {"paused"}))
        self.assertEqual([], ssh.commands, "暂停期间不该执行任何命令")
        manager.control(session.id, "resume")
        self.assertTrue(await _wait_state(session, {"done"}))
        self.assertEqual(3, len(ssh.commands), "恢复后跑满步数上限")

    async def test_ask_waits_then_operator_answers(self):
        ssh = FakeSSH()
        llm = ScriptedLLM([
            '{"thought": "要确认", "ask": "确定要删除 /data 吗？"}',
            '{"command": "rm -rf /data/tmp"}',
            '{"done": true, "conclusion": "清理完成"}',
        ])
        manager = self._manager(ssh, llm)
        session = manager.dispatch("清理磁盘")
        self.assertTrue(await _wait_state(session, {"waiting"}))
        self.assertIn("确定要删除", session.question)
        self.assertEqual([], ssh.commands)
        # 操作者答复 → 子agent继续
        manager.control(session.id, "ask", "只删 /data/tmp，别动别的")
        self.assertTrue(await _wait_state(session, {"done"}))
        self.assertEqual(["rm -rf /data/tmp"], ssh.commands)
        # 答复作为 operator_messages 进入了后续提示
        self.assertIn("只删 /data/tmp", str(llm.calls[1]["prompt"]))

    async def test_update_task_injects_new_goal(self):
        ssh = FakeSSH()
        llm = ScriptedLLM([], default='{"command": "echo working"}')
        manager = self._manager(ssh, llm, max_steps=4)
        session = manager.dispatch("原始目标")
        manager.control(session.id, "pause")
        self.assertTrue(await _wait_state(session, {"paused"}))
        manager.control(session.id, "update", "改成安装 nginx")
        self.assertTrue(await _wait_state(session, {"done"}))
        self.assertEqual("改成安装 nginx", session.task)
        self.assertIn("改成安装 nginx", str(llm.calls[0]["prompt"]))

    async def test_stop_terminates(self):
        ssh = FakeSSH()
        llm = ScriptedLLM([], default='{"command": "echo loop"}')
        manager = self._manager(ssh, llm, max_steps=50)
        session = manager.dispatch("长任务")
        manager.control(session.id, "pause")
        self.assertTrue(await _wait_state(session, {"paused"}))
        manager.control(session.id, "stop")
        self.assertTrue(await _wait_state(session, {"stopped"}))
        self.assertEqual([], ssh.commands)

    async def test_dispatch_requires_configured_ssh(self):
        manager = self._manager(FakeSSH(configured=False),
                                 ScriptedLLM([]))
        with self.assertRaises(SSHError):
            manager.dispatch("任务")

    async def test_session_cap_enforced(self):
        ssh = FakeSSH()
        # 让会话卡在 waiting（不自然结束），方便验证并发上限
        llm = ScriptedLLM([], default='{"ask": "等指示"}')
        manager = self._manager(ssh, llm)
        first = manager.dispatch("任务1")
        second = manager.dispatch("任务2")
        third = manager.dispatch("任务3")
        for s in (first, second, third):
            await _wait_state(s, {"waiting"})
        with self.assertRaises(SSHError):
            manager.dispatch("任务4")
        await manager.stop_all()

    async def test_control_unknown_session(self):
        manager = self._manager(FakeSSH(), ScriptedLLM([]))
        result = manager.control("nope", "status")
        self.assertFalse(result["ok"])


def _hooks():
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks

    return hooks


class SSHPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self, configure_ssh=True):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        # 未配置状态下 initialize（避免后台真的 pip 安装 paramiko），再补上配置
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        if configure_ssh:
            plugin.settings.ssh_host = "1.2.3.4"
            plugin.settings.ssh_user = "ubuntu"
            plugin.settings.ssh_password = "pw"
            plugin.settings.ssh_notes = "4C8G Ubuntu22.04，仅开放22/80/443"
        return plugin, hooks

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    def _fake_ssh(self, plugin):
        from src.ssh_ext import SSHClient
        plugin._ssh = SSHClient(
            plugin._ssh_config,
            opener=lambda cfg, t: object(),
            runner=lambda c, cmd, t: (0, f"ran:{cmd}", ""))
        plugin._ssh_agents._ssh = plugin._ssh

    async def test_tools_gated_by_configuration(self):
        plugin, hooks = await self._plugin(configure_ssh=False)
        names = {str(getattr(t, "name", ""))
                 for t in plugin._effective_tool_set(None).tools}
        for tool in ("ssh_exec", "ssh_agent_dispatch", "ssh_agent_control"):
            self.assertNotIn(tool, names, "未配置SSH时不该暴露SSH工具")
        bot = hooks.FakeBot()
        self.assertIn("未配置", await plugin.ssh_exec_tool(bot, "ls"))
        self.assertIn("未配置", await plugin.ssh_agent_dispatch_tool(bot, "装docker"))

    async def test_tools_visible_once_configured(self):
        plugin, hooks = await self._plugin(configure_ssh=True)
        names = {str(getattr(t, "name", ""))
                 for t in plugin._effective_tool_set(None).tools}
        for tool in ("ssh_exec", "ssh_info", "ssh_agent_dispatch",
                     "ssh_agent_status", "ssh_agent_control"):
            self.assertIn(tool, names)

    async def test_ssh_exec_tool_runs_via_fake_transport(self):
        plugin, hooks = await self._plugin(configure_ssh=True)
        self._fake_ssh(plugin)
        result = await plugin.ssh_exec_tool(hooks.FakeBot(), "whoami")
        self.assertIn("ran:whoami", result)

    async def test_ssh_info_tool_exposes_notes(self):
        plugin, hooks = await self._plugin(configure_ssh=True)
        self._fake_ssh(plugin)
        info = await plugin.ssh_info_tool(hooks.FakeBot())
        self.assertIn("仅开放22/80/443", info)
        self.assertIn("ubuntu@1.2.3.4:22", info)

    async def test_ssh_agent_dispatch_and_status(self):
        plugin, hooks = await self._plugin(configure_ssh=True)
        plugin.context.responses = ['{"done": true, "conclusion": "环境已就绪"}']
        import json as _json
        raw = await plugin.ssh_agent_dispatch_tool(hooks.FakeBot(), "检查环境")
        payload = _json.loads(raw)
        self.assertTrue(payload["ok"])
        session_id = payload["session_id"]
        self.assertTrue(await _wait_state(
            plugin._ssh_agents.get(session_id), {"done"}))
        status = await plugin.ssh_agent_status_tool(hooks.FakeBot(), session_id)
        self.assertIn("环境已就绪", status)

    async def test_dispatch_channel_ssh_exec(self):
        plugin, hooks = await self._plugin(configure_ssh=True)
        self._fake_ssh(plugin)
        result = await plugin._dispatch_task_action(
            "ssh_exec", {"command": "uname -a"}, None)
        self.assertIn("ran:uname -a", result)
        plugin.settings.ssh_host = ""
        blocked = await plugin._dispatch_task_action(
            "ssh_exec", {"command": "ls"}, None)
        self.assertIn("未配置", blocked)

    async def test_conf_page_masks_ssh_password(self):
        plugin, hooks = await self._plugin(configure_ssh=True)
        import astrbot.api.web as web
        plugin.raw_config["ssh_password"] = "supersecret"
        saved = getattr(web.request, "username", None)
        web.request.username = "admin"
        try:
            rows = await plugin._page_api().read("config", {})
        finally:
            web.request.username = saved
        self.assertNotIn("supersecret", str(rows), "页面绝不能回显密码原文")
        ssh = next(row for row in rows if row["key"] == "ssh_password")
        self.assertTrue(ssh["secret"])
        self.assertEqual("********", ssh["value"])


if __name__ == "__main__":
    unittest.main()
