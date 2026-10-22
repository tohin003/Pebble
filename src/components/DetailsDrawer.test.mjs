import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Details drawer fixtures (Phase 22): chrome only (no route), WAI-ARIA tabs with roving focus,
// only the selected tab mounted, a keyboard and pointer resizable edge within bounds, Escape to
// close, and remembered state that survives broken storage. Only checked-in sources are compiled.
const require = createRequire(import.meta.url);
const filename = "./DetailsDrawer.vue";
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
const mod = evaluate(script.content);
const Drawer = mod.default;
Drawer.render = evaluate(template.code).render;
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };

// ---- static ----
ok(!/v-html|innerHTML|\beval\s*\(|new Function|<component\s+:is|\brpc\b|invoke\(|from "\.\.\/bridge"/.test(source), "chrome only: no route, no markup sink");
ok(/role="tablist" aria-label="Details sections"/.test(source) && /role="tab"/.test(source) && /role="tabpanel"/.test(source));
ok(/role="separator"/.test(source) && /aria-label="Resize details"/.test(source) && /aria-label="Close details"/.test(source) && /aria-label="Details"/.test(source));
eq([mod.DRAWER.width, mod.DRAWER.min, mod.DRAWER.max], [360, 320, 520], "the design study's width: 360 px, resizable 320–520 px");

// ---- remembered state ----
const { DRAWER, clampWidth, readDrawer, writeDrawer } = mod;
const tabs = ["activity", "task", "browser"];
const store = (value) => ({ getItem: () => value });
eq(readDrawer(store(JSON.stringify({ open: true, tab: "browser", width: 500 })), "k", tabs), { open: true, tab: "browser", width: 500 });
eq(readDrawer(store(JSON.stringify({ open: "yes", tab: "__proto__", width: 99999 })), "k", tabs), { open: false, tab: "activity", width: DRAWER.max });
eq(readDrawer(store("{not json"), "k", tabs), { open: false, tab: "activity", width: DRAWER.width });
eq(readDrawer(store("x".repeat(500)), "k", tabs).width, DRAWER.width, "oversized values are ignored");
eq(readDrawer({ getItem() { throw new Error("SecurityError"); } }, "k", tabs).open, false, "a refusing storage keeps defaults");
eq(readDrawer(null, "k", tabs).tab, "activity");
const written = [];
ok(writeDrawer({ setItem: (k, v) => written.push([k, JSON.parse(v)]) }, "pebble.details.main", { open: true, tab: "task", width: 10 }));
eq(written, [["pebble.details.main", { open: true, tab: "task", width: DRAWER.min }]]);
eq(writeDrawer({ setItem() { throw new Error("QuotaExceededError"); } }, "k", { open: true, tab: "task", width: 400 }), false);
eq(writeDrawer(null, "k", { open: true, tab: "task", width: 400 }), false);
eq([clampWidth(10), clampWidth(5000), clampWidth(Number.NaN), clampWidth("400"), clampWidth(600, 800)], [DRAWER.min, DRAWER.max, DRAWER.width, DRAWER.width, 480]);

// ---- a small DOM ----
function node(type, text = "") {
  return { type, text, children: [], props: {}, parent: null, focused: 0, focus() { this.focused++; } };
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
const textOf = (el) => (el.type === "#comment" ? "" : el.text) + el.children.map(textOf).join("");
async function settle() { for (let i = 0; i < 6; i++) { await Promise.resolve(); await nextTick(); } }
const key = (k) => ({ key: k, prevented: 0, preventDefault() { this.prevented++; } });

const emitted = [];
const mounted = [];
const props = reactive({
  tabs: [{ id: "activity", label: "Activity", badge: 2 }, { id: "task", label: "Task" }, { id: "browser", label: "Browser" }],
  tab: "task", width: 384, overlay: false, viewport: 1400,
});
const slot = (name) => () => { mounted.push(name); return h("div", { "data-slot": name }, name + " content"); };
const root = node("root");
const vueErrors = [];
const app = renderer.createApp({
  setup: () => () => h(Drawer, {
    ...props,
    "onUpdate:tab": (id) => { emitted.push(["tab", id]); props.tab = id; },
    "onUpdate:width": (w) => { emitted.push(["width", w]); props.width = w; },
    onClose: () => emitted.push(["close"]),
  }, { activity: slot("activity"), task: slot("task"), browser: slot("browser") }),
});
app.config.errorHandler = (e) => vueErrors.push(e);
app.mount(root);
await settle();
const tabEls = () => all(root).filter((el) => el.props.role === "tab");
const panel = () => all(root).find((el) => el.props.role === "tabpanel");
eq(tabEls().map((t) => [t.props.id, t.props["aria-selected"], t.props.tabindex, t.props["aria-controls"]]), [
  ["details-tab-activity", false, -1, "details-panel-activity"], ["details-tab-task", true, 0, "details-panel-task"], ["details-tab-browser", false, -1, "details-panel-browser"],
], "roving tabindex on the selected tab");
eq(textOf(tabEls()[0]).replace(/\s+/g, " ").trim(), "Activity2, 2 waiting", "the badge is read as words");
eq([panel().props.id, panel().props["aria-labelledby"], panel().props.tabindex], ["details-panel-task", "details-tab-task", "0"]);
eq(mounted, ["task"], "only the selected tab's content is mounted");
// Click and keyboard switching.
tabEls()[2].props.onClick();
await settle();
eq(emitted.at(-1), ["tab", "browser"]);
eq(all(root).filter((el) => el.props["data-slot"]).map((el) => el.props["data-slot"]), ["browser"], "the previous tab unmounted");
let e = key("ArrowRight");
tabEls()[2].props.onKeydown(e);
await settle();
eq([emitted.at(-1), e.prevented], [["tab", "activity"], 1], "arrows wrap");
tabEls()[0].props.onKeydown(key("End"));
await settle();
eq(emitted.at(-1), ["tab", "browser"]);
ok(tabEls()[2].focused >= 1, "keyboard selection moves focus to the tab");
tabEls()[2].props.onKeydown(key("Home"));
await settle();
eq(emitted.at(-1), ["tab", "activity"]);
const before = emitted.length;
tabEls()[0].props.onKeydown(key("x"));
eq(emitted.length, before, "other keys do nothing");
// Close: the button and Escape.
all(root).find((el) => el.props["aria-label"] === "Close details").props.onClick();
eq(emitted.at(-1), ["close"]);
const aside = all(root).find((el) => el.type === "aside");
eq([aside.props.id, aside.props["aria-label"], aside.props.style.width], ["details-drawer", "Details", "384px"]);
const esc = { key: "Escape", stopped: 0, stopPropagation() { this.stopped++; } };
aside.props.onKeydown(esc);
eq([emitted.at(-1), esc.stopped], [["close"], 1], "Escape closes the drawer, not the dialog behind it");
// Resizing by keyboard and pointer, within bounds.
const edge = () => all(root).find((el) => el.props.role === "separator");
eq([edge().props["aria-valuenow"], edge().props["aria-valuemin"], edge().props["aria-valuemax"], edge().props.tabindex], [384, DRAWER.min, DRAWER.max, "0"]);
edge().props.onKeydown(key("ArrowLeft"));
await settle();
eq(emitted.at(-1), ["width", 400], "left widens by one step");
edge().props.onKeydown(key("ArrowRight"));
await settle();
eq(emitted.at(-1), ["width", 384]);
edge().props.onKeydown(key("Home"));
await settle();
eq(emitted.at(-1), ["width", DRAWER.min]);
edge().props.onKeydown(key("End"));
await settle();
eq(emitted.at(-1), ["width", DRAWER.max], "End reaches the bound (the viewport allows it)");
props.width = 384;
await settle();
const captured = [];
const target = { setPointerCapture: (id) => captured.push(["set", id]), releasePointerCapture: (id) => captured.push(["release", id]) };
edge().props.onPointerdown({ button: 0, clientX: 1000, pointerId: 7, currentTarget: target, preventDefault() {} });
edge().props.onPointermove({ clientX: 900, pointerId: 7 });
eq(emitted.at(-1), ["width", 484], "dragging left by 100 px widens by 100 px");
edge().props.onPointermove({ clientX: 100, pointerId: 7 });
eq(emitted.at(-1), ["width", DRAWER.max], "never past the bound");
edge().props.onPointermove({ clientX: 50, pointerId: 8 });
eq(emitted.at(-1), ["width", DRAWER.max], "another pointer is ignored");
edge().props.onPointerup({ pointerId: 7 });
eq(captured, [["set", 7], ["release", 7]]);
const n = emitted.length;
edge().props.onPointermove({ clientX: 10, pointerId: 7 });
eq(emitted.length, n, "no resizing after release");
// Overlay (narrow windows): no edge, and never wider than the window.
props.overlay = true;
props.viewport = 500;
props.width = 700;
await settle();
ok(!edge(), "an overlay has no resize edge");
eq(all(root).find((el) => el.type === "aside").props.style.width, "476px");
app.unmount();
eq(vueErrors, []);

// ---- Empty tabs are not passed: "More" reveals them, and with none the empty state is the panel ----
{
  const p2 = reactive({ tabs: [], tab: "", more: [{ id: "task", label: "Goal and plan" }, { id: "tools", label: "Schedules, windows and browser" }] });
  const got = [];
  const root2 = node("root");
  const app2 = renderer.createApp({
    setup: () => () => h(Drawer, { ...p2, onReveal: (id) => got.push(["reveal", id]), "onUpdate:tab": (id) => got.push(["tab", id]), onClose: () => got.push(["close"]) },
      { empty: () => h("p", { "data-slot": "empty" }, "Nothing to review yet."), task: () => h("p", "Goal panel") }),
  });
  app2.config.errorHandler = (e) => vueErrors.push(e);
  app2.mount(root2);
  await settle();
  ok(textOf(root2).includes("Nothing to review yet.") && !all(root2).some((el) => el.props.role === "tabpanel" || el.props.role === "tablist"), "no tab, no empty tablist or tabpanel: the empty state");
  const more = all(root2).find((el) => el.props["aria-label"] === "More sections" && el.type === "button");
  eq([more.props["aria-haspopup"], more.props["aria-expanded"]], ["menu", false]);
  more.props.onClick();
  await settle();
  const items = () => all(root2).filter((el) => el.props.role === "menuitem");
  eq(items().map((el) => textOf(el).trim()), ["Goal and plan", "Schedules, windows and browser"]);
  ok(items()[0].focused >= 1, "the first item takes focus");
  items()[0].props.onKeydown(key("ArrowDown"));
  ok(items()[1].focused >= 1, "arrows move between items");
  const escape = { key: "Escape", prevented: 0, stopped: 0, preventDefault() { this.prevented++; }, stopPropagation() { this.stopped++; } };
  items()[1].props.onKeydown(escape);
  await settle();
  eq([items().length, escape.stopped, got.length], [0, 1, 0], "Escape closes only the menu");
  more.props.onClick();
  await settle();
  items()[1].props.onClick();
  await settle();
  eq(got, [["reveal", "tools"], ["tab", "tools"]], "choosing reveals that section");
  app2.unmount();
  eq(vueErrors, []);
}

// ---- SSR ----
const html = await renderToString(createSSRApp({ render: () => h(Drawer, { tabs: [{ id: "task", label: "Task <b>" }], tab: "missing" }, { task: () => h("p", "Goal") }) }));
ok(html.includes('aria-label="Details"') && html.includes('role="tabpanel"') && html.includes("Goal"), "an unknown tab falls back to the first");
ok(html.includes("Task &lt;b&gt;") && !html.includes("<b>"), "labels are text");
console.log(`Details drawer fixtures passed: ${checks} checks; chrome only, roving-focus tabs with arrow/Home/End, only the selected tab mounted, Escape and button close, keyboard and pointer resize within bounds, overlay without an edge, remembered state through broken storage.`);
