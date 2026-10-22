<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from "vue";
import { avatarMotion } from "../avatarMotion";
import { visualFor } from "../companionState";
import { claimAnimation, lookFor, sameGaze, usePointerGaze, type Gaze } from "../companionPointer";
import { BotEngine } from "../vendor/bloub/engine";
import { liveliness } from "../vendor/bloub/face";
import { SHAPE_BY_ID } from "../vendor/bloub/skins";
import { EXPRESSION_BY_ID } from "../vendor/bloub/expressions";
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
  }>(),
  { state: "idle", size: 80, reduced: false, look: undefined, gaze: true, animate: true, rank: 1, fps: 30 },
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
    <g :transform="motion.transform">
      <path
        :d="frame.bodyPath"
        :opacity="frame.bodyAlpha"
        fill="currentColor"
      />
      <path
        v-for="(eye, i) in frame.eyes"
        :key="i"
        :d="eye.d"
        :transform="eye.matrix"
        :opacity="eye.alpha"
        fill="var(--avatar-eye)"
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
