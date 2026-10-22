import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, createSSRApp, defineComponent, h, nextTick, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Compile only checked-in sources; quiz text and host replies are data and are never evaluated.
const require = createRequire(import.meta.url);
const calls = [];
let reply = async () => null;
const bridge = { native: true, async rpc(method, params = {}) { calls.push({ method, params: structuredClone(params) }); return reply(method, params); } };
const icon = defineComponent({ props: { size: Number, weight: String }, setup: () => () => h("i") });
const icons = new Proxy({ __esModule: true }, { get: (target, name) => (name in target ? target[name] : icon), has: () => true });
function evaluate(code) {
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(
    (name) => (name === "../bridge" ? bridge : name === "@phosphor-icons/vue" ? icons : require(name)), module, module.exports);
  return module.exports;
}
const filename = "./QuizRenderer.vue";
const source = readFileSync(new URL(filename, import.meta.url), "utf8");
const descriptor = parse(source, { filename }).descriptor;
const script = compileScript(descriptor, { id: filename });
const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
const Quiz = evaluate(script.content).default;
Quiz.render = evaluate(template.code).render;

// Static boundary: no markup injection, storage or routes beyond the five quiz RPCs.
const markup = descriptor.template.content;
assert.ok(!/v-html|innerHTML|\beval\s*\(|new Function|<iframe|srcdoc|localStorage|sessionStorage|<form\b|<a\b/.test(source));
assert.ok(!/:(href|src|style|srcdoc|action|formaction)\b|v-bind="|v-on="|@\[/.test(markup));
assert.deepEqual([...source.matchAll(/from "([^"]+)"/g)].map((m) => m[1]).sort(), ["../bridge", "../types", "@phosphor-icons/vue", "vue"]);
assert.deepEqual([...new Set([...source.matchAll(/\brpc(?:<[^>]*>)?\(\s*"([^"]+)"/g)].map((m) => m[1]))].sort(), ["quiz_attempt", "quiz_save", "quiz_share", "quiz_start", "quiz_submit"]);
assert.ok(!/approve|reject|artifact_submit|question_answer/.test(source));

// A small host DOM: real Vue hooks and handlers, no HTML parser.
function node(type, text = "") {
  return { type, text, children: [], props: {}, parent: null, value: "", addEventListener() {}, removeEventListener() {} };
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
const buttons = (root, label) => all(root).filter((el) => el.type === "button" && textOf(el).includes(label));
const button = (root, label) => {
  const found = buttons(root, label);
  assert.equal(found.length, 1, "one button: " + label + " in " + textOf(root));
  return found[0];
};
async function settle() { for (let i = 0; i < 10; i++) { await Promise.resolve(); await nextTick(); } }
const vueErrors = [];
const apps = [];
// The countdown interval is unrelated to these fixtures and must not keep the process alive.
globalThis.setInterval = () => 0;
function mount(props) {
  const root = node("root"), emitted = [];
  let setup;
  const instrumented = { ...Quiz, setup(p, context) { setup = Quiz.setup(p, { ...context, emit: (...args) => emitted.push(args) }); return setup; } };
  const app = renderer.createApp({ setup: () => () => h(instrumented, props) });
  app.config.errorHandler = (error) => { vueErrors.push(error); };
  app.mount(root);
  apps.push(app);
  return { root, emitted, get s() { return setup; } };
}

const hostile = "<img src=x onerror=host()>";
const quiz = () => ({
  id: "quiz-1", chat_id: "chat-a", title: "Science " + hostile, kind: "quiz", revision: 1, status: "ready", owner_run_id: "run-1", created: 1,
  data: { duration_minutes: 10, questions: [
    { id: "q1", question: "Which gas do plants absorb? " + hostile, choices: ["Oxygen", "Carbon dioxide " + hostile] },
    { id: "q2", question: "Water boils at sea level at?", choices: ["90 °C", "100 °C", "110 °C"] },
  ] },
});
// A scripted host: it keeps the key, binds attempts to (chat, view, revision) and grades on submit.
const KEY = { q1: { answer: 1, explanation: "Plants take in carbon dioxide " + hostile }, q2: { answer: 1, explanation: "At one atmosphere." } };
let host;
function fakeHost({ revision = 1, deadline = 600 } = {}) {
  host = { attempts: new Map(), sequence: 0, revision, hold: null };
  reply = async (method, params) => {
    const find = () => {
      const attempt = host.attempts.get(params.attempt_id);
      if (!attempt || attempt.chat_id !== params.chat_id) throw new Error("Unknown quiz attempt.");
      return attempt;
    };
    const copy = (attempt) => { const { chat_id, ...rest } = structuredClone(attempt); return rest; };
    if (method === "quiz_attempt") {
      const found = [...host.attempts.values()].filter((a) => a.chat_id === params.chat_id && a.artifact_id === params.id && a.revision === params.revision).at(-1);
      return found ? copy(found) : null;
    }
    if (method === "quiz_start") {
      const attempt = { attempt_id: "att-" + ++host.sequence, chat_id: params.chat_id, artifact_id: params.id, revision: params.revision, status: "open", deadline: Date.now() / 1000 + deadline, answers: {} };
      host.attempts.set(attempt.attempt_id, attempt);
      return copy(attempt);
    }
    if (method === "quiz_save") {
      if (host.hold) await host.hold;
      const attempt = find();
      if (attempt.status !== "open") throw new Error("This attempt is closed.");
      attempt.answers = structuredClone(params.answers);
      return { saved: true };
    }
    if (method === "quiz_submit") {
      const attempt = find();
      if (params.revision !== host.revision) throw new Error(`This view changed to revision ${host.revision}.`);
      const per_question = Object.entries(KEY).map(([id, key]) => ({ id, correct: params.answers[id] === key.answer, chosen: params.answers[id], answer: key.answer, explanation: key.explanation }));
      Object.assign(attempt, { status: "submitted", answers: structuredClone(params.answers), result: { score: per_question.filter((q) => q.correct).length, total: 2, per_question } });
      return copy(attempt);
    }
    if (method === "quiz_share") {
      const attempt = find();
      attempt.shared = true;
      return { shared: true, attempt_id: attempt.attempt_id };
    }
    throw new Error("Unexpected RPC: " + method);
  };
}

// 1. Main window: read the attempt, start, answer (drafts to the host), submit, then share only on request.
fakeHost();
const props = reactive({ artifact: quiz(), surface: "main" });
const main = mount(props);
await settle();
assert.deepEqual(calls, [{ method: "quiz_attempt", params: { chat_id: "chat-a", id: "quiz-1", revision: 1 } }]);
let text = textOf(main.root);
assert.ok(text.includes("Quiz · checked by Pebble") && text.includes("2 questions") && text.includes("10 min"));
assert.ok(text.includes("the answer key is not part of this view") && text.includes("Your result stays here unless you share it."));
assert.ok(!/Correct|Incorrect|Share/.test(text), "nothing about grading or sharing before an attempt");
assert.ok(!text.includes("Self-check"), "v2 is never labelled a self-check");
calls.length = 0;
button(main.root, "Start quiz").props.onClick();
await settle();
assert.deepEqual(calls, [{ method: "quiz_start", params: { chat_id: "chat-a", id: "quiz-1", revision: 1 } }]);
text = textOf(main.root);
assert.ok(text.includes("Question 1 of 2") && text.includes("Which gas do plants absorb? " + hostile) && /(10:00|09:5\d) left/.test(text), text);
const radios = all(main.root).filter((el) => el.props.role === "radio");
assert.deepEqual(radios.map((el) => el.props["aria-checked"]), ["false", "false"]);
assert.ok(!all(main.root).some((el) => ["form", "input", "a", "iframe", "img"].includes(el.type)));
calls.length = 0;
radios[1].props.onClick();
await settle();
assert.deepEqual(calls, [{ method: "quiz_save", params: { chat_id: "chat-a", attempt_id: "att-1", revision: 1, answers: { q1: 1, q2: null } } }]);
assert.ok(textOf(main.root).includes("Draft saved") && textOf(main.root).includes("1 of 2 answered"));
assert.equal(all(main.root).filter((el) => el.props.role === "radio")[1].props["aria-checked"], "true");
// One save runs at a time; rapid changes coalesce into one follow-up carrying the latest answers.
let release;
host.hold = new Promise((resolve) => (release = resolve));
calls.length = 0;
all(main.root).filter((el) => el.props.role === "radio")[0].props.onClick();
await settle();
all(main.root).filter((el) => el.props.role === "radio")[1].props.onClick();
await settle();
assert.equal(calls.length, 1);
assert.ok(textOf(main.root).includes("Saving…"));
host.hold = null;
release();
await settle();
assert.deepEqual(calls.map((c) => c.params.answers), [{ q1: 0, q2: null }, { q1: 1, q2: null }]);
assert.deepEqual(host.attempts.get("att-1").answers, { q1: 1, q2: null });
button(main.root, "Next").props.onClick();
await settle();
assert.ok(textOf(main.root).includes("Question 2 of 2"));
all(main.root).filter((el) => el.props.role === "radio")[1].props.onClick();
await settle();
calls.length = 0;
button(main.root, "Submit answers").props.onClick();
await settle();
assert.deepEqual(calls, [{ method: "quiz_submit", params: { chat_id: "chat-a", attempt_id: "att-1", revision: 1, answers: { q1: 1, q2: 1 } } }]);
text = textOf(main.root);
assert.ok(text.includes("2 / 2") && text.includes("Graded by Pebble.") && text.includes("Question 1 · Correct"));
assert.ok(text.includes("Plants take in carbon dioxide " + hostile) && text.includes("Your answer: Carbon dioxide " + hostile));
assert.ok(text.includes("Pebble sees your score and each question's result only after you share them."));
assert.ok(!calls.some((c) => c.method === "quiz_share"), "no share without the person's click");
assert.equal(buttons(main.root, "Share in the workspace").length, 0);
calls.length = 0;
button(main.root, "Share result with Pebble").props.onClick();
await settle();
assert.deepEqual(calls, [{ method: "quiz_share", params: { chat_id: "chat-a", attempt_id: "att-1" } }]);
assert.ok(textOf(main.root).includes("Shared with Pebble") && buttons(main.root, "Share result").length === 0);
await main.s.share();
assert.equal(calls.length, 1, "a shared result is not shared again");
assert.deepEqual(main.emitted, []);

// 2. The pet never shares: it shows "Share in the workspace", which only opens the workspace.
calls.length = 0;
const pet = mount(reactive({ artifact: quiz(), surface: "pet" }));
await settle();
text = textOf(pet.root);
assert.ok(text.includes("2 / 2") && text.includes("Share in the workspace"));
assert.equal(buttons(pet.root, "Share result with Pebble").length, 0);
button(pet.root, "Share in the workspace").props.onClick();
await pet.s.share();
await settle();
assert.deepEqual(pet.emitted, [["workspace"]]);
assert.deepEqual(calls.map((c) => c.method), ["quiz_attempt"]);

// 3. A stale submission is refused by the host: the error shows, the draft stays, nothing is retried.
fakeHost({ revision: 2 });
calls.length = 0;
const stale = mount(reactive({ artifact: quiz() }));
await settle();
button(stale.root, "Start quiz").props.onClick();
await settle();
all(stale.root).filter((el) => el.props.role === "radio")[0].props.onClick();
await settle();
button(stale.root, "Next").props.onClick();
await settle();
button(stale.root, "Submit answers").props.onClick();
await settle();
text = textOf(stale.root);
assert.ok(text.includes("This view changed to revision 2."));
assert.ok(all(stale.root).some((el) => el.props.role === "alert" && textOf(el).includes("revision 2")));
assert.equal(stale.s.attempt.value.status, "open");
assert.equal(stale.s.answers.q1, 0, "the local draft is kept");
await settle();
assert.equal(calls.filter((c) => c.method === "quiz_submit").length, 1, "never auto-resubmitted");

// 4. An agent update mid-attempt closes the attempt without a grade and says so.
fakeHost();
const live = reactive({ artifact: quiz() });
const updated = mount(live);
await settle();
button(updated.root, "Start quiz").props.onClick();
await settle();
assert.equal(updated.s.attempt.value.status, "open");
calls.length = 0;
live.artifact = { ...quiz(), revision: 2 };
await settle();
assert.deepEqual(calls, [{ method: "quiz_attempt", params: { chat_id: "chat-a", id: "quiz-1", revision: 2 } }]);
text = textOf(updated.root);
assert.ok(text.includes("This quiz changed after revision 1, so your earlier attempt was closed without a grade."));
assert.ok(!text.includes("Question 1 of 2") && buttons(updated.root, "Start").length === 1);

// 5. Forged or mismatched host replies are refused or closed, never resumed as open.
const forged = [
  [{ attempt_id: "x", deadline: Date.now() / 1000 + 60, status: "open", artifact_id: "other-view" }, "another view"],
  [{ attempt_id: "x", deadline: Date.now() / 1000 + 60, status: "open", chat_id: "chat-b" }, "another conversation"],
  [{ attempt_id: "x", deadline: Date.now() / 1000 + 60, status: "open", answers: { q1: 7 } }, "invalid quiz attempt"],
  [{ attempt_id: "x", deadline: Date.now() / 1000 + 60, status: "approved" }, "invalid quiz attempt"],
  [{ attempt_id: "x", deadline: Date.now() / 1000 + 60, status: "submitted", result: { score: 1, total: 2, per_question: [{ id: "__proto__", correct: true }] } }, "invalid quiz attempt"],
  [{ attempt_id: "x", deadline: Date.now() / 1000 + 60, status: "submitted", result: { score: 3, total: 2, per_question: [] } }, "invalid quiz attempt"],
  [{ attempt_id: "<img>", deadline: 1 }, "invalid quiz attempt"],
];
for (const [value, expected] of forged) {
  reply = async () => structuredClone(value);
  const view = mount(reactive({ artifact: quiz() }));
  await settle();
  assert.equal(view.s.attempt.value, null, expected);
  assert.ok(textOf(view.root).includes(expected), expected + ": " + textOf(view.root));
  assert.ok(!textOf(view.root).includes("Question 1 of 2"));
}
reply = async () => ({ attempt_id: "old", deadline: Date.now() / 1000 + 60, status: "open", revision: 1 });
const moved = mount(reactive({ artifact: { ...quiz(), revision: 2 } }));
await settle();
assert.equal(moved.s.attempt.value.status, "superseded", "an open attempt bound to another revision is closed here");
assert.ok(textOf(moved.root).includes("This quiz changed while you were answering"));

// 6. Time is up: answering stops locally, Submit stays available and the host decides.
fakeHost({ deadline: -5 });
calls.length = 0;
const late = mount(reactive({ artifact: quiz() }));
await settle();
button(late.root, "Start quiz").props.onClick();
await settle();
assert.ok(textOf(late.root).includes("Time is up. Submit to see how you did.") && textOf(late.root).includes("00:00 left"));
assert.ok(all(late.root).filter((el) => el.props.role === "radio").every((el) => el.props.disabled === true));
late.s.choose("q1", 1);
await settle();
assert.ok(!calls.some((c) => c.method === "quiz_save"));
button(late.root, "Next").props.onClick();
await settle();
assert.notEqual(button(late.root, "Submit answers").props.disabled, true);

// 7. Historical, browser-preview and malformed views make no request and cannot start.
calls.length = 0;
const historical = mount(reactive({ artifact: quiz(), historical: true }));
await settle();
assert.ok(textOf(historical.root).includes("Historical revision. Return to the current revision to take this quiz."));
await historical.s.start();
assert.deepEqual(calls, []);
bridge.native = false;
const preview = mount(reactive({ artifact: quiz() }));
await settle();
assert.ok(textOf(preview.root).includes("Open the desktop app to take this quiz."));
assert.equal(button(preview.root, "Start quiz").props.disabled, true);
await preview.s.start();
assert.deepEqual(calls, []);
bridge.native = true;
const malformed = [
  { duration_minutes: 10, questions: [{ id: "q1", question: "Keyed?", choices: ["A", "B"], answer: 1 }] },
  { duration_minutes: 10, questions: [{ id: "q1", question: "Q", choices: ["A", "B"], explanation: "leak" }] },
  { duration_minutes: 10, questions: [{ id: "q1", question: "Q", choices: ["A", "B"], onclick: "approve()" }] },
  { duration_minutes: 10, questions: [{ id: "q1", question: "Q", choices: ["A", "B"] }, { id: "q1", question: "Q", choices: ["A", "B"] }] },
  { duration_minutes: 10, questions: [{ id: "__proto__", question: "Q", choices: ["A", "B"] }] },
  { duration_minutes: 10, questions: [{ id: "q1", question: "Q", choices: ["A"] }] },
  { duration_minutes: 0, questions: [{ id: "q1", question: "Q", choices: ["A", "B"] }] },
  { duration_minutes: 10, questions: [{ id: "q1", question: "Q", choices: ["A", { html: "<b>B</b>" }] }] },
  { duration_minutes: 10, questions: [], script: "x()" },
];
for (const data of malformed) {
  const view = mount(reactive({ artifact: { ...quiz(), data } }));
  await settle();
  assert.ok(all(view.root).some((el) => el.props.role === "alert" && textOf(el).includes("invalid display data")), JSON.stringify(data));
  assert.equal(buttons(view.root, "Start").length, 0);
}
assert.deepEqual(calls, [], "malformed views never reach the host");

// 8. Server rendering escapes every generated string.
bridge.native = false;
const html = await renderToString(createSSRApp(Quiz, { artifact: quiz() }));
assert.ok(html.includes("Science &lt;img src=x onerror=host()&gt;") && !/<(img|script|iframe|a|form)\b/.test(html));
for (const app of apps) app.unmount();
assert.deepEqual(vueErrors, []);
console.log("Quiz v2 fixtures passed: host attempt read/start/save/submit/share with exact params, coalesced drafts, consented main-window share only, pet points to the workspace, stale submission kept and not retried, superseded and forged replies closed, time-up, historical/preview/malformed views make no request, escaping.");
