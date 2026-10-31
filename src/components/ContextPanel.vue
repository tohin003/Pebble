<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { rpc } from '../bridge';
import { moveRowFocus } from '../shortcuts';

// Main window only, host chrome (never generated UI): what fills the model's context for this task's current or
// last request, counted by the host with the estimator its window uses, plus a between-turns Compact command.
// The host sends counts and names only, never message text. Compact asks the host to archive older context
// through its existing compaction: nothing runs, no tool is repeated and memory is unchanged. The shell writes the
// calling window's label, so the pet and generated views cannot use these routes.
type Part = { id: string; tokens: number; messages: number };
type Category = { id: string; tokens: number; messages: number; parts: Part[] };
type Tool = { name: string; tokens: number };
type View = {
  runId: string; latestRunId: string | null; runState: string; basis: string; live: boolean; protocol: string;
  total: number; used: number; calibrated: number; window: number | null; windowSource: string; free: number | null;
  over: number | null; categories: Category[]; tools: Tool[]; toolsBasis: string; unidentified: number;
  recorded: { tokens: number | null; method: string; status: string; step: number | null } | null;
  since: { messages: number; tokens: number } | null; compactions: number; compactable: boolean; reason: string;
  screenshots: number;  // Phase 45 (F213): pictures of windows this task sent; never kept
};
type Reply = Record<string, unknown>;
const props = defineProps<{ chatId: string; runId: string; request?: string }>();
// Phase 45 (F141, audit round R4): the heading named the task by its id ("task 4341ed01"); it quotes the message that
// asked for it when the window knows it, the id staying in the tooltip.
const heading = computed(() => {
  const asked = (props.request || '').replace(/\s+/g, ' ').trim();
  return asked ? '“' + (asked.length > 60 ? asked.slice(0, 59) + '…' : asked) + '”' : 'task ' + props.runId.slice(0, 8);
});
const emit = defineEmits<{ refresh: []; error: [message: string] }>();
const TOP_TOOLS = 8;
const view = ref<View | null>(null);
const busy = ref('');
const confirming = ref(false);
const status = ref('');
let generation = 0;

const CATEGORY: Record<string, string> = {
  system: 'System prompt', tool_schemas: 'Tool descriptions', memory: 'Memory recall', project_host: 'Project and Pebble messages',
  conversation: 'Conversation', tool_results: 'Tool results', framing: 'Estimate margin and framing',
};
const PART: Record<string, string> = {
  request: 'Your request', constraints: 'Your earlier instructions (exact)', history: 'Earlier turns', model_turns: "Pebble's replies and tool calls",
  // Phase 24 (V1-108): plain names where no fixture pins the old words.
  evidence: 'Task checkpoint and evidence', plan_goal: 'Plan and goal', inbox: 'Follow-ups and Pebble notes',
  skills_plugins: 'Skills and connectors', sources: 'Project files and attachments', host_notes: 'Pebble notes',
  screenshots: 'Screenshots of windows or the screen',  // Phase 45 (audit round R6)
};
const BASIS: Record<string, string> = {
  last_request: 'The last request this task sent to the model.',
  checkpoint: 'The stored window the next request starts from; the model has not seen it in this form yet.',
  none: 'This task has not sent a request yet.',
};
const REASON: Record<string, string> = {
  live: 'Compact between turns: pause the task or wait for it to finish.',
  finished: 'This task is finished, so its window is not used again. Your next message starts a new window; Pebble retires older turns there automatically when it fills, and keeps your exact earlier instructions.',
  not_latest: "Only the conversation's latest task can be compacted.",
  not_resumable: "Reconcile this task's uncertain step first.",
  between_tools: 'This task stopped between tool steps. Resume it to a model boundary before compacting.',
  no_window: 'This task has no stored window yet.',
};
const METHOD: Record<string, string> = {
  o200k_local_estimate: "Pebble's own estimate", responses_input_tokens: "the provider's own count", llama_template_tokens: 'the local model template',
};
const named = (table: Record<string, string>, key: string, fallback = '') => (Object.hasOwn(table, key) ? table[key] : fallback);
const reply = (value: unknown): Reply => (value && typeof value === 'object' && !Array.isArray(value) ? (value as Reply) : {});
const list = (value: unknown) => (Array.isArray(value) ? value.map(reply) : []);
const word = (value: unknown) => (typeof value === 'string' ? value : '');
const whole = (value: unknown) => (typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null);
const num = (value: number | null) => (value === null ? '—' : value.toLocaleString());

function review(value: unknown): View {
  const item = reply(value), window = reply(item.window), calibration = reply(item.calibration), compact = reply(item.compact);
  const run = word(item.run_id);
  if (run !== props.runId || word(item.chat_id) !== props.chatId) throw new Error('The reply belongs to a different task.');
  const total = whole(item.total) ?? 0;
  const recorded = item.recorded ? reply(item.recorded) : null;
  const since = item.since_last_request ? reply(item.since_last_request) : null;
  return {
    runId: run, latestRunId: word(item.latest_run_id) || null, runState: word(item.run_state), basis: word(item.basis),
    live: item.live === true, protocol: word(item.protocol), total, used: whole(item.used) ?? total,
    calibrated: whole(calibration.tokens) ?? 0, window: whole(window.tokens), windowSource: word(window.source),
    free: whole(item.free), over: whole(item.over),
    categories: list(item.categories).filter(c => Object.hasOwn(CATEGORY, word(c.id))).map(c => ({
      id: word(c.id), tokens: typeof c.tokens === 'number' && Number.isInteger(c.tokens) ? c.tokens : 0, messages: whole(c.messages) ?? 0,
      parts: list(c.parts).filter(p => Object.hasOwn(PART, word(p.id))).map(p => ({ id: word(p.id), tokens: whole(p.tokens) ?? 0, messages: whole(p.messages) ?? 0 })),
    })),
    tools: list(item.tools).map(t => ({ name: word(t.name) || 'unnamed', tokens: whole(t.tokens) ?? 0 })),
    toolsBasis: word(item.tools_basis), unidentified: whole(item.tools_unidentified) ?? 0,
    recorded: recorded ? { tokens: whole(recorded.input_tokens), method: word(recorded.method), status: word(recorded.status), step: whole(recorded.step) } : null,
    since: since ? { messages: whole(since.messages) ?? 0, tokens: whole(since.tokens) ?? 0 } : null,
    compactions: whole(item.compactions) ?? 0, compactable: compact.available === true, reason: word(compact.reason),
    screenshots: whole(item.screenshots_sent) ?? 0,
  };
}

// The bar is the window: each category's share, the route's calibration margin, then free space.
const segments = computed(() => {
  const current = view.value;
  if (!current) return [];
  const scale = Math.max(current.window ?? 0, current.used, 1);
  const parts = current.categories.filter(c => c.tokens > 0).map(c => ({ id: c.id, label: CATEGORY[c.id], tokens: c.tokens }));
  if (current.calibrated > 0) parts.push({ id: 'calibration', label: 'Route calibration', tokens: current.calibrated });
  if (current.free) parts.push({ id: 'free', label: 'Free space', tokens: current.free });
  return parts.map(part => ({ ...part, width: Math.min(100, Math.max(0, (part.tokens / scale) * 100)).toFixed(2) + '%' }));
});
const barLabel = computed(() => {
  const current = view.value;
  if (!current) return '';
  return `Context use: ${num(current.used)} of ${num(current.window)} tokens. ` + segments.value.map(s => `${s.label} ${num(s.tokens)}`).join('; ') + '.';
});
const share = (tokens: number) => {
  const window = view.value?.window;
  return window ? Math.round((tokens / window) * 1000) / 10 + '%' : '—';
};
const detailed = computed(() => (view.value?.categories || []).flatMap(c => c.parts.filter(p => p.messages > 0).map(p => ({ ...p, category: c.id }))));
const topTools = computed(() => (view.value?.tools || []).slice(0, TOP_TOOLS));
const otherTools = computed(() => {
  const rest = (view.value?.tools || []).slice(TOP_TOOLS);
  return { count: rest.length, tokens: rest.reduce((sum, t) => sum + t.tokens, 0) };
});
const summary = computed(() => {
  const current = view.value;
  if (!current || current.window === null) return current ? `${num(current.used)} tokens; the window is not known.` : '';
  return `${num(current.used)} of ${num(current.window)} tokens` + (current.over ? ` · ${num(current.over)} over the window` : ` · ${num(current.free)} free`);
});

async function load() {
  const version = ++generation;
  busy.value = 'inspect';
  try {
    const result = await rpc<unknown>('context_inspect', { chat_id: props.chatId, run_id: props.runId });
    if (version !== generation) return;
    view.value = review(result);
  } catch (error) {
    if (version === generation) { view.value = null; emit('error', String(error)); }
  } finally { if (version === generation) busy.value = ''; }
}
// Focus follows the confirmation: onto "Compact now" when it opens, back to "Compact…" when it closes.
const askButton = ref<HTMLButtonElement | null>(null);
const confirmButton = ref<HTMLButtonElement | null>(null);
async function ask() {
  if (busy.value || !view.value?.compactable) return;
  status.value = '';
  confirming.value = true;
  await nextTick();
  confirmButton.value?.focus?.();
}
async function cancel() {
  confirming.value = false;
  await nextTick();
  askButton.value?.focus?.();
}
async function compact() {
  if (busy.value || !confirming.value || !view.value?.compactable) return;
  const version = ++generation;
  confirming.value = false;
  busy.value = 'compact'; status.value = 'Compacting…';
  let shown = false;
  try {
    const result = reply(await rpc<unknown>('context_compact', { chat_id: props.chatId }));
    if (version !== generation) return;
    const archived = reply(result.archived), tokens = reply(result.tokens), receipt = word(result.receipt_id).slice(0, 8);
    const kind = word(result.status);
    status.value = kind === 'compacted'
      ? `Archived ${num(whole(archived.messages))} messages (${num(whole(archived.tool_results))} tool results) and freed ${num(whole(tokens.freed))} tokens. Your exact instructions, the plan and the latest step were kept. Receipt ${receipt}.`
      : kind === 'already_fits' ? `Nothing was retired: what is left is protected (your instructions, the plan, the latest step). Receipt ${receipt}.`
      : kind === 'fresh_window' ? REASON.finished : 'Pebble did not report the outcome; refresh to check.';
    try { view.value = review(result.context); shown = true; } catch { /* reloaded below */ }
    emit('refresh');
  } catch (error) {
    if (version === generation) { status.value = ''; emit('error', String(error)); }
  } finally { if (version === generation) busy.value = ''; }
  // The window after compaction comes with the receipt; without a usable one, ask the host again.
  if (version === generation && !shown) await load();
}
watch([() => props.chatId, () => props.runId], () => { view.value = null; confirming.value = false; status.value = ''; void load(); });
onMounted(load);
onBeforeUnmount(() => { generation++; });
</script>

<template>
  <section class="context-panel" aria-label="Context use" :aria-busy="!!busy">
    <div class="context-head">
      <strong :title="'Task ' + runId">Context use · {{ heading }}</strong>
      <button type="button" class="secondary-button" :disabled="!!busy" @click="load">{{ busy === 'inspect' ? 'Loading…' : 'Refresh' }}</button>
    </div>
    <p class="context-note">Counted by Pebble from its own records: names and counts only, never message text. Nothing here asks the model.</p>
    <template v-if="view">
      <p>{{ view.live ? 'The request being answered now.' : named(BASIS, view.basis, 'Unknown basis.') }}</p>
      <template v-if="view.basis !== 'none'">
        <p class="context-summary">{{ summary }}</p>
        <div class="context-bar" role="img" :aria-label="barLabel">
          <span v-for="segment in segments" :key="segment.id" :class="['context-seg', 'context-seg-' + segment.id]" :style="{ width: segment.width }" :title="segment.label + ': ' + num(segment.tokens)" />
        </div>
        <div class="context-table"><table>
          <caption>By category (tokens)</caption>
          <thead><tr><th scope="col">Category</th><th scope="col">Tokens</th><th scope="col" title="Share of the context window">Share</th><th scope="col">Messages</th></tr></thead>
          <tbody>
            <tr v-for="(row, i) in view.categories" :key="row.id" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
              <th scope="row"><span class="context-name"><span :class="['context-key', 'context-seg-' + row.id]" aria-hidden="true" />{{ named(CATEGORY, row.id) }}</span></th>
              <td>{{ num(row.tokens) }}</td><td>{{ share(row.tokens) }}</td><td>{{ row.id === 'tool_schemas' ? view.tools.length + ' tools' : row.id === 'framing' ? '—' : num(row.messages) }}</td>
            </tr>
            <tr v-if="view.calibrated" :tabindex="-1" @keydown="moveRowFocus"><th scope="row"><span class="context-key context-seg-calibration" aria-hidden="true" />Route calibration</th><td>{{ num(view.calibrated) }}</td><td>{{ share(view.calibrated) }}</td><td>—</td></tr>
            <tr v-if="view.free !== null" :tabindex="-1" @keydown="moveRowFocus"><th scope="row"><span class="context-key context-seg-free" aria-hidden="true" />Free space</th><td>{{ num(view.free) }}</td><td>{{ share(view.free) }}</td><td>—</td></tr>
          </tbody>
        </table></div>
        <table v-if="detailed.length">
          <caption>Conversation and Pebble messages</caption>
          <thead><tr><th scope="col">Part</th><th scope="col">Tokens</th><th scope="col">Messages</th></tr></thead>
          <tbody>
            <tr v-for="(part, i) in detailed" :key="part.id" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
              <th scope="row">{{ named(PART, part.id) }}</th><td>{{ num(part.tokens) }}</td><td>{{ num(part.messages) }}</td>
            </tr>
          </tbody>
        </table>
        <table v-if="topTools.length">
          <caption>Largest tool descriptions</caption>
          <thead><tr><th scope="col">Tool</th><th scope="col">Tokens</th></tr></thead>
          <tbody>
            <tr v-for="(tool, i) in topTools" :key="tool.name + ':' + i" :tabindex="i === 0 ? 0 : -1" @keydown="moveRowFocus">
              <th scope="row">{{ tool.name }}</th><td>{{ num(tool.tokens) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="otherTools.count" class="context-note">And {{ otherTools.count }} more {{ otherTools.count === 1 ? 'tool' : 'tools' }}, {{ num(otherTools.tokens) }} tokens.</p>
        <p v-if="view.toolsBasis === 'eligible_offer'" class="context-note">No request was recorded, so these are the tools this task could be offered; a step may send fewer.</p>
        <p v-if="view.unidentified" class="context-note">{{ view.unidentified }} {{ view.unidentified === 1 ? 'tool description sent earlier is' : 'tool descriptions sent earlier are' }} no longer stored with this task and {{ view.unidentified === 1 ? 'is' : 'are' }} not counted above.</p>
        <p v-if="view.recorded && view.recorded.tokens !== null" class="context-note">
          Pebble's own count when it sent this window<template v-if="view.recorded.step !== null"> (step {{ view.recorded.step }})</template>: {{ num(view.recorded.tokens) }} tokens, by {{ named(METHOD, view.recorded.method, 'its own estimate') }}<template v-if="view.recorded.status === 'retired'">, after retiring older context</template>.
        </p>
        <p v-if="view.recorded && view.recorded.status === 'exceeded'" class="context-note">The last request did not fit its window and was not sent.</p>
        <p v-if="view.since && view.since.messages" class="context-note">Added since: {{ num(view.since.messages) }} {{ view.since.messages === 1 ? 'message' : 'messages' }}, {{ num(view.since.tokens) }} tokens. They join the next request.</p>
        <p class="context-note">Context archives for this task: {{ view.compactions }}.</p>
        <!-- Phase 45 (F213, audit round R8): the pictures sent during a task are not in the stored window above -->
        <p v-if="view.screenshots" class="context-note">Pictures of windows sent in this task: {{ num(view.screenshots) }}. They are not kept in the conversation, so the window above does not count them.</p>
      </template>
      <div class="context-compact">
        <button v-if="!confirming" ref="askButton" type="button" class="secondary-button" :disabled="!!busy || !view.compactable" :aria-describedby="view.compactable ? undefined : 'context-compact-reason'" @click="ask">Compact…</button>
        <p v-if="!view.compactable" id="context-compact-reason" class="context-note">{{ named(REASON, view.reason, 'Compaction is not available for this task now.') }}</p>
        <div v-if="confirming" role="group" aria-label="Confirm compaction" class="context-confirm">
          <p>Compact this task's window? Pebble moves older tool results and turns into this task's archive (still readable there) and keeps your exact instructions, the plan, its evidence and the latest step. Nothing runs, no tool is repeated and memory is unchanged. The task continues from the compacted window when you resume it.</p>
          <div class="setting-actions">
            <button ref="confirmButton" type="button" class="primary-button" :disabled="!!busy" @click="compact">Compact now</button>
            <button type="button" class="secondary-button" :disabled="!!busy" @click="cancel">Cancel</button>
          </div>
        </div>
      </div>
    </template>
    <p class="context-status" role="status" aria-live="polite">{{ status }}</p>
  </section>
</template>

<style scoped>
.context-panel { padding: 14px; margin-top: 12px; border: 1px solid var(--border, #b9b9b9); border-radius: 12px; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; min-width: 0; }
.context-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
/* Phase 45 (F172, audit round R5): Refresh broke over three lines (Ref/res/h) and "messages" as message/s at the drawer's
   normal width; the button keeps its word, the heading and the category names wrap between words */
.context-head strong { min-width: 0; }
.context-head .secondary-button { flex: none; white-space: nowrap; }
.context-panel tbody th { overflow-wrap: normal; word-break: normal; }
/* Phase 45 (F202, audit round R7): the swatch stays with its name ("Conversation" wrapped below it) */
.context-name { display: inline-flex; align-items: baseline; }
.context-name .context-key { flex: none; }
.context-panel p { margin: 6px 0; }
.context-note { color: var(--muted); font-size: 11px; }
.context-summary { font-weight: 600; }
.context-bar { display: flex; height: 14px; margin: 8px 0; overflow: hidden; border: 1px solid var(--border, #b9b9b9); border-radius: 7px; background: var(--hover, #8881); }
.context-seg { display: block; height: 100%; min-width: 0; }
.context-key { display: inline-block; width: 10px; height: 10px; margin-right: 6px; border-radius: 2px; vertical-align: -1px; }
.context-seg-system { background: #7a7fd6; }
.context-seg-tool_schemas { background: #d68a4f; }
.context-seg-memory { background: #b56bc2; }
.context-seg-project_host { background: #4f9fb8; }
.context-seg-conversation { background: #5aa86b; }
.context-seg-tool_results { background: #c9a53d; }
.context-seg-framing { background: #9a9a9a; }
.context-seg-calibration { background: repeating-linear-gradient(45deg, #9a9a9a 0 3px, transparent 3px 6px); }
.context-seg-free { background: transparent; }
.context-key.context-seg-free { border: 1px solid var(--border, #b9b9b9); }
/* The bar may ease to a new width only when motion is welcome; never under reduced motion. */
@media (prefers-reduced-motion: no-preference) { .context-seg { transition: width 200ms ease-out; } }
@media (prefers-reduced-motion: reduce) { .context-seg { transition: none; } }
:global(.reduced-motion) .context-seg { transition: none; }
.context-panel table { width: 100%; border-collapse: collapse; margin: 10px 0; }
/* F141: numbers and column headers stay whole (2,444 broke as "2,44" over "4", Tokens as "Toke" over "ns"); a table
   wider than the drawer scrolls sideways */
.context-table { overflow-x: auto; }
.context-panel thead th, .context-panel td { white-space: nowrap; overflow-wrap: normal; }
.context-panel caption { text-align: left; font-weight: 600; padding-bottom: 6px; }
.context-panel th, .context-panel td { text-align: left; vertical-align: top; padding: 4px 6px; border-bottom: 1px solid var(--border, #b9b9b9); }
.context-panel th { font-weight: 500; }
.context-panel tr:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.context-compact { margin-top: 12px; }
.context-confirm { padding: 10px; border: 1px solid var(--border, #b9b9b9); border-radius: 8px; }
.context-panel .setting-actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
</style>
