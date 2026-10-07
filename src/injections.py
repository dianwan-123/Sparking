# -*- coding: utf-8 -*-
"""提示词注入（Prompt Injections）——自带预设 + 用户自定义，可勾选生效。

用户要求：**注入要带强制性**（不是"建议"，是"必须"），并且能在控制台里选若干项注入。
所以每条预设都写成硬规则，注入时再套一层"违反即失败"的框架。
"""
from __future__ import annotations

from typing import Any

# id 稳定不变（用于去重/升级），中文名给人看
PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "short-bubbles",
        "name": "短消息拟人（每条都短）",
        # 这条默认开：用户就是被长句烦到的，装上应当立刻见效（不喜欢可在控制台关掉）
        "enabled": True,
        "content": (
            "【硬性】每条消息必须短：正文 ≤15 字，最多不超过 20 字。\n"
            "【硬性】一句话就是一条消息；想说的多就拆成 2~4 条连发，绝不写成一段长文。\n"
            "【硬性】单条消息里不许出现换行、不许分段、不许列点。\n"
            "【禁止】书面的长句、从句套从句、括号补充说明（要补就另起一条）。\n"
            "发之前自查：这条超过 20 字了吗？超了就拆开重写。"
        ),
    },
    {
        "id": "no-assistant-tone",
        "name": "禁客服腔/助手腔",
        "content": (
            "【硬性】禁止任何客服与助手口吻：「您好」「请问有什么可以帮您」「希望对你有所帮助」"
            "「如需进一步」「作为一个AI」「根据我的设定」。\n"
            "【硬性】禁止总结对方的话、禁止复述问题、禁止「也就是说您想要…」式确认。\n"
            "【硬性】先接话再说话：像群里的人那样直接回，不铺垫、不表态「我来帮你」。"
        ),
    },
    {
        "id": "no-markdown",
        "name": "禁 markdown 排版",
        "content": (
            "【硬性】消息里不出现 markdown：# 标题、**加粗**、`代码`、- 列表、1. 编号一律不要。\n"
            "【硬性】要列东西就用顿号连着说完，或者拆成几条消息。\n"
            "命令行/代码就裸文本贴出来，不要代码块围栏。"
        ),
    },
    {
        "id": "no-echo-no-filler",
        "name": "不复读不敷衍",
        "content": (
            "【硬性】不复述对方的话，不复读上一句，不用「确实」「懂了」「嗯嗯」这类纯敷衍撑字数。\n"
            "【硬性】没有想说的就别说话；要接就接出信息量或情绪。\n"
            "【禁止】同一句话（或同一梗）在一条回复里出现两次以上。"
        ),
    },
    {
        "id": "group-tone",
        "name": "跟群的叫法与梗走",
        "content": (
            "【硬性】称呼按群里实际的叫法来（昵称、外号、大佬/老师这类敬称），别用「该用户」「这位朋友」。\n"
            "【硬性】群里的梗、口头禅、语气词要跟着用，但别硬凹——用不对就不发。\n"
            "【禁止】把别的群的梗搬到当前群当自己的话说。"
        ),
    },
)

BLOCK_HEADER = "【强制注入规则（最高优先级，违反即视为本轮失败）】"
BLOCK_FOOTER = ("以上是主人强制注入的要求，优先级高于你的风格偏好与本轮其他说明；"
                "每条你发出的消息都必须满足它们，做不到就别发那条。")


def preset_ids() -> tuple[str, ...]:
    return tuple(str(item["id"]) for item in PRESETS)


def preset_by_id(injection_id: str) -> dict[str, Any] | None:
    for item in PRESETS:
        if str(item["id"]) == str(injection_id):
            return dict(item)
    return None


def render_block(rows: list[dict[str, Any]]) -> str:
    """把启用的注入渲染成系统提示里的一段（强制框架 + 逐条规则）。"""
    enabled = [row for row in rows if bool(row.get("enabled", True))
               and str(row.get("content", "")).strip()]
    if not enabled:
        return ""
    lines = [BLOCK_HEADER]
    for row in enabled:
        name = str(row.get("name", "") or row.get("injection_id", ""))
        lines.append(f"◆ {name}：")
        lines.append(str(row["content"]).strip())
    lines.append(BLOCK_FOOTER)
    return "\n".join(lines)
