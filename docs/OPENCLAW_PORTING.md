# OpenClaw（2026.9.8）agent 功能对照与移植笔记

> 调研对象：OpenClaw v2026.9.8（npm 包源码，2026-10 从 npmmirror 镜像下载并通读
> `docs/` 与 agent runtime 架构文档后对照本项目）。结论按"已有对应 / 本次移植 /
> 不适用"三类归档，作为后续演进的地图。源码本身未保留在仓库（可随时
> `https://registry.npmmirror.com/openclaw/-/openclaw-2026.9.8.tgz` 重新下载）。

## 一、逐项对照

| OpenClaw 能力 | 本插件对应 | 状态 |
| --- | --- | --- |
| Agent loop（多轮工具循环） | AstrBot `tool_loop_agent` + 判定/回复两段管线 | 已有 |
| Compaction（上下文压缩） | L1/L2/L3 分层压缩 + memory ledger | 已有（且跨会话共享带 origin） |
| Active memory（深度回忆升级） | 检索 facade（混合检索：FTS+embedding+RRF） | 已有 |
| Memory（SQLite 记忆库） | memory.db 全量事实层 | 已有 |
| Subagents（只读分析子代理） | `dispatch_subagent` / `dispatch_parallel_subagents`（可带图） | 已有（v0.27 起默认开） |
| Cron（定时任务） | TaskScheduler + agent_plans 自主日程 | 已有 |
| Heartbeat（心跳循环） | `_heartbeat_loop`（日程/闲时/潜水） | 已有 |
| Browser use（无头浏览器） | PlaywrightDriver + 零依赖引擎双内核 + 滑块/点选验证 | 已有 |
| Web search | `web_search` / `web_fetch` / browse 全家桶 | 已有 |
| Skills（文件夹技能热加载） | `scripts/` 万能拓展接口（v0.26，契约见 docs/SCRIPT_EXT_API.md） | 已有（对等设计） |
| Canvas（agent 自绘 UI） | designer（HTML/SVG 渲染窗口）+ programmer（Flask 小程序） | 已有（对等设计） |
| Media understanding（图片转述入库） | `_ingest_image_notes` 视觉转述链路 | 已有 |
| PDF 阅读 | `read_document`（pypdf，v0.27） | 已有 |
| exec/代码执行 | `python_exec` | 已有 |
| **Standing intents（事件条件意图）** | —— | **本次移植（v0.28）** |
| **Standing orders（常备指令，每轮注入的行动授权）** | —— | **本次移植（v0.28）** |
| 合并转发消息展开 | —— | **本次移植（v0.28）** |
| Code 展示（渲染给用户） | `render_code`（VSCode Dark+ 风格高亮图） | **本次移植（v0.28）** |
| **Self-learning / Skill Workshop（经验复盘→技能；即时修补）** | scripts 拓展（手写） | **本次移植（v0.33，learned root 热加载 + update_skill）** |
| **Tool-loop detection（重复工具调用护栏）** | 失控重复检测（只查文本复读） | **本次补齐（v0.33，自主循环动作级：同动作同参 ≥3 阻止、≥5 强制收尾）** |
| **Queue steering（任务中途新消息=新指示）** | —— | **本次移植（v0.33，自主循环逐轮带 new_messages_since_start）** |
| **Usage tracking（用量统计）** | —— | **本次移植（v0.33，usage_stats 表 + WebUI）** |
| **ask-user / clarify（中途提问等回复）** | say_now（过程播报） | **本次移植（v0.33，ask_user 提问并停下等人）** |
| **code-mode / code-execution（一段代码串多步工具）** | python_exec（纯计算） | **本次移植（v0.33，沙箱 bot.* RPC 流水线）** |
| 多渠道 gateway（WhatsApp/TG/Discord…） | 仅 aiocqhttp/NapCat（本项目定位） | 不适用 |
| Nodes（采集用户设备摄像头/屏幕） | QQ 场景无对应 | 不适用 |
| Control UI（本地 Web 控制台） | 插件 WebUI（memory 页，含用量/任务/技能） | 已有（形态不同） |
| Multi-agent（多 agent 实例/模型路由） | 单 agent + 可配多 provider + 子代理 | 部分覆盖 |
| Dreaming（记忆自省重组） | 反思循环（summary/mood/impressions） | 部分覆盖 |
| Model failover | `_llm_text` 5 次重试 + 空输出重试 + 候选链 | 部分覆盖 |
| exec-approvals（危险操作审批） | 群白名单 + 管理权限校验 | 部分覆盖（无交互式审批） |

## 二、本次移植的实现要点

### 1. Standing Intents（事件条件意图）
- 表 `standing_intents`：instruction + 触发关键词（≤8 个）+ 次数预算（-1=无限）
  + 冷却秒数；命中后自动扣预算、耗尽自动 `spent`。
- 工具 `manage_intent(action=add/list/remove)`；命中消息时插件把
  `standing_intents_hit` 注入判定/回复上下文（每回合最多 3 条），bot 按指令行动。
- 判定上下文命中即 `tech_enriched` 同级强制序列化——不会因消息是普通闲聊而被丢弃。

### 2. Standing Orders（常备指令）
- 配置 `standing_orders`（text）：主人写给 bot 的**长期行动授权**，每轮回复与
  自主行动系统提示词都注入（【常备指令】段），是 cron/意图之外"永久政策"层。

### 3. 合并转发展开
- 消息带 `forward` 段时：判定路径同步 `get_forward_msg`（id+message_id 双参数）
  展开节点 → `forward_content` 注入上下文（谁说的：内容，≤40 节点）；
- 同时异步落一条 `notice.forward` 派生消息进**同一 scope**（必须用 raw 的
  self_id/group_id 还原 account/conversation，防止孤儿 scope）。

### 4. render_code
- `src/code_render.py` 手写 tokenizer（零依赖零外链，离线可渲染）：
  关键词/字符串/注释/数字/装饰器/函数/类着色，VS Code Dark+ 配色 + 窗口框 +
  行号，单文件 400 行封顶；经 designer 截图管线出 media_id。

## 三、后续可选（未做）

- Subagent 带**完整工具循环**（OpenClaw 的 delegate 是带工具的会话；本插件子代理
  是单次调用——升级需要租约+审计，成本较高）。
- Multi-agent：按群绑定不同 persona/模型的"agent bindings"。
- 交互式 exec 审批（exec-approvals 的完整形态：危险操作在群里等管理员确认）。
- Session search 语义化（跨会话"上次聊到什么"检索已靠 L1 共享解决大半）。

## 四、v0.33 本次移植的细节

1. **Self-learning（Skill Workshop 等价物）**：见 `src/self_learning.py` 与
   `tests/test_agent_growth.py`。技能载体 = scripts 拓展（提示词+工具+可选代码），
   写进数据目录 `learned_skills/`（与手写 scripts/ 隔离、内置不可覆盖、插件升级不丢），
   `create_extension` 热加载；前台 `learn_skill` / `update_skill` 工具 + 后台体验复盘
   （耗时阈值 + 20s 静默 + 每 scope 10 分钟节流）。
2. **Tool-loop detection**：`_autonomous_action_loop` 里对 (tool, args) 计数——
   第 3 次相同调用直接阻止并把提示塞回 payload（`loop_warning`），第 5 次强制收尾。
   AstrBot 托管的回复 agent 循环无法拦截，靠既有重复检测/超时兜底。
3. **Queue steering**：自主循环每轮取任务开始后群里的新消息（`new_messages_since_start`）
   + steering_note，让 agent 中途改道，不再做已经过时的步骤。
4. **Usage tracking**：`usage_stats` 表（按天+scope 聚合 calls/prompt_tokens/
   completion_tokens），`_llm_text` 每次成功调用后记录（raw_completion.usage）；
   WebUI 概览可见。
5. **ask_user**：发一句短问句到当前会话并明确"停下等人"，补上 clarify 的 QQ 版。
6. **code-mode 轻量版**：python_exec 沙箱的 `bot.*` 同步接口包（web_search/fetch/
   kb_search/search_memory/recent_messages/send_group/send_private/say），
   让一段脚本跑完多步流水线——对应 OpenClaw 的 code mode 思路、hermes 的
   "write Python scripts that call tools via RPC, collapsing multi-step pipelines"。

> 源码位置：`.refs/hermes/hermes-agent-main/`（保留）与 `.refs/openclaw/package/`
> （v2026.9.8，506MB，随时可删，均已排除打包）。
