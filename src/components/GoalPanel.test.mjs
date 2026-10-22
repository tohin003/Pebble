import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Compile only checked-in sources; goal text is data and is never evaluated.
const require = createRequire(import.meta.url);
const calls = [];
const clone = (value) => structuredClone(value);
function deferred() { let resolve; const promise = new Promise((yes) => (resolve = yes)); return { promise, resolve }; }
function evaluate(code, imports = {}) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(
    (name) => (name === "../bridge" ? bridge : imports[name] || require(name)), module, module.exports);
  return module.exports;
}
const shortcuts = evaluate(readFileSync(new URL("../shortcuts.ts", import.meta.url), "utf8"));
function compile(filename) {
  const source = readFileSync(new URL(filename, import.meta.url), "utf8");
  const descriptor = parse(source, { filename }).descriptor;
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  const component = evaluate(script.content, { "../shortcuts": shortcuts }).default;
  component.render = evaluate(template.code).render;
  return { component, source };
}

// A fake host with A3's contract (goals.py): chat scoping, revision compare-and-swap,
// host-authored refusals, and snapshots shaped like Goals._snapshot.
const hostile = "<img src=x onerror=host()>";
const NOTE = "Goal records are operational records, not memory.";
const server = { goal: null, history: [], barrier: null, refuse: {} };
function freshGoal() {
  return {
    id: "g1", chat_id: "chat-a", origin: "human", title: "Ship the export", revision: 3, state: "blocked", created: 1, updated: 2,
    criteria: [
      { id: "c1", text: "Tests pass", kind: "evidence", status: "open", origin: "human", checked_by: null, evidence: [], detail: "waiting for step s2", proposed_run_id: null, updated: 1 },
      { id: "c2", text: hostile + " looks right", kind: "human_check", status: "met", origin: "human", checked_by: "human", evidence: [], detail: null, proposed_run_id: null, updated: 1 },
      { id: "c3", text: "Docs updated <script>approve()</script>", kind: "human_check", status: "candidate", origin: "agent", checked_by: null, evidence: [], detail: null, proposed_run_id: "run-1", updated: 1 },
      { id: "c4", text: "Rejected idea", kind: "human_check", status: "rejected", origin: "agent", checked_by: null, evidence: [], detail: null, proposed_run_id: "run-1", updated: 1 },
      { id: "c5", text: "Odd record", kind: "constructor", status: "constructor", origin: "human", checked_by: null, evidence: [], detail: null, proposed_run_id: null, updated: 1 },
    ],
    steps: [
      { id: "s1", title: "Write code", status: "completed", depends_on: [], criteria: [], evidence: [{ run_id: "run-1", call_id: "1:a" }], evidence_current: false, blocker: null, run_id: "run-1", ready: false, updated: 1 },
      { id: "s2", title: "Run tests", status: "blocked", depends_on: ["s1"], criteria: ["c1"], evidence: [], evidence_current: null, blocker: { kind: "question", ref: "q1", active: true }, run_id: "run-1", ready: false, updated: 1 },
      { id: "s3", title: "Wait for review", status: "blocked", depends_on: [], criteria: [], evidence: [], evidence_current: null, blocker: { kind: "text", ref: "Waiting for " + hostile, active: true }, run_id: null, ready: false, updated: 1 },
    ],
    blockers: [
      { kind: "question", ref: "q1", label: "Needs your answer", run_id: "run-1", step_id: "s2", detail: "Which <b>branch</b>?", detail_origin: "agent" },
      { kind: "approval", ref: "a1", label: "Needs your review", run_id: "run-1" },
    ],
    progress: { criteria_met: 1, criteria_total: 2, candidates: 1, steps_completed: 1, steps_total: 3 },
    runs: ["run-1"], authorization: "none", note: NOTE,
  };
}
function summary(goal) {
  return { id: goal.id, title: goal.title, state: goal.state, revision: goal.revision, created: goal.created, updated: goal.updated,
    progress: goal.progress, blockers: goal.blockers.length, blocker: goal.blockers[0] ? { kind: goal.blockers[0].kind, label: goal.blockers[0].label } : null };
}
const ROUTES = ["goal_list", "goal_get", "goal_create", "goal_state", "goal_criterion_add", "goal_criterion_edit", "goal_criterion_check", "goal_criterion_review", "goal_step_add", "goal_step_block", "goal_continue"];
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: clone(params) });
    assert.ok(ROUTES.includes(method), "unexpected route " + method);
    if (server.barrier) await server.barrier.promise;
    if (server.refuse[method]) throw new Error(server.refuse[method]);
    if (method === "goal_list") {
      const current = server.goal && ["active", "paused", "blocked"].includes(server.goal.state) && params.chat_id === "chat-a" ? [summary(server.goal)] : [];
      return [...current, ...(params.chat_id === "chat-a" ? server.history.map(summary) : [])];
    }
    if (method === "goal_create") {
      server.goal = { ...freshGoal(), id: "g2", title: params.title, revision: 1, state: "active", criteria: [], steps: [], blockers: [], progress: { criteria_met: 0, criteria_total: 0, candidates: 0, steps_completed: 0, steps_total: 0 } };
      return clone(server.goal);
    }
    const goal = server.goal;
    if (!goal || params.goal_id !== goal.id || params.chat_id !== goal.chat_id) throw new Error("Goal not found in this conversation.");
    if (method === "goal_get") return server.wrongChat ? { ...clone(goal), chat_id: "chat-b" } : clone(goal);
    if (method === "goal_continue") { assert.ok(!("revision" in params)); return { message_id: "m1", run_id: "run-2" }; }
    if (params.revision !== goal.revision) throw new Error(`This goal changed (revision ${goal.revision}). Refresh it before editing.`);
    const criterion = goal.criteria.find((c) => c.id === params.criterion_id);
    if (method === "goal_state") {
      if (params.state === "done") throw new Error("This goal is not done yet: criterion c1 is open (waiting for step s2); resolve 2 blockers.");
      goal.state = params.state;
      if (params.state === "abandoned") { server.history.unshift(goal); server.goal = { ...goal }; }
    } else if (method === "goal_criterion_check") {
      if (criterion.kind === "evidence" && params.met) throw new Error("Criterion c1 has no evidence yet: waiting for step s2.");
      Object.assign(criterion, { status: params.met ? "met" : "unmet", checked_by: "human" });
    } else if (method === "goal_criterion_review") criterion.status = params.decision === "accept" ? "open" : "rejected";
    else if (method === "goal_criterion_edit") Object.assign(criterion, { text: params.text, status: "open", checked_by: null });
    else if (method === "goal_criterion_add") goal.criteria.push({ id: "c6", text: params.text, kind: params.kind, status: "open", origin: "human", checked_by: null, evidence: [], detail: null });
    else if (method === "goal_step_add") goal.steps.push({ id: "s4", title: params.title, status: "pending", depends_on: params.depends_on, criteria: [], evidence: [], evidence_current: null, blocker: null, run_id: null, ready: true });
    else if (method === "goal_step_block") {
      const step = goal.steps.find((s) => s.id === params.step_id);
      Object.assign(step, "text" in params ? { status: "blocked", blocker: { kind: "text", ref: params.text, active: true } } : { status: "pending", blocker: null });
    }
    goal.revision++;
    return clone(goal);
  },
};
const { component: GoalPanel, source } = compile("./GoalPanel.vue");

// Static boundaries: literal content, only A3's native goal routes, never in generated or pet surfaces.
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source));
const used = [...new Set([...source.matchAll(/"(goal_[a-z_]+)"/g)].map((m) => m[1]))].sort();
assert.deepEqual(used, [...ROUTES].sort());
const direct = [...source.matchAll(/\brpc(?:<[^>]*>)?\(\s*([^,)]*)/g)].map((m) => m[1].trim());
assert.ok(direct.length && direct.every((arg) => arg === "method" || /^"goal_[a-z_]+"$/.test(arg)), "every route is a fixed goal literal");
const goals = readFileSync(new URL("../../backend/pebble/goals.py", import.meta.url), "utf8");
for (const route of used) assert.ok(goals.includes(`'${route}': lambda`), route + " is an A3 native route");
assert.ok(!/approve|question_answer|artifact_submit/.test(source));
for (const surface of ["./NativeArtifactRenderer.vue", "./ArtifactPanel.vue", "./ModsSettings.vue", "../Pet.vue"])
  assert.ok(!readFileSync(new URL(surface, import.meta.url), "utf8").includes("GoalPanel"), surface + " never renders the goal panel");

// A small host DOM (as AgentPanel.test.mjs): real hooks and handlers, no HTML parser.
function node(type, text = "") {
  return { type, text, children: [], props: {}, parent: null, value: "", options: [], addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document };
}
const renderer = createRenderer({
  createElement: (type) => node(type), createText: (text) => node("#text", text), createComment: (text) => node("#comment", text),
  insert(child, parent, anchor) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child);
  },
  remove(child) { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null; },
  setText(child, text) { child.text = text; },
  setElementText(child, text) { child.text = text; child.children = []; },
  patchProp(child, key, _previous, value) { child.props[key] = value; },
  parentNode: (child) => child.parent,
  nextSibling: (child) => child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
});
const textOf = (el) => (el.type === "#comment" ? "" : el.text) + el.children.map(textOf).join("");
const all = (el) => [el, ...el.children.flatMap(all)];
async function settle() { for (let i = 0; i < 10; i++) { await Promise.resolve(); await nextTick(); } }
const previous = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot };
globalThis.Document = class Document { activeElement = null; };
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
const vueErrors = [];
const props = reactive({ chatId: "chat-a", refreshKey: 0, running: false });
const root = node("root"), emitted = [];
let s;
const instrumented = { ...GoalPanel, setup(p, context) { s = GoalPanel.setup(p, { ...context, emit: (...args) => emitted.push(args) }); return s; } };
const app = renderer.createApp({ setup: () => () => h(instrumented, props) });
app.config.errorHandler = (error) => { vueErrors.push(error); };
const buttons = () => all(root).filter((el) => el.type === "button");
function button(name) {
  const found = buttons().find((el) => el.props["aria-label"] === name || textOf(el).trim() === name);
  assert.ok(found, "Missing button " + name);
  return found;
}
async function click(name) {
  const target = button(name);
  assert.ok(!target.props.disabled, name + " must be enabled");
  if (target.props.type === "submit") {
    let form = target.parent;
    while (form && form.type !== "form") form = form.parent;
    await form.props.onSubmit({ preventDefault() {} });
  } else await target.props.onClick();
  await settle();
}
const last = (method) => calls.filter((c) => c.method === method).at(-1)?.params;
const count = (method) => calls.filter((c) => c.method === method).length;
const alert = () => all(root).filter((el) => el.props.role === "alert").map(textOf).join(" ");
const tables = () => all(root).filter((el) => el.type === "table");
const caption = (t) => textOf(t.children.find((c) => c.type === "caption"));
const rowsOf = (t) => all(t).filter((el) => el.type === "tr" && el.parent?.type === "tbody");
let mounted = false;
try {
  server.goal = freshGoal();
  server.history = [{ ...freshGoal(), id: "g0", title: "Old goal", state: "done", updated: 1700000000 }];
  app.mount(root); mounted = true;
  await settle();
  assert.deepEqual(calls.map((c) => c.method), ["goal_list", "goal_get"]);
  assert.deepEqual(last("goal_list"), { chat_id: "chat-a", limit: 20 });
  assert.deepEqual(last("goal_get"), { chat_id: "chat-a", goal_id: "g1" });
  assert.ok(textOf(root).includes("Goal · Ship the export"));
  assert.ok(textOf(root).includes("blocked · 1/2 criteria · 1/3 steps · blocked: needs your answer"));
  assert.equal(tables().length, 0, "collapsed by default");
  const toggle = buttons().find((el) => el.props["aria-controls"] === "goal-body");
  assert.equal(toggle.props["aria-expanded"], false);
  await toggle.props.onClick(); await settle();
  assert.equal(toggle.props["aria-expanded"], true);

  const [criteria, proposals, steps, blockers, earlier] = tables();
  assert.deepEqual(tables().map(caption), ["Completion criteria · 1/2 met", "Proposed by Pebble · accept or reject", "Steps · 1/3 completed", "Blockers (host records)", "Earlier goals"]);
  assert.deepEqual(rowsOf(criteria).map((tr) => textOf(tr.children[0])), ["c1", "c2", "c5"]);
  assert.ok(textOf(criteria).includes(hostile + " looks right"), "goal text stays a literal text node");
  assert.ok(textOf(criteria).includes("waiting for step s2") && textOf(criteria).includes("by you"));
  assert.ok(textOf(rowsOf(criteria)[2]).includes("unknown") && !textOf(criteria).includes("function"), "unknown host words are bounded");
  assert.ok(textOf(root).includes("1 rejected proposal is kept in the goal history and not listed."));
  assert.ok(textOf(proposals).includes("Docs updated <script>approve()</script>") && textOf(proposals).includes("agent-authored"));
  assert.ok(textOf(steps).includes("1 receipt · not current") && textOf(steps).includes("Your note: Waiting for " + hostile) && textOf(steps).includes("Question"));
  assert.ok(textOf(blockers).includes("Which <b>branch</b>?") && textOf(blockers).includes("Answer it in the task panel"));
  assert.ok(textOf(earlier).includes("Old goal") && textOf(earlier).includes("done"));
  assert.ok(textOf(root).includes(NOTE));
  for (const table of tables()) {
    const rows = rowsOf(table);
    assert.deepEqual(rows.map((tr) => tr.props.tabindex), rows.map((_, i) => (i ? -1 : 0)), "roving row focus");
    if (rows.length > 1) {
      const body = { children: [] };
      for (const tabIndex of [0, -1]) body.children.push({ tagName: "TR", parentElement: body, tabIndex, focused: 0, focus() { this.focused++; } });
      rows[0].props.onKeydown({ key: "ArrowDown", target: body.children[0], preventDefault() {} });
      assert.deepEqual(body.children.map((r) => [r.tabIndex, r.focused]), [[-1, 0], [0, 1]], caption(table) + " rows use the shared arrow-key handler");
    }
  }

  // A human check, sent with the reviewed revision.
  await click("Mark not met: criterion c2");
  assert.deepEqual(last("goal_criterion_check"), { chat_id: "chat-a", goal_id: "g1", revision: 3, criterion_id: "c2", met: false });
  assert.equal(s.goal.value.revision, 4);
  assert.ok(textOf(root).includes("Criterion c2 marked not met."));
  // An evidence check asks the host; its refusal is shown and the record re-read.
  const lists = count("goal_list");
  await click("Verify receipts for criterion c1");
  assert.deepEqual(last("goal_criterion_check"), { chat_id: "chat-a", goal_id: "g1", revision: 4, criterion_id: "c1", met: true });
  assert.ok(alert().includes("Criterion c1 has no evidence yet: waiting for step s2."));
  assert.equal(count("goal_list"), lists + 1);
  // Revision drift: refused, re-read, then the same click succeeds against the fresh revision.
  server.goal.revision = 9;
  await click("Accept proposed criterion c3");
  assert.ok(alert().includes("This goal changed (revision 9). Refresh it before editing."));
  assert.equal(s.goal.value.revision, 9);
  await click("Accept proposed criterion c3");
  assert.deepEqual(last("goal_criterion_review"), { chat_id: "chat-a", goal_id: "g1", revision: 9, criterion_id: "c3", decision: "accept" });
  assert.equal(alert(), "");

  // Criteria and steps are added by the person; a refusal keeps the draft.
  s.draft.criterion = "  Docs reviewed  "; s.draft.kind = "evidence";
  server.refuse.goal_criterion_add = "This goal already has that criterion.";
  await s.addCriterion(); await settle();
  assert.equal(s.draft.criterion, "  Docs reviewed  ");
  assert.ok(alert().includes("This goal already has that criterion."));
  delete server.refuse.goal_criterion_add;
  await s.addCriterion(); await settle();
  assert.deepEqual(last("goal_criterion_add"), { chat_id: "chat-a", goal_id: "g1", revision: 10, text: "Docs reviewed", kind: "evidence" });
  assert.equal(s.draft.criterion, "");
  s.startEdit(s.goal.value.criteria[1]); s.draft.edit = "Looks right on a phone"; await settle();
  assert.ok(all(root).some((el) => el.type === "input" && el.props["aria-label"] === "Edit criterion c2"));
  await click("Save");
  assert.deepEqual(last("goal_criterion_edit"), { chat_id: "chat-a", goal_id: "g1", revision: 11, criterion_id: "c2", text: "Looks right on a phone" });
  assert.equal(s.editing.value, "");
  server.refuse.goal_step_add = "Step dependencies cannot form a cycle (s1, s4).";
  s.draft.step = "Deploy"; s.draft.depends = ["s1"];
  await s.addStep(); await settle();
  assert.ok(alert().includes("cannot form a cycle") && s.draft.step === "Deploy");
  delete server.refuse.goal_step_add;
  await s.addStep(); await settle();
  assert.deepEqual(last("goal_step_add"), { chat_id: "chat-a", goal_id: "g1", revision: 12, title: "Deploy", depends_on: ["s1"] });
  assert.deepEqual([s.draft.step, s.draft.depends], ["", []]);
  assert.deepEqual(s.blockable.value.map((step) => step.id), ["s4"], "completed and blocked steps cannot be blocked again");
  s.draft.blockStep = "s4"; s.draft.blockText = " Waiting on vendor "; await settle();
  await click("Block step");
  assert.deepEqual(last("goal_step_block"), { chat_id: "chat-a", goal_id: "g1", revision: 13, step_id: "s4", text: "Waiting on vendor" });
  await click("Unblock step s3");
  assert.deepEqual(last("goal_step_block"), { chat_id: "chat-a", goal_id: "g1", revision: 14, step_id: "s3" }, "clearing a blocker sends no text");

  // Done needs every criterion; the host's reasons are shown and nothing changes.
  await click("Mark done");
  assert.ok(alert().includes("This goal is not done yet: criterion c1 is open"));
  assert.equal(s.goal.value.state, "blocked");
  // Approval blockers deep-link to the review inspector.
  await click("Review");
  assert.deepEqual(emitted, [["reviews"]]);

  // Continue starts a task through the host only when nothing runs; it sends no revision.
  props.running = true; await settle();
  assert.equal(button("Continue goal").props.disabled, true);
  await s.continueGoal();
  assert.equal(count("goal_continue"), 0);
  props.running = false; await settle();
  await click("Continue goal");
  assert.deepEqual(last("goal_continue"), { chat_id: "chat-a", goal_id: "g1" });
  assert.ok(textOf(root).includes("Started a new task toward this goal."));

  // One action at a time: a second click during a pending reply is ignored.
  server.barrier = deferred();
  const pausing = s.setState("paused", "Goal paused.");
  await settle();
  assert.equal(button("Mark done").props.disabled, true);
  assert.equal(await s.check(s.goal.value.criteria[0], false), false);
  server.barrier.resolve(); server.barrier = null; await pausing; await settle();
  assert.deepEqual(last("goal_state"), { chat_id: "chat-a", goal_id: "g1", revision: 15, state: "paused" });
  assert.ok(button("Resume") && !buttons().some((el) => textOf(el) === "Continue goal"));
  await click("Resume");
  assert.equal(last("goal_state").state, "active");

  // Abandon is two-step and closes the record; the panel then offers a new goal.
  const states = count("goal_state");
  await click("Abandon…");
  assert.equal(count("goal_state"), states, "the first click only asks for confirmation");
  await click("Confirm: abandon goal");
  assert.equal(last("goal_state").state, "abandoned");
  assert.equal(s.goal.value, null);
  assert.ok(textOf(root).includes("No current goal.") && textOf(root).includes("Goal abandoned. Its record is kept and closed."));
  assert.ok(textOf(tables().at(-1)).includes("abandoned"));
  await click("Set a goal");
  s.draft.title = "  Plan the trip  "; await settle();
  await click("Start goal");
  assert.deepEqual(last("goal_create"), { chat_id: "chat-a", title: "Plan the trip" });
  assert.ok(textOf(root).includes("Goal · Plan the trip") && s.expanded.value);

  // A reply for another conversation or a late reply after switching is refused.
  server.wrongChat = true; s.error.value = "";
  await s.load(); await settle();
  assert.ok(alert().includes("different conversation"));
  server.wrongChat = false; s.error.value = "";
  server.barrier = deferred();
  s.draft.criterion = "Late";
  const late = s.addCriterion(); await settle();
  props.chatId = "chat-b"; await settle();
  server.barrier.resolve(); server.barrier = null; await late; await settle();
  assert.equal(s.goal.value, null);
  assert.equal(s.draft.criterion, "", "drafts do not follow the person into another conversation");
  assert.ok(!textOf(root).includes("Criterion added."));

  // Server rendering escapes every goal string.
  const seeded = { ...GoalPanel, setup(p, context) { const b = GoalPanel.setup(p, context); b.goal.value = freshGoal(); b.goals.value = [summary(freshGoal())]; b.expanded.value = true; return b; } };
  const html = await renderToString(createSSRApp(seeded, { chatId: "chat-a" }));
  assert.ok(html.includes("&lt;img src=x onerror=host()&gt; looks right") && html.includes("&lt;script&gt;approve()&lt;/script&gt;"));
  assert.ok(!/<(img|script|iframe|b)\b/.test(html));
  assert.ok(html.includes('<th scope="col">Criterion</th>') && html.includes("<caption>"));
  assert.deepEqual(vueErrors, []);
  console.log("Goal panel fixtures passed: A3 routes only, revision compare-and-swap and drift recovery, host refusals shown, human and evidence checks, proposals, steps and blockers, two-step abandon, continue gating, busy and stale replies, literal text and keyboard tables.");
} finally {
  if (mounted) app.unmount();
  Object.assign(globalThis, previous);
}
