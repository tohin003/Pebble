"""Credential broker: grant review, per-request checks, revocation, expiry, zeroization and secret hygiene.

No network and no vault: fetch is an in-test callback. Every string the broker
produces is scanned for the secret. The end-to-end injection, reflection and
confined-client (F2) cases live in test_egress.py.
"""

import copy
import pickle
import secrets
import threading
import time
import traceback

import pytest

from pebble import credential_broker
from pebble.credential_broker import CredentialBroker, CredentialRefused
from pebble.egress import ROUTE_METHODS
from pebble.policy import PolicyError

SECRET = 'w3-unit-secret-' + secrets.token_hex(16)
ORIGIN = 'https://api.pebble.test'
URL = 'https://api.pebble.test:443/v1/repos'


def make(value=SECRET, calls=None):
    return CredentialBroker(lambda ref: (calls.append(ref) if calls is not None else None) or value)


def later(seconds=60):
    return time.time() + seconds


def scan(*texts):
    blob = '\n'.join(texts)
    assert SECRET not in blob and SECRET.encode().hex() not in blob


def test_header_for_the_reviewed_origin_method_and_prefix():
    calls = []
    broker = make(calls=calls)
    grant = broker.grant('connector:github', ORIGIN, ['POST', 'GET'], '/v1/repos', later())
    assert grant.startswith('cg_') and len(grant) == 27 and calls == ['connector:github']
    name, value = broker.header(grant, 'GET', URL + '/acme?page=2')
    assert name == 'Authorization' and type(value) is bytearray and value == b'Bearer ' + SECRET.encode()
    assert broker.header(grant, 'POST', ORIGIN + '/v1/repos')[1] == value  # The prefix itself, default port.
    other = broker.grant('vault:key', 'https://api.pebble.test:8443', ['GET'], '/', later(), header='X-Api-Key', scheme=None)
    assert broker.header(other, 'GET', 'https://api.pebble.test:8443/any/path') == ('X-Api-Key', bytearray(SECRET.encode()))
    assert calls == ['connector:github', 'vault:key']  # Fetched once per grant, never per request.
    public = broker.describe(grant)
    assert public == {'grant': grant, 'ref': 'connector:github', 'destination': 'https://api.pebble.test:443',
                      'methods': ['GET', 'POST'], 'path_prefix': '/v1/repos', 'expires': public['expires'],
                      'header': 'Authorization', 'scheme': 'Bearer', 'secret_hash': public['secret_hash'],
                      'secret_length': len(SECRET)}
    assert len(public['secret_hash']) == 16 and broker.state(grant) == 'active'
    assert broker.stats() == {'live': 2, 'granted': 2, 'issued': 3, 'refused': 0, 'revoked': 0, 'expired': 0,
                              'closed': False}
    assert [r['event'] for r in broker.log()] == ['grant', 'header', 'header', 'grant', 'header']
    scan(repr(broker), str(broker.log()), str(public), str(broker.stats()))


@pytest.mark.parametrize('method, url, reason', [
    ('DELETE', URL + '/x', 'method_refused'), ('get', URL, 'method_refused'), ('CONNECT', URL, 'method_refused'),
    ('GET', 'https://other.pebble.test/v1/repos', 'destination_refused'),
    ('GET', 'https://api.pebble.test:8443/v1/repos', 'destination_refused'),
    ('GET', 'http://api.pebble.test/v1/repos', 'destination_refused'),
    ('GET', 'https://user@api.pebble.test/v1/repos', 'destination_refused'),
    ('GET', 'https://api.pebble.test:443@evil.pebble.test/v1/repos', 'destination_refused'),
    ('GET', 'https://API.pebble.test/v1/repos', 'destination_refused'),
    ('GET', 'https://api.pebble.test/v1/repos#x', 'destination_refused'),
    ('GET', 'https://api.pebble.test', 'destination_refused'), ('GET', None, 'destination_refused'),
    ('GET', 7, 'destination_refused'), ('GET', 'https://api.pebble.test/v1/repos/ü', 'destination_refused'),
    ('GET', 'https://api.pebble.test/v1/reposX', 'prefix_refused'), ('GET', 'https://api.pebble.test/v1', 'prefix_refused'),
    ('GET', 'https://api.pebble.test/v1/repos/../admin', 'path_refused'),
    ('GET', 'https://api.pebble.test/v1/repos/%2e%2e/admin', 'path_refused'),
    ('GET', 'https://api.pebble.test/v1/repos/%2F..', 'path_refused'),
    ('GET', 'https://api.pebble.test/v1/repos//x', 'path_refused'),
    ('GET', 'https://api.pebble.test/v1/repos/..;/admin', 'path_refused'),
    ('GET', 'https://api.pebble.test/v1/repos\\..\\admin', 'path_refused'),
])
def test_header_refusals_name_their_reason(method, url, reason):
    broker = make()
    grant = broker.grant('vault:api', ORIGIN, ['GET', 'POST'], '/v1/repos', later())
    with pytest.raises(CredentialRefused) as info:
        broker.header(grant, method, url)
    assert info.value.reason == reason and isinstance(info.value, PolicyError)
    assert broker.stats()['refused'] == 1 and broker.stats()['issued'] == 0
    record = broker.log()[-1]
    assert (record['event'], record['grant'], record['reason']) == ('refused', grant, reason)
    assert record['method'] == (method if method in ROUTE_METHODS else None)
    scan(str(info.value), repr(info.value), str(broker.log()))
    assert broker.state(grant) == 'active'  # A refused request does not end the grant.


@pytest.mark.parametrize('field, value', [
    ('ref', ''), ('ref', ' x'), ('ref', 'a b'), ('ref', 'x' * 129), ('ref', 7), ('ref', None), ('ref', 'ref\n'),
    ('destination', 'http://api.pebble.test'), ('destination', 'https://api.pebble.test/'),
    ('destination', 'https://api.pebble.test/v1'), ('destination', 'https://user@api.pebble.test'),
    ('destination', 'https://127.0.0.1'), ('destination', 'https://[::1]'), ('destination', 'https://*.pebble.test'),
    ('destination', 'https://API.pebble.test'), ('destination', 'https://xn--bcher-kva.example'),
    ('destination', 'https://api.pebble.test:0'), ('destination', 'https://api.pebble.test:65536'),
    ('destination', 'api.pebble.test'), ('destination', None),
    ('methods', []), ('methods', ['CONNECT']), ('methods', ['TRACE']), ('methods', ['get']), ('methods', ['GET', 'GET']),
    ('methods', 'GET'), ('methods', [['GET']]), ('methods', [*ROUTE_METHODS, 'GET']),
    ('path_prefix', ''), ('path_prefix', 'v1'), ('path_prefix', '/v1/../x'), ('path_prefix', '/v1/%2e%2e'),
    ('path_prefix', '/v1?x=1'), ('path_prefix', '/v1#x'), ('path_prefix', '/v1//x'), ('path_prefix', '/' + 'a' * 1024),
    ('path_prefix', None),
    ('expires', -1), ('expires', 3601), ('expires', True), ('expires', '60'), ('expires', float('nan')),
    ('expires', float('inf')), ('expires', None),
    ('header', 'Host'), ('header', 'content-length'), ('header', 'Transfer-Encoding'), ('header', 'Proxy-Authorization'),
    ('header', 'bad name'), ('header', ''), ('header', 'X_Api'), ('header', 7),
    ('scheme', ''), ('scheme', 'Bear er'), ('scheme', 7), ('scheme', 'x' * 17),
])
def test_grant_review_refuses_bad_shapes_before_fetching(field, value):
    calls = []
    broker = make(calls=calls)
    args = {'ref': 'vault:api', 'destination': ORIGIN, 'methods': ['GET'], 'path_prefix': '/v1', 'expires': later()}
    options = {}
    if field in ('header', 'scheme'):
        options[field] = value
    elif field == 'expires' and type(value) is int:
        args['expires'] = time.time() + value  # One second ago, or beyond one hour.
    else:
        args[field] = value
    with pytest.raises(PolicyError):
        broker.grant(*args.values(), **options)
    assert calls == [] and broker.stats()['live'] == 0


# Phase 32: fixed IDs, because SECRET is random per process and parallel workers must collect the same test IDs.
@pytest.mark.parametrize('value', ['has space' + SECRET, SECRET + '\n', '', 'x' * 8193, 'é' + SECRET, 42, None,
                                   [SECRET], SECRET.encode() + b'\x00', b'\r\n' + SECRET.encode()],
                         ids=['space', 'newline', 'empty', 'too-long', 'non-ascii', 'int', 'none', 'list', 'nul-bytes',
                              'crlf-bytes'])
def test_unusable_vault_values_are_refused_without_echo(value):
    broker = make(value)
    with pytest.raises(PolicyError) as info:
        broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later())
    scan(str(info.value), repr(info.value), ''.join(traceback.format_exception(info.value)), str(broker.log()))
    assert broker.stats()['live'] == 0


def test_fetch_failures_never_chain_vault_text():
    def failing(ref):
        raise RuntimeError(f'vault said {SECRET}')
    broker = CredentialBroker(failing)
    with pytest.raises(CredentialRefused) as info:
        broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later())
    assert info.value.reason == 'fetch_failed'
    assert info.value.__context__ is None and info.value.__cause__ is None
    scan(str(info.value), ''.join(traceback.format_exception(info.value)), str(broker.log()), repr(broker))
    with pytest.raises(PolicyError):
        CredentialBroker('not callable')


def test_bytes_and_bytearray_values_are_copied_never_mutated():
    owned = bytearray(SECRET.encode())
    broker = make(owned)
    grant = broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later(), scheme='token')
    assert broker.header(grant, 'GET', ORIGIN + '/v1/x')[1] == b'token ' + SECRET.encode()
    broker.close()
    assert owned == SECRET.encode()  # The caller's buffer is the caller's to clear.
    other = make(SECRET.encode())
    assert other.header(other.grant('vault:api', ORIGIN, ['GET'], '/v1', later()), 'GET', ORIGIN + '/v1')[0] == 'Authorization'


def test_revocation_applies_to_the_next_request_and_zeroizes():
    broker = make()
    grant = broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later())
    held = broker._grants[grant].secret
    issued = broker.header(grant, 'GET', ORIGIN + '/v1/x')[1]
    assert held == SECRET.encode()
    assert broker.revoke(grant) is True and broker.revoke(grant) is False
    assert held == bytearray(len(SECRET))
    assert issued == b'Bearer ' + SECRET.encode()  # An issued copy belongs to the egress caller, which zeroizes it.
    with pytest.raises(CredentialRefused) as info:
        broker.header(grant, 'GET', ORIGIN + '/v1/x')
    assert info.value.reason == 'revoked' and broker.state(grant) == 'revoked'
    with pytest.raises(CredentialRefused) as info:
        broker.describe(grant)
    assert info.value.reason == 'revoked'
    assert broker.state('cg_unknown') == 'unknown' and broker.revoke(['x']) is False and broker.state(None) == 'unknown'
    with pytest.raises(CredentialRefused) as info:
        broker.header('cg_unknown', 'GET', ORIGIN + '/v1/x')
    assert info.value.reason == 'unknown_grant' and broker.log()[-1]['grant'] is None
    assert [r['event'] for r in broker.log()] == ['grant', 'header', 'revoke', 'refused', 'refused']
    assert broker.log()[3]['grant'] == grant and broker.stats()['revoked'] == 1


def test_expiry_holds_by_either_clock(monkeypatch):
    broker = make()
    grant = broker.grant('vault:api', ORIGIN, ['GET'], '/v1', time.time() + 0.3)
    held = broker._grants[grant].secret
    broker.header(grant, 'GET', ORIGIN + '/v1')
    time.sleep(0.4)
    with pytest.raises(CredentialRefused) as info:
        broker.header(grant, 'GET', ORIGIN + '/v1')
    assert info.value.reason == 'expired' and broker.state(grant) == 'expired' and held == bytearray(len(SECRET))
    assert 'expire' in [r['event'] for r in broker.log()] and broker.stats()['expired'] == 1
    # A wall clock stepped back cannot extend a grant: the monotonic deadline still ends it.
    real = credential_broker._wall
    grant = broker.grant('vault:api', ORIGIN, ['GET'], '/v1', real() + 0.3)
    monkeypatch.setattr(credential_broker, '_wall', lambda: real() - 3600)
    time.sleep(0.4)
    assert broker.state(grant) == 'expired'
    # A wall clock stepped forward ends it as well.
    monkeypatch.setattr(credential_broker, '_wall', real)
    grant = broker.grant('vault:api', ORIGIN, ['GET'], '/v1', real() + 60)
    monkeypatch.setattr(credential_broker, '_wall', lambda: real() + 120)
    with pytest.raises(CredentialRefused) as info:
        broker.header(grant, 'GET', ORIGIN + '/v1')
    assert info.value.reason == 'expired'


def test_close_zeroizes_every_grant_and_refuses_later_use():
    broker = make()
    grants = [broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later()) for _ in range(3)]
    held, key = [broker._grants[g].secret for g in grants], broker._key
    assert broker.close() is None and broker.close() is None
    assert all(h == bytearray(len(SECRET)) for h in held) and key == bytearray(32)
    for grant in grants:
        with pytest.raises(CredentialRefused) as info:
            broker.header(grant, 'GET', ORIGIN + '/v1')
        assert info.value.reason == 'closed' and broker.state(grant) == 'closed'
    for call in (lambda: broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later()), lambda: broker.describe(grants[0])):
        with pytest.raises(CredentialRefused) as info:
            call()
        assert info.value.reason == 'closed'
    assert repr(broker) == '<CredentialBroker live=0 closed=True>'
    assert [r['reason'] for r in broker.log() if r['event'] == 'close'] == ['zeroized_3']
    assert broker.log()[-1]['event'] == 'refused' and broker.log()[-1]['reason'] == 'closed'
    assert broker.stats()['closed'] is True and broker.stats()['live'] == 0


def test_no_secret_in_repr_logs_stats_errors_or_serialization():
    broker, twin = make(), make()
    grant = broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later())
    again = broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later())
    elsewhere = twin.grant('vault:api', ORIGIN, ['GET'], '/v1', later())
    messages = []
    for method, url in (('PUT', ORIGIN + '/v1'), ('GET', 'https://evil.pebble.test/v1'), ('GET', ORIGIN + '/v2')):
        with pytest.raises(CredentialRefused) as info:
            broker.header(grant, method, url)
        messages.append(''.join(traceback.format_exception(info.value)))
    for target in (broker, broker._grants[grant]):
        for call in (pickle.dumps, copy.copy, copy.deepcopy):
            with pytest.raises(TypeError) as info:
                call(target)
            messages.append(str(info.value))
    tags = [broker.describe(grant)['secret_hash'], broker.describe(again)['secret_hash'], twin.describe(elsewhere)['secret_hash']]
    assert tags[0] == tags[1] != tags[2]  # Keyed per broker: comparable within one, not guessable offline.
    assert all(r['secret_length'] in (len(SECRET), None) for r in broker.log())
    scan(repr(broker), repr(broker._grants[grant]), repr(vars(broker)), str(broker.log()), str(broker.stats()),
         str(broker.describe(grant)), *messages)
    broker.close()
    scan(repr(broker), str(broker.log()), repr(vars(broker)))


def test_live_grant_capacity_and_bounded_log():
    calls = []
    broker = make(calls=calls)
    grants = [broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later()) for _ in range(16)]
    with pytest.raises(PolicyError):
        broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later())
    assert len(calls) == 16  # The refused seventeenth grant never read the vault.
    broker.revoke(grants[0])
    broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later())
    for _ in range(300):
        broker.header(grants[1], 'GET', ORIGIN + '/v1')
    log = broker.log()
    assert len(log) == 256 and log[-1]['event'] == 'header'
    log[-1]['grant'] = 'mutated'
    assert broker.log()[-1]['grant'] == grants[1]


def test_concurrent_headers_and_revocation():
    broker = make()
    grant = broker.grant('vault:api', ORIGIN, ['GET'], '/v1', later())
    issued, refused, started = [], [], threading.Event()

    def worker():
        started.wait()
        for _ in range(200):
            try:
                issued.append(bytes(broker.header(grant, 'GET', ORIGIN + '/v1/x')[1]))
            except CredentialRefused as exc:
                refused.append(exc.reason)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    started.set()
    time.sleep(0.005)
    assert broker.revoke(grant) is True
    for thread in threads:
        thread.join(10)
    assert set(refused) <= {'revoked'} and set(issued) <= {b'Bearer ' + SECRET.encode()}
    assert len(issued) + len(refused) == 1600
    stats = broker.stats()
    assert stats['issued'] == len(issued) and stats['refused'] == len(refused)
    with pytest.raises(CredentialRefused):
        broker.header(grant, 'GET', ORIGIN + '/v1/x')
