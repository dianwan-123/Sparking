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

简体中文

</div>

---

> [!IMPORTANT]
> Sparking 面向 **AstrBot 4.27+ / aiocqhttp / OneBot v11（NapCat、SnowLuma）**。
> 装上并填好 `group_whitelist` 之后，它会接管这些群的完整消息：进长期记忆、按真人节奏接话、按需自主行动。
>
> **这是我第一次做插件**，很多地方还在打磨；遇到问题、觉得不好用、想要什么功能，都欢迎开 Issue 告诉我。
> 插件交流群 **1121848707**（[点这里加群](https://qm.qq.com/q/t8ogK51vwW)），进来一起聊也行。

## 🌟 它是什么

- **它记得**——你指定群里的每条消息都存下来，按分钟/天/月分层归纳，之后还能顺着原话、日期、链接回查。
- **它有情绪**——情绪随聊天自然起伏、慢慢回归平静，回话的语气和长度也跟着变。
- **它会主动**——不用 @：闲时会自己看群、接话、吐槽、打理心情和资料，也能按你排的日程自己干活。
- **它会干活**——它可以调用 **AstrBot 工具、AstrBot 插件工具和自带拓展**：画画、跑代码、读 PDF、做网页小程序、把聊天记录做成卡片图或合并转发。
- **它分得清场合**——谁说的话只算谁的，旧任务不会硬接新消息，内部思考绝不会漏到聊天里。

## 🎭 像真人的地方

- **真聊天**——短句连发、带语气词和半括号，按打字节奏发，不秒回也不刷屏。
- **戳一戳**——被戳会戳回去，也会主动去戳熟人（可在配置里关）。
- **看图、发表情包**——看得懂群里发的图，会自己发表情包，还会学着用群里的梗图。
- **合并聊天记录**——读得懂别人转发的聊天记录（嵌套转发也能一层层展开），也能把消息打包成合并转发发出去。
- **小程序卡片**——群里的分享卡片、小程序消息它也看得懂。
- **记忆跨群**——同一个人的印象跨群通用，A 群聊过的梗，在私聊和 B 群也能自然接上（并且会如实说"那是在别处发生的"）。
- **自己找事做**——主动找你说话、逛空间、发说说、把离谱发言挂出来……不用你叫。

## 🚀 安装

1. 从 Releases 下载 zip，在 AstrBot WebUI 的「插件」页上传安装（或把插件目录放进 `data/plugins/`）。
2. 到插件**配置页**至少填两项：
   - `group_whitelist`：允许 Sparking 完整记忆与自主聊天的群号；
   - 要 Agent 能力就填 `judge_provider_id` / `reply_provider_id`（留空则用 AstrBot 当前模型）。
3. 可选依赖按需**自动安装**：`playwright`（截图/浏览器）、`pypdf`（读 PDF）、`pandas`/`matplotlib`（表格与画图）、`flask`（小程序）等——不写进 `requirements.txt`，免得和 AstrBot 核心依赖打架。

> [!TIP]
> **NapCat / SnowLuma 用户**：两个网关的差异插件内部都适配好了，直接可用。

## ⚡ 三分钟上手

装上之后，把它当群里一个真人使唤就行：

```
@bot 今天群里都聊啥了             → 查记忆与话题，用人话总结
@bot 把刚才那几条离谱发言挂出来    → 搜记录 → 合并转发/聊天卡片 → "挂人（"
@bot 画个服务器状态图             → 出图（服务器状态图会自动抓真实指标）
@bot 帮我把这份 PDF 读一下        → 引用文件消息说话，它读原文再回答
@bot 晚上十点提醒我发总结         → 排进任务队列，到点自己执行
@bot 把这个流程做成技能           → 复盘 → 固化成技能 → 之后自动复用
```

私聊它也认得你——记忆跨会话共享，A 群聊过的梗在私聊里能自然接上；同一个人在不同群里也只会有一份印象。

## 💻 控制台

插件自带管理台（AstrBot 插件页进去），左侧一栏就是全部功能：

- **概览**：运行状态、给 bot 直接下令（一句话让它带工具去做）、近 7 天用量。
- **记忆**：消息浏览（可搜关键词）、分层摘要、记忆账本，都能逐条删。
- **记忆森林**：整棵记忆树画成节点图——**圆形节点＝一条记忆，连线＝关系**。鼠标悬停看详情（类型、时间、粒度、内容），左键点节点出菜单（编辑／删除／加子节点／展开），双击展开或收拢，滚轮缩放、拖拽平移，上面还能按标题过滤。
- **人物**：跨群合并的人物印象（同一个人只有一份）、好感度、说话风格档案。
- **群与会话**：会话列表、白名单在线改、群话题。
- **日程与任务**：任务队列（短期跑几次、长期定时、限定时间段都行）、待办、"当有人提到 X 就做 Y"的立指令。
- **能力与拓展**：bot 实际能调用的全部工具（AstrBot 内置、其它插件、本插件）与已装载技能。
- **配置**：所有配置项在线改，**改完即时生效**，不用重载插件。
- **运维**：备份、压缩记忆、清空各类数据、改情绪、查看好友申请/群邀请等事件。

## 📑 常用配置

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
| `heartbeat_interval_seconds` | `120` | 心跳：执行日程/闲时消遣/潜水看群（0=关） |
| `task_queue_enabled` | `true` | 任务队列常驻执行器（一切皆任务） |
| `sticker_learning` | `true` | 学习群里的表情包并自主使用 |
| `enable_qzone` / `enable_qq_tools` | `true` | QQ 空间 / QQ 工具自主权 |
| `enable_media_archive` | `true` | 归档群内图片/文件/语音/视频 |
| `agent_max_steps` | `40` | Agent 工具循环最大步数 |
| `subagent_enabled` | `true` | 子 agent（主 agent 可并行派活） |
| `decision_subagent_enabled` | `true` | 决策层需求梳理子 agent |

其余 60+ 项在控制台的「配置」面板里逐条有中文说明。

</details>

> [!TIP]
> **面板保存小坑**：数字字段别留空——AstrBot 保存配置是"全或无"校验，空值会让整次保存被拒。
> 改完不用重启：Sparking 有配置热刷新，面板保存立即生效。

## ❓ 常见问题

<details>
<summary><b>它不回我 / 回得太少</b></summary>

先调 `autonomous_sample_rate`（普通消息采样率），再看冷却项；@ 与私聊不受采样限制。
日志会写明"未采样进入判定""处于冷却"这类原因，按提示调即可。
</details>

<details>
<summary><b>连发几条 @ 它只回了一次</b></summary>

这是**故意的**——连发消息会并成一次回复，像真人一起读完再开口。
想恢复逐条即回，把 `wake_merge_seconds` 设为 0。
</details>

<details>
<summary><b>合并转发 / 聊天记录读不出来</b></summary>

QQ 只在服务端缓存转发资源一小段时间。Sparking 在**消息到达时**就把内容展开存进库，
之后读取优先命中本地记录；只有真的没缓存过才会如实说读不到，不会拿"过期"当借口。
</details>

<details>
<summary><b>它把内部思考 / JSON 发到群里了</b></summary>

发送层做了多重拦截（计划 JSON 泄漏、心声、失控重复）。若仍遇到，请带日志开 Issue——这类问题一律按插件缺陷处理。
</details>

## 🧪 实验性功能（接口可能随版本调整）

- SSH 远程运维
- 手写拓展（scripts/）

## 🗺️ 未来计划

- SSH 运维与 computer use（让它真能操作电脑）
- 多消息平台的接入与互通
- 与本地 agent harness 互通
- 多 bot 平台适配（NapCat 等）

## 📄 说明

- 本项目为个人自用向插件，代码与文档持续迭代；使用前请自行评估风险并做好数据备份。
- **这是我第一次做插件**，bug 和不好用的地方在所难免——欢迎提 Issue，我会尽量修。

<div align="center">

**插件交流群：1121848707** · [点这里加群](https://qm.qq.com/q/t8ogK51vwW)

**如果 Sparking 让你的 Bot 更像"一个人"，欢迎 Star ⭐ / 提 Issue 一起打磨。**

</div>
