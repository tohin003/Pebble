"""Phase 49: scripts/validate_phase49.py, 2.0.1's capture guard and release text (D225-D227; source and frozen).

The real checkout passes; each check fails when the defect it guards returns (the guard polling slower than measured,
holding for an announced ScreenCaptureKit capture, hiding Pebble before its windows' alpha, coming back by activating,
keeping a lost notice; the workspace or the companion's alpha coming back while it holds, a silent tooltip; a Stealth
change from Pebble Remote that never reaches the shell, or any path applying it twice; banners pulled while Stealth is
on, or never asked for again; a version left behind, Highlights in 2.0.1, notes without the honest line or with a
word the public text never uses; the guide naming the old tab or promising focus), so no check passes vacuously.

Restated for 2.0.2 (Phase 51, D234): the guard's checks hold a later release to the capture mask in the guard's place
(the measured value, set on every window and its frame view's layer, applied in apply_stealth after the sharing type,
kept in every run-loop mode while Stealth is on, the keeper started) with no window-server alpha write or app hide left,
and to nothing held, since Pebble never leaves the person's view; a tree without the guard that still says 2.0.1 fails.
The release check follows the checkout's release; the 2.0.1 entry's checks are unchanged.

Restated again for 2.0.3 (Phase 52, D240): 2.0.2's mask hid Pebble from the person's own display too (D237) and was
never released, and 2.0.3 has 2.0.0's capture behaviour (D238). The guard check now holds a 2.0.3-or-later tree to each
window's sharing type in the guard's place, with no capture guard or mask, no window-server alpha write and no app hide
anywhere; the sharing type lost on any window, or set twice by Stealth, fails it; a tree that still says 2.0.2 without its
mask fails, and so does one that says 2.0.1 without its guard.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase49_under_test', ROOT / 'scripts/validate_phase49.py')
v49 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase49_under_test'] = v49
spec.loader.exec_module(v49)
CURRENT = v49.checkout_version()


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v49.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def fails(tmp_path, check):
    result = campaign(tmp_path, {check})[check]
    assert not result['passed'], result.get('evidence')
    return result


def edited(monkeypatch, rel, old, new):
    real = v49.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v49, 'read', read)


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v49.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items()
                                                        if not r['passed']}
    guard = results['the_guard_steps_pebble_out_of_unannounced_captures']['evidence']
    assert guard == {'release': CURRENT, 'in_place_of_the_guard': "each window's sharing type (D238)"}, guard
    held = results['nothing_brings_pebble_back_while_the_guard_holds']['evidence']
    assert held == {'release': CURRENT, 'held': 'nothing: Pebble stays in view'}, held


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v49.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v49.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v49.READS)


def test_a_report_is_never_overwritten(tmp_path):
    report = tmp_path / 'once.json'
    report.write_text('{}')
    with pytest.raises(SystemExit):
        v49.main(['--report', str(report)])
    assert report.read_text() == '{}'


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v49_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v49.READS) <= set(vhc.sources()) and vhc.SUITES['phase49'] == 'validate_phase49.py'
    assert 'phase49' in vhc.default_suites('phase49') and 'phase49' not in vhc.default_suites('phase48')
    assert 'phase48' in vhc.default_suites('phase49')


SHELL = 'src-tauri/src/main.rs'


STEALTH_ON = 'fn stealth_on() -> bool {'
SET = 'let _: () = msg_send![window, setSharingType: if on { 0usize } else { 1usize }]; // None, ReadOnly'
SWITCH = ('pub fn set_sharing_all(on: bool) {\n        unsafe {\n'
          '            let app: *mut AnyObject = msg_send![objc2::class!(NSApplication), sharedApplication];\n'
          '            let windows: *mut AnyObject = msg_send![app, windows];')
APPLY = '        stealth_mac::set_sharing_all(on);\n        // 2.0.3 (Phase 52, D242, F13)'


@pytest.mark.parametrize('old, new', [
    (STEALTH_ON, 'fn mask(l: *mut AnyObject) { unsafe { let _: () = objc2::msg_send![l, setDisableUpdateMask: 9u32]; } }\n' + STEALTH_ON),
    (STEALTH_ON, 'pub const CAPTURE_MASK: u32 = 0x9;\n' + STEALTH_ON),
    (STEALTH_ON, 'fn set_capture_mask_all(on: bool) {}\n' + STEALTH_ON),
    (STEALTH_ON, 'fn refresh() { capture_guard::refresh(); }\n' + STEALTH_ON),
    (SWITCH, SWITCH.replace('msg_send![app, windows]', 'msg_send![app, orderedWindows]')),
    (SET, SET.replace('if on { 0usize } else { 1usize }', 'if on { 1usize } else { 1usize }')),
    (APPLY, '        // 2.0.3 (Phase 52, D242, F13)'),
    (APPLY, '        stealth_mac::set_sharing_all(on);\n' + APPLY),
    (STEALTH_ON, '// CGSSetWindowAlpha(connection, number, 0.0)\n' + STEALTH_ON),
    (STEALTH_ON, '// SLSSetWindowAlpha(connection, number, 0.0)\n' + STEALTH_ON),
    (STEALTH_ON, 'fn step_out() { unsafe { let _: () = objc2::msg_send![app(), hide: std::ptr::null::<AnyObject>()]; } }\n' + STEALTH_ON),
    (STEALTH_ON, 'fn come_back() { unsafe { let _: () = objc2::msg_send![app(), unhideWithoutActivation]; } }\n' + STEALTH_ON),
    (SET, 'let _ = on;'),
])
def test_anything_but_the_sharing_type_in_the_guards_place_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'the_guard_steps_pebble_out_of_unannounced_captures')


@pytest.mark.parametrize('old, new', [
    ('fn reveal(app:&tauri::AppHandle) {\n    WORKSPACE_WANTED',
     'fn reveal(app:&tauri::AppHandle) {\n    if capture_guard::engaged() { diag::log("reveal_held", json!({})); return; }\n    WORKSPACE_WANTED'),
    ('setAlphaValue: if on {0.0f64} else {1.0f64}', 'setAlphaValue: 1.0f64'),
    ('setAlphaValue: if on {0.0f64} else {1.0f64}]; }\n', 'setAlphaValue: if on {0.0f64} else {1.0f64}]; }\n        keep_out(ns);\n'),
    ("fn tray_tooltip() -> &'static str {\n",
     "fn tray_tooltip() -> &'static str {\n    if stealth_on() { return \"Pebble · out of view while the screen is being captured\"; }\n"),
    ('let _=tray.set_tooltip(Some(tray_tooltip()));', 'let _=tray.set_tooltip(Some("Pebble"));'),
])
def test_pebble_coming_back_while_held_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'nothing_brings_pebble_back_while_the_guard_holds')


@pytest.mark.parametrize('old, new', [
    ('if value.get("event").and_then(Value::as_str)==Some("settings") { stealth_follow(&handle); } // 2.0.1', ''),
    ('if stealth_on()!=enabled { apply_stealth(&handle, enabled) }', 'apply_stealth(&handle, enabled)'),
    ('if stealth_on()!=next { apply_stealth(&shown, next) }', 'apply_stealth(&shown, next)'),
    ('run_on_main_thread(move || if !stealth_on() { apply_stealth(&shown, true) });',
     'run_on_main_thread(move || apply_stealth(&shown, true));'),
    ('            if stealth_on()!=wanted { apply_stealth(&shown, wanted); }', '            apply_stealth(&shown, wanted);'),
])
def test_a_stealth_change_that_misses_the_shell_or_applies_twice_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'a_stealth_change_from_anywhere_reaches_the_shell_once')


@pytest.mark.parametrize('old, new', [
    ('            if stealth_on() { continue; }\n', ''),
    ('    if !on { wake_outbox(app); }', ''),
])
def test_banners_pulled_in_stealth_or_never_asked_for_fail(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'banners_wait_while_stealth_lockdown_is_on')


def test_a_version_left_behind_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(v49, 'checkout_version', lambda: '2.0.0')
    fails(tmp_path, 'release_version_is_2_0_1_with_its_notes')


@pytest.mark.parametrize('check', ['the_guard_steps_pebble_out_of_unannounced_captures',
                                   'nothing_brings_pebble_back_while_the_guard_holds'])
def test_the_guard_gone_in_a_tree_that_still_says_2_0_1_fails(tmp_path, monkeypatch, check):
    monkeypatch.setattr(v49, 'checkout_version', lambda: '2.0.1')
    fails(tmp_path, check)


def test_a_tree_that_says_2_0_2_without_its_mask_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(v49, 'checkout_version', lambda: '2.0.2')
    fails(tmp_path, 'the_guard_steps_pebble_out_of_unannounced_captures')


@pytest.mark.parametrize('rel, old, new', [
    ('CHANGELOG.md', '### Fixed\n\n- **Stealth Lockdown, hardened.**', '### Highlights\n\n- Stronger.\n\n### Fixed\n\n- **Stealth Lockdown, hardened.**'),
    ('CHANGELOG.md', 'Hidden from screen capture and screen sharing, not from a camera, a capture card or\n  a process list.',
     'Hidden from screen capture and screen sharing.'),
    ('CHANGELOG.md', 'Its screen-capture protection is stronger,', 'It is now undetectable,'),
    ('CHANGELOG.md', 'Its screen-capture protection is stronger,', 'It is now invisible to every recorder,'),
    ('README.md', 'at%20v' + CURRENT, 'at%20v2.0.0'),
])
def test_release_text_unlike_the_rules_fails(tmp_path, monkeypatch, rel, old, new):
    edited(monkeypatch, rel, old, new)
    fails(tmp_path, 'release_version_is_2_0_1_with_its_notes')


@pytest.mark.parametrize('rel, old, new', [
    ('README.md', 'Turn it on in Settings → Privacy & Stealth,', 'Turn it on in Settings → Privacy and focus,'),
    ('docs/USER_GUIDE.md', '**Settings › Privacy & Stealth › Stealth Lockdown**', '**Settings › Privacy and focus › Stealth Lockdown**'),
    ('docs/USER_GUIDE.md', '- **Lockdown.** The app you are in stays the active one, with its menu bar.',
     '- **Lockdown.** Pebble never takes keyboard focus from the app in front.'),
    ('docs/USER_GUIDE.md', 'a capture card or a\n  process list.', 'a capture card.'),
    ('README.md', '> Hidden from screen capture and screen sharing, not from a camera, a capture card or a process list. Exams and',
     '> Hidden from screen capture and screen sharing. Exams and'),
])
def test_stealth_text_that_names_the_old_tab_or_loses_its_line_fails(tmp_path, monkeypatch, rel, old, new):
    edited(monkeypatch, rel, old, new)
    fails(tmp_path, 'the_stealth_text_names_its_tab_and_keeps_its_line')
