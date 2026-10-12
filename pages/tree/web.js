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
      const cell = 150;
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
              let dx = pos.x - target.x, dy = pos.y - target.y;
              let distance = Math.sqrt(dx * dx + dy * dy) || 1;
              if (distance > cell * 1.6) return;
              const force = 1400 / (distance * distance);
              fx += (dx / distance) * force;
              fy += (dy / distance) * force;
            });
          }
        }
        fx += (W / 2 - pos.x) * 0.012;
        fy += (H / 2 - pos.y) * 0.012;
        pos.vx = (pos.vx + fx) * 0.82;
        pos.vy = (pos.vy + fy) * 0.82;
      });
      edges.forEach((edge) => {
        const a = positions.get(edge.source);
        const b = positions.get(edge.target);
        if (!a || !b) return;
        let dx = b.x - a.x, dy = b.y - a.y;
        const distance = Math.sqrt(dx * dx + dy * dy) || 1;
        const target = 96;
        const strength = 0.018 * Math.min(3, Math.max(1, Number(edge.weight) || 1));
        const force = (distance - target) * strength;
        const ux = dx / distance, uy = dy / distance;
        a.vx += ux * force; a.vy += uy * force;
        b.vx -= ux * force; b.vy -= uy * force;
      });
      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        if (node.id === (dragNode && dragNode.id)) return;
        pos.x += Math.max(-12, Math.min(12, pos.vx));
        pos.y += Math.max(-12, Math.min(12, pos.vy));
        pos.x = Math.max(24, Math.min(W - 24, pos.x));
        pos.y = Math.max(24, Math.min(H - 24, pos.y));
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
        ctx.strokeStyle = edge.archived ? "rgba(148,163,184,0.35)" : "rgba(100,116,139,0.34)";
        ctx.lineWidth = Math.min(3.2, 0.8 + Math.log2(weight + 1) * 0.9) / view.scale;
        if (edge.archived) ctx.setLineDash([5, 5]);
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
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
      const showLabels = view.scale > 1.1;
      nodes.forEach((node) => {
        const pos = positions.get(node.id);
        if (!pos) return;
        const important = (Number(node.weight) || 1) >= 3 || selection.has(node.id)
          || (hover && hover.id === node.id);
        if (!important && !showLabels) return;
        const label = String(node.label || "").slice(0, 14);
        ctx.font = "11px 'Segoe UI', 'Microsoft YaHei', sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        const y = pos.y + radiusOf(node) + 3;
        ctx.strokeStyle = "rgba(255,255,255,0.85)";
        ctx.lineWidth = 3 / view.scale;
        ctx.strokeText(label, pos.x, y);
        ctx.fillStyle = "#0f172a";
        ctx.fillText(label, pos.x, y);
      });
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
        nextNodes.forEach((node, index) => {
          const old = previous.get(node.id);
          if (old) {
            positions.set(node.id, old);
          } else {
            const angle = index * 2.399963;
            const radius = 40 + Math.sqrt(index + 1) * 26;
            positions.set(node.id, {
              x: W / 2 + Math.cos(angle) * radius,
              y: H / 2 + Math.sin(angle) * radius,
              vx: 0, vy: 0,
            });
          }
        });
        data = { nodes: nextNodes, edges: nextEdges };
        const alive = new Set(nextNodes.map((node) => node.id));
        Array.from(selection).forEach((id) => { if (!alive.has(id)) selection.delete(id); });
        schedule(90);
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
