// Pebble for Chrome: what the service worker lets through to chrome.debugger, and what it sends back (Phase 22).
//
// Pure functions only; the Chrome glue is lib/worker.mjs. Pebble's backend (browser_session.BrowserAdapter, by way
// of browser_bridge) is the only caller, and this file is a second fence in front of the debugger. Even a confused
// or compromised backend cannot run its own script in a page, rewrite or read a request, act in a tab that is not
// shared, is on another site, or shows a sign-in or payment page, or learn the other sites a tab visited.

export const PROTOCOL = 'pebble-browser/1';
export const HOST_NAME = 'com.pebble.browser';
export const MAX_FRAME = 1 << 20;            // one native message either way
export const CHUNK_BYTES = 600 * 1024;       // raw bytes a chunk carries; base64 makes it 800 KiB
export const MAX_REPLY = 32 * 1024 * 1024;   // browser_session.MAX_MESSAGE
export const MAX_TEXT = 4000;                // ax_worker.MAX_TEXT
export const WORLD = 'pebble-screen';
export const OBJECT_GROUP = 'pebble-act';
export const VISIBILITY_JS = 'document.visibilityState';
const MAX_CONTEXTS = 64;
const MAX_OBJECTS = 512;
const LOOPBACK = new Set(['127.0.0.1', 'localhost', '[::1]']);
const STORE_HOSTS = new Set(['chromewebstore.google.com', 'chrome.google.com']);

// BEGIN ISOLATED FUNCTIONS (browser_session.py's isolated-world functions; pinned by test_browser_bridge_22.py)
export const ISOLATED_FUNCTIONS = Object.freeze([
  "function(o){return this===o||(this.nodeType===1&&this.contains(o))||(this.nodeType===3&&this.parentElement===o)}",
  "function(){this.focus();if(typeof this.select===\"function\"){this.select();return true}const r=document.createRange();r.selectNodeContents(this);const s=getSelection();s.removeAllRanges();s.addRange(r);return true}",
  "function(t){if(this.tagName!==\"SELECT\")return false;const w=String(t).trim();for(const o of this.options){if(o.label.trim()===w||o.text.trim()===w){this.value=o.value;this.dispatchEvent(new Event(\"input\",{bubbles:true}));this.dispatchEvent(new Event(\"change\",{bubbles:true}));return true}}return false}",
  "function(){const s=this.closest(\"select\");if(!s)return false;this.selected=true;s.dispatchEvent(new Event(\"input\",{bubbles:true}));s.dispatchEvent(new Event(\"change\",{bubbles:true}));return true}",
  "function(o){const d=this.tagName===\"SUMMARY\"?this.parentElement:this;if(!d||d.tagName!==\"DETAILS\")return false;d.open=o;return true}",
  "function(){return this.ownerDocument.activeElement===this}"
]);
// END ISOLATED FUNCTIONS
// The arguments each isolated function takes, in order.
const FUNCTION_ARGS = new Map([
  [ISOLATED_FUNCTIONS[0], ['object']], [ISOLATED_FUNCTIONS[1], []], [ISOLATED_FUNCTIONS[2], ['string']],
  [ISOLATED_FUNCTIONS[3], []], [ISOLATED_FUNCTIONS[4], ['boolean']], [ISOLATED_FUNCTIONS[5], []],
]);
// DOM attributes BrowserAdapter reads; every other attribute (value, style, data-*) stays in the page.
export const ATTRIBUTES = new Set(['type', 'placeholder', 'autocomplete', 'href', 'download', 'form', 'id', 'formaction',
  'action', 'method', 'allow', 'title', 'name', 'aria-label', 'multiple']);
const AX_PROPERTIES = new Set(['focusable', 'editable', 'multiline', 'checked', 'expanded', 'selected', 'disabled',
  'focused']);
const KEYS = new Map([
  ['Enter', { code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, text: '\r' }],
  ['Backspace', { code: 'Backspace', windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 51 }],
]);
// Commands that only release what an earlier command paused or held; allowed whenever the debugger is attached.
export const RELEASES = new Set(['Fetch.continueRequest', 'Fetch.failRequest', 'Fetch.disable', 'Runtime.releaseObjectGroup']);
// Commands that change the page; the indicator shows "working" while they run.
export const EFFECTS = new Set(['Input.dispatchMouseEvent', 'Input.dispatchKeyEvent', 'Input.insertText', 'DOM.focus',
  'DOM.scrollIntoViewIfNeeded', 'Runtime.callFunctionOn']);

const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value, limit) => typeof value === 'string' && value.length > 0 && value.length <= limit && !value.includes('\0');
const node = value => Number.isInteger(value) && value > 0 && value < 2 ** 31;
const finite = value => typeof value === 'number' && Number.isFinite(value) && Math.abs(value) < 1e6;
const only = (params, allowed, required = allowed) => plain(params) && Object.keys(params).every(key => allowed.includes(key))
  && required.every(key => own(params, key));
const empty = params => plain(params) && Object.keys(params).length === 0;

// The canonical origin of an https URL, or of an http URL on this Mac; otherwise null (browser_session.url_origin).
export function canonicalOrigin(url) {
  if (typeof url !== 'string' || url.length > 8192) return null;
  let parsed;
  try { parsed = new URL(url); } catch { return null; }
  if (parsed.username || parsed.password) return null;
  if (parsed.protocol === 'https:') return parsed.origin;
  if (parsed.protocol === 'http:' && LOOPBACK.has(parsed.hostname)) return parsed.origin;
  return null;
}

// Pages Pebble never takes: browser and extension pages, files, and the Chrome Web Store.
export function shareable(url) {
  const origin = canonicalOrigin(url);
  if (!origin) return false;
  return !STORE_HOSTS.has(new URL(url).hostname);
}

export function newTabState() {
  return { contexts: new Set(), objects: new Set() };
}

function callFunction(params, tab) {
  if (!only(params, ['functionDeclaration', 'objectId', 'arguments', 'returnByValue'], ['functionDeclaration', 'objectId'])) return false;
  const kinds = FUNCTION_ARGS.get(params.functionDeclaration);
  if (!kinds || !tab.objects.has(params.objectId)) return false;
  if (own(params, 'returnByValue') && params.returnByValue !== true) return false;
  const args = own(params, 'arguments') ? params.arguments : [];
  if (!Array.isArray(args) || args.length !== kinds.length) return false;
  return args.every((arg, index) => {
    if (!plain(arg) || Object.keys(arg).length !== 1) return false;
    const kind = kinds[index];
    if (kind === 'object') return own(arg, 'objectId') && tab.objects.has(arg.objectId);
    if (kind === 'string') return own(arg, 'value') && typeof arg.value === 'string' && arg.value.length <= MAX_TEXT && !arg.value.includes('\0');
    return own(arg, 'value') && typeof arg.value === kind;
  });
}

function mouse(params) {
  if (!only(params, ['type', 'x', 'y', 'button', 'buttons', 'clickCount'], ['type', 'x', 'y'])) return false;
  if (!finite(params.x) || !finite(params.y)) return false;
  if (params.type === 'mouseMoved') return Object.keys(params).length === 3;
  return (params.type === 'mousePressed' || params.type === 'mouseReleased') && params.button === 'left'
    && params.buttons === (params.type === 'mousePressed' ? 1 : 0) && params.clickCount === 1;
}

function key(params) {
  if (!only(params, ['type', 'key', 'code', 'windowsVirtualKeyCode', 'nativeVirtualKeyCode', 'text'],
    ['type', 'key', 'code', 'windowsVirtualKeyCode', 'nativeVirtualKeyCode'])) return false;
  const known = KEYS.get(params.key);
  if (!known || (params.type !== 'keyDown' && params.type !== 'keyUp')) return false;
  if (params.code !== known.code || params.windowsVirtualKeyCode !== known.windowsVirtualKeyCode
    || params.nativeVirtualKeyCode !== known.nativeVirtualKeyCode) return false;
  if (own(params, 'text')) return params.type === 'keyDown' && params.text === known.text;
  return true;
}

const COMMANDS = {
  'Page.enable': empty,
  'Page.getFrameTree': empty,
  'Page.getNavigationHistory': empty,
  'Page.bringToFront': empty,
  'Page.setInterceptFileChooserDialog': p => only(p, ['enabled']) && p.enabled === true,
  // Phase 24 (V1-153): Chrome's parameter is grantUniversalAccess; the explicit false must reach it (browser_session.py
  // sends the same key; test_browser_bridge_22.py pins both).
  'Page.createIsolatedWorld': p => only(p, ['frameId', 'worldName', 'grantUniversalAccess']) && text(p.frameId, 128)
    && p.worldName === WORLD && p.grantUniversalAccess === false,
  'Page.captureScreenshot': p => only(p, ['format', 'captureBeyondViewport']) && p.format === 'png' && p.captureBeyondViewport === false,
  'Runtime.evaluate': (p, tab) => only(p, ['expression', 'returnByValue', 'contextId']) && p.expression === VISIBILITY_JS
    && p.returnByValue === true && tab.contexts.has(p.contextId),
  'Runtime.callFunctionOn': callFunction,
  'Runtime.releaseObjectGroup': p => only(p, ['objectGroup']) && p.objectGroup === OBJECT_GROUP,
  'DOMSnapshot.captureSnapshot': p => only(p, ['computedStyles', 'includeDOMRects']) && Array.isArray(p.computedStyles)
    && p.computedStyles.length === 0 && p.includeDOMRects === true,
  'Accessibility.getFullAXTree': p => only(p, ['frameId']) && text(p.frameId, 128),
  'Accessibility.getPartialAXTree': p => only(p, ['backendNodeId', 'fetchRelatives']) && node(p.backendNodeId) && p.fetchRelatives === false,
  'DOM.getFrameOwner': p => only(p, ['frameId']) && text(p.frameId, 128),
  'DOM.getContentQuads': p => only(p, ['backendNodeId']) && node(p.backendNodeId),
  'DOM.describeNode': p => only(p, ['backendNodeId']) && node(p.backendNodeId),
  'DOM.resolveNode': (p, tab) => only(p, ['backendNodeId', 'objectGroup', 'executionContextId']) && node(p.backendNodeId)
    && p.objectGroup === OBJECT_GROUP && tab.contexts.has(p.executionContextId),
  'DOM.getNodeForLocation': p => only(p, ['x', 'y', 'includeUserAgentShadowDOM', 'ignorePointerEventsNone']) && Number.isInteger(p.x)
    && Number.isInteger(p.y) && finite(p.x) && finite(p.y) && typeof p.includeUserAgentShadowDOM === 'boolean'
    && typeof p.ignorePointerEventsNone === 'boolean',
  'DOM.focus': p => only(p, ['backendNodeId']) && node(p.backendNodeId),
  'DOM.scrollIntoViewIfNeeded': p => only(p, ['backendNodeId']) && node(p.backendNodeId),
  'Input.dispatchMouseEvent': mouse,
  'Input.dispatchKeyEvent': key,
  'Input.insertText': p => only(p, ['text']) && typeof p.text === 'string' && p.text.length <= MAX_TEXT && !p.text.includes('\0'),
  'Fetch.enable': p => only(p, ['patterns']) && Array.isArray(p.patterns) && p.patterns.length === 1 && only(p.patterns[0], ['urlPattern', 'requestStage'])
    && p.patterns[0].urlPattern === '*' && p.patterns[0].requestStage === 'Request',
  'Fetch.disable': empty,
  // Only the request ID: a continued request is never rewritten (no url, method, headers or body).
  'Fetch.continueRequest': p => only(p, ['requestId']) && text(p.requestId, 128),
  'Fetch.failRequest': p => only(p, ['requestId', 'errorReason']) && text(p.requestId, 128) && p.errorReason === 'Aborted',
};
export const METHODS = Object.freeze(Object.keys(COMMANDS));

// {ok: true} or {ok: false, code, message} for one page command. share is the worker's share for the tab; granted
// says whether Pebble holds a live grant for it (only releases run without one).
export function checkCommand(share, tab, method, params, { paused = false, granted = false } = {}) {
  if (typeof method !== 'string' || !own(COMMANDS, method)) return { ok: false, code: 'unsupported', message: 'Pebble for Chrome does not run that command.' };
  if (!share) return { ok: false, code: 'not_shared', message: 'This tab is not shared with Pebble.' };
  if (!RELEASES.has(method)) {
    if (!granted) return { ok: false, code: 'not_granted', message: 'No task in Pebble holds a live grant for this tab.' };
    if (paused) return { ok: false, code: 'paused', message: 'Pebble is paused in Chrome.' };
    if (share.state === 'away') return { ok: false, code: 'away', message: 'The tab is on a site that was not shared.' };
    if (share.state === 'handback') return { ok: false, code: 'handback', message: "This page is the person's turn." };
  }
  if (!COMMANDS[method](params ?? {}, tab)) return { ok: false, code: 'invalid', message: 'Pebble for Chrome refused that command.' };
  return { ok: true };
}

// Remember what a command created, so later commands may only use it (worlds and the objects resolved in them).
export function noteResult(tab, method, result) {
  if (method === 'Page.createIsolatedWorld' && Number.isInteger(result?.executionContextId)) {
    if (tab.contexts.size >= MAX_CONTEXTS) tab.contexts.clear();
    tab.contexts.add(result.executionContextId);
  } else if (method === 'DOM.resolveNode' && typeof result?.object?.objectId === 'string') {
    if (tab.objects.size >= MAX_OBJECTS) tab.objects.clear();
    tab.objects.add(result.object.objectId);
  } else if (method === 'Runtime.releaseObjectGroup') {
    tab.objects.clear();
  }
}

export function filterAttributes(flat) {
  const out = [];
  if (!Array.isArray(flat)) return out;
  for (let index = 0; index + 1 < flat.length; index += 2) {
    const name = flat[index], value = flat[index + 1];
    if (typeof name === 'string' && typeof value === 'string' && ATTRIBUTES.has(name.toLowerCase())) out.push(name, value);
  }
  return out;
}

// A DOMSnapshot reduced to what BrowserAdapter reads, with a string table holding only those strings:
// typed values (inputValue, textValue), text node contents and every other attribute never leave the page.
export function compactSnapshot(raw) {
  const strings = Array.isArray(raw?.strings) ? raw.strings : [];
  const table = [], index = new Map();
  const keep = position => {
    if (!Number.isInteger(position) || position < 0 || position >= strings.length || typeof strings[position] !== 'string') return -1;
    const value = strings[position];
    if (!index.has(value)) { index.set(value, table.length); table.push(value); }
    return index.get(value);
  };
  const documents = (Array.isArray(raw?.documents) ? raw.documents : []).map(doc => {
    const nodes = plain(doc?.nodes) ? doc.nodes : {};
    const attributes = (Array.isArray(nodes.attributes) ? nodes.attributes : []).map(row => {
      const out = [];
      if (!Array.isArray(row)) return out;
      for (let i = 0; i + 1 < row.length; i += 2) {
        const name = Number.isInteger(row[i]) ? strings[row[i]] : null;
        if (typeof name === 'string' && ATTRIBUTES.has(name.toLowerCase())) out.push(keep(row[i]), keep(row[i + 1]));
      }
      return out;
    });
    const content = plain(nodes.contentDocumentIndex) ? nodes.contentDocumentIndex : {};
    const layout = plain(doc?.layout) ? doc.layout : {};
    return {
      documentURL: keep(doc?.documentURL), baseURL: keep(doc?.baseURL), frameId: keep(doc?.frameId),
      scrollOffsetX: finite(doc?.scrollOffsetX) ? doc.scrollOffsetX : 0, scrollOffsetY: finite(doc?.scrollOffsetY) ? doc.scrollOffsetY : 0,
      nodes: {
        parentIndex: Array.isArray(nodes.parentIndex) ? nodes.parentIndex : [],
        nodeName: (Array.isArray(nodes.nodeName) ? nodes.nodeName : []).map(keep),
        backendNodeId: Array.isArray(nodes.backendNodeId) ? nodes.backendNodeId : [],
        attributes,
        contentDocumentIndex: { index: Array.isArray(content.index) ? content.index : [], value: Array.isArray(content.value) ? content.value : [] },
      },
      layout: { nodeIndex: Array.isArray(layout.nodeIndex) ? layout.nodeIndex : [], bounds: Array.isArray(layout.bounds) ? layout.bounds : [] },
    };
  });
  return { documents, strings: table };
}

// AX nodes reduced to the fields and properties browser_session._facts and _Walk read.
export function compactAX(nodes) {
  return (Array.isArray(nodes) ? nodes : []).filter(plain).map(item => {
    const out = {};
    for (const field of ['nodeId', 'ignored', 'role', 'name', 'description', 'value', 'backendDOMNodeId', 'childIds', 'parentId']) {
      if (own(item, field)) out[field] = item[field];
    }
    if (Array.isArray(item.properties)) out.properties = item.properties.filter(prop => plain(prop) && AX_PROPERTIES.has(prop.name));
    return out;
  });
}

// Frame URLs off the shared site are cut to their origin; the main frame keeps its own address.
function frameTree(tree, origin, depth = 0) {
  if (!plain(tree) || depth > 32) return tree;
  const frame = plain(tree.frame) ? { ...tree.frame } : tree.frame;
  if (plain(frame) && frame.parentId) {
    const site = canonicalOrigin(frame.url);
    if (site !== origin) {
      frame.url = site ? site + '/' : 'about:blank';
      if (!site) delete frame.securityOrigin;
    }
    delete frame.name;
  }
  const out = { frame };
  if (Array.isArray(tree.childFrames)) out.childFrames = tree.childFrames.map(child => frameTree(child, origin, depth + 1));
  return out;
}

// A command's result as it may leave the browser.
export function shapeResult(method, result, share) {
  if (!plain(result)) return {};
  if (method === 'DOMSnapshot.captureSnapshot') return compactSnapshot(result);
  if (method === 'Accessibility.getFullAXTree' || method === 'Accessibility.getPartialAXTree') return { nodes: compactAX(result.nodes) };
  if (method === 'DOM.describeNode') {
    const item = plain(result.node) ? result.node : {};
    return { node: { backendNodeId: item.backendNodeId, nodeName: item.nodeName, localName: item.localName, nodeType: item.nodeType,
      attributes: filterAttributes(item.attributes) } };
  }
  // The tab's history: entry IDs only. The other sites this tab visited are none of Pebble's business.
  if (method === 'Page.getNavigationHistory') {
    return { currentIndex: result.currentIndex, entries: (Array.isArray(result.entries) ? result.entries : []).map(entry => ({ id: entry?.id })) };
  }
  if (method === 'Page.getFrameTree') return { frameTree: frameTree(result.frameTree, share?.origin) };
  if (method === 'Page.captureScreenshot') return { data: typeof result.data === 'string' ? result.data : '' };
  return result;
}

const EVENTS = new Set(['Page.frameRequestedNavigation', 'Page.frameStartedLoading', 'Page.frameStoppedLoading',
  'Page.frameNavigated', 'Page.navigatedWithinDocument', 'Page.javascriptDialogOpening', 'Page.javascriptDialogClosed',
  'Page.fileChooserOpened', 'Inspector.targetCrashed', 'Inspector.detached', 'Fetch.requestPaused']);
export const FORWARDED_EVENTS = Object.freeze([...EVENTS]);

// An event as it may leave the browser, or null to drop it. No URL path, query, dialog text or request body leaves.
export function sanitizeEvent(method, params) {
  if (!EVENTS.has(method)) return null;
  const p = plain(params) ? params : {};
  const id = value => (typeof value === 'string' && value.length <= 128 ? value : undefined);
  switch (method) {
    case 'Page.frameRequestedNavigation':
      return { frameId: id(p.frameId), disposition: id(p.disposition), reason: id(p.reason) };
    case 'Page.frameNavigated': {
      const frame = plain(p.frame) ? p.frame : {};
      return { frame: { id: id(frame.id), parentId: id(frame.parentId), loaderId: id(frame.loaderId) }, type: id(p.type) };
    }
    case 'Page.javascriptDialogOpening':
      return { type: id(p.type) };
    case 'Page.javascriptDialogClosed':
    case 'Inspector.targetCrashed':
      return {};
    case 'Page.fileChooserOpened':
      return { frameId: id(p.frameId), mode: id(p.mode) };
    case 'Inspector.detached':
      return { reason: id(p.reason) };
    case 'Fetch.requestPaused': {
      const request = plain(p.request) ? p.request : {};
      const origin = canonicalOrigin(request.url);
      return { requestId: id(p.requestId), frameId: id(p.frameId), resourceType: id(p.resourceType),
        request: { method: id(request.method) || 'GET', url: origin ? origin + '/' : 'other:' } };
    }
    default:
      return { frameId: id(p.frameId) };
  }
}

function base64(bytes) {
  let binary = '';
  for (let index = 0; index < bytes.length; index += 0x8000) binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  return btoa(binary);
}

// The frames that carry one reply: a single 'reply', or base64 'chunk's when it is larger than one message. Sealed,
// the reply travels as a JSON string inside JSON, so the size that counts is the twice-encoded one.
export function replyFrames(id, reply, { maxFrame = MAX_FRAME, chunkBytes = CHUNK_BYTES, maxReply = MAX_REPLY, sealed = true } = {}) {
  const whole = { type: 'reply', id, ...reply };
  const text = JSON.stringify(whole);
  const bytes = new TextEncoder().encode(text);
  const framed = sealed ? new TextEncoder().encode(JSON.stringify(text)).length + 160 : bytes.length + 64;
  if (framed <= maxFrame) return [whole];
  if (bytes.length > maxReply) return [{ type: 'reply', id, ok: false, error: { code: 'too_large', message: 'The answer is larger than Pebble accepts.' } }];
  const frames = [];
  for (let start = 0, seq = 0; start < bytes.length; start += chunkBytes, seq++) {
    frames.push({ type: 'chunk', id, seq, last: start + chunkBytes >= bytes.length, data: base64(bytes.subarray(start, start + chunkBytes)) });
  }
  return frames;
}

// A token bucket: take() is false when the caller should refuse.
export class Bucket {
  constructor(size, perSecond, now) { this.size = size; this.rate = perSecond; this.level = size; this.at = now; }
  take(now) {
    this.level = Math.min(this.size, this.level + Math.max(0, now - this.at) / 1000 * this.rate);
    this.at = now;
    if (this.level < 1) return false;
    this.level -= 1;
    return true;
  }
}
