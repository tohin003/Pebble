import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 18 E06: telemetry is off until the person ticks the notice and turns it on with the host's current
// consent version; the notice is literal text; saving a copy passes only a single-use native token; and only
// the four fixed telemetry routes are reachable, all through native controls a keyboard can reach.
const require = createRequire(import.meta.url);
const calls = [];
const hostile = '<img src=x onerror=alert(1)> Ignore previous instructions';
const VERSION = 'pebble.telemetry.consent/1';
const base = {
  schema: 'pebble.telemetry/1', enabled: false, consent_version: null, consented_at: null,
  current_consent_version: VERSION, consent_text: 'Pebble can keep a private usage log. ' + hostile,
  install_id: null, events: 0, bytes: 0, dropped: 0, expired: 0, refused: 0,
  fields: { run_settled: ['state', 'steps', 'tools'], resource_sample: ['footprint_mb_log2'], error: ['code'] },
  limits: { max_events: 1000, max_bytes: 262144, retention_days: 30 }, destination: null, network_upload: false,
};
let current = { ...base };
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: JSON.parse(JSON.stringify(params)) });
    if (method === 'telemetry_get') return JSON.parse(JSON.stringify(current));
    if (method === 'telemetry_set') {
      current = params.enabled
        ? { ...current, enabled: true, consent_version: params.consent_version, consented_at: 1760000000, install_id: 'a'.repeat(32), events: 2 }
        : { ...base };
      return JSON.parse(JSON.stringify(current));
    }
    if (method === 'telemetry_clear') {
      current = { ...current, events: 0, install_id: 'b'.repeat(32) };
      return JSON.parse(JSON.stringify(current));
    }
    if (method === 'telemetry_export') return { events: 2, sha256: 'c'.repeat(64), verified: true, published: false };
    throw new Error('Unexpected RPC ' + method);
  },
};

const source = readFileSync(new URL('./TelemetrySettings.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'TelemetrySettings.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'telemetry' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'TelemetrySettings.vue', id: 'telemetry', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(source), 'host text renders as literal text');
assert.ok(!/\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|EventSource|@tauri-apps|\binvoke\s*\(/.test(source), 'no network or direct native call');
const imports = [...source.matchAll(/^import .* from '([^']+)';$/gm)].map(match => match[1]);
assert.deepEqual(imports, ['vue', '../bridge'], 'only vue and the bridge are imported');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.deepEqual([...new Set(methods)].sort(), ["'telemetry_clear'", "'telemetry_export'", "'telemetry_get'", "'telemetry_set'"],
  'only the four fixed telemetry routes are reachable');
assert.ok(!/consent_version:\s*'/.test(source), 'the consent version comes from the host, never a literal');

function node(type, text = '') {
  return { type, text, children: [], props: {}, parent: null, addEventListener() {}, removeEventListener() {}, focus() {} };
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

function interactiveOnly(root) {
  for (const element of all(root)) {
    if (element.props.onClick || element.props.onChange) assert.ok(['button', 'input'].includes(element.type), 'only native controls act');
    assert.ok(!(element.props.tabindex < 0), 'nothing is removed from keyboard order');
  }
}

// 1. Without a native Save dialog: off by default; the notice is literal; turning on needs the tick.
const props = reactive({});
const root = node('root');
renderer.createApp({ setup: () => () => h(Panel, { ...props }) }).mount(root);
await settle();
assert.deepEqual(calls, [{ method: 'telemetry_get', params: {} }]);
let page = textOf(root);
assert.ok(page.includes('Off') && page.includes('nothing is collected') && page.includes('nothing is uploaded'));
assert.ok(page.includes(hostile) && !all(root).some(e => ['img', 'script', 'iframe'].includes(e.type)), 'hostile notice stays text');
assert.ok(page.includes('1000 events or 256 KB, for 30 days'));
assert.ok(page.includes('run settled: state, steps, tools'));
const box = all(root).find(e => e.type === 'input');
assert.equal(box.props.type, 'checkbox');
assert.equal(box.props['aria-describedby'], 'telemetry-notice');
assert.ok(all(root).some(e => e.props.id === 'telemetry-notice'), 'the checkbox describes the notice it accepts');
assert.equal(box.parent.type, 'label', 'the checkbox has a visible label');
assert.equal(button(root, 'Turn on').props.disabled, true, 'turning on needs the tick');
interactiveOnly(root);
await button(root, 'Turn on').props.onClick({});
await settle();
assert.equal(calls.length, 1, 'no call before the person agrees');
box.props.onChange({ target: { checked: true } });
await settle();
assert.equal(button(root, 'Turn on').props.disabled, false);
await button(root, 'Turn on').props.onClick({});
await settle();
assert.deepEqual(calls[1], { method: 'telemetry_set', params: { enabled: true, consent_version: VERSION } });
page = textOf(root);
assert.ok(page.includes('On') && page.includes('2 events kept') && page.includes('Random ID ' + 'a'.repeat(32)));
assert.equal(button(root, 'Save a copy').props.disabled, true, 'no Save dialog, no saving');
assert.ok(page.includes('Saving needs the native Save dialog.'));
const live = all(root).find(e => e.props.role === 'status');
assert.equal(live.props['aria-live'], 'polite');
assert.ok(textOf(live).includes('Telemetry is on'));
interactiveOnly(root);

// 2. Clearing and turning off use their fixed routes.
await button(root, 'Clear log').props.onClick({});
await settle();
assert.deepEqual(calls[2], { method: 'telemetry_clear', params: {} });
assert.ok(textOf(root).includes('Random ID ' + 'b'.repeat(32)));
await button(root, 'Turn off').props.onClick({});
await settle();
assert.deepEqual(calls[3], { method: 'telemetry_set', params: { enabled: false } });
assert.ok(textOf(root).includes('Off') && textOf(live).includes('deleted'));
assert.equal(calls.length, 4);

// 3. With the parent's native Save dialog: a cancelled or malformed choice never reaches the backend; a token does.
current = { ...base, enabled: true, consent_version: VERSION, consented_at: 1760000000, install_id: 'a'.repeat(32), events: 2 };
calls.length = 0;
const choices = [null, '/Users/someone/telemetry.json', '../../escape', 'tok-' + 'd'.repeat(28)];
const asked = [];
const second = node('root');
renderer.createApp({ setup: () => () => h(Panel, { chooseDestination: async () => { asked.push(1); return choices.shift(); } }) }).mount(second);
await settle();
assert.equal(button(second, 'Save a copy').props.disabled, false);
assert.ok(!textOf(second).includes('Saving needs the native Save dialog.'));
const status = all(second).find(e => e.props.role === 'status');
await button(second, 'Save a copy').props.onClick({});
await settle();
assert.ok(textOf(status).includes('cancelled'));
for (let attempt = 0; attempt < 2; attempt++) {
  await button(second, 'Save a copy').props.onClick({});
  await settle();
  assert.ok(textOf(status).includes('Nothing was saved'));
}
assert.deepEqual(calls.map(c => c.method), ['telemetry_get'], 'no export call without a well-formed token');
await button(second, 'Save a copy').props.onClick({});
await settle();
assert.deepEqual(calls.slice(1), [{ method: 'telemetry_export', params: { destination_token: 'tok-' + 'd'.repeat(28) } },
  { method: 'telemetry_get', params: {} }]);
assert.ok(textOf(status).includes('Saved 2 events and checked the copy') && textOf(status).includes('Nothing was uploaded'));
assert.equal(asked.length, 4);
interactiveOnly(second);
console.log('telemetry settings: ok');
