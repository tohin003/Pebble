"""Allowlisted CONNECT broker and reverse-proxy routes: admission, address vetting, limits, teardown and sandbox reach.

Every upstream is a local fixture listener inside this process; no test needs
outbound network. Route upstreams are loopback HTTPS fixtures with a CA made by
/usr/bin/openssl, reached only with loopback_targets plus the test-only ca_file.
The sandboxed client tests run the system Python under sandbox-exec with the
broker's network_profile and observe what it can reach and read.
"""

import hashlib
import json
import secrets
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import egress
from pebble.credential_broker import CredentialBroker
from pebble.egress import EgressBroker, address_allowed, allowlist, host_name, network_profile, route_table
from pebble.policy import PolicyError

MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()


# Restated 2026-10-30 (the lead): Xcode 27.1 became the selected developer directory on this Mac (2026-10-29), and
# /usr/bin/python3, Apple's shim, follows xcode-select into Xcode, which these profiles do not allow ("unable to
# load libxcrun", on 1.9.27's tree too). The shim launches name the developer directory their own profile allows,
# as git_workflows and recipes do; what the sandbox allows and refuses is unchanged.
CLT_ENV = {'DEVELOPER_DIR': '/Library/Developer/CommandLineTools'} if Path('/Library/Developer/CommandLineTools').is_dir() else {}

def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class Upstream:
    """A loopback fixture service: echo, silent (holds the connection) or blast."""

    def __init__(self, mode='echo', payload=b''):
        self.mode, self.payload = mode, payload
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(16)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.accepted += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(0.1)
        try:
            if self.mode == 'blast':
                conn.sendall(self.payload)
            while not self.stop.is_set():
                try:
                    data = conn.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    break
                if self.mode == 'echo':
                    conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


@pytest.fixture
def upstreams():
    made = []

    def make(mode='echo', payload=b''):
        made.append(Upstream(mode, payload))
        return made[-1]
    yield make
    for item in made:
        item.close()


@pytest.fixture
def brokers():
    made = []

    def make(*args, **kwargs):
        made.append(EgressBroker(*args, **kwargs).start())
        return made[-1]
    yield made, make
    for broker in made:
        broker.close()


def send(broker, raw, timeout=5):
    """Send raw request bytes; return (socket, response head)."""
    sock = socket.create_connection(('127.0.0.1', broker.port), timeout=timeout)
    sock.sendall(raw)
    data = b''
    while b'\r\n\r\n' not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return sock, data


def connect(broker, target):
    return send(broker, f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode())


def status(head):
    return int(head.split(b' ', 2)[1]) if head.startswith(b'HTTP/1.1 ') else None


def recv_all(sock, timeout=5):
    sock.settimeout(timeout)
    data = b''
    try:
        while chunk := sock.recv(65536):
            data += chunk
    except (ConnectionResetError, TimeoutError):
        pass
    return data


def test_allowlisted_tunnel_relays_bytes_and_logs(brokers, upstreams):
    upstream = upstreams('echo')
    made, make = brokers
    broker = make([('localhost', upstream.port)], loopback_targets=True)
    sock, head = connect(broker, f'localhost:{upstream.port}')
    assert head == egress.ESTABLISHED
    sock.sendall(b'ping')
    assert sock.recv(4) == b'ping'
    sock.close()
    assert wait_for(lambda: broker.log() and broker.log()[-1]['ended'] is not None)
    record = broker.log()[-1]
    assert set(record) == {'host', 'port', 'allowed', 'reason', 'bytes_up', 'bytes_down', 'started', 'ended'}
    assert record['host'] == 'localhost' and record['port'] == upstream.port and record['allowed'] is True
    assert (record['bytes_up'], record['bytes_down'], record['reason']) == (4, 4, 'eof')
    assert record['ended'] >= record['started']
    assert broker.url == f'http://127.0.0.1:{broker.port}'


def test_denied_by_host_and_by_port_without_resolving(brokers, upstreams, monkeypatch):
    upstream = upstreams('echo')
    calls = []
    monkeypatch.setattr(egress, 'resolve', lambda host, port: calls.append(host) or ['127.0.0.1'])
    made, make = brokers
    broker = make([('registry.pebble.test', upstream.port)], loopback_targets=True)
    for target in (f'registry.pebble.test:{upstream.port + 1}', f'other.pebble.test:{upstream.port}',
                   f'sub.registry.pebble.test:{upstream.port}'):
        sock, head = connect(broker, target)
        assert status(head) == 403
        sock.close()
    assert calls == [] and upstream.accepted == 0
    assert [(r['host'], r['allowed'], r['reason']) for r in broker.log()] == [
        ('registry.pebble.test', False, 'not_allowlisted'), ('other.pebble.test', False, 'not_allowlisted'),
        ('sub.registry.pebble.test', False, 'not_allowlisted')]
    # The exact pair is admitted, resolved by the broker and tunnelled to the vetted address.
    sock, head = connect(broker, f'Registry.Pebble.Test:{upstream.port}')
    assert head == egress.ESTABLISHED and calls == ['registry.pebble.test']
    sock.sendall(b'x')
    assert sock.recv(1) == b'x'
    sock.close()


@pytest.mark.parametrize('host', ['127.0.0.1', '::1', '[::1]', '10.0.0.1', 'xn--bcher-kva.example', 'ab--cd.example',
                                  '*.npmjs.org', 'a..b', 'example.com.', '.example.com', 'Registry.npmjs.org',
                                  'example.123', '0x7f000001', '2130706433', '', 'bücher.example', 'a_b.example',
                                  '-a.example', 'a' * 64 + '.example', 7])
def test_allowlist_refuses_ip_literals_idna_wildcards_and_empty_labels(host):
    with pytest.raises(PolicyError):
        EgressBroker([(host, 443)])


def test_allowlist_shape_ports_and_loopback_literals():
    for bad in ([], [('registry.npmjs.org', 0)], [('registry.npmjs.org', True)], [('registry.npmjs.org', '443')],
                [('registry.npmjs.org',)], ['registry.npmjs.org'], [('a.example', 443)] * 17, 'registry.npmjs.org'):
        with pytest.raises(PolicyError):
            allowlist(bad)
    assert allowlist([['registry.npmjs.org', 443], ('registry.npmjs.org', 443)]) == (('registry.npmjs.org', 443),)
    assert host_name('127.0.0.1', loopback=True) == '127.0.0.1'
    assert host_name('[::1]', loopback=True) == '::1'
    for bad in ('10.0.0.1', '169.254.1.1', '[fe80::1]'):
        with pytest.raises(PolicyError):
            host_name(bad, loopback=True)
    for value in (0, -1, 3601, True, '1', None):
        with pytest.raises(PolicyError):
            EgressBroker([('localhost', 443)], idle_seconds=value)
    for kwargs in ({'max_connections': 0}, {'max_connections': 65}, {'max_bytes': 0}, {'loopback_targets': 1}):
        with pytest.raises(PolicyError):
            EgressBroker([('localhost', 443)], **kwargs)


def test_ip_literal_requests_refused_unless_loopback_fixtures(brokers, upstreams):
    upstream = upstreams('echo')
    made, make = brokers
    broker = make([('localhost', upstream.port)])
    for target in (f'127.0.0.1:{upstream.port}', f'[::1]:{upstream.port}', f'2130706433:{upstream.port}'):
        sock, head = connect(broker, target)
        assert status(head) == 403
        sock.close()
    assert [r['reason'] for r in broker.log()] == ['ip_literal', 'ip_literal', 'invalid_host']
    assert upstream.accepted == 0
    fixture = make([('127.0.0.1', upstream.port)], loopback_targets=True)
    sock, head = connect(fixture, f'127.0.0.1:{upstream.port}')
    assert head == egress.ESTABLISHED
    sock.close()


def test_names_resolving_to_local_or_private_addresses_are_refused(brokers, upstreams, monkeypatch):
    upstream = upstreams('echo')
    answers = {'loop.pebble.test': ['127.0.0.1'], 'mixed.pebble.test': ['93.184.216.34', '10.0.0.5'],
               'mapped.pebble.test': ['::ffff:127.0.0.1'], 'link.pebble.test': ['fe80::1%lo0'],
               'empty.pebble.test': []}

    def fake(host, port):
        if host == 'broken.pebble.test':
            raise socket.gaierror('no such host')
        return answers[host]
    monkeypatch.setattr(egress, 'resolve', fake)
    made, make = brokers
    names = [*answers, 'broken.pebble.test']
    broker = make([(name, upstream.port) for name in names])
    for name in names:
        sock, head = connect(broker, f'{name}:{upstream.port}')
        assert status(head) == (502 if name in ('empty.pebble.test', 'broken.pebble.test') else 403)
        sock.close()
    assert [r['reason'] for r in broker.log()] == ['refused_address'] * 4 + ['resolution_failed'] * 2
    assert upstream.accepted == 0
    # Loopback fixtures admit loopback answers only; a private answer stays refused.
    answers['loop.pebble.test'], answers['private.pebble.test'] = ['::1', '127.0.0.1'], ['192.168.1.10']
    fixture = make([('loop.pebble.test', upstream.port), ('private.pebble.test', upstream.port)], loopback_targets=True)
    sock, head = connect(fixture, f'loop.pebble.test:{upstream.port}')
    assert head == egress.ESTABLISHED
    sock.close()
    sock, head = connect(fixture, f'private.pebble.test:{upstream.port}')
    assert status(head) == 403
    sock.close()


def test_address_vetting_covers_ipv4_ipv6_and_embedded_forms():
    public = ['93.184.216.34', '151.101.1.63', '2606:4700::6810:84e5', '2a04:4e42::223']
    # 6to4 (2002::/16) is never globally reachable for ipaddress, even with a public embedded IPv4.
    refused = ['2002:5db8:d822::1', '127.0.0.1', '127.1.2.3', '10.1.2.3', '172.16.0.1', '192.168.0.1', '169.254.169.254', '100.64.0.1',
               '0.0.0.0', '224.0.0.1', '239.255.255.250', '240.0.0.1', '255.255.255.255', '192.0.2.1', '198.18.0.1',
               '::1', '::', 'fe80::1', 'fe80::1%en0', 'fc00::1', 'fd12:3456::1', 'ff02::1', '::ffff:127.0.0.1',
               '::ffff:10.0.0.1', '2002:7f00:1::1', '2002:0a00:1::1', '64:ff9b::7f00:1', '64:ff9b::a00:1',
               '2001:db8::1', 'fec0::1', 'not-an-address', '', None]
    assert all(address_allowed(value) for value in public)
    assert not any(address_allowed(value) for value in refused)
    assert address_allowed('127.0.0.1', loopback=True) and address_allowed('::1', loopback=True)
    assert address_allowed('::ffff:127.0.0.1', loopback=True)
    assert not address_allowed('10.0.0.1', loopback=True) and not address_allowed('169.254.169.254', loopback=True)


def test_non_connect_methods_are_refused_without_echo(brokers, upstreams):
    upstream = upstreams('echo')
    made, make = brokers
    broker = make([('localhost', upstream.port)], loopback_targets=True)
    expected = b'HTTP/1.1 405 Method Not Allowed\r\nAllow: CONNECT\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'
    for raw in (b'GET http://secret.pebble.test/TOKEN-123 HTTP/1.1\r\nHost: secret.pebble.test\r\n\r\n',
                f'GET http://localhost:{upstream.port}/ HTTP/1.1\r\n\r\n'.encode(), b'POST / HTTP/1.1\r\n\r\n',
                f'connect localhost:{upstream.port} HTTP/1.1\r\n\r\n'.encode()):
        sock, head = send(broker, raw)
        assert head == expected and b'TOKEN' not in head
        assert recv_all(sock) == b''
        sock.close()
    for raw in (f'CONNECT localhost:{upstream.port} HTTP/1.0\r\n\r\n'.encode(), b'CONNECT localhost HTTP/1.1\r\n\r\n',
                b'CONNECT localhost:99999 HTTP/1.1\r\n\r\n', b'\xff\xfe\r\n\r\n', b'CONNECT  x HTTP/1.1\r\n\r\n'):
        sock, head = send(broker, raw)
        assert status(head) == 400 and b'localhost' not in head
        sock.close()
    assert upstream.accepted == 0
    reasons = [r['reason'] for r in broker.log()]
    assert reasons[:4] == ['method_not_allowed'] * 4
    assert set(reasons[4:]) <= {'unsupported_version', 'malformed_target', 'malformed_request'}
    assert all(r['allowed'] is False and r['ended'] == r['started'] for r in broker.log())


def test_request_headers_are_limited_to_8_kb(brokers, upstreams):
    upstream = upstreams('echo')
    made, make = brokers
    broker = make([('localhost', upstream.port)], loopback_targets=True)
    sock, head = send(broker, f'CONNECT localhost:{upstream.port} HTTP/1.1\r\nX-Pad: {"a" * 9000}\r\n\r\n'.encode())
    assert status(head) == 431 and b'aaaa' not in head
    sock.close()
    # Just inside the bound is admitted.
    line = f'CONNECT localhost:{upstream.port} HTTP/1.1\r\nX-Pad: '.encode()
    sock, head = send(broker, line + b'a' * (egress.HEADER_LIMIT - len(line) - 6) + b'\r\n\r\n')
    assert head == egress.ESTABLISHED
    sock.close()
    assert [r['reason'] for r in broker.log()][0] == 'header_too_large' and wait_for(lambda: upstream.accepted == 1)


def test_byte_limit_stops_the_tunnel_and_later_admissions(brokers, upstreams):
    upstream = upstreams('echo')
    made, make = brokers
    broker = make([('localhost', upstream.port)], loopback_targets=True, max_bytes=4000)
    sock, head = connect(broker, f'localhost:{upstream.port}')
    assert head == egress.ESTABLISHED
    sock.sendall(b'z' * 3000)
    received = recv_all(sock)
    sock.close()
    assert wait_for(lambda: broker.log()[-1]['ended'] is not None)
    record = broker.log()[-1]
    assert record['reason'] == 'byte_limit' and record['bytes_up'] + record['bytes_down'] <= 4000
    assert len(received) == record['bytes_down']
    sock, head = connect(broker, f'localhost:{upstream.port}')
    assert status(head) == 503
    sock.close()
    assert broker.log()[-1]['reason'] == 'byte_limit' and broker.log()[-1]['allowed'] is False


def test_idle_tunnels_and_idle_requests_are_closed(brokers, upstreams):
    upstream = upstreams('silent')
    made, make = brokers
    broker = make([('localhost', upstream.port)], loopback_targets=True, idle_seconds=0.5)
    sock, head = connect(broker, f'localhost:{upstream.port}')
    assert head == egress.ESTABLISHED
    started = time.monotonic()
    assert recv_all(sock, timeout=5) == b''
    assert time.monotonic() - started < 4
    sock.close()
    assert wait_for(lambda: broker.log()[-1]['reason'] == 'idle')
    quiet = socket.create_connection(('127.0.0.1', broker.port), timeout=5)
    quiet.sendall(b'CONNECT localhost')
    assert status(recv_all(quiet)) == 408
    quiet.close()
    assert broker.log()[-1]['reason'] == 'header_timeout'


def test_aborted_requests_are_logged_without_reaching_upstream(brokers, upstreams):
    upstream = upstreams('echo')
    made, make = brokers
    broker = make([('localhost', upstream.port)], loopback_targets=True)
    quiet = socket.create_connection(('127.0.0.1', broker.port), timeout=5)
    quiet.sendall(b'CONNECT local')
    quiet.close()
    reset = socket.create_connection(('127.0.0.1', broker.port), timeout=5)
    reset.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b'\x01\x00\x00\x00\x00\x00\x00\x00')
    reset.close()
    assert wait_for(lambda: len(broker.log()) == 2)
    assert [(r['allowed'], r['reason']) for r in broker.log()] == [(False, 'incomplete_request')] * 2
    assert upstream.accepted == 0 and wait_for(lambda: broker.connections == {})


def test_lifetime_bounds_tunnels_and_admission(brokers, upstreams):
    upstream = upstreams('silent')
    made, make = brokers
    broker = make([('localhost', upstream.port)], loopback_targets=True, lifetime_seconds=0.6)
    sock, head = connect(broker, f'localhost:{upstream.port}')
    assert head == egress.ESTABLISHED
    assert recv_all(sock) == b''
    sock.close()
    assert wait_for(lambda: broker.log()[-1]['reason'] == 'lifetime')
    sock, head = connect(broker, f'localhost:{upstream.port}')
    assert status(head) == 503 and broker.log()[-1]['reason'] == 'lifetime'
    sock.close()


def test_close_tears_down_active_tunnels_and_is_idempotent(brokers, upstreams):
    upstream = upstreams('silent')
    made, make = brokers
    broker = make([('localhost', upstream.port)], loopback_targets=True)
    tunnels = []
    for _ in range(3):
        sock, head = connect(broker, f'localhost:{upstream.port}')
        assert head == egress.ESTABLISHED
        tunnels.append(sock)
    pending = socket.create_connection(('127.0.0.1', broker.port), timeout=5)  # Still in its header phase.
    assert wait_for(lambda: len(broker.connections) == 4)
    started = time.monotonic()
    assert broker.close() is True
    assert time.monotonic() - started < egress.JOIN_SECONDS
    for sock in tunnels:
        assert recv_all(sock) == b''
        sock.close()
    pending.close()
    assert broker.connections == {} and not broker.acceptor.is_alive()
    assert [r['reason'] for r in broker.log() if r['allowed']] == ['broker_closed'] * 3
    assert broker.close() is True
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', broker.port), timeout=2)
    with pytest.raises(PolicyError):
        broker.start()


def test_concurrent_connections_are_bounded(brokers, upstreams):
    upstream = upstreams('silent')
    made, make = brokers
    broker = make([('localhost', upstream.port)], loopback_targets=True, max_connections=2)
    first, head = connect(broker, f'localhost:{upstream.port}')
    second, head2 = connect(broker, f'localhost:{upstream.port}')
    assert head == head2 == egress.ESTABLISHED
    third, head3 = connect(broker, f'localhost:{upstream.port}')
    assert status(head3) == 503 and broker.log()[-1]['reason'] == 'connection_limit'
    third.close()
    first.close()
    assert wait_for(lambda: len(broker.connections) == 1)
    again, head = connect(broker, f'localhost:{upstream.port}')
    assert head == egress.ESTABLISHED
    again.close()
    second.close()
    assert wait_for(lambda: upstream.accepted == 3)


def test_log_keeps_only_the_most_recent_256_records(brokers, upstreams):
    upstream = upstreams('echo')
    made, make = brokers
    broker = make([('localhost', upstream.port)], loopback_targets=True)
    for _ in range(300):
        sock, head = send(broker, b'GET / HTTP/1.1\r\n\r\n')
        sock.close()
    sock, head = connect(broker, 'last.pebble.test:443')
    sock.close()
    log = broker.log()
    assert len(log) == 256
    assert log[-1]['host'] == 'last.pebble.test' and log[-1]['reason'] == 'not_allowlisted'
    assert all(r['reason'] == 'method_not_allowed' for r in log[:-1])
    log[-1]['host'] = 'mutated'
    assert broker.log()[-1]['host'] == 'last.pebble.test'


def test_network_profile_fragment():
    assert network_profile(4321) == '(allow network-outbound (remote tcp "localhost:4321"))'
    for bad in (0, 65536, '4321', True, None):
        with pytest.raises(PolicyError):
            network_profile(bad)


CLIENT = r'''
import errno, json, socket, sys
broker, target, direct = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
out = {}
s = socket.create_connection(("127.0.0.1", broker), timeout=5)
s.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target, target)).encode())
head = b""
while b"\r\n\r\n" not in head:
    head += s.recv(1)
out["head"] = head.decode()
s.sendall(b"through-broker")
out["echo"] = s.recv(64).decode()
s.close()
for name, family, address in (("direct", socket.AF_INET, ("127.0.0.1", direct)), ("direct6", socket.AF_INET6, ("::1", direct)),
                              ("public", socket.AF_INET, ("93.184.216.34", 443)), ("public6", socket.AF_INET6, ("2606:4700::6810:84e5", 443))):
    try:
        c = socket.socket(family)
        c.settimeout(3)
        c.connect(address)
        out[name] = "connected"
    except OSError as e:
        out[name] = e.errno
for name, path in (("dns", "/private/var/run/mDNSResponder"),):
    try:
        u = socket.socket(socket.AF_UNIX)
        u.connect(path)
        out[name] = "connected"
    except OSError as e:
        out[name] = e.errno
try:
    socket.getaddrinfo("registry.npmjs.org", 443)
    out["resolve"] = "resolved"
except OSError as e:
    out["resolve"] = "failed"
print(json.dumps(out))
'''


def test_sandboxed_client_reaches_only_the_broker(tmp_path, brokers, upstreams):
    if not MAC:
        raise AssertionError('This product surface requires macOS sandbox-exec; it is not skipped.')
    upstream = upstreams('echo')
    made, make = brokers
    broker = make([('localhost', upstream.port)], loopback_targets=True)
    script = tmp_path / 'client.py'
    script.write_text(CLIENT)
    quote = json.dumps
    handles = ' '.join('(literal %s)' % quote(str(p)) for p in sorted(set(tmp_path.resolve().parents), key=str))
    profile = f'''(version 1) (deny default)
      (allow process-fork) (allow process-exec (subpath "/usr/bin") (subpath "/Library/Developer/CommandLineTools"))
      (allow sysctl-read) (allow file-read-metadata)
      (allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
      (allow file-read* {handles} (subpath {quote(str(tmp_path.resolve()))}) (subpath "/Library/Developer/CommandLineTools")
        (subpath "/System") (subpath "/usr") (subpath "/private/var/db/dyld") (literal "/dev/null") (literal "/dev/urandom"))
      (allow file-write* (literal "/dev/null"))
      {network_profile(broker.port)}'''
    result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, '/usr/bin/python3', '-I', str(script.resolve()),
                             str(broker.port), f'localhost:{upstream.port}', str(upstream.port)],
                            capture_output=True, text=True, timeout=60, env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), **CLT_ENV})
    assert result.returncode == 0, result.stderr[-2000:]
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out['head'].startswith('HTTP/1.1 200') and out['echo'] == 'through-broker'
    # Sandbox denial (EPERM), not a routing failure, for every other destination.
    assert out['direct'] == out['public'] == out['public6'] == 1
    assert out['direct6'] == 1 and out['dns'] == 1 and out['resolve'] == 'failed'
    assert upstream.accepted == 1
    record = [r for r in broker.log() if r['allowed']][-1]
    assert record['host'] == 'localhost' and record['bytes_up'] == len('through-broker')


# Reverse-proxy routes (X04)

SECRET = 'w3-route-secret-' + secrets.token_hex(16)
ROUTE_KEYS = {'route', 'method', 'path_hash', 'status', 'bytes', 'credential_used', 'allowed', 'reason', 'started', 'ended'}


@pytest.fixture(scope='module')
def tls(tmp_path_factory):
    folder = tmp_path_factory.mktemp('tls').resolve()
    (folder / 'ca.cnf').write_text('[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = Pebble W3 Fixture CA\n'
                                   '[v3_ca]\nbasicConstraints = critical,CA:TRUE\nkeyUsage = critical,keyCertSign,cRLSign\n'
                                   'subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid:always\n')
    (folder / 'leaf.cnf').write_text('[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = localhost\n'
                                     '[v3_leaf]\nbasicConstraints = critical,CA:FALSE\n'
                                     'keyUsage = critical,digitalSignature,keyEncipherment\nextendedKeyUsage = serverAuth\n'
                                     'subjectAltName = DNS:localhost,IP:127.0.0.1\nsubjectKeyIdentifier = hash\n'
                                     'authorityKeyIdentifier = keyid\n')
    (folder / 'rogue.cnf').write_text('[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = localhost\n'
                                      '[v3_rogue]\nbasicConstraints = critical,CA:FALSE\n'
                                      'subjectAltName = DNS:localhost,IP:127.0.0.1\n')
    openssl = '/usr/bin/openssl'
    for argv in ([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '2', '-keyout', 'ca.key',
                  '-out', 'ca.pem', '-config', 'ca.cnf', '-extensions', 'v3_ca'],
                 [openssl, 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'leaf.key', '-out', 'leaf.csr',
                  '-config', 'leaf.cnf'],
                 [openssl, 'x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-set_serial', '4343',
                  '-days', '2', '-sha256', '-extfile', 'leaf.cnf', '-extensions', 'v3_leaf', '-out', 'leaf.pem'],
                 [openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '2', '-keyout', 'rogue.key',
                  '-out', 'rogue.pem', '-config', 'rogue.cnf', '-extensions', 'v3_rogue']):
        result = subprocess.run(argv, cwd=folder, capture_output=True, text=True, timeout=60,
                                env={'PATH': '/usr/bin:/bin', 'HOME': str(folder), 'RANDFILE': str(folder / '.rnd')})
        assert result.returncode == 0, result.stderr
    contexts = []
    for name in ('leaf', 'rogue'):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(folder / f'{name}.pem', folder / f'{name}.key')
        contexts.append(context)
    return SimpleNamespace(ca=str(folder / 'ca.pem'), context=contexts[0], rogue=contexts[1])


def reply(status=200, body=b'', headers=()):
    head = f'HTTP/1.1 {status} Fixture\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in headers)
    return [head.encode() + f'Content-Length: {len(body)}\r\n\r\n'.encode() + body]


def slow(request):
    yield b'HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n'
    for _ in range(400):
        time.sleep(0.025)
        yield b'x' * 10


class HTTPSUpstream:
    """A loopback HTTPS fixture. handlers map a path (without query) to request -> iterable of raw bytes."""

    def __init__(self, context, handlers):
        self.context, self.handlers, self.requests = context, dict(handlers), []
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(16)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(10)
        try:
            secure = self.context.wrap_socket(conn, server_side=True)
        except OSError:
            conn.close()
            return
        try:
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = secure.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b'\r\n\r\n')
            lines = head.decode('latin-1').split('\r\n')
            method, target, _ = lines[0].split(' ')
            headers = [(k.strip(), v.strip()) for k, v in (line.split(':', 1) for line in lines[1:])]
            length = int({k.lower(): v for k, v in headers}.get('content-length', 0))
            while len(body) < length and (chunk := secure.recv(65536)):
                body += chunk
            request = {'method': method, 'target': target, 'headers': headers, 'body': body}
            self.requests.append(request)
            for piece in self.handlers.get(target.split('?', 1)[0], lambda r: reply(404))(request):
                if self.stop.is_set():
                    break
                secure.sendall(piece)
        except OSError:
            pass
        finally:
            secure.close()

    def close(self):
        self.stop.set()
        self.sock.close()


@pytest.fixture
def https(tls):
    made = []

    def make(handlers, context=None):
        made.append(HTTPSUpstream(context or tls.context, handlers))
        return made[-1]
    yield make
    for item in made:
        item.close()


def spec(upstream, grant=None, methods=('GET', 'POST'), prefix='/v1'):
    return {'api': {'origin': f'https://localhost:{upstream.port}', 'methods': list(methods), 'path_prefix': prefix,
                    'grant': grant}}


def ask(broker, raw, timeout=10):
    """One raw request through the broker: (status, lowercased headers, body, raw response)."""
    sock = socket.create_connection(('127.0.0.1', broker.port), timeout=timeout)
    sock.sendall(raw)
    data = recv_all(sock, timeout)
    sock.close()
    head, _, body = data.partition(b'\r\n\r\n')
    headers = {}
    for line in head.decode('latin-1').split('\r\n')[1:]:
        key, _, value = line.partition(':')
        headers[key.strip().lower()] = value.strip()
    return status(head), headers, body, data


def get(path, extra=''):
    return f'GET {path} HTTP/1.1\r\nHost: broker\r\n{extra}\r\n'.encode()


def settled(broker):
    assert wait_for(lambda: broker.log() and broker.log()[-1]['ended'] is not None)
    return broker.log()[-1]


def sent_headers(request):
    found = {}
    for key, value in request['headers']:
        found.setdefault(key.lower(), []).append(value)
    return found


def test_route_injects_the_credential_and_strips_hop_by_hop_headers(brokers, tls, https):
    def items(request):
        return [b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: X-Up-Drop\r\nX-Up-Drop: 1\r\n'
                b'Keep-Alive: timeout=5\r\nAlt-Svc: h3=":443"\r\nUpgrade: h2\r\nX-Up-Keep: ok\r\n\r\n'
                b'5\r\nhello\r\n6;ext=1\r\n world\r\n0\r\nX-Trailer: t\r\n\r\n']
    upstream = https({'/v1/items': items})
    credentials = CredentialBroker(lambda ref: SECRET)
    grant = credentials.grant('vault:api', f'https://localhost:{upstream.port}', ['GET'], '/v1', time.time() + 60)
    made, make = brokers
    broker = make(None, routes=spec(upstream, grant, ['GET']), credentials=credentials, loopback_targets=True,
                  ca_file=tls.ca)
    code, headers, body, raw = ask(broker, get('/r/api/v1/items?page=2', 'Connection: keep-alive, X-Drop\r\nX-Drop: 1\r\n'
                                               'Keep-Alive: 5\r\nAuthorization: Bearer spoofed\r\nAccept-Encoding: gzip\r\n'
                                               'Proxy-Authorization: x\r\nX-Keep: yes\r\n'))
    assert code == 200 and body == b'hello world' and SECRET.encode() not in raw
    assert headers['x-up-keep'] == 'ok' and headers['connection'] == 'close'
    assert not {'transfer-encoding', 'keep-alive', 'x-up-drop', 'alt-svc', 'upgrade', 'x-trailer'} & set(headers)
    request = upstream.requests[-1]
    sent = sent_headers(request)
    assert request['method'] == 'GET' and request['target'] == '/v1/items?page=2'
    assert sent['authorization'] == [f'Bearer {SECRET}'] and sent['host'] == [f'localhost:{upstream.port}']
    assert sent['accept-encoding'] == ['identity'] and sent['x-keep'] == ['yes'] and sent['connection'] == ['close']
    assert not {'x-drop', 'keep-alive', 'proxy-authorization'} & set(sent)
    record = settled(broker)
    assert set(record) == ROUTE_KEYS
    assert (record['route'], record['method'], record['status'], record['reason']) == ('api', 'GET', 200, 'complete')
    assert record['path_hash'] == hashlib.sha256(b'/v1/items?page=2').hexdigest()
    assert record['credential_used'] is True and record['allowed'] is True and record['bytes'] == len(raw)
    assert credentials.log()[-1]['path_hash'] == record['path_hash']
    blob = json.dumps(broker.log()) + json.dumps(credentials.log()) + repr(broker) + repr(credentials) + str(credentials.stats())
    assert SECRET not in blob and '/v1/items' not in blob


def test_route_admission_by_name_method_and_prefix(brokers, tls, https, monkeypatch):
    upstream = https({'/v1': lambda r: reply(200, b'one'), '/v1/a/b': lambda r: reply(200, b'two')})
    calls, real = [], egress.resolve
    monkeypatch.setattr(egress, 'resolve', lambda host, port: calls.append(host) or real(host, port))
    made, make = brokers
    broker = make(None, routes=spec(upstream), loopback_targets=True, ca_file=tls.ca)
    for raw, code, reason in ((get('/r/other/v1'), 404, 'unknown_route'), (get('/r/API/v1'), 404, 'unknown_route'),
                              (b'DELETE /r/api/v1 HTTP/1.1\r\n\r\n', 405, 'method_not_allowed'),
                              (b'TRACE /r/api/v1 HTTP/1.1\r\n\r\n', 405, 'method_not_allowed'),
                              (get('/r/api/v2'), 403, 'prefix_refused'), (get('/r/api/v1x'), 403, 'prefix_refused'),
                              (get('/r/api'), 403, 'prefix_refused'), (get('/r/api/'), 403, 'prefix_refused')):
        got, headers, body, _ = ask(broker, raw)
        assert (got, headers['x-pebble-egress'], body) == (code, reason, b'')
        if code == 405:
            assert headers['allow'] == 'GET, POST'
    assert calls == [] and upstream.requests == []
    assert [(r['route'], r['allowed'], r['status']) for r in broker.log()[:2]] == [(None, False, 404)] * 2
    assert [r['method'] for r in broker.log()[2:4]] == ['DELETE', None]  # TRACE is never a route method.
    for target, body in (('/r/api/v1', b'one'), ('/r/api/v1/a/b?x=1', b'two')):
        assert ask(broker, get(target))[:3:2] == (200, body)
    assert [r['target'] for r in upstream.requests] == ['/v1', '/v1/a/b?x=1'] and calls == ['localhost'] * 2


@pytest.mark.parametrize('target, reason', [
    ('/r/api/../v1', 'path_traversal'), ('/r/api/v1/../../etc', 'path_traversal'), ('/r/api/v1/./x', 'path_traversal'),
    ('/r/api/v1/%2e%2e/x', 'path_traversal'), ('/r/api/v1/%2E%2E/x', 'path_traversal'), ('/r/api/v1/.%2e/x', 'path_traversal'),
    ('/r/api/v1%2fx', 'path_traversal'), ('/r/api/v1/%5c..', 'path_traversal'), ('/r/api/v1/%252e%252e/x', 'path_traversal'),
    ('/r/api/v1/..;/x', 'path_traversal'), ('/r/api/v1//x', 'path_traversal'), ('/r//api/v1', 'path_traversal'),
    ('/r/api/v1\\..\\x', 'path_traversal'), ('/r/api/v1/%00', 'path_traversal'), ('/r/api/v1/%zz', 'malformed_target'),
    ('/r/api/v1/<x>', 'malformed_target'), ('http://localhost/r/api/v1', 'malformed_target'), ('/x/api/v1', 'malformed_target'),
    ('/r/api/v1#frag', 'malformed_target'), ('/r/api/v1?a=<b>', 'malformed_target'), ('/r/api/v1?a=%z', 'malformed_target'),
    ('*', 'malformed_target'),
])
def test_route_path_traversal_and_malformed_targets_are_refused(brokers, tls, https, target, reason):
    upstream = https({'/v1': lambda r: reply(200, b'one')})
    made, make = brokers
    broker = make(None, routes=spec(upstream, prefix='/'), loopback_targets=True, ca_file=tls.ca)
    code, headers, body, _ = ask(broker, get(target))
    assert (code, headers['x-pebble-egress'], body) == (400, reason, b'')
    code, headers, _, _ = ask(broker, b'GET /r/api/v1/\xc3\xbc HTTP/1.1\r\n\r\n')
    assert (code, headers['x-pebble-egress']) == (400, 'malformed_request')
    assert upstream.requests == [] and all(r['route'] is None and not r['allowed'] for r in broker.log())


def test_route_mode_refuses_connect_upgrades_and_http2(brokers, tls, https, upstreams):
    upstream = https({'/v1': lambda r: reply(200, b'one')})
    made, make = brokers
    broker = make(None, routes=spec(upstream), loopback_targets=True, ca_file=tls.ca)
    for raw, code, reason in (
            (f'CONNECT localhost:{upstream.port} HTTP/1.1\r\n\r\n', 405, 'connect_refused'),
            ('GET /r/api/v1 HTTP/1.1\r\nConnection: Upgrade, HTTP2-Settings\r\nUpgrade: h2c\r\nHTTP2-Settings: AAMAAABk\r\n\r\n',
             400, 'upgrade_refused'),
            ('GET /r/api/v1 HTTP/1.1\r\nUpgrade: websocket\r\n\r\n', 400, 'upgrade_refused'),
            ('GET /r/api/v1 HTTP/1.1\r\nConnection: upgrade\r\n\r\n', 400, 'upgrade_refused'),
            ('GET /r/api/v1 HTTP/1.1\r\nHTTP2-Settings: AAMAAABk\r\n\r\n', 400, 'upgrade_refused'),
            ('PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n', 400, 'upgrade_refused'),
            ('GET /r/api/v1 HTTP/1.0\r\n\r\n', 400, 'unsupported_version'),
            ('POST /r/api/v1 HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n', 411, 'transfer_encoding_refused'),
            ('GET /r/api/v1 HTTP/1.1\r\nExpect: something\r\n\r\n', 417, 'expectation_refused'),
            ('POST /r/api/v1 HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx', 400, 'malformed_header'),
            ('POST /r/api/v1 HTTP/1.1\r\nContent-Length: -1\r\n\r\n', 400, 'malformed_header'),
            ('GET /r/api/v1 HTTP/1.1\r\n folded: x\r\n\r\n', 400, 'malformed_header'),
            ('GET /r/api/v1 HTTP/1.1\r\nBad Name: x\r\n\r\n', 400, 'malformed_header'),
            ('GET /r/api/v1 HTTP/1.1\r\n' + 'X-A: 1\r\n' * 65 + '\r\n', 431, 'header_too_large')):
        got, headers, body, _ = ask(broker, raw.encode())
        assert (got, headers['x-pebble-egress'], body) == (code, reason, b'')
    assert upstream.requests == []
    # A broker with both an allowlist and routes keeps CONNECT for the allowlist, unchanged.
    echo = upstreams('echo')
    mixed = make([('localhost', echo.port)], routes=spec(upstream), loopback_targets=True, ca_file=tls.ca)
    sock, head = connect(mixed, f'localhost:{echo.port}')
    assert head == egress.ESTABLISHED
    sock.sendall(b'ping')
    assert sock.recv(4) == b'ping'
    sock.close()
    sock, head = connect(mixed, f'localhost:{upstream.port}')
    assert status(head) == 403
    sock.close()
    assert ask(mixed, get('/r/api/v1'))[:3:2] == (200, b'one')
    assert [r.get('host') for r in mixed.log()[:2]] == ['localhost', 'localhost']
    assert mixed.log()[1]['reason'] == 'not_allowlisted' and settled(mixed)['route'] == 'api'


def test_route_redirects_are_returned_and_never_followed(brokers, tls, https, monkeypatch):
    upstream = https({})
    port = upstream.port

    def to(location):
        return lambda request: reply(302, b'', [('Location', location)])
    refused = {'/v1/evil': ('https://evil.pebble.test/x', 'cross_origin_redirect'),
               '/v1/scheme': (f'http://localhost:{port}/v1/x', 'cross_origin_redirect'),
               '/v1/port': (f'https://localhost:{port + 1}/v1/x', 'cross_origin_redirect'),
               '/v1/default-port': ('https://localhost/v1/x', 'cross_origin_redirect'),
               '/v1/userinfo': (f'https://localhost:{port}@evil.pebble.test/x', 'cross_origin_redirect'),
               '/v1/net-path': ('//evil.pebble.test/x', 'cross_origin_redirect'),
               '/v1/backslash': ('/\\evil.pebble.test/x', 'unsafe_redirect'),
               '/v1/bad-port': (f'https://localhost:{port}x/v1', 'unsafe_redirect')}
    same = {'/v1/same': (f'https://localhost:{port}/v1/next?q=1', '/r/api/v1/next?q=1'),
            '/v1/dir/relative': ('next', '/r/api/v1/dir/next'), '/v1/absolute-path': ('/v1/other', '/r/api/v1/other'),
            '/v1/upper': (f'HTTPS://LOCALHOST:{port}/v1/up', '/r/api/v1/up'), '/v1/outside': ('/admin', '/r/api/admin')}
    upstream.handlers.update({path: to(location) for path, (location, _) in {**refused, **same}.items()})
    calls, real = [], egress.resolve
    monkeypatch.setattr(egress, 'resolve', lambda host, port: calls.append(host) or real(host, port))
    credentials = CredentialBroker(lambda ref: SECRET)
    grant = credentials.grant('vault:api', f'https://localhost:{port}', ['GET'], '/v1', time.time() + 60)
    made, make = brokers
    broker = make(None, routes=spec(upstream, grant, ['GET']), credentials=credentials, loopback_targets=True,
                  ca_file=tls.ca)
    for path, (_, reason) in refused.items():
        code, headers, body, raw = ask(broker, get('/r/api' + path))
        assert (code, headers['x-pebble-egress'], body) == (502, reason, b'') and 'location' not in headers
        record = settled(broker)
        assert (record['allowed'], record['status'], record['reason']) == (True, 502, reason)
    for path, (_, mapped) in same.items():
        code, headers, body, raw = ask(broker, get('/r/api' + path))
        assert (code, headers['location'], body) == (302, mapped, b'') and settled(broker)['reason'] == 'complete'
    # Each request reached the upstream exactly once: nothing was followed, and only localhost was resolved.
    assert [r['target'] for r in upstream.requests] == [*refused, *same] and set(calls) == {'localhost'}
    assert ask(broker, get('/r/api/admin'))[:2] == (403, {'x-pebble-egress': 'prefix_refused', 'connection': 'close',
                                                           'content-length': '0'})


def test_revocation_cuts_in_flight_streams_and_refuses_the_next_request(brokers, tls, https):
    upstream = https({'/v1/slow': slow, '/v1/fast': lambda r: reply(200, b'fast')})
    credentials = CredentialBroker(lambda ref: SECRET)
    grant = credentials.grant('vault:api', f'https://localhost:{upstream.port}', ['GET'], '/v1', time.time() + 60)
    made, make = brokers
    broker = make(None, routes=spec(upstream, grant, ['GET']), credentials=credentials, loopback_targets=True,
                  ca_file=tls.ca)
    sock = socket.create_connection(('127.0.0.1', broker.port), timeout=10)
    sock.sendall(get('/r/api/v1/slow'))
    data = b''
    while data.count(b'x') < 20:
        data += sock.recv(4096)
    assert credentials.revoke(grant) is True
    started = time.monotonic()
    data += recv_all(sock, timeout=5)
    sock.close()
    assert time.monotonic() - started < 2 and len(data.partition(b'\r\n\r\n')[2]) < 100000
    record = settled(broker)
    assert (record['reason'], record['status'], record['allowed'], record['credential_used']) == ('revoked', 200, True, True)
    code, headers, body, _ = ask(broker, get('/r/api/v1/fast'))
    assert (code, headers['x-pebble-egress'], body) == (403, 'revoked', b'')
    record = settled(broker)
    assert (record['allowed'], record['credential_used'], record['status']) == (False, False, 403)
    assert [r['target'] for r in upstream.requests] == ['/v1/slow']  # The refused request never left the broker.


def test_expired_grants_are_refused(brokers, tls, https):
    upstream = https({'/v1/fast': lambda r: reply(200, b'fast')})
    credentials = CredentialBroker(lambda ref: SECRET)
    grant = credentials.grant('vault:api', f'https://localhost:{upstream.port}', ['GET'], '/v1', time.time() + 1.0)
    made, make = brokers
    broker = make(None, routes=spec(upstream, grant, ['GET']), credentials=credentials, loopback_targets=True,
                  ca_file=tls.ca)
    assert ask(broker, get('/r/api/v1/fast'))[:3:2] == (200, b'fast')
    time.sleep(1.1)
    code, headers, _, _ = ask(broker, get('/r/api/v1/fast'))
    assert (code, headers['x-pebble-egress']) == (403, 'expired') and len(upstream.requests) == 1


def test_route_rebinding_and_private_answers_never_receive_the_credential(brokers, tls, https, monkeypatch):
    upstream = https({'/v1/x': lambda r: reply(200, b'ok')})
    answers = {}
    monkeypatch.setattr(egress, 'resolve', lambda host, port: list(answers[host]))
    credentials = CredentialBroker(lambda ref: SECRET)
    grant = credentials.grant('vault:api', f'https://api.pebble.test:{upstream.port}', ['GET'], '/v1', time.time() + 60)
    routes = {'api': {'origin': f'https://api.pebble.test:{upstream.port}', 'methods': ['GET'], 'path_prefix': '/v1',
                      'grant': grant}}
    made, make = brokers
    broker = make(None, routes=routes, credentials=credentials)
    for answer in (['127.0.0.1'], ['10.0.0.5'], ['93.184.216.34', '127.0.0.1'], ['::ffff:127.0.0.1'], ['fe80::1%lo0'],
                   ['169.254.169.254'], ['64:ff9b::7f00:1']):
        answers['api.pebble.test'] = answer
        code, headers, _, _ = ask(broker, get('/r/api/v1/x'))
        assert (code, headers['x-pebble-egress']) == (403, 'refused_address')
    answers['api.pebble.test'] = []
    assert ask(broker, get('/r/api/v1/x'))[0] == 502 and settled(broker)['reason'] == 'resolution_failed'
    assert credentials.stats()['issued'] == 0 and upstream.requests == []
    # Loopback answers pass only with the explicit fixture flag; then TLS still checks the name (the cert is for localhost).
    fixture = make(None, routes=routes, credentials=credentials, loopback_targets=True, ca_file=tls.ca)
    answers['api.pebble.test'] = ['127.0.0.1']
    code, headers, _, _ = ask(fixture, get('/r/api/v1/x'))
    assert (code, headers['x-pebble-egress']) == (502, 'tls_verification_failed') and upstream.requests == []
    assert settled(fixture)['allowed'] is False
    # An IP-literal origin needs the fixture flag at review time.
    with pytest.raises(PolicyError):
        EgressBroker(None, routes={'api': {'origin': f'https://127.0.0.1:{upstream.port}', 'methods': ['GET'],
                                           'path_prefix': '/v1'}})


def test_route_requires_a_verified_certificate(brokers, tls, https):
    rogue = https({'/v1': lambda r: reply(200, b'one')}, context=tls.rogue)
    made, make = brokers
    broker = make(None, routes=spec(rogue), loopback_targets=True, ca_file=tls.ca)
    code, headers, _, _ = ask(broker, get('/r/api/v1'))
    assert (code, headers['x-pebble-egress']) == (502, 'tls_verification_failed') and rogue.requests == []
    # The default trust store (certifi) does not know the fixture CA either.
    good = https({'/v1': lambda r: reply(200, b'one')})
    plain = make(None, routes=spec(good), loopback_targets=True)
    code, headers, _, _ = ask(plain, get('/r/api/v1'))
    assert (code, headers['x-pebble-egress']) == (502, 'tls_verification_failed') and good.requests == []
    for kwargs in ({'ca_file': tls.ca}, {'ca_file': 'ca.pem', 'loopback_targets': True},
                   {'ca_file': tls.ca + '.missing', 'loopback_targets': True}, {'ca_file': 7, 'loopback_targets': True}):
        with pytest.raises(PolicyError):
            EgressBroker(None, routes={'api': {'origin': 'https://api.pebble.test', 'methods': ['GET'], 'path_prefix': '/'}},
                         **kwargs)


def test_reflected_credentials_never_reach_the_client(brokers, tls, https):
    def authorization(request):
        return sent_headers(request)['authorization'][0].encode()

    def in_body(request):
        body = b'a' * 70000 + authorization(request) + b'b' * 10  # Crosses the 64 KB read boundary.
        return [f'HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n'.encode(), body[:65000], body[65000:]]

    def in_head(request):
        return reply(200, b'fine', [('X-Echo', authorization(request).decode())])

    def split(request):
        token = authorization(request)
        return [b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n', b'%x\r\n%s\r\n' % (len(token) - 5, token[:-5]),
                b'5\r\n%s\r\n0\r\n\r\n' % token[-5:]]
    upstream = https({'/v1/body': in_body, '/v1/head': in_head, '/v1/split': split})
    credentials = CredentialBroker(lambda ref: SECRET)
    grant = credentials.grant('vault:api', f'https://localhost:{upstream.port}', ['GET'], '/v1', time.time() + 60)
    made, make = brokers
    broker = make(None, routes=spec(upstream, grant, ['GET']), credentials=credentials, loopback_targets=True,
                  ca_file=tls.ca)
    code, headers, _, raw = ask(broker, get('/r/api/v1/head'))
    assert (code, headers['x-pebble-egress']) == (502, 'credential_reflected') and SECRET.encode() not in raw
    for path in ('/r/api/v1/body', '/r/api/v1/split'):
        code, _, body, raw = ask(broker, get(path))
        assert code == 200 and SECRET.encode() not in raw and SECRET[:-1].encode() not in raw
        assert settled(broker)['reason'] == 'credential_reflected'
    assert len(upstream.requests) == 3
    blob = json.dumps(broker.log()) + json.dumps(credentials.log()) + repr(broker) + repr(credentials) + str(credentials.stats())
    assert SECRET not in blob


def test_route_request_body_with_expect_continue(brokers, tls, https):
    upstream = https({'/v1/post': lambda r: reply(201, b'created:' + r['body'])})
    made, make = brokers
    broker = make(None, routes=spec(upstream), loopback_targets=True, ca_file=tls.ca)
    sock = socket.create_connection(('127.0.0.1', broker.port), timeout=10)
    sock.sendall(b'POST /r/api/v1/post HTTP/1.1\r\nContent-Length: 5\r\nExpect: 100-continue\r\n\r\n')
    interim = b''
    while b'\r\n\r\n' not in interim:
        interim += sock.recv(1)
    assert interim == b'HTTP/1.1 100 Continue\r\n\r\n'
    sock.sendall(b'hello')
    data = recv_all(sock)
    sock.close()
    assert data.startswith(b'HTTP/1.1 201 Created\r\n') and data.endswith(b'\r\n\r\ncreated:hello')
    sent = sent_headers(upstream.requests[-1])
    assert upstream.requests[-1]['body'] == b'hello' and sent['content-length'] == ['5'] and 'expect' not in sent
    # A body that arrives with the head needs no interim response.
    code, _, body, raw = ask(broker, b'POST /r/api/v1/post HTTP/1.1\r\nContent-Length: 3\r\n\r\nabc')
    assert (code, body) == (201, b'created:abc') and b'100 Continue' not in raw


def test_route_request_and_byte_limits(brokers, tls, https):
    big = b'z' * 20000
    upstream = https({'/v1/big': lambda r: reply(200, big), '/v1/post': lambda r: reply(200, b'ok')})
    made, make = brokers
    broker = make(None, routes=spec(upstream), loopback_targets=True, ca_file=tls.ca, max_request_bytes=1000)
    code, headers, _, _ = ask(broker, b'POST /r/api/v1/post HTTP/1.1\r\nContent-Length: 1001\r\n\r\n' + b'x' * 1001)
    assert (code, headers['x-pebble-egress']) == (413, 'request_too_large') and upstream.requests == []
    assert ask(broker, b'POST /r/api/v1/post HTTP/1.1\r\nContent-Length: 1000\r\n\r\n' + b'x' * 1000)[0] == 200
    assert upstream.requests[-1]['body'] == b'x' * 1000
    limited = make(None, routes=spec(upstream), loopback_targets=True, ca_file=tls.ca, max_bytes=5000)
    code, _, body, raw = ask(limited, get('/r/api/v1/big'))
    record = settled(limited)
    assert code == 200 and record['reason'] == 'byte_limit' and record['bytes'] == len(raw) <= 5000 and len(body) < 20000
    code, headers, _, _ = ask(limited, get('/r/api/v1/big'))
    assert (code, headers['x-pebble-egress']) == (503, 'byte_limit') and len(upstream.requests) == 2
    for value in (-1, True, 4_000_000_001, '1'):
        with pytest.raises(PolicyError):
            EgressBroker(None, routes=spec(upstream), loopback_targets=True, max_request_bytes=value)


def test_route_concurrency_limit_and_close_tear_down_routes(brokers, tls, https):
    upstream = https({'/v1/slow': slow, '/v1/fast': lambda r: reply(200, b'fast')})
    made, make = brokers
    broker = make(None, routes=spec(upstream), loopback_targets=True, ca_file=tls.ca, max_connections=1)
    first = socket.create_connection(('127.0.0.1', broker.port), timeout=10)
    first.sendall(get('/r/api/v1/slow'))
    data = b''
    while data.count(b'x') < 10:
        data += first.recv(4096)
    code, headers, _, _ = ask(broker, get('/r/api/v1/fast'))
    assert (code, headers['x-pebble-egress']) == (503, 'connection_limit')
    assert broker.log()[-1]['route'] is None and set(broker.log()[-1]) == ROUTE_KEYS
    started = time.monotonic()
    assert broker.close() is True
    assert time.monotonic() - started < egress.JOIN_SECONDS
    data += recv_all(first)
    first.close()
    assert len(data.partition(b'\r\n\r\n')[2]) < 100000 and broker.routes == {} and broker.connections == {}
    assert [r['reason'] for r in broker.log() if r['allowed']] == ['broker_closed']
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', broker.port), timeout=2)


def test_route_table_validation_and_grant_narrowing(tls):
    credentials = CredentialBroker(lambda ref: SECRET)
    grant = credentials.grant('vault:api', 'https://api.pebble.test', ['GET', 'POST'], '/v1', time.time() + 60)
    good = {'origin': 'https://api.pebble.test', 'methods': ['GET'], 'path_prefix': '/v1/repos', 'grant': grant}
    assert route_table({'api': good, 'open-2': {'origin': 'https://other.pebble.test:8443', 'methods': ['POST', 'GET'],
                                                'path_prefix': '/'}}, credentials=credentials) == {
        'api': {'origin': 'https://api.pebble.test:443', 'host': 'api.pebble.test', 'port': 443, 'methods': ('GET',),
                'path_prefix': '/v1/repos', 'grant': grant, 'header': 'authorization'},
        'open-2': {'origin': 'https://other.pebble.test:8443', 'host': 'other.pebble.test', 'port': 8443,
                   'methods': ('GET', 'POST'), 'path_prefix': '/', 'grant': None, 'header': None}}
    assert route_table({'api': {**good, 'origin': 'https://[::1]:8443', 'grant': None}}, loopback=True)['api']['host'] == '::1'
    revoked = credentials.grant('vault:api', 'https://api.pebble.test', ['GET'], '/v1', time.time() + 60)
    credentials.revoke(revoked)
    for routes in ({}, [good], {'API': good}, {'a_b': good}, {'x' * 33: good}, {'-a': good}, {'api': [good]},
                   {'api': {**good, 'extra': 1}}, {'api': {'origin': 'https://api.pebble.test', 'methods': ['GET']}},
                   {'api': {**good, 'origin': 'http://api.pebble.test'}}, {'api': {**good, 'origin': 'https://api.pebble.test/v1'}},
                   {'api': {**good, 'origin': 'https://127.0.0.1'}}, {'api': {**good, 'methods': ['CONNECT']}},
                   {'api': {**good, 'methods': ['TRACE']}}, {'api': {**good, 'methods': []}},
                   {'api': {**good, 'methods': ['GET', 'GET']}}, {'api': {**good, 'methods': ['get']}},
                   {'api': {**good, 'path_prefix': '/v1/../x'}}, {'api': {**good, 'path_prefix': 'v1'}},
                   {'api': {**good, 'path_prefix': '/v1?x'}}, {'api': {**good, 'methods': ['DELETE']}},
                   {'api': {**good, 'path_prefix': '/v2'}}, {'api': {**good, 'path_prefix': '/'}},
                   {'api': {**good, 'origin': 'https://api.pebble.test:8443'}}, {'api': {**good, 'origin': 'https://evil.pebble.test'}},
                   {'api': {**good, 'grant': revoked}}, {'api': {**good, 'grant': 'cg_unknown'}},
                   {f'r{i}': {**good, 'grant': None} for i in range(17)}):
        with pytest.raises(PolicyError):
            route_table(routes, credentials=credentials)
    with pytest.raises(PolicyError):
        route_table({'api': good})  # A grant needs the credential broker.
    for args, kwargs in (((None,), {}), (([],), {'routes': {'api': good}, 'credentials': credentials}),
                         ((None,), {'routes': {'api': good}}),
                         (([('a.example', 443)],), {'credentials': credentials}),
                         (([('a.example', 443)],), {'ca_file': tls.ca, 'loopback_targets': True}),
                         ((None,), {'routes': {'api': good}, 'credentials': object()})):
        with pytest.raises(PolicyError):
            EgressBroker(*args, **kwargs)
    assert EgressBroker(None, routes={'api': good}, credentials=credentials).allow == ()


HELPER = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from pebble.credential_broker import CredentialBroker
from pebble.egress import EgressBroker
packet = json.loads(sys.stdin.readline())
held = bytearray(packet.pop('secret'), 'ascii')
credentials = CredentialBroker(lambda ref: held)
grant = credentials.grant('vault:fixture', packet['origin'], ['GET'], '/v1', packet['expires'])
held[:] = bytes(len(held))
broker = EgressBroker(None, routes={'api': {'origin': packet['origin'], 'methods': ['GET'], 'path_prefix': '/v1', 'grant': grant}},
                      credentials=credentials, loopback_targets=True, ca_file=packet['ca']).start()
print(json.dumps({'port': broker.port}), flush=True)
sys.stdin.readline()
broker.close()
print(json.dumps({'log': broker.log(), 'credentials': credentials.log(), 'reprs': [repr(broker), repr(credentials)]}), flush=True)
credentials.close()
'''

CONFINED = r'''
import ctypes, json, os, socket, sys
port, upstream, helper, control, root = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
out = {}
s = socket.create_connection(("127.0.0.1", port), timeout=10)
s.sendall(b"GET /r/api/v1/items HTTP/1.1\r\nHost: broker\r\n\r\n")
data = b""
while True:
    chunk = s.recv(65536)
    if not chunk:
        break
    data += chunk
s.close()
out["response"] = data.decode("latin-1")
libc = ctypes.CDLL(None, use_errno=True)
libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                        ctypes.c_void_p, ctypes.c_size_t]
def procargs(pid):
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(262144)
    buf = ctypes.create_string_buffer(262144)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return {"errno": ctypes.get_errno()}
    return {"hex": buf.raw[:size.value].hex()}
out["helper"], out["control"] = procargs(helper), procargs(control)
out["environ"] = dict(os.environ)
files = {}
for base, dirs, names in os.walk(root):
    for name in names:
        path = os.path.join(base, name)
        try:
            with open(path, "rb") as handle:
                files[path] = handle.read(1 << 20).hex()
        except OSError as e:
            files[path] = "errno %d" % e.errno
out["files"] = files
try:
    socket.create_connection(("127.0.0.1", upstream), timeout=3)
    out["direct"] = "connected"
except OSError as e:
    out["direct"] = e.errno
print(json.dumps(out))
'''


def test_confined_client_never_holds_the_credential_and_the_broker_host_args_hold_none(tmp_path, tls, https):
    """F2 regression: a confined job can read a same-user process's initial argv and environment
    (KERN_PROCARGS2, proven here on a control canary), so the broker host must receive the secret
    over a pipe only. The confined client gets the upstream response but never the credential."""
    if not MAC:
        raise AssertionError('This product surface requires macOS sandbox-exec; it is not skipped.')
    secret = 'w3-f2-secret-' + secrets.token_hex(20)
    marker = 'w3-f2-control-' + secrets.token_hex(8)
    upstream = https({'/v1/items': lambda r: reply(200, b'{"items": [1, 2]}')})
    helper_dir, client_dir = tmp_path / 'helper', tmp_path / 'client'
    (client_dir / 'home').mkdir(parents=True)
    helper_dir.mkdir()
    (helper_dir / 'helper.py').write_text(HELPER)
    (client_dir / 'client.py').write_text(CONFINED)
    backend = str(Path(egress.__file__).resolve().parents[1])
    clean = {'PATH': '/usr/bin:/bin', 'HOME': str(helper_dir), 'LANG': 'en_US.UTF-8', **CLT_ENV}
    helper = subprocess.Popen([sys.executable, '-I', str(helper_dir / 'helper.py'), backend], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=clean, cwd=helper_dir, text=True)
    control = subprocess.Popen([sys.executable, '-I', '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE,
                               env={**clean, 'PEBBLE_W3_CONTROL': marker}, cwd=helper_dir)
    try:
        helper.stdin.write(json.dumps({'secret': secret, 'origin': f'https://localhost:{upstream.port}', 'ca': tls.ca,
                                       'expires': time.time() + 120}) + '\n')
        helper.stdin.flush()
        assert select.select([helper.stdout], [], [], 30)[0], 'the broker host did not start'
        port = json.loads(helper.stdout.readline())['port']
        quote = json.dumps
        handles = ' '.join('(literal %s)' % quote(str(p)) for p in sorted(set(client_dir.resolve().parents), key=str))
        profile = f'''(version 1) (deny default)
          (allow process-fork) (allow process-exec (subpath "/usr/bin") (subpath "/Library/Developer/CommandLineTools"))
          (allow process-info*) (allow sysctl-read) (allow file-read-metadata)
          (allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
          (allow file-read* {handles} (subpath {quote(str(client_dir.resolve()))}) (subpath "/Library/Developer/CommandLineTools")
            (subpath "/System") (subpath "/usr") (subpath "/private/var/db/dyld") (literal "/dev/null") (literal "/dev/urandom"))
          (allow file-write* (literal "/dev/null"))
          {network_profile(port)}'''
        result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, '/usr/bin/python3', '-I',
                                 str(client_dir.resolve() / 'client.py'), str(port), str(upstream.port), str(helper.pid),
                                 str(control.pid), str(client_dir.resolve())],
                                capture_output=True, text=True, timeout=60,
                                env={'PATH': '/usr/bin:/bin', 'HOME': str(client_dir / 'home'), **CLT_ENV})
        assert result.returncode == 0, result.stderr[-2000:]
        out = json.loads(result.stdout.strip().splitlines()[-1])
        helper.stdin.write('\n')
        helper.stdin.flush()
        assert select.select([helper.stdout], [], [], 30)[0], 'the broker host did not report'
        report = json.loads(helper.stdout.readline())
        assert helper.wait(timeout=10) == 0
    finally:
        control.stdin.close()
        for proc in (helper, control):
            if proc.poll() is None:
                proc.kill()  # Only the live Popen handles this test created.
            proc.wait(timeout=10)
    token = secret.encode()
    assert 'hex' in out['control'] and 'hex' in out['helper'], (out['control'], out['helper'])
    control_args, helper_args = bytes.fromhex(out['control']['hex']), bytes.fromhex(out['helper']['hex'])
    assert marker.encode() in control_args  # The confined job can read a same-user process's environment (F2).
    assert b'helper.py' in helper_args and token not in helper_args  # It read the broker host's, and no secret is there.
    response = out['response'].encode('latin-1')
    assert response.startswith(b'HTTP/1.1 200 ') and response.endswith(b'{"items": [1, 2]}') and token not in response
    assert sent_headers(upstream.requests[-1])['authorization'] == [f'Bearer {secret}']
    # The CLT python3 shim adds SDKROOT/CPATH/... and Python adds LC_CTYPE; nothing credential-like arrives.
    assert secret not in json.dumps(out['environ'])
    assert not [key for key in out['environ'] if key.endswith(('TOKEN', 'KEY', 'SECRET', 'PASSWORD'))]
    readable = [bytes.fromhex(v) for v in out['files'].values() if not v.startswith('errno')]
    assert readable and all(token not in content for content in readable)
    assert out['direct'] == 1  # Sandbox denial: the upstream is reachable only through the broker.
    for path in tmp_path.rglob('*'):
        if path.is_file():
            assert token not in path.read_bytes(), path.name
    assert secret not in json.dumps(report)
    record = [r for r in report['log'] if r.get('route') == 'api'][-1]
    assert (record['status'], record['credential_used'], record['reason']) == (200, True, 'complete')
    assert [r['event'] for r in report['credentials']] == ['grant', 'header']
