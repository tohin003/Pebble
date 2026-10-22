import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Message action fixtures (Phase 22; design study §5.3–§5.4): Copy writes the message's own stored
// text exactly (the person's words as sent, the reply's Markdown source) and tells the window, whose
// toast says "Copied"; Edit and Retry only emit; a Retry that cannot run says why and never emits.
const require = createRequire(import.meta.url);
const filename = "./MessageActions.vue";
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
const Actions = evaluate(script.content).default;
Actions.render = evaluate(template.code).render;
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };

ok(!/v-html|innerHTML|\beval\s*\(|new Function|\brpc\b|invoke\(|from "\.\.\/bridge"/.test(source), "no route: edit and retry only emit");
ok(/navigator\.clipboard\.writeText\(text\)/.test(source) && /write\(props\.text\)/.test(source), "copy writes the text prop itself");
ok(!/role="status"/.test(source), "one announcement: the window's toast, not a second live region");

// A fake clock for the copied icon.
const real = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
const timers = new Map();
let seq = 0;
globalThis.setTimeout = (fn, ms) => { const id = ++seq; timers.set(id, { fn, ms }); return id; };
globalThis.clearTimeout = (id) => timers.delete(id);
function node(type, text = "") { return { type, text, children: [], props: {}, parent: null }; }
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

try {
  // The person's words exactly as sent: spacing, line breaks, Markdown and markup untouched.
  const raw = "  Hi **Pebble**,\n\n- keep  this\n<b>not markup</b>\t\n";
  const copied = [];
  const props = reactive({ text: raw, role: "user", edit: true, retry: false, retryReason: "", disabled: false, copier: async (text) => { copied.push(text); } });
  const emitted = [];
  const slot = reactive({ on: false });
  const root = node("root");
  const app = renderer.createApp({
    setup: () => () => h(Actions, { ...props, onEdit: (t) => emitted.push(["edit", t]), onRetry: () => emitted.push(["retry"]), onCopied: () => emitted.push(["copied"]), onFailed: (m) => emitted.push(["failed", m]) },
      slot.on ? { default: () => h("span", { class: "usage-stub" }, "1,240 in · 312 out") } : {}),
  });
  const errors = [];
  app.config.errorHandler = (e) => errors.push(e);
  app.mount(root);
  await settle();
  const buttons = () => all(root).filter((el) => el.type === "button");
  const group = () => all(root).find((el) => el.props.role === "group");
  eq(group().props["aria-label"], "Your message");
  eq(buttons().map((b) => [b.props.type, b.props["aria-label"]]), [["button", "Copy your message"], ["button", "Edit your message"]], "keyboard-reachable buttons with names (design study §5.4)");
  await buttons()[0].props.onClick();
  await settle();
  eq(copied, [raw], "the stored text, byte for byte");
  eq(emitted.at(-1), ["copied"], "the window's toast announces it");
  eq(buttons()[0].props.title, "Copied");
  eq([...timers.values()].map((t) => t.ms), [1600]);
  [...timers.values()][0].fn();
  timers.clear();
  await settle();
  eq(buttons()[0].props.title, "Copy your message", "the copied state clears");
  buttons()[1].props.onClick();
  eq(emitted.at(-1), ["edit", raw], "edit hands the text to the window; nothing is sent");
  props.disabled = true;
  await settle();
  eq(buttons()[1].props.disabled, true, "no edit while a task runs");
  props.disabled = false;
  // The assistant's Markdown source, Retry, and the usage row.
  props.role = "assistant";
  props.text = "## Title\n\n```js\nconsole.log(1)\n```";
  props.edit = false;
  props.retry = true;
  slot.on = true;
  await settle();
  eq(buttons().map((b) => b.props["aria-label"]), ["Copy Pebble’s reply", "Retry: ask again as a new message"]);
  eq(group().props["aria-label"], "Pebble’s reply");
  eq(textOf(all(root).find((el) => el.props.class === "message-meta")), "1,240 in · 312 out", "usage sits in the same quiet row");
  await buttons()[0].props.onClick();
  eq(copied.at(-1), "## Title\n\n```js\nconsole.log(1)\n```", "the Markdown source, not rendered text");
  buttons()[1].props.onClick();
  eq(emitted.at(-1), ["retry"]);
  props.retryReason = "Attachments aren't sent again; attach them to a new message.";
  await settle();
  const before = emitted.length;
  buttons()[1].props.onClick();
  eq(emitted.length, before, "a Retry that cannot run never emits");
  eq([buttons()[1].props["aria-disabled"], buttons()[1].props.title], ["true", "Attachments aren't sent again; attach them to a new message."], "and says why, to pointer and keyboard users");
  ok(buttons()[1].props["aria-label"].startsWith("Retry is unavailable: "));
  // A refused clipboard is reported, never swallowed.
  props.copier = async () => { throw new Error("Write permission denied."); };
  await settle();
  await buttons()[0].props.onClick();
  eq(emitted.at(-1), ["failed", "Couldn't copy: Write permission denied."]);
  timers.clear();
  props.copier = async () => {};
  await settle();
  await buttons()[0].props.onClick();
  ok(timers.size === 1);
  app.unmount();
  eq(timers.size, 0, "unmounting clears the copied timer");
  eq(errors, []);
} finally {
  globalThis.setTimeout = real.setTimeout;
  globalThis.clearTimeout = real.clearTimeout;
}
const html = await renderToString(createSSRApp(Actions, { text: "<script>x</script>", role: "assistant" }));
ok(!html.includes("<script>") && html.includes('aria-label="Copy Pebble’s reply"') && html.includes('aria-hidden="true"'), "icons are hidden from assistive technology; text is never rendered");
console.log(`Message action fixtures passed: ${checks} checks; exact text copied for the person and the reply, the window's toast announces it, edit and retry only emit, edit off while a task runs, a Retry that cannot run says why, usage in the same row, clipboard refusals reported, timers cleared.`);
