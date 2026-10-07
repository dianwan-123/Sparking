# -*- coding: utf-8 -*-
"""provider 解析候选链测试：配置/会话偏好/全局默认全部指向死模型时，
必须落到一个还注册着的模型，而不是永远重试死模型（v0.28.5 实录）。"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _hooks():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks
    return hooks


class _Meta:
    def __init__(self, pid):
        self.id = pid


class _Provider:
    def __init__(self, pid):
        self.provider_config = {"id": pid}

    def meta(self):
        return _Meta(self.provider_config["id"])


class ResolveProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _plugin(self, global_provider: str | None):
        """global_provider：AstrBot 全局默认模型（可以指向**未注册**的死模型，
        正是实录场景——偏好还留着已删除的 deepseek）。"""
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        providers = [
            _Provider("Justdowork/claude-opus-4-8"),
            _Provider("gptluna_/gpt-5.6-luna"),
        ]

        class _Ctx(types.SimpleNamespace):
            def get_all_providers(self):
                return providers

            async def get_using_provider_async(self):
                if global_provider:
                    return _Provider(global_provider)
                return None

        plugin.context = _Ctx(**vars(plugin.context))
        return plugin, hooks

    async def test_all_candidates_dead_falls_to_registered(self):
        """配置/全局默认全是已删除的 deepseek：必须落到注册表里的活模型。"""
        plugin, hooks = await self._plugin("catapi/deepseek-v4.1-flash")
        plugin.settings.reply_provider_id = "catapi/deepseek-v4.1-flash"
        resolved = await plugin._resolve_provider("catapi/deepseek-v4.1-flash", None)
        self.assertIn(resolved, {
            "Justdowork/claude-opus-4-8", "gptluna_/gpt-5.6-luna"})
        # 幂等：再次解析不炸
        self.assertEqual(resolved, await plugin._resolve_provider(resolved, None))

    async def test_configured_alive_provider_kept(self):
        """配置指向注册中的 opus：原样保留，不回退。"""
        plugin, hooks = await self._plugin("catapi/deepseek-v4.1-flash")
        resolved = await plugin._resolve_provider(
            "Justdowork/claude-opus-4-8", None)
        self.assertEqual("Justdowork/claude-opus-4-8", resolved)

    async def test_empty_config_falls_to_global_when_alive(self):
        """配置为空：走全局默认；全局默认活着就直接用。"""
        plugin, hooks = await self._plugin("gptluna_/gpt-5.6-luna")
        resolved = await plugin._resolve_provider("", None)
        self.assertEqual("gptluna_/gpt-5.6-luna", resolved)

    async def test_event_conversation_preference_validated(self):
        """会话偏好（/provider 切的）也可能是死模型：必须被注册表校验。"""
        hooks = _hooks()
        plugin, _ = await self._plugin("catapi/deepseek-v4.1-flash")

        async def current(umo=""):
            return "catapi/deepseek-v4.1-flash"

        plugin._current_provider = current
        event = hooks.FakeEvent(hooks._group_raw("在吗", 10002), "在吗",
                                hooks.FakeBot(), wake=True)
        resolved = await plugin._resolve_provider("", event)
        self.assertIn(resolved, {
            "Justdowork/claude-opus-4-8", "gptluna_/gpt-5.6-luna"})

    async def test_warns_once_per_dead_provider(self):
        plugin, hooks = await self._plugin("catapi/deepseek-v4.1-flash")
        await plugin._resolve_provider("catapi/deepseek-v4.1-flash", None)
        await plugin._resolve_provider("catapi/deepseek-v4.1-flash", None)
        self.assertEqual(
            1, len(plugin._provider_fallback_warned),
            "同一死模型只警告一次，避免日志刷屏")


if __name__ == "__main__":
    unittest.main()
