import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// StatusLine fixtures (design study §2.3, §2.5–§2.6), following PetRunSummary.test.mjs.
// Only checked-in sources are compiled; status values are data and are never evaluated.
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
const words = loadTs(new URL("../statusWords.ts", import.meta.url));
function evaluate(code) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(
    (name) => (name === "../statusWords" ? words : require(name)), module, module.exports);
  return module.exports;
}
const filename = "./StatusLine.vue";
const source = readFileSync(new URL(filename, import.meta.url), "utf8");
const descriptor = parse(source, { filename }).descriptor;
const script = compileScript(descriptor, { id: filename });
const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
const StatusLine = evaluate(script.content).default;
StatusLine.render = evaluate(template.code).render;
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };

// Static contract: no generated markup, only statusWords and vue, Stop is a plain button.
ok(!/v-html|innerHTML|\beval\s*\(|new Function|<iframe|srcdoc|\brpc\b|invoke\(/.test(source));
eq([...source.matchAll(/from "([^"]+)"/g)].map((m) => m[1]).sort(), ["../statusWords", "vue"]);
ok(/role="status" aria-live="polite"/.test(source));
ok(/class="status-word" aria-hidden="true"/.test(source) && /class="status-elapsed"\s+aria-hidden="true"/.test(source));
ok(/type="button"[\s\S]*?aria-label="Stop task"/.test(source));

// A fake clock: setTimeout and Date.now only (Vue's scheduler uses promises).
const real = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout, now: Date.now };
let now = 1_760_000_000_000, seq = 0;
const timers = new Map();
globalThis.setTimeout = (fn, ms = 0) => { const id = ++seq; timers.set(id, { at: now + Math.max(0, ms), fn }); return id; };
globalThis.clearTimeout = (id) => timers.delete(id);
Date.now = () => now;
const listeners = new Map();
globalThis.document = {
  hidden: false,
  addEventListener: (type, fn) => (listeners.get(type) || listeners.set(type, new Set()).get(type)).add(fn),
  removeEventListener: (type, fn) => listeners.get(type)?.delete(fn),
};
async function settle() { for (let i = 0; i < 6; i++) { await Promise.resolve(); await nextTick(); } }
async function advance(ms) {
  await settle(); // prop changes take effect at the current time, before the clock moves
  const end = now + ms;
  for (;;) {
    const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
    if (!due) break;
    timers.delete(due[0]);
    now = due[1].at;
    due[1].fn();
    await settle();
  }
  now = end;
  await settle();
}

// A minimal DOM renderer that records text writes.
let textWrites = [];
function node(type, text = "") {
  return { type, text, children: [], props: {}, parent: null };
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
  setText(child, text) { child.text = text; textWrites.push([now, child.parent?.props.class, text]); },
  setElementText(child, text) { child.text = text; child.children = []; textWrites.push([now, child.props.class, text]); },
  patchProp(child, key, _previous, value) { child.props[key] = value; },
  parentNode: (child) => child.parent,
  nextSibling: (child) => child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
});
const all = (el) => [el, ...el.children.flatMap(all)];
const textOf = (el) => (el.type === "#comment" ? "" : el.text) + el.children.map(textOf).join("");
// What assistive technology reads: aria-hidden subtrees are skipped.
const accessible = (el) => (el.props["aria-hidden"] === "true" ? "" : (el.type === "#comment" ? "" : el.text) + el.children.map(accessible).join(""));
const byClass = (root, c) => all(root).find((el) => el.props.class === c);

const props = reactive({ status: "reading", startedAt: now, attempt: null, mode: "rotate", stoppable: true, outcome: null, onStop: () => stops++ });
let stops = 0;
const root = node("root");
const vueErrors = [];
const app = renderer.createApp({ setup: () => () => h(StatusLine, props) });
app.config.errorHandler = (error) => vueErrors.push(error);
const vueWarnings = [];
app.config.warnHandler = (message) => vueWarnings.push(message);
app.mount(root);
await settle();
const region = () => all(root).find((el) => el.props.role === "status");
ok(region() && region().props["aria-live"] === "polite" && region().props["aria-atomic"] === "true");
eq(accessible(region()).trim(), "Pebble is reading", "the accessible text is the stable key");
eq(textOf(byClass(root, "status-word")), "Reading");
eq(byClass(root, "status-word").props["aria-hidden"], "true");
ok(!byClass(root, "status-elapsed"), "no elapsed time under a second");
await advance(1000);
eq(textOf(byClass(root, "status-elapsed")), "~1s");
ok(/Approximate/.test(byClass(root, "status-elapsed").props.title), "elapsed time is labelled approximate");
eq(accessible(region()).trim(), "Pebble is reading", "the ticking time is not read out");
const stop = all(root).find((el) => el.type === "button");
eq([stop.props.type, stop.props["aria-label"], textOf(stop).trim()], ["button", "Stop task", "Stop"]);
ok(!all(region()).includes(stop), "Stop sits outside the live region");
stop.props.onClick();
eq(stops, 1, "Stop only emits; the parent's existing stop() runs");

// 4 Hz: twenty key changes inside one second write the word at most four or five times.
await advance(20000);
textWrites = [];
const start = now;
const sequence = ["searching", "reading", "editing", "testing", "running", "reading", "searching", "editing"];
for (let i = 0; i < 20; i++) { props.status = sequence[i % sequence.length]; await advance(50); }
await advance(500);
const wordWrites = textWrites.filter(([t, c]) => c === "status-word" && t - start < 1000);
ok(wordWrites.length <= 5, "word writes in the first second: " + wordWrites.length);
for (let i = 1; i < wordWrites.length; i++) ok(wordWrites[i][0] - wordWrites[i - 1][0] >= 250, "writes 250 ms apart");
eq(textOf(byClass(root, "status-word")), words.statusWord(sequence[19 % sequence.length], 0), "the last key lands");

// One announcement per 10 s, always the latest key.
const announcements = () => textWrites.filter(([, c, text]) => c === "status-sr" && text);
ok(announcements().length <= 1, "one announcement for the burst: " + announcements().length);
const lastSaid = announcements().at(-1)?.[0] ?? start;
await advance(10000);
eq(accessible(region()).trim(), words.statusLabel(props.status), "after the window the region names the current key");
const said = announcements();
for (let i = 1; i < said.length; i++) ok(said[i][0] - said[i - 1][0] >= 10000);
ok(lastSaid >= start);

// Rotation inside the key only; fixed shows one word; off hides the words but keeps the region.
props.status = "thinking";
await advance(11000);
const rotation = [];
for (let i = 0; i < 7; i++) { rotation.push(textOf(byClass(root, "status-word"))); await advance(3000); }
ok(rotation.every((w) => words.STATUS_WORDS.thinking.includes(w)), "synonyms stay inside the key: " + rotation.join(", "));
ok(new Set(rotation).size >= 3, "words rotate every 3 s");
eq(accessible(region()).trim(), "Pebble is thinking", "rotation is never announced");
props.mode = "fixed";
await advance(9000);
eq(textOf(byClass(root, "status-word")), "Thinking");
props.mode = "off";
await advance(300);
ok(!byClass(root, "status-word") && !byClass(root, "status-elapsed"), "off shows no words");
eq(accessible(region()).trim(), "Pebble is thinking", "the accessible status remains");
props.mode = "rotate";

// Reconnecting shows the host's attempt visually; the sentence stays stable.
props.status = "reconnecting"; props.attempt = 3;
await advance(11000);
eq(textOf(byClass(root, "status-word")), "Reconnecting (try 3)");
eq(accessible(region()).trim(), "Pebble is reconnecting to the model");
props.attempt = 4;
await advance(11000);
eq(textOf(byClass(root, "status-word")), "Reconnecting (try 4)");
eq(accessible(region()).trim(), "Pebble is reconnecting to the model", "a new attempt is not a new announcement");

// Hostile or unknown status values show nothing (and never render as markup).
for (const hostile of ["<img src=x onerror=alert(1)>", "constructor", "__proto__", 7, { toString: () => "reading" }]) {
  props.status = hostile;
  await advance(300);
  ok(!byClass(root, "status-word"), String(hostile));
}
eq(accessible(region()).trim(), "", "an unknown key empties the region without an announcement");

// A finished reply is announced once ("Reply finished", F9), and it stays readable.
props.status = "writing"; props.startedAt = now;
await advance(11000);
props.status = null; props.outcome = "completed"; props.stoppable = false;
await advance(11000);
eq(accessible(region()).trim(), "Reply finished");
ok(!all(root).some((el) => el.type === "button"), "no Stop without a live task");
props.outcome = null;
await advance(1000);
eq(accessible(region()).trim(), "Reply finished");

// No timers run while there is nothing to show, while hidden, or after unmount.
eq(timers.size, 0, "idle: no timers");
props.status = "testing"; props.startedAt = now;
await advance(300);
ok(timers.size >= 1, "a visible status ticks");
globalThis.document.hidden = true;
for (const fn of listeners.get("visibilitychange") || []) fn();
await settle();
eq(timers.size, 0, "hidden: no timers");
globalThis.document.hidden = false;
for (const fn of listeners.get("visibilitychange") || []) fn();
await settle();
ok(timers.size >= 1);
app.unmount();
eq(timers.size, 0, "unmounted: no timers");
eq(listeners.get("visibilitychange")?.size || 0, 0, "unmounted: no listeners");
eq(vueErrors, []);
eq(vueWarnings.length, 2, "only the deliberately mistyped hostile values warned");
ok(vueWarnings.every((w) => /Invalid prop: type check failed for prop "status"/.test(w)));

// SSR: escaped, aria-hidden words, accessible sentence.
const html = await renderToString(createSSRApp(StatusLine, { status: "testing", startedAt: now - 5000, stoppable: true }));
ok(html.includes('role="status" aria-live="polite" aria-atomic="true"'));
ok(html.includes('<span class="status-sr">Pebble is testing</span>'));
ok(/<span class="status-word" aria-hidden="true">Testing<\/span>/.test(html));
ok(html.includes("~5s") && html.includes('aria-label="Stop task"'));
const pill = await renderToString(createSSRApp(StatusLine, { status: "reading", pill: true }));
ok(/class="status-line pill"/.test(pill) && pill.includes("Pebble is reading") && !pill.includes("Stop"), "the collapsed pet's pill");
const hostileHtml = await renderToString(createSSRApp(StatusLine, { status: "<script>alert(1)</script>" }));
ok(!/<script|status-word/.test(hostileHtml), "an unknown status renders no word");

// Phase 22: the companion slot replaces the dot; `expandable` makes the visible words one disclosure
// button (it only emits); the `detail` slot is secondary text after the key's word, which never changes.
// The live region still holds only the stable sentence, and Stop stays a separate button. With `exact`
// the host's start gives the time to the second, without "~".
{
  const p = reactive({ status: "searching", startedAt: now, exact: false, stoppable: true, expandable: true, expanded: false, controls: "live-thinking" });
  let toggles = 0, stopped = 0;
  const detail = { value: "Searching the web for /Users/x/notes.md" };
  const slots = reactive({ detail: true });
  const root2 = node("root");
  const app2 = renderer.createApp({
    setup: () => () => h(StatusLine, { ...p, onToggle: () => toggles++, onStop: () => stopped++ }, {
      companion: () => h("svg", { class: "avatar", "aria-hidden": "true" }),
      ...(slots.detail ? { detail: () => detail.value } : {}),
    }),
  });
  app2.config.errorHandler = (error) => vueErrors.push(error);
  app2.mount(root2);
  await advance(1000);
  const buttons = all(root2).filter((el) => el.type === "button");
  const toggle = buttons.find((el) => el.props.class === "status-toggle");
  eq([toggle.props.type, toggle.props["aria-expanded"], toggle.props["aria-controls"], toggle.props["aria-label"]], ["button", false, "live-thinking", "What Pebble is doing"]);
  ok(all(toggle).some((el) => el.type === "svg" && el.props.class === "avatar") && !all(root2).some((el) => el.props.class === "status-dot"), "the companion replaces the dot");
  eq(textOf(byClass(root2, "status-word")), words.statusWord("searching", 0), "the word is always the key's");
  eq(textOf(byClass(root2, "status-detail")), "Searching the web for /Users/x/notes.md", "secondary text is literal, after the word");
  eq([byClass(root2, "status-word").props["aria-hidden"], byClass(root2, "status-detail").props["aria-hidden"]], ["true", "true"]);
  const region2 = all(root2).find((el) => el.props.role === "status");
  eq(accessible(region2).trim(), "Pebble is searching", "the live region keeps the stable sentence");
  ok(!all(region2).includes(toggle) && !all(toggle).slice(1).some((el) => el.type === "button"), "the toggle is outside the region and holds no other button");
  toggle.props.onClick();
  eq(toggles, 1, "the toggle only emits");
  p.expanded = true;
  await advance(300);
  eq(toggle.props["aria-expanded"], true);
  buttons.find((el) => el.props["aria-label"] === "Stop task").props.onClick();
  eq(stopped, 1);
  eq(textOf(byClass(root2, "status-elapsed")), "~1s", "the window's own clock stays approximate");
  p.exact = true;
  p.startedAt = now - 65000;
  await advance(300);
  eq([textOf(byClass(root2, "status-elapsed")), byClass(root2, "status-elapsed").props.title], ["1 min 05 s", "Time since Pebble started this task"], "the host's start: exact, no ~");
  await advance(1000);
  eq(textOf(byClass(root2, "status-elapsed")), "1 min 06 s", "and it ticks by the second");
  slots.detail = false;
  await advance(300);
  ok(!byClass(root2, "status-detail"), "no secondary text without the slot");
  p.status = null;
  p.stoppable = false;
  await advance(300);
  ok(!all(root2).some((el) => el.type === "button"), "no toggle without a status");
  app2.unmount();
  eq([vueErrors, timers.size], [[], 0], "no errors, no timers left");
  const html2 = await renderToString(createSSRApp(StatusLine, { status: "thinking", expandable: true, controls: "x" }));
  ok(html2.includes('class="status-toggle"') && html2.includes('aria-expanded="false"') && html2.includes('aria-controls="x"'));
  ok(html2.includes('<span class="status-sr">Pebble is thinking</span>') && /<span class="status-word" aria-hidden="true">Thinking<\/span>/.test(html2));
}
// Phase 24 (V1-118, i5-ux-06): "Reply finished" is said at once for a reply that settles 100 ms after the last status
// change, and nothing takes it back. 0.6.1 queued it behind the 10 s window, then the coalesced "no status" cleared the
// queued sentence, so the region ended empty (or, for a 3 s reply, spoke about 7 s late).
{
  const p = reactive({ status: "thinking", startedAt: now, outcome: null });
  const root3 = node("root");
  const app3 = renderer.createApp({ setup: () => () => h(StatusLine, p) });
  app3.config.errorHandler = (error) => vueErrors.push(error);
  app3.mount(root3);
  const region3 = () => all(root3).find((el) => el.props.role === "status");
  await advance(300);
  eq(accessible(region3()).trim(), "Pebble is thinking");
  p.status = "writing";
  await advance(100);
  textWrites = [];
  const settledAt = now;
  p.status = null;
  p.outcome = "completed";
  await advance(50);
  eq(accessible(region3()).trim(), "Reply finished", "said at once, inside the 10 s window");
  const finished = textWrites.filter(([, c, text]) => c === "status-sr" && text === "Reply finished");
  ok(finished.length === 1 && finished[0][0] - settledAt < 100, "announced when the reply settled, not 10 s later");
  await advance(11000);
  eq(accessible(region3()).trim(), "Reply finished", "the coalesced empty status does not clear it");
  // The next turn starts 1 s later (inside the window) and is just as quick: its "Reply finished" is a change again.
  p.outcome = null;
  p.status = "thinking";
  await advance(1000);
  textWrites = [];
  p.status = null;
  p.outcome = "completed";
  await advance(50);
  const spoken = textWrites.filter(([, c]) => c === "status-sr").map(([, , text]) => text);
  eq(spoken.at(-1), "Reply finished", "a second quick reply is announced too: " + JSON.stringify(spoken));
  eq(accessible(region3()).trim(), "Reply finished");
  app3.unmount();
}
// Phase 24 (V1-159): with a 500 ms grace, "Sending" shows at once, the first activity word waits until 500 ms after
// the line came on, and a reply quicker than that never flashes "Thinking".
{
  const p = reactive({ status: "sending", grace: 500, outcome: null });
  const root4 = node("root");
  const app4 = renderer.createApp({ setup: () => () => h(StatusLine, p) });
  app4.config.errorHandler = (error) => vueErrors.push(error);
  textWrites = [];
  app4.mount(root4);
  await advance(10);
  eq(textOf(byClass(root4, "status-word")), "Sending", "Sending shows at once");
  p.status = "thinking";
  await advance(100);
  eq(textOf(byClass(root4, "status-word")), "Sending", "the first activity word waits out the grace");
  p.status = null;
  p.outcome = "completed";
  await advance(400);
  ok(!textWrites.some(([, c, text]) => c === "status-word" && text === "Thinking") && !byClass(root4, "status-word"), "a quick reply never shows Thinking");
  eq(accessible(all(root4).find((el) => el.props.role === "status")).trim(), "Reply finished");
  p.outcome = null;
  await advance(20000);
  const start4 = now;
  p.status = "sending";
  await advance(10);
  p.status = "thinking";
  await advance(370);
  eq(textOf(byClass(root4, "status-word")), "Sending");
  await advance(200);
  eq(textOf(byClass(root4, "status-word")), "Thinking", "a slower reply shows its word 500 ms after the line came on");
  ok(now - start4 >= 500 - 20);
  p.status = "question";
  await advance(300);
  eq(textOf(byClass(root4, "status-word")), words.statusWord("question", 0), "host states never wait");
  p.status = null;
  await advance(300);
  app4.unmount();
  eq([vueErrors, timers.size], [[], 0], "no errors, no timers left");
}
globalThis.setTimeout = real.setTimeout;
globalThis.clearTimeout = real.clearTimeout;
Date.now = real.now;
console.log(`StatusLine fixtures passed: ${checks} checks; stable accessible key, aria-hidden synonyms and time, 4 Hz word writes, one announcement per 10 s, Reply finished at once for quick replies (Phase 24), the 500 ms grace, Stop as host chrome, no timers when idle, hidden or unmounted.`);
