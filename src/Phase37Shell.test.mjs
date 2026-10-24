import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Phase 37 (1.5.0): the person's report on 1.4.0. Turning Stealth Lockdown on while the workspace was in full screen
// left it screen-sized, the Dock showing and its red, yellow and green buttons gone: it became a non-activating panel
// while macOS was still animating it out of its full-screen Space. Now capture protection, the Dock and ⌘Tab still
// change at once, the workspace's panel switch waits for the exit to end, the workspace becomes an ordinary window
// (its configured size within 80 % of the screen, centred), and turning stealth off returns it to full screen. The
// behaviour is native and needs a person at a real Mac; these checks hold the shell's source to it.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const shell = read("../src-tauri/src/main.rs");
const fn = (name) => {
  const start = shell.indexOf("fn " + name + "(");
  assert.ok(start >= 0, name);
  return shell.slice(start, shell.indexOf("\n}\n", start) + 2);
};

test("a full-screen workspace waits for the exit before it becomes a panel; protection applies at once", () => {
  const apply = fn("apply_stealth");
  assert.match(apply, /leaving=on && appkit_fullscreen\(window\.ns_window\(\)\)/, "measured before leaving full screen");
  assert.ok(apply.indexOf("leaving=on") < apply.indexOf("set_fullscreen(false)"), "measured first");
  assert.match(apply, /if leaving \{ LEFT_FULL_SCREEN\.store\(true, Ordering::SeqCst\); windowed_after_full_screen\(app\); \}/);
  assert.match(apply, /else \{ let _=stealth_mac::set_panel\(ns, on\); \}/, "every other window switches at once");
  assert.ok(apply.indexOf("set_content_protected(on)") < apply.indexOf("windowed_after_full_screen(app)"),
    "capture protection never waits");
  assert.match(apply, /stealth_mac::set_sharing_all\(on\)/);
});

test("after the exit the workspace is an ordinary centred window, only while stealth is still on", () => {
  const windowed = fn("windowed_after_full_screen");
  assert.match(windowed, /main_fullscreen\(&probe\)/, "waits for AppKit's full-screen flag to clear, as to_pet does");
  assert.match(windowed, /if !stealth_on\(\) \{ return; \}/, "turned off meanwhile: nothing changes");
  assert.match(windowed, /width\.min\(screen\.width\*0\.8\)/);
  assert.match(windowed, /height\.min\(screen\.height\*0\.8\)/);
  assert.ok(windowed.indexOf("set_size(") < windowed.indexOf("center()"), "sized, then centred");
  assert.ok(windowed.indexOf("center()") < windowed.indexOf("set_panel("), "a panel only once it is windowed");
  assert.match(shell, /const WINDOWED_SIZE: \(f64, f64\) = \(1160\.0, 800\.0\);/);
  const conf = JSON.parse(read("../src-tauri/tauri.conf.json"));
  const main = conf.app.windows.find((w) => w.label === "main");
  assert.deepEqual([main.width, main.height], [1160, 800], "the configured workspace size");
});

test("turning stealth off returns the workspace to full screen once Pebble is in the Dock again", () => {
  const apply = fn("apply_stealth");
  assert.match(apply, /!on && LEFT_FULL_SCREEN\.swap\(false, Ordering::SeqCst\) && window\.is_visible\(\)/);
  const policy = apply.indexOf("set_activation_policy(");
  const back = apply.indexOf("RETURN_TO_FULL_SCREEN.swap(false, Ordering::SeqCst)");
  assert.ok(policy >= 0 && back > policy, "full screen after the Regular activation policy");
  assert.match(fn("open_workspace"), /if !stealth_on\(\) && !main\.is_fullscreen\(\)/, "still opens windowed under stealth");
});
