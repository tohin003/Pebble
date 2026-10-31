// Phase 34 (1.2.0): the approval card names Pebble's own check and shows it, a held test-file edit carries the host's
// reason, the Auto mode copy names the hold, and the Keyboard caption cannot go stale (it was "Only ⌘⇧Space…" while
// the table listed ⌃⌥⌘S and ⌃⌥⌘H as Any app; reported from the landing page's build).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
const app = read("./App.vue");
const modes = read("./permissionModes.ts");

test("an ad hoc check has its own title, lead and visible text on the card", () => {
  assert.match(app, /function isCheck\(p: Record<string, any>\): boolean \{\n  return !!p\.check && typeof p\.check === "object" && typeof p\.check\.content === "string";/);
  assert.ok(app.includes('? (isCheck(action.payload || {}) ? "Run an ad hoc check" : "Run isolated tests")'));
  assert.ok(app.includes("Runs Pebble's own check, added to a temporary copy for this run only. Your project files don't change."));
  assert.ok(app.includes("<summary>View the check</summary>") && app.includes("{{ action.payload.check.content }}"));
  assert.ok(app.includes("It doesn't count as your\n                  project's tests."));
});

test("a held test-file edit leads with the host's reason, as text", () => {
  assert.ok(app.includes('return typeof p.scope_review === "string" ? `${p.scope_review} ${lead}` : lead;'));
});

test("the Auto mode copy names the hold it now makes", () => {
  // Phase 46 (1.9.28, D184; restated): what follows the hold changed (a confined command runs, permissionModes.test.mjs)
  assert.ok(modes.includes('"Changes to test files your request didn\'t ask for, '));
});

test("the Keyboard caption refers to the table's own column", () => {
  assert.ok(app.includes("Keys marked Any app work from other apps; the rest work in Pebble's windows."));
  assert.ok(!app.includes("Only ⌘⇧Space works from other apps"));
  assert.ok(app.includes("item.scope === 'global' ? 'Any app'"));
});
