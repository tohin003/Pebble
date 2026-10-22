import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick } from 'vue';
import { transform } from 'sucrase';

const require = createRequire(import.meta.url);

// Phase 14 P10: the output panel shows how many terminal control sequences the
// host projection hid, and keeps the sanitized text as literal content.
const hostile = '<img src=x onerror=alert(1)>';
const PROCESS = 'a'.repeat(32);
let pages = [
  { untrusted_process_output: 'build ok ' + hostile, next_after: 40, more: true, output_complete: false, controls_removed: 3, state: 'succeeded', outcome_uncertain: false },
  { untrusted_process_output: 'done', next_after: 44, more: false, output_complete: true, controls_removed: 0, state: 'succeeded', outcome_uncertain: false },
];
const calls = [], typedInput = [];
let terminalView = null, rows = null;
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: clone(params) });
    if (method === 'process_list') return rows || [{ id: PROCESS, run_id: 'run-1', state: 'succeeded', outcome_uncertain: false, inspection_recorded: false }];
    if (method === 'process_read') return clone(pages.shift());
    if (method === 'process_terminal') return clone(terminalView);
    if (method === 'process_input_human') { typedInput.push(clone(params)); return { delivered: true, origin: 'human' }; }
    throw new Error('Unexpected RPC ' + method);
  },
  async subscribe() { return () => {}; },
};
const source = readFileSync(new URL('./ExecutionOutput.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'ExecutionOutput.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'output' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'ExecutionOutput.vue', id: 'output', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Output = evaluate(script.content).default;
Output.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source), 'output renders as literal native content');

function node(type, text = '') {
  return { type, text, children: [], props: {}, parent: null, value: '', addEventListener() {}, removeEventListener() {},
           getRootNode() { return globalThis.document; }, focus() {} };
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
// v-model directives look up the active element through the root node.
globalThis.Document ||= class Document { activeElement = null; };
globalThis.ShadowRoot ||= class ShadowRoot {};
globalThis.document ||= new globalThis.Document();
const root = node('root');
const app = renderer.createApp({ setup: () => () => h(Output, { chatId: 'chat-a', runId: 'run-1' }) });
app.config.errorHandler = error => { throw error; };
const readButton = () => all(root).find(element => element.type === 'button' && /output/i.test(textOf(element)) && !/Forget|Stop/.test(textOf(element)));
app.mount(root);
await settle();
assert.equal(textOf(readButton()), 'Read output');
await readButton().props.onClick({});
await settle();
let page = textOf(root);
assert.ok(page.includes('Terminal control sequences hidden on this page: 3. The raw output bytes are kept.'));
assert.ok(page.includes('Saved output is a partial record of this command.'));
assert.ok(page.includes(hostile) && !all(root).some(element => element.type === 'img'), 'hostile output stays plain text');
assert.equal(textOf(readButton()), 'Next output page');
await readButton().props.onClick({});
await settle();
page = textOf(root);
assert.ok(!page.includes('Terminal control sequences hidden'), 'a page without hidden sequences shows no notice');
assert.ok(page.includes('done') && !page.includes('partial record'));
assert.deepEqual(calls.filter(call => call.method === 'process_read').map(call => call.params.after), [0, 40]);
app.unmount();

// Phase 14f: a live interactive job takes the person's typing; a password prompt is masked and labelled.
rows = [{ id: PROCESS, run_id: 'run-1', state: 'running', outcome_uncertain: false, inspection_recorded: false }];
terminalView = { interactive: true, live: true, echo: false };
const root2 = node('root');
const app2 = renderer.createApp({ setup: () => () => h(Output, { chatId: 'chat-a', runId: 'run-1' }) });
app2.config.errorHandler = error => { throw error; };
app2.mount(root2);
await settle();
let screen = textOf(root2);
assert.ok(screen.includes('Password prompt: what you type is sent to the command only.'), screen);
const field = all(root2).find(element => element.type === 'input');
assert.equal(field.props.type, 'password');
field.props['onUpdate:modelValue']('hunter2');
await settle();
await all(root2).find(element => element.type === 'form').props.onSubmit({ preventDefault() {} });
await settle();
assert.deepEqual(typedInput.slice(0, 2), [
  { id: 'chat-a', run_id: 'run-1', process_id: PROCESS, text: 'hunter2' },
  { id: 'chat-a', run_id: 'run-1', process_id: PROCESS, key: 'enter' }]);
terminalView = { interactive: true, live: true, echo: true };
await all(root2).find(element => element.type === 'button' && textOf(element) === 'Send Ctrl-C').props.onClick({});
await settle();
assert.deepEqual(typedInput.at(-1), { id: 'chat-a', run_id: 'run-1', process_id: PROCESS, key: 'ctrl_c' });
assert.equal(all(root2).find(element => element.type === 'input').props.type, 'text');
assert.ok(!textOf(root2).includes('Password prompt'));
app2.unmount();
console.log('execution output: ok');
