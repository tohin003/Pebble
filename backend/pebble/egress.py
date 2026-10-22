"""Host-side allowlisted HTTPS egress broker for confined installers.

A confined installer reaches the network only through this HTTP CONNECT proxy
on 127.0.0.1: its sandbox profile permits outbound TCP to the broker's port
alone (network_profile). The broker admits exact (host, port) pairs, resolves
DNS itself and connects to the vetted address, so a name that resolves, or
rebinds, to a private, loopback, link-local, multicast or reserved address
never reaches a local service. It relays TLS bytes without terminating TLS,
never echoes client data and logs every admission and refusal. Request text
is untrusted data and never authorization.

Reverse-proxy routes (X04) exist because CONNECT never terminates TLS and so
cannot add a credential. A confined job sends plain HTTP/1.1 to
http://127.0.0.1:<port>/r/<name>/<path>. Only a declared route, method and
path prefix is admitted, and dot segments or their encodings are refused. The
broker vets the upstream address exactly as for CONNECT, makes the HTTPS
request itself with a verified certificate, adds the credential header from an
in-memory CredentialBroker, and streams the response back without hop-by-hop
headers. A redirect is returned, never followed; one to another origin becomes
a 502. Route records hold a path hash, never the path or a credential.
"""

import hashlib
import ipaddress
import os
import re
import selectors
import socket
import ssl
import threading
import time
from collections import deque
from http import HTTPStatus
from urllib.parse import urljoin, urlsplit

import certifi

from .policy import PolicyError

HEADER_LIMIT = 8192
RESPONSE_HEADER_LIMIT = 32768
REQUEST_LIMIT = 16_000_000
LOG_LIMIT = 256
MAX_ALLOW = 16
MAX_ROUTES = 16
MAX_HEADERS = 64
MAX_ADDRESSES = 16
CHUNK = 65536
POLL = 0.1
CONNECT_SECONDS = 10
JOIN_SECONDS = 5
LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
TARGET = re.compile(r'(\[[0-9a-f:.]{2,45}\]|[a-z0-9.-]{1,253}):([0-9]{1,5})')
NAT64 = ipaddress.ip_network('64:ff9b::/96')
STATUS = {400: 'Bad Request', 403: 'Forbidden', 404: 'Not Found', 405: 'Method Not Allowed', 408: 'Request Timeout',
          411: 'Length Required', 413: 'Content Too Large', 417: 'Expectation Failed',
          431: 'Request Header Fields Too Large', 502: 'Bad Gateway', 503: 'Service Unavailable', 504: 'Gateway Timeout'}
ESTABLISHED = b'HTTP/1.1 200 Connection Established\r\n\r\n'
ROUTE_METHODS = ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS')
ROUTE_NAME = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?')
ORIGIN = re.compile(r'https://(\[[0-9a-f:.]{2,45}\]|[a-z0-9.-]{1,253})(?::([0-9]{1,5}))?')
PATH_TEXT = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]{0,2047}")
QUERY_TEXT = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,2048}")
BAD_ESCAPE = re.compile(r'%(?![0-9A-Fa-f]{2})')
ENCODED = re.compile(r'%(?:2[eEfF]|5[cC]|25|00)')  # Encoded '.', '/', '\', '%' and NUL.
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")
FIELD = re.compile(r'[\t\x20-\x7e]*')
BAD_FIELD = re.compile(rb'[\x00-\x08\x0a-\x1f\x7f]')
STATUS_LINE = re.compile(rb'HTTP/1\.[01] ([1-5][0-9]{2})(?: [^\x00-\x08\x0a-\x1f\x7f]*)?')
CHUNK_LINE = re.compile(rb'([0-9A-Fa-f]{1,8})(?:[ \t]*;[^\x00-\x08\x0a-\x1f\x7f]*)?')
LENGTH = re.compile(r'[0-9]{1,15}')
HOP = frozenset({'connection', 'keep-alive', 'proxy-connection', 'proxy-authenticate', 'proxy-authorization', 'te',
                 'trailer', 'trailers', 'transfer-encoding', 'upgrade'})
HALT = {'broker_closed': 503, 'lifetime': 503, 'byte_limit': 503, 'idle': 504, 'revoked': 403, 'expired': 403}


def _literal(value):
    try:
        return ipaddress.ip_address(value[1:-1] if value.startswith('[') and value.endswith(']') else value)
    except ValueError:
        return None


def host_name(value, loopback=False):
    """Return a canonical allowlist host or raise PolicyError.

    Hosts are lowercase ASCII DNS names. IDNA/punycode (any label with '--'
    in positions three and four), IP literals, numeric host forms, wildcards
    and empty labels are refused. With loopback, a loopback IP literal is
    accepted for local fixtures only.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 253 or not value.isascii():
        raise PolicyError('Egress hosts are lowercase ASCII DNS names.')
    address = _literal(value)
    if address is not None:
        if loopback and address.is_loopback:
            return str(address)
        raise PolicyError('Egress hosts are DNS names; IP literals are refused.')
    labels = value.split('.')
    if value != value.lower() or not all(LABEL.fullmatch(label) for label in labels):
        raise PolicyError('Egress hosts must be lowercase DNS names without wildcards or empty labels.')
    if any(label[2:4] == '--' for label in labels):
        raise PolicyError('Internationalized (IDNA/punycode) egress hosts are refused.')
    if labels[-1].isdigit() or re.fullmatch(r'0x[0-9a-f]*', labels[-1]):
        raise PolicyError('Egress hosts are DNS names; numeric host forms are refused.')
    return value


def port_number(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise PolicyError('Egress ports are integers from 1 to 65535.')
    return value


def allowlist(allow, loopback=False):
    """Normalize exact (host, port) pairs; at most sixteen."""
    if not isinstance(allow, (list, tuple)) or not 1 <= len(allow) <= MAX_ALLOW:
        raise PolicyError('Choose one to sixteen exact (host, port) egress destinations.')
    pairs = []
    for item in allow:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise PolicyError('Each egress destination is an exact (host, port) pair.')
        pair = (host_name(item[0], loopback), port_number(item[1]))
        if pair not in pairs:
            pairs.append(pair)
    return tuple(pairs)


def address_allowed(value, loopback=False):
    """True only for a public unicast address, or a loopback one when fixtures allow it.

    Embedded IPv4 (mapped, 6to4, Teredo, NAT64) must pass too, and scoped
    addresses are refused, so a resolver answer can never steer a tunnel into
    a private or local network.
    """
    if not isinstance(value, str) or '%' in value:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.version == 6:
        if address.ipv4_mapped is not None:
            return address_allowed(str(address.ipv4_mapped), loopback)
        embedded = address.sixtofour or (address.teredo[1] if address.teredo else None)
        if embedded is None and address in NAT64:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if embedded is not None and not address_allowed(str(embedded)):
            return False
    if loopback and address.is_loopback:
        return True
    return (address.is_global and not (address.is_private or address.is_loopback or address.is_link_local
                                       or address.is_multicast or address.is_unspecified or address.is_reserved
                                       or getattr(address, 'is_site_local', False)))


def resolve(host, port):
    """Host-side DNS (a test seam). Returns unique address strings in resolver order."""
    found = []
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP):
        address = info[4][0]
        if isinstance(address, str) and address not in found:
            found.append(address)
    return found


def network_profile(port):
    """SBPL fragment for a (deny default) profile: outbound TCP only to the broker.

    Nothing else is needed. The client never resolves names (the broker does),
    so the mDNSResponder socket, every other loopback port, inbound/bind and
    every remote address stay denied by the deny-default base.
    """
    port_number(port)
    return f'(allow network-outbound (remote tcp "localhost:{port}"))'


def origin(value, loopback=False):
    """Canonical 'https://host:port' for an HTTPS origin, or PolicyError.

    Only https, a host accepted by host_name and an optional port. Userinfo,
    path, query, fragment and uppercase are refused.
    """
    match = ORIGIN.fullmatch(value) if isinstance(value, str) and len(value) <= 300 else None
    if not match:
        raise PolicyError('Origins are https://host[:port] with nothing else.')
    host = host_name(match[1], loopback)
    port = port_number(int(match[2])) if match[2] else 443
    return f'https://[{host}]:{port}' if ':' in host else f'https://{host}:{port}'


def route_methods(value):
    """Distinct methods from ROUTE_METHODS, in that order. CONNECT and TRACE never qualify."""
    if (not isinstance(value, (list, tuple)) or not all(isinstance(m, str) and m in ROUTE_METHODS for m in value)
            or not 1 <= len(value) <= len(ROUTE_METHODS) or len(set(value)) != len(value)):
        raise PolicyError('Choose distinct methods from GET, HEAD, POST, PUT, PATCH, DELETE and OPTIONS.')
    return tuple(m for m in ROUTE_METHODS if m in value)


def path_problem(path):
    """None for a clean origin-form path, else 'path_traversal' or 'malformed_target'.

    Dot segments (also before ';' parameters), empty segments, a backslash and
    the encoded forms of '.', '/', '\\', '%' and NUL count as traversal, since an
    upstream could decode or normalize them past the reviewed prefix.
    """
    if not isinstance(path, str) or not PATH_TEXT.fullmatch(path) or BAD_ESCAPE.search(path):
        return 'path_traversal' if isinstance(path, str) and '\\' in path else 'malformed_target'
    segments = path.split('/')[1:]
    if ENCODED.search(path) or '' in segments[:-1] or any(s.split(';', 1)[0] in ('.', '..') for s in segments):
        return 'path_traversal'
    return None


def path_prefix(value):
    """A reviewed path prefix: a clean absolute path without a query, at most 1024 characters."""
    if not isinstance(value, str) or not 1 <= len(value) <= 1024 or path_problem(value) is not None:
        raise PolicyError('Path prefixes are clean absolute paths without dot segments, encodings or a query.')
    return value


def within(path, prefix):
    """True when path is the prefix itself or below it on a segment boundary."""
    return path == prefix or path.startswith(prefix if prefix.endswith('/') else prefix + '/')


def route_table(routes, loopback=False, credentials=None):
    """Normalize named reverse-proxy routes; at most sixteen.

    Each route is {'origin', 'methods', 'path_prefix', 'grant'?}. A grant must
    be live in credentials, and the route may only narrow it: same origin,
    a subset of its methods and a prefix within its prefix.
    """
    if not isinstance(routes, dict) or not 1 <= len(routes) <= MAX_ROUTES:
        raise PolicyError('Choose one to sixteen named egress routes.')
    table = {}
    for name, spec in routes.items():
        if not isinstance(name, str) or not ROUTE_NAME.fullmatch(name):
            raise PolicyError('Route names are lowercase labels of up to 32 characters.')
        if not isinstance(spec, dict) or not {'origin', 'methods', 'path_prefix'} <= set(spec) <= {
                'origin', 'methods', 'path_prefix', 'grant'}:
            raise PolicyError('Each route declares origin, methods, path_prefix and an optional grant.')
        text = origin(spec['origin'], loopback)
        host, _, port = text[8:].rpartition(':')
        allowed, prefix, grant, header = route_methods(spec['methods']), path_prefix(spec['path_prefix']), spec.get('grant'), None
        if grant is not None:
            if credentials is None:
                raise PolicyError('A credential route needs the credential broker.')
            public = credentials.describe(grant)
            if (public['destination'] != text or not set(allowed) <= set(public['methods'])
                    or not within(prefix, public['path_prefix'])):
                raise PolicyError('A route may only narrow its credential grant.')
            header = public['header'].lower()
        table[name] = {'origin': text, 'host': host.strip('[]'), 'port': int(port), 'methods': allowed,
                       'path_prefix': prefix, 'grant': grant, 'header': header}
    return table


class _Refused(Exception):
    def __init__(self, status, reason, host=None, port=None, *, route=None, method=None, path_hash=None, allow=None):
        super().__init__(reason)
        self.status, self.reason, self.host, self.port = status, reason, host, port
        self.route, self.method, self.path_hash, self.allow = route, method, path_hash, allow


class _Upstream:
    """Buffered reads from an upstream TLS socket; every wait honours close, lifetime, idle and revocation."""

    def __init__(self, broker, sock, grant):
        self.broker, self.sock, self.grant = broker, sock, grant
        self.buffer, self.last, self.eof = bytearray(), time.monotonic(), False

    def fill(self):
        if self.eof:
            return False
        data = self.broker._pull(self.sock, CHUNK, self.grant, self.last)
        self.last = time.monotonic()
        if not data:
            self.eof = True
            return False
        self.buffer += data
        return True

    def line(self):
        while (end := self.buffer.find(b'\r\n')) == -1:
            if len(self.buffer) > HEADER_LIMIT:
                raise _Refused(502, 'response_header_too_large')
            if not self.fill():
                raise _Refused(502, 'upstream_truncated')
        if end > HEADER_LIMIT:
            raise _Refused(502, 'response_header_too_large')
        line = bytes(self.buffer[:end])
        del self.buffer[:end + 2]
        return line

    def read(self, size):
        if not self.buffer and not self.fill():
            return b''
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data


class EgressBroker:
    """HTTP CONNECT proxy on 127.0.0.1 that tunnels only to allowlisted destinations.

    Limits: max_connections concurrent clients, max_bytes relayed in total
    (both directions, across every tunnel), idle_seconds without traffic per
    tunnel or request, and lifetime_seconds for the broker. Each refusal gets a
    fixed status line and a log record; nothing the client sent is echoed.

    With routes (see route_table) the broker is also a reverse proxy. allow may
    then be None for a route-only broker, which refuses CONNECT. Route requests
    share max_connections, max_bytes, idle and lifetime; max_request_bytes bounds
    one request body. TLS uses certifi; ca_file replaces it for loopback
    fixtures only. Route records are {route, method, path_hash, status, bytes,
    credential_used, allowed, reason, started, ended}. allowed means the request
    was sent upstream, so an effect may have occurred. CONNECT records keep their
    shape. The caller owns credentials and closes it after this broker.
    """

    def __init__(self, allow, *, loopback_targets=False, max_connections=8, max_bytes=512_000_000,
                 idle_seconds=60, lifetime_seconds=900, routes=None, credentials=None, ca_file=None,
                 max_request_bytes=REQUEST_LIMIT):
        if type(loopback_targets) is not bool:
            raise PolicyError('loopback_targets must be a boolean.')
        if type(max_connections) is not int or not 1 <= max_connections <= 64:
            raise PolicyError('Choose one to sixty-four egress connections.')
        if type(max_bytes) is not int or not 1 <= max_bytes <= 4_000_000_000:
            raise PolicyError('Choose an egress byte limit from 1 byte to 4 GB.')
        if type(max_request_bytes) is not int or not 0 <= max_request_bytes <= 4_000_000_000:
            raise PolicyError('Choose a request body limit from 0 bytes to 4 GB.')
        for value in (idle_seconds, lifetime_seconds):
            if type(value) not in (int, float) or not 0 < value <= 3600:
                raise PolicyError('Egress idle and lifetime limits are positive seconds up to one hour.')
        if routes is None and (credentials is not None or ca_file is not None):
            raise PolicyError('Credentials and a CA file apply only to reverse-proxy routes.')
        if credentials is not None and not all(callable(getattr(credentials, n, None)) for n in ('header', 'describe', 'state')):
            raise PolicyError('Route credentials come from a CredentialBroker.')
        self.allow = () if routes is not None and allow is None else allowlist(allow, loopback_targets)
        self.routes = None if routes is None else route_table(routes, loopback_targets, credentials)
        self.credentials, self.tls = credentials, None
        if self.routes is not None:
            if ca_file is not None and (not loopback_targets or not isinstance(ca_file, str)
                                        or not os.path.isabs(ca_file) or not os.path.isfile(ca_file)):
                raise PolicyError('A CA file is an absolute file path for loopback fixtures only.')
            try:
                self.tls = ssl.create_default_context(cafile=ca_file or certifi.where())
            except (OSError, ssl.SSLError):
                raise PolicyError('The egress CA file could not be loaded.') from None
            self.tls.set_alpn_protocols(['http/1.1'])
        self.loopback = loopback_targets
        self.max_connections, self.max_bytes, self.max_request = max_connections, max_bytes, max_request_bytes
        self.idle, self.lifetime = float(idle_seconds), float(lifetime_seconds)
        self.lock = threading.Lock()
        self.records = deque(maxlen=LOG_LIMIT)
        self.connections = {}
        self.listener = self.acceptor = self._port = self.deadline = None
        self.started = self.closed = self.exhausted = False
        self.spent = self.sequence = 0

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    @property
    def port(self):
        return self._port

    @property
    def url(self):
        return f'http://127.0.0.1:{self._port}' if self._port else None

    def start(self):
        with self.lock:
            if self.started or self.closed:
                raise PolicyError('An egress broker starts once.')
            self.started = True
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(('127.0.0.1', 0))
            listener.listen(self.max_connections + 8)
            listener.settimeout(POLL)
        except OSError:
            listener.close()
            raise
        self.listener, self._port = listener, listener.getsockname()[1]
        self.deadline = time.monotonic() + self.lifetime
        self.acceptor = threading.Thread(target=self._accept, name='pebble-egress-accept', daemon=True)
        self.acceptor.start()
        return self

    def log(self):
        with self.lock:
            return [dict(record) for record in self.records]

    def close(self):
        """Stop the listener and every tunnel, then join threads (bounded). Idempotent.

        Returns True when the acceptor and every tunnel thread were joined.
        Routes are torn down too; in-flight route requests end as broker_closed.
        """
        with self.lock:
            self.closed = True
            if self.routes is not None:
                self.routes = {}
        acceptor = self.acceptor
        if acceptor is not None:
            acceptor.join(JOIN_SECONDS)
            if acceptor.is_alive() and self.listener is not None:
                _shutdown(self.listener)
        with self.lock:
            entries = list(self.connections.values())
        # Shutdown, never close: each tunnel thread closes its own sockets, so
        # no descriptor can be reused under a thread that still holds it.
        for entry in entries:
            for sock in list(entry['sockets']):
                _shutdown(sock)
        deadline = time.monotonic() + JOIN_SECONDS
        threads = [entry['thread'] for entry in entries if entry['thread'].ident is not None]
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not (acceptor is not None and acceptor.is_alive()) and not any(thread.is_alive() for thread in threads)

    # Internals

    def _record(self, host, port, allowed, reason):
        now = time.time()
        record = {'host': host, 'port': port, 'allowed': allowed, 'reason': reason, 'bytes_up': 0, 'bytes_down': 0,
                  'started': now, 'ended': None if allowed else now}
        with self.lock:
            self.records.append(record)
        return record

    def _route_record(self, route, method, path_hash, reason, status=None, opened=False):
        now = time.time()
        record = {'route': route, 'method': method, 'path_hash': path_hash, 'status': status, 'bytes': 0,
                  'credential_used': False, 'allowed': False, 'reason': reason, 'started': now,
                  'ended': None if opened else now}
        with self.lock:
            self.records.append(record)
        return record

    def _refused(self, client, refused, routed):
        if routed:
            self._route_record(refused.route, refused.method, refused.path_hash, refused.reason, refused.status)
            if refused.status is not None:
                _reply(client, refused.status, refused.reason, refused.allow or ', '.join(ROUTE_METHODS))
            return
        self._record(refused.host, refused.port, False, refused.reason)
        if refused.status is not None:
            _reply(client, refused.status)

    def _end(self, record, reason):
        with self.lock:
            record['reason'], record['ended'] = reason, time.time()

    def _set(self, record, **fields):
        with self.lock:
            record.update(fields)

    def _count(self, record, field, size):
        with self.lock:
            record[field] += size

    def _spend(self, size):
        with self.lock:
            if self.spent + size > self.max_bytes:
                self.exhausted = True
                return False
            self.spent += size
            return True

    def _accept(self):
        try:
            while not self.closed:
                try:
                    client, _ = self.listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self.closed:
                        return
                    time.sleep(POLL)
                    continue
                with self.lock:
                    refusal = ('broker_closed' if self.closed else 'lifetime' if time.monotonic() >= self.deadline
                               else 'byte_limit' if self.exhausted
                               else 'connection_limit' if len(self.connections) >= self.max_connections else None)
                    if refusal is None:
                        self.sequence += 1
                        thread = threading.Thread(target=self._serve, args=(self.sequence, client),
                                                  name='pebble-egress-tunnel', daemon=True)
                        self.connections[self.sequence] = {'sockets': [client], 'thread': thread}
                if refusal is not None:
                    self._refused(client, _Refused(503, refusal), self.routes is not None)
                    client.close()
                    continue
                thread.start()
        finally:
            self.listener.close()

    def _serve(self, ident, client):
        record, reason, routed = None, 'error', self.routes is not None
        try:
            try:
                head, rest = self._head(client)
                # CONNECT keeps its own path on a broker with an allowlist; route-only brokers refuse it.
                routed = routed and not (self.allow and head.startswith(b'CONNECT '))
                if routed:
                    request = self._request(head)
                else:
                    host, port = self._target(head)
                    addresses = self._addresses(host, port)
            except _Refused as refused:
                self._refused(client, refused, routed)
                return
            if routed:
                record = self._route_record(request['route'], request['method'], request['path_hash'], 'open',
                                            opened=True)
                reason = self._proxy(ident, client, request, rest, record)
                return
            record = self._record(host, port, True, 'open')
            upstream = self._connect(ident, addresses, port)
            if upstream is None:
                reason = 'broker_closed' if self.closed else 'upstream_unreachable'
                _reply(client, 503 if self.closed else 502)
                return
            client.sendall(ESTABLISHED)
            reason = self._relay(client, upstream, rest, record)
        except OSError:
            reason = 'broker_closed' if self.closed else 'connection_error'
        finally:
            with self.lock:
                entry = self.connections.pop(ident, None)
            for sock in (entry['sockets'] if entry else [client]):
                _shutdown(sock)
                sock.close()
            if record is not None:
                self._end(record, reason)

    def _head(self, client):
        data = bytearray()
        deadline = time.monotonic() + self.idle
        client.settimeout(POLL)
        while True:
            end = data.find(b'\r\n\r\n')
            if end != -1:
                if end + 4 > HEADER_LIMIT:
                    raise _Refused(431, 'header_too_large')
                return bytes(data[:end]), bytes(data[end + 4:])
            if len(data) >= HEADER_LIMIT:
                raise _Refused(431, 'header_too_large')
            if self.closed:
                raise _Refused(503, 'broker_closed')
            if time.monotonic() >= deadline:
                raise _Refused(408, 'header_timeout')
            try:
                chunk = client.recv(HEADER_LIMIT - len(data))
            except TimeoutError:
                continue
            except OSError:
                raise _Refused(None, 'incomplete_request') from None
            if not chunk:
                raise _Refused(None, 'incomplete_request')
            data += chunk

    def _target(self, head):
        try:
            line = head.split(b'\r\n', 1)[0].decode('ascii')
        except UnicodeDecodeError:
            raise _Refused(400, 'malformed_request') from None
        parts = line.split(' ')
        if len(parts) != 3 or not parts[0]:
            raise _Refused(400, 'malformed_request')
        method, target, version = parts
        if method != 'CONNECT':
            # Absolute-URI forwarding (GET http://...) and every other method.
            raise _Refused(405, 'method_not_allowed')
        if version != 'HTTP/1.1':
            raise _Refused(400, 'unsupported_version')
        match = TARGET.fullmatch(target.lower())
        if not match or not 1 <= int(match[2]) <= 65535:
            raise _Refused(400, 'malformed_target')
        shown, port = match[1], int(match[2])
        try:
            host = host_name(shown, self.loopback)
        except PolicyError:
            raise _Refused(403, 'ip_literal' if _literal(shown) is not None else 'invalid_host', shown, port) from None
        if (host, port) not in self.allow:
            raise _Refused(403, 'not_allowlisted', host, port)
        return host, port

    def _addresses(self, host, port):
        if _literal(host) is not None:
            return [host]  # A loopback literal, admitted only with loopback_targets.
        try:
            found = resolve(host, port)
        except (OSError, UnicodeError):
            raise _Refused(502, 'resolution_failed', host, port) from None
        if not isinstance(found, list) or not found:
            raise _Refused(502, 'resolution_failed', host, port)
        # Every answer must pass: a mixed answer is treated as rebinding.
        if not all(address_allowed(address, self.loopback) for address in found[:64]) or len(found) > 64:
            raise _Refused(403, 'refused_address', host, port)
        return found[:MAX_ADDRESSES]

    def _connect(self, ident, addresses, port):
        for address in addresses:
            sock = socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM)
            with self.lock:
                entry = self.connections.get(ident)
                if entry is None or self.closed:
                    sock.close()
                    return None
                entry['sockets'].append(sock)
            sock.settimeout(min(CONNECT_SECONDS, self.idle))
            try:
                sock.connect((address, port))  # The exact vetted address, never a re-resolution.
                return sock
            except OSError:
                with self.lock:
                    entry['sockets'].remove(sock)
                sock.close()
                if self.closed:
                    return None
        return None

    def _relay(self, client, upstream, rest, record):
        for sock in (client, upstream):
            sock.settimeout(self.idle)  # Bounds a blocked sendall too.
        if rest:
            if not self._spend(len(rest)):
                return 'byte_limit'
            upstream.sendall(rest)
            self._count(record, 'bytes_up', len(rest))
        selector = selectors.DefaultSelector()
        selector.register(client, selectors.EVENT_READ, (upstream, 'bytes_up'))
        selector.register(upstream, selectors.EVENT_READ, (client, 'bytes_down'))
        last = time.monotonic()
        try:
            while True:
                if self.closed:
                    return 'broker_closed'
                now = time.monotonic()
                if now >= self.deadline:
                    return 'lifetime'
                if now - last >= self.idle:
                    return 'idle'
                for key, _ in selector.select(timeout=POLL):
                    peer, field = key.data
                    data = key.fileobj.recv(CHUNK)
                    if not data:
                        # close() sets the flag before it shuts sockets down.
                        return 'broker_closed' if self.closed else 'eof'
                    if not self._spend(len(data)):
                        return 'byte_limit'
                    peer.sendall(data)
                    self._count(record, field, len(data))
                    last = time.monotonic()
        finally:
            selector.close()

    # Reverse-proxy routes

    def _request(self, head):
        """Parse and admit one route request: route, method, prefix, headers and body size."""
        try:
            lines = head.decode('ascii').split('\r\n')
        except UnicodeDecodeError:
            raise _Refused(400, 'malformed_request') from None
        parts = lines[0].split(' ')
        if len(parts) != 3 or not parts[0]:
            raise _Refused(400, 'malformed_request')
        method, target, version = parts
        if method == 'CONNECT':
            raise _Refused(405, 'connect_refused')
        if method == 'PRI' or version.startswith('HTTP/2'):
            raise _Refused(400, 'upgrade_refused')
        if version != 'HTTP/1.1':
            raise _Refused(400, 'unsupported_version')
        path, mark, query = target.partition('?')
        if (not path.startswith('/r/') or '#' in target
                or mark and (not QUERY_TEXT.fullmatch(query) or BAD_ESCAPE.search(query))):
            raise _Refused(400, 'malformed_target')
        if problem := path_problem(path):
            raise _Refused(400, problem)
        name, _, rest = path[3:].partition('/')
        route = self.routes.get(name)
        if route is None:
            raise _Refused(404, 'unknown_route')  # The unknown name is client text and is not logged.
        upstream = '/' + rest + mark + query
        fields = {'route': name, 'method': method if method in ROUTE_METHODS else None,
                  'path_hash': hashlib.sha256(upstream.encode()).hexdigest()}
        if method not in route['methods']:
            raise _Refused(405, 'method_not_allowed', allow=', '.join(route['methods']), **fields)
        if not within('/' + rest, route['path_prefix']):
            raise _Refused(403, 'prefix_refused', **fields)
        if len(lines) - 1 > MAX_HEADERS:
            raise _Refused(431, 'header_too_large', **fields)
        parsed = []
        for line in lines[1:]:
            key, sep, value = line.partition(':')
            if not sep or not TOKEN.fullmatch(key) or not FIELD.fullmatch(value):
                raise _Refused(400, 'malformed_header', **fields)
            parsed.append((key, value.strip(' \t')))
        listed = {t.strip().lower() for key, value in parsed if key.lower() == 'connection' for t in value.split(',')}
        headers, length, expect, counted = [], 0, False, False
        for key, value in parsed:
            lower = key.lower()
            if lower in ('upgrade', 'http2-settings') or lower == 'connection' and 'upgrade' in listed:
                raise _Refused(400, 'upgrade_refused', **fields)
            if lower == 'transfer-encoding':
                raise _Refused(411, 'transfer_encoding_refused', **fields)
            if lower == 'content-length':
                if counted or not LENGTH.fullmatch(value):
                    raise _Refused(400, 'malformed_header', **fields)
                counted, length = True, int(value)
            elif lower == 'expect':
                if value.lower() != '100-continue':
                    raise _Refused(417, 'expectation_refused', **fields)
                expect = True
            elif lower not in HOP and lower not in listed and lower != 'host':
                headers.append((key, value))
        with self.lock:
            room = self.max_bytes - self.spent
        if length > min(self.max_request, room):
            raise _Refused(413, 'request_too_large', **fields)
        return {**fields, 'path': upstream, 'headers': headers, 'length': length, 'expect': expect}

    def _proxy(self, ident, client, request, rest, record):
        """Forward one admitted route request; return the record's final reason."""
        route = self.routes.get(request['route'])
        grant = route['grant'] if route else None
        upstream = value = needle = None
        sent = False
        try:
            if route is None or self.closed:
                raise _Refused(503, 'broker_closed')
            addresses = self._addresses(route['host'], route['port'])
            name = None
            if grant is not None:
                # Only after address vetting, so a refused or rebinding answer never sees a credential.
                try:
                    name, value = self.credentials.header(grant, request['method'], route['origin'] + request['path'])
                except PolicyError as exc:
                    raise _Refused(403, getattr(exc, 'reason', 'credential_refused')) from None
                needle = value[value.rfind(b' ') + 1:]
            upstream = self._tls(ident, addresses, route)
            head = self._outgoing(request, route, name, value)
            try:
                self._send(upstream, head)
            finally:
                _zero(head)
            self._set(record, allowed=True, credential_used=name is not None)
            self._upload(client, upstream, request, rest, record, grant)
            stream = _Upstream(self, upstream, grant)
            code, headers = self._response_head(stream)
            out, framing = self._framing(code, headers, request, route)
            if needle and any(k.lower() == 'content-encoding' and v.strip().lower() not in ('', 'identity')
                              for k, v in out):
                # T7 F8: a compressed body would hide a credential echo from the reflection scan. The broker
                # asks for identity encoding, so an honest upstream is unaffected.
                raise _Refused(502, 'content_encoding_refused')
            reply = (f'HTTP/1.1 {code} {_phrase(code)}\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in out)
                     + 'Connection: close\r\n\r\n').encode('latin-1')
            if needle and needle in reply:
                raise _Refused(502, 'credential_reflected')
            if not self._spend(len(reply)):
                raise _Refused(503, 'byte_limit')
            self._send(client, reply)
            sent = True
            self._set(record, status=code)
            self._count(record, 'bytes', len(reply))
            return self._download(client, stream, framing, record, needle)
        except _Refused as refused:
            if self.closed:  # close() shuts sockets down, which surfaces here as EOF or truncation.
                refused = _Refused(503, 'broker_closed')
            if not sent and refused.status is not None:
                _reply(client, refused.status, refused.reason, refused.allow)
                self._set(record, status=refused.status)
            return refused.reason
        except OSError:
            reason = 'broker_closed' if self.closed else 'connection_error'
            if not sent:
                _reply(client, 503 if self.closed else 502, reason)
                self._set(record, status=503 if self.closed else 502)
            return reason
        finally:
            for buffer in (value, needle):
                if buffer is not None:
                    _zero(buffer)
            if upstream is not None:
                upstream.close()

    def _tls(self, ident, addresses, route):
        raw = self._connect(ident, addresses, route['port'])
        if raw is None:
            raise _Refused(503, 'broker_closed') if self.closed else _Refused(502, 'upstream_unreachable')
        # close() shuts sockets down from another thread, but the TLS object detaches
        # raw and owns state no other thread may touch. Register a duplicate
        # descriptor of the same connection instead; shutting it down ends the stream.
        shadow = raw.dup()
        with self.lock:
            sockets = self.connections[ident]['sockets']
            sockets.remove(raw)
            sockets.append(shadow)
        try:
            tls = self.tls.wrap_socket(raw, server_hostname=route['host'])
        except ssl.SSLCertVerificationError:
            raise _Refused(502, 'tls_verification_failed') from None
        except OSError:
            raise (_Refused(503, 'broker_closed') if self.closed else _Refused(502, 'tls_failed')) from None
        if tls.selected_alpn_protocol() not in (None, 'http/1.1'):
            tls.close()
            raise _Refused(502, 'upgrade_refused')
        return tls

    def _outgoing(self, request, route, name, value):
        """The upstream request head. The credential is copied only into a presized bytearray."""
        host, port = route['host'], route['port']
        shown = (f'[{host}]' if ':' in host else host) + ('' if port == 443 else f':{port}')
        drop = {route['header'], 'accept-encoding'} if name else set()
        lines = [f'{request["method"]} {request["path"]} HTTP/1.1', f'Host: {shown}',
                 *(f'{k}: {v}' for k, v in request['headers'] if k.lower() not in drop)]
        if request['length'] or request['method'] in ('POST', 'PUT', 'PATCH'):
            lines.append(f'Content-Length: {request["length"]}')
        if name:
            lines.append('Accept-Encoding: identity')  # Uncompressed, so the reflection scan sees the payload.
        lines.append('Connection: close')
        plain = ('\r\n'.join(lines) + '\r\n').encode('ascii')
        if name is None:
            return bytearray(plain + b'\r\n')
        return _join(plain, name.encode('ascii'), b': ', value, b'\r\n\r\n')

    def _send(self, sock, data):
        sock.settimeout(self.idle)  # sendall's timeout bounds the whole call.
        sock.sendall(data)

    def _halt(self, grant, last):
        now = time.monotonic()
        if self.closed:
            return 'broker_closed'
        if now >= self.deadline:
            return 'lifetime'
        if now - last >= self.idle:
            return 'idle'
        if grant is not None and (state := self.credentials.state(grant)) != 'active':
            return 'expired' if state == 'expired' else 'revoked'
        return None

    def _pull(self, sock, size, grant, last):
        """recv in POLL steps, so close, lifetime, idle and revocation apply while waiting."""
        sock.settimeout(POLL)
        while True:
            if halt := self._halt(grant, last):
                raise _Refused(HALT[halt], halt)
            try:
                return sock.recv(size)
            except TimeoutError:
                continue
            except ssl.SSLEOFError:
                return b''  # An upstream that closes without close_notify; framing decides truncation.

    def _upload(self, client, upstream, request, rest, record, grant):
        """Relay exactly the declared request body."""
        remaining, data = request['length'], rest[:request['length']]
        if request['expect'] and remaining > len(data):
            self._send(client, b'HTTP/1.1 100 Continue\r\n\r\n')
        last = time.monotonic()
        while True:
            if data:
                if not self._spend(len(data)):
                    raise _Refused(503, 'byte_limit')
                self._send(upstream, data)
                self._count(record, 'bytes', len(data))
                remaining, last = remaining - len(data), time.monotonic()
            if remaining <= 0:
                return
            data = self._pull(client, min(CHUNK, remaining), grant, last)
            if not data:
                raise _Refused(None, 'incomplete_request')

    def _response_head(self, stream):
        """Status and headers of the final response; interim 1xx responses are dropped and 101 refused."""
        for _ in range(5):
            line = stream.line()
            match = STATUS_LINE.fullmatch(line)
            if not match:
                raise _Refused(502, 'malformed_response')
            code, headers, size = int(match[1]), [], len(line)
            while line := stream.line():
                size += len(line) + 2
                if size > RESPONSE_HEADER_LIMIT or len(headers) >= 128:
                    raise _Refused(502, 'response_header_too_large')
                key, sep, value = line.partition(b':')
                if not sep or not TOKEN.fullmatch(key.decode('latin-1')) or BAD_FIELD.search(value):
                    raise _Refused(502, 'malformed_response')
                headers.append((key.decode('ascii'), value.strip(b' \t').decode('latin-1')))
            if code == 101:
                raise _Refused(502, 'upgrade_refused')
            if code >= 200:
                return code, headers
        raise _Refused(502, 'malformed_response')

    def _framing(self, code, headers, request, route):
        """Client headers without hop-by-hop fields, and the upstream body framing."""
        listed = {t.strip().lower() for key, value in headers if key.lower() == 'connection' for t in value.split(',')}
        coding = [t.strip().lower() for key, value in headers if key.lower() == 'transfer-encoding' for t in value.split(',')]
        lengths = {value for key, value in headers if key.lower() == 'content-length'}
        if coding and (coding != ['chunked'] or lengths):
            raise _Refused(502, 'unsupported_transfer_encoding')
        if len(lengths) > 1 or lengths and not LENGTH.fullmatch(next(iter(lengths))):
            raise _Refused(502, 'malformed_response')
        if request['method'] == 'HEAD' or code in (204, 304):
            framing = ('length', 0)
        else:
            framing = ('chunked', 0) if coding else ('length', int(next(iter(lengths)))) if lengths else ('close', 0)
        out = []
        for key, value in headers:
            lower = key.lower()
            if lower in HOP or lower in listed or lower == 'alt-svc':
                continue
            if lower == 'location' and 300 <= code < 400:
                value = self._redirect(request, route, value)
            out.append((key, value))
        return out, framing

    def _redirect(self, request, route, location):
        """Map a same-origin Location into the route; any other origin is a 502. Never followed."""
        if not re.fullmatch(r'[\x21-\x7e]{1,2048}', location) or '\\' in location:
            raise _Refused(502, 'unsafe_redirect')
        try:
            parts = urlsplit(urljoin(route['origin'] + request['path'], location))
            port = parts.port or 443
        except ValueError:
            raise _Refused(502, 'unsafe_redirect') from None
        if (parts.scheme != 'https' or parts.username is not None or parts.password is not None
                or parts.hostname != route['host'] or port != route['port']):
            raise _Refused(502, 'cross_origin_redirect')
        return f'/r/{request["route"]}{parts.path or "/"}' + (f'?{parts.query}' if parts.query else '')

    def _body(self, stream, framing):
        """Payload chunks of the upstream body, decoded from its framing."""
        kind, size = framing
        if kind == 'close':
            while data := stream.read(CHUNK):
                yield data
            return
        if kind == 'length':
            yield from _exact(stream, size)
            return
        while True:
            match = CHUNK_LINE.fullmatch(stream.line())
            if not match:
                raise _Refused(None, 'malformed_response')
            if (size := int(match[1], 16)) == 0:
                break
            yield from _exact(stream, size)
            if stream.line() != b'':
                raise _Refused(None, 'malformed_response')
        for _ in range(64):  # Trailers are dropped.
            if stream.line() == b'':
                return
        raise _Refused(None, 'malformed_response')

    def _download(self, client, stream, framing, record, needle):
        """Stream the body to the client. With a credential, hold back len(secret) - 1 bytes and scan,
        so a reflected credential is never forwarded whole."""
        keep, pending = (len(needle) - 1 if needle else 0), bytearray()
        try:
            for data in self._body(stream, framing):
                if not self._spend(len(data)):
                    return 'byte_limit'
                pending += data
                if needle and needle in pending:
                    return 'credential_reflected'
                if (cut := len(pending) - keep) > 0:
                    self._send(client, pending[:cut])
                    self._count(record, 'bytes', cut)
                    del pending[:cut]
            if pending:
                self._send(client, pending)
                self._count(record, 'bytes', len(pending))
            return 'complete'
        except _Refused as refused:
            return 'broker_closed' if self.closed else refused.reason
        finally:
            _zero(pending)


def _reply(sock, status, reason=None, allow=None):
    extra = f'Allow: {allow or "CONNECT"}\r\n' if status == 405 else ''
    if reason is not None:
        extra += f'X-Pebble-Egress: {reason}\r\n'  # A fixed refusal code, never client text.
    try:
        sock.settimeout(2)
        sock.sendall(f'HTTP/1.1 {status} {STATUS[status]}\r\n{extra}Connection: close\r\nContent-Length: 0\r\n\r\n'.encode())
    except OSError:
        pass


def _exact(stream, size):
    while size > 0:
        data = stream.read(min(CHUNK, size))
        if not data:
            raise _Refused(None, 'upstream_truncated')
        size -= len(data)
        yield data


def _phrase(code):
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return 'Status'


def _join(*parts):
    """One presized bytearray, so no reallocation leaves a stray copy of a credential."""
    out, at = bytearray(sum(len(part) for part in parts)), 0
    for part in parts:
        out[at:at + len(part)] = part
        at += len(part)
    return out


def _zero(buffer):
    buffer[:] = bytes(len(buffer))


def _shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
