<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { rpc } from "../bridge";
import { moveRowFocus } from "../shortcuts";

// Host records from run_get only: the latest content-free context manifest, the
// run's file views, archives and handoff. Nothing is summarised or inferred, and
// every bound or gap is stated (design study §4.5, U05/C09).
interface Unit { unit: number; category: string; roles: string[]; estimated_tokens: number; protected: boolean; selected: boolean; reason: string | null }
interface Manifest {
  status?: string; dispatchable?: boolean;
  limits?: { context?: number; output_reserve?: number; input?: number };
  input_tokens?: { before?: number | null; after?: number | null; protected_floor?: number | null; tools_only?: number | null };
  estimation?: { window_method?: string; unit_method?: string };
  tools?: { count?: number; estimated_tokens?: number };
  messages?: { input?: number; selected?: number };
  omitted?: { units?: number; messages?: number; estimated_tokens?: number };
  summary?: Record<string, Record<string, number>>;
  units?: Unit[]; units_total?: number; units_listed?: number; not_verified?: string[];
}
interface Source { receipt_id: string; path: string; basis: string; status: string; checked: number }
interface RunPacket {
  id: string; chat_id: string; state: string; steps?: number;
  context?: { step?: number; manifest?: Manifest } | null;
  sources?: Source[]; compactions?: { generation: number; created: number }[];
  handoff?: { created: number } | null;
}

const props = defineProps<{ chatId: string; runId: string }>();
const MAX_ROWS = 100;
const methods: Record<string, string> = {
  local_estimate: "Local estimate (about 3 bytes per token), not a provider count",
  injected_counter: "Token count supplied to Pebble",
};
const verify: Record<string, string> = { source_freshness: "source freshness", source_truth: "source truth", permissions: "permissions" };
const statuses: Record<string, string> = {
  complete: "Complete: nothing was retired",
  retired: "Older context retired to fit the budget",
  exceeded: "Exceeded the budget: not sent to the model",
};
const pick = (table: Record<string, string>, key: unknown) =>
  typeof key === "string" && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
const packet = ref<RunPacket | null>(null);
const loading = ref(false);
const error = ref("");
let ticket = 0;
let alive = true;

const num = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "—");
const when = (seconds: unknown) => (typeof seconds === "number" && Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleTimeString() : "—");
const manifest = computed(() => packet.value?.context?.manifest || null);
const categories = computed(() =>
  Object.entries(manifest.value?.summary || {}).filter(([, row]) => row && typeof row === "object" && (row.units || 0) > 0),
);
const units = computed(() =>
  Array.isArray(manifest.value?.units) ? manifest.value!.units!.filter((unit) => unit && typeof unit === "object") : [],
);
const shownUnits = computed(() => units.value.slice(0, MAX_ROWS));
const unitsTotal = computed(() => manifest.value?.units_total ?? units.value.length);
// One row per path: the newest witness (records are ordered by the host).
const witnesses = computed(() => (packet.value?.sources || []).filter((s) => s && typeof s.path === "string"));
const views = computed(() => {
  const latest = new Map<string, Source>();
  for (const source of witnesses.value) latest.set(source.path, source);
  return Array.from(latest.values()).sort((a, b) => Number(a.status === "current") - Number(b.status === "current"));
});
const earlierViews = computed(() => witnesses.value.length - views.value.length);
const stale = computed(() => views.value.filter((view) => view.status !== "current").length);
const notVerified = computed(() => (manifest.value?.not_verified || []).map((item) => pick(verify, item) || "unknown"));
const windowRows = computed(() => {
  const m = manifest.value;
  if (!m) return [];
  return [
    ["Input budget", `${num(m.limits?.input)} of ${num(m.limits?.context)} tokens; ${num(m.limits?.output_reserve)} reserved for output`],
    ["Projected input", `${num(m.input_tokens?.after)} tokens kept of ${num(m.input_tokens?.before)} before retirement`],
    ["Protected floor", `${num(m.input_tokens?.protected_floor)} tokens`],
    ["Tool schemas", `${num(m.tools?.count)} tools · about ${num(m.tools?.estimated_tokens)} tokens`],
    ["Messages", `${num(m.messages?.selected)} of ${num(m.messages?.input)} kept`],
    ["Omitted from the window", `${num(m.omitted?.units)} units · ${num(m.omitted?.messages)} messages · about ${num(m.omitted?.estimated_tokens)} tokens`],
    ["Window count", pick(methods, m.estimation?.window_method)
      || (m.estimation?.window_method ? "Host method: " + String(m.estimation.window_method).slice(0, 80) : "Not recorded")],
    ["Status", pick(statuses, m.status) || "Unknown status"],
  ];
});

async function load() {
  const mine = ++ticket, chat = props.chatId, run = props.runId;
  loading.value = true;
  error.value = "";
  try {
    const result = await rpc<RunPacket>("run_get", { id: chat, run_id: run });
    if (!alive || mine !== ticket) return;
    if (!result || result.id !== run || result.chat_id !== chat)
      throw new Error("The reply belongs to a different task.");
    packet.value = result;
  } catch (e) {
    if (alive && mine === ticket) {
      packet.value = null;
      error.value = e instanceof Error ? e.message : String(e);
    }
  } finally {
    if (alive && mine === ticket) loading.value = false;
  }
}
watch(() => [props.chatId, props.runId], () => { packet.value = null; load(); });
onMounted(load);
onUnmounted(() => (alive = false));
</script>

<template>
  <section class="inspector-tables" aria-label="Context inspector">
    <div class="inspector-head">
      <strong>Context · task {{ runId.slice(0, 8) }}</strong>
      <button type="button" class="secondary-button" :disabled="loading" @click="load">{{ loading ? "Loading…" : "Refresh" }}</button>
    </div>
    <p class="inspector-note">Host records only: nothing here is summarised or judged by a model.</p>
    <p v-if="error" class="inspector-error" role="alert">{{ error }}</p>
    <template v-if="packet">
      <p v-if="!manifest" class="inspector-note">No provider context manifest has been recorded for this task yet.</p>
      <template v-else>
        <table>
          <caption>Model window at step {{ num(packet.context?.step) }}</caption>
          <tbody>
            <tr v-for="(row, i) in windowRows" :key="row[0]" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus"><th scope="row">{{ row[0] }}</th><td>{{ row[1] }}</td></tr>
          </tbody>
        </table>
        <table v-if="categories.length">
          <caption>By category (estimated tokens)</caption>
          <thead><tr><th scope="col">Category</th><th scope="col">Units</th><th scope="col">Messages</th><th scope="col">Kept</th><th scope="col">Omitted</th><th scope="col">Kept tokens</th><th scope="col">Omitted tokens</th></tr></thead>
          <tbody>
            <tr v-for="([name, row], i) in categories" :key="name" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
              <th scope="row">{{ name.replaceAll("_", " ") }}</th><td>{{ num(row.units) }}</td><td>{{ num(row.messages) }}</td>
              <td>{{ num(row.selected_units) }}</td><td>{{ num(row.omitted_units) }}</td>
              <td>{{ num(row.selected_estimated_tokens) }}</td><td>{{ num(row.omitted_estimated_tokens) }}</td>
            </tr>
          </tbody>
        </table>
        <p class="inspector-note">Categories without content are not listed.</p>
        <table v-if="shownUnits.length">
          <caption>Context units (content-free)</caption>
          <thead><tr><th scope="col">Unit</th><th scope="col">Category</th><th scope="col">Messages</th><th scope="col">Tokens</th><th scope="col">In window</th><th scope="col">Reason</th></tr></thead>
          <tbody>
            <tr v-for="(unit, i) in shownUnits" :key="unit.unit" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
              <td>{{ unit.unit }}</td><td>{{ String(unit.category).replaceAll("_", " ") }}</td><td>{{ unit.roles?.length ?? "—" }}</td>
              <td>{{ num(unit.estimated_tokens) }}</td><td>{{ unit.selected ? "kept" : "omitted" }}{{ unit.protected ? " · protected" : "" }}</td>
              <td>{{ unit.reason ? String(unit.reason).replaceAll("_", " ") : "—" }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="unitsTotal > shownUnits.length" class="inspector-note">
          Showing {{ shownUnits.length }} of {{ unitsTotal }} context units; the rest are counted in the tables above.
        </p>
        <p v-if="notVerified.length" class="inspector-note">Not verified by this manifest: {{ notVerified.join(", ") }}.</p>
      </template>

      <table v-if="views.length">
        <caption>File views · {{ stale }} {{ stale === 1 ? "needs" : "need" }} a fresh read</caption>
        <thead><tr><th scope="col">Path</th><th scope="col">Status</th><th scope="col">Read from</th><th scope="col">Record</th><th scope="col">Last checked</th></tr></thead>
        <tbody>
          <tr v-for="(view, i) in views.slice(0, MAX_ROWS)" :key="view.path" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
            <td>{{ view.path }}</td><td>{{ view.status }}</td><td>{{ view.basis }}</td><td>{{ view.receipt_id }}</td><td>{{ when(view.checked) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="inspector-note">This task has no tracked file views.</p>
      <p v-if="views.length > MAX_ROWS" class="inspector-note">{{ views.length - MAX_ROWS }} more file views are not shown.</p>
      <p v-if="earlierViews > 0" class="inspector-note">{{ earlierViews }} earlier {{ earlierViews === 1 ? "view of a listed file is" : "views of listed files are" }} not listed.</p>
      <p v-if="views.length" class="inspector-note">Statuses are as of the last host check and are not re-checked here; /context re-checks them.</p>
      <p class="inspector-note">
        Context archives: {{ packet.compactions?.length || 0 }}<template v-if="packet.compactions?.length"> (latest generation {{ packet.compactions.at(-1)?.generation }})</template>
        · Handoff snapshot: {{ packet.handoff ? "saved at " + when(packet.handoff.created) : "none" }}
      </p>
    </template>
  </section>
</template>

<style scoped>
.inspector-tables { margin: 10px 0; font-size: 12px; min-width: 0; }
.inspector-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.inspector-note { color: var(--muted); font-size: 11px; margin: 6px 0; }
.inspector-error { color: var(--danger); font-size: 12px; margin: 6px 0; }
.inspector-tables table { width: 100%; border-collapse: collapse; margin: 10px 0; }
.inspector-tables caption { text-align: left; font-weight: 600; padding-bottom: 6px; }
.inspector-tables th, .inspector-tables td { text-align: left; vertical-align: top; padding: 4px 6px; border-bottom: 1px solid var(--border, #b9b9b9); overflow-wrap: anywhere; }
.inspector-tables th { font-weight: 500; color: var(--muted); }
.inspector-tables tr:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
</style>
