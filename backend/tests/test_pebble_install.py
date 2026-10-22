"""scripts/pebble_install.py on staged roots only (D20 §2.3-§2.7 and §6.7).

Every executed run passes --root under pytest's tmp_path. The installed and
candidate bundles are small fake Pebble.app trees with real Info.plist files;
the data home is a real legacy (epoch 0) home from test_migrations, upgraded by
the repository's own App through an in-process launcher. Process listing,
copying, codesign, launching, free space and time are injected. Path.home()
points at an empty sentinel that must stay empty, and the OS vault raises.
"""
import ast
import hashlib
import importlib.util
import io
import json
import os
import plistlib
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import attestation, data_home, migrations
from pebble.app import App
from pebble.release_readiness import Root, bundle_digest, bundle_entries
from pebble.runtime import claim_owner
from test_migrations import legacy_home

pytestmark = pytest.mark.skipif(not migrations.supported(), reason='needs POSIX descriptor-relative file operations')

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'pebble_install.py'
_spec = importlib.util.spec_from_file_location('pebble_install', SCRIPT)
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

STAMP = '20261013T120000Z'
T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc).timestamp()
OLD_BACKEND, NEW_BACKEND = b'pebble-backend 0.4.2 (fixture)', b'pebble-backend 0.5.0 (fixture)'
OLD_SHA, NEW_SHA = hashlib.sha256(OLD_BACKEND).hexdigest(), hashlib.sha256(NEW_BACKEND).hexdigest()
SENTINEL = 'sentinel-user-text-5c1f'  # test_migrations' user text: never in a record or a report
# test_migrations' legacy fixture holds a running and a waiting run: startup recovery appends their two
# interruption events (run_events is preserved append-only). The strict row check reports exactly that,
# and these tests accept that exact change by name, as the parent would after reading the receipt.
RECOVERY = ('--accept-row-change', 'run_events:1:3')


# ---- Guards: no real home, no vault --------------------------------------------------------------


@pytest.fixture(autouse=True)
def real_home(tmp_path, monkeypatch):
    """Path.home() is an empty sentinel; the tool must never call it on a staged root."""
    sentinel = tmp_path / 'real-home-sentinel'
    sentinel.mkdir()
    state = SimpleNamespace(calls=[], real_layout_allowed=False)

    def home(cls):
        state.calls.append(sys._getframe(1).f_code.co_filename)
        return sentinel
    monkeypatch.setattr(Path, 'home', classmethod(home))
    yield state
    assert list(sentinel.iterdir()) == [], 'a staged run wrote into the (sentinel) real home'
    if not state.real_layout_allowed:
        assert str(SCRIPT) not in state.calls, 'the tool consulted Path.home() on a staged root'


@pytest.fixture(autouse=True)
def no_vault(monkeypatch):
    from pebble import vault
    calls = []

    def refuse(name):
        def method(self, *args, **kwargs):
            calls.append(name)
            raise AssertionError('The install tests must never reach the OS vault.')
        return method
    for name in ('get', 'set', 'delete', '_find'):
        monkeypatch.setattr(vault.Vault, name, refuse(name))
    yield
    assert calls == []


# ---- Fixtures -----------------------------------------------------------------------------------


def put(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


def make_app(path, version, backend, identifier='com.pebble.local'):
    contents = path / 'Contents'
    put(contents / 'Info.plist', plistlib.dumps({'CFBundleIdentifier': identifier, 'CFBundleShortVersionString': version,
                                                 'CFBundleVersion': version, 'CFBundleExecutable': 'pebble'}))
    put(contents / 'MacOS/pebble', b'pebble shell ' + version.encode(), 0o755)
    resources = contents / 'Resources/resources'
    put(resources / 'backend/pebble-backend', backend, 0o755)
    framework = resources / 'backend/_internal/Python.framework'
    put(framework / 'Versions/3.14/Python', b'python ' + version.encode(), 0o755)
    os.symlink('3.14', framework / 'Versions/Current')
    os.symlink('Versions/Current/Python', framework / 'Python')
    put(resources / 'tokenizer/o200k', b'ranks')
    put(resources / 'model.json', b'{}')
    put(contents / '_CodeSignature/CodeResources', b'signature ' + version.encode())
    return path


def app_manifest(app, path, shape='apps'):
    """T1's seam: release_readiness.bundle_entries/bundle_digest over the .app root."""
    with Root(str(app), 'app') as root:
        entries, links, problems = bundle_entries(root)
    assert not problems
    item = {'bundle_sha256': bundle_digest(entries), 'files': entries, 'links': links}
    doc = {'format': 'pebble.app-manifest/1', 'apps': {'release': item}} if shape == 'apps' else item
    return put(path, json.dumps(doc).encode())


@pytest.fixture
def stage(tmp_path):
    root = tmp_path / 'stage'
    app = make_app(root / 'Applications/Pebble.app', '0.4.2', OLD_BACKEND)
    support = root / 'home/Library/Application Support'
    support.mkdir(parents=True)
    home = legacy_home(support, name='com.pebble.local')
    put(home / 'service.log', b'Traceback (most recent call last): an old one, before the install\n', 0o600)
    put(home / 'backend.log', b'', 0o600)
    put(home / 'runtime.key', b'k' * 32, 0o600)
    put(home / 'attachments/snapshot.bin', b'\x00attachment', 0o600)
    os.symlink('attachments/snapshot.bin', home / 'attachment-link')
    web = root / 'home/Library/WebKit/com.pebble.local'
    put(web / 'WebsiteData/LocalStorage/file__0.localstorage', b'pet drafts', 0o600)
    candidate = make_app(tmp_path / 'C/release/Pebble.app', '0.5.0', NEW_BACKEND)
    manifest = app_manifest(candidate, tmp_path / 'C/app-manifest.json')
    # V1-137, V1-173 (deliberate, Phase 24): new backup roots are '<stamp>-pre-install.noindex' and keep the bundle as
    # 'Pebble.app.backup'; Phase 21-23 roots ('-pre-phase21', 'Pebble.app') still load (test_install_postmove_24.py).
    return SimpleNamespace(tmp=tmp_path, root=root, app=app, home=home, web=web, candidate=candidate,
                           manifest=manifest, backup=root / f'home/PebbleBackups/{STAMP}-pre-install.noindex')


class Fakes:
    def __init__(self):
        self.rows, self.lists = [], 0
        self.copies, self.fail, self.corrupt = [], None, None
        self.codesign_exit, self.signed, self.unsigned = 0, [], None
        self.launches, self.launch = [], None
        self.free, self.verifier = 1 << 50, None
        self.now, self.mono, self.sleeps = T0, 0.0, []

    def lister(self):
        self.lists += 1
        return self.rows(self) if callable(self.rows) else list(self.rows)

    def copier(self, source, destination):
        self.copies.append((str(source), str(destination)))
        if self.fail and self.fail(source, destination):
            shutil.copytree(source, destination, symlinks=True,
                            ignore=lambda folder, names: [name for name in names if name == '_CodeSignature'])
            raise tool.CopyFailed('simulated interruption midway')
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
        if self.corrupt and self.corrupt(source, destination):
            target = next(path for path in sorted(destination.rglob('*'))
                          if path.is_file() and not path.is_symlink() and path.stat().st_size)
            data = bytearray(target.read_bytes())
            data[0] ^= 1
            target.write_bytes(bytes(data))

    def codesign(self, app):
        self.signed.append(str(app))
        return {'exit': 1 if self.unsigned and self.unsigned(app) else self.codesign_exit}

    def launcher(self, spec):
        """The candidate backend's startup, in process: the real App against the given home."""
        self.launches.append(spec)
        if self.launch:
            return self.launch(spec)
        home = Path(spec['home'])
        if spec.get('legacy_home'):
            # Phase 22: what backend/main.py does with the shell's --home ~/.pebble --legacy-home <old home>.
            layout = data_home.resolve(spec['home'], spec['legacy_home'])
            data_home.ensure_layout(layout)
            report = data_home.migrate(layout)
            home = Path(report['data'])
            data_home.activate(layout, home, report)
        app = App(home, Path(spec['resources']), lambda *event: None)
        responses = {}
        try:
            for request in spec['requests']:
                try:
                    responses[request['id']] = {'id': request['id'],
                                                'result': app.dispatch(request['method'], request['params'])}
                except Exception as error:
                    responses[request['id']] = {'id': request['id'], 'error': str(error)}
        finally:
            app.close()
            data_home.deactivate(home)
        return {'responses': responses, 'events': {'ready': 1}, 'exit': 0, 'timed_out': False}

    def clock(self):
        self.now += 1
        return self.now

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.mono += seconds

    def env(self):
        return tool.Env(lister=self.lister, copier=self.copier, codesign=self.codesign, launcher=self.launcher,
                        free_bytes=lambda path: self.free, verifier=self.verifier, clock=self.clock,
                        monotonic=self.monotonic, sleep=self.sleep)


def call(fakes, *argv):
    out = io.StringIO()
    code = tool.main([str(item) for item in argv], env=fakes.env(), out=out)
    return code, json.loads(out.getvalue())


def backup(fakes, stage, *extra, execute=True):
    return call(fakes, 'backup', '--root', stage.root, '--stamp', STAMP, '--expect-backend-sha256', OLD_SHA,
                *extra, *(['--execute'] if execute else []))


def install(fakes, stage, *extra, execute=True):
    return call(fakes, 'install', '--root', stage.root, '--stamp', STAMP, '--candidate', stage.candidate,
                '--app-manifest', stage.manifest, '--expect-backend-sha256', OLD_SHA, *extra,
                *(['--execute'] if execute else []))


def readback(fakes, stage, *extra):
    return call(fakes, 'readback', '--root', stage.root, '--stamp', STAMP, *extra, '--execute')


def rehearse(fakes, stage, work, *extra):
    return call(fakes, 'rehearse', '--root', stage.root, '--stamp', STAMP, '--candidate', stage.candidate,
                '--app-manifest', stage.manifest, '--rehearsal-dir', work, *RECOVERY, *extra, '--execute')


def drill(fakes, stage, root, *extra):
    return call(fakes, 'drill', '--root', root, '--source-app', stage.app, '--source-home', stage.home,
                '--source-webview', stage.web, '--candidate', stage.candidate, '--app-manifest', stage.manifest,
                '--expect-backend-sha256', OLD_SHA, *RECOVERY, *extra, '--execute')


def first_launch(fakes, stage):
    """What the user's first open of the installed app does to the data home."""
    fakes.launcher({'home': str(stage.home), 'resources': str(stage.app / tool.RESOURCES),
                    'requests': [dict(request) for request in tool.REQUESTS]})


def snapshot(top, times=True):
    """Every entry below top: type, mode, size, content hash or link target (and mtimes when asked)."""
    seen = {}
    for folder, dirs, files in os.walk(top):
        for name in dirs + files:
            path = os.path.join(folder, name)
            info = os.lstat(path)
            entry = [stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode)]
            if stat.S_ISREG(info.st_mode):
                entry += [info.st_size, hashlib.sha256(Path(path).read_bytes()).hexdigest()]
            elif stat.S_ISLNK(info.st_mode):
                entry.append(os.readlink(path))
            if times:
                entry.append(info.st_mtime_ns)
            seen[os.path.relpath(path, top)] = entry
    return seen


def tree(top):
    return {'.': stat.S_IMODE(os.lstat(top).st_mode), **snapshot(top, times=False)}


def release_payload(stage, **changes):
    # Merge (i20): pebble.release/1 is T3's shape (attestation.RELEASE_KEYS): backend_executable_sha256, plus
    # issued_at and not_after; the parent made T3 the authority for the format.
    with Root(str(stage.candidate), 'candidate') as root:
        entries, _, _ = bundle_entries(root)
    issued = datetime.fromtimestamp(T0, timezone.utc)
    return {'format': 'pebble.release/1', 'version': '0.5.0', 'build': 7, 'app_bundle_sha256': bundle_digest(entries),
            'backend_executable_sha256': NEW_SHA, 'min_data_epoch': 0, 'data_epoch': migrations.SCHEMA_EPOCH,
            'previous': OLD_SHA, 'rollback': hashlib.sha256((stage.backup / 'backup.json').read_bytes()).hexdigest(),
            'issued_at': attestation.stamp(issued), 'not_after': attestation.stamp(issued + timedelta(days=7)),
            **changes}


def user_version(home):
    with closing(sqlite3.connect((Path(home) / 'pebble.sqlite').as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        return db.execute('PRAGMA user_version').fetchone()[0]


# ---- Dry runs and refusals before any change -----------------------------------------------------


def test_dry_runs_change_nothing_and_print_the_exact_actions(stage):
    fakes = Fakes()
    before = snapshot(stage.tmp)
    code, report = backup(fakes, stage, execute=False)
    assert code == 0 and report['state'] == 'dry_run' and report['ok'] is True and report['execute'] is False
    ops = {(action['op'], action.get('source'), action.get('destination')) for action in report['actions']}
    assert {('ditto', str(stage.app), str(stage.backup / 'Pebble.app.backup')),  # V1-137: the bundle copy's new name
            ('ditto', str(stage.home), str(stage.backup / 'com.pebble.local')),
            ('ditto', str(stage.web), str(stage.backup / 'webview-store'))} <= ops
    assert [a['path'] for a in report['actions'] if a['op'] == 'write'] == [str(stage.backup / 'backup.json')]
    assert report['actions'][0]['state'] == 'done' and {a['state'] for a in report['actions'][1:]} == {'planned'}
    assert snapshot(stage.tmp) == before and fakes.copies == [] and 'record' not in report

    assert backup(fakes, stage)[0] == 0
    after = snapshot(stage.tmp)
    copies = len(fakes.copies)
    drill = ['drill', '--root', stage.tmp / 'drill', '--source-app', stage.backup / 'Pebble.app.backup', '--source-home',
             stage.backup / 'com.pebble.local', '--candidate', stage.candidate, '--app-manifest', stage.manifest,
             '--expect-backend-sha256', OLD_SHA]
    for argv, state in [
            (['plan', '--root', stage.root, '--stamp', STAMP, '--expect-backend-sha256', OLD_SHA], 'dry_run'),
            (['rehearse', '--root', stage.root, '--stamp', STAMP, '--candidate', stage.candidate, '--app-manifest',
              stage.manifest], 'dry_run'),
            (['install', '--root', stage.root, '--stamp', STAMP, '--candidate', stage.candidate, '--app-manifest',
              stage.manifest, '--expect-backend-sha256', OLD_SHA], 'dry_run'),
            (['rollback', '--root', stage.root, '--stamp', STAMP], 'dry_run'),
            (['readback', '--root', stage.root, '--stamp', STAMP], 'refused'),  # no install record yet
            (drill, 'dry_run')]:
        code, report = call(fakes, *argv)
        assert report['execute'] is False and report['state'] == state, (argv[0], report['error'])
        assert snapshot(stage.tmp) == after, argv[0]
    # codesign --verify is read-only: the install dry run checks the candidate's signature too.
    assert len(fakes.copies) == copies and fakes.launches == []
    assert fakes.signed == [str(stage.app), str(stage.backup / 'Pebble.app.backup'), str(stage.candidate)]
    code, report = call(fakes, 'plan', '--root', stage.root, '--stamp', STAMP, '--expect-backend-sha256', OLD_SHA)
    observed = report['results']['observed']
    assert observed['installed_backend_matches'] is True and observed['backups'] == [STAMP + '-pre-install.noindex']
    assert observed['data_home']['database'] == {'exists': True, 'valid': True, 'wal_mode': True, 'user_version': 0}
    assert [step['who'] for step in report['results']['steps']][:2] == ['user', 'parent']


def test_a_process_inside_the_bundle_blocks_backup_and_install_but_a_similar_name_elsewhere_does_not(stage):
    fakes = Fakes()
    shell = str(stage.app / 'Contents/MacOS/pebble')
    for rows in ([(41, shell)],
                 [(42, str(stage.app / tool.BACKEND))],  # the job supervisor shares the bundled backend
                 [(43, shell.replace('Pebble.app', 'PEBBLE.APP'))]):  # APFS is case-insensitive
        fakes.rows = rows
        code, report = backup(fakes, stage)
        assert code == 3 and report['state'] == 'refused' and report['error']['code'] == 'pebble_not_running'
        assert report['error']['processes'] == [{'pid': rows[0][0], 'executable': rows[0][1]}]
        assert report['error']['waited_seconds'] >= 30 and not stage.backup.exists() and fakes.copies == []
    fakes.rows = [(44, str(stage.root / 'Applications/Pebble.app.old/Contents/MacOS/pebble')),
                  (45, str(stage.candidate / 'Contents/MacOS/pebble')),
                  (46, str(stage.root / 'Applications/Pebble Candidate.app/Contents/MacOS/pebble')),
                  (47, str(stage.root / 'Applications/Pebble.app')),  # the bundle path itself is not inside Contents/
                  (48, '/usr/local/bin/pebble'), (49, None)]
    code, report = backup(fakes, stage)
    assert code == 0 and report['state'] == 'done', report['error']
    fakes.rows = [(41, shell)]
    before = snapshot(stage.root / 'Applications')
    code, report = install(fakes, stage)
    assert code == 3 and report['error']['code'] == 'pebble_not_running'
    assert snapshot(stage.root / 'Applications') == before and not (stage.backup / 'Pebble.app.moved').exists()


def test_backup_waits_for_the_user_to_quit_and_never_signals(stage, monkeypatch):
    fakes = Fakes()
    fakes.rows = lambda f: [(41, str(stage.app / 'Contents/MacOS/pebble'))] if f.lists <= 2 else []
    signals = []
    monkeypatch.setattr(os, 'kill', lambda *args: signals.append(args))
    monkeypatch.setattr(os, 'killpg', lambda *args: signals.append(args))
    code, report = backup(fakes, stage, '--wait-seconds', '10')
    assert code == 0 and signals == [] and len(fakes.sleeps) == 2
    assert next(c for c in report['checks'] if c['check'] == 'pebble_not_running')['polls'] == 3


# Merge (i20), X20 F20-04: gate() now refuses a link anywhere between a staged root and its layout paths, so a linked
# data home stops at staged_root_confined, before the data-home check; a regular file there keeps that check covered.
@pytest.mark.parametrize('case, code', [('wrong_hash', 'installed_backend'), ('no_space', 'free_space'),
                                        ('linked_home', 'staged_root_confined'),
                                        ('file_home', 'data_home_is_directory'),
                                        ('foreign_identifier', 'installed_identifier'),
                                        ('lister_fails', 'process_list_unavailable')])
def test_backup_refuses_before_any_change(stage, case, code):
    fakes, expect = Fakes(), OLD_SHA
    if case == 'wrong_hash':
        expect = '0' * 64
    elif case == 'no_space':
        fakes.free = 1000
    elif case == 'linked_home':
        real = stage.home.parent / 'elsewhere'
        os.rename(stage.home, real)
        os.symlink(real, stage.home)
    elif case == 'file_home':
        os.rename(stage.home, stage.home.parent / 'elsewhere')
        stage.home.write_bytes(b'not a folder\n')
    elif case == 'foreign_identifier':
        plist = stage.app / 'Contents/Info.plist'
        plist.write_bytes(plistlib.dumps({**plistlib.loads(plist.read_bytes()), 'CFBundleIdentifier': 'com.pebble.candidate'}))
    else:
        def broken(fakes):
            raise OSError('libproc unavailable')
        fakes.rows = broken
    before = snapshot(stage.tmp)
    exit_code, report = call(fakes, 'backup', '--root', stage.root, '--stamp', STAMP, '--expect-backend-sha256', expect,
                             '--execute')
    assert exit_code == 3 and report['state'] == 'refused' and report['error']['code'] == code
    assert snapshot(stage.tmp) == before and fakes.copies == []


def test_an_existing_backup_root_is_refused_and_left_untouched(stage):
    put(stage.backup / 'evidence.txt', b'keep me')
    before = snapshot(stage.tmp)
    code, report = backup(Fakes(), stage)
    assert code == 3 and report['error']['code'] == 'backup_root_absent' and report['error']['path'] == str(stage.backup)
    assert snapshot(stage.tmp) == before and 'record' not in report


def test_unflushed_wal_and_a_held_service_lease_are_refused(stage):
    with closing(sqlite3.connect(stage.home / 'pebble.sqlite', isolation_level=None)) as writer:
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("INSERT INTO settings VALUES('wal-only', '1')")
        code, report = backup(Fakes(), stage)
        assert code == 3 and report['error']['code'] == 'wal_checkpointed' and report['error']['wal_bytes'] > 0
    lease = claim_owner(stage.home)
    try:
        code, report = backup(Fakes(), stage)
        assert code == 3 and report['error']['code'] == 'data_home_not_owned'
        assert report['error']['locks'] == ['runtime-owner.lock']
    finally:
        lease.close()
    assert not stage.backup.exists()


# ---- Backup ------------------------------------------------------------------------------------


def test_backup_records_both_trees_and_never_opens_the_live_home(stage):
    home_before, app_before = snapshot(stage.home), snapshot(stage.app)
    code, report = backup(Fakes(), stage)
    assert code == 0 and report['state'] == 'done', report['error']
    # SQLite never opened the live home: no -wal or -shm appeared, and nothing else changed.
    assert snapshot(stage.home) == home_before and snapshot(stage.app) == app_before
    B = stage.backup
    assert stat.S_IMODE(os.lstat(B).st_mode) == 0o700 and stat.S_IMODE(os.lstat(B / 'backup.json').st_mode) == 0o600
    data = (B / 'backup.json').read_bytes()
    assert report['results']['backup_json_sha256'] == hashlib.sha256(data).hexdigest()
    doc = json.loads(data)
    assert doc['format'] == 'pebble.install-backup/1' and doc['stamp'] == STAMP and doc['complete'] is True
    assert doc['installed']['backend_sha256'] == OLD_SHA and doc['installed']['version'] == '0.4.2'
    assert doc['database']['integrity'] == 'ok' and doc['database']['user_version'] == 0
    assert doc['inspect']['state'] == 'upgrade_required' and doc['inspect']['database']['rows']['chats'] == 2
    assert doc['upgrade_backup_ids'] == [] and doc['inspected_copy'] == 'inspection'
    assert {'pebble.sqlite', 'runtime.key', 'service.log', 'attachments/snapshot.bin'} <= {
        f['path'] for f in doc['data_home']['files']}
    assert doc['data_home']['links'] == [{'path': 'attachment-link', 'target': 'attachments/snapshot.bin'}]
    framework = 'Contents/Resources/resources/backend/_internal/Python.framework'
    assert {link['path'] for link in doc['app']['links']} == {framework + '/Python', framework + '/Versions/Current'}
    assert doc['webview']['file_count'] == 1 and doc['webview']['source'] == str(stage.web)
    assert tree(B / 'Pebble.app.backup') == tree(stage.app) and tree(B / 'com.pebble.local') == tree(stage.home)
    assert tree(B / 'webview-store') == tree(stage.web)
    # inspect ran only on the scratch copy, which is where its -wal/-shm appeared.
    assert not (B / 'com.pebble.local/pebble.sqlite-shm').exists() and (B / 'inspection/pebble.sqlite-shm').exists()
    assert SENTINEL not in json.dumps(report) and SENTINEL not in data.decode()
    printed = report['results']
    assert printed['data_files'] == doc['data_home']['file_count'] and printed['webview_copied'] is True
    assert Path(report['record']['path']).parent == B


@pytest.mark.parametrize('target, code', [('Pebble.app.backup', 'bundle_copy_matches'),  # V1-137: new copy name
                                          ('com.pebble.local', 'data_copy_matches'),
                                          ('webview-store', 'webview_copy_matches')])
def test_a_corrupted_copy_fails_the_manifest_check(stage, target, code):
    fakes = Fakes()
    fakes.corrupt = lambda source, destination: destination.name == target
    originals = {path: snapshot(path) for path in (stage.app, stage.home, stage.web)}
    exit_code, report = backup(fakes, stage)
    assert exit_code == 4 and report['state'] == 'stopped' and report['error']['code'] == code
    assert report['error']['diff']['counts'] == {'missing': 0, 'extra': 0, 'changed': 1}
    assert not (stage.backup / 'backup.json').exists()
    assert {path: snapshot(path) for path in originals} == originals
    assert Path(report['record']['path']).name.startswith('backup-run-')  # the failed run is kept as evidence


# ---- Rehearsal ----------------------------------------------------------------------------------


def test_rehearsal_upgrades_a_copy_with_the_candidate_and_restores_its_backup(stage):
    fakes = Fakes()
    assert backup(fakes, stage)[0] == 0
    home_before, copy_before = snapshot(stage.home), snapshot(stage.backup / 'com.pebble.local')
    candidate_before = snapshot(stage.candidate)
    # Strict by default: the recovery's appended events fail the rehearsal until accepted exactly.
    code, report = call(fakes, 'rehearse', '--root', stage.root, '--stamp', STAMP, '--candidate', stage.candidate,
                        '--app-manifest', stage.manifest, '--rehearsal-dir', stage.tmp / 'strict', '--execute')
    assert code == 4 and report['error']['code'] == 'row_counts_unchanged'
    assert report['error']['changes'] == [{'table': 'run_events', 'before': 1, 'after': 3}]
    fakes.launches.clear()
    work = stage.tmp / 'rehearsal'
    code, report = rehearse(fakes, stage, work)
    assert code == 0 and report['state'] == 'done', report['error']
    assert report['results']['row_changes']['accepted'] == [{'table': 'run_events', 'before': 1, 'after': 3}]
    [spec] = fakes.launches
    assert spec['backend'] == str(stage.candidate / tool.BACKEND) and spec['home'] == str(work / 'home')
    assert spec['resources'] == str(stage.candidate / tool.RESOURCES) and spec['keychain'] is False
    assert [request['method'] for request in spec['requests']] == ['state', 'upgrade_backups']
    assert {str(stage.root / 'home/Library'), str(stage.root / 'home/PebbleBackups'), str(stage.candidate)} <= set(
        spec['protected'])
    results = report['results']
    assert results['launch']['responses']['state']['ok'] is True
    assert results['launch']['responses']['upgrade_backups'] == {'answered': True, 'ok': False, 'error': 'Unknown request.'}
    assert results['inspect']['state'] == 'current'
    assert results['inspect']['database']['user_version'] == migrations.SCHEMA_EPOCH
    assert results['inspect']['database']['integrity'] == 'ok' and results['row_changes']['unexplained'] == []
    assert [item['valid'] for item in results['new_upgrade_backups']] == [True]
    assert results['restore_copy']['restored'] is True and results['restore_copy']['logical_match'] is True
    assert results['passed'] is True and (work / 'restored/pebble.sqlite').exists()
    assert snapshot(stage.home) == home_before and snapshot(stage.backup / 'com.pebble.local') == copy_before
    assert snapshot(stage.candidate) == candidate_before
    kept = json.loads(Path(report['record']['path']).read_text())
    assert kept['ok'] is True and kept['results']['backup_json_sha256'] == results['backup_json_sha256']
    assert SENTINEL not in json.dumps(kept)


@pytest.mark.parametrize('case, code', [('not_upgraded', 'state_current'), ('timeout', 'launch_completed'),
                                        ('maintenance', 'launch_state')])
def test_a_failed_rehearsal_is_reported_and_kept(stage, case, code):
    fakes = Fakes()
    backup(fakes, stage)
    results = {'not_upgraded': {'chats': []}, 'maintenance': {'maintenance': {'reason': 'backup_budget_exhausted'}},
               'timeout': {}}

    def launch(spec):
        return {'responses': {r['id']: {'id': r['id'], 'result': results[case]} for r in spec['requests']},
                'exit': 0, 'timed_out': case == 'timeout'}
    fakes.launch = launch
    exit_code, report = rehearse(fakes, stage, stage.tmp / 'rehearsal')
    assert exit_code == 4 and report['state'] == 'stopped' and report['error']['code'] == code
    assert json.loads(Path(report['record']['path']).read_text())['ok'] is False


# ---- Install ----------------------------------------------------------------------------------


@pytest.mark.parametrize('case, code', [
    ('tree_changed', 'candidate_matches_manifest'), ('not_higher', 'version_higher'),
    ('previous', 'release_previous'), ('rollback', 'release_rollback'), ('bundle', 'release_app_bundle'),
    ('backend', 'release_backend'), ('epoch', 'release_data_epoch'),
    ('unsigned_required', 'release_manifest_unsigned'), ('no_verifier', 'release_verifier_unavailable'),
    ('envelope_without_verifier', 'release_envelope_unverifiable'), ('bad_signature', 'release_signature_invalid'),
    ('installed_changed', 'installed_unchanged_since_backup'), ('backup_tampered', 'backup_bundle_intact'),
    ('home_changed', 'data_home_unchanged_since_backup'), ('webview_changed', 'webview_unchanged_since_backup'),
    ('candidate_unsigned', 'codesign_verified')])
def test_install_refuses_before_moving_anything(stage, case, code):
    fakes = Fakes()
    assert backup(fakes, stage)[0] == 0
    copies = len(fakes.copies)
    extra = []
    release = stage.tmp / 'release.json'
    envelope = {'format': 'pebble.attestation/1', 'key_id': '0' * 16, 'payload': 'opaque', 'signature': 'opaque'}
    if case == 'tree_changed':
        put(stage.candidate / 'Contents/Resources/resources/model.json', b'{"changed": true}')
    elif case == 'not_higher':
        shutil.rmtree(stage.candidate.parent)  # test fixture only: rebuild the candidate at the same version
        make_app(stage.candidate, '0.4.2', NEW_BACKEND)
        app_manifest(stage.candidate, stage.manifest)
    elif case in ('previous', 'rollback', 'bundle', 'backend', 'epoch'):
        field = {'previous': 'previous', 'rollback': 'rollback', 'bundle': 'app_bundle_sha256',
                 'backend': 'backend_executable_sha256', 'epoch': 'data_epoch'}[case]  # merge (i20): T3's field name
        put(release, json.dumps(release_payload(stage, **{field: 3 if case == 'epoch' else 'a' * 64})).encode())
        extra = ['--release-manifest', release]
    elif case == 'unsigned_required':
        fakes.verifier = lambda data: pytest.fail('a plain manifest never reaches the verifier')
        put(release, json.dumps(release_payload(stage)).encode())
        extra = ['--release-manifest', release, '--require-signed']
    elif case == 'no_verifier':
        put(release, json.dumps(envelope).encode())
        extra = ['--release-manifest', release, '--require-signed']
    elif case == 'envelope_without_verifier':
        put(release, json.dumps(envelope).encode())
        extra = ['--release-manifest', release]
    elif case == 'bad_signature':
        def verifier(data, **context):  # merge (i20): the verifier now also receives T3's install facts
            raise ValueError('bad_signature')
        fakes.verifier = verifier
        put(release, json.dumps(envelope).encode())
        extra = ['--release-manifest', release, '--require-signed']
    elif case == 'installed_changed':
        put(stage.app / 'Contents/Resources/resources/model.json', b'{"edited": true}')
    elif case == 'home_changed':
        put(stage.home / 'exports/after-backup.md', b'made after the backup', 0o600)
    elif case == 'webview_changed':
        put(stage.web / 'WebsiteData/LocalStorage/file__0.localstorage', b'a newer draft', 0o600)
    elif case == 'candidate_unsigned':
        fakes.unsigned = lambda app: Path(app) == stage.candidate
    else:
        put(stage.backup / 'Pebble.app.backup/Contents/MacOS/pebble', b'tampered', 0o755)
    before = snapshot(stage.tmp)
    exit_code, report = install(fakes, stage, *extra)
    assert exit_code == 3 and report['state'] == 'refused' and report['error']['code'] == code, report['error']
    assert snapshot(stage.tmp) == before and len(fakes.copies) == copies


# Parent follow-up 3: a pinned release key requires an accepted signed release manifest, not only --require-signed.
# Trust roots are injected only as release_verifier's arguments; nothing else can add one.

SSH_KEYGEN = '/usr/bin/ssh-keygen'


def release_keys(tmp_path, *names):
    """Throwaway keys made by /usr/bin/ssh-keygen in tmp (T3's own test helper) and a root entry per name."""
    if not os.access(SSH_KEYGEN, os.X_OK):
        pytest.skip('Needs /usr/bin/ssh-keygen for throwaway keys.')
    from test_attestation import Keys, root_for
    (tmp_path / 'keys').mkdir()
    keys = Keys(tmp_path / 'keys')
    roots = {}
    for name, role in names:
        keys.make(name)
        kid, entry = root_for(keys.pub(name), (role,), label='test-' + name)
        roots[name] = {kid: entry}
    return keys, roots


def signed_release(keys, stage, **changes):
    payload = attestation.canonical(release_payload(stage, **changes))
    return attestation.build_envelope(payload, keys.sign(keys.release, payload))


def test_a_pinned_release_key_requires_an_accepted_signed_manifest(stage):
    keys, roots = release_keys(stage.tmp, ('release', 'release'), ('operator', 'operator'))
    assert tool.release_key_pinned(tool.release_verifier(roots['release'], {}))
    assert not tool.release_key_pinned(tool.release_verifier(roots['operator'], {}))  # no release role: not pinned
    fakes = Fakes()
    fakes.verifier = tool.release_verifier(roots['release'], {})
    assert backup(fakes, stage)[0] == 0
    unsigned = put(stage.tmp / 'unsigned.json', json.dumps(release_payload(stage)).encode())
    foreign = put(stage.tmp / 'foreign.json', signed_release(keys, stage, rollback='a' * 64))
    signed = put(stage.tmp / 'signed.json', signed_release(keys, stage))
    copies, before = len(fakes.copies), snapshot(stage.tmp)
    for extra, code in (((), 'release_manifest_given'), (('--release-manifest', unsigned), 'release_manifest_unsigned'),
                        (('--release-manifest', foreign), 'rollback_mismatch')):  # T3's own refusal code
        exit_code, report = install(fakes, stage, *extra)  # never --require-signed: the pinned key alone requires it
        assert exit_code == 3 and report['error']['code'] == code, report['error']
        assert report['results']['release_required'] == {'required': True, 'require_signed': False,
                                                         'release_key_pinned': True, 'drill_exempt': False}
    assert snapshot(stage.tmp) == before and len(fakes.copies) == copies
    exit_code, report = install(fakes, stage, '--release-manifest', signed)
    assert exit_code == 0 and report['state'] == 'done', report['error']
    release = report['results']['release']
    assert release['signed'] is True and release['verdict']['status'] == 'accepted' and release['release_keys_pinned'] == 1
    assert tree(stage.app) == tree(stage.candidate)


def test_with_no_release_key_pinned_the_flag_alone_decides_as_before(stage):
    fakes = Fakes()
    fakes.verifier = tool.release_verifier({}, {})  # the shipped trust_roots.py is empty too
    assert not tool.release_key_pinned(fakes.verifier) and not tool.release_key_pinned(tool.default_release_verifier())
    assert backup(fakes, stage)[0] == 0
    exit_code, report = install(fakes, stage, '--require-signed')
    assert exit_code == 3 and report['error']['code'] == 'release_manifest_given'
    exit_code, report = install(fakes, stage)
    assert exit_code == 0 and report['state'] == 'done', report['error']
    assert report['results']['release'] is None and report['results']['release_required'] == {
        'required': False, 'require_signed': False, 'release_key_pinned': False, 'drill_exempt': False}


def test_a_drills_own_staged_install_is_exempt_from_a_pinned_release_key(stage):
    # Its backup.json is the drill's, which no real release manifest can name (T3 binds `rollback` to the backup).
    keys, roots = release_keys(stage.tmp, ('release', 'release'))
    fakes = Fakes()
    fakes.verifier = tool.release_verifier(roots['release'], {})
    code, report = drill(fakes, stage, stage.tmp / 'drill')
    assert code == 0 and report['results']['passed'] is True, report['error']
    assert report['results']['steps']['install']['ok'] is True
    installed = json.loads(Path(report['results']['steps']['install']['record']['path']).read_text())
    assert installed['results']['release_required'] == {'required': False, 'require_signed': False,
                                                        'release_key_pinned': True, 'drill_exempt': True}


def test_an_interrupted_copy_keeps_the_moved_aside_bundle_and_rollback_recovers(stage):
    fakes = Fakes()
    assert backup(fakes, stage)[0] == 0
    original_app, original_home = tree(stage.app), tree(stage.home)
    fakes.fail = lambda source, destination: source == stage.candidate
    code, report = install(fakes, stage)
    assert code == 4 and report['state'] == 'stopped' and report['error']['code'] == 'copy_interrupted'
    moved = stage.backup / 'Pebble.app.moved'
    assert report['error']['moved_bundle_intact'] is True and tree(moved) == original_app
    assert (stage.app / 'Contents/MacOS/pebble').exists() and not (stage.app / 'Contents/_CodeSignature').exists()
    assert any('rollback' in line for line in report['recovery']) and Path(report['record']['path']).exists()
    code, report = call(fakes, 'rollback', '--root', stage.root, '--stamp', STAMP, '--execute')
    assert code == 0 and report['results']['passed'] is True, report['error']
    assert tree(stage.app) == original_app and tree(stage.home) == original_home and tree(moved) == original_app
    failed = Path(report['results']['failed_bundle'])
    assert failed.parent == stage.backup and (failed / 'Contents/MacOS/pebble').exists()  # the partial copy is kept
    # After the user opens the restored app: one shell and one backend from it, its own epoch and rows.
    fakes.rows = [(1, str(stage.app / tool.SHELL)), (2, str(stage.app / tool.BACKEND))]
    code, report = readback(fakes, stage, '--mode', 'rollback')
    assert code == 0 and report['results']['passed'] is True, report['error']
    assert report['results']['logs']['service.log']['tracebacks'] == 0


def test_install_then_readback_passes_and_catches_processes_rows_tree_and_tracebacks(stage):
    fakes = Fakes()
    assert backup(fakes, stage)[0] == 0
    envelope = put(stage.tmp / 'release.json', json.dumps({'format': 'pebble.attestation/1', 'key_id': '0' * 16,
                                                           'payload': 'opaque', 'signature': 'opaque'}).encode())
    payload, seen = release_payload(stage), []
    # Merge (i20): the verifier is T3's contract: the envelope plus this install's facts in, a ReleaseVerdict out.
    fakes.verifier = lambda data, **context: (seen.append((data, context)),
                                              attestation.ReleaseVerdict('accepted', manifest=payload))[1]
    code, report = install(fakes, stage, '--release-manifest', envelope, '--require-signed')
    assert code == 0 and report['state'] == 'done', report['error']
    assert report['results']['release']['signed'] is True and [data for data, _ in seen] == [envelope.read_bytes()]
    assert seen[0][1]['installed_backend_sha256'] == OLD_SHA and seen[0][1]['backend_executable_sha256'] == NEW_SHA
    assert seen[0][1]['data_epoch'] == migrations.SCHEMA_EPOCH and seen[0][1]['installed_version'] == '0.4.2'
    assert tree(stage.app) == tree(stage.candidate) and fakes.signed[-1] == str(stage.app)
    assert tree(stage.backup / 'Pebble.app.moved') == tree(stage.backup / 'Pebble.app.backup')
    first_launch(fakes, stage)

    shell, backend = str(stage.app / tool.SHELL), str(stage.app / tool.BACKEND)
    fakes.rows = [(10, shell), (11, backend), (12, str(stage.candidate / tool.BACKEND))]
    code, report = readback(fakes, stage, *RECOVERY)
    assert code == 0 and report['results']['passed'] is True, report['error']
    assert report['results']['processes']['similar_outside_bundle'] == [
        {'pid': 12, 'executable': str(stage.candidate / tool.BACKEND)}]
    assert report['results']['inspect']['state'] == 'current'
    assert [item['valid'] for item in report['results']['new_upgrade_backups']] == [True]
    assert report['results']['logs']['service.log']['tracebacks'] == 0  # the pre-install traceback is not new
    assert Path(report['record']['path']).name.startswith('readback-install-')

    fakes.rows = [(10, shell), (13, shell), (11, backend)]
    code, report = readback(fakes, stage, *RECOVERY)
    assert code == 3 and report['error']['code'] == 'one_shell' and report['error']['found'] == 2
    assert Path(report['record']['path']).exists()  # a failed readback is kept: it triggers the rollback
    service = [backend, '--home', str(stage.home), '--resources', str(stage.app / tool.RESOURCES)]
    fakes.rows = [(10, shell, [shell]), (11, backend, service), (14, backend, [backend, '--mcp-stdio-worker']),
                  (15, backend, [backend, '--job-anchor', '/private/tmp/job'])]
    code, report = readback(fakes, stage, *RECOVERY)
    assert code == 0 and [row['pid'] for row in report['results']['processes']['workers']] == [14, 15]
    fakes.rows = [(10, shell), (11, backend, service), (16, backend, service)]
    code, report = readback(fakes, stage, *RECOVERY)
    assert report['error']['code'] == 'one_backend' and report['error']['found'] == 2
    fakes.rows = [(10, shell), (11, backend, service), (17, backend, None)]  # unreadable argv counts as a service
    assert readback(fakes, stage, *RECOVERY)[1]['error']['code'] == 'one_backend'
    fakes.rows = [(10, shell), (11, backend)]

    with closing(sqlite3.connect(stage.home / 'pebble.sqlite', isolation_level=None)) as db:
        db.execute("INSERT INTO chats VALUES('extra-chat', 'Added after install', NULL, 0)")
    code, report = readback(fakes, stage, *RECOVERY)
    assert report['error']['code'] == 'row_counts_unchanged'
    assert report['error']['changes'] == [{'table': 'chats', 'before': 2, 'after': 3}]
    code, report = readback(fakes, stage, *RECOVERY, '--accept-row-change', 'chats:2:4')
    assert report['error']['code'] == 'row_counts_unchanged'  # an acceptance must match exactly
    assert report['results']['row_changes']['unused_acceptances'] == [['chats', 2, 4]]
    both = (*RECOVERY, '--accept-row-change', 'chats:2:3')
    code, report = readback(fakes, stage, *both)
    assert code == 0 and report['results']['row_changes']['accepted'] == [
        {'table': 'chats', 'before': 2, 'after': 3}, {'table': 'run_events', 'before': 1, 'after': 3}]

    extra = put(stage.app / 'Contents/Resources/resources/extra.txt', b'not in the manifest')
    code, report = readback(fakes, stage, *both)
    assert report['error']['code'] == 'installed_tree_matches'
    assert report['error']['diff']['extra'] == ['Contents/Resources/resources/extra.txt']
    extra.unlink()  # test fixture cleanup, not the tool
    swapped = app_manifest(stage.app, stage.tmp / 'other-manifest.json', shape='single')
    code, report = readback(fakes, stage, *both, '--app-manifest', swapped)
    assert report['error']['code'] == 'app_manifest_unchanged'  # same tree, but not the manifest the install checked

    with (stage.home / 'backend.log').open('ab') as log:
        log.write(b'ERROR:root:Request failed\nTraceback (most recent call last):\n  File "x"\n')
    code, report = readback(fakes, stage, *both)
    assert report['error']['code'] == 'no_new_tracebacks' and report['error']['logs']['backend.log']['tracebacks'] == 1


def test_real_installs_need_passed_rehearsal_and_drill_reports_bound_to_this_candidate(stage):
    fakes = Fakes()
    backup(fakes, stage)
    code, rehearsal = rehearse(fakes, stage, stage.tmp / 'rehearsal')
    assert code == 0, rehearsal['error']
    bundle = rehearsal['results']['candidate']['bundle_sha256']

    def drill_report(**results):
        return put(stage.tmp / f'drill-{len(results)}.json', json.dumps({
            'format': 'pebble.install-run/1', 'command': 'drill', 'ok': True, 'execute': True,
            'results': {'candidate': {'bundle_sha256': bundle}, 'expected_backend_sha256': OLD_SHA, **results}}).encode())
    cases = [(stage.tmp / 'missing.json', drill_report(), 'rehearse_report_unreadable'),
             (rehearsal['record']['path'], drill_report(expected_backend_sha256='f' * 64), 'drill_report_binds'),
             (rehearsal['record']['path'], drill_report(candidate={'bundle_sha256': 'e' * 64}, x=1), 'drill_report_binds')]
    for rehearsal_path, drill_path, expected in cases:
        code, report = install(fakes, stage, '--rehearsal-report', rehearsal_path, '--drill-report', drill_path)
        assert code == 3 and report['error']['code'] == expected
    code, report = install(fakes, stage, '--rehearsal-report', rehearsal['record']['path'], '--drill-report',
                           drill_report(), execute=False)
    assert code == 0 and report['state'] == 'dry_run', report['error']
    assert report['results']['rehearse_report']['path'] == rehearsal['record']['path']


# ---- Rollback and the drill ---------------------------------------------------------------------


def test_rollback_refuses_a_tampered_backup_before_moving_anything(stage):
    fakes = Fakes()
    backup(fakes, stage)
    assert install(fakes, stage)[0] == 0
    put(stage.backup / 'com.pebble.local/runtime.key', b'x' * 32, 0o600)
    before = snapshot(stage.tmp)
    code, report = call(fakes, 'rollback', '--root', stage.root, '--stamp', STAMP, '--execute')
    assert code == 3 and report['error']['code'] == 'backup_data_intact'
    assert report['error']['diff']['changed'] == ['runtime.key'] and snapshot(stage.tmp) == before


def test_the_drill_restores_both_trees_byte_for_byte(stage):
    fakes = Fakes()
    root = stage.tmp / 'drill'
    code, report = drill(fakes, stage, root)
    assert code == 0 and report['results']['passed'] is True, report['error']
    steps = report['results']['steps']
    assert [steps[name]['ok'] for name in ('backup', 'install', 'readback', 'rollback')] == [True] * 4
    staged_home = root / 'home/Library/Application Support/com.pebble.local'
    [spec] = fakes.launches
    assert spec['home'] == str(staged_home) and spec['backend'] == str(root / 'Applications/Pebble.app' / tool.BACKEND)
    assert all(value.startswith(str(root)) for value in report['layout'].values() if value)
    # Independent of the tool's manifests: the restored trees equal the sources byte for byte.
    assert tree(root / 'Applications/Pebble.app') == tree(stage.app)
    assert tree(staged_home) == tree(stage.home) and user_version(staged_home) == 0
    assert tree(root / 'home/Library/WebKit/com.pebble.local') == tree(stage.web)
    # Nothing was deleted: the candidate and the upgraded home sit beside the backup.
    B = root / 'home/PebbleBackups' / (report['results']['stamp'] + '-pre-install.noindex')  # V1-137: new root name
    assert tree(B / 'Pebble.app.moved') == tree(stage.app)
    [failed] = B.glob('Pebble.app.failed-*')
    [after] = B.glob('com.pebble.local.after-*')
    assert tree(failed) == tree(stage.candidate) and user_version(after) == migrations.SCHEMA_EPOCH
    kept = json.loads((root / 'drill-report.json').read_text())
    assert kept['ok'] is True and kept['results']['restored'] == {**report['results']['restored']}
    assert stat.S_IMODE(os.lstat(root).st_mode) == 0o700


def test_the_drill_refuses_an_existing_root_and_inputs_inside_it(stage):
    fakes = Fakes()
    argv = ['--candidate', stage.candidate, '--app-manifest', stage.manifest, '--expect-backend-sha256', OLD_SHA,
            '--execute']
    code, report = call(fakes, 'drill', '--root', stage.root, '--source-app', stage.app, '--source-home', stage.home,
                        *argv)
    assert code == 3 and report['error']['code'] == 'drill_root_absent'
    code, report = call(fakes, 'drill', '--source-app', stage.app, '--source-home', stage.home, *argv)
    assert code == 2 and report['error']['code'] == 'drill_root_given'
    assert fakes.copies == []


# ---- Safety properties --------------------------------------------------------------------------


FORBIDDEN = {'remove', 'unlink', 'rmdir', 'removedirs', 'rmtree', 'replace', 'truncate', 'ftruncate', 'kill', 'killpg',
             'send_signal', 'terminate'}


def test_no_code_path_deletes_replaces_or_signals(stage, monkeypatch):
    source = ast.parse(SCRIPT.read_text())
    violations = []

    class Calls(ast.NodeVisitor):
        def __init__(self):
            self.where = ['<module>']

        def visit_FunctionDef(self, node):
            self.where.append(node.name)
            self.generic_visit(node)
            self.where.pop()

        def visit_Call(self, node):
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name) else None)
            if name in FORBIDDEN and not (name == 'kill' and self.where[-1] == 'sandboxed_launcher'):
                violations.append((name, node.lineno))
            if name == 'rename' and self.where[-1] != 'rename_new':
                violations.append((name, node.lineno))
            self.generic_visit(node)
    Calls().visit(source)
    assert violations == []
    absolute = {node.value for node in ast.walk(source) if isinstance(node, ast.Constant)
                and isinstance(node.value, str) and node.value.startswith('/usr/')}
    assert absolute == {'/usr/bin/ditto', '/usr/bin/codesign', '/usr/bin/sandbox-exec', '/usr/lib/libproc.dylib'}

    calls = []

    def guard(owner, name):
        original = getattr(owner, name)

        def wrapper(*args, **kwargs):
            if sys._getframe(1).f_code.co_filename == str(SCRIPT):
                calls.append(name)
            return original(*args, **kwargs)
        monkeypatch.setattr(owner, name, wrapper)
    for owner, names in ((os, ('remove', 'unlink', 'rmdir', 'removedirs', 'replace', 'truncate', 'kill', 'killpg')),
                         (shutil, ('rmtree', 'move')), (Path, ('unlink', 'rmdir', 'replace'))):
        for name in names:
            guard(owner, name)
    # The recorder sees a call made from the tool's own file (compiled under its name).
    probe = put(stage.tmp / 'probe.txt', b'probe')
    exec(compile('import os\nos.unlink(path)\n', str(SCRIPT), 'exec'), {'path': str(probe)})
    assert calls == ['unlink'] and not probe.exists()
    calls.clear()
    fakes = Fakes()
    code, report = drill(fakes, stage, stage.tmp / 'drill')
    assert code == 0, report['error']
    backup(fakes, stage)
    fakes.fail = lambda source, destination: source == stage.candidate
    assert install(fakes, stage)[0] == 4
    assert call(fakes, 'rollback', '--root', stage.root, '--stamp', STAMP, '--execute')[0] == 0
    assert calls == []


def test_real_paths_need_the_parent_approval_naming_the_stamp(stage, real_home):
    real_home.real_layout_allowed = True  # these runs name the real layout, and are refused before using it

    def untouchable(fakes):
        pytest.fail('no real path may be observed before the approval gate')
    fakes = Fakes()
    fakes.rows = untouchable
    for extra in ([], ['--i-am-the-parent-with-user-approval', '20261013T120001Z']):
        code, report = call(fakes, 'backup', '--stamp', STAMP, '--expect-backend-sha256', OLD_SHA, '--execute', *extra)
        assert code == 3 and report['error']['code'] == 'parent_approval' and report['layout']['root'] is None
        assert report['layout']['app'] == '/Applications/Pebble.app'
    code, report = call(fakes, 'backup', '--expect-backend-sha256', OLD_SHA, '--execute',
                        '--i-am-the-parent-with-user-approval', STAMP)
    assert code == 3 and report['error']['code'] == 'stamp_given'
    code, report = call(fakes, 'rollback', '--stamp', STAMP, '--execute')
    assert code == 3 and report['error']['code'] == 'parent_approval'
    assert fakes.copies == [] and fakes.lists == 0 and 'record' not in report
    assert str(SCRIPT) in real_home.calls  # the recorder that the staged tests rely on does see the tool's calls


def test_live_markers_block_execution_until_quiet(stage):
    fakes = Fakes()
    marker = put(stage.tmp / 'trial.live', b'running')
    os.utime(marker, (fakes.now, fakes.now))
    code, report = backup(fakes, stage, '--quiet-file', marker, '--quiet-file', stage.tmp / 'absent.marker')
    assert code == 3 and report['error']['code'] == 'quiet_markers' and not stage.backup.exists()
    states = {item['path']: item['quiet'] for item in report['error']['markers']}
    assert states == {str(marker): False, str(stage.tmp / 'absent.marker'): True}
    os.utime(marker, (fakes.now - 3600, fakes.now - 3600))
    assert backup(fakes, stage, '--quiet-file', marker)[0] == 0


def test_app_manifest_shapes_and_an_inconsistent_digest(stage):
    single = app_manifest(stage.candidate, stage.tmp / 'single.json', shape='single')
    one, many = tool.load_app_manifest(single, 'release'), tool.load_app_manifest(stage.manifest, 'release')
    assert one['bundle_sha256'] == many['bundle_sha256'] and one['links'] == many['links'] and many['key'] == 'release'
    doc = json.loads(single.read_text())
    bad = put(stage.tmp / 'bad.json', json.dumps({**doc, 'bundle_sha256': '0' * 64}).encode())
    for path, key, code in ((bad, 'release', 'app_manifest_inconsistent'), (stage.manifest, 'test', 'app_manifest_shape')):
        with pytest.raises(tool.Stop) as caught:
            tool.load_app_manifest(path, key)
        assert caught.value.code == code


FAKE_BACKEND = '''
import json, os, sys, time
home = sys.argv[sys.argv.index('--home') + 1]
protected = os.environ['TMPDIR'] + '/../../protected'
try:
    open(protected + '/written', 'w').close()
    probe = 'wrote'
except PermissionError:
    probe = 'denied'
with open(home + '/probe', 'w') as stream:
    stream.write(probe)
if os.path.exists(home + '/hang'):
    time.sleep(60)
print(json.dumps({'event': 'ready', 'data': {}}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] == 'state':
        print(json.dumps({'id': request['id'], 'result': {'chats': [{'title': 'private words'}], 'model': {}}}), flush=True)
    else:
        print(json.dumps({'id': request['id'], 'error': 'Unknown request.'}), flush=True)
'''


@pytest.mark.skipif(not os.path.exists('/usr/bin/sandbox-exec'), reason='needs sandbox-exec')
@pytest.mark.parametrize('hang', [False, True])
def test_the_sandboxed_launcher_speaks_the_backend_protocol_and_stays_bounded(tmp_path, real_home, hang):
    real_home.real_layout_allowed = True  # it adds the (sentinel) home's Library to the protected paths
    work, protected, home = tmp_path / 'work', tmp_path / 'protected', tmp_path / 'home'
    for path in (work, work / 'tmp', protected, home):
        path.mkdir()
    if hang:
        put(home / 'hang', b'')
    script = put(tmp_path / 'fake_backend.py', FAKE_BACKEND.encode())
    quote = lambda text: "'" + text.replace("'", "'\\''") + "'"  # noqa: E731
    backend = put(tmp_path / 'pebble-backend',
                  f'#!/bin/sh\nexec {quote(sys.executable)} -I {quote(str(script))} "$@"\n'.encode(), 0o755)
    spec = {'backend': str(backend), 'resources': str(tmp_path), 'home': str(home),
            'requests': [dict(request) for request in tool.REQUESTS], 'timeout': 4 if hang else 60,
            'workdir': str(work), 'tmpdir': str(work / 'tmp'), 'protected': [str(protected)], 'keychain': False}
    result = tool.sandboxed_launcher(spec)
    stderr = (work / 'backend.stderr.log').read_bytes()
    if result['exit'] and b'sandbox_apply' in stderr:
        pytest.skip('this host does not permit a nested sandbox')
    assert (home / 'probe').read_text() == 'denied' and not (protected / 'written').exists()
    assert str(os.path.realpath(protected)) in (work / 'sandbox.sb').read_text()
    summary = tool.summarize_launch(result)
    if hang:
        assert result['timed_out'] is True and summary['responses']['state'] == {'answered': False}
        return
    assert result['exit'] == 0 and result['timed_out'] is False and result['events'] == {'ready': 1}, stderr
    assert summary['responses'] == {'state': {'answered': True, 'ok': True, 'keys': ['chats', 'model'],
                                              'maintenance': False},
                                    'upgrade_backups': {'answered': True, 'ok': False, 'error': 'Unknown request.'}}
    assert 'private words' not in json.dumps(summary)


@pytest.mark.skipif(sys.platform != 'darwin', reason='libproc, ditto and codesign are macOS tools')
def test_the_real_lister_copier_and_codesign_on_owned_paths(stage):
    rows = tool.libproc_lister()  # read-only observation
    mine = next(exe for pid, exe, args in rows if pid == os.getpid())
    assert mine and os.path.isabs(mine) and tool.inside(stage.app, rows) == []
    # argv is read only for Pebble's names; the parser itself is checked on this process's own argv.
    assert tool._procargs(tool._libc(), os.getpid(), 1 << 20) == sys.orig_argv
    assert tool.real_free_bytes(stage.tmp) > 0
    for source, destination in ((stage.app, stage.tmp / 'ditto/Pebble.app'),
                                (stage.home, stage.tmp / 'ditto/com.pebble.local')):
        destination.parent.mkdir(exist_ok=True)
        tool.real_copier(source, destination)
        assert tool.same_tree(tool.tree_manifest(source), tool.tree_manifest(destination))
    assert tool.same_bundle(tool.bundle_manifest(stage.app), tool.bundle_manifest(stage.tmp / 'ditto/Pebble.app'))
    with pytest.raises(tool.CopyFailed):
        tool.real_copier(stage.tmp / 'missing', stage.tmp / 'ditto/missing')
    assert tool.real_codesign(stage.app)['exit'] not in (0, None)  # verify only: the fake bundle is unsigned


def test_rehearsal_profile_quotes_paths_and_confines_writes_and_network(tmp_path):
    protected = tmp_path / 'protected "dir" \\ é'
    protected.mkdir()
    text = tool.sandbox_profile([protected])
    assert '(deny network*)' in text and '(allow network* (remote ip "localhost:*"))' in text
    assert '(deny mach-lookup (global-name "com.apple.SecurityServer"))' in text
    real = os.path.realpath(protected)
    assert '(subpath "' + real.replace('\\', '\\\\').replace('"', '\\"') + '")' in text
    assert '\\u' not in text  # Seatbelt reads no JSON escapes (the 0.4.2 root cause)
    assert 'mach-lookup' not in tool.sandbox_profile([protected], keychain=True)
    with pytest.raises(ValueError):
        tool.sandbox_profile([tmp_path / 'bad\nname'])
    if not os.path.exists('/usr/bin/sandbox-exec'):
        return
    free = tmp_path / 'free'
    free.mkdir()
    profile = tmp_path / 'profile.sb'
    profile.write_text(text)
    probe = textwrap.dedent('''
        import socket, sys
        protected, free = sys.argv[1], sys.argv[2]
        with open(free + '/ok', 'w') as stream:
            stream.write('ok')
        try:
            open(protected + '/no', 'w').close()
            print('WROTE')
        except PermissionError:
            print('DENIED')
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=5)
        print('LOOPBACK')
        client.close()
        server.close()
    ''')
    result = subprocess.run(['/usr/bin/sandbox-exec', '-f', str(profile), sys.executable, '-I', '-S', '-c', probe,
                             str(protected), str(free)], capture_output=True, timeout=60)
    if result.returncode and b'sandbox_apply' in result.stderr:
        pytest.skip('this host does not permit a nested sandbox')
    assert result.stdout.split() == [b'DENIED', b'LOOPBACK'], result.stderr
    assert (free / 'ok').read_text() == 'ok' and not (protected / 'no').exists()


def test_readback_counts_crashes_but_not_handled_request_failures(tmp_path):
    """Phase 21: backend/main.py logs each handled request failure ('ERROR:root:Request failed: <method>') with its
    traceback; readback must not treat those answered errors as crashes, but must still count any other traceback."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('pebble_install_handled', Path(__file__).resolve().parents[2] / 'scripts' / 'pebble_install.py')
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    home = tmp_path / 'home'
    home.mkdir()
    handled = ('ERROR:root:Request failed: upgrade_backups\nTraceback (most recent call last):\n'
               '  File "main.py", line 159, in request\nValueError: Unknown request.\n')
    crash = 'Traceback (most recent call last):\n  File "main.py", line 1, in <module>\nRuntimeError: boom\n'
    for name in tool.LOGS:
        (home / name).write_text('')
    offsets = {name: {'bytes': 0} for name in tool.LOGS}
    first = tool.LOGS[0]
    (home / first).write_text(handled * 2)
    scan = tool.new_tracebacks(home, offsets)
    assert scan[first]['tracebacks'] == 0 and scan[first]['handled_request_failures'] == 2
    (home / first).write_text(handled + crash)
    scan = tool.new_tracebacks(home, offsets)
    assert scan[first]['tracebacks'] == 1 and scan[first]['handled_request_failures'] == 1


# ---- Phase 22 (s22-storage): ~/.pebble ---------------------------------------------------------------
# These add the pebble-home data layout; every test above keeps the default legacy layout unchanged.

MOVING = ('--data-layout', 'pebble-home')


def staged_pebble(stage):
    return stage.root / 'home' / '.pebble'


def first_launch_moving(fakes, stage):
    """The Phase 22 shell's first open: --home ~/.pebble --legacy-home <the old data home>."""
    fakes.launcher({'home': str(staged_pebble(stage)), 'legacy_home': str(stage.home),
                    'resources': str(stage.app / tool.RESOURCES), 'requests': [dict(r) for r in tool.REQUESTS]})


def test_a_pebble_home_rehearsal_moves_a_temporary_copy_and_checks_both_homes(stage):
    fakes = Fakes()
    assert backup(fakes, stage)[0] == 0
    doc = json.loads((stage.backup / 'backup.json').read_text())
    assert doc['pebble_home'] is None and doc['live_home'] == 'legacy'
    home_before, copy_before = snapshot(stage.home), snapshot(stage.backup / 'com.pebble.local')
    work = stage.tmp / 'rehearsal'
    code, report = rehearse(fakes, stage, work, *MOVING)
    assert code == 0 and report['state'] == 'done', report['error']
    [spec] = fakes.launches
    assert spec['home'] == str(work / 'pebble') and spec['legacy_home'] == str(work / 'home')
    assert {str(staged_pebble(stage)), str(stage.root / 'home/Documents')} <= set(spec['protected'])
    results = report['results']
    assert results['data_layout'] == 'pebble-home' and results['move']['state'] == 'migrated'
    assert results['inspect']['state'] == 'current' and results['inspect']['database']['user_version'] == 9
    # The fixture is epoch 0, so the upgrade in ~/.pebble/data made its own backup, and it restores.
    assert [item['valid'] for item in results['new_upgrade_backups']] == [True]
    assert results['restore_copy']['restored'] is True and results['passed'] is True
    assert (work / 'home' / 'MOVED.md').exists() and (work / 'pebble' / 'data' / 'pebble.sqlite').exists()
    checks = {c['check']: c['ok'] for c in report['checks']}
    assert checks['old_folder_intact'] and checks['home_moved'] and checks['old_folder_pointer']
    # The real (staged) homes were never written.
    assert snapshot(stage.home) == home_before and snapshot(stage.backup / 'com.pebble.local') == copy_before
    assert not staged_pebble(stage).exists() and not (stage.root / 'home/Documents').exists()


def test_a_same_epoch_rehearsal_needs_no_new_upgrade_backup(stage):
    fakes = Fakes()
    first_launch(fakes, stage)  # the installed (legacy) app already upgraded its home to this epoch
    assert backup(fakes, stage)[0] == 0
    code, report = rehearse(fakes, stage, stage.tmp / 'rehearsal', *MOVING)
    assert code == 0, report['error']
    assert report['results']['new_upgrade_backups'] == 'not_needed_same_epoch'
    assert report['results']['restore_copy'] == 'not_needed_same_epoch' and report['results']['passed'] is True
    # The legacy layout's rule is unchanged: there, a same-epoch rehearsal still requires a new upgrade backup.
    fakes.launches.clear()
    code, report = rehearse(fakes, stage, stage.tmp / 'legacy-rehearsal')
    assert code == 4 and report['error']['code'] == 'new_upgrade_backup'


def test_install_then_a_pebble_home_readback_checks_both_homes(stage):
    fakes = Fakes()
    assert backup(fakes, stage)[0] == 0
    assert install(fakes, stage)[0] == 0
    first_launch_moving(fakes, stage)
    shell, backend = str(stage.app / tool.SHELL), str(stage.app / tool.BACKEND)
    fakes.rows = [(10, shell), (11, backend)]
    code, report = readback(fakes, stage, *RECOVERY, *MOVING)
    assert code == 0 and report['results']['passed'] is True, report['error']
    results = report['results']
    assert results['move']['state'] == 'migrated' and results['inspect']['state'] == 'current'
    assert [item['valid'] for item in results['new_upgrade_backups']] == [True]
    assert results['logs']['service.log']['tracebacks'] == 0 and results['logs']['pebble:service.log']['scanned']
    # A new traceback in ~/.pebble/logs fails the readback, as in the old home.
    put(staged_pebble(stage) / 'logs' / 'backend.log', b'Traceback (most recent call last):\n  boom\n', 0o600)
    code, report = readback(fakes, stage, *RECOVERY, *MOVING)
    assert report['error']['code'] == 'no_new_tracebacks'
    (staged_pebble(stage) / 'logs' / 'backend.log').write_bytes(b'')
    # The old home must stay as it was, apart from MOVED.md and the pointer.
    put(stage.home / 'surprise.txt', b'written after the move')
    code, report = readback(fakes, stage, *RECOVERY, *MOVING)
    assert report['error']['code'] == 'old_folder_intact' and report['error']['diff']['extra'] == ['surprise.txt']
    (stage.home / 'surprise.txt').unlink()  # test fixture cleanup, not the tool
    # Without the move, the readback says so.
    (staged_pebble(stage) / 'runtime' / 'migration.json').write_text(json.dumps({'state': 'refused', 'reason': 'x'}))
    code, report = readback(fakes, stage, *RECOVERY, *MOVING)
    assert report['error']['code'] == 'home_moved'


def test_rollback_restores_the_old_app_and_home_and_leaves_dot_pebble_untouched(stage):
    fakes = Fakes()
    assert backup(fakes, stage)[0] == 0
    original = tree(stage.home)
    assert install(fakes, stage)[0] == 0
    first_launch_moving(fakes, stage)
    pebble_before = snapshot(staged_pebble(stage))
    code, report = call(fakes, 'rollback', '--root', stage.root, '--stamp', STAMP, '--execute')
    assert code == 0 and report['results']['passed'] is True, report['error']
    assert report['results']['pebble_home']['state'] == 'left untouched'
    assert tree(stage.home) == original and not (stage.home / 'MOVED.md').exists()
    assert snapshot(staged_pebble(stage)) == pebble_before


def test_a_backup_copies_dot_pebble_and_follows_the_live_home(stage):
    fakes = Fakes()
    first_launch_moving(fakes, stage)  # a Phase 22 Pebble already moved the data home
    assert (stage.home / 'MOVED.md').exists()
    code, report = backup(fakes, stage)
    assert code == 0, report['error']
    doc = json.loads((stage.backup / 'backup.json').read_text())
    assert doc['live_home'] == 'pebble_home' and doc['pebble_home']['copy'] == 'pebble-home'
    assert doc['database']['path'] == 'data/pebble.sqlite' and doc['database']['user_version'] == 9
    assert tree(stage.backup / 'pebble-home') == tree(staged_pebble(stage))
    assert doc['data_home'] is not None, 'the old folder is still copied while it exists'
    # V1-006 (deliberate, Phase 24): such a backup now rolls back in one command. Until 0.6.1 this was refused with
    # rollback_supported; the post-move rollback restores ~/.pebble itself (test_install_postmove_24.py covers it).
    assert install(fakes, stage)[0] == 0
    code, report = call(fakes, 'rollback', '--root', stage.root, '--stamp', STAMP, '--execute')
    assert code == 0 and report['results']['passed'] is True, report['error']
    assert report['results']['data_layout'] == 'pebble-home' and report['results']['old_folder']['state'] == 'left untouched'
    expected = tree(stage.backup / 'pebble-home')
    index = expected.pop('runtime/mirror.json')  # the restored Documents copy index is moved aside into the backup
    assert tree(staged_pebble(stage)) == expected
    assert hashlib.sha256(Path(report['results']['mirror_index']).read_bytes()).hexdigest() == index[3]


def test_install_refuses_a_stray_database_in_dot_pebble(stage):
    fakes = Fakes()
    assert backup(fakes, stage)[0] == 0
    App(staged_pebble(stage) / 'data', stage.tmp / 'resources', lambda *event: None).close()
    code, report = install(fakes, stage)
    assert code == 3 and report['error']['code'] == 'pebble_home_free_for_the_move'
    assert tree(stage.app) != tree(stage.candidate), 'nothing was moved'


def test_a_pebble_home_drill_moves_on_first_launch_and_rolls_back(stage):
    fakes = Fakes()
    root = stage.tmp / 'drill'
    code, report = drill(fakes, stage, root, *MOVING)
    assert code == 0 and report['results']['passed'] is True, report['error']
    [spec] = fakes.launches
    staged = root / 'home/Library/Application Support/com.pebble.local'
    assert spec['home'] == str(root / 'home/.pebble') and spec['legacy_home'] == str(staged)
    assert tree(staged) == tree(stage.home), 'the old home is restored byte for byte'
    assert user_version(root / 'home/.pebble/data') == migrations.SCHEMA_EPOCH
    checks = {c['check']: c['ok'] for c in report['checks']}
    assert checks['drill_pebble_home_kept'] and checks['drill_data_restored']
    assert report['results']['steps']['readback']['ok'] is True


def test_the_launcher_passes_the_old_home_when_given(tmp_path, real_home, monkeypatch):
    real_home.real_layout_allowed = True
    seen, real = [], subprocess.Popen

    def fake(argv, **kwargs):
        seen.append(argv)
        return real([sys.executable, '-I', '-c', 'import sys; sys.stdin.read()'], **kwargs)
    monkeypatch.setattr(tool, 'trusted_tool', lambda path: True)
    monkeypatch.setattr(subprocess, 'Popen', fake)
    for name, legacy in (('a', None), ('b', str(tmp_path / 'old'))):
        work = tmp_path / name
        (work / 'tmp').mkdir(parents=True)
        spec = {'backend': '/bin/false', 'resources': str(tmp_path), 'home': str(tmp_path / 'home'), 'requests': [],
                'timeout': 5, 'workdir': str(work), 'tmpdir': str(work / 'tmp'), 'protected': [], 'keychain': False}
        if legacy:
            spec['legacy_home'] = legacy
        tool.sandboxed_launcher(spec)
    assert seen[0][3:] == ['/bin/false', '--home', str(tmp_path / 'home'), '--resources', str(tmp_path)]
    assert seen[1][3:] == ['/bin/false', '--home', str(tmp_path / 'home'), '--legacy-home', str(tmp_path / 'old'),
                           '--resources', str(tmp_path)]
