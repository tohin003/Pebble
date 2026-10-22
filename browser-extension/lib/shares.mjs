// The tabs the person shares with Pebble (Phase 22). Pure state transitions; lib/worker.mjs applies the effects.
//
// A share is one tab on one site, chosen in the popup:
//   scope 'tab'   "Just this visit": it ends when the tab closes or goes to another site.
//   scope 'site'  "This site in this tab": it survives a visit elsewhere ('away': Pebble sees and does nothing
//                 there) and resumes when the tab comes back to the site. It ends when the tab closes.
// Stop ends every share. A share is 'handback' while its page is a sign-in, password, payment or verification
// page: Pebble neither reads nor acts there until the page changes.

import { canonicalOrigin, shareable } from './policy.mjs';

export const SCOPES = Object.freeze(['tab', 'site']);
export const MAX_SHARES = 16;

export function emptyShares() {
  return { shares: {} };
}

const clip = (value, limit) => (typeof value === 'string' ? value.slice(0, limit) : '');
const copy = state => ({ shares: { ...state.shares } });

// {state, share} or {state, error}. id is a fresh random hex string from the caller.
export function shareTab(state, { tabId, url, title, scope, now, id }) {
  if (!Number.isInteger(tabId) || tabId < 0) return { state, error: 'invalid' };
  if (!SCOPES.includes(scope)) return { state, error: 'invalid' };
  if (!shareable(url)) return { state, error: 'unshareable' };
  const key = String(tabId);
  if (!state.shares[key] && Object.keys(state.shares).length >= MAX_SHARES) return { state, error: 'limit' };
  const share = { tabId, shareId: id, origin: canonicalOrigin(url), scope, state: 'ready', nav: 0, title: clip(title, 200),
    url: clip(url, 2048), since: now, reasons: [] };
  const next = copy(state);
  next.shares[key] = share;
  return { state: next, share };
}

export function unshareTab(state, tabId) {
  const key = String(tabId);
  if (!state.shares[key]) return { state, removed: null };
  const next = copy(state);
  const removed = next.shares[key];
  delete next.shares[key];
  return { state: next, removed };
}

export function closeTab(state, tabId) {
  return unshareTab(state, tabId);
}

export function stopAll(state) {
  return { state: emptyShares(), removed: Object.values(state.shares) };
}

// The tab committed a new address. change: 'none' (not shared), 'same', 'back', 'away' or 'cleared'.
export function navigate(state, tabId, url, title) {
  const key = String(tabId), share = state.shares[key];
  if (!share) return { state, change: 'none' };
  const origin = canonicalOrigin(url);
  const next = copy(state);
  if (origin && origin === share.origin) {
    const back = share.state === 'away';
    next.shares[key] = { ...share, state: 'ready', reasons: [], nav: share.nav + 1, url: clip(url, 2048), title: clip(title, 200) };
    return { state: next, change: back ? 'back' : 'same' };
  }
  if (share.scope === 'tab') {
    delete next.shares[key];
    return { state: next, change: 'cleared', removed: share };
  }
  next.shares[key] = { ...share, state: 'away', reasons: [], nav: share.nav + 1, url: '', title: '' };
  return { state: next, change: 'away' };
}

export function retitle(state, tabId, title) {
  const key = String(tabId), share = state.shares[key];
  if (!share || share.state === 'away') return state;
  const next = copy(state);
  next.shares[key] = { ...share, title: clip(title, 200) };
  return next;
}

// Hand the tab back to the person (reasons non-empty) or let Pebble work again (reasons empty), on the shared site.
export function setHandback(state, tabId, reasons) {
  const key = String(tabId), share = state.shares[key];
  if (!share || share.state === 'away') return { state, changed: false };
  const wanted = reasons.length ? 'handback' : 'ready';
  const same = share.state === wanted && JSON.stringify(share.reasons) === JSON.stringify(reasons);
  if (same) return { state, changed: false };
  const next = copy(state);
  next.shares[key] = { ...share, state: wanted, reasons: [...reasons] };
  return { state: next, changed: true };
}

// What the backend gets (browser_bridge._share validates it again). acting is the indicator's own state.
export function forBackend(state, acting = new Set()) {
  return Object.values(state.shares).map(share => ({
    tab_id: String(share.tabId), share_id: share.shareId, origin: share.origin, scope: share.scope, state: share.state,
    nav: share.nav, title: share.state === 'ready' ? share.title : '', url: share.state === 'away' ? null : share.url,
    acting: acting.has(share.tabId),
  }));
}

export function shareOf(state, tabId) {
  return state.shares[String(tabId)] || null;
}

// Storage round trip (chrome.storage.session): anything malformed is dropped, never repaired.
export function restore(value) {
  const out = emptyShares();
  const shares = value && typeof value === 'object' && value.shares && typeof value.shares === 'object' ? value.shares : {};
  for (const [key, share] of Object.entries(shares).slice(0, MAX_SHARES)) {
    if (!share || String(share.tabId) !== key || !SCOPES.includes(share.scope) || typeof share.shareId !== 'string'
      || !/^[0-9a-f]{32}$/.test(share.shareId) || canonicalOrigin(share.origin + '/') !== share.origin
      || !['ready', 'away', 'handback'].includes(share.state) || !Number.isInteger(share.nav)) continue;
    out.shares[key] = { tabId: share.tabId, shareId: share.shareId, origin: share.origin, scope: share.scope, state: share.state,
      nav: share.nav, title: clip(share.title, 200), url: clip(share.url, 2048), since: Number(share.since) || 0,
      reasons: Array.isArray(share.reasons) ? share.reasons.filter(r => typeof r === 'string').slice(0, 8) : [] };
  }
  return out;
}
