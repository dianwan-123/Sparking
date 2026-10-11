# demo-template 使用说明

> 这份文件就是 `manual.md`——拓展的**完整使用说明**。模型平时只在系统提示词里看到
> 一句话的"什么时候用"；决定要用它时，会调 `extension_manual(script="demo-template")`
> 把这份全文读进去。所以：**把"什么时候用、分几步、参数细节、注意什么"都写在这里**，
> 写得越像给新同事的操作手册，模型用得越准。

## 什么时候用

- 对话涉及【你的场景】且需要**确定性结果**（例如：掷骰子、抽签、查排班表）；
- 需要访问**只有这个拓展能拿到的东西**（例如：内部词表、排班数据文件）；
- 不要在闲聊、情感安慰这类场景硬用——宁可不用。

## 怎么用

1. 模型调 `extension_call(script="demo-template", tool="template_hello",
   params_json={"name": "Rikka"})`；
2. 拓展在 `call_tool` 里执行，返回**纯文本**结果（返回值会被当作工具结果交给模型，
   不是直接发进聊天）；
3. 模型拿到结果后组织成聊天回复发给用户。

## 工具参数

| 工具 | 参数 | 说明 |
| :--- | :--- | :--- |
| `template_hello` | `name`（选填） | 打招呼的对象；留空 = 对所有人 |

## 资源文件

目录里的 `data/greetings.txt` 是一个资源示例：工具代码里用
`api.read_resource("data/greetings.txt")` 就能读到它（路径不能越出拓展目录）。

## 配置

`config.default.json` 定义了默认配置；装好后在控制台「能力与拓展」→「配置」里改，
改动写在插件数据目录（升级不丢），**每次调用都重读**——改完立即生效。

## 注意事项

- `extension.py` 里只写工具逻辑；长文档、词表这类放资源文件，别硬编码；
- 返回值是给模型看的，**别在里面发消息**——发消息用 `api.qq_send_group` / `api.qq_send_private`；
- 权限按需声明（`llm/memory/net/send/program/media/ssh`），少声明更安全。
