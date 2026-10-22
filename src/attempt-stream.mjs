// Visible model text only. This reducer cannot publish messages or invoke tools.
// Phase 20 H20: the text of the last few committed attempts is kept (bounded) so the panel can keep showing one the
// host later withdraws; one page may hold its settle and the next attempt's whole lifecycle.
const SETTLED_KEPT = 4;
export function emptyStream() {
  return { cursor: 0, attemptId: null, attempt: 0, state: 'idle', text: '', partial: false, settled: [] };
}
export function replayStream(previous, events) {
  let next = { ...previous };
  for (const event of events) {
    if (!Number.isSafeInteger(event.sequence) || event.sequence < 1) throw new Error('Invalid stream sequence');
    if (event.sequence <= next.cursor) continue;
    if (event.sequence !== next.cursor + 1) throw new Error('Stream replay has a gap');
    if (event.kind === 'begin') {
      if (next.state === 'open' || typeof event.attempt_id !== 'string') throw new Error('Overlapping stream attempts');
      next = { cursor: next.cursor, attemptId: event.attempt_id, attempt: event.attempt, state: 'open', text: '', partial: false,
        settled: next.settled || [] };
    } else {
      if (event.attempt_id !== next.attemptId || next.state !== 'open') throw new Error('Stream event has a stale owner');
      if (event.kind === 'delta') {
        if (typeof event.text !== 'string') throw new Error('Invalid stream text');
        const text = next.text + event.text;
        next.partial ||= text.length > 16000;
        next.text = text.slice(-16000);
      } else if (event.kind === 'settle' && ['committed', 'discarded', 'interrupted'].includes(event.state)) {
        if (event.state === 'committed') {
          next.settled = [...(next.settled || []), { attemptId: next.attemptId, text: next.text, partial: next.partial }].slice(-SETTLED_KEPT);
        }
        next.state = event.state;
        next.text = '';
      } else throw new Error('Invalid stream lifecycle');
    }
    next.cursor = event.sequence;
  }
  return next;
}
