#!/usr/bin/env python3
"""Phase 33 service validator: 1.1.0's Stealth Lockdown, one switch for privacy and no interruptions.

The person asked for Genie's stealth and lockdown as one Pebble switch (D45-D47): a glowing toggle, an activation
animation on the pebble and a state that stays visible while on, presented for privacy and without "undetectable"
claims. They chose look A, the Veil, from the design preview (D57). These checks read the shell, the frontend and the
docs, and drive the backend's setting in process, the same in source and frozen modes:
  - stealth hides every window from capture and lifts it again: content protection on both windows and every window
    the app owns, no workspace shadow, out of the Dock and ⌘Tab (Accessory) and Mission Control (transient), and
    each value restored when off;
  - lockdown never activates Pebble: non-activating panels carrying tao's own methods, swapped only when the runtime
    layout allows; no set_focus while on; the workspace opens windowed;
  - the keys and the menu-bar icon: ⌃⌥⌘S and ⌃⌥⌘H registered beside ⌘⇧Space, template glyphs that change shape, the
    menu's switch and Hide Pebble now;
  - the setting is kept, reaches every window and never asks for confirmation (it gives Pebble less, not more);
  - the Veil is the chosen design's timings and rules, on every avatar;
  - the honest line is in the app, the README, the User Guide and the CHANGELOG, and no "undetectable" wording is;
  - the release version is 1.1.0 and its notes carry Stealth Lockdown and the sealed held-out benchmark set.
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

for _path in (ROOT / 'backend', ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'stealth_hides_every_window_and_lifts_it_again',
    'lockdown_never_activates_pebble',
    'the_keys_and_the_menu_bar_icon',
    'the_setting_is_kept_and_reaches_every_window',
    'the_veil_is_the_chosen_design',
    'the_honest_line_travels_with_the_feature',
    'release_version_is_1_1_0_and_its_notes_carry_stealth_lockdown',
    'backend_inputs_stable_during_campaign',
)
READS = ('src-tauri/src/main.rs', 'src-tauri/build.rs', 'src-tauri/capabilities/main.json', 'src-tauri/icons/tray-off.png',
         'src-tauri/icons/tray-on.png', 'src-tauri/Cargo.lock', 'src/App.vue', 'src/Pet.vue', 'src/components/Avatar.vue',
         'src/stealthVeil.ts', 'src/tokens.css', 'src/style.css', 'src/shortcuts.ts', 'backend/pebble/app.py',
         'backend/pebble/settings_file.py', 'README.md', 'docs/USER_GUIDE.md', 'CHANGELOG.md', 'scripts/validate_phase33.py',
         'backend/pebble/__init__.py', 'package.json', 'package-lock.json', 'src-tauri/Cargo.toml', 'src-tauri/tauri.conf.json')
HONEST = ('Hidden from screen capture and screen sharing, not from a camera, a capture card or a process list. '
          'Exams and interviews that forbid assistants still forbid it.')
RETRACTED = ('undetectable', 'invisible to', 'proctor', 'bypass', 'cheat', 'evade')
CORPUS_25 = '27889ccdb1fa8d26c047ec3d127f10d2dadd9cf8cb550d74f0d565d70054d051'


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def flat(text):
    return re.sub(r'\s+', ' ', re.sub(r'^> ?', '', text, flags=re.M))


def function(source, name):
    """One Rust function's text, from `fn name(` to the closing brace at its own indentation (module functions too)."""
    start = source.find('fn ' + name + '(')
    assert start >= 0, 'the shell has no fn ' + name
    line = source[source.rfind('\n', 0, start) + 1:start]
    close = '\n' + ' ' * (len(line) - len(line.lstrip())) + '}\n'
    end = source.find(close, start)
    assert end > start, 'fn ' + name + ' does not end'
    return source[start:end + len(close)]


def changelog_entry(version):
    text = read('CHANGELOG.md')
    match = re.search(r'^## \[' + re.escape(version) + r'\][^\n]*\n(.*?)(?=^## \[|\Z)', text, re.M | re.S)
    assert match, 'CHANGELOG.md has no entry for ' + version
    return flat(match.group(1))


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

        def hides():
            shell = read('src-tauri/src/main.rs')
            apply = function(shell, 'apply_stealth')
            for needle in ('for label in ["pet", "main"]', 'set_content_protected(on)', 'set_shadow(!on)',
                           'stealth_mac::set_sharing_all(on)', 'stealth_mac::set_mission_control(label, ns, on)',
                           'tray_state(app, on)'):
                assert needle in apply, ('apply_stealth lacks', needle)
            assert 'if on || !workspace_open {tauri::ActivationPolicy::Accessory}' in apply, 'not out of the Dock and Cmd-Tab'
            assert 'if on && window.is_fullscreen().unwrap_or(false) { let _=window.set_fullscreen(false); }' in apply
            every = function(shell, 'set_sharing_all')
            assert 'msg_send![app, windows]' in every and 'setSharingType: if on { 0usize } else { 1usize }' in every
            mission = function(shell, 'set_mission_control')
            assert 'setCollectionBehavior: (current & !MANAGED) | TRANSIENT' in mission and 'saved.remove(label)' in mission
            assert 'const TRANSIENT: usize = 1 << 3;' in shell and 'const MANAGED: usize = 1 << 2;' in shell
            return {'windows': ['pet', 'main', 'every app window'], 'restored': ['sharing', 'shadow', 'behaviour', 'policy']}
        self.check('stealth_hides_every_window_and_lifts_it_again', hides)

        def lockdown():
            shell = read('src-tauri/src/main.rs')
            made = function(shell, 'panel_class')
            for needle in ('AnyClass::get(c"NSPanel")', 'AnyClass::get(c"TaoWindow")',
                           'class_getInstanceSize(panel) != ffi::class_getInstanceSize(window)', 'offset(cls) != offset(tao)',
                           'sel!(canBecomeMainWindow)', 'sel!(canBecomeKeyWindow)', 'sel!(sendEvent:)'):
                assert needle in made, ('panel_class lacks', needle)
            panel = function(shell, 'set_panel')
            assert panel.index('object_setClass(ns, panel)') < panel.index('mask | NONACTIVATING_PANEL'), 'mask before class'
            assert panel.index('mask & !NONACTIVATING_PANEL') < panel.index('object_setClass(ns, tao)'), 'class before mask'
            assert 'const NONACTIVATING_PANEL: usize = 1 << 7;' in shell
            reveal = function(shell, 'reveal')
            assert 'if !stealth_on() { let _=app.set_activation_policy(tauri::ActivationPolicy::Regular); }' in reveal
            assert 'set_focus' not in reveal and 'focus(&window);' in reveal
            focus = function(shell, 'focus')
            assert focus.index('if stealth_on()') < focus.index('set_focus()') and 'front_without_activating' in focus
            assert shell.count('set_focus()') == 1, 'set_focus outside focus()'
            assert 'if expanded { focus(&window); }' in function(shell, 'pet_expand')
            # D146 (Phase 45, the person): full screen also only when the person left the workspace full screen
            assert 'if full && !stealth_on() && !main.is_fullscreen()' in function(shell, 'open_workspace'), \
                'full screen under stealth'
            assert 'makeKeyAndOrderFront:' in function(shell, 'front_without_activating')
            return {'panel': 'NSPanel with TaoWindow methods, layout-checked', 'set_focus_sites': 1}
        self.check('lockdown_never_activates_pebble', lockdown)

        def keys():
            shell = read('src-tauri/src/main.rs')
            registered = re.findall(r'global_shortcut\(\)\.register\("([^"]+)"\)', shell)
            assert registered == ['CommandOrControl+Shift+Space', 'Control+Alt+Command+S', 'Control+Alt+Command+H'], registered
            assert 'shortcut.matches(stealth_keys, Code::KeyS) { stealth_toggle(app); }' in shell
            assert 'shortcut.matches(stealth_keys, Code::KeyH) { stealth_panic(app); }' in shell
            assert 'TrayIconBuilder::with_id("pebble").icon(glyph).icon_as_template(true)' in shell
            assert 'CheckMenuItem::with_id(app,"stealth","Stealth Lockdown",true,false,Some("Control+Alt+Command+S"))' in shell
            assert 'MenuItem::with_id(app,"hide","Hide Pebble now",true,Some("Control+Alt+Command+H"))' in shell
            toggle = function(shell, 'stealth_toggle')
            assert toggle.index('backend.call("setting"') < toggle.index('apply_stealth(&shown, next)'), 'the setting first'
            assert 'pointer("/settings/stealth")' in shell, 'not restored at launch'
            from PIL import Image
            shapes = {}
            for name in ('tray-off.png', 'tray-on.png'):
                image = Image.open(ROOT / 'src-tauri/icons' / name)
                assert image.mode == 'RGBA' and image.size == (36, 36), (name, image.mode, image.size)
                raw = image.tobytes()
                pixels = [tuple(raw[i:i + 4]) for i in range(0, len(raw), 4)]
                assert {p[:3] for p in pixels if p[3]} <= {(0, 0, 0)}, name + ' is not a black template'
                shapes[name] = sum(1 for p in pixels if p[3] > 128)
            assert shapes['tray-on.png'] != shapes['tray-off.png'], 'the glyphs do not change shape'
            assert 'stealth_set' in read('src-tauri/build.rs')
            assert 'allow-stealth-set' in json.loads(read('src-tauri/capabilities/main.json'))['permissions']
            assert 'main_only(&window)?;' in function(shell, 'stealth_set')
            return {'shortcuts': registered, 'glyph_pixels': shapes}
        self.check('the_keys_and_the_menu_bar_icon', keys)

        def setting():
            from pebble import settings_file
            from pebble.app import App
            assert 'stealth' in settings_file.KEYS and settings_file.default('stealth') is False
            assert settings_file.widening('stealth', False, True) == [], 'turning it on waits for a confirmation'
            for bad in ('yes', 1, None):
                try:
                    settings_file.validate('stealth', bad)
                    raise AssertionError('accepted ' + repr(bad))
                except ValueError:
                    pass
            app = App(scratch / 'data', scratch / 'resources', lambda *a: None)
            events = []
            app.emit = lambda kind, payload=None: events.append(kind)
            try:
                assert app.state()['settings']['stealth'] is False
                assert app.setting('stealth', True) is True and app.state()['settings']['stealth'] is True
                assert 'settings' in events, 'the windows are not told'
            finally:
                app.close()
            again = App(scratch / 'data', scratch / 'resources', lambda *a: None)
            try:
                assert again.state()['settings']['stealth'] is True, 'not kept across a restart'
            finally:
                again.close()
            vue = read('src/App.vue')
            assert 'aria-describedby="stealth-desc stealth-honest"' in vue and 'class="toggle stealth"' in vue
            assert 'await rpc("setting", { key: "stealth", value: enabled });' in vue
            assert 'if (native) await invoke("stealth_set", { enabled });' in vue
            avatars = re.findall(r'<Avatar\b[^>]*?>', vue, re.S)
            assert avatars and all(':stealth="!!state.settings.stealth"' in a for a in avatars), 'an avatar without the Veil'
            assert ':stealth="stealthOn"' in read('src/Pet.vue')
            return {'kept_across_restart': True, 'workspace_avatars': len(avatars), 'confirmation': False}
        self.check('the_setting_is_kept_and_reaches_every_window', setting)

        def veil():
            ts = read('src/stealthVeil.ts')
            for name, value in (('ACTIVATE', '1.25'), ('RELEASE', '0.65'), ('FADE', '0.3'), ('SHIMMER_EVERY', '6'),
                                ('SHIMMER', '1.1'), ('GLASS_OPACITY', '0.7')):
                assert re.search(r'export const ' + name + r' = ' + re.escape(value) + ';', ts), name
            avatar = read('src/components/Avatar.vue')
            for needle in ('stealth?: boolean;', ':data-stealth="stealth ? \'on\' : \'off\'"', 'props.size >= 64',
                           '(2 * 1.3 * 220) / props.size', 'var(--avatar-stealth-rim)', 'transition: opacity 0.3s'):
                assert needle in avatar, ('Avatar lacks', needle)
            tokens = read('src/tokens.css')
            for token in ('--avatar-stealth:', '--avatar-stealth-hi:', '--avatar-stealth-eye:', '--avatar-stealth-rim:',
                          '--avatar-stealth-glow:', '--stealth-glow-soft:'):
                assert tokens.count(token) == 2, ('both themes need', token)
            style = read('src/style.css')
            assert '.toggle.stealth::after' in style and '.reduced-motion .toggle.stealth' in style, 'the glowing switch'
            return {'look': 'A, Veil', 'activate_s': 1.25, 'release_s': 0.65, 'reduced_fade_s': 0.3}
        self.check('the_veil_is_the_chosen_design', veil)

        def honest():
            places = {'README.md': flat(read('README.md')), 'docs/USER_GUIDE.md': flat(read('docs/USER_GUIDE.md')),
                      'CHANGELOG 1.1.0': changelog_entry('1.1.0'), 'src/stealthVeil.ts': read('src/stealthVeil.ts'),
                      'src-tauri/src/main.rs': flat(read('src-tauri/src/main.rs').replace('//', ' '))}
            missing = [where for where, text in places.items() if HONEST not in text]
            assert not missing, ('the honest line is missing from', missing)
            found = [(where, word) for where, text in places.items() for word in RETRACTED
                     if word in text.lower() and where != 'README.md']
            readme = places['README.md'].lower()
            start = readme.find('### stealth lockdown')
            section = readme[start:readme.find('---', start)]
            found += [('README.md Stealth Lockdown', word) for word in RETRACTED if word in section]
            assert not found, found
            return {'places': sorted(places), 'retracted_words': len(RETRACTED)}
        self.check('the_honest_line_travels_with_the_feature', honest)

        def version():
            current = checkout_version()
            # D62 (Phase 34): the release that ships Stealth Lockdown is 1.1.0 and this check reads its entry; a later
            # checkout (1.2.0) keeps that entry and every assertion on it, as D60 did for validate_phase30 and 31.
            assert tuple(int(x) for x in current.split('.')) >= (1, 1, 0), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            entry = changelog_entry('1.1.0')
            for needle in ('Stealth Lockdown', '⌃⌥⌘S', '⌃⌥⌘H', 'The Veil', CORPUS_25):
                assert needle in entry, ('the 1.1.0 notes lack', needle)
            return {'version': current, 'declarations': len(declared), 'corpus_25_sha256': CORPUS_25}
        self.check('release_version_is_1_1_0_and_its_notes_carry_stealth_lockdown', version)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase33-', dir='/private/tmp')).resolve()
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
