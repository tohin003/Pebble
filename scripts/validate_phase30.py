#!/usr/bin/env python3
"""Phase 30 service validator: 1.0.3 claims only what the product does, and evaluation never runs the app's identity.

Phase 29 proved the Claude benchmark's pinned CLI (reports/harness-phase29-review.json). The person then asked for every
command, shortcut and feature claim to be checked (.runtime/decisions-after-phase28.json D37). Three read-only audits
found ⌘+ and ⌘− dead, /stop sending /resume, a PDF export that could never succeed, Views controls the pet may not
call, an Esc that left panels open, and public text that promised more than the product does (decisions-after-phase29
.json D42). The benchmark harness had also run Pebble through the release bundle, which owns the person's Keychain item
(D41). The window fixes are proved by the UI suite; this validator proves the rest. Checks, the same in source and
frozen modes:
  - the zoom keys have their permission: every window that turns on Tauri's zoom hotkeys is in a capability that grants
    core:webview:allow-set-webview-zoom (the hotkey polyfill's own requirement), a name the generated schema knows;
  - PDF export is not offered until it exists: an owned service (the frozen executable in frozen mode) lists no pdf
    for /export, refuses /export of a pdf with the way to get one and writes nothing, and the export panel and the slash
    menu offer no PDF;
  - evaluation never runs the installed identity: the benchmark harness refuses a candidate backend inside a
    com.pebble.local bundle before any process starts, and accepts the same bytes in the candidate's test bundle;
  - the public text claims only what the product does: README, the user guide, the in-app What's new, the settings
    labels and the slash descriptions carry none of the retracted claims, and CHANGELOG 1.0.3 lists the corrections;
  - the release version is after 1.0.2 and every declaration agrees.
Usage: --report NEW_PATH [--backend FROZEN_EXECUTABLE] [--keep-scratch].
"""
import argparse
import hashlib
import importlib.util
import json
import os
import plistlib
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
from validate_phase20 import Service, backend_path, write_new  # noqa: E402
from validate_phase21 import checkout_version, declared_versions  # noqa: E402

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    'zoom_hotkeys_have_their_permission',
    'pdf_export_is_not_offered_until_it_exists',
    'evaluation_never_runs_the_installed_identity',
    'public_text_claims_only_what_the_product_does',
    'release_version_is_after_1_0_2',
    'backend_inputs_stable_during_campaign',
)
# The tested sources this validator reads; each is in verify_harness_candidate.sources(), so the campaign binds them.
READS = ('src-tauri/tauri.conf.json', 'src-tauri/capabilities/main.json', 'src-tauri/gen/schemas/desktop-schema.json',
         'backend/pebble/runtime_commands.py', 'src/components/ExportPanel.vue', 'src/slashCommands.ts',
         'benchmarks/drivers/live.py', 'scripts/bench_claude.py', 'backend/pebble/vault.py', 'README.md',
         'docs/USER_GUIDE.md', 'CHANGELOG.md', 'src/App.vue', 'src/components/AdvancedSettings.vue',
         'scripts/validate_phase30.py', 'backend/pebble/__init__.py', 'package.json', 'package-lock.json',
         'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock')
ZOOM = 'core:webview:allow-set-webview-zoom'
# Public text (README, the guide, What's new, the settings labels, the slash descriptions), whitespace collapsed:
# claims the D37 audit found the product does not keep, each with what it overstated.
RETRACTED = (
    ('Export to PDF', 'PDF export never succeeds in a release build'),
    ('as PDF, Word', 'PDF export never succeeds in a release build'),
    ('Control granted app windows', 'app windows cannot be granted'),
    ('App windows and browser tabs', 'app windows cannot be granted'),
    ('Browser and app-window actions', 'app windows cannot be granted, and only presses ask'),
    ('the folders, windows and tabs you grant', 'app windows cannot be granted'),
    ('documents and your apps.', 'Pebble can open an app, not use it'),
    ('to edit or forget', 'no memory can be edited; PEBBLE.md is the only file you edit'),
    ('Switch per conversation', 'the model choice applies to every conversation'),
    ('Results arrive as notifications', 'macOS notifications are off until you turn them on'),
    ('results arrive as notifications', 'macOS notifications are off until you turn them on'),
    ('Agents never write, run commands', 'worktree agents write in their own copy'),
    ('Turning a switch off applies to the next task', 'switches cannot change while a task runs'),
    ('ends every Chrome session at once', "only the Browser panel's Stop ends every Chrome session"),
    ('must have a passing test run of those changes', 'the gate holds only when covering tests exist'),
    ('saves a preference right away', '"Remember that" saves a preference only about you'),
    ('so it does not learn it again from the same place', 'Forget all lets Pebble learn again'),
    ("so it doesn't learn it again from the same place", 'Forget all lets Pebble learn again'),
    ('It is read like your preferences', 'PEBBLE.md is followed as standing instructions'),
    ('RTF, archives (listed', 'only ZIP archives are listed'),
)
PUBLIC = ('README.md', 'docs/USER_GUIDE.md', 'src/App.vue', 'src/components/AdvancedSettings.vue', 'src/slashCommands.ts')
CORRECTIONS = ('⌘+', '/stop', 'PDF', 'App windows', 'Memory', 'Pebble for Chrome', 'Models', 'Schedules')
BUNDLE_BACKEND = 'Contents/Resources/resources/backend/pebble-backend'


def load_script(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bench():
    backend_path()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from benchmarks import plan_24a
    from benchmarks.drivers import live
    return plan_24a, live


def digests():
    return {rel: sha((ROOT / rel).read_bytes()) for rel in READS}


def flat(text):
    return re.sub(r'\s+', ' ', text)


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


def capabilities():
    return [json.loads(path.read_text()) for path in sorted((ROOT / 'src-tauri/capabilities').glob('*.json'))]


def changelog_entry(version):
    text = read('CHANGELOG.md')
    match = re.search(r'^## \[' + re.escape(version) + r'\][^\n]*\n(.*?)(?=^## \[|\Z)', text, re.M | re.S)
    assert match, 'CHANGELOG.md has no entry for ' + version
    return match.group(1)


def notes_version(current, released='1.0.4'):
    """The release whose CHANGELOG entry carries what this validator proves: the checkout's own version up to
    `released`, and `released` once the checkout has moved past it (Phase 33, D60: at 1.1.0 the current entry would
    otherwise have to repeat 1.0.4's notes, which would be false for 1.1.0). Every assertion on the entry is unchanged."""
    parts = lambda v: tuple(int(x) for x in v.split('.'))
    return released if parts(current) > parts(released) else current


def fake_bundle(root, identifier, payload):
    """A minimal .app with the given CFBundleIdentifier and a backend file of the given bytes."""
    app = root / (identifier + '.app')
    (app / 'Contents').mkdir(parents=True)
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': identifier}))
    backend = app / BUNDLE_BACKEND
    backend.parent.mkdir(parents=True)
    backend.write_bytes(payload)
    backend.chmod(0o755)
    return backend


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

    def command(self):
        return [str(self.backend)] if self.backend else [sys.executable, str(ROOT / 'backend/main.py')]

    def run(self):
        scratch = self.scratch

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            return {'outside_checkout': True}
        self.check('fixture_scratch_created_outside_the_checkout', created)

        def zoom():
            windows = json.loads(read('src-tauri/tauri.conf.json'))['app']['windows']
            zoomed = [w['label'] for w in windows if w.get('zoomHotkeysEnabled')]
            assert 'main' in zoomed, 'the main window no longer turns on the zoom keys the guide promises'
            caps = capabilities()
            for label in zoomed:
                granted = {p if isinstance(p, str) else p.get('identifier') for c in caps
                           if label in c.get('windows', []) for p in c.get('permissions', [])}
                assert ZOOM in granted, (label, 'has the zoom keys but no', ZOOM)
            assert json.dumps(ZOOM) in read('src-tauri/gen/schemas/desktop-schema.json'), 'the schema does not know ' + ZOOM
            return {'windows_with_zoom_keys': zoomed, 'permission': ZOOM, 'capability_files': len(caps)}
        self.check('zoom_hotkeys_have_their_permission', zoom)

        def pdf():
            service = Service(self.command(), scratch / 'service-home', scratch / 'service', 'phase30-export')
            try:
                catalogue = service.rpc('command_catalogue')
                cid = service.rpc('new_chat')['id']
                refusal = None
                try:
                    service.rpc('send', id=cid, text='/export view1 pdf', source_ids=[])
                except RuntimeError as exc:
                    refusal = str(exc)
                listed = service.rpc('send', id=cid, text='/exports', source_ids=[])
            finally:
                service.close()
            export = next(c for c in catalogue if c['name'] == 'export')
            assert 'pdf' not in export['arguments'].lower(), ('/export still offers pdf', export['arguments'])
            assert refusal and 'PDF' in refusal and 'HTML' in refusal, ('/export pdf was not refused', refusal)
            assert listed.get('command') is True
            exports = scratch / 'service-home' / 'exports'
            assert not exports.exists() or not any(p.is_file() for p in exports.rglob('*')), 'a file was written'
            panel = read('src/components/ExportPanel.vue')
            formats = re.search(r'const FORMATS = \[(.*?)\];', panel, re.S)
            assert formats and "'pdf'" not in formats.group(1) and 'PDF' not in formats.group(1), 'the panel offers PDF'
            default = re.search(r"const format = ref\('([a-z]+)'\)", panel)
            assert default and default.group(1) in re.findall(r"id: '([a-z]+)'", formats.group(1)), 'the default is not offered'
            slash = re.search(r'EXPORT_FORMATS = Object\.freeze\(\[(.*?)\]\)', read('src/slashCommands.ts'), re.S)
            assert slash and '"pdf"' not in slash.group(1), 'the slash menu offers pdf'
            return {'service': 'frozen' if self.backend else 'source', 'export_arguments': export['arguments'],
                    'refused': True, 'files_written': 0, 'panel_default': default.group(1)}
        self.check('pdf_export_is_not_offered_until_it_exists', pdf)

        def identity():
            bp, live = bench()
            cli = load_script('bench_claude_for_phase30', 'scripts/bench_claude.py')
            payload = b'phase 30 candidate backend'
            plan = {'candidate': {'backend_sha256': hashlib.sha256(payload).hexdigest()}}
            owner = fake_bundle(scratch / 'release', 'com.pebble.local', payload)
            test = fake_bundle(scratch / 'test', 'com.pebble.candidate', payload)
            try:
                live.candidate_command(plan, owner)
                raise AssertionError('the release bundle was accepted as an evaluation backend')
            except bp.PlanRefused as exc:
                assert 'installed_identity' in str(exc), exc
            assert live.candidate_command(plan, test) == [str(test.resolve())]
            started = []

            class Measure:
                class Backend:
                    def __init__(self, argv, home):
                        started.append(argv)

            for fn in (cli.eval_provider, cli.finish_provider):
                try:
                    fn(backend=owner, home=scratch / 'eval-home', ledger=None, metadata=scratch / 'eval.json',
                       model='claude-opus-5-5', measure=Measure, installed=[])
                    raise AssertionError(fn.__name__ + ' accepted the release bundle')
                except bp.PlanRefused as exc:
                    assert 'installed_identity' in str(exc), exc
            assert started == [], ('a backend process started', started)
            return {'release_bundle': 'refused before any process', 'test_bundle': 'accepted', 'same_bytes': True}
        self.check('evaluation_never_runs_the_installed_identity', identity)

        def claims():
            found = []
            for rel in PUBLIC:
                text = flat(read(rel))
                found += [(rel, phrase, why) for phrase, why in RETRACTED if phrase in text]
            assert not found, found[:6]
            entry = flat(changelog_entry(notes_version(checkout_version())))
            assert '### Corrections' in entry, 'CHANGELOG has no Corrections list for this release'
            missing = [word for word in CORRECTIONS if word not in entry]
            assert not missing, ('the Corrections list does not cover', missing)
            limits = flat(read('README.md'))
            assert 'simulated Chrome' in limits, 'the README does not say how far Pebble for Chrome was tested'
            return {'files': list(PUBLIC), 'retracted_phrases': len(RETRACTED), 'corrections_listed': True}
        self.check('public_text_claims_only_what_the_product_does', claims)

        def version():
            current = checkout_version()
            parts = tuple(int(x) for x in current.split('.'))
            assert parts > (1, 0, 2), current
            declared = declared_versions()
            assert declared and set(declared.values()) == {current}, declared
            return {'version': current, 'declarations': len(declared)}
        self.check('release_version_is_after_1_0_2', version)


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase30-')).resolve()
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
