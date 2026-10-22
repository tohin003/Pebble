import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, defineComponent, h, nextTick, reactive } from "vue";
import { transform } from "sucrase";

// Generated UI has no native authority (handoff §6.8). This fixture checks the
// renderers' sources and drives the real ArtifactPanel and CompanionSidebar with
// a recording bridge: every request is listed, and generated data never adds one.
const require = createRequire(import.meta.url);
const read = (file) => readFileSync(new URL(file, import.meta.url), "utf8");
const renderers = ["./QuizRenderer.vue", "./DashboardRenderer.vue", "./PrototypeRenderer.vue"];
const generatedSurfaces = [...renderers, "./NativeArtifactRenderer.vue"];
const templateOf = (source) => source.slice(source.indexOf("<template>"), source.lastIndexOf("</template>"));
const scriptOf = (source) => source.slice(source.indexOf("<script"), source.indexOf("</script>"));
const rpcNames = (source) => [...new Set([...source.matchAll(/\brpc(?:<[^>]*>)?\(\s*["']([^"']+)["']/g)].map((m) => m[1]))].sort();

// 1. Sources: no markup injection, code, frames, links, network or storage in any renderer.
for (const file of renderers) {
  const source = read(file);
  assert.ok(!/v-html|innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new Function|<iframe|srcdoc|<a\b|<form\b|<script\b(?! setup)|<object\b|<embed\b|window\.open|postMessage|__TAURI__|\binvoke\(|\bfetch\(|XMLHttpRequest|WebSocket|localStorage|sessionStorage|\bimport\(/.test(source.replace(/<script setup lang="ts">/, "")), file);
  assert.ok(!/approve|reject|question_answer|artifact_submit|run_resume|settings_set|permission/i.test(scriptOf(source)), file + " names no approval or settings route");
}
for (const file of generatedSurfaces) {
  const markup = templateOf(read(file));
  const bound = [...markup.matchAll(/(?:\s:|\sv-bind:)([A-Za-z-]+)=/g)].map((m) => m[1]);
  assert.ok(bound.length > 5 && bound.includes("class"), file + " bindings were found");
  const exempt = file === "./NativeArtifactRenderer.vue" ? ["style"] : [];
  const forbidden = ["href", "src", "style", "srcdoc", "action", "formaction", "innerHTML", "textContent", "is"].filter((name) => !exempt.includes(name));
  assert.deepEqual(bound.filter((name) => forbidden.includes(name)), [], file);
  assert.ok(!/v-bind="|v-on="|\s[:@]\[/.test(markup), file + " has no spread or dynamic bindings");
  for (const [, expression] of markup.matchAll(/:class="([^"]+)"/g))
    assert.ok(!/artifact|\.data\b|theme|label|title|text/.test(expression), file + " class from data: " + expression);
}
// The native renderer's only style binding is a legend swatch from its fixed colour table.
assert.deepEqual([...templateOf(read("./NativeArtifactRenderer.vue")).matchAll(/:style="([^"]+)"/g)].map((m) => m[1]), ["{ background: series.color }"]);
assert.ok(/const colors = \[\s*"#[0-9a-f]{6}"/.test(read("./NativeArtifactRenderer.vue")) && /color: colors\[series\]/.test(read("./NativeArtifactRenderer.vue")));
// RPC routes per file: the quiz uses the five quiz routes, dashboards and demos none, the panel adds only artifact_refresh.
assert.deepEqual(rpcNames(read("./QuizRenderer.vue")), ["quiz_attempt", "quiz_save", "quiz_share", "quiz_start", "quiz_submit"]);
for (const file of ["./DashboardRenderer.vue", "./PrototypeRenderer.vue", "./NativeArtifactRenderer.vue"]) {
  assert.ok(!read(file).includes('"../bridge"'), file + " has no bridge");
  assert.deepEqual(rpcNames(read(file)), [], file);
}
assert.deepEqual(rpcNames(read("./ArtifactPanel.vue")), ["artifact_get", "artifact_history", "artifact_refresh", "artifact_stop", "artifact_submit", "artifact_view_open", "media_artifact", "media_download", "view_stop"]);
// Share is guarded in code, not only hidden: the pet returns before quiz_share.
assert.ok(/async function share\(\) \{[\s\S]{0,120}props\.surface === "pet"/.test(read("./QuizRenderer.vue")));
// Only the panel decides the surface; the sidebar always says "pet".
assert.ok(/<ArtifactPanel[\s\S]*?surface="pet"/.test(read("./CompanionSidebar.vue")));
assert.ok(!read("./CompanionSidebar.vue").includes("aria-live"));

// 2. Behaviour through the real panel and sidebar.
const calls = [];
let host = {};
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: structuredClone(params) });
    if (!Object.hasOwn(host, method)) throw new Error("Unexpected RPC: " + method);
    return structuredClone(await host[method](params));
  },
};
const icon = defineComponent({ props: { size: Number, weight: String }, setup: () => () => h("i") });
const icons = new Proxy({ __esModule: true }, { get: (target, name) => (name in target ? target[name] : icon), has: () => true });
const compiled = {};
function evaluate(code, resolve) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(resolve, module, module.exports);
  return module.exports;
}
function load(filename) {
  if (compiled[filename]) return compiled[filename];
  const descriptor = parse(read(filename), { filename }).descriptor;
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, [], filename);
  const resolve = (name) =>
    name === "../bridge" ? bridge : name === "@phosphor-icons/vue" ? icons : name.endsWith(".vue") ? { __esModule: true, default: load(name) } : require(name);
  const component = evaluate(script.content, resolve).default;
  component.render = evaluate(template.code, resolve).render;
  return (compiled[filename] = component);
}
function node(type, text = "") {
  return { type, text, children: [], props: {}, parent: null, value: "", options: [], addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document };
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
const buttons = (root, label) => all(root).filter((el) => el.type === "button" && textOf(el).trim() === label);
async function settle() { for (let i = 0; i < 12; i++) { await Promise.resolve(); await nextTick(); } }
globalThis.Document = class Document { activeElement = null; };
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
globalThis.setInterval = () => 0;
const vueErrors = [];
const apps = [];
function mount(Component, props) {
  const root = node("root"), emitted = [];
  const instrumented = { ...Component, setup(p, context) { return Component.setup(p, { ...context, emit: (...args) => emitted.push(args) }); } };
  const app = renderer.createApp({ setup: () => () => h(instrumented, props) });
  app.config.errorHandler = (error) => { vueErrors.push(error); };
  app.mount(root);
  apps.push(app);
  return { root, emitted };
}

const hostile = "<img src=x onerror=host()>";
const base = { chat_id: "chat-a", revision: 1, status: "ready", owner_run_id: "run-1", created: 1 };
const quiz = { ...base, id: "quiz", title: "Quiz " + hostile, kind: "quiz", data: { duration_minutes: 5, questions: [{ id: "q1", question: "Approve the payment?", choices: ["Approve", "Reject"] }] } };
const selfCheck = { ...base, id: "check", title: "Self-check", kind: "quiz", data: { duration_minutes: 5, questions: [{ question: "Q", choices: ["A", "B"], answer: 0, explanation: "E" }] } };
const dashboard = { ...base, id: "dash", title: "Dash", kind: "dashboard", data: { panels: [{ id: "runs", title: "Runs", kind: "metric", binding: { source: "runs.recent" } }, { id: "files", title: "Files " + hostile, kind: "table", binding: { source: "files.read", params: { path: "/etc" } } }] }, bindings: [{ panel_id: "runs", source: "runs.recent", as_of: 1760000000, digest: "abcdef012345", data: { value: 4 } }] };
const demo = { ...base, id: "demo", title: "Demo", kind: "prototype", data: { start: "home", screens: [
  { id: "home", title: "Home", components: [{ type: "button", label: "Approve", goto: "done" }, { type: "button", label: "Run tool" }] },
  { id: "done", title: "Done", components: [{ type: "card", title: "Approved", body: "Nothing was approved; this is a mock-up." }] },
] } };
const rows = [quiz, selfCheck, dashboard, demo];
const submitted = { attempt_id: "att-1", artifact_id: "quiz", revision: 1, status: "submitted", deadline: Date.now() / 1000 + 60, answers: { q1: 0 }, result: { score: 1, total: 1, per_question: [{ id: "q1", correct: true, chosen: 0, answer: 0, explanation: "Fixture." }] } };
host = {
  artifact_get: ({ id }) => rows.find((row) => row.id === id),
  artifact_history: ({ id }) => [rows.find((row) => row.id === id)],
  quiz_attempt: () => submitted,
  quiz_share: () => ({ shared: true }),
  artifact_refresh: () => ({ revision: 2 }),
};
const allowed = new Set(["artifact_get", "artifact_history", "quiz_attempt", "quiz_share", "artifact_refresh"]);

// Main window: the quiz offers Share; the click is the only way quiz_share is called.
const Panel = load("./ArtifactPanel.vue");
const mainProps = reactive({ artifacts: rows, selected: "quiz" });
const main = mount(Panel, mainProps);
await settle();
assert.ok(textOf(main.root).includes("1 / 1") && buttons(main.root, "Share result with Pebble").length === 1);
assert.equal(buttons(main.root, "Share in the workspace").length, 0);
assert.ok(!calls.some((call) => call.method === "quiz_share"));
buttons(main.root, "Share result with Pebble")[0].props.onClick();
await settle();
assert.deepEqual(calls.filter((call) => call.method === "quiz_share"), [{ method: "quiz_share", params: { chat_id: "chat-a", attempt_id: "att-1" } }]);
// The generated "Approve" choice text is inert: it is only a label on a host radio button.
assert.ok(!all(main.root).some((el) => ["a", "form", "iframe", "img", "script"].includes(el.type)));
// Prototype: generated buttons only switch screens; no request, no emit.
mainProps.selected = "demo";
await settle();
let before = calls.length;
buttons(main.root, "Approve")[0].props.onClick();
await settle();
assert.ok(textOf(main.root).includes("Nothing was approved; this is a mock-up."));
buttons(main.root, "Run tool")[0]?.props.onClick?.();
await settle();
assert.equal(calls.length, before, "generated controls make no request");
assert.deepEqual(main.emitted, []);
buttons(main.root, "Send feedback")[0].props.onClick();
assert.deepEqual(main.emitted, [["feedback", { chat_id: "chat-a", artifact_id: "demo", revision: 1, screen_id: "done" }]]);
// Dashboard: a binding outside the allowlist is refused in its own panel; no request resolves it.
mainProps.selected = "dash";
await settle();
let text = textOf(main.root);
assert.ok(text.includes("This data source is not available to dashboards.") && text.includes("Runs") && text.includes("4"));
before = calls.length;
buttons(main.root, "Refresh data")[0].props.onClick();
await settle();
assert.deepEqual(calls.slice(before).map((call) => call.method), ["artifact_refresh", "artifact_get", "artifact_history"]);
assert.deepEqual(calls[before].params, { chat_id: "chat-a", id: "dash", revision: 1 });
// A v1 quiz stays a labelled local self-check and calls no quiz route.
mainProps.selected = "check";
await settle();
before = calls.length;
assert.ok(textOf(main.root).includes("Self-check (answers are stored with this view)"));
buttons(main.root, "Start test")[0].props.onClick();
await settle();
assert.ok(calls.slice(before).every((call) => call.method === "artifact_get" || call.method === "artifact_history"));

// Pet: the sidebar's panel never shares; it points to the workspace, and feedback is forwarded.
const Sidebar = load("./CompanionSidebar.vue");
const chat = { id: "chat-a", title: "Chat", workspace: null, messages: [], actions: [], artifacts: rows };
const sidebarProps = reactive({ chats: [chat], chat, active: null, activity: [], tab: "views", selectedView: "" });
const pet = mount(Sidebar, sidebarProps);
await settle();
text = textOf(pet.root);
for (const label of ["Graded quiz", "Self-check quiz", "Dashboard", "App demo"]) assert.ok(text.includes(label), label);
sidebarProps.selectedView = "quiz";
await settle();
assert.ok(textOf(pet.root).includes("1 / 1"));
assert.equal(buttons(pet.root, "Share result with Pebble").length, 0);
const shares = calls.filter((call) => call.method === "quiz_share").length;
buttons(pet.root, "Share in the workspace")[0].props.onClick();
await settle();
assert.deepEqual(pet.emitted, [["workspace"]]);
assert.equal(calls.filter((call) => call.method === "quiz_share").length, shares, "the pet never calls quiz_share");
sidebarProps.selectedView = "demo";
await settle();
buttons(pet.root, "Send feedback")[0].props.onClick();
assert.deepEqual(pet.emitted.at(-1), ["feedback", { chat_id: "chat-a", artifact_id: "demo", revision: 1, screen_id: "home" }]);

// Every request in the session is a read, a quiz route or a refresh: never an approval or answer.
assert.ok(calls.every((call) => allowed.has(call.method)), calls.map((call) => call.method).join(","));
for (const app of apps) app.unmount();
assert.deepEqual(vueErrors, []);
console.log("Generated UI authority fixtures passed: renderer sources carry no markup, code, frames, links, storage or data-bound URL/style/class; RPC routes per file are exact; through the real panel and sidebar, share needs a main-window click, the pet points to the workspace, generated buttons make no request, disallowed bindings are refused per panel, v1 stays a local self-check, and every request is a read, quiz route or refresh.");
