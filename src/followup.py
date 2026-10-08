# -*- coding: utf-8 -*-
"""聊天生动性：把"话说到一半对方不说话了"这件事，当成一件可以惦记的事。

用户举的例子（要的不是"照抄这个例子"，是它背后的机制）：
> bot 和人聊到一半突然对面不说话了，持续了 xxx 时间，bot 于是可能会比较急问
> "你为啥不说话"之类的，如果一直得不到回应会自己放弃。

本质是三层，任何"真人聊天节奏"的缺口都是这三层的组合：
1. **注意到状态变化**——从"正在对话"变成"对方沉默了"（有未收尾的话头、有等待回复的提问）；
2. **有自己的情绪与冲动**——等得越久越惦记/越急，于是会再开口（而且口气会变：先随口一问、
   再有点急、最后有点委屈或干脆放弃）；
3. **有边界**——不能无限纠缠：次数有上限、间隔递增、对方一回来就立刻清零，
   静默时段不发，别群/别在冷却里发。

本模块只放**纯逻辑**（状态机 + 文案选择），存取与发送在 main 里；这样它可以被单独测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

# 追问节奏：第几次追问、间隔多久、语气
# (追问序号, 至少沉默多少秒, 语气档位)
FOLLOW_UP_LADDER: tuple[tuple[int, int, str], ...] = (
    (1, 240, "casual"),      # 4 分钟：随口一提，像刚想起来
    (2, 900, "curious"),     # 15 分钟：有点在意，问一句
    (3, 2400, "anxious"),    # 40 分钟：明显急了（"你为啥不说话"）
    (4, 7200, "resigned"),   # 2 小时：最后一句，带点"算了"的意思
)
MAX_FOLLOW_UPS = len(FOLLOW_UP_LADDER)

# 语气 → 给模型的要求（真正的话由模型说，这里只给"用什么口气"）
TONE_BRIEF: dict[str, str] = {
    "casual": "随口一提，像刚想起来还有个话头没说完；一句就够，别催。",
    "curious": "有点在意了：直接问一句刚才怎么不说话了；短、不质问。",
    "anxious": "你等得有点急了：语气能带一点急/委屈（「你咋不理我了」这种感觉），但别指责。",
    "resigned": "最后一次：带点「算了不烦你了」的意思，给对方台阶；说完就别再追了。",
}


@dataclass
class Dangling:
    """一段"话说到一半、在等对方回话"的状态。"""

    scope_id: str
    conversation: str = ""
    opened_at: str = ""            # 我们最后一次开口的时间（UTC ISO）
    topic: str = ""                # 话头（最后我们说了什么，用来让追问接得上）
    follow_ups: int = 0            # 已经追问过几次
    last_follow_up_at: str = ""
    replied: bool = False          # 对方是否回过（回来就清）
    waiting_question: bool = False  # 我们问了一个明确的问题在等回答

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id, "conversation": self.conversation,
            "opened_at": self.opened_at, "topic": self.topic[:120],
            "follow_ups": self.follow_ups, "last_follow_up_at": self.last_follow_up_at,
            "waiting_question": self.waiting_question,
        }


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def should_follow_up(dangling: Dangling, *, now: datetime,
                     quiet: bool = False, blocked: bool = False) -> tuple[bool, str, str]:
    """该不该追问这一句？返回 (要不要问, 语气, 为什么)。

    判据（全部满足才问）：
    - 不是静默时段、不在冷却/封锁里；
    - 还没用满次数；
    - 距我们上次开口，已达到下一档的等待时长。
    """
    if blocked:
        return False, "", "冷却/封锁中"
    if quiet:
        return False, "", "静默时段"
    if dangling.follow_ups >= MAX_FOLLOW_UPS:
        return False, "", "已经追问到上限，放弃"
    opened = _parse(dangling.opened_at)
    if opened is None:
        return False, "", "没有可参照的开口时间"
    reference = _parse(dangling.last_follow_up_at) or opened
    waited = (now - reference).total_seconds()
    for index, (order, gap, tone) in enumerate(FOLLOW_UP_LADDER):
        if dangling.follow_ups < order:
            if waited >= gap:
                return True, tone, f"等了 {int(waited // 60)} 分钟（第 {order} 次）"
            return False, "", f"还没到第 {order} 次追问的时长"
    return False, "", "没有可用的档位"


def next_wait_seconds(dangling: Dangling) -> int:
    """下一次最早什么时候可以再追问（秒）；已到上限返回 0。"""
    if dangling.follow_ups >= MAX_FOLLOW_UPS:
        return 0
    for order, gap, _tone in FOLLOW_UP_LADDER:
        if dangling.follow_ups < order:
            return gap
    return 0


def follow_up_brief(dangling: Dangling, tone: str) -> str:
    """给模型的追问指令：接得上话头、口气对、别像机器人。"""
    tone_hint = TONE_BRIEF.get(tone, TONE_BRIEF["casual"])
    parts = [
        "你们刚聊到一半，对方就没再回了（已经过了挺久）。现在你要再开口一次。",
        f"口气：{tone_hint}",
        "要求：一到两句、短；不要道歉式开头（「抱歉打扰」这种别用）、不要解释你在等他。",
    ]
    if dangling.topic:
        parts.append(f"接住之前的话头：「{dangling.topic[:60]}」——可以顺着它说，别装作没聊过。")
    if tone == "resigned":
        parts.append("这是最后一次：说完就把这件事放下，之后不要再追问。")
    return "\n".join(parts)


def note_reply(dangling: Dangling | None) -> None:
    """对方回话了：这段"悬着的话"就此结束。"""
    if dangling is not None:
        dangling.replied = True


def prune(states: Mapping[str, Dangling], *, now: datetime,
          max_age_hours: float = 12.0) -> dict[str, Dangling]:
    """丢掉太老的悬置状态（挂了半天以上的事就别再翻旧账了）。"""
    keep: dict[str, Dangling] = {}
    for key, item in states.items():
        opened = _parse(item.opened_at) or _parse(item.last_follow_up_at)
        if opened is None:
            continue
        if now - opened <= timedelta(hours=max(0.5, float(max_age_hours))):
            keep[key] = item
    return keep
