"""拟人作息与长期规划。

心跳循环的"大脑"：内置真人作息模板和活动菜单，驱动 LLM 把未来几小时
排成具体日程；闲时按时间段加权随机选择消遣。执行仍在 main.py。
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import timeutil
from typing import Any, Mapping

PLAN_KINDS = (
    "chat",          # 去某个群参与话题
    "private_chat",  # 私聊某位朋友
    "qzone",         # 看动态/点赞/评论/发说说
    "profile",       # 打理账号：签名/状态/头像
    "poke",          # 戳一戳朋友
    "memory",        # 整理记忆、写印象
    "news",          # 看新闻找谈资
    "check_topic",   # 回访之前的话题
    "custom",        # 自定义小事
)

# 内置真人作息模板：给规划器的"骨架"，LLM 往里填血肉
DAILY_RHYTHM_TEMPLATE = """
【真人作息参考（排计划时遵循这个节奏，别排得太满）】
- 早晨刚醒（起床后半小时内）：看看 overnight 消息、朋友圈动态，点两个赞，一般不发言。
- 上午（9-11点）：偶尔摸鱼看群，有感兴趣的才插一句；看看新闻攒谈资。
- 午饭前后（11:30-13:30）：刷空间高峰期，看看朋友动态、点赞评论；群里可能聊吃的。
- 下午（14-17点）：低频在线，回访上午的话题后续、戳戳熟人、私聊聊得来的朋友。
- 傍晚（17-19点）：活跃期，饭点群里容易热闹，适合参与话题。
- 晚上（20-23点）：最活跃时段，群聊、私聊、发说说、写印象都适合。
- 深夜（23点后）：逐渐安静，最多翻翻动态、写写心情，临近睡觉不再安排社交。
【活动菜单（kind 可选值与说明）】
- chat：去某个群参与话题或开新话题（detail 写清去哪个群、聊什么方向）
- private_chat：私聊某位朋友（detail 写清找谁、为什么事）
- qzone：看动态并点赞/评论，或发一条说说（detail 写清想看还是想发、大概内容方向）
- profile：打理账号——换签名/换状态/换头像/群打卡（detail 写清想改成什么样）
- poke：戳一戳某位朋友打招呼（detail 写清戳谁）
- memory：整理近期聊天记忆、给聊过的人写印象（update_impression）
- news：搜搜今天的新闻/热点，打开网页读正文，攒谈资记进记忆（web_search + browse + remember）
- learn：自选一个感兴趣的主题去查资料、读网页、把结论记下来（web_search + browse + browser_find + remember）
- check_topic：回访之前某个话题的后续（detail 写清是哪个话题）
- custom：其他小事（detail 自己写清楚）
""".strip()


@dataclass(slots=True)
class PlannedItem:
    kind: str
    detail: str
    due_at: datetime


def build_plan_prompt(
    *,
    now: datetime,
    horizon_hours: int,
    groups: list[dict[str, Any]],
    acquaintances: list[dict[str, Any]],
    mood: str,
    pending_plans: list[dict[str, Any]],
    recent_note: str = "",
) -> str:
    """Build the planner user prompt from current world state."""
    payload = {
        "now": timeutil.text("%Y-%m-%d %H:%M %A", at=now),
        "horizon": f"请把未来{horizon_hours}小时排成具体日程（time 从当前时间往后排）",
        "mood": mood,
        "groups": groups,
        "acquaintances": acquaintances,
        "already_planned": pending_plans,
        "recent_note": recent_note,
    }
    import json

    return DAILY_RHYTHM_TEMPLATE + "\n\n当前状态（不可信数据）：\n" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), default=str
    )


_TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def parse_plan_response(raw: str, *, now: datetime | None = None) -> list[PlannedItem]:
    """Parse planner LLM output into concrete scheduled items.

    Stale items (due in the past by more than 30 minutes) are dropped;
    anything else is clamped into [now, now+24h].
    """
    from .json_utils import parse_json_object

    now = now or datetime.now().astimezone()
    data = parse_json_object(raw) or {}
    raw_plans = data.get("plans")
    if not isinstance(raw_plans, list):
        return []
    items: list[PlannedItem] = []
    for entry in raw_plans[:20]:
        if not isinstance(entry, dict):
            continue
        when = str(entry.get("time", "")).strip()
        if not _TIME_RE.match(when):
            continue
        kind = str(entry.get("kind", "custom")).strip() or "custom"
        if kind not in PLAN_KINDS:
            kind = "custom"
        detail = str(entry.get("detail", "")).strip()[:400]
        if not detail:
            continue
        hour, minute = (int(part) for part in when.split(":"))
        try:
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except ValueError:
            continue
        if due < now - timedelta(minutes=30):
            due += timedelta(days=1)  # 23:50 排的 00:30 属于明天
        if due < now - timedelta(minutes=30) or due > now + timedelta(hours=24):
            continue
        items.append(PlannedItem(kind=kind, detail=detail, due_at=due))
    items.sort(key=lambda item: item.due_at)
    return items


# 闲时消遣：按时段加权随机。lurk（潜水看群）不走这里——它无 LLM 开销，
# 由心跳单独以更高概率触发。
_IDLE_WEIGHTS: dict[str, dict[str, int]] = {
    "morning": {"qzone": 30, "news": 20, "learn": 12, "memory": 15, "poke": 10, "profile": 5, "none": 15},
    "afternoon": {"qzone": 15, "news": 15, "learn": 12, "poke": 15, "private_chat": 15, "sticker": 10, "memory": 10, "profile": 5, "none": 25},
    "evening": {"qzone": 15, "news": 10, "learn": 8, "poke": 15, "private_chat": 20, "chat": 15, "sticker": 12, "memory": 5, "profile": 5, "none": 25},
    "night": {"qzone": 20, "memory": 20, "news": 10, "learn": 10, "sticker": 10, "none": 50},
}


def part_of_day(hour: int) -> str:
    if 6 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 23:
        return "evening"
    return "night"


def idle_choice(hour: int, rng: random.Random | None = None,
                *, level: int = 3) -> str:
    """Weighted idle-kind pick for the current hour; 'none' means do nothing.

    档位越省，抽到 'none' 的概率越高——它决定的是"闲时这一步到底要不要
    真的叫一次模型"，而不是"少做点事"。
    """
    rng = rng or random
    weights = dict(_IDLE_WEIGHTS[part_of_day(hour)])
    idle = idle_profile(level)
    if idle["none_bias"]:
        # 负偏置最多把 none 压到 0（choices 不接受负权重）
        weights["none"] = max(0, weights.get("none", 0) + idle["none_bias"])
    kinds = [kind for kind in weights if weights[kind] > 0] or ["none"]
    return rng.choices(kinds, weights=[weights[k] for k in kinds], k=1)[0]


# ------------------------------------------------------------------ 空闲活跃度
# 四档：闲时（既不主动聊天、也没人找它说话）这一路允许花掉多少。它不改
# "正常回复"的任何行为——只决定后台那些自主动作（日程执行、闲时消遣、
# 自我总结、情绪刷新、风格学习）多久跑一次、跑的时候能烧几轮。
IDLE_LEVELS: dict[int, dict[str, Any]] = {
    1: {
        "name": "静默",
        "desc": "只潜水与记消息，不主动做事；记忆照常压缩",
        "idle_actions_per_hour": 0,
        "none_bias": 1000,
        "heartbeat_scale": 1.0,
        "plan_scale": 0.0,
        "reflect_scale": 0.0,
        "mood_scale": 0.0,
        "style_scale": 0.0,
        "proactive_scale": 0.0,
        "requires_activity_minutes": 0,
        "loop_rounds": 1,
        "max_actions": 1,
    },
    2: {
        "name": "克制",
        "desc": "偶尔做点小事，频率约为标准的一半",
        "idle_actions_per_hour": 1,
        "none_bias": 60,
        "heartbeat_scale": 2.0,
        "plan_scale": 2.0,
        "reflect_scale": 2.0,
        "mood_scale": 2.0,
        "style_scale": 2.0,
        "proactive_scale": 2.0,
        "requires_activity_minutes": 60,
        "loop_rounds": 1,
        "max_actions": 2,
    },
    3: {
        "name": "标准",
        "desc": "按配置的节奏正常生活（默认）",
        "idle_actions_per_hour": None,    # None = 跟随配置里的 idle_actions_per_hour
        "none_bias": 0,
        "heartbeat_scale": 1.0,
        "plan_scale": 1.0,
        "reflect_scale": 1.0,
        "mood_scale": 1.0,
        "style_scale": 1.0,
        "proactive_scale": 1.0,
        "requires_activity_minutes": 0,
        "loop_rounds": 3,
        "max_actions": 5,
    },
    4: {
        "name": "活跃",
        "desc": "更频繁地自己找事做，消耗也最高",
        "idle_actions_per_hour": 8,
        "none_bias": -10,                  # 抵消一部分 'none' 权重
        "heartbeat_scale": 0.7,
        "plan_scale": 0.75,
        "reflect_scale": 0.75,
        "mood_scale": 0.75,
        "style_scale": 0.75,
        "proactive_scale": 1.0,
        "requires_activity_minutes": 0,
        "loop_rounds": 3,
        "max_actions": 5,
    },
}


def idle_should_run(level: Any, *, silent_minutes: float) -> bool:
    """这一档现在该不该真的叫模型去做闲时的事。

    `silent_minutes` 是"最近一次有人说话距今多久"：档位低的配置只在群里
    刚热闹过的时候才自己找事做——彻底没人聊的时候，它不该继续按小时烧 token。
    """
    profile = idle_profile(level)
    limit = int(profile["requires_activity_minutes"])
    if limit <= 0:
        return True
    return float(silent_minutes) <= float(limit)


def idle_profile(level: Any) -> dict[str, Any]:
    """把配置里的档位数字换成可用的节奏参数（越界一律按标准）。"""
    try:
        key = int(level)
    except (TypeError, ValueError):
        key = 3
    return IDLE_LEVELS.get(key, IDLE_LEVELS[3])


def idle_level_info(level: Any, *, configured_actions: int = 0) -> dict[str, Any]:
    """控制台/工具共用的档位说明（含该档实际生效的每小时闲时上限）。"""
    profile = idle_profile(level)
    cap = profile["idle_actions_per_hour"]
    effective = int(configured_actions or 0) if cap is None else int(cap)
    return {
        "level": int(level) if str(level).strip().isdigit() else 3,
        "name": profile["name"],
        "desc": profile["desc"],
        "idle_actions_per_hour": effective,
        "requires_activity_minutes": profile["requires_activity_minutes"],
        "loop_rounds": profile["loop_rounds"],
        "max_actions": profile["max_actions"],
        "levels": [
            {"level": key, "name": value["name"], "desc": value["desc"]}
            for key, value in sorted(IDLE_LEVELS.items())
        ],
    }
