"""Phase 51: scripts/validate_phase51.py, 2.0.2's Stealth Lockdown that keeps Pebble with the person (D229-D234).

The real checkout passes; each check fails when the defect it guards returns (anything that takes Pebble out of the
person's view for a capture: the guard, a window-server alpha write, an app hide, a held workspace, an out-of-view
tooltip, or the companion's own click-through alpha lost; a lost half of Stealth Lockdown), so no check passes vacuously.

Restated for 2.0.3 (Phase 52, D240): 2.0.2's mask hid Pebble from the person's own display too (D237) and was never
released; 2.0.3 has 2.0.0's capture behaviour (D238) with the sharing-only keeper (D241). The mask, apply, keeper and
release checks now hold a 2.0.3-or-later tree to that: any capture mask or guard coming back, the sharing type lost or
wrong on any window, Stealth setting it twice or not at all, a keeper after CoreAnimation's commit, in one run-loop mode
only, not repeating, acting with Stealth off, never re-applying the sharing type or doing more than that, never started,
able to unwind into CoreFoundation, or setting a refusing window before every commit; a version left behind, Highlights,
notes without the honest line or with a word the public text never uses, a README naming another release. A tree that
still says 2.0.2 is held to 2.0.2's own mask, which this tree does not have, so it fails.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase51_under_test', ROOT / 'scripts/validate_phase51.py')
v51 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase51_under_test'] = v51
spec.loader.exec_module(v51)
CURRENT = v51.checkout_version()


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v51.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def fails(tmp_path, check):
    result = campaign(tmp_path, {check})[check]
    assert not result['passed'], result.get('evidence')
    return result


def edited(monkeypatch, rel, old, new):
    real = v51.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v51, 'read', read)


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v51.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items()
                                                        if not r['passed']}
    assert results['the_capture_mask_is_set_on_every_window']['evidence']['mask'] == 'none (D238)'
    keeper = results['the_mask_is_kept_before_every_commit_while_stealth_is_on']['evidence']
    assert 0 < keeper['order'] < keeper['before_commit_order'] == 2_000_000 and keeper['keeps'] == 'sharing type only (D241)'
    release = results['release_version_is_2_0_2_with_its_notes']['evidence']
    assert release['version'] == CURRENT and release['restated'] == 'D240'


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v51.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v51.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v51.READS)


def test_a_report_is_never_overwritten(tmp_path):
    report = tmp_path / 'once.json'
    report.write_text('{}')
    with pytest.raises(SystemExit):
        v51.main(['--report', str(report)])
    assert report.read_text() == '{}'


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v51_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v51.READS) <= set(vhc.sources()) and vhc.SUITES['phase51'] == 'validate_phase51.py'
    assert 'phase51' in vhc.default_suites('phase51') and 'phase51' not in vhc.default_suites('phase50')
    assert {'phase49', 'phase48', 'phase47'} <= set(vhc.default_suites('phase51'))


SHELL = 'src-tauri/src/main.rs'


@pytest.mark.parametrize('old, new', [
    ('#[cfg(target_os="macos")]\nmod capture_keeper {', '#[cfg(target_os="macos")]\nmod capture_guard {}\n#[cfg(target_os="macos")]\nmod capture_keeper {'),
    ('fn stealth_on() -> bool {', '// CGSSetWindowAlpha(connection, number, 0.0)\nfn stealth_on() -> bool {'),
    ('fn stealth_on() -> bool {', '// SLSSetWindowAlpha(connection, number, 0.0)\nfn stealth_on() -> bool {'),
    ('fn stealth_on() -> bool {', '// CGSIsScreenWatcherPresent()\nfn stealth_on() -> bool {'),
    ('fn stealth_on() -> bool {', 'fn step_out() { unsafe { let _: () = objc2::msg_send![app(), hide: std::ptr::null::<AnyObject>()]; } }\n'
                                  'fn stealth_on() -> bool {'),
    ('fn stealth_on() -> bool {', 'fn come_back() { unsafe { let _: () = objc2::msg_send![app(), unhideWithoutActivation]; } }\n'
                                  'fn stealth_on() -> bool {'),
    ('fn reveal(app:&tauri::AppHandle) {\n    WORKSPACE_WANTED',
     'fn reveal(app:&tauri::AppHandle) {\n    if capture_held() { diag::log("reveal_held", json!({})); return; }\n    WORKSPACE_WANTED'),
    ("fn tray_tooltip() -> &'static str {\n",
     "fn tray_tooltip() -> &'static str {\n    if stealth_on() { return \"Pebble · out of view while the screen is being captured\"; }\n"),
    ('setAlphaValue: if on {0.0f64} else {1.0f64}', 'setAlphaValue: 1.0f64'),
    ('setAlphaValue: if on {0.0f64} else {1.0f64}]; }\n', 'setAlphaValue: if on {0.0f64} else {1.0f64}]; }\n        keep_out(ns);\n'),
])
def test_anything_that_takes_pebble_out_of_view_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'pebble_never_steps_out_of_the_persons_view')


SWITCH = ('pub fn set_sharing_all(on: bool) {\n        unsafe {\n'
          '            let app: *mut AnyObject = msg_send![objc2::class!(NSApplication), sharedApplication];\n'
          '            let windows: *mut AnyObject = msg_send![app, windows];')
SET = 'let _: () = msg_send![window, setSharingType: if on { 0usize } else { 1usize }]; // None, ReadOnly'
STEALTH_ON = 'fn stealth_on() -> bool {'


@pytest.mark.parametrize('old, new', [
    (STEALTH_ON, 'fn mask(l: *mut AnyObject) { unsafe { let _: () = objc2::msg_send![l, setDisableUpdateMask: 9u32]; } }\n' + STEALTH_ON),
    (STEALTH_ON, 'pub const CAPTURE_MASK: u32 = 0x9;\n' + STEALTH_ON),
    (STEALTH_ON, 'fn set_capture_mask_all(on: bool) {}\n' + STEALTH_ON),
    (STEALTH_ON, 'mod capture_guard {}\n' + STEALTH_ON),
    (STEALTH_ON, 'fn refresh() { capture_guard::refresh(); }\n' + STEALTH_ON),
    (SWITCH, SWITCH.replace('msg_send![app, windows]', 'msg_send![app, orderedWindows]')),
    (SET, SET.replace('if on { 0usize } else { 1usize }', 'if on { 1usize } else { 1usize }')),
    (SET, 'let _ = on;'),
])
def test_a_mask_or_a_lost_sharing_type_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'the_capture_mask_is_set_on_every_window')


APPLY = '        stealth_mac::set_sharing_all(on);\n        // 2.0.3 (Phase 52, D242, F13)'


@pytest.mark.parametrize('old, new', [
    (APPLY, '        // 2.0.3 (Phase 52, D242, F13)'),
    (APPLY, '        stealth_mac::set_sharing_all(on);\n' + APPLY),
    ('let _=window.set_content_protected(on);', ''),
    (APPLY, '        stealth_mac::set_sharing_all(on);\n        let _=stealth_mac::set_capture_mask_all(on);\n        // 2.0.3 (Phase 52, D242, F13)'),
    ('    if !on { wake_outbox(app); }', '    if !on { wake_outbox(app); }\n    // setDisableUpdateMask'),
])
def test_stealth_setting_the_sharing_type_wrongly_or_masking_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'stealth_applies_the_mask_beside_the_sharing_type')


KEEP = 'let sharing = super::stealth_mac::keep_sharing(force);'


@pytest.mark.parametrize('old, new', [
    ('const ORDER: isize = 1_999_000;', 'const ORDER: isize = 2_000_001;'),
    ('BEFORE_WAITING | EXIT, 1, ORDER', 'BEFORE_WAITING, 1, ORDER'),
    ('BEFORE_WAITING | EXIT, 1, ORDER', 'BEFORE_WAITING | EXIT, 0, ORDER'),
    ('CFRunLoopAddObserver(CFRunLoopGetMain(), observer, kCFRunLoopCommonModes)',
     'CFRunLoopAddObserver(CFRunLoopGetMain(), observer, kCFRunLoopDefaultMode)'),
    ('        if !super::stealth_on() { return; }\n', ''),
    (KEEP, 'let sharing = 0usize;'),
    (KEEP, 'let sharing = super::stealth_mac::keep_sharing(false);'),
    ('const FORCE_EVERY: Duration = Duration::from_secs(1);', 'const FORCE_EVERY: Duration = Duration::from_secs(3600);'),
    ('capture_keeper::start(); //', '//'),
    ('let current: usize = msg_send![window, sharingType];\n                if current != 0 || force {',
     'let current: usize = msg_send![window, sharingType];\n                if true {'),
    ('const RETRY: Duration = Duration::from_secs(1);', 'const RETRY: Duration = Duration::from_secs(0);'),
    ('                    if held_back(number as usize) { continue; }\n', ''),
    ('if now != 0 { refuse(number as usize); } else if current != 0 { changed += 1; }', 'changed += 1;'),
    ('let _ = std::panic::catch_unwind(|| {', 'let _ = (|| {'),
    ('if STARTED.swap(true, Ordering::SeqCst) { return; }\n        let started', 'let started'),
    (KEEP, KEEP + '\n            super::stealth_mac::set_sharing_all(true);'),
    ('refused.retain(|_, until| *until > now);', ''),
    (KEEP, KEEP + '\n            let _ = super::stealth_mac::set_capture_mask_all(true);'),
    ('let _: () = msg_send![window, setSharingType: 0usize]; // NSWindowSharingNone',
     'let _: () = msg_send![window, setSharingType: 0usize]; // NSWindowSharingNone\n'
     '                    let _: () = msg_send![window, setDisableUpdateMask: 9u32];'),
])
def test_a_keeper_that_misses_a_commit_or_does_more_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'the_mask_is_kept_before_every_commit_while_stealth_is_on')


@pytest.mark.parametrize('old, new', [
    ('let _=window.set_content_protected(on);', ''),
    ('let _=window.set_shadow(!on);', ''),
    ('else { let _=stealth_mac::set_panel(ns, on); }', 'else { }'),
    ('msg_send![ns, _setPreventsActivation: Bool::new(on)]', 'msg_send![ns, _setPreventsActivation: Bool::new(false)]'),
    ('Some("settings") { stealth_follow(&handle); }', 'Some("settings") { }'),
    ('            if stealth_on() { continue; }\n', ''),
    ('    if !on { wake_outbox(app); }', ''),
    ('if on && !deferred { yield_on_this_space(); }', ''),
])
def test_a_lost_half_of_stealth_lockdown_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'the_other_halves_of_stealth_lockdown_are_unchanged')


def test_a_version_left_behind_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(v51, 'checkout_version', lambda: '2.0.1')
    fails(tmp_path, 'release_version_is_2_0_2_with_its_notes')


@pytest.mark.parametrize('check', ['the_capture_mask_is_set_on_every_window', 'stealth_applies_the_mask_beside_the_sharing_type',
                                   'the_mask_is_kept_before_every_commit_while_stealth_is_on'])
def test_a_tree_that_says_2_0_2_without_its_mask_fails(tmp_path, monkeypatch, check):
    monkeypatch.setattr(v51, 'checkout_version', lambda: '2.0.2')
    fails(tmp_path, check)


@pytest.mark.parametrize('rel, old, new', [
    ('CHANGELOG.md', 'and the same data. 2.0.2 was not released.\n\n### Fixed\n',
     'and the same data. 2.0.2 was not released.\n\n### Highlights\n\n- Stealthier.\n\n### Fixed\n'),
    ('CHANGELOG.md', 'Hidden from screen capture and screen sharing, not from a camera, a capture card or a process\n  list.',
     'Hidden from screen capture and screen sharing.'),
    ('CHANGELOG.md', 'In 2.0.1 Pebble could step out', 'It is undetectable now. In 2.0.1 Pebble could step out'),
    ('CHANGELOG.md', 'In 2.0.1 Pebble could step out', 'It is invisible to recorders. In 2.0.1 Pebble could step out'),
    ('CHANGELOG.md', 'Lockdown is on, and your typing stays with Pebble.', 'Lockdown is on.'),
    ('README.md', 'at%20v' + CURRENT, 'at%20v2.0.1'),
    ('README.md', '\n' + CURRENT + ' passed ', '\n2.0.1 passed '),
    ('src/App.vue', 'const WHATS_NEW_VERSION = "2.0.0";', 'const WHATS_NEW_VERSION = "2.0.3";'),
])
def test_release_text_unlike_the_rules_fails(tmp_path, monkeypatch, rel, old, new):
    edited(monkeypatch, rel, old, new)
    fails(tmp_path, 'release_version_is_2_0_2_with_its_notes')
