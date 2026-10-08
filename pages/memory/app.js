// ===== 记忆森林渲染器（与 pages/tree/forest.js 同源，内联以保证插件页可用）=====
// 改渲染器请改 pages/tree/forest.js，然后跑 tools/sync_forest.py 同步这一份。

// Sparking 记忆森林 —— 圆形节点 + 连线 的真实可视化（零依赖，纯 SVG）
// 用法：window.SparkingForest.mount(container, { getTree, onEdit, onDelete, onAddChild })
//   getTree(nodeId|null) -> Promise<{roots:[...]}|{children:[...]}>
// 特性：整洁树布局（父子居中对齐）、悬停详情浮层、左键菜单（编辑/删除/加子节点/展开收拢）、
//       滚轮缩放、拖拽平移、懒加载（点开才拉子节点）、按类型着色、数量徽标。
(function () {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  // 亮色主题下调过的配色：节点色够深，白色文字/白描边才压得住（主色跟 AstrBot 的 #3c96ca 同族）
  const COLORS = {
    scope: "#3c96ca",       // 会话（群/私聊）
    category: "#7c6bd6",    // 类目
    summary: "#2fa87a",     // 分层摘要
    catalog: "#e0a01c",     // 记忆账本
    impression: "#d2699f",  // 人物印象
    topic: "#2aa8bf",       // 群话题
    message: "#93a0b4",     // 证据消息
    default: "#a3adba",
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

    // 提示条（空树/加载失败）：单独一个元素，别用 container.innerHTML 覆盖——
    // 那会把上面已经建好的 svg/tip/menu 一起销毁，之后再刷新就永远画不出图了（实录踩过）。
    const notice = document.createElement("div");
    notice.className = "forest-empty";
    notice.hidden = true;
    container.appendChild(notice);

    function showNotice(text) {
      notice.textContent = text;
      notice.hidden = false;
      state.roots = [];
      state.nodes.clear();
      state.children.clear();
      render();
    }

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
          stroke: "#cfd8e3",
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
          stroke: state.selected === node.id ? "#1565c0" : "#ffffff",
          "stroke-width": state.selected === node.id ? 2.5 : 1.5,
        });
        group.appendChild(circle);

        const label = svgEl("text", {
          x: NODE_R + 10, y: 5,
          fill: "#1b1c1d", "font-size": "12.5px",
        });
        label.textContent = String(node.label || node.id).slice(0, 26);
        group.appendChild(label);

        if (node.count) {
          const badge = svgEl("text", {
            x: 0, y: 4, "text-anchor": "middle",
            fill: "#ffffff", "font-size": "10px", "font-weight": "700",
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
        showNotice(`加载失败：${String(error.message || error)}`);
        return;
      }
      notice.hidden = true;
      const keyword = String(filter || "").trim();
      state.roots = ((data && data.roots) || [])
        .filter((node) => !keyword || String(node.label || "").includes(keyword))
        .map((node) => {
          register(node, null);
          return node.id;
        });
      if (!state.roots.length) {
        showNotice("还没有记忆——先去群里聊几句，或点「新增记忆」");
        return;
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
    { id: "overview", icon: "overview", title: "概览", desc: "运行状态、给 bot 下令、模型用量" },
    { id: "memory", icon: "memory", title: "记忆", desc: "消息、分层摘要与记忆账本" },
    { id: "forest", icon: "forest", title: "记忆森林", desc: "圆形节点＝一条记忆，连线＝关系；点节点可编辑" },
    { id: "people", icon: "people", title: "人物", desc: "人物印象（跨群互通）、好感度与说话风格" },
    { id: "groups", icon: "groups", title: "群与会话", desc: "会话列表、白名单与群话题" },
    { id: "schedule", icon: "schedule", title: "日程与任务", desc: "任务队列、待办与事件条件指令" },
    { id: "caps", icon: "caps", title: "能力与拓展", desc: "bot 实际可调用的工具与已装载技能" },
    { id: "inject", icon: "inject", title: "提示词注入", desc: "以【强制规则】形式追加到系统提示，勾选即生效" },
    { id: "culture", icon: "culture", title: "群风格与心理", desc: "它学到的说话习惯、群内黑话、对你的了解与情绪记忆" },
    { id: "learn", icon: "learn", title: "快速学习", desc: "导入导出的群聊天记录，让它一次学会这些群" },
    { id: "backup", icon: "backup", title: "记忆备份", desc: "导出/导入它的记忆（跨 bot 搬运，嵌入模型要一致）" },
    { id: "config", icon: "config", title: "配置", desc: "全部配置项，改完即时生效、不用重启" },
    { id: "ops", icon: "ops", title: "运维", desc: "维护、危险操作、情绪与通知事件" },
  ];

  // 侧栏图标：零依赖内联 SVG（描边风格，跟 AstrBot 的线性图标一致）
  const ICONS = {
    overview: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.6"/>'
      + '<rect x="3.5" y="13.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.6"/>',
    memory: '<rect x="4" y="3.5" width="16" height="17" rx="2.2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    forest: '<circle cx="5.5" cy="12" r="2.4"/><circle cx="18.5" cy="6.5" r="2.4"/><circle cx="18.5" cy="17.5" r="2.4"/>'
      + '<path d="M7.7 11.1 16.3 7.4M7.7 12.9 16.3 16.6"/>',
    people: '<circle cx="12" cy="8" r="3.4"/><path d="M5.2 20c0-3.7 3.1-6.2 6.8-6.2S18.8 16.3 18.8 20"/>',
    groups: '<rect x="3.5" y="5" width="13.5" height="11" rx="2.4"/><path d="M7.5 19h10.6a2.4 2.4 0 0 0 2.4-2.4V9.2"/>',
    schedule: '<circle cx="12" cy="12" r="8.4"/><path d="M12 7.4V12l3.1 2.1"/>',
    caps: '<path d="M12 3.6 14.7 9l6 .9-4.3 4.2 1 6-5.4-2.9-5.4 2.9 1-6L3.3 9.9l6-.9Z"/>',
    config: '<path d="M4 7h16M4 12h16M4 17h16"/><circle cx="9.5" cy="7" r="2.1"/><circle cx="15" cy="12" r="2.1"/><circle cx="8" cy="17" r="2.1"/>',
    ops: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.4"/><path d="M7.8 10 10.6 12.4 7.8 14.8M12.8 15h3.6"/>',
    inject: '<path d="M12 3.5v9"/><path d="M12 15.5v5"/><circle cx="12" cy="13" r="2.6"/>'
      + '<path d="M5.5 6.5h4M5.5 10h3M14.5 6.5h4M14.5 10h3"/>',
    culture: '<path d="M4 5.5h16v11H8.5L4.5 20z"/><path d="M8 10h8M8 13h5"/>',
    learn: '<path d="M12 4 3.5 8.5 12 13l8.5-4.5z"/><path d="M6 10.5V16c0 1.7 2.7 3 6 3s6-1.3 6-3v-5.5"/><path d="M20.5 8.5V14"/>',
    backup: '<path d="M12 3.5v10"/><path d="M8 9.5 12 13.5 16 9.5"/>' + '<path d="M4.5 16.5v2.2a1.8 1.8 0 0 0 1.8 1.8h11.4a1.8 1.8 0 0 0 1.8-1.8v-2.2"/>',
  };

  function iconSvg(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = ICONS[name] || "";
    return svg;
  }

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
    const current = PANELS.find((p) => p.id === panelId) || {};
    $("page-title").textContent = current.title || "";
    const sub = $("page-sub");
    if (sub) sub.textContent = current.desc || "";
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
      case "inject": return loadInjections();
      case "culture": return loadCulture();
      case "learn": return loadLearn();
      case "backup": return loadBackup();
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
      img.src = "logo.png";   // 页面同级副本：跨出插件目录的路径在插件页里会 404
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
    // 后端给的是 totals.{calls,prompt_tokens,completion_tokens}（webui._read_overview）
    // ——以前这里读 total_calls/prompt_tokens，于是条形图有数、上面一行全是 0
    const totals = usage.totals || {};
    const totalCalls = totals.calls ?? usage.total_calls ?? usage.calls ?? 0;
    const promptTokens = totals.prompt_tokens ?? usage.prompt_tokens ?? 0;
    const completionTokens = totals.completion_tokens ?? usage.completion_tokens ?? 0;
    usageBox.appendChild(el("div", "", `总调用 ${totalCalls} 次 · 输入 ${promptTokens} tokens · 输出 ${completionTokens} tokens`));
    const daily = (usage.daily || usage.days || []).slice(-7);
    const peak = daily.reduce((top, row) => Math.max(top, Number(row.calls) || 0), 0) || 1;
    daily.forEach((row) => {
      const line = el("div", "usage-row");
      const dayLabel = String(row.day || "").slice(5) || String(row.day || "");
      const dayNode = el("span", "usage-day", dayLabel);
      dayNode.title = String(row.day || "");
      line.appendChild(dayNode);
      const track = el("div", "usage-track");
      const bar = el("div", "bar");
      bar.style.width = Math.max(2, Math.round(((Number(row.calls) || 0) / peak) * 100)) + "%";
      track.appendChild(bar);
      line.appendChild(track);
      line.appendChild(el("span", "usage-count", `${row.calls} 次`));
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
      // 自定义触发条件：有条件的任务要看得出来（还在等条件 / 已经判假几次）
      if (row.condition) {
        title.appendChild(el("span", "tag",
          row.condition_checks ? `等条件（判过 ${row.condition_checks} 次）` : "等条件"));
      }
      const when = row.next_run_at ? "下次 " + String(row.next_run_at).slice(0, 19) : "";
      const note = (row.condition ? `条件：${row.condition}` + (row.last_error ? "；" : "")
        : "") + (row.last_error ? "上次失败：" + row.last_error
        : (row.last_result ? "上次：" + row.last_result : ""));
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
  // ---------------------------------------------------------------- 提示词注入
  // ---------------------------------------------------------------- 快速学习（导入聊天记录）
  const CHUNK_BYTES = 512 * 1024;      // 每片 512KB：桥接只发 JSON，必须切片

  function readChunk(file, start, end) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const text = String(reader.result || "");
        resolve(text.split(",", 2)[1] || "");
      };
      reader.onerror = () => reject(new Error("读取文件失败"));
      reader.readAsDataURL(file.slice(start, end));
    });
  }

  async function runImport() {
    const input = $("learn-file");
    const file = input && input.files && input.files[0];
    if (!file) return toast("先选一个 zip", "error");
    if (!/\.zip$/i.test(file.name)) return toast("要 zip 文件", "error");
    const uploadId = "up" + Date.now().toString(36);
    const total = Math.ceil(file.size / CHUNK_BYTES);
    const box = $("learn-log");
    box.hidden = false;
    box.textContent = "";
    const say = (line) => { box.textContent += line + "\n"; box.scrollTop = box.scrollHeight; };
    say(`开始上传 ${file.name}（${(file.size / 1048576).toFixed(1)}MB，${total} 片）`);
    const button = $("btn-learn-run");
    button.disabled = true;
    try {
      for (let index = 0; index < total; index += 1) {
        const data = await readChunk(file, index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES);
        await post("import_chatlog_upload", { upload_id: uploadId, index, data });
        if (index % 5 === 4 || index === total - 1) {
          say(`  已上传 ${index + 1}/${total} 片`);
        }
      }
      say("上传完成，正在解析…（群多的包要等一会儿）");
      const learn = $("learn-learn") ? $("learn-learn").checked : true;
      const report = await post("import_chatlog_finish", {
        upload_id: uploadId, filename: file.name, learn,
      });
      say("");
      say(`共 ${report.groups.length} 个群、${report.messages} 条消息；` +
          `学到说话风格 ${report.style || 0} 条、黑话 ${report.lexicon || 0} 个；` +
          `新建/更新印象 ${report.impressions} 条、人物档案 ${report.profiles} 条`);
      (report.groups || []).forEach((group) => {
        const tag = group.whitelisted ? "白名单（含印象学习）" : "仅入库";
        const culture = `；风格 ${group.style || 0} 条 / 黑话 ${group.lexicon || 0} 个`
          + (group.culture_skip ? ` ⚠ ${group.culture_skip}` : "")
          + (group.culture_error ? ` ⚠ ${group.culture_error}` : "");
        const learn = group.whitelisted
          ? `；印象 ${group.impressions || 0} 条 / 档案 ${group.profiles || 0} 条`
            + `（试了 ${group.attempted || 0} 人`
            + (group.skip_no_samples ? `，样本不足 ${group.skip_no_samples} 人` : "")
            + (group.unparsed ? `，模型输出没解析出来 ${group.unparsed} 人` : "")
            + (group.call_failed ? `，模型没调通 ${group.call_failed} 人` : "")
            + (group.failed ? `，写库失败 ${group.failed} 次` : "")
            + (group.provider ? `，用模型 ${group.provider}` : "")
            + "）"
          : "";
        say(`  · ${group.name}（${group.group_id}）：${group.messages} 条 / ${group.people} 人 — ${tag}${culture}${learn}` +
            (group.error ? ` ⚠ ${group.error}` : "") +
            (group.learn_error ? ` ⚠ ${group.learn_error}` : "") +
            (group.skip ? ` ⚠ ${group.skip}` : ""));
      });
      toast("导入完成", "ok");
      loadLearn();
    } catch (error) {
      say("失败：" + String(error.message || error));
      try { await post("import_chatlog_abort", { upload_id: uploadId }); } catch (_) { /* 忽略 */ }
      toast(String(error.message || error), "error");
    } finally {
      button.disabled = false;
    }
  }

  async function loadLearn() {
    const box = $("learn-scopes");
    if (!box) return;
    box.textContent = "";
    box.appendChild(el("div", "muted", "加载中…"));
    let data;
    try {
      data = await get("imports");
    } catch (error) {
      box.textContent = "";
      box.appendChild(el("div", "empty", String(error.message || error)));
      return;
    }
    renderList(box, data.scopes || [], (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.name || row.group_id));
      title.appendChild(el("span", "tag", row.whitelisted ? "白名单" : "非白名单"));
      title.appendChild(el("span", "tag", `${row.messages} 条`));
      return itemShell(title, row.group_id, String(row.last_active || "").slice(0, 16), []);
    }, "还没有导入过任何群");
  }

  // ---------------------------------------------------------------- 记忆导出 / 导入
  function formatSize(bytes) {
    const value = Number(bytes || 0);
    if (value >= 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + " MB";
    if (value >= 1024) return Math.round(value / 1024) + " KB";
    return value + " B";
  }

  async function loadBackup() {
    const box = $("backup-files");
    if (!box) return;
    box.textContent = "";
    box.appendChild(el("div", "muted", "加载中…"));
    let data;
    try {
      data = await get("backup_memory");
    } catch (error) {
      box.textContent = "";
      box.appendChild(el("div", "empty", String(error.message || error)));
      return;
    }
    const embed = $("export-embed");
    if (embed) {
      const counts = data.counts || {};
      embed.textContent = "当前嵌入模型：" + (data.current_embedding_model || "（未配置）")
        + "；库里现有 " + (counts.messages || 0) + " 条消息 / " + (counts.summaries || 0)
        + " 条摘要 / " + (counts.memories || 0) + " 条记忆账本 / " + (counts.embeddings || 0) + " 条向量";
    }
    renderList(box, data.exports || [], (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.name));
      title.appendChild(el("span", "tag", formatSize(row.size)));
      return itemShell(title, "服务器 exports 目录", "", []);
    }, "还没有导出过（点上面的「导出并下载」）");
  }

  async function runExport(download) {
    const log = $("export-log");
    const show = (value) => { if (log) { log.hidden = false; log.textContent = value; } };
    show(download ? "正在打包记忆…" : "正在打包并写入服务器…");
    const bridge = await bridgeReady();
    try {
      if (download && bridge && typeof bridge.download === "function") {
        const result = await bridge.download(endpointOf("memory_export"), {}, "");
        show("已开始下载：" + ((result && result.filename) || "记忆包")
          + "\n（若浏览器没弹出下载，改用「只在服务器上存一份」，文件会写进插件数据目录的 exports/）");
        toast("导出完成", "ok");
        loadBackup();
        return;
      }
      const data = await post("memory_export_save", {});
      show("导出包已生成在服务器：" + (data.name || "")
        + "\n（插件数据目录 / exports/ 下）");
      toast("已存到服务器", "ok");
      loadBackup();
    } catch (error) {
      show("导出失败：" + String(error.message || error));
      toast(String(error.message || error), "error");
    }
  }

  function readPart(file, start, end) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
      reader.onerror = () => reject(new Error("读取文件失败"));
      reader.readAsDataURL(file.slice(start, end));
    });
  }

  async function runMemoryImport() {
    const input = $("backup-file");
    const log = $("import-log");
    const file = input && input.files && input.files[0];
    if (!file) return toast("先选一个记忆包（.zip）", "error");
    const show = (value) => { if (log) { log.hidden = false; log.textContent = value; } };
    const uploadId = "mem_" + Date.now().toString(36);
    const chunk = 512 * 1024;
    try {
      const total = Math.max(1, Math.ceil(file.size / chunk));
      show("开始上传 " + file.name + "（" + formatSize(file.size) + "，" + total + " 片）");
      for (let index = 0; index < total; index += 1) {
        const data = await readPart(file, index * chunk, (index + 1) * chunk);
        await post("memory_import_upload", { upload_id: uploadId, index, data });
        if (index % 5 === 4 || index === total - 1) show("  已上传 " + (index + 1) + "/" + total + " 片");
      }
      show("上传完成，正在合并进记忆…（包大的话要等一会儿）");
      const report = await post("memory_import_finish", { upload_id: uploadId, filename: file.name });
      const pkg = report.package || {};
      const lines = [];
      lines.push("包来自插件 " + (pkg.plugin_version || "未知版本") + "，导出时间 " + String(pkg.exported_at || "").slice(0, 19));
      lines.push("导入 " + report.messages + " 条消息（其中 " + report.reused_messages
        + " 条库里已有、跳过）；涉及 " + report.scopes + " 个会话");
      lines.push("嵌入模型：" + (report.embedding_note || ""));
      if (report.embeddings_skipped) {
        lines.push("（跳过 " + report.embeddings_skipped + " 条向量——文本与记忆照常可用）");
      }
      const tables = report.tables || {};
      const parts = Object.keys(tables).filter((key) => tables[key] && tables[key].added)
        .map((key) => key + " " + tables[key].added);
      if (parts.length) lines.push("各表新增：" + parts.join("、"));
      show(lines.join("\n"));
      toast("记忆导入完成", "ok");
      loadBackup();
    } catch (error) {
      show("导入失败：" + String(error.message || error));
      toast(String(error.message || error), "error");
    }
  }

  // ---------------------------------------------------------------- 群风格与心理
  async function loadCulture() {
    const groups = [["styleRules", "style_rules"], ["lexicon", "lexicon"],
                    ["profiles", "profiles"], ["moodEvents", "mood_events"]];
    groups.forEach(([boxId]) => {
      const box = $(boxId);
      if (box) { box.textContent = ""; box.appendChild(el("div", "muted", "加载中…")); }
    });
    let data;
    try {
      data = await get("culture", { group_id: state.scope });
    } catch (error) {
      groups.forEach(([boxId]) => {
        const box = $(boxId);
        if (box) { box.textContent = ""; box.appendChild(el("div", "empty", String(error.message || error))); }
      });
      return;
    }
    renderList($("styleRules"), data.style_rules || [], (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", `当${row.situation}时`));
      title.appendChild(el("span", "tag", `用过 ${row.hits} 次`));
      const del = actionBtn("删除", async () => {
        if (!(await askConfirm({ title: "删掉这条规律", danger: true, okText: "删除",
                                 message: `当${row.situation}时，可以${row.style}` }))) return;
        try { await post("style_rule_delete", { rule_id: row.rule_id }); toast("已删除", "ok"); loadCulture(); }
        catch (error) { toast(String(error.message || error), "error"); }
      }, "warn");
      return itemShell(title, `可以${row.style}`, "", [del]);
    }, "还没学到这个群的说话习惯（再聊一阵就有了）");

    renderList($("lexicon"), data.lexicon || [], (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.term));
      title.appendChild(el("span", "tag", row.meaning ? `用过 ${row.uses} 次` : "还没搞懂"));
      const edit = actionBtn("改解释", async () => {
        const answer = await askPrompt({
          title: `「${row.term}」是什么意思`,
          fields: [{ key: "meaning", label: "解释", value: row.meaning || "", multiline: true }],
          okText: "保存",
        });
        if (!answer) return;
        try {
          await post("lexicon_save", { group_id: state.scope, term: row.term, meaning: answer.meaning });
          toast("已保存", "ok"); loadCulture();
        } catch (error) { toast(String(error.message || error), "error"); }
      });
      const del = actionBtn("删除", async () => {
        try { await post("lexicon_delete", { term_id: row.term_id }); toast("已删除", "ok"); loadCulture(); }
        catch (error) { toast(String(error.message || error), "error"); }
      }, "warn");
      return itemShell(title, row.meaning || "（等它再出现几次我再学着理解）", "", [edit, del]);
    }, "这个词库还是空的");

    renderList($("profiles"), data.profiles || [], (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.display_name || row.user_id));
      title.appendChild(el("span", "tag", `聊过 ${row.know_counts} 次`));
      const del = actionBtn("删除", async () => {
        if (!(await askConfirm({ title: "删掉这份档案", danger: true, okText: "删除",
                                 message: row.display_name || row.user_id }))) return;
        try { await post("profile_delete", { person_id: row.person_id }); toast("已删除", "ok"); loadCulture(); }
        catch (error) { toast(String(error.message || error), "error"); }
      }, "warn");
      return itemShell(title, (row.points || []).join("；"), "分类:内容:权重", [del]);
    }, "还没有攒下对谁的了解");

    renderList($("moodEvents"), data.mood_events || [], (row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.valence >= 0 ? "😊" : "😞"));
      title.appendChild(el("span", "tag", String(row.created_at || "").slice(5, 16)));
      const del = actionBtn("删除", async () => {
        try { await post("mood_event_delete", { event_id: row.event_id }); toast("已删除", "ok"); loadCulture(); }
        catch (error) { toast(String(error.message || error), "error"); }
      }, "warn");
      return itemShell(title, row.reason || "", "", [del]);
    }, "还没有情绪起伏的记录");
  }

  async function loadInjections() {
    const box = $("injections");
    if (!box) return;
    box.textContent = "";
    box.appendChild(el("div", "muted", "加载中…"));
    let data;
    try {
      data = await get("injections");
    } catch (error) {
      box.textContent = "";
      box.appendChild(el("div", "empty", String(error.message || error)));
      return;
    }
    const items = (data && data.items) || [];
    box.textContent = "";
    if (!items.length) {
      box.appendChild(el("div", "empty", "还没有注入项——点上面的「新增」自己写一条"));
      return;
    }
    items.forEach((row) => {
      const title = el("div", "title");
      title.appendChild(el("span", "", row.name || row.injection_id));
      title.appendChild(el("span", "tag", row.builtin ? "自带" : "自定义"));
      title.appendChild(el("span", "tag", row.enabled ? "已启用" : "未启用"));
      const body = el("div", "body", String(row.content || "").slice(0, 220));
      const toggle = actionBtn(row.enabled ? "关掉" : "启用", async () => {
        try {
          await post("injection_toggle", { injection_id: row.injection_id, enabled: !row.enabled });
          toast(row.enabled ? "已关掉" : "已启用", "ok");
          loadInjections();
        } catch (error) { toast(String(error.message || error), "error"); }
      }, row.enabled ? "" : "primary");
      const edit = actionBtn("编辑", async () => {
        const answer = await askPrompt({
          title: "改这条注入",
          fields: [
            { key: "name", label: "名称", value: row.name || "" },
            { key: "content", label: "规则内容（会以【强制规则】注入）", value: row.content || "", multiline: true },
          ],
          okText: "保存",
        });
        if (!answer) return;
        try {
          await post("injection_save", {
            injection_id: row.injection_id, name: answer.name, content: answer.content,
            enabled: row.enabled,
          });
          toast("已保存", "ok");
          loadInjections();
        } catch (error) { toast(String(error.message || error), "error"); }
      });
      const remove = actionBtn("删除", async () => {
        if (row.builtin) return toast("自带预设不能删，关掉就行", "error");
        if (!(await askConfirm({
          title: "删除这条注入", danger: true, okText: "删除",
          message: row.name || row.injection_id,
        }))) return;
        try {
          await post("injection_delete", { injection_id: row.injection_id });
          toast("已删除", "ok");
          loadInjections();
        } catch (error) { toast(String(error.message || error), "error"); }
      }, "warn");
      box.appendChild(itemShell(title, "", (data.note || ""), [toggle, edit, remove]));
      const main = box.lastChild.querySelector(".main");
      if (main) main.insertBefore(body, main.querySelector(".meta"));
    });
  }

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
      id: script.id, name: script.display_name || script.name || script.id,
      description: script.description || "",
      enabled: script.enabled !== false, tools: (script.tools || []).length,
      kind: script.origin === "learned" ? "自学习技能" : "脚本拓展",
      error: script.error || "",
      config: script.config || {}, template: script.config_template || {},
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
      const buttons = [toggle, reload];
      // 有配置模板（或已经存过配置）的拓展：给一个「配置」按钮，改完立即生效
      if (row.kind === "脚本拓展"
          && (Object.keys(row.template || {}).length || Object.keys(row.config || {}).length)) {
        buttons.push(actionBtn("配置", () => extConfigDialog(row)));
      }
      const detail = row.error ? `${row.description} ⚠ ${row.error}` : row.description;
      return itemShell(title, detail, "", buttons);
    }, "没有额外拓展");
  }

  // 拓展配置：自绘表单（键值对，值的类型按模板猜：bool→开关、number→数字框、其余文本框）
  function extConfigDialog(row) {
    const overlay = el("div", "modal-overlay");
    const box = el("div", "modal");
    box.appendChild(el("h3", "", `${row.name} · 配置`));
    box.appendChild(el("p", "modal-msg",
      "改完点保存即生效（拓展每次调用都会重读配置）。留空表示不设置该键。"));
    const fields = {};
    const keys = Array.from(new Set(
      Object.keys(row.template || {}).concat(Object.keys(row.config || {}))));
    if (!keys.length) {
      box.appendChild(el("p", "muted", "这个拓展没有配置项"));
    }
    keys.forEach((key) => {
      const wrap = el("div", "cfg-row");
      wrap.appendChild(el("label", "muted", key));
      const sample = (row.config || {})[key] !== undefined
        ? row.config[key] : (row.template || {})[key];
      let input;
      if (typeof sample === "boolean") {
        input = el("input");
        input.type = "checkbox";
        input.checked = Boolean(sample);
      } else {
        input = el("input");
        input.type = typeof sample === "number" ? "number" : "text";
        input.value = sample === undefined || sample === null ? "" : String(sample);
        if (key.toLowerCase().includes("key") || key.toLowerCase().includes("token")) {
          input.type = "password";
        }
      }
      input.dataset.kind = typeof sample;
      fields[key] = input;
      wrap.appendChild(input);
      box.appendChild(wrap);
    });
    const row2 = el("div", "row");
    const save = el("button", "btn primary", "保存");
    const reset = el("button", "btn ghost", "恢复默认");
    const cancel = el("button", "btn ghost", "取消");
    save.addEventListener("click", async () => {
      const payload = {};
      Object.keys(fields).forEach((key) => {
        const input = fields[key];
        if (input.type === "checkbox") { payload[key] = input.checked; return; }
        const raw = String(input.value || "");
        if (raw === "") return;                       // 空=不设置这个键
        if (input.dataset.kind === "number") {
          const num = Number(raw);
          payload[key] = Number.isFinite(num) ? num : raw;
        } else if (input.dataset.kind === "boolean") {
          payload[key] = raw === "true";
        } else { payload[key] = raw; }
      });
      try {
        await post("extension_config_save", { id: row.id, config: payload });
        toast("配置已保存", "ok");
        overlay.remove();
        loadCaps();
      } catch (error) { toast(String(error.message || error), "error"); }
    });
    reset.addEventListener("click", async () => {
      try {
        await post("extension_config_reset", { id: row.id });
        toast("已恢复默认", "ok");
        overlay.remove();
        loadCaps();
      } catch (error) { toast(String(error.message || error), "error"); }
    });
    cancel.addEventListener("click", () => overlay.remove());
    row2.appendChild(save); row2.appendChild(reset); row2.appendChild(cancel);
    box.appendChild(row2);
    overlay.appendChild(box);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) overlay.remove();
    });
    document.body.appendChild(overlay);
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
      const ico = el("span", "ico");
      ico.appendChild(iconSvg(panel.icon));
      btn.appendChild(ico);
      btn.appendChild(el("span", "nav-text", panel.title));
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
    $("btn-inject-add").addEventListener("click", async () => {
      const answer = await askPrompt({
        title: "新增提示词注入",
        fields: [
          { key: "name", label: "名称（如：每条都要短）", value: "" },
          { key: "content", label: "规则内容（写成硬性要求最有效）", value: "", multiline: true },
        ],
        okText: "添加并启用",
      });
      if (!answer) return;
      if (!String(answer.content || "").trim()) return toast("内容不能为空", "error");
      try {
        await post("injection_save", { name: answer.name, content: answer.content, enabled: true });
        toast("已添加", "ok");
        loadInjections();
      } catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-forest-refresh").addEventListener("click", loadForest);
    $("btn-refresh-inject").addEventListener("click", loadInjections);
    $("btn-refresh-culture").addEventListener("click", loadCulture);
    $("btn-learn-run").addEventListener("click", runImport);
    $("btn-learn-refresh").addEventListener("click", loadLearn);
    $("btn-export-memory").addEventListener("click", () => runExport(true));
    $("btn-export-server").addEventListener("click", () => runExport(false));
    $("btn-backup-refresh").addEventListener("click", loadBackup);
    $("btn-import-memory").addEventListener("click", runMemoryImport);
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
