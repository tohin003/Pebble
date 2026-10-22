import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 15 C10: the reviewed model handoff shows a content-free preview, needs
// an approval of that exact plan before a start, keeps state on refusals and
// ignores replies for an earlier conversation, run or destination. Only the
// three handoff routes are reachable, and only from the main window.
const require = createRequire(import.meta.url);
const calls = [], failures = new Map(), gates = new Map(), approvals = new Set();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const hostile = '<img src=x onerror=alert(1)>';
let starts = 0, omitRun = false;
function previewFor(params) {
  const destination = params.profile_id === 'hostile-profile' ? 'cloud:' + hostile : 'cloud:https://api.example.com';
  return {
    plan_fingerprint: 'sha256:plan-' + params.id + '-' + params.run_id + '-' + params.profile_id,
    source_class: 'device', destination_class: destination, destination_fingerprint_prefix: 'sha256:0123456789ab',
    counts: { user: { device: 3, [destination]: 1 }, assistant: { device: 2 } },
    withheld_ids: ['w-' + params.id + '-' + params.run_id, 'msg-' + hostile], released_classes: ['device'], authorization: 'none',
  };
}
function answer(method, params) {
  if (method === 'model_handoff_preview') return previewFor(params);
  if (method === 'model_handoff_approve') {
    approvals.add(params.plan_fingerprint);
    return { approved: true, plan_fingerprint: params.plan_fingerprint, authorization: 'none' };
  }
  if (method === 'model_handoff_start') {
    if (!approvals.delete(params.plan_fingerprint)) throw new Error('The reviewed model handoff was refused (not_approved). Nothing was sent.');
    return omitRun ? { authorization: 'none' } : { run_id: 'run-new-' + ++starts, authorization: 'none' };
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

const source = readFileSync(new URL('./ModelHandoff.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'ModelHandoff.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'handoff' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'ModelHandoff.vue', id: 'handoff', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write|<slot/.test(source), 'the preview renders as literal native content');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.ok(methods.length && methods.every(method => /^'[a-z_]+'$/.test(method)), 'every native method name is a fixed literal');
assert.deepEqual([...new Set(methods)].sort(), ["'model_handoff_approve'", "'model_handoff_preview'", "'model_handoff_start'"], 'only the handoff routes are reachable');
assert.deepEqual([...source.matchAll(/from '([^']+)'/g)].map(match => match[1]).sort(), ['../bridge', 'vue']);
// Generated views, mods and the pet window never render it or reach its routes.
for (const surface of ['./NativeArtifactRenderer.vue', './ArtifactPanel.vue', './ModsSettings.vue', '../Pet.vue'])
  assert.ok(!readFileSync(new URL(surface, import.meta.url), 'utf8').includes('ModelHandoff'), surface + ' must not render the handoff');
// The pet window also renders RunPanel; a handoff there must stay an opt-in the pet never passes.
if (readFileSync(new URL('./RunPanel.vue', import.meta.url), 'utf8').includes('ModelHandoff'))
  assert.ok(!/<RunPanel[^>]*profile-?id/i.test(readFileSync(new URL('../Pet.vue', import.meta.url), 'utf8')), 'the pet window never enables the handoff');
const shell = readFileSync(new URL('../../src-tauri/src/main.rs', import.meta.url), 'utf8');
assert.ok(/if !\["main","pet"\]\.contains\(&window\.label\(\)\) \{ return Err\(/.test(shell), 'generated view windows cannot reach native routes');
const pet = shell.match(/window\.label\(\)=="pet" && !\[([^\]]*)\]\.contains/);
assert.ok(pet, 'the pet window has its own allowlist');
for (const route of ['model_handoff_preview', 'model_handoff_approve', 'model_handoff_start']) {
  assert.ok(shell.includes('"' + route + '"'), route + ' is a native route');
  assert.ok(!pet[1].includes('"' + route + '"'), route + ' stays main-window only');
}

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
const events = [];
const props = reactive({ chatId: 'chat-a', runId: 'run-1', profileId: 'cloud-profile' });
const root = node('root');
const app = renderer.createApp({ setup: () => () => h(Panel, { ...props, onStarted: run => events.push('started:' + run), onError: message => events.push('error:' + message) }) });
app.config.errorHandler = error => { throw error; };
const count = method => calls.filter(call => call.method === method).length;
const paramsFor = method => calls.filter(call => call.method === method).at(-1)?.params;
const status = () => all(root).find(element => element.props.role === 'status');
const findButton = label => all(root).find(element => element.type === 'button' && textOf(element) === label);
function button(label) { const found = findButton(label); assert.ok(found, 'Missing button ' + label); return found; }
async function click(label) {
  const target = button(label);
  assert.ok(!target.props.disabled, label + ' must be enabled');
  await target.props.onClick({});
  await settle();
}
const start = () => button('Start the new run');
function terms() {
  const shown = {};
  for (const term of all(root).filter(element => element.type === 'dt')) {
    const siblings = term.parent.children;
    shown[textOf(term)] = textOf(siblings.slice(siblings.indexOf(term) + 1).find(element => element.type === 'dd'));
  }
  return shown;
}
const cells = row => row.children.filter(element => element.type === 'th' || element.type === 'td').map(textOf);

app.mount(root);
await settle();
assert.equal(count('model_handoff_preview'), 1);
assert.deepEqual(calls[0].params, { id: 'chat-a', run_id: 'run-1', profile_id: 'cloud-profile' });
let page = textOf(root);
assert.ok(page.includes('Nothing is sent until you approve this exact handoff and start it.'));
assert.ok(page.includes('History from another route class is withheld unless it is released.'));
assert.deepEqual(terms(), { From: 'This device', To: 'https://api.example.com', 'Destination fingerprint': 'sha256:0123456789ab…' });
assert.ok(page.includes('The messages counted below will be uploaded to https://api.example.com.'));
assert.equal(textOf(all(root).find(element => element.type === 'caption')), 'Messages that will be sent: 6');
const rows = all(root).filter(element => element.type === 'tr').map(cells);
assert.deepEqual(rows, [['Role', 'From https://api.example.com', 'From This device'], ['Pebble’s replies', '0', '2'], ['Your messages', '1', '3']]);
assert.ok(page.includes('Withheld: 2 messages from another route class. They are not sent.'));
assert.deepEqual(all(root).filter(element => element.type === 'li').map(textOf), ['w-chat-a-run-1', 'msg-' + hostile], 'withheld IDs stay plain text');
assert.ok(page.includes('Released for this handoff, so sent although it came from another route class: This device.'));
assert.ok(!all(root).some(element => element.type === 'img'), 'no hostile element is created');
assert.equal(status().props['aria-live'], 'polite');

// Nothing starts before an approval of this exact plan.
assert.equal(start().props.disabled, true, 'start needs an approval first');
await start().props.onClick({});
await settle();
assert.equal(count('model_handoff_start'), 0, 'start cannot run before approval');

failures.set('model_handoff_approve', 'The reviewed model handoff was refused (bad_preview). Nothing was sent.');
await click('Approve this handoff');
assert.equal(events.at(-1), 'error:Error: The reviewed model handoff was refused (bad_preview). Nothing was sent.');
assert.equal(start().props.disabled, true);
assert.ok(textOf(root).includes('w-chat-a-run-1'), 'a refused approval keeps the preview');

await click('Approve this handoff');
assert.deepEqual(paramsFor('model_handoff_approve'), { id: 'chat-a', plan_fingerprint: 'sha256:plan-chat-a-run-1-cloud-profile' });
assert.equal(textOf(status()), 'Approved. Nothing has been sent yet.');
assert.equal(button('Approved').props.disabled, true);
assert.equal(start().props.disabled, false);

// A refused start (the destination changed) sends nothing and keeps the approval.
failures.set('model_handoff_start', 'The reviewed model handoff was refused (destination_changed). Nothing was sent.');
await click('Start the new run');
assert.equal(events.at(-1), 'error:Error: The reviewed model handoff was refused (destination_changed). Nothing was sent.');
assert.ok(!events.some(event => event.startsWith('started:')));
assert.equal(start().props.disabled, false, 'a refused start keeps its state');

await click('Start the new run');
assert.deepEqual(paramsFor('model_handoff_start'), { id: 'chat-a', plan_fingerprint: 'sha256:plan-chat-a-run-1-cloud-profile' });
assert.equal(events.at(-1), 'started:run-new-1');
assert.equal(textOf(status()), 'Started a new run on https://api.example.com.');
assert.equal(start().props.disabled, true, 'one approval starts one run');
assert.equal(button('Approve this handoff').props.disabled, true);

// Replies for an earlier run, conversation or destination are ignored.
const release = hold('model_handoff_preview');
props.runId = 'run-2';
await settle();
props.chatId = 'chat-b';
await settle();
assert.ok(textOf(root).includes('w-chat-b-run-2'));
release();
await settle();
page = textOf(root);
assert.ok(page.includes('w-chat-b-run-2') && !page.includes('w-chat-a-run-2'), 'a stale preview is ignored');
assert.equal(start().props.disabled, true, 'a new plan needs a new approval');
assert.deepEqual(paramsFor('model_handoff_preview'), { id: 'chat-b', run_id: 'run-2', profile_id: 'cloud-profile' });

const releaseApproval = hold('model_handoff_approve');
const late = button('Approve this handoff').props.onClick({});
await settle();
props.profileId = 'hostile-profile';
await settle();
releaseApproval();
await late;
await settle();
assert.equal(start().props.disabled, true, 'an approval for the previous destination is ignored');
assert.equal(textOf(status()), 'Review what would be sent, then approve it.');
assert.equal(terms().To, hostile, 'a hostile destination stays plain text');
assert.ok(textOf(root).includes('The messages counted below will be uploaded to ' + hostile + '.'));
assert.ok(!all(root).some(element => element.type === 'img'));

await click('Approve this handoff');
await click('Start the new run');
assert.deepEqual(paramsFor('model_handoff_start'), { id: 'chat-b', plan_fingerprint: 'sha256:plan-chat-b-run-2-hostile-profile' });
assert.equal(events.at(-1), 'started:run-new-2');

// A start without a reported run is surfaced and is not repeated without a new review.
await click('Review again');
omitRun = true;
await click('Approve this handoff');
await click('Start the new run');
assert.equal(events.at(-1), 'error:Error: Pebble did not report a new run. Check this conversation’s tasks before reviewing the handoff again.');
assert.equal(start().props.disabled, true);
assert.equal(count('model_handoff_start'), 4);
assert.deepEqual([...new Set(calls.map(call => call.method))].sort(), ['model_handoff_approve', 'model_handoff_preview', 'model_handoff_start']);
app.unmount();
console.log('model handoff: ok');
