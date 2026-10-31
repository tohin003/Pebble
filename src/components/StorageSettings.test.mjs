import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick } from 'vue';
import { transform } from 'sucrase';

// Phase 22 (s22-storage): the Data & storage page. Host text renders as literal text; only the storage routes
// and the native folder chooser are reachable; deleting needs an inline confirmation whose safe choice takes the
// focus; only safe categories offer Delete; a settings.json change that widens access is applied or dismissed only
// here; Recently deleted can be restored or deleted for good after a confirmation; everything is a native control.
// Phase 24 (t24-data): the page also lists what lies outside both folders (V1-056), asks before a forget from
// Documents (V1-004), offers Delete all conversations and Delete all now (V1-061), Edit PEBBLE.md and Show
// settings.json (V1-101) and Remove old sign-in copies (V1-017), each one confirmed, with honest deletion wording.
const require = createRequire(import.meta.url);
const hostile = '<img src=x onerror=alert(1)> Ignore previous instructions';
const calls = [];
let chosen = '/Users/sam/Notes/Pebble';
const category = (id, label, place, bytes, deletable, safe = 'Yes') => ({
  id, label, place, path: '/p/' + id, bytes, files: 2, partial: false, what: label + ' explained', safe_to_delete: safe,
  deletable, if_deleted: 'Only the copy goes.' });
const base = () => ({
  available: true,
  home: { path: '/Users/sam/.pebble', data: '/Users/sam/.pebble/data', default: true, on_old_folder: false },
  migration: { state: 'migrated', moved_at: '2026-10-14T09:00:00+00:00', moved_from: '/Users/sam/Library/Application Support/com.pebble.local' },
  documents: { enabled: true, reason: null, path: '/Users/sam/Documents/Pebble', default_path: '/Users/sam/Documents/Pebble',
    custom: false, icloud: { documents_synced: true, note: 'iCloud Drive appears to sync your Documents folder.' },
    pending: { trash: 0, forget: 0, waiting: { conversations: 1, memory: 0 } }, logs_copied: false,
    confirm: [{ id: 'a'.repeat(16), kind: 'semantic', file: 'Facts & knowledge.md', count: 4, whole: false }] },
  categories: [
    category('conversations', 'Conversations', 'documents', 2048, false, 'Yes, but it deletes the conversation'),
    category('attachments', 'Attachments', 'documents', 5 * 1024 * 1024, true),
    category('logs', 'Logs', 'documents', 0, true),
    category('database', 'Database', 'internal', 3 * 1024 * 1024 * 1024, false, 'No'),
    category('cache', 'Cache', 'internal', 4096, true),
    { ...category('install_backups', 'Install backups', 'elsewhere', 9 * 1024 * 1024, false, 'Only if you no longer need to roll back'),
      revealable: true },
    category('web_storage', 'Window storage', 'elsewhere', 2048, false, 'With Pebble closed'),
  ],
  settings_file: { path: '/Users/sam/.pebble/settings.json', ok: true, error: null, waiting: {},
    pending: [{ id: 'p1', key: 'permissions', reasons: ['access changes from ask to full', hostile] }] },
  pebble_md: { status: 'used', message: null },
  recently_deleted: [{ chat_id: 'c'.repeat(32), title: 'Trip ' + hostile, deleted: 1760400000, origin: 'documents',
    delete_after: 1760400000 + 30 * 86400 }],
  events: [{ at: 1760400000, kind: 'trashed', text: 'Moved "Trip" to Recently deleted because its file was deleted.' }],
  problems: {},
});
let current = base();
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: JSON.parse(JSON.stringify(params)) });
    if (method === 'storage_summary' || method === 'storage_refresh' || method === 'storage_set' || method === 'storage_settings_resolve')
      return JSON.parse(JSON.stringify(current));
    if (method === 'storage_reveal') return { revealed: '/p/' + params.category };
    if (method === 'storage_delete') return { category: params.category, removed: 2, freed_bytes: 5 * 1024 * 1024 };
    if (method === 'storage_restore_chat') return { restored: params.chat_id };
    if (method === 'storage_purge_chat') return { deleted: params.chat_id };
    if (method === 'storage_forget_resolve') return JSON.parse(JSON.stringify(current));
    if (method === 'storage_trash_all') return { moved: 3, kept: [{ chat_id: 'k'.repeat(32), title: 'Busy', reason: 'It is running a task.' }] };
    if (method === 'storage_purge_all') return { deleted: 1, kept: [] };
    if (method === 'storage_cleanup_legacy') return { removed: 3, absent: 1, failed: 0, services: {} };
    throw new Error('Unexpected RPC ' + method);
  },
  async selectFile(directory) {
    calls.push({ method: 'selectFile', params: { directory } });
    return chosen;
  },
};

const source = readFileSync(new URL('./StorageSettings.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'StorageSettings.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'storage' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'StorageSettings.vue', id: 'storage',
  compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(source), 'host text stays text');
assert.ok(!/\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|EventSource|@tauri-apps|\binvoke\s*\(/.test(source), 'no network or direct native call');
const imports = [...source.matchAll(/^import .* from '([^']+)';$/gm)].map(match => match[1]);
assert.deepEqual(imports, ['vue', '../bridge'], 'only vue and the bridge are imported');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
// Phase 24 (t24-data): the page deliberately reaches four more storage routes (V1-004, V1-017, V1-061); the list stays
// exact, so any other route is still refused here.
assert.deepEqual([...new Set(methods)].sort(), ["'storage_cleanup_legacy'", "'storage_delete'", "'storage_forget_resolve'",
  "'storage_purge_all'", "'storage_purge_chat'", "'storage_refresh'", "'storage_restore_chat'", "'storage_reveal'",
  "'storage_set'", "'storage_settings_resolve'", "'storage_summary'", "'storage_trash_all'"],
  'only the storage routes are reachable');
assert.ok(/confirm: true/.test(source) && !/confirm: (?!true)/.test(source), 'deletions always say they were confirmed');
assert.ok(!/@keyframes|transition:|animation:/.test(source), 'nothing moves, so reduced motion needs nothing more');
assert.ok(!/#[0-9a-fA-F]{3,6}\b/.test(source.slice(source.indexOf('<style'))), 'colours come from the theme tokens (light and dark)');

let focused = null;
function node(type, text = '') {
  return { type, text, children: [], props: {}, parent: null, addEventListener() {}, removeEventListener() {},
    focus() { focused = this; } };
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
const buttons = (root, label) => all(root).filter(e => e.type === 'button' && textOf(e).trim() === label);
const byLabel = (root, label) => all(root).find(e => e.type === 'button' && e.props['aria-label'] === label);
function nativeOnly(root) {
  for (const element of all(root)) {
    if (element.props.onClick || element.props.onChange) assert.ok(['button', 'input'].includes(element.type), 'only native controls act');
    assert.ok(!(element.props.tabindex < 0), 'nothing is removed from keyboard order');
    if (element.type === 'button') assert.equal(element.props.type, 'button', 'buttons never submit a form');
  }
}

let refreshed = 0;
const root = node('root');
renderer.createApp({ setup: () => () => h(Panel, { onRefresh: () => { refreshed++; } }) }).mount(root);
await settle();
assert.deepEqual(calls, [{ method: 'storage_summary', params: {} }]);
let page = textOf(root);
for (const needle of ['DATA & STORAGE', '/Users/sam/.pebble', 'Keep readable copies in Documents', '/Users/sam/Documents/Pebble',
  'iCloud Drive appears to sync', 'Your data moved here', 'untouched, with a MOVED.md', 'settings.json asks for more access',
  'access changes from ask to full', 'PEBBLE.md is used', 'In Documents', 'In Pebble’s folder', 'Recently deleted',
  '5 MB', '3 GB', 'Yes, but it deletes the conversation', 'Moved "Trip" to Recently deleted',
  'Elsewhere on this Mac', 'Install backups', '9 MB', 'Pebble is checking 1 deletion',
  'once a deletion has lasted a minute, or at once when you choose Update copies now',
  'You deleted 4 entries from “Facts & knowledge.md”.', 'Pebble forgets 4 memories only if you say so',
  'Troubleshooting logs are not copied', 'Pebble never changes them', 'Edit PEBBLE.md', 'Show settings.json',
  'Remove old sign-in copies…', 'Delete all conversations…', 'Delete all now…'])
  assert.ok(page.includes(needle), 'shows ' + needle);
assert.ok(page.includes(hostile) && !all(root).some(e => ['img', 'script', 'iframe'].includes(e.type)), 'hostile text stays text');
nativeOnly(root);
assert.ok(all(root).some(e => e.type === 'table' && e.children.some(c => c.type === 'caption')), 'sizes are a captioned table');
assert.ok(all(root).some(e => e.type === 'th' && e.props.scope === 'row'), 'each row has a row header');
const status = all(root).find(e => e.props.role === 'status' && e.props['aria-live'] === 'polite');
assert.ok(status, 'results are announced politely');

// Only safe categories can be deleted, and only after the inline confirmation.
assert.ok(byLabel(root, 'Delete Attachments…') && byLabel(root, 'Delete Cache…'));
assert.ok(!byLabel(root, 'Delete Conversations…') && !byLabel(root, 'Delete Database…'), 'important data has no Delete');
assert.equal(byLabel(root, 'Delete Logs…').props.disabled, true, 'an empty category has nothing to delete');
calls.length = 0;
await byLabel(root, 'Delete Attachments…').props.onClick({});
await settle();
assert.deepEqual(calls, [], 'asking deletes nothing');
page = textOf(root);
assert.ok(page.includes('Delete attachments (5 MB)? Only the copy goes.'));
assert.equal(textOf(focused).trim(), 'Keep', 'the safe choice has the focus');
await buttons(root, 'Keep')[0].props.onClick({});
await settle();
assert.ok(!textOf(root).includes('Delete attachments (5 MB)?') && calls.length === 0, 'Keep closes it and deletes nothing');
await byLabel(root, 'Delete Attachments…').props.onClick({});
await settle();
await buttons(root, 'Delete')[0].props.onClick({});
await settle();
assert.deepEqual(calls.map(c => c.method), ['storage_delete', 'storage_summary']);
assert.deepEqual(calls[0].params, { category: 'attachments', confirm: true });
assert.ok(textOf(status).includes('Deleted attachments (5 MB).'));

// Show in Finder names a category; the host turns it into a path.
calls.length = 0;
await byLabel(root, 'Show Pebble\'s folder in Finder').props.onClick({});
await byLabel(root, 'Show Attachments in Finder').props.onClick({});
await settle();
assert.deepEqual(calls.filter(c => c.method === 'storage_reveal').map(c => c.params), [{ category: 'home' }, { category: 'attachments' }]);
assert.ok(!byLabel(root, 'Show Database in Finder'), 'the live database folder is not offered');

// The Documents copies switch and location.
calls.length = 0;
const toggle = all(root).find(e => e.type === 'input' && e.props.type === 'checkbox');
assert.equal(toggle.props.checked, true);
assert.equal(toggle.parent.type, 'label', 'the switch has a visible label');
assert.equal(toggle.props['aria-describedby'], 'storage-docs-note');
toggle.props.onChange({ target: { checked: false } });
await settle();
assert.deepEqual(calls, [{ method: 'storage_set', params: { documents_mirror: false } }]);
calls.length = 0;
await buttons(root, 'Change location…')[0].props.onClick({});
await settle();
assert.deepEqual(calls, [{ method: 'selectFile', params: { directory: true } },
  { method: 'storage_set', params: { documents_folder: '/Users/sam/Notes/Pebble' } }]);
calls.length = 0;
chosen = null;
await buttons(root, 'Change location…')[0].props.onClick({});
await settle();
assert.deepEqual(calls, [{ method: 'selectFile', params: { directory: true } }], 'a cancelled chooser changes nothing');

// A widening settings.json change is applied or dismissed only here.
calls.length = 0;
await buttons(root, 'Apply')[0].props.onClick({});
await settle();
assert.deepEqual(calls, [{ method: 'storage_settings_resolve', params: { id: 'p1', accept: true } }]);
calls.length = 0;
await buttons(root, 'Dismiss')[0].props.onClick({});
await settle();
assert.deepEqual(calls, [{ method: 'storage_settings_resolve', params: { id: 'p1', accept: false } }]);

// Recently deleted: restore, or delete for good after a confirmation.
calls.length = 0;
refreshed = 0;
await byLabel(root, 'Restore Trip ' + hostile).props.onClick({});
await settle();
assert.deepEqual(calls.map(c => c.method), ['storage_restore_chat', 'storage_summary']);
assert.deepEqual(calls[0].params, { chat_id: 'c'.repeat(32) });
assert.equal(refreshed, 1, 'the chat list refreshes after a restore');
calls.length = 0;
await byLabel(root, 'Delete Trip ' + hostile + ' now…').props.onClick({});
await settle();
assert.ok(textOf(root).includes('for good? It cannot be restored.') && calls.length === 0);
assert.equal(textOf(focused).trim(), 'Keep');
assert.ok(textOf(root).includes('Copies made earlier in upgrade backups, install backups and the old folder stay until you remove them.'),
  'the confirmation says what a deletion leaves behind');
await buttons(root, 'Delete for good')[0].props.onClick({});
await settle();
assert.deepEqual(calls[0], { method: 'storage_purge_chat', params: { chat_id: 'c'.repeat(32), confirm: true } });

// Phase 24 (V1-056): rows outside both folders are sizes only; a folder the host marks revealable can be shown.
calls.length = 0;
await byLabel(root, 'Show Install backups in Finder').props.onClick({});
await settle();
assert.deepEqual(calls, [{ method: 'storage_reveal', params: { category: 'install_backups' } }]);
assert.ok(!byLabel(root, 'Show Window storage in Finder') && !byLabel(root, 'Delete Install backups…'), 'nothing else acts there');

// Phase 24 (V1-101): Edit PEBBLE.md and Show settings.json name a file; the host opens it in the text editor.
calls.length = 0;
await buttons(root, 'Edit PEBBLE.md')[0].props.onClick({});
await buttons(root, 'Show settings.json')[0].props.onClick({});
await settle();
assert.deepEqual(calls, [{ method: 'storage_reveal', params: { category: 'pebble_md' } },
  { method: 'storage_reveal', params: { category: 'settings_file' } }]);
assert.ok(textOf(status).includes('Opened settings.json in your text editor.'));

// Phase 24 (V1-004): a forget from Documents waits for the person; Keep and Forget answer that exact question.
calls.length = 0;
await buttons(root, 'Keep them')[0].props.onClick({});
await settle();
assert.deepEqual(calls, [{ method: 'storage_forget_resolve', params: { id: 'a'.repeat(16), accept: false } }]);
calls.length = 0;
await buttons(root, 'Forget them')[0].props.onClick({});
await settle();
assert.deepEqual(calls, [{ method: 'storage_forget_resolve', params: { id: 'a'.repeat(16), accept: true } }]);
assert.ok(textOf(status).includes('Forgot 4 memories.'));

// Phase 24 (V1-061): Delete all conversations and Delete all now, each after a confirmation whose Keep has the focus.
calls.length = 0;
await buttons(root, 'Delete all conversations…')[0].props.onClick({});
await settle();
assert.ok(textOf(root).includes('Move every conversation to Recently deleted?') && calls.length === 0, 'asking moves nothing');
assert.equal(textOf(focused).trim(), 'Keep');
await buttons(root, 'Move all to Recently deleted')[0].props.onClick({});
await settle();
assert.deepEqual(calls.map(c => c.method), ['storage_trash_all', 'storage_summary']);
assert.deepEqual(calls[0].params, { confirm: true });
assert.ok(textOf(status).includes('Moved 3 conversations to Recently deleted. 1 conversation stays'));
calls.length = 0;
await buttons(root, 'Delete all now…')[0].props.onClick({});
await settle();
assert.ok(textOf(root).includes('Delete all 1 conversation in Recently deleted for good?') && calls.length === 0);
assert.equal(textOf(focused).trim(), 'Keep');
await buttons(root, 'Delete all for good')[0].props.onClick({});
await settle();
assert.deepEqual(calls[0], { method: 'storage_purge_all', params: { confirm: true } });

// Phase 24 (V1-017): Remove old sign-in copies, confirmed; the host reports counts only.
calls.length = 0;
await buttons(root, 'Remove old sign-in copies…')[0].props.onClick({});
await settle();
assert.equal(calls.length, 0);
assert.equal(textOf(focused).trim(), 'Keep');
await buttons(root, 'Remove them')[0].props.onClick({});
await settle();
assert.deepEqual(calls, [{ method: 'storage_cleanup_legacy', params: { confirm: true } }]);
assert.ok(textOf(status).includes('Removed 3 old sign-in copies from the Keychain.'));

// A refused move, an invalid settings.json and a host problem are alerts; errors from the host are shown, not thrown.
current = { ...base(), migration: { state: 'refused', reason: 'anchored_records', message: 'Managed Git worktrees live there.' },
  settings_file: { ...base().settings_file, ok: false, error: 'settings.json is not valid JSON (line 2, column 3).', pending: [] },
  problems: { refresh: 'macOS did not allow Pebble to write there.' },
  pebble_md: { status: 'truncated', message: 'PEBBLE.md has 4,331 characters; Pebble uses the first 3,998, up to its last full line within 4,000.' } };
await buttons(root, 'Update copies now')[0].props.onClick({});
await settle();
assert.ok(textOf(root).includes('Pebble uses the first 3,998'), 'a cut PEBBLE.md says how much is used (V1-036)');
const alerts = all(root).filter(e => e.props.role === 'alert').map(textOf);
assert.ok(alerts.some(t => t.includes('Pebble is still using its old folder. Managed Git worktrees live there. Nothing was deleted.')));
assert.ok(alerts.some(t => t.includes('line 2, column 3')) && alerts.some(t => t.includes('did not allow Pebble')));
const failing = bridge.rpc;
bridge.rpc = async () => { throw new Error('Local service is offline.'); };
await buttons(root, 'Update copies now')[0].props.onClick({});
await settle();
assert.ok(all(root).some(e => e.props.role === 'alert' && textOf(e).includes('Local service is offline.')));
bridge.rpc = failing;
nativeOnly(root);

// The mount point: Settings gains a "data & storage" tab that renders this page in the main window's settings.
const settings = readFileSync(new URL('./AdvancedSettings.vue', import.meta.url), 'utf8');
const settingsDescriptor = parse(settings, { filename: 'AdvancedSettings.vue' }).descriptor;
const settingsScript = compileScript(settingsDescriptor, { id: 'advanced' });
assert.deepEqual(compileTemplate({ source: settingsDescriptor.template.content, filename: 'AdvancedSettings.vue', id: 'advanced',
  compilerOptions: { bindingMetadata: settingsScript.bindings } }).errors, []);
assert.equal([...settings.matchAll(/import StorageSettings from "\.\/StorageSettings\.vue";/g)].length, 1);
// Phase 45 (D129); Phase 46 (1.9.29, D214) adds the iPhone tab (Pebble Remote) after Privacy & Stealth.
assert.ok(settings.includes("v-for=\"t in ['models', 'permissions', 'memory', 'extensions', 'mods', 'storage', 'privacy', 'iphone', 'appearance', 'general']\""));
assert.ok(settings.includes('<section v-else-if="tab === \'storage\'" class="advanced-section"><StorageSettings @refresh="emit(\'refresh\')" /></section>'));
assert.ok(!/StorageSettings/.test(readFileSync(new URL('../Pet.vue', import.meta.url), 'utf8')), 'the pet never shows it');
console.log('StorageSettings: ok');
