import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript } from "@vue/compiler-sfc";
import { createSSRApp, defineComponent, h, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Standalone component fixtures, following scripts/validate_avatar.mjs.
// Only the checked-in Vue source is compiled; artifact data is never evaluated.
const require = createRequire(import.meta.url);
const requests = [];
let backendArtifact;
const bridge = {
  native: false,
  async rpc(method, params) {
    requests.push({ method, params });
    if (method === "artifact_get") return backendArtifact;
    if (method === "artifact_history") return [backendArtifact];
    if (method === "artifact_view_open")
      return {
        url: "http://127.0.0.1:18422/view/fixture",
        expires_at: Date.now() / 1000 + 60,
      };
    if (method === "view_stop") return true;
    if (method === "artifact_submit") return { status: "answered" };
    if (method === "artifact_refresh") return { revision: 2 };
    if (method === "quiz_attempt") return null;
    throw new Error("Unexpected RPC: " + method);
  },
};
const renderers = {};
function component(filename, inlineTemplate) {
  const source = readFileSync(new URL(filename, import.meta.url), "utf8");
  const descriptor = parse(source, { filename }).descriptor;
  const script = compileScript(descriptor, { id: filename, inlineTemplate });
  const module = { exports: {} };
  new Function(
    "require",
    "module",
    "exports",
    transform(script.content, { transforms: ["typescript", "imports"] }).code,
  )(
    (name) => {
      if (name === "../bridge") return bridge;
      if (name === "./NativeArtifactRenderer.vue")
        return { __esModule: true, default: NativeRenderer };
      // Phase 19 renderers routed by ArtifactPanel; DashboardRenderer nests this renderer.
      if (/^\.\/(Quiz|Dashboard|Prototype)Renderer\.vue$/.test(name))
        return { __esModule: true, default: (renderers[name] ||= component(name, true)) };
      return require(name);
    },
    module,
    module.exports,
  );
  return module.exports.default;
}
const NativeRenderer = component("./NativeArtifactRenderer.vue", true);
const NativeLogic = component("./NativeArtifactRenderer.vue", false);
const Panel = component("./ArtifactPanel.vue", true);
const PanelLogic = component("./ArtifactPanel.vue", false);
const base = {
  id: "artifact",
  chat_id: "chat",
  title: "Fixture",
  revision: 1,
  status: "ready",
  owner_run_id: "run-1",
  created: 1,
  evidence_ids: ["1:tool"],
};
const form = {
  ...base,
  kind: "form",
  form_question: {
    id: "question",
    revision: 1,
    status: "pending",
    answer: null,
  },
  data: {
    description: "Choose the task scope.",
    fields: [
      {
        id: "scope",
        label: "Scope",
        type: "choice",
        required: true,
        options: [
          { id: "project", label: "This project" },
          { id: "folder", label: "This folder" },
        ],
      },
      { id: "details", label: "Details", type: "text", required: true },
    ],
  },
};
const chart = {
  ...base,
  kind: "chart",
  data: {
    type: "bar",
    x_key: "month",
    y_keys: ["net"],
    rows: [
      { month: "Oct", net: -5 },
      { month: "Nov", net: 7 },
    ],
    labels: { net: "Net change" },
  },
};
const table = {
  ...base,
  kind: "table",
  data: {
    columns: [{ id: "name", label: "Name" }],
    rows: [{ name: "<img src=x onerror=alert(1)>" }],
  },
};
const plan = {
  ...base,
  kind: "plan",
  data: {
    steps: [
      {
        id: "done",
        title: "Verified",
        status: "completed",
        evidence_ids: ["1:tool"],
      },
      { id: "next", title: "Continue", status: "in_progress" },
    ],
  },
};
const evidence = {
  ...base,
  kind: "evidence",
  data: {
    items: [
      {
        id: "receipt",
        label: "Tool result",
        evidence_id: "1:tool",
        summary: "Resolved tool receipt",
      },
    ],
  },
};

async function render(artifact, options = {}) {
  return renderToString(createSSRApp(NativeRenderer, { artifact, ...options }));
}
async function state(Component, props) {
  let setup;
  const emitted = [];
  const Wrapper = defineComponent({
    setup(_, context) {
      setup = Component.setup(props, {
        ...context,
        emit: (...args) => emitted.push(args),
      });
      return () => h("div");
    },
  });
  await renderToString(createSSRApp(Wrapper));
  return { setup, emitted };
}

for (const artifact of [form, table, chart, plan, evidence])
  assert.ok(
    !(await render(artifact)).includes("invalid display data"),
    artifact.kind,
  );
const escaped = await render(table);
assert.ok(escaped.includes("&lt;img src=x onerror=alert(1)&gt;"));
assert.ok(!escaped.includes("<img"));
const escapedTitle = await render({
  ...evidence,
  title: "<script>host()</script>",
});
assert.ok(escapedTitle.includes("&lt;script&gt;host()&lt;/script&gt;"));
assert.ok(!escapedTitle.includes("<script>"));

const malformed = [
  { ...form, data: { fields: Array(4).fill(form.data.fields[1]) } },
  { ...form, data: { fields: [{ ...form.data.fields[1], required: false }] } },
  { ...form, data: { fields: [{ ...form.data.fields[1], type: "password" }] } },
  {
    ...form,
    data: {
      fields: [
        {
          ...form.data.fields[0],
          options: [
            { id: "same", label: "A" },
            { id: "same", label: "B" },
          ],
        },
      ],
    },
  },
  {
    ...table,
    data: { ...table.data, rows: [{ name: { html: "<p>bad</p>" } }] },
  },
  {
    ...table,
    data: { ...table.data, rows: Array(201).fill({ name: "large" }) },
  },
  {
    ...table,
    data: JSON.parse(
      '{"columns":[{"id":"name","label":"Name"}],"rows":[{"name":"text","__proto__":"bad"}]}',
    ),
  },
  {
    ...chart,
    data: { ...chart.data, rows: [{ month: "Oct", net: Infinity }] },
  },
  { ...chart, data: { ...chart.data, rows: [{ month: "Oct", net: "7" }] } },
  { ...chart, data: { ...chart.data, type: "script" } },
  {
    ...plan,
    data: { steps: [{ id: "done", title: "Unverified", status: "completed" }] },
  },
  {
    ...evidence,
    data: {
      items: [{ id: "receipt", label: "Missing receipt", evidence_id: null }],
    },
  },
];
for (const artifact of malformed) {
  const html = await render(artifact);
  assert.ok(html.includes('role="alert"'), JSON.stringify(artifact.data));
  assert.ok(
    !html.includes("<svg") &&
      !html.includes("<form") &&
      !html.includes("<table"),
  );
}
const chartState = (await state(NativeLogic, reactive({ artifact: chart })))
  .setup;
assert.ok(
  chartState.chartGeometry.value.baseline > 18 &&
    chartState.chartGeometry.value.baseline < 250,
);
assert.ok(
  chartState.chartGeometry.value.series[0].bars.every(
    (bar) => Number.isFinite(bar.y) && bar.height > 0,
  ),
);
assert.ok(!(await render(chart)).includes("NaN"));

const props = reactive({
  artifact: structuredClone(form),
  historical: false,
  disabled: false,
  busy: false,
});
const pending = await state(NativeLogic, props);
pending.setup.values.scope = "project";
pending.setup.values.details = "  Keep it local  ";
pending.setup.submit();
assert.deepEqual(pending.emitted, [
  [
    "submit",
    { scope: { option_id: "project" }, details: { text: "Keep it local" } },
  ],
]);
for (const change of [
  () => {
    props.historical = true;
  },
  () => {
    props.historical = false;
    props.artifact.status = "stopped";
  },
  () => {
    props.artifact.status = "expired";
  },
  () => {
    props.artifact.status = "ready";
    props.artifact.form_question.status = "answered";
  },
  () => {
    props.artifact.form_question.status = "cancelled";
  },
  () => {
    props.artifact.form_question.status = "pending";
    props.busy = true;
  },
  () => {
    props.busy = false;
    props.disabled = true;
  },
]) {
  change();
  assert.equal(pending.setup.formDisabled.value, true);
  pending.setup.submit();
  assert.equal(pending.emitted.length, 1);
}
const unknownChoice = await state(NativeLogic, reactive({ artifact: form }));
unknownChoice.setup.values.scope = "__host_permission";
unknownChoice.setup.values.details = "ordinary task input";
unknownChoice.setup.submit();
assert.equal(unknownChoice.emitted.length, 0);
assert.equal(
  unknownChoice.setup.inputError.value,
  "Complete every required field.",
);
const answered = {
  ...form,
  form_question: {
    ...form.form_question,
    status: "answered",
    answer: {
      scope: { option_id: "folder", text: "This folder" },
      details: { text: "Stored answer" },
    },
  },
};
const answeredHtml = await render(answered);
assert.ok(
  answeredHtml.includes("Stored answer") &&
    answeredHtml.includes("Form: answered") &&
    answeredHtml.includes("disabled"),
);

// Timers are unrelated to these isolated panel fixtures and cannot outlive them.
const interval = globalThis.setInterval;
globalThis.setInterval = () => 0;
try {
  const scripted = {
    ...base,
    kind: "html",
    status: "review",
    data: { html: "<h1>Saved preview</h1>", script: "host.permission()" },
  };
  const html = await renderToString(
    createSSRApp(Panel, { artifacts: [scripted] }),
  );
  assert.ok(html.includes("Generated scripts cannot run here"));
  assert.ok(
    html.includes("Owner run: run-1") && html.includes("Current · revision 1"),
  );
  assert.ok(!html.includes("<iframe"));
  bridge.native = true;
  backendArtifact = {
    ...scripted,
    data: { html: "<h1>Saved preview</h1>", css: "h1{color:red}", script: "" },
  };
  const preview = (
    await state(PanelLogic, reactive({ artifacts: [backendArtifact] }))
  ).setup;
  await preview.loadArtifact();
  requests.length = 0;
  await preview.runCustom();
  assert.deepEqual(
    requests.filter((request) => request.method === "artifact_view_open"),
    [
      {
        method: "artifact_view_open",
        params: { chat_id: "chat", id: "artifact", revision: 1 },
      },
    ],
  );
  assert.ok(!requests.some((request) => request.method === "view_stage"));
  assert.equal(preview.customUrl.value, "http://127.0.0.1:18422/view/fixture");
  await preview.stopCustom();
  backendArtifact = scripted;
  const blocked = (await state(PanelLogic, reactive({ artifacts: [scripted] })))
    .setup;
  await blocked.loadArtifact();
  requests.length = 0;
  await blocked.runCustom();
  assert.equal(requests.length, 0);
  assert.ok(blocked.customError.value.includes("reviewed Mods"));
} finally {
  globalThis.setInterval = interval;
}

// Phase 19: a dashboard panel hides the heading and never shows or submits a form.
const panelTable = await render(table, { panel: true });
assert.ok(panelTable.includes("<table") && !panelTable.includes("<h2"));
const panelForm = await render(form, { panel: true });
assert.ok(panelForm.includes("Dashboard panels cannot hold forms.") && !/<(form|input|fieldset)\b/.test(panelForm));
const panelFormState = await state(NativeLogic, reactive({ artifact: structuredClone(form), panel: true }));
panelFormState.setup.values.scope = "project";
panelFormState.setup.values.details = "ordinary task input";
panelFormState.setup.submit();
assert.deepEqual(panelFormState.emitted, []);
// The new kinds have their own renderers; this one refuses them.
for (const kind of ["dashboard", "prototype", "quiz", "html"]) {
  const html = await render({ ...base, kind, data: { panels: [] } });
  assert.ok(html.includes("Unsupported native artifact type."), kind);
}

// ArtifactPanel routes v1 quizzes to the labelled self-check, v2 to the host-graded renderer,
// and dashboards and prototypes to theirs; anything ambiguous is refused.
globalThis.setInterval = () => 0;
try {
  bridge.native = false;
  const v1 = { ...base, kind: "quiz", data: { duration_minutes: 5, questions: [{ question: "2 + 2?", choices: ["3", "4"], answer: 1, explanation: "Arithmetic." }] } };
  const v2 = { ...base, kind: "quiz", data: { duration_minutes: 5, questions: [{ id: "q1", question: "2 + 2?", choices: ["3", "4"] }] } };
  let html = await renderToString(createSSRApp(Panel, { artifacts: [v1] }));
  assert.ok(html.includes("Self-check (answers are stored with this view)") && html.includes("Start test") && !html.includes("checked by Pebble"));
  html = await renderToString(createSSRApp(Panel, { artifacts: [v2] }));
  assert.ok(html.includes("Quiz · checked by Pebble") && !html.includes("Self-check") && !html.includes("Start test"));
  for (const data of [
    { ...v2.data, questions: [{ id: "q1", question: "2 + 2?", choices: ["3", "4"], answer: 1 }] },
    { ...v1.data, questions: [{ ...v1.data.questions[0], answer: 5 }] },
    { ...v1.data, questions: [v1.data.questions[0], v2.data.questions[0]] },
    { duration_minutes: 5, questions: [] },
  ]) {
    html = await renderToString(createSSRApp(Panel, { artifacts: [{ ...v1, data }] }));
    assert.ok(html.includes("This quiz contains invalid display data.") && !html.includes("Start test") && !html.includes("checked by Pebble"), JSON.stringify(data));
  }
  const dashboardArtifact = { ...base, kind: "dashboard", data: { panels: [{ id: "m", title: "Metric", kind: "metric", data: { value: 3 } }] } };
  html = await renderToString(createSSRApp(Panel, { artifacts: [dashboardArtifact] }));
  assert.ok(html.includes("dash-grid") && html.includes(">3<") && !html.includes("native-error"));
  const prototypeArtifact = { ...base, kind: "prototype", data: { start: "home", screens: [{ id: "home", title: "Home", components: [{ type: "heading", text: "Hi" }] }] } };
  html = await renderToString(createSSRApp(Panel, { artifacts: [prototypeArtifact] }));
  assert.ok(html.includes("Interactive mock-up · controls only navigate this demo") && html.includes("Send feedback"));
  // The embedded (pet) panel is the "pet" surface unless told otherwise.
  assert.equal((await state(PanelLogic, reactive({ artifacts: [v2] }))).setup.panelSurface.value, "main");
  assert.equal((await state(PanelLogic, reactive({ artifacts: [v2], embedded: true }))).setup.panelSurface.value, "pet");
  assert.equal((await state(PanelLogic, reactive({ artifacts: [v2], embedded: true, surface: "main" }))).setup.panelSurface.value, "main");
  // Refresh data calls artifact_refresh for the revision shown, then rereads; never on a historical revision.
  bridge.native = true;
  backendArtifact = { ...dashboardArtifact, revision: 2, data: { panels: [{ id: "r", title: "Runs", kind: "metric", binding: { source: "runs.recent" } }] }, bindings: [] };
  const live = (await state(PanelLogic, reactive({ artifacts: [backendArtifact] }))).setup;
  await live.loadArtifact();
  requests.length = 0;
  await live.refreshDashboard();
  assert.deepEqual(requests.map((request) => request.method), ["artifact_refresh", "artifact_get", "artifact_history"]);
  assert.deepEqual(requests[0].params, { chat_id: "chat", id: "artifact", revision: 2 });
  live.revisionChoice.value = "1";
  await live.loadArtifact();
  requests.length = 0;
  await live.refreshDashboard();
  assert.deepEqual(requests, []);
  backendArtifact = { ...backendArtifact, status: "stopped" };
  const stopped = (await state(PanelLogic, reactive({ artifacts: [backendArtifact] }))).setup;
  await stopped.loadArtifact();
  requests.length = 0;
  await stopped.refreshDashboard();
  assert.deepEqual(requests, []);
  bridge.native = false;
} finally {
  globalThis.setInterval = interval;
}
console.log(
  "Native artifact fixtures passed: five renderers, text escaping, malformed data, finite charts, native form payloads/read-only states, static preview RPC boundary, dashboard panel mode without forms, and ArtifactPanel routing (v1 self-check, v2 quiz, dashboard refresh, prototype).",
);
