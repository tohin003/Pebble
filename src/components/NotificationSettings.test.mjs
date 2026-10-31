import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, createSSRApp, h, nextTick } from 'vue';
import { renderToString } from '@vue/server-renderer';
import { transform } from 'sucrase';

// Compile only the checked-in component. Host settings, schedule labels and shell replies are never evaluated.
const require = createRequire(import.meta.url);
const calls = [], invokes = [], failures = new Map();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const kinds = { completed: true, failed: true, action_needed: true, monitor_changed: true };
const host = {
  settings: { enabled: false, kinds: { ...kinds }, preview: 'title', quiet_hours: null },
  jobs: [{ job_id: 'a'.repeat(32), chat_id: 'c'.repeat(32), label: 'Morning digest', state: 'active', muted: false },
    { job_id: 'b'.repeat(32), chat_id: 'c'.repeat(32), label: '<img src=x onerror=host()>', state: 'paused', muted: true }],
  outbox: { pending: 1, delivered: 4, suppressed: 2, failed: 1 }, deferred: 0, quiet_now: false,
  last_failure: { reason: 'not_authorized', at: '2026-10-12T08:00:00Z' }, actions: ['open'],
};
const shellState = { login: 'not_registered', auth: 'not_determined', grant: 'authorized' };
let broken = false;
function respond(method, params) {
  if (method === 'notification_status') return broken ? { settings: { enabled: 'yes' } } : { ...host, lifecycle: null };
  if (method === 'setting') {
    assert.equal(params.key, 'notifications');
    host.settings = clone(params.value);
    return true;
  }
  if (method === 'notification_mute') {
    const job = host.jobs.find(item => item.job_id === params.job_id);
    if (!job) throw new Error('Job not found.');
    job.muted = params.muted;
    return { job_id: job.job_id, muted: job.muted };
  }
  throw new Error('Unexpected RPC ' + method);
}
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: clone(params) });
    const error = failures.get(method);
    failures.delete(method);
    if (error) throw new Error(error);
    return clone(respond(method, params));
  },
};
const core = {
  async invoke(command, args = {}) {
    invokes.push({ command, args: clone(args) });
    if (command === 'login_item_status') return { status: shellState.login };
    if (command === 'login_item_set') { shellState.login = args.enabled ? 'requires_approval' : 'not_registered'; return { status: shellState.login }; }
    if (command === 'notification_authorization') {
      if (args.request) shellState.auth = shellState.grant;
      return { status: shellState.auth };
    }
    throw new Error('Unexpected command ' + command);
  },
};
function compile(filename) {
  const source = readFileSync(new URL(filename, import.meta.url), 'utf8');
  const descriptor = parse(source, { filename }).descriptor;
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  function evaluate(code) {
    const module = { exports: {} };
    new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
      name => name === '../bridge' ? bridge : name === '@tauri-apps/api/core' ? core : require(name), module, module.exports,
    );
    return module.exports;
  }
  const component = evaluate(script.content).default;
  component.render = evaluate(template.code).render;
  return { component, source };
}
const { component: Settings, source } = compile('./NotificationSettings.vue');
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(source), 'settings text is literal native content');
assert.deepEqual([...source.matchAll(/from '([^']+)'/g)].map(match => match[1]).sort(), ['../bridge', '@tauri-apps/api/core', 'vue']);
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.ok(methods.every(method => /^'[a-z_]+'$/.test(method)), 'every native method name is a fixed literal');
assert.deepEqual([...new Set(methods.map(method => method.slice(1, -1)))].sort(), ['notification_mute', 'notification_status', 'setting']);
const commands = [...source.matchAll(/\bshell\('([a-z_]+)'/g)].map(match => match[1]);
assert.deepEqual([...new Set(commands)].sort(), ['login_item_set', 'login_item_status', 'notification_authorization']);
assert.ok(!/notification_native|lifecycle_event|approve/i.test(source.replace(/approvals happen only in the Pebble window/g, '').replace(/needs your approval/g, '')), 'no native-only outbox route and no approval path');

// A tiny DOM-free host renderer running real Vue hooks, v-model directives, forms and buttons.
function node(type, text = '') {
  return { type, tagName: type.toUpperCase(), text, children: [], props: {}, parent: null, value: '', addEventListener() {}, removeEventListener() {}, getRootNode() { return globalThis.document; }, focus() {} };
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
const previous = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot, setTimeout: globalThis.setTimeout, setInterval: globalThis.setInterval };
let timers = 0;
globalThis.Document = class Document { activeElement = null; };
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
globalThis.setTimeout = () => { timers++; return 0; };
globalThis.setInterval = () => { timers++; return 0; };
const textOf = element => (element.type === '#comment' ? '' : element.text) + element.children.map(textOf).join('');
const all = element => [element, ...element.children.flatMap(all)];
const ownText = element => element.children.filter(child => child.type === '#text').map(child => child.text).join('').trim();
const nameOf = element => element.props['aria-label'] ?? textOf(element);
async function settle() { for (let index = 0; index < 10; index++) { await Promise.resolve(); await nextTick(); } }
const root = node('root');
let state;
const instrumented = { ...Settings, setup(properties, context) { state = Settings.setup(properties, context); return state; } };
const app = renderer.createApp({ setup: () => () => h(instrumented) });
app.config.errorHandler = error => { throw error; };
const page = () => textOf(root);
function button(label) { const found = all(root).find(element => element.type === 'button' && nameOf(element) === label); assert.ok(found, 'Missing button ' + label); return found; }
async function click(label) {
  const target = button(label);
  assert.ok(!target.props.disabled, label + ' must be enabled');
  const event = { preventDefault() {}, stopPropagation() {} };
  if (target.props.onClick) await target.props.onClick(event);
  else {
    let form = target.parent;
    while (form && form.type !== 'form') form = form.parent;
    assert.ok(target.props.type === 'submit' && form?.props.onSubmit, label + ' must submit its form');
    await form.props.onSubmit(event);
  }
  await settle();
}
function control(name) {
  const label = all(root).filter(element => element.type === 'label').find(element => ownText(element) === name || ownText(element).startsWith(name));
  assert.ok(label, 'Missing field ' + name);
  const found = all(label).find(element => element.type === 'input');
  assert.ok(found, 'Missing control ' + name);
  return found;
}
async function set(name, value) {
  const input = control(name);
  input.props['onUpdate:modelValue'](input.props.type === 'radio' ? input.props.value : value);
  await settle();
}
const count = method => calls.filter(call => call.method === method).length;
const last = method => calls.filter(call => call.method === method).at(-1)?.params;
const asked = () => invokes.filter(call => call.command === 'notification_authorization' && call.args.request === true).length;
let mounted = false;

try {
  app.mount(root); mounted = true;
  await settle();

  // Mount reads host status and shell status only; macOS is never asked for permission at startup.
  assert.deepEqual(calls.map(call => call.method), ['notification_status']);
  assert.deepEqual(invokes.map(call => [call.command, call.args]).sort(), [['login_item_status', {}], ['notification_authorization', { request: false }]]);
  assert.equal(asked(), 0);
  assert.equal(timers, 0, 'the panel never polls');
  let text = page();
  for (const expected of ['Pebble does not open at login.', 'macOS asks for permission when you turn notifications on', 'Title only', 'Default. Shows which schedule has an update', 'Nothing is dropped', 'Banners offer only Open', 'approvals happen only in the Pebble window', 'Waiting 1 · Shown 4 · Not shown 2 · Failed 1', 'Last problem: macOS did not allow a notification.', 'Morning digest · banners on']) {
    assert.ok(text.includes(expected), 'missing ' + expected);
  }
  assert.equal(control('Show macOS notifications').props.type, 'checkbox');
  assert.equal(state.form.enabled, false, 'off until the person turns it on');
  assert.equal(state.form.preview, 'title');
  assert.ok(!all(root).some(element => element.type === 'button' && /approve|reply|grant/i.test(nameOf(element))), 'no approval, reply or grant control');

  // Turning on asks macOS once, then saves exactly the four host fields.
  await set('Show macOS notifications', true);
  await click('Save notification preferences');
  assert.equal(asked(), 1);
  assert.deepEqual(last('setting'), { key: 'notifications', value: { enabled: true, kinds, preview: 'title', quiet_hours: null, replies: true } });
  assert.ok(page().includes('Notification preferences saved.') && page().includes('macOS allows Pebble notifications.'));
  // Later edits do not ask again.
  await set('Failures', false);
  await set('Title and a short preview', true);
  await click('Save notification preferences');
  assert.equal(asked(), 1);
  assert.deepEqual(last('setting').value, { enabled: true, kinds: { ...kinds, failed: false }, preview: 'body', quiet_hours: null, replies: true });

  // Quiet hours are validated locally and carry this device's zone.
  await set('Hold banners during quiet hours', true);
  await set('From', '22:00'); await set('Until', '22:00');
  const saves = count('setting');
  await click('Save notification preferences');
  assert.equal(state.error.value, 'Quiet hours need different start and end times.');
  await set('Until', '7am');
  await click('Save notification preferences');
  assert.match(state.error.value, /24-hour times/);
  assert.equal(count('setting'), saves, 'invalid quiet hours never reach the host');
  await set('Until', '07:00');
  await click('Save notification preferences');
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  assert.deepEqual(last('setting').value.quiet_hours, { start: '22:00', end: '07:00', timezone: zone });
  assert.ok(page().includes('Times are in ' + zone + '.'));

  // A denied permission keeps notifications off and says where to change it.
  host.settings = { enabled: false, kinds: { ...kinds }, preview: 'title', quiet_hours: null };
  shellState.auth = 'denied'; shellState.grant = 'denied';
  await state.load(); await settle();
  await set('Show macOS notifications', true);
  const before = count('setting');
  await click('Save notification preferences');
  assert.equal(count('setting'), before, 'nothing is enabled when macOS denies permission');
  assert.match(state.error.value, /System Settings → Notifications/);
  assert.equal(host.settings.enabled, false);

  // Per-schedule mute sends only the schedule id and the new value.
  await click('Mute banners for Morning digest');
  assert.deepEqual(last('notification_mute'), { job_id: 'a'.repeat(32), muted: true });
  assert.ok(page().includes('Morning digest · banners muted') && page().includes('Its updates stay in the app.'));
  await click('Unmute banners for <img src=x onerror=host()>');
  assert.deepEqual(last('notification_mute'), { job_id: 'b'.repeat(32), muted: false });
  failures.set('notification_mute', 'Job not found.');
  await click('Unmute banners for Morning digest');
  assert.equal(state.error.value, 'Job not found.');
  assert.ok(page().includes('Morning digest · banners muted'), 'a refused change is not shown as done');
  assert.ok(!all(root).some(element => ['img', 'script', 'iframe', 'svg'].includes(element.type)), 'labels never become markup');

  // The login item shows the shell's real status, including the approval macOS may require.
  await click('Open Pebble at login');
  assert.deepEqual(invokes.at(-1), { command: 'login_item_set', args: { enabled: true } });
  assert.ok(page().includes('Open System Settings → General → Login Items and allow Pebble.'));
  await click('Stop opening Pebble at login');
  assert.deepEqual(invokes.at(-1), { command: 'login_item_set', args: { enabled: false } });
  shellState.login = 'not_found';
  await state.load(); await settle();
  assert.ok(page().includes('add Pebble in System Settings → General → Login Items'));  // Phase 45 (F008)
  assert.ok(!all(root).some(element => element.type === 'button' && /at login/.test(nameOf(element))), 'no toggle for a login item the build cannot register');

  // A malformed host reply is refused and changes nothing.
  broken = true; await state.load(); await settle(); broken = false;
  assert.equal(state.error.value, 'Notification settings are unavailable.');
  assert.equal(state.status.value.jobs.length, 2, 'the last valid status stays');

  // Hostile labels render as escaped text.
  const html = await renderToString(createSSRApp({
    ...Settings,
    setup(properties, context) {
      const bindings = Settings.setup(properties, context);
      bindings.status.value = { ...host, jobs: [{ job_id: 'd'.repeat(32), label: '<script>mute()</script>', state: '<svg onload=x()>', muted: false }], last_failure: { reason: '<iframe src=y>' } };
      bindings.login.value = '<img src=z>';
      return bindings;
    },
  }));
  assert.ok(html.includes('&lt;script&gt;mute()&lt;/script&gt;') && !/<(?:img|script|svg|iframe)\b/i.test(html));
  assert.ok(html.includes('Checking login item…') && html.includes('A notification could not be shown.'), 'unknown shell and failure values fall back to fixed text');

  // The browser preview never reaches native routes or shell commands.
  bridge.native = false;
  const beforePreview = calls.length + invokes.length, previewRoot = node('root');
  const preview = renderer.createApp(Settings);
  preview.config.errorHandler = error => { throw error; };
  preview.mount(previewRoot); await settle();
  assert.equal(calls.length + invokes.length, beforePreview);
  assert.ok(textOf(previewRoot).includes('available in the Pebble desktop app'));
  preview.unmount(); bridge.native = true;

  app.unmount(); mounted = false;
  assert.equal(timers, 0, 'no timer survives the panel');
  console.log('Notification settings passed: no permission prompt at startup, one prompt on first enable, exact four-field host setting, local quiet-hour validation with the device zone, denied permission kept off, per-schedule mute by id, honest login-item states, malformed replies refused, hostile labels escaped, browser preview inert. Native IPC and the shell are simulated; no installed app or macOS prompt is exercised.');
} finally {
  if (mounted) app.unmount();
  globalThis.document = previous.document;
  globalThis.Document = previous.Document;
  globalThis.ShadowRoot = previous.ShadowRoot;
  globalThis.setTimeout = previous.setTimeout;
  globalThis.setInterval = previous.setInterval;
}
