"""Phase 30: scripts/validate_phase30.py, the D37 audit's fixes (source and frozen, the same names).

The real checkout passes; each check fails on a fixture that restores the defect it guards (a capability without the
zoom permission, a service that still offers pdf, a panel that still offers PDF, a harness that runs the release bundle,
a retracted claim put back, a 1.0.2 version), so no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase30_under_test', ROOT / 'scripts/validate_phase30.py')
v30 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase30_under_test'] = v30
spec.loader.exec_module(v30)
bp, live = v30.bench()


def campaign(tmp_path):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v30.Campaign(scratch)
    run.run()
    return {r['name']: r for r in run.results}


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v30.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items() if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v30.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v30.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v30.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v30_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v30.READS) <= set(vhc.sources()) and vhc.SUITES['phase30'] == 'validate_phase30.py'


def test_a_capability_without_the_zoom_permission_fails(tmp_path, monkeypatch):
    real = v30.capabilities

    def without():
        caps = real()
        for cap in caps:
            cap['permissions'] = [p for p in cap['permissions'] if p != v30.ZOOM]
        return caps
    monkeypatch.setattr(v30, 'capabilities', without)
    assert not campaign(tmp_path)['zoom_hotkeys_have_their_permission']['passed']


def test_a_service_that_offers_pdf_fails(tmp_path, monkeypatch):
    # The service is its own process: a shim puts the 1.0.2 catalogue and formats back before main.py runs.
    shim = tmp_path / 'service_1_0_2.py'
    shim.write_text(
        'import runpy, sys\n'
        f'sys.path.insert(0, {str(ROOT / "backend")!r})\n'
        'from pebble import runtime_commands as rc\n'
        'rc.EXPORT_FORMATS = ("pdf",) + tuple(rc.EXPORT_FORMATS)\n'
        'next(c for c in rc.CATALOGUE if c["name"] == "export")["arguments"] = "view-id pdf|docx|xlsx|csv|html"\n'
        f'sys.argv[0] = {str(ROOT / "backend/main.py")!r}\n'
        f'runpy.run_path({str(ROOT / "backend/main.py")!r}, run_name="__main__")\n')
    monkeypatch.setattr(v30.Campaign, 'command', lambda self: [sys.executable, str(shim)])
    assert not campaign(tmp_path)['pdf_export_is_not_offered_until_it_exists']['passed']


def test_a_panel_that_offers_pdf_fails(tmp_path, monkeypatch):
    real = v30.read

    def panel(rel):
        text = real(rel)
        if rel == 'src/components/ExportPanel.vue':
            text = text.replace('const FORMATS = [', "const FORMATS = [\n  { id: 'pdf', label: 'PDF document' },", 1)
        return text
    monkeypatch.setattr(v30, 'read', panel)
    assert not campaign(tmp_path)['pdf_export_is_not_offered_until_it_exists']['passed']


def test_a_harness_that_runs_the_release_bundle_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(live, 'evaluation_backend', lambda backend: Path(backend))
    assert not campaign(tmp_path)['evaluation_never_runs_the_installed_identity']['passed']


@pytest.mark.parametrize('rel,claim', [('README.md', 'Export to PDF, Word, Excel, CSV or HTML.'),
                                       ('docs/USER_GUIDE.md', 'Control granted app windows and browser tabs'),
                                       ('src/components/AdvancedSettings.vue', 'Control granted app windows and browser tabs'),
                                       ('README.md', 'Switch per conversation.')])
def test_a_retracted_claim_put_back_fails(tmp_path, monkeypatch, rel, claim):
    real = v30.read
    monkeypatch.setattr(v30, 'read', lambda name: real(name) + ('\n' + claim if name == rel else ''))
    assert not campaign(tmp_path)['public_text_claims_only_what_the_product_does']['passed']


def test_a_release_without_corrections_fails(tmp_path, monkeypatch):
    real = v30.read
    monkeypatch.setattr(v30, 'read', lambda name: real(name).replace('### Corrections', '### Changed') if name == 'CHANGELOG.md' else real(name))
    assert not campaign(tmp_path)['public_text_claims_only_what_the_product_does']['passed']


@pytest.mark.parametrize('version', ['1.0.2', '1.0.1'])
def test_a_version_not_after_1_0_2_fails(tmp_path, monkeypatch, version):
    monkeypatch.setattr(v30, 'checkout_version', lambda: version)
    assert not campaign(tmp_path)['release_version_is_after_1_0_2']['passed']
