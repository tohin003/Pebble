import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { transform } from "sucrase";

// Activity record fixtures (Phase 22 activity contract): item validation and bounds, the live store
// fed by run/activity/reasoning/progress/token events, the progress fallback, message links, the
// on-demand run_activity read and its feature detection. Only checked-in sources are compiled; the
// route is a recording fake and every string is data.
const require = createRequire(import.meta.url);
const source = readFileSync(new URL("./activity.ts", import.meta.url), "utf8");
const module = { exports: {} };
new Function("require", "module", "exports", transform(source, { transforms: ["typescript", "imports"] }).code)(require, module, module.exports);
const A = module.exports;
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };
// Invisible characters are built from code points so this file holds none.
const cp = (...codes) => String.fromCharCode(...codes);
const INVISIBLE = new RegExp("[" + [[0x2028, 0x2029], [0x202a, 0x202e], [0x2066, 0x2069], [0x200b, 0x200f]].map(([a, b]) => cp(a) + "-" + cp(b)).join("") + "]");

// ---- static: pure module, one injected route, no markup sinks, no invisible characters ----
ok(!/innerHTML|v-html|eval\(|new Function|(?<![.\w])fetch\(|import\(|\brpc\b|invoke\(/.test(source), "no route or markup sink of its own (only the injected options.fetch)");
ok(!INVISIBLE.test(source), "the source holds no invisible characters");

// ---- items ----
const item = (extra = {}) => ({ seq: 1, at: 0.5, kind: "tool", label: "Reading notes.md", detail: null, duration: null, status: "running", ...extra });
eq(A.parseItem(item()), { ...item(), activity: null }, "a contract item passes unchanged");
eq([A.parseItem(item({ activity: "reading" })).activity, A.parseItem(item({ activity: "review" })).activity, A.parseItem(item({ activity: "__proto__" })).activity], ["reading", null, null], "only activity classes are kept");
for (const bad of [null, [], "x", 3, {}, item({ seq: -1 }), item({ seq: 1.5 }), item({ seq: "1" }), item({ seq: Number.MAX_SAFE_INTEGER + 1 }), item({ label: "" }), item({ label: "  \n\t " }), item({ label: 7 })])
  eq(A.parseItem(bad), null, JSON.stringify(bad));
eq(A.parseItem(item({ kind: "approve_everything" })).kind, "note", "an unknown kind is a note");
eq(A.parseItem(item({ kind: "__proto__" })).kind, "note");
eq(A.parseItem(item({ status: "approved" })).status, "done", "an unknown status is done, never running");
eq(A.parseItem(item({ at: -3, duration: Number.NaN })).at, 0);
eq(A.parseItem(item({ duration: -1 })).duration, null);
const long = A.parseItem(item({ label: "x".repeat(500), detail: "y".repeat(5000) }));
ok(long.label.length === 80 && long.label.endsWith("…"), "labels are clamped to 80 characters");
ok(long.detail.length === 2000, "details are clamped to 2000 characters");
const hostile = A.parseItem(item({ label: "Reading" + cp(0x202e) + "evil" + cp(0, 0x2028) + "name.md\nNeeds your review", detail: "line one\nline two" + cp(7) }));
eq(hostile.label, "Reading evil name.md Needs your review", "controls, separators and bidi overrides become spaces; one line");
eq(hostile.detail, "line one\nline two", "details keep line breaks but no controls");
eq(A.clampText("<img src=x onerror=1>", 80), "<img src=x onerror=1>", "markup stays text (components never render it as HTML)");

// ---- merge, reasoning, durations, titles ----
let merged = A.mergeItems([item({ seq: 2 })], [item({ seq: 1, label: "Recalled 3 memories", kind: "context", status: "done" }), item({ seq: 2, status: "done", duration: 1.2 })]);
eq(merged.items.map((i) => [i.seq, i.status]), [[1, "done"], [2, "done"]], "upsert by seq, ordered");
merged = A.mergeItems([], Array.from({ length: 230 }, (_, seq) => item({ seq })));
eq([merged.items.length, merged.dropped, merged.items[0].seq], [200, 30, 30], "the newest 200 rows are kept and the rest counted");
let r = A.appendReasoning("", "**Checking the name**\r\n");
eq(r, { text: "**Checking the name**\n", clipped: false });
r = A.appendReasoning("a".repeat(19990), "b".repeat(20));
eq([r.text.length, r.clipped], [20000, true], "the summary is capped at 20000 characters");
eq(A.appendReasoning("x", 5), { text: "x", clipped: false });
eq(A.reasoningHeadline("Some text\n**Recalling your name**\nMore\n**Checking the profile memory**\n"), "Checking the profile memory");
eq(A.reasoningHeadline("no heading **inline bold** here"), null);
eq(["<1 s", "12 s", "1 min", "1 min 05 s", "2 h 03 min", null, null].map(String), [0.4, 12.2, 60, 65, 7380, -1, Number.NaN].map((s) => String(A.formatDuration(s))));
eq([A.formatOffset(3.21), A.formatOffset(0), A.formatOffset(65), A.formatOffset(-1)], ["+3.2 s", "+0.0 s", "+1 min 05 s", ""]);
const settled = (extra) => ({ runId: "r", chatId: "c", state: "completed", startedAt: 1000, endedAt: 13000, elapsed: null, items: [], local: [], dropped: 0, reasoning: "", reasoningClipped: false, attempt: null, next: 0, host: false, loaded: false, error: "", hostStart: null, delegated: false, researched: false, ...extra });
// Receipts (design study §2.3 table).
eq(A.receipt(settled({})), "Thought for 12 s", "reasoning only, no tools");
eq(A.receipt(settled({ elapsed: 11 })), "Thought for 11 s");
const done = (seq, extra = {}) => item({ seq, status: "done", ...extra });
eq(A.receipt(settled({ elapsed: 42, host: true, items: [done(1), done(2, { kind: "check" }), done(3, { kind: "child" }), done(4, { kind: "context" }), done(5, { kind: "model" }), done(6, { kind: "reasoning" })] })), "Worked for 42 s · 3 steps", "tools, checks and helpers are steps");
eq(A.receipt(settled({ elapsed: 42, host: true, items: [done(1), done(2, { kind: "approval", duration: 65 }), done(3, { kind: "retry" })] })), "Worked for 42 s · 1 step · waited 1 min 05 s for you · 1 retry", "waits and retries are never hidden");
eq(A.receipt(settled({ elapsed: 131.7 })), "Thought for 2 min 12 s", "the host's seconds win");
eq(A.receipt(settled({ state: "stopped", elapsed: 12, host: true, items: [done(1)] })), "Stopped after 12 s · 1 step");
eq(A.receipt(settled({ state: "failed", elapsed: 30 })), "Hit a problem after 30 s");
eq(A.receipt(settled({ startedAt: 1000, endedAt: 1600 })), null, "a 0.6 s run with no steps gets no receipt");
eq(A.receipt(settled({ startedAt: 1000, endedAt: 1600, host: true, items: [done(1)] })), "Worked for <1 s · 1 step");
eq(A.receipt(settled({ startedAt: null })), null, "no invented duration");
eq(A.receipt(settled({ startedAt: null, host: true, items: [done(1), done(2)] })), "Worked · 2 steps");
eq(A.receipt(settled({ state: "__proto__" })), "Thought for 12 s");
eq([A.summaryTitle(settled({ startedAt: null })), A.summaryTitle(settled({}))], ["What Pebble did", "Thought for 12 s"]);

// Folding: two or more consecutive finished reads and searches; everything people check stays a row.
const read = (seq, extra = {}) => done(seq, { activity: "reading", label: "Reading f" + seq, ...extra });
const search = (seq) => done(seq, { activity: "searching", label: "Searching q" + seq });
let entries = A.groupRows([read(1), read(2), search(3), read(4), search(5)]);
eq(entries.map((e) => e.type === "group" ? [e.label, e.items.length] : e.item.seq), [["Explored · read 3 files, 2 searches", 5]], "five low-stakes steps, one row");
entries = A.groupRows([read(1), read(2), done(3, { activity: "editing", label: "Editing a.py" }), search(4), search(5), read(6, { status: "running" })]);
eq(entries.map((e) => e.type === "group" ? e.label : e.item.label), ["Explored · read 2 files", "Editing a.py", "Explored · 2 searches", "Reading f6"], "an edit splits the group; a running read stays a row");
entries = A.groupRows([read(1), done(2, { kind: "retry", label: "Reconnecting to ChatGPT (attempt 2)" }), read(3), done(4, { kind: "approval", activity: "reading", label: "Waited for your approval" }), read(5, { status: "failed" })]);
eq(entries.map((e) => e.type), ["row", "row", "row", "row", "row"], "single reads, retries, waits and failures are never folded");
eq(A.groupRows([read(1), done(2), read(3)]).map((e) => e.type), ["row", "row", "row"], "a step without a class is not folded");

// Secondary text: the running step, or the latest reasoning headline while the model works.
eq(A.liveDetail(settled({ state: "running", host: true, items: [item({ seq: 1, status: "done" }), item({ seq: 2, kind: "tool", label: "Searching the web" })] })), "Searching the web");
eq(A.liveDetail(settled({ state: "running", host: true, reasoning: "**Considering your name**", items: [item({ seq: 3, kind: "reasoning", label: "Thinking" })] })), "Considering your name");
eq(A.liveDetail(settled({ state: "running", host: true, reasoning: "**Considering your name**", items: [item({ seq: 3, kind: "model", label: "Waiting for ChatGPT" })] })), "Considering your name");
eq(A.liveDetail(settled({ state: "running", host: true, items: [item({ seq: 3, kind: "model", label: "Waiting for ChatGPT" })] })), "Waiting for ChatGPT");
eq(A.liveDetail(settled({ state: "running", host: true, reasoning: "**Checking the profile**", items: [] })), "Checking the profile");
eq(A.liveDetail(settled({ state: "running", host: false, local: [item()] })), "Reading notes.md", "the progress fallback's running step");
eq(A.liveDetail(settled({ state: "completed", host: true, items: [item()] })), null, "nothing once settled");
eq(A.liveDetail(null), null);
for (const message of ["Unknown request.", "Invalid local request.", "Error: unknown method run_activity"]) ok(A.isUnsupported(new Error(message)), message);
ok(!A.isUnsupported(new Error("Run not found in this conversation.")), "an unknown run is an ordinary error");

// ---- the store, on a fake clock and a recording route ----
let now = 1_000_000;
const calls = [];
let reply = async () => { throw new Error("Unknown request."); };
const store = A.createActivityStore({ now: () => now, fetch: (runId, after) => { calls.push([runId, after]); return reply(runId, after); } });
const send = (event, data) => store.accept({ event, data: { chat_id: "c1", ...data } });
send("run", { run_id: "r1", state: "queued" });
eq(store.liveFor("c1")?.runId, "r1", "a queued run is live for its chat");
now += 500;
send("run", { run_id: "r1", state: "running" });
eq(store.record("r1").startedAt, now, "time counts from the first running event");
// Fallback rows from progress events while no host items exist.
now += 1000;
send("progress", { run_id: "r1", label: "Preparing your local workspace", kind: "working" });
now += 1000;
send("progress", { run_id: "r1", label: "Reading notes.md", kind: "working", tool: "Read", activity: "reading" });
now += 2000;
send("progress", { run_id: "r1", label: "Reading notes.md", kind: "done", tool: "Read" });
send("progress", { run_id: "r1", label: "Reconnecting to the selected model", kind: "working", attempt: 2 });
const local = A.rows(store.record("r1"));
eq(local.map((i) => [i.kind, i.label, i.status]), [
  ["context", "Preparing your local workspace", "done"],
  ["tool", "Reading notes.md", "done"],
  ["retry", "Reconnecting to the selected model (attempt 2)", "running"],
], "progress becomes a timeline; retries are rows with their attempt");
eq(local[1].duration, 2, "a tool row's duration comes from its done event");
eq(A.liveDetail(store.record("r1")), "Reconnecting to the selected model (attempt 2)", "the fallback's running step is secondary text");
eq(store.record("r1").hostStart, null, "no host clock yet");
// Host items replace the fallback, and reasoning deltas accumulate per attempt.
send("activity", { run_id: "r1", item: { seq: 0, at: 0.1, kind: "context", label: "Recalled 3 memories", detail: null, duration: 0.03, status: "done" } });
send("activity", { run_id: "r1", item: { seq: 1, at: 0.2, kind: "retry", label: "Reconnecting to ChatGPT (attempt 2)", detail: null, duration: null, status: "running" } });
send("activity", { run_id: "r1", item: { seq: "bad" } });
send("activity", { run_id: "../r1", item: item() });
eq(A.rows(store.record("r1")).map((i) => i.label), ["Recalled 3 memories", "Reconnecting to ChatGPT (attempt 2)"], "host items, not the fallback");
eq(A.liveDetail(store.record("r1")), "Reconnecting to ChatGPT (attempt 2)");
eq(store.record("r1").hostStart, now - 100, "the first host item's offset (0.1 s) places the start on the host's clock");
send("reasoning", { run_id: "r1", attempt_id: "a1", delta: "**Looking for" });
send("reasoning", { run_id: "r1", attempt_id: "a1", delta: " your name**\nThe profile layer has it." });
send("reasoning", { run_id: "r1", attempt_id: "a2", delta: "Second attempt." });
send("reasoning", { run_id: "r1", attempt_id: "a2", delta: 42 });
eq(store.record("r1").reasoning, "**Looking for your name**\nThe profile layer has it.\n\nSecond attempt.");
ok(store.state.events, "host events were seen");
// Tokens link the streaming reply to the live run; idle settles it.
send("token", { id: "m1", text: "Hi" });
eq(store.forMessage("m1")?.runId, "r1");
now += 8000;
send("run", { run_id: "r1", state: "completed" });
send("idle", { run_id: "r1" });
eq(store.liveFor("c1"), null, "nothing is live after the run settles");
eq(A.settledSeconds(store.record("r1")), 12, "this window's clock: 12 s from running to completed");
send("token", { id: "m-late", text: "x" });
eq(store.forMessage("m-late"), null, "a token after the run never links to it");
// Other chats and stray events do not touch this run.
store.accept({ event: "activity", data: { chat_id: "c2", item: item() } });
store.accept({ event: "reasoning", data: null });
store.accept(null);
eq(A.rows(store.record("r1")).length, 2);

// ---- run_activity: on demand, once at a time, then off when the route is refused ----
await store.load("r1");
eq(calls, [["r1", 0]], "one read with the cursor");
eq(store.state.supported, false, "an unknown request turns the route off for this window");
await store.load("r1");
eq(calls.length, 1, "no further reads once refused");

const s2 = A.createActivityStore({ now: () => now, fetch: async (runId, after) => { calls.push([runId, after]); return reply(runId, after); } });
s2.accept({ event: "run", data: { chat_id: "c1", run_id: "r2", state: "running" } });
reply = async (runId) => ({
  run_id: runId, state: "completed", started: 2000, elapsed: 131.7, next: 7,
  items: [{ seq: 0, at: 0, kind: "context", label: "Recalled 3 memories", detail: null, duration: 0.03, status: "done" },
    { seq: 1, at: 0.1, kind: "model", label: "Waiting for ChatGPT", detail: "No reply for 120 s", duration: 120.1, status: "failed" },
    { seq: 2, at: 120.7, kind: "retry", label: "Reconnecting to ChatGPT (attempt 2)", detail: null, duration: 0.5, status: "done" }, { seq: "x" }],
  reasoning: { available: true, text: "**Checking the profile**" },
  usage: { input: null, cached: null, output: null, reasoning: null, tokens_per_second: null },
});
const loaded = await s2.load("r2");
eq([loaded.state, loaded.elapsed, loaded.next, loaded.startedAt, loaded.host, loaded.loaded], ["completed", 131.7, 7, 2_000_000, true, true]);
eq(A.rows(loaded).map((i) => i.status), ["done", "failed", "done"]);
eq(A.summaryTitle(loaded), "Thought for 2 min 12 s · 1 retry");
eq(s2.liveFor("c1"), null, "a settled reply leaves the live map");
eq(s2.state.supported, true);
reply = async () => ({ run_id: "someone-else", items: [] });
const wrong = await s2.load("r2");
ok(wrong.error.includes("another task") && s2.state.supported === true, "a reply for another run is refused as an ordinary error");
reply = async () => { throw new Error("Run not found in this conversation."); };
ok((await s2.load("r2")).error.includes("not found"));
eq(await s2.load("missing"), null, "no record, no read");
// While live, the host's active seconds fix the start (so time waiting for the person is left out),
// and a new live stretch (after an approval) reads the host's record again.
s2.accept({ event: "run", data: { chat_id: "c7", run_id: "r7", state: "running" } });
reply = async (runId) => ({ run_id: runId, state: "running", started: 1, elapsed: 30, next: 2, items: [] });
const live7 = await s2.load("r7");
eq([live7.hostStart, live7.loaded, live7.elapsed], [now - 30000, true, null], "a live run's elapsed sets its start, not its receipt");
s2.accept({ event: "run", data: { chat_id: "c7", run_id: "r7", state: "waiting_approval" } });
s2.accept({ event: "run", data: { chat_id: "c7", run_id: "r7", state: "running" } });
eq([s2.record("r7").loaded, s2.record("r7").hostStart], [false, null], "after a wait the window asks again");

// ---- fallback phases end when the run moves on; tool rows keep their own done events ----
const s5 = A.createActivityStore({ now: () => now });
const t5 = (event, data) => s5.accept({ event, data: { chat_id: "c5", ...data } });
t5("run", { run_id: "r5", state: "running" });
t5("run", { run_id: "r5", state: "retry_wait" });
t5("progress", { run_id: "r5", label: "Reconnecting to the selected model", kind: "working", attempt: 2 });
now += 600;
t5("run", { run_id: "r5", state: "running" });
eq(A.rows(s5.record("r5")).map((i) => [i.kind, i.status, i.duration]), [["retry", "done", 0.6]], "the reconnect row ends when the next attempt runs");
t5("progress", { run_id: "r5", label: "Preparing your local workspace", kind: "working" });
t5("progress", { run_id: "r5", label: "Reading notes.md", kind: "working", tool: "Read" });
t5("progress", { run_id: "r5", label: "Checking the profile", kind: "working" });
now += 400;
t5("token", { id: "m5", text: "Hi" });
eq(A.rows(s5.record("r5")).map((i) => i.status), ["done", "done", "running", "done"], "streaming text ends phase rows, not a tool row");
t5("run", { run_id: "r5", state: "stopped" });
eq(A.rows(s5.record("r5")).map((i) => i.status), ["done", "done", "running", "done"], "a tool row without its done event stays unfinished");
eq(A.summaryTitle(s5.record("r5")), "Stopped after 1 s · 1 step · 1 retry");

// ---- reopening a conversation: tagged messages and one-window time matches ----
const s3 = A.createActivityStore({ now: () => now });
s3.seedChat({
  id: "c9",
  runs: [
    { id: "live", state: "running", created: 900 },
    { id: "late", state: "completed", created: 500, updated: 520, elapsed: 18 },
    { id: "early", state: "failed", created: 100, updated: 130, elapsed: 30 },
    { id: "overlap", state: "completed", created: 505, updated: 600 },
    { id: "../bad", state: "completed", created: 1, updated: 2 },
  ],
  messages: [
    { id: "u1", role: "user", created: 100 },
    { id: "a1", role: "assistant", created: 125 },
    { id: "a2", role: "assistant", created: 512 },
    { id: "a3", role: "assistant", created: 700, run_id: "late" },
  ],
});
eq(s3.forMessage("a3")?.runId, "late", "a host tag links directly");
eq(s3.forMessage("a1")?.runId, "early", "one run's window holds the reply");
eq(s3.forMessage("a2"), null, "two overlapping windows: no guess");
eq(s3.liveFor("c9")?.runId, "live");
eq(A.summaryTitle(s3.forMessage("a1")), "Hit a problem after 30 s", "the ledger's seconds");
eq(A.summaryTitle(s3.record("overlap")), "Thought for 1 min 35 s", "no ledger seconds: the run's own created and updated times");
eq(s3.record("../bad"), null);

// ---- Phase 24 (V1-121, i5-ux-10): a reopened conversation's receipt comes from the host's per-run counts ----
// 0.6.1 had no counts: a 6-step, 42 s run read "Thought for 42 s" until its timeline was opened.
{
  const s6 = A.createActivityStore({ now: () => now });
  s6.seedChat({
    id: "c6",
    runs: [
      { id: "worked", state: "completed", created: 100, updated: 142, elapsed: 42, counts: { steps: 6, retries: 1, waited: 65 } },
      { id: "thought", state: "completed", created: 200, updated: 211, elapsed: 11, counts: { steps: 0, retries: 0, waited: 0 } },
      { id: "unknown", state: "completed", created: 300, updated: 342, elapsed: 42, counts: null },
      { id: "bad", state: "completed", created: 400, updated: 442, elapsed: 42, counts: { steps: -1, retries: 0, waited: 0 } },
      { id: "stopped", state: "stopped", created: 500, updated: 512, elapsed: 12, counts: null },
      { id: "busy", state: "running", created: 600, counts: { steps: 3, retries: 0, waited: 0 } },
    ],
    messages: [],
  });
  eq(A.receipt(s6.record("worked")), "Worked for 42 s · 6 steps · waited 1 min 05 s for you · 1 retry", "after a reload the receipt is not 'Thought for' when the run had steps");
  eq(A.receipt(s6.record("thought")), "Thought for 11 s", "a run with no steps still reads Thought for");
  eq(A.receipt(s6.record("unknown")), "Took 42 s", "no rows and no counts: neutral words");
  eq(A.receipt(s6.record("bad")), "Took 42 s", "malformed counts are not believed");
  eq(A.receipt(s6.record("stopped")), "Stopped after 12 s", "endings keep their words");
  eq(s6.record("busy").counts, undefined, "a live run's counts are not adopted; its rows arrive live");
  // Rows, once known, are what the receipt counts.
  s6.record("worked").host = true;
  s6.record("worked").items = [item({ seq: 1, status: "done" })];
  eq(A.receipt(s6.record("worked")), "Worked for 42 s · 1 step");
  eq([A.parseCounts({ steps: 2, retries: 0, waited: 1.5 }), A.parseCounts({ steps: 1.5, retries: 0, waited: 0 }), A.parseCounts([]), A.parseCounts("6")],
    [{ steps: 2, retries: 0, waited: 1.5 }, null, null, null]);
}
ok(A.isUnsupported(new Error("Pebble can't do that from this window.")), "the shell's Phase 24 refusal turns the read off quietly");
// Phase 24 (V1-159): after 8 s with nothing reported, the status line's secondary text says the model has not answered.
{
  const live = (extra) => settled({ state: "running", endedAt: null, startedAt: 1000, ...extra });
  eq([A.waitingLong(live({}), 8999, false), A.waitingLong(live({}), 9000, false)], [false, true]);
  eq(A.waitingLong(live({ hostStart: 5000 }), 9000, false), false, "the host's start wins");
  eq([A.waitingLong(live({}), 20000, true), A.waitingLong(live({ host: true, items: [item()] }), 20000, false), A.waitingLong(live({ reasoning: "x" }), 20000, false)], [false, false, false], "a reply, a step or a summary is an event");
  eq(A.waitingLong(settled({}), 20000, false), false, "only a running run waits");
  eq(A.WAITING_TEXT, "Waiting for the model to respond");
}

// ---- Phase 45 (F129, audit round R4): a run resumed after an approval is timed whole ----
{
  // "Worked for 7 s" for 37 s of work: a chat read made while the run waited answered after the approval resumed it
  let t = 5_000_000;
  const s9 = A.createActivityStore({ now: () => t });
  const e9 = (event, data) => s9.accept({ event, data: { chat_id: "c9", ...data } });
  e9("run", { run_id: "r9", state: "running" });
  t += 7000;
  e9("run", { run_id: "r9", state: "waiting_approval", elapsed: 7 });
  eq(s9.record("r9").elapsed, 7, "a settled event's host seconds are the receipt's");
  t += 17500;
  e9("run", { run_id: "r9", state: "queued" });
  e9("run", { run_id: "r9", state: "running" });
  s9.seedChat({ id: "c9", runs: [{ id: "r9", state: "waiting_approval", created: 5000, updated: 5007, elapsed: 7 }], messages: [] });
  eq([s9.record("r9").state, s9.record("r9").elapsed, s9.record("r9").endedAt], ["running", null, null], "a chat read from before the resume changes nothing the window saw since");
  t += 30000;
  e9("run", { run_id: "r9", state: "completed", elapsed: 37 });
  eq(A.receipt(s9.record("r9")), "Thought for 37 s", "the host's seconds for the whole run, not the last stretch (30 s) or the first (7 s)");
  e9("run", { run_id: "r9b", state: "running" });
  t += 4000;
  e9("run", { run_id: "r9b", state: "completed" });
  eq(A.settledSeconds(s9.record("r9b")), 4, "an event without the host's seconds keeps the window's clock");
  e9("run", { run_id: "r9c", state: "failed", elapsed: -3 });
  eq(s9.record("r9c").elapsed, null, "a malformed elapsed is ignored");
}
{
  // An activity read made while the run waited, answered after it resumed: rows count, state and times do not, and the
  // new stretch is read afresh
  let t = 6_000_000;
  const resolvers = [];
  const s10 = A.createActivityStore({ now: () => t, fetch: () => new Promise((resolve) => resolvers.push(resolve)) });
  const e10 = (event, data) => s10.accept({ event, data: { chat_id: "c10", ...data } });
  e10("run", { run_id: "r10", state: "running" });
  e10("run", { run_id: "r10", state: "waiting_approval", elapsed: 7 });
  const first = s10.load("r10");
  e10("run", { run_id: "r10", state: "queued" });
  e10("run", { run_id: "r10", state: "running" });
  resolvers.shift()({ run_id: "r10", state: "waiting_approval", started: 6000, elapsed: 7, next: 1,
    items: [{ seq: 1, at: 0.5, kind: "tool", label: "Opening Wikipedia", detail: null, duration: 0.1, status: "done" }] });
  await new Promise((resolve) => setTimeout(resolve, 0));
  const r10 = s10.record("r10");
  eq([r10.state, r10.elapsed, r10.loaded, A.rows(r10).length], ["running", null, false, 1], "the stale reply's rows are kept; its state and times are not");
  eq(resolvers.length, 1, "the stretch that began meanwhile is read afresh");
  t += 1000;
  resolvers.shift()({ run_id: "r10", state: "running", started: 6000, elapsed: 8, next: 1, items: [] });
  await first;
  eq([r10.state, r10.loaded, r10.hostStart], ["running", true, t - 8000], "the fresh reply fixes the live stretch's start");
}

// ---- bounds: at most 48 records, live ones kept ----
const s4 = A.createActivityStore({ now: () => now });
s4.accept({ event: "run", data: { chat_id: "c", run_id: "keep", state: "running" } });
for (let i = 0; i < 60; i++) s4.accept({ event: "run", data: { chat_id: "c" + i, run_id: "r" + i, state: "completed" } });
ok(s4.state.runs.size <= 48 && s4.record("keep"), "records are bounded and a live run is never dropped");
console.log(`Activity fixtures passed: ${checks} checks; contract items clamped and validated, newest 200 rows, 20000-character summaries, live store from host events with the progress fallback, retries as rows, token links, on-demand run_activity with feature detection, history links without guessing, runs resumed after an approval timed whole (F129), bounded records.`);

// Phase 45 (F182, audit round R6): a model turn's finished plumbing is one row, named by its reply
{
  const mk = (seq, kind, label, extra = {}) => A.parseItem({ seq, at: seq, kind, label, status: "done", duration: 1, ...extra });
  const rows45 = [mk(1, "context", "Recalled 3 memories"), mk(2, "model", "Connected to ChatGPT in 2.0 s"), mk(3, "model", "ChatGPT responded"),
    mk(4, "check", "Checked the reply"), mk(5, "tool", "Looking at the app window", { activity: "reading" }),
    mk(6, "context", "Recalled 3 memories"), mk(7, "model", "ChatGPT responded"), mk(8, "check", "A check asked for more evidence"),
    mk(9, "model", "Connecting to ChatGPT…", { status: "running" })];
  const folded = A.groupRows(rows45);
  eq(folded.map((e) => e.type), ["group", "row", "group", "row", "row"], "each turn's plumbing folds; a check with findings and a live row stay");
  eq(folded[0].label, "ChatGPT responded · 4 steps", "named by the reply");
  eq(folded[2].items.length, 2, "the second turn folds its two plumbing rows");
}

