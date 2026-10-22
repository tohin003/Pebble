import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, defineComponent, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Compile only checked-in sources; chat and goal text are data and are never evaluated.
const require = createRequire(import.meta.url);
const calls = [];
let reply = async () => [];
const bridge = { native: true, async rpc(method, params = {}) { calls.push({ method, params: structuredClone(params) }); return reply(method, params); } };
const icon = defineComponent({ props: { size: Number, weight: String }, setup: () => () => h("i") });
const icons = new Proxy({ __esModule: true }, { get: (target, name) => (name in target ? target[name] : icon), has: () => true });
const ArtifactStub = defineComponent({ props: ["embedded", "artifacts", "selected"], setup: () => () => h("div", "artifact panel") });
function evaluate(code, imports = {}) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(
    (name) => (name === "../bridge" ? bridge : imports[name] || require(name)), module, module.exports);
  return module.exports;
}
function compile(filename, imports = {}) {
  const source = readFileSync(new URL(filename, import.meta.url), "utf8");
  const descriptor = parse(source, { filename }).descriptor;
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  const component = evaluate(script.content, imports).default;
  component.render = evaluate(template.code, imports).render;
  return { component, source };
}
const { component: Summary, source } = compile("./PetRunSummary.vue");
const { component: Sidebar, source: sidebarSource } = compile("./CompanionSidebar.vue", { "@phosphor-icons/vue": icons, "./ArtifactPanel.vue": { __esModule: true, default: ArtifactStub } });

// F6: the pet summary nests no panel and reaches only goal_list, a planned pet route (§8 patch 1).
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source));
assert.deepEqual([...source.matchAll(/from "([^"]+)"/g)].map((m) => m[1]).sort(), ["../bridge", "../types", "vue"]);
assert.deepEqual([...source.matchAll(/\brpc(?:<[^>]*>)?\(\s*([^,)]*)/g)].map((m) => m[1].trim()), ['"goal_list"']);
assert.ok(!/ExecutionOutput|AttemptStream|ReconcilePanel|AgentPanel|process_|stream_read|agent_|run_readback|question_answer|run_resume|approve/.test(source));
// The design study is a maintainer's record, not published (Phase 26): where it exists, it must name the agreed patch.
const studyUrl = new URL("../../docs/HARNESS_PHASE17_DESIGN_STUDY.md", import.meta.url);
if (existsSync(studyUrl)) {
  const study = readFileSync(studyUrl, "utf8");
  assert.ok(/main\.rs:39`\.\*\* Add the read-only .*`goal_list`/.test(study), "goal_list is in the agreed pet allowlist patch");
}
const runPanel = readFileSync(new URL("./RunPanel.vue", import.meta.url), "utf8");
for (const nested of ["ExecutionOutput", "AttemptStream", "ReconcilePanel", "AgentPanel"]) assert.ok(runPanel.includes(nested), "RunPanel keeps the full main-window panels");

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
const vueErrors = [];

const hostile = "<img src=x onerror=host()>";
const chat = reactive({
  id: "chat-a", title: "Fixture", workspace: null, messages: [], actions: [],
  runs: [{ id: "run-1", state: "running", error: null }],
  questions: [{ id: "q1", run_id: "run-1", revision: 1, required: true, status: "pending", items: [{ id: "i", title: "Which folder? " + hostile, options: [{ id: "a", label: "A" }], allow_free_text: true }] }],
  plan: { run_id: "run-1", revision: 2, steps: [{ id: "a", title: "Read", status: "completed", evidence_ids: ["1:r"] }, { id: "b", title: "Edit " + hostile, status: "in_progress", evidence_ids: [] }, { id: "c", title: "Test", status: "pending", evidence_ids: [] }] },
});
const goalList = [
  { id: "old", title: "Old", state: "done", revision: 3, progress: { criteria_met: 1, criteria_total: 1 }, blockers: 0, blocker: null },
  { id: "g1", title: hostile, state: "blocked", revision: 4, progress: { criteria_met: 2, criteria_total: 5 }, blockers: 1, blocker: { kind: "question", label: "Needs your answer" } },
];
reply = async (method) => { assert.equal(method, "goal_list"); return structuredClone(goalList); };
const props = reactive({ chat, refreshKey: 0 });
const root = node("root"), emitted = [];
let s;
const instrumented = { ...Summary, setup(p, context) { s = Summary.setup(p, { ...context, emit: (...args) => emitted.push(args) }); return s; } };
const app = renderer.createApp({ setup: () => () => h(instrumented, props) });
app.config.errorHandler = (error) => { vueErrors.push(error); };
app.mount(root);
await settle();
assert.deepEqual(calls, [{ method: "goal_list", params: { chat_id: "chat-a", limit: 5 } }]);
let text = textOf(root);
assert.ok(text.includes("Goal · 2/5 criteria · blocked: needs your answer → Open"));
assert.ok(text.includes("Task: running") && text.includes("Plan · 1/3 steps · now: Edit " + hostile));
assert.ok(text.includes("Pebble needs your answer.") && text.includes("Answer in the workspace"));
// Phase 24 (V1-111, m24c merge): one name for delegated work, "agents" (it read "helpers"); same check, new word.
assert.ok(text.includes("Output, agents and reviews are in the workspace."));
assert.ok(!text.includes("Which folder?"), "question text and forms stay in the workspace");
assert.equal(all(root).filter((el) => ["form", "input", "textarea", "select", "fieldset"].includes(el.type)).length, 0, "read-only: no form controls");
// Phase 19 §2.6: StatusLine in the pet header is the one status region, so the summary announces nothing.
assert.equal(all(root).filter((el) => el.props.role === "status").length, 0, "no second status region beside StatusLine");
assert.ok(!all(root).some((el) => el.props["aria-live"]));
assert.deepEqual(all(root).filter((el) => el.props.class === "pet-summary-task").map(textOf), ["Task: running"]);
const buttons = all(root).filter((el) => el.type === "button");
assert.deepEqual(buttons.map((b) => textOf(b).trim()), ["Goal · 2/5 criteria · blocked: needs your answer → Open", "Answer in the workspace"]);
for (const b of buttons) b.props.onClick();
assert.deepEqual(emitted, [["workspace"], ["workspace"]], "every action opens the workspace; none changes a record");

// Workspace-only states say where to go; unknown states are bounded.
chat.questions = [];
for (const [state, line] of [["needs_reconciliation", "An outcome is uncertain."], ["budget_exhausted", "Resume it with new limits in the workspace."], ["waiting_approval", "An action needs your review in the workspace."]]) {
  chat.runs = [{ id: "run-1", state, error: state === "budget_exhausted" ? "Budget reached at 64 steps." : null }];
  await settle();
  text = textOf(root);
  assert.ok(text.includes(line), state);
}
assert.ok(text.includes("Task: waiting for your review"));
chat.runs = [{ id: "run-1", state: "budget_exhausted", error: hostile }];
await settle();
assert.ok(textOf(root).includes(hostile), "run errors stay literal text");
chat.runs = [{ id: "run-1", state: "constructor", error: null }];
await settle();
assert.ok(textOf(root).includes("Task: unknown") && !textOf(root).includes("function"));

// A refused goal_list (pet allowlist not yet patched) hides only the chip.
reply = async () => { throw new Error("Open the workspace to manage connections or review actions."); };
props.refreshKey = 1; await settle();
assert.equal(s.goal.value, null);
assert.ok(!textOf(root).includes("Goal ·") && textOf(root).includes("Task: unknown"));
assert.ok(!all(root).some((el) => el.props.role === "alert"));
// A late reply for the previous conversation is ignored.
let release;
reply = () => new Promise((resolve) => (release = resolve));
props.refreshKey = 2; await settle();
const stale = release;
chat.id = "chat-b"; await settle();
const fresh = release;
fresh([]);
await settle();
stale(structuredClone(goalList));
await settle();
assert.equal(s.goal.value, null);
assert.deepEqual(calls.at(-1).params, { chat_id: "chat-b", limit: 5 });
// Nothing to summarise renders nothing.
chat.runs = []; chat.plan = null; await settle();
assert.equal(textOf(root), "");
app.unmount();
const html = await renderToString(createSSRApp(Summary, { chat: { ...chat, id: "chat-a", runs: [{ id: "run-1", state: "failed", error: hostile }], questions: [] } }));
assert.ok(html.includes("&lt;img src=x onerror=host()&gt;") && !/<(img|script|iframe)\b/.test(html));

// F9 in the companion sidebar: progress labels are not announced; only the task status is.
assert.ok(!sidebarSource.includes("aria-live"), "no live region announces every progress label");
const activity = Array.from({ length: 3 }, (_, i) => ({ chat_id: "chat-a", label: "Reading " + hostile + i, kind: i ? "working" : "done", tool: "Read", at: 1700000000000 + i }));
const sidebarProps = { chats: [{ ...chat, id: "chat-a" }], chat: { ...chat, id: "chat-a", actions: [] }, active: "chat-a", activity, tab: "activity", selectedView: "" };
const sidebar = await renderToString(createSSRApp(Sidebar, sidebarProps));
assert.ok(!sidebar.includes("aria-live"));
assert.ok(/<div class="companion-task-status" role="status"><i class="running"><\/i>Task in progress<\/div>/.test(sidebar));
assert.ok(sidebar.includes('aria-label="Task activity, newest first"'));
assert.ok(sidebar.includes("Reading &lt;img src=x onerror=host()&gt;2") && !/<img\b/.test(sidebar));
assert.ok(sidebar.indexOf("onerror=host()&gt;2") < sidebar.indexOf("onerror=host()&gt;0"), "newest first");
const idle = await renderToString(createSSRApp(Sidebar, { ...sidebarProps, active: null }));
assert.ok(idle.includes('role="status"><i class=""></i>Task history</div>'));
assert.deepEqual(vueErrors, []);
console.log("Pet run summary fixtures passed: read-only summary with no nested panels or forms, goal_list only, goal chip and plan line, workspace deep links for answers and workspace-only states, refused route and stale replies, escaping; sidebar announces task status only (F9).");
