<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { rpc } from "../bridge";
import { moveRowFocus } from "../shortcuts";

// Counts by the six memory layers (memory_list, memory_candidates) and the task's
// receipts and uncertain outcomes (run_get). Counts only: no memory content is
// shown, nothing is inferred, and every bound is stated. A record whose layer is
// not one of the six is reported as an omission, never as a seventh row.
interface Layer { id: string; name: string; count: number; enabled?: boolean }
interface MemoryRow { kind: string; chat_id?: string | null }
interface Candidate { kind: string; status: string; run_id: string }
interface Tool { call_id: string; name: string; state: string }

const props = defineProps<{ chatId: string; runId?: string | null; layers?: Layer[]; enabled?: boolean; request?: string }>();
// Phase 45 (F021, audit round R1): the heading named the task by its id ("task e5c3a036"); it quotes the message that
// asked for it when the window knows it (as the context view does, F141), the id staying in the tooltip.
const heading = computed(() => {
  const asked = (props.request || "").replace(/\s+/g, " ").trim();
  if (asked) return "“" + (asked.length > 60 ? asked.slice(0, 59) + "…" : asked) + "”";
  return props.runId ? "task " + props.runId.slice(0, 8) : "this conversation";
});
const LAYERS: readonly (readonly [string, string])[] = [
  ["working", "Working context"],
  ["episodic", "Past experiences"],
  ["semantic", "Facts & knowledge"],
  ["procedural", "Procedures"],
  ["temporal", "Time-sensitive information"],
  ["profile", "Your preferences"],
];
const KNOWN = new Set(LAYERS.map(([id]) => id));
const LIST_CAP = 300; // memory.list returns the newest 300 records
const CANDIDATE_CAP = 100;
const UNCERTAIN_ROWS = 20;
const records = ref<MemoryRow[] | null>(null);
const candidates = ref<Candidate[] | null>(null);
const tools = ref<Tool[] | null>(null);
const errors = ref<Record<string, string>>({});
const loading = ref(false);
let ticket = 0;
let alive = true;

const status = (value: unknown) => (typeof value === "string" && /^[a-z_]{1,32}$/.test(value) ? value : "unknown");
const listTruncated = computed(() => (records.value?.length || 0) >= LIST_CAP);
const candidatesTruncated = computed(() => (candidates.value?.length || 0) >= CANDIDATE_CAP);
const scoped = computed(() =>
  (candidates.value || []).filter((c) => !props.runId || c.run_id === props.runId),
);
const rows = computed(() =>
  LAYERS.map(([id, fallback]) => {
    const host = props.layers?.find((layer) => layer.id === id);
    const listed = (records.value || []).filter((r) => r.kind === id);
    const proposed = scoped.value.filter((c) => c.kind === id);
    const other = new Map<string, number>();
    for (const c of proposed) {
      const s = status(c.status);
      if (s !== "pending" && s !== "promoted") other.set(s, (other.get(s) || 0) + 1);
    }
    const counted = (n: number) => (listTruncated.value ? "≥ " : "") + n;
    return {
      id,
      name: host?.name || fallback,
      on: host ? (host.enabled === false ? "off" : "on") : "—",
      stored: host && Number.isFinite(host.count) ? String(host.count) : id === "working" || !records.value ? "—" : counted(listed.length),
      conversation: id === "working" ? "n/a" : records.value ? counted(listed.filter((r) => r.chat_id === props.chatId).length) : "—",
      pending: candidates.value && id !== "working" ? String(proposed.filter((c) => status(c.status) === "pending").length) : "—",
      promoted: candidates.value && id !== "working" ? String(proposed.filter((c) => status(c.status) === "promoted").length) : "—",
      other: Array.from(other).map(([s, n]) => `${s.replaceAll("_", " ")} ${n}`).join(" · ") || "—",
    };
  }),
);
const unrecognized = computed(
  () => (records.value || []).filter((r) => !KNOWN.has(r.kind)).length + scoped.value.filter((c) => !KNOWN.has(c.kind)).length,
);
const receipts = computed(() => {
  const counts = new Map<string, number>([["completed", 0], ["running", 0], ["uncertain", 0], ["other", 0]]);
  for (const tool of tools.value || []) {
    const state = ["completed", "running", "uncertain"].includes(tool.state) ? tool.state : "other";
    counts.set(state, (counts.get(state) || 0) + 1);
  }
  return Array.from(counts);
});
const uncertain = computed(() => (tools.value || []).filter((tool) => tool.state === "uncertain"));

async function load() {
  const mine = ++ticket, chat = props.chatId, run = props.runId || null;
  loading.value = true;
  const [listed, proposed, packet] = await Promise.allSettled([
    rpc<MemoryRow[]>("memory_list", {}),
    rpc<Candidate[]>("memory_candidates", { id: chat, limit: CANDIDATE_CAP }),
    run ? rpc<{ id: string; chat_id: string; tools: Tool[] }>("run_get", { id: chat, run_id: run }) : Promise.resolve(null),
  ]);
  if (!alive || mine !== ticket) return;
  const next: Record<string, string> = {};
  const reason = (result: PromiseRejectedResult) => (result.reason instanceof Error ? result.reason.message : String(result.reason));
  if (listed.status === "fulfilled" && Array.isArray(listed.value)) records.value = listed.value.filter((r) => r && typeof r === "object");
  else { records.value = null; next.records = listed.status === "rejected" ? reason(listed) : "The memory list was malformed."; }
  if (proposed.status === "fulfilled" && Array.isArray(proposed.value)) candidates.value = proposed.value.filter((c) => c && typeof c === "object");
  else { candidates.value = null; next.candidates = proposed.status === "rejected" ? reason(proposed) : "The learning candidates were malformed."; }
  if (packet.status === "fulfilled" && packet.value && packet.value.id === run && packet.value.chat_id === chat && Array.isArray(packet.value.tools)) tools.value = packet.value.tools.filter((t) => t && typeof t === "object");
  else {
    tools.value = null;
    if (run) next.receipts = packet.status === "rejected" ? reason(packet) : "The reply belongs to a different task.";
  }
  errors.value = next;
  loading.value = false;
}
watch(() => [props.chatId, props.runId], () => {
  records.value = candidates.value = tools.value = null;
  load();
});
onMounted(load);
onUnmounted(() => (alive = false));
</script>

<template>
  <section class="impact-tables" aria-label="Memory impact">
    <div class="impact-head">
      <strong :title="runId ? 'Task ' + runId : undefined">Memory impact · {{ heading }}</strong>
      <button type="button" class="secondary-button" :disabled="loading" @click="load">{{ loading ? "Loading…" : "Refresh" }}</button>
    </div>
    <p v-if="enabled === false" class="impact-note">
      Personalized memory is off: nothing is learned or recalled for personalization. Stored records stay until you forget them.
    </p>
    <p v-if="errors.records" class="impact-error" role="alert">Memory records: {{ errors.records }}</p>
    <p v-if="errors.candidates" class="impact-error" role="alert">Learning candidates: {{ errors.candidates }}</p>
    <table>
      <caption>Six memory layers (counts only)</caption>
      <thead>
        <tr>
          <th scope="col">Layer</th><th scope="col">Switch</th><th scope="col">Stored</th><th scope="col">From this conversation</th>
          <th scope="col">Proposed by {{ runId ? "this task" : "this conversation" }}: waiting</th><th scope="col">Promoted</th><th scope="col">Other outcomes</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, i) in rows" :key="row.id" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
          <th scope="row">{{ row.name }}</th><td>{{ row.on }}</td><td>{{ row.stored }}</td><td>{{ row.conversation }}</td>
          <td>{{ row.pending }}</td><td>{{ row.promoted }}</td><td>{{ row.other }}</td>
        </tr>
      </tbody>
    </table>
    <p class="impact-note">Working context is this conversation's checkpoint, not learned records, so it has no per-conversation or candidate count.</p>
    <p v-if="listTruncated" class="impact-note">Only the newest {{ LIST_CAP }} memory records were read; counts marked ≥ may be higher.</p>
    <p v-if="candidatesTruncated" class="impact-note">Only the newest {{ CANDIDATE_CAP }} learning candidates of this conversation were read.</p>
    <p v-if="unrecognized" class="impact-note">{{ unrecognized }} {{ unrecognized === 1 ? "record has" : "records have" }} an unrecognized layer and {{ unrecognized === 1 ? "is" : "are" }} not counted above.</p>

    <template v-if="runId">
      <p v-if="errors.receipts" class="impact-error" role="alert">Task receipts: {{ errors.receipts }}</p>
      <table v-if="tools">
        <caption>Task actions by state</caption>
        <tbody>
          <tr v-for="([name, count], i) in receipts" :key="name" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus"><th scope="row">{{ name }}</th><td>{{ count }}</td></tr>
        </tbody>
      </table>
      <table v-if="uncertain.length">
        <caption>Uncertain outcomes (never replayed)</caption>
        <thead><tr><th scope="col">Tool</th><th scope="col">Record</th></tr></thead>
        <tbody>
          <tr v-for="(tool, i) in uncertain.slice(0, UNCERTAIN_ROWS)" :key="tool.call_id" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus"><td>{{ tool.name }}</td><td>{{ tool.call_id }}</td></tr>
        </tbody>
      </table>
      <p v-if="uncertain.length > UNCERTAIN_ROWS" class="impact-note">{{ uncertain.length - UNCERTAIN_ROWS }} more uncertain outcomes are not listed.</p>
      <p v-if="uncertain.length" class="impact-note">Inspect and settle uncertain outcomes in the task panel before continuing.</p>
    </template>
    <p class="impact-note">Counts only: this view shows no memory content. Memory never grants permissions.</p>
  </section>
</template>

<style scoped>
.impact-tables { margin: 10px 0; font-size: 12px; min-width: 0; }
.impact-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.impact-note { color: var(--muted); font-size: 11px; margin: 6px 0; }
.impact-error { color: var(--danger); font-size: 12px; margin: 6px 0; }
.impact-tables table { width: 100%; border-collapse: collapse; margin: 10px 0; }
.impact-tables caption { text-align: left; font-weight: 600; padding-bottom: 6px; }
.impact-tables th, .impact-tables td { text-align: left; vertical-align: top; padding: 4px 6px; border-bottom: 1px solid var(--border, #b9b9b9); overflow-wrap: normal; word-break: normal; hyphens: auto; }
/* Phase 45 (F020): in the narrow drawer the table scrolls sideways instead of breaking words a letter per line. */
.impact-tables { overflow-x: auto; }
.impact-tables thead th { min-width: 4.5em; }
.impact-tables th { font-weight: 500; color: var(--muted); }
.impact-tables tr:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
</style>
