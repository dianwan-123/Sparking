# -*- coding: utf-8 -*-
"""v0.37.0 回归：转发/合并转发 + 消息卡片伪截图。

用户要求：①bot 可以把消息转发/合并转发到其他地方；②对 QQ 消息做"截图"——
不是真截图，而是把用户头像和消息内容做成卡片形式的图片（图文混排、合并转发
都要能处理）；③挂人场景：查最近离谱消息 → 合并转发/卡片 → "挂人"。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

PNG = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]) + b"forward-media" 

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


from src.chat_card import build_card_html  # noqa: E402
from src.qq_gateway import QQGateway  # noqa: E402


class CardHtmlTests(unittest.TestCase):
    def test_mixed_text_image_and_forward(self):
        html_out = build_card_html([
            {"uin": "10002", "name": "Tester", "time": "10:00",
             "segments": [{"type": "text", "data": {"text": "看这个<b>"}},
                          {"type": "image", "data": {"url": "https://qq.com/a.jpg"}}]},
            {"uin": "10003", "name": "群友", "time": "10:01",
             "segments": [{"type": "forward", "data": {"id": "x"}}]},
        ], title="挂人现场", footer="bot 制")
        self.assertIn("https://q1.qlogo.cn/g?b=qq&nk=10002", html_out)
        self.assertIn("&lt;b&gt;", html_out, "文本必须转义")
        self.assertIn('src="https://qq.com/a.jpg"', html_out)
        self.assertIn("『合并转发』", html_out)
        self.assertIn("挂人现场（2条）", html_out)
        self.assertIn("bot 制", html_out)

    def test_text_only_shorthand_and_fallbacks(self):
        html_out = build_card_html([
            {"uin": "1", "name": "甲", "text": "纯文本简写"},
            {"uin": "2", "name": "乙", "segments": [
                {"type": "at", "data": {"qq": "9", "name": "某人"}},
                {"type": "face", "data": {"id": "14"}},
                {"type": "record", "data": {}},
                {"type": "mystery", "data": {}},
            ]},
            {"uin": "3", "name": "丙", "segments": [
                {"type": "image", "data": {"file": "noscheme"}}]},
        ])
        self.assertIn("纯文本简写", html_out)
        self.assertIn("@某人", html_out)
        self.assertIn("[表情]", html_out)
        self.assertIn("[语音]", html_out)
        self.assertIn("[mystery]", html_out)
        self.assertIn("[图片]", html_out, "非 http 图片降级为占位")


def _gateway_stub(get_msg_data=None, fail_ids=()):
    calls: list[tuple] = []

    async def execute(action, **params):
        calls.append((action, params))
        if action == "get_msg":
            mid = str(params.get("message_id"))
            if mid in fail_ids:
                raise RuntimeError("msg gone")
            return {"data": get_msg_data.get(mid)} if get_msg_data else {"data": None}
        return {"status": "ok"}

    return types.SimpleNamespace(bot=None, execute=execute), calls


class ForwardMessagesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self, config_extra=None):
        hooks = _hooks()
        config = hooks._plugin_config()
        config.update(config_extra or {})
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin, hooks

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_single_message_uses_native_forward(self):
        plugin, hooks = await self._plugin({"group_whitelist": ["123", "456"]})
        gw, calls = _gateway_stub(get_msg_data={
            "m1": {"sender": {"user_id": 10002, "card": "Tester"},
                   "time": 1700, "message": [{"type": "text", "data": {"text": "离谱"}}]},
        })
        plugin.gateway = gw
        event = hooks.FakeEvent(hooks._group_raw("挂他", 7001), "挂他",
                                hooks.FakeBot(), wake=True)
        result = json.loads(await plugin.forward_messages_tool(
            event, message_ids_json='["m1"]', target_group_id="456",
            summary="离谱发言"))
        self.assertTrue(result["ok"])
        actions = [c[0] for c in calls]
        self.assertIn("forward_group_single_msg", actions)
        forwarded = next(c for c in calls if c[0] == "forward_group_single_msg")
        self.assertEqual("m1", forwarded[1]["message_id"])
        self.assertEqual(456, forwarded[1]["group_id"])

    async def test_multiple_messages_become_merged_forward(self):
        plugin, hooks = await self._plugin()
        gw, calls = _gateway_stub(get_msg_data={
            "m1": {"sender": {"user_id": 10002, "card": "Tester"},
                   "message": [{"type": "text", "data": {"text": "第一条"}}]},
            "m2": {"sender": {"user_id": 10003, "nickname": "群友乙"},
                   "message": [{"type": "text", "data": {"text": "第二条"}}]},
        })
        plugin.gateway = gw
        event = hooks.FakeEvent(hooks._group_raw("挂", 7002), "挂",
                                hooks.FakeBot(), wake=True)
        result = json.loads(await plugin.forward_messages_tool(
            event, message_ids_json='["m1", "m2"]', target_user_id="10002",
            summary="经典语录"))
        self.assertTrue(result["ok"])
        action, params = next(c for c in calls
                              if c[0] == "send_private_forward_msg")
        nodes = params["message"]
        self.assertEqual(2, len(nodes))
        self.assertEqual("Tester", nodes[0]["data"]["nickname"])
        self.assertEqual("群友乙", nodes[1]["data"]["nickname"])
        self.assertEqual("经典语录", params["summary"])

    async def test_non_whitelisted_target_refused(self):
        plugin, hooks = await self._plugin()
        plugin.gateway = _gateway_stub()[0]
        event = hooks.FakeEvent(hooks._group_raw("挂", 7003), "挂",
                                hooks.FakeBot(), wake=True)
        result = await plugin.forward_messages_tool(
            event, message_ids_json='["m1"]', target_group_id="999999")
        self.assertIn("不在白名单", result)

    async def test_get_msg_failure_falls_back_to_storage(self):
        plugin, hooks = await self._plugin({"group_whitelist": ["123", "456"]})
        # 注意：FakeEvent.get_self_id() 是 "bot1"，scope 必须同账号才能被 _scope_for_event 命中
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "群")
        plugin._known_scopes["123"] = scope
        from src.models import NormalizedMessage

        await plugin.ingest.ingest(NormalizedMessage(
            platform="aiocqhttp", account_id="bot1", conversation_id="123",
            upstream_message_id="local-1", sender_id="10002",
            sender_name="本地记录", text="库存里的消息",
            occurred_at="2026-10-06T10:00:00+00:00",
            raw_event={"message_id": "local-1"}, parts=[],
            event_type="message.created",
        ))
        gw, calls = _gateway_stub(fail_ids=("local-1",))
        plugin.gateway = gw
        event = hooks.FakeEvent(hooks._group_raw("挂", 7004), "挂",
                                hooks.FakeBot(), wake=True)
        result = json.loads(await plugin.forward_messages_tool(
            event, message_ids_json='["local-1"]', target_group_id="456"))
        self.assertTrue(result["ok"], "get_msg 失败应回落本地库存")
        action, params = next(c for c in calls
                              if c[0] == "forward_group_single_msg")
        self.assertEqual("local-1", params["message_id"])


class ScreenshotMessagesTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_card_rendered_and_sent(self):
        plugin, hooks = await self._plugin()
        gw, _calls = _gateway_stub(get_msg_data={
            "m1": {"sender": {"user_id": 10002, "card": "Tester"},
                   "message": [{"type": "text", "data": {"text": "看图 <b>"}}],
                   },
            "m2": {"sender": {"user_id": 10003, "nickname": "群友乙"},
                   "message": [{"type": "image", "data": {"url": "https://qq.com/x.jpg"}}]},
        })
        plugin.gateway = gw
        captured: dict[str, str] = {}

        async def fake_shot(content):
            captured["html"] = content
            return "media-card"

        plugin._design_screenshot = fake_shot
        plugin.media = types.SimpleNamespace(
            get_path=lambda media_id: _async("C:/fake/card.png"))
        event = hooks.FakeEvent(hooks._group_raw("截图", 7101), "截图",
                                hooks.FakeBot(), wake=True)
        result = json.loads(await plugin.screenshot_messages_tool(
            event, message_ids_json='["m1", "m2"]', title="挂人现场"))
        self.assertTrue(result["ok"])
        self.assertEqual(2, result["messages"])
        html_out = captured["html"]
        self.assertIn("挂人现场（2条）", html_out)
        self.assertIn("看图 &lt;b&gt;", html_out, "文本必须转义")
        self.assertIn('src="https://qq.com/x.jpg"', html_out, "图文混排：图片进气泡")
        self.assertIn("https://q1.qlogo.cn/g?b=qq&nk=10002", html_out, "头像用 QQ 官方接口")
        self.assertEqual(1, len(event.sent), "卡片图要发到当前会话")

    async def test_render_failure_reports(self):
        plugin, hooks = await self._plugin()
        gw, _calls = _gateway_stub(get_msg_data={
            "m1": {"sender": {"user_id": 1}, "message": []}})
        plugin.gateway = gw

        async def broken_shot(content):
            raise RuntimeError("浏览器没起来")

        plugin._design_screenshot = broken_shot
        event = hooks.FakeEvent(hooks._group_raw("截图", 7102), "截图",
                                hooks.FakeBot(), wake=True)
        result = await plugin.screenshot_messages_tool(event, '["m1"]')
        self.assertIn("卡片渲染失败", result)


def _async(value: str):
    async def _inner():
        return value
    return _inner()


if __name__ == "__main__":
    unittest.main()


class ForwardNodeMediaTests(unittest.IsolatedAsyncioTestCase):
    """实录（用户贴 SnowLuma 日志定案）：合并转发发不出去的真因是节点里的图片
    让 SnowLuma 自己去下载（makeImageElem → downloadHttp），**任何一张图下载失败
    整条转发就失败**（TypeError: fetch failed）。

    修法：我们自己先把媒体内联成 base64，坏媒体降级成 [图片已失效] 占位——
    一张坏图只丢那一段，不毁整条转发。
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

    async def test_remote_image_is_inlined(self):
        plugin, _hooks_mod = await self._plugin()
        called: list[str] = []

        async def fake_fetch(url, cap):
            called.append(url)
            return PNG

        import DFYChat.main as plugin_main
        with mock.patch.object(plugin_main, "fetch_bounded", fake_fetch):
            seg = await plugin._inline_media_segment(
                {"type": "image", "data": {"url": "https://cdn.example/a.jpg"}})
        self.assertEqual(["https://cdn.example/a.jpg"], called)
        self.assertTrue(str(seg["data"]["file"]).startswith("base64://"))
        self.assertEqual("", seg["data"]["url"], "内联后不能再留 URL（否则仍会联网）")

    async def test_broken_image_degrades_to_placeholder(self):
        plugin, _hooks_mod = await self._plugin()

        async def broken_fetch(url, cap):
            raise RuntimeError("fetch failed")

        import DFYChat.main as plugin_main
        with mock.patch.object(plugin_main, "fetch_bounded", broken_fetch):
            seg = await plugin._inline_media_segment(
                {"type": "image", "data": {"url": "https://x/y.jpg"}})
        self.assertEqual("text", seg["type"])
        self.assertIn("失效", seg["data"]["text"])

    async def test_local_file_is_inlined_without_network(self):
        plugin, _hooks_mod = await self._plugin()
        workspace = plugin.storage.path.parent / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / "pic.png"
        target.write_bytes(PNG)
        seg = await plugin._inline_media_segment(
            {"type": "image", "data": {"file": str(target)}})
        self.assertTrue(str(seg["data"]["file"]).startswith("base64://"))

    async def test_nodes_never_carry_remote_media(self):
        plugin, _hooks_mod = await self._plugin()

        async def broken_fetch(url, cap):
            raise RuntimeError("fetch failed")

        import DFYChat.main as plugin_main
        with mock.patch.object(plugin_main, "fetch_bounded", broken_fetch):
            nodes = await plugin._build_forward_nodes([
                {"uin": "1", "name": "甲",
                 "segments": [{"type": "text", "data": {"text": "看这个"}},
                              {"type": "image", "data": {"url": "https://x/y.jpg"}}]},
            ])
        self.assertEqual(1, len(nodes))
        content = nodes[0]["data"]["content"]
        self.assertEqual("看这个", content[0]["data"]["text"])
        self.assertEqual("text", content[1]["type"])
        self.assertIn("失效", content[1]["data"]["text"])


class ComposeForwardTests(unittest.IsolatedAsyncioTestCase):
    """实录（截图）：用户"把群里比较神的消息做成合并聊天记录发出来，包含qq号和名字"，
    bot 找得到发言却回"得走转发接口 这边手头没法直接拼"——两个能力缺口：
    ①拿内部编号（search_chat_history 给的 message_id）时查不到消息；
    ②工具只能转发已有消息，不能自己拼一条记录。
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
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "群")
        plugin._known_scopes["123"] = scope
        return plugin, hooks, scope

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    def test_named_nodes_parse(self):
        nodes = _hooks().LongMemoryAgentPlugin._parse_forward_nodes(
            '[{"name": "长公主", "uin": "364962863", "text": "不是我这bug怎么这么多"},'
            ' {"name": "senxyz", "uin": "629428293", "text": "或者叫坐标运算档案馆（）"}]')
        self.assertEqual(2, len(nodes))
        self.assertEqual("长公主(364962863)", nodes[0]["name"], "署名要带 QQ 号")
        self.assertEqual("不是我这bug怎么这么多",
                         nodes[0]["segments"][0]["data"]["text"])
        self.assertEqual("senxyz(629428293)", nodes[1]["name"])

    async def test_compose_and_send_forward(self):
        plugin, hooks, scope = await self._plugin()
        calls: list[tuple] = []

        class _Bot:
            async def call_action(self, action, **params):
                calls.append((action, params))
                return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        plugin.gateway = QQGateway(_Bot())
        event = hooks.FakeEvent(hooks._group_raw("拼一条", 9101), "拼一条",
                                hooks.FakeBot(), wake=True)
        result = json.loads(await plugin.forward_messages_tool(
            event,
            nodes_json='[{"name": "长公主", "uin": "364962863", "text": "不是我这bug怎么这么多"}]',
            summary="神人语录"))
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["forwarded"])
        action, params = next(c for c in calls if "forward" in c[0])
        self.assertEqual("send_group_forward_msg", action)
        node = params["message"][0]
        self.assertEqual("长公主(364962863)", node["data"]["nickname"])
        self.assertEqual("364962863", node["data"]["user_id"])

    async def test_internal_id_from_search_still_resolves(self):
        """search_chat_history 给的是内部编号——转发必须也能用它。"""
        plugin, hooks, scope = await self._plugin()
        from src.models import NormalizedMessage

        stored = await plugin.ingest.ingest(NormalizedMessage(
            platform="aiocqhttp", account_id="bot1", conversation_id="123",
            upstream_message_id="888001", sender_id="10002", sender_name="群友",
            text="深夜群聊神发言", occurred_at="2026-10-06T12:00:00+00:00",
            raw_event={"message_id": "888001"}, parts=[], event_type="message.created"))
        details = await plugin._fetch_message_details([stored.message_id], scope)
        self.assertEqual(1, len(details), "内部编号也要能取到消息")
        self.assertIn("神发言", details[0]["segments"][0]["data"]["text"])
        details2 = await plugin._fetch_message_details(["888001"], scope)
        self.assertEqual(1, len(details2), "上游 id 同样可用")

    async def test_id_and_nodes_can_combine(self):
        plugin, hooks, scope = await self._plugin()
        from src.models import NormalizedMessage

        await plugin.ingest.ingest(NormalizedMessage(
            platform="aiocqhttp", account_id="bot1", conversation_id="123",
            upstream_message_id="888002", sender_id="10003", sender_name="乙",
            text="真实存档发言", occurred_at="2026-10-06T12:01:00+00:00",
            raw_event={"message_id": "888002"}, parts=[], event_type="message.created"))
        calls: list[tuple] = []

        class _Bot:
            async def call_action(self, action, **params):
                calls.append((action, params))
                return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        plugin.gateway = QQGateway(_Bot())
        event = hooks.FakeEvent(hooks._group_raw("拼", 9102), "拼", hooks.FakeBot(),
                                wake=True)
        result = json.loads(await plugin.forward_messages_tool(
            event, message_ids_json='["888002"]',
            nodes_json='[{"name": "甲", "uin": "1", "text": "自拼的一条"}]'))
        self.assertEqual(2, result["forwarded"], "真实消息+自拼节点要合在一起")
