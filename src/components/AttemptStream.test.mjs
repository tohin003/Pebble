import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 20 H20: a draft the host withdrew after it streamed stays in the live panel, labelled as a withdrawn draft.
// Compile only the checked-in component; stream text is rendered as text, never evaluated.
const require = createRequire(import.meta.url);
const pages = [];
const calls = [];
let listener = null;
const bridge = {
  native: true,
  async subscribe(callback) { listener = callback; return () => { listener = null; }; },
  async rpc(method, params) {
    calls.push({ method, params: structuredClone(params) });
    if (method !== 'stream_read') throw new Error('Unexpected RPC ' + method);
    const events = (pages.shift() || []).filter(event => event.sequence > params.after);
    return { events, next_after: events.length ? events.at(-1).sequence : params.after, more: false };
  },
};
function compile(filename) {
  const source = readFileSync(new URL(filename, import.meta.url), 'utf8');
  const descriptor = parse(source, { filename }).descriptor;
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  const evaluate = code => {
    const module = { exports: {} };
    new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
      name => name === '../bridge' ? bridge : require(name), module, module.exports);
    return module.exports;
  };
  const component = evaluate(script.content).default;
  component.render = evaluate(template.code).render;
  return { component, source };
}
const { component: AttemptStream, source } = compile('./AttemptStream.vue');
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source), 'stream text is literal');

function node(type, text = '') {
  return { type, text, children: [], props: {}, parent: null, value: '', addEventListener() {}, removeEventListener() {}, getRootNode: () => globalThis.document };
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
const sections = (root, label) => all(root).filter(element => element.type === 'section' && element.props['aria-label'] === label);
const previous = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot, setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
const timers = new Map();
let timerId = 0;
async function settle() { for (let index = 0; index < 8; index++) { await Promise.resolve(); await nextTick(); } }
async function tick() { const pending = [...timers.values()]; timers.clear(); pending.forEach(callback => callback()); await settle(); }
const begin = (sequence, id) => ({ sequence, attempt_id: id, kind: 'begin', attempt: 1 });
const delta = (sequence, id, text) => ({ sequence, attempt_id: id, kind: 'delta', text });
const committed = (sequence, id) => ({ sequence, attempt_id: id, kind: 'settle', state: 'committed' });
const emit = (type, attempt_id, run_id = 'run-1', chat_id = 'chat-a') => listener({ event: 'stream', data: { chat_id, run_id, attempt_id, type, authorization: 'none' } });
const hostile = '<img src=x onerror=host()> withdrawn draft text';
try {
  globalThis.Document = class Document { activeElement = null; };
  globalThis.ShadowRoot = class ShadowRoot {};
  globalThis.document = new globalThis.Document();
  globalThis.setTimeout = (callback, delay) => { assert.equal(delay, 150, 'stream coalescing stays bounded'); const id = ++timerId; timers.set(id, callback); return id; };
  globalThis.clearTimeout = id => timers.delete(id);
  const props = reactive({ chatId: 'chat-a', runId: 'run-1' });
  const root = node('root');
  const app = renderer.createApp({ setup: () => () => h(AttemptStream, props) });
  const errors = [];
  app.config.errorHandler = error => { errors.push(error); };
  app.mount(root);
  await settle();
  pages.push([]);
  await tick();
  assert.deepEqual(sections(root, 'Withdrawn draft'), []);

  // 1. The draft streams live, then the host withdraws it after commit. One page holds the draft's settle and the
  // repair's whole attempt (the 150 ms coalescing), so only the bounded list of settled attempts still has the draft.
  pages.push([begin(1, 'draft'), delta(2, 'draft', hostile)]);
  emit('begin', 'draft'); await tick();
  assert.ok(textOf(root).includes('Receiving model response') && textOf(root).includes(hostile));
  pages.push([committed(3, 'draft'), begin(4, 'repair'), delta(5, 'repair', 'The repaired reply.'), committed(6, 'repair')]);
  emit('committed', 'draft'); emit('withdrawn', 'draft'); await tick();
  let [withdrawn] = sections(root, 'Withdrawn draft');
  assert.ok(withdrawn, 'a withdrawn draft never disappears silently');
  assert.ok(textOf(withdrawn).startsWith('Draft withdrawn: a check asked for a fix, so this draft is not the reply.'));
  assert.ok(textOf(withdrawn).includes(hostile), 'its text stays visible as literal text');
  assert.ok(!textOf(withdrawn).includes('The repaired reply.'), 'the repair is not labelled withdrawn');
  assert.equal(all(withdrawn).filter(element => element.type === 'img').length, 0);
  assert.equal(sections(root, 'Live attempt output').length, 0, 'the committed repair leaves the live section');

  // 2. The announcement can arrive before the page with the settle; the draft is adopted when that page is read.
  pages.push([begin(7, 'second'), delta(8, 'second', 'Second draft.')]);
  emit('begin', 'second'); await tick();
  emit('withdrawn', 'second');
  assert.equal(sections(root, 'Withdrawn draft').length, 1, 'not yet settled: nothing is guessed');
  pages.push([committed(9, 'second')]);
  emit('committed', 'second'); await tick();
  assert.deepEqual(sections(root, 'Withdrawn draft').map(item => textOf(item).includes('Second draft.')), [false, true]);

  // 3. Another run's or conversation's announcement, and a committed attempt nobody withdrew, add nothing.
  pages.push([begin(10, 'kept'), delta(11, 'kept', 'A kept reply.'), committed(12, 'kept')]);
  emit('withdrawn', 'kept', 'run-2'); emit('withdrawn', 'kept', 'run-1', 'chat-b'); emit('committed', 'kept'); await tick();
  assert.equal(sections(root, 'Withdrawn draft').length, 2);
  assert.ok(!textOf(root).includes('A kept reply.'));

  // 4. Another run starts with nothing shown; only stream_read was called, always for the panel's own run.
  props.runId = 'run-2'; await settle();
  assert.equal(sections(root, 'Withdrawn draft').length, 0);
  assert.ok(calls.every(call => call.method === 'stream_read' && call.params.id === 'chat-a'));
  app.unmount();
  assert.deepEqual(errors, []);
  console.log('attempt stream panel: withdrawn drafts stay visible and labelled');
} finally {
  Object.assign(globalThis, previous);
}
