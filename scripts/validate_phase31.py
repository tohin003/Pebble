#!/usr/bin/env python3
"""Phase 31 service validator: 1.0.4's full workspace is a real full screen without the Dock in the way.

Phase 30 proved 1.0.3 (reports/harness-phase30-review.json), and it was installed on the person's Mac. Back from being
away, the person found that in macOS full screen the Dock slid up over the message box and the red, yellow and green
buttons were hidden, and asked for the pebble's "Open full workspace" to open a real full screen with the Dock hidden
and the buttons shown, and for leaving it to go back to the floating pebble (.runtime/decisions-after-phase30.json D43).
They tried a build of the change ("All works"), and chose 1.0.4 for the release (D44). The shell is native code that no
test can drive, so these checks read it, the same in source and frozen modes:
  - the full workspace opens in full screen without the Dock: Open full workspace shows the window, drops always on
    top and enters full screen; the Dock is hidden through tao's full-screen presentation answer (HideDock for
    AutoHideDock, the menu bar left to the person's setting), installed once the windows exist, and Cargo.lock pins
    the tao whose delegate class and selector those are;
  - leaving the full workspace returns to the pebble: the red button, Cmd+W and Floating companion all go through one
    path that leaves full screen first and hides the window only once AppKit's full-screen style bit has cleared;
  - full screen uses the standard title bar, and the overlay bar returns after it;
  - the release version is after 1.0.3, every declaration agrees, and CHANGELOG's entry for it carries the fix and says
    that 1.0.2 and 1.0.3 were not published on their own.
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
    'the_full_workspace_opens_in_full_screen_without_the_dock',
    'leaving_the_full_workspace_returns_to_the_pebble',
    'full_screen_uses_the_standard_title_bar',
    'release_version_is_after_1_0_3_and_its_notes_carry_the_fix',
    'backend_inputs_stable_during_campaign',
)
# The tested sources this validator reads; each is in verify_harness_candidate.sources(), so the campaign binds them.
READS = ('src-tauri/src/main.rs', 'src-tauri/Cargo.lock', 'src-tauri/tauri.conf.json', 'CHANGELOG.md',
         'scripts/validate_phase31.py', 'backend/pebble/__init__.py', 'package.json', 'package-lock.json',
         'src-tauri/Cargo.toml')
TAO = '0.37.1'  # the tao whose window delegate is TaoWindowDelegate with window:willUseFullScreenPresentationOptions:


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def function(source, name):
    """One Rust function's text, from `fn name(` to the closing brace at the start of a line."""
    start = source.find('fn ' + name + '(')
    assert start >= 0, 'the shell has no fn ' + name
    end = source.find('\n}\n', start)
    assert end > start, 'fn ' + name + ' does not end'
    return source[start:end + 2]


def changelog_entry(version):
    text = read('CHANGELOG.md')
    match = re.search(r'^## \[' + re.escape(version) + r'\][^\n]*\n(.*?)(?=^## \[|\Z)', text, re.M | re.S)
    assert match, 'CHANGELOG.md has no entry for ' + version
    return re.sub(r'\s+', ' ', match.group(1))


def notes_version(current, released='1.0.4'):
    """The release whose CHANGELOG entry carries what this validator proves: the checkout's own version up to
    `released`, and `released` once the checkout has moved past it (Phase 33, D60: at 1.1.0 the current entry would
    otherwise have to repeat 1.0.4's notes, which would be false for 1.1.0). Every assertion on the entry is unchanged."""
    parts = lambda v: tuple(int(x) for x in v.split('.'))
    return released if parts(current) > parts(released) else current


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


class Campaign:
    def __init__(self, scratch, backend=None):
        self.scratch, self.backend, self.results = scratch, backend, []

    def check(self, name, fn):
        assert name in PLANNED and name not in {r['name'] for r in self.results}, name
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

        def opens():
            shell = read('src-tauri/src/main.rs')
            open_ = function(shell, 'open_workspace')
            assert 0 <= open_.find('reveal(&app)') < open_.find('set_fullscreen(true)'), 'not shown, then full screen'
            assert 'set_always_on_top(false)' in open_, 'the full workspace could stay always on top'
            assert 'fullscreen' not in function(shell, 'reveal'), 'every reveal would go full screen'
            hide = function(shell, 'hide_dock_in_fullscreen')
            for needle in ('AnyClass::get(c"TaoWindowDelegate")', 'sel!(window:willUseFullScreenPresentationOptions:)',
                           'c"Q@:@Q"', '(proposed & !AUTO_HIDE_DOCK) | HIDE_DOCK', 'const AUTO_HIDE_DOCK: usize = 1 << 0;',
                           'const HIDE_DOCK: usize = 1 << 1;', 'class_replaceMethod('):
                assert needle in hide, ('the Dock is not hidden in full screen', needle)
            assert re.search(r'configure_pet\(app\.handle\(\)\)\?;\n\s*#\[cfg\(target_os="macos"\)\]\n\s*let _ = hide_dock_in_fullscreen\(\);',
                             shell), 'the Dock rule is not installed once the windows exist'
            assert re.search(r'name = "tao"\nversion = "' + re.escape(TAO) + '"', read('src-tauri/Cargo.lock')), \
                'Cargo.lock no longer pins the tao whose delegate this relies on'
            return {'open_workspace': 'reveal, not always on top, full screen', 'dock': 'HideDock for AutoHideDock',
                    'tao': TAO}
        self.check('the_full_workspace_opens_in_full_screen_without_the_dock', opens)

        def leaves():
            shell = read('src-tauri/src/main.rs')
            assert re.search(r'fn show_pet\(app:tauri::AppHandle\) \{ to_pet\(&app\); \}', shell), 'Floating companion skips to_pet'
            assert re.search(r'tauri::WindowEvent::CloseRequested\{api,\.\.\} if window\.label\(\)=="main" => '
                             r'\{ api\.prevent_close\(\); to_pet\(window\.app_handle\(\)\); \}', shell), \
                'the red button and Cmd+W skip to_pet'
            to_pet = function(shell, 'to_pet')
            assert 'if !main_fullscreen(app) { return pet_now(app); }' in to_pet, 'outside full screen it does not go straight back'
            assert 'set_fullscreen(false)' in to_pet, 'it never leaves full screen'
            assert 'main_fullscreen(&probe)' in to_pet and 'run_on_main_thread(move || pet_now(&shown))' in to_pet, \
                'it hides the window without waiting for full screen to end'
            assert 'const FULL_SCREEN: usize = 1 << 14;' in function(shell, 'appkit_fullscreen'), 'not AppKit\'s style bit'
            pet = function(shell, 'pet_now')
            assert 0 <= pet.find('get_webview_window("pet")') < pet.find('w.hide()'), 'the pebble is not shown'
            return {'paths': ['Floating companion', 'the red button', 'Cmd+W'], 'waits_for': 'NSWindowStyleMaskFullScreen'}
        self.check('leaving_the_full_workspace_returns_to_the_pebble', leaves)

        def title_bar():
            shell = read('src-tauri/src/main.rs')
            sync = function(shell, 'sync_title_bar')
            assert 'if full {tauri::TitleBarStyle::Visible} else {tauri::TitleBarStyle::Overlay}' in sync, 'no title bar switch'
            assert re.search(r'tauri::WindowEvent::Resized\(_\) \| tauri::WindowEvent::Moved\(_\) \| tauri::WindowEvent::Focused\(_\) '
                             r'=> sync_title_bar\(window\)', shell), 'the switch is not driven by the window events'
            main = next(w for w in json.loads(read('src-tauri/tauri.conf.json'))['app']['windows'] if w['label'] == 'main')
            assert main.get('titleBarStyle') == 'Overlay', main.get('titleBarStyle')
            return {'full_screen': 'Visible', 'otherwise': 'Overlay'}
        self.check('full_screen_uses_the_standard_title_bar', title_bar)

        def version():
            current = checkout_version()
            assert tuple(int(x) for x in current.split('.')) > (1, 0, 3), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            entry = changelog_entry(notes_version(current))
            assert 'full screen' in entry and 'Dock' in entry and 'Open full workspace' in entry, 'the entry lacks the fix'
            assert '1.0.2 and 1.0.3 were not published on their own' in entry, 'the entry hides the unpublished versions'
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_after_1_0_3_and_its_notes_carry_the_fix', version)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase31-')).resolve()
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
