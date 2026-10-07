from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from src.agent_tools import (
    AgentToolService, AuthorizationError, ScopeBoundMemoryTools,
    authorize_admin_instruction, authorize_structured_command,
)
from src.astrbot_runtime import build_runtime_manifest


class Event:
    def __init__(self, text, admin=True, components=None, platform="aiocqhttp", group_id="100"):
        self.text = text
        self._admin = admin
        self._components = components
        self.platform_name = platform
        self.group_id = group_id
        self.sender_id = "admin-1"
        self.message_id = "message-1"

    async def is_admin(self):
        return self._admin

    def get_messages(self):
        return self._components if self._components is not None else [self.text]

    def get_platform_name(self):
        return self.platform_name

    def get_group_id(self):
        return self.group_id


class FakeDashboard:
    def __init__(self):
        self.calls = []

    async def update_plugin(self, name):
        self.calls.append(("update", name))
        return {"updated": name}

    async def install_market_plugin(self, name):
        self.calls.append(("install", name))
        return {}

    async def enable_plugin(self, name):
        self.calls.append(("enable", name))
        return {}

    async def disable_plugin(self, name):
        self.calls.append(("disable", name))
        return {}

    async def reload_plugin(self, name):
        self.calls.append(("reload", name))
        return {}

    async def search_market(self, query):
        return [query]


class AgentToolsTests(unittest.TestCase):
    def test_direct_admin_instruction_is_auditable(self):
        authorization = asyncio.run(authorize_admin_instruction(Event("更新插件 plugin-x")))
        self.assertTrue(authorization.authorized)
        self.assertEqual((authorization.action, authorization.target), ("update", "plugin-x"))
        self.assertEqual(len(authorization.command_digest), 64)
        self.assertEqual(authorization.actor_id, "admin-1")

    def test_rejects_non_admin_question_quote_and_non_text(self):
        events = [
            Event("更新插件 x", admin=False),
            Event("能否更新插件 x？"),
            Event("> 更新插件 x"),
            Event("更新插件 x", components=[{"type": "image", "url": "x"}]),
        ]
        for event in events:
            with self.subTest(text=event.text):
                self.assertFalse(asyncio.run(authorize_admin_instruction(event)).authorized)

    def test_service_rechecks_and_requires_exact_current_command(self):
        dashboard = FakeDashboard()
        service = AgentToolService(
            SimpleNamespace(), dashboard, group_authorizer=lambda group: group == "100"
        )
        result = asyncio.run(service.update_plugin(Event("更新插件 plugin-x"), "plugin-x"))
        self.assertEqual(dashboard.calls, [("update", "plugin-x")])
        self.assertTrue(result["authorization"]["authorized"])
        with self.assertRaises(AuthorizationError):
            asyncio.run(service.enable_plugin(Event("更新插件 plugin-x"), "plugin-x"))
        self.assertEqual(len(dashboard.calls), 1)

    def test_runtime_manifest_filters_inactive_and_never_exposes_config(self):
        star = SimpleNamespace(
            plugin_id="p", name="p", module_path="plugins.p", activated=True,
            desc="untrusted plugin", short_desc="short", config={"api_key": "secret"},
            author="a", version="1", display_name="P",
        )
        context = SimpleNamespace(get_all_stars=lambda: [star])
        tools = [
            SimpleNamespace(name="active", active=True, description="untrusted tool", parameters={"type": "object", "properties": {"x": {"description": "input"}}}, handler_module_path="plugins.p.tool"),
            SimpleNamespace(name="off", active=False, description="off", parameters={}),
        ]
        manifest = asyncio.run(build_runtime_manifest(context, SimpleNamespace(func_tool=SimpleNamespace(tools=tools))))
        self.assertEqual([x["name"] for x in manifest["effective_tools"]], ["active"])
        self.assertEqual(manifest["effective_tools"][0]["plugin_id"], "p")
        self.assertFalse(manifest["plugins"][0]["description_trusted"])
        self.assertNotIn("config", repr(manifest))
        self.assertNotIn("secret", repr(manifest))

    def test_async_component_getter_fails_closed_without_flattened_fallback(self):
        class AsyncEvent(Event):
            async def get_messages(self):
                return ["更新插件 plugin-x"]

        authorization = asyncio.run(
            authorize_admin_instruction(AsyncEvent("更新插件 plugin-x"))
        )
        self.assertFalse(authorization.authorized)

    def test_platform_group_gate_and_audit_all_outcomes(self):
        audits = []
        dashboard = FakeDashboard()
        service = AgentToolService(
            SimpleNamespace(), dashboard,
            group_authorizer=lambda group: group == "100",
            audit_callback=audits.append,
        )
        asyncio.run(service.update_plugin(Event("更新插件 plugin-x"), "plugin-x"))
        self.assertEqual(audits[-1]["outcome"], "success")
        with self.assertRaises(PermissionError):
            asyncio.run(service.update_plugin(
                Event("更新插件 plugin-x", platform="discord"), "plugin-x"
            ))
        self.assertEqual(audits[-1]["outcome"], "rejected")
        with self.assertRaises(PermissionError):
            asyncio.run(service.update_plugin(
                Event("更新插件 plugin-x", group_id="200"), "plugin-x"
            ))
        self.assertEqual(len(audits), 3)

    def test_structured_command_rechecks_admin_and_exact_operation(self):
        authorization = asyncio.run(authorize_structured_command(
            Event("/plugin update plugin-x"), action="update", resource="plugin",
            target="plugin-x",
        ))
        self.assertTrue(authorization.authorized)
        self.assertEqual(authorization.source, "slash")
        dashboard = FakeDashboard()
        service = AgentToolService(
            SimpleNamespace(), dashboard, group_authorizer=lambda _: True
        )
        asyncio.run(service.manage_structured_plugin(
            Event("flattened text ignored"), "update", "plugin-x"
        ))
        with self.assertRaises(AuthorizationError):
            asyncio.run(service.manage_structured_plugin(
                Event("ignored", admin=False), "update", "plugin-x"
            ))

    def test_scope_bound_memory_facade_blocks_scope_override_and_bounds_output(self):
        calls = []

        async def search(scope, query, **kwargs):
            calls.append((scope, query, kwargs))
            return ["x" * 100 for _ in range(20)]

        tools = ScopeBoundMemoryTools(
            "fixed-scope", search_callback=search, max_limit=3, max_output_chars=210
        )
        result = asyncio.run(tools.search(
            "query", limit=999, scope_id="attacker-scope"
        ))
        self.assertEqual(calls[0][0], "fixed-scope")
        self.assertEqual(calls[0][2]["limit"], 3)
        self.assertLessEqual(len(result), 2)

    def test_runtime_manifest_event_and_skill_facts(self):
        context = SimpleNamespace(
            astrbot_version="4.28.0", get_all_stars=lambda: [],
            skill_manager=SimpleNamespace(list_skills=lambda: [
                SimpleNamespace(name="one", description="untrusted", active=True,
                                source_type="local_only", readonly=False),
                SimpleNamespace(name="off", description="x", active=False),
            ]),
        )
        event = Event("hello")
        event.self_id = "bot-1"
        event.session_id = "session-1"
        manifest = asyncio.run(build_runtime_manifest(
            context, SimpleNamespace(func_tool=None), event
        ))
        self.assertEqual(manifest["runtime"]["astrbot_version"], "4.28.0")
        self.assertEqual(manifest["event"]["platform"], "aiocqhttp")
        self.assertEqual(manifest["event"]["group_id"], "100")
        self.assertEqual([x["name"] for x in manifest["skills"]], ["one"])
        self.assertFalse(manifest["skills"][0]["description_trusted"])


if __name__ == "__main__":
    unittest.main()
