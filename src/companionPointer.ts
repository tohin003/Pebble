// Pointer gaze and the one-animated-avatar rule (design study §2.2, F4, F12).
//
// Each window has at most one document-level `pointermove` listener, shared by
// every avatar that follows the pointer and throttled to one update per
// animation frame. Only one avatar animates per window: the welcome avatar if
// shown, else the status line's companion while Pebble works (Phase 22), else
// the last message, else the sidebar brand (by `rank`, lower wins; a tie goes
// to the most recently mounted). The status line is never mounted with the
// booting or welcome avatars, so their shared rank 0 never competes.
//
// In the pet the 112 pt window cannot see a pointer "around" it, so the shell
// samples the cursor natively and sends only a quantized direction in [-1, 1]
// (parent patch P8, `pet_gaze`). Screen coordinates never reach this module.
// Gaze is presentation only: it never changes what the companion says.
import { getCurrentScope, onScopeDispose, ref, shallowRef, watch, type Ref } from "vue";
import type { Look } from "./vendor/bloub/engine";

export interface Gaze {
  x: number;
  y: number;
}
export const GAZE_STEPS = 8; // quantization: 17 positions per axis
export const REACH_PX = 200; // a pointer this far from the centre turns the eyes fully
export const AVATAR_RANK = { booting: 0, welcome: 0, status: 0, message: 1, brand: 2 } as const;

export function quantize(value: number, steps = GAZE_STEPS): number {
  if (!Number.isFinite(value)) return 0;
  const q = Math.round(Math.max(-1, Math.min(1, value)) * steps) / steps;
  return q === 0 ? 0 : q; // no -0
}

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}
/** Direction from an avatar's box to a pointer, or null for an empty or non-finite box. */
export function gazeToward(x: number, y: number, box: Box | null | undefined): Gaze | null {
  if (!box) return null;
  const values = [x, y, box.left, box.top, box.width, box.height];
  if (!values.every(Number.isFinite) || box.width <= 0 || box.height <= 0) return null;
  return {
    x: quantize((x - (box.left + box.width / 2)) / REACH_PX),
    y: quantize((y - (box.top + box.height / 2)) / REACH_PX),
  };
}

/** A native `pet-gaze` payload: exactly {x, y} in [-1, 1], else null (eyes rest). */
export function parseNativeGaze(payload: unknown): Gaze | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const p = payload as Record<string, unknown>;
  const keys = Object.keys(p);
  if (keys.length !== 2 || !keys.includes("x") || !keys.includes("y")) return null;
  const { x, y } = p;
  if (typeof x !== "number" || typeof y !== "number" || !Number.isFinite(x) || !Number.isFinite(y)) return null;
  if (Math.abs(x) > 1 || Math.abs(y) > 1) return null;
  return { x: quantize(x), y: quantize(y) };
}

export function sameGaze(a: Gaze | null, b: Gaze | null): boolean {
  return a === b || (!!a && !!b && a.x === b.x && a.y === b.y);
}

/** The bloub look for a gaze; null returns the eyes to the pose's own direction. */
// Gaze is in screen terms (x right, y DOWN), from the DOM pointer and the shell's sampler alike; bloub's pitch is
// positive UP (vendor/bloub/face.ts HeadGaze), so y changes sign here. Phase 26 (D24): the person saw the eyes look
// up at a pointer below the avatar and down at one above it.
export function lookFor(gaze: Gaze | null): Look | null {
  return gaze ? { yaw: gaze.x * 40, pitch: -gaze.y * 30, mix: 0.8, spin: 0, wander: 0.1 } : null;
}

/** Gaze follows the pointer only with the setting on and motion not reduced. */
export function gazeAllowed(o: { pointer?: boolean; reduced?: boolean; systemReduced?: boolean }): boolean {
  return o.pointer !== false && !o.reduced && !o.systemReduced;
}

// ---- one shared document listener per window -------------------------------

interface Follower {
  box(): Box | null;
  set(gaze: Gaze | null): void;
}
const followers = new Set<Follower>();
let listening = false,
  frame = 0,
  pointer: { x: number; y: number } | null = null;

function flush() {
  frame = 0;
  for (const f of followers) f.set(pointer ? gazeToward(pointer.x, pointer.y, f.box()) : null);
}
function schedule() {
  if (!frame) frame = requestAnimationFrame(flush);
}
function onMove(e: PointerEvent) {
  pointer = { x: e.clientX, y: e.clientY };
  schedule();
}
function onOut(e: PointerEvent) {
  if (e.relatedTarget) return; // still inside the window
  pointer = null;
  schedule();
}
function start() {
  if (listening) return;
  listening = true;
  document.addEventListener("pointermove", onMove, { passive: true });
  document.addEventListener("pointerout", onOut, { passive: true });
}
function stop() {
  if (!listening || followers.size) return;
  listening = false;
  document.removeEventListener("pointermove", onMove);
  document.removeEventListener("pointerout", onOut);
  if (frame) cancelAnimationFrame(frame);
  frame = 0;
  pointer = null;
}
/** Diagnostics for tests and the frame fixture. */
export function pointerListeners() {
  return { listening, followers: followers.size };
}

/**
 * Gaze toward the pointer for one element, while `enabled()` holds. The pointer
 * leaving the window rests the eyes (null).
 */
export function usePointerGaze(el: { readonly value: Element | null | undefined }, enabled: () => boolean) {
  const gaze = shallowRef<Gaze | null>(null);
  const follower: Follower = {
    box: () => {
      const node = el.value;
      return node && typeof node.getBoundingClientRect === "function" ? node.getBoundingClientRect() : null;
    },
    set: (g) => {
      if (!sameGaze(g, gaze.value)) gaze.value = g;
    },
  };
  const sync = (on: boolean) => {
    if (on && !followers.has(follower)) {
      followers.add(follower);
      start();
      if (pointer) schedule();
    } else if (!on && followers.has(follower)) {
      followers.delete(follower);
      gaze.value = null;
      stop();
    }
  };
  const halt = watch(enabled, sync, { immediate: true });
  const dispose = () => {
    halt();
    sync(false);
  };
  if (getCurrentScope()) onScopeDispose(dispose);
  return { gaze, dispose };
}

// ---- one animated avatar per window (F12) ----------------------------------

interface Claim {
  rank: () => number;
  seq: number;
  active: Ref<boolean>;
}
const claims: Claim[] = [];
let sequence = 0;
function elect() {
  const rankOf = (c: Claim) => {
    const r = c.rank();
    return Number.isFinite(r) ? r : 1;
  };
  let best: Claim | null = null;
  for (const c of claims)
    if (!best || rankOf(c) < rankOf(best) || (rankOf(c) === rankOf(best) && c.seq > best.seq)) best = c;
  for (const c of claims) c.active.value = c === best;
}
/** Ask for this window's single animation slot; `active` says whether this avatar holds it. */
export function claimAnimation(rank: () => number) {
  const claim: Claim = { rank, seq: ++sequence, active: ref(false) };
  claims.push(claim);
  elect();
  return {
    active: claim.active,
    update: elect,
    release() {
      const i = claims.indexOf(claim);
      if (i >= 0) claims.splice(i, 1);
      claim.active.value = false;
      elect();
    },
  };
}
export function animationClaims() {
  return claims.length;
}

// ---- the pet's native gaze (P8) --------------------------------------------

export interface NativeGazeBridge {
  listen(event: string, handler: (e: { payload: unknown }) => void): Promise<() => void>;
  invoke(command: string, args: Record<string, unknown>): Promise<unknown>;
}

/**
 * Drives `pet_gaze(enabled)` for the pet window. Sampling is requested only
 * while `allowed()` holds and the document is visible; the shell stops by
 * itself after 5 s without motion (it then sends null), and this re-arms it
 * when the pointer leaves the window, the window becomes visible or focused,
 * or `rearm()` is called (for example on a pose change).
 */
export function useNativeGaze(bridge: NativeGazeBridge, allowed: () => boolean) {
  const look = shallowRef<Gaze | null>(null);
  let armed = false,
    closed = false,
    unlisten: (() => void) | null = null;
  const permitted = () => !closed && allowed() && !document.hidden;
  async function rearm() {
    if (!permitted()) return disarm();
    if (armed) return;
    armed = true;
    try {
      await bridge.invoke("pet_gaze", { enabled: true });
    } catch {
      armed = false;
    }
  }
  async function disarm() {
    if (look.value) look.value = null;
    if (!armed) return;
    armed = false;
    try {
      await bridge.invoke("pet_gaze", { enabled: false });
    } catch {
      /* the shell stops by itself after 5 s without motion */
    }
  }
  const visibility = () => (document.hidden ? disarm() : rearm());
  const leave = (e: PointerEvent) => {
    if (!e.relatedTarget) rearm();
  };
  const focus = () => rearm();
  document.addEventListener("visibilitychange", visibility);
  document.addEventListener("pointerout", leave, { passive: true });
  globalThis.addEventListener?.("focus", focus);
  bridge
    .listen("pet-gaze", (e) => {
      const g = parseNativeGaze(e.payload);
      if (!g) armed = false; // null: the shell stopped sampling (still pointer, hidden pet or out of range)
      if (!sameGaze(g, look.value)) look.value = permitted() ? g : null;
    })
    .then((off) => {
      if (closed) off();
      else unlisten = off;
    })
    .catch(() => {});
  const halt = watch(allowed, (on) => (on ? rearm() : disarm()), { immediate: true });
  function dispose() {
    halt();
    disarm();
    closed = true;
    document.removeEventListener("visibilitychange", visibility);
    document.removeEventListener("pointerout", leave);
    globalThis.removeEventListener?.("focus", focus);
    unlisten?.();
    unlisten = null;
  }
  if (getCurrentScope()) onScopeDispose(dispose);
  return { look, rearm, disarm, dispose };
}
