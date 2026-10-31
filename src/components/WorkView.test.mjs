import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, h, nextTick, reactive } from "vue";
import { transform } from "sucrase";

// Phase 46 (1.9.29, D206): the work view's components drawn from pebble-ui-spec v1's fixtures (docs/ui-spec, written
// from the backend's own builders): every view type becomes its card, an unknown type falls back to today's row,
// every string is a text node (never markup), marks and the ring have words, the branch card reads only git_status and
// git_history through the functions the window passes and proposes a merge only when the person presses Review
// merge, with repository names never inside a button. A small DOM records what is drawn; no route exists here.
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
const sources = {};
function component(file) {
  const url = new URL(file, import.meta.url);
  if (cache.has(url.href)) return cache.get(url.href).exports.default;
  const source = (sources[file] = readFileSync(url, "utf8"));
  const descriptor = parse(source, { filename: file }).descriptor;
  const script = compileScript(descriptor, { id: file });
  const template = compileTemplate({ source: descriptor.template.content, filename: file, id: file, compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, [], file);
  const load = (code) => {
    const module = { exports: {} };
    new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(
      (name) => (name.endsWith(".vue") ? { __esModule: true, default: component(name) } : name.startsWith("..") ? loadTs(new URL(name + ".ts", url)) : require(name)), module, module.exports);
    return module.exports;
  };
  const module = { exports: load(script.content) };
  cache.set(url.href, module);
  module.exports.default.render = load(template.code).render;
  return module.exports.default;
}
const WorkView = component("./WorkView.vue");
const WorkStrip = component("./WorkStrip.vue");
const json = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
const fixtures = json("../../docs/ui-spec/fixtures.json");
const ordered = fixtures.summary_items.map((name) => fixtures.items[name]);
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };

// ---- static: data in, text out; no route, no markup sink ----
for (const file of ["./WorkView.vue", "./WorkCard.vue", "./BranchCard.vue", "./WorkStrip.vue"]) {
  const source = sources[file] || readFileSync(new URL(file, import.meta.url), "utf8");
  ok(!/v-html|innerHTML|\beval\s*\(|new Function|\brpc\b|invoke\(|from "\.\.\/bridge"|@tauri-apps|window\.open|href=|:href/.test(source), file + ": no route, link or markup sink");
}
ok(/prefers-reduced-motion/.test(sources["./WorkView.vue"]) && /prefers-reduced-motion/.test(sources["./WorkCard.vue"]) && /prefers-reduced-motion/.test(sources["./WorkStrip.vue"]), "motion stops when the person asks");
ok(!/aria-live/.test(Object.values(sources).join("")), "StatusLine stays the one live region");

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
const squash = (text) => text.replace(/\s+/g, " ").trim();
const words = (el) => squash(all(el).filter((n) => n.type !== "#comment" && n.text).map((n) => n.text).join(" "));
const cls = (el, name) => String(el.props.class || "").split(" ").includes(name);
async function settle() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick(); } }
globalThis.window ??= { setInterval: () => 1, clearInterval: () => {} };
function mount(Component, props) {
  const root = node("root"), emitted = [], errors = [];
  const app = renderer.createApp({ setup: () => () => h(Component, { ...props, onExpand: (id) => emitted.push(id) }) });
  app.config.errorHandler = (e) => errors.push(e);
  app.mount(root);
  return { root, emitted, errors, unmount: () => app.unmount() };
}
const record = (extra = {}) => reactive({
  runId: "run-1", chatId: "c1", state: "completed", startedAt: 1000, endedAt: 96200, elapsed: 95.2, items: ordered, local: [], dropped: 0,
  reasoning: "", reasoningClipped: false, attempt: null, next: 0, host: true, loaded: true, error: "", hostStart: null, delegated: false, researched: false,
  plan: { steps: fixtures.header.plan.steps }, summary: fixtures.summary, ...extra,
});
// the window's Git functions, recorded
const status = { branch: "feat/session-refresh", upstream: "origin/feat/session-refresh", ahead: 3, behind: 2,
  counts: { staged: 1, unstaged: 2, untracked: 0, unmerged: 0 },
  refs: { branches: [{ name: "main" }, { name: "feat/session-refresh" }, { name: "Approve all actions" }, { name: "<b>x</b>" }, { name: "fifth" }] } };
const history = { commits: [
  { commit: "a".repeat(40), parents: ["b".repeat(40), "c".repeat(40)], author: "you", authored_at: Date.now() / 1000 - 720, subject: "Merge main" },
  { commit: "b".repeat(40), parents: ["d".repeat(40)], author: "you", authored_at: Date.now() / 1000 - 2400, subject: "Refresh sessions in the middleware" },
  { commit: "c".repeat(40), parents: ["d".repeat(40)], author: "Lena", authored_at: Date.now() / 1000 - 7200, subject: "Rotate session keys" },
  { commit: "d".repeat(40), parents: [], author: "Lena", authored_at: Date.now() / 1000 - 86400, subject: "Release 1.4" }] };
function git() {
  const calls = [];
  return { calls, read: async (method, params) => { calls.push([method, params]); return method === "git_status" ? status : history; },
    propose: async (params) => { calls.push(["propose", params]); return { status: "awaiting_user_approval" }; } };
}

// ---- a settled run: header, rail, a card per kind, fallback rows, summary, branch card ----
{
  const access = git();
  const view = mount(WorkView, { record: record(), task: "Fix the session refresh in the auth middleware", chatId: "c1", git: access });
  await settle();
  eq(view.errors, []);
  const text = squash(textOf(view.root));
  const section = all(view.root).find((el) => el.type === "section");
  eq(section.props["aria-label"], "What Pebble did");
  ok(text.includes("Fix the session refresh in the auth middleware") && text.includes("Done · 1 min 35 s · 2 of 5 steps"), "the header: task, word, time, plan");
  const ring = all(view.root).find((el) => el.type === "svg" && el.props.role === "img");
  eq(ring.props["aria-label"], "2 of 5 plan steps done");
  eq(all(view.root).filter((el) => cls(el, "rail-mark")).map((el) => el.props["aria-label"]), ["Done", "Done", "In progress", "Not started", "Not started"], "the plan rail's marks have words");
  const cards = all(view.root).filter((el) => el.type === "article");
  eq(cards.map((el) => el.props["aria-label"]), ["Read 2 files · 1 search", "Searched the web", "Changed 2 files", "npm run build", "python3 scripts/deploy.py --token=***",
    "Tests · python-pytest", "On feat/session-refresh", "Commit 2 files to feat/session-refresh: 'Refresh sessions in the middleware'", "Calculator", "1 agent",
    "What your request requires", "What your request requires", "Branch"], "one card per kind of step, then the branch card");
  for (const words of ["src/auth/session.py", "212 lines", "A private file", "refresh_session", "7 found", "rfc-editor.org", "+6", "−2", "Waiting for you", " · new",
    "dist/app.js 182 kB", "exit 0 · 5 s", "Project folder › scripts · no network", "41 passed · 1 failed", "tests/test_session.py::test_refresh_expired_session",
    "3 ahead · 2 behind", "a1b2c3d4e5f6", "Pressed “=”", "12*7 → 84", "Happened", "Nothing changed", "tests", "42 s", "5.2K tokens",
    "Verified · pytest 42 passed on current files", "The output of make report matches samples/report.txt", "Met after Pebble repaired its work.",
    "1 issue not met yet; the reply says which."])
    ok(text.includes(words), "shown: " + words);
  ok(!text.includes("sk-live") && !text.includes("abc123secret") && !text.includes("hunter2"), "no secret is drawn");
  // the fallback: an unknown type, reviews, a question and plumbing are rows of text
  const rows = all(view.root).filter((el) => cls(el, "row-label")).map((el) => squash(textOf(el)));
  ok(rows.includes("Planting a tree in the garden"), "an unknown view type shows its item's row");
  ok(rows.includes("Waiting for your approval: Drafting tests/test_session.py") && rows.includes("Preparing a clarification form"), "reviews and questions keep their rows");
  eq(all(view.root).filter((el) => el.props.role === "img" && el.props["aria-label"] === undefined).length, 0, "every image-role element has words");
  ok(all(view.root).some((el) => el.props.role === "img" && el.props["aria-label"] === "4 lines added, 2 removed"), "a file's diff bar is described");
  ok(all(view.root).some((el) => el.props.role === "img" && el.props["aria-label"] === "Where Pebble pressed in the window"), "the press ring is described");
  eq(all(view.root).filter((el) => el.props.role === "img" && el.props["aria-label"] === "Met").length, 3, "each met requirement has its word");
  // the summary
  const summary = all(view.root).find((el) => el.props["aria-label"] === "Summary");
  eq(all(summary).filter((el) => el.type === "li").map((el) => squash(textOf(el))), ["1 file changed +4 −2", "2 commands · 1 failed", "Tests 41 passed · 1 failed", "A check asked for more evidence", "1 min 35 s", "52K in · 3.1K out"]);
  // the branch card: two reads through the window's functions, then nothing until the person presses
  eq(access.calls.map(([m]) => m).sort(), ["git_history", "git_status"]);
  eq(access.calls.find(([m]) => m === "git_status")[1], { id: "c1", limit: 1 });
  const branch = cards.at(-1);
  const branchText = words(branch); // chips and columns are separate boxes on screen
  ok(branchText.includes("feat/session-refresh") && branchText.includes("tracks origin/feat/session-refresh") && branchText.includes("3 uncommitted") && branchText.includes("this task +4 −2"), branchText);
  ok(branchText.includes("aaaaaaa Merge main you 12 min") && branchText.includes("Rotate session keys Lena 2 h"), "the commit list with ages: " + branchText);
  const buttons = all(branch).filter((el) => el.type === "button");
  eq(buttons.map((el) => squash(textOf(el))), ["Refresh", "Review merge", "Review merge", "Review merge"], "at most three merges, and no repository name inside a button");
  ok(branchText.includes("Approve all actions") && branchText.includes("<b>x</b>"), "branch names are text beside the buttons");
  ok(!all(branch).some((el) => el.type === "b"), "markup in a name stays text");
  eq(buttons[1].props["aria-describedby"], "merge-0");
  buttons[2].props.onClick();
  await settle();
  eq(access.calls.filter(([m]) => m === "propose"), [["propose", { operation: "merge", name: "Approve all actions" }]], "a merge is only proposed, for review");
  ok(squash(textOf(view.root)).includes("Pebble prepared the merge for your review."));
  eq(view.emitted, ["run-1"], "asks the window once for the host record");
  view.unmount();
}

// ---- a live run: no summary, no merge, an open ring; hostile text stays text ----
{
  const access = git();
  const hostile = { seq: 99999, at: 1, kind: "tool", label: "Reading a file", detail: null, duration: null, status: "running",
    view: { v: 1, type: "read", path: "<img src=x onerror=alert(1)>.md" } };
  const live = record({ state: "running", summary: null, plan: undefined, hostStart: Date.now() - 5000, items: [...ordered.slice(0, 13), hostile] });
  const view = mount(WorkView, { record: live, task: null, chatId: "c1", git: access });
  await settle();
  const text = squash(textOf(view.root));
  ok(text.includes("This task") && text.includes("Working · 5 s"), "a live header");
  eq(all(view.root).find((el) => el.type === "section").props["aria-label"], "What Pebble is doing");
  eq(all(view.root).find((el) => el.type === "svg" && el.props.role === "img").props["aria-label"], "In progress");
  ok(!all(view.root).some((el) => el.props["aria-label"] === "Summary"), "no summary while live");
  ok(text.includes("<img src=x onerror=alert(1)>.md") && !all(view.root).some((el) => el.type === "img"), "markup is text");
  const branch = all(view.root).find((el) => el.props["aria-label"] === "Branch");
  ok(branch && !all(branch).some((el) => el.type === "button" && squash(textOf(el)) === "Review merge"), "no merge while the task runs");
  view.unmount();
}

// ---- the rows of an older service (no views), and the companion's strip ----
{
  const plain = record({ items: ordered.map(({ view, ...item }) => item), summary: null, plan: undefined });
  const view = mount(WorkView, { record: plain, task: "Hi" });
  await settle();
  eq(all(view.root).filter((el) => el.type === "article").length, 0, "no view: no card");
  ok(squash(textOf(view.root)).includes("Reading src/auth/session.py"), "the rows of today");
  view.unmount();
  const strip = mount(WorkStrip, { record: record({ state: "running", items: ordered.slice(0, 7) }) });
  await settle();
  const group = all(strip.root).find((el) => el.props.role === "group");
  eq([group.props["aria-label"], squash(textOf(group))], ["What Pebble is doing now", "npm run build"]);
  eq(all(strip.root).find((el) => el.props.role === "img").props["aria-label"], "2 of 5 plan steps done");
  strip.unmount();
  const idle = mount(WorkStrip, { record: record() });
  await settle();
  eq(all(idle.root).filter((el) => el.props.role === "group").length, 0, "nothing once the task is done");
  idle.unmount();
}
console.log(`WorkView: ${checks} checks passed`);
