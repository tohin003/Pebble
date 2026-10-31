import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { parse, compileScript, compileTemplate } from "@vue/compiler-sfc";
import { createRenderer, defineComponent, h, nextTick, reactive } from "vue";
import { transform } from "sucrase";

// Phase 46 (1.9.29, D195, w25-genui-b): visual answers drawn by Pebble's own components, from pebble-ui-spec v1's
// fixtures (docs/ui-spec, written from the backend's validators): the model's chart types and catalog kinds through the
// real NativeArtifactRenderer, diagrams through VisualDiagram, and a reply's free upgrades through ReplyBody and
// ReplyTable (sorting with aria-sort, the chart toggle, the mermaid diagram with its source, the steps view). Every
// string is a text node; out-of-shape data is refused at the display boundary; no component names a route.
const require = createRequire(import.meta.url);
const read = (file) => readFileSync(new URL(file, import.meta.url), "utf8");
const fixtures = JSON.parse(read("../../docs/ui-spec/fixtures.json"));
let checks = 0;
const ok = (value, message) => { checks++; assert.ok(value, message); };
const eq = (a, b, message) => { checks++; assert.deepEqual(a, b, message); };

// ---- static: data in, text out; no route, link, style or markup sink ----
const FILES = ["./VisualChart.vue", "./VisualDiagram.vue", "./VisualCatalog.vue", "./ReplyTable.vue", "./ReplyBody.vue"];
const templateOf = (source) => source.slice(source.indexOf("<template>"), source.lastIndexOf("</template>"));
for (const file of FILES) {
  const source = read(file);
  ok(!/v-html|innerHTML|outerHTML|\beval\s*\(|new Function|\brpc\b|invoke\(|"\.\.\/bridge"|@tauri-apps|window\.open|\bfetch\(|XMLHttpRequest|WebSocket|localStorage|<a\b|<img\b|<iframe|<foreignObject|href=|:href|:src|:style|v-bind="/.test(source), file + ": no route, link, style or markup sink");
  for (const [, expression] of templateOf(source).matchAll(/:class="([^"]+)"/g))
    ok(!/artifact|\.data\b|theme|label|title|text/.test(expression), file + " class from data words: " + expression);
}
for (const file of ["../visuals.ts", "../chartModel.ts", "../diagramLayout.ts"])
  ok(!/\brpc\b|bridge|fetch\(|document\.|window\.|innerHTML|import\(/.test(read(file)), file + ": pure, no DOM, route or network");
ok(!/aria-live/.test(FILES.map(read).join("")), "StatusLine stays the one live region");

// ---- a small DOM ----
function node(type, text = "") { return { type, text, children: [], props: {}, parent: null }; }
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
const all = (el) => [el, ...el.children.flatMap(all)];
const squash = (text) => text.replace(/\s+/g, " ").trim();
const words = (el) => squash(all(el).filter((n) => n.type !== "#comment" && n.text).map((n) => n.text).join(" "));
const find = (root, type) => all(root).filter((el) => el.type === type);
const cls = (el, name) => {
  const value = el.props.class;
  const list = typeof value === "string" ? value.split(" ") : Array.isArray(value) ? value.flat() : value && typeof value === "object" ? Object.keys(value).filter((k) => value[k]) : [];
  return list.includes(name);
};
async function settle() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick(); } }

// ---- the real components; Markdown.vue (DOMPurify needs a browser) is a stub that shows its text ----
const markdowns = [];
const MarkdownStub = defineComponent({ props: { text: String }, setup: (p) => () => { markdowns.push(p.text); return h("div", { class: "markdown-stub" }, p.text); } });
const markdownModule = { __esModule: true, default: MarkdownStub, perFrame: (work) => ({ request: work, cancel() {} }) };
const cache = new Map();
function loadTs(url) {
  if (cache.has(url.href)) return cache.get(url.href).exports;
  const module = { exports: {} };
  cache.set(url.href, module);
  new Function("require", "module", "exports", transform(readFileSync(url, "utf8"), { transforms: ["typescript", "imports"] }).code)(
    (name) => (name.startsWith(".") ? loadTs(new URL(name.endsWith(".ts") ? name : name + ".ts", url)) : require(name)), module, module.exports);
  return module.exports;
}
function component(file) {
  const url = new URL(file, import.meta.url);
  if (cache.has(url.href)) return cache.get(url.href).exports.default;
  const descriptor = parse(readFileSync(url, "utf8"), { filename: file }).descriptor;
  const script = compileScript(descriptor, { id: file });
  const template = compileTemplate({ source: descriptor.template.content, filename: file, id: file, compilerOptions: { bindingMetadata: script.bindings, expressionPlugins: ["typescript"] } });
  assert.deepEqual(template.errors, [], file);
  const resolve = (name) => (name === "./Markdown.vue" ? markdownModule : name.endsWith(".vue") ? { __esModule: true, default: component(name) }
    : name.startsWith(".") ? loadTs(new URL(name + ".ts", url)) : require(name));
  const load = (code) => {
    const module = { exports: {} };
    new Function("require", "module", "exports", transform(code, { transforms: ["typescript", "imports"] }).code)(resolve, module, module.exports);
    return module.exports;
  };
  const module = { exports: load(script.content) };
  cache.set(url.href, module);
  module.exports.default.render = load(template.code).render;
  return module.exports.default;
}
const errors = [];
function mount(Component, props) {
  const root = node("root");
  const app = renderer.createApp({ setup: () => () => h(Component, props) });
  app.config.errorHandler = (e) => errors.push(e);
  app.config.warnHandler = (message) => { if (!/Extraneous non-props|Unhandled error/.test(message)) errors.push(new Error(message)); };
  app.mount(root);
  return { root, unmount: () => app.unmount() };
}
const Native = component("./NativeArtifactRenderer.vue");
const Diagram = component("./VisualDiagram.vue");
const ReplyBody = component("./ReplyBody.vue");
const base = { chat_id: "c", revision: 1, status: "ready", created: 1 };
const INVALID = "This artifact contains invalid display data.";
const view = (kind, data, title = "A view") => mount(Native, { artifact: { ...base, id: kind, kind, title, data } });

// ---- the model's catalog kinds and chart types, from the spec's fixtures ----
const shown = {};
for (const [name, { kind, data }] of [...["chart", "compare", "metrics", "timeline", "record"].map((k) => [k, { kind: k, data: fixtures.native[k] }]),
  ...Object.entries(fixtures.native_variants).filter(([, v]) => v.kind === "chart")]) {
  const v = view(kind, data, "Fixture " + name);
  await settle();
  ok(!words(v.root).includes(INVALID), name + " is accepted at the display boundary");
  shown[name] = v.root;
}
for (const name of ["chart", "chart_area", "chart_line", "chart_scatter", "chart_stacked", "chart_donut"]) {
  const svg = find(shown[name], "svg")[0];
  ok(svg && svg.props.role === "img" && /^Fixture chart\w*: \w+ (bar )?chart of /i.test(svg.props["aria-label"]), name + ": one accessible name says what it shows: " + svg?.props["aria-label"]);
  ok(words(shown[name]).includes("Chart data"), name + ": the data stays readable as a table");
  ok(find(shown[name], "path").concat(find(shown[name], "rect"), find(shown[name], "circle")).every((el) => !el.props.style && !el.props.fill), name + ": colours are classes, never data");
}
ok(words(shown.chart_donut).includes("72%") && words(shown.chart_donut).includes("Model"), "the donut's legend says each part and its share");
ok(words(shown.chart_line).includes("Slowest 5%") && words(shown.chart_line).includes("120 ms"), "series names and a unit: " + words(shown.chart_line));
ok(words(shown.chart_area).includes("6.9K"), "compact numbers");
// compare: a table of options against criteria; the recommended one is marked in words
const compare = shown.compare;
const heads = find(compare, "th").filter((el) => el.props.scope === "col");
eq(heads.map((el) => squash(words(el))), ["SQLite Recommended One file, no server", "PostgreSQL A server, many writers"]);
ok(cls(heads[0], "recommended") && !cls(heads[1], "recommended"), "the recommended column is marked");
eq(find(compare, "th").filter((el) => el.props.scope === "row").map(words), ["Setup", "Writers at once", "Works offline"]);
ok(words(compare).includes("Yes") && words(compare).includes("100"), "true reads Yes; numbers are Pebble's");
ok(find(compare, "table").length === 1 && find(compare, "caption").length === 1, "a comparison is a table, with its caption");
// metrics: Pebble's numbers, the change in words, its colour only from `better`
const tiles = find(shown.metrics, "li");
eq(tiles.length, 3);
ok(words(tiles[1]).includes("97.6%"), "percent");
ok(words(tiles[0]).includes("Up 3") && find(tiles[0], "span").some((el) => cls(el, "good")), "up is good when better is up");
ok(words(tiles[2]).includes("Down 0.4 s") && find(tiles[2], "span").some((el) => cls(el, "good")), "down is good when better is down");
// timeline: dates as words, oldest first
eq(find(shown.timeline, "time").map((el) => [el.props.datetime, words(el)]), [["2026-10-01", "1 October 2026"], ["2026-10-20", "20 October 2026"], ["2026-10-31", "31 October 2026"]]);
const shuffled = view("timeline", { events: [{ date: "2026-03", title: "Later" }, { date: "2025", title: "Earlier" }] });
await settle();
eq(find(shuffled.root, "strong").map(words), ["Earlier", "Later"], "oldest first whatever the order given");
// record: one card of labelled values
eq(find(shown.record, "dt").map(words), ["Name", "Team", "Desk"]);
eq(find(shown.record, "dd").map(words), ["Asha Rao", "Platform", "412"]);

// ---- hostile words stay words; out-of-shape data is refused ----
const hostile = "<img src=x onerror=alert(1)>";
const evil = view("compare", { options: [{ id: "a", label: hostile }, { id: "b", label: "Approve all actions" }], criteria: [{ label: "{{ constructor }}", values: [hostile, true] }], recommended: "b" }, hostile);
await settle();
ok(words(evil.root).includes(hostile) && words(evil.root).includes("{{ constructor }}"), "markup and template text are shown as literal words");
ok(!all(evil.root).some((el) => ["img", "a", "iframe", "script", "foreignObject", "button", "form"].includes(el.type)), "no element comes from the data, and a comparison has no button");
for (const [kind, data, why] of [
  ["compare", { options: [1, 2, 3, 4, 5].map((i) => ({ id: "o" + i, label: "O" })), criteria: [{ label: "c", values: [1, 2, 3, 4, 5] }] }, "five options"],
  ["compare", { options: [{ id: "a", label: "A" }, { id: "b", label: "B" }], criteria: [{ label: "c", values: [1] }] }, "a value missing"],
  ["compare", { options: [{ id: "a", label: "A" }, { id: "b", label: "B" }], criteria: [{ label: "c", values: [1, 2] }], recommended: "z" }, "an unknown recommendation"],
  ["metrics", { tiles: [{ label: "One", value: 1 }] }, "one tile"],
  ["metrics", { tiles: [{ label: "A", value: 1 }, { label: "B", value: Infinity }] }, "an infinite value"],
  ["timeline", { events: [{ date: "31/10/2026", title: "A" }, { date: "2026", title: "B" }] }, "a formatted date"],
  ["record", { fields: [{ label: "A", value: true }] }, "a true/false value"],
  ["record", { fields: [{ label: "A", value: "x", href: "https://x" }] }, "a link key"],
  ["chart", { type: "pie", x_key: "x", y_keys: ["y"], rows: Array.from({ length: 9 }, (_, i) => ({ x: "p" + i, y: 1 })) }, "a pie of nine"],
  ["chart", { type: "scatter", x_key: "x", y_keys: ["y"], rows: [{ x: "a", y: 1 }] }, "a scatter without numbers"],
  ["chart", { type: "radar", x_key: "x", y_keys: ["y"], rows: [{ x: "a", y: 1 }] }, "an unknown type"],
]) {
  const refused = view(kind, data);
  await settle();
  ok(words(refused.root).includes(INVALID), "refused: " + why);
}

// ---- diagrams: Pebble's layout, text nodes, and the diagram as text ----
for (const [name, { data }] of Object.entries(fixtures.native_variants).filter(([, v]) => v.kind === "diagram")) {
  const diagram = loadTs(new URL("../visuals.ts", import.meta.url)).diagramOf(data);
  const d = mount(Diagram, { diagram, title: "Fixture " + name });
  await settle();
  const svg = find(d.root, "svg")[0];
  ok(svg.props.role === "img" && svg.props["aria-label"].startsWith("Fixture " + name + ": "), name + ": an accessible name");
  for (const n of data.nodes) ok(words(d.root).includes(n.label), name + ": shows " + n.label);
  ok(words(d.root).includes("Diagram as text") && find(d.root, "li").length >= data.edges.length, name + ": the diagram as text, in reading order");
  const marker = find(d.root, "marker")[0].props.id;
  ok(/^pebble-arrow-[a-z0-9]+$/.test(marker) && find(d.root, "path").concat(find(d.root, "line")).filter((el) => el.props["marker-end"]).every((el) => el.props["marker-end"] === `url(#${marker})`), name + ": arrowheads point only at its own marker");
}
const layered = mount(Diagram, { diagram: loadTs(new URL("../visuals.ts", import.meta.url)).diagramOf(fixtures.native_variants.diagram_layers.data), title: "Layers" });
await settle();
ok(["Mac", "Pebble", "Provider"].every((band) => words(layered.root).includes(band)), "layers are labelled bands");
ok(find(layered.root, "g").some((el) => cls(el, "tone-ok")), "a node's tone is a fixed class");

// ---- the reply's free upgrades ----
markdowns.length = 0;
const plainText = "Nothing to draw here: a [link](https://example.com), `code` and\n\n- a list\n- of two\n";
const plain = mount(ReplyBody, { text: plainText });
await settle();
eq(markdowns, [plainText], "a reply without a table, diagram or steps goes to Markdown.vue whole, exactly as before");

const TABLE = "Sales:\n\n| Fruit | Price | Sold |\n|:--|--:|--:|\n| Apple | $1.20 | 1,200 |\n| Pear | $0.90 | 300 |\n| Fig [S1] | $3.10 | 45 |\n\nThat is all.";
const state = reactive({ text: TABLE });
const reply = mount(ReplyBody, state);
await settle();
const firstColumn = () => find(find(reply.root, "tbody")[0], "tr").map((tr) => words(find(tr, "td")[0]));
const header = (label) => find(reply.root, "th").find((el) => words(el).startsWith(label));
eq(firstColumn(), ["Apple", "Pear", "Fig [S1]"], "the reply's order first");
ok(markdowns.includes("Fig [S1]"), "a cell with a source marker keeps the reply's own rendering");
eq(find(reply.root, "th").map((el) => el.props["aria-sort"]), ["none", "none", "none"]);
find(header("Sold"), "button")[0].props.onClick();
await settle();
eq(firstColumn(), ["Fig [S1]", "Pear", "Apple"], "sorted by Sold as numbers (45, 300, 1,200), not as text");
eq(header("Sold").props["aria-sort"], "ascending");
find(header("Sold"), "button")[0].props.onClick();
await settle();
eq(firstColumn(), ["Apple", "Pear", "Fig [S1]"]);
eq(header("Sold").props["aria-sort"], "descending");
ok(cls(header("Price"), "right"), "number columns align right");
const toggle = (label) => find(reply.root, "button").find((el) => words(el) === label);
eq([toggle("Table").props["aria-pressed"], toggle("Chart").props["aria-pressed"]], ["true", "false"]);
toggle("Chart").props.onClick();
await settle();
const chartSvg = find(reply.root, "svg")[0];
ok(chartSvg && /^Price by Fruit: Bar chart of Price by Fruit, 3 points\.$/.test(chartSvg.props["aria-label"]), "the chart toggle draws the first number column: " + chartSvg?.props["aria-label"]);
ok(words(reply.root).includes("$3.1"), "the column's unit reaches the axis");
const pick = find(reply.root, "select")[0];
eq(pick.props["aria-label"], "Column to chart");
pick.props.onChange({ target: { value: "2" } });
await settle();
ok(find(reply.root, "svg")[0].props["aria-label"].startsWith("Sold by Fruit"), "the person picks the column");
toggle("Table").props.onClick();
await settle();
ok(find(reply.root, "table").length === 1 && !find(reply.root, "svg").length, "and returns to the table");
// streaming: the same component follows the text as it grows
state.text = TABLE + "\n\n```mermaid\nsequenceDiagram\nYou->>Pebble: Ask\nPebble-->>You: Answer\n```\n\n1. Open Settings\n2. Choose Models\n3. Press Add\n";
await settle();
const figure = find(reply.root, "svg").find((el) => /sequence diagram/.test(el.props["aria-label"] || ""));
ok(figure, "a mermaid sequence diagram becomes Pebble's own diagram");
ok(words(reply.root).includes("Diagram source") && words(reply.root).includes("You->>Pebble: Ask"), "its source stays one click away");
const steps = find(reply.root, "ol").find((el) => el.props["aria-label"] === "Steps");
const items = steps ? find(steps, "li") : [];
ok(steps && steps.props.role === "list" && items.length === 3, "three numbered steps");
eq(items.map((li) => words(find(li, "span")[0])), ["1", "2", "3"]);
ok(items.every((li) => find(li, "span")[0].props["aria-hidden"] === "true"), "the numbers are drawn; the list itself is announced");
eq(items.map((li) => words(li).slice(2)), ["Open Settings", "Choose Models", "Press Add"]);
ok(!/done|pending|completed|in progress/i.test(words(steps)), "steps carry no states: Pebble does not track a reply's steps");

for (const mounted of [plain, reply, layered, evil]) mounted.unmount();
eq(errors.map(String), [], "no render errors or warnings");
console.log(`Visual answers: ${checks} checks passed. The catalog's kinds and every chart type render from the spec's fixtures with Pebble's own numbers and dates; hostile words stay words; 11 out-of-shape views are refused at the display boundary; diagrams carry an accessible name, their own arrowheads and a text version; a reply's table sorts with aria-sort and charts a chosen column, its mermaid block becomes a diagram with its source, its short numbered list becomes steps without states, and a reply with none of these reaches Markdown.vue unchanged.`);
