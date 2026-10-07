# Hermes 移植功能对照（hermes-agent 25.1万星 + hermes-agent-self-evolution）

> 调研对象：NousResearch/hermes-agent（"The agent that grows with you"）与
> hermes-agent-self-evolution（DSPy+GEPA 进化优化）。源码已存 `.refs/hermes/`
> 供后续查阅。按"已有对应 / 本次移植 / 不适用"归档。

| Hermes 能力 | 本插件对应 | 状态 |
| --- | --- | --- |
| SOUL.md（恒定身份/语气文件，每轮注入） | persona_id + REPLY_SYSTEM_PROMPT | 已有（等价） |
| MemoryManager（记忆 hooks 扇出） | Storage + IngestService 单一后端 | 已有（形态不同） |
| hook_output_spill（超大工具结果落盘防溢出） | compact_json + context_char_budget | 已有（简化版） |
| **repetition_guard（失控重复检测）** | —— | **本次移植（v0.30）** |
| **查证自白过滤**（"你要找的X不就是你本人"） | —— | **本次移植（v0.30，心声防火墙扩展）** |
| **todo 任务表（多步工作清单，压缩后重注入）** | —— | **本次移植（v0.33）** |
| **code_execution RPC（脚本里调用工具，流水线零上下文成本）** | python_exec（纯计算） | **本次移植（v0.33，沙箱 bot.* 同步接口）** |
| **经验→技能自学习（creates skills from experience；skills self-improve during use）** | scripts 拓展（手写） + GEPA（只进化提示词） | **本次移植（v0.33，体验复盘→固化→热加载→update_skill 修补）** |
| pet（活动状态→吉祥物动画） | 无 UI | 不适用 |
| cron delivery queue | TaskScheduler + agent_plans | 已有 |
| Anthropic thinking policy | — | 不适用（由 AstrBot 管理） |
| skill 进化（GEPA） | GEPA 引擎（src/evolution.py，v0.31） | 已有 |

## 本次移植细节

### 1. 失控重复检测（is_runaway_repetition）
Hermes 实录：模型陷入退化循环会把整个输出预算花在复读同一段（60k 字符一轮、
31 条 Discord 消息）。移植其保守判据：
- 只对 ≥400 字符的长片段生效（短截断里的重复是正常续写）；
- 行级：同一行复读 ≥5 次且占片段多数，且有行结构时非空行至多一半不同；
- 窗口级：60+ 字符的窗口在均匀锚点采样（≤32 个）中重复占多数。
命中即整体丢弃回复（PlanValidationError），触发重试/兜底——绝不把循环复读发给用户。

### 2. 查证自白过滤（心声防火墙扩展）
实录泄漏："你要找的sen不就是你本人（"——bot 把"我查了聊天记录才找到"的过程说了出来。
新增检测：`你要找的X不就是你` / `我翻/查/搜了记录记忆档案` / `根据记录搜索` / `不就是你本人`。
提示词同步（MEMORY_TRUST_POLICY + REPLY 第6/8条）：**以记忆为据 = 直接说出记得的内容，
绝不解释查证过程**——记忆在你脑子里，不是需要翻的档案。

### 3. todo 任务表（v0.33）
- 表 `agent_todos`（scope + id + content + status + position）；工具 `todo(action=add/list/update/clear)`。
- 未完成项（pending/in_progress）每轮注入判定/回复上下文（`active_todos`）——即 hermes
  "压缩后重新注入"的语义，跨轮、跨压缩不忘。
- 调度通道同样支持（自主循环里也能用）。

### 4. code_execution RPC（v0.33）
- `python_exec` 沙箱注入同步 `bot` 对象：`bot.web_search/fetch/kb_search/search_memory/
  recent_messages/send_group/send_private/say/now/sleep`——一段脚本跑完"搜→筛→发"流水线，
  一次工具调用顶过去一串（hermes 的 RPC 脚本思路；实现为 `run_coroutine_threadsafe`
  把协程送回主事件循环）。发送仍受群白名单约束。

### 5. 经验→技能自学习（v0.33）
- `src/self_learning.py` + `ScriptExtensionManager.create_extension`（learned root 热加载）：
  1) 够重的会话工作结束（耗时 ≥ 阈值，默认 90s）→ 静默 20s 后**体验复盘**：复盘器读
     真实工作记录（+已有技能清单），只在"可复用流程/用户纠正/反复出现的请求模式"时产出；
  2) 技能 = scripts 拓展形态（提示词 + 可选工具与代码），校验（id/长度/可编译）后写进
     数据目录 `learned_skills/` 并**热加载**（插件升级不丢、与手写 scripts/ 隔离、不能覆盖内置）；
  3) 前台发现技能不好用时 `update_skill` 当回合修补（hermes "skills self-improve during use"
     与 openclaw "immediate repair" 的合并实现）；
  4) 复盘固化会写一条 [技能学习] 留痕进记忆；配置 `self_learning_enabled`（默认开）/
     `self_learning_min_seconds`。
