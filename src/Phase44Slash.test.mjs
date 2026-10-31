import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, defineComponent, h, nextTick } from "vue";
import { transform } from "sucrase";

// Phase 44 (1.9.1): the composer's slash commands and the Details drawer's More menu, as the person used them in the
// installed 1.9.0 (2026-10-25, 23:40: "/ commands ... does nothing as their description says"; 23:45: the three
// sections under the drawer's "⋯" do nothing). The real App.vue, SlashMenu.vue and DetailsDrawer.vue run against a small
// fake DOM that clicks the way WebKit, the macOS web view Pebble runs in, does, and a recording bridge whose `send`
// answers a runtime command exactly as app.send does. Other child panels are stubs that record what they were given.
const require = createRequire(import.meta.url);
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const vue = require("vue");

// ---------- a fake DOM with focus ----------
let focused = null;
const handlersOf = (el, name) => [el?.props?.[name]].flat().filter((fn) => typeof fn === "function");
function moveFocus(to) {
  const from = focused;
  if (from === to) return;
  focused = to;
  if (from) {
    for (const fn of handlersOf(from, "onBlur")) fn({ type: "blur", target: from, currentTarget: from, relatedTarget: to });
    for (let n = from; n; n = n.parent) for (const fn of handlersOf(n, "onFocusout")) fn({ type: "focusout", target: from, currentTarget: n, relatedTarget: to });
  }
  if (to) for (const fn of handlersOf(to, "onFocus")) fn({ type: "focus", target: to, currentTarget: to, relatedTarget: from });
}
// Marked raw: Vue never wraps a real DOM element in a reactive proxy (a template ref holds the element itself).
function node(type, text = "") {
  return vue.markRaw({
    type, text, children: [], props: {}, parent: null, value: "", dataset: {}, scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document, dispatchEvent: () => true,
    focus() { moveFocus(this); }, blur() { if (focused === this) moveFocus(null); }, select() {}, scrollIntoView() {},
    setPointerCapture() {}, releasePointerCapture() {},
    contains(other) { for (let n = other; n; n = n.parent) if (n === this) return true; return false; },
    closest(selector) { for (let n = this; n; n = n.parent) if (selector.startsWith("#") && n.props.id === selector.slice(1)) return n; return null; },
  });
}
const renderer = createRenderer({
  createElement: (type) => node(type), createText: (text) => node("#text", text), createComment: (text) => node("#comment", text),
  insert(child, parent, anchor) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child);
  },
  remove(child) {
    if (focused && child.contains(focused)) focused = null; // a removed element loses focus without an event, as in a browser
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = null;
  },
  setText(child, text) { child.text = text; },
  setElementText(child, text) { child.text = text; child.children = []; },
  patchProp(child, key, _previous, value) { child.props[key] = value; },
  parentNode: (child) => child.parent,
  nextSibling: (child) => child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
});
const all = (el) => [el, ...el.children.flatMap(all)];
const textOf = (el) => (el.type === "#comment" ? "" : el.type === "#text" ? el.text : [el.text, ...el.children.map(textOf)].join(" "));
const squash = (text) => text.replace(/\s+/g, " ").trim();
const tight = (el) => textOf(el).replace(/\s+/g, "");
const attached = (el, root) => { for (let n = el; n; n = n.parent) if (n === root) return true; return false; };
async function settle() { for (let i = 0; i < 30; i++) { await Promise.resolve(); await nextTick(); } }

// Events bubble from the element to the page; a handler may stop them or prevent their default.
function dispatch(el, name, init) {
  let stopped = false, prevented = false;
  const event = { ...init, target: el, currentTarget: el, get defaultPrevented() { return prevented; },
    preventDefault() { prevented = true; }, stopPropagation() { stopped = true; } };
  for (let n = el; n && !stopped; n = n.parent) for (const fn of handlersOf(n, name)) { event.currentTarget = n; fn(event); }
  return { event, stopped, prevented };
}
// A mouse click as WebKit performs it. A button never takes focus from a click (only text fields and elements with a
// tabindex that are not buttons do), so unless a mousedown handler prevents it, mousedown moves the focus to the nearest
// such element, or clears it: the element that had focus gets a focusout whose relatedTarget is null. The click then
// reaches the element only if it is still in the page.
function mouseFocusable(el) {
  for (let n = el; n; n = n.parent)
    if (n.type !== "button" && (["textarea", "input", "select"].includes(n.type) || (n.props.tabindex !== undefined && n.props.tabindex !== null))) return n;
  return null;
}
async function click(el, root) {
  assert.ok(el && attached(el, root), "the element to click is on the page");
  const down = dispatch(el, "onMousedown", { type: "mousedown", button: 0 });
  if (!down.prevented) moveFocus(mouseFocusable(el));
  await settle();
  const landed = attached(el, root);
  if (landed) dispatch(el, "onClick", { type: "click", button: 0 });
  await settle();
  return landed;
}
// A key press on the focused element; unless stopped it reaches the page's own keydown listener.
async function press(el, key, extra = {}) {
  const sent = dispatch(el, "onKeydown", { type: "keydown", key, keyCode: key === "Enter" ? 13 : 0, shiftKey: false, altKey: false, metaKey: false, ctrlKey: false, isComposing: false, repeat: false, ...extra });
  if (!sent.stopped) for (const fn of documentListeners.get("keydown") || []) fn(sent.event);
  await settle();
  return sent.prevented;
}

// ---------- globals the window reads (timers never fire) ----------
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
const copied = [];
Object.defineProperty(globalThis, "navigator", { value: { clipboard: { writeText: async (text) => void copied.push(text) } }, configurable: true });
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
  native: true, RpcError, calls: [], handlers: {}, receive: null,
  async rpc(method, params = {}) {
    bridge.calls.push([method, structuredClone(params)]);
    const handler = bridge.handlers[method];
    if (!handler) throw new Error("Unknown request.");
    return structuredClone(await handler(params));
  },
  async attachBytes() { throw new Error("not in these fixtures"); },
  async subscribe(callback) { bridge.receive = callback; return () => {}; },
  async subscribeDrops() { return () => {}; },
  async selectFile() { return null; },
  async selectAttachments() { return []; },
  async setCompact() {},
  async openExternal() {},
};
const called = (method) => bridge.calls.filter(([m]) => m === method).map(([, params]) => params);

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
// The reply renderer shows its text, so the transcript can be read.
const markdown = { __esModule: true, default: defineComponent({ inheritAttrs: false, setup: (_, { attrs }) => () => h("div", { "data-stub": "Markdown.vue" }, String(attrs.text ?? "")) }) };
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
  const source = read(path);
  const { descriptor } = parse(source, { filename: path });
  const script = compileScript(descriptor, { id: path });
  const template = compileTemplate({ source: descriptor.template.content, filename: path, id: path,
    compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] } });
  assert.deepEqual(template.errors, [], path);
  const base = new URL(path, import.meta.url);
  const resolve = (name) => {
    if (Object.hasOwn(overrides, name)) return overrides[name];
    if (name === "vue") return vueShim;
    if (name === "@phosphor-icons/vue") return icons;
    if (name === "@tauri-apps/api/core") return { invoke: async () => {} };
    if (name === "@tauri-apps/api/event") return { listen: async () => () => {} };
    if (name === "@tauri-apps/api/window") return { getCurrentWindow: () => ({ onResized: async () => () => {}, startDragging: async () => {} }) };
    if (/bridge$/.test(name)) return bridge;
    if (name.endsWith(".vue")) return { __esModule: true, default: stub(name) };
    if (name.startsWith(".")) return loadTs(new URL(name.endsWith(".ts") ? name : name + ".ts", base));
    return require(name);
  };
  const module = evaluate(script.content, resolve);
  module.default.render = evaluate(template.code, resolve).render;
  return module;
}
const errors = [];
function mount(component) {
  const root = node("root");
  const app = renderer.createApp({ setup: () => () => h(component) });
  app.config.errorHandler = (error) => errors.push(error);
  app.config.warnHandler = () => {};
  app.mount(root);
  return { root, app };
}
const SlashMenu = loadSfc("./components/SlashMenu.vue");
const Drawer = loadSfc("./components/DetailsDrawer.vue");
// Phase 46 (1.9.29, w25-genui-b): a reply renders through ReplyBody (which hands its text to Markdown.vue); the stub shows
// the reply's words either way
const App = loadSfc("./App.vue", { "./components/SlashMenu.vue": SlashMenu, "./components/DetailsDrawer.vue": Drawer, "./components/Markdown.vue": markdown, "./components/ReplyBody.vue": markdown }).default;

// ---------- the host ----------
// The host's own catalogue, read from runtime_commands.py as command_catalogue sends it.
const backend = read("../backend/pebble/runtime_commands.py");
const CATALOGUE = [...backend.matchAll(/^\s*\{\s*['"]name['"]:\s*['"]([a-z]+)['"].*$/gm)].map(([line, name]) => {
  const field = (key) => (line.match(new RegExp(`['"]${key}['"]:\\s*(['"])((?:\\\\.|(?!\\1).)*)\\1`)) || [])[2]?.replace(/\\'/g, "'") ?? "";
  return { name, arguments: field("arguments"), description: field("description") };
});
// What runtime_commands.command answers in a conversation with no tasks or schedules (the person's two commands).
const ANSWERS = { status: "This conversation has no tasks yet.", jobs: "This conversation has no schedules. Create one under Details › Tools.",
  memory: "Personalized memory: on." };
const CHAT = "c".repeat(32), RUN = "r".repeat(32);
function chatRecord(id = CHAT, extra = {}) {
  return { id, title: "Trip plan", workspace: null, messages: [], actions: [], artifacts: [], runs: [], questions: [], plan: null, ...extra };
}
function host({ chat = {}, state = {} } = {}) {
  bridge.calls = []; bridge.receive = null;
  for (const key of Object.keys(registry)) delete registry[key];
  copied.length = 0;
  store.clear();
  focused = null;
  const chats = { [CHAT]: chatRecord(CHAT, chat) };
  let sent = 0;
  const current = {
    chats: [{ id: CHAT, title: "Trip plan", workspace: null }],
    model: { state: "ready", configured: true, name: "ChatGPT", context: 272000, kind: "chatgpt", local: false, provider: "ChatGPT", profile_id: "chatgpt" },
    profiles: [], guard: { available: true, error: null }, active: null, active_run: null, runs: [],
    settings: { theme: "system", reduced_motion: false, memory_enabled: true, permissions: { mode: "ask", tools: {} },
      companion: { pointer: true, reactions: true, status_words: "rotate", ui_guidance: true } },
    data_dir: "/x", platform: "darwin", version: "1.9.0", ...state,
  };
  bridge.handlers = {
    state: () => current,
    get_chat: ({ id }) => chats[id] || chatRecord(id),
    new_chat: () => (chats["e".repeat(32)] = chatRecord("e".repeat(32), { title: "New conversation" })),
    command_catalogue: () => CATALOGUE,
    job_list: () => ({ jobs: [], notifications: [] }),
    screen_sessions: () => [],
    goal_list: () => [],
    cancel: () => true,
    // app.send for a runtime command (backend/pebble/app.py): the host stores the person's command and its reply, emits
    // `complete` for the reply, and only then returns {message_id, command: true}. No task starts, so no token, run or
    // idle event follows.
    send: ({ id, text, source_ids }) => {
      const words = text.trim();
      assert.ok(words.startsWith("/"), "these fixtures send commands only: " + words);
      assert.deepEqual(source_ids, [], "a command carries no attachments");
      const name = words.slice(1).split(/\s/)[0];
      sent++;
      const mid = "command-" + String(sent).repeat(32), reply = "command-" + "f".repeat(31) + sent;
      chats[id].messages.push({ id: mid, role: "user", content: words, created: sent }, { id: reply, role: "assistant", content: ANSWERS[name] || "Done.", created: sent });
      bridge.receive({ event: "complete", data: { chat_id: id, id: reply, seconds: 0 } });
      return { message_id: mid, command: true };
    },
  };
  return { chats, current };
}

// ---------- reading the window ----------
const composer = (root) => all(root).find((el) => el.type === "textarea" && el.props["aria-label"] === "Message Pebble");
async function type(root, text) {
  const box = composer(root);
  box.focus(); // a click in a text field focuses it
  box.props["onUpdate:modelValue"](text);
  await settle();
  return box;
}
const options = (root) => all(root).filter((el) => el.type === "li" && el.props.role === "option");
const transcript = (root) => all(root).filter((el) => typeof el.props["data-message-id"] === "string").map((el) => ({
  id: el.props["data-message-id"], role: String(el.props.class).split(" ")[1], text: squash(textOf(el)) }));
const drawerEl = (root) => all(root).find((el) => el.type === "aside" && el.props.id === "details-drawer");
const panelId = (root) => all(root).find((el) => el.props.role === "tabpanel")?.props.id;
const tabs = (root) => all(root).filter((el) => el.props.role === "tab").map((el) => squash(textOf(el)));
const stubbed = (root, label) => all(root).filter((el) => el.props["data-stub"] === label);
const moreButton = (root) => all(root).find((el) => el.type === "button" && el.props["aria-label"] === "More sections");
const menuItems = (root) => all(root).filter((el) => el.props.role === "menuitem");
const toast = (root) => squash(textOf(all(root).find((el) => el.props.class === "toast-region")));

// ---------- host commands: the answer appears ----------
test("a runtime command sent with Enter shows the host's reply below it (the person's commands, 23:36 and 23:37)", async () => {
  const { chats } = host();
  const view = mount(App);
  await settle();
  const box = await type(view.root, "/status");
  assert.ok(options(view.root).length, "the menu lists commands");
  assert.equal(tight(options(view.root)[0]).slice(0, 7), "/status", "the exact name is the first row");
  assert.equal(options(view.root)[0].props["aria-selected"], true, "and Enter runs it");
  assert.equal(await press(box, "Enter"), true, "Enter is the menu's, never the text box's");
  assert.deepEqual(called("send").map((p) => p.text), ["/status"], "sent as typed text, through send()");
  const [command, reply] = chats[CHAT].messages;
  assert.deepEqual(transcript(view.root).map((m) => [m.id, m.role]), [[command.id, "user"], [reply.id, "assistant"]],
    "the command, then the host's reply: " + JSON.stringify(transcript(view.root)));
  assert.ok(transcript(view.root)[1].text.includes("This conversation has no tasks yet."), "the reply's words are shown");
  assert.equal(box.value, "", "the box is empty again");
  assert.equal(squash(textOf(view.root)).includes("could"), false, "no error");
  view.app.unmount();
  assert.deepEqual(errors, []);
});

test("a runtime command chosen with a click shows its reply, and focus stays in the box (WebKit)", async () => {
  const { chats } = host();
  const view = mount(App);
  await settle();
  const box = await type(view.root, "/");
  const row = options(view.root).find((el) => tight(el).startsWith("/jobs"));
  assert.ok(row, "the catalogue's /jobs row: " + options(view.root).map(tight).join(" | "));
  assert.equal(await click(row, view.root), true, "the row is still there when the click lands");
  assert.equal(focused, box, "the box keeps focus: a row never takes it");
  assert.deepEqual(called("send").map((p) => p.text), ["/jobs"]);
  assert.deepEqual(transcript(view.root).map((m) => m.id), chats[CHAT].messages.map((m) => m.id));
  assert.ok(transcript(view.root)[1].text.includes("This conversation has no schedules."), JSON.stringify(transcript(view.root)));
  // A command typed in full and sent with the Send button answers the same way.
  await type(view.root, "/memory status");
  all(view.root).find((el) => el.type === "button" && el.props["aria-label"] === "Send message").props.onClick();
  await settle();
  assert.deepEqual(called("send").map((p) => p.text), ["/jobs", "/memory status"]);
  assert.equal(transcript(view.root).length, 4, "two commands, two replies");
  assert.ok(transcript(view.root)[3].text.includes("Personalized memory: on."));
  view.app.unmount();
  assert.deepEqual(errors, []);
});

test("a reply still streaming for a task is kept when a command's reply is added", async () => {
  const streaming = { id: "m".repeat(32), role: "assistant", content: "" };
  const { chats } = host({ chat: { runs: [{ id: RUN, state: "running" }], messages: [{ id: "u1", role: "user", content: "plan a trip" }] },
    state: { active: CHAT, active_run: RUN } }); // the host has not stored the streaming reply yet
  const view = mount(App);
  await settle();
  bridge.receive({ event: "token", data: { chat_id: CHAT, id: streaming.id, text: "Day one: " } });
  await settle();
  const box = await type(view.root, "/status");
  await press(box, "Enter");
  const ids = transcript(view.root).map((m) => m.id);
  assert.deepEqual(ids.slice(0, 2), ["u1", streaming.id], "the earlier turn and the streaming reply stay");
  assert.ok(transcript(view.root)[1].text.includes("Day one:"), "with its text so far");
  assert.deepEqual(ids.slice(2), chats[CHAT].messages.slice(1).map((m) => m.id), "then the command and its reply");
  view.app.unmount();
  assert.deepEqual(errors, []);
});

// ---------- window actions: each does what its description says ----------
const withRun = { runs: [{ id: RUN, state: "completed", created: 1, updated: 2 }],
  messages: [{ id: "u1", role: "user", content: "plan a trip" }, { id: "a1", role: "assistant", content: "Here is the plan." }] };
const efforts = { model: { state: "ready", configured: true, name: "ChatGPT", context: 272000, kind: "chatgpt", local: false, provider: "ChatGPT",
  profile_id: "chatgpt", efforts: ["low", "medium", "high"], effort: "medium" } };
async function runAction(name, setup = {}, how = "enter") {
  const fake = host(setup);
  const view = mount(App);
  await settle();
  const box = await type(view.root, "/" + name);
  const row = options(view.root).find((el) => tight(el).startsWith("/" + name) && squash(textOf(el)).endsWith("Action"));
  assert.ok(row, "/" + name + " is listed as a window action: " + options(view.root).map(tight).join(" | "));
  if (how === "enter") await press(box, "Enter");
  else assert.equal(await click(row, view.root), true);
  assert.deepEqual(called("send"), [], "/" + name + " is never sent to the host");
  return { view, box, fake };
}
for (const how of ["enter", "click"]) {
  test(`window actions chosen with ${how === "enter" ? "Enter" : "a click"} do what their descriptions say`, async () => {
    let r = await runAction("details", {}, how);
    assert.ok(drawerEl(r.view.root), "/details shows the details panel");
    await press(await type(r.view.root, "/details"), "Enter");
    assert.ok(!drawerEl(r.view.root), "and hides it again");
    r.view.app.unmount();
    for (const [name, panel, stubs] of [["schedule", "details-panel-tools", ["CompanionWorkflows.vue"]], ["windows", "details-panel-tools", ["ScreenSessions.vue"]],
      ["browser", "details-panel-browser", ["PebbleBrowser.vue"]]]) {
      r = await runAction(name, {}, how);
      assert.equal(panelId(r.view.root), panel, "/" + name + " opens " + panel);
      for (const label of stubs) assert.equal(stubbed(r.view.root, label).length, 1, "/" + name + " shows " + label);
      r.view.app.unmount();
    }
    r = await runAction("rewind", { chat: withRun }, how);
    assert.equal(panelId(r.view.root), "details-panel-changes", "/rewind opens Changes");
    r.view.app.unmount();
    for (const name of ["model", "settings"]) {
      r = await runAction(name, {}, how);
      assert.ok(all(r.view.root).some((el) => el.type === "h2" && el.props.id === "modal-heading" && squash(textOf(el)) === "Settings"), "/" + name + " opens Settings");
      assert.equal(registry["AdvancedSettings.vue"]?.at(-1)?.["initial-tab"], "models", "/" + name + " at Models");
      r.view.app.unmount();
    }
    r = await runAction("new", {}, how);
    assert.equal(called("new_chat").length, 1, "/new starts a new conversation");
    r.view.app.unmount();
    r = await runAction("copy", { chat: withRun }, how);
    assert.deepEqual(copied, ["Here is the plan."], "/copy copies the last reply");
    assert.ok(toast(r.view.root).includes("Copied Pebble's last reply"), toast(r.view.root));
    r.view.app.unmount();
    r = await runAction("effort", { state: efforts }, how);
    assert.equal(focused?.props["aria-label"], "Reasoning effort", "/effort moves to the effort picker");
    r.view.app.unmount();
    r = await runAction("stop", { chat: { runs: [{ id: RUN, state: "running" }] }, state: { active: CHAT, active_run: RUN } }, how);
    assert.deepEqual(called("cancel"), [{ id: CHAT }], "/stop stops the task");
    assert.ok(toast(r.view.root).includes("Stopping the current task"), toast(r.view.root));
    r.view.app.unmount();
    r = await runAction("clear", {}, how);
    assert.equal(r.box.value, "", "/clear empties the box");
    r.view.app.unmount();
    assert.deepEqual(errors, []);
  });
}

// ---------- the drawer's More menu (the person's screenshot, 23:45) ----------
test("each section under the drawer's More menu opens when clicked (WebKit)", async () => {
  host();
  const view = mount(App);
  await settle();
  await press(await type(view.root, "/browser"), "Enter");
  assert.deepEqual(tabs(view.root), ["Browser"], "as in the screenshot: only Browser is listed");
  assert.ok(moreButton(view.root), "and the More menu");
  for (const [label, panel, shown] of [["Goal, plan and agents", "details-panel-task", "GoalPanel.vue"], ["Changes and Git", "details-panel-changes", "CodingWorkflows.vue"],
    ["Tools: schedules and shared windows", "details-panel-tools", "CompanionWorkflows.vue"]]) {
    await click(moreButton(view.root), view.root);
    assert.ok(menuItems(view.root).map((el) => squash(textOf(el))).includes(label), "the menu lists " + label + ": " + menuItems(view.root).map((el) => squash(textOf(el))).join(" | "));
    const item = menuItems(view.root).find((el) => squash(textOf(el)) === label);
    assert.equal(await click(item, view.root), true, label + ": the menu is still open when the click lands");
    assert.equal(panelId(view.root), panel, label + " opens " + panel);
    assert.equal(stubbed(view.root, shown).length, 1, label + " shows " + shown);
    assert.equal(menuItems(view.root).length, 0, "and the menu closes");
    assert.ok(tabs(view.root).length >= 2, "the section stays listed: " + tabs(view.root).join(" | "));
  }
  view.app.unmount();
  assert.deepEqual(errors, []);
});

test("the More menu still closes from its button, a click elsewhere, Tab and Escape (WebKit)", async () => {
  host();
  const view = mount(App);
  await settle();
  await press(await type(view.root, "/browser"), "Enter");
  await click(moreButton(view.root), view.root);
  assert.ok(menuItems(view.root).length, "open");
  assert.equal(focused, menuItems(view.root)[0], "the first item takes focus");
  await click(moreButton(view.root), view.root);
  assert.equal(menuItems(view.root).length, 0, "its button closes it (it does not reopen)");
  assert.equal(focused, moreButton(view.root), "focus returns to the button");
  await click(moreButton(view.root), view.root);
  await click(all(view.root).find((el) => el.props.role === "tabpanel"), view.root);
  assert.equal(menuItems(view.root).length, 0, "a click elsewhere closes it");
  await click(moreButton(view.root), view.root);
  moveFocus(all(view.root).find((el) => el.props["aria-label"] === "Close details")); // Tab to the next control
  await settle();
  assert.equal(menuItems(view.root).length, 0, "Tab away closes it");
  await click(moreButton(view.root), view.root);
  await press(menuItems(view.root)[0], "Escape");
  assert.equal(menuItems(view.root).length, 0, "Escape closes it");
  assert.ok(drawerEl(view.root), "and only the menu, not the drawer");
  view.app.unmount();
  assert.deepEqual(errors, []);
});

// ---------- the helper that adds a command's reply ----------
test("addStoredAfter adds what the host stored after the command, once, in order", () => {
  const { addStoredAfter } = loadTs(new URL("./runEvents.ts", import.meta.url));
  assert.equal(typeof addStoredAfter, "function", "runEvents.ts exports addStoredAfter");
  const chat = { id: CHAT, messages: [{ id: "u1", role: "user", content: "hi" }, { id: "s", role: "assistant", content: "stream" }, { id: "cmd", role: "user", content: "/status" }] };
  const stored = [{ id: "u1", role: "user", content: "hi" }, { id: "cmd", role: "user", content: "/status" }, { id: "rep", role: "assistant", content: "No tasks yet." },
    { id: 7, role: "assistant", content: "bad id" }, { id: "x", role: "system", content: "bad role" }, null];
  assert.equal(addStoredAfter(chat, stored, "cmd"), 1);
  assert.deepEqual(chat.messages.map((m) => m.id), ["u1", "s", "cmd", "rep"], "after the command; the streaming reply kept");
  assert.equal(addStoredAfter(chat, stored, "cmd"), 0, "never twice");
  assert.equal(addStoredAfter(chat, stored, "missing"), 0, "nothing when the host does not have the command");
  assert.equal(addStoredAfter(chat, { messages: [] }, "cmd"), 0, "nothing from a malformed reply");
  const later = { id: CHAT, messages: [{ id: "cmd", role: "user", content: "/status" }, { id: "t", role: "user", content: "typed meanwhile" }] };
  addStoredAfter(later, [{ id: "cmd", role: "user", content: "/status" }, { id: "rep", role: "assistant", content: "No tasks yet." }], "cmd");
  assert.deepEqual(later.messages.map((m) => m.id), ["cmd", "rep", "t"], "right after the command it answers");
});

test("no window error", () => assert.deepEqual(errors, []));
