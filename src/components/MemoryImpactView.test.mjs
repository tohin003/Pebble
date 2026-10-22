import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Compile only checked-in sources; memory records are data and are never evaluated.
const require = createRequire(import.meta.url);
const calls = [];
const replies = {};
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: structuredClone(params) });
    const reply = replies[method];
    if (!reply) throw new Error("Unexpected RPC " + method);
    return reply(params);
  },
};
function evaluate(code, imports = {}) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(
    (name) => (name === "../bridge" ? bridge : imports[name] || require(name)), module, module.exports);
  return module.exports;
}
const shortcuts = evaluate(readFileSync(new URL("../shortcuts.ts", import.meta.url), "utf8"));
function compile(filename) {
  const source = readFileSync(new URL(filename, import.meta.url), "utf8");
  const descriptor = parse(source, { filename }).descriptor;
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  const component = evaluate(script.content, { "../shortcuts": shortcuts }).default;
  component.render = evaluate(template.code).render;
  return { component, source };
}
const { component: Impact, source } = compile("./MemoryImpactView.vue");
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source));
assert.deepEqual([...new Set([...source.matchAll(/\brpc(?:<[^>]*>)?\(\s*([^,)]*)/g)].map((m) => m[1].trim()))].sort(), ['"memory_candidates"', '"memory_list"', '"run_get"']);
assert.ok(!/memory_(save|delete|clear|maintain|resolve|export)/.test(source), "the view changes no memory");
for (const surface of ["./NativeArtifactRenderer.vue", "./ArtifactPanel.vue", "./ModsSettings.vue", "../Pet.vue"])
  assert.ok(!readFileSync(new URL(surface, import.meta.url), "utf8").includes("MemoryImpactView"), surface + " never renders the view");
// The six layers match the backend exactly (memory.KINDS).
const kinds = [...readFileSync(new URL("../../backend/pebble/memory.py", import.meta.url), "utf8").match(/KINDS = \{([^}]*)\}/)[1].matchAll(/"([a-z]+)": "([^"]+)"/g)].map((m) => [m[1], m[2]]);
assert.equal(kinds.length, 6);
for (const [id, name] of kinds) assert.ok(source.includes(`["${id}", "${name}"]`), id);

const SECRET = "SECRET-MEMORY-CONTENT";
const memory = (kind, chat_id = "chat-a") => ({ id: kind + Math.random(), kind, content: SECRET, scope: "global", chat_id, origin: "learned", created: 1, updated: 1, expires: null });
const candidate = (kind, status, run_id = "run-1") => ({ id: kind + status + Math.random(), run_id, chat_id: "chat-a", kind, scope: "global", slot: "s", content: SECRET, status, reason: null });
const layers = kinds.map(([id, name], i) => ({ id, name: name + " (host)", count: 10 + i, enabled: id !== "temporal" }));
function standard() {
  replies.memory_list = () => [memory("semantic"), memory("semantic"), memory("semantic", "chat-b"), memory("profile"), memory("seventh"), null];
  replies.memory_candidates = () => [candidate("semantic", "pending"), candidate("semantic", "promoted"), candidate("semantic", "rejected"), candidate("semantic", "rejected"),
    candidate("procedural", "expired"), candidate("episodic", "pending", "run-0"), candidate("profile", "constructor"), candidate("profile", "__proto__"), candidate("seventh", "pending")];
  replies.run_get = (p) => ({ id: p.run_id, chat_id: p.id, tools: [
    { call_id: "1:a", name: "Read", state: "completed" }, { call_id: "2:b", name: "Write", state: "uncertain" },
    { call_id: "3:<b>", name: "<img src=x onerror=host()>", state: "uncertain" }, { call_id: "4:c", name: "RunTests", state: "running" }, { call_id: "5:d", name: "X", state: "constructor" }] });
}

function node(type, text = "") {
  return { type, text, children: [], props: {}, parent: null, value: "", addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document };
}
const renderer = createRenderer({
  createElement: (type) => node(type), createText: (text) => node("#text", text), createComment: (text) => node("#comment", text),
  insert(child, parent, anchor) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child);
  },
  remove(child) { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null; },
  setText(child, text) { child.text = text; },
  setElementText(child, text) { child.text = text; child.children = []; },
  patchProp(child, key, _previous, value) { child.props[key] = value; },
  parentNode: (child) => child.parent,
  nextSibling: (child) => child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
});
const textOf = (el) => (el.type === "#comment" ? "" : el.text) + el.children.map(textOf).join("");
const all = (el) => [el, ...el.children.flatMap(all)];
async function settle() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick(); } }
const tables = (root) => all(root).filter((el) => el.type === "table");
const rowsOf = (t) => all(t).filter((el) => el.type === "tr" && el.parent?.type === "tbody");
const cells = (tr) => tr.children.map(textOf);
const vueErrors = [];
function mount(props) {
  const root = node("root");
  let bindings;
  const instrumented = { ...Impact, setup(p, context) { bindings = Impact.setup(p, context); return bindings; } };
  const app = renderer.createApp({ setup: () => () => h(instrumented, props) });
  app.config.errorHandler = (error) => { vueErrors.push(error); };
  app.mount(root);
  return { root, bindings: () => bindings, unmount: () => app.unmount() };
}

standard();
const props = reactive({ chatId: "chat-a", runId: "run-1", layers, enabled: false });
const view = mount(props);
await settle();
assert.deepEqual(calls.map((c) => [c.method, c.params]), [["memory_list", {}], ["memory_candidates", { id: "chat-a", limit: 100 }], ["run_get", { id: "chat-a", run_id: "run-1" }]]);
let text = textOf(view.root);
assert.ok(text.includes("Memory impact · task run-1"));
assert.ok(text.includes("Personalized memory is off: nothing is learned or recalled for personalization. Stored records stay until you forget them."));
const [layerTable, receiptTable, uncertainTable] = tables(view.root);
assert.deepEqual(rowsOf(layerTable).map(cells), [
  ["Working context (host)", "on", "10", "n/a", "—", "—", "—"],
  ["Past experiences (host)", "on", "11", "0", "0", "0", "—"],
  ["Facts & knowledge (host)", "on", "12", "2", "1", "1", "rejected 2"],
  ["Procedures (host)", "on", "13", "0", "0", "0", "expired 1"],
  ["Time-sensitive information (host)", "off", "14", "0", "0", "0", "—"],
  ["Your preferences (host)", "on", "15", "1", "0", "0", "constructor 1 ·   proto   1"],
], "exactly six rows; the episodic candidate from another run is not this task's");
assert.ok(text.includes("2 records have an unrecognized layer and are not counted above."), "a seventh kind is an omission, never a row");
assert.ok(!text.includes(SECRET), "counts only: no memory content is rendered");
assert.ok(text.includes("Counts only: this view shows no memory content. Memory never grants permissions."));
assert.deepEqual(rowsOf(receiptTable).map(cells), [["completed", "1"], ["running", "1"], ["uncertain", "2"], ["other", "1"]]);
assert.deepEqual(rowsOf(uncertainTable).map(cells), [["Write", "2:b"], ["<img src=x onerror=host()>", "3:<b>"]]);
assert.ok(text.includes("Inspect and settle uncertain outcomes in the task panel before continuing."));
for (const table of tables(view.root)) {
  const rows = rowsOf(table);
  assert.deepEqual(rows.map((tr) => tr.props.tabindex), rows.map((_, i) => (i ? -1 : 0)));
  const body = { children: [] };
  for (const tabIndex of [0, -1]) body.children.push({ tagName: "TR", parentElement: body, tabIndex, focused: 0, focus() { this.focused++; } });
  rows[0].props.onKeydown({ key: "ArrowDown", target: body.children[0], preventDefault() {} });
  assert.equal(body.children[1].focused, 1);
}

// Without host layer counts, stored counts come from the bounded list and say so.
replies.memory_list = () => Array.from({ length: 300 }, (_, i) => memory(i % 2 ? "semantic" : "episodic", i % 3 ? "chat-a" : "chat-z"));
replies.memory_candidates = () => Array.from({ length: 100 }, () => candidate("semantic", "pending"));
replies.run_get = (p) => ({ id: p.run_id, chat_id: p.id, tools: Array.from({ length: 23 }, (_, i) => ({ call_id: i + ":u", name: "Write", state: "uncertain" })) });
props.layers = undefined; props.enabled = true;
await view.bindings().load(); await settle();
text = textOf(view.root);
assert.deepEqual(rowsOf(tables(view.root)[0]).slice(0, 3).map(cells), [
  ["Working context", "—", "—", "n/a", "—", "—", "—"],
  ["Past experiences", "—", "≥ 150", "≥ 100", "0", "0", "—"],
  ["Facts & knowledge", "—", "≥ 150", "≥ 100", "100", "0", "—"],
]);
assert.ok(!text.includes("Personalized memory is off"));
assert.ok(text.includes("Only the newest 300 memory records were read; counts marked ≥ may be higher."));
assert.ok(text.includes("Only the newest 100 learning candidates of this conversation were read."));
assert.equal(rowsOf(tables(view.root)[2]).length, 20);
assert.ok(text.includes("3 more uncertain outcomes are not listed."));

// Each source fails on its own, with its reason; nothing is invented in its place.
standard();
replies.memory_list = () => { throw new Error("Memory store is locked."); };
replies.run_get = (p) => ({ id: "run-other", chat_id: p.id, tools: [] });
await view.bindings().load(); await settle();
text = textOf(view.root);
assert.ok(text.includes("Memory records: Memory store is locked.") && text.includes("Task receipts: The reply belongs to a different task."));
assert.deepEqual(cells(rowsOf(tables(view.root)[0])[2]), ["Facts & knowledge", "—", "—", "—", "1", "1", "rejected 2"]);
assert.equal(tables(view.root).length, 1, "no receipt table without a verified reply");
standard();
replies.memory_candidates = async () => ({ not: "a list" });
await view.bindings().load(); await settle();
assert.ok(textOf(view.root).includes("Learning candidates: The learning candidates were malformed."));

// Conversation scope (no task): no run_get, candidates across the conversation.
standard();
calls.length = 0;
props.runId = null; await settle();
assert.deepEqual(calls.map((c) => c.method), ["memory_list", "memory_candidates"]);
text = textOf(view.root);
assert.ok(text.includes("Memory impact · this conversation") && text.includes("Proposed by this conversation: waiting"));
assert.deepEqual(cells(rowsOf(tables(view.root)[0])[1]).slice(4, 6), ["1", "0"], "the other run's episodic candidate now counts");
assert.ok(!text.includes("Task receipts"));

// A late reply for a previous task cannot overwrite the current one.
let release;
replies.run_get = () => new Promise((resolve) => (release = resolve));
props.runId = "run-1"; await settle();
const stale = release;
props.runId = "run-2"; await settle();
const fresh = release;
fresh({ id: "run-2", chat_id: "chat-a", tools: [{ call_id: "9:z", name: "Edit", state: "uncertain" }] });
await settle();
stale({ id: "run-1", chat_id: "chat-a", tools: [] });
await settle();
assert.deepEqual(view.bindings().tools.value.map((t) => t.call_id), ["9:z"]);
view.unmount();

const html = await renderToString(createSSRApp({ ...Impact, setup(p, context) { const b = Impact.setup(p, context); b.tools.value = [{ call_id: "1:<b>", name: "<script>approve()</script>", state: "uncertain" }]; return b; } }, { chatId: "chat-a", runId: "run-1" }));
assert.ok(html.includes("&lt;script&gt;approve()&lt;/script&gt;") && !/<(img|script|iframe|b)\b/.test(html));
assert.ok(html.includes('<th scope="row">Working context</th>') && html.includes("<caption>Six memory layers (counts only)</caption>"));
assert.deepEqual(vueErrors, []);
console.log("Memory impact fixtures passed: six layers exactly, host counts or bounded list counts marked, per-task candidates by outcome, no memory content, unknown layers as omissions, receipts and uncertain outcomes, per-source failures, scoping and late replies, keyboard rows and escaping.");
