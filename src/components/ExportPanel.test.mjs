import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 17 track A2: exports are previewed, created, read back and saved only
// through a native destination token. No path is ever sent from this view, no
// share/upload/email/send control exists, and host text renders literally.
const require = createRequire(import.meta.url);
const calls = [], invokes = [], failures = new Map(), gates = new Map();
const answers = { render: [], destination: [] };
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const hostile = '<img src=x onerror=alert(1)>';
let previews = 0;
function answer(method, params) {
  if (method === 'export_preview') {
    previews++;
    return { plan_id: 'plan-' + previews, source: { kind: params.source.kind, id: params.source.id, revision: params.source.revision ?? null, digest: 'd'.repeat(64) },
      format: params.format, pages_estimate: 2, deadline: 1_900_000_000 };
  }
  if (method === 'export_render') return answers.render.shift();
  if (method === 'export_cancel') return true;
  if (method === 'export_save')
    return { path: '/Users/test/Documents/' + hostile + '.xlsx', sha256: 'ab12cd34ef56' + '0'.repeat(52), verified: answers.saveVerified, published: false };
  if (method === 'export_discard') return { export_id: params.export_id, status: 'discarded' };
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
const core = { async invoke(command, args) { invokes.push({ command, args: clone(args) }); return answers.destination.shift() ?? null; } };
function hold(method) { let release; gates.set(method, new Promise(resolve => { release = resolve; })); return () => release(); }
const receipt = (status, extra = {}) => ({
  export_id: 'export-' + status, format: 'xlsx', status, reason: null, source: { kind: 'artifact', id: 'view1', revision: 3, digest: 'd'.repeat(64) },
  file: { name: 'export.xlsx', suggested_name: 'Readings ' + hostile + '.xlsx', bytes: 4096, sha256: 'ab12cd34ef56' + '1'.repeat(52) },
  readback: { sheets: 1, text_coverage: status === 'verified' ? 1 : 0.933 },
  checks: [{ name: 'parsed', label: 'The file opens and parses', passed: true, detail: '' },
    { name: 'text_coverage', label: 'Text matches the source', passed: status === 'verified', detail: status === 'verified' ? '' : '93.3% found ' + hostile }],
  failed_checks: status === 'verified' ? [] : ['text_coverage'], notes: ['Spreadsheets hold only the tables from this source.'], published: false, ...extra });

const source = readFileSync(new URL('./ExportPanel.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'ExportPanel.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'export' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'ExportPanel.vue', id: 'export', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : name === '@tauri-apps/api/core' ? core : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;

// Static contract.
const markup = descriptor.template.content;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write|<a\b|href|mailto:/.test(source), 'host text renders as literal native content');
assert.ok(!/<textarea|contenteditable|type="(?:text|file|url|email)"/.test(markup), 'no path or address can be typed into this view');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.ok(methods.length && methods.every(method => /^'[a-z_]+'$/.test(method)), 'every native method name is a fixed literal');
assert.deepEqual([...new Set(methods)].sort(), ["'export_cancel'", "'export_discard'", "'export_preview'", "'export_render'", "'export_save'"]);
assert.deepEqual([...source.matchAll(/\binvoke\(([^,)]*)/g)].map(match => match[1].trim()), ["'export_choose_destination'"]);
assert.deepEqual([...source.matchAll(/from '([^']+)'/g)].map(match => match[1]).sort(), ['../bridge', '@tauri-apps/api/core', 'vue']);
assert.ok(!/share|upload|e-?mail|publish|send to|post to/i.test(markup.replace('Nothing is shared, uploaded or sent.', '').replace('not published', '')),
  'no publication control or wording beyond the explicit disclaimer');

function node(type, text = '') {
  const element = { type, text, children: [], props: {}, parent: null, listeners: {}, focus() {},
    addEventListener(name, handler) { this.listeners[name] = handler; }, removeEventListener(name) { delete this.listeners[name]; } };
  Object.defineProperty(element, 'options', { get() { return this.children.filter(child => child.type === 'option'); } });
  return element;
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
  patchProp(child, key, _previous, value) { child.props[key] = value; if (key === 'value') { child._value = value; child.value = value; } },
  parentNode: child => child.parent,
  nextSibling: child => child.parent?.children[child.parent.children.indexOf(child) + 1] || null,
});
const textOf = element => (element.type === '#comment' ? '' : element.text) + element.children.map(textOf).join('');
const all = element => [element, ...element.children.flatMap(all)];
async function settle() { for (let index = 0; index < 10; index++) { await Promise.resolve(); await nextTick(); } }
const events = [];
const props = reactive({ chatId: 'c'.repeat(32), source: { kind: 'artifact', id: 'view1', revision: 3, title: 'Readings ' + hostile, artifactKind: 'table' } });
const root = node('root');
const app = renderer.createApp({ setup: () => () => h(Panel, { ...props, onError: message => events.push('error:' + message) }) });
app.config.errorHandler = error => { throw error; };
const count = method => calls.filter(call => call.method === method).length;
const paramsFor = method => calls.filter(call => call.method === method).at(-1)?.params;
const region = label => all(root).find(element => element.props['aria-label'] === label);
const status = () => textOf(all(root).find(element => element.props.role === 'status'));
const buttons = () => all(root).filter(element => element.type === 'button');
const findButton = label => buttons().find(element => textOf(element) === label);
function button(label) { const found = findButton(label); assert.ok(found, 'Missing button ' + label); return found; }
async function click(label) {
  const target = button(label);
  assert.ok(!target.props.disabled, label + ' must be enabled');
  await target.props.onClick({});
  await settle();
}
async function choose(format) {
  const input = all(root).find(element => element.type === 'input' && element.props.type === 'radio' && element.props.value === format);
  input.checked = true;
  input.listeners.change({});
  await settle();
}
function noPublication() {
  for (const element of buttons()) assert.ok(!/share|upload|e-?mail|send|publish|post/i.test(textOf(element)), 'button ' + textOf(element));
}

app.mount(root);
await settle();
assert.equal(calls.length + invokes.length, 0, 'nothing is read or written until the person asks');
assert.ok(textOf(root).includes('Nothing is shared, uploaded or sent.'));
assert.equal(all(root).filter(element => element.props.type === 'radio').length, 5);
assert.equal(all(root).find(element => element.props.type === 'checkbox'), undefined, 'answers are offered only for quizzes');
assert.equal(status(), '');
noPublication();

// PDF preview, then an honest "unavailable" receipt with no save control.
await click('Preview export');
assert.deepEqual(paramsFor('export_preview'), { chat_id: 'c'.repeat(32), source: { kind: 'artifact', id: 'view1', revision: 3 }, format: 'pdf', options: { paper: 'a4' } });
let preview = textOf(region('Export preview'));
assert.ok(preview.includes('Readings ' + hostile) && preview.includes('PDF · revision 3 · about 2 page(s)'), preview);
assert.equal(status(), 'Preview ready. Nothing is written until you choose Create file.');
assert.equal(count('export_render'), 0, 'previewing writes nothing');
answers.render.push(receipt('unavailable', { format: 'pdf', file: null, readback: null, checks: [], failed_checks: [],
  reason: "PDF needs Pebble's native export view, which is not available in this build. Nothing was written." }));
await click('Create file');
assert.deepEqual(paramsFor('export_render'), { chat_id: 'c'.repeat(32), plan_id: 'plan-1' });
let result = textOf(region('Export result'));
assert.ok(result.includes('Unavailable · PDF') && result.includes('native export view'), result);
assert.equal(status(), 'Unavailable: nothing was written.');
assert.equal(region('Export preview'), undefined, 'a plan is used once');
assert.equal(findButton('Save to…'), undefined);
assert.equal(findButton('Save unverified file to…'), undefined);

// XLSX: an unverified receipt lists its failed check literally and can still be saved, labelled.
await choose('xlsx');
assert.equal(all(root).find(element => element.type === 'select'), undefined, 'paper is a PDF option');
await click('Preview export');
assert.deepEqual(paramsFor('export_preview').options, {});
assert.equal(paramsFor('export_preview').format, 'xlsx');
answers.render.push(receipt('unverified'));
await click('Create file');
result = textOf(region('Export result'));
for (const line of ['Unverified · XLSX', 'Did not pass: Text matches the source — 93.3% found ' + hostile, 'Passed: The file opens and parses',
  'Readings ' + hostile + '.xlsx · 4 KB · 1 sheet(s) · 93.3% of the source text found · revision 3 · SHA-256 ab12cd34ef56…',
  'Spreadsheets hold only the tables from this source.'])
  assert.ok(result.includes(line), line);
assert.ok(status().startsWith('Unverified: the file was made'));
assert.ok(!all(root).some(element => element.type === 'img'));
noPublication();

// Save: a cancelled native dialog sends nothing; a chosen destination arrives only as a token.
await click('Save unverified file to…');
assert.deepEqual(invokes.at(-1), { command: 'export_choose_destination', args: { chatId: 'c'.repeat(32), exportId: 'export-unverified',
  suggestedName: 'Readings ' + hostile + '.xlsx', extension: 'xlsx' } });
assert.equal(count('export_save'), 0, 'a cancelled dialog sends nothing');
assert.equal(status(), 'Nothing was saved.');
answers.destination.push({ destination_token: 'token-1' });
answers.saveVerified = false;
await click('Save unverified file to…');
assert.deepEqual(paramsFor('export_save'), { chat_id: 'c'.repeat(32), export_id: 'export-unverified', destination_token: 'token-1' });
assert.ok(calls.every(call => !('path' in call.params)), 'no path is ever sent from the webview');
let saved = textOf(region('Saved file'));
assert.ok(saved.includes('Saved to /Users/test/Documents/' + hostile + '.xlsx') && saved.includes('Unverified copy') && saved.includes('not published'), saved);
assert.equal(status(), 'Saved. This copy is unverified.');

// A refused save (for example a stale token) is surfaced and keeps the receipt.
answers.destination.push({ destination_token: 'token-2' });
failures.set('export_save', 'Choose where to save again with the Save dialog. Nothing was written.');
await click('Save unverified file to…');
assert.equal(events.at(-1), 'error:Error: Choose where to save again with the Save dialog. Nothing was written.');
assert.ok(region('Export result'));

// A verified export saves as a verified copy.
await click('Preview export');
answers.render.push(receipt('verified'));
await click('Create file');
assert.ok(textOf(region('Export result')).includes('Verified · XLSX'));
answers.destination.push({ destination_token: 'token-3' });
answers.saveVerified = true;
await click('Save to…');
assert.ok(textOf(region('Saved file')).includes('Verified copy'));
assert.equal(status(), 'Saved. The copy matches the verified export.');

// Deleting Pebble's copy leaves saved files alone.
await click("Delete Pebble's copy");
assert.deepEqual(paramsFor('export_discard'), { chat_id: 'c'.repeat(32), export_id: 'export-verified' });
assert.equal(region('Export result'), undefined);
assert.equal(status(), "Pebble's copy was deleted. Files you saved elsewhere are unchanged.");

// Cancel during creation goes to the host by plan ID.
await click('Preview export');
const release = hold('export_render');
answers.render.push(receipt('cancelled', { file: null, checks: [], failed_checks: [], readback: null, reason: 'Cancelled. The partial file was removed.' }));
const creating = button('Create file').props.onClick({});
await settle();
assert.equal(status(), 'Creating and checking the file…');
await click('Cancel');
assert.deepEqual(paramsFor('export_cancel'), { chat_id: 'c'.repeat(32), plan_id: 'plan-4' });
release();
await creating;
await settle();
assert.ok(textOf(region('Export result')).includes('Cancelled · XLSX'));
assert.equal(findButton('Save to…'), undefined);

// Revision drift drops an open plan, and a late preview reply is ignored.
await click('Preview export');
assert.ok(region('Export preview'));
props.source = { ...props.source, revision: 4 };
await settle();
assert.equal(region('Export preview'), undefined);
assert.equal(status(), 'This view changed to revision 4. Preview the export again.');
const late = hold('export_preview');
const asking = button('Preview export').props.onClick({});
await settle();
props.source = { ...props.source, revision: 5 };
await settle();
late();
await asking;
await settle();
assert.equal(region('Export preview'), undefined, 'a reply for an older revision is ignored');

// Quizzes offer the answer key, off by default; changing it drops the plan.
props.source = { kind: 'artifact', id: 'quiz1', revision: 1, title: 'Science check', artifactKind: 'quiz' };
await settle();
assert.equal(region('Export result'), undefined, 'another source starts clean');
await choose('html');
await click('Preview export');
assert.deepEqual(paramsFor('export_preview').options, { include_answers: false });
const answersBox = all(root).find(element => element.props.type === 'checkbox');
assert.ok(textOf(answersBox.parent).includes('Include the answer key'));
answersBox.checked = true;
answersBox.listeners.change({});
await settle();
assert.equal(region('Export preview'), undefined);
assert.equal(status(), 'Options changed. Preview the export again.');
await click('Preview export');
assert.deepEqual(paramsFor('export_preview').options, { include_answers: true });

// PDF paper choice reaches the plan.
await choose('pdf');
const select = all(root).find(element => element.type === 'select');
select.options.forEach(option => { option.selected = option._value === 'letter'; });
select.listeners.change({});
await settle();
await click('Preview export');
assert.deepEqual(paramsFor('export_preview').options, { include_answers: true, paper: 'letter' });

// Errors are surfaced, never turned into a receipt.
failures.set('export_preview', 'Documents hold up to 2,000 table rows. Export large tables as XLSX or CSV.');
await click('Preview export');
assert.equal(events.at(-1), 'error:Error: Documents hold up to 2,000 table rows. Export large tables as XLSX or CSV.');

// Outside the desktop app no native dialog opens.
answers.render.push(receipt('verified'));
await click('Preview export');
await click('Create file');
bridge.native = false;
const before = invokes.length;
await click('Save to…');
assert.equal(invokes.length, before);
assert.equal(events.at(-1), 'error:Error: Open the desktop app to save an export.');
bridge.native = true;
noPublication();
assert.deepEqual([...new Set(calls.map(call => call.method))].sort(), ['export_cancel', 'export_discard', 'export_preview', 'export_render', 'export_save']);
assert.deepEqual([...new Set(invokes.map(call => call.command))], ['export_choose_destination']);
app.unmount();
console.log('export panel: ok');
