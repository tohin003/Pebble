<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { rpc } from '../bridge';

// Phase 18 E06: opt-in usage telemetry. Off by default, kept only on this Mac, never uploaded. Main window
// only (the native routes refuse the pet and generated views). The notice text comes from the host and
// renders as literal text. Saving a copy uses a single-use token from the native Save dialog, which the
// parent supplies as chooseDestination; this component never handles a file path.
type Status = {
  enabled?: unknown; consent_version?: unknown; consented_at?: unknown; current_consent_version?: unknown;
  consent_text?: unknown; install_id?: unknown; events?: unknown; bytes?: unknown; dropped?: unknown;
  expired?: unknown; refused?: unknown; fields?: unknown; limits?: unknown; destination?: unknown;
};
type Receipt = { events?: unknown; sha256?: unknown; verified?: unknown };
const props = defineProps<{ chooseDestination?: () => Promise<string | null> }>();
const status = ref<Status | null>(null);
const agreed = ref(false);
const busy = ref(false);
const message = ref('');
let alive = true;

const TOKEN = /^[A-Za-z0-9_-]{16,256}$/;
const text = (value: unknown) => typeof value === 'string' ? value : '';
const count = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
const plural = (n: number, word: string) => n + ' ' + word + (n === 1 ? '' : 's');
const enabled = computed(() => status.value?.enabled === true);
const notice = computed(() => text(status.value?.consent_text));
const version = computed(() => text(status.value?.current_consent_version));
const since = computed(() => {
  const t = status.value?.consented_at;
  return typeof t === 'number' && Number.isFinite(t) && t > 0 ? new Date(t * 1000).toLocaleString() : 'unknown';
});
const limits = computed(() => {
  const value = status.value?.limits as Record<string, unknown> | undefined;
  return { events: count(value?.max_events), kb: Math.round(count(value?.max_bytes) / 1024), days: count(value?.retention_days) };
});
const fields = computed(() => {
  const value = status.value?.fields;
  if (!value || typeof value !== 'object') return [] as { kind: string; names: string }[];
  return Object.entries(value as Record<string, unknown>).map(([kind, names]) => ({
    kind: kind.replace(/_/g, ' '),
    names: Array.isArray(names) ? names.filter(n => typeof n === 'string').map(n => n.replace(/_/g, ' ')).join(', ') : '',
  }));
});
const canSave = computed(() => enabled.value && typeof props.chooseDestination === 'function' && !busy.value);

async function run(task: () => Promise<void>) {
  if (busy.value) return;
  busy.value = true;
  try {
    await task();
  } catch (error) {
    if (alive) message.value = String(error);
  } finally {
    if (alive) busy.value = false;
  }
}

function accept(next: unknown) {
  if (!alive) return;
  if (next && typeof next === 'object' && 'enabled' in (next as object)) status.value = next as Status;
}

async function load() {
  accept(await rpc<Status>('telemetry_get'));
}

function turnOn() {
  return run(async () => {
    if (!agreed.value || !version.value) return;
    accept(await rpc<Status>('telemetry_set', { enabled: true, consent_version: version.value }));
    agreed.value = false;
    message.value = enabled.value ? 'Telemetry is on. It stays on this Mac until you save a copy.' : '';
  });
}

function turnOff() {
  return run(async () => {
    accept(await rpc<Status>('telemetry_set', { enabled: false }));
    message.value = 'Telemetry is off and its log was deleted.';
  });
}

function clearLog() {
  return run(async () => {
    accept(await rpc<Status>('telemetry_clear'));
    message.value = 'The log was cleared and the random ID changed.';
  });
}

function save() {
  return run(async () => {
    const choose = props.chooseDestination;
    if (typeof choose !== 'function') return;
    const token = await choose();
    if (token === null || token === undefined) {
      message.value = 'Saving was cancelled.';
      return;
    }
    if (typeof token !== 'string' || !TOKEN.test(token)) {
      message.value = 'The Save dialog did not return a usable destination. Nothing was saved.';
      return;
    }
    const receipt = await rpc<Receipt>('telemetry_export', { destination_token: token });
    const digest = text(receipt?.sha256).slice(0, 12);
    message.value = 'Saved ' + plural(count(receipt?.events), 'event') + (receipt?.verified === true ? ' and checked the copy' : '')
      + (digest ? ' (SHA-256 ' + digest + '…)' : '') + '. Nothing was uploaded.';
    await load();
  });
}

function onAgree(event: Event) {
  agreed.value = (event?.target as HTMLInputElement | null)?.checked === true;
}

onMounted(() => { void run(load); });
onBeforeUnmount(() => { alive = false; });
</script>

<template>
  <section class="telemetry-settings" aria-labelledby="telemetry-heading">
    <h3 id="telemetry-heading">Usage telemetry</h3>
    <p class="setting-note" v-if="!status">Loading…</p>
    <template v-else>
      <p class="setting-note">
        <strong>{{ enabled ? 'On' : 'Off' }}</strong>
        <span v-if="enabled"> since {{ since }} · {{ plural(count(status?.events), 'event') }} kept</span>
        <span v-else> · nothing is collected</span>
        · kept only on this Mac; nothing is uploaded.
      </p>
      <p id="telemetry-notice" class="setting-note telemetry-notice">{{ notice }}</p>
      <p class="setting-note">
        Limits: {{ limits.events }} events or {{ limits.kb }} KB, for {{ limits.days }} days; the oldest are removed first.
      </p>
      <details>
        <summary>What each event can contain</summary>
        <ul aria-label="Telemetry fields">
          <li v-for="item in fields" :key="item.kind">{{ item.kind }}: {{ item.names }}</li>
        </ul>
      </details>
      <div v-if="!enabled" class="telemetry-actions">
        <label>
          <input type="checkbox" :checked="agreed" :disabled="busy" aria-describedby="telemetry-notice" @change="onAgree" />
          I have read this notice and want to turn telemetry on
        </label>
        <button type="button" :disabled="busy || !agreed || !version" @click="turnOn">Turn on</button>
      </div>
      <div v-else class="telemetry-actions">
        <p class="setting-note" v-if="count(status?.dropped) || count(status?.expired) || count(status?.refused)">
          Removed: {{ count(status?.dropped) }} over the limit, {{ count(status?.expired) }} expired, {{ count(status?.refused) }} refused.
        </p>
        <p class="setting-note">Random ID {{ text(status?.install_id) }}; it changes when you clear the log.</p>
        <button type="button" :disabled="!canSave" @click="save">Save a copy…</button>
        <span v-if="typeof chooseDestination !== 'function'" class="setting-note"> Saving needs the native Save dialog.</span>
        <button type="button" :disabled="busy" @click="clearLog">Clear log</button>
        <button type="button" :disabled="busy" @click="turnOff">Turn off and delete the log</button>
      </div>
    </template>
    <p class="telemetry-status" role="status" aria-live="polite">{{ message }}</p>
  </section>
</template>
