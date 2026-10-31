<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from "vue";
import Markdown from "./Markdown.vue";
import { formatDuration, formatOffset, groupRows, isLiveState, receipt, rows, type RunActivity } from "../activity";

// What Pebble is doing, or did, for one run (design study §2.3). In `live` mode the main window's
// status line is the toggle and this renders only the disclosed body. Otherwise it is the one-line
// receipt above a reply ("Thought for 11 s", "Worked for 42 s · 6 steps"), which re-expands on
// click; a quick reply with no steps gets no receipt. `full` (the Details drawer) shows the whole
// timeline without the inline height cap. Rows are host display data (activity.ts clamps them) and
// render as text; low-stakes reads and searches fold into one "Explored" row, while retries, waits,
// withdrawn drafts and failures always keep their own rows. The reasoning summary is model-written
// display data, labelled as such, rendered through the sanitizing Markdown renderer and muted. This
// component calls no route: it emits `expand` so the window can read the host record once.
const props = withDefaults(
  defineProps<{
    record: RunActivity | null;
    live?: boolean;
    full?: boolean;
    open?: boolean; // live and full modes: controlled by the parent
    bodyId?: string;
    unavailable?: boolean; // the service has no run_activity route
    reduced?: boolean;
  }>(),
  { live: false, full: false, open: false, bodyId: "", unavailable: false, reduced: false },
);
const emit = defineEmits<{ expand: [runId: string] }>();
const own = ref(false);
const controlled = computed(() => props.live || props.full);
const shown = computed(() => (controlled.value ? props.open : own.value));
const id = computed(() => props.bodyId || "thinking-" + (props.record?.runId || "none"));
const list = computed(() => (props.record ? rows(props.record) : []));
const entries = computed(() => groupRows(list.value));
const running = computed(() => !!props.record && isLiveState(props.record.state));
const title = computed(() => (props.record ? receipt(props.record) : null));
// Phase 24 (V1-095): the completion check's verdict under the receipt, from the host's check row ("Verified · unittest
// 3 passed on current files", "Not verified · 1 changed file, no tests run"). A run that changed no code has none.
const verdict = computed(() => {
  const checks = list.value.filter((item) => item.kind === "check" && /^(?:Verified|Not verified) · /.test(item.label));
  return checks.length ? checks[checks.length - 1].label : null;
});
const empty = computed(() => !list.value.length && !props.record?.reasoning);
const groups = reactive(new Set<number>());
const scroller = ref<HTMLElement | null>(null);

function toggle() {
  own.value = !own.value;
  if (own.value && props.record) emit("expand", props.record.runId);
}
function toggleGroup(key: number) {
  if (groups.has(key)) groups.delete(key);
  else groups.add(key);
}
// The live body follows new text while it is open, unless the person scrolled up.
let pinned = true;
function scrolled() {
  const el = scroller.value;
  if (el) pinned = el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
}
watch(
  () => [props.record?.reasoning.length, list.value.length, shown.value],
  async () => {
    if (!props.live || !shown.value || !pinned) return;
    await nextTick();
    const el = scroller.value;
    if (el) el.scrollTop = el.scrollHeight;
  },
);
// A controlled body is mounted open: ask once, as it appears, to catch up with steps already taken.
watch(
  () => props.open,
  (open) => {
    if (controlled.value && open && props.record) emit("expand", props.record.runId);
  },
  { immediate: true },
);
// A step the host left running in a run that has ended is shown as unfinished, never as live.
const phase = (status: string) => (status === "running" ? (running.value ? "running" : "unfinished") : status);
const mark = (status: string) => ({ failed: "Failed", running: "In progress", unfinished: "Unfinished" } as Record<string, string>)[phase(status)] || "Done";
const took = (duration: number | null) => (duration !== null && duration >= 1 ? formatDuration(duration) : null);
</script>

<template>
  <div v-if="record && (controlled || title)" class="thinking" :class="{ live, full, reduced }">
    <button
      v-if="!controlled"
      type="button"
      class="thinking-summary"
      :aria-expanded="own"
      :aria-controls="id"
      @click="toggle"
    >
      <span class="thinking-title">{{ title }}</span>
      <span class="thinking-caret" :class="{ open: own }" aria-hidden="true"></span>
    </button>
    <p v-if="!controlled && verdict" class="thinking-verdict" :class="{ unverified: !verdict.startsWith('Verified') }">{{ verdict }}</p>
    <!-- Phase 46 (1.9.29): a window may draw the body itself (the work view's cards) under this receipt -->
    <div v-if="shown && $slots.body" :id="id" class="thinking-cards"><slot name="body" /></div>
    <div v-else-if="shown" :id="id" ref="scroller" class="thinking-body" @scroll="scrolled">
      <ol v-if="list.length" class="thinking-steps" :aria-label="running ? 'What Pebble is doing' : 'What Pebble did'">
        <li v-if="record.dropped" class="thinking-more">{{ record.dropped }} earlier {{ record.dropped === 1 ? "step is" : "steps are" }} not shown.</li>
        <template v-for="entry in entries" :key="entry.type === 'group' ? 'g' + entry.key : entry.item.seq">
          <li v-if="entry.type === 'group'" class="thinking-step thinking-group done">
            <span class="thinking-mark" role="img" aria-label="Done"></span>
            <button type="button" class="thinking-fold" :aria-expanded="groups.has(entry.key)" @click="toggleGroup(entry.key)">
              {{ entry.label }}<span class="thinking-caret" :class="{ open: groups.has(entry.key) }" aria-hidden="true"></span>
            </button>
            <span class="thinking-time">{{ formatOffset(entry.items[0].at) }}</span>
            <ol v-if="groups.has(entry.key)" class="thinking-folded">
              <li v-for="item in entry.items" :key="item.seq">
                <span class="thinking-label">{{ item.label }}</span>
                <span class="thinking-time">{{ formatOffset(item.at) }}</span>
              </li>
            </ol>
          </li>
          <li v-else class="thinking-step" :class="[entry.item.kind, phase(entry.item.status)]">
            <span class="thinking-mark" role="img" :aria-label="mark(entry.item.status)"></span>
            <span class="thinking-label"
              >{{ entry.item.label
              }}<span v-if="took(entry.item.duration) && entry.item.status !== 'running'" class="thinking-took">
                · {{ took(entry.item.duration) }}</span
              ></span
            >
            <span class="thinking-time">{{ formatOffset(entry.item.at) }}</span>
            <details v-if="entry.item.detail" class="thinking-detail">
              <summary>Details</summary>
              <p>{{ entry.item.detail }}</p>
            </details>
          </li>
        </template>
      </ol>
      <section v-if="record.reasoning" class="thinking-reasoning" :aria-labelledby="id + '-reasoning'">
        <p :id="id + '-reasoning'" class="thinking-reasoning-head">Pebble's reasoning, summarised by the model</p>
        <Markdown :text="record.reasoning" />
        <p v-if="record.reasoningClipped" class="thinking-note">The summary is longer than this view keeps; the rest is not shown.</p>
      </section>
      <p v-if="empty" class="thinking-note">
        {{ running ? "Pebble hasn't reported a step yet." : unavailable || record.error ? "This service doesn't keep step details for this reply." : "No step details were recorded for this reply." }}
      </p>
      <p v-if="record.error" class="thinking-note">Details are unavailable: {{ record.error }}</p>
    </div>
  </div>
</template>

<style scoped>
.thinking {
  min-width: 0;
  font-size: 12px;
  color: var(--secondary);
}
.thinking-summary {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin: 0 0 6px -7px;
  padding: 3px 7px;
  border-radius: var(--radius-sm, 6px);
  color: var(--muted);
  font-size: 12px;
}
.thinking-summary:hover,
.thinking-summary[aria-expanded="true"] {
  color: var(--secondary);
  background: var(--hover);
}
.thinking-verdict {
  margin: -4px 0 6px;
  font-size: 11px;
  color: var(--muted);
}
.thinking-verdict.unverified {
  color: var(--secondary);
}
.thinking-caret {
  display: inline-block;
  flex: none;
  width: 6px;
  height: 6px;
  border-right: 1.5px solid currentColor;
  border-bottom: 1.5px solid currentColor;
  transform: rotate(-45deg);
  transition: transform 0.15s ease;
}
.thinking-caret.open {
  transform: rotate(45deg) translate(-1px, -1px);
}
.thinking-body {
  /* Inline the timeline stays short; the Details drawer shows all of it. */
  max-height: 240px;
  overflow: auto;
  margin: 2px 0 14px;
  padding: 2px 4px 2px 12px;
  border-left: 2px solid var(--line-strong);
  scrollbar-width: thin;
  mask-image: linear-gradient(to bottom, transparent 0, #000 8px, #000 calc(100% - 12px), transparent 100%);
}
.live .thinking-body {
  margin: 6px 0 16px 10px;
}
.thinking-cards {
  margin: 2px 0 14px;
}
.full .thinking-body {
  max-height: none;
  mask-image: none;
  margin: 0;
}
.thinking-steps,
.thinking-folded {
  list-style: none;
  margin: 0;
  padding: 0;
}
.thinking-step {
  display: grid;
  grid-template-columns: 12px minmax(0, 1fr) auto;
  align-items: baseline;
  column-gap: 8px;
  padding: 3px 0;
  line-height: 1.55;
}
.thinking-label {
  overflow-wrap: anywhere;
}
.thinking-took {
  color: var(--muted);
}
.thinking-step.running .thinking-label {
  color: var(--text);
}
.thinking-step.failed .thinking-label {
  color: var(--danger);
}
.thinking-mark {
  width: 6px;
  height: 6px;
  margin: 0 3px;
  border-radius: 50%;
  background: var(--line-strong);
  align-self: center;
}
.thinking-step.done .thinking-mark {
  background: var(--accent-soft);
}
.thinking-step.running .thinking-mark {
  background: var(--accent);
  animation: thinking-breathe 2s ease-in-out infinite alternate;
}
.thinking-step.failed .thinking-mark {
  background: var(--danger);
}
.thinking-step.unfinished .thinking-mark {
  background: transparent;
  box-shadow: inset 0 0 0 1.5px var(--muted);
}
.thinking-fold {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0;
  color: inherit;
  text-align: left;
}
.thinking-fold:hover {
  color: var(--text);
}
.thinking-folded {
  grid-column: 2 / 4;
  margin-top: 2px;
}
.thinking-folded li {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 1px 0;
  color: var(--muted);
}
.thinking-time {
  color: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.thinking-detail {
  grid-column: 2 / 4;
  font-size: 11px;
  color: var(--muted);
}
.thinking-detail summary {
  cursor: pointer;
  width: fit-content;
}
.thinking-detail p {
  margin: 4px 0 2px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.thinking-more,
.thinking-note {
  color: var(--muted);
  font-size: 11px;
  margin: 4px 0;
}
.thinking-reasoning {
  margin-top: 8px;
  color: var(--muted);
  font-style: italic;
}
.thinking-reasoning-head {
  margin: 0 0 2px;
  font-size: 11px;
  font-style: normal;
  color: var(--muted);
}
.thinking-reasoning :deep(.markdown) {
  font-size: 12px;
  line-height: 1.7;
  color: var(--secondary);
}
.thinking-reasoning :deep(.markdown strong) {
  font-weight: 560;
  font-style: normal;
  color: var(--text);
}
/* One slow breath every 4 s, as the status dot; none under reduced motion. */
@keyframes thinking-breathe {
  from {
    opacity: 0.45;
  }
  to {
    opacity: 1;
  }
}
.reduced .thinking-step.running .thinking-mark {
  animation: none;
}
.reduced .thinking-caret {
  transition: none;
}
@media (prefers-reduced-motion: reduce) {
  .thinking-step.running .thinking-mark {
    animation: none;
  }
  .thinking-caret {
    transition: none;
  }
}
</style>
