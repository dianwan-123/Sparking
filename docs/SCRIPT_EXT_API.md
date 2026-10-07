# scripts/ 万能拓展接口（Script Extensions）v1

> ⚠️ **实验性功能**：整套拓展机制仍在打磨，接口与加载行为可能随版本调整，
> 不建议把关键流程挂在它上面。
>
> 兼容性承诺（实验期内尽力遵守）：**这份契约一旦发布就不再破坏**。你（或你派出去的 agent）按本文档
> 写的拓展，在插件之后的任何版本里都能继续加载运行——接口只增不删；
> manifest 未知字段一律忽略；钩子缺失一律合法；单个拓展出错绝不影响插件本体。

## 一、它是什么

在插件目录下建 `scripts/<你的拓展ID>/` 文件夹，里面放一个 `extension.json`
（清单）和一个可选的 `extension.py`（代码），重启插件（或 WebUI 触发
`script_reload`）即可给 bot：

1. **注册工具**——bot 会在系统提示词里看到工具名+说明+参数，用
   `script_call(script, tool, params_json)` 调用，结果直接回到对话；
2. **注入提示词**——manifest 里的 `prompts`（或代码里的 `get_prompts()`）
   会拼进 bot 的回复系统提示词与自主行动提示词；
3. **消息分支/外部 agent**——拓展代码里可用 `api.http_post_json` 把消息
   相关内容转发给你自己的服务/其他 agent，再把结论作为工具结果或记忆写回；
4. **带说明的能力**——每个工具的 description/params 就是给 bot 看的使用说明，
   写清楚 bot 才会用得对。

打包发布：`tools/package.py` 会把 `scripts/` 下所有文件夹原样打进插件包
（`__pycache__` 除外），用户解压即得。

## 二、目录结构

```
scripts/
  my_ext/                  ← 文件夹名即拓展ID（小写字母/数字/-_）
    extension.json         ← 必须存在
    extension.py           ← 有 tools 时必须存在；其余情况可选
    任何其他文件…           ← 资源/模板/词库等，随包分发
```

## 三、extension.json（manifest）

```json
{
  "name": "my_ext",                    // 必填，须与文件夹名一致，^[a-z0-9][a-z0-9_-]{0,47}$
  "api_version": 1,                    // 必填，当前为 1；高于插件支持版本会拒绝加载并说明
  "display_name": "我的拓展",           // 可选，WebUI/日志展示名
  "version": "1.0.0",                  // 可选，拓展自身版本
  "description": "一句话说明这个拓展干什么",  // 可选
  "author": "you",                     // 可选
  "permissions": ["llm", "memory", "net", "send", "program", "media", "ssh"],  // 可选，见第五节
  "tools": [                           // 可选：给 bot 的工具
    {
      "name": "weather_query",         // 工具名（bot 调用时用）
      "description": "查询某城市未来三天天气",   // 给 bot 看的使用说明，写清楚！
      "params": {"city": "城市名", "days": "天数1-3，默认3"}  // 参数说明（字符串值=说明）
    }
  ],
  "prompts": ["附加提示词段落1", "段落2"],   // 可选：注入 bot 系统提示词
  "config_schema": {"city": "默认城市"}      // 可选：纯自述用途，插件不读
}
```

**兼容规则**：上面没提到的字段随便加，插件一律忽略；缺 `tools`/`prompts` 合法
（纯提示词拓展可以没有代码文件）。

## 四、extension.py（hooks）

```python
# 【铁律】永远不要 import 插件内部（from main import ... 是禁手）。
# 所有能力都通过参数传入的 api 对象获得——插件升级时 api 只增不删。

async def on_load(api):
    """可选。加载后调用一次。适合初始化文件、读配置。"""

async def on_unload(api):
    """可选。卸载/重载前调用。适合收尾。"""

async def call_tool(api, name: str, params: dict) -> str:
    """manifest 声明了 tools 时必须实现。所有工具共用这一个入口：
    按 name 分发，返回字符串（会作为工具结果回到 bot 对话里）。"""

def get_prompts(api) -> str | list[str]:
    """可选。动态提示词（每次组装系统提示词时调用），与 manifest prompts 合并。"""
```

同步/异步均可（普通 `def` 返回字符串也行）；抛异常只会让这一次工具调用
返回错误说明，不会影响插件。

## 五、api 对象（ScriptExtensionAPI v1）

| 成员 | 说明 | 需要权限 |
|---|---|---|
| `api.name` | 拓展 ID（只读） | - |
| `api.data_dir` | 本拓展专属持久化目录 `Path`（在插件数据目录下，重装/升级不清空） | - |
| `api.log(msg)` | 打日志（自动带 `[拓展:id]` 前缀） | - |
| `api.config()` | 读 `data_dir/config.json`（用户手改热生效），无文件返回 `{}` | - |
| `api.kv_get(key, default=None)` / `api.kv_set(key, value)` | 轻量 kv 持久化（`data_dir/kv.json`） | - |
| `api.now()` | 当前时间字符串 | - |
| `await api.llm(prompt, system_prompt="", provider_id="")` | 调用 bot 配置的聊天模型，返回文本 | `llm` |
| `await api.memory_note(text)` | 写一条记录进 bot 的可查询记忆（不占主线上下文） | `memory` |
| `await api.http_get(url, timeout=20)` | GET，返回 `(status, text)`（带 SSRF 防护；内网回环地址仅放行插件 Studio 端口，供 `run_program` 自托管服务用） | `net` |
| `await api.http_post_json(url, payload, timeout=20)` | POST JSON，返回 `(status, text)`——**给消息开分支转发到外部 agent 用这个** | `net` |
| `await api.http_get_bytes(url, timeout=20)` | GET，返回 `(status, bytes)`——拉二进制（图片等） | `net` |
| `await api.run_program(program_id, code, title, description)` | 把 Flask 代码部署到插件内置 Studio（`/p/<id>/`）并运行，返回 base_url；拓展可借此承载 Web 服务/游戏等（首次部署后可再 `run_program` 热更新代码） | `program` |
| `await api.save_image(png, note="")` | 把 PNG 二进制存进媒体库（WebUI 媒体页可看），返回 media_id | `media` |
| `await api.ssh_exec(command, timeout=60)` | 在主人配置好的远程服务器上执行 shell，返回 `{"exit_code","stdout","stderr","truncated"}`（stdout 上限 12k） | `ssh` |
| `await api.media_from_remote(remote_path, note="")` | **把远程服务器上的文件/截图拉进媒体库**，返回 media_id（配 `send_image` 或由 bot 发出）——传文件必须走这条，`ssh_exec` 的输出传不了 | `ssh`+`media` |
| `await api.qq_send_group(group_id, text)` | 发群消息（只允许白名单群；文本自动做气泡清洗） | `send` |
| `await api.qq_send_private(user_id, text)` | 发私聊消息 | `send` |

**权限模型**：manifest 的 `permissions` 数组声明本拓展用到哪些能力，未声明就
调用会抛"未声明权限"错误。这是给未来留的闸门——现有七种：`llm` `memory`
`net` `send` `program` `media` `ssh`（未来新增能力=新增权限名+新增 API 方法，
老拓展不受影响）。

## 六、bot 侧长什么样

- 系统提示词会多一段工具清单：
  `【scripts 拓展工具（用 script_call(script, tool, params_json) 调用）】
   - weather_query（my_ext）：查询某城市未来三天天气  参数：city:城市名、days:天数`
- bot 调用 `script_call("my_ext", "weather_query", "{\"city\":\"北京\"}")`，
  你的 `call_tool` 返回什么，bot 就看到什么（上限 4000 字）。
- bot 也可主动 `script_list` 查看全部拓展与工具。
- `prompts` 注入位置：回复系统提示词（人在群聊/私聊回复时）与自主行动提示词
  （定时任务/主动参与时）都会带上。

## 七、消息分支到外部 agent 的推荐姿势

`call_tool` 里拿不到原始消息事件（这是刻意设计——消息管线稳定优先）。
要做消息分支，两种方式：

1. **提示词驱动**：在 prompts 里告诉 bot"遇到 X 类消息就调
   `script_call` 的 `forward` 工具"，`call_tool` 收到后用
   `api.http_post_json` 转发原文，把外部 agent 的回答作为工具结果返回；
2. **外部拉取**：外部 agent 定时调插件 WebUI 的
   `page/actions`（`action=script_list`）或后续版本开放的查询接口拉数据。

## 八、调试与生命周期

- 启动日志：`scripts 拓展已加载 N 个（工具 M 个）`；单个拓展的问题会以
  `scripts 拓展加载问题：<id>: ...` 警告列出，不影响其他拓展。
- WebUI 动作：`{"action":"script_list"}` / `{"action":"script_reload"}`
  （改完代码不用重启插件）。
- 数据目录：`plugin_data/astrbot_plugin_long_memory_agent/script_data/<id>/`
  ——`config.json` 用户手写、`kv.json` 拓展自用，都不会被插件升级覆盖。
- 重复 ID：以文件夹名创建条目；manifest.name 与文件夹名不一致时以
  manifest.name 为准并告警。

