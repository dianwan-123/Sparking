from __future__ import annotations

import functools
import unittest

from src.onebot import (
    OneBotActionError, call_action, get_image, is_group_message, is_poke_notice,
    normalize_event, safe_serialize, set_msg_emoji_like,
)


class Event:
    def __init__(self, raw_event):
        self.raw_event = raw_event


class Client:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.error:
            raise self.error
        return self.response


class OneBotTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_duck_typed_group_message(self):
        raw = {
            "post_type": "message", "message_type": "group", "sub_type": "normal",
            "self_id": 10, "group_id": 20, "user_id": 30, "message_id": 40,
            "time": 1_700_000_000,
            "sender": {"nickname": "Alice", "card": "A"},
            "message": [
                {"type": "reply", "data": {"id": "39"}},
                {"type": "text", "data": {"text": "hello"}},
            ],
        }
        message = normalize_event(Event(raw))
        self.assertIsNotNone(message)
        self.assertTrue(is_group_message(raw))
        self.assertEqual(message.conversation_id, "20")
        self.assertEqual(message.sender_name, "A")
        self.assertEqual(message.text, "hello")
        self.assertEqual(message.reply_to, "39")
        self.assertEqual(message.event_type, "message.created")

    def test_recognizes_poke_notice(self):
        raw = {
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "self_id": 1, "group_id": 2, "user_id": 3, "target_id": 1, "time": 9,
        }
        self.assertTrue(is_poke_notice(Event(raw)))
        message = normalize_event(Event(raw))
        self.assertEqual(message.event_type, "notice.poke")
        self.assertTrue(message.upstream_message_id.startswith("poke:"))

    def test_rejects_non_group_or_non_normal_message(self):
        private = {"post_type": "message", "message_type": "private"}
        anonymous = {"post_type": "message", "message_type": "group", "sub_type": "anonymous"}
        missing_id = {"post_type": "message", "message_type": "group", "sub_type": "normal"}
        self.assertIsNone(normalize_event(private))
        self.assertIsNone(normalize_event(anonymous))
        self.assertIsNone(normalize_event(missing_id))

    def test_safe_serialize_enforces_string_binary_and_global_budgets(self):
        value = {"text": "汉" * 1000, "blob": b"x" * 1000, "items": ["y" * 100] * 100}
        serialized = safe_serialize(
            value, max_bytes=300, max_string_bytes=40, max_binary_bytes=12, max_items=5
        )
        import json
        encoded = json.dumps(serialized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), 300)
        self.assertLessEqual(len(serialized["text"].encode("utf-8")), 40)
        if "blob" in serialized:
            self.assertTrue(serialized["blob"]["truncated"])
            self.assertEqual(serialized["blob"]["base64"], "eHh4eHh4eHh4eHh4")

    def test_safe_serialize_handles_cycles_and_bytes(self):
        value = {"blob": b"abc"}
        value["self"] = value
        serialized = safe_serialize(value)
        self.assertEqual(serialized["blob"]["base64"], "YWJj")
        self.assertEqual(serialized["self"], "<cycle>")

    async def test_action_wrapper_is_allowlisted_and_sdk_independent(self):
        client = Client({"status": "ok", "retcode": 0})
        await set_msg_emoji_like(client, 12, 76)
        self.assertEqual(client.calls, [("set_msg_emoji_like", {"message_id": 12, "emoji_id": "76"})])
        with self.assertRaises(OneBotActionError):
            await call_action(client, "delete_msg", message_id=12)

    async def test_action_errors_are_wrapped(self):
        with self.assertRaises(OneBotActionError) as raised:
            await call_action(Client(error=RuntimeError("offline")), "get_msg", message_id=1)
        self.assertEqual(raised.exception.action, "get_msg")
        failed = Client({"status": "failed", "retcode": 100, "wording": "bad"})
        with self.assertRaises(OneBotActionError) as raised:
            await call_action(failed, "get_image", file="x")
        self.assertEqual(raised.exception.response["retcode"], 100)

    async def test_prebound_partial_transport_get_image(self):
        """aiocqhttp Api.__getattr__ shape: partial(call_action, action)."""
        class Transport:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **params):
                self.calls.append((action, params))
                return {"status": "ok", "retcode": 0}

        transport = Transport()
        prebound = functools.partial(transport.call_action, "get_image")
        await get_image(prebound, "ABC.image")
        self.assertEqual(transport.calls, [("get_image", {"file": "ABC.image"})])

    async def test_instance_bound_partial_transport_still_works(self):
        """Desktop-build shape: partial(CQHttp.call_action, bot) needs action passed."""
        class CQHttpLike:
            def __init__(self):
                self.calls = []

            async def call_action(self, action, **params):
                self.calls.append((action, params))
                return {"status": "ok", "retcode": 0}

        bot = CQHttpLike()
        wrapped = functools.partial(CQHttpLike.call_action, bot)
        await set_msg_emoji_like(wrapped, 12, 76)
        self.assertEqual(
            bot.calls, [("set_msg_emoji_like", {"message_id": 12, "emoji_id": "76"})]
        )

    async def test_dynamic_attribute_probe_is_not_mistaken_for_transport(self):
        """aiocqhttp `Api.__getattr__` mints a partial for ANY attribute name.

        A stray probe such as `client.bot` hands back
        `functools.partial(call_action, "bot")`; treating that as the transport
        sends the literal action name "bot" and the API answers retcode 1404
        (不支持的Api bot). The resolver must keep looking for the genuine one.
        """
        transport_calls: list[tuple[str, dict]] = []

        class CQHttpLike:
            def __getattr__(self, item):
                # mimic aiocqhttp: any attribute yields an action-bound partial
                return functools.partial(self.call_action, item)

            async def call_action(self, action, **params):
                transport_calls.append((action, params))
                if action == "bot":
                    return {
                        "status": "failed", "retcode": 1404,
                        "message": "不支持的Api bot",
                    }
                return {"status": "ok", "retcode": 0}

        bot = CQHttpLike()
        # `bot.bot` mints partial(call_action, "bot") — the trap
        trap = bot.bot
        self.assertEqual("bot", trap.args[0])

        await set_msg_emoji_like(bot, 12, 76)
        # the real action name must reach the transport, never "bot"
        self.assertEqual(
            transport_calls,
            [("set_msg_emoji_like", {"message_id": 12, "emoji_id": "76"})],
        )


if __name__ == "__main__":
    unittest.main()
