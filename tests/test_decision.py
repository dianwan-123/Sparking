from __future__ import annotations

import asyncio
import json
import unittest

from src.decision import DecisionEngine, DecisionValidationError


class DecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_decision_and_emoji_allowlist(self):
        async def judge(_prompt):
            return json.dumps({"action": "reaction", "intent": "赞同", "query": "",
                               "reply_to": 12, "emoji_id": 76, "wait_seconds": 999})

        engine = DecisionEngine(judge, allowed_emoji_ids={"76"}, max_wait_seconds=5)
        decision = await engine.decide({"messages": []})
        self.assertEqual(decision.action, "reaction")
        self.assertEqual(decision.emoji_id, "76")
        self.assertEqual(decision.reply_to, "12")
        self.assertEqual(decision.wait_seconds, 5)

    async def test_invalid_json_action_fields_and_emoji_fail_closed(self):
        responses = [
            '{"action":"root"}',
            '{"action":"reaction","emoji_id":"999"}',
            "完全不是 JSON 的一段话",
        ]
        for response in responses:
            async def judge(_prompt, value=response):
                return value
            decision = await DecisionEngine(judge, allowed_emoji_ids={"76"}).decide("x")
            self.assertEqual(decision.action, "ignore")

    async def test_unknown_fields_and_markdown_fences_are_tolerated(self):
        """回归：judge 曾因 unknown fields / ```json 围栏整个判废，被@的用户
        得不到任何回复。Decision 只读白名单键，这些表面瑕疵应容忍。"""
        cases = [
            '{"thought":"想一句","action":"text","extra":1}',
            '```json\n{"action":"text","intent":"打招呼"}\n```',
            '{"action": "Text"}',
        ]
        for response in cases:
            async def judge(_prompt, value=response):
                return value
            engine = DecisionEngine(judge, allowed_emoji_ids={"76"})
            decision = await engine.decide("x")
            self.assertEqual(decision.action, "text", response)
            self.assertIsNone(engine.last_failure)

    async def test_validation_error_triggers_corrective_retry(self):
        calls: list[str] = []

        async def judge(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return "我觉得应该回一句"  # 非 JSON
            return json.dumps({"action": "text", "intent": "打招呼"})

        engine = DecisionEngine(judge)
        decision = await engine.decide("x")
        self.assertEqual("text", decision.action)
        self.assertEqual(2, len(calls))
        self.assertIn("注意", calls[1])

    def test_parse_is_strict(self):
        engine = DecisionEngine(lambda _: "")
        with self.assertRaises(DecisionValidationError):
            engine.parse('{"action":"text"} trailing')

    async def test_agent_is_allowed_unless_it_explicitly_manages_plugins_or_skills(self):
        ordinary = {"action": "agent", "intent": "查询天气", "query": "北京天气"}
        async def ordinary_judge(_prompt):
            return json.dumps(ordinary, ensure_ascii=False)
        self.assertEqual((await DecisionEngine(ordinary_judge).decide("ordinary")).action, "agent")

        for payload in (
            {"action": "agent", "intent": "安装插件", "query": "plugin x"},
            {"action": "text", "intent": "禁用 skill", "query": "skill x"},
        ):
            async def judge(_prompt, value=payload):
                return json.dumps(value, ensure_ascii=False)
            engine = DecisionEngine(judge)
            self.assertEqual((await engine.decide("ordinary", is_admin=True)).action, "ignore")
            self.assertEqual((await engine.decide("ordinary", direct_request=True)).action, "ignore")
            allowed = await engine.decide("direct", is_admin=True, direct_request=True)
            self.assertEqual(allowed.action, payload["action"])

    async def test_timeout_fails_closed_and_cancellation_propagates(self):
        async def slow(_prompt):
            await asyncio.sleep(10)
        engine = DecisionEngine(slow, timeout_seconds=0.01)
        self.assertEqual((await engine.decide("x")).action, "ignore")
        task = asyncio.create_task(DecisionEngine(slow).decide("x"))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
