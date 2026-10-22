"""Phase 22 (k22-identity): scripts/pebble_signing.py, the local code-signing identity tool.

Every case uses a home under pytest's tmp_path and an injected runner that fakes /usr/bin/security and
/usr/bin/openssl inside it. No real security, openssl or codesign call happens, and the real home is never used.
"""
import base64
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('pebble_signing_under_test', REPO / 'scripts' / 'pebble_signing.py')
ps = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ps)

DER = b'fake certificate DER for Pebble Local Signing'
PEM = b'-----BEGIN CERTIFICATE-----\n' + base64.encodebytes(DER) + b'-----END CERTIFICATE-----\n'
SHA1 = hashlib.sha1(DER).hexdigest().upper()
STAMP = '20261014T093000Z'
LOGIN = '/Users/person/Library/Keychains/login.keychain-db'


@pytest.fixture(autouse=True)
def no_real_tools(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('a test ran a real command')
    monkeypatch.setattr(subprocess, 'run', refuse)
    monkeypatch.setattr(subprocess, 'Popen', refuse)
    monkeypatch.setattr(ps.sys, 'platform', 'darwin')


class FakeTools:
    """security and openssl as far as the tool uses them: files appear where the real tools would write them."""

    def __init__(self, home, fail=(), search=(LOGIN,), restore=True):
        self.home, self.fail, self.calls = Path(home), set(fail), []
        self.search, self.restore, self.trusted = list(search), restore, False

    def keychain(self):
        return self.home / ps.KEYCHAIN

    def __call__(self, argv, stdin, timeout):
        self.calls.append({'argv': list(argv), 'stdin': stdin, 'timeout': timeout})
        name = argv[1] if argv[0] == ps.SECURITY else 'openssl-' + argv[1]
        if name in self.fail:
            return 1, b'fake failure'
        if name == 'list-keychains':
            if '-s' in argv:
                if self.restore:
                    self.search = argv[argv.index('-s') + 1:]
                return 0, b''
            return 0, ''.join('    "%s"\n' % path for path in self.search).encode()
        if name == 'create-keychain':
            Path(argv[-1]).write_bytes(b'keychain')
            self.search.append(argv[-1])  # create-keychain adds it to the search list
            return 0, b''
        if name == 'openssl-req':
            Path(argv[argv.index('-keyout') + 1]).write_bytes(b'encrypted key')
            Path(argv[argv.index('-out') + 1]).write_bytes(PEM)
            return 0, b''
        if name == 'openssl-x509':
            return 0, b'notAfter=Oct 11 09:30:00 2036 GMT\n'
        if name == 'openssl-pkcs12':
            Path(argv[argv.index('-out') + 1]).write_bytes(b'p12')
            return 0, b''
        if name == 'add-trusted-cert':
            self.trusted = True
            return 0, b''
        if name == 'find-identity':
            listed = '  1) %s "%s"\n     1 valid identities found\n' % (SHA1, ps.NAME) if self.trusted else \
                     '     0 valid identities found\n'
            return 0, listed.encode()
        if name == 'find-certificate':
            return 0, PEM
        if name == 'remove-trusted-cert':
            self.trusted = False
            return 0, b''
        if name == 'delete-keychain':
            Path(argv[-1]).unlink()
            return 0, b''
        if name == 'unlock-keychain':
            return 0, ('unlocked with %s\n' % argv[3]).encode()  # a tool that echoes its argument
        return 0, b''

    def names(self):
        return [call['argv'][1] if call['argv'][0] == ps.SECURITY else 'openssl-' + call['argv'][1]
                for call in self.calls]


def make_home(tmp_path):
    home = tmp_path.resolve() / 'home'
    (home / 'Library/Keychains').mkdir(parents=True)
    return home


def call(capsys, *argv, home, runner, temporary=None):
    code = ps.main(list(argv), runner=runner, home=home, temporary=temporary)
    return code, json.loads(capsys.readouterr().out)


def tree(path):
    return sorted(str(p.relative_to(path)) for p in Path(path).rglob('*'))


def test_create_is_a_dry_run_by_default_and_shows_every_step(tmp_path, capsys):
    home = make_home(tmp_path)

    def forbidden(*args):
        raise AssertionError('a dry run ran a command')
    before = tree(tmp_path)
    code, report = call(capsys, 'create', home=home, runner=forbidden)
    assert code == 0 and report['mode'] == 'dry-run' and tree(tmp_path) == before
    steps = [step['step'] for step in report['steps']]
    assert steps == ['read-search-list', 'make-folder', 'write-password', 'write-openssl-config',
                     'generate-key-and-certificate', 'inspect-certificate', 'package-identity', 'create-keychain',
                     'restore-search-list', 'check-search-list', 'keychain-settings', 'unlock-keychain',
                     'import-identity', 'allow-codesign', 'trust-for-code-signing', 'verify-identity', 'lock-keychain',
                     'write-record']
    commands = {step['step']: step for step in report['steps']}
    keychain = str(home / ps.KEYCHAIN)
    assert commands['create-keychain']['argv'] == [ps.SECURITY, 'create-keychain', '-p', '<keychain-password>', keychain]
    # It locks itself 15 minutes after use and on sleep, and create leaves it locked.
    assert commands['keychain-settings']['argv'] == [ps.SECURITY, 'set-keychain-settings', '-l', '-u', '-t', '900', keychain]
    assert commands['lock-keychain']['argv'] == [ps.SECURITY, 'lock-keychain', keychain]
    assert commands['import-identity']['argv'][-4:] == ['-P', '<transit-passphrase>', '-x', '-T', ps.CODESIGN][-4:]
    assert '-x' in commands['import-identity']['argv']  # the private key is not extractable
    assert commands['trust-for-code-signing']['argv'][1:6] == ['add-trusted-cert', '-r', 'trustRoot', '-p', 'codeSign']
    assert commands['trust-for-code-signing']['prompt'] == ps.PROMPT and report['prompts'] == [ps.PROMPT]
    generate = commands['generate-key-and-certificate']
    assert generate['argv'][:2] == [ps.OPENSSL, 'req'] and generate['stdin'] == ['<transit-passphrase>']
    assert generate['argv'][generate['argv'].index('-days') + 1] == '3650'
    assert all(argv[0] in (ps.SECURITY, ps.OPENSSL) for argv in (step['argv'] for step in report['steps'] if 'argv' in step))
    assert report['paths']['password_file'] == str(home / '.pebble/signing/keychain-password')
    assert report['identity']['designated_requirements']['release'] == \
        'identifier "com.pebble.local" and certificate leaf = H"<sha1>"'
    assert 'codeSigning' in ps.OPENSSL_CONFIG and 'CA:FALSE' in ps.OPENSSL_CONFIG


def test_execute_needs_the_parents_approval_stamp(tmp_path, capsys):
    home = make_home(tmp_path)
    tools = FakeTools(home)
    code, report = call(capsys, 'create', '--execute', home=home, runner=tools)
    assert code == 2 and report['refused'][0]['code'] == 'approval_required' and tools.calls == []
    with pytest.raises(SystemExit):
        ps.main(['create', '--execute', '--i-am-the-parent-with-user-approval', 'yesterday'], runner=tools, home=home)
    assert tools.calls == [] and not (home / '.pebble').exists()


def test_create_makes_the_identity_and_never_reports_a_secret(tmp_path, capsys):
    home = make_home(tmp_path)
    scratch = tmp_path.resolve() / 'scratch'
    scratch.mkdir()
    tools = FakeTools(home)
    code, report = call(capsys, 'create', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home,
                        runner=tools, temporary=scratch)
    assert code == 0, report
    password = (home / '.pebble/signing/keychain-password').read_text()
    assert len(password) == 64 and stat.S_IMODE((home / '.pebble/signing/keychain-password').stat().st_mode) == 0o600
    assert stat.S_IMODE((home / '.pebble/signing').stat().st_mode) == 0o700
    assert stat.S_IMODE((home / '.pebble').stat().st_mode) == 0o700
    text = json.dumps(report)
    assert password not in text and report['identity']['sha1'] == SHA1
    assert report['identity']['designated_requirements'] == {
        'release': 'identifier "com.pebble.local" and certificate leaf = H"%s"' % SHA1.lower(),
        'test': 'identifier "com.pebble.candidate" and certificate leaf = H"%s"' % SHA1.lower(),
        'backend': 'identifier "com.pebble.local.backend" and certificate leaf = H"%s"' % SHA1.lower(),
        'helper': 'identifier "com.pebble.vault-helper" and certificate leaf = H"%s"' % SHA1.lower()}
    assert tools.names()[-1] == 'lock-keychain'  # the last command: the identity is left locked
    record = json.loads((home / '.pebble/signing/identity.json').read_text())
    assert record['sha1'] == SHA1 and record['approved_stamp'] == STAMP and record['keychain'] == str(tools.keychain())
    assert record['not_after'] == 'Oct 11 09:30:00 2036 GMT' and password not in json.dumps(record)
    # The transient passphrase went to openssl on stdin only, and to security import as its argument.
    transit = tools.calls[tools.names().index('openssl-req')]['stdin'].decode().strip()
    assert transit and transit not in text
    pkcs12 = tools.calls[tools.names().index('openssl-pkcs12')]
    assert pkcs12['stdin'].decode() == '%s\n%s\n' % (transit, transit) and transit not in ' '.join(pkcs12['argv'])
    # The search list is back to what it was, and the private temporary folder (key material) is gone.
    assert tools.search == [LOGIN] and list(scratch.iterdir()) == []
    assert tools.names().index('add-trusted-cert') > tools.names().index('import')
    trust = tools.calls[tools.names().index('add-trusted-cert')]
    assert trust['timeout'] == ps.PROMPT_SECONDS and '-d' not in trust['argv']  # the user domain, not admin

    # The builder's view of it: files only, then status.
    identity = ps.load_identity(home)
    assert identity.sha1 == SHA1 and identity.keychain == tools.keychain()
    assert ps.load_identity(home, SHA1.lower()) == identity and ps.load_identity(home, '0' * 40) is None
    code, shown = call(capsys, 'status', '--probe', home=home, runner=tools)
    assert code == 0 and shown['state'] == 'created' and shown['probe']['valid_identities'] == [{'sha1': SHA1,
                                                                                                'name': ps.NAME}]
    assert shown['password_file']['mode'] == '0600' and password not in json.dumps(shown)

    # A second create is refused before anything runs.
    tools.calls.clear()
    code, again = call(capsys, 'create', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home,
                       runner=tools)
    assert code == 3 and {item['code'] for item in again['refused']} == {'already_exists', 'folder_not_empty'}
    assert tools.calls == []


def test_unlock_and_the_codesign_arguments_carry_no_secret_in_what_is_shown(tmp_path, capsys):
    home = make_home(tmp_path)
    tools = FakeTools(home)
    call(capsys, 'create', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home, runner=tools,
         temporary=tmp_path)
    identity = ps.load_identity(home)
    password = identity.paths.password.read_text()
    tools.calls.clear()
    ps.unlock(identity, tools)
    assert tools.calls[0]['argv'] == [ps.SECURITY, 'unlock-keychain', '-p', password, str(identity.keychain)]
    tools.fail.add('unlock-keychain')
    with pytest.raises(ps.Refused) as error:
        ps.unlock(identity, lambda argv, stdin, timeout: (1, ('bad password %s' % argv[3]).encode()))
    assert error.value.code == 'keychain_unlock_failed' and password not in repr(error.value.facts)
    argv = ps.sign_argv(identity, tmp_path / 'pebble-backend', 'com.pebble.local.backend')
    assert argv == [ps.CODESIGN, '--force', '--sign', SHA1, '--keychain', str(identity.keychain), '--identifier',
                    'com.pebble.local.backend', '--requirements',
                    '=designated => identifier "com.pebble.local.backend" and certificate leaf = H"%s"' % SHA1.lower(),
                    '--timestamp=none', str(tmp_path / 'pebble-backend')]
    assert password not in ' '.join(argv)
    hardened = ps.sign_argv(identity, 'x', 'y', runtime=True, entitlements='e.plist', preserve=True)
    assert hardened[-6:] == ['--preserve-metadata=entitlements,flags,runtime', '--options', 'runtime',
                             '--entitlements', 'e.plist', 'x']
    identity.paths.password.chmod(0o644)  # a password file others can read is refused, never used
    with pytest.raises(ps.Refused) as error:
        ps.unlock(identity, tools)
    assert error.value.code == 'private_file_unsafe'


def test_a_failure_after_a_change_stops_reports_what_changed_and_cleans_its_folder(tmp_path, capsys):
    home = make_home(tmp_path)
    scratch = tmp_path.resolve() / 'scratch'
    scratch.mkdir()
    tools = FakeTools(home, fail={'import'})
    code, report = call(capsys, 'create', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home,
                        runner=tools, temporary=scratch)
    password = (home / '.pebble/signing/keychain-password').read_text()
    assert code == 4 and report['stopped'] == 'command_failed' and report['facts']['step'] == 'import-identity'
    assert report['changed'] == ['make-folder', 'write-password', 'write-openssl-config', 'create-keychain',
                                 'restore-search-list', 'keychain-settings', 'unlock-keychain']
    assert 'remove' in report['recovery'] and password not in json.dumps(report) and list(scratch.iterdir()) == []
    assert not (home / '.pebble/signing/identity.json').exists()
    # remove undoes the partial create.
    code, removed = call(capsys, 'remove', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home,
                         runner=tools, temporary=scratch)
    assert code == 0 and removed['result'] == 'removed'
    assert not tools.keychain().exists() and not (home / '.pebble/signing').exists()


def test_a_search_list_that_cannot_be_restored_stops_the_create(tmp_path, capsys):
    home = make_home(tmp_path)
    tools = FakeTools(home, restore=False)
    code, report = call(capsys, 'create', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home,
                        runner=tools, temporary=tmp_path)
    assert code == 4 and report['stopped'] == 'search_list_not_restored'
    assert report['facts']['original'] == [LOGIN] and report['facts']['now'] == [LOGIN, str(tools.keychain())]
    # A search list without the login keychain is refused before any change.
    other = make_home(tmp_path / 'other')
    tools = FakeTools(other, search=['/Library/Keychains/System.keychain'])
    code, report = call(capsys, 'create', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=other,
                        runner=tools)
    assert code == 3 and report['stopped'] == 'search_list_unexpected' and not (other / '.pebble').exists()


def test_remove_is_the_reverse_of_create(tmp_path, capsys):
    home = make_home(tmp_path)
    tools = FakeTools(home)
    call(capsys, 'create', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home, runner=tools,
         temporary=tmp_path)
    tools.calls.clear()
    code, planned = call(capsys, 'remove', home=home, runner=tools)
    assert code == 0 and planned['mode'] == 'dry-run' and planned['result'] == 'planned' and tools.calls == []
    assert [step['step'] for step in planned['steps']] == ['export-certificate', 'remove-trust', 'delete-keychain',
                                                           'delete-password', 'delete-record', 'remove-folder-if-empty']
    code, removed = call(capsys, 'remove', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home,
                         runner=tools, temporary=tmp_path)
    assert code == 0 and removed['result'] == 'removed' and tools.trusted is False
    assert tools.names() == ['find-certificate', 'remove-trusted-cert', 'delete-keychain']
    assert not tools.keychain().exists() and not (home / '.pebble/signing').exists() and (home / '.pebble').is_dir()
    code, again = call(capsys, 'remove', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home,
                       runner=tools)
    assert code == 0 and again['result'] == 'absent'
    code, shown = call(capsys, 'status', home=home, runner=tools)
    assert shown['state'] == 'absent' and shown['identity'] is None


def test_remove_refuses_a_keychain_holding_another_certificate(tmp_path, capsys):
    home = make_home(tmp_path)
    tools = FakeTools(home)
    call(capsys, 'create', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home, runner=tools,
         temporary=tmp_path)
    other_pem = b'-----BEGIN CERTIFICATE-----\n' + base64.encodebytes(b'another') + b'-----END CERTIFICATE-----\n'
    tools.calls.clear()

    def swapped(argv, stdin, timeout):
        return (0, other_pem) if argv[1] == 'find-certificate' else tools(argv, stdin, timeout)
    code, report = call(capsys, 'remove', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home,
                        runner=swapped, temporary=tmp_path)
    assert code == 3 and report['stopped'] == 'certificate_differs' and tools.keychain().exists()


def test_parsers():
    listing = ('Policy: Code Signing\n  Matching identities\n  1) %s "%s"\n     1 identities found\n\n'
               '  Valid identities only\n  1) %s "%s"\n     1 valid identities found\n' % (SHA1, ps.NAME, SHA1, ps.NAME))
    assert set(ps.parse_identities(listing)) == {(SHA1, ps.NAME)}
    assert ps.parse_identities('  1) %s "Other" (CSSMERR_TP_NOT_TRUSTED)\n' % SHA1) == []
    assert ps.parse_search_list('    "/a/login.keychain-db"\n    "/Library/Keychains/System.keychain"\n') == [
        '/a/login.keychain-db', '/Library/Keychains/System.keychain']
    with pytest.raises(ps.Refused):
        ps.parse_search_list('unquoted\n')
    assert ps.certificate_sha1(PEM) == SHA1
    shown = ('Executable=/C/a/pebble-backend\nIdentifier=com.pebble.local.backend\nFormat=Mach-O thin (arm64)\n'
             'CodeDirectory v=20400 size=1 flags=0x10000(runtime) hashes=1+7 location=embedded\nCDHash=ABCDEF0123\n'
             'Authority=Pebble Local Signing\nTeamIdentifier=not set\nExecutable=/C/b/Python\nIdentifier=x\n'
             'CodeDirectory v=20400 size=1 flags=0x0(none) hashes=1+7 location=embedded\n'
             'designated => identifier "x" and certificate leaf = H"abc"\n')
    blocks = ps.parse_codesign_blocks(shown)
    assert blocks['/C/a/pebble-backend'] == {'identifier': 'com.pebble.local.backend', 'cdhash': 'abcdef0123',
                                             'authorities': [ps.NAME], 'team': 'not set', 'flags': ['runtime'],
                                             'designated': None}
    assert blocks['/C/b/Python']['designated'] == 'identifier "x" and certificate leaf = H"abc"'
    assert ps.same_requirement('identifier "x"  and certificate leaf = H"ABC"', 'identifier "x" and certificate leaf = H"abc"')
    assert ps.redact(b'pw=s3cret!', ['s3cret']) == 'pw=<redacted>!'


def test_lock_closes_the_identity_keychain(tmp_path, capsys):
    home = make_home(tmp_path)
    tools = FakeTools(home)
    call(capsys, 'create', '--execute', '--i-am-the-parent-with-user-approval', STAMP, home=home, runner=tools,
         temporary=tmp_path)
    identity = ps.load_identity(home)
    tools.calls.clear()
    ps.lock(identity, tools)
    assert tools.calls[0]['argv'] == [ps.SECURITY, 'lock-keychain', str(identity.keychain)]
    with pytest.raises(ps.Refused) as error:
        ps.lock(identity, lambda argv, stdin, timeout: (1, b'no'))
    assert error.value.code == 'keychain_lock_failed'


def pin_helper(root, identity_sha1=SHA1, source=b'int main(void) { return 0; }\n', binary=b'\xcf\xfa\xed\xfe helper'):
    (root / 'src-tauri/helpers/pebble-vault').mkdir(parents=True, exist_ok=True)
    (root / ps.HELPER_SOURCE).write_bytes(source)
    folder = root / 'src-tauri/resources' / ps.HELPER_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ps.HELPER_BINARY).write_bytes(binary)
    manifest = {'format': ps.HELPER_FORMAT, 'identifier': ps.IDENTIFIERS['helper'],
                'sha256': hashlib.sha256(binary).hexdigest(), 'cdhash': 'c' * 40,
                'designated_requirement': ps.requirement(ps.IDENTIFIERS['helper'], identity_sha1),
                'identity': {'name': ps.NAME, 'sha1': identity_sha1},
                'source': {'path': ps.HELPER_SOURCE, 'sha256': hashlib.sha256(source).hexdigest()}}
    (folder / ps.HELPER_MANIFEST).write_text(json.dumps(manifest))
    return folder, manifest


def test_the_pinned_helper_must_match_its_manifest_source_and_signer(tmp_path):
    root = tmp_path.resolve() / 'checkout'
    assert ps.check_pinned_helper(root) is None
    folder, manifest = pin_helper(root)
    identity = ps.Identity(SHA1, ps.NAME, tmp_path / 'kc', ps.Paths.at(tmp_path))
    assert ps.check_pinned_helper(root, identity) == manifest == ps.check_pinned_helper(root)
    other = ps.Identity('B' * 40, ps.NAME, tmp_path / 'kc', ps.Paths.at(tmp_path))
    cases = []
    with pytest.raises(ps.Refused) as error:
        ps.check_pinned_helper(root, other)
    cases.append(error.value.code)
    (root / ps.HELPER_SOURCE).write_bytes(b'changed\n')
    with pytest.raises(ps.Refused) as error:
        ps.check_pinned_helper(root)
    cases.append(error.value.code)
    pin_helper(root)
    (folder / ps.HELPER_BINARY).write_bytes(b'\xcf\xfa\xed\xfe rebuilt')
    with pytest.raises(ps.Refused) as error:
        ps.check_pinned_helper(root)
    cases.append(error.value.code)
    pin_helper(root)
    (folder / 'extra').write_bytes(b'x')
    with pytest.raises(ps.Refused) as error:
        ps.check_pinned_helper(root)
    cases.append(error.value.code)
    assert cases == ['vault_helper_identity_mismatch', 'vault_helper_source_differs', 'vault_helper_differs',
                     'vault_helper_invalid']


def test_the_real_runner_gets_a_fixed_environment(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return subprocess.CompletedProcess(argv, 0, b'ok', None)
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setenv('PEBBLE_LEAK', 'leaked')
    assert ps.run(['/usr/bin/true'], b'in', 5, home=tmp_path) == (0, b'ok')
    assert seen['env'] == {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'C', 'HOME': str(tmp_path)}
    assert seen['input'] == b'in' and seen['cwd'] == '/' and seen['timeout'] == 5 and seen['start_new_session']
