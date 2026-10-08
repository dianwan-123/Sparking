"""scripts/ 万能拓展接口（Script Extensions）。

设计目标：**长期兼容**。用户（或外部 agent）在插件目录 ``scripts/<id>/`` 放一个
文件夹，即可给 bot 提供工具、附加提示词与消息钩子；插件升级不需要拓展跟着改。

兼容性三原则（改动本文件时必须遵守）：
1. 拓展**永远不 import 插件内部**——所有能力通过调用方注入的
   :class:`ScriptExtensionAPI` 对象获得；API 方法只增不删不改签名，
   ``api_version`` 只升不降，老拓展在未来的插件里依旧可加载。
2. manifest（extension.json）只做**宽松校验**：未知字段忽略、缺省字段给默认、
   不因多余内容报错。
3. 每个拓展的每个调用都**异常隔离**：单个拓展坏了只影响它自己，
   绝不波及插件主流程。

目录约定：
``scripts/<id>/``
    ``extension.json``  manifest（必须）：name/api_version 必填，tools/prompts 可选
    ``extension.py``    hooks 模块（tools 非空时必须提供 ``call_tool``）
    其余文件随意（资源、数据模板等），会随插件一起打包

hooks 模块约定（全部可选，同步/异步函数均可）：
    ``async def on_load(api)``        加载后调用一次
    ``async def on_unload(api)``      卸载/重载前调用
    ``async def call_tool(api, name, params) -> str``   工具执行入口（必配工具时必须）
    ``def get_prompts(api) -> str|list``   动态提示词（与 manifest prompts 合并）
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

SCRIPT_API_VERSION = 1
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def _slug(value: str) -> str:
    """把任意名字清洗成合法拓展 id（小写字母/数字/中划线/下划线，2-48 位）。"""
    text = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text or len(text) < 2 or not _ID_RE.match(text):
        return ""
    return text[:48].rstrip("-")
_KNOWN_PERMISSIONS = frozenset({"llm", "memory", "net", "send", "program", "media", "ssh"})
_OPEN_TIMEOUT = 20


class ScriptExtensionError(RuntimeError):
    pass


# ---------------------------------------------------------------- API 对象


class ScriptExtensionAPI:
    """交给拓展的唯一能力入口。**只增不删**：未来加能力只能新增方法，
    绝不修改/移除已有方法与签名——这是长期兼容的根基。"""

    def __init__(self, manager: "ScriptExtensionManager", ext_id: str,
                 permissions: frozenset[str]) -> None:
        self._manager = manager
        self.name = ext_id
        self._permissions = permissions

    # ---- 基础 ----
    @property
    def data_dir(self) -> Path:
        """本拓展的持久化数据目录（随插件数据目录，重装/升级不清空）。"""
        return self._manager.data_dir(self.name)

    def log(self, message: Any) -> None:
        self._manager.log(self.name, message)

    def config(self) -> dict[str, Any]:
        """读取本拓展的配置（``data_dir/config.json`` 覆盖 ``config.default.json``）。

        每个拓展可以在自己的文件夹里放 ``config.default.json``（默认值模板，随插件走），
        用户的改动写到数据目录的 ``config.json``（升级不丢）。**每次调用都重读**，
        所以控制台改完立刻生效，不用重载拓展。
        """
        return self._manager.effective_config(self.name)

    def config_schema(self) -> dict[str, Any]:
        """配置模板（``config.default.json``），控制台据此渲染表单。"""
        return self._manager.config_template(self.name)

    def get_config(self, key: str = "", default: Any = None) -> Any:
        """取一个配置项：``get_config()`` 给整份，``get_config("api_key")`` 给单项。"""
        data = self.config()
        if not str(key or "").strip():
            return data
        return data.get(str(key), default)

    def set_config(self, key: str, value: Any) -> None:
        """改一个配置项（写进用户配置文件，立即对后续调用生效）。"""
        self._manager.set_config_value(self.name, str(key), value)

    def now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    # ---- 轻量持久化（kv.json）----
    def _kv_path(self) -> Path:
        return self.data_dir / "kv.json"

    def kv_get(self, key: str, default: Any = None) -> Any:
        try:
            data = json.loads(self._kv_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        return data.get(str(key), default) if isinstance(data, dict) else default

    def kv_set(self, key: str, value: Any) -> None:
        try:
            data = json.loads(self._kv_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        data[str(key)] = value
        self._kv_path().parent.mkdir(parents=True, exist_ok=True)
        self._kv_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 能力（受 permissions 约束）----
    def _require(self, permission: str) -> None:
        if permission not in self._permissions:
            raise ScriptExtensionError(
                f"拓展 {self.name} 未在 extension.json 声明权限 \"{permission}\"")

    async def llm(self, prompt: str, system_prompt: str = "",
                  provider_id: str = "") -> str:
        """调用 bot 配置的聊天模型。"""
        self._require("llm")
        return await self._manager.llm(str(prompt), str(system_prompt or ""),
                                       str(provider_id or ""))

    async def memory_note(self, text: str) -> None:
        """写一条记录进 bot 的可查询记忆（notice.script 事件，不占主线上下文）。"""
        self._require("memory")
        await self._manager.memory_note(f"[{self.name}] {str(text)[:600]}")

    @staticmethod
    async def http_get(url: str, timeout: int = _OPEN_TIMEOUT) -> tuple[int, str]:
        """GET 一个 http(s) 地址，返回 (status, text)。"""
        def _fetch() -> tuple[int, str]:
            request = urllib.request.Request(
                str(url), headers={"User-Agent": "Mozilla/5.0 longmem-script"})
            with urllib.request.urlopen(request, timeout=int(timeout)) as response:
                return response.status, response.read().decode("utf-8", "replace")
        return await asyncio.to_thread(_fetch)

    @staticmethod
    async def http_post_json(url: str, payload: dict[str, Any],
                             timeout: int = _OPEN_TIMEOUT) -> tuple[int, str]:
        """POST JSON 到一个 http(s) 地址（给外部 agent/webhook 开分支用）。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def _fetch() -> tuple[int, str]:
            request = urllib.request.Request(
                str(url), data=data, method="POST",
                headers={"Content-Type": "application/json; charset=utf-8",
                         "User-Agent": "Mozilla/5.0 longmem-script"})
            with urllib.request.urlopen(request, timeout=int(timeout)) as response:
                return response.status, response.read().decode("utf-8", "replace")
        return await asyncio.to_thread(_fetch)

    @staticmethod
    async def http_get_bytes(url: str, timeout: int = _OPEN_TIMEOUT) -> tuple[int, bytes]:
        """GET 一个 http(s) 地址，返回 (status, 原始字节)——下载图片/文件用。"""
        def _fetch() -> tuple[int, bytes]:
            request = urllib.request.Request(
                str(url), headers={"User-Agent": "Mozilla/5.0 longmem-script"})
            with urllib.request.urlopen(request, timeout=int(timeout)) as response:
                return response.status, response.read()
        return await asyncio.to_thread(_fetch)

    async def run_program(self, program_id: str, code: str,
                          title: str = "", description: str = "") -> dict[str, Any]:
        """启动/热重启一个 Flask 小程序（studio 单端口宿主上，/p/<id>/ 可访问）。
        返回 {"ok": bool, "url": ..., "data_dir": ...}。代码与数据由宿主持久化。"""
        self._require("program")
        return await self._manager.run_program(program_id, str(code), str(title or ""),
                                               str(description or ""))

    async def save_image(self, png: bytes, note: str = "") -> str:
        """把一张图片（PNG 字节）存进媒体归档，返回 media_id（send_image 可发）。"""
        self._require("media")
        return str(await self._manager.save_image(bytes(png), str(note or "")))

    async def ssh_exec(self, command: str, timeout: int = 60) -> dict[str, Any]:
        """在配置好的远程服务器上执行一条 shell 命令（与插件的 SSH 配置共用），
        返回 {"exit_code", "stdout", "stderr", "truncated"}——把"服务器运维"流程
        封装成拓展用（如装包、跑脚本、远程截图）。"""
        self._require("ssh")
        return await self._manager.ssh_exec(str(command), int(timeout))

    async def media_from_remote(self, remote_path: str, note: str = "") -> str:
        """把远程服务器上的文件拉进媒体归档，返回 media_id（send_image 可发）。
        这是"把服务器上的图/文件发到 QQ"的专用能力（ssh_exec 的输出传不了文件）。"""
        self._require("ssh")
        self._require("media")
        return str(await self._manager.media_from_remote(
            str(remote_path), str(note or "")))

    async def qq_send_group(self, group_id: str, text: str) -> None:
        """发 QQ 群消息（需 permissions 含 "send"；只允许白名单群）。"""
        self._require("send")
        await self._manager.qq_send_group(str(group_id), str(text))

    async def qq_send_private(self, user_id: str, text: str) -> None:
        """发 QQ 私聊消息（需 permissions 含 "send"）。"""
        self._require("send")
        await self._manager.qq_send_private(str(user_id), str(text))


# ---------------------------------------------------------------- 加载器


def validate_manifest(raw: Any, source: Path) -> dict[str, Any]:
    """宽松校验：只要求 name/api_version，其余一切缺省或忽略。"""
    if not isinstance(raw, dict):
        raise ScriptExtensionError(f"{source}: manifest 必须是 JSON 对象")
    name = str(raw.get("name") or "").strip()
    api_version = raw.get("api_version", 1)
    try:
        api_version = int(api_version)
    except (TypeError, ValueError) as error:
        raise ScriptExtensionError(f"{source}: api_version 必须是整数") from error
    if not _ID_RE.match(name):
        raise ScriptExtensionError(
            f"{source}: name 只能是小写字母/数字/中划线（{name!r}）")
    if api_version > SCRIPT_API_VERSION:
        raise ScriptExtensionError(
            f"{source}: 需要 API v{api_version}，当前插件只支持到 v{SCRIPT_API_VERSION}"
            "（拓展比插件新，请升级插件或降低拓展的 api_version）")
    tools = raw.get("tools")
    if tools is None:
        tools = []
    if not isinstance(tools, list):
        raise ScriptExtensionError(f"{source}: tools 必须是数组")
    clean_tools: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_name = str(tool.get("name") or "").strip()
        if not tool_name:
            continue
        clean_tools.append({
            "name": tool_name,
            "description": str(tool.get("description") or "")[:300],
            "params": tool.get("params") if isinstance(tool.get("params"), dict) else {},
        })
    prompts = raw.get("prompts")
    if isinstance(prompts, str):
        prompts = [prompts]
    if not isinstance(prompts, list):
        prompts = []
    permissions = frozenset(
        str(p) for p in (raw.get("permissions") or [])
        if str(p) in _KNOWN_PERMISSIONS)
    return {
        "name": name,
        "api_version": api_version,
        "display_name": str(raw.get("display_name") or name),
        "version": str(raw.get("version") or "0.0.0"),
        "description": str(raw.get("description") or "")[:300],
        "author": str(raw.get("author") or ""),
        "tools": clean_tools,
        "prompts": [str(x) for x in prompts if str(x).strip()],
        "permissions": permissions,
        # 默认是否启用：拓展可以声明 default_enabled=false（内置实验性拓展用），
        # 用户随时能在控制台打开；用户的选择优先于这里的默认值。
        "default_enabled": bool(raw.get("default_enabled", True)),
        # 未知字段一律忽略（向前兼容：老插件读新 manifest 不炸）
    }


def _call_maybe_async(value: Any) -> Any:
    return asyncio.ensure_future(value) if inspect_isawaitable(value) else value


def inspect_isawaitable(value: Any) -> bool:
    import inspect

    return inspect.isawaitable(value)


class ScriptExtension:
    """一个已加载的 scripts 拓展实例。"""

    def __init__(self, folder: Path, manager: "ScriptExtensionManager") -> None:
        self.folder = folder
        self._manager = manager
        self.id = folder.name
        self.origin = "builtin"
        self.manifest: dict[str, Any] = {}
        self.module: Any = None
        self.error: str = ""
        self.loaded_at: str = ""

    async def load(self) -> None:
        manifest_path = self.folder / "extension.json"
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            self.error = f"缺少 extension.json：{manifest_path.name}"
            return
        except (OSError, json.JSONDecodeError) as error:
            self.error = f"extension.json 读取失败：{type(error).__name__}: {error}"[:200]
            return
        try:
            self.manifest = validate_manifest(raw, manifest_path)
        except ScriptExtensionError as error:
            self.error = str(error)[:220]
            return
        self.id = self.manifest["name"]
        module_path = self.folder / "extension.py"
        if self.manifest["tools"] or module_path.exists():
            self.module = self._import_module(module_path)
            if self.module is None:
                return
        api = self._manager.api_for(self)
        await self._run_hook("on_load", api)
        self.loaded_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.error = ""

    def _import_module(self, module_path: Path) -> Any:
        """以独立模块身份加载 extension.py——拓展永远不 import 插件内部，
        插件重载/升级也不受旧模块缓存影响。"""
        module_name = f"longmem_script_{self.id}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            if spec is None or spec.loader is None:
                self.error = f"extension.py 无法加载：{module_path}"
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            return module
        except Exception as error:  # 语法错误/顶层异常：只毁掉这个拓展
            self.error = f"extension.py 加载失败：{type(error).__name__}: {error}"[:220]
            sys.modules.pop(module_name, None)
            return None

    async def unload(self) -> None:
        if self.module is None:
            return
        try:
            api = self._manager.api_for(self)
            await self._run_hook("on_unload", api)
        except Exception as error:
            self._manager.log(self.id, f"on_unload 异常（忽略）：{error}")
        sys.modules.pop(f"longmem_script_{self.id}", None)
        self.module = None

    async def _run_hook(self, hook: str, api: ScriptExtensionAPI) -> Any:
        func = getattr(self.module, hook, None) if self.module else None
        if not callable(func):
            return None
        result = func(api)
        if inspect_isawaitable(result):
            return await result
        return result

    async def call_tool(self, name: str, params: dict[str, Any]) -> str:
        if self.module is None:
            raise ScriptExtensionError(
                f"拓展 {self.id} 没有可用的 extension.py（{self.error or '未加载'}）")
        func = getattr(self.module, "call_tool", None)
        if not callable(func):
            raise ScriptExtensionError(
                f"拓展 {self.id} 声明了工具但没有实现 call_tool(api, name, params)")
        result = func(self._manager.api_for(self), str(name), dict(params or {}))
        if inspect_isawaitable(result):
            result = await result
        return str(result)

    def prompts(self) -> list[str]:
        """manifest prompts + 模块 get_prompts() 合并。"""
        result = [str(x) for x in self.manifest.get("prompts", []) if str(x).strip()]
        getter = getattr(self.module, "get_prompts", None) if self.module else None
        if callable(getter):
            try:
                value = getter(self._manager.api_for(self))
                if isinstance(value, str):
                    result.append(value)
                elif isinstance(value, list):
                    result.extend(str(x) for x in value if str(x).strip())
            except Exception as error:
                self._manager.log(self.id, f"get_prompts 异常（忽略）：{error}")
        return result

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "origin": self.origin,
            "display_name": self.manifest.get("display_name", self.id),
            "version": self.manifest.get("version", ""),
            "api_version": self.manifest.get("api_version", 1),
            "description": self.manifest.get("description", ""),
            "author": self.manifest.get("author", ""),
            "permissions": sorted(self.manifest.get("permissions", frozenset())),
            "tools": self.manifest.get("tools", []),
            "error": self.error,
            "loaded_at": self.loaded_at,
            "config": self._manager.effective_config(self.id),
            "config_template": self._manager.config_template(self.id),
            "enabled": self._manager.is_enabled(self.id),
            "default_enabled": bool(self.manifest.get("default_enabled", True)),
        }


class ScriptExtensionManager:
    """加载/重载/枚举 scripts/ 下的拓展；宿主能力由回调注入（解耦 main.py）。"""

    def __init__(
        self,
        scripts_root: str | Path,
        data_root: str | Path,
        *,
        learned_root: str | Path | None = None,
        llm: Callable[..., Awaitable[str]] | None = None,
        memory_note: Callable[[str], Awaitable[None]] | None = None,
        qq_send_group: Callable[[str, str], Awaitable[None]] | None = None,
        qq_send_private: Callable[[str, str], Awaitable[None]] | None = None,
        run_program: Callable[[str, str, str, str], Awaitable[dict]] | None = None,
        save_image: Callable[[bytes, str], Awaitable[str]] | None = None,
        ssh_exec: Callable[..., Awaitable[dict]] | None = None,
        media_from_remote: Callable[[str, str], Awaitable[str]] | None = None,
        logger: Callable[[str], Any] | None = None,
    ) -> None:
        self.scripts_root = Path(scripts_root)
        self.data_root = Path(data_root)
        # 自学习技能的独立加载根（插件数据目录）——插件升级重装不会丢，
        # 也不与手写 scripts/ 混淆：builtin 优先，重名时 learned 被跳过。
        self.learned_root = Path(learned_root) if learned_root else None
        self._llm = llm
        self._memory_note = memory_note
        self._qq_send_group = qq_send_group
        self._qq_send_private = qq_send_private
        self._run_program = run_program
        self._save_image = save_image
        self._ssh_exec = ssh_exec
        self._media_from_remote = media_from_remote
        self._logger = logger or (lambda message: None)
        self.extensions: dict[str, ScriptExtension] = {}

    # ---- 注入给 API 的宿主能力 ----
    def log(self, ext_id: str, message: Any) -> None:
        self._logger(f"[拓展:{ext_id}] {str(message)[:300]}")

    def data_dir(self, ext_id: str) -> Path:
        path = self.data_root / ext_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---- 启用状态（拓展可以声明默认关闭；用户的选择优先并落盘） ----
    def _state_path(self) -> Path:
        self.data_root.mkdir(parents=True, exist_ok=True)
        return self.data_root / "_extensions.json"

    def _state(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def is_enabled(self, ext_id: str) -> bool:
        """用户改过就用用户的选择；没改过看拓展自己声明的 default_enabled。"""
        ext = self.get(ext_id)
        if ext is None:
            return False
        overrides = self._state().get("enabled")
        if isinstance(overrides, dict) and ext_id in overrides:
            return bool(overrides[ext_id])
        return bool(ext.manifest.get("default_enabled", True))

    def set_enabled(self, ext_id: str, enabled: bool) -> bool:
        if self.get(ext_id) is None:
            raise ScriptExtensionError(f"没有叫 {ext_id} 的拓展")
        state = self._state()
        overrides = state.get("enabled")
        if not isinstance(overrides, dict):
            overrides = {}
        overrides[ext_id] = bool(enabled)
        state["enabled"] = overrides
        try:
            self._state_path().write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            raise ScriptExtensionError(f"写入拓展状态失败：{error}") from error
        self.log(ext_id, "已启用" if enabled else "已关闭")
        return bool(enabled)

    # ---- 拓展配置（模板随拓展走，用户改动落在数据目录） ----
    def config_path(self, ext_id: str) -> Path:
        return self.data_dir(ext_id) / "config.json"

    def config_template(self, ext_id: str) -> dict[str, Any]:
        """拓展自带的默认配置（``<拓展目录>/config.default.json``）。"""
        ext = self.get(ext_id)
        if ext is None:
            return {}
        path = Path(ext.folder) / "config.default.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def effective_config(self, ext_id: str) -> dict[str, Any]:
        """模板打底 + 用户改动覆盖（用户没写过的项用默认值）。"""
        data = dict(self.config_template(ext_id))
        try:
            raw = json.loads(self.config_path(ext_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            data.update(raw)
        return data

    def save_config(self, ext_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """整份写入用户配置（控制台用）。未知键也保留——拓展可能读自定义字段。"""
        if self.get(ext_id) is None:
            raise ScriptExtensionError(f"没有叫 {ext_id} 的拓展")
        clean = {str(k): v for k, v in dict(values or {}).items() if str(k).strip()}
        path = self.config_path(ext_id)
        try:
            path.write_text(json.dumps(clean, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError as error:
            raise ScriptExtensionError(f"写入配置失败：{error}") from error
        self.log(ext_id, f"配置已更新（{len(clean)} 项）")
        return self.effective_config(ext_id)

    def set_config_value(self, ext_id: str, key: str, value: Any) -> dict[str, Any]:
        current = self.effective_config(ext_id)
        current[str(key)] = value
        return self.save_config(ext_id, current)

    async def llm(self, prompt: str, system_prompt: str, provider_id: str) -> str:
        if self._llm is None:
            raise ScriptExtensionError("宿主未提供 LLM 能力")
        return await self._llm(prompt, system_prompt, provider_id)

    async def memory_note(self, text: str) -> None:
        if self._memory_note is None:
            raise ScriptExtensionError("宿主未提供记忆能力")
        await self._memory_note(text)

    async def qq_send_group(self, group_id: str, text: str) -> None:
        if self._qq_send_group is None:
            raise ScriptExtensionError("宿主未提供 QQ 发送能力")
        await self._qq_send_group(group_id, text)

    async def qq_send_private(self, user_id: str, text: str) -> None:
        if self._qq_send_private is None:
            raise ScriptExtensionError("宿主未提供 QQ 发送能力")
        await self._qq_send_private(user_id, text)

    async def run_program(self, program_id: str, code: str,
                          title: str, description: str) -> dict[str, Any]:
        if self._run_program is None:
            raise ScriptExtensionError("宿主未提供 program 运行能力")
        return await self._run_program(program_id, code, title, description)

    async def save_image(self, png: bytes, note: str) -> str:
        if self._save_image is None:
            raise ScriptExtensionError("宿主未提供媒体归档能力")
        return await self._save_image(png, note)

    async def ssh_exec(self, command: str, timeout: int = 60) -> dict[str, Any]:
        if self._ssh_exec is None:
            raise ScriptExtensionError("宿主未提供 SSH 能力（先配置 SSH）")
        return await self._ssh_exec(command, timeout)

    async def media_from_remote(self, remote_path: str, note: str = "") -> str:
        if self._media_from_remote is None:
            raise ScriptExtensionError("宿主未提供远程文件回传能力（先配置 SSH）")
        return await self._media_from_remote(remote_path, note)

    def api_for(self, ext: ScriptExtension) -> ScriptExtensionAPI:
        return ScriptExtensionAPI(
            self, ext.id, frozenset(ext.manifest.get("permissions", frozenset())))

    # ---- 生命周期 ----
    def _roots(self) -> list[tuple[Path, str]]:
        roots: list[tuple[Path, str]] = [(self.scripts_root, "builtin")]
        if self.learned_root is not None:
            roots.append((self.learned_root, "learned"))
        return roots

    async def load_all(self) -> list[str]:
        problems: list[str] = []
        for ext in list(self.extensions.values()):
            await ext.unload()
        self.extensions.clear()
        for root, origin in self._roots():
            if not root.is_dir():
                continue
            for folder in sorted(root.iterdir()):
                if not folder.is_dir() or folder.name.startswith(("_", ".")):
                    continue
                if folder.name in self.extensions:
                    problems.append(f"{folder.name}: 与已加载的同名拓展冲突（{origin} 版本被跳过）")
                    continue
                ext = ScriptExtension(folder, self)
                ext.origin = origin
                try:
                    await ext.load()
                except Exception as error:  # 兜底：绝不让一个拓展炸掉加载流程
                    ext.error = f"加载异常：{type(error).__name__}: {error}"[:220]
                if ext.id in self.extensions:
                    problems.append(f"{ext.id}: manifest 名与已加载拓展重名，已跳过（{origin}）")
                    await ext.unload()
                    continue
                self.extensions[ext.id] = ext
                if ext.error:
                    problems.append(f"{ext.id}: {ext.error}")
                    self.log(ext.id, ext.error)
        return problems

    async def create_extension(
        self, ext_id: str, manifest: dict[str, Any], code: str = "",
    ) -> dict[str, Any]:
        """把一个（自学习的）拓展写进 learned root 并热加载。

        返回 {"ok": bool, "id": ..., "tools": [...], "error": ...}。
        与手写 scripts/ 完全隔离：id 冲突时保留 builtin 版本，不覆盖。
        """
        if self.learned_root is None:
            return {"ok": False, "id": "", "error": "未配置 learned root（自学习技能目录）"}
        clean_id = _slug(str(ext_id or ""))
        if not clean_id:
            return {"ok": False, "id": "", "error": "技能 id 无效（只能小写字母/数字/中划线，至少2位）"}
        if clean_id in self.extensions and getattr(
                self.extensions[clean_id], "origin", "builtin") == "builtin":
            return {"ok": False, "id": clean_id,
                    "error": f"{clean_id} 是内置/手写拓展，自学习不能覆盖它"}
        folder = self.learned_root / clean_id
        try:
            folder.mkdir(parents=True, exist_ok=True)
            payload = dict(manifest or {})
            payload["name"] = clean_id
            payload.setdefault("api_version", 1)
            payload.setdefault("author", "self-learned")
            if isinstance(payload.get("permissions"), (set, frozenset)):
                payload["permissions"] = sorted(str(x) for x in payload["permissions"])
            (folder / "extension.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
            if str(code or "").strip():
                (folder / "extension.py").write_text(str(code), encoding="utf-8")
        except OSError as error:
            return {"ok": False, "id": clean_id, "error": f"写入失败：{error}"}
        await self.reload()
        ext = self.get(clean_id)
        if ext is None or ext.error:
            return {"ok": False, "id": clean_id,
                    "error": (ext.error if ext is not None else "加载后未找到该拓展")}
        tools = [t.get("name", "") for t in ext.manifest.get("tools", [])]
        self.log(clean_id, f"技能已固化并热加载（{len(tools)} 个工具）")
        return {"ok": True, "id": clean_id, "tools": tools,
                "prompts": len(ext.prompts()), "display_name":
                ext.manifest.get("display_name", clean_id)}

    async def reload(self, ext_id: str | None = None) -> list[str]:
        """重载全部拓展（廉价：目录扫描+模块导入）。ext_id 仅用于日志语义。"""
        return await self.load_all()

    async def unload_all(self) -> None:
        for ext in list(self.extensions.values()):
            try:
                await ext.unload()
            except Exception:
                pass
        self.extensions.clear()

    # ---- 查询 ----
    def get(self, ext_id: str) -> ScriptExtension | None:
        return self.extensions.get(str(ext_id or "").strip())

    def list(self) -> list[dict[str, Any]]:
        return [ext.summary() for ext in sorted(self.extensions.values(), key=lambda e: e.id)]

    def tool_catalog(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ext in sorted(self.extensions.values(), key=lambda e: e.id):
            if ext.error and ext.module is None:
                continue
            if not self.is_enabled(ext.id):
                continue          # 关闭的拓展不向模型暴露工具
            for tool in ext.manifest.get("tools", []):
                rows.append({
                    "script": ext.id,
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "params": tool.get("params", {}),
                })
        return rows

    def prompts(self) -> list[str]:
        rows: list[str] = []
        for ext in sorted(self.extensions.values(), key=lambda e: e.id):
            if ext.module is None and ext.error:
                continue
            if not self.is_enabled(ext.id):
                continue
            rows.extend(ext.prompts())
        return rows

    async def call(self, ext_id: str, tool: str, params: dict[str, Any]) -> str:
        ext = self.get(ext_id)
        if ext is None:
            known = "、".join(self.extensions) or "无"
            raise ScriptExtensionError(f"没有叫 {ext_id} 的拓展（已加载：{known}）")
        if not self.is_enabled(ext_id):
            raise ScriptExtensionError(
                f"拓展 {ext_id} 已关闭（控制台「能力与拓展」里可以打开）")
        return await ext.call_tool(tool, params)
