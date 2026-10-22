import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, defineComponent, h, nextTick } from "vue";
import { transform } from "sucrase";

// Phase 24 wave-C/D merge (m24c). V1-002 (P0) end to end: the workspace's "Review…" on the on-device notice opens the
// REAL reviewed handoff (ModelHandoff.vue with release ['device']), and the withheld on-device messages are included
// only after the person approves that exact plan and starts it. The host is a recording fake that "sends" a plan's
// messages to the cloud only on model_handoff_start, as App.model_handoff_start does; no service, network or model.
// Also: About's "What's new" lines are CHANGELOG.md's 1.0.0 highlights (V1-135, t24-ux-shell and t24-publish).
const require = createRequire(import.meta.url);
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const vue = require("vue");

// ---------- a fake DOM (as Phase24Shell.test.mjs) ----------
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
const textOf = (el) => (el.type === "#comment" ? "" : el.type === "#text" ? el.text : [el.text, ...el.children.map(textOf)].join(" "));
const squash = (text) => text.replace(/\s+/g, " ").trim();
const nameOf = (el) => el.props["aria-label"] ?? squash(textOf(el));
const button = (root, name) => {
  const found = all(root).filter((el) => el.type === "button" && nameOf(el) === name);
  assert.ok(found.length, "missing button " + JSON.stringify(name) + " in: " + all(root).filter((el) => el.type === "button").map(nameOf).join(" | "));
  return found[0];
};
async function settle() { for (let i = 0; i < 25; i++) { await Promise.resolve(); await nextTick(); } }

const store = new Map();
const localStorageStub = {
  get length() { return store.size; },
  key: (i) => [...store.keys()][i] ?? null,
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => void store.set(k, String(v)),
  removeItem: (k) => void store.delete(k),
};
const fakeTimers = { setTimeout: () => 0, clearTimeout: () => {} };
globalThis.window = { localStorage: localStorageStub, innerWidth: 1300, devicePixelRatio: 2, addEventListener() {}, removeEventListener() {}, ...fakeTimers };
globalThis.localStorage = localStorageStub;
globalThis.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
globalThis.Document = class Document {
  documentElement = { dataset: {}, classList: { toggle() {}, add() {} } };
  hidden = false;
  get activeElement() { return focused; }
  addEventListener() {}
  removeEventListener() {}
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
  native: true, RpcError, calls: [], handlers: {}, receive: null,
  async rpc(method, params = {}) {
    bridge.calls.push([method, structuredClone(params)]);
    const handler = bridge.handlers[method];
    if (!handler) throw new Error("Unknown request.");
    return structuredClone(await handler(params));
  },
  async attachBytes() { throw new Error("not used"); },
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
function loadSfc(path, overrides = {}, source = read(path)) {
  const filename = path;
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
    if (name === "@tauri-apps/api/core") return { invoke: async () => undefined };
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

// ---------- the fake host: one conversation that ran on this Mac, then on ChatGPT ----------
const CHAT = "c".repeat(32), DEVICE_RUN = "s".repeat(32), CLOUD_RUN = "n".repeat(32);
const CLOUD = "cloud:https://chatgpt.com";
const CANARY = "locker-7Q4Z";
const MESSAGES = [
  { id: "m1", role: "user", content: "My locker code is " + CANARY + ".", route: "device" },
  { id: "m2", role: "assistant", content: "Noted " + CANARY + ".", route: "device" },
  { id: "m3", role: "user", content: "Summarise what we discussed.", route: CLOUD },
  { id: "m4", role: "assistant", content: "Done.", route: CLOUD },
];
function hostState() {
  return {
    chats: [{ id: CHAT, title: "Locker", workspace: null }],
    model: { state: "ready", configured: true, name: "gpt-6-luna", path: "", context: 272000, kind: "chatgpt", local: false, provider: "ChatGPT", profile_id: "chatgpt" },
    profiles: [{ id: "default-local", name: "On this Mac", kind: "gguf", context: 4096, tools: true }, { id: "chatgpt", name: "ChatGPT", kind: "chatgpt", model: "gpt-6-luna", context: 272000, tools: true, local: false }],
    guard: { available: true, error: null }, active: null, active_run: null, runs: [],
    settings: { theme: "system", reduced_motion: false, memory_enabled: true, permissions: { mode: "ask", tools: { files: true, edits: true, tests: true, apps: true, web: true, terminal: true, plugins: true, views: true, screen: false } },
      companion: { pointer: true, reactions: true, status_words: "rotate", ui_guidance: true } },
    data_dir: "/x", platform: "darwin", version: "1.0.0",
  };
}
const runs = [{ id: CLOUD_RUN, state: "completed", created: 2, updated: 3 }, { id: DEVICE_RUN, state: "completed", created: 1, updated: 1 }];
const chatOf = (id) => ({ id, title: id === CHAT ? "Locker" : "Handoff: Locker", workspace: null, messages: id === CHAT ? MESSAGES.map(({ route: _, ...m }) => m) : [],
  actions: [], artifacts: [], runs: id === CHAT ? runs : [], questions: [], plan: null });
function host() {
  bridge.calls = []; bridge.receive = null;
  for (const key of Object.keys(registry)) delete registry[key];
  const cloud = { sent: [], plans: new Map(), approvals: new Set() };
  bridge.handlers = {
    state: () => hostState(),
    get_chat: ({ id }) => chatOf(id),
    new_chat: () => chatOf("e".repeat(32)),
    job_list: () => ({ jobs: [], notifications: [] }),
    screen_sessions: () => [],
    send: () => { throw new Error("the composer must not send here"); },
    // As App.model_handoff_preview: the plan carries the destination's turns plus the classes the person releases;
    // the rest is withheld. Nothing is sent by previewing.
    model_handoff_preview: ({ id, run_id, profile_id, release }) => {
      assert.equal(id, CHAT);
      const allowed = new Set([CLOUD, ...(Array.isArray(release) ? release : [])]);
      const items = MESSAGES.filter((m) => allowed.has(m.route));
      const fingerprint = "sha256:" + [run_id, profile_id, ...items.map((m) => m.id)].join("-");
      cloud.plans.set(fingerprint, items);
      const counts = {};
      for (const m of items) (counts[m.role] ||= {})[m.route] = (counts[m.role][m.route] || 0) + 1;
      return { plan_fingerprint: fingerprint, source_class: "device", destination_class: CLOUD, destination_fingerprint_prefix: "sha256:0123456789ab",
        counts, withheld_ids: MESSAGES.filter((m) => !allowed.has(m.route)).map((m) => m.id),
        released_classes: [...allowed].filter((c) => c !== CLOUD && MESSAGES.some((m) => m.route === c)), authorization: "none" };
    },
    model_handoff_approve: ({ plan_fingerprint }) => {
      assert.ok(cloud.plans.has(plan_fingerprint), "an approval names a reviewed plan");
      cloud.approvals.add(plan_fingerprint);
      return { approved: true, plan_fingerprint, authorization: "none" };
    },
    // As App.model_handoff_start: one start per approval, and only then does the plan reach the cloud route.
    model_handoff_start: ({ plan_fingerprint }) => {
      if (!cloud.approvals.delete(plan_fingerprint)) throw new Error("The reviewed model handoff was refused (not_approved). Nothing was sent.");
      cloud.sent.push(...cloud.plans.get(plan_fingerprint).map((m) => m.content));
      return { run_id: "h".repeat(32), chat_id: "f".repeat(32), authorization: "none" };
    },
  };
  return cloud;
}
const NOTICE = "Earlier on-device messages were not shared with ChatGPT. Review to include them.";
const withheldEvent = { event: "progress", data: { chat_id: CHAT, run_id: CLOUD_RUN, label: NOTICE, kind: "note", activity: "note",
  handoff: { source_run_id: DEVICE_RUN, profile_id: "chatgpt", withheld: 2, actions: 0 } } };

const HANDOFF = "./components/ModelHandoff.vue";
/** The V1-002 scenario against a given ModelHandoff source (the merged file, or another for a comparison). */
export async function reviewIncludesDeviceMessagesOnlyAfterConfirmation(handoffSource = read(HANDOFF)) {
  const Handoff = loadSfc(HANDOFF, {}, handoffSource);
  const App = loadSfc("./App.vue", { "./components/DetailsDrawer.vue": drawerModule, [HANDOFF]: { __esModule: true, default: Handoff } });
  const cloud = host();
  const view = mount(App);
  await settle();
  const picker = () => registry["ModelPicker.vue"].at(-1);
  bridge.receive(withheldEvent);
  await settle();
  assert.equal(picker().notice, NOTICE, "the host's notice is shown");
  const page = () => squash(textOf(view.root));

  // Review… then Not now: nothing is approved, started or sent.
  picker().onReview();
  await settle();
  assert.deepEqual(called("model_handoff_preview").at(-1), { id: CHAT, run_id: DEVICE_RUN, profile_id: "chatgpt", release: ["device"] },
    "Review asks the handoff to include the on-device messages");
  button(view.root, "Not now").props.onClick();
  await settle();
  assert.ok(!page().includes("Continue on another model"), "Not now closes the handoff");
  assert.deepEqual([called("model_handoff_approve"), called("model_handoff_start"), cloud.sent], [[], [], []]);

  // Review… again: the plan lists the on-device messages as released, and still nothing has been sent.
  picker().onReview();
  await settle();
  assert.equal(registry["DetailsDrawer.vue"].at(-1).tab, "task", "the handoff opens in Details › Task");
  assert.ok(page().includes("Released for this handoff, so sent although it came from another route class: This device."), page().slice(-900));
  assert.ok(page().includes("No message is withheld.") && page().includes("Messages that will be sent: 4"));
  assert.deepEqual(cloud.sent, [], "previewing sends nothing");
  assert.equal(button(view.root, "Start the new run").props.disabled, true, "a start needs the person's approval first");
  await button(view.root, "Start the new run").props.onClick();
  await settle();
  assert.deepEqual([called("model_handoff_start"), cloud.sent], [[], []], "no start without an approval");

  // The person approves this exact plan: still nothing sent.
  await button(view.root, "Approve this handoff").props.onClick();
  await settle();
  assert.ok(page().includes("Approved. Nothing has been sent yet."));
  assert.deepEqual(cloud.sent, [], "approving sends nothing");

  // The person starts it: only now do the withheld on-device messages reach the cloud route.
  await button(view.root, "Start the new run").props.onClick();
  await settle();
  assert.equal(called("model_handoff_start").length, 1);
  assert.ok(cloud.sent.some((text) => text.includes(CANARY)), "the released on-device messages are sent after the start");
  assert.deepEqual(cloud.sent, MESSAGES.map((m) => m.content));
  assert.deepEqual(called("send"), [], "the composer sent nothing");
  view.app.unmount();
  assert.deepEqual(errors, []);
}

test("Review… on the on-device notice includes the withheld messages only after the person approves and starts the handoff (V1-002)",
  () => reviewIncludesDeviceMessagesOnlyAfterConfirmation());

test("without the notice's release the same handoff keeps the on-device messages out (V1-002 control)", async () => {
  const Handoff = loadSfc(HANDOFF);
  const cloud = host();
  const view = mount(Handoff, { chatId: CHAT, runId: DEVICE_RUN, profileId: "chatgpt" });
  await settle();
  assert.deepEqual(called("model_handoff_preview"), [{ id: CHAT, run_id: DEVICE_RUN, profile_id: "chatgpt" }], "the exact Phase 15 parameters");
  assert.ok(squash(textOf(view.root)).includes("Withheld: 2 messages from another route class. They are not sent."));
  await button(view.root, "Approve this handoff").props.onClick();
  await settle();
  await button(view.root, "Start the new run").props.onClick();
  await settle();
  assert.deepEqual(cloud.sent, ["Summarise what we discussed.", "Done."], "nothing from this Mac");
  view.app.unmount();
  assert.deepEqual(errors, []);
});

test("About's What's new lines are CHANGELOG.md's 1.0.0 highlights, in order (V1-135)", () => {
  const app = read("./App.vue");
  const list = app.match(/const WHATS_NEW = Object\.freeze\(\[([\s\S]*?)\]\);/)?.[1];
  assert.ok(list, "App.vue keeps WHATS_NEW");
  const lines = [...list.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => JSON.parse('"' + m[1] + '"'));
  const changelog = read("../CHANGELOG.md");
  const start = changelog.search(/^## \[1\.0\.0\]/m);
  assert.ok(start >= 0, "CHANGELOG.md has a 1.0.0 section");
  const section = changelog.slice(start, changelog.indexOf("\n## ", start + 1));
  const highlights = section.slice(section.indexOf("### Highlights"), section.indexOf("\n### ", section.indexOf("### Highlights") + 1));
  const bullets = [...highlights.matchAll(/^- (.+)$/gm)].map((m) => m[1].trim());
  assert.equal(lines.length, 7);
  assert.deepEqual(bullets, lines);
});
