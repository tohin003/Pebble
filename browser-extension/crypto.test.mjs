// Pebble for Chrome, Phase 22: pairing and sealing bytes match backend/pebble/browser_bridge.py exactly.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { codeKey, confirmTag, hex, pairProof, randomHex, sameHex, seal, serviceProof, sessionKey, unhex, unseal,
  unwrapKey } from './lib/crypto.mjs';

// The same vectors as test_browser_bridge_22.py: key bytes 0..31, nonces 'aa'*32 and 'bb'*32, code '123456'.
const KEY = Uint8Array.from({ length: 32 }, (_, index) => index);
const EXT = 'aa'.repeat(32), LINK = 'bb'.repeat(32), CODE = '123456';
const VECTORS = {
  proof: 'bc0e77f1feb10151779f6790baccbe3e5ecd16626102373cb98bce9223774789',
  wrapped: 'b7eb874fbdd887d0696c44b2c3baf287f4940742f4268313020bec10ee785b14',
  confirm: '10bb9f0fd9e315ebd00cd109a16bb650f561aa04b1323223483d8615ee47f265',
  service: 'a066cbf181e59fc4394a4420cb14ab58a9917a72e5e5cdceed13862093f81f2d',
  session: 'abb7cf94bf12fb78ff20cab565e26af5cd06f1371c88145fb3b5618e5df223ef',
  mac: 'a6233690299d7ff3dc6c4443f0eb340b49e1b27eee8ad6160c1eda11e4b00773',
};

test('pairing and session bytes equal the backend vectors', async () => {
  assert.equal(await pairProof(CODE, EXT, LINK), VECTORS.proof);
  assert.deepEqual(await unwrapKey(VECTORS.wrapped, CODE, EXT, LINK), KEY, 'the code unwraps the key the service sent');
  assert.notDeepEqual(await unwrapKey(VECTORS.wrapped, '654321', EXT, LINK), KEY, 'a wrong code yields a different key');
  assert.equal(await confirmTag(KEY, EXT, LINK), VECTORS.confirm);
  assert.equal(await serviceProof(KEY, EXT, LINK), VECTORS.service);
  const session = await sessionKey(KEY, EXT, LINK);
  assert.equal(hex(session), VECTORS.session);
  const frame = await seal(session, 'e2s', 1, { type: 'shares', shares: [] });
  assert.deepEqual(frame, { type: 'sealed', seq: 1, body: '{"type":"shares","shares":[]}', mac: VECTORS.mac });
  assert.equal(hex(await codeKey(CODE)).length, 64);
});

test('a sealed frame opens only with its key, direction, sequence and exact body', async () => {
  const session = await sessionKey(KEY, EXT, LINK);
  const frame = await seal(session, 's2e', 3, { type: 'call', id: 9, text: 'ünï' });
  assert.deepEqual(await unseal(session, 's2e', 3, frame), { type: 'call', id: 9, text: 'ünï' });
  const other = await sessionKey(Uint8Array.from({ length: 32 }, () => 7), EXT, LINK);
  for (const [bad, seq, key, direction] of [[{ ...frame, mac: '0'.repeat(64) }, 3, session, 's2e'], [frame, 4, session, 's2e'],
    [frame, 3, other, 's2e'], [frame, 3, session, 'e2s'], [{ ...frame, body: frame.body + ' ' }, 3, session, 's2e'],
    [{ ...frame, extra: 1 }, 3, session, 's2e'], [{ type: 'call', id: 1 }, 3, session, 's2e']]) {
    await assert.rejects(unseal(key, direction, seq, bad));
  }
});

test('hex helpers and nonces', () => {
  assert.deepEqual(unhex('00ff10'), Uint8Array.from([0, 255, 16]));
  assert.throws(() => unhex('0g'));
  assert.throws(() => unhex('abc'));
  assert.equal(randomHex(32).length, 64);
  assert.notEqual(randomHex(32), randomHex(32));
  assert.equal(sameHex('ab', 'ab'), true);
  assert.equal(sameHex('ab', 'ac'), false);
  assert.equal(sameHex('ab', 'abc'), false);
  assert.equal(sameHex(undefined, 'ab'), false);
});
