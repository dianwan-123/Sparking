// ===== 记忆森林渲染器（与 pages/tree/forest.js 同源，内联以保证插件页可用）=====
// 说明：AstrBot 插件页里跨页相对路径（../tree/forest.js）会 404，所以这里内联一份；
// 改渲染逻辑时请两个文件一起改。
// Sparking 记忆森林 —— 圆形节点 + 连线 的真实可视化（零依赖，纯 SVG）
// 用法：window.SparkingForest.mount(container, { getTree, onEdit, onDelete, onAddChild })
//   getTree(nodeId|null) -> Promise<{roots:[...]}|{children:[...]}>
// 特性：整洁树布局（父子居中对齐）、悬停详情浮层、左键菜单（编辑/删除/加子节点/展开收拢）、
//       滚轮缩放、拖拽平移、懒加载（点开才拉子节点）、按类型着色、数量徽标。
(function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const COLORS = {
    scope: "#6ea8fe",       // 会话（群/私聊）
    category: "#8b7bf7",    // 类目
    summary: "#3ecf8e",     // 分层摘要
    catalog: "#f0b429",     // 记忆账本
    impression: "#e879b9",  // 人物印象
    topic: "#59c2d6",       // 群话题
    message: "#8d97a8",     // 证据消息
    default: "#98a2b3",
  };
  const ROW_GAP = 46;
  const COL_GAP = 210;
  const NODE_R = 17;

  function svgEl(tag, attrs) {
    const node = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach((key) => node.setAttribute(key, attrs[key]));
    return node;
  }

  function colorOf(node) {
    return COLORS[node.type] || COLORS.default;
  }

  function mount(container, api) {
    const state = {
      nodes: new Map(),      // id -> node 数据
      children: new Map(),   // id -> [childId]
      expanded: new Set(),
      roots: [],
      scale: 1,
      offset: { x: 40, y: 30 },
      drag: null,
      selected: null,
      api,
    };

    container.classList.add("forest-host");
    container.innerHTML = "";
    const svg = svgEl("svg", { class: "forest-svg" });
    const viewport = svgEl("g", { class: "forest-viewport" });
    svg.appendChild(viewport);
    container.appendChild(svg);

    const tip = document.createElement("div");
    tip.className = "forest-tip";
    tip.hidden = true;
    container.appendChild(tip);

    const menu = document.createElement("div");
    menu.className = "forest-menu";
    menu.hidden = true;
    container.appendChild(menu);

    function applyTransform() {
      viewport.setAttribute("transform",
        `translate(${state.offset.x},${state.offset.y}) scale(${state.scale})`);
    }

    function register(node, parentId) {
      state.nodes.set(node.id, node);
      if (parentId) {
        const list = state.children.get(parentId) || [];
        if (!list.includes(node.id)) list.push(node.id);
        state.children.set(parentId, list);
      }
    }

    // ---------------------------------------------------------------- 布局
    function layout() {
      const positions = new Map();
      let cursor = 0;
      const walk = (id, depth) => {
        const node = state.nodes.get(id);
        if (!node) return { y: cursor };
        const kids = (state.children.get(id) || [])
          .filter((kid) => state.expanded.has(id) || state.expanded.has(kid));
        let y;
        if (!state.expanded.has(id) || !kids.length) {
          y = cursor;
          cursor += ROW_GAP;
        } else {
          const childYs = kids.map((kid) => walk(kid, depth + 1).y);
          y = (childYs[0] + childYs[childYs.length - 1]) / 2;
        }
        positions.set(id, { x: depth * COL_GAP, y });
        return { y };
      };
      state.roots.forEach((id) => walk(id, 0));
      return positions;
    }

    function render() {
      const positions = layout();
      viewport.innerHTML = "";
      const edges = svgEl("g", { class: "forest-edges" });
      const nodesLayer = svgEl("g", { class: "forest-nodes" });
      viewport.appendChild(edges);
      viewport.appendChild(nodesLayer);

      state.nodes.forEach((node) => {
        const parent = node.__parent;
        if (!parent) return;
        const from = positions.get(parent);
        const to = positions.get(node.id);
        if (!from || !to) return;
        const mid = (from.x + to.x) / 2;
        edges.appendChild(svgEl("path", {
          d: `M ${from.x + NODE_R} ${from.y} C ${mid} ${from.y}, ${mid} ${to.y}, ${to.x - NODE_R} ${to.y}`,
          fill: "none",
          stroke: "#39424f",
          "stroke-width": 1.5,
        }));
      });

      state.nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        const group = svgEl("g", { class: "forest-node", tabindex: "0" });
        group.setAttribute("transform", `translate(${pos.x},${pos.y})`);
        group.dataset.nodeId = node.id;

        const circle = svgEl("circle", {
          r: NODE_R + (node.type === "scope" ? 4 : 0),
          fill: colorOf(node),
          stroke: state.selected === node.id ? "#ffffff" : "#10141a",
          "stroke-width": state.selected === node.id ? 2.5 : 1.5,
        });
        group.appendChild(circle);

        const label = svgEl("text", {
          x: NODE_R + 10, y: 5,
          fill: "#e6e9ef", "font-size": "12.5px",
        });
        label.textContent = String(node.label || node.id).slice(0, 26);
        group.appendChild(label);

        if (node.count) {
          const badge = svgEl("text", {
            x: 0, y: 4, "text-anchor": "middle",
            fill: "#0b0e13", "font-size": "10px", "font-weight": "700",
          });
          badge.textContent = state.expanded.has(node.id) ? String(node.count)
            : `${node.count}+`;
          group.appendChild(badge);
        }
        nodesLayer.appendChild(group);

        group.addEventListener("mouseenter", (event) => showTip(node, event));
        group.addEventListener("mousemove", (event) => moveTip(event));
        group.addEventListener("mouseleave", () => { tip.hidden = true; });
        group.addEventListener("click", (event) => {
          event.stopPropagation();
          state.selected = node.id;
          openMenu(node, event);
          render();
        });
        group.addEventListener("dblclick", (event) => {
          event.stopPropagation();
          menu.hidden = true;
          toggleExpand(node.id);
        });
      });
      applyTransform();
    }

    // ---------------------------------------------------------------- 提示
    function showTip(node, event) {
      const lines = [];
      lines.push(`<b>${escapeHtml(node.label || node.id)}</b>`);
      if (node.type) lines.push(`类型：${escapeHtml(kindText(node))}`);
      if (node.time) {
        lines.push(`时间：${escapeHtml(node.time)}`
          + (node.time_granularity ? `（${escapeHtml(node.time_granularity)}）` : ""));
      }
      if (node.count) lines.push(`子节点：${node.count}`);
      if (node.status) lines.push(`状态：${escapeHtml(node.status)}`);
      if (node.hint) lines.push(`<span class="tip-body">${escapeHtml(String(node.hint).slice(0, 240))}</span>`);
      lines.push('<span class="tip-hint">左键：菜单 · 双击：展开/收起</span>');
      tip.innerHTML = lines.join("<br>");
      tip.hidden = false;
      moveTip(event);
    }

    function moveTip(event) {
      const box = container.getBoundingClientRect();
      const x = event.clientX - box.left + 16;
      const y = event.clientY - box.top + 12;
      tip.style.left = Math.min(x, box.width - 300) + "px";
      tip.style.top = Math.min(y, box.height - 120) + "px";
    }

    function kindText(node) {
      const map = {
        scope: "会话", category: "类目", summary: "分层摘要", catalog: "记忆账本",
        impression: "人物印象", topic: "群话题", message: "证据消息",
      };
      return map[node.type] || node.type || "-";
    }

    // ---------------------------------------------------------------- 菜单
    function openMenu(node, event) {
      const box = container.getBoundingClientRect();
      menu.innerHTML = "";
      const items = [];
      if (node.count) {
        items.push([state.expanded.has(node.id) ? "收起子节点" : "展开子节点",
                    () => toggleExpand(node.id)]);
      }
      if (state.api.onAddChild) {
        items.push(["＋ 加子节点", () => state.api.onAddChild(node)]);
      }
      if (node.editable && state.api.onEdit) {
        items.push(["✎ 编辑", () => state.api.onEdit(node)]);
      }
      if (node.editable && state.api.onDelete) {
        items.push(["🗑 删除", () => state.api.onDelete(node)]);
      }
      if (!items.length) items.push(["（这一层没有可执行的操作）", null]);
      items.forEach(([label, handler]) => {
        const button = document.createElement("button");
        button.className = "forest-menu-item";
        button.textContent = label;
        if (handler) {
          button.addEventListener("click", (clickEvent) => {
            clickEvent.stopPropagation();
            menu.hidden = true;
            handler();
          });
        } else {
          button.disabled = true;
        }
        menu.appendChild(button);
      });
      menu.hidden = false;
      menu.style.left = Math.min(event.clientX - box.left, box.width - 180) + "px";
      menu.style.top = Math.min(event.clientY - box.top, box.height - 160) + "px";
    }

    // ---------------------------------------------------------------- 交互
    async function toggleExpand(id) {
      const node = state.nodes.get(id);
      if (!node) return;
      if (state.expanded.has(id)) {
        state.expanded.delete(id);
        render();
        return;
      }
      if (!state.children.has(id)) {
        try {
          const data = await state.api.getTree(id);
          const kids = (data && data.children) || [];
          kids.forEach((child) => {
            child.__parent = id;
            register(child, id);
          });
          node.count = kids.length || node.count;
        } catch (error) {
          node.hint = `加载失败：${String(error.message || error)}`;
        }
      }
      state.expanded.add(id);
      render();
    }

    svg.addEventListener("click", () => { menu.hidden = true; });
    svg.addEventListener("wheel", (event) => {
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.12 : 0.89;
      state.scale = Math.max(0.35, Math.min(2.4, state.scale * factor));
      applyTransform();
    }, { passive: false });
    svg.addEventListener("mousedown", (event) => {
      if (event.target.closest(".forest-node")) return;
      state.drag = { x: event.clientX, y: event.clientY,
                     ox: state.offset.x, oy: state.offset.y };
      menu.hidden = true;
    });
    window.addEventListener("mousemove", (event) => {
      if (!state.drag) return;
      state.offset.x = state.drag.ox + (event.clientX - state.drag.x);
      state.offset.y = state.drag.oy + (event.clientY - state.drag.y);
      applyTransform();
    });
    window.addEventListener("mouseup", () => { state.drag = null; });

    async function reload(filter) {
      state.nodes.clear();
      state.children.clear();
      state.expanded.clear();
      state.selected = null;
      let data;
      try {
        data = await state.api.getTree(null);
      } catch (error) {
        container.innerHTML = `<div class="forest-empty">加载失败：${escapeHtml(String(error.message || error))}</div>`;
        return;
      }
      const keyword = String(filter || "").trim();
      state.roots = ((data && data.roots) || [])
        .filter((node) => !keyword || String(node.label || "").includes(keyword))
        .map((node) => {
          register(node, null);
          return node.id;
        });
      if (!state.roots.length) {
        container.appendChild(Object.assign(document.createElement("div"),
          { className: "forest-empty", textContent: "还没有记忆——先去群里聊几句，或点「新增记忆」" }));
      }
      render();
    }

    function escapeHtml(value) {
      return String(value == null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    reload();
    return { reload, render, expand: toggleExpand };
  }

  window.SparkingForest = { mount };
})();

// Sparking 控制台前端（完全重写）
// 桥接：window.AstrBotPluginPage —— endpoint 只能是纯路径段（不得含 ? :// \ #），
// 查询参数一律走 apiGet 的第二参数。
//
// 设计要点（对齐"删除点了没反应 / 有错字 / 很卡"三条实录）：
// 1) 一次只加载当前面板（按需分片），列表带 limit，消息支持"加载更早"；
// 2) 所有删除/写操作走 POST action，成功后**局部刷新**对应列表并弹提示；
// 3) 文案全部来自本文件与后端，界面不再出现占位式错字。
(() => {
  "use strict";

  // 插件名（同源 fetch 兜底时拼路径用）——与后端 _PAGE_PREFIX 一致
  const PLUGIN = "astrbot_plugin_long_memory_agent";
  const PANELS = [
    { id: "overview", icon: "◉", title: "概览" },
    { id: "memory", icon: "❖", title: "记忆" },
    { id: "forest", icon: "🌳", title: "记忆森林" },
    { id: "people", icon: "☺", title: "人物" },
    { id: "groups", icon: "▤", title: "群与会话" },
    { id: "schedule", icon: "⏱", title: "日程与任务" },
    { id: "caps", icon: "✦", title: "能力与拓展" },
    { id: "config", icon: "⚙", title: "配置" },
    { id: "ops", icon: "⌘", title: "运维" },
  ];

  const state = {
    panel: "overview",
    scope: "",
    scopes: [],
    messages: [],
    msgCursor: null,
    msgKeyword: "",
    capabilities: [],
    config: [],
    loaded: {},
  };

  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- 基础
  function toast(message, kind = "") {
    const box = $("toast");
    box.textContent = message;
    box.className = "toast " + kind;
    box.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { box.hidden = true; }, kind === "error" ? 4200 : 2200);
  }

  // ---------------------------------------------------------------- 桥接
  // 实录：之前"插件页桥接不可用"——三个坑：①在模块加载时就抓 window.AstrBotPluginPage
  // （宿主注入稍晚就永久 undefined）；②没 await bridge.ready()（对象存在≠就绪）；
  // ③端点没带 page/ 前缀。这里逐条修掉，并给同源 fetch 兜底。
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
          try { await bridge.ready(); } catch (_error) { /* 老版本无 ready 语义 */ }
        }
        return bridge;
      }
      await new Promise((resolve) => setTimeout(resolve, 120));
    }
    return null;
  }

  function endpointOf(path) {
    return "page/" + String(path || "").replace(/^\/+/, "");
  }

  function unwrap(payload, fallbackMessage) {
    // 兼容三种信封：本插件的 {ok,data,message}、AstrBot 的 {status,data|message}、
    // 以及裸数据（老版本直接回 body）
    const body = payload;
    if (body && typeof body === "object") {
      if (body.ok === false || body.status === "error" || body.success === false) {
        throw new Error(String(body.message || body.error || fallbackMessage || "请求失败"));
      }
      if (body.data !== undefined) return body.data;
    }
    return body === undefined || body === null ? {} : body;
  }

  async function sameOrigin(path, options) {
    // 桥接不可用时的兜底：同源直连页面接口（Dashboard 会话 cookie 仍带得到）
    const url = "/" + PLUGIN + "/" + endpointOf(path);
    const response = await fetch(url, Object.assign({
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
    }, options || {}));
    const text = await response.text();
    let payload;
    try { payload = JSON.parse(text); } catch (_error) { payload = { ok: true, data: text }; }
    if (!response.ok && payload && payload.message === undefined) {
      throw new Error("HTTP " + response.status);
    }
    return unwrap(payload, "HTTP " + response.status);
  }

  async function get(endpoint, params) {
    const query = new URLSearchParams();
    Object.keys(params || {}).forEach((key) => {
      const value = params[key];
      if (value !== undefined && value !== null) query.set(key, String(value));
    });
    const bridge = await bridgeReady();
    if (bridge) {
      return unwrap(await bridge.apiGet(endpointOf(endpoint), params || {}), "请求失败");
    }
    const suffix = query.toString() ? "?" + query.toString() : "";
    return sameOrigin(endpoint + suffix);
  }

  async function post(action, data) {
    const body = Object.assign({ action }, data || {});
    const bridge = await bridgeReady();
    if (bridge) {
      return unwrap(await bridge.apiPost(endpointOf("action"), body), "操作失败");
    }
    return sameOrigin("action", { method: "POST", body: JSON.stringify(body) });
  }

  // 页面内模态框：插件页在 iframe 沙箱里，原生 confirm()/prompt() 会被拦或直接返回
  // 空值——实录"删除全部记忆点了显示已取消""群记忆无法删除"都是这个原因。
  function askConfirm(options) {
    const opts = options || {};
    return new Promise((resolve) => {
      const overlay = el("div", "modal-overlay");
      const box = el("div", "modal" + (opts.danger ? " danger" : ""));
      box.appendChild(el("h3", "", opts.title || "确认操作"));
      if (opts.message) box.appendChild(el("p", "modal-msg", opts.message));
      let input = null;
      if (opts.confirmWord) {
        box.appendChild(el("p", "muted", `请输入「${opts.confirmWord}」以确认：`));
        input = el("input");
        input.placeholder = opts.confirmWord;
        box.appendChild(input);
      }
      const actions = el("div", "modal-actions");
      const cancel = el("button", "btn ghost", "取消");
      const ok = el("button", "btn " + (opts.danger ? "danger" : "primary"),
                    opts.okText || "确定");
      const close = (value) => {
        overlay.remove();
        document.removeEventListener("keydown", onKey);
        resolve(value);
      };
      const onKey = (event) => {
        if (event.key === "Escape") close(opts.confirmWord ? null : false);
        if (event.key === "Enter" && (!opts.confirmWord || (input && input.value.trim()))) {
          close(opts.confirmWord ? (input.value.trim() || null) : true);
        }
      };
      cancel.addEventListener("click", () => close(opts.confirmWord ? null : false));
      ok.addEventListener("click", () => close(opts.confirmWord ? (input.value.trim() || null) : true));
      actions.appendChild(cancel);
      actions.appendChild(ok);
      box.appendChild(actions);
      overlay.appendChild(box);
      overlay.addEventListener("click", (event) => {
        if (event.target === overlay) close(opts.confirmWord ? null : false);
      });
      document.body.appendChild(overlay);
      document.addEventListener("keydown", onKey);
      if (input) setTimeout(() => input.focus(), 30);
      else setTimeout(() => ok.focus(), 30);
    });
  }

  function askPrompt(options) {
    const opts = options || {};
    return new Promise((resolve) => {
      const overlay = el("div", "modal-overlay");
      const box = el("div", "modal");
      box.appendChild(el("h3", "", opts.title || "填写"));
      const inputs = {};
      (opts.fields || []).forEach((field) => {
        box.appendChild(el("div", "muted", field.label || field.key));
        const input = field.multiline ? el("textarea") : el("input");
        if (field.multiline) input.rows = 3;
        input.value = field.value || "";
        inputs[field.key] = input;
        box.appendChild(input);
      });
      const actions = el("div", "modal-actions");
      const cancel = el("button", "btn ghost", "取消");
      const ok = el("button", "btn primary", opts.okText || "确定");
      const close = (value) => { overlay.remove(); resolve(value); };
      cancel.addEventListener("click", () => close(null));
      ok.addEventListener("click", () => {
        const out = {};
        Object.keys(inputs).forEach((key) => { out[key] = inputs[key].value.trim(); });
        close(out);
      });
      actions.appendChild(cancel);
      actions.appendChild(ok);
      box.appendChild(actions);
      overlay.appendChild(box);
      overlay.addEventListener("click", (event) => { if (event.target === overlay) close(null); });
      document.body.appendChild(overlay);
      const first = Object.values(inputs)[0];
      if (first) setTimeout(() => first.focus(), 30);
    });
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderList(container, items, renderItem, emptyText) {
    container.textContent = "";
    if (!items || !items.length) {
      container.appendChild(el("div", "empty", emptyText || "暂无数据"));
      return;
    }
    const frag = document.createDocumentFragment();
    items.forEach((item) => frag.appendChild(renderItem(item)));
    container.appendChild(frag);
  }

  function itemShell(titleNode, bodyText, metaText, actions) {
    const wrap = el("div", "item");
    const main = el("div", "main");
    main.appendChild(titleNode);
    if (bodyText) main.appendChild(el("div", "body", bodyText));
    if (metaText) main.appendChild(el("div", "meta", metaText));
    wrap.appendChild(main);
    if (actions && actions.length) {
      const box = el("div", "actions");
      actions.forEach((btn) => box.appendChild(btn));
      wrap.appendChild(box);
    }
    return wrap;
  }

  function actionBtn(label, handler, className) {
    const btn = el("button", "btn " + (className || ""), label);
    btn.addEventListener("click", handler);
    return btn;
  }

  // ---------------------------------------------------------------- 会话
  function fillScopes(scopes, keepCurrent) {
    state.scopes = scopes || [];
    const select = $("scope-select");
    const previous = keepCurrent && state.scope ? state.scope : state.scope;
    select.textContent = "";
    if (!state.scopes.length) {
      select.appendChild(el("option", "", "（还没有会话）"));
      select.disabled = true;
      return;
    }
    select.disabled = false;
    state.scopes.forEach((row) => {
      const option = el("option", "", `${row.group_id}${row.display_name ? " · " + row.display_name : ""}（${row.messages} 条）`);
      option.value = row.group_id;
      select.appendChild(option);
    });
    if (previous && state.scopes.some((row) => row.group_id === previous)) {
      state.scope = previous;
    } else {
      state.scope = state.scopes[0].group_id;
    }
    select.value = state.scope;
  }

  const scopeParams = (extra) => Object.assign({ group_id: state.scope }, extra || {});

  // ---------------------------------------------------------------- 面板
  async function showPanel(panelId) {
    state.panel = panelId;
    document.querySelectorAll("#nav button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.panel === panelId);
    });
    PANELS.forEach((panel) => {
      const node = $("panel-" + panel.id);
      if (node) node.hidden = panel.id !== panelId;
    });
    $("page-title").textContent = (PANELS.find((p) => p.id === panelId) || {}).title || "";
    location.hash = "#" + panelId;
    try {
      await refresh();
    } catch (error) {
      toast(String(error.message || error), "error");
    }
  }

  async function refresh() {
    switch (state.panel) {
      case "overview": return loadOverview();
      case "memory": return loadMemory();
      case "forest": return loadForest();
      case "people": return loadPeople();
      case "groups": return loadGroups();
      case "schedule": return loadSchedule();
      case "caps": return loadCaps();
      case "config": return loadConfig();
      case "ops": return loadOps();
      default: return undefined;
    }
  }

  // ---------------------------------------------------------------- 概览
  async function loadOverview() {
    const [data, scopes] = await Promise.all([get("overview"), get("scopes")]);
    fillScopes(scopes, true);
    const pill = $("pill-status");
    pill.textContent = data.enabled ? "已启用" : "未启用";
    pill.className = "pill " + (data.enabled ? "on" : "off");
    $("brand-version").textContent = data.version ? "v" + data.version : "Sparking";
    const logo = document.querySelector(".brand .logo");
    if (logo && !logo.dataset.image) {
      const img = document.createElement("img");
      img.src = "../../logo.png";
      img.alt = "Sparking";
      img.width = 34;
      img.height = 34;
      img.style.borderRadius = "10px";
      img.addEventListener("error", () => { img.remove(); });
      logo.textContent = "";
      logo.appendChild(img);
      logo.dataset.image = "1";
    }

    const counts = data.counts || {};
    const cards = [
      ["群/会话", counts.scopes], ["消息", counts.messages], ["分层摘要", counts.summaries],
      ["记忆条目", counts.memories], ["账本条目", counts.catalog], ["待办任务", counts.jobs],
    ];
    const box = $("overview-cards");
    box.textContent = "";
    cards.forEach(([key, value]) => {
      const metric = el("div", "metric");
      metric.appendChild(el("div", "k", key));
      metric.appendChild(el("div", "v", String(value ?? 0)));
      box.appendChild(metric);
    });

    const detail = $("overview-detail");
    detail.textContent = "";
    const rows = [
      ["判定模型", (data.models || {}).judge], ["回复模型", (data.models || {}).reply],
      ["压缩模型", (data.models || {}).summary], ["人格", (data.models || {}).persona],
      ["白名单群", (data.groups || []).join("、") || "（空）"],
      ["情绪", data.mood ? `${data.mood.mood}（${data.mood.intensity}）` : "未启用"],
      ["拓展", data.scripts ? `自定义 ${data.scripts.custom} · 自学习 ${data.scripts.learned}` : "未启用"],
    ];
    rows.forEach(([key, value]) => {
      detail.appendChild(el("b", "", key));
      detail.appendChild(el("span", "", String(value ?? "")));
    });

    const usage = data.usage || {};
    const usageBox = $("overview-usage");
    usageBox.textContent = "";
    const totalCalls = usage.total_calls ?? usage.calls ?? 0;
    usageBox.appendChild(el("div", "", `总调用 ${totalCalls} 次 · 输入 ${usage.prompt_tokens ?? 0} tokens · 输出 ${usage.completion_tokens ?? 0} tokens`));
    const daily = usage.days || usage.daily || [];
    daily.slice(-7).forEach((row) => {
      const line = el("div", "");
      line.appendChild(el("div", "", `${row.day}：${row.calls} 次`));
      usageBox.appendChild(line);
    });
    if (!daily.length && !totalCalls) usageBox.appendChild(el("div", "muted", "近 7 天暂无调用记录"));
  }

  // ---------------------------------------------------------------- 记忆
  function messageNode(row) {
    const title = el("div", "title");
    title.appendChild(el("span", "", row.sender_name || row.sender_id || "?"));
    title.appendChild(el("span", "tag", row.occurred_at || ""));
    if (row.qq_id) title.appendChild(el("span", "tag", "QQ " + row.qq_id));
    const del = actionBtn("删除", async () => {
      const okayDel = await askConfirm({
        title: "删除这条消息", danger: true, okText: "删除",
        message: `${(row.text || "").slice(0, 80)}  （不可恢复）`,
      });
      if (!okayDel) return;
      try {
        const result = await post("message_delete", scopeParams({ message_id: row.message_id }));
        toast(`已删除 ${result.removed} 条`, "ok");
        state.messages = state.messages.filter((item) => item.message_id !== row.message_id);
        renderMessages();
      } catch (error) {
        toast(String(error.message || error), "error");
      }
    }, "danger");
    return itemShell(title, (row.text || "").slice(0, 600), "", [del]);
  }

  function renderMessages() {
    $("msg-count").textContent = state.messages.length ? `（已加载 ${state.messages.length} 条）` : "";
    renderList($("messages"), state.messages, messageNode, "这个会话还没有消息");
  }

  async function loadMemory() {
    const [messages, summaries, catalog] = await Promise.all([
      get("messages", scopeParams({ limit: 40, events: $("msg-events").checked ? 1 : 0 })),
      get("summaries", scopeParams({ limit: 12 })),
      get("catalog", scopeParams({ limit: 20 })),
    ]);
    state.messages = messages.items || [];
    // 游标取本页最旧那条（服务端 before_seq 是"比它更早"）
    state.msgCursor = state.messages.length ? state.messages[0].seq : null;
    renderMessages();
    $("msg-count").textContent = messages.total !== undefined ? `（共 ${messages.total} 条）` : "";

    renderList($("summaries"), summaries, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", `L${row.level} · ${row.title || "（无标题）"}`));
      title.appendChild(el("span", "tag", `#${(row.range || []).join("-")}`));
      if (row.at) title.appendChild(el("span", "tag", row.at));
      return itemShell(title, row.body || "", (row.topics || []).join(" / "), []);
    }, "还没有压缩出摘要（可在「运维」里手动压缩）");

    renderList($("catalog"), catalog, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.subject || row.entry_id || "记忆"));
      title.appendChild(el("span", "tag", row.kind || ""));
      title.appendChild(el("span", "tag", row.status || ""));
      return itemShell(title, row.value || "", row.updated_at ? "更新于 " + row.updated_at : "", []);
    }, "账本还是空的");
  }

  async function loadMoreMessages() {
    if (!state.msgCursor) return toast("没有更早的了");
    const page = await get("messages", scopeParams({ limit: 40, before_seq: state.msgCursor }));
    state.messages = state.messages.concat(page.items || []);
    state.msgCursor = state.messages.length ? state.messages[0].seq : null;
    renderMessages();
  }

  // ---------------------------------------------------------------- 记忆森林
  function forestNode(node, depth) {
    const wrap = el("div", "node");
    const row = el("div", "item");
    const toggle = el("button", "btn ghost", node.count ? "▸" : "·");
    toggle.style.minWidth = "28px";
    row.appendChild(toggle);
    const main = el("div", "main");
    const title = el("div", "title");
    title.appendChild(el("span", "", node.label || node.id));
    if (node.count) title.appendChild(el("span", "tag", `${node.count} 项`));
    if (node.time) {
      const tag = el("span", "tag", node.time);
      tag.title = node.time_granularity || "";
      title.appendChild(tag);
    }
    if (node.kind) title.appendChild(el("span", "tag", node.kind));
    main.appendChild(title);
    if (node.hint) main.appendChild(el("div", "body", node.hint));
    row.appendChild(main);
    const actions = el("div", "actions");
    if (node.editable) {
      actions.appendChild(actionBtn("编辑", () => forestEdit(node)));
      actions.appendChild(actionBtn("删除", async () => {
        if (!(await askConfirm({ title: "删除这个记忆节点", danger: true, okText: "删除",
                                 message: `${node.label}\n${(node.hint || "").slice(0, 80)}` }))) return;
        try {
          await post("memory_node_delete", scopeParams({ node_id: node.id }));
          toast("已删除", "ok");
          loadForest();
        } catch (error) { toast(String(error.message || error), "error"); }
      }, "danger"));
    }
    if (node.type === "scope" || node.type === "category") {
      actions.appendChild(actionBtn("新增", () => forestEdit(null, node)));
    }
    if (actions.childNodes.length) row.appendChild(actions);
    wrap.appendChild(row);
    const kids = el("div", "list");
    kids.hidden = true;
    kids.style.marginLeft = "18px";
    wrap.appendChild(kids);
    if (node.count) {
      toggle.addEventListener("click", async () => {
        const open = !kids.hidden;
        kids.hidden = open;
        toggle.textContent = open ? "▸" : "▾";
        if (!open || kids.dataset.loaded === "1") return;
        try {
          const data = await get("memory_tree", { node: node.id });
          kids.textContent = "";
          const list = (data && data.children) || [];
          if (!list.length) kids.appendChild(el("div", "muted", "（这一层是空的）"));
          list.forEach((child) => kids.appendChild(forestNode(child, depth + 1)));
          kids.dataset.loaded = "1";
        } catch (error) { toast(String(error.message || error), "error"); }
      });
    }
    return wrap;
  }

  let forestGraph = null;

  async function loadForest() {
    // 优先用圆形节点图（pages/tree/forest.js）；渲染器不可用时回退成列表
    if (window.SparkingForest && $("forest-graph")) {
      $("forest-graph").hidden = false;
      $("forest").hidden = true;   // [hidden] 已在 CSS 里强制 display:none
      if (!forestGraph) {
        forestGraph = window.SparkingForest.mount($("forest-graph"), {
          getTree: (nodeId) => nodeId ? get("memory_tree", { node: nodeId })
                                      : get("memory_tree", {}),
          onAddChild: (parent) => forestEdit(null, parent),
          onEdit: (node) => forestEdit(node),
          onDelete: async (node) => {
            if (!(await askConfirm({
              title: "删除这个记忆节点", danger: true, okText: "删除",
              message: `${node.label}
${(node.hint || "").slice(0, 80)}` }))) return;
            try {
              await post("memory_node_delete", scopeParams({ node_id: node.id }));
              toast("已删除", "ok");
              forestGraph.reload($("forest-filter").value);
            } catch (error) { toast(String(error.message || error), "error"); }
          },
        });
      } else {
        forestGraph.reload($("forest-filter").value);
      }
      return;
    }
    // 渲染器不可用：藏掉图形区，只用列表（不留一个空框）
    if ($("forest-graph")) $("forest-graph").hidden = true;
    $("forest").hidden = false;
    const box = $("forest");
    box.textContent = "";
    box.appendChild(el("div", "muted", "加载中…"));
    try {
      const data = await get("memory_tree", {});
      box.textContent = "";
      const keyword = ($("forest-filter").value || "").trim();
      const roots = (data.roots || []).filter(
        (node) => !keyword || String(node.label || "").includes(keyword));
      if (!roots.length) box.appendChild(el("div", "empty", "还没有记忆——去群里聊几句，或点「新增记忆」"));
      roots.forEach((node) => box.appendChild(forestNode(node, 0)));
    } catch (error) {
      box.textContent = "";
      box.appendChild(el("div", "empty", String(error.message || error)));
    }
  }

  let forestEditing = { nodeId: "" };

  async function forestEdit(node, parent) {
    forestEditing = { nodeId: node ? node.id : "" };
    const subject = await askPrompt({
      title: node ? "编辑记忆" : "新增记忆",
      fields: [
        { key: "subject", label: "标题/对象", value: node ? node.label : "" },
        { key: "value", label: "内容", value: node ? node.hint : "", multiline: true },
      ],
      okText: node ? "保存" : "新增",
    });
    if (!subject) return;
    try {
      await post("memory_node_save", scopeParams({
        node_id: forestEditing.nodeId, subject: subject.subject,
        value: subject.value, kind: (node && node.kind) || "fact",
      }));
      toast(forestEditing.nodeId ? "已保存" : "已新增", "ok");
      loadForest();
    } catch (error) { toast(String(error.message || error), "error"); }
  }

  // ---------------------------------------------------------------- 人物
  async function loadPeople() {
    const [impressions, affinity, styles] = await Promise.all([
      get("impressions", scopeParams({ limit: 60 })),
      get("affinity", scopeParams({ limit: 60 })),
      get("styles", scopeParams({ limit: 40 })),
    ]);
    renderList($("impressions"), impressions, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.display_name || row.user_id || "?"));
      title.appendChild(el("span", "tag", String(row.user_id || "")));
      const del = actionBtn("删除", async () => {
        if (!(await askConfirm({ title: "删除印象", danger: true, okText: "删除" }))) return;
        try {
          await post("impression_delete", scopeParams({ user_id: row.user_id }));
          toast("已删除", "ok");
          loadPeople();
        } catch (error) { toast(String(error.message || error), "error"); }
      }, "danger");
      const edit = actionBtn("编辑", () => {
        $("imp-user").value = row.user_id || "";
        $("imp-name").value = row.display_name || "";
        $("imp-text").value = row.impression || "";
        $("imp-tags").value = (row.tags || []).join("，");
        toast("已填入上方表单，改完点保存");
      });
      return itemShell(title, row.impression || "", (row.tags || []).join(" / "), [edit, del]);
    }, "还没有人物印象");

    renderList($("affinity"), affinity, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", String(row.user_id || "")));
      title.appendChild(el("span", "tag", `好感 ${Math.round(row.warmth ?? 50)}`));
      title.appendChild(el("span", "tag", `${row.interactions ?? 0} 次互动`));
      const up = actionBtn("+5", () => adjust(row.user_id, 5));
      const down = actionBtn("-5", () => adjust(row.user_id, -5));
      return itemShell(title, row.note || "", row.updated_at ? String(row.updated_at).slice(0, 19) : "", [up, down]);
    }, "还没有好感度记录");

    renderList($("styles"), styles, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.display_name || row.user_id || "?"));
      title.appendChild(el("span", "tag", `${row.msg_count ?? 0} 条样本`));
      const phrases = (row.catchphrases || []).join(" / ");
      return itemShell(title, row.style_summary || "", phrases ? "口头禅：" + phrases : "", []);
    }, "还没有风格档案");
  }

  async function adjust(userId, delta) {
    try {
      const out = await post("affinity_adjust", scopeParams({ user_id: userId, delta }));
      toast(`好感度 → ${Math.round((out.affinity || {}).warmth ?? 0)}`, "ok");
      loadPeople();
    } catch (error) { toast(String(error.message || error), "error"); }
  }

  // ---------------------------------------------------------------- 群
  async function loadGroups() {
    const [scopes, topics] = await Promise.all([
      get("scopes"), get("topics", scopeParams({ limit: 30 })),
    ]);
    fillScopes(scopes, true);
    $("wl-input").value = state.scopes.map((row) => row.group_id).join(" ");
    renderList($("scopes"), scopes, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.group_id));
      if (row.display_name) title.appendChild(el("span", "tag", row.display_name));
      title.appendChild(el("span", "tag", `${row.messages} 条 · ${row.summaries} 摘要`));
      const use = actionBtn("切到", () => {
        state.scope = row.group_id;
        $("scope-select").value = row.group_id;
        toast("已切换到 " + row.group_id);
        refresh();
      });
      const wipe = actionBtn("清空该群", async () => {
        if (!(await askConfirm({
          title: "清空该群记忆", danger: true, okText: "清空",
          message: `${row.group_id} 的全部消息、摘要、账本与媒体归档都会被删除，不可恢复。`,
        }))) return;
        try {
          const out = await post("group_wipe", { group_id: row.group_id });
          toast(`已删除 ${out.removed} 条消息、${out.media_files} 个媒体文件`, "ok");
          loadGroups();
        } catch (error) { toast(String(error.message || error), "error"); }
      }, "danger");
      return itemShell(title, "", row.last_at ? "最后消息 " + row.last_at : "", [use, wipe]);
    }, "还没有任何群数据");

    renderList($("topics"), topics, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.title || "话题"));
      title.appendChild(el("span", "tag", `${row.hits ?? 0} 次`));
      return itemShell(title, "", row.last_seq ? "最后出现 #" + row.last_seq : "", []);
    }, "还没有记录到话题");
  }

  // ---------------------------------------------------------------- 日程
  async function loadSchedule() {
    const [queue, todos, intents] = await Promise.all([
      get("tasks", { limit: 60 }),
      get("todos", scopeParams({})),
      get("intents", scopeParams({})),
    ]);
    renderQueue(queue);
    renderQueue(queue);

    renderList($("todos"), todos, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.content || ""));
      title.appendChild(el("span", "tag", row.status || ""));
      const done = actionBtn("完成", async () => {
        try { await post("todo_update", { todo_id: row.todo_id, status: "completed" }); toast("已完成", "ok"); loadSchedule(); }
        catch (error) { toast(String(error.message || error), "error"); }
      });
      return itemShell(title, "", "", [done]);
    }, "任务表是空的");

    renderList($("intents"), intents, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.instruction || ""));
      title.appendChild(el("span", "tag", (row.keywords || []).join("/") || "任意"));
      const drop = actionBtn("撤销", async () => {
        try { await post("intent_remove", { intent_id: row.intent_id }); toast("已撤销", "ok"); loadSchedule(); }
        catch (error) { toast(String(error.message || error), "error"); }
      }, "danger");
      return itemShell(title, "", "剩余次数 " + (row.remaining ?? -1), [drop]);
    }, "没有事件条件指令");
  }

  // ---------------------------------------------------------------- 任务队列
  function renderQueue(queue) {
    const data = queue || {};
    const stats = data.stats || {};
    const rows = (data.tasks || []).concat(data.legacy || []);
    $("queue-stats").textContent =
      `排队 ${stats.pending || 0} · 运行 ${stats.running || 0} · 完成 ${stats.done || 0}`
      + ` · 失败 ${stats.failed || 0} · 过期 ${stats.expired || 0}`;
    renderList($("plans"), rows, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.title || row.detail || row.kind));
      title.appendChild(el("span", "tag", row.kind));
      title.appendChild(el("span", "tag", row.status));
      if (row.long_term) title.appendChild(el("span", "tag", "长期"));
      if (row.max_runs !== undefined && row.max_runs >= 0) {
        title.appendChild(el("span", "tag", `${row.runs}/${row.max_runs} 次`));
      } else if (row.max_runs === -1) {
        title.appendChild(el("span", "tag", "不限次"));
      }
      if (row.interval_minutes) title.appendChild(el("span", "tag", `每 ${row.interval_minutes} 分`));
      if (row.window && row.window.daily) {
        title.appendChild(el("span", "tag", `${row.window.daily[0]}-${row.window.daily[1]}`));
      }
      const when = row.next_run_at ? "下次 " + String(row.next_run_at).slice(0, 19) : "";
      const note = row.last_error ? "上次失败：" + row.last_error
        : (row.last_result ? "上次：" + row.last_result : "");
      const cancel = actionBtn("撤销", async () => {
        try {
          await post(row.legacy ? "task_cancel" : "task_cancel", { task_id: row.task_id });
          toast("已撤销", "ok");
          loadSchedule();
        } catch (error) { toast(String(error.message || error), "error"); }
      }, "danger");
      return itemShell(title, note, when, [cancel]);
    }, "队列是空的——可以让 bot 自己排任务，或在上面手动加");
  }

  async function addTaskFromForm() {
    const detail = $("plan-detail").value.trim();
    if (!detail) return toast("先写要做什么", "error");
    const interval = Number($("task-interval").value || 0);
    const runs = Number($("task-runs").value || 1);
    const daily = $("task-window").value.trim();
    const kind = $("task-kind").value;
    try {
      await post("task_add", scopeParams({
        kind, detail, delay_minutes: Number($("plan-hours").value || 0) * 60,
        interval_minutes: interval, max_runs: runs, daily_window: daily,
      }));
      toast("已排进队列", "ok");
      $("plan-detail").value = "";
      loadSchedule();
    } catch (error) { toast(String(error.message || error), "error"); }
  }

  // ---------------------------------------------------------------- 能力
  async function loadCaps() {
    const [caps, extensions] = await Promise.all([get("capabilities"), get("extensions")]);
    state.capabilities = caps.items || [];
    $("caps-count").textContent = `（共 ${caps.total ?? state.capabilities.length} 个工具）`;
    renderCapabilities();

    const bundles = extensions.bundles || [];
    const scripts = extensions.scripts || [];
    const rows = bundles.map((bundle) => ({
      id: bundle.id, name: bundle.name || bundle.id, description: bundle.description || "",
      enabled: bundle.enabled !== false, tools: (bundle.tools || []).length, kind: "内置包",
    })).concat(scripts.map((script) => ({
      id: script.id, name: script.name || script.id, description: script.description || "",
      enabled: script.enabled !== false, tools: (script.tools || []).length,
      kind: script.origin === "learned" ? "自学习技能" : "脚本拓展",
    })));
    renderList($("extensions"), rows, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.name));
      title.appendChild(el("span", "tag", row.kind));
      title.appendChild(el("span", "tag", `${row.tools} 个工具`));
      title.appendChild(el("span", "tag", row.enabled ? "已启用" : "已关闭"));
      const toggle = actionBtn(row.enabled ? "关闭" : "启用", async () => {
        try {
          await post("extension_toggle", { id: row.id, enabled: !row.enabled });
          toast(row.enabled ? "已关闭" : "已启用", "ok");
          loadCaps();
        } catch (error) { toast(String(error.message || error), "error"); }
      }, row.enabled ? "warn" : "primary");
      const reload = actionBtn("重载", async () => {
        try {
          const out = await post("extension_reload", { id: row.id });
          toast(out.problems && out.problems.length ? `重载完成，${out.problems.length} 个问题` : "已重载", "ok");
          loadCaps();
        } catch (error) { toast(String(error.message || error), "error"); }
      });
      return itemShell(title, row.description, "", [toggle, reload]);
    }, "没有额外拓展");
  }

  function renderCapabilities() {
    const keyword = ($("caps-search").value || "").trim().toLowerCase();
    const rows = state.capabilities.filter((row) =>
      !keyword || row.name.toLowerCase().includes(keyword)
      || (row.description || "").toLowerCase().includes(keyword));
    renderList($("capabilities"), rows.slice(0, 300), (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.name));
      title.appendChild(el("span", "tag", row.source));
      if (!row.active) title.appendChild(el("span", "tag", "未启用"));
      return itemShell(title, row.description || "", "", []);
    }, "没有匹配的工具");
  }

  // ---------------------------------------------------------------- 配置
  async function loadConfig() {
    state.config = await get("config");
    renderConfig();
  }

  function renderConfig() {
    const keyword = ($("cfg-search").value || "").trim().toLowerCase();
    const groups = {};
    state.config.forEach((row) => {
      if (keyword && !(row.key.toLowerCase().includes(keyword)
        || (row.description || "").toLowerCase().includes(keyword))) return;
      const name = row.group || "其它";
      (groups[name] = groups[name] || []).push(row);
    });
    const box = $("config");
    box.textContent = "";
    Object.keys(groups).forEach((name) => {
      const section = el("div", "cfg-group");
      section.appendChild(el("h3", "", name));
      groups[name].forEach((row) => {
        const item = el("div", "cfg-item");
        const key = el("div", "key", row.key);
        item.appendChild(key);

        let input;
        if (row.type === "bool") {
          input = el("input");
          input.type = "checkbox";
          input.checked = Boolean(row.value);
        } else if (row.type === "int" || row.type === "float") {
          input = el("input");
          input.type = "number";
          input.step = row.type === "float" ? "0.1" : "1";
          input.value = row.value ?? row.default ?? "";
        } else if (Array.isArray(row.value)) {
          input = el("input");
          input.value = (row.value || []).join(", ");
        } else {
          input = el("input");
          input.value = row.value ?? "";
        }
        input.addEventListener("change", () => saveConfig(row, input));
        item.appendChild(input);

        const save = actionBtn("保存", () => saveConfig(row, input));
        item.appendChild(save);
        if (row.description) item.appendChild(el("div", "desc", row.description));
        section.appendChild(item);
      });
      box.appendChild(section);
    });
    if (!box.childNodes.length) box.appendChild(el("div", "empty", "没有匹配的配置项"));
  }

  async function saveConfig(row, input) {
    let value;
    if (row.type === "bool") value = input.checked;
    else if (row.type === "int" || row.type === "float") value = Number(input.value);
    else if (Array.isArray(row.default)) {
      value = String(input.value || "").split(/[,，\s]+/).filter(Boolean);
    } else value = input.value;
    try {
      await post("config_set", { key: row.key, value });
      row.value = value;
      toast(`${row.key} 已保存`, "ok");
    } catch (error) {
      toast(String(error.message || error), "error");
    }
  }

  // ---------------------------------------------------------------- 运维
  async function loadOps() {
    const [overview, backups, events] = await Promise.all([
      get("overview"), get("backups"), get("events", scopeParams({ limit: 30 })),
    ]);
    const mood = overview.mood;
    const view = $("mood-view");
    view.textContent = "";
    if (mood) {
      view.appendChild(el("b", "", "当前情绪"));
      view.appendChild(el("span", "", `${mood.mood}（强度 ${mood.intensity}）${mood.note ? " · " + mood.note : ""}`));
    } else {
      view.appendChild(el("span", "muted", "情绪系统未启用"));
    }
    renderList($("backups"), backups, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.name));
      title.appendChild(el("span", "tag", `${Math.round((row.size || 0) / 1024)} KB`));
      const restore = actionBtn("恢复", async () => {
        if (!(await askConfirm({
          title: "从备份恢复", danger: true, okText: "恢复",
          message: `用 ${row.name} 覆盖当前记忆库，当前数据会被替换。`,
        }))) return;
        try { await post("restore", { name: row.name }); toast("已恢复", "ok"); }
        catch (error) { toast(String(error.message || error), "error"); }
      }, "warn");
      return itemShell(title, "", "", [restore]);
    }, "还没有备份");

    renderList($("events"), events, (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.sender_name || "系统事件"));
      if (row.at) title.appendChild(el("span", "tag", row.at));
      return itemShell(title, (row.text || "").slice(0, 300), "", []);
    }, "没有通知/请求记录");
  }

  // ---------------------------------------------------------------- 命令
  async function sendOrder(kind) {
    const text = $("order-text").value.trim();
    if (!text) return toast("先写一句要它做的事", "error");
    const output = $("order-output");
    output.hidden = false;
    output.textContent = kind === "order" ? "已下达，bot 正在执行（可去群里看它的汇报）…" : "思考中…";
    try {
      const out = kind === "order"
        ? await post("order", scopeParams({ text }))
        : await post("ask", scopeParams({ question: text }));
      output.textContent = kind === "order"
        ? `已派发：${out.order || text}\n目标会话：${out.group_id || state.scope}\n执行方式：${out.dispatched ? "后台 agent 循环（带工具）" : "仅写入任务表"}`
        : `bot 回答：${out.answer || "（空）"}`;
      toast(kind === "order" ? "指令已下达" : "已回复", "ok");
    } catch (error) {
      output.textContent = String(error.message || error);
      toast(String(error.message || error), "error");
    }
  }

  // ---------------------------------------------------------------- 绑定
  function bind() {
    const nav = $("nav");
    PANELS.forEach((panel) => {
      const btn = el("button");
      btn.dataset.panel = panel.id;
      btn.appendChild(el("span", "ico", panel.icon));
      btn.appendChild(el("span", "", panel.title));
      btn.addEventListener("click", () => showPanel(panel.id));
      nav.appendChild(btn);
    });
    $("btn-refresh").addEventListener("click", () => refresh().catch((error) => toast(String(error.message || error), "error")));
    $("scope-select").addEventListener("change", (event) => {
      state.scope = event.target.value;
      refresh().catch((error) => toast(String(error.message || error), "error"));
    });
    $("btn-order").addEventListener("click", () => sendOrder("order"));
    $("btn-ask").addEventListener("click", () => sendOrder("ask"));
    $("btn-msg-load").addEventListener("click", () => loadMemory().catch((error) => toast(String(error.message || error), "error")));
    $("btn-msg-more").addEventListener("click", () => loadMoreMessages().catch((error) => toast(String(error.message || error), "error")));
    $("msg-search").addEventListener("keydown", (event) => { if (event.key === "Enter") loadMemory(); });
    $("caps-search").addEventListener("input", renderCapabilities);
    $("cfg-search").addEventListener("input", renderConfig);
    $("btn-forest-refresh").addEventListener("click", loadForest);
    $("forest-filter").addEventListener("input", loadForest);
    $("btn-forest-new").addEventListener("click", () => forestEdit(null, null));
    $("btn-imp-save").addEventListener("click", async () => {
      const userId = $("imp-user").value.trim();
      if (!userId) return toast("先填 QQ 号", "error");
      try {
        await post("impression_save", scopeParams({
          user_id: userId, display_name: $("imp-name").value.trim(),
          impression: $("imp-text").value.trim(), tags: $("imp-tags").value.trim(),
        }));
        toast("已保存", "ok");
        loadPeople();
      } catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-wl-save").addEventListener("click", async () => {
      const groups = $("wl-input").value.split(/[,，\s]+/).filter(Boolean);
      try { await post("whitelist_set", { groups }); toast("白名单已保存", "ok"); loadGroups(); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-plan-add").addEventListener("click", addTaskFromForm);
    $("btn-todo-add").addEventListener("click", async () => {
      const content = $("todo-text").value.trim();
      if (!content) return toast("先写任务内容", "error");
      try { await post("todo_add", scopeParams({ content })); toast("已加任务", "ok"); $("todo-text").value = ""; loadSchedule(); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-todo-clear").addEventListener("click", async () => {
      if (!(await askConfirm({ title: "清空任务表", danger: true, okText: "清空" }))) return;
      try { await post("todo_clear", scopeParams({})); toast("已清空", "ok"); loadSchedule(); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-intent-add").addEventListener("click", async () => {
      const instruction = $("intent-text").value.trim();
      if (!instruction) return toast("先写命中后做什么", "error");
      const keywords = $("intent-keys").value.split(/[,，\s]+/).filter(Boolean);
      try { await post("intent_add", { instruction, keywords }); toast("已立指令", "ok"); loadSchedule(); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-compress").addEventListener("click", async () => {
      try { const out = await post("compress", scopeParams({})); toast(`已生成 ${out.created ?? 0} 条摘要`, "ok"); loadMemory(); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-reflect").addEventListener("click", async () => {
      toast("让 bot 自我总结中…");
      try { await post("reflect", scopeParams({})); toast("总结完成", "ok"); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-backup").addEventListener("click", async () => {
      try { const out = await post("backup", {}); toast("已备份 " + out.backup, "ok"); loadOps(); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-wipe-group").addEventListener("click", async () => {
      if (!(await askConfirm({
        title: "清空当前会话", danger: true, okText: "清空",
        message: `${state.scope} 的全部记忆与消息记录都会被删除，不可恢复。`,
      }))) return;
      try { const out = await post("group_wipe", scopeParams({})); toast(`已删除 ${out.removed} 条消息`, "ok"); refresh(); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-clear-media").addEventListener("click", async () => {
      if (!(await askConfirm({
        title: "清空媒体归档", danger: true, okText: "清空",
        message: "图片/文件/语音等归档文件都会被删除（数据库记录保留）。",
      }))) return;
      try { const out = await post("media_clear", {}); toast(`已删除 ${out.removed_files} 个文件`, "ok"); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-clear-stickers").addEventListener("click", async () => {
      if (!(await askConfirm({ title: "清空表情包库", danger: true, okText: "清空" }))) return;
      try { const out = await post("sticker_clear", {}); toast(`已删除 ${out.removed ?? 0} 张`, "ok"); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-wipe-all").addEventListener("click", async () => {
      const word = await askConfirm({
        title: "清空全部记忆", danger: true, okText: "确认清空", confirmWord: "确认清空",
        message: "全部会话的消息、摘要、账本、话题、印象与媒体归档都会被删除，不可恢复。",
      });
      if (word !== "确认清空") return toast("已取消");
      try { const out = await post("wipe_all", { confirm: word }); toast(`已清除 ${out.removed} 条记录`, "ok"); refresh(); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-mood").addEventListener("click", async () => {
      try {
        await post("mood_set", {
          mood: $("mood-name").value.trim() || "平静",
          intensity: Number($("mood-intensity").value || 0.5),
          note: $("mood-note").value.trim(),
        });
        toast("已设置", "ok"); loadOps();
      } catch (error) { toast(String(error.message || error), "error"); }
    });
  }

  async function start() {
    bind();
    const initial = (location.hash || "").replace("#", "");
    const first = PANELS.some((panel) => panel.id === initial) ? initial : "overview";
    try {
      const scopes = await get("scopes");
      fillScopes(scopes, false);
    } catch (error) {
      toast(String(error.message || error), "error");
    }
    await showPanel(first);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
