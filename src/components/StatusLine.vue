<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  COALESCE_MS,
  FINISHED_LABEL,
  LIVE_KEYS,
  ROTATE_MS,
  STATUS_WORDS,
  announcer,
  coalesce,
  elapsedChangeIn,
  exactChangeIn,
  formatElapsed,
  formatExactElapsed,
  isStatusKey,
  statusLabel,
  statusWord,
  type StatusKey,
} from "../statusWords";

// The companion's status line (design study §2.3, §2.5–§2.6). `status` is the
// pose's key from companionState.ts, which the host's own events decide; this
// component never reads progress labels, paths or model text. A screen reader
// hears one stable sentence per key ("Pebble is testing"), at most once every
// 10 s, and "Reply finished" when a reply completes; the rotating synonyms and
// the elapsed time are aria-hidden. Visual updates are coalesced to 4 Hz. Stop
// is ordinary host chrome that only emits `stop`.
//
// Phase 22: the `companion` slot replaces the dot (the main window puts its
// Avatar there; it takes part in the window's one-animated-avatar election),
// and with `expandable` the visible words become one button that discloses the
// live activity timeline (it only emits `toggle`). The optional `detail` slot
// is secondary text after the word (the parent passes the host's latest
// reasoning headline or step label); the word itself is always the key's, the
// live region still says only the key's stable sentence, and this component
// still takes no label, tool or path. `exact` means `startedAt` is the host's
// own run start: the time is then shown to the second and without "~".
//
// Phase 24 (V1-118, V1-159): "Reply finished" is said at once, outside the
// 10 s window, and nothing clears it afterwards; the next turn's first status
// empties the region first, so a second quick reply is a change again. With
// `grace` (ms) an activity word appears no sooner than that after the status
// line came on ("Sending" still shows at once), so a quick reply never
// flashes "Thinking".
const props = withDefaults(
  defineProps<{
    status?: string | null;
    startedAt?: number | null; // first host `running` event, from the pose (or the host's start when `exact`)
    exact?: boolean;
    attempt?: number | null;
    mode?: string; // companion.status_words: rotate | fixed | off
    stoppable?: boolean;
    outcome?: string | null; // pose.outcome
    reduced?: boolean;
    pill?: boolean; // the collapsed-active pet's compact status pill
    expandable?: boolean; // the words open a disclosure (main window)
    expanded?: boolean;
    controls?: string; // id of the disclosed region
    grace?: number; // ms before the first activity word (Phase 24, V1-159); 0 shows it at once
  }>(),
  { status: null, startedAt: null, exact: false, attempt: null, mode: "rotate", stoppable: false, outcome: null, reduced: false, pill: false, expandable: false, expanded: false, controls: "", grace: 0 },
);
const emit = defineEmits<{ stop: []; toggle: [] }>();

const shown = ref<StatusKey | null>(null);
const shownAttempt = ref<number | null>(null);
const announced = ref("");
const clock = ref(Date.now());
const rotateFrom = ref(Date.now());
let mounted = false,
  timer: ReturnType<typeof setTimeout> | null = null;

const speaker = announcer((text) => (announced.value = text));
const sync = coalesce<{ key: StatusKey | null; attempt: number | null }>((value) => {
  const t = Date.now();
  if (value.key !== shown.value) rotateFrom.value = t;
  shown.value = value.key;
  shownAttempt.value = value.attempt;
  clock.value = t;
  if (value.key) {
    if (speaker.current === FINISHED_LABEL) speaker.clear(); // a new turn: the next "Reply finished" is a change
    speaker.say(statusLabel(value.key));
  } else if (speaker.current !== FINISHED_LABEL) speaker.clear();
  schedule();
}, COALESCE_MS);
// The grace before the first activity word (V1-159): counted from the moment the status line came on.
let since: number | null = null,
  graceTimer: ReturnType<typeof setTimeout> | null = null,
  held: { key: StatusKey | null; attempt: number | null } | null = null;
function dropGrace() {
  if (graceTimer !== null) clearTimeout(graceTimer);
  graceTimer = null;
  held = null;
}
function offer(value: { key: StatusKey | null; attempt: number | null }) {
  if (value.key === null) {
    since = null;
    dropGrace();
    sync.push(value);
    return;
  }
  const t = Date.now();
  if (since === null) since = t;
  // Only the first activity word waits (nothing, or only "Sending", shows yet); host states never wait.
  const first = shown.value === null || shown.value === "sending";
  const wait = props.grace > 0 && first && LIVE_KEYS.has(value.key) ? props.grace - (t - since) : 0;
  if (wait > 0) {
    held = value;
    if (graceTimer === null)
      graceTimer = setTimeout(() => {
        graceTimer = null;
        const next = held;
        held = null;
        if (next) sync.push(next);
      }, wait);
    return;
  }
  dropGrace();
  sync.push(value);
}

const visible = computed(() => !!shown.value && props.mode !== "off");
const word = computed(() =>
  shown.value
    ? statusWord(shown.value, Math.floor((clock.value - rotateFrom.value) / ROTATE_MS), props.mode === "rotate", shownAttempt.value)
    : "",
);
const elapsed = computed(() =>
  visible.value && typeof props.startedAt === "number"
    ? (props.exact ? formatExactElapsed : formatElapsed)(clock.value - props.startedAt)
    : null,
);
const elapsedText = computed(() => (elapsed.value ? (props.exact ? elapsed.value : "~" + elapsed.value) : ""));
const elapsedTitle = computed(() =>
  props.exact ? "Time since Pebble started this task" : "Approximate time since Pebble started this task",
);

function schedule() {
  if (timer !== null) clearTimeout(timer);
  timer = null;
  if (!mounted || !visible.value || document.hidden) return;
  const t = Date.now();
  const waits: number[] = [];
  if (props.mode === "rotate" && STATUS_WORDS[shown.value!].length > 1)
    waits.push(ROTATE_MS - ((t - rotateFrom.value) % ROTATE_MS));
  if (typeof props.startedAt === "number") waits.push((props.exact ? exactChangeIn : elapsedChangeIn)(t - props.startedAt));
  if (!waits.length) return;
  timer = setTimeout(() => {
    timer = null;
    clock.value = Date.now();
    schedule();
  }, Math.max(COALESCE_MS, Math.min(...waits)));
}

watch(
  () => [props.status, props.attempt] as const,
  ([status, attempt]) =>
    offer({
      key: isStatusKey(status) ? status : null,
      attempt: Number.isInteger(attempt) ? (attempt as number) : null,
    }),
  { immediate: true },
);
watch(
  () => props.outcome,
  (outcome, previous) => {
    if (outcome === "completed" && previous !== "completed") speaker.now(FINISHED_LABEL);
  },
);
watch(
  () => [props.mode, props.startedAt, props.exact],
  () => {
    clock.value = Date.now(); // a new start (the host's clock arriving) shows at once
    schedule();
  },
);
const visibility = () => {
  clock.value = Date.now();
  schedule();
};
onMounted(() => {
  mounted = true;
  document.addEventListener("visibilitychange", visibility);
  schedule();
});
onUnmounted(() => {
  mounted = false;
  sync.cancel();
  dropGrace();
  speaker.cancel();
  if (timer !== null) clearTimeout(timer);
  timer = null;
  document.removeEventListener("visibilitychange", visibility);
});
</script>

<template>
  <div class="status-line" :class="{ quiet: !visible, reduced, pill, expandable }">
    <p class="status-text" role="status" aria-live="polite" aria-atomic="true">
      <span class="status-sr">{{ announced }}</span>
      <template v-if="visible && !expandable">
        <slot name="companion"><span class="status-dot" aria-hidden="true"></span></slot>
        <span class="status-word" aria-hidden="true">{{ word }}</span>
        <span v-if="$slots.detail" class="status-detail" aria-hidden="true"><slot name="detail" /></span>
        <span
          v-if="elapsed"
          class="status-elapsed"
          aria-hidden="true"
          :title="elapsedTitle"
          >{{ elapsedText }}</span
        >
      </template>
    </p>
    <button
      v-if="visible && expandable"
      type="button"
      class="status-toggle"
      :aria-expanded="expanded"
      :aria-controls="controls || undefined"
      aria-label="What Pebble is doing"
      @click="emit('toggle')"
    >
      <slot name="companion"><span class="status-dot" aria-hidden="true"></span></slot>
      <span class="status-word" aria-hidden="true">{{ word }}</span>
      <span v-if="$slots.detail" class="status-detail" aria-hidden="true"><slot name="detail" /></span>
      <span
        v-if="elapsed"
        class="status-elapsed"
        aria-hidden="true"
        :title="elapsedTitle"
        >{{ elapsedText }}</span
      >
      <span class="status-caret" :class="{ open: expanded }" aria-hidden="true"></span>
    </button>
    <button
      v-if="stoppable"
      type="button"
      class="status-stop"
      aria-label="Stop task"
      @click="emit('stop')"
    >
      Stop
    </button>
  </div>
</template>

<style scoped>
.status-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  min-height: 22px;
  color: var(--muted);
  font-size: 12px;
}
.status-text {
  display: flex;
  align-items: baseline;
  gap: 7px;
  min-width: 0;
  margin: 0;
}
.status-sr {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
.status-dot {
  width: 6px;
  height: 6px;
  flex: none;
  align-self: center;
  border-radius: 50%;
  background: var(--accent);
  animation: status-breathe 2s ease-in-out infinite alternate;
}
.status-word {
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* Phase 22: secondary text (headline or step) gives way first when space is short. */
.status-detail {
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--secondary);
}
.status-detail::before,
.expandable .status-elapsed::before {
  content: "·";
  margin-right: 7px;
  color: var(--muted);
}
.status-elapsed {
  flex: none;
  font-variant-numeric: tabular-nums;
}
/* Phase 24 (V1-160): a 28 px target. */
.status-stop {
  flex: none;
  min-height: 28px;
  padding: 4px 10px;
  border-radius: 6px;
  color: var(--text);
  background: var(--hover);
  font-size: 12px;
}
.status-stop:hover {
  background: var(--selected);
}
/* Phase 22: the words as one quiet disclosure button, with the companion in place of the dot. */
.status-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  max-width: 100%;
  margin-left: -6px;
  padding: 3px 8px 3px 6px;
  border-radius: 8px;
  color: inherit;
  text-align: left;
}
.status-toggle:hover {
  background: var(--hover);
}
.status-toggle:hover .status-caret,
.status-toggle:focus-visible .status-caret {
  opacity: 1;
}
.status-caret {
  flex: none;
  width: 6px;
  height: 6px;
  margin-left: 1px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  transform: rotate(-45deg);
  opacity: 0.6;
  transition: transform 0.15s ease;
}
.status-caret.open {
  transform: rotate(45deg) translate(-1px, -1px);
}
.expandable .status-word {
  flex: none;
  font-weight: 500;
}
.status-line :deep(.avatar) {
  align-self: center;
}
.pill {
  max-width: 100%;
  padding: 3px 9px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  box-shadow: 0 2px 8px #0002;
  font-size: 11px;
}
.pill.quiet {
  visibility: hidden;
}
/* One slow breath every 4 s (0.25 Hz): well under three luminance changes a second. */
@keyframes status-breathe {
  from {
    opacity: 0.45;
  }
  to {
    opacity: 1;
  }
}
.reduced .status-dot {
  animation: none;
}
.reduced .status-caret {
  transition: none;
}
@media (prefers-reduced-motion: reduce) {
  .status-dot {
    animation: none;
  }
  .status-caret {
    transition: none;
  }
}
</style>
