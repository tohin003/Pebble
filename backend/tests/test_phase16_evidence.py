"""Phase 16 evidence tooling: the phase16 proof namespace and its service suite (T6's proposal)."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_phase16_namespace_adds_only_its_own_suites():
    v = module('verify_phase16_ns', 'verify_harness_candidate.py')
    rank = v.PHASES.index('phase16')  # later phases append after it (Phase 17 added phase17)
    assert v.PHASES[rank - 1:rank + 1] == ('phase15', 'phase16') and v.SUITES['phase16'] == 'validate_phase16.py'
    own = [s for s in v.SUITES if s.startswith('phase16')]
    assert all((ROOT / 'scripts' / v.SUITES[s]).is_file() for s in own)
    assert not set(own) & set(v.default_suites('phase15'))
    assert v.default_suites('phase16') == v.default_suites('phase15') + own
    assert v.default_suites(v.PHASES[-1]) == [s for s in v.SUITES if s not in v.RETIRED]
    assert not any(phase == 'phase16' for phase, _ in v.RETIRED.values())


def test_phase15_namespace_refuses_the_phase16_suite(monkeypatch, tmp_path):
    v = module('verify_phase16_refusal', 'verify_harness_candidate.py')
    snapshot = tmp_path / 'snapshot.json'
    snapshot.write_text('{}')
    monkeypatch.setattr(v, 'sources', lambda: {})
    monkeypatch.setattr(v, 'candidate_manifest', lambda executable: {'bundle_sha256': 'x', 'files': []})
    monkeypatch.setattr(v, 'sha', lambda path: 'x')
    candidate = tmp_path / 'exe'
    candidate.write_text('')
    monkeypatch.setattr(sys, 'argv', ['verify', '--candidate', str(candidate), '--snapshot', str(snapshot),
                                      '--phase', 'phase15', '--suite', 'phase16'])
    with pytest.raises(RuntimeError, match='Phase 16 proof requires its own namespace'):
        v.main()


def test_phase16_validator_report_cannot_pass_partially(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))  # the validator imports earlier validators by name
    p16 = module('validate_phase16_contract', 'validate_phase16.py')
    assert len(p16.PLANNED) == len(set(p16.PLANNED)) and p16.PLANNED[-1] == 'backend_inputs_stable_during_campaign'
    assert p16.VAULT_CHECKS < set(p16.PLANNED) and issubclass(p16.Pending, AssertionError)
