<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue';
import { rpc } from '../bridge';

// The host classification of one pending connector call (payload.effect). It
// sends nothing: App.vue keeps Approve and Don't allow, and the broker decides.
// Its one native route confirms exactly the reviewed recipient set when some
// recipients did not come from the person. Recipients, subject, body and
// connector names come from the model or the connector: they are untrusted
// data and render as literal text.
type Data = Record<string, unknown>;
type Row = [string, string];
interface ReviewedAction { id?: string; kind: string; status?: string; payload?: unknown }
const props = defineProps<{ action: ReviewedAction; chatId: string; disabled?: boolean }>();
const emit = defineEmits<{ confirmed: [actionId: string] }>();

const MAX_RECIPIENTS = 32;
const untrusted = 'Recipients, subject, message and names come from the model and the connector. They are shown as plain text and approve nothing.';
const CLASSES: Record<string, [string, string]> = {
  read: ['Read', 'Reads data from the connected account. Nothing is changed or sent.'],
  draft: ['Draft', 'Saves a draft only. Nothing is delivered to anyone.'],
  send: ['Send', 'Delivers to the recipients below when you approve. A sent message cannot be recalled.'],
  modify: ['Change', 'Changes existing data in the connected account when you approve.'],
  delete: ['Delete', 'Removes data from the connected account when you approve.'],
};
const ASSIGNED: Record<string, string> = {
  person: 'This kind comes from the connector effects you reviewed.',
  default: 'This tool is not in the connector effects you reviewed, so Pebble treats it as a send.',
  unreviewed: 'This connector has no reviewed effects, so Pebble treats every call as a send it cannot check.',
};
const EXPOSURE: Record<string, string> = {
  host_only: 'credential held by Pebble only',
  reviewed_route_placeholder_only: 'credential routed through Pebble; the server holds a placeholder',
  environment_readable_by_local_commands: "credential in the local server's environment, readable by other programs you run",
};
const SOURCES: Record<string, string> = { person: 'named by you', untrusted_output: 'found in tool or page output, not from you', unknown: 'not from you' };
const IDEMPOTENCY: Record<string, string> = {
  host_injected: 'Pebble adds a one-time key, so the service can ignore a repeat of this exact call.',
  none: 'No one-time key. If the outcome is uncertain, Pebble checks it instead of retrying.',
};
const busy = ref(false), status = ref(''), problem = ref(''), confirmedKey = ref('');
let disposed = false;

function record(value: unknown): Data { return value && typeof value === 'object' && !Array.isArray(value) ? value as Data : {}; }
function text(value: unknown, limit = 600) {
  const result = typeof value === 'string' ? value : typeof value === 'number' && Number.isFinite(value) ? String(value) : '';
  return result.length > limit ? result.slice(0, limit) + '…' : result;
}
const own = (map: Record<string, string>, key: string) => Object.hasOwn(map, key) ? map[key] : '';
const hash = (value: unknown) => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value) ? value : '';
const short = (value: string) => value ? value.slice(0, 12) + '…' : '';

const effect = computed(() => {
  const action = props.action, raw = record(action?.payload).effect;
  if (action?.kind !== 'plugin' || !raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const e = raw as Data, connector = record(e.connector), sources = record(e.recipient_sources);
  const kind = typeof e.class === 'string' && Object.hasOwn(CLASSES, e.class) ? e.class : 'send';
  const listed = Array.isArray(e.recipients) ? e.recipients : [];
  const unconfirmed = new Set((Array.isArray(e.unconfirmed_recipients) ? e.unconfirmed_recipients : []).map(item => text(item, 320)));
  const recipients = listed.slice(0, MAX_RECIPIENTS).map(item => {
    const address = text(item, 320), source = typeof item === 'string' ? text(sources[item], 40) : '';
    return { address, source: own(SOURCES, source) || SOURCES.unknown, outside: source !== 'person' };
  });
  return {
    kind, name: CLASSES[kind][0], note: CLASSES[kind][1],
    assigned: own(ASSIGNED, text(e.assigned, 40)) || ASSIGNED.unreviewed,
    connector: text(connector.name, 200), exposure: own(EXPOSURE, text(connector.credential_exposure, 80)),
    count: typeof e.recipient_count === 'number' && Number.isInteger(e.recipient_count) && e.recipient_count >= 0 ? e.recipient_count : listed.length,
    recipients, hidden: Math.max(0, listed.length - MAX_RECIPIENTS), recipientsSha: hash(e.recipients_sha256),
    unconfirmed: [...unconfirmed].filter(Boolean),
    destination: text(e.destination, 300), subject: text(e.subject, 300), body: text(e.body_preview, 300), bodySha: hash(e.body_sha256),
    idempotency: own(IDEMPOTENCY, text(e.idempotency, 40)) || 'Not reported.',
    repeats: (Array.isArray(e.repeat_of) ? e.repeat_of : []).slice(0, 4).map(item => text(item, 64)).filter(Boolean),
    requiresConfirmation: e.requires_confirmation === true,
  };
});
const facts = computed<Row[]>(() => {
  const e = effect.value;
  if (!e) return [];
  const used = text(record(record(props.action?.payload).result).idempotency, 80);
  return ([
    ['Kind', e.name],
    ['Connector', e.connector + (e.exposure ? ' · ' + e.exposure : '')],
    ['Destination', e.destination],
    ['Subject', e.subject],
    ['Recipients', e.count ? e.count + (e.count === 1 ? ' address' : ' addresses') + ' (listed below)' : e.kind === 'send' || e.kind === 'draft' ? 'none' : ''],
    ['Recipient set', short(e.recipientsSha)],
    ['Message SHA-256', short(e.bodySha)],
    ['Repeat protection', e.idempotency],
    ['One-time key sent', used.startsWith('pebble-') ? used : ''],
  ] as Row[]).filter(([, value]) => value !== '');
});
const pending = computed(() => props.action?.status === 'pending');
const key = () => (props.action?.id || '') + ':' + (effect.value?.recipientsSha || '');
const confirmed = computed(() => !!effect.value?.recipientsSha && confirmedKey.value === key());
const verb = computed(() => effect.value?.kind === 'send' ? 'send to them' : 'run this call with them');

async function confirm() {
  const e = effect.value, id = props.action?.id, asked = key();
  if (!e || !id || !e.recipientsSha || !e.requiresConfirmation || !pending.value || busy.value || props.disabled || confirmed.value) return;
  busy.value = true; status.value = ''; problem.value = '';
  try {
    await rpc('connector_send_confirm', { id: props.chatId, action_id: id, recipients_sha256: e.recipientsSha });
    if (disposed || asked !== key()) return;
    confirmedKey.value = asked;
    status.value = 'Recipients confirmed. Nothing was sent; approve the call to ' + (e.kind === 'send' ? 'send it.' : 'run it.');
    emit('confirmed', id);
  } catch (cause) {
    if (!disposed && asked === key()) problem.value = ((cause instanceof Error ? cause.message : String(cause)) || 'The request failed.').slice(0, 1000);
  } finally { busy.value = false; }
}
onBeforeUnmount(() => { disposed = true; });
</script>

<template>
  <section v-if="effect" class="connector-effect" :aria-label="'Connector effect: ' + effect.name">
    <p class="effect-untrusted">{{ untrusted }}</p>
    <p class="effect-title"><strong>{{ effect.name }}</strong> · {{ effect.note }}</p>
    <p class="effect-note">{{ effect.assigned }}</p>
    <dl class="effect-facts" aria-label="Connector effect facts"><template v-for="[name, value] in facts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
    <template v-if="effect.recipients.length">
      <p class="effect-label">Recipients</p>
      <ul class="effect-list" aria-label="Recipients">
        <li v-for="(item, index) in effect.recipients" :key="index" :class="{ outside: item.outside }">{{ item.address }} · {{ item.source }}</li>
      </ul>
      <p v-if="effect.hidden" class="effect-note">{{ effect.hidden }} more not shown.</p>
    </template>
    <template v-if="effect.body">
      <p class="effect-label">Message preview (plain text)</p>
      <pre class="effect-pre">{{ effect.body }}</pre>
    </template>
    <p v-if="effect.repeats.length" class="effect-alert">An identical {{ effect.name.toLowerCase() }} already completed (action {{ effect.repeats.join(', ') }}). Approving does it again.</p>
    <div v-if="effect.requiresConfirmation" class="effect-confirm" role="group" aria-label="Confirm recipients">
      <p><strong>Some recipients didn't come from you.</strong> Pebble won't {{ verb }} unless you confirm these recipients here:</p>
      <ul class="effect-list"><li v-for="(item, index) in effect.unconfirmed" :key="index">{{ item }}</li></ul>
      <p>Confirming sends nothing. You still approve the call or choose Don't allow.</p>
      <p v-if="!effect.recipientsSha" class="effect-note">This recipient set can't be confirmed. Choose Don't allow instead.</p>
      <p v-else-if="confirmed" class="effect-note">You confirmed these recipients.</p>
      <button v-else type="button" class="secondary-button" :disabled="busy || disabled || !pending" @click="confirm">Confirm recipients</button>
    </div>
    <p class="effect-status" role="status" aria-live="polite">{{ status }}</p>
    <p v-if="problem" class="effect-problem" role="alert">{{ problem }}</p>
  </section>
</template>

<style scoped>
.connector-effect { margin: 10px 0; min-width: 0; font-size: 10px; line-height: 1.6; overflow-wrap: anywhere; }
.effect-untrusted, .effect-note { margin: 6px 0; color: var(--muted); }
.effect-title { margin: 6px 0; font-size: 11px; }
.effect-facts { display: grid; grid-template-columns: max-content 1fr; gap: 3px 10px; margin: 8px 0; }
.effect-facts dt { color: var(--muted); }
.effect-facts dd { margin: 0; overflow-wrap: anywhere; }
.effect-label { margin: 8px 0 4px; font-weight: 600; }
.effect-list { margin: 0; padding-left: 16px; }
.effect-list li { white-space: pre-wrap; overflow-wrap: anywhere; }
.effect-list li.outside { color: var(--danger); }
.effect-pre { margin: 4px 0; padding: 8px; max-height: 200px; overflow: auto; border-radius: 6px; background: var(--code); font: 10px/1.6 ui-monospace, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.effect-alert, .effect-problem { margin: 6px 0; color: var(--danger); font-weight: 600; }
.effect-confirm { border: 1px solid var(--danger-line); border-radius: 8px; padding: 8px 10px; margin: 8px 0; }
.effect-confirm p { margin: 4px 0; }
.effect-status { margin: 4px 0; }
</style>
