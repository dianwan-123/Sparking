# -*- coding: utf-8 -*-
"""v0.33.2 回归：面板配置热刷新（SSH 填了却"未配置"）+ set_conf 落盘。

根因（实录）：AstrBotConfig 是普通 dict 子类，面板 save_config 会 merge 进同一
对象，但运行中的插件只在 __init__ 构建过一次 settings——保存后若热重载没生效
（或校验失败），运行实例永远拿旧值；我们自己 WebUI 的 set_conf 更是只改内存
从不落盘（setitem 不自动保存）。
"""
from __future__ import annotations

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


class ConfigHotRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _make_plugin(self):
        hooks = _hooks()
        config = hooks._plugin_config()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        async def _noop_setup():
            return None

        # 测试里绝不能让 SSH 配置真的触发 pip 安装 paramiko（污染本地环境）
        plugin._ssh_setup_task = _noop_setup
        return plugin, config

    async def test_ssh_fields_saved_after_init_take_effect(self):
        """面板在插件启动后才保存 SSH → 运行实例无需重载即可生效。"""
        plugin, config = await self._make_plugin()
        self.assertFalse(plugin._ssh_configured())

        plugin.raw_config["ssh_host"] = "120.220.76.164"
        plugin.raw_config["ssh_port"] = 19155
        plugin.raw_config["ssh_user"] = "root"
        plugin.raw_config["ssh_password"] = "secret"
        plugin.raw_config["ssh_notes"] = "ubuntu 22.04"

        self.assertTrue(plugin._ssh_configured(), "热刷新后 SSH 应就绪")
        self.assertEqual("120.220.76.164", plugin.settings.ssh_host)
        self.assertEqual(19155, plugin.settings.ssh_port)
        self.assertEqual("root@120.220.76.164:19155", plugin._ssh_config().target())

    async def test_tool_gating_sees_fresh_config(self):
        """SSH 工具的门控在配置热更新后应变化（此前永远看 __init__ 的旧值）。"""
        plugin, _ = await self._make_plugin()
        event = _hooks().FakeEvent(
            _hooks()._group_raw("hi", 4401), "hi", _hooks().FakeBot(), wake=True)
        before = {t.name for t in plugin._effective_tool_set(event).tools}
        self.assertNotIn("ssh_exec", before)

        plugin.raw_config["ssh_host"] = "1.2.3.4"
        plugin.raw_config["ssh_user"] = "root"
        plugin.raw_config["ssh_password"] = "pw"
        after = {t.name for t in plugin._effective_tool_set(event).tools}
        self.assertIn("ssh_exec", after)

    async def test_set_conf_persists_to_disk(self):
        """我们 WebUI 的 set_conf 必须落盘（AstrBotConfig setitem 不自动保存）。"""
        plugin, _ = await self._make_plugin()
        saved = []

        class _Config(dict):
            def save_config(self):
                saved.append(dict(self))

        plugin.raw_config = _Config(plugin.raw_config)
        result = await plugin._page_api().write(
            "config_set", {"key": "reply_provider_id", "value": "opus-x"})
        self.assertTrue(saved, "save_config 必须被调用")
        self.assertEqual("opus-x", saved[-1]["reply_provider_id"])
        self.assertEqual("opus-x", plugin.settings.reply_provider_id)
        self.assertIsNotNone(result)

    def test_signature_detects_change_only_once(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config())
        self.assertFalse(plugin._refresh_settings_if_changed())
        plugin.raw_config["ssh_host"] = "9.9.9.9"
        self.assertTrue(plugin._refresh_settings_if_changed())
        self.assertFalse(plugin._refresh_settings_if_changed())

    async def test_ssh_ready_injected_into_reply_context(self):
        """实录：SSH 配好了、工具可用，但回复 agent 没被告知，回用户
        「ip呢账号呢密码呢 一个没掏出来」——上下文必须显式带 ssh_ready。"""
        plugin, _ = await self._make_plugin()
        plugin.raw_config["ssh_host"] = "1.2.3.4"
        plugin.raw_config["ssh_user"] = "root"
        plugin.raw_config["ssh_password"] = "pw"
        event = _hooks().FakeEvent(
            _hooks()._group_raw("你再试试ssh", 4410), "你再试试ssh",
            _hooks().FakeBot(), wake=True)
        await plugin.on_onebot_event(event)
        import asyncio

        while plugin._tasks:
            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)
        prompt = "\n".join(
            str(call.get("prompt", "")) for call in plugin.context.llm_calls
            if isinstance(call, dict))
        self.assertIn("ssh_ready", prompt)
        self.assertIn("root@1.2.3.4:22", prompt)


if __name__ == "__main__":
    unittest.main()
