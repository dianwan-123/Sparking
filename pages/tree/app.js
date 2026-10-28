// 记忆网（独立页）：桥接 + MemoryWeb 画布 + 分支/回溯/剪枝
// 桥接约定：惰性取 window.AstrBotPluginPage + await ready() + 端点带 page/ 前缀 + 同源 fetch 兜底
(() => {
  "use strict";

  const PLUGIN = "astrbot_plugin_long_memory_agent";
  const $ = (id) => document.getElementById(id);

  function toast(message, kind = "") {
    const box = $("toast");
    box.textContent = message;
    box.className = "toast " + kind;
    box.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { box.hidden = true; }, kind === "error" ? 5200 : 2600);
  }

  function bridgeObject() {
    const bridge = window.AstrBotPluginPage;
    return bridge && typeof bridge === "object" ? bridge : null;
  }

  async function bridgeReady(timeoutMs) {
    const deadline = Date.now() + (timeoutMs || 4000);
    while (Date.now() < deadline) {
      const bridge = bridgeObject();
      if (bridge && typeof bridge.apiGet === "function") {
        if (typeof bridge.ready === "function") {
          try { await bridge.ready(); } catch (_error) { /* 老版本 */ }
        }
        return bridge;
      }
      await new Promise((resolve) => setTimeout(resolve, 120));
    }
    return null;
  }

  const endpointOf = (path) => "page/" + String(path || "").replace(/^\/+/, "");

  function unwrap(payload, fallback) {
    const body = payload;
    if (body && typeof body === "object") {
      if (body.ok === false || body.status === "error" || body.success === false) {
        throw new Error(String(body.message || body.error || fallback || "请求失败"));
      }
      if (body.data !== undefined) return body.data;
    }
    return body === undefined || body === null ? {} : body;
  }

  async function sameOrigin(path, options) {
    const response = await fetch("/" + PLUGIN + "/" + endpointOf(path),
      Object.assign({ credentials: "same-origin",
                      headers: { "Content-Type": "application/json" } }, options || {}));
    const text = await response.text();
    let payload;
    try { payload = JSON.parse(text); } catch (_error) { payload = { ok: true, data: text }; }
    return unwrap(payload, "HTTP " + response.status);
  }

  // 插件重载的瞬间路由会短暂消失（旧实例注销 → 新实例注册之间）：遇到
  // "未找到该路由"自动重试几次，别把重载窗口当故障；重试仍失败给出明确指引。
  const ROUTE_MISSING_HINT = "未找到该路由";

  async function _withReloadRetry(run) {
    let lastError = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        return await run();
      } catch (error) {
        lastError = error;
        const message = String((error && error.message) || error);
        if (!message.includes(ROUTE_MISSING_HINT) || attempt >= 2) break;
        toast("插件可能正在重载，正在重试…");
        await new Promise((resolve) => setTimeout(resolve, 1200 * (attempt + 1)));
      }
    }
    const message = String((lastError && lastError.message) || lastError || "");
    if (message.includes(ROUTE_MISSING_HINT)) {
      throw new Error("插件似乎还没重载完成：在插件列表点一次「重载」，或稍等几秒刷新页面即可");
    }
    throw lastError;
  }

  async function get(path, params) {
    return _withReloadRetry(async () => {
      const bridge = await bridgeReady();
      if (bridge) return unwrap(await bridge.apiGet(endpointOf(path), params || {}), "读取失败");
      const query = new URLSearchParams();
      Object.keys(params || {}).forEach((key) => {
        const value = params[key];
        if (value !== undefined && value !== null) query.set(key, String(value));
      });
      return sameOrigin(path + (query.toString() ? "?" + query.toString() : ""));
    });
  }

  async function post(action, data) {
    const body = Object.assign({ action }, data || {});
    const bridge = await bridgeReady();
    if (bridge) return unwrap(await bridge.apiPost(endpointOf("action"), body), "操作失败");
    return sameOrigin("action", { method: "POST", body: JSON.stringify(body) });
  }

  // ---------------------------------------------------------------- 模态
  function modal(options) {
    const opts = options || {};
    return new Promise((resolve) => {
      const overlay = document.createElement("div");
      overlay.className = "overlay";
      const dialog = document.createElement("div");
      dialog.className = "dialog";
      const title = document.createElement("h3");
      title.textContent = opts.title || "";
      dialog.appendChild(title);
      if (opts.hint) {
        const hint = document.createElement("p");
        hint.className = "dialog-hint";
        hint.textContent = opts.hint;
        dialog.appendChild(hint);
      }
      const inputs = {};
      (opts.fields || []).forEach((field) => {
        const label = document.createElement("label");
        label.textContent = field.label || field.key;
        const input = field.multiline ? document.createElement("textarea")
                                      : document.createElement("input");
        if (field.multiline) input.rows = 4;
        input.value = field.value || "";
        inputs[field.key] = input;
        label.appendChild(input);
        dialog.appendChild(label);
      });
      const actions = document.createElement("div");
      actions.className = "actions";
      const cancel = document.createElement("button");
      cancel.className = "btn ghost";
      cancel.textContent = "取消";
      const ok = document.createElement("button");
      ok.className = "btn " + (opts.danger ? "danger" : "primary");
      ok.textContent = opts.okText || "确定";
      const close = (value) => { overlay.remove(); resolve(value); };
      cancel.addEventListener("click", () => close(null));
      ok.addEventListener("click", () => {
        const out = {};
        Object.keys(inputs).forEach((key) => { out[key] = inputs[key].value.trim(); });
        close(out);
      });
      actions.appendChild(cancel);
      actions.appendChild(ok);
      dialog.appendChild(actions);
      overlay.appendChild(dialog);
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay) close(null);
      });
      document.body.appendChild(overlay);
      const first = Object.values(inputs)[0];
      if (first) setTimeout(() => first.focus(), 30);
    });
  }

  // ---------------------------------------------------------------- 状态
  const state = {
    web: null,
    snapshot: null,
    selected: null,
    pruneMode: false,
    pruneSelection: [],
    showArchived: false,
    hiddenKinds: new Set(),
  };

  const KIND_LABEL = { person: "人物", group: "会话", topic: "话题", fact: "事实", summary: "摘要" };
  const KIND_COLOR = { person: "#f59e0b", group: "#6366f1", topic: "#10b981", fact: "#64748b", summary: "#ec4899" };
  const REL_LABEL = window.SparkingWeb ? window.SparkingWeb.REL_LABEL : {};

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderKinds() {
    const box = $("kinds");
    box.textContent = "";
    const nodes = (state.snapshot && state.snapshot.nodes) || [];
    const counts = {};
    nodes.forEach((node) => {
      counts[node.kind] = (counts[node.kind] || 0) + 1;
    });
    const total = nodes.length;
    $("kind-total").textContent = total ? total + " 个节点" : "";
    const allChip = el("button", "chip" + (state.hiddenKinds.size ? "" : " off"));
    allChip.appendChild(el("span", "name", "全部"));
    allChip.appendChild(el("span", "n", String(total)));
    allChip.addEventListener("click", () => {
      state.hiddenKinds.clear();
      renderKinds();
      applyKindFilter();
    });
    box.appendChild(allChip);
    Object.keys(KIND_LABEL).forEach((kind) => {
      const count = counts[kind] || 0;
      const chip = el("button", "chip" + (state.hiddenKinds.has(kind) ? " off" : ""));
      const dot = el("span", "dot");
      dot.style.background = KIND_COLOR[kind];
      chip.appendChild(dot);
      chip.appendChild(el("span", "name", KIND_LABEL[kind]));
      chip.appendChild(el("span", "n", String(count)));
      chip.addEventListener("click", () => {
        // 点一下"只看这一类"（再点回到全部）——比原来的"隐藏这一类"更符合直觉
        const onlyThis = state.hiddenKinds.size !== Object.keys(KIND_LABEL).length - 1
          || state.hiddenKinds.has(kind);
        state.hiddenKinds.clear();
        if (onlyThis) {
          Object.keys(KIND_LABEL).forEach((other) => {
            if (other !== kind) state.hiddenKinds.add(other);
          });
        }
        renderKinds();
        applyKindFilter();
      });
      box.appendChild(chip);
    });
  }

  // 搜索命中列表（点一条就选中并聚焦它）
  function renderSearchHits(keyword) {
    const list = $("search-hits");
    if (!list) return;
    list.textContent = "";
    const text = String(keyword || "").trim().toLowerCase();
    if (!text || !state.snapshot) return;
    const matches = state.snapshot.nodes
      .filter((node) => String(node.label || "").toLowerCase().includes(text))
      .slice(0, 12);
    if (!matches.length) {
      const li = el("li");
      li.appendChild(el("span", "muted small", "没有匹配的节点"));
      list.appendChild(li);
      return;
    }
    matches.forEach((node) => {
      const li = el("li");
      const button = el("button");
      const dot = el("span", "dot");
      dot.style.background = KIND_COLOR[node.kind] || "#64748b";
      button.appendChild(dot);
      button.appendChild(el("span", "label", String(node.label || "")));
      button.appendChild(el("span", "meta", (KIND_LABEL[node.kind] || node.kind)
        + " · " + (Number(node.weight) || 1)));
      button.addEventListener("click", () => {
        state.web.setSelection([node.id]);
        state.web.focus(node.id, 1.6);
        if (!state.pruneMode) loadDetail(node.id);
      });
      li.appendChild(button);
      list.appendChild(li);
    });
  }

  function applyKindFilter() {
    if (!state.snapshot) return;
    const hidden = state.hiddenKinds;
    const nodes = state.snapshot.nodes.filter((node) => !hidden.has(node.kind));
    const alive = new Set(nodes.map((node) => node.id));
    const edges = state.snapshot.edges.filter(
      (edge) => alive.has(edge.source) && alive.has(edge.target));
    // 藏掉某类节点后，连着它的线也没了——剩下的节点里不留"孤零零的点"：
    // 没有可见连线的节点不画（和快照的口径一致）。
    const linked = new Set();
    edges.forEach((edge) => { linked.add(edge.source); linked.add(edge.target); });
    state.web.setData({
      nodes: nodes.filter((node) => linked.has(node.id)),
      edges,
    });
  }

  function renderStats() {
    const stats = (state.snapshot && state.snapshot.stats) || {};
    const kinds = stats.kinds || {};
    const bits = [
      "节点 " + (stats.nodes || 0),
      "连线 " + (stats.edges || 0),
      "来源 " + (stats.sources || 0),
    ];
    const pending = stats.pending || {};
    const pendingTotal = Object.values(pending).reduce((a, b) => a + (b || 0), 0);
    if (pendingTotal) bits.push("待入网 " + pendingTotal);
    if (stats.pruned) bits.push("已剪枝 " + stats.pruned);
    if (stats.last_sync_at) bits.push("上次同步 " + String(stats.last_sync_at).replace("T", " ").slice(0, 16));
    const kindBits = Object.keys(kinds).map((kind) => (KIND_LABEL[kind] || kind) + " " + kinds[kind]);
    $("stats").textContent = bits.join(" · ") + (kindBits.length ? "　｜　" + kindBits.join(" / ") : "");
  }

  function renderBranches() {
    const list = (state.snapshot && state.snapshot.branches) || [];
    $("branch-count").textContent = String(list.length);
    const drawer = $("branches");
    drawer.textContent = "";
    const head = el("div", "drawer-head");
    head.appendChild(el("h3", "", "记忆分支"));
    const close = el("button", "btn ghost", "关闭");
    close.addEventListener("click", () => { drawer.hidden = true; });
    head.appendChild(close);
    drawer.appendChild(head);
    if (!list.length) {
      drawer.appendChild(el("p", "muted", "还没有分支。选中一个节点后点「创建分支」，以后可以回溯到这里。"));
      return;
    }
    list.forEach((branch) => {
      const card = el("div", "branch-card" + (branch.status === "reverted" ? " reverted" : ""));
      const title = el("div", "branch-title", branch.label || branch.node_key);
      card.appendChild(title);
      const metaBits = [
        "切口 " + String(branch.cut_at || "").replace("T", " ").slice(0, 16),
        branch.status === "reverted" ? "已回溯 " + String(branch.reverted_at || "").slice(0, 16) : "未回溯",
      ];
      if (branch.note) metaBits.push("备注：" + branch.note);
      card.appendChild(el("div", "muted small", metaBits.join("　")));
      if (branch.status !== "reverted") {
        const revert = el("button", "btn danger small", "回溯到此分支");
        revert.addEventListener("click", async () => {
          const okay = await modal({
            title: "回溯到「" + (branch.label || "该分支") + "」",
            hint: "会把该节点之后的记忆压缩成一份「回溯存档」（可在「显示归档」里查看），"
              + "删除这些记忆的原始记录，并封住对应的消息区间（不会再被重新压成新记忆）。"
              + "记忆基本回到该节点时刻。此操作不可撤销。",
            okText: "确认回溯", danger: true,
            fields: [{ key: "note", label: "备注（可选）", value: "" }],
          });
          if (!okay) return;
          toast("回溯中……");
          try {
            const out = await post("web_branch_revert", { branch_id: branch.branch_id });
            toast("已回溯：归档 " + (out.archived || []).length + " 份，删除摘要 " + (out.summaries || 0)
              + " 条、账本 " + (out.ledger || 0) + " 条，封口 " + (out.sealed || []).length + " 个会话", "ok");
            await load();
            drawer.hidden = false;
          } catch (error) { toast(String(error.message || error), "error"); }
        });
        card.appendChild(revert);
      } else if (branch.archive_ref) {
        card.appendChild(el("div", "muted small", "回溯存档：" + branch.archive_ref.split(",").length + " 份（勾选「显示归档」可看）"));
      }
      drawer.appendChild(card);
    });
  }

  function renderDetail(detail) {
    const box = $("detail");
    box.textContent = "";
    if (!detail) { box.hidden = true; return; }
    box.hidden = false;
    const node = detail.node;
    const head = el("div", "detail-head");
    const dot = el("span", "dot");
    dot.style.background = KIND_COLOR[node.kind] || "#64748b";
    head.appendChild(dot);
    head.appendChild(el("h3", "", node.label || node.key));
    const close = el("button", "btn ghost small", "×");
    close.addEventListener("click", () => { box.hidden = true; state.selected = null; });
    head.appendChild(close);
    box.appendChild(head);
    const meta = el("div", "muted small");
    const bits = [KIND_LABEL[node.kind] || node.kind, "权重 " + node.weight];
    if (node.meta && node.meta.at) bits.push("时间 " + String(node.meta.at).replace("T", " ").slice(0, 16));
    if (node.meta && node.meta.level) bits.push("层级 L" + node.meta.level);
    if (node.status === "archived") bits.push("（归档）");
    meta.textContent = bits.join("　");
    box.appendChild(meta);

    const actions = el("div", "row");
    const branchBtn = el("button", "btn primary small", "在此创建分支");
    branchBtn.addEventListener("click", async () => {
      const out = await modal({
        title: "在「" + (node.label || "").slice(0, 20) + "」创建分支",
        hint: "分支记录此刻的记忆状态；以后可以「回溯」到这里——之后的记忆会被压缩归档并删回该点。",
        okText: "创建",
        fields: [{ key: "note", label: "备注（可选）", value: "" }],
      });
      if (!out) return;
      try {
        const result = await post("web_branch_create", { node_id: node.id, note: out.note });
        toast("分支已创建（切口 " + String(result.branch.cut_at || "").slice(0, 16) + "）", "ok");
        await load();
      } catch (error) { toast(String(error.message || error), "error"); }
    });
    actions.appendChild(branchBtn);
    if (state.pruneMode) {
      const add = el("button", "btn small", "加入剪枝选择");
      add.addEventListener("click", () => {
        if (!state.pruneSelection.includes(node.id)) state.pruneSelection.push(node.id);
        state.web.setSelection(state.pruneSelection);
        updatePruneBar();
      });
      actions.appendChild(add);
    }
    box.appendChild(actions);

    if (detail.edges && detail.edges.length) {
      box.appendChild(el("h4", "", "关系（" + detail.edges.length + "）"));
      const list = el("div", "detail-list");
      detail.edges.slice(0, 30).forEach((edge) => {
        const row = el("div", "detail-edge");
        const rel = REL_LABEL[edge.relation] || edge.relation;
        row.textContent = (edge.direction === "out" ? rel + " → " : "← " + rel + " ")
          + edge.other_label + (edge.weight > 1 ? "（×" + edge.weight + "）" : "");
        row.style.cursor = "pointer";
        row.addEventListener("click", () => {
          if (edge.other_id) {
            state.web.focus(edge.other_id);
            loadDetail(edge.other_id);
          }
        });
        list.appendChild(row);
      });
      box.appendChild(list);
    }
    if (detail.sources && detail.sources.length) {
      box.appendChild(el("h4", "", "来源（" + detail.sources.length + "）"));
      const list = el("div", "detail-list");
      detail.sources.slice(0, 14).forEach((source) => {
        list.appendChild(el("div", "detail-source", source.title || source.ref));
      });
      box.appendChild(list);
    }
  }

  async function loadDetail(nodeId) {
    try {
      const detail = await get("memory_web_node", { node_id: nodeId });
      state.selected = nodeId;
      renderDetail(detail);
    } catch (error) {
      toast(String(error.message || error), "error");
    }
  }

  function updatePruneBar() {
    const bar = $("prune-bar");
    bar.hidden = !state.pruneMode;
    if (!state.pruneMode) return;
    const count = state.pruneSelection.length;
    $("prune-info").textContent = "剪枝：已选 " + count + " 个节点"
      + "（沿它们之间的路径丢弃低价值节点、把必要摘要向上压缩；压缩程度跟随「上下文详细程度」配置）";
    $("btn-prune-run").disabled = count < 2;
  }

  // ---------------------------------------------------------------- 加载
  async function load() {
    const params = { include_archived: state.showArchived ? 1 : 0 };
    const scope = $("scope").value;
    if (scope) params.scope = scope;
    const snapshot = await get("memory_web", params);
    state.snapshot = snapshot;
    fillScopes(snapshot.scopes || []);
    renderStats();
    renderBranches();
    applyKindFilter();
  }

  function fillScopes(scopes) {
    const select = $("scope");
    const current = select.value;
    select.textContent = "";
    const all = el("option", "", "全部会话");
    all.value = "";
    select.appendChild(all);
    scopes.forEach((scope) => {
      const option = el("option", "", scope.display_name || scope.conversation_id);
      option.value = scope.scope_id;
      select.appendChild(option);
    });
    select.value = current;
  }

  function start() {
    if (!window.SparkingWeb) {
      $("graph").innerHTML = '<div class="empty">渲染器未加载（web.js）</div>';
      return;
    }
    state.web = window.SparkingWeb.mount($("graph"), {
      onSelect: (node) => {
        if (state.pruneMode) return;
        if (node) loadDetail(node.id);
        else { state.selected = null; renderDetail(null); }
      },
      onMultiSelect: (nodes) => {
        state.pruneSelection = nodes.map((node) => node.id);
        updatePruneBar();
      },
      onFocus: (node, size) => {
        if (!node) return;
        if (!state.pruneMode) loadDetail(node.id);
        toast("已聚焦 " + String(node.label || "") + " 的邻域（" + size + " 个节点）", "ok");
      },
    });
    renderKinds();
    $("btn-refresh").addEventListener("click", () => load().catch((error) => toast(String(error.message || error), "error")));
    $("scope").addEventListener("change", () => load().catch((error) => toast(String(error.message || error), "error")));
    $("btn-archived").addEventListener("click", () => {
      state.showArchived = !state.showArchived;
      $("btn-archived").classList.toggle("active", state.showArchived);
      load().catch((error) => toast(String(error.message || error), "error"));
    });
    $("btn-branches").addEventListener("click", () => {
      const drawer = $("branches");
      drawer.hidden = !drawer.hidden;
    });
    $("btn-prune").addEventListener("click", () => {
      state.pruneMode = !state.pruneMode;
      $("btn-prune").classList.toggle("active", state.pruneMode);
      state.pruneSelection = [];
      state.web.setMulti(state.pruneMode);
      state.web.setSelection([]);
      updatePruneBar();
      toast(state.pruneMode ? "剪枝模式：点选若干节点" : "已退出剪枝", "ok");
    });
    $("btn-prune-cancel").addEventListener("click", () => {
      state.pruneMode = false;
      state.pruneSelection = [];
      $("btn-prune").classList.remove("active");
      state.web.setMulti(false);
      updatePruneBar();
    });
    $("btn-prune-run").addEventListener("click", async () => {
      if (state.pruneSelection.length < 2) return;
      const okay = await modal({
        title: "执行剪枝",
        hint: "沿所选节点之间的路径：低价值叶子节点会被丢弃（不再出现在网与检索里），"
          + "必要摘要会被向上压缩。压缩程度跟随「上下文详细程度」配置。",
        okText: "剪枝", danger: true,
        fields: [{ key: "note", label: "备注（可选）", value: "" }],
      });
      if (!okay) return;
      toast("剪枝中……");
      try {
        const out = await post("web_prune", { node_ids: state.pruneSelection });
        toast("剪枝完成：路径 " + out.path_count + " 个节点，丢弃 " + out.pruned
          + "，压缩 " + (out.merged || []).length + " 次"
          + (out.notes && out.notes.length ? "（" + out.notes[0] + "）" : ""), "ok");
        state.pruneMode = false;
        state.pruneSelection = [];
        $("btn-prune").classList.remove("active");
        state.web.setMulti(false);
        updatePruneBar();
        await load();
      } catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-rebuild").addEventListener("click", async () => {
      const okay = await modal({
        title: "重建记忆网",
        hint: "按当前全部记忆（摘要/账本/话题/印象）清空并重建整张网。分支会保留。",
        okText: "重建", danger: true,
        fields: [{ key: "note", label: "确认请留空", value: "" }],
      });
      if (!okay) return;
      toast("重建中……");
      try {
        const out = await post("web_rebuild", {});
        toast("重建完成：来源 " + (out.sources || 0) + "，节点 " + ((out.stats || {}).nodes || 0), "ok");
        await load();
      } catch (error) { toast(String(error.message || error), "error"); }
    });
    $("search").addEventListener("input", () => {
      const keyword = $("search").value.trim().toLowerCase();
      if (!state.snapshot) return;
      renderSearchHits(keyword);
      if (!keyword) { state.web.setSelection([]); return; }
      const matches = state.snapshot.nodes.filter(
        (node) => String(node.label || "").toLowerCase().includes(keyword));
      state.web.setSelection(matches.map((node) => node.id));
      if (matches.length) state.web.focus(matches[0].id, 1.25);
    });
    load().catch((error) => toast(String(error.message || error), "error"));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
