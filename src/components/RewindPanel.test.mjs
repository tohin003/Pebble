import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 20 (G20-rewind): the rewind panel reaches only the preview and apply routes, applies exactly
// the digest it previewed, never names a window, lists every non-file effect as not reverted, and
// renders paths, diffs and labels as literal text.
const require = createRequire(import.meta.url);
const calls = [], failures = new Map(), held = new Map();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const hostile = '<img src=x onerror=alert(1)>';
const DIGESTS = { 0: 'a'.repeat(64), 1: 'b'.repeat(64), 3: 'c'.repeat(64) };
let applied = false;

function preview(step) {
  const files = step >= 3 ? [] : [
    { path: 'src/app.py', status: applied ? 'unchanged' : 'revert', reason: null, detail: applied ? 'Already as it was after that step.' : null,
      steps: [2], diff: applied ? '' : '--- a/src/app.py (now)\n+++ b/src/app.py (after rewind)\n-print(2)\n+print(1)\n', diff_truncated: false },
    { path: hostile, status: 'refused', reason: 'changed_since', detail: 'Changed after this task\'s last write.', steps: [2], diff: '', diff_truncated: false },
    { path: 'notes/new.md', status: 'kept', reason: 'created', detail: 'A rewind never deletes files.', steps: [2], diff: '', diff_truncated: false },
  ];
  const effects = step >= 3 ? [] : [
    { kind: 'command', tool: 'RunCommand', step: 2, label: 'touch build.stamp', outcome: 'occurred', outcome_text: 'It happened.',
      note: 'Not reverted. Commands are never undone or run again.', connector_class: null, reverted: false },
    { kind: 'connector', tool: 'PluginCall', step: 3, label: 'Mail ' + hostile, outcome: 'occurred', outcome_text: 'It happened.',
      note: 'Not reverted. It was sent outside this computer.', connector_class: 'send', reverted: false },
  ].filter(effect => effect.step > step);
  return {
    chat_id: 'chat-a', run_id: 'run-1', step, steps: 3, state: 'completed', digest: DIGESTS[step] || 'd'.repeat(64),
    files, effects, effects_omitted: step === 0 ? 4 : 0,
    timeline: [{ step: 1, files: ['src/app.py'], effects: 0 }, { step: 2, files: ['src/app.py', hostile], effects: 1 }, { step: 3, files: [], effects: 1 }],
    rewinds: applied ? [{ rewind_id: 'r'.repeat(32), step: 1, previous_step: 3, reverses: null, files: 1, applied: 1 }] : [],
    previous_step: applied ? 1 : 3, reverses: applied && step === 3 ? 'r'.repeat(32) : null, authorization: 'none',
  };
}
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: clone(params) });
    const error = failures.get(method);
    failures.delete(method);
    if (error) throw new Error(error);
    if (held.has(method)) { const gate = held.get(method); held.delete(method); await gate; }
    if (method === 'rewind_preview') return preview(params.step);
    if (method === 'rewind_apply') {
      applied = true;
      return { rewind_id: 'r'.repeat(32), step: params.step, files: [
        { path: 'src/app.py', status: 'reverted', detail: null, verified: true },
        { path: hostile, status: 'refused', detail: 'Changed after this task\'s last write.', verified: false }],
        effects: preview(params.step).effects, effects_reverted: 0, replayed: 0, authorization: 'none' };
    }
    throw new Error('Unexpected RPC ' + method);
  },
};
const source = readFileSync(new URL('./RewindPanel.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'RewindPanel.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'rewind' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'RewindPanel.vue', id: 'rewind', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source), 'paths, diffs and labels render as literal text');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(\s*([^,)]*)/g)].map(match => match[1].trim());
assert.deepEqual([...new Set(methods)].sort(), ["'rewind_apply'", "'rewind_preview'"], 'only the preview and apply routes are reachable');
assert.ok(!/surface/.test(source.slice(source.indexOf('<script'), source.indexOf('</script>'))), 'the shell, never the panel, names the window');

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
async function settle() { for (let index = 0; index < 12; index++) { await Promise.resolve(); await nextTick(); } }
const events = [];
const props = reactive({ chatId: 'chat-a', runId: 'run-1' });
const root = node('root');
const app = renderer.createApp({ setup: () => () => h(Panel, { ...props, onRefresh: () => events.push('refresh'), onError: message => events.push('error:' + message) }) });
app.config.errorHandler = error => { throw error; };
const buttons = () => all(root).filter(element => element.type === 'button');
const button = label => { const found = buttons().find(element => textOf(element).trim() === label); assert.ok(found, 'Missing button ' + label); return found; };
const select = () => all(root).find(element => element.type === 'select');
const count = method => calls.filter(call => call.method === method).length;
const last = method => calls.filter(call => call.method === method).at(-1).params;

// Mounting previews the whole task (step 0), with no window named in the request.
app.mount(root);
await settle();
assert.equal(count('rewind_preview'), 1);
assert.deepEqual(calls[0].params, { chat_id: 'chat-a', run_id: 'run-1', step: 0 });
let page = textOf(root);
assert.ok(page.includes('never undone or run again') && page.includes('a rewind never deletes a file'));
assert.ok(page.includes('src/app.py') && page.includes('Will be restored') && page.includes('-print(2)\n+print(1)'));
assert.ok(page.includes(hostile + ' Left alone') && page.includes('Changed after this task\'s last write.'));
assert.ok(page.includes('notes/new.md') && page.includes('Kept, never deleted') && page.includes('Undoes step 2'));
assert.ok(page.includes('Not reverted') && page.includes('touch build.stamp') && page.includes('Mail ' + hostile));
assert.ok(page.includes('Step 2 · It happened.') && page.includes('Step 3 · It happened. · send') && page.includes('And 4 more, also not reverted.'));
assert.ok(!all(root).some(element => element.type === 'img'), 'a hostile path or label stays plain text');
const diff = all(root).find(element => element.type === 'pre');
assert.equal(diff.props['aria-label'], 'Changes to src/app.py');
assert.equal(all(root).filter(element => element.type === 'pre').length, 1, 'only files to restore show a diff');
assert.deepEqual(all(select()).filter(e => e.type === 'option').map(e => textOf(e)),
  ['Before this task', 'After step 1 (wrote src/app.py)', 'After step 2 (wrote src/app.py, ' + hostile + '; 1 other action)', 'After step 3 (1 other action)']);
assert.equal(button('Rewind 1 file').props.disabled, false);
assert.ok(!buttons().some(element => textOf(element).includes('undoing')), 'no rewind yet, nothing to undo');

// Choosing a step re-previews it. While a preview loads nothing else can be chosen, and an older,
// slower preview (here: of the task the person just left) never overwrites the newer one.
let release;
held.set('rewind_preview', new Promise(resolve => { release = resolve; }));
select().props.onChange({ target: { value: '1' } });
await settle();
assert.equal(select().props.disabled, true);
assert.equal(button('Rewind 0 files').props.disabled, true, 'nothing to apply while a preview loads');
props.runId = 'run-2';
await settle();
release();
await settle();
assert.deepEqual(calls.filter(c => c.method === 'rewind_preview').slice(-2).map(c => [c.params.run_id, c.params.step]),
  [['run-1', 1], ['run-2', 0]]);
assert.equal(select().props.value, 0);
assert.ok(textOf(root).includes('And 4 more, also not reverted.'), 'the step-0 preview of the newer task is shown');
props.runId = 'run-1';
await settle();
select().props.onChange({ target: { value: '3' } });
await settle();
page = textOf(root);
assert.ok(page.includes('This task wrote no file after that step.') && page.includes('No command, connector or other action after that step.'));
assert.equal(button('Rewind 0 files').props.disabled, true);
assert.equal(select().props.value, 3);

// Rewind applies exactly the previewed step and digest, shows the readback and re-previews.
select().props.onChange({ target: { value: '1' } });
await settle();
assert.equal(last('rewind_preview').step, 1);
await button('Rewind 1 file').props.onClick({});
await settle();
assert.deepEqual(last('rewind_apply'), { chat_id: 'chat-a', run_id: 'run-1', step: 1, digest: 'b'.repeat(64) });
assert.deepEqual(events, ['refresh']);
assert.equal(last('rewind_preview').step, 1, 'a preview is single-use: the panel shows the files as they are now');
page = textOf(root);
assert.ok(page.includes('Restored 1 file.') && page.includes('src/app.py: restored and read back'));
assert.ok(page.includes(hostile + ': left alone. Changed after this task'));
assert.ok(page.includes('Already as it was') && button('Rewind 0 files').props.disabled === true);

// Undoing the last rewind is only a preview of the step it started from, never an apply.
const applies = count('rewind_apply');
await button('Review undoing the last rewind').props.onClick({});
await settle();
assert.equal(last('rewind_preview').step, 3);
assert.equal(count('rewind_apply'), applies);
assert.ok(textOf(root).includes('This undoes the last rewind.'));
assert.ok(!textOf(root).includes('restored and read back'), 'choosing another step clears the old result');

// A refused apply is reported and the panel re-previews instead of keeping a stale digest.
applied = false;
select().props.onChange({ target: { value: '0' } });
await settle();
failures.set('rewind_apply', 'The files, effects or permissions changed since this preview. Nothing was written.');
const previews = count('rewind_preview');
await button('Rewind 1 file').props.onClick({});
await settle();
assert.equal(events.at(-1), 'error:Error: The files, effects or permissions changed since this preview. Nothing was written.');
assert.equal(count('rewind_preview'), previews + 1);

// A malformed preview is an error, never an applicable plan.
const original = bridge.rpc;
bridge.rpc = async (method, params) => method === 'rewind_preview' ? { files: [], digest: 'not-a-digest', step: 0 } : original(method, params);
await button('Review again').props.onClick({});
await settle();
assert.ok(events.at(-1).startsWith('error:Error: Pebble could not prepare this rewind.'));
assert.ok(!buttons().some(element => textOf(element).trim().startsWith('Rewind') && element.props.disabled === false));
bridge.rpc = original;

// Another task resets to the whole-task preview.
props.runId = 'run-2';
await settle();
assert.deepEqual(last('rewind_preview'), { chat_id: 'chat-a', run_id: 'run-2', step: 0 });
assert.ok(calls.every(call => !('surface' in call.params)), 'no request ever names a window');
app.unmount();
console.log('rewind panel: ok');
