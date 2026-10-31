import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Phase 38 (1.5.1, the person's request): the floating companion stays on screen with the person over every app,
// full-screen ones included, like a genie; the full workspace still opens like any other app, in Pebble's own Space,
// never over another app's full screen. Joining every Space is not enough for another app's full-screen Space: macOS
// shows only a non-activating panel there, so the companion is always one (the panel Stealth Lockdown makes). The
// behaviour is native and needs a person at a real Mac; these checks hold the shell's source to it.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const shell = read("../src-tauri/src/main.rs");
const fn = (name) => {
  const start = shell.indexOf("fn " + name + "(");
  assert.ok(start >= 0, name);
  return shell.slice(start, shell.indexOf("\n}\n", start) + 2);
};

test("the companion is a non-activating panel from launch, on every Space, and never hides when Pebble is inactive", () => {
  const companion = fn("companion_over_full_screen");
  assert.match(companion, /get_webview_window\("pet"\)/);
  assert.match(companion, /stealth_mac::set_panel\(ns, true\)/, "always a panel, stealth or not");
  assert.match(companion, /setHidesOnDeactivate: objc2::runtime::Bool::NO/, "a panel would otherwise hide");
  assert.doesNotMatch(companion, /"main"/, "only the companion");
  const configure = fn("configure_pet");
  assert.match(configure, /B::CanJoinAllSpaces \| B::FullScreenAuxiliary/, "joins every Space, full-screen ones too");
  assert.ok(configure.indexOf("setCollectionBehavior(behavior)") < configure.indexOf("companion_over_full_screen(app)"),
    "a panel after its Spaces are set");
});

test("turning stealth off keeps the companion a panel; the workspace becomes an ordinary window again", () => {
  const apply = fn("apply_stealth");
  assert.match(apply, /else \{ let _=stealth_mac::set_panel\(ns, on\); \}/, "1.5.0's switch for every window, unchanged");
  const loopEnd = apply.indexOf("stealth_mac::set_sharing_all(on)");
  const companion = apply.indexOf("companion_over_full_screen(app)");
  assert.ok(companion > loopEnd, "the companion is made a panel again after the switch");
  assert.ok(companion < apply.indexOf("tray_state(app, on)"));
});

test("the companion never activates Pebble, so a full-screen app stays in front", () => {
  const focus = fn("focus");
  assert.match(focus, /if stealth_on\(\) \|\| window\.label\(\)=="pet" \{/);
  assert.ok(focus.indexOf('window.label()=="pet"') < focus.indexOf("set_focus()"), "set_focus only for the workspace");
  assert.match(fn("pet_expand"), /if expanded \{ focus\(&window\); \}/);
});

test("the full workspace opens like any other app, in its own Space, never over another app's full screen", () => {
  const conf = JSON.parse(read("../src-tauri/tauri.conf.json"));
  const main = conf.app.windows.find((w) => w.label === "main");
  const pet = conf.app.windows.find((w) => w.label === "pet");
  assert.equal(main.visibleOnAllWorkspaces, undefined, "the workspace stays in its own Space");
  assert.equal(main.alwaysOnTop, undefined);
  assert.equal(pet.visibleOnAllWorkspaces, true);
  // Every all-Spaces and full-screen-auxiliary behaviour in the shell is the companion's, set in configure_pet.
  const start = shell.indexOf("fn configure_pet(");
  const end = start + fn("configure_pet").length;
  for (const needle of ["CanJoinAllSpaces", "FullScreenAuxiliary", "CanJoinAllApplications", "set_visible_on_all_workspaces"])
    for (const match of shell.matchAll(new RegExp(needle, "g")))
      assert.ok(match.index > start && match.index < end, needle + " outside configure_pet");
  const reveal = fn("reveal");
  assert.match(reveal, /if !stealth_on\(\) \{ let _=app\.set_activation_policy\(tauri::ActivationPolicy::Regular\); \}/,
    "an ordinary app with a Dock icon, so macOS takes the person to its Space");
  assert.match(fn("open_workspace"), /if full && !stealth_on\(\) && !main\.is_fullscreen\(\)\.unwrap_or\(false\) \{ let _=main\.set_fullscreen\(true\); \}/,
    "its own full-screen Space, as in 1.0.3, when the person left it full screen (D146)");
  // Phase 45 (D146, the person): the first open is full screen; leaving it windowed is remembered at the pebble
  assert.match(shell, /static WORKSPACE_FULL: AtomicBool = AtomicBool::new\(true\);/);
  assert.match(fn("to_pet"), /if !stealth_on\(\) && app\.get_webview_window\("main"\)\.map\(\|w\| w\.is_visible\(\)\.unwrap_or\(false\)\)\.unwrap_or\(false\) \{\s*WORKSPACE_FULL\.store\(main_fullscreen\(app\), Ordering::SeqCst\);/);
});
