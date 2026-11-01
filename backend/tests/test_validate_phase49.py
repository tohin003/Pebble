"""Phase 49: scripts/validate_phase49.py, 2.0.1's capture guard and release text (D225-D227; source and frozen).

The real checkout passes; each check fails when the defect it guards returns (the guard polling slower than measured,
holding for an announced ScreenCaptureKit capture, hiding Pebble before its windows' alpha, coming back by activating,
keeping a lost notice; the workspace or the companion's alpha coming back while it holds, a silent tooltip; a Stealth
change from Pebble Remote that never reaches the shell, or any path applying it twice; banners pulled while Stealth is
on, or never asked for again; a version left behind, Highlights in 2.0.1, notes without the honest line or with a
word the public text never uses; the guide naming the old tab or promising focus), so no check passes vacuously.
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
    assert results['the_guard_steps_pebble_out_of_unannounced_captures']['evidence']['poll_ms'] == 2


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


@pytest.mark.parametrize('old, new', [
    ('Duration::from_millis(if stealth { 2 } else { 100 })', 'Duration::from_millis(if stealth { 16 } else { 100 })'),
    ('let qos = unsafe { pthread_set_qos_class_self_np(QOS_USER_INTERACTIVE, 0) };', 'let qos = 0;'),
    ('const QOS_USER_INTERACTIVE: u32 = 0x21;', 'const QOS_USER_INTERACTIVE: u32 = 0x15;'),
    ('} else if stealth && watched && !announced() {', '} else if stealth && watched {'),
    ('if !stealth || !watched {', 'if !watched {'),
    ('for number in &windows { unsafe { (api.alpha)(connection, *number, 0.0); } }\n', ''),
    ('const SESSION_BEGAN: u32 = 1327;', 'const SESSION_BEGAN: u32 = 1502;'),
    ('msg_send![ns_app, unhideWithoutActivation]', 'msg_send![ns_app, unhide: std::ptr::null::<AnyObject>()]'),
    ('if super::app_active() { super::yield_on_this_space(); }\n', ''),
    ('sessions.retain(|(_, began)| began.elapsed() < Duration::from_secs(3));', ''),
    ('(api.alpha)(connection, number as u32, alpha as f32);', '(api.alpha)(connection, number as u32, 1.0);'),
    ('capture_guard::refresh(); //', '//'),
    ('capture_guard::start(app.handle());', 'capture_guard::refresh();'),
])
def test_a_guard_unlike_the_measured_one_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'the_guard_steps_pebble_out_of_unannounced_captures')


@pytest.mark.parametrize('old, new', [
    ('    if capture_guard::engaged() { diag::log("reveal_held", json!({})); return; }\n', ''),
    ('        capture_guard::keep_out(ns as *mut objc2::runtime::AnyObject);', ''),
    ('if capture_guard::engaged() { return "Pebble · out of view while the screen is being captured"; }', ''),
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


@pytest.mark.parametrize('rel, old, new', [
    ('CHANGELOG.md', '### Fixed\n\n- **Stealth Lockdown, hardened.**', '### Highlights\n\n- Stronger.\n\n### Fixed\n\n- **Stealth Lockdown, hardened.**'),
    ('CHANGELOG.md', 'Hidden from screen capture and screen sharing, not from a camera, a capture card or\n  a process list.',
     'Hidden from screen capture and screen sharing.'),
    ('CHANGELOG.md', 'Its screen-capture protection is stronger,', 'It is now undetectable,'),
    ('CHANGELOG.md', 'Its screen-capture protection is stronger,', 'It is now invisible to every recorder,'),
    ('README.md', 'at%20v2.0.1', 'at%20v2.0.0'),
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
