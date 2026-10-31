import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick } from 'vue';
import { transform } from 'sucrase';

// Phase 46 (1.9.29, D214, D215): Settings › iPhone. Off by default; only the six remote_* routes are reachable; the
// pairing link is never shown as text (Copy puts it on the clipboard) and its QR code is drawn from rows of 0 and 1
// only; the six digits a pairing waits on are confirmed or cancelled here (X-1); phone names and activity lines from
// the bridge stay literal text; every control is a native one a keyboard reaches.
const require = createRequire(import.meta.url);
const calls = [];
const listeners = [];
const copied = [];
const hostile = '<img src=x onerror=alert(1)> iPhone';
const LINK = 'pebble-remote://pair?v=1&bid=b-AAAAAAAAAAAAAAAA&name=Mac&pin=p&code=c&ep=192.168.1.20%3A47800';
const QR = ['1111111', '1000001', '1011101', '1011101', '1011101', '1000001', '1111111'];
const off = { enabled: false, running: false, error: null, lan: true, keep_awake: true, keep_awake_held: false,
  unlock_minutes: 15, unlock_choices: [5, 15, 30, 60], listening: { wifi: [], tailscale: [] }, pairing: null,
  confirm: null, devices: [], connected: 0, activity: [] };
const on = { ...off, enabled: true, running: true, keep_awake_held: true, listening: { wifi: ['192.168.1.20'], tailscale: [] },
  devices: [{ device_id: 'd-1', name: hostile, paired_at: 1761800000000, last_seen: 1761800000000, connected: true }],
  activity: [{ at: 1761800000000, text: hostile + ' approved: npm test' }] };
let current = { ...off };
let pairAnswer = { link: LINK, qr: QR, expires_at: Date.now() + 300000 };
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: JSON.parse(JSON.stringify(params)) });
    if (method === 'remote_status') return JSON.parse(JSON.stringify(current));
    if (method === 'remote_set') {
      current = params.key === 'enabled' ? { ...(params.value ? on : off) } : { ...current, [params.key]: params.value };
      return JSON.parse(JSON.stringify(current));
    }
    if (method === 'remote_pair_start') {
      current = { ...current, pairing: { expires_at: pairAnswer.expires_at } };
      return { ...JSON.parse(JSON.stringify(pairAnswer)), status: JSON.parse(JSON.stringify(current)) };
    }
    if (method === 'remote_pair_cancel' || method === 'remote_pair_confirm') {
      current = { ...current, pairing: null, confirm: null };
      return JSON.parse(JSON.stringify(current));
    }
    if (method === 'remote_device_remove') {
      current = { ...current, devices: [] };
      return JSON.parse(JSON.stringify(current));
    }
    throw new Error('Unexpected RPC ' + method);
  },
  async subscribe(callback) {
    listeners.push(callback);
    return () => listeners.splice(listeners.indexOf(callback), 1);
  },
};
const timers = new Map();
let nextTimer = 1;
globalThis.window = { setInterval: (fn) => { timers.set(nextTimer, fn); return nextTimer++; }, clearInterval: (id) => timers.delete(id) };
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async (text) => { copied.push(text); } } } });

const source = readFileSync(new URL('./RemoteSettings.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'RemoteSettings.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'remote' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'RemoteSettings.vue', id: 'remote',
  compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(source), 'bridge text renders as literal text');
assert.ok(!/\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|EventSource|@tauri-apps|\binvoke\s*\(|localStorage|sessionStorage/.test(source),
  'no network, no direct native call, and the link is never stored');
const imports = [...source.matchAll(/^import .* from '([^']+)';$/gm)].map(match => match[1]);
assert.deepEqual(imports, ['vue', '../bridge'], 'only vue and the bridge are imported');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.deepEqual([...new Set(methods)].sort(), ["'remote_device_remove'", "'remote_pair_cancel'", "'remote_pair_confirm'",
  "'remote_pair_start'", "'remote_set'", "'remote_status'"], 'only the six Pebble Remote routes are reachable');
assert.ok(!/\b(inference|receipts?|host|runtime|capacity)\b/i.test(descriptor.template.content), 'plain words');

function node(type, text = '') {
  return { type, text, children: [], props: {}, parent: null, focused: 0, addEventListener() {}, removeEventListener() {},
    focus() { this.focused++; } };
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
const textOf = element => (element.type === '#comment' ? '' : element.text) + element.children.map(textOf).join('');
const all = element => [element, ...element.children.flatMap(all)];
async function settle() { for (let index = 0; index < 10; index++) { await Promise.resolve(); await nextTick(); } }
const button = (root, label) => all(root).find(e => e.type === 'button' && textOf(e).includes(label));
const switches = root => all(root).filter(e => e.props.role === 'switch');
const emit = async (event, data) => { for (const listener of [...listeners]) await listener({ event, data }); await settle(); };

function nativeOnly(root) {
  for (const element of all(root)) {
    if (element.props.onClick || element.props.onChange) assert.ok(['button', 'select'].includes(element.type), 'only native controls act');
    assert.ok(!(element.props.tabindex < 0) || element.type === 'h4', 'nothing but the card heading leaves the keyboard order');
  }
}

// 1. Off by default: one switch, no pairing, no phones; turning it on is the person's click.
const root = node('root');
const app = renderer.createApp({ setup: () => () => h(Panel) });
app.mount(root);
await settle();
assert.deepEqual(calls, [{ method: 'remote_status', params: {} }]);
let page = textOf(root);
assert.ok(page.includes('Off: no iPhone can reach Pebble.'));
assert.equal(switches(root).length, 1, 'off: only the Pebble Remote switch');
assert.equal(switches(root)[0].props['aria-checked'], 'false');
assert.ok(!button(root, 'Show a pairing code'), 'no pairing while Remote is off');
nativeOnly(root);
await switches(root)[0].props.onClick({});
await settle();
assert.deepEqual(calls[1], { method: 'remote_set', params: { key: 'enabled', value: true } });
page = textOf(root);
assert.equal(switches(root)[0].props['aria-checked'], 'true');
assert.ok(page.includes('Listening on Wi-Fi (192.168.1.20).'));
assert.ok(page.includes(hostile) && !all(root).some(e => ['img', 'script', 'iframe'].includes(e.type)), 'phone names stay text');
assert.ok(page.includes('Connected now') && page.includes(hostile + ' approved: npm test'));
assert.ok(page.includes("A MacBook still sleeps when you close its lid, unless it's on power with an external display."), 'the honest note');
const live = all(root).find(e => e.props.role === 'status');
assert.equal(live.props['aria-live'], 'polite');
assert.ok(textOf(live).includes('Pebble Remote is on.'));
nativeOnly(root);

// 2. A pairing code: a QR code drawn from 0 and 1 only, the link never on screen, Copy puts it on the clipboard.
await button(root, 'Show a pairing code').props.onClick({});
await settle();
assert.deepEqual(calls.at(-1), { method: 'remote_pair_start', params: {} });
const svg = all(root).find(e => e.type === 'svg');
assert.equal(svg.props.role, 'img');
assert.equal(svg.props['aria-label'], 'Pairing code for Pebble Remote');
const path = all(svg).find(e => e.type === 'path');
assert.match(path.props.d, /^(M\d+ \d+h\d+v1h-\d+z)+$/, 'only squares of dark modules');
assert.ok(path.props.d.startsWith('M0 0h7v1h-7z'), 'the first row is one run of seven');
assert.ok(!textOf(root).includes('pebble-remote://'), 'the link itself is never shown');
assert.ok(textOf(root).includes('It works once, for 5:00') || textOf(root).includes('It works once, for 4:5'));
await button(root, 'Copy link').props.onClick({});
await settle();
assert.deepEqual(copied, [LINK]);
assert.ok(textOf(live).includes('Link copied.'));
// The phone used the code: the next state has no pairing link, so the picture goes.
await emit('remote_state', { ...current, pairing: null });
assert.ok(!all(root).some(e => e.type === 'svg'), 'a spent code is no longer drawn');

// 3. X-1: a pairing waits on the Mac. The six digits and the phone's name show as text; focus goes to the question.
await emit('remote_state', { ...current, confirm: { code: '035072', device: hostile, expires_at: Date.now() + 120000 } });
page = textOf(root);
assert.ok(page.includes('Does your iPhone show 035 072?'), 'six digits, leading zero kept');
assert.ok(page.includes('“' + hostile + '” wants to pair with this Mac.'));
const heading = all(root).find(e => e.type === 'h4' && e.props.id === 'remote-confirm-heading');
assert.ok(heading.focused >= 1, 'focus moves to the question');
const card = all(root).find(e => e.props.role === 'group');
assert.equal(card.props['aria-labelledby'], 'remote-confirm-heading');
assert.equal(button(root, 'Show a pairing code').props.disabled, true, 'no new code while one waits');
await button(root, 'They match: pair').props.onClick({});
await settle();
// The answer names the digits it was given for (w27's review F9), so it never answers another phone's pairing.
assert.deepEqual(calls.at(-1), { method: 'remote_pair_confirm', params: { confirm: true, code: '035072' } });
await emit('remote_state', { ...current, confirm: { code: '994460', device: 'iPhone', expires_at: Date.now() + 120000 } });
await button(root, 'Cancel').props.onClick({});
await settle();
assert.deepEqual(calls.at(-1), { method: 'remote_pair_confirm', params: { confirm: false, code: '994460' } });
assert.ok(textOf(live).includes('Nothing was saved.'));

// 4. The switches and the unlock window use their fixed route with the person's choice.
current = JSON.parse(JSON.stringify(on));
await emit('remote_state', current);
const [, awake, lan] = switches(root);
await awake.props.onClick({});
await settle();
assert.deepEqual(calls.at(-1), { method: 'remote_set', params: { key: 'keep_awake', value: false } });
await lan.props.onClick({});
await settle();
assert.deepEqual(calls.at(-1), { method: 'remote_set', params: { key: 'lan', value: false } });
const select = all(root).find(e => e.type === 'select');
assert.equal(select.parent.type, 'label', 'the unlock window has a visible label');
assert.deepEqual(all(select).filter(e => e.type === 'option').map(textOf), ['5 minutes', '15 minutes', '30 minutes', '1 hour']);
await select.props.onChange({ target: { value: '30' } });
await settle();
assert.deepEqual(calls.at(-1), { method: 'remote_set', params: { key: 'unlock_minutes', value: 30 } });

// 5. Removing a phone names it, and takes it away at once.
const remove = all(root).find(e => e.type === 'button' && e.props['aria-label'] === 'Remove ' + hostile);
await remove.props.onClick({});
await settle();
assert.deepEqual(calls.at(-1), { method: 'remote_device_remove', params: { device_id: 'd-1' } });
assert.ok(textOf(root).includes('No iPhone is paired yet.'));

// 6. A refusal shows in an alert; a QR answer with anything but 0 and 1 draws nothing and says so.
pairAnswer = { link: LINK, qr: ['10<script>1'], expires_at: Date.now() + 300000 };
await button(root, 'Show a pairing code').props.onClick({});
await settle();
assert.ok(!all(root).some(e => e.type === 'svg'));
assert.ok(all(root).some(e => e.props.role === 'alert' && textOf(e).includes("Pebble couldn't make a pairing code.")));
nativeOnly(root);

// 7. Leaving the page stops listening and its clock.
app.unmount();
assert.equal(listeners.length, 0, 'the event listener is removed');
assert.equal(timers.size, 0, 'the clock is stopped');
console.log('remote settings: ok');
