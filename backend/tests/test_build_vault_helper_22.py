"""Phase 22 (k22-identity): scripts/build_vault_helper.py, which builds the Keychain vault helper once and pins it.

Every case uses an owned checkout and identity under tmp_path and an injected runner that fakes xcrun clang and
codesign; the keychain unlock and lock are recorded stand-ins. No real compiler, codesign, security or Keychain call
happens.
"""
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('build_vault_helper_under_test', REPO / 'scripts' / 'build_vault_helper.py')
bvh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bvh)
signing = bvh.signing
SHA1 = 'A1B2C3D4E5F60718293A4B5C6D7E8F9012345678'
STAMP = '20261014T120000Z'
BINARY = b'\xcf\xfa\xed\xfe unsigned vault helper'


def make_checkout(tmp_path):
    root = tmp_path.resolve() / 'checkout'
    source = root / signing.HELPER_SOURCE
    source.parent.mkdir(parents=True)
    source.write_bytes((REPO / signing.HELPER_SOURCE).read_bytes())
    (root / '.runtime').mkdir()
    (root / 'src-tauri/resources').mkdir(parents=True)
    home = tmp_path.resolve() / 'home'
    paths = signing.Paths.at(home)
    paths.keychain.parent.mkdir(parents=True)
    paths.keychain.write_bytes(b'keychain')
    paths.folder.mkdir(parents=True, mode=0o700)
    for path, data in ((paths.password, b'0' * 64), (paths.record, json.dumps({
            'format': signing.RECORD_FORMAT, 'name': signing.NAME, 'sha1': SHA1, 'keychain': str(paths.keychain)}).encode())):
        path.write_bytes(data)
        path.chmod(0o600)
    return root, home


class FakeTools:
    """xcrun clang and codesign as far as the tool uses them, writing inside the work folder only."""

    def __init__(self, deterministic=True, runtime=True):
        self.calls, self.links, self.deterministic, self.runtime, self.signed = [], 0, deterministic, runtime, {}

    def __call__(self, argv, cwd, env, stream):
        self.calls.append({'argv': argv, 'cwd': cwd, 'env': env})
        assert set(env) == {'PATH', 'LANG', 'HOME', 'TMPDIR'} and env['HOME'].startswith(cwd)
        if argv[:3] == [bvh.XCRUN, 'clang', '--version']:
            stream.write(b'Apple clang version 17.0.0 (clang-1700.0.13.5)\nTarget: arm64-apple-darwin\n')
        elif argv[:2] == [bvh.XCRUN, '--show-sdk-version']:
            stream.write(b'26.0\n')
        elif argv[:2] == [bvh.XCRUN, 'clang'] and '-c' in argv:
            assert argv[argv.index('-o') + 1] == bvh.OBJECT and argv[-1].endswith(signing.HELPER_SOURCE)
            Path(cwd, bvh.OBJECT).write_bytes(b'object')
        elif argv[:2] == [bvh.XCRUN, 'clang']:
            assert argv[argv.index('-o') + 1] == bvh.OUTPUT and argv[-1] == bvh.OBJECT and '-Wl,-no_adhoc_codesign' in argv
            self.links += 1
            Path(cwd, bvh.OUTPUT).write_bytes(BINARY + (b'' if self.deterministic else b'%d' % self.links))
        elif argv[0] == bvh.CODESIGN and '--sign' in argv:
            target = Path(argv[-1])
            target.write_bytes(target.read_bytes() + b'+signature')
            self.signed[str(target)] = (argv[argv.index('--identifier') + 1],
                                        argv[argv.index('--requirements') + 1][len('=designated => '):], argv)
        elif argv[0] == bvh.CODESIGN and '--display' in argv:
            path = argv[-1]
            identifier, requirement, signed = self.signed[path]
            if '--requirements' in argv:
                stream.write(('Executable=%s\ndesignated => %s\n' % (path, requirement)).encode())
            else:
                flags = '0x10000(runtime)' if self.runtime and 'runtime' in signed else '0x0(none)'
                stream.write(('Executable=%s\nIdentifier=%s\nCodeDirectory v=20500 size=1 flags=%s hashes=1+7 '
                              'location=embedded\nCDHash=%s\nAuthority=%s\nTeamIdentifier=not set\n'
                              % (path, identifier, flags, hashlib.sha1(Path(path).read_bytes()).hexdigest(),
                                 signing.NAME)).encode())
        return 0


def call(capsys, root, home, *extra, tools=None, events=None):
    events = [] if events is None else events
    code = bvh.main(['--root', str(root), *extra], runner=tools or FakeTools(), home=home,
                    unlock=lambda: events.append('unlock'), lock=lambda: events.append('lock'))
    return code, json.loads(capsys.readouterr().out)


def tree(path):
    return sorted(str(p.relative_to(path)) for p in Path(path).rglob('*'))


def test_the_plan_is_the_default_and_writes_nothing(tmp_path, capsys):
    root, home = make_checkout(tmp_path)
    before = tree(tmp_path)
    code, plan = call(capsys, root, home, tools=lambda *args: pytest.fail('a plan ran a command'))
    assert code == 0 and plan['mode'] == 'plan' and plan['problems'] == [] and tree(tmp_path) == before
    commands = {command['label']: command for command in plan['commands']}
    assert list(commands) == ['clang-version', 'sdk-version', 'compile-1', 'link-1', 'compile-2', 'link-2', 'sign',
                              'verify', 'display', 'requirements']
    assert commands['compile-1']['argv'] == commands['compile-2']['argv'] == [
        bvh.XCRUN, 'clang', *bvh.CFLAGS, '-c', '-o', bvh.OBJECT, str(root / signing.HELPER_SOURCE)]
    assert commands['link-1']['argv'] == [bvh.XCRUN, 'clang', *bvh.LDFLAGS, '-o', bvh.OUTPUT, bvh.OBJECT]
    assert {'-Os', '-arch', 'arm64'} <= set(bvh.CFLAGS) and '-Wl,-no_adhoc_codesign' in bvh.LDFLAGS
    sign = commands['sign']['argv']
    assert sign[sign.index('--identifier') + 1] == 'com.pebble.vault-helper' and '--timestamp=none' in sign
    assert sign[sign.index('--options') + 1] == 'runtime' and '--entitlements' not in sign
    assert sign[sign.index('--requirements') + 1] == (
        '=designated => identifier "com.pebble.vault-helper" and certificate leaf = H"%s"' % SHA1.lower())
    assert plan['work'] == str(root / '.runtime/vault-helper-<stamp>')
    assert all(command['cwd'] == plan['work'] for command in plan['commands'])
    assert '0' * 64 not in json.dumps(plan)  # the keychain password never appears
    assert plan['writes'][1:] == [str(root / 'src-tauri/resources/helpers' / name)
                                  for name in ('pebble-vault', 'pebble-vault.json')]


def test_execute_needs_approval_and_the_local_identity(tmp_path, capsys):
    root, home = make_checkout(tmp_path)
    tools = FakeTools()
    code, report = call(capsys, root, home, '--execute', tools=tools)
    assert code == 2 and report['refused'] == 'approval_required' and tools.calls == []
    code, report = call(capsys, root, home, '--identity', 'adhoc', tools=tools)
    assert code == 3 and report['refused'] == 'identity_required'
    code, plan = call(capsys, root, tmp_path / 'empty-home', tools=tools)
    assert code == 3 and [problem['refused'] for problem in plan['problems']] == ['identity_required']
    assert tools.calls == [] and not (root / 'src-tauri/resources/helpers').exists()


def test_execute_builds_twice_signs_once_and_pins_the_helper(tmp_path, capsys):
    root, home = make_checkout(tmp_path)
    tools, events = FakeTools(), []
    code, report = call(capsys, root, home, '--execute', '--i-am-the-parent-with-user-approval', STAMP,
                        tools=tools, events=events)
    assert code == 0, report
    work = root / '.runtime' / ('vault-helper-' + STAMP)
    pinned = root / 'src-tauri/resources/helpers'
    binary = (pinned / 'pebble-vault').read_bytes()
    assert binary == BINARY + b'+signature' and (work / 'first/pebble-vault').read_bytes() == BINARY
    assert stat.S_IMODE((pinned / 'pebble-vault').stat().st_mode) == 0o755
    assert stat.S_IMODE((pinned / 'pebble-vault.json').stat().st_mode) == 0o644
    manifest = json.loads((pinned / 'pebble-vault.json').read_text())
    assert manifest == report['manifest']
    assert manifest['sha256'] == hashlib.sha256(binary).hexdigest()
    assert manifest['cdhash'] == hashlib.sha1(binary).hexdigest()
    assert manifest['designated_requirement'] == signing.requirement('com.pebble.vault-helper', SHA1)
    assert manifest['source'] == {'path': signing.HELPER_SOURCE,
                                  'sha256': hashlib.sha256((root / signing.HELPER_SOURCE).read_bytes()).hexdigest()}
    assert manifest['identity'] == {'name': signing.NAME, 'sha1': SHA1} and manifest['hardened_runtime'] is True
    assert manifest['unsigned_sha256'] == hashlib.sha256(BINARY).hexdigest() and manifest['approved_stamp'] == STAMP
    assert manifest['build']['clang'].startswith('Apple clang version') and manifest['build']['sdk_version'] == '26.0'
    # It ships inside every app: no machine path in it.
    assert str(tmp_path.resolve()) not in json.dumps(manifest) and '/Users/' not in json.dumps(manifest)
    # The candidate builder's own check accepts what was pinned.
    assert signing.check_pinned_helper(root, signing.load_identity(home)) == manifest
    # The keychain was open only around the one signature; every command ran in the work folder.
    assert events == ['unlock', 'lock'] and report['keychain_locked_after_signing'] is True
    assert {call['cwd'] for call in tools.calls} == {str(work)}
    assert sorted(os.listdir(work / 'logs')) == sorted(label + '.log' for label in (
        'clang-version', 'sdk-version', 'compile-1', 'link-1', 'compile-2', 'link-2', 'sign', 'verify', 'display',
        'requirements'))
    assert json.loads((work / 'build.json').read_text())['manifest'] == manifest

    # Pinned once: a second build is refused unless the parent means to replace it.
    code, again = call(capsys, root, home, '--execute', '--i-am-the-parent-with-user-approval', '20261014T130000Z')
    assert code == 3 and again['refused'] == 'pinned_helper_exists'
    code, replaced = call(capsys, root, home, '--execute', '--i-am-the-parent-with-user-approval', '20261014T130000Z',
                          '--replace-pinned-helper')
    assert code == 0 and replaced['replaced'] == ['pebble-vault', 'pebble-vault.json']
    assert (root / '.runtime/vault-helper-20261014T130000Z/replaced/pebble-vault').read_bytes() == binary


def test_a_build_that_is_not_reproducible_is_refused_before_signing(tmp_path, capsys):
    root, home = make_checkout(tmp_path)
    events = []
    code, report = call(capsys, root, home, '--execute', '--i-am-the-parent-with-user-approval', STAMP,
                        tools=FakeTools(deterministic=False), events=events)
    assert code == 3 and report['refused'] == 'not_deterministic' and events == []
    assert not (root / 'src-tauri/resources/helpers').exists()


def test_a_signature_without_the_hardened_runtime_is_refused_and_the_keychain_still_locks(tmp_path, capsys):
    root, home = make_checkout(tmp_path)
    events = []
    code, report = call(capsys, root, home, '--execute', '--i-am-the-parent-with-user-approval', STAMP,
                        tools=FakeTools(runtime=False), events=events)
    assert code == 3 and report['refused'] == 'signature_unexpected'
    assert report['detail']['problems'] == ['hardened_runtime'] and events == ['unlock', 'lock']
    assert not (root / 'src-tauri/resources/helpers').exists()

    second, second_home = make_checkout(tmp_path / 'second')
    tools, events = FakeTools(), []

    def failing(argv, cwd, env, stream):
        return 1 if argv[0] == bvh.CODESIGN and '--sign' in argv else tools(argv, cwd, env, stream)
    code, report = call(capsys, second, second_home, '--execute', '--i-am-the-parent-with-user-approval', STAMP,
                        tools=failing, events=events)
    assert code == 3 and report['refused'] == 'command_failed' and events == ['unlock', 'lock']
