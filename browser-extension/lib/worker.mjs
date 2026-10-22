// Pebble for Chrome's service worker logic (Phase 22). service-worker.js only calls createWorker(chrome).start();
// every Chrome API arrives through `chrome`, so the tests drive this file with a fake browser.
//
// A thin relay; Pebble keeps the brain (design study D22 §7):
// - Pairing: the first time, the person types the 6-digit code Pebble's main window shows. The pairing key stays in
//   chrome.storage.local; after the welcome every message either way is sealed with a per-link session key
//   (lib/crypto.mjs). Unsealed or wrongly signed messages from Pebble end the link.
// - Shares are the person's choices in the popup (lib/shares.mjs); a shared tab joins a "Pebble" tab group.
// - Grants come from Pebble (a task granted the tab in Pebble's main window). Page commands run only for a tab with a
//   live grant; the grant's expiry is enforced here and detaches the debugger at once.
// - The debugger attaches only when Pebble looks at a granted tab, lets go after IDLE_MS, when the tab leaves its
//   site, on a sign-in or payment page and when the grant ends. The page indicator and the page facts run through
//   that same debugger in Pebble's own isolated world, so no `scripting` or host permission is needed.
// - lib/policy.mjs decides which of Pebble's page commands pass.
//
// Kill switch: Stop in the popup, the page indicator's Stop, Cancel on Chrome's debugging bar, Pebble's own Stop and
// Pebble quitting all detach every debugger. The first three also clear every share and tell Pebble.

import { Bucket, checkCommand, EFFECTS, HOST_NAME, newTabState, noteResult, PROTOCOL, replyFrames, sanitizeEvent,
  canonicalOrigin, shareable, shapeResult, MAX_FRAME } from './policy.mjs';
import { emptyShares, forBackend, navigate, restore, retitle, setHandback, shareOf, shareTab, stopAll as clearShares,
  unshareTab } from './shares.mjs';
import { credentialUrl, handbackReasons } from './credential.mjs';
import { pebblePage } from './page.mjs';
import { confirmTag, hex, pairProof, randomHex, sameHex, seal, serviceProof, sessionKey, unhex, unseal,
  unwrapKey } from './crypto.mjs';

export const IDLE_MS = 60_000;
export const ACTING_MS = 4_000;
export const RETRY_MS = Object.freeze([2_000, 5_000, 15_000, 30_000, 60_000]);
export const MAX_RETRIES = 12;
export const COMMAND_RATE = Object.freeze([400, 200]);  // bucket size, refill per second, per tab
export const INDICATOR_WORLD = 'pebble-indicator';
export const BINDING = 'pebbleSignal';
const PROTOCOL_VERSION = '1.3';
const TAB = /^[0-9]{1,12}$/;
const HEX64 = /^[0-9a-f]{64}$/;
const HEX32 = /^[0-9a-f]{32}$/;
const CODE = /^[0-9]{6}$/;
const NO_GROUP = -1;
const CONNECTIONS = ['idle', 'connecting', 'connected', 'unpaired', 'app_not_running', 'refused', 'not_installed', 'forbidden',
  'disconnected', 'mismatch'];

const fail = (code, message) => ({ ok: false, error: { code, message } });

export function createWorker(chrome, options = {}) {
  const now = options.now || (() => Date.now());
  const setTimer = options.setTimer || ((fn, ms) => setTimeout(fn, ms));
  const clearTimer = options.clearTimer || (id => clearTimeout(id));
  const randomId = options.randomId || (() => randomHex(16));
  const nonce = options.nonce || (() => randomHex(32));
  const draw = options.draw || null;
  const S = { shares: emptyShares(), link: null, connection: 'idle', paused: false, tabs: new Map(), acting: new Set(),
    retry: 0, retryTimer: null, popups: new Set(), lastStop: null, ready: null, message: '', pairing: null, pairState: null,
    grants: new Map(), grantTimers: new Map(), groups: new Map(), grouped: new Set() };

  // ---- small helpers ----

  const chromeVersion = () => (/Chrome\/([0-9.]+)/.exec(globalThis.navigator?.userAgent || '') || [])[1] || null;
  const own = (tabId, method, params = {}) => chrome.debugger.sendCommand({ tabId }, method, params);

  function tabState(tabId) {
    let tab = S.tabs.get(tabId);
    if (!tab) {
      tab = { attached: false, attaching: null, policy: newTabState(), bucket: new Bucket(COMMAND_RATE[0], COMMAND_RATE[1], now()),
        idle: null, actingTimer: null, indicator: null, detaching: null, world: null };
      S.tabs.set(tabId, tab);
    }
    return tab;
  }

  async function targetOf(tabId) {
    try { return (await chrome.debugger.getTargets()).find(target => target.tabId === tabId) || null; } catch { return null; }
  }

  async function persist() {
    try { await chrome.storage.session.set({ shares: S.shares }); } catch { /* storage is best effort; state lives here */ }
  }

  // ---- the link to Pebble: plain only for the hello and pairing, sealed for everything else ----

  function post(message) {
    const link = S.link;
    if (!link || !link.session || S.connection !== 'connected') return false;
    link.outbox = link.outbox.then(async () => {
      link.sendSeq += 1;
      const frame = await seal(link.session, 'e2s', link.sendSeq, message);
      if (!link.ended) link.port.postMessage(frame);
    }).catch(() => {});
    return true;
  }

  function postPlain(message) {
    const link = S.link;
    if (!link) return false;
    try { link.port.postMessage(message); return true; } catch { return false; }
  }

  function sendShares() {
    post({ type: 'shares', shares: forBackend(S.shares, S.acting) });
  }

  // ---- grants (from Pebble; enforced here too) ----

  function grantLive(tabId) {
    const grants = S.grants.get(tabId);
    if (!grants) return false;
    const at = now();
    for (const until of grants.values()) if (until > at) return true;
    return false;
  }

  function scheduleGrant(tabId) {
    clearTimer(S.grantTimers.get(tabId));
    S.grantTimers.delete(tabId);
    const grants = S.grants.get(tabId);
    if (!grants || !grants.size) return;
    const next = Math.min(...grants.values());
    S.grantTimers.set(tabId, setTimer(() => { void expire(tabId); }, Math.max(0, next - now())));
  }

  // A grant ended (expiry or Pebble's End access): with none left, the debugger lets go at once.
  async function expire(tabId) {
    const grants = S.grants.get(tabId), at = now();
    if (grants) for (const [session, until] of grants) if (until <= at) grants.delete(session);
    if (grants && grants.size) { scheduleGrant(tabId); return; }
    S.grants.delete(tabId);
    clearTimer(S.grantTimers.get(tabId));
    S.grantTimers.delete(tabId);
    await detach(tabId, 'expired');
    void updateBadge(tabId);
  }

  function grant(message) {
    const tabId = Number(message.tab_id), seconds = message.expires_in;
    if (!TAB.test(String(message.tab_id)) || typeof message.session_id !== 'string' || !HEX32.test(message.session_id)
      || !Number.isInteger(seconds) || seconds < 1 || seconds > 1800 || !shareOf(S.shares, tabId)) return;
    const grants = S.grants.get(tabId) || new Map();
    grants.set(message.session_id, now() + seconds * 1000);
    S.grants.set(tabId, grants);
    scheduleGrant(tabId);
    void updateBadge(tabId);
  }

  function ungrant(message) {
    const tabId = Number(message.tab_id);
    S.grants.get(tabId)?.delete(message.session_id);
    void expire(tabId);
  }

  // The link to Pebble ended: its grants end with it (Pebble forgets them too and grants again on a new link).
  function dropGrants() {
    for (const timer of S.grantTimers.values()) clearTimer(timer);
    const tabs = [...S.grants.keys()];
    S.grants.clear(); S.grantTimers.clear();
    for (const tabId of tabs) void updateBadge(tabId);
  }

  // ---- the toolbar badge, the tab group and the page indicator ----

  async function updateBadge(tabId) {
    const share = shareOf(S.shares, tabId);
    const live = grantLive(tabId);
    const text = !share ? '' : share.state === 'handback' ? '!' : live ? 'ON' : '•';
    try {
      await chrome.action.setBadgeText({ tabId, text });
      if (text) await chrome.action.setBadgeBackgroundColor({ tabId, color: share.state === 'handback' ? '#ad5a4a' : '#506e54' });
      await chrome.action.setTitle({ tabId, title: !share ? 'Pebble for Chrome'
        : S.acting.has(tabId) ? 'Pebble is working in this tab' : share.state === 'handback' ? 'Your turn: Pebble paused on this page'
          : share.state === 'away' ? 'Shared with Pebble (paused on this site)' : live ? 'Pebble can use this tab for a task' : 'Shared with Pebble' });
    } catch { /* the tab closed */ }
  }

  // A shared tab joins the "Pebble" tab group of its window, unless the person keeps it in a group of their own.
  async function joinGroup(tabId) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (tab.groupId !== undefined && tab.groupId !== NO_GROUP) return;
      let groupId = S.groups.get(tab.windowId);
      try { groupId = await chrome.tabs.group(groupId !== undefined ? { tabIds: tabId, groupId } : { tabIds: tabId, createProperties: { windowId: tab.windowId } }); }
      catch { groupId = await chrome.tabs.group({ tabIds: tabId, createProperties: { windowId: tab.windowId } }); }
      S.groups.set(tab.windowId, groupId);
      S.grouped.add(tabId);
      await chrome.tabGroups.update(groupId, { title: 'Pebble', color: 'green' });
    } catch { /* grouping is a courtesy; the badge and Chrome's debugging bar still show */ }
  }

  async function leaveGroup(tabId) {
    if (!S.grouped.delete(tabId)) return;
    try { await chrome.tabs.ungroup(tabId); } catch { /* closed */ }
  }

  function indicatorState(tabId) {
    const share = shareOf(S.shares, tabId);
    if (!share || share.state === 'away') return 'off';
    if (share.state === 'handback') return 'handback';
    if (S.paused) return 'paused';
    return S.acting.has(tabId) ? 'acting' : 'shared';
  }

  async function evaluatePage(tabId, command) {
    const tab = S.tabs.get(tabId);
    const result = await own(tabId, 'Runtime.evaluate', { expression: `(${pebblePage.toString()})(${JSON.stringify(command)})`,
      contextId: tab.world.contextId, returnByValue: true });
    return result?.result?.value;
  }

  // Show the indicator's current state while the debugger is attached; true when it is showing (or correctly off).
  async function showIndicator(tabId) {
    const tab = S.tabs.get(tabId);
    if (!tab || !tab.attached || !tab.world) return false;
    const wanted = indicatorState(tabId);
    if (tab.indicator === wanted) return true;
    try {
      const result = await evaluatePage(tabId, { action: 'indicator', state: wanted, label: 'Pebble for Chrome' });
      tab.indicator = result?.state === wanted ? wanted : null;
    } catch {
      tab.indicator = null;
    }
    void updateBadge(tabId);
    return tab.indicator === wanted;
  }

  // Pebble's own isolated world in the current document (made once per document), then the page facts and the indicator.
  async function refreshPage(tabId) {
    const tab = S.tabs.get(tabId);
    if (!tab || !tab.attached) return;
    try {
      const frame = (await own(tabId, 'Page.getFrameTree'))?.frameTree?.frame;
      if (!frame?.id) return;
      if (!tab.world || tab.world.loaderId !== frame.loaderId) {
        const created = await own(tabId, 'Page.createIsolatedWorld', { frameId: frame.id, worldName: INDICATOR_WORLD,
          grantUniversalAccess: false });
        tab.world = { loaderId: frame.loaderId, contextId: created?.executionContextId };
        tab.indicator = null;
      }
      const facts = (await evaluatePage(tabId, { action: 'facts' }))?.facts || null;
      applyFacts(tabId, (await targetOf(tabId))?.url || frame.url || '', facts);
      await showIndicator(tabId);
    } catch { tab.indicator = null; }
  }

  function applyFacts(tabId, url, facts) {
    const result = setHandback(S.shares, tabId, handbackReasons(url, facts));
    if (!result.changed) return false;
    S.shares = result.state;
    if (shareOf(S.shares, tabId)?.state === 'handback') void detach(tabId, 'away');  // no debugger on a sign-in page
    void updateBadge(tabId);
    void persist();
    sendShares();
    return true;
  }

  // ---- the debugger ----

  async function attach(tabId) {
    const tab = tabState(tabId);
    if (tab.attached) return;
    if (tab.attaching) return tab.attaching;
    tab.attaching = (async () => {
      await chrome.debugger.attach({ tabId }, PROTOCOL_VERSION);
      tab.attached = true;
      tab.policy = newTabState();
      tab.world = null;
      tab.indicator = null;
      await own(tabId, 'Page.enable');
      await own(tabId, 'Runtime.enable');
      await own(tabId, 'Runtime.addBinding', { name: BINDING, executionContextName: INDICATOR_WORLD });
      touch(tabId);
      await refreshPage(tabId);
    })();
    try { await tab.attaching; } finally { tab.attaching = null; }
  }

  async function detach(tabId, reason) {
    const tab = S.tabs.get(tabId);
    if (!tab || !tab.attached) return;
    tab.detaching = reason;
    clearTimer(tab.idle);
    if (tab.world) { try { await evaluatePage(tabId, { action: 'indicator', state: 'off' }); } catch { /* gone */ } }
    try { await chrome.debugger.detach({ tabId }); } catch { /* already gone */ }
    if (tab.attached) onDetach({ tabId }, reason);  // Chrome does not always report a detach the extension asked for
  }

  function onDetach(source, chromeReason) {
    const tabId = source?.tabId, tab = S.tabs.get(tabId);
    if (!tab || !tab.attached) return;
    const ours = tab.detaching;
    const reason = ours || chromeReason || 'detached';
    tab.attached = false; tab.detaching = null; tab.policy = newTabState(); tab.world = null; tab.indicator = null;
    clearTimer(tab.idle); clearTimer(tab.actingTimer);
    S.acting.delete(tabId);
    post({ type: 'detached', tab_id: String(tabId), reason });
    void updateBadge(tabId);
    if (!ours && chromeReason === 'canceled_by_user') void stopEverything('infobar');  // Cancel on Chrome's debugging bar
  }

  // Any command from Pebble: the tab shows "working" for ACTING_MS, and the debugger lets go after IDLE_MS.
  function touch(tabId) {
    const tab = tabState(tabId);
    clearTimer(tab.idle);
    tab.idle = setTimer(() => { void detach(tabId, 'idle'); }, IDLE_MS);
    clearTimer(tab.actingTimer);
    const was = S.acting.has(tabId);
    S.acting.add(tabId);
    tab.actingTimer = setTimer(() => {
      S.acting.delete(tabId);
      void showIndicator(tabId);
      sendShares();
    }, ACTING_MS);
    if (!was) { void showIndicator(tabId); sendShares(); }
  }

  function onEvent(source, method, params) {
    const tabId = source?.tabId, tab = S.tabs.get(tabId);
    if (!tab || !tab.attached) return;
    if (method === 'Runtime.bindingCalled') {  // the indicator's Stop, a tampered indicator or new page facts
      if (params?.name === BINDING && tab.world && params.executionContextId === tab.world.contextId) void signal(tabId, params.payload);
      return;
    }
    if (method === 'Page.frameNavigated' && !params?.frame?.parentId) tab.policy = newTabState();
    if (method === 'Page.loadEventFired') void refreshPage(tabId);
    const clean = sanitizeEvent(method, params);
    if (!clean) return;
    const message = { type: 'event', tab_id: String(tabId), method, params: clean };
    if (new TextEncoder().encode(JSON.stringify(message)).length > MAX_FRAME / 2) { void detach(tabId, 'detached'); return; }
    post(message);
  }

  async function signal(tabId, payload) {
    let message = null;
    try { message = JSON.parse(payload); } catch { return; }
    if (message?.type === 'stop') await stopEverything('page');
    else if (message?.type === 'tampered') await unshare(tabId);
    else if (message?.type === 'facts') applyFacts(tabId, (await targetOf(tabId))?.url || '', message.facts);
  }

  // ---- calls from Pebble ----

  async function call(message) {
    const { op } = message;
    const tabId = TAB.test(String(message.tab_id ?? '')) ? Number(message.tab_id) : null;
    if (tabId === null) return fail('invalid', 'That is not a tab.');
    const share = shareOf(S.shares, tabId);
    if (op === 'detach') { await detach(tabId, 'detached'); return { ok: true, result: {} }; }
    if (!share) return fail('not_shared', 'This tab is not shared with Pebble.');
    if (op === 'cdp') return command(tabId, share, message.method, message.params);
    if (!grantLive(tabId)) return fail('not_granted', 'No task in Pebble holds a live grant for this tab.');
    if (op === 'tab_info') {
      const target = await targetOf(tabId);
      if (!target) return fail('detached', 'The tab is gone.');
      const same = canonicalOrigin(target.url) === share.origin;
      return { ok: true, result: { title: same ? String(target.title || '').slice(0, 1024) : '', url: same ? target.url : '' } };
    }
    if (share.state === 'away') return fail('away', 'The tab is on a site that was not shared.');
    if (share.state === 'handback') return fail('handback', "This page is the person's turn.");
    if (op === 'attach') {
      if (S.paused) return fail('paused', 'Pebble is paused in Chrome.');
      const tab = tabState(tabId);
      if (!tab.attached && (await targetOf(tabId))?.attached) {
        return fail('refused', 'Another tool is already debugging this tab, so Pebble does not attach to it.');
      }
      try { await attach(tabId); }
      catch (error) { return fail('cdp_error', String(error?.message || 'The debugger could not attach.').slice(0, 200)); }
      const after = shareOf(S.shares, tabId);
      if (after?.state === 'handback') return fail('handback', "This page is the person's turn.");
      return { ok: true, result: {} };
    }
    if (op === 'activate') {
      try {
        const tab = await chrome.tabs.update(tabId, { active: true });
        if (tab?.windowId !== undefined) await chrome.windows.update(tab.windowId, { focused: true });
        return { ok: true, result: {} };
      } catch { return fail('detached', 'The tab is gone.'); }
    }
    if (op === 'navigate') {
      if (S.paused) return fail('paused', 'Pebble is paused in Chrome.');
      if (!shareable(message.url) || canonicalOrigin(message.url) !== share.origin || credentialUrl(message.url)) {
        return fail('refused', 'Pebble only goes to pages on the shared site, and never to a sign-in or payment page.');
      }
      try { await chrome.tabs.update(tabId, { url: message.url }); return { ok: true, result: {} }; }
      catch { return fail('detached', 'The tab is gone.'); }
    }
    return fail('unsupported', 'Pebble for Chrome does not know that request.');
  }

  async function command(tabId, share, method, params) {
    const tab = tabState(tabId);
    const checked = checkCommand(share, tab.policy, method, params, { paused: S.paused, granted: grantLive(tabId) });
    if (!checked.ok) return fail(checked.code, checked.message);
    if (!tab.attached) return fail('detached', 'Pebble is not attached to this tab.');
    if (!tab.bucket.take(now())) return fail('rate_limited', 'Too many commands; slow down.');
    touch(tabId);
    if (EFFECTS.has(method) && !(await showIndicator(tabId))) {
      return fail('refused', 'Pebble cannot show its indicator on this page, so it does not act here.');
    }
    try {
      const result = await own(tabId, method, params ?? {});
      noteResult(tab.policy, method, result);
      return { ok: true, result: shapeResult(method, result, share) };
    } catch (error) {
      return fail('cdp_error', String(error?.message || 'The command failed.').slice(0, 200));
    }
  }

  function onPebbleMessage(message) {
    if (message.type === 'call' && Number.isInteger(message.id)) {
      void call(message).catch(() => fail('refused', 'The request failed.')).then(reply => {
        for (const frame of replyFrames(message.id, reply)) post(frame);
      });
    } else if (message.type === 'send' && message.op === 'cdp' && TAB.test(String(message.tab_id ?? ''))) {
      const tabId = Number(message.tab_id), share = shareOf(S.shares, tabId);
      if (share) void command(tabId, share, message.method, message.params);
    } else if (message.type === 'control') {
      if (message.action === 'stop_all') void stopEverything('pebble', { tell: false });
      else if (message.action === 'pause') setPaused(true);
      else if (message.action === 'resume') setPaused(false);
      else if (message.action === 'grant') grant(message);
      else if (message.action === 'ungrant') ungrant(message);
      else if (message.action === 'bye') { S.connection = 'disconnected'; dropGrants(); void detachAll('pebble_quit'); }
    }
  }

  function setPaused(paused) {
    S.paused = paused;
    for (const tabId of Object.keys(S.shares.shares).map(Number)) void showIndicator(tabId);
  }

  // ---- the native port, the welcome and pairing ----

  function wanted() {
    return Object.keys(S.shares.shares).length > 0 || S.popups.size > 0;
  }

  function connect() {
    if (S.link || !wanted()) return;
    clearTimer(S.retryTimer); S.retryTimer = null;
    let port;
    try { port = chrome.runtime.connectNative(HOST_NAME); } catch { S.connection = 'not_installed'; return; }
    const link = { port, extNonce: nonce(), linkNonce: null, session: null, sendSeq: 0, recvSeq: 0, welcomed: false,
      code: null, ended: false, outbox: Promise.resolve(), inbox: Promise.resolve() };
    S.link = link; S.connection = 'connecting';
    port.onMessage.addListener(frame => {
      if (S.link !== link) return;
      link.inbox = link.inbox.then(() => receive(link, frame)).catch(() => refuse(link, 'A message from Pebble did not verify, so Pebble for Chrome disconnected.'));
    });
    port.onDisconnect.addListener(() => { link.ended = true; if (S.link === link) onDisconnect(); });
    postPlain({ type: 'hello', protocol: PROTOCOL, nonce: link.extNonce, pairing_id: S.pairing?.id ?? null,
      extension: { version: chrome.runtime.getManifest().version, chrome: chromeVersion() } });
  }

  async function receive(link, frame) {
    if (S.link !== link || !frame || typeof frame !== 'object') return;
    if (link.session) {
      link.recvSeq += 1;
      onPebbleMessage(await unseal(link.session, 's2e', link.recvSeq, frame));
      return;
    }
    if (frame.type === 'status' && !link.welcomed) {
      S.connection = frame.state === 'app_not_running' ? 'app_not_running' : 'refused';
      S.message = typeof frame.message === 'string' ? frame.message.slice(0, 200) : '';
    } else if (frame.type === 'welcome' && !link.welcomed) {
      await welcome(link, frame);
    } else if (frame.type === 'pair_challenge' && S.connection === 'unpaired') {
      const seconds = Number.isInteger(frame.expires_in) ? frame.expires_in : 0;
      S.pairState = { expiresAt: now() + seconds * 1000, attemptsLeft: Number.isInteger(frame.attempts) ? frame.attempts : 0 };
      S.message = 'Type the 6-digit code Pebble shows in its Browser panel.';
    } else if (frame.type === 'pair_result' && S.connection === 'unpaired' && (link.code || frame.reason === 'locked')) {
      await paired(link, frame);
    }  // anything else unsealed is dropped
  }

  async function welcome(link, frame) {
    if (frame.protocol !== PROTOCOL || !HEX64.test(String(frame.link_nonce ?? ''))) throw new Error('welcome');
    link.welcomed = true;
    link.linkNonce = frame.link_nonce;
    S.paused = frame.paused === true;
    if (frame.paired === true) {
      const pairing = S.pairing;
      if (!pairing || frame.pairing_id !== pairing.id
        || !sameHex(frame.proof, await serviceProof(pairing.key, link.extNonce, link.linkNonce))) {
        refuse(link, 'Pebble did not prove this pairing. Pair again from Pebble.', 'mismatch');
        return;
      }
      link.session = await sessionKey(pairing.key, link.extNonce, link.linkNonce);
      S.connection = 'connected'; S.retry = 0; S.message = '';
      sendShares();
      return;
    }
    S.connection = 'unpaired';
    S.message = S.pairing ? "Pebble doesn't recognise this browser's pairing. Pair again." : '';
  }

  async function paired(link, frame) {
    if (frame.ok !== true && frame.reason === 'locked') {
      const minutes = Math.max(1, Math.ceil((Number.isInteger(frame.retry_in) ? frame.retry_in : 0) / 60));
      link.code = null;
      S.pairState = null;
      S.message = `Too many wrong codes, so pairing is paused for about ${minutes} min. Pebble's Browser panel can allow it again sooner.`;
      return;
    }
    if (frame.ok !== true) {
      const left = Number.isInteger(frame.attempts_left) ? frame.attempts_left : 0;
      S.pairState = left > 0 && S.pairState ? { ...S.pairState, attemptsLeft: left } : null;
      S.message = frame.reason === 'expired' || left === 0 ? 'The code expired or ran out of tries. Start pairing again.'
        : `That code didn't match. ${left === 1 ? '1 try' : left + ' tries'} left.`;
      return;
    }
    const key = await unwrapKey(String(frame.wrapped ?? ''), link.code, link.extNonce, link.linkNonce);
    if (!HEX32.test(String(frame.pairing_id ?? '')) || !sameHex(frame.confirm, await confirmTag(key, link.extNonce, link.linkNonce))) {
      throw new Error('pairing');
    }
    S.pairing = { id: frame.pairing_id, key };
    try { await chrome.storage.local.set({ pairing: { id: frame.pairing_id, key: hex(key) } }); } catch { /* kept in memory */ }
    link.session = await sessionKey(key, link.extNonce, link.linkNonce);
    link.code = null;
    S.pairState = null;
    S.connection = 'connected'; S.retry = 0; S.message = 'Paired with Pebble.';
    sendShares();
  }

  async function pairStart() {
    if (S.connection !== 'unpaired' || !S.link?.linkNonce) return { ok: false, error: 'not_ready' };
    postPlain({ type: 'pair_request' });
    return { ok: true };
  }

  async function pairCode(code) {
    const link = S.link;
    if (typeof code !== 'string' || !CODE.test(code)) return { ok: false, error: 'code' };
    if (!link || S.connection !== 'unpaired' || !S.pairState || S.pairState.expiresAt <= now()) return { ok: false, error: 'not_ready' };
    link.code = code;
    postPlain({ type: 'pair_proof', proof: await pairProof(code, link.extNonce, link.linkNonce) });
    return { ok: true };
  }

  async function unpair() {
    S.pairing = null;
    S.pairState = null;
    try { await chrome.storage.local.remove('pairing'); } catch { /* gone */ }
    closePort('idle');
    S.retry = 0;
    connect();
    return { ok: true };
  }

  function refuse(link, message, state = 'refused') {
    if (S.link !== link) return;
    closePort(state);
    S.message = message;
  }

  function closePort(state) {
    const link = S.link;
    S.link = null; S.connection = state; S.pairState = null;
    // What was already sealed for Pebble (the last shares, a stop) leaves before the port closes.
    if (link) link.outbox = link.outbox.then(() => { link.ended = true; try { link.port.disconnect(); } catch { /* gone */ } });
    dropGrants();
    void detachAll('pebble_gone');
  }

  function onDisconnect() {
    const error = String(chrome.runtime.lastError?.message || '');
    S.link = null; S.pairState = null;
    if (/not found/i.test(error)) S.connection = 'not_installed';
    else if (/forbidden/i.test(error)) S.connection = 'forbidden';
    else if (!['app_not_running', 'refused', 'mismatch'].includes(S.connection)) S.connection = 'disconnected';
    dropGrants();
    void detachAll('pebble_gone');
    // Retry for about ten minutes while a tab is shared; after that, opening the popup tries again.
    if (wanted() && !['not_installed', 'forbidden', 'mismatch'].includes(S.connection) && S.retry < MAX_RETRIES) {
      const delay = RETRY_MS[Math.min(S.retry, RETRY_MS.length - 1)];
      S.retry++;
      S.retryTimer = setTimer(() => { S.retryTimer = null; connect(); }, delay);
    }
  }

  function disconnectIfUnused() {
    if (wanted() || !S.link) return;
    closePort('idle');
  }

  async function detachAll(reason) {
    await Promise.all([...S.tabs.keys()].map(tabId => detach(tabId, reason)));
    for (const tabId of Object.keys(S.shares.shares).map(Number)) void updateBadge(tabId);
  }

  // ---- the person's controls ----

  async function stopEverything(reason, { tell = true } = {}) {
    const { state, removed } = clearShares(S.shares);
    S.shares = state;
    S.lastStop = { reason, at: now() };
    for (const tabId of [...S.grantTimers.keys()]) clearTimer(S.grantTimers.get(tabId));
    S.grants.clear(); S.grantTimers.clear();
    await Promise.all([...S.tabs.keys()].map(tabId => detach(tabId, 'stopped')));
    for (const share of removed) {
      S.acting.delete(share.tabId);
      await leaveGroup(share.tabId);
      void updateBadge(share.tabId);
    }
    await persist();
    if (tell) post({ type: 'stop', reason });
    sendShares();
    disconnectIfUnused();
    return { stopped: removed.length };
  }

  async function share(tabId, scope) {
    const target = await targetOf(tabId);
    let url = target?.url, title = target?.title;
    if (!url) { try { ({ url, title } = await chrome.tabs.get(tabId)); } catch { return { ok: false, error: 'gone' }; } }
    if (!url || !shareable(url)) return { ok: false, error: 'unshareable' };
    const result = shareTab(S.shares, { tabId, url, title, scope, now: now(), id: randomId() });
    if (result.error) return { ok: false, error: result.error };
    await detach(tabId, 'detached');  // a share again is a new share: no earlier attachment or grant carries over
    S.grants.delete(tabId);
    S.shares = result.state;
    if (credentialUrl(url)) applyFacts(tabId, url, null);
    await joinGroup(tabId);
    void updateBadge(tabId);
    await persist();
    connect();
    sendShares();
    return { ok: true };
  }

  async function unshare(tabId) {
    const { state, removed } = unshareTab(S.shares, tabId);
    if (!removed) return { ok: true };
    S.shares = state;
    S.acting.delete(tabId);
    S.grants.delete(tabId);
    clearTimer(S.grantTimers.get(tabId));
    S.grantTimers.delete(tabId);
    await detach(tabId, 'stopped');
    await leaveGroup(tabId);
    void updateBadge(tabId);
    await persist();
    sendShares();
    disconnectIfUnused();
    return { ok: true };
  }

  // ---- tabs ----

  async function onUpdated(tabId, change, info) {
    await S.ready;
    if (!shareOf(S.shares, tabId)) return;
    if (change.url !== undefined || change.status === 'loading') {
      const url = (await targetOf(tabId))?.url ?? info?.url;
      const result = navigate(S.shares, tabId, url || '', info?.title);
      if (result.change === 'none') return;
      S.shares = result.state;
      if (result.change === 'cleared' || result.change === 'away') {
        // An address Chrome hides from this extension counts as another site (it fails closed).
        await detach(tabId, result.change === 'cleared' ? 'stopped' : 'away');
        if (result.change === 'cleared') {
          S.grants.delete(tabId);
          await leaveGroup(tabId);
        }
      } else if (credentialUrl(url)) {
        applyFacts(tabId, url, null);
      }
      void updateBadge(tabId);
      await persist();
      sendShares();
      if (result.change === 'cleared') disconnectIfUnused();
    } else if (typeof change.title === 'string') {
      S.shares = retitle(S.shares, tabId, change.title);
      sendShares();
    }
  }

  async function onRemoved(tabId) {
    await S.ready;
    const tab = S.tabs.get(tabId);
    if (tab) { clearTimer(tab.idle); clearTimer(tab.actingTimer); if (tab.attached) onDetach({ tabId }, 'target_closed'); }
    S.tabs.delete(tabId);
    S.acting.delete(tabId);
    S.grouped.delete(tabId);
    S.grants.delete(tabId);
    clearTimer(S.grantTimers.get(tabId));
    S.grantTimers.delete(tabId);
    const { state, removed } = unshareTab(S.shares, tabId);
    if (!removed) return;
    S.shares = state;
    await persist();
    sendShares();
    disconnectIfUnused();
  }

  function onCreated(info) {
    const opener = info?.openerTabId;
    if (Number.isInteger(opener) && S.tabs.get(opener)?.attached) {
      post({ type: 'event', tab_id: String(opener), method: 'Pebble.popupOpened', params: {} });
    }
  }

  // ---- messages from the popup ----

  function popupSender(sender) {
    return sender?.id === chrome.runtime.id && !sender.tab && typeof sender.url === 'string'
      && sender.url.startsWith(chrome.runtime.getURL('popup.html'));
  }

  async function view(tabId) {
    let current = null;
    if (Number.isInteger(tabId)) {
      let tab = null;
      try { tab = await chrome.tabs.get(tabId); } catch { tab = null; }
      const url = (await targetOf(tabId))?.url ?? tab?.url;
      if (tab || url) {
        const share = shareOf(S.shares, tabId);
        current = { tabId, origin: canonicalOrigin(url), title: String(tab?.title || '').slice(0, 200), shareable: shareable(url),
          credential: credentialUrl(url),
          share: share ? { scope: share.scope, state: share.state, reasons: share.reasons, acting: S.acting.has(tabId), granted: grantLive(tabId) } : null };
      }
    }
    return { connection: CONNECTIONS.includes(S.connection) ? S.connection : 'idle', message: S.message, paused: S.paused, current,
      pairing: { paired: !!S.pairing, pending: S.pairState && S.pairState.expiresAt > now()
        ? { attemptsLeft: S.pairState.attemptsLeft, expiresIn: Math.round((S.pairState.expiresAt - now()) / 1000) } : null },
      shares: Object.values(S.shares.shares).map(item => ({ tabId: item.tabId, origin: item.origin, title: item.state === 'ready' ? item.title : '',
        scope: item.scope, state: item.state, acting: S.acting.has(item.tabId), granted: grantLive(item.tabId) })),
      lastStop: S.lastStop };
  }

  async function onMessage(message, sender) {
    await S.ready;
    if (!message || typeof message !== 'object' || typeof message.type !== 'string' || !message.type.startsWith('popup:')) return null;
    if (!popupSender(sender)) return null;  // pages, frames and other extensions never reach these
    if (message.type === 'popup:state') return view(message.tabId);
    if (message.type === 'popup:share' && Number.isInteger(message.tabId) && (message.scope === 'tab' || message.scope === 'site')) {
      const result = await share(message.tabId, message.scope);
      return { ...result, view: await view(message.tabId) };
    }
    if (message.type === 'popup:unshare' && Number.isInteger(message.tabId)) {
      await unshare(message.tabId);
      return { ok: true, view: await view(message.tabId) };
    }
    if (message.type === 'popup:stop') {
      const result = await stopEverything('popup');
      return { ok: true, ...result, view: await view(message.tabId) };
    }
    if (message.type === 'popup:connect') { S.retry = 0; connect(); return view(message.tabId); }
    if (message.type === 'popup:pair_start') return { ...(await pairStart()), view: await view(message.tabId) };
    if (message.type === 'popup:pair_code') return { ...(await pairCode(message.code)), view: await view(message.tabId) };
    if (message.type === 'popup:unpair') return { ...(await unpair()), view: await view(message.tabId) };
    return null;
  }

  function onPopupConnect(port) {
    if (port?.name !== 'popup' || !popupSender(port.sender)) return;
    S.popups.add(port);
    connect();
    port.onDisconnect.addListener(() => { S.popups.delete(port); disconnectIfUnused(); });
  }

  // ---- start ----

  async function restoreState() {
    let saved = null, pairing = null;
    try { saved = (await chrome.storage.session.get('shares'))?.shares; } catch { saved = null; }
    try { pairing = (await chrome.storage.local.get('pairing'))?.pairing; } catch { pairing = null; }
    if (pairing && HEX32.test(String(pairing.id ?? '')) && HEX64.test(String(pairing.key ?? ''))) {
      S.pairing = { id: pairing.id, key: unhex(pairing.key) };
    }
    S.shares = restore(saved);
    for (const item of Object.values(S.shares.shares)) {
      const target = await targetOf(item.tabId);
      if (!target || (item.state !== 'away' && canonicalOrigin(target.url) !== item.origin)) S.shares = unshareTab(S.shares, item.tabId).state;
    }
    await persist();
    if (draw) { try { await chrome.action.setIcon({ imageData: draw() }); } catch { /* the default icon stays */ } }
    connect();
  }

  function start() {
    // Listeners are registered synchronously, as Manifest V3 requires; they wait for the restored state.
    S.ready = restoreState();
    chrome.runtime.onMessage.addListener((message, sender, respond) => {
      onMessage(message, sender).then(respond, () => respond(null));
      return true;
    });
    chrome.runtime.onConnect.addListener(onPopupConnect);
    chrome.tabs.onUpdated.addListener((tabId, change, info) => { void onUpdated(tabId, change, info); });
    chrome.tabs.onRemoved.addListener(tabId => { void onRemoved(tabId); });
    chrome.tabs.onCreated.addListener(onCreated);
    chrome.debugger.onEvent.addListener(onEvent);
    chrome.debugger.onDetach.addListener(onDetach);
    return S.ready;
  }

  return { start, state: S, call, onMessage, stopEverything, share, unshare, view, grantLive };
}
