import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 16: "Route through Pebble" saves a reviewed credential route for a
// local connector, shows the current credential_exposure, keeps the credential
// out of the page, and reaches only its one route.
const require = createRequire(import.meta.url);
const calls = [], failures = new Map(), gates = new Map();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const hostile = '<img src=x onerror=alert(1)>';
const TOKEN = 'secret-token-value-123';
let cleanupPending = false;
const routeInfo = (extra = {}) => ({ kind: 'environment_route', resource: 'https://api.example.com:443', issuer: null, client_id: null, scopes: [],
  environment_names: ['EXAMPLE_TOKEN'], endpoints: { credential_route: { base_url_env: 'EXAMPLE_API_URL', origin: 'https://api.example.com:443', methods: ['GET', 'POST'],
    path_prefix: '/v1/', header: 'Authorization', scheme: 'Bearer' } }, base_digest: '5'.repeat(64), plugin_id: 'p1', state: 'connected', grant_id: 'g2', revision: 2,
  storage_cleanup_pending: false, credential_exposure: 'reviewed_route_placeholder_only', ...extra });
function answer(method, params) {
  if (method === 'connector_save_environment_route') return routeInfo({ plugin_id: params.id, storage_cleanup_pending: cleanupPending });
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

const source = readFileSync(new URL('./ConnectorRouteSettings.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'ConnectorRouteSettings.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'route' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'ConnectorRouteSettings.vue', id: 'route', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(source), 'route facts render as literal native content');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.ok(methods.length && methods.every(method => /^'[a-z_]+'$/.test(method)), 'every native method name is a fixed literal');
assert.deepEqual([...new Set(methods)].sort(), ["'connector_save_environment_route'"], 'only the reviewed route save is reachable');
assert.deepEqual([...source.matchAll(/from '([^']+)'/g)].map(match => match[1]).sort(), ['../bridge', 'vue']);
assert.ok(/const region = shallowRef<HTMLElement \| null>\(null\), card = shallowRef<HTMLElement/.test(source), 'template element refs are shallow');
for (const file of ['../Pet.vue', './NativeArtifactRenderer.vue', './ArtifactPanel.vue', './ModsSettings.vue', './Markdown.vue'])
  assert.ok(!/ScreenSessions|PebbleBrowser|ConnectorEffectReview|ConnectorRouteSettings/.test(readFileSync(new URL(file, import.meta.url), 'utf8')), file + ' renders no connector settings');
// Settings mount it next to the unchanged environment-token warning, for local connectors only.
const settings = readFileSync(new URL('./AdvancedSettings.vue', import.meta.url), 'utf8');
assert.equal([...settings.matchAll(/import ConnectorRouteSettings from "\.\/ConnectorRouteSettings\.vue";/g)].length, 1);
const warning = settings.indexOf("This credential is placed in the local server's environment while it runs. Other local commands that run as your user can read a running program's environment, and macOS has no sandbox rule that prevents it. Use a scoped, revocable credential.");
const saveButton = settings.indexOf('@click="saveConnectorSecret">Save and grant access to this destination</button>');
const mount = settings.indexOf('<ConnectorRouteSettings v-if="connectorMode === \'environment\'" :plugin-id="x.id" :info="connectorInfo[x.id]" :disabled="busy || disabled" @saved="loadExtensions()" />');
assert.ok(warning > 0 && saveButton > warning && mount > saveButton && settings.indexOf('</template>', saveButton) > mount, 'the route mode sits in the environment block, after its warning');
assert.equal([...settings.matchAll(/<ConnectorRouteSettings\b/g)].length, 1);

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
const previous = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot };
globalThis.Document = class Document { activeElement = null; };
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
const textOf = element => (element.type === '#comment' ? '' : element.text) + element.children.map(textOf).join('');
const all = element => [element, ...element.children.flatMap(all)];
async function settle() { for (let index = 0; index < 10; index++) { await Promise.resolve(); await nextTick(); } }
const events = [];
const environment = { kind: 'environment', resource: null, issuer: null, client_id: null, scopes: [], environment_names: ['API_KEY'], endpoints: {}, base_digest: '4'.repeat(64),
  plugin_id: 'p1', state: 'connected', grant_id: 'g1', revision: 1, storage_cleanup_pending: false, credential_exposure: 'environment_readable_by_local_commands' };
const props = reactive({ pluginId: 'p1', info: environment, disabled: false });
const root = node('root');
const view = renderer.createApp({ setup: () => () => h(Panel, { ...props, onSaved: () => events.push('saved') }) });
view.config.errorHandler = error => { throw error; };
const count = method => calls.filter(call => call.method === method).length;
const region = label => all(root).find(element => element.props['aria-label'] === label);
const status = () => textOf(all(root).find(element => element.props.role === 'status'));
const alert = () => all(root).find(element => element.props.role === 'alert');
const button = label => all(root).find(item => item.type === 'button' && textOf(item) === label);
const form = () => region('Route through Pebble settings');
const field = text => { const label = all(root).find(item => item.type === 'label' && textOf(item).startsWith(text)); return label && all(label).find(item => item.type === 'input'); };
const method = name => { const label = all(root).find(item => item.type === 'label' && textOf(item) === name); return label && all(label).find(item => item.type === 'input'); };
const exposure = () => textOf(all(root).find(element => element.type === 'p' && textOf(element).startsWith('Current credential exposure: ')));
const routeFacts = () => {
  const list = region('Reviewed route'), rows = {};
  for (let index = 0; list && index < list.children.length; index++) if (list.children[index].type === 'dt') rows[textOf(list.children[index])] = textOf(list.children[index + 1]);
  return rows;
};
async function click(target) {
  assert.ok(target, 'missing button');
  assert.ok(!target.props.disabled, textOf(target) + ' must be enabled');
  await target.props.onClick({});
  await settle();
}
async function type(target, value) { assert.ok(target, 'missing field'); target.props['onUpdate:modelValue'](value); await settle(); }
async function change(target) { assert.ok(target, 'missing input'); await target.props.onChange({}); await settle(); }
async function fill() {
  await type(field('Credential variable name'), ' EXAMPLE_TOKEN ');
  await type(field('Base-URL variable the server reads'), 'EXAMPLE_API_URL');
  await type(field('Site (HTTPS origin)'), 'https://api.example.com');
  await type(field('Path prefix'), '/v1/');
  await type(field('Connector credential'), TOKEN);
}

view.mount(root);
await settle();
assert.equal(calls.length, 0, 'showing the settings calls nothing');
assert.equal(exposure(), "Current credential exposure: In the local server's environment. Other programs that run as you on this Mac can read it while the server runs.");
assert.equal(region('Reviewed route'), undefined, 'an environment grant has no route');

// The route mode opens a form; nothing is saved until every field is filled.
await click(button('Route through Pebble instead…'));
assert.ok(form() && focused === form(), 'focus moves to the form');
assert.ok(textOf(form()).includes('Instead of the credential, the server gets a placeholder, and its base-URL variable points to a local address that only Pebble answers.'));
assert.ok(textOf(form()).includes("Saving replaces this connector's current access and turns the plugin off until you review and enable it again."));
assert.equal(field('Credential variable name').value, 'API_KEY');
assert.equal(field('Path prefix').value, '/');
assert.equal(field('Connector credential').props.type, 'password');
assert.equal(method('GET').props.checked, true, 'GET is the default method');
assert.equal(button('Save route and grant access').props.disabled, true);
await fill();
await change(method('POST'));
await change(method('GET'));
assert.equal(method('GET').props.checked, false);
await change(method('GET'));
assert.equal(button('Save route and grant access').props.disabled, false);
assert.ok(!textOf(root).includes(TOKEN), 'the credential never becomes page text');

// A refusal is shown; the credential leaves the form before the request, so it is not kept.
failures.set('connector_save_environment_route', 'Route origins are remote HTTPS services, never loopback.');
await click(button('Save route and grant access'));
assert.equal(calls.at(-1).params.token, TOKEN, 'the credential goes only to the native route');
assert.equal(textOf(alert()), 'Route origins are remote HTTPS services, never loopback.');
assert.ok(form() && field('Connector credential').value === '', 'a refused save keeps the form but not the credential');
assert.equal(button('Save route and grant access').props.disabled, true, 'the credential must be entered again');
assert.ok(!events.length);

// A saved route sends exactly the reviewed fields, in canonical method order.
await type(field('Connector credential'), TOKEN);
await click(button('Save route and grant access'));
assert.deepEqual(calls.at(-1), { method: 'connector_save_environment_route', params: { id: 'p1', name: 'EXAMPLE_TOKEN', token: TOKEN, base_url_env: 'EXAMPLE_API_URL',
  origin: 'https://api.example.com', methods: ['GET', 'POST'], path_prefix: '/v1/' } });
assert.equal(status(), 'Route saved in secure storage. The server gets only a placeholder. Review and enable the plugin separately.');
assert.ok(!form() && alert() === undefined && events.join() === 'saved');
assert.equal(exposure(), 'Current credential exposure: Routed through Pebble. The server holds only a placeholder; Pebble adds the credential to the reviewed requests below.');
assert.deepEqual(routeFacts(), { Site: 'https://api.example.com:443', Methods: 'GET, POST', 'Path prefix': '/v1/', 'Base-URL variable': 'EXAMPLE_API_URL',
  'Credential variable': 'EXAMPLE_TOKEN', Header: 'Authorization · Bearer' }, 'the saved reply is shown until settings reload');
assert.ok(!textOf(root).includes(TOKEN));

// The reloaded connector_metadata replaces the saved reply; its values stay plain text.
props.info = routeInfo({ endpoints: { credential_route: { base_url_env: 'EXAMPLE_API_URL', origin: 'https://' + hostile, methods: ['GET'], path_prefix: '/' + hostile, header: 'X-Api-Key', scheme: null } } });
await settle();
assert.deepEqual(routeFacts(), { Site: 'https://' + hostile, Methods: 'GET', 'Path prefix': '/' + hostile, 'Base-URL variable': 'EXAMPLE_API_URL', 'Credential variable': 'EXAMPLE_TOKEN', Header: 'X-Api-Key' });
assert.ok(!all(root).some(element => ['img', 'script'].includes(element.type)), 'no hostile element is created');

// Every exposure label is named in words.
for (const [info, words] of [
  [{ state: 'connected', kind: 'oauth', credential_exposure: 'host_only' }, 'Held by Pebble only. The server never receives it.'],
  [{ state: 'disconnected', plugin_id: 'p1', storage_cleanup_pending: false }, 'No connector access is saved.'],
  [{ state: 'connected', kind: 'mystery', credential_exposure: hostile }, 'Not reported.'],
  [null, 'Not checked yet.'],
]) {
  props.info = info;
  await settle();
  assert.equal(exposure(), 'Current credential exposure: ' + words);
}

// A save whose old credential could not be removed says so.
cleanupPending = true;
await click(button('Route through Pebble instead…'));
await fill();
await click(button('Save route and grant access'));
assert.equal(status(), 'Route saved. Removing the previous credential from secure storage needs another attempt; disconnect to retry.');
cleanupPending = false;

// Busy settings disable the mode; cancelling or switching plugins drops the credential.
props.disabled = true;
await settle();
assert.equal(button('Route through Pebble instead…').props.disabled, true);
props.disabled = false;
await settle();
await click(button('Route through Pebble instead…'));
await type(field('Connector credential'), TOKEN);
await click(button('Cancel'));
assert.ok(!form() && focused === region('Route through Pebble'), 'cancelling returns focus');
await click(button('Route through Pebble instead…'));
assert.equal(field('Connector credential').value, '', 'a cancelled credential is gone');
await type(field('Connector credential'), TOKEN);
props.pluginId = 'p2';
await settle();
assert.equal(form(), undefined, 'another plugin closes the form');
await click(button('Route through Pebble instead…'));
assert.equal(field('Connector credential').value, '');

// A save that settles after the settings close emits nothing.
await fill();
const release = hold('connector_save_environment_route');
const seen = events.length;
const late = button('Save route and grant access').props.onClick({});
await settle();
assert.equal(button('Save route and grant access').props.disabled, true, 'a save in flight cannot be repeated');
view.unmount();
release();
await late;
await settle();
assert.equal(events.length, seen, 'a reply after unmount emits nothing');
assert.equal(calls.at(-1).params.id, 'p2');
assert.equal(count('connector_save_environment_route'), 4);
Object.assign(globalThis, previous);
console.log('connector route settings: ok');
