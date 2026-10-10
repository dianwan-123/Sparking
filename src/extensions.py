"""Extension system: capability packs the LLM can browse, toggle, and install.

Two kinds of extensions:

* **Built-in packs** gate the plugin's own llm_tools into named groups
  (browser / web / memory / social / qzone / media / automation / platform).
  Disabling a pack removes its tools from the agent's tool set.
* **Custom extensions** are JSON manifests the LLM (or the WebUI) installs.
  Their tools are *declarative HTTP tools* — method + URL template + headers —
  executed through the stdlib fetch with the same SSRF policy as the browser.
  No arbitrary code is ever executed, so "the LLM installs its own extensions"
  stays safe.

The registry persists custom extensions and enabled/disabled state under
``plugin_data/astrbot_plugin_long_memory_agent/extensions.json``.
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .browser import DEFAULT_TIMEOUT, assert_browsable, parse_html

MAX_CUSTOM = 24
MAX_TEXT = 20000


class ExtensionError(RuntimeError):
    pass


@dataclass(slots=True)
class ExtensionTool:
    """A declarative HTTP tool provided by a custom extension."""

    name: str
    description: str
    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    params: list[str] = field(default_factory=list)

    def render_url(self, args: dict[str, Any]) -> str:
        url = self.url
        for key in self.params:
            value = urllib.parse.quote(str(args.get(key, "")), safe="")
            url = url.replace("{" + key + "}", value)
        return url


@dataclass
class Extension:
    id: str
    name: str
    version: str
    description: str
    builtin: bool
    tools: list[str] = field(default_factory=list)          # builtin packs
    http_tools: list[ExtensionTool] = field(default_factory=list)  # custom
    enabled: bool = True
    installed_at: str = ""
    source: str = ""

    def to_public(self) -> dict[str, Any]:
        data = asdict(self)
        if self.builtin:
            data["http_tools"] = []
        return data


# id -> (name, description, tool names). Tool names must match the
# @filter.llm_tool registrations in main.py.
BUILTIN_PACKS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "memory": (
        "记忆与人物",
        "记忆账本、全文检索、聊天回查、人物印象与好感度",
        (
            "memory_catalog", "search_memory_summaries", "search_chat_history",
            "get_chat_messages", "get_chat_context", "propose_memory_update",
            "memory_edit", "memory_manage", "group_memory",
            "group_topics", "recent_events", "get_user_profile",
            "update_impression", "list_known_users", "get_affinity",
            "adjust_affinity",
        ),
    ),
    "browser": (
        "内置浏览器",
        "浏览网页+真实渲染截图+过人机验证：打开/点链接/填表单/查找/滚动页面、真实浏览器截图、"
        "滑块拖动与坐标点击（应对滑块/CAPTCHA/Cloudflare 等验证）",
        (
            "browse", "browser_links", "browser_click", "browser_form",
            "browser_back", "browser_forward", "browser_tabs", "browser_new_tab",
            "browser_switch_tab", "browser_close_tab", "browser_find",
            "browser_cookies", "browser_screenshot", "browser_dom",
            "browser_click_element", "browser_type_text", "browser_grid_shot",
            "browser_slide", "browser_click_xy", "browser_drag_element",
            "browser_scroll",
        ),
    ),
    "web": (
        "网页搜索",
        "DuckDuckGo 搜索与网页正文抓取",
        ("web_search", "web_fetch"),
    ),
    "origin": (
        "原版兼容",
        "以自定义唤醒词/前缀开头的消息直通 AstrBot 原生命令与插件命令"
        "（前缀在插件配置 origin_wake_prefixes 里改）",
        (),
    ),
    "knowledge": (
        "知识库",
        "检索 AstrBot 知识库——与可查询记忆同级的知识渠道",
        ("kb_list", "kb_search"),
    ),
    "social": (
        "QQ 社交",
        "好友/群列表、发消息、处理申请、戳一戳、改群名片、改自己的资料与头像、下载头像",
        (
            "qq_friend_list", "qq_group_list", "qq_group_members",
            "qq_stranger_info", "qq_recent_history", "qq_send_group",
            "qq_send_private", "qq_handle_friend_request",
            "qq_handle_group_invite", "qq_join_group", "qq_delete_friend",
            "qq_leave_group",
            "qq_set_group_card", "qq_set_group_name", "qq_set_special_title",
            "qq_set_profile", "qq_my_profile", "qq_download_profile",
            "qq_set_status", "qq_set_avatar", "qq_recall",
            "qq_poke", "napcat_catalog", "napcat_call",
        ),
    ),
    "qzone": (
        "QQ 空间",
        "看自己的与好友的动态、给好友说说点赞评论、发说说、删说说",
        ("qzone_list", "qzone_friend_feeds", "qzone_friend_posts",
         "qzone_like_friend", "qzone_comment_friend",
         "qzone_publish", "qzone_like", "qzone_comment", "qzone_delete"),
    ),
    "media": (
        "媒体与表情包",
        "发图、看图、媒体归档、表情包库存与挑选",
        ("send_image", "look_at_image", "read_document", "media_recent",
         "media_fetch_url", "list_stickers", "pick_sticker",
         "forward_messages", "screenshot_messages", "read_forward"),
    ),
    "automation": (
        "自动化",
        "执行 Python 代码、定时任务、心情、任务表、提问",
        ("python_exec", "schedule_task", "list_scheduled", "cancel_scheduled",
         "manage_intent", "set_mood", "todo", "ask_user"),
    ),
    "growth": (
        "技能自学习",
        "把复杂流程固化成自己的技能并热加载；技能不好用时可当回合修补",
        ("learn_skill", "update_skill"),
    ),
    "workspace": (
        "工作区文件",
        "对 AstrBot 数据目录/各插件数据目录/插件 workspace 做文件增删改查与搜索，"
        "并运行工作区内的脚本（.py/.js/.sh/.bat/.ps1）；还可把本地图片/文件/视频/音频直发聊天",
        ("fs_list", "fs_read", "fs_write", "fs_delete", "fs_move", "fs_search",
         "run_script", "send_local_file", "read_tabular"),
    ),
    "ssh": (
        "SSH 远程运维",
        "通过 SSH 操作远程 Ubuntu 机器的 shell（装环境/部署/排查），支持派子agent"
        "自主执行并可中途查看/暂停/追问/改目标/结束；需先在插件配置填 ip/账号/密码",
        ("ssh_exec", "ssh_info", "ssh_agent_dispatch", "ssh_agent_status",
         "ssh_agent_control", "ssh_fetch", "ssh_screenshot"),
    ),
    "designer": (
        "绘图/设计",
        "画图优先 draw_picture（SVG→渲染→截图→发送一条龙，服务器状态图自动带真实指标）；"
        "精细设计用 HTML/SVG 画图并渲染成图片发送；渲染窗口 60 秒内可访问，"
        "项目可保存（标题+简介，加入记忆）供以后修改复用",
        ("draw_picture", "design_render", "design_list", "design_load", "render_code"),
    ),
    "programmer": (
        "程序编写",
        "用 Python+Flask 给自己写网页小程序（小游戏/工具页），单端口多页面、"
        "热重启、数据实时保存；进程到期自动停止，可随时重启或存档",
        ("program_write", "program_list", "program_read",
         "program_view", "program_screenshot", "program_archive"),
    ),
    "scripts": (
        "scripts 拓展",
        "scripts/ 目录下的自定义拓展（工具+提示词）：script_call 调用拓展工具，script_list 列出全部，evolve_prompt 用 GEPA 进化自己的提示词",
        ("script_call", "script_list", "evolve_prompt"),
    ),
    "platform": (
        "AstrBot 平台",
        "运行时能力清单、插件市场搜索、插件/Skill 管理",
        ("astrbot_runtime_inventory", "astrbot_market_search",
         "astrbot_manage_plugin", "astrbot_skill_list", "astrbot_manage_skill"),
    ),
}

_CORE_TOOLS = {"extension_list", "extension_call", "find_tools"}  # never gated


def known_tool_names() -> set[str]:
    """本插件（含内置各包与核心工具）注册的全部工具名——用于区分"自己的工具"
    与"AstrBot/其它插件提供的工具"（能力总表与 find_tools 都要用到）。"""
    names: set[str] = set(_CORE_TOOLS)
    for _name, _desc, tools in BUILTIN_PACKS.values():
        names.update(tools)
    return names


def _valid_id(raw: str) -> str:
    value = re.sub(r"[^a-z0-9_-]", "-", str(raw or "").strip().lower()).strip("-")
    if not value or len(value) > 40:
        raise ExtensionError("拓展 ID 只能是小写字母/数字/中划线（1-40 字）")
    return value


class ExtensionRegistry:
    """Owns built-in packs + installed custom extensions + enable state."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._custom: dict[str, Extension] = {}
        self._disabled: set[str] = set()
        self.load()

    # ------------------------------------------------------------ persistence
    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self._disabled = {str(x) for x in data.get("disabled", [])}
        for item in data.get("custom", []):
            try:
                extension = self._from_manifest(item, installed=True)
                self._custom[extension.id] = extension
            except ExtensionError:
                continue

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "disabled": sorted(self._disabled),
                "custom": [self._manifest_of(x) for x in self._custom.values()],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ built-in
    def builtin_extensions(self) -> list[Extension]:
        result = []
        for pack_id, (name, description, tools) in BUILTIN_PACKS.items():
            result.append(Extension(
                id=pack_id, name=name, version=self.version_of(pack_id),
                description=description, builtin=True, tools=list(tools),
                enabled=pack_id not in self._disabled,
            ))
        return result

    @staticmethod
    def version_of(pack_id: str) -> str:
        # built-in packs ship with the plugin; a stable label is enough
        return "1.0.0"

    # ------------------------------------------------------------ custom
    @staticmethod
    def _manifest_of(extension: Extension) -> dict[str, Any]:
        return {
            "id": extension.id,
            "name": extension.name,
            "version": extension.version,
            "description": extension.description,
            "source": extension.source,
            "installed_at": extension.installed_at,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "method": tool.method,
                    "url": tool.url,
                    "headers": tool.headers,
                    "params": tool.params,
                }
                for tool in extension.http_tools
            ],
        }

    @staticmethod
    def _from_manifest(data: dict[str, Any], *, installed: bool = False) -> Extension:
        if not isinstance(data, dict):
            raise ExtensionError("拓展清单必须是 JSON 对象")
        extension_id = _valid_id(data.get("id", ""))
        name = str(data.get("name", "") or extension_id).strip()[:60]
        tools_raw = data.get("tools")
        if not isinstance(tools_raw, list) or not tools_raw:
            raise ExtensionError(f"拓展 {extension_id} 至少要有一个工具")
        http_tools: list[ExtensionTool] = []
        seen: set[str] = set()
        for item in tools_raw[:12]:
            if not isinstance(item, dict):
                continue
            tool_name = re.sub(r"[^a-z0-9_]", "_", str(item.get("name", "")).lower())
            if not tool_name or tool_name in seen:
                continue
            url = str(item.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                continue
            method = str(item.get("method", "GET") or "GET").upper()
            if method not in {"GET", "POST"}:
                method = "GET"
            headers = {
                str(k): str(v)[:300]
                for k, v in (item.get("headers") or {}).items()
                if isinstance(item.get("headers"), dict)
            } if isinstance(item.get("headers"), dict) else {}
            params = [str(x)[:40] for x in item.get("params", []) if str(x).strip()][:8]
            seen.add(tool_name)
            http_tools.append(ExtensionTool(
                name=tool_name,
                description=str(item.get("description", ""))[:300],
                method=method,
                url=url[:500],
                headers=headers,
                params=params,
            ))
        if not http_tools:
            raise ExtensionError(f"拓展 {extension_id} 没有合法的 HTTP 工具")
        return Extension(
            id=extension_id,
            name=name or extension_id,
            version=str(data.get("version", "1.0"))[:20],
            description=str(data.get("description", ""))[:300],
            builtin=False,
            http_tools=http_tools,
            enabled=True,
            installed_at=str(data.get("installed_at", "")),
            source=str(data.get("source", ""))[:300],
        )

    # ------------------------------------------------------------ queries
    def get(self, extension_id: str) -> Extension | None:
        extension_id = str(extension_id).strip()
        for pack_id, (name, description, tools) in BUILTIN_PACKS.items():
            if pack_id == extension_id:
                return Extension(
                    id=pack_id, name=name, version=self.version_of(pack_id),
                    description=description, builtin=True, tools=list(tools),
                    enabled=pack_id not in self._disabled,
                )
        return self._custom.get(extension_id)

    def list(self) -> list[dict[str, Any]]:
        result = [x.to_public() for x in self.builtin_extensions()]
        result.extend(x.to_public() for x in self._custom.values())
        return result

    def is_enabled(self, extension_id: str) -> bool:
        return str(extension_id) not in self._disabled

    def is_tool_enabled(self, tool_name: str) -> bool:
        if tool_name in _CORE_TOOLS:
            return True
        for pack_id, (_n, _d, tools) in BUILTIN_PACKS.items():
            if tool_name in tools:
                return pack_id not in self._disabled
        return True  # unknown/plugin tools are not gated by packs

    # ------------------------------------------------------------ mutations
    def enable(self, extension_id: str) -> bool:
        extension_id = str(extension_id).strip()
        if self.get(extension_id) is None:
            return False
        self._disabled.discard(extension_id)
        custom = self._custom.get(extension_id)
        if custom is not None:
            custom.enabled = True
        self.save()
        return True

    def disable(self, extension_id: str) -> bool:
        extension_id = str(extension_id).strip()
        if self.get(extension_id) is None or extension_id in _CORE_TOOLS:
            return False
        self._disabled.add(extension_id)
        custom = self._custom.get(extension_id)
        if custom is not None:
            custom.enabled = False
        self.save()
        return True

    def install(self, data: dict[str, Any], source: str = "") -> Extension:
        if len(self._custom) >= MAX_CUSTOM:
            raise ExtensionError(f"自定义拓展最多 {MAX_CUSTOM} 个，先卸载一些")
        extension = self._from_manifest(data)
        extension.source = source[:300]
        from datetime import datetime, timezone

        extension.installed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._custom[extension.id] = extension
        self.save()
        return extension

    def uninstall(self, extension_id: str) -> bool:
        extension_id = str(extension_id).strip()
        if extension_id in self._custom:
            self._custom.pop(extension_id, None)
            self._disabled.discard(extension_id)
            self.save()
            return True
        return False

    def dynamic_tools(self) -> list[tuple[Extension, ExtensionTool]]:
        pairs: list[tuple[Extension, ExtensionTool]] = []
        for extension in self._custom.values():
            if not extension.enabled:
                continue
            for tool in extension.http_tools:
                pairs.append((extension, tool))
        return pairs

    # ------------------------------------------------------------ execution
    def call_sync(self, extension_id: str, tool_name: str,
                  args: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT,
                  max_bytes: int = 1024 * 1024) -> str:
        extension = self._custom.get(str(extension_id).strip())
        if extension is None or not extension.enabled:
            raise ExtensionError(f"拓展 {extension_id} 未安装或已停用")
        tool = next((t for t in extension.http_tools if t.name == str(tool_name)), None)
        if tool is None:
            raise ExtensionError(f"拓展 {extension_id} 没有工具 {tool_name}")
        url = assert_browsable(tool.render_url(dict(args or {})))
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AstrBotAgent)",
                   **tool.headers}
        data = None
        if tool.method == "POST":
            data = urllib.parse.urlencode(
                {k: str(v) for k, v in (args or {}).items()}).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=tool.method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes)
            charset = "utf-8"
            match = re.search(r"charset=([\w\-]+)",
                              str(response.headers.get("Content-Type", "")), re.I)
            if match:
                charset = match.group(1)
            body = raw.decode(charset, "replace")
        mime = str(response.headers.get("Content-Type", "")).split(";")[0]
        if "html" in mime:
            _, text, _, _ = parse_html(body, url)
            return text[:MAX_TEXT]
        return body[:MAX_TEXT]

    async def call(self, extension_id: str, tool_name: str,
                   args: dict[str, Any]) -> str:
        return await asyncio.to_thread(
            self.call_sync, extension_id, tool_name, dict(args or {}))
