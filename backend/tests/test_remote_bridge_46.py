"""Phase 46 (1.9.29, D214, D215): the iPhone bridge end to end on loopback, with the iOS kit's own client code.

The phone here is the kit's remote_wire.py (read-only, loaded from its folder): its TLS pin check, its WebSocket client,
its software P-256 keys, its transcripts. Pebble is a stand-in (remote_kit.FakeApp) except in the last test, which
puts a real App behind the bridge. Covered: pairing with the Mac's six-digit confirmation (X-1), hello and resume,
allowlist refusals, the unlock and remote.lock (C1, S-05), every bad_proof path and C2's (r, n-s) twin, idem, limits,
revocation, events (C4, batching, trimming, backpressure), v0.5's folders and new projects, screenshots (C3), the
Mac's record of what a phone did (4.8, C7), and the WebSocket decoder under random and malformed frames.
"""

import asyncio
import base64
import json
import logging
import os
import random
import stat
import threading
import time

import pytest

from remote_kit import FakeApp, Phone, jpeg_size, paired, start_hub, wire

pytestmark = pytest.mark.skipif(wire is None, reason="the iOS side's kit (bridge-mock/remote_wire.py) is not on this Mac")

from pebble import remote_bridge, remote_policy as policy, remote_ws  # noqa: E402


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


def lines(events):
    return [data['text'] for data in events.named('remote_notice')]


# ---- pairing (3.2, X-1) -------------------------------------------------------------------------------------------

def test_pairing_waits_for_the_macs_confirmation_then_welcomes_and_stores_the_phone_owner_only(bridge, tmp_path,
                                                                                                 caplog):
    hub, events, app = bridge
    caplog.set_level(logging.DEBUG, logger='pebble.remote')

    async def main():
        started = hub.remote_pair_start()
        link = wire.parse_pairing_link(started['link'])
        assert link['endpoints'] == [('127.0.0.1', link['endpoints'][0][1])] and link['name'] == 'Test Mac'
        assert started['qr'] and all(set(row) <= {'0', '1'} for row in started['qr'])
        assert abs(started['expires_at'] - wire.now_ms() - 300000) < 5000
        phone = Phone(started['link'])
        connection = await phone.connection().open()
        assert connection.tls_version == 'TLSv1.3'
        assert connection.headers['sec-websocket-protocol'] == ['pebble-remote.v1']
        challenge = connection.challenge
        assert challenge['t'] == 'challenge' and challenge['v'] == 1 and challenge['bid'] == link['bid']
        assert len(wire.b64u_decode(challenge['nonce'])) == 32 and abs(challenge['time'] - wire.now_ms()) < 5000
        sas = []
        await connection.pair(sas=sas)
        with pytest.raises(asyncio.TimeoutError):
            await connection.next(1.2)  # nothing before the person answers on the Mac
        waiting = await asyncio.to_thread(events.wait, lambda n, d: n == 'remote_state' and d.get('confirm'))
        assert waiting['confirm']['code'] == sas[0] and waiting['confirm']['device'] == 'iPhone 17'
        assert waiting['pairing'] is None, 'the code is spent once the signatures verify'
        await asyncio.to_thread(hub.remote_pair_confirm, True)
        frame = await connection.next()
        assert frame['t'] == 'paired' and frame['device_id'].startswith('d-')
        phone.device_id = frame['device_id']
        welcome = await connection.next()
        assert welcome['t'] == 'welcome' and welcome['v'] == 1 and welcome['resumed'] is False
        assert welcome['bridge']['name'] == 'Test Mac' and welcome['pebble']['running'] is True
        assert welcome['methods'] == list(policy.METHODS) and welcome['elevated'] == list(policy.ELEVATED)
        assert welcome['unlock_seconds'] == 900
        assert welcome['limits'] == {'max_request': 131072, 'max_frame': 8388608}
        devices = await connection.until(lambda f: f.get('event') == 'remote.devices')
        assert [row['device_id'] for row in devices['data']] == [phone.device_id] and devices['data'][0]['current']
        reused = await phone.connection().open()
        await reused.pair()
        bye = await reused.next()
        assert (bye['t'], bye['code']) == ('bye', 'bad_code') and bye['error']
        assert await reused.next() is None
        for method in policy.MAC_ROUTES:  # a phone never pairs, confirms or removes on the Mac's behalf
            answer = await connection.call(method, {'confirm': True})
            assert (answer['ok'], answer['code']) == (False, 'not_allowed'), method
        await connection.close()
        return phone, link

    phone, link = run(main())
    folder = tmp_path / 'data' / 'remote'
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    for name in ('devices.json', 'remote.json', 'tls-key.pem', 'tls-cert.pem', 'activity.json'):
        assert stat.S_IMODE(os.stat(folder / name).st_mode) == 0o600, name
    stored = json.loads((folder / 'devices.json').read_text())['devices'][phone.device_id]
    assert stored['device_key'] == phone.device_key.x963_b64u and stored['approve_key'] == phone.approve_key.x963_b64u
    assert 'iPhone 17 is now paired with this Mac' in lines(events)
    notice = next(d for d in events.named('remote_notice') if 'paired' in d['text'])
    assert notice['banner'] is True and len(notice['id']) == 32
    logged = '\n'.join(record.getMessage() for record in caplog.records)
    assert 'Remote conn#' in logged, 'the log has its content-free lines'
    for secret in (link['code'], link['pin'], 'pebble-remote://', 'iPhone 17', phone.device_key.x963_b64u,
                   phone.approve_key.x963_b64u, phone.device_id):
        assert secret not in logged, 'content-free logs'
    assert link['code'] not in json.dumps(events.items), 'the pairing code never leaves the main window'
    assert phone.device_key.x963_b64u not in json.dumps(events.items), 'nor do the keys'


def test_a_pairing_cancelled_unanswered_or_left_is_refused_and_spends_its_code(bridge, monkeypatch):
    hub, events, app = bridge

    async def main():
        link = hub.remote_pair_start()['link']
        connection = await Phone(link).connection().open()
        sas = []
        await connection.pair(sas=sas)
        await asyncio.to_thread(events.wait, lambda n, d: n == 'remote_state' and (d.get('confirm') or {})
                                .get('code') == sas[0])
        await asyncio.to_thread(hub.remote_pair_confirm, False)
        bye = await connection.next()
        assert (bye['t'], bye['code']) == ('bye', 'bad_code')
        assert bye['error'] == "Pairing wasn't confirmed on your Mac. Show a new pairing link there and try again."
        assert await connection.next() is None
        again = await Phone(link).connection().open()
        await again.pair()
        assert (await again.next())['code'] == 'bad_code', 'a refused pairing spends its code'
        # The phone leaves while the Mac waits: nothing is stored, and the Mac's question goes away.
        link = hub.remote_pair_start()['link']
        leaving = await Phone(link).connection().open()
        await leaving.pair()
        await asyncio.to_thread(events.wait, lambda n, d: n == 'remote_state' and d.get('confirm'))
        await leaving.close()
        await asyncio.to_thread(events.wait, lambda n, d: n == 'remote_state' and not d.get('confirm')
                                and not d.get('pairing'))
        with pytest.raises(ValueError, match='No iPhone is waiting'):
            await asyncio.to_thread(hub.remote_pair_confirm, True)
        # No answer within the confirmation's time is a refusal too (two minutes; shortened here).
        monkeypatch.setattr(remote_bridge, 'CONFIRM_SECONDS', 0.6)
        link = hub.remote_pair_start()['link']
        slow = await Phone(link).connection().open()
        await slow.pair()
        bye = await slow.until(lambda f: f['t'] in ('bye', 'paired'), 5)
        assert (bye['t'], bye['code']) == ('bye', 'bad_code')

    run(main())
    assert hub.status()['devices'] == []


def test_pairing_codes_have_five_tries_both_signatures_a_version_and_an_end(bridge, monkeypatch):
    hub, events, app = bridge

    async def refused(link, code=None, **change):
        connection = await Phone(link).connection().open()
        phone = connection.phone
        if change.get('same_keys'):
            phone.approve_key = phone.device_key
        if change.get('bad_approve'):
            phone.approve_key = wire.SigningKey()
            real = phone.approve_key.x963_b64u
            await connection.pair(code=code)  # signed with this approve key, then the frame names another below
            return real
        if change.get('version'):
            await connection.send({'t': 'pair', 'v': 2, 'code': code})
        else:
            await connection.pair(code=code)
        frame = await connection.next()
        await connection.next()
        return frame

    async def main():
        link = hub.remote_pair_start()['link']
        code = wire.parse_pairing_link(link)['code']
        assert (await refused(link, version=True))['code'] == 'bad_version'
        assert (await refused(link, same_keys=True))['code'] == 'bad_signature'
        # approve_sig by the device key over a pair naming a fresh approve key (conformance K-1)
        connection = await Phone(link).connection().open()
        phone, data = connection.phone, wire.parse_pairing_link(link)
        other = wire.SigningKey()
        transcript = wire.t_pair(data['bid'], connection.nonce, data['pin'], code, phone.device_key.x963_b64u,
                                 other.x963_b64u)
        await connection.send({'t': 'pair', 'v': 1, 'code': code, 'device': {}, 'device_key': phone.device_key.x963_b64u,
                               'approve_key': other.x963_b64u, 'sig': wire.b64u(phone.device_key.sign(transcript)),
                               'approve_sig': wire.b64u(phone.device_key.sign(transcript))})
        assert (await connection.next())['code'] == 'bad_signature'
        for _ in range(2):
            assert (await refused(link, code=wire.b64u(os.urandom(16))))['code'] == 'bad_code'
        # Four tries are spent (bad version is not a try); the fifth, with the right code, still pairs.
        phone, connection = await paired(hub, events, Phone(link))
        await connection.close()
        link = hub.remote_pair_start()['link']
        for _ in range(5):
            assert (await refused(link, code=wire.b64u(os.urandom(16))))['code'] == 'bad_code'
        assert (await refused(link))['code'] == 'bad_code', 'five tries and the right code is refused too'
        link = hub.remote_pair_start()['link']
        hub.bridge.offer['expires'] = time.monotonic() - 1
        assert (await refused(link))['code'] == 'bad_code', 'an expired code'
        hub.remote_pair_start()
        hub.remote_pair_cancel()
        assert hub.status()['pairing'] is None
        # Frames before pair or hello get no answer, and the connection closes.
        early = await Phone(link).connection().open()
        await early.send({'t': 'req', 'id': 'early', 'method': 'state', 'params': {}})
        assert await early.next() is None

    run(main())


# ---- the handshake and resume (3.3) -------------------------------------------------------------------------------

def test_hello_refusals_one_connection_per_device_and_resume_in_order(bridge):
    hub, events, app = bridge

    async def main():
        phone, first = await paired(hub, events)
        await first.until(lambda f: f.get('event') == 'remote.devices')
        wrong = await phone.connection().open()
        assert (await wrong.hello(key=phone.approve_key))['code'] == 'bad_signature'
        replay = await phone.connection().open()
        assert (await replay.hello(nonce=first.nonce))['code'] == 'bad_signature'
        unknown = await phone.connection().open()
        assert (await unknown.hello(device_id='d-unknown'))['code'] == 'unknown_device'
        version = await phone.connection().open()
        assert (await version.hello(version=2))['code'] == 'bad_version'
        assert (await first.call('remote.ping', {'ts': 1}, idem=False))['ok'] is True, 'the paired one is undisturbed'
        epoch, seen = first.welcome['epoch'], max([first.welcome['seq']] + [f['seq'] for f in first.seen
                                                                             if f['t'] == 'evt'])
        await first.close()
        for index in range(5):
            hub.tee('progress', {'chat_id': 'c1', 'run_id': 'r1', 'label': 'step %d' % index})
        hub.tee('native_export_pdf', {'chat_id': 'c1', 'attach_token': 'secret'})
        hub.tee('remote_state', {'confirm': {'code': '123456'}})
        await asyncio.sleep(0.3)
        later = await phone.connection().open()
        welcome = await later.hello(resume={'epoch': epoch, 'seq': seen})
        assert welcome['resumed'] is True and welcome['seq'] == seen + 5
        replayed = [await later.next() for _ in range(5)]
        assert [f['seq'] for f in replayed] == list(range(seen + 1, seen + 6))
        assert [f['data']['label'] for f in replayed] == ['step %d' % i for i in range(5)]
        assert all(f['event'] == 'progress' and abs(f['ts'] - wire.now_ms()) < 10000 for f in replayed)
        newer = await phone.connection().open()
        stale = await newer.hello(resume={'epoch': 'e-stale', 'seq': 1})
        assert stale['resumed'] is False
        bye = await later.until(lambda f: f['t'] == 'bye')
        assert bye['code'] == 'replaced' and bye['error']
        ahead = await phone.connection().open()
        assert (await ahead.hello(resume={'epoch': epoch, 'seq': 10 ** 9}))['resumed'] is False

    run(main())


# ---- the allowlist, the unlock and proofs (3.6, 3.7, C1, C2, S-05) --------------------------------------------------

def test_reads_work_before_the_unlock_acting_does_not_and_the_allowlist_holds(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        for method, params in (('state', {}), ('chat_search', {'query': 'x'}), ('remote.devices', {}),
                               ('remote.status', {}), ('remote.ping', {'ts': 7}), ('remote.lock', {})):
            answer = await connection.call(method, params, idem=False)
            assert answer['ok'] is True, (method, answer)
        status = (await connection.call('remote.status', idem=False))['result']
        assert status['mac'] == {'name': 'Test Mac', 'battery_percent': 80, 'charging': False, 'on_power': False,
                                 'keep_awake': True}
        assert status['bridge']['endpoints'] == ['127.0.0.1:%d' % phone.port] and status['pebble']['running']
        devices = (await connection.call('remote.devices', idem=False))['result']
        assert devices[0]['current'] is True and set(devices[0]) == {'device_id', 'name', 'paired_at', 'last_seen',
                                                                     'current'}
        for method, params in (('new_chat', {}), ('model_profiles', {}), ('remote.keep_awake', {'on': False}),
                               ('remote.screenshot', {'display': None}), ('remote.folders', {}), ('send', {'id': 'c1',
                                                                                                           'text': 'hi'})):
            answer = await connection.call(method, params)
            assert (answer['ok'], answer['code']) == (False, 'unlock_required'), method
            assert answer['error'] == 'Unlock Pebble Remote with Face ID before Pebble acts on what you ask.'
        for method in ('attach', 'set_model', 'chatgpt_start', 'connector_start', 'plugin_package_install',
                       'extension_add', 'lifecycle_event', 'export_pdf_attach', 'storage_reveal', 'no_such_method',
                       'remote.not_a_method'):
            answer = await connection.call(method, {'conformance_check': True})
            assert (answer['ok'], answer['code']) == (False, 'not_allowed'), method
        assert not [m for m, _ in app.calls if m not in ('state', 'chat_search')], 'nothing else reached Pebble'

    run(main())


def test_every_bad_proof_is_refused_a_good_one_unlocks_and_c2_refuses_its_twin(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        for method, params in (('approve', {'id': 'c1', 'action_id': 'a1'}), ('setting', {'key': 'permissions',
                                                                                           'value': {}}),
                               ('remote.unlock', {}), ('open_url', {'url': 'https://example.com'})):
            answer = await connection.call(method, params)
            assert (answer['ok'], answer['code']) == (False, 'proof_required'), method
        variants = [dict(key=phone.device_key), dict(ts=wire.now_ms() - 120000), dict(ts=wire.now_ms() + 120000),
                    dict(params_json='{ }', signed_json='{}'), dict(sign_method='approve'),
                    dict(epoch='e-not-this-one'), dict(nonce=wire.b64u(os.urandom(32))), dict(sign_idem='other'),
                    dict(v02=True)]
        for options in variants:
            answer = await connection.exchange(connection.elevated('remote.unlock', {}, **options))
            assert (answer['ok'], answer['code']) == (False, 'bad_proof'), options
        for broken in ({'ts': wire.now_ms()}, {'ts': wire.now_ms(), 'sig': 'not base64!'},
                       {'ts': wire.now_ms(), 'sig': wire.b64u(b'\x30\x02\x02\x00')}, {'sig': 'AAAA'},
                       {'ts': str(wire.now_ms()), 'sig': 'AAAA'}, 'nope'):
            frame = connection.elevated('remote.unlock', {})
            frame['proof'] = broken
            answer = await connection.exchange(frame)
            assert (answer['ok'], answer['code']) == (False, 'bad_proof'), broken
        assert (await connection.call('new_chat', {}))['code'] == 'unlock_required', 'no refused proof unlocked'
        good = connection.elevated('remote.unlock', {})
        answer = await connection.exchange(good)
        assert answer['ok'] is True
        assert answer['result']['unlock_seconds'] == 900
        assert abs(answer['result']['unlocked_until'] - wire.now_ms() - 900000) < 5000
        assert (await connection.call('new_chat', {}))['ok'] is True
        again = connection.elevated('remote.unlock', {}, request_id=good['id'])
        assert (await connection.exchange(again))['code'] == 'bad_proof', 'a request id is used once'
        approve = await connection.exchange(connection.elevated('approve', {'id': 'c1', 'action_id': 'nope'}))
        assert approve['ok'] is False and 'code' not in approve and approve['error'].startswith('This action')
        missing = dict(connection.elevated('remote.unlock', {}))
        del missing['params_json']
        assert (await connection.exchange(missing))['code'] == 'bad_request'
        listed = connection.elevated('setting', [1, 2])
        assert (await connection.exchange(listed))['code'] == 'bad_request', 'params_json must be an object'
        # C2: the accepted frame on another connection, verbatim and under a new id
        other = await phone.connection().open()
        await other.hello()
        assert (await other.exchange(dict(good)))['code'] == 'bad_proof'
        assert (await other.exchange(dict(good, id='moved-1')))['code'] == 'bad_proof'
        # ... and its (r, n-s) twin, which verifies too. With the id rule set aside (an id memory that forgot), the
        # memory keyed by (device, r) is what refuses it; without that memory, the twin would pass.
        fresh = other.elevated('remote.unlock', {})
        assert (await other.exchange(fresh))['ok'] is True
        r, s = wire.der_decode_signature(wire.b64u_decode(fresh['proof']['sig']))
        twin = dict(fresh, proof=dict(fresh['proof'], sig=wire.b64u(wire.der_encode_signature(r, wire.N - s))))
        conn = next(c for c in hub.bridge.conns if c.nonce == other.nonce)
        await asyncio.to_thread(hub._in_loop, conn.used.pop, fresh['id'], None)
        assert (await other.exchange(twin))['code'] == 'bad_proof', 'C2 keyed by r refuses the twin'
        await asyncio.to_thread(hub._in_loop, conn.used.pop, fresh['id'], None)
        await asyncio.to_thread(hub._in_loop, hub.bridge.proofs.clear)
        assert (await other.exchange(twin))['ok'] is True, 'the twin verifies: only the r memory stopped it'

    run(main())


def test_setting_keys_cannot_dodge_their_proof_and_plain_keys_need_only_the_unlock(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.unlock()
        for key in ('permissions', 'git_decisions', 'memory_enabled', 'memory_layers', 'notifications'):
            assert (await connection.call('setting', {'key': key, 'value': None}))['code'] == 'proof_required', key
        assert (await connection.call('setting', {'key': 'theme', 'value': 'dark'}))['ok'] is True
        for params in ('{"key":"theme","key":"permissions","value":1}', '{"key":["permissions"],"value":1}',
                       '{"key":"Permissions","value":1}', '{"key":"permissions ","value":1}'):
            request_id = phone.next_id()
            await connection.send('{"t":"req","id":"%s","method":"setting","params":%s,"idem":"%s"}'
                                  % (request_id, params, request_id))
            answer = await connection.until(lambda f: f.get('id') == request_id)
            assert (answer['ok'], answer['code']) == (False, 'proof_required'), params
        assert app.called('setting') == [{'key': 'theme', 'value': 'dark'}]

    run(main())


def test_unlock_lock_and_the_last_connection(bridge):
    hub, events, app = bridge

    async def main():
        phone, first = await paired(hub, events)
        await first.unlock()
        assert (await first.call('model_profiles'))['ok'] is True
        lock = await first.call('remote.lock', {}, idem=False)
        assert (lock['ok'], lock['result']) == (True, {})
        assert (await first.call('model_profiles'))['code'] == 'unlock_required'
        assert (await first.call('remote.lock', {}, idem=False))['result'] == {}
        await first.unlock()
        second = await phone.connection().open()
        await second.hello()  # replaces the first: not the device's last connection
        await first.until(lambda f: f['t'] == 'bye')
        assert (await second.call('model_profiles'))['ok'] is True, 'a replaced connection keeps the unlock'
        await asyncio.to_thread(hub.remote_set, 'unlock_minutes', 5)
        assert hub.bridge.unlock_seconds == 300
        assert hub.bridge.unlocked[phone.device_id][0] - time.monotonic() <= 301, 'a shorter window applies at once'
        await second.close()
        await asyncio.sleep(0.3)
        third = await phone.connection().open()
        welcome = await third.hello()
        assert welcome['unlock_seconds'] == 300
        assert (await third.call('model_profiles'))['code'] == 'unlock_required', 'the last connection ended it'

    run(main())


# ---- idem (3.4, v0.3 item 4) --------------------------------------------------------------------------------------

def test_a_repeat_returns_the_first_answer_and_acts_once_on_any_connection(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.unlock()
        first = await connection.call('send', {'id': 'c1', 'text': 'hello'}, idem='idem-1')
        second = await connection.call('send', {'id': 'c1', 'text': 'hello'}, idem='idem-1')
        assert first['result'] == second['result'] and len(app.called('send')) == 1
        assert (await connection.call('cancel', {}, idem='idem-1'))['code'] == 'bad_request', 'another method'
        assert (await connection.call('chat_rename', {'id': 'c1', 'title': 'x'}, idem=False))['code'] == 'bad_request'
        assert (await connection.call('state', idem=False))['ok'] is True
        assert (await connection.call('send', {}, idem='has space'))['code'] == 'bad_request'
        await connection.close()
        later = await phone.connection().open()
        await later.hello()
        await later.unlock()
        third = await later.call('send', {'id': 'c1', 'text': 'hello'}, idem='idem-1')
        assert third['result'] == first['result'] and len(app.called('send')) == 1, 'a retry after a reconnect'
        for ident in ('has space', 'dot.ted', 'x' * 65, 'line\nbreak', ''):
            await later.send({'t': 'req', 'id': ident, 'method': 'remote.ping', 'params': {'ts': 1}})
            answer = await later.until(lambda f: f['t'] == 'res' and f.get('id') in (ident, None))
            assert (answer['ok'], answer['code']) == (False, 'bad_request')
            assert answer['id'] == (ident if len(ident) <= 64 and '\n' not in ident else None)
        assert (await later.call('remote.ping', {'ts': 2}, idem=False, request_id='Az09_-' + 'y' * 58))['ok']

    run(main())


# ---- limits (3.9, 3.11 item 6, X-5) -------------------------------------------------------------------------------

def test_size_rate_and_nesting_limits(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.unlock()
        big = await connection.call('chat_search', {'query': 'x' * 133000}, idem=False, request_id='too-large-1')
        assert (big['ok'], big['code']) == (False, 'too_large')
        params = await connection.call('send', {'id': 'c1', 'text': 'x' * 128500})
        assert params['code'] == 'too_large', "over the shell's 128,000 for params"
        assert (await connection.call('remote.ping', {'ts': 1}, idem=False))['ok'] is True, 'the connection stays'
        await asyncio.sleep(2.2)
        frames = [{'t': 'req', 'id': 'burst-%d' % i, 'method': 'remote.ping', 'params': {'ts': i}} for i in range(40)]
        for frame in frames:
            await connection.send(frame)
        answers = [await connection.until(lambda f: f.get('id') == 'burst-%d' % i) for i in range(40)]
        limited = sum(1 for a in answers if a.get('code') == 'rate_limited')
        assert limited >= 1 and sum(1 for a in answers if a['ok']) >= 15
        await asyncio.sleep(2.2)
        await connection.send('{"t":"req","id":"unreadable-1","method":"state","params":{"pad":"' + 'x' * 133000)
        bye = await connection.next()
        assert (bye['t'], bye['code']) == ('bye', 'too_large') and bye['error']
        assert await connection.next() is None
        for depth in (20000, 300000):
            deep = await phone.connection().open()
            await deep.hello()
            try:
                await deep.send('{"t":"req","id":"deep-1","method":"remote.ping","params":{"ts":1,"pad":' + '[' * depth
                                + ']' * depth + '}}')
            except (ConnectionError, OSError):
                pass
            frame = await deep.until(lambda f: f['t'] in ('res', 'bye'))
            assert frame is None or frame['t'] == 'bye'
        huge = await phone.connection().open()
        await huge.hello()
        try:
            await huge.ws.send_frame(wire.OP_TEXT, b'{' * (2 << 20))
        except (ConnectionError, OSError):
            pass
        assert await huge.until(lambda f: False, 5) is None and huge.ws.close_code in (1009, 1006)
        after = await phone.connection().open()
        await after.hello()
        assert (await after.call('remote.ping', {'ts': 3}, idem=False))['ok'] is True, 'the bridge keeps serving'
        app.huge = True
        oversize = await after.call('remote.chat_tail', {'id': 'c1', 'limit': 1}, idem=False)
        assert (oversize['ok'], oversize['code']) == (False, 'too_large'), 'an answer over 8 MB'

    run(main())


def test_answers_too_deep_for_the_phone_are_clipped_and_unwritable_ones_refused(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.send({'t': 'req', 'id': 'deep-answer', 'method': 'run_get', 'params': {'deep': True}})
        message = await connection.until(lambda f: f['t'] == 'res' and f['id'] == 'deep-answer')
        assert not remote_bridge.too_deep(json.dumps(message), 64), 'the phone refuses frames over 64 levels (S-02)'
        node, depth = message['result']['args'], 0
        while isinstance(node, dict):
            node, depth = node['next'], depth + 1
        assert node == '(nested too deeply to show here)' and 50 < depth < 64
        nan = await connection.call('run_get', {'nan': True}, idem=False)
        assert (nan['ok'], nan['error']) == (False, remote_bridge.DEFECT_TEXT)
        tail = (await connection.call('remote.chat_tail', {'id': 'c1', 'limit': 3}, idem=False))['result']
        assert [m['id'] for m in tail['messages']] == ['m7', 'm8', 'm9'] and tail['has_older'] is True
        before = (await connection.call('remote.chat_before', {'id': 'c1', 'before': 'm7', 'limit': 3},
                                        idem=False))['result']
        assert before == {'messages': [{'id': 'm%d' % i, 'role': 'user', 'content': str(i)} for i in (4, 5, 6)],
                          'has_older': True}
        first = (await connection.call('remote.chat_before', {'id': 'c1', 'before': 'm2', 'limit': 5}, idem=False))
        assert [m['id'] for m in first['result']['messages']] == ['m0', 'm1'] and not first['result']['has_older']
        assert (await connection.call('remote.chat_before', {'id': 'c1', 'before': 'nope', 'limit': 5},
                                      idem=False))['error'] == 'Message not found in this conversation.'
        for params in ({'id': 'c1', 'limit': 0}, {'id': 'c1', 'limit': 501}, {'limit': 5}, {'id': 'c1', 'limit': '5'}):
            assert (await connection.call('remote.chat_tail', params, idem=False))['code'] == 'bad_request'

    run(main())


# ---- revocation (4.6, X-8) ----------------------------------------------------------------------------------------

def test_revocation_is_instant_from_the_mac_or_the_phone_and_refuses_the_key(bridge):
    hub, events, app = bridge

    async def main():
        one, first = await paired(hub, events)
        two, second = await paired(hub, events)
        await first.unlock()
        other = await first.call('remote.revoke_device', {'device_id': two.device_id})
        assert other['code'] == 'proof_required', "removing another phone needs Face ID (security review X-8)"
        await asyncio.to_thread(hub.remote_device_remove, two.device_id)
        bye = await second.until(lambda f: f['t'] == 'bye', 3)
        assert bye['code'] == 'revoked' and await second.next() is None
        back = await two.connection().open()
        assert (await back.hello())['code'] == 'revoked'
        devices = await first.until(lambda f: f.get('event') == 'remote.devices' and len(f['data']) == 1)
        assert devices['data'][0]['device_id'] == one.device_id
        answer = await first.call('remote.revoke_device', {'device_id': one.device_id})
        assert answer['ok'] is True and answer['result'] == {}
        assert (await first.until(lambda f: f['t'] == 'bye'))['code'] == 'revoked'
        final = await one.connection().open()
        assert (await final.hello())['code'] == 'revoked'
        with pytest.raises(ValueError):
            hub.remote_device_remove(one.device_id)

    run(main())
    assert hub.status()['devices'] == []
    assert 'iPhone 17 removed itself from this Mac' in lines(events)


# ---- events (3.5, C4) ---------------------------------------------------------------------------------------------

def test_events_are_forwarded_unchanged_merged_in_30_ms_and_trimmed_when_too_big_or_deep(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.until(lambda f: f.get('event') == 'remote.devices')
        for piece in ('Hel', 'lo', ' wor', 'ld'):
            hub.tee('token', {'chat_id': 'c1', 'id': 'm1', 'text': piece})
        hub.tee('progress', {'chat_id': 'c1', 'run_id': 'r1', 'label': 'Reading', 'nested': {'a': [1, {'b': None}]}})
        hub.tee('token', {'chat_id': 'c1', 'id': 'm1', 'text': '!'})
        hub.tee('reasoning', {'chat_id': 'c1', 'run_id': 'r1', 'attempt_id': 't1', 'delta': 'th'})
        hub.tee('reasoning', {'chat_id': 'c1', 'run_id': 'r1', 'attempt_id': 't1', 'delta': 'ink'})
        hub.tee('activity', {'chat_id': 'c1', 'run_id': 'r1', 'item': {'kind': 'tool', 'label': 'Read'}})
        hub.tee('native_export_pdf', {'attach_token': 'secret'})
        hub.tee('remote_notice', {'text': 'x'})
        hub.tee('artifact', {'chat_id': 'c1', 'id': 'big', 'body': 'x' * (1 << 20)})
        deep = {'chat_id': 'c1', 'run_id': 'r1'}
        node = deep
        for _ in range(80):
            node['next'] = {}
            node = node['next']
        hub.tee('actions', deep)
        frames = [await connection.until(lambda f: f['t'] == 'evt') for _ in range(7)]
        names = [f['event'] for f in frames]
        assert names == ['token', 'progress', 'token', 'reasoning', 'activity', 'artifact', 'actions'], names
        assert frames[0]['data'] == {'chat_id': 'c1', 'id': 'm1', 'text': 'Hello world'}, 'merged within 30 ms'
        assert frames[1]['data'] == {'chat_id': 'c1', 'run_id': 'r1', 'label': 'Reading',
                                     'nested': {'a': [1, {'b': None}]}}, 'forwarded unchanged'
        assert frames[2]['data']['text'] == '!' and frames[3]['data']['delta'] == 'think'
        assert frames[4]['data']['item'] == {'kind': 'tool', 'label': 'Read'}
        assert frames[5]['data'] == {'chat_id': 'c1', 'id': 'big', 'trimmed': True}
        assert frames[6]['data'] == {'chat_id': 'c1', 'run_id': 'r1', 'trimmed': True}
        seqs = [f['seq'] for f in frames]
        assert seqs == list(range(seqs[0], seqs[0] + 7)), 'one seq per event sent, in order'
        assert not any(f.get('event') in ('native_export_pdf', 'remote_notice') for f in connection.seen)

    run(main())


def test_a_phone_that_stops_reading_is_dropped_at_4_mb_and_others_are_not(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.close()
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        reader, writer = await asyncio.open_connection('127.0.0.1', phone.port, ssl=context, limit=1 << 16)
        socket_ = writer.get_extra_info('socket')
        if socket_ is not None:
            socket_.setsockopt(__import__('socket').SOL_SOCKET, __import__('socket').SO_RCVBUF, 4096)
        ws, _ = await wire.client_handshake(reader, writer, '127.0.0.1')
        challenge = json.loads(await ws.recv())
        link = phone.link
        signature = phone.device_key.sign(wire.t_hello(link['bid'], phone.device_id, challenge['nonce'], link['pin']))
        await ws.send_text(json.dumps({'t': 'hello', 'v': 1, 'device_id': phone.device_id, 'sig': wire.b64u(signature),
                                       'resume': None}))
        assert json.loads(await ws.recv())['t'] == 'welcome'
        for index in range(12):  # about 10 MB, never read
            hub.tee('artifact', {'chat_id': 'c1', 'id': 'a%d' % index, 'body': 'y' * 900000})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and hub.bridge.by_device.get(phone.device_id):
            await asyncio.sleep(0.1)
        assert hub.bridge.by_device.get(phone.device_id) is None, 'dropped once more than 4 MB waited'
        writer.transport.abort()
        again = await phone.connection().open()
        assert (await again.hello())['t'] == 'welcome', 'and it comes back'

    run(main())


# ---- v0.5: folders, new projects, opening things on the Mac ------------------------------------------------------

def test_folders_and_connecting_a_new_project_follow_v05(bridge, tmp_path):
    hub, events, app = bridge
    tree = tmp_path / 'tree'
    for name in ('apps/web/.git', 'apps/notes', '.ssh', 'secrets'):
        (tree / name).mkdir(parents=True)
    os.symlink(tree / 'apps', tree / 'link')
    real = os.path.realpath(tree)
    app.workspaces.add(real + '/apps/notes')

    async def main():
        phone, connection = await paired(hub, events)
        assert (await connection.call('remote.folders', {}))['code'] == 'unlock_required'
        new = real + '/apps/web'
        for method, params in (('set_workspace', {'id': 'c1', 'path': new}), ('new_chat', {'workspace': new}),
                               ('set_workspace', {'id': 'c1', 'path': '/usr'})):
            assert (await connection.call(method, params))['code'] == 'proof_required', 'locked or not, the same'
        await connection.unlock()
        listing = (await connection.call('remote.folders', {}, idem=False))['result']
        assert listing['home'] == real and listing['parent'] is None
        assert [(row['name'], row['is_project']) for row in listing['folders']] == [('apps', False)]
        inner = (await connection.call('remote.folders', {'path': real + '/apps'}, idem=False))['result']
        assert [(row['name'], row['is_project']) for row in inner['folders']] == [('notes', False), ('web', True)]
        for path in ('/usr', real + '/..', real + '/link', real + '/.ssh', real + '/secrets'):
            answer = await connection.call('remote.folders', {'path': path}, idem=False)
            assert (answer['ok'], answer['code']) == (False, 'not_allowed'), path
        assert (await connection.call('set_workspace', {'id': 'c1', 'path': None}))['ok'] is True
        assert (await connection.call('set_workspace', {'id': 'c1', 'path': real + '/apps/notes'}))['ok'] is True
        assert (await connection.call('set_workspace', {'id': 'c1', 'path': new}))['code'] == 'proof_required'
        for path in ('/usr', real + '/.ssh', real + '/link', real):
            answer = await connection.exchange(connection.elevated('set_workspace', {'id': 'c1', 'path': path}))
            assert (answer['ok'], answer['code']) == (False, 'not_allowed'), path
            answer = await connection.exchange(connection.elevated('new_chat', {'workspace': path}))
            assert (answer['ok'], answer['code']) == (False, 'not_allowed'), path
        assert (await connection.exchange(connection.elevated('set_workspace', {'id': 'c1', 'path': new})))['ok']
        assert app.called('set_workspace')[-1] == {'id': 'c1', 'path': new}
        assert not [p for p in app.called('set_workspace') + app.called('new_chat')
                    if (p.get('path') or p.get('workspace') or '') in ('/usr', real + '/.ssh', real + '/link', real)]
        for method in policy.OPEN_ON_MAC:
            answer = await connection.call(method, {'conformance_check': True})
            assert (answer['ok'], answer['code']) == (False, 'proof_required'), method
        opened = await connection.exchange(connection.elevated('open_url', {'url': 'https://example.com'}))
        assert opened['ok'] is True and app.called('open_url') == [{'url': 'https://example.com'}]

    run(main())
    assert 'iPhone 17 opened a page on this Mac' in lines(events)
    assert next(d for d in events.named('remote_notice') if 'opened a page' in d['text'])['banner'] is True


def test_the_main_windows_surface_is_written_whatever_the_phone_sends(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.unlock()
        await connection.call('rewind_preview', {'chat_id': 'c1', 'run_id': 'r1', 'step': 0, 'surface': 'pet'})
        await connection.exchange(connection.elevated('quiz_share', {'chat_id': 'c1', 'attempt_id': 'q1',
                                                                     'surface': 'pet'}))

    run(main())
    assert app.called('rewind_preview')[0]['surface'] == 'main' and app.called('quiz_share')[0]['surface'] == 'main'


# ---- C3, the Mac's record (4.8, C7), Pebble's refusals, keep awake ----------------------------------------------------

def test_screenshots_are_scaled_jpegs_with_a_notice_on_the_mac(bridge, monkeypatch):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        await connection.unlock()
        shot = (await connection.call('remote.screenshot', {'display': None}, idem=False))['result']
        assert abs(shot['taken_at'] - wire.now_ms()) < 10000
        sizes = [(d['index'], d['width'], d['height'], d['format']) for d in shot['displays']]
        assert sizes == [(0, 2560, 1663, 'jpeg'), (1, 1920, 1080, 'jpeg')]
        assert all(jpeg_size(base64.b64decode(d['data'])) == (d['width'], d['height']) for d in shot['displays'])
        one = (await connection.call('remote.screenshot', {'display': 1}, idem=False))['result']
        assert [d['index'] for d in one['displays']] == [1]
        absent = await connection.call('remote.screenshot', {'display': 7}, idem=False)
        assert absent['ok'] is False and 'code' not in absent and absent['error'] == 'This Mac has no display 7.'
        assert (await connection.call('remote.screenshot', {'display': 'x'}, idem=False))['code'] == 'bad_request'
        hub.dev['screen_allowed'] = False
        refused = await connection.call('remote.screenshot', {'display': None}, idem=False)
        assert refused['ok'] is False and 'code' not in refused and 'Screen & System Audio Recording' in refused['error']

    run(main())
    taken = [d for d in events.named('remote_notice') if d['text'] == 'iPhone 17 took a screenshot of this Mac']
    assert len(taken) == 2 and all(d['banner'] for d in taken), 'a notice on the Mac for every screenshot'


def test_the_mac_records_what_a_phone_did_and_pebbles_refusals_pass_without_bridge_codes(bridge):
    hub, events, app = bridge
    app.cards['c1'] = [{'id': 'a1', 'kind': 'command', 'payload': {'command': 'npm test'}},
                       {'id': 'a2', 'kind': 'edit', 'payload': {'path': '/Users/me/p/src/app.ts'}}]

    async def main():
        phone, connection = await paired(hub, events)
        await connection.unlock()
        assert (await connection.exchange(connection.elevated('approve', {'id': 'c1', 'action_id': 'a1'})))['ok']
        assert (await connection.call('reject', {'id': 'c1', 'action_id': 'a2'}))['ok']
        assert (await connection.call('send', {'id': 'c1', 'text': 'go on'}))['ok']
        assert (await connection.call('remote.keep_awake', {'on': False}))['result'] == {'keep_awake': False}
        mac = await connection.event(lambda f: f.get('event') == 'remote.mac')
        assert mac['data']['keep_awake'] is False
        full = await connection.call('send', {'id': 'c1', 'text': 'full disk'})
        assert (full['ok'], full['code'], full['error']) == (False, 'disk_full', 'The disk is full.')
        spoof = await connection.call('send', {'id': 'c1', 'text': 'bridge code'})
        assert spoof['ok'] is False and 'code' not in spoof, "Pebble's code never poses as the bridge's (X-6)"
        defect = await connection.call('send', {'id': 'c1', 'text': 'defect'})
        assert defect['error'] == remote_bridge.DEFECT_TEXT and 'secret' not in json.dumps(defect)

    run(main())
    recorded = lines(events)
    assert 'iPhone 17 approved: npm test' in recorded and 'iPhone 17 declined: an edit to app.ts' in recorded
    assert 'iPhone 17 sent a message' in recorded and 'iPhone 17 turned keep awake off' in recorded
    banners = {d['text']: d['banner'] for d in events.named('remote_notice')}
    assert banners['iPhone 17 approved: npm test'] is True and banners['iPhone 17 sent a message'] is False
    assert hub.status()['keep_awake'] is False and hub.settings['keep_awake'] is False
    assert [line['text'] for line in hub.status()['activity']][0] == 'iPhone 17 turned keep awake off'


# ---- the WebSocket decoder under random and malformed frames ---------------------------------------------------------

class _Writer:
    def __init__(self):
        self.data, self.closed = bytearray(), False
        self.transport = self

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def abort(self):
        self.closed = True


def _masked(opcode, payload, fin=True, rsv=0, mask=b'\x01\x02\x03\x04', length=None):
    size = len(payload) if length is None else length
    head = bytes([(0x80 if fin else 0) | rsv << 4 | opcode])
    if size < 126:
        head += bytes([0x80 | size])
    elif size < 65536:
        head += bytes([0x80 | 126]) + size.to_bytes(2, 'big')
    else:
        head += bytes([0x80 | 127]) + size.to_bytes(8, 'big')
    return head + mask + remote_ws.unmask(payload, mask)


def _decode(data, max_message=1 << 20):
    async def main():
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        socket_ = remote_ws.Socket(reader, _Writer(), max_message)
        out = []
        while True:
            message = await asyncio.wait_for(socket_.recv(), 5)
            if message is None:
                return out, socket_.close_code
            out.append(message)
    return asyncio.run(main())


def test_the_decoder_refuses_each_malformed_frame_with_its_close_code():
    text = _masked(remote_ws.OP_TEXT, b'{"t":"req"}')
    assert _decode(text) == (['{"t":"req"}'], 1006)
    pieces = _masked(remote_ws.OP_TEXT, b'{"a"', fin=False) + _masked(remote_ws.OP_PING, b'p') + \
        _masked(remote_ws.OP_CONT, b':1}')
    assert _decode(pieces)[0] == ['{"a":1}'], 'a ping between fragments'
    split = 'é'.encode()
    assert _decode(_masked(remote_ws.OP_TEXT, split[:1], fin=False) + _masked(remote_ws.OP_CONT, split[1:]))[0] == ['é']
    cases = {
        'reserved bits': (_masked(remote_ws.OP_TEXT, b'x', rsv=4), 1002),
        'unknown opcode': (_masked(0x3, b'x'), 1002),
        'unmasked': (bytes([0x81, 0x01]) + b'x', 1002),
        'non-minimal 16-bit length': (_masked(remote_ws.OP_TEXT, b'x' * 10, length=10).replace(bytes([0x8a]), bytes(
            [0x80 | 126, 0, 10]), 1), 1002),
        'fragmented control frame': (_masked(remote_ws.OP_PING, b'x', fin=False), 1002),
        'control frame over 125': (_masked(remote_ws.OP_PING, b'x' * 126), 1002),
        'continuation first': (_masked(remote_ws.OP_CONT, b'x'), 1002),
        'new message inside one': (_masked(remote_ws.OP_TEXT, b'x', fin=False) + _masked(remote_ws.OP_TEXT, b'y'), 1002),
        'binary': (_masked(remote_ws.OP_BINARY, b'\x00'), 1003),
        'not UTF-8': (_masked(remote_ws.OP_TEXT, b'\xff\xfe'), 1007),
        'too big': (_masked(remote_ws.OP_TEXT, b'', length=(1 << 20) + 1), 1009),
        'too big in pieces': (_masked(remote_ws.OP_TEXT, b'x' * 70000, fin=False) + _masked(remote_ws.OP_CONT,
                                                                                            b'x' * 70000), 1009),
        'close with one byte': (_masked(remote_ws.OP_CLOSE, b'\x03'), 1002),
    }
    for label, (data, code) in cases.items():
        messages, closed = _decode(data, max_message=100000 if label == 'too big in pieces' else 1 << 20)
        assert messages == [] and closed == code, (label, messages, closed)


def test_the_decoder_survives_random_frames():
    rng = random.Random(46)
    for _ in range(1500):
        kind = rng.random()
        if kind < 0.4:
            data = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 200)))
        else:
            data = b''.join(_masked(rng.choice([0, 1, 2, 3, 8, 9, 10, 15]),
                                    bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 140))),
                                    fin=rng.random() < 0.7, rsv=rng.choice([0, 0, 0, 1]),
                                    mask=bytes(rng.getrandbits(8) for _ in range(4)))
                            for _ in range(rng.randint(1, 4)))
        messages, closed = _decode(data)  # ends, with nothing raised, whatever the bytes were
        assert isinstance(closed, int) and all(isinstance(m, str) for m in messages)


def test_random_and_malformed_frames_over_the_network_leave_the_bridge_serving(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        rng = random.Random(1929)
        for round_ in range(12):
            raw = await phone.connection().open()
            if round_ % 2:
                await raw.hello()
            if round_ % 3:  # random bytes, then the phone goes: whatever they declared, the bridge lets go
                raw.ws.writer.write(bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 300))))
                raw.ws.abort()
                continue
            raw.ws.writer.write(_masked(rng.choice([0, 3, 2]), bytes(rng.getrandbits(8) for _ in range(100))))
            await raw.until(lambda f: False, 3)
            assert raw.closed and raw.ws.close_code in (1002, 1003)
        for text in ('[]', '"x"', '{"t":"req"', 'null', '{"t":"pair","v":1}', '{"t":"hello","v":1,"device_id":5}'):
            fresh = await phone.connection().open()
            await fresh.send(text)
            frame = await fresh.until(lambda f: f['t'] in ('res', 'welcome'), 3)
            assert frame is None or frame['t'] != 'welcome'
        another = await phone.connection().open()  # the hellos above replaced the first connection (3.1)
        assert (await another.hello())['t'] == 'welcome'
        assert (await another.call('remote.ping', {'ts': 5}, idem=False))['ok'] is True

    run(main())


def test_a_client_without_tls_is_dropped_and_logged_without_its_address(bridge, caplog):
    hub, events, app = bridge
    caplog.set_level(logging.DEBUG)

    async def main():
        reader, writer = await asyncio.open_connection('127.0.0.1', hub.bridge.port)
        writer.write(b'GET /pebble-remote/v1 HTTP/1.1\r\nHost: x\r\n\r\n' + os.urandom(64))
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 15) == b'', 'no answer to plain text on a TLS port'
        writer.close()
        phone, connection = await paired(hub, events)
        assert (await connection.call('remote.ping', {'ts': 1}, idem=False))['ok'] is True

    run(main())
    logged = '\n'.join(record.getMessage() for record in caplog.records if record.name.startswith(('pebble', 'asyncio')))
    assert '127.0.0.1' not in logged and 'peername' not in logged, 'connection reports carry no address'


def test_the_bridge_listens_again_when_the_network_changes_and_lists_only_what_it_serves(tmp_path, monkeypatch):
    plan = {'now': ([], [])}
    monkeypatch.setattr(remote_bridge, 'NETWORK_SECONDS', 0.3)
    app = FakeApp()
    hub, events = start_hub(tmp_path, app, monkeypatch, link_loopback=False, address_plan=lambda: plan['now'])
    try:
        with pytest.raises(ValueError, match='Connect this Mac to Wi-Fi or to Tailscale'):
            hub.remote_pair_start()  # loopback alone is never put in a link a phone could use
        # A new network: 10.255.255.1 is not this Mac's, so the bind fails and nothing lists or advertises it.
        plan['now'] = (['10.255.255.1'], ['100.101.102.103'])
        events.wait(lambda name, data: name == 'remote_state' and hub.bridge.addresses == plan['now'], 5)
        status = hub.status()
        assert status['listening'] == {'wifi': [], 'tailscale': []}
        assert hub.bridge.endpoints() == [] and hub.bridge.bonjour.advertised == []
        assert set(hub.bridge.servers) == {'127.0.0.1'}
    finally:
        hub.close()


def test_housekeeping_survives_a_failing_pass_and_still_ends_silent_connections(bridge, monkeypatch):
    hub, events, app = bridge
    monkeypatch.setattr(remote_bridge, 'NETWORK_SECONDS', 0.5)
    monkeypatch.setattr(remote_bridge, 'AUTH_SECONDS', 2.0)

    def broken():
        raise OSError('the network check failed')
    hub.bridge.address_plan = broken

    async def main():
        phone, connection = await paired(hub, events)
        await connection.close()
        silent = await phone.connection().open()  # never says hello
        assert await silent.until(lambda f: False, 8) is None, 'closed after the time to authenticate'

    run(main())


# ---- off is off; the cost of a phone ----------------------------------------------------------------------------------

def test_turning_remote_off_ends_every_connection_and_thread_and_on_again_keeps_the_pin(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        epoch = connection.welcome['epoch']
        await asyncio.to_thread(hub.remote_set, 'enabled', False)
        assert await connection.until(lambda f: False, 5) is None
        deadline = time.monotonic() + 3  # the call threads finish their queue's end marker and go
        while time.monotonic() < deadline and [t for t in threading.enumerate() if t.name.startswith('pebble-remote')]:
            await asyncio.sleep(0.05)
        assert not [t.name for t in threading.enumerate() if t.name.startswith('pebble-remote')]
        assert hub.bridge is None and hub.status()['running'] is False
        with pytest.raises((ConnectionError, OSError)):
            await phone.connection().open()
        await asyncio.to_thread(hub.remote_set, 'enabled', True)
        hub.remote_pair_start()
        phone.port = hub.bridge.port
        again = await phone.connection().open()  # the same key: the pin the phone holds still matches
        welcome = await again.hello(resume={'epoch': epoch, 'seq': 1})
        assert welcome['t'] == 'welcome' and welcome['epoch'] != epoch and welcome['resumed'] is False

    run(main())
    assert hub.status()['enabled'] is True and len(hub.status()['devices']) == 1


def test_the_cost_of_each_event_with_a_phone_connected(bridge):
    hub, events, app = bridge

    async def main():
        phone, connection = await paired(hub, events)
        data = {'chat_id': 'c' * 32, 'id': 'm' * 32, 'text': 'token'}
        count = 20000
        started = time.perf_counter()
        for _ in range(count):
            hub.tee('progress', data)
        per_event_us = (time.perf_counter() - started) / count * 1e6
        print('remote on, a phone connected: %.1f us per event in the emitting thread' % per_event_us)
        assert per_event_us < 200, per_event_us
        last = await connection.until(lambda f: f['t'] == 'evt' and f['event'] == 'progress' and
                                      f['seq'] >= connection.welcome['seq'] + count, 60)
        assert last is not None

    run(main())


# ---- a real Pebble behind the bridge ----------------------------------------------------------------------------------

def test_a_real_app_answers_the_phone_through_its_own_dispatch(tmp_path, monkeypatch):
    from pebble.app import App
    events_seen = []
    hub_ref = {}

    def emit(name, data):
        events_seen.append(name)
        if hub_ref.get('hub') is not None:
            hub_ref['hub'].tee(name, data)
    project = tmp_path / 'project'
    project.mkdir()
    app = App(tmp_path / 'home', tmp_path / 'resources', emit)
    try:
        known = app.new_chat(workspace=str(project))
        app.store.message(known['id'], 'user', 'hello')
        hub, events = start_hub(tmp_path, app, monkeypatch)
        hub_ref['hub'] = hub

        async def main():
            phone, connection = await paired(hub, events)
            state = (await connection.call('state', idem=False))['result']
            assert any(chat['id'] == known['id'] for chat in state['chats'])
            await connection.unlock()
            fresh = (await connection.call('new_chat', {'workspace': str(project.resolve())}))
            assert fresh['ok'] is True and fresh['result']['workspace'] == str(project.resolve()), \
                'a project a conversation uses needs no proof'
            tail = (await connection.call('remote.chat_tail', {'id': known['id'], 'limit': 1}, idem=False))['result']
            assert [m['content'] for m in tail['messages']] == ['hello'] and tail['has_older'] is False
            missing = await connection.call('get_chat', {'id': 'nope'}, idem=False)
            assert (missing['ok'], missing['error']) == (False, 'Conversation not found.')
            theme = await connection.call('setting', {'key': 'theme', 'value': 'dark'})
            assert theme['ok'] is True and app.store.setting('theme', None) == 'dark'
            settings = await connection.event(lambda f: f['t'] == 'evt' and f['event'] == 'settings')
            assert settings['data'] == {}
            rename = await connection.call('chat_rename', {'id': known['id'], 'title': 'From the phone'})
            assert rename['result'] == {'id': known['id'], 'title': 'From the phone'}

        run(main())
        hub.close()
        assert events.named('remote_state')[-1]['running'] is False
    finally:
        app.close()
