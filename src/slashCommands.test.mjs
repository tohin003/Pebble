import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { transform } from "sucrase";

// Slash command fixtures (Phase 22; design study §4.3–§4.4): window actions never share a host name,
// host commands come only from the validated catalogue, the starter order then recent use, idle-only
// commands dimmed with the reason, argument completion over the catalogue, and what Enter, Tab and a
// click do. The recent-use order stays in this window's storage.
const source = readFileSync(new URL("./slashCommands.ts", import.meta.url), "utf8");
const module = { exports: {} };
new Function("module", "exports", transform(source, { transforms: ["typescript", "imports"] }).code)(module, module.exports);
const S = module.exports;
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };

// Static: pure data and functions; no route, no markup, and no host command text of its own.
ok(!/\brpc\b|invoke\(|fetch\(|innerHTML|v-html|eval\(|new Function/.test(source));
ok(Object.isFrozen(S.LOCAL_COMMANDS) && S.LOCAL_COMMANDS.every(Object.isFrozen) && Object.isFrozen(S.STARTER));
const names = S.LOCAL_COMMANDS.map((c) => c.name);
eq(new Set(names).size, names.length, "one entry per name");
eq(names, ["new", "model", "rewind", "copy", "stop", "details", "effort", "clear", "schedule", "windows", "browser", "settings"]);
const backend = readFileSync(new URL("../backend/pebble/runtime_commands.py", import.meta.url), "utf8");
const catalogue = [...backend.matchAll(/['"]name['"]:\s*['"]([a-z]+)['"]/g)].map((m) => m[1]);
eq(catalogue.length, 28, "the host's 28 runtime commands were read");
for (const name of names) ok(!catalogue.includes(name), "/" + name + " never collides with a host command");
for (const c of S.LOCAL_COMMANDS) {
  ok(/^[a-z][a-z0-9_-]{0,31}$/.test(c.name) && c.description.length <= 80 && !/[<>]/.test(c.description), c.name);
  ok(!/approve|answer|permission|grant/i.test(c.description), c.name + " never approves, answers or grants");
}
for (const name of catalogue) ok(!new RegExp('"/' + name + '\\b').test(source), "/" + name + " is never typed as text here; host commands come from command_catalogue");
for (const name of ["compact", "handoff", "skill", "goal", "status", "memory"]) ok(catalogue.includes(name), name + " is a host command");

// The real catalogue, as the host sends it.
const host = S.validHost(catalogue.map((name) => {
  const line = backend.split("\n").find((l) => new RegExp(`['"]name['"]:\\s*['"]${name}['"]`).test(l));
  const field = (key) => (line.match(new RegExp(`['"]${key}['"]:\\s*(['"])((?:\\\\.|(?!\\1).)*)\\1`)) || [])[2]?.replace(/\\'/g, "'") ?? "";
  return { name, arguments: field("arguments"), description: field("description") };
}));
eq(host.length, 28);
eq(S.validHost([{ name: "Bad", arguments: "", description: "" }, { name: "dup", arguments: "", description: "" }, { name: "dup", arguments: "", description: "x" },
  { name: "huge", arguments: "a".repeat(161), description: "" }, null, "x", ["y"]]).map((c) => c.name), ["dup"], "the palette's validation");
eq(S.validHost({ help: "x" }), []);

// Phase 24 (V1-108, V1-124): plain descriptions in the menu, and replies printed as words, not JSON.
const JARGON = /\b(inference|receipts?|host|runtime|capacity|E-B1|native|confined|scoped worker)\b/i;
for (const c of [...S.LOCAL_COMMANDS, ...host]) ok(!JARGON.test(c.description), "/" + c.name + " is described in plain words: " + c.description);
eq(S.LOCAL_COMMANDS.filter((c) => ["schedule", "windows", "browser"].includes(c.name)).map((c) => c.description), [
  "Schedules for this conversation (Details › Tools)", "Chrome tabs you shared with Pebble (Details › Tools)", "Open the Browser panel (Pebble for Chrome)"]);
ok(!/Windows Pebble can use|Pebble's browser|Activity & review/.test(source), "no place that does not exist");
eq(host.find((c) => c.name === "skill").description, "Start a new task with complete pinned instructions from one enabled skill.", "test_phase17_security pins /skill");
const dispatcher = backend.slice(backend.indexOf("def command(app, cid, text):"));
ok(dispatcher.length > 1000 && !/json['"]?\)?\.dumps|__import__\(['"]json/.test(dispatcher), "no command reply is a JSON dump");
const pyFunction = (name) => {
  const start = backend.indexOf("\ndef " + name + "(");
  return start < 0 ? "" : backend.slice(start, backend.indexOf("\ndef ", start + 5));
};
for (const [name, renderer] of [["jobs", "_jobs_text"], ["git", "_git_text"], ["agent", "_agent_text"]]) {
  const body = pyFunction(renderer);
  ok(body && dispatcher.includes(renderer + "("), "/" + name + " is rendered by " + renderer);
  // The renderer with the helpers it calls (pytest test_jobs_git_and_agent_render_markdown_lists checks the output).
  const helpers = [...body.matchAll(/\b(_[a-z_]+)\(/g)].map((m) => pyFunction(m[1])).join("");
  ok(/f"- \{/.test(body + helpers), "/" + name + " prints Markdown list items");
  ok(!/json/.test(body + helpers), renderer + " never serialises JSON");
}
ok(/GOAL_FOOTER = '[^']*no inference[^']*'/.test(backend), "the /goal reply keeps the phrase test_phase17_security.py:1201 pins");

// Order: the eight starter rows, then recent use, then the catalogue, then window actions.
const idle = { chat: true, run: true, settled_run: true, efforts: true, reply: true, working: false };
let items = S.availableCommands(host, idle);
eq(items.slice(0, 8).map((i) => i.name), ["new", "goal", "status", "compact", "memory", "model", "rewind", "skill"]);
eq(new Set(items.map((i) => i.name)).size, items.length, "every name once");
eq(items.length, 28 + names.length - 1, "every host command and every available action (/stop waits for a task)");
ok(items.every((i) => i.kind === (names.includes(i.name) ? "action" : "host")));
items = S.availableCommands(host, idle, { plugins: 300, hooks: 900, "Bad name": 999 });
eq(items.slice(8, 10).map((i) => i.name), ["hooks", "plugins"], "the person's recent commands follow the starter rows, newest first");
const bare = S.availableCommands(host, { chat: false, run: false, settled_run: false, efforts: false, reply: false, working: false });
eq(bare.filter((i) => i.kind === "action").map((i) => i.name), ["new", "model", "details", "clear", "settings"], "actions only when their condition holds");
const busy = S.availableCommands(host, { ...idle, working: true, settled_run: false });
eq(busy.filter((i) => i.disabled).map((i) => [i.name, i.disabled]), [["compact", S.IDLE_REASON], ["skill", S.IDLE_REASON], ["handoff", S.IDLE_REASON]], "idle-only commands are dimmed with the reason, not hidden");
ok(busy.some((i) => i.name === "stop") && !items.some((i) => i.name === "stop"), "/stop only while a task runs");
ok(["pause", "cancel", "steer", "status", "questions"].every((n) => busy.find((i) => i.name === n && !i.disabled)), "what works mid-run stays enabled");

// Queries and filtering (acceptance: /co lists /compact then /context).
eq(["/", "/co", "/CoMp", "/compact x", " /co", "hello /co", "/co\nx", "", "//"].map(S.slashQuery), ["", "co", "comp", null, null, null, null, null, null]);
eq(S.filterSlash(items, "co").slice(0, 3).map((i) => i.name), ["compact", "context", "copy"], "prefix before substring, in display order");
eq(S.filterSlash(items, "xyz"), [], "no match: the menu says so and Enter sends nothing");
eq(S.filterSlash(items, "follow-up").map((i) => i.name), ["steer"], "then descriptions");

// Phase 30 (D42): Enter runs only a command the person named. /stop with no task running ran /resume ("…paused or
// stopped task"), and /agent and /export ran /agents and /exports.
eq(S.filterSlash(items, "agent").slice(0, 2).map((i) => i.name), ["agent", "agents"], "the exact name first");
eq(S.filterSlash(items, "export").slice(0, 2).map((i) => i.name), ["export", "exports"], "the exact name first");
const idleStop = S.filterSlash(items, "stop");
ok(idleStop.length && !idleStop.some((i) => i.name === "stop") && idleStop.some((i) => i.name === "resume"), "idle: /stop matches descriptions only");
eq(S.defaultSlashIndex(idleStop, "stop"), -1, "idle: Enter after /stop runs nothing");
eq(S.filterSlash(busy, "stop")[0].id, "action:stop");
eq(S.defaultSlashIndex(S.filterSlash(busy, "stop"), "stop"), 0, "while a task runs, Enter after /stop stops it");
eq(S.defaultSlashIndex(S.filterSlash(bare, "schedule"), "schedule"), -1, "no conversation: /schedule never runs /jobs");
eq(S.defaultSlashIndex(S.filterSlash(items, "co"), "co"), 0, "a name match is still chosen");
eq(S.defaultSlashIndex(S.filterSlash(items, "follow-up"), "follow-up"), -1, "a description match waits for the person");
eq(S.defaultSlashIndex(items, ""), 0);
eq(S.defaultSlashIndex([], "x"), -1);

// Parsing the argument being typed.
eq(S.parseSlash("/compact "), { name: "compact", args: [], partial: "", stage: "argument" });
eq(S.parseSlash("/export v1 p"), { name: "export", args: ["v1"], partial: "p", stage: "argument" });
eq(S.parseSlash('/skill "Deep res'), { name: "skill", args: [], partial: '"Deep res', stage: "argument" });
eq(S.parseSlash('/skill "Deep research" '), { name: "skill", args: ['"Deep research"'], partial: "", stage: "argument" });
eq([S.parseSlash("hi /x"), S.parseSlash("/ x"), S.parseSlash("/x\ny"), S.parseSlash("/co!")], [null, null, null, null]);

// Argument completion from the catalogue's hints and the window's data.
const data = {
  runs: [{ id: "a1b2c3d4e5f6", state: "completed", updated: 1760000000 }, { id: "ffff0000", state: "paused" }],
  views: [{ id: "view-1", title: "Sales chart" }],
  skills: null, criteria: null, agents: null,
};
const opts = (text, d = data) => S.argumentOptions(S.parseSlash(text), d);
eq(opts("/compact ").options.map((o) => [o.name, o.value]), [["a1b2c3d4", "a1b2c3d4e5f6"], ["ffff0000", "ffff0000"]], "run IDs for /compact");
ok(opts("/resume ").options[1].description.startsWith("Task · paused"));
eq(opts("/memory ").options.map((o) => o.value), ["status", "candidates", "maintain"]);
eq(opts("/memory ca").options.map((o) => o.value), ["candidates"]);
eq(opts("/goal ").options.map((o) => o.value), ["done", "pause", "resume", "abandon"]);
eq(opts('/goal "Ship').options, [], 'a "title" is free text: no options, so Enter sends');
eq(opts("/criteria ").options.map((o) => o.value), ["add", "check", "uncheck", "accept", "reject"]);
eq([opts("/criteria check ").options, opts("/criteria check ").source], [[], "criteria"], "criterion IDs are read first");
eq(opts("/criteria check ", { ...data, criteria: [{ id: "c1", text: "Tests pass" }] }).options.map((o) => [o.value, o.description]), [["c1", "Tests pass"]]);
eq(opts("/agent ").source, "agents");
eq(opts("/export ").options.map((o) => o.value), ["view-1"]);
eq(opts("/export view-1 ").options.map((o) => o.value), ["docx", "xlsx", "csv", "html"], "no PDF (Phase 30)");
eq(opts("/skill ").source, "skills");
const skills = { ...data, skills: [{ id: "sk-1", name: "Deep research" }, { id: "sk-2", name: 'Odd "quoted" name' }] };
eq(opts("/skill ", skills).options.map((o) => o.value), ['"Deep research"', '"Odd quoted name"']);
eq(opts('/skill "dee', skills).options.map((o) => o.value), ['"Deep research"'], "matching ignores the opening quote");
eq(opts("/steer ").options, [], "free text has no options");
eq(opts("/status ").options, []);
eq(S.completeArgument(S.parseSlash("/export view-1 ht"), { name: "html", value: "html" }), "/export view-1 html ");
eq(S.completeArgument(S.parseSlash("/skill "), { name: "Deep research", value: '"Deep research"' }), '/skill "Deep research" ');

// Choosing: actions run; host commands send when nothing must be typed, else complete; dimmed ones say why.
const pick = (name, list = items) => list.find((i) => i.name === name);
eq(S.chooseSlash(pick("new"), "enter"), { kind: "run", action: "new_chat" });
eq(S.chooseSlash(pick("new"), "tab"), { kind: "insert", text: "/new" });
eq(S.chooseSlash(pick("status"), "enter"), { kind: "send", text: "/status" }, "the same text a person would type and send");
eq(S.chooseSlash(pick("compact"), "click"), { kind: "send", text: "/compact" }, "optional arguments may be left out");
eq(S.chooseSlash(pick("compact"), "tab"), { kind: "insert", text: "/compact " });
eq(S.chooseSlash(pick("steer"), "enter"), { kind: "insert", text: "/steer " }, "a required argument is typed first");
eq(S.chooseSlash(pick("compact", busy), "enter"), { kind: "none", reason: S.IDLE_REASON }, "Enter does nothing on a dimmed command");
ok(S.needsArguments(pick("steer")) && S.needsArguments(pick("agent")) && !S.needsArguments(pick("compact")) && !S.needsArguments(pick("new")));
eq([S.moveSlash(0, -1, 5), S.moveSlash(4, 1, 5), S.moveSlash(-1, 1, 5), S.moveSlash(-1, -1, 5), S.moveSlash(0, 1, 0)], [4, 0, 0, 4, 0]);

// Recent use: local storage only, bounded, refusals tolerated.
const store = new Map();
const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) };
S.recordUse(storage, "compact", 10);
S.recordUse(storage, "status", 20);
S.recordUse(storage, "<script>", 30);
eq({ ...S.readRecent(storage) }, { compact: 10, status: 20 }, "only command names are kept");
for (let i = 0; i < 40; i++) S.recordUse(storage, "c" + i, 100 + i);
eq(Object.keys(S.readRecent(storage)).length, 24, "bounded");
S.clearRecent(storage);
eq({ ...S.readRecent(storage) }, {});
eq(S.recordUse({ getItem() { throw new Error("SecurityError"); }, setItem() { throw new Error("SecurityError"); } }, "status", 1), { status: 1 });
eq({ ...S.readRecent({ getItem: () => "{broken" }) }, {});
console.log(`Slash command fixtures passed: ${checks} checks; no window action shares a host name, the 28-command catalogue validated, starter order then recent use, idle-only commands dimmed with the reason, argument completion (runs, sub-words, criteria, agents, views and formats, skills), and Enter/Tab/click choices.`);
