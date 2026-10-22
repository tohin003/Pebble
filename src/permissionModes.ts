// Phase 24 (V1-103, i11a-core-06): the one source of the permission modes' names and exact copy. Settings, the pet
// and the composer label read these strings; none keeps its own. The words say what the host does today
// (backend/pebble/permissions.py automatic(), terminal.py READ_ONLY and the requires_review rules), not what a mode
// might mean: Auto and Full run the same terminal commands, and Full adds only app launches and Git changes that
// neither delete work nor publish. Changing what a mode allows means changing this copy in the same change (D3).
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
  auto: mode(
    "auto",
    "Auto in project",
    "Edits to project files, test runs, and a short list of read-only commands (ls, cat, rg, head, tail, wc and a few more) in this project.",
    "Other terminal commands (including grep and builds), app launches, Git changes, connector calls, installs, and network or dangerous commands.",
  ),
  full: mode(
    "full",
    "Full access",
    "Everything Auto runs, plus app launches and Git changes that neither delete work nor publish it.",
    "Other terminal commands (including grep and builds), connector calls, installs, browser and app-window actions, network or dangerous commands, and Git changes that delete work or publish it.",
  ),
});
export const MODE_IDS: readonly PermissionMode[] = Object.freeze(["ask", "auto", "full"] as PermissionMode[]);
// True in every mode; shown once under the choices.
export const EVERY_MODE =
  "In every mode, reading project files, searching and reading the web, and creating views run without asking while their switches below are on. Browser and app-window actions, connector calls and installs always ask. Protected credentials stay blocked.";

/** The mode's one name, or "Ask for approval" for anything unknown (the host's default). */
export function modeName(value: unknown): string {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(MODES, value) ? MODES[value as PermissionMode].name : MODES.ask.name;
}
