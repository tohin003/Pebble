import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { transform } from "sucrase";

// Pure reducer fixtures for design study §2.1 (rows 1–18, run ordering, F1–F3).
// Only checked-in sources are compiled; event payloads are data and are never evaluated.
const require = createRequire(import.meta.url);
const cache = new Map();
function load(url) {
  if (cache.has(url.href)) return cache.get(url.href).exports;
  const module = { exports: {} };
  cache.set(url.href, module);
  const code = transform(readFileSync(url, "utf8"), { transforms: ["typescript", "imports"] }).code;
  new Function("require", "module", "exports", code)(
    (name) => (name.startsWith(".") ? load(new URL(name.endsWith(".ts") ? name : name + ".ts", url)) : require(name)),
    module, module.exports);
  return module.exports;
}
const S = load(new URL("./companionState.ts", import.meta.url));
const W = load(new URL("./statusWords.ts", import.meta.url));
const { reduce, pose, initial, nextChange, fromEvent, seedSignals, visualFor, POSES } = S;
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };

// Apply [time, signal] pairs from a fresh state at time 0.
function play(steps, from = initial(0)) {
  let s = from;
  for (const [t, sig] of steps) s = reduce(s, sig, t);
  return s;
}
const run = (run_id, state) => ({ k: "run", run_id, state });
const work = (tool, extra = {}) => ({ k: "progress", run_id: "r1", kind: "working", tool, ...extra });
const live = [[0, run("r1", "queued")], [10, run("r1", "running")]];
const at = (steps, t, options) => pose(play(steps), t, options);

// ---- the 18-row priority table (first match wins) ----------------------------
const rows = [
  // [name, steps, time, expected pose id, status, expression, engine]
  ["1 offline", [...live, [20, work("Read")], [30, { k: "approvals", pending: 2 }], [40, { k: "offline" }]], 50, "offline", "offline", "triste", "idle"],
  ["2 needs_reconciliation", [...live, [20, run("r1", "needs_reconciliation")], [21, { k: "model", state: "error" }]], 30, "uncertain", "uncertain", "mefiant", "alert"],
  ["3 model error", [[0, { k: "model", state: "error" }], [1, { k: "approvals", pending: 1 }]], 5, "error", "error", "triste", "idle"],
  ["3 run failed settles", [...live, [20, run("r1", "failed")]], 20 + 9999, "error", "error", "triste", "idle"],
  ["4 approvals pending", [...live, [20, work("Read")], [21, { k: "approvals", pending: 1 }]], 30, "review", "review", "attentif", "alert"],
  ["4 waiting_approval", [...live, [20, run("r1", "waiting_approval")]], 30, "review", "review", "attentif", "alert"],
  ["5 question pending", [[0, { k: "question", pending: 1 }]], 5, "question", "question", "curieux", "notify"],
  ["5 waiting_input", [...live, [20, run("r1", "waiting_input")]], 30, "question", "question", "curieux", "notify"],
  ["6 retry_wait", [...live, [20, run("r1", "retry_wait")], [21, { k: "progress", run_id: "r1", kind: "working", attempt: 2 }]], 30, "reconnecting", "reconnecting", "attentif", "idle"],
  ["7 budget_exhausted", [...live, [20, run("r1", "budget_exhausted")]], 30, "blocked", "blocked", "confus", "idle"],
  ["7 verification_required", [...live, [20, run("r1", "verification_required")]], 30, "blocked", "blocked", "confus", "idle"],
  ["7.5 sending", [[0, { k: "sending", on: true }]], 5, "sending", "sending", "attentif", "idle"],
  ["8 reading", [...live, [20, work("Read")]], 30, "reading", "reading", "attentif", "idle"],
  ["8 searching", [...live, [20, work("Grep")]], 30, "searching", "searching", "curieux", "idle"],
  ["8 editing", [...live, [20, work("Edit")]], 30, "editing", "editing", "fier", "idle"],
  ["8 testing", [...live, [20, work("RunTests")]], 30, "testing", "testing", "mefiant", "idle"],
  ["8 running", [...live, [20, work("RunCommand")]], 30, "running", "running", "attentif", "idle"],
  ["8 delegating", [...live, [20, work("SpawnAgent")]], 30, "delegating", "delegating", "curieux", "idle"],
  ["8 viewing", [...live, [20, work("UIRender")]], 30, "viewing", "viewing", "fier", "idle"],
  ["8 unknown tool is working", [...live, [20, work("Approve")]], 30, "working", "working", "attentif", "idle"],
  ["9 writing (F2)", [...live, [20, { k: "token" }]], 20 + 1499, "writing", "writing", "fier", "idle"],
  ["10 thinking", [...live], 30, "thinking", "thinking", "curieux", "idle"],
  ["10 queued is thinking", [[0, run("r1", "queued")]], 30, "thinking", "thinking", "curieux", "idle"],
  ["11 loading", [...live, [20, { k: "model", state: "loading" }]], 30, "loading", "loading", "attentif", "idle"],
  ["12 paused", [...live, [20, run("r1", "paused")]], 30, "paused", "paused", "blase", "idle"],
  ["13 completed", [...live, [20, run("r1", "completed")], [21, { k: "idle", run_id: "r1" }]], 21 + 4499, "happy", null, "heureux", "idle"],
  ["14 stopped", [...live, [20, run("r1", "stopped")]], 20 + 4999, "stopped", null, "blase", "idle"],
  ["15 notices", [[0, { k: "notices", pending: 3 }]], 5, "notify", null, "neutre", "notify"],
  ["16 hover", [[0, { k: "interaction", what: "hover" }]], 5, "hover", null, "attentif", "idle"],
  ["16 tap", [[0, { k: "interaction", what: "hover" }], [1, { k: "interaction", what: "tap" }]], 1 + 1199, "tap", null, "heureux", "idle"],
  ["17 quiet ten minutes", [[0, { k: "interaction", what: "leave" }]], 10 * 60 * 1000, "sleepy", null, "somnolent", "idle"],
  ["18 otherwise", [], 5, "idle", null, "neutre", "idle"],
];
for (const [name, steps, t, id, status, expression, engine] of rows) {
  const p = at(steps, t);
  eq([p.id, p.status, p.expression, p.engine], [id, status, expression, engine], name);
}
// Settle windows end exactly; tap ends; the token window ends.
eq(at(rows.find((r) => r[0] === "3 run failed settles")[1], 20 + 10000).id, "idle", "a failed run settles after 10 s");
eq(at([...live, [20, run("r1", "completed")], [21, { k: "idle", run_id: "r1" }]], 21 + 4500).id, "idle");
eq(at([...live, [20, run("r1", "stopped")]], 20 + 5000).id, "idle");
eq(at([[0, { k: "interaction", what: "tap" }]], 1200).id, "idle");
eq(at([...live, [20, { k: "token" }]], 20 + 1500).id, "thinking", "writing ends 1.5 s after the last token");
eq(at([[0, { k: "interaction", what: "leave" }]], 10 * 60 * 1000 - 1).id, "idle");
// The attempt shown while reconnecting comes from the host's progress event.
eq(at(rows.find((r) => r[0] === "6 retry_wait")[1], 30).attempt, 2);
eq(at(rows.find((r) => r[0] === "6 retry_wait")[1], 30).cue, "orbit");
eq(at([...live], 30).startedAt, 10, "elapsed time counts from the first host running event");
eq(at([[0, run("r1", "queued")]], 30).startedAt, null, "queued is not running yet");
// Row 11 needs a live run like rows 8–10 (§2.1): loading with no run is not activity.
eq(at([[0, { k: "model", state: "loading" }]], 5).id, "idle");
// companion.reactions off drops the expressive rows 13, 16 and 17.
const quiet = { reactions: false };
eq(at([...live, [20, run("r1", "completed")], [21, { k: "idle", run_id: "r1" }]], 22, quiet).id, "idle");
eq(at([[0, { k: "interaction", what: "tap" }]], 5, quiet).id, "idle");
eq(at([[0, { k: "interaction", what: "hover" }]], 5, quiet).id, "idle");
eq(at([], 11 * 60 * 1000, quiet).id, "idle");
eq(at([...live, [20, work("Read")]], 30, quiet).id, "reading", "reactions never hide activity");
eq(at([...live, [20, run("r1", "completed")], [21, { k: "idle", run_id: "r1" }]], 22).outcome, "completed");
eq(at([...live, [20, run("r1", "stopped")]], 22).outcome, "stopped");
eq(at([...live, [20, run("r1", "failed")]], 22).outcome, "failed");

// ---- the host's activity field wins; host keys never arrive through it -------
eq(at([...live, [20, work("Read", { activity: "searching" })]], 30).id, "searching");
eq(at([...live, [20, work("Read", { activity: "review" })]], 30).id, "working", "a progress event cannot claim a host state");
eq(at([...live, [20, work("Read", { activity: "constructor" })]], 30).id, "working");
eq(at([...live, [20, work("UpdatePlan", { activity: "thinking" })]], 30).id, "thinking");
eq(at([...live, [20, work("UpdatePlan")]], 30).id, "thinking", "bookkeeping tools are the model deciding");
for (const name of ["__proto__", "constructor", "toString", "hasOwnProperty", "Approve", "<img src=x onerror=1>"])
  eq(at([...live, [20, work(name)]], 30).id, "working", name);
eq(at([...live, [20, work("Read")], [25, { k: "progress", run_id: "r1", kind: "done", tool: "Read" }]], 30).id, "thinking");
eq(at([...live, [20, work("Read")], [25, { k: "progress", run_id: "r1", kind: "blocked", tool: "Read" }]], 30).id, "thinking");
eq(at([...live, [20, work("Read")], [25, { k: "progress", run_id: "r1", kind: "working" }]], 30).id, "thinking", "a model turn clears the tool");

// ---- run-id ordering ---------------------------------------------------------
let s = play([...live, [20, work("Read")], [30, run("r2", "queued")], [31, run("r2", "running")]]);
eq(pose(s, 40).id, "thinking");
s = play([[41, { k: "progress", run_id: "r1", kind: "working", tool: "Write" }], [42, run("r1", "completed")], [43, { k: "idle", run_id: "r1" }]], s);
eq(pose(s, 44).id, "thinking", "a stale run's late events are ignored once a later run is seen");
eq(s.run.id, "r2");
ok(s.retired.includes("r1"));
s = play([[45, run("x9", "completed")], [46, run("x9", "failed")]], s);
eq(s.run.id, "r2", "a non-live event for an unknown run cannot replace the live one");
s = play([[50, run("r2", "completed")], [51, { k: "idle", run_id: "r2" }]], s);
eq(pose(s, 52).id, "happy");
s = play([[60, run("r1", "queued")]], s);
eq([s.run.id, pose(s, 61).id], ["r1", "thinking"], "a host admission (queued) of an older id is a resumed run");
eq(s.retired.includes("r1"), false);
s = play([[62, run("r3", "running")]], s);
eq([s.run.id, s.retired.includes("r1")], ["r3", true], "a different live run replaces the current one (single inference lane)");
s = play([[63, { k: "token", run_id: "r1" }]], s);
eq(pose(s, 64).id, "thinking", "a token tagged for another run is ignored");
s = play([[65, { k: "progress", run_id: null, kind: "working", tool: "Glob" }]], s);
eq(pose(s, 66).id, "searching", "an untagged event belongs to the live run");
// Nothing before admission: progress or tokens for a run never seen show nothing.
eq(at([[0, { k: "progress", run_id: "r9", kind: "working", tool: "Read" }], [1, { k: "token" }]], 2).id, "idle");
eq(at([[0, { k: "idle", run_id: "r9" }]], 1).id, "idle");
// Nothing after settlement: post-settle notices do not revive activity.
eq(at([...live, [20, run("r1", "completed")], [21, { k: "progress", run_id: "r1", kind: "working", tool: "Read" }], [22, { k: "token" }]], 23).id, "idle");
// The retired list is bounded.
let many = initial(0);
for (let i = 0; i < 200; i++) many = reduce(many, run("run" + i, "queued"), i);
ok(many.retired.length <= 64 && many.run.id === "run199");
// A worker that ends without a settle event (setup failure) stops showing work.
s = play([...live, [20, work("Read")], [21, { k: "error", run_id: "r1" }], [22, { k: "idle", run_id: "r1" }]]);
eq([pose(s, 23).id, pose(s, 23).live, s.run.state], ["error", false, "interrupted"]);
eq(pose(s, 22 + 10000).id, "idle");

// ---- F3: sending until the host's run queued ---------------------------------
s = play([[0, { k: "sending", on: true }]]);
eq([pose(s, 100).id, pose(s, 100).live], ["sending", false]);
eq(pose(play([[1, run("r1", "queued")]], s), 2).id, "thinking", "queued replaces sending");
eq(play([[1, run("r1", "queued")]], s).sending, false);
eq(pose(play([[1, { k: "error" }]], s), 2).id, "error", "an error before admission ends sending");
eq(pose(play([[1, { k: "sending", on: false }]], s), 2).id, "idle", "a command with no run ends sending");
eq(pose(play([[0, { k: "sending", on: true }]], play(live)), 20).id, "thinking", "a follow-up to a live run is not sending");
s = play([...live, [20, run("r1", "completed")], [21, { k: "idle", run_id: "r1" }], [22, { k: "sending", on: true }]]);
eq(pose(s, 23).id, "sending", "a new message ends the celebration");
eq(pose(play([[0, { k: "error" }]]), 1).id, "idle", "an error unrelated to a shown task is not sadness");

// ---- interaction changes gaze only while a run is live -----------------------
for (const what of ["hover", "tap", "drag", "leave"]) {
  const base = play([...live, [20, work("Read")]]);
  eq(pose(reduce(base, { k: "interaction", what }, 30), 31).id, "reading", what + " during a live run");
}
s = play([...live, [20, { k: "interaction", what: "hover" }], [21, run("r1", "completed")], [22, { k: "idle", run_id: "r1" }]]);
eq(pose(s, 23).id, "happy", "row 13 outranks a hover");
eq(pose(s, 22 + 4500).id, "hover");
eq(pose(reduce(s, { k: "interaction", what: "leave" }, 5000), 5001).id, "idle");

// ---- F1: no time-derived mood during a phase ---------------------------------
for (const [steps, expected] of [[[...live, [20, work("Read")]], "reading"], [[...live], "thinking"], [[...live, [20, work("RunTests")]], "testing"]]) {
  const fixed = play(steps);
  for (const t of [30, 9e3, 45e3, 57e3, 92e3, 100e3, 6e5, 36e5, 864e5]) eq(pose(fixed, t).id, expected, `${expected} at ${t} ms`);
}
ok(!Object.keys(POSES).some((id) => ["impatient", "curious", "building"].includes(id)), "the fabricated moods are gone");

// ---- p19_status_live_only and gaze-only interaction under random sequences ----
let seed = 0x19b1;
const rand = (n) => ((seed = (seed * 1103515245 + 12345) >>> 0) % n);
const STATES = ["queued", "running", "retry_wait", "paused", "interrupted", "stopped", "failed", "budget_exhausted", "waiting_approval", "waiting_input", "verification_required", "completed", "needs_reconciliation"];
const TOOLS = ["Read", "Grep", "Write", "RunTests", "RunCommand", "SpawnAgent", "UIRender", "OpenApp", "Approve", undefined];
const randomSignal = () => {
  const id = "r" + rand(3);
  switch (rand(13)) {
    case 0: case 1: return run(id, STATES[rand(STATES.length)]);
    case 2: case 3: return { k: "progress", run_id: rand(3) ? id : null, kind: ["working", "done", "blocked", "review"][rand(4)], tool: TOOLS[rand(TOOLS.length)], attempt: rand(4) };
    case 4: return { k: "token", run_id: rand(2) ? null : id };
    case 5: return { k: "idle", run_id: rand(2) ? id : null };
    case 6: return { k: "error", run_id: rand(2) ? id : null };
    case 7: return { k: "sending", on: !!rand(2) };
    case 8: return { k: ["question", "approvals", "notices"][rand(3)], pending: rand(3) - 1 };
    case 9: return { k: "model", state: ["loading", "ready", "error", "unloaded"][rand(4)] };
    case 10: return rand(8) ? { k: "interaction", what: ["hover", "leave", "tap", "drag"][rand(4)] } : { k: rand(2) ? "offline" : "online" };
    default: return { k: "progress", run_id: id, kind: "working", tool: TOOLS[rand(TOOLS.length)] };
  }
};
let liveChecks = 0;
for (let trial = 0; trial < 400; trial++) {
  let st = initial(0), t = 0;
  for (let step = 0; step < 40; step++) {
    t += rand(4000);
    const sig = randomSignal();
    const before = st;
    st = reduce(st, sig, t);
    for (const dt of [0, 700, 1600, 5000, 11000]) {
      const p = pose(st, t + dt);
      const isLive = ["queued", "running", "retry_wait"].includes(st.run?.state);
      if (W.LIVE_KEYS.has(p.status)) { liveChecks++; assert.ok(isLive, "activity words only during a live run"); }
      assert.ok(!(isLive && ["sleepy", "hover", "tap", "happy", "stopped", "notify", "idle"].includes(p.id)), "no calm pose while live");
      assert.equal(p.status === null || W.isStatusKey(p.status), true);
    }
    if (sig.k === "interaction" && ["queued", "running", "retry_wait"].includes(before.run?.state))
      assert.equal(pose(st, t).id, pose({ ...before, lastInput: t }, t).id, "interaction never changes a live pose");
  }
}
ok(liveChecks > 1000, "the random sequences exercised live activity: " + liveChecks);

// ---- E2: replay a recorded journal (bridge events, as main.rs forwards them) --
const journal = [
  [0, { event: "progress", data: { chat_id: "c", run_id: "r7", label: "Reading /etc/passwd", kind: "working", tool: "Read" } }], // before admission
  [5, { event: "token", data: { chat_id: "c", id: "m", text: "early" } }],
  [100, { event: "run", data: { chat_id: "c", run_id: "r7", state: "queued" } }],
  [120, { event: "run", data: { chat_id: "c", run_id: "r7", state: "running" } }],
  [130, { event: "progress", data: { chat_id: "c", run_id: "r7", label: "Preparing your local workspace", kind: "working" } }],
  [900, { event: "progress", data: { chat_id: "c", run_id: "r7", label: "Reading src/a.ts", kind: "working", tool: "Read", activity: "reading" } }],
  [1400, { event: "progress", data: { chat_id: "c", run_id: "r7", label: "Reading src/a.ts", kind: "done", tool: "Read", activity: null } }],
  [1500, { event: "progress", data: { chat_id: "c", run_id: "r7", label: "Working with your model", kind: "working" } }],
  [2000, { event: "token", data: { chat_id: "c", id: "m", text: "Done" } }],
  [2600, { event: "run", data: { chat_id: "c", run_id: "r7", state: "completed" } }],
  [2610, { event: "progress", data: { chat_id: "c", run_id: "r7", label: "Automatic memory update needs attention", kind: "review" } }],
  [2700, { event: "idle", data: { chat_id: "c", run_id: "r7" } }],
  [2800, { event: "token", data: { chat_id: "c", id: "m", text: "late" } }],
];
let st = initial(0);
const timeline = [];
let admitted = false, terminal = false;
for (const [t, event] of journal) {
  for (const sig of fromEvent(event)) st = reduce(st, sig, t);
  if (event.event === "run" && event.data.state === "queued") admitted = true;
  if (event.event === "run" && event.data.state === "completed") terminal = true;
  const p = pose(st, t);
  timeline.push([t, p.status]);
  if (W.LIVE_KEYS.has(p.status)) ok(admitted && !terminal, `t=${t}: ${p.status} only between queued and the terminal state`);
}
eq(timeline.map(([, k]) => k), [null, null, "thinking", "thinking", "thinking", "reading", "thinking", "thinking", "writing", null, null, null, null]);
eq(pose(st, 2701).id, "happy");
// A key appears at the moment of the host event that caused it (StatusLine adds at most 250 ms);
// between events it changes only at the reducer's own deadline (a quiet token, a settle window).
let replay = initial(0);
for (let i = 0; i < journal.length; i++) {
  const [t, event] = journal[i];
  for (const sig of fromEvent(event)) replay = reduce(replay, sig, t);
  const end = i + 1 < journal.length ? journal[i + 1][0] : t + 20000;
  let shown = pose(replay, t).id, deadline = nextChange(replay, t);
  for (let u = t + 1; u < end; u += 50) {
    const id = pose(replay, u).id;
    if (id === shown) continue;
    ok(deadline !== null && u >= deadline && u - deadline < 50, `t=${u}: ${shown} -> ${id} only at a deadline`);
    shown = id;
    deadline = nextChange(replay, u);
  }
}
ok(!JSON.stringify(st).includes("passwd") && !JSON.stringify(st).includes("src/a.ts"), "labels and paths never enter the companion state");

// ---- fromEvent shapes and hostile payloads -----------------------------------
eq(fromEvent({ event: "progress", data: { kind: "working", tool: "Read", label: "<img src=x onerror=1>", run_id: "r" } }),
  [{ k: "progress", run_id: "r", kind: "working", tool: "Read", activity: undefined, attempt: undefined }]);
eq(fromEvent({ event: "run", data: { run_id: 7, state: "running" } }), []);
eq(fromEvent({ event: "run", data: null }), []);
eq(fromEvent({ event: "progress", data: { tool: "Read" } }), []);
eq(fromEvent({ event: "model", data: { state: "loading" } }), [{ k: "model", state: "loading" }]);
eq(fromEvent({ event: "offline", data: {} }), [{ k: "offline" }]);
eq(fromEvent({ event: "ready", data: {} }), [{ k: "online" }]);
eq(fromEvent({ event: "actions", data: { actions: [{ status: "pending" }] } }), [], "counts come from the window's own records");
eq(fromEvent(null), []);
eq(reduce(initial(0), { k: "run", run_id: "x".repeat(65), state: "running" }, 1).run, null, "an oversized run id is ignored");
eq(reduce(initial(0), { k: "approvals", pending: Number.NaN }, 1).approvals, 0);
eq(reduce(initial(0), { k: "approvals", pending: -4 }, 1).approvals, 0);
eq(reduce(initial(0), { k: "question", pending: 2.7 }, 1).questions, 2);
eq(reduce(initial(0), { k: "interaction", what: "explode" }, 1), initial(0), "an unknown interaction is ignored");
const same = initial(0);
ok(reduce(same, { k: "approvals", pending: 0 }, 5) === same, "no change keeps the same object");

// ---- service restart and seeding ----------------------------------------------
s = play([...live, [20, work("Read")], [30, { k: "offline" }], [40, { k: "online" }]]);
eq([pose(s, 41).id, s.run, s.retired.includes("r1")], ["idle", null, true]);
eq(seedSignals([{ id: "a", state: "completed" }, { id: "b", state: "running" }], "b"), [{ k: "run", run_id: "b", state: "running" }]);
eq(seedSignals([{ id: "a", state: "paused" }]), [{ k: "run", run_id: "a", state: "paused" }]);
for (const state of ["completed", "stopped", "failed", "interrupted"]) eq(seedSignals([{ id: "a", state }]), [], state + " seeds nothing");
eq(seedSignals(undefined), []);
eq(seedSignals([{ id: 3, state: "running" }]), []);

// ---- nextChange: one wake-up at the next deadline, none when nothing is pending --
eq(nextChange(play([...live, [20, { k: "token" }]]), 25), 1520);
eq(nextChange(play([...live]), 25), null, "a live run with no token needs no timer");
eq(nextChange(play([...live, [20, run("r1", "completed")], [21, { k: "idle", run_id: "r1" }]]), 30), 4521);
eq(nextChange(initial(0), 5), 10 * 60 * 1000);
eq(nextChange(initial(0), 10 * 60 * 1000), null);

// ---- visuals: every pose names a real bloub expression and engine state -------
const expressions = load(new URL("./vendor/bloub/expressions.ts", import.meta.url)).EXPRESSION_BY_ID;
const states = load(new URL("./vendor/bloub/states.ts", import.meta.url)).STATE_BY_ID;
for (const [id, v] of Object.entries(POSES)) {
  ok(expressions.has(v.expression), id + " expression " + v.expression);
  ok(states.has(v.engine), id + " engine " + v.engine);
  ok(v.row >= 1 && v.row <= 18, id + " row");
}
eq(visualFor("success").id, "happy");
eq(visualFor("building").id, "editing");
for (const hostile of ["__proto__", "constructor", "impatient", 42, null]) eq(visualFor(hostile).id, "idle", String(hostile));
eq(new Set(Object.values(POSES).map((v) => v.row)).size, 19, "rows 1–18 plus sending (7.5) are all represented");
// The release script that used to lock in the time-derived moods now locks in F1–F3.
await import(new URL("../scripts/validate_avatar.mjs", import.meta.url).href);
console.log(`Companion state fixtures passed: ${checks} checks; 18-row priority table, run-id ordering, F1 (no time-derived moods), F2 (tokens are writing), F3 (sending until queued), gaze-only interaction during live runs, live-only status words over 400 random sequences, E2 journal replay.`);
