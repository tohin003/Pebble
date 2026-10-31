import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 16: the person grants, hands back ("Done") and ends screen sessions
// only from this panel; window titles and app names stay literal; only the
// five screen routes are reachable.
// Phase 24 (t24-browser), deliberate changes: a pending grant is listed as waiting for the next message (V1-007);
// with Screen control off the grant offers to turn it on in the same step (V1-007); and without pinned app identities
// the app-window grant is not offered at all (V1-127: no dead control).
const require = createRequire(import.meta.url);
const calls = [], failures = new Map(), gates = new Map();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const hostile = '<img src=x onerror=alert(1)>';
const scripted = '<script>alert("x")</script>';
const H1 = '1'.repeat(64), H2 = '2'.repeat(64);
const S1 = 'a'.repeat(32), S2 = 'b'.repeat(32), S3 = 'c'.repeat(32), S4 = 'd'.repeat(32), S5 = 'e'.repeat(32), GRANTED = 'f'.repeat(32);
const S6 = '6'.repeat(32);
let screenOff = false;
const LIVE = ['active', 'handoff', 'interrupted'];
const at = seconds => Date.now() / 1000 + seconds;
const app = (bundle, team = null) => ({ kind: 'app', bundle_id: bundle, team_id: team, window: { title_sha256: H1, ax_identifier: null } });
const tab = origins => ({ kind: 'browser', profile: 'pebble', tab_id: 'T1', origins });
const row = (session_id, kind, target, actions, state, reason, expires) => ({ session_id, run_id: '9'.repeat(32), kind, target, actions, state, reason, expires });
const sessions = {
  'chat-a': [
    row(S4, 'app', app('com.apple.Notes'), ['focus'], 'revoked', null, at(-300)),
    row(S5, 'app', app('com.apple.Notes'), ['focus'], 'expired', null, at(-200)),
    row(S1, 'browser', tab(['https://shop.example']), ['focus', 'scroll', 'type'], 'handoff', { reasons: ['login_form', 'secure_field'] }, at(590)),
    row(S2, 'app', app('com.apple.TextEdit'), ['focus', 'scroll', 'expand', 'collapse', 'select', 'toggle', 'type'], 'active', null, at(55)),
    row(S3, 'browser', tab([hostile]), ['press'], 'interrupted', null, at(300)),
    { session_id: 'not-a-session', kind: 'app', target: {}, actions: [], state: 'active', reason: null, expires: at(100) },
    { ...row(S6, 'browser', { kind: 'browser', profile: 'chrome', tab_id: '7', origins: ['https://docs.example'] }, ['focus', 'scroll'], 'active', null, at(400)),
      run_id: null, pending: true },
  ],
  'chat-b': [],
};
const windows = {
  'com.example.hostile': [
    { title: scripted + ' ' + hostile, title_sha256: H1, ax_identifier: null, focused: true },
    { title: 'Twin', title_sha256: H2, ax_identifier: 'twin-1', focused: false },
    { title: 'Twin', title_sha256: H2, ax_identifier: 'twin-2', focused: false },
    { title: 'Broken', title_sha256: 'not-a-hash', ax_identifier: null, focused: false },
  ],
};
const find = (chat, id) => sessions[chat]?.find(item => item.session_id === id) || Object.values(sessions).flat().find(item => item.session_id === id);
function answer(method, params) {
  if (method === 'screen_sessions') return clone(sessions[params.id] || []);
  if (method === 'screen_handoff_done') {
    const item = find(null, params.session_id);
    if (!item || !['handoff', 'interrupted'].includes(item.state)) throw new Error('This screen session has ended. Ask the person to grant the window or tab again.');
    item.state = 'active'; item.reason = null;
    return { session_id: item.session_id, state: 'active', fresh_observation_required: true };
  }
  if (method === 'screen_session_revoke') {
    const item = find(null, params.session_id);
    if (!item || !LIVE.includes(item.state)) return 0;
    item.state = 'revoked';
    return 1;
  }
  if (method === 'screen_windows') return clone(windows[params.target.bundle_id] || []);
  if (method === 'screen_session_grant') {
    if (screenOff && params.enable_screen !== true) {
      return { status: 'needs_screen_permission', granted: false, message: 'Screen control is off in Permissions, so Pebble cannot use this window yet. Turn it on and grant the window in one step; nothing else in Permissions changes.',
        enable: { permission: 'screen', enable_screen: true, available: true, label: 'Turn on Screen control and grant' }, authorization: 'none' };
    }
    const enabled = screenOff;
    screenOff = false;
    sessions[params.id].push(row(GRANTED, 'app', params.target, params.actions, 'active', null, at(params.ttl)));
    return { session_id: GRANTED, chat_id: params.id, kind: 'app', target: params.target, actions: params.actions, state: 'active', ttl_seconds: params.ttl,
      pending: false, screen_enabled: enabled };
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

const source = readFileSync(new URL('./ScreenSessions.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'ScreenSessions.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'screens' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'ScreenSessions.vue', id: 'screens', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(source), 'titles render as literal native content');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.ok(methods.length && methods.every(method => /^'[a-z_]+'$/.test(method)), 'every native method name is a fixed literal');
// D103 (Phase 42, 1.8.0): the Accessibility state and macOS's own request join the screen routes.
assert.deepEqual([...new Set(methods)].sort(), ["'app_control_request'", "'app_control_status'", "'screen_handoff_done'", "'screen_session_grant'", "'screen_session_revoke'", "'screen_sessions'", "'screen_windows'"], 'only the screen routes are reachable');
assert.deepEqual([...source.matchAll(/from '([^']+)'/g)].map(match => match[1]).sort(), ['../bridge', 'vue']);
assert.ok(/shallowRef<HTMLElement \| null>\(null\), grantCard = shallowRef<HTMLElement/.test(source), 'template element refs are shallow');
// Generated views, mods and the pet window never render a screen panel.
for (const file of ['../Pet.vue', './NativeArtifactRenderer.vue', './ArtifactPanel.vue', './ModsSettings.vue', './Markdown.vue'])
  assert.ok(!/ScreenSessions|PebbleBrowser|ConnectorEffectReview|ConnectorRouteSettings/.test(readFileSync(new URL(file, import.meta.url), 'utf8')), file + ' renders no native screen panel');

let focused = null;
function node(type, text = '') {
  return { type, text, children: [], props: {}, parent: null, addEventListener() {}, removeEventListener() {}, focus() { focused = this; } };
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
// Polling uses setTimeout; the fixture holds the callbacks and runs them on demand.
const timers = new Map(), realTimers = { setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
let timerId = 0;
globalThis.setTimeout = (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; };
globalThis.clearTimeout = id => { timers.delete(id); };
const textOf = element => (element.type === '#comment' ? '' : element.text) + element.children.map(textOf).join('');
const all = element => [element, ...element.children.flatMap(all)];
async function settle() { for (let index = 0; index < 10; index++) { await Promise.resolve(); await nextTick(); } }
const events = [];
const pinned = [
  { id: '/Applications/TextEdit.app', name: 'TextEdit', bundle_id: 'com.apple.TextEdit', team_id: null },
  { id: '/Applications/Hostile.app', name: hostile, bundle_id: 'com.example.hostile', team_id: 'ABCDE12345' },
  { id: '/Applications/Plain.app', name: 'No identity' },
  { id: '/Applications/Bad.app', name: 'Bad team', bundle_id: 'com.example.bad', team_id: 'lowercase' },
];
const props = reactive({ chatId: 'chat-a', apps: pinned, refreshKey: 0 });
const root = node('root');
const view = renderer.createApp({ setup: () => () => h(Panel, { ...props, onChanged: () => events.push('changed') }) });
view.config.errorHandler = error => { throw error; };
const count = method => calls.filter(call => call.method === method).length;
const sent = method => calls.filter(call => call.method === method).at(-1)?.params;
const region = label => all(root).find(element => element.props['aria-label'] === label);
const status = () => textOf(all(root).find(element => element.props.role === 'status'));
const alert = () => all(root).find(element => element.props.role === 'alert');
const sessionCard = text => all(root).find(element => element.type === 'article' && element.props['aria-label']?.includes(text));
const buttonIn = (element, label) => element && all(element).find(item => item.type === 'button' && textOf(item) === label);
const inputFor = (element, text) => { const label = element && all(element).find(item => item.type === 'label' && textOf(item).startsWith(text)); return label && all(label).find(item => item.type === 'input'); };
const grantCard = () => region('Grant an app window');
async function click(target) {
  assert.ok(target, 'missing button');
  assert.ok(!target.props.disabled, textOf(target) + ' must be enabled');
  await target.props.onClick({});
  await settle();
}
async function change(target) { assert.ok(target, 'missing input'); await target.props.onChange({}); await settle(); }
const pendingPoll = () => [...timers.values()].filter(timer => timer.ms === 4000).length;
async function poll() { const [id, timer] = [...timers].at(-1); timers.delete(id); timer.fn(); await settle(); }

view.mount(root);
await settle();
assert.equal(count('screen_sessions'), 1);
assert.deepEqual(calls[0].params, { id: 'chat-a' }, 'the list names only this conversation');
let page = textOf(root);
assert.ok(page.includes('At a sign-in, a code or a check, Pebble stops and it\'s your turn: you finish it yourself, and Pebble never sees what you type.'));
const turn = sessionCard("Your turn: Pebble's browser tab · https://shop.example");
assert.ok(turn, 'a handoff is announced as your turn');
let words = textOf(turn);
assert.ok(words.includes('Your turn · Can look around, fill in · Ends in about 10 minutes'));
assert.ok(words.includes('Your turn. Pebble stopped at a sign-in form, a password, code or card field. Finish it yourself in the tab. Pebble doesn\'t look at it while it\'s your turn and never sees what you type.'));
assert.ok(buttonIn(turn, 'Done') && buttonIn(turn, 'End access'));
words = textOf(sessionCard('App window · TextEdit (com.apple.TextEdit)'));
assert.ok(words.includes('Pebble can use it · Can look around, fill in · Ends in about 1 minute'), 'the app name comes from the pinned identity');
assert.ok(!buttonIn(sessionCard('App window · TextEdit'), 'Done'), 'an active session has nothing to hand back');
const paused = sessionCard("Pebble's browser tab · " + hostile);
assert.ok(paused && textOf(paused).includes('The last step has an uncertain outcome, so Pebble paused. Check the tab yourself; nothing is retried.'), 'a hostile origin stays plain text');
assert.ok(textOf(paused).includes('Paused: the last step has an uncertain outcome · Can press buttons'));
assert.ok(page.includes('Recently ended') && page.includes('App window · com.apple.Notes · Time ran out') && page.includes('App window · com.apple.Notes · Ended by you'));
assert.ok(page.indexOf('Time ran out') < page.indexOf('Ended by you'), 'the newest ended session is listed first');
assert.ok(!page.includes('not-a-session'), 'a malformed row is ignored');
assert.ok(!all(root).some(element => ['img', 'script'].includes(element.type)), 'no hostile element is created');
assert.equal(pendingPoll(), 1, 'live sessions are re-read');
// Phase 24 (V1-007): a grant made while no task could use it waits for the next message, and can be ended like any other.
const waiting = sessionCard('Your Chrome tab · https://docs.example');
assert.ok(waiting && textOf(waiting).includes('Waiting for your next message here · Can look around · Ends in about 7 minutes'));
assert.ok(buttonIn(waiting, 'End access') && !buttonIn(waiting, 'Done'));

// Polling re-reads the list, so a fresh handoff appears without a reload.
await poll();
assert.equal(count('screen_sessions'), 2);
assert.equal(pendingPoll(), 1, 'one poll is pending at a time');

// Done hands the window back; Pebble must look again before it acts.
await click(buttonIn(turn, 'Done'));
assert.deepEqual(sent('screen_handoff_done'), { session_id: S1 });
assert.equal(status(), 'Done. Pebble looks at the tab again before it does anything else.');
assert.ok(events.includes('changed') && count('screen_sessions') === 3);
assert.ok(!sessionCard('Your turn:') && sessionCard("Pebble's browser tab · https://shop.example"), 'the session is active again');

// A refused Done is surfaced and changes nothing.
const seen = events.length;
failures.set('screen_handoff_done', 'This screen session has ended. Ask the person to grant the window or tab again.');
await click(buttonIn(sessionCard("Pebble's browser tab · " + hostile), 'Done'));
assert.equal(textOf(alert()), 'This screen session has ended. Ask the person to grant the window or tab again.');
assert.equal(events.length, seen, 'a refusal emits nothing');

await click(buttonIn(sessionCard('App window · TextEdit'), 'End access'));
assert.deepEqual(sent('screen_session_revoke'), { session_id: S2 });
assert.equal(status(), 'Access ended. Pebble can no longer use this window.');
assert.equal(alert(), undefined, 'a later success clears the old refusal');
page = textOf(root);
assert.ok(page.includes('App window · TextEdit (com.apple.TextEdit) · Ended by you') && !sessionCard('App window · TextEdit'));
assert.equal(all(region('Recently ended')).filter(element => element.type === 'li').length, 3, 'at most three ended sessions are listed');

// Granting an app window: only pinned identities are offered; titles stay literal.
await click(all(root).find(item => item.type === 'button' && textOf(item) === 'Let Pebble use an app window…'));
assert.ok(grantCard() && focused === grantCard(), 'focus moves to the grant');
words = textOf(grantCard());
assert.ok(words.includes('TextEdit · com.apple.TextEdit') && words.includes(hostile + ' · com.example.hostile'));
assert.ok(!words.includes('No identity') && !words.includes('Bad team'), 'apps without a valid pinned identity are not offered');
assert.equal(buttonIn(grantCard(), 'Grant this window').props.disabled, true, 'nothing is granted before a window is chosen');
assert.equal(buttonIn(grantCard(), 'Show its windows'), undefined);

// Until E-N2 the service refuses to list windows; the refusal is shown as it is.
const blocked = 'Accessibility control is not available. Accessibility control is not enabled yet: it waits for the Accessibility grant and the E-N2 check that macOS attributes it to Pebble.';
await change(inputFor(grantCard(), 'TextEdit'));
failures.set('screen_windows', blocked);
await click(buttonIn(grantCard(), 'Show its windows'));
assert.deepEqual(sent('screen_windows'), { target: { kind: 'app', bundle_id: 'com.apple.TextEdit', team_id: null } });
assert.equal(textOf(alert()), blocked);
assert.equal(buttonIn(grantCard(), 'Grant this window').props.disabled, true);

await change(inputFor(grantCard(), hostile));
await click(buttonIn(grantCard(), 'Show its windows'));
assert.deepEqual(sent('screen_windows'), { target: { kind: 'app', bundle_id: 'com.example.hostile', team_id: 'ABCDE12345' } });
words = textOf(grantCard());
assert.ok(words.includes(scripted + ' ' + hostile + ' · in front'), 'a hostile window title stays plain text');
assert.ok(words.includes('Twin · another window has the same title, so Pebble cannot tell them apart'));
assert.ok(!words.includes('Broken'), 'a window without a title hash is not offered');
assert.ok(!all(root).some(element => ['img', 'script'].includes(element.type)));
const twin = all(grantCard()).filter(item => item.type === 'input' && item.props.name === 'screen-window')[1];
assert.equal(twin.props.disabled, true, 'a window that shares its title cannot be chosen');
await change(all(grantCard()).filter(item => item.type === 'input' && item.props.name === 'screen-window')[0]);
await change(inputFor(grantCard(), 'Press buttons'));
await change(inputFor(grantCard(), '5 minutes'));

// The service decides: a grant needs a task, and the refusal keeps the form.
failures.set('screen_session_grant', 'Start a task in this conversation before granting it a window.');
await click(buttonIn(grantCard(), 'Grant this window'));
assert.equal(textOf(alert()), 'Start a task in this conversation before granting it a window.');
assert.ok(grantCard(), 'a refused grant keeps its form');
// Phase 24 (V1-007): with Screen control off the service offers to turn it on; one confirmation does both.
screenOff = true;
const before = events.length;
await click(buttonIn(grantCard(), 'Grant this window'));
const offerCard = () => region('Turn on Screen control');
assert.ok(grantCard() && offerCard() && textOf(offerCard()).includes('Screen control is off in Permissions, so Pebble cannot use this window yet.'));
assert.equal(buttonIn(grantCard(), 'Grant this window'), undefined, 'the one remaining step is the offer');
assert.equal(events.length, before, 'an offer grants nothing');
await click(buttonIn(offerCard(), 'Turn on Screen control and grant'));
assert.deepEqual(sent('screen_session_grant'), { id: 'chat-a', target: { kind: 'app', bundle_id: 'com.example.hostile', team_id: 'ABCDE12345', window: { title_sha256: H1, ax_identifier: null } },
  actions: ['focus', 'scroll', 'expand', 'collapse', 'select', 'toggle', 'type', 'press', 'enter'], ttl: 300, enable_screen: true }, 'the grant names the window by hash and the chosen actions');
assert.equal(status(), 'Pebble can use this window for the current task, for 5 minutes. Screen control is now on in Permissions. End access here at any time.');
assert.ok(!grantCard() && focused === region('Windows Pebble can use'), 'the form closes and focus returns');
assert.equal(events.length, before + 1);
assert.ok(sessionCard('App window · ' + hostile + ' (com.example.hostile)'), 'the new session is listed with the pinned name');

// Removing every group leaves nothing to grant.
await click(all(root).find(item => item.type === 'button' && textOf(item) === 'Let Pebble use an app window…'));
await change(inputFor(grantCard(), 'Look around'));
await change(inputFor(grantCard(), 'Fill in'));
await change(inputFor(grantCard(), hostile));
await click(buttonIn(grantCard(), 'Show its windows'));
await change(all(grantCard()).filter(item => item.type === 'input' && item.props.name === 'screen-window')[0]);
assert.equal(buttonIn(grantCard(), 'Grant this window').props.disabled, true, 'a grant needs at least one action');

// A window list that arrives after the form closed is ignored.
const releaseWindows = hold('screen_windows');
const lateWindows = buttonIn(grantCard(), 'Show its windows').props.onClick({});
await settle();
await click(buttonIn(grantCard(), 'Cancel'));
releaseWindows();
await lateWindows;
await settle();
await click(all(root).find(item => item.type === 'button' && textOf(item) === 'Let Pebble use an app window…'));
assert.ok(!textOf(grantCard()).includes('Twin'), 'a stale window list is ignored');
await click(buttonIn(grantCard(), 'Cancel'));

// A list reply for the previous conversation is ignored.
const releaseList = hold('screen_sessions');
props.refreshKey = 1;
await settle();
props.chatId = 'chat-b';
await settle();
assert.deepEqual(sent('screen_sessions'), { id: 'chat-b' });
releaseList();
await settle();
page = textOf(root);
assert.ok(page.includes("Pebble isn't using any window in this conversation.") && !page.includes('com.example.hostile'), 'a stale list reply is ignored');
assert.equal(pendingPoll(), 0, 'nothing is re-read when no session is live');

// Without pinned identities no app window can be granted. V1-127 (deliberate change): the control is not offered at
// all, instead of opening a form that says no window can be granted.
props.apps = [{ id: '/Applications/Plain.app', name: 'Plain' }];
await settle();
assert.equal(all(root).find(item => item.type === 'button' && textOf(item) === 'Let Pebble use an app window…'), undefined);
assert.equal(grantCard(), undefined);
props.apps = [];
await settle();
assert.equal(all(root).find(item => item.type === 'button' && textOf(item) === 'Let Pebble use an app window…'), undefined, 'the shell passes none until E-N2');

// A Done that settles after the panel closes emits nothing, and polling stops.
props.chatId = 'chat-a';
await settle();
sessions['chat-a'].push(row('0'.repeat(32), 'app', app('com.apple.TextEdit'), ['focus'], 'handoff', { reasons: ['requested'] }, at(120)));
props.refreshKey = 2;
await settle();
const requested = sessionCard('Your turn: App window · com.apple.TextEdit');
assert.ok(textOf(requested).includes('Your turn. Pebble handed this window to you.'), 'a requested handoff says so');
const releaseDone = hold('screen_handoff_done');
const settled = events.length;
const late = buttonIn(requested, 'Done').props.onClick({});
await settle();
view.unmount();
releaseDone();
await late;
await settle();
assert.equal(events.length, settled, 'a reply after unmount emits nothing');
assert.equal(timers.size, 0, 'unmounting stops polling');
globalThis.setTimeout = realTimers.setTimeout;
globalThis.clearTimeout = realTimers.clearTimeout;
console.log('screen sessions: ok');
