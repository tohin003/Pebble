import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { transform } from "sucrase";

// The checked-in registry is compiled as TypeScript and evaluated alone.
const module = { exports: {} };
new Function("module", "exports", transform(readFileSync(new URL("./shortcuts.ts", import.meta.url), "utf8"), { transforms: ["typescript", "imports"] }).code)(module, module.exports);
const { SHORTCUTS, matchShortcut, shortcutsFor, keysLabel, moveRowFocus } = module.exports;

// One registry with the design study's exact entries (§4.5); nothing approves or answers.
assert.ok(Object.isFrozen(SHORTCUTS) && SHORTCUTS.every(Object.isFrozen));
// Phase 22 adds two main-window entries, "details" (⌘⇧D, the Details drawer) and "copy_reply" (⌘⇧C,
// design study §5.3); the earlier entries are unchanged.
assert.deepEqual(SHORTCUTS.map((s) => s.id), ["palette", "stop", "activity", "details", "copy_reply", "new_chat", "settings", "close", "send", "reveal"]);
assert.equal(new Set(SHORTCUTS.map((s) => s.id)).size, SHORTCUTS.length);
for (const s of SHORTCUTS) {
  assert.ok(["window", "composer", "global"].includes(s.scope), s.id);
  assert.equal(s.key, s.key.toLowerCase(), s.id);
  assert.ok(s.surfaces.length && s.surfaces.every((x) => ["main", "pet"].includes(x)), s.id);
  assert.ok(!/approve|answer|permission|setting change/i.test(s.label), s.id);
}
const bound = SHORTCUTS.filter((s) => s.scope === "window");
assert.equal(new Set(bound.map((s) => `${s.key}:${s.mod}:${s.shift}`)).size, bound.length, "no two window shortcuts share keys");

const ev = (key, extra = {}) => ({ key, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, ...extra });
const cases = [
  [ev("k", { metaKey: true }), "main", "palette"],
  [ev("k", { ctrlKey: true }), "pet", "palette"],
  [ev("K", { metaKey: true }), "main", "palette"],
  [ev(".", { metaKey: true }), "pet", "stop"],
  [ev("j", { metaKey: true }), "main", "activity"],
  [ev("j", { metaKey: true }), "pet", "activity"],
  [ev("d", { metaKey: true, shiftKey: true }), "main", "details"],
  [ev("D", { ctrlKey: true, shiftKey: true }), "main", "details"],
  [ev("d", { metaKey: true, shiftKey: true }), "pet", null],
  [ev("d", { metaKey: true }), "main", null],
  [ev("C", { metaKey: true, shiftKey: true }), "main", "copy_reply"],
  [ev("c", { metaKey: true, shiftKey: true }), "pet", null],
  [ev("c", { metaKey: true }), "main", null], // plain ⌘C stays the system's copy
  [ev("n", { metaKey: true }), "main", "new_chat"],
  [ev("n", { metaKey: true }), "pet", null],
  [ev(",", { metaKey: true }), "main", "settings"],
  [ev(",", { metaKey: true }), "pet", null],
  [ev("Escape"), "main", "close"],
  [ev("Escape"), "pet", "close"],
  [ev("k"), "main", null],
  [ev("k", { metaKey: true, shiftKey: true }), "main", null],
  [ev("k", { metaKey: true, altKey: true }), "main", null],
  [ev("k", { metaKey: true, repeat: true }), "main", null],
  [ev("k", { metaKey: true, isComposing: true }), "main", null],
  [ev("Enter"), "main", null], // the composer binds Enter itself
  [ev(" ", { metaKey: true, shiftKey: true }), "main", null], // registered natively, never in a webview
  [ev("a", { metaKey: true }), "main", null],
  [{ key: undefined }, "main", null],
];
for (const [event, surface, expected] of cases) assert.equal(matchShortcut(event, surface), expected, JSON.stringify([event, surface]));

assert.deepEqual(shortcutsFor("pet").map((s) => s.id), ["palette", "stop", "activity", "close", "send", "reveal"]);
assert.deepEqual(shortcutsFor("main").map((s) => s.id), SHORTCUTS.map((s) => s.id));
const byId = Object.fromEntries(SHORTCUTS.map((s) => [s.id, s]));
assert.deepEqual(["palette", "stop", "activity", "details", "copy_reply", "new_chat", "settings", "close", "send", "reveal"].map((id) => keysLabel(byId[id])), ["⌘K", "⌘.", "⌘J", "⌘⇧D", "⌘⇧C", "⌘N", "⌘,", "Esc", "Enter", "⌘⇧Space"]);
assert.equal(keysLabel(byId.details, "linux"), "Ctrl+Shift+D");
assert.equal(keysLabel(byId.palette, "linux"), "Ctrl+K");
assert.equal(keysLabel(byId.reveal, "windows"), "Ctrl+Shift+Space");

// The only global shortcut is the native reveal, matching the shell's one registration.
const shell = readFileSync(new URL("../src-tauri/src/main.rs", import.meta.url), "utf8");
const registered = [...shell.matchAll(/global_shortcut\(\)\.register\("([^"]+)"\)/g)].map((m) => m[1]);
assert.deepEqual(registered, ["CommandOrControl+Shift+Space"]);
assert.deepEqual(SHORTCUTS.filter((s) => s.scope === "global").map((s) => [s.id, s.key, s.mod, s.shift]), [["reveal", " ", true, true]]);

// Table rows: arrow keys move a roving tabindex only when a row itself has focus.
function table(count) {
  const body = { children: [] };
  for (let i = 0; i < count; i++) body.children.push({ tagName: "TR", parentElement: body, tabIndex: i ? -1 : 0, focused: 0, focus() { this.focused++; } });
  return body.children;
}
function key(target, name) {
  let prevented = false;
  const moved = moveRowFocus({ key: name, target, preventDefault: () => (prevented = true) });
  return { moved, prevented };
}
const rows = table(4);
assert.deepEqual(key(rows[0], "ArrowDown"), { moved: true, prevented: true });
assert.deepEqual(rows.map((r) => r.tabIndex), [-1, 0, -1, -1]);
assert.equal(rows[1].focused, 1);
assert.deepEqual(key(rows[1], "End"), { moved: true, prevented: true });
assert.equal(rows[3].tabIndex, 0);
assert.deepEqual(key(rows[3], "ArrowDown"), { moved: false, prevented: false }, "no wrap past the last row");
assert.deepEqual(key(rows[3], "Home"), { moved: true, prevented: true });
assert.deepEqual(key(rows[0], "ArrowUp"), { moved: false, prevented: false });
assert.deepEqual(key(rows[0], "Enter"), { moved: false, prevented: false });
const control = { tagName: "BUTTON", parentElement: rows[0] };
assert.deepEqual(key(control, "ArrowDown"), { moved: false, prevented: false }, "controls inside a row keep their keys");
assert.equal(moveRowFocus({ key: "ArrowDown", target: null, preventDefault() { throw new Error("unexpected"); } }), false);

console.log("Shortcut registry fixtures passed: exact entries, per-surface matching, modifier/repeat/IME refusal, display labels, one native global shortcut and roving row focus.");
