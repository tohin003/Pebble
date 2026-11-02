import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// 2.0.3 (Phase 52, D242, F13): with another app in front, a click in Pebble's box while macOS's correction bubble was up
// (left by earlier typing) only closed the bubble, so the box never got the keyboard and the keys went to that app; a
// click on the bubble itself activated Pebble. While Stealth Lockdown is on, Pebble's five text boxes turn spelling,
// correction, capitalisation, autocomplete and writing suggestions off, so no bubble appears, and Stealth's switch
// dismisses one already up. Normal mode keeps WebKit's own. The behaviour is native (measured in the Phase 52 step 0);
// these checks hold the source to it.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const pet = read("./Pet.vue");
const app = read("./App.vue");
const shell = read("../src-tauri/src/main.rs");
const ATTRS =
  '{ spellcheck: false, autocorrect: "off", autocapitalize: "off", autocomplete: "off", writingsuggestions: "false" }';
const textareas = (text) =>
  [...text.matchAll(/<textarea\b/g)].map((m) => {
    const ends = [text.indexOf("/>", m.index), text.indexOf("</textarea>", m.index)].filter((i) => i >= 0);
    return text.slice(m.index, Math.min(...ends));
  });
const box = (text, marker) => {
  const found = textareas(text).filter((t) => t.includes(marker));
  assert.equal(found.length, 1, marker);
  return found[0];
};

test("the companion's boxes turn macOS's correction off only while Stealth Lockdown is on", () => {
  const computed = pet.match(/const stealthTyping = computed\(\(\) =>\s*(.+?)\s*\?\s*(\{[^}]*\})\s*:\s*(\{\s*\}),?\s*\);/s);
  assert.ok(computed, "stealthTyping");
  assert.equal(computed[1], "stealthOn.value", "keyed on Stealth Lockdown");
  assert.equal(computed[2], ATTRS, "all five off while on");
  assert.equal(computed[3], "{}", "nothing set in normal mode");
  for (const marker of ['aria-label="Message Pebble"', 'aria-label="Terminal command"'])
    assert.ok(box(pet, marker).includes('v-bind="stealthTyping"'), marker);
});

test("the workspace's boxes do the same", () => {
  const computed = app.match(/const stealthTyping = computed\(\(\) =>\s*(.+?)\s*\?\s*(\{[^}]*\})\s*:\s*(\{\s*\}),?\s*\);/s);
  assert.ok(computed, "stealthTyping");
  assert.equal(computed[1], "state.value?.settings?.stealth", "keyed on Stealth Lockdown");
  assert.equal(computed[2], ATTRS, "all five off while on");
  assert.equal(computed[3], "{}", "nothing set in normal mode");
  for (const marker of ['ref="composer"', 'aria-label="Edited message"', 'class="terminal-input"'])
    assert.ok(box(app, marker).includes('v-bind="stealthTyping"'), marker);
});

test("Stealth's switch dismisses a correction bubble already up, for each window's web view", () => {
  const start = shell.indexOf("fn apply_stealth(");
  const apply = shell.slice(start, shell.indexOf("\n}\n", start) + 2);
  const on = apply.slice(apply.indexOf('        if on {\n            for label in ["pet", "main"] {'));
  assert.ok(on.length > 0 && apply.includes('        if on {\n            for label in ["pet", "main"] {'), "on Stealth on");
  assert.match(on, /let view=webview\.inner\(\) as \*mut objc2::runtime::AnyObject;/);
  assert.match(on, /msg_send!\[objc2::class!\(NSSpellChecker\), sharedSpellChecker\]/);
  assert.match(on, /msg_send!\[checker, dismissCorrectionIndicatorForView: view\]/);
});
