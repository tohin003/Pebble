#!/usr/bin/env python3
"""Phase 49 service validator: 2.0.1, Stealth Lockdown's capture guard (D225-D227; .runtime/phase49-plan.md).

A window's sharing type keeps it out of what macOS composes for a capture (ScreenCaptureKit, the one-shot capture
calls) but not out of a display stream, which is given the screen as it is drawn (measured on macOS 27.0 with stand-in
windows, .runtime/phase49-dev/). 2.0.1's shell steps Pebble out of view while Stealth Lockdown is on and the window
server reports a screen watcher that macOS did not announce as a ScreenCaptureKit session, applies a Stealth Lockdown
change made anywhere, and holds banners while it is on. These checks hold the shell's source to what was measured, and
the release text to the person's rules, the same in source and frozen modes:
  - the guard reads the window server's watcher flag every 2 ms on a user-interactive thread while Stealth Lockdown is
    on, takes every Pebble window's window-server alpha to 0 from that thread before anything else, then hides Pebble as
    an app on the main thread (yielding on this Space first), and brings it back without activating when the watcher
    goes or Stealth is off;
  - an announced ScreenCaptureKit session (notices 1327 and 1328) keeps the companion with the person; a lost end
    notice is dropped 3 s after the watcher goes;
  - nothing brings Pebble back while the guard holds (the workspace's reveal, the companion's click-through alpha), and
    the menu-bar icon's tooltip says why Pebble is out of view;
  - a Stealth change made anywhere reaches the shell once (the settings event re-reads the backend's state), and every
    path applies it only when it differs;
  - banners wait unpulled while Stealth Lockdown is on, and turning it off asks for them;
  - every version declaration says 2.0.1, whose notes have no Highlights (About's What's new stays 2.0.0's) and carry
    Stealth Lockdown's honest line, with none of the words its public text never uses;
  - the guide and the README name Stealth Lockdown's Settings tab and promise no keyboard focus.
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

RELEASE = '2.0.1'
PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'the_guard_steps_pebble_out_of_unannounced_captures',
    'nothing_brings_pebble_back_while_the_guard_holds',
    'a_stealth_change_from_anywhere_reaches_the_shell_once',
    'banners_wait_while_stealth_lockdown_is_on',
    'release_version_is_2_0_1_with_its_notes',
    'the_stealth_text_names_its_tab_and_keeps_its_line',
    'backend_inputs_stable_during_campaign',
)
READS = ('src-tauri/src/main.rs', 'CHANGELOG.md', 'README.md', 'docs/USER_GUIDE.md', 'src/App.vue',
         'backend/pebble/__init__.py', 'scripts/validate_phase49.py')
HONEST = ('Hidden from screen capture and screen sharing, not from a camera, a capture card or a process list. Exams '
          'and interviews that forbid assistants still forbid it.')
TOOLTIP = 'Pebble · out of view while the screen is being captured'
NEVER = ('undetectable', 'invisible', 'proctor', 'bypass', 'cheat', 'evade')
FOCUS = ('never takes focus', 'never takes keyboard focus', "won't take focus", 'never makes pebble the active app')


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def function(shell, name):
    """A top-level Rust function's text, from its fn line to the next line that starts a top-level item."""
    match = re.search(r'^fn ' + re.escape(name) + r'\(.*?(?=^(?:fn |#\[|static |struct |mod |const |// |/// |impl ))',
                      shell, re.M | re.S)
    assert match, 'main.rs has no top-level fn ' + name
    return match.group(0)


def module(shell, name):
    match = re.search(r'^mod ' + re.escape(name) + r' \{\n(.*?)^\}\n', shell, re.M | re.S)
    assert match, 'main.rs has no module ' + name
    return match.group(1)


def inner(text, name):
    """A function inside a module (four-space indent), up to the next item at that indent or the module's end."""
    match = re.search(r'^    (?:pub )?(?:unsafe extern "C" )?fn ' + re.escape(name) + r'\(.*?(?=^    (?:pub |fn |unsafe |static |const |///)|\Z)',
                      text, re.M | re.S)
    assert match, 'no fn ' + name
    return match.group(0)


def prose(text):
    """Markdown text as read: block-quote markers dropped, whitespace collapsed."""
    return flat(re.sub(r'^> ?', '', text, flags=re.M))


def before(text, first, second):
    assert first in text, ('missing', first)
    assert second in text, ('missing', second)
    assert text.index(first) < text.index(second), (first, 'must come before', second)


def check_guard(shell):
    guard = module(shell, 'capture_guard')
    for name in ('CGSIsScreenWatcherPresent', 'SLSIsScreenWatcherPresent', 'CGSSetWindowAlpha', 'CGSRegisterNotifyProc',
                 'CGSMainConnectionID'):
        assert 'c"' + name + '"' in guard, ('the guard does not look up', name)
    assert 'const SESSION_BEGAN: u32 = 1327;' in guard and 'const SESSION_ENDED: u32 = 1328;' in guard
    start = inner(guard, 'start')
    assert 'Duration::from_millis(if stealth { 2 } else { 100 })' in start, 'the guard no longer polls every 2 ms'
    before(start, 'pthread_set_qos_class_self_np(QOS_USER_INTERACTIVE, 0)', 'loop {')
    assert 'const QOS_USER_INTERACTIVE: u32 = 0x21;' in guard, 'the guard thread is not user-interactive'
    assert '} else if stealth && watched && !announced() {' in start, 'the guard holds for something other than an unannounced watcher'
    assert 'if !stealth || !watched {' in start, 'the guard does not lift when the watcher goes or Stealth is off'
    before(start, '(api.alpha)(connection, *number, 0.0);', 'step_out(&handle')
    assert 'began.elapsed() < Duration::from_secs(3)' in start and 'if !watched {' in start, 'a lost end notice is kept'
    notice = inner(guard, 'notice')
    assert 'SESSION_ENDED => sessions.retain(' in notice and 'SESSION_BEGAN =>' in notice
    step = inner(guard, 'step_out')
    before(step, 'super::yield_on_this_space();', 'msg_send![ns_app, hide: std::ptr::null::<AnyObject>()];')
    assert 'HIDDEN_BY_GUARD.store(true' in step and 'if !ENGAGED.load(Ordering::SeqCst) { return; }' in step
    back = inner(guard, 'come_back')
    assert 'msg_send![ns_app, unhideWithoutActivation]' in back, 'Pebble comes back activating'
    assert 'let alpha: f64 = msg_send![window, alphaValue];' in back and '(api.alpha)(connection, number as u32, alpha as f32);' in back
    assert 'if ENGAGED.load(Ordering::SeqCst) { return; }' in back
    apply = function(shell, 'apply_stealth')
    before(apply, '        stealth_mac::set_sharing_all(on);\n', 'capture_guard::refresh();')
    assert 'capture_guard::start(app.handle());' in shell, 'the guard is never started'
    return {'poll_ms': 2, 'qos': 'user-interactive', 'notices': [1327, 1328], 'stale_notice_s': 3}


def check_held(shell):
    reveal = function(shell, 'reveal')
    before(reveal, 'if capture_guard::engaged() {', 'WORKSPACE_WANTED.store(true, Ordering::SeqCst);')
    assert 'if capture_guard::engaged() { diag::log("reveal_held", json!({})); return; }' in reveal, 'reveal goes on while the guard holds'
    passthrough = function(shell, 'pet_passthrough')
    before(passthrough, 'setAlphaValue: if on {0.0f64} else {1.0f64}', 'capture_guard::keep_out(')
    tooltip = function(shell, 'tray_tooltip')
    assert 'if capture_guard::engaged() { return "' + TOOLTIP + '"; }' in tooltip, 'the tooltip does not say why'
    assert 'tray.set_tooltip(Some(tray_tooltip()))' in function(shell, 'remote_mark')
    guard = module(shell, 'capture_guard')
    for name in ('step_out', 'come_back'):
        assert 'tray.set_tooltip(Some(super::tray_tooltip()))' in inner(guard, name), name
    return {'tooltip': TOOLTIP}


def check_follow(shell):
    assert 'Some("settings") { stealth_follow(&handle); }' in shell, 'a settings event does not reach the shell'
    follow = function(shell, 'stealth_follow')
    before(follow, 'backend.call("state"', 'pointer("/settings/stealth")')
    before(follow, 'if stealth_on()!=wanted {', 'apply_stealth(&shown, wanted);')
    assert 'if stealth_on()!=enabled { apply_stealth(&handle, enabled) }' in function(shell, 'stealth_set')
    toggle = function(shell, 'stealth_toggle')
    before(toggle, 'backend.call("setting"', 'if stealth_on()!=next { apply_stealth(&shown, next) }')
    assert 'run_on_main_thread(move || if !stealth_on() { apply_stealth(&shown, true) });' in shell, 'the launch applies twice'
    return {'paths': ['settings event', 'switch', 'keys and menu', 'launch']}


def check_banners(shell):
    pump = function(shell, 'start_notification_pump')
    before(pump, 'while events.try_recv().is_ok() {}', 'if stealth_on() { continue; }')
    before(pump, 'if stealth_on() { continue; }', 'native_call(&backend, "notification_native_pending"')
    apply = function(shell, 'apply_stealth')
    assert 'if !on { wake_outbox(app); }' in apply, 'turning Stealth off does not ask for the banners that waited'
    assert shell.count('if app_active() || stealth_on() { return; }') == 2, 'reply banners or remote notices show in Stealth'
    return {'held': ['scheduled and approval banners'], 'never_shown': ['reply banners', 'remote notices']}


def check_release(entry, app, readme):
    current = checkout_version()
    assert current == RELEASE, current
    declared = declared_versions()
    assert declared and set(declared.values()) == {RELEASE}, declared
    assert '### Highlights' not in entry, "2.0.1 has Highlights, so About's What's new would change"
    assert re.search(r'const WHATS_NEW_VERSION = "2\.0\.0";', app), "About's What's new is not 2.0.0's"
    text = flat(entry)
    assert HONEST in text, 'the 2.0.1 notes name Stealth Lockdown without its honest line'
    lowered = text.lower()
    for word in NEVER:
        assert word not in lowered, ('the 2.0.1 notes say', word)
    for fixed in ('Stealth Lockdown, hardened.', 'made from Pebble Remote now applies on the Mac at once',
                  'Banners wait while Stealth Lockdown is on.', 'Pebble for Chrome from another macOS user.',
                  'https://github.com/tohin003/pebble-remote'):
        assert fixed in text, ('the 2.0.1 notes do not say', fixed)
    assert 'at%20v2.0.1' in readme and 'at v2.0.1' in readme, 'the README badge is not 2.0.1'
    assert re.search(r'^2\.0\.1 passed [\d,]+ tests and [\d,]+ service checks', readme, re.M), 'the README counts are not 2.0.1'
    return {'version': current, 'declarations': len(declared)}


def check_text(shell, readme, guide, app):
    stealth = readme[readme.index('### Stealth Lockdown'):]
    stealth = stealth[:stealth.index('\n---\n')]
    assert 'Settings → Privacy & Stealth' in stealth and 'Privacy and focus' not in stealth, 'the README names another tab'
    part = guide[guide.index('## Stealth Lockdown'):]
    part = part[:part.index('\n## ', 1)]
    assert '**Settings › Privacy & Stealth › Stealth Lockdown**' in part and 'Privacy and focus' not in part
    assert HONEST in prose(part) and HONEST in prose(stealth), 'Stealth Lockdown lost its honest line'
    for name, body in (('the guide', part), ('the README', stealth)):
        lowered = prose(body).lower()
        for phrase in FOCUS:
            assert phrase not in lowered, (name, 'promises focus:', phrase)
    return {'tab': 'Privacy & Stealth', 'checked': ['README', 'USER_GUIDE']}


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
        readme, guide, app = read('README.md'), read('docs/USER_GUIDE.md'), read('src/App.vue')
        self.check('the_guard_steps_pebble_out_of_unannounced_captures', lambda: check_guard(shell))
        self.check('nothing_brings_pebble_back_while_the_guard_holds', lambda: check_held(shell))
        self.check('a_stealth_change_from_anywhere_reaches_the_shell_once', lambda: check_follow(shell))
        self.check('banners_wait_while_stealth_lockdown_is_on', lambda: check_banners(shell))
        entry = changelog_entry(RELEASE, read('CHANGELOG.md'))
        self.check('release_version_is_2_0_1_with_its_notes', lambda: check_release(entry, app, readme))
        self.check('the_stealth_text_names_its_tab_and_keeps_its_line', lambda: check_text(shell, readme, guide, app))


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase49-', dir='/private/tmp')).resolve()
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
