import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, createSSRApp, h, nextTick, reactive } from 'vue';
import { renderToString } from '@vue/server-renderer';
import { transform } from 'sucrase';

// Compile only the checked-in components. Recipe, receipt, worktree, environment and plan text is never evaluated.
const require = createRequire(import.meta.url);
const calls = [], failures = new Map(), gates = new Map();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const now = Math.floor(Date.now() / 1000);
let actionCount = 0, brokenReceipts = false, proposeReply = null;
function hold(method, error = '') {
  const gate = { error };
  gate.promise = new Promise(resolve => { gate.resolve = resolve; });
  gates.set(method, gate);
  return gate;
}
const R1 = 'a'.repeat(32), R2 = 'e'.repeat(32), E1 = 'f'.repeat(32), E2 = '9'.repeat(32), BASE = 'b'.repeat(40), BLOB = '7'.repeat(40);
const W1 = 'c1'.repeat(16), W2 = 'c2'.repeat(16), W3 = 'c3'.repeat(16), W4 = 'c4'.repeat(16);
// Phase 24 V1-124: the notice names the Details drawer
const reviewNotice = 'Plan prepared as a review card in Details › Activity. Nothing runs until you approve it there.';
const notIntegrated = '1 worktree change(s) are not integrated into the project: src/new.ts. Integrate them first, or discard them through a native review that keeps recoverable blobs.';
const worktree = (id, agent, state, extra = {}) => ({ worktree_id: id, run_id: 'run-1', chat_id: 'chat-a', agent_id: agent, generation: 1, action_id: 'action-w', state, path: '/managed/' + id, workspace: '/work/a', base_commit: BASE, evidence: {}, writes: 2, bytes_written: 40, changed_paths: 1, origin: 'child_worktree', authorization: 'none', ...extra });
const chats = {
  'chat-a': {
    workspace: true,
    recipes: [
      { id: 'python-pytest', kind: 'test', reason: 'pyproject.toml declares pytest', available: true, issue: null },
      { id: 'npm-test', kind: 'test', reason: 'package.json scripts.test', available: false, issue: 'Node.js was not found under the pinned toolchain root.', script: 'node --test' },
    ],
    receipts: [
      { receipt_id: R1, operation: 'commit', state: 'applied', plan_hash: '5'.repeat(64), summary: 'Commit 2 selected path(s) to branch main as Ada <ada@example.test>: "Fix parser".', effects: [], destructive: false, publication: false, undo_of: null, undone_by: null, created: now - 120, authorization: 'none' },
      { receipt_id: R2, operation: 'push', state: 'failed', plan_hash: '6'.repeat(64), summary: 'Publish main to origin/main.', effects: [], destructive: false, publication: true, created: now - 60, error: 'non_fast_forward: The remote branch has commits that this branch does not contain.', authorization: 'none' },
    ],
    worktrees: [worktree(W1, 'agent-1', 'active', { pending: true }), worktree(W2, 'agent-2', 'retired'), worktree(W3, 'agent-3', 'active'), worktree(W4, 'agent-4', 'active')],
    environments: [
      { environment_id: E1, kind: 'npm-ci', state: 'ready', root: '/work/a', path: '/envs/' + E1, host_generated: true, lock_digest: '1'.repeat(64), registries: [['registry.npmjs.org', 443]], size_bytes: 3 * 2 ** 20, file_count: 42, reason: null },
      { environment_id: E2, kind: 'pip-hashes', state: 'installing', root: '/work/a', path: '/envs/' + E2, host_generated: true, lock_digest: '2'.repeat(64), registries: [['pypi.org', 443], ['files.pythonhosted.org', 443]], size_bytes: null, file_count: null, reason: null },
    ],
  },
  'chat-b': { workspace: false, recipes: [], receipts: [], worktrees: [], environments: [] },
};
function respond(method, params, chat) {
  if (!chat) throw new Error('Unknown chat.');
  if (['project_recipes', 'git_plan_propose', 'environment_plan'].includes(method) && !chat.workspace) throw new Error('Choose a project folder for this conversation first.');
  if (method === 'project_recipes') return { recipes: chat.recipes, authorization: 'none', inventory_truncated: false };
  if (method === 'git_plan_propose' || method === 'environment_plan') return proposeReply ?? { status: 'awaiting_user_approval', action_id: 'action-' + ++actionCount, kind: method === 'environment_plan' ? 'install' : 'git' };
  if (method === 'git_plan_receipts') return brokenReceipts ? { items: [{ receipt_id: 'not-a-receipt' }] } : { items: chat.receipts, authorization: 'none' };
  if (method === 'git_plan_readback') {
    const receipt = chat.receipts.find(item => item.receipt_id === params.receipt_id);
    if (!receipt) throw new Error('Git plan receipt not found in this conversation.');
    return { receipt_id: receipt.receipt_id, operation: receipt.operation, state: receipt.state, plan_hash: receipt.plan_hash, origin: 'pebble_git_plan', authorization: 'none', effect: 'none', verified: true, matches_receipt: true, matches_before: false,
      expected: [{ fact: 'ref', key: 'refs/heads/main', expected: '9'.repeat(40), current: '9'.repeat(40), match: true }, { fact: 'index', key: 'src/a.ts', expected: { mode: '100644', oid: '6'.repeat(40) }, current: 'unavailable', match: false }], before: [] };
  }
  if (method === 'agent_worktrees') return chat.worktrees;
  if (method === 'agent_worktree_retire') {
    const item = chat.worktrees.find(row => row.run_id === params.run_id && row.agent_id === params.agent_id);
    if (!item) throw new Error('This child has no managed worktree.');
    if (typeof params.discard !== 'boolean') throw new Error('discard must be true or false.');
    if (item.state !== 'active') throw new Error('Only an active child worktree can be retired.');
    if (item.pending && !params.discard) throw new Error(notIntegrated);
    item.state = 'retired';
    const preserved = item.pending ? [{ path: 'src/new.ts', blob_oid: BLOB, sha256: '8'.repeat(64), bytes: 12 }, { path: 'src/old.ts', deleted: true, blob_oid: null, sha256: null }] : [];
    return { worktree_id: item.worktree_id, state: 'retired', path: item.path, integrated: item.pending ? [] : ['src/done.ts'], integrated_count: item.pending ? 0 : 1, preserved, discard_review: preserved.length > 0, ignored_deleted: 0, origin: 'child_worktree', authorization: 'none' };
  }
  if (method === 'environment_list') return { items: chat.environments, authorization: 'none' };
  if (method === 'environment_remove') {
    const item = chat.environments.find(row => row.environment_id === params.environment_id);
    if (!item || !['ready', 'failed', 'uncertain'].includes(item.state)) throw new Error('Only a ready, failed or uncertain environment can be removed.');
    item.state = 'removed';
    return { environment_id: item.environment_id, state: 'removed', deleted: true, authorization: 'none' };
  }
  throw new Error('Unexpected RPC ' + method);
}
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: clone(params) });
    const gate = gates.get(method);
    gates.delete(method);
    let result, error = failures.get(method);
    failures.delete(method);
    if (!error) {
      try { result = clone(respond(method, params, chats[params.id])); } catch (cause) { error = cause.message; }
    }
    if (gate) { await gate.promise; error ||= gate.error; }
    if (error) throw new Error(error);
    return result;
  },
};
function compile(filename) {
  const source = readFileSync(new URL(filename, import.meta.url), 'utf8');
  const descriptor = parse(source, { filename }).descriptor;
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  function evaluate(code) {
    const module = { exports: {} };
    new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
      name => name === '../bridge' ? bridge : require(name), module, module.exports,
    );
    return module.exports;
  }
  const component = evaluate(script.content).default;
  component.render = evaluate(template.code).render;
  return { component, source };
}
const { component: Workflows, source } = compile('./CodingWorkflows.vue');
const { component: Review, source: reviewSource } = compile('./ActionReview.vue');
for (const text of [source, reviewSource]) assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(text), 'records render as literal native content');
const allowed = ['project_recipes', 'git_plan_propose', 'git_plan_receipts', 'git_plan_readback', 'agent_worktrees', 'agent_worktree_retire', 'environment_list', 'environment_plan', 'environment_remove'];
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.ok(methods.every(method => /^'[a-z_]+'$/.test(method)), 'every native method name is a fixed literal, never generated');
assert.deepEqual([...new Set(methods.map(method => method.slice(1, -1)))].sort(), [...allowed].sort(), 'no approval, permission or settings route is reachable from the panel');
assert.ok(!/\brpc\b|bridge|<button|@click|onClick/.test(reviewSource), 'the review card is read only and reaches no native route');

// The host runs real Vue hooks, watchers, v-model directives, forms and buttons.
// It has no HTML parser, browser capability API or script evaluator.
let focused = null, timers = 0;
function node(type, text = '') {
  return { type, tagName: type.toUpperCase(), text, children: [], props: {}, parent: null, value: '', addEventListener() {}, removeEventListener() {}, getRootNode() { return globalThis.document; }, focus() { focused = this; } };
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
const previous = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot, setTimeout: globalThis.setTimeout, setInterval: globalThis.setInterval };
globalThis.Document = class Document { activeElement = null; };
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
globalThis.setTimeout = () => { timers++; return 0; };
globalThis.setInterval = () => { timers++; return 0; };
const textOf = element => (element.type === '#comment' ? '' : element.text) + element.children.map(textOf).join('');
const all = element => [element, ...element.children.flatMap(all)];
const ownText = element => element.children.filter(child => child.type === '#text').map(child => child.text).join('').trim();
const nameOf = element => element.props['aria-label'] ?? textOf(element);
async function settle() { for (let index = 0; index < 10; index++) { await Promise.resolve(); await nextTick(); } }
const props = reactive({ chatId: 'chat-a', workspace: '/work/a', refreshKey: 'initial' });
const root = node('root');
let state;
const instrumented = { ...Workflows, setup(properties, context) { state = Workflows.setup(properties, context); return state; } };
const app = renderer.createApp({ setup: () => () => h(instrumented, { ...props }) });
app.config.errorHandler = error => { throw error; };
const page = () => textOf(root);
const region = label => all(root).find(element => element.props['aria-label'] === label);
function findButton(label) { return all(root).find(element => element.type === 'button' && nameOf(element) === label); }
function button(label) { const found = findButton(label); assert.ok(found, 'Missing button ' + label); return found; }
function press(label) {
  const target = button(label);
  assert.ok(!target.props.disabled, label + ' must be enabled');
  const event = { preventDefault() {}, stopPropagation() {} };
  if (target.props.onClick) return target.props.onClick(event);
  let form = target.parent;
  while (form && form.type !== 'form') form = form.parent;
  assert.ok(target.props.type === 'submit' && form?.props.onSubmit, label + ' must submit its form');
  return form.props.onSubmit(event);
}
async function click(label) { await press(label); await settle(); }
function disabled(label, value = true) { assert.equal(!!button(label).props.disabled, value, label + (value ? ' must be disabled' : ' must be enabled')); }
function control(name) {
  const labels = all(root).filter(element => element.type === 'label');
  const label = labels.find(element => ownText(element) === name) || labels.find(element => ownText(element).startsWith(name));
  assert.ok(label, 'Missing field ' + name);
  const found = all(label).find(element => element.type === 'input' || element.type === 'textarea');
  assert.ok(found, 'Missing control ' + name);
  return found;
}
async function input(name, value) { control(name).props['onUpdate:modelValue'](value); await settle(); }
const count = method => calls.filter(call => call.method === method).length;
const paramsFor = method => calls.filter(call => call.method === method).at(-1)?.params;
const alerted = message => all(root).some(element => element.props.role === 'alert' && textOf(element) === message);
const announced = message => all(root).some(element => element.props.role === 'status' && element.props['aria-live'] === 'polite' && textOf(element) === message);
const retireLabel = (agent, id) => 'Retire the worktree of ' + agent + ' (' + id.slice(0, 8) + ')';
const discardLabel = (agent, id) => 'Discard un-integrated work of ' + agent + ' (' + id.slice(0, 8) + ')';
const undoLabel = id => 'Undo receipt ' + id.slice(0, 12) + ' as a new reviewed plan';
// One mounted review card per payload; facts are read before teardown.
function review(action) {
  const host = node('root');
  const view = renderer.createApp(Review, { action });
  view.config.errorHandler = error => { throw error; };
  view.mount(host);
  const elements = all(host);
  const shown = {
    text: textOf(host),
    types: new Set(elements.map(element => element.type)),
    alerts: elements.filter(element => element.props.class === 'review-badge review-alert').map(textOf),
    badges: elements.filter(element => /^review-badge(?: review-alert)?$/.test(element.props.class ?? '')).map(textOf),
    pres: elements.filter(element => element.type === 'pre').map(textOf),
    lists: Object.fromEntries(elements.filter(element => element.type === 'ul').map(element => [element.props['aria-label'], element.children.filter(child => child.type === 'li').map(textOf)])),
    controls: elements.filter(element => ['button', 'input', 'textarea', 'select', 'form'].includes(element.type)).length,
  };
  view.unmount();
  return shown;
}
function includes(shown, expected, label) { for (const item of expected) assert.ok(shown.text.includes(item), label + ' missing ' + JSON.stringify(item)); }
let mounted = false, text, gate;

try {
  app.mount(root); mounted = true;
  await settle();

  // Mount loads recipes plus three chat-scoped lists; nothing polls.
  assert.deepEqual(calls.map(call => call.method).sort(), ['agent_worktrees', 'environment_list', 'git_plan_receipts', 'project_recipes']);
  for (const call of calls) assert.deepEqual(call.params, { id: 'chat-a' });
  assert.equal(timers, 0, 'the panel never polls');
  text = page();
  for (const expected of ['python-pytest', 'test · available', 'Detected from: pyproject.toml declares pytest', 'npm-test', 'test · unavailable', 'Issue: Node.js was not found under the pinned toolchain root.', 'Script: node --test', 'grants nothing',
    'Commit 2 selected path(s) to branch main', 'failed · publication', 'Error: non_fast_forward', 'nothing runs until you approve it there', 'Hooks and the network never run',
    'agent-1', 'Worktree c1c1c1c1c1c1 · Base bbbbbbbbbbbb · Run run-1 · Generation 1', '2 write(s) · 1 path(s)', 'never copied into one',
    'Environment ffffffffffff · Lockfile 111111111111 · 3 MiB · 42 files', 'Registries: registry.npmjs.org:443', 'Registries: pypi.org:443, files.pythonhosted.org:443', 'installing', 'Install scripts never run']) {
    assert.ok(text.includes(expected), 'missing ' + expected);
  }
  assert.ok(findButton(undoLabel(R1)) && !findButton(undoLabel(R2)), 'only an applied receipt offers undo');
  assert.ok(findButton('Read back receipt ' + R2.slice(0, 12)), 'any receipt can be read back');
  assert.ok(findButton(retireLabel('agent-1', W1)) && findButton(retireLabel('agent-3', W3)) && !findButton(retireLabel('agent-2', W2)), 'only active worktrees can be retired');
  assert.ok(!findButton(discardLabel('agent-2', W2)));
  assert.ok(findButton('Remove environment ' + E1.slice(0, 12)) && !findButton('Remove environment ' + E2.slice(0, 12)), 'an installing environment cannot be removed');
  const checkboxes = all(root).filter(element => element.type === 'input' && element.props.type === 'checkbox');
  assert.equal(checkboxes.length, 1, 'the only checkbox chooses whether a new branch is switched to');
  assert.ok(ownText(checkboxes[0].parent).startsWith('Switch to it'));
  assert.ok(!all(root).some(element => element.type === 'button' && /approve|grant|permission|setting|apply|force/i.test(nameOf(element))), 'no approval, permission or settings controls');

  props.refreshKey = 'chat-updated'; await settle();
  assert.equal(count('git_plan_receipts'), 2, 'parent updates refresh receipts');
  assert.equal(count('agent_worktrees'), 2); assert.equal(count('environment_list'), 2);
  assert.equal(count('project_recipes'), 1, 'parent updates do not re-detect recipes');

  // Commit input is validated before any native call.
  async function rejected(label, pattern) {
    await click(label);
    assert.match(state.gitError.value, pattern);
    assert.ok(alerted(state.gitError.value), 'the error is announced');
  }
  await rejected('Prepare commit plan', /List 1–64 relative file paths/);
  for (const bad of ['../secrets.txt', '/etc/hosts', 'src/.git/config', 'C:\\Users\\me\\notes.txt', '~/notes.txt', 'src//a.ts']) {
    await input('Paths to commit, one per line', bad);
    await rejected('Prepare commit plan', /relative to the project/);
  }
  await input('Paths to commit, one per line', 'src/a.ts\nREADME.md');
  await rejected('Prepare commit plan', /commit message of 1–4000/);
  await input('Commit message', 'x'.repeat(201)); await rejected('Prepare commit plan', /first line/);
  await input('Commit message', 'Fix\u0007 parser'); await rejected('Prepare commit plan', /control characters/);
  await input('Commit message', 'Fix the parser\n\nKeeps other work.');
  for (const bad of ['feature..x', 'HEAD', 'topic/x.lock', '.hidden', 'a b', 'trailing/', 'a'.repeat(40)]) {
    await input('New branch (optional)', bad);
    await rejected('Prepare commit plan', /plain branch name/);
  }
  assert.equal(count('git_plan_propose'), 0, 'invalid input never reaches native');

  // A commit plan is only proposed: the host stages a review card and nothing runs here.
  await input('New branch (optional)', ' feature/parser ');
  gate = hold('git_plan_propose');
  const proposing = press('Prepare commit plan'); await settle();
  assert.equal(state.busy.value, 'commit');
  disabled('Preparing…'); disabled(retireLabel('agent-3', W3)); disabled('Refresh receipts'); disabled('Prepare npm-ci install');
  gate.resolve(); await proposing; await settle();
  assert.deepEqual(paramsFor('git_plan_propose'), { id: 'chat-a', operation: 'commit', paths: JSON.stringify(['src/a.ts', 'README.md']), message: 'Fix the parser\n\nKeeps other work.', branch: 'feature/parser' });
  assert.equal(typeof paramsFor('git_plan_propose').paths, 'string', 'paths travel as the JSON string the host parses');
  assert.equal(state.gitNotice.value, reviewNotice);
  assert.ok(announced(reviewNotice), 'the staged plan is announced politely');
  assert.deepEqual({ ...state.commitForm }, { paths: '', message: '', branch: '' }, 'a staged plan clears its form');
  assert.equal(state.busy.value, '');
  for (const name of ['git_plan_readback', 'agent_worktree_retire', 'environment_plan', 'environment_remove']) assert.equal(count(name), 0, 'proposing runs nothing else');

  await input('Paths to commit, one per line', 'src/a.ts\n\nsrc/a.ts');
  await input('Commit message', 'Second commit');
  await click('Prepare commit plan');
  assert.deepEqual(paramsFor('git_plan_propose'), { id: 'chat-a', operation: 'commit', paths: JSON.stringify(['src/a.ts']), message: 'Second commit' });
  assert.ok(!('branch' in paramsFor('git_plan_propose')), 'an empty optional branch is not sent');

  // Branch plans: create (optionally switching) and switch.
  await rejected('Prepare branch plan', /plain branch name/);
  await input('Branch name', 'release/1.2'); await input('Switch to it after creating', true);
  await click('Prepare branch plan');
  assert.deepEqual(paramsFor('git_plan_propose'), { id: 'chat-a', operation: 'create_branch', name: 'release/1.2', switch: true });
  assert.deepEqual({ ...state.branchForm }, { name: '', switch: false });
  await input('Branch name', 'topic'); await click('Prepare branch plan');
  assert.deepEqual(paramsFor('git_plan_propose'), { id: 'chat-a', operation: 'create_branch', name: 'topic', switch: false });
  await rejected('Prepare switch plan', /plain branch name/);
  await input('Branch to switch to', ' main '); await click('Prepare switch plan');
  assert.deepEqual(paramsFor('git_plan_propose'), { id: 'chat-a', operation: 'switch_branch', name: 'main' });
  assert.equal(state.target.value, '');

  // Host refusals are shown and keep the input; an approval-looking reply is not a staged plan.
  failures.set('git_plan_propose', 'Wait for the current task to finish first.');
  await input('Branch to switch to', 'develop'); await click('Prepare switch plan');
  assert.equal(paramsFor('git_plan_propose').name, 'develop', 'the request reached the host, which refused it');
  assert.equal(state.gitError.value, 'Wait for the current task to finish first.');
  assert.ok(alerted('Wait for the current task to finish first.'));
  assert.equal(state.gitNotice.value, ''); assert.equal(state.target.value, 'develop', 'refused input stays for a retry');
  proposeReply = { approved: true, authorization: 'granted' };
  await click('Prepare switch plan');
  proposeReply = null;
  assert.match(state.gitError.value, /host reply was incomplete/);
  assert.equal(state.gitNotice.value, '');

  // Readback is a read-only request whose facts render as text.
  await click('Read back receipt ' + R1.slice(0, 12));
  assert.deepEqual(paramsFor('git_plan_readback'), { id: 'chat-a', receipt_id: R1 });
  assert.equal(focused, state.readbackCard.value, 'focus moves to the readback');
  text = textOf(region('Readback of receipt ' + R1.slice(0, 12)));
  for (const expected of ['Receipt stateapplied', 'Readback verifiedyes', 'Matches the receiptyes', 'Matches the state beforeno', 'ref refs/heads/main: matches · expected 999999999999 · current 999999999999', 'index src/a.ts: differs', 'current unavailable', 'never retries or repairs']) {
    assert.ok(text.includes(expected), 'readback missing ' + expected);
  }
  await click('Close readback');
  assert.equal(state.readback.value, null);

  // Undo proposes a new reviewed plan bound to the receipt.
  await click(undoLabel(R1));
  assert.deepEqual(paramsFor('git_plan_propose'), { id: 'chat-a', operation: 'undo', receipt_id: R1 });
  assert.equal(state.gitNotice.value, reviewNotice);
  assert.equal(state.gitError.value, '', 'a successful request clears the earlier error');

  // List errors keep the last good list; malformed and older replies never replace it.
  failures.set('git_plan_receipts', 'The receipt store is busy.');
  await click('Refresh receipts');
  assert.equal(state.receiptsError.value, 'The receipt store is busy.');
  assert.ok(alerted('The receipt store is busy.'));
  assert.equal(state.receipts.value.length, 2, 'a failed refresh keeps the last list');
  brokenReceipts = true; await click('Refresh receipts'); brokenReceipts = false;
  assert.equal(state.receiptsError.value, 'Git receipt data is incomplete.');
  assert.equal(state.receipts.value.length, 2);
  gate = hold('git_plan_receipts');
  const older = state.loadReceipts(); await settle();
  chats['chat-a'].receipts[0].summary = 'Renamed summary.';
  await state.loadReceipts(); await settle();
  gate.resolve(); await older; await settle();
  assert.equal(state.receipts.value[0].summary, 'Renamed summary.', 'an older reply cannot replace a newer one');
  assert.equal(state.receiptsError.value, ''); assert.equal(state.receiptsLoading.value, false);

  // Retire never discards: a non-integrated worktree is refused and stays listed.
  await click(retireLabel('agent-1', W1));
  assert.deepEqual(paramsFor('agent_worktree_retire'), { id: 'chat-a', run_id: 'run-1', agent_id: 'agent-1', discard: false });
  assert.equal(state.worktreeError.value, notIntegrated);
  assert.ok(alerted(notIntegrated) && findButton(retireLabel('agent-1', W1)), 'a refused retirement keeps the worktree listed');

  // Discard is a separate confirmation step; only the confirmation sends discard: true.
  await click(discardLabel('agent-1', W1));
  assert.equal(count('agent_worktree_retire'), 1, 'opening the discard review sends nothing');
  assert.equal(focused, state.discardCard.value, 'focus moves to the discard review');
  text = textOf(region('Discard review for agent-1'));
  for (const expected of ['saves every changed file that is not already in the project as a Git blob', 'stay recoverable', 'deletes the worktree folder', 'Folder to delete/managed/' + W1, 'refused while the child still runs']) {
    assert.ok(text.includes(expected), 'discard review missing ' + expected);
  }
  await click('Keep worktree');
  assert.equal(state.discarding.value, null); assert.equal(count('agent_worktree_retire'), 1);
  await click(discardLabel('agent-1', W1));
  gate = hold('agent_worktree_retire');
  const discarding = press('Confirm discard and delete folder'); await settle();
  assert.equal(state.busy.value, 'discard:' + W1);
  disabled('Discarding…'); disabled('Keep worktree');
  gate.resolve(); await discarding; await settle();
  assert.deepEqual(paramsFor('agent_worktree_retire'), { id: 'chat-a', run_id: 'run-1', agent_id: 'agent-1', discard: true });
  assert.equal(count('agent_worktree_retire'), 2);
  assert.equal(state.discarding.value, null);
  text = textOf(region('Retirement result for agent-1'));
  for (const expected of ['Stateretired', 'Saved as blobs2', 'src/new.ts · blob ' + BLOB, 'src/old.ts · deleted in the worktree', 'git cat-file -p']) assert.ok(text.includes(expected), 'retirement missing ' + expected);
  assert.ok(announced('Worktree removed. Un-integrated files were saved as Git blobs first; their IDs are listed below.'));
  assert.ok(!findButton(retireLabel('agent-1', W1)) && !findButton(discardLabel('agent-1', W1)), 'a retired worktree offers no controls');

  // A refresh reporting a changed worktree closes the confirmation without a request.
  await click(discardLabel('agent-3', W3));
  chats['chat-a'].worktrees.find(item => item.agent_id === 'agent-3').state = 'retiring';
  await click('Refresh worktrees');
  assert.equal(state.discarding.value, null);
  assert.equal(state.worktreeError.value, 'That worktree changed or is no longer active. Nothing was removed.');
  assert.equal(count('agent_worktree_retire'), 2);
  chats['chat-a'].worktrees.find(item => item.agent_id === 'agent-3').state = 'active';
  await click('Refresh worktrees');
  await click(retireLabel('agent-3', W3));
  assert.deepEqual(paramsFor('agent_worktree_retire'), { id: 'chat-a', run_id: 'run-1', agent_id: 'agent-3', discard: false });
  assert.equal(state.worktreeError.value, '');
  assert.equal(state.worktreeNotice.value, 'Worktree retired. Every change it made was already in the project.');
  assert.ok(textOf(region('Retirement result for agent-3')).includes('Saved as blobs0'));

  // Installs are only planned here; removal is a separate confirmation.
  await click('Prepare npm-ci install');
  assert.deepEqual(paramsFor('environment_plan'), { id: 'chat-a', kind: 'npm-ci' });
  assert.equal(state.environmentNotice.value, reviewNotice);
  await click('Prepare pip-hashes install');
  assert.deepEqual(paramsFor('environment_plan'), { id: 'chat-a', kind: 'pip-hashes' });
  failures.set('environment_plan', 'Dependency installs need project read and web access in Permissions.');
  await click('Prepare npm-ci install');
  assert.equal(state.environmentError.value, 'Dependency installs need project read and web access in Permissions.');
  assert.equal(state.environmentNotice.value, '');
  await click('Remove environment ' + E1.slice(0, 12));
  assert.equal(count('environment_remove'), 0, 'opening the removal review sends nothing');
  assert.equal(focused, state.removeCard.value);
  assert.ok(textOf(region('Removal review for environment ' + E1.slice(0, 12))).includes('Your project and its lockfile are not touched'));
  await click('Keep environment');
  assert.equal(state.removing.value, null); assert.equal(count('environment_remove'), 0);
  await click('Remove environment ' + E1.slice(0, 12)); await click('Confirm remove');
  assert.deepEqual(paramsFor('environment_remove'), { id: 'chat-a', environment_id: E1 });
  assert.equal(state.environmentNotice.value, 'Environment folder deleted and the record settled.');
  assert.ok(!findButton('Remove environment ' + E1.slice(0, 12)), 'a removed environment offers no control');

  // A chat switch discards open reviews and ignores replies addressed to the old chat.
  gate = hold('git_plan_receipts');
  const staleList = state.loadReceipts(); await settle();
  await click(discardLabel('agent-4', W4));
  assert.ok(state.discarding.value && state.retired.value);
  props.chatId = 'chat-b'; props.workspace = null; await settle();
  assert.equal(state.discarding.value, null); assert.equal(state.retired.value, null); assert.equal(state.readback.value, null); assert.equal(state.removing.value, null);
  gate.resolve(); await staleList; await settle();
  assert.deepEqual(state.receipts.value, [], 'a reply for the previous chat is ignored');
  text = page();
  for (const expected of ['No reviewed Git receipts in this chat.', 'No child worktrees in this chat.', 'No managed dependency environments in this chat.', 'Choose a workspace folder for this chat to detect project recipes.', 'Choose a workspace folder for this chat to prepare Git plans.', 'Choose a workspace folder for this chat to prepare a dependency install.']) {
    assert.ok(text.includes(expected), 'empty state missing ' + expected);
  }
  assert.ok(!text.includes('Renamed summary.') && !text.includes('agent-4') && !text.includes('python-pytest') && !text.includes('ffffffffffff'));
  assert.equal(calls.filter(call => ['project_recipes', 'git_plan_propose', 'environment_plan'].includes(call.method) && call.params.id === 'chat-b').length, 0, 'no project routes without a workspace');
  assert.ok(!findButton('Prepare commit plan') && !findButton('Prepare npm-ci install') && !findButton('Refresh recipes'));

  props.chatId = 'chat-a'; props.workspace = '/work/a'; await settle();
  await input('Branch to switch to', 'main');
  gate = hold('git_plan_propose');
  const crossing = press('Prepare switch plan'); await settle();
  assert.equal(state.busy.value, 'switch_branch');
  props.chatId = 'chat-b'; props.workspace = null; await settle();
  assert.equal(state.busy.value, '', 'a new chat is not blocked by another chat’s request');
  gate.resolve(); await crossing; await settle();
  assert.equal(state.gitNotice.value, ''); assert.equal(state.gitError.value, '');

  props.chatId = 'chat-a'; props.workspace = '/work/a'; await settle();
  await click(discardLabel('agent-4', W4));
  gate = hold('agent_worktree_retire');
  const crossingDiscard = press('Confirm discard and delete folder'); await settle();
  props.chatId = 'chat-b'; props.workspace = null; await settle();
  const beforeLate = calls.length;
  gate.resolve(); await crossingDiscard; await settle();
  assert.equal(calls.length, beforeLate, 'a late reply does not reload into another chat');
  assert.equal(state.retired.value, null); assert.equal(state.worktreeNotice.value, ''); assert.equal(state.worktreeError.value, '');
  assert.deepEqual(state.worktrees.value, []);

  // A workspace switch in the same chat re-detects recipes (loading, then empty) and clears the forms.
  props.chatId = 'chat-a'; props.workspace = '/work/a'; await settle();
  const recipeCalls = count('project_recipes'), detected = chats['chat-a'].recipes;
  await input('Commit message', 'Draft for /work/a');
  chats['chat-a'].recipes = [];
  gate = hold('project_recipes');
  props.workspace = '/work/a-other'; await settle();
  assert.equal(count('project_recipes'), recipeCalls + 1, 'a workspace switch reloads recipes for the new scope');
  assert.ok(page().includes('Loading recipes…'));
  assert.equal(region('Project recipes').props['aria-busy'], true);
  assert.equal(state.commitForm.message, '');
  gate.resolve(); await settle();
  assert.ok(page().includes('No recipes were detected in this project.'));
  assert.equal(region('Project recipes').props['aria-busy'], false);
  chats['chat-a'].recipes = detected;

  // Hostile records from every native route render as escaped text.
  const hostile = '<img src=x onerror=host()>';
  const seeded = {
    ...Workflows,
    setup(properties, context) {
      const bindings = Workflows.setup(properties, context);
      bindings.recipes.value = [{ id: hostile, kind: '<svg/onload=k()>', reason: '<script>reason()</script>', available: false, issue: '<iframe src=issue>', script: '<script>script()</script>' }];
      bindings.receipts.value = [{ receipt_id: R1, operation: '<script>op()</script>', state: '<img src=state>', summary: '<script>summary()</script>', error: hostile, note: '<svg onload=note()>', created: '<img src=y>', undo_of: '<iframe src=undo>' }];
      bindings.readback.value = { receipt_id: R1, data: { state: 'applied', verified: true, issue: '<script>issue()</script>', expected: [{ fact: '<img src=fact>', key: '<svg onload=key()>', expected: '<script>e()</script>', current: hostile, match: false }] } };
      bindings.worktrees.value = [{ worktree_id: W1, run_id: '<script>run()</script>', agent_id: hostile, state: 'active', base_commit: '<iframe src=base>', path: '<svg onload=path()>' }];
      bindings.discarding.value = bindings.worktrees.value[0];
      bindings.retired.value = { agent_id: hostile, data: { state: '<script>state()</script>', worktree_id: '<img src=wt>', preserved: [{ path: '<script>kept()</script>', blob_oid: '<iframe src=blob>' }] } };
      bindings.environments.value = [{ environment_id: E1, kind: '<script>env()</script>', state: 'ready', lock_digest: '<img src=lock>', registries: [['<svg onload=reg()>', 443]], reason: '<iframe src=reason>' }];
      bindings.removing.value = bindings.environments.value[0];
      return bindings;
    },
  };
  const html = await renderToString(createSSRApp(seeded, { chatId: 'chat-x', workspace: '/w' }));
  for (const literal of ['&lt;img src=x onerror=host()&gt;', '&lt;svg/onload=k()&gt;', '&lt;script&gt;reason()&lt;/script&gt;', '&lt;iframe src=issue&gt;', '&lt;script&gt;script()&lt;/script&gt;', '&lt;script&gt;op()&lt;/script&gt;', '&lt;script&gt;summary()&lt;/script&gt;', '&lt;svg onload=note()&gt;', '&lt;iframe src=undo&gt;', '&lt;script&gt;issue()&lt;/script&gt;', '&lt;img src=fact&gt;', '&lt;svg onload=key()&gt;', '&lt;script&gt;e()&lt;/script&gt;', '&lt;script&gt;run()&lt;/script&gt;', '&lt;iframe src=base&gt;', '&lt;svg onload=path()&gt;', '&lt;script&gt;state()&lt;/script&gt;', '&lt;script&gt;kept()&lt;/script&gt;', '&lt;iframe src=blob&gt;', '&lt;script&gt;env()&lt;/script&gt;', '&lt;svg onload=reg()&gt;', '&lt;iframe src=reason&gt;']) {
    assert.ok(html.includes(literal), 'missing literal ' + literal);
  }
  assert.ok(!/<(?:img|script|svg|iframe)\b/i.test(html), 'no hostile markup is created');
  assert.ok(html.includes('unknown') && !html.includes('Undo receipt'), 'an unknown receipt state enables no undo');

  // ActionReview: a read-only rendering of the immutable reviewed payload.
  const identity = { name: 'Ada Lovelace', email: 'ada@example.test', source: 'repository' };
  const commitPlan = {
    plan_version: 1, plan_id: '0123456789abcdef0123456789abcdef', chat_id: 'chat-a', operation: 'commit', prepared: now, root: '/work/a', root_identity: [1, 2], fingerprint: '3'.repeat(64),
    bindings: {
      head_ref: 'refs/heads/main', head_commit: '4'.repeat(40), refs_digest: '5'.repeat(64), index_digest: '6'.repeat(64),
      paths: [
        { path: 'old.txt', kind: 'delete', mode: null, sha256: null, blob_oid: null, size: null, index_before: { mode: '100644', oid: '1'.repeat(40) }, head: { mode: '100644', oid: '1'.repeat(40) }, tree_after: null, changed: true },
        { path: 'src/a.ts', kind: 'add', mode: '100644', sha256: '7'.repeat(64), blob_oid: 'abcdef1234567890abcdef1234567890abcdef12', size: 10, index_before: null, head: null, tree_after: { mode: '100644', oid: 'abcdef1234567890abcdef1234567890abcdef12' }, changed: true },
      ],
      target_ref: 'refs/heads/main', parent: '4'.repeat(40), new_branch: false, identity,
    },
    summary: 'Commit 2 selected path(s) to branch main as Ada Lovelace <ada@example.test>: "Fix parser". Other staged and unstaged work stays exactly as it is.',
    effects: ['Write blob objects from the reviewed file bytes', 'Create one commit on parent 444444444444 containing only the selected paths'],
    destructive: false, publication: false, requires_review: true, hooks_run: false, network: false, sensitive_findings: [], origin: 'pebble_git_plan', trust: 'untrusted', authorization: 'none',
    paths: ['old.txt', 'src/a.ts'], message: 'Fix parser\n\nBody line.\n', branch: null, plan_hash: '8'.repeat(64), permission_mode: 'ask',
  };
  let shown = review({ id: 'action-1', chat_id: 'chat-a', kind: 'git', status: 'pending', payload: commitPlan });
  includes(shown, ['Untrusted repository data, shown as plain text.', 'Git commit', 'Repository root/work/a', 'Branchmain', 'HEAD444444444444', 'AuthorAda Lovelace <ada@example.test> · from repository', 'Target refrefs/heads/main',
    'Sensitive findingsnone reported', 'Plan ID0123456789abcdef0123456789abcdef', 'Plan hash8888888888888888', 'Selected paths (2)', 'Commit message (plain text)', 'Write blob objects from the reviewed file bytes', 'Other staged and unstaged work stays exactly as it is.'], 'commit review');
  assert.deepEqual(shown.lists['Selected paths'], ['old.txt · delete · blob none', 'src/a.ts · add · blob abcdef123456']);
  assert.deepEqual(shown.pres, ['Fix parser\n\nBody line.\n'], 'the commit message is one plain-text block');
  assert.deepEqual(shown.badges, ['Needs native review', 'hooks_run: false', 'network: false']);
  assert.deepEqual(shown.alerts, [], 'a local commit has no destructive or publication badge');
  assert.ok(!shown.text.includes('New branch') && !shown.text.includes('Receipt readback'), 'absent facts are omitted, not invented');
  assert.equal(shown.controls, 0, 'the review card has no controls; App.vue keeps Approve and Dismiss');

  const discardPlan = { ...commitPlan, operation: 'discard', destructive: true, message: undefined, paths: ['gone.txt', 'notes.txt'],
    bindings: { head_ref: null, head_commit: '4'.repeat(40), refs_digest: '5'.repeat(64), index_digest: '6'.repeat(64), paths: [
      { path: 'notes.txt', index: { mode: '100644', oid: '2'.repeat(40) }, previous: { sha256: '3'.repeat(64), blob_oid: '1234567890abcdef1234567890abcdef12345678', size: 5, perms: 420 } },
      { path: 'gone.txt', index: { mode: '100644', oid: '2'.repeat(40) }, previous: null }] },
    summary: 'Discard unstaged changes in 2 path(s), restoring them to their staged (index) content.', effects: ['Save the current bytes of notes.txt as blob 1234567890ab, then restore it from the index'] };
  shown = review({ kind: 'git', status: 'pending', payload: discardPlan });
  assert.deepEqual(shown.alerts, ['Destructive']);
  includes(shown, ['Git discard', 'Destructive: this removes or rewrites work', 'every permission mode', 'Branchdetached HEAD'], 'discard review');
  assert.deepEqual(shown.lists['Selected paths'], ['notes.txt · discard; current bytes saved first · blob 1234567890ab', 'gone.txt · recreate the deleted file · blob none']);
  assert.ok(!shown.text.includes('Author') && !shown.text.includes('Publication'));

  const pushPlan = { ...commitPlan, operation: 'push', publication: true, message: undefined, paths: undefined, remote: 'origin', branch: 'main', remote_branch: 'main',
    bindings: { head_ref: 'refs/heads/main', head_commit: '4'.repeat(40), refs_digest: '5'.repeat(64), index_digest: '6'.repeat(64), remote: 'origin', url_digest: '0'.repeat(64), remote_path: '/remotes/origin.git', remote_identity: [3, 4], remote_config_digest: '1'.repeat(64), remote_ref: 'refs/heads/main', old_oid: '9'.repeat(40), new_oid: 'a'.repeat(40), source_ref: 'refs/heads/main', exclude: ['9'.repeat(40)], commits: 3, scanned_blobs: 2 },
    summary: 'Publish main (aaaaaaaaaaaa) to origin/main in the local bare repository /remotes/origin.git: fast-forward from 999999999999.', effects: ['Copy the objects of 3 commit(s) into the remote object store (remote hooks never run)'] };
  shown = review({ kind: 'git', status: 'pending', payload: pushPlan });
  assert.deepEqual(shown.alerts, ['Publication']);
  includes(shown, ['Publication: this copies commits into another repository', 'Remoteorigin', 'Local branchmain', 'Remote branchmain', 'Remote URL kindlocal bare repository (network remotes are refused)', 'Remote repository/remotes/origin.git', 'Remote ref movesrefs/heads/main: 999999999999 → aaaaaaaaaaaa', 'Commits copied3'], 'push review');
  assert.ok(!shown.text.includes('New branch'), 'the source branch of a push is not labelled as a new branch');
  shown = review({ kind: 'git', payload: { ...pushPlan, destructive: true } });
  assert.deepEqual(shown.alerts, ['Destructive', 'Publication'], 'both badges show together');
  // Phase 46 (D159): a forced push with a lease, a remote branch deletion, the remote-tracking branch and -u
  shown = review({ kind: 'git', payload: { ...pushPlan, destructive: true, bindings: { ...pushPlan.bindings, force: true, dropped: 2,
    tracking: ['refs/remotes/origin/main', '9'.repeat(40)], upstream: { branch: 'main', remote: 'origin', merge: 'refs/heads/main' } } } });
  includes(shown, ['Forced updateyes: only while the remote still points at 999999999999; 2 remote commit(s) leave the branch',
    'Remote-tracking branchrefs/remotes/origin/main', 'Sets upstreamorigin · refs/heads/main'], 'forced push review');
  shown = review({ kind: 'git', payload: { ...pushPlan, destructive: true, branch: undefined,
    bindings: { ...pushPlan.bindings, new_oid: null, delete: true, commits: 0, exclude: [] } } });
  includes(shown, ['Remote ref movesrefs/heads/main: 999999999999 → absent (deleted)'], 'deletion review');
  assert.ok(!shown.text.includes('Forced update'), 'a deletion is not labelled forced');

  const settledCommit = { ...commitPlan, result: {
    receipt_id: commitPlan.plan_id, operation: 'commit', plan_hash: commitPlan.plan_hash, state: 'applied', phase: 'applied and read back', effect_started: true, broker: { requires_review: true, permission_mode: 'ask' }, steps: ['write-tree'],
    result: { commit: 'c'.repeat(40), tree: 'd'.repeat(40), date: '1 +0000', parent: '4'.repeat(40), target_ref: 'refs/heads/main', identity, index_sync: { 'old.txt': 'kept_changed_entry', 'src/a.ts': 'updated' } },
    readback: { verified: true, checks: [{ fact: 'ref', key: 'refs/heads/main', expected: 'c'.repeat(40), current: 'c'.repeat(40), match: true }, { fact: 'head_commit', key: null, expected: 'c'.repeat(40), current: 'c'.repeat(40), match: true }, { fact: 'index', key: 'old.txt', expected: null, current: 'unavailable', match: false }], commit: 'c'.repeat(40), tree: 'd'.repeat(40), changed_paths: ['old.txt', 'src/a.ts'] },
    summary: commitPlan.summary, destructive: false, publication: false, hooks_run: false, network: false, authorization: 'none' } };
  shown = review({ kind: 'git', status: 'applied', payload: settledCommit });
  includes(shown, ['Receipt readback', 'Receipt stateapplied', 'Phaseapplied and read back', 'New commitcccccccccccc', 'Treedddddddddddd', 'Refrefs/heads/main', 'Effect startedyes', 'Readback verifiedyes', 'Readback checks2 of 3 matched', 'Index sync (index_sync)'], 'settled commit');
  assert.deepEqual(shown.lists['Index sync'], ['old.txt · kept_changed_entry', 'src/a.ts · updated']);
  assert.deepEqual(shown.lists['Changed paths'], ['old.txt', 'src/a.ts']);

  const allocation = { operation: 'allocate_child_worktree', run_id: 'run-1', agent_id: 'agent-1', generation: 2, worktree_id: W1, root: '/work/a', root_identity: [1, 2], fingerprint: '3'.repeat(64), refs: '5'.repeat(64), base_commit: BASE, path: '/managed/' + W1,
    effects: ['Create folder /managed/' + W1, 'Add Git metadata /work/a/.git/worktrees/' + W1], disclosure: 'Detached checkout at the exact HEAD commit; uncommitted project changes are not copied; the child writes only inside this folder, never the project.', requires_review: false, permission_mode: 'ask' };
  shown = review({ kind: 'worktree', status: 'pending', payload: allocation });
  includes(shown, ['Allocate a child worktree', 'Dirty work is not copied', 'uncommitted project changes are not copied', 'Child agentagent-1', 'Generation2', 'Base commitbbbbbbbbbbbb', 'Folder/managed/' + W1, 'Create folder /managed/' + W1], 'allocation');
  shown = review({ kind: 'worktree', status: 'applied', payload: { ...allocation, result: { worktree_id: W1, state: 'active', path: '/managed/' + W1, base_commit: BASE, detached: true, branch_created: false, hooks_run: false, dirty_work_copied: false, parent_status_unchanged: true, parent_index_unchanged: true, origin: 'child_worktree', authorization: 'none' } } });
  includes(shown, ['Stateactive', 'Detached checkoutyes', 'Dirty work copiedno', 'Project status unchangedyes', 'Project index unchangedyes'], 'allocation receipt');
  shown = review({ kind: 'worktree', status: 'pending', payload: { operation: 'retire_child_worktree', run_id: 'run-1', agent_id: 'agent-1', worktree_id: W1, requires_review: false, effects: ['Remove the clean, fully integrated managed worktree ' + W1] } });
  includes(shown, ['Retire a child worktree', 'Remove the clean, fully integrated managed worktree ' + W1], 'retirement');
  assert.ok(!shown.text.includes('Dirty work is not copied') && !shown.text.includes('Base commit'));

  const installPlan = { operation: 'install_environment', kind: 'npm-ci', environment_id: E1, chat_id: 'chat-a', root: '/work/a', root_identity: [1, 2], manifest_digest: '2'.repeat(64), lock_digest: '1'.repeat(64), toolchain: { node: { sha256: '3'.repeat(64) } }, registries: [['registry.npmjs.org', 443]], packages: 12, size_limit: 2 ** 30, timeout: 600, requires_review: true, network: 'allowlisted_registry_only', effects: ['Create the managed folder', 'Run npm ci with install scripts disabled through the allowlist proxy'], summary: 'Install 12 locked npm packages from package-lock.json.', permission_mode: 'ask' };
  shown = review({ kind: 'install', status: 'pending', payload: installPlan });
  includes(shown, ['Install dependencies from the lockfile', 'Install scripts disabled', 'Network: allowlisted registries only', 'Kindnpm-ci', 'Lockfile digest111111111111', 'Manifest digest222222222222', 'Packages12', 'Size limit1 GiB', 'Timeout600 seconds', 'Install 12 locked npm packages'], 'install plan');
  assert.deepEqual(shown.lists['Allowlisted registries'], ['registry.npmjs.org:443']);
  assert.deepEqual(shown.alerts, []);
  shown = review({ kind: 'install', status: 'applied', payload: { ...installPlan, result: { environment_id: E1, state: 'ready', kind: 'npm-ci', path: '/envs/' + E1, exit_code: 0, output: 'added 12 packages', output_truncated: false, size_bytes: 5 * 2 ** 20, file_count: 99, tree_digest: '4'.repeat(64), tree_partial: false, lock_digest: '1'.repeat(64), authorization: 'none',
    egress_log: [{ host: 'registry.npmjs.org', port: 443, allowed: true, reason: 'eof', bytes_up: 1000, bytes_down: 2048000, started: now, ended: now }, { host: 'evil.example', port: 443, allowed: false, reason: 'not_allowlisted', bytes_up: 0, bytes_down: 0, started: now, ended: now }] } } });
  includes(shown, ['Install record', 'Stateready', 'Exit code0', 'Tree digest444444444444', 'Size5 MiB', 'Files99', 'Egress connections2 (1 allowed)'], 'install record');
  assert.deepEqual(shown.lists['Egress log'], ['registry.npmjs.org:443 · allowed · eof · 2 MiB', 'evil.example:443 · refused · not_allowlisted · 0 bytes']);
  shown = review({ kind: 'install', payload: { ...installPlan, network: true } });
  assert.deepEqual(shown.alerts, ['Unexpected network value: true'], 'any other network value is flagged');

  const testsPlan = { root: '/work/a', root_identity: [1, 2], recipe: 'npm-test', drafts: [{ path: 'src/a.ts' }], requires_review: false, permission_mode: 'ask',
    plan: { recipe: 'npm-test', kind: 'test', argv: ['/opt/node/bin/node', '/opt/node/lib/npm-cli.js', '--ignore-scripts', 'run', 'test'], cwd: 'snapshot',
      executable: { path: '/opt/node/bin/node', realpath: '/opt/node/bin/node', sha256: 'e'.repeat(64), size: 1, mtime_ns: 1, dev: 1, ino: 1, uid: 0, mode: 33261 },
      entrypoint: { path: '/opt/node/lib/npm-cli.js', realpath: '/opt/node/lib/npm-cli.js', sha256: 'f'.repeat(64), size: 1, mtime_ns: 1, dev: 1, ino: 2, uid: 0, mode: 33188 },
      toolchain_reads: ['/opt/node'], env: { HOME: '{temp}', PATH: '/opt/node/bin:/usr/bin:/bin', npm_config_ignore_scripts: 'true' }, timeout: 120, output_limit: 192000, network: false, script: 'node --test test/*.mjs',
      mount: { kind: 'node_modules', root: '/envs/' + E1, reads: ['/envs/' + E1], links: { node_modules: '/envs/' + E1 + '/work/node_modules' }, executable: null, env: { NODE_PATH: '/envs/' + E1 + '/work/node_modules' }, environment_id: E1, tree_digest: '4'.repeat(64) } } };
  testsPlan.mount = testsPlan.plan.mount;
  shown = review({ kind: 'tests', status: 'pending', payload: testsPlan });
  includes(shown, ['Test recipe npm-test', 'Recipenpm-test', 'Kindtest', 'Working foldertemporary project snapshot', 'Timeout120 seconds', 'Output limit187.5 KiB', 'Networkdenied', 'Drafts in the snapshot1',
    'HOME{temp}', 'PATH/opt/node/bin:/usr/bin:/bin', 'npm_config_ignore_scriptstrue', 'Executable realpath/opt/node/bin/node', 'Executable SHA-256eeeeeeeeeeee', 'npm entrypoint/opt/node/lib/npm-cli.js', 'npm entrypoint SHA-256ffffffffffff',
    'Read-only environment mount', 'Kindnode_modules', 'Root/envs/' + E1, 'Environment ID' + E1, 'Tree digest444444444444'], 'tests plan');
  assert.deepEqual(shown.pres, ['/opt/node/bin/node /opt/node/lib/npm-cli.js --ignore-scripts run test', 'node --test test/*.mjs']);
  assert.deepEqual(shown.lists['Toolchain reads'], ['/opt/node']);
  assert.deepEqual(shown.lists['Mount reads'], ['Reads /envs/' + E1]);
  assert.deepEqual(shown.lists['Mount links'], ['Links node_modules → /envs/' + E1 + '/work/node_modules']);
  shown = review({ kind: 'tests', status: 'applied', payload: { ...testsPlan, result: { exit_code: 0, output: '# pass 3', output_bytes: 8, output_truncated: false, outcome: 'passed', tests: { passed: 3, failed: 0, skipped: 1, errors: 0, total: 4 }, recipe: 'npm-test', kind: 'test', snapshot_digest: '0123456789abcdef'.repeat(4), snapshot_files: 10, skipped_large: [], skipped_large_count: 2, snapshot_truncated: false, drafts_included: ['src/a.ts'], executable_sha256: 'e'.repeat(64), pinned: true, environment: null } } });
  includes(shown, ['Outcomepassed', 'Testspassed 3 · failed 0 · skipped 1 · errors 0 · total 4', 'Exit code0', 'Snapshot digest0123456789ab', 'Large files skipped2', 'Snapshot truncatedno', 'Drafts included1 (listed below)'], 'tests result');
  assert.deepEqual(shown.lists['Drafts included'], ['src/a.ts']);
  shown = review({ kind: 'tests', payload: { ...testsPlan, plan: { ...testsPlan.plan, argv: ['/opt/python', 'a b', ''], script: null, entrypoint: null, mount: null }, mount: null } });
  assert.deepEqual(shown.pres, ['/opt/python "a b" ""'], 'arguments with spaces stay distinguishable');
  assert.ok(!shown.text.includes('npm entrypoint') && !shown.text.includes('Read-only environment mount'));

  // Phase 14: commands render their reviewed typed plan, or an explicit unplanned notice.
  shown = review({ kind: 'command', payload: { command: 'ls' } });
  includes(shown, ['Unplanned command', 'executable is resolved when it runs'], 'legacy command');
  shown = review({ kind: 'command', payload: { command: 'python3 -c x', plan: { pipeline: 'none', writable: [], network: { mode: 'none' },
    resources: { timeout: 60, output_bytes: 192000 }, plan_hash: 'a'.repeat(64),
    steps: [{ argv: ['/usr/bin/python3', '<img src=x onerror=alert(1)>'], cwd: '.', executable: { realpath: '/usr/bin/python3', sha256: 'b'.repeat(64) } }] } } });
  includes(shown, ['Typed command plan', 'executable pinned at review', '/usr/bin/python3', 'read-only'], 'planned command');
  assert.ok(shown.text.includes('<img src=x onerror=alert(1)>') && !shown.types.has('img'), 'hostile argv stays plain text');

  // Phase 14f: a typed RunPlan shows every grant it asks for, as text.
  shown = review({ kind: 'command', payload: { command: 'plan [["python3","gen.py"],["python3","upper.py"]]', typed: true, plan: {
    pipeline: 'linear', writable: ['out'], process_model: 'tree', interactive: true,
    network: { mode: 'broker', destinations: [['api.example.test', 443]], credentials: [{ ref: 'deploy<b>', destination: ['api.example.test', 443], methods: ['GET', 'POST'], path_prefix: '/v1/' }] },
    resources: { timeout: 120, cpu_seconds: 120, memory_mb: 512, nproc: 64, output_bytes: 192000 }, plan_hash: 'c'.repeat(64),
    steps: [{ argv: ['/usr/bin/python3', 'gen.py'], cwd: '.', env: { MODE: '<script>x</script>' }, stdin: 'none', executable: { realpath: '/usr/bin/python3', sha256: 'd'.repeat(64) } },
            { argv: ['/usr/bin/python3', 'upper.py'], cwd: '.', env: {}, stdin: 'pty', executable: { realpath: '/usr/bin/python3', sha256: 'e'.repeat(64) } }] } } });
  includes(shown, ['Step 2 argv', 'linear', 'reviewed destinations only', 'api.example.test:443', 'Uses saved command credentials',
    'deploy<b> → api.example.test GET/POST /v1/', 'interactive terminal', 'Interactive: the model may type', '512 MB',
    'MODE=<script>x</script>'], 'typed plan');
  assert.ok(!shown.types.has('b') && !shown.types.has('script'), 'plan text stays plain text');

  // Only reviewed kinds render; legacy tests, other kinds and malformed payloads stay inert.
  for (const action of [{ kind: 'tests', payload: { recipe: 'python-unittest', root: '/work/a' } }, { kind: 'edit', payload: { path: 'a.ts' } }, { kind: 'plugin', payload: {} }, { kind: 'tests', payload: { plan: ['not', 'a', 'plan'] } }]) {
    shown = review(action);
    assert.equal(shown.text, '', action.kind + ' renders nothing here');
    assert.ok(!shown.types.has('section'));
  }
  shown = review({ kind: 'git', payload: null });
  includes(shown, ['Git unknown operation', 'hooks_run: not reported as false', 'network: not reported'], 'malformed git payload');
  shown = review({ kind: 'install', payload: 'not an object' });
  includes(shown, ['Unexpected network value: not reported', 'No registry was reported.'], 'malformed install payload');
  shown = review({ kind: 'git', payload: { operation: 'stage', bindings: { paths: 'not a list' }, effects: { not: 'a list' }, sensitive_findings: [{ path: 'a.env', line: null, kind: 'env_file' }, { path: 'k.pem', line: 3, kind: 'private_key' }] } });
  includes(shown, ['Sensitive findings2 (listed below)', 'values are never shown'], 'findings');
  assert.deepEqual(shown.lists['Sensitive findings'], ['a.env · whole file · env_file', 'k.pem · line 3 · private_key']);
  assert.equal(shown.lists['Selected paths'], undefined);

  // Long values are visibly truncated, and long lists are bounded with a count.
  shown = review({ kind: 'git', payload: { ...commitPlan, message: 'x'.repeat(5000), bindings: { ...commitPlan.bindings, paths: Array.from({ length: 70 }, (_, index) => ({ path: 'src/file' + index + '.ts', kind: 'add', blob_oid: 'a'.repeat(40) })) } } });
  assert.ok(shown.pres[0].endsWith('x … [truncated for display: 4000 of 5000 characters shown]'));
  includes(shown, ['Selected paths (70)', '6 more path(s) not shown.'], 'bounded paths');
  assert.equal(shown.lists['Selected paths'].length, 64);

  // Hostile payload strings render as text nodes and escaped server text, never as markup.
  const hostileGit = { ...commitPlan, operation: 'commit', summary: hostile, root: '<svg onload=root()>', message: '<script>message()</script>', branch: '<iframe src=branch>', remote: hostile, sensitive_findings: [{ path: '<script>finding()</script>', line: 1, kind: hostile }], effects: ['<iframe src=effect>'],
    bindings: { ...commitPlan.bindings, head_ref: 'refs/heads/<img src=head>', identity: { name: '<script>name()</script>', email: hostile, source: 'repository' }, paths: [{ path: '<svg onload=path()>', kind: '<script>kind()</script>', blob_oid: hostile }] } };
  const hostileActions = [
    { kind: 'git', payload: hostileGit },
    { kind: 'worktree', payload: { ...allocation, agent_id: '<script>agent()</script>', disclosure: hostile, path: '<iframe src=folder>', effects: ['<svg onload=effect()>'] } },
    { kind: 'install', payload: { ...installPlan, kind: '<script>kind()</script>', registries: [['<img src=registry>', 443]], summary: '<iframe src=summary>', result: { state: 'failed', reason: '<script>reason()</script>', egress_log: [{ host: '<svg onload=host()>', port: 443, allowed: false, reason: hostile, bytes_up: 0, bytes_down: 0 }] } } },
    { kind: 'tests', payload: { ...testsPlan, plan: { ...testsPlan.plan, argv: ['<script>argv()</script>'], script: '<img src=script>', env: { '<svg onload=key()>': '<iframe src=value>' }, toolchain_reads: [hostile] } } },
  ];
  for (const action of hostileActions) {
    shown = review(action);
    for (const type of ['script', 'img', 'svg', 'iframe']) assert.ok(!shown.types.has(type), action.kind + ' payload text never becomes ' + type);
  }
  assert.ok(review(hostileActions[0]).pres.includes('<script>message()</script>'), 'the hostile message is shown literally');
  const reviewHtml = (await Promise.all(hostileActions.map(action => renderToString(createSSRApp(Review, { action }))))).join('\n');
  for (const literal of ['&lt;img src=x onerror=host()&gt;', '&lt;svg onload=root()&gt;', '&lt;script&gt;message()&lt;/script&gt;', '&lt;iframe src=branch&gt;', '&lt;script&gt;finding()&lt;/script&gt;', '&lt;iframe src=effect&gt;', '&lt;img src=head&gt;','&lt;script&gt;name()&lt;/script&gt;', '&lt;svg onload=path()&gt;', '&lt;script&gt;kind()&lt;/script&gt;',
    '&lt;script&gt;agent()&lt;/script&gt;', '&lt;iframe src=folder&gt;', '&lt;svg onload=effect()&gt;', '&lt;img src=registry&gt;:443', '&lt;iframe src=summary&gt;', '&lt;script&gt;reason()&lt;/script&gt;', '&lt;svg onload=host()&gt;:443', '&lt;script&gt;argv()&lt;/script&gt;', '&lt;img src=script&gt;', '&lt;svg onload=key()&gt;', '&lt;iframe src=value&gt;']) {
    assert.ok(reviewHtml.includes(literal), 'review missing literal ' + literal);
  }
  assert.ok(!/<(?:img|script|svg|iframe)\b/i.test(reviewHtml), 'no hostile markup in the review card');

  // The browser preview never reaches native routes.
  bridge.native = false;
  const beforePreview = calls.length, previewRoot = node('root');
  const preview = renderer.createApp(Workflows, { chatId: 'chat-a', workspace: '/work/a' });
  preview.config.errorHandler = error => { throw error; };
  preview.mount(previewRoot); await settle();
  assert.equal(calls.length, beforePreview);
  assert.ok(textOf(previewRoot).includes('available in the Pebble desktop app'));
  assert.ok(!all(previewRoot).some(element => element.type === 'button'), 'the preview has no controls');
  preview.unmount(); bridge.native = true;

  // No in-flight reply or timer survives teardown.
  gate = hold('git_plan_receipts');
  const late = state.loadReceipts(); await settle();
  const listed = state.receipts.value;
  app.unmount(); mounted = false;
  const afterUnmount = calls.length;
  gate.resolve(); await late; await settle();
  assert.equal(calls.length, afterUnmount);
  assert.equal(state.receipts.value, listed, 'a reply after unmount changes nothing');
  assert.equal(timers, 0, 'no timer survives the panel');
  console.log('Coding workflows passed: fixed native routes only; commit, create/switch branch and undo sent to git_plan_propose as review-card proposals with exact payloads (paths as a JSON string, empty branch omitted); local validation before native; host refusals and approval-looking replies shown as errors; read-only readback; retire with discard false, discard only after a separate confirmation with blob IDs shown; environment plan and confirmed removal; stale, older, cross-chat and post-unmount replies ignored; empty states; hostile text escaped; browser preview inert. ActionReview passed: read-only git/worktree/install/tests payloads with destructive and publication badges, hooks_run and network, readback, index_sync, egress and test results, bounded and visibly truncated values, inert for other kinds. Native IPC is simulated; no installed app is exercised.');
} finally {
  if (mounted) app.unmount();
  globalThis.document = previous.document;
  globalThis.Document = previous.Document;
  globalThis.ShadowRoot = previous.ShadowRoot;
  globalThis.setTimeout = previous.setTimeout;
  globalThis.setInterval = previous.setInterval;
}
