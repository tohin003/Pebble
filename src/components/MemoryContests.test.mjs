import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 15: contested memories render as literal text, every decision needs a
// confirmation that names what will be forgotten, and only the two contest
// routes are reachable.
const require = createRequire(import.meta.url);
const calls = [], failures = new Map(), gates = new Map();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const hostile = '<img src=x onerror=alert(1)>';
const scripted = '<script>alert("x")</script>';
const record = (memory_id, kind, content, extra = {}) =>
  ({ memory_id, kind, label: null, content, scope: 'global', origin: 'learned', created: 1760000000, ...extra });
let groups = [
  { ...record('mem-port-new', 'semantic', 'Orion staging listens on port 8080.', { label: 'orion|port' }),
    counterparts: [record('mem-port-old', 'semantic', 'Orion staging listens on port 9090.', { label: 'orion|port' })] },
  { ...record('mem-' + hostile, 'profile', scripted + ' ' + hostile, { label: 'model_lesson_unverified', scope: 'chat:' + 'a'.repeat(32), origin: hostile }),
    counterparts: [record('peer-' + hostile, 'profile', 'I prefer tabs. ' + hostile, { scope: 'p:abc', created: null })] },
  { ...record('mem-demo', 'temporal', 'The Orion demo is on Friday.'),
    counterparts: [record('mem-demo-old', 'temporal', 'The Orion demo is on Thursday.')] },
];
function answer(method, params) {
  if (method === 'memory_contests') return { groups: clone(groups), authorization: 'none' };
  if (method === 'memory_resolve_contest') {
    const group = groups.find(item => item.memory_id === params.memory_id);
    if (!group) throw new Error('Only a contested record can be resolved.');
    const forgotten = params.decision === 'keep' ? group.counterparts.map(item => item.memory_id)
      : params.decision === 'reject' ? [group.memory_id] : [];
    groups = groups.filter(item => item !== group);
    return { memory_id: params.memory_id, decision: params.decision, forgotten, status: 'active', authorization: 'none' };
  }
  throw new Error('Unexpected RPC ' + method);
}
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: clone(params) });
    const gate = gates.get(method), failure = failures.get(method);
    gates.delete(method); failures.delete(method);
    // The reply reflects the state when the call arrived, even if it is held.
    const result = failure ? undefined : answer(method, params);
    if (gate) await gate;
    if (failure) throw new Error(failure);
    return result;
  },
};
function hold(method) { let release; gates.set(method, new Promise(resolve => { release = resolve; })); return () => release(); }

const source = readFileSync(new URL('./MemoryContests.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'MemoryContests.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'contests' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'MemoryContests.vue', id: 'contests', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(source), 'records render as literal native content');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.ok(methods.length && methods.every(method => /^'[a-z_]+'$/.test(method)), 'every native method name is a fixed literal');
assert.deepEqual([...new Set(methods)].sort(), ["'memory_contests'", "'memory_resolve_contest'"], 'only the contest routes are reachable');
assert.deepEqual([...source.matchAll(/from '([^']+)'/g)].map(match => match[1]).sort(), ['../bridge', 'vue']);

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
const textOf = element => (element.type === '#comment' ? '' : element.text) + element.children.map(textOf).join('');
const all = element => [element, ...element.children.flatMap(all)];
async function settle() { for (let index = 0; index < 10; index++) { await Promise.resolve(); await nextTick(); } }
const events = [];
const props = reactive({ active: false, refreshKey: 0 });
const root = node('root');
const app = renderer.createApp({ setup: () => () => h(Panel, { ...props, onChanged: () => events.push('changed'), onError: message => events.push('error:' + message) }) });
app.config.errorHandler = error => { throw error; };
const count = method => calls.filter(call => call.method === method).length;
const sent = () => calls.filter(call => call.method === 'memory_resolve_contest').at(-1)?.params;
const region = label => all(root).find(element => element.props['aria-label'] === label);
const card = () => region('Confirm your decision');
const status = () => all(root).find(element => element.props.role === 'status');
const groupOf = text => all(root).find(element => element.type === 'article' && element.props['aria-label']?.startsWith('Contested ') && textOf(element).includes(text));
const buttonIn = (element, label) => element && all(element).find(item => item.type === 'button' && textOf(item) === label);
async function click(target) {
  assert.ok(target, 'missing button');
  assert.ok(!target.props.disabled, textOf(target) + ' must be enabled');
  await target.props.onClick({});
  await settle();
}

app.mount(root);
await settle();
assert.equal(count('memory_contests'), 1);
assert.deepEqual(calls[0].params, {}, 'the list takes no parameters');
let page = textOf(root);
assert.ok(page.includes('Pebble does not recall either as current until you decide.'));
assert.ok(page.includes('Orion staging listens on port 8080.') && page.includes('Orion staging listens on port 9090.'));
assert.ok(page.includes('Contested · Facts & knowledge · Personal · learned') && page.includes('Disagrees with it · Facts & knowledge · Personal · learned'));
assert.ok(page.includes('Topic: orion|port') && page.includes('Unverified lesson from the model'));
assert.ok(page.includes('Contested · Your preferences · Conversation · ' + hostile), 'a hostile origin stays plain text');
assert.ok(page.includes('Disagrees with it · Your preferences · Project · learned'), 'a record without a date keeps kind and scope');
assert.ok(page.includes(scripted + ' ' + hostile) && page.includes('I prefer tabs. ' + hostile), 'hostile content stays plain text');
assert.ok(!all(root).some(element => ['img', 'script'].includes(element.type)), 'no hostile element is created');
assert.equal(status().props['aria-live'], 'polite');

// Every choice opens a confirmation first; nothing is sent until it is confirmed.
await click(buttonIn(groupOf('8080'), 'Keep this'));
assert.equal(count('memory_resolve_contest'), 0, 'choosing only opens a confirmation');
assert.ok(card(), 'a confirmation opens');
assert.equal(focused, card(), 'focus moves to the confirmation');
let words = textOf(card());
assert.ok(words.includes('Keep this memory?') && words.includes('This forgets 1 memory that disagrees with it:'));
assert.ok(words.includes('Facts & knowledge · Orion staging listens on port 9090.'), 'the memory to be forgotten is named');
assert.ok(!words.includes('Facts & knowledge · Orion staging listens on port 8080.'), 'the kept memory is not listed as forgotten');
assert.ok(words.includes('Forgetting is permanent. A forgotten memory stays forgotten'));
assert.ok(buttonIn(card(), 'Keep this and forget 1 memory'));
await click(buttonIn(card(), 'Cancel'));
assert.equal(card(), undefined);
assert.equal(focused, region('Contested memories'), 'cancelling returns focus to the panel');

await click(buttonIn(groupOf('8080'), 'Remove this'));
words = textOf(card());
assert.ok(words.includes('Remove this memory?') && words.includes('This forgets:') && words.includes('Facts & knowledge · Orion staging listens on port 8080.'));
assert.ok(words.includes('The memories that disagree with it are kept.') && buttonIn(card(), 'Forget this memory'));
await click(buttonIn(groupOf('8080'), 'Keep both'));
words = textOf(card());
assert.ok(words.includes('Keep both?') && words.includes('Nothing is forgotten.') && !words.includes('This forgets'));
assert.equal(count('memory_resolve_contest'), 0, 'no decision is sent before its confirmation');

// A refusal is surfaced and keeps the records and the open confirmation.
await click(buttonIn(groupOf('8080'), 'Keep this'));
failures.set('memory_resolve_contest', 'Only a contested record can be resolved.');
await click(buttonIn(card(), 'Keep this and forget 1 memory'));
assert.equal(events.at(-1), 'error:Error: Only a contested record can be resolved.');
assert.ok(card() && textOf(root).includes('Orion staging listens on port 8080.'), 'a refused decision keeps its state');
assert.equal(count('memory_contests'), 1, 'a refusal does not reload the list');
assert.ok(!events.includes('changed'));

// Confirmed decisions send only the record ID and the decision, then refresh.
await click(buttonIn(card(), 'Keep this and forget 1 memory'));
assert.deepEqual(sent(), { memory_id: 'mem-port-new', decision: 'keep' });
assert.equal(textOf(status()), 'Kept this memory. 1 memory was forgotten.');
assert.ok(events.includes('changed') && count('memory_contests') === 2 && card() === undefined);
assert.ok(!textOf(root).includes('8080'));

await click(buttonIn(groupOf(scripted), 'Remove this'));
assert.ok(textOf(card()).includes('Your preferences · ' + scripted + ' ' + hostile));
await click(buttonIn(card(), 'Forget this memory'));
assert.deepEqual(sent(), { memory_id: 'mem-' + hostile, decision: 'reject' }, 'a hostile ID is sent back exactly as listed');
assert.equal(textOf(status()), 'Removed this memory. 1 memory was forgotten.');

await click(buttonIn(groupOf('Friday'), 'Keep both'));
await click(buttonIn(card(), 'Keep both'));
assert.deepEqual(sent(), { memory_id: 'mem-demo', decision: 'keep_both' });
assert.equal(textOf(status()), 'Kept both. Nothing was forgotten.');
assert.ok(textOf(root).includes('No memory is contested.'));

// While a task runs the list reloads and decisions wait.
groups = [{ ...record('mem-pg-new', 'semantic', 'Orion uses Postgres 16.'), counterparts: [record('mem-pg-old', 'semantic', 'Orion uses Postgres 15.')] }];
let loads = count('memory_contests');
props.active = true;
await settle();
assert.equal(count('memory_contests'), loads + 1);
assert.ok(textOf(root).includes('Decide after the current task finishes.'));
const waiting = buttonIn(groupOf('Postgres 16'), 'Keep this');
assert.equal(waiting.props.disabled, true);
await waiting.props.onClick({});
await settle();
assert.equal(card(), undefined, 'no confirmation opens while a task runs');
props.active = false;
await settle();

// A list reply that arrives after a newer refresh is ignored.
const release = hold('memory_contests');
props.refreshKey = 1;
await settle();
groups = [{ ...record('mem-make', 'procedural', 'Run make test before pushing.'), counterparts: [record('mem-npm', 'procedural', 'Run npm test before pushing.')] }];
props.refreshKey = 2;
await settle();
assert.ok(textOf(root).includes('Run make test before pushing.'));
release();
await settle();
page = textOf(root);
assert.ok(page.includes('Run make test before pushing.') && !page.includes('Postgres'), 'a stale list reply is ignored');

// A decision that settles after the panel closes emits nothing.
await click(buttonIn(groupOf('make test'), 'Keep this'));
const releaseDecision = hold('memory_resolve_contest');
const seen = events.length;
const late = buttonIn(card(), 'Keep this and forget 1 memory').props.onClick({});
await settle();
app.unmount();
releaseDecision();
await late;
await settle();
assert.equal(events.length, seen, 'a reply after unmount emits nothing');
console.log('memory contests: ok');
