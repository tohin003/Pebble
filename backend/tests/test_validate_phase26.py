"""Phase 26: scripts/validate_phase26.py, the 1.0.1 release's licence checks (source and frozen, the same names).

The real checkout passes; each check fails on a fixture that breaks it (a spec or archive with setuptools, an inventory
that ignores the macOS filter or forgives a missing notice, a 1.0.0 version), so no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase26_under_test', ROOT / 'scripts/validate_phase26.py')
v26 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase26_under_test'] = v26
spec.loader.exec_module(v26)


def campaign(tmp_path, backend=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    run = v26.Campaign(scratch, backend)
    run.run()
    return {r['name']: r for r in run.results}


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v26.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items() if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v26.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v26.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v26.READS)


def test_a_spec_that_keeps_setuptools_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(v26, 'spec_excluded', lambda: {'fastapi', 'pkg_resources', '_distutils_hack', 'distutils'})
    assert not campaign(tmp_path)['backend_ships_no_setuptools_family']['passed']


def test_excluding_backports_fails_because_the_build_would(tmp_path, monkeypatch):
    real = v26.spec_excluded
    monkeypatch.setattr(v26, 'spec_excluded', lambda: real() | {'backports'})
    assert not campaign(tmp_path)['backend_ships_no_setuptools_family']['passed']


@pytest.mark.parametrize('hooks, modules', [
    (['pyi_rth_inspect'], {'pebble', 'setuptools', 'setuptools._distutils'}),
    (['pyi_rth_inspect', 'pyi_rth_setuptools'], {'pebble'}),
])
def test_a_frozen_archive_with_setuptools_fails(tmp_path, monkeypatch, hooks, modules):
    executable = tmp_path / 'dist/pebble-backend/pebble-backend'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'frozen')
    monkeypatch.setattr(v26, 'frozen_contents', lambda path: (hooks, modules))
    assert not campaign(tmp_path, executable)['backend_ships_no_setuptools_family']['passed']


def test_a_clean_frozen_archive_passes(tmp_path, monkeypatch):
    executable = tmp_path / 'dist/pebble-backend/pebble-backend'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'frozen')
    monkeypatch.setattr(v26, 'frozen_contents', lambda path: (['pyi_rth_inspect'], {'pebble', 'urllib3'}))
    result = campaign(tmp_path, executable)['backend_ships_no_setuptools_family']
    assert result['passed'] and result['evidence']['family_modules'] == 0


def test_an_inventory_that_ignores_the_filter_fails(tmp_path, monkeypatch):
    real = v26.load

    def load(name, rel):
        module = real(name, rel)
        original = module.inventory
        module.inventory = lambda *a, cargo_metadata=None, **k: original(*a, **k)  # the pre-Phase 26 inventory
        return module
    monkeypatch.setattr(v26, 'load', load)
    assert not campaign(tmp_path)['inventory_counts_only_the_macos_build']['passed']


def test_an_inventory_that_forgives_a_listed_crate_fails(tmp_path, monkeypatch):
    real = v26.load

    def load(name, rel):
        module = real(name, rel)
        original = module.inventory

        def forgiving(*a, **k):
            result = original(*a, **k)
            result['findings'] = [f for f in result['findings'] if f['code'] != 'notice_missing']
            result['result'] = 'complete'
            return result
        module.inventory = forgiving
        return module
    monkeypatch.setattr(v26, 'load', load)
    assert not campaign(tmp_path)['a_counted_crate_still_needs_its_notice']['passed']


@pytest.mark.parametrize('version', ['1.0.0', '0.6.1'])
def test_a_version_not_after_1_0_0_fails(tmp_path, monkeypatch, version):
    monkeypatch.setattr(v26, 'checkout_version', lambda: version)
    assert not campaign(tmp_path)['release_version_is_after_1_0_0']['passed']
