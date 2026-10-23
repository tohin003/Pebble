"""Phase 29: scripts/validate_phase29.py, the Claude benchmark's pinned CLI and the absent-CLI driver stop (source and
frozen, the same names).

The real checkout passes; each check fails on a fixture that restores the defect it guards (a pin that runs the
installed file itself, a driver that scores an absent CLI as an answer, a 1.0.1 version), so no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase29_under_test', ROOT / 'scripts/validate_phase29.py')
v29 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase29_under_test'] = v29
spec.loader.exec_module(v29)
bp, cc = v29.bench()


def campaign(tmp_path):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v29.Campaign(scratch)
    run.run()
    return {r['name']: r for r in run.results}


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v29.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items() if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v29.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v29.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v29.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v29_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v29.READS) <= set(vhc.sources()) and vhc.SUITES['phase29'] == 'validate_phase29.py'


def test_a_pin_that_runs_the_installed_file_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, 'pin_binary', lambda binary, root: (Path(binary), 'x' * 64))
    assert not campaign(tmp_path)['the_cli_is_pinned_once_per_plan_and_an_update_cannot_reach_it']['passed']


def test_a_driver_that_scores_an_absent_cli_as_an_answer_fails(tmp_path, monkeypatch):
    real = cc.run_cell

    def old(*args, **kwargs):
        out, metrics = real(*args, **kwargs)
        if metrics['error'] == 'claude_unavailable':
            out.failure, metrics['error'] = None, None  # the pre-Phase 29 driver: a failed turn with no kind
        return out, metrics
    monkeypatch.setattr(cc, 'run_cell', old)
    assert not campaign(tmp_path)['a_cli_that_never_starts_is_a_driver_error_and_stops_the_run']['passed']


@pytest.mark.parametrize('version', ['1.0.1', '1.0.0'])
def test_a_version_not_after_1_0_1_fails(tmp_path, monkeypatch, version):
    monkeypatch.setattr(v29, 'checkout_version', lambda: version)
    assert not campaign(tmp_path)['release_version_is_after_1_0_1']['passed']
