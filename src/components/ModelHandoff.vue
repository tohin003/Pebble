<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { rpc } from '../bridge';

// Reviewed handoff of a paused or interrupted conversation to another model
// route. The preview is content-free: counts, message IDs, route classes and a
// fingerprint prefix. Nothing is sent until the person approves this exact
// plan and starts a new run; the host refuses the start if the destination
// changed since approval. Main window only: generated views and mods never
// render this component or reach its routes.
type Table = { classes: string[]; rows: { role: string; cells: number[] }[] };
type Plan = { fingerprint: string; source: string; destination: string; prefix: string; counts: Table; withheld: string[]; released: string[] };
type Reply = Record<string, unknown>;
// Phase 24 (V1-002): `release` names route classes the person may include in this handoff (the workspace's
// "Review…" on the on-device notice passes ['device']). It only changes what the preview plans; nothing is sent
// until the person approves this exact plan and starts it.
const props = defineProps<{ chatId: string; runId: string; profileId: string; release?: string[] }>();
const emit = defineEmits<{ started: [runId: string, chatId: string]; error: [message: string] }>();
const plan = ref<Plan | null>(null);
const approved = ref('');
const started = ref(false);
const busy = ref('');
const status = ref('');
let generation = 0;

const ROLES: Record<string, string> = { user: 'Your messages', assistant: 'Pebble’s replies', host: 'Host records' };
const SHOWN_IDS = 50;
const roleName = (role: string) => Object.hasOwn(ROLES, role) ? ROLES[role] : role;
const className = (value: string) => value === 'device' ? 'This device' : value === 'unknown' ? 'An unrecorded route'
  : value.startsWith('cloud:') ? value.slice(6) : value;
const word = (value: unknown) => typeof value === 'string' ? value : '';
const words = (value: unknown) => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
const reply = (value: unknown): Reply => value && typeof value === 'object' && !Array.isArray(value) ? value as Reply : {};
const total = computed(() => plan.value ? plan.value.counts.rows.reduce((sum, row) => sum + row.cells.reduce((a, b) => a + b, 0), 0) : 0);
const ready = computed(() => !!plan.value && approved.value === plan.value.fingerprint && !started.value);
const upload = computed(() => !!plan.value && plan.value.source !== plan.value.destination && plan.value.destination.startsWith('cloud:'));
const withheldText = computed(() => {
  const count = plan.value?.withheld.length || 0;
  return count === 0 ? 'No message is withheld.' : count === 1 ? 'Withheld: 1 message from another route class. It is not sent.'
    : 'Withheld: ' + count + ' messages from another route class. They are not sent.';
});

function table(value: unknown): Table {
  const source = reply(value);
  const roles = Object.keys(source).sort();
  const classes = [...new Set(roles.flatMap(role => Object.keys(reply(source[role]))))].sort();
  const cell = (role: string, name: string) => {
    const row = reply(source[role]);
    const count = Object.hasOwn(row, name) ? row[name] : 0;
    return typeof count === 'number' && Number.isFinite(count) && count >= 0 ? count : 0;
  };
  return { classes, rows: roles.map(role => ({ role, cells: classes.map(name => cell(role, name)) })) };
}
function review(value: unknown): Plan {
  const item = reply(value), fingerprint = item.plan_fingerprint;
  if (typeof fingerprint !== 'string' || !fingerprint) throw new Error('Pebble could not prepare this handoff. Nothing was sent.');
  return { fingerprint, source: word(item.source_class), destination: word(item.destination_class),
    prefix: word(item.destination_fingerprint_prefix).slice(0, 32), counts: table(item.counts),
    withheld: words(item.withheld_ids), released: words(item.released_classes) };
}

async function load() {
  const version = ++generation;
  plan.value = null; approved.value = ''; started.value = false;
  busy.value = 'preview'; status.value = 'Checking what this handoff would send…';
  try {
    const result = await rpc<unknown>('model_handoff_preview', { id: props.chatId, run_id: props.runId, profile_id: props.profileId,
      ...(props.release?.length ? { release: [...props.release] } : {}) });
    if (version !== generation) return;
    plan.value = review(result);
    status.value = 'Review what would be sent, then approve it.';
  } catch (error) { if (version === generation) { status.value = ''; emit('error', String(error)); } }
  finally { if (version === generation) busy.value = ''; }
}
async function approve() {
  const reviewed = plan.value;
  if (busy.value || !reviewed || started.value) return;
  const version = generation;
  busy.value = 'approve'; status.value = '';
  try {
    const result = reply(await rpc<unknown>('model_handoff_approve', { id: props.chatId, plan_fingerprint: reviewed.fingerprint }));
    if (version !== generation) return;
    if (result.approved !== true || result.plan_fingerprint !== reviewed.fingerprint)
      throw new Error('Pebble did not confirm approval of this exact handoff. Nothing was sent.');
    approved.value = reviewed.fingerprint;
    status.value = 'Approved. Nothing has been sent yet.';
  } catch (error) { if (version === generation) emit('error', String(error)); }
  finally { if (version === generation) busy.value = ''; }
}
async function start() {
  const reviewed = plan.value;
  if (busy.value || !reviewed || !ready.value) return;
  const version = generation;
  busy.value = 'start'; status.value = '';
  try {
    const result = reply(await rpc<unknown>('model_handoff_start', { id: props.chatId, plan_fingerprint: reviewed.fingerprint }));
    if (version !== generation) return;
    const run = result.run_id;
    // A start is an effect and is never repeated from here: an unreported run
    // needs a fresh review after checking this conversation's tasks.
    started.value = true; approved.value = '';
    if (typeof run !== 'string' || !run) throw new Error('Pebble did not report a new run. Check this conversation’s tasks before reviewing the handoff again.');
    status.value = 'Started a new run on ' + className(reviewed.destination) + '.';
    emit('started', run, word(result.chat_id));  // The new run lives in a new conversation.
  } catch (error) { if (version === generation) emit('error', String(error)); }
  finally { if (version === generation) busy.value = ''; }
}
watch([() => props.chatId, () => props.runId, () => props.profileId, () => (props.release || []).join()], () => { void load(); });
onMounted(load);
onBeforeUnmount(() => { generation++; });
</script>

<template>
  <section class="model-handoff" aria-label="Reviewed model handoff" :aria-busy="!!busy">
    <strong>Continue on another model</strong>
    <p>Pebble can continue this conversation as a new run on the model you chose. Nothing is sent until you approve this exact handoff and start it. History from another route class is withheld unless it is released. The current run stays as it is. The new route checks its own settings again, and this handoff grants no permission.</p>
    <p class="handoff-status" role="status" aria-live="polite">{{ status }}</p>
    <template v-if="plan">
      <dl>
        <dt>From</dt><dd>{{ className(plan.source) }}</dd>
        <dt>To</dt><dd>{{ className(plan.destination) }}</dd>
        <dt>Destination fingerprint</dt><dd>{{ plan.prefix }}…</dd>
      </dl>
      <p v-if="upload" class="handoff-warning">The messages counted below will be uploaded to {{ className(plan.destination) }}.</p>
      <table v-if="plan.counts.rows.length">
        <caption>Messages that will be sent: {{ total }}</caption>
        <thead>
          <tr><th scope="col">Role</th><th v-for="name in plan.counts.classes" :key="name" scope="col">From {{ className(name) }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in plan.counts.rows" :key="row.role">
            <th scope="row">{{ roleName(row.role) }}</th>
            <td v-for="(count, index) in row.cells" :key="index">{{ count }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else>No message would be sent.</p>
      <p>{{ withheldText }}</p>
      <details v-if="plan.withheld.length">
        <summary>Withheld message IDs</summary>
        <ul>
          <li v-for="(ident, index) in plan.withheld.slice(0, SHOWN_IDS)" :key="index">{{ ident }}</li>
        </ul>
        <p v-if="plan.withheld.length > SHOWN_IDS">And {{ plan.withheld.length - SHOWN_IDS }} more.</p>
      </details>
      <p v-if="plan.released.length">Released for this handoff, so sent although it came from another route class: {{ plan.released.map(className).join(', ') }}.</p>
    </template>
    <div class="setting-actions">
      <button v-if="plan" type="button" class="secondary-button" :disabled="!!busy || started || approved === plan.fingerprint" @click="approve">{{ approved === plan.fingerprint ? 'Approved' : 'Approve this handoff' }}</button>
      <button v-if="plan" type="button" class="primary-button" :disabled="!!busy || !ready" @click="start">Start the new run</button>
      <button type="button" class="secondary-button" :disabled="!!busy" @click="load">Review again</button>
    </div>
  </section>
</template>

<style scoped>
.model-handoff { padding: 14px; margin-top: 12px; border: 1px solid var(--border); border-radius: 12px; font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
.model-handoff > strong { display: block; font-size: 13px; }
.model-handoff p { margin: 8px 0; }
.model-handoff .setting-actions { flex-wrap: wrap; }
.model-handoff dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 8px 0; }
.model-handoff dt { color: var(--muted); }
.model-handoff dd { margin: 0; overflow-wrap: anywhere; }
.model-handoff table { border-collapse: collapse; margin: 8px 0; font-size: 11px; }
.model-handoff caption { text-align: left; font-weight: 600; margin-bottom: 4px; }
.model-handoff th, .model-handoff td { border: 1px solid var(--border); padding: 4px 8px; text-align: left; overflow-wrap: anywhere; }
.model-handoff td { text-align: right; }
.handoff-warning { color: var(--danger); }
.model-handoff details ul { margin: 6px 0; padding-left: 18px; font-size: 11px; }
</style>
