import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { transform } from "sucrase";

// Phase 46 (1.9.29, D206): the work view's parser, cards and words against pebble-ui-spec v1. The schema and the
// fixtures are the files scripts/generate_ui_spec.py writes from the backend (docs/ui-spec), so the window's field
// table (SPEC) and its summary are held equal to the host's: a field added on one side and not the other fails here.
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
const W = loadTs(new URL("./workView.ts", import.meta.url));
const source = readFileSync(new URL("./workView.ts", import.meta.url), "utf8");
const json = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
const schema = json("../docs/ui-spec/pebble-ui-spec-v1.schema.json");
const fixtures = json("../docs/ui-spec/fixtures.json");
const ordered = fixtures.summary_items.map((name) => fixtures.items[name]);
const cp = (...codes) => String.fromCharCode(...codes);

test("the module is pure: no route, no markup sink, no invisible characters", () => {
  assert.ok(!/innerHTML|v-html|eval\(|new Function|(?<![.\w])fetch\(|import\(|\brpc\b|invoke\(/.test(source));
  const invisible = new RegExp("[" + [[0x2028, 0x2029], [0x202a, 0x202e], [0x2066, 0x2069], [0x200b, 0x200f]].map(([a, b]) => cp(a) + "-" + cp(b)).join("") + "]");
  assert.ok(!invisible.test(source));
});

test("the window's field table is the spec's, type by type and field by field", () => {
  assert.deepEqual(Object.keys(W.SPEC), schema["x-pebble-view-types"]);
  const same = (field, def, where) => {
    switch (field[0]) {
      case "text": return assert.equal(def.maxLength, field[1], where);
      case "int": return assert.equal(def.type, "integer", where);
      case "num": return assert.equal(def.type, "number", where);
      case "bool": return assert.equal(def.type, "boolean", where);
      case "enum": return assert.deepEqual(def.enum, [...field[1]], where);
      case "list":
        assert.equal(def.maxItems, field[1], where);
        return same(field[2], def.items, where + "[]");
      case "object":
        assert.deepEqual(Object.keys(def.properties).sort(), Object.keys(field[1]).sort(), where);
        for (const key of Object.keys(field[1])) same(field[1][key], def.properties[key], where + "." + key);
    }
  };
  for (const type of Object.keys(W.SPEC)) {
    const def = schema.$defs["view." + type];
    const fields = Object.keys(def.properties).filter((k) => !["v", "type", "more"].includes(k));
    assert.deepEqual(fields.sort(), Object.keys(W.SPEC[type]).sort(), type);
    for (const key of fields) same(W.SPEC[type][key], def.properties[key], type + "." + key);
  }
});

test("every host view in the fixtures parses to itself; an unknown type falls back to its row", () => {
  for (const [name, item] of Object.entries(fixtures.items)) {
    if (!item.view) continue;
    if (item.view.type === "hologram") assert.equal(W.parseView(item.view), null, "unknown type: the row shows");
    else assert.deepEqual(W.parseView(item.view), item.view, name);
  }
});

test("a hostile or malformed view keeps only checked fields, as text", () => {
  const hostile = W.parseView({ v: 1, type: "read", path: "<img src=x onerror=alert(1)>" + cp(0x202e) + "evil\nname", start: -1, total: 1.5, href: "https://x.test", __proto__: { polluted: true } });
  assert.deepEqual(hostile, { v: 1, type: "read", path: "<img src=x onerror=alert(1)> evil name" });
  for (const bad of [null, [], "read", { v: 2, type: "read" }, { v: 1, type: "__proto__" }, { v: 1, type: "constructor" }, { v: 1 }])
    assert.equal(W.parseView(bad), null, JSON.stringify(bad));
  const long = W.parseView({ v: 1, type: "command", command: "x".repeat(900), tail: Array.from({ length: 30 }, () => "y".repeat(300)), state: "approved" });
  assert.equal(long.command.length, 400);
  assert.equal(long.tail.length, 8);
  assert.ok(long.tail.every((line) => line.length === 160));
  assert.equal(long.state, undefined, "an unknown state is dropped, never shown as done");
});

test("the window's summary is the host's, from the same items", () => {
  assert.deepEqual(W.summaryOf(ordered, { input: 52310, cached: 28770, output: 3120 }, 95.2), fixtures.summary);
  assert.deepEqual(W.summaryWords(fixtures.summary), ["1 file changed +4 −2", "2 commands · 1 failed", "Tests 41 passed · 1 failed", "A check asked for more evidence", "1 min 35 s", "52K in · 3.1K out"]);
});

test("items become cards by kind, and reviews, questions, plumbing and unknown types stay rows", () => {
  const entries = W.workEntries(ordered);
  const shape = entries.map((e) => (e.type === "card" ? e.kind + ":" + e.items.length : e.type));
  assert.deepEqual(shape, ["explored:3", "web:1", "files:2", "command:1", "command:1", "tests:1", "git:1", "git:1", "window:2", "agents:1", "checks:1", "checks:1", "row", "row", "row", "row"]);
  const titles = entries.filter((e) => e.type === "card").map(W.cardTitle);
  assert.deepEqual(titles.slice(0, 6), ["Read 2 files · 1 search", "Searched the web", "Changed 2 files", "npm run build", "python3 scripts/deploy.py --token=***", "Tests · python-pytest"]);
  assert.equal(W.testWords(entries[5].views[0]), "41 passed · 1 failed");
  assert.equal(W.commandWords(entries[3].views[0]), "exit 0 · 5 s");
  assert.deepEqual(W.diffWords(entries[2].views), { added: 6, removed: 2 });
});

test("the header's parts: plan, task line, run words", () => {
  const plan = W.parsePlan(fixtures.header.plan);
  assert.deepEqual([plan.done, plan.total, plan.steps[2]], [2, 5, { title: "Fix the refresh", status: "in_progress" }]);
  assert.equal(W.parsePlan({ steps: [{ title: "x", status: "approved" }] }), null);
  assert.equal(W.parsePlan(null), null);
  assert.equal(W.taskLine("\n  Fix the session refresh\nin the middleware"), "Fix the session refresh");
  assert.equal(W.taskLine("x".repeat(300)).length, 120);
  assert.equal(W.runWord("waiting_approval"), "Waiting for you");
  assert.equal(W.runWord("anything else"), "Working");
  assert.deepEqual([980, 1234, 52310, 1_340_000].map(W.round), ["980", "1.2K", "52K", "1.3M"]);
});

test("the companion's strip names the running card and the plan's progress", () => {
  const record = { state: "running", items: ordered.slice(0, 7), host: true, local: [] };
  const strip = W.stripOf(record, W.parsePlan(fixtures.header.plan));
  assert.deepEqual(strip, { words: "npm run build", done: 2, total: 5 }, "nothing runs: the newest card");
  const live = { ...record, items: [...ordered.slice(0, 3), { ...ordered[3], status: "running" }] };
  assert.equal(W.stripOf(live, null).words, "Searched the web");
  assert.equal(W.stripOf({ state: "running", items: [] }, null), null);
});

test("the branch card's lanes follow parents, and history is bounded and checked", () => {
  const c = (id, ...parents) => ({ commit: id.repeat(7), parents: parents.map((p) => p.repeat(7)), author: "you", subject: id, at: null });
  // a merge at the tip: main's line and the feature's line join
  const rows = W.lanes([c("a", "b", "c"), c("b", "d"), c("c", "d"), c("d")]);
  assert.deepEqual(rows.map((r) => [r.lane, r.merges]), [[0, [1]], [0, []], [1, []], [0, []]]);
  assert.ok(rows.every((r) => r.lanes <= 4));
  const wide = W.lanes(Array.from({ length: 8 }, (_, i) => c(String(i), ...Array.from({ length: 4 }, (_, j) => String.fromCharCode(97 + i * 4 + j)))));
  assert.ok(wide.every((r) => r.lane < 4 && r.merges.every((m) => m < 4)), "never more than four lanes");
  const history = W.parseHistory({ commits: [{ commit: "a".repeat(40), parents: ["b".repeat(40), "not a hash"], author: "Lena" + cp(0x202e), subject: "s".repeat(200), authored_at: 1 }, { commit: "zzz" }, null] });
  assert.equal(history.length, 1);
  assert.deepEqual(history[0].parents, ["b".repeat(40)]);
  assert.equal(history[0].subject.length, 80);
  assert.equal(history[0].author, "Lena");
  assert.equal(W.age(Date.now() / 1000 - 7200, Date.now()), "2 h");
});
