// 记忆森林（独立页）：桥接 + 圆形节点图
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
    toast._timer = setTimeout(() => { box.hidden = true; }, kind === "error" ? 4200 : 2000);
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

  async function get(path, params) {
    const bridge = await bridgeReady();
    if (bridge) return unwrap(await bridge.apiGet(endpointOf(path), params || {}), "读取失败");
    const query = new URLSearchParams();
    Object.keys(params || {}).forEach((key) => {
      const value = params[key];
      if (value !== undefined && value !== null) query.set(key, String(value));
    });
    return sameOrigin(path + (query.toString() ? "?" + query.toString() : ""));
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

  // ---------------------------------------------------------------- 挂载
  let forest = null;

  function mountForest() {
    forest = window.SparkingForest.mount($("graph"), {
      getTree: (nodeId) => nodeId ? get("memory_tree", { node: nodeId })
                                 : get("memory_tree", {}),
      onAddChild: async (parent) => {
        const parentGroup = String(parent.id).startsWith("scope:")
          ? String(parent.id).split(":")[1] : "";
        const out = await modal({
          title: `在「${parent.label}」下新增记忆`, okText: "新增",
          fields: [
            { key: "subject", label: "标题/对象", value: "" },
            { key: "value", label: "内容", value: "", multiline: true },
            { key: "kind", label: "类型（fact/preference/agreement/identity/task）", value: "fact" },
          ],
        });
        if (!out || (!out.subject && !out.value)) return;
        try {
          await post("memory_node_save", { node_id: "", subject: out.subject,
                                           value: out.value, kind: out.kind || "fact",
                                           group_id: parentGroup });
          toast("已新增", "ok");
          forest.reload($("filter").value);
        } catch (error) { toast(String(error.message || error), "error"); }
      },
      onEdit: async (node) => {
        const out = await modal({
          title: "编辑记忆", okText: "保存",
          fields: [
            { key: "subject", label: "标题/对象", value: node.label || "" },
            { key: "value", label: "内容", value: node.hint || "", multiline: true },
            { key: "kind", label: "类型", value: node.kind || "fact" },
          ],
        });
        if (!out) return;
        try {
          await post("memory_node_save", { node_id: node.id, subject: out.subject,
                                           value: out.value, kind: out.kind || "fact" });
          toast("已保存", "ok");
          forest.reload($("filter").value);
        } catch (error) { toast(String(error.message || error), "error"); }
      },
      onDelete: async (node) => {
        const okay = await modal({
          title: "删除这个记忆节点", danger: true, okText: "删除",
          fields: [{ key: "note", label: `确认删除「${node.label}」？可留一句备注（可选）`, value: "" }],
        });
        if (!okay) return;
        try {
          await post("memory_node_delete", { node_id: node.id });
          toast("已删除", "ok");
          forest.reload($("filter").value);
        } catch (error) { toast(String(error.message || error), "error"); }
      },
    });
  }

  function bind() {
    $("btn-refresh").addEventListener("click", () => forest && forest.reload($("filter").value));
    $("filter").addEventListener("input", () => forest && forest.reload($("filter").value));
    $("btn-new").addEventListener("click", async () => {
      const out = await modal({
        title: "新增记忆", okText: "新增",
        fields: [
          { key: "group_id", label: "归属群号（可选，留空=第一个会话）", value: "" },
          { key: "subject", label: "标题/对象", value: "" },
          { key: "value", label: "内容", value: "", multiline: true },
          { key: "kind", label: "类型（fact/preference/agreement/identity/task）", value: "fact" },
        ],
      });
      if (!out || (!out.subject && !out.value)) return;
      try {
        await post("memory_node_save", { group_id: out.group_id, subject: out.subject,
                                         value: out.value, kind: out.kind || "fact" });
        toast("已新增", "ok");
        forest.reload($("filter").value);
      } catch (error) { toast(String(error.message || error), "error"); }
    });
  }

  function start() {
    if (!window.SparkingForest) {
      $("graph").innerHTML = '<div class="forest-empty">渲染器未加载（forest.js）</div>';
      return;
    }
    bind();
    mountForest();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
