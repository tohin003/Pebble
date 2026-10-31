<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, shallowRef } from 'vue';
import { invoke } from '@tauri-apps/api/core';
import { native, rpc } from '../bridge';

// Startup and native notification preferences for the main window's Settings.
// Every value shown comes from the host or the desktop shell. Banners offer only
// Open; approvals stay in the Pebble window. macOS is asked for permission only
// when the person turns notifications on here, never at startup.
type Kind = 'completed' | 'failed' | 'action_needed' | 'monitor_changed';
type Preview = 'none' | 'title' | 'body';
interface Quiet { start: string; end: string; timezone: string }
interface Preferences { enabled: boolean; kinds: Record<Kind, boolean>; preview: Preview; quiet_hours: Quiet | null; replies?: boolean }
interface MuteJob { job_id: string; label: string; state: string; muted: boolean }
interface Status { settings: Preferences; quiet_now: boolean; outbox: Record<string, number>; deferred: number; last_failure: { reason: string; at?: string | null } | null; jobs: MuteJob[] }

const kindList: [Kind, string][] = [['completed', 'Completed runs'], ['failed', 'Failures'], ['action_needed', 'Requests for your action'], ['monitor_changed', 'Monitor results that changed']];
const previewList: [Preview, string, string][] = [
  ['title', 'Title only', 'Default. Shows which schedule has an update, not what the run said.'],
  ['body', 'Title and a short preview', 'Up to 140 characters of the run’s answer, without links or control characters. The answer is run output, not a message from Pebble.'],
  ['none', 'No details', 'Shows only that a schedule has an update.'],
];
const loginText: Record<string, string> = {
  enabled: 'Pebble opens at login and shows only the companion.',
  requires_approval: 'macOS needs your approval. Open System Settings → General → Login Items and allow Pebble.',
  not_registered: 'Pebble does not open at login.',
  // Phase 45 (F008): said for a person, with the way that still works
  not_found: "Pebble can't add itself to Login Items on this Mac. To open it at login, add Pebble in System Settings → General → Login Items (the + button).",
  unavailable: 'Login items are unavailable in this build.',
};
const authText: Record<string, string> = {
  authorized: 'macOS allows Pebble notifications.',
  provisional: 'macOS delivers Pebble notifications quietly to Notification Center.',
  denied: 'Notifications for Pebble are off in System Settings → Notifications. Every update stays in the Pebble window.',
  not_determined: 'macOS asks for permission when you turn notifications on and save.',
  unavailable: 'Native notifications are unavailable in this build. Every update stays in the Pebble window.',
};
const failureText: Record<string, string> = {
  not_authorized: 'macOS did not allow a notification.', denied: 'macOS declined a notification.',
  unavailable: 'The notification service was unavailable.', error: 'A notification could not be shown.',
  no_acknowledgement: 'The desktop shell did not confirm a notification.', removal_unconfirmed: 'The desktop shell did not confirm removing a notification.',
};
const allowedAuth = new Set(['authorized', 'provisional']);
const clockPattern = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

const status = shallowRef<Status | null>(null);
const login = ref(''), authorization = ref('');
const loading = ref(false), busy = ref(''), error = ref(''), notice = ref('');
const form = reactive({ replies: true, enabled: false, kinds: { completed: true, failed: true, action_needed: true, monitor_changed: true } as Record<Kind, boolean>, preview: 'title' as Preview, quiet: false, start: '22:00', end: '07:00', zone: localZone() });
let mounted = false, request = 0;

const outboxLine = computed(() => {
  const counts = status.value?.outbox ?? {};
  const count = (key: string) => (Number.isInteger(counts[key]) ? counts[key] : 0);
  return 'Waiting ' + count('pending') + ' · Shown ' + count('delivered') + ' · Not shown ' + count('suppressed') + ' · Failed ' + count('failed');
});
const loginAction = computed(() => login.value === 'enabled' || login.value === 'requires_approval'
  ? { enabled: false, label: busy.value === 'login' ? 'Updating…' : 'Stop opening Pebble at login' }
  : { enabled: true, label: busy.value === 'login' ? 'Updating…' : 'Open Pebble at login' });

function localZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; }
}
function failure(cause: unknown) {
  const text = cause instanceof Error ? cause.message : String(cause);
  return (text || 'The request failed.').slice(0, 500);
}
function shellStatus(value: unknown, known: Record<string, string>) {
  const item = value as { status?: unknown } | null;
  return item && typeof item.status === 'string' && item.status in known ? item.status : 'unavailable';
}
function validStatus(value: unknown): value is Status {
  const item = value as Partial<Status> | null;
  const settings = item?.settings as Partial<Preferences> | undefined;
  return !!item && !!settings && typeof settings.enabled === 'boolean' && !!settings.kinds && typeof settings.kinds === 'object'
    && kindList.every(([key]) => typeof (settings.kinds as Record<string, unknown>)[key] === 'boolean')
    && previewList.some(([key]) => key === settings.preview)
    && (settings.quiet_hours === null || (!!settings.quiet_hours && typeof settings.quiet_hours.start === 'string' && typeof settings.quiet_hours.end === 'string' && typeof settings.quiet_hours.timezone === 'string'))
    && Array.isArray(item.jobs) && item.jobs.every(job => !!job && typeof job.job_id === 'string' && typeof job.label === 'string' && typeof job.muted === 'boolean')
    && !!item.outbox && typeof item.outbox === 'object';
}
function adopt(settings: Preferences) {
  form.enabled = settings.enabled;
  form.replies = settings.replies !== false;  // Phase 45 (D134): a value saved before 1.9.10 has none: on
  for (const [key] of kindList) form.kinds[key] = settings.kinds[key];
  form.preview = settings.preview;
  form.quiet = settings.quiet_hours !== null;
  if (settings.quiet_hours) { form.start = settings.quiet_hours.start; form.end = settings.quiet_hours.end; form.zone = settings.quiet_hours.timezone; }
}

async function shell(command: 'login_item_status' | 'login_item_set' | 'notification_authorization', args: Record<string, unknown>) {
  try { return await invoke<unknown>(command, args); } catch { return null; }
}
async function load() {
  if (!mounted || !native) return;
  const current = ++request;
  loading.value = true; error.value = '';
  try {
    const [result, item, auth] = await Promise.all([
      rpc<unknown>('notification_status', {}),
      shell('login_item_status', {}),
      shell('notification_authorization', { request: false }),
    ]);
    if (!mounted || current !== request) return;
    login.value = shellStatus(item, loginText);
    authorization.value = shellStatus(auth, authText);
    if (!validStatus(result)) throw new Error('Notification settings are unavailable.');
    status.value = result;
    adopt(result.settings);
  } catch (cause) {
    if (mounted && current === request) error.value = failure(cause);
  } finally {
    if (mounted && current === request) loading.value = false;
  }
}
async function act(key: string, work: () => Promise<string | void>) {
  if (busy.value || !mounted || !native) return;
  busy.value = key; error.value = ''; notice.value = '';
  try {
    const message = await work();
    if (mounted && message) notice.value = message;
  } catch (cause) {
    if (mounted) error.value = failure(cause);
  } finally {
    if (mounted) busy.value = '';
  }
}
function preferences(): Preferences {
  let quiet_hours: Quiet | null = null;
  if (form.quiet) {
    const start = String(form.start).trim(), end = String(form.end).trim();
    if (!clockPattern.test(start) || !clockPattern.test(end)) throw new Error('Give quiet hours as 24-hour times such as 22:00.');
    if (start === end) throw new Error('Quiet hours need different start and end times.');
    quiet_hours = { start, end, timezone: form.zone };
  }
  return { enabled: form.enabled === true, kinds: Object.fromEntries(kindList.map(([key]) => [key, form.kinds[key] === true])) as Record<Kind, boolean>, preview: form.preview, quiet_hours, replies: form.replies === true };
}
// Phase 45 (F137, audit round R4): this page alone saves with a button (turning banners on asks macOS for
// permission), so a change waiting for it says so instead of being lost when Settings closes.
const unsaved = computed(() => {
  const saved = status.value?.settings;
  if (!saved) return false;
  const quiet = (q: Quiet | null | undefined) => (q ? [q.start, q.end, q.timezone].join('|') : '');
  try {
    const value = preferences();
    return value.enabled !== saved.enabled || value.preview !== saved.preview || quiet(value.quiet_hours) !== quiet(saved.quiet_hours)
      || value.replies !== (saved.replies !== false)
      || kindList.some(([key]) => value.kinds[key] !== (saved.kinds?.[key] === true));
  } catch {
    return true;  // quiet hours being typed: not saved yet
  }
});
function save() {
  return act('save', async () => {
    const value = preferences();
    const turningOn = value.enabled && !status.value?.settings.enabled;
    if (turningOn && !allowedAuth.has(authorization.value)) {
      // The only place Pebble asks macOS for permission.
      authorization.value = shellStatus(await shell('notification_authorization', { request: true }), authText);
      if (!allowedAuth.has(authorization.value)) throw new Error(authText[authorization.value] ?? authText.unavailable);
    }
    await rpc('setting', { key: 'notifications', value });
    await load();
    return 'Notification preferences saved.';
  });
}
function mute(job: MuteJob) {
  return act('mute:' + job.job_id, async () => {
    await rpc('notification_mute', { job_id: job.job_id, muted: !job.muted });
    await load();
    return job.muted ? 'Banners for “' + job.label + '” are back on.' : 'Banners for “' + job.label + '” are muted. Its updates stay in the app.';
  });
}
function toggleLogin() {
  const enabled = loginAction.value.enabled;
  return act('login', async () => {
    login.value = shellStatus(await shell('login_item_set', { enabled }), loginText);
    return loginText[login.value];
  });
}
function useLocalZone() { form.zone = localZone(); }

onMounted(() => { mounted = true; void load(); });
onUnmounted(() => { mounted = false; request++; });
</script>

<template>
  <section class="notify-panel" aria-label="Startup and notifications" :aria-busy="loading">
    <p v-if="!native" class="notify-note">Startup and notification settings are available in the Pebble desktop app.</p>
    <template v-else>
      <section class="notify-section" aria-label="Start at login">
        <strong>Start at login</strong>
        <p class="notify-note" role="status">{{ loginText[login] ?? 'Checking login item…' }}</p>
        <button v-if="login && login !== 'not_found' && login !== 'unavailable'" class="secondary-button" type="button" :disabled="!!busy" @click="toggleLogin">{{ loginAction.label }}</button>
        <p class="notify-note">Pebble runs schedules only while it is open. Quitting asks first when schedules are active.</p>
      </section>

      <p v-if="loading && !status" role="status">Loading notification settings…</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-if="notice" role="status">{{ notice }}</p>

      <form v-if="status" class="notify-section" aria-label="Notifications" @submit.prevent="save">
        <fieldset :disabled="!!busy">
          <legend>Notifications</legend>
          <p class="notify-note">{{ authText[authorization] ?? authText.unavailable }}</p>
          <label class="notify-check"><input v-model="form.enabled" type="checkbox" />Show macOS notifications</label>
          <fieldset class="notify-group" :disabled="!form.enabled">
            <legend>Notify me about</legend>
            <!-- Phase 45 (D134, the person): a task in a conversation, while Pebble is in the background -->
            <label class="notify-check"><input v-model="form.replies" type="checkbox" />Replies and cards while Pebble is in the background</label>
            <label v-for="[key, label] in kindList" :key="key" class="notify-check"><input v-model="form.kinds[key]" type="checkbox" />{{ label }}</label>
          </fieldset>
          <fieldset class="notify-group" :disabled="!form.enabled">
            <legend>Content preview</legend>
            <label v-for="[key, label, note] in previewList" :key="key" class="notify-choice"><input v-model="form.preview" type="radio" :value="key" />{{ label }}<small>{{ note }}</small></label>
          </fieldset>
          <fieldset class="notify-group" :disabled="!form.enabled">
            <legend>Quiet hours</legend>
            <label class="notify-check"><input v-model="form.quiet" type="checkbox" />Hold banners during quiet hours</label>
            <div v-if="form.quiet" class="notify-times">
              <label>From<input v-model="form.start" type="time" /></label>
              <label>Until<input v-model="form.end" type="time" /></label>
            </div>
            <p v-if="form.quiet" class="notify-note">Times are in {{ form.zone }}.<button v-if="form.zone !== localZone()" class="link-button" type="button" @click="useLocalZone">Use this Mac’s zone ({{ localZone() }})</button></p>
            <p class="notify-note">During quiet hours banners wait. When quiet hours end, Pebble shows one summary. Nothing is dropped, and every update stays in the app.</p>
          </fieldset>
          <p class="notify-note">Several updates from one schedule within a minute share one banner, and after three banners in ten minutes Pebble shows one summary. Banners offer only Open, which opens the conversation; approvals happen only in the Pebble window.</p>
          <p v-if="unsaved" class="notify-note" role="status">Changes here apply when you save them.</p>
          <button class="primary-button" type="submit" :disabled="!!busy">{{ busy === 'save' ? 'Saving…' : 'Save notification preferences' }}</button>
        </fieldset>
      </form>

      <section v-if="status" class="notify-section" aria-label="Notification delivery">
        <strong>Delivery</strong>
        <p class="notify-note">{{ outboxLine }}<template v-if="status.deferred"> · Held for quiet hours {{ status.deferred }}</template></p>
        <p v-if="status.quiet_now" class="notify-note" role="status">Quiet hours are on now.</p>
        <p v-if="status.last_failure" class="notify-note">Last problem: {{ failureText[status.last_failure.reason] ?? 'A notification could not be shown.' }}</p>
        <ul v-if="status.jobs.length" class="notify-list" aria-label="Banners per schedule">
          <li v-for="job in status.jobs" :key="job.job_id">
            <span>{{ job.label }}<small>{{ job.muted ? ' · banners muted' : ' · banners on' }}</small></span>
            <button class="secondary-button" type="button" :disabled="!!busy" :aria-label="(job.muted ? 'Unmute banners for ' : 'Mute banners for ') + job.label" @click="mute(job)">{{ busy === 'mute:' + job.job_id ? 'Updating…' : job.muted ? 'Unmute' : 'Mute' }}</button>
          </li>
        </ul>
        <p v-else class="notify-note">No schedules yet.</p>
      </section>
    </template>
  </section>
</template>

<style scoped>
.notify-panel { min-width: 0; font-size: 13px; }
.notify-section { margin: 12px 0; }
.notify-section fieldset { border: 0; padding: 0; margin: 8px 0; min-width: 0; }
/* Phase 45 (F170, audit round R5): options that follow the main switch look off while it is off */
.notify-section fieldset.notify-group:disabled { opacity: .45; }
.notify-section legend { font-weight: 600; margin-bottom: 6px; }
.notify-note, .notify-choice small, .notify-list small { font-size: 12px; opacity: .75; }
.notify-check, .notify-choice { display: flex; gap: 8px; align-items: baseline; margin: 6px 0; flex-wrap: wrap; }
.notify-choice small { flex-basis: 100%; margin-left: 22px; }
.notify-times { display: flex; gap: 8px; }
.notify-times label { flex: 1; }
.notify-times input { display: block; width: 100%; box-sizing: border-box; margin-top: 4px; font: inherit; color: inherit; background: transparent; border: 1px solid var(--border, #b9b9b9); border-radius: 6px; padding: 6px 8px; }
.notify-list { padding: 0; list-style: none; }
.notify-list li { display: flex; gap: 8px; align-items: center; justify-content: space-between; margin: 6px 0; overflow-wrap: anywhere; }
.link-button { margin-left: 6px; background: none; border: 0; padding: 0; color: inherit; text-decoration: underline; cursor: pointer; font: inherit; }
</style>
