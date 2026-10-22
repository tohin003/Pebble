"""Actual Phase 12 epoch-3 fixture -> current (epoch 4) service upgrade and downgrade refusal.

Uses the parent-owned synthetic epoch-3 fixture (.runtime/phase13-epoch3-fixture,
built by .runtime/phase13-baseline/build_epoch3_fixture.py with the preserved
Phase 12 candidate) and that preserved Phase 12 executable. Never touches an
installed home, account, credential or model. Each mode upgrades its own private
copy under the system temporary folder, never under .runtime. The fixture's
manual synthetic rows (six memory layers, a settled run, process jobs, output
and inspection, a committed stream attempt) are preservation fixtures, not
learned or executed evidence.

Checks are recorded independently by name; a failure never stops later
independent checks and a report is written either way. --report must be a new
file. The Phase 12 executable runs only when its SHA-256 matches the preserved
candidate, and only against the private copy.

Usage:
  python scripts/validate_phase13_upgrade.py --report PATH [--backend PATH]
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

from validate_runtime import ROOT, Service
sys.path.insert(0, str(ROOT / 'backend'))
from pebble.migrations import BACKUPS, SCHEMA_EPOCH


OLD = ROOT / '.runtime/phase12-candidate/pebble-backend/pebble-backend'
FIXTURE = ROOT / '.runtime/phase13-epoch3-fixture'
METADATA = ROOT / '.runtime/phase13-epoch3-fixture.json'
BUILDER = ROOT / '.runtime/phase13-baseline/build_epoch3_fixture.py'
EXPECTED_OLD = '2268defc69577654b256ecf6d7e3a2c589963023eb45f5c819395e6b09c97373'
SIDECARS = frozenset({'pebble.sqlite-shm', 'pebble.sqlite-wal'})
RUN = 'f13e' + '0' * 28
LAYERS = frozenset({'working', 'episodic', 'semantic', 'procedural', 'temporal', 'profile'})
CORE = {'memories': 'SELECT * FROM memories ORDER BY id', 'checkpoints': 'SELECT * FROM checkpoints ORDER BY chat_id',
        'messages': 'SELECT * FROM messages ORDER BY id', 'chats': 'SELECT * FROM chats ORDER BY id',
        'permissions': "SELECT * FROM settings WHERE key='permissions'"}
PHASE12 = {'run': f"SELECT * FROM runs WHERE id='{RUN}'", 'process_jobs': 'SELECT * FROM process_jobs ORDER BY id',
           'process_output': 'SELECT * FROM process_output ORDER BY process_id,sequence',
           'process_inspections': 'SELECT * FROM process_inspections ORDER BY process_id',
           'run_stream_attempts': 'SELECT * FROM run_stream_attempts ORDER BY id',
           'run_stream_events': 'SELECT * FROM run_stream_events ORDER BY run_id,sequence'}
# Fixture metadata row counts -> the rows they describe (the epoch-2 source had none of the Phase 12 rows).
COUNTED = {'runs': 'run', 'process_jobs': 'process_jobs', 'process_output': 'process_output',
           'process_inspections': 'process_inspections', 'run_stream_attempts': 'run_stream_attempts',
           'run_stream_events': 'run_stream_events'}
NEW_TABLES = ('agent_worktrees', 'agent_worktree_writes', 'git_plan_receipts', 'project_environments')
HISTORY = 'SELECT backup_id,from_version,to_version,completed FROM upgrade_history ORDER BY id'
REFUSAL = 'Pebble stopped this upgrade: The database was written by a newer Pebble.'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounded(exc, limit=1500):
    return (type(exc).__name__ + ': ' + str(exc))[:limit]


def read(home, statement, immutable=False):
    uri = (home / 'pebble.sqlite').as_uri() + '?mode=ro' + ('&immutable=1' if immutable else '')
    with sqlite3.connect(uri, uri=True) as db:
        return db.execute(statement).fetchall()


def tables(home, immutable=False):
    return {name for (name,) in read(home, "SELECT name FROM sqlite_master WHERE type='table'", immutable)}


def snapshot(home, immutable=False):
    return {key: read(home, statement, immutable) for key, statement in {**CORE, **PHASE12}.items()}


def backups(home):
    folder = home / BACKUPS
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


def wal(home):
    """Committed-but-uncheckpointed bytes; an absent and an empty WAL are the same state."""
    path = home / 'pebble.sqlite-wal'
    return sha(path) if path.is_file() and path.stat().st_size else None


class Upgrade:
    def __init__(self, command):
        self.command = command
        self.checks, self.names, self.facts = [], set(), {}

    def check(self, name, fn):
        assert name not in self.names, 'Check names must be unique: ' + name
        self.names.add(name)
        try:
            evidence = fn() or {}
            self.checks.append({'name': name, 'passed': True, 'evidence': evidence})
            return True
        except Exception as exc:
            self.checks.append({'name': name, 'passed': False, 'error': bounded(exc, 2000),
                                'trace': traceback.format_exc(limit=4)[-1500:]})
            return False

    def need(self, key):
        if key not in self.facts:
            raise AssertionError('Prerequisite was not established: ' + key)
        return self.facts[key]

    def run(self, home):
        self.check('fixture metadata, file hashes and preserved Phase 12 executable verified', self.verify_fixture)
        shutil.copytree(FIXTURE, home)
        self.facts['home'] = home
        self.check('private copy holds the six memory layers and synthetic Phase 12 rows at epoch 3', self.copy_holds_rows)
        self.check('actual service upgrades the private epoch-3 copy to epoch 4 once', self.upgrade)
        self.check('six memory layers, checkpoint, chats, messages and permissions survive upgrade exactly', self.core_kept)
        self.check('synthetic run, process jobs, output, inspection and stream attempt/events survive upgrade exactly',
                   self.phase12_kept)
        self.check('new Phase 13 tables exist and are empty after upgrade', self.new_tables)
        self.check('verified pre-upgrade backup is epoch 3 and retains the exact preserved rows', self.backup)
        self.check('second current-service startup creates no new upgrade or backup', self.second_start)
        self.check('preserved Phase 12 executable refuses epoch 4 before mutating the database', self.refusal)

    def verify_fixture(self):
        metadata = json.loads(METADATA.read_text())
        assert (metadata['synthetic_only'] is True and metadata['credentials_used'] is False
                and metadata['inference_calls'] == 0), 'The fixture is not marked synthetic and credential-free.'
        assert metadata['epoch'] == 3 and SCHEMA_EPOCH == 4, (metadata['epoch'], SCHEMA_EPOCH)
        assert metadata['old_executable_sha256'] == EXPECTED_OLD and sha(OLD) == EXPECTED_OLD, \
            'The preserved Phase 12 executable does not match its recorded SHA-256.'
        self.facts['old_verified'] = True
        assert sha(BUILDER) == metadata['builder_sha256'], 'The fixture builder changed after the fixture was made.'
        assert FIXTURE.is_dir() and not FIXTURE.is_symlink()
        assert not any(path.is_symlink() for path in FIXTURE.rglob('*'))
        # Closed SQLite sidecars can disappear after a read-only connection; the
        # builder removed empty ones. Every other file must match the record.
        recorded = {name: digest for name, digest in metadata['files_sha256'].items() if name not in SIDECARS}
        actual = {path.relative_to(FIXTURE).as_posix(): sha(path) for path in FIXTURE.rglob('*')
                  if path.is_file() and path.name not in SIDECARS}
        assert actual == recorded, sorted(set(actual) ^ set(recorded)) or 'A fixture file digest differs.'
        assert wal(FIXTURE) is None, 'The fixture has uncheckpointed WAL frames.'
        assert read(FIXTURE, 'PRAGMA user_version', immutable=True) == [(3,)]
        self.facts['metadata'] = metadata
        return {'files': len(recorded), 'epoch': 3, 'legacy_backend_sha256': EXPECTED_OLD}

    def copy_holds_rows(self):
        home, metadata = self.need('home'), self.need('metadata')
        assert wal(home) is None and read(home, 'PRAGMA user_version', immutable=True) == [(3,)]
        before = self.facts['before'] = snapshot(home, immutable=True)
        self.facts['history_before'] = read(home, HISTORY, immutable=True)
        assert {row[1] for row in before['memories']} >= LAYERS, sorted({row[1] for row in before['memories']})
        counts = {key: len(before[rows]) for key, rows in COUNTED.items()}
        assert counts == metadata['synthetic_rows'], (counts, metadata['synthetic_rows'])
        # The fixture has chats and explicit permissions but no message rows; messages are still compared exactly.
        assert before['permissions'] and before['chats'], 'The fixture lacks conversation or permission rows.'
        assert not set(NEW_TABLES) & tables(home, immutable=True), 'An epoch-3 database already has Phase 13 tables.'
        return {'memory_layers': sorted(LAYERS), 'synthetic_rows': counts}

    def upgrade(self):
        home = self.need('home')
        self.need('before')
        service = Service(self.command, home)
        try:
            assert service.rpc('state')['active'] is None
        finally:
            service.close()
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        history = read(home, HISTORY)
        added = [row for row in history if row not in self.need('history_before')]
        assert [(row[1], row[2]) for row in added] == [(3, SCHEMA_EPOCH)], [(row[1], row[2]) for row in added]
        self.facts['backup_id'] = added[0][0]
        self.facts['history'], self.facts['backups'] = history, backups(home)
        return {'from_version': 3, 'to_version': SCHEMA_EPOCH}

    def core_kept(self):
        home, before = self.need('home'), self.need('before')
        self.need('backup_id')
        for key, statement in CORE.items():
            assert read(home, statement) == before[key], key
        return {'tables': sorted(CORE)}

    def phase12_kept(self):
        home, before = self.need('home'), self.need('before')
        self.need('backup_id')
        for key, statement in PHASE12.items():
            assert read(home, statement) == before[key], key
        return {'tables': sorted(PHASE12), 'run_state': before['run'][0][3] if before['run'] else None}

    def new_tables(self):
        home = self.need('home')
        self.need('backup_id')
        present = tables(home)
        assert set(NEW_TABLES) <= present, sorted(set(NEW_TABLES) - present)
        counts = {name: read(home, f'SELECT count(*) FROM {name}')[0][0] for name in NEW_TABLES}
        assert not any(counts.values()), counts
        return {'empty': sorted(NEW_TABLES)}

    def backup(self):
        home, before, ident = self.need('home'), self.need('before'), self.need('backup_id')
        folder = home / BACKUPS
        files = {suffix: folder / (ident + suffix) for suffix in ('.sqlite', '.manifest.json', '.complete.json')}
        for path in files.values():
            assert path.is_file() and not path.is_symlink(), path.name
        with sqlite3.connect(files['.sqlite'].as_uri() + '?mode=ro&immutable=1', uri=True) as db:
            assert db.execute('PRAGMA user_version').fetchall() == [(3,)]
            for key, statement in {**CORE, **PHASE12}.items():
                assert db.execute(statement).fetchall() == before[key], key
        return {'backup_epoch': 3, 'tables_compared': len(CORE) + len(PHASE12)}

    def second_start(self):
        home, history, listed = self.need('home'), self.need('history'), self.need('backups')
        service = Service(self.command, home)
        try:
            assert service.rpc('state')['active'] is None
        finally:
            service.close()
        assert read(home, HISTORY) == history, 'A second startup recorded another upgrade.'
        assert backups(home) == listed, 'A second startup wrote another backup.'
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        return {'upgrades': len(history), 'backup_files': len(listed)}

    def refusal(self):
        home = self.need('home')
        self.need('old_verified')  # never execute an unverified binary
        self.need('backup_id')
        before = (sha(home / 'pebble.sqlite'), wal(home), backups(home), read(home, HISTORY))
        refused = subprocess.run([str(OLD), '--home', str(home), '--resources', str(ROOT / 'src-tauri/resources')],
                                 input='', capture_output=True, text=True, timeout=60)
        assert refused.returncode != 0, 'The Phase 12 executable accepted an epoch-4 database.'
        assert REFUSAL in refused.stderr, refused.stderr[-600:]
        after = (sha(home / 'pebble.sqlite'), wal(home), backups(home), read(home, HISTORY))
        assert after == before, 'The Phase 12 executable changed the database, its WAL, backups or upgrade history.'
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        return {'exit_code': refused.returncode, 'database_unchanged': True}


def owned_backend(parser, value):
    given = Path(value)
    try:
        info = os.lstat(given)
    except OSError as exc:
        parser.error('--backend is not readable: ' + bounded(exc))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        parser.error('--backend must be a regular file, not a symbolic link.')
    if hasattr(os, 'getuid') and info.st_uid != os.getuid():
        parser.error('--backend must be owned by the current user.')
    if not os.access(given, os.X_OK):
        parser.error('--backend must be executable.')
    return given.resolve()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    args = parser.parse_args()
    if os.path.lexists(args.report):
        parser.error('--report already exists; preserve existing upgrade evidence and select a new path.')
    executable = owned_backend(parser, args.backend) if args.backend else ROOT / 'backend/main.py'
    command = [str(executable)] if args.backend else [sys.executable, str(executable)]
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase13-upgrade-')).resolve()
    upgrade, fatal = Upgrade(command), None
    try:
        upgrade.run(scratch / 'home')
    except Exception as exc:
        fatal = bounded(exc, 3000)
    passed = fatal is None and bool(upgrade.checks) and all(c['passed'] for c in upgrade.checks)
    if passed:
        shutil.rmtree(scratch, ignore_errors=True)
    result = {'passed': passed, 'kind': 'frozen' if args.backend else 'source', 'date': datetime.now(timezone.utc).isoformat(),
              'backend_sha256': sha(executable), 'legacy_backend_sha256': sha(OLD) if OLD.is_file() else None,
              'checks': [c['name'] for c in upgrade.checks if c['passed']],
              'failed': [c['name'] for c in upgrade.checks if not c['passed']],
              'check_results': upgrade.checks, 'fatal_error': fatal,
              'scratch_preserved': None if passed else str(scratch),
              'fixture_metadata_sha256': sha(METADATA),
              'source_sha256': {p: sha(ROOT / p) for p in ('scripts/validate_phase13_upgrade.py', 'scripts/validate_runtime.py',
                                                           'backend/pebble/migrations.py', 'backend/pebble/child_worktrees.py',
                                                           'backend/pebble/git_plans.py', 'backend/pebble/environments.py')},
              'synthetic_only': True, 'inference_calls': 0, 'credentials_used': False}
    descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'kind': result['kind'], 'checks': len(result['checks']), 'failed': result['failed'],
                      'fatal_error': fatal, 'passed': passed}))
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()


# Development self-checks, run only by pytest on this file; the validator never
# calls them. They read the parent-owned fixture and work on a copy under
# pytest's tmp_path. Neither the service nor the Phase 12 executable is started.

def test_parent_fixture_and_preserved_executable_verify():
    upgrade = Upgrade(['unused'])
    assert upgrade.check('fixture', upgrade.verify_fixture), upgrade.checks[-1].get('error')
    assert upgrade.facts['old_verified'] is True and upgrade.facts['metadata']['epoch'] == 3


def test_private_copy_holds_the_preserved_rows(tmp_path):
    upgrade = Upgrade(['unused'])
    assert upgrade.check('fixture', upgrade.verify_fixture), upgrade.checks[-1].get('error')
    home = tmp_path / 'home'
    shutil.copytree(FIXTURE, home)
    upgrade.facts['home'] = home
    assert upgrade.check('copy', upgrade.copy_holds_rows), upgrade.checks[-1].get('error')
    before = upgrade.facts['before']
    assert before['run'] and before['run'][0][0] == RUN and len(before['process_jobs']) == 2
    assert {row[1] for row in before['memories']} >= LAYERS and upgrade.facts['history_before']
    assert sha(home / 'pebble.sqlite') == upgrade.facts['metadata']['files_sha256']['pebble.sqlite'], 'A read changed the copy.'


def test_dependent_checks_never_run_without_their_prerequisites():
    upgrade = Upgrade(['unused'])
    for name, fn in (('refusal', upgrade.refusal), ('backup', upgrade.backup), ('second', upgrade.second_start),
                     ('core', upgrade.core_kept), ('upgrade', upgrade.upgrade)):
        assert upgrade.check(name, fn) is False and 'Prerequisite' in upgrade.checks[-1]['error'], name
    try:
        upgrade.check('refusal', upgrade.refusal)
    except AssertionError:
        pass
    else:
        raise AssertionError('A duplicate check name was accepted.')
