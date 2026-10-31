"""Phase 39: scripts/validate_phase39.py, 1.5.2's companion over full-screen apps and Stealth Lockdown's windows.

The real checkout passes; each check fails when the defect it guards returns (the Space behaviour no longer
re-applied, a left-out companion not brought back or brought back while the workspace is open, the Dock left during
the full-screen exit, the windowed size not kept, the anchor missing or taking clicks, full screen requested mid-slide,
the hide keys hiding only, the diagnostic open to others or to new commands, notes that promise focus or hide the known
issue), so no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase39_under_test', ROOT / 'scripts/validate_phase39.py')
v39 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase39_under_test'] = v39
spec.loader.exec_module(v39)


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v39.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def fails(tmp_path, check):
    result = campaign(tmp_path, {check})[check]
    assert not result['passed'], result.get('evidence')
    return result


def edited(monkeypatch, rel, old, new):
    real = v39.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v39, 'read', read)


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v39.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items()
                                                        if not r['passed']}


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v39.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v39.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v39.READS)


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v39_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v39.READS) <= set(vhc.sources()) and vhc.SUITES['phase39'] == 'validate_phase39.py'


MAIN = 'src-tauri/src/main.rs'


@pytest.mark.parametrize('old, new', [
    ('        let _: () = objc2::msg_send![ns, setCollectionBehavior: 0usize];\n', ''),
    ('    #[cfg(target_os="macos")]\n    companion_over_full_screen(app); // Phase 39 (D78): re-applied after the switch to Accessory\n', ''),
    ('if !stealth_on() { companion_over_full_screen(app); }', ''),
])
def test_a_space_behaviour_not_re_applied_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, MAIN, old, new)
    fails(tmp_path, 'the_companion_is_re_applied_after_every_policy_change')


@pytest.mark.parametrize('old, new', [
    ('    if policy != 1 { return; }\n', ''),
    ('    unsafe { let _: () = msg_send![ns, orderFrontRegardless]; }\n', ''),
    ('from_str("NSWorkspaceActiveSpaceDidChangeNotification")', 'from_str("NSWorkspaceDidWakeNotification")'),
])
def test_a_companion_not_brought_back_or_brought_back_in_the_wrong_mode_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, MAIN, old, new)
    fails(tmp_path, 'a_companion_left_out_of_a_space_is_ordered_back_in')


@pytest.mark.parametrize('old, new', [
    ('            deferred |= leaving;\n', ''),
    ('    exit_observer::watch(app, ended_tx);\n', ''),
    ('"NSWindowDidExitFullScreenNotification"', '"NSWindowWillExitFullScreenNotification"'),
])
def test_leaving_the_dock_during_the_exit_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, MAIN, old, new)
    fails(tmp_path, 'stealth_waits_for_the_full_screen_exit_before_leaving_the_dock')


@pytest.mark.parametrize('old, new', [
    ('        if !stealth_on() && !main.is_fullscreen().unwrap_or(false) { let _=windowed_frame(&main); }\n', ''),
    ('            if oversized {\n', '            if true {\n'),
])
def test_a_windowed_size_not_kept_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, MAIN, old, new)
    fails(tmp_path, 'the_workspace_keeps_its_windowed_size_through_full_screen')


@pytest.mark.parametrize('old, new', [
    ('setIgnoresMouseEvents: Bool::YES', 'setIgnoresMouseEvents: Bool::NO'),
    ('if stealth_on() && space_anchor::elsewhere() && !space_anchor::GOING.swap(true', 'if false && !space_anchor::GOING.swap(true'),
    ('            space_anchor::place(app.handle(), "launch"); // Phase 39 (D82)', '            // space_anchor::place(app.handle(), "launch"); (D82)'),
    ('if full != Some(true) { create(); }', 'create();'),  # D101: a full-screen Space no longer gets the anchor
])
def test_a_workspace_opened_over_the_full_screen_app_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, MAIN, old, new)
    fails(tmp_path, 'stealth_opens_the_workspace_on_the_desktop')


def test_full_screen_requested_mid_slide_fails(tmp_path, monkeypatch):
    edited(monkeypatch, MAIN, 'if !from_elsewhere || on_active_space(&main) { enter(&main); }', 'if true { enter(&main); }')
    fails(tmp_path, 'full_screen_waits_for_the_workspace_space')


@pytest.mark.parametrize('old, new', [
    ('"hide"=>hide_now(app)', '"hide"=>stealth_panic(app)'),
    ('        if hidden.contains(&"main") { reveal(app); }\n', ''),
])
def test_hide_keys_that_only_hide_fail(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, MAIN, old, new)
    fails(tmp_path, 'the_hide_keys_bring_back_what_they_hid')


@pytest.mark.parametrize('old, new', [
    ('meta.mode() & 0o022 != 0', 'false'),
    ('(Some("state"), None)', '(Some("shell"), None)'),
    ('let _ = file.write_all(line.as_bytes());', 'let _ = write!(file, "{}", line);'),
])
def test_a_diagnostic_open_to_others_or_new_commands_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, MAIN, old, new)
    fails(tmp_path, 'the_diagnostic_is_off_unless_named')


@pytest.mark.parametrize('rel, old, new', [
    ('README.md', 'Clicking or typing in the companion leaves the app you were in active',
     'Pebble never takes focus from the app in front'),  # Phase 45 (F151): the honest sentence, and the overclaim guard
    ('src/stealthVeil.ts', 'On. Pebble is hidden from screen capture.', "On. Pebble is hidden from screen capture and won't take focus."),
    ('CHANGELOG.md', '### Known issue', '### Notes'),
])
def test_notes_that_promise_focus_or_hide_the_known_issue_fail(tmp_path, monkeypatch, rel, old, new):
    edited(monkeypatch, rel, old, new)
    fails(tmp_path, 'release_version_is_1_5_2_and_its_notes_are_honest')
