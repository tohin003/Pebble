#!/usr/bin/env python3
"""Phase 52 service validator: 2.0.3, Stealth Lockdown as in 2.0.0, with a click that never takes focus (D237-D241;
.runtime/phase52-plan.md).

2.0.2's capture mask hid Pebble from the person's own display as well as from captures, so it was never released (D237).
The person chose 2.0.0's capture behaviour for 2.0.3 (D238): each Pebble window's sharing type, which keeps it out of
screen capture and screen sharing while it stays on their screen, with a keeper that gives the same sharing type to
windows AppKit adds while Stealth Lockdown is on, such as menus, pop-ups and tooltips (D241). And a click in Pebble must
never take focus from the app in front, also after an accessibility client has read Pebble's windows (D239; measured on
the real app and on stand-ins, .runtime/phase51-dev/native/panel-kvo-ab*.jsonl, .runtime/phase52-dev/). These checks
hold the shell's source to that, and the release text to the person's rules, the same in source and frozen modes:
  - nothing takes Pebble out of the person's view: no capture guard, no window-server alpha write, no app hide, no held
    workspace, no out-of-view tooltip, and no capture mask written anywhere; the companion's click-through alpha stays;
  - Stealth Lockdown sets the sharing type on every window when it switches, and a main-run-loop observer keeps it on
    every window just before each CoreAnimation commit, in every run-loop mode, only while Stealth Lockdown is on, read
    first, set only when it differs, read back, a refusing window retried at most once a second; it changes nothing
    else;
  - Lockdown's non-activating panels hold when AppKit observes the windows' keys: the panel carries a notifying setter
    for contentView, opaque and level, and for any other set<Key>: a KVO subclass overrides that NSPanel implements with
    one argument of a supported type; anything else is still refused; a set is never dropped; and the click that gives
    a box the keyboard is never taken by macOS's correction bubble (D242): while Stealth Lockdown is on Pebble's five text
    boxes turn spelling, correction, capitalisation, autocomplete and writing suggestions off (normal mode unchanged),
    and Stealth's switch dismisses a bubble already up;
  - the other halves of Stealth Lockdown are unchanged;
  - every version declaration says 2.0.3, whose notes have no Highlights (About's What's new stays 2.0.0's), carry
    Stealth Lockdown's honest line word for word (D239) and none of the words its public text never uses, and say that
    2.0.2 was not released, which has no entry; the README's badge and counts name 2.0.3.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from validate_runtime import ROOT  # noqa: E402
from validate_phase13 import bounded, owned_backend, sha  # noqa: E402
from validate_phase20 import write_new  # noqa: E402
from validate_phase21 import checkout_version, declared_versions  # noqa: E402
from validate_phase47 import changelog_entry, flat  # noqa: E402
from validate_phase49 import HONEST, NEVER, before, function, module  # noqa: E402

RELEASE = '2.0.3'
PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'pebble_stays_in_the_persons_view',
    'stealth_sets_and_keeps_the_sharing_type_on_every_window',
    'lockdown_panels_hold_when_accessibility_observes_the_windows',
    'the_other_halves_of_stealth_lockdown_are_unchanged',
    'release_version_is_2_0_3_with_its_notes',
    'readme_names_2_0_3',
    'backend_inputs_stable_during_campaign',
)
READS = ('src-tauri/src/main.rs', 'CHANGELOG.md', 'README.md', 'src/App.vue', 'src/Pet.vue', 'backend/pebble/__init__.py',
         'scripts/validate_phase52.py')
# CoreAnimation's own commit observer runs at this order on the main run loop (measured on macOS 27.0); the keeper must
# run before it.
COMMIT_ORDER = 2_000_000
# What 2.0.1's guard and 2.0.2's mask used; none of it may come back (D229, D237, D238).
GONE = ('mod capture_guard', 'capture_guard::', 'CGSSetWindowAlpha', 'SLSSetWindowAlpha', 'IsScreenWatcherPresent',
        'msg_send![ns_app, hide:', 'hide: std::ptr::null', 'unhideWithoutActivation', 'setDisableUpdateMask',
        'CAPTURE_MASK', 'set_capture_mask_all')
NOTIFIED = ('const NOTIFIED: [(&str, &str); 3] = [("setContentView:", "contentView"), ("setOpaque:", "opaque"), '
            '("setLevel:", "level")];')
# The argument types a carried setter passes on, as the runtime encodes them.
CARRIED_TYPES = ("Some(b'@') => std::mem::transmute(notify_object as SetObject),",
                 "Some(b'B' | b'c') => std::mem::transmute(notify_bool as SetFlag),",
                 "Some(b'q' | b'l') => std::mem::transmute(notify_isize as SetSigned),",
                 "Some(b'Q' | b'L') => std::mem::transmute(notify_usize as SetUnsigned),",
                 "Some(b'd') => std::mem::transmute(notify_f64 as SetDouble),",
                 '_ if kind.starts_with("{CGSize=") => std::mem::transmute(notify_size as SetSize),',
                 '_ if kind.starts_with("{CGPoint=") => std::mem::transmute(notify_point as SetPoint),',
                 '_ if kind.starts_with("{CGRect=") => std::mem::transmute(notify_rect as SetRect),')


# D242 (F13): what the five boxes carry while Stealth Lockdown is on, and how each box is found.
STEALTH_TYPING = ('{ spellcheck: false, autocorrect: "off", autocapitalize: "off", autocomplete: "off", '
                  'writingsuggestions: "false" }')
BOXES = {'src/Pet.vue': ('aria-label="Terminal command"', 'aria-label="Message Pebble"'),
         'src/App.vue': ('aria-label="Edited message"', 'ref="composer"', 'class="terminal-input"')}
KEYED_ON = {'src/Pet.vue': 'stealthOn.value', 'src/App.vue': 'state.value?.settings?.stealth'}


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def body(text, start, end):
    """The text from the line holding `start` up to (not including) the next line holding `end`."""
    assert start in text, ('missing', start)
    rest = text[text.index(start):]
    assert end in rest, ('missing', end)
    return rest[:rest.index(end)]


def item(text, name):
    """A function inside a module (four-space indent), up to the next item, comment or attribute at that indent."""
    match = re.search(r'^    (?:pub )?(?:extern "C(?:-unwind)?" )?fn ' + re.escape(name) + r'\(.*?'
                      r'(?=^    (?:pub |fn |extern |unsafe |static |const |type |struct |#\[|//)|\Z)', text, re.M | re.S)
    assert match, 'no fn ' + name
    return match.group(0)


def check_in_view(shell):
    for gone in GONE:
        assert gone not in shell, ('the shell can take Pebble out of view or mask it:', gone)
    reveal = function(shell, 'reveal')
    assert 'capture' not in reveal and 'reveal_held' not in reveal, 'reveal holds the workspace for a capture'
    tooltip = function(shell, 'tray_tooltip')
    assert 'out of view' not in tooltip and 'captured' not in tooltip, 'the tooltip says Pebble is out of view'
    passthrough = function(shell, 'pet_passthrough')
    assert 'setAlphaValue: if on {0.0f64} else {1.0f64}' in passthrough, "the companion's click-through alpha is gone"
    assert 'keep_out' not in passthrough, 'the companion is kept out of view'
    return {'guard': 'none', 'mask_writes': 0, 'alpha_writes': 0, 'app_hide': 0, 'click_through_alpha': 'kept'}


def check_sharing(shell):
    apply = function(shell, 'apply_stealth')
    assert apply.count('stealth_mac::set_sharing_all(on);') == 1, 'Stealth does not set the sharing type once'
    assert 'let _=window.set_content_protected(on);' in apply, "the labelled windows lose Tauri's own call"
    stealth = module(shell, 'stealth_mac')
    switch = item(stealth, 'set_sharing_all')
    before(switch, 'msg_send![app, windows]', 'for index in 0..count {')
    assert 'msg_send![window, setSharingType: if on { 0usize } else { 1usize }]' in switch, 'not none on and read-only off'
    keeper = module(shell, 'capture_keeper')
    assert 'const BEFORE_WAITING: usize = 1 << 5;' in keeper and 'const EXIT: usize = 1 << 7;' in keeper
    order = re.search(r'const ORDER: isize = ([\d_]+);', keeper)
    assert order and 0 < int(order.group(1).replace('_', '')) < COMMIT_ORDER, 'the keeper does not run before the commit'
    assert ('CFRunLoopObserverCreate(std::ptr::null(), BEFORE_WAITING | EXIT, 1, ORDER, before_commit, '
            'std::ptr::null_mut())') in keeper, 'the keeper is not a repeating observer before each commit'
    assert 'CFRunLoopAddObserver(CFRunLoopGetMain(), observer, kCFRunLoopCommonModes)' in keeper, 'not in every mode'
    callback = body(keeper, 'extern "C" fn before_commit(', '\n    }\n')
    before(callback, 'if !super::stealth_on() { return; }', 'std::panic::catch_unwind(')
    before(callback, 'std::panic::catch_unwind(', 'let due = last.is_none_or(|at| at.elapsed() >= FORCE_EVERY);')
    before(callback, 'let due = last.is_none_or(|at| at.elapsed() >= FORCE_EVERY);',
           'let sharing = super::stealth_mac::keep_sharing(force);')
    # Sharing only (D241): the keeper changes nothing but each window's sharing type.
    assert re.findall(r'super::stealth_mac::(\w+)\(', callback) == ['keep_sharing'], 'the keeper does more than sharing'
    assert 'const FORCE_EVERY: Duration = Duration::from_secs(1);' in keeper, 'the sharing type is not forced once a second'
    assert 'if STARTED.swap(true, Ordering::SeqCst) { return; }' in keeper, 'the keeper can start twice'
    assert shell.count('capture_keeper::start();') == 1, 'the keeper is not started once at launch'
    keep = item(stealth, 'keep_sharing')
    before(keep, 'let current: usize = msg_send![window, sharingType];', 'if current != 0 || force {')
    before(keep, 'if current != 0 || force {', 'if held_back(number as usize) { continue; }')
    before(keep, 'if held_back(number as usize) { continue; }', 'msg_send![window, setSharingType: 0usize]')
    before(keep, 'msg_send![window, setSharingType: 0usize]', 'let now: usize = msg_send![window, sharingType];')
    assert 'if now != 0 { refuse(number as usize); } else if current != 0 { changed += 1; }' in keep, \
        'a refusing window is not held back'
    assert 'setSharingType: 0usize' in keep and keep.count('setSharingType') == 1, 'the keeper sets another sharing type'
    assert 'const RETRY: Duration = Duration::from_secs(1);' in stealth, 'a refusal is retried before every commit'
    return {'switch': 'set_sharing_all(on) on every window', 'keeper': 'main run loop, common modes, before the commit',
            'order': int(order.group(1).replace('_', '')), 'before_commit_order': COMMIT_ORDER, 'only_while_on': True,
            'keeps': 'sharing type only', 'refusal_retry_s': 1}


def textareas(text):
    """Each <textarea> element's opening tag and content, self-closing (<textarea ... />) or not (... </textarea>)."""
    out = []
    for start in (m.start() for m in re.finditer(r'<textarea\b', text)):
        ends = [i for i in (text.find('/>', start), text.find('</textarea>', start)) if i >= 0]
        out.append(text[start:min(ends)] if ends else text[start:])
    return out


def check_typing(views):
    """D242 (F13): no correction bubble while Stealth Lockdown is on, in every box Pebble types into."""
    for rel, text in views.items():
        computed = re.search(r'const stealthTyping = computed\(\(\) =>\s*(.+?)\s*\?\s*(\{[^}]*\})\s*:\s*(\{\s*\}),?\s*\);', text, re.S)
        assert computed, (rel, 'has no Stealth-keyed stealthTyping')
        assert computed.group(1) == KEYED_ON[rel], (rel, 'stealthTyping is not keyed on Stealth Lockdown', computed.group(1))
        assert computed.group(2) == STEALTH_TYPING, (rel, 'stealthTyping lost an attribute', computed.group(2))
        boxes = textareas(text)
        for marker in BOXES[rel]:
            found = [b for b in boxes if marker in b]
            assert len(found) == 1, (rel, 'no single box', marker)
            assert 'v-bind="stealthTyping"' in found[0], (rel, 'a box keeps macOS correction under Stealth:', marker)
    return {'boxes': sum(len(v) for v in BOXES.values()), 'off_while_stealth': ['spellcheck', 'autocorrect', 'autocapitalize',
                                                                               'autocomplete', 'writingsuggestions']}


def check_panels(shell, views):
    typing = check_typing(views)
    apply = function(shell, 'apply_stealth')
    on = body(apply, '        if on {\n            for label in ["pet", "main"] {', '\n        }\n')
    before(on, 'msg_send![objc2::class!(NSSpellChecker), sharedSpellChecker]', 'msg_send![checker, dismissCorrectionIndicatorForView: view]')
    assert 'webview.inner()' in on, "the bubble is not dismissed for the window's web view"
    stealth = module(shell, 'stealth_mac')
    assert NOTIFIED in stealth, 'the panel does not carry contentView, opaque and level'
    assert 'const KVO_OWN: [&str; 3] = ["class", "dealloc", "_isKVOA"];' in stealth, "KVO's own bookkeeping changed"
    panel = item(stealth, 'panel_class')
    assert '(sel!(setLevel:), std::mem::transmute(set_level as SetLevel))' in panel, 'the panel has no level setter'
    level = item(stealth, 'set_level')
    assert 'notified(this, sel, |imp|' in level, 'the level setter does not notify'
    notified = item(stealth, 'notified')
    before(notified, 'NOTIFIED.iter().find(', '.or_else(|| carried_key(&selector))')
    some = body(notified, 'Some(key) => {', 'None => {')
    before(some, 'willChangeValueForKey', 'call(imp);')
    before(some, 'call(imp);', 'didChangeValueForKey')
    none = body(notified, 'None => {', '\n            }\n')
    assert 'call(imp);' in none, 'a set without a key is dropped'
    key = item(stealth, 'key_for_setter')
    for part in ('let body = name.strip_prefix("set")?.strip_suffix(\':\')?;',
                 "if !first.is_ascii_uppercase() || body.contains(':') { return None; }",
                 'if chars.next().is_some_and(|second| second.is_ascii_uppercase()) { return Some(body.to_string()); }',
                 'Some(first.to_ascii_lowercase().to_string() + &body[first.len_utf8()..])'):
        assert part in key, ("the setter's key is not KVO's", part)
    carry = item(stealth, 'carry_setter')
    before(carry, 'let Some(key) = key_for_setter(&selector) else { return false };', 'ffi::class_getInstanceMethod(ns_panel, sel)')
    assert 'method.is_null() || ffi::method_getNumberOfArguments(method) != 3' in carry, 'not one argument'
    assert "if returns[0] as u8 != b'v' { return false; }" in carry, 'not a void setter'
    for kind in CARRIED_TYPES:
        assert kind in carry, ('an argument type is not passed on', kind)
    assert '_ => return false,' in carry, 'an unsupported argument type is carried'
    before(carry, 'CARRIED.lock().unwrap().get_or_insert_with(HashMap::new).insert(selector.clone(), key.clone());',
           'ffi::class_addMethod(panel as *const AnyClass as *mut AnyClass, sel, imp, ffi::method_getTypeEncoding(method))')
    assert stealth.count('ffi::class_addMethod(') == stealth.count('ffi::class_addMethod(cls,') + 1, \
        "a setter is added to a class other than Pebble's own panel"
    swappable = item(stealth, 'swappable')
    assert 'fn swappable(class: *const AnyClass, tao: &AnyClass, panel: &AnyClass)' in swappable
    before(swappable, 'carried_key(&n).is_none()', '.filter(|sel| !carry_setter(panel, *sel))')
    assert 'if extra.is_empty() { Ok(()) } else { Err(extra) }' in swappable, 'what cannot be carried does not block'
    original = item(stealth, 'as_original')
    before(original, 'let keep = swappable(now, tao, stealth_panel).is_ok();', 'if keep {\n                ffi::object_setClass(ns, panel);')
    assert 'match swappable(class, tao, panel) {' in item(stealth, 'set_panel'), 'set_panel does not carry setters'
    return {'notified': ['contentView', 'opaque', 'level'], 'carried_types': len(CARRIED_TYPES), 'unsupported': 'refused',
            'never_dropped': True, 'correction_bubble': 'off while Stealth is on, dismissed at the switch', **typing}


def check_halves(shell):
    apply = function(shell, 'apply_stealth')
    for kept in ('STEALTH.store(on, Ordering::SeqCst);', 'let _=window.set_content_protected(on);',
                 'let _=window.set_shadow(!on);', 'stealth_mac::set_mission_control(label, ns, on);',
                 'else { let _=stealth_mac::set_panel(ns, on); }', 'if on && !deferred { yield_on_this_space(); }',
                 'if !on { wake_outbox(app); }', 'tray_state(app, on);'):
        assert kept in apply, ('Stealth Lockdown lost a half:', kept)
    stealth = module(shell, 'stealth_mac')
    assert 'msg_send![ns, _setPreventsActivation: Bool::new(on)]' in stealth, 'the panels can activate Pebble'
    assert 'Some("settings") { stealth_follow(&handle); }' in shell, 'a Stealth change from elsewhere misses the shell'
    before(function(shell, 'start_notification_pump'), 'if stealth_on() { continue; }', 'native_call(&backend, "notification_native_pending"')
    return {'kept': ['content protection', 'shadow', 'Mission Control', 'panels', 'Space yield', 'banner hold',
                     'menu-bar state', 'Stealth from anywhere']}


def check_release(entry, changelog, app):
    current = checkout_version()
    assert current == RELEASE, current
    declared = declared_versions()
    assert declared and set(declared.values()) == {RELEASE}, declared
    assert not re.search(r'^## \[2\.0\.2\]', changelog, re.M), '2.0.2 was not released, yet CHANGELOG.md has an entry for it'
    assert '### Highlights' not in entry, "2.0.3 has Highlights, so About's What's new would change"
    assert re.search(r'const WHATS_NEW_VERSION = "2\.0\.0";', app), "About's What's new is not 2.0.0's"
    text = flat(entry)
    assert HONEST in text, 'the 2.0.3 notes name Stealth Lockdown without its honest line'
    lowered = text.lower()
    for word in NEVER:
        assert word not in lowered, ('the 2.0.3 notes say', word)
    for said in ('2.0.2 was not released.', 'Stealth Lockdown keeps Pebble on your screen.', 'your typing stays with Pebble',
                 'no longer makes it the active app', 'Install 2.0.3 over'):
        assert said in text, ('the 2.0.3 notes do not say', said)
    return {'version': current, 'declarations': len(declared), 'no_2_0_2_entry': True}


def check_readme(readme):
    assert 'at%20v2.0.3' in readme and 'at v2.0.3' in readme, 'the README badge is not 2.0.3'
    assert re.search(r'^2\.0\.3 passed [\d,]+ tests and [\d,]+ service checks', readme, re.M), 'the README counts are not 2.0.3'
    assert re.search(r'^\| \*\*Tests\*\* \| [\d,]+ automated tests at v2\.0\.3,', readme, re.M), 'the Tests row is not 2.0.3'
    return {'badge': 'v2.0.3', 'counts': '2.0.3'}


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


class Campaign:
    def __init__(self, scratch, backend=None, only=None):
        self.scratch, self.backend, self.results, self.only = scratch, backend, [], only

    def check(self, name, fn):
        assert name in PLANNED and name not in {r['name'] for r in self.results}, name
        if self.only is not None and name not in self.only:
            return
        began = time.monotonic()
        try:
            evidence = fn() or {}
            self.results.append({'name': name, 'passed': True, 'evidence': evidence,
                                 'seconds': round(time.monotonic() - began, 2)})
        except Exception as exc:
            self.results.append({'name': name, 'passed': False, 'error': bounded(exc, 1500),
                                 'trace': traceback.format_exc(limit=3)[-1200:].replace(str(ROOT), '<checkout>')})

    def run(self):
        scratch = self.scratch

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            return {'outside_checkout': True}
        self.check('fixture_scratch_created_outside_the_checkout', created)

        shell = read('src-tauri/src/main.rs')
        readme, app, changelog = read('README.md'), read('src/App.vue'), read('CHANGELOG.md')
        self.check('pebble_stays_in_the_persons_view', lambda: check_in_view(shell))
        self.check('stealth_sets_and_keeps_the_sharing_type_on_every_window', lambda: check_sharing(shell))
        views = {rel: read(rel) for rel in BOXES}
        self.check('lockdown_panels_hold_when_accessibility_observes_the_windows', lambda: check_panels(shell, views))
        self.check('the_other_halves_of_stealth_lockdown_are_unchanged', lambda: check_halves(shell))
        self.check('release_version_is_2_0_3_with_its_notes',
                   lambda: check_release(changelog_entry(RELEASE, changelog), changelog, app))
        self.check('readme_names_2_0_3', lambda: check_readme(readme))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary folder even on success')
    args = parser.parse_args(argv)
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; choose a new path (earlier raw evidence is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    backend_sha = sha(backend.read_bytes()) if backend else None
    before_run = digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase52-', dir='/private/tmp')).resolve()
    started = time.monotonic()
    campaign = Campaign(scratch, backend)
    fatal = None
    try:
        campaign.run()
    except Exception as exc:
        fatal = bounded(exc, 1500)
    after = digests()
    stable = before_run == after and (not backend or sha(backend.read_bytes()) == backend_sha)
    campaign.results.append({'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                             'evidence': {'files_compared': len(before_run), 'executable_compared': bool(backend)}})
    recorded = {r['name'] for r in campaign.results}
    failed = [r['name'] for r in campaign.results if not r['passed']] + [n for n in PLANNED if n not in recorded]
    passed = fatal is None and not failed and recorded == set(PLANNED)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source', 'passed': passed,
        'checks': [r['name'] for r in campaign.results if r['passed']], 'failed': failed, 'planned_checks': list(PLANNED),
        'check_results': campaign.results, 'fatal_error': fatal,
        'duration_seconds': round(time.monotonic() - started, 1),
        'backend_sha256': backend_sha, 'source_sha256': before_run,
        'source_note': 'Digests of the tested sources this validator reads; the campaign binds them to its snapshot.',
    }
    if passed and not args.keep_scratch:
        shutil.rmtree(scratch, ignore_errors=True)
    else:
        report['scratch_preserved'] = str(scratch)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_new(report_path, json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']), 'failed': failed,
                      'fatal_error': fatal}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
