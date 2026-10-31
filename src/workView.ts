// Phase 46 (1.9.29, D206, w24-genui-a): the work view, Pebble's work drawn as cards (pebble-ui-spec v1).
//
// Each run_activity item may carry a `view` the host built from its own records (backend/pebble/work_view.py). This
// module checks a view against the spec's fields and bounds (SPEC, which mirrors work_view.SPEC; the tests compare
// the two through docs/ui-spec), groups a run's items into cards, and words the header, the summary and the
// companion's strip. A view of a type this window does not know, or one that fails its check, is dropped, and its item
// shows as today's row of text (the fallback rule). Every string is display data: clamped, stripped of controls and
// rendered as text, never as a link or markup. Nothing here calls a route.
import { clampText, formatDuration, groupRows, type ActivityItem, type Entry, type RunActivity } from "./activity";

export const VIEW_VERSION = 1;
export const STATES = ["waiting", "running", "done", "failed", "declined", "held", "undone", "stopped"] as const;
export type State = (typeof STATES)[number];
type Field =
  | readonly ["text", number]
  | readonly ["int"]
  | readonly ["num"]
  | readonly ["bool"]
  | readonly ["enum", readonly string[]]
  | readonly ["list", number, Field]
  | readonly ["object", Readonly<Record<string, Field>>];
const text = (n: number) => ["text", n] as const;
const int = ["int"] as const, num = ["num"] as const, bool = ["bool"] as const;
const state = ["enum", STATES] as const;
// The view types and their fields, as work_view.SPEC defines them (WorkView.test.mjs holds the two equal).
export const SPEC: Readonly<Record<string, Readonly<Record<string, Field>>>> = {
  read: { path: text(200), start: int, total: int, private: bool },
  search: { tool: ["enum", ["Grep", "Glob", "SearchFiles", "CodeReferences"]], query: text(120), hits: int },
  web: { query: text(120), sites: ["list", 5, text(120)] },
  edit: { path: text(200), added: int, removed: int, new: bool, private: bool, state, action_id: text(64) },
  command: { command: text(400), where: text(80), network: bool, state, exit: int, seconds: num, tail: ["list", 8, text(160)], cut: bool, action_id: text(64) },
  tests: { recipe: text(40), state, passed: int, failed: int, skipped: int, errors: int, total: int, failing: ["list", 10, text(160)], exit: int, seconds: num, action_id: text(64) },
  git: { op: text(24), summary: text(240), branch: text(120), upstream: text(120), ahead: int, behind: int, changed: int, commit: text(12), push_to: text(64), destructive: bool, publication: bool, state, action_id: text(64) },
  window: {
    app: text(64), title: text(60), verb: text(40), target: text(60), before: text(40), after: text(40),
    verdict: ["enum", ["happened", "nothing_changed", "uncertain", "refused"]],
    picture: ["object", { ref: text(64), width: int, height: int }], point: ["object", { x: num, y: num }],
  },
  agent: { name: text(60), state, depth: int, parent: text(64), seconds: num, tokens: int },
  checks: {
    verdict: text(80), tests: ["object", { runner: text(40), passed: int, failed: int, current: bool }], files_changed: int, issues: int, repairable: bool,
    requirements: ["list", 10, ["object", { text: text(160), state: ["enum", ["met", "open"]] }]], unmet: int, repaired: bool,
  },
  review: { action_id: text(64), action: text(24), state, decided_by: ["enum", ["you", "pebble", "mode"]], quote: text(200) },
  question: { questions: int, state },
};
export type View = { v: 1; type: string; more?: boolean; [field: string]: unknown };

function value(field: Field, raw: unknown): unknown {
  switch (field[0]) {
    case "text": {
      const shown = clampText(raw, field[1]);
      return shown || undefined;
    }
    case "int":
      return typeof raw === "number" && Number.isSafeInteger(raw) && raw >= 0 ? raw : undefined;
    case "num":
      return typeof raw === "number" && Number.isFinite(raw) && raw >= 0 ? raw : undefined;
    case "bool":
      return typeof raw === "boolean" ? raw : undefined;
    case "enum":
      return typeof raw === "string" && field[1].includes(raw) ? raw : undefined;
    case "list":
      if (!Array.isArray(raw)) return undefined;
      return raw.slice(0, field[1]).map((entry) => value(field[2], entry)).filter((entry) => entry !== undefined);
    case "object":
      return record(field[1], raw);
  }
}
function record(fields: Readonly<Record<string, Field>>, raw: unknown): Record<string, unknown> | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const r = raw as Record<string, unknown>, out: Record<string, unknown> = {};
  for (const key of Object.keys(fields)) {
    if (!Object.prototype.hasOwnProperty.call(r, key)) continue;
    const parsed = value(fields[key], r[key]);
    if (parsed !== undefined) out[key] = parsed;
  }
  return out;
}
/** A view the spec describes, with only its known fields, each checked and clamped; null for anything else (the item
 *  then shows as its row of text). */
export function parseView(raw: unknown): View | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  if (r.v !== VIEW_VERSION || typeof r.type !== "string" || !Object.prototype.hasOwnProperty.call(SPEC, r.type)) return null;
  const fields = record(SPEC[r.type], r);
  return fields ? { v: 1, type: r.type, ...(r.more === true ? { more: true } : {}), ...fields } : null;
}

// ---- words ----
const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;
/** Rounded counts: 980, 1.2K, 52K, 1.3M. */
export function round(n: number): string {
  if (!Number.isFinite(n)) return "";
  if (n < 1000) return String(Math.round(n));
  if (n < 10_000) return (Math.round(n / 100) / 10).toString().replace(/\.0$/, "") + "K";
  if (n < 1_000_000) return Math.round(n / 1000) + "K";
  return (Math.round(n / 100_000) / 10).toString().replace(/\.0$/, "") + "M";
}
export const STATE_WORDS: Readonly<Record<string, string>> = {
  waiting: "Waiting for you", running: "Running", done: "Done", failed: "Failed", declined: "Declined",
  held: "Held by Pebble", undone: "Undone", stopped: "Stopped",
};
/** The run's state in one or two words, for the header. */
export function runWord(runState: string): string {
  return ({
    queued: "Starting", running: "Working", retry_wait: "Reconnecting", waiting_approval: "Waiting for you", waiting_input: "Waiting for you",
    completed: "Done", paused: "Paused", stopped: "Stopped", failed: "Hit a problem", interrupted: "Interrupted",
    budget_exhausted: "Reached its limit", needs_reconciliation: "Needs a check", verification_required: "Not verified",
  } as Record<string, string>)[runState] || "Working";
}

// ---- cards ----
export type CardKind = "explored" | "web" | "files" | "command" | "tests" | "git" | "window" | "agents" | "checks";
export type Card = { type: "card"; kind: CardKind; key: number; items: ActivityItem[]; views: View[] };
export type WorkEntry = Card | Entry;
const KIND: Readonly<Record<string, CardKind>> = {
  read: "explored", search: "explored", web: "web", edit: "files", command: "command", tests: "tests", git: "git",
  window: "window", agent: "agents", checks: "checks",
};
// Kinds whose consecutive items share one card (reads and searches, edits, presses, agents); each command, test run,
// Git step and check keeps its own card, as they are the steps people check.
const SHARED: ReadonlySet<CardKind> = new Set(["explored", "web", "files", "window", "agents"]);
const viewOf = (item: ActivityItem) => parseView((item as ActivityItem & { view?: unknown }).view);

/** A run's items as cards and, between them, today's rows (folded as before). Review and question items stay rows:
 *  their own cards are drawn in place by the window. */
export function workEntries(items: readonly ActivityItem[]): WorkEntry[] {
  const out: WorkEntry[] = [];
  let rows: ActivityItem[] = [];
  const flush = () => {
    if (rows.length) out.push(...groupRows(rows));
    rows = [];
  };
  for (const item of items) {
    const view = viewOf(item);
    const kind = view ? KIND[view.type] : undefined;
    if (!view || !kind) {
      rows.push(item);
      continue;
    }
    flush();
    const last = out.at(-1);
    if (last && last.type === "card" && last.kind === kind && SHARED.has(kind)) {
      last.items.push(item);
      last.views.push(view);
    } else out.push({ type: "card", kind, key: item.seq, items: [item], views: [view] });
  }
  flush();
  return out;
}

const n = (v: unknown) => (typeof v === "number" ? v : 0);
const s = (v: unknown) => (typeof v === "string" ? v : "");
/** A card's title in plain words, at most five of them where the design allows (the skill's complexity budget). */
export function cardTitle(card: Card): string {
  const views = card.views, first = views[0];
  switch (card.kind) {
    case "explored": {
      const reads = views.filter((v) => v.type === "read").length, searches = views.length - reads;
      return ["Read " + plural(reads, "file"), searches ? plural(searches, "search", "searches") : ""].filter((w) => !w.startsWith("Read 0")).filter(Boolean).join(" · ") || "Explored";
    }
    case "web":
      return "Searched the web";
    case "files": {
      const paths = new Set(views.map((v) => s(v.path) || "private"));
      return "Changed " + plural(paths.size, "file");
    }
    case "command":
      return s(first.command) || "A command";
    case "tests":
      return "Tests" + (s(first.recipe) ? " · " + s(first.recipe) : "");
    case "git":
      return s(first.summary) || (s(first.branch) ? "On " + s(first.branch) : "Git");
    case "window":
      return s(first.app) || s(first.title) || "An app window";
    case "agents":
      return plural(views.length, "agent");
    case "checks":
      return "What your request requires";
  }
}
/** "+48 −12" for edits, from their counts. */
export function diffWords(views: readonly View[]): { added: number; removed: number } {
  return { added: views.reduce((t, v) => t + n(v.added), 0), removed: views.reduce((t, v) => t + n(v.removed), 0) };
}
/** "41 passed · 1 failed"; skipped and errors only when there are some. */
export function testWords(v: View): string {
  return [`${n(v.passed)} passed`, n(v.failed) ? `${n(v.failed)} failed` : "", n(v.skipped) ? `${n(v.skipped)} skipped` : "", n(v.errors) ? plural(n(v.errors), "error") : ""].filter(Boolean).join(" · ");
}
/** A command's outcome: "exit 0 · 4.6 s", "Waiting for you", "Running". */
export function commandWords(v: View): string {
  const st = s(v.state);
  if (st === "waiting" || st === "running" || st === "declined" || st === "held") return STATE_WORDS[st];
  const took = typeof v.seconds === "number" ? formatDuration(v.seconds) : null;
  return [typeof v.exit === "number" ? `exit ${v.exit}` : STATE_WORDS[st] || "", took || ""].filter(Boolean).join(" · ");
}

// ---- the header, the summary and the companion's strip ----
export interface PlanStep { title: string; status: string }
export interface PlanProgress { done: number; total: number; steps: PlanStep[] }
const STEP_STATES = new Set(["pending", "in_progress", "blocked", "completed"]);
/** Plan steps ({title, status}) from an UpdatePlan record or a header, at most 20, titles clamped. */
export function parsePlan(raw: unknown): PlanProgress | null {
  const steps = raw && typeof raw === "object" && Array.isArray((raw as Record<string, unknown>).steps) ? ((raw as Record<string, unknown>).steps as unknown[]) : null;
  if (!steps) return null;
  const shown = steps
    .filter((st): st is Record<string, unknown> => !!st && typeof st === "object" && STEP_STATES.has(String((st as Record<string, unknown>).status)))
    .slice(0, 20)
    .map((st) => ({ title: clampText(st.title, 120) || "Step", status: String(st.status) }));
  return shown.length ? { done: shown.filter((st) => st.status === "completed").length, total: shown.length, steps: shown } : null;
}
/** The task line: the first line of the person's own message, at most 120 characters. */
export function taskLine(message: unknown): string | null {
  if (typeof message !== "string") return null;
  const first = message.split("\n").find((line) => line.trim());
  return first ? clampText(first, 120) || null : null;
}
export interface Summary {
  files: { count: number; added: number; removed: number } | null;
  commands: { count: number; failed: number } | null;
  tests: { passed: number; failed: number } | null;
  checks: string | null;
  seconds: number;
  tokens: { input: number; cached: number; output: number } | null;
}
/** The end-of-run receipt as data, as work_view.summary() builds it (the tests compare both on the spec's fixtures),
 *  for a run whose host summary was not read yet. Pebble records no price, so cost is the tokens. */
export function summaryOf(items: readonly ActivityItem[], usage: Record<string, unknown> | null, seconds: number): Summary {
  const files = new Map<string, [number, number]>();
  let commands = 0, failed = 0, tests: Summary["tests"] = null, checks: string | null = null;
  for (const item of items) {
    const v = viewOf(item);
    if (!v) continue;
    if (v.type === "edit" && v.state === "done") {
      const key = s(v.path) || "private:" + item.seq, [a, r] = files.get(key) || [0, 0];
      files.set(key, [a + n(v.added), r + n(v.removed)]);
    } else if (v.type === "command" && ["done", "failed", "stopped"].includes(s(v.state))) {
      commands++;
      if (v.state !== "done") failed++;
    } else if (v.type === "tests" && ["done", "failed"].includes(s(v.state))) tests = { passed: n(v.passed), failed: n(v.failed) };
    else if (v.type === "checks") checks = s(v.verdict) || null;
  }
  const whole = (v: unknown) => (typeof v === "number" && Number.isSafeInteger(v) && v >= 0 ? v : 0);
  return {
    files: files.size ? { count: files.size, added: [...files.values()].reduce((t, [a]) => t + a, 0), removed: [...files.values()].reduce((t, [, r]) => t + r, 0) } : null,
    commands: commands ? { count: commands, failed } : null,
    tests, checks, seconds: Math.round(seconds * 10) / 10,
    tokens: usage ? { input: whole(usage.input), cached: whole(usage.cached), output: whole(usage.output) } : null,
  };
}
/** The summary card's chips, in words: "2 files changed +48 −12", "3 commands", "Tests 41 passed · 1 failed", … */
export function summaryWords(summary: Summary): string[] {
  const out: string[] = [];
  if (summary.files) out.push(`${plural(summary.files.count, "file")} changed +${summary.files.added} −${summary.files.removed}`);
  if (summary.commands) out.push(plural(summary.commands.count, "command") + (summary.commands.failed ? ` · ${summary.commands.failed} failed` : ""));
  if (summary.tests) out.push(`Tests ${summary.tests.passed} passed` + (summary.tests.failed ? ` · ${summary.tests.failed} failed` : ""));
  if (summary.checks) out.push(summary.checks);
  const took = formatDuration(summary.seconds);
  if (took) out.push(took);
  if (summary.tokens) out.push(`${round(summary.tokens.input)} in · ${round(summary.tokens.output)} out`);
  return out;
}
/** The companion's strip: the current card's words and the plan's progress (null: no host items yet). */
export function stripOf(record: RunActivity | null | undefined, plan: PlanProgress | null): { words: string; done: number; total: number } | null {
  if (!record || !record.items.length) return null;
  const entries = workEntries(record.items);
  const running = [...entries].reverse().find((e) => (e.type === "card" ? e.items : e.type === "row" ? [e.item] : e.items).some((i) => i.status === "running"));
  const current = running || entries.at(-1);
  if (!current) return null;
  const words = current.type === "card" ? cardTitle(current) : current.type === "row" ? current.item.label : current.label;
  return { words: clampText(words, 80), done: plan?.done ?? 0, total: plan?.total ?? 0 };
}

// ---- the branch card's commit lanes ----
export interface Commit { commit: string; parents: string[]; author: string; subject: string; at: number | null }
export interface LaneRow { commit: Commit; lane: number; lanes: number; merges: number[] }
/** Lanes for a first-parent-ordered history (newest first): each commit's lane, how many lanes are open at it, and
 *  the lanes its other parents continue in. At most `max` lanes; history beyond them shares the last lane. */
export function lanes(commits: readonly Commit[], max = 4): LaneRow[] {
  const open: (string | null)[] = [];
  const rows: LaneRow[] = [];
  for (const commit of commits) {
    let lane = open.indexOf(commit.commit);
    if (lane < 0) {
      lane = open.indexOf(null);
      if (lane < 0) lane = Math.min(open.length, max - 1);
    }
    for (let i = 0; i < open.length; i++) if (i !== lane && open[i] === commit.commit) open[i] = null; // lanes that join here
    open[lane] = commit.parents[0] ?? null;
    const merges: number[] = [];
    for (const parent of commit.parents.slice(1)) {
      let at = open.indexOf(parent);
      if (at < 0) {
        at = open.indexOf(null);
        if (at < 0) at = Math.min(open.length, max - 1);
        open[at] = parent;
      }
      merges.push(at);
    }
    while (open.length && open.at(-1) === null) open.pop();
    rows.push({ commit, lane, lanes: Math.max(open.length, lane + 1), merges });
  }
  return rows;
}
/** A commit list from git_history's reply ({commits: [{commit, parents, author, authored_at, subject}]}), bounded. */
export function parseHistory(reply: unknown, limit = 12): Commit[] {
  const list = reply && typeof reply === "object" && Array.isArray((reply as Record<string, unknown>).commits) ? ((reply as Record<string, unknown>).commits as unknown[]) : [];
  const hash = /^[0-9a-f]{7,64}$/;
  return list
    .filter((c): c is Record<string, unknown> => !!c && typeof c === "object" && typeof (c as Record<string, unknown>).commit === "string" && hash.test((c as Record<string, unknown>).commit as string))
    .slice(0, limit)
    .map((c) => ({
      commit: c.commit as string,
      parents: Array.isArray(c.parents) ? (c.parents as unknown[]).filter((p): p is string => typeof p === "string" && hash.test(p)).slice(0, 4) : [],
      author: clampText(c.author, 40), subject: clampText(c.subject, 80),
      at: typeof c.authored_at === "number" && Number.isFinite(c.authored_at) ? c.authored_at : null,
    }));
}
/** "12 min", "2 h", "3 d": how long ago, rounded. */
export function age(at: number | null, now: number): string {
  if (at === null) return "";
  const seconds = Math.max(0, now / 1000 - at);
  if (seconds < 3600) return Math.max(1, Math.round(seconds / 60)) + " min";
  if (seconds < 86_400) return Math.round(seconds / 3600) + " h";
  return Math.round(seconds / 86_400) + " d";
}
