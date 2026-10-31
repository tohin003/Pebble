"""Phase 46 (1.9.29, D214): w26-bridge-fix, the fixes of w27's review that its own tests do not reach.

- F1: the socket budget is a small share of the open-file limit; while 300 idle sockets come from one address the
  bridge keeps at most its cap of them, and a paired phone still connects; one network address has its cap, sockets that
  are not yet a phone give way (the busiest address's first), a phone never does; a socket that never finishes TLS and
  the upgrade is closed at the deadline.
- F2: a welcomed phone that reads nothing is cut even under the 4 MB line; a connection cut while frames still wait in
  its buffer leaves no error behind (the AttributeError the review saw).
- F4: a removed phone's queued Mac jobs (keep awake, a screenshot) never run.
- F7: answers kept under retry keys are bounded per device; past the bound a retry learns only that the call happened.
- F8: before welcome, a message over 16 KB is refused unread (1009); after it, the request limit is unchanged.
- F9: the Mac's answer counts only for the six digits it was given; a pairing that ends lets the next code open.

Loopback only, with the iOS kit's own client code (remote_wire.py) as the phone.
"""

import asyncio
import base64
import json
import logging
import os
import resource
import socket
import ssl
import threading
import time
from collections import Counter
from types import SimpleNamespace

import pytest

from remote_kit import FakeApp, Phone, paired, start_hub, wire

pytestmark = pytest.mark.skipif(wire is None, reason="the iOS side's kit (bridge-mock/remote_wire.py) is not on this Mac")

from pebble import remote_bridge, remote_ws  # noqa: E402


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


def held(hub):
    """{address: sockets the bridge counts}, read on its own loop."""
    return hub._in_loop(lambda: Counter(r.address for group in hub.bridge.accepted.values() for r in group
                                        if not r.dropped))


async def still_open(readers, wait=0.3):
    """How many of these client sockets the bridge has not closed (no end of stream, no reset, within `wait`)."""
    count = 0
    for reader in readers:
        try:
            await asyncio.wait_for(reader.read(1), wait)
        except asyncio.TimeoutError:
            count += 1
        except (ConnectionError, OSError):
            pass
    return count


# ---- F1: sockets counted from accept ---------------------------------------------------------------------------------

def test_the_socket_budget_is_a_small_share_of_the_open_file_limit(monkeypatch):
    for soft, budget in ((256, 32), (10240, 64), (64, 8), (40, 8), (resource.RLIM_INFINITY, 64)):
        monkeypatch.setattr(remote_bridge.resource, 'getrlimit', lambda kind, soft=soft: (soft, resource.RLIM_INFINITY))
        assert remote_bridge.socket_budget() == budget, soft


def test_300_idle_sockets_from_one_address_are_held_at_its_cap_and_a_paired_phone_still_connects(bridge):
    hub, events, app = bridge
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 1024 if hard == resource.RLIM_INFINITY else min(1024, hard)
    if soft < wanted:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))  # this test's own 300 client sockets

    async def main():
        phone, connection = await paired(hub, events)
        await connection.close()
        idle = []
        try:
            for _ in range(300):  # TCP only: no ClientHello, nothing sent (a LAN scanner, or a local process)
                idle.append(await asyncio.open_connection('127.0.0.1', phone.port))
            await asyncio.sleep(0.5)
            counted = held(hub)
            open_before = await still_open([reader for reader, _ in idle], 0.05)
            again = await phone.connection().open()  # the idle ones are still open on their side
            welcome = await again.hello()
            answer = await again.call('remote.ping', {'ts': 1}, idem=False)
            await again.close()
        finally:
            for _, writer in idle:
                writer.close()
        return counted, open_before, welcome, answer

    try:
        counted, open_before, welcome, answer = run(main())
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    print('F1: 300 idle sockets from loopback: the bridge counts %s and %d are still open; then the phone got %s'
          % (dict(counted), open_before, welcome['t']))
    assert sum(counted.values()) <= remote_bridge.PER_LOOPBACK and open_before <= remote_bridge.PER_LOOPBACK
    assert welcome['t'] == 'welcome' and answer['ok'] is True


def test_each_address_has_a_cap_the_busiest_gives_way_first_and_a_phone_never_does(bridge):
    hub, events, app = bridge
    theirs = []

    def take(address, phone=False):
        ours, other = socket.socketpair()  # an accepted socket that never starts TLS
        theirs.append(other)
        hub.bridge.take(ours, address, '127.0.0.1')
        if phone:  # as if it had been welcomed: a phone, which never gives way
            record = hub.bridge.accepted[address][-1]
            record.conn = SimpleNamespace(device='d-' + address, confirming=False, ws=SimpleNamespace(abort=lambda: 0))

    try:
        for _ in range(6):
            hub._in_loop(take, '192.168.1.50')
        assert held(hub) == {'192.168.1.50': remote_bridge.PER_ADDRESS}, 'one address holds at most 4'
        for index in range(6):
            hub._in_loop(take, '192.168.1.%d' % (60 + index))
        counted = held(hub)
        assert sum(counted.values()) == remote_bridge.MAX_UNAUTHENTICATED, counted
        assert counted['192.168.1.50'] == 2 and all(counted['192.168.1.%d' % (60 + i)] == 1 for i in range(6)), \
            'the address holding the most gave way first'
        # Phones fill the budget: a newer socket finds nothing that may give way, and is closed at once.
        hub._in_loop(lambda: [hub.bridge.drop(r) for g in hub.bridge.accepted.values() for r in g])
        time.sleep(0.3)
        hub.bridge.budget = 6
        for index in range(6):
            hub._in_loop(take, '10.0.0.%d' % index, True)
        hub._in_loop(take, '10.0.0.99')
        time.sleep(0.3)
        assert sum(held(hub).values()) == 6 and '10.0.0.99' not in held(hub)
        theirs[-1].settimeout(2)
        assert theirs[-1].recv(1) == b'', 'the newest socket was closed'
    finally:
        hub._in_loop(lambda: [setattr(r, 'conn', None) for g in hub.bridge.accepted.values() for r in g])
        for other in theirs:
            other.close()


def test_a_socket_that_never_finishes_tls_and_the_upgrade_is_closed_at_the_deadline(bridge, monkeypatch):
    hub, events, app = bridge
    monkeypatch.setattr(remote_bridge, 'HANDSHAKE_SECONDS', 0.6)

    async def ended(**options):
        reader, writer = await asyncio.open_connection('127.0.0.1', hub.bridge.port, **options)
        started = time.monotonic()
        try:
            data = await asyncio.wait_for(reader.read(), 5)
        except (ConnectionError, OSError):
            data = b''
        writer.close()
        return data, time.monotonic() - started

    async def main():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        return await ended(), await ended(ssl=context)  # no ClientHello; TLS, then no upgrade

    (silent, silent_s), (no_upgrade, no_upgrade_s) = run(main())
    print('F1: closed after %.2f s (no TLS) and %.2f s (TLS, no upgrade)' % (silent_s, no_upgrade_s))
    assert silent == b'' and no_upgrade == b'' and silent_s < 3 and no_upgrade_s < 3
    deadline = time.monotonic() + remote_bridge.CLOSE_SECONDS + 1
    while sum(held(hub).values()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert sum(held(hub).values()) == 0, 'nothing is counted once both are closed'


# ---- F2: peers that read nothing --------------------------------------------------------------------------------------

def test_a_welcomed_phone_that_reads_nothing_is_cut_under_the_4_mb_line(bridge, monkeypatch):
    hub, events, app = bridge
    monkeypatch.setattr(remote_ws, 'STALLED_SECONDS', 1.0)  # 45 s in the product

    async def main():
        phone, connection = await paired(hub, events)
        await connection.close()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        plain = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        plain.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        plain.connect(('127.0.0.1', phone.port))
        reader, writer = await asyncio.open_connection(sock=plain, ssl=context, server_hostname='localhost',
                                                       limit=1 << 16)
        ws, _ = await wire.client_handshake(reader, writer, '127.0.0.1')
        challenge = json.loads(await ws.recv())
        link = phone.link
        signature = phone.device_key.sign(wire.t_hello(link['bid'], phone.device_id, challenge['nonce'], link['pin']))
        await ws.send_text(json.dumps({'t': 'hello', 'v': 1, 'device_id': phone.device_id,
                                       'sig': wire.b64u(signature), 'resume': None}))
        assert json.loads(await ws.recv())['t'] == 'welcome'

        def waiting():  # what the bridge's writer still holds for this phone (its queue and TLS's own buffer)
            conn = hub.bridge.by_device.get(phone.device_id)
            return conn and (conn.queued + conn.ws.writer.transport.get_write_buffer_size())

        queued = 0
        for index in range(40):  # events the phone never reads, until the bridge's writer waits on it (the kernel's
            hub.tee('artifact', {'chat_id': 'c1', 'id': 'a%d' % index, 'body': 'y' * 700000})  # buffers are full)
            await asyncio.sleep(0.2)
            queued = await asyncio.to_thread(hub._in_loop, waiting)
            if queued:
                break
        started = time.monotonic()
        while time.monotonic() - started < 10 and hub.bridge.by_device.get(phone.device_id):
            await asyncio.sleep(0.1)
        seconds = time.monotonic() - started
        writer.transport.abort()
        return queued, hub.bridge.by_device.get(phone.device_id) is None, seconds

    queued, cut, seconds = run(main())
    print('F2: a phone that read nothing, %d bytes waiting, was cut after %.1f s (the rule at 1 s here)' % (queued,
                                                                                                       seconds))
    assert 0 < queued < remote_bridge.MAX_QUEUE, 'the writer waited on the phone, far under the 4 MB line'
    assert cut and seconds < 5, 'still connected though it read nothing'


def test_connections_cut_with_frames_still_buffered_leave_no_error(bridge, monkeypatch, caplog):
    hub, events, app = bridge
    monkeypatch.setattr(remote_bridge, 'AUTH_SECONDS', 1.0)
    caplog.set_level(logging.WARNING, logger='pebble.remote')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    ping = wire.encode_frame(wire.OP_PING, b'p' * 125, fin=True, mask=os.urandom(4))
    stuck = []
    try:
        for _ in range(4):  # w27 saw the error on about half of such connections
            plain = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            plain.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            plain.settimeout(5)
            plain.connect(('127.0.0.1', hub.bridge.port))
            sock = context.wrap_socket(plain)
            stuck.append(sock)
            key = base64.b64encode(os.urandom(16)).decode('ascii')
            sock.sendall(('GET /pebble-remote/v1 HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: '
                          'Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: '
                          'pebble-remote.v1\r\n\r\n' % key).encode('latin-1'))
            sock.settimeout(0.5)
            try:
                for _ in range(60000):  # pongs it never reads, until the bridge stops reading too
                    sock.sendall(ping)
            except (TimeoutError, OSError, ssl.SSLError):
                pass
        def left():  # connections, and sockets not yet released (dropped ones too)
            return len(hub.bridge.conns) + sum(len(group) for group in hub.bridge.accepted.values())
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and hub._in_loop(left):
            time.sleep(0.2)
        assert hub._in_loop(left) == 0, 'every one was cut and released'
    finally:
        for sock in stuck:
            sock.close()
    errors = [r.getMessage() for r in caplog.records if 'Remote bridge error' in r.getMessage()]
    assert errors == [], errors


# ---- F4: a removed phone's queued Mac jobs ---------------------------------------------------------------------------

def test_a_removed_phones_queued_mac_jobs_never_run(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.unlock()
        app.gate = threading.Event()
        for index in range(4):  # Pebble is slow: the 4 call threads are busy
            await connection.send({'t': 'req', 'id': 'b%d' % index, 'method': 'send', 'idem': 'b-%d' % index,
                                   'params': {'id': 'c1', 'text': 'busy'}})
        await connection.send({'t': 'req', 'id': 'k1', 'method': 'remote.keep_awake', 'idem': 'k-1',
                               'params': {'on': False}})
        await connection.send({'t': 'req', 'id': 's1', 'method': 'remote.screenshot', 'params': {'display': None}})
        await asyncio.sleep(0.6)
        await asyncio.to_thread(hub.remote_device_remove, phone.device_id)
        app.gate.set()
        await asyncio.sleep(1.0)

    run(main())
    texts = [d['text'] for d in events.named('remote_notice')]
    assert hub.settings['keep_awake'] is True, 'keep awake was not turned off after the removal'
    assert not [t for t in texts if 'screenshot' in t or 'keep awake' in t], texts


# ---- F7: kept answers, bounded per device -------------------------------------------------------------------------------

def test_kept_answers_are_bounded_per_device_and_a_retry_past_the_bound_learns_only_that_it_happened(bridge):
    hub, events, app = bridge
    real = app.dispatch

    def dispatch(method, params):
        result = real(method, params)
        return {'id': 'c1', 'title': 'x' * params['size']} if method == 'chat_rename' else result
    app.dispatch = dispatch

    async def main():
        phone, connection = await paired(hub, events)
        await connection.unlock()
        large = await connection.call('chat_rename', {'id': 'c1', 'title': 't', 'size': 300 << 10}, idem='big-1')
        firsts = []
        for index in range(24):  # 24 answers of 200 KB, 4.8 MB: past the device's 4 MB
            firsts.append(await connection.call('chat_rename', {'id': 'c1', 'title': 't', 'size': 200 << 10},
                                                idem='mid-%d' % index))
        kept = await asyncio.to_thread(hub._in_loop, lambda: (hub.bridge.idem_bytes.get(phone.device_id),
                                                              hub.bridge.idem_total))
        again_large = await connection.call('chat_rename', {'id': 'c1', 'title': 't', 'size': 300 << 10},
                                            idem='big-1')
        again_first = await connection.call('chat_rename', {'id': 'c1', 'title': 't', 'size': 200 << 10},
                                            idem='mid-0')
        again_last = await connection.call('chat_rename', {'id': 'c1', 'title': 't', 'size': 200 << 10},
                                           idem='mid-23')
        return large, firsts, kept, again_large, again_first, again_last

    large, firsts, (device_bytes, total), again_large, again_first, again_last = run(main())
    print('F7: kept for one device %d bytes (bound %d), in all %d' % (device_bytes, remote_bridge.IDEM_DEVICE, total))
    assert large['ok'] is True and len(large['result']['title']) == 300 << 10, 'the first answer is whole'
    assert all(f['ok'] is True and len(f['result']['title']) == 200 << 10 for f in firsts)
    assert device_bytes <= remote_bridge.IDEM_DEVICE and total <= remote_bridge.IDEM_BYTES
    assert again_large == {'t': 'res', 'id': again_large['id'], 'ok': True, 'result': None}, 'over 256 KB: ok only'
    assert again_first['result'] == firsts[0]['result'], 'within the bound: the answer itself'
    assert again_last['ok'] is True and again_last['result'] is None, 'past the device bound: ok only'
    assert len(app.called('chat_rename')) == 25, 'and nothing ran twice'


# ---- F8: the size of a message before welcome ------------------------------------------------------------------------

def test_before_welcome_a_message_over_16_kb_is_refused_unread(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.close()
        early = await phone.connection().open()
        await early.send('{"t":"hello","v":1,"pad":"' + 'x' * 20000 + '"}')
        assert await early.until(lambda f: False, 5) is None
        later = await phone.connection().open()
        await later.hello()
        answer = await later.call('remote.ping', {'ts': 1, 'pad': 'x' * 20000}, idem=False)
        await later.close()
        return early.ws.close_code, answer

    closed, answer = run(main())
    assert closed == 1009 and answer['ok'] is True


# ---- F9: the Mac's answer and the digits it showed --------------------------------------------------------------------

def test_the_macs_answer_counts_only_for_the_digits_it_was_given(bridge):
    hub, events, app = bridge

    async def main():
        link = hub.remote_pair_start()['link']
        connection = await Phone(link).connection().open()
        sas = []
        await connection.pair(sas=sas)
        await asyncio.to_thread(events.wait, lambda name, data: name == 'remote_state' and (
            data.get('confirm') or {}).get('code') == sas[0])
        other = '%06d' % ((int(sas[0]) + 1) % 1000000)
        with pytest.raises(ValueError, match='No iPhone is waiting'):
            await asyncio.to_thread(hub.remote_pair_confirm, True, other)
        for bad in ('12345', 'abcdef', 123456):
            with pytest.raises(ValueError, match='Choose Pair or Cancel'):
                await asyncio.to_thread(hub.remote_pair_confirm, True, bad)
        with pytest.raises(ValueError, match='Answer the iPhone waiting to pair first'):
            await asyncio.to_thread(hub.remote_pair_start)
        await asyncio.to_thread(hub.remote_pair_confirm, True, sas[0])
        frame = await connection.until(lambda f: f['t'] in ('paired', 'bye'))
        await connection.close()
        return frame

    assert run(main())['t'] == 'paired'
    assert hub.remote_pair_start()['link'], 'once answered, a new code opens'
