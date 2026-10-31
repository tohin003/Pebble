// Pairing and sealing for Pebble for Chrome (Phase 22), with WebCrypto only.
//
// The same bytes as backend/pebble/browser_bridge.py (both test suites pin shared test vectors):
//   codeKey   = SHA-256('pebble-browser/1|pair|' + code)   (Phase 43: a fixed label, not the link protocol, so pairing
//                                                          and every existing pairing key are unchanged by protocol 2)
//   proof     = HMAC(codeKey, 'ext|' + extNonce + '|' + linkNonce)            the person typed Pebble's code
//   wrapped   = key XOR HMAC(codeKey, 'wrap|' + extNonce + '|' + linkNonce)   the pairing key, carried once
//   confirm   = HMAC(key, 'confirm|…'), serviceProof = HMAC(key, 'svc|…')     each side proves it holds the key
//   session   = HMAC(key, 'session|' + extNonce + '|' + linkNonce)            one key per link
//   sealed    = {type:'sealed', seq, body, mac: HMAC(session, direction + '|' + seq + '|' + body)}
// direction is 'e2s' (extension to service) or 's2e'. Sequence numbers start at 1 per direction and per link.

export const PAIRING_LABEL = 'pebble-browser/1';
const encoder = new TextEncoder();

export function hex(bytes) {
  return [...bytes].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

export function unhex(text) {
  if (typeof text !== 'string' || text.length % 2 || !/^[0-9a-f]*$/.test(text)) throw new Error('not hex');
  const out = new Uint8Array(text.length / 2);
  for (let index = 0; index < out.length; index++) out[index] = parseInt(text.slice(index * 2, index * 2 + 2), 16);
  return out;
}

export function randomHex(size, source = globalThis.crypto) {
  const bytes = new Uint8Array(size);
  source.getRandomValues(bytes);
  return hex(bytes);
}

export function sameHex(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let difference = 0;
  for (let index = 0; index < a.length; index++) difference |= a.charCodeAt(index) ^ b.charCodeAt(index);
  return difference === 0;
}

export async function mac(keyBytes, text) {
  const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await crypto.subtle.sign('HMAC', key, encoder.encode(text)));
}

export async function codeKey(code) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(`${PAIRING_LABEL}|pair|${code}`)));
}

export async function pairProof(code, extNonce, linkNonce) {
  return hex(await mac(await codeKey(code), `ext|${extNonce}|${linkNonce}`));
}

export async function unwrapKey(wrapped, code, extNonce, linkNonce) {
  const mask = await mac(await codeKey(code), `wrap|${extNonce}|${linkNonce}`);
  const bytes = unhex(wrapped);
  if (bytes.length !== 32) throw new Error('bad key');
  return bytes.map((byte, index) => byte ^ mask[index]);
}

export async function confirmTag(key, extNonce, linkNonce) {
  return hex(await mac(key, `confirm|${extNonce}|${linkNonce}`));
}

export async function serviceProof(key, extNonce, linkNonce) {
  return hex(await mac(key, `svc|${extNonce}|${linkNonce}`));
}

export async function sessionKey(key, extNonce, linkNonce) {
  return mac(key, `session|${extNonce}|${linkNonce}`);
}

export async function seal(session, direction, seq, message) {
  const body = JSON.stringify(message);
  return { type: 'sealed', seq, body, mac: hex(await mac(session, `${direction}|${seq}|${body}`)) };
}

// The message inside a sealed frame with exactly the expected sequence number; throws otherwise.
export async function unseal(session, direction, seq, frame) {
  if (!frame || typeof frame !== 'object' || frame.type !== 'sealed' || Object.keys(frame).sort().join() !== 'body,mac,seq,type') {
    throw new Error('unsealed');
  }
  if (frame.seq !== seq) throw new Error('sequence');
  if (typeof frame.body !== 'string' || typeof frame.mac !== 'string') throw new Error('sealed');
  if (!sameHex(frame.mac, hex(await mac(session, `${direction}|${seq}|${frame.body}`)))) throw new Error('mac');
  const message = JSON.parse(frame.body);
  if (!message || typeof message !== 'object' || Array.isArray(message)) throw new Error('not an object');
  return message;
}
