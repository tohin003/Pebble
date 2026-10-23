<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from "vue";
import { avatarMotion } from "../avatarMotion";
import { visualFor } from "../companionState";
import { claimAnimation, lookFor, sameGaze, usePointerGaze, type Gaze } from "../companionPointer";
import { BotEngine } from "../vendor/bloub/engine";
import { liveliness } from "../vendor/bloub/face";
import { SHAPE_BY_ID } from "../vendor/bloub/skins";
import { EXPRESSION_BY_ID } from "../vendor/bloub/expressions";
import { ACTIVATE, GLASS_OPACITY, OFF, ON, RELEASE, moving, nextVeilId, shimmer, veil, type Veil } from "../stealthVeil";
// The pose comes from host signals (companionState.ts). `look` is an external
// gaze (the pet's native sampler); inside the window the shared document
// listener is used. Only the window's elected avatar animates or follows the
// pointer (F12); every other one, and every avatar under reduced motion, shows
// one settled static frame and runs no animation frame at all.
const props = withDefaults(
  defineProps<{
    state?: string;
    size?: number;
    reduced?: boolean;
    look?: Gaze | null;
    gaze?: boolean;
    animate?: boolean;
    rank?: number;
    fps?: number;
    stealth?: boolean;
  }>(),
  { state: "idle", size: 80, reduced: false, look: undefined, gaze: true, animate: true, rank: 1, fps: 30, stealth: false },
);
const HOLD_SECONDS = 8; // calm poses settle to a static frame (§2.4)
const engine = new BotEngine(65, "idle", SHAPE_BY_ID.get("galet")!.radii);
const frame = shallowRef(engine.sample(0));
const time = ref(0);
const svg = ref<SVGSVGElement | null>(null);
const systemMotion = matchMedia("(prefers-reduced-motion: reduce)");
const systemReduced = ref(systemMotion.matches);
const visual = computed(() => visualFor(props.state));
const wants = computed(() => props.animate && !props.reduced && !systemReduced.value);
// The window's single animation slot, claimed only while mounted and wanting it.
const slot = shallowRef<ReturnType<typeof claimAnimation> | null>(null);
const elected = computed(() => !!slot.value && slot.value.active.value);
const still = computed(() => !wants.value || !elected.value);
const motion = computed(() => avatarMotion(visual.value.id, time.value, still.value));
const pointer = usePointerGaze(svg, () => props.gaze && !still.value);
const gaze = computed<Gaze | null>(() =>
  !props.gaze || still.value ? null : pointer.gaze.value || (props.look ?? null),
);
// Phase 33 (1.1.0, D57): the Veil, Stealth Lockdown's look (stealthVeil.ts). The still frame carries the tint and the
// rim, so an avatar that does not animate (another one is elected, or motion is reduced) still shows the mode; the
// cast, the release and the shimmer play only on the animated one. Under 64 px the glass, highlight, shimmer and ping
// are left out and the rim is thickened, so 24 and 30 px avatars keep the signal.
const ids = nextVeilId();
const fine = computed(() => props.size >= 64);
const motionReduced = computed(() => props.reduced || systemReduced.value);
let veilAt = -Infinity,
  veilFrom: Veil = props.stealth ? ON : OFF;
const veilFrame = shallowRef<Veil>(props.stealth ? ON : OFF);
const shine = shallowRef<{ x: number; opacity: number } | null>(null);
function veilAtTime(t: number): Veil {
  if (still.value || veilAt === -Infinity) return props.stealth ? { ...ON, fade: motionReduced.value } : { ...OFF, fade: motionReduced.value };
  return veil(props.stealth, t - veilAt, veilFrom, motionReduced.value);
}
function paintVeil(t: number) {
  veilFrame.value = veilAtTime(t);
  // The shimmer counts from the end of the cast, or from mount when the mode was already on.
  const since = veilAt === -Infinity ? t : t - veilAt - 0.95;
  shine.value = props.stealth && fine.value && !still.value && !motionReduced.value ? shimmer(since) : null;
}
const edge = computed(() => -78 + 156 * veilFrame.value.cloak);
const squash = computed(() => {
  const v = veilFrame.value;
  return v.sy === 1 ? undefined : `translate(0 ${(66 * (1 - v.sy)).toFixed(2)}) scale(${v.sx.toFixed(4)} ${v.sy.toFixed(4)})`;
});
const showVeil = computed(() => veilFrame.value.cloak > 0.001 || veilFrame.value.rim > 0.001 || veilFrame.value.ping >= 0);
const rimWidth = computed(() => (fine.value ? 5 : (2 * 1.3 * 220) / props.size));
function eyeTransform(matrix: string) {
  const k = veilFrame.value.eye;
  return k >= 0.999 ? matrix : `${matrix} scale(1 ${Math.max(0.06, k).toFixed(3)})`;
}
let raf = 0,
  last = 0,
  holdUntil = Infinity,
  mounted = false;
const start = performance.now();
const now = () => (performance.now() - start) / 1000;
const frameMs = () => (props.fps > 0 && props.fps < 30 ? 1000 / props.fps : 1000 / 30) - 0.5;

// A settled instant at least 2.5 s after `t` (morphs and the alert glyph have
// finished) with the eyes open, so a static frame never freezes mid-blink.
function settled(t: number) {
  let s = t + 2.5;
  for (let i = 0; i < 40 && liveliness(s).lid < 0.99; i++) s += 0.05;
  return s;
}
function paint() {
  if (still.value) {
    time.value = 0;
    frame.value = engine.sample(settled(now()));
  } else {
    time.value = now();
    frame.value = engine.sample(time.value);
  }
  paintVeil(now());
}
function pose() {
  const t = now();
  engine.setState(visual.value.engine, t);
  engine.setExpression(EXPRESSION_BY_ID.get(visual.value.expression) || null, t);
}
// Sleep through display frames that would be skipped: wake about one display
// frame before the next sample is due, then let the animation frame align it.
let gap: ReturnType<typeof setTimeout> | null = null;
function next() {
  const wait = frameMs() - (performance.now() - last) - 12;
  if (wait > 4)
    gap = setTimeout(() => {
      gap = null;
      raf = requestAnimationFrame(tick);
    }, wait);
  else raf = requestAnimationFrame(tick);
}
function tick(ts: number) {
  raf = 0;
  if (document.hidden || still.value) return;
  if (ts - last >= frameMs()) {
    frame.value = engine.sample(now());
    time.value = now();
    paintVeil(time.value);
    last = ts;
  }
  if (now() >= holdUntil) return; // hold: this frame stays until something changes
  next();
}
function cancel() {
  if (raf) cancelAnimationFrame(raf);
  raf = 0;
  if (gap !== null) clearTimeout(gap);
  gap = null;
}
function animation() {
  cancel();
  if (mounted && !document.hidden && !still.value) raf = requestAnimationFrame(tick);
}
// Something changed: animate again, and settle a calm pose after HOLD_SECONDS.
function wake() {
  holdUntil = visual.value.hold ? now() + HOLD_SECONDS : Infinity;
  paint();
  animation();
}
watch(
  () => visual.value.id,
  () => {
    pose();
    wake();
  },
);
watch(gaze, (to, from) => {
  if (sameGaze(to, from ?? null)) return;
  engine.setLook(lookFor(to), now());
  if (!still.value) wake();
});
watch(still, () => {
  if (still.value) engine.setLook(null, now());
  wake();
});
// The switch changed: start from wherever the Veil is now and keep frames coming until the cast or release ends.
watch(
  () => props.stealth,
  (on) => {
    const t = now();
    veilFrom = veilAtTime(t);
    veilAt = t;
    if (!still.value) holdUntil = Math.max(holdUntil, t + (on ? ACTIVATE : RELEASE) + 0.1);
    paintVeil(t);
    animation();
  },
);
function claim(on: boolean) {
  if (on && mounted && !slot.value) slot.value = claimAnimation(() => props.rank);
  else if (!on && slot.value) {
    slot.value.release();
    slot.value = null;
  }
}
watch(wants, claim);
watch(() => props.rank, () => slot.value?.update());
watch(() => props.fps, () => animation());
function visibility() {
  if (!document.hidden) wake();
  else animation();
}
function motionPreference() {
  systemReduced.value = systemMotion.matches;
}
onMounted(() => {
  mounted = true;
  pose();
  document.addEventListener("visibilitychange", visibility);
  systemMotion.addEventListener("change", motionPreference);
  claim(wants.value);
  wake();
});
onUnmounted(() => {
  mounted = false;
  cancel();
  claim(false);
  pointer.dispose();
  document.removeEventListener("visibilitychange", visibility);
  systemMotion.removeEventListener("change", motionPreference);
});
</script>
<template>
  <svg
    ref="svg"
    class="avatar"
    :data-emotion="visual.emotion"
    :data-pose="visual.id"
    :data-animated="still ? 'false' : 'true'"
    :data-stealth="stealth ? 'on' : 'off'"
    :width="size"
    :height="size"
    viewBox="-110 -110 220 220"
    aria-hidden="true"
    focusable="false"
  >
    <g v-if="frame.dotsBehind">
      <circle
        v-for="(dot, i) in frame.dots"
        :key="i"
        :cx="dot.x"
        :cy="dot.y"
        :r="dot.r"
        :opacity="dot.opacity"
        fill="currentColor"
      />
    </g>
    <defs v-if="showVeil">
      <clipPath :id="ids.body"><path :d="frame.bodyPath" /></clipPath>
      <clipPath :id="ids.base"><rect x="-110" :y="edge" width="220" :height="Math.max(0, 110 - edge)" /></clipPath>
      <clipPath :id="ids.cloak"><rect x="-110" y="-110" width="220" :height="Math.max(0, edge + 110)" /></clipPath>
      <linearGradient :id="ids.glass" x1=".15" y1="0" x2=".85" y2="1">
        <stop offset="0" stop-color="var(--avatar-stealth-hi)" />
        <stop offset=".75" stop-color="var(--avatar-stealth)" />
      </linearGradient>
      <linearGradient :id="ids.sheen" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0" stop-color="#fff" stop-opacity="0" />
        <stop offset=".5" stop-color="#fff" stop-opacity=".55" />
        <stop offset="1" stop-color="#fff" stop-opacity="0" />
      </linearGradient>
    </defs>
    <g :transform="motion.transform">
      <g :transform="squash">
        <g
          :clip-path="showVeil && !veilFrame.fade ? `url(#${ids.base})` : undefined"
          :opacity="showVeil && veilFrame.fade ? 1 - veilFrame.cloak : undefined"
          class="avatar-base"
        >
          <path
            :d="frame.bodyPath"
            :opacity="frame.bodyAlpha"
            fill="currentColor"
          />
          <path
            v-for="(eye, i) in frame.eyes"
            :key="i"
            :d="eye.d"
            :transform="eyeTransform(eye.matrix)"
            :opacity="eye.alpha"
            fill="var(--avatar-eye)"
          />
        </g>
        <g
          v-if="showVeil"
          class="avatar-veil"
          :clip-path="veilFrame.fade ? undefined : `url(#${ids.cloak})`"
          :opacity="veilFrame.fade ? veilFrame.cloak : undefined"
        >
          <path
            :d="frame.bodyPath"
            :opacity="frame.bodyAlpha"
            :fill="fine ? `url(#${ids.glass})` : 'var(--avatar-stealth)'"
            :fill-opacity="fine ? 1 - (1 - GLASS_OPACITY) * veilFrame.glass : 1"
          />
          <g v-if="fine" :clip-path="`url(#${ids.body})`">
            <ellipse cx="-26" cy="-30" rx="20" ry="9" transform="rotate(-35 -26 -30)" fill="#fff" :opacity="0.2 * veilFrame.glass" />
            <rect
              v-if="shine"
              x="-20"
              y="-140"
              width="40"
              height="280"
              :fill="`url(#${ids.sheen})`"
              :opacity="shine.opacity * veilFrame.glass"
              :transform="`translate(${(-130 + 260 * shine.x).toFixed(1)} 0) rotate(22)`"
            />
          </g>
          <path
            v-for="(eye, i) in frame.eyes"
            :key="i"
            :d="eye.d"
            :transform="eyeTransform(eye.matrix)"
            :opacity="eye.alpha"
            fill="var(--avatar-stealth-eye)"
          />
        </g>
        <g v-if="showVeil" :clip-path="`url(#${ids.body})`">
          <path :d="frame.bodyPath" fill="none" stroke="var(--avatar-stealth-rim)" :stroke-width="rimWidth" :opacity="0.95 * veilFrame.rim" />
          <rect v-if="veilFrame.hem > 0" x="-110" :y="edge - 0.9" width="220" height="1.8" fill="var(--avatar-stealth-rim)" :opacity="0.95 * veilFrame.hem" />
        </g>
      </g>
      <circle
        v-if="fine && veilFrame.ping >= 0"
        r="66"
        fill="none"
        stroke="var(--avatar-stealth-glow)"
        :transform="`scale(${(1 + (16 / 66) * (1 - (1 - veilFrame.ping) ** 5)).toFixed(4)})`"
        :stroke-width="(3.5 - 2.8 * veilFrame.ping).toFixed(2)"
        :opacity="(0.85 * (1 - veilFrame.ping)).toFixed(3)"
      />
    </g>
    <g
      class="avatar-cues"
      fill="currentColor"
      stroke="currentColor"
      stroke-linecap="round"
    >
      <g v-if="motion.cue === 'thought'">
        <circle
          v-for="i in 3"
          :key="i"
          :cx="-18 + i * 10"
          cy="-76"
          :r="2.5 + motion.pulse[i - 1]"
          :opacity="0.35 + 0.65 * motion.pulse[i - 1]"
          stroke="none"
        />
      </g>
      <g
        v-else-if="motion.cue === 'orbit'"
        :transform="`rotate(${still ? -30 : motion.t * 85})`"
      >
        <circle
          v-for="i in 3"
          :key="i"
          :cx="76 * Math.cos(i * 2.094)"
          :cy="76 * Math.sin(i * 2.094)"
          :r="4 - i * 0.6"
          :opacity="1 - i * 0.2"
          stroke="none"
        />
      </g>
      <path
        v-else-if="motion.cue === 'scan'"
        :d="`M -28 ${-76 + (motion.pulse[0] - 0.5) * 6} h 56`"
        fill="none"
        stroke-width="3"
        stroke-dasharray="18 6 6 6"
        opacity=".65"
      />
      <g
        v-else-if="motion.cue === 'spark' || motion.cue === 'celebrate'"
        fill="none"
        stroke-width="2.8"
      >
        <path
          v-for="(p, i) in [
            [-58, -46],
            [57, -48],
            [0, -77],
          ]"
          :key="i"
          :transform="`translate(${p[0]} ${p[1]}) rotate(${(motion.pulse[i] - 0.5) * 40}) scale(${0.75 + 0.25 * motion.pulse[i]})`"
          d="M -5 0 H 5 M 0 -5 V 5"
        />
      </g>
      <path
        v-else-if="motion.cue === 'attention'"
        d="M 0 -81 V -73 M 0 -67 V -66"
        fill="none"
        stroke-width="3.5"
      />
      <path
        v-else-if="motion.cue === 'sleep'"
        d="M 39 -65 h 9 l -9 9 h 9 M 52 -77 h 7 l -7 7 h 7"
        fill="none"
        stroke-width="2.5"
        :opacity="0.4 + 0.4 * motion.pulse[0]"
      />
      <path
        v-else-if="motion.cue === 'sigh'"
        d="M -54 -43 q -5 8 0 12 q 5 -4 0 -12"
        stroke="none"
        opacity=".6"
      />
    </g>
    <g v-if="!frame.dotsBehind">
      <circle
        v-for="(dot, i) in frame.dots"
        :key="i"
        :cx="dot.x"
        :cy="dot.y"
        :r="dot.r"
        :opacity="dot.opacity"
        fill="currentColor"
      />
    </g>
    <circle
      v-if="frame.notif"
      :cx="frame.notif.x"
      :cy="frame.notif.y"
      :r="frame.notif.r"
      fill="var(--accent)"
    />
  </svg>
</template>
<style scoped>
/* Phase 33: under Reduce Motion the Veil cross-fades (0.3 s, ease-in-out) instead of wiping. */
.avatar-veil,
.avatar-base {
  transition: opacity 0.3s cubic-bezier(0.65, 0, 0.35, 1);
}
</style>
