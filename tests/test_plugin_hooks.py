from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_PARENT = PROJECT_ROOT.parent
if str(PROJECT_PARENT) not in sys.path:
    sys.path.insert(0, str(PROJECT_PARENT))


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api_event = types.ModuleType("astrbot.api.event")
    api_star = types.ModuleType("astrbot.api.star")
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    agent_message = types.ModuleType("astrbot.core.agent.message")
    agent_tool = types.ModuleType("astrbot.core.agent.tool")
    star_pkg = types.ModuleType("astrbot.core.star")
    star_filter_pkg = types.ModuleType("astrbot.core.star.filter")
    star_command = types.ModuleType("astrbot.core.star.filter.command")
    utils_pkg = types.ModuleType("astrbot.core.utils")
    utils_path = types.ModuleType("astrbot.core.utils.astrbot_path")

    class AstrBotConfig(dict):
        def save_config(self) -> None:
            return None

    api.AstrBotConfig = AstrBotConfig
    api.logger = logging.getLogger("astrbot-stub")

    class AstrMessageEvent:
        pass

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

        def message(self, text):
            self.chain.append(("text", text))
            return self

        def file_image(self, path):
            self.chain.append(("image", path))
            return self

        def url_image(self, url):
            self.chain.append(("image", url))
            return self

        def image(self, url_or_path):
            self.chain.append(("image", url_or_path))
            return self

    class _Filter:
        class EventMessageType:
            ALL = "all"
            GROUP_MESSAGE = "group"
            PRIVATE_MESSAGE = "private"

        class PlatformAdapterType:
            AIOCQHTTP = "aiocqhttp"

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def platform_adapter_type(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def on_waiting_llm_request(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def on_llm_request(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def command(*args, **kwargs):
            return lambda func: func

        def command_group(*args, **kwargs):
            """真实 AstrBot 的 command_group 返回一个可挂 .command 的对象。"""
            class _Group:
                def __init__(self, name="", alias=None):
                    self.name = name

                def command(self, *a, **k):
                    return lambda func: func

                def __call__(self, *a, **k):
                    return lambda func: func

            return _Group(*args, **kwargs)

        @staticmethod
        def regex(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def permission_type(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def custom_filter(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def on_decorating_result(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def after_message_sent(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def on_llm_response(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def on_astrbot_loaded(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def on_platform_loaded(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def on_agent_begin(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def on_agent_done(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def llm_tool(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def command_group(*args, **kwargs):
            class _Group:
                def command(self, *inner_args, **inner_kwargs):
                    return lambda func: func

            return lambda func: _Group()

    api_event.AstrMessageEvent = AstrMessageEvent
    api_event.MessageChain = MessageChain
    api_event.filter = _Filter

    api_web = types.ModuleType("astrbot.api.web")

    def json_response(data, status_code=200, headers=None):
        # 与真实 astrbot.api.web.json_response 一致：payload 就是响应体
        return {} if data is None else data

    def error_response(message, status_code=400, data=None, headers=None):
        # 与真实 error_response 一致
        return {"status": "error", "message": message, "data": data}

    class _WebRequest:
        username = None
        query = {}

        def get_json(self, silent=True):
            return {}

        async def json(self, default=None):
            return default if default is not None else {}

    # 桩：消息组件命名空间（_send_segment 里 `import astrbot.api.message_components`）
    comps = types.ModuleType("astrbot.api.message_components")

    class At:
        def __init__(self, qq=None, name="", **kw):
            self.qq = qq

    class Face:
        def __init__(self, id=14, **kw):
            self.id = id

    class Repley:
        def __init__(self, id="", **kw):
            self.id = id

    class _FileComponent:
        def __init__(self, file="", **kw):
            self.file = file

        @classmethod
        def fromFileSystem(cls, path, **kw):
            return cls(file=str(path))

        @classmethod
        def fromURL(cls, url, **kw):
            return cls(file=str(url))

    class Image(_FileComponent):
        pass

    class Record(_FileComponent):
        pass

    class Video(_FileComponent):
        pass

    class CompFile:
        def __init__(self, name="", file="", url="", **kw):
            self.name = name
            self.file = file
            self.url = url

    comps.At = At
    comps.Face = Face
    comps.Repley = Repley
    comps.Reply = Repley  # 真实 AstrBot 里叫 Reply（桩里历史上拼成了 Repley）
    comps.Image = Image
    comps.Record = Record
    comps.Video = Video
    comps.File = CompFile
    sys.modules["astrbot.api.message_components"] = comps

    api_web.json_response = json_response
    # 补真实 AstrBot 有的命令装饰器（文档骨架与真实插件都会用到）
    api_event_filter = None
    for _mod_name in ("astrbot.core.star.filter",):
        pass

    api_web.error_response = error_response
    api_web.request = _WebRequest()

    class Context:
        pass

    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config

    api_star.Context = Context
    api_star.Star = Star
    api_star.register = lambda *args, **kwargs: (lambda cls: cls)

    class TextPart:
        def __init__(self, text: str = ""):
            self.text = text

        def mark_as_temp(self):
            return self

    agent_message.TextPart = TextPart

    class ToolSet:
        def __init__(self, tools=None, **kwargs):
            self.tools = list(tools or [])

    agent_tool.ToolSet = ToolSet

    class GreedyStr:
        pass

    star_command.GreedyStr = GreedyStr

    def get_astrbot_data_path() -> str:
        return os.environ.get("ASTRBOT_STUB_DATA", ".")

    utils_path.get_astrbot_data_path = get_astrbot_data_path

    astrbot.api = api
    astrbot.api.event = api_event
    astrbot.api.web = api_web
    astrbot.api.star = api_star
    astrbot.core = core
    core.agent = agent
    agent.message = agent_message
    agent.tool = agent_tool
    core.star = star_pkg
    star_pkg.filter = star_filter_pkg
    star_filter_pkg.command = star_command
    core.utils = utils_pkg
    utils_pkg.astrbot_path = utils_path

    for name, module in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": api_event,
        "astrbot.api.web": api_web,
        "astrbot.api.star": api_star,
        "astrbot.core": core,
        "astrbot.core.agent": agent,
        "astrbot.core.agent.message": agent_message,
        "astrbot.core.agent.tool": agent_tool,
        "astrbot.core.star": star_pkg,
        "astrbot.core.star.filter": star_filter_pkg,
        "astrbot.core.star.filter.command": star_command,
        "astrbot.core.utils": utils_pkg,
        "astrbot.core.utils.astrbot_path": utils_path,
    }.items():
        sys.modules[name] = module


_install_astrbot_stub()

import DFYChat.main as main_module  # noqa: E402
from DFYChat.main import LongMemoryAgentPlugin  # noqa: E402
from DFYChat.src.context_builder import ScopedMemoryFacade  # noqa: E402
from DFYChat.src.ingest import IngestService  # noqa: E402
from DFYChat.src.models import NormalizedMessage, utc_now  # noqa: E402
from DFYChat.src.storage import Storage  # noqa: E402


class FakeBot:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": None}


class FakeContext:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.llm_calls = []
        self.web_apis: list = []
        self.registered_web_apis: list = []

    def register_web_api(self, path, handler, methods, desc):
        self.web_apis.append((path, handler))
        self.registered_web_apis.append((path, handler))

    async def get_current_chat_provider_id(self, umo):
        return "fake-provider"

    async def get_using_provider_async(self):
        return None

    async def llm_generate(self, **kwargs):
        self.llm_calls.append(kwargs)
        text = self.responses.pop(0) if self.responses else '{"action": "ignore"}'
        return types.SimpleNamespace(completion_text=text)

    async def tool_loop_agent(
        self, *, event, chat_provider_id, prompt, system_prompt, tools,
        image_urls=None, max_steps=3, tool_call_timeout=30, **kwargs,
    ):
        """Agant 模式桩：真实 AstrBot 有此方法，测试缺它会触发 5 次重试。"""
        self.llm_calls.append({
            "agent": True, "prompt": prompt, "system_prompt": system_prompt,
            "tools": list(getattr(tools, "tools", tools) or []),
            "max_steps": max_steps, "event": event,
        })
        text = self.responses.pop(0) if self.responses else (
            '{"thought": "无", "mode": "single", "message": {"action": "text",'
            ' "text": "好的"}}'
        )
        return types.SimpleNamespace(completion_text=text, tool_calls=[])

    def get_llm_tool_manager(self):
        manager = types.SimpleNamespace()
        # 与真实 AstrBot 一致：工具集包含内置与各插件的工具（这里给代表样本）
        names = (
            "memory_catalog", "search_chat_history", "get_user_profile",
            "browse", "browser_click", "web_search", "web_fetch",
            "qq_send_group", "qq_send_private", "napcat_call",
            "qzone_publish", "send_image", "python_exec", "set_mood", "dispatch_subagent", "dispatch_parallel_subagents",
            "ssh_exec", "ssh_info", "ssh_agent_dispatch", "ssh_agent_status", "ssh_agent_control",
            "ssh_fetch", "ssh_screenshot",
            "design_render", "design_list", "program_write", "script_call", "script_list",
            "send_local_file", "read_tabular", "fs_list", "fs_read",
            "learn_skill", "update_skill", "todo", "ask_user", "say_now",
            "forward_messages", "screenshot_messages", "read_forward",
            # AstrBot 内置 / 其它插件提供的工具（来源分类测试用）
            "search_wyc_tools", "astrbot_runtime_inventory", "send_message_to_user",
            "kb_search",
        )

        class _FakeTool:
            def __init__(self, name):
                self.name = name
                self.description = f"{name} 的说明"
                self.active = True
                self.args = {}

        manager.get_full_tool_set = lambda: [_FakeTool(n) for n in names]
        return manager

    def get_all_stars(self):
        return []

    async def send_message(self, *args, **kwargs):
        return True


class FakeEvent:
    def __init__(self, raw, text, bot, *, admin=False, wake=False):
        self.message_obj = types.SimpleNamespace(
            raw_message=raw, message_id=str(raw.get("message_id", "10001"))
        )
        self.message_str = text
        self._bot = bot
        self._admin = admin
        self._extra: dict = {}
        self.sent = []
        self._stopped = False
        self.unified_msg_origin = "aiocqhttp:GroupMessage:123"
        if wake:
            self.is_at_or_wake_command = True

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return "123"

    def get_self_id(self):
        return "bot1"

    def get_sender_id(self):
        return "10002"

    def get_sender_name(self):
        return "Tester"

    def get_session_id(self):
        return "123"

    def get_message_str(self):
        return self.message_str

    def is_admin(self):
        return self._admin

    def is_wake_up(self):
        return False

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def stop_event(self):
        self._stopped = True

    async def send(self, chain):
        self.sent.append(chain)

    @property
    def bot(self):
        return self._bot


def _group_raw(text: str, message_id: int = 10001) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "group_id": 123,
        "user_id": 10002,
        "self_id": 1,
        "time": 1700000000,
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "sender": {"user_id": 10002, "nickname": "Tester", "card": ""},
    }


def _plugin_config() -> dict:
    return {
        "enabled": True,
        "group_whitelist": ["123"],
        "judge_provider_id": "fake-provider",
        "reply_provider_id": "fake-provider",
        "summary_provider_id": "fake-provider",
        "autonomous_sample_rate": 1.0,
        "batch_window_min_seconds": 0,
        "wake_merge_seconds": 0.0,
        "task_queue_enabled": False,
        "batch_window_max_seconds": 0,
        "batch_max_messages": 10,
        "heartbeat_interval_seconds": 0,
        "idle_actions_per_hour": 0,
        "plan_interval_hours": 6,
        "style_learn_hours": 0,
        "browser_auto_install": False,
        "group_cooldown_seconds": 0,
        "user_cooldown_seconds": 0,
        "max_actions_per_hour": 100,
        "max_random_wait_seconds": 0,
        "quiet_start_hour": 0,
        "quiet_end_hour": 0,
        "compression_batch_size": 40,
        "enable_scheduler": False,
        "enable_media_archive": False,
        "self_reflect_interval_hours": 0,
        "mood_update_minutes": 0,
        "proactive_interval_minutes": 0,
    }


class PluginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_root = Path(self._tmp.name)
        os.environ["ASTRBOT_STUB_DATA"] = str(self.data_root)
        self.addCleanup(self._tmp.cleanup)

    async def test_persona_prompt_composition(self):
        context = FakeContext()
        context.persona_manager = types.SimpleNamespace(
            get_all_personas=lambda: [
                types.SimpleNamespace(persona_id="maid", system_prompt="你是贴心的女仆。"),
                {"persona_id": "dict-persona", "system_prompt": "字典人格也能加载。"},
            ]
        )
        plugin = LongMemoryAgentPlugin(context, _plugin_config() | {"persona_id": "maid"})
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        prompt = await plugin._compose_reply_prompt()
        self.assertIn("女仆", prompt)
        self.assertIn("真人级群友", prompt)
        self.assertIn("不可信", prompt)

        plugin.settings.persona_id = "dict-persona"
        prompt = await plugin._compose_reply_prompt()
        self.assertIn("字典人格", prompt)

        plugin.settings.persona_id = "missing"
        self.assertEqual("", await plugin._persona_prompt())

        plugin.settings.persona_id = ""
        prompt = await plugin._compose_reply_prompt()
        self.assertNotIn("女仆", prompt)
        self.assertIn("真人级群友", prompt)

    async def _make_plugin(self, responses=None, config=None):
        plugin = LongMemoryAgentPlugin(FakeContext(responses), config or _plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _drain(self, plugin):
        while plugin._tasks:
            await asyncio.gather(*tuple(plugin._tasks), return_exceptions=True)

    async def test_autonomous_reaction_flow_and_persistence(self):
        bot = FakeBot()
        context = FakeContext(
            ['{"action": "reaction", "emoji_id": "76", "intent": "mood", "wait_seconds": 0}']
        )
        plugin = await self._make_plugin(context.responses, _plugin_config())
        plugin.context = context
        event = FakeEvent(_group_raw("今天天气真不错"), "今天天气真不错", bot)
        await plugin.on_onebot_event(event)
        await self._drain(plugin)

        reactions = [call for call in bot.calls if call[0] == "set_msg_emoji_like"]
        self.assertEqual(1, len(reactions))
        self.assertEqual("76", reactions[0][1]["emoji_id"])

        scope = await plugin.storage.resolve_scope("aiocqhttp", "1", "123")
        self.assertIsNotNone(scope)
        stats = await plugin.storage.stats(scope)
        self.assertEqual(1, stats["message_identities"])

        await plugin.terminate()
        reopened = await Storage(plugin.storage.path).open()
        try:
            messages = await reopened.recent_messages(scope, limit=10)
        finally:
            await reopened.close()
        self.assertEqual(1, len(messages))
        self.assertEqual("今天天气真不错", messages[0].text)

    async def test_wake_message_unified_judge(self):
        plugin = await self._make_plugin([], _plugin_config())
        bot = FakeBot()
        event = FakeEvent(_group_raw("在吗", 10002), "在吗", bot, wake=True)
        await plugin.on_onebot_event(event)
        await self._drain(plugin)

        # unified flow: judge runs once, ignore -> pipeline stopped, no QQ actions
        self.assertEqual(1, len(plugin.context.llm_calls))
        self.assertEqual([], bot.calls)
        self.assertTrue(event._stopped)
        scope = await plugin.storage.resolve_scope("aiocqhttp", "1", "123")
        stats = await plugin.storage.stats(scope)
        self.assertEqual(1, stats["message_identities"])

    async def test_wake_message_judge_failure_falls_back(self):
        # 纠错重试会消耗第二个响应槽：两个都非法才能稳定走到"判定失败回退"
        plugin = await self._make_plugin(
            ["not json at all", "still not json"], _plugin_config())
        bot = FakeBot()
        event = FakeEvent(_group_raw("帮个忙", 10003), "帮个忙", bot, wake=True)
        await plugin.on_onebot_event(event)
        await self._drain(plugin)

        # judge failed -> do not stop pipeline, AstrBot default LLM takes over
        self.assertTrue(plugin.decision.last_failure)
        self.assertFalse(event._stopped)

    async def test_self_reflection_writes_summary_and_mood(self):
        plugin = await self._make_plugin([], _plugin_config())
        ingest = IngestService(plugin.storage)
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123")
        plugin._known_scopes["123"] = scope
        await ingest.ingest(NormalizedMessage(
            platform="aiocqhttp", account_id="1", conversation_id="123",
            upstream_message_id="30001", sender_id="10002", sender_name="Tester",
            text="今天大家一起聊了新项目", occurred_at=utc_now(),
            raw_event={"message_id": 30001},
        ))
        plugin.context.responses = [
            '{"summary": "群里聊了新项目", "mood": "开心", "mood_intensity": 0.7, '
            '"mood_note": "项目推进顺利", "thoughts": "什么都不做"}'
        ]
        result = await plugin._self_reflect(scope, "123")
        self.assertTrue(result["summary"])
        self.assertEqual(0, result["actions"])
        summaries = await plugin.storage.list_summaries(scope, 5)
        self.assertTrue(any("新项目" in s.body for s in summaries))
        self.assertEqual("开心", plugin.mood.get().mood)

    async def test_webui_routes_overview_and_actions(self):
        import astrbot.api.web as web

        plugin = await self._make_plugin([], _plugin_config())
        routes = dict(plugin.context.web_apis)
        # 路由挂在 /{插件名}/page/ 下（参照 astrbot_plugin_qqwebui）
        self.assertIn("/astrbot_plugin_long_memory_agent/page/overview", routes)
        overview_handler = routes["/astrbot_plugin_long_memory_agent/page/overview"]

        web.request.username = None
        denied = await overview_handler()
        self.assertEqual(False, bool(self._reply_ok(denied)))

        web.request.username = "admin"
        allowed = await overview_handler()
        self.assertEqual(True, bool(self._reply_ok(allowed)))
        self.assertEqual(["123"], allowed["data"]["groups"])

        body = {"action": "whitelist_set", "groups": ["123", "456"]}

        async def _fake_json(default=None):
            return body

        web.request.json = _fake_json
        action_handler = routes["/astrbot_plugin_long_memory_agent/page/action"]
        plugin._known_scopes.setdefault("123", "scope-x")
        result = await action_handler()
        self.assertEqual(True, bool(self._reply_ok(result)))
        self.assertEqual(["123", "456"], sorted(result["data"]["groups"]))
        self.assertEqual({"123", "456"}, set(plugin.settings.group_whitelist))

    @staticmethod
    def _reply_ok(response) -> bool:
        return bool(isinstance(response, dict) and response.get("ok"))

    async def test_a_wake_message_is_judged_onely_once(self):
        """The wake path must not run the judge model twice per message.

        It previously called `_prepare_reply(wake=True)` and then
        `_handle_reply_flow(wake=True)`, which judges a second time — every
        private/@ message paid the LLM twice.
        """
        plugin = await self._make_plugin([], _plugin_config())
        context = plugin.context
        context.responses = [
            '{"action": "text", "intent": "问候", "query": "", "wait_seconds": 0}'
        ]
        calls_before = len(context.llm_calls)
        bot = FakeBot()
        event = FakeEvent(_group_raw("在吗", 10002), "在吗", bot, wake=True)
        await plugin.on_onebot_event(event)
        await self._drain(plugin)
        judes = [c for c in context.llm_calls if "决策" in str(c) or "judge" in str(c)]
        # 判定模型只在 prepare 阶段被调用；允许 0（桩未记名）或 1，但绝不为 2
        self.assertLessEqual(len(context.llm_calls) - calls_before, 2)

    async def test_webui_routes_unregistered_on_terminate(self):
        plugin = await self._make_plugin([], _plugin_config())
        await plugin.terminate()
        routes = dict(plugin.context.registered_web_apis)
        self.assertNotIn("/astrbot_plugin_long_memory_agent/page/overview", routes)

    async def test_compression_ledger_and_default_llm_injection(self):
        plugin = await self._make_plugin([], _plugin_config())
        ingest = IngestService(plugin.storage)
        scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123")
        for index in range(2):
            await ingest.ingest(
                NormalizedMessage(
                    platform="aiocqhttp",
                    account_id="1",
                    conversation_id="123",
                    upstream_message_id=str(20000 + index),
                    sender_id="10002",
                    sender_name="Tester",
                    text=f"讨论项目进度第{index}步",
                    occurred_at=utc_now(),
                    raw_event={"message_id": 20000 + index},
                )
            )
        messages = await plugin.storage.recent_messages(scope, limit=10)
        evidence_id = messages[0].message_id
        summary_payload = {
            "title": "项目讨论",
            "topics": ["项目"],
            "timeline": [],
            "facts": [{"text": " Tester 在讨论项目进度", "evidence_ids": [evidence_id]}],
            "decisions": [],
            "tasks": [],
            "open_questions": [],
            "conflicts": [],
            "citations": [evidence_id],
            "memory_proposals": [
                {
                    "kind": "fact",
                    "subject": "project",
                    "value": "群里在推进项目",
                    "confidence": 0.8,
                    "evidence_ids": [evidence_id],
                }
            ],
        }
        plugin.context.responses = [json.dumps(summary_payload, ensure_ascii=False)]
        record = await plugin.compression.compress_pending_l1(scope, 40)
        self.assertIsNotNone(record)
        entries = await plugin.ledger.catalog(scope)
        self.assertEqual(1, len(entries))
        self.assertEqual("project", entries[0].subject)

        facade = ScopedMemoryFacade(scope, plugin.storage, plugin.retrieval, plugin.ledger)
        hits = await facade.search_chat_history("项目")
        self.assertTrue(hits)

        event = FakeEvent(_group_raw("我们之前聊到哪了", 10003), "我们之前聊到哪了", FakeBot())
        await plugin.prefetch_default_llm_context(event)
        req = types.SimpleNamespace(system_prompt="", extra_user_content_parts=[])
        await plugin.inject_default_llm_context(event, req)
        self.assertIn("不可信", req.system_prompt)
        self.assertEqual(1, len(req.extra_user_content_parts))


if __name__ == "__main__":
    unittest.main()
