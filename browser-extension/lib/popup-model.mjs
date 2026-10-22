// The toolbar popup (Phase 22): model(view) turns the worker's state into words and buttons, and render() builds
// them with DOM calls only (textContent, never HTML), so page titles stay plain text. Both are pure enough to test.

const CONNECTION = {
  connected: { tone: 'ok', text: 'Connected to Pebble' },
  connecting: { tone: 'wait', text: 'Connecting to Pebble…' },
  idle: { tone: 'wait', text: 'Connecting to Pebble…' },
  disconnected: { tone: 'wait', text: 'Reconnecting to Pebble…' },
  unpaired: { tone: 'wait', text: 'Not paired with Pebble yet' },
  mismatch: { tone: 'off', text: "Pebble didn't prove this pairing", note: 'Pair again: Pebble will show a new code.' },
  app_not_running: { tone: 'off', text: "Pebble isn't open", note: 'Open Pebble on this Mac. Shared tabs reconnect on their own.' },
  refused: { tone: 'off', text: "Pebble didn't accept the connection", note: 'Quit and reopen Pebble, then try again.' },
  not_installed: { tone: 'off', text: "Pebble's Chrome helper isn't set up" },
  forbidden: { tone: 'off', text: "Pebble doesn't recognise this copy of the extension" },
};
// Phase 24 (V1-043): the Browser panel is in Pebble's Details drawer (the /browser command opens it); Settings has no
// Browser page. "Connect Chrome" there adds the helper after one confirmation.
const WHERE = 'Details › Browser (or type /browser)';
const SETUP = [
  `In Pebble, open ${WHERE}.`,
  'Choose "Connect Chrome" and confirm: Pebble adds a small helper that lets Chrome talk to it.',
  'Come back here and press Try again.',
];
const FORBIDDEN = [
  `Load the extension from the folder Pebble shows in ${WHERE}.`,
  "If it still says this, remove this copy from chrome://extensions and load Pebble's folder again.",
];

const plainText = (value, limit) => (typeof value === 'string' ? value.slice(0, limit) : '');

function shareLine(share) {
  if (share.state === 'handback') return "Your turn: Pebble doesn't act on sign-in or payment pages.";
  if (share.state === 'away') return 'Paused: this tab is on another site.';
  if (share.acting) return 'Pebble is working in this tab now.';
  return share.granted ? 'A task in Pebble can use this tab.' : 'Shared. Choose it for a task in Pebble.';
}

export function model(view) {
  const v = view && typeof view === 'object' ? view : {};
  const connection = CONNECTION[v.connection] || CONNECTION.idle;
  const pairing = v.pairing && typeof v.pairing === 'object' ? v.pairing : {};
  const out = { status: { tone: connection.tone, text: connection.text, note: connection.note || plainText(v.message, 200) },
    paused: v.paused === true, setup: [], pair: null, tab: null, others: [], stop: false, retry: false, unpair: false };
  if (v.connection === 'not_installed') out.setup = SETUP;
  if (v.connection === 'forbidden') out.setup = FORBIDDEN;
  out.retry = ['not_installed', 'forbidden', 'app_not_running', 'refused'].includes(v.connection);
  if (v.connection === 'unpaired' || v.connection === 'mismatch') {
    const pending = pairing.pending && typeof pairing.pending === 'object' ? pairing.pending : null;
    out.pair = pending
      ? { kind: 'code', heading: 'Type the code from Pebble',
        note: `Pebble shows a 6-digit code in ${WHERE}. Type it here to connect this Chrome to your Pebble.`,
        hint: `${pending.attemptsLeft === 1 ? '1 try' : (pending.attemptsLeft || 0) + ' tries'} left · the code expires in about ${Math.max(1, Math.ceil((pending.expiresIn || 0) / 60))} min.` }
      : { kind: 'start', heading: 'Pair with Pebble',
        note: 'Pairing connects this Chrome to the Pebble app on this Mac and nothing else. Pebble shows a code; you type it here.' };
    return out;  // nothing is shared or used until the pairing holds
  }
  out.unpair = pairing.paired === true && v.connection === 'connected';
  const current = v.current && typeof v.current === 'object' ? v.current : null;
  const shares = Array.isArray(v.shares) ? v.shares : [];
  if (current) {
    const origin = plainText(current.origin, 300);
    const title = plainText(current.title, 200);
    if (current.share) {
      const share = current.share;
      out.tab = { kind: 'shared', title, origin, heading: 'Pebble can use this tab', line: shareLine(share),
        scope: share.scope === 'site' ? `Kept for ${origin} in this tab` : `Just this visit to ${origin}`,
        note: share.granted ? 'Anything that sends, buys, posts or deletes still waits for your approval in Pebble.'
          : `To use it in a task, choose this tab in Pebble’s ${WHERE}.`,
        actions: [{ id: 'unshare', label: 'Stop sharing this tab' }] };
    } else if (!current.shareable) {
      out.tab = { kind: 'unshareable', title, origin, heading: "Pebble can't use this page",
        note: 'Pebble works on https websites and on pages served from this Mac, not on browser or extension pages.', actions: [] };
    } else {
      out.tab = { kind: 'shareable', title, origin, heading: 'Let Pebble use this tab?',
        note: `Pebble sees and acts only in this tab, only on ${origin}, and only for a task you choose in Pebble. Anything that sends, buys, posts or deletes waits for your approval there.`,
        caution: current.credential ? "This looks like a sign-in or payment page. Pebble waits until you're past it." : '',
        actions: [
          { id: 'share-tab', label: 'Let Pebble use this tab', primary: true, hint: `Ends when you leave ${origin} or close the tab.` },
          { id: 'share-site', label: `Keep for ${origin}`, hint: `Stays while this tab is on ${origin}; pauses on other sites.` },
        ] };
    }
  }
  for (const share of shares) {
    if (!share || typeof share !== 'object' || (current && share.tabId === current.tabId)) continue;
    out.others.push({ tabId: share.tabId, label: plainText(share.title, 200) || plainText(share.origin, 300) || 'A tab',
      origin: plainText(share.origin, 300), line: shareLine(share) });
  }
  out.stop = shares.length > 0;
  return out;
}

// Build the popup into root. doc is the document; onAction(id, tabId, value) runs a button or the code form.
export function render(root, m, doc, onAction) {
  const el = (tag, className, text) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  const button = (label, id, tabId, kind = 'secondary', type = 'button') => {
    const node = el('button', `button ${kind}`, label);
    node.type = type;
    if (type === 'button') node.addEventListener('click', () => onAction(id, tabId));
    return node;
  };
  root.replaceChildren();
  const status = el('p', `status ${m.status.tone}`);
  status.setAttribute('role', 'status');
  const dot = el('span', 'dot');
  dot.setAttribute('aria-hidden', 'true');
  status.append(dot, el('span', '', m.status.text));
  root.append(status);
  if (m.status.note) root.append(el('p', 'note', m.status.note));
  if (m.paused) root.append(el('p', 'banner', 'Pebble is paused from the Pebble app. Resume it there.'));
  if (m.setup.length) {
    const list = el('ol', 'steps');
    list.setAttribute('aria-label', 'Set up');
    for (const step of m.setup) list.append(el('li', '', step));
    root.append(list);
  }
  if (m.retry) root.append(button('Try again', 'connect', null));
  if (m.pair) {
    const card = el('section', 'card pair');
    card.setAttribute('aria-label', 'Pair with Pebble');
    card.append(el('h2', '', m.pair.heading), el('p', 'note', m.pair.note));
    if (m.pair.kind === 'code') {
      const form = el('form', 'code-form');
      const label = el('label', 'code-label', 'Pairing code');
      const input = el('input', 'code');
      input.setAttribute('inputmode', 'numeric');
      input.setAttribute('autocomplete', 'one-time-code');
      input.setAttribute('maxlength', '6');
      input.setAttribute('pattern', '[0-9]{6}');
      input.setAttribute('aria-describedby', 'pair-hint');
      label.append(input);
      form.append(label, button('Pair', 'pair-code', null, 'primary', 'submit'));
      form.addEventListener('submit', event => { event?.preventDefault?.(); onAction('pair-code', null, String(input.value || '').trim()); });
      const hint = el('p', 'hint', m.pair.hint);
      hint.id = 'pair-hint';
      card.append(form, hint);
    } else {
      card.append(button('Pair with Pebble', 'pair-start', null, 'primary'));
    }
    root.append(card);
  }
  if (m.tab) {
    const card = el('section', `card ${m.tab.kind}`);
    card.setAttribute('aria-label', 'This tab');
    card.append(el('h2', '', m.tab.heading));
    if (m.tab.origin) card.append(el('p', 'site', m.tab.origin));
    if (m.tab.title) {
      const title = el('p', 'page-title', m.tab.title);
      title.title = 'Page title, written by the page';
      card.append(title);
    }
    if (m.tab.line) card.append(el('p', 'line', m.tab.line));
    if (m.tab.scope) card.append(el('p', 'note', m.tab.scope));
    if (m.tab.caution) card.append(el('p', 'caution', m.tab.caution));
    if (m.tab.note) card.append(el('p', 'note', m.tab.note));
    for (const action of m.tab.actions) {
      const row = el('div', 'action');
      row.append(button(action.label, action.id, null, action.primary ? 'primary' : 'secondary'));
      if (action.hint) row.append(el('p', 'hint', action.hint));
      card.append(row);
    }
    root.append(card);
  }
  if (m.others.length) {
    const list = el('ul', 'others');
    list.setAttribute('aria-label', 'Other shared tabs');
    for (const other of m.others) {
      const item = el('li');
      const words = el('div', 'other-words');
      words.append(el('span', 'other-label', other.label), el('span', 'note', other.line));
      item.append(words, button('Stop sharing', 'unshare', other.tabId, 'quiet'));
      list.append(item);
    }
    root.append(el('h3', '', 'Other shared tabs'), list);
  }
  if (m.stop) {
    const stop = el('div', 'stop');
    stop.append(button('Stop Pebble', 'stop', null, 'danger'),
      el('p', 'hint', 'Ends every shared tab and anything Pebble is doing in Chrome.'));
    root.append(stop);
  }
  const footer = el('div', 'footer');
  footer.append(el('p', '', 'Pebble only sees tabs you share. Page text is never treated as instructions.'));
  if (m.unpair) footer.append(button('Unpair this Chrome', 'unpair', null, 'quiet'));
  root.append(footer);
}
