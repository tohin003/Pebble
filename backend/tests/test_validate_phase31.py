"""Phase 31: scripts/validate_phase31.py, 1.0.4's full workspace (source and frozen, the same names).

The real checkout passes; each check fails on a fixture that restores the defect it guards (an Open full workspace that
never goes full screen, a Dock left to slide up, a tao that is not the pinned one, a way back that hides the window in
its full-screen Space, a title bar that stays the overlay one, a 1.0.3 version, notes without the fix), so no check
passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase31_under_test', ROOT / 'scripts/validate_phase31.py')
v31 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase31_under_test'] = v31
spec.loader.exec_module(v31)


def campaign(tmp_path):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v31.Campaign(scratch)
    run.run()
    return {r['name']: r for r in run.results}


def edited(monkeypatch, rel, old, new):
    real = v31.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v31, 'read', read)


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v31.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items() if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v31.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v31.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v31.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v31_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v31.READS) <= set(vhc.sources()) and vhc.SUITES['phase31'] == 'validate_phase31.py'


@pytest.mark.parametrize('rel,old,new', [
    ('src-tauri/src/main.rs', 'if !main.is_fullscreen().unwrap_or(false) { let _=main.set_fullscreen(true); }', ''),
    ('src-tauri/src/main.rs', 'let _=main.set_always_on_top(false);', ''),
    ('src-tauri/src/main.rs', '(proposed & !AUTO_HIDE_DOCK) | HIDE_DOCK', 'proposed'),
    ('src-tauri/src/main.rs', 'let _ = hide_dock_in_fullscreen();', ''),
    ('src-tauri/Cargo.lock', 'name = "tao"\nversion = "0.37.1"', 'name = "tao"\nversion = "0.38.0"'),
])
def test_a_workspace_that_is_not_full_screen_without_the_dock_fails(tmp_path, monkeypatch, rel, old, new):
    edited(monkeypatch, rel, old, new)
    assert not campaign(tmp_path)['the_full_workspace_opens_in_full_screen_without_the_dock']['passed']


@pytest.mark.parametrize('old,new', [
    ('fn show_pet(app:tauri::AppHandle) { to_pet(&app); }', 'fn show_pet(app:tauri::AppHandle) { pet_now(&app); }'),
    ('=> { api.prevent_close(); to_pet(window.app_handle()); }', '=> { api.prevent_close(); let _=window.hide(); }'),
    ('let _=tx.send(main_fullscreen(&probe));', 'let _=tx.send(false);'),
])
def test_a_way_back_that_hides_the_window_in_full_screen_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'src-tauri/src/main.rs', old, new)
    assert not campaign(tmp_path)['leaving_the_full_workspace_returns_to_the_pebble']['passed']


def test_a_title_bar_that_stays_the_overlay_one_fails(tmp_path, monkeypatch):
    edited(monkeypatch, 'src-tauri/src/main.rs', 'if full {tauri::TitleBarStyle::Visible} else {tauri::TitleBarStyle::Overlay}',
           'tauri::TitleBarStyle::Overlay')
    assert not campaign(tmp_path)['full_screen_uses_the_standard_title_bar']['passed']


def test_a_version_not_after_1_0_3_or_notes_without_the_fix_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(v31, 'checkout_version', lambda: '1.0.3')
    assert not campaign(tmp_path)['release_version_is_after_1_0_3_and_its_notes_carry_the_fix']['passed']
    monkeypatch.undo()
    edited(monkeypatch, 'CHANGELOG.md', '1.0.2 and 1.0.3 were not published on their own', '1.0.3 was not published')
    assert not campaign(tmp_path / 'b')['release_version_is_after_1_0_3_and_its_notes_carry_the_fix']['passed']
