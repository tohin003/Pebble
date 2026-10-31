<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { clampText } from "../activity";
import { age, lanes, parseHistory, type Commit } from "../workView";

// Phase 46 (1.9.29, D206): the branch card, the reel's merge card adapted to Pebble's local Git. Pebble has no hosted
// pull requests (its reviewed pushes go to repositories on this Mac), so the card shows the current branch: its
// upstream, ahead and behind, uncommitted changes, this task's diff size and a small commit graph with lanes. It reads
// only while it is shown, through the window's own read routes (git_status, git_history: confined Git, no network),
// so a run without a window (a benchmark) never pays for it. Merging is a button that asks Pebble to prepare a merge
// plan (git_plan_propose), which lands on the usual review card: the card never merges by itself. Branch names,
// subjects and authors are repository data: shown as text, never as a button's label.
export interface GitAccess {
  read: (method: "git_status" | "git_history", params: Record<string, unknown>) => Promise<unknown>;
  propose: (params: { operation: "merge"; name: string }) => Promise<unknown>;
}
const props = withDefaults(defineProps<{ chatId: string; git: GitAccess; settled?: boolean; taskDiff?: { added: number; removed: number } | null }>(), { settled: false, taskDiff: null });
const loading = ref(true), error = ref(""), notice = ref(""), busy = ref("");
const status = ref<Record<string, unknown> | null>(null);
const history = ref<Commit[]>([]);
let alive = true;
onUnmounted(() => (alive = false));
const str = (v: unknown, max = 120) => clampText(v, max);
const whole = (v: unknown) => (typeof v === "number" && Number.isSafeInteger(v) && v >= 0 ? v : null);
const failure = (cause: unknown) => clampText(cause instanceof Error ? cause.message : String(cause), 200) || "Pebble couldn't read Git here.";

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const [found, log] = await Promise.all([props.git.read("git_status", { id: props.chatId, limit: 1 }), props.git.read("git_history", { id: props.chatId, limit: 12 })]);
    if (!alive) return;
    status.value = found && typeof found === "object" ? (found as Record<string, unknown>) : null;
    history.value = parseHistory(log);
  } catch (cause) {
    if (alive) error.value = failure(cause);
  } finally {
    if (alive) loading.value = false;
  }
}
onMounted(load);

const branch = computed(() => str(status.value?.branch));
const upstream = computed(() => str(status.value?.upstream));
const ahead = computed(() => whole(status.value?.ahead));
const behind = computed(() => whole(status.value?.behind));
const changed = computed(() => {
  const counts = status.value?.counts as Record<string, unknown> | undefined;
  return counts ? ["staged", "unstaged", "untracked"].reduce((t, k) => t + (whole(counts[k]) || 0), 0) : null;
});
const conflicts = computed(() => whole((status.value?.counts as Record<string, unknown> | undefined)?.unmerged) || 0);
// other local branches, at most three (the complexity budget), as merge candidates into the current one
const others = computed(() => {
  const refs = status.value?.refs as Record<string, unknown> | undefined;
  const list = Array.isArray(refs?.branches) ? (refs!.branches as unknown[]) : [];
  return list
    .map((b) => (b && typeof b === "object" ? str((b as Record<string, unknown>).name, 120) : ""))
    .filter((name) => name && name !== branch.value)
    .slice(0, 3);
});
const rows = computed(() => lanes(history.value, 4));
const width = computed(() => 12 + Math.max(1, ...rows.value.map((r) => r.lanes)) * 12);
const x = (lane: number) => 8 + lane * 12;
const ROW = 22;
const now = Date.now();

async function merge(name: string) {
  if (busy.value || !props.settled) return;
  busy.value = name;
  notice.value = error.value = "";
  try {
    await props.git.propose({ operation: "merge", name });
    if (alive) notice.value = "Pebble prepared the merge for your review. It is with your other cards to review.";
  } catch (cause) {
    if (alive) error.value = failure(cause);
  } finally {
    if (alive) busy.value = "";
  }
}
</script>

<template>
  <article class="work-card branch-card" aria-label="Branch">
    <header class="card-head">
      <h4 class="card-title">Branch</h4>
      <button type="button" class="card-more" :disabled="loading" @click="load">Refresh</button>
    </header>
    <p v-if="loading && !status" class="card-line muted">Reading Git…</p>
    <p v-else-if="error && !status" class="card-line muted">{{ error }}</p>
    <template v-if="status">
      <ul class="chips" aria-label="This branch">
        <li class="chip"><span class="chip-text">{{ branch || "No branch (detached)" }}</span></li>
        <li v-if="upstream" class="chip"><span class="chip-note">tracks</span><span class="chip-text">{{ upstream }}</span></li>
        <li v-if="ahead !== null" class="chip"><span class="chip-text">{{ ahead }} ahead · {{ behind ?? 0 }} behind</span></li>
        <li v-if="changed" class="chip"><span class="chip-text">{{ changed }} uncommitted</span></li>
        <li v-if="taskDiff" class="chip"><span class="chip-note">this task</span><span class="plus">+{{ taskDiff.added }}</span><span class="minus">−{{ taskDiff.removed }}</span></li>
      </ul>
      <p v-if="conflicts" class="card-line warn">{{ conflicts }} {{ conflicts === 1 ? "file conflicts" : "files conflict" }}; resolve them before a merge.</p>
      <div v-if="rows.length" class="graph">
        <svg :width="width" :height="rows.length * ROW" aria-hidden="true">
          <template v-for="(row, i) in rows" :key="row.commit.commit">
            <!-- open lanes pass through; the commit's own lane starts at its tip and ends at its root -->
            <line
              v-for="lane in row.lanes"
              :key="lane"
              :x1="x(lane - 1)"
              :x2="x(lane - 1)"
              :y1="i * ROW + (lane - 1 === row.lane && i === 0 ? ROW / 2 : 0)"
              :y2="i * ROW + (lane - 1 === row.lane && !row.commit.parents.length ? ROW / 2 : ROW)"
            />
            <line v-for="to in row.merges" :key="'m' + to" class="merge" :x1="x(row.lane)" :y1="i * ROW + ROW / 2" :x2="x(to)" :y2="(i + 1) * ROW" />
            <circle :cx="x(row.lane)" :cy="i * ROW + ROW / 2" r="4" :class="{ tip: i === 0 }" />
          </template>
        </svg>
        <ol class="commits" aria-label="Latest commits">
          <li v-for="row in rows" :key="row.commit.commit" :style="{ height: ROW + 'px' }">
            <span class="mono">{{ row.commit.commit.slice(0, 7) }}</span>
            <span class="subject">{{ row.commit.subject }}</span>
            <span class="muted">{{ row.commit.author }}</span>
            <span class="muted">{{ age(row.commit.at, now) }}</span>
          </li>
        </ol>
      </div>
      <ul v-if="others.length && settled" class="merges" aria-label="Merge another branch into this one">
        <li v-for="(name, i) in others" :key="name">
          <span :id="'merge-' + i" class="chip-text">{{ name }}</span>
          <button type="button" class="merge-button" :aria-describedby="'merge-' + i" :disabled="!!busy || conflicts > 0" @click="merge(name)">{{ busy === name ? "Preparing…" : "Review merge" }}</button>
        </li>
      </ul>
      <p v-if="notice" class="card-line ok">{{ notice }}</p>
      <p v-if="error" class="card-line muted">{{ error }}</p>
    </template>
  </article>
</template>

<style scoped>
.work-card {
  display: grid;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--panel);
  font-size: var(--text-sm);
  color: var(--secondary);
}
.card-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.card-title {
  flex: 1;
  margin: 0;
  font-size: var(--text-md);
  font-weight: 560;
  color: var(--text);
}
.card-line {
  margin: 0;
}
.muted {
  color: var(--muted);
}
.ok {
  color: var(--ok);
}
.warn {
  color: var(--warn);
}
.plus {
  color: var(--ok);
}
.minus {
  color: var(--danger);
}
.chips,
.merges,
.commits {
  margin: 0;
  padding: 0;
  list-style: none;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.chip {
  display: inline-flex;
  gap: 6px;
  padding: 2px 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--soft);
  color: var(--text);
  overflow-wrap: anywhere;
}
.chip-note {
  color: var(--muted);
}
.graph {
  display: flex;
  gap: 4px;
  min-width: 0;
}
.graph svg {
  flex: none;
}
.graph line {
  stroke: var(--line-strong);
  stroke-width: 2;
}
.graph line.merge {
  stroke: var(--accent-soft);
}
.graph circle {
  fill: var(--panel);
  stroke: var(--accent);
  stroke-width: 2;
}
.graph circle.tip {
  fill: var(--accent);
}
.commits {
  flex: 1;
  min-width: 0;
}
.commits li {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
  overflow: hidden;
}
.subject {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text);
}
.mono {
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
}
.merges {
  display: grid;
  gap: 6px;
}
.merges li {
  display: flex;
  align-items: center;
  gap: 8px;
}
.merges .chip-text {
  flex: 1;
  min-width: 0;
  color: var(--text);
  overflow-wrap: anywhere;
}
.merge-button,
.card-more {
  padding: 3px 10px;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-sm);
  color: var(--text);
  font-size: var(--text-xs);
}
.card-more {
  border: 0;
  color: var(--secondary);
}
.merge-button:hover:not(:disabled),
.card-more:hover:not(:disabled) {
  background: var(--hover);
}
.merge-button:disabled,
.card-more:disabled {
  color: var(--muted);
}
.merge-button:focus-visible,
.card-more:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 1px;
}
</style>
