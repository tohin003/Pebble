import errno
import functools
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
# Phase 32 (T2): --proof-log writes the serial-format log the release finalizers read, with or without xdist.
sys.path.insert(0, str(Path(__file__).resolve().parent))
pytest_plugins = ('proof_log', 'serial_register')


# V1-131 (Phase 24): suite-wide guards. No test reaches the network or the login Keychain unless it arranges that
# itself: a test's or module's own patch of socket or pebble.vault is applied after these and wins. The fixture
# names are unique, so they never take the place of a module's own fixture.

class NetworkRefused(OSError):
    """A test tried to connect beyond this machine."""


def loopback(family, address):
    """True for a connection that stays on this machine: Unix and other non-IP sockets, loopback and unspecified
    addresses, and 'localhost'. Any other host name would need DNS and the network."""
    if family not in (socket.AF_INET, socket.AF_INET6):
        return True
    host = address[0] if isinstance(address, tuple) and address else address
    if isinstance(host, bytes):
        host = host.decode('ascii', 'replace')
    if not isinstance(host, str):
        return False
    if host.lower() == 'localhost':
        return True
    try:
        ip = ipaddress.ip_address(host.split('%', 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_unspecified


_CONNECT, _CONNECT_EX = socket.socket.connect, socket.socket.connect_ex


def _connect(self, address):
    if not loopback(self.family, address):
        raise NetworkRefused(errno.ENETUNREACH, 'Tests never connect beyond this machine: ' + repr(address)[:120])
    return _CONNECT(self, address)


def _connect_ex(self, address):
    if not loopback(self.family, address):
        return errno.ENETUNREACH
    return _CONNECT_EX(self, address)


@pytest.fixture(autouse=True)
def _pebble_suite_network_guard(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', _connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', _connect_ex)


class MemoryKeychain:
    """pebble.vault.MacKeychain's calls (read, exists, write, remove) over one dict per test, shared by every Vault
    the test creates, as the login Keychain is shared. Never the Security framework."""

    lib = None

    def __init__(self, items):
        self.items = items

    def read(self, service, account):
        return self.items.get((bytes(service), bytes(account)))

    def exists(self, service, account):
        return (bytes(service), bytes(account)) in self.items

    def write(self, service, account, raw):
        self.items[(bytes(service), bytes(account))] = bytes(raw)

    def remove(self, service, account):
        return self.items.pop((bytes(service), bytes(account)), None) is not None


@pytest.fixture(autouse=True)
def _pebble_suite_keychain(monkeypatch):
    from pebble import vault
    monkeypatch.setattr(vault, 'MacKeychain', functools.partial(MemoryKeychain, {}))


# Phase 46 (1.9.28, w13b follow-up 1): an Auto task now needs App.containment_status (confined.admit, and the runner's
# prewarm at task start), whose containment.self_test starts a sandboxed setsid escapee and an outside canary. No test
# starts it as a side effect: App's per-process verdict is preset. test_containment.py and test_phase14_security.py
# call self_test themselves; a test that needs another verdict sets its own.

@pytest.fixture(autouse=True)
def _pebble_suite_containment(monkeypatch):
    from pebble.app import App
    monkeypatch.setattr(App, '_containment_result', {'contained': True, 'reason': 'Preset by the test suite.'})


# Phase 24: the real backend/main.py as a child process, for tests of its protocol, threads and logs.

SERVICE_SETUP = ''  # code every service child runs before backend/main.py; none unless a test session sets it


class Service:
    """backend/main.py over its JSON-lines protocol, with a flat --home (logs in the home, no ~/.pebble). Replies are
    kept by id with their arrival time; events in order. HOME points at a scratch folder."""

    def __init__(self, home, resources, scratch, patch=''):
        user = Path(scratch) / 'user-home'
        user.mkdir(exist_ok=True)
        env = {**os.environ, 'HOME': str(user), 'PYTHONDONTWRITEBYTECODE': '1'}
        code = ('import runpy, sys\nfrom pathlib import Path\nbackend = Path(sys.argv[1])\n'
                'sys.path.insert(0, str(backend))\n' + SERVICE_SETUP + '\n' + patch + '\n'
                "sys.argv = [str(backend / 'main.py')] + sys.argv[2:]\n"
                "runpy.run_path(str(backend / 'main.py'), run_name='__main__')\n")
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [sys.executable, '-c', code, str(BACKEND), '--home', str(home), '--resources', str(resources)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True, encoding='utf-8', env=env)
        self.replies, self.events, self.changed, self.sequence = {}, [], threading.Condition(), 0
        self.reader = threading.Thread(target=self._read, name='test-service-reader', daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            with self.changed:
                if isinstance(message, dict) and 'id' in message:
                    self.replies[message['id']] = (message, time.monotonic())
                else:
                    self.events.append(message)
                self.changed.notify_all()

    def send(self, method, **params):
        """Write one request; (id, the monotonic time it was written)."""
        self.sequence += 1
        ident = 'r' + str(self.sequence)
        self.process.stdin.write(json.dumps({'id': ident, 'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        return ident, time.monotonic()

    def reply(self, ident, timeout=30.0):
        """(reply, arrival time), or None when none came within timeout or the service ended."""
        deadline = time.monotonic() + timeout
        with self.changed:
            while ident not in self.replies:
                left = deadline - time.monotonic()
                if left <= 0 or self.process.poll() is not None:
                    return self.replies.get(ident)
                self.changed.wait(min(left, 0.1))
            return self.replies[ident]

    def call(self, method, timeout=30.0, **params):
        ident, _ = self.send(method, **params)
        found = self.reply(ident, timeout)
        assert found is not None, method + ' got no reply; service stderr: ' + self.stderr_text()[-3000:]
        return found[0]

    def stderr_text(self):
        self.stderr.flush()
        self.stderr.seek(0)
        return self.stderr.read().decode('utf-8', 'replace')

    def close(self, timeout=15.0):
        """End of input, as when the shell quits; the exit code."""
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.stderr.close()
        return self.process.returncode


@pytest.fixture
def pebble_service(tmp_path):
    """start(home, patch='') -> Service; every service a test started is closed after it."""
    started = []

    def start(home, patch='', resources=None):
        service = Service(home, resources or tmp_path / 'resources', tmp_path, patch)
        started.append(service)
        return service
    yield start
    for service in started:
        if service.process.returncode is None:
            service.close()


@pytest.fixture
def short_tmp_path():
    """A new private folder with a short path, removed afterwards. The Chrome bridge's socket path must fit 103 bytes
    (macOS sun_path), and pytest's default temp folder (under $TMPDIR, /var/folders/...) is too long for it, so a
    test file that starts a bridge overrides tmp_path with this (Phase 26: the documented one-file test command
    failed four such files on a default macOS setup)."""
    path = Path(tempfile.mkdtemp(prefix='pb-', dir='/private/tmp'))
    try:
        yield path
    finally:
        import shutil
        shutil.rmtree(path, ignore_errors=True)
