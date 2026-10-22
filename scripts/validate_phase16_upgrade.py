"""Actual Phase 15 epoch-6 fixture -> current service upgrade, older fixtures, and downgrade refusal.

Uses the parent-owned synthetic epoch-6 fixture (.runtime/phase16-epoch6-fixture,
built by .runtime/phase16-baseline/build_epoch6_fixture.py with the preserved
Phase 15 candidate) and that preserved Phase 15 executable. Never touches an
installed home, account, credential or model. Each mode upgrades its own private
copies under the system temporary folder, never under .runtime. The fixture's
manual synthetic rows (six memory layers, a settled run, Phase 12 process jobs,
output, inspection and stream rows, settled Phase 13 worktree, write, Git plan
receipt and environment rows, settled Phase 14 ledger slice, reservation and
supervised process job, a superseded learned memory pair, and the Phase 15
memory graph (a contested pair with claims and its contradicts edge, a tombstone
Bloom filter, an imported scope salt), delegation rounds and history, message
routes and token calibration samples) are preservation fixtures, not learned or
executed evidence.

The epoch-6 fixture retains three upgrade backups (epochs 3->4, 4->5, 5->6),
which is migrations.Limits.max_backups including the new one. The service must
therefore wait in upgrade maintenance (pebble.maintenance) and change nothing,
serving only its maintenance routes. The validator then removes the oldest backup
through the native upgrade_discard_backup route, as a person would in the app,
confirmed by the manifest hash the service lists (cross-checked with the
fixture's recorded hash), and retries startup in place with upgrade_retry.
Nothing is deleted automatically. Preserved older fixtures whose budget is also
full (epochs 4 and 5) go through the same native route.

Time-aware preservation (user decision, 2026-10-12): Pebble deletes expired memories, and the migration
layer permits exactly that (migrations.PRESERVED 'memories'). So every row survives exactly, except that
expired memories may go, together with the learning evidence, claims and graph edges of a gone memory.
The epoch-5 and epoch-6 synthetic memories expire years after their builds or never; the older fixtures'
shared temporal memory expired at 2026-10-12 12:48. This validator also upgrades each preserved older
fixture (epochs 1-5), verified against its recorded metadata first, straight to the current epoch.

The current epoch is never pinned: every comparison uses migrations.SCHEMA_EPOCH and the fixture's own
recorded epoch, so this validator keeps its subject in later phases. The Phase 16 tables are read from the
backend sources (source_ledger.TABLES, research_store.TABLES, screen_session.TABLES and the
connector_effects DDL); they, the
Phase 15 tables and every table startup must not fill (migrations.NO_NEW) must be empty when they are new
since a fixture's epoch, except that the memory graph may hold this installation's local scope salt and
the supersedes edges migrated from memory_evidence.superseded_by. Other new tables are listed, not judged.
Every preserved table is compared through the exact column list it had before the upgrade; a column added
to a preserved table must be empty on every preserved row.

Checks are recorded independently by name; a failure never stops later
independent checks and a report is written either way. --report must be a new
file. The Phase 15 executable runs only when its SHA-256 matches the preserved
candidate, and only against the private copy.

Usage:
  python scripts/validate_phase16_upgrade.py --report PATH [--backend PATH]
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
from pebble.migrations import BACKUPS, NO_NEW, SCHEMA_EPOCH, Limits, discard_backup


OLD = ROOT / '.runtime/phase15-candidate/pebble-backend/pebble-backend'
FIXTURE = ROOT / '.runtime/phase16-epoch6-fixture'
METADATA = ROOT / '.runtime/phase16-epoch6-fixture.json'
BUILDER = ROOT / '.runtime/phase16-baseline/build_epoch6_fixture.py'
# The epoch-3, 4 and 5 metadata record the synthetic Phase 12, 13 and 14 rows the epoch-6 fixture inherited.
PRIOR_METADATA = (ROOT / '.runtime/phase13-epoch3-fixture.json', ROOT / '.runtime/phase14-epoch4-fixture.json',
                  ROOT / '.runtime/phase15-epoch5-fixture.json')
SOURCE_FIXTURE = '.runtime/phase15-epoch5-fixture'
EXPECTED_OLD = '046c97994822f8ad20cc2b7f4057ebdd3dc090de0ceffa20c2498f501ff365a8'
FROM_EPOCH = 6  # the fixture's own epoch, never the current one
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
# The epoch-6 tables (epoch6_tables() reads the same names from the sources; a self-check keeps them equal).
PHASE15 = {'memory_relations': ('memory_relations', 'from_id,to_id,relation', ''),
           'memory_claims': ('memory_claims', 'memory_id', ''),
           'memory_tombstone_blooms': ('memory_tombstone_blooms', 'scope', ''),
           'memory_scope_salts': ('memory_scope_salts', 'salt_id', ''),
           'agent_rounds': ('agent_rounds', 'agent_id,generation,call_id', ''),
           'delegation_history': ('delegation_history', 'scope,event', ''),
           'delegation_salt': ('delegation_salt', 'id', ''),
           'message_routes': ('message_routes', 'message_id', ''),
           'token_calibration': ('token_calibration', 'route,sequence', '')}
PRESERVED = {**CORE, **PHASE12, **PHASE13, **PHASE14, **LEARNING, **PHASE15}
# Rows of a gone memory that may go with it, or stay until the next maintenance pass; nothing else changes.
DEPENDENT = frozenset({'memory_evidence', 'memory_claims', 'memory_relations'})
# Rows the epoch-6 graph may hold right after an upgrade from an older epoch (see the module docstring).
GRAPH_ROWS = frozenset({'memory_scope_salts', 'memory_relations'})
HISTORY = 'SELECT backup_id,from_version,to_version,completed FROM upgrade_history ORDER BY id'
# Preserved older fixtures, upgraded straight to the current epoch (time-aware; see the module docstring).
OLDER = ((1, ROOT / '.runtime/phase11-epoch1-fixture', ROOT / '.runtime/phase11-epoch1-fixture.json'),
         (2, ROOT / '.runtime/phase12-epoch2-fixture', ROOT / '.runtime/phase12-epoch2-fixture.json'),
         (3, ROOT / '.runtime/phase13-epoch3-fixture', ROOT / '.runtime/phase13-epoch3-fixture.json'),
         (4, ROOT / '.runtime/phase14-epoch4-fixture', ROOT / '.runtime/phase14-epoch4-fixture.json'),
         (5, ROOT / '.runtime/phase15-epoch5-fixture', ROOT / '.runtime/phase15-epoch5-fixture.json'))
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
    """Names the Phase 15 epoch-6 DDL creates, read from the backend sources."""
    from pebble import agent_tree, delegation_history, memory_graph, store, token_calibration
    ddl = (memory_graph.GRAPH_DDL + agent_tree.ROUNDS_DDL + delegation_history.HISTORY_DDL + store.ROUTES_DDL
           + token_calibration.DDL)
    return frozenset(CREATED.findall(ddl))


def epoch7_tables():
    """Names the Phase 16 sources declare: the research source ledger and its kept claim rows, screen
    sessions and their acts, and the connector effect journal with its native confirmations (never a
    hard-coded epoch)."""
    from pebble import connector_effects, research_store, screen_session, source_ledger
    effects = getattr(connector_effects, 'TABLES', None) or CREATED.findall(';'.join(connector_effects.DDL))
    return (frozenset(source_ledger.TABLES) | frozenset(research_store.TABLES) | frozenset(screen_session.TABLES)
            | frozenset(effects))


def judged_tables():
    """New tables that must be empty right after an upgrade (except the allowed graph rows)."""
    return epoch6_tables() | epoch7_tables() | frozenset(NO_NEW)


def expected_rows(name, before, columns, survivors):
    """Time-aware expectation: expired memories, and the evidence, claims and edges of a gone memory, may go."""
    rows = before[name]
    if name == 'memories':
        at = columns[name].index('expires')
        now = time.time()
        return [row for row in rows if row[at] is None or row[at] > now]
    if name in ('memory_evidence', 'memory_claims'):
        at = columns[name].index('memory_id')
        return [row for row in rows if row[at] in survivors]
    if name == 'memory_relations':
        a, b = columns[name].index('from_id'), columns[name].index('to_id')
        return [row for row in rows if row[a] in survivors and row[b] in survivors]
    return rows


def compare(db, names, before, columns):
    """{name: problem} for preserved tables whose rows changed beyond the time-aware rule."""
    at = columns['memories'].index('id') if 'memories' in columns else None
    survivors = {row[at] for row in expected_rows('memories', before, columns, None)} if at is not None else set()
    problems = {}
    for name in names:
        actual = db.execute(statement(name, columns)).fetchall()
        expected = expected_rows(name, before, columns, survivors)
        if name in DEPENDENT:
            # Rows of a gone memory may stay until the next maintenance pass, or go now; nothing else changes.
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
    """Judged tables new since the fixture's epoch: empty except the allowed graph rows. Other new tables
    (such as earlier subsystems' *_meta version rows gained by older fixtures) are listed, not judged."""
    checked = judged_tables() if checked is None else checked
    with connect(home / 'pebble.sqlite') as db:
        rows = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        new = {name: sql or '' for name, sql in rows if name not in before_tables}
        virtual = [name for name, sql in new.items() if sql.upper().startswith('CREATE VIRTUAL TABLE')]
        shadows = {name for name in new for v in virtual if name.startswith(v + '_')}
        counts = {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in new if name not in shadows}
        problems = {name: count for name, count in counts.items() if count and name in checked and name not in GRAPH_ROWS}
        unjudged = {name: count for name, count in counts.items() if count and name not in checked}
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
    return problems, {'new_tables': sorted(set(new) - shadows), 'salts': len(salts), 'supersedes_edges': len(edges),
                      'unjudged_rows': unjudged}


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
        self.check('fixture metadata, file hashes and preserved Phase 15 executable verified', self.verify_fixture)
        shutil.copytree(FIXTURE, home)
        self.facts['home'] = home
        self.check('private copy holds the six memory layers and the synthetic Phase 12, 13, 14 and 15 rows at epoch 6',
                   self.copy_holds_rows)
        self.check('actual service waits in upgrade maintenance while the backup budget is full and changes nothing',
                   self.budget_refusal)
        self.check('oldest retained backup removed through the native maintenance route with its manifest hash',
                   self.discard_oldest)
        self.check('native retry upgrades the private epoch-6 copy to the current epoch once', self.upgrade)
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
        self.check('synthetic Phase 15 memory graph, delegation rounds and history, message routes and calibration '
                   'samples survive upgrade exactly', self.kept_phase15)
        self.check('preserved tables keep their columns; any added column is empty on preserved rows', self.additive)
        self.check('Phase 16 research, screen and connector effect tables exist after the upgrade and are empty, '
                   'as is every new table startup must not fill', self.new_tables)
        self.check('verified pre-upgrade backup is epoch 6 and retains the exact preserved rows', self.backup)
        self.check('second current-service startup creates no new upgrade or backup', self.second_start)
        self.check('preserved Phase 15 executable refuses the current epoch before mutating the database', self.refusal)
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
            for name, count in record['synthetic_rows'].items():
                prior[name] = prior.get(name, 0) + count
        assert (metadata['synthetic_only'] is True and metadata['credentials_used'] is False
                and metadata['inference_calls'] == 0), 'The fixture is not marked synthetic and credential-free.'
        assert metadata['source_fixture'] == SOURCE_FIXTURE
        assert metadata['source_metadata_sha256'] == sha(PRIOR_METADATA[-1]), 'The epoch-5 record changed.'
        assert metadata['epoch'] == FROM_EPOCH and SCHEMA_EPOCH > FROM_EPOCH, (metadata['epoch'], SCHEMA_EPOCH)
        assert metadata['old_executable_sha256'] == EXPECTED_OLD and sha(OLD) == EXPECTED_OLD, \
            'The preserved Phase 15 executable does not match its recorded SHA-256.'
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
        self.facts['prior_ids'] = json.loads(PRIOR_METADATA[-1].read_text())['synthetic_ids']
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
        # Phase 12-14 rows are counted by the earlier records; Phase 15 rows by this one (only it adds them).
        inherited = {name: len(before[name]) for name in prior if name in before}
        assert all(inherited[name] >= prior[name] for name in inherited), (inherited, prior)
        added = metadata['synthetic_rows']
        # Every epoch-6 table holds rows, so the comparison after the upgrade is never vacuous.
        assert all(before[name] and len(before[name]) >= added.get(name, 0) for name in PHASE15), \
            {name: len(before[name]) for name in PHASE15}
        ids, prior_ids = metadata['synthetic_ids'], self.need('prior_ids')
        job = dict(zip(columns['process_jobs'], next(r for r in before['process_jobs'] if r[0] == prior_ids['process'])))
        assert job['contained'] == 1 and job['settled_by'] == 'supervisor_record' and job['reservation_id'] == prior_ids['reservation']
        evidence = {r[0]: dict(zip(columns['memory_evidence'], r)) for r in before['memory_evidence']}
        assert evidence[prior_ids['superseded']]['superseded_by'] == prior_ids['current'], 'The superseded memory link is missing.'
        assert evidence[ids['fact_old']]['status'] == evidence[ids['fact_new']]['status'] == 'contested'
        assert not epoch7_tables() & present, 'An epoch-6 database already has Phase 16 tables.'
        return {'memory_layers': sorted(LAYERS), 'inherited_rows': inherited,
                'phase15_rows': {name: len(before[name]) for name in PHASE15},
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
        home, columns, ids = self.need('home'), self.need('columns'), self.need('prior_ids')
        with connect(home / 'pebble.sqlite') as db:
            row = db.execute(f'SELECT {",".join(columns["process_jobs"])} FROM process_jobs WHERE id=?',
                             (ids['process'],)).fetchone()
        job = dict(zip(columns['process_jobs'], row))
        assert (job['state'], job['contained'], job['settled_by'], job['plan_hash']) == (
            'succeeded', 1, 'supervisor_record', 'e' * 64), 'The supervised process job changed.'
        return {**evidence, 'supervised_job_kept': True}

    def kept_phase15(self):
        """The epoch-6 rows survive exactly; the contest, its edge, both salts and the routes stay as they were."""
        evidence = self.kept(PHASE15)
        home, ids = self.need('home'), self.need('metadata')['synthetic_ids']
        with connect(home / 'pebble.sqlite') as db:
            statuses = dict(db.execute('SELECT memory_id,status FROM memory_evidence WHERE memory_id IN (?,?)',
                                       (ids['fact_old'], ids['fact_new'])).fetchall())
            edge = db.execute("SELECT evidence FROM memory_relations WHERE from_id=? AND to_id=? AND relation='contradicts'",
                              (ids['fact_new'], ids['fact_old'])).fetchone()
            origins = sorted(origin for (origin,) in db.execute('SELECT origin FROM memory_scope_salts'))
            routes = dict(db.execute('SELECT message_id,route_class FROM message_routes WHERE message_id IN (?,?)',
                                     (ids['user_message'], ids['assistant_message'])).fetchall())
            rounds = db.execute('SELECT kind FROM agent_rounds WHERE agent_id=? ORDER BY kind', (ids['agent'],)).fetchall()
        assert statuses == {ids['fact_old']: 'contested', ids['fact_new']: 'contested'}, statuses
        assert edge and not json.loads(edge[0]).get('resolution'), 'The open contradicts edge changed.'
        assert origins == ['imported', 'local'], origins
        assert set(routes.values()) == {'device'} and len(routes) == 2, routes
        assert rounds == [('review',), ('steer',)], rounds
        return {**evidence, 'contested_pair_kept': True, 'salt_origins': origins}

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
        expected = epoch7_tables()
        present = tables(home)
        assert expected <= present, sorted(expected - present)
        problems, evidence = new_tables_problems(home, before)
        assert not problems, problems
        assert expected <= set(evidence['new_tables']), 'A Phase 16 table existed before the upgrade.'
        return {**evidence, 'phase16_tables': sorted(expected)}

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
            assert not names & epoch7_tables(), 'The pre-upgrade backup already holds Phase 16 tables.'
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
        assert refused.returncode != 0, 'The Phase 15 executable accepted a newer epoch.'
        assert REFUSAL in refused.stderr, refused.stderr[-600:]
        after = (sha(home / 'pebble.sqlite'), wal(home), backups(home), read(home, HISTORY))
        assert after == before, 'The Phase 15 executable changed the database, its WAL, backups or upgrade history.'
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
        current = epoch6_tables() | epoch7_tables()
        assert current <= tables(home), sorted(current - tables(home))
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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase16-upgrade-')).resolve()
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
                  'scripts/validate_phase16_upgrade.py', 'scripts/validate_runtime.py', 'backend/pebble/migrations.py',
                  'backend/pebble/memory_graph.py', 'backend/pebble/memory_learning.py', 'backend/pebble/maintenance.py',
                  'backend/pebble/source_ledger.py', 'backend/pebble/research_store.py', 'backend/pebble/screen_session.py',
                  'backend/pebble/connector_effects.py', 'backend/pebble/app.py')},
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
# pytest's tmp_path. Neither the service nor the Phase 15 executable is started.
# Checks that need the epoch-6 fixture skip, naming the reason, until the parent
# has built it with the preserved Phase 15 candidate.

CHECK_COUNT = 16 + len(OLDER)


def _need_epoch6_fixture():
    import pytest
    if not (FIXTURE.is_dir() and METADATA.is_file()):
        pytest.skip('The parent has not built .runtime/phase16-epoch6-fixture yet (build_epoch6_fixture.py).')


def _builder():
    import importlib.util
    spec = importlib.util.spec_from_file_location('build_epoch6_fixture', BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _epoch6_copy(tmp_path):
    """The real epoch-5 database plus the source epoch-6 DDL, a local salt and the migrated supersedes edges:
    what the Phase 15 candidate's startup makes of the epoch-5 fixture before the builder's inserts."""
    from pebble import agent_tree, delegation_history, memory_graph, store, token_calibration
    copy = tmp_path / 'pebble.sqlite'
    with connect(OLDER[-1][1] / 'pebble.sqlite', immutable=True) as source, contextlib.closing(sqlite3.connect(copy)) as db:
        source.backup(db)
        db.executescript(memory_graph.GRAPH_DDL + agent_tree.ROUNDS_DDL + delegation_history.HISTORY_DDL
                         + store.ROUTES_DDL + token_calibration.DDL)
        with db:
            salt = memory_graph.new_salt()
            db.execute("INSERT INTO memory_scope_salts VALUES(?,?,'local',?)", (memory_graph.salt_id(salt), salt, time.time()))
            memory_graph.migrate_supersedes(db, time.time())
    return copy


def test_parent_fixture_and_preserved_executable_verify():
    _need_epoch6_fixture()
    upgrade = Upgrade(['unused'])
    assert upgrade.check('fixture', upgrade.verify_fixture), upgrade.checks[-1].get('error')
    assert upgrade.facts['old_verified'] is True and upgrade.facts['metadata']['epoch'] == FROM_EPOCH
    assert set(upgrade.facts['metadata']['synthetic_rows']) >= set(PHASE15)


def test_private_copy_holds_the_preserved_rows(tmp_path):
    _need_epoch6_fixture()
    upgrade = Upgrade(['unused'])
    assert upgrade.check('fixture', upgrade.verify_fixture), upgrade.checks[-1].get('error')
    home = tmp_path / 'home'
    shutil.copytree(FIXTURE, home)
    upgrade.facts['home'] = home
    assert upgrade.check('copy', upgrade.copy_holds_rows), upgrade.checks[-1].get('error')
    assert upgrade.facts['history_before'][-1][1:3] == (5, FROM_EPOCH)
    assert sum(name.endswith('.sqlite') for name in backups(home)) >= Limits().max_backups
    assert sha(home / 'pebble.sqlite') == upgrade.facts['metadata']['files_sha256']['pebble.sqlite'], 'A read changed the copy.'


def test_explicit_discard_needs_the_listed_manifest_hash(tmp_path):
    _need_epoch6_fixture()
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
    # The epoch-4 and epoch-5 budgets are full, so their upgrades go through the native maintenance route.
    for fixture in (OLDER[-2][1], OLDER[-1][1]):
        assert sum(name.endswith('.sqlite') for name in backups(fixture)) >= Limits().max_backups


def test_table_names_match_the_backend_sources():
    from pebble import connector_effects, migrations, research_store, screen_session, source_ledger
    phase15, phase16 = epoch6_tables(), epoch7_tables()
    assert set(PHASE15) == phase15
    assert phase15 == {'memory_relations', 'memory_claims', 'memory_tombstone_blooms', 'memory_scope_salts',
                       'agent_rounds', 'delegation_history', 'delegation_salt', 'message_routes', 'token_calibration'}
    # The declared names are exactly what the Phase 16 DDL creates.
    ddl = (source_ledger.RESEARCH_DDL + research_store.DDL + screen_session.SCREEN_DDL
           + ';'.join(connector_effects.DDL))
    assert phase16 == set(CREATED.findall(ddl)), sorted(phase16 ^ set(CREATED.findall(ddl)))
    assert {'research_sources', 'research_claim_rows', 'screen_sessions', 'connector_effects',
            'connector_effect_confirmations'} <= phase16 and len(phase16) == 10, sorted(phase16)
    # The parent's epoch-7 migration creates, preserves and guards every one of them.
    assert phase15 | phase16 <= set(CREATED.findall(migrations.REFERENCE_DDL))
    assert all(name in migrations.PRESERVED for name in phase15 | phase16)
    assert phase16 <= set(migrations.NO_NEW) and GRAPH_ROWS <= phase15 and not GRAPH_ROWS & set(migrations.NO_NEW)
    assert SCHEMA_EPOCH > FROM_EPOCH  # the subject of this validator; never pinned to one number


def test_builder_helpers_match_the_backend():
    from pebble import delegation_history, memory_graph, model_handoff, token_calibration
    builder = _builder()
    salt = memory_graph.new_salt()
    assert builder.salt_id(salt) == memory_graph.salt_id(salt)
    assert builder.project_scope(builder.PROJECT, salt) == memory_graph.scope_key(builder.PROJECT, salt)
    kind, slot, fingerprint = builder.FORGOTTEN
    items = builder.tombstone_items(kind, slot, fingerprint)
    assert items == memory_graph.items(kind, [slot], fingerprint)
    assert builder.bloom(items) == memory_graph.bloom_add(None, items)
    assert (builder.BLOOM_BITS, builder.BLOOM_HASHES) == (memory_graph.BLOOM_BITS, memory_graph.BLOOM_HASHES)
    assert builder.delegation_key('s', 'run', 'r') == delegation_history.DelegationHistory._key('s', 'run', 'r')
    assert builder.route_fingerprint(builder.CALIBRATION_ROUTE) == token_calibration.route_fingerprint(builder.CALIBRATION_ROUTE)
    assert model_handoff.CLASS.fullmatch('device')
    assert set(builder.EPOCH6) == epoch6_tables()
    assert builder.EXPECTED_OLD == EXPECTED_OLD and builder.OUT == FIXTURE and builder.META == METADATA


def test_builder_rows_fit_the_real_epoch_6_schema(tmp_path):
    """The builder's inserts on what the candidate makes of the real epoch-5 database, then the time
    consolidation would find nothing to change and the epoch-7 new-table rule holds."""
    from pebble import delegation_history, memory_graph, migrations, token_calibration
    builder = _builder()
    copy = _epoch6_copy(tmp_path)
    now = time.time()
    with contextlib.closing(sqlite3.connect(copy)) as db:
        with db:
            added = builder.insert_rows(db, now)
        assert added == {'memories': 2, 'memory_evidence': 2, 'memory_claims': 3, 'memory_relations': 1,
                         'memory_tombstone_blooms': 1, 'memory_scope_salts': 1, 'agent_rounds': 2, 'delegation_salt': 1,
                         'delegation_history': 2, 'messages': 2, 'message_routes': 2, 'token_calibration': 3}, added
        before = builder.rows_of(db)
        ids = builder.IDS
        # The idle consolidation steps (relations, graph backfill, tombstones) and expiry change nothing.
        with db:
            assert memory_graph.migrate_supersedes(db, now) == 0
            assert memory_graph.collect_garbage(db, now, 64) == 0
            assert memory_graph.backfill(db, now, 64) == 0
            assert memory_graph.settle(db, [ids['fact_old'], ids['fact_new']], now) == {}
        assert builder.rows_of(db) == before
        assert memory_graph.withheld(db, [ids['fact_old'], ids['fact_new']]) == {
            ids['fact_old']: 'contested', ids['fact_new']: 'contested'}
        # The Bloom filter forgets the synthetic tombstone under the local salt's project key.
        local = db.execute("SELECT salt FROM memory_scope_salts WHERE origin='local'").fetchone()[0]
        kind, slot, fingerprint = builder.FORGOTTEN
        assert memory_graph.bloom_forgotten(db, [memory_graph.scope_key(builder.PROJECT, local)], kind, [slot], fingerprint)
        # Delegation advice reads the synthetic history for the run's own scope.
        rid, raw = db.execute("SELECT id,config FROM runs WHERE state='completed' ORDER BY id LIMIT 1").fetchone()
        config = json.loads(raw)
        scope = delegation_history.DelegationHistory._key(builder.DELEGATION_SALT, 'scope', config.get('workspace') or '',
                                                          json.dumps(config.get('workspace_identity')))
        assert db.execute('SELECT count(*) FROM delegation_history WHERE scope=?', (scope,)).fetchone() == (2,)
        route = token_calibration.route_fingerprint(builder.CALIBRATION_ROUTE)
        assert db.execute(token_calibration.SELECT_SQL, (route,)).fetchall() == list(builder.SAMPLES)
        assert db.execute('SELECT count(*) FROM memory_claims c JOIN memory_evidence e ON e.memory_id=c.memory_id '
                          "WHERE e.status IN ('active','contested')").fetchone()[0] == 3
        before_tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not epoch7_tables() & before_tables
        from pebble import connector_effects, research_store, screen_session, source_ledger
        db.executescript(source_ledger.RESEARCH_DDL + research_store.DDL + screen_session.SCREEN_DDL
                         + ';\n'.join(connector_effects.DDL) + ';')
        assert set(migrations.KINDS) <= LAYERS  # the memories table still holds only the six layers
    problems, evidence = new_tables_problems(tmp_path, before_tables)
    assert not problems and set(evidence['new_tables']) == set(epoch7_tables()), (problems, evidence)


def test_new_tables_rule_refuses_any_other_startup_row(tmp_path):
    path = tmp_path / 'pebble.sqlite'
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE memory_evidence(memory_id TEXT, superseded_by TEXT)')
        db.execute("INSERT INTO memory_evidence VALUES('old','new')")
    before = tables(tmp_path)
    from pebble.memory_graph import GRAPH_DDL
    from pebble.screen_session import SCREEN_DDL
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.executescript(GRAPH_DDL + SCREEN_DDL + 'CREATE TABLE delegation_history(scope TEXT);'
                         'CREATE TABLE goals_meta(version INTEGER);')
        db.execute("INSERT INTO memory_relations VALUES('new','old','supersedes','{\"basis\":\"superseded_by\"}',0)")
        db.execute("INSERT INTO memory_scope_salts VALUES('abc','" + 'b' * 64 + "','local',0)")
        db.execute('INSERT INTO goals_meta VALUES(1)')
    problems, evidence = new_tables_problems(tmp_path, before)
    assert problems == {} and evidence['unjudged_rows'] == {'goals_meta': 1}, (problems, evidence)
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO memory_relations VALUES('new','old','contradicts','{}',0)")
        db.execute("INSERT INTO delegation_history VALUES('x')")
        db.execute("INSERT INTO screen_sessions VALUES('s','c','r','app','t','" + 'a' * 64 + "','p','[]','active',"
                   "NULL,0,1,0)")
    problems = new_tables_problems(tmp_path, before)[0]
    assert set(problems) == {'memory_relations', 'delegation_history', 'screen_sessions'}, problems


def test_time_aware_comparison_lets_only_rows_of_expired_memories_go(tmp_path):
    path = tmp_path / 'pebble.sqlite'
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE memories(id TEXT, kind TEXT, expires REAL)')
        db.execute('CREATE TABLE memory_evidence(memory_id TEXT, status TEXT)')
        db.execute('CREATE TABLE memory_claims(memory_id TEXT, value TEXT)')
        db.execute('CREATE TABLE memory_relations(from_id TEXT, to_id TEXT, relation TEXT)')
        db.executemany('INSERT INTO memories VALUES(?,?,?)', [('a', 'temporal', 1.0), ('b', 'semantic', None),
                                                              ('c', 'semantic', time.time() + 86400 * 3650)])
        db.executemany('INSERT INTO memory_evidence VALUES(?,?)', [('a', 'active'), ('b', 'active')])
        db.executemany('INSERT INTO memory_claims VALUES(?,?)', [('a', 'x'), ('b', 'y')])
        db.executemany('INSERT INTO memory_relations VALUES(?,?,?)', [('b', 'a', 'contradicts'), ('c', 'b', 'supersedes')])
    names = ['memories', 'memory_evidence', 'memory_claims', 'memory_relations']
    with connect(path) as db:
        columns = {name: columns_of(db, PRESERVED[name][0]) for name in names}
        before = snapshot(db, columns)
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM memories WHERE id='a'")
    with connect(path) as db:
        assert compare(db, names, before, columns) == {}
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        for table, column in (('memory_evidence', 'memory_id'), ('memory_claims', 'memory_id'),
                              ('memory_relations', 'to_id')):
            db.execute(f"DELETE FROM {table} WHERE {column}='a'")
    with connect(path) as db:
        assert compare(db, names, before, columns) == {}
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("DELETE FROM memory_relations WHERE from_id='c'")
        db.execute("UPDATE memory_evidence SET status='contested' WHERE memory_id='b'")
    with connect(path) as db:
        assert set(compare(db, names, before, columns)) == {'memory_evidence', 'memory_relations'}


def test_comparison_through_pre_upgrade_columns_ignores_only_additive_columns(tmp_path):
    path = tmp_path / 'pebble.sqlite'
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE process_jobs(id TEXT PRIMARY KEY, state TEXT)')
        db.execute("INSERT INTO process_jobs VALUES('a','succeeded')")
    with connect(path) as db:
        columns = {'process_jobs': columns_of(db, 'process_jobs')}
        before = db.execute(statement('process_jobs', columns)).fetchall()
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute('ALTER TABLE process_jobs ADD COLUMN jobdir TEXT')
    with connect(path) as db:
        assert db.execute(statement('process_jobs', columns)).fetchall() == before
        assert added_columns(db, columns) == {}
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE process_jobs SET jobdir='x'")
    with connect(path) as db:
        assert added_columns(db, columns)['process_jobs']['filled'] == 1


def test_check_names_are_identical_in_both_modes(monkeypatch, tmp_path):
    class Names(Upgrade):
        def check(self, name, fn):  # records the name only; nothing runs
            assert name not in self.names, name
            self.names.add(name)
            self.checks.append({'name': name})
            return True

    monkeypatch.setattr(shutil, 'copytree', lambda *unused, **ignored: None)
    lists = []
    for command in ([sys.executable, str(ROOT / 'backend/main.py')], ['/frozen/pebble-backend']):
        upgrade = Names(command)
        upgrade.run(tmp_path / 'home')
        lists.append([c['name'] for c in upgrade.checks])
    assert lists[0] == lists[1] and len(lists[0]) == len(set(lists[0])) == CHECK_COUNT


def test_source_upgrade_run_on_explicit_request(request, tmp_path):
    """The whole validator in source mode, as a subprocess with its scratch under tmp_path.

    It starts the actual service and the preserved Phase 15 executable, so it runs only when this
    node ID is named explicitly; the default collection skips it.
    """
    import pytest
    if not any(str(arg).endswith('::test_source_upgrade_run_on_explicit_request') for arg in request.config.args):
        pytest.skip('Starts the actual service; name this test explicitly to run it.')
    _need_epoch6_fixture()
    report, log = tmp_path / 'report.json', tmp_path / 'upgrade.log'
    with log.open('w') as sink:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--report', str(report)], cwd=ROOT,
                              env={**os.environ, 'TMPDIR': str(tmp_path)}, stdout=sink, stderr=subprocess.STDOUT,
                              timeout=400)
    outcome = json.loads(report.read_text())
    assert done.returncode == 0 and outcome['passed'] is True, json.dumps(
        {'failed': outcome['failed'], 'fatal': outcome['fatal_error'],
         'errors': [c.get('error') for c in outcome['check_results'] if not c['passed']]})
    assert outcome['kind'] == 'source' and len(outcome['checks']) == len(set(outcome['checks'])) == CHECK_COUNT
    import warnings
    warnings.warn('validator result line: ' + log.read_text().strip().splitlines()[-1])  # shown in the -q summary


def test_older_fixture_upgrades_on_explicit_request(request, tmp_path):
    """The five older-fixture checks against the actual source service, without the epoch-6 fixture.

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
                                                   for c in upgrade.checks])[:8000])
    assert all(c['passed'] for c in upgrade.checks), [c.get('error') for c in upgrade.checks if not c['passed']]


def test_dependent_checks_never_run_without_their_prerequisites():
    upgrade = Upgrade(['unused'])
    for name, fn in (('budget', upgrade.budget_refusal), ('discard', upgrade.discard_oldest),
                     ('refusal', upgrade.refusal), ('backup', upgrade.backup), ('second', upgrade.second_start),
                     ('core', lambda: upgrade.kept(CORE)), ('phase14', upgrade.kept_phase14),
                     ('phase15', upgrade.kept_phase15), ('additive', upgrade.additive),
                     ('tables', upgrade.new_tables), ('upgrade', upgrade.upgrade)):
        assert upgrade.check(name, fn) is False and 'Prerequisite' in upgrade.checks[-1]['error'], name
    try:
        upgrade.check('refusal', upgrade.refusal)
    except AssertionError:
        pass
    else:
        raise AssertionError('A duplicate check name was accepted.')
