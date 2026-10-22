import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick } from 'vue';
import { transform } from 'sucrase';

// Phase 14: with a full upgrade-backup budget, startup shows only this screen.
// A backup is removed only after a confirmation, by the id and manifest hash shown.
const require = createRequire(import.meta.url);
const hostile = '<img src=x onerror=alert(1)>';
const calls = [];
const clone = value => JSON.parse(JSON.stringify(value));
const backups = [
  { id: 'b1', state: 'settled', from_version: 1, to_version: 2, created: 1700000000, bytes: 3 << 20, manifest_sha256: '1'.repeat(64) },
  { id: 'b2', state: 'pending', from_version: 4, to_version: 5, created: 1700000500, bytes: 4096, manifest_sha256: '2'.repeat(64) },
];
let view = { reason: 'backup_budget_exhausted', message: 'Pebble stopped this upgrade: ' + hostile, backups };
let retryReady = false;
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: clone(params) });
    if (method === 'upgrade_discard_backup') {
      view = { ...view, backups: view.backups.filter(item => item.id !== params.backup_id) };
      return { discarded: params.backup_id, bytes: 1, maintenance: clone(view) };
    }
    if (method === 'upgrade_retry') return retryReady ? { ready: true } : { ready: false, maintenance: clone(view) };
    throw new Error('Unexpected RPC ' + method);
  },
};
const source = readFileSync(new URL('./UpgradeMaintenance.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'UpgradeMaintenance.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'maintenance' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'UpgradeMaintenance.vue', id: 'maintenance', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Screen = evaluate(script.content).default;
Screen.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source), 'maintenance text renders as literal native content');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.deepEqual([...new Set(methods)].sort(), ["'upgrade_discard_backup'", "'upgrade_retry'"], 'only the maintenance routes are reachable');

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
const root = node('root');
const app = renderer.createApp({ setup: () => () => h(Screen, { view: clone(view) }) });
app.config.errorHandler = error => { throw error; };
const buttons = () => all(root).filter(element => element.type === 'button');
const press = async label => { const target = buttons().find(element => textOf(element) === label); assert.ok(target, 'Missing button ' + label); assert.ok(!target.props.disabled); await target.props.onClick({}); await settle(); };
app.mount(root);
await settle();
let page = textOf(root);
assert.ok(page.includes(hostile) && !all(root).some(element => element.type === 'img'), 'refusal text stays plain text');
assert.ok(page.includes('Backup before upgrade 1 → 2') && page.includes('3.0 MB'));
assert.ok(page.includes('belongs to an unfinished upgrade'));
// A pending backup offers no removal; a settled one needs an explicit confirmation.
assert.equal(buttons().filter(element => textOf(element) === 'Remove this backup…').length, 1);
await press('Remove this backup…');
assert.equal(calls.length, 0, 'the first click only asks for confirmation');
await press('Keep it');
assert.equal(calls.length, 0);
await press('Remove this backup…');
await press('Confirm removal of Backup before upgrade 1 → 2');
assert.deepEqual(calls.at(-1), { method: 'upgrade_discard_backup', params: { backup_id: 'b1', manifest_sha256: '1'.repeat(64) } });
page = textOf(root);
const listed = () => all(root).filter(element => element.type === 'strong').map(textOf);
assert.deepEqual(listed(), ['Backup before upgrade 4 → 5']);
assert.ok(all(root).some(element => element.props.role === 'status' && textOf(element) === 'Backup before upgrade 1 → 2 was removed.'));
await press('Try the upgrade again');
assert.deepEqual(calls.at(-1), { method: 'upgrade_retry', params: {} });
assert.ok(all(root).some(element => element.props.role === 'alert' && textOf(element).startsWith('The upgrade is still waiting')));
retryReady = true;
await press('Try the upgrade again');
assert.ok(all(root).some(element => element.props.role === 'status' && textOf(element) === 'Upgrade finished. Opening Pebble…'));
app.unmount();
console.log('upgrade maintenance: ok');
