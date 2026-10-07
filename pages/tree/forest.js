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
