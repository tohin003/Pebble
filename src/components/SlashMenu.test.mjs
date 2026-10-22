import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Slash menu fixtures (Phase 22): a presentational listbox. Focus stays in the message box (option
// mousedown is prevented), choosing and hovering only emit, catalogue text stays text, window actions
// are tagged, dimmed commands carry their reason, and argument options show without a slash.
const require = createRequire(import.meta.url);
const filename = "./SlashMenu.vue";
const source = readFileSync(new URL(filename, import.meta.url), "utf8");
const descriptor = parse(source, { filename }).descriptor;
const script = compileScript(descriptor, { id: filename });
const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(require, module, module.exports);
  return module.exports;
}
const Menu = evaluate(script.content).default;
Menu.render = evaluate(template.code).render;
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };

ok(!/v-html|innerHTML|\beval\s*\(|new Function|\brpc\b|invoke\(|from "\.\.\/bridge"/.test(source), "presentation only");
ok(/role="listbox" aria-label="Slash commands"/.test(source) && /role="option"/.test(source) && /@mousedown\.prevent/.test(source));
ok(/max-height: calc\(8 \* 33px/.test(source), "eight rows at a time");

function node(type, text = "") { return { type, text, children: [], props: {}, parent: null, scrolled: 0, scrollIntoView() { this.scrolled++; } }; }
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
const textOf = (el) => (el.type === "#comment" ? "" : el.text) + el.children.map(textOf).join("");
async function settle() { for (let i = 0; i < 6; i++) { await Promise.resolve(); await nextTick(); } }

const items = [
  { id: "action:new", name: "new", hint: "", description: "Start a new conversation", kind: "action", action: "new_chat" },
  { id: "host:steer", name: "steer", hint: "text", description: "Queue a human follow-up.", kind: "host", arguments: "text" },
  { id: "host:compact", name: "compact", hint: "[run-id]", description: "Archive older context.", kind: "host", arguments: "[run-id]", disabled: "Available when the task is idle" },
  { id: "host:x", name: "x", hint: "", description: "<img src=x onerror=host()>", kind: "host", arguments: "" },
];
const props = reactive({ items, active: 0, heading: "", listId: "slash-menu", note: "" });
const emitted = [];
const root = node("root");
const app = renderer.createApp({ setup: () => () => h(Menu, { ...props, onChoose: (item) => emitted.push(["choose", item.id]), onHover: (i) => emitted.push(["hover", i]) }) });
const errors = [];
app.config.errorHandler = (e) => errors.push(e);
app.mount(root);
await settle();
const options = () => all(root).filter((el) => el.props.role === "option");
const list = () => all(root).find((el) => el.props.role === "listbox");
const cell = (el, name) => all(el).find((x) => x.props.class === name);
eq([list().props.id, list().props["aria-label"]], ["slash-menu", "Slash commands"]);
eq(options().map((o) => [o.props.id, o.props["aria-selected"]]), [["slash-menu-0", true], ["slash-menu-1", false], ["slash-menu-2", false], ["slash-menu-3", false]], "ids match the box's aria-activedescendant");
eq([textOf(cell(options()[1], "slash-name")), textOf(cell(options()[1], "slash-hint")), textOf(cell(options()[1], "slash-desc"))], ["/steertext", "text", "Queue a human follow-up."], "name, argument hint (spaced by CSS), description");
eq(textOf(cell(options()[0], "slash-tag")), "Action", "window actions are tagged");
ok(!cell(options()[1], "slash-tag"), "service commands are not");
eq([options()[2].props["aria-disabled"], textOf(cell(options()[2], "slash-desc"))], ["true", "Available when the task is idle"], "a dimmed command says why");
ok(/\bdimmed\b/.test(options()[2].props.class), "and is dimmed");
let prevented = 0;
options()[1].props.onMousedown({ preventDefault: () => prevented++ });
eq(prevented, 1, "a mouse press never takes focus from the message box");
options()[1].props.onClick();
eq(emitted.at(-1), ["choose", "host:steer"]);
options()[3].props.onMousemove();
eq(emitted.at(-1), ["hover", 3]);
const count = emitted.length;
options()[0].props.onMousemove();
eq(emitted.length, count, "hovering the active option emits nothing");
props.active = 3;
await settle();
eq(options().map((o) => o.props["aria-selected"]), [false, false, false, true]);
ok(list().children[3].scrolled >= 1, "the active option is scrolled into view");
// Argument options: no slash, a heading naming the command, and nothing chosen until the person moves.
props.items = [{ id: "arg:pdf", name: "pdf", hint: "", description: "", kind: "argument", value: "pdf" }, { id: "arg:csv", name: "csv", hint: "", description: "", kind: "argument", value: "csv" }];
props.active = -1;
props.heading = "/export view-id pdf|docx|xlsx|csv|html";
await settle();
eq(options().map((o) => [textOf(cell(o, "slash-name")), o.props["aria-selected"]]), [["pdf", false], ["csv", false]]);
ok(textOf(root).includes("/export view-id pdf|docx|xlsx|csv|html"));
props.items = [];
props.note = "Service commands are unavailable: Invalid local request.";
await settle();
ok(textOf(root).includes("No matching command.") && !list(), "no empty listbox");
ok(all(root).some((el) => el.props.role === "alert" && textOf(el).includes("unavailable")));
app.unmount();
eq(errors, []);

const html = await renderToString(createSSRApp(Menu, { items, active: 1 }));
ok(html.includes("&lt;img src=x onerror=host()&gt;") && !html.includes("<img"), "descriptions stay text");
ok(html.includes('aria-selected="true"') && html.includes('id="slash-menu-1"') && html.includes('aria-disabled="true"'));
console.log(`Slash menu fixtures passed: ${checks} checks; listbox and option roles with ids for aria-activedescendant, focus kept in the box, actions tagged, dimmed commands with their reason, argument options without a slash, choose and hover only emit, escaped catalogue text.`);
