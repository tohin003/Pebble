// The minimal page script (Phase 22). lib/worker.mjs runs pebblePage in a granted tab's main frame through the
// debugger it already holds: Runtime.evaluate of the function's source in Pebble's own isolated world
// ('pebble-indicator'), so the extension needs no `scripting` permission and no host permission. The function must be
// self-contained (only its source travels). The page cannot read its variables or call it. It talks back through
// the binding pebbleSignal, which Runtime.addBinding exposes in that world only.
//
// It does two things and nothing else:
//   indicator  A visible border and a status pill while Pebble works ("acting"), a quieter pill while the tab is
//              shared, paused or the person's turn. The host element <pebble-indicator> is a zero-size box with a
//              closed shadow root; the border is four thin strips, so it never covers the page it frames and
//              browser_bridge never reads it (INDICATOR_TAG). If the page removes it, it comes back; a page that
//              keeps removing it makes Pebble stop in that tab.
//   facts      Counts of visible password, one-time-code and card fields and of CAPTCHA widgets, from input
//              types and autocomplete hints. Values are never read. A change is reported to the worker.

export function pebblePage(command) {
  const KEY = '__pebbleIndicator';
  const root = globalThis;
  const self = root[KEY] || (root[KEY] = { host: null, shadow: null, state: 'off', restores: 0, observer: null,
    facts: null, timer: 0, label: '' });
  const send = message => {
    try {
      const signal = root.pebbleSignal;  // Runtime.addBinding, only in Pebble's isolated world
      if (typeof signal === 'function') signal(JSON.stringify(message));
    } catch { /* the debugger went away */ }
  };

  function visible(element) {
    if (!(element instanceof Element) || element.closest('[hidden],[aria-hidden="true"]')) return false;
    const box = element.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) return false;
    const style = getComputedStyle(element);
    return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity) !== 0;
  }

  function facts() {
    const out = { password: 0, one_time_code: 0, payment: 0, captcha: 0 };
    const fields = document.querySelectorAll('input, textarea, select');
    for (let index = 0; index < fields.length && index < 2000; index++) {
      const field = fields[index];
      const type = (field.getAttribute('type') || '').trim().toLowerCase();
      if (type === 'hidden' || !visible(field)) continue;
      const hint = (field.getAttribute('autocomplete') || '').toLowerCase();
      if (type === 'password' || /(^|\s)(current-password|new-password)(\s|$)/.test(hint)) out.password++;
      if (/(^|\s)one-time-code(\s|$)/.test(hint)) out.one_time_code++;
      if (/(^|\s)cc-[a-z-]+(\s|$)/.test(hint)) out.payment++;
    }
    const widgets = document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], '
      + 'iframe[title*="captcha" i], .g-recaptcha, .h-captcha, .cf-turnstile');
    for (const widget of widgets) if (visible(widget)) out.captcha++;
    return out;
  }

  const STYLE = `
    :host { all: initial; }
    .edge { position: fixed; z-index: 2147483647; pointer-events: none; background: var(--accent);
      box-shadow: 0 0 12px var(--glow); opacity: 0; transition: opacity 180ms ease; }
    .on .edge { opacity: 1; animation: breathe 2.4s ease-in-out infinite; }
    .top { top: 0; left: 0; right: 0; height: 3px; } .bottom { bottom: 0; left: 0; right: 0; height: 3px; }
    .left { top: 0; bottom: 0; left: 0; width: 3px; } .right { top: 0; bottom: 0; right: 0; width: 3px; }
    .pill { position: fixed; z-index: 2147483647; right: 16px; bottom: 16px; display: flex; align-items: center; gap: 8px;
      max-width: min(420px, calc(100vw - 32px)); padding: 7px 8px 7px 12px; border-radius: 999px; background: var(--panel);
      color: var(--text); border: 1px solid var(--line); box-shadow: 0 8px 28px rgba(19, 37, 27, .18);
      font: 500 12px/1.3 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; letter-spacing: 0;
      pointer-events: auto; }
    .pill.quiet { padding-right: 12px; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); flex: none; }
    .on .dot { animation: breathe 1.6s ease-in-out infinite; }
    .turn .dot { background: var(--turn); }
    .text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    button { all: unset; cursor: pointer; padding: 4px 10px; border-radius: 999px; background: var(--accent); color: var(--on);
      font: 600 12px/1.2 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
    button:hover { filter: brightness(1.06); }
    button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    @keyframes breathe { 0%, 100% { opacity: 1; } 50% { opacity: .55; } }
    @media (prefers-reduced-motion: reduce) { .on .edge, .on .dot { animation: none; } .edge { transition: none; } }
    .frame { --accent: #506e54; --glow: rgba(80, 110, 84, .45); --panel: #ffffff; --text: #252c28; --line: #e5e9e1;
      --on: #ffffff; --turn: #ad5a4a; }
    @media (prefers-color-scheme: dark) {
      .frame { --accent: #a6c795; --glow: rgba(166, 199, 149, .4); --panel: #212822; --text: #e0e7de; --line: #2b342c;
        --on: #1b281a; --turn: #e3a38f; }
    }`;
  const WORDS = {
    shared: 'Shared with Pebble',
    acting: 'Pebble is working in this tab',
    paused: 'Pebble is paused in this tab',
    handback: "Your turn: Pebble doesn't act on sign-in or payment pages",
  };

  function build() {
    const host = document.createElement('pebble-indicator');
    host.setAttribute('style', 'all: initial !important; display: block !important; position: fixed !important; top: 0 !important; '
      + 'left: 0 !important; width: 0 !important; height: 0 !important; overflow: visible !important; z-index: 2147483647 !important;');
    const shadow = host.attachShadow({ mode: 'closed' });
    const style = document.createElement('style');
    style.textContent = STYLE;
    const frame = document.createElement('div');
    frame.className = 'frame';
    for (const side of ['top', 'right', 'bottom', 'left']) {
      const edge = document.createElement('div');
      edge.className = 'edge ' + side;
      frame.append(edge);
    }
    const pill = document.createElement('div');
    pill.className = 'pill';
    pill.setAttribute('role', 'status');
    pill.setAttribute('aria-live', 'polite');
    const dot = document.createElement('span');
    dot.className = 'dot';
    dot.setAttribute('aria-hidden', 'true');
    const words = document.createElement('span');
    words.className = 'text';
    const stop = document.createElement('button');
    stop.type = 'button';
    stop.textContent = 'Stop';
    stop.setAttribute('aria-label', 'Stop Pebble');
    stop.addEventListener('click', event => {
      if (!event.isTrusted) return;  // only the person's own click
      send({ type: 'page:stop' });
    });
    pill.append(dot, words, stop);
    frame.append(pill);
    shadow.append(style, frame);
    self.host = host; self.shadow = { frame, pill, words, stop };
  }

  function render() {
    if (self.state === 'off') {
      if (self.host) self.host.remove();
      return;
    }
    if (!self.host) build();
    const { frame, pill, words, stop } = self.shadow;
    frame.className = 'frame' + (self.state === 'acting' ? ' on' : '') + (self.state === 'handback' ? ' turn' : '');
    words.textContent = WORDS[self.state] || WORDS.shared;
    const quiet = self.state === 'handback';
    stop.hidden = quiet;
    pill.className = 'pill' + (quiet ? ' quiet' : '');
    pill.title = self.label;
    if (!self.host.isConnected) (document.documentElement || document).append(self.host);
  }

  function watch() {
    if (self.observer) return;
    self.observer = new MutationObserver(() => {
      if (self.state !== 'off' && self.host && !self.host.isConnected) {
        if (++self.restores > 20) { send({ type: 'page:tampered' }); self.state = 'off'; }
        else render();
      }
      clearTimeout(self.timer);
      self.timer = setTimeout(() => {
        const next = facts();
        if (JSON.stringify(next) !== JSON.stringify(self.facts)) { self.facts = next; send({ type: 'page:facts', facts: next }); }
      }, 400);
    });
    // Added nodes and field-kind changes only: style and class churn on busy pages would cost too much. A field the
    // page reveals by a class change is still caught by the backend's own secure-field check at the next read.
    self.observer.observe(document.documentElement || document, { childList: true, subtree: true, attributes: true,
      attributeFilter: ['type', 'autocomplete', 'hidden'] });
  }

  if (!command || typeof command !== 'object') return { ok: false };
  if (command.action === 'facts') {
    watch();
    self.facts = facts();
    return { ok: true, facts: self.facts };
  }
  if (command.action === 'indicator') {
    self.state = ['off', 'shared', 'acting', 'paused', 'handback'].includes(command.state) ? command.state : 'off';
    self.label = typeof command.label === 'string' ? command.label.slice(0, 120) : '';
    if (self.state === 'off') {
      if (self.observer) { self.observer.disconnect(); self.observer = null; }
      clearTimeout(self.timer);
      self.restores = 0;
    } else {
      watch();
    }
    render();
    return { ok: true, state: self.state };
  }
  return { ok: false };
}
