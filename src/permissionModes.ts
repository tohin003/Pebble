// Phase 24 (V1-103, i11a-core-06): the one source of the permission modes' names and exact copy. Settings, the pet
// and the composer label read these strings; none keeps its own. The words say what the host does today
// (backend/pebble/permissions.py automatic(), terminal.py READ_ONLY, confined.py and the requires_review rules), not what
// a mode might mean. Changing what a mode allows means changing this copy in the same change (D3).
// Pure data: no route, no markup, and nothing here grants or changes a permission.

export type PermissionMode = "ask" | "auto" | "full";
export interface ModeCopy {
  readonly id: PermissionMode;
  readonly name: string;
  readonly runs_without_asking: string;
  readonly always_asks: string;
}

const mode = (id: PermissionMode, name: string, runs_without_asking: string, always_asks: string): ModeCopy =>
  Object.freeze({ id, name, runs_without_asking, always_asks });

export const MODES: Readonly<Record<PermissionMode, ModeCopy>> = Object.freeze({
  ask: mode(
    "ask",
    "Ask for approval",
    "Nothing that changes your project or your Mac.",
    "Every edit, test run, terminal command, app launch, Git change and connector call.",
  ),
  // Phase 34 (W1): edit_scope.review_reason holds a test-file change the request didn't ask for, in Auto mode too.
  // Phase 46 (1.9.28, D184, the person: "Ask only for risky steps"): a command confined to the project runs with a copy
  // of it behind it (backend/pebble/confined.py), and a Git step the plans can undo runs (git_plans.undo_kept).
  auto: mode(
    "auto",
    "Auto in project",
    "Edits to project files, test runs, read-only commands (ls, cat, rg and a few more), commands that stay in this project without network (Pebble copies the project first), and Git steps it can undo.",
    "Changes to test files your request didn't ask for, commands with network, installs or paths outside the project, Git pushes, deletions and history rewrites, app launches and connector calls.",
  ),
  // Phase 45 (D141, the person, 2026-10-26): in Full access Pebble checks each step that would ask against the
  // person's own messages (backend/pebble/step_judge.py) and runs the ones they asked for; money always asks.
  // Phase 46 (1.9.28, D185, the person: "Send it back; ask if stuck"): a step they did not ask for, or a delete, push
  // or send their words do not name, goes back to Pebble instead of asking; Pebble asks after 3 in a row or 20 in a task.
  // A command confined to the project that the host shows only reads or makes new files runs here unchecked (w14-full
  // item 7); a delete, an overwrite or a program the host cannot read keeps the check against the person's words (D185,
  // the final review: Auto's copy goes when the task ends, D189, or a day later since 1.9.29, D209; backend/pebble/app.py,
  // App.execute).
  full: mode(
    "full",
    "Full access",
    "Commands that only read or make new files, and each step your messages ask for: commands (including grep and builds), installs, Git, connector calls, app launches, windows, sites, presses.",
    "Spending money and your hooks' reviews. A step you didn't ask for, or a delete, push or send your words don't name, doesn't run; Pebble asks after 3 in a row or 20 in a task.",
  ),
});
export const MODE_IDS: readonly PermissionMode[] = Object.freeze(["ask", "auto", "full"] as PermissionMode[]);
// True in every mode; shown once under the choices.
export const EVERY_MODE =
  "In every mode, reading project files, searching and reading the web, and creating views run without asking while their switches below are on, and signing in is yours. In Ask and Auto, connector calls, installs and every press in a Chrome tab you granted always ask; looking, typing and choosing in that tab run under your grant. Protected credentials stay blocked.";

/** The mode's one name, or "Ask for approval" for anything unknown (the host's default). */
export function modeName(value: unknown): string {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(MODES, value) ? MODES[value as PermissionMode].name : MODES.ask.name;
}
