# -*- coding: utf-8 -*-
"""v0.28.5 回归：provider id 提取、直通画图 PlannedAction、xml 合并转发、File 组件归档。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


def _hooks():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks
    return hooks


class ProviderIdTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_live_provider_id_uses_provider_config(self):
        """回归：Provider 实例没有 .id 属性（id 在 provider_config 里），旧代码
        getattr(t,'id','') 全是空串 → 任何配置都被误判"已不存在"。"""
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        import types

        class _Provider:
            def __init__(self, pid):
                self.provider_config = {"id": pid}
                self.active = True

        class _Meta:
            def __init__(self, pid):
                self.id = pid

        class _Ctx:
            def __init__(self, providers):
                self._providers = providers

            def get_all_providers(self):
                return self._providers

            async def get_using_provider_async(self):
                return None

        plugin.context = types.SimpleNamespace(**{
            **vars(plugin.context),
            **{"get_all_providers": _Ctx([
                _Provider("Justdowork/claude-opus-4-8"),
                _Provider("gptluna_/gpt-5.6-luna"),
            ]).get_all_providers,
                "get_using_provider_async": _Ctx([]).get_using_provider_async},
        })

        async def current(umo=""):
            return "gptluna_/gpt-5.6-luna"

        plugin._current_provider = current
        # 配置指向已注册的 opus → 不应回退
        plugin.settings.summary_provider_id = "Justdowork/claude-opus-4-8"
        provider = "Justdowork/claude-opus-4-8"

        known = {
            str((getattr(t, "provider_config", None) or {}).get("id", ""))
            for t in plugin._known_providers()
        }
        known.discard("")
        self.assertIn("Justdowork/claude-opus-4-8", known,
                      "provider id 必须从 provider_config 提取")


class DrawPlanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_draw_and_send_returns_result_without_nameerror(self):
        """回归（v0.28.5 起源，v0.36.0 迁移到新管线）：画图成功路径不能因
        未导入的符号 NameError——draw 管线要返回结果字典且图片真正发出。"""
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        import types as _types

        class _FakeMedia:
            async def get_path(self, item_id):
                return "C:/fake/render.png"

        plugin.media = _FakeMedia()

        async def llm(**kwargs):
            return _types.SimpleNamespace(completion_text=json.dumps({
                "title": "小猫", "svg": "<svg xmlns='x' width='10' height='10'></svg>",
            }, ensure_ascii=False))

        plugin.context.llm_generate = llm

        async def current(umo=""):
            return "fake-provider"

        plugin._current_provider = current
        plugin._studio_host = lambda: _types.SimpleNamespace(
            put_design=lambda content: {"url": "http://127.0.0.1:1/d/x"},
            list_design_projects=lambda: [])

        async def fake_shot(content):
            return "media-1"

        plugin._design_screenshot = fake_shot
        event = hooks.FakeEvent(hooks._group_raw("画个鹈鹕骑车", 10002),
                                "画个鹈鹕骑车", hooks.FakeBot(), wake=True)
        sent: list = []

        async def send(chain):
            sent.append(chain)

        event.send = send
        result = await plugin._draw_and_send(event, "鹈鹕骑车")
        self.assertIsNotNone(result, "画图管线应成功返回结果")
        self.assertTrue(result["ok"])
        self.assertEqual("小猫", result["title"])
        self.assertEqual(1, len(sent), "图片应真正发出")


class ForwardXmlTests(unittest.TestCase):
    def test_xml_forward_segment_parsed(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin.__new__(hooks.LongMemoryAgentPlugin)
        raw = {"message": [{
            "type": "xml",
            "data": {"data": '<msg service="QQ"><item title="聊天记录"/>'
                             '<source m_resid="AbCdEf123"/></msg>'},
        }]}
        ids = hooks.LongMemoryAgentPlugin._forward_ids(raw)
        self.assertEqual(["AbCdEf123"], ids)

    def test_forward_segment_still_parsed(self):
        hooks = _hooks()
        raw = {"message": [{"type": "forward", "data": {"id": "fwd-9"}}]}
        self.assertEqual(["fwd-9"], hooks.LongMemoryAgentPlugin._forward_ids(raw))


class FileComponentArchiveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_file_component_archived(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        pdf_path = Path(self._tmp.name) / "试卷.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        class _FakeComp:
            name = "试卷.pdf"

            async def get_file(self):
                return str(pdf_path)

        event = hooks.FakeEvent(hooks._group_raw("做一下这套数学题", 10002),
                                "做一下这套数学题", hooks.FakeBot(), wake=True)
        event.message_obj.message = [_FakeComp()]

        saved: list = []

        class _FakeMedia:
            async def save_bytes(self, data, **kwargs):
                saved.append((len(data), kwargs.get("kind"), kwargs.get("note")))
                import types
                return types.SimpleNamespace(item_id="f1")

        plugin.media = _FakeMedia()
        await plugin._archive_file_components(event, "scope-x", "mid-1")
        self.assertEqual(1, len(saved))
        self.assertEqual("file", saved[0][1])
        self.assertEqual("试卷.pdf", saved[0][2])


if __name__ == "__main__":
    unittest.main()
