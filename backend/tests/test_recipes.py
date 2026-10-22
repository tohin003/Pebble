"""Project recipes: registry, detection, pinned identity, runner and failure semantics.

Fixture projects, toolchain copies and fake environments live under tmp_path.
Sandboxed runs use the real macOS sandbox and the toolchains on this Mac; a
missing toolchain fails loudly instead of skipping.
"""

import dataclasses
import hashlib
import json
import os
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from pebble import recipes, testing
from pebble.policy import PolicyError, Workspace
from pebble.testing import run_tests, snapshot_manifest

macos = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox')
FRAMEWORK_PY = recipes.PYTHONS[0]
FAKE_UNITTEST = '\n' + '-' * 70 + '\nRan 5 tests in 0.001s\n\nOK\n'
UNIT_PASS = ('import unittest\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1 + 1, 2)\n\n'
             '    def test_two(self):\n        self.assertTrue(True)\n')
UNIT_FAIL = ('import unittest\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1 + 1, 2)\n\n'
             '    def test_bad(self):\n        self.assertEqual(1, 2)\n')
NODE_PASS = "const test = require('node:test');\nconst assert = require('node:assert/strict');\ntest('adds', () => { assert.equal(1 + 1, 2); });\n"
NODE_FAIL = "const test = require('node:test');\nconst assert = require('node:assert/strict');\ntest('adds', () => { assert.equal(1 + 1, 3); });\n"
GO_MOD = 'module example.com/fixture\n\ngo 1.21\n'
GO_ADD = 'package fixture\n\nfunc Add(a, b int) int { return a + b }\n'


def make_project(tmp_path, files, name='project'):
    root = tmp_path / name
    root.mkdir()
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
    return root


def payload_for(ws, recipe, mount=None, plan=True):
    payload = {'root': str(ws.root), 'root_identity': list(ws.identity), 'recipe': recipe, 'drafts': list(ws.drafts.values())}
    if plan:
        payload['plan'] = recipes.resolve(recipe, ws, mount)
        payload['recipe'] = payload['plan']['recipe']
        if mount is not None:
            payload['mount'] = mount
    return payload


def run(target, recipe, mount=None, plan=True, cancel=None):
    ws = target if isinstance(target, Workspace) else Workspace(target)
    return run_tests(payload_for(ws, recipe, mount, plan), cancel or threading.Event())


def counts(passed=0, failed=0, skipped=0, errors=0, total=None):
    return {'passed': passed, 'failed': failed, 'skipped': skipped, 'errors': errors,
            'total': passed + failed + skipped + errors if total is None else total}


def require_pytest():
    real = FRAMEWORK_PY.resolve()
    assert recipes._has_pytest(real.parent.parent, real), 'The framework Python must provide pytest for these checks.'


def require(path):
    assert Path(path).exists(), f'Required toolchain {path} is missing on this machine.'


def toolchain_copy(tmp_path, source, name):
    folder = tmp_path / 'toolchain' / 'bin'
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / name
    shutil.copyfile(Path(source).resolve(), exe)
    os.chmod(exe, 0o755)
    return exe


# Registry and detection (no execution).

def test_registry_contract():
    expected = {'python-unittest', 'python-pytest', 'node-test', 'npm-test', 'npm-build', 'npm-lint', 'npm-typecheck',
                'go-test', 'go-build', 'cargo-test', 'cargo-build', 'make-test'}
    assert expected <= set(recipes.REGISTRY)
    for recipe in recipes.REGISTRY.values():
        assert recipe.kind in ('test', 'build', 'lint') and 0 < recipe.timeout <= 300
        assert recipe.output_limit <= 192_000 and recipe.network is False and callable(recipe.detect)
        assert recipe.needs in (None, 'node_modules', 'python-env')
    assert recipes.REGISTRY['python-unittest'].argv == ('-I', '-m', 'unittest', 'discover', '-v')
    assert recipes.REGISTRY['python-pytest'].argv == ('-I', '-m', 'pytest', '-q', '-p', 'no:cacheprovider')
    assert recipes.REGISTRY['node-test'].argv == ('--test',)
    assert recipes.REGISTRY['npm-typecheck'].kind == 'lint' and recipes.REGISTRY['npm-build'].kind == 'build'
    go = dict(recipes.REGISTRY['go-test'].env)
    assert {k: go[k] for k in ('GOFLAGS', 'GOPROXY', 'GOTOOLCHAIN', 'GOWORK')} == {
        'GOFLAGS': '-mod=readonly', 'GOPROXY': 'off', 'GOTOOLCHAIN': 'local', 'GOWORK': 'off'}
    assert go['GOCACHE'].startswith('{temp}/') and go['GOPATH'].startswith('{temp}/')
    assert recipes.REGISTRY['cargo-test'].argv == ('test', '--offline', '--locked')
    assert dict(recipes.REGISTRY['cargo-test'].env)['CARGO_TARGET_DIR'].startswith('{temp}/')
    assert recipes.REGISTRY['make-test'].argv == ('-f', 'Makefile', 'test')
    assert recipes.AUTO == ('python-pytest', 'python-unittest', 'npm-test', 'node-test', 'go-test', 'cargo-test', 'make-test')
    with pytest.raises(ValueError):
        recipes._recipe('bad', 'deploy', 'x', 'make', (), detect=lambda f: None)
    with pytest.raises(ValueError):
        recipes._recipe('slow', 'test', 'x', 'make', (), timeout=301, detect=lambda f: None)


@macos
def test_inspect_reports_detected_recipes_without_authority(tmp_path):
    root = make_project(tmp_path, {
        'package.json': json.dumps({'scripts': {'test': 'node --test', 'build': 'node build.js', 'lint': 'eslint .'}}),
        'pyproject.toml': '[project]\nname = "x"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n',
        'test_calc.py': UNIT_PASS, 'math.test.js': NODE_PASS, 'go.mod': GO_MOD, 'add_test.go': 'package fixture\n',
        'Cargo.toml': '[package]\nname = "x"\n', 'Makefile': '.PHONY: test\ntest:\n\techo ok\n'})
    report = recipes.inspect(Workspace(root))
    assert report['authorization'] == 'none' and report['inventory_truncated'] is False
    rows = {row['id']: row for row in report['recipes']}
    assert rows['python-pytest']['reason'] == 'pyproject.toml [tool.pytest]'
    assert rows['python-unittest']['reason'] == 'test*.py files'
    assert rows['npm-test']['script'] == 'node --test' and rows['npm-build']['script'] == 'node build.js'
    assert rows['npm-lint']['available'] is True and 'npm-typecheck' not in rows
    assert rows['node-test']['available'] is True
    assert rows['go-test']['reason'] == 'go.mod with *_test.go files' and rows['go-build']['available'] is True
    assert rows['cargo-test']['available'] is False and rows['cargo-test']['issue']
    assert rows['make-test']['reason'] == 'Makefile test: target'
    assert all(set(row) <= {'id', 'kind', 'reason', 'available', 'issue', 'script'} for row in report['recipes'])
    # Staged drafts are detected exactly as the snapshot will contain them.
    bare = make_project(tmp_path, {'calc.py': 'X = 1\n'}, 'bare')
    ws = Workspace(bare)
    assert recipes.inspect(ws)['recipes'] == []
    ws.draft('test_calc.py', UNIT_PASS)
    ws.draft('package.json', json.dumps({'scripts': {'lint': 'node lint.js'}}))
    rows = {row['id']: row for row in recipes.inspect(ws)['recipes']}
    assert set(rows) == {'python-unittest', 'python-pytest', 'npm-lint'} and rows['npm-lint']['script'] == 'node lint.js'


def test_detection_is_a_pure_function_of_bounded_facts():
    base = {'files': (), 'truncated': False, 'texts': {}, 'issues': {}, 'present': set(), 'package': None, 'package_issue': None}
    detect = lambda rid, **kw: recipes.REGISTRY[rid].detect({**base, **kw})
    assert detect('python-pytest', files=('pkg/conftest.py',)) == 'conftest.py'
    assert detect('python-pytest', present={'pytest.ini'}) == 'pytest.ini'
    assert detect('python-pytest', texts={'setup.cfg': '[tool:pytest]\n'}) == 'setup.cfg [tool:pytest]'
    assert detect('python-pytest', files=('src/calc_test.py',)) == 'test_*.py files'
    assert detect('python-pytest', files=('calc.py',)) is None
    assert detect('node-test', files=('test/unit/a.mjs',)) and detect('node-test', files=('lib/a-test.cjs',))
    assert detect('node-test', files=('lib/contest.js',)) is None
    assert detect('make-test', texts={'Makefile': 'test := 1\n'}) is None
    assert detect('make-test', texts={'Makefile': 'unit test:\n'}) is None
    assert detect('make-test', texts={'Makefile': 'test::\n\ttrue\n'}) == 'Makefile test: target'
    assert detect('go-test', texts={'go.mod': 'go 1.21\n'}, files=('a_test.go',)) is None
    assert detect('cargo-test', texts={'Cargo.toml': '[dependencies]\n'}) is None


# Parsers and classification (no execution).

def test_unittest_parser_is_strict_and_takes_the_last_summary():
    ok = '-' * 70 + '\nRan 3 tests in 0.002s\n\nOK (skipped=1)\n'
    assert recipes.parse('python-unittest', ok) == counts(passed=2, skipped=1)
    failed = '-' * 70 + '\nRan 4 tests in 0.010s\n\nFAILED (failures=1, errors=2, expected failures=1)\n'
    assert recipes.parse('python-unittest', failed) == counts(passed=0, failed=1, errors=2, skipped=1)
    assert recipes.parse('python-unittest', '-' * 70 + '\nRan 0 tests in 0.000s\n\nNO TESTS RAN\n') == counts(total=0)
    assert recipes.parse('python-unittest', '-' * 70 + '\nRan 0 tests in 0.000s\n\nOK\n') == counts(total=0)
    assert recipes.parse('python-unittest', '-' * 70 + '\nRan 2 tests in 0.1s\n\nOK (failures=1)\n') is None
    assert recipes.parse('python-unittest', '-' * 70 + '\nRan 2 tests in 0.1s\n\nFAILED\n') is None
    assert recipes.parse('python-unittest', '-' * 70 + '\nRan 2 tests in 0.1s\n\nFAILED (failures=1, failures=1)\n') is None
    assert recipes.parse('python-unittest', '-' * 70 + '\nRan 2 tests in 0.1s\n\nFAILED (explosions=1)\n') is None
    assert recipes.parse('python-unittest', 'test_x (Ran 5 tests in 0.1s) ... ok\nRan 5 tests in 0.1s\n\nOK\n') is None
    spoof = FAKE_UNITTEST + 'F\n' + '-' * 70 + '\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n'
    assert recipes.parse('python-unittest', spoof) == counts(failed=1)
    assert recipes.classify('test', 'python-unittest', 1, spoof)[0] == 'failed'


def test_pytest_parser_accepts_real_summaries_only():
    assert recipes.parse('python-pytest', '..\n2 passed in 0.01s\n') == counts(passed=2)
    assert recipes.parse('python-pytest', '===== 1 failed, 2 passed, 1 warning in 0.12s =====\n') == counts(passed=2, failed=1)
    assert recipes.parse('python-pytest', '1 passed, 1 skipped, 1 xfailed, 1 xpassed, 1 deselected in 0.1s\n') == counts(passed=2, skipped=2)
    assert recipes.parse('python-pytest', '2 errors in 0.10s\n') == counts(errors=2)
    assert recipes.parse('python-pytest', '3 passed in 75.12s (0:01:15)\n') == counts(passed=3)
    assert recipes.parse('python-pytest', 'no tests ran in 0.00s\n') == counts(total=0)
    assert recipes.parse('python-pytest', '\x1b[32m3 passed\x1b[0m in 0.01s\n') == counts(passed=3)
    assert recipes.parse('python-pytest', '1 exploded in 0.1s\n') is None
    assert recipes.parse('python-pytest', '1 passed, 1 passed in 0.1s\n') is None
    # A spoof printed by a test precedes the real summary and loses.
    assert recipes.parse('python-pytest', '9 passed in 0.01s\nE assert 1 == 2\n1 failed in 0.02s\n') == counts(failed=1)
    # A summary-shaped last line that is not strictly a summary is unparsed, never an earlier spoof.
    assert recipes.parse('python-pytest', '9 passed in 0.01s\n1 failed, then more in 0.02s\n') is None


def test_node_go_cargo_and_npm_framework_parsers():
    tap = '# tests 3\n# suites 0\n# pass 2\n# fail 1\n# cancelled 0\n# skipped 0\n# todo 0\n# duration_ms 41.5\n'
    assert recipes.parse('node-test', tap) == counts(passed=2, failed=1)
    spec = 'ℹ tests 1\nℹ suites 0\nℹ pass 1\nℹ fail 0\nℹ cancelled 0\nℹ skipped 0\nℹ todo 0\nℹ duration_ms 5\n'
    assert recipes.parse('node-test', spec) == counts(passed=1)
    assert recipes.parse('node-test', '# tests 2\n# pass 2\n') is None
    go = ('=== RUN   TestAdd\n--- PASS: TestAdd (0.00s)\n=== RUN   TestSub\n    --- PASS: TestSub/x (0.00s)\n--- FAIL: TestSub (0.00s)\n'
          'FAIL\nFAIL\texample.com/m\t0.003s\n?   \texample.com/m/sub\t[no test files]\nFAIL\n')
    assert recipes.parse('go-test', go) == counts(passed=1, failed=1)
    assert recipes.classify('test', 'go-test', 0, '?   \texample.com/m\t[no test files]\n')[0] == 'no_tests'
    assert recipes.classify('test', 'go-test', 1, 'FAIL\texample.com/m [build failed]\nFAIL\n') == ('failed', counts(errors=1, total=0))
    assert recipes.classify('test', 'go-test', 0, 'ok  \texample.com/m\t0.002s\n--- PASS: TestA (0.00s)\n')[0] == 'passed'
    cargo = ('running 2 tests\ntest a ... ok\ntest b ... ok\n\ntest result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n'
             'test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out\n')
    assert recipes.parse('cargo-test', cargo) == counts(passed=3, failed=1, skipped=1)
    assert recipes.parse('cargo-test', 'test result: ok. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n') is None
    assert recipes.classify('test', 'cargo-test', 0, 'test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n')[0] == 'no_tests'
    jest = 'Test Suites: 1 failed, 1 passed, 2 total\nTests:       1 failed, 1 skipped, 3 passed, 5 total\n'
    assert recipes.parse('npm-test', jest) == counts(passed=3, failed=1, skipped=1, total=5)
    assert recipes.parse('npm-test', '  4 passing (12ms)\n  1 pending\n  2 failing\n') == counts(passed=4, failed=2, skipped=1)
    assert recipes.parse('npm-test', ' Tests  1 failed | 2 passed (3)\n') == counts(passed=2, failed=1)
    # Generic recipes take the summary that ends last.
    mixed = '-' * 70 + '\nRan 9 tests in 0.1s\n\nOK\n' + tap
    assert recipes.parse('make-test', mixed) == counts(passed=2, failed=1)


def test_classification_never_passes_without_clean_counted_evidence():
    ok = '2 passed in 0.01s\n'
    assert recipes.classify('test', 'python-pytest', 0, ok) == ('passed', counts(passed=2))
    assert recipes.classify('test', 'python-pytest', 1, ok)[0] == 'error'
    assert recipes.classify('test', 'python-pytest', 5, 'no tests ran in 0.00s\n')[0] == 'no_tests'
    assert recipes.classify('test', 'python-pytest', 0, '2 skipped in 0.01s\n')[0] == 'no_tests'
    assert recipes.classify('test', 'python-pytest', 0, 'all good\n') == ('error', None)
    assert recipes.classify('test', 'npm-test', 0, 'Error: no test specified\n') == ('error', None)
    assert recipes.classify('test', 'python-unittest', 0, FAKE_UNITTEST)[0] == 'passed'  # only with exit 0 and no later summary
    assert recipes.classify('test', 'python-unittest', 1, FAKE_UNITTEST)[0] == 'error'
    assert recipes.classify('build', 'npm-build', 0, '') == ('passed', None)
    assert recipes.classify('lint', 'npm-lint', 2, '2 passed in 0.01s\n') == ('failed', None)


# Pinned identity and confinement (no execution).

def test_executable_identity_drift_is_refused(tmp_path, monkeypatch):
    exe = toolchain_copy(tmp_path, FRAMEWORK_PY, 'python3.14')
    monkeypatch.setattr(recipes, 'PYTHONS', (exe,))
    root = make_project(tmp_path, {'test_a.py': UNIT_PASS})
    ws = Workspace(root)
    plan = recipes.resolve('python-unittest', ws)
    assert plan['executable']['realpath'] == str(exe.resolve()) and plan['argv'][0] == str(exe.resolve())
    assert plan['toolchain_reads'] == [str((tmp_path / 'toolchain').resolve())]
    assert plan['executable']['sha256'] == hashlib.sha256(exe.read_bytes()).hexdigest()
    assert recipes.verify(plan, ws.root) is plan
    with exe.open('ab') as handle:
        handle.write(b'\0')
    with pytest.raises(PolicyError, match=r'changed after review \(sha256, size, mtime_ns\)'):
        recipes.verify(plan)
    with pytest.raises(PolicyError, match='changed after review'):
        run_tests({'root': str(root), 'root_identity': list(ws.identity), 'recipe': 'python-unittest', 'drafts': [], 'plan': plan},
                  threading.Event())
    plan = recipes.resolve('python-unittest', ws)
    replacement = exe.with_name('replacement')
    shutil.copy2(exe, replacement)
    os.replace(replacement, exe)
    with pytest.raises(PolicyError, match=r'changed after review \(ino\)'):
        recipes.verify(plan)
    plan = recipes.resolve('python-unittest', ws)
    stamp = exe.stat()
    os.utime(exe, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
    with pytest.raises(PolicyError, match=r'changed after review \(mtime_ns\)'):
        recipes.verify(plan)
    plan = recipes.resolve('python-unittest', ws)
    os.chmod(exe, 0o750)
    with pytest.raises(PolicyError, match=r'changed after review \(mode\)'):
        recipes.verify(plan)
    os.chmod(exe, 0o775)
    with pytest.raises(PolicyError, match='group-writable'):
        recipes.verify(plan)
    os.chmod(exe, 0o757)
    with pytest.raises(PolicyError, match='world-writable'):
        recipes.resolve('python-unittest', ws)
    os.chmod(exe, 0o644)
    with pytest.raises(PolicyError, match='not executable'):
        recipes.resolve('python-unittest', ws)


def test_toolchains_are_confined(tmp_path, monkeypatch):
    root = make_project(tmp_path, {'test_a.py': UNIT_PASS, 'bin/python3.14': b''})
    for folder in (Path.home(), Path('/'), Path.home().parent):
        with pytest.raises(PolicyError, match='confined development runtime'):
            recipes._confined(folder, root)
    with pytest.raises(PolicyError, match='overlapping the selected project'):
        recipes._confined(root / 'bin', root)
    with pytest.raises(PolicyError, match='overlapping the selected project'):
        recipes._confined(tmp_path, root)
    inside = root / 'bin/python3.14'
    shutil.copy2(FRAMEWORK_PY.resolve(), inside)
    os.chmod(inside, 0o755)
    monkeypatch.setattr(recipes, 'PYTHONS', (inside,))
    with pytest.raises(PolicyError, match='inside the selected project'):
        recipes.resolve('python-unittest', Workspace(root))
    with pytest.raises(PolicyError, match='inside the selected project'):
        recipes.pin(inside, Workspace(root).root)
    with pytest.raises(PolicyError, match='regular file'):
        recipes.pin(tmp_path)
    with pytest.raises(PolicyError, match='plain absolute path'):
        recipes.pin('relative/python')
    with pytest.raises(PolicyError, match='plain absolute path'):
        recipes.pin('/tmp/with"quote')
    link = tmp_path / 'linked-python'
    link.symlink_to(FRAMEWORK_PY)
    pinned = recipes.pin(link)
    assert pinned['path'] == str(link) and pinned['realpath'] == str(FRAMEWORK_PY.resolve())


def test_framework_python_group_write_bit_is_accepted_only_for_root_admin(tmp_path):
    real = FRAMEWORK_PY.resolve()
    require(real)
    info = real.stat()
    pinned = recipes.pin(FRAMEWORK_PY)
    assert pinned['uid'] == info.st_uid and pinned['mode'] == info.st_mode
    if info.st_mode & stat.S_IWGRP:
        assert info.st_uid == 0 and info.st_gid in recipes.TRUSTED_WRITER_GROUPS
    own = toolchain_copy(tmp_path, FRAMEWORK_PY, 'python3.14')
    os.chmod(own, 0o775)
    with pytest.raises(PolicyError, match='group-writable'):
        recipes.pin(own)


@macos
def test_cargo_recipes_resolve_offline_but_are_unavailable_here(tmp_path, monkeypatch):
    fake_home = tmp_path / 'home'
    fake_home.mkdir()
    monkeypatch.setenv('HOME', str(fake_home))
    monkeypatch.setattr(recipes, 'CARGOS', (tmp_path / 'missing/cargo',))
    root = make_project(tmp_path, {'Cargo.toml': '[package]\nname = "fixture"\nversion = "0.1.0"\n', 'src/lib.rs': ''})
    rows = {row['id']: row for row in recipes.inspect(Workspace(root))['recipes']}
    assert rows['cargo-test']['issue'] == 'Cargo.lock is required for an offline locked Cargo build.'
    (root / 'Cargo.lock').write_text('version = 3\n')
    rows = {row['id']: row for row in recipes.inspect(Workspace(root))['recipes']}
    assert rows['cargo-test'] == {'id': 'cargo-test', 'kind': 'test', 'reason': 'Cargo.toml', 'available': False,
                                  'issue': 'Install Rust (cargo) to run Cargo recipes.'}
    assert rows['cargo-build']['available'] is False
    with pytest.raises(PolicyError, match='Install Rust'):
        recipes.resolve('cargo-test', Workspace(root))
    with pytest.raises(PolicyError, match='Install Rust'):
        recipes.resolve('cargo-build', Workspace(root))
    # The real resolver on this Mac also finds no cargo.
    monkeypatch.undo()
    if not any(p.exists() for p in recipes.CARGOS) and not list((Path.home() / '.rustup/toolchains').glob('*/bin/cargo')):
        with pytest.raises(PolicyError, match='Install Rust'):
            recipes.resolve('cargo-test', Workspace(root))
    # A stand-in binary (never run) shows the offline, locked, temp-target plan shape.
    fake = toolchain_copy(tmp_path, '/usr/bin/true', 'cargo')
    monkeypatch.setenv('HOME', str(fake_home))
    monkeypatch.setattr(recipes, 'CARGOS', (fake,))
    plan = recipes.resolve('cargo-test', Workspace(root))
    assert plan['argv'] == [str(fake.resolve()), 'test', '--offline', '--locked']
    assert plan['env']['CARGO_TARGET_DIR'] == '{temp}/cargo-target' and plan['env']['CARGO_HOME'] == '{temp}/cargo-home'
    assert plan['env']['CARGO_NET_OFFLINE'] == 'true' and plan['network'] is False


@macos
def test_auto_resolution_is_deterministic(tmp_path, monkeypatch):
    require_pytest()
    everything = make_project(tmp_path, {
        'test_calc.py': UNIT_PASS, 'package.json': json.dumps({'scripts': {'test': 'node --test'}}), 'math.test.js': NODE_PASS,
        'go.mod': GO_MOD, 'add_test.go': 'package fixture\n', 'Makefile': 'test:\n\ttrue\n'}, 'everything')
    assert [recipes.resolve('auto', Workspace(everything))['recipe'] for _ in range(2)] == ['python-pytest'] * 2
    monkeypatch.setattr(recipes, '_has_pytest', lambda prefix, real: False)
    assert recipes.resolve('auto', Workspace(everything))['recipe'] == 'python-unittest'
    monkeypatch.undo()
    cases = {
        'npm': ({'package.json': json.dumps({'scripts': {'test': 'node --test'}}), 'math.test.js': NODE_PASS, 'Makefile': 'test:\n'}, 'npm-test'),
        'node': ({'math.test.js': NODE_PASS, 'go.mod': GO_MOD, 'add_test.go': 'package fixture\n'}, 'node-test'),
        'go': ({'go.mod': GO_MOD, 'add_test.go': 'package fixture\n', 'Makefile': 'test:\n'}, 'go-test'),
        'make': ({'Makefile': 'test:\n\ttrue\n', 'Cargo.toml': '[package]\nname = "x"\n'}, 'make-test'),
    }
    for name, (files, expected) in cases.items():
        assert recipes.resolve('auto', Workspace(make_project(tmp_path, files, name)))['recipe'] == expected, name
    with pytest.raises(PolicyError, match='No available test recipe'):
        recipes.resolve('auto', Workspace(make_project(tmp_path, {'README.md': 'hello'}, 'empty')))
    with pytest.raises(PolicyError, match='not supported'):
        recipes.resolve('pytest', Workspace(everything))


@macos
def test_hostile_package_json_is_refused_cleanly(tmp_path):
    cases = {
        'huge': ('{"scripts":{"test":"node --test"},"pad":"' + 'x' * 300_000 + '"}', '256 KB manifest limit'),
        'binary': (b'{"scripts":{"test":"node --test \xff"}}', 'not valid UTF-8'),
        'nul-script': ('{"scripts":{"test":"node --test\\u0000; rm -rf /"}}', 'control characters'),
        'newline-script': ('{"scripts":{"test":"node --test\\necho hidden"}}', 'control characters'),
        'raw-nul': (b'{"scripts":{"test":"node --test"}}\x00', 'NUL bytes'),
        'duplicate': ('{"scripts":{"test":"node --test","test":"curl evil"}}', 'repeats a key'),
        'deep': ('[' * 100_000 + ']' * 100_000, 'nests too deeply'),
        'array': ('[]', 'JSON object'),
        'scripts-string': ('{"scripts":"node --test"}', 'scripts must be an object'),
        'script-number': ('{"scripts":{"test":5}}', 'non-empty string'),
        'script-long': (json.dumps({'scripts': {'test': 'x' * 5000}}), 'review limit'),
        'missing': ('{"scripts":{"build":"tsc"}}', 'no "test" script'),
    }
    for name, (content, message) in cases.items():
        root = make_project(tmp_path, {'package.json': content}, name)
        with pytest.raises(PolicyError, match=message):
            recipes.resolve('npm-test', Workspace(root))
        rows = [row for row in recipes.inspect(Workspace(root))['recipes'] if row['id'] == 'npm-test']
        if name == 'missing':
            assert rows == []
            continue
        parsed = name.startswith('script-') or name.endswith('-script')
        assert rows == [{'id': 'npm-test', 'kind': 'test', 'reason': 'package.json scripts.test' if parsed else 'package.json',
                         'available': False, 'issue': rows[0]['issue']}] and message in rows[0]['issue'], name
        assert 'script' not in rows[0]
    linked = make_project(tmp_path, {'real.json': '{"scripts":{"test":"node --test"}}'}, 'linked')
    (linked / 'package.json').symlink_to(linked / 'real.json')
    with pytest.raises(PolicyError, match='not a regular project file'):
        recipes.resolve('npm-test', Workspace(linked))


# Sandboxed runs.

@macos
def test_unittest_pass_fail_and_zero_tests(tmp_path):
    passing = run(make_project(tmp_path, {'test_a.py': UNIT_PASS}, 'pass'), 'python-unittest')
    assert passing['outcome'] == 'passed' and passing['exit_code'] == 0, passing['output']
    assert passing['tests'] == counts(passed=2) and passing['pinned'] is True and passing['kind'] == 'test'
    assert passing['executable_sha256'] == recipes.pin(FRAMEWORK_PY)['sha256']
    assert passing['isolation'] == testing.ISOLATION and passing['output_truncated'] is False
    assert passing['output_bytes'] == len(passing['output'].encode()) and passing['environment'] is None
    failing = run(make_project(tmp_path, {'test_a.py': UNIT_FAIL}, 'fail'), 'python-unittest')
    assert failing['outcome'] == 'failed' and failing['exit_code'] != 0 and failing['tests'] == counts(passed=1, failed=1)
    empty = run(make_project(tmp_path, {'calc.py': 'X = 1\n'}, 'empty'), 'python-unittest')
    assert empty['outcome'] == 'no_tests' and empty['tests'] == counts(total=0), empty['output']


@macos
def test_pytest_pass_fail_and_no_tests(tmp_path):
    require_pytest()
    passing = run(make_project(tmp_path, {'test_a.py': 'def test_one():\n    assert 1 + 1 == 2\n\ndef test_two():\n    assert True\n'}, 'pass'), 'python-pytest')
    assert passing['outcome'] == 'passed' and passing['tests'] == counts(passed=2), passing['output']
    assert passing['executable_sha256'] == recipes.pin(FRAMEWORK_PY)['sha256']
    failing = run(make_project(tmp_path, {'test_a.py': 'def test_one():\n    assert True\n\ndef test_bad():\n    assert 1 == 2\n'}, 'fail'), 'python-pytest')
    assert failing['outcome'] == 'failed' and failing['exit_code'] == 1 and failing['tests'] == counts(passed=1, failed=1)
    empty = run(make_project(tmp_path, {'pytest.ini': '[pytest]\n', 'calc.py': 'X = 1\n'}, 'empty'), 'python-pytest')
    assert empty['exit_code'] == 5 and empty['outcome'] == 'no_tests', empty['output']


@macos
def test_node_test_pass_and_fail(tmp_path):
    passing = run(make_project(tmp_path, {'math.test.js': NODE_PASS}, 'pass'), 'node-test')
    assert passing['outcome'] == 'passed' and passing['tests'] == counts(passed=1), passing['output']
    failing = run(make_project(tmp_path, {'math.test.js': NODE_FAIL}, 'fail'), 'node-test')
    assert failing['outcome'] == 'failed' and failing['exit_code'] != 0 and failing['tests'] == counts(failed=1)


@macos
def test_npm_test_records_the_script_and_skips_lifecycle_hooks(tmp_path):
    package = {'name': 'fixture', 'version': '1.0.0', 'scripts': {
        'pretest': "node -e \"console.log('PRE-HOOK-RAN')\"", 'test': 'node --test',
        'posttest': "node -e \"console.log('POST-HOOK-RAN')\""}}
    root = make_project(tmp_path, {'package.json': json.dumps(package), 'math.test.js': NODE_PASS}, 'pass')
    plan = recipes.resolve('npm-test', Workspace(root))
    assert plan['script'] == 'node --test' and plan['argv'][2:] == ['--ignore-scripts', 'run', 'test']
    assert plan['argv'][1].endswith('/lib/node_modules/npm/bin/npm-cli.js') and plan['entrypoint']['realpath'] == plan['argv'][1]
    assert plan['env']['npm_config_ignore_scripts'] == 'true' and plan['env']['npm_config_globalconfig'] == '{temp}/npmrc-global'
    passing = run(root, 'npm-test')
    assert passing['outcome'] == 'passed' and passing['tests'] == counts(passed=1), passing['output']
    assert 'node --test' in passing['output']
    assert 'PRE-HOOK-RAN' not in passing['output'] and 'POST-HOOK-RAN' not in passing['output']
    package['scripts']['test'] = 'node --test'
    failing_root = make_project(tmp_path, {'package.json': json.dumps(package), 'math.test.js': NODE_FAIL}, 'fail')
    failing = run(failing_root, 'npm-test')
    assert failing['outcome'] == 'failed' and failing['exit_code'] != 0 and failing['tests'] == counts(failed=1)
    assert 'PRE-HOOK-RAN' not in failing['output'] and 'POST-HOOK-RAN' not in failing['output']
    missing = make_project(tmp_path, {'package.json': json.dumps({'scripts': {'build': 'tsc'}})}, 'missing')
    with pytest.raises(PolicyError, match='no "test" script'):
        recipes.resolve('npm-test', Workspace(missing))
    with pytest.raises(PolicyError, match='needs a plan pinned'):
        run(missing, 'npm-test', plan=False)


@macos
def test_npm_build_and_lint_pass_on_exit_status_only(tmp_path):
    root = make_project(tmp_path, {'package.json': json.dumps({'scripts': {
        'build': "node -e \"console.log('1 failed in 0.1s')\"", 'lint': 'node -e "process.exit(3)"'}})})
    built = run(root, 'npm-build')
    assert built['outcome'] == 'passed' and built['tests'] is None and built['kind'] == 'build', built['output']
    linted = run(root, 'npm-lint')
    assert linted['outcome'] == 'failed' and linted['exit_code'] != 0 and linted['kind'] == 'lint'


@macos
def test_go_test_pass_and_fail_offline(tmp_path):
    require(recipes.GOS[0])
    passing_test = 'package fixture\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) {\n\tif Add(1, 2) != 3 {\n\t\tt.Fatal("bad")\n\t}\n}\n'
    root = make_project(tmp_path, {'go.mod': GO_MOD, 'add.go': GO_ADD, 'add_test.go': passing_test}, 'pass')
    plan = recipes.resolve('go-test', Workspace(root))
    assert plan['env']['GOPROXY'] == 'off' and plan['env']['GOMODCACHE'] in ('{temp}/go-mod', str((Path.home() / 'go/pkg/mod').resolve()))
    passing = run(root, 'go-test')
    assert passing['outcome'] == 'passed' and passing['tests'] == counts(passed=1), passing['output']
    built = run(root, 'go-build')
    assert built['outcome'] == 'passed' and built['kind'] == 'build' and built['tests'] is None, built['output']
    failing = run(make_project(tmp_path, {'go.mod': GO_MOD, 'add.go': GO_ADD,
                                          'add_test.go': passing_test.replace('!= 3', '!= 4')}, 'fail'), 'go-test')
    assert failing['outcome'] == 'failed' and failing['exit_code'] != 0 and failing['tests'] == counts(failed=1), failing['output']


@macos
def test_go_module_needing_a_download_fails_offline(tmp_path):
    require(recipes.GOS[0])
    root = make_project(tmp_path, {
        'go.mod': GO_MOD + '\nrequire example.invalid/remote v1.2.3\n',
        'use.go': 'package fixture\n\nimport "example.invalid/remote"\n\nvar _ = remote.Value\n',
        'use_test.go': 'package fixture\n\nimport "testing"\n\nfunc TestUse(t *testing.T) {}\n'})
    result = run(root, 'go-test')
    assert result['outcome'] in ('error', 'failed') and result['exit_code'] != 0, result['output']
    assert 'GOPROXY=off' in result['output'] or 'missing go.sum entry' in result['output'], result['output']
    assert 'dial tcp' not in result['output'] and 'proxy.golang.org' not in result['output']


@macos
def test_make_test_runs_the_literal_target(tmp_path):
    require(recipes.MAKE)
    root = make_project(tmp_path, {'Makefile': '.PHONY: test\ntest:\n\t./check.sh\n\tpython3 -m unittest discover -v\n',
                                   'check.sh': '#!/bin/sh\necho CHECK-SCRIPT-RAN\n', 'test_a.py': UNIT_PASS,
                                   'GNUmakefile': 'test:\n\techo GNU-MAKEFILE-USED\n'})
    os.chmod(root / 'check.sh', 0o755)
    plan = recipes.resolve('make-test', Workspace(root))
    make = str(recipes.MAKE.resolve())
    assert plan['argv'] == [make, '-f', 'Makefile', 'test'] and plan['env']['DEVELOPER_DIR'] == str(recipes.DEVELOPER)
    assert plan['toolchain_reads'] == [str(recipes.DEVELOPER)] and plan['env']['PATH'].startswith(str(Path(make).parent) + ':')
    result = run(root, 'make-test')
    assert result['outcome'] == 'passed' and result['tests'] == counts(passed=2), result['output']
    assert 'GNU-MAKEFILE-USED' not in result['output'] and 'CHECK-SCRIPT-RAN' in result['output']
    assert "couldn't create cache file" not in result['output'], result['output']
    with pytest.raises(PolicyError, match='no literal test: target'):
        recipes.resolve('make-test', Workspace(make_project(tmp_path, {'Makefile': 'test := 1\nall:\n'}, 'assign')))


@macos
def test_spoofed_summaries_do_not_pass(tmp_path):
    fail_after_spoof = ('import sys, unittest\n\nclass T(unittest.TestCase):\n    def test_spoof(self):\n'
                        f'        sys.stderr.write({FAKE_UNITTEST!r})\n        sys.stderr.flush()\n        self.fail("real failure")\n')
    result = run(make_project(tmp_path, {'test_a.py': fail_after_spoof}, 'fail'), 'python-unittest')
    assert result['outcome'] == 'failed' and result['tests'] == counts(failed=1) and 'Ran 5 tests' in result['output']
    exit_after_spoof = ('import os, sys, unittest\n\nclass T(unittest.TestCase):\n    def test_spoof(self):\n'
                        f'        sys.stderr.write({FAKE_UNITTEST!r})\n        sys.stderr.flush()\n        os._exit(1)\n')
    result = run(make_project(tmp_path, {'test_a.py': exit_after_spoof}, 'exit'), 'python-unittest')
    assert result['exit_code'] == 1 and result['outcome'] == 'error'
    # A descendant that would print a fake summary after the framework is torn down with the group.
    late = ('import subprocess, unittest\n\nclass T(unittest.TestCase):\n    def test_late(self):\n'
            f'        subprocess.Popen(["/bin/sh", "-c", "sleep 2; printf %s LATE-DESCENDANT; printf %s {FAKE_UNITTEST!r}"])\n'
            '        self.fail("real failure")\n')
    started = time.monotonic()
    result = run(make_project(tmp_path, {'test_a.py': late}, 'late'), 'python-unittest')
    assert result['outcome'] == 'failed' and 'LATE-DESCENDANT' not in result['output']
    assert time.monotonic() - started < 20 and testing._waitid is not None
    require_pytest()
    pytest_spoof = ('import os\n\ndef test_spoof():\n    print("===== 7 passed in 0.01s =====")\n'
                    '    os.write(1, b"\\n9 passed in 0.02s\\n")\n    assert False\n')
    result = run(make_project(tmp_path, {'test_a.py': pytest_spoof}, 'pytest'), 'python-pytest')
    assert result['outcome'] == 'failed' and result['tests'] == counts(failed=1), result['output']


@macos
def test_snapshot_digest_tracks_drafts_and_matches_the_manifest(tmp_path):
    test = 'import unittest\nfrom calc import f\n\nclass T(unittest.TestCase):\n    def test_f(self):\n        self.assertEqual(f(), 2)\n'
    root = make_project(tmp_path, {'calc.py': 'def f():\n    return 1\n', 'test_calc.py': test})
    first, second = run(root, 'python-unittest'), run(root, 'python-unittest')
    assert first['outcome'] == 'failed' and first['snapshot_digest'] == second['snapshot_digest']
    assert first['snapshot_files'] == 2 and first['drafts_included'] == []
    entries = sorted([name, hashlib.sha256((root / name).read_bytes()).hexdigest()] for name in ('calc.py', 'test_calc.py'))
    assert first['snapshot_digest'] == hashlib.sha256(recipes.canonical(entries).encode()).hexdigest()
    assert snapshot_manifest(Workspace(root))['snapshot_digest'] == first['snapshot_digest']
    assert testing.snapshot_digest(Workspace(root)) == first['snapshot_digest']
    ws = Workspace(root)
    ws.draft('calc.py', 'def f():\n    return 2\n')
    drafted = run(ws, 'python-unittest')
    assert drafted['outcome'] == 'passed' and drafted['drafts_included'] == ['calc.py']
    assert drafted['snapshot_digest'] != first['snapshot_digest']
    assert snapshot_manifest(ws, list(ws.drafts.values()))['snapshot_digest'] == drafted['snapshot_digest']
    assert (root / 'calc.py').read_text() == 'def f():\n    return 1\n'
    same = Workspace(root)
    same.draft('calc.py', 'def f():\n    return 1\n')
    assert snapshot_manifest(same, list(same.drafts.values()))['snapshot_digest'] == first['snapshot_digest']
    (root / 'calc.py').write_text('def f():\n    return 2\n')  # the draft, applied
    assert testing.snapshot_digest(Workspace(root)) == drafted['snapshot_digest']
    (root / 'calc.py').write_text('def f():\n    return 3\n')  # a later edit: the old passing run no longer covers it
    assert testing.snapshot_digest(Workspace(root)) != drafted['snapshot_digest']


@macos
def test_large_private_and_linked_files_stay_out_of_the_snapshot(tmp_path):
    outside = tmp_path / 'outside.txt'
    outside.write_text('private')
    test = ('import unittest\nfrom pathlib import Path\n\nclass T(unittest.TestCase):\n    def test_view(self):\n'
            '        self.assertTrue(Path("small.txt").exists())\n        self.assertFalse(Path("big.bin").exists())\n'
            '        self.assertFalse(Path(".env").exists())\n        self.assertFalse(Path("link.txt").exists())\n')
    root = make_project(tmp_path, {'test_a.py': test, 'small.txt': 'x', 'big.bin': b'\0' * 2_000_001, '.env': 'TOKEN=1'})
    (root / 'link.txt').symlink_to(outside)
    result = run(root, 'python-unittest')
    assert result['outcome'] == 'passed', result['output']
    assert result['skipped_large'] == ['big.bin'] and result['skipped_large_count'] == 1 and result['snapshot_files'] == 2
    assert snapshot_manifest(Workspace(root))['skipped_large'] == ['big.bin']


def test_snapshot_inventory_and_size_bounds(tmp_path):
    many = make_project(tmp_path, {f'f{i:04d}.txt': 'x' for i in range(1501)}, 'many')
    manifest = snapshot_manifest(Workspace(many))
    assert manifest['snapshot_truncated'] is True and manifest['snapshot_files'] == 1500
    heavy = make_project(tmp_path, {f'blob{i}.bin': b'\0' * 1_900_000 for i in range(13)}, 'heavy')
    with pytest.raises(PolicyError, match='24 MB isolated test limit'):
        snapshot_manifest(Workspace(heavy))
    ws = Workspace(make_project(tmp_path, {'a.py': 'A = 1\n'}, 'drafts'))
    for bad in ({'path': '.env', 'after': 'TOKEN=1'}, {'path': '../escape.py', 'after': ''}, {'path': 'a.py'}):
        with pytest.raises(PolicyError):
            snapshot_manifest(ws, [bad])


@macos
def test_long_output_is_displayed_head_and_tail_but_parsed_whole(tmp_path):
    chatty = ('import sys, unittest\n\nclass T(unittest.TestCase):\n    def test_chatty(self):\n'
              '        sys.stdout.write("HEAD-MARK " + "y" * 50_000 + "\\n")\n        sys.stdout.flush()\n')
    result = run(make_project(tmp_path, {'test_a.py': chatty}), 'python-unittest')
    assert result['outcome'] == 'passed' and result['tests'] == counts(passed=1)
    assert result['output_truncated'] is True and len(result['output']) <= testing.DISPLAY_LIMIT
    assert result['output_bytes'] > 50_000 and 'HEAD-MARK' in result['output'][:6000]
    assert 'characters omitted' in result['output'] and result['output'].rstrip().endswith('OK')


@macos
def test_sandbox_denies_network_dns_home_and_original_writes(tmp_path):
    sentinel = tmp_path / 'secret.txt'
    sentinel.write_text('private')
    root = make_project(tmp_path, {'data.txt': 'original', 'test_net.py': (
        'import socket, unittest\n\nclass T(unittest.TestCase):\n'
        '    def test_connect(self):\n        socket.create_connection(("1.1.1.1", 443), timeout=3).close()\n')})
    result = run(root, 'python-unittest')
    assert result['outcome'] == 'failed' and result['tests'] == counts(errors=1), result['output']
    assert 'PermissionError' in result['output'] or 'Operation not permitted' in result['output']
    (root / 'test_net.py').unlink()
    (root / 'test_write.py').write_text(
        'import unittest\nfrom pathlib import Path\n\nclass T(unittest.TestCase):\n'
        f'    def test_write_original(self):\n        Path({str(root / "data.txt")!r}).write_text("corrupt")\n'
        f'    def test_write_outside(self):\n        Path({str(tmp_path / "planted.txt")!r}).write_text("x")\n'
        f'    def test_read_home(self):\n        Path({str(sentinel)!r}).read_text()\n')
    result = run(root, 'python-unittest')
    assert result['outcome'] == 'failed' and result['tests'] == counts(errors=3), result['output']
    assert (root / 'data.txt').read_text() == 'original' and not (tmp_path / 'planted.txt').exists()
    (root / 'test_write.py').unlink()
    (root / 'test_dns.py').write_text(
        'import socket, unittest\n\nclass T(unittest.TestCase):\n'
        '    def test_dns(self):\n        with self.assertRaises(OSError):\n            socket.getaddrinfo("example.com", 443)\n')
    result = run(root, 'python-unittest')
    assert result['outcome'] == 'passed', result['output']


@macos
def test_legacy_payload_without_plan(tmp_path):
    root = make_project(tmp_path, {'test_a.py': UNIT_PASS})
    result = run(root, 'python-unittest', plan=False)
    assert result['outcome'] == 'passed' and result['pinned'] is False and result['exit_code'] == 0, result['output']
    assert set(result) >= {'exit_code', 'output', 'isolation'}
    ws = Workspace(root)
    with pytest.raises(PolicyError, match='mount needs a pinned test plan'):
        run_tests({'root': str(ws.root), 'recipe': 'python-unittest', 'drafts': [], 'mount': {'kind': 'python-env'}}, threading.Event())
    with pytest.raises(PolicyError, match='not supported'):
        run_tests({'root': str(ws.root), 'recipe': 'arbitrary', 'drafts': []}, threading.Event())
    with pytest.raises(PolicyError, match='needs a plan pinned'):
        run_tests({'root': str(ws.root), 'recipe': 'go-test', 'drafts': []}, threading.Event())


@macos
def test_plan_tampering_and_stale_scripts_are_refused(tmp_path):
    package = {'scripts': {'test': 'node --test'}}
    root = make_project(tmp_path, {'package.json': json.dumps(package), 'math.test.js': NODE_PASS, 'test_a.py': UNIT_PASS})
    ws = Workspace(root)
    base = payload_for(ws, 'npm-test')

    def attempt(change, message, **extra):
        payload = json.loads(json.dumps(base))
        change(payload)
        payload.update(extra)
        with pytest.raises(PolicyError, match=message):
            run_tests(payload, threading.Event())

    # The stored (JSON round-tripped) plan runs unchanged.
    assert run_tests(json.loads(json.dumps(base)), threading.Event())['outcome'] == 'passed'
    attempt(lambda p: p['plan']['argv'].append('--watch'), r'changed after review \(argv\)')
    attempt(lambda p: p['plan']['env'].update(NODE_OPTIONS='--require /tmp/x.js'), r'changed after review \(env\)')
    attempt(lambda p: p['plan']['toolchain_reads'].append(str(tmp_path)), r'changed after review \(toolchain_reads\)')
    attempt(lambda p: p['plan'].update(script='node --test; curl evil'), r'changed after review \(script\)')
    attempt(lambda p: p['plan'].update(timeout=301), 'malformed')
    attempt(lambda p: p['plan'].update(network=True), 'malformed')
    attempt(lambda p: p['plan'].update(extra=1), 'malformed')
    attempt(lambda p: p['plan']['executable'].update(sha256='0' * 64), r'changed after review \(sha256\)')
    attempt(lambda p: p.update(recipe='node-test'), 'does not match')
    env = tmp_path / 'env'
    (env / 'node_modules').mkdir(parents=True)
    attempt(lambda p: None, r'changed after review \(mount\)',
            mount={'kind': 'node_modules', 'root': str(env), 'reads': [str(env)], 'links': {'node_modules': str(env / 'node_modules')}})
    stale = Workspace(root)
    plan = recipes.resolve('npm-test', stale)
    stale.draft('package.json', json.dumps({'scripts': {'test': 'node --test && node -e "1"'}}))
    with pytest.raises(PolicyError, match=r'changed after review \(script\)'):
        run_tests({**payload_for(stale, 'npm-test', plan=False), 'plan': plan}, threading.Event())
    # A draft staged before proposal is part of the reviewed plan.
    assert recipes.resolve('npm-test', stale)['script'] == 'node --test && node -e "1"'
    replaced = dict(base, root_identity=[0, 0])
    with pytest.raises(PolicyError, match='replaced before test execution'):
        run_tests(replaced, threading.Event())


@macos
def test_node_modules_mount_is_linked_read_only(tmp_path):
    env = tmp_path / 'env'
    modules = env / 'work/node_modules'
    (modules / 'leftpad').mkdir(parents=True)
    (modules / 'leftpad/package.json').write_text('{"name":"leftpad","main":"index.js"}')
    (modules / 'leftpad/index.js').write_text('module.exports = (s, n) => String(s).padStart(n);\n')
    test = ("const test = require('node:test');\nconst assert = require('node:assert/strict');\nconst fs = require('node:fs');\n"
            "const pad = require('leftpad');\ntest('pads', () => { assert.equal(pad('a', 3), '  a');\n"
            f"  assert.throws(() => fs.writeFileSync({str(modules / 'leftpad/index.js')!r}, 'x')); }});\n")
    root = make_project(tmp_path, {'package.json': json.dumps({'scripts': {'test': 'node --test'}}), 'pad.test.js': test})
    mount = {'kind': 'node_modules', 'root': str(env), 'reads': [str(env)], 'links': {'node_modules': str(modules)},
             'env': {}, 'environment_id': 'a' * 32, 'tree_digest': 'b' * 64}
    plan = recipes.resolve('npm-test', Workspace(root), mount)
    assert plan['mount']['links'] == {'node_modules': str(modules.resolve())} and plan['mount']['reads'] == [str(env.resolve())]
    result = run(root, 'npm-test', mount)
    assert result['outcome'] == 'passed', result['output']
    assert result['environment'] == {'kind': 'node_modules', 'environment_id': 'a' * 32, 'tree_digest': 'b' * 64}
    assert (modules / 'leftpad/index.js').read_text().startswith('module.exports')
    bare = run(root, 'npm-test')
    assert bare['outcome'] == 'failed', bare['output']
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    refusals = [
        ({**mount, 'links': {'node_modules': str(elsewhere)}}, 'inside the environment root'),
        ({**mount, 'links': {'node_modules': str(modules), '.bin': str(modules)}}, 'only provide node_modules'),
        ({**mount, 'surprise': 1}, 'managed mount fields'),
        ({**mount, 'kind': 'python-env'}, 'cannot use that managed environment'),
        ({**mount, 'env': {'PATH': '/tmp'}}, 'only VIRTUAL_ENV and NODE_PATH'),
        ({**mount, 'env': {'NODE_OPTIONS': '--require x'}}, 'only VIRTUAL_ENV and NODE_PATH'),
        ({**mount, 'reads': [str(elsewhere)]}, 'read its own root'),
        ({**mount, 'reads': [str(Path.home())]}, 'confined development runtime'),
        ({**mount, 'root': str(root), 'reads': [str(root)]}, 'overlapping the selected project'),
        ({**mount, 'executable': recipes.pin(FRAMEWORK_PY)}, 'Only a Python environment'),
        ({**mount, 'environment_id': 'x' * 200}, 'short identifiers'),
    ]
    for spec, message in refusals:
        with pytest.raises(PolicyError, match=message):
            recipes.resolve('npm-test', Workspace(root), spec)
    with pytest.raises(PolicyError, match='cannot use that managed environment'):
        recipes.resolve('python-unittest', Workspace(root), mount)
    os.makedirs(root / 'node_modules')
    ws = Workspace(root)
    ws.draft('node_modules/planted.js', 'module.exports = 1;\n')
    with pytest.raises(PolicyError, match='already contains node_modules'):
        run(ws, 'npm-test', mount)


@macos
def test_python_env_mount_uses_the_pinned_environment_interpreter(tmp_path):
    require_pytest()
    real = FRAMEWORK_PY.resolve()
    env = tmp_path / 'env'
    venv = env / 'venv'
    (venv / 'bin').mkdir(parents=True)
    (venv / 'bin/python3').symlink_to(real)
    (venv / 'pyvenv.cfg').write_text(f'home = {real.parent}\ninclude-system-site-packages = true\nversion = 3.14.0\n')
    site = venv / 'lib/python3.14/site-packages'
    site.mkdir(parents=True)
    for package in ('pytest', '_pytest', 'pluggy'):
        (site / package).symlink_to(real.parent.parent / 'lib/python3.14/site-packages' / package)
    mount = {'kind': 'python-env', 'root': str(env), 'reads': [str(env)], 'links': {},
             'executable': recipes.pin(venv / 'bin/python3'), 'env': {'VIRTUAL_ENV': str(venv)}}
    test = f'import sys\n\ndef test_prefix():\n    assert sys.prefix == {str(venv)!r} and sys.prefix != sys.base_prefix\n'
    root = make_project(tmp_path, {'test_env.py': test})
    plan = recipes.resolve('python-pytest', Workspace(root), mount)
    assert plan['argv'][0] == str(venv / 'bin/python3') and plan['executable'] == mount['executable']
    assert plan['env']['VIRTUAL_ENV'] == str(venv) and plan['env']['PATH'].startswith(str(venv / 'bin') + ':')
    result = run(root, 'python-pytest', mount)
    assert result['outcome'] == 'passed' and result['tests'] == counts(passed=1), result['output']
    assert result['environment']['kind'] == 'python-env'
    assert run(root, 'python-pytest')['outcome'] == 'failed'
    forged = dict(mount, executable=dict(mount['executable'], sha256='1' * 64))
    with pytest.raises(PolicyError, match=r'environment interpreter changed after review \(sha256\)'):
        recipes.resolve('python-pytest', Workspace(root), forged)
    outside = dict(mount, executable=recipes.pin(FRAMEWORK_PY))
    with pytest.raises(PolicyError, match='pinned interpreter inside its root'):
        recipes.resolve('python-pytest', Workspace(root), outside)
    (site / 'pytest').unlink()
    with pytest.raises(PolicyError, match='does not provide pytest'):
        recipes.resolve('python-pytest', Workspace(root), mount)


@macos
def test_leader_reaped_elsewhere_fails_closed_without_signalling(tmp_path, monkeypatch):
    signalled = []

    def reaped_elsewhere(idtype, pid, options):
        os.waitpid(pid, 0)
        raise ChildProcessError

    monkeypatch.setattr(testing, '_waitid', reaped_elsewhere)
    monkeypatch.setattr(os, 'killpg', lambda pid, sig: signalled.append(pid))
    with pytest.raises(PolicyError, match='could not be observed'):
        run(make_project(tmp_path, {'test_a.py': UNIT_PASS}), 'python-unittest')
    assert signalled == []


@macos
def test_cancel_timeout_and_output_overflow_stop_the_group(tmp_path, monkeypatch):
    sleeper = make_project(tmp_path, {'test_a.py': 'import time, unittest\n\nclass T(unittest.TestCase):\n'
                                                   '    def test_wait(self):\n        time.sleep(60)\n'}, 'sleep')
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(PolicyError, match='stopped, timed out, or exceeded the output limit'):
        run(sleeper, 'python-unittest', cancel=cancel)
    assert time.monotonic() - started < 15
    noisy = make_project(tmp_path, {'test_a.py': 'import sys, unittest\n\nclass T(unittest.TestCase):\n'
                                                 '    def test_noise(self):\n        sys.stdout.write("x" * 400_000)\n'
                                                 '        sys.stdout.flush()\n'}, 'noisy')
    with pytest.raises(PolicyError, match='exceeded the output limit'):
        run(noisy, 'python-unittest')
    monkeypatch.setitem(recipes.REGISTRY, 'python-unittest', dataclasses.replace(recipes.REGISTRY['python-unittest'], timeout=1))
    started = time.monotonic()
    with pytest.raises(PolicyError, match='timed out'):
        run(sleeper, 'python-unittest')
    assert time.monotonic() - started < 15
