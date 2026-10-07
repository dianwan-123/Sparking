/* 本地预览用假桥接（开发件，不进包、不进 git）。
   用法：起个静态服务（python -m http.server 8877）打开
   http://127.0.0.1:8877/pages/memory/index.html ，控制台执行：
   import('/tools/webui_mock.js')  —— 或直接把本文件粘进 DevTools 控制台。
   然后在页面里点左侧任一菜单，就会用这份假数据渲染。 */
(() => {
  const leaf = (id, label, hint, time, gran) => ({
    id, type: "catalog", label, hint, time, time_granularity: gran, kind: "fact", editable: true,
  });
  const cat = (id, label, count) => ({ id, type: "category", label, count });

  const scopeChildren = [
    cat("cat:sum", "分层摘要", 3), cat("cat:cat", "记忆账本", 3),
    cat("cat:imp", "人物印象", 2), cat("cat:top", "群话题", 2), cat("cat:msg", "证据消息", 1),
  ];
  const roots = [
    { id: "scope:123456789", type: "scope", label: "数学指令讨论群", count: 5, time: "2026-10-07 14:22", time_granularity: "分钟" },
    { id: "scope:987654321", type: "scope", label: "BE指令饮品店", count: 3, time: "2026-10-07 12:10", time_granularity: "分钟" },
    { id: "scope:556677", type: "scope", label: "私聊 · 小明", count: 2, time: "2026-10-07 08:30", time_granularity: "分钟" },
  ];
  const byNode = {
    "scope:123456789": { children: scopeChildren },
    "scope:987654321": { children: scopeChildren.slice(0, 3) },
    "scope:556677": { children: scopeChildren.slice(1, 3) },
    "cat:sum": { children: [
      { id: "s1", type: "summary", label: "L1 · 14:20", hint: "有人在调盔甲架旋转，讨论了计分板定点整数", time: "2026-10-07 14:20", time_granularity: "分钟" },
      { id: "s2", type: "summary", label: "L2 · 10-07", hint: "一天主线：指令答疑 + 音游凹分 + 求管理发癫", time: "2026-10-07", time_granularity: "天" },
      { id: "s3", type: "summary", label: "L3 · 2026-10", hint: "本月主题：MCBE 指令自动化、开学吐槽", time: "2026-10", time_granularity: "月" },
    ] },
    "cat:cat": { children: [
      leaf("m1", "小明喜欢布偶猫", "他家里养了两只布偶，一只叫团子", "2026-10-05 21:12", "分钟"),
      leaf("m2", "周五要交推导作业", "约定周五前把旋转矩阵推导补完", "2026-10-07 09:40", "分钟"),
      leaf("m3", "奶昔在准备音游比赛", "提到要凹变速谱，别打扰", "2 天前", "相对"),
    ] },
    "cat:imp": { children: [
      { id: "i1", type: "impression", label: "小明 · 印象", hint: "指令很强但总装萌新，喜欢反串（跨群合并）", time: "3 小时前", time_granularity: "相对" },
      { id: "i2", type: "impression", label: "奶昔 · 印象", hint: "音游玩家，说话带喵，爱传播知识", time: "昨天", time_granularity: "相对" },
    ] },
    "cat:top": { children: [
      { id: "t1", type: "topic", label: "盔甲架旋转", hint: "rotated ~ 0 锁死仰角；计分板放大做定点", time: "2026-10-07 14:05", time_granularity: "分钟" },
      { id: "t2", type: "topic", label: "求管理发癫", hint: "演到刷屏 1 2 3 4 被怼「？别发癫」", time: "2026-10-06 23:40", time_granularity: "分钟" },
    ] },
    "cat:msg": { children: [leaf("e1", "小明：那不是万象天引吗？", "", "2026-10-07 14:11", "分钟")] },
  };

  const data = {
    overview: {
      enabled: true, version: "1.0.2",
      counts: { scopes: 3, messages: 17844, summaries: 126, memories: 412, catalog: 88, jobs: 5 },
      models: { judge: "mimo-v2.6-flash", reply: "mimo-v2.6-flash", summary: "deepseek-v3.2", persona: "小铃" },
      groups: ["123456789", "987654321"], mood: { mood: "雀跃", intensity: 0.62 },
      scripts: { custom: 2, learned: 7 },
      usage: {
        // 与后端 storage.usage_summary 的真实形状一致（totals 对象 + daily 数组）
        daily: [{ day: "2026-10-01", calls: 120, prompt_tokens: 82000, completion_tokens: 14000 },
                { day: "2026-10-02", calls: 210, prompt_tokens: 143000, completion_tokens: 21000 },
                { day: "2026-10-03", calls: 180, prompt_tokens: 121000, completion_tokens: 18000 },
                { day: "2026-10-04", calls: 150, prompt_tokens: 98000, completion_tokens: 15000 },
                { day: "2026-10-05", calls: 300, prompt_tokens: 205000, completion_tokens: 33000 },
                { day: "2026-10-06", calls: 220, prompt_tokens: 151000, completion_tokens: 24000 },
                { day: "2026-10-07", calls: 104, prompt_tokens: 71000, completion_tokens: 11000 }],
        totals: { calls: 1284, prompt_tokens: 871000, completion_tokens: 136000 },
      },
    },
    scopes: [
      { group_id: "123456789", display_name: "数学指令讨论群", messages: 9821 },
      { group_id: "987654321", display_name: "BE指令饮品店", messages: 6223 },
      { group_id: "556677", display_name: "私聊 · 小明", messages: 1800 },
    ],
    messages: { messages: [
      { message_id: 1, sender_name: "小明", text: "rotated ~ 0 锁死仰角不就行了", occurred_at: "2026-10-07 14:11:02" },
      { message_id: 2, sender_name: "奶昔", text: "大佬喵", occurred_at: "2026-10-07 14:11:40" },
    ], next_cursor: null },
    summaries: [{ id: "s1", layer: "L1", text: "有人在调盔甲架旋转", evidence: "message_id 1", created_at: "2026-10-07 14:20" }],
    catalog: [{ id: "m1", subject: "小明喜欢布偶猫", fact: "家里两只布偶", evidence: "id 1", created_at: "2026-10-05" }],
    impressions: [
      { user_id: "10001", display_name: "小明", impression: "指令很强但总装萌新，喜欢反串。", tags: ["大佬", "反串"], groups: ["数学指令讨论群", "BE指令饮品店"] },
      { user_id: "10002", display_name: "奶昔", impression: "音游玩家，说话带喵，热心。", tags: ["音游", "喵"], groups: ["数学指令讨论群"] },
    ],
    affinity: [{ user_id: "10001", display_name: "小明", warmth: 72, interactions: 34, note: "一起调过指令" }],
    styles: [{ user_id: "10001", display_name: "小明", msg_count: 320, style_summary: "短句、反问多、爱半括号", catchphrases: ["这不…吗", "建议重修（"] }],
    topics: [{ topic: "盔甲架旋转", summary: "rotated ~ 0 锁死仰角", hits: 12 }],
    tasks: [{ task_id: "t1", kind: "agent", title: "总结本周群聊并写进记忆", detail: "周日 22:00", status: "pending",
              next_run_at: "2026-10-11 22:00", runs_done: 0, max_runs: -1, interval_seconds: 604800, priority: 5, source: "bot" }],
    todos: [{ id: "td1", content: "把旋转矩阵推导补完", created_at: "2026-10-07 09:41" }],
    intents: [{ intent_id: "i1", text: "有人提到「凹分」就去问进度", keywords: ["凹分"], hits: 3 }],
    capabilities: [
      { name: "draw_picture", description: "一条龙出图（服务器状态图自动抓真指标）", source: "Sparking", enabled: true },
      { name: "forward_messages", description: "单条转发 / 多条合并转发", source: "Sparking", enabled: true },
      { name: "web_search", description: "联网搜索", source: "AstrBot", enabled: true },
      { name: "learn_skill", description: "把可复用流程固化成技能", source: "Sparking", enabled: false },
    ],
    extensions: [{ id: "demo", name: "示例拓展", tools: 2, enabled: true, origin: "builtin" }],
    config: [[{ key: "group_whitelist", value: "123456789,987654321", description: "完整记忆与自主聊天的群号" },
              { key: "wake_merge_seconds", value: "4.0", description: "连发 @ 的合并窗口（0=逐条即回）" }]],
    backups: [{ name: "sparking-2026-10-07.db", size: 288000, created_at: "2026-10-07 03:00" }],
    events: [{ kind: "friend_request", text: "小明 请求加好友", created_at: "2026-10-07 10:00", handled: true }],
    mood: { mood: "雀跃", intensity: 0.62, note: "群里在夸它" },
    imports: {
      note: "支持 QQChatExporter V5 导出的 chunked-jsonl：zip 根目录下每个文件夹是一个群。不在白名单里的群也能导入，只是不会建人物印象。",
      scopes: [
        { group_id: "1095747640", name: "数学指令讨论群", messages: 8720, last_active: "2026-09-12T11:00:26+00:00", whitelisted: true },
        { group_id: "1046398077", name: "BE指令饮品店☕️", messages: 10422, last_active: "2026-09-12T10:58:02+00:00", whitelisted: false },
      ],
    },
    backup_memory: {
      current_embedding_model: "bge-m3",
      counts: { scopes: 3, messages: 17844, summaries: 126, memories: 412, catalog: 88, embeddings: 2931 },
      exports: [
        { name: "sparking-memory-20261007T143012Z.zip", size: 4_182_000 },
        { name: "sparking-memory-20261005T090000Z.zip", size: 3_960_412 },
      ],
      last: { name: "sparking-memory-20261007T143012Z.zip" },
      note: "导出的包可以在别的 bot 上导入（会话、消息、分层摘要、记忆账本、人物印象、群文化、情绪记忆、注入与日程）。重新导入时要匹配同一个嵌入模型：对不上就只导文本与记忆、跳过向量，面板会写明。",
    },
    culture: {
      scope: "123456789",
      note: "说话风格与黑话是按群学的；人物档案与情绪记忆跨群共用一份。",
      style_rules: [
        { rule_id: "r1", situation: "有人发癫", style: "用 又在这串", hits: 7 },
        { rule_id: "r2", situation: "被夸", style: "用 太强了", hits: 3 },
      ],
      lexicon: [
        { term_id: "l1", term: "蚌埠住", meaning: "绷不住，笑到控制不住", uses: 5 },
        { term_id: "l2", term: "沙子", meaning: "", uses: 1 },
      ],
      profiles: [
        { person_id: "p1", user_id: "10001", display_name: "小明", know_counts: 34,
          points: ["身份:高二学生:5", "喜好:爱吃辣:4", "雷点:别提成绩:4"] },
      ],
      mood_events: [
        { event_id: "m1", valence: 0.4, reason: "「你画得真好」让我挺开心", created_at: "2026-10-07T14:20:00+00:00" },
        { event_id: "m2", valence: -0.5, reason: "「你就是个废物」让我不太舒服", created_at: "2026-10-07T12:05:00+00:00" },
      ],
    },
    injections: {
      note: "注入会以【强制规则】的形式追加到系统提示；自带预设立即可用，可改内容。",
      enabled: ["short-bubbles"],
      items: [
        { injection_id: "short-bubbles", name: "短消息拟人（每条都短）", builtin: true, enabled: true,
          content: "【硬性】每条消息必须短：正文 ≤15 字，最多不超过 20 字。\
【硬性】一句话就是一条消息；想说的多就拆成 2~4 条连发，绝不写成一段长文。\
【硬性】单条消息里不许出现换行、不许分段、不许列点。" },
        { injection_id: "no-assistant-tone", name: "禁客服腔/助手腔", builtin: true, enabled: false,
          content: "【硬性】禁止任何客服与助手口吻：「您好」「请问有什么可以帮您」「希望对你有所帮助」。" },
        { injection_id: "no-markdown", name: "禁 markdown 排版", builtin: true, enabled: false,
          content: "【硬性】消息里不出现 markdown：# 标题、**加粗**、`代码`、- 列表、1. 编号一律不要。" },
        { injection_id: "custom-0001", name: "别叫我主人", builtin: false, enabled: true,
          content: "【硬性】不要称呼我为爸爸/主人，就叫 Rikka。" },
      ],
    },
  };

  window.AstrBotPluginPage = {
    ready: async () => {},
    apiGet: async (path, params) => {
      const route = String(path || "").replace(/^page\//, "").split("?")[0];
      if (route === "memory_tree") {
        const node = (params || {}).node;
        return { status: "ok", data: node ? (byNode[node] || { children: [] }) : { roots } };
      }
      return { status: "ok", data: data[route] === undefined ? [] : data[route] };
    },
    apiPost: async () => ({ status: "ok", data: { ok: true, removed: 1 } }),
  };
  return "mock 已装好：点左侧任一菜单即可";
})();
