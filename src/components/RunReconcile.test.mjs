import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 14: the reconciliation panel reaches only the readback and reconcile routes,
// never sends a verdict of its own, and renders readback items as literal text.
const require = createRequire(import.meta.url);
const calls = [], failures = new Map();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const hostile = '<img src=x onerror=alert(1)>';
let items = [
  { call_id: 'call-edit', tool: 'EditFile', kind: 'edit', verdict: 'occurred', basis: 'after_present', authorization: 'none' },
  { call_id: 'call-cmd', tool: hostile, kind: 'command', verdict: 'unknown', basis: 'containment_unproven', authorization: 'none' },
];
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: clone(params) });
    const error = failures.get(method);
    failures.delete(method);
    if (error) throw new Error(error);
    if (method === 'run_readback') return { items: clone(items), authorization: 'none' };
    if (method === 'run_reconcile') { items = items.filter(item => item.call_id !== params.call_id); return { status: 'ok' }; }
    throw new Error('Unexpected RPC ' + method);
  },
};
const source = readFileSync(new URL('./ReconcilePanel.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'ReconcilePanel.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'reconcile' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'ReconcilePanel.vue', id: 'reconcile', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source), 'readback items render as literal native content');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.deepEqual([...new Set(methods)].sort(), ["'run_readback'", "'run_reconcile'"], 'only the readback and reconcile routes are reachable');
assert.ok(!/rpc\('run_reconcile'[^)]*verdict/.test(source), 'the panel never sends a verdict of its own');

function node(type, text = '') {
  return { type, text, children: [], props: {}, parent: null, addEventListener() {}, removeEventListener() {} };
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
const events = [];
const props = reactive({ chatId: 'chat-a', runId: 'run-1' });
const root = node('root');
const app = renderer.createApp({ setup: () => () => h(Panel, { ...props, onRefresh: () => events.push('refresh'), onError: message => events.push('error:' + message) }) });
app.config.errorHandler = error => { throw error; };
const buttons = () => all(root).filter(element => element.type === 'button');
const named = label => buttons().find(element => (element.props['aria-label'] ?? textOf(element)) === label);
const forms = () => all(root).filter(element => element.type === 'form');
const count = method => calls.filter(call => call.method === method).length;

app.mount(root);
await settle();
let page = textOf(root);
assert.equal(count('run_readback'), 1);
assert.deepEqual(calls[0].params, { id: 'chat-a', run_id: 'run-1' });
assert.ok(page.includes('Nothing is retried') && page.includes('EditFile (edit)') && page.includes('Pebble’s readback shows this happened.'));
assert.ok(page.includes('Pebble cannot tell whether this happened.') && page.includes('Basis: containment unproven'));
assert.ok(page.includes(hostile) && !all(root).some(element => element.type === 'img'), 'a hostile tool name stays plain text');
// Only a clean occurred/not_occurred readback offers settlement; the unknown one needs a report.
assert.ok(named('Settle EditFile (edit) from Pebble’s readback'));
assert.equal(buttons().filter(element => textOf(element) === 'Settle from readback').length, 1);

// Settling sends only the call id and the 'readback' request; the host recomputes the verdict.
await named('Settle EditFile (edit) from Pebble’s readback').props.onClick({});
await settle();
const settled = calls.filter(call => call.method === 'run_reconcile').at(-1).params;
assert.deepEqual(settled, { id: 'chat-a', run_id: 'run-1', call_id: 'call-edit', effect: 'readback', note: '' });
assert.deepEqual(events, ['refresh']);
assert.equal(count('run_readback'), 2);
assert.ok(!textOf(root).includes('EditFile'));

// A manual report needs a choice and a note, and is labelled unverified.
page = textOf(root);
assert.ok(page.includes('Report it yourself (recorded as unverified)'));
const submit = () => buttons().find(element => textOf(element) === 'Record my report');
assert.equal(submit().props.disabled, true);
const radios = all(root).filter(element => element.type === 'input' && element.props.type === 'radio');
radios.find(element => element.props.value === 'did_not_occur').props.onChange({});
await settle();
assert.equal(submit().props.disabled, true, 'a note is required');
const area = all(root).find(element => element.type === 'textarea');
area.props.onInput({ target: { value: '  checked the process list  ' } });
await settle();
assert.equal(submit().props.disabled, false);
failures.set('run_reconcile', 'Report occurred or did_not_occur with a note under 2000 characters.');
await forms()[0].props.onSubmit({ preventDefault() {} });
await settle();
assert.deepEqual(events.at(-1), 'error:Error: Report occurred or did_not_occur with a note under 2000 characters.');
assert.ok(textOf(root).includes(hostile), 'a refused report keeps the item');
await forms()[0].props.onSubmit({ preventDefault() {} });
await settle();
assert.deepEqual(calls.filter(call => call.method === 'run_reconcile').at(-1).params,
  { id: 'chat-a', run_id: 'run-1', call_id: 'call-cmd', effect: 'did_not_occur', note: '  checked the process list  ' });
assert.ok(all(root).some(element => element.props.role === 'status' && textOf(element) === 'No uncertain outcome is waiting.'));

// A readback failure is reported, never shown as an empty list.
failures.set('run_readback', 'Stop the active run before reading back its outcomes.');
props.runId = 'run-2';
await settle();
assert.equal(events.at(-1), 'error:Error: Stop the active run before reading back its outcomes.');
assert.ok(!all(root).some(element => element.props.role === 'status'));
app.unmount();
console.log('reconcile panel: ok');
