<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { rpc, subscribe } from '../bridge';

const props = defineProps<{ chatId: string; runId?: string }>();
const emit = defineEmits<{ error: [message: string] }>();
type Process = { id: string; run_id: string; state: string; outcome_uncertain: boolean | number; inspection_recorded: boolean | number };
const processes = ref<Process[]>([]);
const outputs = ref<Record<string, { text: string; after: number; more: boolean; incomplete: boolean; controls: number }>>({});
const busy = ref(false);
const inspectionNotes = ref<Record<string, string>>({});
const observations = ref<Record<string, string>>({});
const inspected = ref<Record<string, boolean>>({});
// Interactive jobs: the terminal's echo state decides whether this is a password prompt.
type Terminal = { interactive: boolean; live: boolean; echo: boolean | null };
const terminals = ref<Record<string, Terminal>>({});
const typed = ref<Record<string, string>>({});
const typing = ref('');
let generation = 0;
let unsubscribe = () => {};

async function refresh() {
  const owner = props.chatId, version = generation;
  try {
    const rows = await rpc<Process[]>('process_list', { id: owner });
    if (owner === props.chatId && version === generation) {
      processes.value = rows;
      for (const row of rows) observations.value[row.id] ||= 'still_uncertain';
      for (const row of rows.filter(r => ['admitted', 'running'].includes(r.state))) void terminal(row);
    }
  } catch (error) { if (version === generation) emit('error', String(error)); }
}
async function terminal(process: Process) {
  const owner = props.chatId, version = generation;
  try {
    const view = await rpc<Terminal>('process_terminal', { id: owner, run_id: process.run_id, process_id: process.id });
    if (owner === props.chatId && version === generation) terminals.value[process.id] = view;
  } catch { /* a non-interactive or settled job has no terminal */ }
}
async function send(process: Process, key?: string) {
  if (typing.value) return;
  const text = key ? undefined : typed.value[process.id] || '';
  if (!key && !text) return;
  typing.value = process.id;
  const version = generation;
  try {
    const target = { id: props.chatId, run_id: process.run_id, process_id: process.id };
    if (key) await rpc('process_input_human', { ...target, key });
    else {
      await rpc('process_input_human', { ...target, text });
      await rpc('process_input_human', { ...target, key: 'enter' });
      if (version === generation) typed.value[process.id] = '';
    }
    if (version === generation) await terminal(process);
  } catch (error) { if (version === generation) emit('error', String(error)); }
  finally { typing.value = ''; }
}
async function read(process: Process) {
  if (busy.value) return;
  busy.value = true;
  const owner = props.chatId, version = generation;
  try {
    const previous = outputs.value[process.id];
    const page = await rpc('process_read', { id: owner, run_id: process.run_id,
      process_id: process.id, after: previous?.after || 0, limit: 4000 });
    if (owner !== props.chatId || version !== generation) return;
    // Keep a bounded visible page. The service retains the durable cursor history.
    outputs.value[process.id] = { text: page.untrusted_process_output || '',
      after: page.next_after, more: !!page.more, incomplete: page.output_complete !== true,
      controls: Number(page.controls_removed) || 0 };
    process.state = page.state;
    process.outcome_uncertain = page.outcome_uncertain;
  } catch (error) { if (version === generation) emit('error', String(error)); }
  finally { busy.value = false; }
}
async function stop(process: Process) {
  const owner = props.chatId, version = generation;
  try {
    await rpc('process_stop', { id: owner, run_id: process.run_id, process_id: process.id });
    if (version === generation) await refresh();
  } catch (error) { if (version === generation) emit('error', String(error)); }
}
async function inspect(process: Process) {
  if (busy.value || !inspectionNotes.value[process.id]?.trim()) return;
  busy.value = true;
  const version = generation;
  try {
    await rpc('process_inspect', { id: props.chatId, run_id: process.run_id, process_id: process.id,
      observation: observations.value[process.id] || 'still_uncertain', note: inspectionNotes.value[process.id] });
    if (version === generation) { inspected.value[process.id] = true; process.inspection_recorded = true; }
  } catch (error) { if (version === generation) emit('error', String(error)); }
  finally { busy.value = false; }
}
async function dismiss(process: Process) {
  const version = generation;
  try {
    await rpc('process_dismiss', { id: props.chatId, run_id: process.run_id, process_id: process.id });
    if (version === generation) { delete outputs.value[process.id]; await refresh(); }
  } catch (failure) { if (version === generation) emit('error', String(failure)); }
}
watch(() => props.chatId, () => {
  generation++; processes.value = []; outputs.value = {}; terminals.value = {}; typed.value = {};
  inspectionNotes.value = {}; observations.value = {}; inspected.value = {}; void refresh();
});
watch(() => props.runId, () => { void refresh(); });
onMounted(async () => {
  await refresh();
  const release = await subscribe(event => {
    if (event.data.chat_id === props.chatId && ['process', 'run', 'idle', 'actions'].includes(event.event)) void refresh();
  });
  if (disposed) release(); else unsubscribe = release;
});
let disposed = false;
onBeforeUnmount(() => { disposed = true; generation++; unsubscribe(); });
</script>

<template>
  <section v-if="processes.length" aria-label="Command output">
    <details v-for="process in processes" :key="process.id">
      <summary>Command: {{ process.state.replaceAll('_', ' ') }}</summary>
      <p v-if="process.state === 'unknown'">The service did not observe this outcome. Inspect external effects before retrying. This run still requires verification.</p>
      <form v-if="process.outcome_uncertain && !process.inspection_recorded" @submit.prevent="inspect(process)">
        <label>Observed outcome
          <select v-model="observations[process.id]">
            <option value="still_uncertain">Still uncertain after inspection</option>
            <option value="effect_observed">Effect observed</option>
            <option value="no_effect_observed">No effect observed</option>
          </select>
        </label>
        <label>Inspection note <input v-model="inspectionNotes[process.id]" maxlength="500" required /></label>
        <p>Recording an inspection allows newly reviewed project actions. Prior effects remain unverified.</p>
        <button class="secondary-button" :disabled="busy">Record my inspection</button>
      </form>
      <p v-if="inspected[process.id]">Inspection recorded. Command effects remain unverified.</p>
      <form v-if="terminals[process.id]?.interactive && terminals[process.id]?.live && ['admitted', 'running'].includes(process.state)"
        class="process-terminal" @submit.prevent="send(process)">
        <p v-if="terminals[process.id].echo === false" role="status">Password prompt: what you type is sent to the command only. It is not shown, not stored and the model cannot answer it.</p>
        <label>Type into the command
          <input v-model="typed[process.id]" :type="terminals[process.id].echo === false ? 'password' : 'text'" maxlength="1024" autocomplete="off" :spellcheck="false" />
        </label>
        <button type="submit" class="secondary-button" :disabled="!!typing || !typed[process.id]">Send line</button>
        <button type="button" class="secondary-button" :disabled="!!typing" @click="send(process, 'ctrl_c')">Send Ctrl-C</button>
        <button type="button" class="secondary-button" :disabled="!!typing" @click="send(process, 'ctrl_d')">Send Ctrl-D</button>
      </form>
      <button class="secondary-button" :disabled="busy" @click="read(process)">{{ outputs[process.id]?.more ? 'Next output page' : 'Read output' }}</button>
      <button v-if="['admitted', 'running'].includes(process.state)" class="secondary-button" @click="stop(process)">Stop command</button>
      <button v-else-if="!process.outcome_uncertain || process.inspection_recorded" class="secondary-button" @click="dismiss(process)">Forget saved command output</button>
      <p v-if="outputs[process.id]?.incomplete">Saved output is a partial record of this command.</p>
      <p v-if="outputs[process.id]?.controls">Terminal control sequences hidden on this page: {{ outputs[process.id].controls }}. The raw output bytes are kept.</p>
      <pre v-if="outputs[process.id]" style="white-space:pre-wrap;overflow-wrap:anywhere">{{ outputs[process.id].text }}</pre>
    </details>
  </section>
</template>
