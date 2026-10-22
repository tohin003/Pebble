import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 15: archive paths come only from the native dialogs, an import needs a
// preview of that exact plan first, refusals keep the preview, and archive
// text renders literally. Only the three archive routes are reachable.
const require = createRequire(import.meta.url);
const calls = [], failures = new Map(), gates = new Map(), dialogCalls = [];
const answers = { save: [], open: [] };
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const hostile = '<img src=x onerror=alert(1)>';
const COUNTS = { add: 5, skip_disabled: 2, skip_tombstoned: 1, skip_duplicate: 3, revalidate: 1, expired: 1, tombstones: 4 };
let localVersion = 1, draftShape = false;
function answer(method, params) {
  if (method === 'memory_archive_export_to')
    return { path: params.path, sha256: 'ab12cd34ef56' + '0'.repeat(52), bytes: 3 * 1024, counts: { records: 12, relations: 3, tombstones: 4, scopes: 2 }, authorization: 'none' };
  if (method === 'memory_archive_preview_file')
    return { plan_digest: 'digest-' + localVersion, counts: { ...COUNTS }, format: params.path.includes('evil') ? hostile : 'pebble-memory-archive/1', authorization: 'none' };
  if (method === 'memory_archive_import_file') {
    if (params.plan_digest !== 'digest-' + localVersion)
      throw new Error('The memory archive was refused (stale_preview: local memory changed after the preview). Nothing was imported.');
    localVersion++;
    // The draft host route returns the archive layer's own result; the panel reads both shapes.
    return { applied: draftShape ? { imported: 2, skipped: { already_present: 1 }, tombstones: { rows: 1, filters: 0 }, forgotten: 1, needs_revalidation: 0 } : { ...COUNTS },
      authorization: 'none' };
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
const dialog = {
  async save(options) { dialogCalls.push({ kind: 'save', options: clone(options) }); return answers.save.shift() ?? null; },
  async open(options) { dialogCalls.push({ kind: 'open', options: clone(options) }); return answers.open.shift() ?? null; },
};
function hold(method) { let release; gates.set(method, new Promise(resolve => { release = resolve; })); return () => release(); }

const source = readFileSync(new URL('./MemoryArchive.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'MemoryArchive.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'archive' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'MemoryArchive.vue', id: 'archive', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : name === '@tauri-apps/plugin-dialog' ? dialog : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(source), 'archive text renders as literal native content');
assert.ok(!/<input|<textarea|contenteditable/.test(source), 'archive paths come only from the native dialogs');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.ok(methods.length && methods.every(method => /^'[a-z_]+'$/.test(method)), 'every native method name is a fixed literal');
assert.deepEqual([...new Set(methods)].sort(), ["'memory_archive_export_to'", "'memory_archive_import_file'", "'memory_archive_preview_file'"], 'only the archive routes are reachable');
assert.deepEqual([...source.matchAll(/from '([^']+)'/g)].map(match => match[1]).sort(), ['../bridge', '@tauri-apps/plugin-dialog', 'vue']);

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
const props = reactive({ active: false });
const root = node('root');
const app = renderer.createApp({ setup: () => () => h(Panel, { ...props, onChanged: () => events.push('changed'), onError: message => events.push('error:' + message) }) });
app.config.errorHandler = error => { throw error; };
const count = method => calls.filter(call => call.method === method).length;
const paramsFor = method => calls.filter(call => call.method === method).at(-1)?.params;
const region = label => all(root).find(element => element.props['aria-label'] === label);
const status = () => all(root).find(element => element.props.role === 'status');
const findButton = label => all(root).find(element => element.type === 'button' && textOf(element) === label);
function button(label) { const found = findButton(label); assert.ok(found, 'Missing button ' + label); return found; }
async function click(label) {
  const target = button(label);
  assert.ok(!target.props.disabled, label + ' must be enabled');
  await target.props.onClick({});
  await settle();
}

app.mount(root);
await settle();
assert.equal(calls.length + dialogCalls.length, 0, 'nothing is read or opened until the person asks');
let page = textOf(root);
assert.ok(page.includes('Forgotten memories stay forgotten: an archive cannot bring them back'));
assert.ok(page.includes('Layers that are off here are not filled'));
assert.ok(page.includes('The file is plain JSON and is not encrypted.'));
assert.equal(findButton('Import this archive'), undefined, 'importing needs a preview first');
assert.equal(status().props['aria-live'], 'polite');

// Export: a cancelled save dialog sends nothing; a chosen path is exported once.
answers.save.push(null);
await click('Export memories…');
assert.equal(dialogCalls.at(-1).kind, 'save');
assert.deepEqual(dialogCalls.at(-1).options.filters, [{ name: 'Pebble memory archive', extensions: ['json'] }]);
assert.equal(count('memory_archive_export_to'), 0, 'a cancelled dialog sends nothing');
const savePath = '/Users/test/Documents/' + hostile + '.json';
answers.save.push(savePath);
await click('Export memories…');
assert.deepEqual(paramsFor('memory_archive_export_to'), { path: savePath });
let saved = textOf(region('Saved archive'));
assert.ok(saved.includes('Saved to ' + savePath), 'a hostile path stays plain text');
for (const line of ['Memories: 12', 'Links between memories: 3', 'Forgetting records: 4', 'Scopes: 2', '3 KB · SHA-256 ab12cd34ef56…'])
  assert.ok(saved.includes(line), line);
assert.equal(textOf(status()), 'Memory archive saved.');
assert.ok(!all(root).some(element => element.type === 'img'));

// A refused export (an existing file) is surfaced and keeps the last result.
answers.save.push('/Users/test/Documents/existing.json');
failures.set('memory_archive_export_to', 'That file already exists; choose a new name. Nothing was overwritten.');
await click('Export memories…');
assert.equal(events.at(-1), 'error:Error: That file already exists; choose a new name. Nothing was overwritten.');
assert.ok(textOf(region('Saved archive')).includes('Saved to ' + savePath), 'a refused export keeps its state');

// Import: the open dialog leads only to a preview, shown in plain words.
const archivePath = '/Users/test/Downloads/evil-' + hostile + '.json';
answers.open.push(archivePath);
await click('Choose an archive to import…');
assert.equal(dialogCalls.at(-1).kind, 'open');
assert.equal(dialogCalls.at(-1).options.multiple, false);
assert.deepEqual(paramsFor('memory_archive_preview_file'), { path: archivePath });
assert.equal(count('memory_archive_import_file'), 0, 'previewing imports nothing');
let shown = textOf(region('Import preview'));
for (const line of ['To be added: 5', 'Skipped because their layer is off: 2', 'Skipped because you forgot them: 1',
  'Skipped because they are already here: 3', 'Need revalidation before Pebble relies on them: 1', 'Skipped because they expired: 1',
  'Forgetting records (tombstones): 4', 'Nothing has been imported yet.'])
  assert.ok(shown.includes(line), line);
assert.ok(shown.includes(archivePath) && shown.includes('Format: ' + hostile), 'archive text stays plain text');
assert.ok(!all(root).some(element => element.type === 'img'));
assert.equal(textOf(status()), 'Archive checked. Nothing is imported until you choose Import.');

// Local memory changed after the preview: the host refuses and the preview stays.
localVersion++;
await click('Import this archive');
assert.deepEqual(paramsFor('memory_archive_import_file'), { path: archivePath, plan_digest: 'digest-1' });
assert.ok(events.at(-1).startsWith('error:Error: The memory archive was refused (stale_preview'));
assert.ok(region('Import preview') && findButton('Import this archive'), 'a refused import keeps its preview');
assert.ok(!events.includes('changed'));

await click('Check this file again');
assert.deepEqual(paramsFor('memory_archive_preview_file'), { path: archivePath });
await click('Import this archive');
assert.deepEqual(paramsFor('memory_archive_import_file'), { path: archivePath, plan_digest: 'digest-2' });
assert.ok(events.includes('changed'));
assert.equal(region('Import preview'), undefined, 'a reviewed plan is used once');
let result = textOf(region('Import result'));
for (const line of ['Added: 5', 'Need revalidation before Pebble relies on them: 1', 'Forgetting records applied: 4'])
  assert.ok(result.includes(line), line);
assert.equal(textOf(status()), 'Import finished.');

// The draft host result shape is read too.
draftShape = true;
answers.open.push('/Users/test/Downloads/second.json');
await click('Choose an archive to import…');
await click('Import this archive');
result = textOf(region('Import result'));
for (const line of ['Added: 2', 'Need revalidation before Pebble relies on them: 0', 'Forgetting records applied: 1', 'Memories on this device forgotten by those records: 1'])
  assert.ok(result.includes(line), line);

// Cancelling a preview imports nothing.
answers.open.push('/Users/test/Downloads/third.json');
await click('Choose an archive to import…');
let imports = count('memory_archive_import_file');
await click('Cancel');
assert.equal(region('Import preview'), undefined);
assert.equal(count('memory_archive_import_file'), imports);
assert.equal(textOf(status()), 'Nothing was imported.');

// A preview reply that returns after the task state changed is ignored.
answers.open.push('/Users/test/Downloads/fourth.json');
const release = hold('memory_archive_preview_file');
const chooser = button('Choose an archive to import…');
const late = chooser.props.onClick({});
await settle();
props.active = true;
await settle();
assert.ok(textOf(root).includes('Importing waits until the current task finishes.'));
release();
await late;
await settle();
assert.equal(region('Import preview'), undefined, 'a stale preview is ignored');
assert.equal(chooser.props.disabled, true, 'importing waits for the task');
props.active = false;
await settle();

// An open preview closes when a task starts, since local memory may change.
answers.open.push('/Users/test/Downloads/fifth.json');
await click('Choose an archive to import…');
assert.ok(region('Import preview'));
props.active = true;
await settle();
assert.equal(region('Import preview'), undefined);
assert.equal(textOf(status()), 'The preview was closed because local memory may have changed. Check the archive again.');
assert.equal(findButton('Import this archive'), undefined, 'a closed preview cannot be imported');
props.active = false;
await settle();

// Outside the desktop app no dialog opens.
bridge.native = false;
const opened = dialogCalls.length;
await click('Export memories…');
assert.equal(dialogCalls.length, opened);
assert.equal(events.at(-1), 'error:Error: Open the desktop app to save a memory archive.');
bridge.native = true;
assert.deepEqual([...new Set(calls.map(call => call.method))].sort(), ['memory_archive_export_to', 'memory_archive_import_file', 'memory_archive_preview_file']);
app.unmount();
console.log('memory archive: ok');
