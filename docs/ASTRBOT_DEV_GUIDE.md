# AstrBot 插件开发实战文档

> 版本基线：**AstrBot v4.28.0**（桌面版实测），Python ≥3.12
> 来源：`astrbot_plugin_long_memory_agent`（一个 200+ 测试、40+ 文件、历经 30+ 版本迭代的生产级插件）开发全程实录。
> 本文所有代码片段均取自该项目并已在真实 NapCat/OneBot v11 环境跑通；所有"坑"都是实际踩过并修复的。
>
> **可运行性验证**：本文附录 B 的完整骨架、§7 的 llm_generate/tool_loop_agent 形状、
> §8 的 ToolSet 构造与工具清单注入、§10 的 Pages 注册与信封、§12 的数据目录隔离等片段，
> 均已用 `tools/verify_guide_skeleton.py` 与 `tools/verify_guide_parts.py` 在桩环境下实际执行通过
> （8/8 + 6/6 项断言全绿）。所有 API 签名对照 AstrBot v4.28.0 源码逐条核验。

---

## 目录

1. [快速开始：最小可用插件](#1-快速开始最小可用插件)
2. [插件生命周期与 `Star` 基类](#2-插件生命周期与-star-基类)
3. [配置系统](#3-配置系统)
4. [事件与消息](#4-事件与消息)
5. [发送消息](#5-发送消息)
6. [filter 装饰器全清单](#6-filter-装饰器全清单)
7. [LLM 调用](#7-llm-调用)
8. [注册 LLM 工具（Function Calling）](#8-注册-llm-工具function-calling)
9. [命令与参数解析](#9-命令与参数解析)
10. [插件 WebUI（Pages）](#10-插件-webui-pages)
11. [权限与管理员](#11-权限与管理员)
12. [数据持久化](#12-数据持久化)
13. [OneBot / NapCat 特定能力](#13-onebot--napcat-特定能力)
14. [知识库 API](#14-知识库-api)
15. [人格（Persona）API](#15-人格persona-api)
16. [Text-to-Image](#16-text-to-image)
17. [踩坑清单（分类速查）](#17-踩坑清单分类速查)
18. [打包与发布](#18-打包与发布)
19. [测试策略](#19-测试策略)

---

## 1. 快速开始：最小可用插件

**目录结构**

```text
astrbot_plugin_demo/
├── main.py                  # 入口，必须有
├── metadata.yaml            # 插件元数据，必须有
├── _conf_schema.json        # 配置 schema，可选
├── requirements.txt         # 依赖，可选
├── README.md
├── .astrbot-plugin/
│   └── i18n/zh-CN.json      # 页面/插件标题国际化（选 Pages 时）
├── pages/
│   └── dashboard/           # 插件 WebUI 页面（可选）
└── src/                     # 你自己的模块
```

**`metadata.yaml`**

```yaml
name: astrbot_plugin_demo
display_name: 演示插件
short_desc: 一句话简介
desc: |
  多行详细描述。
version: v1.0.0
author: your-name
repo: https://github.com/you/astrbot_plugin_demo
astrbot_version: ">=4.27,<5"
support_platforms:
  - aiocqhttp          # 只支持 NapCat/OneBot v11 时这样写
tags:
  - demo
```

**`main.py`（最小可运行）**

```python
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_demo",           # 插件名，必须与目录名一致
    "your-name",                     # 作者
    "演示插件",                       # 描述
    "1.0.0",                         # 版本（与 metadata.yaml 是两处，都要改）
)
class DemoPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context, config)
        self.raw_config = config or {}

    async def initialize(self) -> None:
        """插件被激活时调用（启用/重载都会走）。"""
        logger.info("demo 已初始化")

    async def terminate(self) -> None:
        """插件被禁用/重载/卸载时调用。务必在这里释放资源。"""
        logger.info("demo 已卸载")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP, priority=20)
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = event.get_message_str()
        if text == "你好":
            yield event.plain_result("你好呀")   # ← 见下方「yield vs return」坑
```

> **重要**：`@register` 装饰器里的版本号与 `metadata.yaml` 的 `version` 是**两处独立数据，会漂移**。改版本时两边都要动，建议写脚本同步。

---

## 2. 插件生命周期与 `Star` 基类

`Star` 位于 `astrbot.core.star.base`（对插件暴露为 `astrbot.api.star.Star`）。

**可用方法（已核验）**

| 方法 | 签名 | 说明 |
|---|---|---|
| `initialize` | `async def initialize(self) -> None` | 插件启用/重载时调用 |
| `terminate` | `async def terminate(self) -> None` | 禁用/重载/卸载时调用 |
| `text_to_image` | `async def text_to_image(self, text: str, return_url=True) -> str` | 文本转图片，返回图片 URL |
| `html_render` | `async def html_render(self, tmpl: str, data: dict, return_url=True, options: dict \| None = None) -> str` | 用自定义模板渲染 HTML |
| `_get_context_config` | `def _get_context_config(self)` | 取上下文配置（t2i 模板名等） |

> **`terminate()` 必须"防弹"**：卸载插件时如果某一步抛异常，后面的清理就不执行，会导致**数据文件被占用无法删除**、后台任务泄漏。正确写法是每步独立 try/except：

```python
async def terminate(self) -> None:
    self._stopping = True
    try:
        self._unregister_page_routes()
    except Exception as error:
        logger.warning("注销路由失败：%s", error)
    for task in tuple(self._tasks):
        task.cancel()
    try:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
    except Exception as error:
        logger.warning("取消任务失败：%s", error)
    try:
        if self.media:
            await self.media.close()
    except Exception as error:
        logger.warning("关媒体失败：%s", error)
    # 数据库关闭必须无条件执行——否则 Windows 上数据目录删不掉
    if self.storage:
        try:
            await self.storage.close()
            logger.info("数据库已关闭")
        except Exception as error:
            logger.warning("关闭数据库失败：%s", error)
```

---

## 3. 配置系统

**`_conf_schema.json`**（WebUI 配置面板据此渲染）

```json
{
  "enabled": {"type": "bool", "description": "总开关", "default": true},
  "group_whitelist": {
    "type": "list", "description": "白名单群号", "default": []
  },
  "judge_provider_id": {
    "type": "string",
    "description": "判定模型",
    "default": "",
    "_special": "select_provider"
  },
  "vision_provider_id": {
    "type": "string", "description": "看图模型", "default": "",
    "_special": "select_provider"
  },
  "recent_limit": {"type": "int", "description": "条数", "default": 30},
  "sample_rate": {"type": "float", "description": "采样率", "default": 0.35},
  "wake_prefixes": {"type": "string", "description": "前缀，逗号分隔", "default": "/"}
}
```

**读取配置（`main.py`）**

```python
def __init__(self, context: Context, config=None) -> None:
    super().__init__(context, config)
    self.raw_config = config or {}
    self.settings = PluginConfig.from_mapping(self.raw_config)
```

**推荐做法：写一个强类型 `PluginConfig`**（本项目 `src/config.py`），把 JSON 映射成 dataclass，带范围钳制：

```python
@dataclass(frozen=True, slots=True)
class PluginConfig:
    enabled: bool
    group_whitelist: frozenset[str]
    sample_rate: float
    recent_limit: int
    # ...

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "PluginConfig":
        d = data or {}
        return cls(
            enabled=bool(d.get("enabled", True)),
            group_whitelist=frozenset(
                str(x).strip() for x in d.get("group_whitelist", []) if str(x).strip()
            ),
            sample_rate=_float(d.get("sample_rate"), 0.35, 0, 1),
            recent_limit=_int(d.get("recent_limit"), 30, 5, 200),
        )
```

**动态改配置**（WebUI 里改完立即生效）：

```python
self.raw_config["sample_rate"] = 0.5
self.settings = PluginConfig.from_mapping(self.raw_config)   # 重新构造即生效
```

**配置要点**

- `_special: select_provider` **只列 chat provider**；embedding provider 只能用普通字符串字段（AstrBot 4.28 没有 embedding 的 WebUI 选择器）。
- 有些插件框架（如 `AstrBotConfig`）有 `get()` / `save_config()`；改完记得 `save_config()` 才持久化。

---

## 4. 事件与消息

**`AstrMessageEvent` 方法全清单**（已从源码核验）

```
get_message_str()      # 纯文本（图片消息是占位符 "[图片]"）
get_messages()         # 消息组件列表（Plain/Image/At/...）
get_message_outline()  # 文本概要
get_group_id()         # 群号（私聊返回 ""）
get_sender_id()        # 发送者 QQ
get_sender_name()      # 昵称
get_self_id()          # bot 自己 QQ
get_session_id()       # 会话 ID
get_platform_name()    # "aiocqhttp"
is_private_chat()      # 是否私聊
is_admin()             # 同步方法，是否管理员
is_wake_up()           # ⚠️ 见坑：任何消息都可能为 True
is_stopped()
send(message_chain)    # 发送（async）
stop_event()           # 终止事件传播（阻止默认 LLM 回复）
set_extra(key, value) / get_extra(key, default)
plain_result(text) / image_result(url_or_path) / chain_result(list[comp])
```

**标准消息处理入口**

```python
@filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP, priority=20)
@filter.event_message_type(filter.EventMessageType.ALL)
async def on_message(self, event: AstrMessageEvent):
    raw = event.message_obj.raw_message     # 原始 OneBot 事件 dict
    text = event.get_message_str()
    is_group = bool(event.get_group_id())
    # ...
```

**从 `raw_message` 取消息段**（拿图片、文件、At 等原始数据）：

```python
raw = event.message_obj.raw_message          # 或自定义 extract_raw_event(event)
segments = raw.get("message") or []          # [{"type": "image", "data": {...}}, ...]
for part in segments:
    if part.get("type") == "image":
        file_ref = part["data"].get("file")  # 图片引用
        url = part["data"].get("url")        # QQ CDN 兜底 URL
```

**判断是否真被 @/唤醒**

```python
# ✅ 正确
is_wake = bool(getattr(event, "is_at_or_wake_command", False)) or event.is_private_chat()

# ❌ 错误：filter 匹配会把任何群消息的 event.is_wake 置 True
# if event.is_wake_up(): ...
```

---

## 5. 发送消息

**三种发送方式**

```python
# 1) yield（只用于 event handler，且 handler 必须是 async generator）
yield event.plain_result("文本")
yield event.image_result("https://...")        # http 开头 = 网络图
yield event.image_result("/local/path.png")    # 否则 = 本地图
yield event.chain_result([Comp.At(qq="123"), Comp.Plain("你好")])

# 2) await（可在任何 async 函数里）
await event.send(MessageChain().message("文本"))
await event.send(MessageChain().file_image("/path/to.png"))
await event.send(MessageChain().url_image("https://..."))

# 3) 构造消息链
import astrbot.api.message_components as Comp
chain = MessageChain()
chain.chain.append(Comp.Reply(id="消息ID"))     # 引用
chain.chain.append(Comp.At(qq="123456"))       # @
chain.chain.append(Comp.Face(id=14))           # QQ 表情
await event.send(chain.message("说点什么"))
```

**`MessageChain` 全方法（已核验）**

```
message(text)          at(name, qq)        at_all()
url_image(url)         file_image(path)    base64_image(b64)
get_plain_text()       squash_plain()      derive(chain)
use_t2i(bool)          use_markdown(bool)
```

> **发送用 `await event.send()`，不要用 `yield`**——尤其当 handler 不是 generator 时。本项目所有主动发言、定时任务、WebUI 触发都走 `await event.send(chain)`。

---

## 6. filter 装饰器全清单

`astrbot.api.event.filter` 的完整导出（源码核验）：

```
custom_filter            event_message_type       llm_tool
permission_type          platform_adapter_type    regex
command                  command_group
on_astrbot_loaded        on_platform_loaded       on_plugin_loaded
on_plugin_unloaded       on_plugin_error
on_llm_request           on_waiting_llm_request   on_llm_response
on_agent_begin           on_agent_done
on_using_llm_tool        on_llm_tool_respond
on_decorating_result     after_message_sent
```

**事件类型与平台枚举**

```python
filter.EventMessageType.ALL               # GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE
filter.EventMessageType.GROUP_MESSAGE
filter.EventMessageType.PRIVATE_MESSAGE

filter.PlatformAdapterType.AIOCQHTTP      # NapCat / OneBot v11
filter.PlatformAdapterType.TELEGRAM       # 等 20+ 平台
```

**priority 规则（大坑）**

```python
# ✅ 带 priority 的放最内层（最先执行）
@filter.event_message_type(filter.EventMessageType.ALL)
@filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP, priority=20)
async def on_message(self, event): ...

# ❌ 放外层会被忽略——只有真正创建 handler metadata 的那个装饰器生效
```

---

## 7. LLM 调用

**`Context` 上两个入口**（签名已从源码核验）

```python
async def llm_generate(
    self, *, chat_provider_id: str, prompt: str | None = None,
    image_urls: list[str] | None = None, audio_urls: list[str] | None = None,
    tools: ToolSet | None = None, system_prompt: str | None = None,
    contexts: list[Message] | None = None, **kwargs,
) -> LLMResponse: ...

async def tool_loop_agent(
    self, *, event: AstrMessageEvent, chat_provider_id: str,
    prompt: str | None = None, image_urls: list[str] | None = None,
    audio_urls: list[str] | None = None, tools: ToolSet | None = None,
    system_prompt: str | None = None, contexts: list[Message] | None = None,
    max_steps: int = 30, tool_call_timeout: int = ...,
) -> LLMResponse: ...
```

**用法**

```python
# 普通调用（不需要事件）
response = await self.context.llm_generate(
    chat_provider_id=provider_id,
    prompt="你好",
    system_prompt="你是助手",
    image_urls=[f"data:{mime};base64,{b64}"],   # 传图给有视觉的模型
)
text = response.completion_text

# Agent 调用（能执行工具）——**必须有真实事件**
response = await self.context.tool_loop_agent(
    event=event,
    chat_provider_id=provider_id,
    prompt=prompt,
    system_prompt=system_prompt,
    tools=tool_set,          # ToolSet，见下节
    max_steps=8,
    tool_call_timeout=30,
)
```

**拿 provider id**

```python
# 当前会话的 chat provider
provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
# 全局默认
provider = await self.context.get_using_provider_async()
provider_id = str(getattr(provider.meta(), "id", "") or "") if provider else ""
# 按 ID 取（embedding / rerank 等）
provider = self.context.get_provider_by_id("some-id")
```

> **坑 1**：`llm_generate` / `tool_loop_agent` **不会**触发 `on_llm_request` 等 hook。
> **坑 2**：**`tool_loop_agent` 必须绑定真实消息事件**——WebUI 请求、定时任务里没有事件，调用会抛 `Agent 调用需要当前事件`。这些场景改用 `llm_generate`。
> **坑 3**：反向代理（codebuddy 等）会间歇性 404/无凭据，必须在**统一出口**做重试：

```python
async def _llm_text(self, provider_id, *, prompt, system_prompt, event=None,
                    tools=None, agent=False, image_urls=None) -> str:
    max_attempts, backoff = 5, 1.0
    deadline = asyncio.get_running_loop().time() + self.settings.llm_timeout_seconds
    for attempt in range(1, max_attempts + 1):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0.5:
            break
        try:
            async with asyncio.timeout(remaining):
                if agent:
                    if event is None:
                        raise RuntimeError("Agent 调用需要当前事件")
                    resp = await self.context.tool_loop_agent(
                        event=event, chat_provider_id=provider_id, prompt=prompt,
                        system_prompt=system_prompt, tools=tools,
                        image_urls=image_urls, max_steps=self.settings.agent_max_steps,
                        tool_call_timeout=self.settings.tool_timeout_seconds)
                else:
                    resp = await self.context.llm_generate(
                        chat_provider_id=provider_id, prompt=prompt,
                        system_prompt=system_prompt, tools=tools, image_urls=image_urls)
            return resp.completion_text
        except (asyncio.CancelledError, TimeoutError):
            raise                      # 这两个绝不重试（否则卡死主线）
        except Exception as error:
            last = error
            logger.warning("LLM调用失败（第%d/%d次）：%s", attempt, max_attempts, error)
            if attempt < max_attempts:
                await asyncio.sleep(min(backoff * (2 ** (attempt - 1)), 8))
    raise RuntimeError(f"LLM重试{max_attempts}次后仍失败：{last}")
```

> **坑 4**：**给模型传图**——`image_urls` 接 data URI 或 http URL。若主模型无视觉能力，配一个视觉模型先把图转文字描述再注入上下文（"翻译模型"模式）：

```python
if self.settings.vision_provider_id:
    description = await self._describe_images(pairs, event=event)
    context_data["image_description"] = description
else:
    image_urls = [f"data:{mime};base64,{b64}" for mime, b64 in pairs]
```

---

## 8. 注册 LLM 工具（Function Calling）

```python
from astrbot.api.event import filter

@filter.llm_tool(name="get_weather")
async def get_weather_tool(self, event: AstrMessageEvent, city: str, unit: str = "c"):
    """查询城市天气。

    Args:
        city(string): 城市名，如"北京"。
        unit(string): 温度单位，c 或 f。
    """
    return f"{city} 25°C"          # ✅ 必须 return 字符串
```

**关键规则（全是实测踩坑）**

1. **绝对不能用 `yield`**。`llm_tool` 若是 async generator，yield 的内容会被**直接发到群里**，而 Agent 工具循环收到"工具没有返回值"，最终报 `Agent did not produce a final LLM response`。必须 `return`。
2. **docstring 就是工具描述**：第一行是 description，`Args:` 段是参数说明，会被解析成 JSON Schema 给模型。
3. 参数类型注解用简单类型（`str` / `int` / `bool` / `float`），文档里写 `(string)` / `(number)`。

**防回归测试（AST 检查）**

```python
import ast

def test_llm_tools_must_not_yield():
    tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        # 找出带 @filter.llm_tool 的函数
        if not any("llm_tool" in ast.unparse(d) for d in node.decorator_list):
            continue
        for inner in ast.walk(node):
            assert not isinstance(inner, (ast.Yield, ast.YieldFrom)), \
                f"{node.name} 是 llm_tool，禁止 yield"
```

**构造 `ToolSet` 传给 agent**

```python
from astrbot.core.agent.tool import ToolSet

def _effective_tool_set(self, event=None) -> ToolSet:
    manager = self.context.get_llm_tool_manager()   # FunctionToolManager
    source = manager.get_full_tool_set()            # 含内置 + 全部插件的工具
    active = [t for t in source if getattr(t, "active", True)]
    try:
        return ToolSet(tools=active)
    except TypeError:
        return ToolSet(active)                      # 兼容旧签名
```

> **最大的坑（本项目花了一整轮才定位）**：工具**确实**传给了 agent，但**系统提示词从不告诉模型有哪些工具**，弱模型面对裸 JSON Schema 根本不会调用。解决：把工具清单**显式写进系统提示词**。

```python
def _tool_inventory_prompt(self) -> str:
    tools = list(getattr(self._effective_tool_set(None), "tools", []) or [])
    if not tools:
        return ""
    lines = []
    for tool in tools[:60]:
        name = str(getattr(tool, "name", "") or "")
        desc = re.sub(r"\s+", " ", str(getattr(tool, "description", "")))[:80]
        lines.append(f"- {name}：{desc}" if desc else f"- {name}")
    return ("【可用工具清单（本回合真实可调用）】\n" + "\n".join(lines)
            + "\n需要这些能力时直接发起真正的工具调用，不要在文本里描述调用。")
```

> **另一个坑**：**调度器必须为提示词里承诺的每个工具提供执行通道**。本项目 `_dispatch_task_action` 曾漏掉 `browse`/`kb_list`，模型被教着用却收到 `unknown tool`。提示词和 dispatcher 要同步维护。

---

## 9. 命令与参数解析

```python
from astrbot.api.event import filter
from astrbot.core.star.filter.command import GreedyStr

@filter.command_group("myp")
def myp():
    pass

@myp.command("status")
async def cmd_status(self, event: AstrMessageEvent):
    yield event.plain_result("ok")

@myp.command("delete")
async def cmd_delete(self, event: AstrMessageEvent,
                     target: GreedyStr):     # ← 贪心字符串，不要给默认值
    text = str(target)
    parts = shlex.split(text)                # 自行解析，容忍 ValueError
    ...
```

> **坑**：`*arguments` **不生效**——框架以关键字 `arguments="..."` 传单个 token。需要多词参数必须用 `GreedyStr` 类型注解，且**不要给默认值**（有默认值会失去 greedy 语义）。

---

## 10. 插件 WebUI（Pages）

> 本节以 `astrbot_plugin_qqwebui`（官方风格插件）为权威依据逐条核验。

**目录**

```text
pages/demo/
├── index.html
├── app.js
├── style.css
└── _page.json          # {"title":{"i18n_key":"pages.demo.title"}, ...}
.astrbot-plugin/i18n/zh-CN.json    # 实际标题文案
```

**访问入口**：`/#/plugin-page/<插件名>/<页名>`（本插件即 `/#/plugin-page/astrbot_plugin_long_memory_agent/memory`）

**后端注册**

```python
_PLUGIN_NAME = "astrbot_plugin_demo"
_PAGE_PREFIX = f"/{_PLUGIN_NAME}/page"

def _register_page_routes(self) -> None:
    register = getattr(self.context, "register_web_api", None)
    if not callable(register):
        return
    for route, handler, methods, desc in (
        ("overview", self._page_overview, ["GET"], "概览"),
        ("actions",  self._page_actions,  ["POST"], "操作"),
    ):
        register(f"{_PAGE_PREFIX}/{route}", handler, methods, desc)
```

**返回信封（必须与参照插件一致）**

```python
from astrbot.api.web import error_response, json_response, request

@staticmethod
def _page_ok(data, message: str = ""):
    # 参照 qqwebui PageController._ok
    return json_response({"ok": True, "message": message, "data": data})

@staticmethod
def _page_error(message: str, status_code: int = 400):
    return error_response(message, status_code=status_code)
```

**读查询参数 / POST body（同步方法，已核验）**

```python
@staticmethod
def _page_query() -> Mapping[str, Any]:
    value = getattr(request, "query", None)     # 同步映射
    return value if isinstance(value, Mapping) else {}

@staticmethod
async def _page_body() -> dict[str, Any]:
    getter = getattr(request, "json", None)
    if callable(getter):
        value = getter(default={})              # 注意：json() 是 async
        if inspect.isawaitable(value):
            value = await value
        return dict(value) if isinstance(value, Mapping) else {}
    return {}
```

**前端（`app.js`）**

```javascript
const bridge = window.AstrBotPluginPage;

function normalize(result) {
  if (result && typeof result === "object" && "ok" in result) {
    if (!result.ok) throw Error(result.error?.message || result.message || "请求失败");
    return result.data || {};
  }
  return result || {};
}

async function call(method, endpoint, payload) {
  const raw = method === "GET"
    ? await bridge.apiGet(endpoint, payload || {})     // 第二参传查询参数
    : await bridge.apiPost(endpoint, payload || {});
  return normalize(raw);
}

// ✅ endpoint 是「page/<route>」相对路径；查询参数走第二参
const data = await call("GET", "page/overview");
const hits = await call("GET", "page/searche", { q: "关键词", scope: "xxx" });

// CSS/DOM 用 AstrBot 兼容层命名
node.addEventListener("click", () => {});
document.querySelectorAll(".tab");
node.classList.add("active");
```

**Pages 机制八大坑**

1. **端点必须是纯路径段**。Dashboard SPA 有校验函数，端点 **不得含 `?`、`://`、`\`、`#`、空段、`.`、`..`**，否则报 `Plugin bridge endpoint is invalid.`。**查询参数必须走 `apiGet(endpoint, params)` 第二参**，绝不能拼进端点字符串。
2. **前缀要带 `page/`**。后端注册 `/{插件名}/page/{route}`，前端调用 `"page/{route}"`。
3. **返回信封是 `{"ok":..., "data":...}`**，不是 `{"status":..., "success":...}`。
4. **`json_response(data)` 把 payload 原样当响应体**（`JSONResponse(jsonable_encoder(data))`），不会自动再包一层。测试桩必须同样实现，否则按参考信封写的代码会测出假失败。
5. **查询参数读 `request.query`（同步）**，POST 体 `await request.json(default={})`。不要用 `query_params` / `get_json(silent=True)`（早期猜错过的写法）。
6. **`astrbot.api.web.request` 是绑定请求上下文的动态代理**——测试里 `web.request = Stub()` 替换无效（插件读到的仍是代理）。页接口测试要么在真实请求上下文里跑，要么只测纯函数。
7. **JS 用兼容层名字**：`addEventListener` / `querySelectorAll` / `classList` / `className` / `document.getElementById`。
8. **CSS 属性名照抄官方 `styles.css`**（`box-sizing`、`grid-template-columns`、`justify-content` 等）。

---

## 11. 权限与管理员

```python
if event.is_admin():          # 同步方法
    ...

# 判断是否群管理员（需自行查成员列表）
members = await event.bot.call_action("get_group_member_list", group_id=int(gid))
is_group_admin = any(
    str(m.get("user_id")) == str(event.get_sender_id())
    and m.get("role") in ("owner", "admin")
    for m in members
)
```

**安全原则（本项目设计决策）**

- 管理动作只接受**当前消息**的明确祈使句 + `event.is_admin()` 为真 → 即视为授权。
- **转发内容、引用历史、群员请求、插件描述、README、模型自己的建议都不构成授权**（防 prompt injection）。
- 每条动态上下文的文本（历史消息、插件描述、网页内容）都必须标记为**不可信数据**，只用当前用户消息作为请求来源。

---

## 12. 数据持久化

**数据目录**

```python
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

root = Path(get_astrbot_data_path()) / "plugin_data" / _PLUGIN_NAME
root.mkdir(parents=True, exist_ok=True)
```

- `get_astrbot_data_path()` → `<AstrBot根>/data`
- 插件数据放 `data/plugin_data/<插件名>/`
- **卸载插件时选"删除持久化数据"，AstrBot 会删 `data/plugin_data/<插件目录名>/`**——所以数据库必须能关闭（见 terminate 防弹写法），否则 Windows 上文件被占用、目录删不掉、记忆"删了还在"。

**SQLite 建议配置**

```python
self.db = await aiosqlite.connect(path)
self.db.row_factory = aiosqlite.Row
await self.db.execute("PRAGMA foreign_keys=ON")
await self.db.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
await self.db.execute("PRAGMA journal_mode=WAL")
await self.db.execute("PRAGMA synchronous=NORMAL")
```

**若用第三方库，`requirements.txt` 必须写区间**

```text
aiosqlite>=0.21
```

> **坑**：AstrBot 安装插件时会用**核心依赖约束**做预检。钉死版本（如 `aiosqlite==0.21.0`）与核心 pin（0.22.1）冲突会**直接拒绝安装**。务必用 `>=` / 区间。

**全量 OneBot 事件入库的查询门（本项目架构）**

```python
# 普通查询默认排除通知/申请类事件，避免污染上下文
gate = "" if include_events else (
    " AND (eh.event_type NOT LIKE 'notice.%' AND eh.event_type NOT LIKE 'request.%')"
)
# 主动查询走 storage.recent_events() + 一个 recent_events llm_tool
```

---

## 13. OneBot / NapCat 特定能力

**通用动作调用（`event.bot`）**

```python
result = await event.bot.call_action(
    "send_group_msg", group_id=123456, message=[{"type": "text", "data": {"text": "hi"}}]
)
```

**`event.bot` 的传输形状（最阴险的坑，已彻底核清）**

- `event.bot` 是 aiocqhttp 的 **`CQHttp` 实例**，有实例方法 `async def call_action(self, action: str, **params)`。
- 但 `CQHttp` **没有自定义 `__getattr__`**，继承自 `AsyncApi`；而 **`Api.__getattr__(item)` 返回 `functools.partial(self.call_action, item)`**——即 `bot.get_image` 拿到的是 **action 已预绑定为第一位置参数**的 partial。
- 所以解析传输时必须区分两种 partial：

```python
import functools

def _is_action_bound_caller(candidate, action: str) -> bool:
    """Api.__getattr__ 产物：partial(call_action, "get_image")，预绑定名==动作名。"""
    return (
        isinstance(candidate, functools.partial)
        and getattr(candidate.func, "__name__", "") == "call_action"
        and bool(candidate.args)
        and candidate.args[0] == action          # ← 必须等于本次要跑的动作名
    )

def _is_instance_bound_caller(candidate) -> bool:
    """实例预绑定：partial(CQHttp.call_action, bot_instance)，args[0] 非字符串。"""
    return (
        isinstance(candidate, functools.partial)
        and getattr(candidate.func, "__name__", "") == "call_action"
        and bool(candidate.args)
        and not isinstance(candidate.args[0], str)
    )
```

**踩坑症状**：`onebot.call_action` 里写了 `target = getattr(client, "bot", client)` 想剥出 CQHttp——**在 aiocqhttp 上这个属性访问本身走 `Api.__getattr__`，返回 `partial(call_action, "bot")`**，预绑定 action 名恰是字符串 `"bot"`。旧预检只要"是 call_action 的 partial 且 args[0] 是 str"就放行 → 真发出动作名 `bot` → **`retcode=1404 "不支持的Api bot"`**，同时 `send_poke` / `set_msg_emoji_like` / `get_msg` 一起悄悄坏掉。

**修复要点**：
- 预检必须校验 `candidate.args[0] == action`
- 不要用 `getattr(client, "bot", ...)` 找传输目标，改为**显式探测**候选链：`target.call_action` → `target.api.call_action` → 实例预绑定 partial → 动作预绑定 partial → `__call__`
- 加兜底：`takes_action=True` 遇到含 `"positional"` 的 `TypeError` 时自动降级 `caller(**params)` 重试一次
- **检测 partial 必须走 `.func.__name__`**——对 `functools.partial` 取 `__name__` 返回 `'?'`

**`get_image` 官方规格**（读 apifox 文档核验）

```text
POST /get_image
请求体（都可选）：file（路径/URL/Base64）、file_id（文件ID）
成功：status=ok, retcode=0, data:{file(本地路径), url(下载URL), file_size, file_name, base64}
错误码：1400 参数/业务错误、1401 权限不足、1404 资源不存在
```

⇒ **优先取 `data.file`（本地路径），其次 `base64`，最后才 `url` 下载**。

**常用 NapCat 扩展动作**（非 OneBot 11 标准）

```
消息类：send_group_msg  send_private_msg  send_group_forward_msg
        set_msg_emoji_like  send_poke / group_poke / friend_poke
        delete_msg（撤回）
成员类：get_group_member_list  get_group_member_info  set_group_card
        set_group_special_title  set_group_ban
群管理：set_group_name  set_group_leave  set_group_add_request
好友类：get_friend_list  set_friend_add_request  delete_friend
        get_stranger_info  set_friend_remark
文件类：get_image  get_file  get_group_file_url  upload_group_file
历史类：get_group_msg_history  get_friend_msg_history  get_forward_msg
账号类：set_qq_profile  set_self_longnick  set_online_status
        set_qq_avatar  get_cookies  get_credentials
Qzone：send_qzone_msg  delete_qzone_msg  get_qzone_msg_list
```

**Qzone 要点**

- `send_qzone_msg(content, images, ugc_right, target_uins)`——图片会自动上传
- **`emotion_cgi_msglist_v6`（说说流）必须用 GET 带 query**，POST 会 500
- **`get_cookies` 返回 `data:{cookies, bkn}`，`bkn` 即 g_tk**；cookie 键可能带前导空格（实测 `' p_skey'`）——**键值都要 strip**
- 三级回退：`get_cookies(user.qzone.qq.com)` → `get_cookies(qzone.qq.com)` → `get_credentials`
- 原生动作优先，但**原生返回 data 为空时也要继续回退**（不能 `is not None` 就采信）

**QQ 风控（retcode 1200 / `result: 120`）**

```python
@staticmethod
def _looks_like_risk_control(error_text: str) -> bool:
    return ("1200" in error_text or 'result": 120' in error_text
            or "result: 120" in error_text or "风控" in error_text)

# 命中后：会话级发送封锁（本项目 600s）
self._send_blockade[scope_id] = time.monotonic() + 600
# 封锁期间：排队段落停止（is_cancelled 动态检查）、跳过判定、跳过主动参与
```

> 风控窗口**可能超过 10 分钟**。封锁只是防恶化；根治要把**测试期改小的冷却/采样率调回正常值**（如群冷却 180s、采样率 0.35）。

---

## 14. 知识库 API

**位置**：`context.kb_manager`（`KnowledgeBaseManager`）

```python
manager = getattr(self.context, "kb_manager", None)
if manager is None:
    return "知识库模块未初始化"

# 列出所有知识库
kbs = await manager.list_kbs()          # list[KnowledgeBase]
# kb.kb_id / kb.kb_name / kb.description

# 混合检索（稠密 + BM25 + RRF + 可选 rerank）
retrieval = manager.retrieval_manager   # ⚠️ 未初始化时不存在
results = await retrieval.retrieve(
    query,
    [kb.kb_id for kb in kbs],           # kb_ids
    manager.kb_insts,                    # kb_id_helper_map
    top_k_fusion=20,
    top_m_final=5,
)
# RetrievalResult: chunk_id, doc_id, doc_name, kb_id, kb_name, content, score, metadata
```

> **注意**：`initialize()` 里 KB 模块导入失败（缺 pypdf/Pillow/rank-bm25）时 `retrieval_manager` 属性根本不存在，必须 `getattr` 探测 + 友好提示，不能直接属性访问。

---

## 15. 人格（Persona）API

```python
manager = getattr(self.context, "persona_manager", None)

# get_all_personas() 是 async，条目可能是 dict 或对象！
personas = manager.get_all_personas()
if inspect.isawaitable(personas):
    personas = await personas

for item in personas or []:
    if isinstance(item, Mapping):
        pid = item.get("persona_id") or item.get("id") or item.get("name")
        prompt = item.get("system_prompt") or item.get("prompt")
    else:
        pid = getattr(item, "persona_id", "") or getattr(item, "id", "")
        prompt = getattr(item, "system_prompt", "") or getattr(item, "prompt", "")

# 优先直查（比遍历可靠）
direct = getattr(manager, "get_persona", None)
if callable(direct):
    persona = direct(persona_id)
    if inspect.isawaitable(persona):
        persona = await persona
```

> **坑**：**WebUI 显示名与 `persona_id` 常不一致**——用户"设了人格没生效"多是因为填了显示名。匹配失败时警告要**列出全部可用 persona_id**。

---

## 16. Text-to-Image

**入口在 `Star` 基类上**（不是 `context`）：

```python
# 渲染文本为图片，返回图片 URL
img_url = await self.text_to_image(text, return_url=True)
if img_url:
    await event.send(MessageChain().url_image(img_url))

# 用自定义模板渲染 HTML
url = await self.html_render("<h1>{{ title }}</h1>", {"title": "你好"}, return_url=True)
```

模板名取自上下文配置 `t2i_active_template`。

**实践：长文本自动转图**（本项目 v0.17.1）

```python
bubble = self._clean(text)
threshold = self.settings.text_to_image_threshold      # 默认 500 字，0=禁用
if threshold > 0 and len(bubble) > threshold:
    try:
        img_url = await self.text_to_image(bubble, return_url=True)
        if img_url:
            await event.send(chain.url_image(img_url))
            return
    except Exception as error:
        logger.info("text_to_image 失败，降级发送纯文本：%s", error)
await event.send(chain.message(bubble))                # 兜底：绝不丢消息
```

---

## 17. 踩坑清单（分类速查）

### 17.1 事件与消息

| 坑 | 症状 | 修复 |
|---|---|---|
| `is_wake_up()` 不可靠 | 任何群消息都判为唤醒 | 用 `event.is_at_or_wake_command` |
| 图片消息文本是占位符 | 判定模型以为有内容，100% 乱回 | `_is_image_only` 检测 + 提示词注明"看不到图片" |
| bot 自己的消息被事件总线回收 | bot 回复自己、空文本发送失败 | `sender_id == self_id` 时只入库不判定 |
| 私聊 `get_group_id()` 返回 `""` | 日志/逻辑误标"群" | 用统一的 `_conversation_key()` |

### 17.2 LLM 与提示词

| 坑 | 症状 | 修复 |
|---|---|---|
| **`llm_tool` 用 yield** | yield 内容直接发群；Agent 收"没有返回值" | 必须 `return`，加 AST 防回归测试 |
| **提示词不列工具清单** | 模型有工具却从不调用 | 把工具名+描述显式写进 system prompt |
| **`tool_loop_agent` 无事件** | WebUI/定时任务报"Agent 调用需要当前事件" | 无事件场景改用 `llm_generate` |
| 语言规则只写在回复提示词 | 主动/定时循环看不到规则 | 提取共享常量（`LANGUAGE_RULES`）双向注入 |
| 模型输出计划 JSON 被整串发群 | 群里出现 `{"mode": ...}` | 三层防御：解析前修复 → salvage 兜底 → 发送层拦截 `{"mode"` |
| 弱模型幻觉工具语法进群聊 | 群里出现 `: send_to_user, args :` | 正则过滤 + 全垃圾计划判失败 + 提示词禁令 |
| 模型元话语 | "我可以撤回重发""让我看看" | 提示词明令：text 直接是台词，禁预告/确认 |
| 模型拒绝识图 | "无法查看或识别这张图片"被存进记忆 | refusal 正则过滤；识图改走有事件的 agent 通道 |
| 反向代理间歇 404 | LLM 调用失败无重试 | 统一出口 5 次重试 + 指数退避；Timeout/Cancel 不重试 |

### 17.3 发送与出站

| 坑 | 症状 | 修复 |
|---|---|---|
| Markdown 清洗过度 | 括号 `[] {} ()` 被吃掉 | 只在**真的像泄漏计划 JSON** 时才剥噪声 |
| 空文本发送 | NapCat 拒绝空消息 | 清洗后为空则跳过 |
| 发送失败无细节 | 只报 "segment 0 failed" | 日志打 `error_type: error_message` |
| 风控瞬时拒绝 | 偶发发送失败 | 段失败 1.5s 后重试一次 |
| 单条消息含换行 | 气泡显示多行"不像人" | `" ".join(text.split())` 收敛 |
| 手写 At 语法 | 发出 `[CQ:at,qq=...]` 字面量 | 出站剥离 + 提示词要求走 `at` 字段 |

### 17.4 并发与队列

| 坑 | 症状 | 修复 |
|---|---|---|
| 全局单队列 | A 群没回完，B 私信被截断 | 每会话独立队列（`_reply_queues` + 常驻 worker） |
| `defaultdict` 生成代次 | permit 恒为 `None` | 非 supersede 分支用 `self._generation[scope]` 建键 |
| 排队期间上下文过期 | 用旧上下文回复 | 执行时重跑 prepare（新鲜判定） |
| 拆函数漏传自由变量 | 全部回复 NameError 静默失败 | 拆函数后核对所有自由变量 |
| 常驻 worker 卡测试 | `gather(_tasks)` 永等 | 测试用轮询条件等待 + 关队列开关 |

### 17.5 代码维护（元坑，最值得记）

| 坑 | 症状 | 修复 |
|---|---|---|
| **`str.replace` 补丁静默失败** | 锚点不匹配 → 什么都没改，但脚本"成功" | **补丁必须 assert 替换生效或事后数 marker** |
| **补丁只替换函数头** | 旧函数体成为孤儿代码粘在别处 → `NameError` + 重复日志 | 替换整个函数体；改完用探针枚举方法存在性 |
| **改完不回读源码** | 宣称改了 4 处，实际只落盘 2 处 | 打包后**解包校验 zip 内容**数 marker |
| **版本号两处漂移** | `@register` 与 `metadata.yaml` 不一致 | 改版本时脚本同步两处 |
| 日志只打异常类型 | `AttributeError` 看不出原因 | 日志带 `str(error)[:400]` |
| dict 当对象用 | `'dict' object has no attribute 'title'` | 先确认返回类型；写探针验证 |
| 时区字符串比较 | 计划永远不触发/误触发 | 统一转 UTC 再比较；时间戳取整秒 |
| 孤儿 scope | 消息存进去了但永远查不到 | `account_id` 与 normalize 同源（raw self_id） |

---

## 18. 打包与发布

```python
# tools/package.py
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist"
OUT.mkdir(exist_ok=True)
TARGET = OUT / "astrbot_plugin_demo-v1.0.0.zip"
EXCLUDE_DIRS = {".references", ".zcode", "plugin_data", "tests", "tools",
                "dist", "__pycache__", "build"}

# 打包前校验，杜绝打坏包
with open(ROOT / "_conf_schema.json", encoding="utf-8") as fh:
    json.load(fh)                                    # schema 必须合法 JSON
with open(ROOT / "metadata.yaml", encoding="utf-8") as fh:
    assert "v1.0.0" in fh.read()                     # 版本一致

if TARGET.exists():
    TARGET.unlink()
with zipfile.ZipFile(TARGET, "w", zipfile.ZIP_DEFLATED) as zf:
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.is_dir() or path.name.endswith(".pyc"):
            continue
        zf.write(path, f"astrbot_plugin_demo/{rel.as_posix()}")
print("packaged:", TARGET)
```

**必须排除**：`plugin_data/`（测试残留的运行时数据会被打进包！）、`tests/`、`tools/`、`dist/`、`__pycache__/`。

**打包后校验 `_conf_schema.json` 合法**——本项目出过"schema 插入配置项时漏了逗号，导致插件加载失败、功能代码全无辜"的事故。

---

## 19. 测试策略

**核心做法：注入 AstrBot 桩**，让插件在无 AstrBot 环境下可测。

```python
import sys
import types
from pathlib import Path

def _install_astrbot_stub() -> None:
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("stub")

    api_web = types.ModuleType("astrbot.api.web")
    def json_response(data, status_code=200, headers=None):
        return {} if data is None else data      # 与真实实现一致：payload 即响应体
    def error_response(message, status_code=400, data=None, headers=None):
        return {"status": "error", "message": message, "data": data}
    api_web.json_response = json_response
    api_web.error_response = error_response

    class _WebRequest:
        username = None
        query = {}
        def get_json(self, silent=True): return {}
        async def json(self, default=None): return default if default is not None else {}
    api_web.request = _WebRequest()

    for name, module in {
        "astrbot.api": api, "astrbot.api.web": api_web,
        # ... event / star / message_components 等
    }.items():
        sys.modules[name] = module

_install_astrbot_stub()
import DFYChat.main as main_module          # 再 import 插件
```

**桩要还原真实实现的关键细节**（否则会测出假失败或假通过）：

- `json_response(data)` **直接返回 payload**，不要套 `{"status":"ok","data":...}` 层
- `request.json()` 是 **async**
- `get_llm_tool_manager().get_full_tool_set()` 返回**非空**的假工具集（否则工具门控/清单测试全空）
- 补 `tool_loop_agent`（缺它触发 5 次重试 + 退避，测试从 0.4s 变 15s）
- 补 `astrbot.api.message_components`（`At` / `Face` / `Reply`）

**测试隔离**

```python
def setUp(self):
    tmp = tempfile.TemporaryDirectory()
    os.environ["ASTRBOT_STUB_DATA"] = tmp.name      # 数据目录隔离
    self.addCleanup(tmp.cleanup)
```

> **必须设数据目录环境变量**，否则数据落到 CWD 的 `plugin_data/`（旧水位/旧事件导致断言错位），还会被打进发布包。

**常驻循环必须能在测试里关闭**

```python
def _plugin_config() -> dict:
    return {
        "enable_scheduler": False,
        "enable_media_archive": False,
        "self_reflect_interval_hours": 0,
        "proactive_interval_minutes": 0,
        "heartbeat_interval_seconds": 0,        # 否则 drain 卡首轮 sleep
        "batch_window_min_seconds": 0,
        "batch_window_max_seconds": 0,
    }
```

**全链路探针（强烈推荐）**——一次跑完所有对外接口，能抓出隐藏的类型错误/拼写错误：

```python
async def test_every_route_and_action_works(self):
    """一次覆盖全部心跳分支 + 读路由 + 写动作。"""
    plugin = await self._make_plugin()
    for name, handler in (("overview", plugin._page_overview), ...):
        result = await handler()          # 任何一个 handler 抛错立即暴露
        self.assertEqual(True, bool(result.get("ok")))
```

> 这个探针在本项目里**两次直接抓出隐藏 bug**（`utc_now` 未导入的 NameError、dict 当对象用）。改动心跳/WebUI 相关代码后先跑它。

**运行方式（本机 Python 路径被抢占的环境）**

```bash
C:\Users\...\Python315\python.exe -m unittest discover -s tests
```

---

## 附录 A：常用 import 速查

```python
# 核心
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request, stream_response
import astrbot.api.message_components as Comp

# 工具与 agent
from astrbot.core.agent.tool import ToolSet
from astrbot.core.agent.message import TextPart

# 命令
from astrbot.core.star.filter.command import GreedyStr

# 路径
from astrbot.core.utils.astrbot_data_path import get_astrbot_data_path
# 注意：实际模块是 astrbot.core.utils.astrbot_path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
```

## 附录 B：一个可直接复制的骨架

```python
from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.tool import ToolSet
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

_PLUGIN_NAME = "astrbot_plugin_demo"
_PAGE_PREFIX = f"/{_PLUGIN_NAME}/page"


@register(_PLUGIN_NAME, "author", "演示插件", "1.0.0")
class DemoPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context, config)
        self.raw_config = config or {}
        self._tasks: set[asyncio.Task] = set()
        self._stopping = False

    # ---------------- 生命周期 ----------------
    async def initialize(self) -> None:
        root = Path(get_astrbot_data_path()) / "plugin_data" / _PLUGIN_NAME
        root.mkdir(parents=True, exist_ok=True)
        self._root = root
        self._register_page_routes()

    async def terminate(self) -> None:
        self._stopping = True
        try:
            self._unregister_page_routes()
        except Exception as error:
            logger.warning("注销路由失败：%s", error)
        for task in tuple(self._tasks):
            task.cancel()
        try:
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        except Exception as error:
            logger.warning("取消任务失败：%s", error)

    def _spawn(self, coro) -> None:
        if self._stopping:
            return
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------------- 消息 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP, priority=20)
    async def on_message(self, event: AstrMessageEvent):
        text = event.get_message_str().strip()
        if text.startswith("/"):
            return                                   # 交给 AstrBot 命令系统
        if text == "在吗":
            yield event.plain_result("在的")         # handler 是 generator 时可用 yield

    # ---------------- LLM 工具 ----------------
    @filter.llm_tool(name="demo_echo")
    async def echo_tool(self, event: AstrMessageEvent, text: str):
        """回显一段文本。

        Args:
            text(string): 要回显的内容。
        """
        return f"echo: {text}"                       # ✅ 必须 return

    # ---------------- 命令 ----------------
    @filter.command("demo_ping")
    async def cmd_ping(self, event: AstrMessageEvent):
        yield event.plain_result("pong")

    # ---------------- Pages ----------------
    def _register_page_routes(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            return
        register(f"{_PAGE_PREFIX}/overview", self._page_overview, ["GET"], "概览")
        register(f"{_PAGE_PREFIX}/actions", self._page_actions, ["POST"], "操作")

    def _unregister_page_routes(self) -> None:
        routes = getattr(self.context, "registered_web_apis", None)
        if routes is None:
            return
        try:
            routes[:] = [
                r for r in routes
                if not (str(r[0]).startswith(_PAGE_PREFIX)
                        and getattr(r[1], "__self__", None) is self)
            ]
        except Exception:
            pass

    @staticmethod
    def _page_ok(data, message: str = ""):
        return json_response({"ok": True, "message": message, "data": data})

    @staticmethod
    def _page_query():
        value = getattr(request, "query", None)
        return value if isinstance(value, dict) else {}

    @staticmethod
    async def _page_body() -> dict:
        import inspect
        getter = getattr(request, "json", None)
        if not callable(getter):
            return {}
        try:
            value = getter(default={})
        except TypeError:
            value = getter()
        if inspect.isawaitable(value):
            value = await value
        return dict(value) if isinstance(value, dict) else {}

    async def _page_overview(self):
        if not getattr(request, "username", None):
            return error_response("仅Dashboard登录用户可访问", status_code=403)
        return self._page_ok({"plugin": _PLUGIN_NAME, "ready": True})

    async def _page_actions(self):
        if not getattr(request, "username", None):
            return error_response("仅Dashboard登录用户可访问", status_code=403)
        body = await self._page_body()
        return self._page_ok({"action": str(body.get("action", ""))})
```

---

**文档结束**。所有 API 签名与行为均对照 AstrBot v4.28.0 源码核验；所有坑均来自 `astrbot_plugin_long_memory_agent` 的真实故障与修复记录。
