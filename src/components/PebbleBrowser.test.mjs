import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 16: Pebble's browser says plainly when it cannot start (E-B1), page titles stay literal, forgetting the
// profile is confirmed first, and only the browser routes plus the tab grant are reachable.
// Phase 22 (b22-browser): the panel also shows the person's own Chrome. Shared tabs are granted for their one
// shared site, Pause and Stop reach only the extension routes, and the setup steps are plain words. Deliberate
// changes to the Phase 16 assertions: the panel's region is now "Browser" (focus returns there), and the reachable
// routes add browser_extension_status, _pause, _resume and _stop.
// Phase 22 second pass (design study D22 §7): the panel shows the pairing code (only here, never in a page), a
// paired Chrome can be forgotten (browser_extension_unpair), shared rows say when a task holds a grant, and a Chrome
// grant carries an explicit screenshots choice (off by default). Grants in Pebble's own browser send exactly what
// they did in Phase 16.
// Phase 24 (t24-browser), deliberate changes to the assertions above, each marked with its V1 ID below:
// - V1-127: until it can start, Pebble's own browser is one line, not a card with a Start that cannot work (the E-B1
//   reason is no longer shown; a refresh, not "Check again", brings the card once it is available);
// - V1-043: "Connect Chrome…" adds the helper after one confirmation listing its two files, and the steps say
//   plainly that the extension is loaded in Developer mode (D6);
// - V1-007: with Screen control off the grant card offers to turn it on and grant in one step, and a grant made with
//   no task running is for the person's next message;
// - V1-124 and V1-059: End access sits on the granted tab's row; screenshots go to the model and are not kept.
// The reachable routes add browser_connect_status, browser_connect_install, screen_sessions and screen_session_revoke.
const require = createRequire(import.meta.url);
const calls = [], failures = new Map(), gates = new Map();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const hostile = '<img src=x onerror=alert(1)>';
const scripted = '<script>alert("x")</script>';
const UNAVAILABLE = "Pebble's browser is not enabled yet: it waits for the E-B1 check that a Chromium-family browser runs over --remote-debugging-pipe with a fresh private profile and no TCP port.";
const EXTENSION_ID = 'ilmkbniehiecgoodnbkkcjgaelebehbj';
const FOLDER = '/Applications/Pebble.app/Contents/Resources/resources/browser-extension';
const browser = { available: false, running: false, profile_exists: false, sessions: 0, screen: false, live: false };
const extension = { connected: false, present: false, paired: false, pending: null, lockedFor: null, host: false, paused: false, shares: [] };
const PLAN = 'a'.repeat(64), LAUNCHER = '/Users/p/.pebble/data/chrome/pebble-native-host';
const MANIFEST = '/Users/p/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.pebble.browser.json';
const helper = { available: true, installed: false, problems: [] };
let tabs = [], opened = 0, grants = [], granted = 0;
const connectStatus = () => ({ format: 'pebble.connect-chrome/1', available: helper.available, browser: 'chrome', extension_id: EXTENSION_ID,
  files: helper.available ? [{ role: 'launcher', path: LAUNCHER, mode: '0700', sha256: 'b'.repeat(64), state: helper.installed ? 'current' : 'absent' },
    { role: 'manifest', path: MANIFEST, mode: '0644', sha256: 'c'.repeat(64), state: helper.installed ? 'current' : 'absent' }] : [],
  installed: helper.installed, ready: helper.available && !helper.problems.length, problems: helper.problems.map(message => ({ code: 'x', message })),
  plan_sha256: helper.available ? PLAN : null, never: [], authorization: 'none' });
const status = () => ({ running: browser.running, instance: browser.running ? 'i'.repeat(32) : null, product: browser.running ? 'HeadlessChrome/140 ' + hostile : null,
  profile: '/home/browser/profile', profile_exists: browser.profile_exists, available: browser.available,
  reason: browser.running ? null : browser.available ? "Pebble's browser is not running; start it in Pebble first." : UNAVAILABLE });
const chromeStatus = () => ({ extension: { connected: extension.connected, present: extension.present, id: EXTENSION_ID,
  version: extension.connected ? '0.1.0' : null, chrome: extension.connected ? '130.0.6723.70' : null, connected_at: null },
  pairing: { paired: extension.paired, paired_at: extension.paired ? 1_791_979_200 : null, pending: clone(extension.pending),
    locked_for: extension.lockedFor },
  bridge: { listening: true, error: null },
  host: { chrome: extension.host, 'chrome-beta': false, 'chrome-canary': false, chromium: false }, extension_folder: FOLDER,
  paused: extension.paused, shared_tabs: clone(extension.shares), last_stop: null, authorization: 'none' });
function answer(method, params) {
  if (method === 'browser_status') return status();
  if (method === 'browser_extension_status') return chromeStatus();
  if (method === 'browser_extension_pause') { extension.paused = true; return { paused: true, sessions: 1 }; }
  if (method === 'browser_extension_resume') { extension.paused = false; return { paused: false, sessions: 1 }; }
  if (method === 'browser_extension_stop') {
    const result = { stopped: true, shares_cleared: extension.shares.length, revoked_sessions: browser.sessions };
    extension.shares = []; browser.sessions = 0;
    return result;
  }
  if (method === 'browser_extension_unpair') {
    const result = { paired: false, revoked_sessions: browser.sessions };
    Object.assign(extension, { paired: false, connected: false, present: false, lockedFor: null, shares: [] }); browser.sessions = 0;
    return result;
  }
  if (method === 'browser_start') {
    if (!browser.available) throw new Error(UNAVAILABLE);
    browser.running = true; browser.profile_exists = true;
    return status();
  }
  if (method === 'browser_tabs') {
    if (!browser.running) throw new Error("That kind of window or tab is not supported here. Pebble's browser is not running; start it in Pebble first.");
    return clone(tabs);
  }
  if (method === 'browser_open_tab') {
    const origin = /^https:\/\/[a-z.]+/.exec(params.url)?.[0];
    if (!origin) throw new Error('Open an https address, or an http address on this Mac.');
    const tab_id = 'N' + ++opened;
    tabs.push({ tab_id, title: '', origin, url: params.url });
    return { tab_id, origin, url: params.url };
  }
  if (method === 'browser_stop') {
    const result = { revoked_sessions: browser.sessions, stopped: browser.running };
    browser.running = false; browser.sessions = 0; tabs = [];
    return result;
  }
  if (method === 'browser_profile_forget') {
    const result = { revoked_sessions: browser.sessions, stopped: browser.running, forgotten: browser.profile_exists, existed: browser.profile_exists };
    browser.running = false; browser.sessions = 0; browser.profile_exists = false; tabs = [];
    return result;
  }
  if (method === 'browser_connect_status') return connectStatus();
  if (method === 'browser_connect_install') {
    if (params.confirm !== PLAN) throw new Error('Confirm Connect Chrome in Pebble first. Nothing was written.');
    helper.installed = true; extension.host = true;
    return { ...connectStatus(), written: [LAUNCHER, MANIFEST] };
  }
  if (method === 'screen_sessions') return clone(grants.filter(item => item.chat === params.id));
  if (method === 'screen_session_revoke') {
    const item = grants.find(entry => entry.session_id === params.session_id && entry.state === 'active');
    if (item) item.state = 'revoked';
    return item ? 1 : 0;
  }
  if (method === 'screen_session_grant') {
    // The service checks the screen permission in the same step (V1-007): off, it offers to turn it on.
    if (!browser.screen && params.enable_screen !== true) {
      return { status: 'needs_screen_permission', granted: false, message: 'Screen control is off in Permissions, so Pebble cannot use this tab yet. Turn it on and grant the tab in one step; nothing else in Permissions changes.',
        enable: { permission: 'screen', enable_screen: true, available: !browser.live, label: 'Turn on Screen control and grant' }, authorization: 'none' };
    }
    const enabled = !browser.screen;
    browser.screen = true; browser.sessions++;
    const session_id = (++granted).toString(16).padStart(32, '0');
    grants.push({ chat: params.id, session_id, run_id: browser.live ? '9'.repeat(32) : null, pending: !browser.live, kind: 'browser', target: params.target,
      actions: params.actions, state: 'active', reason: null, expires: 1_900_000_000 });
    return { session_id, kind: 'browser', target: params.target, actions: params.actions, state: 'active', pending: !browser.live, screen_enabled: enabled };
  }
  throw new Error('Unexpected RPC ' + method);
}
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: clone(params) });
    const gate = gates.get(method), failure = failures.get(method);
    gates.delete(method); failures.delete(method);
    const result = failure ? undefined : answer(method, params);
    if (gate) await gate;
    if (failure) throw new Error(failure);
    return result;
  },
};
function hold(method) { let release; gates.set(method, new Promise(resolve => { release = resolve; })); return () => release(); }

const source = readFileSync(new URL('./PebbleBrowser.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'PebbleBrowser.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'browser' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'PebbleBrowser.vue', id: 'browser', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(source), 'page text renders as literal native content');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.ok(methods.length && methods.every(method => /^'[a-z_]+'$/.test(method)), 'every native method name is a fixed literal');
assert.deepEqual([...new Set(methods)].sort(), ["'browser_connect_install'", "'browser_connect_status'", "'browser_extension_pause'", "'browser_extension_resume'",
  "'browser_extension_status'", "'browser_extension_stop'", "'browser_extension_unpair'", "'browser_open_tab'", "'browser_profile_forget'", "'browser_start'",
  "'browser_status'", "'browser_stop'", "'browser_tabs'", "'screen_session_grant'", "'screen_session_revoke'", "'screen_sessions'"],
'only the browser routes, the Chrome routes, Connect Chrome, the tab grant and ending it are reachable (V1-043, V1-124)');
assert.deepEqual([...source.matchAll(/from '([^']+)'/g)].map(match => match[1]).sort(), ['../bridge', 'vue']);
assert.ok(/const region = shallowRef<HTMLElement \| null>\(null\), forgetCard = shallowRef<HTMLElement \| null>\(null\), grantCard = shallowRef<HTMLElement/.test(source), 'template element refs are shallow');
assert.ok(/prefers-reduced-motion: reduce/.test(source), 'the working dot stops under reduced motion');
for (const file of ['../Pet.vue', './NativeArtifactRenderer.vue', './ArtifactPanel.vue', './ModsSettings.vue', './Markdown.vue'])
  assert.ok(!/ScreenSessions|PebbleBrowser|ConnectorEffectReview|ConnectorRouteSettings/.test(readFileSync(new URL(file, import.meta.url), 'utf8')), file + ' renders no browser panel');

// The host runs real Vue hooks, v-model directives and buttons, without an HTML parser.
let focused = null;
function node(type, text = '') {
  return { type, text, children: [], props: {}, parent: null, value: '', addEventListener() {}, removeEventListener() {}, getRootNode() { return globalThis.document; }, focus() { focused = this; } };
}
const renderer = createRenderer({
  createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
  insert(child, parent, anchor) {
    if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1);
    child.parent = parent;
    const index = anchor ? parent.children.indexOf(anchor) : -1;
    parent.children.splice(index < 0 ? parent.children.length : index, 0, child);
  },
  remove(child) { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null; },
  setText(child, text) { child.text = text; },
  setElementText(child, text) { child.text = text; child.children = []; },
  patchProp(child, key, _previous, value) { child.props[key] = value; },
  parentNode: child => child.parent,
  nextSibling: child => child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
});
const timers = [];
const copied = [];
const previous = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot, setTimeout: globalThis.setTimeout,
  clearTimeout: globalThis.clearTimeout, navigator: Object.getOwnPropertyDescriptor(globalThis, 'navigator') };
globalThis.Document = class Document { activeElement = null; };
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms, live: true }); return timers.length; };
globalThis.clearTimeout = id => { if (timers[id - 1]) timers[id - 1].live = false; };
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async text => { copied.push(text); } } } });
const textOf = element => (element.type === '#comment' ? '' : element.text) + element.children.map(textOf).join('');
const all = element => [element, ...element.children.flatMap(all)];
async function settle() { for (let index = 0; index < 10; index++) { await Promise.resolve(); await nextTick(); } }
const events = [];
const props = reactive({ chatId: 'chat-a', refreshKey: 0 });
const root = node('root');
const view = renderer.createApp({ setup: () => () => h(Panel, { ...props, onChanged: () => events.push('changed') }) });
view.config.errorHandler = error => { throw error; };
const count = method => calls.filter(call => call.method === method).length;
const sent = method => calls.filter(call => call.method === method).at(-1)?.params;
const region = label => all(root).find(element => element.props['aria-label'] === label);
const notice = () => textOf(all(root).find(element => element.props.role === 'status'));
const alert = () => all(root).find(element => element.props.role === 'alert');
const button = (label, within = root) => all(within).find(item => item.type === 'button' && textOf(item) === label);
const tabCard = origin => region('Tab on ' + origin);
const sharedRow = origin => region('Shared tab on ' + origin);
const grantCard = () => region('Grant this tab');
const confirmCard = () => region('Confirm forgetting the browser profile');
const inputFor = (element, text) => { const label = element && all(element).find(item => item.type === 'label' && textOf(item).startsWith(text)); return label && all(label).find(item => item.type === 'input'); };
async function click(target) {
  assert.ok(target, 'missing button');
  assert.ok(!target.props.disabled, textOf(target) + ' must be enabled');
  await target.props.onClick({});
  await settle();
}
async function change(target) { assert.ok(target, 'missing input'); await target.props.onChange({}); await settle(); }
async function type(target, value) { assert.ok(target, 'missing field'); target.props['onUpdate:modelValue'](value); await settle(); }
async function poll() { const due = timers.filter(timer => timer.live && timer.ms === 4000); due.forEach(timer => { timer.live = false; }); for (const timer of due) timer.fn(); await settle(); }

// Until E-B1 the browser refuses to start. V1-127 (deliberate change): the panel says so in one line, with no card
// and no Start button that cannot work, and without the check's internal name.
view.mount(root);
await settle();
assert.equal(count('browser_status'), 1);
assert.deepEqual(calls[0].params, {}, 'status takes no parameters');
assert.equal(count('browser_tabs'), 0, 'a stopped browser has no tabs to read');
let page = textOf(root);
assert.ok(page.includes("Pebble's own browser is not available yet.") && !page.includes(UNAVAILABLE) && !page.includes('E-B1'));
assert.equal(region("Pebble's browser"), undefined, 'no card for a browser that cannot start');
assert.equal(button("Start Pebble's browser"), undefined, 'no Start button that cannot work');
assert.equal(button('Forget browser profile…'), undefined, 'there is no profile to forget');

// Your Chrome, not set up yet: plain setup steps with the folder and the extension ID, open by default.
const chromeCard = () => region('Your Chrome');
let words = textOf(chromeCard());
assert.ok(words.includes('Not set up') && words.includes('Connect Chrome'));
assert.ok(words.includes('chrome://extensions') && words.includes('Load unpacked') && words.includes(FOLDER) && words.includes(EXTENSION_ID));
// D6: the steps say plainly that the extension is loaded in Developer mode.
assert.ok(words.includes("Pebble for Chrome isn't in the Chrome Web Store yet, so you add it yourself: in Chrome, open the Extensions page (chrome://extensions) and turn on Developer mode."));
assert.equal(all(chromeCard()).find(item => item.type === 'details').props.open, true, 'the steps are open while Chrome is not connected');
assert.equal(button('Pause', chromeCard()), undefined, 'nothing to pause before Chrome connects');
await click(button('Copy folder path', chromeCard()));
assert.deepEqual(copied, [FOLDER]);
assert.equal(notice(), 'Folder path copied.');

// V1-043: Connect Chrome adds the helper after one confirmation that names its two files; Cancel writes nothing.
const connectCard = () => region('Connect Chrome');
assert.ok(words.includes('Pebble adds a small helper so Chrome can reach it.'));
await click(button('Connect Chrome…', chromeCard()));
assert.ok(connectCard() && focused === connectCard(), 'focus moves to the confirmation');
assert.equal(count('browser_connect_install'), 0, 'asking only opens a confirmation');
words = textOf(connectCard());
assert.ok(words.includes('Pebble writes these two files on this Mac and nothing else.') && words.includes(LAUNCHER) && words.includes(MANIFEST));
await click(button('Cancel', connectCard()));
assert.ok(!connectCard() && focused === region('Browser') && count('browser_connect_install') === 0);
helper.problems = ['Chrome has not been opened on this Mac yet. Open it once, then try again.'];
props.refreshKey = 0.25;
await settle();
await click(button('Connect Chrome…', chromeCard()));
assert.ok(textOf(connectCard()).includes('Chrome has not been opened on this Mac yet.'));
assert.equal(button('Add the helper', connectCard()).props.disabled, true, 'nothing can be added while something stands in the way');
await click(button('Cancel', connectCard()));
helper.problems = [];
props.refreshKey = 0.5;
await settle();
await click(button('Connect Chrome…', chromeCard()));
await click(button('Add the helper', connectCard()));
assert.deepEqual(sent('browser_connect_install'), { confirm: PLAN }, 'the one confirmation sends the digest of the plan it showed');
assert.equal(notice(), "Pebble's helper for Chrome is added. Next, add Pebble for Chrome in Chrome (the steps below), then pair.");
assert.ok(!connectCard() && textOf(chromeCard()).includes("Pebble's helper for Chrome is added on this Mac."));
assert.equal(button('Connect Chrome…', chromeCard()), undefined);
extension.host = false;

// Chrome reaches Pebble but is not paired: the panel says where to start, then shows the code only here.
extension.host = true; extension.present = true;
await poll();
words = textOf(chromeCard());
assert.ok(words.includes('Not paired') && words.includes('Chrome is asking to connect. In Chrome, click the Pebble button and choose "Pair with Pebble"; the code appears here.'));
assert.ok(words.includes('Pin Pebble to Chrome\'s toolbar, click it and choose "Pair with Pebble". Type the code this panel shows.'));
assert.equal(button('Forget pairing', chromeCard()), undefined, 'nothing to forget before pairing');
// Too many wrong codes lock pairing; the person can lift the lock here (the unpair route clears it).
extension.lockedFor = 1800;
await poll();
words = textOf(chromeCard());
assert.ok(words.includes('Pairing paused') && words.includes('Pairing is paused. Someone typed too many wrong codes, so Pebble shows no new code for about 30 minutes. If that was you, allow pairing now.'));
await click(button('Allow pairing now', chromeCard()));
assert.deepEqual(sent('browser_extension_unpair'), {});
assert.equal(notice(), 'Pairing is allowed again. In Chrome, choose "Pair with Pebble" for a new code.');
assert.ok(!textOf(chromeCard()).includes('Pairing is paused'));
extension.present = true;
extension.pending = { code: '482913', expires_in: 119, attempts_left: 5 };
await poll();
const codeCard = region('Pairing code');
assert.ok(codeCard && textOf(chromeCard()).includes('Pairing'));
assert.equal(codeCard.parent.props['aria-live'], 'polite', 'the code appears inside a polite live region');
const digits = all(codeCard).filter(item => item.type === 'span');
assert.deepEqual(digits.map(item => [textOf(item), item.props['aria-hidden'] ?? null, item.props.class]),
  [['482 913', 'true', undefined], ['4 8 2 9 1 3', null, 'browser-sr']], 'grouped for the eye, digit by digit for a screen reader');
assert.ok(textOf(codeCard).includes('Type this code in the Pebble for Chrome popup, never into a web page. It works once, expires in about 2 minutes, and has 5 tries left.'));
extension.pending = { code: '12345', expires_in: 60, attempts_left: 1 };
await poll();
assert.equal(region('Pairing code'), undefined, 'a malformed code is never shown');
extension.pending = null;

// Chrome pairs and the person shares tabs there; the panel picks them up on its own (it polls while open).
extension.connected = true; extension.paired = true;
extension.shares = [
  { tab_id: '41', origin: 'https://mail.example', title: scripted + ' ' + hostile, url: 'https://mail.example/inbox', scope: 'tab', state: 'ready', acting: true },
  { tab_id: '45', origin: 'https://news.example', title: 'News', url: 'https://news.example/', scope: 'tab', state: 'ready', acting: false, granted: true },
  { tab_id: '42', origin: 'https://bank.example', title: '', url: null, scope: 'site', state: 'handback', acting: false },
  { tab_id: '43', origin: 'https://docs.example', title: '', url: null, scope: 'site', state: 'away', acting: false },
  { tab_id: 'bad id!', origin: 'https://evil.example', title: 'Malformed', scope: 'tab', state: 'ready' },
  { tab_id: '44', origin: 'https://evil.example', title: 'Odd', scope: 'forever', state: 'ready' },
];
const statusReads = count('browser_extension_status');
await poll();
assert.equal(count('browser_extension_status'), statusReads + 1);
words = textOf(chromeCard());
assert.ok(words.includes('Connected') && words.includes('Chrome 130.0.6723.70 · Pebble for Chrome 0.1.0'));
assert.ok(!words.includes('Chrome is asking to connect') && !region('Pairing code'), 'paired: no code, no prompt');
assert.ok(textOf(sharedRow('https://news.example')).includes('https://news.example · Just this visit · A task can use it'));
assert.equal(all(chromeCard()).find(item => item.type === 'details').props.open, false, 'the steps fold away once connected');
assert.ok(!words.includes('Malformed') && !words.includes('Odd'), 'malformed shares are ignored');
const mail = sharedRow('https://mail.example');
assert.ok(textOf(mail).includes('Page title, written by the page' + scripted + ' ' + hostile), 'a hostile title stays plain text');
assert.ok(textOf(mail).includes('https://mail.example · Just this visit · Pebble is working here'));
assert.ok(!all(root).some(element => ['img', 'script'].includes(element.type)), 'no hostile element is created');
assert.ok(textOf(sharedRow('https://bank.example')).includes('Kept for this site · Your turn: a sign-in or payment page'));
assert.ok(textOf(sharedRow('https://docs.example')).includes('Paused: the tab is on another site'));
assert.equal(button('Let Pebble use this tab…', sharedRow('https://bank.example')), undefined, "a hand-back page can't be granted");
assert.equal(button('Let Pebble use this tab…', sharedRow('https://docs.example')), undefined, 'a tab away from its site cannot be granted');

// Granting a Chrome tab: only its one shared site, no other site can be added.
await click(button('Let Pebble use this tab…', mail));
assert.ok(grantCard() && focused === grantCard(), 'focus moves to the grant');
words = textOf(grantCard());
assert.ok(words.includes('Let Pebble use this Chrome tab') && words.includes('Pebble can use this tab only on https://mail.example, the site you shared.'));
assert.equal(inputFor(grantCard(), 'Add another site'), undefined, 'a Chrome tab is granted for its shared site only');
assert.equal(inputFor(grantCard(), 'https://mail.example').props.checked, true);
const shotBox = () => inputFor(grantCard(), 'Let Pebble see screenshots of this tab');
assert.equal(shotBox().props.checked, false, 'screenshots are off until the person ticks them');
assert.ok(textOf(grantCard()).includes('A screenshot shows everything visible on the page, including anything private'));
assert.ok(textOf(grantCard()).includes('Screenshots go to the model and are not kept.'), 'V1-059: said next to the checkbox');
assert.ok(textOf(grantCard()).includes('With a task running here, it can use the tab now; otherwise your next message in this conversation can.'));
await change(inputFor(grantCard(), 'Press buttons'));
await click(button('Grant this tab', grantCard()));
assert.deepEqual(sent('screen_session_grant'), { id: 'chat-a', target: { kind: 'browser', profile: 'chrome', tab_id: '41', origins: ['https://mail.example'] },
  actions: ['focus', 'scroll', 'expand', 'collapse', 'select', 'toggle', 'type', 'press', 'enter'], ttl: 900, screenshots: false });
// V1-007 (deliberate change): Screen control is off, so the service offers to turn it on; nothing is granted yet.
const offerCard = () => region('Turn on Screen control');
assert.ok(grantCard() && offerCard() && textOf(offerCard()).includes('Screen control is off in Permissions, so Pebble cannot use this tab yet.'));
assert.equal(button('Grant this tab', grantCard()), undefined, 'the one remaining step is the offer');
assert.equal(notice(), '');
await click(button('Turn on Screen control and grant', offerCard()));
assert.deepEqual(sent('screen_session_grant'), { id: 'chat-a', target: { kind: 'browser', profile: 'chrome', tab_id: '41', origins: ['https://mail.example'] },
  actions: ['focus', 'scroll', 'expand', 'collapse', 'select', 'toggle', 'type', 'press', 'enter'], ttl: 900, screenshots: false, enable_screen: true });
assert.equal(notice(), 'Pebble can use this Chrome tab on https://mail.example from your next message in this conversation, for 15 minutes. Screen control is now on in Permissions. End access on its row here at any time.');
assert.ok(!grantCard() && events.at(-1) === 'changed' && focused === region('Browser'));
assert.ok(button('End access', sharedRow('https://mail.example')), 'V1-124: End access is on the granted row');
assert.equal(button('End access', sharedRow('https://news.example')), undefined, "another conversation's grant is not ended from here");
await click(button('Let Pebble use this tab…', sharedRow('https://mail.example')));
assert.equal(shotBox().props.checked, false, 'a new grant starts with screenshots off again');
assert.equal(offerCard(), undefined, 'a new grant starts without the offer');
await change(shotBox());
assert.equal(shotBox().props.checked, true);
await click(button('Grant this tab', grantCard()));
assert.deepEqual(sent('screen_session_grant'), { id: 'chat-a', target: { kind: 'browser', profile: 'chrome', tab_id: '41', origins: ['https://mail.example'] },
  actions: ['focus', 'scroll', 'expand', 'collapse', 'select', 'toggle', 'type'], ttl: 900, screenshots: true });
assert.equal(notice(), 'Pebble can use this Chrome tab on https://mail.example from your next message in this conversation, for 15 minutes. It may also see screenshots of the tab; they go to the model and are not kept. End access on its row here at any time.');
// End access from the row ends every live grant this conversation holds on the tab.
const ended = events.length;
await click(button('End access', sharedRow('https://mail.example')));
assert.deepEqual(calls.filter(call => call.method === 'screen_session_revoke').map(call => call.params.session_id), [grants[0].session_id, grants[1].session_id]);
assert.equal(notice(), 'Access ended. Pebble can no longer use this tab.');
assert.ok(events.length === ended + 1 && !button('End access', sharedRow('https://mail.example')));
// While a task runs, Screen control cannot be turned on: the offer says so and its button is disabled.
browser.screen = false; browser.live = true;
await click(button('Let Pebble use this tab…', sharedRow('https://mail.example')));
await click(button('Grant this tab', grantCard()));
assert.equal(button('Turn on Screen control and grant', offerCard()).props.disabled, true);
await click(button('Cancel', grantCard()));
browser.screen = true; browser.live = false;

// An open Chrome grant closes when the person stops sharing that tab in Chrome.
await click(button('Let Pebble use this tab…', sharedRow('https://mail.example')));
extension.shares = extension.shares.filter(item => item.tab_id !== '41');
await poll();
assert.ok(!grantCard() && !sharedRow('https://mail.example'), 'an unshared tab cannot be granted');

// Pause hands every Chrome session to the person; Resume gives back only those.
const before = events.length;
await click(button('Pause', chromeCard()));
assert.deepEqual(sent('browser_extension_pause'), {});
assert.equal(notice(), "Paused. Pebble doesn't read or act in your Chrome tabs until you resume.");
assert.ok(textOf(chromeCard()).includes("Paused. Pebble doesn't read or act in your Chrome tabs until you resume.") && events.length === before + 1);
await click(button('Resume', chromeCard()));
assert.deepEqual(sent('browser_extension_resume'), {});
assert.equal(notice(), 'Resumed. Pebble looks at each tab again before it does anything.');
assert.ok(button('Pause', chromeCard()));

// Stop is immediate (no confirmation), names what it let go of, and is never blocked by another busy action.
browser.sessions = 1;
await click(button('Stop Pebble in Chrome', chromeCard()));
assert.deepEqual(sent('browser_extension_stop'), {});
assert.equal(notice(), 'Stopped. Pebble let go of 5 shared tabs in Chrome. 1 tab session ended.');
assert.ok(textOf(chromeCard()).includes('No tab is shared.'));
assert.ok(source.includes(`:disabled="busy === 'chrome-stop'"`), 'Stop stays available while anything else is busy');

// Forgetting the pairing ends every Chrome session; Chrome must pair again.
assert.ok(textOf(chromeCard()).includes('Chrome is paired with Pebble. Forgetting the pairing ends every Chrome session; Chrome then pairs again with a new code.'));
browser.sessions = 2;
await click(button('Forget pairing', chromeCard()));
assert.deepEqual(sent('browser_extension_unpair'), {});
assert.equal(notice(), 'Pairing forgotten. Chrome pairs again with a new code before Pebble can use any tab there. 2 tab sessions ended.');
assert.ok(events.at(-1) === 'changed' && textOf(chromeCard()).includes('Not connected') && !button('Forget pairing', chromeCard()));

// Once available, a refused start is shown, then a start runs it and reads its tabs.
browser.available = true;
tabs = [
  { tab_id: 'T1', title: scripted + ' ' + hostile, origin: 'https://shop.example', url: 'https://shop.example/cart?token=[redacted]' },
  { tab_id: 'T2', title: '', origin: null, url: 'about:blank' },
  { tab_id: 'bad id!', title: 'Malformed', origin: 'https://bad.example', url: 'https://bad.example/' },
];
props.refreshKey = 0.75;  // V1-127: no card (and no Check again) while it cannot start; a refresh brings it
await settle();
assert.ok(textOf(root).includes('Not running') && !textOf(root).includes("Pebble's own browser is not available yet."));
assert.ok(region("Pebble's browser") && button('Check again'));
failures.set('browser_start', "Pebble's browser did not answer over its pipe.");
await click(button("Start Pebble's browser"));
assert.equal(textOf(alert()), "Pebble's browser did not answer over its pipe.");
await click(button("Start Pebble's browser"));
assert.deepEqual(sent('browser_start'), {});
assert.equal(notice(), "Pebble's browser is running with its private profile.");
assert.equal(alert(), undefined);
page = textOf(root);
assert.ok(page.includes('Running · HeadlessChrome/140 ' + hostile), 'the product string stays plain text');
assert.equal(count('browser_tabs'), 1);
words = textOf(tabCard('https://shop.example'));
assert.ok(words.includes('Page title, written by the page' + scripted + ' ' + hostile), 'a hostile page title stays plain text');
assert.ok(words.includes('Site: https://shop.example') && words.includes('Address: https://shop.example/cart?token=[redacted]'));
assert.ok(!all(root).some(element => ['img', 'script'].includes(element.type)), 'no hostile element is created');
words = textOf(tabCard('no site'));
assert.ok(words.includes('No title') && words.includes('Site: not a website, so it cannot be granted'));
assert.equal(button('Let Pebble use this tab…', tabCard('no site')), undefined, 'a tab without a site cannot be granted');
assert.ok(!page.includes('Malformed'), 'a malformed tab is ignored');
assert.ok(page.includes("Its profile, with any sign-ins you made in Pebble's browser, is saved on this Mac."));

// Opening an address: the service checks it.
const address = () => inputFor(root, 'Address');
assert.equal(button('Open in a new tab').props.disabled, true, 'an empty address opens nothing');
await type(address(), 'http://evil.example/');
await click(button('Open in a new tab'));
assert.equal(textOf(alert()), 'Open an https address, or an http address on this Mac.');
await type(address(), '  https://docs.example/start  ');
await click(button('Open in a new tab'));
assert.deepEqual(sent('browser_open_tab'), { url: 'https://docs.example/start' });
assert.equal(notice(), 'Opened a new tab on https://docs.example.');
assert.equal(address().value, '', 'the address is cleared');
assert.ok(tabCard('https://docs.example'), 'the new tab is listed');

// Granting a tab: its own site is ticked, other tabs' sites are never suggested.
await click(button('Let Pebble use this tab…', tabCard('https://docs.example')));
assert.ok(grantCard() && focused === grantCard(), 'focus moves to the grant');
assert.ok(!textOf(grantCard()).includes('https://shop.example'), "another tab's site is not offered");
await click(button('Cancel', grantCard()));
assert.equal(focused, region('Browser'), 'cancelling returns focus to the panel');
await click(button('Let Pebble use this tab…', tabCard('https://shop.example')));
words = textOf(grantCard());
assert.ok(words.includes(scripted + ' ' + hostile) && words.includes("The title is page text; it can't give Pebble permission."));
assert.equal(inputFor(grantCard(), 'https://shop.example').props.checked, true, "the tab's own site is ticked");
assert.equal(inputFor(grantCard(), 'Let Pebble see screenshots'), undefined, "Pebble's own browser offers no screenshot choice");
await type(inputFor(grantCard(), 'Add another site'), 'https://pay.example/checkout');
await click(button('Add site', grantCard()));
assert.equal(textOf(alert()), 'Enter a site as https://example.com, with nothing after its name.');
await type(inputFor(grantCard(), 'Add another site'), ' HTTPS://Pay.Example/ ');
await click(button('Add site', grantCard()));
assert.equal(alert(), undefined);
assert.equal(inputFor(grantCard(), 'https://pay.example').props.checked, true, 'a typed site is added, normalized and ticked');
await change(inputFor(grantCard(), 'https://shop.example'));
await change(inputFor(grantCard(), 'https://pay.example'));
assert.equal(button('Grant this tab', grantCard()).props.disabled, true, 'a grant needs at least one site');
await change(inputFor(grantCard(), 'https://shop.example'));
await change(inputFor(grantCard(), 'https://pay.example'));
failures.set('screen_session_grant', 'Start a task in this conversation before granting it a window.');
const refusedBefore = events.length;
await click(button('Grant this tab', grantCard()));
assert.equal(textOf(alert()), 'Start a task in this conversation before granting it a window.');
assert.ok(grantCard() && events.length === refusedBefore, 'a refused grant keeps its form and emits nothing');
const grantsBefore = events.length;
browser.live = true;  // a task is running in this conversation, so the grant is for it
await change(inputFor(grantCard(), '30 minutes'));
await click(button('Grant this tab', grantCard()));
assert.deepEqual(sent('screen_session_grant'), { id: 'chat-a', target: { kind: 'browser', profile: 'pebble', tab_id: 'T1', origins: ['https://shop.example', 'https://pay.example'] },
  actions: ['focus', 'scroll', 'expand', 'collapse', 'select', 'toggle', 'type'], ttl: 1800 }, 'the grant names the tab, the ticked sites and the chosen actions');
assert.equal(notice(), 'Pebble can use this tab on https://shop.example, https://pay.example for the current task, for 30 minutes. End access on its row here at any time.');
assert.ok(!grantCard() && events.length === grantsBefore + 1 && focused === region('Browser'));
assert.ok(button('End access', tabCard('https://shop.example')), "End access is on the granted tab of Pebble's browser too");
browser.live = false;

// An open grant closes when its tab is gone.
await click(button('Let Pebble use this tab…', tabCard('https://docs.example')));
tabs = tabs.filter(item => item.tab_id !== 'N1');
props.refreshKey = 1;
await settle();
assert.ok(!grantCard() && !tabCard('https://docs.example'), 'a closed tab cannot be granted');

// Without a conversation there is nothing to grant to.
props.chatId = undefined;
await settle();
assert.equal(button('Let Pebble use this tab…', tabCard('https://shop.example')), undefined);
props.chatId = 'chat-a';
await settle();

// A status reply that arrives after a newer refresh is ignored.
const release = hold('browser_status');
props.refreshKey = 2;
await settle();
browser.profile_exists = true;
tabs.push({ tab_id: 'T9', title: 'Fresh tab', origin: 'https://fresh.example', url: 'https://fresh.example/' });
props.refreshKey = 3;
await settle();
assert.ok(tabCard('https://fresh.example'), 'the newest reply wins');
const tabReads = count('browser_tabs');
release();
await settle();
assert.equal(count('browser_tabs'), tabReads, 'a stale status reply reads nothing more');
assert.ok(tabCard('https://fresh.example'));

// Stopping ends the tab sessions and is reported.
await click(button("Stop Pebble's browser"));
assert.deepEqual(sent('browser_stop'), {});
assert.equal(notice(), "Pebble's browser stopped. 1 tab session ended.");
assert.ok(events.at(-1) === 'changed' && !tabCard('https://shop.example'), 'a stopped browser lists no tabs');

// Forgetting the profile is confirmed first and names what it signs out of.
const forgets = count('browser_profile_forget');
await click(button('Forget browser profile…'));
assert.equal(count('browser_profile_forget'), forgets, 'asking only opens a confirmation');
assert.ok(confirmCard() && focused === confirmCard(), 'focus moves to the confirmation');
assert.ok(textOf(confirmCard()).includes("This signs you out of every site in Pebble's browser and deletes its cookies, saved site data and history."));
await click(button('Cancel', confirmCard()));
assert.ok(!confirmCard() && focused === region('Browser') && count('browser_profile_forget') === forgets);
await click(button('Forget browser profile…'));
failures.set('browser_profile_forget', "That folder is not Pebble's browser profile, so it was left untouched.");
await click(button('Forget profile and sign out', confirmCard()));
assert.equal(textOf(alert()), "That folder is not Pebble's browser profile, so it was left untouched.");
assert.ok(confirmCard(), 'a refused forget keeps its confirmation');
const forgetEvents = events.length;
await click(button('Forget profile and sign out', confirmCard()));
assert.deepEqual(sent('browser_profile_forget'), {});
assert.equal(notice(), "Browser profile forgotten. You're signed out of every site in Pebble's browser.");
assert.ok(!confirmCard() && events.length === forgetEvents + 1);
assert.ok(textOf(root).includes('No browser profile is saved.') && button('Forget browser profile…').props.disabled);

// A reply that settles after the panel closes emits nothing, and polling stops.
await click(button("Start Pebble's browser"));
const releaseStop = hold('browser_stop');
const seen = events.length;
const late = button("Stop Pebble's browser").props.onClick({});
await settle();
view.unmount();
releaseStop();
await late;
await settle();
assert.equal(events.length, seen, 'a reply after unmount emits nothing');
assert.ok(!timers.some(timer => timer.live), 'no poll is left running after the panel closes');
globalThis.setTimeout = previous.setTimeout;
globalThis.clearTimeout = previous.clearTimeout;
if (previous.navigator) Object.defineProperty(globalThis, 'navigator', previous.navigator);
Object.assign(globalThis, { document: previous.document, Document: previous.Document, ShadowRoot: previous.ShadowRoot });
console.log('pebble browser: ok');
