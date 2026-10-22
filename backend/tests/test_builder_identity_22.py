"""Phase 22 (k22-identity): scripts/build_candidate_app.py signing with the local identity.

The fixtures are test_build_candidate_app.py's (an owned checkout under tmp_path and a runner that fakes every tool);
this file adds fakes for codesign signing and display and for `security find-identity`. No real codesign, security or
Keychain call happens, and the identity is a record under tmp_path, never the real home's.
"""
import hashlib
import importlib.util
import json
import plistlib
import re
import shlex
from pathlib import Path

import pytest

from pebble.release_readiness import Root, bundle_digest, bundle_entries, read_manifest

REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location('build_candidate_app_fixtures_22', Path(__file__).with_name(
    'test_build_candidate_app.py'))
base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(base)
bca, signing = base.bca, base.bca.signing
SHA1 = 'A1B2C3D4E5F60718293A4B5C6D7E8F9012345678'
NAME = signing.NAME


def make_identity(tmp_path):
    """The files scripts/pebble_signing.py create leaves behind, under an owned home."""
    home = tmp_path.resolve() / 'home'
    paths = signing.Paths.at(home)
    paths.keychain.parent.mkdir(parents=True)
    paths.keychain.write_bytes(b'keychain')
    paths.folder.mkdir(parents=True, mode=0o700)
    for path, data in ((paths.password, b'0' * 64), (paths.record, json.dumps({
            'format': signing.RECORD_FORMAT, 'name': NAME, 'sha1': SHA1, 'keychain': str(paths.keychain)}).encode())):
        path.write_bytes(data)
        path.chmod(0o600)
    return home, signing.load_identity(home)


class SigningRunner(base.FakeRunner):
    """FakeRunner plus codesign signing/display and security find-identity, as the real tools behave."""

    def __init__(self, checkout, identity, valid=True, tamper=None, helper_cdhash=None, **options):
        super().__init__(checkout, **options)
        self.identity, self.valid, self.tamper = identity, valid, tamper
        self.signed = {}  # path -> (identifier, requirement, argv)
        self.helper_cdhash = helper_cdhash  # what codesign shows for the pinned helper (never signed here)

    def __call__(self, argv, cwd, env, stream):
        if argv[0] == bca.SECURITY:
            self.calls.append({'kind': 'find-identity', 'argv': argv, 'cwd': cwd, 'env': env})
            assert argv == signing.find_argv(self.identity.keychain)
            listed = '  1) %s "%s"\n' % (SHA1, NAME) if self.valid else ''
            stream.write(('%s     %d valid identities found\n' % (listed, int(self.valid))).encode())
            return 0
        if argv[0] == bca.CODESIGN and '--sign' in argv:
            self.calls.append({'kind': 'codesign-sign', 'argv': argv, 'cwd': cwd, 'env': env})
            if 'codesign-sign' in self.fail:
                stream.write(b'fake failure\n')
                return 1
            return self.sign(argv, stream)
        if argv[0] == bca.CODESIGN and '--display' in argv:
            self.calls.append({'kind': 'codesign-display', 'argv': argv, 'cwd': cwd, 'env': env})
            return self.display(argv, stream)
        return super().__call__(argv, cwd, env, stream)

    def sign(self, argv, stream):
        target = Path(argv[-1])
        identifier = argv[argv.index('--identifier') + 1]
        requirement = argv[argv.index('--requirements') + 1]
        assert argv[argv.index('--sign') + 1] == SHA1 and argv[argv.index('--keychain') + 1] == str(self.identity.keychain)
        assert requirement == '=designated => ' + self.identity.requirement(identifier)
        if target.suffix == '.app':
            executable = target / 'Contents/MacOS/pebble'
            executable.write_bytes(executable.read_bytes() + b'+signature:' + identifier.encode())
            (target / 'Contents/_CodeSignature/CodeResources').write_bytes(b'<plist>sealed</plist>')
            if self.tamper:
                self.tamper(target)
        else:
            # Signing a copy in place: its own bytes change, nothing else (no _CodeSignature folder appears).
            assert 'signing/backend/' in str(target) and '.framework' not in str(target.parent)
            target.write_bytes(target.read_bytes() + b'+signature:' + identifier.encode())
            executable = target
        self.signed[str(executable)] = (identifier, requirement[len('=designated => '):], argv)
        stream.write(b'%s: replacing existing signature\n' % str(target).encode())
        return 0

    def display(self, argv, stream):
        paths = [arg for arg in argv[2:] if arg not in ('--verbose=4', '--requirements', '-')]
        for path in paths:
            executable = path + '/Contents/MacOS/pebble' if path.endswith('.app') else path
            if executable not in self.signed:  # the pinned helper, as build_vault_helper.py signed it once
                assert executable.endswith('/stage/resources/helpers/pebble-vault')
                stream.write(('Executable=%s\nIdentifier=com.pebble.vault-helper\nCodeDirectory v=20500 size=1 '
                              'flags=0x10000(runtime) hashes=1+7 location=embedded\nCDHash=%s\nAuthority=%s\n'
                              'TeamIdentifier=not set\n' % (executable, self.helper_cdhash, NAME)).encode())
                continue
            identifier, requirement, signed_argv = self.signed[executable]
            lines = ['Executable=' + executable]
            if '--requirements' in argv:
                lines.append('designated => ' + requirement)
            else:
                flags = '0x10000(runtime)' if 'runtime' in signed_argv else '0x0(none)'
                cdhash = hashlib.sha1(Path(executable).read_bytes()).hexdigest()
                lines += ['Identifier=' + identifier, 'Format=Mach-O thin (arm64)',
                          'CodeDirectory v=20400 size=512 flags=%s hashes=8+7 location=embedded' % flags,
                          'CDHash=' + cdhash, 'Authority=' + NAME, 'TeamIdentifier=not set']
            stream.write(('\n'.join(lines) + '\n').encode())
        return 0


def signed_build(checkout, identity, runner=None, **options):
    """A build whose keychain unlock and lock are recorded stand-ins (never the real security tool)."""
    events = []
    built = bca.Build(checkout.root, checkout.out, snapshot=checkout.snapshot, node_bin=checkout.node,
                      runner=runner if runner is not None else SigningRunner(checkout, identity),
                      identity=identity, unlock=lambda: events.append('unlock'), lock=lambda: events.append('lock'),
                      **options)
    built.events = events
    return built


def test_a_build_with_the_identity_signs_the_backend_and_both_bundles(tmp_path):
    checkout = base.make_checkout(tmp_path)
    home, identity = make_identity(tmp_path)
    C = checkout.out
    runner = SigningRunner(checkout, identity, hooks={'pyinstaller': base.framework})
    build = signed_build(checkout, identity, runner)
    planned = build.plan()
    assert planned['problems'] == [] and planned['signing']['mode'] == 'local_identity'
    assert planned['signing']['designated_requirements']['backend'] == identity.requirement('com.pebble.local.backend')
    labels = [command['label'] for step in planned['steps'] for command in step['commands']]
    assert labels.count('find-identity') == 2 and {'sign-release', 'sign-test', 'display-release',
                                                    'requirements-test'} <= set(labels)
    result = build.execute()
    # The keychain is open only around each signing command: the backend batch, then each bundle.
    assert result['result'] == 'built' and build.events == ['unlock', 'lock'] * 3

    # Every Mach-O of the staged backend was signed as a copy, deepest first and the executable last.
    sign_calls = [call for call in runner.calls if call['kind'] == 'codesign-sign']
    backend_signs = [call['argv'][-1] for call in sign_calls if '/signing/backend/' in call['argv'][-1]]
    assert Path(backend_signs[-1]).name == 'pebble-backend'
    stage = json.loads((C / 'stage/stage-manifest.json').read_text())
    order = [entry['path'] for entry in stage['signing']['files']]  # recorded in signing order
    assert order[-1] == 'backend/pebble-backend' and stage['signing']['files'][-1]['identifier'] == 'com.pebble.local.backend'
    depths = [path.count('/') for path in order[:-1]]
    assert depths == sorted(depths, reverse=True)
    assert [call['argv'][call['argv'].index('--identifier') + 1] for call in sign_calls[:len(order)]] == \
        [entry['identifier'] for entry in stage['signing']['files']]
    signed_paths = sorted(order)
    assert signed_paths == sorted([
        'backend/pebble-backend', 'backend/_internal/Python', 'backend/_internal/Python.framework/Python',
        'backend/_internal/Python.framework/Versions/3.12/Python',
        'backend/_internal/Python.framework/Versions/Current/Python',
        'backend/_internal/Python.framework/Versions/3.12/lib/libpython3.12.dylib',
        'backend/_internal/Python.framework/Versions/Current/lib/libpython3.12.dylib',
        # V1-045: the fixture backend's dylib alias and extension are Pillow's own now (no pillow_heif ships).
        'backend/_internal/libwebp.7.dylib', 'backend/_internal/PIL/.dylibs/libwebp.7.dylib',
        'backend/_internal/PIL/_imaging.so'])
    assert not any('_CodeSignature' in entry['path'] for entry in stage['files'])
    for entry in stage['signing']['files']:
        staged = C / 'stage/resources' / entry['path']
        assert entry['sha256'] == base.sha(staged) != entry['unsigned_sha256']
        assert entry['cdhash'] == hashlib.sha1(staged.read_bytes()).hexdigest()
        assert re.fullmatch(r'com\.pebble\.local\.backend(\.[A-Za-z0-9._-]+)?', entry['identifier'])
    assert stage['backend']['signed_executable_sha256'] == base.sha(C / 'stage/resources/backend/pebble-backend')
    assert stage['backend']['flattened_bundle_sha256'] != stage['backend']['signed_bundle_sha256']
    # Every other staged file is still exactly the flattened dist's; modes are kept.
    assert stage['modes']['backend/pebble-backend'] == '0755'
    assert (C / 'stage/resources/backend/_internal/base_library.zip').read_bytes() == b'zip'

    # Both bundles: re-signed with the stable identifier and the explicit requirement, then verified.
    manifest = json.loads((C / 'app-manifest.json').read_text())
    record = base.inputs(checkout)
    assert record['signing'] == dict(identity.describe(), hardened_backend=False)
    assert manifest['signing']['mode'] == 'local_identity' and manifest['signing']['developer_id'] is False
    assert manifest['signing']['notarized'] is False and manifest['signing']['backend'] == stage['signing']
    backend = record['steps']['backend']['attempts'][-1]['evidence']
    assert manifest['signing']['unsigned_executable_sha256'] == backend['executable_sha256']
    assert manifest['candidate_executable_sha256'] == stage['backend']['signed_executable_sha256']
    assert manifest['candidate_bundle_sha256'] == stage['backend']['signed_bundle_sha256'] == \
        manifest['subject']['candidate_bundle_sha256']
    for kind, (name, identifier, _) in bca.APPS.items():
        app = C / kind / (name + '.app')
        bundle = manifest['apps'][kind]
        signature = bundle['signing']
        assert signature['identifier'] == identifier and signature['designated_requirement'] == \
            'identifier "%s" and certificate leaf = H"%s"' % (identifier, SHA1.lower())
        assert signature['cdhash'] == hashlib.sha1((app / 'Contents/MacOS/pebble').read_bytes()).hexdigest()
        assert signature['built_bundle_sha256'] == record['steps']['shell-' + kind]['attempts'][-1]['evidence'][
            'built_bundle_sha256'] != bundle['app_bundle_sha256']
        with Root(str(app), 'test') as root:
            entries, links, problems = bundle_entries(root)
        assert not problems and bundle['files'] == entries and bundle['app_bundle_sha256'] == bundle_digest(entries)
        assert read_manifest(json.dumps(bundle).encode())['files'] == entries
        assert bundle['subject']['app_bundle_sha256'] == bundle_digest(entries)
        assert bundle['backend_executable_sha256'] == manifest['candidate_executable_sha256']
        assert manifest['signing']['apps'][kind] == signature
        # The sign command keeps Tauri's entitlements and flags; the deep strict verify runs after it.
        kinds = [call['kind'] for call in runner.calls if str(app) in ' '.join(call['argv'])]
        assert kinds == ['ditto', 'codesign-sign', 'codesign', 'codesign-display', 'codesign-display']
        sign = next(call['argv'] for call in runner.calls if call['kind'] == 'codesign-sign' and call['argv'][-1] == str(app))
        assert '--preserve-metadata=entitlements,flags,runtime' in sign and '--options' not in sign
    # All signing commands ran with the native environment (C/home, no inherited signing variables).
    for call in runner.calls:
        if call['kind'].startswith(('codesign', 'find-identity')):
            assert set(call['env']) == {'PATH', 'LANG', 'HOME', 'TMPDIR'} and bca.within(call['env']['HOME'], C)
    # The per-binary commands went to one log each, with their argv; no secret is in any log or record.
    log = (C / 'logs/stage.1.sign-backend.log').read_text()
    assert log.count('$ /usr/bin/codesign --force --sign ' + SHA1) == len(backend_signs)
    password = identity.paths.password.read_text()
    for path in list((C / 'logs').iterdir()) + [C / 'inputs.json', C / 'app-manifest.json', C / 'stage/stage-manifest.json']:
        assert password not in path.read_text()


def test_without_an_identity_the_build_is_exactly_as_before(tmp_path, capsys):
    checkout = base.make_checkout(tmp_path)
    runner = base.FakeRunner(checkout)
    plain = bca.Build(checkout.root, checkout.out, snapshot=checkout.snapshot, node_bin=checkout.node, runner=runner)
    planned = [command['argv'] for step in plain.plan()['steps'] for command in step['commands']]
    assert plain.plan()['signing']['mode'] == 'adhoc'
    plain.execute()
    assert [call['argv'] for call in runner.calls] == planned
    assert not any(call['kind'] in ('codesign-sign', 'find-identity') for call in runner.calls)
    assert all(call['argv'][1:4] == ['--verify', '--deep', '--strict'] for call in runner.calls if call['kind'] == 'codesign')
    manifest = json.loads((checkout.out / 'app-manifest.json').read_text())
    assert manifest['signing'] == {'mode': 'adhoc'} and manifest['apps']['release']['signing'] == {'mode': 'adhoc'}
    assert 'signing' not in base.inputs(checkout) and not (checkout.out / 'signing').exists()
    stage = json.loads((checkout.out / 'stage/stage-manifest.json').read_text())
    assert 'signing' not in stage and 'signed_bundle_sha256' not in stage['backend']

    # --identity: auto finds nothing in an empty home; adhoc is ad hoc; an unknown SHA-1 is refused.
    home = tmp_path.resolve() / 'empty-home'
    home.mkdir()
    assert bca.resolve_identity('auto', home) is None and bca.resolve_identity('adhoc', home) is None
    with pytest.raises(bca.Refused) as error:
        bca.resolve_identity(SHA1, home)
    assert error.value.code == 'signing_identity_unknown'
    with pytest.raises(bca.Refused) as error:
        bca.resolve_identity('not-a-sha1', home)
    assert error.value.code == 'identity_argument'
    arguments = ['--root', str(checkout.root), '--out', str(tmp_path / 'other-C'), '--snapshot', str(checkout.snapshot),
                 '--node-bin', str(checkout.node)]
    assert bca.main(arguments + ['--hardened-backend'], runner=runner, home=home) == 3
    assert json.loads(capsys.readouterr().out)['refused'] == 'hardened_backend_needs_identity'


def test_auto_and_an_explicit_sha1_find_the_local_identity_for_the_plan(tmp_path, capsys):
    checkout = base.make_checkout(tmp_path)
    home, identity = make_identity(tmp_path)

    def forbidden(*args):
        raise AssertionError('plan mode ran a command')
    arguments = ['--root', str(checkout.root), '--out', str(checkout.out), '--snapshot', str(checkout.snapshot),
                 '--node-bin', str(checkout.node)]
    for choice in ([], ['--identity', 'auto'], ['--identity', SHA1.lower()]):
        assert bca.main(arguments + choice, runner=forbidden, home=home) == 0
        plan = json.loads(capsys.readouterr().out)
        assert plan['signing']['identity'] == identity.describe() and plan['problems'] == []
    assert bca.main(arguments + ['--identity', 'adhoc'], runner=forbidden, home=home) == 0
    assert json.loads(capsys.readouterr().out)['signing']['mode'] == 'adhoc'
    # The identity's files gone after the plan: the build refuses before writing anything.
    identity.paths.keychain.unlink()
    build = signed_build(checkout, identity)
    problems = [problem['refused'] for problem in build.plan()['problems']]
    assert problems == ['signing_identity_unavailable']
    with pytest.raises(bca.Refused) as error:
        build.execute()
    assert error.value.code == 'signing_identity_unavailable' and not checkout.out.exists()


def test_an_identity_that_is_not_valid_stops_the_build_before_signing(tmp_path):
    checkout = base.make_checkout(tmp_path)
    _, identity = make_identity(tmp_path)
    runner = SigningRunner(checkout, identity, valid=False)
    with pytest.raises(bca.Refused) as error:
        signed_build(checkout, identity, runner).execute()
    assert error.value.code == 'signing_identity_unavailable'
    assert error.value.detail['valid'] == [] and base.inputs(checkout)['failure']['step'] == 'stage'
    assert not any(call['kind'] == 'codesign-sign' for call in runner.calls)

    second = base.make_checkout(tmp_path / 'second')
    _, identity = make_identity(tmp_path / 'second')

    def locked():
        raise signing.Refused('keychain_unlock_failed', 'The signing keychain could not be unlocked.', exit=51)
    relocked = []
    build = bca.Build(second.root, second.out, snapshot=second.snapshot, node_bin=second.node,
                      runner=SigningRunner(second, identity), identity=identity, unlock=locked,
                      lock=lambda: relocked.append(True))
    with pytest.raises(bca.Refused) as error:
        build.execute()
    assert error.value.code == 'signing_keychain_locked' and relocked == []  # never opened, so nothing to close
    assert error.value.detail == {'code': 'keychain_unlock_failed', 'message': 'The signing keychain could not be unlocked.'}


def test_signing_that_touches_anything_else_is_refused(tmp_path):
    checkout = base.make_checkout(tmp_path)
    _, identity = make_identity(tmp_path)

    def into_resources(app):
        (app / 'Contents/Resources/resources/model.json').write_text('{"model": "changed"}\n')
    with pytest.raises(bca.Refused) as error:
        signed_build(checkout, identity, SigningRunner(checkout, identity, tamper=into_resources)).execute()
    assert error.value.code == 'signing_changed_unexpected_paths'
    assert error.value.paths == ['Contents/Resources/resources/model.json']
    assert not (checkout.out / 'app-manifest.json').exists()

    second = base.make_checkout(tmp_path / 'second')
    _, identity = make_identity(tmp_path / 'second')

    def nested(argv, cwd, env, stream):
        app = Path(env['CARGO_TARGET_DIR']) / 'release/bundle/macos/Pebble.app'
        base.put(app / 'Contents/Frameworks/Helper.framework/Helper', b'\xcf\xfa\xed\xfe helper', 0o755)
    with pytest.raises(bca.Refused) as error:
        signed_build(second, identity, SigningRunner(second, identity, hooks={'tauri-release': nested})).execute()
    assert error.value.code == 'nested_code_not_supported'
    assert error.value.paths == ['Contents/Frameworks/Helper.framework/Helper']


def test_a_signature_without_the_identity_or_requirement_is_refused(tmp_path):
    checkout = base.make_checkout(tmp_path)
    _, identity = make_identity(tmp_path)

    class Adhoc(SigningRunner):
        def display(self, argv, stream):
            text = []
            super().display(argv, type('S', (), {'write': lambda _, data: text.append(data)})())
            stream.write(b''.join(text).replace(b'Authority=' + NAME.encode() + b'\n', b'Signature=adhoc\n'))
            return 0
    with pytest.raises(bca.Refused) as error:
        signed_build(checkout, identity, Adhoc(checkout, identity)).execute()
    assert error.value.code == 'signature_unexpected' and 'authority' in error.value.detail['problems']
    # Nothing was put in place: the staged backend is still the unsigned flattened dist.
    staged = checkout.out / 'stage/resources/backend/pebble-backend'
    assert staged.read_bytes() == base.BACKEND_BYTES

    second = base.make_checkout(tmp_path / 'second')
    _, identity = make_identity(tmp_path / 'second')

    class Loose(SigningRunner):
        def display(self, argv, stream):
            text = []
            super().display(argv, type('S', (), {'write': lambda _, data: text.append(data)})())
            stream.write(re.sub(rb'designated => [^\n]*', b'designated => cdhash H"00"', b''.join(text)))
            return 0
    with pytest.raises(bca.Refused) as error:
        signed_build(second, identity, Loose(second, identity)).execute()
    assert error.value.code == 'signature_unexpected' and error.value.detail['problems'] == ['designated_requirement']

    # A bundle whose re-signature still carries the ad-hoc flag (release_readiness would read it as ad hoc).
    third = base.make_checkout(tmp_path / 'third')
    _, identity = make_identity(tmp_path / 'third')

    class Flagged(SigningRunner):
        def display(self, argv, stream):
            text = []
            super().display(argv, type('S', (), {'write': lambda _, data: text.append(data)})())
            data = b''.join(text)
            stream.write(data.replace(b'flags=0x0(none)', b'flags=0x2(adhoc)') if argv[-1].endswith('.app') else data)
            return 0
    with pytest.raises(bca.Refused) as error:
        signed_build(third, identity, Flagged(third, identity)).execute()
    assert error.value.code == 'signature_unexpected' and error.value.detail['problems'] == ['adhoc_flag']
    assert error.value.paths == [base.C_REL + '/release/Pebble.app/Contents/MacOS/pebble']
    assert not (third.out / 'app-manifest.json').exists()


def test_the_hardened_backend_experiment_and_resume_bind_the_signer(tmp_path):
    checkout = base.make_checkout(tmp_path)
    _, identity = make_identity(tmp_path)
    runner = SigningRunner(checkout, identity)
    build = signed_build(checkout, identity, runner, hardened_backend=True)
    assert build.execute(steps=bca.STEPS[:bca.STEPS.index('stage') + 1])['result'] is None
    entitlements = plistlib.loads((checkout.out / 'signing/backend.entitlements').read_bytes())
    assert entitlements == {'com.apple.security.cs.disable-library-validation': True,
                            'com.apple.security.cs.allow-unsigned-executable-memory': True}
    backend_signs = [call['argv'] for call in runner.calls if call['kind'] == 'codesign-sign']
    assert all(argv[argv.index('--options') + 1] == 'runtime' and '--entitlements' in argv for argv in backend_signs)
    stage = json.loads((checkout.out / 'stage/stage-manifest.json').read_text())
    assert stage['signing']['hardened_runtime'] is True
    assert all(entry['flags'] == ['runtime'] for entry in stage['signing']['files'])
    # A resume must use the same signer: ad hoc, or another identity, is refused.
    plain = bca.Build(checkout.root, checkout.out, snapshot=checkout.snapshot, node_bin=checkout.node,
                      runner=base.FakeRunner(checkout))
    with pytest.raises(bca.Refused) as error:
        plain.execute(resume=True)
    assert error.value.code == 'resume_mismatch' and error.value.paths == ['signing']
    with pytest.raises(bca.Refused) as error:
        signed_build(checkout, identity, SigningRunner(checkout, identity)).execute(resume=True)
    assert error.value.code == 'resume_mismatch' and error.value.paths == ['signing']
    result = signed_build(checkout, identity, SigningRunner(checkout, identity), hardened_backend=True).execute(resume=True)
    assert result['result'] == 'built'


HELPER_BYTES = b'\xcf\xfa\xed\xfe pinned vault helper'


def pin_helper(root, sha1=SHA1, cdhash='c' * 40):
    """What scripts/build_vault_helper.py leaves in a checkout: the source and the pinned, signed binary."""
    source = b'/* pebble_vault.c */\nint main(void) { return 3; }\n'
    base.put(root / signing.HELPER_SOURCE, source)
    folder = root / 'src-tauri/resources' / signing.HELPER_FOLDER
    base.put(folder / signing.HELPER_BINARY, HELPER_BYTES, 0o755)
    manifest = {'format': signing.HELPER_FORMAT, 'identifier': signing.IDENTIFIERS['helper'],
                'path': 'helpers/pebble-vault', 'sha256': hashlib.sha256(HELPER_BYTES).hexdigest(), 'cdhash': cdhash,
                'designated_requirement': signing.requirement(signing.IDENTIFIERS['helper'], sha1),
                'identity': {'name': NAME, 'sha1': sha1},
                'source': {'path': signing.HELPER_SOURCE, 'sha256': hashlib.sha256(source).hexdigest()}}
    base.put(folder / signing.HELPER_MANIFEST, json.dumps(manifest))
    return manifest


def test_the_pinned_helper_ships_byte_for_byte_and_is_never_re_signed(tmp_path):
    checkout = base.make_checkout(tmp_path)
    _, identity = make_identity(tmp_path)
    manifest = pin_helper(checkout.root)
    # Merge (i22): the tested snapshot now covers the helper and its source (this track's P2 to
    # verify_harness_candidate.sources()), so it is taken after the helper is pinned, as the parent's will be.
    base.snap(checkout)
    runner = SigningRunner(checkout, identity, helper_cdhash=manifest['cdhash'])
    build = signed_build(checkout, identity, runner)
    planned = build.plan()
    assert planned['problems'] == [] and planned['vault_helper']['manifest'] == manifest
    stage = next(step for step in planned['steps'] if step['step'] == 'stage')
    assert [command['label'] for command in stage['commands']] == ['ditto-backend', 'display-helper', 'find-identity']
    assert build.execute()['result'] == 'built'
    C = checkout.out
    for path in [C / 'stage/resources/helpers/pebble-vault'] + [
            C / kind / (name + '.app') / 'Contents/Resources/resources/helpers/pebble-vault'
            for kind, (name, _, _) in bca.APPS.items()]:
        assert path.read_bytes() == HELPER_BYTES and oct(path.stat().st_mode & 0o777) == '0o755'
    assert not any('/helpers/' in call['argv'][-1] for call in runner.calls if call['kind'] == 'codesign-sign')
    record = json.loads((C / 'stage/stage-manifest.json').read_text())
    expected = {'path': 'helpers/pebble-vault', 'sha256': manifest['sha256'], 'cdhash': manifest['cdhash'],
                'identifier': 'com.pebble.vault-helper', 'designated_requirement': manifest['designated_requirement'],
                'identity_sha1': SHA1, 'source_sha256': manifest['source']['sha256'], 'resigned': False}
    assert record['vault_helper'] == expected
    assert record['sources']['helpers/pebble-vault'] == 'src-tauri/resources/helpers/pebble-vault'
    app_manifest = json.loads((C / 'app-manifest.json').read_text())
    assert app_manifest['vault_helper'] == expected
    assert base.inputs(checkout)['steps']['stage']['attempts'][-1]['evidence']['vault_helper'] == {
        'sha256': manifest['sha256'], 'cdhash': manifest['cdhash']}


def test_a_helper_that_is_not_the_pinned_one_is_refused(tmp_path):
    checkout = base.make_checkout(tmp_path)
    _, identity = make_identity(tmp_path)
    manifest = pin_helper(checkout.root)
    base.snap(checkout)  # merge (i22): the snapshot covers the pinned helper (see above)
    with pytest.raises(bca.Refused) as error:  # codesign shows another code hash than the pinned one
        signed_build(checkout, identity, SigningRunner(checkout, identity, helper_cdhash='d' * 40)).execute()
    assert error.value.code == 'vault_helper_signature_differs' and error.value.detail['problems'] == ['cdhash']
    assert error.value.detail['pinned_cdhash'] == manifest['cdhash']

    cases = {}
    for name, change in (
            ('identity', lambda root: pin_helper(root, sha1='B' * 40)),
            ('source', lambda root: (root / signing.HELPER_SOURCE).write_bytes(b'int main(void) { return 0; }\n')),
            ('binary', lambda root: (root / 'src-tauri/resources/helpers/pebble-vault').write_bytes(b'rebuilt')),
            ('extra', lambda root: base.put(root / 'src-tauri/resources/helpers/notes.txt', 'x'))):
        other = base.make_checkout(tmp_path / name)
        _, other_identity = make_identity(tmp_path / name)
        pin_helper(other.root)
        change(other.root)
        base.snap(other)  # merge (i22): a tested snapshot of this very checkout, so only the helper's own check refuses
        build = signed_build(other, other_identity)
        cases[name] = [problem['refused'] for problem in build.plan()['problems']]
        with pytest.raises(bca.Refused):
            build.execute()
        assert not other.out.exists()
    assert cases == {'identity': ['vault_helper_identity_mismatch'], 'source': ['vault_helper_source_differs'],
                     'binary': ['vault_helper_differs'], 'extra': ['vault_helper_invalid']}


def test_an_ad_hoc_build_ships_the_pinned_helper_unchanged(tmp_path):
    checkout = base.make_checkout(tmp_path)
    _, identity = make_identity(tmp_path)
    manifest = pin_helper(checkout.root)
    base.snap(checkout)  # merge (i22): the snapshot covers the pinned helper (see above)
    runner = SigningRunner(checkout, identity, helper_cdhash=manifest['cdhash'])
    plain = bca.Build(checkout.root, checkout.out, snapshot=checkout.snapshot, node_bin=checkout.node, runner=runner)
    assert plain.execute()['result'] == 'built'
    assert [call['kind'] for call in runner.calls].count('codesign-display') == 1  # display-helper only
    assert not any(call['kind'] in ('codesign-sign', 'find-identity') for call in runner.calls)
    app_manifest = json.loads((checkout.out / 'app-manifest.json').read_text())
    assert app_manifest['signing'] == {'mode': 'adhoc'} and app_manifest['vault_helper']['cdhash'] == manifest['cdhash']


def test_a_failed_signature_still_locks_the_keychain(tmp_path):
    checkout = base.make_checkout(tmp_path)
    _, identity = make_identity(tmp_path)
    build = signed_build(checkout, identity, SigningRunner(checkout, identity, fail={'codesign-sign'}))
    with pytest.raises(bca.Refused) as error:
        build.execute()
    assert error.value.code == 'command_failed' and build.events == ['unlock', 'lock']


def test_mach_o_detection_and_identifiers():
    assert bca.is_macho(b'\xcf\xfa\xed\xfe\x0c\x00\x00\x01') and bca.is_macho(b'\xca\xfe\xba\xbe\x00\x00\x00\x02')
    assert not bca.is_macho(b'\xca\xfe\xba\xbe\x00\x00\x00\x34')  # a Java class file (major version 52)
    assert not bca.is_macho(b'PK\x03\x04') and not bca.is_macho(b'')
    assert bca.backend_identifier('pebble-backend') == 'com.pebble.local.backend'
    assert bca.backend_identifier('_internal/PIL/.dylibs/libjpeg.62.dylib') == \
        'com.pebble.local.backend._internal.PIL..dylibs.libjpeg.62.dylib'
    long = bca.backend_identifier('_internal/' + 'a' * 300 + '.so')
    assert len(long) <= 200 and long == bca.backend_identifier('_internal/' + 'a' * 300 + '.so')
    assert 'scripts/pebble_signing.py' in bca.IMPORTED
    assert shlex.join(['a b']) == "'a b'"
