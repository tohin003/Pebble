import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { transform } from "sucrase";

// Phase 46 (1.9.29, D195, w25-genui-b): visual answers' pure parts. A reply's free upgrades (tables, mermaid in a
// bounded subset, short numbered lists), Pebble's own number and date words, the chart geometry and the diagram
// layouts. The mermaid parser, the segmenter and the layouts are fuzzed: they never throw, stay within their bounds and
// finish in time; anything outside the subset stays as the reply wrote it.
const require = createRequire(import.meta.url);
const cache = new Map();
function loadTs(url) {
  if (cache.has(url.href)) return cache.get(url.href).exports;
  const module = { exports: {} };
  cache.set(url.href, module);
  new Function("require", "module", "exports", transform(readFileSync(url, "utf8"), { transforms: ["typescript", "imports"] }).code)(
    (name) => (name.startsWith(".") ? loadTs(new URL(name.endsWith(".ts") ? name : name + ".ts", url)) : require(name)), module, module.exports);
  return module.exports;
}
const V = loadTs(new URL("./visuals.ts", import.meta.url));
const C = loadTs(new URL("./chartModel.ts", import.meta.url));
const L = loadTs(new URL("./diagramLayout.ts", import.meta.url));
const fixtures = JSON.parse(readFileSync(new URL("../docs/ui-spec/fixtures.json", import.meta.url), "utf8"));

// a small seeded generator, so a failure names a seed that reproduces it
function random(seed) {
  let s = seed >>> 0 || 1;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
}

test("numbers are rounded and worded by Pebble; table cells parse as people write them", () => {
  assert.equal(V.formatNumber(1234.5678), "1,235");
  assert.equal(V.formatNumber(12.345), "12.3");
  assert.equal(V.formatNumber(0.123456), "0.123");
  assert.equal(V.formatNumber(0.976, "percent"), "97.6%");
  assert.equal(V.formatNumber(12500, "compact"), "12.5K");
  assert.equal(V.formatNumber(3.12, "number", "s"), "3.12 s");
  assert.equal(V.formatNumber(4.2, "number", "$"), "$4.2");
  assert.equal(V.formatNumber(-4.2, "number", "€"), "-€4.2");
  assert.equal(V.formatNumber(12, "number", "%"), "12%");
  assert.equal(V.formatNumber(Number.NaN), "—");
  const cells = { "1,200": 1200, "-3.5": -3.5, "12%": 12, "$4.20": 4.2, "1.2k": 1200, "3 ms": 3, "2M": 2e6, "−7": -7, "5m": 5 };
  for (const [text, value] of Object.entries(cells)) assert.equal(V.parseNumber(text), value, text);
  for (const text of ["", "abc", "1.2.3", "12-10-2026", "2026-10-30", "1,23", "v2", "≈5"]) assert.equal(V.parseNumber(text), null, text);
  assert.deepEqual(V.niceTicks(0, 87, 5), [0, 25, 50, 75, 100]);
  const ticks = V.niceTicks(-3, 9, 5);
  assert.ok(ticks[0] <= -3 && ticks.at(-1) >= 9 && ticks.includes(0), String(ticks));
  assert.ok(V.niceTicks(5, 5).length >= 2, "a flat series still gets an axis");
  assert.equal(V.formatDate("2026-10-31"), "31 October 2026");
  assert.equal(V.formatDate("2026-10"), "October 2026");
  assert.equal(V.formatDate("2026"), "2026");
  assert.equal(V.formatDate("2026-13"), "2026-13", "not a month: the text stays");
});

const TABLE = "Some text.\n\n| Fruit | Price | Sold |\n|:--|--:|--:|\n| Apple **red** | $1.20 | 1,200 |\n| Pear | $0.90 | 300 |\n| Fig | $3.10 | 45 |\n\nAfter.\n";
test("a reply's table, mermaid block and short numbered list become native parts; the rest stays Markdown", () => {
  const parts = V.replySegments(TABLE);
  assert.deepEqual(parts.map((p) => p.type), ["markdown", "table", "markdown"]);
  const table = parts[1].table;
  assert.deepEqual(table.header.map((c) => c.text), ["Fruit", "Price", "Sold"]);
  assert.equal(table.rows[0][0].text, "Apple red", "plain words for sorting");
  assert.equal(table.rows[0][0].markdown, "Apple **red**", "the cell's own Markdown for display");
  assert.deepEqual([...V.numericColumns(table).keys()], [1, 2]);
  assert.deepEqual(V.sortedRows(table, 2, "ascending"), [2, 1, 0], "numbers by value, not as text");
  assert.deepEqual(V.sortedRows(table, 0, "descending"), [1, 2, 0]);
  assert.deepEqual(V.tableChart(table), { label: 0, values: [1, 2] });
  // the Markdown around it is the reply's own text, token for token
  assert.equal(parts.map((p) => (p.type === "markdown" ? p.tokens.map((t) => t.raw).join("") : "")).join("").trim(), "Some text.\n\nAfter.");

  const mermaid = "Here:\n\n```mermaid\nflowchart LR\n  A[Draft] --> B{Review}\n  B -->|yes| C([Done])\n  B -- no --> A\n  %% a comment\n  style A fill:#f00\n```\n";
  const diagram = V.replySegments(mermaid).find((p) => p.type === "diagram");
  assert.ok(diagram, "a closed mermaid block in the subset");
  assert.equal(diagram.diagram.direction, "right");
  assert.deepEqual(diagram.diagram.nodes.map((n) => [n.id, n.label, n.shape]), [["A", "Draft", "box"], ["B", "Review", "decision"], ["C", "Done", "round"]]);
  assert.deepEqual(diagram.diagram.edges.map((e) => [e.from, e.to, e.label]), [["A", "B", ""], ["B", "C", "yes"], ["B", "A", "no"]]);
  assert.ok(diagram.source.includes("flowchart LR"), "the source stays one click away");

  for (const block of ["```mermaid\ngraph TD\nA-->B\n", "```mermaid\nsubgraph X\nA-->B\nend\n```", "```mermaid\ngraph TD\nA-->B\nclick A callback\n```",
    "```mermaid\ngantt\ntitle A\n```", "```mermaid\ngraph TD\nA --> B & C\n```", "```js\ngraph TD\nA-->B\n```"])
    assert.ok(!V.replySegments(block).some((p) => p.type === "diagram"), "stays a code block: " + JSON.stringify(block));

  assert.deepEqual(V.replySegments("1. Open Settings\n2. Choose **Models**\n3. Press `Add`\n").map((p) => p.type), ["steps"]);
  assert.deepEqual(V.replySegments("1. Open Settings\n2. Choose **Models**\n3. Press `Add`\n")[0].items, ["Open Settings", "Choose **Models**", "Press `Add`"]);
  for (const list of ["1. One\n2. Two\n", "- a\n- b\n- c\n", "3. c\n4. d\n5. e\n", "1. a\n\n2. b\n\n3. c\n", "1. a\n2. b\n   ```\n   x\n   ```\n3. c\n",
    "1. a\n   - nested\n2. b\n3. c\n", Array.from({ length: 13 }, (_, i) => `${i + 1}. step`).join("\n")])
    assert.ok(!V.replySegments(list).some((p) => p.type === "steps"), "stays a list: " + JSON.stringify(list));
  const plain = "Just words, a [link](https://example.com) and `code`.\n";
  assert.deepEqual(V.replySegments(plain).map((p) => p.type), ["markdown"], "a reply with nothing to upgrade is one Markdown part");
});

test("mermaid sequence diagrams in the subset; loops, boxes and bounds stay code", () => {
  const seq = V.parseMermaid("sequenceDiagram\n  autonumber\n  participant U as You\n  U->>P: Ask\n  P-->>U: Answer\n  Note over U,P: Both sides\n  P->>P: Think");
  assert.equal(seq.layout, "sequence");
  assert.deepEqual(seq.nodes.map((n) => n.label), ["You", "P"]);
  assert.deepEqual(seq.edges.map((e) => [e.from, e.to, e.label, !!e.dashed, !!e.note]),
    [["U", "P", "Ask", false, false], ["P", "U", "Answer", true, false], ["U", "P", "Both sides", false, true], ["P", "P", "Think", false, false]]);
  assert.equal(V.parseMermaid("sequenceDiagram\nA->>B: x\nloop Every minute\nB->>A: y\nend"), null);
  assert.equal(V.parseMermaid("sequenceDiagram\n" + Array.from({ length: 9 }, (_, i) => `P${i}->>P${i + 1}: m`).join("\n")), null, "more than 8 participants");
  assert.equal(V.parseMermaid("graph TD\n" + Array.from({ length: 41 }, (_, i) => `N${i}`).join("\n")), null, "more than 40 nodes");
  assert.equal(V.parseMermaid("graph TD\n" + Array.from({ length: 81 }, () => "A-->B").join("\n")), null, "more than 80 edges");
  assert.equal(V.parseMermaid("graph TD\nA-->B\n%%" + "x".repeat(6001)), null, "more than 6000 characters");
  assert.equal(V.parseMermaid("graph TD\nA-->"), null, "an arrow to nothing");
  assert.equal(V.parseMermaid('graph TD\nA["<img src=x onerror=alert(1)>"]').nodes[0].label, "<img src=x onerror=alert(1)>",
    "a label is only words: the renderer binds it as text");
});

test("fuzz: the mermaid parser and the segmenter never throw, stay within bounds and finish in time", () => {
  const pieces = ["graph TD", "flowchart LR", "sequenceDiagram", "A", "B1", "c_2", "-->", "---", "-.->", "==>", "-- yes -->", "|no|",
    "[x]", "(y)", "{z}", "([r])", "((o))", "{{h}}", ";", "&", "\n", "  ", "%% c", "subgraph", "end", "click", "style A fill:#f00",
    "participant", "as", "->>", "-->>", "-x", ":", "Note over A,B: n", "loop", '"q"', "<br/>", "]", ")", "}", "|", "‮", "\u0000"];
  let diagrams = 0;
  const started = performance.now();
  // half token soup, half well-formed diagrams with a few characters inserted, deleted or swapped
  const shapes = ["[a b]", "(c)", "{d?}", "([e])", '["f g"]', ""], arrows = ["-->", "---", "-.->", "==>", "-->|yes|", "-- no -->"];
  const wellFormed = (next) => {
    if (next() < 0.3) {
      const people = Array.from({ length: 1 + Math.floor(next() * 9) }, (_, i) => "P" + i);
      return "sequenceDiagram\n" + Array.from({ length: 1 + Math.floor(next() * 12) }, () => {
        const a = people[Math.floor(next() * people.length)], b = people[Math.floor(next() * people.length)];
        return next() < 0.2 ? `Note over ${a}: note` : `${a}${["->>", "-->>", "-x", "->"][Math.floor(next() * 4)]}${b}: hello`;
      }).join("\n");
    }
    const count = 1 + Math.floor(next() * 45);
    return (next() < 0.5 ? "graph TD\n" : "flowchart LR\n") + Array.from({ length: 1 + Math.floor(next() * 30) }, () =>
      `N${Math.floor(next() * count)}${shapes[Math.floor(next() * shapes.length)]} ${arrows[Math.floor(next() * arrows.length)]} N${Math.floor(next() * count)}`).join(next() < 0.8 ? "\n" : ";");
  };
  const mutate = (next, text) => {
    let out = text;
    for (let i = Math.floor(next() * 3); i > 0; i--) {
      const at = Math.floor(next() * (out.length + 1)), piece = pieces[Math.floor(next() * pieces.length)];
      out = next() < 0.5 ? out.slice(0, at) + piece + out.slice(at) : out.slice(0, at) + out.slice(at + 1 + Math.floor(next() * 4));
    }
    return out;
  };
  for (let seed = 1; seed <= 4000; seed++) {
    const next = random(seed);
    const words = Array.from({ length: 1 + Math.floor(next() * 40) }, () => pieces[Math.floor(next() * pieces.length)]);
    const source = seed % 2 ? mutate(next, wellFormed(next)) : (next() < 0.5 ? pieces[Math.floor(next() * 3)] + "\n" : "") + words.join(next() < 0.5 ? " " : "");
    let found;
    assert.doesNotThrow(() => (found = V.parseMermaid(source)), "seed " + seed);
    if (found) {
      diagrams++;
      const ids = new Set(found.nodes.map((n) => n.id));
      assert.ok(found.nodes.length >= 1 && found.nodes.length <= 40 && found.edges.length <= 80, "seed " + seed);
      assert.ok(found.nodes.every((n) => n.label.length <= 180) && found.edges.every((e) => ids.has(e.from) && ids.has(e.to) && e.label.length <= 100), "seed " + seed);
      assert.doesNotThrow(() => L.layout(found), "layout, seed " + seed);
    }
    const reply = "Intro\n\n```mermaid\n" + source + "\n```\n\n| a | b |\n|--|--|\n| " + words.slice(0, 3).join(" ") + " | 1 |\n\n1. x\n2. y\n3. " + words.join("");
    assert.doesNotThrow(() => V.replySegments(reply), "segments, seed " + seed);
  }
  const elapsed = performance.now() - started;
  console.log(`fuzzed 4000 sources: ${diagrams} parsed as diagrams, the rest stayed code; ${elapsed.toFixed(0)} ms`);
  assert.ok(diagrams > 500, "the fuzz reaches the parser's accepting paths: " + diagrams);
  assert.ok(elapsed < 20000, "bounded work");
});

// ---- the layouts ----
function overlaps(a, b) {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}
function checkLayout(placed, diagram, label) {
  for (const node of placed.nodes) {
    assert.ok([node.x, node.y, node.width, node.height].every(Number.isFinite), label);
    assert.ok(node.x >= 0 && node.y >= 0 && node.x + node.width <= placed.width + 0.5 && node.y + node.height <= placed.height + 0.5, label + ": inside the canvas");
  }
  for (let i = 0; i < placed.nodes.length; i++)
    for (let j = i + 1; j < placed.nodes.length; j++) assert.ok(!overlaps(placed.nodes[i], placed.nodes[j]), label + ": no two boxes overlap");
  if (diagram.layout !== "sequence") {
    const rows = new Map();
    for (const node of placed.nodes) {
      const key = diagram.layout === "flow" && diagram.direction === "right" ? Math.round(node.x) : Math.round(node.y);
      rows.set(key, (rows.get(key) || 0) + 1);
    }
    assert.ok(Math.max(...rows.values()) <= L.MAX_ROW, label + ": at most 4 boxes in a row");
  }
  assert.ok(placed.edges.every((e) => !/NaN|Infinity/.test(e.path)), label + ": every path is finite");
}

test("layouts: rows from the longest chain, back edges along the side, at most 4 boxes in a row, no overlaps", () => {
  const flow = { layout: "flow", direction: "down", nodes: ["a", "b", "c", "d", "e"].map((id) => ({ id, label: id.toUpperCase() })),
    edges: [{ from: "a", to: "b", label: "" }, { from: "b", to: "c", label: "" }, { from: "a", to: "c", label: "" }, { from: "c", to: "a", label: "again" }, { from: "d", to: "e", label: "" }] };
  const back = L.backEdges(flow.nodes.map((n) => n.id), flow.edges);
  assert.deepEqual([...back], [3], "c → a closes the cycle");
  const rows = L.layers(flow.nodes.map((n) => n.id), flow.edges, back);
  assert.deepEqual([...rows.entries()], [["a", 0], ["b", 1], ["c", 2], ["d", 0], ["e", 1]], "c sits below its longest chain a → b → c");
  const placed = L.layout(flow);
  checkLayout(placed, flow, "flow");
  assert.equal(placed.edges.filter((e) => e.back).length, 1);
  const wide = { layout: "flow", direction: "down", nodes: [{ id: "r", label: "Root" }, ...Array.from({ length: 9 }, (_, i) => ({ id: "n" + i, label: "Node " + i }))],
    edges: Array.from({ length: 9 }, (_, i) => ({ from: "r", to: "n" + i, label: "" })) };
  checkLayout(L.layout(wide), wide, "a fan of 9 wraps");
  checkLayout(L.layout({ ...wide, direction: "right" }), { ...wide, direction: "right" }, "a fan of 9, left to right");
  for (const [name, variant] of Object.entries(fixtures.native_variants).filter(([, v]) => v.kind === "diagram")) {
    const diagram = V.diagramOf(variant.data);
    assert.ok(diagram, name);
    checkLayout(L.layout(diagram), diagram, name);
  }
  const layers = L.layout(V.diagramOf(fixtures.native_variants.diagram_layers.data));
  assert.deepEqual(layers.bands.map((b) => b.label), ["Mac", "Pebble", "Provider"]);
  const sequence = L.layout(V.diagramOf(fixtures.native_variants.diagram_sequence.data));
  assert.equal(sequence.lifelines.length, 3);
  assert.deepEqual(sequence.messages.map((m) => m.label), ["Ask", "Request", "Reply", "Answer"]);
  assert.ok(sequence.messages.every((m, i, all) => !i || m.y > all[i - 1].y), "messages in order, top to bottom");
  assert.deepEqual(L.wrap("one two three four five six seven", 9, 2), ["one two", "three…"]);
});

test("fuzz: random diagrams of up to 40 boxes and 80 edges lay out without overlaps, within 4 per row, in time", () => {
  let slowest = 0;
  for (let seed = 1; seed <= 300; seed++) {
    const next = random(seed * 7919);
    const count = 1 + Math.floor(next() * 40);
    const nodes = Array.from({ length: count }, (_, i) => ({ id: "n" + i, label: "Box " + i + " ".repeat(Math.floor(next() * 3)) + "word".repeat(Math.floor(next() * 6)),
      ...(next() < 0.3 ? { sub: "a short note" } : {}), group: "g" + Math.floor(next() * 4) }));
    const edges = Array.from({ length: Math.floor(next() * 80) }, () => ({ from: "n" + Math.floor(next() * count), to: "n" + Math.floor(next() * count), label: next() < 0.3 ? "yes" : "" }));
    for (const layout of ["flow", "layers"]) {
      const diagram = { layout, direction: next() < 0.5 ? "down" : "right", nodes, edges };
      const started = performance.now();
      const placed = L.layout(diagram);
      slowest = Math.max(slowest, performance.now() - started);
      checkLayout(placed, diagram, `seed ${seed} ${layout}`);
    }
  }
  console.log(`slowest layout of up to 40 boxes and 80 edges: ${slowest.toFixed(2)} ms`);
  assert.ok(slowest < 250, "a diagram lays out within a frame budget on this Mac: " + slowest);
});

test("a model's diagram is checked again at the display boundary", () => {
  assert.ok(V.diagramOf(fixtures.native.diagram), "a Phase 18 diagram");
  for (const bad of [null, [], { nodes: [], edges: [] }, { nodes: [{ id: "a", label: "A" }], edges: [{ from: "a", to: "z", label: "" }] },
    { nodes: [{ id: "a", label: "A" }, { id: "a", label: "B" }], edges: [] }, { nodes: [{ id: "a", label: "A" }], edges: [], layout: "radial" },
    { nodes: [{ id: "a", label: "" }], edges: [] }, { nodes: Array.from({ length: 41 }, (_, i) => ({ id: "n" + i, label: "N" })), edges: [] }])
    assert.equal(V.diagramOf(bad), null, JSON.stringify(bad)?.slice(0, 80));
  assert.deepEqual(V.diagramText(V.diagramOf(fixtures.native.diagram)), ["Request → Auth middleware: checks session", "Auth middleware → App: passes on"]);
});

test("charts: every type's geometry is finite, axes cover the data from zero where bars stand, and parts sum to 100%", () => {
  const all = { chart: { kind: "chart", data: fixtures.native.chart }, ...fixtures.native_variants };
  for (const [name, { kind, data }] of Object.entries(all).filter(([, v]) => v.kind === "chart")) {
    const model = C.chartModel(data);
    assert.ok(!/NaN|Infinity/.test(JSON.stringify(model)), name);
    assert.ok(C.chartSummary(data, "Title").startsWith("Title: " + C.TYPE_NAMES[data.type]), name);
    if (model.kind === "pie") {
      const shares = model.slices.map((s) => Number(s.share.replace("%", "")));
      assert.ok(Math.abs(shares.reduce((a, b) => a + b, 0) - 100) < 0.5, name + " " + shares);
      continue;
    }
    const values = data.type === "stacked" ? data.rows.map((r) => data.y_keys.reduce((s, k) => s + r[k], 0)) : data.rows.flatMap((r) => data.y_keys.map((k) => r[k]));
    const ys = model.yTicks.map((t) => t.at);
    assert.ok(Math.max(...ys) <= model.plot.bottom + 0.5 && Math.min(...ys) >= model.plot.top - 0.5, name);
    for (const series of model.series) for (const point of series.points) assert.ok(point.y >= model.plot.top - 0.5 && point.y <= model.plot.bottom + 0.5, name);
    if (["bar", "stacked", "area"].includes(data.type)) assert.equal(model.baseline, model.plot.bottom, name + ": bars stand on zero");
    assert.ok(values.length);
  }
  const stacked = C.chartModel(fixtures.native_variants.chart_stacked.data);
  const [passed, failed] = stacked.series;
  assert.ok(Math.abs(failed.bars[0].y + failed.bars[0].height - passed.bars[0].y) < 0.2, "the second part sits on the first");
  const percent = C.chartModel(fixtures.native_variants.chart_donut.data);
  assert.deepEqual(percent.slices.map((s) => s.value), ["72%", "21%", "7%"], "percent reads fractions");
  const many = { type: "bar", x_key: "x", y_keys: ["y"], rows: Array.from({ length: 50 }, (_, i) => ({ x: "Label number " + i, y: i })) };
  const labels = C.chartModel(many).xTicks;
  assert.ok(labels.length <= 9 && labels.every((t) => t.label.length <= 12), "labels thinned and shortened");
  assert.equal(labels[0].full, "Label number 0", "the full label stays in its title");
});

test("measured: the free upgrades' cost on a long reply stays far below a frame", () => {
  const words = Array.from({ length: 2000 }, (_, i) => "word" + (i % 50)).join(" ");
  const long = words + "\n\n" + TABLE + "\n```mermaid\ngraph TD\n" + Array.from({ length: 30 }, (_, i) => `N${i}[Step ${i}] --> N${i + 1}`).join("\n") + "\n```\n";
  for (let i = 0; i < 5; i++) V.replySegments(long);
  const times = [];
  for (let i = 0; i < 21; i++) {
    const started = performance.now();
    const parts = V.replySegments(long);
    const diagram = parts.find((p) => p.type === "diagram");
    L.layout(diagram.diagram);
    times.push(performance.now() - started);
  }
  times.sort((a, b) => a - b);
  console.log(`a 2,000-word reply with a table and a 31-box diagram: median ${times[10].toFixed(2)} ms to segment and lay out`);
  assert.ok(times[10] < 30, "median " + times[10]);
});
