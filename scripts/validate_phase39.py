#!/usr/bin/env python3
"""Phase 39 service validator: 1.5.2, the companion over full-screen apps and Stealth Lockdown's windows, as measured.

1.5.1 did not keep the companion over full-screen apps on the person's Mac (D78), and the person reported three Stealth
Lockdown problems. Each fix below was found and then confirmed with native probes and the shell's own diagnostic on a
real Mac (macOS 27, `.runtime/phase39-fsprobe`, `.runtime/phase39-devcheck/sess1-8`); the behaviour is native, so these
checks hold the shell's source to what the measurements showed, the same in source and frozen modes:
  - the companion's Space behaviour is re-applied after it becomes the panel and after every activation-policy change
    (D78), and a companion a Space change left out is ordered back in, in companion mode only (D84);
  - Stealth Lockdown waits for macOS to end the full-screen exit before Pebble leaves the Dock, follows AppKit's own
    end-of-exit notification, keeps the workspace's windowed size through full screen and resizes only an oversized
    frame (D83);
  - in Stealth Lockdown, opening the workspace from another Space goes to the desktop through a 1x1 anchor window that
    lives there (D82); without it, full screen waits until the workspace is on the active Space (D85);
  - ⌃⌥⌘H brings back what it hid; the menu's "Hide Pebble now" only hides;
  - the shell's diagnostic is off unless PEBBLE_SHELL_DIAG names a folder of the person's that others cannot write, and
    it runs only its fixed commands (D81);
  - the release is 1.5.2 and its notes, the README and Pebble's own Settings no longer promise that Pebble never takes
    focus (it does, D86), and the notes describe the fixes.
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

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'the_companion_is_re_applied_after_every_policy_change',
    'a_companion_left_out_of_a_space_is_ordered_back_in',
    'stealth_waits_for_the_full_screen_exit_before_leaving_the_dock',
    'the_workspace_keeps_its_windowed_size_through_full_screen',
    'stealth_opens_the_workspace_on_the_desktop',
    'full_screen_waits_for_the_workspace_space',
    'the_hide_keys_bring_back_what_they_hid',
    'the_diagnostic_is_off_unless_named',
    'release_version_is_1_5_2_and_its_notes_are_honest',
    'backend_inputs_stable_during_campaign',
)
READS = ('src-tauri/src/main.rs', 'src-tauri/tauri.conf.json', 'CHANGELOG.md', 'README.md', 'src/App.vue',
         'src/stealthVeil.ts', 'scripts/validate_phase39.py', 'backend/pebble/__init__.py', 'package.json',
         'package-lock.json', 'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock')
FOCUS_PROMISES = ('never takes focus', 'never takes keyboard focus', "won't take focus", 'never makes Pebble the active app')


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def flat(text):
    return re.sub(r'\s+', ' ', text)


def changelog_entry(version):
    text = read('CHANGELOG.md')
    match = re.search(r'^## \[' + re.escape(version) + r'\][^\n]*\n(.*?)(?=^## \[|\Z)', text, re.M | re.S)
    assert match, 'CHANGELOG.md has no entry for ' + version
    return flat(match.group(1))


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


def function(shell, name):
    start = shell.index('fn ' + name + '(')
    return shell[start:shell.index('\n}\n', start) + 2]


def module(shell, name):
    start = shell.index('mod ' + name + ' {')
    return shell[start:shell.index('\n}\n', start) + 2]


def after(text, first, then):
    """`then` appears after `first` in `text` (both present)."""
    assert first in text, first
    assert then in text[text.index(first) + len(first):], (then, 'not after', first)


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

        def re_applied():
            shell = read('src-tauri/src/main.rs')
            companion = function(shell, 'companion_over_full_screen')
            after(companion, 'stealth_mac::set_panel(ns, true)', 'setCollectionBehavior: 0usize')
            after(companion, 'setCollectionBehavior: 0usize', 'setCollectionBehavior: behaviour')
            after(function(shell, 'configure_pet'), 'set_activation_policy(tauri::ActivationPolicy::Accessory)',
                  'companion_over_full_screen(app)')
            after(function(shell, 'pet_now'), 'set_activation_policy(tauri::ActivationPolicy::Accessory)',
                  'companion_over_full_screen(app)')
            reveal = function(shell, 'reveal')
            after(reveal, 'set_activation_policy(tauri::ActivationPolicy::Regular)', 'if !stealth_on() { companion_over_full_screen(app); }')
            after(function(shell, 'apply_stealth'), 'set_activation_policy(', 'companion_over_full_screen(app)')
            return {'re_applied_in': ['configure_pet', 'pet_now', 'reveal (when it changes the policy)', 'apply_stealth',
                                      'the Stealth windowing (after the deferred policy change)']}
        self.check('the_companion_is_re_applied_after_every_policy_change', re_applied)

        def rejoin():
            shell = read('src-tauri/src/main.rs')
            follows = function(shell, 'companion_follows_spaces')
            assert 'addObserverForName: &*NSString::from_str("NSWorkspaceActiveSpaceDidChangeNotification")' in follows
            assert 'companion_rejoin(&handle' in follows
            rejoining = function(shell, 'companion_rejoin')
            assert 'if policy != 1 { return; }' in rejoining, 'companion mode (an accessory app) only'
            after(rejoining, 'isOnActiveSpace', 'orderFrontRegardless')
            after(rejoining, 'orderFrontRegardless', 'companion_over_full_screen(app)')
            assert 'makeKeyAndOrderFront' not in rejoining and 'activate' not in rejoining.replace('activationPolicy', ''), \
                'brought back without being made key or activating Pebble'
            windowed = function(shell, 'windowed_after_full_screen')
            assert 'companion_rejoin(&main_thread, "after_windowing")' in windowed and 'from_millis(400)' in windowed
            return {'on': 'NSWorkspaceActiveSpaceDidChangeNotification, and 400 ms after the Stealth windowing'}
        self.check('a_companion_left_out_of_a_space_is_ordered_back_in', rejoin)

        def deferred():
            shell = read('src-tauri/src/main.rs')
            apply = function(shell, 'apply_stealth')
            assert 'deferred |= leaving;' in apply
            assert re.search(r'if !deferred \{\s*let _=app\.set_activation_policy\(', apply), 'the Accessory switch waits'
            windowed = function(shell, 'windowed_after_full_screen')
            assert 'exit_observer::watch(app, ended_tx);' in windowed
            assert 'ended_rx.recv_timeout(Duration::from_millis(1500))' in windowed
            after(windowed, 'exit_observer::stop();', 'set_activation_policy(tauri::ActivationPolicy::Accessory)')
            observer = module(shell, 'exit_observer')
            assert '"NSWindowDidExitFullScreenNotification"' in observer and 'object: Some(window)' in observer
            assert 'removeObserver:' in observer
            return {'policy_switch': 'after the end of the exit', 'end_of_exit': 'NSWindowDidExitFullScreenNotification'}
        self.check('stealth_waits_for_the_full_screen_exit_before_leaving_the_dock', deferred)

        def windowed_size():
            shell = read('src-tauri/src/main.rs')
            frame = function(shell, 'windowed_frame')
            assert 'width.min(screen.width*0.8)' in frame and 'height.min(screen.height*0.8)' in frame
            assert 'inner_size()' in frame and '.abs()<2.0' in frame, 'set only when the inner size differs'
            opened = function(shell, 'open_workspace')
            after(opened, 'windowed_frame(&main)', 'main.set_fullscreen(true)')
            windowed = function(shell, 'windowed_after_full_screen')
            assert 'oversized=now.width>width+2.0 || now.height>height+2.0;' in windowed
            assert re.search(r'if oversized \{\s*let _=main\.set_size\(', windowed), 'only an oversized frame is resized'
            return {'before_full_screen': 'windowed size within 80 % of the screen', 'after': 'resized only if oversized'}
        self.check('the_workspace_keeps_its_windowed_size_through_full_screen', windowed_size)

        def desktop():
            shell = read('src-tauri/src/main.rs')
            anchor = module(shell, 'space_anchor')
            for needle in ('width: 1.0, height: 1.0', 'setIgnoresMouseEvents: Bool::YES', 'setSharingType: 0usize',
                           'setBackgroundColor: clear', '(1usize << 2) | (1usize << 6)', 'orderFrontRegardless',
                           'activateIgnoringOtherApps: Bool::YES', 'from_millis(1000)', 'isOnActiveSpace'):
                assert needle in anchor, needle
            opened = function(shell, 'open_workspace')
            assert re.search(r'if stealth_on\(\) && space_anchor::elsewhere\(\) && !space_anchor::GOING\.swap\(true', opened)
            after(opened, 'space_anchor::go_then(', 'return Ok(());')
            setup = shell[shell.index('configure_pet(app.handle())?;'):]
            # D101 (Phase 42): launch places the anchor (made at once unless another app's full-screen Space is active,
            # then on the first desktop Space, measured 2026-10-25); every other assertion is kept.
            assert re.search(r'\n\s*space_anchor::place\(app\.handle\(\), "launch"\);', setup[:1500]), \
                'the anchor is placed at launch (an uncommented call)'
            assert 'space_anchor::place(&handle, "space_changed");' in function(shell, 'companion_follows_spaces')
            assert 'if full != Some(true) { create(); }' in anchor and 'menuBarOwningApplication' in anchor
            return {'anchor': '1x1, transparent, ignores the mouse, hidden from capture, managed, on the desktop Space'}
        self.check('stealth_opens_the_workspace_on_the_desktop', desktop)

        def space_wait():
            shell = read('src-tauri/src/main.rs')
            opened = function(shell, 'open_workspace')
            after(opened, 'let from_elsewhere=space_anchor::elsewhere();', 'reveal(&app);')
            assert 'if !from_elsewhere || on_active_space(&main) { enter(&main); }' in opened
            assert 'began.elapsed()<Duration::from_millis(1000)' in opened
            # D146 (Phase 45, the person): full screen only when the person left the workspace full screen
            assert 'if full && !stealth_on() && !main.is_fullscreen().unwrap_or(false) { let _=main.set_fullscreen(true); }' in opened
            assert 'isOnActiveSpace' in function(shell, 'on_active_space')
            return {'waits': 'until the workspace is on the active Space, at most a second, only when opened from elsewhere'}
        self.check('full_screen_waits_for_the_workspace_space', space_wait)

        def hide_keys():
            shell = read('src-tauri/src/main.rs')
            panic = function(shell, 'stealth_panic')
            assert 'if visible_windows(app).is_empty() {' in panic
            after(panic, 'if visible_windows(app).is_empty() {', 'std::mem::take(&mut *PANIC_HIDDEN.lock().unwrap())')
            assert 'if hidden.contains(&"main") { reveal(app); }' in panic
            assert '*PANIC_HIDDEN.lock().unwrap()=visible_windows(app);' in panic
            assert 'for label in ["pet", "main"] { if let Some(w)=app.get_webview_window(label) { let _=w.hide(); } }' in panic
            hide_now = function(shell, 'hide_now')
            assert 'reveal(' not in hide_now and '.show()' not in hide_now, 'the menu item only hides'
            assert '"hide"=>hide_now(app)' in shell and 'shortcut.matches(stealth_keys, Code::KeyH) { stealth_panic(app); }' in shell
            return {'keys': 'toggle', 'menu': 'hide only'}
        self.check('the_hide_keys_bring_back_what_they_hid', hide_keys)

        def diagnostic():
            shell = read('src-tauri/src/main.rs')
            diag = module(shell, 'diag')
            assert 'std::env::var_os("PEBBLE_SHELL_DIAG")?' in diag, 'off without the variable'
            assert 'meta.uid() != unsafe { libc_getuid() }' in diag and 'meta.mode() & 0o022 != 0' in diag
            assert 'file.write_all(line.as_bytes())' in diag, 'one write per line'
            run = function(shell, 'diag_run')
            commands = set(re.findall(r'\(Some\("([a-z_]+)"\)', run))
            # D100 (Phase 42, E-N2): `ax_probe` joins: one backend call that reads TextEdit only (the backend spawns its
            # AX worker as production would); the shell still executes nothing. Every other assertion is kept.
            assert commands == {'stealth', 'open_workspace', 'to_pet', 'panic', 'state', 'latency', 'ax_probe'}, commands
            assert 'backend.call("diag_ax_probe", json!({"bundle_id":"com.apple.TextEdit","title_contains":fragment})' in run
            assert '_ => diag::log("command_unknown"' in run
            assert 'Command::new' not in run and 'std::process' not in diag, 'nothing is executed'
            return {'switch': 'PEBBLE_SHELL_DIAG, an owned folder not writable by others', 'commands': sorted(commands)}
        self.check('the_diagnostic_is_off_unless_named', diagnostic)

        def version():
            current = checkout_version()
            # D88 (after Phase 39): 1.5.2's changes are installed as 1.5.3 (the person's Mac had a 1.5.2 test build and
            # the installer takes only a higher version); the 1.5.2 entry is read by name and every assertion is kept.
            assert tuple(int(x) for x in current.split('.')) >= (1, 5, 2), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            entry = changelog_entry('1.5.2')
            for needle in ('full-screen apps', 'waits for macOS to finish leaving full screen', 'red, yellow and green buttons',
                           'takes you to the desktop', '⌃⌥⌘H', 'Known issue', 'does not keep keyboard focus off Pebble',
                           'PEBBLE_SHELL_DIAG', 'measured on a real Mac'):
                assert needle in entry, ('the 1.5.2 notes lack', needle)
            for promise in FOCUS_PROMISES:
                assert promise not in entry, ('the 1.5.2 notes promise', promise)
            readme = flat(read('README.md'))
            # Phase 45 (F151): measured since 1.9.6 (native/s3-1.9.6.jsonl), said for the companion only
            assert 'Clicking or typing in the companion leaves the app you were in active' in readme
            assert 'Hidden from screen capture and screen sharing, not from a camera' in readme, 'the honest line'
            app_vue, veil = read('src/App.vue'), read('src/stealthVeil.ts')
            for text, where in ((readme, 'README.md'), (app_vue, 'src/App.vue'), (veil, 'src/stealthVeil.ts')):
                for promise in FOCUS_PROMISES:
                    assert promise not in text, (where, 'still promises', promise)
            assert "Clicking or typing in the companion leaves the app you were in active" in app_vue
            # D96 (Phase 40): What's new mirrors the newest Highlights (merge24c), 1.6.0's from then on; the 1.5.2 line
            # is checked where it is kept, in the 1.5.2 entry, and Settings' Lockdown sentence above stays in App.vue.
            assert 'In Stealth Lockdown the full workspace opens on the desktop' in (
                app_vue if tuple(int(x) for x in current.split('.')) < (1, 6, 0) else entry)
            assert 'undetectable' not in (entry + readme).lower() and 'Context Warping' not in entry, 'wording rules'
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_1_5_2_and_its_notes_are_honest', version)


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
    before = digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase39-', dir='/private/tmp')).resolve()
    started = time.monotonic()
    campaign = Campaign(scratch, backend)
    fatal = None
    try:
        campaign.run()
    except Exception as exc:
        fatal = bounded(exc, 1500)
    after_digests = digests()
    stable = before == after_digests and (not backend or sha(backend.read_bytes()) == backend_sha)
    campaign.results.append({'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                             'evidence': {'files_compared': len(before), 'executable_compared': bool(backend)}})
    recorded = {r['name'] for r in campaign.results}
    failed = [r['name'] for r in campaign.results if not r['passed']] + [n for n in PLANNED if n not in recorded]
    passed = fatal is None and not failed and recorded == set(PLANNED)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source', 'passed': passed,
        'checks': [r['name'] for r in campaign.results if r['passed']], 'failed': failed, 'planned_checks': list(PLANNED),
        'check_results': campaign.results, 'fatal_error': fatal,
        'duration_seconds': round(time.monotonic() - started, 1),
        'backend_sha256': backend_sha, 'source_sha256': before,
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
