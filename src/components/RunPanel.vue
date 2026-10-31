<script setup lang="ts">
import { computed, defineAsyncComponent, reactive, ref, watch } from 'vue';
import { rpc } from '../bridge';
import type { Chat, State } from '../types';
import AgentPanel from './AgentPanel.vue';
import ExecutionOutput from './ExecutionOutput.vue';
import AttemptStream from './AttemptStream.vue';
import ReconcilePanel from './ReconcilePanel.vue';

// Main window only: the pet renders PetRunSummary, because these nested panels
// read routes the pet allowlist refuses (F6). Inspectors load when opened.
const ContextInspector = defineAsyncComponent(() => import('./ContextInspector.vue'));
const MemoryImpactView = defineAsyncComponent(() => import('./MemoryImpactView.vue'));
const RewindPanel = defineAsyncComponent(() => import('./RewindPanel.vue'));
const ContextPanel = defineAsyncComponent(() => import('./ContextPanel.vue'));
// Phase 22: `view` splits the panel (design study §10.3). "conversation" (the chat column) shows
// only what needs the person: pending questions and a one-line note when the task stopped short.
// The Details drawer's tabs each take one part: "activity" (task state, Pause/Resume, output,
// stream history, reconciliation), "task" (plan and agents), "context" (context, context use with
// Compact, memory impact) and "changes" (rewind for the latest settled task). "details" is all the
// drawer parts together and "all" is the earlier panel; neither shows a question form twice.
type Inspect = '' | 'context' | 'usage' | 'memory' | 'rewind';
type View = 'all' | 'conversation' | 'details' | 'activity' | 'task' | 'context' | 'changes';
const props = withDefaults(defineProps<{ chat: Chat; memory?: State['memory']; view?: View; focus?: { what: string; n: number } | null }>(), { view: 'all', focus: null });
const emit = defineEmits<{ refresh: []; error: [message: string]; details: []; settled: [] }>();
const opening = () => (props.view === 'context' ? 'usage' : '') as Inspect;
const inspect = ref<Inspect>(opening());
watch(() => props.chat.id, () => (inspect.value = opening()));
// A slash command (or another window control) can open one inspector for the current run.
watch(() => props.focus, (focus) => {
  if (focus && ['context', 'usage', 'memory', 'rewind'].includes(focus.what)) inspect.value = focus.what as Inspect;
}, { immediate: true });
const busy = ref('');
const choices = reactive<Record<string, Record<string, string>>>({});
const texts = reactive<Record<string, Record<string, string>>>({});
const pending = computed(() => props.chat.questions?.filter(q => q.status === 'pending') || []);
const current = computed(() => {
  const runs = props.chat.runs || [];
  return runs.find(r => ['queued', 'running', 'retry_wait'].includes(r.state))
    || runs.find(r => pending.value.some(q => q.run_id === r.id))
    || runs.find(r => r.state === 'waiting_approval') || runs[0];
});
// Phase 45 (F141): the message that asked for a run (the person's last message at its start), to name it by
function requestFor(run: { created?: number } | undefined) {
  if (!run || typeof run.created !== 'number') return '';
  const asked = (props.chat.messages || []).filter(m => m.role === 'user' && typeof m.created === 'number' && m.created <= run.created! + 2);
  const last = asked.at(-1);
  return last && typeof last.content === 'string' ? last.content : '';
}
const canPause = computed(() => ['queued', 'running', 'retry_wait', 'waiting_input', 'waiting_approval'].includes(current.value?.state || ''));
// Phase 24 (V1-029): a task held at "verification required" gets three decisions instead of Resume, which only
// repeated the verdict: run the tests (through the normal approval), ask Pebble to finish (one fresh repair), or
// accept it as unverified (recorded as yours). Main window only: the shell gives these routes this window's label.
const canResume = computed(() => ['paused', 'stopped', 'interrupted', 'failed', 'budget_exhausted'].includes(current.value?.state || ''));
const held = computed(() => current.value?.state === 'verification_required');
const accepting = ref(false);
watch(() => current.value?.id, () => (accepting.value = false));
// Phase 24 (V1-108): each state as a sentence, never the internal name.
const SENTENCES: Record<string, string> = {
  queued: 'Pebble is about to start.', running: 'Pebble is working on this.', retry_wait: 'Pebble is reconnecting.',
  waiting_approval: 'Waiting for your approval.', waiting_input: 'Waiting for your answer.', paused: 'Paused.',
  stopped: 'Stopped.', failed: 'Stopped by an error.', interrupted: 'Interrupted when Pebble closed.',
  budget_exhausted: "Reached this task's limit.", needs_reconciliation: 'One step has an uncertain outcome. Check it before resuming.',
  verification_required: "Pebble's change isn't verified yet.", completed: 'Done.',
};
// Phase 45 (F236, audit round R10): a run that ended on "I'll clear the calculator and enter 123 × 456 again" said
// "Done."; a finished run whose note is runner.UNFINISHED_NOTE (its reply says the work remains) ends without it
const UNFINISHED = "Pebble's reply says the task isn't finished.";
// Phase 45 (F254, audit round R11): after Done on the paused window, the uncertain step reads as checked
const checked = ref<Set<string>>(new Set());
const sentence = computed(() => current.value?.state === 'completed' && current.value?.error === UNFINISHED
  ? 'Ended without finishing.'
  : current.value?.state === 'needs_reconciliation' && checked.value.has(current.value.id)
    ? 'Checked: Pebble can use the window again. Send a message to continue.'
    : SENTENCES[current.value?.state || ''] || 'This task is in an unknown state.');
// Phase 45 (F070, F087; audit rounds R2 and R3): a run's note is written for the model ("Do not claim ...", "Never say
// it plays", "ScreenAct focus on its Address row"); the person sees only the sentences meant for them. The same in
// PetRunSummary.vue and RunPanel.vue (personNote.test.mjs holds them equal).
const FOR_THE_MODEL = /^(Do not|Don't|Never|Say |Tell the person|Check the|Otherwise)/;
const TOOL_NAME = /\b(ScreenAct|ScreenObserve|ScreenRequest|BrowserRequest|OpenURL|OpenApp|AppAction|RunTests|RunCode|ReadFile|WriteFile|EditFile|Shell)\b/;
function personNote(text: unknown) {
  return String(text || "").split(/(?<=\.)\s+/).filter((s) => !FOR_THE_MODEL.test(s) && !TOOL_NAME.test(s)).join(" ");
}
const note = computed(() => personNote(current.value?.error));
const conversation = computed(() => props.view === 'conversation');
// States the chat column mentions, with Resume where the task can resume.
const attention = computed(() => ['paused', 'interrupted', 'failed', 'budget_exhausted', 'verification_required', 'needs_reconciliation'].includes(current.value?.state || ''));
// The latest settled task: the one a rewind can put back.
const settled = computed(() => (props.chat.runs || []).find(r => !['queued', 'running', 'retry_wait'].includes(r.state)) || null);
const part = (...views: View[]) => views.includes(props.view);
const show = computed(() => ({
  state: part('all', 'details', 'activity') || (conversation.value && attention.value),
  inspect: part('all', 'details', 'context'),
  rewindButton: part('all', 'details'),
  output: part('all', 'details', 'activity'),
  plan: part('all', 'details', 'task'),
  agents: part('all', 'details', 'task'),
  questions: part('all', 'conversation'),
  questionNote: part('details', 'activity', 'task') && !!pending.value.length,
}));
const visible = computed(() => {
  // Phase 45 (F255): a paused window from an earlier task shows its Done and End access here too
  if (conversation.value) return !!pending.value.length || attention.value || !!paused.value.length;
  if (props.view === 'changes') return !!settled.value;
  return !!current.value || !!pending.value.length;
});
const newLimits = reactive({ max_steps: 128, max_seconds: 1800 });
// Phase 45 (F224, audit round R9): after an uncertain step the window Pebble used is paused, and its Done lived only in
// Details › Tools; the conversation's Needs a check block offers it, so the person can check the window and go on.
type Paused = { session_id: string; app: string };
const paused = ref<Paused[]>([]);
async function loadPaused() {
  paused.value = [];
  // Phase 45 (F255, audit round R11): a later task's reply pointed to this block, which showed only while the paused
  // task was the latest: a paused window in this conversation keeps its Done and End access here
  if (!conversation.value || !(props.chat.runs || []).some((r) => r.state === 'needs_reconciliation')) return;
  try {
    const rows = await rpc<unknown>('screen_sessions', { id: props.chat.id });
    paused.value = (Array.isArray(rows) ? rows : []).filter((r: any) => r && r.state === 'interrupted'
      && typeof r.session_id === 'string').map((r: any) => ({ session_id: r.session_id,
      app: typeof r.target?.bundle_id === 'string' && r.target.bundle_id ? String(r.target.bundle_id).split('.').pop() || 'The app' : 'The app' }));
  } catch { paused.value = []; }
}
watch(() => [props.chat.id, current.value?.id, current.value?.state], loadPaused, { immediate: true });
// Phase 45 (F239, audit round R10): these two routes take the session alone (as in Details › Tools); action() adds the
// conversation's id, and Done answered "App._route_table.<locals>.<lambda>() got an unexpected keyword argument 'id'"
async function decidePaused(item: Paused, done: boolean) {
  if (busy.value) return;
  busy.value = done ? 'paused-done' : 'paused-end';
  try {
    await rpc(done ? 'screen_handoff_done' : 'screen_session_revoke', { session_id: item.session_id });
    if (current.value) checked.value = new Set([...checked.value, current.value.id]);
    emit('settled');
    emit('refresh');
  } catch (e) { emit('error', String(e)); }
  finally { busy.value = ''; }
  await loadPaused();
}

function choice(question: string, item: string, value: string) {
  (choices[question] ||= {})[item] = value;
}
function text(question: string, item: string, value: string) {
  (texts[question] ||= {})[item] = value;
}
function answerable(run: string) {
  return !['stopped', 'failed', 'budget_exhausted', 'verification_required', 'needs_reconciliation', 'completed'].includes(props.chat.runs?.find(r => r.id === run)?.state || '');
}
function ready(question: typeof pending.value[number]) {
  return question.items.every(item => choices[question.id]?.[item.id] && (choices[question.id][item.id] !== '__text' || texts[question.id]?.[item.id]?.trim()));
}
async function action(key: string, method: string, params: Record<string, unknown>) {
  if (busy.value) return;
  busy.value = key;
  try { await rpc(method, { id: props.chat.id, ...params }); emit('refresh'); }
  catch (e) { emit('error', String(e)); }
  finally { busy.value = ''; }
}
function submit(question: typeof pending.value[number]) {
  const answers = Object.fromEntries(question.items.map(item => [item.id,
    choices[question.id]?.[item.id] === '__text'
      ? { text: texts[question.id]?.[item.id] || '' }
      : { option_id: choices[question.id]?.[item.id] }]));
  return action(question.id, 'question_answer', { question_id: question.id, revision: question.revision, answers });
}
</script>

<template>
  <section v-if="visible" class="run-panel" :class="{ 'run-conversation': conversation }" aria-label="Task controls">
    <div v-if="current && show.state" class="run-state">
      <span role="status">{{ sentence }}<template v-if="conversation && note"> · {{ note }}</template></span>
      <button v-if="canPause && !conversation" class="secondary-button" :disabled="!!busy" @click="action('pause', 'run_pause', { run_id: current.id })">Pause</button>
      <button v-if="canResume" class="secondary-button" :disabled="!!busy" @click="action('resume', 'run_resume', { run_id: current.id, ...(current.state === 'budget_exhausted' ? { limits: newLimits } : {}) })">Resume</button>
      <button v-if="conversation" type="button" class="text-button run-details" @click="emit('details')">Details</button>
    </div>
    <div v-for="item in paused" :key="item.session_id" class="run-held" role="group" :aria-label="'Paused window: ' + item.app">
      <span>{{ item.app }}'s window is paused because the last step's result is uncertain. Look at it: if it looks right, press Done and Pebble can use it again; then send a message to continue.</span>
      <button type="button" class="secondary-button" :disabled="!!busy" @click="decidePaused(item, true)">Done</button>
      <button type="button" class="text-button" :disabled="!!busy" @click="decidePaused(item, false)">End access</button>
    </div>
    <div v-if="current && held && show.state" class="run-held" role="group" aria-label="Finish this task">
      <template v-if="!accepting">
        <!-- Phase 43 (1.9.0): tests run only in a project; a Companion task offers the other two decisions -->
        <button v-if="chat.workspace" type="button" class="secondary-button" :disabled="!!busy" @click="action('held-tests', 'run_held_tests', { run_id: current.id })">Run the tests</button>
        <button type="button" class="secondary-button" :disabled="!!busy" @click="action('held-finish', 'run_held_finish', { run_id: current.id })">Ask Pebble to finish</button>
        <button type="button" class="text-button" :disabled="!!busy" @click="accepting = true">Accept as unverified</button>
      </template>
      <template v-else>
        <span>Keep this change without verification? Pebble records that you decided.</span>
        <button type="button" class="secondary-button" :disabled="!!busy" @click="action('held-accept', 'run_held_accept', { run_id: current.id }); accepting = false">Accept as unverified</button>
        <button type="button" class="text-button" :disabled="!!busy" @click="accepting = false">Cancel</button>
      </template>
    </div>
    <div v-if="current?.state === 'budget_exhausted' && show.state" class="run-limits">
      <label>Step limit <input v-model.number="newLimits.max_steps" type="number" min="1" max="256" /></label>
      <label>Active seconds <input v-model.number="newLimits.max_seconds" type="number" min="1" max="86400" /></label>
    </div>
    <div v-if="current && show.inspect" class="run-inspect">
      <button type="button" class="secondary-button" :aria-expanded="inspect === 'context'" aria-controls="run-inspector" @click="inspect = inspect === 'context' ? '' : 'context'">Context</button>
      <button type="button" class="secondary-button" :aria-expanded="inspect === 'usage'" aria-controls="run-inspector" @click="inspect = inspect === 'usage' ? '' : 'usage'">Context use</button>
      <button type="button" class="secondary-button" :aria-expanded="inspect === 'memory'" aria-controls="run-inspector" @click="inspect = inspect === 'memory' ? '' : 'memory'">Memory impact</button>
      <button v-if="show.rewindButton && !['queued', 'running', 'retry_wait'].includes(current.state)" type="button" class="secondary-button" :aria-expanded="inspect === 'rewind'" aria-controls="run-inspector" @click="inspect = inspect === 'rewind' ? '' : 'rewind'">Rewind files</button>
    </div>
    <div v-if="current && show.inspect && inspect && (inspect !== 'rewind' || show.rewindButton)" id="run-inspector">
      <ContextInspector v-if="inspect === 'context'" :chat-id="chat.id" :run-id="current.id" />
      <ContextPanel v-else-if="inspect === 'usage'" :chat-id="chat.id" :run-id="current.id" :request="requestFor(current)" @refresh="emit('refresh')" @error="emit('error', $event)" />
      <MemoryImpactView v-else-if="inspect === 'memory'" :chat-id="chat.id" :run-id="current.id" :request="requestFor(current)" :layers="memory?.types" :enabled="memory?.enabled" />
      <RewindPanel v-else :chat-id="chat.id" :run-id="current.id" @refresh="emit('refresh')" @error="emit('error', $event)" />
    </div>
    <RewindPanel v-if="view === 'changes' && settled" :chat-id="chat.id" :run-id="settled.id" @refresh="emit('refresh')" @error="emit('error', $event)" />
    <template v-if="show.output">
      <p v-if="note" class="run-note">{{ note }}</p>
      <ReconcilePanel v-if="current?.state === 'needs_reconciliation'" :chat-id="chat.id" :run-id="current.id" @refresh="emit('refresh')" @error="emit('error', $event)" />
      <ExecutionOutput :chat-id="chat.id" :run-id="current?.id" @error="emit('error', $event)" />
      <AttemptStream :chat-id="chat.id" :run-id="current?.id" />
    </template>
    <ol v-if="show.plan && chat.plan && chat.plan.run_id === current?.id" class="run-plan" aria-label="Agent task plan">
      <li v-for="step in chat.plan.steps" :key="step.id"><span>{{ step.title }}</span><small>{{ step.status.replaceAll('_', ' ') }}{{ step.evidence_ids.length ? ' · evidence linked' : '' }}</small></li>
    </ol>
    <p v-if="show.questionNote" class="run-note">{{ pending.length === 1 ? 'A question is' : pending.length + ' questions are' }} waiting for your answer in the conversation.</p>
    <form v-for="question in (show.questions ? pending : [])" :key="question.id + ':' + question.revision" class="run-question" @submit.prevent="submit(question)">
      <p v-if="question.requester_agent_id">Requested by {{ question.requester_agent_name || question.requester_agent_id }}<template v-if="question.parent_task_id"> · Task: {{ question.parent_task_id }}</template></p>
      <p>{{ question.required ? (question.requester_agent_id ? 'This agent needs your answer to continue. Other agents can keep working.' : 'Your answer is needed to continue.') : 'You can answer while other work continues.' }}</p>
      <fieldset v-for="item in question.items" :key="item.id" :disabled="!!busy || !answerable(question.run_id)">
        <legend>{{ item.title }}</legend>
        <label v-for="option in item.options" :key="option.id" class="run-choice">
          <input type="radio" :name="question.id + ':' + item.id" :value="option.id" :checked="choices[question.id]?.[item.id] === option.id" @change="choice(question.id, item.id, option.id)" />
          <span>{{ option.label }}<small v-if="option.description">{{ option.description }}</small></span>
        </label>
        <label class="run-choice"><input type="radio" :name="question.id + ':' + item.id" value="__text" :checked="choices[question.id]?.[item.id] === '__text'" @change="choice(question.id, item.id, '__text')" />Write an answer</label>
        <textarea :aria-label="item.title + ' — written answer'" :value="texts[question.id]?.[item.id] || ''" maxlength="2000" rows="2" @input="text(question.id, item.id, ($event.target as HTMLTextAreaElement).value); choice(question.id, item.id, '__text')" />
      </fieldset>
      <div class="run-question-actions">
        <button type="submit" class="primary-button" :disabled="!!busy || !ready(question) || !answerable(question.run_id)">{{ busy === question.id ? 'Saving…' : 'Submit answer' }}</button>
        <button type="button" class="secondary-button" :disabled="!!busy" @click="action(question.id, 'question_cancel', { question_id: question.id, revision: question.revision })">{{ question.required ? 'Cancel request' : 'Skip' }}</button>
      </div>
    </form>
    <AgentPanel v-if="show.agents" :chat-id="chat.id" :run-id="current?.id" :run-state="current?.state" :refresh-key="chat" @refresh="emit('refresh')" />
  </section>
</template>

<style scoped>
.run-panel { margin: 12px 0; font-size: 13px; min-width: 0; }
.run-state, .run-question-actions, .run-inspect, .run-held { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.run-held { margin-top: 8px; }
.run-inspect { margin-top: 8px; }
.run-state > span { flex: 1; }
.run-question { padding: 14px; margin-top: 12px; border: 1px solid var(--border, #b9b9b9); border-radius: 12px; }
.run-question p, .run-note { font-size: 12px; margin: 0 0 12px; }
.run-question fieldset { border: 0; padding: 0; margin: 0 0 16px; min-width: 0; }
.run-question legend { font-weight: 600; margin-bottom: 8px; overflow-wrap: anywhere; }
.run-choice { display: flex; gap: 8px; align-items: start; margin: 8px 0; }
.run-choice small, .run-plan small { display: block; opacity: .7; }
.run-question textarea { width: 100%; box-sizing: border-box; font: inherit; color: inherit; background: transparent; border: 1px solid var(--border, #b9b9b9); border-radius: 6px; padding: 8px; }
.run-plan { padding-left: 22px; }
.run-plan li { margin: 8px 0; }
.run-limits { display: flex; gap: 8px; margin: 12px 0; }
.run-limits input { width: 80px; font: inherit; }
.run-conversation { margin: 0 0 18px 42px; }
.run-conversation .run-state { font-size: 12px; color: var(--secondary); }
.run-conversation .run-state > span { overflow-wrap: anywhere; }
.run-details { margin-top: 0; font-size: 11px; color: var(--accent); }
</style>
