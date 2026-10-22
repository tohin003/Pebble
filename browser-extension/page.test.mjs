// Pebble for Chrome, Phase 22: the injected page script, run from its own source text (as chrome.scripting does)
// against a small fake DOM. It must not depend on anything in its module.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { pebblePage } from './lib/page.mjs';

class FakeElement {
  constructor(tag, doc) {
    Object.assign(this, { tagName: tag.toUpperCase(), attrs: {}, children: [], parent: null, host: null, doc, listeners: {},
      className: '', hidden: false, title: '', type: '', rect: { width: 120, height: 24 }, computed: { visibility: 'visible', display: 'block', opacity: '1' } });
    this._text = '';
  }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  set innerHTML(value) { throw new Error('innerHTML is never used'); }
  get value() { throw new Error('a field value was read'); }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return Object.hasOwn(this.attrs, key) ? this.attrs[key] : null; }
  append(...nodes) { for (const node of nodes) { if (node.parent) node.detach(); node.parent = this; this.children.push(node); } this.doc.mutate(); }
  detach() { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
  remove() { if (this.parent) { this.detach(); this.doc.mutate(); } }
  get isConnected() { let node = this; while (node) { if (node === this.doc.documentElement) return true; node = node.parent || node.host; } return false; }
  attachShadow({ mode }) { const root = new FakeElement('#shadow-root', this.doc); root.host = this; this.shadow = root; this.shadowMode = mode; return root; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click(trusted) { for (const fn of this.listeners.click || []) fn({ isTrusted: trusted }); }
  closest() { let node = this; while (node) { if (node.attrs && (Object.hasOwn(node.attrs, 'hidden') || node.attrs['aria-hidden'] === 'true')) return node; node = node.parent; } return null; }
  getBoundingClientRect() { return this.rect; }
  all() { return [this, ...this.children.flatMap(c => c.all())]; }
}

function fakePage() {
  const sent = [], observers = [];
  const doc = { observers, mutate() { for (const observer of observers) if (observer.live) observer.fn([]); } };
  doc.createElement = tag => new FakeElement(tag, doc);
  doc.documentElement = new FakeElement('html', doc);
  doc.body = new FakeElement('body', doc);
  doc.documentElement.children.push(doc.body); doc.body.parent = doc.documentElement;
  doc.querySelectorAll = selector => {
    const every = doc.documentElement.all();
    if (selector.startsWith('input')) return every.filter(e => ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName));
    return every.filter(e => (e.tagName === 'IFRAME' && /recaptcha|hcaptcha|turnstile/.test(e.getAttribute('src') || ''))
      || /captcha/i.test(e.getAttribute('title') || '') || /g-recaptcha|h-captcha|cf-turnstile/.test(e.className));
  };
  const timers = [];
  Object.assign(globalThis, {
    document: doc, Element: FakeElement, getComputedStyle: element => element.computed,
    MutationObserver: class { constructor(fn) { this.fn = fn; this.live = false; } observe() { this.live = true; observers.push(this); } disconnect() { this.live = false; } },
    // Runtime.addBinding exposes this in Pebble's isolated world only; its payload is a JSON string.
    pebbleSignal: payload => { assert.equal(typeof payload, 'string'); sent.push(JSON.parse(payload)); },
    setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout: () => {},
  });
  delete globalThis.chrome;
  delete globalThis.__pebbleIndicator;
  return { doc, sent, timers, run: () => { const due = timers.splice(0); due.forEach(fn => fn()); } };
}

// As chrome.scripting does: only the function's source travels.
const page = new Function('return (' + pebblePage.toString() + ')')();
const field = (doc, attrs, visible = true) => {
  const node = doc.createElement('input');
  Object.assign(node.attrs, attrs);
  if (!visible) node.rect = { width: 0, height: 0 };
  doc.body.append(node);
  return node;
};

test('the indicator is a zero-size host with a closed shadow root, four edge strips and a status pill', () => {
  const { doc } = fakePage();
  assert.deepEqual(page({ action: 'indicator', state: 'acting', label: '<img src=x onerror=alert(1)>' }), { ok: true, state: 'acting' });
  const host = doc.documentElement.children.find(node => node.tagName === 'PEBBLE-INDICATOR');
  assert.ok(host && host.shadowMode === 'closed');
  assert.match(host.attrs.style, /width: 0 !important; height: 0 !important/);
  const parts = host.shadow.all();
  assert.equal(parts.filter(node => / edge /.test(' ' + node.className + ' ') || /^edge /.test(node.className)).length, 4);
  const pill = parts.find(node => node.className.startsWith('pill'));
  assert.equal(pill.attrs.role, 'status');
  assert.equal(pill.attrs['aria-live'], 'polite');
  assert.ok(pill.textContent.includes('Pebble is working in this tab'));
  assert.equal(pill.title, '<img src=x onerror=alert(1)>', 'the label is text, never markup');
  const frame = parts.find(node => node.className.startsWith('frame'));
  assert.equal(frame.className, 'frame on');
  const style = parts.find(node => node.tagName === 'STYLE').textContent;
  assert.match(style, /prefers-reduced-motion: reduce/);
  assert.match(style, /prefers-color-scheme: dark/);
  page({ action: 'indicator', state: 'handback' });
  assert.ok(pill.textContent.includes("Your turn: Pebble doesn't act on sign-in or payment pages"));
  assert.equal(parts.find(node => node.tagName === 'BUTTON').hidden, true, 'no Stop on a hand-back page: Pebble is not acting');
  page({ action: 'indicator', state: 'off' });
  assert.ok(!doc.documentElement.children.some(node => node.tagName === 'PEBBLE-INDICATOR'));
});

test('only the person\'s own click on Stop stops Pebble', () => {
  const { doc, sent } = fakePage();
  page({ action: 'indicator', state: 'acting' });
  const host = doc.documentElement.children.find(node => node.tagName === 'PEBBLE-INDICATOR');
  const stop = host.shadow.all().find(node => node.tagName === 'BUTTON');
  assert.equal(stop.attrs['aria-label'], 'Stop Pebble');
  stop.click(false);
  assert.deepEqual(sent, []);
  stop.click(true);
  assert.deepEqual(sent, [{ type: 'page:stop' }]);
});

test('a removed indicator comes back; a page that keeps removing it makes Pebble stop there', () => {
  const { doc, sent } = fakePage();
  page({ action: 'indicator', state: 'acting' });
  const find = () => doc.documentElement.children.find(node => node.tagName === 'PEBBLE-INDICATOR');
  find().remove();
  assert.ok(find(), 'it came back');
  for (let i = 0; i < 25 && find(); i++) find().remove();
  assert.deepEqual(sent.filter(m => m.type === 'page:tampered'), [{ type: 'page:tampered' }]);
});

test('page facts count visible credential fields by type and autocomplete, never reading a value', () => {
  const { doc, sent, run } = fakePage();
  field(doc, { type: 'email' });
  field(doc, { type: 'password' });
  field(doc, { type: 'password' }, false);
  field(doc, { type: 'hidden', autocomplete: 'one-time-code' });
  field(doc, { autocomplete: 'one-time-code' });
  field(doc, { autocomplete: 'cc-number' });
  field(doc, { autocomplete: 'section-pay cc-csc' });
  const hidden = field(doc, { type: 'password' });
  hidden.attrs.hidden = '';
  const widget = doc.createElement('iframe');
  widget.attrs.src = 'https://www.google.com/recaptcha/api2/anchor';
  doc.body.append(widget);
  assert.deepEqual(page({ action: 'facts' }), { ok: true, facts: { password: 1, one_time_code: 1, payment: 2, captcha: 1 } });
  field(doc, { autocomplete: 'new-password' });  // a sign-up form appears later
  run();
  assert.deepEqual(sent.at(-1), { type: 'page:facts', facts: { password: 2, one_time_code: 1, payment: 2, captcha: 1 } });
});

test('the page script answers nothing else', () => {
  fakePage();
  assert.deepEqual(page({ action: 'eval', code: 'alert(1)' }), { ok: false });
  assert.deepEqual(page(null), { ok: false });
  assert.ok(!/\beval\s*\(|new Function|innerHTML|insertAdjacentHTML|document\.write|\bfetch\s*\(|XMLHttpRequest|\.value\b/.test(pebblePage.toString()));
});
