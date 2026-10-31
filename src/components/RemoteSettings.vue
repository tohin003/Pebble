<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { rpc, subscribe } from '../bridge';

// Phase 46 (1.9.29, D214, D215): Pebble Remote, this Mac's Pebble controlled from the person's iPhone. Off by default.
// Turning it on, showing a pairing code, confirming a pairing's six digits (X-1) and removing a phone happen only here,
// in the main window: the remote_* routes refuse the companion and generated views (main.rs) and the phone itself
// (remote_policy.MAC_ROUTES). The pairing link lives in this component's memory only, until it is used, expires or is
// cancelled. Text from the bridge (phone names, activity lines) renders as literal text.
type Device = { device_id?: unknown; name?: unknown; paired_at?: unknown; last_seen?: unknown; connected?: unknown };
type Line = { at?: unknown; text?: unknown };
type Status = {
  enabled?: unknown; running?: unknown; error?: unknown; lan?: unknown; keep_awake?: unknown; keep_awake_held?: unknown;
  unlock_minutes?: unknown; unlock_choices?: unknown; listening?: { wifi?: unknown; tailscale?: unknown } | null;
  pairing?: { expires_at?: unknown } | null; confirm?: { code?: unknown; device?: unknown; expires_at?: unknown } | null;
  devices?: unknown; connected?: unknown; activity?: unknown;
};
type Pairing = { link?: unknown; qr?: unknown; expires_at?: unknown; status?: Status };
const status = ref<Status | null>(null);
const pairing = ref<{ link: string; rows: string[]; until: number } | null>(null);
const now = ref(Date.now());
const busy = ref(false);
const message = ref('');
const problem = ref('');
const confirmHeading = ref<HTMLElement | null>(null);
let alive = true;
let clock = 0;
let unsubscribe: () => void = () => {};

const text = (value: unknown) => typeof value === 'string' ? value : '';
const time = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : 0;
const list = (value: unknown) => Array.isArray(value) ? value.filter(v => typeof v === 'string') as string[] : [];
const enabled = computed(() => status.value?.enabled === true);
const running = computed(() => status.value?.running === true);
const wifi = computed(() => list(status.value?.listening?.wifi));
const tailscale = computed(() => list(status.value?.listening?.tailscale));
const listening = computed(() => {
  if (!running.value) return enabled.value ? 'Starting…' : '';
  const where = [wifi.value.length ? 'Wi-Fi (' + wifi.value.join(', ') + ')' : '',
    tailscale.value.length ? 'Tailscale (' + tailscale.value.join(', ') + ')' : ''].filter(Boolean);
  return where.length ? 'Listening on ' + where.join(' and ') + '.'
    : 'No network yet: connect this Mac to Wi-Fi or to Tailscale.';
});
const devices = computed(() => (Array.isArray(status.value?.devices) ? status.value!.devices as Device[] : [])
  .filter(d => d && typeof d === 'object' && text(d.device_id))
  .map(d => ({ id: text(d.device_id), name: text(d.name) || 'iPhone', connected: d.connected === true,
    paired: time(d.paired_at), seen: time(d.last_seen) })));
const activity = computed(() => (Array.isArray(status.value?.activity) ? status.value!.activity as Line[] : [])
  .filter(l => l && typeof l === 'object' && text(l.text)).map(l => ({ at: time(l.at), text: text(l.text) })));
const confirm = computed(() => {
  const c = status.value?.confirm;
  const code = text(c?.code);
  if (!c || !/^\d{6}$/.test(code)) return null;
  return { code: code.slice(0, 3) + ' ' + code.slice(3), digits: code, device: text(c.device) || 'An iPhone',
    until: time(c.expires_at) };
});
const choices = computed(() => {
  const values = Array.isArray(status.value?.unlock_choices) ? (status.value!.unlock_choices as unknown[])
    .filter(v => typeof v === 'number') as number[] : [5, 15, 30, 60];
  return values.map(v => ({ value: v, label: v === 60 ? '1 hour' : v + ' minutes' }));
});
const unlockMinutes = computed(() => typeof status.value?.unlock_minutes === 'number' ? status.value.unlock_minutes : 15);
const left = (until: number) => {
  const seconds = Math.max(0, Math.round((until - now.value) / 1000));
  return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
};
// The QR code's dark modules as one SVG path, from rows of '0' and '1' only (anything else draws nothing).
const qrSize = computed(() => pairing.value?.rows.length || 0);
const qrPath = computed(() => {
  const rows = pairing.value?.rows || [];
  let path = '';
  rows.forEach((row, y) => {
    for (const run of row.matchAll(/1+/g)) path += 'M' + (run.index ?? 0) + ' ' + y + 'h' + run[0].length + 'v1h-' + run[0].length + 'z';
  });
  return path;
});
const stamp = (at: number) => at ? new Date(at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';

async function run(task: () => Promise<void>) {
  if (busy.value) return;
  busy.value = true;
  problem.value = '';
  try {
    await task();
  } catch (error) {
    if (alive) problem.value = error instanceof Error ? error.message : String(error);
  } finally {
    if (alive) busy.value = false;
  }
}

function accept(next: unknown) {
  if (!alive || !next || typeof next !== 'object' || !('enabled' in (next as object))) return;
  status.value = next as Status;
  // The code is spent once a phone sends it, and gone once it expires or is cancelled: so is the picture of it.
  if (pairing.value && !status.value.pairing) pairing.value = null;
  if (!status.value.pairing && !status.value.confirm) stopClock();
}

function startClock() {
  if (!clock) clock = window.setInterval(() => {
    now.value = Date.now();
    if (pairing.value && pairing.value.until <= now.value) {
      pairing.value = null;
      message.value = 'The pairing code expired. Show a new one when your iPhone is ready.';
    }
  }, 1000);
}

function stopClock() {
  if (clock) window.clearInterval(clock);
  clock = 0;
}

const set = (key: string, value: unknown) => run(async () => accept(await rpc<Status>('remote_set', { key, value })));

function turn(on: boolean) {
  return run(async () => {
    accept(await rpc<Status>('remote_set', { key: 'enabled', value: on }));
    pairing.value = null;
    message.value = on ? 'Pebble Remote is on.' : 'Pebble Remote is off. No iPhone can reach Pebble.';
  });
}

function showCode() {
  return run(async () => {
    const answer = await rpc<Pairing>('remote_pair_start');
    const rows = Array.isArray(answer?.qr) ? (answer.qr as unknown[]).filter(r => typeof r === 'string' && /^[01]+$/.test(r)) as string[] : [];
    const link = text(answer?.link);
    if (!link.startsWith('pebble-remote://pair?') || !rows.length) throw new Error("Pebble couldn't make a pairing code. Try again.");
    pairing.value = { link, rows, until: time(answer.expires_at) };
    accept(answer.status);
    now.value = Date.now();
    startClock();
    message.value = 'Scan the code with Pebble Remote on your iPhone.';
  });
}

function copyLink() {
  return run(async () => {
    if (!pairing.value) return;
    await navigator.clipboard.writeText(pairing.value.link);
    message.value = 'Link copied. Paste it in Pebble Remote on your iPhone.';
  });
}

function cancelCode() {
  return run(async () => {
    pairing.value = null;
    accept(await rpc<Status>('remote_pair_cancel'));
    message.value = 'The pairing code was cancelled.';
  });
}

function answer(confirmed: boolean) {
  return run(async () => {
    const name = confirm.value?.device || 'The iPhone';
    // The six digits on screen go with the answer, so it answers only that phone's pairing (w27's review F9).
    accept(await rpc<Status>('remote_pair_confirm', { confirm: confirmed, code: confirm.value?.digits }));
    message.value = confirmed ? name + ' is paired.' : 'Pairing cancelled. Nothing was saved.';
  });
}

function remove(id: string, name: string) {
  return run(async () => {
    accept(await rpc<Status>('remote_device_remove', { device_id: id }));
    message.value = name + ' was removed. It can no longer reach Pebble.';
  });
}

function onUnlock(event: Event) {
  const value = Number((event?.target as HTMLSelectElement | null)?.value);
  if (Number.isInteger(value)) void set('unlock_minutes', value);
}

async function receive(event: { event: string; data: any }) {
  if (event?.event !== 'remote_state') return;
  const waiting = !!confirm.value;
  accept(event.data);
  if (confirm.value) {
    now.value = Date.now();
    startClock();
    if (!waiting) {
      await nextTick();
      confirmHeading.value?.focus?.();
    }
  }
}

onMounted(async () => {
  void run(async () => accept(await rpc<Status>('remote_status')));
  const stop = await subscribe(receive);
  if (alive) unsubscribe = stop; else stop();
});
onBeforeUnmount(() => {
  alive = false;
  pairing.value = null;
  stopClock();
  unsubscribe();
});
</script>

<template>
  <section class="remote-settings" aria-labelledby="remote-heading">
    <div class="section-label" id="remote-heading">PEBBLE REMOTE</div>
    <p class="setting-note">
      Use Pebble on this Mac from Pebble Remote on your iPhone. Your iPhone asks for Face ID before it acts, and again for
      every approval and every change to your permissions.
    </p>
    <p class="setting-note" v-if="!status">Loading…</p>
    <template v-else>
      <button type="button" class="setting-row" role="switch" :aria-checked="enabled ? 'true' : 'false'"
        aria-describedby="remote-switch-note" :disabled="busy" @click="turn(!enabled)">
        <div>
          <strong>Pebble Remote</strong>
          <small id="remote-switch-note">{{ enabled ? 'On: your paired iPhones can use Pebble on this Mac.' : 'Off: no iPhone can reach Pebble.' }}</small>
        </div>
        <span class="toggle" aria-hidden="true" :class="{ on: enabled }" />
      </button>
      <p v-if="text(status.error)" class="remote-problem" role="alert">{{ text(status.error) }}</p>
      <template v-if="enabled">
        <p class="setting-note">{{ listening }}</p>

        <div v-if="confirm" class="remote-confirm" role="group" aria-labelledby="remote-confirm-heading" aria-describedby="remote-confirm-note">
          <h4 id="remote-confirm-heading" ref="confirmHeading" tabindex="-1">Does your iPhone show {{ confirm.code }}?</h4>
          <p id="remote-confirm-note">
            “{{ confirm.device }}” wants to pair with this Mac. Pair only if your iPhone shows the same six digits.
            <span v-if="confirm.until"> Pebble cancels it in {{ left(confirm.until) }} without an answer.</span>
          </p>
          <div class="remote-actions">
            <button type="button" class="primary-button" :disabled="busy" @click="answer(true)">They match: pair</button>
            <button type="button" class="secondary-button" :disabled="busy" @click="answer(false)">Cancel</button>
          </div>
        </div>

        <div class="remote-block">
          <h4>Pair an iPhone</h4>
          <template v-if="pairing">
            <svg class="remote-qr" role="img" aria-label="Pairing code for Pebble Remote" :viewBox="'-4 -4 ' + (qrSize + 8) + ' ' + (qrSize + 8)"
              shape-rendering="crispEdges">
              <!-- Black on white in both themes: scanners read a QR code that way. -->
              <rect :x="-4" :y="-4" :width="qrSize + 8" :height="qrSize + 8" fill="#ffffff" />
              <path :d="qrPath" fill="#000000" />
            </svg>
            <p class="setting-note" id="remote-code-note">
              Scan it with Pebble Remote on your iPhone, not the Camera app. It works once, for {{ left(pairing.until) }}.
              Then this Mac asks you to confirm six digits.
            </p>
            <div class="remote-actions">
              <button type="button" class="secondary-button" :disabled="busy" aria-describedby="remote-code-note" @click="copyLink">Copy link</button>
              <button type="button" class="secondary-button" :disabled="busy" @click="cancelCode">Cancel</button>
            </div>
          </template>
          <template v-else>
            <p class="setting-note">A pairing code works once, for 5 minutes.</p>
            <button type="button" class="secondary-button" :disabled="busy || !running || !!confirm" @click="showCode">Show a pairing code</button>
          </template>
        </div>

        <div class="remote-block">
          <h4>Paired iPhones</h4>
          <p v-if="!devices.length" class="setting-note">No iPhone is paired yet.</p>
          <ul v-else class="remote-devices" aria-label="Paired iPhones">
            <li v-for="d in devices" :key="d.id">
              <div>
                <strong>{{ d.name }}</strong>
                <small>{{ d.connected ? 'Connected now' : d.seen ? 'Last seen ' + stamp(d.seen) : 'Paired ' + stamp(d.paired) }}</small>
              </div>
              <button type="button" class="secondary-button danger" :disabled="busy" :aria-label="'Remove ' + d.name" @click="remove(d.id, d.name)">Remove</button>
            </li>
          </ul>
        </div>

        <button type="button" class="setting-row" role="switch" :aria-checked="status.keep_awake === true ? 'true' : 'false'"
          aria-describedby="remote-awake-note remote-lid-note" :disabled="busy" @click="set('keep_awake', status.keep_awake !== true)">
          <div>
            <strong>Keep this Mac awake</strong>
            <small id="remote-awake-note">While Pebble Remote is on, this Mac stays awake, on battery too, so your iPhone can reach it. The screen can still sleep.</small>
          </div>
          <span class="toggle" aria-hidden="true" :class="{ on: status.keep_awake === true }" />
        </button>
        <p id="remote-lid-note" class="setting-note">A MacBook still sleeps when you close its lid, unless it's on power with an external display.</p>

        <button type="button" class="setting-row" role="switch" :aria-checked="status.lan === true ? 'true' : 'false'"
          aria-describedby="remote-lan-note" :disabled="busy" @click="set('lan', status.lan !== true)">
          <div>
            <strong>Listen on this Wi-Fi network</strong>
            <small id="remote-lan-note">Off leaves Tailscale only, so your iPhone reaches this Mac through Tailscale.</small>
          </div>
          <span class="toggle" aria-hidden="true" :class="{ on: status.lan === true }" />
        </button>

        <label class="remote-unlock">
          After Face ID, your iPhone can act for
          <select :value="unlockMinutes" :disabled="busy" @change="onUnlock">
            <option v-for="c in choices" :key="c.value" :value="c.value">{{ c.label }}</option>
          </select>
        </label>

        <div class="remote-block">
          <h4>What your iPhone did</h4>
          <p class="setting-note">
            Approvals, things opened on this Mac and screenshots also show as a notification on this Mac while Pebble is in
            the background, if Pebble may send notifications.
          </p>
          <p v-if="!activity.length" class="setting-note">Nothing yet.</p>
          <ul v-else class="remote-activity" aria-label="What your iPhone did">
            <li v-for="(line, index) in activity" :key="index"><time>{{ stamp(line.at) }}</time> {{ line.text }}</li>
          </ul>
        </div>
      </template>
    </template>
    <p v-if="problem" class="remote-problem" role="alert">{{ problem }}</p>
    <p class="remote-status" role="status" aria-live="polite">{{ message }}</p>
  </section>
</template>

<style scoped>
.remote-settings h4 {
  margin: 0 0 var(--space-2);
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--text);
}
.remote-block {
  margin: var(--space-4) 0;
}
.remote-actions {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.remote-confirm {
  margin: var(--space-4) 0;
  padding: var(--space-4);
  border: 1px solid var(--accent);
  border-radius: var(--radius-md);
  background: var(--accent-soft);
  color: var(--text);
}
.remote-confirm h4 {
  font-size: var(--text-lg);
  font-variant-numeric: tabular-nums;
}
.remote-confirm h4:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.remote-qr {
  display: block;
  width: 224px;
  height: 224px;
  margin: 0 0 var(--space-2);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
}
.remote-devices,
.remote-activity {
  list-style: none;
  margin: 0;
  padding: 0;
}
.remote-devices li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--line);
}
.remote-devices small {
  display: block;
  color: var(--muted);
}
.remote-activity li {
  padding: var(--space-1) 0;
  color: var(--secondary);
  font-size: var(--text-sm);
}
.remote-activity time {
  color: var(--muted);
  margin-right: var(--space-2);
  font-variant-numeric: tabular-nums;
}
.remote-unlock {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: var(--space-3) 0;
  color: var(--text);
}
.remote-problem {
  color: var(--danger);
}
.remote-status:empty {
  display: none;
}
</style>
