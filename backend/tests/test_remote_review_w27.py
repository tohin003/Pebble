"""Phase 46 (1.9.29, D214): w27's adversarial review of Pebble Remote's bridge, one test per finding.

Each test states what the bridge should do and failed on the bridge as w26 built it. w26-bridge-fix fixed F1 to F11
and dropped their strict-xfail markers, so each now holds its fix. Loopback only, the iOS kit's own client code
(remote_wire.py) where a phone is needed, nothing sent off this Mac. The last tests are not findings: they hold what
the review confirmed.
"""

import asyncio
import base64
import errno
import json
import os
import queue
import resource
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from remote_kit import FakeApp, Phone, paired, start_hub, wire

pytestmark = pytest.mark.skipif(wire is None, reason="the iOS side's kit (bridge-mock/remote_wire.py) is not on this Mac")

from pebble import remote_bridge, remote_mac, remote_policy as policy, remote_ws  # noqa: E402

TESTS = Path(__file__).resolve().parent
BACKEND = TESTS.parent


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    app = FakeApp()
    hub, events = start_hub(tmp_path, app, monkeypatch)
    try:
        yield hub, events, app
    finally:
        hub.close()


def run(coroutine):
    return asyncio.run(asyncio.wait_for(coroutine, 120))


def in_loop(hub, function):
    """`function()` on the bridge's own loop, where its state lives (never read across threads)."""
    return hub._in_loop(function)


def tls_websocket(port, receive_buffer=None):
    """A blocking TLS socket upgraded to the bridge's WebSocket, as any LAN peer can make one: no pairing, no key."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    plain = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if receive_buffer:
        plain.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
    plain.settimeout(5)
    plain.connect(('127.0.0.1', port))
    sock = context.wrap_socket(plain)
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    sock.sendall(('GET /pebble-remote/v1 HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                  'Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: pebble-remote.v1\r\n'
                  '\r\n' % key).encode('latin-1'))
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError('the bridge closed the upgrade')
        head += chunk
    assert head.startswith(b'HTTP/1.1 101'), head[:40]
    return sock


def masked(opcode, payload, fin=True):
    return wire.encode_frame(opcode, payload, fin=fin, mask=os.urandom(4))


def decode(data, max_message=1 << 20):
    """remote_ws.Socket.recv() over `data` alone: (messages, close code)."""
    class Writer:
        def __init__(self):
            self.transport = self

        def write(self, data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        def abort(self):
            pass

    async def main():
        reader = asyncio.StreamReader(limit=1 << 22)
        reader.feed_data(data)
        reader.feed_eof()
        socket_ = remote_ws.Socket(reader, Writer(), max_message)
        out = []
        while True:
            message = await socket_.recv()
            if message is None:
                return out, socket_.close_code
            out.append(message)
    return asyncio.run(main())


# ---- F1: sockets before the handshake ------------------------------------------------------------------------------

CHILD = r'''
import errno, os, resource, sys
backend, tests, home, soft = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sys.path[:0] = [backend, tests]
resource.setrlimit(resource.RLIMIT_NOFILE, (soft, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
from remote_kit import FakeApp, FakeAwake, FakeBonjour
from pebble import remote, remote_mac
remote_mac.KeepAwake, remote_mac.Bonjour = FakeAwake, FakeBonjour
remote_mac.local_host_name, remote_mac.magic_dns = (lambda: ''), (lambda address: '')
remote_mac.battery = lambda: {'battery_percent': None, 'charging': False, 'on_power': True}
hub = remote.RemoteHub()
hub.attach(FakeApp(), os.path.join(home, 'data'), lambda name, data: None, name='Test Mac', folders_home=home,
           link_loopback=True, address_plan=lambda: ([], []), port=0)
hub.remote_set('enabled', True)
print('PORT=%d' % hub.bridge.port, flush=True)
for line in sys.stdin:
    if line.strip() == 'quit':
        break
    try:  # what Pebble's backend does all day: open a file (SQLite, logs, attachments, a tool's pipes)
        with open(os.path.join(home, 'probe.txt'), 'w') as handle:
            handle.write('Pebble writes a file')
        print('OPEN=ok', flush=True)
    except OSError as exc:
        print('OPEN=' + errno.errorcode.get(exc.errno, '?'), flush=True)
hub.close()
'''


def test_w27_f1_idle_connections_never_use_up_the_backends_files(tmp_path):
    """The backend runs with macOS's default soft limit of 256 open files (launchd's maxfiles); the bridge accepts every
    TCP connection and holds it 10 s waiting for a TLS ClientHello, before C6's per-address count or MAX_CONNECTIONS
    is consulted. 300 connections that send nothing (loopback here; a LAN or tailnet peer does the same) leave
    Pebble's backend unable to open a file."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 1024 if hard == resource.RLIM_INFINITY else min(1024, hard)
    if soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))  # this test's own client sockets
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    child = subprocess.Popen([sys.executable, '-c', CHILD, str(BACKEND), str(TESTS), str(tmp_path), '256'],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             cwd=str(tmp_path), env=env)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line.strip()) for line in child.stdout], daemon=True).start()
    held = []

    def probe():
        child.stdin.write('probe\n')
        child.stdin.flush()
        return lines.get(timeout=20)
    try:
        port = int(lines.get(timeout=60).split('=')[1])
        assert probe() == 'OPEN=ok'
        for _ in range(300):
            try:
                held.append(socket.create_connection(('127.0.0.1', port), timeout=2))
            except OSError:
                break
        time.sleep(1.0)
        after = probe()
    finally:
        for sock in held:
            sock.close()
        try:
            child.stdin.write('quit\n')
            child.stdin.flush()
            child.wait(20)
        except Exception:
            child.kill()
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    print('F1: %d idle connections accepted; then the backend could open a file: %s' % (len(held), after))
    assert after == 'OPEN=ok', 'Pebble\'s backend could not open a file (%s) while %d idle connections waited' % (
        after, len(held))


# ---- F2: a pre-authentication connection that never reads -----------------------------------------------------------

def ping_without_reading(sock, most=60000):
    """Pings (each answered with a pong the bridge must write) until the bridge stops reading, never reading a byte."""
    frame = masked(wire.OP_PING, b'p' * 125)
    sock.settimeout(0.5)
    sent = 0
    try:
        for _ in range(most):
            sock.sendall(frame)
            sent += 1
    except (TimeoutError, OSError, ssl.SSLError):
        pass
    return sent


def test_w27_f2_connections_that_never_read_cannot_hold_the_bridge_busy(bridge, monkeypatch):
    hub, events, app = bridge
    monkeypatch.setattr(remote_bridge, 'AUTH_SECONDS', 1.0)  # 20 s in the product; the shape is the same
    port = hub.bridge.port
    stuck = []
    try:
        for _ in range(remote_bridge.MAX_UNAUTHENTICATED):
            sock = tls_websocket(port, receive_buffer=4096)
            sent = ping_without_reading(sock)
            stuck.append((sock, sent))
        # Past the time to authenticate (1 s here) and the 2 s the bridge gives a closing handshake, with margin.
        deadline = time.monotonic() + 12
        waiting = None
        while time.monotonic() < deadline:
            waiting = in_loop(hub, lambda: sum(1 for c in hub.bridge.conns if c.device is None))
            if waiting == 0:
                break
            time.sleep(0.5)
        print('F2: pings sent before the bridge stopped reading: %s; unauthenticated connections still held: %d'
              % ([sent for _, sent in stuck], waiting))

        async def phone():
            link = hub.remote_pair_start()['link']
            connection = await Phone(link).connection().open()
            first = connection.challenge
            await connection.close()
            return first
        first = run(phone())
        print('F2: a phone then got: %s' % {k: first.get(k) for k in ('t', 'code')})
    finally:
        for sock, _ in stuck:
            sock.close()
    assert waiting == 0, '%d connections that never authenticated are still held' % waiting
    assert first['t'] == 'challenge', 'a phone was refused: %s' % first


# ---- F3: the unlock outlives the connection that proved it (S-05) ------------------------------------------------------

def test_w27_f3_an_unlock_never_outlives_the_connection_that_proved_it(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.close()
        raw = await phone.connection().open()
        await raw.hello()
        frame = raw.elevated('remote.unlock', {})
        # Face ID, then the app goes to the background at once: the request and the app's close frame leave together.
        raw.ws.writer.write(masked(wire.OP_TEXT, json.dumps(frame).encode()) +
                            masked(wire.OP_CLOSE, (1000).to_bytes(2, 'big')))
        await raw.ws.writer.drain()
        assert await raw.until(lambda f: False, 5) is None
        await asyncio.sleep(0.5)
        left_unlocked = await asyncio.to_thread(in_loop, hub, lambda: (
            phone.device_id in hub.bridge.unlocked, phone.device_id in hub.bridge.by_device))
        later = await phone.connection().open()  # someone with the phone (and its passcode) opens the app later
        await later.hello()
        acting = await later.call('model_profiles')  # any call that needs the unlock, with no Face ID
        await later.close()
        return left_unlocked, acting

    (unlocked, connected), acting = run(main())
    print('F3: unlocked after its last connection closed: %s (a connection open: %s); the next call: %s'
          % (unlocked, connected, acting.get('code', 'ok')))
    assert not unlocked, 'the device stayed unlocked with no connection open'
    assert acting.get('code') == 'unlock_required', acting


# ---- F4: revocation and calls already queued ---------------------------------------------------------------------------

def test_w27_f4_calls_waiting_for_pebble_never_run_once_the_phone_is_removed(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.unlock()
        app.gate = threading.Event()  # Pebble is slow to answer (a busy lock, a long read)
        for index in range(6):  # 4 call threads (remote_bridge: max_workers=4): 4 run, 2 wait behind them
            await connection.send({'t': 'req', 'id': 'q%d' % index, 'method': 'send', 'idem': 'q-%d' % index,
                                   'params': {'id': 'c1', 'text': 'delete the build folder'}})
        await asyncio.sleep(0.6)
        started = len(app.called('send'))
        await asyncio.to_thread(hub.remote_device_remove, phone.device_id)  # the person removes the phone on the Mac
        app.gate.set()
        await asyncio.sleep(1.0)
        return started, len(app.called('send'))

    started, total = run(main())
    print('F4: calls inside Pebble when the phone was removed: %d; calls Pebble received in all: %d' % (started, total))
    assert started == 4
    assert total == started, '%d calls reached Pebble after the phone was removed' % (total - started)


# ---- F5: answers between 4 MB and the 8 MB limit ---------------------------------------------------------------------------

def test_w27_f5_an_answer_under_the_8_mb_limit_reaches_the_phone(bridge):
    hub, events, app = bridge
    app.chat = lambda chat_id: {'id': 'c1', 'title': 'Long', 'messages': [
        {'id': 'm0', 'role': 'assistant', 'content': 'x' * (5 << 20)}]}  # a long reply, a pasted log

    async def main():
        phone, connection = await paired(hub, events)
        assert connection.welcome['limits']['max_frame'] == 8 << 20
        answer = await connection.call('remote.chat_tail', {'id': 'c1', 'limit': 1}, idem=False)
        return answer, connection.ws.close_code

    answer, closed = run(main())
    print('F5: a 5 MB answer: %s (close code %s)' % ('received' if answer else 'connection dropped', closed))
    assert answer is not None and answer['ok'] is True, 'the connection was dropped instead (close %s)' % closed


# ---- F6: what counts as the tailnet ----------------------------------------------------------------------------------------

def test_w27_f6_a_carrier_grade_nat_address_on_wifi_is_not_the_tailnet():
    up, broadcast, running, p2p = 0x1, 0x2, 0x40, 0x10
    found = [('en0', up | broadcast | running, '100.72.14.9'),     # a hotspot's, a carrier's or Starlink's address
             ('utun4', up | p2p | running, '100.101.102.103')]      # Tailscale's tunnel
    lan, tailnet = remote_mac.address_plan(found)
    print('F6: tailnet addresses the bridge would listen on: %s' % tailnet)
    assert tailnet == ['100.101.102.103'], tailnet


# ---- F7: the retry memory --------------------------------------------------------------------------------------------------

def test_w27_f7_retry_memory_has_a_byte_bound_and_a_locked_phone_cannot_fill_it(bridge):
    hub, events, app = bridge
    app.chat = lambda chat_id: {'id': 'c1', 'title': 'Long', 'messages': [
        {'id': 'm0', 'role': 'assistant', 'content': os.urandom(384 << 10).hex()}]}  # 768 KB, a fresh copy each time

    async def main():
        phone, connection = await paired(hub, events)  # never unlocked: reads only
        for index in range(24):
            answer = await connection.call('remote.chat_tail', {'id': 'c1', 'limit': 1}, idem='read-%d' % index)
            assert answer['ok'] is True
        return await asyncio.to_thread(in_loop, hub, lambda: sum(
            len(remote_bridge.dumps(entry['future'].result())) for entry in hub.bridge.idem.values()
            if entry['future'].done()))

    kept = run(main())
    print('F7: bytes of answers kept by 24 reads from a locked phone: %d (10 a second for 10 minutes keeps %.1f GB)'
          % (kept, kept / 24 * 6000 / 1e9))
    assert kept < 4 << 20, kept


# ---- F8: the WebSocket decoder and fragments ----------------------------------------------------------------------------

def test_w27_f8_a_message_cannot_be_made_of_unbounded_fragments():
    count = 100_000
    data = masked(wire.OP_TEXT, b'{', fin=False) + masked(wire.OP_CONT, b'', fin=False) * count + \
        masked(wire.OP_CONT, b'}')
    started = time.perf_counter()
    messages, closed = decode(data)
    seconds = time.perf_counter() - started
    print('F8: %d empty fragments (%d bytes) read in %.2f s: %s, close %s' % (
        count, len(data), seconds, messages and 'one message' or 'refused', closed))
    assert messages == [] and closed in (remote_ws.POLICY, remote_ws.TOO_BIG), (len(messages), closed)


# ---- F9: the Mac's confirmation of a pairing (X-1) ----------------------------------------------------------------------

def test_w27_f9_no_new_pairing_code_while_the_mac_waits_for_an_answer(bridge):
    hub, events, app = bridge

    async def main():
        phone = Phone(hub.remote_pair_start()['link'])
        first = await phone.connection().open()
        sas = []
        await first.pair(sas=sas)
        await asyncio.to_thread(events.wait, lambda name, data: name == 'remote_state' and (
            data.get('confirm') or {}).get('code') == sas[0])
        try:
            hub.remote_pair_start()
            opened = True
        except ValueError:
            opened = False
        await asyncio.to_thread(hub.remote_pair_confirm, False)
        await first.close()
        return opened

    opened = run(main())
    print('F9: a second pairing code opened while the Mac asked about the first phone: %s' % opened)
    assert not opened


# ---- F10: Stealth from the phone ----------------------------------------------------------------------------------------

def test_w27_f10_turning_stealth_on_from_a_phone_needs_face_id():
    known = lambda: frozenset()
    assert policy.needs_proof('setting', {'key': 'stealth', 'value': True}, known, 'd-1')


# ---- F11: a call that timed out ----------------------------------------------------------------------------------------

def test_w27_f11_a_retry_after_a_timeout_learns_what_pebble_did(bridge, monkeypatch):
    hub, events, app = bridge
    monkeypatch.setattr(remote_bridge, 'CALL_SECONDS', 0.5)

    async def main():
        phone, connection = await paired(hub, events)
        await connection.unlock()
        app.gate = threading.Event()
        first = await connection.call('send', {'id': 'c1', 'text': 'hello'}, idem='slow-1')
        app.gate.set()
        await asyncio.sleep(0.5)
        app.gate = None
        again = await connection.call('send', {'id': 'c1', 'text': 'hello'}, idem='slow-1')
        return first, again, len(app.called('send'))

    first, again, sent = run(main())
    print('F11: first answer %s; Pebble received %d; the retry with the same key: %s' % (
        first.get('code'), sent, again.get('code', 'ok')))
    assert first['code'] == 'timeout' and sent == 1
    assert again['ok'] is True, 'the message was sent, but its retry still says it timed out'


# ---- what the review confirmed ----------------------------------------------------------------------------------------

def test_w27_remote_off_costs_nothing_a_run_would_feel(tmp_path):
    """Item 8: Remote off (the default, and D207's benchmark setting) holds no thread, socket or file, and adds one
    attribute read per event and one tuple lookup per call. Measured here and bounded."""
    from pebble.remote import RemoteHub
    threads = {t.ident for t in threading.enumerate()}
    descriptors = len(os.listdir('/dev/fd'))
    hub = RemoteHub()
    hub.attach(object(), tmp_path / 'data', lambda *a: None)
    assert hub.bridge is None and hub.loop is None and hub.keep_awake is None
    assert not (tmp_path / 'data').exists()
    data = {'chat_id': 'c' * 32, 'id': 'm' * 32, 'text': 'tok'}
    count = 200_000
    started = time.perf_counter()
    for _ in range(count):
        hub.tee('token', data)
    tee_us = (time.perf_counter() - started) / count * 1e6
    started = time.perf_counter()
    for _ in range(count):
        hub.owns('send')
    owns_us = (time.perf_counter() - started) / count * 1e6
    print('Remote off: %.3f us per event (tee), %.3f us per call (owns); 1,000 events in a step: %.3f ms'
          % (tee_us, owns_us, tee_us))
    assert tee_us < 1.0 and owns_us < 1.0
    assert {t.ident for t in threading.enumerate()} - threads == set(), 'no thread while Remote is off'
    assert len(os.listdir('/dev/fd')) <= descriptors, 'no socket or file while Remote is off'
    hub.close()


def test_w27_tls_below_1_3_is_refused(bridge):
    hub, events, app = bridge
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection(('127.0.0.1', hub.bridge.port), timeout=5) as plain:
        with pytest.raises((ssl.SSLError, ConnectionError)):
            context.wrap_socket(plain).close()


def test_w27_the_nesting_guard_counts_what_json_nests():
    """X-5/S-02's guard against JSON whose strings and keys hold brackets, escaped quotes and backslashes."""
    import random
    rng = random.Random(1929)
    for _ in range(400):
        depth = rng.randint(0, 130)
        text = rng.choice(['"a[{\\"]}"', '1', 'null', '"\\\\"', '"\\u005b"'])
        for _ in range(depth):
            text = '[' + text + ',"]}{["]' if rng.random() < 0.5 else '{"k\\"[":' + text + '}'
        json.loads(text)
        assert remote_bridge.too_deep(text, 64) == (depth > 64), (depth, text[:80])


def test_w27_what_the_review_confirmed_holds(bridge):
    """Pairing, proofs and revocation paths the review attacked without finding a hole: two phones racing one link,
    the phone answering its own confirmation, a hello replayed on another connection, a proof replayed on the next
    connection, the Mac's routes from the phone, and a removed phone's key. (C2's (r, n-s) twin: w26's own test.)"""
    hub, events, app = bridge

    async def main():
        link = hub.remote_pair_start()['link']
        phone, rival = Phone(link), Phone(link)
        one, two = await phone.connection().open(), await rival.connection().open()
        await asyncio.gather(one.pair(), two.pair())  # two phones, one link, at once
        frames = await asyncio.gather(one.until(lambda f: f['t'] in ('bye',), 2), two.until(lambda f: f['t'] in (
            'bye',), 2), return_exceptions=True)
        refused = [f for f in frames if isinstance(f, dict) and f.get('code') == 'bad_code']
        assert len(refused) == 1, frames  # exactly one waits for the Mac
        waiting = one if not isinstance(frames[0], dict) else two
        await waiting.send({'t': 'req', 'id': 'x1', 'method': 'remote_pair_confirm', 'params': {'confirm': True}})
        await asyncio.sleep(0.5)
        assert hub.status()['devices'] == [], 'the phone cannot answer for the Mac'
        await asyncio.to_thread(hub.remote_pair_confirm, True)
        paired_frame = await waiting.until(lambda f: f['t'] in ('paired', 'bye'))
        assert paired_frame['t'] == 'paired'
        owner = phone if waiting is one else rival
        owner.device_id = paired_frame['device_id']
        waiting.welcome = await waiting.until(lambda f: f['t'] == 'welcome')
        # A hello signed for one connection's nonce is refused on another.
        other = await owner.connection().open()
        bye = await other.hello(nonce=waiting.nonce)
        assert bye['t'] == 'bye' and bye['code'] == 'bad_signature'
        await other.close()
        # A Face ID proof made on one connection is refused on the next one (v0.3 item 3: bound to the nonce).
        made_before = waiting.elevated('remote.unlock', {})
        third = await owner.connection().open()
        await third.hello()  # replaces the first connection (3.1)
        assert (await third.exchange(made_before))['code'] == 'bad_proof', 'bound to its connection'
        assert (await third.exchange(third.elevated('remote.unlock', {})))['ok'] is True
        for method in policy.MAC_ROUTES:
            assert (await third.call(method, {}))['code'] == 'not_allowed'
        await asyncio.to_thread(hub.remote_device_remove, owner.device_id)
        again = await owner.connection().open()
        assert (await again.hello())['code'] == 'revoked'

    run(main())
