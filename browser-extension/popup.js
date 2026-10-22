// The toolbar popup: Chrome glue only (lib/popup-model.mjs holds the words and the rendering).
import { model, render } from './lib/popup-model.mjs';

const root = document.getElementById('root');
chrome.runtime.connect({ name: 'popup' });  // tells the worker the popup is open; closing the popup ends it
const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
const tabId = Number.isInteger(tab?.id) ? tab.id : null;
let busy = false, first = true;

async function ask(message) {
  try { return await chrome.runtime.sendMessage({ ...message, tabId }); } catch { return null; }
}

function show(view) {
  const focused = document.activeElement?.dataset?.action;
  const typing = document.activeElement?.classList?.contains('code') ? document.activeElement.value : null;
  render(root, model(view), document, act);
  const buttons = [...root.querySelectorAll('button')];
  buttons.forEach(node => { node.dataset.action = node.textContent; });
  const code = root.querySelector('input.code');
  if (code && typing !== null) { code.value = typing; code.focus(); return; }  // a poll never wipes a code being typed
  const again = buttons.find(node => node.dataset.action === focused);
  if (again) again.focus();
  else if (first) (code || root.querySelector('.button.primary') || buttons[0])?.focus();
  first = false;
}

const REQUESTS = { 'share-tab': { type: 'popup:share', scope: 'tab' }, 'share-site': { type: 'popup:share', scope: 'site' },
  stop: { type: 'popup:stop' }, 'pair-start': { type: 'popup:pair_start' }, unpair: { type: 'popup:unpair' } };

async function act(id, other, value) {
  if (busy) return;
  busy = true;
  try {
    let reply = null;
    if (Object.hasOwn(REQUESTS, id)) reply = await ask(REQUESTS[id]);
    else if (id === 'pair-code') reply = await ask({ type: 'popup:pair_code', code: value });
    else if (id === 'unshare') reply = await chrome.runtime.sendMessage({ type: 'popup:unshare', tabId: other ?? tabId }).catch(() => null);
    else if (id === 'connect') reply = { view: await ask({ type: 'popup:connect' }) };
    if (reply?.view) show(reply.view);
  } finally {
    busy = false;
  }
}

async function refresh() {
  if (!busy) {
    const view = await ask({ type: 'popup:state' });
    if (view) show(view);
  }
  setTimeout(refresh, 1500);
}
void refresh();
