// Slash commands for the composer (Phase 22; design study §4.3). Each entry is either a window
// action that a button, menu or shortcut already performs (App.vue's runSlash calls that same
// handler; actions never become a message and never share a name with a host command), or a
// runtime command from the host's own `command_catalogue`, sent as ordinary message text through
// send(), exactly as if typed. Nothing here grants authority. A command that is unknown is not
// listed; one the host refuses mid-run is shown dimmed with the reason. Arguments are completed
// from data the window already has or reads on demand. The recent-use order is kept in this
// window's localStorage only and never sent.

export type SlashAction =
  | "new_chat" | "clear" | "model" | "effort" | "rewind" | "copy" | "stop" | "details"
  | "schedule" | "browser" | "windows" | "settings";
export type SlashNeed = "chat" | "run" | "settled_run" | "efforts" | "reply" | "working";
export interface LocalCommand {
  readonly name: string;
  readonly description: string;
  readonly action: SlashAction;
  readonly needs: readonly SlashNeed[];
}
export interface HostCommand {
  name: string;
  arguments: string;
  description: string;
}
export interface SlashItem {
  id: string;
  name: string; // shown after "/" (commands) or as is (arguments)
  hint: string; // argument hint shown after the name
  description: string;
  kind: "action" | "host" | "argument";
  action?: SlashAction;
  arguments?: string;
  value?: string; // an argument's text
  disabled?: string; // why it cannot run now
}
export type SlashContext = Readonly<Record<SlashNeed, boolean>>;

const entry = (name: string, description: string, action: SlashAction, needs: SlashNeed[] = []): LocalCommand =>
  Object.freeze({ name, description, action, needs: Object.freeze(needs) });
export const LOCAL_COMMANDS: readonly LocalCommand[] = Object.freeze([
  entry("new", "Start a new conversation", "new_chat"),
  entry("model", "Choose the model", "model"),
  entry("rewind", "Rewind files the latest task changed", "rewind", ["settled_run"]),
  entry("copy", "Copy Pebble's last reply", "copy", ["reply"]),
  entry("stop", "Stop the current task", "stop", ["working"]),
  entry("details", "Show or hide the details panel", "details"),
  entry("effort", "Change the reasoning effort", "effort", ["efforts"]),
  entry("clear", "Clear the message box", "clear"),
  // Phase 24 (V1-108, V1-124): each says where it goes, by the names the drawer shows.
  entry("schedule", "Schedules for this conversation (Details › Tools)", "schedule", ["chat"]),
  entry("windows", "Chrome tabs you shared with Pebble (Details › Tools)", "windows", ["chat"]),
  entry("browser", "Open the Browser panel (Pebble for Chrome)", "browser", ["chat"]),
  entry("settings", "Open settings", "settings"),
]);
// The first rows, in this order, when available (design study §4.3); then recent use.
export const STARTER = Object.freeze(["new", "goal", "status", "compact", "memory", "model", "rewind", "skill"]);
// Host commands the host refuses while a task runs: shown dimmed, never hidden, so the rule is learnt.
export const IDLE_ONLY: ReadonlySet<string> = new Set(["compact", "handoff", "skill"]);
export const IDLE_REASON = "Available when the task is idle";
export const ROWS = 8; // visible rows; the rest scroll

const NAME = /^[a-z][a-z0-9_-]{0,31}$/;
const MAX_HOST = 200;

/** The host catalogue's valid, unique entries (the palette's rules). */
export function validHost(list: unknown): HostCommand[] {
  if (!Array.isArray(list)) return [];
  const seen = new Set<string>(), out: HostCommand[] = [];
  for (const item of list) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    const v = item as Record<string, unknown>;
    if (typeof v.name !== "string" || !NAME.test(v.name) || seen.has(v.name)) continue;
    if (typeof v.arguments !== "string" || v.arguments.length > 160) continue;
    if (typeof v.description !== "string" || v.description.length > 400) continue;
    seen.add(v.name);
    out.push({ name: v.name, arguments: v.arguments, description: v.description });
    if (out.length >= MAX_HOST) break;
  }
  return out;
}

/**
 * The menu's commands in display order: the starter rows, then the person's recent commands (most
 * recent first), then the rest of the catalogue, then the remaining window actions. Window actions
 * whose condition is not met are left out; idle-only host commands are dimmed while a task runs.
 */
export function availableCommands(host: readonly HostCommand[], context: SlashContext, recent: Readonly<Record<string, number>> = {}): SlashItem[] {
  const local = LOCAL_COMMANDS.filter((c) => c.needs.every((need) => context[need]));
  const items = new Map<string, SlashItem>();
  for (const c of host)
    items.set(c.name, {
      id: "host:" + c.name, name: c.name, hint: c.arguments, description: c.description, kind: "host", arguments: c.arguments,
      ...(context.working && IDLE_ONLY.has(c.name) ? { disabled: IDLE_REASON } : {}),
    });
  for (const c of local)
    if (!items.has(c.name)) items.set(c.name, { id: "action:" + c.name, name: c.name, hint: "", description: c.description, kind: "action", action: c.action });
  const order: string[] = [];
  const add = (name: string) => {
    if (items.has(name) && !order.includes(name)) order.push(name);
  };
  STARTER.forEach(add);
  Object.entries(recent).filter(([name, at]) => NAME.test(name) && Number.isFinite(at)).sort((a, b) => b[1] - a[1]).forEach(([name]) => add(name));
  host.forEach((c) => add(c.name));
  local.forEach((c) => add(c.name));
  return order.map((name) => items.get(name)!);
}

export interface SlashInput {
  name: string;
  args: string[]; // finished arguments
  partial: string; // the argument being typed
  stage: "name" | "argument";
}
/** The composer text as a command being typed, or null (not one line starting with "/"). */
export function parseSlash(text: string): SlashInput | null {
  const match = /^\/([A-Za-z0-9_-]{0,32})(?:(\s+)(.*))?$/.exec(text);
  if (!match || /[\r\n]/.test(text)) return null;
  const name = match[1].toLowerCase();
  if (match[2] === undefined) return { name, args: [], partial: "", stage: "name" };
  if (!name) return null;
  const rest = match[3] || "";
  const tokens = rest.match(/"[^"]*(?:"|$)|\S+/g) || [];
  const last = tokens.at(-1) || "";
  const open = last.startsWith('"') && (last.length === 1 || !last.endsWith('"'));
  const typing = open || (!!rest && !/\s$/.test(rest));
  return { name, args: typing ? tokens.slice(0, -1) : tokens, partial: typing ? last : "", stage: "argument" };
}

/** The partial command name while the composer holds only "/" and a name; otherwise null. */
export function slashQuery(text: string): string | null {
  const input = parseSlash(text);
  return input && input.stage === "name" ? input.name : null;
}

/** The exact name first, then name prefix, then name substring, then description; display order within a rank. */
export function filterSlash(items: readonly SlashItem[], query: string): SlashItem[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return items.slice();
  const ranked: { rank: number; order: number; item: SlashItem }[] = [];
  items.forEach((item, order) => {
    const rank = item.name === needle ? 0 : item.name.startsWith(needle) ? 1 : item.name.includes(needle) ? 2
      : item.description.toLowerCase().includes(needle) ? 3 : -1;
    if (rank >= 0) ranked.push({ rank, order, item });
  });
  return ranked.sort((a, b) => a.rank - b.rank || a.order - b.order).map((r) => r.item);
}

/**
 * The row Enter runs while a name is typed: the first, when its name holds what was typed; otherwise none (-1), so a
 * match on a description alone runs only when the person moves to it. Phase 30 (D42): with no task running, the stop
 * action was hidden and Enter ran the host's resume command instead, whose description says "stopped".
 */
export function defaultSlashIndex(items: readonly SlashItem[], query: string): number {
  const needle = query.trim().toLowerCase();
  return items.length && (!needle || items[0].name.includes(needle)) ? 0 : -1;
}

/** Whether a host command needs an argument before it can be sent ("text", "agent-id"; not "[run-id]"). */
export function needsArguments(item: SlashItem): boolean {
  const args = (item.arguments || "").trim();
  return item.kind === "host" && !!args && !args.startsWith("[");
}

// ---- argument completion ----
export interface SlashData {
  runs?: readonly { id: string; state: string; updated?: number }[];
  views?: readonly { id: string; title: string }[];
  skills?: readonly { id: string; name: string }[] | null;
  criteria?: readonly { id: string; text: string }[] | null;
  agents?: readonly { id: string; name?: string }[] | null;
}
export type SlashSource = "skills" | "criteria" | "agents";
// Phase 30 (D42): the host's formats; no PDF until a native PDF view exists.
export const EXPORT_FORMATS = Object.freeze(["docx", "xlsx", "csv", "html"]);
const words = (list: readonly string[], description = ""): SlashItem[] =>
  list.map((w) => ({ id: "arg:" + w, name: w, hint: "", description, kind: "argument", value: w }));
const when = (seconds: unknown) =>
  typeof seconds === "number" && Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" }) : "";
const runs = (data: SlashData): SlashItem[] =>
  (data.runs || []).slice(0, 8).map((r) => ({
    id: "arg:" + r.id, name: r.id.slice(0, 8), hint: "", kind: "argument", value: r.id,
    description: ["Task", r.state.replaceAll("_", " "), when(r.updated)].filter(Boolean).join(" · "),
  }));

/**
 * Options for the argument being typed, from the host catalogue's argument hints: run IDs for
 * /compact, /context, /handoff and /resume; sub-words for /memory, /goal and /criteria; criterion IDs
 * after /criteria check|uncheck|accept|reject; agent IDs for /agent; view IDs then formats for
 * /export; enabled skills for /skill. `source` names data the window must read first.
 */
export function argumentOptions(input: SlashInput, data: SlashData): { options: SlashItem[]; source: SlashSource | null } {
  const at = input.args.length;
  let options: SlashItem[] = [];
  let source: SlashSource | null = null;
  switch (input.name) {
    case "compact": case "context": case "handoff": case "resume":
      if (at === 0) options = runs(data);
      break;
    case "memory":
      if (at === 0) options = words(["status", "candidates", "maintain"]);
      break;
    case "goal":
      if (at === 0) options = words(["done", "pause", "resume", "abandon"], 'Or type a "title" to start a goal');
      break;
    case "criteria":
      if (at === 0) options = words(["add", "check", "uncheck", "accept", "reject"]);
      else if (at === 1 && input.args[0] === "add") options = words(["--evidence"], "Met only by current results of finished steps");
      else if (at === 1 && ["check", "uncheck", "accept", "reject"].includes(input.args[0])) {
        if (data.criteria) options = data.criteria.map((c) => ({ id: "arg:" + c.id, name: c.id, hint: "", description: c.text, kind: "argument", value: c.id }));
        else source = "criteria";
      }
      break;
    case "agent":
      if (at === 0) {
        if (data.agents) options = data.agents.map((a) => ({ id: "arg:" + a.id, name: a.id, hint: "", description: a.name || "", kind: "argument", value: a.id }));
        else source = "agents";
      }
      break;
    case "export":
      if (at === 0) options = (data.views || []).map((v) => ({ id: "arg:" + v.id, name: v.id, hint: "", description: v.title, kind: "argument", value: v.id }));
      else if (at === 1) options = words(EXPORT_FORMATS);
      break;
    case "skill":
      if (at === 0) {
        if (data.skills) options = data.skills.map((s) => ({ id: "arg:" + s.id, name: s.name, hint: "", description: s.id, kind: "argument", value: `"${s.name.replaceAll('"', "")}"` }));
        else source = "skills";
      }
      break;
  }
  const needle = input.partial.replace(/^"/, "").replace(/"$/, "").toLowerCase();
  if (needle) {
    const starts = options.filter((o) => (o.value || "").replace(/^"/, "").toLowerCase().startsWith(needle) || o.name.toLowerCase().startsWith(needle));
    const contains = options.filter((o) => !starts.includes(o) && (o.name.toLowerCase().includes(needle) || o.description.toLowerCase().includes(needle)));
    options = [...starts, ...contains];
  }
  return { options, source };
}

/** The composer text after choosing an argument option. */
export function completeArgument(input: SlashInput, option: SlashItem): string {
  return "/" + input.name + " " + [...input.args, option.value || option.name].join(" ") + " ";
}

export type SlashChoice =
  | { kind: "run"; action: SlashAction }
  | { kind: "insert"; text: string }
  | { kind: "send"; text: string }
  | { kind: "none"; reason: string };

/**
 * What choosing a command does. Tab completes the name in the message box. Enter or a click runs a
 * window action, sends a host command that needs no argument (the same send() a typed command
 * uses), and completes one that needs an argument so the person can type it. A dimmed command does
 * nothing and says why.
 */
export function chooseSlash(item: SlashItem, how: "enter" | "tab" | "click"): SlashChoice {
  if (item.disabled && how !== "tab") return { kind: "none", reason: item.disabled };
  if (how === "tab") return { kind: "insert", text: "/" + item.name + (item.kind === "host" && item.arguments ? " " : "") };
  if (item.kind === "action" && item.action) return { kind: "run", action: item.action };
  if (needsArguments(item)) return { kind: "insert", text: "/" + item.name + " " };
  return { kind: "send", text: "/" + item.name };
}

/** Arrow-key movement inside the menu, wrapping at both ends; from "nothing chosen" (-1) it starts at an end. */
export function moveSlash(index: number, step: number, count: number): number {
  if (!count) return 0;
  if (index < 0) return step > 0 ? 0 : count - 1;
  return (((index + step) % count) + count) % count;
}

// ---- recent use: this window's localStorage only, never sent ----
export const RECENT_KEY = "pebble.slash.recent";
const MAX_RECENT = 24;
export function readRecent(storage: Pick<Storage, "getItem"> | null | undefined): Record<string, number> {
  try {
    const raw = storage?.getItem(RECENT_KEY);
    if (!raw || raw.length > 4000) return {};
    const value = JSON.parse(raw) as Record<string, unknown>;
    const out: Record<string, number> = Object.create(null);
    for (const [name, at] of Object.entries(value || {})) if (NAME.test(name) && typeof at === "number" && Number.isFinite(at)) out[name] = at;
    return out;
  } catch {
    return {};
  }
}
export function recordUse(storage: Pick<Storage, "getItem" | "setItem"> | null | undefined, name: string, now = Date.now()): Record<string, number> {
  const recent = readRecent(storage);
  if (!NAME.test(name)) return recent;
  recent[name] = now;
  const kept = Object.fromEntries(Object.entries(recent).sort((a, b) => b[1] - a[1]).slice(0, MAX_RECENT));
  try {
    storage?.setItem(RECENT_KEY, JSON.stringify(kept));
  } catch {
    /* a full or refusing storage keeps the order as it was */
  }
  return kept;
}
export function clearRecent(storage: Pick<Storage, "removeItem"> | null | undefined) {
  try {
    storage?.removeItem(RECENT_KEY);
  } catch {
    /* nothing to clear */
  }
}
