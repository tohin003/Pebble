import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, createSSRApp, h, nextTick, reactive } from 'vue';
import { renderToString } from '@vue/server-renderer';
import { transform } from 'sucrase';

// Phase 20 (C20-context): the context panel reaches only context_inspect and context_compact, never names a
// window, draws the window as a bar of host categories with free space, lists the largest tool schemas, renders
// every name as literal text, and compacts only after an explicit confirmation, between turns.
const require = createRequire(import.meta.url);
const calls = [], failures = new Map(), held = new Map();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const hostile = '<img src=x onerror=alert(1)>';
const CATS = ['system', 'tool_schemas', 'memory', 'project_host', 'conversation', 'tool_results', 'framing'];
const TOOLS = [['Read', 420], ['Edit', 390], ['Write', 300], ['Glob', 250], ['Grep', 240], ['SpawnAgent', 230],
  ['RunTests', 200], [hostile, 150], ['UpdatePlan', 120], ['AskUserQuestion', 90]];

function inspection(extra = {}) {
  const tokens = { system: 480, tool_schemas: TOOLS.reduce((s, [, n]) => s + n, 0), memory: 0, project_host: 381, conversation: 394, tool_results: 1743, framing: 220 };
  const total = Object.values(tokens).reduce((s, n) => s + n, 0);
  return {
    chat_id: 'chat-a', run_id: 'run-1', latest_run_id: 'run-1', run_state: 'budget_exhausted', steps: 4, basis: 'last_request', live: false,
    counter: 'o200k_base', protocol: 'chat', total, used: total, calibration: { multiplier: 1, tokens: 0 }, free: 32704 - total, over: 0,
    categories: CATS.map(id => ({ id, tokens: tokens[id], messages: { system: 1, project_host: 3, conversation: 4, tool_results: 4 }[id] || 0,
      parts: id === 'conversation' ? [{ id: 'request', tokens: 60, messages: 1 }, { id: 'constraints', tokens: 0, messages: 0 }, { id: 'history', tokens: 90, messages: 2 }, { id: 'model_turns', tokens: 244, messages: 1 }]
        : id === 'project_host' ? [{ id: 'evidence', tokens: 181, messages: 2 }, { id: 'sources', tokens: 200, messages: 1 }, { id: 'inbox', tokens: 0, messages: 0 }] : [] })),
    tools: TOOLS.map(([name, n]) => ({ name, tokens: n })),
    window: { tokens: 32704, context: 32768, output_reserve: 64, source: 'last_projection', route_budget: 32768 },
    tools_basis: 'last_request', tools_unidentified: 0,
    recorded: { status: 'complete', input_tokens: total, before_retirement: total, method: 'o200k_local_estimate', omitted_messages: 0, compacted: false, step: 4 },
    since_last_request: { messages: 2, tokens: 640 }, compactions: 0, compact: { available: true, reason: null },
    omissions: [], authorization: 'none', inference: 'none', ...extra,
  };
}
let current = inspection();
const bridge = {
  native: true,
  async rpc(method, params = {}) {
    calls.push({ method, params: clone(params) });
    const error = failures.get(method);
    failures.delete(method);
    if (error) throw new Error(error);
    if (held.has(method)) { const gate = held.get(method); held.delete(method); await gate; }
    if (method === 'context_inspect') return typeof current === 'function' ? current(params) : { ...clone(current), run_id: params.run_id };
    if (method === 'context_compact') {
      return { receipt_id: 'f'.repeat(32), status: 'compacted', run_id: 'run-1', chat_id: params.chat_id, generation: 1,
        archived: { messages: 9, tool_results: 3, by_category: { tool_results: { messages: 3, tokens: 1200 } } },
        tokens: { before: 4000, after: 2100, freed: 1900 }, replayed: 0, inference: 'none', memory: 'unchanged', authorization: 'none',
        context: inspection({ basis: 'checkpoint', total: 2100, used: 2100, free: 30604, compactions: 1, since_last_request: null }) };
    }
    throw new Error('Unexpected RPC ' + method);
  },
};
function evaluate(code, imports = {}) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => (name === '../bridge' ? bridge : imports[name] || require(name)), module, module.exports);
  return module.exports;
}
const shortcuts = evaluate(readFileSync(new URL('../shortcuts.ts', import.meta.url), 'utf8'));
const source = readFileSync(new URL('./ContextPanel.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'ContextPanel.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'context-panel' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'ContextPanel.vue', id: 'context-panel', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
const Panel = evaluate(script.content, { '../shortcuts': shortcuts }).default;
Panel.render = evaluate(template.code).render;

// Static contract: literal text only, two routes, no window named, host chrome only, motion only when welcome.
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc/.test(source), 'names render as literal text');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(\s*([^,)]*)/g)].map(match => match[1].trim());
assert.deepEqual([...new Set(methods)].sort(), ["'context_compact'", "'context_inspect'"], 'only the two context routes');
assert.ok(!/surface/.test(source.slice(source.indexOf('<script'), source.indexOf('</script>'))), 'the shell, never the panel, names the window');
for (const surface of ['../Pet.vue', './PetRunSummary.vue', './NativeArtifactRenderer.vue', './ArtifactPanel.vue', './ModsSettings.vue',
  './DashboardRenderer.vue', './PrototypeRenderer.vue', './QuizRenderer.vue'])
  assert.ok(!/ContextPanel|context_compact|context_inspect/.test(readFileSync(new URL(surface, import.meta.url), 'utf8')), surface + ' never reaches the panel or its routes');
const style = descriptor.styles.map(block => block.content).join('\n');
const transitions = [...style.matchAll(/transition:\s*([^;]+);/g)].map(m => m[1].trim());
assert.deepEqual(transitions, ['width 200ms ease-out', 'none', 'none'], 'one width transition, and none under reduced motion');
assert.ok(/@media \(prefers-reduced-motion: no-preference\) \{ \.context-seg \{ transition: width 200ms ease-out; \} \}/.test(style), 'the transition is opt-in');
assert.ok(/@media \(prefers-reduced-motion: reduce\) \{ \.context-seg \{ transition: none; \} \}/.test(style));
assert.ok(/:global\(\.reduced-motion\) \.context-seg \{ transition: none; \}/.test(style), "the app's own Reduce motion switch wins too");
assert.ok(!/@keyframes|animation:/.test(style), 'no animation');

const focused = [];
function node(type, text = '') {
  return { type, text, children: [], props: {}, parent: null, addEventListener() {}, removeEventListener() {}, focus() { focused.push(this); } };
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
const tables = () => all(root).filter(element => element.type === 'table');
const caption = table => textOf(table.children.find(child => child.type === 'caption'));
const rowsOf = table => all(table).filter(element => element.type === 'tr' && element.parent?.type === 'tbody');
const segments = () => all(all(root).find(element => element.props.role === 'img')).filter(element => element.type === 'span');
const count = method => calls.filter(call => call.method === method).length;

// Mounting inspects this task once; nothing is compacted, and no window is named.
app.mount(root);
await settle();
assert.deepEqual(calls, [{ method: 'context_inspect', params: { chat_id: 'chat-a', run_id: 'run-1' } }]);
let page = textOf(root);
assert.ok(page.includes('Context use · task run-1') && page.includes('never message text') && page.includes('Nothing here asks the model.'));
assert.ok(page.includes('The last request this task sent to the model.'));
const used = inspection().used;
assert.ok(page.includes(`${used.toLocaleString()} of 32,704 tokens · ${(32704 - used).toLocaleString()} free`));

// The bar: the non-empty categories in host order, then free space; together they fill the window.
const kind = segment => segment.props.class.split(' ')[1];  // Vue normalizes the class list to 'context-seg context-seg-<id>'
assert.deepEqual(segments().map(kind), ['system', 'tool_schemas', 'project_host', 'conversation', 'tool_results', 'framing', 'free'].map(id => 'context-seg-' + id));
const widths = segments().map(s => parseFloat(s.props.style.width));
assert.ok(Math.abs(widths.reduce((a, b) => a + b, 0) - 100) < 0.1, 'categories plus free space fill the bar');
const bar = all(root).find(element => element.props.role === 'img');
assert.ok(bar.props['aria-label'].startsWith(`Context use: ${used.toLocaleString()} of 32,704 tokens. System prompt 480; Tool descriptions 2,390;`));
assert.ok(bar.props['aria-label'].includes('Free space ' + (32704 - used).toLocaleString()) && !bar.props['aria-label'].includes('Memory recall'));

// Tables: every category with its share; the conversation and host parts that hold messages; the largest schemas.
assert.deepEqual(tables().map(caption), ['By category (tokens)', 'Conversation and Pebble messages', 'Largest tool descriptions']);
const [categories, parts, tools] = tables();
assert.deepEqual(rowsOf(categories).map(row => textOf(row.children[0])), ['System prompt', 'Tool descriptions', 'Memory recall', 'Project and Pebble messages',
  'Conversation', 'Tool results', 'Estimate margin and framing', 'Free space']);
assert.deepEqual(rowsOf(categories)[1].children.slice(1).map(textOf), ['2,390', '7.3%', '10 tools']);
assert.deepEqual(rowsOf(categories)[5].children.slice(1).map(textOf), ['1,743', '5.3%', '4']);
assert.deepEqual(rowsOf(parts).map(row => textOf(row.children[0])), ['Task checkpoint and evidence', 'Project files and attachments',
  'Your request', 'Earlier turns', "Pebble's replies and tool calls"], 'parts in host category order; empty parts are left out');
assert.deepEqual(rowsOf(tools).map(row => [textOf(row.children[0]), textOf(row.children[1])]), TOOLS.slice(0, 8).map(([name, n]) => [name, n.toLocaleString()]));
page = textOf(root);
assert.ok(page.includes('And 2 more tools, 210 tokens.'));
assert.ok(page.includes("Pebble's own count when it sent this window (step 4): " + used.toLocaleString() + " tokens, by Pebble's own estimate."));
assert.ok(page.includes('Added since: 2 messages, 640 tokens. They join the next request.') && page.includes('Context archives for this task: 0.'));
assert.ok(!all(root).some(element => element.type === 'img'), 'a hostile tool name stays plain text');
for (const table of tables()) {
  const rows = rowsOf(table);
  assert.deepEqual(rows.map(row => row.props.tabindex), rows.map((_, i) => (i ? -1 : 0)), caption(table) + ' has one tab stop');
  const body = { children: [] };
  for (const tabIndex of [0, -1]) body.children.push({ tagName: 'TR', parentElement: body, tabIndex, focused: 0, focus() { this.focused++; } });
  rows[0].props.onKeydown({ key: 'ArrowDown', target: body.children[0], preventDefault() {} });
  assert.equal(body.children[1].focused, 1, caption(table) + ' rows are keyboard-navigable');
}

// Compact needs a confirmation: opening and cancelling it makes no request; focus follows the dialog.
await button('Compact…').props.onClick({});
await settle();
assert.equal(count('context_compact'), 0);
const group = all(root).find(element => element.props.role === 'group');
assert.equal(group.props['aria-label'], 'Confirm compaction');
assert.ok(textOf(group).includes('Nothing runs, no tool is repeated and memory is unchanged.') && textOf(group).includes('keeps your exact instructions'));
assert.equal(textOf(focused.at(-1)).trim(), 'Compact now', 'focus moves to the confirmation');
await button('Cancel').props.onClick({});
await settle();
assert.ok(!all(root).some(element => element.props.role === 'group') && count('context_compact') === 0);
assert.equal(textOf(focused.at(-1)).trim(), 'Compact…', 'focus returns to the command');

// Confirming sends the conversation only (the host compacts its latest task) and shows the receipt and new window.
await button('Compact…').props.onClick({});
await settle();
await button('Compact now').props.onClick({});
await settle();
assert.deepEqual(calls.filter(call => call.method === 'context_compact').map(call => call.params), [{ chat_id: 'chat-a' }]);
page = textOf(root);
assert.ok(page.includes('Archived 9 messages (3 tool results) and freed 1,900 tokens.') && page.includes('Receipt ffffffff.'));
assert.ok(page.includes('The stored window the next request starts from') && page.includes('2,100 of 32,704 tokens · 30,604 free'));
assert.ok(page.includes('Context archives for this task: 1.') && !page.includes('Added since'));
assert.deepEqual(events, ['refresh']);
assert.equal(count('context_inspect'), 1, 'the receipt carries the new window; no second read was needed');

// A refused compaction is reported; nothing claims success.
failures.set('context_compact', 'A task is running. Compact between turns: wait for it to finish, or pause it first.');
await button('Compact…').props.onClick({});
await settle();
await button('Compact now').props.onClick({});
await settle();
assert.equal(events.at(-1), 'error:Error: A task is running. Compact between turns: wait for it to finish, or pause it first.');
assert.ok(!textOf(root).includes('Compacting…') && button('Compact…').props.disabled === false);

// Not compactable: the reason is shown, the command is disabled and described by it.
const reasons = { live: 'Compact between turns: pause the task or wait for it to finish.', finished: 'This task is finished, so its window is not used again.',
  not_latest: "Only the conversation's latest task can be compacted.", between_tools: 'Resume it to a model boundary before compacting.' };
for (const [reason, text] of Object.entries(reasons)) {
  current = inspection({ compact: { available: false, reason }, live: reason === 'live', run_state: reason === 'live' ? 'running' : 'completed' });
  await button('Refresh').props.onClick({});
  await settle();
  const command = button('Compact…');
  assert.equal(command.props.disabled, true, reason);
  assert.equal(command.props['aria-describedby'], 'context-compact-reason');
  assert.ok(textOf(all(root).find(element => element.props.id === 'context-compact-reason')).includes(text), reason);
  await command.props.onClick({});
  await settle();
  assert.ok(!all(root).some(element => element.props.role === 'group'), 'a disabled command never opens the confirmation');
}
current = inspection({ compact: { available: false, reason: 'live' }, live: true, run_state: 'running', since_last_request: { messages: 0, tokens: 0 } });
await button('Refresh').props.onClick({});
await settle();
assert.ok(textOf(root).includes('The request being answered now.') && !textOf(root).includes('Added since'));

// Over the window, a calibrated route and an offer without a recorded request are all stated, never hidden.
current = inspection({ used: 40000, total: 36000, calibration: { multiplier: 1.12, tokens: 4000 }, free: 0, over: 40000 - 32704,
  tools_basis: 'eligible_offer', tools_unidentified: 2, recorded: null, basis: 'checkpoint', since_last_request: null });
await button('Refresh').props.onClick({});
await settle();
page = textOf(root);
assert.ok(page.includes('40,000 of 32,704 tokens · 7,296 over the window'));
assert.ok(page.includes('these are the tools this task could be offered') && page.includes('2 tool descriptions sent earlier are no longer stored'));
assert.ok(segments().some(s => kind(s) === 'context-seg-calibration') && !segments().some(s => kind(s) === 'context-seg-free'));
assert.ok(segments().every(s => parseFloat(s.props.style.width) <= 100));
assert.ok(rowsOf(tables()[0]).some(row => textOf(row).startsWith('Route calibration4,000')));
current = inspection({ basis: 'none', compact: { available: false, reason: 'no_window' } });
await button('Refresh').props.onClick({});
await settle();
assert.ok(textOf(root).includes('This task has not sent a request yet.') && tables().length === 0);

// Replies for another task are refused; a slower reply for a task the person left never replaces the newer one.
current = params => ({ ...inspection(), run_id: 'run-other' });
await button('Refresh').props.onClick({});
await settle();
assert.equal(events.at(-1), 'error:Error: The reply belongs to a different task.');
assert.ok(!all(root).some(element => element.props.role === 'img'));
current = inspection();
let release;
held.set('context_inspect', new Promise(resolve => { release = resolve; }));
const slow = button('Refresh').props.onClick({});  // held: not awaited until released
await settle();
assert.equal(button('Loading…').props.disabled, true, 'one read at a time');
props.runId = 'run-2';
await settle();
release();
await slow;
await settle();
assert.deepEqual(calls.filter(call => call.method === 'context_inspect').slice(-2).map(call => call.params.run_id), ['run-1', 'run-2']);
assert.ok(textOf(root).includes('Context use · task run-2') && segments().length > 0);
assert.ok(calls.every(call => !('surface' in call.params)), 'no request ever names a window');
app.unmount();

// The parent's RunPanel patch (track note), applied to a copy until RunPanel.vue carries it: the panel opens on
// demand from the run panel, for the current task, and loads only then.
const RUN_PANEL_PATCH = [
  ["const RewindPanel = defineAsyncComponent(() => import('./RewindPanel.vue'));",
    "const RewindPanel = defineAsyncComponent(() => import('./RewindPanel.vue'));\nconst ContextPanel = defineAsyncComponent(() => import('./ContextPanel.vue'));"],
  ["const inspect = ref<'' | 'context' | 'memory' | 'rewind'>('');", "const inspect = ref<'' | 'context' | 'usage' | 'memory' | 'rewind'>('');"],
  [`@click="inspect = inspect === 'context' ? '' : 'context'">Context</button>`,
    `@click="inspect = inspect === 'context' ? '' : 'context'">Context</button>\n      <button type="button" class="secondary-button" :aria-expanded="inspect === 'usage'" aria-controls="run-inspector" @click="inspect = inspect === 'usage' ? '' : 'usage'">Context use</button>`],
  [`      <MemoryImpactView v-else-if="inspect === 'memory'"`,
    `      <ContextPanel v-else-if="inspect === 'usage'" :chat-id="chat.id" :run-id="current.id" @refresh="emit('refresh')" @error="emit('error', $event)" />\n      <MemoryImpactView v-else-if="inspect === 'memory'"`],
];
let runPanel = readFileSync(new URL('./RunPanel.vue', import.meta.url), 'utf8');
if (!runPanel.includes('ContextPanel')) {
  for (const [anchor, replacement] of RUN_PANEL_PATCH) {
    assert.equal(runPanel.split(anchor).length, 2, 'the RunPanel patch anchor moved: ' + anchor);
    runPanel = runPanel.replace(anchor, replacement);
  }
}
assert.ok(/defineAsyncComponent\(\(\) => import\('\.\/ContextPanel\.vue'\)\)/.test(runPanel), 'the panel loads only when opened');
const runDescriptor = parse(runPanel, { filename: 'RunPanel.vue' }).descriptor;
const runScript = compileScript(runDescriptor, { id: 'run-panel' });
const runTemplate = compileTemplate({ source: runDescriptor.template.content, filename: 'RunPanel.vue', id: 'run-panel', compilerOptions: { bindingMetadata: runScript.bindings } });
assert.deepEqual(runTemplate.errors, []);
const loaded = [];
const stub = name => ({ __esModule: true, default: { name, props: ['chatId', 'runId', 'runState', 'refreshKey'], setup: () => () => h('div', name) } });
const runImports = { './AgentPanel.vue': stub('AgentPanel'), './ExecutionOutput.vue': stub('ExecutionOutput'), './AttemptStream.vue': stub('AttemptStream'),
  './ReconcilePanel.vue': stub('ReconcilePanel'), './ContextPanel.vue': { __esModule: true, get default() { loaded.push('ContextPanel'); return Panel; } } };
const RunPanel = evaluate(runScript.content, runImports).default;
RunPanel.render = evaluate(runTemplate.code, runImports).render;
calls.length = 0; events.length = 0; current = inspection();
const runRoot = node('root');
const runApp = renderer.createApp({ setup: () => () => h(RunPanel, { chat: { id: 'chat-a', runs: [{ id: 'run-1', state: 'budget_exhausted', error: null }], questions: [] } }) });
runApp.config.errorHandler = error => { throw error; };
runApp.mount(runRoot);
await settle();
assert.deepEqual(loaded, [], 'mounting the run panel loads no context panel');
const toggle = all(runRoot).find(element => element.type === 'button' && textOf(element).trim() === 'Context use');
assert.ok(toggle && toggle.props['aria-expanded'] === false && toggle.props['aria-controls'] === 'run-inspector');
assert.deepEqual(calls, []);
await toggle.props.onClick();
await settle();
assert.deepEqual(loaded, ['ContextPanel']);
assert.deepEqual(calls, [{ method: 'context_inspect', params: { chat_id: 'chat-a', run_id: 'run-1' } }]);
assert.ok(textOf(runRoot).includes('Context use · task run-1') && toggle.props['aria-expanded'] === true);
await toggle.props.onClick();
await settle();
assert.ok(!textOf(runRoot).includes('Context use · task'), 'the toggle closes the panel');
runApp.unmount();

// Server rendering escapes every name.
const seeded = { ...Panel, setup(p, context) { const bound = Panel.setup(p, context); bound.view.value = bound.review(inspection()); return bound; } };
const html = await renderToString(createSSRApp(seeded, { chatId: 'chat-a', runId: 'run-1' }));
assert.ok(html.includes('aria-label="Context use"') && html.includes('&lt;img src=x onerror=alert(1)&gt;'));
assert.ok(!/<(img|script|iframe)\b/.test(html) && html.includes('<th scope="col">Tool</th>'));
console.log('Context panel fixtures passed: two routes only, no window named, a bar of host categories and free space, category, part and largest-schema tables with one tab stop each, confirmation with focus, receipt and new window, refusals, disabled reasons, over-window, calibration and offer notes, task scoping, late replies, the run panel toggle (lazy, current task), literal names and motion only when welcome.');
