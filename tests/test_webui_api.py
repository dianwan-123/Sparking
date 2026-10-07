# -*- coding: utf-8 -*-
"""WebUI（重写版）回归：删除真的删、计数用真键名、面板按需分片、能力总表可见。

用户实录：①"网页端所有删除功能都没法正常删数据"；②界面有错别字；③很卡。
定位到的真因（本地实测）：
- `delete_message` 只认**内部编号**，前端拿到的是 QQ 消息号 → 静默删 0 条；
- `storage.stats()` 的键是 `message_identities`，前端却读 `messages` → 计数全空；
- 旧前端一次性拉全量数据、无分页。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class PageApiTests(unittest.IsolatedAsyncioTestCase):
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
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "群")
        plugin._known_scopes["123"] = scope
        from src.models import NormalizedMessage

        for index in range(5):
            await plugin.ingest.ingest(NormalizedMessage(
                platform="aiocqhttp", account_id="bot1", conversation_id="123",
                upstream_message_id=f"7700{index}", sender_id="10002", sender_name="甲",
                text=f"第{index}条测试消息",
                occurred_at=f"2026-10-06T12:0{index}:00+00:00",
                raw_event={"message_id": f"7700{index}"}, parts=[],
                event_type="message.created"))
        return plugin, scope

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_delete_accepts_both_id_spaces(self):
        """两条删除路径都要真删（旧实现只认内部编号 → 前端点删除没反应）。"""
        plugin, scope = await self._plugin()
        api = plugin._page_api()

        out = await api.write("message_delete", {"group_id": "123", "message_id": "77000"})
        self.assertEqual(1, out["removed"], "按 QQ 消息号删除必须生效")
        remaining = await plugin.storage.recent_messages(scope, 10)
        self.assertEqual(4, len(remaining))

        target = remaining[0]
        out2 = await api.write("message_delete",
                               {"group_id": "123", "message_id": target.message_id})
        self.assertEqual(1, out2["removed"], "内部编号同样可删")
        remaining2 = await plugin.storage.recent_messages(scope, 10)
        self.assertEqual(3, len(remaining2))

        with self.assertRaises(Exception):
            await api.write("message_delete",
                            {"group_id": "123", "message_id": "不存在的id"})

    async def test_user_delete_and_group_wipe(self):
        plugin, scope = await self._plugin()
        api = plugin._page_api()
        out = await api.write("user_delete", {"group_id": "123", "user_id": "10002"})
        self.assertEqual(5, out["removed"])
        self.assertEqual([], await plugin.storage.recent_messages(scope, 10))

    async def test_overview_uses_real_stat_keys(self):
        """概览计数必须来自真实键名（旧前端读 messages 恒为空）。"""
        plugin, _scope = await self._plugin()
        data = await plugin._page_api().read("overview", {})
        counts = data["counts"]
        self.assertEqual(5, counts["messages"])
        self.assertEqual(1, counts["scopes"])
        self.assertIn("reply", data["models"])

    async def test_messages_paging_and_keyword(self):
        plugin, _scope = await self._plugin()
        api = plugin._page_api()
        page = await api.read("messages", {"group_id": "123", "limit": 2})
        self.assertEqual(2, len(page["items"]))
        self.assertEqual(5, page["total"])
        cursor = page["items"][0]["seq"]  # 本页最旧那条 → 取更早
        older = await api.read("messages", {"group_id": "123", "limit": 10,
                                            "before_seq": cursor})
        self.assertEqual(3, len(older["items"]), "游标分页要能取更早且不重复")
        hit = await api.read("messages", {"group_id": "123", "q": "第3条"})
        self.assertEqual(1, len(hit["items"]))

    async def test_message_view_exposes_both_ids(self):
        plugin, _scope = await self._plugin()
        page = await plugin._page_api().read("messages", {"group_id": "123", "limit": 1})
        row = page["items"][0]
        self.assertIn("message_id", row)
        self.assertIn("qq_id", row, "前端需要 QQ 号来展示与删除")

    async def test_capabilities_lists_astrbot_and_plugin_tools(self):
        plugin, _scope = await self._plugin()
        data = await plugin._page_api().read("capabilities", {})
        names = {item["name"] for item in data["items"]}
        self.assertIn("web_search", names, "AstrBot/其它插件工具要出现在能力总表")
        self.assertIn("memory_catalog", names, "本插件工具也要在")
        sources = {item["source"] for item in data["items"]}
        self.assertIn("本插件", sources)

    async def test_config_read_write_roundtrip(self):
        plugin, _scope = await self._plugin()
        api = plugin._page_api()
        rows = await api.read("config", {})
        keys = {row["key"] for row in rows}
        self.assertIn("wake_merge_seconds", keys)
        self.assertTrue(all("group" in row for row in rows), "配置要分组，界面才好浏览")
        await api.write("config_set", {"key": "wake_merge_seconds", "value": 0})
        self.assertEqual(0, plugin.settings.wake_merge_seconds)

    async def test_whitelist_and_mood_and_backup(self):
        plugin, _scope = await self._plugin()
        api = plugin._page_api()
        out = await api.write("whitelist_set", {"groups": ["123", "456"]})
        self.assertEqual(["123", "456"], out["groups"])
        mood = await api.write("mood_set", {"mood": "雀跃", "intensity": 0.8, "note": "测试"})
        self.assertEqual("雀跃", mood["mood"]["mood"])
        backup = await api.write("backup", {})
        self.assertTrue(str(backup["backup"]).endswith(".db"))
        listed = await api.read("backups", {})
        self.assertTrue(any(item["name"] == backup["backup"] for item in listed))

    async def test_order_dispatches_agent_loop(self):
        """「给 bot 下令」= 写进任务表 + 立刻跑一轮带工具的自主行动。"""
        plugin, scope = await self._plugin()
        spawned: list = []
        plugin._spawn = spawned.append

        async def fake_loop(scope_id, task, reason=""):
            return {"results": []}

        plugin._autonomous_action_loop = fake_loop
        out = await plugin._page_api().write(
            "order", {"group_id": "123", "text": "把今天的神人发言拼成合并记录"})
        self.assertTrue(out["dispatched"])
        self.assertEqual(1, len(spawned))
        todos = await plugin.storage.list_todos(scope)
        self.assertTrue(any("神人发言" in str(item.get("content", "")) for item in todos))

    async def test_unknown_scope_is_reported(self):
        plugin, _scope = await self._plugin()
        with self.assertRaises(Exception) as ctx:
            await plugin._page_api().read("messages", {"group_id": "999999"})
        self.assertIn("未知", str(ctx.exception))

    async def test_page_routes_registered_and_unregistered(self):
        plugin, _scope = await self._plugin()
        routes = [path for path, _handler in plugin.context.registered_web_apis]
        self.assertTrue(any(path.endswith("/overview") for path in routes))
        self.assertTrue(any(path.endswith("/action") for path in routes))
        plugin._unregister_page_routes()
        routes2 = [path for path, _handler in plugin.context.registered_web_apis]
        self.assertFalse(any("/page" in path for path in routes2))


class FrontendContractTests(unittest.TestCase):
    """前后端契约与文案（错字/端点合法性）。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.html = (root / "pages" / "memory" / "index.html").read_text(encoding="utf-8")
        cls.js = (root / "pages" / "memory" / "app.js").read_text(encoding="utf-8")

    def test_no_placeholder_typos(self):
        for text in (self.html, self.js):
            self.assertNotIn("LLLM", text, "界面里不能有 LLLM 这类错字")
            self.assertNotIn("impessions", text, "端点名不能拼错")
            self.assertNotIn("modles", text)

    def test_endpoints_are_bridge_legal(self):
        """桥接要求 endpoint 只能是纯路径段（不得含 ? :// \\ #）。"""
        for match in re.finditer(r'get\(\s*"([^"]+)"', self.js):
            endpoint = match.group(1)
            self.assertNotIn("?", endpoint)
            self.assertNotIn("://", endpoint)
            self.assertNotIn("#", endpoint)
            self.assertFalse(endpoint.startswith("/"))

    def test_frontend_calls_match_backend_routes(self):
        from src.webui import READ_ROUTES, WRITE_ACTIONS

        reads = set(re.findall(r'get\(\s*"([^"]+)"', self.js))
        writes = set(re.findall(r'post\(\s*"([^"]+)"', self.js))
        known_reads = {route for route, _desc in READ_ROUTES}
        known_writes = {action for action, _desc in WRITE_ACTIONS}
        self.assertTrue(reads <= known_reads, f"前端读了未定义的数据面：{reads - known_reads}")
        self.assertTrue(writes <= known_writes, f"前端调了未定义的操作：{writes - known_writes}")

    def test_no_framework_imports(self):
        """零依赖：不引外部 CDN（内网/离线也能开）。"""
        for text in (self.html, self.js):
            self.assertNotIn("cdn.", text)
            self.assertNotIn("unpkg", text)
            self.assertNotIn("jsdelivr", text)


if __name__ == "__main__":
    unittest.main()


class CapabilityDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    """用户实录："bot 不会使用知识库和网络搜索，以及插件工具和 astrbot 工具"。

    工具集本身是全的（`get_full_tool_set` 含内置与各插件），缺的是**发现机制**：
    60+ 工具时模型不知道该用哪个。修法：①`find_tools` 按关键词搜全部可用工具
    （含 AstrBot 内置与其它插件的）；②agent 提示词里给一份运行时能力清单。
    """

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

    async def test_find_tools_searches_all_sources(self):
        plugin, hooks = await self._plugin()
        event = hooks.FakeEvent(hooks._group_raw("找工具", 9201), "找工具",
                                hooks.FakeBot(), wake=True)
        result = await plugin.find_tools_tool(event, query="搜索")
        self.assertIn("web_search", result, "要能搜到搜索类工具")
        result3 = await plugin.find_tools_tool(event, query="知识库")
        self.assertIn("kb_search", result3, "中文关键词要能命中英文工具名（知识库→kb）")
        result2 = await plugin.find_tools_tool(event, query="记忆")
        self.assertIn("memory_catalog", result2, "也要能搜到本插件工具")

    async def test_find_tools_empty_lists_by_source(self):
        plugin, hooks = await self._plugin()
        event = hooks.FakeEvent(hooks._group_raw("列工具", 9202), "列工具",
                                hooks.FakeBot(), wake=True)
        out = json.loads(await plugin.find_tools_tool(event, query=""))
        self.assertIn("count", out)
        self.assertTrue(out["count"] > 0)

    async def test_find_tools_is_never_gated(self):
        from src.extensions import _CORE_TOOLS

        self.assertIn("find_tools", _CORE_TOOLS, "能力发现工具不能被拓展开关关掉")

    async def test_agent_prompt_lists_external_capabilities(self):
        plugin, _hooks_mod = await self._plugin()
        prompt = plugin._agent_reply_system_prompt("你是群友。")
        self.assertIn("可用能力", prompt)
        self.assertIn("find_tools", prompt)
        self.assertIn("search_wyc_tools", prompt, "外部工具名要出现在能力清单里")

    async def test_capability_digest_splits_own_and_external(self):
        plugin, _hooks_mod = await self._plugin()
        digest = plugin._capability_digest()
        self.assertIn("memory_catalog", digest["own"])
        self.assertIn("search_wyc_tools", digest["external"],
                      "其它插件的工具要归到 external，界面才能分清来源")


class BridgeContractTests(unittest.TestCase):
    """实录："插件页桥接不可用"——三个坑（在模块加载时就抓桥接对象、没 await ready()、
    端点没带 page/ 前缀）都已修，并有同源 fetch 兜底。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.js = (root / "pages" / "memory" / "app.js").read_text(encoding="utf-8")

    def test_bridge_is_looked_up_lazily(self):
        self.assertNotIn("const PAGE = window.AstrBotPluginPage", self.js,
                         "不能在模块加载时就抓桥接对象（宿主注入可能更晚）")
        self.assertIn("function bridgeObject()", self.js)
        self.assertIn("window.AstrBotPluginPage", self.js)

    def test_bridge_ready_is_awaited_with_retry(self):
        self.assertIn("async function bridgeReady(", self.js)
        self.assertIn("await bridge.ready()", self.js)
        self.assertIn("setTimeout", self.js, "要重试等待注入")

    def test_endpoints_carry_page_prefix(self):
        self.assertIn('return "page/" + String(path', self.js,
                      "桥接端点必须带 page/ 前缀（参照 self_learning dashboard）")
        self.assertNotIn('bridge.apiGet(path', self.js)

    def test_same_origin_fallback_exists(self):
        self.assertIn("async function sameOrigin(", self.js)
        self.assertIn('"/" + PLUGIN + "/" + endpointOf(path)', self.js)
        self.assertIn('credentials: "same-origin"', self.js)

    def test_unwrap_handles_all_envelopes(self):
        for marker in ('body.ok === false', 'body.status === "error"', "body.data !== undefined"):
            self.assertIn(marker, self.js)


class ReaderShapeTests(unittest.IsolatedAsyncioTestCase):
    """实录（截图）：记忆账本整列显示"记忆 — —"——CatalogEntry 是
    dataclass(slots=True)，**没有 __dict__**，读字段必须逐属性取。"""

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
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "群")
        plugin._known_scopes["123"] = scope
        return plugin, scope

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_catalog_fields_are_populated(self):
        plugin, scope = await self._plugin()
        db = plugin.storage._conn()
        await db.execute(
            "INSERT INTO memory_items(memory_id,scope_id,kind,subject,status,confidence,"
            "idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("mem-1", scope, "fact", "小明喜欢猫", "active", 0.8, "k1",
             "2026-10-06T12:00:00+00:00", "2026-10-06T12:00:00+00:00"))
        await db.execute(
            "INSERT INTO catalog_entries(entry_id,memory_id,scope_id,kind,subject,value,"
            "status,confidence,evidence_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("entry-1", "mem-1", scope, "fact", "小明喜欢猫", "他说过家里有两只猫",
             "active", 0.8, "[]", "2026-10-06T12:00:00+00:00"))
        await db.commit()

        rows = await plugin._page_api().read("catalog", {"group_id": "123"})
        self.assertTrue(rows, "账本不能是空的")
        row = rows[0]
        self.assertEqual("entry-1", row["entry_id"])
        self.assertEqual("fact", row["kind"], "kind 必须有值（截图里是空的）")
        self.assertEqual("小明喜欢猫", row["subject"])
        self.assertIn("两只猫", row["value"])
        self.assertEqual("active", row["status"])

    async def test_sticker_and_media_readers_return_fields(self):
        plugin, _scope = await self._plugin()
        stickers = await plugin._page_api().read("stickers", {})
        self.assertIn("count", stickers)
        self.assertIn("items", stickers)
        media = await plugin._page_api().read("media", {"group_id": "123"})
        self.assertIn("items", media)

    async def test_group_wipe_purges_scope(self):
        plugin, scope = await self._plugin()
        from src.models import NormalizedMessage

        for index in range(3):
            await plugin.ingest.ingest(NormalizedMessage(
                platform="aiocqhttp", account_id="bot1", conversation_id="123",
                upstream_message_id=f"6600{index}", sender_id="10002", sender_name="甲",
                text=f"待清空{index}", occurred_at=f"2026-10-06T13:0{index}:00+00:00",
                raw_event={"message_id": f"6600{index}"}, parts=[],
                event_type="message.created"))
        out = await plugin._page_api().write("group_wipe", {"group_id": "123"})
        self.assertEqual(3, out["removed"], "群记忆必须真的删掉")
        self.assertEqual([], await plugin.storage.recent_messages(scope, 10))
        stats = await plugin.storage.stats(scope)
        self.assertEqual(0, stats.get("message_identities", 0))

    async def test_wipe_all_requires_confirm_word(self):
        plugin, _scope = await self._plugin()
        with self.assertRaises(Exception) as ctx:
            await plugin._page_api().write("wipe_all", {"confirm": "随便"})
        self.assertIn("确认清空", str(ctx.exception))


class NoNativeDialogsTests(unittest.TestCase):
    """插件页在 iframe 沙箱里——原生 confirm()/prompt() 会被拦或返回空值，
    实录"删除全部记忆点了显示已取消""群记忆无法删除"。全部改用页面内模态框。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.js = (root / "pages" / "memory" / "app.js").read_text(encoding="utf-8")
        cls.css = (root / "pages" / "memory" / "style.css").read_text(encoding="utf-8")

    def test_no_native_dialogs_in_code(self):
        self.assertNotIn("!confirm(", self.js)
        self.assertNotIn("= prompt(", self.js)
        self.assertNotIn("window.confirm", self.js)
        self.assertNotIn("window.prompt", self.js)

    def test_in_page_modal_used_everywhere(self):
        self.assertIn("function askConfirm(", self.js)
        self.assertGreaterEqual(self.js.count("askConfirm({"), 8,
                                "所有危险操作都要走页面内模态框")
        self.assertIn(".modal-overlay", self.css)

    def test_modal_supports_confirm_word(self):
        self.assertIn("confirmWord", self.js)


class MemoryForestTests(unittest.IsolatedAsyncioTestCase):
    """新增功能：记忆森林（树状可视化，可展开/编辑/删除/新增；原记忆页不动）。
    记忆含时间且层级不同粒度不同：L1 精确到分钟 / L2 天 / L3 月 / 证据消息分钟 /
    账本与印象用相对时间。"""

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
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "群")
        plugin._known_scopes["123"] = scope
        return plugin, scope

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_forest_layers_and_lazy_children(self):
        plugin, scope = await self._plugin()
        forest = await plugin._page_api().read("memory_tree", {})
        self.assertTrue(forest["roots"], "至少有一棵树（每群一棵）")
        root = forest["roots"][0]
        self.assertEqual("scope", root["type"])
        categories = await plugin._page_api().read("memory_tree", {"node": root["id"]})
        labels = [node["label"] for node in categories["children"]]
        for expected in ("分层摘要", "记忆账本", "人物印象", "群话题"):
            self.assertIn(expected, labels)

    def test_time_granularity_by_level(self):
        from src.webui import PageAPI

        stamp = "2026-10-06T12:34:56+00:00"
        self.assertEqual("2026-10-06 12:34", PageAPI._time_text(stamp, "minute"))
        self.assertEqual("2026-10-06", PageAPI._time_text(stamp, "day"))
        self.assertEqual("2026-10", PageAPI._time_text(stamp, "month"))
        self.assertIn("分钟前", PageAPI._time_text(
            __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(), "relative"))

    async def test_create_edit_delete_memory_node(self):
        plugin, scope = await self._plugin()
        api = plugin._page_api()
        created = await api.write("memory_node_save", {
            "group_id": "123", "type": "catalog", "subject": "小明喜欢猫",
            "value": "家里两只布偶", "kind": "fact"})
        self.assertTrue(created.get("created"))
        nodes = await api.read("memory_tree", {"node": f"cat:{scope}:catalog"})
        self.assertEqual(1, len(nodes["children"]))
        node = nodes["children"][0]
        self.assertTrue(node["editable"], "账本节点要能编辑")
        self.assertTrue(node["time"], "记忆节点必须带时间")
        self.assertEqual("相对时间", node["time_granularity"])

        edited = await api.write("memory_node_save", {
            "group_id": "123", "node_id": node["id"],
            "subject": "小明超喜欢猫", "value": "两只布偶 一只叫团子"})
        self.assertTrue(edited.get("updated"))
        nodes2 = await api.read("memory_tree", {"node": f"cat:{scope}:catalog"})
        self.assertEqual("小明超喜欢猫", nodes2["children"][0]["label"])

        removed = await api.write("memory_node_delete",
                                  {"group_id": "123", "node_id": node["id"]})
        self.assertEqual(1, removed["removed"])
        nodes3 = await api.read("memory_tree", {"node": f"cat:{scope}:catalog"})
        self.assertEqual([], nodes3["children"])

    async def test_tree_page_files_and_bridge_contract(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "pages" / "tree" / "index.html").read_text(encoding="utf-8")
        js = (root / "pages" / "tree" / "app.js").read_text(encoding="utf-8")
        page = json.loads((root / "pages" / "tree" / "_page.json").read_text(encoding="utf-8"))
        self.assertIn("tree", page["title"]["i18n_key"])
        self.assertIn("function bridgeObject()", js, "桥接要惰性取")
        self.assertIn("await bridge.ready()", js)
        self.assertIn('"page/" + String(path', js, "端点要带 page/ 前缀")
        self.assertNotIn("!confirm(", js, "插件页不能用原生 confirm")
        self.assertIn("memory_tree", js)
        # 原有记忆页必须原样保留（用户明确要求"不要修改或删除"）
        self.assertTrue((root / "pages" / "memory" / "index.html").is_file())


class ImpressionSharingTests(unittest.IsolatedAsyncioTestCase):
    """实录："bot 的人物印象每个群不互通，一个人在两个群会有不同的印象"
    → 读的时候合并成一份；名字为空时自动继承任意群里已有的名字。"""

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
        group_a = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "群A")
        group_b = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "456", "群B")
        plugin._known_scopes["123"] = group_a
        plugin._known_scopes["456"] = group_b
        return plugin, group_a, group_b

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_same_person_merges_across_groups(self):
        plugin, group_a, group_b = await self._plugin()
        await plugin.storage.upsert_impression(
            group_a, "10002", display_name="小明", impression="在 A 群聊技术很多",
            tags=["技术"])
        await plugin.storage.upsert_impression(
            group_b, "10002", display_name="小明", impression="在 B 群爱发梗图",
            tags=["梗图"])
        rows = await plugin.storage.list_impressions((group_a, group_b))
        self.assertEqual(1, len(rows), "同一个人只该有一条（跨群互通）")
        row = rows[0]
        self.assertEqual("10002", row["user_id"])
        self.assertEqual(2, len(row["groups"]), "要能看出他出现在哪些会话")
        self.assertEqual({"技术", "梗图"}, set(row["tags"]), "标签取并集")

    async def test_name_is_inherited_across_groups(self):
        plugin, group_a, group_b = await self._plugin()
        await plugin.storage.upsert_impression(group_a, "10003", display_name="阿花")
        # B 群里没给名字 → 应自动继承 A 群的名字（自己给人物标名字）
        await plugin.storage.upsert_impression(group_b, "10003", impression="最近在考研")
        row = await plugin.storage.get_impression(group_b, "10003")
        self.assertEqual("阿花", row["display_name"], "名字要跨群继承")

    async def test_agent_prompt_states_shared_impressions(self):
        from src.prompts import REPLY_SYSTEM_PROMPT

        self.assertIn("人物印象是跨群的", REPLY_SYSTEM_PROMPT)
        self.assertIn("只有一份", REPLY_SYSTEM_PROMPT)


class ConsoleForestEntryTests(unittest.TestCase):
    """实录："可视化记忆的界面入口呢？我没找到" → 控制台里加「记忆森林」面板
    （独立页 pages/tree 也保留）。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.html = (root / "pages" / "memory" / "index.html").read_text(encoding="utf-8")
        cls.js = (root / "pages" / "memory" / "app.js").read_text(encoding="utf-8")

    def test_nav_and_panel_exist(self):
        self.assertIn('id: "forest"', self.js, "侧栏要有记忆森林入口")
        self.assertIn('id="panel-forest"', self.html)
        self.assertIn('id="forest"', self.html)
        self.assertIn("loadForest", self.js)

    def test_forest_uses_tree_api_and_node_actions(self):
        self.assertIn('get("memory_tree"', self.js)
        self.assertIn("memory_node_save", self.js)
        self.assertIn("memory_node_delete", self.js)
        self.assertIn("askPrompt", self.js, "新增/编辑用页面内模态，不能用原生 prompt")

    def test_standalone_tree_page_still_present(self):
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / "pages" / "tree" / "app.js").is_file())


class ForestGraphTests(unittest.TestCase):
    """实录：用户要的是**圆形节点 + 连线**的可视化树，不是嵌套列表。
    要求：悬停看详情、左键菜单（编辑/删除/加子节点）、双击展开、滚轮缩放、拖拽平移。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        cls.forest = (root / "pages" / "tree" / "forest.js").read_text(encoding="utf-8")
        cls.tree_html = (root / "pages" / "tree" / "index.html").read_text(encoding="utf-8")
        cls.tree_app = (root / "pages" / "tree" / "app.js").read_text(encoding="utf-8")
        cls.console_html = (root / "pages" / "memory" / "index.html").read_text(encoding="utf-8")
        cls.console_js = (root / "pages" / "memory" / "app.js").read_text(encoding="utf-8")

    def test_renders_circles_and_edges(self):
        self.assertIn('svgEl("circle"', self.forest, "节点必须是圆形")
        self.assertIn('svgEl("path"', self.forest, "父子之间要有连线")
        self.assertIn("SVG_NS" if False else "createElementNS", self.forest)

    def test_hover_tooltip_and_click_menu(self):
        self.assertIn("showTip", self.forest)
        self.assertIn("forest-tip", self.forest)
        self.assertIn("openMenu", self.forest)
        self.assertIn("forest-menu", self.forest)
        for label in ("编辑", "删除", "加子节点", "展开子节点"):
            self.assertIn(label, self.forest, f"左键菜单要有「{label}」")

    def test_zoom_pan_and_lazy_expand(self):
        self.assertIn("wheel", self.forest, "滚轮缩放")
        self.assertIn("mousedown", self.forest, "拖拽平移")
        self.assertIn("toggleExpand", self.forest, "懒加载展开")
        self.assertIn("getTree(id)", self.forest, "展开时才去取子节点")

    def test_type_colors(self):
        for key in ("scope", "category", "summary", "catalog", "impression", "topic", "message"):
            self.assertIn(key + ":", self.forest, f"{key} 要有颜色")

    def test_tree_page_mounts_graph(self):
        self.assertIn('id="graph"', self.tree_html)
        self.assertIn("forest.js", self.tree_html, "独立页要引渲染器")
        self.assertIn("SparkingForest.mount", self.tree_app)

    def test_console_inlines_renderer_with_list_fallback(self):
        """控制台**内联**渲染器：插件页里跨页相对路径会 404（截图里图空白就是这个原因），
        所以 app.js 里带一份同源代码；渲染器不可用时回退列表并藏掉空白图形区。"""
        self.assertIn('id="forest-graph"', self.console_html)
        self.assertIn("window.SparkingForest = { mount }", self.console_js,
                      "控制台要内联渲染器定义")
        self.assertIn("SparkingForest.mount", self.console_js)
        self.assertIn("回退成列表", self.console_js)
        self.assertNotIn("../tree/forest.js", self.console_html,
                         "不能依赖跨页相对路径")

    def test_hidden_wins_over_display(self):
        """[hidden] 必须压过 .list{display:flex}，否则隐藏不生效（列表会一直露着）。"""
        for page in ("memory", "tree"):
            css = (Path(__file__).resolve().parents[1] / "pages" / page / "style.css").read_text(encoding="utf-8")
            self.assertIn("[hidden]", css)
            self.assertIn("display: none !important", css)


class PluginLogoTests(unittest.TestCase):
    """用户要求：把这张图当打包后的插件图标。AstrBot 认插件根目录的 logo.png
    （参照 astrbot_plugin_browser：根目录 logo.png + metadata 不写 logo 字段）。"""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]

    def test_logo_exists_at_plugin_root(self):
        logo = self.root / "logo.png"
        self.assertTrue(logo.is_file(), "插件根目录必须有 logo.png")
        from PIL import Image

        with Image.open(logo) as image:
            self.assertEqual((512, 512), image.size)
            self.assertEqual("RGB", image.mode)
        self.assertLess(logo.stat().st_size, 600 * 1024, "图标别超 600KB")

    def test_pages_reference_the_logo(self):
        for page in ("memory", "tree"):
            html = (self.root / "pages" / page / "index.html").read_text(encoding="utf-8")
            self.assertIn("logo.png", html, f"{page} 页要用它当 favicon")

    def test_readme_shows_logo(self):
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("logo.png", readme)

    def test_package_includes_logo(self):
        """现场打包到临时目录校验——发布前 dist 会清空，不能依赖既有产物。"""
        import importlib.util
        import sys
        import tempfile
        import zipfile

        spec = importlib.util.spec_from_file_location(
            "sparking_package", self.root / "tools" / "package.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["sparking_package"] = module
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = module.build(out_dir=Path(tmp))
            with zipfile.ZipFile(bundle_path) as bundle:
                names = bundle.namelist()
        self.assertTrue(any(name.endswith("/logo.png") for name in names), "包里要带 logo.png")
        self.assertTrue(any(name.endswith("/metadata.yaml") for name in names), "包里要带 metadata.yaml")
        self.assertFalse(any("/tests/" in name or "/tools/" in name for name in names),
                         "开发件（tests/tools）不进包")
