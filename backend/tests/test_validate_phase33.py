"""Phase 33: scripts/validate_phase33.py, 1.1.0's Stealth Lockdown (source and frozen, the same names).

The real checkout passes; each check fails on a fixture that restores the defect it guards (a window left shareable, a
reveal that makes Pebble a regular app, a second set_focus, a missing key, an avatar without the Veil, other timings,
the honest line missing or "undetectable" in it, notes without the sealed corpus), so no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase33_under_test', ROOT / 'scripts/validate_phase33.py')
v33 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase33_under_test'] = v33
spec.loader.exec_module(v33)


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v33.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def edited(monkeypatch, rel, old, new):
    real = v33.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v33, 'read', read)


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v33.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items() if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v33.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v33.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v33.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v33_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v33.READS) <= set(vhc.sources()) and vhc.SUITES['phase33'] == 'validate_phase33.py'


@pytest.mark.parametrize('check, rel, old, new', [
    ('stealth_hides_every_window_and_lifts_it_again', 'src-tauri/src/main.rs',
     '        stealth_mac::set_sharing_all(on);\n', ''),
    ('stealth_hides_every_window_and_lifts_it_again', 'src-tauri/src/main.rs',
     'setCollectionBehavior: (current & !MANAGED) | TRANSIENT', 'setCollectionBehavior: current'),
    ('lockdown_never_activates_pebble', 'src-tauri/src/main.rs',
     '    if !stealth_on() { let _=app.set_activation_policy(tauri::ActivationPolicy::Regular); }',
     '    let _=app.set_activation_policy(tauri::ActivationPolicy::Regular);'),
    ('lockdown_never_activates_pebble', 'src-tauri/src/main.rs',
     '    if expanded { focus(&window); }', '    if expanded { let _=window.set_focus(); }'),
    ('lockdown_never_activates_pebble', 'src-tauri/src/main.rs',
     'if ffi::class_getInstanceSize(panel) != ffi::class_getInstanceSize(window) { return None; }', ''),
    ('the_keys_and_the_menu_bar_icon', 'src-tauri/src/main.rs',
     '            let _ = app.global_shortcut().register("Control+Alt+Command+H");\n', ''),
    ('the_setting_is_kept_and_reaches_every_window', 'src/App.vue',
     '<Avatar :size="38" :reduced="true" :stealth="!!state.settings.stealth" />', '<Avatar :size="38" :reduced="true" />'),
    ('the_veil_is_the_chosen_design', 'src/stealthVeil.ts', 'export const ACTIVATE = 1.25;', 'export const ACTIVATE = 2;'),
    ('the_honest_line_travels_with_the_feature', 'docs/USER_GUIDE.md',
     'a capture card or a\n  process list.', 'a capture card.'),
    ('the_honest_line_travels_with_the_feature', 'README.md',
     'One switch for presenting, calls and screen shares.', 'One undetectable switch for presenting, calls and screen shares.'),
    ('release_version_is_1_1_0_and_its_notes_carry_stealth_lockdown', 'CHANGELOG.md',
     '27889ccdb1fa8d26c047ec3d127f10d2dadd9cf8cb550d74f0d565d70054d051', 'a sealed set'),
])
def test_each_check_fails_when_its_defect_returns(tmp_path, monkeypatch, check, rel, old, new):
    edited(monkeypatch, rel, old, new)
    result = campaign(tmp_path, {check})[check]
    assert not result['passed'], check


def test_a_setting_that_asks_for_confirmation_fails(tmp_path, monkeypatch):
    from pebble import settings_file
    real = settings_file.widening
    monkeypatch.setattr(settings_file, 'widening', lambda key, old, new: ['stealth turns on'] if key == 'stealth' else real(key, old, new))
    check = 'the_setting_is_kept_and_reaches_every_window'
    assert not campaign(tmp_path, {check})[check]['passed']
