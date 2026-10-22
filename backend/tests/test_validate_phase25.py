"""Phase 25: scripts/validate_phase25.py, the release build's checks (source and frozen, the same names).

The real checkout passes; each check family fails on a fixture that breaks it (a stand-in build tool, profile,
toolchain or version), so no check passes vacuously.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase25_under_test', ROOT / 'scripts/validate_phase25.py')
v25 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase25_under_test'] = v25
spec.loader.exec_module(v25)


def campaign(tmp_path):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    run = v25.Campaign(scratch)
    run.run()
    return {r['name']: r for r in run.results}


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v25.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items() if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v25.main(['--report', str(report)]) == 0
    import json
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v25.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v25.READS)


def test_an_absolute_frontend_dist_fails(tmp_path, monkeypatch):
    real = v25.build_tool

    def tool():
        module = real()
        original = module.Build.merge_config
        module.Build.merge_config = lambda self, kind: {**original(self, kind),
                                                        'build': {'frontendDist': str(self.out / 'frontend/dist')}}
        return module
    monkeypatch.setattr(v25, 'build_tool', tool)
    assert not campaign(tmp_path)['release_merge_configs_embed_no_machine_path']['passed']


def test_a_profile_that_does_not_strip_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(v25.tomllib, 'loads', lambda text: {'profile': {'release': {'strip': False}}})
    assert not campaign(tmp_path)['release_profile_strips_the_shell']['passed']


def test_a_strip_tool_that_does_not_run_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(v25.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 6, '', 'dyld: Library not loaded'))
    assert not campaign(tmp_path)['pinned_toolchain_strip_tool_runs']['passed']


def test_a_guard_that_misses_this_machines_path_fails(tmp_path, monkeypatch):
    real = v25.build_tool

    def tool():
        module = real()
        module.machine_path_scan = lambda folder, needles, only=None: {'files': 1, 'machine': {}, 'users_prefix': 1, 'paths': []}
        return module
    monkeypatch.setattr(v25, 'build_tool', tool)
    assert not campaign(tmp_path)['shell_guard_refuses_this_machines_paths_only']['passed']


def test_a_guard_that_reads_the_index_after_the_prompt_fails(tmp_path, monkeypatch):
    """The pre-fix begin: whether to ground the task later was read after the engine handled the prompt."""
    real = v25.guard_adapter

    def adapter():
        base = real()

        class LateRead(base):
            def begin(self, root, cid, prompt, resume=False, changed=()):
                result = super().begin(root, cid, prompt, resume, changed)
                key = (str(Path(root)), cid)
                if key in self.roots and self.guard.workspace(self.roots[key]).index.is_built:
                    self.ungrounded.discard(key)
                return result
        return LateRead
    monkeypatch.setattr(v25, 'guard_adapter', adapter)
    result = campaign(tmp_path)['guard_grounds_the_task_whatever_the_index_build_order']
    assert not result['passed'] and "'between'" in result['error'], result


@pytest.mark.parametrize('version', ['0.9.0', '0.6.1'])
def test_a_pre_1_0_release_fails(tmp_path, monkeypatch, version):
    monkeypatch.setattr(v25, 'checkout_version', lambda: version)
    assert not campaign(tmp_path)['release_version_is_1_0_or_later']['passed']
