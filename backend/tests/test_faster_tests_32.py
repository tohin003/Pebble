"""Phase 32: faster tests. The proof log a parallel run writes is the one the release finalizers read, the serial
register stays true, and the affected-test selector runs everything when it is unsure.

Each test drives the real plugins and scripts on a small fixture suite in tmp_path (its own conftest loads the same
plugins), so no full suite runs here.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / 'backend' / 'tests'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The release finalizers' row rule (the same pattern as theirs; they are not published, so it is restated here).
FINALIZER_ROW = re.compile(r'^(backend/tests/[\w/.-]+\.py)::(\S.*?) (PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)(?: \(.*\))?\s+\[\s*\d+%\]\s*$')
from pebble import release_readiness as rr  # noqa: E402


def fixture_suite(tmp_path, body, register=None):
    """A tiny backend/tests stand-in that loads the real proof_log and serial_register plugins."""
    suite = tmp_path / 'backend' / 'tests'
    suite.mkdir(parents=True)
    for name in ('proof_log.py', 'serial_register.py', 'pytest.ini'):
        # The real pytest.ini too: it makes backend/tests the rootdir, so pytest lists test IDs relative to it (the
        # first Phase 32 measurement's runner missed every ID of the real suite for want of this in its fixture).
        (suite / name).write_text((TESTS / name).read_text())
    (suite / 'conftest.py').write_text('import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n'
                                       "pytest_plugins = ('proof_log', 'serial_register')\n")
    (suite / 'serial_register.json').write_text(json.dumps(register or {}))
    (suite / 'test_fixture.py').write_text(body)
    return suite


def pytest_run(tmp_path, *args):
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    return subprocess.run([sys.executable, '-m', 'pytest', 'backend/tests', '-p', 'no:cacheprovider', '-q', *args],
                          cwd=tmp_path, capture_output=True, text=True, env=env)


BODY = '''
import time, pytest
@pytest.mark.parametrize("n", range(6))
def test_value(n):
    time.sleep(0.01)
    assert n >= 0
def test_alone():
    assert True
'''


def rows(text):
    return [line for line in text.splitlines() if re.match(r'^backend/tests/\S+ (PASSED|FAILED|ERROR|SKIPPED)', line)]


def test_the_parallel_proof_log_equals_the_serial_one_and_the_finalizers_read_it(tmp_path):
    pytest.importorskip('xdist')
    fixture_suite(tmp_path, BODY)
    serial = pytest_run(tmp_path, '--proof-log', str(tmp_path / 'serial.log'))
    parallel = pytest_run(tmp_path, '-n', '2', '--proof-log', str(tmp_path / 'parallel.log'))
    assert serial.returncode == parallel.returncode == 0, (serial.stdout[-800:], parallel.stdout[-800:])
    a, b = (tmp_path / 'serial.log').read_text(), (tmp_path / 'parallel.log').read_text()
    assert rows(a) == rows(b) and len(rows(a)) == 7, (a, b)
    summary = rr.parse_test_log(b.encode())
    assert summary['collected'] == summary['passed'] == 7 and summary['failed'] == summary['errors'] == 0
    parsed = [FINALIZER_ROW.match(line) for line in b.splitlines()]
    assert [m.group(3) for m in parsed if m] == ['PASSED'] * 7 and 'collected 7 items' in b


def test_a_failure_and_a_teardown_error_reach_the_proof_log(tmp_path):
    fixture_suite(tmp_path, '''
import pytest
@pytest.fixture
def broken():
    yield
    raise RuntimeError("teardown")
def test_fails():
    assert False
def test_teardown(broken):
    assert True
def test_ok():
    assert True
''')
    done = pytest_run(tmp_path, '--proof-log', str(tmp_path / 'proof.log'))
    assert done.returncode == 1
    text = (tmp_path / 'proof.log').read_text()
    assert 'test_fixture.py::test_fails FAILED' in text and 'test_fixture.py::test_teardown ERROR' in text
    assert 'test_fixture.py::test_ok PASSED' in text
    summary = rr.parse_test_log(text.encode())
    assert summary['failed'] == 1 and summary['errors'] == 1 and summary['failure_lines'] == 2


def test_the_proof_log_is_never_overwritten(tmp_path):
    fixture_suite(tmp_path, BODY)
    (tmp_path / 'proof.log').write_text('earlier evidence\n')
    done = pytest_run(tmp_path, '--proof-log', str(tmp_path / 'proof.log'))
    assert done.returncode != 0 and (tmp_path / 'proof.log').read_text() == 'earlier evidence\n'


def test_the_serial_register_marks_named_tests_and_refuses_a_stale_or_unexplained_entry(tmp_path):
    fixture_suite(tmp_path, BODY, {'test_fixture.py::test_alone': {'reason': 'a fixture that measures time'}})
    alone = pytest_run(tmp_path, '-m', 'serial')
    rest = pytest_run(tmp_path, '-m', 'not serial')
    assert '1 passed, 6 deselected' in alone.stdout and '6 passed, 1 deselected' in rest.stdout
    stale = tmp_path / 'stale'
    fixture_suite(stale, BODY, {'test_fixture.py::test_gone': {'reason': 'removed long ago'}})
    assert 'not collected' in pytest_run(stale).stdout + pytest_run(stale).stderr
    unexplained = tmp_path / 'unexplained'
    fixture_suite(unexplained, BODY, {'test_fixture.py::test_alone': {'reason': ' '}})
    assert 'needs a test file or node and a reason' in pytest_run(unexplained).stdout + pytest_run(unexplained).stderr


def test_the_real_serial_register_is_valid():
    register = load('serial_register_real', TESTS / 'serial_register.py')
    entries = register.load()
    assert all(key.startswith('test_') and value['reason'].strip() for key, value in entries.items())
    assert all((TESTS / key.split('::')[0]).is_file() for key in entries)


affected = load('affected_tests_under_test', ROOT / 'scripts/affected_tests.py')


def test_a_module_change_selects_the_tests_that_reach_it_and_the_safety_set():
    chosen, reasons, unmapped = affected.select(['backend/pebble/runtime_commands.py'])
    assert 'test_runtime_commands.py' in chosen and 'test_phase17_security.py' in chosen and not unmapped
    assert set(affected.SAFETY) & set(chosen) == {n for n in affected.SAFETY if (TESTS / n).is_file()}
    assert len(chosen) < len(list(TESTS.glob('test_*.py'))) and 'everything' not in reasons


def test_a_changed_test_selects_itself_and_the_test_setup_selects_everything():
    chosen, _, _ = affected.select(['backend/tests/test_runtime_commands.py'])
    assert 'test_runtime_commands.py' in chosen
    for setup in ('backend/tests/conftest.py', 'backend/tests/serial_register.json', 'backend/requirements.lock'):
        chosen, reasons, _ = affected.select([setup])
        assert len(chosen) == len(list(TESTS.glob('test_*.py'))) and reasons['everything'] == setup


def test_a_file_no_test_reaches_selects_everything():
    chosen, reasons, unmapped = affected.select(['docs/A_NEW_UNREAD_PAGE.md'])
    assert unmapped == ['docs/A_NEW_UNREAD_PAGE.md'] and len(chosen) == len(list(TESTS.glob('test_*.py')))
    assert reasons['everything'].startswith('no test reaches')


def test_static_imports_and_path_literals_are_both_followed():
    cache, memo = {}, {}
    reached = affected.closure(TESTS / 'test_validate_phase31.py', cache, memo)
    names = {p.relative_to(ROOT).as_posix() for p in reached}
    assert 'scripts/validate_phase31.py' in names  # a path literal
    assert 'scripts/validate_phase21.py' in names  # imported by that script in turn


runner = load('run_full_suite_under_test', ROOT / 'scripts/run_full_suite.py')


def test_the_full_suite_runner_merges_both_sessions_into_one_proof_log(tmp_path):
    pytest.importorskip('xdist')
    fixture_suite(tmp_path, BODY, {'test_fixture.py::test_alone': {'reason': 'runs alone in this fixture'}})
    out = tmp_path / 'run'
    assert runner.main(['--out', str(out), '--workers', '2', '--root', str(tmp_path)]) == 0
    summary = json.loads((out / 'summary.json').read_text())
    assert summary['passed'] and summary['collected'] == 7 and summary['session_b']['tests'] == 1
    assert summary['missing'] == summary['extra'] == summary['in_both_sessions'] == []
    assert 'scratch_kept' not in summary and not any(out.glob('cases-*'))  # scratch never in --out; removed on a pass
    text = (out / 'proof.log').read_text()
    assert len(rows(text)) == 7 and rows(text)[-1].startswith('backend/tests/test_fixture.py::test_alone PASSED')
    parsed = rr.parse_test_log(text.encode())
    assert parsed['collected'] == parsed['passed'] == 7 and parsed['deselected'] == 0
    with pytest.raises(SystemExit):
        runner.main(['--out', str(out), '--root', str(tmp_path)])  # never into an existing folder


def test_the_full_suite_runner_fails_when_a_test_fails_in_either_session(tmp_path):
    pytest.importorskip('xdist')
    fixture_suite(tmp_path, BODY + '\ndef test_broken():\n    assert False\n',
                  {'test_fixture.py::test_alone': {'reason': 'runs alone in this fixture'}})
    out = tmp_path / 'run'
    assert runner.main(['--out', str(out), '--workers', '2', '--root', str(tmp_path)]) == 1
    summary = json.loads((out / 'summary.json').read_text())
    assert not summary['passed'] and summary['outcomes'].get('FAILED') == 1
    assert 'FAILED backend/tests/test_fixture.py::test_broken' in (out / 'proof.log').read_text()
    kept = Path(summary['scratch_kept'])  # a failed run keeps its scratch, outside the checkout, for diagnosis
    assert kept.parent == Path('/private/tmp') and kept.name.startswith('pebble-suite-') and kept.is_dir()
    assert not kept.is_relative_to(ROOT)
    import shutil
    shutil.rmtree(kept)


def test_the_runner_reads_ids_relative_to_the_suite_rootdir(tmp_path):
    """backend/tests/pytest.ini makes backend/tests the rootdir: `--collect-only -q` lists `test_x.py::t` there."""
    fixture_suite(tmp_path, BODY)
    ids = runner.collect(sys.executable, tmp_path)
    assert len(ids) == 7 and all(i.startswith('backend/tests/test_fixture.py::') for i in ids), ids[:3]
    (tmp_path / 'backend/tests/pytest.ini').unlink()  # without it the rootdir is the checkout: same IDs
    assert runner.collect(sys.executable, tmp_path) == ids


def campaign_driver(tmp_path, monkeypatch, fail=None):
    """verify_harness_candidate.main on stand-in suites: every check is kept, only the validators are fakes that write
    a passing (or, for `fail`, a failing) raw report, and the snapshot and candidate are fixed."""
    v = load('vhc_pool_' + tmp_path.name, ROOT / 'scripts/verify_harness_candidate.py')
    monkeypatch.setattr(v, 'ROOT', tmp_path)
    (tmp_path / 'reports').mkdir()
    (tmp_path / '.runtime').mkdir()
    (tmp_path / 'scripts').mkdir()
    suites = {f'phase{n}': f'validate_s{n}.py' for n in (90, 91, 92, 93, 94)}
    for name in suites.values():
        (tmp_path / 'scripts' / name).write_text('# stand-in\n')
    monkeypatch.setattr(v, 'SUITES', suites)
    monkeypatch.setattr(v, 'PHASES', ('phase90', 'phase91', 'phase92', 'phase93', 'phase94'))
    monkeypatch.setattr(v, 'RETIRED', {})
    monkeypatch.setattr(v, 'SERIAL_SUITES', frozenset({'phase92'}))
    monkeypatch.setattr(v, 'sources', lambda: {'a.py': 'x'})
    monkeypatch.setattr(v, 'candidate_manifest', lambda executable: {'bundle_sha256': 'b' * 64, 'files': []})
    order = []

    def fake(command, log):
        raw = Path(command[command.index('--report') + 1])
        suite = Path(command[1]).name
        kind = 'frozen' if '--backend' in command else 'source'
        order.append((suite, kind))
        log.write_text('ok\n')
        raw.write_text(json.dumps({'passed': True, 'kind': kind, 'checks': ['c1', 'c2'], 'source_sha256': {'a.py': 'x'},
                                   'backend_sha256': 'e' * 64}))
        return 1 if fail == (suite, kind) else 0
    monkeypatch.setattr(v, 'run_validator', fake)
    monkeypatch.setattr(v, 'sha', lambda path: 'e' * 64)
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text(json.dumps({'a.py': 'x'}))
    candidate = tmp_path / 'backend'
    candidate.write_bytes(b'x')
    return v, order, candidate, snapshot


def test_the_pooled_campaign_gives_the_same_reports_and_runs_serial_suites_alone(tmp_path, monkeypatch, capsys):
    reports = {}
    for jobs in ('1', '3'):
        root = tmp_path / ('jobs' + jobs)
        root.mkdir()
        v, order, candidate, snapshot = campaign_driver(root, monkeypatch)
        monkeypatch.setattr(sys, 'argv', ['vhc', '--candidate', str(candidate), '--snapshot', str(snapshot),
                                          '--phase', 'phase94', '--jobs', jobs])
        v.main()
        reports[jobs] = sorted(p.name for p in (root / 'reports').iterdir())
        if jobs == '3':
            assert order[-2:] == [('validate_s92.py', 'source'), ('validate_s92.py', 'frozen')], order  # alone, last
    assert reports['1'] == reports['3'] and len(reports['1']) == 10
    assert '"reports": 10, "checks": 20' in capsys.readouterr().out


def test_a_failure_in_the_pool_stops_the_campaign(tmp_path, monkeypatch):
    v, order, candidate, snapshot = campaign_driver(tmp_path, monkeypatch, fail=('validate_s90.py', 'frozen'))
    monkeypatch.setattr(sys, 'argv', ['vhc', '--candidate', str(candidate), '--snapshot', str(snapshot),
                                      '--phase', 'phase94', '--jobs', '2'])
    with pytest.raises(RuntimeError, match='Validator failed: phase90/frozen'):
        v.main()
    assert ('validate_s92.py', 'source') not in order  # the serial suite never started after the failure


def test_every_safety_set_name_is_a_real_test_file():
    """The first draft named test_permissions.py and test_vault.py, which do not exist: the set silently lost them."""
    missing = [name for name in affected.SAFETY if not (TESTS / name).is_file()]
    assert not missing, missing
