// Phase 24 (V1-130, i8-code-04): a conversation follows new text only while the person is at its end. 0.6.1 scrolled
// to the bottom on every streamed token, in both windows, so reading an earlier part of a long reply was impossible
// while it streamed. The measurement is taken when the text arrives, before the window draws it (the caller runs
// `follow()` synchronously in its event handler and passes its framework's next-tick as `after`), so the decision is
// about where the person was, not where the new text pushed them. Sending a message always shows it (`force()`).
// Pure: no route, no storage, no timers of its own.

export const STICK_PX = 48;

export interface Scroller {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
}

/** Pixels between the visible end of the scroller and the end of its content (0 at the bottom). */
export function distanceFromBottom(el: Scroller): number {
  return Math.max(0, el.scrollHeight - el.clientHeight - el.scrollTop);
}

/** Whether the person is at the end, within `threshold` px (STICK_PX). A missing scroller counts as at the end. */
export function atBottom(el: Scroller | null | undefined, threshold = STICK_PX): boolean {
  return !el || distanceFromBottom(el) <= threshold;
}

/** Show the end of the content, at once (no smooth scrolling, so reduced motion needs nothing more). */
export function toBottom(el: Scroller | null | undefined): void {
  if (el) el.scrollTop = el.scrollHeight;
}

export interface ScrollStick {
  /** New content arrived: follow it only if the person was at the end. Returns whether it will follow. */
  follow(): boolean;
  /** The person sent a message or opened a conversation: always show the end. */
  force(): void;
  /** Whether the person is at the end now. */
  stuck(): boolean;
}

export function createScrollStick(
  target: () => Scroller | null | undefined,
  after: (fn: () => void) => unknown,
  threshold = STICK_PX,
): ScrollStick {
  return {
    follow() {
      const stay = atBottom(target(), threshold);
      if (stay) after(() => toBottom(target()));
      return stay;
    },
    force() {
      after(() => toBottom(target()));
    },
    stuck: () => atBottom(target(), threshold),
  };
}
