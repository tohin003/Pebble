<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { rpc } from '../bridge';

// Uncertain outcomes of a run that needs reconciliation. Pebble reads back the
// current state itself; a settlement never replays the effect, and a manual
// report is recorded as unverified.
const props = defineProps<{ chatId: string; runId: string }>();
const emit = defineEmits<{ refresh: []; error: [message: string] }>();
type Item = { call_id: string; tool: string | null; kind: string | null; verdict: string; basis: string };
const items = ref<Item[]>([]);
const loaded = ref(false);
const busy = ref('');
const reports = ref<Record<string, { effect: string; note: string }>>({});
let generation = 0;

const VERDICTS: Record<string, string> = {
  occurred: 'Pebble’s readback shows this happened.',
  not_occurred: 'Pebble’s readback shows this did not happen.',
  diverged: 'The current state matches neither the before nor the after state.',
  unknown: 'Pebble cannot tell whether this happened.',
};
const label = (item: Item) => (item.tool || 'Tool call') + (item.kind ? ' (' + item.kind + ')' : '');
const settleable = (item: Item) => item.verdict === 'occurred' || item.verdict === 'not_occurred';
const report = (item: Item) => (reports.value[item.call_id] ||= { effect: '', note: '' });

async function load() {
  const version = ++generation;
  loaded.value = false;
  try {
    const page = await rpc<{ items: Item[] }>('run_readback', { id: props.chatId, run_id: props.runId });
    if (version === generation) { items.value = Array.isArray(page?.items) ? page.items : []; loaded.value = true; }
  } catch (error) {
    if (version !== generation) return;
    items.value = [];
    // Phase 45 (F224, audit round R9): while another task runs the readback waits; that is not an error
    if (/Stop the active run/.test(String(error))) { waiting.value = true; return; }
    emit('error', String(error));
  }
}
const waiting = ref(false);
async function settle(item: Item, effect: string, note: string) {
  if (busy.value) return;
  busy.value = item.call_id;
  const version = generation;
  try {
    await rpc('run_reconcile', { id: props.chatId, run_id: props.runId, call_id: item.call_id, effect, note });
    if (version === generation) { delete reports.value[item.call_id]; emit('refresh'); await load(); }
  } catch (error) { if (version === generation) emit('error', String(error)); }
  finally { busy.value = ''; }
}
function submit(item: Item) {
  const entry = report(item);
  if (!['occurred', 'did_not_occur'].includes(entry.effect) || !entry.note.trim()) return;
  return settle(item, entry.effect, entry.note);
}
onMounted(load);
watch(() => [props.chatId, props.runId], load);
</script>

<template>
  <section class="run-reconcile" aria-label="Uncertain outcomes">
    <p>Pebble lost track of these actions before it could confirm their outcome. Nothing is retried. Settle each one before resuming.</p>
    <p v-if="waiting" role="status">Pebble reads these back once the current task ends.</p>
    <p v-else-if="loaded && !items.length" role="status">No uncertain outcome is waiting.</p>
    <ul v-else>
      <li v-for="item in items" :key="item.call_id">
        <strong>{{ label(item) }}</strong>
        <span>{{ VERDICTS[item.verdict] || VERDICTS.unknown }}</span>
        <small>Basis: {{ (item.basis || 'unknown').replaceAll('_', ' ') }}</small>
        <button v-if="settleable(item)" type="button" class="secondary-button" :disabled="!!busy"
          :aria-label="'Settle ' + label(item) + ' from Pebble’s readback'"
          @click="settle(item, 'readback', '')">Settle from readback</button>
        <form class="run-reconcile-report" @submit.prevent="submit(item)">
          <fieldset :disabled="!!busy">
            <legend>Report it yourself (recorded as unverified)</legend>
            <label class="run-choice"><input type="radio" :name="'effect:' + item.call_id" value="occurred" :checked="report(item).effect === 'occurred'" @change="report(item).effect = 'occurred'" />It happened</label>
            <label class="run-choice"><input type="radio" :name="'effect:' + item.call_id" value="did_not_occur" :checked="report(item).effect === 'did_not_occur'" @change="report(item).effect = 'did_not_occur'" />It did not happen</label>
            <textarea :aria-label="'What you checked for ' + label(item)" maxlength="2000" rows="2" :value="report(item).note"
              @input="report(item).note = ($event.target as HTMLTextAreaElement).value" />
            <button type="submit" class="secondary-button" :disabled="!!busy || !report(item).effect || !report(item).note.trim()">Record my report</button>
          </fieldset>
        </form>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.run-reconcile { padding: 14px; margin-top: 12px; border: 1px solid var(--border, #b9b9b9); border-radius: 12px; font-size: 12px; }
.run-reconcile p { margin: 0 0 12px; }
.run-reconcile ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 14px; }
.run-reconcile li { display: grid; gap: 6px; overflow-wrap: anywhere; }
.run-reconcile small { opacity: .7; }
.run-reconcile li > button { justify-self: start; }
.run-reconcile fieldset { border: 0; padding: 0; margin: 0; min-width: 0; }
.run-reconcile legend { font-weight: 600; margin-bottom: 4px; }
.run-choice { display: flex; gap: 8px; align-items: start; margin: 6px 0; }
.run-reconcile textarea { width: 100%; box-sizing: border-box; font: inherit; color: inherit; background: transparent; border: 1px solid var(--border, #b9b9b9); border-radius: 6px; padding: 8px; margin: 4px 0 8px; }
</style>
