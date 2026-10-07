<!-- markdownlint-disable MD024 MD033 MD041 -->

<div align="center">

<img src="logo.png" alt="Sparking" width="140">

# ✨ Sparking

**让你的 Bot 拥有记忆、情绪与自主行动的拟人化 Agent**

*不只是"回答问题"——它会记得你、会主动找你、会在群里像真人一样接话、攒梗、挂人。*

<p>
  <img src="https://img.shields.io/badge/version-v1.0.0-orange.svg" alt="v1.0.0">
  <img src="https://img.shields.io/badge/AstrBot-4.27%2B-orange.svg" alt="AstrBot 4.27+">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Platform-OneBot%20v11%20(NapCat%20%2F%20SnowLuma)-12B7F3.svg" alt="Platform">
</p>

简体中文 · 作者 **Rikka0612**

</div>

---

> [!IMPORTANT]
> Sparking 面向 **AstrBot 4.27+ / aiocqhttp / OneBot v11（NapCat、SnowLuma）**。
> 装上并填好 `group_whitelist` 之后，它会接管这些群的完整消息：建长期记忆、按真人节奏接话、按需自主行动。

## 🌟 它是什么

Sparking 把"群聊机器人"做成一个**有连续生活的角色**，而不是一个问答窗口：

- **它记得**——白名单群的每条消息都入库，用分层摘要 + 带证据的记忆账本维持长期上下文，需要原话、日期、链接时再全文回查。
- **它有情绪**——效价/唤醒双轴随消息连续变化，表达温度分档，心情会自然回归，签名会跟着生活改。
- **它会主动**——不等你 @：心跳里执行日程、闲时潜水看群；短巡查看通知、梳理话题、接话吐槽；长周期总结记忆、发说说、打理资料。
- **它会干活**——Agent 模式能调几十个工具：画图、跑代码、连 SSH、读写工作区文件、做网页小程序、把聊天记录做成卡片图或合并转发。
- **它分得清场合**——每个人的话只属于说话的人，旧任务不会硬接新消息，心声与内部字段绝不会漏到聊天里。

## 🧠 核心原理

这一节是 Sparking 的全部设计要点。看懂它，就知道遇到问题该调哪一项。

### 1. 记忆：分层压缩 + 证据回查

```
消息到达 ─▶ ① 原样入库（含媒体 / 合并转发展开 / 引用消息展开）
            │
            ├▶ ② L1 压缩：一批消息 → 带证据的结构化摘要
            │      └▶ L2 情节 → L3 主题与时间线（滚雪球式分层）
            │
            ├▶ ③ 记忆账本：事实/偏好/待办，每条挂在真实 message_id 上
            │
            └▶ ④ 上下文组装（有界）：最近消息 + 分层摘要 + 账本 + 精确证据
                     + 跨会话记忆（带 origin 出处）+ 情绪/好感/风格档案
```

- **压缩不等于丢失**：摘要只是索引，模型说"我记得你说过 X"时背后是一次真实全文检索（FTS5，可选 embedding 语义检索），而不是幻觉。
- **跨会话但带出处**：其他群/私聊的记忆能引用，但必须如实说明"那是在别处发生的"；同一个人的印象跨群合并，名字会自动继承。
- **消费失败不卡死**：压缩解析再离谱也会落一个合法节点、水位照常推进——记忆不该因为模型偶发抽风而断流。
- **可读可改**：控制台里的**记忆森林**把整棵树画成节点图（圆形节点 + 贝塞尔连线），悬停看详情、点节点编辑/删除/加子节点。

### 2. 情绪与人格

- 情绪走**效价/唤醒双轴**，每条消息做一次确定性更新（不靠模型"演"），随时间**橡皮筋式回归**。
- 情绪映射到**表达温度**（四档）：直接影响回复的句子长度、语气词密度、是否连发。
- 人格（Persona）沿用 AstrBot 的人格系统，Sparking 只在外层追加语言铁律与群生存行为，不覆盖你的人设。

### 3. 主动性：三层循环 + 一切皆任务

| 循环 | 默认节奏 | 干什么 |
| :--- | :--- | :--- |
| 心跳 | 120 秒 | 执行到点的日程/任务、闲时消遣、潜水看群 |
| 短巡查 | 15 分钟 | 看通知、梳理群话题、判断是否接话、处理私聊 |
| 长打理 | 6 小时 | 总结自己的记忆、发说说、打理账号资料 |

三者最终都落到**统一任务队列**（同一张 `tasks` 表）：

- **一切皆任务**：回复、Agent 执行、工具调用、通知、反思、压缩都是任务，语义一致——同会话串行、带优先级、可抢占。
- **短期 / 长期 / 区间**：`max_runs` 控制"跑几次"，`interval_seconds` 控制"多久跑一次"；`window` 支持绝对时间窗与每日时段（`daily: ["09:00","22:00"]`），超出窗口即过期，不补救、不堆积。
- **失败退避重试**：出错按 60/300/900 秒退避，重试上限后如实记 `last_error`。
- **bot 自己排期**：它可以用 `task_add` / `task_list` / `task_cancel` / `task_update` 给自己安排"晚上十点提醒我发今日总结"这类事。

### 4. 判定与执行：两次模型调用

1. **判定器**——这条消息要不要接、怎么接、是否先做需求梳理（`needs_plan` 由模型自己判断，不是关键词表）。
2. **回复 / 执行 Agent**——带工具循环干活：能画就画、能查就查、要挂人就挂人；最后用拟人短句收尾，**不许空头承诺**。

需求梳理由**只读子 agent** 完成（可查记忆、看设计项目、核服务器状态），产出自由形式的 `understanding / plan / reply_strategy`——**没有固定任务分类，做什么、怎么做由主 agent 自己决定**。

### 5. 拟人节奏

- **连发 @ 只回一次**：@/私聊先进合并窗口（防抖 4 秒、上限 15 秒），像真人一起读完再开口。
- **普通群消息攒批**：15~60 秒随机窗口后统一判定一次，一批通常最多回一次；判定与回复两层都有【批次纪律】，避免"逐条作答"。
- **打字延迟**：按字符数模拟打字速度并加抖动，不秒回也不卡死。
- **三道闸**：静默时段 / 同群冷却 / 每小时动作上限，全部可配。

### 6. 安全边界（这些是硬约束，不是提示词祈祷）

- **心声防火墙**：内部思考、自白、计划 JSON 一律不出网；发送层/纯文本层/兜底文案三处各自拦截。
- **对象纪律**：回复对象、引用对象、材料归属三层校验——不会回错人、认错材料。
- **不猜**：读不到的合并转发如实说读不到，绝不用"过期"当借口或猜内容。
- **失控重复检测**：模型陷入复读时自动截断。

## 🚀 安装

1. 从 Releases 下载 zip，在 AstrBot WebUI「插件」页上传安装（或直接把插件目录放进 `data/plugins/`）。
2. 进入插件**配置页**，至少填两项：
   - `group_whitelist`：允许 Sparking 完整记忆与自主聊天的群号；
   - 需要 Agent 能力时填 `judge_provider_id` / `reply_provider_id`（留空则用 AstrBot 当前模型）。
3. 可选依赖按需**自动安装**：`playwright`（截图/浏览器）、`pypdf`（读 PDF）、`pandas`/`matplotlib`（表格与画图）、`flask`（小程序）、`paramiko`（SSH）——不写进 `requirements.txt`，免得与 AstrBot 核心依赖打架。

> [!TIP]
> **NapCat / SnowLuma 用户**：网关差异（`get_forward_msg` 只认 message_id、`get_image` 返回远端 URL 等）插件都已适配，两套网关直接可用。

## ⚡ 三分钟上手

```
@bot 今天群里都聊啥了           → 查记忆与话题，用人话总结
@bot 把刚才那几条离谱发言挂出来   → 搜记录 → 合并转发/聊天卡片 → "挂人（"
@bot 画个服务器状态图            → draw_picture 出图（自动抓真实指标）
@bot 帮我把这份 PDF 读一下       → 引用文件消息说话，它读原文再回答
@bot 在服务器上看看磁盘          → SSH 工具直连你的远程机
@bot 晚上十点提醒我发总结        → 排进任务队列，到点自己执行
@bot 把这个流程做成技能          → 复盘 → 固化成技能 → 热加载可复用
```

私聊它也认得你——记忆跨会话共享并带出处，A 群聊过的梗在私聊里可以自然引用。

## 💻 控制台

插件自带 WebUI 管理台（AstrBot 插件页进入）：

- **概览**：群与白名单、消息/摘要/账本计数、模型用量（7 天）、自学习技能数。
- **记忆馆**：分层摘要、记忆账本、全文检索、群话题、人物印象与好感度、说话风格档案。
- **记忆森林**：整棵记忆树的节点图——圆形节点 + 边连线，悬停看详情，点击编辑/删除/新增子节点，滚轮缩放、拖拽平移，懒加载展开。
- **人物**：跨群合并的人物印象、昵称与好感度。
- **日程与任务**：任务表增删改查——加一条短期/长期/区间任务当场生效。
- **能力与拓展 / 配置 / 运维**：技能与拓展管理、全部配置在线改（改完即时生效，不用重启）、清空与维护。

## 📑 配置速查

<details>
<summary><b>最常用的 20 项</b></summary>

| 配置 | 默认 | 说明 |
| :--- | :--- | :--- |
| `group_whitelist` | `[]` | 完整记忆与自主聊天的群号 |
| `judge_provider_id` / `reply_provider_id` | 空 | 判定模型 / 回复模型（留空用 AstrBot 当前模型） |
| `persona_id` | 空 | 使用哪个人格（AstrBot 人格页可见） |
| `autonomous_sample_rate` | `0.35` | 普通消息进入判定的概率（越高越爱接话） |
| `wake_merge_seconds` | `4.0` | 连发 @ 的合并窗口（0=逐条即回） |
| `wake_merge_max_seconds` | `15.0` | 合并上限：再连发也最多等这么久 |
| `batch_window_min/max_seconds` | `15` / `60` | 普通消息攒批窗口 |
| `group_cooldown_seconds` | `180` | 同群自主互动冷却 |
| `max_actions_per_hour` | `8` | 每群每小时自主动作上限 |
| `quiet_start_hour` / `quiet_end_hour` | `1` / `7` | 夜间静默 |
| `proactive_interval_minutes` | `15` | 短巡查：通知/话题/私聊（0=关） |
| `self_reflect_interval_hours` | `6` | 长打理：总结/说说/资料（0=关） |
| `heartbeat_interval_seconds` | `120` | 心跳：执行日程/闲时消遣/潜水看群 |
| `task_queue_enabled` | `true` | 统一任务队列（关掉则只剩事件驱动） |
| `sticker_learning` | `true` | 学习群里的表情包并自主使用 |
| `enable_qzone` / `enable_qq_tools` | `true` | QQ 空间 / QQ 工具自主权 |
| `enable_media_archive` | `true` | 归档群内媒体（图/文件/语音/视频） |
| `agent_max_steps` | `40` | Agent 工具循环最大步数 |
| `subagent_enabled` | `true` | 执行型子 agent（除浏览器外全工具） |
| `decision_subagent_enabled` | `true` | 决策层需求梳理子 agent |

其余 60+ 项（进化代数、拓展、浏览器、工作区、程序端口…）在控制台或 `_conf_schema.json` 里都能看到，每项都有中文说明。

</details>

> [!TIP]
> **面板保存小坑**：数字字段别留空——AstrBot 保存配置是"全或无"校验，空值会让整次保存被拒。
> Sparking 支持**配置热刷新**，面板改完即使没重载也立刻生效。

## ❓ 常见问题

<details>
<summary><b>它不回我 / 回得太少</b></summary>

先看 `autonomous_sample_rate`（普通消息采样率）与冷却项；@ 与私聊不受采样限制。
日志会写明"未采样进入判定""处于冷却"这类原因，按提示调即可。
</details>

<details>
<summary><b>连发几条 @ 它只回了一次</b></summary>

这是**故意的**——合并窗口把连发消息并成一次回复，像真人一起读完再开口。
想恢复逐条即回，把 `wake_merge_seconds` 设为 0。
</details>

<details>
<summary><b>合并转发 / 聊天记录读不出来</b></summary>

QQ 只在服务端缓存转发资源一小段时间。Sparking 在**消息到达时**就展开并入库，
之后读取优先命中库里的内容（"能读出来就一定读得出来"），现场拉取还会多候选 id 重试。
只有真的没缓存过才会如实说读不到——不会拿"过期"当借口。
</details>

<details>
<summary><b>它说"没法连服务器 / 没给我账号"</b></summary>

旧版本的问题。现在 SSH 就绪时会主动告诉模型"连接信息在你手里"，
配好 `ssh_host/user/password` 后它应该直接动手。
</details>

<details>
<summary><b>它把内部思考 / JSON 发到群里了</b></summary>

发送层已做三重拦截（计划 JSON 泄漏、心声防火墙、失控重复检测）。
若仍遇到，请带日志反馈——这类问题一律按插件缺陷处理。
</details>

## 🧩 拓展与技能

- **手写拓展**：在插件目录 `scripts/<id>/` 放 manifest + hooks 即可，接口契约见 [docs/SCRIPT_EXT_API.md](docs/SCRIPT_EXT_API.md)——**只增不删不改签名**，插件升级不用跟着改。
- **自学习技能**：模型复盘自己的工作，把可复用流程固化成技能写入数据目录并热加载；`learn_skill` / `update_skill` 可当场增改。
- **能力缺口自己补**：需要反复做的事（远程拉文件、批量处理…）它会写带 `api.ssh_exec` / `api.media_from_remote` / `api.save_image` 的拓展自己解决。

## 📂 目录结构

```
astrbot_plugin_long_memory_agent/   # 插件包名（保持不变，避免破坏既有安装与数据目录）
├── main.py                 # 主体：事件链路、Agent、工具、三层循环
├── src/
│   ├── storage.py          # SQLite 分层存储（消息/摘要/账本/印象/情绪/用量…）
│   ├── retrieval.py        # 检索：FTS5 全文 + 可选 embedding
│   ├── context_builder.py  # 有界长上下文组装
│   ├── task_queue.py       # 统一任务队列（一切皆任务 + 窗口 + 退避重试）
│   ├── decision.py         # 判定器（是否接话 / 要不要先梳理需求）
│   ├── humanization.py     # 拟人发送：拆句、打字延迟、心声防火墙、计划校验
│   ├── emotions.py         # 效价/唤醒情绪轴 + 表达温度
│   ├── compression.py      # L1/L2/L3 分层压缩
│   ├── prompts.py          # 全部提示词（语言铁律、群生存行为…）
│   ├── webui.py            # 控制台后端（读路由 + 写动作）
│   ├── chat_card.py        # 聊天卡片渲染（伪截图）
│   ├── qq_actions.py / qq_gateway.py   # QQ 全托管动作表与网关
│   ├── workspace.py        # 受限工作区（文件增删改查 + 跑脚本）
│   ├── script_ext.py       # 万能拓展接口（长期兼容契约）
│   ├── self_learning.py    # 体验复盘 → 技能固化
│   ├── evolution.py        # GEPA 提示词进化
│   └── ssh_ext.py          # SSH 远程运维
├── pages/
│   ├── memory/             # 控制台前端（含记忆森林渲染器）
│   └── tree/               # 记忆森林独立页
├── docs/                   # 开发/移植文档、人格语料手册
├── scripts/                # 你的手写拓展放这里
└── logo.png                # 插件图标
```

## 📄 作者

**Rikka0612** · 仓库：[github.com/Rikka0612/Sparking](https://github.com/Rikka0612/Sparking)

本项目为个人自用向插件，代码与文档持续迭代；使用前请自行评估风险并做好数据备份。

<div align="center">

**如果 Sparking 让你的 Bot 更像"一个人"，欢迎 Star ⭐ / 提 Issue 一起打磨。**

</div>
