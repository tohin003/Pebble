// One keyboard registry for the main window and the pet (design study §4.5).
// Both windows match keydown events against it, and Settings renders it, so the
// listed keys are exactly the bound keys. The webview binds only window-scoped
// entries: the composer binds Enter itself, and the native shell registers the
// global reveal shortcut (main.rs). No entry approves, answers or changes a setting.

export type Surface = "main" | "pet";
export type ShortcutId =
  | "palette"
  | "stop"
  | "activity"
  | "details"
  | "copy_reply"
  | "new_chat"
  | "settings"
  | "close"
  | "send"
  | "reveal";
export interface Shortcut {
  readonly id: ShortcutId;
  readonly label: string;
  /** KeyboardEvent.key in lower case. */
  readonly key: string;
  /** ⌘ on macOS, Ctrl elsewhere. */
  readonly mod: boolean;
  readonly shift: boolean;
  readonly surfaces: readonly Surface[];
  readonly scope: "window" | "composer" | "global";
}
export interface KeyLike {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  shiftKey?: boolean;
  altKey?: boolean;
  repeat?: boolean;
  isComposing?: boolean;
}

const both = Object.freeze(["main", "pet"] as Surface[]);
const main = Object.freeze(["main"] as Surface[]);
export const SHORTCUTS: readonly Shortcut[] = Object.freeze(
  (
    [
      { id: "palette", label: "Open the command palette", key: "k", mod: true, shift: false, surfaces: both, scope: "window" },
      { id: "stop", label: "Stop the current task", key: ".", mod: true, shift: false, surfaces: both, scope: "window" },
      { id: "activity", label: "Show task activity", key: "j", mod: true, shift: false, surfaces: both, scope: "window" },
      // Phase 22: the main window's Details drawer (task, goal, schedules, project, windows, browser).
      { id: "details", label: "Show or hide details", key: "d", mod: true, shift: true, surfaces: main, scope: "window" },
      { id: "copy_reply", label: "Copy the last reply", key: "c", mod: true, shift: true, surfaces: main, scope: "window" },
      { id: "new_chat", label: "New conversation", key: "n", mod: true, shift: false, surfaces: main, scope: "window" },
      { id: "settings", label: "Open settings", key: ",", mod: true, shift: false, surfaces: main, scope: "window" },
      { id: "close", label: "Close the open panel or dialog", key: "escape", mod: false, shift: false, surfaces: both, scope: "window" },
      { id: "send", label: "Send the message", key: "enter", mod: false, shift: false, surfaces: both, scope: "composer" },
      { id: "reveal", label: "Show Pebble from any app", key: " ", mod: true, shift: true, surfaces: both, scope: "global" },
    ] as Shortcut[]
  ).map((item) => Object.freeze(item)),
);

/** The window-scoped shortcut this keydown triggers in a surface, if any. */
export function matchShortcut(event: KeyLike, surface: Surface): ShortcutId | null {
  if (event.isComposing || event.altKey || event.repeat) return null;
  const key = String(event.key || "").toLowerCase();
  const mod = !!(event.metaKey || event.ctrlKey);
  const found = SHORTCUTS.find(
    (item) =>
      item.scope === "window" &&
      item.surfaces.includes(surface) &&
      item.key === key &&
      item.mod === mod &&
      item.shift === !!event.shiftKey,
  );
  return found ? found.id : null;
}

/** Every entry a surface documents, including the composer and global keys. */
export function shortcutsFor(surface: Surface): Shortcut[] {
  return SHORTCUTS.filter((item) => item.surfaces.includes(surface));
}

const names: Record<string, string> = { escape: "Esc", enter: "Enter", " ": "Space" };
/** Display text, e.g. ⌘K on macOS and Ctrl+K elsewhere. */
export function keysLabel(item: Shortcut, platform = "darwin"): string {
  const key = names[item.key] || item.key.toUpperCase();
  if (platform === "darwin")
    return (item.mod ? "⌘" : "") + (item.shift ? "⇧" : "") + key;
  return [item.mod ? "Ctrl" : "", item.shift ? "Shift" : "", key]
    .filter(Boolean)
    .join("+");
}

/**
 * Arrow-key navigation between focusable rows of an inspector table (roving
 * tabindex). Acts only when a row itself has focus, so controls inside a row
 * keep their own keys. Returns whether focus moved.
 */
export function moveRowFocus(event: KeyboardEvent): boolean {
  const row = event.target as HTMLElement | null;
  const body = row?.parentElement;
  if (!row || row.tagName !== "TR" || !body) return false;
  const rows = Array.from(body.children).filter(
    (item): item is HTMLElement => item.tagName === "TR",
  );
  const index = rows.indexOf(row);
  const next = (
    { ArrowDown: index + 1, ArrowUp: index - 1, Home: 0, End: rows.length - 1 } as Record<string, number>
  )[event.key];
  if (next === undefined || index < 0 || !rows[next] || next === index) return false;
  event.preventDefault();
  row.tabIndex = -1;
  rows[next].tabIndex = 0;
  rows[next].focus();
  return true;
}
