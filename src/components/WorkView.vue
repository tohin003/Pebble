<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch } from "vue";
import { formatDuration, formatOffset, isLiveState, rows, settledSeconds, type ActivityItem, type RunActivity } from "../activity";
import { parsePlan, runWord, summaryOf, summaryWords, workEntries, type Summary } from "../workView";
import BranchCard, { type GitAccess } from "./BranchCard.vue";
import WorkCard from "./WorkCard.vue";

// Phase 46 (1.9.29, D206): the work view. The person asked to see what Pebble is doing as its working window, not as
// rows of text: a header (the task, a ring from the plan's steps, the state word and the time), the plan rail from
// UpdatePlan, then a card per kind of step (reading, edits, commands, tests, Git, app windows, agents, checks) and,
// once the run settles, its summary. Items arrive with views the host built from its own records (no model tokens);
// an item without a usable view, or with a type this window does not know, shows as today's row of text. The
// "Work view" setting switches the windows back to the rows (ThinkingDisclosure). Like that disclosure this calls no
// route itself: it emits `expand` so the window reads the host record, and the branch card reads Git only through the
// functions the window passes in. StatusLine stays the one live region; cards update in place, unannounced.
const props = withDefaults(
  defineProps<{
    record: RunActivity | null;
    task?: string | null;
    full?: boolean;
    reduced?: boolean;
    unavailable?: boolean;
    chatId?: string;
    git?: GitAccess | null;
  }>(),
  { task: null, full: false, reduced: false, unavailable: false, chatId: "", git: null },
);
const emit = defineEmits<{ expand: [runId: string] }>();
const running = computed(() => !!props.record && isLiveState(props.record.state));
const list = computed<ActivityItem[]>(() => (props.record ? rows(props.record) : []));
const entries = computed(() => workEntries(list.value));
const plan = computed(() => parsePlan(props.record?.plan) || parsePlan((props.record?.header as Record<string, unknown> | undefined)?.plan));
const groups = reactive(new Set<number>());
// the time: the host's for a settled run; while live, from the host's start, ticking once a second
const tick = ref(Date.now());
let timer = 0;
watch(running, (live) => {
  window.clearInterval(timer);
  if (live) timer = window.setInterval(() => (tick.value = Date.now()), 1000);
}, { immediate: true });
onUnmounted(() => window.clearInterval(timer));
const seconds = computed(() => {
  const r = props.record;
  if (!r) return null;
  if (!running.value) return settledSeconds(r);
  const start = r.hostStart ?? r.startedAt;
  return start === null ? null : Math.max(0, (tick.value - start) / 1000);
});
const word = computed(() => (props.record ? runWord(props.record.state) : ""));
const meta = computed(() => [word.value, formatDuration(seconds.value), plan.value ? `${plan.value.done} of ${plan.value.total} steps` : ""].filter(Boolean).join(" · "));
// the ring: the share of plan steps done; without a plan, a full ring once settled and an open one while live
const RADIUS = 9, CIRCLE = 2 * Math.PI * RADIUS;
const share = computed(() => (plan.value ? plan.value.done / plan.value.total : running.value ? 0.25 : 1));
const ringLabel = computed(() => (plan.value ? `${plan.value.done} of ${plan.value.total} plan steps done` : running.value ? "In progress" : word.value));
const STEP_WORDS: Record<string, string> = { completed: "Done", in_progress: "In progress", blocked: "Blocked", pending: "Not started" };
// the summary: the host's once read, else the same receipt worked out from the items this window holds
const summary = computed<Summary | null>(() => {
  const r = props.record;
  if (!r || running.value || ["waiting_approval", "waiting_input"].includes(r.state)) return null;
  const host = r.summary as Summary | undefined;
  if (host && typeof host === "object" && typeof host.seconds === "number") return host;
  const usage = r.usage && typeof r.usage === "object" ? (r.usage as Record<string, unknown>) : null;
  return list.value.some((i) => (i as ActivityItem & { view?: unknown }).view) ? summaryOf(list.value, usage, settledSeconds(r) ?? 0) : null;
});
const chips = computed(() => (summary.value ? summaryWords(summary.value) : []));
const hasGit = computed(() => entries.value.some((e) => e.type === "card" && e.kind === "git"));
const taskDiff = computed(() => (summary.value?.files ? { added: summary.value.files.added, removed: summary.value.files.removed } : null));
const phase = (status: string) => (status === "running" ? (running.value ? "running" : "unfinished") : status);
const mark = (status: string) => ({ failed: "Failed", running: "In progress", unfinished: "Unfinished" } as Record<string, string>)[phase(status)] || "Done";
function toggleGroup(key: number) {
  if (groups.has(key)) groups.delete(key);
  else groups.add(key);
}
// mounted open, as the disclosure's controlled body: ask once to catch up with steps already taken
watch(() => props.record?.runId, (run) => run && emit("expand", run), { immediate: true });
</script>

<template>
  <section v-if="record" class="work" :class="{ full, reduced }" :aria-label="running ? 'What Pebble is doing' : 'What Pebble did'">
    <header class="work-head">
      <svg class="ring" width="24" height="24" viewBox="0 0 24 24" role="img" :aria-label="ringLabel">
        <circle class="ring-track" cx="12" cy="12" :r="RADIUS" />
        <circle class="ring-fill" :class="{ live: running && !plan }" cx="12" cy="12" :r="RADIUS" :stroke-dasharray="CIRCLE" :stroke-dashoffset="CIRCLE * (1 - share)" transform="rotate(-90 12 12)" />
      </svg>
      <div class="work-task">
        <p class="work-title">{{ task || "This task" }}</p>
        <p class="work-meta">{{ meta }}</p>
      </div>
    </header>
    <ol v-if="plan" class="work-rail" aria-label="Plan">
      <li v-for="(step, i) in plan.steps" :key="i" :class="step.status">
        <span class="rail-mark" role="img" :aria-label="STEP_WORDS[step.status]"></span><span class="rail-title">{{ step.title }}</span>
      </li>
    </ol>
    <ol v-if="entries.length" class="work-list" :aria-label="running ? 'Steps so far' : 'Steps'">
      <li v-if="record.dropped" class="work-note">{{ record.dropped }} earlier {{ record.dropped === 1 ? "step is" : "steps are" }} not shown.</li>
      <template v-for="entry in entries" :key="entry.type === 'row' ? 'r' + entry.item.seq : entry.type + entry.key">
        <li v-if="entry.type === 'card'" class="work-entry"><WorkCard :card="entry" :running="running" :reduced="reduced" /></li>
        <li v-else-if="entry.type === 'group'" class="work-row done">
          <span class="row-mark" role="img" aria-label="Done"></span>
          <button type="button" class="row-fold" :aria-expanded="groups.has(entry.key)" @click="toggleGroup(entry.key)">{{ entry.label }}</button>
          <span class="row-time">{{ formatOffset(entry.items[0].at) }}</span>
          <ol v-if="groups.has(entry.key)" class="row-folded">
            <li v-for="item in entry.items" :key="item.seq">{{ item.label }}</li>
          </ol>
        </li>
        <li v-else class="work-row" :class="[entry.item.kind, phase(entry.item.status)]">
          <span class="row-mark" role="img" :aria-label="mark(entry.item.status)"></span>
          <span class="row-label">{{ entry.item.label }}</span>
          <span class="row-time">{{ formatOffset(entry.item.at) }}</span>
          <details v-if="entry.item.detail" class="row-detail"><summary>Details</summary><p>{{ entry.item.detail }}</p></details>
        </li>
      </template>
    </ol>
    <p v-else class="work-note">{{ running ? "Pebble hasn't reported a step yet." : unavailable || record.error ? "This service doesn't keep step details for this reply." : "No step details were recorded for this reply." }}</p>
    <BranchCard v-if="hasGit && git && chatId" :chat-id="chatId" :git="git" :settled="!running" :task-diff="taskDiff" />
    <section v-if="chips.length" class="work-summary" aria-label="Summary">
      <h4 class="summary-title">{{ word }}</h4>
      <ul class="summary-chips"><li v-for="chip in chips" :key="chip">{{ chip }}</li></ul>
    </section>
    <p v-if="record.error" class="work-note">Details are unavailable: {{ record.error }}</p>
  </section>
</template>

<style scoped>
.work {
  display: grid;
  gap: 10px;
  min-width: 0;
  margin: 6px 0 16px;
  font-size: var(--text-sm);
  color: var(--secondary);
}
.work:not(.full) .work-list {
  max-height: 420px;
  overflow: auto;
  scrollbar-width: thin;
}
.work-head {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.ring {
  flex: none;
}
.ring-track {
  fill: none;
  stroke: var(--line-strong);
  stroke-width: 3;
}
.ring-fill {
  fill: none;
  stroke: var(--accent);
  stroke-width: 3;
  stroke-linecap: round;
  transition: stroke-dashoffset 0.3s ease;
}
.ring-fill.live {
  transform-origin: 12px 12px;
  animation: ring-turn 1.6s linear infinite;
}
@keyframes ring-turn {
  from {
    transform: rotate(-90deg);
  }
  to {
    transform: rotate(270deg);
  }
}
.work-task {
  min-width: 0;
}
.work-title {
  margin: 0;
  font-size: var(--text-md);
  font-weight: 560;
  color: var(--text);
  overflow-wrap: anywhere;
}
.work-meta {
  margin: 0;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.work-rail {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.work-rail li {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
}
.rail-title {
  overflow-wrap: anywhere;
}
.work-rail li.completed .rail-title {
  color: var(--muted);
}
.work-rail li.in_progress .rail-title {
  color: var(--text);
}
.rail-mark {
  flex: none;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  border: 1.5px solid var(--line-strong);
}
.completed .rail-mark {
  background: var(--ok);
  border-color: var(--ok);
}
.in_progress .rail-mark {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.blocked .rail-mark {
  border-color: var(--warn);
}
.work-list {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.work-entry {
  min-width: 0;
}
.work-row {
  display: grid;
  grid-template-columns: 12px minmax(0, 1fr) auto;
  align-items: baseline;
  column-gap: 8px;
  padding: 0 2px;
  line-height: 1.55;
}
.row-label {
  overflow-wrap: anywhere;
}
.work-row.failed .row-label {
  color: var(--danger);
}
.work-row.running .row-label {
  color: var(--text);
}
.row-mark {
  width: 6px;
  height: 6px;
  margin: 0 3px;
  border-radius: 50%;
  background: var(--accent-soft);
  align-self: center;
}
.work-row.failed .row-mark {
  background: var(--danger);
}
.work-row.running .row-mark {
  background: var(--accent);
}
.work-row.unfinished .row-mark {
  background: transparent;
  box-shadow: inset 0 0 0 1.5px var(--muted);
}
.row-fold {
  padding: 0;
  color: inherit;
  text-align: left;
}
.row-fold:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}
.row-time {
  color: var(--muted);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.row-folded,
.row-detail {
  grid-column: 2 / 4;
  margin: 2px 0 0;
  padding: 0;
  list-style: none;
  color: var(--muted);
  font-size: var(--text-xs);
}
.row-detail p {
  margin: 4px 0 2px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.work-summary {
  display: grid;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--soft);
}
.summary-title {
  margin: 0;
  font-size: var(--text-md);
  font-weight: 560;
  color: var(--text);
}
.summary-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-variant-numeric: tabular-nums;
}
.work-note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
}
.reduced .ring-fill,
.reduced .ring-fill.live {
  transition: none;
  animation: none;
}
@media (prefers-reduced-motion: reduce) {
  .ring-fill,
  .ring-fill.live {
    transition: none;
    animation: none;
  }
}
</style>
