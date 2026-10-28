// ===== 记忆网渲染器（MemoryWeb）=====
// 零依赖 Canvas 力导向图：节点 = 人物 / 会话 / 话题 / 事实 / 摘要，连线 = 关系。
// 与 pages/memory/app.js 内联版本同源，两份内容需保持一致。
//
// 交互（按知识图谱软件的通用做法设计）：
//   · 滚轮缩放 / 拖拽平移 / 拖动节点 / 悬停卡片 / 单击选中 / Ctrl·Shift 多选（剪枝）
//   · 双击节点 = 聚焦邻域（只看这一圈），左上角显示面包屑，Esc 退出
//   · 右下角缩略图：显示全图与当前视口，点击/拖拽直接跳转
//   · 左下角视图工具条：放大 / 缩小 / 适配全图 / 群落底色开关（键盘 + - 0 F 同效）
//   · 群落底色：按连通分量给节点加一层极淡的光晕，一眼看出分成几团
//   · 标签随缩放逐级出现；放大到一定倍数后强连线显示关系名
// 用法：const web = window.SparkingWeb.mount(container, { onSelect, onMultiSelect, onHover });
//       web.setData({nodes, edges}); web.focus(id); web.setSelection([ids]); web.destroy();
window.SparkingWeb = (() => {
  "use strict";

  const KIND_STYLE = {
    person:  { color: "#f59e0b", label: "人物" },
    group:   { color: "#6366f1", label: "会话" },
    topic:   { color: "#10b981", label: "话题" },
    fact:    { color: "#64748b", label: "事实" },
    summary: { color: "#ec4899", label: "摘要" },
  };
  const REL_LABEL = {
    includes: "包含", about: "关于", mentions: "提到", has_participant: "参与者",
    derives_from: "压缩自", co_occurs: "同现", in: "属于", member_of: "成员",
  };
  const ARCHIVED_COLOR = "#94a3b8";
  // 群落色（淡）：只做光晕，不抢节点本色
  const CLUSTER_HUES = ["#6366f1", "#10b981", "#f59e0b", "#ec4899",
                        "#06b6d4", "#8b5cf6", "#f97316", "#84cc16"];

  function hashUnit(value, salt) {
    const str = String(value) + ":" + String(salt || 0);
    let h = 2166136261;
    for (let i = 0; i < str.length; i += 1) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return ((h >>> 0) % 100000) / 100000;
  }

  function mount(container, opts) {
    const options = opts || {};
    const canvas = document.createElement("canvas");
    canvas.className = "web-canvas";
    container.appendChild(canvas);
    const ctx = canvas.getContext("2d");
    const tip = document.createElement("div");
    tip.className = "web-tip";
    tip.hidden = true;
    container.appendChild(tip);

    // 视图工具条（左下）与聚焦提示（左上）
    const tools = document.createElement("div");
    tools.className = "web-tools";
    container.appendChild(tools);
    const crumb = document.createElement("div");
    crumb.className = "web-crumb";
    crumb.hidden = true;
    container.appendChild(crumb);

    const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    let W = 800, H = 520;
    let data = { nodes: [], edges: [] };
    const positions = new Map();       // id -> {x, y, vx, vy}
    const selection = new Set();
    let hover = null;
    let dragNode = null;
    let panning = null;
    let moved = false;
    let view = { x: 0, y: 0, scale: 1 };
    let running = 0;
    let raf = 0;
    let componentOf = new Map();
    let components = [];
    let clusterColors = true;
    // 聚焦（局部图谱）：focusSet 为空 = 看全图
    let focusSet = null;
    let focusNode = null;
    let hoverEdge = null;

    function computeComponents(nodes, edges) {
      // 并查集：把有连线的节点归成一个个"群落"——布点与内聚力都按群落来
      const parent = new Map();
      nodes.forEach((node) => parent.set(node.id, node.id));
      const find = (value) => {
        let root = value;
        while (parent.get(root) !== root) root = parent.get(root);
        while (parent.get(value) !== root) {
          const next = parent.get(value);
          parent.set(value, root);
          value = next;
        }
        return root;
      };
      edges.forEach((edge) => {
        if (!parent.has(edge.source) || !parent.has(edge.target)) return;
        const a = find(edge.source);
        const b = find(edge.target);
        if (a !== b) parent.set(a, b);
      });
      const groups = new Map();
      nodes.forEach((node) => {
        const root = find(node.id);
        if (!groups.has(root)) groups.set(root, []);
        groups.get(root).push(node);
      });
      return Array.from(groups.values());
    }

    function resize() {
      const rect = container.getBoundingClientRect();
      W = Math.max(240, Math.round(rect.width || 800));
      H = Math.max(200, Math.round(rect.height || 520));
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      canvas.style.width = W + "px";
      canvas.style.height = H + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      render();
    }

    function radiusOf(node) {
      const weight = Math.max(1, Number(node.weight) || 1);
      return Math.min(16, 4.2 + Math.sqrt(weight) * 1.7);
    }

    function degreeOf(nodeId) {
      let degree = 0;
      (data.edges || []).forEach((edge) => {
        if (edge.source === nodeId || edge.target === nodeId) degree += 1;
      });
      return degree;
    }

    function seed() {
      positions.clear();
      const nodes = data.nodes || [];
      nodes.forEach((node, index) => {
        const angle = index * 2.399963;
        const radius = 40 + Math.sqrt(index + 1) * 26;
        positions.set(node.id, {
          x: W / 2 + Math.cos(angle) * radius,
          y: H / 2 + Math.sin(angle) * radius,
          vx: 0, vy: 0,
        });
      });
    }

    function step() {
      const nodes = data.nodes || [];
      const edges = data.edges || [];
      // 群落重心：每个"群落"朝自己的重心收——比全局引力更有结构感
      const centroids = new Map();
      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        const key = componentOf.get(node.id) ?? -1;
        const slot = centroids.get(key) || { x: 0, y: 0, n: 0 };
        slot.x += pos.x; slot.y += pos.y; slot.n += 1;
        centroids.set(key, slot);
      });
      centroids.forEach((slot) => { slot.x /= slot.n; slot.y /= slot.n; });
      const cell = 130;
      const grid = new Map();
      const cellKey = (x, y) => Math.round(x / cell) + "|" + Math.round(y / cell);
      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        const key = cellKey(pos.x, pos.y);
        if (!grid.has(key)) grid.set(key, []);
        grid.get(key).push(node);
      });
      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        let fx = 0, fy = 0;
        const cx = Math.round(pos.x / cell), cy = Math.round(pos.y / cell);
        for (let gx = cx - 1; gx <= cx + 1; gx += 1) {
          for (let gy = cy - 1; gy <= cy + 1; gy += 1) {
            const bucket = grid.get(gx + "|" + gy);
            if (!bucket) continue;
            bucket.forEach((other) => {
              if (other.id === node.id) return;
              const target = positions.get(other.id);
              if (!target) return;
              const dx = pos.x - target.x, dy = pos.y - target.y;
              const distance = Math.sqrt(dx * dx + dy * dy) || 1;
              if (distance > cell * 1.9) return;
              const force = 2400 / (distance * distance);
              fx += (dx / distance) * force;
              fy += (dy / distance) * force;
            });
          }
        }
        const centroid = centroids.get(componentOf.get(node.id) ?? -1);
        if (centroid) {
          fx += (centroid.x - pos.x) * 0.006;
          fy += (centroid.y - pos.y) * 0.006;
        }
        fx += (W / 2 - pos.x) * 0.0015;
        fy += (H / 2 - pos.y) * 0.0015;
        pos.vx = (pos.vx + fx) * 0.84;
        pos.vy = (pos.vy + fy) * 0.84;
      });
      edges.forEach((edge) => {
        const a = positions.get(edge.source);
        const b = positions.get(edge.target);
        if (!a || !b) return;
        const dx = b.x - a.x, dy = b.y - a.y;
        const distance = Math.sqrt(dx * dx + dy * dy) || 1;
        const target = 104;
        const strength = 0.045 * Math.min(3, Math.max(1, Number(edge.weight) || 1));
        const force = (distance - target) * strength;
        const ux = dx / distance, uy = dy / distance;
        a.vx += ux * force; a.vy += uy * force;
        b.vx -= ux * force; b.vy -= uy * force;
      });
      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        if (node.id === (dragNode && dragNode.id)) return;
        pos.x += Math.max(-14, Math.min(14, pos.vx));
        pos.y += Math.max(-14, Math.min(14, pos.vy));
        pos.x = Math.max(24, Math.min(W - 24, pos.x));
        pos.y = Math.max(24, Math.min(H - 24, pos.y));
      });
      resolveCollisions(0.5);
    }

    // 防重叠：半径和之内直接推开——没有这一步，节点会挤成一团看不出结构。
    // 返回本轮推开的对数（收尾松弛用它判断还有没有重叠）。
    function resolveCollisions(strength) {
      const nodes = data.nodes || [];
      const gcell = 170;
      const g2 = new Map();
      const key2 = (x, y) => Math.round(x / gcell) + "|" + Math.round(y / gcell);
      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        const key = key2(pos.x, pos.y);
        if (!g2.has(key)) g2.set(key, []);
        g2.get(key).push(node);
      });
      let pushed = 0;
      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        const cx = Math.round(pos.x / gcell), cy = Math.round(pos.y / gcell);
        const r1 = radiusOf(node);
        for (let gx = cx - 1; gx <= cx + 1; gx += 1) {
          for (let gy = cy - 1; gy <= cy + 1; gy += 1) {
            const bucket = g2.get(gx + "|" + gy);
            if (!bucket) continue;
            bucket.forEach((other) => {
              if (other.id <= node.id) return;
              if (other.id === (dragNode && dragNode.id)) return;
              if (node.id === (dragNode && dragNode.id)) return;
              const target = positions.get(other.id);
              if (!target) return;
              const dx = target.x - pos.x, dy = target.y - pos.y;
              const distance = Math.sqrt(dx * dx + dy * dy) || 0.01;
              const minimum = r1 + radiusOf(other) + 6;
              if (distance >= minimum) return;
              const push = (minimum - distance) * strength;
              const ux = dx / distance, uy = dy / distance;
              pos.x -= ux * push; pos.y -= uy * push;
              target.x += ux * push; target.y += uy * push;
              pushed += 1;
            });
          }
        }
      });
      return pushed;
    }

    // 收尾去重叠：动画停稳后做几轮"只推开、不加力"的松弛——弹簧挤压下
    // 残留的顽固重叠（挤成一团的观感来源）在这里彻底解开，且不越出画布。
    function relaxCollisions() {
      for (let round = 0; round < 90; round += 1) {
        if (!resolveCollisions(0.6)) break;
        (data.nodes || []).forEach((node) => {
          const pos = positions.get(node.id);
          if (!pos) return;
          pos.x = Math.max(24, Math.min(W - 24, pos.x));
          pos.y = Math.max(24, Math.min(H - 24, pos.y));
        });
      }
    }

    function toScreen(point) {
      return { x: point.x * view.scale + view.x, y: point.y * view.scale + view.y };
    }
    function toWorld(x, y) {
      return { x: (x - view.x) / view.scale, y: (y - view.y) / view.scale };
    }

    // ---------- 聚焦（局部图谱） ----------
    function buildFocus(nodeId, hops) {
      const id = String(nodeId);
      if (!positions.has(id)) return null;
      const depth = Math.max(0, Math.min(3, Number(hops) || 2));
      const neighbours = new Map();
      (data.edges || []).forEach((edge) => {
        if (!neighbours.has(edge.source)) neighbours.set(edge.source, []);
        if (!neighbours.has(edge.target)) neighbours.set(edge.target, []);
        neighbours.get(edge.source).push(edge.target);
        neighbours.get(edge.target).push(edge.source);
      });
      const level = new Map([[id, 0]]);
      let frontier = [id];
      for (let d = 0; d < depth && frontier.length; d += 1) {
        const next = [];
        frontier.forEach((current) => {
          (neighbours.get(current) || []).forEach((other) => {
            if (level.has(other)) return;
            level.set(other, d + 1);
            next.push(other);
          });
        });
        frontier = next;
      }
      return new Set(level.keys());
    }

    function boundsOf(ids) {
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      (ids ? Array.from(ids) : (data.nodes || []).map((node) => node.id)).forEach((id) => {
        const pos = positions.get(id);
        if (!pos) return;
        minX = Math.min(minX, pos.x); maxX = Math.max(maxX, pos.x);
        minY = Math.min(minY, pos.y); maxY = Math.max(maxY, pos.y);
      });
      if (!Number.isFinite(minX)) return null;
      return { minX, minY, maxX, maxY };
    }

    let viewAnim = 0;
    function animateView(target) {
      const from = { x: view.x, y: view.y, scale: view.scale };
      const start = (typeof performance !== "undefined" ? performance.now() : Date.now());
      const duration = 260;
      if (viewAnim) cancelAnimationFrame(viewAnim);
      const tick = () => {
        const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
        const t = Math.min(1, (now - start) / duration);
        const ease = 1 - Math.pow(1 - t, 3);
        view.x = from.x + (target.x - from.x) * ease;
        view.y = from.y + (target.y - from.y) * ease;
        view.scale = from.scale + (target.scale - from.scale) * ease;
        render();
        if (t < 1) viewAnim = requestAnimationFrame(tick);
        else viewAnim = 0;
      };
      viewAnim = requestAnimationFrame(tick);
    }

    function viewForBounds(box, padding) {
      if (!box) return null;
      const pad = padding === undefined ? 90 : padding;
      const width = Math.max(24, box.maxX - box.minX);
      const height = Math.max(24, box.maxY - box.minY);
      const scale = Math.max(0.35, Math.min(2.2,
        Math.min((W - pad * 2) / width, (H - pad * 2) / height)));
      return {
        scale,
        x: W / 2 - ((box.minX + box.maxX) / 2) * scale,
        y: H / 2 - ((box.minY + box.maxY) / 2) * scale,
      };
    }

    function setFocusInternal(nodeId, hops) {
      const set = buildFocus(nodeId, hops);
      if (!set) return false;
      focusSet = set;
      focusNode = String(nodeId);
      const target = viewForBounds(boundsOf(set), 80);
      if (target) animateView(target);
      renderCrumb();
      if (typeof options.onFocus === "function") {
        const node = (data.nodes || []).find((item) => item.id === focusNode);
        options.onFocus(node || null, set.size);
      }
      return true;
    }

    function clearFocusInternal() {
      if (!focusSet) return;
      focusSet = null;
      focusNode = null;
      renderCrumb();
      const target = viewForBounds(boundsOf(null), 70);
      if (target) animateView(target);
      if (typeof options.onFocus === "function") options.onFocus(null, 0);
    }

    function renderCrumb() {
      crumb.textContent = "";
      if (!focusSet) { crumb.hidden = true; return; }
      const node = (data.nodes || []).find((item) => item.id === focusNode);
      const label = node ? String(node.label || "").slice(0, 20) : "节点";
      const text = document.createElement("span");
      text.textContent = "聚焦：" + label + "（邻域 " + focusSet.size + " 个节点）";
      const exit = document.createElement("button");
      exit.type = "button";
      exit.textContent = "退出聚焦 ✕";
      exit.addEventListener("click", () => clearFocusInternal());
      crumb.appendChild(text);
      crumb.appendChild(exit);
      crumb.hidden = false;
    }

    // ---------- 视图工具条 ----------
    function zoomBy(factor) {
      const cx = W / 2, cy = H / 2;
      const world = toWorld(cx, cy);
      const scale = Math.max(0.35, Math.min(3.2, view.scale * factor));
      view.scale = scale;
      view.x = cx - world.x * scale;
      view.y = cy - world.y * scale;
      render();
    }

    function fitView() {
      const target = viewForBounds(boundsOf(focusSet), 70);
      if (target) animateView(target);
    }

    function addTool(label, title, handler) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.title = title;
      button.addEventListener("click", handler);
      tools.appendChild(button);
      return button;
    }

    function buildTools() {
      tools.textContent = "";
      addTool("＋", "放大（+）", () => zoomBy(1.25));
      addTool("－", "缩小（-）", () => zoomBy(0.8));
      addTool("⤢", "适配全图（0）", () => fitView());
      addTool("◐", "群落底色开/关", () => {
        clusterColors = !clusterColors;
        render();
      });
    }

    function onKey(event) {
      const target = event.target || {};
      const tag = String(target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      if (event.key === "+" || event.key === "=") { zoomBy(1.25); }
      else if (event.key === "-" || event.key === "_") { zoomBy(0.8); }
      else if (event.key === "0" || event.key.toLowerCase() === "f") { fitView(); }
      else if (event.key === "Escape") {
        if (focusSet) clearFocusInternal();
        else {
          selection.clear();
          render();
          emitSelect(null);
        }
      }
    }

    // ---------- 渲染 ----------
    function dimmed(nodeId) {
      return Boolean(focusSet) && !focusSet.has(nodeId);
    }

    function render() {
      ctx.clearRect(0, 0, W, H);
      const nodes = data.nodes || [];
      const edges = data.edges || [];
      ctx.save();
      ctx.translate(view.x, view.y);
      ctx.scale(view.scale, view.scale);

      // 群落光晕：同一连通分量用同一淡色底，看出"分成几团"
      if (clusterColors) {
        nodes.forEach((node) => {
          const pos = positions.get(node.id);
          if (!pos || dimmed(node.id)) return;
          const index = componentOf.get(node.id) ?? 0;
          if ((components[index] || []).length < 3) return;
          const color = CLUSTER_HUES[index % CLUSTER_HUES.length];
          const radius = radiusOf(node) + 9;
          ctx.beginPath();
          ctx.arc(pos.x, pos.y, radius, 0, Math.PI * 2);
          ctx.fillStyle = color;
          ctx.globalAlpha = 0.09;
          ctx.fill();
          ctx.globalAlpha = 1;
        });
      }

      edges.forEach((edge) => {
        const a = positions.get(edge.source);
        const b = positions.get(edge.target);
        if (!a || !b) return;
        const weight = Math.max(1, Number(edge.weight) || 1);
        // 曲线边：中点沿法线偏一个"随机的拐弯"（按边名取散列，稳定不闪）
        const dx = b.x - a.x, dy = b.y - a.y;
        const length = Math.sqrt(dx * dx + dy * dy) || 1;
        const bend = (hashUnit(edge.source + edge.relation + edge.target, 1) - 0.5)
          * 2 * Math.min(46, length * 0.22);
        const midX = (a.x + b.x) / 2 - (dy / length) * bend;
        const midY = (a.y + b.y) / 2 + (dx / length) * bend;
        const faded = focusSet && !(focusSet.has(edge.source) && focusSet.has(edge.target));
        ctx.strokeStyle = faded
          ? "rgba(148,163,184,0.10)"
          : (edge.archived ? "rgba(148,163,184,0.42)" : "rgba(100,116,139,0.45)");
        ctx.lineWidth = Math.min(3.2, 1.0 + Math.log2(weight + 1) * 0.9) / view.scale;
        if (edge.archived) ctx.setLineDash([5, 5]);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.quadraticCurveTo(midX, midY, b.x, b.y);
        ctx.stroke();
        ctx.setLineDash([]);
        // 放大后给强连线标关系名
        if (!faded && view.scale >= 1.6 && weight >= 8) {
          const label = REL_LABEL[edge.relation] || String(edge.relation || "");
          if (label) {
            const mx = (a.x + b.x) / 2 - (dy / length) * bend * 0.5;
            const my = (a.y + b.y) / 2 + (dx / length) * bend * 0.5;
            ctx.font = "9px 'Segoe UI', 'Microsoft YaHei', sans-serif";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.strokeStyle = "rgba(255,255,255,0.9)";
            ctx.lineWidth = 3 / view.scale;
            ctx.strokeText(label, mx, my);
            ctx.fillStyle = "rgba(71,85,105,0.9)";
            ctx.fillText(label, mx, my);
          }
        }
      });

      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        const style = KIND_STYLE[node.kind] || KIND_STYLE.fact;
        const archived = node.status === "archived";
        const radius = radiusOf(node);
        const faded = dimmed(node.id);
        ctx.globalAlpha = faded ? 0.16 : (archived ? 0.45 : 1);
        ctx.beginPath();
        ctx.arc(pos.x, pos.y, radius, 0, Math.PI * 2);
        ctx.fillStyle = archived ? ARCHIVED_COLOR : style.color;
        ctx.fill();
        ctx.globalAlpha = 1;
        if (archived) {
          ctx.setLineDash([4, 4]);
          ctx.strokeStyle = faded ? "rgba(100,116,139,0.25)" : "#64748b";
          ctx.lineWidth = 1.4 / view.scale;
          ctx.stroke();
          ctx.setLineDash([]);
        } else {
          ctx.strokeStyle = faded ? "rgba(15,23,42,0.06)" : "rgba(15, 23, 42, 0.25)";
          ctx.lineWidth = 1.2 / view.scale;
          ctx.stroke();
        }
        if (selection.has(node.id)) {
          ctx.beginPath();
          ctx.arc(pos.x, pos.y, radius + 4, 0, Math.PI * 2);
          ctx.strokeStyle = "#2563eb";
          ctx.lineWidth = 2.4 / view.scale;
          ctx.stroke();
        } else if (hover && hover.id === node.id) {
          ctx.beginPath();
          ctx.arc(pos.x, pos.y, radius + 3, 0, Math.PI * 2);
          ctx.strokeStyle = "#0f172a";
          ctx.lineWidth = 1.6 / view.scale;
          ctx.stroke();
        } else if (focusSet && focusNode === node.id) {
          ctx.beginPath();
          ctx.arc(pos.x, pos.y, radius + 5, 0, Math.PI * 2);
          ctx.strokeStyle = "#0ea5e9";
          ctx.lineWidth = 1.8 / view.scale;
          ctx.stroke();
        }
      });

      // 标签：按重要度限流 + 防叠字（选中的、悬停的优先，其次按权重）
      const showLabels = view.scale > 1.1;
      ctx.font = "11px 'Segoe UI', 'Microsoft YaHei', sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const candidates = [];
      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos || dimmed(node.id)) return;
        const pinned = selection.has(node.id) || (hover && hover.id === node.id)
          || (focusSet && focusNode === node.id);
        const important = (Number(node.weight) || 1) >= 3 || pinned;
        if (!important && !showLabels) return;
        candidates.push({ node, pos, pinned });
      });
      candidates.sort((a, b) => (Number(b.pinned) - Number(a.pinned))
        || ((Number(b.node.weight) || 1) - (Number(a.node.weight) || 1)));
      const labelBudget = showLabels ? 90 : 48;
      const placed = [];
      let drawn = 0;
      for (const item of candidates) {
        if (drawn >= labelBudget) break;
        const label = String(item.node.label || "").slice(0, 14);
        const width = ctx.measureText(label).width;
        const y = item.pos.y + radiusOf(item.node) + 3;
        const box = { x: item.pos.x - width / 2 - 2, y: y - 1, w: width + 4, h: 13 };
        const overlaps = placed.some((other) =>
          box.x < other.x + other.w && box.x + box.w > other.x
          && box.y < other.y + other.h && box.y + box.h > other.y);
        if (overlaps && !item.pinned) continue;
        placed.push(box);
        drawn += 1;
        ctx.strokeStyle = "rgba(255,255,255,0.85)";
        ctx.lineWidth = 3 / view.scale;
        ctx.strokeText(label, item.pos.x, y);
        ctx.fillStyle = "#0f172a";
        ctx.fillText(label, item.pos.x, y);
      }
      ctx.restore();
      renderMinimap();
    }

    // 缩略图导航：全图缩影 + 当前视口框，点击/拖动定位
    const MINI = { w: 150, h: 96, pad: 10 };
    function minimapRect() {
      return { x: W - MINI.w - 14, y: H - MINI.h - 14, w: MINI.w, h: MINI.h };
    }
    function worldToMini(pos, box, bounds) {
      const width = Math.max(1, bounds.maxX - bounds.minX);
      const height = Math.max(1, bounds.maxY - bounds.minY);
      const scale = Math.min((box.w - MINI.pad * 2) / width, (box.h - MINI.pad * 2) / height);
      return {
        x: box.x + MINI.pad + (pos.x - bounds.minX) * scale
          + ((box.w - MINI.pad * 2) - width * scale) / 2,
        y: box.y + MINI.pad + (pos.y - bounds.minY) * scale
          + ((box.h - MINI.pad * 2) - height * scale) / 2,
        scale,
      };
    }
    function renderMinimap() {
      const nodes = data.nodes || [];
      if (nodes.length < 8) return;
      const bounds = boundsOf(null);
      if (!bounds) return;
      const box = minimapRect();
      ctx.save();
      ctx.fillStyle = "rgba(255,255,255,0.88)";
      ctx.strokeStyle = "rgba(148,163,184,0.55)";
      ctx.lineWidth = 1;
      const r = 6;
      ctx.beginPath();
      ctx.moveTo(box.x + r, box.y);
      ctx.arcTo(box.x + box.w, box.y, box.x + box.w, box.y + box.h, r);
      ctx.arcTo(box.x + box.w, box.y + box.h, box.x, box.y + box.h, r);
      ctx.arcTo(box.x, box.y + box.h, box.x, box.y, r);
      ctx.arcTo(box.x, box.y, box.x + box.w, box.y, r);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        const point = worldToMini(pos, box, bounds);
        const style = KIND_STYLE[node.kind] || KIND_STYLE.fact;
        ctx.beginPath();
        ctx.arc(point.x, point.y, 1.3, 0, Math.PI * 2);
        ctx.fillStyle = dimmed(node.id) ? "rgba(148,163,184,0.5)" : style.color;
        ctx.fill();
      });
      // 当前视口框
      const topLeft = toWorld(0, 0);
      const bottomRight = toWorld(W, H);
      const a = worldToMini(topLeft, box, bounds);
      const b = worldToMini(bottomRight, box, bounds);
      const rect = {
        x: Math.max(box.x + 2, Math.min(a.x, b.x)),
        y: Math.max(box.y + 2, Math.min(a.y, b.y)),
        w: Math.min(box.w - 4, Math.abs(b.x - a.x)),
        h: Math.min(box.h - 4, Math.abs(b.y - a.y)),
      };
      ctx.strokeStyle = "rgba(37,99,235,0.85)";
      ctx.lineWidth = 1.4;
      ctx.strokeRect(rect.x, rect.y, rect.w, rect.h);
      ctx.restore();
    }

    function minimapJump(px, py) {
      const bounds = boundsOf(null);
      if (!bounds) return;
      const box = minimapRect();
      const width = Math.max(1, bounds.maxX - bounds.minX);
      const height = Math.max(1, bounds.maxY - bounds.minY);
      const scale = Math.min((box.w - MINI.pad * 2) / width, (box.h - MINI.pad * 2) / height);
      const offsetX = box.x + MINI.pad + ((box.w - MINI.pad * 2) - width * scale) / 2;
      const offsetY = box.y + MINI.pad + ((box.h - MINI.pad * 2) - height * scale) / 2;
      const worldX = bounds.minX + (px - offsetX) / scale;
      const worldY = bounds.minY + (py - offsetY) / scale;
      view.x = W / 2 - worldX * view.scale;
      view.y = H / 2 - worldY * view.scale;
      render();
    }

    function inMinimap(x, y) {
      const box = minimapRect();
      return x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h;
    }

    function schedule(steps) {
      running = Math.max(running, steps);
      if (raf) return;
      const loop = () => {
        raf = 0;
        const batch = Math.min(14, running);
        for (let i = 0; i < batch; i += 1) step();
        running -= batch;
        if (running <= 0) relaxCollisions();
        render();
        if (running > 0) raf = requestAnimationFrame(loop);
      };
      raf = requestAnimationFrame(loop);
    }

    function hit(x, y) {
      const world = toWorld(x, y);
      let best = null, bestDistance = 0;
      (data.nodes || []).forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        const dx = world.x - pos.x, dy = world.y - pos.y;
        const distance = Math.sqrt(dx * dx + dy * dy);
        const limit = radiusOf(node) + 5;
        if (distance <= limit && (best === null || distance < bestDistance)) {
          best = node;
          bestDistance = distance;
        }
      });
      return best;
    }

    function pointer(event) {
      const rect = canvas.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    function emitSelect(node) {
      if (typeof options.onSelect === "function") options.onSelect(node);
    }
    function emitMulti() {
      if (typeof options.onMultiSelect === "function") {
        const nodes = (data.nodes || []).filter((node) => selection.has(node.id));
        options.onMultiSelect(nodes);
      }
    }

    function showTip(node, point) {
      if (!node) { tip.hidden = true; return; }
      const style = KIND_STYLE[node.kind] || KIND_STYLE.fact;
      const archived = node.status === "archived";
      tip.textContent = "";
      const head = document.createElement("div");
      head.className = "web-tip-head";
      const dot = document.createElement("span");
      dot.className = "web-tip-dot";
      dot.style.background = archived ? ARCHIVED_COLOR : style.color;
      const name = document.createElement("strong");
      name.textContent = String(node.label || "").slice(0, 24);
      head.appendChild(dot);
      head.appendChild(name);
      const meta = document.createElement("div");
      meta.className = "web-tip-meta";
      meta.textContent = style.label + (archived ? " · 已归档" : "")
        + " · 权重 " + (Number(node.weight) || 1)
        + " · 连线 " + degreeOf(node.id);
      const hint = document.createElement("div");
      hint.className = "web-tip-hint";
      hint.textContent = "双击看它的邻域";
      tip.appendChild(head);
      tip.appendChild(meta);
      tip.appendChild(hint);
      tip.style.left = Math.min(W - 190, point.x + 14) + "px";
      tip.style.top = Math.max(8, point.y - 18) + "px";
      tip.hidden = false;
    }

    function onPointerDown(event) {
      const point = pointer(event);
      if (inMinimap(point.x, point.y)) {
        minimapJump(point.x, point.y);
        panning = { mini: true };
        canvas.setPointerCapture(event.pointerId);
        return;
      }
      const node = hit(point.x, point.y);
      moved = false;
      if (node) {
        const world = toWorld(point.x, point.y);
        const pos = positions.get(node.id);
        dragNode = { id: node.id, dx: pos.x - world.x, dy: pos.y - world.y };
      } else {
        panning = { x: point.x, y: point.y, vx: view.x, vy: view.y };
      }
      canvas.setPointerCapture(event.pointerId);
    }

    function onPointerMove(event) {
      const point = pointer(event);
      if (panning && panning.mini) {
        minimapJump(point.x, point.y);
        return;
      }
      if (dragNode) {
        const world = toWorld(point.x, point.y);
        const pos = positions.get(dragNode.id);
        pos.x = world.x + dragNode.dx;
        pos.y = world.y + dragNode.dy;
        moved = true;
        schedule(6);
        render();
        return;
      }
      if (panning) {
        view.x = panning.vx + (point.x - panning.x);
        view.y = panning.vy + (point.y - panning.y);
        moved = true;
        render();
        return;
      }
      const node = hit(point.x, point.y);
      const changed = (node && node.id) !== (hover && hover.id);
      hover = node;
      if (changed) {
        render();
        showTip(node, point);
        if (typeof options.onHover === "function") options.onHover(node);
      } else if (node) {
        tip.style.left = Math.min(W - 190, point.x + 14) + "px";
        tip.style.top = Math.max(8, point.y - 18) + "px";
      }
    }

    function onPointerUp(event) {
      const point = pointer(event);
      if (dragNode && !moved) {
        const node = hit(point.x, point.y);
        if (node) {
          if (options.multi) {
            if (selection.has(node.id)) selection.delete(node.id);
            else selection.add(node.id);
            emitMulti();
          } else if (event.ctrlKey || event.metaKey || event.shiftKey) {
            if (selection.has(node.id)) selection.delete(node.id);
            else selection.add(node.id);
            emitMulti();
          } else {
            selection.clear();
            selection.add(node.id);
            emitSelect(node);
          }
          render();
        }
      } else if (!dragNode && !moved && !(panning && panning.mini)) {
        selection.clear();
        render();
        emitSelect(null);
      }
      dragNode = null;
      panning = null;
    }

    function onWheel(event) {
      event.preventDefault();
      const point = pointer(event);
      const world = toWorld(point.x, point.y);
      const factor = event.deltaY < 0 ? 1.12 : 0.89;
      view.scale = Math.max(0.35, Math.min(3.2, view.scale * factor));
      view.x = point.x - world.x * view.scale;
      view.y = point.y - world.y * view.scale;
      render();
    }

    function onDblClick(event) {
      const point = pointer(event);
      const node = hit(point.x, point.y);
      if (node) {
        // 双击 = 聚焦它的邻域；已在聚焦里再双击就换中心
        setFocusInternal(node.id, 2);
      }
    }

    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointerleave", () => { hover = null; tip.hidden = true; render(); });
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("dblclick", onDblClick);
    window.addEventListener("resize", resize);
    window.addEventListener("keydown", onKey);
    let observer = null;
    if (typeof ResizeObserver === "function") {
      observer = new ResizeObserver(() => resize());
      observer.observe(container);
    }

    buildTools();
    resize();
    requestAnimationFrame(resize);

    return {
      setData(next) {
        const nextNodes = (next && next.nodes) || [];
        const nextEdges = (next && next.edges) || [];
        // 保留已有节点的位置：切换类别过滤 / 刷新时不重排（只给新节点排位置）
        const previous = new Map(positions);
        positions.clear();
        components = computeComponents(nextNodes, nextEdges);
        componentOf = new Map();
        components.forEach((members, index) => {
          members.forEach((node) => componentOf.set(node.id, index));
        });
        // 群落布点：大群落靠中心，小群落绕外圈——布局有"块"有留白，不是均匀铺满
        const ordered = components
          .map((members, index) => ({ members, index }))
          .sort((a, b) => b.members.length - a.members.length);
        const fresh = new Set(nextNodes.map((node) => node.id)
          .filter((id) => !previous.has(id)));
        ordered.forEach(({ members }, rank) => {
          const freshMembers = members.filter((node) => fresh.has(node.id));
          if (!freshMembers.length) return;
          const angle = rank * 2.399963;
          const ring = 70 + Math.sqrt(rank + 1) * 76;
          const cx = W / 2 + Math.cos(angle) * ring * 0.75;
          const cy = H / 2 + Math.sin(angle) * ring * 0.55;
          freshMembers.forEach((node, inner) => {
            const a2 = inner * 2.399963;
            const radius = Math.min(150, 24 + Math.sqrt(inner + 1) * 15);
            positions.set(node.id, {
              x: cx + Math.cos(a2) * radius,
              y: cy + Math.sin(a2) * radius,
              vx: 0, vy: 0,
            });
          });
        });
        nextNodes.forEach((node) => {
          if (positions.has(node.id)) return;
          const old = previous.get(node.id);
          positions.set(node.id, old || { x: W / 2, y: H / 2, vx: 0, vy: 0 });
        });
        data = { nodes: nextNodes, edges: nextEdges };
        const alive = new Set(nextNodes.map((node) => node.id));
        Array.from(selection).forEach((id) => { if (!alive.has(id)) selection.delete(id); });
        // 数据变化后原焦点可能已不存在
        if (focusNode && !alive.has(focusNode)) {
          focusSet = null;
          focusNode = null;
        } else if (focusNode && focusSet) {
          focusSet = buildFocus(focusNode, 2) || focusSet;
        }
        renderCrumb();
        schedule(420);
      },
      setSelection(ids) {
        selection.clear();
        (ids || []).forEach((id) => selection.add(String(id)));
        render();
      },
      getSelection() {
        return Array.from(selection);
      },
      focus(id, scale) {
        const pos = positions.get(String(id));
        if (!pos) return;
        view.scale = Math.max(0.6, Math.min(3, scale || 1.4));
        view.x = W / 2 - pos.x * view.scale;
        view.y = H / 2 - pos.y * view.scale;
        render();
      },
      // 聚焦某个节点的邻域（等价于双击）
      setFocus(id, hops) {
        return setFocusInternal(id, hops === undefined ? 2 : hops);
      },
      clearFocus() {
        clearFocusInternal();
      },
      fit() {
        fitView();
      },
      zoomBy(factor) {
        zoomBy(Number(factor) || 1.2);
      },
      // 图例用的按类别计数
      kindCounts() {
        const counts = {};
        (data.nodes || []).forEach((node) => {
          const key = String(node.kind || "fact");
          counts[key] = (counts[key] || 0) + 1;
        });
        return counts;
      },
      setMulti(enabled) {
        options.multi = Boolean(enabled);
        if (!enabled) {
          selection.clear();
          render();
        }
      },
      destroy() {
        if (raf) cancelAnimationFrame(raf);
        if (viewAnim) cancelAnimationFrame(viewAnim);
        if (observer) observer.disconnect();
        window.removeEventListener("resize", resize);
        window.removeEventListener("keydown", onKey);
        container.textContent = "";
      },
    };
  }

  return { mount, KIND_STYLE, REL_LABEL };
})();
