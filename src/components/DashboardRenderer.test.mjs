import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript } from "@vue/compiler-sfc";
import { createSSRApp, defineComponent, h, reactive } from "vue";
import { renderToString } from "@vue/server-renderer";
import { transform } from "sucrase";

// Compile only checked-in sources; panel data is never evaluated. The dashboard has
// no bridge: a request for "../bridge" fails the fixture.
const require = createRequire(import.meta.url);
function component(filename, inlineTemplate, imports = {}) {
  const source = readFileSync(new URL(filename, import.meta.url), "utf8");
  const script = compileScript(parse(source, { filename }).descriptor, { id: filename, inlineTemplate });
  const module = { exports: {} };
  new Function("require", "module", "exports", transform(script.content, { transforms: ["typescript", "imports"] }).code)(
    (name) => {
      if (name === "../bridge") throw new Error("The dashboard renderer must not reach the bridge.");
      if (Object.hasOwn(imports, name)) return imports[name];
      return require(name);
    },
    module,
    module.exports,
  );
  return { component: module.exports.default, source };
}
const { component: Native } = component("./NativeArtifactRenderer.vue", true);
const nested = { "./NativeArtifactRenderer.vue": { __esModule: true, default: Native } };
const { component: Dashboard, source } = component("./DashboardRenderer.vue", true, nested);
const { component: DashboardLogic } = component("./DashboardRenderer.vue", false, nested);

// Static boundary: no bridge, no markup injection, no data-bound URL, style or class.
const markup = source.slice(source.indexOf("<template>"), source.lastIndexOf("</template>"));
assert.ok(!/from "\.\.\/bridge"|\brpc\(|v-html|innerHTML|\beval\s*\(|new Function|<iframe|srcdoc|<form\b|<a\b/.test(source));
assert.ok(!/:(href|src|style|srcdoc|action|formaction)\b|v-bind="|v-on="|@\[|:is=/.test(markup));
assert.deepEqual([...markup.matchAll(/:class="([^"]+)"/g)].map((m) => m[1]), ["columns", "panel.kind === 'metric' ? 'metric' : 'wide'", "panel.metric.trend"]);
assert.deepEqual([...markup.matchAll(/@click="([^"]+)"/g)].map((m) => m[1]), ["refresh"]);

const hostile = "<img src=x onerror=host()>";
const base = { id: "dash-1", chat_id: "chat-a", title: "Week " + hostile, kind: "dashboard", revision: 3, status: "ready", owner_run_id: "run-1", created: 1 };
const panels = () => [
  { id: "runs", title: "Runs this week", kind: "metric", binding: { source: "runs.recent", params: { days: 7 } } },
  { id: "tokens", title: "Tokens", kind: "metric", data: { value: 12840, unit: "tokens", delta: -320, caption: "Since Monday " + hostile } },
  { id: "recent", title: "Recent " + hostile, kind: "table", data: { columns: [{ id: "name", label: "Name" }], rows: [{ name: hostile }] } },
  { id: "trend", title: "Trend", kind: "chart", binding: { source: "runs.usage" } },
  { id: "plan", title: "Plan", kind: "plan", data: { steps: [{ id: "a", title: "Read", status: "completed", evidence_ids: ["1:tool"] }] } },
  { id: "memory", title: "Memory", kind: "metric", binding: { source: "memory.stats" } },
];
const bindings = () => [
  { panel_id: "runs", source: "runs.recent", as_of: 1760000000, digest: "a1b2c3d4e5f6a7b8", data: { value: 14, delta: 3 } },
  { panel_id: "trend", source: "runs.usage", as_of: 1760000100, digest: "sha256:0123456789abcdef", data: { type: "line", x_key: "day", y_keys: ["runs"], rows: [{ day: "Mon", runs: 2 }, { day: "Tue", runs: 5 }] } },
  { panel_id: "memory", source: "memory.stats", error: "Memory is off for this conversation." },
];
const dashboard = (change = {}) => ({ ...base, data: { columns: 3, panels: panels() }, bindings: bindings(), ...change });
const render = (artifact, options = {}) => renderToString(createSSRApp(Dashboard, { artifact, ...options }));
const panelsOf = (html) => [...html.matchAll(/<article class="dash-panel[^"]*"[^>]*>([\s\S]*?)<\/article>/g)].map((m) => m[1]);

// Live, snapshot and failed panels side by side; tables, charts and plans go through the native renderer.
let html = await render(dashboard());
assert.ok(html.includes('class="dash-grid columns-3"'));
assert.ok(html.includes("6 panels") && html.includes("3 live") && html.includes("Refresh data"));
let cards = panelsOf(html);
assert.equal(cards.length, 6);
assert.ok(cards[0].includes("Runs this week") && cards[0].includes(">14<") && cards[0].includes("+3") && cards[0].includes("trend-up"));
assert.ok(/Recent runs · as of [^<]+ ·\s*<code>a1b2c3d4<\/code>/.test(cards[0]) && cards[0].includes(">Live<"));
assert.ok(cards[1].includes("12,840") && cards[1].includes("tokens") && cards[1].includes("-320") && cards[1].includes("trend-down") && cards[1].includes(">Snapshot<"));
assert.ok(!cards[1].includes("dash-stamp"), "inline data never carries a live stamp");
assert.ok(cards[2].includes("<table") && cards[2].includes("&lt;img src=x onerror=host()&gt;"));
assert.ok(cards[3].includes("<svg") && cards[3].includes("<code>01234567</code>") && cards[3].includes("Run usage"));
assert.ok(cards[4].includes("native-plan") && cards[4].includes("Read"));
assert.ok(cards[5].includes('role="alert"') && cards[5].includes("Live data unavailable: Memory is off for this conversation.") && !cards[5].includes("dash-stamp"));
assert.equal((html.match(/<h2/g) || []).length, 1, "panels show their own header, not a second title");
assert.ok(html.includes("Week &lt;img src=x onerror=host()&gt;") && !/<(img|script|iframe|a|form|input)\b/.test(html));

// A bad panel is refused on its own; the rest of the dashboard still renders.
const good = { id: "good", title: "Good", kind: "metric", data: { value: 1 } };
const refusedPanels = [
  [{ id: "p", title: "Answer", kind: "form", data: { fields: [{ id: "a", label: "A", type: "text", required: true }] } }, "Dashboards cannot hold forms."],
  [{ id: "p", title: "Both", kind: "metric", data: { value: 1 }, binding: { source: "runs.recent" } }, "either its own data or one host binding"],
  [{ id: "p", title: "Neither", kind: "metric" }, "either its own data or one host binding"],
  [{ id: "p", title: "Styled", kind: "metric", data: { value: 1 }, style: "color:red" }, "fields dashboards do not allow"],
  [{ id: "p", title: "Linked", kind: "metric", data: { value: 1 }, href: "https://example.invalid" }, "fields dashboards do not allow"],
  [{ id: "p", title: "Markup", kind: "metric", data: { value: 1 }, html: "<b>x</b>" }, "fields dashboards do not allow"],
  [{ id: "p", title: "Page", kind: "html", data: { html: "<p>x</p>" } }, "This panel type is not supported."],
  [{ id: "p", title: "Records", kind: "metric", binding: { source: "memory.records" } }, "not available to dashboards"],
  [{ id: "p", title: "Files", kind: "table", binding: { source: "files.read", params: { name: "x" } } }, "not available to dashboards"],
  [{ id: "p", title: "Path", kind: "metric", binding: { source: "../etc/passwd" } }, "binding is not valid"],
  [{ id: "p", title: "Other chat", kind: "metric", binding: { source: "runs.recent", params: { chat_id: "chat-b" } } }, "cannot name a chat"],
  [{ id: "p", title: "Folder", kind: "metric", binding: { source: "git.summary", params: { root: "/Users/me" } } }, "binding is not valid"],
  [{ id: "p", title: "Text", kind: "metric", data: { value: "7" } }, "Metric values must be finite numbers."],
  [{ id: "p", title: "Change", kind: "metric", data: { value: 7, delta: Infinity } }, "Metric changes must be finite numbers."],
  [{ id: "p", title: "Extra", kind: "metric", data: { value: 7, onclick: "x()" } }, "This metric is not valid."],
  [{ id: "p", title: "", kind: "metric", data: { value: 7 } }, "Every panel needs a short title."],
];
for (const [panel, expected] of refusedPanels) {
  html = await render({ ...base, data: { panels: [good, panel] }, bindings: [] });
  cards = panelsOf(html);
  assert.equal(cards.length, 2, expected);
  assert.ok(cards[0].includes(">1<") && !cards[0].includes('role="alert"'), expected);
  assert.ok(cards[1].includes('role="alert"') && cards[1].includes(expected), expected + ": " + cards[1]);
  assert.ok(!/<(form|input|table|svg)\b/.test(cards[1]), expected);
}
// The native renderer refuses malformed panel data inside its own card.
html = await render({ ...base, data: { panels: [good, { id: "c", title: "Bad chart", kind: "chart", data: { type: "line", x_key: "d", y_keys: ["v"], rows: [{ d: "x", v: Infinity }] } }] } });
cards = panelsOf(html);
assert.ok(cards[1].includes('role="alert"') && cards[1].includes("invalid display data") && !cards[1].includes("<svg"));
assert.ok(cards[0].includes(">1<") && !cards[0].includes('role="alert"'));
html = await render({ ...base, data: { panels: [good, { id: "c", title: "Text series", kind: "chart", data: { type: "bar", x_key: "d", y_keys: ["v"], rows: [{ d: "x", v: "7" }] } }] } });
assert.ok(panelsOf(html)[1].includes("Chart series must contain finite numbers.") && !panelsOf(html)[1].includes("<svg"));

// Host binding results are checked against the panel's own binding.
const bound = { id: "live", title: "Live", kind: "metric", binding: { source: "runs.recent" } };
const resultCases = [
  [[], "Waiting for host data. Use Refresh data to load it."],
  [[{ panel_id: "live", source: "jobs.fires", as_of: 1760000000, digest: "abcdef0123", data: { value: 2 } }], "Host data does not match this panel&#39;s binding."],
  [[{ panel_id: "live", source: "runs.recent", as_of: 1760000000, digest: "abcdef0123", data: { value: 2 } }, { panel_id: "live", source: "runs.recent", as_of: 1, digest: "abcdef0123", data: { value: 3 } }], "conflicting data"],
  [[{ panel_id: "live", source: "runs.recent", digest: "abcdef0123", data: { value: 2 } }], "invalid data for this panel"],
  [[{ panel_id: "live", source: "runs.recent", as_of: 1760000000, digest: "<b>", data: { value: 2 } }], "invalid data for this panel"],
  [[{ panel_id: "live", source: "runs.recent", as_of: 1760000000, digest: "abcdef0123", data: { value: 2 }, html: "x" }], "invalid data for this panel"],
  [[{ panel_id: "live", source: "runs.recent", error: "" }], "invalid data for this panel"],
];
for (const [results, expected] of resultCases) {
  html = await render({ ...base, data: { panels: [bound] }, bindings: results });
  assert.ok(panelsOf(html)[0].includes(expected), expected);
  assert.ok(!panelsOf(html)[0].includes("dash-stamp"), expected);
}

// Structural faults refuse the whole dashboard.
const refusedDashboards = [
  { panels: [] },
  { panels: Array.from({ length: 9 }, (_, i) => ({ ...good, id: "p" + i })) },
  { panels: [good, good] },
  { panels: [{ ...good, id: "../x" }] },
  { panels: [good], columns: 4 },
  { panels: [good], columns: "3; color:red" },
  { panels: [good], script: "approve()" },
  JSON.parse('{"panels":[{"id":"p","title":"P","kind":"metric","data":{"value":1,"__proto__":{"x":1}}}]}'),
  { panels: [{ ...good, data: { value: 1, note: "x".repeat(97000) } }] },
];
for (const data of refusedDashboards) {
  html = await render({ ...base, data, bindings: [] });
  assert.ok(html.includes('class="dash-error" role="alert"'), JSON.stringify(data).slice(0, 80));
  assert.ok(!html.includes("dash-grid") && !html.includes("Refresh data"));
}
html = await render({ ...dashboard(), bindings: "not a list" });
assert.ok(html.includes("The host returned invalid dashboard data."));

// Refresh is host chrome: it only emits, and only for a current, live dashboard with bindings.
async function state(props) {
  let setup;
  const emitted = [];
  const Wrapper = defineComponent({ setup(_, context) { setup = DashboardLogic.setup(props, { ...context, emit: (...args) => emitted.push(args) }); return () => h("div"); } });
  await renderToString(createSSRApp(Wrapper));
  return { setup, emitted };
}
const props = reactive({ artifact: dashboard(), historical: false, disabled: false, busy: false });
const live = await state(props);
live.setup.refresh();
assert.deepEqual(live.emitted, [["refresh"]]);
for (const change of [
  () => { props.historical = true; },
  () => { props.historical = false; props.disabled = true; },
  () => { props.disabled = false; props.busy = true; },
  () => { props.busy = false; props.artifact = { ...dashboard(), status: "stopped" }; },
  () => { props.artifact = { ...base, data: { panels: [good] } }; },
]) {
  change();
  assert.equal(live.setup.refreshDisabled.value, true);
  live.setup.refresh();
  assert.equal(live.emitted.length, 1);
}
html = await render({ ...base, data: { panels: [good] } });
assert.ok(!html.includes("Refresh data") && html.includes("1 panel<"));
html = await render(dashboard(), { historical: true });
assert.ok(html.includes("Historical revision. Live data is shown as it was saved.") && html.includes("disabled"));
html = await render(dashboard(), { disabled: true });
assert.ok(html.includes("Open the desktop app to refresh live data."));
html = await render(dashboard(), { error: "This view changed to revision 4." + hostile });
assert.ok(html.includes('role="alert">This view changed to revision 4.&lt;img'));
// Unknown column tokens never become a class.
html = await render({ ...base, data: { panels: [good] } });
assert.ok(html.includes('class="dash-grid columns-2"'));
console.log("Dashboard fixtures passed: live/snapshot/failed panels side by side, live stamps, native delegation without forms, per-panel refusals (forms, keys, sources, chats, paths, values), binding result checks, structural refusals, refresh emits only when allowed, no bridge, escaping.");
