"""Phase 24 (t24-install): the vault helper's caller check (V1-025 interim, V1-140) and its re-pin (D5).

pebble_vault.c is read as source: it must find its parent's executable by the kernel's path, require it inside an
app bundle at Contents/Resources/resources/backend/pebble-backend, validate that app statically with its resources,
and compare the signers' leaf certificates by SHA-256. When a local clang and SDK exist, the source is also compiled
with -fsyntax-only (nothing is written, signed or run). build_vault_helper.py runs with test_build_vault_helper_22's
fake clang and codesign: a replacement needs its own approval stamp. No real codesign, Keychain or helper process runs.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import test_build_vault_helper_22 as tb

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / 'src-tauri/helpers/pebble-vault/pebble_vault.c'
bvh, signing = tb.bvh, tb.signing


def code():
    """The source without comments: promises are checked in the code."""
    return re.sub(r'/\*.*?\*/', '', SOURCE.read_text(), flags=re.S)


def body(text, name):
    """The body of one C function: from its signature to the closing brace at column 0."""
    match = re.search(r'\n[^\n]*\b' + name + r'\([^)]*\) \{\n(.*?)\n\}\n', text, re.S)
    assert match, name
    return match.group(1)


def test_the_parent_must_be_the_backend_inside_an_app_bundle():
    text = code()
    assert '#include <libproc.h>' in text
    assert '#define BACKEND_IN_APP "/Contents/Resources/resources/backend/pebble-backend"' in text
    assert '#define APP_SUFFIX ".app"' in text
    path = body(text, 'parent_app')
    # The kernel's path for the parent's PID, never argv or the environment; the bundle must end in .app and have a name.
    assert 'proc_pidpath(parent, path, sizeof path)' in path
    assert 'strcmp(path + app, BACKEND_IN_APP) != 0' in path and 'memcmp(path + app - extension, APP_SUFFIX' in path
    assert "path[app - extension - 1] == '/'" in path and "path[0] != '/'" in path


def test_the_enclosing_app_is_validated_statically_with_its_resources():
    text = code()
    check = body(text, 'app_intact')
    assert 'SecStaticCodeCreateWithPath(url' in check and 'SecStaticCodeCheckValidity(app, APP_VALIDATION, requirement)' in check
    assert '#define APP_VALIDATION (kSecCSStrictValidate | kSecCSCheckAllArchitectures)' in text
    # Resources are validated: nothing anywhere opts out of them.
    for opt_out in ('kSecCSDoNotValidateResources', 'kSecCSDoNotValidateExecutable', 'kSecCSQuickCheck',
                    'kSecCSBasicValidateOnly'):
        assert opt_out not in text, opt_out
    # The app is Pebble's (identifier com.pebble.local) and signed with this helper's own leaf.
    assert '#define APP_IDENTIFIER "identifier \\"com.pebble.local\\""' in text
    assert 'requirement_for(APP_IDENTIFIER, own)' in check and 'same_leaf(&leaf, own)' in check


def test_the_caller_requirement_compares_the_leaf_by_sha256():
    text = code()
    leaf = body(text, 'leaf_of')
    assert 'CC_SHA256(CFDataGetBytePtr(der), (CC_LONG)CFDataGetLength(der), leaf->sha256)' in leaf
    assert 'unsigned char sha256[CC_SHA256_DIGEST_LENGTH];' in text
    assert 'memcmp(a->sha256, b->sha256, sizeof a->sha256) == 0' in body(text, 'same_leaf')
    # The requirement language's H"" stays (it binds the running guest's leaf), with the SHA-256 compared besides.
    assert 'certificate leaf = H\\"%s\\"' in body(text, 'requirement_for')


def test_every_check_runs_before_the_request_and_the_parent_is_read_again_last():
    text = code()
    allowed = body(text, 'caller_allowed')
    order = [allowed.index(step) for step in (
        'pid_t parent = getppid();', 'own_leaf(&own)', 'parent_app(parent, bundle)',
        'SecCodeCheckValidity(guest, kSecCSDefaultFlags, requirement)', 'leaf_of(guest, &caller)',
        'same_leaf(&caller, &own)', 'app_intact(bundle, &own)', 'getppid() == parent')]
    assert order == sorted(order)
    assert text.count('getppid()') == 2  # test_vault_consolidated_22 pins the same count
    main = body(text, 'main')
    assert main.index('caller_allowed()') < main.index('malloc(MAX_REQUEST + 2)')  # ping is checked too
    assert 'return fail("caller_refused", 0, EXIT_REFUSED);' in main


def compilers():
    """A local clang and macOS SDK, called directly: never through xcrun, which may offer to install tools."""
    pairs = (('/Library/Developer/CommandLineTools/usr/bin/clang', '/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk'),
             ('/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang',
              '/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk'))
    return [pair for pair in pairs if os.access(pair[0], os.X_OK) and os.path.isdir(pair[1])]


@pytest.mark.skipif(not compilers(), reason='needs a local clang and macOS SDK')
def test_the_helper_source_compiles_with_the_pinned_flags(tmp_path):
    clang, sdk = compilers()[0]
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'TMPDIR': str(tmp_path) + '/', 'LANG': 'C'}
    done = subprocess.run([clang, *bvh.CFLAGS, '-isysroot', sdk, '-fsyntax-only', str(SOURCE)], cwd=tmp_path, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    assert done.returncode == 0, done.stderr.decode(errors='replace')[-4000:]


def pinned(capsys, tmp_path):
    root, home = tb.make_checkout(tmp_path)
    code, report = tb.call(capsys, root, home, '--execute', '--i-am-the-parent-with-user-approval', tb.STAMP)
    assert code == 0, report
    folder = root / 'src-tauri/resources/helpers'
    return root, home, folder, {name: (folder / name).read_bytes() for name in ('pebble-vault', 'pebble-vault.json')}


def test_a_replacement_needs_its_own_approval_stamp(tmp_path, capsys):
    root, home, folder, before = pinned(capsys, tmp_path)
    tools = tb.FakeTools()
    code, report = tb.call(capsys, root, home, '--execute', '--replace-pinned-helper', tools=tools)
    assert code == 2 and report['refused'] == 'approval_required' and tools.calls == []
    fresh = ('--work', str(tmp_path / 'fresh-work'))  # a new work folder: the reused stamp is the only problem
    code, report = tb.call(capsys, root, home, '--execute', '--replace-pinned-helper',
                           '--i-am-the-parent-with-user-approval', tb.STAMP, *fresh, tools=tools)
    assert code == 3 and report['refused'] == 'replacement_needs_new_approval' and tools.calls == []
    code, plan = tb.call(capsys, root, home, '--replace-pinned-helper', '--i-am-the-parent-with-user-approval', tb.STAMP,
                         *fresh, tools=tools)
    assert code == 3 and [problem['refused'] for problem in plan['problems']] == ['replacement_needs_new_approval']
    assert not (tmp_path / 'fresh-work').exists()
    assert {name: (folder / name).read_bytes() for name in before} == before and tools.calls == []


def test_a_replacement_records_what_it_replaces(tmp_path, capsys):
    root, home, folder, before = pinned(capsys, tmp_path)
    old = json.loads(before['pebble-vault.json'])
    code, report = tb.call(capsys, root, home, '--execute', '--replace-pinned-helper',
                           '--i-am-the-parent-with-user-approval', '20261022T090000Z')
    assert code == 0, report
    manifest = json.loads((folder / 'pebble-vault.json').read_text())
    assert manifest['approved_stamp'] == '20261022T090000Z' and manifest['replaces'] == {
        'sha256': old['sha256'], 'cdhash': old['cdhash'], 'approved_stamp': tb.STAMP}
    assert signing.check_pinned_helper(root, signing.load_identity(home)) == manifest
    replaced = root / '.runtime/vault-helper-20261022T090000Z/replaced'
    assert {name: (replaced / name).read_bytes() for name in before} == before  # moved aside, never deleted
    assert 'replaces' not in old  # a first pin replaces nothing
