import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Phase 42 (1.8.0, app control): Pebble asks for one app window in the conversation and the person allows it there;
// a press in a shared window shows what and where before it runs; the window panel asks macOS for Accessibility,
// which only the person can switch on. Window titles and labels are the app's own text, shown literally.
const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const app = read("./App.vue");
const review = read("./components/ActionReview.vue");
const panel = read("./components/ScreenSessions.vue");

test("every Companion and app-window action has its own title, lead and button (no 'Open undefined')", () => {
  for (const needle of ['action.kind === "shell"\n                                  ? action.payload.command',
    'action.kind === "file"\n                                    ? action.payload.path',
    '`Use ${action.payload.app}`', 'case "screen_grant":', 'case "screen":', 'case "shell":', '? "Allow"', '? "Do it"'])
    assert.ok(app.includes(needle), needle);
});

test("a window request shows the app, the window as the app names it, why, what Pebble could do and for how long", () => {
  for (const needle of ["kind === 'screen_grant'", "the window “{{ screen.window || 'untitled' }}” (as the app names it)",
    // Phase 45 (F198, D136): in this conversation, up to the grant's minutes; board squares join the exceptions (F181)
    "Pebble says: {{ screen.why }}", "In this conversation, for up to {{ screen.minutes }} minutes, Pebble could",
    "press buttons (each press waits for you, except keypad keys, board squares, tabs, checkboxes and switches)",  // Phase 45 (F156) "never a password, code or card field",
    "End access any time in Details › Tools (⇧⌘D)."])  // Phase 45 (F090): a place that exists
    assert.ok(review.includes(needle), needle);
});

test("a press in a shared window says what, where and that it cannot be undone", () => {
  for (const needle of ["kind === 'screen'", "Pebble will {{ screen.action || 'act on' }}", "Next to it on screen",
    "Pebble can't undo it and never retries it."])
    assert.ok(review.includes(needle), needle);
  assert.doesNotMatch(review, /v-html/, "app text is never rendered as HTML");
});

test("the window panel asks macOS for Accessibility and says where the switch is when macOS does not ask", () => {
  for (const needle of ["rpc<unknown>('app_control_status')", "rpc<unknown>('app_control_request')",
    "Ask macOS for Accessibility…", "Pebble can't turn it on.",
    "System Settings › Privacy &amp; Security › Accessibility (use + if Pebble isn't listed)"])
    assert.ok(panel.includes(needle), needle);
  assert.equal([...panel.matchAll(/rpc<unknown>\('app_control_request'\)/g)].length, 1, "one place asks macOS, on a click");
});
