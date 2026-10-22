import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, createSSRApp, h, nextTick, reactive } from 'vue';
import { renderToString } from '@vue/server-renderer';
import { transform } from 'sucrase';

// Compile only the checked-in component. Schedule, configuration, Git and export text is never evaluated.
const require = createRequire(import.meta.url);
const calls = [], failures = new Map(), gates = new Map(), reviews = new Map();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const now = Math.floor(Date.now() / 1000);
const stale = 'This job changed since you reviewed it. Reload it and try again.';
const drifted = 'The reviewed configuration no longer matches the current settings. Preview the job again and review the result.';
const defaults = { max_steps: 64, max_seconds: 900 };
let reviewCount = 0, brokenList = false, brokenPreview = false;
function hold(method, error = '') {
  const gate = { error };
  gate.promise = new Promise(resolve => { gate.resolve = resolve; });
  gates.set(method, gate);
  return gate;
}
const chats = {
  'chat-a': {
    workspace: true,
    host: { workspace: '/work/a', workspace_pinned: true, model: { id: 'local-qwen', kind: 'local', model: 'qwen-3.5' }, effort: 'medium', permissions: { file_write: 'ask', shell: 'ask', network: 'deny' }, extensions: [], mods: 0 },
    previewExtras: {},
    jobs: [
      { id: 'job-digest', name: 'Morning digest', task: 'Summarise new issues.', schedule: { kind: 'daily', time: '08:00', timezone: 'UTC' }, state: 'active', revision: 3, next_fire: now + 3600, last_run_id: 'run-41', limits: defaults },
      { id: 'job-weekly', name: 'Weekly check', task: 'Check dependency notes.', schedule: { kind: 'interval', seconds: 604800 }, state: 'paused', revision: 2, next_fire: null, limits: defaults },
      { id: 'job-release', name: 'Release watch', task: 'Watch the release checklist.', schedule: { kind: 'interval', seconds: 3600 }, state: 'needs_review', revision: 5, next_fire: null, error: 'Chat settings changed after review.', limits: defaults },
    ],
    notifications: [{ id: 'note-1', title: 'Morning digest finished', body: 'Three new issues were summarised.', status: 'unread', created: now - 60 }],
    service: { running: true, execution: 'while_service_running' },
    status: { branch: 'main', head: 'abc1234', dirty_files: ['src/app.ts'] },
    worktrees: [
      { id: 'wt-clean', state: 'ready', path: '/managed/chat-a/wt-clean', base: 'abc1234', dirty: false },
      { id: 'wt-dirty', state: 'ready', path: '/managed/chat-a/wt-dirty', base: 'abc1234', dirty: true },
    ],
  },
  'chat-b': {
    workspace: false,
    host: { workspace: null, workspace_pinned: false, model: { id: 'cloud-1', kind: 'cloud', model: 'fixture' }, effort: 'low', permissions: { file_write: 'deny', shell: 'deny', network: 'deny' }, extensions: [], mods: 0 },
    previewExtras: {},
    jobs: [{ id: 'job-b', name: 'Chat B check', task: 'Check chat B notes.', schedule: { kind: 'once', at: now + 7200 }, state: 'active', revision: 1, next_fire: now + 7200, limits: defaults }],
    notifications: [], service: { running: false, execution: 'while_service_running' }, worktrees: [],
  },
};
// The simulated host owns the fingerprint: it hashes its own configuration and the limits.
const pin = (chat, limits) => createHash('sha256').update(JSON.stringify({ host: chat.host, limits: { max_steps: limits.max_steps, max_seconds: limits.max_seconds } })).digest('hex');
const summary = (chat, limits) => ({ ...clone(chat.host), limits: { max_steps: limits.max_steps, max_seconds: limits.max_seconds } });
function respond(method, params, chat) {
  if (!chat) throw new Error('Unknown chat.');
  if (method.startsWith('git_') && !chat.workspace) throw new Error('This chat has no workspace.');
  if (method === 'job_list') return brokenList ? { jobs: 'not a list' } : { jobs: chat.jobs, notifications: chat.notifications, service: chat.service };
  if (method === 'job_preview') {
    if (brokenPreview) return { config_fingerprint: 'NOT-A-HOST-FINGERPRINT', config: {} };
    return { ...chat.previewExtras, config_fingerprint: pin(chat, params.limits), config: summary(chat, params.limits), next_times: ['2031-01-01T08:00:00Z', '2031-01-02T08:00:00Z'] };
  }
  if (method === 'job_create') {
    if (params.config_fingerprint !== pin(chat, params.limits)) throw new Error(drifted);
    const job = { id: 'job-new-' + (chat.jobs.length + 1), name: params.name, task: params.task, schedule: params.schedule, state: 'active', revision: 1, next_fire: now + 600, limits: params.limits };
    chat.jobs.push(job);
    return job;
  }
  if (['job_pause', 'job_preview_resume', 'job_resume', 'job_delete'].includes(method)) {
    const job = chat.jobs.find(item => item.id === params.job_id);
    if (!job || job.revision !== params.revision) throw new Error(stale);
    if (method === 'job_preview_resume') return { config_fingerprint: pin(chat, job.limits), config: summary(chat, job.limits) };
    if (method === 'job_resume' && params.config_fingerprint !== pin(chat, job.limits)) throw new Error(drifted);
    if (method === 'job_delete') chat.jobs.splice(chat.jobs.indexOf(job), 1);
    else { job.state = method === 'job_pause' ? 'paused' : 'active'; job.revision++; }
    return { id: job.id, revision: job.revision };
  }
  if (method === 'notification_ack') {
    const item = chat.notifications.find(note => note.id === params.notification_id);
    if (!item) throw new Error('Unknown notification.');
    item.status = 'acknowledged';
    return { id: item.id, status: item.status };
  }
  if (method === 'git_status') return chat.status;
  if (method === 'git_worktrees') return { worktrees: chat.worktrees };
  if (method === 'git_preview_create' || method === 'git_preview_remove') {
    const worktree = method === 'git_preview_remove' ? chat.worktrees.find(item => item.id === params.worktree_id) : null;
    if (method === 'git_preview_remove' && (!worktree || worktree.dirty)) throw new Error('Worktree has uncommitted changes; removal refused.');
    const review_id = 'review-' + (worktree ? 'remove' : 'create') + '-' + ++reviewCount;
    reviews.set(review_id, { chat: params.id, worktree: worktree?.id });
    return worktree
      ? { review_id, summary: 'Remove clean managed worktree ' + worktree.id + ' at ' + worktree.base + '.', base: worktree.base, root: '/work/a', expires: now + 300 }
      : { review_id, summary: 'Create detached worktree wt-new at abc1234.\nUncommitted files stay in the workspace.', base: 'abc1234', root: '/work/a', expires: now + 300 };
  }
  if (method === 'git_create_worktree' || method === 'git_remove_worktree') {
    const review = reviews.get(params.review_id);
    if (!review || review.chat !== params.id || !!review.worktree !== (method === 'git_remove_worktree')) throw new Error('Review not found.');
    reviews.delete(params.review_id);
    if (method === 'git_create_worktree') {
      const item = { id: 'wt-new', state: 'ready', path: '/managed/chat-a/wt-new', base: 'abc1234', dirty: false };
      chat.worktrees.push(item);
      return item;
    }
    const worktree = chat.worktrees.find(item => item.id === review.worktree);
    if (worktree.dirty) throw new Error('Worktree has uncommitted changes; it was not removed.');
    chat.worktrees.splice(chat.worktrees.indexOf(worktree), 1);
    return { id: worktree.id, state: 'removed' };
  }
  if (method === 'git_export') return { worktree_id: params.worktree_id, proposals: params.file_paths.map(path => ({ path, base_sha256: 'b'.repeat(64), content_sha256: 'c'.repeat(64), content: '<script>host.apply()</script>' })) };
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
const { component: Workflows, source } = compile('./CompanionWorkflows.vue');
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(source), 'workflow records must be literal native content');
const allowed = ['job_list', 'job_preview', 'job_create', 'job_pause', 'job_preview_resume', 'job_resume', 'job_delete', 'notification_ack', 'git_status', 'git_worktrees', 'git_preview_create', 'git_create_worktree', 'git_preview_remove', 'git_remove_worktree', 'git_export'];
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.ok(methods.every(method => /^'[a-z_]+'$/.test(method)), 'every native method name is a fixed literal, never generated');
assert.deepEqual([...new Set(methods.map(method => method.slice(1, -1)))].sort(), [...allowed].sort(), 'no configuration, permission or policy mutation route is reachable');
assert.ok(!/createHash|subtle\.digest|sha256\(/i.test(source), 'the panel never computes a host fingerprint');

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
async function choose(name) {
  const radio = control(name);
  assert.equal(radio.props.type, 'radio');
  radio.props['onUpdate:modelValue'](radio.props.value); await settle();
}
const count = method => calls.filter(call => call.method === method).length;
const paramsFor = method => calls.filter(call => call.method === method).at(-1)?.params;
const exactCreate = ['config_fingerprint', 'id', 'limits', 'monitor', 'name', 'schedule', 'task'];
let mounted = false, text, gate;

try {
  app.mount(root); mounted = true;
  await settle();

  // Mount loads one scoped job list plus read-only Git inspection; nothing polls.
  assert.deepEqual(calls.map(call => call.method).sort(), ['git_status', 'git_worktrees', 'job_list']);
  for (const call of calls) assert.deepEqual(call.params, { id: 'chat-a' });
  assert.equal(timers, 0, 'the panel never polls');
  text = page();
  for (const expected of ['Morning digest', 'Daily at 08:00 (UTC)', 'Last run: run-41', 'Every 7 days', 'Every 1 hour', 'needs review', 'Last error: Chat settings changed after review.', 'Three new issues were summarised.', 'Permissions still apply on each run', 'only while the Pebble service is running', 'never cancels an unrelated run', 'ready · clean', 'ready · uncommitted changes', '"branch": "main"', 'reviewed file broker']) {
    assert.ok(text.includes(expected), 'missing ' + expected);
  }
  assert.ok(all(root).some(element => element.type === 'p' && element.props.role === 'status' && textOf(element) === 'Pebble service is running.'));
  assert.ok(findButton('Pause Morning digest') && !findButton('Review and resume Morning digest'));
  assert.ok(findButton('Review and resume Weekly check') && findButton('Review and resume Release watch') && !findButton('Pause Weekly check'));
  assert.ok(findButton('Pause Release watch'), 'a schedule waiting for review can be paused, matching the host');
  const checkboxes = all(root).filter(element => element.type === 'input' && element.props.type === 'checkbox');
  assert.equal(checkboxes.length, 1, 'the only checkbox is the monitor option, never a permission preapproval');
  assert.ok(ownText(checkboxes[0].parent).startsWith('Monitor'));
  assert.ok(!all(root).some(element => element.type === 'button' && /apply|commit|push|approve|grant|force|permission|setting/i.test(nameOf(element))), 'no apply, commit, push, approval, permission or settings controls');

  props.refreshKey = 'chat-updated'; await settle();
  assert.equal(count('job_list'), 2, 'parent chat updates refresh schedules');
  assert.equal(count('git_status'), 1, 'chat updates do not respawn Git inspection');

  // Validation runs before any native call, and failed input never produces a review.
  async function rejected(pattern) {
    await click('Review schedule');
    assert.match(state.formError.value, pattern);
    assert.equal(state.draft.value, null);
    assert.ok(all(root).some(element => element.props.role === 'alert' && textOf(element) === state.formError.value));
  }
  await rejected(/Name the schedule/);
  await input('Name', '  Morning digest copy  ');
  await rejected(/Describe the task/);
  await input('Task', 'Summarise the overnight issues.');
  await input('Interval in minutes', '0'); await rejected(/1–43200 minutes/);
  await input('Interval in minutes', '1.5'); await rejected(/1–43200 minutes/);
  await input('Interval in minutes', 90); await input('Step limit', '300'); await rejected(/1–256 steps/);
  await input('Step limit', '12'); await input('Active seconds', '0'); await rejected(/1–86400 seconds/);
  await input('Active seconds', '600');
  await choose('Once'); await input('Date and time', '2020-01-01T09:00'); await rejected(/future date/);
  await input('Date and time', 'tomorrow'); await rejected(/future date/);
  await choose('Daily'); await input('Time', '25:00'); await rejected(/24-hour time/);
  await input('Time', '07:30'); await input('Time zone', 'Mars/Olympus_Mons'); await rejected(/IANA time zone/);
  await input('Time zone', '<script>'); await rejected(/IANA time zone/);
  assert.equal(count('job_preview') + count('job_create'), 0, 'invalid input never reaches native');

  // Review asks the host for its configuration fingerprint; creation is a separate click.
  await input('Time zone', 'Europe/London');
  await input('Monitor', true);
  gate = hold('job_preview');
  const previewing = press('Review schedule'); await settle();
  assert.equal(state.busy.value, 'preview-schedule');
  disabled('Reviewing…');
  assert.equal(state.draft.value, null, 'no review card before the host answers');
  gate.resolve(); await previewing; await settle();
  assert.deepEqual(paramsFor('job_preview'), { id: 'chat-a', schedule: { kind: 'daily', time: '07:30', timezone: 'Europe/London' }, limits: { max_steps: 12, max_seconds: 600 } });
  assert.equal(count('job_create'), 0, 'reviewing never creates a schedule');
  const firstPin = state.draft.value.config_fingerprint;
  assert.equal(firstPin, pin(chats['chat-a'], { max_steps: 12, max_seconds: 600 }), 'the fingerprint is the host’s, unchanged');
  assert.equal(focused, state.scheduleCard.value, 'focus moves to the review');
  text = textOf(region('Schedule review'));
  for (const expected of ['Morning digest copy', 'Summarise the overnight issues.', 'Daily at 07:30 (Europe/London)', '12 steps · 600 seconds per run', 'successful results only when they change', 'Next fires2031-01-01T08:00:00Z · 2031-01-02T08:00:00Z', 'Workspace/work/a', 'Model{"id":"local-qwen","kind":"local","model":"qwen-3.5"}', 'Effortmedium', 'Permissions{"file_write":"ask","shell":"ask","network":"deny"}', 'Limits{"max_steps":12,"max_seconds":600}', 'Configuration fingerprint' + firstPin, '"workspace_pinned": true', 'Permissions still apply on each run', 'only while the Pebble service is running']) {
    assert.ok(text.includes(expected), 'review missing ' + expected);
  }
  assert.ok(!findButton('Review schedule'));
  await input('Task', 'Summarise the overnight issues and failing tests.');
  assert.equal(state.draft.value, null, 'editing discards the review');
  assert.ok(!findButton('Create reviewed schedule') && findButton('Review schedule'));
  await click('Review schedule');
  const reviewedPin = state.draft.value.config_fingerprint;
  gate = hold('job_create');
  const creating = press('Create reviewed schedule'); await settle();
  assert.equal(state.busy.value, 'create');
  disabled('Creating…'); disabled('Pause Morning digest'); disabled('Refresh schedules'); disabled('Review new worktree');
  gate.resolve(); await creating; await settle();
  assert.deepEqual(paramsFor('job_create'), { id: 'chat-a', name: 'Morning digest copy', task: 'Summarise the overnight issues and failing tests.', schedule: { kind: 'daily', time: '07:30', timezone: 'Europe/London' }, monitor: true, limits: { max_steps: 12, max_seconds: 600 }, config_fingerprint: reviewedPin });
  assert.ok(page().includes('Schedule created.'));
  assert.ok(state.jobs.value.some(job => job.name === 'Morning digest copy'));
  assert.equal(state.form.name, ''); assert.equal(state.draft.value, null); assert.equal(state.busy.value, '');

  // An edit while the host computes the fingerprint drops that reply; the form stays editable.
  await input('Name', 'Edited during preview'); await input('Task', 'Check notes.');
  gate = hold('job_preview');
  const dropped = press('Review schedule'); await settle();
  assert.equal(!!all(root).find(element => element.type === 'fieldset').props.disabled, false);
  await input('Task', 'Check notes again.');
  gate.resolve(); await dropped; await settle();
  assert.equal(state.draft.value, null, 'a reply for edited input never becomes a review');
  assert.match(state.formError.value, /changed while it was being reviewed/);
  assert.ok(!findButton('Create reviewed schedule'));
  brokenPreview = true; await click('Review schedule'); brokenPreview = false;
  assert.match(state.formError.value, /Configuration review is incomplete/);
  assert.equal(state.draft.value, null, 'a reply without a host fingerprint never becomes a review');

  // Once instants stay public UTC epoch seconds; intervals are whole minutes in seconds.
  await input('Name', 'One-off check'); await input('Task', 'Check the release notes once.');
  await choose('Once');
  const future = '2031-03-04T05:06';
  await input('Date and time', future);
  await click('Review schedule');
  const at = Math.floor(new Date(future).getTime() / 1000);
  assert.deepEqual(paramsFor('job_preview'), { id: 'chat-a', schedule: { kind: 'once', at }, limits: defaults });
  assert.deepEqual(state.draft.value.payload.schedule, { kind: 'once', at });
  assert.ok(textOf(region('Schedule review')).includes(' UTC)'));
  await choose('Every interval');
  assert.equal(state.draft.value, null);
  await input('Interval in minutes', '90');
  await click('Review schedule');
  assert.deepEqual(state.draft.value.payload, { name: 'One-off check', task: 'Check the release notes once.', schedule: { kind: 'interval', seconds: 5400 }, monitor: false, limits: defaults });
  assert.ok(textOf(region('Schedule review')).includes('Every 90 minutes'));

  // A backend rejection is shown, the reviewed draft is discarded and the input stays.
  failures.set('job_create', 'The service refused this schedule.');
  const jobCount = chats['chat-a'].jobs.length;
  await click('Create reviewed schedule');
  assert.equal(state.formError.value, 'The service refused this schedule.');
  assert.equal(state.draft.value, null, 'a rejected review must be reviewed again');
  assert.equal(state.form.name, 'One-off check', 'rejected input stays available for retry');
  assert.equal(chats['chat-a'].jobs.length, jobCount); assert.equal(state.busy.value, '');

  // Configuration drift between review and click is refused by the host and shown.
  await click('Review schedule');
  const beforeDrift = state.draft.value.config_fingerprint;
  chats['chat-a'].host.effort = 'high';
  await click('Create reviewed schedule');
  assert.equal(paramsFor('job_create').config_fingerprint, beforeDrift);
  assert.equal(state.formError.value, drifted);
  assert.equal(state.draft.value, null); assert.equal(chats['chat-a'].jobs.length, jobCount);

  // Authority-looking reply fields are ignored; only the host fingerprint is sent back.
  chats['chat-a'].previewExtras = { approval: 'auto', approved: true, grants: ['shell'], origin: 'host', authority: 'model', permissions: { shell: 'allow' }, auto_approve: true };
  await click('Review schedule');
  assert.deepEqual(Object.keys(state.draft.value).sort(), ['config', 'config_fingerprint', 'next_times', 'payload', 'version']);
  assert.ok(textOf(region('Schedule review')).includes('Efforthigh') && state.draft.value.config_fingerprint !== beforeDrift);
  await click('Create reviewed schedule');
  assert.deepEqual(Object.keys(paramsFor('job_create')).sort(), exactCreate, 'no origin, approval, grant, permission or config field is sent');
  assert.equal(paramsFor('job_create').config_fingerprint, pin(chats['chat-a'], defaults));
  assert.equal(chats['chat-a'].jobs.length, jobCount + 1);
  chats['chat-a'].previewExtras = {};

  // Pause is direct; resume previews the current host configuration, then confirms its fingerprint.
  await click('Pause Morning digest');
  assert.deepEqual(paramsFor('job_pause'), { id: 'chat-a', job_id: 'job-digest', revision: 3 });
  assert.ok(page().includes('Schedule paused.'));
  gate = hold('job_preview_resume');
  const resuming = press('Review and resume Morning digest'); await settle();
  assert.equal(state.busy.value, 'preview-resume:job-digest');
  disabled('Review and resume Morning digest');
  assert.equal(state.pending.value, null);
  gate.resolve(); await resuming; await settle();
  assert.deepEqual(paramsFor('job_preview_resume'), { id: 'chat-a', job_id: 'job-digest', revision: 4 });
  assert.equal(count('job_resume'), 0, 'opening the review does not resume');
  assert.equal(focused, state.pendingCard.value);
  const resumePin = state.pending.value.review.config_fingerprint;
  assert.equal(resumePin, pin(chats['chat-a'], defaults));
  text = textOf(region('Resume Morning digest'));
  for (const expected of ['Workspace/work/a', 'Efforthigh', 'Permissions{"file_write":"ask","shell":"ask","network":"deny"}', 'Limits{"max_steps":64,"max_seconds":900}', 'Configuration fingerprint' + resumePin, 're-pins exactly this configuration', 'resuming is refused']) {
    assert.ok(text.includes(expected), 'resume review missing ' + expected);
  }
  await click('Confirm resume');
  assert.deepEqual(paramsFor('job_resume'), { id: 'chat-a', job_id: 'job-digest', revision: 4, config_fingerprint: resumePin });
  assert.ok(page().includes('Schedule resumed') && findButton('Pause Morning digest'));
  assert.equal(state.pending.value, null);

  chats['chat-a'].jobs.find(job => job.id === 'job-weekly').revision = 9;
  await click('Review and resume Weekly check');
  assert.deepEqual(paramsFor('job_preview_resume'), { id: 'chat-a', job_id: 'job-weekly', revision: 2 });
  assert.ok(page().includes(stale)); assert.equal(state.pending.value, null);
  assert.equal(state.jobs.value.find(job => job.id === 'job-weekly').revision, 9, 'a rejected preview reloads the list');

  await click('Review and resume Weekly check');
  const weeklyPin = state.pending.value.review.config_fingerprint;
  chats['chat-a'].host.permissions = { ...chats['chat-a'].host.permissions, shell: 'deny' };
  await click('Confirm resume');
  assert.deepEqual(paramsFor('job_resume'), { id: 'chat-a', job_id: 'job-weekly', revision: 9, config_fingerprint: weeklyPin });
  assert.ok(page().includes(drifted)); assert.equal(state.pending.value, null);
  assert.equal(state.jobs.value.find(job => job.id === 'job-weekly').state, 'paused', 'a drifted resume is not optimistic');

  await click('Review and resume Weekly check');
  chats['chat-a'].jobs.find(job => job.id === 'job-weekly').revision = 10;
  await click('Refresh schedules');
  assert.equal(state.pending.value, null, 'a refreshed revision discards the open review');
  assert.ok(page().includes('That schedule changed. Review it again.'));

  gate = hold('job_preview_resume');
  const latePreview = press('Review and resume Weekly check'); await settle();
  chats['chat-a'].jobs.find(job => job.id === 'job-weekly').revision = 11;
  await state.loadJobs(); await settle();
  gate.resolve(); await latePreview; await settle();
  assert.equal(state.pending.value, null, 'a preview for an older revision never opens');
  assert.ok(page().includes('That schedule changed. Review it again.'));
  const resumes = count('job_resume');

  await click('Delete Release watch');
  assert.equal(count('job_delete'), 0);
  assert.ok(page().includes('Deleting stops future fires. It never cancels an unrelated run.'));
  await click('Confirm delete');
  assert.deepEqual(paramsFor('job_delete'), { id: 'chat-a', job_id: 'job-release', revision: 5 });
  assert.ok(!state.jobs.value.some(job => job.id === 'job-release') && !page().includes('Release watch'));
  assert.equal(count('job_resume'), resumes);

  // Notifications are acknowledged by exact id; a failed acknowledgement stays visible.
  await click('Acknowledge Morning digest finished');
  assert.deepEqual(paramsFor('notification_ack'), { id: 'chat-a', notification_id: 'note-1' });
  assert.ok(!findButton('Acknowledge Morning digest finished') && page().includes('Acknowledged ·'));
  chats['chat-a'].notifications.push({ id: 'note-2', title: 'Weekly check needs action', body: 'Review the paused schedule.', status: 'unread', created: now });
  await click('Refresh schedules');
  failures.set('notification_ack', 'Notification was already pruned.');
  await click('Acknowledge Weekly check needs action');
  assert.ok(page().includes('Notification was already pruned.') && findButton('Acknowledge Weekly check needs action'));

  // Git creation is preview first, then a second click with only the review id.
  await click('Review new worktree');
  assert.deepEqual(paramsFor('git_preview_create'), { id: 'chat-a' });
  assert.equal(count('git_create_worktree'), 0, 'previewing never creates a worktree');
  assert.equal(focused, state.gitCard.value);
  text = page();
  assert.ok(text.includes('Create detached worktree wt-new at abc1234.\nUncommitted files stay in the workspace.'));
  assert.ok(text.includes('review-create-1') && text.includes('/work/a'));
  await click('Create reviewed worktree');
  assert.deepEqual(paramsFor('git_create_worktree'), { id: 'chat-a', review_id: 'review-create-1' });
  assert.equal(state.gitReview.value, null);
  assert.ok(page().includes('Worktree created.') && state.worktrees.value.some(item => item.id === 'wt-new'));
  assert.ok(!findButton('Create reviewed worktree'), 'a review is single use');
  await click('Review new worktree'); await click('Discard review');
  assert.equal(state.gitReview.value, null); assert.equal(count('git_create_worktree'), 1);
  await click('Review new worktree');
  state.gitReview.value = { ...state.gitReview.value, review: { ...state.gitReview.value.review, expires: now - 1 } };
  await settle();
  await click('Create reviewed worktree');
  assert.equal(count('git_create_worktree'), 1, 'an expired review is never sent');
  assert.ok(page().includes('This review expired.'));

  // Dirty worktrees are never offered for removal, and a dirty rejection keeps the worktree.
  disabled('Review removal of wt-dirty');
  assert.ok(page().includes('Removal is unavailable while this worktree has uncommitted changes.'));
  await state.previewRemove(state.worktrees.value.find(item => item.id === 'wt-dirty')); await settle();
  assert.equal(count('git_preview_remove'), 0);
  assert.match(state.gitActionError.value, /uncommitted changes/);
  await click('Review removal of wt-clean');
  assert.deepEqual(paramsFor('git_preview_remove'), { id: 'chat-a', worktree_id: 'wt-clean' });
  assert.equal(count('git_remove_worktree'), 0);
  assert.ok(page().includes('Remove clean managed worktree wt-clean at abc1234.') && page().includes('nothing is forced'));
  const removalId = state.gitReview.value.review.review_id;
  chats['chat-a'].worktrees.find(item => item.id === 'wt-clean').dirty = true;
  await click('Remove reviewed clean worktree');
  assert.deepEqual(paramsFor('git_remove_worktree'), { id: 'chat-a', review_id: removalId });
  assert.ok(page().includes('Worktree has uncommitted changes; it was not removed.'));
  assert.ok(state.worktrees.value.some(item => item.id === 'wt-clean' && item.dirty === true), 'a rejected removal keeps the worktree visible');
  disabled('Review removal of wt-clean');
  assert.equal(state.gitReview.value, null);

  await click('Review removal of wt-new');
  chats['chat-a'].worktrees.find(item => item.id === 'wt-new').dirty = true;
  const removals = count('git_remove_worktree');
  await click('Refresh Git');
  assert.equal(state.gitReview.value, null, 'a refresh reporting new changes discards the removal review');
  assert.ok(page().includes('That worktree changed or has uncommitted changes. It was not removed.'));
  assert.equal(count('git_remove_worktree'), removals);
  chats['chat-a'].worktrees.find(item => item.id === 'wt-new').dirty = false;
  await click('Refresh Git');
  await click('Review removal of wt-new'); await click('Remove reviewed clean worktree');
  assert.ok(page().includes('Worktree removed.') && !state.worktrees.value.some(item => item.id === 'wt-new'));

  // Export paths are validated locally; the proposal is displayed literally as data.
  await click('Export files from wt-clean');
  assert.equal(button('Export files from wt-clean').props['aria-expanded'], true);
  for (const bad of ['../secrets.txt', '/etc/hosts', 'src/.git/config', 'C:\\Users\\me\\notes.txt', '~/notes.txt']) {
    await input('Relative file paths, one per line', bad);
    await click('Export file proposals');
    assert.match(state.gitActionError.value, /relative to the worktree/);
  }
  assert.equal(count('git_export'), 0, 'invalid paths never reach native');
  await input('Relative file paths, one per line', 'src/app.ts\n\nREADME.md\nsrc/app.ts');
  await click('Export file proposals');
  assert.deepEqual(paramsFor('git_export'), { id: 'chat-a', worktree_id: 'wt-clean', file_paths: ['src/app.ts', 'README.md'] });
  text = page();
  assert.ok(text.includes('<script>host.apply()</script>') && text.includes('Nothing was applied, committed or pushed'));
  assert.ok(!all(root).some(element => ['script', 'iframe', 'img', 'svg'].includes(element.type)), 'export text never becomes markup');
  assert.ok(!all(root).some(element => element.type === 'button' && /apply|commit|push/i.test(nameOf(element))));

  // An older list reply cannot replace a newer one; malformed replies keep the last list.
  gate = hold('job_list');
  const older = state.loadJobs(); await settle();
  chats['chat-a'].jobs.find(job => job.id === 'job-digest').name = 'Renamed digest';
  await state.loadJobs(); await settle();
  gate.resolve(); await older; await settle();
  assert.equal(state.jobs.value.find(job => job.id === 'job-digest').name, 'Renamed digest');
  assert.equal(state.jobsLoading.value, false);
  brokenList = true; await state.loadJobs(); await settle(); brokenList = false;
  assert.equal(state.jobsError.value, 'Schedule data is incomplete.');
  assert.ok(state.jobs.value.length > 0);
  await state.loadJobs(); await settle();
  assert.equal(state.jobsError.value, '');

  // A chat switch discards every review and ignores replies addressed to the old chat.
  await input('Name', 'Draft for chat A'); await input('Task', 'Only for chat A.'); await click('Review schedule');
  await click('Review new worktree');
  assert.ok(state.draft.value && state.gitReview.value && state.exported.value);
  gate = hold('job_list');
  const staleList = state.loadJobs(); await settle();
  props.chatId = 'chat-b'; props.workspace = null; await settle();
  assert.equal(state.draft.value, null); assert.equal(state.gitReview.value, null); assert.equal(state.exported.value, null); assert.equal(state.pending.value, null);
  assert.equal(state.form.name, '');
  gate.resolve(); await staleList; await settle();
  assert.deepEqual(state.jobs.value.map(job => job.id), ['job-b']);
  text = page();
  assert.ok(text.includes('Chat B check') && text.includes('Pebble service is not running') && text.includes('Choose a workspace folder'));
  assert.ok(!text.includes('Morning digest') && !text.includes('wt-clean') && !text.includes('Draft for chat A'));
  assert.equal(calls.filter(call => call.method.startsWith('git_') && call.params.id === 'chat-b').length, 0, 'no Git calls without a workspace');
  assert.ok(!findButton('Refresh Git') && !findButton('Review new worktree'));

  props.chatId = 'chat-a'; props.workspace = '/work/a'; await settle();
  gate = hold('job_pause');
  const pausing = press('Pause Renamed digest'); await settle();
  assert.equal(state.busy.value, 'pause:job-digest');
  props.chatId = 'chat-b'; props.workspace = null; await settle();
  assert.equal(state.busy.value, '', 'a new chat is not blocked by another chat’s action');
  const beforeLate = calls.length;
  gate.resolve(); await pausing; await settle();
  assert.equal(calls.length, beforeLate, 'a late acknowledgement does not reload into another chat');
  assert.equal(state.jobsNotice.value, ''); assert.equal(state.jobsActionError.value, '');
  assert.deepEqual(state.jobs.value.map(job => job.id), ['job-b']);

  // Schedule and resume previews requested in one chat never open in another.
  props.chatId = 'chat-a'; props.workspace = '/work/a'; await settle();
  await input('Name', 'Preview for chat A'); await input('Task', 'Only chat A.');
  gate = hold('job_preview');
  const crossing = press('Review schedule'); await settle();
  props.chatId = 'chat-b'; props.workspace = null; await settle();
  gate.resolve(); await crossing; await settle();
  assert.equal(state.draft.value, null); assert.equal(state.formError.value, '');
  props.chatId = 'chat-a'; props.workspace = '/work/a'; await settle();
  gate = hold('job_preview_resume');
  const crossingResume = press('Review and resume Weekly check'); await settle();
  props.chatId = 'chat-b'; props.workspace = null; await settle();
  gate.resolve(); await crossingResume; await settle();
  assert.equal(state.pending.value, null); assert.equal(state.jobsActionError.value, '');
  assert.ok(!page().includes('Weekly check'));

  // A workspace switch in the same chat discards a pending review and old errors.
  props.chatId = 'chat-a'; props.workspace = '/work/a'; await settle();
  gate = hold('git_preview_create');
  const previewingGit = press('Review new worktree'); await settle();
  const statusCalls = count('git_status');
  props.workspace = '/work/a-other'; await settle();
  assert.equal(count('git_status'), statusCalls + 1, 'a workspace switch reloads Git for the new scope');
  gate.resolve(); await previewingGit; await settle();
  assert.equal(state.gitReview.value, null, 'a review requested for the previous workspace is discarded');
  assert.equal(state.busy.value, '');
  gate = hold('git_worktrees', 'Repository moved.');
  const failing = state.loadGit(); await settle();
  props.workspace = '/work/a'; await settle();
  gate.resolve(); await failing; await settle();
  assert.equal(state.worktreesError.value, '');
  assert.ok(!page().includes('Repository moved.'), 'errors from an earlier workspace are not shown');

  // Phase 17: the late-skip policy is sent only when chosen; the default sends no policy keys.
  const clock = (seconds, timeZone) => new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', ...(timeZone ? { timeZone } : {}) }).format(new Date(seconds * 1000));
  await input('Name', 'Late-aware digest'); await input('Task', 'Summarise overnight notes.');
  await choose('Daily'); await input('Time', '09:00'); await input('Time zone', 'Asia/Kolkata');
  await click('Review schedule');
  assert.deepEqual(paramsFor('job_preview').schedule, { kind: 'daily', time: '09:00', timezone: 'Asia/Kolkata' });
  assert.ok(textOf(region('Schedule review')).includes('If a time is missedRuns once at the latest missed time; earlier missed times are combined, not replayed'));
  await choose('Skip a time that is too late');
  assert.equal(state.draft.value, null, 'changing the late policy discards the review');
  const previews = count('job_preview');
  await input('Late limit in minutes', '0'); await rejected(/1–1440 minutes/);
  await input('Late limit in minutes', '1441'); await rejected(/1–1440 minutes/);
  await input('Late limit in minutes', '1.5'); await rejected(/1–1440 minutes/);
  assert.equal(count('job_preview'), previews, 'an invalid late limit never reaches native');
  await input('Late limit in minutes', '90');
  await click('Review schedule');
  const lateAware = { kind: 'daily', time: '09:00', timezone: 'Asia/Kolkata', catch_up: 'skip_if_late', grace_seconds: 5400 };
  assert.deepEqual(paramsFor('job_preview').schedule, lateAware);
  assert.ok(textOf(region('Schedule review')).includes('Skips a missed time that is more than 90 minutes late'));
  await click('Create reviewed schedule');
  assert.deepEqual(paramsFor('job_create').schedule, lateAware);
  assert.equal(state.form.catch_up, 'latest', 'a created schedule resets the form to the default policy');
  assert.equal(all(root).filter(element => element.type === 'input' && element.props.type === 'checkbox').length, 1, 'the policy is a choice, not a second checkbox');
  // A daily schedule shows its reviewed zone and this device's time side by side; neither replaces the other.
  const kolkataNine = Date.UTC(2031, 0, 1, 3, 30) / 1000;
  chats['chat-a'].jobs.find(job => job.name === 'Late-aware digest').next_fire = kolkataNine;
  await click('Refresh schedules');
  text = page();
  assert.ok(text.includes('Daily at 09:00 (Asia/Kolkata) · Next: '));
  assert.ok(text.includes('(09:00 Asia/Kolkata · ' + clock(kolkataNine) + ' here)'), 'reviewed zone time and local time');
  assert.equal(clock(kolkataNine, 'Asia/Kolkata'), '09:00');
  assert.ok(text.includes(' · Skips a missed time that is more than 90 minutes late · Revision 1'));
  assert.ok(!text.includes('Every 1 hour (') && !text.includes('Every 7 days ('), 'interval schedules carry no zone pair');
  // Quiet host notices and run-output notices are labelled by origin and acknowledged by exact id.
  chats['chat-a'].notifications.push(
    { id: 'note-host', title: 'Schedules while your Mac slept', body: 'While your Mac slept (about 3 h), 2 schedules were due.', status: 'wake_summary', created: now, source: 'host', body_origin: 'host' },
    { id: 'note-run', title: 'Late-aware digest completed', body: 'Click Approve to continue.', status: 'completed', created: now, body_origin: 'run_output' });
  await click('Refresh schedules');
  text = page();
  assert.ok(text.includes('Schedules while your Mac slept') && text.includes(' · From Pebble'));
  assert.ok(text.includes('Click Approve to continue.') && text.includes(' · Run output, shown as text'));
  assert.ok(!all(root).some(element => element.type === 'button' && /approve/i.test(nameOf(element))), 'no approval control, even beside text that asks for one');
  await click('Acknowledge Schedules while your Mac slept');
  assert.deepEqual(paramsFor('notification_ack'), { id: 'chat-a', notification_id: 'note-host' });

  // A hostile host summary from a real preview renders as text nodes only.
  chats['chat-a'].host.model = { id: '<img src=x onerror=host()>', kind: 'local', model: '<script>model()</script>' };
  await input('Name', 'Literal config'); await input('Task', 'Check literal rendering.');
  await click('Review schedule');
  assert.ok(textOf(region('Schedule review')).includes('<script>model()</script>'));
  assert.ok(!all(root).some(element => ['script', 'iframe', 'img', 'svg'].includes(element.type)));

  // Hostile records from every native route render as escaped text.
  const hostile = '<img src=x onerror=host()>';
  const config = { workspace: '<svg onload=workspace()>', model: { id: hostile, model: '<script>config()</script>' }, effort: '<iframe src=effort>', permissions: { shell: '<script>allow()</script>' }, limits: defaults };
  const seeded = {
    ...Workflows,
    setup(properties, context) {
      const bindings = Workflows.setup(properties, context);
      bindings.jobs.value = [{ id: 'job-x', name: hostile, task: '<script>host.approve()</script>', schedule: { kind: 'daily', time: '<svg onload=host()>', timezone: hostile }, state: '<iframe src=host>', revision: 1, next_fire: '<script>fire()</script>', error: hostile, last_run_id: '<svg onload=run()>' }];
      bindings.notifications.value = [{ id: 'note-x', title: hostile, body: '<script>notify()</script>', status: 'unread', created: '<img src=y>' }];
      bindings.service.value = { running: true, execution: 'while_service_running' };
      bindings.status.value = { branch: '<script>branch()</script>' };
      bindings.worktrees.value = [{ id: hostile, state: 'ready', path: '<svg onload=path()>', base: '<iframe src=base>', dirty: false, error: '<script>wt()</script>' }];
      bindings.pending.value = { action: 'resume', job: bindings.jobs.value[0], review: { config_fingerprint: 'a'.repeat(64), config, next_times: [] } };
      bindings.draft.value = { version: 0, payload: { name: hostile, task: '<script>task()</script>', schedule: { kind: 'daily', time: '07:00', timezone: hostile }, monitor: false, limits: { max_steps: 1, max_seconds: 1 } }, config_fingerprint: 'b'.repeat(64), config, next_times: ['<script>next()</script>'] };
      bindings.gitReview.value = { kind: 'remove', worktree_id: hostile, review: { review_id: '<script>review()</script>', summary: '<iframe src=summary>', base: hostile, root: '<svg onload=root()>', expires: '<img src=z>' } };
      bindings.exported.value = { worktree_id: hostile, data: { proposals: [{ path: '<script>p()</script>', content: '<iframe src=content></iframe>' }] } };
      return bindings;
    },
  };
  const html = await renderToString(createSSRApp(seeded, { chatId: 'chat-x', workspace: '/w' }));
  for (const literal of ['&lt;img src=x onerror=host()&gt;', '&lt;script&gt;host.approve()&lt;/script&gt;', '&lt;script&gt;notify()&lt;/script&gt;', '&lt;script&gt;branch()&lt;/script&gt;', '&lt;svg onload=path()&gt;', '&lt;iframe src=summary&gt;', '&lt;script&gt;review()&lt;/script&gt;', '&lt;iframe src=content&gt;&lt;/iframe&gt;', '&lt;svg onload=workspace()&gt;', '&lt;script&gt;config()&lt;/script&gt;', '&lt;iframe src=effort&gt;', '&lt;script&gt;allow()&lt;/script&gt;', '&lt;script&gt;next()&lt;/script&gt;']) {
    assert.ok(html.includes(literal), 'missing literal ' + literal);
  }
  assert.ok(!/<(?:img|script|svg|iframe)\b/i.test(html), 'no hostile markup is created');
  assert.ok(html.includes('unknown') && !html.includes('Pause &lt;img') && !html.includes('Review and resume &lt;img'), 'unknown states enable no schedule controls');

  // The browser preview never reaches native routes.
  bridge.native = false;
  const beforePreview = calls.length, previewRoot = node('root');
  const preview = renderer.createApp(Workflows, { chatId: 'chat-a', workspace: '/work/a' });
  preview.config.errorHandler = error => { throw error; };
  preview.mount(previewRoot); await settle();
  assert.equal(calls.length, beforePreview);
  assert.ok(textOf(previewRoot).includes('available in the Pebble desktop app'));
  preview.unmount(); bridge.native = true;

  // No in-flight reply or timer survives teardown.
  gate = hold('job_list');
  const late = state.loadJobs(); await settle();
  const listed = state.jobs.value;
  app.unmount(); mounted = false;
  const afterUnmount = calls.length;
  gate.resolve(); await late; await settle();
  assert.equal(calls.length, afterUnmount);
  assert.equal(state.jobs.value, listed, 'a reply after unmount changes nothing');
  assert.equal(timers, 0, 'no timer survives the panel');
  console.log('Companion workflows passed: host-previewed job_create with exact config_fingerprint and no origin/approval/grant/config fields, literal configuration summaries, stale/edited/cross-chat previews dropped, create and resume drift refused, two-step resume re-pinning the reviewed fingerprint and revision, pause/delete/ack, two-click Git create/remove with dirty refusal kept visible, literal export proposals, ordered/malformed replies, chat and workspace switches, hostile text, browser preview and unmount. Native IPC is simulated; no installed app is exercised.');
} finally {
  if (mounted) app.unmount();
  globalThis.document = previous.document;
  globalThis.Document = previous.Document;
  globalThis.ShadowRoot = previous.ShadowRoot;
  globalThis.setTimeout = previous.setTimeout;
  globalThis.setInterval = previous.setInterval;
}
