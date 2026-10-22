"""Actual Phase 13 epoch-4 fixture -> current (epoch 5) service upgrade and downgrade refusal.

Uses the parent-owned synthetic epoch-4 fixture (.runtime/phase14-epoch4-fixture,
built by .runtime/phase14-baseline/build_epoch4_fixture.py with the preserved
Phase 13 candidate) and that preserved Phase 13 executable. Never touches an
installed home, account, credential or model. Each mode upgrades its own private
copy under the system temporary folder, never under .runtime. The fixture's
manual synthetic rows (six memory layers, a settled run, Phase 12 process jobs,
output, inspection and stream rows, and settled Phase 13 worktree, write, Git
plan receipt and environment rows) are preservation fixtures, not learned or
executed evidence.

The fixture already retains three upgrade backups (epochs 1->2, 2->3, 3->4),
which is migrations.Limits.max_backups including the new one. The service must
therefore wait in upgrade maintenance (pebble.maintenance) and change nothing,
serving only its maintenance routes. The validator then removes the oldest backup
through the native upgrade_discard_backup route, as a person would in the app,
confirmed by the manifest hash the service lists (cross-checked with the
fixture's recorded hash), and retries startup in place with upgrade_retry.
Nothing is deleted automatically.

Time-aware preservation (user decision, 2026-10-12): every fixture in the chain holds a synthetic temporal
memory that expired at 2026-10-12 12:48. Pebble deletes expired memories, and the migration layer permits
exactly that (migrations.PRESERVED 'memories'). So every unexpired row must survive exactly, and a row past
its expiry must be gone. The historical Phase 11-13 upgrade validators, fixtures and reports are left
untouched as historical evidence. They assume the row survives forever and cannot pass after its expiry. This
validator also upgrades each preserved older fixture (epochs 1, 2 and 3), verified against its recorded
metadata first, straight to the current epoch.

Epoch 5 adds the active-time ledger tables and five additive supervised-job
columns on process_jobs. Every preserved table is therefore compared through
the exact column list it had before the upgrade; the additive columns are
checked separately (present, and empty on every preserved row).

Checks are recorded independently by name; a failure never stops later
independent checks and a report is written either way. --report must be a new
file. The Phase 13 executable runs only when its SHA-256 matches the preserved
candidate, and only against the private copy.

Usage:
  python scripts/validate_phase14_upgrade.py --report PATH [--backend PATH]
"""
import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from validate_runtime import ROOT, Service
sys.path.insert(0, str(ROOT / 'backend'))
from pebble.migrations import BACKUPS, SCHEMA_EPOCH, Limits, discard_backup


OLD = ROOT / '.runtime/phase13-candidate/pebble-backend/pebble-backend'
FIXTURE = ROOT / '.runtime/phase14-epoch4-fixture'
METADATA = ROOT / '.runtime/phase14-epoch4-fixture.json'
BUILDER = ROOT / '.runtime/phase14-baseline/build_epoch4_fixture.py'
# The epoch-3 fixture's metadata records the synthetic Phase 12 rows the epoch-4 fixture inherited.
PRIOR_METADATA = ROOT / '.runtime/phase13-epoch3-fixture.json'
EXPECTED_OLD = 'acb17af4f647fe4c813502a1072382676cf9a5ffed490e9bacc6399cade58244'
FROM_EPOCH = 4
SIDECARS = frozenset({'pebble.sqlite-shm', 'pebble.sqlite-wal'})
LAYERS = frozenset({'working', 'episodic', 'semantic', 'procedural', 'temporal', 'profile'})
# name -> (table, ORDER BY, WHERE). Rows are always selected through the pre-upgrade column list.
CORE = {'memories': ('memories', 'id', ''), 'checkpoints': ('checkpoints', 'chat_id', ''),
        'messages': ('messages', 'id', ''), 'chats': ('chats', 'id', ''),
        'permissions': ('settings', 'key', "WHERE key='permissions'")}
PHASE12 = {'runs': ('runs', 'id', ''), 'process_jobs': ('process_jobs', 'id', ''),
           'process_output': ('process_output', 'process_id,sequence', ''),
           'process_inspections': ('process_inspections', 'process_id', ''),
           'run_stream_attempts': ('run_stream_attempts', 'id', ''),
           'run_stream_events': ('run_stream_events', 'run_id,sequence', '')}
PHASE13 = {'agent_worktrees': ('agent_worktrees', 'worktree_id', ''),
           'agent_worktree_writes': ('agent_worktree_writes', 'id', ''),
           'git_plan_receipts': ('git_plan_receipts', 'id', ''),
           'project_environments': ('project_environments', 'id', '')}
PRESERVED = {**CORE, **PHASE12, **PHASE13}
NEW_TABLES = ('run_time_ledger', 'run_time_reservations')
# Additive epoch-5 columns (migrations.ADDITIVE['process_jobs'], process_jobs.V2_COLUMNS).
NEW_COLUMNS = ('jobdir', 'plan_hash', 'contained', 'settled_by', 'reservation_id')
HISTORY = 'SELECT backup_id,from_version,to_version,completed FROM upgrade_history ORDER BY id'
# Preserved older fixtures, upgraded straight to the current epoch (time-aware; see the module docstring).
OLDER = ((1, ROOT / '.runtime/phase11-epoch1-fixture', ROOT / '.runtime/phase11-epoch1-fixture.json'),
         (2, ROOT / '.runtime/phase12-epoch2-fixture', ROOT / '.runtime/phase12-epoch2-fixture.json'),
         (3, ROOT / '.runtime/phase13-epoch3-fixture', ROOT / '.runtime/phase13-epoch3-fixture.json'))
REFUSAL = 'Pebble stopped this upgrade: The database was written by a newer Pebble.'
BUDGET = 'Pebble stopped this upgrade: Retained upgrade backups reached their count or size limit.'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounded(exc, limit=1500):
    return (type(exc).__name__ + ': ' + str(exc))[:limit]


def read(home, statement, immutable=False):
    with connect(home / 'pebble.sqlite', immutable) as db:
        return db.execute(statement).fetchall()


def tables(home, immutable=False):
    return {name for (name,) in read(home, "SELECT name FROM sqlite_master WHERE type='table'", immutable)}


def columns_of(db, table):
    return [row[1] for row in db.execute(f'PRAGMA table_info({table})')]


def statement(name, columns):
    table, order, where = PRESERVED[name]
    return f'SELECT {",".join(columns[name])} FROM {table} {where} ORDER BY {order}'


def snapshot(db, columns):
    """Rows of every preserved table, selected through the given (pre-upgrade) column lists."""
    return {name: db.execute(statement(name, columns)).fetchall() for name in PRESERVED}


@contextlib.contextmanager
def connect(path, immutable=False):
    """A read-only connection, always closed on exit."""
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro' + ('&immutable=1' if immutable else ''),
                                            uri=True)) as db:
        yield db


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
        self.check('fixture metadata, file hashes and preserved Phase 13 executable verified', self.verify_fixture)
        shutil.copytree(FIXTURE, home)
        self.facts['home'] = home
        self.check('private copy holds the six memory layers, synthetic Phase 12 rows and Phase 13 rows at epoch 4',
                   self.copy_holds_rows)
        self.check('actual service waits in upgrade maintenance while the backup budget is full and changes nothing',
                   self.budget_refusal)
        self.check('oldest retained backup removed through the native maintenance route with its manifest hash',
                   self.discard_oldest)
        self.check('native retry upgrades the private epoch-4 copy to epoch 5 once', self.upgrade)
        self.check('six memory layers, checkpoint, chats, messages and permissions survive upgrade exactly; only expired memories go',
                   lambda: self.kept(CORE))
        self.check('synthetic Phase 12 run, process jobs, output, inspection and stream rows survive upgrade exactly',
                   lambda: self.kept(PHASE12))
        self.check('synthetic Phase 13 worktree, write, Git plan receipt and environment rows survive upgrade exactly',
                   lambda: self.kept(PHASE13))
        self.check('process jobs gain only the additive supervised-job columns, empty on preserved rows', self.additive)
        self.check('new Phase 14 ledger tables exist and are empty after upgrade', self.new_tables)
        self.check('verified pre-upgrade backup is epoch 4 and retains the exact preserved rows', self.backup)
        self.check('second current-service startup creates no new upgrade or backup', self.second_start)
        self.check('preserved Phase 13 executable refuses epoch 5 before mutating the database', self.refusal)
        for epoch, fixture, metadata in OLDER:
            self.check(f'preserved epoch-{epoch} fixture upgrades to the current epoch once; its rows survive exactly; '
                       'only expired memories go', lambda e=epoch, f=fixture, m=metadata: self.older(e, f, m, home.parent))

    def verify_fixture(self):
        metadata = json.loads(METADATA.read_text())
        prior = json.loads(PRIOR_METADATA.read_text())
        assert (metadata['synthetic_only'] is True and metadata['credentials_used'] is False
                and metadata['inference_calls'] == 0), 'The fixture is not marked synthetic and credential-free.'
        assert metadata['source_fixture'] == '.runtime/phase13-epoch3-fixture' and prior['synthetic_only'] is True
        assert metadata['epoch'] == FROM_EPOCH and SCHEMA_EPOCH == FROM_EPOCH + 1, (metadata['epoch'], SCHEMA_EPOCH)
        assert metadata['old_executable_sha256'] == EXPECTED_OLD and sha(OLD) == EXPECTED_OLD, \
            'The preserved Phase 13 executable does not match its recorded SHA-256.'
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
        assert read(FIXTURE, 'PRAGMA user_version', immutable=True) == [(FROM_EPOCH,)]
        # Phase 12 rows are counted by the epoch-3 record, Phase 13 rows by the epoch-4 record.
        self.facts['counts'] = {**prior['synthetic_rows'], **metadata['synthetic_rows']}
        self.facts['metadata'] = metadata
        return {'files': len(recorded), 'epoch': FROM_EPOCH, 'legacy_backend_sha256': EXPECTED_OLD}

    def copy_holds_rows(self):
        home, counts = self.need('home'), self.need('counts')
        assert wal(home) is None and read(home, 'PRAGMA user_version', immutable=True) == [(FROM_EPOCH,)]
        with connect(home / 'pebble.sqlite', immutable=True) as db:
            columns = {name: columns_of(db, PRESERVED[name][0]) for name in PRESERVED}
            before = snapshot(db, columns)
        assert all(columns.values()), [name for name, value in columns.items() if not value]
        assert not set(NEW_COLUMNS) & set(columns['process_jobs']), 'An epoch-4 process table already has v2 columns.'
        self.facts['columns'], self.facts['before'] = columns, before
        self.facts['history_before'] = read(home, HISTORY, immutable=True)
        assert {row[columns['memories'].index('kind')] for row in before['memories']} >= LAYERS
        rows = {name: len(before[name]) for name in counts}
        assert rows == counts, (rows, counts)
        # The fixture has chats and explicit permissions but no message rows; messages are still compared exactly.
        assert before['permissions'] and before['chats'], 'The fixture lacks conversation or permission rows.'
        assert not set(NEW_TABLES) & tables(home, immutable=True), 'An epoch-4 database already has Phase 14 tables.'
        return {'memory_layers': sorted(LAYERS), 'synthetic_rows': rows,
                'upgrade_history_rows': len(self.facts['history_before'])}

    def budget_refusal(self):
        """The fixture retains max_backups backups: the service waits in upgrade maintenance and changes nothing."""
        home = self.need('home')
        self.need('before')
        listed = backups(home)
        assert sum(name.endswith('.sqlite') for name in listed) >= Limits().max_backups, listed
        before = (sha(home / 'pebble.sqlite'), wal(home), listed, read(home, HISTORY))
        service = Service(self.command, home)
        try:
            view = service.rpc('state').get('maintenance')
            assert view and view['reason'] == 'backup_budget_exhausted', view
            for method, params in (('new_chat', {}), ('get_chat', {'id': 'x'}), ('setting', {'key': 'memory_enabled', 'value': True})):
                try:
                    service.rpc(method, **params)
                except RuntimeError as exc:
                    assert 'waiting for an upgrade step' in str(exc), str(exc)[:300]
                else:
                    raise AssertionError(method + ' was served while the upgrade waits.')
            listing = {item['id']: item for item in view['backups']}
        finally:
            service.close()
        after = (sha(home / 'pebble.sqlite'), wal(home), backups(home), read(home, HISTORY))
        assert after == before, 'Maintenance changed the database, its WAL, backups or upgrade history.'
        assert read(home, 'PRAGMA user_version') == [(FROM_EPOCH,)]
        self.facts['budget_refused'] = True
        self.facts['maintenance_backups'] = listing
        return {'reason': view['reason'], 'listed_backups': len(listing), 'max_backups': Limits().max_backups}

    def discard_oldest(self):
        """A person's removal through the native maintenance route, confirmed by the manifest hash it lists."""
        home, metadata = self.need('home'), self.need('metadata')
        self.need('budget_refused')
        listing = self.need('maintenance_backups')
        oldest = sorted(listing)[0]
        confirmation = metadata['files_sha256'][f'{BACKUPS}/{oldest}.manifest.json']
        assert listing[oldest]['manifest_sha256'] == confirmation, 'The native listing shows another manifest hash.'
        listed = backups(home)
        service = Service(self.command, home)
        try:
            try:
                service.rpc('upgrade_discard_backup', backup_id=oldest, manifest_sha256='0' * 64)
            except RuntimeError as exc:
                assert 'backup' in str(exc).lower(), str(exc)[:300]
            else:
                raise AssertionError('A wrong manifest hash removed a backup.')
            assert backups(home) == listed, 'A refused removal changed the backups.'
            removed = service.rpc('upgrade_discard_backup', backup_id=oldest, manifest_sha256=confirmation)
            assert removed['discarded'] == oldest and not any(name.startswith(oldest) for name in backups(home))
            assert service.rpc('upgrade_retry') == {'ready': True}
            assert service.rpc('state')['active'] is None  # the real service answers in the same process
        finally:
            service.close()
        self.facts['discarded'] = oldest
        return {'discarded': oldest, 'remaining_before_upgrade': len(listing) - 1, 'retried_in_place': True}

    def upgrade(self):
        """The in-place retry upgraded the copy to epoch 5 exactly once."""
        home = self.need('home')
        self.need('before')
        self.need('discarded')
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        history, earlier = read(home, HISTORY), self.need('history_before')
        assert history[:len(earlier)] == earlier, 'Earlier upgrade history rows changed.'
        added = history[len(earlier):]
        assert [(row[1], row[2]) for row in added] == [(FROM_EPOCH, SCHEMA_EPOCH)], [(row[1], row[2]) for row in added]
        assert added[0][0] and added[0][0] + '.sqlite' in backups(home), 'The upgrade recorded no backup.'
        self.facts['backup_id'] = added[0][0]
        self.facts['history'], self.facts['backups'] = history, backups(home)
        return {'from_version': FROM_EPOCH, 'to_version': SCHEMA_EPOCH, 'history_rows': len(history)}

    def older(self, epoch, fixture, metadata_path, scratch):
        """Upgrade an untouched historical fixture (verified first) straight to the current epoch."""
        metadata = json.loads(metadata_path.read_text())
        assert (metadata['epoch'] == epoch and metadata['synthetic_only'] is True
                and metadata['credentials_used'] is False), (metadata['epoch'], epoch)
        assert read(fixture, 'PRAGMA user_version', immutable=True) == [(epoch,)]
        if metadata.get('files_sha256'):
            recorded = {k: v for k, v in metadata['files_sha256'].items() if Path(k).name not in SIDECARS}
            actual = {p.relative_to(fixture).as_posix(): sha(p) for p in sorted(fixture.rglob('*'))
                      if p.is_file() and p.name not in SIDECARS}
            assert actual == recorded, sorted(set(actual) ^ set(recorded)) or 'A historical fixture file changed.'
        home = scratch / f'older-epoch{epoch}'
        shutil.copytree(fixture, home)
        if not metadata.get('files_sha256'):
            # The epoch-1 fixture recorded row counts (including its WAL), not file hashes: count on the copy.
            present = tables(home)
            # FTS5 shadow tables (memory_search_*) are derived index structures, not data.
            counted = {t: n for t, n in metadata['counts'].items() if t in present and not t.startswith('memory_search')}
            assert {t: read(home, f'SELECT count(*) FROM {t}')[0][0] for t in counted} == counted, 'Fixture rows differ.'
        names = [n for n in PRESERVED if PRESERVED[n][0] in tables(home)]
        with connect(home / 'pebble.sqlite') as db:
            columns = {n: columns_of(db, PRESERVED[n][0]) for n in names}
            before = {n: db.execute(statement(n, columns)).fetchall() for n in names}
            history_before = db.execute(HISTORY).fetchall() if 'upgrade_history' in tables(home) else []
        service = Service(self.command, home)
        try:
            assert service.rpc('state')['active'] is None
        finally:
            service.close()
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        history = read(home, HISTORY)
        assert history[:len(history_before)] == history_before, 'Earlier upgrade history rows changed.'
        assert [(r[1], r[2]) for r in history[len(history_before):]] == [(epoch, SCHEMA_EPOCH)], history[len(history_before):]
        now = time.time()
        at = columns['memories'].index('expires')
        expected = {**before, 'memories': [r for r in before['memories'] if r[at] is None or r[at] > now]}
        with connect(home / 'pebble.sqlite') as db:
            changed = [n for n in names if db.execute(statement(n, columns)).fetchall() != expected[n]]
            added = [c for c in columns_of(db, 'process_jobs') if c not in columns.get('process_jobs', [])] \
                if 'process_jobs' in columns else []
            filled = db.execute(f'SELECT count(*) FROM process_jobs WHERE {" OR ".join(c + " IS NOT NULL" for c in added)}') \
                .fetchone()[0] if added else 0
        assert not changed, changed
        assert filled == 0, 'An additive column is set on a preserved process job.'
        return {'from_version': epoch, 'to_version': SCHEMA_EPOCH, 'tables_compared': len(names),
                'expired_memories_removed': len(before['memories']) - len(expected['memories'])}

    def kept(self, names):
        home, before, columns = self.need('home'), self.need('before'), self.need('columns')
        self.need('backup_id')
        now = time.time()
        expected, expired = dict(before), 0
        if 'memories' in names:
            # The migration rule (migrations.PRESERVED 'memories'): a row past its expiry may go; nothing else may.
            at = columns['memories'].index('expires')
            kept_rows = [row for row in before['memories'] if row[at] is None or row[at] > now]
            expired = len(before['memories']) - len(kept_rows)
            expected['memories'] = kept_rows
        with connect(home / 'pebble.sqlite') as db:
            changed = [name for name in names if db.execute(statement(name, columns)).fetchall() != expected[name]]
        assert not changed, changed
        return {'tables': sorted(names), 'rows': {name: len(expected[name]) for name in sorted(names)},
                'expired_memories_removed': expired}

    def additive(self):
        home, columns = self.need('home'), self.need('columns')
        self.need('backup_id')
        with connect(home / 'pebble.sqlite') as db:
            now = columns_of(db, 'process_jobs')
            values = db.execute(f'SELECT {",".join(NEW_COLUMNS)} FROM process_jobs ORDER BY id').fetchall()
            others = {name: columns_of(db, PRESERVED[name][0]) for name in PRESERVED if name != 'process_jobs'}
        assert now[:len(columns['process_jobs'])] == columns['process_jobs'], 'Existing process columns moved.'
        assert now[len(columns['process_jobs']):] == list(NEW_COLUMNS), now[len(columns['process_jobs']):]
        assert values and all(value is None for row in values for value in row), \
            'A preserved process row gained supervised-job values during the upgrade.'
        grown = [name for name, value in others.items() if value != columns[name]]
        assert not grown, ('Other preserved tables changed shape.', grown)
        return {'added_columns': list(NEW_COLUMNS), 'preserved_process_rows': len(values)}

    def new_tables(self):
        home = self.need('home')
        self.need('backup_id')
        present = tables(home)
        assert set(NEW_TABLES) <= present, sorted(set(NEW_TABLES) - present)
        counts = {name: read(home, f'SELECT count(*) FROM {name}')[0][0] for name in NEW_TABLES}
        assert not any(counts.values()), counts
        return {'empty': sorted(NEW_TABLES)}

    def backup(self):
        home, before, ident, columns = self.need('home'), self.need('before'), self.need('backup_id'), self.need('columns')
        folder = home / BACKUPS
        files = {suffix: folder / (ident + suffix) for suffix in ('.sqlite', '.manifest.json', '.complete.json')}
        for path in files.values():
            assert path.is_file() and not path.is_symlink(), path.name
        with connect(files['.sqlite'], immutable=True) as db:
            assert db.execute('PRAGMA user_version').fetchall() == [(FROM_EPOCH,)]
            assert snapshot(db, columns) == before, 'The backup differs from the pre-upgrade rows.'
            assert not {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} & set(NEW_TABLES)
        return {'backup_epoch': FROM_EPOCH, 'tables_compared': len(PRESERVED)}

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
        assert refused.returncode != 0, 'The Phase 13 executable accepted an epoch-5 database.'
        assert REFUSAL in refused.stderr, refused.stderr[-600:]
        after = (sha(home / 'pebble.sqlite'), wal(home), backups(home), read(home, HISTORY))
        assert after == before, 'The Phase 13 executable changed the database, its WAL, backups or upgrade history.'
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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase14-upgrade-')).resolve()
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
              'source_sha256': {p: sha(ROOT / p) for p in ('scripts/validate_phase14_upgrade.py', 'scripts/validate_runtime.py',
                                                           'backend/pebble/migrations.py', 'backend/pebble/time_ledger.py',
                                                           'backend/pebble/process_jobs.py', 'backend/pebble/app.py')},
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
# calls them. They read the parent-owned fixture and work on copies under
# pytest's tmp_path. Neither the service nor the Phase 13 executable is started.

def test_parent_fixture_and_preserved_executable_verify():
    upgrade = Upgrade(['unused'])
    assert upgrade.check('fixture', upgrade.verify_fixture), upgrade.checks[-1].get('error')
    assert upgrade.facts['old_verified'] is True and upgrade.facts['metadata']['epoch'] == FROM_EPOCH
    assert set(upgrade.facts['counts']) == set(PHASE12) | set(PHASE13)


def test_private_copy_holds_the_preserved_rows(tmp_path):
    upgrade = Upgrade(['unused'])
    assert upgrade.check('fixture', upgrade.verify_fixture), upgrade.checks[-1].get('error')
    home = tmp_path / 'home'
    shutil.copytree(FIXTURE, home)
    upgrade.facts['home'] = home
    assert upgrade.check('copy', upgrade.copy_holds_rows), upgrade.checks[-1].get('error')
    before, columns = upgrade.facts['before'], upgrade.facts['columns']
    assert [row[columns['runs'].index('state')] for row in before['runs']] == ['completed']
    assert len(before['process_jobs']) == 2 and len(before['git_plan_receipts']) == 1
    assert upgrade.facts['history_before'][-1][1:3] == (3, FROM_EPOCH)
    assert sha(home / 'pebble.sqlite') == upgrade.facts['metadata']['files_sha256']['pebble.sqlite'], 'A read changed the copy.'


def test_explicit_discard_needs_the_listed_manifest_hash(tmp_path):
    upgrade = Upgrade(['unused'])
    assert upgrade.check('fixture', upgrade.verify_fixture), upgrade.checks[-1].get('error')
    home = tmp_path / 'home'
    shutil.copytree(FIXTURE, home)
    upgrade.facts['home'] = home
    assert upgrade.check('copy', upgrade.copy_holds_rows), upgrade.checks[-1].get('error')
    listed = backups(home)
    oldest = sorted(name[:-len('.sqlite')] for name in listed if name.endswith('.sqlite'))[0]
    try:
        discard_backup(home, oldest, '0' * 64)
    except ValueError as exc:
        assert 'backup' in str(exc).lower()
    else:
        raise AssertionError('A wrong confirmation hash removed a backup.')
    assert backups(home) == listed
    assert sha(home / 'pebble.sqlite') == upgrade.facts['metadata']['files_sha256']['pebble.sqlite']


def test_epoch_5_names_match_the_backend_sources():
    from pebble import migrations, process_jobs, time_ledger
    created = {line.split('(')[0].split()[-1] for line in time_ledger.LEDGER_DDL.splitlines()
               if line.startswith('CREATE TABLE')}
    assert created == set(NEW_TABLES)
    assert tuple(name for name, _ in process_jobs.V2_COLUMNS) == NEW_COLUMNS
    assert migrations.ADDITIVE['process_jobs'] == set(NEW_COLUMNS)
    assert set(NEW_TABLES) <= set(migrations.NO_NEW) and all(name in migrations.PRESERVED for name in NEW_TABLES)


def test_comparison_through_pre_upgrade_columns_ignores_only_additive_columns(tmp_path):
    path = tmp_path / 'pebble.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE process_jobs(id TEXT PRIMARY KEY, state TEXT)')
        db.execute("INSERT INTO process_jobs VALUES('a','succeeded')")
    with connect(path) as db:
        columns = {'process_jobs': columns_of(db, 'process_jobs')}
        before = db.execute(statement('process_jobs', columns)).fetchall()
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE process_jobs ADD COLUMN jobdir TEXT')
    with connect(path) as db:
        assert db.execute(statement('process_jobs', columns)).fetchall() == before
    with sqlite3.connect(path) as db:
        db.execute("UPDATE process_jobs SET state='unknown'")
    with connect(path) as db:
        assert db.execute(statement('process_jobs', columns)).fetchall() != before


def test_source_upgrade_run_on_explicit_request(request, tmp_path):
    """The whole validator in source mode, as a subprocess with its scratch under tmp_path.

    It starts the actual service and the preserved Phase 13 executable, so it runs only when this
    node ID is named explicitly; the default collection skips it.
    """
    import pytest
    if not any(str(arg).endswith('::test_source_upgrade_run_on_explicit_request') for arg in request.config.args):
        pytest.skip('Starts the actual service; name this test explicitly to run it.')
    report, log = tmp_path / 'report.json', tmp_path / 'upgrade.log'
    with log.open('w') as sink:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--report', str(report)], cwd=ROOT,
                              env={**os.environ, 'TMPDIR': str(tmp_path)}, stdout=sink, stderr=subprocess.STDOUT,
                              timeout=300)
    outcome = json.loads(report.read_text())
    assert done.returncode == 0 and outcome['passed'] is True, json.dumps(
        {'failed': outcome['failed'], 'fatal': outcome['fatal_error'],
         'errors': [c.get('error') for c in outcome['check_results'] if not c['passed']]})
    assert outcome['kind'] == 'source' and len(outcome['checks']) == len(set(outcome['checks'])) == 13
    import warnings
    warnings.warn('validator result line: ' + log.read_text().strip().splitlines()[-1])  # shown in the -q summary


def test_dependent_checks_never_run_without_their_prerequisites():
    upgrade = Upgrade(['unused'])
    for name, fn in (('budget', upgrade.budget_refusal), ('discard', upgrade.discard_oldest),
                     ('refusal', upgrade.refusal), ('backup', upgrade.backup), ('second', upgrade.second_start),
                     ('core', lambda: upgrade.kept(CORE)), ('additive', upgrade.additive),
                     ('tables', upgrade.new_tables), ('upgrade', upgrade.upgrade)):
        assert upgrade.check(name, fn) is False and 'Prerequisite' in upgrade.checks[-1]['error'], name
    try:
        upgrade.check('refusal', upgrade.refusal)
    except AssertionError:
        pass
    else:
        raise AssertionError('A duplicate check name was accepted.')
