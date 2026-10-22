"""Authenticated production evidence for readiness contract v2 (docs/HARNESS_TRUST_CHANNEL.md).

An agent cannot authenticate its own evidence. The contract v2 production gates
therefore accept only envelopes signed by people whose public keys are pinned in
backend/pebble/trust_roots.py. A person signs the exact canonical payload bytes
with `/usr/bin/ssh-keygen -Y sign -n pebble-attestation@1`. This module parses
only SSHSIG with Ed25519 and SHA-512 and verifies those exact bytes with
`cryptography`; it never re-serializes them. Validation follows D20 §6.4 in
order, and each refusal has exactly one code. Accepted envelopes are stored by
SHA256 in an append-only ledger (O_EXCL, never replaced or deleted), which also
detects replayed nonces. The signed release manifest `pebble.release/1` uses
the same envelope. Nothing here holds, generates or asks for a private key, and
nothing reads the environment, the network or a credential store.
"""

import base64
import binascii
import hashlib
import json
import os
import platform as host_platform
import re
import secrets
import stat
import struct
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import release_readiness as rr
from . import trust_roots
from .trust_roots import key_id

FORMAT = 'pebble.attestation/1'
CONTRACT = 'pebble.release-readiness/2'
RELEASE_FORMAT = 'pebble.release/1'
LEDGER_FORMAT = 'pebble.attestation-ledger/1'
NAMESPACE = 'pebble-attestation@1'
KEY_TYPE, HASH_ALGORITHM = 'ssh-ed25519', 'sha512'
# The review key that opts a review into contract v2; reviews without it stay contract v1.
REVIEW_CONTRACT_KEY = 'readiness_contract'

# D20 §6.6: the role whose key may attest each gate. Two production gates are never attestable:
# other-platform containment needs evidence produced on those platforms (or a scope agreement), and
# macOS distribution signing keeps the native checks that release_readiness runs itself.
GATE_ROLES = {'installed_application': 'operator', 'independent_human_audit': 'auditor',
              'signed_updates': 'release', 'real_model_quality': 'operator'}
UNATTESTABLE = ('other_platform_containment', 'macos_distribution_signing')
ROLES = ('operator', 'auditor', 'release')
# Version 2: the pass criteria changed from "never" (v1) to a verified attestation.
GATE_VERSIONS = dict.fromkeys(GATE_ROLES, 2)
SEVERITIES = ('critical', 'high', 'medium', 'low', 'info')
SERIOUS = frozenset({'critical', 'high'})
PASSED_REASON = 'Verified by an attestation from a pinned trust root, bound to the subject this validator computed.'

# Every refusal code, in D20 §6.4 order; result_failed and serious_finding are step 11.
REFUSALS = ('malformed', 'format', 'downgrade', 'unknown_key', 'revoked', 'key_expired', 'role_not_allowed',
            'bad_signature', 'non_canonical', 'gate_version', 'foreign_subject', 'stale', 'future', 'expired',
            'lifetime', 'replay', 'evidence_mismatch', 'result_failed', 'serious_finding')
# The codes verify_release_manifest returns, which the install tool surfaces.
RELEASE_REFUSALS = ('malformed', 'format', 'downgrade', 'unknown_key', 'revoked', 'key_expired',
                    'role_not_allowed', 'bad_signature', 'non_canonical', 'foreign_subject', 'future', 'expired',
                    'lifetime', 'previous_mismatch', 'rollback_mismatch', 'version_not_higher', 'data_epoch')

SUBJECT_KEYS = ('source_snapshot_sha256', 'candidate_executable_sha256', 'candidate_bundle_sha256',
                'app_bundle_sha256', 'platform')
ENVELOPE_KEYS = frozenset({'format', 'key_id', 'payload', 'signature'})
PAYLOAD_KEYS = frozenset({'contract', 'gate', 'gate_version', 'role', 'subject', 'result', 'findings',
                          'evidence_sha256', 'issued_at', 'not_after', 'nonce'})
RELEASE_KEYS = frozenset({'format', 'version', 'build', 'app_bundle_sha256', 'backend_executable_sha256',
                          'min_data_epoch', 'data_epoch', 'previous', 'rollback', 'issued_at', 'not_after'})
FINDING_KEYS = frozenset({'code', 'severity', 'count'})
ROOT_KEYS = frozenset({'alg', 'public_key', 'roles', 'gates', 'not_before', 'not_after', 'label'})

MAX_ENVELOPE_BYTES, MAX_PAYLOAD_BYTES, MAX_SIGNATURE_CHARS = 65_536, 16_384, 4096
MAX_FINDINGS, MAX_FINDING_COUNT, MAX_ENVELOPES, MAX_LEDGER_RECORD = 64, 1_000_000, 64, 8192
MAX_FUTURE, MAX_LIFETIME = timedelta(seconds=300), timedelta(days=30)
MAX_SAFE_INTEGER = 2 ** 53 - 1

HEX64 = re.compile(r'[0-9a-f]{64}')
KEY_ID = re.compile(r'[0-9a-f]{16}')
NONCE = re.compile(r'[0-9a-f]{32}')
CODE = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}')
PLATFORM_LABEL = re.compile(r'[a-z0-9][a-z0-9_.-]{0,31}')
VERSION = re.compile(r'(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})')
LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _.@-]{0,79}')
REASON = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _.,:@-]{0,119}')
# ASCII digits only: \d would also match other scripts' digits and admit non-canonical times.
STAMP = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z')
CONTRACT_NUMBER = re.compile(r'pebble\.release-readiness/([0-9]{1,6})')
RELEASE_NUMBER = re.compile(r'pebble\.release/([0-9]{1,6})')
ARMOR_BEGIN, ARMOR_END = '-----BEGIN SSH SIGNATURE-----', '-----END SSH SIGNATURE-----'
ARMOR_LINE = re.compile(r'[A-Za-z0-9+/=]{1,76}')

_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)


class SignatureError(ValueError):
    """An SSHSIG that is malformed, of another algorithm or namespace, or does not verify."""


class LedgerError(Exception):
    """The ledger cannot be read or written safely; nothing was recorded."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class _Refusal(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---- Canonical JSON, times and encodings ------------------------------------------------------


def canonical(value):
    """Canonical JSON bytes: UTF-8, sorted keys, compact, integers only, within the v1 depth and node bounds."""
    nodes, stack = 0, [(value, 1)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > rr.MAX_JSON_DEPTH or nodes > rr.MAX_JSON_NODES:
            raise ValueError('too_complex')
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ValueError('non_string_key')
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, bool) or item is None or isinstance(item, str):
            continue
        elif isinstance(item, int):
            if abs(item) > MAX_SAFE_INTEGER:
                raise ValueError('integer_out_of_range')
        else:
            raise ValueError('not_integer_or_json_type')
    try:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                          allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValueError('unencodable') from None


def parse_stamp(value):
    """A strict UTC `YYYY-MM-DDTHH:MM:SSZ` timestamp, else None."""
    match = STAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    try:
        return datetime(*(int(part) for part in match.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None


def stamp(moment):
    """The canonical payload form of an aware datetime, truncated to whole seconds."""
    _aware(moment, 'moment')
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _aware(moment, what):
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f'{what} must be a timezone-aware datetime.')
    return moment


def _b64decode(text):
    """Strict standard base64 whose canonical re-encoding equals the text, else None."""
    if not isinstance(text, str) or not text or len(text) > 2 * MAX_ENVELOPE_BYTES:
        return None
    try:
        raw = base64.b64decode(text.encode('ascii'), validate=True)
    except (ValueError, binascii.Error, UnicodeEncodeError):
        return None
    return raw if base64.b64encode(raw).decode('ascii') == text else None


def _hex(value):
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def _integer(value, low=0, high=MAX_SAFE_INTEGER):
    return type(value) is int and low <= value <= high


def new_nonce():
    """A fresh 128-bit nonce in lowercase hex."""
    return secrets.token_hex(16)


def subject_platform():
    """The platform label bound into subjects: sys.platform and the machine, e.g. darwin-arm64."""
    label = f'{sys.platform}-{host_platform.machine()}'.lower()
    return label if PLATFORM_LABEL.fullmatch(label) else 'unknown'


def app_bundle_digest(app):
    """The release_readiness bundle digest of an .app tree (bundle_entries plus bundle_digest, as T1's
    app-manifest records it), or None when the walk reports any problem."""
    with rr.Root(os.fspath(app), 'app_bundle') as root:
        try:
            entries, _links, problems = rr.bundle_entries(root)
        except rr.Unsafe:
            return None
    return rr.bundle_digest(entries) if entries and not problems else None


def pinned_trust():
    """The production roots and revocations; the only source of trust outside tests."""
    return trust_roots.ROOTS, trust_roots.REVOKED


def pinned_keys(roots, revoked, *, gate, now):
    """Sorted key_ids that may attest gate at now: pinned, unrevoked, in their window and holding the
    gate and its role. Empty means the gate has no channel (the install tool's 'release key pinned?')."""
    role = GATE_ROLES.get(gate)
    _aware(now, 'now')
    loaded, revocations = load_roots(roots, revoked)
    return sorted(kid for kid, root in loaded.items()
                  if role is not None and kid not in revocations and role in root.roles and gate in root.gates
                  and root.not_before <= now <= root.not_after)


# ---- SSH wire format and SSHSIG ---------------------------------------------------------------


class _Wire:
    def __init__(self, data):
        self.data, self.at = data, 0

    def take(self, size):
        if size < 0 or self.at + size > len(self.data):
            raise SignatureError('truncated')
        chunk = self.data[self.at:self.at + size]
        self.at += size
        return chunk

    def uint32(self):
        return struct.unpack('>I', self.take(4))[0]

    def string(self, limit=8192):
        size = self.uint32()
        if size > limit:
            raise SignatureError('oversized_field')
        return self.take(size)

    def end(self):
        if self.at != len(self.data):
            raise SignatureError('trailing_data')


def _ssh_string(data):
    return struct.pack('>I', len(data)) + data


def public_key_raw(blob_base64):
    """The raw 32-byte key of a base64 OpenSSH ssh-ed25519 wire blob (the .pub line's second field)."""
    blob = _b64decode(blob_base64)
    if blob is None:
        raise ValueError('public key is not canonical base64.')
    try:
        wire = _Wire(blob)
        if wire.string(64) != KEY_TYPE.encode():
            raise ValueError('only ssh-ed25519 public keys are accepted.')
        raw = wire.string(64)
        wire.end()
    except SignatureError:
        raise ValueError('public key blob is malformed.') from None
    if len(raw) != 32:
        raise ValueError('an Ed25519 public key has 32 bytes.')
    return raw


def public_key_line(line):
    """(raw key, wire blob base64) from one `ssh-ed25519 <base64> [comment]` public key line."""
    if not isinstance(line, str) or len(line) > 2048 or '\n' in line.rstrip('\n') or 'PRIVATE KEY' in line:
        raise ValueError('expected one ssh-ed25519 public key line.')
    fields = line.strip().split()
    if len(fields) < 2 or fields[0] != KEY_TYPE:
        raise ValueError('expected one ssh-ed25519 public key line.')
    return public_key_raw(fields[1]), fields[1]


@dataclass(frozen=True)
class SshSignature:
    """A parsed SSHSIG blob. Only ssh-ed25519 with sha512 in the Pebble namespace ever verifies."""
    public_key_type: bytes
    public_key: bytes
    namespace: bytes
    reserved: bytes
    hash_algorithm: bytes
    signature_type: bytes
    signature: bytes


def parse_sshsig(text):
    """Parse an armored SSHSIG (OpenSSH PROTOCOL.sshsig) strictly: one armor block, canonical
    base64, version 1 and no trailing bytes. Keys of other types are kept as an opaque blob."""
    if not isinstance(text, str) or len(text) > MAX_SIGNATURE_CHARS:
        raise SignatureError('armor')
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    if len(lines) < 3 or lines[0] != ARMOR_BEGIN or lines[-1] != ARMOR_END:
        raise SignatureError('armor')
    body = lines[1:-1]
    if not all(ARMOR_LINE.fullmatch(line) for line in body):
        raise SignatureError('armor')
    blob = _b64decode(''.join(body))
    if blob is None:
        raise SignatureError('armor')
    wire = _Wire(blob)
    if wire.take(6) != b'SSHSIG' or wire.uint32() != 1:
        raise SignatureError('magic')
    key_blob, namespace, reserved = wire.string(), wire.string(256), wire.string(256)
    hash_algorithm, signature_blob = wire.string(64), wire.string()
    wire.end()
    key = _Wire(key_blob)
    key_type = key.string(64)
    if key_type == KEY_TYPE.encode():
        raw = key.string(64)
        key.end()
    else:
        raw = key_blob
    signature = _Wire(signature_blob)
    signature_type, signature_raw = signature.string(64), signature.string(1024)
    signature.end()
    return SshSignature(key_type, raw, namespace, reserved, hash_algorithm, signature_type, signature_raw)


def verify_sshsig(text, message, raw_public_key):
    """Verify an armored SSHSIG over the exact message bytes with one Ed25519 public key.

    The embedded key must equal raw_public_key, the namespace must be pebble-attestation@1 and the
    hash SHA-512. RSA, ECDSA, other namespaces and other hashes raise SignatureError."""
    sig = parse_sshsig(text)
    if (sig.public_key_type != KEY_TYPE.encode() or sig.signature_type != KEY_TYPE.encode()
            or len(sig.public_key) != 32 or len(sig.signature) != 64):
        raise SignatureError('algorithm')
    if sig.namespace != NAMESPACE.encode():
        raise SignatureError('namespace')
    if sig.reserved != b'':
        raise SignatureError('reserved')
    if sig.hash_algorithm != HASH_ALGORITHM.encode():
        raise SignatureError('hash_algorithm')
    if not isinstance(raw_public_key, (bytes, bytearray)) or sig.public_key != bytes(raw_public_key):
        raise SignatureError('key_mismatch')
    signed = (b'SSHSIG' + _ssh_string(sig.namespace) + _ssh_string(sig.reserved)
              + _ssh_string(sig.hash_algorithm) + _ssh_string(hashlib.sha512(bytes(message)).digest()))
    try:
        Ed25519PublicKey.from_public_bytes(sig.public_key).verify(sig.signature, signed)
    except (InvalidSignature, ValueError):
        raise SignatureError('signature') from None
    return sig


# ---- Trust roots ------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustRoot:
    key_id: str
    raw: bytes
    roles: frozenset
    gates: frozenset
    not_before: datetime
    not_after: datetime
    label: str


def _names(value, allowed, what):
    if not isinstance(value, (list, tuple)) or not value or len(set(value)) != len(value):
        raise ValueError(f'{what} must be a non-empty list without duplicates.')
    if not all(isinstance(item, str) and item in allowed for item in value):
        raise ValueError(f'{what} may only name {", ".join(allowed)}.')
    return frozenset(value)


def load_roots(roots, revoked):
    """Validate pinned roots and revocations. A malformed pin raises ValueError; it is never skipped."""
    if not isinstance(roots, dict) or not isinstance(revoked, dict):
        raise ValueError('roots and revoked must be dicts.')
    loaded = {}
    for kid, entry in roots.items():
        where = f'trust root {kid!r}'
        if not isinstance(kid, str) or not KEY_ID.fullmatch(kid):
            raise ValueError(f'{where}: key_id must be 16 lowercase hex digits.')
        if not isinstance(entry, dict) or set(entry) != ROOT_KEYS:
            raise ValueError(f'{where}: needs exactly {", ".join(sorted(ROOT_KEYS))}.')
        if entry['alg'] != KEY_TYPE:
            raise ValueError(f'{where}: only ssh-ed25519 roots are accepted.')
        raw = public_key_raw(entry['public_key'])
        if key_id(raw) != kid:
            raise ValueError(f'{where}: key_id does not match its public key.')
        roles = _names(entry['roles'], ROLES, f'{where} roles')
        gates = _names(entry['gates'], tuple(GATE_ROLES), f'{where} gates')
        if 'auditor' in roles and len(roles) > 1:
            raise ValueError(f'{where}: an auditor key holds no other role.')
        if any(GATE_ROLES[gate] not in roles for gate in gates):
            raise ValueError(f'{where}: every gate must belong to one of its roles.')
        not_before, not_after = parse_stamp(entry['not_before']), parse_stamp(entry['not_after'])
        if not_before is None or not_after is None or not_after <= not_before:
            raise ValueError(f'{where}: needs a UTC not_before earlier than not_after.')
        if not isinstance(entry['label'], str) or not LABEL.fullmatch(entry['label']):
            raise ValueError(f'{where}: label must be short printable ASCII.')
        loaded[kid] = TrustRoot(kid, raw, roles, gates, not_before, not_after, entry['label'])
    revocations = {}
    for kid, reason in revoked.items():
        if not isinstance(kid, str) or not KEY_ID.fullmatch(kid) or not isinstance(reason, str) \
                or not REASON.fullmatch(reason):
            raise ValueError(f'revocation {kid!r}: needs a key_id and a short reason.')
        revocations[kid] = reason
    return loaded, revocations


def root_entry(public_key_text, *, roles, gates, not_before, not_after, label):
    """(key_id, ROOTS entry) for one public key line, validated exactly as load_roots will read it."""
    raw, blob = public_key_line(public_key_text)
    kid = key_id(raw)
    entry = {'alg': KEY_TYPE, 'public_key': blob, 'roles': tuple(roles), 'gates': tuple(gates),
             'not_before': not_before, 'not_after': not_after, 'label': label}
    load_roots({kid: entry}, {})
    return kid, entry


# ---- Typed results ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """The outcome of one attestation envelope. Only status 'accepted' counts toward a gate.

    key_id, gate, result and findings are what the envelope claims; on a refusal they are reported
    for diagnosis only and are not authenticated. recorded is 'new', 'existing' or 'dry_run'."""
    status: str
    code: Optional[str] = None
    key_id: Optional[str] = None
    gate: Optional[str] = None
    result: Optional[str] = None
    findings: tuple = ()
    envelope_sha256: Optional[str] = None
    payload_sha256: Optional[str] = None
    recorded: Optional[str] = None

    @property
    def accepted(self):
        return self.status == 'accepted'

    def as_dict(self):
        return {'status': self.status, 'code': self.code, 'key_id': self.key_id, 'gate': self.gate,
                'result': self.result,
                'findings': [{'code': code, 'severity': severity, 'count': count}
                             for code, severity, count in self.findings],
                'envelope_sha256': self.envelope_sha256, 'payload_sha256': self.payload_sha256,
                'recorded': self.recorded}


@dataclass(frozen=True)
class ReleaseVerdict:
    """The outcome of one signed pebble.release/1 manifest; manifest is set only when accepted."""
    status: str
    code: Optional[str] = None
    key_id: Optional[str] = None
    manifest: Optional[dict] = None
    envelope_sha256: Optional[str] = None
    payload_sha256: Optional[str] = None

    @property
    def accepted(self):
        return self.status == 'accepted'

    def as_dict(self):
        return {'status': self.status, 'code': self.code, 'key_id': self.key_id,
                'manifest': None if self.manifest is None else dict(self.manifest),
                'envelope_sha256': self.envelope_sha256, 'payload_sha256': self.payload_sha256}


# ---- Shared validation steps ------------------------------------------------------------------


def _open(envelope):
    """Step 1: both JSON documents, strict and bounded by the v1 parser."""
    if not isinstance(envelope, (bytes, bytearray)) or len(envelope) > MAX_ENVELOPE_BYTES:
        raise _Refusal('malformed')
    try:
        doc = rr.parse_json(bytes(envelope), 'envelope')
    except rr.EvidenceError:
        raise _Refusal('malformed') from None
    payload = _b64decode(doc.get('payload')) if isinstance(doc, dict) else None
    if payload is None or len(payload) > MAX_PAYLOAD_BYTES:
        raise _Refusal('malformed')
    try:
        body = rr.parse_json(payload, 'payload')
    except rr.EvidenceError:
        raise _Refusal('malformed') from None
    return doc, payload, body


def _envelope_format(doc):
    if (set(doc) != ENVELOPE_KEYS or doc['format'] != FORMAT or not isinstance(doc['key_id'], str)
            or not KEY_ID.fullmatch(doc['key_id']) or not isinstance(doc['signature'], str)):
        raise _Refusal('format')


def _subject_valid(value, allow_missing=False):
    if not isinstance(value, dict) or set(value) != set(SUBJECT_KEYS):
        return False
    for name in SUBJECT_KEYS:
        item = value[name]
        if item is None and allow_missing:
            continue
        if name == 'platform':
            if not (isinstance(item, str) and PLATFORM_LABEL.fullmatch(item)):
                return False
        elif not _hex(item):
            return False
    return True


def _findings(value):
    if not isinstance(value, list) or len(value) > MAX_FINDINGS:
        return None
    out, seen = [], set()
    for item in value:
        if (not isinstance(item, dict) or set(item) != FINDING_KEYS or not isinstance(item['code'], str)
                or not CODE.fullmatch(item['code']) or item['severity'] not in SEVERITIES
                or not _integer(item['count'], 1, MAX_FINDING_COUNT) or item['code'] in seen):
            return None
        seen.add(item['code'])
        out.append((item['code'], item['severity'], item['count']))
    return tuple(out)


def _attestation_body(body):
    """Step 2 for an attestation payload: contract first (so v1 is a downgrade), then the exact schema."""
    if not isinstance(body, dict):
        raise _Refusal('format')
    contract = body.get('contract')
    number = CONTRACT_NUMBER.fullmatch(contract) if isinstance(contract, str) else None
    if contract == rr.CONTRACT or (number and int(number.group(1)) < 2):
        raise _Refusal('downgrade')
    if contract != CONTRACT or set(body) != PAYLOAD_KEYS:
        raise _Refusal('format')
    findings = _findings(body['findings'])
    issued, not_after = parse_stamp(body['issued_at']), parse_stamp(body['not_after'])
    if (body['gate'] not in rr.PRODUCTION_GATES or not _integer(body['gate_version'], 1, 1000)
            or not isinstance(body['role'], str) or not CODE.fullmatch(body['role'])
            or not _subject_valid(body['subject']) or body['result'] not in ('passed', 'failed')
            or findings is None or not _hex(body['evidence_sha256']) or not isinstance(body['nonce'], str)
            or not NONCE.fullmatch(body['nonce']) or issued is None or not_after is None):
        raise _Refusal('format')
    return issued, not_after, findings


def _release_body(body):
    """Step 2 for a release manifest."""
    if not isinstance(body, dict):
        raise _Refusal('format')
    name = body.get('format')
    number = RELEASE_NUMBER.fullmatch(name) if isinstance(name, str) else None
    if number and int(number.group(1)) < 1:
        raise _Refusal('downgrade')
    if name != RELEASE_FORMAT or set(body) != RELEASE_KEYS:
        raise _Refusal('format')
    issued, not_after = parse_stamp(body['issued_at']), parse_stamp(body['not_after'])
    if (not isinstance(body['version'], str) or not VERSION.fullmatch(body['version'])
            or not _integer(body['build'])
            or not all(_hex(body[key]) for key in ('app_bundle_sha256', 'backend_executable_sha256',
                                                   'previous', 'rollback'))
            or not _integer(body['min_data_epoch'], 0, 10_000) or not _integer(body['data_epoch'], 0, 10_000)
            or body['min_data_epoch'] > body['data_epoch'] or issued is None or not_after is None):
        raise _Refusal('format')
    return issued, not_after


def _key(kid, roots, revoked, now, issued):
    """Step 3. A key listed in REVOKED is reported revoked whether or not ROOTS still pins it."""
    if kid not in roots and kid not in revoked:
        raise _Refusal('unknown_key')
    if kid in revoked:
        raise _Refusal('revoked')
    root = roots[kid]
    if not (root.not_before <= now <= root.not_after and root.not_before <= issued <= root.not_after):
        raise _Refusal('key_expired')
    return root


def _signature(doc, payload, body, root):
    """Step 5: the exact payload bytes verify under the pinned key, and they are canonical."""
    try:
        verify_sshsig(doc['signature'], payload, root.raw)
    except SignatureError:
        raise _Refusal('bad_signature') from None
    try:
        same = canonical(body) == payload
    except ValueError:
        same = False
    if not same:
        raise _Refusal('non_canonical')


def _lifetime(issued, not_after, now):
    """Step 8 after the review check: not issued too far ahead, not expired, at most 30 days long."""
    if issued - now > MAX_FUTURE:
        raise _Refusal('future')
    if not_after <= now:
        raise _Refusal('expired')
    if not_after <= issued or not_after - issued > MAX_LIFETIME:
        raise _Refusal('lifetime')


def check_subject(subject):
    """A copy of a complete subject (SUBJECT_KEYS, four SHA256 values and a platform label), else ValueError."""
    if not _subject_valid(subject):
        raise ValueError('subject must hold exactly ' + ', '.join(SUBJECT_KEYS) + ', computed by the validator.')
    return {name: subject[name] for name in SUBJECT_KEYS}


def check_findings(value):
    """Payload findings as ((code, severity, count), ...), else ValueError."""
    findings = _findings(value)
    if findings is None:
        raise ValueError(f'findings must be at most {MAX_FINDINGS} unique {{code, severity, count}} entries.')
    return findings


# ---- Ledger -----------------------------------------------------------------------------------


class Ledger:
    """Append-only store of accepted envelopes (production: reports/harness-attestations/).

    `envelopes/<sha256>.json` holds each accepted envelope's exact bytes and `nonces/<nonce>.json`
    claims its nonce. Files are created with O_EXCL and mode 0444 and are never replaced or deleted.
    The nonce claim is written first, so an interrupted commit leaves only a claim that the same
    payload later completes. With record=False the ledger is read only: replay detection still
    reads it, and acceptances are remembered for this object's lifetime only."""

    ENVELOPES, NONCES = 'envelopes', 'nonces'

    def __init__(self, path, *, record=True):
        self.path, self.record = os.path.abspath(os.fspath(path)), bool(record)
        self._session = {}

    def _directory(self, create):
        try:
            info = os.lstat(self.path)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.makedirs(self.path, mode=0o755, exist_ok=True)
                info = os.lstat(self.path)
            except OSError:
                raise LedgerError('ledger_unwritable') from None
        except OSError:
            raise LedgerError('ledger_unreadable') from None
        if not stat.S_ISDIR(info.st_mode):
            raise LedgerError('ledger_not_directory')
        return self._open_directory(self.path, None, info)

    @staticmethod
    def _open_directory(name, parent, info):
        try:
            fd = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
                         **({} if parent is None else {'dir_fd': parent}))
        except OSError:
            raise LedgerError('ledger_unreadable') from None
        found = os.fstat(fd)
        if (found.st_dev, found.st_ino) != (info.st_dev, info.st_ino):
            os.close(fd)
            raise LedgerError('ledger_changed')
        return fd

    def _child(self, parent, name, create):
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(name, 0o755, dir_fd=parent)
            except FileExistsError:
                pass
            except OSError:
                raise LedgerError('ledger_unwritable') from None
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise LedgerError('ledger_not_directory')
        return self._open_directory(name, parent, info)

    @staticmethod
    def _read(parent, name, limit):
        try:
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError:
            raise LedgerError('ledger_unreadable') from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise LedgerError('ledger_not_regular_file')
            chunks, size = [], 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise LedgerError('ledger_record_too_large')
                chunks.append(chunk)
            return b''.join(chunks)
        finally:
            os.close(fd)

    @staticmethod
    def _create(parent, name, data):
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o444, dir_fd=parent)
        except FileExistsError:
            return False
        except OSError:
            raise LedgerError('ledger_unwritable') from None
        try:
            os.fchmod(fd, 0o444)  # read-only whatever the umask
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.fsync(parent)
        except OSError:
            pass
        return True

    def lookup(self, nonce):
        """The claim recorded for a nonce, {'corrupt': True} for an unreadable claim, else None."""
        if nonce in self._session:
            return self._session[nonce]
        root = self._directory(False)
        if root is None:
            return None
        try:
            nonces = self._child(root, self.NONCES, False)
            if nonces is None:
                return None
            try:
                data = self._read(nonces, nonce + '.json', MAX_LEDGER_RECORD)
            finally:
                os.close(nonces)
        finally:
            os.close(root)
        if data is None:
            return None
        try:
            claim = rr.parse_json(data, 'ledger')
        except rr.EvidenceError:
            return {'corrupt': True}
        return claim if isinstance(claim, dict) else {'corrupt': True}

    def commit(self, claim, envelope, envelope_sha256, previous):
        """Record an accepted envelope; returns 'new', 'existing' or 'dry_run'.

        A concurrent claim on the same nonce for another payload refuses with replay."""
        nonce = claim['nonce']
        if not self.record:
            self._session.setdefault(nonce, claim)
            return 'existing' if previous is not None else 'dry_run'
        root = self._directory(True)
        nonces = envelopes = None
        try:
            nonces = self._child(root, self.NONCES, True)
            envelopes = self._child(root, self.ENVELOPES, True)
            created = self._create(nonces, nonce + '.json', canonical(claim))
            if not created:
                try:
                    existing = rr.parse_json(self._read(nonces, nonce + '.json', MAX_LEDGER_RECORD) or b'', 'ledger')
                except rr.EvidenceError:
                    existing = {}
                if not isinstance(existing, dict) or existing.get('payload_sha256') != claim['payload_sha256']:
                    raise _Refusal('replay')
            name = envelope_sha256 + '.json'
            if not self._create(envelopes, name, envelope) and self._read(envelopes, name,
                                                                          MAX_ENVELOPE_BYTES) != envelope:
                raise LedgerError('ledger_conflict')
        finally:
            for fd in (envelopes, nonces, root):
                if fd is not None:
                    os.close(fd)
        self._session[nonce] = claim
        return 'new' if created else 'existing'

    def accepted_envelopes(self):
        """[(sha256, exact bytes)] of every stored envelope, for re-verification audits."""
        root = self._directory(False)
        if root is None:
            return []
        try:
            folder = self._child(root, self.ENVELOPES, False)
            if folder is None:
                return []
            try:
                with os.scandir(folder) as listing:
                    names = sorted(entry.name for entry in listing)
                found = []
                for name in names:
                    if name.endswith('.json') and HEX64.fullmatch(name[:-5]):
                        found.append((name[:-5], self._read(folder, name, MAX_ENVELOPE_BYTES)))
                return found
            finally:
                os.close(folder)
        finally:
            os.close(root)


def _ledger(value):
    if isinstance(value, Ledger):
        return value
    if isinstance(value, (str, os.PathLike)):
        return Ledger(value)
    raise ValueError('ledger must be a Ledger or a directory path; replay detection needs one.')


# ---- Attestation verification -----------------------------------------------------------------


def verify(envelope, *, roots, revoked, subject, gate, review_date, now, ledger, evidence=None):
    """Verify one pebble.attestation/1 envelope for one gate, in D20 §6.4 order.

    envelope and evidence are the exact bytes of the envelope file and of the evidence file it
    names by hash (missing evidence is evidence_mismatch). roots and revoked are trust_roots.ROOTS
    and REVOKED in production and injected in tests. subject is the dict the validator computed
    itself. review_date and now are aware datetimes. An accepted envelope is recorded in the ledger.
    Returns a Verdict; the refusal code is one of REFUSALS."""
    if gate not in rr.PRODUCTION_GATES:
        raise ValueError(f'{gate!r} is not a release readiness production gate.')
    expected = check_subject(subject)
    _aware(review_date, 'review_date')
    _aware(now, 'now')
    pinned, revocations = load_roots(roots, revoked)
    store = _ledger(ledger)
    data = bytes(envelope) if isinstance(envelope, (bytes, bytearray)) else None
    seen = {'envelope_sha256': None if data is None else rr.sha256(data)}
    try:
        recorded = _verify_steps(data, seen, pinned, revocations, expected, gate, review_date, now, store, evidence)
    except _Refusal as refusal:
        return Verdict('refused', refusal.code, **seen)
    return Verdict('accepted', None, recorded=recorded, **seen)


def _verify_steps(data, seen, roots, revoked, subject, gate, review_date, now, ledger, evidence):
    doc, payload, body = _open(data)
    seen['payload_sha256'] = rr.sha256(payload)
    _envelope_format(doc)
    issued, not_after, findings = _attestation_body(body)
    seen.update(key_id=doc['key_id'], gate=body['gate'], result=body['result'], findings=findings)
    root = _key(doc['key_id'], roots, revoked, now, issued)
    role = GATE_ROLES.get(gate)
    if (role is None or body['gate'] != gate or body['role'] != role or role not in root.roles
            or gate not in root.gates):
        raise _Refusal('role_not_allowed')
    _signature(doc, payload, body, root)
    if body['gate_version'] != GATE_VERSIONS[gate]:
        raise _Refusal('gate_version')
    if body['subject'] != subject:
        raise _Refusal('foreign_subject')
    if issued <= review_date:
        raise _Refusal('stale')
    _lifetime(issued, not_after, now)
    previous = ledger.lookup(body['nonce'])
    if previous is not None and previous.get('payload_sha256') != seen['payload_sha256']:
        raise _Refusal('replay')
    if not isinstance(evidence, (bytes, bytearray)) or rr.sha256(bytes(evidence)) != body['evidence_sha256']:
        raise _Refusal('evidence_mismatch')
    if gate == 'signed_updates':
        # The evidence must itself be a signed release manifest for this app and backend.
        release_state = {}
        try:
            _release_steps(bytes(evidence), release_state, roots, revoked, now, subject['app_bundle_sha256'],
                           subject['candidate_executable_sha256'])
        except _Refusal:
            raise _Refusal('evidence_mismatch') from None
    if body['result'] != 'passed':
        raise _Refusal('result_failed')
    if any(severity in SERIOUS for _code, severity, _count in findings):
        raise _Refusal('serious_finding')
    claim = {'format': LEDGER_FORMAT, 'nonce': body['nonce'], 'payload_sha256': seen['payload_sha256'],
             'envelope_sha256': seen['envelope_sha256'], 'gate': gate, 'key_id': doc['key_id'],
             'subject': subject, 'accepted_at': stamp(now)}
    return ledger.commit(claim, data, seen['envelope_sha256'], previous)


# ---- Release manifest -------------------------------------------------------------------------


def _release_steps(data, seen, roots, revoked, now, app_bundle_sha256, backend_executable_sha256):
    doc, payload, body = _open(data)
    seen['payload_sha256'] = rr.sha256(payload)
    _envelope_format(doc)
    issued, not_after = _release_body(body)
    seen['key_id'] = doc['key_id']
    root = _key(doc['key_id'], roots, revoked, now, issued)
    if 'release' not in root.roles or 'signed_updates' not in root.gates:
        raise _Refusal('role_not_allowed')
    _signature(doc, payload, body, root)
    if body['app_bundle_sha256'] != app_bundle_sha256 or body['backend_executable_sha256'] != backend_executable_sha256:
        raise _Refusal('foreign_subject')
    _lifetime(issued, not_after, now)
    return body


def _version(text):
    return tuple(int(part) for part in VERSION.fullmatch(text).groups())


def check_release_body(body):
    """The pebble.release/1 schema step verify_release_manifest applies to a signed manifest (§6.4 step 2), for
    a manifest the install tool reads without an envelope. Returns (issued_at, not_after); raises ValueError
    whose first argument is the refusal code ('format' or 'downgrade'). It authenticates nothing."""
    try:
        return _release_body(body)
    except _Refusal as refusal:
        raise ValueError(refusal.code) from None


def verify_release_manifest(envelope, *, roots, revoked, app_bundle_sha256, backend_executable_sha256,
                            installed_backend_sha256, backup_record_sha256, installed_version,
                            installed_data_epoch, data_epoch, now):
    """Verify a signed pebble.release/1 manifest for one install (D20 §6.7); the install tool's check.

    app_bundle_sha256 and backend_executable_sha256 are what the install tool re-hashed in the
    candidate. installed_backend_sha256 (`previous`) and backup_record_sha256 (`rollback`, the
    SHA256 of backup.json) are required. installed_version is the installed Info.plist version, or
    None when no app is installed. installed_data_epoch is the data home's epoch, or None when there
    is none. data_epoch is the candidate's schema epoch. Returns a ReleaseVerdict whose refusal code
    is one of RELEASE_REFUSALS."""
    for name, value in (('app_bundle_sha256', app_bundle_sha256), ('backend_executable_sha256', backend_executable_sha256),
                        ('installed_backend_sha256', installed_backend_sha256),
                        ('backup_record_sha256', backup_record_sha256)):
        if not _hex(value):
            raise ValueError(f'{name} must be a lowercase SHA256 the install tool computed.')
    if installed_version is not None and not (isinstance(installed_version, str) and VERSION.fullmatch(installed_version)):
        raise ValueError('installed_version must be X.Y.Z or None.')
    if installed_data_epoch is not None and not _integer(installed_data_epoch, 0, 10_000):
        raise ValueError('installed_data_epoch must be a non-negative integer or None.')
    if not _integer(data_epoch, 0, 10_000):
        raise ValueError('data_epoch must be a non-negative integer.')
    _aware(now, 'now')
    pinned, revocations = load_roots(roots, revoked)
    data = bytes(envelope) if isinstance(envelope, (bytes, bytearray)) else None
    seen = {'envelope_sha256': None if data is None else rr.sha256(data)}
    try:
        body = _release_steps(data, seen, pinned, revocations, now, app_bundle_sha256, backend_executable_sha256)
        if body['previous'] != installed_backend_sha256:
            raise _Refusal('previous_mismatch')
        if body['rollback'] != backup_record_sha256:
            raise _Refusal('rollback_mismatch')
        if installed_version is not None and _version(body['version']) <= _version(installed_version):
            raise _Refusal('version_not_higher')
        if body['data_epoch'] != data_epoch or (
                installed_data_epoch is not None
                and not body['min_data_epoch'] <= installed_data_epoch <= body['data_epoch']):
            raise _Refusal('data_epoch')
    except _Refusal as refusal:
        return ReleaseVerdict('refused', refusal.code, **seen)
    return ReleaseVerdict('accepted', None, manifest=dict(body), **seen)


# ---- Payload and envelope builders (no key material) ------------------------------------------


def build_payload(*, gate, subject, evidence_sha256, result, issued_at, not_after, findings=(), nonce=None):
    """The exact canonical attestation payload bytes a person signs, validated as verify() reads them."""
    if gate not in GATE_ROLES:
        raise ValueError(f'{gate!r} is not attestable; attestable gates: {", ".join(GATE_ROLES)}.')
    body = {'contract': CONTRACT, 'gate': gate, 'gate_version': GATE_VERSIONS[gate], 'role': GATE_ROLES[gate],
            'subject': dict(subject) if isinstance(subject, dict) else subject, 'result': result,
            'findings': [dict(item) for item in findings], 'evidence_sha256': evidence_sha256,
            'issued_at': stamp(issued_at), 'not_after': stamp(not_after),
            'nonce': new_nonce() if nonce is None else nonce}
    try:
        issued, expiry, parsed = _attestation_body(body)
    except _Refusal:
        raise ValueError('payload fields do not match the pebble.attestation/1 payload schema.') from None
    if expiry <= issued or expiry - issued > MAX_LIFETIME:
        raise ValueError('not_after must be later than issued_at and at most 30 days after it.')
    if result == 'passed' and any(severity in SERIOUS for _code, severity, _count in parsed):
        raise ValueError('a passed result cannot carry an open critical or high finding.')
    data = canonical(body)
    if len(data) > MAX_PAYLOAD_BYTES:
        raise ValueError('payload is too large.')
    return data


def build_release_payload(*, version, build, app_bundle_sha256, backend_executable_sha256, min_data_epoch,
                          data_epoch, previous, rollback, issued_at, not_after):
    """The exact canonical pebble.release/1 manifest bytes a release-key holder signs."""
    body = {'format': RELEASE_FORMAT, 'version': version, 'build': build, 'app_bundle_sha256': app_bundle_sha256,
            'backend_executable_sha256': backend_executable_sha256, 'min_data_epoch': min_data_epoch,
            'data_epoch': data_epoch, 'previous': previous, 'rollback': rollback,
            'issued_at': stamp(issued_at), 'not_after': stamp(not_after)}
    try:
        issued, expiry = _release_body(body)
    except _Refusal:
        raise ValueError('manifest fields do not match the pebble.release/1 schema.') from None
    if expiry <= issued or expiry - issued > MAX_LIFETIME:
        raise ValueError('not_after must be later than issued_at and at most 30 days after it.')
    return canonical(body)


def build_envelope(payload, signature_text):
    """Assemble the canonical envelope for exact payload bytes and the SSHSIG text ssh-keygen wrote.

    The signature must verify under the key it embeds; key_id is derived from that key. Assembly
    is not trust: only verify() with pinned roots decides acceptance."""
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError('payload must be at most 16 KiB of bytes.')
    payload = bytes(payload)
    try:
        same = canonical(rr.parse_json(payload, 'payload')) == payload
    except (rr.EvidenceError, ValueError):
        same = False
    if not same:
        raise ValueError('payload is not canonical JSON; sign the exact bytes `attest.py payload` wrote.')
    embedded = parse_sshsig(signature_text).public_key
    verify_sshsig(signature_text, payload, embedded)
    data = canonical({'format': FORMAT, 'key_id': key_id(embedded),
                      'payload': base64.b64encode(payload).decode('ascii'), 'signature': signature_text})
    if len(data) > MAX_ENVELOPE_BYTES:
        raise ValueError('envelope is too large.')
    return data


def envelope_key_id(envelope):
    """The key_id an envelope claims (unauthenticated), or None."""
    try:
        doc, _payload, _body = _open(envelope)
    except _Refusal:
        return None
    value = doc.get('key_id')
    return value if isinstance(value, str) and KEY_ID.fullmatch(value) else None


# ---- Contract v2 gate adapter -----------------------------------------------------------------


class _AttestedGate(rr.Gate):
    """The v1 gate semantics (any issue fails, any unverified reason or missing input blocks) with
    the v2 version and pass reason."""

    def render(self):
        rendered = super().render()
        rendered['version'] = GATE_VERSIONS[self.id]
        if rendered['status'] == 'passed':
            rendered['reason'] = PASSED_REASON
        return rendered


def _review_claims(review):
    probe = rr.Assessment(False, rr.PLATFORM)
    probe.claims_from('review', review)
    return [{'document': document, 'key': key} for document, key in sorted(probe.claims)]


def evaluate_gates(review, envelopes, *, roots, revoked, subject, review_date, now, ledger):
    """Contract v2 attestation gates, rendered in the release_readiness gate shape.

    review is the parsed review that declares readiness_contract pebble.release-readiness/2; its
    claim keys are listed and never used. envelopes is a sequence of (gate, envelope bytes,
    evidence bytes or None), with the gate the caller names. subject maps SUBJECT_KEYS to the hashes
    the validator computed itself, None for any it could not compute. ledger may be None only when
    no envelope is supplied. Returns {'contract', 'gates', 'verdicts', 'ignored_claims'}, where
    gates holds the four attestable gates. other_platform_containment and
    macos_distribution_signing are never returned, so the caller keeps their v1 gates; envelopes
    named for them are refused (role_not_allowed) and listed in verdicts."""
    if not isinstance(review, dict) or review.get(REVIEW_CONTRACT_KEY) != CONTRACT:
        raise ValueError(f'evaluate_gates needs a review that declares {REVIEW_CONTRACT_KEY} {CONTRACT}.')
    supplied = list(envelopes)
    if len(supplied) > MAX_ENVELOPES:
        raise rr.EvidenceError('too_many_attestations')
    for item in supplied:
        if (not isinstance(item, (tuple, list)) or len(item) != 3 or item[0] not in rr.PRODUCTION_GATES
                or not isinstance(item[1], (bytes, bytearray))
                or not (item[2] is None or isinstance(item[2], (bytes, bytearray)))):
            raise rr.EvidenceError('attestation_invalid')
    if not isinstance(subject, dict) or not _subject_valid(subject, allow_missing=True):
        raise ValueError('subject must map ' + ', '.join(SUBJECT_KEYS) + ' to computed values or None.')
    _aware(review_date, 'review_date')
    _aware(now, 'now')
    load_roots(roots, revoked)  # a malformed pin is a ValueError even when nothing is supplied
    if supplied and ledger is None:
        raise ValueError('a ledger is required to verify envelopes.')
    store = None if ledger is None else _ledger(ledger)
    missing_subject = [name for name in SUBJECT_KEYS if subject[name] is None]
    verdicts, outcomes, ledger_failure = [], {}, None
    for index, (gate, data, evidence) in enumerate(supplied):
        if missing_subject:
            outcomes[index] = None
            verdicts.append({'index': index, 'gate': gate, 'status': 'not_evaluated'})
            continue
        try:
            verdict = verify(data, roots=roots, revoked=revoked, subject=subject, gate=gate, review_date=review_date,
                             now=now, ledger=store, evidence=evidence)
        except LedgerError as error:
            ledger_failure, outcomes[index] = error.code, None
            verdicts.append({'index': index, 'gate': gate, 'status': 'not_evaluated', 'ledger': error.code})
            continue
        outcomes[index] = verdict
        verdicts.append({'index': index, 'gate': gate, 'status': verdict.status, 'verdict': verdict.as_dict()})
    gates = {}
    for gate, role in GATE_ROLES.items():
        rendered = _AttestedGate(gate, 'production')
        mine = [(index, outcomes[index]) for index, item in enumerate(supplied) if item[0] == gate]
        channel = pinned_keys(roots, revoked, gate=gate, now=now)
        accepted = [{'key_id': verdict.key_id, 'envelope_sha256': verdict.envelope_sha256,
                     'recorded': verdict.recorded} for _index, verdict in mine if verdict is not None and verdict.accepted]
        refused = [{'envelope': f'envelope[{index}]', 'code': verdict.code} for index, verdict in mine
                   if verdict is not None and not verdict.accepted]
        rendered.observed.update(role=role, pinned_keys=channel, supplied=len(mine), accepted=accepted, refused=refused)
        if missing_subject:
            rendered.unverified('The validator could not compute the complete subject, so no attestation can be bound '
                                'to this candidate.', 'validator-computed subject: ' + ', '.join(missing_subject))
        elif not channel:
            rendered.unverified('No pinned trust root may attest this gate; unauthenticated statements are ignored.',
                                f'pinned {role} public key allowing {gate} in backend/pebble/trust_roots.py')
        elif not mine:
            rendered.unverified('No attestation was supplied for this gate.',
                                f'{role}-signed {FORMAT} envelope bound to this subject, with its evidence file')
        else:
            for item in refused:
                rendered.issue('attestation_' + item['code'], item['envelope'])
            if any(verdict is None for _index, verdict in mine):
                rendered.observed['ledger'] = ledger_failure
                rendered.unverified('The attestation ledger could not be read or written safely.',
                                    'readable, append-only attestation ledger')
        gates[gate] = rendered.render()
    return {'contract': CONTRACT, 'gates': gates, 'verdicts': verdicts, 'ignored_claims': _review_claims(review)}


# ---- Wiring for release_readiness.check_release (the parent's contract v2 patch) --------------

SNAPSHOT_CODES = frozenset({'snapshot_digest_mismatch', 'snapshot_content_mismatch', 'snapshot_digest_unreproducible'})


def declares_v2(review):
    """Whether a parsed review opts into contract v2; every other review stays contract v1."""
    return isinstance(review, dict) and review.get(REVIEW_CONTRACT_KEY) == CONTRACT


def readiness_subject(run, review, app):
    """The subject a release_readiness Assessment computed itself after its v1 steps, None for any
    value it could not compute: the snapshot digest it reproduced or checked, the candidate
    executable and bundle it walked without a problem, the explicit app's bundle digest and the host
    platform."""
    integrity, candidate = run.gates['evidence_integrity'], run.gates['candidate_bundle']
    snapshot = None
    if 'review' in run.checked and not SNAPSHOT_CODES & set(integrity.problems):
        snapshot = review['source_snapshot_sha256']
    clean = 'candidate' in run.checked and not candidate.problems
    app_digest = None
    if app is not None:
        try:
            app_digest = app_bundle_digest(rr.owned_app_path(app))
        except (rr.EvidenceError, rr.UnsupportedPlatform):
            app_digest = None
    return {'source_snapshot_sha256': snapshot,
            'candidate_executable_sha256': candidate.observed.get('executable_sha256') if clean else None,
            'candidate_bundle_sha256': candidate.observed.get('bundle_sha256') if clean else None,
            'app_bundle_sha256': app_digest, 'platform': subject_platform()}


def readiness_attestations(run, review, attestations, ledger, record, trust, now, app):
    """The contract v2 step of check_release, run through Assessment.guard after the v1 steps.

    attestations is [(gate, envelope path, evidence path)] from the command line. A v1 review
    returns None, and with attestations it is malformed evidence. trust is (roots, revoked) for
    tests only; the command line never sets it. Returns the evaluate_gates outcome."""
    if not declares_v2(review):
        if attestations:
            raise rr.EvidenceError('attestation_requires_contract_v2')
        return None
    if len(attestations) > MAX_ENVELOPES:
        raise rr.EvidenceError('too_many_attestations')
    supplied = []
    for index, item in enumerate(attestations):
        label = f'attestation[{index}]'
        if not isinstance(item, (tuple, list)) or len(item) != 3 or item[0] not in rr.PRODUCTION_GATES:
            raise rr.EvidenceError('attestation_gate_invalid', label)
        supplied.append((item[0], rr.read_explicit(item[1], 2 * MAX_ENVELOPE_BYTES, label),
                         rr.read_explicit(item[2], rr.MAX_ARTIFACT_BYTES, label + '.evidence')))
    if supplied and ledger is None:
        raise rr.EvidenceError('attestation_ledger_missing')
    roots, revoked = pinned_trust() if trust is None else trust
    if supplied:
        run.inputs['attestation_sha256'] = [rr.sha256(envelope) for _gate, envelope, _evidence in supplied]
    return evaluate_gates(review, supplied, roots=roots, revoked=revoked, subject=readiness_subject(run, review, app),
                          review_date=run.review_date, now=datetime.now(timezone.utc) if now is None else now,
                          ledger=None if ledger is None else Ledger(ledger, record=record))


def apply_contract_v2(result, outcome):
    """A contract v2 result: the v1 result with the attested gates, their versions and the verdicts,
    and production readiness recomputed from every gate. The v1 observations of a replaced gate are
    kept under observed.unauthenticated. An outcome of None (a malformed attestation step) keeps
    the v1 gates, which never pass."""
    gates, versions = dict(result['gates']), dict(result['gate_versions'])
    for gate_id, rendered in (outcome or {'gates': {}})['gates'].items():
        merged = dict(rendered, observed=dict(rendered['observed']))
        if gates[gate_id]['observed']:
            merged['observed']['unauthenticated'] = gates[gate_id]['observed']
        gates[gate_id], versions[gate_id] = merged, rendered['version']
    production = (result['evidence'] == 'well_formed' and result['readiness']['local_candidate']
                  and not result['fixture'] and all(gates[gate_id]['status'] == 'passed' for gate_id in rr.PRODUCTION_GATES))
    code = result['exit_code']
    if code != rr.EXIT_MALFORMED:
        code = rr.EXIT_PRODUCTION_READY if production else code
    return dict(result, contract_version=2, gate_versions=versions, gates=gates, production_ready=production,
                readiness=dict(result['readiness'], production=production), exit_code=code,
                attestations=[] if outcome is None else outcome['verdicts'])
