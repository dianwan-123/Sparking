# -*- coding: utf-8 -*-
"""designer（绘图/设计）与 programmer（程序编写）功能测试。

覆盖：本机端口白名单、设计窗口 60 秒 TTL、项目存档、程序热重启、
TTL 到期清扫、registry 落盘、以及工具层与记忆留痕。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

import src.program_host as ph
from src.browser import allow_local_port, assert_browsable
from src.browser import BrowserError
from src.program_host import ProgramHost

FAKE_APP_V1 = (
    "class A:\n"
    "    def __call__(self, environ, start_response):\n"
    '        start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])\n'
    '        return [b"hello v1"]\n'
    "app = A()\n"
)
FAKE_APP_V2 = FAKE_APP_V1.replace("v1", "v2")


class _Start:
    def __call__(self, status, headers):
        self.status = status
        self.headers = headers


def _request(host: ProgramHost, path: str) -> tuple[int, str]:
    captured: list = []

    def start_response(status, headers):
        captured.append((status, headers))

    chunks = host._dispatch({"PATH_INFO": path, "REQUEST_METHOD": "GET"}, start_response)
    status = int(captured[0][0].split(" ", 1)[0]) if captured else 0
    body = b"".join(chunks).decode("utf-8", "replace")
    return status, body


class StudioHostTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.host = ProgramHost(
            Path(self._tmp.name) / "studio", port=8765,
            program_ttl_seconds=600.0,
            logger=lambda message: None)
        self.host.start()
        self.addCleanup(self.host.close)
        self.addCleanup(self._tmp.cleanup)

    def test_design_window_lives_and_expires(self):
        info = self.host.put_design("<h1>鹈鹕骑自行车</h1>")
        self.assertEqual(60, info["expires_in_seconds"])
        self.assertTrue(info["url"].startswith("http://127.0.0.1:"))
        status, body = _request(self.host, f"/d/{info['design_id']}")
        self.assertEqual(200, status)
        self.assertIn("鹈鹕骑自行车", body)
        # 过期（60 秒窗口）：404
        self.host._designs[info["design_id"]]["expires_at"] = 0.0
        status, _ = _request(self.host, f"/d/{info['design_id']}")
        self.assertEqual(404, status)

    def test_design_project_roundtrip(self):
        self.host.save_design_project(
            "鹈鹕骑自行车", "SVG 白描：鹈鹕骑着小自行车", "<svg/>")
        rows = self.host.list_design_projects()
        self.assertEqual(1, len(rows))
        self.assertEqual("鹈鹕骑自行车", rows[0]["title"])
        self.assertNotIn("content", rows[0], "列表不带正文")
        row = self.host.get_design_project(rows[0]["id"])
        self.assertEqual("<svg/>", row["content"])

    def test_program_run_hot_reload_and_ttl(self):
        original = ph.ensure_flask
        ph.ensure_flask = lambda: (True, "stub")
        self.addCleanup(setattr, ph, "ensure_flask", original)
        result = self.host.run_program("wordle", FAKE_APP_V1, "Wordle 猜词")
        self.assertTrue(result["ok"])
        self.assertTrue(result["url"].endswith("/p/wordle/"))
        self.assertTrue((Path(result["data_dir"])).is_dir(), "DATA_DIR 会建出来")
        status, body = _request(self.host, "/p/wordle/")
        self.assertEqual(200, status)
        self.assertIn("hello v1", body)
        # 热重启：同 id 重写代码立刻生效
        again = self.host.run_program("wordle", FAKE_APP_V2, "Wordle 猜词")
        self.assertTrue(again["ok"])
        _, body = _request(self.host, "/p/wordle/")
        self.assertIn("hello v2", body)
        # 坏代码：返回错误而不是崩掉宿主
        bad = self.host.run_program("bad", "raise ValueError('boom')", "坏")
        self.assertFalse(bad["ok"])
        self.assertIn("boom", bad["error"])
        # TTL 到期清扫
        self.host._apps["wordle"]["started_at"] = 0.0
        expired = self.host.sweep()
        self.assertIn("wordle", expired)
        status, body = _request(self.host, "/p/wordle/")
        self.assertEqual(404, status)
        self.assertIn("未在运行", body)

    def test_registry_survives_restart(self):
        original = ph.ensure_flask
        ph.ensure_flask = lambda: (True, "stub")
        self.addCleanup(setattr, ph, "ensure_flask", original)
        self.host.upsert_program("wordle", FAKE_APP_V1, "Wordle 猜词", "猜 5 个字母")
        self.host.archive_program("wordle")
        reopened = ProgramHost(
            Path(self._tmp.name) / "studio", port=8765, program_ttl_seconds=600.0)
        row = reopened.get_program("wordle")
        self.assertIsNotNone(row)
        self.assertEqual("猜 5 个字母", row["description"])
        self.assertTrue(row["archived"])
        self.assertIn("app = A()", row["code"])
        self.assertEqual([], reopened.running())


class LocalPortAllowTests(unittest.TestCase):
    def test_loopback_allowed_only_for_registered_port(self):
        with self.assertRaises(BrowserError):
            assert_browsable("http://127.0.0.1:9999/p/x/")
        allow_local_port(8765)
        url = assert_browsable("http://127.0.0.1:8765/p/x/")
        self.assertTrue(url.endswith("/p/x/"))
        assert_browsable("http://localhost:8765/d/abc")
        with self.assertRaises(BrowserError):
            assert_browsable("http://127.0.0.1:9999/")
        with self.assertRaises(BrowserError):
            assert_browsable("http://192.168.1.5:8765/")


def _hooks():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks
    return hooks


class _FakeMedia:
    def __init__(self):
        self.saved: list = []

    async def save_bytes(self, data, *, scope_id, kind, source_message_id="",
                         mime="", note=""):
        self.saved.append((len(data), scope_id, kind))
        import types
        return types.SimpleNamespace(item_id="media-123")


class _FakeDriver:
    def __init__(self):
        self.calls: list = []

    async def screenshot(self, url, full_page=False, **kwargs):
        self.calls.append((url, full_page, kwargs))
        return b"PNGBYTES"


class StudioToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _plugin(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin, hooks

    async def test_design_render_tool_saves_project_and_memory(self):
        plugin, hooks = await self._plugin()
        plugin.media = _FakeMedia()
        fake_driver = _FakeDriver()
        plugin._pw = fake_driver  # 注入假内核
        result = await plugin.design_render_tool(
            None, svg='<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60">'
                      '<text x="8" y="30">pelican</text></svg>',
            title="鹈鹕骑自行车", description="SVG 白描：鹈鹕骑小自行车", save=True)
        data = json.loads(result)
        self.assertTrue(data["ok"])
        self.assertEqual("media-123", data["media_id"])
        self.assertEqual(60, data["expires_in_seconds"])
        self.assertTrue(data["project_id"])
        # 项目已存
        projects = plugin._studio_host().list_design_projects()
        self.assertEqual(1, len(projects))
        # 留痕已入记忆（查询库）：先等 spawn 的留痕任务跑完
        while plugin._tasks:
            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)
        studio_scope = await plugin.storage.resolve_scope("aiocqhttp", "", "studio")
        events = await plugin.storage.recent_events([studio_scope], 5)
        self.assertTrue(any("[设计] 鹈鹕骑自行车" in e.text for e in events))
        # 渲染走的是 fallback_html（不需要真实网络）
        self.assertIsNone(fake_driver.calls[0][0])
        self.assertIn("fallback_html", fake_driver.calls[0][2])

    async def test_program_write_view_archive_flow(self):
        plugin, hooks = await self._plugin()
        original = ph.ensure_flask
        ph.ensure_flask = lambda: (True, "stub")
        self.addCleanup(setattr, ph, "ensure_flask", original)
        result = await plugin.program_write_tool(
            None, code=FAKE_APP_V1, title="Wordle 猜词",
            description="和 bot 轮流猜 5 个字母的词")
        data = json.loads(result)
        self.assertTrue(data["ok"], data)
        pid = data["program_id"]
        # 真实 HTTP 访问本机端口
        def fetch():
            with urllib.request.urlopen(data["url"], timeout=5) as response:
                return response.read().decode("utf-8")
        body = await asyncio.to_thread(fetch)
        self.assertIn("hello v1", body)
        # 存档：停止 + 入记忆
        archived = await plugin.program_archive_tool(None, pid)
        archived_data = json.loads(archived)
        self.assertTrue(archived_data["archived"])
        listed = json.loads(await plugin.program_list_tool(None))
        self.assertTrue(any(row["id"] == pid for row in listed["projects"]))
        self.assertEqual([], listed["running"])
        while plugin._tasks:
            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)
        studio_scope = await plugin.storage.resolve_scope("aiocqhttp", "", "studio")
        events = await plugin.storage.recent_events([studio_scope], 5)
        self.assertTrue(any("[程序存档] Wordle 猜词" in e.text for e in events))


if __name__ == "__main__":
    unittest.main()
