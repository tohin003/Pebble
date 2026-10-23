// Phase 33 (1.1.0, D57): the Veil, Stealth Lockdown's look on the pebble. The person chose look A from the design
// preview (.runtime/stealth-preview-v1.html); these are its timings and curves, as a pure function of the seconds since
// the switch last changed, so every avatar, the tests and the README renders agree.
//   Activation 1.25 s: gather (squash to 92%, eyes close) 0-0.15 s; cloak (the dusk colour wipes down behind a bright
//   hem, one ping, stretch to 104%) 0.15-0.6 s; wake (eyes reopen to a calm 80%, the light rim fades in) 0.6-0.85 s;
//   finale (the glass arrives) 0.85-1.25 s.
//   Release 0.65 s: fold (glass and rim go) 0-0.25 s; lift (the colour wipes back up) 0.15-0.45 s; hop (3.5% and one
//   blink) 0.45-0.65 s.
//   Reduce Motion: one 0.3 s cross-fade each way, no squash, wipe, ping or blink.
//   While on: a 70%-opaque glass body and one 1.1 s shimmer every 6 s (none under Reduce Motion or in a still frame).
// A change mid-transition starts from where the last one had got to, so reversing the switch never jumps.
// The honest line (D47): shown under the switch, in the README, the User Guide and the CHANGELOG, word for word.
export const STEALTH_HONEST =
  "Hidden from screen capture and screen sharing, not from a camera, a capture card or a process list. Exams and interviews that forbid assistants still forbid it.";
export const STEALTH_ON_TEXT = "On. Pebble is hidden from screen capture and won't take focus. Find it in the menu bar.";
export const STEALTH_OFF_TEXT = "Off. Pebble shows in screen shares, the Dock and ⌘Tab as usual.";

export const ACTIVATE = 1.25;
export const RELEASE = 0.65;
export const FADE = 0.3;
export const SHIMMER_EVERY = 6;
export const SHIMMER = 1.1;
export const GLASS_OPACITY = 0.7;

export interface Veil {
  cloak: number; // how far the dusk colour has covered the body, 0-1 (the wipe edge, or the cross-fade)
  rim: number; // the light rim's strength, 0-1
  glass: number; // the glass finish, 0-1 (body opacity runs from 1 to GLASS_OPACITY)
  eye: number; // eye openness factor, 1 open, 0.8 calm, 0 closed
  sy: number; // vertical squash/stretch about the bottom of the pebble
  sx: number;
  hem: number; // the bright line at the wipe edge, 0-1
  ping: number; // the ring's progress 0-1, or -1 when there is none
  fade: boolean; // Reduce Motion: cross-fade the layers instead of wiping
}

export const OFF: Veil = { cloak: 0, rim: 0, glass: 0, eye: 1, sy: 1, sx: 1, hem: 0, ping: -1, fade: false };
export const ON: Veil = { cloak: 1, rim: 1, glass: 1, eye: 0.8, sy: 1, sx: 1, hem: 0, ping: -1, fade: false };

const clamp = (v: number, a = 0, b = 1) => (v < a ? a : v > b ? b : v);
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const outCubic = (t: number) => 1 - (1 - t) ** 3;
const inCubic = (t: number) => t * t * t;
const inOutCubic = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);
const outBack = (t: number) => 1 + 2.70158 * (t - 1) ** 3 + 1.70158 * (t - 1) ** 2;

/** The Veil `dt` seconds after the switch went `on` (or off), starting from `from`. */
export function veil(on: boolean, dt: number, from: Veil = on ? OFF : ON, reduced = false): Veil {
  if (reduced) {
    const c = inOutCubic(clamp(dt / FADE));
    const f = on ? from.cloak + (1 - from.cloak) * c : from.cloak * (1 - c);
    return { cloak: f, rim: f, glass: f, eye: 1 - 0.2 * f, sy: 1, sx: 1, hem: 0, ping: -1, fade: true };
  }
  let sy = 1, eye: number, hem: number, cloak: number, rim: number, glass: number, ping = -1;
  if (on) {
    cloak = from.cloak + (1 - from.cloak) * (dt < 0.15 ? 0 : inOutCubic(clamp((dt - 0.15) / 0.45)));
    rim = from.rim + (1 - from.rim) * outCubic(clamp((dt - 0.6) / 0.25));
    glass = from.glass + (1 - from.glass) * outCubic(clamp((dt - 0.85) / 0.4));
    if (dt < 0.15) sy = lerp(1, 0.92, outCubic(clamp(dt / 0.15)));
    else if (dt < 0.45) sy = lerp(0.92, 1.04, outCubic((dt - 0.15) / 0.3));
    else if (dt < 0.75) sy = lerp(1.04, 1, inOutCubic((dt - 0.45) / 0.3));
    if (dt < 0.06) eye = lerp(from.eye, 0, clamp(dt / 0.06));
    else if (dt < 0.6) eye = 0;
    else if (dt < 0.85) eye = 0.8 * outBack((dt - 0.6) / 0.25);
    else eye = 0.8;
    hem = dt > 0.15 && dt < 0.6 ? Math.sin((Math.PI * (dt - 0.15)) / 0.45) : 0;
    if (dt >= 0.15 && dt < 0.85) ping = (dt - 0.15) / 0.7;
  } else {
    glass = from.glass * (1 - inCubic(clamp(dt / 0.25)));
    rim = from.rim * (1 - clamp(dt / 0.2));
    cloak = from.cloak * (1 - inOutCubic(clamp((dt - 0.15) / 0.3)));
    if (dt >= 0.45 && dt < 0.55) sy = lerp(1, 1.035, outCubic((dt - 0.45) / 0.1));
    else if (dt >= 0.55 && dt < 0.65) sy = lerp(1.035, 1, inOutCubic((dt - 0.55) / 0.1));
    if (dt < 0.42) eye = from.eye;
    else if (dt < 0.48) eye = lerp(from.eye, 0, (dt - 0.42) / 0.06);
    else if (dt < 0.62) eye = outCubic((dt - 0.48) / 0.14);
    else eye = 1;
    hem = dt > 0.15 && dt < 0.45 && from.cloak > 0.05 ? Math.sin((Math.PI * (dt - 0.15)) / 0.3) : 0;
  }
  return { cloak, rim, glass, eye, sy, sx: 1 + (1 - sy) * 0.7, hem, ping, fade: false };
}

/** Whether a transition started `dt` seconds ago is still moving (the avatar needs animation frames). */
export function moving(on: boolean, dt: number, reduced = false): boolean {
  return dt < (reduced ? FADE : on ? ACTIVATE : RELEASE);
}

/** The shimmer band's progress 0-1 and strength while on, `t` seconds after the activation ended; null between. */
export function shimmer(t: number): { x: number; opacity: number } | null {
  if (t < 0) return null;
  const c = t % SHIMMER_EVERY;
  if (c >= SHIMMER) return null;
  const p = c / SHIMMER;
  return { x: inOutCubic(p), opacity: Math.sin(Math.PI * p) };
}

let made = 0;
/** Clip and gradient ids unique to one avatar (several avatars share a document). */
export function nextVeilId() {
  const n = ++made;
  return { body: `veil-body-${n}`, base: `veil-base-${n}`, cloak: `veil-cloak-${n}`, glass: `veil-glass-${n}`, sheen: `veil-sheen-${n}` };
}
