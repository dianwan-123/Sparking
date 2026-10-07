"""情绪系统 v2（参考 private_companion 的 affect 域与 angel_memory 的 soul_state）。

三通道构成：
1. **基础心境**（mood）：2~3 字主导情绪词 + 强度 + 一句话原因，LLM/管理端可设，
   持久化到 mood.json（v1 兼容，老文件直接读）。
2. **效价/唤醒**（valence/arousal，-1..1）：事件驱动的连续情绪轴，带半衰期衰减
   （private_companion 的 compose_affect_modulation 思路）：每次互动产生带置信度的
   效价增量，按半衰期指数衰减；合成时按权重平均，而不是最后一条覆盖前史。
3. **表达温度**（reply_temperature，参考 reply_temperature.py）：把效价/唤醒/精力
   投影成 guarded/neutral/warm/close 四档，直接决定回复的亲近度提示词——比"当前
   情绪词"更能稳定影响语气。

情绪惯性：能量向中点回归（橡皮筋），极端情绪不会永久保持。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HALF_LIFE_DEFAULT = 3600.0 * 6  # 效价增量默认半衰期 6 小时
_SOFT_LIMIT = 1.0

_TEMPERATURE_TIERS = ("guarded", "neutral", "warm", "close")
_TIER_SCORE = {"guarded": 0.15, "neutral": 0.45, "warm": 0.70, "close": 0.90}
_TIER_PROMPT = {
    "guarded": "保持简短克制，不主动扩展话题，礼貌但有距离。",
    "neutral": "自然交流，语气平和。",
    "warm": "语气温和友好，可以主动多聊两句。",
    "close": "亲近随意，可以开玩笑、用昵称、语气词更多。",
}


@dataclass(slots=True)
class MoodState:
    mood: str = "平静"
    intensity: float = 0.3
    note: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "mood": self.mood, "intensity": round(self.intensity, 2),
            "note": self.note, "updated_at": self.updated_at,
        }

    def as_prompt(self) -> str:
        return (
            f"当前情绪：{self.mood}（强度 {self.intensity:.1f}）。"
            f"{('最近心绪：' + self.note) if self.note else ''}"
            "情绪只影响语气与主动性，不得改变事实与安全边界。"
        )


@dataclass(slots=True)
class AffectDelta:
    """一次互动产生的情绪增量（事件驱动，带置信度与半衰期）。"""

    valence: float          # -1 负面 .. +1 正面
    arousal: float          # -1 平静 .. +1 激动
    confidence: float = 0.5
    half_life_seconds: float = _HALF_LIFE_DEFAULT
    source: str = ""
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()
        self.valence = max(-1.0, min(1.0, float(self.valence)))
        self.arousal = max(-1.0, min(1.0, float(self.arousal)))
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.half_life_seconds = max(60.0, float(self.half_life_seconds))


class AffectState:
    """效价/唤醒连续情绪轴（事件增量 + 半衰期衰减 + 橡皮筋回归）。"""

    def __init__(self) -> None:
        self.deltas: list[AffectDelta] = []

    def add(self, delta: AffectDelta) -> None:
        self.deltas.append(delta)
        cutoff = time.time() - 48 * 3600
        self.deltas = [d for d in self.deltas if d.created_at >= cutoff][-200:]

    def compose(self, now: float | None = None) -> dict[str, Any]:
        """合成当前效价/唤醒（半衰期加权平均 + 橡皮筋回归原点）。"""
        now = now if now is not None else time.time()
        valence = arousal = total = 0.0
        for delta in self.deltas:
            age = max(0.0, now - delta.created_at)
            decay = 0.5 ** (age / delta.half_life_seconds)
            weight = delta.confidence * decay
            if weight <= 0:
                continue
            valence += delta.valence * weight
            arousal += delta.arousal * weight
            total += weight
        if total > 0:
            valence /= total
            arousal /= total
        valence = _rubber_band(valence)
        arousal = _rubber_band(arousal)
        return {
            "valence": round(valence, 3),
            "arousal": round(arousal, 3),
            "confidence": round(min(1.0, total), 3),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "deltas": [
                {
                    "valence": d.valence, "arousal": d.arousal,
                    "confidence": d.confidence,
                    "half_life_seconds": d.half_life_seconds,
                    "source": d.source, "created_at": d.created_at,
                }
                for d in self.deltas
            ]
        }

    def load_dict(self, data: dict[str, Any]) -> None:
        rows = (data or {}).get("deltas") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                self.deltas.append(AffectDelta(
                    valence=float(row.get("valence", 0)),
                    arousal=float(row.get("arousal", 0)),
                    confidence=float(row.get("confidence", 0.5)),
                    half_life_seconds=float(row.get("half_life_seconds", _HALF_LIFE_DEFAULT)),
                    source=str(row.get("source", "")),
                    created_at=float(row.get("created_at", time.time())),
                ))
            except (TypeError, ValueError):
                continue


def _rubber_band(value: float) -> float:
    """越远离 0 回归力越强：v' = v - elasticity * v * |v|（软限幅）。"""
    elasticity = 0.15
    if abs(value) > _SOFT_LIMIT:
        value = math.copysign(_SOFT_LIMIT, value)
    pulled = value - elasticity * value * abs(value)
    return max(-_SOFT_LIMIT, min(_SOFT_LIMIT, pulled))


def _positive_words(text: str) -> bool:
    return any(w in text for w in (
        "谢谢", "感谢", "想你", "喜欢", "开心", "好呀", "太好了", "顺利", "温柔",
        "哈哈", "笑死", "乐", "草", "可爱", "夸", "成功", "好评"))


def _negative_words(text: str) -> bool:
    return any(w in text for w in (
        "难过", "生气", "焦虑", "累", "困", "不舒服", "失望", "烦", "滚", "闭嘴",
        "傻", "笨", "垃圾", "没用", "讨厌", "投诉", "失败", "崩了", "寄"))


def classify_interaction(text: str) -> AffectDelta | None:
    """从一条用户消息粗分类情绪增量（确定性规则，零 LLM 成本）。"""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return None
    valence = arousal = 0.0
    if _positive_words(cleaned):
        valence += 0.35
        arousal += 0.15
    if _negative_words(cleaned):
        valence -= 0.45
        arousal += 0.25
    if any(m in cleaned for m in ("！", "！", "??", "？？", "草", "乐")):
        arousal += 0.1
    if abs(valence) < 0.05 and abs(arousal) < 0.05:
        return None
    return AffectDelta(valence=valence, arousal=arousal, confidence=0.4,
                       source="interaction")


def compose_reply_temperature(affect: dict[str, Any],
                              mood_intensity: float = 0.3) -> dict[str, Any]:
    """把效价/唤醒/心境强度投影成回复温度四档（参考 reply_temperature.py）。

    warm 需要正面情绪支撑；负面情绪直接压到 guarded；高唤醒略提表达欲。
    """
    valence = float(affect.get("valence", 0))
    arousal = float(affect.get("arousal", 0))
    score = 0.45  # neutral 起点
    score += valence * 0.35
    score += (0.5 - abs(mood_intensity - 0.5)) * 0.05
    if arousal > 0.5 and valence >= 0:
        score += 0.05
    if valence < -0.3:
        score = min(score, 0.2)
    score = max(0.05, min(0.95, score))
    tier = _TEMPERATURE_TIERS[0]
    for name, tier_score in _TIER_SCORE.items():
        if score >= tier_score - 0.15:
            tier = name
    return {"tier": tier, "score": round(score, 3),
            "valence": round(valence, 3), "arousal": round(arousal, 3)}


def temperature_prompt(tier: str) -> str:
    return _TIER_PROMPT.get(tier, _TIER_PROMPT["neutral"])


class MoodStore:
    """情绪持久化：mood.json（v1 兼容）+ affect.json（v2 增量历史）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.affect_path = self.path.parent / "affect.json"
        self._state = {
            "mood": "平静", "intensity": 0.3, "note": "", "updated_at": "",
        }
        self.affect = AffectState()
        self.load()

    def load(self) -> None:
        try:
            self._state = {**self._state,
                           **json.loads(self.path.read_text(encoding="utf-8"))}
        except (OSError, json.JSONDecodeError):
            pass
        try:
            self.affect.load_dict(json.loads(self.affect_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            self.affect_path.write_text(
                json.dumps(self.affect.as_dict(), ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def get(self) -> MoodState:
        return MoodState(
            mood=str(self._state.get("mood", "平静"))[:24],
            intensity=float(self._state.get("intensity", 0.3)),
            note=str(self._state.get("note", ""))[:200],
            updated_at=str(self._state.get("updated_at", "")),
        )

    def set(self, mood: str, intensity: float, note: str = "") -> MoodState:
        mood = str(mood).strip()[:24]
        if not mood:
            raise ValueError("mood cannot be empty")
        intensity = min(max(float(intensity), 0.0), 1.0)
        self._state = {
            "mood": mood, "intensity": intensity,
            "note": str(note).strip()[:200],
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.save()
        return self.get()

    def observe(self, delta: AffectDelta) -> None:
        """事件驱动的情绪更新（不影响 mood 词，只动连续轴）。"""
        self.affect.add(delta)
        self.save()

    def reply_temperature(self) -> dict[str, Any]:
        mood = self.get()
        return compose_reply_temperature(self.affect.compose(), mood.intensity)

    def as_prompt(self) -> str:
        mood = self.get()
        base = mood.as_prompt()
        temperature = self.reply_temperature()
        return (f"{base}\n表达温度：{temperature['tier']}——"
                f"{temperature_prompt(temperature['tier'])}")

    def as_dict(self) -> dict[str, Any]:
        state = self.get().as_dict()
        state["affect"] = self.affect.compose()
        state["reply_temperature"] = self.reply_temperature()
        return state
