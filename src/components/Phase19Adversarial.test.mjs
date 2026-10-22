// Phase 19 adversarial fixtures for the renderers and the companion (X19; docs/HARNESS_PHASE19_THREAT_MODEL.md).
// Gates G5 (generated UI never interprets data), G20 (presence honesty), G22 (pet gaze) and the F2
// host/renderer quiz contract. Only checked-in sources are compiled; view data is never evaluated, the
// bridge is a recording stub, and nothing reaches a model or the network.
//
// A gate whose module is absent from this copy is a strict expected failure on Missing; F2 is a strict
// expected failure on AssertionError until the host and renderer agree. Either XPASSes, and so fails,
// once its patch lands: remove the marker then.
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { parse, compileScript } from "@vue/compiler-sfc";
import { createSSRApp, defineComponent, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

const require = createRequire(import.meta.url);
const SRC = fileURLToPath(new URL("..", import.meta.url));

class Missing extends Error {}

// Recording bridge: replies by method; anything unexpected is a failure.
const calls = [];
let replies = {};
const bridge = {
  native: false,
  async rpc(method, params) {
    calls.push({ method, params: structuredClone(params) });
    if (!Object.hasOwn(replies, method)) throw new Error("Unexpected RPC: " + method);
    const reply = replies[method];
    return typeof reply === "function" ? reply(params) : structuredClone(reply);
  },
};
function reset(native = false, next = {}) {
  calls.length = 0;
  replies = next;
  bridge.native = native;
}

// Source loader: TypeScript and single-file components, compiled from this checkout only.
const loaded = new Map();
function resolveFrom(from, name) {
  const base = resolve(dirname(from), name);
  for (const candidate of [base, base + ".ts", base + ".mts", base + ".js"]) if (existsSync(candidate)) return candidate;
  throw new Missing(name + " is absent (imported by " + from.replace(SRC, "src/") + ")");
}
function evaluate(code, path, module) {
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(
    (name) => (name === "../bridge" || name === "./bridge" ? bridge : name.startsWith(".") ? load(resolveFrom(path, name)) : require(name)),
    module,
    module.exports,
  );
}
function load(path, inlineTemplate = true) {
  const key = path + (path.endsWith(".vue") ? ":" + inlineTemplate : "");
  if (loaded.has(key)) return loaded.get(key).exports;
  if (!existsSync(path)) throw new Missing(path.replace(SRC, "src/") + " is absent");
  const module = { exports: {} };
  loaded.set(key, module);
  if (path.endsWith(".vue")) {
    const descriptor = parse(readFileSync(path, "utf8"), { filename: path }).descriptor;
    evaluate(compileScript(descriptor, { id: path, inlineTemplate }).content, path, module);
    module.exports = module.exports.default;
  } else evaluate(readFileSync(path, "utf8"), path, module);
  return module.exports;
}
const source = (name) => load(resolve(SRC, name));
const component = (name, inlineTemplate = true) => load(resolve(SRC, "components", name), inlineTemplate);

async function render(Component, props) {
  return renderToString(createSSRApp(Component, props));
}
async function setupOf(Component, props) {
  let bindings;
  const emitted = [];
  const Wrapper = defineComponent({
    setup(_, context) {
      bindings = Component.setup(props, { ...context, emit: (...args) => emitted.push(args) });
      return () => h("div");
    },
  });
  await renderToString(createSSRApp(Wrapper));
  return { bindings, emitted };
}
const settle = async () => {
  for (let i = 0; i < 5; i++) await nextTick();
  await new Promise((done) => setTimeout(done, 0));
};

function gate(name, fn) {
  test(name, async () => {
    await fn();
  });
}
function xfail(name, reason, fn, expected = (error) => error instanceof Missing) {
  test(`${name} [strict xfail: ${reason}]`, async () => {
    try {
      await fn();
    } catch (error) {
      if (expected(error)) return;
      throw error;
    }
    assert.fail(`XPASS(strict): ${reason}. The control landed and holds; remove this marker.`);
  });
}
// Presence gates run on the module when it exists, else they are a strict expected failure naming B1.
const presence = (name, fn) =>
  existsSync(resolve(SRC, "companionState.ts")) ? gate(name, fn) : xfail(name, "B1: src/companionState.ts", fn);
const renderer = (file, name, fn) =>
  existsSync(resolve(SRC, "components", file)) ? gate(name, fn) : xfail(name, "B3: src/components/" + file, fn);

// Markup that would matter only if a renderer interpreted it: a real tag (escaped text is not one) that
// loads, navigates or runs something, a handler or resource attribute, or an active attribute value.
const HOSTILE = "<img src=x onerror=host()><a href=\"javascript:host()\">x</a><script>window.__TAURI__.invoke('approve')</script>";
function active(html) {
  for (const [, tag, attributes] of html.matchAll(/<([a-zA-Z][\w-]*)\b([^>]*)>/g)) {
    if (/^(a|script|iframe|frame|img|object|embed|form|base|link|meta|style|template|portal)$/i.test(tag)) return "<" + tag + ">";
    for (const [, name, value = ""] of attributes.matchAll(/\s([^\s=/>]+)(?:="([^"]*)")?/g)) {
      if (/^on/i.test(name) || /^(href|src|srcset|srcdoc|xlink:href|action|formaction|ping|background|poster)$/i.test(name))
        return tag + " " + name;
      // Text attributes (aria-label, title, alt) are inert; a style value must not load or run anything.
      if (name === "style" && /javascript:|data:|url\(|expression\(|@import/i.test(value)) return tag + " style=" + value;
    }
  }
  return null;
}

const base = { chat_id: "chat-a", title: "View", revision: 1, status: "ready", owner_run_id: "run-1", created: 1, evidence_ids: [] };
const prototypeData = () => ({
  viewport: "phone",
  start: "home",
  theme: { accent: "teal", density: "cozy", radius: 8 },
  screens: [
    { id: "home", title: "Home", components: [
      { type: "heading", text: "Welcome" },
      { type: "button", label: "Approve payment", goto: "done" },
      { type: "input", label: "Email", placeholder: "name at example" },
      { type: "nav", items: [{ label: "Done", goto: "done" }] },
    ] },
    { id: "done", title: "Done", components: [{ type: "card", title: "Paid", body: "Thanks." }] },
  ],
});

// G5. Generated UI never interprets data ------------------------------------------------------------

renderer("PrototypeRenderer.vue", "g5 a prototype draws only host markup: hostile keys refuse the view and hostile text stays text", async () => {
  const Prototype = component("PrototypeRenderer.vue");
  reset();
  const good = await render(Prototype, { artifact: { ...base, id: "p", kind: "prototype", data: prototypeData() } });
  assert.ok(good.includes("Approve payment") && !good.includes('role="alert"'), "the plain demo renders");
  assert.equal(active(good), null);
  for (const [where, key] of [["component", "href"], ["component", "onclick"], ["component", "style"], ["screen", "src"],
    ["root", "script"], ["component", "action"], ["component", "html"], ["theme", "url"], ["component", "ｈｒｅｆ"]]) {
    const data = prototypeData();
    const target = { component: data.screens[0].components[1], screen: data.screens[0], root: data, theme: data.theme }[where];
    target[key] = "javascript:host()";
    const html = await render(Prototype, { artifact: { ...base, id: "p", kind: "prototype", data } });
    assert.ok(html.includes('role="alert"') && !html.includes("javascript:"), `${where}.${key}`);
  }
  for (const slot of ["heading", "button", "title", "card"]) {
    const data = prototypeData();
    if (slot === "heading") data.screens[0].components[0].text = HOSTILE;
    if (slot === "button") data.screens[0].components[1].label = HOSTILE;
    if (slot === "title") data.screens[0].title = HOSTILE;
    if (slot === "card") data.start = "done", data.screens[1].components[0].body = HOSTILE;
    const html = await render(Prototype, { artifact: { ...base, id: "p", kind: "prototype", data } });
    assert.equal(active(html), null, slot);
    assert.ok(html.includes("&lt;img") || html.includes('role="alert"'), `${slot}: shown as text or refused`);
  }
  assert.equal(calls.length, 0, "a prototype never calls the bridge");
});

renderer("PrototypeRenderer.vue", "g5 a prototype's controls only switch its own screens; feedback carries the screen ID only", async () => {
  const Logic = component("PrototypeRenderer.vue", false);
  reset();
  const props = reactive({ artifact: { ...base, id: "p", kind: "prototype", data: prototypeData() }, historical: false });
  const { bindings, emitted } = await setupOf(Logic, props);
  for (const target of ["https://evil.example/approve", "missing", "../done", { id: "done" }, null, "approve"]) {
    bindings.go(target);
    assert.equal(bindings.screenId.value, "home", String(target));
  }
  bindings.go("done");
  assert.equal(bindings.screenId.value, "done");
  bindings.feedback();
  assert.deepEqual(emitted, [["feedback", { chat_id: "chat-a", artifact_id: "p", revision: 1, screen_id: "done" }]]);
  assert.equal(calls.length, 0);
});

renderer("DashboardRenderer.vue", "g5 a dashboard shows host data only for its own binding and never calls the bridge", async () => {
  const Dashboard = component("DashboardRenderer.vue");
  const Logic = component("DashboardRenderer.vue", false);
  reset();
  const data = {
    columns: 2,
    panels: [
      { id: "runs", title: "Runs", kind: "table", binding: { source: "runs.recent", params: { limit: 10 } } },
      { id: "goal", title: "Goal", kind: "metric", binding: { source: "runs.usage" } },
      { id: "note", title: HOSTILE, kind: "metric", data: { value: 3, unit: "runs", caption: HOSTILE } },
    ],
  };
  const row = (panel, source, extra = {}) => ({ panel_id: panel, source, as_of: 1760000000, digest: "sha256:0123456789abcdef", ...extra });
  const table = { columns: [{ id: "item", label: "Item" }, { id: "count", label: "Count" }], rows: [{ item: HOSTILE, count: 2 }] };
  const bindings = [
    row("runs", "runs.recent", { data: table }),
    row("goal", "memory.stats", { data: { value: 9 } }), // another source than the panel's binding
    row("other-panel", "runs.recent", { data: { value: 99 } }), // a panel this view does not have
  ];
  const html = await render(Dashboard, { artifact: { ...base, id: "d", kind: "dashboard", data, bindings } });
  assert.equal(active(html), null);
  assert.ok(html.includes("&lt;img"), "hostile titles, captions and cells are shown as text");
  assert.ok(html.includes("does not match") && !html.includes(">99<") && !html.includes(">9<"), "a mismatched or foreign binding is never shown");
  for (const params of [{ chat_id: "chat-b" }, { path: "/etc/hosts" }, { limit: "../x" }, { url: "https://x" }]) {
    const bad = { columns: 1, panels: [{ id: "runs", title: "Runs", kind: "table", binding: { source: "runs.recent", params } }] };
    const page = await render(Dashboard, { artifact: { ...base, id: "d", kind: "dashboard", data: bad, bindings: [row("runs", "runs.recent", { data: table })] } });
    assert.ok(page.includes('role="alert"') && !page.includes("<table"), JSON.stringify(params));
  }
  for (const source of ["memory.records", "files.read", "../../etc"]) {
    const bad = { columns: 1, panels: [{ id: "x", title: "X", kind: "metric", binding: { source } }] };
    assert.ok((await render(Dashboard, { artifact: { ...base, id: "d", kind: "dashboard", data: bad } })).includes('role="alert"'), source);
  }
  const crowded = { panels: Array.from({ length: 9 }, (_, i) => ({ id: "p" + i, title: "P", kind: "metric", data: { value: i } })) };
  assert.ok((await render(Dashboard, { artifact: { ...base, id: "d", kind: "dashboard", data: crowded } })).includes('role="alert"'));
  // Refresh is host chrome: it only emits, and never for a historical, stopped or unbound view.
  const props = reactive({ artifact: { ...base, id: "d", kind: "dashboard", data, bindings }, historical: true });
  const { bindings: logic, emitted } = await setupOf(Logic, props);
  logic.refresh();
  props.historical = false;
  props.artifact.status = "stopped";
  logic.refresh();
  assert.equal(emitted.length, 0);
  props.artifact.status = "ready";
  logic.refresh();
  assert.deepEqual(emitted, [["refresh"]]);
  assert.equal(calls.length, 0, "a dashboard never calls the bridge");
});

renderer("QuizRenderer.vue", "g5 a quiz view that carries its key is refused and the renderer never grades or keeps answers in the browser", async () => {
  const Quiz = component("QuizRenderer.vue");
  const Logic = component("QuizRenderer.vue", false);
  const questions = [{ id: "q1", question: "Which gas do plants absorb?", choices: ["Oxygen", "Carbon dioxide"] },
    { id: "q2", question: "Which planet is largest?", choices: ["Jupiter", "Mars"] }];
  const publicData = { duration_minutes: 10, questions };
  reset();
  for (const leak of [{ answer: 1 }, { explanation: "Because." }, { correct: 1 }]) {
    const data = { duration_minutes: 10, questions: [{ ...questions[0], ...leak }, questions[1]] };
    const html = await render(Quiz, { artifact: { ...base, id: "q", kind: "quiz", data } });
    assert.ok(html.includes('role="alert"') && !html.includes("Because.") && !/Correct|Carbon dioxide<\/strong>/.test(html), Object.keys(leak)[0]);
  }
  const storage = [];
  globalThis.localStorage = new Proxy({}, { get: (_, key) => (...args) => storage.push([key, ...args]) });
  try {
    const open = { attempt_id: "a1", deadline: Date.now() / 1000 + 600, status: "open", answers: {}, artifact_id: "q", chat_id: "chat-a", revision: 1 };
    reset(true, { quiz_attempt: null, quiz_start: open, quiz_save: { ...open, answers: { q1: 1 } } });
    const props = reactive({ artifact: { ...base, id: "q", kind: "quiz", data: publicData }, surface: "main" });
    const { bindings } = await setupOf(Logic, props);
    await settle();
    await bindings.start();
    await settle();
    bindings.choose("q1", 1);
    bindings.choose("q2", 7); // not an offered answer: ignored, never sent
    await settle();
    assert.equal(bindings.result.value, null, "no score until the host grades");
    const saves = calls.filter((c) => c.method === "quiz_save");
    assert.ok(saves.length >= 1 && saves.every((c) => c.params.chat_id === "chat-a" && c.params.attempt_id === "a1"));
    assert.ok(saves.every((c) => c.params.answers.q2 === null || c.params.answers.q2 === undefined), JSON.stringify(saves));
    assert.deepEqual(storage, [], "answers never go to browser storage");
    // A reply for another view or conversation is never adopted as this view's attempt.
    for (const foreign of [{ ...open, artifact_id: "other" }, { ...open, chat_id: "chat-b" }, { ...open, status: "graded-by-model" }]) {
      reset(true, { quiz_attempt: null, quiz_start: foreign });
      const other = await setupOf(Logic, reactive({ artifact: { ...base, id: "q", kind: "quiz", data: publicData }, surface: "main" }));
      await settle();
      await other.bindings.start();
      await settle();
      assert.equal(other.bindings.attempt.value, null, JSON.stringify(foreign));
      assert.ok(other.bindings.actionError.value, "the bad reply is reported");
    }
    // The pet never shares: Share is main-window chrome.
    const graded = { ...open, status: "submitted", result: { score: 1, total: 2, per_question: [{ id: "q1", correct: true }, { id: "q2", correct: false }] } };
    reset(true, { quiz_attempt: graded, quiz_share: { shared: true } });
    const pet = await setupOf(Logic, reactive({ artifact: { ...base, id: "q", kind: "quiz", data: publicData }, surface: "pet" }));
    await settle();
    await pet.bindings.share();
    assert.ok(!calls.some((c) => c.method === "quiz_share"), "the pet called quiz_share");
  } finally {
    delete globalThis.localStorage;
  }
});

gate("g5 no Phase 19 renderer can turn data into markup: no v-html, innerHTML, dynamic component or bridge import", () => {
  for (const file of ["PrototypeRenderer.vue", "DashboardRenderer.vue", "QuizRenderer.vue", "StatusLine.vue"]) {
    const path = resolve(SRC, "components", file);
    if (!existsSync(path)) continue;
    const text = readFileSync(path, "utf8");
    assert.ok(!/v-html|innerHTML|outerHTML|insertAdjacentHTML|<component\s+:is|new Function|eval\(/.test(text), file);
    assert.ok(!/:(?:href|src|srcdoc|action|formaction)=/.test(text), file + " binds a URL attribute");
    if (file !== "QuizRenderer.vue") assert.ok(!/from "\.\.\/bridge"/.test(text), file + " imports the bridge");
  }
});

gate("g5 a generated html frame is sandboxed with no permissions and a saved script never opens", () => {
  const panel = readFileSync(resolve(SRC, "components", "ArtifactPanel.vue"), "utf8");
  const frames = panel.match(/<iframe\b[^>]*>/g) || [];
  assert.ok(frames.length, "no generated frame found");
  for (const tag of frames) {
    assert.ok(/\ssandbox=""/.test(tag) && !/srcdoc|allow-[a-z]/.test(tag), tag);
    const policy = tag.match(/\sallow="([^"]*)"/);
    // A permissions policy may only deny features.
    assert.ok(!policy || policy[1].split(";").map((s) => s.trim()).filter(Boolean).every((s) => /^[a-z-]+ 'none'$/.test(s)), tag);
  }
  assert.ok(/Generated scripts cannot run here/.test(panel), "a saved script is refused before opening");
});

// G20. Presence honesty ---------------------------------------------------------------------------

presence("g20 no activity is shown before the host admits a run, nor after it settles", () => {
  const { initial, reduce, pose } = source("companionState.ts");
  const LIVE = new Set(source("statusWords.ts").ACTIVITY_KEYS);
  let s = initial(0);
  for (const sig of [{ k: "progress", run_id: "r1", kind: "working", tool: "Read" }, { k: "token", run_id: "r1" },
    { k: "progress", run_id: null, kind: "working", tool: "Edit", activity: "editing" }])
    s = reduce(s, sig, 10);
  assert.ok(!LIVE.has(pose(s, 20).status), pose(s, 20).status);
  s = reduce(s, { k: "run", run_id: "r1", state: "running" }, 30);
  s = reduce(s, { k: "run", run_id: "r1", state: "completed" }, 40);
  s = reduce(s, { k: "progress", run_id: "r1", kind: "working", tool: "RunTests" }, 50);
  s = reduce(s, { k: "token", run_id: "r1" }, 50);
  assert.ok(!LIVE.has(pose(s, 60).status) && !pose(s, 60).live, JSON.stringify(pose(s, 60)));
});

presence("g20 a stale run's late events never change what the companion shows", () => {
  const { initial, reduce, pose } = source("companionState.ts");
  let s = initial(0);
  s = reduce(s, { k: "run", run_id: "old", state: "running" }, 1);
  s = reduce(s, { k: "run", run_id: "new", state: "running" }, 2);
  s = reduce(s, { k: "progress", run_id: "new", kind: "working", tool: "Read" }, 3);
  const before = pose(s, 4);
  for (const sig of [{ k: "progress", run_id: "old", kind: "working", tool: "RunTests" }, { k: "token", run_id: "old" },
    { k: "idle", run_id: "old" }, { k: "error", run_id: "old" }, { k: "run", run_id: "old", state: "failed" }])
    s = reduce(s, sig, 5);
  assert.equal(pose(s, 6).id, before.id);
  assert.equal(pose(s, 6).status, "reading");
});

presence("g20 time alone never changes the pose of a live run, and the pointer never changes its meaning", () => {
  const { initial, reduce, pose } = source("companionState.ts");
  let s = initial(0);
  s = reduce(s, { k: "run", run_id: "r", state: "running" }, 0);
  s = reduce(s, { k: "progress", run_id: "r", kind: "working", tool: "RunTests" }, 0);
  const seen = new Set();
  for (const later of [1e3, 9e4, 3.6e6, 3.6e7]) seen.add(pose(s, later).id + "/" + pose(s, later).status);
  for (const what of ["hover", "tap", "drag", "leave"]) {
    s = reduce(s, { k: "interaction", what }, 5e3);
    seen.add(pose(s, 5e3 + 1).id + "/" + pose(s, 5e3 + 1).status);
  }
  assert.deepEqual([...seen], ["testing/testing"]);
});

presence("g20 a forged activity or a tool named like a host state never borrows its meaning", () => {
  const { initial, reduce, pose } = source("companionState.ts");
  const { HOST_KEYS } = source("statusWords.ts");
  for (const forged of [{ activity: "review" }, { activity: "question" }, { activity: "offline" }, { activity: "__proto__" },
    { tool: "Approve" }, { tool: "AskUserQuestion " }, { tool: "/etc/passwd" }, { tool: "constructor" }]) {
    let s = initial(0);
    s = reduce(s, { k: "run", run_id: "r", state: "running" }, 0);
    s = reduce(s, { k: "progress", run_id: "r", kind: "working", tool: "Tool", ...forged }, 1);
    const shown = pose(s, 2);
    assert.ok(!HOST_KEYS.includes(shown.status) && ["working", "thinking"].includes(shown.status), JSON.stringify([forged, shown.status]));
  }
});

presence("g20 status words come only from the key table: hostile keys show Working, never a name or path", () => {
  const { STATUS_WORDS, STATUS_LABELS, statusWord, statusLabel, toolClass } = source("statusWords.ts");
  for (const hostile of ["/etc/passwd", "Read /Users/x/secret.md", "__proto__", "constructor", "toString", "Approve", "", null, 7, {}]) {
    assert.equal(statusWord(hostile, 3), "Working", String(hostile));
    assert.equal(statusLabel(hostile), "Pebble is working", String(hostile));
  }
  for (const name of ["Approve", "mcp__mail__approve_all", "Review", "__proto__", "hasOwnProperty"]) assert.equal(toolClass(name), "working", name);
  const owner = new Map();
  for (const [key, words] of Object.entries(STATUS_WORDS))
    for (const word of words) {
      assert.ok(!owner.has(word), `"${word}" belongs to ${owner.get(word)} and ${key}`);
      owner.set(word, key);
      assert.ok(!/[\\/<>]|https?:/.test(word), word);
    }
  assert.deepEqual(Object.keys(STATUS_LABELS).sort(), Object.keys(STATUS_WORDS).sort());
  for (const attempt of [0, 100, 1.5, "3", -1, NaN]) assert.equal(statusWord("reconnecting", 0, true, attempt), "Reconnecting", String(attempt));
  assert.equal(statusWord("reconnecting", 0, true, 3), "Reconnecting (try 3)");
});

presence("g20 the status line takes a host key and never a label, tool name or path", () => {
  const line = readFileSync(resolve(SRC, "components", "StatusLine.vue"), "utf8");
  const props = line.slice(line.indexOf("defineProps<{"), line.indexOf("}>()", line.indexOf("defineProps<{")));
  assert.ok(props && !/\b(label|tool|path|text|message)\??\s*:/.test(props), props);
  assert.ok(!/v-html|innerHTML/.test(line));
});

// G22. The pet's gaze is a quantized direction only -----------------------------------------------

presence("g22 the pet gaze accepts only a quantized direction in [-1, 1], never screen coordinates", () => {
  const { parseNativeGaze, quantize, gazeAllowed, GAZE_STEPS } = source("companionPointer.ts");
  for (const payload of [{ x: 512, y: 300 }, { x: 0.2, y: 0.1, screen: { x: 512 } }, { x: "0.2", y: 0 }, { x: NaN, y: 0 },
    { x: 0.2 }, [0.2, 0.1], null, { x: 1.0001, y: 0 }, Object.assign(Object.create({ y: 0 }), { x: 0 })])
    assert.equal(parseNativeGaze(payload), null, JSON.stringify(payload));
  const gaze = parseNativeGaze({ x: 0.3333, y: -1 });
  assert.deepEqual(gaze, { x: quantize(0.3333), y: -1 });
  assert.equal((gaze.x * GAZE_STEPS) % 1, 0, "quantized");
  for (const options of [{ reduced: true }, { systemReduced: true }, { pointer: false }]) assert.equal(gazeAllowed(options), false);
  assert.equal(gazeAllowed({ pointer: true }), true);
});

// F2. The host and the renderer agree on the quiz contract ------------------------------------------

// Regression for X19 F2, resolved by B2 during this session (was a strict xfail): the renderer refused the
// host's stored view (a version field) and its replies (graded status, flat result, share reply). The
// shapes below mirror backend/pebble/quiz_grading.py (split, QuizAttempts._public_attempt, grade, share);
// test_phase19_security.py::test_f2_* checks the host side of the same contract.
renderer("QuizRenderer.vue", "f2 the quiz renderer accepts the host's stored view and adopts its graded and shared replies", async () => {
  const Quiz = component("QuizRenderer.vue");
  const Logic = component("QuizRenderer.vue", false);
  const stored = { duration_minutes: 10, questions: [
    { id: "q1", question: "Which gas do plants absorb?", choices: ["Oxygen", "Carbon dioxide"] },
    { id: "q2", question: "Which planet is largest?", choices: ["Jupiter", "Mars"] }] };
  reset();
  assert.ok(!(await render(Quiz, { artifact: { ...base, id: "q", kind: "quiz", data: stored } })).includes('role="alert"'),
    "the renderer refuses the host's stored quiz view");
  const open = { attempt_id: "a1", artifact_id: "q", revision: 1, origin: "human", status: "open", started: 1,
    deadline: Date.now() / 1000 + 600, answers: {}, result: null, shared: false, seconds_left: 600 };
  const submitted = { ...open, status: "submitted", answers: { q1: 1 }, seconds_left: undefined, result: { score: 1, total: 2,
    per_question: [{ id: "q1", chosen: 1, correct: true, answer: 1, explanation: "Plants take in carbon dioxide." },
      { id: "q2", chosen: null, correct: false, answer: 0, explanation: "Jupiter is the largest." }] } };
  delete submitted.seconds_left;
  reset(true, { quiz_attempt: null, quiz_start: open, quiz_save: { ...open, answers: { q1: 1 } }, quiz_submit: submitted,
    quiz_share: { input_id: "i1", run_id: "run-1", status: "shared", shared: true, origin: "human" } });
  const { bindings } = await setupOf(Logic, reactive({ artifact: { ...base, id: "q", kind: "quiz", data: stored }, surface: "main" }));
  await settle();
  await bindings.start();
  await settle();
  bindings.choose("q1", 1);
  await settle();
  await bindings.submit();
  await settle();
  assert.equal(bindings.actionError.value, "", bindings.actionError.value);
  assert.equal(bindings.result.value?.score, 1);
  await bindings.share();
  await settle();
  assert.equal(bindings.actionError.value, "", bindings.actionError.value);
  assert.equal(bindings.attempt.value?.shared, true);
  assert.deepEqual(calls.filter((c) => c.method === "quiz_share").map((c) => Object.keys(c.params).sort()),
    [["attempt_id", "chat_id"]], "the webview never names its own surface");
});
