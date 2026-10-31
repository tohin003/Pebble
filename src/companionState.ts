// Companion presence: one pure reducer over host signals (design study §2.1).
// The host's run, progress, token, question and approval records decide what
// the companion shows. Time only ends a settle window or marks a long quiet
// spell; it never invents a mood while work is under way (F1). The pointer and
// taps are local presentation: with a live run they change gaze only.
import { RUN_STATUS, toolClass, type StatusKey } from "./statusWords";

export const LIVE = ["queued", "running", "retry_wait"] as const;
// Row 8 poses: the class of the tool the host is running now.
export const CLASS_POSES = ["reading", "searching", "editing", "testing", "running", "delegating", "viewing", "working"] as const;
export type ClassPose = (typeof CLASS_POSES)[number];
const isClassPose = (key: unknown): key is ClassPose =>
  typeof key === "string" && (CLASS_POSES as readonly string[]).includes(key);
const isLive = (state: string | undefined) =>
  state === "queued" || state === "running" || state === "retry_wait";

export const TOKEN_MS = 1500; // row 9: a token within the last 1.5 s
export const COMPLETED_MS = 4500; // row 13
export const STOPPED_MS = 5000; // row 14
export const FAILED_MS = 10000; // row 3
export const TAP_MS = 1200; // row 16
export const QUIET_MS = 10 * 60 * 1000; // row 17
const RETIRED_LIMIT = 64;

export type HostSignal =
  | { k: "model"; state: string }
  | { k: "run"; run_id: string; state: string }
  | { k: "progress"; run_id?: string | null; kind: string; tool?: string; activity?: string; attempt?: number }
  | { k: "token"; run_id?: string | null }
  | { k: "question"; pending: number }
  | { k: "approvals"; pending: number }
  | { k: "notices"; pending: number }
  | { k: "offline" }
  | { k: "online" }
  | { k: "idle"; run_id?: string | null }
  | { k: "error"; run_id?: string | null }
  | { k: "sending"; on: boolean }
  | { k: "interaction"; what: "hover" | "leave" | "tap" | "drag" };

export interface RunView {
  id: string;
  state: string;
  startedAt: number | null; // first host `running` event of this live stretch
  attempt: number | null; // reconnect attempt, from the host's progress event
}

export interface CompanionState {
  online: boolean;
  model: string;
  run: RunView | null;
  retired: readonly string[]; // earlier runs whose late events are ignored
  tool: ClassPose | null; // class of the last `working` progress with a tool
  tokenAt: number;
  sending: boolean;
  approvals: number;
  questions: number;
  notices: number;
  completedUntil: number;
  stoppedUntil: number;
  failedUntil: number;
  hover: boolean;
  tapAt: number;
  lastInput: number;
}

export function initial(now = 0): CompanionState {
  return {
    online: true, model: "", run: null, retired: [], tool: null, tokenAt: -Infinity,
    sending: false, approvals: 0, questions: 0, notices: 0,
    completedUntil: 0, stoppedUntil: 0, failedUntil: 0,
    hover: false, tapAt: -Infinity, lastInput: now,
  };
}

const count = (n: unknown) =>
  typeof n === "number" && Number.isFinite(n) && n > 0 ? Math.min(Math.floor(n), 9999) : 0;
const id = (value: unknown): string | null =>
  typeof value === "string" && value.length > 0 && value.length <= 64 ? value : null;

// Which run an event belongs to. "current": the run being shown; "new": a run
// that supersedes it; null: ignore (a retired run, or a stray non-live event
// while another run is live).
function owner(s: CompanionState, runId: string | null, state?: string): "current" | "new" | null {
  if (runId === null) return s.run ? "current" : null;
  if (s.run && s.run.id === runId) return "current";
  // A host admission (`queued`) is authoritative even for an older id: resumed
  // runs keep their id.
  if (state === "queued") return "new";
  if (s.retired.includes(runId)) return null;
  if (state === undefined) return null; // progress/token/idle for a run never seen
  if (s.run && isLive(s.run.state) && !isLive(state)) return null;
  return "new";
}

function adopt(s: CompanionState, runId: string): CompanionState {
  const retired = s.retired.filter((r) => r !== runId);
  if (s.run && s.run.id !== runId) retired.push(s.run.id);
  return {
    ...s,
    retired: retired.slice(-RETIRED_LIMIT),
    run: { id: runId, state: "", startedAt: null, attempt: null },
    tool: null, tokenAt: -Infinity, sending: false,
    completedUntil: 0, stoppedUntil: 0, failedUntil: 0,
  };
}

function transition(s: CompanionState, state: string, now: number): CompanionState {
  const run = s.run!;
  const wasLive = isLive(run.state);
  const live = isLive(state);
  const next: CompanionState = { ...s, sending: false, run: { ...run, state } };
  if (live && !wasLive) {
    // A fresh live stretch (new, or resumed after a pause): count elapsed time anew.
    next.run!.startedAt = null;
    next.run!.attempt = null;
    next.completedUntil = next.stoppedUntil = next.failedUntil = 0;
  }
  if (state === "running" && next.run!.startedAt === null) next.run!.startedAt = now;
  if (state !== "retry_wait") next.run!.attempt = null;
  if (!live) {
    next.tool = null;
    next.tokenAt = -Infinity;
  }
  if (state === "stopped") next.stoppedUntil = now + STOPPED_MS;
  if (state === "failed") next.failedUntil = Math.max(next.failedUntil, now + FAILED_MS);
  return next;
}

export function reduce(s: CompanionState, sig: HostSignal, now: number): CompanionState {
  switch (sig.k) {
    case "model":
      return typeof sig.state === "string" && sig.state !== s.model ? { ...s, model: sig.state } : s;
    case "question": {
      const n = count(sig.pending);
      return s.questions === n ? s : { ...s, questions: n };
    }
    case "approvals": {
      const n = count(sig.pending);
      return s.approvals === n ? s : { ...s, approvals: n };
    }
    case "notices": {
      const n = count(sig.pending);
      return s.notices === n ? s : { ...s, notices: n };
    }
    case "offline":
      return { ...s, online: false, sending: false, tool: null, tokenAt: -Infinity };
    case "online":
      // A restarted service recovers runs itself; nothing from before is live.
      return { ...initial(now), model: s.model, approvals: s.approvals, questions: s.questions, notices: s.notices, retired: s.run ? [...s.retired, s.run.id].slice(-RETIRED_LIMIT) : s.retired };
    case "sending": {
      if (sig.on) {
        const live = isLive(s.run?.state);
        return { ...s, sending: !live, lastInput: now, ...(live ? {} : { completedUntil: 0, stoppedUntil: 0, failedUntil: 0 }) };
      }
      return s.sending ? { ...s, sending: false } : s;
    }
    case "interaction": {
      const next = { ...s, lastInput: now };
      if (sig.what === "hover" || sig.what === "drag") next.hover = true;
      else if (sig.what === "leave") next.hover = false;
      else if (sig.what === "tap") next.tapAt = now;
      else return s;
      return next;
    }
    case "run": {
      const runId = id(sig.run_id);
      if (runId === null || typeof sig.state !== "string") return s;
      const who = owner(s, runId, sig.state);
      if (who === null) return s;
      const base = who === "new" ? adopt(s, runId) : s;
      return transition({ ...base, lastInput: now }, sig.state, now);
    }
    case "progress": {
      if (!s.run || !isLive(s.run.state)) return s; // nothing before admission or after settlement
      if (sig.run_id !== undefined && sig.run_id !== null && id(sig.run_id) !== s.run.id) return s;
      let tool: ClassPose | null = null;
      if (sig.kind === "working" && typeof sig.tool === "string" && sig.tool) {
        // The host's own class wins; the browser table is only a fallback. A
        // host key (question, review, ...) never arrives this way: those come
        // from run states and counts.
        const key = sig.activity === undefined ? toolClass(sig.tool) : sig.activity;
        tool = isClassPose(key) ? key : key === "thinking" ? null : "working";
      }
      const attempt =
        Number.isInteger(sig.attempt) && (sig.attempt as number) >= 1 && (sig.attempt as number) <= 99
          ? (sig.attempt as number)
          : s.run.attempt;
      if (tool === s.tool && attempt === s.run.attempt) return s;
      return { ...s, tool, run: { ...s.run, attempt } };
    }
    case "token":
      if (!s.run || !isLive(s.run.state)) return s;
      if (sig.run_id !== undefined && sig.run_id !== null && id(sig.run_id) !== s.run.id) return s;
      return { ...s, tokenAt: now };
    case "idle": {
      const runId = sig.run_id === undefined || sig.run_id === null ? null : id(sig.run_id);
      if (runId !== null && owner(s, runId) !== "current") return s;
      if (!s.run) return s.sending ? { ...s, sending: false } : s;
      if (isLive(s.run.state)) {
        // The worker ended without a settle event (a setup failure): stop
        // showing work, and claim no outcome the host did not report.
        return { ...s, sending: false, tool: null, tokenAt: -Infinity, run: { ...s.run, state: "interrupted" } };
      }
      return s.run.state === "completed" ? { ...s, sending: false, completedUntil: now + COMPLETED_MS } : s;
    }
    case "error": {
      const runId = sig.run_id === undefined || sig.run_id === null ? null : id(sig.run_id);
      if (runId !== null && owner(s, runId) !== "current") return s;
      const live = isLive(s.run?.state);
      if (!live && !s.sending) return s; // not tied to a task this window is showing
      return { ...s, sending: false, failedUntil: now + FAILED_MS };
    }
  }
  return s;
}

export type PoseId =
  | "offline" | "uncertain" | "error" | "review" | "question" | "reconnecting" | "blocked"
  | "reading" | "searching" | "editing" | "testing" | "running" | "delegating" | "viewing" | "working"
  | "writing" | "loading" | "thinking" | "sending" | "paused" | "happy" | "stopped" | "notify"
  | "hover" | "tap" | "sleepy" | "idle";

export interface Visual {
  row: number;
  emotion: string;
  expression: string; // bloub expression id
  engine: "idle" | "alert" | "notify"; // bloub engine state
  cue: string; // avatarMotion cue
  hold: boolean; // may settle to a static frame after 8 s
}

// One row per pose. "row" is the §2.1 priority row (7.5 marks `sending`, the
// local fact that a message is on its way, shown until the host admits a run).
export const POSES: Record<PoseId, Visual> = {
  offline: { row: 1, emotion: "worried", expression: "triste", engine: "idle", cue: "", hold: true },
  uncertain: { row: 2, emotion: "concerned", expression: "mefiant", engine: "alert", cue: "", hold: true },
  error: { row: 3, emotion: "sad", expression: "triste", engine: "idle", cue: "sigh", hold: true },
  review: { row: 4, emotion: "asking", expression: "attentif", engine: "alert", cue: "", hold: true },
  question: { row: 5, emotion: "asking", expression: "curieux", engine: "notify", cue: "", hold: true },
  reconnecting: { row: 6, emotion: "patient", expression: "attentif", engine: "idle", cue: "orbit", hold: false },
  blocked: { row: 7, emotion: "stuck", expression: "confus", engine: "idle", cue: "", hold: true },
  sending: { row: 7.5, emotion: "attentive", expression: "attentif", engine: "idle", cue: "", hold: false },
  reading: { row: 8, emotion: "reading", expression: "attentif", engine: "idle", cue: "scan", hold: false },
  searching: { row: 8, emotion: "searching", expression: "curieux", engine: "idle", cue: "scan", hold: false },
  editing: { row: 8, emotion: "editing", expression: "fier", engine: "idle", cue: "spark", hold: false },
  testing: { row: 8, emotion: "testing", expression: "mefiant", engine: "idle", cue: "orbit", hold: false },
  running: { row: 8, emotion: "running", expression: "attentif", engine: "idle", cue: "orbit", hold: false },
  delegating: { row: 8, emotion: "delegating", expression: "curieux", engine: "idle", cue: "thought", hold: false },
  viewing: { row: 8, emotion: "viewing", expression: "fier", engine: "idle", cue: "spark", hold: false },
  working: { row: 8, emotion: "working", expression: "attentif", engine: "idle", cue: "thought", hold: false },
  writing: { row: 9, emotion: "writing", expression: "fier", engine: "idle", cue: "", hold: false },
  loading: { row: 11, emotion: "loading", expression: "attentif", engine: "idle", cue: "orbit", hold: false },
  thinking: { row: 10, emotion: "thinking", expression: "curieux", engine: "idle", cue: "thought", hold: false },
  paused: { row: 12, emotion: "resting", expression: "blase", engine: "idle", cue: "", hold: true },
  happy: { row: 13, emotion: "happy", expression: "heureux", engine: "idle", cue: "celebrate", hold: true },
  stopped: { row: 14, emotion: "neutral", expression: "blase", engine: "idle", cue: "", hold: true },
  notify: { row: 15, emotion: "neutral", expression: "neutre", engine: "notify", cue: "", hold: true },
  hover: { row: 16, emotion: "attentive", expression: "attentif", engine: "idle", cue: "", hold: true },
  tap: { row: 16, emotion: "happy", expression: "heureux", engine: "idle", cue: "", hold: true },
  sleepy: { row: 17, emotion: "resting", expression: "somnolent", engine: "idle", cue: "sleep", hold: true },
  idle: { row: 18, emotion: "neutral", expression: "neutre", engine: "idle", cue: "", hold: true },
};
// Older names still passed as an Avatar `state` (App.vue's booting avatar uses "loading").
const ALIASES: Record<string, PoseId> = Object.assign(Object.create(null), {
  success: "happy", cancelled: "stopped", building: "editing", curious: "thinking", ready: "idle", unloaded: "idle",
});

export function poseId(name: unknown): PoseId {
  if (typeof name !== "string") return "idle";
  if (Object.prototype.hasOwnProperty.call(POSES, name)) return name as PoseId;
  return ALIASES[name] || "idle";
}
export function visualFor(name: unknown): Visual & { id: PoseId } {
  const pid = poseId(name);
  return { id: pid, ...POSES[pid] };
}

export interface Pose extends Visual {
  id: PoseId;
  status: StatusKey | null;
  live: boolean;
  startedAt: number | null;
  attempt: number | null;
  outcome: "completed" | "stopped" | "failed" | null;
}
export interface PoseOptions {
  reactions?: boolean; // companion.reactions: off drops rows 13, 16 and 17
}

const STATUS: Partial<Record<PoseId, StatusKey>> = {
  offline: "offline", uncertain: "uncertain", error: "error", review: "review", question: "question",
  reconnecting: "reconnecting", blocked: "blocked", sending: "sending", reading: "reading",
  searching: "searching", editing: "editing", testing: "testing", running: "running",
  delegating: "delegating", viewing: "viewing", working: "working", writing: "writing",
  loading: "loading", thinking: "thinking", paused: "paused",
};

function choose(s: CompanionState, now: number, reactions: boolean): PoseId {
  const state = s.run?.state;
  const live = isLive(state);
  if (!s.online) return "offline"; // 1
  if (state === "needs_reconciliation") return "uncertain"; // 2
  if (s.model === "error" || now < s.failedUntil) return "error"; // 3
  if (s.approvals > 0 || state === "waiting_approval") return "review"; // 4
  if (s.questions > 0 || state === "waiting_input") return "question"; // 5
  if (state === "retry_wait") return "reconnecting"; // 6
  if (state === "budget_exhausted" || state === "verification_required") return "blocked"; // 7
  if (s.sending && !live) return "sending"; // 7.5: until the host's `run queued`
  if (live) {
    // Rows 8–11 require a live run seen from the host.
    if (s.tool) return s.tool as PoseId; // 8
    if (now - s.tokenAt < TOKEN_MS) return "writing"; // 9
    if (s.model === "loading") return "loading"; // 11 (checked before 10, which matches any live run)
    return "thinking"; // 10
  }
  if (state === "paused") return "paused"; // 12
  if (reactions && now < s.completedUntil) return "happy"; // 13
  if (now < s.stoppedUntil) return "stopped"; // 14
  if (s.notices > 0) return "notify"; // 15
  if (reactions && now - s.tapAt < TAP_MS) return "tap"; // 16
  if (reactions && s.hover) return "hover"; // 16
  if (reactions && now - s.lastInput >= QUIET_MS) return "sleepy"; // 17
  return "idle"; // 18
}

export function pose(s: CompanionState, now: number, options: PoseOptions = {}): Pose {
  const pid = choose(s, now, options.reactions !== false);
  const live = isLive(s.run?.state);
  const outcome =
    pid === "happy" ? "completed" : pid === "stopped" ? "stopped" : pid === "error" && s.run?.state === "failed" ? "failed" : null;
  return {
    id: pid,
    ...POSES[pid],
    status: STATUS[pid] ?? null,
    live,
    startedAt: live ? s.run!.startedAt : null,
    attempt: pid === "reconnecting" ? s.run!.attempt : null,
    outcome,
  };
}

/** The earliest future time at which pose() may change without a new signal, or null. */
export function nextChange(s: CompanionState, now: number): number | null {
  const live = isLive(s.run?.state);
  const times = [s.failedUntil, s.completedUntil, s.stoppedUntil, s.tapAt + TAP_MS];
  if (live) times.push(s.tokenAt + TOKEN_MS);
  else times.push(s.lastInput + QUIET_MS);
  const future = times.filter((t) => Number.isFinite(t) && t > now);
  return future.length ? Math.min(...future) : null;
}

/** Host bridge events (`{event, data}`) as signals. Counts come from the windows' own records. */
export function fromEvent(event: { event: string; data: any }): HostSignal[] {
  const d = event && typeof event.data === "object" && event.data !== null ? event.data : {};
  const str = (v: unknown) => (typeof v === "string" ? v : undefined);
  switch (event?.event) {
    case "run":
      return str(d.run_id) && str(d.state) ? [{ k: "run", run_id: d.run_id, state: d.state }] : [];
    case "progress":
      return typeof d.kind === "string"
        ? [{ k: "progress", run_id: str(d.run_id) ?? null, kind: d.kind, tool: str(d.tool), activity: str(d.activity), attempt: typeof d.attempt === "number" ? d.attempt : undefined }]
        : [];
    case "token":
      return [{ k: "token", run_id: str(d.run_id) ?? null }];
    // Phase 45 (F105): text arriving in the stream is writing too (the checked reply is presented at the end, H20); the
    // status said "Connecting" for the whole of a 62-second story
    case "stream":
      return d.type === "delta" && str(d.run_id) ? [{ k: "token", run_id: d.run_id }] : [];
    case "idle":
      return [{ k: "idle", run_id: str(d.run_id) ?? null }];
    case "error":
      return [{ k: "error", run_id: str(d.run_id) ?? null }];
    case "offline":
      return [{ k: "offline" }];
    case "ready":
      return [{ k: "online" }];
    case "model":
      return str(d.state) ? [{ k: "model", state: d.state }] : [];
  }
  return [];
}

/** Signals that restore a window's view from `state()` (a run already in progress or waiting). */
export function seedSignals(
  runs: readonly { id?: unknown; state?: unknown }[] | undefined,
  activeRun?: unknown,
): HostSignal[] {
  const list = Array.isArray(runs) ? runs : [];
  const run = list.find((r) => r && r.id === activeRun) || list[0];
  if (!run || typeof run.id !== "string" || typeof run.state !== "string") return [];
  // Finished runs seed nothing: a celebration or a stop belongs to the moment it happens.
  if (!isLive(run.state) && !Object.prototype.hasOwnProperty.call(RUN_STATUS, run.state)) return [];
  if (run.state === "failed") return [];
  return [{ k: "run", run_id: run.id, state: run.state }];
}
