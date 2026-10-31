"""Phase 45 (layer 4): scripts/build_screen_helper.py builds the screen helper twice, signs it once and pins it beside
the vault helper; pebble_signing checks both pins; the candidate builder stages the screen helper byte for byte and
checks its pin and signature without re-signing it.

Every case uses an owned checkout and identity under tmp_path and an injected runner that fakes xcrun swiftc and
codesign; the keychain unlock and lock are recorded stand-ins. No real compiler, codesign, security or Keychain call
happens, and the real home's signing folder is never read."""
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location('build_screen_helper_under_test',
                                               REPO / 'scripts' / 'build_screen_helper.py')
bsh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bsh)
signing = bsh.signing
SHA1 = 'A1B2C3D4E5F60718293A4B5C6D7E8F9012345678'
STAMP = '20261026T030000Z'
BINARY = b'\xcf\xfa\xed\xfe unsigned screen helper'
VAULT = b'\xcf\xfa\xed\xfe pinned vault helper'


def pin_vault(root, sha1=SHA1):
    source = b'/* pebble_vault.c */\nint main(void) { return 3; }\n'
    (root / signing.HELPER_SOURCE).parent.mkdir(parents=True, exist_ok=True)
    (root / signing.HELPER_SOURCE).write_bytes(source)
    folder = root / 'src-tauri/resources' / signing.HELPER_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    (folder / signing.HELPER_BINARY).write_bytes(VAULT)
    manifest = {'format': signing.HELPER_FORMAT, 'identifier': signing.IDENTIFIERS['helper'],
                'sha256': hashlib.sha256(VAULT).hexdigest(), 'cdhash': 'c' * 40,
                'designated_requirement': signing.requirement(signing.IDENTIFIERS['helper'], sha1),
                'identity': {'name': signing.NAME, 'sha1': sha1},
                'source': {'path': signing.HELPER_SOURCE, 'sha256': hashlib.sha256(source).hexdigest()}}
    (folder / signing.HELPER_MANIFEST).write_text(json.dumps(manifest))
    return manifest


def make_checkout(tmp_path, vault=True):
    root = tmp_path.resolve() / 'checkout'
    source = root / signing.SCREEN_HELPER_SOURCE
    source.parent.mkdir(parents=True)
    source.write_bytes((REPO / signing.SCREEN_HELPER_SOURCE).read_bytes())
    (root / '.runtime').mkdir()
    (root / 'src-tauri/resources').mkdir(parents=True, exist_ok=True)
    if vault:
        pin_vault(root)
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
    """xcrun swiftc and codesign as far as the tool uses them, writing inside the work folder only."""

    def __init__(self, deterministic=True, runtime=True, leak=None):
        self.calls, self.builds, self.signed = [], 0, {}
        self.deterministic, self.runtime, self.leak = deterministic, runtime, leak

    def __call__(self, argv, cwd, env, stream):
        self.calls.append({'argv': argv, 'cwd': cwd, 'env': env})
        assert set(env) == {'PATH', 'LANG', 'HOME', 'TMPDIR'} and env['HOME'].startswith(cwd)
        if argv[:3] == [bsh.XCRUN, 'swiftc', '--version']:
            stream.write(b'swift-driver version: 1.127.14.1 Apple Swift version 6.2.1 (swiftlang-6.2.1.4.8)\n'
                         b'Target: arm64-apple-macosx26.0\n')
        elif argv[:2] == [bsh.XCRUN, '--show-sdk-version']:
            stream.write(b'26.1\n')
        elif argv[:2] == [bsh.XCRUN, 'swiftc']:
            assert argv[2:] == [*bsh.SWIFTC_FLAGS, 'main.swift', '-o', bsh.OUTPUT, *bsh.LDFLAGS]
            assert Path(cwd, 'main.swift').read_bytes() == (REPO / signing.SCREEN_HELPER_SOURCE).read_bytes()
            self.builds += 1
            data = BINARY + (b'' if self.deterministic else b'%d' % self.builds)
            if self.leak:
                data += self.leak(cwd).encode()
            Path(cwd, 'home', 'ModuleCache').mkdir(parents=True, exist_ok=True)
            Path(cwd, bsh.OUTPUT).write_bytes(data)
        elif argv[0] == bsh.CODESIGN and '--sign' in argv:
            target = Path(argv[-1])
            target.write_bytes(target.read_bytes() + b'+signature')
            self.signed[str(target)] = (argv[argv.index('--identifier') + 1],
                                        argv[argv.index('--requirements') + 1][len('=designated => '):], argv)
        elif argv[0] == bsh.CODESIGN and '--display' in argv:
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
    code = bsh.main(['--root', str(root), *extra], runner=tools or FakeTools(), home=home,
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
    assert list(commands) == ['swiftc-version', 'sdk-version', 'build-1', 'build-2', 'sign', 'verify', 'display',
                              'requirements']
    assert commands['build-1']['argv'] == commands['build-2']['argv'] == [
        bsh.XCRUN, 'swiftc', '-O', '-swift-version', '5', '-target', 'arm64-apple-macos14.0', '-file-compilation-dir',
        '.', 'main.swift', '-o', 'out/pebble-screen-helper', '-Xlinker', '-no_adhoc_codesign']
    sign = commands['sign']['argv']
    assert sign[sign.index('--identifier') + 1] == 'com.pebble.screen-helper' and '--timestamp=none' in sign
    assert sign[sign.index('--options') + 1] == 'runtime' and '--entitlements' not in sign
    assert sign[sign.index('--requirements') + 1] == (
        '=designated => identifier "com.pebble.screen-helper" and certificate leaf = H"%s"' % SHA1.lower())
    assert plan['work'] == str(root / '.runtime/screen-helper-<stamp>')
    assert all(command['cwd'] == plan['work'] for command in plan['commands'])
    assert '0' * 64 not in json.dumps(plan)  # the keychain password never appears
    assert plan['writes'][1:] == [str(root / 'src-tauri/resources/helpers' / name)
                                  for name in ('pebble-screen-helper', 'pebble-screen-helper.json')]


def test_execute_needs_approval_the_identity_and_the_vault_helper_beside_it(tmp_path, capsys):
    root, home = make_checkout(tmp_path)
    tools = FakeTools()
    code, report = call(capsys, root, home, '--execute', tools=tools)
    assert code == 2 and report['refused'] == 'approval_required'
    code, report = call(capsys, root, home, '--identity', 'adhoc', tools=tools)
    assert code == 3 and report['refused'] == 'identity_required'
    code, plan = call(capsys, root, tmp_path / 'empty-home', tools=tools)
    assert code == 3 and [problem['refused'] for problem in plan['problems']] == ['identity_required']
    bare, bare_home = make_checkout(tmp_path / 'bare', vault=False)
    code, plan = call(capsys, bare, bare_home, tools=tools)
    assert code == 3 and [problem['refused'] for problem in plan['problems']] == ['vault_helper_missing']
    assert tools.calls == []


def test_execute_builds_twice_signs_once_and_pins_beside_the_vault_helper(tmp_path, capsys):
    root, home = make_checkout(tmp_path)
    tools, events = FakeTools(), []
    code, report = call(capsys, root, home, '--execute', '--i-am-the-parent-with-user-approval', STAMP,
                        tools=tools, events=events)
    assert code == 0, report
    work = root / '.runtime' / ('screen-helper-' + STAMP)
    pinned = root / 'src-tauri/resources/helpers'
    binary = (pinned / 'pebble-screen-helper').read_bytes()
    assert binary == BINARY + b'+signature' and (work / 'first/pebble-screen-helper').read_bytes() == BINARY
    assert stat.S_IMODE((pinned / 'pebble-screen-helper').stat().st_mode) == 0o755
    assert stat.S_IMODE((pinned / 'pebble-screen-helper.json').stat().st_mode) == 0o644
    manifest = json.loads((pinned / 'pebble-screen-helper.json').read_text())
    assert manifest == report['manifest'] and manifest['format'] == 'pebble.screen-helper/1'
    assert manifest['identifier'] == 'com.pebble.screen-helper' and manifest['protocol'] == 'pebble-screen/1'
    assert manifest['sha256'] == hashlib.sha256(binary).hexdigest()
    assert manifest['cdhash'] == hashlib.sha1(binary).hexdigest()
    assert manifest['designated_requirement'] == signing.requirement('com.pebble.screen-helper', SHA1)
    assert manifest['source'] == {'path': signing.SCREEN_HELPER_SOURCE, 'sha256': hashlib.sha256(
        (root / signing.SCREEN_HELPER_SOURCE).read_bytes()).hexdigest()}
    assert manifest['unsigned_sha256'] == hashlib.sha256(BINARY).hexdigest() and manifest['approved_stamp'] == STAMP
    assert manifest['build']['swiftc'].startswith('swift-driver version') and manifest['build']['sdk_version'] == '26.1'
    assert str(tmp_path.resolve()) not in json.dumps(manifest) and '/Users/' not in json.dumps(manifest)
    # Both pins pass the candidate builder's own checks, the vault helper's unchanged.
    identity = signing.load_identity(home)
    assert signing.check_pinned_screen_helper(root, identity) == manifest
    assert signing.check_pinned_helper(root, identity)['identifier'] == 'com.pebble.vault-helper'
    assert events == ['unlock', 'lock'] and report['keychain_locked_after_signing'] is True
    assert {c['cwd'] for c in tools.calls} == {str(work)} and tools.builds == 2
    assert not (work / 'home').exists() and not (work / 'tmp').exists(), 'the compiler caches are gone'
    assert sorted(os.listdir(work / 'logs')) == sorted(label + '.log' for label in (
        'swiftc-version', 'sdk-version', 'build-1', 'build-2', 'sign', 'verify', 'display', 'requirements'))


def test_a_build_that_differs_or_carries_this_macs_paths_is_never_pinned(tmp_path, capsys):
    root, home = make_checkout(tmp_path)
    code, report = call(capsys, root, home, '--execute', '--i-am-the-parent-with-user-approval', STAMP,
                        tools=FakeTools(deterministic=False))
    assert code == 3 and report['refused'] == 'not_deterministic'
    other, other_home = make_checkout(tmp_path / 'leaky')
    events = []
    code, report = call(capsys, other, other_home, '--execute', '--i-am-the-parent-with-user-approval', STAMP,
                        tools=FakeTools(leak=lambda cwd: cwd), events=events)
    assert code == 3 and report['refused'] == 'machine_paths_in_helper' and events == [], 'never signed'
    for checkout in (root, other):
        assert not (checkout / 'src-tauri/resources/helpers/pebble-screen-helper').exists()


def test_a_pinned_helper_is_replaced_only_on_request_and_kept_as_evidence(tmp_path, capsys):
    root, home = make_checkout(tmp_path)
    call(capsys, root, home, '--execute', '--i-am-the-parent-with-user-approval', STAMP)
    old = json.loads((root / 'src-tauri/resources/helpers/pebble-screen-helper.json').read_text())
    code, plan = call(capsys, root, home)
    assert code == 3 and [p['refused'] for p in plan['problems']] == ['pinned_helper_exists']
    later = '20261026T040000Z'
    code, report = call(capsys, root, home, '--execute', '--replace-pinned-helper',
                        '--i-am-the-parent-with-user-approval', later)
    assert code == 0 and report['manifest']['replaces'] == {key: old[key] for key in ('sha256', 'cdhash',
                                                                                       'approved_stamp')}
    aside = root / '.runtime' / ('screen-helper-' + later) / 'replaced'
    assert sorted(os.listdir(aside)) == ['pebble-screen-helper', 'pebble-screen-helper.json']


def pin_screen(root, sha1=SHA1, binary=b'\xcf\xfa\xed\xfe pinned screen helper', cdhash='d' * 40):
    folder = root / 'src-tauri/resources' / signing.HELPER_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    (folder / signing.SCREEN_HELPER_BINARY).write_bytes(binary)
    (folder / signing.SCREEN_HELPER_BINARY).chmod(0o755)
    source = (root / signing.SCREEN_HELPER_SOURCE).read_bytes()
    manifest = {'format': signing.SCREEN_HELPER_FORMAT, 'identifier': signing.SCREEN_HELPER_IDENTIFIER,
                'path': 'helpers/pebble-screen-helper', 'sha256': hashlib.sha256(binary).hexdigest(),
                'cdhash': cdhash, 'designated_requirement': signing.requirement(signing.SCREEN_HELPER_IDENTIFIER, sha1),
                'identity': {'name': signing.NAME, 'sha1': sha1},
                'source': {'path': signing.SCREEN_HELPER_SOURCE, 'sha256': hashlib.sha256(source).hexdigest()}}
    (folder / signing.SCREEN_HELPER_MANIFEST).write_text(json.dumps(manifest))
    return manifest


def test_the_pins_are_checked_together_and_every_drift_is_refused(tmp_path):
    root, home = make_checkout(tmp_path)
    identity = signing.load_identity(home)
    assert signing.check_pinned_screen_helper(root) is None, 'no screen helper pinned: nothing to check'
    manifest = pin_screen(root)
    assert signing.check_pinned_screen_helper(root, identity) == manifest
    assert signing.check_pinned_helper(root, identity)['identifier'] == 'com.pebble.vault-helper'
    folder = root / 'src-tauri/resources/helpers'
    cases = {}
    for name, change in (
            ('half', lambda: (folder / signing.SCREEN_HELPER_MANIFEST).unlink()),
            ('binary', lambda: (folder / signing.SCREEN_HELPER_BINARY).write_bytes(b'rebuilt')),
            ('source', lambda: (root / signing.SCREEN_HELPER_SOURCE).write_bytes(b'// changed\n')),
            ('identity', lambda: pin_screen(root, sha1='B' * 40)),
            ('identifier', lambda: (folder / signing.SCREEN_HELPER_MANIFEST).write_text(json.dumps(
                {**manifest, 'identifier': 'com.pebble.vault-helper'})))):
        (root / signing.SCREEN_HELPER_SOURCE).write_bytes((REPO / signing.SCREEN_HELPER_SOURCE).read_bytes())
        pin_screen(root)
        change()
        with pytest.raises(signing.Refused) as error:
            signing.check_pinned_screen_helper(root, identity)
        cases[name] = error.value.code
    assert cases == {'half': 'screen_helper_invalid', 'binary': 'screen_helper_differs',
                     'source': 'screen_helper_source_differs', 'identity': 'screen_helper_identity_mismatch',
                     'identifier': 'screen_helper_invalid'}
    pin_screen(root)
    (folder / 'notes.txt').write_text('x')
    with pytest.raises(signing.Refused) as extra:
        signing.check_pinned_helper(root)
    assert extra.value.code == 'vault_helper_invalid', 'the folder holds the two pairs and nothing else'
    (folder / 'notes.txt').unlink()
    (folder / signing.SCREEN_HELPER_MANIFEST).unlink()
    with pytest.raises(signing.Refused) as half:
        signing.check_pinned_helper(root)
    assert half.value.code == 'vault_helper_invalid', 'half a screen pair is not a pinned folder either'


# ---- the candidate builder ----------------------------------------------------------------------------------------

def identity_fixtures():
    spec = importlib.util.spec_from_file_location('builder_identity_fixtures_45',
                                                  Path(__file__).with_name('test_builder_identity_22.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ident():
    if not (REPO / 'src-tauri/resources/tokenizer').is_dir():
        pytest.skip('the candidate builder fixture needs the checkout tokenizer under src-tauri/resources')
    return identity_fixtures()


def screen_runner(ident, checkout, identity, vault_cdhash, screen_cdhash):
    class ScreenSigningRunner(ident.SigningRunner):
        def display(self, argv, stream):
            path = argv[-1]
            if path.endswith('/stage/resources/helpers/pebble-screen-helper'):
                self.calls.append({'kind': 'display-screen-helper', 'argv': argv})
                stream.write(('Executable=%s\nIdentifier=com.pebble.screen-helper\nCodeDirectory v=20500 size=1 '
                              'flags=0x10000(runtime) hashes=1+7 location=embedded\nCDHash=%s\nAuthority=%s\n'
                              'TeamIdentifier=not set\n' % (path, screen_cdhash, ident.NAME)).encode())
                return 0
            return super().display(argv, stream)
    return ScreenSigningRunner(checkout, identity, helper_cdhash=vault_cdhash)


def staged_checkout(ident, tmp_path):
    base = ident.base
    checkout = base.make_checkout(tmp_path)
    _, identity = ident.make_identity(tmp_path)
    vault = ident.pin_helper(checkout.root)
    base.put(checkout.root / signing.SCREEN_HELPER_SOURCE, (REPO / signing.SCREEN_HELPER_SOURCE).read_bytes())
    screen = pin_screen(checkout.root)
    base.snap(checkout)  # the tested snapshot covers both pins and the Swift source
    return checkout, identity, vault, screen


def test_the_candidate_stages_the_pinned_screen_helper_unchanged_and_checks_its_signature(ident, tmp_path):
    bca = ident.bca
    checkout, identity, vault, screen = staged_checkout(ident, tmp_path)
    runner = screen_runner(ident, checkout, identity, vault['cdhash'], screen['cdhash'])
    build = ident.signed_build(checkout, identity, runner)
    planned = build.plan()
    assert planned['problems'] == [] and planned['screen_helper']['manifest'] == screen
    stage = next(step for step in planned['steps'] if step['step'] == 'stage')
    assert [c['label'] for c in stage['commands']] == ['ditto-backend', 'display-helper', 'display-screen-helper',
                                                      'find-identity']
    assert build.execute()['result'] == 'built'
    C = checkout.out
    for path in [C / 'stage/resources/helpers/pebble-screen-helper'] + [
            C / kind / (name + '.app') / 'Contents/Resources/resources/helpers/pebble-screen-helper'
            for kind, (name, _, _) in bca.APPS.items()]:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == screen['sha256']
        assert oct(path.stat().st_mode & 0o777) == '0o755'
    assert not any('/helpers/' in call['argv'][-1] for call in runner.calls if call['kind'] == 'codesign-sign')
    record = json.loads((C / 'stage/stage-manifest.json').read_text())
    expected = {'path': 'helpers/pebble-screen-helper', 'sha256': screen['sha256'], 'cdhash': screen['cdhash'],
                'identifier': 'com.pebble.screen-helper', 'designated_requirement': screen['designated_requirement'],
                'identity_sha1': SHA1, 'source_sha256': screen['source']['sha256'], 'resigned': False}
    assert record['screen_helper'] == expected and record['vault_helper']['cdhash'] == vault['cdhash']
    assert json.loads((C / 'app-manifest.json').read_text())['screen_helper'] == expected


def test_the_tested_snapshot_covers_the_swift_source_and_the_screen_pin(ident, tmp_path):
    checkout, *_ = staged_checkout(ident, tmp_path)
    names = set(ident.bca.load_verifier(checkout.root).sources())
    assert {'src-tauri/helpers/screen/main.swift', 'src-tauri/resources/helpers/pebble-screen-helper',
            'src-tauri/resources/helpers/pebble-screen-helper.json',
            'src-tauri/resources/helpers/pebble-vault'} <= names


def test_a_screen_helper_that_is_not_the_pinned_one_is_refused(ident, tmp_path):
    checkout, identity, vault, screen = staged_checkout(ident, tmp_path)
    runner = screen_runner(ident, checkout, identity, vault['cdhash'], 'e' * 40)
    with pytest.raises(ident.bca.Refused) as error:
        ident.signed_build(checkout, identity, runner).execute()
    assert error.value.code == 'screen_helper_signature_differs' and error.value.detail['problems'] == ['cdhash']
    other = tmp_path / 'other'
    checkout, identity, vault, screen = staged_checkout(ident, other)
    (checkout.root / 'src-tauri/resources/helpers/pebble-screen-helper').write_bytes(b'rebuilt')
    ident.base.snap(checkout)
    build = ident.signed_build(checkout, identity, screen_runner(ident, checkout, identity, vault['cdhash'],
                                                                 screen['cdhash']))
    assert [p['refused'] for p in build.plan()['problems']] == ['screen_helper_differs']
    with pytest.raises(ident.bca.Refused):
        build.execute()
    assert not checkout.out.exists()
