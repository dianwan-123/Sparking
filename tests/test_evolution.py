# -*- coding: utf-8 -*-
"""GEPA 进化引擎测试：合成数据集、约束门、反思变异、精英保留、冠军采纳。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from src.evolution import (
    ConstraintValidator,
    EvalDataset,
    EvalExample,
    GEPAOptimizer,
    LLMJudge,
    SyntheticDatasetBuilder,
    quick_fitness,
)

BASELINE = "你是插画生成器。把画图请求画成 SVG 插画。只输出严格 JSON。"


def _scripted_llm(responses: list[str]):
    """按顺序弹出的假 LLM；prompt 内容会 append 到 calls。"""
    calls: list[str] = []
    state = {"index": 0}

    async def llm(prompt, system_prompt="", **kwargs):
        calls.append(prompt[:80])
        if state["index"] < len(responses):
            value = responses[state["index"]]
        else:
            value = responses[-1]
        state["index"] += 1
        return value

    llm.calls = calls
    return llm


class QuickFitnessTests(unittest.TestCase):
    def test_empty_output_zero(self):
        self.assertEqual(0.0, quick_fitness("", "标准"))

    def test_overlap_scores(self):
        low = quick_fitness("完全无关的内容", "画一只鹈鹕骑车 SVG 插画")
        high = quick_fitness("画了一只鹈鹕骑车的 SVG 插画", "画一只鹈鹕骑车 SVG 插画")
        self.assertGreater(high, low)


class JudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_judge_parses_llm_scores(self):
        async def llm(prompt, system_prompt="", **kwargs):
            return json.dumps({"correctness": 0.9, "procedure_following": 0.8,
                               "conciseness": 0.7, "feedback": "再简短一点"})

        judge = LLMJudge(llm)
        score = await judge.score("任务", "标准", "回答", "指令")
        self.assertAlmostEqual(0.9, score.correctness)
        self.assertEqual(0.0, score.length_penalty)  # 未传尺寸无惩罚
        self.assertGreater(score.composite, 0.5)
        self.assertIn("简短", score.feedback)

    async def test_judge_empty_output(self):
        async def llm(prompt, system_prompt="", **kwargs):
            return "{}"

        score = await LLMJudge(llm).score("任务", "标准", "", "指令")
        self.assertEqual(0.0, score.correctness)

    async def test_judge_parse_failure_neutral(self):
        async def llm(prompt, system_prompt="", **kwargs):
            return "不是JSON"

        score = await LLMJudge(llm).score("任务", "标准", "回答", "指令")
        self.assertAlmostEqual(0.5, score.correctness)


class ConstraintTests(unittest.TestCase):
    def test_growth_limit_rejects(self):
        validator = ConstraintValidator(max_size=15000, max_growth=0.2)
        results = validator.validate_all("长" * 2000, "短" * 1000)
        failed = [r for r in results if not r.passed]
        self.assertTrue(any(r.constraint_name == "growth_limit" for r in failed))

    def test_leak_constraint_rejects(self):
        validator = ConstraintValidator()
        results = validator.validate_all("正常指令。\n作为一个AI你不能这样。")
        failed = [r for r in results if not r.passed]
        self.assertTrue(any(r.constraint_name == "no_leak" for r in failed))

    def test_clean_text_passes(self):
        validator = ConstraintValidator(max_size=15000)
        results = validator.validate_all("你是插画生成器。画 SVG。", "你是插画生成器。画 SVG。")
        self.assertTrue(all(r.passed for r in results))


class DatasetBuilderTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_dataset_build_and_split(self):
        cases = [
            {"task_input": f"画个{i}号图", "expected_behavior": f"输出含图案{i}的SVG",
             "difficulty": "easy", "category": "render"}
            for i in range(12)
        ]
        async def llm(prompt, system_prompt="", **kwargs):
            return json.dumps(cases, ensure_ascii=False)

        dataset = await SyntheticDatasetBuilder(llm).generate(BASELINE, num_cases=12)
        self.assertEqual(12, len(dataset.all_examples))
        self.assertTrue(dataset.train)
        self.assertTrue(dataset.holdout or dataset.val)

    async def test_malformed_cases_filtered(self):
        async def llm(prompt, system_prompt="", **kwargs):
            return json.dumps([
                {"task_input": "正常", "expected_behavior": "有标准"},
                {"task_input": "", "expected_behavior": "缺任务"},
                "不是对象",
            ], ensure_ascii=False)

        dataset = await SyntheticDatasetBuilder(llm).generate(BASELINE, num_cases=3)
        self.assertEqual(1, len(dataset.all_examples))


class GEPAOptimizerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _scripted(self, holdout_improvement=True):
        """脚本化 LLM：数据集生成 → 反思变异（给进化版）→ judge 打分
        （进化版分数高于基线）。"""
        # 数据集生成：8 条用例
        cases = [
            {"task_input": f"画个测试图{i}", "expected_behavior": "输出严格JSON含title和svg字段，svg可渲染",
             "difficulty": "medium", "category": "format"}
            for i in range(8)
        ]
        evolved = BASELINE + "输出前自检 SVG 闭合标签。"  # +10字≈+29%，验证更完整的变体
        responses: list[str] = [
            json.dumps(cases, ensure_ascii=False),  # 数据集生成
        ]
        # 变异调用（每代 population 个）：返回进化版
        responses.append(json.dumps({
            "mutation_note": "加了自检步骤",
            "evolved_text": evolved,
        }, ensure_ascii=False))
        # judge 调用：按 skill_text 是否为进化版给分
        async def llm(prompt, system_prompt="", **kwargs):
            calls.append(prompt[:80])
            if state["mode"] == "dataset":
                return responses[0]
            if state["mode"] == "mutate":
                return responses[1]
            # judge
            if holdout_improvement and "自检" in json.dumps(json.loads(prompt), ensure_ascii=False).get("skill_text", ""):
                return json.dumps({"correctness": 0.95, "procedure_following": 0.9,
                                   "conciseness": 0.85, "feedback": "更完整了"})
            return json.dumps({"correctness": 0.6, "procedure_following": 0.6,
                               "conciseness": 0.6, "feedback": "可以更完整"})

        calls: list[str] = []
        state = {"mode": "dataset"}

        kinds: list[str] = []

        async def llm_router(prompt, system_prompt="", **kwargs):
            calls.append(prompt[:60])
            # 判定调用类型：judge prompt 是 JSON 且含 correctness 关键字的 system
            if "评测裁判" in system_prompt:
                payload = json.loads(prompt)
                kinds.append("judge")
                if holdout_improvement and "自检" in str(payload.get("skill_text", "")):
                    return json.dumps({"correctness": 0.95, "procedure_following": 0.9,
                                       "conciseness": 0.85, "feedback": "更完整"})
                return json.dumps({"correctness": 0.6, "procedure_following": 0.6,
                                   "conciseness": 0.6, "feedback": "可以更完整"})
            if "评测集生成器" in system_prompt:
                kinds.append("dataset")
                return responses[0]
            if "提示词优化器" in system_prompt:
                kinds.append("mutate")
                return responses[1]
            # 执行模型：返回一个像样的 JSON 回答
            return json.dumps({"title": "图", "svg": "<svg></svg>"}, ensure_ascii=False)

        llm_router.calls = calls
        llm_router.kinds = kinds
        return llm_router

    async def test_evolve_adopts_improved_champion(self):
        out = Path(self._tmp.name) / "evo"
        llm = self._scripted(holdout_improvement=True)
        optimizer = GEPAOptimizer(llm=llm, output_dir=out, iterations=2,
                                  population_size=2, dataset_size=8,
                                  max_growth=0.5)
        report = await optimizer.evolve("test_designer", BASELINE)
        self.assertEqual("test_designer", report.artifact_name)
        self.assertGreater(report.champion_holdout, report.baseline_holdout)
        self.assertTrue(report.adopted)
        # 落盘审计
        self.assertTrue((out / "champion.md").is_file())
        self.assertTrue((out / "report.json").is_file())
        self.assertTrue((out / "dataset" / "train.jsonl").is_file())
        self.assertIn("自检", report.champion_text)

    async def test_evolve_keeps_baseline_when_no_improvement(self):
        out = Path(self._tmp.name) / "evo2"
        llm = self._scripted(holdout_improvement=False)
        optimizer = GEPAOptimizer(llm=llm, output_dir=out, iterations=2,
                                  population_size=2, dataset_size=8,
                                  max_growth=0.5)
        report = await optimizer.evolve("test_designer", BASELINE)
        self.assertFalse(report.adopted, "无改进必须保留基线")
        self.assertEqual(BASELINE, (out / "champion.md").read_text(encoding="utf-8"))

    async def test_dataset_reuse(self):
        out = Path(self._tmp.name) / "evo3"
        llm = self._scripted(holdout_improvement=False)
        optimizer = GEPAOptimizer(llm=llm, output_dir=out, iterations=1,
                                  population_size=2, dataset_size=8)
        dataset = EvalDataset(
            train=[EvalExample("画个图", "输出JSON")],
            val=[EvalExample("画个图2", "输出JSON")],
            holdout=[EvalExample("画个图3", "输出JSON")],
        )
        await optimizer.evolve("test", BASELINE, dataset=dataset)
        self.assertTrue((out / "dataset" / "train.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
