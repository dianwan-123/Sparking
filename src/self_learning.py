"""技能自学习闭环（hermes 的 "creates skills from experience, improves them
during use" + openclaw 的 self-learning / Skill Workshop 移植）。

与两家的差异说明：它们用 markdown SKILL.md 文件；本插件用 scripts 拓展形态
（extension.json + 可选 extension.py），因为拓展本来就支持"提示词注入 + 工具
声明 + 热加载"，是同一语义的等价载体。

流程：
1. 一段够重的会话工作结束后（静默期），复盘器带着真实证据找"可复用的流程 /
   被用户纠正过的做法"；
2. 值得固化 → 产出技能提案（JSON）→ 校验（id/长度/代码可编译）→ 写进
   learned root 并热加载 → 下次直接可用；
3. 前台发现技能不好用时，agent 用 update_skill 当回合修补（openclaw 的
   "immediate repair"）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

MAX_SKILL_CODE = 12000
MAX_SKILL_PROMPTS = 8
MAX_PROMPT_CHARS = 800
MAX_SKILL_TOOLS = 4
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")

SKILL_REVIEW_SYSTEM_PROMPT = """
你是自己的经验复盘器。输入是一段刚结束的真实工作记录（对话 + 你的行为痕迹 +
本轮耗时）。任务：从中找出**值得固化的可复用技能**，或判定"没有值得固化的"。

只在满足至少一条时才产出技能：
- 一段经过验证、下次能直接复用的操作流程（省掉未来至少两次来回试探）；
- 用户纠正过你的某个做法（把纠正后的正确做法记下来，避免再犯）；
- 某个反复出现的请求模式（如"读试卷给答案""整理合并转发"）的标准处理步骤。

产出格式（严格 JSON，不要 markdown 围栏）：
没有值得固化的：{"skill": null, "reason": "一句话原因"}
值得固化：
{"skill": {
  "id": "小写英文/数字/中划线短名，如 exam-paper-answers",
  "display_name": "中文技能名",
  "description": "一句话：做什么 + 什么时候用（≤200字）",
  "prompts": ["写给未来的自己的操作说明，会注入你的上下文。一条一个要点。"],
  "tools": [{"name": "工具名", "description": "说明", "params": {"参数名": "说明"}}],
  "code": "可选：Python 代码。若要实现工具，必须提供 async def call_tool(api, name, params) 或 def call_tool(...)，用注入的 api 调宿主能力",
  "permissions": ["llm", "memory", "net", "send", "program", "media", "ssh 中实际需要的"]
}, "reason": "为什么值得固化"}

硬性规则：
- 一个技能只做一件事；id 不许与已有技能重复（要改进已有技能就返回同 id，会覆盖更新）。
- prompts 每条 ≤800 字、最多 8 条；code ≤12000 字符且必须是完整可运行的 Python。
- api 能力（与 docs/SCRIPT_EXT_API.md 契约一致，只能用这些）：
  api.log(msg)、api.data_dir()、api.kv_get(k)/api.kv_set(k,v)、api.now()、
  api.llm(prompt, system_prompt="", provider_id="")、api.memory_note(text)、
  api.http_get(url)/api.http_post_json(url, payload)/api.http_get_bytes(url)（原始字节下载）、
  api.ssh_exec(command, timeout=60)（在主人配置好的远程服务器上执行 shell，返回
  {exit_code, stdout, stderr}）、api.media_from_remote(remote_path, note="")（把服务器上
  的文件/截图拉进媒体库，返回 media_id）、api.save_image(png, note)（存 PNG 出 media_id）、
  api.run_program(program_id, code, title, description)（部署/热重启 Flask 小程序）、
  api.qq_send_group(group_id, text)/api.qq_send_private(user_id, text)。
  （发媒体：让主 agent 用 media_id 调 send_image，或用 qq_send_group 发文字。）
  代码里禁止 import 插件内部模块（插件升级会断）；标准库随意。
- permissions 只填实际需要的：llm / memory / net / send / program / media / ssh。
- 不许编造：只固化这段记录里真实做过、可见有效的东西。
- 没有值得固化的就老实返回 null，不要为了产出而产出。
""".strip()


def normalize_skill(proposal: Mapping[str, Any]) -> dict[str, Any] | None:
    """校验并规范化技能提案；不合法返回 None。"""
    if not isinstance(proposal, Mapping):
        return None
    skill_id = str(proposal.get("id") or "").strip().lower()
    if not _ID_RE.match(skill_id):
        return None
    description = str(proposal.get("description") or "").strip()[:300]
    if not description:
        return None
    prompts_raw = proposal.get("prompts") or []
    if isinstance(prompts_raw, str):
        prompts_raw = [prompts_raw]
    prompts = [
        str(item).strip()[:MAX_PROMPT_CHARS]
        for item in prompts_raw if str(item).strip()
    ][:MAX_SKILL_PROMPTS]
    tools: list[dict[str, Any]] = []
    for tool in (proposal.get("tools") or [])[:MAX_SKILL_TOOLS]:
        if not isinstance(tool, Mapping):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        tools.append({
            "name": name[:60],
            "description": str(tool.get("description") or "")[:300],
            "params": tool.get("params") if isinstance(tool.get("params"), dict) else {},
        })
    code = str(proposal.get("code") or "")
    if len(code) > MAX_SKILL_CODE:
        return None
    if code.strip():
        try:
            compile(code, "<learned-skill>", "exec")
        except SyntaxError:
            return None
        if tools and "def call_tool" not in code:
            return None
    permissions = [
        str(p) for p in (proposal.get("permissions") or [])
        if str(p) in {"llm", "memory", "net", "send", "program", "media", "ssh"}
    ]
    return {
        "id": skill_id,
        "display_name": str(proposal.get("display_name") or skill_id)[:80],
        "description": description,
        "prompts": prompts,
        "tools": tools,
        "code": code,
        "permissions": permissions,
    }


def parse_skill_proposal(raw: Any) -> dict[str, Any] | None:
    """复盘器输出 → 规范化技能提案（无需固化时返回 None）。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1]) if "{" in text else None
    except (json.JSONDecodeError, ValueError):
        data = None
    if not isinstance(data, Mapping):
        return None
    return normalize_skill(data.get("skill") or {})


def build_evidence(
    messages: Sequence[Any],
    *,
    elapsed_seconds: float,
    known_skills: Sequence[str] = (),
    max_chars: int = 6000,
) -> str:
    """把一轮工作整理成复盘证据文本（messages 为 StoredMessage 列表）。"""
    lines = [f"[本轮工作耗时约 {int(elapsed_seconds)} 秒]"]
    if known_skills:
        lines.append("你已有的自学习技能：" + "、".join(str(x) for x in known_skills[:30]))
    lines.append("—— 工作记录（旧→新）——")
    for item in messages[-40:]:
        sender = str(getattr(item, "sender_name", "") or getattr(item, "sender_id", "") or "?")
        text = str(getattr(item, "text", "") or "").replace("\n", " ").strip()
        if text:
            lines.append(f"{sender}: {text[:220]}")
    return "\n".join(lines)[:max_chars]


async def apply_skill(manager: Any, proposal: Mapping[str, Any]) -> dict[str, Any]:
    """把规范化后的技能写进 learned root 并热加载（manager 为 ScriptExtensionManager）。"""
    manifest = {
        "display_name": proposal.get("display_name") or proposal["id"],
        "description": proposal.get("description", ""),
        "version": "1.0.0",
        "prompts": list(proposal.get("prompts") or []),
        "tools": list(proposal.get("tools") or []),
        "permissions": list(proposal.get("permissions") or []),
    }
    return await manager.create_extension(
        str(proposal["id"]), manifest, str(proposal.get("code") or ""))
