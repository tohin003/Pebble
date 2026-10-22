import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, createSSRApp, h, nextTick, reactive } from 'vue';
import { renderToString } from '@vue/server-renderer';
import { transform } from 'sucrase';

// Compile only the application component source. Agent text is never evaluated.
const require = createRequire(import.meta.url);
const calls = [], events = [];
const clone = value => structuredClone(value);
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function treeFor(root, suffix = '') {
  return {
    root_run_id: root,
    nodes: [
      { id: 'coordinator' + suffix, agent_id: 'coordinator' + suffix, run_id: root, parent_id: null, generation: 1, name: 'Coordinator' + suffix, task: 'Coordinate the task.', status: 'running', tools: ['SpawnAgent', 'WaitForAgents'], limits: { max_steps: 24 }, depth: 0, write_paths: [], dependencies: [] },
      { id: 'worker' + suffix, agent_id: 'worker' + suffix, run_id: root, parent_id: 'coordinator' + suffix, generation: 2, name: 'Worker' + suffix, task: 'Inspect the assigned files.', status: 'running', tools: ['Read'], limits: { max_steps: 8, max_seconds: 30, model_tokens: 100 }, depth: 1, write_paths: ['src/'], dependencies: [], review: 'pending', dispatches: 2, charged_tokens: 50, elapsed: 3 },
    ],
    budget: { root_limits: { max_steps: 24, max_seconds: 300, model_tokens: 1000, max_dispatches: 72 }, allocated: { steps: 8, seconds: 30, model_tokens: 100 }, used: { dispatches: 2, model_tokens: 50, child_seconds: 3 }, accounting: 'conservative_dispatch_reservation' },
    runtime: { local_lane: 1, cloud_limit: 3 },
  };
}
const trees = new Map([['root-1', treeFor('root-1')], ['root-2', treeFor('root-2', '-new')]]);
let listBarrier = null, detailBarrier = null, actionBarrier = null, actionError = '', cancelChanges = true;
const bridge = {
  native: true,
  async subscribe() { return () => {}; },
  async rpc(method, params) {
    calls.push({ method, params: clone(params) });
    if (method === 'process_list') return [];
    if (method === 'stream_read') return { events: [], next_after: params.after, more: false };
    if (method === 'agent_list') {
      if (params.run_id === 'missing-root') throw new Error('Root run not found in this conversation.');
      const result = clone(trees.get(params.run_id));
      if (listBarrier?.run === params.run_id) await listBarrier.promise;
      return result;
    }
    if (method === 'agent_get') {
      const node = clone(trees.get(params.run_id).nodes.find(item => item.agent_id === params.agent_id));
      const result = { ...node, result: 'Saved worker result.', evidence_ids: ['receipt-1'], receipts: [{ id: 'receipt-1', tool: 'read_file', status: 'completed' }], messages: [{ role: 'assistant', content: 'Task progress.' }], questions: [{ id: 'question-1', status: 'pending' }], patches: [{ id: 'patch-1', review_status: 'pending' }] };
      if (detailBarrier?.agent === params.agent_id) await detailBarrier.promise;
      return result;
    }
    if (['agent_steer', 'agent_cancel'].includes(method)) {
      if (actionBarrier) await actionBarrier.promise;
      if (actionError) throw new Error(actionError);
      if (method === 'agent_cancel' && cancelChanges) trees.get(params.run_id).nodes.find(item => item.agent_id === params.agent_id).status = 'stopped';
      return { accepted: true };
    }
    throw new Error('Unexpected RPC ' + method);
  },
};
function compile(filename, imports = {}) {
  const source = readFileSync(new URL(filename, import.meta.url), 'utf8');
  const descriptor = parse(source, { filename }).descriptor;
  const script = compileScript(descriptor, { id: filename });
  const template = compileTemplate({ source: descriptor.template.content, filename, id: filename, compilerOptions: { bindingMetadata: script.bindings } });
  assert.deepEqual(template.errors, []);
  function evaluate(code) {
    const module = { exports: {} };
    new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
      name => name === '../bridge' ? bridge : imports[name] || require(name), module, module.exports,
    );
    return module.exports;
  }
  const component = evaluate(script.content).default;
  component.render = evaluate(template.code).render;
  return { component, source };
}
const { component: AgentPanel, source } = compile('./AgentPanel.vue');
const { component: ExecutionOutput } = compile('./ExecutionOutput.vue');
const { component: AttemptStream } = compile('./AttemptStream.vue');
const { component: ReconcilePanel } = compile('./ReconcilePanel.vue');
const { component: RunPanel } = compile('./RunPanel.vue', {
  './AgentPanel.vue': { __esModule: true, default: AgentPanel },
  './ExecutionOutput.vue': { __esModule: true, default: ExecutionOutput },
  './AttemptStream.vue': { __esModule: true, default: AttemptStream },
  './ReconcilePanel.vue': { __esModule: true, default: ReconcilePanel },
});
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|<iframe/.test(source), 'agent records must be literal native content');

// The host runs real Vue hooks, watchers, input directives and button handlers.
// It has no HTML parser, browser capability API or agent script evaluator.
function node(type, text = '') {
  return { type, tagName: type.toUpperCase(), text, children: [], props: {}, parent: null, value: '', addEventListener() {}, removeEventListener() {}, getRootNode() { return globalThis.document; } };
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
const previous = { document: globalThis.document, Document: globalThis.Document, ShadowRoot: globalThis.ShadowRoot, setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
const timers = new Map();
let timerId = 0;
globalThis.Document = class Document { activeElement = null; };
globalThis.ShadowRoot = class ShadowRoot {};
globalThis.document = new globalThis.Document();
globalThis.setTimeout = (callback, delay) => { assert.ok([150, 2000].includes(delay), 'polling and stream coalescing must use bounded intervals'); const id = ++timerId; timers.set(id, callback); return id; };
globalThis.clearTimeout = id => timers.delete(id);
const textOf = element => (element.type === '#comment' ? '' : element.text) + element.children.map(textOf).join('');
const all = element => [element, ...element.children.flatMap(all)];
async function settle() { for (let index = 0; index < 8; index++) { await Promise.resolve(); await nextTick(); } }
const props = reactive({ chatId: 'chat-a', runId: 'root-1', runState: 'running', refreshKey: '1' });
const root = node('root');
let state;
const instrumented = { ...AgentPanel, setup(properties, context) { state = AgentPanel.setup(properties, context); return state; } };
const app = renderer.createApp({ setup: () => () => h(instrumented, { ...props, onRefresh: () => events.push('refresh') }) });
app.config.errorHandler = error => { throw error; };
function button(label) {
  const found = all(root).find(element => element.type === 'button' && textOf(element) === label);
  assert.ok(found, 'Missing button ' + label);
  return found;
}
async function click(label) { const target = button(label); assert.ok(!target.props.disabled, label + ' must be enabled'); await target.props.onClick(); await settle(); }
function disabled(label, value = true) { assert.equal(!!button(label).props.disabled, value, label); }
function paramsFor(method) { return calls.filter(call => call.method === method).at(-1)?.params; }
let mounted = false;

try {
  app.mount(root); mounted = true;
  await settle();
  assert.deepEqual(paramsFor('agent_list'), { chat_id: 'chat-a', run_id: 'root-1' });
  assert.equal(timers.size, 1);
  assert.ok(textOf(root).includes('Local execution lanes: 1 · Cloud agent limit: 3'));
  assert.ok(textOf(root).includes('Generation 2 · Tools: Read · Review: pending'));
  assert.ok(textOf(root).includes('used · dispatches2') && textOf(root).includes('root limits · max steps24'));
  const listsBeforeParentUpdate = calls.filter(call => call.method === 'agent_list').length;
  props.refreshKey = 'updated-parent-chat'; await settle();
  assert.equal(calls.filter(call => call.method === 'agent_list').length, listsBeforeParentUpdate + 1, 'parent chat updates refresh the tree');
  await click('Worker');
  assert.deepEqual(paramsFor('agent_get'), { chat_id: 'chat-a', run_id: 'root-1', agent_id: 'worker' });
  assert.ok(textOf(root).includes('Saved worker result.') && textOf(root).includes('receipt-1'));
  disabled('Send input');

  state.input.value = '  Keep the task in its assigned scope.  ';
  await settle();
  actionBarrier = deferred();
  const sending = state.act('agent_steer');
  await settle();
  disabled('Sending…'); disabled('Cancel agent and descendants'); disabled('Worker');
  assert.deepEqual(paramsFor('agent_steer'), { chat_id: 'chat-a', run_id: 'root-1', agent_id: 'worker', text: 'Keep the task in its assigned scope.' });
  assert.ok(!('origin' in paramsFor('agent_steer')) && !('approval' in paramsFor('agent_steer')));
  assert.equal(state.input.value, '  Keep the task in its assigned scope.  ');
  actionBarrier.resolve(); await sending; actionBarrier = null; await settle();
  assert.equal(state.input.value, '');
  assert.equal(events.length, 1);
  assert.equal(state.notice.value, 'Input sent.');
  assert.equal(state.selectedNode.value.status, 'running');

  actionError = 'Task input could not be stored.';
  state.input.value = 'Keep this input for retry.';
  await state.act('agent_steer'); await settle();
  assert.equal(state.input.value, 'Keep this input for retry.');
  assert.ok(textOf(root).includes(actionError));
  assert.equal(state.busy.value, ''); assert.equal(events.length, 1);
  actionError = 'Cancellation failed.';
  await state.act('agent_cancel'); await settle();
  assert.equal(state.selectedNode.value.status, 'running');
  assert.ok(textOf(root).includes('Cancellation failed.'));
  actionError = ''; actionBarrier = deferred();
  const cancelling = state.act('agent_cancel'); await settle();
  disabled('Cancelling…'); disabled('Send input');
  assert.equal(state.selectedNode.value.status, 'running', 'cancellation is not optimistic');
  assert.equal(state.notice.value, '');
  assert.deepEqual(paramsFor('agent_cancel'), { chat_id: 'chat-a', run_id: 'root-1', agent_id: 'worker' });
  actionBarrier.resolve(); await cancelling; actionBarrier = null; await settle();
  assert.equal(state.selectedNode.value.status, 'stopped');
  assert.ok(textOf(root).includes('saved record is read only'));
  assert.equal(all(root).filter(element => element.type === 'textarea').length, 0);

  trees.get('root-1').nodes[0].status = 'completed';
  props.runState = 'completed'; await settle();
  assert.equal(timers.size, 0, 'finished task and nodes stop polling');
  trees.get('root-1').nodes[0].status = 'running'; props.runState = 'running'; await settle();
  assert.equal(timers.size, 1);
  const [pollId, poll] = [...timers][0]; timers.delete(pollId); poll(); await settle();
  assert.equal(timers.size, 1, 'each poll schedules only one next poll');

  // A pending list from a previous task cannot replace the new task's tree.
  listBarrier = { ...deferred(), run: 'root-1' };
  const oldList = state.load(); await settle();
  props.runId = 'root-2'; props.chatId = 'chat-b'; await settle();
  assert.equal(state.tree.value.root_run_id, 'root-2');
  assert.equal(state.selected.value, ''); assert.equal(state.input.value, '');
  listBarrier.resolve(); await oldList; listBarrier = null; await settle();
  assert.equal(state.tree.value.root_run_id, 'root-2');
  assert.deepEqual(paramsFor('agent_list'), { chat_id: 'chat-b', run_id: 'root-2' });

  // The same refusal applies to details and mutation acknowledgements.
  props.runId = 'root-1'; props.chatId = 'chat-a'; await settle();
  detailBarrier = { ...deferred(), agent: 'worker' };
  state.choose(state.tree.value.nodes[1]); await settle();
  props.runId = 'root-2'; props.chatId = 'chat-b'; await settle();
  detailBarrier.resolve(); detailBarrier = null; await settle();
  assert.equal(state.detail.value, null);
  await click('Worker-new');
  assert.deepEqual(paramsFor('agent_get'), { chat_id: 'chat-b', run_id: 'root-2', agent_id: 'worker-new' });
  state.input.value = 'Input for the new task.'; await settle();
  actionBarrier = deferred(); const oldAction = state.act('agent_steer'); await settle();
  props.runId = 'root-1'; props.chatId = 'chat-a'; await settle();
  const refreshCount = events.length;
  actionBarrier.resolve(); await oldAction; actionBarrier = null; await settle();
  assert.equal(events.length, refreshCount, 'late action acknowledgement does not refresh another task');
  assert.equal(state.notice.value, ''); assert.equal(state.selected.value, '');

  // Unknown status strings are bounded and cannot enable action controls.
  trees.get('root-1').nodes[1].status = '<img src=x onerror=host()>';
  await state.load(); await settle(); await click('Worker');
  assert.equal(state.controllable.value, false);
  assert.ok(textOf(root).includes('unknown'));
  assert.equal(all(root).filter(element => element.type === 'textarea').length, 0);
  trees.get('root-1').nodes[1].status = 'interrupted'; await state.load(); await settle();
  assert.equal(state.controllable.value, true, 'resumable agent can be cancelled');
  assert.equal(state.steerable.value, false, 'task input only targets live agents');
  assert.equal(all(root).filter(element => element.type === 'textarea').length, 0);
  trees.get('root-1').nodes[1].status = 'waiting_parent'; await state.load(); await settle();
  assert.equal(state.steerable.value, true, 'parent-waiting child accepts ordinary task input');
  trees.get('root-1').nodes[1].status = 'completed'; await state.load(); await settle();
  const actionCalls = calls.filter(call => ['agent_steer', 'agent_cancel'].includes(call.method)).length;
  await state.act('agent_cancel'); await state.act('agent_steer');
  assert.equal(calls.filter(call => ['agent_steer', 'agent_cancel'].includes(call.method)).length, actionCalls);

  // Native scope validation rejects even a successful but mismatched IPC response.
  const savedTree = trees.get('root-1');
  trees.set('root-1', { ...savedTree, root_run_id: 'wrong-root' });
  await state.load(); await settle();
  assert.ok(state.error.value.includes('different task'));
  assert.equal(timers.size, 0, 'IPC errors wait for explicit refresh');
  trees.set('root-1', savedTree); await state.load(); await settle();
  assert.equal(state.error.value, '');

  const hostile = '<img src=x onerror=host()>';
  const maliciousTree = treeFor('malicious-root');
  maliciousTree.nodes[1] = { ...maliciousTree.nodes[1], name: hostile, task: '<script>host.permission()</script>', tools: ['<svg onload=host()>'], result: '<iframe src=host>', evidence_ids: ['<script>receipt()</script>'], status: 'completed', review: '<svg onload=approve()>' };
  const seeded = {
    ...AgentPanel,
    setup(properties, context) {
      const bindings = AgentPanel.setup(properties, context);
      bindings.tree.value = maliciousTree;
      bindings.selected.value = 'worker';
      bindings.detail.value = { ...maliciousTree.nodes[1], receipts: [{ id: hostile, permission: '<script>approve()</script>' }] };
      return bindings;
    },
  };
  const html = await renderToString(createSSRApp(seeded, { chatId: 'chat-a', runId: 'malicious-root' }));
  assert.ok(html.includes('&lt;img src=x onerror=host()&gt;'));
  assert.ok(html.includes('&lt;script&gt;host.permission()&lt;/script&gt;'));
  assert.ok(html.includes('&lt;iframe src=host&gt;') && html.includes('&lt;script&gt;receipt()&lt;/script&gt;'));
  assert.ok(!/<(?:img|script|svg|iframe)\b/.test(html));
  assert.ok(!html.includes('Send input') && !html.includes('Cancel agent and descendants'));

  // Child nodes stay separate from runs; their questions use the native root form.
  const chat = {
    id: 'chat-integration', title: 'Fixture', workspace: null, messages: [], actions: [],
    runs: [{ id: 'root-1', state: 'running' }],
    questions: [{ id: 'child-question', run_id: 'root-1', revision: 1, required: true, status: 'pending', requester_agent_id: 'worker', requester_agent_name: hostile, parent_task_id: 'coordinator', origin: 'agent', items: [{ id: 'scope', title: 'Choose scope', options: [{ id: 'local', label: 'This folder' }], allow_free_text: true }] }],
  };
  const questionHtml = await renderToString(createSSRApp(RunPanel, { chat }));
  assert.ok(questionHtml.includes('Requested by &lt;img src=x onerror=host()&gt;'));
  assert.ok(questionHtml.includes('Task: coordinator') && questionHtml.includes('Submit answer'));
  assert.ok(questionHtml.includes('This agent needs your answer to continue. Other agents can keep working.'));
  assert.ok(!questionHtml.includes('<img'));
  const integrationRoot = node('root');
  const integration = renderer.createApp(RunPanel, { chat }); integration.mount(integrationRoot); await settle();
  assert.deepEqual(paramsFor('agent_list'), { chat_id: 'chat-integration', run_id: 'root-1' });
  integration.unmount(); await settle();

  // Phase 22 (log noise): a queued run is listed once it runs, and a run the host does not know in this
  // conversation is not asked about again on every chat refresh; only Refresh agents asks again.
  {
    const lists = () => calls.filter(call => call.method === 'agent_list').map(call => call.params.run_id);
    const start = lists().length, baseTimers = timers.size;
    const noise = reactive({ chatId: 'chat-a', runId: 'root-1', runState: 'queued', refreshKey: 'a' });
    const noiseRoot = node('root');
    let noiseState;
    const noisy = { ...AgentPanel, setup(properties, context) { noiseState = AgentPanel.setup(properties, context); return noiseState; } };
    const noiseApp = renderer.createApp({ setup: () => () => h(noisy, noise) });
    noiseApp.mount(noiseRoot); await settle();
    assert.deepEqual(lists().slice(start), [], 'a queued run is not listed yet');
    noise.runState = 'running'; await settle();
    assert.deepEqual(lists().slice(start), ['root-1'], 'listed once it runs');
    noise.runId = 'missing-root'; await settle();
    assert.deepEqual(lists().slice(start), ['root-1', 'missing-root']);
    assert.ok(noiseState.error.value.includes('Root run not found'));
    for (const key of ['b', 'c', 'd']) { noise.refreshKey = key; await settle(); }
    noise.runState = 'waiting_approval'; await settle();
    assert.deepEqual(lists().slice(start), ['root-1', 'missing-root'], 'chat refreshes do not repeat a refused request');
    assert.equal(timers.size, baseTimers, 'and nothing polls it');
    all(noiseRoot).find(element => element.type === 'button' && textOf(element) === 'Refresh agents').props.onClick(); await settle();
    assert.deepEqual(lists().slice(start), ['root-1', 'missing-root', 'missing-root'], 'Refresh agents asks again');
    noise.runId = 'root-1'; await settle();
    assert.equal(lists().at(-1), 'root-1', 'another run is asked about as usual');
    noiseApp.unmount(); await settle();
    assert.equal(timers.size, baseTimers, 'its poll ends with it');
  }

  // No timer or in-flight response can survive component teardown.
  listBarrier = { ...deferred(), run: 'root-1' };
  const afterUnmount = state.load(); await settle();
  app.unmount(); mounted = false;
  assert.equal(timers.size, 0);
  listBarrier.resolve(); await afterUnmount; listBarrier = null; await settle();
  assert.equal(timers.size, 0);
  assert.equal(events.length, refreshCount);
  console.log('Agent panel fixtures passed: scoped native RPC, literal tree/detail rendering, bounded status and polling, steering/cancellation busy and error states, stale replies and teardown, native run integration and requester labels. IPC is simulated; no agent content executes.');
} finally {
  if (mounted) app.unmount();
  globalThis.document = previous.document;
  globalThis.Document = previous.Document;
  globalThis.ShadowRoot = previous.ShadowRoot;
  globalThis.setTimeout = previous.setTimeout;
  globalThis.clearTimeout = previous.clearTimeout;
}
