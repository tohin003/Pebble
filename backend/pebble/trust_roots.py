"""Pinned public trust roots for authenticated production evidence (readiness contract v2).

Only public keys live here. Private keys stay with the people who hold them and
are never given to an agent, a tool or this repository. This file is covered by
the tested-source snapshot, so pinning, rotating or revoking a key is a new
release that needs a new proof. Nothing else can add a root: no flag,
environment variable or file is consulted, and tests pass their own roots to
backend/pebble/attestation.py as function arguments.

ROOTS maps a key_id to
    {'alg': 'ssh-ed25519',
     'public_key': '<base64 OpenSSH wire blob, the second field of the .pub line>',
     'roles': ('operator',),                    # operator, auditor or release
     'gates': ('installed_application',),       # gates this key may attest
     'not_before': 'YYYY-MM-DDTHH:MM:SSZ', 'not_after': 'YYYY-MM-DDTHH:MM:SSZ',
     'label': 'operator-2026'}
REVOKED maps a key_id to a short reason; a listed key is refused even while ROOTS
still pins it. `scripts/attest.py root-entry <key.pub>` prints a ready entry.
The ceremony, rotation and revocation are in docs/HARNESS_TRUST_CHANNEL.md.
"""

import hashlib

ROOTS_VERSION = 1

# Empty until the user supplies public keys: every attestation gate stays unverified.
ROOTS = {}

REVOKED = {}


def key_id(raw_public_key: bytes) -> str:
    """The first 16 hex digits of the SHA256 of a raw public key (32 bytes for Ed25519).

    This is not the `ssh-keygen -l` fingerprint, which hashes the wire blob instead.
    """
    if not isinstance(raw_public_key, (bytes, bytearray)):
        raise TypeError('key_id needs the raw public key bytes.')
    return hashlib.sha256(bytes(raw_public_key)).hexdigest()[:16]
