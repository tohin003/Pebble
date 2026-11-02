"""Phase 52: scripts/validate_phase52.py, 2.0.3's Stealth Lockdown as in 2.0.0 with a click that never takes focus
(D237-D241).

The real checkout passes; each check fails when the defect it guards returns: anything that takes Pebble out of the
person's view (2.0.1's guard, a window-server alpha write, an app hide, a held workspace, an out-of-view tooltip, the
companion's click-through alpha lost) or masks it (2.0.2's capture mask written anywhere); the sharing type set twice or
not at all by Stealth, wrong or on fewer windows; a keeper after CoreAnimation's commit, in one run-loop mode only, not
repeating, acting with Stealth off, never re-applying the sharing type or doing anything else, never started, able to
unwind into CoreFoundation, or setting a refusing window before every commit; Lockdown's panels losing a notified key,
KVO's bookkeeping widened, a set dropped without its key or notified out of order, a setter carried with the wrong key,
arguments or return, an unsupported type carried, a setter added to another class, a swap that does not try to carry or
that is refused for nothing; a lost half of Stealth Lockdown; a version left behind, a 2.0.2 entry, Highlights, notes
without the honest line, with a word the public text never uses or without what 2.0.3 fixes; a README naming another
release. So no check passes vacuously.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('validate_phase52_under_test', ROOT / 'scripts/validate_phase52.py')
v52 = importlib.util.module_from_spec(spec)
sys.modules['validate_phase52_under_test'] = v52
spec.loader.exec_module(v52)


def campaign(tmp_path, only=None):
    scratch = tmp_path / 'scratch'
    scratch.mkdir(parents=True)
    run = v52.Campaign(scratch, only=only)
    run.run()
    return {r['name']: r for r in run.results}


def fails(tmp_path, check):
    result = campaign(tmp_path, {check})[check]
    assert not result['passed'], result.get('evidence')
    return result


def edited(monkeypatch, rel, old, new):
    real = v52.read

    def read(name):
        text = real(name)
        if name == rel:
            assert old in text, old
            text = text.replace(old, new, 1)
        return text
    monkeypatch.setattr(v52, 'read', read)


def test_the_checkout_passes_every_check(tmp_path):
    results = campaign(tmp_path)
    assert set(results) == set(v52.PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert all(r['passed'] for r in results.values()), {n: r.get('error') for n, r in results.items()
                                                        if not r['passed']}
    sharing = results['stealth_sets_and_keeps_the_sharing_type_on_every_window']['evidence']
    assert 0 < sharing['order'] < sharing['before_commit_order'] == 2_000_000 and sharing['keeps'] == 'sharing type only'
    panels = results['lockdown_panels_hold_when_accessibility_observes_the_windows']['evidence']
    assert panels['notified'] == ['contentView', 'opaque', 'level'] and panels['carried_types'] == 8
    assert panels['boxes'] == 5 and panels['correction_bubble'] == 'off while Stealth is on, dismissed at the switch'
    assert results['release_version_is_2_0_3_with_its_notes']['evidence']['version'] == '2.0.3'


def test_the_report_names_the_same_checks_in_both_modes(tmp_path):
    report = tmp_path / 'source.json'
    assert v52.main(['--report', str(report)]) == 0
    data = json.loads(report.read_text())
    assert data['kind'] == 'source' and data['passed'] and sorted(data['checks']) == sorted(v52.PLANNED)
    assert data['backend_sha256'] is None and set(data['source_sha256']) == set(v52.READS)


def test_a_report_is_never_overwritten(tmp_path):
    report = tmp_path / 'once.json'
    report.write_text('{}')
    with pytest.raises(SystemExit):
        v52.main(['--report', str(report)])
    assert report.read_text() == '{}'


def test_every_read_is_bound_by_the_campaign():
    spec = importlib.util.spec_from_file_location('vhc_for_v52_test', ROOT / 'scripts/verify_harness_candidate.py')
    vhc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vhc)
    assert set(v52.READS) <= set(vhc.sources()) and vhc.SUITES['phase52'] == 'validate_phase52.py'
    assert 'phase52' in vhc.default_suites('phase52') and 'phase52' not in vhc.default_suites('phase51')
    assert {'phase51', 'phase49', 'phase48', 'phase47'} <= set(vhc.default_suites('phase52'))


SHELL = 'src-tauri/src/main.rs'
STEALTH_ON = 'fn stealth_on() -> bool {'


@pytest.mark.parametrize('old, new', [
    ('#[cfg(target_os="macos")]\nmod capture_keeper {', '#[cfg(target_os="macos")]\nmod capture_guard {}\n#[cfg(target_os="macos")]\nmod capture_keeper {'),
    (STEALTH_ON, 'fn refresh() { capture_guard::refresh(); }\n' + STEALTH_ON),
    (STEALTH_ON, '// CGSSetWindowAlpha(connection, number, 0.0)\n' + STEALTH_ON),
    (STEALTH_ON, '// SLSSetWindowAlpha(connection, number, 0.0)\n' + STEALTH_ON),
    (STEALTH_ON, '// CGSIsScreenWatcherPresent()\n' + STEALTH_ON),
    (STEALTH_ON, 'fn step_out() { unsafe { let _: () = objc2::msg_send![app(), hide: std::ptr::null::<AnyObject>()]; } }\n' + STEALTH_ON),
    (STEALTH_ON, 'fn come_back() { unsafe { let _: () = objc2::msg_send![app(), unhideWithoutActivation]; } }\n' + STEALTH_ON),
    (STEALTH_ON, 'fn mask(l: *mut AnyObject) { unsafe { let _: () = objc2::msg_send![l, setDisableUpdateMask: 9u32]; } }\n' + STEALTH_ON),
    (STEALTH_ON, 'pub const CAPTURE_MASK: u32 = 0x9;\n' + STEALTH_ON),
    (STEALTH_ON, 'fn set_capture_mask_all(on: bool) {}\n' + STEALTH_ON),
    ('fn reveal(app:&tauri::AppHandle) {\n    WORKSPACE_WANTED',
     'fn reveal(app:&tauri::AppHandle) {\n    if capture_held() { diag::log("reveal_held", json!({})); return; }\n    WORKSPACE_WANTED'),
    ("fn tray_tooltip() -> &'static str {\n",
     "fn tray_tooltip() -> &'static str {\n    if stealth_on() { return \"Pebble · out of view while the screen is being captured\"; }\n"),
    ('setAlphaValue: if on {0.0f64} else {1.0f64}', 'setAlphaValue: 1.0f64'),
    ('setAlphaValue: if on {0.0f64} else {1.0f64}]; }\n', 'setAlphaValue: if on {0.0f64} else {1.0f64}]; }\n        keep_out(ns);\n'),
])
def test_anything_that_takes_pebble_out_of_view_or_masks_it_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'pebble_stays_in_the_persons_view')


APPLY = '        stealth_mac::set_sharing_all(on);\n        // 2.0.3 (Phase 52, D242, F13)'
SWITCH = ('pub fn set_sharing_all(on: bool) {\n        unsafe {\n'
          '            let app: *mut AnyObject = msg_send![objc2::class!(NSApplication), sharedApplication];\n'
          '            let windows: *mut AnyObject = msg_send![app, windows];')
SET = 'let _: () = msg_send![window, setSharingType: if on { 0usize } else { 1usize }]; // None, ReadOnly'
KEEP = 'let sharing = super::stealth_mac::keep_sharing(force);'
NONE = 'let _: () = msg_send![window, setSharingType: 0usize]; // NSWindowSharingNone'


@pytest.mark.parametrize('old, new', [
    (APPLY, '        // 2.0.3 (Phase 52, D242, F13)'),
    (APPLY, '        stealth_mac::set_sharing_all(on);\n' + APPLY),
    ('let _=window.set_content_protected(on);', ''),
    (SWITCH, SWITCH.replace('msg_send![app, windows]', 'msg_send![app, orderedWindows]')),
    (SET, SET.replace('if on { 0usize } else { 1usize }', 'if on { 1usize } else { 1usize }')),
    (SET, 'let _ = on;'),
    ('const ORDER: isize = 1_999_000;', 'const ORDER: isize = 2_000_001;'),
    ('BEFORE_WAITING | EXIT, 1, ORDER', 'BEFORE_WAITING, 1, ORDER'),
    ('BEFORE_WAITING | EXIT, 1, ORDER', 'BEFORE_WAITING | EXIT, 0, ORDER'),
    ('CFRunLoopAddObserver(CFRunLoopGetMain(), observer, kCFRunLoopCommonModes)',
     'CFRunLoopAddObserver(CFRunLoopGetMain(), observer, kCFRunLoopDefaultMode)'),
    ('        if !super::stealth_on() { return; }\n', ''),
    (KEEP, 'let sharing = 0usize;'),
    (KEEP, KEEP + '\n            super::stealth_mac::set_panel(std::ptr::null_mut(), true);'),
    ('const FORCE_EVERY: Duration = Duration::from_secs(1);', 'const FORCE_EVERY: Duration = Duration::from_secs(3600);'),
    ('let _ = std::panic::catch_unwind(|| {', 'let _ = (|| {'),
    ('if STARTED.swap(true, Ordering::SeqCst) { return; }\n        let started', 'let started'),
    ('capture_keeper::start(); //', '//'),
    ('let current: usize = msg_send![window, sharingType];\n                if current != 0 || force {',
     'let current: usize = msg_send![window, sharingType];\n                if true {'),
    ('                    if held_back(number as usize) { continue; }\n', ''),
    ('if now != 0 { refuse(number as usize); } else if current != 0 { changed += 1; }', 'changed += 1;'),
    (NONE, NONE + '\n                    let _: () = msg_send![window, setSharingType: 1usize];'),
    ('const RETRY: Duration = Duration::from_secs(1);', 'const RETRY: Duration = Duration::from_secs(0);'),
])
def test_a_sharing_type_set_wrongly_or_a_keeper_that_misses_a_commit_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'stealth_sets_and_keeps_the_sharing_type_on_every_window')


NOTIFIED = '("setContentView:", "contentView"), ("setOpaque:", "opaque"), ("setLevel:", "level")]'
CARRY_TYPES = [
    "                Some(b'@') => std::mem::transmute(notify_object as SetObject),\n",
    "                Some(b'B' | b'c') => std::mem::transmute(notify_bool as SetFlag),\n",
    "                Some(b'q' | b'l') => std::mem::transmute(notify_isize as SetSigned),\n",
    "                Some(b'Q' | b'L') => std::mem::transmute(notify_usize as SetUnsigned),\n",
    "                Some(b'd') => std::mem::transmute(notify_f64 as SetDouble),\n",
    '                _ if kind.starts_with("{CGSize=") => std::mem::transmute(notify_size as SetSize),\n',
    '                _ if kind.starts_with("{CGPoint=") => std::mem::transmute(notify_point as SetPoint),\n',
    '                _ if kind.starts_with("{CGRect=") => std::mem::transmute(notify_rect as SetRect),\n',
]
RECORD = '            CARRIED.lock().unwrap().get_or_insert_with(HashMap::new).insert(selector.clone(), key.clone());\n'
ADD = ('            let added = ffi::class_addMethod(panel as *const AnyClass as *mut AnyClass, sel, imp, '
       'ffi::method_getTypeEncoding(method));\n')


@pytest.mark.parametrize('old, new', [
    ('const NOTIFIED: [(&str, &str); 3] = [' + NOTIFIED, 'const NOTIFIED: [(&str, &str); 2] = [("setContentView:", "contentView"), ("setOpaque:", "opaque")]'),
    ('const KVO_OWN: [&str; 3] = ["class", "dealloc", "_isKVOA"];', 'const KVO_OWN: [&str; 4] = ["class", "dealloc", "_isKVOA", "setLevel:"];'),
    ('                (sel!(setLevel:), std::mem::transmute(set_level as SetLevel))];', '];'),
    ('    extern "C-unwind" fn set_level(this: *mut AnyObject, sel: Sel, level: isize) {\n        notified(this, sel, |imp| unsafe {',
     '    extern "C-unwind" fn set_level(this: *mut AnyObject, sel: Sel, level: isize) {\n        (|imp: Imp| unsafe {'),
    ('            .or_else(|| carried_key(&selector));', ';'),
    ('                None => {\n                    call(imp);\n', '                None => {\n'),
    ('                    let _: () = msg_send![this, willChangeValueForKey: &*key];\n                    call(imp);\n',
     '                    call(imp);\n                    let _: () = msg_send![this, willChangeValueForKey: &*key];\n'),
    ("if !first.is_ascii_uppercase() || body.contains(':') { return None; }", "if body.contains(':') { return None; }"),
    ('if method.is_null() || ffi::method_getNumberOfArguments(method) != 3 { return false; }', 'if method.is_null() { return false; }'),
    ("            if returns[0] as u8 != b'v' { return false; }\n", ''),
    (CARRY_TYPES[0], ''), (CARRY_TYPES[1], ''), (CARRY_TYPES[2], ''), (CARRY_TYPES[3], ''),
    (CARRY_TYPES[4], ''), (CARRY_TYPES[5], ''), (CARRY_TYPES[6], ''), (CARRY_TYPES[7], ''),
    ('                _ => return false,\n', '                _ => std::mem::transmute(notify_object as SetObject),\n'),
    (RECORD + ADD, ADD + RECORD),
    ('ffi::class_addMethod(panel as *const AnyClass as *mut AnyClass, sel,', 'ffi::class_addMethod(class as *const AnyClass as *mut AnyClass, sel,'),
    ('            .filter(|sel| !carry_setter(panel, *sel))\n', ''),
    ('        if extra.is_empty() { Ok(()) } else { Err(extra) }\n    }\n\n    /// 2.0.3',
     '        let _ = extra;\n        Err(Vec::new())\n    }\n\n    /// 2.0.3'),
    ('let keep = swappable(now, tao, stealth_panel).is_ok();', 'let keep = false;'),
    ('match swappable(class, tao, panel) {', 'match swappable(class, tao, tao) {'),
])
def test_a_panel_that_lets_go_when_keys_are_observed_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, SHELL, old, new)
    fails(tmp_path, 'lockdown_panels_hold_when_accessibility_observes_the_windows')


ATTRS = '{ spellcheck: false, autocorrect: "off", autocapitalize: "off", autocomplete: "off", writingsuggestions: "false" }'
DISMISS = 'if !checker.is_null() { let _: () = objc2::msg_send![checker, dismissCorrectionIndicatorForView: view]; }'


@pytest.mark.parametrize('rel, old, new', [
    # D242 (F13): a box that keeps macOS's correction under Stealth, the attributes incomplete, normal mode changed, or
    # a bubble already up left in place when Stealth goes on
    ('src/Pet.vue', '              v-model="command"\n              v-bind="stealthTyping"\n', '              v-model="command"\n'),
    ('src/Pet.vue', '          v-model="input"\n          v-bind="stealthTyping"\n', '          v-model="input"\n'),
    ('src/App.vue', '                      v-model="editText"\n                      v-bind="stealthTyping"\n',
     '                      v-model="editText"\n'),
    ('src/App.vue', '                v-model="input"\n                v-bind="stealthTyping"\n', '                v-model="input"\n'),
    ('src/App.vue', '              v-model="terminalCommand"\n              v-bind="stealthTyping"\n', '              v-model="terminalCommand"\n'),
    ('src/Pet.vue', ATTRS, ATTRS.replace('spellcheck: false, ', '')),
    ('src/Pet.vue', ATTRS, ATTRS.replace(', writingsuggestions: "false"', '')),
    ('src/App.vue', ATTRS, ATTRS.replace('autocorrect: "off", ', '')),
    ('src/App.vue', ATTRS, ATTRS.replace('autocomplete: "off", ', '')),
    ('src/Pet.vue', '  stealthOn.value\n    ? {', '  true\n    ? {'),
    ('src/App.vue', '  state.value?.settings?.stealth\n    ? {', '  true\n    ? {'),
    (SHELL, DISMISS, ''),
    (SHELL, '        if on {\n            for label in ["pet", "main"] {', '        if !on {\n            for label in ["pet", "main"] {'),
])
def test_a_box_that_keeps_the_correction_bubble_under_stealth_fails(tmp_path, monkeypatch, rel, old, new):
    edited(monkeypatch, rel, old, new)
    fails(tmp_path, 'lockdown_panels_hold_when_accessibility_observes_the_windows')


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


@pytest.mark.parametrize('version', ['2.0.2', '2.0.1', '2.0.4'])
def test_a_version_other_than_2_0_3_fails(tmp_path, monkeypatch, version):
    monkeypatch.setattr(v52, 'checkout_version', lambda: version)
    fails(tmp_path, 'release_version_is_2_0_3_with_its_notes')


def test_a_declaration_left_behind_fails(tmp_path, monkeypatch):
    real = v52.declared_versions
    monkeypatch.setattr(v52, 'declared_versions', lambda: {**real(), 'package.json': '2.0.2'})
    fails(tmp_path, 'release_version_is_2_0_3_with_its_notes')


@pytest.mark.parametrize('rel, old, new', [
    ('CHANGELOG.md', '## [2.0.1]\n', '## [2.0.2]\n\nNot released.\n\n## [2.0.1]\n'),
    ('CHANGELOG.md', 'and the same data. 2.0.2 was not released.\n\n### Fixed\n',
     'and the same data. 2.0.2 was not released.\n\n### Highlights\n\n- Stealthier.\n\n### Fixed\n'),
    ('CHANGELOG.md', 'Hidden from screen capture and screen sharing, not from a camera, a capture card or a process\n  list.',
     'Hidden from screen capture and screen sharing.'),
    ('CHANGELOG.md', 'In 2.0.1 Pebble could step out', 'It is undetectable now. In 2.0.1 Pebble could step out'),
    ('CHANGELOG.md', 'In 2.0.1 Pebble could step out', 'It is invisible to recorders. In 2.0.1 Pebble could step out'),
    ('CHANGELOG.md', 'and the same data. 2.0.2 was not released.', 'and the same data.'),
    ('CHANGELOG.md', 'Lockdown is on, and your typing stays with Pebble.', 'Lockdown is on.'),
    ('CHANGELOG.md', '- **A click in Pebble no longer makes it the active app while', '- **A click in Pebble keeps you where you are while'),
    ('CHANGELOG.md', 'Install 2.0.3 over 2.0.1 or 2.0.0', 'Install it over 2.0.1 or 2.0.0'),
    ('src/App.vue', 'const WHATS_NEW_VERSION = "2.0.0";', 'const WHATS_NEW_VERSION = "2.0.3";'),
])
def test_release_text_unlike_the_rules_fails(tmp_path, monkeypatch, rel, old, new):
    edited(monkeypatch, rel, old, new)
    fails(tmp_path, 'release_version_is_2_0_3_with_its_notes')


@pytest.mark.parametrize('old, new', [
    ('at%20v2.0.3', 'at%20v2.0.2'),
    ('\n2.0.3 passed ', '\n2.0.2 passed '),
    ('automated tests at v2.0.3,', 'automated tests at v2.0.2,'),
])
def test_a_readme_naming_another_release_fails(tmp_path, monkeypatch, old, new):
    edited(monkeypatch, 'README.md', old, new)
    fails(tmp_path, 'readme_names_2_0_3')
