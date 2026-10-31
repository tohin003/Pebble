<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import { rpc } from '../bridge';

// The browser panel, main window only. Two browsers:
// - Your Chrome (Phase 22): tabs the person shares from the Pebble for Chrome toolbar button. A shared tab is
//   granted to a task here, for its one shared site; Pause hands every Chrome session back to the person and
//   Stop ends them all and clears every share in Chrome.
// - Pebble's own browser (Phase 16), with a private profile started, stopped and forgotten only from here. Until
//   the E-B1 check the service refuses to start it; Phase 24 (V1-127) then shows one line instead of a card whose
//   Start can never work.
// Page titles and addresses are untrusted page text: they render as literal text and never grant anything.
// Sign-in, payment and verification pages are always the person's turn.
// Pairing (design study D22 §7): the first time Chrome connects, this panel shows a 6-digit code that the person
// types into the Pebble for Chrome popup. The code is shown only here, never in a page.
// Phase 24 (t24-browser):
// - Connect Chrome (V1-043): the helper Chrome needs is added from here after one confirmation that lists the two
//   files it writes (browser_connect_status / browser_connect_install). The extension itself is loaded in Chrome's
//   Developer mode, said plainly (D6).
// - A grant made while no task can use the tab waits for the person's next message in this conversation (V1-007).
//   With Screen control off, the service answers with an offer; one confirmation turns it on and grants.
// - End access sits on the granted tab's own row (V1-124), and screenshots are said not to be kept (V1-059).
// Phase 43 (1.9.0, Pebble for Chrome 2):
// - Connect a browser (decision 8): beside Chrome, each other browser that has run on this Mac (Comet, Brave, Edge, Arc,
//   Chromium) gets its own Connect, one confirmation that lists its two files; only that browser's file is written.
// - An extension in another protocol is named, with the side to reload or update; Pebble's own tabs (opened under a
//   task's site grant, in its tab group) are listed, page text shown as such.
type Status = { running: boolean; available: boolean; reason: string; product: string; profile_exists: boolean };
type Tab = { tab_id: string; title: string; origin: string | null; url: string };
type Shared = { tab_id: string; origin: string; title: string; scope: 'tab' | 'site'; state: 'ready' | 'away' | 'handback'; acting: boolean;
  granted: boolean };
type Pending = { code: string; expiresIn: number; attemptsLeft: number };
type BrowserRow = { id: string; name: string; support: boolean; host: boolean; handshake: boolean };
type Owned = { tab_id: string; origin: string; state: 'ready' | 'away' | 'handback'; title: string; acting: boolean };
type Chrome = { connected: boolean; present: boolean; paired: boolean; pending: Pending | null; lockedFor: number; id: string; version: string;
  chrome: string; error: string; host: boolean; folder: string; paused: boolean; shared: Shared[]; browser: string; browsers: BrowserRow[];
  mismatch: string; owned: Owned[] };
type Outcome = { revoked_sessions?: unknown; stopped?: unknown; forgotten?: unknown; shares_cleared?: unknown };
type Target = { profile: 'pebble' | 'chrome'; tab_id: string; title: string; origin: string };
type ConnectFile = { role: string; path: string };
type Connect = { available: boolean; installed: boolean; ready: boolean; files: ConnectFile[]; problems: string[]; plan: string };
type Offer = { message: string; available: boolean };
type Granted = { ids: string[]; pending: boolean };
const props = defineProps<{ chatId?: string; refreshKey?: unknown }>();
const emit = defineEmits<{ changed: [] }>();

const GROUPS = [
  { key: 'look', label: 'Look around', actions: ['focus', 'scroll', 'expand', 'collapse'], note: 'Move focus, scroll, and open or close sections. Nothing changes.' },
  { key: 'fill', label: 'Fill in', actions: ['select', 'toggle', 'type'], note: 'Choose options, tick boxes and type text, never into a password, code or card field.' },
  { key: 'press', label: 'Press buttons', actions: ['press', 'enter'], note: 'Every press waits for your approval in Pebble first, because pages decide what a button does.' },
] as const;
type GroupKey = typeof GROUPS[number]['key'];
const LIFETIMES = [[300, '5 minutes'], [900, '15 minutes'], [1800, '30 minutes']] as const;
const MAX_SITES = 8;
const POLL_MS = 4000;
const LIVE = ['active', 'handoff', 'interrupted'];
const SCOPES: Record<string, string> = { tab: 'Just this visit', site: 'Kept for this site' };
const STATES: Record<string, string> = { ready: 'Ready', away: 'Paused: the tab is on another site', handback: "Your turn: a sign-in or payment page" };
const status = shallowRef<Status | null>(null);
const chrome = shallowRef<Chrome | null>(null);
const connect = shallowRef<Connect | null>(null);
// Phase 43: Connect for one browser other than Chrome, while its confirmation is open.
const other = shallowRef<{ id: string; name: string; plan: Connect } | null>(null);
const otherCard = shallowRef<HTMLElement | null>(null);
const granted = shallowRef<Map<string, Granted>>(new Map());
const tabs = ref<Tab[]>([]);
const address = ref('');
const busy = ref('');
const notice = ref('');
const problem = ref('');
const forgetting = ref(false);
const connecting = ref(false);
const granting = shallowRef<Target | null>(null);
const offer = shallowRef<Offer | null>(null);
const sites = ref<string[]>([]), added = ref<string[]>([]);
const extra = ref('');
const groups = ref<GroupKey[]>(['look', 'fill']);
const lifetime = ref(900);
const screenshots = ref(false);
// Element refs stay shallow: a template element is never made reactive.
const region = shallowRef<HTMLElement | null>(null), forgetCard = shallowRef<HTMLElement | null>(null), grantCard = shallowRef<HTMLElement | null>(null),
  connectCard = shallowRef<HTMLElement | null>(null);
let generation = 0, chromeGeneration = 0, connectGeneration = 0, grantVersion = 0, disposed = false, timer: ReturnType<typeof setTimeout> | undefined;

const word = (value: unknown) => typeof value === 'string' ? value : '';
const object = (value: unknown) => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const clip = (value: string, limit: number) => value.length > limit ? value.slice(0, limit) + '…' : value;
const own = (map: Record<string, string>, key: string) => Object.hasOwn(map, key) ? map[key] : '';
const chosenActions = computed(() => GROUPS.filter(group => groups.value.includes(group.key)).flatMap(group => [...group.actions]));
// Only the granted tab's own site and, in Pebble's browser, sites the person types are offered; another tab's site never is.
const siteChoices = computed(() => {
  const own = granting.value?.origin;
  return [...new Set([...(own ? [own] : []), ...(granting.value?.profile === 'pebble' ? added.value : [])])];
});
// Pebble's own browser shows as a card only once it can start (or runs); until then one line says so.
const ownBrowser = computed(() => !!status.value && (status.value.available || status.value.running));
const chromeState = computed(() => {
  const item = chrome.value;
  if (!item) return 'Checking…';
  if (item.connected) return item.paused ? 'Paused' : 'Connected';
  if (item.pending) return 'Pairing';
  if (item.lockedFor) return 'Pairing paused';
  if (item.present) return 'Not paired';
  return item.host ? 'Not connected' : 'Not set up';
});
function readStatus(value: unknown): Status {
  const item = object(value);
  return { running: item.running === true, available: item.available === true, reason: clip(word(item.reason), 600),
    product: clip(word(item.product), 64), profile_exists: item.profile_exists === true };
}
function shared(value: unknown): Shared | null {
  const item = object(value);
  if (typeof item.tab_id !== 'string' || !/^[0-9]{1,12}$/.test(item.tab_id) || typeof item.origin !== 'string' || !item.origin) return null;
  if (item.scope !== 'tab' && item.scope !== 'site') return null;
  if (item.state !== 'ready' && item.state !== 'away' && item.state !== 'handback') return null;
  return { tab_id: item.tab_id, origin: clip(item.origin, 300), title: clip(word(item.title), 200), scope: item.scope, state: item.state,
    acting: item.acting === true, granted: item.granted === true };
}
function readPending(value: unknown): Pending | null {
  const item = object(value);
  if (typeof item.code !== 'string' || !/^[0-9]{6}$/.test(item.code)) return null;
  return { code: item.code, expiresIn: count(item.expires_in), attemptsLeft: count(item.attempts_left) };
}
function browserRow([id, value]: [string, unknown]): BrowserRow | null {
  const item = object(value);
  if (!/^[a-z][a-z-]{1,23}$/.test(id) || typeof item.name !== 'string' || !item.name) return null;
  return { id, name: clip(item.name, 40), support: item.support === true, host: item.host === true,
    handshake: !!item.handshake && typeof item.handshake === 'object' };
}
function ownedTab(value: unknown): Owned | null {
  const item = object(value);
  if (typeof item.tab_id !== 'string' || !/^[0-9]{1,12}$/.test(item.tab_id)) return null;
  if (item.state !== 'ready' && item.state !== 'away' && item.state !== 'handback') return null;
  return { tab_id: item.tab_id, origin: clip(word(item.origin), 300), state: item.state, title: clip(word(item.title), 200), acting: item.acting === true };
}
function readChrome(value: unknown): Chrome {
  const item = object(value), extension = object(item.extension), bridge = object(item.bridge), host = object(item.host);
  const pairing = object(item.pairing), mismatch = object(item.mismatch);
  return { connected: extension.connected === true, present: extension.present === true, paired: pairing.paired === true,
    pending: readPending(pairing.pending), lockedFor: count(pairing.locked_for), id: clip(word(extension.id), 64),
    version: clip(word(extension.version), 32),
    chrome: clip(word(extension.chrome), 64), error: clip(word(bridge.error), 300), host: host.chrome === true,
    folder: clip(word(item.extension_folder), 1000), paused: item.paused === true,
    shared: (Array.isArray(item.shared_tabs) ? item.shared_tabs : []).map(shared).filter((entry): entry is Shared => !!entry).slice(0, 16),
    browser: clip(word(extension.browser), 24),
    browsers: Object.entries(object(item.browsers)).map(browserRow).filter((entry): entry is BrowserRow => !!entry).slice(0, 12),
    mismatch: typeof mismatch.protocol === 'string' ? clip(mismatch.protocol, 32) : '',
    owned: (Array.isArray(item.owned_tabs) ? item.owned_tabs : []).map(ownedTab).filter((entry): entry is Owned => !!entry).slice(0, 16) };
}
// The browsers beyond Chrome that have run on this Mac (or already have Pebble's file), for their own Connect.
const otherBrowsers = computed(() => (chrome.value?.browsers || []).filter(row => row.id !== 'chrome' && (row.support || row.host)));
const browserName = computed(() => (chrome.value?.browsers || []).find(row => row.id === chrome.value?.browser)?.name || '');
const OWNED_STATES: Record<string, string> = { ready: 'Pebble is using it for a task', away: 'Paused: on a site the task may not use',
  handback: 'Your turn: a sign-in or payment page' };
// The Connect Chrome plan: the two files the helper needs, what stands in the way, and the digest one confirmation sends.
function readConnect(value: unknown): Connect {
  const item = object(value);
  const files = (Array.isArray(item.files) ? item.files : []).map(object).filter(file => typeof file.path === 'string' && !!file.path)
    .slice(0, 2).map(file => ({ role: word(file.role), path: clip(word(file.path), 1000) }));
  const problems = (Array.isArray(item.problems) ? item.problems : []).map(entry => clip(word(object(entry).message), 600)).filter(Boolean).slice(0, 6);
  const plan = typeof item.plan_sha256 === 'string' && /^[0-9a-f]{64}$/.test(item.plan_sha256) ? item.plan_sha256 : '';
  return { available: item.available === true, installed: item.installed === true, ready: item.ready === true && !!plan && files.length === 2,
    files, problems, plan };
}
function tab(value: unknown): Tab | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  if (typeof item.tab_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(item.tab_id)) return null;
  return { tab_id: item.tab_id, title: clip(word(item.title), 200), origin: typeof item.origin === 'string' && item.origin ? clip(item.origin, 300) : null,
    url: clip(word(item.url), 2000) };
}
function count(value: unknown) { return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : 0; }
function endedSessions(value: unknown) {
  const ended = count(value);
  return ended ? ' ' + (ended === 1 ? '1 tab session' : ended + ' tab sessions') + ' ended.' : '';
}
function failure(cause: unknown) {
  const text = cause instanceof Error ? cause.message : String(cause);
  return clip(text || 'The request failed.', 1000);
}
const grantOf = (profile: string, tabId: string) => granted.value.get(profile + ':' + tabId) || null;
const scopeName = (item: Shared) => own(SCOPES, item.scope);
const stateName = (item: Shared) => item.state !== 'ready' ? own(STATES, item.state)
  : item.acting ? 'Pebble is working here' : grantOf('chrome', item.tab_id)?.pending ? 'Your next message here can use it'
    : item.granted ? 'A task can use it' : own(STATES, item.state);
const minutes = (seconds: number) => { const value = Math.max(1, Math.ceil(seconds / 60)); return value === 1 ? '1 minute' : value + ' minutes'; };
const tries = (value: number) => value === 1 ? '1 try' : value + ' tries';
const grouped = (code: string) => code.slice(0, 3) + ' ' + code.slice(3);
const spelled = (code: string) => code.split('').join(' ');

async function load() {
  const version = ++generation;
  try {
    const next = readStatus(await rpc<unknown>('browser_status'));
    if (disposed || version !== generation) return;
    status.value = next;
    if (!next.running) {
      tabs.value = [];
      if (granting.value?.profile === 'pebble') granting.value = null;
    } else {
      const rows = await rpc<unknown>('browser_tabs');
      if (disposed || version !== generation) return;
      tabs.value = (Array.isArray(rows) ? rows : []).map(tab).filter((item): item is Tab => !!item).slice(0, 64);
    }
  } catch (cause) { if (!disposed && version === generation) problem.value = failure(cause); }
  await loadConnect();
  await loadChrome();
}
async function loadConnect() {
  const version = ++connectGeneration;
  try {
    const next = readConnect(await rpc<unknown>('browser_connect_status'));
    if (!disposed && version === connectGeneration) connect.value = next;
  } catch (cause) { if (!disposed && version === connectGeneration) problem.value = failure(cause); }
}
// The Chrome side changes in Chrome (shares, the working dot), so it is re-read while the panel is open.
async function loadChrome() {
  const version = ++chromeGeneration;
  clearTimeout(timer); timer = undefined;
  try {
    const next = readChrome(await rpc<unknown>('browser_extension_status'));
    if (disposed || version !== chromeGeneration) return;
    chrome.value = next;
    await loadGrants(version);
  } catch (cause) { if (!disposed && version === chromeGeneration) problem.value = failure(cause); }
  finally {
    if (!disposed && version === chromeGeneration) {
      keepGrant();
      timer = setTimeout(() => { timer = undefined; void loadChrome(); }, POLL_MS);
    }
  }
}
// This conversation's live grants per tab, so a granted tab's row can end access (and say when it waits for the next
// message). Read only while a tab is shared or open.
async function loadGrants(version: number) {
  const chat = props.chatId;
  if (!chat || (!(chrome.value?.shared.length) && !tabs.value.length)) { granted.value = new Map(); return; }
  const rows = await rpc<unknown>('screen_sessions', { id: chat });
  if (disposed || version !== chromeGeneration || chat !== props.chatId) return;
  const next = new Map<string, Granted>();
  for (const row of Array.isArray(rows) ? rows : []) {
    const item = object(row), target = object(item.target);
    if (typeof item.session_id !== 'string' || !/^[0-9a-f]{32}$/.test(item.session_id) || !LIVE.includes(word(item.state))) continue;
    if ((target.profile !== 'chrome' && target.profile !== 'pebble') || typeof target.tab_id !== 'string' || !target.tab_id) continue;
    const key = target.profile + ':' + target.tab_id, entry = next.get(key) || { ids: [], pending: true };
    next.set(key, { ids: [...entry.ids, item.session_id], pending: entry.pending && item.pending === true });
  }
  granted.value = next;
}
// An open grant always describes a tab that is still listed (and, in Chrome, still ready), or closes.
function keepGrant() {
  const open = granting.value;
  if (!open) return;
  const still = open.profile === 'pebble'
    ? tabs.value.some(item => item.tab_id === open.tab_id && item.origin === open.origin)
    : (chrome.value?.shared || []).some(item => item.tab_id === open.tab_id && item.origin === open.origin && item.state === 'ready');
  if (!still) granting.value = null;
}
// changes: the reply may have ended screen sessions, so the sessions panel reloads.
async function act(name: string, work: () => Promise<string | null>, changes = false) {
  if (busy.value) return;
  busy.value = name; notice.value = ''; problem.value = '';
  try {
    const message = await work();
    if (disposed || message === null) return;
    notice.value = message;
    if (changes) emit('changed');
  } catch (cause) { if (!disposed) problem.value = failure(cause); }
  finally { busy.value = ''; }
  if (!disposed) await load();
}
const start = () => act('start', async () => {
  await rpc('browser_start');
  return "Pebble's browser is running with its private profile.";
});
const stop = () => act('stop', async () => {
  const result = await rpc<Outcome>('browser_stop');
  if (granting.value?.profile === 'pebble') granting.value = null;
  return (result?.stopped === false ? "Pebble's browser was not running." : "Pebble's browser stopped.") + endedSessions(result?.revoked_sessions);
}, true);
const openTab = () => act('open', async () => {
  const url = address.value.trim();
  if (!url) return null;
  const result = await rpc<{ origin?: unknown }>('browser_open_tab', { url });
  address.value = '';
  const origin = word(result?.origin);
  return 'Opened a new tab' + (origin ? ' on ' + clip(origin, 300) : '') + '.';
});
const pauseChrome = () => act('pause', async () => {
  await rpc('browser_extension_pause');
  return "Paused. Pebble doesn't read or act in your Chrome tabs until you resume.";
}, true);
const resumeChrome = () => act('resume', async () => {
  await rpc('browser_extension_resume');
  return 'Resumed. Pebble looks at each tab again before it does anything.';
}, true);
const stopChrome = () => act('chrome-stop', async () => {
  const result = await rpc<Outcome>('browser_extension_stop');
  if (granting.value?.profile === 'chrome') granting.value = null;
  const cleared = count(result?.shares_cleared);
  return 'Stopped. ' + (cleared ? 'Pebble let go of ' + (cleared === 1 ? '1 shared tab' : cleared + ' shared tabs') + ' in Chrome.'
    : 'No Chrome tab was shared.') + endedSessions(result?.revoked_sessions);
}, true);
// Forgetting the pairing also lifts a pairing lock (too many wrong codes), so the same route serves "Allow pairing now".
const unpairChrome = () => act('unpair', async () => {
  const paired = chrome.value?.paired === true;
  const result = await rpc<Outcome>('browser_extension_unpair');
  if (granting.value?.profile === 'chrome') granting.value = null;
  return (paired ? 'Pairing forgotten. Chrome pairs again with a new code before Pebble can use any tab there.'
    : 'Pairing is allowed again. In Chrome, choose "Pair with Pebble" for a new code.') + endedSessions(result?.revoked_sessions);
}, true);
// End access for one tab: every live grant this conversation holds on it, pending or not.
const endAccess = (profile: 'chrome' | 'pebble', tabId: string) => act('end', async () => {
  const entry = grantOf(profile, tabId);
  if (!entry) return null;
  for (const id of entry.ids) await rpc('screen_session_revoke', { session_id: id });
  return 'Access ended. Pebble can no longer use this tab.';
}, true);
async function copyFolder() {
  const folder = chrome.value?.folder;
  if (!folder) return;
  try { await navigator.clipboard.writeText(folder); if (!disposed) notice.value = 'Folder path copied.'; }
  catch { if (!disposed) problem.value = 'Copying is not available here; select the path and copy it.'; }
}
function askForget() {
  if (busy.value) return;
  forgetting.value = true;
  void nextTick(() => forgetCard.value?.focus());
}
function cancelForget() {
  forgetting.value = false;
  void nextTick(() => region.value?.focus());
}
const confirmForget = () => act('forget', async () => {
  const result = await rpc<Outcome>('browser_profile_forget');
  forgetting.value = false;
  if (granting.value?.profile === 'pebble') granting.value = null;
  return (result?.forgotten === true ? "Browser profile forgotten. You're signed out of every site in Pebble's browser."
    : 'There was no saved browser profile to forget.') + endedSessions(result?.revoked_sessions);
}, true);
// Connect Chrome: the confirmation lists the two files; installing sends the digest of exactly that plan.
function askConnect() {
  if (busy.value || !connect.value?.available) return;
  connecting.value = true;
  void nextTick(() => connectCard.value?.focus());
}
function cancelConnect() {
  connecting.value = false;
  void nextTick(() => region.value?.focus());
}
const confirmConnect = () => act('connect', async () => {
  const plan = connect.value?.plan;
  if (!plan) return null;
  const result = readConnect(await rpc<unknown>('browser_connect_install', { confirm: plan }));
  connecting.value = false;
  connect.value = result;
  return result.installed ? "Pebble's helper for Chrome is added. Next, add Pebble for Chrome in Chrome (the steps below), then pair."
    : "Pebble's helper for Chrome was not added.";
});

// Phase 43: Connect one other browser. The confirmation lists the plan's two files for that browser only, and installing
// sends the digest of exactly that plan with the browser named.
async function askBrowser(row: BrowserRow) {
  if (busy.value) return;
  problem.value = '';
  try {
    const plan = readConnect(await rpc<unknown>('browser_connect_status', { browsers: [row.id] }));
    if (disposed) return;
    other.value = { id: row.id, name: row.name, plan };
    void nextTick(() => otherCard.value?.focus());
  } catch (cause) { if (!disposed) problem.value = failure(cause); }
}
function cancelBrowser() {
  other.value = null;
  void nextTick(() => region.value?.focus());
}
const confirmBrowser = () => act('connect-browser', async () => {
  const open = other.value;
  if (!open?.plan.plan) return null;
  const result = readConnect(await rpc<unknown>('browser_connect_install', { confirm: open.plan.plan, browsers: [open.id] }));
  other.value = null;
  return result.installed ? `Pebble's helper for ${open.name} is added. Next, load Pebble for Chrome in ${open.name} (the same folder), then pair.`
    : `Pebble's helper for ${open.name} was not added.`;
});

function openGrant(target: Target) {
  if (busy.value || !props.chatId || !target.origin) return;
  grantVersion++;
  granting.value = target; sites.value = [target.origin]; added.value = []; extra.value = ''; groups.value = ['look', 'fill']; lifetime.value = 900;
  screenshots.value = false; offer.value = null; problem.value = '';
  void nextTick(() => grantCard.value?.focus());
}
const openPebbleGrant = (item: Tab) => item.origin && openGrant({ profile: 'pebble', tab_id: item.tab_id, title: item.title, origin: item.origin });
const openChromeGrant = (item: Shared) => openGrant({ profile: 'chrome', tab_id: item.tab_id, title: item.title, origin: item.origin });
function closeGrant() {
  grantVersion++;
  granting.value = null; offer.value = null;
  void nextTick(() => region.value?.focus());
}
function toggleSite(origin: string) { sites.value = sites.value.includes(origin) ? sites.value.filter(item => item !== origin) : [...sites.value, origin]; }
function toggleGroup(key: GroupKey) { groups.value = groups.value.includes(key) ? groups.value.filter(item => item !== key) : [...groups.value, key]; }
function setLifetime(seconds: number) { lifetime.value = seconds; }
function toggleScreenshots() { screenshots.value = !screenshots.value; }
function addSite() {
  const value = extra.value.trim().replace(/\/+$/, '').toLowerCase();
  if (!/^(https:\/\/[a-z0-9.-]+(:[0-9]{1,5})?|http:\/\/(127\.0\.0\.1|localhost|\[::1\])(:[0-9]{1,5})?)$/.test(value)) {
    problem.value = 'Enter a site as https://example.com, with nothing after its name.';
    return;
  }
  problem.value = '';
  if (!siteChoices.value.includes(value) && siteChoices.value.length < MAX_SITES) added.value = [...added.value, value];
  if (siteChoices.value.includes(value) && !sites.value.includes(value)) sites.value = [...sites.value, value];
  extra.value = '';
}
// enable: the person confirmed turning Screen control on for this grant (only after the service offered it).
async function grant(enable?: unknown) {
  const item = granting.value, chat = props.chatId, origins = siteChoices.value.filter(origin => sites.value.includes(origin));
  const actions = chosenActions.value, ttl = lifetime.value, version = grantVersion, turnOn = enable === true;
  // Screenshots are a Chrome-only opt-in, off by default: a picture shows everything on the page.
  const shot = item?.profile === 'chrome' && screenshots.value;
  if (busy.value || !item || !chat || !origins.length || origins.length > MAX_SITES || !actions.length) return;
  busy.value = 'grant'; notice.value = ''; problem.value = '';
  try {
    const reply = object(await rpc<unknown>('screen_session_grant', { id: chat, target: { kind: 'browser', profile: item.profile, tab_id: item.tab_id, origins },
      actions, ttl, ...(item.profile === 'chrome' ? { screenshots: shot } : {}), ...(turnOn ? { enable_screen: true } : {}) }));
    if (disposed || chat !== props.chatId || version !== grantVersion) return;
    if (reply.status === 'needs_screen_permission') {
      offer.value = { message: clip(word(reply.message), 600) || 'Screen control is off in Permissions.', available: object(reply.enable).available === true };
      return;
    }
    const when = reply.pending === true ? 'from your next message in this conversation' : 'for the current task';
    notice.value = 'Pebble can use this ' + (item.profile === 'chrome' ? 'Chrome tab' : 'tab') + ' on ' + origins.join(', ') + ' ' + when + ', for '
      + (LIFETIMES.find(([seconds]) => seconds === ttl)?.[1] || ttl + ' seconds') + '.'
      + (shot ? ' It may also see screenshots of the tab; they go to the model and are not kept.' : '')
      + (reply.screen_enabled === true ? ' Screen control is now on in Permissions.' : '') + ' End access on its row here at any time.';
    closeGrant();
    emit('changed');
  } catch (cause) { if (!disposed && chat === props.chatId && version === grantVersion) problem.value = failure(cause); }
  finally { busy.value = ''; }
  if (!disposed && !granting.value) await loadChrome();
}

watch(() => props.chatId, () => { grantVersion++; granting.value = null; offer.value = null; granted.value = new Map(); });
watch(() => props.refreshKey, () => { void load(); });
onMounted(load);
onBeforeUnmount(() => { disposed = true; generation++; chromeGeneration++; connectGeneration++; grantVersion++; other.value = null; clearTimeout(timer); timer = undefined; });
</script>

<template>
  <section ref="region" tabindex="-1" class="pebble-browser" aria-label="Browser" :aria-busy="!!busy">
    <div class="section-label">BROWSER</div>
    <p class="browser-status" role="status" aria-live="polite">{{ notice }}</p>
    <p v-if="problem" class="browser-problem" role="alert">{{ problem }}</p>

    <section class="browser-card" aria-label="Your Chrome">
      <header class="browser-head">
        <strong>Your Chrome</strong>
        <span class="browser-pill" :class="{ on: chrome?.connected && !chrome?.paused }">{{ chromeState }}</span>
      </header>
      <p class="setting-note">Pebble works only in tabs you share from Chrome's toolbar, only on the site you shared, and only for a task you choose here. Sign-in and payment pages are always your turn, and anything that sends, buys, posts or deletes waits for your approval.</p>
      <template v-if="chrome">
        <p v-if="chrome.connected && chrome.chrome" class="browser-meta">{{ browserName && chrome.browser !== 'chrome' ? browserName + ' (Chromium ' + chrome.chrome + ')' : 'Chrome ' + chrome.chrome }} · Pebble for Chrome {{ chrome.version || 'unknown version' }}</p>
        <p v-if="chrome.mismatch && !chrome.connected" class="browser-state"><strong>Pebble for Chrome needs updating.</strong> A copy speaking {{ chrome.mismatch }} tried to connect; this Pebble speaks a different version. Reload Pebble for Chrome on the browser's Extensions page, or load the folder below again.</p>
        <p v-if="chrome.paused" class="browser-state"><strong>Paused.</strong> Pebble doesn't read or act in your Chrome tabs until you resume.</p>
        <div class="browser-pairing" aria-live="polite">
          <article v-if="chrome.pending" class="browser-code-card" role="group" aria-label="Pairing code">
            <strong>Pair Pebble for Chrome</strong>
            <p class="browser-code"><span aria-hidden="true">{{ grouped(chrome.pending.code) }}</span><span class="browser-sr">{{ spelled(chrome.pending.code) }}</span></p>
            <p class="setting-note">Type this code in the Pebble for Chrome popup, never into a web page. It works once, expires in about {{ minutes(chrome.pending.expiresIn) }}, and has {{ tries(chrome.pending.attemptsLeft) }} left.</p>
          </article>
          <template v-else-if="chrome.lockedFor && !chrome.connected">
            <p class="browser-state"><strong>Pairing is paused.</strong> Someone typed too many wrong codes, so Pebble shows no new code for about {{ minutes(chrome.lockedFor) }}. If that was you, allow pairing now.</p>
            <button v-if="!chrome.paired" type="button" class="secondary-button" :disabled="!!busy" @click="unpairChrome">Allow pairing now</button>
          </template>
          <p v-else-if="chrome.present && !chrome.connected" class="setting-note">Chrome is asking to connect. In Chrome, click the Pebble button and choose "Pair with Pebble"; the code appears here.</p>
        </div>
        <ul v-if="chrome.shared.length" class="browser-shared" aria-label="Tabs you shared from Chrome">
          <li v-for="item in chrome.shared" :key="item.tab_id" :aria-label="'Shared tab on ' + item.origin">
            <span class="browser-dot" :class="{ working: item.acting && item.state === 'ready', turn: item.state !== 'ready' }" aria-hidden="true"></span>
            <div class="browser-words">
              <small>Page title, written by the page</small>
              <p class="browser-text">{{ item.title || (item.state === 'ready' ? 'No title' : 'Hidden while Pebble is paused here') }}</p>
              <small>{{ item.origin }} · {{ scopeName(item) }} · {{ stateName(item) }}</small>
            </div>
            <div class="browser-row-actions">
              <button v-if="chatId && item.state === 'ready'" type="button" class="text-button" :disabled="!!busy" @click="openChromeGrant(item)">Let Pebble use this tab…</button>
              <button v-if="grantOf('chrome', item.tab_id)" type="button" class="text-button" :disabled="!!busy" @click="endAccess('chrome', item.tab_id)">End access</button>
            </div>
          </li>
        </ul>
        <p v-else-if="chrome.connected" class="setting-note">No tab is shared. In Chrome, click the Pebble button on a tab and choose "Let Pebble use this tab".</p>
        <ul v-if="chrome.owned.length" class="browser-shared" aria-label="Tabs Pebble opened">
          <li v-for="item in chrome.owned" :key="'owned-' + item.tab_id" :aria-label="'Pebble\'s tab' + (item.origin ? ' on ' + item.origin : '')">
            <span class="browser-dot" :class="{ working: item.acting && item.state === 'ready', turn: item.state !== 'ready' }" aria-hidden="true"></span>
            <div class="browser-words">
              <small>Pebble opened this tab · page title, written by the page</small>
              <p class="browser-text">{{ item.title || 'No title' }}</p>
              <small>{{ item.origin || 'a site the task may not use' }} · {{ OWNED_STATES[item.state] }}</small>
            </div>
          </li>
        </ul>
        <div v-if="chrome.connected || chrome.shared.length" class="setting-actions">
          <button v-if="!chrome.paused" type="button" class="secondary-button" :disabled="!!busy" @click="pauseChrome">Pause</button>
          <button v-else type="button" class="primary-button" :disabled="!!busy" @click="resumeChrome">Resume</button>
          <button type="button" class="secondary-button danger" :disabled="busy === 'chrome-stop'" @click="stopChrome">Stop Pebble in Chrome</button>
        </div>
        <details class="browser-setup" :open="!chrome.connected">
          <summary>{{ chrome.connected ? 'How Pebble for Chrome is set up' : 'Connect Chrome' }}</summary>
          <ol>
            <li v-if="connect?.installed || (!connect && chrome.host)">Pebble's helper for Chrome is added on this Mac.</li>
            <li v-else-if="connect?.available">Pebble adds a small helper so Chrome can reach it. <button type="button" class="text-button" :disabled="!!busy" @click="askConnect">Connect Chrome…</button></li>
            <li v-else>{{ connect?.problems[0] || "Pebble's helper for Chrome can't be added from here." }}</li>
            <li>Pebble for Chrome isn't in the Chrome Web Store yet, so you add it yourself: in Chrome, open the Extensions page (chrome://extensions) and turn on Developer mode.</li>
            <li>Choose "Load unpacked" and pick Pebble's extension folder<template v-if="chrome.folder">: <code class="browser-text">{{ chrome.folder }}</code></template>.</li>
            <li>Pin Pebble to Chrome's toolbar, click it and choose "Pair with Pebble". Type the code this panel shows.</li>
            <li>On a tab you want help with, click Pebble in Chrome and choose "Let Pebble use this tab".</li>
            <li v-if="chrome.id">Chrome should list the extension with the ID {{ chrome.id }}. If it shows another, remove it and load Pebble's folder again.</li>
          </ol>
          <template v-if="otherBrowsers.length">
            <p class="setting-note">Pebble for Chrome also works in the other browsers on this Mac. Each needs its own helper file, added only for the browsers you choose; then load the same folder on that browser's Extensions page (Developer mode) and pair.</p>
            <ul class="browser-files" aria-label="Other browsers">
              <li v-for="row in otherBrowsers" :key="row.id">
                {{ row.name }}: {{ row.handshake ? 'connected before' : row.host ? 'helper added' : 'not set up' }}
                <button v-if="!row.host" type="button" class="text-button" :disabled="!!busy" @click="askBrowser(row)">{{ 'Connect ' + row.name + '…' }}</button>
              </li>
            </ul>
          </template>
          <button v-if="chrome.folder" type="button" class="text-button" @click="copyFolder">Copy folder path</button>
          <p v-if="chrome.error" class="setting-note">{{ chrome.error }}</p>
          <template v-if="chrome.paired">
            <p class="setting-note">Chrome is paired with Pebble. Forgetting the pairing ends every Chrome session; Chrome then pairs again with a new code.</p>
            <button type="button" class="text-button" :disabled="!!busy" @click="unpairChrome">Forget pairing</button>
          </template>
        </details>
        <article v-if="other" ref="otherCard" tabindex="-1" class="browser-confirm" role="group" :aria-label="'Connect ' + other.name">
          <strong>{{ 'Add Pebble\'s helper for ' + other.name + '?' }}</strong>
          <p>Pebble writes these two files on this Mac and nothing else. It doesn't change the browser's settings or add the extension for you.</p>
          <ul class="browser-files">
            <li v-for="file in other.plan.files" :key="file.path"><code class="browser-text">{{ file.path }}</code></li>
          </ul>
          <p v-for="item in other.plan.problems" :key="item" class="browser-problem">{{ item }}</p>
          <div class="setting-actions">
            <button type="button" class="primary-button" :disabled="!!busy || !other.plan.ready" @click="confirmBrowser">Add the helper</button>
            <button type="button" class="secondary-button" :disabled="busy === 'connect-browser'" @click="cancelBrowser">Cancel</button>
          </div>
        </article>
        <article v-if="connecting && connect" ref="connectCard" tabindex="-1" class="browser-confirm" role="group" aria-label="Connect Chrome">
          <strong>Add Pebble's helper for Chrome?</strong>
          <p>Pebble writes these two files on this Mac and nothing else. It doesn't change Chrome's settings or add the extension for you.</p>
          <ul class="browser-files">
            <li v-for="file in connect.files" :key="file.path"><code class="browser-text">{{ file.path }}</code></li>
          </ul>
          <p v-for="item in connect.problems" :key="item" class="browser-problem">{{ item }}</p>
          <div class="setting-actions">
            <button type="button" class="primary-button" :disabled="!!busy || !connect.ready" @click="confirmConnect">Add the helper</button>
            <button type="button" class="secondary-button" :disabled="busy === 'connect'" @click="cancelConnect">Cancel</button>
          </div>
        </article>
      </template>
      <p v-else-if="!problem" class="setting-note">Checking Pebble for Chrome…</p>
    </section>

    <section v-if="ownBrowser && status" class="browser-card" aria-label="Pebble's browser">
      <div class="section-label">PEBBLE'S BROWSER</div>
      <p class="setting-note">A separate browser with its own private profile, started and stopped only here. Pebble uses a tab only when you grant it, only on the sites you tick, for the current task. Sign-in, codes and CAPTCHAs are always your turn. Your own browser and its sign-ins are not touched.</p>
      <p class="browser-state"><strong>{{ status.running ? 'Running' : 'Not running' }}</strong>{{ status.running && status.product ? ' · ' + status.product : '' }}</p>
      <div class="setting-actions">
        <button v-if="!status.running" type="button" class="primary-button" :disabled="!!busy" @click="start">Start Pebble's browser</button>
        <button v-else type="button" class="secondary-button" :disabled="!!busy" @click="stop">Stop Pebble's browser</button>
        <button type="button" class="text-button" :disabled="!!busy" @click="load">Check again</button>
      </div>
      <template v-if="status.running">
        <label class="browser-address">Address<input v-model="address" type="url" maxlength="2048" autocomplete="off" :spellcheck="false" placeholder="https://" /></label>
        <button type="button" class="secondary-button" :disabled="!!busy || !address.trim()" @click="openTab">Open in a new tab</button>
        <p v-if="!tabs.length" class="setting-note">No tabs are open.</p>
        <article v-for="item in tabs" :key="item.tab_id" class="browser-tab" :aria-label="'Tab on ' + (item.origin || 'no site')">
          <small>Page title, written by the page</small>
          <p class="browser-text">{{ item.title || 'No title' }}</p>
          <small>Site: {{ item.origin || 'not a website, so it cannot be granted' }}</small>
          <small v-if="item.url">Address: {{ item.url }}</small>
          <button v-if="chatId && item.origin" type="button" class="text-button" :disabled="!!busy" @click="openPebbleGrant(item)">Let Pebble use this tab…</button>
          <button v-if="grantOf('pebble', item.tab_id)" type="button" class="text-button" :disabled="!!busy" @click="endAccess('pebble', item.tab_id)">End access</button>
        </article>
      </template>
      <p class="setting-note">{{ status.profile_exists ? "Its profile, with any sign-ins you made in Pebble's browser, is saved on this Mac." : 'No browser profile is saved.' }}</p>
      <button type="button" class="text-button danger" :disabled="!!busy || (!status.profile_exists && !status.running)" @click="askForget">Forget browser profile…</button>
    </section>
    <template v-else-if="status">
      <p class="setting-note">Pebble's own browser is not available yet.</p>
      <button v-if="status.profile_exists" type="button" class="text-button danger" :disabled="!!busy" @click="askForget">Forget browser profile…</button>
    </template>
    <article v-if="forgetting" ref="forgetCard" tabindex="-1" class="browser-confirm" role="group" aria-label="Confirm forgetting the browser profile">
      <strong>Forget Pebble's browser profile?</strong>
      <p>This signs you out of every site in Pebble's browser and deletes its cookies, saved site data and history. It stops the browser first and ends any tab Pebble is using. Your own browser is not affected.</p>
      <div class="setting-actions">
        <button type="button" class="secondary-button danger" :disabled="!!busy" @click="confirmForget">Forget profile and sign out</button>
        <button type="button" class="secondary-button" :disabled="busy === 'forget'" @click="cancelForget">Cancel</button>
      </div>
    </article>

    <article v-if="granting" ref="grantCard" tabindex="-1" class="browser-grant" role="group" aria-label="Grant this tab">
      <strong>{{ granting.profile === 'chrome' ? 'Let Pebble use this Chrome tab' : 'Let Pebble use this tab' }}</strong>
      <p class="browser-text">{{ granting.title || 'No title' }}</p>
      <p v-if="granting.profile === 'chrome'" class="setting-note">The title is page text; it can't give Pebble permission. Pebble can use this tab only on {{ granting.origin }}, the site you shared. If the tab goes to another site, shows a sign-in or payment page, or you stop sharing in Chrome, Pebble stops and it's your turn.</p>
      <p v-else class="setting-note">The title is page text; it can't give Pebble permission. If the tab goes to a site you didn't tick, or shows a sign-in, a code or a CAPTCHA, Pebble stops and it's your turn.</p>
      <p class="setting-note">With a task running here, it can use the tab now; otherwise your next message in this conversation can.</p>
      <fieldset>
        <legend>Sites Pebble may use in this tab</legend>
        <label v-for="origin in siteChoices" :key="origin"><input type="checkbox" :checked="sites.includes(origin)" @change="toggleSite(origin)" />{{ origin }}</label>
      </fieldset>
      <template v-if="granting.profile === 'pebble'">
        <label class="browser-address">Add another site<input v-model="extra" maxlength="300" autocomplete="off" :spellcheck="false" placeholder="https://example.com" /></label>
        <button type="button" class="text-button" :disabled="!extra.trim() || siteChoices.length >= MAX_SITES" @click="addSite">Add site</button>
      </template>
      <fieldset>
        <legend>What Pebble may do</legend>
        <label v-for="group in GROUPS" :key="group.key"><input type="checkbox" :checked="groups.includes(group.key)" @change="toggleGroup(group.key)" />{{ group.label }}<small>{{ group.note }}</small></label>
      </fieldset>
      <fieldset v-if="granting.profile === 'chrome'">
        <legend>Screenshots</legend>
        <label><input type="checkbox" :checked="screenshots" @change="toggleScreenshots" />Let Pebble see screenshots of this tab<small>Only models that can see images get them. A screenshot shows everything visible on the page, including anything private; Pebble still reads the page as text either way. Screenshots go to the model and are not kept.</small></label>
      </fieldset>
      <fieldset>
        <legend>For how long</legend>
        <label v-for="[seconds, name] in LIFETIMES" :key="seconds"><input type="radio" name="browser-lifetime" :checked="lifetime === seconds" @change="setLifetime(seconds)" />{{ name }}</label>
      </fieldset>
      <div v-if="offer" class="browser-offer" role="group" aria-label="Turn on Screen control">
        <p>{{ offer.message }}</p>
        <button type="button" class="primary-button" :disabled="!!busy || !offer.available" @click="grant(true)">Turn on Screen control and grant</button>
      </div>
      <div class="setting-actions">
        <button v-if="!offer" type="button" class="primary-button" :disabled="!!busy || !sites.length || !chosenActions.length" @click="grant(false)">Grant this tab</button>
        <button type="button" class="secondary-button" :disabled="busy === 'grant'" @click="closeGrant">Cancel</button>
      </div>
    </article>
  </section>
</template>

<style scoped>
.pebble-browser { border-top: 1px solid var(--border); margin-top: 22px; padding-top: 20px; }
.pebble-browser .setting-actions { flex-wrap: wrap; }
.browser-status, .browser-problem, .browser-state, .browser-meta { font-size: 11px; line-height: 1.6; margin: 8px 0; overflow-wrap: anywhere; }
.browser-status:empty { margin: 0; }
.browser-problem { color: var(--danger); }
.browser-meta { color: var(--muted); }
.browser-card { border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; margin: 12px 0; }
.browser-card .section-label { margin-top: 0; }
.browser-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; }
.browser-pill { font-size: 10px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); }
.browser-pill.on { color: var(--accent); border-color: var(--accent-soft); }
.browser-shared { list-style: none; margin: 8px 0; padding: 0; }
.browser-shared li { display: flex; align-items: flex-start; gap: 10px; padding: 8px 0; border-top: 1px solid var(--border); font-size: 11px; line-height: 1.6; }
.browser-shared li:first-child { border-top: 0; }
.browser-row-actions { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
.browser-dot { width: 8px; height: 8px; margin-top: 6px; border-radius: 50%; background: var(--accent-soft); flex: none; }
.browser-dot.working { background: var(--accent); animation: browser-breathe 1.6s ease-in-out infinite; }
.browser-dot.turn { background: var(--danger); }
.browser-words { flex: 1; min-width: 0; }
.browser-words small { display: block; color: var(--muted); overflow-wrap: anywhere; }
.browser-setup { margin: 10px 0 2px; font-size: 11px; line-height: 1.6; }
.browser-setup summary { cursor: pointer; font-weight: 600; }
.browser-setup summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.browser-setup ol { margin: 6px 0; padding-left: 18px; }
.browser-code-card { border: 1px solid var(--accent-soft); border-radius: 10px; padding: 10px 12px; margin: 10px 0; font-size: 11px; line-height: 1.6; }
.browser-code-card strong { display: block; font-size: 12px; }
.browser-code { margin: 6px 0; font: 600 22px/1.2 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .12em; font-variant-numeric: tabular-nums; }
.browser-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.browser-setup code, .browser-files code { font-size: 10px; background: var(--code); padding: 1px 4px; border-radius: 4px; }
.browser-files { margin: 6px 0; padding-left: 18px; }
.browser-address { display: block; margin: 8px 0 4px; font-size: 11px; }
.browser-address input { display: block; width: 100%; }
.browser-tab, .browser-grant, .browser-confirm { border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; margin: 10px 0; font-size: 11px; line-height: 1.6; overflow-wrap: anywhere; }
.browser-tab small { display: block; color: var(--muted); }
.browser-text { margin: 4px 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.browser-confirm { border-color: var(--danger-line); }
.browser-confirm strong, .browser-grant strong { display: block; font-size: 12px; }
.browser-grant fieldset { border: 0; margin: 8px 0; padding: 0; }
.browser-grant legend { font-weight: 600; margin-bottom: 4px; }
.browser-grant fieldset label { display: block; margin: 3px 0; overflow-wrap: anywhere; }
.browser-grant label small { display: block; margin-left: 22px; color: var(--muted); }
.browser-offer { border: 1px solid var(--accent-soft); border-radius: 10px; padding: 8px 10px; margin: 8px 0; }
.browser-offer p { margin: 0 0 6px; }
@keyframes browser-breathe { 0%, 100% { opacity: 1; } 50% { opacity: .45; } }
@media (prefers-reduced-motion: reduce) { .browser-dot.working { animation: none; } }
</style>
