# -*- coding: utf-8 -*-
"""QQ 消息卡片渲染器（伪截图）。

用户要求：不是对 QQ 真截图，而是把「用户头像 + 消息内容」做成聊天卡片形式的
图片——要处理好图文混排与合并转发的情况。纯函数构建 HTML，交给
``_design_screenshot``（fresh page 渲染）出图；头像直接用 QQ 官方头像接口，
图片段用消息里自带的 CDN url。
"""
from __future__ import annotations

import html
import hashlib
from typing import Any

_CARD_BG = "#eef1f6"
_BUBBLE_BG = "#ffffff"
_TIME_COLOR = "#9aa3ae"
_PALETTE = ("#e0616c", "#d98a2b", "#5a9e31", "#3d9bc7", "#7a6bd6",
            "#c8549b", "#2ea08e", "#b87b3a", "#6d7b8d", "#cc5f3d")


def avatar_url(uin: str) -> str:
    """QQ 官方头像（不存在/非数字时会拿到默认灰头，不会挂渲染）。"""
    return f"https://q1.qlogo.cn/g?b=qq&nk={uin or 10000}&s=100"


def _name_color(name: str) -> str:
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    return _PALETTE[int(digest, 16) % len(_PALETTE)]


def _escape(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def _inline_segment(seg: dict[str, Any]) -> str:
    """一段 OneBot 段 → 行内 HTML。未识别的段渲染成占位 chip，不炸。"""
    data = seg.get("data") or {}
    kind = str(seg.get("type") or "")
    if kind == "text":
        return _escape(data.get("text"))
    if kind == "at":
        who = str(data.get("name") or data.get("qq") or data.get("uid") or "?")
        return f'<span class="at">@{_escape(who)}</span>'
    if kind == "face":
        return f'<span class="chip">[表情]</span>'
    if kind == "image":
        url = str(data.get("url") or data.get("file") or "")
        if url.startswith(("http://", "https://")):
            return (f'<img class="msg-img" src="{_escape(url)}" '
                    'onerror="this.replaceWith(Object.assign(document.createElement(\'span\'),'
                    '{className:\'chip\',textContent:\'[图片加载失败]\'}))">')
        return '<span class="chip">[图片]</span>'
    if kind == "forward":
        return '<span class="chip forward">『合并转发』</span>'
    if kind == "reply":
        # 引用头由消息级处理；行内出现时只给占位
        return '<span class="chip">[回复消息]</span>'
    if kind == "record":
        return '<span class="chip">[语音]</span>'
    if kind == "video":
        return '<span class="chip">[视频]</span>'
    if kind == "file":
        return f'<span class="chip">[文件:{_escape(data.get("name") or data.get("file_id") or "?")}]</span>'
    if kind in ("json", "xml"):
        return '<span class="chip">[卡片消息]</span>'
    if kind == "node":
        # 合并转发节点（发送侧构造）——卡片里按子消息渲染由调用方展开，这里兜底
        return '<span class="chip">『转发消息』</span>'
    return f'<span class="chip">[{_escape(kind or "未知")}]</span>'


def _render_message(item: dict[str, Any]) -> str:
    name = str(item.get("name") or item.get("uin") or "群友")
    uin = str(item.get("uin") or "")
    when = str(item.get("time") or "").strip()
    segments = item.get("segments")
    if not segments and item.get("text"):
        segments = [{"type": "text", "data": {"text": str(item["text"])}}]
    body = "".join(_inline_segment(seg) for seg in (segments or [])) or "&nbsp;"
    reply = ""
    if item.get("reply_text"):
        reply = (f'<div class="quote">回复 {_escape(item.get("reply_name") or "")}'
                 f'：{_escape(item["reply_text"])}</div>')
    time_html = f'<span class="time">{_escape(when)}</span>' if when else ""
    return (
        f'<div class="msg">'
        f'<img class="avatar" src="{avatar_url(uin)}" alt="">'
        f'<div class="col">'
        f'<div class="name" style="color:{_name_color(name)}">{_escape(name)}{time_html}</div>'
        f'<div class="bubble">{reply}{body}</div>'
        f'</div></div>'
    )


def build_card_html(messages: list[dict[str, Any]], *, title: str = "聊天记录",
                    footer: str = "") -> str:
    """把一批消息渲染成 QQ 风格聊天卡片（整页 HTML，交给截图管线）。

    ``messages`` 每项：``{uin, name, time, segments|text, reply_text?, reply_name?}``。
    """
    rows = "".join(_render_message(item) for item in messages)
    footer_html = f'<div class="footer">{_escape(footer)}</div>' if footer else ""
    count = len(messages)
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        f"html,body{{margin:0;padding:0;background:{_CARD_BG};}}"
        ".card{width:520px;padding:14px 16px 10px;font-family:'Microsoft YaHei',"
        "'PingFang SC','Noto Sans CJK SC',sans-serif;}"
        ".title{display:flex;justify-content:center;margin-bottom:10px;}"
        ".title span{background:rgba(0,0,0,.28);color:#fff;font-size:13px;"
        "padding:3px 12px;border-radius:12px;}"
        f".msg{{display:flex;margin:10px 0;gap:10px;}}"
        ".avatar{width:40px;height:40px;border-radius:6px;flex:none;"
        "background:#d8dce3;object-fit:cover;}"
        ".col{max-width:400px;}"
        ".name{font-size:12px;line-height:18px;margin-bottom:3px;}"
        f".time{{color:{_TIME_COLOR};font-size:11px;margin-left:8px;}}"
        f".bubble{{background:{_BUBBLE_BG};border-radius:4px 14px 14px 14px;"
        "padding:8px 12px;font-size:15px;line-height:1.55;word-break:break-word;"
        "box-shadow:0 1px 1px rgba(0,0,0,.05);}}"
        ".bubble::after{content:'';display:block;clear:both;}"
        ".msg-img{display:block;max-width:260px;max-height:190px;border-radius:6px;"
        "margin:4px 0;}"
        ".at{color:#3d9bc7;}"
        f".chip{{background:#f0f1f4;color:#7d8590;border-radius:4px;padding:1px 6px;"
        "font-size:13px;}}"
        ".chip.forward{background:#e7f0fb;color:#4a90d9;font-weight:bold;}"
        ".quote{background:#f5f6f8;border-left:3px solid #c9ced6;color:#8a92a0;"
        "font-size:12px;padding:4px 8px;border-radius:4px;margin-bottom:5px;"
        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}"
        f".footer{{text-align:center;color:{_TIME_COLOR};font-size:11px;margin-top:10px;}}"
        "</style></head><body>"
        # data-shot-fit：让截图管线按这个盒子的真实尺寸定画布（否则固定宽视口
        # 会把卡片塞在左上角、其余全白——用户截图里的"消息只占一点点"）
        "<div class='card' data-shot-fit>"
        f"<div class='title'><span>{_escape(title)}（{count}条）</span></div>"
        + rows + footer_html +
        "</div></body></html>"
    )
