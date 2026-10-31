import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Compile only checked-in sources; catalogue text is data and is never evaluated.
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
const { component: Palette, source } = compile("./CommandPalette.vue");

// Static boundaries: literal native content, one fixed route, no command list in the source.
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source));
assert.deepEqual([...new Set([...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map((m) => m[1].trim()))], ['"command_catalogue"']);
assert.deepEqual([...source.matchAll(/from "([^"]+)"/g)].map((m) => m[1]).sort(), ["../bridge", "../shortcuts", "vue"]);
const backend = readFileSync(new URL("../../backend/pebble/runtime_commands.py", import.meta.url), "utf8");
const names = [...backend.matchAll(/['"]name['"]:\s*['"]([a-z]+)['"]/g)].map((m) => m[1]);
assert.ok(names.length >= 28 && names.includes("goal") && names.includes("export"), "catalogue names were read from the dispatcher");
for (const name of names) assert.ok(!new RegExp("/" + name + "\\b").test(source), "/" + name + " must come from command_catalogue");
for (const surface of ["./NativeArtifactRenderer.vue", "./ArtifactPanel.vue", "./ModsSettings.vue"])
  assert.ok(!readFileSync(new URL(surface, import.meta.url), "utf8").includes("CommandPalette"), surface + " never renders the palette");

// A small host DOM: real Vue hooks and handlers, no HTML parser.
function node(type, text = "") {
  return { type, text, children: [], props: {}, parent: null, value: "", focused: 0, focus() { this.focused++; }, addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document };
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
const previous = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot };
globalThis.Document = class Document { activeElement = null; };
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
const opener = node("textarea");
globalThis.document.activeElement = opener;
const vueErrors = [];
function mount(props) {
  const root = node("root"), emitted = [];
  let bindings;
  const instrumented = { ...Palette, setup(p, context) { bindings = Palette.setup(p, { ...context, emit: (...args) => emitted.push(args) }); return bindings; } };
  const app = renderer.createApp({ setup: () => () => h(instrumented, props) });
  app.config.errorHandler = (error) => { vueErrors.push(error); };
  app.mount(root);
  return { root, emitted, bindings: () => bindings, unmount: () => app.unmount() };
}
const keyEvent = (key) => { const e = { key, prevented: 0, stopped: 0, preventDefault() { this.prevented++; }, stopPropagation() { this.stopped++; } }; return e; };
const options = (root) => all(root).filter((el) => el.props.role === "option");

const fixture = [
  { name: "help", arguments: "", description: "List implemented runtime commands." },
  { name: "goal", arguments: '["title"|done|pause|resume|abandon]', description: "Start, inspect or settle this conversation's goal." },
  { name: "export", arguments: "view-id pdf|docx", description: "Preview an export plan only." },
  { name: "exports", arguments: "", description: "List exports; nothing is published." },
  { name: "zeta-fixture", arguments: "", description: "<img src=x onerror=host()> fixture command" },
  { name: "Bad", arguments: "", description: "uppercase" },
  { name: "<script>", arguments: "", description: "markup" },
  { name: "help", arguments: "", description: "duplicate" },
  null,
  "text",
  { name: "long", arguments: "", description: "x".repeat(401) },
];
try {
  reply = async (method) => { assert.equal(method, "command_catalogue"); return structuredClone(fixture); };
  const main = mount(reactive({ surface: "main", running: false }));
  await settle();
  const s = main.bindings();
  assert.deepEqual(calls, [{ method: "command_catalogue", params: {} }]);
  assert.equal(opener.focused, 0);
  const input = all(main.root).find((el) => el.type === "input");
  assert.equal(input.focused, 1, "the search field takes focus");
  assert.equal(input.props.role, "combobox");
  assert.equal(input.props["aria-controls"], "command-palette-options");
  assert.deepEqual(s.entries.value.map((e) => e.title), [
    "/help", '/goal ["title"|done|pause|resume|abandon]', "/export view-id pdf|docx", "/exports", "/zeta-fixture",
    // Phase 22: the registry's new main-window entries ("details", "copy_reply") are listed as actions like the others.
    "Show task activity", "Show or hide details", "Copy the last reply", "New conversation", "Open settings",
  ]);
  assert.ok(!s.entries.value.some((e) => /status|palette|Stop/.test(e.title)), "no hardcoded command; stop only while running; no self entry");
  const text = textOf(main.root);
  assert.ok(text.includes("6 catalogue entries were malformed or duplicated and are not shown."));
  assert.ok(text.includes("5 commands from this Pebble service. Enter puts a command in the message box; nothing runs until you send it."));
  assert.ok(text.includes("⌘J") && text.includes("⌘N") && text.includes("⌘,"));
  assert.ok(text.includes("<img src=x onerror=host()> fixture command"), "descriptions stay literal text nodes");
  assert.equal(options(main.root)[0].props["aria-selected"], true);
  assert.equal(input.props["aria-activedescendant"], "command-palette-0");

  s.query.value = "exp"; await settle();
  assert.deepEqual(s.entries.value.map((e) => e.title), ["/export view-id pdf|docx", "/exports"]);
  assert.deepEqual(options(main.root).map((el) => textOf(el)), ["/export view-id pdf|docxPreview an export plan only.", "/exportsList exports; nothing is published."]);
  s.query.value = "/go"; await settle();
  assert.equal(s.entries.value[0].title, '/goal ["title"|done|pause|resume|abandon]');
  s.query.value = "published"; await settle();
  assert.deepEqual(s.entries.value.map((e) => e.title), ["/exports"]);
  s.query.value = "nothing-like-this"; await settle();
  assert.ok(textOf(main.root).includes("No matching command or action."));
  s.query.value = ""; await settle();

  // Wrapping upward lands on the last entry, index 9 since Phase 22 added "details" and "copy_reply" (was 7).
  for (const [key, index] of [["ArrowDown", 1], ["ArrowDown", 2], ["ArrowUp", 1], ["ArrowUp", 0], ["ArrowUp", 9]]) {
    const e = keyEvent(key);
    input.props.onKeydown(e);
    assert.equal(e.prevented, 1);
    assert.equal(s.index.value, index, key);
  }
  input.props.onKeydown(keyEvent("Enter"));
  assert.deepEqual(main.emitted.at(-1), ["action", "settings"]);
  s.index.value = 1;
  input.props.onKeydown(keyEvent("Enter"));
  assert.deepEqual(main.emitted.at(-1), ["choose", "/goal "], "commands with arguments leave room to type them");
  s.index.value = 0; await settle();
  options(main.root)[3].props.onMousedown({ preventDefault() {}, stopPropagation() {} });
  assert.deepEqual(main.emitted.at(-1), ["choose", "/exports"]);
  input.props.onKeydown({ ...keyEvent("Enter"), isComposing: true });
  assert.equal(main.emitted.length, 3, "IME composition never chooses");
  const tab = keyEvent("Tab");
  input.props.onKeydown(tab);
  assert.equal(tab.prevented, 1);
  const escape = keyEvent("Escape");
  input.props.onKeydown(escape);
  assert.deepEqual(main.emitted.at(-1), ["close"]);
  assert.equal(escape.stopped, 1, "Escape closes only the palette, not the panel behind it");
  assert.ok(!main.emitted.some(([name]) => !["choose", "action", "close"].includes(name)));
  main.unmount();
  assert.equal(opener.focused, 1, "focus returns to where it was");

  // Pet surface: only pet shortcuts; Stop appears while a task runs.
  calls.length = 0;
  const pet = mount(reactive({ surface: "pet", running: true }));
  await settle();
  assert.deepEqual(pet.bindings().entries.value.filter((e) => e.action).map((e) => [e.title, e.keys]), [["Stop the current task", "⌘."], ["Show task activity", "⌘J"]]);
  pet.unmount();

  // A refused or malformed catalogue shows the reason and no fallback commands.
  for (const failure of [async () => { throw new Error("Invalid local request."); }, async () => ({ help: "not a list" })]) {
    reply = failure;
    const failed = mount(reactive({ surface: "main" }));
    await settle();
    assert.deepEqual(failed.bindings().entries.value.map((e) => e.action || e.title), ["activity", "details", "copy_reply", "new_chat", "settings"]);
    assert.ok(all(failed.root).some((el) => el.props.role === "alert" && textOf(el).startsWith("Commands are unavailable: ")));
    assert.ok(textOf(failed.root).includes("0 commands from this Pebble service."));
    failed.unmount();
  }

  // A late reply after closing changes nothing and raises nothing.
  let release;
  reply = () => new Promise((resolve) => (release = resolve));
  const late = mount(reactive({ surface: "main" }));
  await settle();
  late.unmount();
  release(structuredClone(fixture));
  await settle();
  assert.equal(late.bindings().catalogue.value.length, 0);

  // Phase 45 (F024): the window's own launcher entries come first and run at once; with no words only the recent
  // conversations show; slash commands still fill the message box.
  reply = async () => structuredClone(fixture);
  const extras = [
    { key: "theme-dark", title: "Dark theme", detail: "Appearance", run: "theme:dark", words: "appearance mode" },
    { key: "stealth", title: "Turn Stealth Lockdown on", detail: "Hidden from screen capture", run: "stealth", words: "privacy" },
    { key: "chat-a", title: "Trip plan", detail: "Conversation", run: "chat:a", words: "conversation", recent: true },
    { key: "chat-b", title: "Old recipes", detail: "Conversation", run: "chat:b", words: "conversation", recent: false },
  ];
  const launcher = mount(reactive({ surface: "main", running: false, extras }));
  await settle();
  const l = launcher.bindings();
  assert.deepEqual(l.entries.value.slice(0, 3).map((e) => e.title), ["Dark theme", "Turn Stealth Lockdown on", "Trip plan"]);
  assert.ok(!l.entries.value.some((e) => e.title === "Old recipes"), "older conversations wait for words");
  assert.ok(textOf(launcher.root).includes("Actions run at once."));
  l.query.value = "dark"; await settle();
  assert.equal(l.entries.value[0].title, "Dark theme");
  all(launcher.root).find((el) => el.type === "input").props.onKeydown(keyEvent("Enter"));
  assert.deepEqual(launcher.emitted.at(-1), ["run", "theme:dark"]);
  l.query.value = "recipes"; await settle();
  assert.equal(l.entries.value[0].title, "Old recipes", "every conversation is found by its words");
  l.query.value = "exp"; await settle();
  assert.equal(l.entries.value[0].title, "/export view-id pdf|docx", "commands still rank by their names");
  launcher.unmount();

  // Server rendering escapes catalogue text.
  const seeded = { ...Palette, setup(p, context) { const b = Palette.setup(p, context); b.catalogue.value = fixture.slice(0, 5); b.loading.value = false; return b; } };
  const html = await renderToString(createSSRApp(seeded, { surface: "main" }));
  assert.ok(html.includes("&lt;img src=x onerror=host()&gt; fixture command"));
  assert.ok(!/<(img|script|iframe)\b/.test(html));
  assert.ok(html.includes('role="dialog"') && html.includes('aria-modal="true"') && html.includes('role="listbox"'));
  assert.deepEqual(vueErrors, []);
  console.log("Command palette fixtures passed: catalogue-only commands, malformed and duplicate entries omitted with a count, ranking, keyboard and IME handling, registry actions per surface, refusal without fallback, late replies, focus return and escaping.");
} finally {
  Object.assign(globalThis, previous);
}
