import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Phase 39 (1.5.2): the companion over full-screen apps and Stealth Lockdown's windows, each fix first measured on a
// real Mac (macOS 27) with native probes and the shell's diagnostic (D78, D81-D86). The behaviour is native; these
// checks hold the shell's source to what the measurements showed.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const shell = read("../src-tauri/src/main.rs");
const fn = (name) => {
  const start = shell.indexOf("fn " + name + "(");
  assert.ok(start >= 0, name);
  return shell.slice(start, shell.indexOf("\n}\n", start) + 2);
};
const after = (text, first, then) => {
  assert.ok(text.includes(first), first);
  assert.ok(text.indexOf(then, text.indexOf(first) + first.length) > 0, `${then} after ${first}`);
};

test("the companion's Space behaviour is re-applied after the panel switch and every policy change (D78)", () => {
  after(fn("companion_over_full_screen"), "setCollectionBehavior: 0usize", "setCollectionBehavior: behaviour");
  after(fn("pet_now"), "ActivationPolicy::Accessory", "companion_over_full_screen(app)");
  after(fn("reveal"), "ActivationPolicy::Regular", "if !stealth_on() { companion_over_full_screen(app); }");
  after(fn("configure_pet"), "ActivationPolicy::Accessory", "companion_over_full_screen(app)");
});

test("a companion a Space change left out is ordered back in, in companion mode only (D84)", () => {
  const rejoin = fn("companion_rejoin");
  assert.match(rejoin, /if policy != 1 \{ return; \}/);
  after(rejoin, "orderFrontRegardless", "companion_over_full_screen(app)");
  assert.doesNotMatch(rejoin, /makeKeyAndOrderFront/);
  assert.match(fn("companion_follows_spaces"), /NSWorkspaceActiveSpaceDidChangeNotification/);
});

test("Stealth leaves the Dock only after the full-screen exit has ended, in one animation (D83)", () => {
  const apply = fn("apply_stealth");
  assert.match(apply, /deferred \|= leaving;/);
  assert.match(apply, /if !deferred \{/);
  const windowed = fn("windowed_after_full_screen");
  assert.match(windowed, /exit_observer::watch\(app, ended_tx\);/);
  after(windowed, "exit_observer::stop();", "set_activation_policy(tauri::ActivationPolicy::Accessory)");
  assert.match(windowed, /if oversized \{/);
  after(fn("open_workspace"), "windowed_frame(&main)", "main.set_fullscreen(true)");
});

test("in Stealth the workspace opens on the desktop; without it, full screen waits for the slide (D82, D85)", () => {
  const opened = fn("open_workspace");
  assert.match(opened, /if stealth_on\(\) && space_anchor::elsewhere\(\) && !space_anchor::GOING\.swap\(true/);
  assert.match(opened, /if !from_elsewhere \|\| on_active_space\(&main\) \{ enter\(&main\); \}/);
  const anchor = shell.slice(shell.indexOf("mod space_anchor {"));
  for (const needle of ["width: 1.0, height: 1.0", "setIgnoresMouseEvents: Bool::YES", "setSharingType: 0usize"])
    assert.ok(anchor.includes(needle), needle);
});

test("the hide keys bring back what they hid; the menu item only hides; the diagnostic is off unless named", () => {
  const panic = fn("stealth_panic");
  assert.match(panic, /if hidden\.contains\(&"main"\) \{ reveal\(app\); \}/);
  assert.match(shell, /"hide"=>hide_now\(app\)/);
  assert.doesNotMatch(fn("hide_now"), /reveal\(|\.show\(\)/);
  assert.match(shell, /std::env::var_os\("PEBBLE_SHELL_DIAG"\)\?/);
  assert.match(shell, /meta\.mode\(\) & 0o022 != 0/);
});

test("no surface promises that Pebble never takes focus (D86)", () => {
  for (const [name, text] of [["README.md", read("../README.md")], ["App.vue", read("./App.vue")], ["stealthVeil.ts", read("./stealthVeil.ts")]])
    for (const promise of ["never takes focus", "never takes keyboard focus", "won't take focus"])
      assert.ok(!text.includes(promise), `${name}: ${promise}`);
  assert.ok(read("./App.vue").includes("Clicking or typing in the companion leaves the app you were in active"));  // Phase 45 (F151)
});
