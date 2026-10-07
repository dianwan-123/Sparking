# -*- coding: utf-8 -*-
"""用户实测反馈的三处修复（v1.0.0 内改，不 bump 版本）：

① 合并转发发不出去/发出来不对：卡片两行同样的字、节点署名乱码、转发里图片全变
   "[图片已失效]"（根因：get_msg 的 image.file 常是**裸文件名**，旧代码优先拿它）。
② 让它取"数学指令讨论群"的记录，它却拿了私聊的：只有当前会话能被检索/取详情，
   群名没法解析 → 现在带名字的目标都能定位，定位不了就如实报候选清单，绝不默默换群。
③ 聊天卡片比例离谱：卡片只有 520px 宽，却被塞进 1280×860 的画布里，其余全白
   → HTML 标 data-shot-fit，截图管线按内容定画布（窄卡片 2 倍）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PNG = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]) + b"card"


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


from src.chat_card import build_card_html  # noqa: E402


class CardFitTests(unittest.TestCase):
    """③ 卡片比例：HTML 必须标出"按我尺寸截图"的锚点。"""

    def test_card_marks_fit_target(self):
        html_out = build_card_html([{"uin": "1", "name": "甲", "text": "你好"}], title="随便挑的")
        self.assertIn("data-shot-fit", html_out, "卡片要标出自适应锚点")
        self.assertIn("随便挑的一条" if False else "随便挑的（1条）", html_out)


class ScreenshotFitTests(unittest.IsolatedAsyncioTestCase):
    """③ 截图管线：量到锚点就按内容定视口（窄内容 2 倍），量不到维持老行为。"""

    class FakePage:
        def __init__(self, box):
            self.box = box
            self.viewports: list[dict] = []
            self.shots: list[dict] = []
            self.zoom = ""
            self.closed = False

        def set_default_timeout(self, _timeout):
            pass

        async def set_content(self, *_args, **_kwargs):
            pass

        async def wait_for_timeout(self, _ms):
            pass

        async def evaluate(self, script, *_args):
            if "zoom" in str(script):
                self.zoom = str(script)
                return None
            return self.box

        async def set_viewport_size(self, size):
            self.viewports.append(dict(size))

        async def screenshot(self, **kwargs):
            self.shots.append(dict(kwargs))
            return PNG

        async def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self, page):
            self.page = page

        async def new_page(self):
            return self.page

    async def _shoot(self, box):
        from src.pw_driver import PlaywrightDriver

        driver = PlaywrightDriver(timeout=5.0)
        page = self.FakePage(box)
        driver._context = self.FakeContext(page)
        png = await driver._screenshot_html("<html><body>x</body></html>", None, False)
        return page, png

    async def test_narrow_card_scaled_and_sized_to_content(self):
        page, png = await self._shoot([552, 180])
        self.assertEqual(PNG, png)
        self.assertTrue(page.viewports, "要按内容设视口")
        self.assertEqual(1104, page.viewports[-1]["width"], "窄内容 2 倍宽")
        self.assertEqual(360, page.viewports[-1]["height"])
        self.assertIn("zoom", page.zoom)
        self.assertNotIn("full_page", page.shots[-1], "自适应时不再整页截")

    async def test_wide_page_keeps_normal_behaviour(self):
        page, _png = await self._shoot([1280, 900])
        self.assertEqual(1280, page.viewports[-1]["width"])
        self.assertEqual(900, page.viewports[-1]["height"])
        self.assertEqual("", page.zoom, "宽内容不放大")

    async def test_missing_marker_falls_back_to_full_page(self):
        page, _png = await self._shoot(None)
        self.assertFalse(page.viewports, "没有锚点就别动视口")
        self.assertFalse(page.shots[-1].get("full_page", False),
                         "没有锚点时维持原行为（不整页截）")


def _private_raw(text: str, message_id: int = 20001) -> dict:
    """私聊事件：群链路之外的对照场景（用户就是在私聊里要群记录的）。"""
    return {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "message_id": message_id, "user_id": 10002, "self_id": 1,
        "time": 1700000000,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "sender": {"user_id": 10002, "nickname": "Tester", "card": ""},
    }


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


class ScopeResolveTests(unittest.IsolatedAsyncioTestCase):
    """② 群名 → 会话：名字能定位、歧义要报错、找不到给候选（不许默默用当前会话）。"""

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

    async def test_resolves_by_exact_and_partial_name(self):
        plugin, _hooks_module = await self._plugin({"group_whitelist": ["111", "222"]})
        scope_a = await plugin.storage.get_or_create_scope(
            "aiocqhttp", "bot1", "111", "数学指令讨论群")
        plugin._known_scopes["111"] = scope_a
        scope_b = await plugin.storage.get_or_create_scope(
            "aiocqhttp", "bot1", "222", "BE指令饮品店")
        plugin._known_scopes["222"] = scope_b

        exact, key, _hint = await plugin._resolve_scope_ref("数学指令讨论群")
        self.assertEqual(scope_a, exact)
        self.assertEqual("111", key)
        partial, key, _hint = await plugin._resolve_scope_ref("饮品")
        self.assertEqual(scope_b, partial)
        by_id, key, _hint = await plugin._resolve_scope_ref("111")
        self.assertEqual(scope_a, by_id)

    async def test_unknown_name_lists_candidates(self):
        plugin, _hooks_module = await self._plugin({"group_whitelist": ["111"]})
        scope_a = await plugin.storage.get_or_create_scope(
            "aiocqhttp", "bot1", "111", "数学指令讨论群")
        plugin._known_scopes["111"] = scope_a
        resolved, _key, hint = await plugin._resolve_scope_ref("不存在的群")
        self.assertEqual("", resolved)
        self.assertIn("数学指令讨论群", hint, "要给出候选，模型才知道改口")

    async def test_ambiguous_name_refused(self):
        plugin, _hooks_module = await self._plugin({"group_whitelist": ["111", "222"]})
        for key, name in (("111", "数学群一号"), ("222", "数学群二号")):
            plugin._known_scopes[key] = await plugin.storage.get_or_create_scope(
                "aiocqhttp", "bot1", key, name)
        resolved, _key, hint = await plugin._resolve_scope_ref("数学群")
        self.assertEqual("", resolved)
        self.assertIn("多个", hint)


class CrossScopeSearchTests(unittest.IsolatedAsyncioTestCase):
    """② 检索能指定别的群（否则私聊里根本够不到群记录）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self):
        hooks = _hooks()
        config = hooks._plugin_config()
        config.update({"group_whitelist": ["111"]})
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

    async def test_search_targets_named_group(self):
        plugin, hooks = await self._plugin()
        scope = await plugin.storage.get_or_create_scope(
            "aiocqhttp", "bot1", "111", "数学指令讨论群")
        plugin._known_scopes["111"] = scope
        from src.models import NormalizedMessage

        await plugin.ingest.ingest(NormalizedMessage(
            platform="aiocqhttp", account_id="bot1", conversation_id="111",
            upstream_message_id="up-1", sender_id="10002", sender_name="由页喵~",
            text="最后三小时 我还剩四张卷子", occurred_at="2026-10-07T04:07:00+00:00",
            raw_event={"message_id": "up-1"}, parts=[], event_type="message.created",
        ))
        # 私聊里问：必须能搜到那个群
        event = hooks.FakeEvent(_private_raw("找找看"), "找找看",
                                hooks.FakeBot(), wake=True)
        hits = json.loads(await plugin.search_chat_history_tool(
            event, query="四张卷子", group_id="数学指令讨论群"))
        self.assertTrue(hits, "按群名应当能搜到该群消息")
        self.assertIn("四张卷子", json.dumps(hits, ensure_ascii=False))

    async def test_unknown_group_reports_instead_of_current_scope(self):
        plugin, hooks = await self._plugin()
        event = hooks.FakeEvent(_private_raw("找找看"), "找找看",
                                hooks.FakeBot(), wake=True)
        out = await plugin.search_chat_history_tool(
            event, query="随便", group_id="根本没这个群")
        self.assertIn("没能定位", out)


class ForwardPayloadTests(unittest.IsolatedAsyncioTestCase):
    """① 合并转发：文案不重复、署名清洗、图片能内联。"""

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

    async def test_summary_prompt_preview_are_distinct(self):
        plugin, hooks = await self._plugin({"group_whitelist": ["123"]})
        gw, calls = _gateway_stub(get_msg_data={
            "m1": {"sender": {"user_id": 10002, "card": "由页喵~"},
                   "message": [{"type": "text", "data": {"text": "我还剩四张卷子"}}]},
            "m2": {"sender": {"user_id": 10003, "card": "Kaguya"},
                   "message": [{"type": "text", "data": {"text": "建议直接开抄"}}]},
        })
        plugin.gateway = gw
        event = hooks.FakeEvent(hooks._group_raw("打包", 8001), "打包",
                                hooks.FakeBot(), wake=True)
        await plugin.forward_messages_tool(
            event, message_ids_json='["m1", "m2"]', target_group_id="123",
            summary="数学指令讨论群 最近5条")
        _action, params = next(c for c in calls if c[0] == "send_group_forward_msg")
        self.assertEqual("数学指令讨论群 最近5条", params["summary"])
        self.assertNotEqual(params["summary"], params["prompt"],
                            "summary/prompt 不能再是同一句话（卡片上会重复两行）")
        preview = params["news"]
        self.assertTrue(all(item["text"] != params["summary"] for item in preview))
        self.assertIn("由页喵~：", preview[0]["text"])

    async def test_bare_filename_image_goes_through_gateway(self):
        """实录：get_msg 的 image.file 是裸文件名 → 旧代码读不到就当"已失效"。"""
        plugin, _hooks_module = await self._plugin()
        calls: list[tuple] = []

        async def execute(action, **params):
            calls.append((action, params))
            if action == "get_image":
                import base64 as b64

                return {"data": {"base64": b64.b64encode(PNG).decode("ascii")}}
            return {"status": "ok"}

        plugin.gateway = types.SimpleNamespace(bot=None, execute=execute)
        plugin._bind_gateway_client = lambda: None
        segment = {"type": "image", "data": {"file": "a1b2c3.jpg", "url": ""}}
        out = await plugin._inline_media_segment(dict(segment))
        self.assertEqual("image", out["type"])
        self.assertTrue(str(out["data"]["file"]).startswith("base64://"))
        self.assertEqual("get_image", calls[0][0])

    async def test_url_preferred_over_bare_filename(self):
        plugin, _hooks_module = await self._plugin()
        seen: list[str] = []

        async def fake_fetch(url, *_args, **_kwargs):
            seen.append(str(url))
            return PNG

        segment = {"type": "image",
                   "data": {"file": "a1b2c3.jpg", "url": "https://cdn.example/x.png"}}
        with mock.patch("DFYChat.main.fetch_bounded", new=fake_fetch):
            out = await plugin._inline_media_segment(dict(segment))
        self.assertEqual(["https://cdn.example/x.png"], seen,
                         "有 URL 就下 URL；裸文件名读不到别当坏图")
        self.assertEqual("image", out["type"])
        self.assertTrue(str(out["data"]["file"]).startswith("base64://"))

    async def test_dead_media_degrades_to_placeholder(self):
        plugin, _hooks_module = await self._plugin()
        plugin.gateway = None
        out = await plugin._inline_media_segment(
            {"type": "image", "data": {"file": "gone.jpg"}})
        self.assertEqual("text", out["type"])
        self.assertIn("已失效", out["data"]["text"])

    async def test_node_names_cleaned(self):
        plugin, _hooks_module = await self._plugin()
        cleaned = plugin._clean_forward_name("&ÿÿÆê\u0000 ◆◆由页\uFFFD  ")
        self.assertNotIn("\u0000", cleaned)
        self.assertNotIn("\uFFFD", cleaned)
        self.assertNotIn("\uDC80", cleaned)
        self.assertLessEqual(len(plugin._clean_forward_name("长" * 80)), 24)
        self.assertEqual("群友", plugin._clean_forward_name("\uFFFD\u0001") or "群友")

    async def test_forward_to_named_group(self):
        plugin, hooks = await self._plugin({"group_whitelist": ["123"]})
        scope = await plugin.storage.get_or_create_scope(
            "aiocqhttp", "bot1", "123", "数学指令讨论群")
        plugin._known_scopes["123"] = scope
        gw, calls = _gateway_stub(get_msg_data={
            "m1": {"sender": {"user_id": 10002, "card": "甲"},
                   "message": [{"type": "text", "data": {"text": "一"}}]},
            "m2": {"sender": {"user_id": 10003, "card": "乙"},
                   "message": [{"type": "text", "data": {"text": "二"}}]},
        })
        plugin.gateway = gw
        event = hooks.FakeEvent(_private_raw("打包"), "打包",
                                hooks.FakeBot(), wake=True)
        result = json.loads(await plugin.forward_messages_tool(
            event, message_ids_json='["m1", "m2"]', target_group_id="数学指令讨论群"))
        self.assertTrue(result["ok"])
        _action, params = next(c for c in calls if c[0] == "send_group_forward_msg")
        self.assertEqual(123, params["group_id"])

    async def test_unknown_target_group_reports(self):
        plugin, hooks = await self._plugin({"group_whitelist": ["123"]})
        plugin.gateway = _gateway_stub()[0]
        event = hooks.FakeEvent(_private_raw("打包"), "打包",
                                hooks.FakeBot(), wake=True)
        out = await plugin.forward_messages_tool(
            event, nodes_json='[{"name": "甲", "uin": "1", "text": "x"}]',
            target_group_id="根本没有的群")
        self.assertIn("没能定位", out)


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


if __name__ == "__main__":
    unittest.main()
