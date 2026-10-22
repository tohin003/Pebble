// Pebble for Chrome, Phase 22: what a share is, when it ends, and which pages hand back to the person.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { credentialUrl, handbackReasons } from './lib/credential.mjs';
import { emptyShares, forBackend, MAX_SHARES, navigate, restore, setHandback, shareOf, shareTab, stopAll, unshareTab } from './lib/shares.mjs';

const id = n => n.toString(16).padStart(32, '0');
const share = (state, tabId, url, scope = 'tab', extra = {}) => shareTab(state, { tabId, url, title: 'T', scope, now: 1, id: id(tabId), ...extra });

test('a share is one tab on one https site (or this Mac), never a browser or store page', () => {
  let { state, share: one } = share(emptyShares(), 7, 'https://mail.example/inbox?sid=1');
  assert.equal(one.origin, 'https://mail.example');
  assert.equal(one.state, 'ready');
  for (const url of ['chrome://settings', 'chrome-extension://x/popup.html', 'file:///Users/me/a.txt', 'http://mail.example/',
    'https://chromewebstore.google.com/detail/x', 'about:blank', 'view-source:https://mail.example/']) {
    assert.equal(share(state, 8, url).error, 'unshareable', url);
  }
  assert.equal(share(state, 8, 'https://x.example/', 'everything').error, 'invalid');
  for (let tab = 100; tab < 100 + MAX_SHARES - 1; tab++) state = share(state, tab, 'https://many.example/').state;
  assert.equal(share(state, 999, 'https://one-more.example/').error, 'limit');
});

test("a 'tab' share ends when the tab leaves its site; a 'site' share waits away and comes back", () => {
  let state = share(emptyShares(), 1, 'https://mail.example/', 'tab').state;
  state = share(state, 2, 'https://docs.example/', 'site').state;
  let result = navigate(state, 1, 'https://mail.example/compose', 'Compose');
  assert.equal(result.change, 'same');
  state = result.state;
  assert.equal(shareOf(state, 1).nav, 1);
  result = navigate(state, 1, 'https://evil.example/', 'Evil');
  assert.equal(result.change, 'cleared');
  state = result.state;
  assert.equal(shareOf(state, 1), null);
  result = navigate(state, 2, 'https://elsewhere.example/', 'Elsewhere');
  assert.equal(result.change, 'away');
  state = result.state;
  assert.deepEqual([shareOf(state, 2).state, shareOf(state, 2).url, shareOf(state, 2).title], ['away', '', ''],
    'nothing of the other site is kept');
  result = navigate(state, 2, '', '');
  assert.equal(result.change, 'away', 'an address Chrome hides from the extension counts as another site');
  result = navigate(result.state, 2, 'https://docs.example/a', 'A');
  assert.equal(result.change, 'back');
  assert.equal(shareOf(result.state, 2).state, 'ready');
  assert.equal(navigate(result.state, 99, 'https://x.example/', '').change, 'none');
});

test('hand-back, stop and what the backend sees', () => {
  let state = share(emptyShares(), 1, 'https://mail.example/').state;
  state = share(state, 2, 'https://docs.example/', 'site').state;
  let result = setHandback(state, 1, ['password']);
  assert.equal(result.changed, true);
  state = result.state;
  assert.equal(setHandback(state, 1, ['password']).changed, false);
  state = navigate(state, 2, 'https://other.example/x', 'X').state;
  const rows = forBackend(state, new Set([1]));
  assert.deepEqual(rows.map(row => [row.tab_id, row.state, row.scope, row.acting]), [['1', 'handback', 'tab', true], ['2', 'away', 'site', false]]);
  assert.equal(rows[0].title, '', 'a hand-back page title is not sent');
  assert.equal(rows[1].url, null);
  assert.ok(rows.every(row => /^[0-9a-f]{32}$/.test(row.share_id)));
  const stopped = stopAll(state);
  assert.equal(stopped.removed.length, 2);
  assert.deepEqual(stopped.state, emptyShares());
  assert.equal(unshareTab(state, 5).removed, null);
});

test('stored shares are checked again and anything malformed is dropped', () => {
  const state = share(emptyShares(), 3, 'https://mail.example/').state;
  assert.deepEqual(restore(JSON.parse(JSON.stringify(state))), state);
  const bad = { shares: { 3: { ...state.shares[3], origin: 'http://evil.example' }, 4: { ...state.shares[3], tabId: 5 }, 6: null } };
  assert.deepEqual(restore(bad), emptyShares());
  assert.deepEqual(restore('nonsense'), emptyShares());
});

test('sign-in, password, payment and verification pages hand back by address or by their fields', () => {
  for (const url of ['https://accounts.google.com/v3/signin', 'https://github.com/login', 'https://github.com/sessions/two-factor',
    'https://shop.example/checkout', 'https://shop.example/cart/checkout/step-2', 'https://bank.example/auth/otp',
    'https://login.example.com/', 'https://pay.example.com/x', 'https://app.example/settings/password', 'https://x.example/oauth2/authorize',
    'https://checkout.stripe.com/c/pay/cs_live', 'https://site.example/sign-in?next=/']) {
    assert.equal(credentialUrl(url), true, url);
  }
  for (const url of ['https://mail.example/inbox', 'https://shop.example/products/payday-loans-explained', 'https://blog.example/author/sam',
    'https://news.example/security-update', 'https://docs.example/verifying-claims', 'not a url']) {
    assert.equal(credentialUrl(url), false, url);
  }
  // Conservative on purpose: a slug that starts with a credential word hands back too (a false hand-back only costs a click).
  assert.equal(credentialUrl('https://docs.example/verify-your-claims-guide'), true);
  assert.deepEqual(handbackReasons('https://mail.example/', { password: 1, one_time_code: 0, payment: 2, captcha: 0 }), ['password', 'payment']);
  assert.deepEqual(handbackReasons('https://github.com/login', null), ['address']);
  assert.deepEqual(handbackReasons('https://mail.example/', { password: '1', payment: -1 }), [], 'malformed counts do nothing');
});
