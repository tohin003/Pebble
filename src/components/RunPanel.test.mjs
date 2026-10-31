import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, defineComponent, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Compile only checked-in sources. Nested run panels are recorded stubs here (their
// own fixtures cover them); the inspectors are the real components, loaded lazily.
const require = createRequire(import.meta.url);
const calls = [];
const replies = {};
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: JSON.parse(JSON.stringify(params)) }); // IPC serialises to JSON
    if (!replies[method]) throw new Error("Unexpected RPC " + method);
    return replies[method](params);
  },
};
function evaluate(code, imports = {}) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(
    (name) => (name === "../bridge" ? bridge : imports[name] || require(name)), module, module.exports);
  return module.exports;
}
const shortcuts = evaluate(readFileSync(new URL("../shortcuts.ts", import.meta.url), "utf8"));
function compile(filename, imports = {}) {
  const source = readFileSync(new URL(filename, import.meta.url), "utf8");
  const descriptor = parse(source, { filename }).descriptor;
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  const component = evaluate(script.content, { "../shortcuts": shortcuts, ...imports }).default;
  component.render = evaluate(template.code, imports).render;
  return { component, source };
}
const mounted = [];
const stub = (name) => ({ __esModule: true, default: defineComponent({ name, props: ["chatId", "runId", "runState", "refreshKey"], setup(p) { mounted.push(name); return () => h("div", { "data-stub": name, "data-run": p.runId }, name); } }) });
const loaded = [];
const lazy = (file) => ({ __esModule: true, get default() { loaded.push(file); return compile(file).component; } });
const { component: RunPanel, source } = compile("./RunPanel.vue", {
  "./AgentPanel.vue": stub("AgentPanel"), "./ExecutionOutput.vue": stub("ExecutionOutput"),
  "./AttemptStream.vue": stub("AttemptStream"), "./ReconcilePanel.vue": stub("ReconcilePanel"),
  "./ContextInspector.vue": lazy("./ContextInspector.vue"), "./MemoryImpactView.vue": lazy("./MemoryImpactView.vue"),
  // Phase 22: the Context and Changes tabs open these directly; recorded stubs here (own fixtures cover them).
  "./ContextPanel.vue": stub("ContextPanel"), "./RewindPanel.vue": stub("RewindPanel"),
});
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source));
// Phase 24 (V1-029, deliberate): the three held-task routes join the fixed set; the panel never sends a `surface`.
assert.deepEqual([...new Set([...source.matchAll(/'(run_[a-z_]+|question_[a-z_]+)'/g)].map((m) => m[1]))].sort(), ["question_answer", "question_cancel", "run_held_accept", "run_held_finish", "run_held_tests", "run_pause", "run_resume"]);
assert.ok(!/surface/.test(source), "the shell, not the panel, names the calling window");
assert.ok(/defineAsyncComponent\(\(\) => import\('\.\/ContextInspector\.vue'\)\)/.test(source) && /defineAsyncComponent\(\(\) => import\('\.\/MemoryImpactView\.vue'\)\)/.test(source), "inspectors load only when opened");
assert.deepEqual(loaded, [], "compiling the panel loads no inspector");

function node(type, text = "") {
  return { type, text, children: [], props: {}, parent: null, value: "", addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document };
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
async function settle() { for (let i = 0; i < 12; i++) { await Promise.resolve(); await nextTick(); } }
const previous = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot };
globalThis.Document = class Document { activeElement = null; };
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
const vueErrors = [];
const hostile = "<img src=x onerror=host()>";
try {
  // The question contract AgentPanel.test.mjs also checks, with an agent requester.
  const childChat = {
    id: "chat-integration", title: "Fixture", workspace: null, messages: [], actions: [], runs: [{ id: "root-1", state: "running" }],
    questions: [{ id: "child-question", run_id: "root-1", revision: 1, required: true, status: "pending", requester_agent_id: "worker", requester_agent_name: hostile, parent_task_id: "coordinator", origin: "agent", items: [{ id: "scope", title: "Choose scope", options: [{ id: "local", label: "This folder" }], allow_free_text: true }] }],
  };
  const html = await renderToString(createSSRApp(RunPanel, { chat: childChat }));
  assert.ok(html.includes("Requested by &lt;img src=x onerror=host()&gt;") && html.includes("Task: coordinator") && html.includes("Submit answer"));
  assert.ok(html.includes("This agent needs your answer to continue. Other agents can keep working."));
  assert.ok(!html.includes("<img") && html.includes('role="status"'));
  assert.ok(html.includes('aria-controls="run-inspector"') && html.includes('aria-expanded="false"'));
  assert.deepEqual(calls, [], "server rendering makes no request");

  const chat = reactive({
    id: "chat-a", title: "Fixture", workspace: null, messages: [], actions: [], runs: [{ id: "run-1", state: "running", error: null }],
    questions: [{ id: "q1", run_id: "run-1", revision: 2, required: false, status: "pending", items: [{ id: "scope", title: "Scope " + hostile, options: [{ id: "local", label: "This folder", description: "Only here" }], allow_free_text: true }] }],
    plan: { run_id: "run-1", revision: 1, steps: [{ id: "a", title: "Read", status: "in_progress", evidence_ids: [] }] },
  });
  const props = reactive({ chat, memory: { enabled: false, strategy: "s", types: [{ id: "semantic", name: "Facts (host)", count: 4, enabled: true }] } });
  const root = node("root"), emitted = [];
  let s;
  const instrumented = { ...RunPanel, setup(p, context) { s = RunPanel.setup(p, { ...context, emit: (...args) => emitted.push(args) }); return s; } };
  const app = renderer.createApp({ setup: () => () => h(instrumented, props) });
  app.config.errorHandler = (error) => { vueErrors.push(error); };
  app.mount(root);
  await settle();
  const button = (label) => { const found = all(root).find((el) => el.type === "button" && textOf(el).trim() === label); assert.ok(found, "Missing button " + label); return found; };
  const stubs = () => all(root).filter((el) => el.props["data-stub"]).map((el) => [el.props["data-stub"], el.props["data-run"]]);
  assert.deepEqual(calls, [], "mounting opens no inspector and makes no request of its own");
  assert.deepEqual(stubs(), [["ExecutionOutput", "run-1"], ["AttemptStream", "run-1"], ["AgentPanel", "run-1"]]);
  // Phase 24 (V1-108, deliberate): run states read as sentences ("Task: running" before).
  assert.ok(textOf(root).includes("Pebble is working on this.") && textOf(root).includes("Scope " + hostile) && textOf(root).includes("Read"));

  // Inspectors open on demand, one at a time, for the current run.
  replies.run_get = (p) => ({ id: p.run_id, chat_id: p.id, state: "running", tools: [{ call_id: "1:a", name: "Write", state: "uncertain" }], context: null, sources: [], compactions: [], handoff: null });
  replies.memory_list = () => [];
  replies.memory_candidates = () => [];
  await button("Context").props.onClick(); await settle();
  assert.deepEqual(loaded, ["./ContextInspector.vue"]);
  assert.equal(button("Context").props["aria-expanded"], true);
  assert.deepEqual(calls.map((c) => [c.method, c.params]), [["run_get", { id: "chat-a", run_id: "run-1" }]]);
  assert.ok(textOf(root).includes("Context · task run-1") && textOf(root).includes("No provider context manifest has been recorded for this task yet."));
  calls.length = 0;
  await button("Memory impact").props.onClick(); await settle();
  assert.deepEqual(loaded, ["./ContextInspector.vue", "./MemoryImpactView.vue"]);
  assert.equal(button("Context").props["aria-expanded"], false);
  assert.deepEqual(calls.map((c) => c.method), ["memory_list", "memory_candidates", "run_get"]);
  assert.ok(!textOf(root).includes("Context · task"));
  const impact = textOf(root);
  assert.ok(impact.includes("Personalized memory is off") && impact.includes("Facts (host)") && impact.includes("Uncertain outcomes (never replayed)"), "memory state reaches the view");
  await button("Memory impact").props.onClick(); await settle();
  assert.ok(!textOf(root).includes("Memory impact ·") && !all(root).some((el) => el.props.id === "run-inspector"));
  await button("Context").props.onClick(); await settle();
  assert.equal(s.inspect.value, "context");
  chat.id = "chat-b"; await settle();
  assert.equal(s.inspect.value, "", "another conversation starts with inspectors closed");
  chat.id = "chat-a"; await settle();

  // Native controls send fixed routes with the current run.
  calls.length = 0;
  replies.run_pause = () => ({ status: "pausing" });
  await button("Pause").props.onClick(); await settle();
  assert.deepEqual(calls.at(-1), { method: "run_pause", params: { id: "chat-a", run_id: "run-1" } });
  assert.deepEqual(emitted.at(-1), ["refresh"]);
  const radio = all(root).find((el) => el.type === "input" && el.props.type === "radio" && el.props.value === "local");
  radio.props.onChange();
  replies.question_answer = () => ({ status: "answered" });
  const form = all(root).find((el) => el.type === "form");
  await form.props.onSubmit({ preventDefault() {} }); await settle();
  assert.deepEqual(calls.at(-1), { method: "question_answer", params: { id: "chat-a", question_id: "q1", revision: 2, answers: { scope: { option_id: "local" } } } });
  const area = all(root).find((el) => el.type === "textarea");
  assert.equal(area.props["aria-label"], "Scope " + hostile + " — written answer");
  area.props.onInput({ target: { value: "Only src/" } });
  await settle();
  await form.props.onSubmit({ preventDefault() {} }); await settle();
  assert.deepEqual(calls.at(-1).params.answers, { scope: { text: "Only src/" } });
  replies.question_cancel = () => { throw new Error("Question changed."); };
  await button("Skip").props.onClick(); await settle();
  assert.deepEqual(calls.at(-1), { method: "question_cancel", params: { id: "chat-a", question_id: "q1", revision: 2 } });
  assert.deepEqual(emitted.at(-1), ["error", "Error: Question changed."]);
  assert.equal(s.busy.value, "");

  // Budget and reconciliation states keep their main-window controls.
  chat.questions = [];
  chat.runs = [{ id: "run-1", state: "budget_exhausted", error: "Budget reached." }];
  await settle();
  replies.run_resume = () => ({ status: "queued" });
  await button("Resume").props.onClick(); await settle();
  assert.deepEqual(calls.at(-1), { method: "run_resume", params: { id: "chat-a", run_id: "run-1", limits: { max_steps: 128, max_seconds: 1800 } } });
  chat.runs = [{ id: "run-1", state: "needs_reconciliation", error: null }];
  await settle();
  assert.ok(stubs().some(([name, run]) => name === "ReconcilePanel" && run === "run-1"));
  chat.runs = []; await settle();
  assert.ok(!all(root).some((el) => el.type === "button" && ["Context", "Memory impact"].includes(textOf(el).trim())), "no run, no inspector toggles");
  app.unmount();
  assert.deepEqual(vueErrors, []);

  // Phase 22: the chat column ("conversation") shows only what needs the person; the Details drawer
  // ("details") shows the inspector without the question forms. The default above is unchanged.
  {
  const question = { id: "q9", run_id: "run-9", revision: 1, required: true, status: "pending", items: [{ id: "a", title: "Pick one", options: [{ id: "x", label: "X" }], allow_free_text: true }] };
  const viewChat = (state, extra = {}) => ({ id: "chat-v", title: "F", workspace: null, messages: [], actions: [], runs: [{ id: "run-9", state, error: "Provider stalled." }], questions: [], ...extra });
  const render = (chat, view) => renderToString(createSSRApp(RunPanel, { chat, view }));
  const before = calls.length;
  assert.equal(await render(viewChat("running"), "conversation"), "<!--v-if-->", "a running task adds nothing to the chat column");
  let html = await render(viewChat("running", { questions: [question] }), "conversation");
  assert.ok(html.includes("Submit answer") && html.includes("Pick one"), "a pending question is asked in the conversation");
  assert.ok(!/Context use|Memory impact|ExecutionOutput|AttemptStream|AgentPanel|Pause|Pebble is working on this/.test(html), "no inspector, stream or agent panel in the chat column");
  html = await render(viewChat("failed"), "conversation");
  // Phase 24 (V1-108, deliberate): "Stopped by an error." replaces "Task: failed".
  assert.ok(html.includes("Stopped by an error.") && html.includes("Provider stalled.") && html.includes(">Details<") && html.includes(">Resume<"), "a failed task is a one-line note with its own Resume");
  html = await render(viewChat("paused"), "conversation");
  assert.ok(html.includes(">Resume<") && html.includes(">Details<"));
  html = await render(viewChat("budget_exhausted"), "conversation");
  assert.ok(html.includes("Step limit") && html.includes(">Resume<"), "the budget form stays with its Resume");
  html = await render(viewChat("running", { questions: [question] }), "details");
  assert.ok(!html.includes("Submit answer") && html.includes("A question is waiting for your answer in the conversation."), "the drawer never duplicates a question form");
  assert.ok(html.includes("Context use") && html.includes("AgentPanel") && html.includes("Pause"), "the drawer has the inspector");
  // The drawer's tabs each take one part (design study §10.3).
  const has = (page, ...words) => words.every((w) => page.includes(w));
  const lacks = (page, ...words) => words.every((w) => !page.includes(w));
  html = await render(viewChat("running", { questions: [question], plan: { run_id: "run-9", revision: 1, steps: [{ id: "s", title: "Read the notes", status: "pending", evidence_ids: [] }] } }), "activity");
  assert.ok(has(html, "Pebble is working on this.", "Pause", "ExecutionOutput", "AttemptStream", "A question is waiting") && lacks(html, "Context use", "Read the notes", "AgentPanel", "Submit answer"), "Activity: state, output and stream");
  html = await render(viewChat("running", { plan: { run_id: "run-9", revision: 1, steps: [{ id: "s", title: "Read the notes", status: "pending", evidence_ids: [] }] } }), "task");
  assert.ok(has(html, "Read the notes", "AgentPanel") && lacks(html, "Pebble is working on this.", "ExecutionOutput", "Context use"), "Task: plan and agents");
  html = await render(viewChat("completed"), "context");
  assert.ok(has(html, "Context use", "Memory impact", 'data-stub="ContextPanel"') && lacks(html, "Rewind files", "ExecutionOutput", "AgentPanel", 'role="status"'), "Context: the inspectors, Context use open first, no rewind");
  html = await renderToString(createSSRApp(RunPanel, { chat: { ...viewChat("running"), runs: [{ id: "run-live", state: "running" }, { id: "run-9", state: "completed" }] }, view: "changes" }));
  assert.ok(html.includes('data-stub="RewindPanel" data-run="run-9"') && lacks(html, 'role="status"', "ExecutionOutput", "Context use"), "Changes: rewind for the latest settled task");
  assert.equal(await render(viewChat("running"), "changes"), "<!--v-if-->", "nothing to rewind while the only task is live");
  assert.equal(calls.length, before, "rendering the views makes no request");

  // A slash command's focus opens one inspector for the current run; Details only emits.
  const focusProps = reactive({ chat: viewChat("failed"), view: "details", focus: null });
  const root2 = node("root"), emitted2 = [];
  let s2;
  const instrumented2 = { ...RunPanel, setup(p, context) { s2 = RunPanel.setup(p, { ...context, emit: (...args) => emitted2.push(args) }); return s2; } };
  const app2 = renderer.createApp({ setup: () => () => h(instrumented2, focusProps) });
  app2.config.errorHandler = (error) => { vueErrors.push(error); };
  app2.mount(root2);
  await settle();
  assert.equal(s2.inspect.value, "");
  focusProps.focus = { what: "memory", n: 1 };
  await settle();
  assert.equal(s2.inspect.value, "memory");
  focusProps.focus = { what: "agents", n: 2 };
  await settle();
  assert.equal(s2.inspect.value, "memory", "a focus without an inspector changes nothing");
  focusProps.focus = { what: "context", n: 3 };
  await settle();
  assert.equal(s2.inspect.value, "context");
  focusProps.view = "conversation";
  await settle();
  const details = all(root2).find((el) => el.type === "button" && textOf(el).trim() === "Details");
  details.props.onClick();
  assert.deepEqual(emitted2.at(-1), ["details"]);
  app2.unmount();
  assert.deepEqual(vueErrors, []);
  }

  // Phase 43 (1.9.0): a Companion task (no project) held at verification required is not offered "Run the tests" (the
  // backend refuses it there: there is no project to test); the other two decisions stay.
  {
  const companion = reactive({ id: "chat-c", title: "C", workspace: null, messages: [], actions: [], questions: [],
    runs: [{ id: "run-c", state: "verification_required", error: "The app was only opened; nothing shows it was operated." }] });
  const html = await renderToString(createSSRApp(RunPanel, { chat: companion, view: "conversation" }));
  assert.ok(!html.includes(">Run the tests<") && html.includes(">Ask Pebble to finish<") && html.includes(">Accept as unverified<"), html);
  }

  // Phase 24 (V1-029): a task held at verification required offers three decisions, never a Resume that repeats the
  // verdict; accepting it unverified takes a second, explicit click. Every request names only the chat and the run.
  {
  const heldChat = reactive({ id: "chat-h", title: "F", workspace: "/p", messages: [], actions: [], questions: [],
    runs: [{ id: "run-h", state: "verification_required", error: "Pebble changed calc.py but did not run the tests that cover it." }] });
  let html = await renderToString(createSSRApp(RunPanel, { chat: heldChat, view: "conversation" }));
  assert.ok(html.includes("Pebble&#39;s change isn&#39;t verified yet.") && html.includes("did not run the tests"), "the held state is a sentence with its reason");
  assert.ok(html.includes(">Run the tests<") && html.includes(">Ask Pebble to finish<") && html.includes(">Accept as unverified<") && !html.includes(">Resume<"));
  assert.ok(html.includes('role="group"') && html.includes('aria-label="Finish this task"'));
  const root3 = node("root"), emitted3 = [];
  const instrumented3 = { ...RunPanel, setup(p, context) { return RunPanel.setup(p, { ...context, emit: (...args) => emitted3.push(args) }); } };
  const app3 = renderer.createApp({ setup: () => () => h(instrumented3, { chat: heldChat, view: "details" }) });
  app3.config.errorHandler = (error) => { vueErrors.push(error); };
  app3.mount(root3);
  await settle();
  const held = (label) => { const found = all(root3).find((el) => el.type === "button" && textOf(el).trim() === label); assert.ok(found, "Missing button " + label); return found; };
  calls.length = 0;
  replies.run_held_tests = () => ({ run_id: "run-h", status: "waiting_approval", action_id: "a1" });
  replies.run_held_finish = () => ({ run_id: "run-h", status: "queued" });
  replies.run_held_accept = () => ({ run_id: "run-h", status: "completed", verified: false, origin: "human" });
  await held("Run the tests").props.onClick(); await settle();
  await held("Ask Pebble to finish").props.onClick(); await settle();
  assert.deepEqual(calls.map((c) => [c.method, c.params]), [["run_held_tests", { id: "chat-h", run_id: "run-h" }], ["run_held_finish", { id: "chat-h", run_id: "run-h" }]]);
  held("Accept as unverified").props.onClick(); await settle();
  assert.equal(calls.length, 2, "the first click only asks");
  assert.ok(textOf(root3).includes("Keep this change without verification? Pebble records that you decided."));
  held("Cancel").props.onClick(); await settle();
  assert.ok(!textOf(root3).includes("Keep this change without verification?"));
  held("Accept as unverified").props.onClick(); await settle();
  await held("Accept as unverified").props.onClick(); await settle();
  assert.deepEqual(calls.at(-1), { method: "run_held_accept", params: { id: "chat-h", run_id: "run-h" } });
  assert.ok(calls.every((c) => !("surface" in c.params)));
  assert.deepEqual(emitted3.filter((e) => e[0] === "refresh").length, 3);
  heldChat.runs = [{ id: "run-h", state: "completed", error: "You accepted this task as unverified." }];
  await settle();
  assert.ok(!all(root3).some((el) => el.type === "button" && ["Run the tests", "Ask Pebble to finish"].includes(textOf(el).trim())), "decided: no held actions");
  app3.unmount();
  assert.deepEqual(vueErrors, []);
  html = await renderToString(createSSRApp(RunPanel, { chat: { ...heldChat, runs: [{ id: "r", state: "needs_reconciliation", error: null }] }, view: "details" }));
  assert.ok(html.includes("One step has an uncertain outcome. Check it before resuming.") && !html.includes("Run the tests"));
  // Phase 45 (F236, audit round R10): a run whose reply still says the work remains does not read "Done."
  html = await renderToString(createSSRApp(RunPanel, { chat: { ...heldChat, runs: [{ id: "r", state: "completed", error: "Pebble's reply says the task isn't finished." }] }, view: "details" }));
  assert.ok(html.includes("Ended without finishing.") && !html.includes("Done."));
  html = await renderToString(createSSRApp(RunPanel, { chat: { ...heldChat, runs: [{ id: "r", state: "completed", error: "You accepted this task as unverified." }] }, view: "details" }));
  assert.ok(html.includes("Done.") && !html.includes("Ended without finishing."), "an accepted task stays done");
  }
  // Phase 45 (F239, audit round R10): the Needs a check block's Done and End access send the paused window's session
  // alone; with the conversation's id added, the backend refused the call and Done showed a Python error
  {
  const pausedChat = reactive({ id: "chat-p", title: "P", workspace: null, messages: [], actions: [], questions: [],
    runs: [{ id: "run-p", state: "needs_reconciliation", error: null }] });
  let sessions = [{ session_id: "S1", state: "interrupted", target: { bundle_id: "com.apple.Preview" } }];
  replies.screen_sessions = () => sessions;
  replies.screen_handoff_done = (params) => { assert.deepEqual(Object.keys(params), ["session_id"]); sessions = []; return { session_id: params.session_id, state: "active" }; };
  replies.screen_session_revoke = (params) => { assert.deepEqual(Object.keys(params), ["session_id"]); sessions = []; return { session_id: params.session_id, state: "revoked" }; };
  for (const [label, method] of [["Done", "screen_handoff_done"], ["End access", "screen_session_revoke"]]) {
    sessions = [{ session_id: "S1", state: "interrupted", target: { bundle_id: "com.apple.Preview" } }];
    const rootP = node("root"), emittedP = [];
    const instrumentedP = { ...RunPanel, setup(p, context) { return RunPanel.setup(p, { ...context, emit: (...args) => emittedP.push(args) }); } };
    const appP = renderer.createApp({ setup: () => () => h(instrumentedP, { chat: pausedChat, view: "conversation" }) });
    appP.config.errorHandler = (error) => { vueErrors.push(error); };
    appP.mount(rootP);
    await settle();
    assert.ok(textOf(rootP).includes("Preview's window is paused"), textOf(rootP));
    const found = all(rootP).find((el) => el.type === "button" && textOf(el).trim() === label);
    assert.ok(found, "Missing button " + label);
    calls.length = 0;
    await found.props.onClick(); await settle();
    assert.deepEqual(calls.filter((c) => c.method === method).map((c) => c.params), [{ session_id: "S1" }]);
    assert.deepEqual(emittedP.filter((e) => e[0] === "error"), [], "no error reaches the person");
    assert.ok(emittedP.some((e) => e[0] === "refresh") && !textOf(rootP).includes("window is paused"), "the block goes once decided");
    // Phase 45 (F254, audit round R11): after Done the task reads as checked, and the banner may clear
    assert.ok(emittedP.some((e) => e[0] === "settled"), JSON.stringify(emittedP));
    if (label === "Done") assert.ok(textOf(rootP).includes("Checked: Pebble can use the window again."), textOf(rootP));
    appP.unmount();
  }
  // Phase 45 (F255, audit round R11): a later task is the latest, and the earlier task's paused window keeps its block
  sessions = [{ session_id: "S2", state: "interrupted", target: { bundle_id: "com.apple.Preview" } }];
  const laterChat = reactive({ ...pausedChat, runs: [{ id: "run-later", state: "completed", error: null }, { id: "run-p", state: "needs_reconciliation", error: null }] });
  const rootL = node("root");
  const appL = renderer.createApp({ setup: () => () => h(RunPanel, { chat: laterChat, view: "conversation" }) });
  appL.config.errorHandler = (error) => { vueErrors.push(error); };
  appL.mount(rootL);
  await settle();
  assert.ok(textOf(rootL).includes("Preview's window is paused"), textOf(rootL));
  appL.unmount();
  assert.deepEqual(vueErrors, []);
  }
  console.log("Run panel fixtures passed: question contract and escaping, no request on mount, lazy context and memory inspectors for the current run, reset on conversation change, pause/answer/skip/resume routes, errors, reconciliation and nested panel scoping; Phase 22 conversation and details views and slash-command focus; Phase 24 state sentences and the three held-task decisions.");
} finally {
  Object.assign(globalThis, previous);
}
