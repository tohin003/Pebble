// Pebble's own tabs in the person's browser (Phase 43, 1.9.0, decision 3). Pure state transitions; lib/worker.mjs
// applies the effects.
//
// A site grant is one task's, sent by Pebble after the person approved it in Pebble's main window: the sites Pebble
// may act on, the sites it may only read (every act site is also a read site) and an expiry. Each site is an exact
// origin: scheme, host and port. Under a live grant Pebble may open tabs at its read sites (inside its own tab group),
// move them between read sites and close them. A tab is Pebble's only when Pebble created it in this browser session:
// a tab the person moves into the group never is, and a tab ID from before a browser restart is never trusted
// (chrome.storage.session starts empty then, and the incarnation changes). Reading needs the tab on a read site, acting
// an act site; a sign-in, password, payment or verification page is the person's turn, as for shared tabs. Grants end
// with the link to Pebble, at their expiry and on Stop; their tabs stay open but are no longer Pebble's.

import { canonicalOrigin, shareable } from './policy.mjs';
import { credentialUrl } from './credential.mjs';

export const MAX_GRANTS = 8;
export const MAX_ORIGINS = 16;        // act and read sites together, per grant
export const MAX_TABS = 16;           // Pebble's tabs in this browser, across every grant
export const MAX_TABS_PER_GRANT = 8;
export const MAX_SECONDS = 1800;
const HEX32 = /^[0-9a-f]{32}$/;
const STATES = ['ready', 'away', 'handback'];

const clip = (value, limit) => (typeof value === 'string' ? value.slice(0, limit) : '');
const copy = state => ({ incarnation: state.incarnation, grants: { ...state.grants }, tabs: { ...state.tabs } });
const exact = origin => typeof origin === 'string' && canonicalOrigin(origin + '/') === origin;

export function emptyOwned(incarnation) {
  return { incarnation, grants: {}, tabs: {} };
}

// {state, grant} or {state, error} for a grant Pebble sent: {grant_id, act: [origins], read: [origins], expires_in}.
export function addGrant(state, message, now) {
  const id = message?.grant_id, seconds = message?.expires_in;
  const act = Array.isArray(message?.act) ? message.act : null, read = Array.isArray(message?.read) ? message.read : null;
  if (typeof id !== 'string' || !HEX32.test(id) || !Number.isInteger(seconds) || seconds < 1 || seconds > MAX_SECONDS
    || !act || !read || ![...act, ...read].every(exact)) return { state, error: 'invalid' };
  const sites = [...new Set([...act, ...read])];
  if (!sites.length || sites.length > MAX_ORIGINS) return { state, error: 'invalid' };
  if (!state.grants[id] && Object.keys(state.grants).length >= MAX_GRANTS) return { state, error: 'limit' };
  const next = copy(state);
  next.grants[id] = { grantId: id, until: now + seconds * 1000, act: [...new Set(act)], read: sites };
  return { state: next, grant: next.grants[id] };
}

// Pebble added one read site to a live grant (its own provenance rule decided; an act site never arrives this way).
export function addRead(state, grantId, origin) {
  const grant = state.grants[grantId];
  if (!grant || !exact(origin)) return { state, error: 'invalid' };
  if (grant.read.includes(origin)) return { state };
  if (grant.read.length >= MAX_ORIGINS) return { state, error: 'limit' };
  const next = copy(state);
  next.grants[grantId] = { ...grant, read: [...grant.read, origin] };
  return { state: next };
}

// The grant ended (expiry, Pebble's end, a dropped link or Stop): its tabs are no longer Pebble's. Returns their IDs.
export function removeGrant(state, grantId) {
  if (!state.grants[grantId]) return { state, tabs: [] };
  const next = copy(state);
  delete next.grants[grantId];
  const tabs = Object.values(next.tabs).filter(tab => tab.grantId === grantId).map(tab => tab.tabId);
  for (const tabId of tabs) delete next.tabs[String(tabId)];
  return { state: next, tabs };
}

export function expired(state, now) {
  return Object.values(state.grants).filter(grant => grant.until <= now).map(grant => grant.grantId);
}

export function liveGrant(state, grantId, now) {
  const grant = state.grants[grantId];
  return grant && grant.until > now ? grant : null;
}

// Whether Pebble may open or move a tab of this grant to url: 'ok', or the reason it may not.
export function allowed(state, grantId, url, now) {
  const grant = liveGrant(state, grantId, now);
  if (!grant) return 'not_granted';
  if (!shareable(url)) return 'refused';
  if (!grant.read.includes(canonicalOrigin(url))) return 'off_grant';
  if (credentialUrl(url)) return 'handback';
  return 'ok';
}

// {state, tab} or {state, error}: a tab Pebble just created for this grant at url.
export function addTab(state, { tabId, grantId, url, now }) {
  if (!Number.isInteger(tabId) || tabId < 0 || !state.grants[grantId]) return { state, error: 'invalid' };
  const tabs = Object.values(state.tabs);
  if (!state.tabs[String(tabId)] && (tabs.length >= MAX_TABS
    || tabs.filter(tab => tab.grantId === grantId).length >= MAX_TABS_PER_GRANT)) return { state, error: 'limit' };
  const next = copy(state);
  next.tabs[String(tabId)] = { tabId, grantId, origin: canonicalOrigin(url), state: 'ready', reasons: [], nav: 0,
    url: clip(url, 2048), title: '', since: now };
  return { state: next, tab: next.tabs[String(tabId)] };
}

export function removeTab(state, tabId) {
  const key = String(tabId);
  if (!state.tabs[key]) return { state, removed: null };
  const next = copy(state);
  const removed = next.tabs[key];
  delete next.tabs[key];
  return { state: next, removed };
}

export function tabOf(state, tabId) {
  return state.tabs[String(tabId)] || null;
}

// The tab committed an address. change: 'none' (not Pebble's), 'same', 'back', 'away' (off the read sites, or an
// address Chrome hides: it fails closed) or 'handback' (a sign-in or payment page).
export function landed(state, tabId, url, title) {
  const key = String(tabId), tab = state.tabs[key];
  if (!tab) return { state, change: 'none' };
  const grant = state.grants[tab.grantId];
  const origin = canonicalOrigin(url);
  const next = copy(state);
  if (!grant || !origin || !grant.read.includes(origin)) {
    next.tabs[key] = { ...tab, origin, state: 'away', reasons: [], nav: tab.nav + 1, url: '', title: '' };
    return { state: next, change: tab.state === 'away' ? 'same' : 'away' };
  }
  const handback = credentialUrl(url);
  next.tabs[key] = { ...tab, origin, state: handback ? 'handback' : 'ready', reasons: handback ? ['address'] : [],
    nav: tab.nav + 1, url: clip(url, 2048), title: clip(title, 200) };
  if (handback) return { state: next, change: 'handback' };
  return { state: next, change: tab.state === 'away' ? 'back' : 'same' };
}

// The page's own facts (lib/page.mjs) on a read site: reasons non-empty hands the tab back, empty lets Pebble work.
export function setHandback(state, tabId, reasons) {
  const key = String(tabId), tab = state.tabs[key];
  if (!tab || tab.state === 'away') return { state, changed: false };
  const wanted = reasons.length ? 'handback' : 'ready';
  if (tab.state === wanted && JSON.stringify(tab.reasons) === JSON.stringify(reasons)) return { state, changed: false };
  const next = copy(state);
  next.tabs[key] = { ...tab, state: wanted, reasons: [...reasons] };
  return { state: next, changed: true };
}

export function retitle(state, tabId, title) {
  const key = String(tabId), tab = state.tabs[key];
  if (!tab || tab.state === 'away') return state;
  const next = copy(state);
  next.tabs[key] = { ...tab, title: clip(title, 200) };
  return next;
}

// How the page fence sees one of Pebble's tabs: {origin, state, live, readOnly}, or null when it is not Pebble's.
export function view(state, tabId, now) {
  const tab = tabOf(state, tabId);
  if (!tab) return null;
  const grant = liveGrant(state, tab.grantId, now);
  return { origin: tab.origin, state: tab.state, live: !!grant, readOnly: !grant || !grant.act.includes(tab.origin),
    grantId: tab.grantId };
}

// What Pebble gets (browser_bridge._owned_tab validates it again): titles and addresses only on read sites.
export function forBackend(state, acting = new Set()) {
  return Object.values(state.tabs).map(tab => ({
    tab_id: String(tab.tabId), grant_id: tab.grantId, origin: tab.state === 'away' ? null : tab.origin, state: tab.state,
    nav: tab.nav, title: tab.state === 'ready' ? tab.title : '', url: tab.state === 'away' ? null : tab.url,
    acting: acting.has(tab.tabId),
  }));
}

// Storage round trip (chrome.storage.session): another incarnation, or anything malformed, is dropped, never repaired.
// Grants never survive a worker restart (they end with the link), so restored tabs come back without one.
export function restore(value, incarnation) {
  const out = emptyOwned(incarnation);
  if (!value || typeof value !== 'object' || value.incarnation !== incarnation || !value.tabs || typeof value.tabs !== 'object') return out;
  for (const [key, tab] of Object.entries(value.tabs).slice(0, MAX_TABS)) {
    if (!tab || String(tab.tabId) !== key || typeof tab.grantId !== 'string' || !HEX32.test(tab.grantId)
      || !STATES.includes(tab.state) || !Number.isInteger(tab.nav)) continue;
    out.tabs[key] = { tabId: tab.tabId, grantId: tab.grantId, origin: exact(tab.origin) ? tab.origin : null, state: tab.state,
      reasons: Array.isArray(tab.reasons) ? tab.reasons.filter(r => typeof r === 'string').slice(0, 8) : [], nav: tab.nav,
      url: clip(tab.url, 2048), title: clip(tab.title, 200), since: Number(tab.since) || 0 };
  }
  return out;
}
