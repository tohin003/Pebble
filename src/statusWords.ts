// Status words for the companion (design study §2.3). Every word names what the
// host says it is doing; synonyms rotate only inside one key, so the meaning
// never drifts while the words vary. The list is Pebble's own. Labels from
// progress events (which can embed untrusted paths) never reach a status word.

export const ACTIVITY_KEYS = [
  "thinking", "writing", "reading", "searching", "editing", "testing",
  "running", "delegating", "viewing", "working", "loading",
] as const;
export const HOST_KEYS = [
  "question", "review", "reconnecting", "blocked", "uncertain", "offline",
  "paused", "error", "sending",
] as const;
export type ActivityKey = (typeof ACTIVITY_KEYS)[number];
export type HostKey = (typeof HOST_KEYS)[number];
export type StatusKey = ActivityKey | HostKey;
// Keys that may appear only while the host reports a live run (§2.1 rows 8–11).
export const LIVE_KEYS: ReadonlySet<string> = new Set(ACTIVITY_KEYS);

export const ROTATE_MS = 3000; // synonym cadence inside one key
export const ANNOUNCE_MS = 10000; // at most one screen-reader announcement per window
export const COALESCE_MS = 250; // progress-driven DOM updates at most 4 Hz

// Lookup tables without a prototype, so "constructor" or "__proto__" never resolves.
const table = <K extends string, T>(entries: Record<K, T>): Record<K, T> =>
  Object.assign(Object.create(null), entries);
const has = (t: Record<string, unknown>, key: unknown): key is string =>
  typeof key === "string" && Object.prototype.hasOwnProperty.call(t, key);

// The browser's copy of backend/pebble/activity_kinds.py, used only when a
// progress event has no host `activity` field. test_activity_kinds.py keeps the
// two tables equal; keep the begin/end markers and one class per line.
// activity-kinds:begin
const TOOL_CLASS_TABLE: Record<string, readonly string[]> = {
  reading: ["Read", "ReadAttachment", "ReadURL", "ReadSource", "ReadProcess", "ReadRunEvidence", "LoadSkill", "LoadPluginTool", "LoadPluginData", "ReadPluginData", "ReadPluginMedia", "ReadGitStatus", "ReadGitDiff", "ReadGitHistory", "ReadGitPlanReceipt", "ReadGitWorktrees", "ReadWorktreeExport", "ListProjectRecipes", "ScreenObserve", "ResearchStatus"],
  searching: ["Glob", "Grep", "SearchFiles", "CodeReferences", "WebSearch", "Weather", "SearchPluginTools", "SearchPluginData", "ResearchPlan"],
  editing: ["Write", "Append", "Edit", "ProposePatch", "IntegrateAgentPatch", "ProposeGitChange", "PreviewWorktreeExport", "IntegrateWorktreeFile"],
  testing: ["RunTests", "RunRecipe"],
  running: ["RunCommand", "StartCommand", "WaitProcess", "StopProcess", "WriteProcessInput", "RunPlan", "StartPreview", "PrepareEnvironment", "RunCode"],
  delegating: ["SpawnAgent", "SpawnWorktreeAgent", "ReadAgentTree", "SendAgentMessage", "WaitForAgents", "DelegateAgentTask", "StopAgent", "RetryAgent", "AskParentQuestion", "AnswerAgentQuestion", "PromoteAgentQuestion", "ReviewAgentResult", "ReadAgentMessage", "ReadAgentPatch", "RequestAgentEffect", "ReadAgentEffect", "ReadAgentEffects", "ExecuteAgentEffect", "DismissAgentEffect"],
  viewing: ["UIRender", "UIUpdate", "RenderMod"],
  thinking: ["AskUserQuestion", "UpdatePlan", "UpdateGoal", "MemoryPropose"],
  working: ["OpenApp", "PluginCall", "FetchPluginData", "ScreenAct"],
};
// activity-kinds:end
// Run states that carry a host key (activity_kinds.HOST).
// run-status:begin
export const RUN_STATUS: Record<string, HostKey> = table<string, HostKey>({
  retry_wait: "reconnecting",
  waiting_input: "question",
  waiting_approval: "review",
  budget_exhausted: "blocked",
  verification_required: "blocked",
  needs_reconciliation: "uncertain",
  paused: "paused",
  failed: "error",
});
// run-status:end

const TOOL_CLASS: Record<string, ActivityKey> = table<string, ActivityKey>(
  Object.fromEntries(
    Object.entries(TOOL_CLASS_TABLE).flatMap(([key, names]) =>
      names.map((name): [string, ActivityKey] => [name, key as ActivityKey]),
    ),
  ),
);

/** Fallback class for a tool name; anything unknown (or invented) is plain "working". */
export function toolClass(tool?: unknown): ActivityKey {
  return has(TOOL_CLASS, tool) ? TOOL_CLASS[tool] : "working";
}

export const STATUS_WORDS: Record<StatusKey, readonly string[]> = table({
  thinking: ["Thinking", "Reasoning it through", "Weighing options", "Working it out", "Joining the dots", "Turning it over"],
  writing: ["Writing", "Drafting a reply", "Putting it into words"],
  reading: ["Reading", "Reading closely"],
  searching: ["Searching", "Looking it up", "Scanning for matches"],
  editing: ["Editing", "Drafting changes", "Shaping the change"],
  testing: ["Testing", "Checking", "Running checks", "Running tests"],
  running: ["Running a command", "Handling a command"],
  // Phase 24 (V1-111): one name for child agents, "agents", the word the Details drawer uses.
  delegating: ["Coordinating agents", "Checking on agents"],
  viewing: ["Building a view", "Laying out a view"],
  working: ["Working", "On it"],
  loading: ["Loading model", "Warming up the model"],
  question: ["Waiting for you", "Needs your answer"],
  review: ["Needs your review", "Waiting for your review"],
  reconnecting: ["Reconnecting"],
  blocked: ["Blocked"],
  uncertain: ["Needs a check"],
  offline: ["Offline"],
  paused: ["Paused"],
  error: ["Hit a problem"],
  sending: ["Sending"],
});

// Stable accessible text: one sentence per key, so a screen reader hears a
// change of activity, never a rotating synonym or an attempt counter.
export const STATUS_LABELS: Record<StatusKey, string> = table({
  thinking: "Pebble is thinking",
  writing: "Pebble is writing a reply",
  reading: "Pebble is reading",
  searching: "Pebble is searching",
  editing: "Pebble is editing",
  testing: "Pebble is testing",
  running: "Pebble is running a command",
  delegating: "Pebble is coordinating agents",
  viewing: "Pebble is building a view",
  working: "Pebble is working",
  loading: "Pebble is loading the model",
  question: "Pebble is waiting for your answer",
  review: "An action needs your review",
  reconnecting: "Pebble is reconnecting to the model",
  blocked: "The task is blocked",
  uncertain: "An outcome needs a check",
  offline: "The local service is offline",
  paused: "The task is paused",
  error: "The task hit a problem",
  sending: "Sending your message",
});
export const FINISHED_LABEL = "Reply finished";

export function isStatusKey(key: unknown): key is StatusKey {
  return has(STATUS_WORDS, key);
}

/** The word to show for `key` at rotation step `tick`; only reconnecting shows a count. */
export function statusWord(key: unknown, tick = 0, rotate = true, attempt?: unknown): string {
  if (!isStatusKey(key)) return "Working";
  const words = STATUS_WORDS[key];
  const step = rotate && Number.isFinite(tick) ? Math.floor(Math.abs(tick)) % words.length : 0;
  const word = words[step];
  return key === "reconnecting" && Number.isInteger(attempt) && (attempt as number) >= 1 && (attempt as number) <= 99
    ? `${word} (try ${attempt})`
    : word;
}

export function statusLabel(key: unknown): string {
  return isStatusKey(key) ? STATUS_LABELS[key] : "Pebble is working";
}

/** Approximate elapsed time; null under a second. Counted by this window, not the run ledger. */
export function formatElapsed(ms: number): string | null {
  if (!Number.isFinite(ms) || ms < 1000) return null;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

/**
 * Exact elapsed time from the host's own run start (Phase 22): "12 s", "1 min 05 s", "1 h 02 min";
 * null under a second. Shown without "~", because the host counted it.
 */
export function formatExactElapsed(ms: number): string | null {
  if (!Number.isFinite(ms) || ms < 1000) return null;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

/** Milliseconds until formatExactElapsed(ms) changes. */
export function exactChangeIn(ms: number): number {
  if (!Number.isFinite(ms) || ms < 0) return 1000;
  if (ms < 3600000) return 1000 - (ms % 1000);
  return 60000 - (ms % 60000);
}

/** Milliseconds until formatElapsed(ms) changes. */
export function elapsedChangeIn(ms: number): number {
  if (!Number.isFinite(ms) || ms < 0) return 1000;
  if (ms < 60000) return 1000 - (ms % 1000);
  return 60000 - (ms % 60000);
}

export interface Timers {
  now(): number;
  set(fn: () => void, ms: number): unknown;
  clear(id: unknown): void;
}
const realTimers = (): Timers => ({
  now: () => Date.now(),
  set: (fn, ms) => setTimeout(fn, ms),
  clear: (id) => clearTimeout(id as ReturnType<typeof setTimeout>),
});

/** Leading and trailing throttle: `apply` runs at most once per `ms`, always with the latest value. */
export function coalesce<T>(apply: (value: T) => void, ms = COALESCE_MS, timers: Timers = realTimers()) {
  let last = -Infinity,
    timer: unknown = null,
    pending: { value: T } | null = null;
  const run = () => {
    timer = null;
    if (!pending) return;
    const { value } = pending;
    pending = null;
    last = timers.now();
    apply(value);
  };
  return {
    push(value: T) {
      pending = { value };
      if (timer !== null) return;
      const wait = last + ms - timers.now();
      if (wait <= 0) run();
      else timer = timers.set(run, wait);
    },
    cancel() {
      if (timer !== null) timers.clear(timer);
      timer = null;
      pending = null;
    },
  };
}

/** Announce a changed sentence at most once per `ms`; a deferred sentence is the latest one, never a stale one. */
export function announcer(apply: (text: string) => void, ms = ANNOUNCE_MS, timers: Timers = realTimers()) {
  let last = -Infinity,
    current = "",
    pending: string | null = null,
    timer: unknown = null;
  const deliver = (text: string) => {
    pending = null;
    if (text === current) return;
    current = text;
    last = timers.now();
    apply(text);
  };
  return {
    say(text: string) {
      if (!text) return;
      if (text === current) {
        pending = null; // a change and its reversal inside the window say nothing
        return;
      }
      if (timer === null && timers.now() - last >= ms) {
        deliver(text);
        return;
      }
      pending = text;
      if (timer === null)
        timer = timers.set(() => {
          timer = null;
          if (pending !== null) deliver(pending);
        }, Math.max(0, last + ms - timers.now()));
    },
    /**
     * Phase 24 (V1-118): say `text` now, outside the window: the once-per-turn "Reply finished", which no later
     * clear() may take back. A sentence still waiting is dropped (it is stale once the reply has finished).
     */
    now(text: string) {
      if (!text) return;
      if (timer !== null) timers.clear(timer);
      timer = null;
      pending = null;
      if (text === current) return;
      current = text;
      last = timers.now();
      apply(text);
    },
    get current() {
      return current;
    },
    /** Empty the region without announcing; the next sentence is a change again. */
    clear() {
      if (timer !== null) timers.clear(timer);
      timer = null;
      pending = null;
      if (current) {
        current = "";
        apply("");
      }
    },
    cancel() {
      if (timer !== null) timers.clear(timer);
      timer = null;
      pending = null;
    },
  };
}
