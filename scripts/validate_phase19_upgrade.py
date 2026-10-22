"""Actual Phase 18 epoch-8 fixture -> current service upgrade, older fixtures, restart and downgrade refusal.

Uses the parent-owned synthetic epoch-8 fixture (.runtime/phase19-epoch8-fixture, built by
.runtime/phase19-baseline/build_epoch8_fixture.py with the preserved Phase 18 candidate) and that
preserved Phase 18 executable. Never touches an installed home, account, credential or model. Each run
upgrades private copies under the system temporary folder, never under .runtime.

The epoch-8 fixture is the epoch-7 fixture after the Phase 18 candidate's own 7 -> 8 upgrade (its
epoch-7 in-flight screen and connector rows already settled fail-closed), plus settled synthetic
Phase 17 rows (lifecycle, notifications, exports, goals), two personal settings and the artifact chains
Phase 19 reads differently: two keyed v1 quizzes (one updated, then stopped), a static html view that
stored a script before Phase 19 refused new ones, and a table. Two rows are in flight, as a crash leaves
them: an export render and an export save.

It retains three upgrade backups (epochs 5->6, 6->7 and 7->8), migrations.Limits.max_backups, so the
service must wait in upgrade maintenance, change nothing and serve only its maintenance routes until
the validator removes the oldest backup through the native upgrade_discard_backup route (confirmed by
the manifest hash the service lists, cross-checked with the fixture record) and retries in place.

Preservation is time-aware and recovery-aware, as in the Phase 17 upgrade validator: expired memories
(and the evidence, claims and edges of a gone memory) may go; the interrupted export render may only
become failed and the interrupted save uncertain, each with a later updated time; every other row
survives exactly through the column list it had before the upgrade, a column added to a preserved table
stays empty on preserved rows, and a table startup must not fill (migrations.NO_NEW) gains no row.
Artifact chains survive byte for byte; the v1 quizzes stay readable as labelled self-checks and no key
is moved into the host's quiz tables. The Phase 19 tables are read from the backend sources
(quiz_grading and view_bindings) and must be new and empty after the upgrade. An upgraded database,
given settled synthetic Phase 19 rows, then restarts without another upgrade, backup or row change.
The preserved epoch-1 to epoch-7 fixtures, verified against their records, also upgrade straight to the
current epoch.

The current epoch is never pinned. Until the parent has built the epoch-8 fixture, or while the
current source is still at the fixture's epoch (the parent's epoch-9 migration patch), every check that
needs them reports pending with that reason and the run does not pass. --report must be a new file. The
Phase 18 executable runs only when its SHA-256 matches the preserved candidate, and only against a
private copy.

Usage:
  python scripts/validate_phase19_upgrade.py --report PATH [--backend PATH]
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import validate_phase17_upgrade as p17u
from validate_runtime import ROOT, Service
sys.path.insert(0, str(ROOT / 'backend'))
from pebble import migrations  # noqa: E402
from pebble.migrations import BACKUPS, NO_NEW, SCHEMA_EPOCH, Limits  # noqa: E402

OLD = ROOT / '.runtime/phase18-candidate/pebble-backend/pebble-backend'
FIXTURE = ROOT / '.runtime/phase19-epoch8-fixture'
METADATA = ROOT / '.runtime/phase19-epoch8-fixture.json'
BUILDER = ROOT / '.runtime/phase19-baseline/build_epoch8_fixture.py'
# The epoch-3 to 7 records count the synthetic Phase 12 to 16 rows the epoch-8 fixture inherited.
PRIOR_METADATA = (ROOT / '.runtime/phase13-epoch3-fixture.json', ROOT / '.runtime/phase14-epoch4-fixture.json',
                  ROOT / '.runtime/phase15-epoch5-fixture.json', ROOT / '.runtime/phase16-epoch6-fixture.json',
                  ROOT / '.runtime/phase17-epoch7-fixture.json')
SOURCE_FIXTURE = '.runtime/phase17-epoch7-fixture'
EXPECTED_OLD = 'a6f8749a165ca09c5bce57abe012923a03d0b1c43cf15e84e3c0e613ea6e0f9a'  # reports/harness-candidate-manifest.json
FROM_EPOCH = 8  # the fixture's own epoch, never the current one
PENDING = ('The epoch-8 fixture (.runtime/phase19-epoch8-fixture and its .json record) does not exist yet; the parent '
           'builds it with .runtime/phase19-baseline/build_epoch8_fixture.py and the preserved Phase 18 candidate.')
PENDING_MIGRATION = ('Parent patch: migrations.py epoch 9 (SCHEMA_EPOCH above 8, the Phase 19 tables in REFERENCE_DDL, '
                     'PRESERVED and NO_NEW). The current source is at epoch %d, the fixture\'s own epoch.' % SCHEMA_EPOCH)
PENDING_TABLES = 'B2: the Phase 19 table DDL (quiz_grading.DDL and TABLES) is absent from the backend sources.'
REFUSAL = p17u.REFUSAL
SIDECARS = p17u.SIDECARS
LAYERS = p17u.LAYERS
HISTORY = p17u.HISTORY
# Preserved older fixtures, upgraded straight to the current epoch.
OLDER = (*p17u.OLDER, (7, ROOT / '.runtime/phase17-epoch7-fixture', ROOT / '.runtime/phase17-epoch7-fixture.json'))
# name -> (table, ORDER BY, WHERE). Rows are always selected through the pre-upgrade column list.
PHASE17 = {'lifecycle_observations': ('lifecycle_observations', 'id', ''),
           'native_deliveries': ('native_deliveries', 'notification_id', ''),
           'native_banners': ('native_banners', 'id', ''), 'host_notices': ('host_notices', 'id', ''),
           'goals': ('goals', 'id', ''), 'goal_criteria': ('goal_criteria', 'goal_id,id', ''),
           'goal_steps': ('goal_steps', 'goal_id,id', ''), 'goal_events': ('goal_events', 'goal_id,sequence', '')}
EXPORTS = {'export_records': ('export_records', 'id', ''), 'export_saves': ('export_saves', 'id', '')}
ARTIFACTS = {'artifacts': ('artifacts', 'id', ''), 'artifact_revisions': ('artifact_revisions', 'artifact_id,revision', ''),
             'artifact_heads': ('artifact_heads', 'artifact_id', ''),
             'artifact_registry': ('artifact_registry', 'artifact_id', '')}
EARLIER = {**p17u.PHASE12, **p17u.PHASE13, **p17u.PHASE14, **p17u.LEARNING, **p17u.PHASE15, **p17u.PHASE16}
PRESERVED = {**p17u.PRESERVED, **PHASE17, **EXPORTS, **ARTIFACTS}
# In this fixture only the export rows are in flight; the epoch-7 screen and connector rows settled at 7 -> 8.
RECOVERING = {'export_records': 'id', 'export_saves': 'id'}
EXPORT_REASON = 'The service stopped before this export finished. Nothing was retried.'  # exports.Exports.__init__
# Settled synthetic current-epoch rows the restart check adds; validator-owned identifiers.
CURRENT_IDS = {name: prefix + '0' * 28 for name, prefix in (('quiz', 'e191'), ('attempt', 'e192'))}


class Pending(Exception):
    """A check that cannot run yet: its fixture or the parent's epoch-9 migration does not exist; never a pass."""


sha, bounded, read, tables, columns_of, quoted, connect, backups, wal = (
    p17u.sha, p17u.bounded, p17u.read, p17u.tables, p17u.columns_of, p17u.quoted, p17u.connect, p17u.backups, p17u.wal)


def builder():
    spec = importlib.util.spec_from_file_location('build_epoch8_fixture', BUILDER)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def epoch9_tables():
    """Names the Phase 19 sources declare (quiz_grading, and view_bindings once it stores bindings)."""
    from pebble import quiz_grading, view_bindings
    names = set(getattr(quiz_grading, 'TABLES', ())) | set(p17u.CREATED.findall(getattr(quiz_grading, 'DDL', '')))
    names |= set(getattr(view_bindings, 'TABLES', ())) | set(p17u.CREATED.findall(getattr(view_bindings, 'DDL', '')))
    if not names:
        raise Pending(PENDING_TABLES)
    return frozenset(names)


def judged_tables():
    return p17u.judged_tables() | epoch9_tables() | frozenset(NO_NEW)


def statement(name, columns):
    table, order, where = PRESERVED[name]
    return f'SELECT {quoted(columns[name])} FROM {table} {where} ORDER BY {order}'


def snapshot(db, columns):
    return {name: db.execute(statement(name, columns)).fetchall() for name in columns}


def fail_closed(name, row):
    """The only change Phase 19 startup may make to a preserved row of this fixture (exports.Exports.__init__)."""
    if name == 'export_records' and row.get('state') == 'rendering':
        receipt = {**json.loads(row['receipt']), 'status': 'failed', 'file': None, 'reason': EXPORT_REASON}
        return {'state': 'failed', 'receipt': json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                                                         separators=(',', ':'), allow_nan=False)}
    if name == 'export_saves' and row.get('state') == 'writing':
        return {'state': 'uncertain'}
    return p17u.fail_closed(name, row)


def recovery_problem(name, before_rows, actual_rows, columns):
    """Rows of a recovering table: settled rows exactly, in-flight rows exactly their fail-closed change."""
    at = columns.index(RECOVERING[name])
    old = {row[at]: dict(zip(columns, row)) for row in before_rows}
    new = {row[at]: dict(zip(columns, row)) for row in actual_rows}
    wrong = 0
    for ident in set(old) & set(new):
        row, after = old[ident], new[ident]
        change = fail_closed(name, row)
        if change is None:
            wrong += after != row
        else:
            expected = {**row, **change}
            wrong += not (all(after[c] == expected[c] for c in columns if c != 'updated')
                          and type(after['updated']) in (int, float) and after['updated'] >= row['updated'])
    missing, unexpected = len(set(old) - set(new)), len(set(new) - set(old))
    return {'missing': missing, 'unexpected': unexpected, 'changed': wrong} if missing or unexpected or wrong else None


def settled_count(name, rows, columns):
    return sum(fail_closed(name, dict(zip(columns, row))) is not None for row in rows) if name in RECOVERING else 0


def compare(db, names, before, columns):
    """{name: problem} for preserved tables whose rows changed beyond the time- and recovery-aware rule."""
    at = columns['memories'].index('id') if 'memories' in columns else None
    survivors = {row[at] for row in p17u.expected_rows('memories', before, columns, None)} if at is not None else set()
    problems = {}
    for name in names:
        actual = db.execute(statement(name, columns)).fetchall()
        if name in RECOVERING:
            problem = recovery_problem(name, before[name], actual, columns[name])
            if problem:
                problems[name] = problem
            continue
        expected = p17u.expected_rows(name, before, columns, survivors)
        if name in p17u.DEPENDENT:
            if not set(expected) <= set(actual) <= set(before[name]):
                problems[name] = {'missing': len(set(expected) - set(actual)),
                                  'unexpected': len(set(actual) - set(before[name]))}
        elif actual != expected:
            problems[name] = {'expected_rows': len(expected), 'actual_rows': len(actual)}
    return problems


def grouped_tables(names):
    return {PRESERVED[name][0] for name in names if not PRESERVED[name][2]}


def added_columns(db, columns):
    """{table: (moved, added, filled)}: existing columns keep their order; an added column stays empty."""
    report = {}
    for name, old in columns.items():
        table = PRESERVED[name][0]
        now = columns_of(db, table)
        added = now[len(old):]
        filled = db.execute(f'SELECT count(*) FROM {table} WHERE ' + ' OR '.join(f'"{c}" IS NOT NULL' for c in added)) \
            .fetchone()[0] if added else 0
        if now[:len(old)] != old or filled:
            report[name] = {'moved': now[:len(old)] != old, 'added': added, 'filled': filled}
    return report


def insert_current_rows(db, now):
    """Settled synthetic Phase 19 rows for the restart check: a host-graded quiz with its key and one graded
    attempt, owned by the fixture's completed run. Returns the rows added per table."""
    from pebble import quiz_grading
    rid, chat = db.execute("SELECT id,chat_id FROM runs WHERE state='completed' ORDER BY id LIMIT 1").fetchone()
    loaded = builder()
    public, key = quiz_grading.split(loaded.QUIZ_V1)
    assert quiz_grading.is_public(public), 'quiz_grading.split no longer yields a host-graded public view.'
    added = {}
    loaded.insert_artifact(db, CURRENT_IDS['quiz'], chat, rid, 'quiz', 'Host-graded quiz', [(public, 'ready', now - 120)],
                           now - 120)
    for table in ('artifacts', 'artifact_registry', 'artifact_heads', 'artifact_revisions'):
        added[table] = 1
    db.execute('INSERT INTO quiz_answer_keys(artifact_id,revision,chat_id,key_json,key_hash,public_hash,created) '
               'VALUES(?,?,?,?,?,?,?)', (CURRENT_IDS['quiz'], 1, chat, quiz_grading._encode(key), quiz_grading._digest(key),
                                         quiz_grading._digest(public), now - 120))
    result = quiz_grading.grade(public, key, {'q1': 1, 'q2': 2})
    # A settled (submitted, unshared) attempt, through the columns the table has; any column this validator
    # cannot fill is reported, never guessed.
    values = {'id': CURRENT_IDS['attempt'], 'chat_id': chat, 'artifact_id': CURRENT_IDS['quiz'], 'revision': 1,
              'origin': 'human', 'status': 'submitted', 'clock_id': 'boot:fixture', 'started': now - 110,
              'started_mono': 10.0, 'limit_seconds': 600, 'elapsed_mark': 30.0, 'draft': '{"q1":1,"q2":2}',
              'saved': now - 90, 'answers': '{"q1":1,"q2":2}', 'result': quiz_grading._encode(result), 'timed_out': 0,
              'reason': None, 'submitted': now - 80, 'share_input_id': None, 'shared': None, 'updated': now - 80}
    present = columns_of(db, 'quiz_attempts')
    unknown = sorted(set(present) - set(values))
    assert not unknown, ('This validator needs a settled value for these quiz_attempts columns.', unknown)
    row = {column: values[column] for column in present}
    db.execute(f'INSERT INTO quiz_attempts({quoted(row)}) VALUES({",".join("?" * len(row))})', tuple(row.values()))
    added.update(quiz_answer_keys=1, quiz_attempts=1)
    unfilled = sorted(epoch9_tables() - {'quiz_answer_keys', 'quiz_attempts'})
    assert not unfilled, ('This validator needs a settled row shape for these Phase 19 tables.', unfilled)
    return added


class Upgrade:
    def __init__(self, command):
        self.command = command
        self.checks, self.names, self.facts = [], set(), {}
        self.blocked = None  # the pending reason once the fixture or the migration is known to be absent

    def check(self, name, fn):
        assert name not in self.names, 'Check names must be unique: ' + name
        self.names.add(name)
        try:
            evidence = fn() or {}
            self.checks.append({'name': name, 'passed': True, 'evidence': evidence})
            return True
        except Pending as exc:
            self.checks.append({'name': name, 'passed': False, 'pending': True, 'reason': str(exc)})
            return False
        except Exception as exc:
            self.checks.append({'name': name, 'passed': False, 'error': bounded(exc, 2000),
                                'trace': traceback.format_exc(limit=4)[-1500:]})
            return False

    def need(self, key):
        if key not in self.facts:
            if self.blocked:
                raise Pending(self.blocked)
            raise AssertionError('Prerequisite was not established: ' + key)
        return self.facts[key]

    def run(self, home):
        self.check('Phase 19 tables are read from the backend sources; the epoch-9 migration creates, preserves and '
                   'guards each one', self.sources_guarded)
        self.check('fixture metadata, file hashes, the epoch-7 record and the preserved Phase 18 executable verified',
                   self.verify_fixture)
        if 'metadata' in self.facts and 'epoch9' in self.facts:  # never start a service on an unverified copy
            shutil.copytree(FIXTURE, home)
            self.facts['home'] = home
        self.check('private copy holds the six memory layers, the synthetic Phase 12 to 17 rows, the v1 quiz chains and '
                   'the in-flight export render and save at epoch 8', self.copy_holds_rows)
        self.check('actual service waits in upgrade maintenance while the backup budget is full and changes nothing',
                   self.budget_refusal)
        self.check('oldest retained backup removed through the native maintenance route with its manifest hash',
                   self.discard_oldest)
        self.check('native retry upgrades the private epoch-8 copy to the current epoch once', self.upgrade)
        self.check('six memory layers, checkpoint, chats, messages and permissions survive upgrade exactly; only expired '
                   'memories go', lambda: self.kept(p17u.CORE))
        self.check('synthetic Phase 12 to 16 rows survive upgrade exactly', lambda: self.kept(EARLIER))
        self.check('synthetic Phase 17 lifecycle, notification and goal rows survive upgrade exactly',
                   lambda: self.kept(PHASE17))
        self.check('synthetic Phase 17 exports survive upgrade; only the interrupted render fails and the interrupted '
                   'save becomes uncertain', self.kept_exports)
        self.check('artifact chains survive byte for byte; v1 quizzes stay readable as labelled self-checks and no key '
                   'moves into the host quiz tables', self.kept_artifacts)
        self.check('every other pre-existing row survives; no table startup must not fill gains a row', self.other_rows)
        self.check('preserved tables keep their columns; any added column is empty on preserved rows', self.additive)
        self.check('Phase 19 quiz and binding tables exist after the upgrade and are empty, as is every new table '
                   'startup must not fill', self.new_tables)
        self.check('verified pre-upgrade backup is epoch 8 and retains the exact preserved rows, in-flight rows included',
                   self.backup)
        self.check('upgraded database with settled Phase 19 rows restarts with no upgrade, backup or row change',
                   self.second_start)
        self.check('preserved Phase 18 executable refuses the current epoch before mutating the database', self.refusal)
        for epoch, fixture, metadata in OLDER:
            self.check(f'preserved epoch-{epoch} fixture upgrades to the current epoch once; its rows survive exactly; '
                       'only expired memories go; a restart changes nothing',
                       lambda e=epoch, f=fixture, m=metadata: self.older(e, f, m, home.parent))

    def sources_guarded(self):
        """The Phase 19 tables come from the sources; the parent's epoch-9 migration creates, preserves and guards
        each one, and the export recovery rule this validator applies is the one the migration layer admits."""
        if SCHEMA_EPOCH <= FROM_EPOCH:
            self.blocked = self.blocked or PENDING_MIGRATION
            raise Pending(PENDING_MIGRATION)
        phase19 = epoch9_tables()
        reference = set(p17u.CREATED.findall(migrations.REFERENCE_DDL))
        assert phase19 <= reference, sorted(phase19 - reference)
        assert all(name in migrations.PRESERVED for name in phase19), sorted(n for n in phase19 if n not in migrations.PRESERVED)
        assert phase19 <= set(NO_NEW), sorted(phase19 - set(NO_NEW))
        earlier = p17u.epoch6_tables() | p17u.epoch7_tables() | p17u.epoch8_tables()
        assert not phase19 & earlier, sorted(phase19 & earlier)
        wrong = [case[:4] for case in p17u.transition_cases() if case[4] != case[3]]
        assert not wrong, ('The migration layer no longer admits exactly the fail-closed recovery.', wrong)
        self.facts['epoch9'] = phase19
        return {'phase19_tables': sorted(phase19), 'current_epoch': SCHEMA_EPOCH, 'fixture_epoch': FROM_EPOCH}

    def verify_fixture(self):
        if not os.path.lexists(FIXTURE) and not os.path.lexists(METADATA):  # a dangling link is a failure
            self.blocked = PENDING if not self.blocked else self.blocked + ' ' + PENDING
            raise Pending(PENDING)
        assert METADATA.is_file() and FIXTURE.is_dir(), 'Only part of the epoch-8 fixture exists.'
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
        assert metadata['source_metadata_sha256'] == sha(PRIOR_METADATA[-1]), 'The epoch-7 record changed.'
        assert metadata['epoch'] == FROM_EPOCH, metadata['epoch']
        assert metadata['old_executable_sha256'] == EXPECTED_OLD and sha(OLD) == EXPECTED_OLD, \
            'The preserved Phase 18 executable does not match its recorded SHA-256.'
        self.facts['old_verified'] = True
        assert sha(BUILDER) == metadata['builder_sha256'], 'The fixture builder changed after the fixture was made.'
        assert not FIXTURE.is_symlink() and not any(path.is_symlink() for path in FIXTURE.rglob('*'))
        recorded = {name: digest for name, digest in metadata['files_sha256'].items() if Path(name).name not in SIDECARS}
        actual = {path.relative_to(FIXTURE).as_posix(): sha(path) for path in FIXTURE.rglob('*')
                  if path.is_file() and path.name not in SIDECARS}
        assert actual == recorded, sorted(set(actual) ^ set(recorded)) or 'A fixture file digest differs.'
        assert wal(FIXTURE) is None, 'The fixture has uncheckpointed WAL frames.'
        assert read(FIXTURE, 'PRAGMA user_version', immutable=True) == [(FROM_EPOCH,)]
        loaded = builder()
        assert set(loaded.IN_FLIGHT) <= set(metadata['synthetic_ids']) and metadata['settles_to'] == loaded.SETTLES_TO
        self.facts['prior_rows'], self.facts['metadata'], self.facts['builder'] = prior, metadata, loaded
        if SCHEMA_EPOCH <= FROM_EPOCH:
            self.blocked = self.blocked or PENDING_MIGRATION
        return {'files': len(recorded), 'epoch': FROM_EPOCH, 'current_epoch': SCHEMA_EPOCH,
                'legacy_backend_sha256': EXPECTED_OLD}

    def copy_holds_rows(self):
        home, metadata, prior, loaded = self.need('home'), self.need('metadata'), self.need('prior_rows'), self.need('builder')
        assert wal(home) is None and read(home, 'PRAGMA user_version', immutable=True) == [(FROM_EPOCH,)]
        present = tables(home, immutable=True)
        assert {PRESERVED[name][0] for name in PRESERVED} <= present, sorted({PRESERVED[n][0] for n in PRESERVED} - present)
        with connect(home / 'pebble.sqlite', immutable=True) as db:
            columns = {name: columns_of(db, PRESERVED[name][0]) for name in PRESERVED}
            before = snapshot(db, columns)
            everything = p17u.all_rows(db)
            counts = {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in metadata['table_rows']}
            ids = metadata['synthetic_ids']
            in_flight = {name: db.execute(f'SELECT state FROM {table} WHERE id=?', (ids[name],)).fetchone()
                         for name, (table, _) in loaded.IN_FLIGHT.items()}
            settled7 = {name: db.execute(f'SELECT state FROM {table} WHERE {key}=?', (ids[name],)).fetchone()
                        for name, (table, key, _) in loaded.EPOCH7_SETTLED.items()}
            kinds = dict(db.execute('SELECT id,kind FROM artifacts WHERE id IN (%s)' % ','.join('?' * len(loaded.ARTIFACTS)),
                                    [ids[n] for n in loaded.ARTIFACTS]).fetchall())
        assert counts == metadata['table_rows'], 'Row counts differ from the builder record.'
        assert {row[columns['memories'].index('kind')] for row in before['memories']} >= LAYERS
        inherited = {name: len(before[name]) for name in prior if name in before}
        assert all(inherited[name] >= prior[name] for name in inherited), (inherited, prior)
        assert all(before[name] for name in {**PHASE17, **EXPORTS}), {n: len(before[n]) for n in {**PHASE17, **EXPORTS}}
        assert in_flight == {name: (spec[1],) for name, spec in loaded.IN_FLIGHT.items()}, in_flight
        assert settled7 == {name: (spec[2],) for name, spec in loaded.EPOCH7_SETTLED.items()}, settled7
        assert kinds == {ids[n]: k for n, k in (('quiz', 'quiz'), ('stopped_quiz', 'quiz'), ('html', 'html'), ('table', 'table'))}
        assert not self.need('epoch9') & present, 'An epoch-8 database already has Phase 19 tables.'
        self.facts.update(columns=columns, before=before, tables_before=present, all_before=everything,
                          history_before=read(home, HISTORY, immutable=True))
        return {'memory_layers': sorted(LAYERS), 'inherited_rows': inherited,
                'phase17_rows': {name: len(before[name]) for name in {**PHASE17, **EXPORTS}},
                'artifacts': len(kinds), 'in_flight': {name: spec[1] for name, spec in loaded.IN_FLIGHT.items()}}

    def budget_refusal(self):
        home = self.need('home')
        self.need('before')
        listed = backups(home)
        assert sum(name.endswith('.sqlite') for name in listed) >= Limits().max_backups, listed
        before = (sha(home / 'pebble.sqlite'), wal(home), listed, read(home, HISTORY))
        service = Service(self.command, home)
        try:
            view = service.rpc('state').get('maintenance')
            assert view and view['reason'] == 'backup_budget_exhausted', view
            for method, params in (('new_chat', {}), ('get_chat', {'id': 'x'}), ('artifact_get', {'chat_id': 'x', 'id': 'x'}),
                                   ('quiz_start', {'chat_id': 'x', 'id': 'x', 'revision': 1}),
                                   ('setting', {'key': 'companion', 'value': {}})):
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
        self.facts.update(budget_refused=True, maintenance_backups=listing)
        return {'reason': view['reason'], 'listed_backups': len(listing), 'max_backups': Limits().max_backups}

    def discard_oldest(self):
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
            assert service.rpc('state')['active'] is None
        finally:
            service.close()
        self.facts['discarded'] = oldest
        return {'discarded': oldest, 'remaining_before_upgrade': len(listing) - 1, 'retried_in_place': True}

    def upgrade(self):
        home = self.need('home')
        self.need('discarded')
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        history, earlier = read(home, HISTORY), self.need('history_before')
        assert history[:len(earlier)] == earlier, 'Earlier upgrade history rows changed.'
        added = history[len(earlier):]
        assert [(row[1], row[2]) for row in added] == [(FROM_EPOCH, SCHEMA_EPOCH)], [(row[1], row[2]) for row in added]
        assert added[0][0] and added[0][0] + '.sqlite' in backups(home), 'The upgrade recorded no backup.'
        self.facts.update(backup_id=added[0][0], history=history, backups=backups(home))
        return {'from_version': FROM_EPOCH, 'to_version': SCHEMA_EPOCH, 'history_rows': len(history)}

    def kept(self, names):
        home, before, columns = self.need('home'), self.need('before'), self.need('columns')
        self.need('backup_id')
        with connect(home / 'pebble.sqlite') as db:
            problems = compare(db, sorted(names), before, columns)
        assert not problems, problems
        expired = len(before['memories']) - len(p17u.expected_rows('memories', before, columns, None)) if 'memories' in names else 0
        settled = {name: settled_count(name, before[name], columns[name]) for name in sorted(names) if name in RECOVERING}
        return {'tables': sorted(names), 'rows_before': {name: len(before[name]) for name in sorted(names)},
                'expired_memories_removed': expired, **({'settled_fail_closed': settled} if settled else {})}

    def kept_exports(self):
        evidence = self.kept(EXPORTS)
        home, ids, loaded = self.need('home'), self.need('metadata')['synthetic_ids'], self.need('builder')
        found = {}
        with connect(home / 'pebble.sqlite') as db:
            for name, (table, _) in loaded.IN_FLIGHT.items():
                found[name] = db.execute(f'SELECT state FROM {table} WHERE id=?', (ids[name],)).fetchone()[0]
        assert found == loaded.SETTLES_TO, found
        return {**evidence, 'in_flight': found}

    def kept_artifacts(self):
        """Byte-for-byte chains, read back by the upgraded service: v1 quizzes are self-checks with their own
        answers, a host quiz route refuses them, and no key was copied into the host's quiz tables."""
        evidence = self.kept(ARTIFACTS)
        home, ids, loaded = self.need('home'), self.need('metadata')['synthetic_ids'], self.need('builder')
        with connect(home / 'pebble.sqlite') as db:
            chat = db.execute('SELECT chat_id FROM artifact_registry WHERE artifact_id=?', (ids['quiz'],)).fetchone()[0]
            keys = {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in self.need('epoch9')}
        assert not any(keys.values()), ('The upgrade wrote Phase 19 rows.', keys)
        service = Service(self.command, home)
        read_back = {}
        try:
            for name in loaded.ARTIFACTS:
                history = service.rpc('artifact_history', chat_id=chat, id=ids[name])
                current = service.rpc('artifact_get', chat_id=chat, id=ids[name])
                read_back[name] = (len(history), current['status'], current.get('grading'))
            for name in ('quiz', 'stopped_quiz'):
                current = service.rpc('artifact_get', chat_id=chat, id=ids[name])
                assert current.get('grading') == 'self_check' and 'answer' in current['data']['questions'][0], name
            try:
                service.rpc('quiz_start', chat_id=chat, id=ids['quiz'], revision=2)
            except RuntimeError as exc:
                assert str(exc) != 'Unknown request.', 'Parent patch: the quiz_start route is missing.'
            else:
                raise AssertionError('A v1 self-check quiz started a host-graded attempt.')
        finally:
            service.close()
        assert read_back == {'quiz': (2, 'ready', 'self_check'), 'stopped_quiz': (3, 'stopped', 'self_check'),
                             'html': (1, 'review', None), 'table': (1, 'ready', None)}, read_back
        with connect(home / 'pebble.sqlite') as db:
            assert not any(db.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in self.need('epoch9'))
        return {**evidence, 'read_back': read_back}

    def other_rows(self):
        home, everything = self.need('home'), self.need('all_before')
        self.need('backup_id')
        grouped = grouped_tables(PRESERVED)
        with connect(home / 'pebble.sqlite') as db:
            problems = p17u.other_problems(db, everything, grouped)
        assert not problems, problems
        judged = sorted(set(everything) - grouped - {'upgrade_history'})
        return {'tables': len(judged), 'rows': sum(len(everything[name][1]) for name in judged),
                'no_new_tables': sorted(set(judged) & set(NO_NEW))}

    def additive(self):
        home, columns = self.need('home'), self.need('columns')
        self.need('backup_id')
        with connect(home / 'pebble.sqlite') as db:
            report = added_columns(db, columns)
        assert not report, report
        return {'tables_compared': len(columns)}

    def new_tables(self):
        home, before, expected = self.need('home'), self.need('tables_before'), self.need('epoch9')
        self.need('backup_id')
        present = tables(home)
        assert expected <= present, sorted(expected - present)
        problems, evidence = p17u.new_tables_problems(home, before, judged_tables())
        assert not problems, problems
        assert expected <= set(evidence['new_tables']), 'A Phase 19 table existed before the upgrade.'
        counts = {name: read(home, f'SELECT count(*) FROM "{name}"')[0][0] for name in sorted(expected)}
        assert not any(counts.values()), counts
        return {**evidence, 'phase19_rows': counts}

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
            assert not names & self.need('epoch9'), 'The pre-upgrade backup already holds Phase 19 tables.'
        in_flight = sum(settled_count(name, before[name], columns[name]) for name in RECOVERING)
        assert in_flight == len(self.need('builder').IN_FLIGHT), in_flight
        return {'backup_epoch': FROM_EPOCH, 'tables_compared': len(columns), 'in_flight_rows_kept': in_flight}

    def second_start(self):
        home, history, listed = self.need('home'), self.need('history'), self.need('backups')
        self.need('backup_id')
        assert read(home, HISTORY) == history and backups(home) == listed
        with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite')) as db:
            with db:
                added = insert_current_rows(db, time.time())
        assert self.need('epoch9') <= set(added), sorted(self.need('epoch9') - set(added))
        evidence = p17u.restart_unchanged(self.command, home)
        return {**evidence, 'phase19_rows_added': added}

    def refusal(self):
        home = self.need('home')
        self.need('old_verified')  # never execute an unverified binary
        self.need('backup_id')
        before = (sha(home / 'pebble.sqlite'), wal(home), backups(home), read(home, HISTORY))
        refused = subprocess.run([str(OLD), '--home', str(home), '--resources', str(ROOT / 'src-tauri/resources')],
                                 input='', capture_output=True, text=True, timeout=60)
        assert refused.returncode != 0, 'The Phase 18 executable accepted a newer epoch.'
        assert REFUSAL in refused.stderr, refused.stderr[-600:]
        after = (sha(home / 'pebble.sqlite'), wal(home), backups(home), read(home, HISTORY))
        assert after == before, 'The Phase 18 executable changed the database, its WAL, backups or upgrade history.'
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        return {'exit_code': refused.returncode, 'refused_epoch': SCHEMA_EPOCH, 'database_unchanged': True}

    def older(self, epoch, fixture, metadata_path, scratch):
        """Upgrade an untouched historical fixture (verified first) straight to the current epoch, then restart."""
        if SCHEMA_EPOCH <= FROM_EPOCH:
            raise Pending(PENDING_MIGRATION)
        if not os.path.lexists(fixture) and not os.path.lexists(metadata_path):
            # Merge (i19): reached only once epoch 9 exists; a fixture path outside the checkout (the pytest
            # self-check's) is named as is instead of raising from relative_to.
            shown = fixture.relative_to(ROOT) if fixture.is_relative_to(ROOT) else fixture
            raise Pending(f'The preserved epoch-{epoch} fixture ({shown}) is not in this checkout.')
        metadata = p17u.verify_older(epoch, fixture, metadata_path)
        home = scratch / f'older-epoch{epoch}'
        shutil.copytree(fixture, home)
        if not metadata.get('files_sha256'):
            present = tables(home)
            counted = {t: n for t, n in metadata['counts'].items() if t in present and not t.startswith('memory_search')}
            assert {t: read(home, f'SELECT count(*) FROM {t}')[0][0] for t in counted} == counted, 'Fixture rows differ.'
        before_tables = tables(home)
        names = [n for n in PRESERVED if PRESERVED[n][0] in before_tables]
        with connect(home / 'pebble.sqlite') as db:
            columns = {n: columns_of(db, PRESERVED[n][0]) for n in names}
            before = snapshot(db, columns)
            everything = p17u.all_rows(db)
            history_before = db.execute(HISTORY).fetchall() if 'upgrade_history' in before_tables else []
        discarded = p17u.upgrade_in_place(self.command, home, metadata.get('files_sha256'))
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        history = read(home, HISTORY)
        assert history[:len(history_before)] == history_before, 'Earlier upgrade history rows changed.'
        assert [(r[1], r[2]) for r in history[len(history_before):]] == [(epoch, SCHEMA_EPOCH)], history[len(history_before):]
        with connect(home / 'pebble.sqlite') as db:
            # Older fixtures carry the epoch-7 in-flight screen and connector rows: the Phase 17 rule settles them.
            problems = compare(db, [n for n in names if n not in RECOVERING and n not in p17u.RECOVERING], before, columns)
            recovered = {n: p17u.recovery_problem(n, before[n], db.execute(statement(n, columns)).fetchall(), columns[n])
                         for n in names if n in p17u.RECOVERING}
            added = added_columns(db, columns)
            others = p17u.other_problems(db, everything, grouped_tables(names))
        assert not problems and not any(recovered.values()), (problems, recovered)
        assert not added, ('A preserved table changed shape or an added column was filled.', added)
        assert not others, ('Another pre-existing row changed or a guarded table gained a row.', others)
        current = p17u.epoch6_tables() | p17u.epoch7_tables() | p17u.epoch8_tables() | epoch9_tables()
        assert current <= tables(home), sorted(current - tables(home))
        graph, evidence = p17u.new_tables_problems(home, before_tables, judged_tables())
        assert not graph, graph
        restart = p17u.restart_unchanged(self.command, home)
        expired = len(before['memories']) - len(p17u.expected_rows('memories', before, columns, None)) if 'memories' in before else 0
        return {'from_version': epoch, 'to_version': SCHEMA_EPOCH, 'tables_compared': len(names),
                'maintenance_discarded': discarded, 'expired_memories_removed': expired, **evidence, 'restart': restart}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    args = parser.parse_args()
    if os.path.lexists(args.report):
        parser.error('--report already exists; preserve existing upgrade evidence and select a new path.')
    executable = p17u.owned_backend(parser, args.backend) if args.backend else ROOT / 'backend/main.py'
    command = [str(executable)] if args.backend else [sys.executable, str(executable)]
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase19-upgrade-')).resolve()
    if scratch.is_relative_to(ROOT):
        parser.error('The temporary folder is inside this checkout; set TMPDIR elsewhere.')
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
              'failed': [c['name'] for c in upgrade.checks if not c['passed'] and not c.get('pending')],
              'pending': [c['name'] for c in upgrade.checks if c.get('pending')],
              'check_results': upgrade.checks, 'fatal_error': fatal,
              'scratch_preserved': None if passed else str(scratch),
              'fixture_metadata_sha256': sha(METADATA) if METADATA.is_file() else None,
              'source_sha256': {p: sha(ROOT / p) if (ROOT / p).is_file() else None for p in (
                  'scripts/validate_phase19_upgrade.py', 'scripts/validate_phase17_upgrade.py', 'scripts/validate_runtime.py',
                  'backend/pebble/migrations.py', 'backend/pebble/quiz_grading.py', 'backend/pebble/view_bindings.py',
                  'backend/pebble/artifacts.py', 'backend/pebble/artifact_inputs.py', 'backend/pebble/artifact_schema.py',
                  'backend/pebble/exports.py', 'backend/pebble/app.py', '.runtime/phase19-baseline/build_epoch8_fixture.py')},
              'synthetic_only': True, 'inference_calls': 0, 'credentials_used': False}
    descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'kind': result['kind'], 'checks': len(result['checks']), 'failed': result['failed'],
                      'pending': len(result['pending']), 'fatal_error': fatal, 'passed': passed}))
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()


# Development self-checks, run only by pytest on this file; the validator never calls them. They read
# the builder and work on owned databases under pytest's tmp_path. Neither the service nor the Phase 18
# executable is started, except by the explicit-request run at the end.

CHECK_COUNT = 17 + len(OLDER)


def _app(home):
    from pebble.app import App
    return App(home, home.parent / 'resources', lambda *args: None)


def _completed_run(home):
    """An owned App database with one chat and one completed run, closed again."""
    app = _app(home)
    try:
        chat = app.new_chat()
        rid = app.runs.create(chat['id'], 'Synthetic fixture run.', app.runtime_config(chat, False))
        app.runs.transition(rid, 'running')
        app.runs.transition(rid, 'completed')
        return chat['id'], rid
    finally:
        app.close()


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


def test_every_check_is_pending_with_its_reason_while_the_fixture_or_the_migration_is_absent(monkeypatch, tmp_path):
    """Pending is never passed: with no fixture (and, here, no epoch-9 migration) nothing passes or fails."""
    monkeypatch.setattr(sys.modules[__name__], 'FIXTURE', tmp_path / 'absent-fixture')
    monkeypatch.setattr(sys.modules[__name__], 'METADATA', tmp_path / 'absent-fixture.json')
    monkeypatch.setattr(sys.modules[__name__], 'OLDER', tuple((e, tmp_path / f'absent-{e}', tmp_path / f'absent-{e}.json')
                                                              for e, _, _ in OLDER))
    probe = Upgrade(['unused'])
    (tmp_path / 'scratch').mkdir()
    probe.run(tmp_path / 'scratch' / 'home')
    assert len(probe.checks) == CHECK_COUNT
    # Only the sources check can run without a fixture: it passes once the parent's epoch-9 migration exists.
    sources, rest = probe.checks[0], probe.checks[1:]
    assert sources['passed'] or sources.get('pending'), sources.get('error')
    assert all(c.get('pending') and not c['passed'] and 'error' not in c for c in rest), \
        [(c['name'], c.get('error')) for c in rest if not c.get('pending')]
    reasons = {c['reason'] for c in probe.checks if c.get('pending')}
    if SCHEMA_EPOCH <= FROM_EPOCH:
        assert PENDING_MIGRATION in reasons
    assert any(PENDING in reason for reason in reasons)
    assert not (tmp_path / 'scratch' / 'home').exists(), 'A pending run copied a fixture.'
    # Half a fixture is a failure, never pending.
    (tmp_path / 'absent-fixture').mkdir()
    half = Upgrade(['unused'])
    assert not half.check('fixture', half.verify_fixture) and 'pending' not in half.checks[-1]
    assert 'Only part' in half.checks[-1]['error']


def test_dependent_checks_never_run_without_their_prerequisites():
    def steps(upgrade):
        return (('copy', upgrade.copy_holds_rows), ('budget', upgrade.budget_refusal), ('discard', upgrade.discard_oldest),
                ('upgrade', upgrade.upgrade), ('core', lambda: upgrade.kept(p17u.CORE)), ('exports', upgrade.kept_exports),
                ('artifacts', upgrade.kept_artifacts), ('other', upgrade.other_rows), ('additive', upgrade.additive),
                ('tables', upgrade.new_tables), ('backup', upgrade.backup), ('second', upgrade.second_start),
                ('refusal', upgrade.refusal))
    upgrade = Upgrade(['unused'])
    for name, fn in steps(upgrade):
        assert upgrade.check(name, fn) is False and 'Prerequisite' in upgrade.checks[-1]['error'], name
    waiting = Upgrade(['unused'])
    waiting.blocked = PENDING
    for name, fn in steps(waiting):
        assert waiting.check(name, fn) is False and waiting.checks[-1].get('pending') is True, name


def test_builder_constants_and_helpers_match_the_backend():
    from pebble.artifacts import Artifacts
    from pebble.exports import canonical
    loaded = builder()
    assert (loaded.EXPECTED_OLD, loaded.OUT, loaded.META, loaded.EPOCH) == (EXPECTED_OLD, FIXTURE, METADATA, FROM_EPOCH)
    assert loaded.SOURCE.relative_to(ROOT).as_posix() == SOURCE_FIXTURE and loaded.SOURCE_META == PRIOR_METADATA[-1]
    value = {'b': [1, 'é', None], 'a': 2.5}
    assert loaded.encode(value) == Artifacts._encode(value) == canonical(value)
    assert loaded.digest(value) == Artifacts._digest(value)
    assert loaded.ARTIFACT_COLUMNS == Artifacts._COLUMNS
    assert all(len(ident) == 32 and int(ident, 16) >= 0 for ident in loaded.IDS.values())
    assert len(set(loaded.IDS.values())) == len(loaded.IDS)
    assert set(loaded.IN_FLIGHT) == set(loaded.SETTLES_TO) <= set(loaded.IDS)
    assert {spec[0] for spec in loaded.IN_FLIGHT.values()} == set(RECOVERING)
    assert set(loaded.EPOCH7_SETTLED) == set(p17u.IN_FLIGHT)
    assert {n: spec[2] for n, spec in loaded.EPOCH7_SETTLED.items()} == {n: spec[3] for n, spec in p17u.IN_FLIGHT.items()}
    assert set(PHASE17) | set(EXPORTS) == p17u.epoch8_tables()
    assert all(PRESERVED[name][0] == name for name in {**PHASE17, **EXPORTS, **ARTIFACTS})
    from pebble import exports
    assert EXPORT_REASON in Path(exports.__file__).read_text()


def test_builder_rows_fit_the_current_schema_and_the_backends_own_readers(tmp_path):
    """The builder's rows on an owned current-schema database: the backend's readers accept every chain,
    v1 quizzes are labelled self-checks, its own startup settles exactly the two in-flight rows, and this
    validator's recovery rule agrees."""
    loaded = builder()
    home = tmp_path / 'home'
    chat, rid = _completed_run(home)
    with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite')) as db:
        with db:
            settled = loaded.insert_settled(db, time.time() - 3600)
        with db:
            flying = loaded.insert_in_flight(db, time.time() - 60)
        assert loaded.states(db) == {name: spec[1] for name, spec in loaded.IN_FLIGHT.items()}
        columns = {name: columns_of(db, PRESERVED[name][0]) for name in {**PHASE17, **EXPORTS, **ARTIFACTS}}
        before = snapshot(db, columns)
    assert settled['artifact_revisions'] == 7 and settled['export_records'] == settled['export_saves'] == 1
    assert flying == {'export_records': 1, 'export_saves': 1}
    app = _app(home)  # startup recovery runs here
    try:
        ids = loaded.IDS
        for name, revisions in (('quiz', 2), ('stopped_quiz', 3), ('html', 1), ('table', 1)):
            assert len(app.artifacts.history(chat, ids[name])) == revisions, name
        for name in ('quiz', 'stopped_quiz'):
            row = app.artifact_get(chat, ids[name])
            assert row['grading'] == 'self_check' and row['data']['questions'][0]['answer'] == 1, name
        assert app.artifact_get(chat, ids['stopped_quiz'])['status'] == 'stopped'
        assert app.artifact_get(chat, ids['html'])['data']['script'] == loaded.HTML['script']
        with app.store.lock:
            assert loaded.states(app.store.db) == loaded.SETTLES_TO
            assert compare(app.store.db, sorted({**PHASE17, **EXPORTS, **ARTIFACTS}), before, columns) == {}
    finally:
        app.close()


def test_settled_phase19_rows_fit_the_current_tables_and_read_back_through_the_backend(tmp_path):
    """The restart check's Phase 19 rows on an owned database with the sources' epoch-9 tables: the host reads
    the quiz as host-graded with its stored grade, and its own constructors leave every row unchanged."""
    from pebble import quiz_grading
    home = tmp_path / 'home'
    chat, _ = _completed_run(home)
    with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite')) as db:
        db.executescript(quiz_grading.DDL)
        with db:
            added = insert_current_rows(db, time.time())
        before = p17u.all_rows(db)
    assert added['quiz_answer_keys'] == added['quiz_attempts'] == 1 and set(added) >= epoch9_tables()
    app = _app(home)
    try:
        row = app.artifact_get(chat, CURRENT_IDS['quiz'])
        assert row['grading'] == 'host' and all(set(q) == {'id', 'question', 'choices'} for q in row['data']['questions'])
        attempt = quiz_grading.QuizAttempts(app).attempt(chat, CURRENT_IDS['quiz'], 1)
        assert attempt['attempt_id'] == CURRENT_IDS['attempt'] and attempt['status'] == 'submitted'
        assert (attempt['result']['score'], attempt['result']['total']) == (1, 2)
        with app.store.lock:
            after = p17u.all_rows(app.store.db)
    finally:
        app.close()
    assert {t: after[t] for t in epoch9_tables()} == {t: before[t] for t in epoch9_tables()}


def test_export_recovery_rule_refuses_any_other_change(tmp_path):
    from pebble.exports import EXPORT_DDL
    path = tmp_path / 'pebble.sqlite'
    receipt = json.dumps({'status': 'rendering', 'file': None, 'format': 'csv'}, sort_keys=True, separators=(',', ':'))
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.executescript(EXPORT_DDL)
        for ident, state in (('a' * 32, 'rendering'), ('b' * 32, 'verified')):
            db.execute('INSERT INTO export_records VALUES(?,?,?,?,?,?,?,?,?)', (ident, 'c', 'p', 'csv', '{}', state, receipt, 1.0, 1.0))
        db.execute('INSERT INTO export_saves VALUES(?,?,?,?,?,?,?,?,?)', ('s' * 32, 'a' * 32, 'c', 'writing', '/x', None, 0, 1.0, 1.0))
    with connect(path) as db:
        columns = {name: columns_of(db, name) for name in EXPORTS}
        before = snapshot(db, columns)

    def outcome(*statements):
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            for sql in statements:
                db.execute(sql)
        with connect(path) as db:
            return compare(db, sorted(EXPORTS), before, columns)

    assert set(outcome()) == {'export_records', 'export_saves'}  # not yet recovered
    from pebble.exports import Exports
    from pebble.store import Store
    store = Store(tmp_path)
    try:
        Exports(tmp_path, store, None)
    finally:
        store.db.close()
    assert outcome() == {}
    assert outcome("UPDATE export_records SET updated=9 WHERE state='verified'") == {
        'export_records': {'missing': 0, 'unexpected': 0, 'changed': 1}}


def test_source_upgrade_run_on_explicit_request(request, tmp_path):
    """The whole validator in source mode as a subprocess; it starts the actual service once the fixture and
    the epoch-9 migration exist, so it runs only when this node ID is named explicitly."""
    import pytest
    if not any(str(arg).endswith('::test_source_upgrade_run_on_explicit_request') for arg in request.config.args):
        pytest.skip('Starts the actual service; name this test explicitly to run it.')
    report = tmp_path / 'report.json'
    with (tmp_path / 'upgrade.log').open('w') as sink:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--report', str(report)], cwd=ROOT,
                              env={**os.environ, 'TMPDIR': str(tmp_path)}, stdout=sink, stderr=subprocess.STDOUT,
                              timeout=600)
    outcome = json.loads(report.read_text())
    names = outcome['checks'] + outcome['pending'] + outcome['failed']
    assert outcome['kind'] == 'source' and len(names) == len(set(names)) == CHECK_COUNT
    if FIXTURE.is_dir() and METADATA.is_file() and SCHEMA_EPOCH > FROM_EPOCH:
        assert done.returncode == 0 and outcome['passed'] is True and not outcome['pending'], outcome['failed']
    else:
        assert done.returncode == 1 and outcome['passed'] is False and outcome['pending'] and not outcome['failed']
    import warnings
    warnings.warn('upgrade result: ' + json.dumps({'pending': outcome['pending'], 'failed': outcome['failed']})[:6000])
