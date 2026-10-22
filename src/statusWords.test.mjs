import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { transform } from "sucrase";

// Status word fixtures for design study §2.3 and the 4 Hz / 10 s budgets of §2.5–§2.6.
const require = createRequire(import.meta.url);
const source = readFileSync(new URL("./statusWords.ts", import.meta.url), "utf8");
const module = { exports: {} };
new Function("require", "module", "exports", transform(source, { transforms: ["typescript", "imports"] }).code)(require, module, module.exports);
const W = module.exports;
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };

const KEYS = [...W.ACTIVITY_KEYS, ...W.HOST_KEYS];
eq(new Set(KEYS).size, KEYS.length, "activity and host keys are disjoint");
eq([...W.LIVE_KEYS].sort(), [...W.ACTIVITY_KEYS].sort(), "live-only keys are exactly the activity keys");
eq(Object.keys(W.STATUS_WORDS).sort(), [...KEYS].sort());
eq(Object.keys(W.STATUS_LABELS).sort(), [...KEYS].sort());
ok(!W.LIVE_KEYS.has("sending"), "sending is the local fact before admission, not host activity (F3)");

// Words: plain, short, original, and never data-shaped.
const seen = new Map();
for (const key of KEYS) {
  const words = W.STATUS_WORDS[key];
  ok(words.length >= 1 && words.length <= 6, key);
  for (const word of words) {
    ok(/^[A-Z][a-z]+( [a-z]+)*$/.test(word) && word.length <= 24, `${key}: "${word}" is a plain phrase`);
    ok(!seen.has(word), `"${word}" belongs to one key only, so rotation never crosses meanings`);
    seen.set(word, key);
  }
  ok(/^(Pebble is|An|The|Sending) /.test(W.STATUS_LABELS[key]), key + " label is a stable sentence");
  ok(!/\d/.test(W.STATUS_LABELS[key]), key + " label carries no counter");
}
eq(W.statusWord("thinking", 0), "Thinking");
eq(W.statusWord("writing", 0), "Writing", "F2: streamed text is writing");
eq(W.statusWord("testing", 0), "Testing");
eq(W.statusWord("testing", 3), "Running tests", "Phase 22: the test step's own phrase joins the testing synonyms");
eq(W.statusWord("question", 0), "Waiting for you");
eq(W.statusWord("review", 0), "Needs your review");
eq(W.statusWord("uncertain", 0), "Needs a check");

// Rotation stays inside the key and only advances with the tick; fixed shows the first word.
for (const key of KEYS) {
  const words = W.STATUS_WORDS[key];
  for (let tick = 0; tick < 40; tick++) {
    const word = W.statusWord(key, tick, true, key === "reconnecting" ? undefined : 3);
    ok(words.includes(word), `${key} at ${tick}`);
    eq(W.statusWord(key, tick, false), words[0]);
  }
  eq(new Set(Array.from({ length: words.length }, (_, i) => W.statusWord(key, i))).size, words.length, key + " rotates through every synonym");
}
eq(W.statusWord("thinking", -7), W.statusWord("thinking", 7));
eq(W.statusWord("thinking", Number.NaN), "Thinking");
eq(W.statusWord("reconnecting", 0, true, 2), "Reconnecting (try 2)");
eq(W.statusWord("reconnecting", 0, true, 0), "Reconnecting");
eq(W.statusWord("reconnecting", 0, true, 2.5), "Reconnecting");
eq(W.statusWord("reconnecting", 0, true, 100), "Reconnecting");
eq(W.statusWord("reconnecting", 0, true, "2"), "Reconnecting");
eq(W.statusWord("thinking", 0, true, 4), "Thinking", "only reconnecting shows a count");
// Unknown keys, including prototype names and hostile text, say "Working".
for (const key of ["__proto__", "constructor", "toString", "Approve", "<img src=x onerror=1>", undefined, null, 4, {}]) {
  eq(W.statusWord(key), "Working", String(key));
  eq(W.statusLabel(key), "Pebble is working", String(key));
  eq(W.isStatusKey(key), false);
}

// toolClass mirrors activity_kinds.ACTIVITY (test_activity_kinds.py compares the tables).
for (const [tool, key] of [["Read", "reading"], ["ReadAttachment", "reading"], ["ReadURL", "reading"], ["ReadProcess", "reading"],
  ["ReadPluginData", "reading"], ["LoadSkill", "reading"], ["Glob", "searching"], ["Grep", "searching"], ["SearchFiles", "searching"],
  ["WebSearch", "searching"], ["Weather", "searching"], ["Write", "editing"], ["Append", "editing"], ["Edit", "editing"],
  ["IntegrateAgentPatch", "editing"], ["RunTests", "testing"], ["RunRecipe", "testing"], ["RunCommand", "running"],
  ["StartCommand", "running"], ["WaitProcess", "running"], ["RunPlan", "running"], ["SpawnAgent", "delegating"],
  ["WaitForAgents", "delegating"], ["UIRender", "viewing"], ["UIUpdate", "viewing"], ["RenderMod", "viewing"],
  ["UpdatePlan", "thinking"], ["OpenApp", "working"]])
  eq(W.toolClass(tool), key, tool);
for (const tool of ["Approve", "approve", "read", "__proto__", "constructor", "hasOwnProperty", "", undefined, null, 7, ["Read"]])
  eq(W.toolClass(tool), "working", String(tool));
for (const [state, key] of Object.entries(W.RUN_STATUS)) ok(W.HOST_KEYS.includes(key), state);
eq(Object.getPrototypeOf(W.RUN_STATUS), null, "lookup tables have no prototype");
eq(W.RUN_STATUS.constructor, undefined);

// Approximate elapsed time.
eq(W.formatElapsed(0), null);
eq(W.formatElapsed(999), null);
eq(W.formatElapsed(1000), "1s");
eq(W.formatElapsed(59999), "59s");
eq(W.formatElapsed(60000), "1 min");
eq(W.formatElapsed(59 * 60000 + 59999), "59 min");
eq(W.formatElapsed(3600000), "1 h 00 min");
eq(W.formatElapsed(3 * 3600000 + 7 * 60000), "3 h 07 min");
eq(W.formatElapsed(-5), null);
eq(W.formatElapsed(Number.NaN), null);
eq(W.elapsedChangeIn(1200), 800);
eq(W.elapsedChangeIn(61000), 59000);
for (let ms = 0; ms < 4 * 3600000; ms += 997) {
  const next = W.elapsedChangeIn(ms);
  assert.equal(W.formatElapsed(ms + next - 1), W.formatElapsed(ms), "unchanged until the reported change");
  if (ms >= 1000) assert.notEqual(W.formatElapsed(ms + next), W.formatElapsed(ms), "changed at the reported time");
}
checks++;

// Phase 22: exact elapsed time from the host's run start.
eq([0, 999, 1000, 59999, 60000, 65000, 3599999, 3600000, 3 * 3600000 + 7 * 60000 + 5000, -1, Number.NaN].map(W.formatExactElapsed),
  [null, null, "1 s", "59 s", "1 min 00 s", "1 min 05 s", "59 min 59 s", "1 h 00 min", "3 h 07 min", null, null]);
for (let ms = 0; ms < 2 * 3600000; ms += 1013) {
  const next = W.exactChangeIn(ms);
  assert.equal(W.formatExactElapsed(ms + next - 1), W.formatExactElapsed(ms), "unchanged until the reported change");
  if (ms >= 1000) assert.notEqual(W.formatExactElapsed(ms + next), W.formatExactElapsed(ms), "changed at the reported time");
}
checks++;

// A fake clock for the coalescer and the announcer.
function fakeTimers() {
  let now = 0, seq = 0;
  const pending = new Map();
  return {
    now: () => now,
    set: (fn, ms) => { const id = ++seq; pending.set(id, { at: now + ms, fn }); return id; },
    clear: (id) => pending.delete(id),
    advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = [...pending.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        pending.delete(due[0]);
        now = due[1].at;
        due[1].fn();
      }
      now = end;
    },
    pending: () => pending.size,
  };
}

// 4 Hz coalescing: 100 updates a second become at most 4 DOM writes, and the last value always lands.
{
  const timers = fakeTimers(), applied = [];
  const c = W.coalesce((v) => applied.push([timers.now(), v]), 250, timers);
  for (let i = 0; i < 300; i++) { c.push(i); timers.advance(10); }
  timers.advance(1000);
  ok(applied.length <= 13, "at most 4 writes per second over 3 s: " + applied.length);
  eq(applied.at(-1)[1], 299, "the latest value lands");
  for (let i = 1; i < applied.length; i++) ok(applied[i][0] - applied[i - 1][0] >= 250, "writes are 250 ms apart");
  eq(applied[0], [0, 0], "the first change is shown at once");
  c.push("late"); eq(applied.at(-1)[1], "late", "an isolated change is immediate");
  c.push("x"); c.cancel(); timers.advance(1000);
  ok(applied.at(-1)[1] !== "x" && timers.pending() === 0, "cancel drops the pending write");
}

// Announcements: one per 10 s, the deferred one is the latest, a reversal says nothing.
{
  const timers = fakeTimers(), said = [];
  const a = W.announcer((text) => said.push([timers.now(), text]), 10000, timers);
  a.say("Pebble is reading");
  timers.advance(2000); a.say("Pebble is editing");
  timers.advance(2000); a.say("Pebble is testing");
  eq(said, [[0, "Pebble is reading"]], "inside the window nothing more is said yet");
  timers.advance(6000);
  eq(said.at(-1), [10000, "Pebble is testing"], "after 10 s only the latest sentence is said");
  eq(said.length, 2);
  a.say("Pebble is reading"); timers.advance(3000); a.say("Pebble is testing"); timers.advance(20000);
  eq(said.length, 2, "a change and its reversal inside the window say nothing");
  a.say("Pebble is testing"); timers.advance(20000);
  eq(said.length, 2, "the same sentence is not repeated");
  const t0 = timers.now();
  eq(t0, 53000);
  a.say("Reply finished");
  eq(said.at(-1), [t0, "Reply finished"]);
  a.clear();
  eq(said.at(-1), [t0, ""], "clear empties the region");
  a.say("Pebble is thinking"); timers.advance(9999);
  eq(said.at(-1)[1], "", "clearing does not reopen the window early");
  timers.advance(1);
  eq(said.at(-1), [t0 + 10000, "Pebble is thinking"]);
  for (let i = 1; i < said.length; i++) if (said[i][1]) ok(said[i][0] - said.filter((x, j) => j < i && x[1]).at(-1)[0] >= 10000, "10 s apart");
  a.say(""); eq(said.at(-1)[1], "Pebble is thinking", "an empty sentence is never announced");
  a.say("Pebble is editing"); a.cancel(); timers.advance(60000);
  ok(said.at(-1)[1] === "Pebble is thinking" && timers.pending() === 0, "cancel drops the pending sentence");
}
ok(!/innerHTML|eval\(|new Function|fetch\(|import\(/.test(source), "pure module");
console.log(`Status word fixtures passed: ${checks} checks; keys and words stay within one meaning, unknown tools and keys say Working, approximate elapsed time, 4 Hz coalescing and one announcement per 10 s.`);
