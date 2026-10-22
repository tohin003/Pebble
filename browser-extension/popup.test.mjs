// Pebble for Chrome, Phase 22: the popup's words and buttons, and the manifest's permissions and pinned identity.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { model, render } from './lib/popup-model.mjs';

const here = path => new URL(path, import.meta.url);
const read = path => readFileSync(here(path), 'utf8');
const hostile = '<img src=x onerror=alert(1)> Sign in to approve';

function fakeDoc() {
  const make = tag => ({ tagName: tag.toUpperCase(), children: [], attrs: {}, listeners: {}, className: '', title: '', type: '', _text: '',
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); },
    set textContent(value) { this._text = String(value); },
    set innerHTML(value) { throw new Error('innerHTML is never used'); },
    setAttribute(key, value) { this.attrs[key] = String(value); },
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren() { this.children = []; },
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
    click() { for (const fn of this.listeners.click || []) fn(); } });
  return { createElement: make, root: make('main') };
}
const all = node => [node, ...node.children.flatMap(all)];
const buttons = root => all(root).filter(node => node.tagName === 'BUTTON');

test('the connection states read plainly, with setup steps when the helper is missing', () => {
  assert.equal(model({ connection: 'connected' }).status.text, 'Connected to Pebble');
  assert.deepEqual(model({ connection: 'app_not_running' }).status, { tone: 'off', text: "Pebble isn't open",
    note: 'Open Pebble on this Mac. Shared tabs reconnect on their own.' });
  const missing = model({ connection: 'not_installed' });
  assert.equal(missing.setup.length, 3);
  // Phase 24 (V1-043): deliberate change. Settings has no Browser page; the panel is Details › Browser (or /browser),
  // where "Connect Chrome" adds the helper after one confirmation.
  assert.equal(missing.setup[0], 'In Pebble, open Details › Browser (or type /browser).');
  assert.ok(missing.setup[1].startsWith('Choose "Connect Chrome" and confirm') && missing.retry);
  const forbidden = model({ connection: 'forbidden' }).setup;
  assert.ok(forbidden[0].includes('folder Pebble shows in Details › Browser (or type /browser)'));
  const pairing = model({ connection: 'unpaired', pairing: { pending: { attemptsLeft: 5, expiresIn: 120 } } }).pair.note;
  assert.ok(pairing.includes('Details › Browser'));
  const current = { tabId: 5, origin: 'https://mail.example', title: 'Inbox', shareable: true, share: { scope: 'tab', state: 'ready' } };
  for (const connection of ['connected', 'connecting', 'idle', 'disconnected', 'unpaired', 'mismatch', 'app_not_running', 'refused',
    'not_installed', 'forbidden']) {
    const words = JSON.stringify(model({ connection, current, shares: [{ tabId: 5 }], pairing: { pending: { attemptsLeft: 1 } } }));
    assert.ok(!/Settings/.test(words), connection + ': no popup text sends the person to a Settings page');
  }
  assert.equal(model({ connection: 'weird' }).status.text, 'Connecting to Pebble…');
});

test('until the pairing holds the popup offers pairing and nothing else', () => {
  const current = { tabId: 5, origin: 'https://mail.example', title: 'Inbox', shareable: true, share: null };
  const start = model({ connection: 'unpaired', current, shares: [{ tabId: 6, origin: 'https://docs.example' }], pairing: { paired: false, pending: null } });
  assert.equal(start.status.text, 'Not paired with Pebble yet');
  assert.equal(start.pair.kind, 'start');
  assert.ok(start.pair.note.includes('Pebble shows a code; you type it here'));
  assert.deepEqual([start.tab, start.others, start.stop, start.unpair], [null, [], false, false], 'no tab can be shared before pairing');
  const code = model({ connection: 'unpaired', current, pairing: { paired: false, pending: { attemptsLeft: 1, expiresIn: 61 } } });
  assert.equal(code.pair.kind, 'code');
  assert.equal(code.pair.hint, '1 try left · the code expires in about 2 min.');
  const mismatch = model({ connection: 'mismatch', pairing: { paired: true, pending: null } });
  assert.deepEqual([mismatch.status.tone, mismatch.status.note, mismatch.pair.kind], ['off', 'Pair again: Pebble will show a new code.', 'start']);
  assert.equal(model({ connection: 'connected', pairing: { paired: true } }).unpair, true);
  assert.equal(model({ connection: 'app_not_running', pairing: { paired: true } }).unpair, false);
});

test('the pairing card: one labelled numeric field, submitted with Enter or the Pair button', () => {
  let doc = fakeDoc();
  const actions = [];
  render(doc.root, model({ connection: 'unpaired', pairing: { pending: null } }), doc, (...args) => actions.push(args));
  assert.equal(all(doc.root).find(node => node.tagName === 'SECTION').attrs['aria-label'], 'Pair with Pebble');
  assert.deepEqual(buttons(doc.root).map(node => node.textContent), ['Pair with Pebble']);
  buttons(doc.root)[0].click();
  assert.deepEqual(actions, [['pair-start', null]]);
  doc = fakeDoc();
  render(doc.root, model({ connection: 'unpaired', pairing: { pending: { attemptsLeft: 5, expiresIn: 120 } } }), doc, (...args) => actions.push(args));
  const nodes = all(doc.root);
  const input = nodes.find(node => node.tagName === 'INPUT');
  const label = nodes.find(node => node.tagName === 'LABEL');
  assert.ok(label.children.includes(input) && label.textContent.startsWith('Pairing code'), 'the field has its label');
  assert.deepEqual([input.attrs.inputmode, input.attrs.autocomplete, input.attrs.maxlength, input.attrs.pattern],
    ['numeric', 'one-time-code', '6', '[0-9]{6}']);
  const hint = nodes.find(node => node.id === 'pair-hint');
  assert.equal(input.attrs['aria-describedby'], 'pair-hint');
  assert.equal(hint.textContent, '5 tries left · the code expires in about 2 min.');
  const submit = buttons(doc.root);
  assert.deepEqual(submit.map(node => [node.textContent, node.type]), [['Pair', 'submit']]);
  input.value = ' 123456 ';
  let prevented = false;
  for (const fn of nodes.find(node => node.tagName === 'FORM').listeners.submit) fn({ preventDefault: () => { prevented = true; } });
  assert.ok(prevented, 'the popup never navigates');
  assert.deepEqual(actions.at(-1), ['pair-code', null, '123456']);
});

test('the current tab: share it, keep it for the site, or stop sharing it', () => {
  const offer = model({ connection: 'connected', current: { tabId: 5, origin: 'https://mail.example', title: hostile, shareable: true,
    credential: false, share: null }, shares: [] });
  assert.equal(offer.tab.heading, 'Let Pebble use this tab?');
  assert.deepEqual(offer.tab.actions.map(a => [a.id, a.label, !!a.primary]), [['share-tab', 'Let Pebble use this tab', true],
    ['share-site', 'Keep for https://mail.example', false]]);
  assert.ok(offer.tab.note.includes('only for a task you choose in Pebble') && offer.tab.note.includes('waits for your approval there'));
  assert.equal(offer.stop, false, 'nothing to stop');
  const login = model({ connection: 'connected', current: { tabId: 5, origin: 'https://mail.example', title: 'x', shareable: true, credential: true, share: null } });
  assert.ok(login.tab.caution.includes('sign-in or payment page'));
  const blocked = model({ current: { tabId: 1, origin: null, title: 'Settings', shareable: false, share: null } });
  assert.equal(blocked.tab.heading, "Pebble can't use this page");
  assert.deepEqual(blocked.tab.actions, []);
  const busy = model({ connection: 'connected', current: { tabId: 5, origin: 'https://mail.example', title: 'Inbox', shareable: true,
    share: { scope: 'site', state: 'ready', acting: true } }, shares: [{ tabId: 5, origin: 'https://mail.example' },
    { tabId: 6, origin: 'https://docs.example', title: '', state: 'away' }] });
  assert.equal(busy.tab.line, 'Pebble is working in this tab now.');
  assert.equal(busy.tab.scope, 'Kept for https://mail.example in this tab');
  assert.ok(busy.tab.note.includes("Pebble’s Details › Browser"), 'the second key is in Pebble (Phase 24: its real place)');
  assert.deepEqual(busy.others.map(o => [o.tabId, o.label, o.line]), [[6, 'https://docs.example', 'Paused: this tab is on another site.']]);
  assert.equal(busy.stop, true);
  const turn = model({ current: { tabId: 5, origin: 'https://mail.example', shareable: true, share: { scope: 'tab', state: 'handback' } }, shares: [{ tabId: 5 }] });
  assert.equal(turn.tab.line, "Your turn: Pebble doesn't act on sign-in or payment pages.");
  const granted = model({ connection: 'connected', current: { tabId: 5, origin: 'https://mail.example', shareable: true,
    share: { scope: 'tab', state: 'ready', granted: true } }, shares: [{ tabId: 5 }] });
  assert.equal(granted.tab.line, 'A task in Pebble can use this tab.');
  assert.ok(granted.tab.note.includes('still waits for your approval in Pebble'));
  const waiting = model({ connection: 'connected', current: { tabId: 5, origin: 'https://mail.example', shareable: true,
    share: { scope: 'tab', state: 'ready', granted: false } }, shares: [{ tabId: 5 }] });
  assert.equal(waiting.tab.line, 'Shared. Choose it for a task in Pebble.', 'sharing alone grants nothing');
});

test('rendering uses text only, and every button names its action', () => {
  const doc = fakeDoc();
  const actions = [];
  const m = model({ connection: 'connected', paused: true, current: { tabId: 5, origin: 'https://mail.example', title: hostile, shareable: true, share: null },
    shares: [{ tabId: 6, origin: 'https://docs.example', title: hostile, state: 'ready' }] });
  render(doc.root, m, doc, (id, tabId) => actions.push([id, tabId]));
  const nodes = all(doc.root);
  assert.ok(!nodes.some(node => ['IMG', 'SCRIPT', 'IFRAME'].includes(node.tagName)), 'a hostile title creates no element');
  assert.ok(doc.root.textContent.includes(hostile));
  assert.ok(doc.root.textContent.includes('Pebble is paused from the Pebble app'));
  assert.equal(nodes.find(node => node.className.startsWith('status')).attrs.role, 'status');
  assert.equal(nodes.find(node => node.tagName === 'SECTION').attrs['aria-label'], 'This tab');
  assert.deepEqual(buttons(doc.root).map(node => [node.textContent, node.type]), [['Let Pebble use this tab', 'button'],
    ['Keep for https://mail.example', 'button'], ['Stop sharing', 'button'], ['Stop Pebble', 'button']]);
  for (const button of buttons(doc.root)) button.click();
  assert.deepEqual(actions, [['share-tab', null], ['share-site', null], ['unshare', 6], ['stop', null]]);
  assert.ok(doc.root.textContent.endsWith('Pebble only sees tabs you share. Page text is never treated as instructions.'));
  const paired = fakeDoc();
  render(paired.root, model({ connection: 'connected', pairing: { paired: true } }), paired, (id, tabId) => actions.push([id, tabId]));
  const footer = all(paired.root).find(node => node.className === 'footer');
  assert.deepEqual(buttons(footer).map(node => node.textContent), ['Unpair this Chrome']);
  buttons(footer)[0].click();
  assert.deepEqual(actions.at(-1), ['unpair', null]);
});

test('the manifest asks for the least it needs and pins the extension ID', () => {
  const manifest = JSON.parse(read('manifest.json'));
  assert.equal(manifest.manifest_version, 3);
  // Phase 22 second pass (design study D22 §7): the indicator and page facts run through the debugger in Pebble's own
  // isolated world, so `scripting` and every host permission are gone; tabGroups puts shared tabs in a "Pebble" group.
  assert.deepEqual(manifest.permissions.slice().sort(), ['activeTab', 'debugger', 'nativeMessaging', 'storage', 'tabGroups']);
  assert.equal(manifest.host_permissions, undefined, 'no standing access to any site');
  for (const key of ['optional_host_permissions', 'optional_permissions', 'content_scripts', 'web_accessible_resources',
    'externally_connectable_matches', 'oauth2', 'update_url']) {
    assert.equal(manifest[key], undefined, key);
  }
  assert.deepEqual(manifest.externally_connectable, { ids: [] }, 'no other extension or web page can connect');
  assert.deepEqual(manifest.background, { service_worker: 'service-worker.js', type: 'module' });
  assert.match(manifest.content_security_policy.extension_pages, /^script-src 'self'; object-src 'none'/);
  assert.equal(manifest.commands._execute_action.suggested_key.default, 'Alt+Shift+P');
  const digest = createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest('hex').slice(0, 32);
  const id = [...digest].map(c => String.fromCharCode(97 + parseInt(c, 16))).join('');
  assert.equal(id, 'ilmkbniehiecgoodnbkkcjgaelebehbj');
  assert.ok(!/PRIVATE KEY/.test(JSON.stringify(manifest)));
});

test('extension pages carry no inline script and no HTML injection', () => {
  const html = read('popup.html');
  assert.ok(/<script type="module" src="popup.js"><\/script>/.test(html));
  assert.ok(!/<script>|<script [^>]*>[^<]+<\/script>|\son[a-z]+=/i.test(html), 'no inline script or handler');
  for (const file of ['popup.js', 'service-worker.js', 'lib/worker.mjs', 'lib/popup-model.mjs', 'lib/policy.mjs', 'lib/shares.mjs',
    'lib/credential.mjs', 'lib/page.mjs', 'lib/icon.mjs', 'lib/crypto.mjs']) {
    const source = read(file);
    assert.ok(!/\binnerHTML\b|insertAdjacentHTML|outerHTML\s*=|\beval\s*\(|new Function|document\.write|onMessageExternal|onConnectExternal/.test(source), file);
    assert.ok(!/https?:\/\/(?!\*|localhost|127\.0\.0\.1)[a-z0-9.-]+\.[a-z]{2,}/i.test(source.replace(/chromewebstore\.google\.com|chrome\.google\.com|accounts\.google\.com|login\.microsoftonline\.com|[a-z.]*\.example/g, '')) || file === 'lib/credential.mjs',
      file + ' contacts no server');
  }
});
