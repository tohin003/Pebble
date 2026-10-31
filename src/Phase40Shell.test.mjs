import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Phase 40 (1.6.0): the companion joins the Space Pebble starts on (measured with the shell's diagnostic on a real Mac
// before install), and the window's new review cards, undo and Settings rows for the Companion's sandboxed tools.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const shell = read("../src-tauri/src/main.rs");
const fn = (name) => {
  const start = shell.indexOf("fn " + name + "(");
  assert.ok(start >= 0, name);
  return shell.slice(start, shell.indexOf("\n}\n", start) + 2);
};

test("after launch the companion rejoins the Space Pebble started on, three times, on the main thread", () => {
  const setup = shell.slice(shell.indexOf("companion_follows_spaces(app.handle());"));
  assert.ok(setup.indexOf("companion_rejoin_after_launch(app.handle());") > 0, "called right after the Space follower");
  const launch = fn("companion_rejoin_after_launch");
  assert.match(launch, /for wait in \[300u64, 700, 1500\]/);
  assert.match(launch, /run_on_main_thread\(move \|\| companion_rejoin\(&main_thread, "launch"\)\)/);
  assert.match(fn("companion_rejoin"), /if policy != 1 \{ return; \}/);
});

test("Shell and file asks render their reasons, hosts, folders and undo note; Always allow only where offered", () => {
  const review = read("./components/ActionReview.vue");
  for (const needle of ["kind === 'shell'", "kind === 'file'", "Network (only these hosts)", "May write outside the workbench",
    "Pebble keeps a copy of what this may change first", "it cannot be allowed always"])
    assert.ok(review.includes(needle), needle);
  const app = read("./App.vue");
  assert.match(app, /v-if="\(action\.kind === 'shell' \|\| action\.kind === 'file'\) && action\.payload\.always_offered"/);
  assert.match(app, /Undo this change/);
});

test("Settings says what the Shell may do and lists what is always allowed, with Remove", () => {
  const settings = read("./components/AdvancedSettings.vue");
  for (const needle of ["capture: \"Take screenshots when a task needs them\"", "PEBBLE'S SHELL", "removeShellGrant(kind",
    "never read or written, whatever you allow"])
    assert.ok(settings.includes(needle), needle);
});
