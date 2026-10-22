// What Pebble is doing during a run, and what it did (Phase 22 activity contract).
//
// Host items arrive through the `activity` and `reasoning` events and the `run_activity` route.
// When neither exists (an older service, or a shell that refuses the route) the timeline is
// rebuilt from the progress events this window already receives, so the disclosure still works
// (feature detection, contract §3). Every string is display data: clamped, stripped of control
// and bidi characters, and rendered as text by the components. Nothing here calls a route
// except `load`, which uses the fetch function the window injects.
import { reactive } from "vue";

export const ITEM_KINDS = ["context", "model", "reasoning", "tool", "retry", "approval", "child", "check", "note"] as const;
export type ItemKind = (typeof ITEM_KINDS)[number];
export type ItemStatus = "running" | "done" | "failed";
export interface ActivityItem {
  seq: number;
  at: number; // seconds since the run started
  kind: ItemKind;
  label: string;
  detail: string | null;
  duration: number | null;
  status: ItemStatus;
  activity?: string | null; // the host's activity class (reading, searching, …) when it sent one
}
// Activity classes (statusWords ACTIVITY_KEYS) a row may carry; only reading and searching fold.
const CLASSES: ReadonlySet<string> = new Set(["thinking", "writing", "reading", "searching", "editing", "testing", "running", "delegating", "viewing", "working", "loading"]);
const RESEARCH_TOOLS: ReadonlySet<string> = new Set(["WebSearch", "ReadURL", "ResearchPlan", "ResearchStatus"]);
export interface RunActivity {
  runId: string;
  chatId: string;
  state: string;
  startedAt: number | null; // epoch ms of the live stretch (host `started`, or this window's first `running`)
  endedAt: number | null; // epoch ms when this window saw the run settle
  elapsed: number | null; // host seconds, when the host reported them
  items: ActivityItem[]; // host items, by seq
  local: ActivityItem[]; // fallback rows from progress events
  dropped: number; // older rows beyond the window
  reasoning: string; // reasoning summary text (display only)
  reasoningClipped: boolean;
  attempt: string | null;
  next: number; // run_activity cursor
  host: boolean; // host items were seen for this run
  loaded: boolean; // run_activity answered for the current live stretch
  error: string;
  hostStart: number | null; // epoch ms the host's own clock puts the run's start at (exact elapsed time)
  delegated: boolean; // a delegation step was seen
  researched: boolean; // a web research step was seen
  // Phase 24 (V1-121): the host's per-run counts from get_chat, used while no rows are known (a reopened
  // conversation). null: the host said it could not count them; absent: a host that does not report counts.
  counts?: Counts | null;
}
export interface Counts {
  steps: number;
  retries: number;
  waited: number; // seconds spent waiting for the person
}
/** The host's per-run counts ({steps, retries, waited}), or null when the value is not exactly that shape. */
export function parseCounts(value: unknown): Counts | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const r = value as Record<string, unknown>;
  const whole = (v: unknown) => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
  if (!whole(r.steps) || !whole(r.retries) || typeof r.waited !== "number" || !Number.isFinite(r.waited) || r.waited < 0) return null;
  return { steps: r.steps as number, retries: r.retries as number, waited: r.waited };
}

export const LIMITS = Object.freeze({ label: 80, detail: 2000, reasoning: 20000, items: 200, runs: 48 });
const LIVE: ReadonlySet<string> = new Set(["queued", "running", "retry_wait"]);
const WORK: ReadonlySet<string> = new Set(["tool", "check", "child", "approval"]);
// C0/C1 controls (tab and line feed are handled per field), line and paragraph separators and bidi
// overrides. Built from code points, so this source holds no invisible characters.
const hex = (n: number) => "\\u" + n.toString(16).padStart(4, "0");
const CONTROL = new RegExp(
  "[" + [[0x00, 0x08], [0x0b, 0x1f], [0x7f, 0x9f], [0x2028, 0x2029], [0x202a, 0x202e], [0x2066, 0x2069]].map(([a, b]) => hex(a) + "-" + hex(b)).join("") + "]",
  "g",
);
const ID = /^[A-Za-z0-9_-]{1,64}$/;

export const isLiveState = (state: unknown) => typeof state === "string" && LIVE.has(state);
export const validId = (value: unknown): value is string => typeof value === "string" && ID.test(value);

/** Display text: controls removed, one line unless `multiline`, at most `max` characters. */
export function clampText(value: unknown, max: number, multiline = false): string {
  if (typeof value !== "string") return "";
  let text = value.replace(CONTROL, " ");
  text = multiline ? text.replace(/\t/g, " ").trim() : text.replace(/\s+/g, " ").trim();
  return text.length > max ? text.slice(0, Math.max(0, max - 1)).trimEnd() + "…" : text;
}

const finite = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : null);

/** One host item, or null when it is not one. Unknown kinds are notes; unknown statuses are done. */
export function parseItem(raw: unknown): ActivityItem | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const seq = r.seq;
  if (typeof seq !== "number" || !Number.isSafeInteger(seq) || seq < 0) return null;
  const label = clampText(r.label, LIMITS.label);
  if (!label) return null;
  const kind = typeof r.kind === "string" && (ITEM_KINDS as readonly string[]).includes(r.kind) ? (r.kind as ItemKind) : "note";
  const status: ItemStatus = r.status === "running" || r.status === "failed" ? r.status : "done";
  const at = finite(r.at), duration = finite(r.duration);
  const detail = clampText(r.detail, LIMITS.detail, true);
  const activity = typeof r.activity === "string" && CLASSES.has(r.activity) ? r.activity : null;
  return { seq, at: at !== null && at >= 0 ? at : 0, kind, label, detail: detail || null, duration: duration !== null && duration >= 0 ? duration : null, status, activity };
}

/** Upsert by seq, ordered, keeping the newest LIMITS.items rows. */
export function mergeItems(list: readonly ActivityItem[], incoming: readonly ActivityItem[]): { items: ActivityItem[]; dropped: number } {
  const bySeq = new Map(list.map((item) => [item.seq, item] as const));
  for (const item of incoming) bySeq.set(item.seq, item);
  const sorted = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  const over = Math.max(0, sorted.length - LIMITS.items);
  return { items: over ? sorted.slice(over) : sorted, dropped: over };
}

/** Reasoning summary text so far, capped at LIMITS.reasoning characters (the head is kept). */
export function appendReasoning(text: string, delta: unknown): { text: string; clipped: boolean } {
  if (typeof delta !== "string" || !delta) return { text, clipped: false };
  const next = text + delta.replace(CONTROL, (c) => (c === "\r" ? "" : " "));
  return next.length > LIMITS.reasoning ? { text: next.slice(0, LIMITS.reasoning), clipped: true } : { text: next, clipped: false };
}

/** The latest bold heading line of a reasoning summary ("**Checking the name**"), if any. */
export function reasoningHeadline(text: string): string | null {
  let last: string | null = null;
  for (const match of text.matchAll(/^[ \t]*\*\*([^*\n]{2,120})\*\*[ \t]*$/gm)) last = match[1];
  return last ? clampText(last, LIMITS.label) || null : null;
}

/** "12 s", "1 min 05 s", "2 min", "2 h 03 min" (the status line's exact style); null when unknown. */
export function formatDuration(seconds: number | null | undefined): string | null {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 1) return "<1 s";
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60), rest = s % 60;
  if (m < 60) return rest ? `${m} min ${String(rest).padStart(2, "0")} s` : `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

/** A row's offset from the run's start: "+3.2 s", "+1 min 05 s". */
export function formatOffset(at: number): string {
  if (!Number.isFinite(at) || at < 0) return "";
  return at < 60 ? `+${at.toFixed(1)} s` : "+" + formatDuration(at);
}

export function rows(record: RunActivity): ActivityItem[] {
  return record.host ? record.items : record.local;
}

/** Seconds the run took, from the host when it said so, else from this window's clock. */
export function settledSeconds(record: RunActivity): number | null {
  if (record.elapsed !== null) return record.elapsed;
  if (record.startedAt !== null && record.endedAt !== null) return Math.max(0, (record.endedAt - record.startedAt) / 1000);
  return null;
}

const ENDINGS: Record<string, string> = Object.assign(Object.create(null), {
  stopped: "Stopped after", failed: "Hit a problem after", interrupted: "Interrupted after",
  paused: "Paused after", budget_exhausted: "Reached its limit after", needs_reconciliation: "Needs a check after",
});

/** What the receipt counts: tool steps, retries and the time spent waiting for the person. The rows when any are
 * known; otherwise the host's own counts for the run (Phase 24, V1-121: a reopened conversation). */
export function tally(record: RunActivity): { steps: number; retries: number; waited: number } {
  const list = rows(record);
  if (!list.length && record.counts) return { ...record.counts };
  let steps = 0, retries = 0, waited = 0;
  for (const item of list) {
    if (WORK.has(item.kind) && item.kind !== "approval") steps++;
    if (item.kind === "retry") retries++;
    if (item.kind === "approval" && item.duration !== null) waited += item.duration;
  }
  return { steps, retries, waited };
}

/**
 * The one-line receipt above a reply (design study §2.3): "Thought for 11 s", "Worked for 42 s · 6
 * steps · waited 1 min for you · 1 retry", "Stopped after 12 s", "Hit a problem after 30 s". A run
 * under a second with no step, retry or wait gets none (null): a "hi" gets no chrome.
 */
export function receipt(record: RunActivity): string | null {
  const seconds = settledSeconds(record);
  const { steps, retries, waited } = tally(record);
  const quiet = !steps && !retries && !waited && !ENDINGS[record.state];
  if (quiet && (seconds === null || seconds < 1)) return null; // no invented duration, no chrome for a "hi"
  const duration = formatDuration(seconds);
  // Phase 24 (V1-121): with no rows and a host that could not count the run, neither "Worked" nor "Thought" is known.
  const unknown = !rows(record).length && record.counts === null;
  const lead = ENDINGS[record.state] || (unknown ? "Took" : steps ? "Worked for" : "Thought for");
  const head = duration ? `${lead} ${duration}` : steps ? "Worked" : ENDINGS[record.state] ? lead.replace(/ (after|for)$/, "") : "Thought";
  const parts = [head];
  if (steps) parts.push(`${steps} ${steps === 1 ? "step" : "steps"}`);
  if (waited >= 1) parts.push(`waited ${formatDuration(waited)} for you`);
  if (retries) parts.push(`${retries} ${retries === 1 ? "retry" : "retries"}`);
  return parts.join(" · ");
}
/** The receipt, or a plain name for the disclosure when there is no receipt. */
export function summaryTitle(record: RunActivity): string {
  return receipt(record) || "What Pebble did";
}

export type Entry =
  | { type: "row"; item: ActivityItem }
  | { type: "group"; key: number; items: ActivityItem[]; label: string };
const FOLD: ReadonlySet<string> = new Set(["reading", "searching"]);
const foldable = (item: ActivityItem) => item.kind === "tool" && item.status === "done" && !!item.activity && FOLD.has(item.activity);

/**
 * Timeline rows with low-stakes steps folded: two or more consecutive finished reads and searches
 * become "Explored · read 3 files, 2 searches". Edits, commands, tests, delegation, views, retries,
 * waits, withdrawn drafts and failures are never folded: they are the steps people check.
 */
export function groupRows(list: readonly ActivityItem[]): Entry[] {
  const out: Entry[] = [];
  let run: ActivityItem[] = [];
  const flush = () => {
    if (run.length >= 2) {
      const reads = run.filter((i) => i.activity === "reading").length, searches = run.length - reads;
      const words = [reads ? `read ${reads} ${reads === 1 ? "file" : "files"}` : "", searches ? `${searches} ${searches === 1 ? "search" : "searches"}` : ""];
      out.push({ type: "group", key: run[0].seq, items: run, label: "Explored · " + words.filter(Boolean).join(", ") });
    } else run.forEach((item) => out.push({ type: "row", item }));
    run = [];
  };
  for (const item of list) {
    if (foldable(item)) run.push(item);
    else {
      flush();
      out.push({ type: "row", item });
    }
  }
  flush();
  return out;
}

/**
 * Secondary text for the live status line (design study §2.3, §3.3): the step the host reports as
 * running, or, while the model itself is working (or no step runs), the latest reasoning-summary
 * headline. It never chooses the status word, which stays the host key's.
 */
export function liveDetail(record: RunActivity | null | undefined): string | null {
  if (!record || !isLiveState(record.state)) return null;
  const list = rows(record);
  const headline = reasoningHeadline(record.reasoning);
  for (let i = list.length - 1; i >= 0; i--) {
    const item = list[i];
    if (item.status !== "running") continue;
    return (item.kind === "reasoning" || item.kind === "model") && headline ? headline : item.label;
  }
  return headline;
}

/** Phase 24 (V1-159, design study §3.3 item 5): the secondary text once a live run has gone 8 s with nothing reported. */
export const WAITING_AFTER_MS = 8000;
export const WAITING_TEXT = "Waiting for the model to respond";
/** Whether a running run has shown nothing yet (no step, no summary, no reply text) for WAITING_AFTER_MS. */
export function waitingLong(record: RunActivity | null | undefined, now: number, replied: boolean): boolean {
  if (!record || record.state !== "running" || replied || rows(record).length || record.reasoning) return false;
  const start = record.hostStart ?? record.startedAt;
  return start !== null && now - start >= WAITING_AFTER_MS;
}

/**
 * How much of its window the task's last request used, from the host's `context_inspect` reply
 * (the Context tab's own data): null unless the reply is for this chat and run and names a window.
 */
export function contextShare(reply: unknown, chatId: string, runId: string): { used: number; window: number; percent: number } | null {
  if (!reply || typeof reply !== "object" || Array.isArray(reply)) return null;
  const r = reply as Record<string, unknown>;
  if (r.chat_id !== chatId || r.run_id !== runId) return null;
  const whole = (v: unknown) => (typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : null);
  const used = whole(r.used) ?? whole(r.total);
  const window = r.window && typeof r.window === "object" ? whole((r.window as Record<string, unknown>).tokens) : null;
  if (used === null || !window) return null;
  return { used, window, percent: Math.min(999, Math.round((used / window) * 100)) };
}
export const RING_AT = 60; // percent of the window at which the composer shows the ring

/** A service without the route ("Unknown request.") or a shell that refuses it ("Invalid local request.", and since
 * Phase 24, V1-108, "Pebble can't do that from this window."). */
export function isUnsupported(error: unknown): boolean {
  const text = error instanceof Error ? error.message : String(error);
  // Grouped so the literal never reads as a slash command in shipped UI text (test_runtime_commands scans it).
  return /(?:unknown request|invalid local request|unknown method|not allowed|can't do that from this window)/i.test(text);
}

function blank(runId: string, chatId: string): RunActivity {
  return {
    runId, chatId, state: "", startedAt: null, endedAt: null, elapsed: null, items: [], local: [], dropped: 0,
    reasoning: "", reasoningClipped: false, attempt: null, next: 0, host: false, loaded: false, error: "",
    hostStart: null, delegated: false, researched: false,
  };
}

export interface ActivityStoreOptions {
  now?: () => number;
  fetch?: (runId: string, after: number) => Promise<unknown>;
}
interface PastRun { id?: unknown; state?: unknown; created?: unknown; updated?: unknown; elapsed?: unknown; counts?: unknown }
interface PastMessage { id?: unknown; role?: unknown; created?: unknown; run_id?: unknown }

export function createActivityStore(options: ActivityStoreOptions = {}) {
  const now = options.now || (() => Date.now());
  const state = reactive({
    runs: new Map<string, RunActivity>(),
    messages: new Map<string, string>(), // assistant message id -> run id
    live: new Map<string, string>(), // chat id -> live run id
    supported: null as boolean | null, // run_activity: null unknown, false refused by this service or shell
    events: false, // host activity events reached this window
  });
  const pending = new Set<string>();

  function ensure(runId: string, chatId: string): RunActivity {
    let record = state.runs.get(runId);
    if (!record) {
      if (state.runs.size >= LIMITS.runs) {
        // Forget the oldest settled record (a live one is never dropped).
        for (const [id, old] of state.runs)
          if (!isLiveState(old.state)) {
            state.runs.delete(id);
            for (const [message, run] of state.messages) if (run === id) state.messages.delete(message);
            break;
          }
      }
      state.runs.set(runId, blank(runId, chatId));
      record = state.runs.get(runId)!;
    }
    return record;
  }
  const since = (record: RunActivity) => (record.startedAt === null ? 0 : Math.max(0, (now() - record.startedAt) / 1000));

  function settle(record: RunActivity) {
    if (record.endedAt === null && record.startedAt !== null) record.endedAt = now();
    for (const [chat, run] of state.live) if (run === record.runId) state.live.delete(chat);
  }

  // Fallback rows that only mark a phase (preparing, reconnecting) end when the run moves on: a new
  // attempt runs, text streams, or the run settles. Tool rows keep their own done events.
  function finishPhases(record: RunActivity) {
    if (!record.local.some((i) => i.status === "running" && (i.kind === "context" || i.kind === "retry"))) return;
    const at = since(record);
    record.local = record.local.map((i) =>
      i.status === "running" && (i.kind === "context" || i.kind === "retry") ? { ...i, status: "done", duration: Math.max(0, at - i.at) } : i,
    );
  }

  function fallback(record: RunActivity, data: Record<string, unknown>) {
    const label = clampText(data.label, LIMITS.label);
    if (!label) return;
    const tool = typeof data.tool === "string" ? data.tool : "";
    const activity = typeof data.activity === "string" && CLASSES.has(data.activity) ? data.activity : null;
    if (activity === "delegating") record.delegated = true;
    if (RESEARCH_TOOLS.has(tool)) record.researched = true;
    const attempt = typeof data.attempt === "number" && Number.isInteger(data.attempt) && data.attempt >= 1 && data.attempt <= 99 ? data.attempt : null;
    const kind: ItemKind = attempt !== null ? "retry" : tool ? (data.activity === "testing" ? "check" : "tool") : data.kind === "review" ? "note" : "context";
    const list = record.local.slice();
    if (data.kind === "done" && tool) {
      for (let i = list.length - 1; i >= 0; i--)
        if (list[i].status === "running" && list[i].label === label) {
          list[i] = { ...list[i], status: "done", duration: Math.max(0, since(record) - list[i].at) };
          record.local = list;
          return;
        }
    }
    // A new step ends the previous phase row: the host moved on.
    for (let i = 0; i < list.length; i++)
      if (list[i].status === "running" && (list[i].kind === "context" || list[i].kind === "retry"))
        list[i] = { ...list[i], status: "done", duration: Math.max(0, since(record) - list[i].at) };
    const shown = attempt !== null ? clampText(`${label} (attempt ${attempt})`, LIMITS.label) : label;
    const status: ItemStatus = data.kind === "blocked" ? "failed" : data.kind === "working" ? "running" : "done";
    const seq = (list.at(-1)?.seq ?? -1) + 1;
    list.push({ seq, at: since(record), kind, label: shown, detail: null, duration: null, status, activity: tool ? activity : null });
    record.local = list.length > LIMITS.items ? list.slice(list.length - LIMITS.items) : list;
  }

  function accept(event: { event: string; data: any }) {
    const d = event && event.data && typeof event.data === "object" ? (event.data as Record<string, unknown>) : null;
    if (!d) return;
    const chatId = typeof d.chat_id === "string" ? d.chat_id : "";
    // An event without a run id belongs to the chat's live run; a malformed id belongs to none.
    const runId = d.run_id === undefined || d.run_id === null ? (chatId ? state.live.get(chatId) : undefined) : validId(d.run_id) ? d.run_id : undefined;
    switch (event.event) {
      case "run": {
        if (!validId(d.run_id) || typeof d.state !== "string") return;
        const record = ensure(d.run_id, chatId);
        const wasLive = isLiveState(record.state);
        record.state = d.state;
        if (isLiveState(d.state)) {
          // A new live stretch (new, or resumed): its time is counted afresh, and the host's record is
          // read again once (its start and active seconds), so the elapsed time stays exact.
          if (!wasLive) Object.assign(record, { endedAt: null, startedAt: null, elapsed: null, hostStart: null, loaded: false });
          if (d.state === "running" && record.startedAt === null) record.startedAt = now();
          if (d.state === "running" && wasLive) finishPhases(record); // e.g. a reconnect succeeded
          if (chatId) state.live.set(chatId, d.run_id);
        } else {
          finishPhases(record);
          settle(record);
        }
        return;
      }
      case "activity": {
        if (!runId) return;
        const item = parseItem(d.item);
        if (!item) return;
        const record = ensure(runId, chatId);
        const merged = mergeItems(record.items, [item]);
        record.items = merged.items;
        record.dropped += merged.dropped;
        record.host = true;
        if (item.activity === "delegating" || item.kind === "child") record.delegated = true;
        // An item's offset is on the host's clock: it places the run's start to within transport delay.
        if (record.hostStart === null && isLiveState(record.state)) record.hostStart = now() - item.at * 1000;
        state.events = true;
        return;
      }
      case "reasoning": {
        if (!runId || typeof d.delta !== "string") return;
        const record = ensure(runId, chatId);
        const attempt = typeof d.attempt_id === "string" ? d.attempt_id.slice(0, 64) : null;
        let text = record.reasoning;
        if (attempt && record.attempt && attempt !== record.attempt && text) text += "\n\n";
        if (attempt) record.attempt = attempt;
        const next = appendReasoning(text, d.delta);
        record.reasoning = next.text;
        record.reasoningClipped ||= next.clipped;
        state.events = true;
        return;
      }
      case "progress": {
        if (!runId) return;
        const record = state.runs.get(runId);
        if (record && isLiveState(record.state)) fallback(record, d);
        return;
      }
      case "token": {
        const live = chatId ? state.live.get(chatId) : undefined;
        if (live && typeof d.id === "string" && !state.messages.has(d.id)) state.messages.set(d.id, live);
        const record = live ? state.runs.get(live) : undefined;
        if (record) finishPhases(record); // the reply is streaming
        return;
      }
      case "idle": {
        const record = runId ? state.runs.get(runId) : undefined;
        if (record) settle(record);
        return;
      }
    }
  }

  function link(messageId: string, runId: string) {
    if (typeof messageId === "string" && messageId && validId(runId)) state.messages.set(messageId, runId);
  }
  function forMessage(messageId: string): RunActivity | null {
    const run = state.messages.get(messageId);
    return run ? state.runs.get(run) || null : null;
  }
  const record = (runId: string | null | undefined) => (runId ? state.runs.get(runId) || null : null);
  const liveFor = (chatId: string | null | undefined) => (chatId ? record(state.live.get(chatId)) : null);

  /**
   * Restore links for a chat opened from history. A message the host tags with `run_id` is linked
   * directly; otherwise a settled run claims the last assistant message written inside its own
   * time window, when exactly one run's window holds it. Elapsed seconds come from the run ledger.
   */
  function seedChat(chat: { id: string; runs?: readonly PastRun[]; messages: readonly PastMessage[] }) {
    // The newest runs only (get_chat lists them newest first), so a long history never churns the records.
    const runs = (chat.runs || []).slice(0, 20).filter((r) => validId(r.id) && typeof r.state === "string");
    for (const run of runs) {
      const record = ensure(run.id as string, chat.id);
      if (!record.state) record.state = run.state as string;
      if (record.elapsed === null && finite(run.elapsed) !== null && !isLiveState(run.state)) record.elapsed = finite(run.elapsed);
      // V1-121: the host's counts for a settled run (null when it said it could not count them).
      if (Object.prototype.hasOwnProperty.call(run, "counts") && !isLiveState(run.state)) record.counts = parseCounts(run.counts);
      if (record.startedAt === null && finite(run.created) !== null) record.startedAt = (run.created as number) * 1000;
      if (isLiveState(run.state)) state.live.set(chat.id, run.id as string);
      else if (record.endedAt === null && finite(run.updated) !== null) record.endedAt = (run.updated as number) * 1000;
    }
    const assistant = chat.messages.filter((m) => m.role === "assistant" && typeof m.id === "string");
    for (const message of assistant) if (validId(message.run_id) && state.runs.has(message.run_id)) link(message.id as string, message.run_id);
    const windows = runs
      .filter((r) => !isLiveState(r.state) && finite(r.created) !== null && finite(r.updated) !== null)
      .map((r) => ({ id: r.id as string, from: r.created as number, to: (r.updated as number) + 1 }));
    const claimed = new Set(state.messages.values());
    for (const run of windows) {
      if (claimed.has(run.id)) continue;
      const inside = assistant.filter((m) => {
        const t = finite(m.created);
        return t !== null && t >= run.from && t <= run.to && !state.messages.has(m.id as string);
      });
      const last = inside.at(-1);
      if (!last) continue;
      const t = last.created as number;
      if (windows.filter((w) => t >= w.from && t <= w.to).length !== 1) continue;
      link(last.id as string, run.id);
      claimed.add(run.id);
    }
  }

  /** Read the host's record once (on demand); a refused route turns the feature off for this window. */
  async function load(runId: string): Promise<RunActivity | null> {
    const record = state.runs.get(runId);
    if (!record || !options.fetch || state.supported === false || pending.has(runId)) return record || null;
    pending.add(runId);
    try {
      const reply = (await options.fetch(runId, record.next)) as Record<string, unknown> | null;
      if (!reply || typeof reply !== "object" || reply.run_id !== runId) throw new Error("The activity reply belongs to another task.");
      state.supported = true;
      const items = (Array.isArray(reply.items) ? reply.items : []).map(parseItem).filter((i): i is ActivityItem => !!i);
      const merged = mergeItems(record.items, items);
      record.items = merged.items;
      record.dropped += merged.dropped;
      if (items.length) record.host = true;
      const reasoning = reply.reasoning as Record<string, unknown> | null | undefined;
      if (reasoning && typeof reasoning.text === "string" && reasoning.text.length >= record.reasoning.length) {
        const text = appendReasoning("", reasoning.text);
        record.reasoning = text.text;
        record.reasoningClipped = text.clipped;
      }
      if (typeof reply.state === "string" && reply.state) record.state = reply.state;
      const started = finite(reply.started);
      if (started !== null && started > 0) record.startedAt = started * 1000;
      const elapsed = finite(reply.elapsed);
      if (elapsed !== null && elapsed >= 0 && !isLiveState(record.state)) record.elapsed = elapsed;
      // While live, the host's own seconds (which leave out waits for the person) fix the start.
      if (isLiveState(record.state))
        record.hostStart = elapsed !== null && elapsed >= 0 ? now() - elapsed * 1000 : started !== null && started > 0 ? started * 1000 : record.hostStart;
      if (items.some((i) => i.activity === "delegating" || i.kind === "child")) record.delegated = true;
      const next = reply.next;
      if (typeof next === "number" && Number.isSafeInteger(next) && next >= record.next) record.next = next;
      if (!isLiveState(record.state)) for (const [chat, run] of state.live) if (run === runId) state.live.delete(chat);
      record.loaded = true;
      record.error = "";
    } catch (error) {
      if (isUnsupported(error)) state.supported = false;
      else record.error = clampText(error instanceof Error ? error.message : String(error), 200);
    } finally {
      pending.delete(runId);
    }
    return record;
  }

  return { state, accept, link, forMessage, record, liveFor, seedChat, load };
}
export type ActivityStore = ReturnType<typeof createActivityStore>;
