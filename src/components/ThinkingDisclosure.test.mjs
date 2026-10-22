import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, defineComponent, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Thinking disclosure fixtures (Phase 22): the collapsed "Thought for N s" row and its re-expansion,
// the live body opened by the status line, rows and summaries as text, and on-demand reads by event
// only. The Markdown renderer is a recording stub here (its sanitizer has its own checks).
const require = createRequire(import.meta.url);
const cache = new Map();
function loadTs(url) {
  if (cache.has(url.href)) return cache.get(url.href).exports;
  const module = { exports: {} };
  cache.set(url.href, module);
  new Function("require", "module", "exports", transform(readFileSync(url, "utf8"), { transforms: ["typescript", "imports"] }).code)(
    (name) => (name.startsWith(".") ? loadTs(new URL(name.endsWith(".ts") ? name : name + ".ts", url)) : require(name)), module, module.exports);
  return module.exports;
}
const activity = loadTs(new URL("../activity.ts", import.meta.url));
const rendered = [];
const Markdown = defineComponent({ name: "Markdown", props: ["text"], setup: (p) => () => { rendered.push(p.text); return h("div", { class: "markdown-stub" }, p.text); } });
const filename = "./ThinkingDisclosure.vue";
const source = readFileSync(new URL(filename, import.meta.url), "utf8");
const descriptor = parse(source, { filename }).descriptor;
const script = compileScript(descriptor, { id: filename });
const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(
    (name) => (name === "./Markdown.vue" ? { __esModule: true, default: Markdown } : name === "../activity" ? activity : require(name)), module, module.exports);
  return module.exports;
}
const Disclosure = evaluate(script.content).default;
Disclosure.render = evaluate(template.code).render;
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };

// ---- static ----
ok(!/v-html|innerHTML|\beval\s*\(|new Function|\brpc\b|invoke\(|from "\.\.\/bridge"/.test(source), "no route and no markup sink; the summary goes through Markdown");
ok(/aria-expanded/.test(source) && /aria-controls/.test(source));
ok(/prefers-reduced-motion/.test(source) && /\.reduced/.test(source), "reduced motion stops the breathing mark");

const base = (extra = {}) => ({
  runId: "run-1", chatId: "c", state: "completed", startedAt: 1000, endedAt: 13000, elapsed: null, items: [], local: [], dropped: 0,
  reasoning: "", reasoningClipped: false, attempt: null, next: 0, host: true, loaded: false, error: "", ...extra,
});
const step = (seq, extra = {}) => ({ seq, at: seq, kind: "tool", label: "Step " + seq, detail: null, duration: 1.4, status: "done", ...extra });

// ---- a small DOM ----
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
function mount(props) {
  const root = node("root"), emitted = [];
  const app = renderer.createApp({ setup: () => () => h(Disclosure, { ...props, onExpand: (id) => emitted.push(id) }) });
  const errors = [];
  app.config.errorHandler = (e) => errors.push(e);
  app.mount(root);
  return { root, emitted, errors, unmount: () => app.unmount() };
}

// ---- the collapsed row above a reply ----
{
  const props = reactive({ record: base({ items: [step(0, { kind: "context", label: "Recalled 3 memories", duration: 0.03 }), step(1, { label: "Searching the web" }), step(2, { kind: "retry", label: "Reconnecting to ChatGPT (attempt 2)", status: "failed", detail: "No reply for 120 s" })], reasoning: "**Checking your name**\nThe profile says Sam." }) });
  const view = mount(props);
  await settle();
  const toggle = all(view.root).find((el) => el.type === "button");
  eq([textOf(toggle).trim(), toggle.props["aria-expanded"], toggle.props["aria-controls"], toggle.props.type], ["Worked for 12 s · 1 step · 1 retry", false, "thinking-run-1", "button"]);
  ok(!all(view.root).some((el) => el.props.class === "thinking-body"), "collapsed: no body");
  eq(view.emitted, [], "nothing is read until the person opens it");
  toggle.props.onClick();
  await settle();
  eq(view.emitted, ["run-1"], "opening asks the window for the host record once");
  eq(toggle.props["aria-expanded"], true);
  const body = all(view.root).find((el) => el.props.id === "thinking-run-1");
  ok(body, "the controlled region exists");
  const squash = (text) => text.replace(/\s+/g, " ").trim();
  const labels = all(body).filter((el) => el.props.class === "thinking-label").map((el) => squash(textOf(el)));
  eq(labels, ["Recalled 3 memories", "Searching the web · 1 s", "Reconnecting to ChatGPT (attempt 2) · 1 s"], "retries are rows; durations of 1 s or more follow the label");
  eq(all(body).filter((el) => el.props.class === "thinking-time").map(textOf), ["+0.0 s", "+1.0 s", "+2.0 s"], "each row's offset from the start");
  eq(all(body).filter((el) => el.props.role === "img").map((el) => el.props["aria-label"]), ["Done", "Done", "Failed"], "status marks have words");
  ok(textOf(body).includes("No reply for 120 s"), "details are kept behind a native disclosure");
  eq(rendered.at(-1), "**Checking your name**\nThe profile says Sam.", "the reasoning summary goes through the Markdown renderer");
  const reasoning = all(body).find((el) => el.props.class === "thinking-reasoning");
  const head = all(reasoning).find((el) => el.props.class === "thinking-reasoning-head");
  eq([textOf(head), reasoning.props["aria-labelledby"], head.props.id], ["Pebble's reasoning, summarised by the model", "thinking-run-1-reasoning", "thinking-run-1-reasoning"], "the summary is labelled as the model's");
  toggle.props.onClick();
  await settle();
  ok(!all(view.root).some((el) => el.props.id === "thinking-run-1"), "collapses again");
  toggle.props.onClick();
  await settle();
  eq(view.emitted, ["run-1", "run-1"], "each opening may catch up (the store reads once at a time)");
  props.record = base({ host: false, local: [], reasoning: "" });
  await settle();
  ok(textOf(view.root).includes("No step details were recorded for this reply."), "an empty record says so");
  props.record = base({ host: false, error: "Run not found in this conversation." });
  await settle();
  ok(textOf(view.root).includes("Details are unavailable: Run not found in this conversation."));
  props.record = base({ state: "failed", elapsed: 120, dropped: 3, items: [step(5)] });
  await settle();
  ok(textOf(view.root).includes("Hit a problem after 2 min · 1 step") && textOf(view.root).includes("3 earlier steps are not shown."));
  // Low-stakes steps fold into one row that expands; an edit between them stays its own row.
  props.record = base({ items: [step(0, { activity: "reading", label: "Read a.py" }), step(1, { activity: "reading", label: "Read b.py" }), step(2, { activity: "searching", label: "Searched for todo" }), step(3, { activity: "editing", label: "Edited a.py" })] });
  await settle();
  const fold = all(view.root).find((el) => el.props.class === "thinking-fold");
  eq([squash(textOf(fold)), fold.props["aria-expanded"]], ["Explored · read 2 files, 1 search", false]);
  ok(!textOf(view.root).includes("Read b.py") && textOf(view.root).includes("Edited a.py"), "folded until opened");
  fold.props.onClick();
  await settle();
  ok(fold.props["aria-expanded"] === true && textOf(view.root).includes("Read b.py") && textOf(view.root).includes("Searched for todo"));
  eq(textOf(all(view.root).find((el) => el.props.class === "thinking-summary")).trim(), "Worked for 12 s · 4 steps", "folded steps still count");
  // A quick reply with no step gets no receipt at all.
  props.record = base({ startedAt: 1000, endedAt: 1600, items: [] });
  await settle();
  eq(all(view.root).filter((el) => el.type === "button").length, 0, "no chrome for a quick reply");
  props.record = null;
  await settle();
  eq(all(view.root).filter((el) => el.type === "button").length, 0, "no record, nothing rendered");
  view.unmount();
  eq(view.errors, []);
}

// ---- the live body (the status line is its toggle) ----
{
  const props = reactive({ live: true, open: true, bodyId: "live-thinking", record: base({ state: "running", endedAt: null, items: [step(0, { status: "done", kind: "context", label: "Recalled 3 memories" }), step(1, { status: "running", kind: "model", label: "Waiting for ChatGPT", duration: null })] }) });
  const view = mount(props);
  await settle();
  eq(view.emitted, ["run-1"], "the live body catches up as it opens");
  ok(!all(view.root).some((el) => el.type === "button"), "no second toggle: the status line owns it");
  const body = all(view.root).find((el) => el.props.id === "live-thinking");
  ok(body && body.children.some((el) => el.props["aria-label"] === "What Pebble is doing"));
  const classes = (el) => (typeof el.props.class === "string" ? el.props.class.split(" ") : []);
  const running = all(body).find((el) => classes(el).includes("running"));
  ok(running && textOf(running).includes("Waiting for ChatGPT"), "the current step is marked running");
  eq(all(running).filter((el) => el.props.class === "thinking-took").length, 0, "no duration while running");
  props.record = base({ state: "completed", items: props.record.items });
  await settle();
  ok(!all(view.root).some((el) => classes(el).includes("running")), "a settled run shows no running step");
  ok(all(view.root).some((el) => el.props["aria-label"] === "Unfinished" && el.parent && classes(el.parent).includes("unfinished")), "a step left running is shown as unfinished");
  props.open = false;
  await settle();
  ok(!all(view.root).some((el) => el.props.id === "live-thinking"), "closed by the status line");
  props.record = base({ state: "running", items: [], reasoning: "" });
  props.open = true;
  await settle();
  ok(textOf(view.root).includes("Pebble hasn't reported a step yet."));
  view.unmount();
  eq(view.errors, []);
}

// ---- the drawer's full timeline: controlled, no receipt button, no inline cap ----
{
  const view = mount({ full: true, open: true, bodyId: "drawer-thinking", record: base({ items: [step(0)] }) });
  await settle();
  ok(!all(view.root).some((el) => el.type === "button" && el.props.class === "thinking-summary"));
  ok(all(view.root).some((el) => el.props.id === "drawer-thinking") && all(view.root).some((el) => typeof el.props.class === "string" && el.props.class.includes("full")));
  eq(view.emitted, ["run-1"]);
  view.unmount();
}
ok(/max-height: 240px/.test(source) && /\.full \.thinking-body \{\s*max-height: none/.test(source), "inline 240 px with scroll; the drawer shows all");

// ---- Phase 24 (V1-095): the completion check's verdict line under the receipt ----
{
  const check = (label, seq = 4) => step(seq, { kind: "check", label, duration: 0.01 });
  const lines = (root) => all(root).filter((el) => typeof el.props.class === "string" && el.props.class.split(" ").includes("thinking-verdict"));
  const view = mount({ record: base({ items: [step(0), check("Verified · unittest 3 passed on current files")] }) });
  await settle();
  const [line] = lines(view.root);
  ok(line && textOf(line) === "Verified · unittest 3 passed on current files" && line.props.class === "thinking-verdict", "a verified check shows its verdict without opening");
  ok(!all(view.root).some((el) => el.props.id === "thinking-run-1"), "still collapsed");
  view.unmount();
  const held = mount({ record: base({ items: [step(0), check("Not verified · 2 changed files, no tests run", 3), check("Not verified · 1 changed file, no tests run", 4)] }) });
  await settle();
  eq(lines(held.root).map((el) => [textOf(el), el.props.class]), [["Not verified · 1 changed file, no tests run", "thinking-verdict unverified"]], "the latest verdict only, marked unverified");
  held.unmount();
  const plain = mount({ record: base({ items: [step(0), check("Checked the reply")] }) });
  await settle();
  eq(lines(plain.root).length, 0, "no code changed: no verdict line");
  plain.unmount();
  const live = mount({ live: true, open: true, record: base({ state: "running", items: [check("Verified · pytest 2 passed")] }) });
  await settle();
  eq(lines(live.root).length, 0, "the live body has its own check row, no second line");
  ok(textOf(live.root).includes("Verified · pytest 2 passed"));
  live.unmount();
}

// ---- SSR: hostile text stays text ----
const hostile = "<img src=x onerror=host()>";
const html = await renderToString(createSSRApp(Disclosure, { live: true, open: true, record: base({ items: [step(0, { label: hostile, detail: hostile })] }) }));
ok(html.includes("&lt;img src=x onerror=host()&gt;") && !html.includes("<img"), "labels and details are text");
eq(await renderToString(createSSRApp(Disclosure, { record: null })), "<!--v-if-->");
console.log(`Thinking disclosure fixtures passed: ${checks} checks; collapsed Worked/Thought-for row with aria-expanded and controls, re-expansion, retries and failures as rows, details behind a disclosure, the summary through Markdown, live body without a second toggle, empty and error notes, escaped labels, reads only on opening.`);
