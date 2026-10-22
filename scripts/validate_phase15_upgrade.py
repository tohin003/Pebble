"""Actual Phase 14 epoch-5 fixture -> current service upgrade, older fixtures, and downgrade refusal.

Uses the parent-owned synthetic epoch-5 fixture (.runtime/phase15-epoch5-fixture,
built by .runtime/phase15-baseline/build_epoch5_fixture.py with the preserved
Phase 14 candidate) and that preserved Phase 14 executable. Never touches an
installed home, account, credential or model. Each mode upgrades its own private
copies under the system temporary folder, never under .runtime. The fixture's
manual synthetic rows (six memory layers, a settled run, Phase 12 process jobs,
output, inspection and stream rows, settled Phase 13 worktree, write, Git plan
receipt and environment rows, and settled Phase 14 ledger slice, reservation and
supervised process job, plus a superseded learned memory pair) are preservation
fixtures, not learned or executed evidence.

The epoch-5 fixture retains three upgrade backups (epochs 2->3, 3->4, 4->5),
which is migrations.Limits.max_backups including the new one. The service must
therefore wait in upgrade maintenance (pebble.maintenance) and change nothing,
serving only its maintenance routes. The validator then removes the oldest backup
through the native upgrade_discard_backup route, as a person would in the app,
confirmed by the manifest hash the service lists (cross-checked with the
fixture's recorded hash), and retries startup in place with upgrade_retry.
Nothing is deleted automatically. A preserved older fixture whose budget is also
full (epoch 4) goes through the same native route.

Time-aware preservation (user decision, 2026-10-12): every fixture in the chain holds a synthetic temporal
memory that expired at 2026-10-12 12:48. Pebble deletes expired memories, and the migration layer permits
exactly that (migrations.PRESERVED 'memories'), together with the learning evidence of an expired memory.
So every unexpired row survives exactly, and only expired memories (and their evidence) go. This validator
also upgrades each preserved older fixture (epochs 1-4), verified against its recorded metadata first,
straight to the current epoch.

The current epoch is never pinned: every comparison uses migrations.SCHEMA_EPOCH and the fixture's own
recorded epoch, so this validator keeps its subject in later phases. Tables that are new since a
fixture's epoch must exist after the upgrade and be empty, except that the memory graph may hold this
installation's local scope salt and the supersedes edges migrated from memory_evidence.superseded_by. Every
preserved table is compared through the exact column list it had before the upgrade; a column added to a
preserved table must be empty on every preserved row.

Checks are recorded independently by name; a failure never stops later
independent checks and a report is written either way. --report must be a new
file. The Phase 14 executable runs only when its SHA-256 matches the preserved
candidate, and only against the private copy.

Usage:
  python scripts/validate_phase15_upgrade.py --report PATH [--backend PATH]
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
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


OLD = ROOT / '.runtime/phase14-candidate/pebble-backend/pebble-backend'
FIXTURE = ROOT / '.runtime/phase15-epoch5-fixture'
METADATA = ROOT / '.runtime/phase15-epoch5-fixture.json'
BUILDER = ROOT / '.runtime/phase15-baseline/build_epoch5_fixture.py'
# The epoch-3 and epoch-4 metadata record the synthetic Phase 12 and Phase 13 rows the epoch-5 fixture inherited.
PRIOR_METADATA = (ROOT / '.runtime/phase13-epoch3-fixture.json', ROOT / '.runtime/phase14-epoch4-fixture.json')
EXPECTED_OLD = '13f9545cead3fe9df5083feeabdcef7018603e5edf00af11845a80f67cd350a3'
FROM_EPOCH = 5  # the fixture's own epoch, never the current one
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
PHASE14 = {'run_time_ledger': ('run_time_ledger', 'slice_id', ''),
           'run_time_reservations': ('run_time_reservations', 'reservation_id', '')}
LEARNING = {'memory_evidence': ('memory_evidence', 'memory_id', ''),
            'memory_tombstones': ('memory_tombstones', 'scope,kind,slot,fingerprint', '')}
PRESERVED = {**CORE, **PHASE12, **PHASE13, **PHASE14, **LEARNING}
# Rows the epoch-6 graph may hold right after an upgrade (see the module docstring).
GRAPH_ROWS = frozenset({'memory_scope_salts', 'memory_relations'})
HISTORY = 'SELECT backup_id,from_version,to_version,completed FROM upgrade_history ORDER BY id'
# Preserved older fixtures, upgraded straight to the current epoch (time-aware; see the module docstring).
OLDER = ((1, ROOT / '.runtime/phase11-epoch1-fixture', ROOT / '.runtime/phase11-epoch1-fixture.json'),
         (2, ROOT / '.runtime/phase12-epoch2-fixture', ROOT / '.runtime/phase12-epoch2-fixture.json'),
         (3, ROOT / '.runtime/phase13-epoch3-fixture', ROOT / '.runtime/phase13-epoch3-fixture.json'),
         (4, ROOT / '.runtime/phase14-epoch4-fixture', ROOT / '.runtime/phase14-epoch4-fixture.json'))
REFUSAL = 'Pebble stopped this upgrade: The database was written by a newer Pebble.'
CREATED = re.compile(r'CREATE (?:VIRTUAL )?TABLE (?:IF NOT EXISTS )?(\w+)')


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
    """Rows of every preserved table present, selected through the given (pre-upgrade) column lists."""
    return {name: db.execute(statement(name, columns)).fetchall() for name in columns}


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


def epoch6_tables():
    """Names the Phase 15 epoch-6 DDL creates, read from the backend sources (never a hard-coded epoch)."""
    from pebble import agent_tree, delegation_history, memory_graph, store, token_calibration
    ddl = (memory_graph.GRAPH_DDL + agent_tree.ROUNDS_DDL + delegation_history.HISTORY_DDL + store.ROUTES_DDL
           + token_calibration.DDL)
    return frozenset(CREATED.findall(ddl))


def expected_rows(name, before, columns, survivors):
    """Time-aware expectation: expired memories, and the evidence of a gone memory, may go."""
    rows = before[name]
    if name == 'memories':
        at = columns[name].index('expires')
        now = time.time()
        return [row for row in rows if row[at] is None or row[at] > now]
    if name == 'memory_evidence':
        at = columns[name].index('memory_id')
        return [row for row in rows if row[at] in survivors]
    return rows


def compare(db, names, before, columns):
    """{name: problem} for preserved tables whose rows changed beyond the time-aware rule."""
    at = columns['memories'].index('id') if 'memories' in columns else None
    survivors = {row[at] for row in expected_rows('memories', before, columns, None)} if at is not None else set()
    problems = {}
    for name in names:
        actual = db.execute(statement(name, columns)).fetchall()
        expected = expected_rows(name, before, columns, survivors)
        if name == 'memory_evidence':
            # Evidence of a gone memory may stay until the next expiry pass, or go now; nothing else changes.
            if not set(expected) <= set(actual) <= set(before[name]):
                problems[name] = {'missing': len(set(expected) - set(actual)),
                                  'unexpected': len(set(actual) - set(before[name]))}
        elif actual != expected:
            problems[name] = {'expected_rows': len(expected), 'actual_rows': len(actual)}
    return problems


def added_columns(db, columns):
    """{table: (moved, added, filled)} for preserved tables: existing columns must keep their order and an
    added column must be empty on every row (startup creates no rows in these tables)."""
    report = {}
    for name, old in columns.items():
        table = PRESERVED[name][0]
        now = columns_of(db, table)
        added = now[len(old):]
        filled = db.execute(f'SELECT count(*) FROM {table} WHERE ' + ' OR '.join(f'{c} IS NOT NULL' for c in added)) \
            .fetchone()[0] if added else 0
        if now[:len(old)] != old or filled:
            report[name] = {'moved': now[:len(old)] != old, 'added': added, 'filled': filled}
    return report


def new_tables_problems(home, before_tables, checked=None):
    """Epoch-6 tables new since the fixture's epoch: empty except the allowed graph rows. Older
    fixtures also gain earlier subsystems' tables (with their version rows); those are listed only."""
    checked = epoch6_tables() if checked is None else checked
    with connect(home / 'pebble.sqlite') as db:
        rows = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        new = {name: sql or '' for name, sql in rows if name not in before_tables}
        virtual = [name for name, sql in new.items() if sql.upper().startswith('CREATE VIRTUAL TABLE')]
        shadows = {name for name in new for v in virtual if name.startswith(v + '_')}
        counts = {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in new if name in checked}
        problems = {name: count for name, count in counts.items() if count and name not in GRAPH_ROWS}
        salts = edges = []
        if 'memory_scope_salts' in new:
            salts = db.execute('SELECT origin FROM memory_scope_salts').fetchall()
            if salts != [('local',)]:
                problems['memory_scope_salts'] = {'origins': sorted(origin for (origin,) in salts)}
        if 'memory_relations' in new:
            edges = db.execute('SELECT from_id,to_id,relation,evidence FROM memory_relations').fetchall()
            links = set()
            if 'memory_evidence' in before_tables:
                links = set(db.execute('SELECT superseded_by,memory_id FROM memory_evidence '
                                       'WHERE superseded_by IS NOT NULL AND superseded_by<>memory_id').fetchall())
            wrong = [edge for edge in edges if edge[2] != 'supersedes' or (edge[0], edge[1]) not in links
                     or json.loads(edge[3]).get('basis') != 'superseded_by']
            if wrong or {(a, b) for a, b, _, _ in edges} != links:
                problems['memory_relations'] = {'edges': len(edges), 'links': len(links), 'unexpected': len(wrong)}
    return problems, {'new_tables': sorted(set(new) - shadows), 'salts': len(salts), 'supersedes_edges': len(edges)}


def upgrade_in_place(command, home, recorded=None):
    """Start the current service once; when it waits in maintenance for a full backup budget, remove the
    oldest backup through the native route (confirmed by the recorded manifest hash) and retry."""
    service = Service(command, home)
    discarded = None
    try:
        state = service.rpc('state')
        view = state.get('maintenance')
        if view:
            assert view['reason'] == 'backup_budget_exhausted', view
            listing = {item['id']: item for item in view['backups']}
            oldest = sorted(listing)[0]
            confirmation = listing[oldest]['manifest_sha256']
            if recorded is not None:
                assert recorded.get(f'{BACKUPS}/{oldest}.manifest.json') == confirmation, \
                    'The native listing shows another manifest hash than the fixture recorded.'
            removed = service.rpc('upgrade_discard_backup', backup_id=oldest, manifest_sha256=confirmation)
            assert removed['discarded'] == oldest, removed
            assert service.rpc('upgrade_retry') == {'ready': True}
            state, discarded = service.rpc('state'), oldest
        assert state['active'] is None
    finally:
        service.close()
    return discarded


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
        self.check('fixture metadata, file hashes and preserved Phase 14 executable verified', self.verify_fixture)
        shutil.copytree(FIXTURE, home)
        self.facts['home'] = home
        self.check('private copy holds the six memory layers and the synthetic Phase 12, 13 and 14 rows at epoch 5',
                   self.copy_holds_rows)
        self.check('actual service waits in upgrade maintenance while the backup budget is full and changes nothing',
                   self.budget_refusal)
        self.check('oldest retained backup removed through the native maintenance route with its manifest hash',
                   self.discard_oldest)
        self.check('native retry upgrades the private epoch-5 copy to the current epoch once', self.upgrade)
        self.check('six memory layers, checkpoint, chats, messages and permissions survive upgrade exactly; only expired memories go',
                   lambda: self.kept(CORE))
        self.check('synthetic Phase 12 run, process jobs, output, inspection and stream rows survive upgrade exactly',
                   lambda: self.kept(PHASE12))
        self.check('synthetic Phase 13 worktree, write, Git plan receipt and environment rows survive upgrade exactly',
                   lambda: self.kept(PHASE13))
        self.check('synthetic Phase 14 ledger slice, reservation and supervised process job survive upgrade exactly',
                   self.kept_phase14)
        self.check('learning evidence and tombstones survive upgrade exactly; only evidence of expired memories goes',
                   lambda: self.kept(LEARNING))
        self.check('preserved tables keep their columns; any added column is empty on preserved rows', self.additive)
        self.check('tables new since the fixture epoch exist and are empty, except the local scope salt and migrated '
                   'supersedes edges', self.new_tables)
        self.check('verified pre-upgrade backup is epoch 5 and retains the exact preserved rows', self.backup)
        self.check('second current-service startup creates no new upgrade or backup', self.second_start)
        self.check('preserved Phase 14 executable refuses the current epoch before mutating the database', self.refusal)
        for epoch, fixture, metadata in OLDER:
            self.check(f'preserved epoch-{epoch} fixture upgrades to the current epoch once; its rows survive exactly; '
                       'only expired memories go',
                       lambda e=epoch, f=fixture, m=metadata: self.older(e, f, m, home.parent))

    def verify_fixture(self):
        metadata = json.loads(METADATA.read_text())
        prior = {}
        for path in PRIOR_METADATA:
            record = json.loads(path.read_text())
            assert record['synthetic_only'] is True and record['credentials_used'] is False
            prior.update(record['synthetic_rows'])
        assert (metadata['synthetic_only'] is True and metadata['credentials_used'] is False
                and metadata['inference_calls'] == 0), 'The fixture is not marked synthetic and credential-free.'
        assert metadata['source_fixture'] == '.runtime/phase14-epoch4-fixture'
        assert metadata['epoch'] == FROM_EPOCH and SCHEMA_EPOCH > FROM_EPOCH, (metadata['epoch'], SCHEMA_EPOCH)
        assert metadata['old_executable_sha256'] == EXPECTED_OLD and sha(OLD) == EXPECTED_OLD, \
            'The preserved Phase 14 executable does not match its recorded SHA-256.'
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
        self.facts['prior_rows'] = prior
        self.facts['metadata'] = metadata
        return {'files': len(recorded), 'epoch': FROM_EPOCH, 'current_epoch': SCHEMA_EPOCH,
                'legacy_backend_sha256': EXPECTED_OLD}

    def copy_holds_rows(self):
        home, metadata, prior = self.need('home'), self.need('metadata'), self.need('prior_rows')
        assert wal(home) is None and read(home, 'PRAGMA user_version', immutable=True) == [(FROM_EPOCH,)]
        present = tables(home, immutable=True)
        assert {PRESERVED[name][0] for name in PRESERVED} <= present, sorted({PRESERVED[n][0] for n in PRESERVED} - present)
        with connect(home / 'pebble.sqlite', immutable=True) as db:
            columns = {name: columns_of(db, PRESERVED[name][0]) for name in PRESERVED}
            before = snapshot(db, columns)
            counts = {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in metadata['table_rows']}
        assert counts == metadata['table_rows'], 'Row counts differ from the builder record.'
        self.facts['columns'], self.facts['before'], self.facts['tables_before'] = columns, before, present
        self.facts['history_before'] = read(home, HISTORY, immutable=True)
        assert {row[columns['memories'].index('kind')] for row in before['memories']} >= LAYERS
        # Phase 12 and 13 rows are counted by the earlier records; Phase 14 rows by this one (only it adds them).
        inherited = {name: len(before[name]) for name in prior if name in before}
        assert all(inherited[name] >= prior[name] for name in inherited), (inherited, prior)
        for name in PHASE14:
            assert len(before[name]) == metadata['synthetic_rows'][name], (name, len(before[name]))
        ids = metadata['synthetic_ids']
        job = dict(zip(columns['process_jobs'], next(r for r in before['process_jobs'] if r[0] == ids['process'])))
        assert job['contained'] == 1 and job['settled_by'] == 'supervisor_record' and job['reservation_id'] == ids['reservation']
        evidence = {r[0]: dict(zip(columns['memory_evidence'], r)) for r in before['memory_evidence']}
        assert evidence[ids['superseded']]['superseded_by'] == ids['current'], 'The superseded memory link is missing.'
        assert not epoch6_tables() & present, 'An epoch-5 database already has epoch-6 tables.'
        return {'memory_layers': sorted(LAYERS), 'inherited_rows': inherited,
                'phase14_rows': {name: len(before[name]) for name in PHASE14},
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
            for method, params in (('new_chat', {}), ('get_chat', {'id': 'x'}), ('memory_contests', {}),
                                   ('setting', {'key': 'memory_enabled', 'value': True})):
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
        """The in-place retry upgraded the copy to the current epoch exactly once."""
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

    def kept(self, names):
        home, before, columns = self.need('home'), self.need('before'), self.need('columns')
        self.need('backup_id')
        with connect(home / 'pebble.sqlite') as db:
            problems = compare(db, sorted(names), before, columns)
        assert not problems, problems
        expired = 0
        if 'memories' in names:
            expired = len(before['memories']) - len(expected_rows('memories', before, columns, None))
        return {'tables': sorted(names), 'rows_before': {name: len(before[name]) for name in sorted(names)},
                'expired_memories_removed': expired}

    def kept_phase14(self):
        evidence = self.kept(PHASE14)
        home, columns, ids = self.need('home'), self.need('columns'), self.need('metadata')['synthetic_ids']
        with connect(home / 'pebble.sqlite') as db:
            row = db.execute(f'SELECT {",".join(columns["process_jobs"])} FROM process_jobs WHERE id=?',
                             (ids['process'],)).fetchone()
        job = dict(zip(columns['process_jobs'], row))
        assert (job['state'], job['contained'], job['settled_by'], job['plan_hash']) == (
            'succeeded', 1, 'supervisor_record', 'e' * 64), 'The supervised process job changed.'
        return {**evidence, 'supervised_job_kept': True}

    def additive(self):
        home, columns = self.need('home'), self.need('columns')
        self.need('backup_id')
        with connect(home / 'pebble.sqlite') as db:
            report = added_columns(db, columns)
        assert not report, report
        return {'tables_compared': len(columns)}

    def new_tables(self):
        home, before = self.need('home'), self.need('tables_before')
        self.need('backup_id')
        expected = epoch6_tables()
        present = tables(home)
        assert expected <= present, sorted(expected - present)
        problems, evidence = new_tables_problems(home, before)
        assert not problems, problems
        assert evidence['salts'] == 1, 'The upgraded database holds no local scope salt.'
        ids = self.need('metadata')['synthetic_ids']
        assert read(home, "SELECT count(*) FROM memory_relations WHERE relation='supersedes' AND from_id='%s' "
                          "AND to_id='%s'" % (ids['current'], ids['superseded'])) == [(1,)], \
            'The superseded_by link was not migrated to a supersedes edge.'
        return {**evidence, 'epoch6_tables': sorted(expected)}

    def backup(self):
        home, before, ident, columns = self.need('home'), self.need('before'), self.need('backup_id'), self.need('columns')
        folder = home / BACKUPS
        files = {suffix: folder / (ident + suffix) for suffix in ('.sqlite', '.manifest.json', '.complete.json')}
        for path in files.values():
            assert path.is_file() and not path.is_symlink(), path.name
        with connect(files['.sqlite'], immutable=True) as db:
            assert db.execute('PRAGMA user_version').fetchall() == [(FROM_EPOCH,)]
            assert snapshot(db, columns) == before, 'The backup differs from the pre-upgrade rows.'
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert not names & epoch6_tables(), 'The pre-upgrade backup already holds epoch-6 tables.'
        return {'backup_epoch': FROM_EPOCH, 'tables_compared': len(columns)}

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
        assert refused.returncode != 0, 'The Phase 14 executable accepted a newer epoch.'
        assert REFUSAL in refused.stderr, refused.stderr[-600:]
        after = (sha(home / 'pebble.sqlite'), wal(home), backups(home), read(home, HISTORY))
        assert after == before, 'The Phase 14 executable changed the database, its WAL, backups or upgrade history.'
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        return {'exit_code': refused.returncode, 'refused_epoch': SCHEMA_EPOCH, 'database_unchanged': True}

    def older(self, epoch, fixture, metadata_path, scratch):
        """Upgrade an untouched historical fixture (verified first) straight to the current epoch."""
        metadata = verify_older(epoch, fixture, metadata_path)
        home = scratch / f'older-epoch{epoch}'
        shutil.copytree(fixture, home)
        if not metadata.get('files_sha256'):
            # The epoch-1 fixture recorded row counts (including its WAL), not file hashes: count on the copy.
            present = tables(home)
            # FTS5 shadow tables (memory_search_*) are derived index structures, not data.
            counted = {t: n for t, n in metadata['counts'].items() if t in present and not t.startswith('memory_search')}
            assert {t: read(home, f'SELECT count(*) FROM {t}')[0][0] for t in counted} == counted, 'Fixture rows differ.'
        before_tables = tables(home)
        names = [n for n in PRESERVED if PRESERVED[n][0] in before_tables]
        with connect(home / 'pebble.sqlite') as db:
            columns = {n: columns_of(db, PRESERVED[n][0]) for n in names}
            before = snapshot(db, columns)
            history_before = db.execute(HISTORY).fetchall() if 'upgrade_history' in before_tables else []
        discarded = upgrade_in_place(self.command, home, metadata.get('files_sha256'))
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        history = read(home, HISTORY)
        assert history[:len(history_before)] == history_before, 'Earlier upgrade history rows changed.'
        assert [(r[1], r[2]) for r in history[len(history_before):]] == [(epoch, SCHEMA_EPOCH)], history[len(history_before):]
        with connect(home / 'pebble.sqlite') as db:
            problems = compare(db, names, before, columns)
            added = added_columns(db, columns)
        assert not problems, problems
        assert not added, ('A preserved table changed shape or an added column was filled.', added)
        assert epoch6_tables() <= tables(home), sorted(epoch6_tables() - tables(home))
        graph, evidence = new_tables_problems(home, before_tables)
        assert not graph, graph
        expired = len(before['memories']) - len(expected_rows('memories', before, columns, None)) if 'memories' in before else 0
        return {'from_version': epoch, 'to_version': SCHEMA_EPOCH, 'tables_compared': len(names),
                'maintenance_discarded': discarded, 'expired_memories_removed': expired, **evidence}


def verify_older(epoch, fixture, metadata_path):
    """A preserved older fixture must match its own record before it is copied."""
    metadata = json.loads(metadata_path.read_text())
    assert (metadata['epoch'] == epoch and metadata['synthetic_only'] is True
            and metadata['credentials_used'] is False), (metadata['epoch'], epoch)
    assert read(fixture, 'PRAGMA user_version', immutable=True) == [(epoch,)]
    if metadata.get('files_sha256'):
        recorded = {k: v for k, v in metadata['files_sha256'].items() if Path(k).name not in SIDECARS}
        actual = {p.relative_to(fixture).as_posix(): sha(p) for p in sorted(fixture.rglob('*'))
                  if p.is_file() and p.name not in SIDECARS}
        assert actual == recorded, sorted(set(actual) ^ set(recorded)) or 'A historical fixture file changed.'
    return metadata


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase15-upgrade-')).resolve()
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
              'current_epoch': SCHEMA_EPOCH, 'fixture_epoch': FROM_EPOCH,
              'checks': [c['name'] for c in upgrade.checks if c['passed']],
              'failed': [c['name'] for c in upgrade.checks if not c['passed']],
              'check_results': upgrade.checks, 'fatal_error': fatal,
              'scratch_preserved': None if passed else str(scratch),
              'fixture_metadata_sha256': sha(METADATA) if METADATA.is_file() else None,
              'source_sha256': {p: sha(ROOT / p) for p in (
                  'scripts/validate_phase15_upgrade.py', 'scripts/validate_runtime.py', 'backend/pebble/migrations.py',
                  'backend/pebble/memory_graph.py', 'backend/pebble/memory_learning.py', 'backend/pebble/maintenance.py',
                  'backend/pebble/time_ledger.py', 'backend/pebble/process_jobs.py', 'backend/pebble/app.py')},
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
# calls them. They read the parent-owned fixtures and work on copies under
# pytest's tmp_path. Neither the service nor the Phase 14 executable is started.
# Checks that need the epoch-5 fixture skip, naming the reason, until the parent
# has built it with the preserved Phase 14 candidate.

def _need_epoch5_fixture():
    import pytest
    if not (FIXTURE.is_dir() and METADATA.is_file()):
        pytest.skip('The parent has not built .runtime/phase15-epoch5-fixture yet (build_epoch5_fixture.py).')


def _builder():
    import importlib.util
    spec = importlib.util.spec_from_file_location('build_epoch5_fixture', BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parent_fixture_and_preserved_executable_verify():
    _need_epoch5_fixture()
    upgrade = Upgrade(['unused'])
    assert upgrade.check('fixture', upgrade.verify_fixture), upgrade.checks[-1].get('error')
    assert upgrade.facts['old_verified'] is True and upgrade.facts['metadata']['epoch'] == FROM_EPOCH
    assert set(upgrade.facts['metadata']['synthetic_rows']) >= set(PHASE14)


def test_private_copy_holds_the_preserved_rows(tmp_path):
    _need_epoch5_fixture()
    upgrade = Upgrade(['unused'])
    assert upgrade.check('fixture', upgrade.verify_fixture), upgrade.checks[-1].get('error')
    home = tmp_path / 'home'
    shutil.copytree(FIXTURE, home)
    upgrade.facts['home'] = home
    assert upgrade.check('copy', upgrade.copy_holds_rows), upgrade.checks[-1].get('error')
    assert upgrade.facts['history_before'][-1][1:3] == (4, FROM_EPOCH)
    assert sum(name.endswith('.sqlite') for name in backups(home)) >= Limits().max_backups
    assert sha(home / 'pebble.sqlite') == upgrade.facts['metadata']['files_sha256']['pebble.sqlite'], 'A read changed the copy.'


def test_explicit_discard_needs_the_listed_manifest_hash(tmp_path):
    _need_epoch5_fixture()
    home = tmp_path / 'home'
    shutil.copytree(FIXTURE, home)
    listed = backups(home)
    oldest = sorted(name[:-len('.sqlite')] for name in listed if name.endswith('.sqlite'))[0]
    try:
        discard_backup(home, oldest, '0' * 64)
    except ValueError as exc:
        assert 'backup' in str(exc).lower()
    else:
        raise AssertionError('A wrong confirmation hash removed a backup.')
    assert backups(home) == listed


def test_older_fixtures_verify_against_their_records():
    for epoch, fixture, metadata in OLDER:
        assert verify_older(epoch, fixture, metadata)['epoch'] == epoch
    # The epoch-4 fixture's budget is full, so its upgrade goes through the native maintenance route.
    assert sum(name.endswith('.sqlite') for name in backups(OLDER[-1][1])) >= Limits().max_backups


def test_epoch_6_names_match_the_backend_sources():
    from pebble import migrations
    names = epoch6_tables()
    graph = {'memory_relations', 'memory_claims', 'memory_tombstone_blooms', 'memory_scope_salts'}
    assert names == graph | {'agent_rounds', 'delegation_history', 'delegation_salt', 'message_routes', 'token_calibration'}
    assert all(name in migrations.PRESERVED for name in names)
    # Startup never creates delegation, route or calibration rows; only the graph may be written (salt, edges).
    assert names - graph <= set(migrations.NO_NEW) and not graph & set(migrations.NO_NEW) and GRAPH_ROWS <= graph
    assert SCHEMA_EPOCH > FROM_EPOCH  # the subject of this validator; never pinned to one number


def test_builder_rows_fit_the_real_epoch_5_schema(tmp_path):
    """The builder's inserts on a copy of the real epoch-4 database plus the source epoch-5 DDL."""
    from pebble import process_jobs, time_ledger
    copy = tmp_path / 'pebble.sqlite'
    with connect(OLDER[-1][1] / 'pebble.sqlite', immutable=True) as source, contextlib.closing(sqlite3.connect(copy)) as db:
        source.backup(db)
        db.executescript(time_ledger.LEDGER_DDL)
        present = {row[1] for row in db.execute('PRAGMA table_info(process_jobs)')}
        for name, kind in process_jobs.V2_COLUMNS:
            if name not in present:
                db.execute(f'ALTER TABLE process_jobs ADD COLUMN {name} {kind}')
        with db:
            added = _builder().insert_rows(db, time.time())
        assert added == {'run_time_ledger': 1, 'run_time_reservations': 1, 'process_jobs': 1, 'memories': 2,
                         'memory_evidence': 2}
        ids = _builder().IDS
        assert db.execute('SELECT superseded_by FROM memory_evidence WHERE memory_id=?', (ids['superseded'],)).fetchone() \
            == (ids['current'],)
        from pebble.memory_graph import GRAPH_DDL, migrate_supersedes
        db.executescript(GRAPH_DDL)
        with db:
            assert migrate_supersedes(db, time.time()) >= 1
        before = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} - epoch6_tables()
    home = tmp_path
    with contextlib.closing(sqlite3.connect(copy)) as db, db:
        db.execute("INSERT INTO memory_scope_salts VALUES('abc','" + 'a' * 64 + "','local',0)")
    problems, evidence = new_tables_problems(home, before)
    assert not problems and evidence['salts'] == 1 and evidence['supersedes_edges'] >= 1, (problems, evidence)


def test_new_tables_rule_refuses_any_other_startup_row(tmp_path):
    path = tmp_path / 'pebble.sqlite'
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE memory_evidence(memory_id TEXT, superseded_by TEXT)')
        db.execute("INSERT INTO memory_evidence VALUES('old','new')")
    before = tables(tmp_path)
    from pebble.memory_graph import GRAPH_DDL
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.executescript(GRAPH_DDL + 'CREATE TABLE delegation_history(scope TEXT);')
        db.execute("INSERT INTO memory_relations VALUES('new','old','supersedes','{\"basis\":\"superseded_by\"}',0)")
        db.execute("INSERT INTO memory_scope_salts VALUES('abc','" + 'b' * 64 + "','local',0)")
    assert new_tables_problems(tmp_path, before)[0] == {}
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO memory_relations VALUES('new','old','contradicts','{}',0)")
        db.execute("INSERT INTO delegation_history VALUES('x')")
    problems = new_tables_problems(tmp_path, before)[0]
    assert set(problems) == {'memory_relations', 'delegation_history'}, problems


def test_time_aware_comparison_lets_only_expired_memories_and_their_evidence_go(tmp_path):
    path = tmp_path / 'pebble.sqlite'
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE memories(id TEXT, kind TEXT, expires REAL)')
        db.execute('CREATE TABLE memory_evidence(memory_id TEXT, status TEXT)')
        db.executemany('INSERT INTO memories VALUES(?,?,?)', [('a', 'temporal', 1.0), ('b', 'semantic', None)])
        db.executemany('INSERT INTO memory_evidence VALUES(?,?)', [('a', 'active'), ('b', 'active')])
    with connect(path) as db:
        columns = {name: columns_of(db, PRESERVED[name][0]) for name in ('memories', 'memory_evidence')}
        before = snapshot(db, columns)
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM memories WHERE id='a'")
    with connect(path) as db:
        assert compare(db, ['memories', 'memory_evidence'], before, columns) == {}
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM memory_evidence WHERE memory_id='a'")
    with connect(path) as db:
        assert compare(db, ['memories', 'memory_evidence'], before, columns) == {}
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE memory_evidence SET status='contested' WHERE memory_id='b'")
    with connect(path) as db:
        assert set(compare(db, ['memories', 'memory_evidence'], before, columns)) == {'memory_evidence'}


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
        assert added_columns(db, columns) == {}
    with sqlite3.connect(path) as db:
        db.execute("UPDATE process_jobs SET jobdir='x'")
    with connect(path) as db:
        assert added_columns(db, columns)['process_jobs']['filled'] == 1


def test_source_upgrade_run_on_explicit_request(request, tmp_path):
    """The whole validator in source mode, as a subprocess with its scratch under tmp_path.

    It starts the actual service and the preserved Phase 14 executable, so it runs only when this
    node ID is named explicitly; the default collection skips it.
    """
    import pytest
    if not any(str(arg).endswith('::test_source_upgrade_run_on_explicit_request') for arg in request.config.args):
        pytest.skip('Starts the actual service; name this test explicitly to run it.')
    _need_epoch5_fixture()
    report, log = tmp_path / 'report.json', tmp_path / 'upgrade.log'
    with log.open('w') as sink:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--report', str(report)], cwd=ROOT,
                              env={**os.environ, 'TMPDIR': str(tmp_path)}, stdout=sink, stderr=subprocess.STDOUT,
                              timeout=300)
    outcome = json.loads(report.read_text())
    assert done.returncode == 0 and outcome['passed'] is True, json.dumps(
        {'failed': outcome['failed'], 'fatal': outcome['fatal_error'],
         'errors': [c.get('error') for c in outcome['check_results'] if not c['passed']]})
    assert outcome['kind'] == 'source' and len(outcome['checks']) == len(set(outcome['checks'])) == 19
    import warnings
    warnings.warn('validator result line: ' + log.read_text().strip().splitlines()[-1])  # shown in the -q summary


def test_older_fixture_upgrades_on_explicit_request(request, tmp_path):
    """The four older-fixture checks against the actual source service, without the epoch-5 fixture.

    Starts the actual service on private copies under tmp_path, so it runs only when this node ID is
    named explicitly; the default collection skips it.
    """
    import pytest
    if not any(str(arg).endswith('::test_older_fixture_upgrades_on_explicit_request') for arg in request.config.args):
        pytest.skip('Starts the actual service; name this test explicitly to run it.')
    upgrade = Upgrade([sys.executable, str(ROOT / 'backend/main.py')])
    for epoch, fixture, metadata in OLDER:
        upgrade.check(f'epoch-{epoch}', lambda e=epoch, f=fixture, m=metadata: upgrade.older(e, f, m, tmp_path))
    import warnings
    warnings.warn('older fixtures: ' + json.dumps([{k: c.get(k) for k in ('name', 'passed', 'evidence', 'error')}
                                                   for c in upgrade.checks])[:6000])
    assert all(c['passed'] for c in upgrade.checks), [c.get('error') for c in upgrade.checks if not c['passed']]


def test_dependent_checks_never_run_without_their_prerequisites():
    upgrade = Upgrade(['unused'])
    for name, fn in (('budget', upgrade.budget_refusal), ('discard', upgrade.discard_oldest),
                     ('refusal', upgrade.refusal), ('backup', upgrade.backup), ('second', upgrade.second_start),
                     ('core', lambda: upgrade.kept(CORE)), ('phase14', upgrade.kept_phase14),
                     ('additive', upgrade.additive), ('tables', upgrade.new_tables), ('upgrade', upgrade.upgrade)):
        assert upgrade.check(name, fn) is False and 'Prerequisite' in upgrade.checks[-1]['error'], name
    try:
        upgrade.check('refusal', upgrade.refusal)
    except AssertionError:
        pass
    else:
        raise AssertionError('A duplicate check name was accepted.')
