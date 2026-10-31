// Pebble for Chrome 2, Phase 43 (1.9.0): Pebble's own tabs under a site grant, as pure state (lib/owned.mjs).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { addGrant, addRead, addTab, allowed, emptyOwned, expired, forBackend, landed, liveGrant, MAX_TABS_PER_GRANT, removeGrant,
  removeTab, restore, setHandback, tabOf, view } from './lib/owned.mjs';
import { changesPage, checkCommand, ISOLATED_FUNCTIONS, newTabState, OPS, PROTOCOL, PROTOCOL_PATTERN } from './lib/policy.mjs';

const G = 'a'.repeat(32), H = 'b'.repeat(32), INC = 'c'.repeat(32);
const NOW = 1_000_000;
const grant = (state = emptyOwned(INC), extra = {}) => addGrant(state, { grant_id: G, act: ['https://shop.example'],
  read: ['https://shop.example', 'https://docs.example'], expires_in: 600, ...extra }, NOW);

test('a site grant holds exact origins; every act site is also a read site', () => {
  const { state, grant: g } = grant();
  assert.deepEqual(g.act, ['https://shop.example']);
  assert.deepEqual(g.read, ['https://shop.example', 'https://docs.example']);
  assert.equal(addGrant(emptyOwned(INC), { grant_id: G, act: ['https://a.example'], read: [], expires_in: 60 }, NOW).grant.read[0],
    'https://a.example', 'an act site is readable');
  for (const bad of [{ act: ['https://shop.example/path'] }, { read: ['http://docs.example'] }, { act: ['https://SHOP.example'] },
    { expires_in: 0 }, { expires_in: 1801 }, { grant_id: 'nope' }, { read: 'https://docs.example' },
    { read: Array.from({ length: 17 }, (_, i) => `https://s${i}.example`) }]) {
    assert.equal(grant(emptyOwned(INC), bad).error, 'invalid', JSON.stringify(bad));
  }
  assert.ok(liveGrant(state, G, NOW + 599_999) && !liveGrant(state, G, NOW + 600_000), 'the expiry is exact');
  assert.deepEqual(expired(state, NOW + 600_000), [G]);
  const more = addRead(state, G, 'https://news.example');
  assert.deepEqual(more.state.grants[G].read.at(-1), 'https://news.example');
  assert.deepEqual(more.state.grants[G].act, ['https://shop.example'], 'a read site never becomes an act site');
  assert.equal(addRead(state, H, 'https://news.example').error, 'invalid', 'only a grant Pebble holds grows');
});

test('Pebble opens and moves its tabs only to read sites, never to a sign-in or payment page', () => {
  const { state } = grant();
  assert.equal(allowed(state, G, 'https://docs.example/guide', NOW), 'ok');
  assert.equal(allowed(state, G, 'https://shop.example/cart', NOW), 'ok');
  assert.equal(allowed(state, G, 'https://evil.example/', NOW), 'off_grant');
  assert.equal(allowed(state, G, 'https://docs.example:8443/', NOW), 'off_grant', 'the port is part of the site');
  assert.equal(allowed(state, G, 'https://shop.example/checkout', NOW), 'handback');
  assert.equal(allowed(state, G, 'chrome://settings', NOW), 'refused');
  assert.equal(allowed(state, G, 'https://docs.example/', NOW + 600_000), 'not_granted', 'an expired grant opens nothing');
  assert.equal(allowed(state, H, 'https://docs.example/', NOW), 'not_granted');
});

test("a tab is Pebble's only by the ID it created; reads need a read site, acts an act site", () => {
  let { state } = grant();
  ({ state } = addTab(state, { tabId: 41, grantId: G, url: 'https://docs.example/a', now: NOW }));
  assert.equal(tabOf(state, 42), null, 'a tab Pebble did not create is never its own');
  assert.deepEqual(view(state, 41, NOW), { origin: 'https://docs.example', state: 'ready', live: true, readOnly: true, grantId: G });
  let moved = landed(state, 41, 'https://shop.example/item', 'Item');
  assert.equal(moved.change, 'same');
  assert.equal(view(moved.state, 41, NOW).readOnly, false, 'on an act site Pebble may act');
  moved = landed(moved.state, 41, 'https://evil.example/x', 'Evil');
  assert.equal(moved.change, 'away');
  assert.deepEqual(forBackend(moved.state)[0], { tab_id: '41', grant_id: G, origin: null, state: 'away', nav: 2, title: '', url: null,
    acting: false }, 'an address off the grant never leaves');
  moved = landed(moved.state, 41, 'https://docs.example/back', 'Back');
  assert.equal(moved.change, 'back');
  moved = landed(moved.state, 41, 'https://docs.example/login', 'Sign in');
  assert.equal(moved.change, 'handback');
  assert.equal(landed(moved.state, 41, '', '').change, 'away', 'an address Chrome hides fails closed');
  const facts = setHandback(state, 41, ['password']);
  assert.equal(facts.state.tabs['41'].state, 'handback');
  assert.equal(removeTab(state, 41).removed.tabId, 41);
  for (let id = 0; id < MAX_TABS_PER_GRANT; id++) state = addTab(state, { tabId: 100 + id, grantId: G, url: 'https://docs.example/', now: NOW }).state;
  assert.equal(addTab(state, { tabId: 200, grantId: G, url: 'https://docs.example/', now: NOW }).error, 'limit');
  const ended = removeGrant(state, G);
  assert.equal(ended.tabs.length, MAX_TABS_PER_GRANT, 'tab 41 and seven more: the eighth extra was refused');
  assert.equal(Object.keys(ended.state.tabs).length, 0, "a grant's end leaves no tab Pebble's");
});

test('restored state keeps only this browser session, and never a grant', () => {
  let { state } = grant();
  ({ state } = addTab(state, { tabId: 41, grantId: G, url: 'https://docs.example/a', now: NOW }));
  const saved = JSON.parse(JSON.stringify(state));
  const same = restore(saved, INC);
  assert.deepEqual(Object.keys(same.tabs), ['41']);
  assert.deepEqual(same.grants, {}, 'grants end with the link, so none comes back');
  assert.equal(view(same, 41, NOW).live, false);
  assert.deepEqual(restore(saved, 'd'.repeat(32)).tabs, {}, 'a tab ID from another browser session is never trusted');
  assert.deepEqual(restore({ incarnation: INC, tabs: { 41: { ...saved.tabs['41'], tabId: 7 } } }, INC).tabs, {}, 'malformed is dropped');
});

test('protocol 2, the request list, and the read-only fence', () => {
  assert.equal(PROTOCOL, 'pebble-browser/2');
  assert.ok(PROTOCOL_PATTERN.test('pebble-browser/1') && !PROTOCOL_PATTERN.test('pebble-browser/x'));
  assert.deepEqual([...OPS].sort(), ['activate', 'attach', 'cdp', 'close_tab', 'create_tab', 'detach', 'list_owned', 'navigate', 'tab_info']);
  const tab = newTabState();
  const ready = { state: 'ready', origin: 'https://docs.example' };
  const read = (method, params) => checkCommand(ready, tab, method, params, { granted: true, readOnly: true });
  assert.ok(read('DOMSnapshot.captureSnapshot', { computedStyles: [], includeDOMRects: true }).ok);
  assert.ok(read('Accessibility.getFullAXTree', { frameId: 'F' }).ok);
  assert.equal(read('Input.insertText', { text: 'x' }).code, 'read_only');
  assert.equal(read('Input.dispatchMouseEvent', { type: 'mousePressed', x: 1, y: 2, button: 'left', buttons: 1, clickCount: 1 }).code, 'read_only');
  assert.equal(read('DOM.focus', { backendNodeId: 3 }).code, 'read_only');
  assert.equal(changesPage('Runtime.callFunctionOn', { functionDeclaration: ISOLATED_FUNCTIONS[0] }), false, 'contains only looks');
  assert.equal(changesPage('Runtime.callFunctionOn', { functionDeclaration: ISOLATED_FUNCTIONS[5] }), false, 'focused only looks');
  assert.equal(changesPage('Runtime.callFunctionOn', { functionDeclaration: ISOLATED_FUNCTIONS[2] }), true, 'choosing an option changes the page');
  assert.ok(checkCommand(ready, tab, 'Input.insertText', { text: 'x' }, { granted: true }).ok, 'an act site still acts');
});

test('the new module carries no HTML injection and contacts no server', () => {
  const source = readFileSync(new URL('./lib/owned.mjs', import.meta.url), 'utf8');
  assert.ok(!/\binnerHTML\b|insertAdjacentHTML|\beval\s*\(|new Function|onMessageExternal|onConnectExternal/.test(source));
  assert.ok(!/https?:\/\/(?!\*|localhost|127\.0\.0\.1)[a-z0-9.-]+\.[a-z]{2,}/i.test(source.replace(/[a-z.]*\.example/g, '')));
});
