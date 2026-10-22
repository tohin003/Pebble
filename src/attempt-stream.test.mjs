import assert from 'node:assert/strict';
import test from 'node:test';
import { emptyStream, replayStream } from './attempt-stream.mjs';
const begin = (sequence, id, attempt = 1) => ({ sequence, attempt_id: id, kind: 'begin', attempt });
const delta = (sequence, id, text) => ({ sequence, attempt_id: id, kind: 'delta', text });
const settle = (sequence, id, state) => ({ sequence, attempt_id: id, kind: 'settle', state });
test('replay discards a disconnected attempt and never concatenates it into its retry', () => {
  const first = replayStream(emptyStream(), [begin(1, 'a'), delta(2, 'a', 'uncommitted')]);
  assert.equal(first.text, 'uncommitted');
  const retry = replayStream(first, [settle(3, 'a', 'discarded'), begin(4, 'b', 2), delta(5, 'b', 'answer')]);
  assert.equal(retry.text, 'answer');
  assert.equal(replayStream(retry, [settle(6, 'b', 'committed')]).text, '');
});
test('duplicate transport delivery is idempotent; gaps and foreign attempt owners fail', () => {
  const events = [begin(1, 'a'), delta(2, 'a', 'visible')];
  const state = replayStream(emptyStream(), events);
  assert.deepEqual(replayStream(state, events), state);
  assert.throws(() => replayStream(state, [delta(4, 'a', 'missing')]));
  assert.throws(() => replayStream(state, [delta(3, 'foreign', 'poison')]));
  assert.throws(() => replayStream(state, [begin(3, 'b')]));
});
test('presentation stays bounded and marks partial text while advancing all observed events', () => {
  const state = replayStream(emptyStream(), [begin(1, 'a'), delta(2, 'a', 'x'.repeat(16001))]);
  assert.equal(state.text.length, 16000); assert.equal(state.partial, true); assert.equal(state.cursor, 2);
});
test('the last few committed attempts keep their text for a later withdrawal; discarded and interrupted text does not', () => {
  // Phase 20 H20: one page may settle a draft and run the next attempt's whole lifecycle.
  let state = replayStream(emptyStream(), [begin(1, 'a'), delta(2, 'a', 'draft'), settle(3, 'a', 'committed'),
    begin(4, 'b'), delta(5, 'b', 'lost'), settle(6, 'b', 'interrupted'), begin(7, 'c'), delta(8, 'c', 'reply'), settle(9, 'c', 'committed')]);
  assert.deepEqual(state.settled, [{ attemptId: 'a', text: 'draft', partial: false }, { attemptId: 'c', text: 'reply', partial: false }]);
  assert.equal(state.text, '');
  for (let index = 0; index < 5; index++) {
    const at = state.cursor;
    state = replayStream(state, [begin(at + 1, 'n' + index), settle(at + 2, 'n' + index, 'committed')]);
  }
  assert.deepEqual(state.settled.map(item => item.attemptId), ['n1', 'n2', 'n3', 'n4'], 'bounded');
});
test('settled text cannot be revived by late callbacks or reused IDs', () => {
  const state = replayStream(emptyStream(), [begin(1, 'a'), delta(2, 'a', '<script>untrusted</script>'), settle(3, 'a', 'interrupted')]);
  assert.equal(state.text, '');
  assert.throws(() => replayStream(state, [delta(4, 'a', 'late')]));
});
