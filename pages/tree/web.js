// ===== 记忆网渲染器（MemoryWeb）=====
// 零依赖 Canvas 力导向图：节点 = 人物 / 会话 / 话题 / 事实 / 摘要，连线 = 关系。
// 与 pages/memory/app.js 内联版本同源，两份内容需保持一致。
// 交互：滚轮缩放、拖拽平移、拖动节点、悬停提示、点击选中、多选（剪枝用）。
// 用法：const web = window.SparkingWeb.mount(container, { onSelect, onMultiSelect });
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
              const push = (minimum - distance) * 0.5;
              const ux = dx / distance, uy = dy / distance;
              pos.x -= ux * push; pos.y -= uy * push;
              target.x += ux * push; target.y += uy * push;
            });
          }
        }
      });
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
        schedule(260);
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
