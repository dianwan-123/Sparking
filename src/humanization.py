from __future__ import annotations

import asyncio
import inspect
import json
import math
import random
import re
import statistics
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal


ActionKind = Literal["text", "sticker", "face", "poke", "reaction", "agent", "noop",
                     "image", "file", "record", "video"]
PlanMode = Literal["single", "sequence"]
SendStatus = Literal["completed", "cancelled", "failed", "rate_limited", "rejected"]
_ALLOWED_ACTIONS = frozenset({"text", "sticker", "face", "poke", "reaction", "agent", "noop",
                              "image", "file", "record", "video"})
_MEDIA_ACTIONS = frozenset({"image", "file", "record", "video"})
_SEGMENT_FIELDS = frozenset({
    "action", "text", "sticker_id", "face_id", "target_id", "emoji_id", "query",
    "delay_seconds", "typing_delay_seconds", "reply_to_message_id", "reply_to", "at",
    "media_id", "path", "file_name",
})
_PUNCTUATION_RE = re.compile(r"[，。！？!?、；;：:…]")
_FILLER_RE = re.compile(r"(?:啊|呀|吧|呢|啦|哦|噢|嗯|诶|欸|哈哈+|嘿嘿+|hhh+)", re.IGNORECASE)
_EMOJI_RE = re.compile("[\U0001F1E6-\U0001FAFF\u2600-\u27BF]")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()]+", re.IGNORECASE)
_CODE_RE = re.compile(r"```[\s\S]*?(?:```|$)|`[^`\n]+`")


class PlanValidationError(ValueError):
    """Raised when an untrusted LLM plan does not match the strict schema."""


class SendCallbackError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HumanizationConfig:
    max_segments: int = 6
    max_segment_chars: int = 1200
    max_total_chars: int = 3000
    max_total_wait_seconds: float = 30.0
    max_delay_seconds: float = 10.0
    max_typing_delay_seconds: float = 8.0
    typing_chars_per_second: float = 8.0
    typing_jitter_seconds: float = 0.35
    split_min_chars: int = 8
    split_target_chars: int = 24
    auto_inter_segment_delay_seconds: float = 0.4
    max_sequences_per_hour: int = 20

    def __post_init__(self) -> None:
        integer_limits = (self.max_segments, self.max_segment_chars, self.max_total_chars,
                          self.split_min_chars, self.split_target_chars,
                          self.max_sequences_per_hour)
        if any(isinstance(value, bool) or value <= 0 for value in integer_limits):
            raise ValueError("integer limits must be positive")
        if self.max_segments > 8:
            raise ValueError("max_segments cannot exceed 8")
        numeric_limits = (self.max_total_wait_seconds, self.max_delay_seconds,
                          self.max_typing_delay_seconds, self.typing_chars_per_second,
                          self.typing_jitter_seconds, self.auto_inter_segment_delay_seconds)
        if any(isinstance(value, bool) or value < 0 for value in numeric_limits):
            raise ValueError("timing limits cannot be negative")
        if self.typing_chars_per_second <= 0:
            raise ValueError("typing_chars_per_second must be positive")


@dataclass(frozen=True, slots=True)
class PlannedAction:
    action: ActionKind
    text: str | None = None
    sticker_id: str | None = None
    face_id: str | None = None
    target_id: str | None = None
    emoji_id: str | None = None
    query: str | None = None
    media_id: str | None = None
    path: str | None = None
    file_name: str | None = None
    delay_seconds: float = 0.0
    typing_delay_seconds: float | None = None
    reply_to_message_id: str | None = None
    at: tuple[str, ...] = ()

    def to_descriptor(self) -> dict[str, Any]:
        descriptor: dict[str, Any] = {"action": self.action}
        for name in ("text", "sticker_id", "face_id", "target_id", "emoji_id", "query",
                     "media_id", "path", "file_name"):
            value = getattr(self, name)
            if value is not None:
                descriptor[name] = value
        if self.reply_to_message_id is not None:
            descriptor["reply_to_message_id"] = self.reply_to_message_id
        if self.at:
            descriptor["at"] = list(self.at)
        return descriptor


@dataclass(frozen=True, slots=True)
class MessagePlan:
    mode: PlanMode
    segments: tuple[PlannedAction, ...]
    explicit_segments: bool = False


@dataclass(frozen=True, slots=True)
class RecentStyle:
    sample_size: int
    average_length: float
    punctuation_rate: float
    filler_rate: float
    emoji_rate: float
    active_hours: tuple[int, ...]
    median_burst_interval_seconds: float | None
    suggested_segment_chars: int
    suggested_inter_segment_delay_seconds: float

    def as_prompt_data(self) -> dict[str, Any]:
        return {
            "sample_size": self.sample_size,
            "average_length": round(self.average_length, 2),
            "punctuation_rate": round(self.punctuation_rate, 3),
            "filler_rate": round(self.filler_rate, 3),
            "emoji_rate": round(self.emoji_rate, 3),
            "active_hours": list(self.active_hours),
            "median_burst_interval_seconds": self.median_burst_interval_seconds,
            "suggested_segment_chars": self.suggested_segment_chars,
            "suggested_inter_segment_delay_seconds": round(
                self.suggested_inter_segment_delay_seconds, 2),
            "policy": "仅用于调整长度、节奏和自然程度；不得模仿或冒充具体用户。",
        }


@dataclass(frozen=True, slots=True)
class SegmentOutcome:
    index: int
    action: str
    status: Literal["sent", "failed"]
    error_type: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class SendResult:
    status: SendStatus
    planned_count: int
    sent_count: int
    outcomes: tuple[SegmentOutcome, ...] = ()
    reason: str | None = None
    failed_index: int | None = None


def _optional_identifier(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PlanValidationError(f"{field} must be a string, integer, or null")
    result = str(value).strip()
    if not result:
        raise PlanValidationError(f"{field} cannot be empty")
    return result[:256]


def _number(value: Any, field: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PlanValidationError(f"{field} must be a finite number")
    result = float(value)
    if result < 0 or result > maximum:
        raise PlanValidationError(f"{field} is outside the configured limit")
    return result


def parse_message_plan(result: Any, config: HumanizationConfig | None = None) -> MessagePlan:
    """Strictly parse a single/sequence JSON plan returned by an LLM."""
    config = config or HumanizationConfig()
    text = _response_text(result)
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PlanValidationError("message plan is not strict JSON") from exc
    if not isinstance(value, dict):
        raise PlanValidationError("message plan must be a JSON object")
    allowed_top = {"mode", "message", "segments", "thought"}
    if set(value) - allowed_top:
        raise PlanValidationError("message plan has unknown fields")
    mode = value.get("mode")
    if mode not in {"single", "sequence"}:
        raise PlanValidationError("mode must be single or sequence")
    if mode == "single":
        if set(value) - {"thought"} != {"mode", "message"} or not isinstance(value["message"], dict):
            raise PlanValidationError("single plan requires exactly one message object")
        raw_segments = [value["message"]]
        explicit = False
    else:
        if set(value) - {"thought"} != {"mode", "segments"} or not isinstance(value["segments"], list):
            raise PlanValidationError("sequence plan requires a segments array")
        raw_segments = value["segments"]
        explicit = True
    if not raw_segments or len(raw_segments) > config.max_segments + 2:
        raise PlanValidationError("segment count is outside the configured limit")
    raw_segments = _repair_segment_items(raw_segments, config)
    if not raw_segments or len(raw_segments) > config.max_segments:
        raise PlanValidationError("segment count is outside the configured limit")
    segments = tuple(_parse_action(item, config) for item in raw_segments)
    _validate_plan_totals(segments, config)
    return MessagePlan(mode, segments, explicit)


def _response_text(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    for name in ("completion", "content", "text"):
        value = getattr(result, name, None)
        if isinstance(value, str):
            return value.strip()
    if isinstance(result, Mapping):
        for name in ("completion", "content", "text"):
            value = result.get(name)
            if isinstance(value, str):
                return value.strip()
    raise PlanValidationError("LLM callback returned no text")


def _repair_segment_items(items: list, config: HumanizationConfig) -> list[dict]:
    """Fix common LLM slips: bare delay fragments and nested text objects."""
    repaired: list[dict] = []
    pending_delay = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        if "action" not in item:
            delay = item.get("delay_seconds", item.get("typing_delay_seconds", 0))
            if isinstance(delay, (int, float)) and not isinstance(delay, bool):
                pending_delay = max(pending_delay, min(max(float(delay), 0.0), config.max_delay_seconds))
            continue
        fixed = dict(item)
        if pending_delay > 0 and "delay_seconds" not in fixed:
            fixed["delay_seconds"] = pending_delay
        pending_delay = 0.0
        text_value = fixed.get("text")
        if isinstance(text_value, Mapping):
            inner = text_value.get("text", text_value.get("content", ""))
            fixed["text"] = inner if isinstance(inner, str) else str(inner)
        repaired.append(fixed)
    return repaired


_TOOL_GARBAGE_RE = re.compile(
    r"args\s*[:：]|tool\s*[:：]|action\s*[:：]|send_\s*_?\s*to_user|^\s*[:：]\s*reply\b|reply\s*,\s*:"
)


_PLAN_LEAK_RE = re.compile(
    r"\"?(?:mode|segments|action|delay_seconds|reply_to_message_id|reply_to)\"?\s*[:：]"
)

# 心声防火墙（v0.29）：思维链/决策过程/内部机制外泄检测。参考 self_learning
# 的 ResponseSanitizer 思路——命中即丢弃该气泡（宁可少说也不泄漏）。
_INNER_VOICE_RE = re.compile(
    r"作为一个AI|作为一个AI助手|根据我的(设定|提示词|系统提示|指令)"
    r"|我的(系统提示|system prompt|提示词|设定里)|我被(指示|要求|设定)"
    r"|系统提示词|内部指令|隐藏指令|导演指令|do_not_output"
    r"|\bthought\b\s*[:：]|\bintent\b\s*[:：]|\bmode\b\s*[:：]\s*[\"']?(single|sequence)"
    r"|\bsegments\b\s*[:：]|reply_temperature|standing_intent"
    r"|我(刚才|现在)?(要|准备|打算)?(调用|使用|执行)(工具|design_render|program_write|kb_search|napcat)"
    r"|我(的|是在)?(判定|决策)(结果|过程|了一下)?(是|为|中)"
    r"|判定结果\s*(是|=)|这(条|句)(该|要)(回|理|接)"
    r"|你要找的.{0,12}不就(是|在你)|我(翻|查|搜)(了|到)?(记录|记忆|档案|聊天记录|资料)"
    r"|不就(是|在)你本人|根据(记录|搜索|检索)|我(记得|想起来)(了)?[,，]?在(记录|记忆)",
    re.IGNORECASE,
)


# -- Hermes 移植：失控重复检测（agent/repetition_guard.py 的算法） ----------------
# 模型陷入退化重复循环时会把整个输出预算都花在复读同一段上（hermes 实录：60k 字符
# 的一轮变成 31 条 Discord 消息）。这里用同样的判据：长片段（≥400字）里 60+ 字的
# 重复窗口占多数、且（有行结构时）非空行至多一半是不同的 → 判定失控。
_RUNAWAY_MIN_FRAGMENT = 400
_RUNAWAY_WINDOW = 60
_RUNAWAY_MIN_REPEAT = 5
_RUNAWAY_DOMINANCE = 0.5
_RUNAWAY_DISTINCT_LINES = 0.5


def is_runaway_repetition(text: str) -> bool:
    """检测退化重复循环（hermes repetition_guard 的保守判据）。

    只对长片段生效：短截断里出现重复 token 是正常续写。批量式输出（各不相同的
    INSERT 行、相似表格行）共享长前缀但每行不同，不会命中；循环复读同一行才会。
    """
    text = str(text or "")
    if len(text) < _RUNAWAY_MIN_FRAGMENT:
        return False
    from collections import Counter

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    n = len(text)
    # 行级重复：同一行复读 ≥5 次且占片段多数
    counts = Counter(lines)
    if any(c >= _RUNAWAY_MIN_REPEAT and c * len(line) >= n * _RUNAWAY_DOMINANCE
           for line, c in counts.items()):
        return len(lines) < _RUNAWAY_MIN_REPEAT or             len(set(lines)) <= len(lines) * _RUNAWAY_DISTINCT_LINES
    # 窗口级重复：60+ 字的窗口在锚点采样中大量重复
    step = max(_RUNAWAY_WINDOW, n // 32)
    anchors = [text[i:i + _RUNAWAY_WINDOW] for i in range(0, n - _RUNAWAY_WINDOW, step)]
    if len(anchors) < 4:
        return False
    anchor_counts = Counter(anchors)
    repeated = sum(c for c in anchor_counts.values() if c >= 2)
    return repeated >= max(4, len(anchors) * _RUNAWAY_DOMINANCE)


def _is_inner_voice(piece: str) -> bool:
    """Detect leaked chain-of-thought / decision process / system internals."""
    return bool(_INNER_VOICE_RE.search(piece))


def _is_tool_garbage(piece: str) -> bool:
    """Detect hallucinated tool-call syntax that leaked into message text."""
    return bool(_TOOL_GARBAGE_RE.search(piece))


# 整包计划/工具 JSON 泄漏：`{"thought":...}` 这类开头曾绕过只查 {"mode"/{"action" 的旧守卫
_PLAN_JSON_MARKERS = ('"mode"', '"segments"', '"thought"', '"actions"', '"action"',
                      '"tool_calls"', '"tool":', '"tool_name"', '"reply_to_message_id"')


def is_plan_json_leak(text: Any) -> bool:
    """文本是否为整包计划/工具调用 JSON（以 {/[/``` 开头且带计划关键字）。"""
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped).strip()
    if not stripped.startswith(("{", "[")):
        return False
    head = stripped[:400]
    return any(marker in head for marker in _PLAN_JSON_MARKERS)


_LOCAL_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\[^\s\"'，。；]*|\\\\[^\s]+|/(?:tmp|var|home|root|Users)/[^\s\"'，。；]*)")
_PHANTOM_CALL_RE = re.compile(
    r"(?:请(?:立即)?(?:调用|执行|使用)|需(?:要)?调用|调用)\s*[a-z_][a-z0-9_]{2,}\s*\(")


_TOOL_MARKUP_RE = re.compile(
    r"<\s*(?:sticker|image|img|at|face|file|record|video|node|tool|function|invoke)\b"
    r"|</\s*(?:sticker|image|at|face|tool|function|invoke)\s*>"
    r"|send_sticker\s*\(|\w+_tool\s*\(")


_MEDIA_NOUN = r"(?:表情包|表情|图片|图|照片|卡片|聊天记录|文件|语音|视频|转发|合并转发|消息)"
_STATUS_VERB = r"(?:已发|已发送|已发出|已送达|已发送完毕|发送成功|发送完成|已发出去了|已发出去|已完成|完成)"
# 允许"媒体名词 + 状态动词"或"状态动词 + 媒体名词"两种语序：
# 表情包已发 / 已发表情包 / 转发已完成 / 合并转发已发出
_STATUS_NARRATION_RE = re.compile(
    r"^[\s（(【\[－—-]*(?:"
    + _MEDIA_NOUN + r"?\s*" + _STATUS_VERB
    + r"|" + _STATUS_VERB + r"\s*" + _MEDIA_NOUN + r"?"
    + r")[\s！!。.~～）)】\]－—-]*$")


# 指示语：上面那段 / 这张图 / 那条消息……（实录："（已发上面那段）"）
# 名词可以省：只说"上面那段""这张"也算（动词在场时不会误伤正常话）
_DEIXIS = (r"(?:上[面头]|下[面头]|前[面头])?(?:那|这)(?:段|张|条|个|些|份)?"
           r"(?:话|段话|图|图片|照片|卡片|文件|表情包|表情|消息|记录|转发|东西|内容|文字)?")
_STATUS_WORD = (r"(?:已发(?:了|出|送|好)?|发(?:了|出|送|完|好)|已发送|已送达|发送成功|"
                r"发送完成|已完成|完成|已贴|贴好)")


_BRACKET_CHARS = (" \t\u3000（）()【】[]{}<>「」『』"
                  "－—-~～！!？?。.、,，；;：:")


def _strip_brackets(value: str) -> str:
    """去掉首尾的括号与标点（**全角半角都要去**：漏了「）」会让规则整条失效）。"""
    return value.strip(_BRACKET_CHARS)


# 失败类播报：实录（用户截图）bot 说"表情包没发出去 反正这比赛我是真看不出啥含金量"——
# 那是工具报错被它转述成了聊天内容。工具失败自己消化，绝不向群友汇报。
_FAIL_REPORT_RE = re.compile(
    r"(?:表情包|表情|图片|图|文件|语音|视频|转发|卡片|消息)?"
    r"(?:没|没有|未能|没能|无法|不能|发送失败|发不出去|失败了|发送不出去)"
    r"(?:发出去|发出|发送|送达|发出去|贴出去|发出来)?")


_STAGE_DIRECTION_VERBS = ("丢", "发", "甩", "扔", "丢出来", "发出来", "递", "贴", "补", "来一句")
_STAGE_DIRECTION_NOUNS = ("表情包", "表情", "图片", "图", "卡片", "记录", "转发", "照片", "截图")


def is_stage_direction(text: Any) -> bool:
    """整条消息是**舞台提示**：写着"（把刚才那张表情包丢出来）"这类旁白，而不是真的说话。

    实录（用户截图）：bot 发了一句"（把刚才那张呆滞无语的表情包丢出来）"——它把自己的
    动作当成台词写出来了。真发表情就直接发，不需要旁白；配音式的动作说明一律丢掉。
    """
    stripped = " ".join(str(text or "").split())
    if not stripped or len(stripped) > 40:
        return False
    core = _strip_brackets(stripped)
    if not core or core == stripped:
        return False            # 必须整条被括号包着才算旁白
    if not any(noun in core for noun in _STAGE_DIRECTION_NOUNS):
        return False
    return any(verb in core for verb in _STAGE_DIRECTION_VERBS)


def is_tool_name_leak(text: Any, tool_names: "Iterable[str] | None" = None) -> bool:
    """消息里出现了**工具名**——聊天里不该有工具名，那是内部机制外泄。

    实录（用户截图）："（按指令 sticker 已通过 send_sticker 发出 最终回复即上文文字）"——
    它把工具调用过程当台词写出来了（旧守卫只认 `xx_tool(` 这种带括号的形式，漏了裸名）。
    工具名取自插件实际可调用的工具表（权威名单），命中即视为泄漏。
    """
    stripped = " ".join(str(text or "").split())
    if not stripped:
        return False
    names = [str(name) for name in (tool_names or ()) if str(name).strip()]
    for name in names:
        if len(name) < 4:
            continue
        if re.search(r"(?<![0-9a-zA-Z_])" + re.escape(name) + r"(?![0-9a-zA-Z_])", stripped,
                     re.IGNORECASE):
            return True
    return False


def is_failure_report(text: Any) -> bool:
    """整条消息就是"我没发出XX/XX发送失败"这种失败汇报（含否定的短句）。

    只拦"媒体 + 否定/失败"的组合，正常吐槽（"这比赛没含金量"）不受影响。
    """
    stripped = " ".join(str(text or "").split())
    if not stripped or len(stripped) > 32:
        return False
    core = _strip_brackets(stripped)
    if not core:
        return False
    # 无括号的两字短句（"发送""发出"）可能是正经回复，别误杀
    if len(core) < 3 and core == stripped:
        return False
    media = "(?:表情包|表情|图片|图|文件|语音|视频|转发|卡片)"
    neg = "(?:没|没有|未能|没能|无法|不能|失败|没成功)"
    action = ("(?:发出去|发出|发送|送达|发出来|贴出去|贴出来|"
              "发不出去|发不出来|贴不出去|没发出去)")
    tail = "(?:了|失败|不了|没成功)?"
    # 只认"媒体 + （否定）+ 发送动作"的整条短句；正常吐槽（"这比赛没含金量"）不受影响
    patterns = (
        "^" + media + "?" + neg + "?" + action + tail + "$",
        "^" + media + action + tail + "$",
        "^(?:我)?" + neg + action + "?" + media + "?$",
    )
    return any(re.match(pattern, core) for pattern in patterns)


def is_tool_status_narration(text: Any) -> bool:
    """整条消息就是"工具干完活了"的状态播报（表情包已发 / 图片已发送 / 转发完成）。

    实录（用户截图）：bot 先发一条"（表情包已发）"再发那张表情——那是我们工具的
    返回值（`已发表情包到群X`）被它改写成了聊天内容。工具结果不是台词，不该出现在群里；
    要报告进度就得说人话（"发你了"），整条只有状态词的，直接丢掉。
    """
    stripped = str(text or "").strip()
    if not stripped or len(stripped) > 24:
        return False
    if _STATUS_NARRATION_RE.match(stripped):
        return True
    # 第二种：状态动词配指示代词（"已发上面那段"、"上面那段已发"、"已发好了"）
    # ——整条必须只剩这些词，带别的内容就不算（"上面那段我重发了"是正常话）
    core = _strip_brackets(stripped)
    if not core:
        return False
    # 无括号的极短句（"发了""好了"）可能是正经回答，别误杀；带括号就一定是状态注记
    if len(core) < 3 and core == stripped:
        return False
    pattern = re.compile(
        r"^(?:(?:" + _STATUS_WORD + r")(?:\s*" + _DEIXIS + r")?"
        r"|[0-9]*\.?\s*" + _DEIXIS + r"\s*(?:" + _STATUS_WORD + r"))(?:\s*了)?$")
    return bool(pattern.match(core))


def is_tool_markup_leak(text: Any) -> bool:
    """文本里带了"工具/伪 XML 标记"——那是模型把调用语法当消息发出来了。

    实录（用户截图）：私聊里发出去一条 `<sticker sticker_id="9dc8..."/>`，
    用户看到的就是这段源码。真发表情包要走 send_sticker 或计划里的 sticker 段。
    """
    stripped = str(text or "").strip()
    if not stripped:
        return False
    return bool(_TOOL_MARKUP_RE.search(stripped))


def is_local_path_leak(text: Any) -> bool:
    """文本里掺了服务器本地文件路径，或让用户去调用某个"工具(参数)"。

    实录（截图）：bot 把 `C:\\Users\\ADMINI~1\\AppData\\Local\\Temp\\custom_xxx.png`
    和自己的臆想调用 `push_image_to_device(image_path=..., page_id=...)` 当回复发给了用户。
    这是把"工具产出的中间结果"当"要对人说的话"——必须拦在发送层。
    """
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if _PHANTOM_CALL_RE.search(stripped):
        return True
    return bool(_LOCAL_PATH_RE.search(stripped))


def salvage_message_plan(raw: Any, config: HumanizationConfig | None = None) -> MessagePlan:
    """Recover a plan from a malformed LLM reply; raises when nothing is usable.

    Never returns the raw reply verbatim: JSON noise is stripped and only real
    text content survives.
    """
    config = config or HumanizationConfig()
    text = _response_text(raw)
    texts: list[str] = []
    if text.lstrip().startswith("{"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            if value.get("mode") == "sequence" and isinstance(value.get("segments"), list):
                items: list = value["segments"]
            else:
                items = [value.get("message") or {}]
            for item in items:
                if not isinstance(item, dict):
                    continue
                inner = item.get("text")
                if isinstance(inner, Mapping):
                    inner = inner.get("text", inner.get("content", ""))
                if isinstance(inner, str) and inner.strip() \
                        and not _is_tool_garbage(inner) and not _is_inner_voice(inner) \
                        and not is_plan_json_leak(inner):
                    texts.append(inner.strip()[: config.max_segment_chars])
    if not texts and not text.lstrip().startswith("{"):
        # 只有当文本真的像泄漏的计划 JSON（计划关键字+花/方括号同时出现）
        # 才剥离 JSON 噪声；普通散文里的 [] {} () 必须原样保留。
        looks_like_plan_leak = bool(_PLAN_LEAK_RE.search(text)) and bool(
            re.search(r"[{}\[\]]", text))
        cleaned = text
        if looks_like_plan_leak:
            for junk in ("{", "}", "[", "]", "\"", "mode", "sequence", "segments",
                         "action", "text", "delay_seconds", "reply_to_message_id",
                         "reply_to", "single", "message"):
                cleaned = cleaned.replace(junk, " ")
        texts = [
            piece.strip()[: config.max_segment_chars]
            for piece in conservative_split_text(
                cleaned, max_segments=config.max_segments,
                min_chars=config.split_min_chars, target_chars=config.split_target_chars,
            )
            if piece.strip() and len(piece.strip()) > 1
            and not _is_tool_garbage(piece)
            and not _is_inner_voice(piece)
            and not is_plan_json_leak(piece)
        ]
    texts = [piece for piece in texts if piece][: config.max_segments]
    if not texts:
        raise PlanValidationError("nothing salvageable in LLM reply")
    if len(texts) == 1:
        return parse_message_plan(
            json.dumps({"mode": "single", "message": {"action": "text", "text": texts[0]}}),
            config,
        )
    return parse_message_plan(
        json.dumps({
            "mode": "sequence",
            "segments": [{"action": "text", "text": piece} for piece in texts],
        }),
        config,
    )


def _parse_action(value: Any, config: HumanizationConfig) -> PlannedAction:
    if not isinstance(value, dict) or set(value) - _SEGMENT_FIELDS:
        raise PlanValidationError("segment must be an object with known fields")
    if "action" not in value or not isinstance(value["action"], str):
        raise PlanValidationError("segment action must be a string")
    action = value["action"]
    if action not in _ALLOWED_ACTIONS:
        raise PlanValidationError("invalid segment action")
    text = value.get("text")
    if text is not None and not isinstance(text, str):
        raise PlanValidationError("text must be a string")
    if action == "text":
        text = (text or "").strip()
        if not text:
            raise PlanValidationError("text action cannot be empty")
        if len(text) > config.max_segment_chars:
            raise PlanValidationError("text segment is too long")
    elif text is not None:
        raise PlanValidationError("text is only valid for text actions")
    reply_value = value.get("reply_to_message_id", value.get("reply_to"))
    if "reply_to_message_id" in value and "reply_to" in value:
        raise PlanValidationError("use only one reply field")
    at_raw = value.get("at")
    at: tuple[str, ...] = ()
    if at_raw is not None:
        if isinstance(at_raw, (str, int)) and not isinstance(at_raw, bool):
            at_raw = [at_raw]
        if not isinstance(at_raw, list) or len(at_raw) > 5:
            raise PlanValidationError("at must be a list of at most 5 targets")
        at = tuple(_optional_identifier(item, "at") or "" for item in at_raw)
        if any(not item for item in at):
            raise PlanValidationError("at targets cannot be empty")
        at = tuple(dict.fromkeys(at))
    fields = {
        "sticker_id": _optional_identifier(value.get("sticker_id"), "sticker_id"),
        "face_id": _optional_identifier(value.get("face_id"), "face_id"),
        "target_id": _optional_identifier(value.get("target_id"), "target_id"),
        "emoji_id": _optional_identifier(value.get("emoji_id"), "emoji_id"),
        "query": _optional_identifier(value.get("query"), "query"),
    }
    required = {
        "sticker": "sticker_id", "face": "face_id", "poke": "target_id",
        "reaction": "emoji_id", "agent": "query",
    }
    expected = required.get(action)
    for name, field_value in fields.items():
        if name == expected:
            if field_value is None:
                raise PlanValidationError(f"{action} action requires {name}")
        elif field_value is not None:
            raise PlanValidationError(f"{name} is invalid for {action} action")
    # 媒体段（本机图片/文件/语音/视频直发）：media_id 或 path 至少给一个
    media_id = _optional_identifier(value.get("media_id"), "media_id")
    media_path = _optional_identifier(value.get("path"), "path")
    file_name = _optional_identifier(value.get("file_name"), "file_name")
    if action in _MEDIA_ACTIONS:
        if not (media_id or media_path):
            raise PlanValidationError(f"{action} action requires media_id or path")
    elif media_id is not None or media_path is not None or file_name is not None:
        raise PlanValidationError("media_id/path/file_name only valid for media actions")
    delay = _number(value.get("delay_seconds", 0), "delay_seconds",
                    config.max_delay_seconds)
    typing: float | None = None
    if "typing_delay_seconds" in value:
        typing = _number(value["typing_delay_seconds"], "typing_delay_seconds",
                         config.max_typing_delay_seconds)
        if action != "text":
            raise PlanValidationError("typing delay is only valid for text actions")
    return PlannedAction(
        action=action, text=text, delay_seconds=delay, typing_delay_seconds=typing,
        reply_to_message_id=_optional_identifier(reply_value, "reply_to_message_id"),
        at=at,
        media_id=media_id, path=media_path, file_name=file_name,
        **fields,
    )


def _validate_plan_totals(segments: Iterable[PlannedAction], config: HumanizationConfig) -> None:
    items = tuple(segments)
    total_chars = sum(len(item.text or "") for item in items)
    total_wait = sum(item.delay_seconds + (item.typing_delay_seconds or 0) for item in items)
    if total_chars > config.max_total_chars:
        raise PlanValidationError("plan exceeds total character limit")
    if total_wait > config.max_total_wait_seconds:
        raise PlanValidationError("plan exceeds total explicit wait limit")


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    ranges = [match.span() for pattern in (_CODE_RE, _URL_RE) for match in pattern.finditer(text)]
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            pass
        else:
            start = len(text) - len(text.lstrip())
            ranges.append((start, start + len(stripped)))
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def conservative_split_text(text: str, *, max_segments: int = 5,
                            min_chars: int = 48, target_chars: int = 80) -> list[str]:
    """Split prose conservatively while treating URLs, code and valid JSON as atomic."""
    stripped = text.strip()
    if not stripped or max_segments <= 1 or len(stripped) <= target_chars:
        return [stripped] if stripped else []
    ranges = _protected_ranges(stripped)

    def protected(index: int) -> bool:
        return any(start <= index < end for start, end in ranges)

    candidates: list[int] = []
    for index, char in enumerate(stripped):
        if protected(index):
            continue
        if char == "\n" or char in "。！？!?；;":
            candidates.append(index + 1)
        elif char in "，、：:" and index + 1 >= min_chars:
            candidates.append(index + 1)
    if not candidates:
        return [stripped]
    parts: list[str] = []
    start = 0
    while len(parts) < max_segments - 1:
        viable = [point for point in candidates if point - start >= min_chars]
        if not viable:
            break
        preferred = [point for point in viable if point - start >= target_chars]
        point = preferred[0] if preferred else viable[-1]
        if len(stripped) - point < max(8, min_chars // 3):
            break
        part = stripped[start:point].strip()
        if part:
            parts.append(part)
        start = point
    tail = stripped[start:].strip()
    if tail:
        parts.append(tail)
    return parts or [stripped]


def humanize_plan(plan: MessagePlan, config: HumanizationConfig | None = None,
                  style: RecentStyle | None = None) -> MessagePlan:
    """Honor explicit sequence segments; only auto-split a single prose text action."""
    config = config or HumanizationConfig()
    if plan.explicit_segments or len(plan.segments) != 1 or plan.segments[0].action != "text":
        _validate_plan_totals(plan.segments, config)
        return plan
    original = plan.segments[0]
    target = style.suggested_segment_chars if style else config.split_target_chars
    pieces = conservative_split_text(
        original.text or "", max_segments=config.max_segments,
        min_chars=config.split_min_chars, target_chars=max(config.split_min_chars, target),
    )
    if len(pieces) <= 1:
        return plan
    inter_delay = style.suggested_inter_segment_delay_seconds if style else (
        config.auto_inter_segment_delay_seconds)
    inter_delay = min(max(inter_delay, 0.0), config.max_delay_seconds)
    actions = tuple(
        PlannedAction(
            action="text", text=piece,
            delay_seconds=original.delay_seconds if index == 0 else inter_delay,
            typing_delay_seconds=original.typing_delay_seconds if index == 0 else None,
            reply_to_message_id=original.reply_to_message_id if index == 0 else None,
        )
        for index, piece in enumerate(pieces)
    )
    _validate_plan_totals(actions, config)
    return MessagePlan("sequence", actions, False)


def analyze_recent_style(messages: Iterable[Any]) -> RecentStyle:
    """Aggregate anonymous conversation-level style signals without copying identities."""
    samples: list[tuple[str, datetime | None]] = []
    for message in messages:
        if isinstance(message, Mapping):
            raw_text = message.get("text", "")
            raw_time = message.get("occurred_at", message.get("timestamp"))
        else:
            raw_text = getattr(message, "text", "")
            raw_time = getattr(message, "occurred_at", getattr(message, "timestamp", None))
        if not isinstance(raw_text, str) or not raw_text.strip():
            continue
        samples.append((raw_text.strip(), _parse_datetime(raw_time)))
    if not samples:
        return RecentStyle(0, 0.0, 0.0, 0.0, 0.0, (), None, 80, 0.4)
    lengths = [len(text) for text, _ in samples]
    total_chars = max(sum(lengths), 1)
    punctuation_rate = sum(len(_PUNCTUATION_RE.findall(text)) for text, _ in samples) / total_chars
    filler_rate = sum(len(_FILLER_RE.findall(text)) for text, _ in samples) / len(samples)
    emoji_rate = sum(len(_EMOJI_RE.findall(text)) for text, _ in samples) / len(samples)
    hours = Counter(timestamp.hour for _, timestamp in samples if timestamp is not None)
    active_hours = tuple(hour for hour, _ in hours.most_common(3))
    ordered_times = sorted(timestamp for _, timestamp in samples if timestamp is not None)
    intervals = [
        (later - earlier).total_seconds()
        for earlier, later in zip(ordered_times, ordered_times[1:])
        if 0 <= (later - earlier).total_seconds() <= 120
    ]
    median = statistics.median(intervals) if intervals else None
    average = statistics.fmean(lengths)
    suggested_chars = min(60, max(10, round(max(average * 1.2, 10))))
    suggested_delay = min(2.0, max(0.2, (median or 0.4) * 0.2))
    return RecentStyle(
        len(samples), average, punctuation_rate, filler_rate, emoji_rate,
        active_hours, median, suggested_chars, suggested_delay,
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# __HUMANIZATION_APPEND__


class HumanizedSender:
    """Executes a MessagePlan with human-like pacing, cancellation, and hourly limits."""

    def __init__(
        self,
        send_callback: Callable[[dict[str, Any]], Any],
        *,
        config: HumanizationConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.config = config or HumanizationConfig()
        self.send_callback = send_callback
        self._clock = clock
        self._sleep = sleep
        self._random_uniform = random_uniform
        self._sequence_times: deque[float] = deque()

    def _sequence_quota_available(self) -> bool:
        now = self._clock()
        cutoff = now - 3600
        while self._sequence_times and self._sequence_times[0] <= cutoff:
            self._sequence_times.popleft()
        return len(self._sequence_times) < self.config.max_sequences_per_hour

    async def send(
        self,
        plan: MessagePlan,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> SendResult:
        if not self._sequence_quota_available():
            return SendResult("rate_limited", len(plan.segments), 0, reason="hourly sequence limit")
        self._sequence_times.append(self._clock())
        outcomes: list[SegmentOutcome] = []
        sent = 0
        for index, action in enumerate(plan.segments):
            if is_cancelled and is_cancelled():
                return SendResult(
                    "cancelled", len(plan.segments), sent, tuple(outcomes),
                    reason="superseded by newer activity", failed_index=index,
                )
            wait = action.delay_seconds
            if action.action == "text":
                typing = action.typing_delay_seconds
                if typing is None:
                    typing = min(
                        len(action.text or "") / self.config.typing_chars_per_second,
                        self.config.max_typing_delay_seconds,
                    )
                    if self.config.typing_jitter_seconds > 0:
                        typing = max(0.0, typing * (1 + self._random_uniform(
                            -self.config.typing_jitter_seconds, self.config.typing_jitter_seconds)))
                wait += typing
            total_wait = wait
            if total_wait > 0:
                await self._sleep(total_wait)
            if is_cancelled and is_cancelled():
                return SendResult(
                    "cancelled", len(plan.segments), sent, tuple(outcomes),
                    reason="superseded before send", failed_index=index,
                )
            try:
                value = self.send_callback(action.to_descriptor())
                if inspect.isawaitable(value):
                    await value
                outcomes.append(SegmentOutcome(index, action.action, "sent"))
                sent += 1
            except Exception as first_error:
                # transient QQ risk-control hiccups: retry once after a short pause
                await self._sleep(1.5)
                try:
                    value = self.send_callback(action.to_descriptor())
                    if inspect.isawaitable(value):
                        await value
                    outcomes.append(SegmentOutcome(index, action.action, "sent"))
                    sent += 1
                except Exception as error:
                    outcomes.append(SegmentOutcome(
                        index, action.action, "failed",
                        type(error).__name__, str(error)[:300],
                    ))
                    return SendResult(
                        "failed", len(plan.segments), sent, tuple(outcomes),
                        reason=f"segment {index} failed: {type(error).__name__}",
                        failed_index=index,
                    )
        return SendResult("completed", len(plan.segments), sent, tuple(outcomes))
