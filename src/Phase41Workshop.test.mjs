import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Phase 41 (1.7.0, Pebble's Workshop part 2, D69): a tool Pebble kept on its own is always shown in the conversation
// with View code and Remove; a keep Pebble asks about waits for the person's Keep or Discard; Settings lists the Toolbox.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const app = read("./App.vue");
const settings = read("./components/AdvancedSettings.vue");

test("the Toolbox card shows every keep: kept ones with Remove, asked ones with Keep and Discard", () => {
  assert.match(app, /if \(event\.event === "toolbox" && d\) toolboxEvent\(d\);/);
  for (const needle of ["Kept in Pebble's Toolbox", "Keep this tool in Pebble's Toolbox?", "Pebble asks because {{ why }}.",
    "toolboxRemove(card)", "toolboxDecide(card, false)", "toolboxDecide(card, true)", "View code"])
    assert.ok(app.includes(needle), needle);
});

test("each Toolbox write is one handler, run by the person's click", () => {
  assert.equal([...app.matchAll(/rpc\("toolbox_remove"/g)].length, 1);
  assert.equal([...app.matchAll(/rpc\("toolbox_decide"/g)].length, 1);
});

test("Settings lists kept tools with who kept them, View code and Remove", () => {
  for (const needle of ["PEBBLE'S TOOLBOX", "kept by {{ tool.kept_by === 'pebble' ? 'Pebble' : 'you' }}", "removeTool(tool.name)",
    "needs attention: it failed its re-check and is not used", "No kept tools yet."])
    assert.ok(settings.includes(needle), needle);
});
