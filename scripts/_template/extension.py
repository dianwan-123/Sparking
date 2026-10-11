# -*- coding: utf-8 -*-
"""demo-template 示例工具实现。

这个文件展示一个拓展的最小骨架：实现 call_tool(api, name, params)，
按工具名分发，返回**给模型看的文本结果**。发消息要用 api 的发送能力，别在这里发。
"""
from __future__ import annotations

import json


async def call_tool(api, name: str, params: dict) -> str:
    if name == "template_hello":
        target = str((params or {}).get("name") or "").strip()
        # 资源文件示例：词表/模板放在拓展目录里，运行时按需读取
        try:
            lines = [line.strip() for line in
                     api.read_resource("data/greetings.txt").splitlines() if line.strip()]
        except Exception:
            lines = ["你好，{name}！"]          # 资源缺失时的兜底
        template = lines[0] if lines else "你好，{name}！"
        return json.dumps({
            "ok": True,
            "text": template.replace("{name}", target or "大家"),
            "hint": "把 text 作为你回复的一部分说出来即可",
        }, ensure_ascii=False)
    return f"demo-template 没有叫 {name} 的工具"
