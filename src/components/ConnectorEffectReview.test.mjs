import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc';
import { createRenderer, h, nextTick, reactive } from 'vue';
import { transform } from 'sucrase';

// Phase 16: the connector review card shows the host classification as literal
// text, never sends, and its one route confirms exactly the reviewed recipients.
const require = createRequire(import.meta.url);
const calls = [], failures = new Map(), gates = new Map();
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const hostile = '<img src=x onerror=alert(1)>';
const scripted = '<script>alert("x")</script>';
const A1 = 'a'.repeat(32), A2 = 'b'.repeat(32), PRIOR = '1'.repeat(32), R = '7'.repeat(64), B = '8'.repeat(64);
const outsider = hostile + '@evil.example';
const effect = (extra = {}) => ({ class: 'send', assigned: 'person', recipients: ['ada@example.test', outsider], recipient_count: 2, recipients_sha256: R,
  recipient_sources: { 'ada@example.test': 'person', [outsider]: 'untrusted_output' }, unconfirmed_recipients: [outsider], requires_confirmation: true,
  destination: null, subject: scripted, body_sha256: B, body_preview: 'Hello ' + hostile, idempotency: 'host_injected', send_intent: 'explicit',
  idempotency_key: null, requires_review: true, blocked: null, authorization: 'none', request_sha256: '9'.repeat(64),
  connector: { name: 'Mail ' + hostile, credential_exposure: 'environment_readable_by_local_commands' }, repeat_of: [PRIOR], ...extra });
const plugin = (id, extra = {}, status = 'pending', result) => ({ id, chat_id: 'chat-a', kind: 'plugin', status,
  payload: { plugin_id: 'p1', name: 'Mail', tool: 'draft_email', arguments: { to: ['ada@example.test'] }, effect: effect(extra), ...(result ? { result } : {}) } });
const confirmed = new Set();
function answer(method, params) {
  if (method === 'connector_send_confirm') {
    if (params.id !== 'chat-a' || params.recipients_sha256 !== R) throw new Error('Only a pending connector effect with these exact reviewed recipients can be confirmed.');
    confirmed.add(params.action_id);
    return { action_id: params.action_id, recipients_sha256: R, state: 'confirmed', confirmed_recipients: 1 };
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
function hold(method) { let release; gates.set(method, new Promise(resolve => { release = resolve; })); return () => release(); }

const source = readFileSync(new URL('./ConnectorEffectReview.vue', import.meta.url), 'utf8');
const descriptor = parse(source, { filename: 'ConnectorEffectReview.vue' }).descriptor;
const script = compileScript(descriptor, { id: 'effect' });
const template = compileTemplate({ source: descriptor.template.content, filename: 'ConnectorEffectReview.vue', id: 'effect', compilerOptions: { bindingMetadata: script.bindings } });
assert.deepEqual(template.errors, []);
function evaluate(code) {
  const module = { exports: {} };
  new Function('require', 'module', 'exports', transform(code, { transforms: ['typescript', 'imports'] }).code)(
    name => name === '../bridge' ? bridge : require(name), module, module.exports);
  return module.exports;
}
const Panel = evaluate(script.content).default;
Panel.render = evaluate(template.code).render;
assert.ok(!/v-html|\binnerHTML\b|\beval\s*\(|new Function|<iframe|srcdoc|document\.write/.test(source), 'connector data renders as literal native content');
const methods = [...source.matchAll(/\brpc(?:<[^>]*>)?\(([^,)]*)/g)].map(match => match[1].trim());
assert.ok(methods.length && methods.every(method => /^'[a-z_]+'$/.test(method)), 'every native method name is a fixed literal');
assert.deepEqual([...new Set(methods)].sort(), ["'connector_send_confirm'"], 'only the recipient confirmation is reachable; nothing approves or sends');
assert.ok(!/['"](approve|reject)['"]/.test(source), 'no approval or dismissal route is named');
assert.deepEqual([...source.matchAll(/from '([^']+)'/g)].map(match => match[1]).sort(), ['../bridge', 'vue']);
for (const file of ['../Pet.vue', './NativeArtifactRenderer.vue', './ArtifactPanel.vue', './ModsSettings.vue', './Markdown.vue', './ActionReview.vue'])
  assert.ok(!/ScreenSessions|PebbleBrowser|ConnectorEffectReview|ConnectorRouteSettings/.test(readFileSync(new URL(file, import.meta.url), 'utf8')), file + ' renders no connector confirmation');

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
const props = reactive({ action: plugin(A1), chatId: 'chat-a', disabled: false });
const root = node('root');
const view = renderer.createApp({ setup: () => () => h(Panel, { ...props, onConfirmed: id => events.push('confirmed:' + id) }) });
view.config.errorHandler = error => { throw error; };
const count = method => calls.filter(call => call.method === method).length;
const region = label => all(root).find(element => element.props['aria-label'] === label);
const status = () => textOf(all(root).find(element => element.props.role === 'status'));
const alert = () => all(root).find(element => element.props.role === 'alert');
const confirmButton = () => all(root).find(item => item.type === 'button' && textOf(item) === 'Confirm recipients');
const facts = () => {
  const list = region('Connector effect facts'), rows = {};
  for (let index = 0; list && index < list.children.length; index++) {
    const item = list.children[index];
    if (item.type === 'dt') rows[textOf(item)] = textOf(list.children[index + 1]);
  }
  return rows;
};
async function click(target) {
  assert.ok(target, 'missing button');
  assert.ok(!target.props.disabled, textOf(target) + ' must be enabled');
  await target.props.onClick({});
  await settle();
}

view.mount(root);
await settle();
assert.equal(calls.length, 0, 'showing the card calls nothing');
let page = textOf(root);
assert.ok(region('Connector effect: Send'));
assert.ok(page.includes('Recipients, subject, message and names come from the model and the connector. They are shown as plain text and approve nothing.'));
assert.ok(page.includes('Send · Delivers to the recipients below when you approve. A sent message cannot be recalled.'));
assert.ok(page.includes('This kind comes from the connector effects you reviewed.'));
assert.deepEqual(facts(), {
  Kind: 'Send',
  Connector: 'Mail ' + hostile + " · credential in the local server's environment, readable by other programs you run",
  Subject: scripted,
  Recipients: '2 addresses (listed below)',
  'Recipient set': '777777777777…',
  'Message SHA-256': '888888888888…',
  'Repeat protection': 'Pebble adds a one-time key, so the service can ignore a repeat of this exact call.',
}, 'only reported facts are shown, and hostile names stay plain text');
const listed = all(region('Recipients')).filter(item => item.type === 'li').map(textOf);
assert.deepEqual(listed, ['ada@example.test · named by you', outsider + ' · found in tool or page output, not from you']);
assert.ok(page.includes('Message preview (plain text)Hello ' + hostile));
assert.ok(page.includes('An identical send already completed (action ' + PRIOR + '). Approving does it again.'), 'a repeat is named before approval');
let card = textOf(region('Confirm recipients'));
assert.ok(card.includes("Some recipients didn't come from you. Pebble won't send to them unless you confirm these recipients here:" + outsider));
// Phase 24 (V1-120, m24c merge): the card's second button is "Don't allow" now (it was Dismiss); same check, new name.
assert.ok(card.includes("Confirming sends nothing. You still approve the call or choose Don't allow."));
assert.ok(!all(root).some(element => ['img', 'script'].includes(element.type)), 'no hostile element is created');

// While the parent is busy nothing is confirmed.
props.disabled = true;
await settle();
assert.equal(confirmButton().props.disabled, true);
props.disabled = false;
await settle();

// A refusal is surfaced and confirms nothing.
failures.set('connector_send_confirm', 'Only a pending connector effect with these exact reviewed recipients can be confirmed.');
await click(confirmButton());
assert.equal(textOf(alert()), 'Only a pending connector effect with these exact reviewed recipients can be confirmed.');
assert.ok(confirmButton() && !events.length, 'a refused confirmation emits nothing and keeps the button');

// Confirming sends exactly the chat, the action and the reviewed recipient set, and nothing else.
await click(confirmButton());
assert.deepEqual(calls.at(-1), { method: 'connector_send_confirm', params: { id: 'chat-a', action_id: A1, recipients_sha256: R } });
assert.equal(status(), 'Recipients confirmed. Nothing was sent; approve the call to send it.');
assert.equal(alert(), undefined);
assert.deepEqual(events, ['confirmed:' + A1]);
assert.ok(!confirmButton() && textOf(region('Confirm recipients')).includes('You confirmed these recipients.'));
assert.deepEqual([...new Set(calls.map(call => call.method))], ['connector_send_confirm'], 'nothing was approved or sent');

// Another action starts unconfirmed.
props.action = plugin(A2, { class: 'delete', recipients: [outsider], recipient_count: 1, recipient_sources: {}, unconfirmed_recipients: [outsider], repeat_of: [], subject: null, body_preview: null, body_sha256: null });
await settle();
page = textOf(root);
assert.ok(page.includes('Delete · Removes data from the connected account when you approve.'));
assert.ok(confirmButton() && textOf(region('Confirm recipients')).includes("Pebble won't run this call with them unless you confirm these recipients here:"));
assert.deepEqual(all(region('Recipients')).filter(item => item.type === 'li').map(textOf), [outsider + ' · not from you'], 'a recipient without a source is not from the person');
assert.ok(!page.includes('already completed') && !page.includes('Message preview'));
assert.ok(!('Subject' in facts()) && !('Message SHA-256' in facts()), 'absent facts are omitted');

// A settled action cannot be confirmed; the key that was sent is shown.
props.action = plugin(A2, {}, 'applied', { idempotency: 'pebble-' + A2, effect_class: 'send' });
await settle();
assert.equal(confirmButton().props.disabled, true, 'only a pending action can be confirmed');
assert.equal(facts()['One-time key sent'], 'pebble-' + A2);

// A recipient set without a valid hash cannot be confirmed.
props.action = plugin(A2, { recipients_sha256: 'not-a-hash' });
await settle();
// Phase 24 (V1-120, m24c merge): the same refusal names the button the card shows now, Don't allow.
assert.ok(!confirmButton() && textOf(root).includes("This recipient set can't be confirmed. Choose Don't allow instead."));
assert.ok(!('Recipient set' in facts()));

// Drafts deliver nothing and need no confirmation.
props.action = plugin(A2, { class: 'draft', requires_confirmation: false, unconfirmed_recipients: [], idempotency: 'none', repeat_of: [] });
await settle();
page = textOf(root);
assert.ok(page.includes('Draft · Saves a draft only. Nothing is delivered to anyone.') && !region('Confirm recipients'));
assert.equal(facts()['Repeat protection'], 'No one-time key. If the outcome is uncertain, Pebble checks it instead of retrying.');

// An unreviewed connector is labelled; an unknown class is treated as a send.
props.action = plugin(A2, { class: 'teleport', assigned: 'unreviewed', recipients: [], recipient_count: 0, recipient_sources: {}, unconfirmed_recipients: [], requires_confirmation: false,
  connector: { name: 'Legacy', credential_exposure: 'host_only' }, idempotency: 'surprise', repeat_of: [] });
await settle();
page = textOf(root);
assert.ok(region('Connector effect: Send') && page.includes('This connector has no reviewed effects, so Pebble treats every call as a send it cannot check.'));
assert.equal(facts().Recipients, 'none');
assert.equal(facts().Connector, 'Legacy · credential held by Pebble only');
assert.equal(facts()['Repeat protection'], 'Not reported.');

// Nothing renders for other actions or for a plugin call without a classification.
props.action = { id: A2, kind: 'command', status: 'pending', payload: { effect: effect() } };
await settle();
assert.equal(textOf(root), '', 'only plugin actions show a connector effect');
props.action = { id: A2, kind: 'plugin', status: 'pending', payload: { name: 'Mail', tool: 'x', arguments: {} } };
await settle();
assert.equal(textOf(root), '', 'an unclassified plugin call keeps the existing card only');

// A confirmation that settles after the card closes emits nothing.
props.action = plugin(A2);
await settle();
const release = hold('connector_send_confirm');
const seen = events.length;
const late = confirmButton().props.onClick({});
await settle();
assert.equal(confirmButton().props.disabled, true, 'a confirmation in flight cannot be repeated');
view.unmount();
release();
await late;
await settle();
assert.equal(events.length, seen, 'a reply after unmount emits nothing');
assert.equal(count('connector_send_confirm'), 3, 'one refused, one confirmed and one late confirmation; nothing else was called');
console.log('connector effect review: ok');
