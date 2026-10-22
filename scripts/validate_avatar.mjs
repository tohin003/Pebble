import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { transform } from "sucrase";

// Deterministic avatar checks: the companion's pose comes from host events, never from elapsed
// time during work (F1); streamed text is "writing" (F2); the optimistic `active` flag shows
// nothing before the host admits a run (F3). No model calls.
const require = createRequire(import.meta.url);
const cache = new Map();
function load(url) {
  if (cache.has(url.href)) return cache.get(url.href).exports;
  const module = { exports: {} };
  cache.set(url.href, module);
  new Function("require", "module", "exports", transform(readFileSync(url, "utf8"), { transforms: ["typescript", "imports"] }).code)(
    (name) => (name.startsWith(".") ? load(new URL(name.endsWith(".ts") ? name : name + ".ts", url)) : require(name)),
    module, module.exports);
  return module.exports;
}
const activity = load(new URL("../src/avatarActivity.ts", import.meta.url));
const { avatarMotion } = load(new URL("../src/avatarMotion.ts", import.meta.url));
const { POSES } = load(new URL("../src/companionState.ts", import.meta.url));
const { effectScope, nextTick, ref } = require("vue");
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.equal(a, b, message); };

// F1: the time-derived reactions are gone.
eq(activity.reaction, undefined, "reaction() and its impatient/sleepy/curious swaps are removed");
for (const fabricated of ["impatient", "curious", "building"]) ok(!(fabricated in POSES), fabricated + " is not a pose");

// Motion: live poses move with time, reduced motion is still, nothing is NaN.
for (const state of ["thinking", "loading", "reading", "searching", "editing", "testing", "running", "writing", "happy", "sleepy"]) {
  ok(avatarMotion(state, 0.2).transform !== avatarMotion(state, 1.3).transform, state + " moves");
  assert.deepEqual(avatarMotion(state, 0.2, true), avatarMotion(state, 8, true));
  checks++;
  ok(!avatarMotion(state, 0.2).transform.includes("NaN"), state);
}
eq(avatarMotion("loading", 0).cue, "orbit");
eq(avatarMotion("editing", 0).cue, "spark");
eq(avatarMotion("happy", 0).cue, "celebrate");
eq(avatarMotion("idle", 0).cue, "");

// The composable, on a fake clock.
const real = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
let now = 0;
const timers = new Map();
let seq = 0;
globalThis.setTimeout = (fn, ms = 0) => { const id = ++seq; timers.set(id, { at: now + ms, fn }); return id; };
globalThis.clearTimeout = (id) => timers.delete(id);
const advance = async (ms) => {
  const end = now + ms;
  for (;;) {
    const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
    if (!due) break;
    timers.delete(due[0]);
    now = due[1].at;
    due[1].fn();
  }
  now = end;
  await nextTick();
};
try {
  const scope = effectScope();
  const c = scope.run(() => activity.useCompanion({ clock: () => now }));
  const event = (name, data) => c.accept({ event: name, data: { chat_id: "c", ...data } });
  eq(c.mood.value, "idle");
  event("run", { run_id: "r1", state: "queued" });
  event("run", { run_id: "r1", state: "running" });
  event("progress", { run_id: "r1", kind: "working", tool: "Read", label: "Reading notes.md" });
  for (const seconds of [9, 45, 57, 90, 92, 100, 600]) {
    await advance(seconds * 1000 - (now % 1000));
    eq(c.mood.value, "reading", `still reading after ${Math.round(now / 1000)} s: no time-derived mood (F1)`);
  }
  eq(timers.size, 0, "a steady phase needs no timer");
  event("progress", { run_id: "r1", kind: "done", tool: "Read" });
  eq(c.mood.value, "thinking");
  event("token", { id: "m", text: "Hello" });
  eq(c.mood.value, "writing", "streamed text is writing (F2)");
  eq(c.status.value, "writing");
  await advance(1500);
  eq(c.mood.value, "thinking", "writing ends 1.5 s after the last token");
  event("run", { run_id: "r1", state: "completed" });
  event("idle", { run_id: "r1" });
  eq(c.mood.value, "happy");
  eq(c.status.value, null);
  await advance(4500);
  eq(c.mood.value, "idle");
  scope.stop();
  eq(timers.size, 0, "stopping the scope clears its timer");

  // F3: the legacy call shape ignores the optimistic `active` flag.
  const legacy = effectScope();
  const active = ref(true), loading = ref(false), pending = ref(false);
  const old = legacy.run(() => activity.useAvatarActivity(active, loading, pending));
  await nextTick();
  eq(old.mood.value, "idle", "optimistic active shows nothing before the host admits a run (F3)");
  old.companion.sending(true);
  eq(old.mood.value, "sending");
  old.accept({ event: "run", data: { run_id: "r2", state: "queued" } });
  eq(old.mood.value, "thinking");
  loading.value = true;
  await nextTick();
  eq(old.mood.value, "loading");
  old.accept({ event: "model", data: { state: "error" } });
  loading.value = false;
  await nextTick();
  eq(old.mood.value, "error", "a host model error is not overwritten when the loading flag clears");
  pending.value = true;
  old.accept({ event: "model", data: { state: "ready" } });
  await nextTick();
  eq(old.mood.value, "review", "pending approvals outrank live activity");
  legacy.stop();
} finally {
  globalThis.setTimeout = real.setTimeout;
  globalThis.clearTimeout = real.clearTimeout;
}
console.log(`Avatar reactions and motion: ${checks} deterministic checks passed; host events decide the pose (F1–F3); no model calls.`);
