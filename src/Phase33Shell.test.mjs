import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Phase 33 (1.1.0, D45-D47, D57): Stealth Lockdown in the shell. One switch turns on both halves:
//   Stealth: every Pebble window is left out of screen capture and screen sharing, the workspace drops its shadow,
//   Pebble stays out of the Dock and ⌘Tab and its windows leave Mission Control; ⌃⌥⌘H hides Pebble at once.
//   Lockdown: Pebble never takes focus from the app in front (non-activating panels; no set_focus).
// The behaviour itself is native and needs a person at a real Mac (the checklist in the Phase 33 review); these checks
// hold the shell's source to it, as Phase31Shell.test.mjs does for the full workspace.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const shell = read("../src-tauri/src/main.rs");
const fn = (name) => {
  const start = shell.indexOf("fn " + name + "(");
  assert.ok(start >= 0, name);
  return shell.slice(start, shell.indexOf("\n}\n", start) + 2);
};

test("one switch applies both halves to every window and lifts them all again", () => {
  const apply = fn("apply_stealth");
  for (const needle of ['for label in ["pet", "main"]', "set_content_protected(on)", "set_shadow(!on)",
    "stealth_mac::set_mission_control(label, ns, on)", "stealth_mac::set_panel(ns, on)", "stealth_mac::set_sharing_all(on)",
    "tray_state(app, on)"])
    assert.ok(apply.includes(needle), needle);
  assert.match(apply, /if on \|\| !workspace_open \{tauri::ActivationPolicy::Accessory\}/, "out of the Dock and ⌘Tab while on");
  assert.match(apply, /if on && window\.is_fullscreen\(\)\.unwrap_or\(false\) \{ let _=window\.set_fullscreen\(false\); \}/,
    "a full-screen workspace leaves its own Space");
});

test("every window the app owns is left out of capture, AppKit's hidden ones too", () => {
  const all = fn("set_sharing_all");
  assert.match(all, /msg_send!\[app, windows\]/);
  assert.match(all, /setSharingType: if on \{ 0usize \} else \{ 1usize \}/, "None while on, ReadOnly after");
});

test("Mission Control: transient while on, the window's own behaviour restored after", () => {
  const mission = fn("set_mission_control");
  assert.match(mission, /setCollectionBehavior: \(current & !MANAGED\) \| TRANSIENT/);
  assert.match(mission, /saved\.remove\(label\)/);
  assert.match(shell, /const TRANSIENT: usize = 1 << 3;/);
  assert.match(shell, /const MANAGED: usize = 1 << 2;/);
});

test("Lockdown: a non-activating panel carrying tao's own methods, swapped only when the layout allows", () => {
  const made = fn("panel_class");
  assert.ok(made.includes('AnyClass::get(c"NSPanel")') && made.includes('AnyClass::get(c"TaoWindow")'));
  assert.match(made, /class_getInstanceSize\(panel\) != ffi::class_getInstanceSize\(window\)/, "same instance size as NSWindow");
  for (const sel of ["canBecomeMainWindow", "canBecomeKeyWindow", "sendEvent:"]) assert.ok(made.includes(`sel!(${sel})`), sel);
  assert.match(made, /offset\(cls\) != offset\(tao\)/, "the focusable ivar where tao reads it");
  const panel = fn("set_panel");
  assert.ok(panel.indexOf("object_setClass(ns, panel)") < panel.indexOf("mask | NONACTIVATING_PANEL"), "class first, then the mask");
  assert.ok(panel.indexOf("mask & !NONACTIVATING_PANEL") < panel.indexOf("object_setClass(ns, tao)"), "mask cleared, then tao's class");
  assert.match(shell, /const NONACTIVATING_PANEL: usize = 1 << 7;/);
});

test("nothing activates Pebble while Stealth Lockdown is on", () => {
  assert.match(fn("reveal"), /if !stealth_on\(\) \{ let _=app\.set_activation_policy\(tauri::ActivationPolicy::Regular\); \}/);
  assert.match(fn("reveal"), /focus\(&window\);/);
  assert.doesNotMatch(fn("reveal"), /set_focus/);
  const focus = fn("focus");
  assert.ok(focus.indexOf("if stealth_on()") < focus.indexOf("set_focus()"), "set_focus only when off");
  assert.match(focus, /front_without_activating/);
  assert.match(fn("pet_expand"), /if expanded \{ focus\(&window\); \}/);
  assert.match(fn("front_without_activating"), /makeKeyAndOrderFront:/);
  // set_focus remains in exactly one place: focus(), when the switch is off.
  assert.equal([...shell.matchAll(/set_focus\(\)/g)].length, 1);
  // Phase 45 (D146): full screen also only when the person left the workspace full screen
  assert.match(fn("open_workspace"), /if full && !stealth_on\(\) && !main\.is_fullscreen\(\)/, "windowed under stealth");
});

test("the keys, the menu-bar icon and its menu", () => {
  for (const key of ["CommandOrControl+Shift+Space", "Control+Alt+Command+S", "Control+Alt+Command+H"])
    assert.ok(shell.includes(`global_shortcut().register("${key}")`), key);
  assert.match(shell, /shortcut\.matches\(stealth_keys, Code::KeyS\) \{ stealth_toggle\(app\); \}/);
  assert.match(shell, /shortcut\.matches\(stealth_keys, Code::KeyH\) \{ stealth_panic\(app\); \}/);
  assert.match(shell, /TrayIconBuilder::with_id\("pebble"\)\.icon\(glyph\)\.icon_as_template\(true\)/);
  assert.match(shell, /CheckMenuItem::with_id\(app,"stealth","Stealth Lockdown",true,false,Some\("Control\+Alt\+Command\+S"\)\)/);
  assert.match(shell, /MenuItem::with_id\(app,"hide","Hide Pebble now",true,Some\("Control\+Alt\+Command\+H"\)\)/);
  assert.match(fn("tray_state"), /include_bytes!\("\.\.\/icons\/tray-on\.png"\)/);
  assert.match(fn("tray_state"), /set_icon_with_as_template\(Some\(image\), true\)/);
  assert.match(fn("stealth_panic"), /for label in \["pet", "main"\] \{ if let Some\(w\)=app\.get_webview_window\(label\) \{ let _=w\.hide\(\); \} \}/);
  // The toggle keys write the setting first, so every window and the next launch agree.
  const toggle = fn("stealth_toggle");
  assert.ok(toggle.indexOf('backend.call("setting"') < toggle.indexOf("apply_stealth(&shown, next)"));
  assert.match(shell, /pointer\("\/settings\/stealth"\)/, "it survives a restart");
});

test("only the workspace may change it, through a registered command", () => {
  assert.match(fn("stealth_set"), /main_only\(&window\)\?;/);
  assert.ok(read("../src-tauri/build.rs").includes('"stealth_set"'));
  assert.ok(JSON.parse(read("../src-tauri/capabilities/main.json")).permissions.includes("allow-stealth-set"));
});

test("the honest line travels with the feature", () => {
  assert.ok(shell.includes("Hidden from screen capture and screen sharing, not from a camera, a capture card or a process list."));
  for (const word of ["undetectable", "proctor", "invisible to"]) assert.ok(!shell.toLowerCase().includes(word), word);
});
