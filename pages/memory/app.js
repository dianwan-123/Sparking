// ===== 记忆网渲染器（MemoryWeb，内联版）=====
// 与 pages/tree/web.js 同源，两份内容需保持一致。
// 零依赖 Canvas 力导向图：节点 = 人物 / 会话 / 话题 / 事实 / 摘要，连线 = 关系。
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
      W = Math.max(320, Math.round(rect.width));
      H = Math.max(240, Math.round(rect.height));
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
      // 防重叠：半径和之内直接推开——没有这一步，节点会挤成一团看不出结构
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

    function render() {
      ctx.clearRect(0, 0, W, H);
      const nodes = data.nodes || [];
      const edges = data.edges || [];
      const byId = new Map(nodes.map((node) => [node.id, node]));
      ctx.save();
      ctx.translate(view.x, view.y);
      ctx.scale(view.scale, view.scale);
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
        ctx.strokeStyle = edge.archived ? "rgba(148,163,184,0.42)" : "rgba(100,116,139,0.45)";
        ctx.lineWidth = Math.min(3.2, 1.0 + Math.log2(weight + 1) * 0.9) / view.scale;
        if (edge.archived) ctx.setLineDash([5, 5]);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.quadraticCurveTo(midX, midY, b.x, b.y);
        ctx.stroke();
        ctx.setLineDash([]);
      });
      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        const style = KIND_STYLE[node.kind] || KIND_STYLE.fact;
        const archived = node.status === "archived";
        const radius = radiusOf(node);
        ctx.beginPath();
        ctx.arc(pos.x, pos.y, radius, 0, Math.PI * 2);
        ctx.fillStyle = archived ? ARCHIVED_COLOR : style.color;
        ctx.globalAlpha = archived ? 0.45 : 1;
        ctx.fill();
        ctx.globalAlpha = 1;
        if (archived) {
          ctx.setLineDash([4, 4]);
          ctx.strokeStyle = "#64748b";
          ctx.lineWidth = 1.4 / view.scale;
          ctx.stroke();
          ctx.setLineDash([]);
        } else {
          ctx.strokeStyle = "rgba(15, 23, 42, 0.25)";
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
        if (!pos) return;
        const pinned = selection.has(node.id) || (hover && hover.id === node.id);
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

    function onPointerDown(event) {
      const point = pointer(event);
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
        if (node) {
          const style = KIND_STYLE[node.kind] || KIND_STYLE.fact;
          tip.textContent = "[" + style.label + "] " + node.label
            + "（权重 " + (Number(node.weight) || 1) + "）";
          tip.style.left = Math.min(W - 220, point.x + 12) + "px";
          tip.style.top = Math.max(6, point.y - 30) + "px";
          tip.hidden = false;
        } else {
          tip.hidden = true;
        }
        if (typeof options.onHover === "function") options.onHover(node);
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
      } else if (!dragNode && !moved) {
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
      if (node) focus(node.id, 1.5);
    }

    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointerleave", () => { hover = null; tip.hidden = true; render(); });
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("dblclick", onDblClick);
    window.addEventListener("resize", resize);
    let observer = null;
    if (typeof ResizeObserver === "function") {
      observer = new ResizeObserver(() => resize());
      observer.observe(container);
    }

    resize();
    requestAnimationFrame(resize);

    return {
      setData(next) {
        const nextNodes = (next && next.nodes) || [];
        const nextEdges = (next && next.edges) || [];
        // 保留已有节点的位置：切换类别过滤 / 刷新时不重排（只给新节点排位置）
        const previous = new Map(positions);
        positions.clear();
        const components = computeComponents(nextNodes, nextEdges);
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
      setMulti(enabled) {
        options.multi = Boolean(enabled);
        if (!enabled) {
          selection.clear();
          render();
        }
      },
      destroy() {
        if (raf) cancelAnimationFrame(raf);
        if (observer) observer.disconnect();
        window.removeEventListener("resize", resize);
        container.textContent = "";
      },
    };
  }

  return { mount, KIND_STYLE, REL_LABEL };
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
    { id: "forest", icon: "forest", title: "记忆网 MemoryWeb", desc: "人物/会话/话题/事实/摘要连成一张网；支持分支、回溯与剪枝" },
    { id: "people", icon: "people", title: "人物", desc: "人物印象（跨群互通）、好感度与说话风格" },
    { id: "groups", icon: "groups", title: "群与会话", desc: "会话列表、白名单与群话题" },
    { id: "schedule", icon: "schedule", title: "日程与任务", desc: "任务队列、待办与事件条件指令" },
    { id: "caps", icon: "caps", title: "能力与拓展", desc: "bot 实际可调用的工具与已装载技能" },
    { id: "inject", icon: "inject", title: "注入与压缩", desc: "强制规则注入（不受压缩影响）+ 上下文详细程度" },
    { id: "culture", icon: "culture", title: "群风格与心理", desc: "它学到的说话习惯、群内黑话、对你的了解与情绪记忆" },
    { id: "learn", icon: "learn", title: "快速学习", desc: "导入导出的群聊天记录，让它一次学会这些群" },
    { id: "gallery", icon: "gallery", title: "表情与媒体", desc: "表情包库与媒体归档：看、改描述、删" },
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
    gallery: '<rect x="3.5" y="4.5" width="17" height="15" rx="2.4"/>'
      + '<circle cx="9" cy="10" r="1.8"/><path d="M4.5 17.5 10 12l3.5 3.5L16.5 12l3 4.5"/>',
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

  async function get(endpoint, params) {
    return _withReloadRetry(async () => {
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
    });
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
      case "forest": return loadWeb();
      case "people": return loadPeople();
      case "groups": return loadGroups();
      case "schedule": return loadSchedule();
      case "caps": return loadCaps();
      case "inject": return Promise.all([loadInjections(), loadContextDetail()]);
      case "culture": return loadCulture();
      case "learn": return loadLearn();
      case "backup": return loadBackup();
      case "gallery": return loadGallery();
      case "config": return loadConfig();
      case "ops": return loadOps();
      default: return undefined;
    }
  }

  // 侧栏图标：优先用页面自带的 logo.png。
  // 插件页的静态资源带一次性 asset_token（宿主把 HTML 里的相对路径重写成了带 token 的绝对地址），
  // 运行时自己拼 "logo.png" 会 401/404 → 图标消失。所以这里直接复用宿主已经重写好的
  // favicon 地址（它一定带 token），并且**只有图片真的加载成功才替换掉那个字形**。
  function brandAssetUrl() {
    const icon = document.querySelector('link[rel="icon"]');
    const href = icon && icon.getAttribute("href");
    if (href) return href;
    return "logo.png";
  }

  function swapBrandLogo() {
    const logo = document.querySelector(".brand .logo");
    if (!logo || logo.dataset.image) return;
    const img = document.createElement("img");
    img.alt = "Sparking";
    img.width = 34;
    img.height = 34;
    img.style.borderRadius = "10px";
    img.addEventListener("load", () => {
      // 加载成功才换：失败时保留原来的字形，不能留一个空方块
      logo.textContent = "";
      logo.appendChild(img);
      logo.dataset.image = "1";
    });
    img.addEventListener("error", () => { img.remove(); });
    img.src = brandAssetUrl();
  }

  // ---------------------------------------------------------------- 概览
  async function loadOverview() {
    const [data, scopes] = await Promise.all([get("overview"), get("scopes")]);
    fillScopes(scopes, true);
    const pill = $("pill-status");
    pill.textContent = data.enabled ? "已启用" : "未启用";
    pill.className = "pill " + (data.enabled ? "on" : "off");
    $("brand-version").textContent = data.version ? "v" + data.version : "Sparking";
    swapBrandLogo();

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

  // ---------------------------------------------------------------- 记忆网（MemoryWeb）
  const webState = {
    graph: null, snapshot: null, pruneMode: false, pruneSelection: [],
    showArchived: false, hiddenKinds: new Set(),
  };
  const WEB_KIND_LABEL = { person: "人物", group: "会话", topic: "话题", fact: "事实", summary: "摘要" };
  const WEB_KIND_COLOR = { person: "#f59e0b", group: "#6366f1", topic: "#10b981", fact: "#64748b", summary: "#ec4899" };
  const WEB_REL_LABEL = (window.SparkingWeb && window.SparkingWeb.REL_LABEL) || {};

  function webRenderKinds() {
    const box = $("web-kinds");
    box.textContent = "";
    Object.keys(WEB_KIND_LABEL).forEach((kind) => {
      const chip = el("button", "chip" + (webState.hiddenKinds.has(kind) ? " off" : ""));
      const dot = el("span", "dot");
      dot.style.background = WEB_KIND_COLOR[kind];
      chip.appendChild(dot);
      chip.appendChild(el("span", "", WEB_KIND_LABEL[kind]));
      chip.addEventListener("click", () => {
        if (webState.hiddenKinds.has(kind)) webState.hiddenKinds.delete(kind);
        else webState.hiddenKinds.add(kind);
        webRenderKinds();
        webApplyKindFilter();
      });
      box.appendChild(chip);
    });
  }

  function webApplyKindFilter() {
    if (!webState.snapshot || !webState.graph) return;
    const hidden = webState.hiddenKinds;
    const nodes = webState.snapshot.nodes.filter((node) => !hidden.has(node.kind));
    const alive = new Set(nodes.map((node) => node.id));
    const edges = webState.snapshot.edges.filter(
      (edge) => alive.has(edge.source) && alive.has(edge.target));
    // 藏掉某类节点后，连着它的线也没了——剩下的节点里不留"孤零零的点"：
    // 没有可见连线的节点不画（和快照的口径一致）。
    const linked = new Set();
    edges.forEach((edge) => { linked.add(edge.source); linked.add(edge.target); });
    webState.graph.setData({
      nodes: nodes.filter((node) => linked.has(node.id)),
      edges,
    });
  }

  function webRenderStats() {
    const stats = (webState.snapshot && webState.snapshot.stats) || {};
    const pending = Object.values(stats.pending || {}).reduce((a, b) => a + (b || 0), 0);
    const bits = ["节点 " + (stats.nodes || 0), "连线 " + (stats.edges || 0),
                  "来源 " + (stats.sources || 0)];
    if (pending) bits.push("待入网 " + pending);
    if (stats.pruned) bits.push("已剪枝 " + stats.pruned);
    if (stats.last_sync_at) bits.push("上次同步 " + String(stats.last_sync_at).replace("T", " ").slice(0, 16));
    $("web-stats").textContent = bits.join(" · ");
  }

  function webRenderBranches() {
    const list = (webState.snapshot && webState.snapshot.branches) || [];
    $("web-branch-count").textContent = String(list.length);
    const drawer = $("web-branches");
    drawer.textContent = "";
    const head = el("div", "web-drawer-head");
    head.appendChild(el("strong", "", "记忆分支"));
    const close = el("button", "btn ghost", "关闭");
    close.addEventListener("click", () => { drawer.hidden = true; });
    head.appendChild(close);
    drawer.appendChild(head);
    if (!list.length) {
      drawer.appendChild(el("div", "muted small",
        "还没有分支：选中一个节点，点「创建分支」；以后可以「回溯」到这里。"));
      return;
    }
    list.forEach((branch) => {
      const card = el("div", "branch-card" + (branch.status === "reverted" ? " reverted" : ""));
      card.appendChild(el("div", "branch-title", branch.label || branch.node_key));
      const bits = ["切口 " + String(branch.cut_at || "").replace("T", " ").slice(0, 16),
                    branch.status === "reverted"
                      ? "已回溯 " + String(branch.reverted_at || "").replace("T", " ").slice(0, 16)
                      : "未回溯"];
      if (branch.note) bits.push("备注：" + branch.note);
      card.appendChild(el("div", "muted small", bits.join("　")));
      if (branch.status !== "reverted") {
        const revert = el("button", "btn danger", "回溯到此分支");
        revert.addEventListener("click", async () => {
          const okay = await askConfirm({
            title: "回溯到「" + (branch.label || "该分支") + "」",
            message: "会把该节点之后的记忆压缩成一份「回溯存档」（勾选「显示归档」可查看），"
              + "删除这些记忆的原始记录，并封住对应的消息区间（不会再被重新压成新记忆）。"
              + "记忆基本回到该节点时刻。此操作不可撤销。",
            okText: "确认回溯", danger: true,
          });
          if (!okay) return;
          toast("回溯中……");
          try {
            const out = await post("web_branch_revert", { branch_id: branch.branch_id });
            toast("已回溯：归档 " + (out.archived || []).length + " 份，删除摘要 "
              + (out.summaries || 0) + " 条、账本 " + (out.ledger || 0) + " 条，封口 "
              + (out.sealed || []).length + " 个会话", "ok");
            await loadWeb();
            $("web-branches").hidden = false;
          } catch (error) { toast(String(error.message || error), "error"); }
        });
        card.appendChild(revert);
      } else if (branch.archive_ref) {
        card.appendChild(el("div", "muted small",
          "回溯存档：" + String(branch.archive_ref).split(",").length + " 份"));
      }
      drawer.appendChild(card);
    });
  }

  function webRenderDetail(detail) {
    const box = $("web-detail");
    box.textContent = "";
    if (!detail) { box.hidden = true; return; }
    box.hidden = false;
    const node = detail.node;
    const head = el("div", "web-detail-head");
    const dot = el("span", "dot");
    dot.style.background = WEB_KIND_COLOR[node.kind] || "#64748b";
    head.appendChild(dot);
    head.appendChild(el("strong", "", node.label || node.key));
    const close = el("button", "btn ghost", "×");
    close.addEventListener("click", () => { box.hidden = true; });
    head.appendChild(close);
    box.appendChild(head);
    const bits = [WEB_KIND_LABEL[node.kind] || node.kind, "权重 " + node.weight];
    if (node.meta && node.meta.at) bits.push("时间 " + String(node.meta.at).replace("T", " ").slice(0, 16));
    if (node.meta && node.meta.level) bits.push("层级 L" + node.meta.level);
    if (node.status === "archived") bits.push("（归档）");
    box.appendChild(el("div", "muted small", bits.join("　")));

    const actions = el("div", "row");
    const branchBtn = el("button", "btn primary", "在此创建分支");
    branchBtn.addEventListener("click", async () => {
      const out = await askPrompt({
        title: "在「" + String(node.label || "").slice(0, 20) + "」创建分支",
        fields: [{ key: "note", label: "备注（可选）", value: "" }],
        okText: "创建",
      });
      if (!out) return;
      try {
        const result = await post("web_branch_create", { node_id: node.id, note: out.note });
        toast("分支已创建（切口 " + String(result.branch.cut_at || "").slice(0, 16) + "）", "ok");
        await loadWeb();
      } catch (error) { toast(String(error.message || error), "error"); }
    });
    actions.appendChild(branchBtn);
    const ledgerSource = (detail.sources || []).find(
      (source) => String(source.ref || "").startsWith("ledger:"));
    if (ledgerSource) {
      const askBot = el("button", "btn ghost", "让 bot 改");
      askBot.addEventListener("click", async () => {
        const want = await askPrompt({
          title: "让 bot 改这条记忆",
          fields: [{ key: "note", label: "你想让它怎么改", value: "", multiline: true,
                     placeholder: "例：这条记错了，其实我早就不学 Rust 了" }],
          okText: "交给它改",
        });
        if (!want || !String(want.note || "").trim()) return;
        try {
          toast("已交给它，稍等…", "ok");
          const entryId = String(ledgerSource.ref).slice("ledger:".length);
          const out = await post("memory_edit_by_bot", {
            node_id: "item:catalog:" + entryId,
            label: node.label, hint: node.label,
            instruction: String(want.note).trim(), scope: node.scope_id,
          });
          toast(out && out.changed ? "它已经改好了" : "它看过后没改（理由见提示）", "ok");
          if (out && out.reply) {
            await askConfirm({ title: "它说", message: String(out.reply).slice(0, 600),
                               okText: "知道了", cancelText: "" });
          }
          await loadWeb();
        } catch (error) { toast(String(error.message || error), "error"); }
      });
      actions.appendChild(askBot);
    }
    if (webState.pruneMode) {
      const add = el("button", "btn", "加入剪枝选择");
      add.addEventListener("click", () => {
        if (!webState.pruneSelection.includes(node.id)) webState.pruneSelection.push(node.id);
        webState.graph.setSelection(webState.pruneSelection);
        webUpdatePruneBar();
      });
      actions.appendChild(add);
    }
    box.appendChild(actions);

    if (detail.edges && detail.edges.length) {
      box.appendChild(el("div", "web-detail-title", "关系（" + detail.edges.length + "）"));
      const list = el("div", "web-detail-list");
      detail.edges.slice(0, 30).forEach((edge) => {
        const row = el("div", "web-detail-edge");
        const rel = WEB_REL_LABEL[edge.relation] || edge.relation;
        row.textContent = (edge.direction === "out" ? rel + " → " : "← " + rel + " ")
          + edge.other_label + (edge.weight > 1 ? "（×" + edge.weight + "）" : "");
        row.style.cursor = "pointer";
        row.addEventListener("click", () => {
          if (edge.other_id) {
            webState.graph.focus(edge.other_id);
            webLoadDetail(edge.other_id);
          }
        });
        list.appendChild(row);
      });
      box.appendChild(list);
    }
    if (detail.sources && detail.sources.length) {
      box.appendChild(el("div", "web-detail-title", "来源（" + detail.sources.length + "）"));
      const list = el("div", "web-detail-list");
      detail.sources.slice(0, 14).forEach((source) => {
        list.appendChild(el("div", "web-detail-edge", source.title || source.ref));
      });
      box.appendChild(list);
    }
  }

  async function webLoadDetail(nodeId) {
    try {
      const detail = await get("memory_web_node", { node_id: nodeId });
      webRenderDetail(detail);
    } catch (error) {
      toast(String(error.message || error), "error");
    }
  }

  function webUpdatePruneBar() {
    const bar = $("web-prune-bar");
    bar.hidden = !webState.pruneMode;
    if (!webState.pruneMode) return;
    const count = webState.pruneSelection.length;
    $("web-prune-info").textContent = "剪枝：已选 " + count + " 个节点"
      + "（沿路径丢弃低价值节点、把必要摘要向上压缩；压缩程度跟随「上下文详细程度」）";
    $("btn-web-prune-run").disabled = count < 2;
  }

  async function loadWeb() {
    const box = $("forest-graph");
    if (!window.SparkingWeb || !box) {
      toast("记忆网渲染器未加载", "error");
      return;
    }
    const params = { include_archived: webState.showArchived ? 1 : 0 };
    const scope = $("web-scope").value;
    if (scope) params.scope = scope;
    const snapshot = await get("memory_web", params);
    webState.snapshot = snapshot;
    const select = $("web-scope");
    const current = select.value;
    select.textContent = "";
    const all = el("option", "", "全部会话");
    all.value = "";
    select.appendChild(all);
    (snapshot.scopes || []).forEach((item) => {
      const option = el("option", "", item.display_name || item.conversation_id);
      option.value = item.scope_id;
      select.appendChild(option);
    });
    select.value = current;
    webRenderStats();
    webRenderBranches();
    if (!webState.graph) {
      webState.graph = window.SparkingWeb.mount(box, {
        onSelect: (node) => {
          if (webState.pruneMode) return;
          if (node) webLoadDetail(node.id);
          else webRenderDetail(null);
        },
        onMultiSelect: (nodes) => {
          webState.pruneSelection = nodes.map((node) => node.id);
          webUpdatePruneBar();
        },
      });
    }
    webApplyKindFilter();
    webUpdatePruneBar();
  }

  function webSearchHighlight() {
    const keyword = ($("web-search").value || "").trim().toLowerCase();
    if (!webState.snapshot || !webState.graph) return;
    if (!keyword) { webState.graph.setSelection([]); return; }
    const matches = webState.snapshot.nodes.filter(
      (node) => String(node.label || "").toLowerCase().includes(keyword));
    webState.graph.setSelection(matches.map((node) => node.id));
    if (matches.length) {
      webState.graph.focus(matches[0].id, 1.3);
      toast("命中 " + matches.length + " 个节点", "ok");
    } else {
      toast("没有匹配的节点", "error");
    }
  }

  async function webRunPrune() {
    if (webState.pruneSelection.length < 2) return;
    const okay = await askConfirm({
      title: "执行剪枝",
      message: "沿所选节点之间的路径：低价值叶子节点会被丢弃（不再出现在网与检索里），"
        + "必要摘要会被向上压缩。压缩程度跟随「上下文详细程度」配置。",
      okText: "剪枝", danger: true,
    });
    if (!okay) return;
    toast("剪枝中……");
    try {
      const out = await post("web_prune", { node_ids: webState.pruneSelection });
      toast("剪枝完成：路径 " + out.path_count + " 个节点，丢弃 " + out.pruned
        + "，压缩 " + (out.merged || []).length + " 次"
        + (out.notes && out.notes.length ? "（" + out.notes[0] + "）" : ""), "ok");
      webState.pruneMode = false;
      webState.pruneSelection = [];
      $("btn-web-prune").classList.remove("active");
      webState.graph.setMulti(false);
      webUpdatePruneBar();
      await loadWeb();
    } catch (error) { toast(String(error.message || error), "error"); }
  }

  async function webRunRebuild() {
    const okay = await askConfirm({
      title: "重建记忆网",
      message: "按当前全部记忆（摘要/账本/话题/印象）清空并重建整张网。分支会保留。",
      okText: "重建", danger: true,
    });
    if (!okay) return;
    toast("重建中……");
    try {
      const out = await post("web_rebuild", {});
      toast("重建完成：来源 " + (out.sources || 0) + "，节点 " + ((out.stats || {}).nodes || 0), "ok");
      await loadWeb();
    } catch (error) { toast(String(error.message || error), "error"); }
  }

  async function webNewMemory() {
    const out = await askPrompt({
      title: "新增记忆",
      fields: [
        { key: "group_id", label: "归属群号（可选，留空=第一个会话）", value: "" },
        { key: "subject", label: "标题/对象", value: "" },
        { key: "value", label: "内容", value: "", multiline: true },
      ],
      okText: "新增",
    });
    if (!out || (!out.subject && !out.value)) return;
    try {
      await post("memory_node_save", { group_id: out.group_id, subject: out.subject,
                                       value: out.value, kind: "fact" });
      toast("已新增（稍后自动入网）", "ok");
      await loadWeb();
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
      if (row.tag) title.appendChild(el("span", "tag", row.tag));
      if (Number(row.activity) >= 0) {
        title.appendChild(el("span", "tag", `活跃度 ${Number(row.activity).toFixed(2)}`));
      }
      if (row.quiet_hours) title.appendChild(el("span", "tag", `静默 ${row.quiet_hours}`));
      const use = actionBtn("切到", () => {
        state.scope = row.group_id;
        $("scope-select").value = row.group_id;
        toast("已切换到 " + row.group_id);
        refresh();
      });
      const tagBtn = actionBtn("群配置", async () => {
        const answer = await askPrompt({
          title: `${row.display_name || row.group_id} · 群配置`,
          fields: [
            { key: "tag", label: "这个群是干什么的（影响说话分寸）", value: row.tag || "",
              placeholder: "例：工作群 / 亲友群 / 游戏群" },
            { key: "activity", label: "活跃度 0~1（越低越安静；-1=跟随全局）",
              value: String(row.activity ?? -1), placeholder: "-1" },
            { key: "reply_rate", label: "回复概率 0~1（越高越爱接话；-1=跟随全局）",
              value: String(row.reply_rate ?? -1), placeholder: "-1" },
            { key: "cooldown_scale", label: "冷却倍率（2=冷却翻倍更安静；-1=跟随全局）",
              value: String(row.cooldown_scale ?? -1), placeholder: "-1" },
            { key: "quiet_hours", label: "本群静默时段（如 23-7，留空=不设）",
              value: row.quiet_hours || "", placeholder: "23-7" },
            { key: "max_actions_per_hour", label: "每小时主动上限（-1=跟随全局）",
              value: String(row.max_actions_per_hour ?? -1), placeholder: "-1" },
            { key: "note", label: "备注（只给自己看）", value: row.note || "" },
          ],
          okText: "保存",
        });
        if (!answer) return;
        const number = (value) => {
          const raw = String(value ?? "").trim();
          if (raw === "" || raw === "-1") return -1;
          const num = Number(raw);
          return Number.isFinite(num) ? num : -1;
        };
        try {
          await post("group_config_save", {
            group_id: row.group_id, scope_id: row.scope_id,
            tag: String(answer.tag || "").trim(),
            activity: number(answer.activity),
            reply_rate: number(answer.reply_rate),
            cooldown_scale: number(answer.cooldown_scale),
            quiet_hours: String(answer.quiet_hours || "").trim(),
            max_actions_per_hour: number(answer.max_actions_per_hour),
            note: String(answer.note || "").trim(),
          });
          toast("群配置已保存（立即生效）", "ok");
          loadGroups();
        } catch (error) { toast(String(error.message || error), "error"); }
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
      return itemShell(title, "", row.last_at ? "最后消息 " + row.last_at : "",
                       [use, tagBtn, wipe]);
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
      const biggest = Math.max(0, ...(report.groups || [])
        .map((g) => Number(g.input_chars || 0)));
      say(`共 ${report.groups.length} 个群、${report.messages} 条消息；` +
          `学到说话风格 ${report.style || 0} 条、黑话 ${report.lexicon || 0} 个；` +
          `新建/更新印象 ${report.impressions} 条、人物档案 ${report.profiles} 条；` +
          `模型调用 ${report.calls || 0} 次` +
          (biggest ? `（单次最多约 ${(biggest / 1000).toFixed(1)} 千字符）`
                   : "（可在配置里调 import_learn_chars 加大单次输入量）"));
      (report.groups || []).forEach((group) => {
        const tag = group.whitelisted ? "白名单（含印象学习）" : "仅入库";
        const culture = `；风格 ${group.style || 0} 条 / 黑话 ${group.lexicon || 0} 个`
          + (group.input_chars ? `（单次输入约 ${(group.input_chars / 1000).toFixed(1)} 千字符）` : "")
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
            + (group.calls ? `，模型调用 ${group.calls} 次` : "")
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

  // ---------------------------------------------------------------- 媒体与表情
  function _thumb(img, mime, label) {
    const box = el("div", "thumb-box");
    if (img) {
      const pic = el("img", "thumb-img");
      pic.src = img;
      pic.alt = label || "";
      pic.loading = "lazy";
      box.appendChild(pic);
    } else {
      box.appendChild(el("div", "thumb-empty", label || mime || "·"));
    }
    return box;
  }

  function _editableLine(label, value, onSave) {
    const wrap = el("div", "edit-line");
    const input = el("input");
    input.value = value || "";
    input.placeholder = label;
    input.addEventListener("change", () => {
      if ((input.value || "").trim() !== (value || "").trim()) onSave(input.value.trim());
    });
    wrap.appendChild(input);
    return wrap;
  }

  async function loadGallery() {
    const grid = $("sticker-grid");
    const mediaGrid = $("media-grid");
    if (!grid || !mediaGrid) return;
    grid.textContent = "";
    mediaGrid.textContent = "";
    grid.appendChild(el("div", "muted", "加载中…"));
    mediaGrid.appendChild(el("div", "muted", "加载中…"));
    let data;
    try {
      data = await get("stickers");
    } catch (error) {
      grid.textContent = "";
      grid.appendChild(el("div", "empty", String(error.message || error)));
      return;
    }
    grid.textContent = "";
    const stickers = data.items || [];
    if (!stickers.length) {
      grid.appendChild(el("div", "empty", "表情包库是空的——它在群里看到好表情会自己收"));
    }
    stickers.forEach((row) => {
      const item = el("div", "thumb-item");
      item.appendChild(_thumb(row.thumb, row.media_type, "表情"));
      const meta = el("div", "thumb-meta");
      meta.appendChild(el("div", "thumb-title", row.summary || row.file || row.sticker_id));
      meta.appendChild(el("div", "muted",
        `${row.uses || 0} 次使用 · ${(row.size / 1024).toFixed(0)} KB`));
      item.appendChild(meta);
      item.appendChild(_editableLine("一句话概要（bot 选表情用）", row.summary, async (value) => {
        try {
          await post("sticker_desc_save", {
            sticker_id: row.sticker_id, summary: value,
            description: row.description || "",
          });
          toast("概要已保存", "ok");
          loadGallery();
        } catch (error) { toast(String(error.message || error), "error"); }
      }));
      item.appendChild(_editableLine("详细描述（什么时候用）", row.description, async (value) => {
        try {
          await post("sticker_desc_save", {
            sticker_id: row.sticker_id, summary: row.summary || "",
            description: value,
          });
          toast("描述已保存", "ok");
          loadGallery();
        } catch (error) { toast(String(error.message || error), "error"); }
      }));
      const actions = el("div", "thumb-actions");
      actions.appendChild(actionBtn("删除", async () => {
        if (!(await askConfirm({
          title: "删除这张表情", danger: true, okText: "删除",
          message: row.summary || row.file || row.sticker_id,
        }))) return;
        try {
          await post("sticker_delete", { sticker_id: row.sticker_id });
          toast("已删除", "ok");
          loadGallery();
        } catch (error) { toast(String(error.message || error), "error"); }
      }, "danger"));
      item.appendChild(actions);
      grid.appendChild(item);
    });

    let mediaData;
    try {
      mediaData = await get("media", { limit: 60 });
    } catch (error) {
      mediaGrid.textContent = "";
      mediaGrid.appendChild(el("div", "empty", String(error.message || error)));
      return;
    }
    mediaGrid.textContent = "";
    const mediaItems = mediaData.items || [];
    if (!mediaItems.length) {
      mediaGrid.appendChild(el("div", "empty", "媒体归档是空的"));
    }
    mediaItems.forEach((row) => {
      const item = el("div", "thumb-item");
      const isImage = (row.mime || "").startsWith("image/");
      item.appendChild(_thumb(row.thumb, row.mime, isImage ? "图片" : (row.kind || "文件")));
      const meta = el("div", "thumb-meta");
      meta.appendChild(el("div", "thumb-title",
        `${row.kind || ""} · ${(row.size / 1024).toFixed(0)} KB`));
      meta.appendChild(el("div", "muted", row.at || ""));
      item.appendChild(meta);
      item.appendChild(_editableLine("备注（bot 会按这个想起它）", row.note, async (value) => {
        try {
          await post("media_note_save", { media_id: row.media_id, note: value });
          toast("备注已保存", "ok");
        } catch (error) { toast(String(error.message || error), "error"); }
      }));
      const actions = el("div", "thumb-actions");
      actions.appendChild(actionBtn("删除", async () => {
        if (!(await askConfirm({
          title: "删除这条媒体", danger: true, okText: "删除",
          message: `${row.kind || ""} ${row.at || ""}`,
        }))) return;
        try {
          await post("media_delete", { media_id: row.media_id });
          toast("已删除", "ok");
          loadGallery();
        } catch (error) { toast(String(error.message || error), "error"); }
      }, "danger"));
      item.appendChild(actions);
      mediaGrid.appendChild(item);
    });
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

  // 上下文详细程度（压缩档次）：调低会先压缩一次记忆，再按更短的上下文运行
  async function loadContextDetail() {
    const select = $("detail-level");
    const hint = $("detail-hint");
    try {
      const data = await get("context_detail");
      if (select) select.value = String(data.level || 3);
      if (hint) {
        const limits = data.limits || {};
        hint.textContent = "当前：" + (data.name || "")
          + "（记忆预算约 " + (data.char_budget || 0) + " 字符 / 最近消息 "
          + (limits.recent_limit || 0) + " 条 / 摘要 " + (limits.summary_limit || 0)
          + " 条 / 证据 " + (limits.evidence_limit || 0) + " 条）";
      }
    } catch (error) {
      if (hint) hint.textContent = String(error.message || error);
    }
  }

  async function saveContextDetail() {
    const select = $("detail-level");
    if (!select) return;
    try {
      const out = await post("context_detail_set", { level: Number(select.value) });
      toast("已切到「" + (out.name || "") + "」"
        + (out.compressed ? "，并先压缩了一次记忆" : ""), "ok");
      loadContextDetail();
    } catch (error) {
      toast(String(error.message || error), "error");
    }
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
      if (row.origin_label) title.appendChild(el("span", "tag", row.origin_label));
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
      if (row.deletable) {
        buttons.push(actionBtn("删除", async () => {
          if (!(await askConfirm({
            title: "删除这个拓展", danger: true, okText: "删除",
            message: `${row.name}（用户上传的拓展；文件与配置一起删除）`,
          }))) return;
          try {
            await post("extension_delete", { id: row.id });
            toast("已删除", "ok");
            loadCaps();
          } catch (error) { toast(String(error.message || error), "error"); }
        }, "danger"));
      }
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

  // 上传自定义拓展：zip 整包 base64 发过去（限制 8MB，够装脚本与资源）
  async function runExtensionUpload() {
    const input = $("ext-file");
    const log = $("ext-log");
    const file = input && input.files && input.files[0];
    if (!file) return toast("先选一个拓展 zip", "error");
    const show = (value) => { if (log) { log.hidden = false; log.textContent = value; } };
    show(`正在上传 ${file.name}（${(file.size / 1024).toFixed(0)} KB）…`);
    try {
      const data = await readPart(file, 0, file.size);
      const out = await post("extension_upload", { filename: file.name, data });
      show(`已安装：${out.display_name || out.id}（${out.id}）` + NL
           + (out.tools && out.tools.length ? `工具：${out.tools.join("、")}` : "（无工具）")
           + NL + "可以在下面列表里开关、配置或删除。");
      toast("拓展已安装", "ok");
      input.value = "";
      loadCaps();
    } catch (error) {
      show("上传失败：" + String(error.message || error));
      toast(String(error.message || error), "error");
    }
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
    $("btn-web-refresh").addEventListener("click", () => loadWeb().catch((error) => toast(String(error.message || error), "error")));
    $("btn-web-archived").addEventListener("click", () => {
      webState.showArchived = !webState.showArchived;
      $("btn-web-archived").classList.toggle("active", webState.showArchived);
      loadWeb().catch((error) => toast(String(error.message || error), "error"));
    });
    $("btn-web-branches").addEventListener("click", () => {
      const drawer = $("web-branches");
      drawer.hidden = !drawer.hidden;
    });
    $("btn-web-prune").addEventListener("click", () => {
      webState.pruneMode = !webState.pruneMode;
      $("btn-web-prune").classList.toggle("active", webState.pruneMode);
      webState.pruneSelection = [];
      if (webState.graph) { webState.graph.setMulti(webState.pruneMode); webState.graph.setSelection([]); }
      webUpdatePruneBar();
      toast(webState.pruneMode ? "剪枝模式：点选若干节点" : "已退出剪枝", "ok");
    });
    $("btn-web-prune-cancel").addEventListener("click", () => {
      webState.pruneMode = false;
      webState.pruneSelection = [];
      $("btn-web-prune").classList.remove("active");
      if (webState.graph) webState.graph.setMulti(false);
      webUpdatePruneBar();
    });
    $("btn-web-prune-run").addEventListener("click", webRunPrune);
    $("btn-web-rebuild").addEventListener("click", webRunRebuild);
    $("btn-web-new").addEventListener("click", webNewMemory);
    $("web-scope").addEventListener("change", () => loadWeb().catch((error) => toast(String(error.message || error), "error")));
    $("web-search").addEventListener("input", webSearchHighlight);
    $("btn-ext-refresh").addEventListener("click", loadCaps);
    $("btn-ext-upload").addEventListener("click", runExtensionUpload);
    $("btn-refresh-inject").addEventListener("click", loadInjections);
    $("btn-detail-save").addEventListener("click", saveContextDetail);
    $("btn-refresh-culture").addEventListener("click", loadCulture);
    $("btn-learn-run").addEventListener("click", runImport);
    $("btn-learn-refresh").addEventListener("click", loadLearn);
    $("btn-export-memory").addEventListener("click", () => runExport(true));
    $("btn-export-server").addEventListener("click", () => runExport(false));
    $("btn-backup-refresh").addEventListener("click", loadBackup);
    $("btn-import-memory").addEventListener("click", runMemoryImport);
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
    $("btn-gallery-refresh").addEventListener("click", loadGallery);
    $("btn-media-refresh").addEventListener("click", loadGallery);
    $("btn-clear-stickers2").addEventListener("click", async () => {
      if (!(await askConfirm({ title: "清空表情包库", danger: true, okText: "清空" }))) return;
      try { await post("sticker_clear", {}); toast("已清空", "ok"); loadGallery(); }
      catch (error) { toast(String(error.message || error), "error"); }
    });
    $("btn-clear-media2").addEventListener("click", async () => {
      if (!(await askConfirm({ title: "清空媒体归档", danger: true, okText: "清空" }))) return;
      try { const out = await post("media_clear", {}); toast(`已删除 ${out.removed_files} 个文件`, "ok"); loadGallery(); }
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
