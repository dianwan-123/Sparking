# -*- coding: utf-8 -*-
"""v0.37.0 回归：合并转发"点不开/总说已过期"的修复。

实录：用户让 bot 读合并聊天记录，bot 老回"已过期"——因为 QQ 只在服务端缓存
转发资源一小段时间，而插件在**回复时才去现场拉**；拉失败还亲手把
"（资源可能已过期）"写进注入文案，模型照着念。

修法：①消息到达时展开并入库（原有）→ 读转发**先查入库结果**；
②现场拉时把承载消息 id 等更多候选 id 一起试（不同网关认的 id 形态不同）；
③成功展开的正文补写进库（能读出来就一定读得出来）；
④`read_forward` 新工具：传 message_id 自动解析出转发 id 再读；
⑤失败文案不再提"过期"，改为让模型用 read_forward 再试、真不行才如实说读不到。
"""
from __future__ import annotations

import json
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


def _gateway_stub(nodes_by_id=None, *, fail_all=False, parsed=None):
    calls: list[tuple] = []

    async def execute(action, **params):
        calls.append((action, params))
        if action == "get_forward_msg":
            if fail_all:
                raise RuntimeError("forward resource gone")
            key = str(params.get("id") or params.get("message_id") or "")
            nodes = (nodes_by_id or {}).get(key)
            if nodes:
                return {"data": {"messages": nodes}}
            raise RuntimeError("not found")
        if action == "get_msg":
            mid = str(params.get("message_id") or "")
            return {"data": (parsed or {}).get(mid)}
        return {"status": "ok"}

    return types.SimpleNamespace(bot=None, execute=execute), calls


class ForwardCacheTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_cached_forward_survives_resource_expiry(self):
        """入库过的转发：现场拉全失败也要能读出来（不再说"已过期"）。"""
        plugin, hooks, scope = await self._plugin()
        from src.models import NormalizedMessage

        body = "甲：第一条\n乙：第二条\n甲：第三条内容足够长用于入库判断"
        await plugin.ingest.ingest(NormalizedMessage(
            platform="aiocqhttp", account_id="bot1", conversation_id="123",
            upstream_message_id="forward:resABC:deadbeef", sender_id="self",
            sender_name="self",
            text=f"[合并转发内容 resABC123456]\n{body}",
            occurred_at="2026-10-06T10:00:00+00:00",
            raw_event={"message_id": "forward:resABC:deadbeef", "derived": True},
            parts=[], event_type="notice.forward",
        ))
        gw, calls = _gateway_stub(fail_all=True)
        plugin.gateway = gw
        text = await plugin._fetch_forward_text("resABC123456", scope_id=scope)
        self.assertIn("第二条", text, "应命中入库内容")
        self.assertEqual([], [c for c in calls if c[0] == "get_forward_msg"],
                         "命中缓存后不该再打网络")

    async def test_extra_ids_used_when_primary_fails(self):
        """主 id 拉不到时，改用承载消息的 message_id 也要能拉出来。"""
        plugin, hooks, scope = await self._plugin()
        gw, calls = _gateway_stub({"msg-777": [
            {"sender": {"nickname": "甲"}, "content": [{"type": "text",
                                                        "data": {"text": "正文在这"}}]},
        ]})
        plugin.gateway = gw
        text = await plugin._fetch_forward_text(
            "bad-res-id", extra_ids=["msg-777"], scope_id=scope)
        self.assertIn("正文在这", text)
        tried = [c[1].get("id") or c[1].get("message_id") for c in calls
                 if c[0] == "get_forward_msg"]
        self.assertIn("msg-777", tried, "候选 id 必须被尝试")

    async def test_success_is_written_back_to_store(self):
        """成功展开的正文要补写入库，下次读取命中缓存。"""
        plugin, hooks, scope = await self._plugin()
        gw, _calls = _gateway_stub({"res-X": [
            {"sender": {"nickname": "甲"},
             "content": [{"type": "text", "data": {"text": "补写测试正文" * 4}}]},
        ]})
        plugin.gateway = gw
        text = await plugin._fetch_forward_text("res-X", scope_id=scope)
        self.assertIn("补写测试", text)
        await plugin._remember_forward_text(scope, "res-X", text)
        cached = await plugin.storage.find_forward_note(scope, "res-X")
        self.assertIn("补写测试", cached)

    async def test_read_forward_resolves_message_id(self):
        """read_forward：传承载消息 id，先 get_msg 解出转发 id 再读。"""
        plugin, hooks, scope = await self._plugin()
        gw, calls = _gateway_stub(
            {"fwd-res-9": [{"sender": {"nickname": "乙"},
                            "content": [{"type": "text", "data": {"text": "内容OK"}}]}]},
            parsed={"mid-1": {
                "sender": {"user_id": 1},
                "message": [{"type": "forward", "data": {"id": "fwd-res-9"}}],
            }},
        )
        plugin.gateway = gw
        event = hooks.FakeEvent(hooks._group_raw("读一下", 7201), "读一下",
                                hooks.FakeBot(), wake=True)
        result = await plugin.read_forward_tool(event, message_id="mid-1")
        self.assertIn("内容OK", result)
        self.assertIn("get_msg", [c[0] for c in calls])

    async def test_read_forward_honest_failure_text(self):
        plugin, hooks, scope = await self._plugin()
        gw, _calls = _gateway_stub(fail_all=True)
        plugin.gateway = gw
        event = hooks.FakeEvent(hooks._group_raw("读一下", 7202), "读一下",
                                hooks.FakeBot(), wake=True)
        result = await plugin.read_forward_tool(event, message_id="mid-x")
        self.assertIn("没取到", result)
        self.assertNotIn("过期", result, "不许再拿'过期'当说法")

    async def test_reply_note_no_longer_says_expired(self):
        """注入给模型的失败文案不许提'过期'（模型会照着念）。"""
        plugin, hooks, scope = await self._plugin()
        gw, _calls = _gateway_stub(fail_all=True)
        plugin.gateway = gw
        plugin.context.responses = [
            json.dumps({"action": "text", "intent": "读转发"}),
            '{"thought": "好", "mode": "single", "message": {"action": "text", "text": "读不到"}}',
        ]
        raw = hooks._group_raw("看看这个", 7203)
        raw["message"] = [{"type": "forward", "data": {"id": "res-fail-1"}}]
        event = hooks.FakeEvent(raw, "看看这个", hooks.FakeBot(), wake=True)
        prepared = await plugin._prepare_reply(
            event, scope, "7203", False, wake=True)
        self.assertIsNotNone(prepared)
        prompt = str(plugin.context.llm_calls[-1].get("prompt", ""))
        self.assertIn("read_forward", prompt, "失败时要点名让模型用 read_forward 再试")
        self.assertNotIn("可能已过期", prompt)


if __name__ == "__main__":
    unittest.main()
