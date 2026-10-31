"""Phase 38: scripts/validate_phase38.py, 1.5.1's floating companion over full-screen apps (source and frozen).

The real checkout passes; each check fails when the defect it guards returns (the companion left an ordinary window,
turned back into one when stealth turns off, activating Pebble, the workspace given an all-Spaces behaviour, notes
that do not describe the release), so no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase38_under_test', ROOT / 'scripts/validate_phase38.py')
v38 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase38_under_test'] = v38
spec.loader.exec_module(v38)


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v38.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def fails(tmp_path, check):
    result = campaign(tmp_path, {check})[check]
    assert not result['passed'], result.get('evidence')
    return result


def edited(monkeypatch, rel, old, new):
    real = v38.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v38, 'read', read)


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v38.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items()
                                                        if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v38.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v38.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v38.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v38_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v38.READS) <= set(vhc.sources()) and vhc.SUITES['phase38'] == 'validate_phase38.py'


@pytest.mark.parametrize('old, new', [
    ('        companion_over_full_screen(app); // Phase 38: also over other apps\' full-screen Spaces\n', ''),
    ('let panel=stealth_mac::set_panel(ns, true);', 'let panel=false;'),
    ('setHidesOnDeactivate: objc2::runtime::Bool::NO', 'setHidesOnDeactivate: objc2::runtime::Bool::YES'),
    ('B::CanJoinAllSpaces | B::FullScreenAuxiliary | B::IgnoresCycle', 'B::CanJoinAllSpaces | B::IgnoresCycle'),
])
def test_a_companion_that_is_not_always_a_panel_on_every_space_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'src-tauri/src/main.rs', old, new)
    fails(tmp_path, 'the_companion_is_always_a_nonactivating_panel_on_every_space')


def test_a_companion_turned_back_into_a_window_when_stealth_turns_off_fails(tmp_path, monkeypatch):
    edited(monkeypatch, 'src-tauri/src/main.rs',
           '    // Phase 38: turning stealth off made every window an ordinary one again; the companion stays a panel.\n'
           '    #[cfg(target_os="macos")]\n    companion_over_full_screen(app);\n', '')
    fails(tmp_path, 'turning_stealth_off_keeps_the_companion_a_panel')


def test_a_companion_that_activates_pebble_fails(tmp_path, monkeypatch):
    edited(monkeypatch, 'src-tauri/src/main.rs', 'if stealth_on() || window.label()=="pet" {', 'if stealth_on() {')
    fails(tmp_path, 'the_companion_never_activates_pebble')


@pytest.mark.parametrize('rel, old, new', [
    ('src-tauri/src/main.rs', 'fn reveal(app:&tauri::AppHandle) {\n',
     'fn reveal(app:&tauri::AppHandle) {\n    let _behavior = "CanJoinAllSpaces";\n'),
    ('src-tauri/tauri.conf.json', '"label": "main",', '"label": "main",\n        "visibleOnAllWorkspaces": true,'),
    ('src-tauri/src/main.rs', 'if !stealth_on() && !main.is_fullscreen().unwrap_or(false) { let _=main.set_fullscreen(true); }',
     'let _=main.set_always_on_top(true);'),
])
def test_a_workspace_that_could_open_over_another_full_screen_fails(tmp_path, monkeypatch, rel, old, new):
    edited(monkeypatch, rel, old, new)
    fails(tmp_path, 'the_workspace_opens_in_its_own_space_never_over_full_screen')


@pytest.mark.parametrize('old, new', [
    ('non-activating panel', 'floating window'),
    ('D75: validate_phase37 accepts', 'validate_phase37 accepts'),
])
def test_notes_that_do_not_describe_the_release_fail(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'CHANGELOG.md', old, new)
    fails(tmp_path, 'release_version_is_1_5_1_and_its_notes_describe_it')
