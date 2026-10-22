"""In-memory credential grants for the egress reverse proxy (X04).

A credential is fetched once through a host vault callback and held only in
this process's memory, in a bytearray. It never enters a child's environment,
argv or files: E9 showed that any same-user process, a confined job included,
can read another's initial environment and argv through KERN_PROCARGS2. Each
grant is pinned to one HTTPS origin, a method set, a path prefix and an expiry.
The egress broker asks for the header per request. Revocation applies to the
next request at once, and the egress broker polls state() to cut in-flight
streams. repr, exceptions, log() and stats() carry grant ids, a per-instance
keyed hash and the length of a secret, never the secret.

Zeroization limits: revoke(), expiry and close() overwrite the broker's own
bytearrays and the egress broker overwrites its copies. Python cannot reach the
str or bytes the vault callback returned, OpenSSL's record buffers, freed heap
or swap. The host should set RLIMIT_CORE=0 for any process that holds a broker.
"""

import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from collections import deque

from . import egress
from .policy import PolicyError

LOG_LIMIT = 256
MAX_GRANTS = 16
MAX_SECRET = 8192
MAX_SECONDS = 3600
REF = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}')
HEADER = re.compile(r'[A-Za-z][A-Za-z0-9-]{0,63}')
SCHEME = re.compile(r'[A-Za-z][A-Za-z0-9-]{0,15}')
FRAMING = frozenset({'host', 'content-length', 'transfer-encoding', 'connection', 'keep-alive', 'upgrade', 'te',
                     'trailer', 'expect', 'proxy-authorization', 'proxy-connection', 'http2-settings'})
URL = re.compile(r'(https://[^/?#]{1,300})(/[^?#]*)(?:\?[^#]*)?')

# Test seams: expiry holds when either clock passes, so a wall-clock step back cannot extend a grant.
_wall, _mono = time.time, time.monotonic


class CredentialRefused(PolicyError):
    """A refused credential operation; reason is a fixed code for logs and the egress status."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class _Grant:
    __slots__ = ('ref', 'destination', 'methods', 'prefix', 'expires', 'deadline', 'header', 'scheme', 'secret',
                 'length', 'tag')

    def __repr__(self):
        return f'<credential grant {self.destination} length={self.length}>'

    def __reduce_ex__(self, protocol):
        raise TypeError('Credential grants are not serializable.')


class CredentialBroker:
    """Holds credentials in memory and issues one header per vetted upstream request.

    fetch(ref) -> bytes|str is a host callback the parent wires to the vault; the
    broker never reads the vault itself. Thread-safe. Not serializable.
    """

    def __init__(self, fetch):
        if not callable(fetch):
            raise PolicyError('The credential broker needs a host vault callback.')
        self._fetch = fetch
        self._key = bytearray(secrets.token_bytes(32))
        self._grants, self._ended = {}, {}
        self._records = deque(maxlen=LOG_LIMIT)
        self._counts = {'granted': 0, 'issued': 0, 'refused': 0, 'revoked': 0, 'expired': 0}
        self._lock = threading.Lock()
        self.closed = False

    def __repr__(self):
        with self._lock:
            return f'<CredentialBroker live={len(self._grants)} closed={self.closed}>'

    def __reduce_ex__(self, protocol):
        raise TypeError('Credential brokers are not serializable.')

    def grant(self, ref, destination, methods, path_prefix, expires, *, header='Authorization', scheme='Bearer'):
        """Fetch ref once, hold it for destination and return a grant id ('cg_' + 24 hex).

        destination is an https origin, methods a subset of egress.ROUTE_METHODS,
        path_prefix a clean absolute path, and expires a time.time() instant at
        most one hour ahead. The header value is '<scheme> <secret>', or the bare
        secret when scheme is None.
        """
        if not isinstance(ref, str) or not REF.fullmatch(ref):
            raise PolicyError('Credential references are printable vault names of up to 128 characters.')
        target, allowed, prefix = egress.origin(destination), egress.route_methods(methods), egress.path_prefix(path_prefix)
        if type(expires) not in (int, float) or not math.isfinite(expires) or not 0 < expires - _wall() <= MAX_SECONDS:
            raise PolicyError('Credential grants expire within one hour from now.')
        if not isinstance(header, str) or not HEADER.fullmatch(header) or header.lower() in FRAMING:
            raise PolicyError('Choose a credential header name that is not an HTTP framing or proxy field.')
        if scheme is not None and (not isinstance(scheme, str) or not SCHEME.fullmatch(scheme)):
            raise PolicyError('A credential scheme is a short token such as Bearer, or None.')
        with self._lock:
            self._admit()
        secret = self._read(ref)
        with self._lock:
            try:
                self._admit()
            except PolicyError:
                _zero(secret)
                raise
            item = _Grant()
            item.ref, item.destination, item.methods, item.prefix = ref, target, allowed, prefix
            item.expires, item.deadline = float(expires), _mono() + (expires - _wall())
            item.header, item.scheme, item.secret, item.length = header, scheme, secret, len(secret)
            item.tag = hmac.new(self._key, secret, hashlib.sha256).hexdigest()[:16]
            ident = 'cg_' + secrets.token_hex(12)
            self._grants[ident] = item
            self._counts['granted'] += 1
            self._log('grant', ident, item)
        return ident

    def header(self, grant_id, method, url):
        """(name, value) for one upstream request, or CredentialRefused.

        Called only by the egress reverse proxy after it vetted the route and the
        address. It checks the grant's origin, method, path prefix and expiry.
        value is a fresh bytearray that the caller overwrites after sending.
        """
        with self._lock:
            self._purge()
            known = isinstance(grant_id, str) and (grant_id in self._grants or grant_id in self._ended)
            item = self._grants.get(grant_id) if known else None
            match = URL.fullmatch(url) if isinstance(url, str) and len(url) <= 8192 and url.isascii() else None
            refusal = self._check(grant_id, item, method, match)
            shown = method if method in egress.ROUTE_METHODS else None
            # The same hash as the egress route record: path and query, never the text.
            digest = hashlib.sha256(url[len(match[1]):].encode()).hexdigest() if match else None
            if refusal is None:
                prefix = (item.scheme + ' ').encode('ascii') if item.scheme else b''
                value = bytearray(len(prefix) + item.length)
                value[:len(prefix)] = prefix
                value[len(prefix):] = item.secret
                self._counts['issued'] += 1
                self._log('header', grant_id, item, shown, digest)
                return item.header, value
            self._counts['refused'] += 1
            self._log('refused', grant_id if known else None, item, shown, digest, refusal[0])
        raise CredentialRefused(*refusal)

    def state(self, grant_id):
        """'active', 'expired', 'revoked', 'unknown' or 'closed'."""
        with self._lock:
            self._purge()
            if self.closed:
                return 'closed'
            if isinstance(grant_id, str) and grant_id in self._grants:
                return 'active'
            return self._ended.get(grant_id, 'unknown') if isinstance(grant_id, str) else 'unknown'

    def describe(self, grant_id):
        """Public facts of a live grant, for route review. Never the secret."""
        with self._lock:
            self._purge()
            item = self._grants.get(grant_id) if isinstance(grant_id, str) and not self.closed else None
            if item is None:
                state = 'closed' if self.closed else self._ended.get(grant_id, 'unknown_grant') if isinstance(grant_id, str) else 'unknown_grant'
                raise CredentialRefused(state, 'The credential grant is not live.')
            return {'grant': grant_id, 'ref': item.ref, 'destination': item.destination, 'methods': list(item.methods),
                    'path_prefix': item.prefix, 'expires': item.expires, 'header': item.header, 'scheme': item.scheme,
                    'secret_hash': item.tag, 'secret_length': item.length}

    def revoke(self, grant_id):
        """End a grant now and zeroize it. Returns True when a live grant ended."""
        with self._lock:
            item = self._grants.pop(grant_id, None) if isinstance(grant_id, str) else None
            if item is None:
                return False
            self._finish(grant_id, item, 'revoked')
            return True

    def log(self):
        with self._lock:
            return [dict(record) for record in self._records]

    def stats(self):
        with self._lock:
            self._purge()
            return {'live': len(self._grants), **self._counts, 'closed': self.closed}

    def close(self):
        """Zeroize and drop every grant and the hash key; later calls refuse. Idempotent.

        Only this broker's bytearrays are overwritten (see the module limits).
        """
        with self._lock:
            if self.closed:
                return
            for ident, item in list(self._grants.items()):
                _zero(item.secret)
                self._remember(ident, 'revoked')
            self._log('close', None, None, reason=f'zeroized_{len(self._grants)}')
            self._grants.clear()
            _zero(self._key)
            self.closed = True

    # Internals (the caller holds the lock)

    def _admit(self):
        self._purge()
        if self.closed:
            raise CredentialRefused('closed', 'The credential broker is closed.')
        if len(self._grants) >= MAX_GRANTS:
            raise PolicyError('At most sixteen credential grants may be live.')

    def _read(self, ref):
        failed = False
        try:
            value = self._fetch(ref)
        except Exception:
            failed = True
        if failed:
            # Raised outside the handler, so no __context__ can carry vault text.
            raise CredentialRefused('fetch_failed', 'The credential could not be read from the vault.')
        if isinstance(value, str) and value.isascii():
            secret = bytearray(value, 'ascii')
        elif isinstance(value, (bytes, bytearray)):
            secret = bytearray(value)
        else:
            secret = None
        del value
        if secret is None or not 1 <= len(secret) <= MAX_SECRET or any(not 33 <= byte <= 126 for byte in secret):
            if secret is not None:
                _zero(secret)
            raise PolicyError('The vault returned an unusable credential; use 1 to 8192 printable ASCII bytes without spaces.')
        return secret

    def _check(self, grant_id, item, method, match):
        if self.closed:
            return 'closed', 'The credential broker is closed.'
        if item is None:
            state = self._ended.get(grant_id, 'unknown_grant') if isinstance(grant_id, str) else 'unknown_grant'
            return state, 'The credential grant is revoked, expired or unknown.'
        if method not in item.methods:
            return 'method_refused', 'The credential grant does not cover this method.'
        try:
            target = egress.origin(match[1]) if match else None
        except PolicyError:
            target = None
        if target != item.destination:
            return 'destination_refused', 'The credential grant does not cover this origin.'
        if egress.path_problem(match[2]) is not None:
            return 'path_refused', 'The credential request path is not a clean absolute path.'
        if not egress.within(match[2], item.prefix):
            return 'prefix_refused', 'The credential grant does not cover this path.'
        return None

    def _purge(self):
        now, clock = _wall(), _mono()
        for ident, item in list(self._grants.items()):
            if now >= item.expires or clock >= item.deadline:
                del self._grants[ident]
                self._finish(ident, item, 'expired')

    def _finish(self, ident, item, state):
        _zero(item.secret)
        self._remember(ident, state)
        self._counts[state] += 1
        self._log('revoke' if state == 'revoked' else 'expire', ident, item, reason=state)

    def _remember(self, ident, state):
        self._ended[ident] = state
        while len(self._ended) > LOG_LIMIT:
            del self._ended[next(iter(self._ended))]

    def _log(self, event, ident, item, method=None, path_hash=None, reason=None):
        self._records.append({'event': event, 'grant': ident, 'ref': item.ref if item else None,
                              'destination': item.destination if item else None, 'method': method,
                              'path_hash': path_hash, 'reason': reason, 'secret_hash': item.tag if item else None,
                              'secret_length': item.length if item else None, 'at': time.time()})


def _zero(buffer):
    buffer[:] = bytes(len(buffer))
