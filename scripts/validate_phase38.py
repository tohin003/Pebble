#!/usr/bin/env python3
"""Phase 38 service validator: 1.5.1, the floating companion over full-screen apps.

The person asked for the companion to stay on screen with them over every app, full-screen ones included, like a
genie, while the full workspace still opens like any other app, in its own Space and never over another app's full
screen. Joining every Space is not enough for another app's full-screen Space: macOS shows only a non-activating panel
there, so the companion is always one (the panel Stealth Lockdown makes). The behaviour is native and needs a person
at a real Mac; these checks hold the shell's source to it, the same in source and frozen modes:
  - the companion is a non-activating panel from launch, joins every Space, full-screen ones included, and never hides
    when Pebble is not the active app;
  - turning Stealth Lockdown off keeps the companion a panel (1.5.0's switch for every window is unchanged);
  - the companion never activates Pebble, so the full-screen app stays in front;
  - the full workspace opens like any other app: no all-Spaces or full-screen-auxiliary behaviour outside the
    companion's setup, an ordinary app with a Dock icon when stealth is off, its own full-screen Space;
  - the release version is 1.5.1 and its notes describe this.
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
    'the_companion_is_always_a_nonactivating_panel_on_every_space',
    'turning_stealth_off_keeps_the_companion_a_panel',
    'the_companion_never_activates_pebble',
    'the_workspace_opens_in_its_own_space_never_over_full_screen',
    'release_version_is_1_5_1_and_its_notes_describe_it',
    'backend_inputs_stable_during_campaign',
)
READS = ('src-tauri/src/main.rs', 'src-tauri/tauri.conf.json', 'CHANGELOG.md', 'scripts/validate_phase38.py',
         'backend/pebble/__init__.py', 'package.json', 'package-lock.json', 'src-tauri/Cargo.toml',
         'src-tauri/Cargo.lock', 'src/App.vue')
ALL_SPACES = ('CanJoinAllSpaces', 'FullScreenAuxiliary', 'CanJoinAllApplications', 'set_visible_on_all_workspaces',
              'MoveToActiveSpace')


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


def span(shell, name):
    start = shell.index('fn ' + name + '(')
    return start, start + len(function(shell, name))


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

        def panel():
            shell = read('src-tauri/src/main.rs')
            companion = function(shell, 'companion_over_full_screen')
            assert 'get_webview_window("pet")' in companion, 'the companion window'
            assert '"main"' not in companion, 'only the companion becomes a panel for good'
            assert 'stealth_mac::set_panel(ns, true)' in companion, 'always a non-activating panel'
            assert 'setHidesOnDeactivate: objc2::runtime::Bool::NO' in companion, 'a panel would hide when inactive'
            configure = function(shell, 'configure_pet')
            behaviour = re.search(r'let mut behavior=([^;]+);', configure)
            assert behaviour and 'B::CanJoinAllSpaces' in behaviour.group(1) \
                and 'B::FullScreenAuxiliary' in behaviour.group(1), 'every Space, full-screen ones included'
            assert configure.index('setCollectionBehavior(behavior)') < configure.index('companion_over_full_screen(app)')
            assert 'native.setLevel(3);' in configure, 'the floating level, never a focus-stealing system level'
            panel_fn = function(shell, 'set_panel')
            assert 'setStyleMask: mask | NONACTIVATING_PANEL' in panel_fn
            conf = json.loads(read('src-tauri/tauri.conf.json'))
            pet = next(w for w in conf['app']['windows'] if w['label'] == 'pet')
            assert pet.get('visibleOnAllWorkspaces') is True and pet.get('alwaysOnTop') is True, pet
            return {'companion': 'non-activating panel from launch', 'spaces': behaviour.group(1).strip(),
                    'level': 'NSFloatingWindowLevel'}
        self.check('the_companion_is_always_a_nonactivating_panel_on_every_space', panel)

        def stealth_off():
            shell = read('src-tauri/src/main.rs')
            apply = function(shell, 'apply_stealth')
            assert 'else { let _=stealth_mac::set_panel(ns, on); }' in apply, "1.5.0's switch, unchanged"
            assert 'companion_over_full_screen(app)' in apply, 'the companion is made a panel again'
            assert apply.index('stealth_mac::set_sharing_all(on)') < apply.index('companion_over_full_screen(app)') \
                < apply.index('tray_state(app, on)'), 'after every window has switched'
            return {'after_stealth_off': 'the companion is a panel again; the workspace is an ordinary window'}
        self.check('turning_stealth_off_keeps_the_companion_a_panel', stealth_off)

        def never_activates():
            shell = read('src-tauri/src/main.rs')
            focus = function(shell, 'focus')
            assert 'if stealth_on() || window.label()=="pet" {' in focus, 'the companion is brought forward only'
            assert focus.index('front_without_activating') < focus.index('set_focus()')
            assert shell.count('set_focus()') == 1, 'set_focus outside focus()'
            assert 'if expanded { focus(&window); }' in function(shell, 'pet_expand')
            assert 'makeKeyAndOrderFront:' in function(shell, 'front_without_activating')
            return {'companion': 'made key without activating Pebble', 'workspace': 'set_focus when stealth is off'}
        self.check('the_companion_never_activates_pebble', never_activates)

        def own_space():
            shell = read('src-tauri/src/main.rs')
            start, end = span(shell, 'configure_pet')
            for needle in ALL_SPACES:
                for match in re.finditer(needle, shell):
                    assert start < match.start() < end, (needle, 'outside the companion setup')
            conf = json.loads(read('src-tauri/tauri.conf.json'))
            main = next(w for w in conf['app']['windows'] if w['label'] == 'main')
            assert 'visibleOnAllWorkspaces' not in main and 'alwaysOnTop' not in main, main
            reveal = function(shell, 'reveal')
            assert 'if !stealth_on() { let _=app.set_activation_policy(tauri::ActivationPolicy::Regular); }' in reveal
            opened = function(shell, 'open_workspace')
            # D146 (Phase 45, the person): its own full-screen Space when the person left it full screen; the first open
            # is full screen, and leaving it windowed is remembered at the pebble (never under Stealth Lockdown)
            assert 'if full && !stealth_on() && !main.is_fullscreen().unwrap_or(false) { let _=main.set_fullscreen(true); }' \
                in opened
            assert 'static WORKSPACE_FULL: AtomicBool = AtomicBool::new(true);' in shell
            assert 'WORKSPACE_FULL.store(main_fullscreen(app), Ordering::SeqCst)' in function(shell, 'to_pet')
            assert function(shell, 'to_pet').index('if !stealth_on()') < function(shell, 'to_pet').index('WORKSPACE_FULL.store')
            # D79 (Phase 39): this asserted the companion helper stays out of reveal and open_workspace. D78 measured
            # that the companion's Space behaviour must be re-applied after every activation-policy change, and reveal
            # changes it, so reveal now calls the helper after its policy line. The helper touches only the companion
            # (the panel check above), so the workspace still gets no all-Spaces or panel behaviour.
            assert 'companion_over_full_screen' not in opened
            if 'companion_over_full_screen' in reveal:
                assert reveal.index('set_activation_policy(') < reveal.index('companion_over_full_screen(app)')
            return {'workspace': 'an ordinary app in its own full-screen Space', 'all_spaces_only_in': 'configure_pet'}
        self.check('the_workspace_opens_in_its_own_space_never_over_full_screen', own_space)

        def version():
            current = checkout_version()
            # D80 (Phase 39): the 1.5.1 entry is read by name and every assertion on it is kept; the checkout may be
            # later (1.5.1 was not released, D78; 1.5.2 ships its fix).
            assert tuple(int(x) for x in current.split('.')) >= (1, 5, 1), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            entry = changelog_entry('1.5.1')
            for needle in ('floating companion', 'full-screen apps', 'non-activating panel', 'its own full-screen Space',
                           'never over another app', 'D75'):
                assert needle in entry, ('the 1.5.1 notes lack', needle)
            assert 'undetectable' not in entry.lower() and 'Context Warping' not in entry, 'wording rules'
            app_vue = read('src/App.vue')
            # D96 (Phase 40): What's new mirrors the newest Highlights (merge24c); from 1.6.0 this 1.5.2 line is read in
            # the 1.5.2 entry, where it is kept.
            assert 'The floating companion now stays on screen over full-screen apps, on every Space.' in (
                app_vue if tuple(int(x) for x in current.split('.')) < (1, 6, 0) else changelog_entry('1.5.2'))
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_1_5_1_and_its_notes_describe_it', version)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase38-', dir='/private/tmp')).resolve()
    started = time.monotonic()
    campaign = Campaign(scratch, backend)
    fatal = None
    try:
        campaign.run()
    except Exception as exc:
        fatal = bounded(exc, 1500)
    after = digests()
    stable = before == after and (not backend or sha(backend.read_bytes()) == backend_sha)
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
