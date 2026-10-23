import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Phase 31 (1.0.4, D43): the full workspace. The person found that in macOS full screen the Dock slid up over the
// message box and the red, yellow and green buttons were hidden (the overlay title bar's strip stays hidden in full
// screen). The pebble's "Open full workspace" now opens the workspace in full screen with the Dock hidden and the
// standard title bar; "Floating companion", the red button and ⌘W leave full screen first, then go back to the pebble.
// The person tried a build of this code before it was proved ("All works"). These checks hold the shell to it.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const shell = read("../src-tauri/src/main.rs");
const fn = (name) => {
  const start = shell.indexOf("fn " + name + "(");
  assert.ok(start >= 0, name);
  return shell.slice(start, shell.indexOf("\n}\n", start) + 2);
};

test("Open full workspace opens the workspace in full screen, never always on top", () => {
  const open = fn("open_workspace");
  assert.ok(open.indexOf("reveal(&app)") < open.indexOf("set_fullscreen(true)"), "shown first, then full screen");
  assert.match(open, /set_always_on_top\(false\)/);
  assert.match(open, /if !main\.is_fullscreen\(\)\.unwrap_or\(false\) \{ let _=main\.set_fullscreen\(true\); \}/);
  // ⌘⇧Space, the tray and reopening from Finder keep the window as it was.
  assert.doesNotMatch(fn("reveal"), /fullscreen/);
});

test("every way back to the pebble leaves full screen first, and only hides once it has", () => {
  assert.match(fn("show_pet"), /\{ to_pet\(&app\); \}/);
  assert.match(shell, /tauri::WindowEvent::CloseRequested\{api,\.\.\} if window\.label\(\)=="main" => \{ api\.prevent_close\(\); to_pet\(window\.app_handle\(\)\); \}/,
    "the red button and ⌘W");
  const toPet = fn("to_pet");
  assert.ok(toPet.indexOf("if !main_fullscreen(app) { return pet_now(app); }") >= 0, "not in full screen: straight back");
  assert.match(toPet, /set_fullscreen\(false\)/);
  // tao clears its own flag when asked to leave; AppKit's style bit stays until the transition has ended.
  assert.match(toPet, /main_fullscreen\(&probe\)/);
  assert.match(toPet, /run_on_main_thread\(move \|\| pet_now\(&shown\)\)/);
  assert.match(fn("appkit_fullscreen"), /const FULL_SCREEN: usize = 1 << 14;/, "NSWindowStyleMaskFullScreen");
  const pet = fn("pet_now");
  assert.ok(pet.indexOf('get_webview_window("pet")') < pet.indexOf("w.hide()"), "the pebble shows, the workspace hides");
});

test("full screen uses the standard title bar, and the overlay bar returns after it", () => {
  const sync = fn("sync_title_bar");
  assert.match(sync, /if full \{tauri::TitleBarStyle::Visible\} else \{tauri::TitleBarStyle::Overlay\}/);
  assert.match(shell, /tauri::WindowEvent::Resized\(_\) \| tauri::WindowEvent::Moved\(_\) \| tauri::WindowEvent::Focused\(_\) => sync_title_bar\(window\)/);
  const conf = JSON.parse(read("../src-tauri/tauri.conf.json"));
  const main = conf.app.windows.find((w) => w.label === "main");
  assert.equal(main.titleBarStyle, "Overlay", "the overlay bar outside full screen, as before");
});

test("the Dock is hidden in full screen through tao's delegate, pinned to the tao that has it", () => {
  const hide = fn("hide_dock_in_fullscreen");
  assert.match(hide, /AnyClass::get\(c"TaoWindowDelegate"\)/);
  assert.match(hide, /sel!\(window:willUseFullScreenPresentationOptions:\)/);
  assert.match(hide, /c"Q@:@Q"/, "NSUInteger (id, SEL, id, NSUInteger)");
  assert.match(hide, /\(proposed & !AUTO_HIDE_DOCK\) \| HIDE_DOCK/, "the person's menu bar setting is kept");
  assert.match(hide, /const AUTO_HIDE_DOCK: usize = 1 << 0;/);
  assert.match(hide, /const HIDE_DOCK: usize = 1 << 1;/);
  assert.match(shell, /configure_pet\(app\.handle\(\)\)\?;\n\s*#\[cfg\(target_os="macos"\)\]\n\s*let _ = hide_dock_in_fullscreen\(\);/,
    "installed once the windows (and so tao's delegate class) exist");
  // The class name and selector are tao 0.37's (platform_impl/macos/window_delegate.rs): a tao update must re-check them.
  const lock = read("../src-tauri/Cargo.lock");
  assert.match(lock, /name = "tao"\nversion = "0\.37\.1"/);
});
