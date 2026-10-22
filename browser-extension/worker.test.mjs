// Pebble for Chrome, Phase 22: the service worker against a fake Chrome. Nothing here starts a browser or a host.
// The test plays Pebble's service with its own HMAC code (node:crypto), so the sealing is checked independently of
// lib/crypto.mjs. The fake Chrome has no `scripting` and no `permissions` API: the worker must not need either.
import assert from 'node:assert/strict';
import { createHash, createHmac } from 'node:crypto';
import { test } from 'node:test';
import { BINDING, IDLE_MS, INDICATOR_WORLD, MAX_RETRIES, createWorker } from './lib/worker.mjs';
import { ISOLATED_FUNCTIONS } from './lib/policy.mjs';

const ID = 'ilmkbniehiecgoodnbkkcjgaelebehbj';
const POPUP = { id: ID, url: `chrome-extension://${ID}/popup.html` };
const KEY = Buffer.from(Array.from({ length: 32 }, (_, index) => index));
const PAIRING_ID = 'c'.repeat(32);
const LINK = 'bb'.repeat(32);
const WORLD = 77;
const hmac = (key, text) => createHmac('sha256', key).update(text, 'utf8').digest();

class Emitter {
  constructor() { this.listeners = []; }
  addListener(fn) { this.listeners.push(fn); }
  emit(...args) { return this.listeners.map(fn => fn(...args)); }
}

class Port {
  constructor(name) { this.name = name; this.sent = []; this.onMessage = new Emitter(); this.onDisconnect = new Emitter(); this.closed = false; }
  postMessage(message) { if (this.closed) throw new Error('closed'); this.sent.push(JSON.parse(JSON.stringify(message))); }
  disconnect() { this.closed = true; }
}

function fake({ paired = true } = {}) {
  const f = { tabs: new Map(), attached: new Set(), commands: [], own: [], ports: [], timers: [], session: {}, local: {},
    indicator: [], facts: new Map(), failIndicator: false, updates: [], groups: [], ungrouped: [], tabGroups: [], answers: {},
    otherDebugger: new Set(), loader: new Map(), clock: 1_000_000 };
  if (paired) f.local.pairing = { id: PAIRING_ID, key: KEY.toString('hex') };
  const evaluate = ({ expression, contextId }) => {
    if (contextId !== WORLD) throw new Error('Cannot find context with specified id');
    const command = JSON.parse(expression.slice(expression.lastIndexOf(')(') + 2, -1));
    if (command.action === 'facts') return { result: { value: { ok: true, facts: f.facts.get('current') || { password: 0, one_time_code: 0, payment: 0, captcha: 0 } } } };
    if (f.failIndicator) throw new Error('Execution context was destroyed.');
    f.indicator.push(command.state);
    return { result: { value: { ok: true, state: command.state } } };
  };
  const chrome = {
    runtime: {
      id: ID, lastError: null, onMessage: new Emitter(), onConnect: new Emitter(), onMessageExternal: new Emitter(),
      onConnectExternal: new Emitter(), getManifest: () => ({ version: '0.1.0' }), getURL: path => `chrome-extension://${ID}/${path}`,
      connectNative: name => { const port = new Port(name); f.ports.push(port); f.svc = null; return port; },
    },
    tabs: {
      onUpdated: new Emitter(), onRemoved: new Emitter(), onCreated: new Emitter(),
      get: async id => { if (!f.tabs.has(id)) throw new Error('No tab'); const { url, ...rest } = f.tabs.get(id); return { ...rest }; },  // no URL: no tabs permission
      update: async (id, props) => { f.updates.push([id, props]); return { id, windowId: 1 }; },
      group: async options => { f.groups.push(options); return 900; },
      ungroup: async id => { f.ungrouped.push(id); },
    },
    tabGroups: { update: async (id, props) => { f.tabGroups.push([id, props]); } },
    windows: { update: async () => ({}) },
    debugger: {
      onEvent: new Emitter(), onDetach: new Emitter(),
      getTargets: async () => [...f.tabs.values()].map(tab => ({ tabId: tab.id, url: tab.url, title: tab.title, type: 'page',
        attached: f.attached.has(tab.id) || f.otherDebugger.has(tab.id) })),
      attach: async ({ tabId }) => { f.attached.add(tabId); },
      detach: async ({ tabId }) => { f.attached.delete(tabId); },
      sendCommand: async ({ tabId }, method, params) => {
        if (method === 'Page.enable' || method === 'Runtime.enable' || method === 'Runtime.addBinding'
          || (method === 'Page.createIsolatedWorld' && params.worldName === INDICATOR_WORLD)
          || (method === 'Runtime.evaluate' && params.contextId === WORLD) || (method === 'Page.getFrameTree' && !f.backend)) {
          f.own.push([tabId, method, params]);
          if (method === 'Page.createIsolatedWorld') return { executionContextId: WORLD };
          if (method === 'Page.getFrameTree') return { frameTree: { frame: { id: 'MAIN', loaderId: f.loader.get(tabId) || 'L1', url: f.tabs.get(tabId)?.url } } };
          if (method === 'Runtime.evaluate') return evaluate(params);
          return {};
        }
        f.commands.push([tabId, method, params]);
        if (method in f.answers) return typeof f.answers[method] === 'function' ? f.answers[method](params) : f.answers[method];
        return {};
      },
    },
    storage: {
      session: { get: async key => ({ [key]: f.session[key] }), set: async value => { Object.assign(f.session, JSON.parse(JSON.stringify(value))); } },
      local: { get: async key => ({ [key]: f.local[key] }), set: async value => { Object.assign(f.local, JSON.parse(JSON.stringify(value))); },
        remove: async key => { delete f.local[key]; } },
    },
    action: { setBadgeText: async ({ tabId, text }) => { f.badge = { ...(f.badge || {}), [tabId]: text }; }, setBadgeBackgroundColor: async () => {},
      setTitle: async () => {}, setIcon: async () => {} },
  };
  f.chrome = chrome;
  let nonceCount = 0;
  f.worker = createWorker(chrome, {
    now: () => f.clock, randomId: (() => { let n = 0; return () => (++n).toString(16).padStart(32, '0'); })(),
    nonce: () => (++nonceCount % 16).toString(16).repeat(64),
    setTimer: (fn, ms) => { const timer = { fn, ms, live: true }; f.timers.push(timer); return timer; },
    clearTimer: timer => { if (timer) timer.live = false; },
  });
  f.port = () => f.ports.at(-1);
  // ---- the service's side of the link ----
  f.welcome = async (overrides = {}) => {
    const hello = f.port().sent[0];
    const session = hmac(KEY, `session|${hello.nonce}|${LINK}`);
    f.svc = { session, sendSeq: 0, read: 1, hello };
    f.port().onMessage.emit({ type: 'welcome', protocol: 'pebble-browser/1', instance: 'i', link_nonce: LINK, paused: false,
      paired: true, pairing_id: PAIRING_ID, proof: hmac(KEY, `svc|${hello.nonce}|${LINK}`).toString('hex'), ...overrides });
    await settle();
  };
  f.toWorker = async message => {
    f.svc.sendSeq += 1;
    const body = JSON.stringify(message);
    f.port().onMessage.emit({ type: 'sealed', seq: f.svc.sendSeq, body, mac: hmac(f.svc.session, `s2e|${f.svc.sendSeq}|${body}`).toString('hex') });
    await settle();
  };
  // Every sealed frame the worker sent since the hello, opened and checked by the service.
  f.fromWorker = () => f.port().sent.slice(f.svc.read).map((frame, index) => {
    assert.equal(frame.type, 'sealed');
    assert.equal(frame.seq, index + 1, 'sequence numbers start at 1 and never skip');
    assert.equal(frame.mac, hmac(f.svc.session, `e2s|${frame.seq}|${frame.body}`).toString('hex'), 'the worker sealed it with the session key');
    return JSON.parse(frame.body);
  });
  f.sent = type => f.fromWorker().filter(message => message.type === type);
  f.fire = ms => { for (const timer of f.timers.filter(t => t.live && t.ms === ms)) { timer.live = false; timer.fn(); } };
  f.ask = (message, sender = POPUP) => new Promise(resolve => {
    const answered = chrome.runtime.onMessage.listeners.map(fn => fn(message, sender, resolve));
    assert.ok(answered.every(value => value === true));
  });
  f.call = async message => {
    const before = f.fromWorker().length;
    await f.toWorker({ type: 'call', ...message });
    await settle();
    return f.fromWorker().slice(before).filter(m => (m.type === 'reply' || m.type === 'chunk') && m.id === message.id);
  };
  f.grant = async (tabId = 5, seconds = 30, session = 'd'.repeat(32)) => {
    await f.toWorker({ type: 'control', action: 'grant', tab_id: String(tabId), session_id: session, expires_in: seconds, actions: ['press'] });
  };
  return f;
}

async function settle() { for (let i = 0; i < 60; i++) await new Promise(resolve => setImmediate(resolve)); }

async function started({ tabs = [[5, 'https://mail.example/inbox', 'Inbox']], paired = true } = {}) {
  const f = fake({ paired });
  for (const [id, url, title] of tabs) f.tabs.set(id, { id, url, title, windowId: 1, groupId: -1 });
  await f.worker.start();
  return f;
}

async function shared(scope = 'tab', tabId = 5) {
  const f = await started();
  const reply = await f.ask({ type: 'popup:share', tabId, scope });
  assert.equal(reply.ok, true);
  await f.welcome();
  return f;
}

async function granted(scope = 'tab') {
  const f = await shared(scope);
  await f.grant();
  const reply = await f.call({ id: 100, op: 'attach', tab_id: '5' });
  assert.equal(reply[0].ok, true, JSON.stringify(reply[0]));
  return f;
}

test('nothing runs in the background until a tab is shared or the popup opens, and no one else can talk to it', async () => {
  const f = await started();
  assert.equal(f.ports.length, 0, 'no native host without a share');
  assert.equal(f.chrome.runtime.onMessageExternal.listeners.length, 0);
  assert.equal(f.chrome.runtime.onConnectExternal.listeners.length, 0);
  const popup = new Port('popup');
  popup.sender = POPUP;
  f.chrome.runtime.onConnect.emit(popup);
  assert.equal(f.ports.length, 1);
  const hello = f.port().sent[0];
  assert.deepEqual(Object.keys(hello).sort(), ['extension', 'nonce', 'pairing_id', 'protocol', 'type']);
  assert.match(hello.nonce, /^[0-9a-f]{64}$/);
  assert.equal(hello.pairing_id, PAIRING_ID);
  assert.ok(!JSON.stringify(hello).includes('token') && !JSON.stringify(hello).includes(KEY.toString('hex')), 'no token, no key');
  popup.onDisconnect.emit();
  await settle();
  assert.equal(f.port().closed, true, 'closing the popup with nothing shared closes the host');
  const stranger = new Port('popup');
  stranger.sender = { id: 'someotherextensionidxxxxxxxxxxxx', url: 'chrome-extension://other/popup.html' };
  f.chrome.runtime.onConnect.emit(stranger);
  assert.equal(f.ports.length, 1);
  const fromPage = await f.ask({ type: 'popup:share', tabId: 5, scope: 'tab' }, { id: ID, tab: { id: 5 }, frameId: 0, url: 'https://mail.example/' });
  assert.equal(fromPage, null, 'a page cannot share itself or reach the popup routes');
});

test('pairing: the person types the code Pebble shows; nothing else crosses before it holds', async () => {
  const f = await started({ paired: false });
  const popup = new Port('popup');
  popup.sender = POPUP;
  f.chrome.runtime.onConnect.emit(popup);
  const hello = f.port().sent[0];
  assert.equal(hello.pairing_id, null);
  f.port().onMessage.emit({ type: 'welcome', protocol: 'pebble-browser/1', instance: 'i', link_nonce: LINK, paired: false, paused: false });
  await settle();
  let view = await f.ask({ type: 'popup:state', tabId: 5 });
  assert.equal(view.connection, 'unpaired');
  assert.deepEqual(view.pairing, { paired: false, pending: null });
  f.port().onMessage.emit({ type: 'control', action: 'stop_all' });  // unsealed: dropped while unpaired
  f.port().onMessage.emit({ type: 'call', id: 1, op: 'attach', tab_id: '5' });
  await settle();
  assert.equal(f.port().sent.length, 1, 'nothing answered, nothing sent');
  assert.equal((await f.ask({ type: 'popup:pair_code', tabId: 5, code: '123456' })).ok, false, 'no code before Pebble asks');
  assert.equal((await f.ask({ type: 'popup:pair_start', tabId: 5 })).ok, true);
  assert.deepEqual(f.port().sent.at(-1), { type: 'pair_request' });
  f.port().onMessage.emit({ type: 'pair_challenge', expires_in: 120, attempts: 5 });
  await settle();
  view = await f.ask({ type: 'popup:state', tabId: 5 });
  assert.deepEqual(view.pairing.pending, { attemptsLeft: 5, expiresIn: 120 });
  assert.equal((await f.ask({ type: 'popup:pair_code', tabId: 5, code: '12a456' })).ok, false);
  assert.equal((await f.ask({ type: 'popup:pair_code', tabId: 5, code: '123456' })).ok, true);
  const codeKey = createHash('sha256').update('pebble-browser/1|pair|123456').digest();
  assert.deepEqual(f.port().sent.at(-1), { type: 'pair_proof', proof: hmac(codeKey, `ext|${hello.nonce}|${LINK}`).toString('hex') });
  f.port().onMessage.emit({ type: 'pair_result', ok: false, reason: 'wrong_code', attempts_left: 4 });
  await settle();
  assert.match((await f.ask({ type: 'popup:state', tabId: 5 })).message, /4 tries left/);
  await f.ask({ type: 'popup:pair_code', tabId: 5, code: '123456' });
  const mask = hmac(codeKey, `wrap|${hello.nonce}|${LINK}`);
  const wrapped = Buffer.from(KEY.map((byte, index) => byte ^ mask[index])).toString('hex');
  f.port().onMessage.emit({ type: 'pair_result', ok: true, pairing_id: PAIRING_ID, wrapped,
    confirm: hmac(KEY, `confirm|${hello.nonce}|${LINK}`).toString('hex') });
  await settle();
  assert.deepEqual(f.local.pairing, { id: PAIRING_ID, key: KEY.toString('hex') }, 'the key stays in chrome.storage.local');
  view = await f.ask({ type: 'popup:state', tabId: 5 });
  assert.equal(view.connection, 'connected');
  const plain = f.port().sent.findIndex(frame => frame.type === 'sealed');
  assert.deepEqual(f.port().sent.slice(0, plain).map(frame => frame.type), ['hello', 'pair_request', 'pair_proof', 'pair_proof']);
  f.svc = { session: hmac(KEY, `session|${hello.nonce}|${LINK}`), sendSeq: 0, read: plain };
  await f.ask({ type: 'popup:share', tabId: 5, scope: 'tab' });
  await settle();
  assert.deepEqual(f.sent('shares').map(message => message.shares.map(s => s.tab_id)), [[], ['5']], 'after pairing every message is sealed');
});

test('a locked pairing says so and accepts no code until Pebble asks again', async () => {
  const f = await started({ paired: false });
  await f.ask({ type: 'popup:share', tabId: 5, scope: 'tab' });
  f.port().onMessage.emit({ type: 'welcome', protocol: 'pebble-browser/1', instance: 'i', link_nonce: LINK, paired: false });
  await settle();
  await f.ask({ type: 'popup:pair_start', tabId: 5 });
  f.port().onMessage.emit({ type: 'pair_result', ok: false, reason: 'locked', attempts_left: 0, retry_in: 1800 });
  await settle();
  const view = await f.ask({ type: 'popup:state', tabId: 5 });
  assert.equal(view.message, "Too many wrong codes, so pairing is paused for about 30 min. Pebble's Browser panel can allow it again sooner.");
  assert.deepEqual([view.connection, view.pairing.pending], ['unpaired', null]);
  assert.equal((await f.ask({ type: 'popup:pair_code', tabId: 5, code: '123456' })).ok, false);
  f.port().onMessage.emit({ type: 'pair_result', ok: false, reason: 'wrong_code', attempts_left: 3 });
  await settle();
  assert.match((await f.ask({ type: 'popup:state', tabId: 5 })).message, /^Too many wrong codes/, 'a result for no code is dropped');
});

test('a pairing result with a wrong confirmation, or a welcome with a wrong proof, ends the link', async () => {
  let f = await started({ paired: false });
  await f.ask({ type: 'popup:share', tabId: 5, scope: 'tab' });
  f.port().onMessage.emit({ type: 'welcome', protocol: 'pebble-browser/1', instance: 'i', link_nonce: LINK, paired: false });
  await settle();
  await f.ask({ type: 'popup:pair_start', tabId: 5 });
  f.port().onMessage.emit({ type: 'pair_challenge', expires_in: 120, attempts: 5 });
  await settle();
  await f.ask({ type: 'popup:pair_code', tabId: 5, code: '111111' });
  f.port().onMessage.emit({ type: 'pair_result', ok: true, pairing_id: PAIRING_ID, wrapped: '00'.repeat(32), confirm: 'ff'.repeat(32) });
  await settle();
  assert.equal(f.port().closed, true);
  assert.equal(f.local.pairing, undefined, 'nothing stored');
  f = await started();
  await f.ask({ type: 'popup:share', tabId: 5, scope: 'tab' });
  await f.welcome({ proof: 'ab'.repeat(32) });
  assert.equal(f.port().closed, true, 'an impostor that cannot prove the key gets nothing');
  assert.equal((await f.ask({ type: 'popup:state', tabId: 5 })).connection, 'mismatch');
  assert.equal(f.timers.filter(t => t.live && t.ms >= 2000 && t.ms <= 60000 && t.ms !== IDLE_MS).length, 0, 'no retry loop');
});

for (const how of ['bad mac', 'replay', 'unsealed']) {
  test(`a ${how} from Pebble ends the link and is never acted on`, async () => {
    const f = await shared();
    await f.grant();
    const body = JSON.stringify({ type: 'control', action: 'stop_all' });
    const seq = how === 'replay' ? f.svc.sendSeq : f.svc.sendSeq + 1;
    const frame = how === 'unsealed' ? JSON.parse(body)
      : { type: 'sealed', seq, body, mac: how === 'bad mac' ? '0'.repeat(64) : hmac(f.svc.session, `s2e|${seq}|${body}`).toString('hex') };
    f.port().onMessage.emit(frame);
    await settle();
    assert.equal(f.port().closed, true);
    assert.equal((await f.ask({ type: 'popup:state', tabId: 5 })).shares.length, 1, 'the forged Stop did nothing');
  });
}

test('sharing needs the popup, joins the Pebble tab group, checks the address and attaches nothing', async () => {
  const f = await started();
  const reply = await f.ask({ type: 'popup:share', tabId: 5, scope: 'site' });
  assert.equal(reply.ok, true);
  assert.deepEqual(reply.view.current.share, { scope: 'site', state: 'ready', reasons: [], acting: false, granted: false });
  assert.deepEqual(f.groups, [{ tabIds: 5, createProperties: { windowId: 1 } }]);
  assert.deepEqual(f.tabGroups, [[900, { title: 'Pebble', color: 'green' }]]);
  assert.equal(f.attached.size, 0, 'sharing does not attach the debugger');
  assert.equal(f.badge[5], '•');
  await f.welcome();
  const shares = f.sent('shares').at(-1).shares;
  assert.deepEqual(shares.map(s => [s.tab_id, s.origin, s.scope, s.state, s.url]), [['5', 'https://mail.example', 'site', 'ready', 'https://mail.example/inbox']]);
  f.tabs.set(9, { id: 9, url: 'chrome://settings', title: 'Settings', windowId: 1, groupId: -1 });
  assert.equal((await f.ask({ type: 'popup:share', tabId: 9, scope: 'tab' })).error, 'unshareable');
  f.tabs.set(10, { id: 10, url: 'https://docs.example/', title: 'Docs', windowId: 1, groupId: 42 });
  await f.ask({ type: 'popup:share', tabId: 10, scope: 'tab' });
  assert.equal(f.groups.length, 1, "a tab in the person's own group stays there");
});

test('7.4.1: page commands need a live grant, and cookie or storage methods never reach the debugger', async () => {
  const f = await shared();
  let reply = await f.call({ id: 1, op: 'attach', tab_id: '5' });
  assert.equal(reply[0].error.code, 'not_granted');
  reply = await f.call({ id: 2, op: 'cdp', tab_id: '5', method: 'DOMSnapshot.captureSnapshot', params: { computedStyles: [], includeDOMRects: true } });
  assert.equal(reply[0].error.code, 'not_granted');
  for (const [id, method] of [[3, 'Network.getAllCookies'], [4, 'Storage.getCookies'], [5, 'Target.getTargets'], [6, 'Page.navigate']]) {
    reply = await f.call({ id, op: 'cdp', tab_id: '5', method, params: {} });
    assert.equal(reply[0].error.code, 'unsupported', method);
  }
  reply = await f.call({ id: 7, op: 'tab_info', tab_id: '5' });
  assert.equal(reply[0].error.code, 'not_granted');
  assert.equal(f.attached.size, 0);
  assert.deepEqual(f.commands, [], 'nothing reached chrome.debugger');
  await f.grant();
  reply = await f.call({ id: 8, op: 'cdp', tab_id: '5', method: 'Network.getAllCookies', params: {} });
  assert.equal(reply[0].error.code, 'unsupported', 'a grant never widens the allowlist');
});

test('a grant attaches through Pebble\'s own isolated world, shows the indicator and reads the page facts', async () => {
  const f = await granted();
  assert.ok(f.attached.has(5));
  assert.equal(f.badge[5], 'ON');
  const own = f.own.map(([, method, params]) => [method, params.worldName || params.name || params.expression?.match(/"action":"(\w+)"/)?.[1] || '']);
  assert.deepEqual(own.slice(0, 6), [['Page.enable', ''], ['Runtime.enable', ''], ['Runtime.addBinding', BINDING],
    ['Page.getFrameTree', ''], ['Page.createIsolatedWorld', INDICATOR_WORLD], ['Runtime.evaluate', 'facts']]);
  assert.equal(f.own[2][2].executionContextName, INDICATOR_WORLD, 'the binding exists only in Pebble\'s world');
  // Phase 24 (V1-153): the indicator's world is made with Chrome's real parameter, so the explicit false reaches it.
  assert.deepEqual(f.own[4][2], { frameId: 'MAIN', worldName: INDICATOR_WORLD, grantUniversalAccess: false });
  assert.ok(f.indicator.includes('acting'), 'the border shows while Pebble works');
  assert.ok(f.own.every(([, method, params]) => method !== 'Runtime.evaluate' || params.contextId === WORLD), 'never the page world');
});

test('7.4.2: an expired grant detaches at once and later commands are refused', async () => {
  const f = await granted();
  f.clock += 29_999;
  assert.equal(f.worker.grantLive(5), true);
  f.clock += 1;
  f.fire(30_000);  // the grant's own timer, at its expiry
  await settle();
  assert.equal(f.attached.size, 0, 'detached at expiry');
  assert.deepEqual(f.sent('detached').at(-1), { type: 'detached', tab_id: '5', reason: 'expired' });
  assert.equal(f.indicator.at(-1), 'off', 'the indicator goes away first');
  const reply = await f.call({ id: 2, op: 'cdp', tab_id: '5', method: 'Page.getFrameTree', params: {} });
  assert.equal(reply[0].error.code, 'not_granted');
  assert.equal(f.badge[5], '•');
  const again = await granted();
  await again.toWorker({ type: 'control', action: 'ungrant', tab_id: '5', session_id: 'd'.repeat(32) });
  assert.equal(again.attached.size, 0, "Pebble's End access lets go too");
});

test('only reviewed commands reach the debugger, and their answers leave compacted', async () => {
  const f = await granted();
  f.answers['DOMSnapshot.captureSnapshot'] = { strings: ['A', 'value', 'hunter2'], documents: [] };
  f.answers['Page.createIsolatedWorld'] = { executionContextId: 4 };
  f.answers['DOM.resolveNode'] = { object: { objectId: 'o1' } };
  let reply = await f.call({ id: 3, op: 'cdp', tab_id: '5', method: 'DOMSnapshot.captureSnapshot', params: { computedStyles: [], includeDOMRects: true } });
  assert.ok(reply[0].ok && !JSON.stringify(reply).includes('hunter2'));
  reply = await f.call({ id: 4, op: 'cdp', tab_id: '5', method: 'Runtime.evaluate', params: { expression: 'document.cookie', returnByValue: true, contextId: 1 } });
  assert.equal(reply[0].error.code, 'invalid');
  reply = await f.call({ id: 5, op: 'cdp', tab_id: '5', method: 'Runtime.evaluate', params: { expression: 'document.visibilityState', returnByValue: true, contextId: WORLD } });
  assert.equal(reply[0].error.code, 'invalid', "Pebble's backend cannot borrow the indicator's world");
  // Phase 24 (V1-153): deliberate change to Chrome's real parameter name; the misspelt one is refused by the fence.
  reply = await f.call({ id: 60, op: 'cdp', tab_id: '5', method: 'Page.createIsolatedWorld', params: { frameId: 'F', worldName: 'pebble-screen', grantUniveralAccess: false } });
  assert.equal(reply[0].error.code, 'invalid', 'the misspelt parameter never reaches the debugger');
  await f.call({ id: 6, op: 'cdp', tab_id: '5', method: 'Page.createIsolatedWorld', params: { frameId: 'F', worldName: 'pebble-screen', grantUniversalAccess: false } });
  await f.call({ id: 7, op: 'cdp', tab_id: '5', method: 'DOM.resolveNode', params: { backendNodeId: 3, objectGroup: 'pebble-act', executionContextId: 4 } });
  reply = await f.call({ id: 8, op: 'cdp', tab_id: '5', method: 'Runtime.callFunctionOn', params: { functionDeclaration: ISOLATED_FUNCTIONS[1], objectId: 'o1', arguments: [], returnByValue: true } });
  assert.equal(reply[0].ok, true);
  assert.deepEqual(f.commands.map(c => c[1]), ['DOMSnapshot.captureSnapshot', 'Page.createIsolatedWorld', 'DOM.resolveNode', 'Runtime.callFunctionOn']);
  f.answers['Accessibility.getFullAXTree'] = { nodes: Array.from({ length: 6000 }, (_, i) => ({ nodeId: String(i), role: { value: 'StaticText' },
    name: { value: 'Line "' + i + '" ' + ' '.repeat(200) }, backendDOMNodeId: i + 1, childIds: [] })) };
  const frames = await f.call({ id: 9, op: 'cdp', tab_id: '5', method: 'Accessibility.getFullAXTree', params: { frameId: 'F' } });
  assert.ok(frames.length > 1 && frames.every(frame => frame.type === 'chunk'));
  for (const frame of f.port().sent.slice(-frames.length)) assert.ok(new TextEncoder().encode(JSON.stringify(frame)).length < 1 << 20, 'each sealed frame fits');
  const whole = JSON.parse(Buffer.concat(frames.map(frame => Buffer.from(frame.data, 'base64'))).toString('utf8'));
  assert.equal(whole.result.nodes.length, 6000);
});

test('no effect without a visible indicator, and another debugger on the tab is never shared', async () => {
  const f = await granted();
  f.failIndicator = true;
  f.loader.set(5, 'L2');  // a new document: the indicator must show again
  f.chrome.debugger.onEvent.emit({ tabId: 5 }, 'Page.loadEventFired', {});
  await settle();
  for (const timer of f.timers) timer.live = false;
  let reply = await f.call({ id: 2, op: 'cdp', tab_id: '5', method: 'Input.insertText', params: { text: 'hi' } });
  assert.equal(reply[0].error.code, 'refused');
  assert.ok(!f.commands.some(c => c[1] === 'Input.insertText'));
  const g = await shared();
  await g.grant();
  g.otherDebugger.add(5);
  reply = await g.call({ id: 3, op: 'attach', tab_id: '5' });
  assert.equal(reply[0].error.code, 'refused');
  assert.match(reply[0].error.message, /Another tool is already debugging/);
});

test('events leave sanitized; the page binding only ever narrows; a page popup counts as a disturbance', async () => {
  const f = await granted();
  f.chrome.debugger.onEvent.emit({ tabId: 5 }, 'Fetch.requestPaused', { requestId: 'r1', frameId: 'F', resourceType: 'Document',
    request: { url: 'https://mail.example/send?token=t', method: 'POST', postData: 'pw=hunter2' } });
  f.chrome.debugger.onEvent.emit({ tabId: 5 }, 'Network.responseReceived', { response: {} });
  f.chrome.debugger.onEvent.emit({ tabId: 5 }, 'Runtime.consoleAPICalled', { args: [{ value: 'secret' }] });
  f.chrome.tabs.onCreated.emit({ id: 11, openerTabId: 5 });
  await settle();
  const events = f.sent('event');
  assert.deepEqual(events.map(e => [e.tab_id, e.method]), [['5', 'Fetch.requestPaused'], ['5', 'Pebble.popupOpened']]);
  assert.ok(!JSON.stringify(events).includes('hunter2') && !JSON.stringify(events).includes('token') && !JSON.stringify(events).includes('secret'));
  f.chrome.debugger.onEvent.emit({ tabId: 5 }, 'Runtime.bindingCalled', { name: BINDING, executionContextId: 1, payload: '{"type":"stop"}' });
  await settle();
  assert.equal((await f.ask({ type: 'popup:state', tabId: 5 })).shares.length, 1, "the page's own world cannot press Stop");
  f.chrome.debugger.onEvent.emit({ tabId: 5 }, 'Runtime.bindingCalled', { name: BINDING, executionContextId: WORLD,
    payload: JSON.stringify({ type: 'facts', facts: { password: 1 } }) });
  await settle();
  assert.equal(f.sent('shares').at(-1).shares[0].state, 'handback');
  assert.equal(f.attached.size, 0, 'no debugger stays on a sign-in page');
});

test("leaving the site ends a 'tab' share; a 'site' share waits away and returns", async () => {
  let f = await granted('tab');
  f.tabs.set(5, { id: 5, url: 'https://evil.example/', title: 'Evil', windowId: 1, groupId: 900 });
  f.chrome.tabs.onUpdated.emit(5, { status: 'loading' }, { id: 5 });
  await settle();
  assert.equal(f.attached.size, 0);
  assert.deepEqual(f.sent('detached').at(-1), { type: 'detached', tab_id: '5', reason: 'stopped' });
  assert.deepEqual(f.sent('shares').at(-1).shares, []);
  assert.deepEqual(f.ungrouped, [5], 'the tab leaves the Pebble group');

  f = await granted('site');
  f.tabs.set(5, { id: 5, url: 'https://elsewhere.example/', title: 'Elsewhere', windowId: 1 });
  f.chrome.tabs.onUpdated.emit(5, { status: 'loading' }, { id: 5 });
  await settle();
  assert.deepEqual(f.sent('detached').at(-1).reason, 'away');
  assert.equal(f.sent('shares').at(-1).shares[0].state, 'away');
  let reply = await f.call({ id: 2, op: 'attach', tab_id: '5' });
  assert.equal(reply[0].error.code, 'away');
  reply = await f.call({ id: 3, op: 'tab_info', tab_id: '5' });
  assert.deepEqual(reply[0].result, { title: '', url: '' }, "another site's address and title never leave");
  f.tabs.set(5, { id: 5, url: 'https://mail.example/later', title: 'Later', windowId: 1 });
  f.chrome.tabs.onUpdated.emit(5, { url: 'https://mail.example/later' }, { id: 5 });
  await settle();
  assert.equal(f.sent('shares').at(-1).shares[0].state, 'ready');
});

test('sign-in and payment pages hand back: by address at once, by page facts at attach', async () => {
  const f = await granted();
  f.tabs.set(5, { id: 5, url: 'https://mail.example/login', title: 'Sign in', windowId: 1 });
  f.chrome.tabs.onUpdated.emit(5, { url: 'https://mail.example/login' }, { id: 5 });
  await settle();
  assert.equal(f.sent('shares').at(-1).shares[0].state, 'handback');
  assert.equal(f.attached.size, 0, 'no debugger stays on a sign-in page');
  assert.equal(f.badge[5], '!');
  let reply = await f.call({ id: 2, op: 'attach', tab_id: '5' });
  assert.equal(reply[0].error.code, 'handback');
  f.tabs.set(5, { id: 5, url: 'https://mail.example/inbox', title: 'Inbox', windowId: 1 });
  f.chrome.tabs.onUpdated.emit(5, { url: 'https://mail.example/inbox' }, { id: 5 });
  await settle();
  assert.equal(f.sent('shares').at(-1).shares[0].state, 'ready');
  f.facts.set('current', { password: 0, one_time_code: 0, payment: 1, captcha: 0 });
  reply = await f.call({ id: 3, op: 'attach', tab_id: '5' });
  assert.equal(reply[0].error.code, 'handback', 'a card field found at attach hands back before any read');
  assert.equal(f.attached.size, 0);
  assert.deepEqual(f.commands, [], 'Pebble read nothing');
});

test('navigate stays on the shared site and never goes to a sign-in page', async () => {
  const f = await granted();
  for (const url of ['https://evil.example/', 'http://mail.example/', 'https://mail.example/checkout', 'javascript:alert(1)']) {
    const reply = await f.call({ id: 1, op: 'navigate', tab_id: '5', url });
    assert.equal(reply[0].error.code, 'refused', url);
  }
  const reply = await f.call({ id: 2, op: 'navigate', tab_id: '5', url: 'https://mail.example/compose' });
  assert.equal(reply[0].ok, true);
  assert.deepEqual(f.updates.at(-1), [5, { url: 'https://mail.example/compose' }]);
});

test('Stop in the popup, the page, the infobar or Pebble ends everything', async () => {
  for (const how of ['popup', 'page', 'infobar', 'pebble']) {
    const f = await granted('site');
    if (how === 'popup') await f.ask({ type: 'popup:stop', tabId: 5 });
    if (how === 'page') f.chrome.debugger.onEvent.emit({ tabId: 5 }, 'Runtime.bindingCalled', { name: BINDING, executionContextId: WORLD, payload: '{"type":"stop"}' });
    if (how === 'infobar') { f.attached.delete(5); f.chrome.debugger.onDetach.emit({ tabId: 5 }, 'canceled_by_user'); }
    if (how === 'pebble') await f.toWorker({ type: 'control', action: 'stop_all', reason: 'pebble' });
    await settle();
    assert.equal(f.attached.size, 0, how);
    assert.deepEqual(f.sent('shares').at(-1).shares, [], how);
    assert.deepEqual(f.ungrouped, [5], how);
    assert.deepEqual(f.sent('stop').map(s => s.reason), how === 'pebble' ? [] : [how], how);
    assert.equal(f.port().closed, true, how + ': with nothing shared the host closes');
    assert.equal(f.worker.grantLive(5), false, how);
  }
});

test('pause refuses page commands but still releases held requests', async () => {
  const f = await granted();
  await f.toWorker({ type: 'control', action: 'pause' });
  assert.ok(f.indicator.includes('paused'));
  let reply = await f.call({ id: 2, op: 'cdp', tab_id: '5', method: 'Input.insertText', params: { text: 'x' } });
  assert.equal(reply[0].error.code, 'paused');
  reply = await f.call({ id: 3, op: 'cdp', tab_id: '5', method: 'Fetch.continueRequest', params: { requestId: 'r' } });
  assert.equal(reply[0].ok, true);
  await f.toWorker({ type: 'control', action: 'resume' });
  reply = await f.call({ id: 4, op: 'cdp', tab_id: '5', method: 'Input.insertText', params: { text: 'x' } });
  assert.equal(reply[0].ok, true);
});

test('the debugger lets go when idle, when the tab closes and when Pebble quits; reconnects are bounded', async () => {
  const f = await granted();
  f.fire(IDLE_MS);
  await settle();
  assert.deepEqual(f.sent('detached').at(-1), { type: 'detached', tab_id: '5', reason: 'idle' });
  await f.call({ id: 2, op: 'attach', tab_id: '5' });
  await f.toWorker({ type: 'control', action: 'bye' });
  assert.equal(f.attached.size, 0, 'Pebble quitting is a kill switch');
  assert.equal(f.worker.grantLive(5), false, 'and its grants end with it');
  const g = await granted();
  g.port().onDisconnect.emit();
  await settle();
  assert.equal(g.worker.grantLive(5), false, 'a dropped link ends its grants: a new link must grant again');
  assert.equal(g.badge[5], '•');
  for (const timer of f.timers) timer.live = false;
  f.chrome.runtime.lastError = { message: 'Native host has exited.' };
  f.port().onDisconnect.emit();
  f.chrome.runtime.lastError = null;
  for (let attempt = 0; attempt < MAX_RETRIES + 3; attempt++) {
    const timer = f.timers.find(t => t.live);  // only the reconnect timer is live now
    if (!timer) break;
    timer.live = false;
    timer.fn();
    f.port().onMessage.emit({ type: 'status', state: 'app_not_running', message: 'Pebble is not running.' });
    await settle();
    f.port().onDisconnect.emit();
  }
  assert.equal(f.ports.length, 1 + MAX_RETRIES, 'retries stop after about ten minutes');
  assert.equal((await f.ask({ type: 'popup:state', tabId: 5 })).connection, 'app_not_running');
  f.chrome.tabs.onRemoved.emit(5);
  await settle();
  assert.equal((await f.ask({ type: 'popup:state', tabId: 5 })).shares.length, 0, 'closing the tab ends its share');
});

test('a host that is not installed or refuses this extension is reported, not retried', async () => {
  for (const [error, state] of [['Specified native messaging host not found.', 'not_installed'],
    ['Access to the specified native messaging host is forbidden.', 'forbidden']]) {
    const f = await shared();
    f.chrome.runtime.lastError = { message: error };
    f.port().onDisconnect.emit();
    assert.equal(f.timers.filter(t => t.live && t.ms === 2000).length, 0, state);
    assert.equal((await f.ask({ type: 'popup:state', tabId: 5 })).connection, state);
  }
});

test('shares survive a worker restart only while their tab is still on the site; unpairing forgets the key', async () => {
  const f = await shared();
  const g = fake();
  Object.assign(g.session, f.session);
  g.tabs.set(5, { id: 5, url: 'https://mail.example/inbox', title: 'Inbox', windowId: 1 });
  await g.worker.start();
  assert.equal(g.ports.length, 1, 'a restored share reconnects');
  const h = fake();
  Object.assign(h.session, f.session);
  h.tabs.set(5, { id: 5, url: 'https://evil.example/', title: 'Evil', windowId: 1 });
  await h.worker.start();
  assert.equal(h.ports.length, 0);
  assert.deepEqual(h.session.shares.shares, {});
  await g.welcome();
  const reply = await g.ask({ type: 'popup:unpair', tabId: 5 });
  assert.equal(reply.ok, true);
  await settle();
  assert.equal(g.local.pairing, undefined);
  assert.equal(g.ports.at(-2).closed, true);
  assert.equal(g.port().sent[0].pairing_id, null, 'the next hello asks to pair again');
});
