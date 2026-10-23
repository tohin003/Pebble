"""Phase 32: scripts/validate_phase32.py, faster tests and the fair benchmark harness (source and frozen, the same names).

The real checkout passes; each check fails on a fixture that restores the defect it guards (a proof log in completion
order, a runner that passes a skipped test, a register entry without a reason, a selector that trusts an unmapped
file, a campaign that pools the machine-measuring suites, a harness that counts caches, ignores a question or dies on
EPERM), so no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase32_under_test', ROOT / 'scripts/validate_phase32.py')
v32 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase32_under_test'] = v32
spec.loader.exec_module(v32)


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v32.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def one(tmp_path, name):
    result = campaign(tmp_path, {name})[name]
    return result['passed'], result.get('error')


def test_the_checkout_passes_every_check(tmp_path):
    pytest.importorskip('xdist')
    results = campaign(tmp_path)
    assert set(results) == set(v32.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items() if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    pytest.importorskip('xdist')
    report = tmp_path / 'source.json'
    assert v32.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v32.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v32.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v32_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v32.READS) <= set(vhc.sources()) and vhc.SUITES['phase32'] == 'validate_phase32.py'


def plugins(tmp_path, proof_log=None, register=None):
    """A stand-in tests folder: the real plugins (one optionally edited) and register."""
    tests = tmp_path / 'tests'
    tests.mkdir()
    for name in ('proof_log.py', 'serial_register.py', 'serial_register.json', 'pytest.ini'):
        (tests / name).write_text((v32.TESTS / name).read_text())
    if proof_log:
        text = (tests / 'proof_log.py').read_text()
        assert proof_log[0] in text
        (tests / 'proof_log.py').write_text(text.replace(*proof_log, 1))
    if register is not None:
        (tests / 'serial_register.json').write_text(json.dumps(register))
    return tests


def test_a_proof_log_in_completion_order_fails(tmp_path, monkeypatch):
    pytest.importorskip('xdist')
    # Completion order: under -n 2 the quick file's row arrives before the slower file's; serially they agree.
    monkeypatch.setattr(v32, 'TESTS', plugins(tmp_path, ('for index, nodeid in enumerate(order, 1):',
                                                          'for index, nodeid in enumerate([n for n in self.outcomes '
                                                          'if n in order] or order, 1):')))
    passed, error = one(tmp_path, 'a_parallel_proof_log_equals_the_serial_one')
    assert not passed and 'differ' in error


def test_a_runner_that_passes_a_skipped_test_fails(tmp_path, monkeypatch):
    pytest.importorskip('xdist')
    real = v32.load

    def lenient(name, path):
        module = real(name, path)
        if name == 'run_full_suite_p32':
            main = module.main
            module.main = lambda argv=None: (main(argv), 0)[1]
        return module
    monkeypatch.setattr(v32, 'load', lenient)
    passed, _ = one(tmp_path, 'the_full_suite_runner_merges_both_sessions_and_fails_on_any_failure')
    assert not passed


@pytest.mark.parametrize('register', [{'test_x.py': {'reason': ''}}, {'not_a_test.py': {'reason': 'measured'}},
                                      {'test_no_such_file_32.py': {'reason': 'measured once'}}])
def test_a_register_entry_without_a_reason_or_a_collected_test_fails(tmp_path, monkeypatch, register):
    monkeypatch.setattr(v32, 'TESTS', plugins(tmp_path, register=register))
    passed, _ = one(tmp_path, 'the_serial_register_is_true')
    assert not passed


def test_a_selector_that_trusts_an_unmapped_file_fails(tmp_path, monkeypatch):
    real = v32.load

    def trusting(name, path):
        module = real(name, path)
        if name == 'affected_tests_p32':
            select = module.select
            module.select = lambda changed: (lambda r: (r[0], {}, []) if r[1].get('everything', '').startswith('no test')
                                             else r)(select(changed))
        return module
    monkeypatch.setattr(v32, 'load', trusting)
    passed, _ = one(tmp_path, 'affected_tests_run_everything_when_unsure')
    assert not passed


def test_a_campaign_that_pools_the_measuring_suites_fails(tmp_path, monkeypatch):
    real = v32.load

    def pooled(name, path):
        module = real(name, path)
        if name.startswith('vhc_p32_'):
            module.SERIAL_SUITES = frozenset({'phase18'})
        return module
    monkeypatch.setattr(v32, 'load', pooled)
    passed, _ = one(tmp_path, 'the_pooled_campaign_gives_the_same_reports')
    assert not passed


@pytest.mark.parametrize('defect', ['caches', 'question', 'eperm'])
def test_each_harness_defect_restored_fails(tmp_path, monkeypatch, defect):
    sys.path.insert(0, str(ROOT))
    from benchmarks.drivers import codex_cell as cx
    from benchmarks.drivers import workspace as ws
    if defect == 'caches':
        monkeypatch.setattr(ws, 'CACHE_DIRS', frozenset())
    elif defect == 'question':
        old = cx.asks_question
        monkeypatch.setattr(cx, 'asks_question', lambda reply, changed=None: old(reply))
    else:
        def raising(pid, signum):
            raise PermissionError(1, 'Operation not permitted')
        monkeypatch.setattr(cx, 'signal_group', raising)
    passed, _ = one(tmp_path, 'benchmark_harness_defects_stay_fixed')
    assert not passed


def test_it_runs_as_a_script_the_way_the_campaign_starts_it(tmp_path):
    """The campaign starts each validator as its own process with no inherited PYTHONPATH; the first draft imported
    pebble only because pytest's path had it, and the pre-trial sweep caught the ModuleNotFoundError."""
    pytest.importorskip('xdist')
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    report = tmp_path / 'script.json'
    done = subprocess.run([sys.executable, str(ROOT / 'scripts/validate_phase32.py'), '--report', str(report)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600)
    data = json.loads(report.read_text())
    assert done.returncode == 0 and data['passed'] and data['fatal_error'] is None, (data['failed'], data['fatal_error'])
