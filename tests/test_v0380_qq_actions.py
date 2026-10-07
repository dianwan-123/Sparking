# -*- coding: utf-8 -*-
"""v0.38.0 回归：QQ 工具层完全重写（对照 SnowLuma 文档的 193 个动作）。

用户："让bot可以完全管理qq……我给你把 snowluma 文档搞下来了，你自己阅读 API 部分，
然后重写插件的 qq 工具部分，把它的 195 种 api 全都做一遍，注意是重写。"

事实核对：文档站 catalog.json 共 **193** 个动作（12 个分类）。
本文件覆盖：①动作表全量性；②网关的参数校验/类型转换/配额；
③**"合并聊天记录点不开"的真因修复**——实测发现 SnowLuma 的 get_forward_msg
只认 `message_id`（传 `id` 会被服务端判 missing required params），且传输层
直接把 OneBot 信封的 data 交回来，旧代码找 `response["data"]` 永远解不出节点。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.qq_actions import (  # noqa: E402
    ACTIONS,
    CATEGORIES,
    coerce,
    describe,
    normalize_params,
    resolve,
    stats,
)
from src.qq_gateway import messages_of, payload, QQGateway, QQGatewayError  # noqa: E402


class ActionCatalogTests(unittest.TestCase):
    def test_full_action_set(self):
        self.assertEqual(193, len(ACTIONS))
        counts = stats()
        self.assertEqual(193, sum(counts.values()))
        # 12 个分类都在，且数量与文档一致
        for category, expected in (("信息", 5), ("消息", 5), ("好友", 3), ("群信息", 6),
                                   ("群管理", 18), ("群文件", 12), ("请求", 2),
                                   ("扩展", 115), ("群相册", 8), ("空间", 9),
                                   ("系统表情", 4), ("流式接口", 6)):
            self.assertEqual(expected, counts.get(category), category)
        for category in CATEGORIES:
            self.assertIn(category, counts)

    def test_aliases_resolve(self):
        self.assertEqual("fetch_ptt_text", resolve("get_record_text").name)
        self.assertIsNone(resolve("no_such_action_xyz"))

    def test_risk_levels(self):
        self.assertEqual("read", resolve("get_group_info").risk)
        self.assertTrue(resolve("get_group_info").read_only)
        self.assertEqual("credential", resolve("get_cookies").risk)
        self.assertEqual("destructive", resolve("set_group_kick").risk)
        self.assertEqual("send", resolve("send_group_msg").risk)

    def test_param_specs_carry_types_and_required(self):
        spec = resolve("send_group_msg")
        names = {p.name: p for p in spec.params}
        self.assertTrue(names["group_id"].required)
        self.assertTrue(names["message"].required)
        self.assertEqual("uint", names["group_id"].type)
        spec2 = resolve("get_forward_msg")
        self.assertEqual({"id", "message_id"}, {p.name for p in spec2.params})
        self.assertEqual("string", spec2.param("message_id").type)

    def test_coerce_and_normalize(self):
        self.assertEqual(123, coerce("123", "uint"))
        self.assertTrue(coerce("true", "bool"))
        self.assertFalse(coerce("0", "bool"))
        self.assertEqual([1, 2], coerce("1,2", "uint[]"))
        spec = resolve("set_group_ban")
        clean, missing = normalize_params(spec, {"group_id": "42", "user_id": "7"})
        self.assertEqual({"group_id": 42, "user_id": 7}, clean)
        self.assertIn("duration", missing) if "duration" in spec.required else None

    def test_describe_mentions_params(self):
        text = describe(resolve("get_forward_msg"))
        self.assertIn("get_forward_msg", text)
        self.assertIn("message_id", text)


class ForwardReadShapeTests(unittest.TestCase):
    """读转发的形状解包（旧代码 `response["data"]` 是错的）。"""

    def test_payload_handles_unwrapped_and_envelope(self):
        self.assertEqual({"messages": [1]}, payload({"messages": [1]}))
        self.assertEqual({"message_id": 5}, payload({"message_id": 5}))
        self.assertEqual({"messages": [1]},
                         payload({"status": "ok", "retcode": 0,
                                  "data": {"messages": [1]}}))

    def test_messages_of_variants(self):
        self.assertEqual([{"x": 1}], messages_of({"messages": [{"x": 1}]}))
        self.assertEqual([{"x": 1}],
                         messages_of({"data": {"messages": [{"x": 1}]}}))
        self.assertEqual([], messages_of(None))
        self.assertEqual([], messages_of({"nope": 1}))


class _ForwardBot:
    """假的 OneBot 客户端：只认 message_id 形态（复刻 SnowLuma 行为）。"""

    def __init__(self, nodes):
        self.nodes = nodes
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if action == "get_forward_msg":
            if "message_id" not in params:
                return {"status": "failed", "retcode": 1400,
                        "message": "missing required params: ['message_id']"}
            key = str(params.get("message_id"))
            if key in self.nodes:
                return {"status": "ok", "retcode": 0,
                        "data": {"messages": self.nodes[key]}}
            return {"status": "failed", "retcode": 1404, "message": "资源不存在"}
        return {"status": "ok", "retcode": 0, "data": {}}


class ForwardFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self, nodes):
        import tests.test_plugin_hooks as hooks

        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        bot = _ForwardBot(nodes)
        plugin.gateway = QQGateway(bot)
        return plugin, bot

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    @staticmethod
    def _nodes(text="转发正文内容"):
        return [{"sender": {"nickname": "甲", "user_id": 10001},
                 "content": [{"type": "text", "data": {"text": text}}]}]

    async def test_message_id_first_and_unwraps_envelope(self):
        plugin, bot = await self._plugin({"res-1": self._nodes("第一条正文")})
        text = await plugin._fetch_forward_text("res-1")
        self.assertIn("第一条正文", text, "必须能解出节点（旧代码卡在解包上）")
        first = [c for c in bot.calls if c[0] == "get_forward_msg"][0]
        self.assertIn("message_id", first[1], "首次尝试就要用 message_id")

    async def test_carrying_message_id_is_a_candidate(self):
        # 资源 id 取不到，但承载消息的 message_id 能取到 → 必须兜底成功
        plugin, bot = await self._plugin({"msg-99": self._nodes("兜底正文")})
        text = await plugin._fetch_forward_text("bad-res", extra_ids=["msg-99"])
        self.assertIn("兜底正文", text)

    async def test_failure_is_honest_and_logged(self):
        plugin, bot = await self._plugin({})
        text = await plugin._fetch_forward_text("nowhere")
        self.assertEqual("", text)


if __name__ == "__main__":
    unittest.main()


class ReplyTargetTests(unittest.IsolatedAsyncioTestCase):
    """实录：bot 回复群友感慨，却引用了"戳一戳"通知那条消息（引用错消息）。

    真因：上下文同时给内部编号与 QQ 消息号，模型拿内部编号当 QQ 号去引用，
    QQ 端按号一找就落在了不相干的消息上。修法：发送前校验并翻译——两种 id
    都接受，系统通知一律拒绝，确认不了就干脆不引用。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self):
        import tests.test_plugin_hooks as hooks

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

    async def _seed(self, plugin, scope, upstream_id, text, *, event_type="message.created"):
        from src.models import NormalizedMessage

        stored = await plugin.ingest.ingest(NormalizedMessage(
            platform="aiocqhttp", account_id="bot1", conversation_id="123",
            upstream_message_id=upstream_id, sender_id="10002", sender_name="群友",
            text=text, occurred_at="2026-10-06T12:00:00+00:00",
            raw_event={"message_id": upstream_id}, parts=[], event_type=event_type,
        ))
        return stored

    async def test_qq_id_and_internal_id_both_translate(self):
        plugin, hooks, scope = await self._plugin()
        stored = await self._seed(plugin, scope, "777777", "三年真的熬人 懂的")
        self.assertEqual("777777",
                         await plugin._reply_id_for(scope, "777777"))
        self.assertEqual("777777",
                         await plugin._reply_id_for(scope, stored.message_id),
                         "内部编号也要能翻成 QQ 消息号")

    async def test_notice_messages_are_never_quoted(self):
        plugin, hooks, scope = await self._plugin()
        # 戳一戳这类通知以事件形式入库
        stored = await self._seed(plugin, scope, "888888",
                                  "[戳一戳] 群里的小公主喵~ 揉了揉 由页",
                                  event_type="notice.poke")
        self.assertEqual("", await plugin._reply_id_for(scope, "888888"),
                         "系统通知绝不能被引用")
        self.assertEqual("", await plugin._reply_id_for(scope, stored.message_id))

    async def test_unknown_target_is_dropped(self):
        plugin, hooks, scope = await self._plugin()
        self.assertEqual("", await plugin._reply_id_for(scope, "不存在的id"))
        self.assertEqual("", await plugin._reply_id_for(scope, ""))
        self.assertEqual("", await plugin._reply_id_for(None, "777777"))

    async def test_send_segment_drops_unverifiable_reply(self):
        plugin, hooks, scope = await self._plugin()
        await self._seed(plugin, scope, "777777", "正常发言")
        event = hooks.FakeEvent(hooks._group_raw("回一下", 6001), "回一下",
                                hooks.FakeBot(), wake=True)
        event.sent = []

        async def _send(chain):
            event.sent.append(chain)

        event.send = _send
        # 引用一个确认不了的目标 → 不引用，但消息照发
        await plugin._send_segment(event, {"action": "text", "text": "在的",
                                           "reply_to_message_id": "乱填的id"})
        self.assertEqual(1, len(event.sent))
        parts = list(getattr(event.sent[0], "chain", []))
        self.assertFalse([p for p in parts if type(p).__name__ == "Repley"],
                         "确认不了的引用必须被丢掉")
        # 引用真实消息 → 正确翻译成 QQ 消息号
        event.sent.clear()
        await plugin._send_segment(event, {"action": "text", "text": "抱抱",
                                           "reply_to_message_id": "777777"})
        parts = list(getattr(event.sent[0], "chain", []))
        replies = [p for p in parts if type(p).__name__ == "Repley"]
        self.assertEqual(1, len(replies))
        self.assertEqual("777777", replies[0].id)


class ForwardNodeShapeTests(unittest.IsolatedAsyncioTestCase):
    """实录（v0.38.0 验收）：读转发的节点是 OneBot 消息事件（内容在 message、
    发送者在 sender），旧代码只认发送侧的 content → 全部渲染成 "[空]"。
    两种形态都必须能渲染出"谁说的：内容"。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self, nodes):
        import tests.test_plugin_hooks as hooks

        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        plugin.gateway = QQGateway(_ForwardBot({"res-x": nodes}))
        return plugin

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_event_side_node_renders_text(self):
        # SnowLuma 实际返回：整条 OneBot 消息事件
        plugin = await self._plugin([{
            "self_id": 1961003014, "user_id": 10001, "time": 1791289611,
            "sender": {"user_id": 10001, "nickname": "甲"},
            "message": [{"type": "text", "data": {"text": "结构探测内容"}}],
            "message_format": "array", "post_type": "message",
        }])
        text = await plugin._fetch_forward_text("res-x")
        self.assertIn("甲", text)
        self.assertIn("结构探测内容", text)
        self.assertNotIn("[空]", text)

    async def test_send_side_node_still_works(self):
        # 我们构造出去的形态：{"type":"node","data":{nickname, content}}
        plugin = await self._plugin([{
            "type": "node",
            "data": {"user_id": "10002", "nickname": "乙",
                     "content": [{"type": "text", "data": {"text": "发送侧内容"}}]},
        }])
        text = await plugin._fetch_forward_text("res-x")
        self.assertIn("乙", text)
        self.assertIn("发送侧内容", text)

    async def test_event_side_image_only_node_shows_placeholder(self):
        plugin = await self._plugin([{
            "sender": {"nickname": "丙"},
            "message": [{"type": "image", "data": {"url": "https://x/y.jpg"}}],
        }])
        text = await plugin._fetch_forward_text("res-x")
        self.assertIn("丙", text)
        self.assertIn("[image]", text)


class NestedForwardCacheTests(unittest.IsolatedAsyncioTestCase):
    """实录：外层转发还能读、内层（转发里套的转发）在服务端已失效
    （SnowLuma: download forward message payload is empty）——尤其"别人转的旧记录
    又被转进来"时，内层往往早就没了。

    能做的：**能读到的时刻就把内层单独入库**，之后读内层 id 永远命中缓存；
    读不到才诚实标注 [嵌套转发未能展开]。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self, nodes_by_id):
        import tests.test_plugin_hooks as hooks

        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "bot1", "123", "群")
        plugin._known_scopes["123"] = scope
        plugin.gateway = QQGateway(_ForwardBot(nodes_by_id))
        return plugin, scope

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_nested_expansion_is_cached(self):
        """外层带一个嵌套转发：内层内容要单独入库，之后直接读内层 id 命中缓存。"""
        plugin, scope = await self._plugin({
            "outer-1": [{
                "sender": {"nickname": "远黛"},
                "message": [{"type": "forward", "data": {"id": "inner-1"}}],
            }],
            "inner-1": [{
                "sender": {"nickname": "烟雨念卿"},
                "message": [{"type": "text", "data": {"text": "内层正文内容"}}],
            }],
        })
        text = await plugin._fetch_forward_text("outer-1", scope_id=scope)
        self.assertIn("内层正文内容", text, "嵌套层要展开")
        cached = await plugin.storage.find_forward_note(scope, "inner-1")
        self.assertIn("内层正文内容", cached, "嵌套层必须单独入库")
        # 之后资源失效也能读出来
        plugin.gateway = QQGateway(_ForwardBot({}))
        again = await plugin._fetch_forward_text("inner-1", scope_id=scope)
        self.assertIn("内层正文内容", again)

    async def test_dead_nested_resource_is_marked_honestly(self):
        plugin, scope = await self._plugin({
            "outer-2": [{
                "sender": {"nickname": "远黛"},
                "message": [{"type": "forward", "data": {"id": "inner-dead"}}],
            }],
        })
        text = await plugin._fetch_forward_text("outer-2", scope_id=scope)
        self.assertIn("远黛", text)
        self.assertIn("嵌套转发未能展开", text)
        self.assertIn("绝不许猜", text, "占位要写明禁止脑补，防止模型编内容")
        self.assertNotIn("过期", text, "文案不许提过期（模型会照念）")
