import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, defineComponent, h, nextTick } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Phase 24 (t24-ux-shell) behaviour fixtures. The real App.vue, Pet.vue and their Phase 24 components run against a
// small fake DOM and a recording bridge: no service, no network, no model. Child panels the window only places are
// stubs that record what they were given. Each block names the V1 item it covers; the P1 blocks fail on 0.6.1
// (0.6.1 had none of these controls, names or routes).
const require = createRequire(import.meta.url);
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const vue = require("vue");

// ---------- a fake DOM ----------
let focused = null;
function node(type, text = "") {
  return {
    type, text, children: [], props: {}, parent: null, value: "", dataset: {}, scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    events: [], options: [],
    addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document,
    focus() { focused = this; }, select() {}, scrollIntoView() {},
    dispatchEvent(event) { this.events.push(event); return true; },
  };
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
const all = (el) => [el, ...el.children.flatMap(all)];
// Elements are separated by a space, as a person reads them (squash() then normalises the spacing).
const textOf = (el) => (el.type === "#comment" ? "" : el.type === "#text" ? el.text : [el.text, ...el.children.map(textOf)].join(" "));
const squash = (text) => text.replace(/\s+/g, " ").trim();
const nameOf = (el) => el.props["aria-label"] ?? squash(textOf(el));
const buttons = (root, name) => all(root).filter((el) => el.type === "button" && nameOf(el) === name);
const button = (root, name) => {
  const found = buttons(root, name);
  assert.ok(found.length, "missing button " + JSON.stringify(name) + " in: " + all(root).filter((el) => el.type === "button").map(nameOf).join(" | "));
  return found[0];
};
const roleOf = (root, role) => all(root).filter((el) => el.props.role === role);
async function settle() { for (let i = 0; i < 25; i++) { await Promise.resolve(); await nextTick(); } }

// Globals the windows read. Timers are recorded and never fire (no test waits on one).
const store = new Map();
const localStorageStub = {
  get length() { return store.size; },
  key: (i) => [...store.keys()][i] ?? null,
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => void store.set(k, String(v)),
  removeItem: (k) => void store.delete(k),
};
const documentListeners = new Map();
const fakeTimers = { setTimeout: () => 0, clearTimeout: () => {} };
globalThis.window = { localStorage: localStorageStub, innerWidth: 1300, devicePixelRatio: 2, addEventListener() {}, removeEventListener() {}, ...fakeTimers };
globalThis.localStorage = localStorageStub;
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
// Vue's v-model checks `instanceof Document` / `ShadowRoot` on getRootNode().
globalThis.Document = class Document {
  documentElement = { dataset: {}, classList: { toggle() {}, add() {} } };
  hidden = false;
  get activeElement() { return focused; }
  addEventListener(type, fn) { (documentListeners.get(type) || documentListeners.set(type, new Set()).get(type)).add(fn); }
  removeEventListener(type, fn) { documentListeners.get(type)?.delete(fn); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
};
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
const real = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout, setInterval: globalThis.setInterval, clearInterval: globalThis.clearInterval };
globalThis.setTimeout = fakeTimers.setTimeout;
globalThis.clearTimeout = fakeTimers.clearTimeout;
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
process.on("exit", () => Object.assign(globalThis, real));

// ---------- the recording bridge ----------
class RpcError extends Error {
  constructor(message, code) { super(message); this.code = code; }
}
const bridge = {
  native: true, RpcError, calls: [], pastes: [], picks: [], handlers: {}, receive: null,
  async rpc(method, params = {}) {
    bridge.calls.push([method, structuredClone(params)]);
    const handler = bridge.handlers[method];
    if (!handler) throw new Error("Unknown request.");
    return structuredClone(await handler(params));
  },
  async attachBytes(id, name, blob) {
    bridge.pastes.push([id, name, blob.size, blob.type]);
    return { id: "source-" + bridge.pastes.length, name: name || "Pasted image.png", kind: blob.type.startsWith("image/") ? "image" : "file", characters: 0 };
  },
  async subscribe(callback) { bridge.receive = callback; return () => {}; },
  async subscribeDrops() { return () => {}; },
  async selectFile(...args) { bridge.picks.push(args); return null; },
  async selectAttachments() { return []; },
  async setCompact() {},
  async openExternal() {},
};
const called = (method) => bridge.calls.filter(([m]) => m === method).map(([, params]) => params);
const invokes = [];

// ---------- loading components ----------
const icon = defineComponent({ props: { size: Number, weight: String, mirrored: Boolean }, setup: () => () => h("i") });
const icons = new Proxy({ __esModule: true }, { get: (target, name) => (name in target ? target[name] : icon), has: () => true });
const registry = {};
function stub(file) {
  const label = file.split("/").pop();
  return defineComponent({
    name: label.replace(/\.vue$/, ""), inheritAttrs: false,
    setup(_, { attrs, slots }) {
      (registry[label] ||= []).push(attrs);
      return () => h("div", { "data-stub": label }, slots.default?.());
    },
  });
}
const drawerModule = {
  __esModule: true,
  readDrawer: () => ({ open: false, tab: "task", width: 360 }),
  writeDrawer: () => true,
  default: defineComponent({
    name: "DetailsDrawer", inheritAttrs: false,
    setup(_, { attrs, slots, expose }) {
      expose({ focus() {} });
      (registry["DetailsDrawer.vue"] ||= []).push(attrs);
      return () => h("aside", { "data-stub": "DetailsDrawer" }, (slots[attrs.tab] || slots.empty)?.());
    },
  }),
};
const vueShim = { ...vue, Transition: (_, { slots }) => slots.default?.() };
const tsCache = new Map();
function loadTs(url) {
  if (tsCache.has(url.href)) return tsCache.get(url.href).exports;
  const module = { exports: {} };
  tsCache.set(url.href, module);
  new Function("require", "module", "exports", transform(readFileSync(url, "utf8"), { transforms: ["typescript", "imports"] }).code)(
    (name) => (name === "vue" ? vueShim : name.startsWith(".") ? loadTs(new URL(name.endsWith(".ts") ? name : name + ".ts", url)) : require(name)),
    module, module.exports);
  return module.exports;
}
function evaluate(code, resolve) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(resolve, module, module.exports);
  return module.exports;
}
function loadSfc(path, overrides = {}) {
  const filename = path;
  const source = read(path);
  const { descriptor } = parse(source, { filename });
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename,
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] } });
  assert.deepEqual(template.errors, [], path);
  const base = new URL(path, import.meta.url);
  const resolve = (name) => {
    if (Object.hasOwn(overrides, name)) return overrides[name];
    if (name === "vue") return vueShim;
    if (name === "@phosphor-icons/vue") return icons;
    if (name === "@tauri-apps/api/core") return { invoke: async (command, args) => void invokes.push([command, args]) };
    if (name === "@tauri-apps/api/event") return { listen: async () => () => {} };
    if (name === "@tauri-apps/api/window") return { getCurrentWindow: () => ({ onResized: async () => () => {}, startDragging: async () => {} }) };
    if (/bridge$/.test(name)) return bridge;
    if (name.endsWith(".vue")) return { __esModule: true, default: stub(name) };
    if (name.startsWith(".")) return loadTs(new URL(name.endsWith(".ts") ? name : name + ".ts", base));
    return require(name);
  };
  const component = evaluate(script.content, resolve).default;
  component.render = evaluate(template.code, resolve).render;
  return component;
}
const errors = [];
function mount(component, props = {}) {
  const root = node("root");
  const app = renderer.createApp({ setup: () => () => h(component, props) });
  app.config.errorHandler = (error) => errors.push(error);
  app.config.warnHandler = () => {};
  app.mount(root);
  return { root, app };
}

// ---------- fake host data ----------
const CHAT = "c".repeat(32), OTHER = "d".repeat(32);
function hostState(model = {}) {
  return {
    chats: [{ id: CHAT, title: "Trip plan", workspace: null }, { id: OTHER, title: "Recipes", workspace: null }],
    model: { state: "unloaded", configured: false, name: "Choose a local model", path: "", context: 4096, kind: "gguf", profile_id: "default-local", ...model },
    profiles: [{ id: "default-local", name: "On this Mac", kind: "gguf", context: 4096, tools: true }, { id: "chatgpt", name: "ChatGPT", kind: "chatgpt", model: "gpt-6-luna", context: 272000, tools: true, local: false }],
    guard: { available: true, error: null }, active: null, active_run: null, runs: [],
    settings: { theme: "system", reduced_motion: false, memory_enabled: true, permissions: { mode: "ask", tools: { files: true, edits: true, tests: true, apps: true, web: true, terminal: true, plugins: true, views: true, screen: false } },
      companion: { pointer: true, reactions: true, status_words: "rotate", ui_guidance: true } },
    data_dir: "/x", platform: "darwin", version: "1.0.0",
  };
}
function chat(id = CHAT, extra = {}) {
  return { id, title: id === CHAT ? "Trip plan" : "Recipes", workspace: null, messages: [], actions: [], artifacts: [], runs: [], questions: [], plan: null, ...extra };
}
function host({ model, chats = { [CHAT]: chat() } } = {}) {
  bridge.calls = []; bridge.pastes = []; bridge.picks = []; bridge.receive = null;
  for (const key of Object.keys(registry)) delete registry[key];
  let current = hostState(model);
  bridge.handlers = {
    state: () => current,
    get_chat: ({ id }) => chats[id] || chat(id),
    new_chat: () => chat("e".repeat(32)),
    job_list: () => ({ jobs: [], notifications: [] }),
    screen_sessions: () => [],
    setting: ({ key, value }) => { current = { ...current, settings: { ...current.settings, [key]: value } }; return true; },
    model_select: ({ profile_id }) => { current = { ...current, model: { ...current.model, profile_id, kind: profile_id === "chatgpt" ? "chatgpt" : "gguf", local: profile_id !== "chatgpt", configured: true, state: "ready", provider: profile_id === "chatgpt" ? "ChatGPT" : undefined } }; return current.model; },
  };
  return { setState: (next) => (current = next), get state() { return current; } };
}

const App = loadSfc("./App.vue", { "./components/DetailsDrawer.vue": drawerModule });

// ---------- V1-038: the first launch offers the two ways to connect a model ----------
test("first-run welcome renders the sign-in card when no model is configured (V1-038)", async () => {
  host();
  const view = mount(App);
  await settle();
  const text = squash(textOf(view.root));
  assert.ok(text.includes("Welcome to Pebble."), text.slice(0, 400));
  for (const words of ["Connect your ChatGPT account", "Use a local model", "Your messages are sent to OpenAI to be answered.",
    "Pebble keeps useful facts from your conversations on this Mac", "Settings → Memory", "Permissions: Ask for approval.",
    "Always asks: Every edit, test run, terminal command, app launch, Git change and connector call."])
    assert.ok(text.includes(words), "missing: " + words);
  for (const slogan of ["YOUR IDEAS. YOUR MACHINE.", "Your device. Your models.", "Private by design", "API selected", "capacity"])
    assert.ok(!text.includes(slogan), "slogan or jargon left: " + slogan);
  assert.ok(text.includes("Nothing is sent anywhere until you connect a model.") && text.includes("Connect a model"));
  // ChatGPT opens Settings at Models (where Continue with ChatGPT is); a local model picks a model file.
  button(view.root, "Connect your ChatGPT account Sign in with your ChatGPT plan. Your messages are sent to OpenAI to be answered.").props.onClick();
  await settle();
  assert.equal(registry["AdvancedSettings.vue"]?.at(-1)?.["initial-tab"], "models");
  assert.ok(squash(textOf(view.root)).includes("Settings") && !squash(textOf(view.root)).includes("Make yourself at home."), "a plain dialog title");
  view.app.unmount();
  // With a model there is no setup card, and the foot says where replies come from.
  host({ model: { configured: true, state: "ready", kind: "chatgpt", local: false, provider: "ChatGPT", profile_id: "chatgpt", context: 272000 } });
  const ready = mount(App);
  await settle();
  const after = squash(textOf(ready.root));
  assert.ok(!after.includes("Connect your ChatGPT account") && after.includes("Good to see you."));
  assert.ok(after.includes("Replies come from ChatGPT, which receives your messages.") && after.includes((272000).toLocaleString() + "-token context · answered by ChatGPT"), after.slice(0, 600));
  ready.app.unmount();
  assert.deepEqual(errors, []);
});

// ---------- V1-041: switch, pressed and named states ----------
test("switches expose role=switch with aria-checked, and the theme aria-pressed (V1-041)", async () => {
  host();
  const view = mount(App);
  await settle();
  button(view.root, "Settings").props.onClick();
  await settle();
  const switches = roleOf(view.root, "switch");
  assert.deepEqual(switches.map((el) => [squash(textOf(el)).split(" ").slice(0, 2).join(" "), el.props["aria-checked"]]),
    [["Reduce motion", "false"], ["Follow the", "true"], ["Expressive reactions", "true"], ["UI design", "true"]]);
  assert.deepEqual(switches.map((el) => el.props.type), ["button", "button", "button", "button"]);
  for (const el of switches) assert.ok(el.children.some((child) => child.props?.class?.toggle !== undefined || child.props?.["aria-hidden"] === "true"), "the drawn toggle is hidden");
  const theme = ["Light", "Dark", "System"].map((name) => button(view.root, name));
  assert.deepEqual(theme.map((el) => el.props["aria-pressed"]), ["false", "false", "true"]);
  switches[0].props.onClick();
  theme[1].props.onClick();
  await settle();
  assert.equal(roleOf(view.root, "switch")[0].props["aria-checked"], "true", "Reduce motion now reads on");
  assert.deepEqual(["Light", "Dark", "System"].map((name) => button(view.root, name).props["aria-pressed"]), ["false", "true", "false"]);
  assert.deepEqual(called("setting").map((p) => p.key), ["reduced_motion", "theme"]);
  // Names and landmarks (V1-161): the sidebar toggle says whether it is open; the conversation list is a list.
  const toggle = all(view.root).find((el) => el.props["aria-label"] === "Conversations sidebar");
  assert.equal(toggle.props["aria-expanded"], true);
  assert.ok(all(view.root).some((el) => el.type === "ul" && el.props["aria-labelledby"] === "recent-heading"));
  assert.ok(all(view.root).some((el) => el.type === "h2" && el.props.id === "modal-heading" && squash(textOf(el)) === "Settings"));
  view.app.unmount();
});

// ---------- V1-042: paste attaches files; text is left alone ----------
test("pasting an image or a file sends it through attach_bytes; a text paste is unchanged (V1-042)", async () => {
  host({ model: { configured: true, state: "ready" } });
  const view = mount(App);
  await settle();
  const box = all(view.root).find((el) => el.type === "textarea" && el.props["aria-label"] === "Message Pebble");
  assert.ok(box && typeof box.props.onPaste === "function", "the message box handles paste");
  let prevented = 0;
  const image = new File([new Uint8Array([137, 80, 78, 71])], "image.png", { type: "image/png" });
  const report = new File(["a,b\n1,2\n"], "report.csv", { type: "text/csv" });
  await box.props.onPaste({ preventDefault: () => prevented++, clipboardData: { items: [{ kind: "file", getAsFile: () => image }, { kind: "string", getAsFile: () => null }], files: [] } });
  await box.props.onPaste({ preventDefault: () => prevented++, clipboardData: { items: [], files: [report] } });
  await settle();
  assert.deepEqual(bridge.pastes.map(([, name, size, type]) => [name, size, type]), [["image.png", 4, "image/png"], ["report.csv", 8, "text/csv"]]);
  assert.ok(bridge.pastes.every(([id]) => id === CHAT), "into this conversation");
  assert.equal(prevented, 2);
  assert.ok(squash(textOf(view.root)).includes("image.png") && squash(textOf(view.root)).includes("report.csv"), "both chips show");
  await box.props.onPaste({ preventDefault: () => prevented++, clipboardData: { items: [{ kind: "string", getAsFile: () => null }], files: [] } });
  assert.equal(prevented, 2, "text paste is not prevented");
  assert.equal(bridge.pastes.length, 2, "and attaches nothing");
  view.app.unmount();
});

// ---------- V1-063 (D2): Delete moves to Recently deleted, with Undo ----------
test("Delete moves a conversation to Recently deleted and offers Undo (V1-063)", async () => {
  const fake = host({ model: { configured: true, state: "ready" } });
  bridge.handlers.storage_trash_chat = ({ chat_id }) => ({ trashed: chat_id, delete_after: 9e9 });
  bridge.handlers.storage_restore_chat = ({ chat_id }) => ({ restored: chat_id });
  const view = mount(App);
  await settle();
  button(view.root, "Delete conversation Recipes").props.onClick();
  await settle();
  const text = squash(textOf(view.root));
  assert.ok(text.includes("Delete this conversation?") && text.includes("moves to Recently deleted, where you can restore it for 30 days"), text.slice(-800));
  assert.ok(!text.includes("Delete permanently") && !text.includes("Keep this conversation?"));
  button(view.root, "Move to Recently deleted").props.onClick();
  await settle();
  assert.deepEqual(called("storage_trash_chat"), [{ chat_id: OTHER }]);
  assert.deepEqual(called("delete_chat"), [], "nothing is deleted for good from here");
  const region = all(view.root).find((el) => el.props.class === "toast-region");
  assert.ok(region && region.props.role === "status" && region.props["aria-live"] === "polite", "the toast region stays mounted");
  assert.ok(squash(textOf(region)).includes("Moved to Recently deleted."));
  button(region, "Undo").props.onClick();
  await settle();
  assert.deepEqual(called("storage_restore_chat"), [{ chat_id: OTHER }]);
  assert.ok(squash(textOf(region)).includes("Restored the conversation."));
  view.app.unmount();
  void fake;
});

test("a send refused because the conversation is in Recently deleted opens a new one and keeps the words (V1-018)", async () => {
  host({ model: { configured: true, state: "ready" } });
  bridge.handlers.send = () => { throw new RpcError("This conversation is in Recently deleted.", "trashed_conversation"); };
  const view = mount(App);
  await settle();
  const box = () => all(view.root).find((el) => el.type === "textarea" && el.props["aria-label"] === "Message Pebble");
  box().props["onUpdate:modelValue"]("hello there");
  await settle();
  await button(view.root, "Send message").props.onClick();
  await settle();
  assert.ok(squash(textOf(view.root)).includes("That conversation is in Recently deleted, so Pebble opened a new one."));
  assert.equal(called("new_chat").length, 1);
  assert.equal(box().value, "hello there", "the words stay in the box");
  view.app.unmount();
});

// ---------- V1-002: the on-device history notice opens the reviewed handoff ----------
test("a cloud run that left out on-device messages offers the reviewed handoff with release ['device'] (V1-002)", async () => {
  const runs = [{ id: "r".repeat(32), state: "completed", created: 1, updated: 2 }];
  host({ model: { configured: true, state: "ready", kind: "chatgpt", local: false, provider: "ChatGPT", profile_id: "chatgpt" }, chats: { [CHAT]: chat(CHAT, { runs, messages: [{ id: "m1", role: "user", content: "hi" }] }) } });
  const view = mount(App);
  await settle();
  const picker = () => registry["ModelPicker.vue"].at(-1);
  assert.equal(picker().notice, "", "no notice before the host says so");
  const label = "Earlier on-device messages were not shared with ChatGPT. Review to include them.";
  bridge.receive({ event: "progress", data: { chat_id: CHAT, run_id: "n".repeat(32), label, kind: "note", activity: "note", handoff: { source_run_id: "s".repeat(32), profile_id: "chatgpt", withheld: 4, actions: 1 } } });
  await settle();
  assert.equal(picker().notice, label, "the host's own sentence");
  picker().onReview();
  await settle();
  const handoff = registry["ModelHandoff.vue"]?.at(-1);
  assert.ok(handoff, "Review opens the reviewed handoff in Details › Task");
  assert.deepEqual([handoff["chat-id"], handoff["run-id"], handoff["profile-id"], handoff.release], [CHAT, "s".repeat(32), "chatgpt", ["device"]]);
  assert.equal(registry["DetailsDrawer.vue"].at(-1).tab, "task");
  assert.deepEqual(called("send"), [], "nothing is sent by reviewing");
  // Switching from this Mac to a cloud model says so at once, before any send.
  view.app.unmount();
  host({ model: { configured: true, state: "ready" }, chats: { [CHAT]: chat(CHAT, { runs, messages: [{ id: "m1", role: "user", content: "hi" }] }) } });
  const local = mount(App);
  await settle();
  registry["ModelPicker.vue"].at(-1).onSelect("chatgpt");
  await settle();
  assert.equal(registry["ModelPicker.vue"].at(-1).notice, "Earlier messages were on this Mac. Review what is sent…");
  local.app.unmount();
});

// ---------- V1-034: the Forget card ----------
test("the Forget card lists the matched memories and forgets one only on the person's click (V1-034)", async () => {
  host({ model: { configured: true, state: "ready" }, chats: { [CHAT]: chat(CHAT, { messages: [{ id: "u1", role: "user", content: "forget my gym" }, { id: "a1", role: "assistant", content: "I found 2 saved memories about my gym." }] }) } });
  bridge.handlers.memory_forget_preview = ({ ids }) => ({ subject: "my gym", authorization: "none", items: ids.map((id, i) => ({ id, kind: "semantic", layer: "semantic", content: ["I go to the gym on Mondays", "Gym code is 4411"][i], origin: "direct-user", where: i ? "conversation" : "all", updated: 1, authorization: "none" })) });
  bridge.handlers.memory_delete = () => true;
  const view = mount(App);
  await settle();
  bridge.receive({ event: "memory_forget", data: { chat_id: OTHER, run_id: "r", message_id: "m", subject: "x", memory_ids: ["m-9"], authorization: "none" } });
  await settle();
  assert.deepEqual(called("memory_forget_preview"), [], "another conversation's event is not shown here");
  bridge.receive({ event: "memory_forget", data: { chat_id: CHAT, run_id: "r", message_id: "m", subject: "my gym", memory_ids: ["m-1", "m-2"], authorization: "none" } });
  await settle();
  assert.deepEqual(called("memory_forget_preview"), [{ ids: ["m-1", "m-2"] }]);
  const text = squash(textOf(view.root));
  assert.ok(text.includes("Saved memories about “my gym”") && text.includes("I go to the gym on Mondays") && text.includes("In every conversation") && text.includes("Nothing is forgotten until you choose Forget."), text.slice(-700));
  // V1-126: each message names its speaker for a screen reader.
  const speakers = all(view.root).filter((el) => el.type === "h3" && el.props.class === "sr-only").map((el) => squash(textOf(el)));
  assert.deepEqual(speakers, ["You said:", "Pebble said:"]);
  assert.deepEqual(called("memory_delete"), [], "nothing is forgotten by showing the card");
  button(view.root, "Forget: Gym code is 4411").props.onClick();
  await settle();
  assert.deepEqual(called("memory_delete"), [{ id: "m-2" }]);
  assert.ok(squash(textOf(view.root)).includes("Forgotten"));
  view.app.unmount();
});

// ---------- V1-156, V1-018: deletion events ----------
test("a deleted conversation's quiz drafts leave this window's storage, and its open view closes (V1-156)", async () => {
  host({ model: { configured: true, state: "ready" } });
  store.set("pebble.quiz.art1", "{}"); store.set("pebble.quiz.art1.2", "{}"); store.set("pebble.quiz.art10", "{}"); store.set("pebble.details.main", "{}");
  const view = mount(App);
  await settle();
  bridge.receive({ event: "storage", data: { changed: "deleted", chat_id: CHAT, deleted_artifacts: ["art1"] } });
  await settle();
  assert.deepEqual([...store.keys()].filter((k) => k.startsWith("pebble.quiz.")).sort(), ["pebble.quiz.art10"], "exactly art1's drafts");
  assert.ok(called("new_chat").length >= 1, "the deleted conversation is no longer the open one");
  // The one-time iCloud note and a waiting Documents confirmation (V1-058).
  bridge.receive({ event: "storage", data: { changed: "icloud", icloud_first_seen: true, path: "/Users/x/Documents/Pebble" } });
  await settle();
  assert.ok(squash(textOf(view.root)).includes("Your Documents folder syncs with iCloud Drive"));
  bridge.receive({ event: "storage", data: { changed: "forget_confirm", id: "q1" } });
  await settle();
  assert.ok(all(view.root).some((el) => el.props["aria-label"] === "Settings, something waits for you in Data and storage"));
  view.app.unmount();
  store.clear();
});

// ---------- V1-124: Tools is listed when a schedule or a shared window exists ----------
test("the Tools tab is listed whenever the conversation has a schedule or a shared window (V1-124)", async () => {
  host({ model: { configured: true, state: "ready" }, chats: { [CHAT]: chat(CHAT, { runs: [{ id: "r".repeat(32), state: "completed", created: 1, updated: 2 }] }) } });
  bridge.handlers.screen_sessions = () => [{ session_id: "s", state: "active", kind: "browser", pending: true }];
  const view = mount(App);
  await settle();
  button(view.root, "Details").props.onClick();
  await settle();
  const tabs = registry["DetailsDrawer.vue"].at(-1).tabs.map((t) => t.id);
  assert.ok(tabs.includes("tools") && tabs.includes("browser"), "Tools and Browser are listed: " + tabs);
  assert.deepEqual(called("job_list").at(-1), { id: CHAT });
  // V1-144: approvals and scoped tools are the protection; a checker that could not start says project tools are off.
  registry["DetailsDrawer.vue"].at(-1)["onUpdate:tab"]("activity");
  await settle();
  assert.ok(squash(textOf(view.root)).includes("Ask for approval Approvals and scoped tools") && !squash(textOf(view.root)).includes("AgentGuard + scoped tools"));
  view.app.unmount();
  const fake = host({ model: { configured: true, state: "ready" }, chats: { [CHAT]: chat(CHAT, { runs: [{ id: "r".repeat(32), state: "completed", created: 1, updated: 2 }] }) } });
  fake.setState({ ...fake.state, guard: { available: false, error: "x" } });
  const down = mount(App);
  await settle();
  button(down.root, "Details").props.onClick();
  await settle();
  registry["DetailsDrawer.vue"].at(-1)["onUpdate:tab"]("activity");
  await settle();
  assert.ok(squash(textOf(down.root)).includes("Project tools are off: the reliability checker could not start."));
  down.app.unmount();
});

// ---------- V1-163: the narrow-window drawer sheet ----------
test("below 1,050 px the drawer sheet makes the conversation inert and closes on a click beside it (V1-163)", async () => {
  host({ model: { configured: true, state: "ready" }, chats: { [CHAT]: chat(CHAT, { runs: [{ id: "r".repeat(32), state: "completed", created: 1, updated: 2 }] }) } });
  globalThis.window.innerWidth = 900;
  const view = mount(App);
  await settle();
  const inert = () => ["conversation-scroll", "composer-area"].map((c) => all(view.root).find((el) => el.props.class === c)?.props.inert);
  assert.deepEqual(inert(), [false, false]);
  button(view.root, "Details").props.onClick();
  await settle();
  assert.equal(registry["DetailsDrawer.vue"].at(-1).overlay, true);
  assert.deepEqual(inert(), [true, true], "the chat behind the sheet is inert");
  const scrim = all(view.root).find((el) => el.props.class === "details-scrim");
  assert.ok(scrim && scrim.props["aria-hidden"] === "true");
  scrim.props.onClick();
  await settle();
  assert.ok(!all(view.root).some((el) => el.props.class === "details-scrim") && !all(view.root).some((el) => el.props["data-stub"] === "DetailsDrawer"), "a click beside it closes it");
  assert.deepEqual(inert(), [false, false]);
  view.app.unmount();
  globalThis.window.innerWidth = 1300;
});

// ---------- V1-125: search and rename conversations ----------
test("the sidebar finds conversations by title and renames one through chat_rename; Escape cancels (V1-125)", async () => {
  host({ model: { configured: true, state: "ready" } });
  bridge.handlers.chat_rename = ({ id, title }) => ({ id, title });
  const view = mount(App);
  await settle();
  const listed = () => all(view.root).filter((el) => el.type === "li" && el.props.class === "chat-entry").map((el) => el.props["data-chat-id"]);
  assert.deepEqual(listed(), [CHAT, OTHER]);
  const search = all(view.root).find((el) => el.props["aria-label"] === "Search conversations");
  assert.ok(search && search.parent.props.role === "search");
  search.props["onUpdate:modelValue"]("RECI");
  await settle();
  assert.deepEqual(listed(), [OTHER], "case-insensitive title match");
  search.props["onUpdate:modelValue"]("zzz");
  await settle();
  assert.ok(squash(textOf(view.root)).includes("No conversation title matches “zzz”."));
  search.props["onUpdate:modelValue"]("");
  await settle();
  const current = all(view.root).find((el) => el.type === "button" && el.props["aria-current"] === "page");
  assert.ok(current && squash(textOf(current)).startsWith("Trip plan"), "the open conversation is marked current");
  button(view.root, "Rename conversation Recipes").props.onClick();
  await settle();
  const field = () => all(view.root).find((el) => el.props["aria-label"] === "Conversation name");
  field().props["onUpdate:modelValue"]("Cooking ideas");
  await settle();
  const form = field().parent;
  [form.props.onKeydown].flat().forEach((fn) => fn({ key: "Escape", preventDefault() {}, stopPropagation() {} }));
  await settle();
  assert.ok(!field(), "Escape closes the field");
  assert.deepEqual(called("chat_rename"), [], "and saves nothing");
  button(view.root, "Rename conversation Recipes").props.onClick();
  await settle();
  field().props["onUpdate:modelValue"]("  Cooking   ideas ");
  await settle();
  await field().parent.props.onSubmit({ preventDefault() {} });
  await settle();
  assert.deepEqual(called("chat_rename"), [{ id: OTHER, title: "Cooking ideas" }]);
  view.app.unmount();
});

// ---------- Pet.vue ----------
const Pet = loadSfc("./Pet.vue");
async function openPet(fake) {
  const view = mount(Pet);
  await settle();
  const avatar = all(view.root).find((el) => el.props["aria-label"] === "Chat with Pebble");
  for (const handler of [avatar.props.onKeydown].flat()) await handler({ key: "Enter", preventDefault() {}, stopPropagation() {} });
  await settle();
  void fake;
  return view;
}
test("the pet lets go of a saved conversation that is in Recently deleted (V1-018)", async () => {
  const fake = host({ model: { configured: true, state: "ready" } });
  store.set("pebble-pet-chat", OTHER);
  bridge.handlers.get_chat = ({ id }) => (id === OTHER ? chat(OTHER, { trashed: true }) : chat(id));
  const view = await openPet(fake);
  assert.deepEqual(called("get_chat"), [{ id: OTHER }]);
  assert.equal(called("new_chat").length, 1, "a new conversation instead");
  assert.equal(store.get("pebble-pet-chat"), "e".repeat(32));
  // A conversation trashed while open: the send is refused with the code, and the pet lets go of it.
  bridge.handlers.send = () => { throw new RpcError("This conversation is in Recently deleted.", "trashed_conversation"); };
  const box = all(view.root).find((el) => el.type === "textarea");
  box.props["onUpdate:modelValue"]("hello");
  await settle();
  await all(view.root).find((el) => el.props.class === "pet-composer").props.onSubmit({ preventDefault() {} });
  await settle();
  assert.ok(squash(textOf(view.root)).includes("That conversation is in Recently deleted"), squash(textOf(view.root)).slice(-300));
  assert.equal(store.get("pebble-pet-chat"), undefined, "the saved id is dropped");
  view.app.unmount();
  store.clear();
});
test("the pet: Enter during input-method composition does not send; paste attaches (V1-055, V1-042)", async () => {
  const fake = host({ model: { configured: true, state: "ready" } });
  bridge.handlers.send = () => ({ message_id: "m" });
  const view = await openPet(fake);
  const box = () => all(view.root).find((el) => el.type === "textarea");
  box().props["onUpdate:modelValue"]("こんにちは");
  await settle();
  const enter = (extra) => box().props.onKeydown({ key: "Enter", ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, preventDefault() {}, stopPropagation() {}, ...extra });
  enter({ isComposing: true, keyCode: 13 });
  enter({ isComposing: false, keyCode: 229 });
  await settle();
  assert.deepEqual(called("send"), [], "confirming a composition sends nothing");
  enter({ isComposing: false, keyCode: 13 });
  await settle();
  assert.deepEqual(called("send").map((p) => p.text), ["こんにちは"]);
  let prevented = 0;
  const shot = new File([new Uint8Array(10)], "", { type: "image/png" });
  await box().props.onPaste({ preventDefault: () => prevented++, clipboardData: { items: [{ kind: "file", getAsFile: () => shot }], files: [] } });
  await box().props.onPaste({ preventDefault: () => prevented++, clipboardData: { items: [{ kind: "string", getAsFile: () => null }], files: [] } });
  await settle();
  assert.deepEqual(bridge.pastes.map(([, name, size]) => [name, size]), [["", 10]], "a clipboard image is named by the host");
  assert.equal(prevented, 1, "text paste untouched");
  view.app.unmount();
});
test("the pet cannot widen permissions; it can narrow them, and Full shows its warning (V1-055)", async () => {
  const fake = host({ model: { configured: true, state: "ready" } });
  const view = await openPet(fake);
  button(view.root, "Tool permissions").props.onClick();
  await settle();
  const radios = () => roleOf(view.root, "radio");
  assert.deepEqual(radios().map((el) => el.props["aria-checked"]), ["true", "false", "false"], "the mode is a checked radio");
  assert.ok(squash(textOf(radios()[1])).startsWith("Auto in project"), "the shared name, not 0.6.1's 'Approve in my project'");
  assert.deepEqual(radios().map((el) => el.props["aria-disabled"]), [undefined, "true", "true"], "wider modes are marked");
  radios()[2].props.onClick();
  const off = roleOf(view.root, "switch").find((el) => squash(textOf(el)) === "App and browser control");
  assert.equal(off.props["aria-checked"], "false");
  off.props.onClick();
  await settle();
  assert.deepEqual(called("setting"), [], "nothing wider is sent");
  assert.ok(squash(textOf(view.root)).includes("Open the workspace to turn this on."));
  // Narrowing works: the web switch off.
  const web = roleOf(view.root, "switch").find((el) => squash(textOf(el)) === "Web access");
  assert.equal(web.props["aria-checked"], "true");
  web.props.onClick();
  await settle();
  assert.equal(called("setting").length, 1);
  assert.equal(called("setting")[0].value.tools.web, false);
  assert.equal(called("setting")[0].value.mode, "ask");
  // With Full on (set in the workspace), the pet warns and can step down to Auto.
  fake.setState({ ...fake.state, settings: { ...fake.state.settings, permissions: { ...fake.state.settings.permissions, mode: "full" } } });
  bridge.receive({ event: "settings", data: {} });
  await settle();
  assert.ok(squash(textOf(view.root)).includes("Full access is on. Everything Auto runs, plus app launches and Git changes that neither delete work nor publish it."));
  roleOf(view.root, "radio")[1].props.onClick();
  await settle();
  assert.equal(called("setting").at(-1).value.mode, "auto");
  view.app.unmount();
});

// ---------- ModelPicker: the provider list only filters (V1-145, V1-163) ----------
const Picker = loadSfc("./components/ModelPicker.vue");
test("ModelPicker's provider select filters only, and is hidden with one provider (V1-145, V1-163)", async () => {
  const profiles = [{ id: "local", name: "Local", kind: "gguf", context: 4096, tools: true }, { id: "gpt", name: "ChatGPT", kind: "chatgpt", model: "gpt-6-luna", context: 1, tools: true, connection_id: "c1", provider_name: "ChatGPT" }];
  const selected = [], reviewed = [];
  const view = mount(Picker, { profiles, current: "local", onSelect: (id) => selected.push(id), onReview: () => reviewed.push(1), notice: "Earlier messages were on this Mac. Review what is sent…" });
  await settle();
  const providerSelect = all(view.root).find((el) => el.type === "select" && el.props["aria-label"] === "Show models from");
  assert.ok(providerSelect, "two providers: the filter shows");
  providerSelect.props.onChange({ target: { value: "c1" } });
  await settle();
  assert.deepEqual(selected, [], "choosing a provider never switches the model (0.6.1 selected its first model)");
  const modelSelect = all(view.root).find((el) => el.type === "select" && el.props["aria-label"] === "Choose model");
  assert.equal(modelSelect.props.value, "", "the current model is outside the filter: nothing is chosen for the person");
  assert.ok(squash(textOf(view.root)).includes("Choose a model from this provider"));
  modelSelect.props.onChange({ target: { value: "gpt" } });
  assert.deepEqual(selected, ["gpt"], "only the person's own choice selects");
  assert.ok(squash(textOf(view.root)).includes("Earlier messages were on this Mac. Review what is sent…"));
  button(view.root, "Review…").props.onClick();
  assert.equal(reviewed.length, 1);
  view.app.unmount();
  const one = mount(Picker, { profiles: profiles.slice(0, 1), current: "local" });
  await settle();
  assert.ok(!all(one.root).some((el) => el.props["aria-label"] === "Show models from"), "one provider: no filter");
  one.app.unmount();
});

// ---------- Markdown: Copy on code blocks, scrolling tables, refused links, one parse per frame ----------
test("Markdown adds Copy to code blocks and a scroll region to tables, refuses non-https links with a notice, and parses at most once per frame (V1-122, V1-081)", async () => {
  let parses = 0;
  const frames = [];
  globalThis.requestAnimationFrame = (fn) => frames.push(fn);
  globalThis.cancelAnimationFrame = () => {};
  const marked = { marked: { parse: (text) => { parses++; return text.includes("|") ? "<table><tr><td>1</td></tr></table>" : "<pre><code>a &lt;b&gt;</code></pre><p><a href=\"" + (text.includes("http:") ? "http://evil.example/" : "https://ok.example/") + "\">x</a></p>"; } } };
  const purify = { __esModule: true, default: { sanitize: (html) => html } };
  const Markdown = loadSfc("./components/Markdown.vue", { marked, dompurify: purify });
  const M = evaluate(compileScript(parse(read("./components/Markdown.vue")).descriptor, { id: "m" }).content, (name) => (name === "marked" ? marked : name === "dompurify" ? purify : /bridge$/.test(name) ? bridge : name === "vue" ? vueShim : require(name)));
  assert.equal(M.decorate("<pre><code>x</code></pre>"), '<div class="code-block"><button type="button" class="code-copy" aria-label="Copy code">Copy</button><pre><code>x</code></pre></div>');
  assert.equal(M.decorate("<table><tr><td>1</td></tr></table>"), '<div class="table-scroll" role="region" aria-label="Table" tabindex="0"><table><tr><td>1</td></tr></table></div>');
  assert.equal(M.decorate("<p>&lt;pre&gt;</p>"), "<p>&lt;pre&gt;</p>", "escaped text is never a tag");
  assert.deepEqual([M.openable("https://a"), M.openable("http://127.0.0.1:9/v"), M.openable("http://a"), M.openable("javascript:x"), M.openable(null)], [true, true, false, false, false]);
  const props = vue.reactive({ text: "start" });
  const view = mount(Markdown, props);
  await settle();
  assert.equal(parses, 1, "the first text is parsed at once");
  for (let i = 0; i < 50; i++) { props.text += " token" + i; await settle(); }
  assert.equal(parses, 1, "fifty tokens before the next frame: no parse yet");
  assert.equal(frames.length, 1, "one frame requested for all of them");
  frames.shift()();
  await settle();
  assert.equal(parses, 2, "one parse for the frame (0.6.1 parsed once per token: 51)");
  const div = all(view.root).find((el) => el.props.class === "markdown");
  assert.ok(div.props.innerHTML.includes('class="code-copy"'));
  // A refused link: nothing opens, a notice bubbles to the window's toast.
  props.text = "http: link";
  await settle();
  frames.shift()();
  await settle();
  const anchor = { getAttribute: () => "http://evil.example/", closest: (sel) => (sel === "a" ? anchor : null) };
  div.props.onClick({ target: { closest: (sel) => (sel === "a" ? anchor : null) }, preventDefault() {} });
  assert.deepEqual(div.events.map((e) => e.detail), ["Pebble opens only secure web links (https)."]);
  assert.deepEqual(called("open_url"), []);
  view.app.unmount();
  delete globalThis.requestAnimationFrame;
});

// ---------- QuizRenderer keyboard flow (V1-162) ----------
test("quiz choices are one radio group with arrow keys; Next moves focus and says which question (V1-162)", async () => {
  host();
  const now = Date.now() / 1000;
  bridge.handlers.quiz_attempt = () => ({ attempt_id: "a1", deadline: now + 600, status: "open", answers: { q1: null, q2: null } });
  bridge.handlers.quiz_save = () => ({ saved: true });
  const Quiz = loadSfc("./components/QuizRenderer.vue");
  const artifact = { id: "quiz", chat_id: CHAT, title: "Gases", kind: "quiz", revision: 1, status: "ready", created: 1, data: { duration_minutes: 10, questions: [{ id: "q1", question: "Which gas?", choices: ["O2", "CO2", "N2"] }, { id: "q2", question: "Second?", choices: ["A", "B"] }] } };
  const view = mount(Quiz, { artifact, surface: "main" });
  await settle();
  const radios = () => roleOf(view.root, "radio");
  assert.deepEqual(radios().map((el) => el.props.tabindex), [0, -1, -1], "one Tab stop for the group");
  radios()[0].props.onKeydown({ key: "ArrowDown", preventDefault() {} });
  await settle();
  assert.deepEqual(radios().map((el) => [el.props["aria-checked"], el.props.tabindex]), [["false", -1], ["true", 0], ["false", -1]], "arrow keys choose, as native radios do");
  assert.equal(focused, radios()[1], "and move focus");
  assert.deepEqual(called("quiz_save").at(-1).answers, { q1: 1, q2: null });
  const jumps = all(view.root).filter((el) => el.type === "button" && /^Question \d/.test(el.props["aria-label"] || ""));
  assert.deepEqual(jumps.map((el) => el.props["aria-label"]), ["Question 1, answered", "Question 2"]);
  button(view.root, "Next").props.onClick();
  await settle();
  assert.ok(roleOf(view.root, "status").some((el) => squash(textOf(el)) === "Question 2 of 2"), "the move is announced");
  assert.equal(focused?.type, "h3", "focus moves to the new question");
  view.app.unmount();
});

// ---------- ArtifactPanel header (V1-127) and ActionReview (V1-120, V1-040, V1-141) ----------
test("the canvas header is the view's title, version, Refresh, Export…, Copy and Close; internals sit under Details (V1-127)", async () => {
  host();
  const Panel = loadSfc("./components/ArtifactPanel.vue");
  const artifact = { id: "t1", chat_id: CHAT, title: "Monthly costs", kind: "table", revision: 3, status: "ready", owner_run_id: "run-1", created: 1, data: { columns: [{ id: "a", label: "Item" }, { id: "b", label: "Cost" }], rows: [{ a: "Rent", b: 900 }] } };
  bridge.handlers.artifact_get = () => artifact;
  bridge.handlers.artifact_history = () => [artifact];
  const exported = [];
  const view = mount(Panel, { artifacts: [artifact], selected: "t1", onExport: (id) => exported.push(id) });
  await settle();
  const header = all(view.root).find((el) => el.type === "header");
  const names = all(header).filter((el) => el.type === "button").map(nameOf);
  assert.deepEqual(names, ["Refresh", "Export…", "Copy", "Close canvas"]);
  assert.ok(squash(textOf(header)).startsWith("Monthly costs Version 3"));
  const details = all(view.root).find((el) => el.type === "details" && el.props.class === "artifact-ownership");
  assert.ok(details && squash(textOf(details)).startsWith("Details") && squash(textOf(details)).includes("Owner run: run-1") && squash(textOf(details)).includes("Stop artifact"), "internals under Details");
  button(header, "Export…").props.onClick();
  assert.deepEqual(exported, ["t1"]);
  assert.equal(focused?.type, "h2", "opening the canvas moves focus to its title (V1-129)");
  view.app.unmount();
  const app = read("./App.vue");
  assert.ok(/@close="closeCanvas"/.test(app) && /function closeCanvas\(\) \{\s*canvas\.value = false;\s*nextTick\(\(\) => \(canvasButton\.value \|\| composer\.value\)\?\.focus\(\)\);/.test(app), "closing returns focus to the canvas button");
  assert.ok(/@keydown\.esc\.stop\.prevent="cancelEdit\(message\.id\)"/.test(app), "cancelling an edit returns focus to its Edit button");
});
test("approval cards lead with what changes, keep internals under Technical details, and say why AgentGuard asked (V1-120, V1-141, V1-040)", async () => {
  const Review = loadSfc("./components/ActionReview.vue");
  const html = await renderToString(createSSRApp(Review, { action: { kind: "command", status: "pending", payload: { command: "rm -rf build", guard_review: "AgentGuard asked for your review: removes a folder recursively.",
    plan: { steps: [{ argv: ["rm", "-rf", "build"], executable: { realpath: "/bin/rm", sha256: "a".repeat(64) }, cwd: "." }], network: { mode: "none" }, resources: {}, plan_hash: "b".repeat(64) } } } }));
  const why = html.indexOf("AgentGuard asked for your review: removes a folder recursively."), argv = html.indexOf("rm -rf build"), technical = html.indexOf("<summary>Technical details</summary>");
  assert.ok(why > 0 && argv > why && technical > argv, "why, then the command, then the internals");
  assert.ok(html.indexOf("Executable realpath") > technical && html.indexOf("Untrusted repository data") > technical, "internals and the data note are under Technical details");
  const css = read("./components/ActionReview.vue") + read("./style.css");
  for (const [selector, min] of [[".action-review", 12], [".review-pre", 12], [".review-badge", 11], [".diff,\n.test-output", 12], [".action-description", 12], [".action-heading > span", 11]]) {
    const at = css.indexOf("\n" + selector + " {");
    assert.ok(at >= 0, selector);
    const size = Number(/font(?:-size)?: (\d+)px/.exec(css.slice(at, css.indexOf("}", at)))[1]);
    assert.ok(size >= min, `${selector} is ${size}px (at least ${min}px)`);
  }
  assert.ok(/\.action-buttons \.primary-button,\s*\.action-buttons \.secondary-button \{\s*min-height: 32px;/.test(css), "32 px approval buttons");
  const app = read("./App.vue");
  assert.ok(/Don't allow<\/button/.test(app) && !/>\s*Dismiss<\/button/.test(app), "Don't allow replaces Dismiss");
});

// ---------- static checks the validator can reuse ----------
test("no type under 11 px in the owned surfaces; ⌘+ zooms the main window (V1-119)", () => {
  for (const path of ["./style.css", "./App.vue", "./Pet.vue", "./components/ActionReview.vue", "./components/StatusLine.vue", "./components/MessageActions.vue",
    "./components/DetailsDrawer.vue", "./components/ArtifactPanel.vue", "./components/QuizRenderer.vue", "./components/ModelPicker.vue"]) {
    const small = [...read(path).matchAll(/font(?:-size)?:\s*(\d+(?:\.\d+)?)px/g)].map((m) => Number(m[1])).filter((n) => n < 11);
    assert.deepEqual(small, [], path);
  }
  const conf = JSON.parse(read("../src-tauri/tauri.conf.json"));
  assert.equal(conf.app.windows.find((w) => w.label === "main").zoomHotkeysEnabled, true);
});
test("targets are at least 24–28 px and the pet loads Markdown on first use (V1-160, V1-165)", () => {
  const css = read("./style.css"), pet = read("./Pet.vue"), status = read("./components/StatusLine.vue");
  assert.match(css, /\.chat-row-action,\n\.chat-delete \{[^}]*width: 28px;\s*height: 28px;/);
  assert.match(css, /\.source-chips button \{[^}]*width: 24px;\s*height: 24px;/);
  assert.match(status, /\.status-stop \{[^}]*min-height: 28px;/);
  assert.match(pet, /\.quick-chat header button \{[^}]*width: 28px;\s*height: 28px;/);
  assert.match(pet, /\.quick-chat form \.pet-attachments button \{[^}]*width: 24px;\s*height: 24px;/);
  assert.match(css, /\.composer:focus-within \{\s*border-color: var\(--focus, var\(--accent\)\);/);
  assert.ok(/const Markdown = defineAsyncComponent\(\(\) => import\("\.\/components\/Markdown\.vue"\)\);/.test(pet) && !/^import Markdown from/m.test(pet));
  assert.ok(!/^import CommandPalette from/m.test(pet), "the palette too");
  assert.ok(read("./App.vue").includes('<summary>What\'s new</summary>'), "What's new in About (V1-135)");
});
// m24c merge: the parent's main.rs patches landed, so these two checks are plain tests now (they were `todo` until
// then); a later main.rs without either patch fails here instead of reporting a todo.
test("chat_rename stays a main-window route once the shell allows it (V1-125)", () => {
  const shell = read("../src-tauri/src/main.rs");
  const allowed = shell.match(/const ALLOWED: &\[&str\] = &\[(.*?)\];/s)?.[1] || "";
  const pet = shell.match(/window\.label\(\)=="pet" && !\[(.*?)\]\.contains/s)?.[1] || "";
  const native = shell.match(/const NATIVE_ONLY: &\[&str\] = &\[(.*?)\];/s)?.[1] || "";
  assert.ok(allowed.includes('"chat_rename"') && !pet.includes('"chat_rename"') && !native.includes('"chat_rename"'));
});
test("the pet's shell refuses a wider permission (V1-055, parent main.rs patch)", () => {
  const shell = read("../src-tauri/src/main.rs");
  assert.ok(/fn widens\(/.test(shell) && /Open the workspace to turn this on\./.test(shell), "main.rs compares the pet's permissions with the current ones");
  // The refusal is the pet's alone, inside rpc, before the change reaches the host.
  const rpc = shell.slice(shell.indexOf("async fn rpc("), shell.indexOf("fn compact("));
  const line = rpc.split("\n").find((l) => l.includes("Open the workspace to turn this on.")) || "";
  assert.ok(/from_pet && method=="setting" && pet_widens\(/.test(line), line);
  assert.ok(rpc.indexOf("pet_widens(") < rpc.indexOf("backend.call(&method"), "checked before the setting is sent");
});
test("no window error", () => assert.deepEqual(errors, []));
