// 渲染器冒烟测试：用最小 DOM 桩跑通 mount → 布局 → 渲染 → 菜单
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..", "pages", "tree", "forest.js");
const source = fs.readFileSync(root, "utf8");

function makeEl(tag) {
  const el = {
    tagName: tag,
    children: [],
    attrs: {},
    dataset: {},
    style: {},
    classList: { add() {}, remove() {} },
    hidden: false,
    set innerHTML(value) { this._html = value; if (!value) this.children = []; },
    get innerHTML() { return this._html || ""; },
    textContent: "",
    listeners: {},
    setAttribute(k, v) { this.attrs[k] = v; if (k === "class") this._class = v; },
    set className(v) { this._class = v; },
    get className() { return this._class || ""; },
    appendChild(child) { this.children.push(child); return child; },
    remove() {},
    addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); },
    dispatch(name, event) { (this.listeners[name] || []).forEach((fn) => fn(event || {})); },
    closest() { return null; },
    querySelector() { return null; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 900, height: 600 }; },
  };
  return el;
}

global.document = {
  createElement: makeEl,
  createElementNS: (_ns, tag) => makeEl(tag),
  addEventListener() {},
};
global.window = { addEventListener() {} };

eval(source);

const tree = {
  "root": { roots: [
    { id: "scope:s1", type: "scope", label: "123", count: 4, time: "", hint: "群A" },
    { id: "scope:s2", type: "scope", label: "456", count: 2, time: "", hint: "群B" },
  ] },
  "scope:s1": { children: [
    { id: "cat:s1:summaries", type: "category", label: "分层摘要", count: 3, hint: "压缩结果" },
    { id: "cat:s1:catalog", type: "category", label: "记忆账本", count: 1, hint: "证据" },
  ] },
  "cat:s1:catalog": { children: [
    { id: "item:catalog:e1", type: "catalog", label: "小明喜欢猫", hint: "两只布偶",
      time: "2 天前", time_granularity: "相对时间", kind: "fact", status: "active",
      editable: true, count: 2 },
  ] },
  "item:catalog:e1": { children: [
    { id: "item:message:m1", type: "message", label: "甲", hint: "原话",
      time: "2026-10-06 12:34", time_granularity: "精确到分钟", count: 0 },
  ] },
};

const calls = [];
const container = makeEl("div");
const forest = window.SparkingForest.mount(container, {
  getTree: async (nodeId) => {
    calls.push(nodeId);
    return tree[nodeId || "root"];
  },
  onEdit: (n) => calls.push("edit:" + n.id),
  onDelete: (n) => calls.push("del:" + n.id),
  onAddChild: (n) => calls.push("add:" + n.id),
});

function findNodes(el, out = []) {
  if (el.tagName === "g" && (el._class || el.attrs.class) === "forest-node") out.push(el);
  el.children.forEach((child) => findNodes(child, out));
  return out;
}

(async () => {
  await new Promise((r) => setTimeout(r, 30));
  let nodes = findNodes(container);
  console.log("初始节点数:", nodes.length, "（应为 2 个会话）");

  const first = nodes[0];
  first.dispatch("click", { clientX: 100, clientY: 100, stopPropagation() {} });
  await new Promise((r) => setTimeout(r, 20));
  const menu = container.children.find((c) => (c._class || c.attrs.class) === "forest-menu");
  console.log("菜单项:", menu.children.map((b) => b.textContent).join(" / "));

  // 展开第一个会话（懒加载子节点）
  await forest.expand("scope:s1");
  nodes = findNodes(container);
  console.log("展开后节点数:", nodes.length, "（会话+类目）");
  console.log("getTree 调用:", calls.join(","));

  await forest.expand("cat:s1:catalog");
  await forest.expand("item:catalog:e1");
  nodes = findNodes(container);
  console.log("全部展开后节点数:", nodes.length);
  const circles = nodes.filter((n) => n.children.some((c) => c.tagName === "circle"));
  const labels = nodes.map((n) =>
    (n.children.find((c) => c.tagName === "text") || {}).textContent).filter(Boolean);
  console.log("带圆形的节点:", circles.length, "| 标签:", labels.join(", "));
  console.log("OK");
})();
