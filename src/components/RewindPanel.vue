<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { rpc } from '../bridge';

// Main window only: put back the files a task wrote, as they were after a chosen
// step. The preview is a read. Rewinding stages ordinary reviewed edits through
// the broker and reads each file back; it applies only the preview the person
// saw (its digest). Commands, connector calls, Git plans and other actions are
// listed as not reverted and never run again. The shell writes the calling
// window's label, so the pet and generated views cannot use these routes.
type FileItem = { path: string; status: string; detail: string; steps: number[]; diff: string; truncated: boolean };
type Effect = { kind: string; label: string; step: number | null; outcome: string; note: string; connector: string };
type StepItem = { step: number; files: string[]; effects: number };
type History = { id: string; step: number; previous: number; reverses: boolean; applied: number };
type Plan = { digest: string; step: number; steps: number; files: FileItem[]; effects: Effect[]; omitted: number;
  timeline: StepItem[]; rewinds: History[]; previous: number; reverses: boolean };
type Outcome = { path: string; status: string; detail: string };
type Reply = Record<string, unknown>;
const props = defineProps<{ chatId: string; runId: string }>();
const emit = defineEmits<{ refresh: []; error: [message: string] }>();
const chosen = ref(0);
const plan = ref<Plan | null>(null);
const outcome = ref<Outcome[] | null>(null);
const busy = ref('');
const status = ref('');
let generation = 0;

const STATUS: Record<string, string> = { revert: 'Will be restored', unchanged: 'Already as it was', kept: 'Kept, never deleted', refused: 'Left alone' };
const RESULT: Record<string, string> = { reverted: 'restored and read back', refused: 'left alone', kept: 'kept', unchanged: 'already as it was',
  diverged: 'written, but it read back differently', uncertain: 'not confirmed' };
const KIND: Record<string, string> = { command: 'Command', connector: 'Connector', git: 'Git', app: 'App or screen', agent: 'Delegated agent', view: 'View' };
const named = (table: Record<string, string>, key: string) => Object.hasOwn(table, key) ? table[key] : key;
const reply = (value: unknown): Reply => value && typeof value === 'object' && !Array.isArray(value) ? value as Reply : {};
const list = (value: unknown) => Array.isArray(value) ? value.map(reply) : [];
const word = (value: unknown) => typeof value === 'string' ? value : '';
const whole = (value: unknown) => typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
const reverting = computed(() => plan.value?.files.filter(file => file.status === 'revert') || []);
const latest = computed(() => [...(plan.value?.rewinds || [])].reverse().find(item => item.applied > 0) || null);
const options = computed(() => {
  const current = plan.value;
  const steps = new Set([0, chosen.value, ...(current ? [current.steps, ...current.timeline.map(item => item.step)] : [])]);
  const notes = new Map((current?.timeline || []).map(item => [item.step, item]));
  return [...steps].sort((a, b) => a - b).map(step => {
    const item = notes.get(step);
    const detail = item ? [item.files.length ? 'wrote ' + item.files.join(', ') : '', item.effects ? item.effects + ' other action' + (item.effects === 1 ? '' : 's') : '']
      .filter(Boolean).join('; ') : '';
    return { step, label: (step === 0 ? 'Before this task' : 'After step ' + step) + (detail ? ' (' + detail + ')' : '') };
  });
});
const stepText = (step: number | null) => step === null ? 'Step unknown' : 'Step ' + step;

function review(value: unknown): Plan {
  const item = reply(value), digest = item.digest, step = whole(item.step);
  if (typeof digest !== 'string' || !/^[0-9a-f]{64}$/.test(digest) || step === null)
    throw new Error('Pebble could not prepare this rewind. Nothing was changed.');
  return {
    digest, step, steps: whole(item.steps) ?? step,
    files: list(item.files).map(file => ({ path: word(file.path), status: word(file.status), detail: word(file.detail),
      steps: Array.isArray(file.steps) ? file.steps.filter((n): n is number => whole(n) !== null) : [],
      diff: word(file.diff), truncated: file.diff_truncated === true })),
    effects: list(item.effects).map(effect => ({ kind: word(effect.kind), label: word(effect.label), step: whole(effect.step),
      outcome: word(effect.outcome_text), note: word(effect.note), connector: word(effect.connector_class) })),
    omitted: whole(item.effects_omitted) ?? 0,
    timeline: list(item.timeline).map(entry => ({ step: whole(entry.step) ?? 0,
      files: Array.isArray(entry.files) ? entry.files.filter((name): name is string => typeof name === 'string') : [],
      effects: whole(entry.effects) ?? 0 })).filter(entry => entry.step > 0),
    rewinds: list(item.rewinds).map(entry => ({ id: word(entry.rewind_id), step: whole(entry.step) ?? 0,
      previous: whole(entry.previous_step) ?? 0, reverses: typeof entry.reverses === 'string', applied: whole(entry.applied) ?? 0 })),
    previous: whole(item.previous_step) ?? step, reverses: typeof item.reverses === 'string',
  };
}

async function load(step = chosen.value) {
  const version = ++generation;
  plan.value = null;
  busy.value = 'preview'; status.value = 'Checking what a rewind would change…';
  try {
    const result = await rpc<unknown>('rewind_preview', { chat_id: props.chatId, run_id: props.runId, step });
    if (version !== generation) return;
    plan.value = review(result);
    chosen.value = plan.value.step;
    status.value = reverting.value.length ? 'Review the changes, then rewind.' : 'Nothing to restore for this step.';
  } catch (error) { if (version === generation) { status.value = ''; emit('error', String(error)); } }
  finally { if (version === generation) busy.value = ''; }
}
function choose(value: string) {
  const step = Number(value);
  if (busy.value || !Number.isInteger(step) || step < 0) return;
  chosen.value = step; outcome.value = null;
  void load(step);
}
function reviewUndo() {
  if (latest.value) choose(String(latest.value.previous));
}
async function rewind() {
  const reviewed = plan.value;
  if (busy.value || !reviewed || !reverting.value.length) return;
  const version = generation;
  busy.value = 'apply'; status.value = 'Rewinding…';
  let message = '';
  try {
    const result = reply(await rpc<unknown>('rewind_apply',
      { chat_id: props.chatId, run_id: props.runId, step: reviewed.step, digest: reviewed.digest }));
    if (version !== generation) return;
    outcome.value = list(result.files).map(file => ({ path: word(file.path), status: word(file.status), detail: word(file.detail) }));
    const restored = outcome.value.filter(file => file.status === 'reverted').length;
    message = restored === 1 ? 'Restored 1 file.' : 'Restored ' + restored + ' files.';
    emit('refresh');
  } catch (error) { if (version === generation) { status.value = ''; emit('error', String(error)); } }
  finally { if (version === generation) busy.value = ''; }
  // A preview is single-use: always show the files as they are now, and keep the result in view.
  if (version !== generation) return;
  await load(reviewed.step);
  if (message && outcome.value) status.value = message;
}
watch([() => props.chatId, () => props.runId], () => { chosen.value = 0; outcome.value = null; void load(0); });
onMounted(() => load(0));
onBeforeUnmount(() => { generation++; });
</script>

<template>
  <section class="rewind-panel" aria-label="Rewind files" :aria-busy="!!busy">
    <strong>Rewind files</strong>
    <p>Pebble puts back the files this task wrote, as they were after the step you choose. Each file is an ordinary reviewed edit and is read back afterwards. Commands, connector messages, Git changes and other actions are listed below; they are never undone or run again. A file changed since this task is left alone, and a rewind never deletes a file.</p>
    <label class="rewind-step">Rewind to
      <select :value="chosen" :disabled="!!busy" @change="choose(($event.target as HTMLSelectElement).value)">
        <option v-for="option in options" :key="option.step" :value="option.step">{{ option.label }}</option>
      </select>
    </label>
    <p class="rewind-status" role="status" aria-live="polite">{{ status }}</p>
    <template v-if="plan">
      <p v-if="plan.reverses" class="rewind-note">This undoes the last rewind.</p>
      <h4>Files</h4>
      <p v-if="!plan.files.length">This task wrote no file after that step.</p>
      <ul v-else class="rewind-files">
        <li v-for="file in plan.files" :key="file.path">
          <div><span class="rewind-path">{{ file.path }}</span> <span class="rewind-badge">{{ named(STATUS, file.status) }}</span></div>
          <small v-if="file.steps.length">Undoes {{ file.steps.length === 1 ? 'step' : 'steps' }} {{ file.steps.join(', ') }}</small>
          <p v-if="file.detail">{{ file.detail }}</p>
          <pre v-if="file.status === 'revert' && file.diff" class="rewind-diff" tabindex="0" :aria-label="'Changes to ' + file.path">{{ file.diff }}</pre>
          <small v-if="file.truncated">The change is longer than shown here.</small>
        </li>
      </ul>
      <h4>Not reverted</h4>
      <p v-if="!plan.effects.length">No command, connector or other action after that step.</p>
      <ul v-else class="rewind-effects">
        <li v-for="(effect, index) in plan.effects" :key="index">
          <div><strong>{{ named(KIND, effect.kind) }}</strong> <span>{{ effect.label }}</span></div>
          <small>{{ stepText(effect.step) }} · {{ effect.outcome }}<template v-if="effect.connector"> · {{ effect.connector }}</template></small>
          <p>{{ effect.note }}</p>
        </li>
      </ul>
      <p v-if="plan.omitted">And {{ plan.omitted }} more, also not reverted.</p>
    </template>
    <ul v-if="outcome" class="rewind-outcome" aria-label="Rewind result">
      <li v-for="item in outcome" :key="item.path">{{ item.path }}: {{ named(RESULT, item.status) }}<template v-if="item.detail">. {{ item.detail }}</template></li>
    </ul>
    <div class="setting-actions">
      <button type="button" class="primary-button" :disabled="!!busy || !reverting.length" @click="rewind">{{ reverting.length === 1 ? 'Rewind 1 file' : 'Rewind ' + reverting.length + ' files' }}</button>
      <button v-if="latest && latest.previous !== chosen" type="button" class="secondary-button" :disabled="!!busy" @click="reviewUndo">Review undoing the last rewind</button>
      <button type="button" class="secondary-button" :disabled="!!busy" @click="load()">Review again</button>
    </div>
  </section>
</template>

<style scoped>
.rewind-panel { padding: 14px; margin-top: 12px; border: 1px solid var(--border, #b9b9b9); border-radius: 12px; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.rewind-panel > strong { display: block; font-size: 13px; }
.rewind-panel p { margin: 6px 0; }
.rewind-panel h4 { margin: 12px 0 4px; font-size: 12px; }
.rewind-step { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.rewind-step select { font: inherit; max-width: 100%; }
.rewind-files, .rewind-effects, .rewind-outcome { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; }
.rewind-path { font-family: var(--mono, ui-monospace, monospace); }
.rewind-badge { opacity: .75; }
.rewind-panel small { display: block; opacity: .7; }
.rewind-diff { max-height: 240px; overflow: auto; margin: 4px 0; padding: 8px; border: 1px solid var(--border, #b9b9b9); border-radius: 6px; font-size: 11px; white-space: pre; }
.rewind-panel .setting-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
</style>
