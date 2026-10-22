import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, defineComponent, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Compile only checked-in sources; demo text is data and is never evaluated. The
// prototype has no bridge: a request for "../bridge" fails the fixture.
const require = createRequire(import.meta.url);
const icon = defineComponent({ props: { size: Number, weight: String }, setup: () => () => h("i") });
const icons = new Proxy({ __esModule: true }, { get: (target, name) => (name in target ? target[name] : icon), has: () => true });
function evaluate(code) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(
    (name) => {
      if (name === "../bridge") throw new Error("The prototype renderer must not reach the bridge.");
      return name === "@phosphor-icons/vue" ? icons : require(name);
    },
    module,
    module.exports,
  );
  return module.exports;
}
const filename = "./PrototypeRenderer.vue";
const source = readFileSync(new URL(filename, import.meta.url), "utf8");
const descriptor = parse(source, { filename }).descriptor;
const script = compileScript(descriptor, { id: filename });
const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
const Prototype = evaluate(script.content).default;
Prototype.render = evaluate(template.code).render;

// Static boundary: no bridge, links, frames or forms; classes come from host tables only.
const markup = descriptor.template.content;
assert.ok(!/from "\.\.\/bridge"|\brpc\(|v-html|innerHTML|\beval\s*\(|new Function|<iframe|srcdoc|<form\b|<a\b|localStorage/.test(source));
assert.ok(!/:(href|src|style|srcdoc|action|formaction)\b|v-bind="|v-on="|@\[|:is=/.test(markup));
assert.deepEqual([...markup.matchAll(/:class="([^"]+)"/g)].map((m) => m[1]), ["frame", "{ inert: !component.goto }", "aspect(component.aspect)"]);
assert.deepEqual([...markup.matchAll(/@click="([^"]+)"/g)].map((m) => m[1]).sort(), ["back", "feedback", "flip(key)", "go(component.goto)", "go(link.goto)", "restart"]);
assert.equal((markup.match(/type="button"/g) || []).length, (markup.match(/<button\b/g) || []).length, "no button can submit anything");

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
const button = (root, label) => {
  const found = all(root).filter((el) => el.type === "button" && textOf(el).trim() === label);
  assert.equal(found.length, 1, "one button: " + label);
  return found[0];
};
const device = (root) => all(root).find((el) => el.props.role === "region");
async function settle() { for (let i = 0; i < 6; i++) { await Promise.resolve(); await nextTick(); } }
const vueErrors = [];

const hostile = "<img src=x onerror=host()>";
const demo = () => ({
  id: "demo-1", chat_id: "chat-a", title: "Sign-in flow " + hostile, kind: "prototype", revision: 2, status: "ready", owner_run_id: "run-1", created: 1,
  data: {
    viewport: "phone", start: "home", theme: { accent: "violet", density: "compact", radius: 12 },
    screens: [
      { id: "home", title: "Home", components: [
        { type: "heading", text: "Welcome back " + hostile },
        { type: "button", label: "Sign in", goto: "login" },
        { type: "button", label: "Learn more" },
        { type: "card", title: "Today", body: "javascript:alert(1)" },
        { type: "image_placeholder", label: "Hero", aspect: "1:1" },
        { type: "nav", items: [{ label: "Home", goto: "home" }, { label: "Settings", goto: "settings" }] },
      ] },
      { id: "login", title: "Sign in", components: [
        { type: "input", label: "Email", placeholder: "you@example.invalid" },
        { type: "toggle", label: "Remember me" },
        { type: "button", label: "Approve", goto: "home" },
      ] },
      { id: "settings", title: "Settings", components: [
        { type: "list", items: ["Notifications", "Privacy " + hostile] },
        { type: "image_placeholder", label: "Avatar", aspect: "1:1;x" },
      ] },
    ],
  },
});

// Server rendering: banner, host theme classes, escaped text, no links or images.
let html = await renderToString(createSSRApp(Prototype, { artifact: demo() }));
assert.ok(html.includes("Interactive mock-up · controls only navigate this demo"));
assert.ok(html.includes('class="proto-device frame-phone accent-violet density-compact radius-12"'));
assert.ok(html.includes("Welcome back &lt;img src=x onerror=host()&gt;") && html.includes("javascript:alert(1)"));
assert.ok(!/<(a|img|iframe|form|script|object|embed)\b|\s(href|src|style|srcdoc|action|on\w+)="/.test(html), "no link, image, frame, form, handler or inline style");
assert.ok(html.includes("proto-image aspect-1-1") && html.includes("Send feedback"));
// Unknown or hostile theme tokens fall back to host defaults and never reach a class.
for (const [theme, viewport, expected] of [
  [{ accent: "red;x:url(y)", density: "}body{", radius: 7 }, "javascript:x", "frame-phone accent-teal density-cozy radius-8"],
  [{ accent: "constructor", density: "__proto__", radius: 1e9 }, "toString", "frame-phone accent-teal density-cozy radius-8"],
  [{ accent: "amber", density: "comfortable", radius: 0 }, "desktop", "frame-desktop accent-amber density-comfortable radius-0"],
  [undefined, "tablet", "frame-tablet accent-teal density-cozy radius-8"],
]) {
  const data = { ...demo().data, viewport, theme };
  if (!theme) delete data.theme;
  html = await renderToString(createSSRApp(Prototype, { artifact: { ...demo(), data } }));
  assert.ok(html.includes(`class="proto-device ${expected}"`), expected);
  assert.ok(!/red;|url\(|\}body|constructor|__proto__|javascript/.test(html.slice(html.indexOf("proto-device"), html.indexOf("proto-appbar"))));
}

// Rejected keys, missing screens and limits refuse the whole demo.
const refused = [];
for (const key of ["href", "url", "src", "action", "onclick", "onload", "style", "class", "html", "script", "srcdoc"]) {
  const component = demo(); component.data.screens[0].components[1][key] = key === "onclick" ? "approve()" : "https://example.invalid";
  refused.push([component.data, "not allowed"]);
  const screen = demo(); screen.data.screens[1][key] = "x";
  refused.push([screen.data, "not allowed"]);
  const top = demo(); top.data[key] = "x";
  refused.push([top.data, "not allowed"]);
}
const withChange = (change) => { const value = demo().data; change(value); return value; };
refused.push(
  [withChange((d) => { d.screens[0].components[1].goto = "missing"; }), "does not exist"],
  [withChange((d) => { d.screens[0].components[1].goto = "../login"; }), "does not exist"],
  [withChange((d) => { d.screens[0].components[1].goto = "https://example.invalid"; }), "does not exist"],
  [withChange((d) => { d.screens[0].components[5].items[0].goto = "nowhere"; }), "does not exist"],
  [withChange((d) => { d.start = "missing"; }), "start screen does not exist"],
  [withChange((d) => { d.screens[0].id = "../home"; }), "unique, simple IDs"],
  [withChange((d) => { d.screens[1].id = "home"; }), "unique, simple IDs"],
  [withChange((d) => { d.screens = Array.from({ length: 13 }, (_, i) => ({ id: "s" + i, title: "S", components: [] })); d.start = "s0"; }), "invalid display data"],
  [withChange((d) => { d.screens[0].components = Array(31).fill({ type: "heading", text: "x" }); }), "invalid display data"],
  [withChange((d) => { d.screens[0].components[0] = { type: "iframe", text: "x" }; }), "not supported"],
  [withChange((d) => { d.screens[0].components[0] = { type: "toString", text: "x" }; }), "not supported"],
  [withChange((d) => { d.screens[0].components[5].items = []; }), "invalid display data"],
  [withChange((d) => { d.screens[0].components[5].items[0].onclick = "x"; }), "not allowed"],
  [withChange((d) => { d.screens[0].components[0].text = { html: "<b>x</b>" }; }), "invalid label"],
  [withChange((d) => { d.screens[0].components[3].body = "x".repeat(2001); }), "invalid label"],
  [withChange((d) => { d.theme.accent = { css: "red" }; }), "invalid display data"],
  [withChange((d) => { d.screens[2].components[1].aspect = "url(https://example.invalid)"; }), "invalid display data"],
  [JSON.parse('{"start":"home","screens":[{"id":"home","title":"Home","components":[{"type":"heading","text":"x","__proto__":{"goto":"x"}}]}]}'), "not allowed"],
);
for (const [data, expected] of refused) {
  html = await renderToString(createSSRApp(Prototype, { artifact: { ...demo(), data } }));
  assert.ok(html.includes('class="proto-error" role="alert"') && html.includes(expected), expected + " " + JSON.stringify(data).slice(0, 120));
  assert.ok(!html.includes("proto-device") && !html.includes("Send feedback") && !html.includes("proto-banner"));
}

// Theme values of the wrong type are refused rather than coerced.
for (const theme of [{ radius: "12" }, { accent: "x".repeat(21) }, { font: "Comic Sans" }]) {
  html = await renderToString(createSSRApp(Prototype, { artifact: { ...demo(), data: { ...demo().data, theme } } }));
  assert.ok(html.includes('role="alert"') && !html.includes("proto-device"), JSON.stringify(theme));
}

// Interaction: generated controls only switch screens locally; the host Back/Restart work.
// v-model reads the root node's activeElement when a value changes; nothing else touches it.
globalThis.Document = class Document { activeElement = null; };
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
const props = reactive({ artifact: demo() });
const root = node("root"), emitted = [];
let s;
const instrumented = { ...Prototype, setup(p, context) { s = Prototype.setup(p, { ...context, emit: (...args) => emitted.push(args) }); return s; } };
const app = renderer.createApp({ setup: () => () => h(instrumented, props) });
app.config.errorHandler = (error) => { vueErrors.push(error); };
app.mount(root);
await settle();
assert.equal(device(root).props["aria-label"], "Demo screen: Home");
assert.ok(textOf(root).includes("1 of 3"));
const back = all(root).find((el) => el.props["aria-label"] === "Previous screen");
assert.equal(back.props.disabled, true);
button(root, "Learn more").props.onClick();
await settle();
assert.equal(device(root).props["aria-label"], "Demo screen: Home", "a button without a target does nothing");
button(root, "Sign in").props.onClick();
await settle();
assert.equal(device(root).props["aria-label"], "Demo screen: Sign in");
const input = all(root).find((el) => el.type === "input");
assert.equal(input.props.placeholder, "you@example.invalid");
s.fields["login:0"] = "person@example.invalid";
const toggle = all(root).find((el) => el.props.role === "switch");
assert.equal(toggle.props["aria-checked"], "false");
toggle.props.onClick();
await settle();
assert.equal(all(root).find((el) => el.props.role === "switch").props["aria-checked"], "true");
// The generated "Approve" button only navigates: no bridge exists, and nothing is emitted.
button(root, "Approve").props.onClick();
await settle();
assert.equal(device(root).props["aria-label"], "Demo screen: Home");
assert.deepEqual(emitted, []);
all(root).find((el) => el.props["aria-label"] === "Previous screen").props.onClick();
await settle();
assert.equal(device(root).props["aria-label"], "Demo screen: Sign in");
assert.equal(s.fields["login:0"], "person@example.invalid", "ephemeral state lives for this session only");
const tabs = () => all(root).filter((el) => el.type === "nav").flatMap((el) => el.children.filter((c) => c.type === "button"));
assert.equal(tabs().length, 0, "the sign-in screen has no navigation");
s.go("home");
await settle();
assert.deepEqual(tabs().map((el) => [textOf(el).trim(), el.props["aria-current"]]), [["Home", "page"], ["Settings", undefined]]);
tabs()[1].props.onClick();
await settle();
assert.equal(device(root).props["aria-label"], "Demo screen: Settings");
const select = all(root).find((el) => el.type === "select");
assert.equal(select.props.value, "settings");
for (const value of ["../home", "https://example.invalid", "__proto__", "constructor"]) {
  select.props.onChange({ target: { value } });
  await settle();
  assert.equal(device(root).props["aria-label"], "Demo screen: Settings", value);
}
select.props.onChange({ target: { value: "login" } });
await settle();
assert.equal(device(root).props["aria-label"], "Demo screen: Sign in");
// Send feedback is the only outward signal: host chrome carrying the screen ID only.
button(root, "Send feedback").props.onClick();
assert.deepEqual(emitted, [["feedback", { chat_id: "chat-a", artifact_id: "demo-1", revision: 2, screen_id: "login" }]]);
all(root).find((el) => el.props["aria-label"] === "Restart demo").props.onClick();
await settle();
assert.equal(device(root).props["aria-label"], "Demo screen: Home");
assert.deepEqual({ ...s.fields }, {});
assert.deepEqual({ ...s.toggles }, {});
// A new revision restarts the demo and clears local state.
s.go("login");
s.fields["login:0"] = "typed";
await settle();
props.artifact = { ...demo(), revision: 3 };
await settle();
assert.equal(device(root).props["aria-label"], "Demo screen: Home");
assert.deepEqual({ ...s.fields }, {});
assert.ok(!all(root).some((el) => ["form", "a", "iframe", "img"].includes(el.type)));
// A malformed revision shows only the refusal; feedback cannot be sent for it.
props.artifact = { ...demo(), data: { ...demo().data, start: "nowhere" } };
await settle();
assert.ok(all(root).some((el) => el.props.role === "alert"));
s.feedback();
s.go("home");
assert.equal(emitted.length, 1);
app.unmount();
assert.deepEqual(vueErrors, []);
console.log("Prototype fixtures passed: banner and host theme tables, hostile tokens fall back, rejected keys/targets/limits refuse the demo, local-only navigation (Approve only navigates), host Back/Restart/screen picker, ephemeral inputs and switches, Send feedback carries the screen ID only, no bridge, escaping.");
