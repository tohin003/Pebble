"""Phase 22 (k22-identity): the vault through its pinned helper, backend/pebble/vault.py and pebble_vault.c.

Every case uses fakes: FakeKeychain stands in for the per-item Keychain items (it counts reads of an item's data by a
code identity other than the one that wrote it, which is what makes macOS ask), and FakeHelperProcess for the helper
process and its one item, speaking pebble_vault.c's exact protocol. No real Keychain call or helper process runs.
"""
import base64
import hashlib
import json
import logging
import plistlib
import re
import sys
import threading
from pathlib import Path

import pytest

from pebble import vault as vault_module
from pebble.vault import (FORMAT, GET_ALL, LEGACY_SERVICE, MAX_MAP, PING, PUT_PREFIX, PUT_SUFFIX, SUPERSEDED,
                          HelperError, HelperStore, HelperUnavailable, Vault, VaultHelper)

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / 'src-tauri/helpers/pebble-vault/pebble_vault.c'
SECRET = 'sk-live-0123456789abcdef-SECRET'
CONNECTORS, PLANS = b'com.pebble.local.connectors', b'com.pebble.local.plan-credentials'


class FakeKeychain:
    """Per-item generic passwords by (service, account) and the identity that wrote each."""

    def __init__(self, identity='pebble-0.6.0'):
        self.items, self.writers, self.calls, self.prompts = {}, {}, [], []
        self.identity, self.fail_remove = identity, set()

    def put(self, service, account, value, identity='pebble-0.5.0'):
        key = (service, account.encode())
        self.items[key], self.writers[key] = value.encode(), identity

    def read(self, service, account):
        self.calls.append(('read', service, account))
        key = (service, account)
        if key in self.items and self.writers[key] != self.identity:
            self.prompts.append(key)  # macOS asks once; "Always Allow" adds this identity
            self.writers[key] = self.identity
        return self.items.get(key)

    def exists(self, service, account):
        self.calls.append(('exists', service, account))
        return (service, account) in self.items

    def write(self, service, account, raw):
        self.calls.append(('write', service, account))
        self.items[(service, account)], self.writers[(service, account)] = raw, self.identity

    def remove(self, service, account):
        self.calls.append(('remove', service, account))
        if (service, account) in self.fail_remove:
            raise ValueError('Keychain could not delete the key.')
        return self.items.pop((service, account), None) is not None

    def legacy(self, service=LEGACY_SERVICE):
        return sorted(account.decode() for svc, account in self.items if svc == service)

    def legacy_reads(self):
        return [call for call in self.calls if call[0] == 'read']


class FakeHelperProcess:
    """pebble_vault.c as the vault sees it: byte-exact requests, its one item, and failures on demand."""

    def __init__(self):
        self.item, self.requests = None, []
        self.refuse = self.crash = self.corrupt = False
        self.deny = None

    def __call__(self, path, request, timeout):
        self.requests.append(request)
        assert request.endswith(b'\n') and request.count(b'\n') == 1 and timeout == vault_module.HELPER_SECONDS
        body = request[:-1]
        if self.refuse:
            return 3, b'{"ok":false,"error":"caller_refused"}\n'
        if self.crash:
            return -11, b''
        if body == PING:
            return 0, b'{"ok":true,"version":1}\n'
        if body == GET_ALL:
            if self.item is None:
                return 0, b'{"ok":true,"found":false}\n'
            return 0, b'{"ok":true,"found":true,"map_b64":"' + base64.b64encode(self.item) + b'"}\n'
        if body.startswith(PUT_PREFIX) and body.endswith(PUT_SUFFIX):
            if self.deny is not None:
                return 1, b'{"ok":false,"error":"keychain_write_failed","status":%d}\n' % self.deny
            raw = base64.b64decode(body[len(PUT_PREFIX):-len(PUT_SUFFIX)], validate=True)
            assert 0 < len(raw) <= MAX_MAP
            self.item = raw + (b' ' if self.corrupt else b'')
            return 0, b'{"ok":true}\n'
        return 2, b'{"ok":false,"error":"bad_request"}\n'

    def store(self):
        return None if self.item is None else json.loads(self.item)


@pytest.fixture(autouse=True)
def darwin(monkeypatch):
    """The macOS path, with the real Security framework and helper process never used."""
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


def store_for(process):
    return HelperStore(VaultHelper(Path('/fake/pebble-vault'), run=process))


def owner(keychain, process, cls=Vault):
    return cls(keychain=keychain, owner=True, helper=store_for(process))


def sign_in(keychain, identity='pebble-0.5.0'):
    """A ChatGPT sign-in as chatgpt.SecretRecords stores it, plus one API key, all per-item (Phase 21)."""
    record = 'x' * 4000
    chunks = [record[i:i + 1800] for i in range(0, len(record), 1800)]
    keychain.put(LEGACY_SERVICE, 'oauth:p1:manifest', json.dumps({'generation': 'g1', 'count': len(chunks)}), identity)
    for index, chunk in enumerate(chunks):
        keychain.put(LEGACY_SERVICE, 'oauth:p1:g1:%d' % index, chunk, identity)
    keychain.put(LEGACY_SERVICE, 'profile-1', SECRET, identity)
    return {account.decode(): value.decode() for (service, account), value in keychain.items.items()}


def section(process, service=LEGACY_SERVICE):
    return process.store()['services'][service.decode()]


# ---- The protocol --------------------------------------------------------------------------------------------


def c_literal(name):
    match = re.search(r'static const char %s\[\] = "((?:[^"\\]|\\.)*)";' % name, SOURCE.read_text())
    return match.group(1).replace('\\"', '"').encode()


def test_requests_are_byte_for_byte_the_helpers_protocol():
    assert (c_literal('PING'), c_literal('GET_ALL'), c_literal('PUT_PREFIX'), c_literal('PUT_SUFFIX')) == (
        PING, GET_ALL, PUT_PREFIX, PUT_SUFFIX)
    assert re.search(r'#define MAX_MAP \(1024 \* 1024\)', SOURCE.read_text()) and MAX_MAP == 1024 * 1024
    process = FakeHelperProcess()
    helper = VaultHelper(Path('/fake/pebble-vault'), run=process)
    helper.ping()
    assert helper.get_all() is None
    helper.put_all(b'{"a":1}')
    assert helper.get_all() == b'{"a":1}'
    assert process.requests == [PING + b'\n', GET_ALL + b'\n',
                                PUT_PREFIX + base64.b64encode(b'{"a":1}') + PUT_SUFFIX + b'\n', GET_ALL + b'\n']
    for raw in (b'', b'x' * (MAX_MAP + 1)):
        with pytest.raises(ValueError):
            helper.put_all(raw)
    assert len(process.requests) == 4


@pytest.mark.parametrize('reply,error,code', [
    ((3, b'{"ok":false,"error":"caller_refused"}\n'), HelperUnavailable, 'caller_refused'),
    ((None, b''), HelperUnavailable, 'timeout'),
    ((-11, b''), HelperUnavailable, 'bad_reply'),
    ((0, b'not json'), HelperUnavailable, 'bad_reply'),
    ((0, b'{"ok":true,"version":2}\n'), HelperUnavailable, 'version'),
    ((2, b'{"ok":false,"error":"bad_request"}\n'), HelperUnavailable, 'bad_request'),
    ((1, b'{"ok":false,"error":"internal"}\n'), HelperUnavailable, 'internal'),
    ((0, b'x' * (vault_module.MAX_REPLY + 1)), HelperUnavailable, 'reply_too_large'),
    ((1, b'{"ok":false,"error":"keychain_read_failed","status":-25293}\n'), HelperError, 'keychain_read_failed'),
])
def test_helper_replies_map_to_unavailable_or_keychain_errors(reply, error, code):
    helper = VaultHelper(Path('/fake/pebble-vault'), run=lambda path, request, timeout: reply)
    with pytest.raises(error) as raised:
        helper.ping()
    assert raised.value.code == code
    assert isinstance(raised.value, HelperUnavailable) == (error is HelperUnavailable)


# ---- Migration: once, and the old items are kept ---------------------------------------------------------------


def test_old_items_are_read_once_copied_and_kept_and_never_read_again():
    keychain, process = FakeKeychain(), FakeHelperProcess()
    values = sign_in(keychain)
    vault = owner(keychain, process)
    for account, value in values.items():
        assert vault.get(account) == value
    # One prompt per old item, once (each was written by 0.5.0 and read by this build's backend).
    assert sorted(account.decode() for _, account in keychain.prompts) == sorted(values)
    assert keychain.legacy() == sorted(values)  # KEPT: a rollback to 0.5.0 still finds them
    assert section(process) == {'secrets': values,
                                'migrated': {account: hashlib.sha256(value.encode()).hexdigest()
                                             for account, value in values.items()}}
    # The helper's write is read back before anything relies on it.
    puts = [index for index, request in enumerate(process.requests) if request.startswith(PUT_PREFIX)]
    assert all(process.requests[index + 1] == GET_ALL + b'\n' for index in puts)

    # The next build (another code hash; a new process): the item answers everything and no old item is read.
    keychain.identity, keychain.prompts, keychain.calls = 'pebble-0.7.0', [], []
    later = owner(keychain, process)
    for account, value in values.items():
        assert later.get(account) == value
    assert keychain.legacy_reads() == [] and keychain.prompts == []

    # A rollback to 0.5.0 reads its own per-item items, unchanged.
    rolled_back = Vault(keychain=keychain, owner=False)
    assert {account: rolled_back.get(account) for account in values} == values


def test_new_values_and_deletions_never_fall_back_to_a_kept_item():
    keychain, process = FakeKeychain(), FakeHelperProcess()
    values = sign_in(keychain)
    vault = owner(keychain, process)
    assert vault.get('profile-1') == SECRET
    keychain.calls.clear()
    keychain.prompts.clear()
    vault.set('profile-1', 'sk-new')
    vault.delete('oauth:p1:g1:0')  # an old item never read: it is marked and not read (V1-017: its kept item goes)
    assert vault.get('oauth:p1:g1:0') == '' and vault.get('profile-1') == 'sk-new'
    vault.delete('profile-1')
    assert vault.get('profile-1') == ''
    vault.set('oauth:p1:g1:1', 'replacement')  # an unread old item is superseded without being read
    assert vault.get('oauth:p1:g1:1') == 'replacement'
    assert keychain.legacy_reads() == [] and keychain.prompts == []
    # V1-017 (Phase 24, deliberate): deleting an account now also removes its kept per-item item (removal reads no
    # item data, so still no read or prompt above); the two deleted accounts' kept items are gone, the others kept.
    assert keychain.legacy() == sorted(set(values) - {'oauth:p1:g1:0', 'profile-1'})
    migrated = section(process)['migrated']
    assert migrated['oauth:p1:g1:0'] == migrated['oauth:p1:g1:1'] == SUPERSEDED
    assert 'profile-1' not in section(process)['secrets']
    # Accounts that never had an old item cost one lookup each, and nothing is recorded for them.
    assert vault.get('nothing') == '' and 'nothing' not in section(process)['migrated']


def test_every_service_has_its_own_section_in_the_one_item():
    from pebble.app import PlanVault
    from pebble.connector_auth import ConnectorVault
    keychain, process = FakeKeychain(), FakeHelperProcess()
    keychain.put(CONNECTORS, 'connector:grant:manifest', 'old-manifest')
    owner(keychain, process).set('profile-1', 'key')
    owner(keychain, process, ConnectorVault).set('grant:0', 'chunk')
    assert owner(keychain, process, ConnectorVault).get('grant:manifest') == 'old-manifest'
    owner(keychain, process, PlanVault).set('plan:deploy', 'token')
    services = process.store()['services']
    assert set(services) == {'com.pebble.local.providers', 'com.pebble.local.connectors',
                             'com.pebble.local.plan-credentials'}
    assert services['com.pebble.local.connectors']['secrets'] == {'connector:grant:0': 'chunk',
                                                                  'connector:grant:manifest': 'old-manifest'}
    assert services['com.pebble.local.plan-credentials']['secrets'] == {'plan:deploy': 'token'}
    assert keychain.legacy(CONNECTORS) == ['connector:grant:manifest'] and keychain.legacy() == []


def test_other_processes_keep_per_item_storage_and_never_run_the_helper():
    keychain, process = FakeKeychain(identity='python-validator'), FakeHelperProcess()
    vault = Vault(keychain=keychain, owner=False, helper=store_for(process))
    vault.set('fixture', 'fixture-key')
    assert vault.get('fixture') == 'fixture-key' and vault.get('missing') == ''
    vault.delete('fixture')
    assert keychain.items == {} and process.requests == []
    assert [call[0] for call in keychain.calls] == ['write', 'read', 'read', 'remove']


# ---- Fallbacks: sign-in never fails because of the helper ------------------------------------------------------


def bundle(tmp_path, name, identifier, helper=False):
    app = tmp_path / name
    (app / 'Contents/Resources/resources/backend').mkdir(parents=True)
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': identifier}))
    executable = app / 'Contents/Resources/resources/backend/pebble-backend'
    executable.write_bytes(b'\xcf\xfa\xed\xfe')
    if helper:
        (app / 'Contents/Resources/resources/helpers').mkdir()
        (app / 'Contents/Resources/resources/helpers/pebble-vault').write_bytes(b'\xcf\xfa\xed\xfe helper')
    return executable


@pytest.mark.parametrize('case,frozen,expected', [
    ('release', True, True), ('release', False, False), ('candidate', True, False), ('stage', True, False)])
def test_only_the_release_bundles_frozen_backend_owns_the_item(tmp_path, monkeypatch, case, frozen, expected):
    executables = {'release': bundle(tmp_path, 'Pebble.app', 'com.pebble.local'),
                   'candidate': bundle(tmp_path, 'Pebble Candidate.app', 'com.pebble.candidate')}
    stage = tmp_path / 'C/stage/resources/backend/pebble-backend'
    stage.parent.mkdir(parents=True)
    stage.write_bytes(b'\xcf\xfa\xed\xfe')
    executables['stage'] = stage
    monkeypatch.setattr(sys, 'executable', str(executables[case]))
    if frozen:
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
    else:
        monkeypatch.delattr(sys, 'frozen', raising=False)
    assert vault_module.installed_owner() is expected
    assert vault_module.bundle_identifier(tmp_path / 'nowhere') is None


def test_the_release_backend_finds_its_pinned_helper_or_keeps_per_item_items(tmp_path, monkeypatch, caplog):
    process = FakeHelperProcess()
    monkeypatch.setattr(vault_module, 'run_helper', process)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)

    # No helper in the bundle: per-item, without trying to run anything.
    monkeypatch.setattr(sys, 'executable', str(bundle(tmp_path / 'one', 'Pebble.app', 'com.pebble.local')))
    keychain = FakeKeychain()
    Vault(keychain=keychain).set('profile-1', 'key')
    assert keychain.legacy() == ['profile-1'] and process.requests == []

    # The helper is there and answers: the one item.
    vault_module._OWNER.clear()
    vault_module._HELPER.clear()
    executable = bundle(tmp_path / 'two', 'Pebble.app', 'com.pebble.local', helper=True)
    monkeypatch.setattr(sys, 'executable', str(executable))
    assert vault_module.helper_path() == executable.parent.parent / 'helpers/pebble-vault'
    keychain = FakeKeychain()
    Vault(keychain=keychain).set('profile-1', 'key')
    assert keychain.legacy() == [] and section(process)['secrets'] == {'profile-1': 'key'}
    assert process.requests[0] == PING + b'\n'

    # The helper refuses this backend (for example an ad-hoc build): per-item, and sign-in still works.
    vault_module._HELPER.clear()
    process.refuse = True
    keychain = FakeKeychain()
    with caplog.at_level(logging.WARNING):
        Vault(keychain=keychain).set('oauth:p2:manifest', 'manifest')
    assert keychain.legacy() == ['oauth:p2:manifest'] and 'caller_refused' in caplog.text


def test_a_helper_that_stops_answering_falls_back_for_the_session(caplog):
    keychain, process = FakeKeychain(), FakeHelperProcess()
    vault = owner(keychain, process)
    vault.set('profile-1', SECRET)
    process.crash = True
    with caplog.at_level(logging.WARNING):
        vault.set('profile-2', SECRET + '-2')
    assert keychain.legacy() == ['profile-2'] and vault.get('profile-2') == SECRET + '-2'
    asked = len(process.requests)
    vault.delete('profile-2')
    assert len(process.requests) == asked and keychain.legacy() == []  # this session no longer runs the helper
    assert 'bad_reply' in caplog.text and SECRET not in caplog.text


def test_a_keychain_refusal_behind_the_helper_is_reported_not_bypassed(caplog):
    keychain, process = FakeKeychain(), FakeHelperProcess()
    process.deny = -128  # userCanceledErr: the person chose Deny
    vault = owner(keychain, process)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError) as error:
            vault.set('profile-1', SECRET)
    assert str(error.value) == 'Keychain access failed (vault helper: keychain_write_failed, -128).'
    assert keychain.items == {} and SECRET not in repr(error.value) and SECRET not in caplog.text
    process.deny, process.corrupt = None, True
    with pytest.raises(ValueError) as error:
        vault.set('profile-1', SECRET)
    assert str(error.value) == 'Keychain could not save the key.' and keychain.items == {}


def test_secrets_never_reach_errors_or_logs(caplog):
    keychain, process = FakeKeychain(), FakeHelperProcess()
    process.item = b'{"format":"pebble.vault/1","services":{"x":"%s"}}' % SECRET.encode()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError) as error:
            owner(keychain, process).get('profile-1')
    assert str(error.value) == 'The Pebble vault item in Keychain is unreadable.'
    assert SECRET not in repr(error.value) and SECRET not in caplog.text


# ---- Maintenance -----------------------------------------------------------------------------------------------


def test_cleanup_removes_the_kept_items_only_when_asked():
    keychain, process = FakeKeychain(), FakeHelperProcess()
    values = sign_in(keychain)
    vault = owner(keychain, process)
    for account in values:
        vault.get(account)
    assert vault.migration_status() == {'store': 'helper', 'services': {
        'com.pebble.local.providers': {'secrets': len(values), 'migrated': len(values)}}}
    keychain.fail_remove.add((LEGACY_SERVICE, b'profile-1'))
    report = vault.cleanup_legacy()
    assert report == {'com.pebble.local.providers': {'removed': len(values) - 1, 'absent': 0, 'failed': 1}}
    assert keychain.legacy() == ['profile-1'] and section(process)['migrated'] == {
        'profile-1': hashlib.sha256(SECRET.encode()).hexdigest()}
    keychain.fail_remove.clear()
    assert vault.cleanup_legacy() == {'com.pebble.local.providers': {'removed': 1, 'absent': 0, 'failed': 0}}
    assert keychain.legacy() == [] and {account: vault.get(account) for account in values} == values
    with pytest.raises(ValueError):
        Vault(keychain=keychain, owner=False).cleanup_legacy()
    assert Vault(keychain=keychain, owner=False).migration_status() == {'store': 'per_item', 'services': {}}


def test_concurrent_writers_in_one_process_lose_nothing():
    keychain, process = FakeKeychain(), FakeHelperProcess()
    store = store_for(process)
    threads = [threading.Thread(target=lambda n=n: Vault(keychain=keychain, owner=True, helper=store).set(
        'account-%d' % n, 'key-%d' % n)) for n in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert section(process)['secrets'] == {'account-%d' % n: 'key-%d' % n for n in range(16)}


def test_the_public_api_is_unchanged():
    from pebble.app import PlanVault
    from pebble.connector_auth import ConnectorVault
    assert (Vault.service, ConnectorVault.service, PlanVault.service) == (LEGACY_SERVICE, CONNECTORS, PLANS)
    for name in ('get', 'set', 'delete', '_find'):  # test_pebble_install and test_phase20_security patch these
        assert callable(getattr(Vault, name))
    keychain = FakeKeychain()
    for bad in ('', 'x' * 2401, 'line\nbreak', 'carriage\rreturn'):
        with pytest.raises(ValueError) as error:
            owner(keychain, FakeHelperProcess()).set('a', bad)
        assert str(error.value) == 'Enter a valid API key.'


# ---- The helper's source ---------------------------------------------------------------------------------------


def test_the_helper_source_keeps_its_promises():
    text = SOURCE.read_text()
    code = re.sub(r'/\*.*?\*/', '', text, flags=re.S)  # promises are checked in the code, not the comments
    for forbidden in ('getenv', 'environ', 'stderr', '__DATE__', '__TIME__', 'printf("%s', 'system(', 'popen',
                      'SecKeychainFindGenericPassword', 'kSecUseDataProtectionKeychain'):
        assert forbidden not in code, forbidden
    for required in ('getppid()', 'SecCodeCopySelf', 'SecCodeCopySigningInformation', 'kSecCodeInfoCertificates',
                     'SecCodeCopyGuestWithAttributes', 'kSecGuestAttributePid', 'SecCodeCheckValidity',
                     'certificate leaf = H', 'memset_s', 'MAX_REQUEST', 'EXIT_REFUSED = 3'):
        assert required in code, required
    assert code.count('getppid()') == 2  # read again after the check: a re-parented helper refuses
    assert '#define SERVICE "com.pebble.local.vault"' in code and '#define ACCOUNT "v1"' in code
    assert '\\"com.pebble.local.backend\\"' in code and '\\"com.pebble.local\\")' in code
    # The reply never echoes the request or item data in an error: fail() takes only a fixed code and a status.
    assert re.search(r'static int fail\(const char \*error, OSStatus status, int code\)', code)
    calls = re.findall(r'(?<!int )\bfail\(([^,]+),', code)
    assert calls and all(re.fullmatch(r'"[a-z_]+"', argument) for argument in calls)
