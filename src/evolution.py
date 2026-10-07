"""GEPA 式提示词自进化引擎（参考 hermes-agent-self-evolution 的 DSPy+GEPA 设计）。

无 DSPy 依赖的原生移植：用插件的 _llm_text 做反射变异与评分，保留 GEPA 的
核心循环语义——**反思式变异**（读执行轨迹理解"为什么失败"而不仅是"失败了"），
而不是盲目随机改写。

三大组件（对应 hermes-evo 的三个 core 模块）：
1. DatasetBuilder（≈ dataset_builder.py）：合成长评测集——LLM 读目标文本生成
   多样化的 (task_input, expected_behavior) 对，按 train/val/holdout 切分；
   也支持金标 JSONL 手工集。
2. LLMJudge（≈ fitness.py）：LLM-as-judge 多维评分
   correctness/procedure_following/conciseness + 长度惩罚 + 反馈文本；
   快速代理指标（关键词重叠）用于粗筛，LLM 评分只用在关键节点（省 token）。
3. ConstraintValidator（≈ constraints.py）：硬约束门——尺寸上限、相对基线的
   增长上限、非空、结构完整性。任一失败 = 立即淘汰（不等评分）。

进化循环（≈ evolve_skill.py 的 orchestration）：
    基线在 holdout 打分 → 反思式变异生成候选种群 → 约束过滤 → train 快评 →
    val 精评 → 精英保留（top-k 进入下一代）→ 迭代 N 轮 → holdout 终评 →
    仅当进化版严格优于基线才采纳（永不回退）。

产物持久化：evolution/<name>/（baseline.md、gen_N.md、history.jsonl、
champion.md、report.json），全程可审计、可回滚。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

LLMFn = Callable[..., Awaitable[str]]


# ---------------------------------------------------------------- 数据模型


@dataclass(slots=True)
class EvalExample:
    """一条评测用例（≈ hermes-evo EvalExample）。"""

    task_input: str          # 用户会怎么问
    expected_behavior: str   # 好回答的标准（评分 rubric，不是标准答案文本）
    difficulty: str = "medium"
    category: str = "general"
    source: str = "synthetic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_input": self.task_input,
            "expected_behavior": self.expected_behavior,
            "difficulty": self.difficulty,
            "category": self.category,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvalExample":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass(slots=True)
class EvalDataset:
    """train/val/holdout 三段切分的评测集（≈ hermes-evo EvalDataset）。"""

    train: list[EvalExample] = field(default_factory=list)
    val: list[EvalExample] = field(default_factory=list)
    holdout: list[EvalExample] = field(default_factory=list)

    @property
    def all_examples(self) -> list[EvalExample]:
        return self.train + self.val + self.holdout

    @classmethod
    def split(cls, examples: list[EvalExample],
              train_ratio: float = 0.5, val_ratio: float = 0.25) -> "EvalDataset":
        shuffled = list(examples)
        rng = __import__("random")
        rng.shuffle(shuffled)
        n_total = len(shuffled)
        n_train = max(1, int(n_total * train_ratio))
        n_val = max(1, int(n_total * val_ratio))
        return cls(
            train=shuffled[:n_train],
            val=shuffled[n_train:n_train + n_val],
            holdout=shuffled[n_train + n_val:],
        )

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        for split_name, split_data in (
            ("train", self.train), ("val", self.val), ("holdout", self.holdout),
        ):
            (path / f"{split_name}.jsonl").write_text(
                "\n".join(json.dumps(ex.to_dict(), ensure_ascii=False)
                          for ex in split_data),
                encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "EvalDataset":
        dataset = cls()
        for split_name in ("train", "val", "holdout"):
            split_file = path / f"{split_name}.jsonl"
            if not split_file.exists():
                continue
            examples = []
            for line in split_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        examples.append(EvalExample.from_dict(json.loads(line)))
                    except json.JSONDecodeError:
                        continue
            setattr(dataset, split_name, examples)
        return dataset


@dataclass(slots=True)
class FitnessScore:
    """多维适应度（≈ hermes-evo FitnessScore）。"""

    correctness: float = 0.0
    procedure_following: float = 0.0
    conciseness: float = 0.0
    length_penalty: float = 0.0
    feedback: str = ""

    @property
    def composite(self) -> float:
        raw = (0.5 * self.correctness
               + 0.3 * self.procedure_following
               + 0.2 * self.conciseness)
        return max(0.0, raw - self.length_penalty)


@dataclass(slots=True)
class ConstraintResult:
    """一条硬约束的结果（≈ hermes-evo ConstraintResult）。"""

    passed: bool
    constraint_name: str
    message: str


@dataclass(slots=True)
class Candidate:
    """一个进化候选（变异后的提示词 + 它的轨迹）。"""

    text: str
    generation: int = 0
    parent: str = "baseline"
    mutation_note: str = ""
    val_score: float = -1.0
    holdout_score: float = -1.0


@dataclass(slots=True)
class EvolutionReport:
    """一次进化运行的完整报告（≈ evolve_skill.py 的最终表格）。"""

    artifact_name: str
    baseline_holdout: float
    champion_holdout: float
    improvement: float
    generations: int
    candidates_evaluated: int
    champion_text: str
    adopted: bool
    elapsed_seconds: float
    history: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------- 约束门


class ConstraintValidator:
    """硬约束校验（≈ hermes-evo ConstraintValidator）。任一失败 = 立即淘汰。"""

    def __init__(self, *, max_size: int = 15_000,
                 max_growth: float = 0.2) -> None:
        self.max_size = max_size
        self.max_growth = max_growth

    def validate_all(self, text: str, baseline_text: str = "") -> list[ConstraintResult]:
        return [
            self._check_size(text),
            self._check_non_empty(text),
            *( [self._check_growth(text, baseline_text)] if baseline_text else [] ),
            self._check_no_leak(text),
        ]

    def _check_size(self, text: str) -> ConstraintResult:
        size = len(text)
        if size <= self.max_size:
            return ConstraintResult(True, "size_limit", f"尺寸合格 {size}/{self.max_size}")
        return ConstraintResult(False, "size_limit",
                                f"超尺寸 {size}/{self.max_size}（超 {size - self.max_size}）")

    def _check_growth(self, text: str, baseline: str) -> ConstraintResult:
        growth = (len(text) - len(baseline)) / max(1, len(baseline))
        if growth <= self.max_growth:
            return ConstraintResult(True, "growth_limit",
                                    f"增长合格 {growth:+.1%}（上限 {self.max_growth:+.1%}）")
        return ConstraintResult(False, "growth_limit",
                                f"增长超限 {growth:+.1%}（上限 {self.max_growth:+.1%}）")

    @staticmethod
    def _check_non_empty(text: str) -> ConstraintResult:
        if text.strip():
            return ConstraintResult(True, "non_empty", "非空")
        return ConstraintResult(False, "non_empty", "空文本")

    @staticmethod
    def _check_no_leak(text: str) -> ConstraintResult:
        """进化产物自身不得引入心声泄漏模式（防 GEPA 把泄漏当'改进'）。"""
        from .humanization import _is_inner_voice
        # 只抽指令性语句粗查：逐行扫描
        for line in text.splitlines():
            if line.strip() and _is_inner_voice(line):
                return ConstraintResult(False, "no_leak", f"疑似心声泄漏行：{line[:60]}")
        return ConstraintResult(True, "no_leak", "无泄漏模式")


# ---------------------------------------------------------------- 评分


class LLMJudge:
    """LLM-as-judge 多维评分（≈ hermes-evo LLMJudge，无 DSPy 依赖）。"""

    _JUDGE_PROMPT = """你是严格的评测裁判。根据评分标准对 AI 的回答打分（每项 0.0~1.0）。
只输出一个严格 JSON 对象：
{"correctness": 0.0, "procedure_following": 0.0, "conciseness": 0.0, "feedback": "具体可执行的改进建议（供反思式变异用：指出为什么不够好，而不是只说不够好）"}

评分标准：
- correctness：回答是否正确地完成了任务
- procedure_following：是否遵守了给定指令/人设的要求（含格式、语言风格）
- conciseness：是否简洁而不遗漏关键信息

评分必须以 expected_behavior 为准绳；AI 回答与标准答案文字不同但满足 rubric 不扣分。"""

    def __init__(self, llm: LLMFn) -> None:
        self._llm = llm

    async def score(self, task_input: str, expected_behavior: str,
                    agent_output: str, skill_text: str,
                    artifact_size: int | None = None,
                    max_size: int | None = None) -> FitnessScore:
        from .json_utils import parse_json_object

        agent_output = (agent_output or "").strip()
        if not agent_output:
            return FitnessScore(correctness=0.0, procedure_following=0.0,
                                conciseness=0.0, feedback="空输出")
        prompt = json.dumps({
            "task_input": task_input[:2000],
            "expected_behavior": expected_behavior[:2000],
            "agent_output": agent_output[:3000],
            "skill_text": skill_text[:2000],
        }, ensure_ascii=False)
        raw = await self._llm(prompt, system_prompt=self._JUDGE_PROMPT)
        data = parse_json_object(raw) or {}
        correctness = _parse_score(data.get("correctness"))
        procedure = _parse_score(data.get("procedure_following"))
        conciseness = _parse_score(data.get("conciseness"))
        feedback = str(data.get("feedback") or "")[:600]

        length_penalty = 0.0
        if artifact_size is not None and max_size:
            ratio = artifact_size / max_size
            if ratio > 0.9:
                length_penalty = min(0.3, (ratio - 0.9) * 3.0)
        return FitnessScore(correctness=correctness,
                            procedure_following=procedure,
                            conciseness=conciseness,
                            length_penalty=length_penalty,
                            feedback=feedback)


def _char_bigrams(text: str) -> set[str]:
    """字符 bigram 集合——中文没有空格分词，词重叠在中文下恒为 0。"""
    cleaned = "".join(str(text or "").split())
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def quick_fitness(agent_output: str, expected_behavior: str) -> float:
    """快速代理指标（≈ hermes-evo skill_fitness_metric 的重叠粗筛）。

    零 LLM 成本：用于 train 集的粗筛轮。**字符 bigram 重叠**而非空格分词——
    中文没有空格分词，英文式词重叠对中文恒为 0（实录：粗筛恒 0.3 全被淘汰）。
    """
    output = (agent_output or "").strip()
    if not output:
        return 0.0
    expected_grams = _char_bigrams(expected_behavior)
    if not expected_grams:
        return 0.5
    output_grams = _char_bigrams(output)
    overlap = len(expected_grams & output_grams) / len(expected_grams)
    return min(1.0, max(0.0, 0.3 + 0.7 * overlap))


def _parse_score(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return min(1.0, max(0.0, float(value)))
    try:
        return min(1.0, max(0.0, float(str(value).strip())))
    except (TypeError, ValueError):
        return 0.5


# ---------------------------------------------------------------- 数据集构建


class SyntheticDatasetBuilder:
    """合成长评测集（≈ hermes-evo SyntheticDatasetBuilder）。"""

    _PROMPT = """你是评测集生成器。阅读下面的"目标文本"（一段 AI 的人设/指令/技能描述），
生成 {num_cases} 条多样化的评测用例，覆盖它的不同侧面。

只输出一个严格 JSON 数组，每项：
{{"task_input": "用户实际会怎么问（口语，QQ聊天风格）",
  "expected_behavior": "好回答的标准——描述该做到什么（含格式/语言风格要求），不是标准答案文本",
  "difficulty": "easy|medium|hard",
  "category": "考察目标文本的哪个侧面"}}

要求：task_input 必须像真人随口说的话（短、口语、可能有错别字）；难度分布均匀；
expected_behavior 要能客观判定（有可观察的特征），不要主观描述。"""

    def __init__(self, llm: LLMFn) -> None:
        self._llm = llm

    async def generate(self, artifact_text: str, *,
                       num_cases: int = 20,
                       artifact_type: str = "prompt") -> EvalDataset:
        from .json_utils import parse_json_value

        prompt = json.dumps({
            "artifact_type": artifact_type,
            "artifact_text": artifact_text[:6000],
            "num_cases": max(4, min(int(num_cases), 40)),
        }, ensure_ascii=False)
        raw = await self._llm(prompt, system_prompt=self._PROMPT)
        data = parse_json_value(raw)
        cases = data if isinstance(data, list) else []
        if not cases and isinstance(data, dict):
            cases = data.get("test_cases") or data.get("cases") or []
        examples = [
            EvalExample(
                task_input=str(c.get("task_input", "")).strip(),
                expected_behavior=str(c.get("expected_behavior", "")).strip(),
                difficulty=str(c.get("difficulty", "medium")).strip()[:12],
                category=str(c.get("category", "general")).strip()[:24],
            )
            for c in cases
            if isinstance(c, dict) and str(c.get("task_input", "")).strip()
            and str(c.get("expected_behavior", "")).strip()
        ]
        return EvalDataset.split(examples)


# ---------------------------------------------------------------- 执行器


class PromptExecutor:
    """把"目标文本作为指令 + 任务输入"交给执行模型（≈ hermes-evo SkillModule）。

    目标文本就是要被进化的参数：每次 forward 用当前文本作为指令处理任务。
    """

    _EXEC_PROMPT = """完成任务。严格遵守下面给出的指令文本的要求（语言风格/格式/角色）。

[指令文本开始]
{skill_text}
[指令文本结束]

回答要自然，像指令描述的角色本人在说话。"""

    def __init__(self, llm: LLMFn, provider_id: str = "") -> None:
        self._llm = llm
        self.provider_id = provider_id

    async def run(self, skill_text: str, task_input: str) -> str:
        system = self._EXEC_PROMPT.format(skill_text=skill_text[:6000])
        return str(await self._llm(task_input, system_prompt=system) or "").strip()


# ---------------------------------------------------------------- 变异器


class ReflectiveMutator:
    """反思式变异（GEPA 的核心）：读执行轨迹理解"为什么失败"再定向改写。

    不是"随便改改"：每次变异都带着 (当前文本, 轨迹里最差的用例, judge 的反馈)
    让优化模型做针对性改进。
    """

    _MUTATE_PROMPT = """你是提示词优化器（GEPA 反思式变异）。下面是一段 AI 的人设/指令文本、
它表现最差的用例（任务+评分反馈），请产出这段文本的**改进版**。

规则：
1. 只改真正影响失败用例的部分——定向手术，不要全文重写。
2. 保持原有语言风格与角色（这是中文 QQ 群聊 bot 的提示词，改进版必须是中文）。
3. 长度变化不超过 ±20%。
4. 改进版不能包含这些泄漏模式：思考过程解说、"作为一个AI"、内部字段名。
5. 在 mutation_note 里用一句话说明你改了什么、为什么（给审计看）。

只输出一个严格 JSON 对象：
{{"mutation_note": "一句话改进说明", "evolved_text": "改进后的完整文本"}}"""

    def __init__(self, llm: LLMFn) -> None:
        self._llm = llm

    async def mutate(self, current_text: str,
                     worst_cases: Sequence[dict[str, Any]]) -> tuple[str, str]:
        from .json_utils import parse_json_object

        payload = {
            "current_text": current_text[:6000],
            "worst_cases": [
                {
                    "task": str(c.get("task_input", ""))[:500],
                    "expected": str(c.get("expected_behavior", ""))[:500],
                    "actual_output": str(c.get("actual_output", ""))[:600],
                    "judge_feedback": str(c.get("feedback", ""))[:400],
                    "score": c.get("score", 0),
                }
                for c in worst_cases[:3]
            ],
        }
        raw = await self._llm(json.dumps(payload, ensure_ascii=False),
                              system_prompt=self._MUTATE_PROMPT)
        data = parse_json_object(raw) or {}
        text = str(data.get("evolved_text") or "").strip()
        note = str(data.get("mutation_note") or "").strip()[:300]
        return text, note


# ---------------------------------------------------------------- 主引擎


class GEPAOptimizer:
    """GEPA 进化主引擎：基线评估 → 反思变异 → 约束过滤 → 粗筛/精评 → 精英保留。

    不依赖 DSPy：反射变异与评分走注入的 LLM 函数；循环结构与 GEPA 语义一致
    （population + 反思式 mutation + 精英保留 + holdout 终评 + 只升不降）。
    """

    def __init__(
        self,
        *,
        llm: LLMFn,
        output_dir: str | Path,
        iterations: int = 6,
        population_size: int = 4,
        dataset_size: int = 20,
        max_size: int = 15_000,
        max_growth: float = 0.2,
        quick_train_limit: int = 6,
    ) -> None:
        self._llm = llm
        self.output_dir = Path(output_dir)
        self.iterations = max(1, int(iterations))
        self.population_size = max(2, int(population_size))
        self.dataset_size = max(8, int(dataset_size))
        self.quick_train_limit = max(3, int(quick_train_limit))
        self.validator = ConstraintValidator(max_size=max_size, max_growth=max_growth)
        self.judge = LLMJudge(llm)
        self.builder = SyntheticDatasetBuilder(llm)
        self.mutator = ReflectiveMutator(llm)
        self.executor = PromptExecutor(llm)

    async def evolve(self, artifact_name: str, artifact_text: str, *,
                     dataset: EvalDataset | None = None,
                     progress: Callable[[str], Any] | None = None) -> EvolutionReport:
        """进化一段提示词文本。champion 只在严格优于基线时才采纳。"""
        started = time.time()

        def log(message: str) -> None:
            if progress is not None:
                try:
                    progress(message)
                except Exception:
                    pass

        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "baseline.md").write_text(artifact_text, encoding="utf-8")

        # ── 1. 评测数据集（金标或合成）──
        if dataset is None or not dataset.all_examples:
            log(f"生成合成评测集（{self.dataset_size} 条）")
            dataset = await self.builder.generate(
                artifact_text, num_cases=self.dataset_size)
        dataset.save(self.output_dir / "dataset")  # 预置数据集也落盘（审计一致）
        log(f"评测集：train {len(dataset.train)} / val {len(dataset.val)} / "
            f"holdout {len(dataset.holdout)}")

        # ── 2. 基线 holdout 分数 ──
        log("评估基线（holdout）")
        baseline_holdout = await self._holdout_score(artifact_text, dataset.holdout)
        log(f"基线 holdout 分数：{baseline_holdout:.3f}")

        history: list[dict[str, Any]] = []
        candidates_evaluated = 0
        champion = Candidate(text=artifact_text, generation=0, parent="baseline",
                             holdout_score=baseline_holdout)
        worst_cases: list[dict[str, Any]] = []

        # ── 3. 迭代进化 ──
        for generation in range(1, self.iterations + 1):
            log(f"第 {generation}/{self.iterations} 代：反思式变异种群 "
                f"({self.population_size} 个候选)")
            gen_candidates: list[Candidate] = []
            for index in range(self.population_size):
                try:
                    text, note = await self.mutator.mutate(
                        champion.text, worst_cases)
                except Exception as error:
                    log(f"  变异失败：{str(error)[:100]}")
                    continue
                if not text or text == champion.text:
                    continue
                constraints = self.validator.validate_all(text, champion.text)
                failed = [c for c in constraints if not c.passed]
                if failed:
                    log(f"  候选{index} 被约束淘汰：{failed[0].message}")
                    history.append({
                        "generation": generation, "candidate": index,
                        "mutation": note, "rejected": failed[0].constraint_name,
                    })
                    continue
                gen_candidates.append(Candidate(
                    text=text, generation=generation,
                    parent=f"gen{generation - 1}", mutation_note=note))
                candidates_evaluated += 1

            if not gen_candidates:
                log("  本代没有存活候选，提前收敛")
                break

            # ── 4. train 粗筛（零 LLM 成本）→ val 精评 ──
            scored: list[tuple[Candidate, list[dict[str, Any]]]] = []
            for candidate in gen_candidates:
                quick = await self._quick_train_score(candidate.text, dataset.train)
                log(f"  候选 [{candidate.mutation_note[:24]}] train 粗评 {quick:.3f}")
                if quick < 0.35:
                    continue
                val_score, worst = await self._val_score(
                    candidate.text, dataset.val, champion.text)
                candidate.val_score = val_score
                scored.append((candidate, worst))
                log(f"  候选 val 精评 {val_score:.3f}")

            if not scored:
                log("  本代无候选通过粗筛，提前收敛")
                break

            scored.sort(key=lambda pair: pair[0].val_score, reverse=True)
            best_candidate, best_worst = scored[0]
            worst_cases = best_worst

            # ── 5. 挑战者 holdout 终评：只升不降 ──
            if best_candidate.val_score <= champion.val_score if champion.val_score >= 0 else False:
                log("  最优候选未超越冠军 val 分数，跳过 holdout")
                continue
            best_candidate.holdout_score = await self._holdout_score(
                best_candidate.text, dataset.holdout)
            log(f"  挑战者 holdout {best_candidate.holdout_score:.3f} "
                f"vs 冠军 {champion.holdout_score:.3f}")
            history.append({
                "generation": generation,
                "mutation": best_candidate.mutation_note,
                "val": best_candidate.val_score,
                "holdout": best_candidate.holdout_score,
                "champion_holdout": champion.holdout_score,
            })
            if best_candidate.holdout_score > champion.holdout_score:
                champion = best_candidate
                (self.output_dir / f"gen_{generation}.md").write_text(
                    champion.text, encoding="utf-8")
                log(f"  ★ 新冠军（gen{generation}，holdout {champion.holdout_score:.3f}）")
            else:
                log("  挑战者未超越冠军，保留现任")

        improvement = champion.holdout_score - baseline_holdout
        adopted = improvement > 0.01  # 显著改善才采纳，防噪声回退
        report = EvolutionReport(
            artifact_name=artifact_name,
            baseline_holdout=baseline_holdout,
            champion_holdout=champion.holdout_score,
            improvement=improvement,
            generations=self.iterations,
            candidates_evaluated=candidates_evaluated,
            champion_text=champion.text,
            adopted=adopted,
            elapsed_seconds=time.time() - started,
            history=history,
        )
        (self.output_dir / "champion.md").write_text(champion.text, encoding="utf-8")
        (self.output_dir / "history.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in history),
            encoding="utf-8")
        (self.output_dir / "report.json").write_text(json.dumps({
            "artifact_name": report.artifact_name,
            "baseline_holdout": report.baseline_holdout,
            "champion_holdout": report.champion_holdout,
            "improvement": report.improvement,
            "generations": report.generations,
            "candidates_evaluated": report.candidates_evaluated,
            "adopted": report.adopted,
            "elapsed_seconds": round(report.elapsed_seconds, 1),
            "history": report.history,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"进化完成：{report.baseline_holdout:.3f} → {report.champion_holdout:.3f} "
            f"（{report.improvement:+.3f}，{'采纳' if adopted else '保留基线'}）")
        return report


# ---------------------------------------------------------------- 内部方法


def _export_internal(opt: "GEPAOptimizer") -> dict[str, Any]:
    """供测试/工具内省的内部方法表。"""
    return {
        "holdout_score": opt._holdout_score,
        "val_score": opt._val_score,
        "quick_train_score": opt._quick_train_score,
    }


async def _holdout_score(self, text: str, holdout: list[EvalExample]) -> float:
    if not holdout:
        return 0.5
    scores: list[float] = []
    for example in holdout[:8]:
        try:
            output = await self.executor.run(text, example.task_input)
        except Exception:
            scores.append(0.0)
            continue
        fitness = await self.judge.score(
            example.task_input, example.expected_behavior, output, text,
            artifact_size=len(text), max_size=self.validator.max_size)
        scores.append(fitness.composite)
    return sum(scores) / max(1, len(scores))


async def _val_score(self, text: str, valset: list[EvalExample],
                     champion_text: str) -> tuple[float, list[dict[str, Any]]]:
    """val 精评：LLM judge 逐例打分，同时收集最差用例（供反思变异）。"""
    if not valset:
        return 0.5, []
    rows: list[dict[str, Any]] = []
    scores: list[float] = []
    for example in valset[:8]:
        try:
            output = await self.executor.run(text, example.task_input)
        except Exception:
            output = ""
        fitness = await self.judge.score(
            example.task_input, example.expected_behavior, output, text)
        scores.append(fitness.composite)
        rows.append({
            "task_input": example.task_input,
            "expected_behavior": example.expected_behavior,
            "actual_output": output,
            "feedback": fitness.feedback,
            "score": fitness.composite,
        })
    rows.sort(key=lambda row: row["score"])
    return (sum(scores) / max(1, len(scores))), rows


async def _quick_train_score(self, text: str, trainset: list[EvalExample]) -> float:
    """train 粗筛：执行模型跑用例，quick_fitness 关键词重叠粗评（零 judge 成本）。"""
    if not trainset:
        return 0.5
    scores: list[float] = []
    for example in trainset[: self.quick_train_limit]:
        try:
            output = await self.executor.run(text, example.task_input)
        except Exception:
            scores.append(0.0)
            continue
        scores.append(quick_fitness(output, example.expected_behavior))
    return sum(scores) / max(1, len(scores))


# 绑定内部方法到类（保持方法签名可测试）
GEPAOptimizer._holdout_score = _holdout_score  # type: ignore[attr-defined]
GEPAOptimizer._val_score = _val_score  # type: ignore[attr-defined]
GEPAOptimizer._quick_train_score = _quick_train_score  # type: ignore[attr-defined]
