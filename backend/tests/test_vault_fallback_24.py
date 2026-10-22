"""Phase 24 (t24-auth): the vault helper's fallback never serves or writes a stale per-item record for an account the
item lists (V1-009), a helper that timed out is pinged again with backoff, one ChatGPT refresh costs at most three
helper processes, and deleting an account removes its kept per-item item (V1-017).

Fakes only, as in test_vault_consolidated_22: FakeKeychain for the per-item items (it counts reads of item data) and
FakeHelper for pebble_vault.c's protocol, which can time out on chosen operations. No Keychain call or helper process.
"""
import base64
import json
import logging
import plistlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import vault as vault_module
from pebble.chatgpt import ChatGPT, SecretRecords
from pebble.vault import (GET_ALL, LEGACY_SERVICE, MAX_MAP, PING, PUT_PREFIX, PUT_SUFFIX, SUPERSEDED, VAULT_ACCOUNT,
                          VAULT_SERVICE, HelperStore, Vault, VaultHelper, VaultUnavailable)

SECRET = 'sk-live-0123456789abcdef-SECRET'
DISCOVERY = {'token_endpoint': 'https://auth.openai.com/oauth/token'}
SCOPE = 'resource.invoke chatgpt.tokens.use.direct'


class FakeKeychain:
    """Per-item generic passwords by (service, account); reads of item data are what make macOS ask."""

    def __init__(self):
        self.items, self.calls, self.fail_remove = {}, [], set()

    def put(self, service, account, value):
        self.items[(service, account.encode())] = value.encode()

    def read(self, service, account):
        self.calls.append(('read', service, account))
        return self.items.get((service, account))

    def exists(self, service, account):
        self.calls.append(('exists', service, account))
        return (service, account) in self.items

    def write(self, service, account, raw):
        self.calls.append(('write', service, account))
        self.items[(service, account)] = raw

    def remove(self, service, account):
        self.calls.append(('remove', service, account))
        if (service, account) in self.fail_remove:
            raise ValueError('Keychain could not delete the key.')
        return self.items.pop((service, account), None) is not None

    def legacy(self):
        return sorted(account.decode() for service, account in self.items if service == LEGACY_SERVICE)

    def touched(self, kind):
        return [call for call in self.calls if call[0] == kind and call[1] == LEGACY_SERVICE]


class FakeHelper:
    """pebble_vault.c as the vault sees it; an operation named in `stall` times out (no reply)."""

    def __init__(self):
        self.item, self.requests, self.stall = None, [], set()

    def __call__(self, path, request, timeout):
        assert request.endswith(b'\n') and timeout == vault_module.HELPER_SECONDS
        body = request[:-1]
        op = 'ping' if body == PING else 'get_all' if body == GET_ALL else 'put_all' if body.startswith(PUT_PREFIX) \
            else 'other'
        self.requests.append(op)
        if op in self.stall:
            return None, b''
        if op == 'ping':
            return 0, b'{"ok":true,"version":1}\n'
        if op == 'get_all':
            if self.item is None:
                return 0, b'{"ok":true,"found":false}\n'
            return 0, b'{"ok":true,"found":true,"map_b64":"' + base64.b64encode(self.item) + b'"}\n'
        if op == 'put_all' and body.endswith(PUT_SUFFIX):
            raw = base64.b64decode(body[len(PUT_PREFIX):-len(PUT_SUFFIX)], validate=True)
            assert 0 < len(raw) <= MAX_MAP
            self.item = raw
            return 0, b'{"ok":true}\n'
        return 2, b'{"ok":false,"error":"bad_request"}\n'

    def secrets(self):
        return json.loads(self.item)['services'][LEGACY_SERVICE.decode()]['secrets']

    def migrated(self):
        return json.loads(self.item)['services'][LEGACY_SERVICE.decode()]['migrated']


@pytest.fixture(autouse=True)
def darwin(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')

    def refuse(*args, **kwargs):
        raise AssertionError('a test reached the real Keychain or helper')
    monkeypatch.setattr(vault_module, 'MacKeychain', refuse)
    monkeypatch.setattr(vault_module, 'run_helper', refuse)
    vault_module._OWNER.clear()
    vault_module._HELPER.clear()
    yield
    vault_module._OWNER.clear()
    vault_module._HELPER.clear()


@pytest.fixture
def rig():
    now = [1000.0]
    keychain, helper = FakeKeychain(), FakeHelper()
    store = HelperStore(VaultHelper(Path('/fake/pebble-vault'), run=helper), clock=lambda: now[0])
    vault = Vault(keychain=keychain, owner=True, helper=store)
    return SimpleNamespace(now=now, keychain=keychain, helper=helper, store=store, vault=vault,
                           records=SecretRecords(vault))


def oauth(refresh, expires=0):
    return {'access_token': 'AT-' + refresh, 'refresh_token': refresh, 'client_id': 'oaiapp_fixture',
            'expires_at': expires, 'scopes': SCOPE.split(), 'padding': 'p' * 3000}


def old_sign_in(keychain, refresh='RT1'):
    """A ChatGPT sign-in as 0.5.0 left it: chunked per-item items under the providers service."""
    data = json.dumps(oauth(refresh))
    chunks = [data[i:i + 1800] for i in range(0, len(data), 1800)]
    keychain.put(LEGACY_SERVICE, 'oauth:p1:manifest', json.dumps({'generation': 'g1', 'count': len(chunks)}))
    for index, chunk in enumerate(chunks):
        keychain.put(LEGACY_SERVICE, 'oauth:p1:g1:%d' % index, chunk)
    return ['oauth:p1:manifest'] + ['oauth:p1:g1:%d' % i for i in range(len(chunks))]


# -- V1-009: no stale per-item record for an account the item lists ------------------------------------------------------

def test_a_helper_timeout_after_migration_never_serves_the_kept_per_item_record(rig):
    old_sign_in(rig.keychain)
    assert rig.records.get('oauth:p1')['refresh_token'] == 'RT1'  # migrated: one read of each old item, once
    rig.records.set('oauth:p1', oauth('RT2'))  # a refresh rotated RT1 into RT2, in the helper's item only
    reads = len(rig.keychain.touched('read'))
    rig.now[0] += 31  # the 30 s cache is over
    rig.helper.stall = {'get_all'}
    with pytest.raises(VaultUnavailable) as caught:
        rig.records.get('oauth:p1')
    assert len(rig.keychain.touched('read')) == reads  # the kept per-item manifest and chunks were not read
    assert 'RT1' not in str(caught.value) and 'oauth' not in str(caught.value)
    with pytest.raises(VaultUnavailable):
        rig.vault.get('oauth:p1:manifest')  # resting: refused at once, no helper process
    assert rig.helper.requests[-1] == 'get_all' and rig.helper.requests.count('get_all') >= 1
    rig.helper.stall = set()
    rig.now[0] += vault_module.REST_FIRST
    assert rig.records.get('oauth:p1')['refresh_token'] == 'RT2'  # pinged again, then the item answers
    assert len(rig.keychain.touched('read')) == reads


def test_a_write_that_times_out_never_lands_per_item(rig):
    old_sign_in(rig.keychain)
    rig.records.get('oauth:p1')
    rig.helper.stall = {'put_all'}
    with pytest.raises(VaultUnavailable):
        rig.records.set('oauth:p1', oauth('RT3'))
    assert rig.keychain.touched('write') == []  # RT3 did not become a per-item item nobody reconciles
    rig.helper.stall = set()
    rig.now[0] += vault_module.REST_FIRST
    rig.records.set('oauth:p1', oauth('RT3'))
    assert rig.records.get('oauth:p1')['refresh_token'] == 'RT3' and rig.keychain.touched('write') == []


def test_an_unlisted_account_still_falls_back_and_an_unread_existing_item_refuses(rig):
    rig.vault.set('profile-1', SECRET)
    rig.helper.stall = {'put_all'}
    with pytest.raises(VaultUnavailable):
        rig.vault.set('profile-1', SECRET + '-2')  # listed: refused
    rig.vault.set('profile-2', 'new-key')  # never listed: per-item, as in Phase 22
    assert rig.keychain.legacy() == ['profile-2'] and rig.vault.get('profile-2') == 'new-key'
    # A process that never read the item while it exists cannot know what it lists: nothing per-item is used.
    keychain, helper = FakeKeychain(), FakeHelper()
    keychain.items[(VAULT_SERVICE, VAULT_ACCOUNT)] = b'the item'
    keychain.put(LEGACY_SERVICE, 'profile-1', 'stale')
    helper.stall = {'get_all'}
    fresh = Vault(keychain=keychain, owner=True, helper=HelperStore(VaultHelper(Path('/fake'), run=helper)))
    with pytest.raises(VaultUnavailable):
        fresh.get('profile-1')
    assert keychain.touched('read') == []


def test_a_timed_out_helper_is_pinged_again_with_backoff(rig, caplog):
    rig.vault.set('profile-1', SECRET)
    rig.now[0] += 31
    rig.helper.stall = {'get_all', 'ping'}
    with caplog.at_level(logging.WARNING, logger='pebble.vault'):
        with pytest.raises(VaultUnavailable):
            rig.vault.get('profile-1')
    assert 'Vault helper stopped answering (timeout); asking it again in 2 s.' in caplog.text
    pings = []
    for step in (1.9, 0.2, 3.9, 0.2, 7.9, 0.2):  # rests of 2 s, then 4 s, then 8 s
        rig.now[0] += step
        before = len(rig.helper.requests)
        with pytest.raises(VaultUnavailable):
            rig.vault.get('profile-1')
        pings.append(rig.helper.requests[before:])
    assert pings == [[], ['ping'], [], ['ping'], [], ['ping']]
    rig.helper.stall = set()
    rig.now[0] += 16
    assert rig.vault.get('profile-1') == SECRET and rig.helper.requests[-2:] == ['ping', 'get_all']
    assert SECRET not in caplog.text and rig.vault._helper is rig.store  # never dropped for the session
    for _ in range(8):
        rig.store.rest()
    assert rig.store.resting - rig.now[0] == vault_module.REST_MAX


def bundle(tmp_path):
    app = tmp_path / 'Pebble.app'
    (app / 'Contents/Resources/resources/backend').mkdir(parents=True)
    (app / 'Contents/Resources/resources/helpers').mkdir()
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.pebble.local'}))
    executable = app / 'Contents/Resources/resources/backend/pebble-backend'
    executable.write_bytes(b'\xcf\xfa\xed\xfe')
    (app / 'Contents/Resources/resources/helpers/pebble-vault').write_bytes(b'\xcf\xfa\xed\xfe helper')
    return executable


def test_the_release_backend_keeps_a_helper_whose_first_ping_timed_out(tmp_path, monkeypatch):
    helper = FakeHelper()
    helper.stall = {'ping'}
    monkeypatch.setattr(vault_module, 'run_helper', helper)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(bundle(tmp_path)))
    keychain = FakeKeychain()
    assert Vault(keychain=keychain).get('profile-1') == ''  # no item exists yet: per-item is still the truth
    store = vault_module._HELPER['store']
    assert isinstance(store, HelperStore) and store.resting is not None  # resting, not dropped for the session
    keychain.items[(VAULT_SERVICE, VAULT_ACCOUNT)] = b'the item'
    with pytest.raises(VaultUnavailable):
        Vault(keychain=keychain).get('profile-1')  # now the item exists and was never read: refused
    helper.stall = set()
    store.resting = 0.0  # the rest is over (the auto store keeps time.monotonic)
    Vault(keychain=keychain).set('profile-1', 'key')
    assert helper.requests[-4:] == ['ping', 'get_all', 'put_all', 'get_all'] and helper.secrets() == {'profile-1': 'key'}


# -- V1-009: one refresh costs at most three helper processes ------------------------------------------------------------

def test_one_refresh_uses_at_most_three_helper_processes(rig):
    rig.records.set('oauth:p1', oauth('RT1'))
    auth = ChatGPT(SimpleNamespace(store=None, vault=rig.vault))
    auth.config = lambda: DISCOVERY
    presented = []

    def refresh(url, form=None):
        presented.append(form['refresh_token'])
        return {'access_token': 'AT%d' % (len(presented) + 1), 'refresh_token': 'RT%d' % (len(presented) + 1),
                'token_type': 'Bearer', 'expires_in': 3600, 'scope': SCOPE}
    auth.http_json = refresh
    rig.now[0] += 31  # a cold cache: the record is read from the helper first
    before = len(rig.helper.requests)
    assert auth.access('p1') == 'AT2'
    assert rig.helper.requests[before:] == ['get_all', 'put_all', 'get_all']  # Phase 23: 21 processes
    before = len(rig.helper.requests)
    assert auth.access('p1', rejected='AT2') == 'AT3'  # a warm cache: the write and its read-back
    assert rig.helper.requests[before:] == ['put_all', 'get_all'] and presented == ['RT1', 'RT2']
    secrets = rig.helper.secrets()
    manifest = json.loads(secrets['oauth:p1:manifest'])
    assert sorted(secrets) == sorted(['oauth:p1:manifest'] + ['oauth:p1:%s:%d' % (manifest['generation'], i)
                                                              for i in range(manifest['count'])])  # one generation
    assert rig.records.get('oauth:p1')['refresh_token'] == 'RT3'


def test_a_renewed_sign_in_the_helper_could_not_save_is_kept_and_saved_after_the_rest(rig):
    rig.records.set('oauth:p1', oauth('RT1'))
    auth = ChatGPT(SimpleNamespace(store=None, vault=rig.vault))
    auth.config = lambda: DISCOVERY
    presented = []

    def refresh(url, form=None):
        presented.append(form['refresh_token'])
        return {'access_token': 'AT2', 'refresh_token': 'RT2', 'token_type': 'Bearer', 'expires_in': 3600,
                'scope': SCOPE}
    auth.http_json = refresh
    rig.helper.stall = {'put_all'}
    assert auth.access('p1') == 'AT2' and 'p1' in auth.unsaved  # RT1 is consumed: RT2 lives in memory
    assert auth.access('p1') == 'AT2' and presented == ['RT1']  # resting: no helper call, no second refresh
    rig.helper.stall = set()
    rig.now[0] += vault_module.REST_FIRST
    assert auth.access('p1') == 'AT2' and auth.unsaved == {} and presented == ['RT1']
    assert rig.records.get('oauth:p1')['refresh_token'] == 'RT2' and rig.keychain.touched('write') == []


# -- V1-017: delete removes the kept per-item item -----------------------------------------------------------------------

def test_delete_removes_the_kept_per_item_item(rig, caplog):
    rig.keychain.put(LEGACY_SERVICE, 'profile-1', SECRET)
    rig.keychain.put(LEGACY_SERVICE, 'profile-2', SECRET + '-never-read')
    assert rig.vault.get('profile-1') == SECRET  # migrated; the old item is kept for a rollback
    assert rig.keychain.legacy() == ['profile-1', 'profile-2']
    reads = len(rig.keychain.touched('read'))
    rig.vault.delete('profile-1')
    rig.vault.delete('profile-2')  # removed without being read
    assert rig.keychain.legacy() == [] and len(rig.keychain.touched('read')) == reads
    assert rig.vault.get('profile-1') == '' and rig.vault.get('profile-2') == ''
    assert rig.helper.migrated()['profile-2'] == SUPERSEDED and 'profile-1' not in rig.helper.secrets()
    # A sign-out removes the kept chunks and manifest too, in one item update.
    names = old_sign_in(rig.keychain)
    rig.records.get('oauth:p1')
    before = len(rig.helper.requests)
    rig.records.delete('oauth:p1')
    assert rig.helper.requests[before:] == ['put_all', 'get_all'] and rig.keychain.legacy() == []
    assert not set(names) & set(rig.helper.secrets())
    # A kept item that cannot be removed keeps its marker (never read) and names nothing in the log.
    rig.keychain.put(LEGACY_SERVICE, 'profile-3', SECRET)
    rig.vault.get('profile-3')
    rig.keychain.fail_remove.add((LEGACY_SERVICE, b'profile-3'))
    with caplog.at_level(logging.WARNING, logger='pebble.vault'):
        rig.vault.delete('profile-3')
    assert 'could not be deleted' in caplog.text and 'profile-3' not in caplog.text and SECRET not in caplog.text
    assert rig.keychain.legacy() == ['profile-3'] and 'profile-3' in rig.helper.migrated()
    assert rig.vault.get('profile-3') == ''
    rig.keychain.fail_remove.clear()
    assert rig.vault.cleanup_legacy()['com.pebble.local.providers']['removed'] == 1
    assert rig.keychain.legacy() == []


def test_set_many_writes_per_item_when_the_helper_is_not_in_use():
    keychain = FakeKeychain()
    vault = Vault(keychain=keychain, owner=False)
    records = SecretRecords(vault)
    assert vault.set_many({'a': 'x'}) is False and keychain.items == {}  # the caller writes item by item
    records.set('oauth:p9', oauth('RT1'))
    assert records.get('oauth:p9')['refresh_token'] == 'RT1'
    records.delete('oauth:p9')
    assert keychain.items == {}
    with pytest.raises(ValueError):
        vault.set_many({'a': 'line\nbreak'})
