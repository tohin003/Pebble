import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, h, nextTick, reactive } from "vue";
import { transform } from "sucrase";

// Phase 24 (t24-ux-settings): Settings fixtures for V1-001 (sign-in UI), V1-041 (switch, radio and tab states),
// V1-099 (memory search and Load more), V1-100 (memory records read as words), V1-102 (local MCP scope), V1-107
// (Views behind Developer features), V1-113 (the search destination), V1-146 (recall size, Forget while a task runs,
// candidate reasons), V1-161 (Forget names) and V1-163 (Sign out). The component is compiled and mounted on a small
// host renderer; every route is a scripted fake, and no model, network or Keychain is reached.
const require = createRequire(import.meta.url);
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const evaluateTs = (code, resolve) => {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(resolve, module, module.exports);
  return module.exports;
};
const modes = evaluateTs(read("../permissionModes.ts"), (name) => require(name));

class FakeRpcError extends Error {
  constructor(message, code) { super(message); this.code = code; }
  toString() { return this.message; }
}
const calls = [];
const handlers = new Map();
const gates = new Map();
function hold(method) {
  const gate = {};
  gate.promise = new Promise((resolve) => { gate.resolve = resolve; });
  gates.set(method, gate);
  return gate;
}
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: JSON.parse(JSON.stringify(params)) });
    const gate = gates.get(method);
    gates.delete(method);
    if (gate) await gate.promise;
    const handler = handlers.get(method);
    if (!handler) throw new Error("Unexpected RPC " + method);
    return handler(params);
  },
  async selectFile() { return null; },
};
const stub = (name) => ({ name, render: () => null });
const icons = new Proxy({}, { get: (_target, key) => (key === "__esModule" ? true : stub(String(key))) });
const imports = {
  "../bridge": bridge,
  "../permissionModes": modes,
  "./ModsSettings.vue": { __esModule: true, default: stub("ModsSettings") },
  "./MemoryContests.vue": { __esModule: true, default: stub("MemoryContests") },
  "./MemoryArchive.vue": { __esModule: true, default: stub("MemoryArchive") },
  "./ConnectorRouteSettings.vue": { __esModule: true, default: stub("ConnectorRouteSettings") },
  "./StorageSettings.vue": { __esModule: true, default: stub("StorageSettings") },
  "./RemoteSettings.vue": { __esModule: true, default: stub("RemoteSettings") }, // Phase 46: RemoteSettings.test.mjs
  "@phosphor-icons/vue": icons,
};
const filename = "AdvancedSettings.vue";
const source = read("./AdvancedSettings.vue");
const { descriptor, errors } = parse(source, { filename });
assert.deepEqual(errors, []);
const script = compileScript(descriptor, { id: filename });
const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, [], "the template compiles without TypeScript-only expressions");
const resolve = (name) => (name in imports ? imports[name] : require(name));
const Settings = evaluateTs(script.content, resolve).default;
Settings.render = evaluateTs(template.code, resolve).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function/.test(source), "Settings renders literal native content");

// ---- A small host DOM (as the other component fixtures): real hooks, handlers and directives, no HTML parser ----
let focused = null;
function node(type, text = "") {
  return { type, tagName: String(type).toUpperCase(), text, children: [], props: {}, parent: null, value: "", options: [], addEventListener() {}, removeEventListener() {}, getRootNode() { return globalThis.document; }, focus() { focused = this; } };
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
const textOf = (element) => (element.type === "#comment" ? "" : element.text) + element.children.map(textOf).join("");
const all = (element) => [element, ...element.children.flatMap(all)];
const root = node("root");
const timers = new Map();
let timerId = 0;
const store = new Map();
const previous = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot, setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout, window: globalThis.window, navigator: Object.getOwnPropertyDescriptor(globalThis, "navigator") };
globalThis.Document = class Document { activeElement = null; getElementById(id) { return all(root).find((element) => element.props.id === id) || null; } };
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
globalThis.setTimeout = (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay }); return id; };
globalThis.clearTimeout = (id) => timers.delete(id);
globalThis.window = { localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) } };
let clipboard = null;
Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { async writeText(text) { clipboard = text; } } } });
async function settle() { for (let i = 0; i < 12; i++) { await Promise.resolve(); await nextTick(); } }
async function tick() {
  // Run the pending sign-in poll, as the 1.5 s timer would.
  const [[id, timer]] = [...timers];
  assert.equal(timer.delay, 1500, "the status is read every 1.5 s");
  timers.delete(id);
  timer.callback();
  await settle();
}
const nameOf = (element) => element.props["aria-label"] ?? textOf(element).trim();
const buttons = () => all(root).filter((element) => element.type === "button");
const findButton = (label) => buttons().find((element) => nameOf(element) === label);
function button(label) { const found = findButton(label); assert.ok(found, "Missing button " + label + " in: " + buttons().map(nameOf).join(" | ")); return found; }
async function click(label) { const target = button(label); assert.ok(!target.props.disabled, label + " must be enabled"); await target.props.onClick({}); await settle(); }
const count = (method) => calls.filter((call) => call.method === method).length;
const paramsFor = (method) => calls.filter((call) => call.method === method).at(-1)?.params;
const byClass = (name) => all(root).filter((element) => typeof element.props.class === "string" && element.props.class.split(" ").includes(name));

const memoryTypes = [["working", "Working context"], ["episodic", "Past experiences"], ["semantic", "Facts & knowledge"], ["procedural", "Procedures"], ["temporal", "Time-sensitive information"], ["profile", "Your preferences"]];
const state = reactive({
  chats: [{ id: "chat-a", title: "Trip planning", workspace: null, messages: [], actions: [] }],
  model: { state: "ready", configured: true, name: "gpt-6-luna", path: "", context: 32768, budget: 32768, kind: "chatgpt", profile_id: "p1" },
  guard: { available: true, error: null }, active: null,
  settings: { theme: "system", reduced_motion: false, memory_enabled: true, memory_layers: {},
    permissions: { mode: "auto", tools: { files: true, edits: true, tests: true, apps: false, web: true, terminal: true, plugins: true, views: true, screen: false } } },
  memory: { enabled: true, strategy: "v2", types: memoryTypes.map(([id, name], i) => ({ id, name, count: i + 1 })), learning: { status_counts: {}, last_error_type: "OperationalError" } },
  profiles: [{ id: "p1", name: "ada@example.test · ChatGPT", kind: "chatgpt", model: "gpt-6-luna", context: 32768, tools: true, connection_id: "p1", url: "https://chatgpt.com/backend-api/codex" },
    { id: "g1", name: "Local Qwen", kind: "gguf", model: "", context: 4096, tools: true }],
  data_dir: "/tmp/data", platform: "darwin", version: "1.0.0",
});
handlers.set("setting", (params) => { if (params.key === "permissions") state.settings.permissions = params.value; else state.settings[params.key] = params.value; return null; });
handlers.set("plan_credentials", () => ({ names: [] }));
handlers.set("memory_candidates", () => candidates);
handlers.set("extensions", () => []);
handlers.set("open_url", () => null);
handlers.set("chatgpt_cancel", () => null);
let candidates = [];
let bindings;
const instrumented = { ...Settings, setup(properties, context) { bindings = Settings.setup(properties, context); return bindings; } };
const props = reactive({ initialTab: "models" });
const app = renderer.createApp({ setup: () => () => h(instrumented, { state, chatId: "chat-a", initialTab: props.initialTab, onRefresh: () => {} }) });
app.config.errorHandler = (error) => { throw error; };
let mounted = false;

try {
  app.mount(root); mounted = true;
  await settle();

  // ---- V1-100: every memory shape reads as words; no "undefined", no field names, no origin codes ----
  const text = bindings.memoryText, origin = bindings.memoryOrigin;
  const episode = { kind: "episodic", scope: "/work/a", content: JSON.stringify({ user_request: "Fix the parser", run_id: "r1", task_outcome: "failed", broker_evidence: [{ action_id: "a", kind: "tests", status: "failed", result: { exit_code: 1 } }, { action_id: "b", kind: "edit", status: "applied" }], note: "Completion is not proof of factual correctness." }) };
  const outcome = { kind: "episodic", scope: "/work/a", content: JSON.stringify({ claim_class: "observed_outcome", type: "test_failure", observation: "python-unittest failed after an edit to calc.py.", run_id: "r1", observed_at: 1, receipt_ids: ["r1/2"], reuse: "Host-recorded past outcome; not an instruction." }) };
  const lesson = { kind: "episodic", scope: "chat:chat-a", content: JSON.stringify({ claim_class: "host_lesson", type: "test_failure", signature: "test_add", observation: "Failing test test_add was observed in 2 consented runs here.", failing_runs: 2, receipt_ids: [] }) };
  const modelLesson = { kind: "episodic", scope: "/work/a", content: JSON.stringify({ claim_class: "model_lesson_unverified", lesson: "Run the linter before the tests.", receipt_ids: [] }) };
  const bare = { kind: "episodic", scope: "/work/a", content: JSON.stringify({ user_request: "Plan a trip" }) };
  const shown = [episode, outcome, lesson, modelLesson, bare].map(text);
  assert.deepEqual(shown, [
    "Asked: Fix the parser\nResult: failed\nTest run · failed; File edit · done",
    "Seen in this project: python-unittest failed after an edit to calc.py.",
    "Seen in this conversation: Failing test test_add was observed in 2 consented runs here.",
    "Lesson suggested by the model (not verified): Run the linter before the tests.",
    "Asked: Plan a trip\nResult: not recorded\nNo file, command or app actions.",
  ], "the episodic shapes (episode, observed outcome, host lesson, model lesson) read as words");
  for (const line of shown) assert.ok(!/undefined|claim_class|broker_evidence|\{|\}/.test(line), "no undefined or raw fields: " + line);
  // Phase 45 (F228, audit round R9): a task whose steps needed no card says what it did; an uncertain result reads in words
  const quiet = { kind: "episodic", scope: "chat:c", content: JSON.stringify({ user_request: "Draw a box", task_outcome: "needs_reconciliation", broker_evidence: [], tool_steps: { command: 3, file: 1, app_step: 5, bogus: 2 } }) };
  assert.equal(text(quiet), "Asked: Draw a box\nResult: not completed: an app step's result was uncertain\nSteps: 3 commands, 1 file written, 5 steps in app windows.");
  // Phase 45 (F243, F242; audit round R10): approvals keep the Steps line, and refused steps are named apart
  const chess = { kind: "episodic", scope: "chat:c", content: JSON.stringify({ user_request: "Move the pawn", task_outcome: "completed", broker_evidence: [{ action_id: "a", kind: "app_launch", status: "applied" }], tool_steps: { app_step: 2, refused: 5 } }) };
  assert.ok(text(chess).endsWith("\nSteps: 2 steps in app windows, 5 steps refused."), text(chess));
  assert.equal(text(chess).split("\n").length, 4, "asked, result, the approvals, the steps");
  assert.equal(text({ kind: "working", content: JSON.stringify({ current_goal: "Ship it", phase: "waiting_approval", actions: [{ kind: "edit", status: "pending" }] }) }), "Current task: Ship it\nState: waiting approval\nActions: File edit · waiting for you");
  assert.equal(text({ kind: "working", content: JSON.stringify({}) }), "A task in progress");
  assert.equal(text({ kind: "semantic", content: JSON.stringify({ claim_class: "source_report", path: "docs/a.md", quote: "The port is 8080.", file_hash: "h" }) }), "“The port is 8080.”\nFrom docs/a.md");
  assert.equal(text({ kind: "procedural", content: JSON.stringify({ recipe: "python-unittest", recipe_kind: "test", language: "python", argv_digest: "ab" }) }), "Known test recipe: python-unittest (python). It never runs from memory; each run still follows your permissions.");
  assert.ok(!text({ kind: "temporal", content: JSON.stringify({ place: "Lyon" }) }).includes("undefined"), "a partial weather record has no undefined");
  assert.equal(text({ kind: "profile", content: "I prefer metric units." }), "I prefer metric units.");
  assert.equal(text({ kind: "episodic", content: "{broken" }), "{broken", "text that is not JSON is shown as saved");
  const origins = [["direct-user", "profile", null], ["user", "profile", null], ["curated", "procedural", null], ["conversation", "episodic", null], ["auto-learned", "semantic", { evidence_class: "attributed_file_quote" }],
    ["auto-learned", "profile", { evidence_class: "human_preference" }], ["auto-learned", "procedural", {}], ["imported", "semantic", null], ["broker", "working", null], ["mystery-code", "semantic", null]];
  assert.deepEqual(origins.map(([o, kind, proof]) => origin({ origin: o, kind, provenance: proof && JSON.stringify(proof) })), [
    "You asked Pebble to remember this", "Added in Settings", "Built into Pebble", "Learned from a task", "Learned from a file Pebble read",
    "Learned from what you said", "Learned from a test run", "Imported from a memory archive", "Task state", "Saved by Pebble"]);
  assert.equal(origin({ id: "pebble-md", origin: "user", kind: "profile" }), "From PEBBLE.md");

  // ---- V1-001: the sign-in starts visibly, fails inline with the action its code allows, and returns to the list ----
  const status = () => byClass("signin-status");
  const signInButton = () => all(root).find((element) => element.type === "button" && element.props.class === "chatgpt-sign-in");
  let gate = hold("chatgpt_start");
  handlers.set("chatgpt_start", () => { throw new FakeRpcError("Pebble could not reach OpenAI: this Mac seems to be offline. Your current sign-in is unchanged.", "offline"); });
  const starting = signInButton().props.onClick({}); await settle();
  assert.ok(textOf(signInButton()).includes("Opening ChatGPT sign-in…"), "the button says so at once");
  assert.equal(textOf(status()[0]).trim(), "Opening ChatGPT sign-in…", "and the status line, before the service answers");
  assert.ok(signInButton().props.disabled, "a second click cannot start another sign-in");
  gate.resolve(); await starting; await settle();
  let block = status()[0];
  assert.equal(block.parent.children[block.parent.children.indexOf(signInButton()) + 1], block, "the error sits right after the button");
  assert.equal(block.props.role, "alert");
  assert.ok(textOf(block).includes("seems to be offline") && findButton("Try again"));
  assert.equal(bindings.error.value, "", "nothing is shown at the top of the panel");
  assert.equal(byClass("settings-error").length, 0);
  const expected = { offline: "retry", connect: "retry", timeout: "retry", http_408: "retry", http_429: "retry", http_503: "retry", http_500: "retry", invalid_response: "retry",
    tls: "none", http_403: "none", redirect: "none", reconnect_required: "reconnect" };
  for (const [code, action] of Object.entries(expected)) {
    const before = count("chatgpt_start");
    handlers.set("chatgpt_start", () => { throw new FakeRpcError("Message for " + code + ".", code); });
    await signInButton().props.onClick({}); await settle();
    assert.equal(count("chatgpt_start"), before + 1);
    block = status()[0];
    assert.ok(textOf(block).includes("Message for " + code + "."), code + " message inline");
    assert.equal(!!findButton("Try again"), action === "retry", code + ": Try again");
    assert.equal(all(block).some((element) => element.type === "button" && textOf(element).trim() === "Continue with ChatGPT"), action === "reconnect", code + ": Continue with ChatGPT in the error");
  }
  handlers.set("chatgpt_start", () => { throw new FakeRpcError("Message for timeout.", "timeout"); });
  await signInButton().props.onClick({}); await settle();
  handlers.set("chatgpt_start", () => ({ url: "https://auth.openai.com/oauth/authorize?state=s1", state: "waiting" }));
  let statuses = [];
  handlers.set("chatgpt_status", () => { const next = statuses.shift(); if (next instanceof Error) throw next; return next; });
  await click("Try again");
  assert.deepEqual(paramsFor("open_url"), { url: "https://auth.openai.com/oauth/authorize?state=s1" }, "Try again starts a fresh sign-in");
  block = status()[0];
  const link = all(block).find((element) => element.type === "input");
  assert.equal(link.props.value, "https://auth.openai.com/oauth/authorize?state=s1", "the sign-in link is shown");
  await click("Copy link");
  assert.equal(clipboard, "https://auth.openai.com/oauth/authorize?state=s1");
  assert.ok(findButton("Copied"));
  // Two failed reads in a row are tolerated; a good read resets the count; the third failure in a row stops.
  statuses = [new Error("busy"), new Error("busy"), { state: "waiting" }, new Error("busy"), new Error("busy"), new Error("busy")];
  for (let i = 0; i < 5; i++) { await tick(); assert.equal(bindings.signin.phase, "waiting", "poll " + (i + 1) + " keeps waiting"); }
  await tick();
  assert.equal(bindings.signin.phase, "failed");
  assert.equal(timers.size, 0, "the wait stops after three failed reads in a row");
  assert.ok(textOf(status()[0]).includes("lost track of this sign-in") && findButton("Try again"));
  // The browser does not open: the person is told and the link stays available.
  handlers.set("open_url", () => { throw new Error("Open the desktop app"); });
  await click("Try again");
  assert.ok(textOf(status()[0]).includes("Your browser did not open. Copy the sign-in link"));
  handlers.set("open_url", () => null);
  // Success returns to the model list: the provider form stays closed and nothing is fetched.
  bindings.adding.value = true; await settle();
  statuses = [{ state: "validating" }, { state: "connected", profile_id: "p1", email: "ada@example.test" }];
  await tick();
  assert.ok(textOf(status()[0]).includes("Checking your ChatGPT account"));
  await tick();
  assert.equal(bindings.signin.phase, "connected");
  assert.equal(bindings.adding.value, false, "the technical provider form is not opened");
  assert.equal(count("model_discover"), 0);
  assert.ok(textOf(status()[0]).includes("Connected to ChatGPT as ada@example.test"));
  assert.ok(byClass("just-connected").length === 1 && textOf(byClass("just-connected")[0]).includes("ada@example.test · ChatGPT"), "the connected row is marked in the list");
  assert.equal(timers.size, 0);
  // A failed browser step reported by the status (e.g. declined) is inline with Try again.
  statuses = [{ state: "failed", error: "ChatGPT sign-in was declined. Your previous account is unchanged." }];
  await signInButton().props.onClick({}); await settle(); await tick();
  assert.ok(textOf(status()[0]).includes("declined") && findButton("Try again"));
  // Reconnect from a row shows its status right after that row.
  handlers.set("chatgpt_start", () => { throw new FakeRpcError("ChatGPT ended this sign-in (it expired or was revoked).", "reconnect_required"); });
  await click("Reconnect");
  block = status()[0];
  const row = all(root).find((element) => element.type === "article" && textOf(element).includes("ada@example.test · ChatGPT"));
  assert.equal(row.parent.children[row.parent.children.indexOf(row) + 1], block, "a row's sign-in status follows that row");
  assert.deepEqual(paramsFor("chatgpt_start"), { profile_id: "p1" });
  // 1.0.2: each model row names its window once. A GGUF row has no model name, and it used to stand in the window for
  // the name, so "4,096-token window" showed twice.
  const rowLine = (name) => textOf(all(all(root).find((element) => element.type === "article"
    && textOf(element).includes(name))).find((element) => element.type === "small")).replace(/\s+/g, " ").trim();
  assert.equal(rowLine("Local Qwen"), "On this Mac (GGUF file) · 4,096-token window · " + rowLine("Local Qwen").split("window · ")[1]);
  assert.equal(rowLine("Local Qwen").match(/token window/g).length, 1, rowLine("Local Qwen"));
  assert.ok(rowLine("ada@example.test · ChatGPT").startsWith("ChatGPT plan · gpt-6-luna · 32,768-token window · "),
    rowLine("ada@example.test · ChatGPT"));
  // V1-163: Sign out is a secondary button, not an icon button.
  const signOut = button("Sign out");
  assert.equal(signOut.props.class, "secondary-button");
  // Cancel during a wait.
  handlers.set("chatgpt_start", () => ({ url: "https://auth.openai.com/oauth/authorize?state=s2" }));
  await signInButton().props.onClick({}); await settle();
  await click("Cancel sign-in");
  assert.equal(count("chatgpt_cancel") > 0, true);
  assert.equal(timers.size, 0, "cancelling stops the wait");
  assert.equal(status().length, 0);

  // ---- Phase 46 (D166): Fast replies, one radio group on the Models tab when a ChatGPT connection exists ----
  const fastRadios = () => all(root).filter((element) => element.props.role === "radio" && String(element.props.id).startsWith("fast-replies-"));
  assert.deepEqual(fastRadios().map((element) => element.props.id), ["fast-replies-off", "fast-replies-follow_ups", "fast-replies-long_tasks", "fast-replies-always"]);
  assert.deepEqual(fastRadios().map((element) => element.props["aria-checked"]), [false, false, true, false], "long_tasks is the default");
  await fastRadios()[3].props.onClick({}); await settle();
  assert.deepEqual(paramsFor("setting"), { key: "fast_replies", value: "always" });
  assert.deepEqual(fastRadios().map((element) => element.props.tabindex), [-1, -1, -1, 0], "one tab stop, on the choice");
  await fastRadios()[3].props.onKeydown({ key: "ArrowLeft", preventDefault() {} }); await settle();
  assert.equal(state.settings.fast_replies, "long_tasks", "arrow keys move the choice");
  assert.ok(textOf(all(root).find((element) => element.props.id === "fast-replies-note")).includes("twice the standard rate"));

  // ---- V1-041: tabs, the mode radio group and the tool switches expose their state ----
  const tablist = all(root).find((element) => element.props.role === "tablist" && element.props["aria-label"] === "Settings sections");
  const tabButtons = () => all(tablist).filter((element) => element.props.role === "tab");
  // Phase 45 (D129): Privacy & Stealth, Appearance and General have their own tabs (they trailed under every tab).
  // Phase 46 (1.9.29, D214): the iPhone tab (Pebble Remote) follows Privacy & Stealth.
  assert.deepEqual(tabButtons().map((element) => textOf(element).trim()), ["Models", "Permissions", "Memory", "Skills & connectors", "Data & storage", "Privacy & Stealth", "iPhone", "Appearance", "General"], "Views is hidden without Developer features (V1-107)");
  assert.deepEqual(tabButtons().map((element) => element.props["aria-selected"]), [true, false, false, false, false, false, false, false, false]);
  assert.deepEqual(tabButtons().map((element) => element.props.tabindex), [0, -1, -1, -1, -1, -1, -1, -1, -1], "one tab stop");
  const panel = all(root).find((element) => element.props.role === "tabpanel" && element.props.id === "settings-panel");
  assert.equal(panel.props["aria-labelledby"], "settings-tab-models");
  await tabButtons()[0].props.onKeydown({ key: "ArrowRight", preventDefault() {} }); await settle();
  assert.equal(bindings.tab.value, "permissions", "arrow keys move between tabs");
  assert.equal(focused?.props.id, "settings-tab-permissions", "and focus follows");
  const radios = () => all(root).filter((element) => element.props.role === "radio");
  assert.equal(all(root).filter((element) => element.props.role === "radiogroup").length, 1);
  assert.deepEqual(radios().map((element) => element.props["aria-checked"]), [false, true, false]);
  assert.deepEqual(radios().map((element) => all(element).find((child) => child.type === "strong") && textOf(all(element).find((child) => child.type === "strong"))), ["Ask for approval", "Auto in project", "Full access"]);
  for (const [index, id] of ["ask", "auto", "full"].entries()) {
    const words = textOf(radios()[index]);
    assert.ok(words.includes("Runs without asking: " + modes.MODES[id].runs_without_asking) && words.includes("Always asks: " + modes.MODES[id].always_asks), id + " copy from permissionModes");
  }
  await radios()[1].props.onKeydown({ key: "ArrowDown", preventDefault() {} }); await settle();
  assert.equal(paramsFor("setting").value.mode, "full", "arrow keys choose the next mode");
  assert.equal(focused?.props.id, "permission-mode-full");
  assert.deepEqual(radios().map((element) => element.props["aria-checked"]), [false, false, true]);
  const switches = () => all(root).filter((element) => element.props.role === "switch");
  const tools = state.settings.permissions.tools;
  assert.equal(switches().length, Object.keys(tools).length, "every tool permission is a switch");
  assert.deepEqual(switches().map((element) => element.props["aria-checked"]), Object.values(tools));
  await switches()[3].props.onClick({}); await settle();
  assert.equal(state.settings.permissions.tools.apps, true);
  assert.equal(switches()[3].props["aria-checked"], true, "the state follows the setting");
  // V1-113 (D9): the search destination is named under Web access.
  const web = switches().find((element) => textOf(element).includes("Search and read the web"));
  assert.ok(textOf(web).includes("Microsoft Bing") && textOf(web).includes("fetched from their own sites"));

  // ---- Memory: switches, the layer tabs, search and Load more (V1-099), names and idle-only Forget ----
  const rows = (offset, n, prefix = "Fact") => Array.from({ length: n }, (_, i) => ({ id: "m" + (offset + i), kind: "profile", scope: "global", origin: i % 2 ? "direct-user" : "user", chat_id: i === 0 ? "chat-a" : null, updated: 1760000000, content: prefix + " number " + (offset + i) + " is a long enough sentence to clip at forty characters." }));
  handlers.set("memory_list", (params) => {
    if (params.query) return params.query === "nothing" ? [] : rows(0, 2, "Matching");
    return params.offset === 0 ? rows(0, 100) : rows(100, 20);
  });
  candidates = [{ id: "c1", kind: "profile", status: "unsupported", reason: "run_consent_disabled", content: "I like tea", scope: "global" },
    { id: "c2", kind: "profile", status: "pending", reason: "brand_new_code", content: "I like coffee", scope: "global" }];
  await bindings.changeTab("memory"); await settle();
  assert.deepEqual(paramsFor("memory_list"), { kind: "profile", offset: 0, limit: 100 }, "the first page");
  assert.equal(switches()[0].props["aria-checked"], true, "Remember useful context is a switch with its state");
  const layers = all(root).find((element) => element.props["aria-label"] === "Memory layers");
  assert.equal(layers.props.role, "tablist");
  assert.deepEqual(all(layers).filter((element) => element.props.role === "tab").map((element) => element.props["aria-selected"]), [false, false, false, false, false, true]);
  const forgetButtons = () => buttons().filter((element) => /^Forget this memory: /.test(nameOf(element)));
  assert.equal(forgetButtons().length, 100);
  assert.equal(nameOf(forgetButtons()[0]), "Forget this memory: Fact number 0 is a long enough sentence …", "V1-161: named by the first 40 characters");
  const page = textOf(root);
  assert.ok(page.includes("Added in Settings · Personal · From: Trip planning") && page.includes("You asked Pebble to remember this · Personal"));
  assert.ok(!/direct-user|run_consent_disabled|brand_new_code|OperationalError|unsupported ·/.test(page), "no codes on the memory page");
  assert.ok(page.includes("Not saved: memory was off when that task started") && page.includes("Waiting for evidence: Pebble could not confirm it from its evidence"), "V1-146: reasons in words");
  assert.ok(page.includes("up to about 1,966 characters of memory"), "V1-146: the real recall size for a 32K window");
  await click("Load more");
  assert.deepEqual(paramsFor("memory_list"), { kind: "profile", offset: 100, limit: 100 });
  assert.equal(forgetButtons().length, 120);
  assert.ok(!findButton("Load more"), "a short page is the last one");
  bindings.memoryQuery.value = "match"; await settle();
  const search = all(root).find((element) => element.type === "form" && element.props.role === "search");
  await search.props.onSubmit({ preventDefault() {} }); await settle();
  assert.deepEqual(paramsFor("memory_list"), { kind: "profile", query: "match", offset: 0, limit: 100 });
  assert.equal(forgetButtons().length, 2);
  bindings.memoryQuery.value = "nothing"; await settle();
  await search.props.onSubmit({ preventDefault() {} }); await settle();
  assert.ok(textOf(root).includes("No memories in this layer match “nothing”."));
  await click("Clear search");
  assert.deepEqual(paramsFor("memory_list"), { kind: "profile", offset: 0, limit: 100 });
  state.active = "chat-a"; await settle();
  assert.ok(forgetButtons().every((element) => element.props.disabled && element.props.title === "Available when the task is idle"), "V1-146: Forget waits for an idle task");
  await bindings.forget("m1");
  assert.equal(count("memory_delete"), 0);
  state.active = null; state.model.budget = 4096; state.model.context = 4096; await settle();
  assert.ok(textOf(root).includes("up to about 600 characters"), "600 on a 4K model");

  // ---- V1-102: a refused local server explains what is supported ----
  await bindings.changeTab("extensions"); await settle();
  assert.ok(textOf(root).includes("SKILLS AND CONNECTORS") && !textOf(root).includes("not supported yet"));
  handlers.set("extension_add", () => { throw new Error("MCP launch grants require regular absolute files without symlinks, under 100 MB."); });
  bindings.extensionType.value = "plugin"; bindings.extensionName.value = "files"; bindings.extensionText.value = '{"tools":["read"],"transport":"stdio"}'; await settle();
  assert.ok(textOf(root).includes("Servers started through npx, uvx or Node are not supported yet"), "the connector form says what local servers can be");
  await bindings.installExtension(); await settle();
  assert.ok(bindings.error.value.startsWith("Error: Local servers must be one self-contained program under 100 MB") && bindings.error.value.includes("(Pebble said: MCP launch grants require"), bindings.error.value);

  // ---- Phase 24 (parent): a failed model fetch is shown by its button, once; editing the form clears it ----
  await bindings.changeTab("models"); bindings.adding.value = true; await settle();
  Object.assign(bindings.form.value, { kind: "anthropic", url: "https://api.anthropic.com/v1", api_key: "test-key" }); await settle();
  const discoverAlerts = () => all(root).filter((element) => element.props.role === "alert" && /\bdiscover-error\b/.test(element.props.class || ""));
  handlers.set("model_discover", () => { throw new Error("The provider answered HTTP 401: the API key was not accepted."); });
  await click("Fetch available models");
  assert.equal(discoverAlerts().length, 1, "the fetch failure is shown by its button");
  assert.ok(textOf(discoverAlerts()[0]).includes("HTTP 401"));
  assert.equal(bindings.error.value, "", "and not repeated at the top of Settings");
  bindings.form.value.url = "https://api.anthropic.com/v1/messages"; await settle();
  assert.equal(discoverAlerts().length, 0, "editing the URL clears it");
  handlers.set("model_discover", () => [{ id: "claude-opus-5-5", name: "Claude Opus 5.5", context: 200000 }]);
  await click("Fetch available models");
  assert.equal(discoverAlerts().length, 0);
  assert.deepEqual(bindings.fetched.value.map((model) => model.id), ["claude-opus-5-5"]);
  assert.equal(paramsFor("model_discover").url, "https://api.anthropic.com/v1/messages", "the backend reduces a pasted request URL (test_provider_endpoint_24)");
  bindings.adding.value = false; await bindings.changeTab("extensions"); await settle();

  // ---- V1-107 (D7): Views (mods) only with Developer features, remembered in this window ----
  const developer = switches().find((element) => textOf(element).includes("Developer features"));
  assert.equal(developer.props["aria-checked"], false);
  await developer.props.onClick({}); await settle();
  assert.equal(store.get("pebble.settings.developer"), "on");
  assert.deepEqual(tabButtons().map((element) => textOf(element).trim()), ["Models", "Permissions", "Memory", "Skills & connectors", "Views", "Data & storage", "Privacy & Stealth", "iPhone", "Appearance", "General"]);  // Phase 46: iPhone
  await bindings.changeTab("mods"); await settle();
  await bindings.toggleDeveloper(); await settle();
  assert.equal(bindings.tab.value, "extensions", "turning it off leaves the Views tab");
  assert.ok(!store.has("pebble.settings.developer"));

  // Phase 45 (F063): Privacy & Stealth, Appearance and General show nothing of Skills & connectors (App.vue fills them).
  for (const t of ["privacy", "appearance", "general"]) {
    await bindings.changeTab(t); await settle();
    assert.ok(!textOf(root).includes("SKILLS AND CONNECTORS") && !textOf(root).includes("PEBBLE'S TOOLBOX"), t + " is its own page");
  }
  await bindings.changeTab("extensions"); await settle();
  assert.ok(textOf(root).includes("SKILLS AND CONNECTORS"), "and Skills & connectors still has its own");

  app.unmount(); mounted = false;
  assert.equal(timers.size, 0, "no timer survives Settings");
  console.log("AdvancedSettings fixtures passed: memory records as words for every episodic shape and origins in words; sign-in shown at once, failures inline by the button per code (Try again, Continue with ChatGPT or message only), the link with Copy, two failed reads tolerated and the third stopping, a return to the model list, row status after its row, Sign out as a secondary button; tab, radio-group and switch states with arrow keys; memory search and Load more; idle-only Forget named by its words; recall size; local MCP scope; Views behind Developer features. IPC is simulated.");
} finally {
  if (mounted) app.unmount();
  globalThis.document = previous.document;
  globalThis.Document = previous.Document;
  globalThis.ShadowRoot = previous.ShadowRoot;
  globalThis.setTimeout = previous.setTimeout;
  globalThis.clearTimeout = previous.clearTimeout;
  globalThis.window = previous.window;
  if (previous.navigator) Object.defineProperty(globalThis, "navigator", previous.navigator);
  else delete globalThis.navigator;
}
