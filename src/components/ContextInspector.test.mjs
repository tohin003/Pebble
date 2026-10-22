import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Compile only checked-in sources; run records are data and are never evaluated.
const require = createRequire(import.meta.url);
const calls = [];
let reply = async () => { throw new Error("no reply"); };
const bridge = { native: true, async rpc(method, params = {}) { calls.push({ method, params: structuredClone(params) }); return reply(method, params); } };
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
const { component: Inspector, source } = compile("./ContextInspector.vue");
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source));
assert.deepEqual([...source.matchAll(/\brpc(?:<[^>]*>)?\(\s*([^,)]*)/g)].map((m) => m[1].trim()), ['"run_get"'], "reads one host record; runs no inference");
for (const surface of ["./NativeArtifactRenderer.vue", "./ArtifactPanel.vue", "./ModsSettings.vue", "../Pet.vue"])
  assert.ok(!readFileSync(new URL(surface, import.meta.url), "utf8").includes("ContextInspector"), surface + " never renders the inspector");

// A run_get packet shaped like app.run_get and context_manifest.project (manifest v1).
const hostile = "<img src=x onerror=host()>";
const empty = { units: 0, messages: 0, selected_units: 0, omitted_units: 0, selected_estimated_tokens: 0, omitted_estimated_tokens: 0 };
function manifest(extra = {}) {
  return {
    version: 1, status: "retired", dispatchable: true,
    limits: { context: 8192, output_reserve: 1024, input: 7168 },
    input_tokens: { before: 9100, after: 6900, protected_floor: 2100, tools_only: null },
    estimation: { window_method: "local_estimate", unit_method: "local_estimate", units_additive: true, counter_calls: 4 },
    tools: { count: 12, fingerprint: "t", fingerprints: [], estimated_tokens: 1800 },
    messages: { input: 160, selected: 120 },
    omitted: { units: 40, messages: 40, estimated_tokens: 2200 },
    summary: { policy: { ...empty, units: 1, messages: 1, selected_units: 1, selected_estimated_tokens: 300 }, goal: empty, tool_exchange: { ...empty, units: 149, messages: 159, selected_units: 109, omitted_units: 40, selected_estimated_tokens: 4600, omitted_estimated_tokens: 2200 } },
    units: Array.from({ length: 150 }, (_, i) => ({ unit: i, category: i ? "tool_exchange" : "policy", first_index: i, last_index: i, roles: ["assistant"], markers: [], opaque_fields: [], tool_calls: 0, images: 0, fingerprint: "f", estimated_tokens: 40, protected: !i, selected: i === 0 || i > 40, reason: i ? (i > 40 ? "within_budget" : "retired_for_budget") : "protected_policy" })),
    units_total: 150, units_listed: 150, window_fingerprint: "w",
    not_verified: ["source_freshness", "source_truth", "permissions", "constructor"],
    ...extra,
  };
}
function packet(extra = {}) {
  return {
    id: "run-1", chat_id: "chat-a", goal: "never shown", state: "paused", steps: 7, config: { workspace: "/tmp/project" },
    tools: [], plan: null, questions: [], continuation: null,
    context: { step: 7, manifest: manifest() },
    sources: [
      { run_id: "run-1", receipt_id: "1:read", path: "src/a.ts", file_hash: "h", disk_hash: "h", basis: "disk", status: "current", checked: 1700000000, current_hash: "h" },
      { run_id: "run-1", receipt_id: "2:read", path: hostile + ".md", file_hash: "h", disk_hash: "h", basis: "disk", status: "current", checked: 1700000000, current_hash: "h" },
      { run_id: "run-1", receipt_id: "3:read", path: "src/a.ts", file_hash: "h2", disk_hash: "h2", basis: "draft", status: "changed", checked: 1700000100, current_hash: "x" },
      null,
    ],
    handoff: { manifest: {}, created: 1700000200 }, compactions: [{ generation: 1, manifest_bytes: 10, created: 1 }, { generation: 2, manifest_bytes: 10, created: 2 }],
    ...extra,
  };
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
const caption = (t) => textOf(t.children.find((c) => c.type === "caption"));
const rowsOf = (t) => all(t).filter((el) => el.type === "tr" && el.parent?.type === "tbody");
const vueErrors = [];
const props = reactive({ chatId: "chat-a", runId: "run-1" });
const root = node("root");
let s;
const instrumented = { ...Inspector, setup(p, context) { s = Inspector.setup(p, context); return s; } };
const app = renderer.createApp({ setup: () => () => h(instrumented, props) });
app.config.errorHandler = (error) => { vueErrors.push(error); };
let mounted = false;
try {
  reply = async () => packet();
  app.mount(root); mounted = true;
  await settle();
  assert.deepEqual(calls, [{ method: "run_get", params: { id: "chat-a", run_id: "run-1" } }]);
  const text = textOf(root);
  assert.ok(text.includes("Context · task run-1") && text.includes("Host records only: nothing here is summarised or judged by a model."));
  assert.deepEqual(tables(root).map(caption), ["Model window at step 7", "By category (estimated tokens)", "Context units (content-free)", "File views · 1 needs a fresh read"]);
  const [window, categories, units, views] = tables(root);
  const windowText = rowsOf(window).map(textOf);
  assert.deepEqual(windowText, [
    "Input budget7,168 of 8,192 tokens; 1,024 reserved for output",
    "Projected input6,900 tokens kept of 9,100 before retirement",
    "Protected floor2,100 tokens",
    "Tool schemas12 tools · about 1,800 tokens",
    "Messages120 of 160 kept",
    "Omitted from the window40 units · 40 messages · about 2,200 tokens",
    "Window countLocal estimate (about 3 bytes per token), not a provider count",
    "StatusOlder context retired to fit the budget",
  ]);
  assert.deepEqual(rowsOf(categories).map((tr) => textOf(tr.children[0])), ["policy", "tool exchange"], "empty categories are omitted and said so");
  assert.ok(text.includes("Categories without content are not listed."));
  assert.equal(rowsOf(units).length, 100);
  assert.ok(text.includes("Showing 100 of 150 context units; the rest are counted in the tables above."));
  assert.ok(textOf(rowsOf(units)[0]).includes("kept · protected") && textOf(rowsOf(units)[1]).includes("omittedretired for budget"));
  assert.ok(text.includes("Not verified by this manifest: source freshness, source truth, permissions, unknown."));
  assert.deepEqual(rowsOf(views).map((tr) => [textOf(tr.children[0]), textOf(tr.children[1]), textOf(tr.children[2])]), [["src/a.ts", "changed", "draft"], [hostile + ".md", "current", "disk"]]);
  assert.ok(text.includes("1 earlier view of a listed file is not listed."));
  assert.ok(text.includes("Statuses are as of the last host check and are not re-checked here; /context re-checks them."));
  assert.ok(text.includes("Context archives: 2 (latest generation 2)") && text.includes("Handoff snapshot: saved at"));
  assert.ok(!text.includes("never shown"), "the run's goal text is not part of this view");
  for (const table of tables(root)) {
    const rows = rowsOf(table);
    assert.deepEqual(rows.map((tr) => tr.props.tabindex), rows.map((_, i) => (i ? -1 : 0)));
    const body = { children: [] };
    for (const tabIndex of [0, -1]) body.children.push({ tagName: "TR", parentElement: body, tabIndex, focused: 0, focus() { this.focused++; } });
    rows[0].props.onKeydown({ key: "ArrowDown", target: body.children[0], preventDefault() {} });
    assert.equal(body.children[1].focused, 1, caption(table) + " rows are keyboard-navigable");
  }

  // Exceeded projections, unknown counters and a missing manifest are stated, not guessed.
  reply = async () => packet({ context: { step: 2, manifest: manifest({ status: "exceeded", dispatchable: false, estimation: { window_method: "provider<b>" }, units_total: 300, units: manifest().units.slice(0, 3) }) } });
  await button("Refresh").props.onClick(); await settle();
  assert.equal(calls.length, 2);
  assert.ok(textOf(root).includes("StatusExceeded the budget: not sent to the model"));
  assert.ok(textOf(root).includes("Window countHost method: provider<b>"));
  assert.ok(textOf(root).includes("Showing 3 of 300 context units"));
  reply = async () => packet({ context: null, sources: [], compactions: [], handoff: null });
  await s.load(); await settle();
  assert.ok(textOf(root).includes("No provider context manifest has been recorded for this task yet."));
  assert.ok(textOf(root).includes("This task has no tracked file views.") && textOf(root).includes("Context archives: 0 · Handoff snapshot: none"));
  assert.equal(tables(root).length, 0);

  // Replies for another task are refused; a late reply after switching tasks is ignored.
  reply = async () => packet({ id: "run-other" });
  await s.load(); await settle();
  assert.ok(all(root).some((el) => el.props.role === "alert" && textOf(el) === "The reply belongs to a different task."));
  assert.equal(s.packet.value, null);
  let release;
  reply = () => new Promise((resolve) => (release = resolve));
  const late = s.load(); await settle();
  const first = release;
  props.runId = "run-2"; await settle();
  assert.deepEqual(calls.at(-1).params, { id: "chat-a", run_id: "run-2" });
  const second = release;
  second(packet({ id: "run-2", context: { step: 3, manifest: manifest() } }));
  first(packet());
  await late; await settle();
  assert.equal(s.packet.value.id, "run-2");
  assert.ok(textOf(root).includes("Context · task run-2"));
  reply = async () => { throw new Error("Run not found in this conversation."); };
  await s.load(); await settle();
  assert.ok(textOf(root).includes("Run not found in this conversation."));

  const seeded = { ...Inspector, setup(p, context) { const b = Inspector.setup(p, context); b.packet.value = packet(); return b; } };
  const html = await renderToString(createSSRApp(seeded, { chatId: "chat-a", runId: "run-1" }));
  assert.ok(html.includes("&lt;img src=x onerror=host()&gt;.md") && !/<(img|script|iframe)\b/.test(html));
  assert.ok(html.includes('<th scope="col">Path</th>') && html.includes('<th scope="row">Input budget</th>'));
  assert.deepEqual(vueErrors, []);
  console.log("Context inspector fixtures passed: run_get only, model-window and category tables, content-free units with bounded rows, newest file view per path with stale first, explicit omissions and not-verified list, exceeded and missing manifests, task scoping and late replies, keyboard rows and escaping.");
} finally {
  if (mounted) app.unmount();
}
function button(label) {
  const found = all(root).find((el) => el.type === "button" && textOf(el).trim() === label);
  assert.ok(found, "Missing button " + label);
  return found;
}
