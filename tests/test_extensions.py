# -*- coding: utf-8 -*-
"""拓展系统测试：内置包门控、自定义拓展安装/调用、持久化。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.extensions import (
    BUILTIN_PACKS,
    ExtensionError,
    ExtensionRegistry,
)


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "extensions.json"

    def _registry(self) -> ExtensionRegistry:
        return ExtensionRegistry(self.path)

    def test_builtin_packs_cover_the_tool_groups(self):
        registry = self._registry()
        listed = {x["id"] for x in registry.list()}
        self.assertEqual(set(BUILTIN_PACKS), listed)
        self.assertTrue(registry.is_tool_enabled("browse"))
        self.assertTrue(registry.is_tool_enabled("web_search"))
        self.assertTrue(registry.is_tool_enabled("qq_send_group"))

    def test_disable_pack_gates_its_tools(self):
        registry = self._registry()
        self.assertTrue(registry.disable("browser"))
        self.assertFalse(registry.is_tool_enabled("browse"))
        self.assertFalse(registry.is_tool_enabled("browser_click"))
        # other packs unaffected
        self.assertTrue(registry.is_tool_enabled("web_search"))
        # core extension tools are never gated
        self.assertTrue(registry.is_tool_enabled("extension_list"))
        self.assertTrue(registry.is_tool_enabled("extension_call"))
        # re-enable restores
        self.assertTrue(registry.enable("browser"))
        self.assertTrue(registry.is_tool_enabled("browse"))

    def test_disable_unknown_pack_fails(self):
        registry = self._registry()
        self.assertFalse(registry.disable("nope"))
        self.assertFalse(registry.enable("nope"))

    def test_state_survives_restart(self):
        registry = self._registry()
        registry.disable("qzone")
        reopened = self._registry()
        self.assertFalse(reopened.is_tool_enabled("qzone_publish"))
        self.assertTrue(reopened.is_tool_enabled("browse"))

    async def test_install_and_call_custom_extension(self):
        registry = self._registry()
        calls: list[tuple[str, str]] = []

        def fake_urlopen(request, timeout=None):
            calls.append((request.get_full_url(), request.get_method()))

            class Response:
                status = 200
                headers = {"Content-Type": "application/json"}

                def read(self, amount=-1):
                    return json.dumps({"temp": 25}).encode("utf-8")

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

            return Response()

        import urllib.request

        import src.extensions as ext_module

        saved = urllib.request.urlopen
        saved_guard = ext_module.assert_browsable
        urllib.request.urlopen = fake_urlopen
        ext_module.assert_browsable = lambda url: url  # hermetic: no DNS
        try:
            extension = registry.install({
                "id": "weather",
                "name": "天气查询",
                "tools": [{
                    "name": "get_weather",
                    "description": "查城市天气",
                    "method": "GET",
                    "url": "https://api.example.com/weather?city={city}",
                    "params": ["city"],
                }],
            })
            self.assertEqual("weather", extension.id)
            output = await registry.call("weather", "get_weather", {"city": "北京"})
        finally:
            urllib.request.urlopen = saved
            ext_module.assert_browsable = saved_guard
        self.assertIn("25", output)
        self.assertIn("city=%E5%8C%97%E4%BA%AC", calls[0][0])
        self.assertEqual("GET", calls[0][1])

    def test_install_rejects_bad_manifests(self):
        registry = self._registry()
        with self.assertRaises(ExtensionError):
            registry.install({"id": "x"})                       # 无工具
        with self.assertRaises(ExtensionError):
            registry.install({"id": "x", "tools": [
                {"name": "t", "url": "ftp://x"}]})              # 非 http(s)
        # 非法字符被清洗成合法 ID（刻意设计，对 LLM 友好）
        sanitized = registry.install({"id": "BAD ID!!", "tools": [
            {"name": "t", "url": "https://x.com"}]})
        self.assertEqual("bad-id", sanitized.id)
        registry.uninstall("bad-id")
        with self.assertRaises(ExtensionError):
            registry.install({"id": "", "tools": [
                {"name": "t", "url": "https://x.com"}]})        # 空 ID

    def test_uninstall_only_custom(self):
        registry = self._registry()
        registry.install({"id": "demo", "tools": [
            {"name": "go", "url": "https://api.example.com/go"}]})
        self.assertTrue(registry.uninstall("demo"))
        self.assertFalse(registry.uninstall("demo"))
        # built-in packs cannot be uninstalled
        self.assertFalse(registry.uninstall("browser"))
        self.assertTrue(registry.is_tool_enabled("browse"))

    def test_custom_extension_survives_restart(self):
        registry = self._registry()
        registry.install({
            "id": "demo",
            "tools": [{"name": "go", "description": "走一个",
                       "url": "https://api.example.com/go"}],
        })
        reopened = self._registry()
        listed = {x["id"] for x in reopened.list()}
        self.assertIn("demo", listed)
        pairs = reopened.dynamic_tools()
        self.assertTrue(any(x.id == "demo" for x, _ in pairs))

    def test_disabled_custom_extension_not_dispatched(self):
        registry = self._registry()
        registry.install({"id": "demo", "tools": [
            {"name": "go", "url": "https://api.example.com/go"}]})
        registry.disable("demo")
        pairs = registry.dynamic_tools()
        self.assertFalse(any(x.id == "demo" for x, _ in pairs))
        registry.enable("demo")
        pairs = registry.dynamic_tools()
        self.assertTrue(any(x.id == "demo" for x, _ in pairs))


if __name__ == "__main__":
    unittest.main()
