<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import { rpc } from '../bridge';

// The windows and tabs this conversation lets Pebble use, for the current task
// only. Only this main-window panel grants a window, hands one back ("Done") or
// ends access. "Your turn" waits for the person to finish a sign-in, a code or
// a check themselves; Pebble does not read the window meanwhile and never sees
// what they type. Window titles and app names are untrusted data: they render
// as literal text and never grant anything.
// Phase 24 (t24-browser): a grant made while no task can use it waits for the
// person's next message here (V1-007; listed as such); with Screen control off
// the grant offers to turn it on in the same step; and the app-window grant is
// not offered until Pebble can read app identities (V1-127: no dead control).
type Target = Record<string, unknown>;
type Session = { session_id: string; kind: string; target: Target; actions: string[]; state: string; reasons: string[]; expires: number | null;
  pending: boolean };
type Offer = { message: string; available: boolean };
type AppEntry = { id?: unknown; name?: unknown; bundle_id?: unknown; team_id?: unknown };
type PinnedApp = { name: string; bundle_id: string; team_id: string | null };
type AppWindow = { title: string; title_sha256: string; ax_identifier: string | null; focused: boolean };
const props = defineProps<{ chatId: string; apps?: AppEntry[]; refreshKey?: unknown }>();
const emit = defineEmits<{ changed: [] }>();

const GROUPS = [
  { key: 'look', label: 'Look around', actions: ['focus', 'scroll', 'expand', 'collapse'], note: 'Move focus, scroll, and open or close sections. Nothing changes.' },
  { key: 'fill', label: 'Fill in', actions: ['select', 'toggle', 'type'], note: 'Choose options, tick boxes and type text, never into a password, code or card field.' },
  { key: 'press', label: 'Press buttons', actions: ['press', 'enter'], note: 'Anything that could submit, send, buy or delete waits for your approval in Pebble first.' },
] as const;
type GroupKey = typeof GROUPS[number]['key'];
const LIFETIMES = [[300, '5 minutes'], [900, '15 minutes'], [1800, '30 minutes']] as const;
const LIVE = ['active', 'handoff', 'interrupted'];
const STATES: Record<string, string> = { active: 'Pebble can use it', handoff: 'Your turn', interrupted: 'Paused: the last step has an uncertain outcome',
  revoked: 'Ended by you', expired: 'Time ran out', restarted: 'Ended when Pebble restarted' };
const REASONS: Record<string, string> = { secure_field: 'a password, code or card field', two_factor: 'a verification code',
  captcha: 'a check that you are a person', login_form: 'a sign-in form', cross_origin: 'a site you did not allow',
  paused: 'you paused Pebble in Chrome' };
const POLL_MS = 4000;
const sessions = ref<Session[]>([]);
const loaded = ref(false);
const busy = ref('');
const status = ref('');
const problem = ref('');
const granting = ref(false);
const offer = ref<Offer | null>(null);
const chosenApp = ref('');
const windows = ref<AppWindow[] | null>(null);
const chosenWindow = ref(-1);
const groups = ref<GroupKey[]>(['look', 'fill']);
const lifetime = ref(900);
// Element refs stay shallow: a template element is never made reactive.
const region = shallowRef<HTMLElement | null>(null), grantCard = shallowRef<HTMLElement | null>(null);
let generation = 0, formVersion = 0, disposed = false, timer: ReturnType<typeof setTimeout> | undefined;

const word = (value: unknown) => typeof value === 'string' ? value : '';
const clip = (value: string, limit: number) => value.length > limit ? value.slice(0, limit) + '…' : value;
const own = (map: Record<string, string>, key: string) => Object.hasOwn(map, key) ? map[key] : '';
const keyOf = (app: PinnedApp) => app.bundle_id + '\u0000' + (app.team_id || '');
const live = computed(() => sessions.value.filter(item => LIVE.includes(item.state)));
const ended = computed(() => sessions.value.filter(item => !LIVE.includes(item.state)).slice(-3).reverse());
// Only entries that carry a pinned identity can name an app window.
const pinnedApps = computed<PinnedApp[]>(() => {
  const seen = new Set<string>(), found: PinnedApp[] = [];
  for (const entry of Array.isArray(props.apps) ? props.apps : []) {
    if (!entry || typeof entry !== 'object') continue;
    const bundle = entry.bundle_id, team = entry.team_id ?? null;
    if (typeof bundle !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9.-]{0,254}$/.test(bundle)) continue;
    if (team !== null && (typeof team !== 'string' || !/^[A-Z0-9]{10}$/.test(team))) continue;
    const app = { name: clip(word(entry.name), 200) || bundle, bundle_id: bundle, team_id: team };
    if (!seen.has(keyOf(app))) { seen.add(keyOf(app)); found.push(app); }
  }
  return found;
});
const app = computed(() => pinnedApps.value.find(item => keyOf(item) === chosenApp.value) || null);
const titleCounts = computed(() => {
  const counts = new Map<string, number>();
  for (const item of windows.value || []) counts.set(item.title_sha256, (counts.get(item.title_sha256) || 0) + 1);
  return counts;
});
const chosenActions = computed(() => GROUPS.filter(group => groups.value.includes(group.key)).flatMap(group => [...group.actions]));
const chosen = computed(() => windows.value?.[chosenWindow.value] || null);
const duplicate = (item: AppWindow) => (titleCounts.value.get(item.title_sha256) || 0) > 1;
const canGrant = computed(() => !busy.value && !!app.value && !!chosen.value && !duplicate(chosen.value) && chosenActions.value.length > 0);

function session(value: unknown): Session | null {
  if (!value || typeof value !== 'object') return null;
  const row = value as Record<string, unknown>;
  if (typeof row.session_id !== 'string' || !/^[0-9a-f]{32}$/.test(row.session_id)) return null;
  const target = row.target && typeof row.target === 'object' && !Array.isArray(row.target) ? row.target as Target : {};
  const reasons = row.reason && typeof row.reason === 'object' ? (row.reason as { reasons?: unknown }).reasons : null;
  return { session_id: row.session_id, kind: word(row.kind), target, actions: Array.isArray(row.actions) ? row.actions.map(word).filter(Boolean) : [],
    state: word(row.state), reasons: Array.isArray(reasons) ? reasons.map(word).filter(Boolean).slice(0, 8) : [],
    expires: typeof row.expires === 'number' && Number.isFinite(row.expires) ? row.expires : null, pending: row.pending === true };
}
function appWindow(value: unknown): AppWindow | null {
  if (!value || typeof value !== 'object') return null;
  const row = value as Record<string, unknown>;
  if (typeof row.title_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(row.title_sha256)) return null;
  if (row.ax_identifier != null && typeof row.ax_identifier !== 'string') return null;
  return { title: clip(word(row.title), 200), title_sha256: row.title_sha256, ax_identifier: typeof row.ax_identifier === 'string' ? row.ax_identifier : null, focused: row.focused === true };
}
function describe(item: Session) {
  if (item.kind === 'app') {
    const bundle = word(item.target.bundle_id), named = pinnedApps.value.find(entry => entry.bundle_id === bundle);
    return clip('App window · ' + (named ? named.name + ' (' + bundle + ')' : bundle || 'unknown app'), 400);
  }
  if (item.kind === 'browser') {
    const origins = Array.isArray(item.target.origins) ? item.target.origins.map(word).filter(Boolean) : [];
    // Phase 22: a tab the person shared from Pebble for Chrome, or a tab in Pebble's own browser.
    const where = item.target.profile === 'chrome' ? 'Your Chrome tab · ' : "Pebble's browser tab · ";
    return clip(where + (origins.join(', ') || 'no site'), 400);
  }
  return 'Unknown window';
}
const stateName = (state: string) => own(STATES, state) || 'Ended';
const sessionState = (item: Session) => item.pending && item.state === 'active' ? 'Waiting for your next message here' : stateName(item.state);
function abilities(actions: string[]) {
  const names = GROUPS.filter(group => group.actions.some(action => actions.includes(action))).map(group => group.label.toLowerCase());
  return names.length ? 'Can ' + names.join(', ') : 'Can do nothing';
}
function turn(item: Session) {
  const found = item.reasons.map(reason => own(REASONS, reason)).filter(Boolean);
  return found.length ? 'Pebble stopped at ' + found.join(', ') + '.' : 'Pebble handed this ' + (item.kind === 'browser' ? 'tab' : 'window') + ' to you.';
}
function remaining(item: Session) {
  if (item.expires === null) return '';
  const minutes = Math.ceil((item.expires * 1000 - Date.now()) / 60000);
  return minutes > 0 ? ' · Ends in about ' + (minutes === 1 ? '1 minute' : minutes + ' minutes') : ' · Time is up';
}
function failure(cause: unknown) {
  const text = cause instanceof Error ? cause.message : String(cause);
  return clip(text || 'The request failed.', 1000);
}

// While a session is live the list is re-read, so "Your turn" appears without a reload.
function schedule() {
  clearTimeout(timer); timer = undefined;
  if (!disposed && live.value.length) timer = setTimeout(() => { timer = undefined; void load(); }, POLL_MS);
}
async function load() {
  const version = ++generation;
  try {
    const rows = await rpc<unknown>('screen_sessions', { id: props.chatId });
    if (disposed || version !== generation) return;
    sessions.value = (Array.isArray(rows) ? rows : []).map(session).filter((item): item is Session => !!item);
    loaded.value = true;
  } catch (cause) { if (!disposed && version === generation) problem.value = failure(cause); }
  finally { if (!disposed && version === generation) schedule(); }
}
async function settleSession(item: Session, method: 'screen_handoff_done' | 'screen_session_revoke', done: string) {
  if (busy.value) return;
  const chat = props.chatId;
  busy.value = method + ':' + item.session_id; status.value = ''; problem.value = '';
  try {
    if (method === 'screen_handoff_done') await rpc('screen_handoff_done', { session_id: item.session_id });
    else await rpc('screen_session_revoke', { session_id: item.session_id });
    if (disposed || chat !== props.chatId) return;
    status.value = done;
    emit('changed');
  } catch (cause) { if (!disposed && chat === props.chatId) problem.value = failure(cause); }
  finally {
    busy.value = '';
    if (!disposed && chat === props.chatId) void load();
  }
}
const finish = (item: Session) => settleSession(item, 'screen_handoff_done', 'Done. Pebble looks at the ' + (item.kind === 'browser' ? 'tab' : 'window') + ' again before it does anything else.');
const end = (item: Session) => settleSession(item, 'screen_session_revoke', 'Access ended. Pebble can no longer use this ' + (item.kind === 'browser' ? 'tab' : 'window') + '.');

function resetPick() { formVersion++; chosenApp.value = ''; windows.value = null; chosenWindow.value = -1; }
function openGrant() {
  if (busy.value) return;
  granting.value = true; resetPick(); groups.value = ['look', 'fill']; lifetime.value = 900; problem.value = ''; offer.value = null;
  void nextTick(() => grantCard.value?.focus());
}
function closeGrant() {
  granting.value = false; resetPick(); offer.value = null;
  void nextTick(() => region.value?.focus());
}
function pickApp(item: PinnedApp) { resetPick(); chosenApp.value = keyOf(item); }
function pickWindow(index: number) { chosenWindow.value = index; }
function toggleGroup(key: GroupKey) { groups.value = groups.value.includes(key) ? groups.value.filter(item => item !== key) : [...groups.value, key]; }
function setLifetime(seconds: number) { lifetime.value = seconds; }
async function showWindows() {
  const picked = app.value;
  if (!picked || busy.value) return;
  const chat = props.chatId, version = formVersion;
  busy.value = 'windows'; problem.value = ''; windows.value = null; chosenWindow.value = -1;
  try {
    const rows = await rpc<unknown>('screen_windows', { target: { kind: 'app', bundle_id: picked.bundle_id, team_id: picked.team_id } });
    if (disposed || chat !== props.chatId || version !== formVersion) return;
    windows.value = (Array.isArray(rows) ? rows : []).map(appWindow).filter((item): item is AppWindow => !!item).slice(0, 32);
  } catch (cause) { if (!disposed && chat === props.chatId && version === formVersion) problem.value = failure(cause); }
  finally { busy.value = ''; }
}
// enable: the person confirmed turning Screen control on for this grant (only after the service offered it).
async function grant(enable?: unknown) {
  const picked = app.value, selected = chosen.value, actions = chosenActions.value, ttl = lifetime.value;
  if (!canGrant.value || !picked || !selected) return;
  const chat = props.chatId, version = formVersion;
  busy.value = 'grant'; status.value = ''; problem.value = '';
  try {
    const answer = await rpc<unknown>('screen_session_grant', { id: chat, target: { kind: 'app', bundle_id: picked.bundle_id, team_id: picked.team_id,
      window: { title_sha256: selected.title_sha256, ax_identifier: selected.ax_identifier } }, actions, ttl, ...(enable === true ? { enable_screen: true } : {}) });
    if (disposed || chat !== props.chatId || version !== formVersion) return;
    const reply = answer && typeof answer === 'object' ? answer as Record<string, unknown> : null;
    if (reply?.status === 'needs_screen_permission') {
      const enabling = reply.enable && typeof reply.enable === 'object' ? reply.enable as Record<string, unknown> : {};
      offer.value = { message: clip(word(reply.message), 600) || 'Screen control is off in Permissions.', available: enabling.available === true };
      return;
    }
    status.value = 'Pebble can use this window ' + (reply?.pending === true ? 'from your next message in this conversation' : 'for the current task') + ', for '
      + (LIFETIMES.find(([seconds]) => seconds === ttl)?.[1] || ttl + ' seconds') + '.' + (reply?.screen_enabled === true ? ' Screen control is now on in Permissions.' : '')
      + ' End access here at any time.';
    closeGrant();
    emit('changed');
    void load();
  } catch (cause) { if (!disposed && chat === props.chatId && version === formVersion) problem.value = failure(cause); }
  finally { busy.value = ''; }
}

watch(() => props.chatId, () => {
  granting.value = false; offer.value = null; resetPick(); sessions.value = []; loaded.value = false; status.value = ''; problem.value = '';
  void load();
});
watch(() => props.refreshKey, () => { void load(); });
onMounted(load);
onBeforeUnmount(() => { disposed = true; generation++; formVersion++; clearTimeout(timer); timer = undefined; });
</script>

<template>
  <section ref="region" tabindex="-1" class="screen-sessions" aria-label="Windows Pebble can use" :aria-busy="!!busy">
    <div class="section-label">WINDOWS PEBBLE CAN USE</div>
    <p class="setting-note">You choose each window or tab Pebble may use in this conversation, for one task at a time: the current one, or your next message when none is running. Window text is shown as the app or page wrote it; it never gives Pebble permission. At a sign-in, a code or a check, Pebble stops and it's your turn: you finish it yourself, and Pebble never sees what you type.</p>
    <p class="screen-status" role="status" aria-live="polite">{{ status }}</p>
    <p v-if="problem" class="screen-problem" role="alert">{{ problem }}</p>
    <p v-if="loaded && !live.length" class="screen-empty">Pebble isn't using any window in this conversation.</p>
    <article v-for="item in live" :key="item.session_id" class="screen-session" :class="{ turn: item.state !== 'active' }"
      :aria-label="(item.state === 'handoff' ? 'Your turn: ' : '') + describe(item)">
      <strong>{{ describe(item) }}</strong>
      <small>{{ sessionState(item) }} · {{ abilities(item.actions) }}{{ remaining(item) }}</small>
      <template v-if="item.state === 'handoff'">
        <p class="screen-turn"><strong>Your turn.</strong> {{ turn(item) }} Finish it yourself in the {{ item.kind === 'browser' ? 'tab' : 'window' }}. Pebble doesn't look at it while it's your turn and never sees what you type.</p>
        <p class="setting-note">Press Done when you've finished.</p>
      </template>
      <template v-else-if="item.state === 'interrupted'">
        <p class="screen-turn">The last step has an uncertain outcome, so Pebble paused. Check the {{ item.kind === 'browser' ? 'tab' : 'window' }} yourself; nothing is retried.</p>
        <p class="setting-note">Press Done when it looks right.</p>
      </template>
      <div class="setting-actions">
        <button v-if="item.state !== 'active'" type="button" class="primary-button" :disabled="!!busy" @click="finish(item)">Done</button>
        <button type="button" class="secondary-button" :disabled="!!busy" @click="end(item)">End access</button>
      </div>
    </article>
    <template v-if="ended.length">
      <p class="screen-label">Recently ended</p>
      <ul class="screen-ended" aria-label="Recently ended">
        <li v-for="item in ended" :key="item.session_id">{{ describe(item) }} · {{ stateName(item.state) }}</li>
      </ul>
    </template>
    <button v-if="!granting && pinnedApps.length" type="button" class="text-button" :disabled="!!busy" @click="openGrant">Let Pebble use an app window…</button>
    <article v-else-if="granting" ref="grantCard" tabindex="-1" class="screen-grant" role="group" aria-label="Grant an app window">
      <strong>Let Pebble use an app window</strong>
      <p class="setting-note">Pebble can use one window of one app, for the current task, until the time runs out or you end access. Tabs in Pebble's browser are granted from the browser panel.</p>
      <p v-if="!pinnedApps.length" class="setting-note">Pebble can't read app identities here yet, so no app window can be granted from this panel.</p>
      <fieldset v-else>
        <legend>App</legend>
        <label v-for="item in pinnedApps" :key="keyOf(item)"><input type="radio" name="screen-app" :checked="chosenApp === keyOf(item)" @change="pickApp(item)" />{{ item.name }} · {{ item.bundle_id }}</label>
      </fieldset>
      <button v-if="app" type="button" class="secondary-button" :disabled="!!busy" @click="showWindows">Show its windows</button>
      <template v-if="windows">
        <p v-if="!windows.length" class="setting-note">This app has no open windows.</p>
        <fieldset v-else>
          <legend>Window (titles are shown as the app wrote them)</legend>
          <label v-for="(item, index) in windows" :key="index + ':' + item.title_sha256"><input type="radio" name="screen-window" :checked="chosenWindow === index" :disabled="duplicate(item)" @change="pickWindow(index)" />{{ item.title || 'No title' }}{{ item.focused ? ' · in front' : '' }}{{ duplicate(item) ? ' · another window has the same title, so Pebble cannot tell them apart' : '' }}</label>
        </fieldset>
      </template>
      <fieldset>
        <legend>What Pebble may do</legend>
        <label v-for="group in GROUPS" :key="group.key"><input type="checkbox" :checked="groups.includes(group.key)" @change="toggleGroup(group.key)" />{{ group.label }}<small>{{ group.note }}</small></label>
      </fieldset>
      <fieldset>
        <legend>For how long</legend>
        <label v-for="[seconds, name] in LIFETIMES" :key="seconds"><input type="radio" name="screen-lifetime" :checked="lifetime === seconds" @change="setLifetime(seconds)" />{{ name }}</label>
      </fieldset>
      <div v-if="offer" class="screen-offer" role="group" aria-label="Turn on Screen control">
        <p>{{ offer.message }}</p>
        <button type="button" class="primary-button" :disabled="!canGrant || !offer.available" @click="grant(true)">Turn on Screen control and grant</button>
      </div>
      <div class="setting-actions">
        <button v-if="!offer" type="button" class="primary-button" :disabled="!canGrant" @click="grant(false)">Grant this window</button>
        <button type="button" class="secondary-button" :disabled="busy === 'grant'" @click="closeGrant">Cancel</button>
      </div>
    </article>
  </section>
</template>

<style scoped>
.screen-sessions { border-top: 1px solid var(--border); margin-top: 22px; padding-top: 20px; }
.screen-sessions .setting-actions { flex-wrap: wrap; }
.screen-status, .screen-problem, .screen-empty { font-size: 11px; line-height: 1.6; margin: 8px 0; overflow-wrap: anywhere; }
.screen-problem { color: var(--danger); }
.screen-empty, .screen-label { color: var(--muted); }
.screen-session, .screen-grant { border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; margin: 10px 0; font-size: 11px; line-height: 1.6; overflow-wrap: anywhere; }
.screen-session.turn { border-color: var(--danger-line); }
.screen-session small { display: block; color: var(--muted); }
.screen-turn { margin: 6px 0; white-space: pre-wrap; }
.screen-label { font-size: 10px; margin: 10px 0 4px; }
.screen-ended { margin: 0; padding-left: 18px; font-size: 10px; color: var(--muted); overflow-wrap: anywhere; }
.screen-grant fieldset { border: 0; margin: 8px 0; padding: 0; }
.screen-grant legend { font-weight: 600; margin-bottom: 4px; }
.screen-grant label { display: block; margin: 3px 0; overflow-wrap: anywhere; }
.screen-grant label small { display: block; margin-left: 22px; color: var(--muted); }
.screen-offer { border: 1px solid var(--accent-soft); border-radius: 10px; padding: 8px 10px; margin: 8px 0; }
.screen-offer p { margin: 0 0 6px; }
</style>
