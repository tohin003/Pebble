#!/usr/bin/env python3
"""Phase 51 service validator: 2.0.2, Stealth Lockdown that keeps Pebble with the person (D229-D234; .runtime/phase51-plan.md).

2.0.1 kept Pebble out of some recordings by taking it out of the person's own view, and their typing could then reach
the app behind it; the person rejected that (D229). 2.0.2 keeps every Pebble window on the person's screen and leaves it
out of screen recordings, screen sharing and screenshots with CoreAnimation's capture mask on each window's root layer,
beside the window's sharing type (measured on macOS 27.0 with stand-in windows and on the real app, .runtime/phase51-dev/).
These checks hold the shell's source to what was measured, and the release text to the person's rules, the same in
source and frozen modes:
  - nothing in the shell takes Pebble out of the person's view for a capture: no capture guard, no window-server alpha
    write, no app hide, no hold on the workspace, no tooltip saying Pebble is out of view; the companion's own
    click-through alpha (F085, F122) is kept;
  - the mask is the measured value, set on every window's frame-view layer and content-view layer when the property
    exists, read first and set only when it differs, read back, and cleared when Stealth Lockdown is off;
  - Stealth Lockdown applies it right after the sharing type and records what it set;
  - a main-run-loop observer keeps the sharing type and the mask on every window just before each CoreAnimation commit,
    in every run-loop mode, only while Stealth Lockdown is on, so windows AppKit adds carry them; it starts at launch;
  - the other halves of Stealth Lockdown are unchanged: content protection, Mission Control, the non-activating panels,
    the shadow, the Space yield, a Stealth change from anywhere, the held banners;
  - every version declaration says 2.0.2, whose notes have no Highlights (About's What's new stays 2.0.0's) and carry
    Stealth Lockdown's honest line, with none of the words its public text never uses; the README's badge and counts
    name 2.0.2.

Restated for 2.0.3 (Phase 52, D240), in D226's pattern. 2.0.2's capture mask hid Pebble from the person's own display
too (D237), so 2.0.2 was never released, and the person chose 2.0.0's capture behaviour for 2.0.3 (D238): each window's
sharing type, kept on every window by the same main-run-loop keeper while Stealth Lockdown is on (D241). The mask, apply,
keeper and release checks pass for 2.0.2 as pinned, or for a release of 2.0.3 or later in which that behaviour stands: no
capture mask written anywhere, Stealth Lockdown setting the sharing type on every window once when it switches, the
keeper keeping only the sharing type, and the release's own notes (every declaration the same, the honest line, none of
the never-used words, no Highlights; 2.0.2 needs no entry). The README check follows the checkout's release. The view
and halves checks are unchanged. Each check keeps its name; validate_phase52 pins 2.0.3.
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

RELEASE = '2.0.2'
PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'pebble_never_steps_out_of_the_persons_view',
    'the_capture_mask_is_set_on_every_window',
    'stealth_applies_the_mask_beside_the_sharing_type',
    'the_mask_is_kept_before_every_commit_while_stealth_is_on',
    'the_other_halves_of_stealth_lockdown_are_unchanged',
    'release_version_is_2_0_2_with_its_notes',
    'backend_inputs_stable_during_campaign',
)
READS = ('src-tauri/src/main.rs', 'CHANGELOG.md', 'README.md', 'src/App.vue', 'backend/pebble/__init__.py',
         'scripts/validate_phase51.py')
# CoreAnimation's own commit observer runs at this order on the main run loop (measured on macOS 27.0); the keeper must
# run before it.
COMMIT_ORDER = 2_000_000


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def d238():
    """Restated for 2.0.3 (D240): a release of 2.0.3 or later, where D238's 2.0.0 capture behaviour stands."""
    return tuple(int(part) for part in checkout_version().split('.')) >= (2, 0, 3)


def sharing_only(shell):
    """Restated for 2.0.3 (D240): no capture mask anywhere, and the sharing type set on every window."""
    for gone in ('setDisableUpdateMask', 'CAPTURE_MASK', 'set_capture_mask_all', 'mod capture_guard', 'capture_guard::'):
        assert gone not in shell, ('a capture mask or guard is back:', gone)
    stealth = module(shell, 'stealth_mac')
    switch = body(stealth, 'pub fn set_sharing_all(on: bool) {', '\n    }\n')
    before(switch, 'msg_send![app, windows]', 'for index in 0..count {')
    assert 'msg_send![window, setSharingType: if on { 0usize } else { 1usize }]' in switch, 'not none on and read-only off'
    return stealth


def body(text, start, end):
    """The text from the line holding `start` up to (not including) the next line holding `end`."""
    assert start in text, ('missing', start)
    rest = text[text.index(start):]
    assert end in rest, ('missing', end)
    return rest[:rest.index(end)]


def check_in_view(shell):
    for gone in ('mod capture_guard', 'capture_guard::', 'CGSSetWindowAlpha', 'SLSSetWindowAlpha', 'IsScreenWatcherPresent',
                 'msg_send![ns_app, hide:', 'hide: std::ptr::null', 'unhideWithoutActivation'):
        assert gone not in shell, ('the shell can still take Pebble out of view:', gone)
    reveal = function(shell, 'reveal')
    assert 'capture' not in reveal and 'reveal_held' not in reveal, 'reveal holds the workspace for a capture'
    tooltip = function(shell, 'tray_tooltip')
    assert 'out of view' not in tooltip and 'captured' not in tooltip, 'the tooltip says Pebble is out of view'
    passthrough = function(shell, 'pet_passthrough')
    assert 'setAlphaValue: if on {0.0f64} else {1.0f64}' in passthrough, "the companion's click-through alpha is gone"
    assert 'keep_out' not in passthrough, 'the companion is kept out of view'
    return {'guard': 'none', 'alpha_writes': 0, 'app_hide': 0, 'click_through_alpha': 'kept'}


def check_mask(shell):
    if d238():
        sharing_only(shell)
        return {'mask': 'none (D238)', 'release': checkout_version(), 'sharing_type': 'every window'}
    stealth = module(shell, 'stealth_mac')
    assert 'pub const CAPTURE_MASK: u32 = 0x9;' in stealth, 'the capture mask is not the measured value'
    mask = body(stealth, 'pub fn set_capture_mask_all(on: bool) -> (usize, usize, bool) {', '\n    }\n')
    assert 'let wanted: u32 = if on { CAPTURE_MASK } else { 0 };' in mask, 'the mask is not 0x9 on and 0 off'
    before(mask, 'msg_send![app, windows]', 'msg_send![window, contentView]')
    before(mask, 'msg_send![content, superview]', 'msg_send![frame, layer]')
    assert 'msg_send![content, layer]' in mask and 'for layer in [root, own] {' in mask, 'not both root and content layers'
    before(mask, 'respondsToSelector: sel!(setDisableUpdateMask:)', 'msg_send![layer, setDisableUpdateMask: wanted]')
    assert 'if !responds.as_bool() { continue; }' in mask, 'a runtime without the property is not refused'
    before(mask, 'let current: u32 = msg_send![layer, disableUpdateMask];', 'if current != wanted {')
    before(mask, 'if current != wanted {', 'msg_send![layer, setDisableUpdateMask: wanted]')
    before(mask, 'msg_send![layer, setDisableUpdateMask: wanted]', 'let now: u32 = msg_send![layer, disableUpdateMask];')
    assert 'readback &= now == wanted;' in mask, 'the mask is not read back'
    return {'mask': '0x9', 'off': '0', 'layers': ['frame view', 'content view'], 'read_first': True, 'read_back': True}


def check_apply(shell):
    apply = function(shell, 'apply_stealth')
    if d238():
        sharing_only(shell)
        assert apply.count('stealth_mac::set_sharing_all(on);') == 1, 'Stealth does not set the sharing type once'
        assert 'let _=window.set_content_protected(on);' in apply, "the labelled windows lose Tauri's own call"
        return {'after': 'nothing: the sharing type alone (D238)', 'release': checkout_version()}
    before(apply, '        stealth_mac::set_sharing_all(on);\n', 'stealth_mac::set_capture_mask_all(on);')
    before(apply, 'stealth_mac::set_capture_mask_all(on);',
           'diag::log("stealth_capture_mask", json!({"on": on, "windows": windows, "layers": layers, "readback": readback}));')
    assert apply.count('set_capture_mask_all(') == 1, 'Stealth applies the mask more than once'
    return {'after': 'set_sharing_all(on)', 'logged': 'stealth_capture_mask'}


def check_keeper(shell):
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
    before(callback, 'let due = last.is_none_or(|at| at.elapsed() >= FORCE_EVERY);', 'super::stealth_mac::keep_sharing(force)')
    if d238():
        # Restated for 2.0.3 (D240, D241): the keeper keeps the sharing type alone.
        sharing_only(shell)
        assert re.findall(r'super::stealth_mac::(\w+)\(', callback) == ['keep_sharing'], 'the keeper does more than sharing'
    else:
        before(callback, 'super::stealth_mac::keep_sharing(force)', 'super::stealth_mac::set_capture_mask_all(true)')
    assert 'const FORCE_EVERY: Duration = Duration::from_secs(1);' in keeper, 'the sharing type is not forced once a second'
    assert 'if STARTED.swap(true, Ordering::SeqCst) { return; }' in keeper, 'the keeper can start twice'
    assert shell.count('capture_keeper::start();') == 1, 'the keeper is not started once at launch'
    stealth = module(shell, 'stealth_mac')
    sharing = body(stealth, 'pub fn keep_sharing(force: bool) -> usize {', '\n    }\n')
    before(sharing, 'let current: usize = msg_send![window, sharingType];', 'if current != 0 || force {')
    before(sharing, 'if current != 0 || force {', 'msg_send![window, setSharingType: 0usize]')
    before(sharing, 'msg_send![window, setSharingType: 0usize]', 'let now: usize = msg_send![window, sharingType];')
    # A window or layer that refuses (AppKit's menu-bar icon windows keep their sharing type) is retried at most once a
    # second, not before every commit; Stealth off forgets every refusal.
    assert 'const RETRY: Duration = Duration::from_secs(1);' in stealth, 'a refusal is retried before every commit'
    if d238():
        # Restated for 2.0.3 (D240): windows only (no layer is ever set); a refusal lapses after its second.
        before(sharing, 'if held_back(number as usize) { continue; }', 'msg_send![window, setSharingType: 0usize]')
        assert 'if now != 0 { refuse(number as usize); } else if current != 0 { changed += 1; }' in sharing, \
            'a refusing window is not held back'
        assert 'refused.retain(|_, until| *until > now);' in stealth, 'old refusals accumulate'
        return {'observer': 'main run loop, common modes', 'activities': 'before waiting, exit',
                'order': int(order.group(1).replace('_', '')), 'before_commit_order': COMMIT_ORDER, 'only_while_on': True,
                'refusal_retry_s': 1, 'keeps': 'sharing type only (D241)'}
    before(sharing, 'if held_back((0, number as usize)) { continue; }', 'msg_send![window, setSharingType: 0usize]')
    assert 'if now != 0 { refuse((0, number as usize)); } else if current != 0 { changed += 1; }' in sharing, \
        'a refusing window is not held back'
    mask = body(stealth, 'pub fn set_capture_mask_all(on: bool) -> (usize, usize, bool) {', '\n    }\n')
    assert 'if !on { REFUSED.lock().unwrap().take(); }' in mask, 'Stealth off keeps old refusals'
    before(mask, 'if held_back((1, layer as usize)) { continue; }', 'msg_send![layer, setDisableUpdateMask: wanted]')
    assert 'if now != wanted { refuse((1, layer as usize)); }' in mask, 'a refusing layer is not held back'
    return {'observer': 'main run loop, common modes', 'activities': 'before waiting, exit',
            'order': int(order.group(1).replace('_', '')), 'before_commit_order': COMMIT_ORDER, 'only_while_on': True,
            'refusal_retry_s': 1}


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


def check_release(entry, app, readme):
    current = checkout_version()
    if d238():
        return check_later_release(current, entry, app, readme)
    assert current == RELEASE, current
    declared = declared_versions()
    assert declared and set(declared.values()) == {RELEASE}, declared
    assert '### Highlights' not in entry, "2.0.2 has Highlights, so About's What's new would change"
    assert re.search(r'const WHATS_NEW_VERSION = "2\.0\.0";', app), "About's What's new is not 2.0.0's"
    text = flat(entry)
    assert HONEST in text, 'the 2.0.2 notes name Stealth Lockdown without its honest line'
    lowered = text.lower()
    for word in NEVER:
        assert word not in lowered, ('the 2.0.2 notes say', word)
    for said in ('Stealth Lockdown keeps Pebble on your screen.', 'your typing stays with Pebble',
                 'Install 2.0.2 over 2.0.1'):
        assert said in text, ('the 2.0.2 notes do not say', said)
    assert 'at%20v2.0.2' in readme and 'at v2.0.2' in readme, 'the README badge is not 2.0.2'
    assert re.search(r'^2\.0\.2 passed [\d,]+ tests and [\d,]+ service checks', readme, re.M), 'the README counts are not 2.0.2'
    return {'version': current, 'declarations': len(declared)}


def check_later_release(current, entry, app, readme):
    """Restated for 2.0.3 (D240): the release's own notes, by the same rules; 2.0.2 needs no entry (never released)."""
    declared = declared_versions()
    assert declared and set(declared.values()) == {current}, declared
    assert '### Highlights' not in entry, current + " has Highlights, so About's What's new would change"
    assert re.search(r'const WHATS_NEW_VERSION = "2\.0\.0";', app), "About's What's new is not 2.0.0's"
    text = flat(entry)
    assert HONEST in text, 'the ' + current + ' notes name Stealth Lockdown without its honest line'
    lowered = text.lower()
    for word in NEVER:
        assert word not in lowered, ('the notes say', word)
    for said in ('Stealth Lockdown keeps Pebble on your screen.', 'your typing stays with Pebble', 'Install ' + current + ' over'):
        assert said in text, ('the ' + current + ' notes do not say', said)
    assert ('at%20v' + current) in readme and ('at v' + current) in readme, 'the README badge is not ' + current
    assert re.search(r'^' + re.escape(current) + r' passed [\d,]+ tests and [\d,]+ service checks', readme, re.M), \
        'the README counts are not ' + current
    return {'version': current, 'declarations': len(declared), 'restated': 'D240'}


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
        readme, app = read('README.md'), read('src/App.vue')
        self.check('pebble_never_steps_out_of_the_persons_view', lambda: check_in_view(shell))
        self.check('the_capture_mask_is_set_on_every_window', lambda: check_mask(shell))
        self.check('stealth_applies_the_mask_beside_the_sharing_type', lambda: check_apply(shell))
        self.check('the_mask_is_kept_before_every_commit_while_stealth_is_on', lambda: check_keeper(shell))
        self.check('the_other_halves_of_stealth_lockdown_are_unchanged', lambda: check_halves(shell))
        changelog = read('CHANGELOG.md')
        # Restated for 2.0.3 (D240): the entry is looked up inside the check, so a missing one fails it.
        self.check('release_version_is_2_0_2_with_its_notes', lambda: check_release(
            changelog_entry(checkout_version() if d238() else RELEASE, changelog), app, readme))


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase51-', dir='/private/tmp')).resolve()
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
