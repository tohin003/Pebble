"""Actual Phase 16 epoch-7 fixture -> current service upgrade, older fixtures, restart and downgrade refusal.

Uses the parent-owned synthetic epoch-7 fixture (.runtime/phase17-epoch7-fixture,
built by .runtime/phase17-baseline/build_epoch7_fixture.py with the preserved
Phase 16 candidate) and that preserved Phase 16 executable. Never touches an
installed home, account, credential or model. Each mode upgrades its own private
copies under the system temporary folder, never under .runtime. The fixture's
manual synthetic rows (six memory layers, a settled run, the Phase 12 to 15
rows of the earlier fixtures, and the Phase 16 research plan, sources, texts,
claims, kept claim rows and their open conflict, screen sessions and acts, and
connector effects with native recipient confirmations) are preservation
fixtures, not learned or executed evidence. Some Phase 16 rows are in flight, as
a crash leaves them: a live screen session with an admitted input act and a
proposed commit, and an admitted connector send.

The epoch-7 fixture retains three upgrade backups (epochs 4->5, 5->6, 6->7),
which is migrations.Limits.max_backups including the new one. The service must
therefore wait in upgrade maintenance (pebble.maintenance) and change nothing,
serving only its maintenance routes. The validator then removes the oldest backup
through the native upgrade_discard_backup route, as a person would in the app,
confirmed by the manifest hash the service lists (cross-checked with the
fixture's recorded hash), and retries startup in place with upgrade_retry.
Nothing is deleted automatically. Preserved older fixtures whose budget is also
full (epochs 4, 5 and 6) go through the same native route.

Preservation is time-aware and recovery-aware. Pebble deletes expired memories, and the migration layer
permits exactly that (migrations.PRESERVED 'memories'), so expired memories may go, together with the
learning evidence, claims and graph edges of a gone memory. Startup recovery settles in-flight rows only to
their fail-closed states, which the migration layer also admits: an admitted screen act becomes unknown with
an unknown verdict and restart evidence, a proposal expires, a live session is restarted and an admitted
connector effect becomes unknown, each with a later updated time. Every other row survives exactly, through
the column list it had before the upgrade; a column added to a preserved table must be empty on every
preserved row. Every other pre-existing row survives too, and a table startup must not fill
(migrations.NO_NEW) gains no row. Search caches and full-text index structures are derived, not preserved.

The current epoch is never pinned: every comparison uses migrations.SCHEMA_EPOCH and the fixture's own
recorded epoch. The Phase 17 tables are read from the backend sources (lifecycle.LIFECYCLE_DDL,
notifications.NOTIFY_DDL, exports.EXPORT_DDL and goals.GOAL_DDL), as are the Phase 15 and 16 tables. They
and every table startup must not fill must be empty when they are new since a fixture's epoch, except that
the memory graph may hold this installation's local scope salt and the supersedes edges migrated from
memory_evidence.superseded_by. Other new tables are listed, not judged. An upgraded database, given settled
synthetic Phase 17 rows, then restarts without another upgrade, backup or row change. This validator also
upgrades each preserved older fixture (epochs 1-6), verified against its recorded metadata first, straight
to the current epoch, and restarts it.

Checks are recorded independently by name; a failure never stops later
independent checks and a report is written either way. Until the parent has
built the epoch-7 fixture, its checks report pending with that reason (and the
run does not pass) while every other check still runs. --report must be a new
file. The Phase 16 executable runs only when its SHA-256 matches the preserved
candidate, and only against the private copy.

Usage:
  python scripts/validate_phase17_upgrade.py --report PATH [--backend PATH]
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
import zlib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from validate_runtime import ROOT, Service
sys.path.insert(0, str(ROOT / 'backend'))
from pebble import migrations
from pebble.migrations import BACKUPS, NO_NEW, SCHEMA_EPOCH, Limits, discard_backup


OLD = ROOT / '.runtime/phase16-candidate/pebble-backend/pebble-backend'
FIXTURE = ROOT / '.runtime/phase17-epoch7-fixture'
METADATA = ROOT / '.runtime/phase17-epoch7-fixture.json'
BUILDER = ROOT / '.runtime/phase17-baseline/build_epoch7_fixture.py'
# The epoch-3 to 6 metadata record the synthetic Phase 12 to 15 rows the epoch-7 fixture inherited.
PRIOR_METADATA = (ROOT / '.runtime/phase13-epoch3-fixture.json', ROOT / '.runtime/phase14-epoch4-fixture.json',
                  ROOT / '.runtime/phase15-epoch5-fixture.json', ROOT / '.runtime/phase16-epoch6-fixture.json')
SOURCE_FIXTURE = '.runtime/phase16-epoch6-fixture'
EXPECTED_OLD = 'ccc3e17f36a2f13c6d01ce34df0b9adea5ad0aeafab0ba1deb1efed5b7ff842f'
FROM_EPOCH = 7  # the fixture's own epoch, never the current one
PENDING = ('The epoch-7 fixture (.runtime/phase17-epoch7-fixture and its .json manifest) does not exist yet; the '
           'parent builds it with .runtime/phase17-baseline/build_epoch7_fixture.py and the preserved Phase 16 '
           'candidate.')
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
# The epoch-7 tables (epoch7_tables() reads the same names from the sources; a self-check keeps them equal).
RESEARCH = {'research_sources': ('research_sources', 'run_id,n', ''),
            'research_texts': ('research_texts', 'sha256', ''),
            'research_plans': ('research_plans', 'id', ''),
            'research_claims': ('research_claims', 'id', ''),
            'research_claim_rows': ('research_claim_rows', 'run_id,claim_id', ''),
            'research_conflicts': ('research_conflicts', 'id', '')}
SCREEN = {'screen_sessions': ('screen_sessions', 'session_id', ''),
          'screen_actions': ('screen_actions', 'action_id', '')}
EFFECTS = {'connector_effects': ('connector_effects', 'action_id', ''),
           'connector_effect_confirmations': ('connector_effect_confirmations', 'action_id', '')}
PHASE16 = {**RESEARCH, **SCREEN, **EFFECTS}
PRESERVED = {**CORE, **PHASE12, **PHASE13, **PHASE14, **LEARNING, **PHASE15, **PHASE16}
# Rows of a gone memory that may go with it, or stay until the next maintenance pass; nothing else changes.
DEPENDENT = frozenset({'memory_evidence', 'memory_claims', 'memory_relations'})
# Rows the epoch-6 graph may hold right after an upgrade from an older epoch (see the module docstring).
GRAPH_ROWS = frozenset({'memory_scope_salts', 'memory_relations'})
# Tables whose in-flight rows startup recovery settles fail-closed, and the key column of each.
RECOVERING = {'screen_sessions': 'session_id', 'screen_actions': 'action_id', 'connector_effects': 'action_id'}
LIVE = ('active', 'handoff', 'interrupted')
RESTART_EVIDENCE = '{"basis":"restart_during_act"}'
# Search caches their owner rebuilds; full-text index tables are found from sqlite_master. Not preserved data.
DERIVED = frozenset({'search_index_meta', 'search_index_files', 'search_index_roots'})
# The fixture's in-flight rows (builder IDS keys), their pre-upgrade states and the states they must settle to.
IN_FLIGHT = {'live_session': ('screen_sessions', 'session_id', 'active', 'restarted'),
             'admitted_act': ('screen_actions', 'action_id', 'admitted', 'unknown'),
             'proposed_act': ('screen_actions', 'action_id', 'proposed', 'expired'),
             'admitted_effect': ('connector_effects', 'action_id', 'admitted', 'unknown')}
HISTORY = 'SELECT backup_id,from_version,to_version,completed FROM upgrade_history ORDER BY id'
# Preserved older fixtures, upgraded straight to the current epoch (time-aware; see the module docstring).
OLDER = ((1, ROOT / '.runtime/phase11-epoch1-fixture', ROOT / '.runtime/phase11-epoch1-fixture.json'),
         (2, ROOT / '.runtime/phase12-epoch2-fixture', ROOT / '.runtime/phase12-epoch2-fixture.json'),
         (3, ROOT / '.runtime/phase13-epoch3-fixture', ROOT / '.runtime/phase13-epoch3-fixture.json'),
         (4, ROOT / '.runtime/phase14-epoch4-fixture', ROOT / '.runtime/phase14-epoch4-fixture.json'),
         (5, ROOT / '.runtime/phase15-epoch5-fixture', ROOT / '.runtime/phase15-epoch5-fixture.json'),
         (6, ROOT / '.runtime/phase16-epoch6-fixture', ROOT / '.runtime/phase16-epoch6-fixture.json'))
REFUSAL = 'Pebble stopped this upgrade: The database was written by a newer Pebble.'
CREATED = re.compile(r'CREATE (?:VIRTUAL )?TABLE (?:IF NOT EXISTS )?(\w+)')
# Settled synthetic current-epoch rows the restart check adds; validator-owned identifiers.
CURRENT_IDS = {name: prefix + '0' * 28 for name, prefix in (
    ('sleep', 'e170'), ('zone', 'e171'), ('notice', 'e172'), ('banner', 'e173'), ('delivery', 'e174'),
    ('job', 'e175'), ('export', 'e176'), ('save', 'e177'), ('goal', 'e178'), ('plan', 'e179'))}


class Pending(Exception):
    """A check that cannot run yet because its fixture does not exist; never a pass."""


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
    return [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]


def quoted(columns):
    return ','.join(f'"{column}"' for column in columns)


def statement(name, columns):
    table, order, where = PRESERVED[name]
    return f'SELECT {quoted(columns[name])} FROM {table} {where} ORDER BY {order}'


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
    sessions and their acts, and the connector effect journal with its native confirmations."""
    from pebble import connector_effects, research_store, screen_session, source_ledger
    effects = getattr(connector_effects, 'TABLES', None) or CREATED.findall(';'.join(connector_effects.DDL))
    return (frozenset(source_ledger.TABLES) | frozenset(research_store.TABLES) | frozenset(screen_session.TABLES)
            | frozenset(effects))


def epoch8_tables():
    """Names the Phase 17 DDL creates, read from the backend sources: lifecycle observations, the native
    notification outbox, banners and host notices, export records and saves, and goals with their
    criteria, steps and events."""
    from pebble import exports, goals, lifecycle, notifications
    return frozenset(CREATED.findall(lifecycle.LIFECYCLE_DDL + notifications.NOTIFY_DDL + exports.EXPORT_DDL
                                     + goals.GOAL_DDL))


def judged_tables():
    """New tables that must be empty right after an upgrade (except the allowed graph rows)."""
    return epoch6_tables() | epoch7_tables() | epoch8_tables() | frozenset(NO_NEW)


def data_tables(db):
    """Every table holding preserved data: no full-text index structure and no derived search cache."""
    rows = db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    virtual = [name for name, sql in rows if (sql or '').upper().startswith('CREATE VIRTUAL TABLE')]
    return sorted(name for name, _ in rows if name not in DERIVED and name not in virtual
                  and not any(name.startswith(v + '_') for v in virtual))


def all_rows(db):
    """{table: (columns, rows)} of every data table, for the other-rows and restart comparisons."""
    result = {}
    for name in data_tables(db):
        columns = columns_of(db, name)
        result[name] = (columns, sorted(db.execute(f'SELECT {quoted(columns)} FROM "{name}"').fetchall(), key=repr))
    return result


def fail_closed(name, row):
    """The only change startup recovery may make to a preserved row besides a later updated time
    (screen_session.ScreenSessions.recover, connector_effects.ConnectorEffects): None for a settled row."""
    state = row.get('state')
    if name == 'screen_sessions' and state in LIVE:
        return {'state': 'restarted'}
    if name == 'screen_actions' and state == 'admitted':
        return {'state': 'unknown', 'verdict': 'unknown', 'evidence': RESTART_EVIDENCE}
    if name == 'screen_actions' and state == 'proposed':
        return {'state': 'expired'}
    if name == 'connector_effects' and state == 'admitted':
        return {'state': 'unknown'}
    return None


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
    """How many rows of a table startup recovery must settle."""
    return sum(fail_closed(name, dict(zip(columns, row))) is not None for row in rows) if name in RECOVERING else 0


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
    """{name: problem} for preserved tables whose rows changed beyond the time- and recovery-aware rule."""
    at = columns['memories'].index('id') if 'memories' in columns else None
    survivors = {row[at] for row in expected_rows('memories', before, columns, None)} if at is not None else set()
    problems = {}
    for name in names:
        actual = db.execute(statement(name, columns)).fetchall()
        if name in RECOVERING:
            problem = recovery_problem(name, before[name], actual, columns[name])
            if problem:
                problems[name] = problem
            continue
        expected = expected_rows(name, before, columns, survivors)
        if name in DEPENDENT:
            # Rows of a gone memory may stay until the next maintenance pass, or go now; nothing else changes.
            if not set(expected) <= set(actual) <= set(before[name]):
                problems[name] = {'missing': len(set(expected) - set(actual)),
                                  'unexpected': len(set(actual) - set(before[name]))}
        elif actual != expected:
            problems[name] = {'expected_rows': len(expected), 'actual_rows': len(actual)}
    return problems


def other_problems(db, before_all, grouped):
    """Pre-existing data tables outside the compared groups: every earlier row survives, through its earlier
    columns; a table startup must not fill (migrations.NO_NEW) gains no row either."""
    problems = {}
    for name, (columns, rows) in before_all.items():
        if name in grouped or name == 'upgrade_history':  # upgrade_history is checked row by row elsewhere
            continue
        try:
            actual = db.execute(f'SELECT {quoted(columns)} FROM "{name}"').fetchall()
        except sqlite3.Error as exc:
            problems[name] = {'error': bounded(exc, 200)}
            continue
        old, new = Counter(rows), Counter(actual)
        missing, added = sum((old - new).values()), sum((new - old).values())
        if missing or (added and name in NO_NEW):
            problems[name] = {'missing': missing, 'added': added}
    return problems


def grouped_tables(names):
    """Tables a group compares whole (a WHERE-filtered group, such as permissions, leaves its table to the
    other-rows rule as well)."""
    return {PRESERVED[name][0] for name in names if not PRESERVED[name][2]}


def added_columns(db, columns):
    """{table: (moved, added, filled)} for preserved tables: existing columns must keep their order and an
    added column must be empty on every row (startup creates no rows in these tables)."""
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


# The migration layer's own recovery rules (migrations.PRESERVED), evaluated on minimal rows.
BASE = {
    'screen_sessions': {'session_id': 'a' * 32, 'chat_id': 'c', 'run_id': 'r', 'kind': 'browser', 'target': '{}',
                        'target_sha256': '0' * 64, 'pin': '{}', 'actions': '[]', 'state': None, 'reason': None,
                        'created': 0.0, 'expires': 1.0, 'updated': 1.0},
    'screen_actions': {'action_id': 'a' * 32, 'session_id': 's', 'chat_id': 'c', 'run_id': 'r',
                       'observation_digest': 'd', 'element': '{}', 'action': 'type', 'class': 'input',
                       'text_sha256': None, 'text_chars': None, 'review': None, 'postcondition': '{}', 'state': None,
                       'verdict': None, 'evidence': None, 'created': 0.0, 'updated': 1.0},
    'connector_effects': {'action_id': 'a' * 32, 'chat_id': 'c', 'run_id': 'r', 'plugin_id': 'p', 'tool': 't',
                          'effect_class': 'send', 'assigned': 'person', 'catalogue_digest': 'd', 'request_digest': 'q',
                          'idempotency_key': None, 'recipients_sha256': 'h', 'facts': '{}', 'state': None,
                          'created': 0.0, 'updated': 1.0},
    'export_records': {'id': 'e', 'chat_id': 'c', 'plan_id': 'p', 'format': 'csv', 'source': '{}', 'state': None,
                       'receipt': '{}', 'created': 0.0, 'updated': 1.0},
    'export_saves': {'id': 's', 'export_id': 'e', 'chat_id': 'c', 'state': None, 'path': '/x', 'sha256': None,
                     'verified': 0, 'created': 0.0, 'updated': 1.0},
}
# (table, old fields, new fields, admitted): every new row also carries a later updated time.
CASES = (
    ('screen_sessions', {'state': 'active'}, {'state': 'restarted'}, True),
    ('screen_sessions', {'state': 'interrupted'}, {'state': 'restarted'}, True),
    ('screen_sessions', {'state': 'expired'}, {'state': 'restarted'}, False),
    ('screen_actions', {'state': 'admitted'}, {'state': 'unknown', 'verdict': 'unknown', 'evidence': RESTART_EVIDENCE},
     True),
    ('screen_actions', {'state': 'proposed'}, {'state': 'expired'}, True),
    ('screen_actions', {'state': 'admitted'}, {'state': 'settled', 'verdict': 'occurred'}, False),
    ('screen_actions', {'state': 'settled', 'verdict': 'occurred'}, {'state': 'unknown', 'verdict': 'unknown'}, False),
    ('connector_effects', {'state': 'admitted'}, {'state': 'unknown'}, True),
    ('connector_effects', {'state': 'admitted'}, {'state': 'responded'}, False),
    ('connector_effects', {'state': 'responded'}, {'state': 'unknown'}, False),
    ('export_records', {'state': 'rendering'}, {'state': 'failed', 'receipt': '{"status":"failed"}'}, True),
    ('export_records', {'state': 'verified'}, {'state': 'failed'}, False),
    ('export_saves', {'state': 'writing'}, {'state': 'uncertain'}, True),
    ('export_saves', {'state': 'saved'}, {'state': 'uncertain'}, False),
)


def migration_admits(db, table):
    """Rows of main.table whose backup row b.table the migration layer admits: stable columns equal and the
    transition (if any) holds. db has the backup attached as b."""
    keep = migrations.PRESERVED[table]
    on = ' AND '.join(f'n."{k}" = o."{k}"' for k in keep.keys)
    rule = ' AND '.join(f'n."{c}" IS o."{c}"' for c in keep.stable)
    if keep.transition:
        rule = f'({rule}) AND ({keep.transition})'
    return db.execute(f'SELECT count(*) FROM main.{table} n JOIN b.{table} o ON {on} WHERE {rule}').fetchone()[0]


def transition_cases():
    """[(table, old, new, expected, admitted_by_migration, matches_validator_rule)] for CASES."""
    from pebble import connector_effects, exports, screen_session
    results = []
    with contextlib.closing(sqlite3.connect(':memory:')) as db:
        db.executescript(screen_session.SCREEN_DDL + ';\n'.join(connector_effects.DDL) + ';\n' + exports.EXPORT_DDL)
        db.execute("ATTACH ':memory:' AS b")
        for table in BASE:
            db.execute(f'CREATE TABLE b.{table} AS SELECT * FROM main.{table} WHERE 0')
        for table, old_fields, new_fields, expected in CASES:
            old, new = {**BASE[table], **old_fields}, {**BASE[table], **old_fields, **new_fields, 'updated': 2.0}
            for schema, row in (('b', old), ('main', new)):
                db.execute(f'DELETE FROM {schema}.{table}')
                db.execute(f'INSERT INTO {schema}.{table}({quoted(row)}) VALUES({",".join("?" * len(row))})',
                           tuple(row.values()))
            admitted = migration_admits(db, table) == 1
            if table in RECOVERING:
                columns = list(BASE[table])
                ours = recovery_problem(table, [tuple(old.values())], [tuple(new.values())], columns) is None
            else:
                ours = None  # export rows exist only from epoch 8 on: no fixture can carry them into an upgrade
            results.append((table, old_fields, new_fields, expected, admitted, ours))
    return results


def insert_current_rows(db, now):
    """Settled synthetic current-epoch rows for the restart check: one of each Phase 17 record, in a final
    state no startup step touches. Returns the rows added per table."""
    rid, chat = db.execute("SELECT id,chat_id FROM runs WHERE state='completed' ORDER BY id LIMIT 1").fetchone()
    ids, added = CURRENT_IDS, {}

    def insert(table, **row):
        db.execute(f'INSERT INTO {table}({quoted(row)}) VALUES({",".join("?" * len(row))})', tuple(row.values()))
        added[table] = added.get(table, 0) + 1

    packed = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))
    insert('lifecycle_observations', id=ids['sleep'], kind='sleep', source='clock', observed=now - 300,
           started=now - 900, seconds=600.0, zone=None)
    insert('lifecycle_observations', id=ids['zone'], kind='timezone_changed', source='native', observed=now - 280,
           started=None, seconds=0.0, zone='Europe/London')
    insert('host_notices', id=ids['notice'], chat_id=None, job_id=None, kind='wake_summary',
           fingerprint=hashlib.sha256(b'synthetic-wake').hexdigest(), title='While this Mac slept',
           body='While this Mac slept (about 10 minutes), 1 schedule was due.', created=now - 270,
           acknowledged=now - 260)
    insert('native_banners', id=ids['banner'], scope='job', job_id=ids['job'], chat_id=chat, members=1, version=1,
           state='delivered', delivered_version=1, attempts=1, reason=None, opened=now - 250, posted=now - 249,
           updated=now - 249)
    insert('native_deliveries', notification_id=ids['delivery'], job_id=ids['job'], chat_id=chat, category='completed',
           state='delivered', reason=None, banner_id=ids['banner'], banner_version=1, attempts=1, created=now - 250,
           posted=now - 249, updated=now - 249)
    source = {'kind': 'message', 'id': 'fixture-message', 'revision': None}
    insert('export_records', id=ids['export'], chat_id=chat, plan_id=ids['plan'], format='csv', source=packed(source),
           state='verified', receipt=packed({'status': 'verified', 'format': 'csv', 'checks': []}),
           created=now - 240, updated=now - 239)
    insert('export_saves', id=ids['save'], export_id=ids['export'], chat_id=chat, state='saved',
           path='/private/tmp/pebble-synthetic-export.csv', sha256=hashlib.sha256(b'synthetic-export').hexdigest(),
           verified=1, created=now - 230, updated=now - 229)
    insert('goals', id=ids['goal'], chat_id=chat, origin='human', title='Synthetic fixture report', revision=3,
           state='done', created=now - 220, updated=now - 200)
    insert('goal_criteria', goal_id=ids['goal'], id='c1', ordinal=1, text='The person checked the report.',
           kind='human_check', status='met', origin='human', evidence_ids='[]', checked_by='human',
           proposed_run_id=None, updated=now - 205)
    insert('goal_steps', goal_id=ids['goal'], id='s1', ordinal=1, title='Write the report', status='completed',
           depends_on='[]', criteria='["c1"]', evidence_ids='[]', blocker_kind=None, blocker_ref=None, run_id=rid,
           updated=now - 210)
    for sequence, kind in ((1, 'created'), (2, 'done')):
        insert('goal_events', goal_id=ids['goal'], sequence=sequence, kind=kind, origin='human', run_id=None,
               payload='{}', created=now - 220 + 20 * (sequence - 1))
    return added


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


def restart_unchanged(command, home):
    """Start the current service once more: no upgrade, backup, epoch or data row changes."""
    with connect(home / 'pebble.sqlite') as db:
        before = all_rows(db)
    history, listed = read(home, HISTORY), backups(home)
    service = Service(command, home)
    try:
        assert service.rpc('state')['active'] is None
    finally:
        service.close()
    with connect(home / 'pebble.sqlite') as db:
        after = all_rows(db)
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    assert not changed, ('A restart changed rows.', changed)
    assert read(home, HISTORY) == history, 'A restart recorded another upgrade.'
    assert backups(home) == listed, 'A restart wrote another backup.'
    assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
    return {'tables_compared': len(before), 'rows_compared': sum(len(rows) for _, rows in before.values()),
            'upgrades': len(history), 'backup_files': len(listed)}


class Upgrade:
    def __init__(self, command):
        self.command = command
        self.checks, self.names, self.facts = [], set(), {}
        self.blocked = None  # the pending reason once the epoch-7 fixture is known to be absent

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
        self.check('Phase 16 and 17 tables are read from the backend sources; the migration layer guards each one '
                   'and admits only fail-closed recovery', self.sources_guarded)
        self.check('fixture metadata, file hashes and preserved Phase 16 executable verified', self.verify_fixture)
        if 'metadata' in self.facts:  # never start a service on an unverified copy
            shutil.copytree(FIXTURE, home)
            self.facts['home'] = home
        self.check('private copy holds the six memory layers, the synthetic Phase 12 to 16 rows and the in-flight '
                   'screen acts and connector effect at epoch 7', self.copy_holds_rows)
        self.check('actual service waits in upgrade maintenance while the backup budget is full and changes nothing',
                   self.budget_refusal)
        self.check('oldest retained backup removed through the native maintenance route with its manifest hash',
                   self.discard_oldest)
        self.check('native retry upgrades the private epoch-7 copy to the current epoch once', self.upgrade)
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
        self.check('synthetic Phase 16 research sources, texts, plans, claims, claim rows and conflicts survive '
                   'upgrade exactly', self.kept_research)
        self.check('synthetic Phase 16 screen sessions and acts survive upgrade; only the admitted act, the proposal '
                   'and the live session settle fail-closed', self.kept_screen)
        self.check('synthetic Phase 16 connector effects and confirmations survive upgrade; only the admitted effect '
                   'settles to unknown', self.kept_effects)
        self.check('every other pre-existing row survives; no table startup must not fill gains a row',
                   self.other_rows)
        self.check('preserved tables keep their columns; any added column is empty on preserved rows', self.additive)
        self.check('Phase 17 lifecycle, notification, export and goal tables exist after the upgrade and are empty, '
                   'as is every new table startup must not fill', self.new_tables)
        self.check('verified pre-upgrade backup is epoch 7 and retains the exact preserved rows, in-flight rows '
                   'included', self.backup)
        self.check('upgraded database with settled Phase 17 rows restarts with no upgrade, backup or row change',
                   self.second_start)
        self.check('preserved Phase 16 executable refuses the current epoch before mutating the database', self.refusal)
        for epoch, fixture, metadata in OLDER:
            self.check(f'preserved epoch-{epoch} fixture upgrades to the current epoch once; its rows survive exactly; '
                       'only expired memories go; a restart changes nothing',
                       lambda e=epoch, f=fixture, m=metadata: self.older(e, f, m, home.parent))

    def sources_guarded(self):
        """The judged table sets come from the sources; the migration layer creates, preserves and guards each
        one, and its transition rules admit exactly the fail-closed recovery this validator expects."""
        phase15, phase16, phase17 = epoch6_tables(), epoch7_tables(), epoch8_tables()
        assert set(PHASE15) == phase15 and set(PHASE16) == phase16, (sorted(phase15 ^ set(PHASE15)),
                                                                     sorted(phase16 ^ set(PHASE16)))
        reference = set(CREATED.findall(migrations.REFERENCE_DDL))
        assert phase15 | phase16 | phase17 <= reference, sorted((phase15 | phase16 | phase17) - reference)
        assert all(name in migrations.PRESERVED for name in phase15 | phase16 | phase17)
        assert phase16 | phase17 <= set(NO_NEW), sorted((phase16 | phase17) - set(NO_NEW))
        assert not phase17 & (phase15 | phase16) and SCHEMA_EPOCH > FROM_EPOCH, SCHEMA_EPOCH
        cases = transition_cases()
        wrong = [case[:4] for case in cases if case[4] != case[3] or case[5] not in (None, case[3])]
        assert not wrong, ('The migration rule or this validator disagrees on recovery.', wrong)
        return {'phase15_tables': len(phase15), 'phase16_tables': sorted(phase16), 'phase17_tables': sorted(phase17),
                'current_epoch': SCHEMA_EPOCH, 'transition_cases': len(cases),
                'admitted': sum(case[4] for case in cases)}

    def verify_fixture(self):
        if not os.path.lexists(FIXTURE) and not os.path.lexists(METADATA):  # a dangling link is a failure
            self.blocked = PENDING
            raise Pending(PENDING)
        assert METADATA.is_file() and FIXTURE.is_dir(), 'Only part of the epoch-7 fixture exists.'
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
        assert metadata['source_metadata_sha256'] == sha(PRIOR_METADATA[-1]), 'The epoch-6 record changed.'
        assert metadata['epoch'] == FROM_EPOCH and SCHEMA_EPOCH > FROM_EPOCH, (metadata['epoch'], SCHEMA_EPOCH)
        assert metadata['old_executable_sha256'] == EXPECTED_OLD and sha(OLD) == EXPECTED_OLD, \
            'The preserved Phase 16 executable does not match its recorded SHA-256.'
        self.facts['old_verified'] = True
        assert sha(BUILDER) == metadata['builder_sha256'], 'The fixture builder changed after the fixture was made.'
        assert not FIXTURE.is_symlink() and not any(path.is_symlink() for path in FIXTURE.rglob('*'))
        # Closed SQLite sidecars can disappear after a read-only connection; the
        # builder removed empty ones. Every other file must match the record.
        recorded = {name: digest for name, digest in metadata['files_sha256'].items() if name not in SIDECARS}
        actual = {path.relative_to(FIXTURE).as_posix(): sha(path) for path in FIXTURE.rglob('*')
                  if path.is_file() and path.name not in SIDECARS}
        assert actual == recorded, sorted(set(actual) ^ set(recorded)) or 'A fixture file digest differs.'
        assert wal(FIXTURE) is None, 'The fixture has uncheckpointed WAL frames.'
        assert read(FIXTURE, 'PRAGMA user_version', immutable=True) == [(FROM_EPOCH,)]
        assert set(IN_FLIGHT) <= set(metadata['synthetic_ids']), 'The manifest names no in-flight rows.'
        self.facts['prior_rows'] = prior
        self.facts['ids5'] = json.loads(PRIOR_METADATA[-2].read_text())['synthetic_ids']
        self.facts['ids6'] = json.loads(PRIOR_METADATA[-1].read_text())['synthetic_ids']
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
            everything = all_rows(db)
            counts = {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in metadata['table_rows']}
            ids = metadata['synthetic_ids']
            states = {name: db.execute(f'SELECT state FROM {table} WHERE {key}=?', (ids[name],)).fetchone()
                      for name, (table, key, _, _) in IN_FLIGHT.items()}
        assert counts == metadata['table_rows'], 'Row counts differ from the builder record.'
        self.facts['columns'], self.facts['before'], self.facts['tables_before'] = columns, before, present
        self.facts['all_before'] = everything
        self.facts['history_before'] = read(home, HISTORY, immutable=True)
        assert {row[columns['memories'].index('kind')] for row in before['memories']} >= LAYERS
        # Phase 12-15 rows are counted by the earlier records; Phase 16 rows by this one (only it adds them).
        inherited = {name: len(before[name]) for name in prior if name in before}
        assert all(inherited[name] >= prior[name] for name in inherited), (inherited, prior)
        added = metadata['synthetic_rows']
        # Every epoch-6 and epoch-7 table holds rows, so the comparison after the upgrade is never vacuous.
        assert all(before[name] and len(before[name]) >= added.get(name, 0) for name in {**PHASE15, **PHASE16}), \
            {name: len(before[name]) for name in {**PHASE15, **PHASE16}}
        assert states == {name: (spec[2],) for name, spec in IN_FLIGHT.items()}, states
        ids5, ids6 = self.need('ids5'), self.need('ids6')
        job = dict(zip(columns['process_jobs'], next(r for r in before['process_jobs'] if r[0] == ids5['process'])))
        assert job['contained'] == 1 and job['settled_by'] == 'supervisor_record' and job['reservation_id'] == ids5['reservation']
        evidence = {r[0]: dict(zip(columns['memory_evidence'], r)) for r in before['memory_evidence']}
        assert evidence[ids5['superseded']]['superseded_by'] == ids5['current'], 'The superseded memory link is missing.'
        assert evidence[ids6['fact_old']]['status'] == evidence[ids6['fact_new']]['status'] == 'contested'
        assert not epoch8_tables() & present, 'An epoch-7 database already has Phase 17 tables.'
        return {'memory_layers': sorted(LAYERS), 'inherited_rows': inherited,
                'phase16_rows': {name: len(before[name]) for name in PHASE16},
                'in_flight': {name: spec[2] for name, spec in IN_FLIGHT.items()},
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
                                   ('goal_list', {'chat_id': 'x'}),
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
        settled = {name: settled_count(name, before[name], columns[name]) for name in sorted(names) if name in RECOVERING}
        return {'tables': sorted(names), 'rows_before': {name: len(before[name]) for name in sorted(names)},
                'expired_memories_removed': expired, **({'settled_fail_closed': settled} if settled else {})}

    def kept_phase14(self):
        evidence = self.kept(PHASE14)
        home, columns, ids = self.need('home'), self.need('columns'), self.need('ids5')
        with connect(home / 'pebble.sqlite') as db:
            row = db.execute(f'SELECT {quoted(columns["process_jobs"])} FROM process_jobs WHERE id=?',
                             (ids['process'],)).fetchone()
        job = dict(zip(columns['process_jobs'], row))
        assert (job['state'], job['contained'], job['settled_by'], job['plan_hash']) == (
            'succeeded', 1, 'supervisor_record', 'e' * 64), 'The supervised process job changed.'
        return {**evidence, 'supervised_job_kept': True}

    def kept_phase15(self):
        """The epoch-6 rows survive exactly; the contest, its edge, both salts and the routes stay as they were."""
        evidence = self.kept(PHASE15)
        home, ids = self.need('home'), self.need('ids6')
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

    def kept_research(self):
        """The research rows survive exactly; every stored text still matches its address, and the conflict
        stays open."""
        evidence = self.kept(RESEARCH)
        home, ids = self.need('home'), self.need('metadata')['synthetic_ids']
        with connect(home / 'pebble.sqlite') as db:
            texts = db.execute('SELECT sha256,characters,data FROM research_texts').fetchall()
            conflict = db.execute('SELECT state,claim_values FROM research_conflicts WHERE id=?', (ids['conflict'],)).fetchone()
            orphans = db.execute('SELECT count(*) FROM research_sources s WHERE NOT EXISTS(SELECT 1 FROM research_texts t '
                                 'WHERE t.sha256=s.content_sha256)').fetchone()[0]
        for digest, characters, data in texts:
            raw = zlib.decompress(data)
            assert hashlib.sha256(raw).hexdigest() == digest and len(raw.decode('utf-8')) == characters, digest
        assert conflict and conflict[0] == 'open' and len(json.loads(conflict[1])) == 2, conflict
        assert orphans == 0, 'A source lost its text.'
        return {**evidence, 'texts_verified': len(texts), 'conflict_open': True}

    def kept_screen(self):
        """Settled sessions and acts survive exactly; the in-flight ones settle exactly fail-closed."""
        evidence = self.kept(SCREEN)
        return {**evidence, 'in_flight': self.in_flight_states(('live_session', 'admitted_act', 'proposed_act'))}

    def kept_effects(self):
        evidence = self.kept(EFFECTS)
        return {**evidence, 'in_flight': self.in_flight_states(('admitted_effect',))}

    def in_flight_states(self, names):
        home, ids = self.need('home'), self.need('metadata')['synthetic_ids']
        found = {}
        with connect(home / 'pebble.sqlite') as db:
            for name in names:
                table, key, _, settled = IN_FLIGHT[name]
                extra = ',verdict,evidence' if table == 'screen_actions' else ''
                row = db.execute(f'SELECT state{extra} FROM {table} WHERE {key}=?', (ids[name],)).fetchone()
                assert row and row[0] == settled, (name, row)
                if name == 'admitted_act':
                    assert row[1:] == ('unknown', RESTART_EVIDENCE), row
                found[name] = row[0]
        return found

    def other_rows(self):
        home, everything = self.need('home'), self.need('all_before')
        self.need('backup_id')
        grouped = grouped_tables(PRESERVED)
        with connect(home / 'pebble.sqlite') as db:
            problems = other_problems(db, everything, grouped)
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
        home, before = self.need('home'), self.need('tables_before')
        self.need('backup_id')
        expected = epoch8_tables()
        present = tables(home)
        assert expected <= present, sorted(expected - present)
        problems, evidence = new_tables_problems(home, before)
        assert not problems, problems
        assert expected <= set(evidence['new_tables']), 'A Phase 17 table existed before the upgrade.'
        counts = {name: read(home, f'SELECT count(*) FROM "{name}"')[0][0] for name in sorted(expected)}
        assert not any(counts.values()), counts
        return {**evidence, 'phase17_rows': counts}

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
            assert not names & epoch8_tables(), 'The pre-upgrade backup already holds Phase 17 tables.'
        in_flight = sum(settled_count(name, before[name], columns[name]) for name in RECOVERING)
        assert in_flight == len(IN_FLIGHT), in_flight
        return {'backup_epoch': FROM_EPOCH, 'tables_compared': len(columns), 'in_flight_rows_kept': in_flight}

    def second_start(self):
        """Settled synthetic Phase 17 rows join the upgraded copy; a restart then changes nothing."""
        home, history, listed = self.need('home'), self.need('history'), self.need('backups')
        self.need('backup_id')
        assert read(home, HISTORY) == history and backups(home) == listed
        with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite')) as db:
            with db:
                added = insert_current_rows(db, time.time())
        assert set(added) == epoch8_tables(), sorted(epoch8_tables() ^ set(added))
        evidence = restart_unchanged(self.command, home)
        return {**evidence, 'phase17_rows_added': added}

    def refusal(self):
        home = self.need('home')
        self.need('old_verified')  # never execute an unverified binary
        self.need('backup_id')
        before = (sha(home / 'pebble.sqlite'), wal(home), backups(home), read(home, HISTORY))
        refused = subprocess.run([str(OLD), '--home', str(home), '--resources', str(ROOT / 'src-tauri/resources')],
                                 input='', capture_output=True, text=True, timeout=60)
        assert refused.returncode != 0, 'The Phase 16 executable accepted a newer epoch.'
        assert REFUSAL in refused.stderr, refused.stderr[-600:]
        after = (sha(home / 'pebble.sqlite'), wal(home), backups(home), read(home, HISTORY))
        assert after == before, 'The Phase 16 executable changed the database, its WAL, backups or upgrade history.'
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        return {'exit_code': refused.returncode, 'refused_epoch': SCHEMA_EPOCH, 'database_unchanged': True}

    def older(self, epoch, fixture, metadata_path, scratch):
        """Upgrade an untouched historical fixture (verified first) straight to the current epoch, then restart."""
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
            everything = all_rows(db)
            history_before = db.execute(HISTORY).fetchall() if 'upgrade_history' in before_tables else []
        discarded = upgrade_in_place(self.command, home, metadata.get('files_sha256'))
        assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
        history = read(home, HISTORY)
        assert history[:len(history_before)] == history_before, 'Earlier upgrade history rows changed.'
        assert [(r[1], r[2]) for r in history[len(history_before):]] == [(epoch, SCHEMA_EPOCH)], history[len(history_before):]
        with connect(home / 'pebble.sqlite') as db:
            problems = compare(db, names, before, columns)
            added = added_columns(db, columns)
            others = other_problems(db, everything, grouped_tables(names))
        assert not problems, problems
        assert not added, ('A preserved table changed shape or an added column was filled.', added)
        assert not others, ('Another pre-existing row changed or a guarded table gained a row.', others)
        current = epoch6_tables() | epoch7_tables() | epoch8_tables()
        assert current <= tables(home), sorted(current - tables(home))
        graph, evidence = new_tables_problems(home, before_tables)
        assert not graph, graph
        restart = restart_unchanged(self.command, home)
        expired = len(before['memories']) - len(expected_rows('memories', before, columns, None)) if 'memories' in before else 0
        return {'from_version': epoch, 'to_version': SCHEMA_EPOCH, 'tables_compared': len(names),
                'other_tables_compared': len(set(everything) - grouped_tables(names) - {'upgrade_history'}),
                'maintenance_discarded': discarded, 'expired_memories_removed': expired, **evidence,
                'restart': restart}


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
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase17-upgrade-')).resolve()
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
              'source_sha256': {p: sha(ROOT / p) for p in (
                  'scripts/validate_phase17_upgrade.py', 'scripts/validate_runtime.py', 'backend/pebble/migrations.py',
                  'backend/pebble/memory_graph.py', 'backend/pebble/memory_learning.py', 'backend/pebble/maintenance.py',
                  'backend/pebble/source_ledger.py', 'backend/pebble/research_store.py', 'backend/pebble/screen_session.py',
                  'backend/pebble/connector_effects.py', 'backend/pebble/lifecycle.py', 'backend/pebble/notifications.py',
                  'backend/pebble/exports.py', 'backend/pebble/goals.py', 'backend/pebble/jobs.py',
                  'backend/pebble/app.py')},
              'synthetic_only': True, 'inference_calls': 0, 'credentials_used': False}
    descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'kind': result['kind'], 'checks': len(result['checks']), 'failed': result['failed'],
                      'pending': len(result['pending']), 'fatal_error': fatal, 'passed': passed}))
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()


# Development self-checks, run only by pytest on this file; the validator never
# calls them. They read the parent-owned fixtures and work on copies under
# pytest's tmp_path. Neither the service nor the Phase 16 executable is started,
# except by the two explicit-request campaign tests at the end.
# Checks that need the epoch-7 fixture skip, naming the reason, until the parent
# has built it with the preserved Phase 16 candidate.

CHECK_COUNT = 21 + len(OLDER)
EPOCH7_CHECKS = 20  # every check except the source check and the older-fixture checks


def _need_epoch7_fixture():
    import pytest
    if not (FIXTURE.is_dir() and METADATA.is_file()):
        pytest.skip('The parent has not built .runtime/phase17-epoch7-fixture yet (build_epoch7_fixture.py).')


def _builder():
    import importlib.util
    spec = importlib.util.spec_from_file_location('build_epoch7_fixture', BUILDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _epoch7_copy(tmp_path):
    """The real epoch-6 database plus the source epoch-7 DDL: what the Phase 16 candidate's startup makes of
    the epoch-6 fixture before the builder's inserts (its new tables are empty)."""
    from pebble import connector_effects, research_store, screen_session, source_ledger
    home = tmp_path / 'home'
    home.mkdir()
    copy = home / 'pebble.sqlite'
    with connect(OLDER[-1][1] / 'pebble.sqlite', immutable=True) as source, contextlib.closing(sqlite3.connect(copy)) as db:
        source.backup(db)
        db.executescript(source_ledger.RESEARCH_DDL + research_store.DDL + screen_session.SCREEN_DDL
                         + ';\n'.join(connector_effects.DDL) + ';')
    return home


def _store(home):
    from pebble.store import Store
    return Store(home)


def test_parent_fixture_and_preserved_executable_verify():
    _need_epoch7_fixture()
    upgrade = Upgrade(['unused'])
    assert upgrade.check('fixture', upgrade.verify_fixture), upgrade.checks[-1].get('error')
    assert upgrade.facts['old_verified'] is True and upgrade.facts['metadata']['epoch'] == FROM_EPOCH
    assert set(upgrade.facts['metadata']['synthetic_rows']) >= set(PHASE16)


def test_private_copy_holds_the_preserved_rows(tmp_path):
    _need_epoch7_fixture()
    upgrade = Upgrade(['unused'])
    assert upgrade.check('fixture', upgrade.verify_fixture), upgrade.checks[-1].get('error')
    home = tmp_path / 'home'
    shutil.copytree(FIXTURE, home)
    upgrade.facts['home'] = home
    assert upgrade.check('copy', upgrade.copy_holds_rows), upgrade.checks[-1].get('error')
    assert upgrade.facts['history_before'][-1][1:3] == (6, FROM_EPOCH)
    assert sum(name.endswith('.sqlite') for name in backups(home)) >= Limits().max_backups
    assert sha(home / 'pebble.sqlite') == upgrade.facts['metadata']['files_sha256']['pebble.sqlite'], 'A read changed the copy.'


def test_explicit_discard_needs_the_listed_manifest_hash(tmp_path):
    _need_epoch7_fixture()
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


def test_epoch_7_checks_are_pending_until_the_fixture_exists(monkeypatch, tmp_path):
    """Without the epoch-7 fixture its checks report pending with the reason; every other check still runs."""
    monkeypatch.setattr(sys.modules[__name__], 'FIXTURE', tmp_path / 'absent-fixture')
    monkeypatch.setattr(sys.modules[__name__], 'METADATA', tmp_path / 'absent-fixture.json')
    ran = []

    class Probe(Upgrade):
        def older(self, epoch, fixture, metadata_path, scratch):
            ran.append(epoch)
            return {'epoch': epoch}

    probe = Probe(['unused'])
    (tmp_path / 'scratch').mkdir()
    probe.run(tmp_path / 'scratch' / 'home')
    results = {c['name']: c for c in probe.checks}
    pending = [c for c in probe.checks if c.get('pending')]
    assert len(probe.checks) == CHECK_COUNT and len(pending) == EPOCH7_CHECKS
    assert all(c['reason'] == PENDING and not c['passed'] and 'error' not in c for c in pending)
    assert ran == [epoch for epoch, _, _ in OLDER] and probe.checks[0]['passed'], probe.checks[0]
    assert all(results[name]['passed'] for name in results if name.startswith('preserved epoch-'))
    assert not (tmp_path / 'scratch' / 'home').exists(), 'A pending run copied a fixture.'
    # Half a fixture is a failure, never pending.
    (tmp_path / 'absent-fixture').mkdir()
    half = Upgrade(['unused'])
    assert not half.check('fixture', half.verify_fixture) and 'pending' not in half.checks[-1]
    assert 'Only part' in half.checks[-1]['error']


def test_preserved_phase16_executable_matches_its_recorded_hash():
    assert sha(OLD) == EXPECTED_OLD


def test_older_fixtures_verify_against_their_records():
    for epoch, fixture, metadata in OLDER:
        assert verify_older(epoch, fixture, metadata)['epoch'] == epoch
    # The epoch-4, 5 and 6 budgets are full, so their upgrades go through the native maintenance route.
    for _, fixture, _ in OLDER[3:]:
        assert sum(name.endswith('.sqlite') for name in backups(fixture)) >= Limits().max_backups


def test_table_names_match_the_backend_sources():
    from pebble import exports, goals, lifecycle, notifications
    phase15, phase16, phase17 = epoch6_tables(), epoch7_tables(), epoch8_tables()
    assert set(PHASE15) == phase15 and set(PHASE16) == phase16
    assert phase17 == {'lifecycle_observations', 'native_deliveries', 'native_banners', 'host_notices',
                       'export_records', 'export_saves', 'goals', 'goal_criteria', 'goal_steps', 'goal_events'}
    # The parent's epoch-8 migration creates, preserves and guards every one of them; it pins nothing here.
    assert phase17 <= set(CREATED.findall(migrations.REFERENCE_DDL))
    assert all(name in migrations.PRESERVED for name in phase17) and phase17 <= set(NO_NEW)
    assert migrations.REFERENCE_DDL.count(lifecycle.LIFECYCLE_DDL.strip()) == 1
    assert all(ddl.strip()[:40] in migrations.REFERENCE_DDL for ddl in (notifications.NOTIFY_DDL, exports.EXPORT_DDL,
                                                                         goals.GOAL_DDL))
    assert set(RECOVERING) <= phase16 and set(IN_FLIGHT[name][0] for name in IN_FLIGHT) <= set(RECOVERING)
    assert SCHEMA_EPOCH > FROM_EPOCH  # the subject of this validator; never pinned to one number


def test_migration_layer_admits_only_fail_closed_recovery():
    cases = transition_cases()
    assert len(cases) == len(CASES)
    for table, old, new, expected, admitted, ours in cases:
        assert admitted == expected, (table, old, new)
        assert ours in (None, expected), (table, old, new)
    assert {case[0] for case in cases if case[4]} == set(BASE)


def test_builder_helpers_match_the_backend():
    from pebble import ax_worker, connector_effects, research, research_store, screen_session, source_ledger
    builder = _builder()
    assert builder.EXPECTED_OLD == EXPECTED_OLD and builder.OUT == FIXTURE and builder.META == METADATA
    assert set(builder.EPOCH7) == epoch7_tables() and set(builder.EPOCH6) == epoch6_tables()
    value = {'b': [1, 'é'], 'a': None}
    assert builder.canonical(value) == screen_session._canonical(value)
    assert builder.packed(value) == connector_effects.packed(value)
    assert builder.recipients_sha256([builder.RECIPIENT]) == connector_effects.sha256([builder.RECIPIENT])
    assert builder.text_sha('fixture feedback') == ax_worker.value_digest('fixture feedback')
    for n, url, domain, _, text in builder.PAGES:
        assert source_ledger.registrable('docs.example.org' if n == 1 else 'archive.example.net') == domain
        assert source_ledger.host_class(url) == builder.PRIMARY_CLASS
        assert builder.stored_text(text)[:2] == (hashlib.sha256(text.encode()).hexdigest(), len(text))
    target = builder.browser_target('tab-17b')
    assert screen_session.validate_target(target) == target
    assert builder.session_row('x', 'c', 'r', 'tab-17b', 'active', 0, 1, 0)[5] == ax_worker.sha256(
        screen_session._canonical(target))
    row = json.loads(builder.claim_row(builder.IDS['row_uncited'], 'A sentence.', 'uncited', []))
    assert set(row) == research.CLAIM_FIELDS and research.CLAIM_ID.fullmatch(row['claim_id'])
    assert research_store._value('2024') == '2024' and research_store._key(builder.CLAIM_KEY) == builder.CLAIM_KEY
    assert set(builder.IN_FLIGHT) == set(IN_FLIGHT)
    assert {name: spec[2] for name, spec in IN_FLIGHT.items()} == builder.IN_FLIGHT
    assert {name: spec[3] for name, spec in IN_FLIGHT.items()} == builder.SETTLES_TO
    assert all(screen_session.HEX32.fullmatch(builder.IDS[name]) for name in IN_FLIGHT)


def test_builder_rows_fit_the_real_epoch_7_schema(tmp_path):
    """The builder's inserts on what the candidate makes of the real epoch-6 database; the backend's own readers
    accept them, and its own startup recovery settles exactly the in-flight rows, as this validator and the
    migration layer both expect."""
    from pebble import research_store, screen_session
    from pebble.connector_effects import ConnectorEffects
    from pebble.research_store import ResearchStore
    from pebble.source_ledger import SourceLedger
    builder = _builder()
    home = _epoch7_copy(tmp_path)
    now = time.time() - 1800
    with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite')) as db:
        with db:
            settled = builder.insert_settled(db, now)
        held_settled = builder.rows_of(db)
        with db:
            flying = builder.insert_in_flight(db, now + 1700)
        assert settled == {'research_plans': 1, 'research_texts': 2, 'research_sources': 2, 'research_claims': 2,
                           'research_conflicts': 1, 'research_claim_rows': 2, 'screen_sessions': 1, 'screen_actions': 2,
                           'connector_effect_confirmations': 1, 'connector_effects': 2}, settled
        assert flying == {'screen_sessions': 1, 'screen_actions': 2, 'connector_effect_confirmations': 1,
                          'connector_effects': 1}, flying
        assert builder.states(db) == builder.IN_FLIGHT
        # The in-flight insert adds only in-flight rows (and the admitted send's confirmation).
        assert builder.without_in_flight(builder.rows_of(db)) == builder.without_in_flight(held_settled)
        held = builder.rows_of(db)
        columns = {name: columns_of(db, PRESERVED[name][0]) for name in PHASE16}
        before = snapshot(db, columns)
    shutil.copy2(home / 'pebble.sqlite', tmp_path / 'before.sqlite')
    store = _store(home)
    try:
        rid, chat = store.query("SELECT id,chat_id FROM runs WHERE state='completed' ORDER BY id LIMIT 1")[0].values()
        ledger = SourceLedger(store)
        for n, url, domain, _, text in builder.PAGES:
            assert ledger.text(rid, f'S{n}') == text
            described = ledger.get(rid, f'S{n}')
            assert described['domain'] == domain and described['url'] == url and described['kind'] == 'page'
        claims = ledger.claims(rid)
        assert [c['value'] for c in claims] == ['2024', '2025'] and all(c['match'] == 'exact' for c in claims)
        conflicts = ledger.conflicts(rid, state='open')
        assert len(conflicts) == 1 and conflicts[0]['values'] == ['2024', '2025'] and len(conflicts[0]['sides']) == 2
        assert ledger.plan(rid, builder.IDS['plan'])['questions'] == [builder.QUESTION]
        kept = ResearchStore(ledger).claims(rid, origins=research_store.ORIGINS)
        assert [row['support'] for row in kept] == ['paraphrase_unverified', 'uncited']
        for (target,) in store.db.execute('SELECT target FROM screen_sessions'):
            assert screen_session.validate_target(json.loads(target)) == json.loads(target)
        # The backend's own startup recovery: exactly the in-flight rows settle.
        screens = screen_session.ScreenSessions(store, adapters={})
        assert screens.recovered == {'unknown_acts': 1, 'expired_proposals': 1, 'ended_sessions': 1}
        ConnectorEffects(store)
        assert builder.states(store.db) == builder.SETTLES_TO
        assert builder.without_in_flight(builder.rows_of(store.db)) == builder.without_in_flight(held)
        assert compare(store.db, sorted(PHASE16), before, columns) == {}
        store.db.execute("ATTACH ? AS b", ((tmp_path / 'before.sqlite').as_posix(),))
        for table in RECOVERING:
            total = store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
            assert migration_admits(store.db, table) == total, table
        store.db.execute('DETACH b')
        assert chat
    finally:
        store.db.close()


def test_current_rows_fit_the_current_schema_and_survive_a_source_restart(tmp_path):
    """The restart check's Phase 17 rows: every epoch-8 table gets one, and the backend's own constructors
    (which run at every startup) leave them unchanged."""
    from types import SimpleNamespace
    from pebble.exports import Exports
    from pebble.goals import Goals
    from pebble.lifecycle import Lifecycle
    from pebble.notifications import Notifications
    from pebble import exports, goals, lifecycle, notifications
    home = _epoch7_copy(tmp_path)
    with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite')) as db:
        db.executescript(lifecycle.LIFECYCLE_DDL + notifications.NOTIFY_DDL + exports.EXPORT_DDL + goals.GOAL_DDL)
        with db:
            added = insert_current_rows(db, time.time())
        assert set(added) == epoch8_tables() and added['goal_events'] == 2, added
        before = all_rows(db)
    store = _store(home)
    try:
        app = SimpleNamespace(store=store, emit=lambda *unused: None)
        Lifecycle(app), Notifications(app), Goals(app), Exports(home, store, None)
        after = all_rows(store.db)
    finally:
        store.db.close()
    assert {name: after[name] for name in epoch8_tables()} == {name: before[name] for name in epoch8_tables()}


def test_recovery_rule_refuses_any_other_change(tmp_path):
    path = tmp_path / 'pebble.sqlite'
    from pebble.screen_session import SCREEN_DDL
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.executescript(SCREEN_DDL)
        for ident, state in (('a' * 32, 'admitted'), ('b' * 32, 'proposed'), ('c' * 32, 'settled')):
            db.execute(f'INSERT INTO screen_actions({quoted(BASE["screen_actions"])}) VALUES({",".join("?" * 17)})',
                       tuple({**BASE['screen_actions'], 'action_id': ident, 'state': state}.values()))
    with connect(path) as db:
        columns = {'screen_actions': columns_of(db, 'screen_actions')}
        before = snapshot(db, columns)

    def outcome(*statements):
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            for sql in statements:
                db.execute(sql)
        with connect(path) as db:
            return compare(db, ['screen_actions'], before, columns)

    assert outcome() == {'screen_actions': {'missing': 0, 'unexpected': 0, 'changed': 2}}  # not yet recovered
    assert outcome("UPDATE screen_actions SET state='unknown',verdict='unknown',evidence='" + RESTART_EVIDENCE
                   + "',updated=5 WHERE state='admitted'",
                   "UPDATE screen_actions SET state='expired',updated=5 WHERE state='proposed'") == {}
    assert outcome("UPDATE screen_actions SET updated=6 WHERE state='settled'")['screen_actions']['changed'] == 1
    assert outcome("UPDATE screen_actions SET updated=5 WHERE state='settled'",
                   "UPDATE screen_actions SET updated=0.5 WHERE state='expired'")['screen_actions']['changed'] == 2
    assert outcome("UPDATE screen_actions SET updated=1 WHERE state='settled'",
                   "UPDATE screen_actions SET updated=5 WHERE state='expired'",
                   "DELETE FROM screen_actions WHERE state='unknown'") == {
        'screen_actions': {'missing': 1, 'unexpected': 0, 'changed': 0}}


def test_new_tables_rule_refuses_any_other_startup_row(tmp_path):
    path = tmp_path / 'pebble.sqlite'
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE memory_evidence(memory_id TEXT, superseded_by TEXT)')
        db.execute("INSERT INTO memory_evidence VALUES('old','new')")
    before = tables(tmp_path)
    from pebble.goals import GOAL_DDL
    from pebble.lifecycle import LIFECYCLE_DDL
    from pebble.memory_graph import GRAPH_DDL
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.executescript(GRAPH_DDL + LIFECYCLE_DDL + GOAL_DDL + 'CREATE TABLE exports_meta(version INTEGER);')
        db.execute("INSERT INTO memory_relations VALUES('new','old','supersedes','{\"basis\":\"superseded_by\"}',0)")
        db.execute("INSERT INTO memory_scope_salts VALUES('abc','" + 'b' * 64 + "','local',0)")
        db.execute('INSERT INTO exports_meta VALUES(1)')
    problems, evidence = new_tables_problems(tmp_path, before)
    assert problems == {} and evidence['unjudged_rows'] == {'exports_meta': 1}, (problems, evidence)
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO lifecycle_observations VALUES('x','sleep','clock',1,0,1,NULL)")
        db.execute("INSERT INTO goals VALUES('g','c','human','t',1,'active',0,0)")
    problems = new_tables_problems(tmp_path, before)[0]
    assert set(problems) == {'lifecycle_observations', 'goals'}, problems


def test_other_rows_rule_keeps_every_row_and_guards_no_new_tables(tmp_path):
    path = tmp_path / 'pebble.sqlite'
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)')
        db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE memory_watermarks(key TEXT PRIMARY KEY, through REAL)')
        db.execute('CREATE VIRTUAL TABLE memory_search USING fts5(content)')
        db.execute('CREATE TABLE search_index_meta(key TEXT PRIMARY KEY, value)')
        db.execute("INSERT INTO settings VALUES('a','1')")
        db.execute("INSERT INTO memory_watermarks VALUES('all',5)")
        db.execute("INSERT INTO memory_search VALUES('x')")
        db.execute("INSERT INTO search_index_meta VALUES('schema',1)")
    with connect(path) as db:
        everything = all_rows(db)
    assert set(everything) == {'settings', 'jobs', 'memory_watermarks'}

    def outcome(sql):
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute(sql)
        with connect(path) as db:
            return other_problems(db, everything, set())

    assert outcome("INSERT INTO settings VALUES('b','2')") == {}  # an ordinary table may gain rows
    assert outcome("UPDATE search_index_meta SET value=2") == {}  # a derived cache is not judged
    assert outcome("INSERT INTO jobs VALUES('forged')") == {'jobs': {'missing': 0, 'added': 1}}
    assert outcome("DELETE FROM jobs") == {}
    assert outcome("UPDATE memory_watermarks SET through=6") == {'memory_watermarks': {'missing': 1, 'added': 1}}
    assert grouped_tables(CORE) == {'memories', 'checkpoints', 'messages', 'chats'}  # settings stays judged


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
        db.execute('CREATE TABLE research_claims(id TEXT PRIMARY KEY, "match" TEXT)')
        db.execute("INSERT INTO research_claims VALUES('a','exact')")
    with connect(path) as db:
        columns = {'research_claims': columns_of(db, 'research_claims')}
        before = db.execute(statement('research_claims', columns)).fetchall()
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute('ALTER TABLE research_claims ADD COLUMN "row" TEXT')
    with connect(path) as db:
        assert db.execute(statement('research_claims', columns)).fetchall() == before
        assert added_columns(db, columns) == {}
    with contextlib.closing(sqlite3.connect(path)) as db, db:
        db.execute('UPDATE research_claims SET "row"=\'x\'')
    with connect(path) as db:
        assert added_columns(db, columns)['research_claims']['filled'] == 1


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


def test_dependent_checks_never_run_without_their_prerequisites():
    steps = lambda upgrade: (
        ('budget', upgrade.budget_refusal), ('discard', upgrade.discard_oldest), ('refusal', upgrade.refusal),
        ('backup', upgrade.backup), ('second', upgrade.second_start), ('core', lambda: upgrade.kept(CORE)),
        ('phase14', upgrade.kept_phase14), ('phase15', upgrade.kept_phase15), ('research', upgrade.kept_research),
        ('screen', upgrade.kept_screen), ('effects', upgrade.kept_effects), ('other', upgrade.other_rows),
        ('additive', upgrade.additive), ('tables', upgrade.new_tables), ('upgrade', upgrade.upgrade))
    upgrade = Upgrade(['unused'])
    for name, fn in steps(upgrade):
        assert upgrade.check(name, fn) is False and 'Prerequisite' in upgrade.checks[-1]['error'], name
    try:
        upgrade.check('refusal', upgrade.refusal)
    except AssertionError:
        pass
    else:
        raise AssertionError('A duplicate check name was accepted.')
    waiting = Upgrade(['unused'])
    waiting.blocked = PENDING
    for name, fn in steps(waiting):
        assert waiting.check(name, fn) is False and waiting.checks[-1].get('pending') is True, name


def test_source_upgrade_run_on_explicit_request(request, tmp_path):
    """The whole validator in source mode, as a subprocess with its scratch under tmp_path.

    It starts the actual service (and, once the epoch-7 fixture exists, the preserved Phase 16 executable),
    so it runs only when this node ID is named explicitly; the default collection skips it. Without the
    epoch-7 fixture the run cannot pass: its checks must be pending and every other check must pass.
    """
    import pytest
    if not any(str(arg).endswith('::test_source_upgrade_run_on_explicit_request') for arg in request.config.args):
        pytest.skip('Starts the actual service; name this test explicitly to run it.')
    report, log = tmp_path / 'report.json', tmp_path / 'upgrade.log'
    with log.open('w') as sink:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--report', str(report)], cwd=ROOT,
                              env={**os.environ, 'TMPDIR': str(tmp_path)}, stdout=sink, stderr=subprocess.STDOUT,
                              timeout=400)
    outcome = json.loads(report.read_text())
    errors = json.dumps({'failed': outcome['failed'], 'fatal': outcome['fatal_error'],
                         'errors': [c.get('error') for c in outcome['check_results'] if c.get('error')]})
    assert outcome['kind'] == 'source' and outcome['fatal_error'] is None and not outcome['failed'], errors
    names = outcome['checks'] + outcome['pending']
    assert len(names) == len(set(names)) == CHECK_COUNT
    if FIXTURE.is_dir() and METADATA.is_file():
        assert done.returncode == 0 and outcome['passed'] is True and not outcome['pending'], errors
    else:
        assert done.returncode == 1 and outcome['passed'] is False and len(outcome['pending']) == EPOCH7_CHECKS
        assert all(c['reason'] == PENDING for c in outcome['check_results'] if c.get('pending'))
    import warnings
    warnings.warn('validator result line: ' + log.read_text().strip().splitlines()[-1])  # shown in the -q summary
    warnings.warn('passed checks: ' + json.dumps(outcome['checks']))
    warnings.warn('pending checks: ' + json.dumps(outcome['pending']))


def test_epoch_7_path_on_a_simulated_fixture_on_explicit_request(request, tmp_path):
    """Every epoch-7 check except the two that need the parent's fixture and the Phase 16 executable
    (verification and refusal), against the actual source service, on a simulated epoch-7 copy: the epoch-6
    fixture plus the source epoch-7 DDL and the builder's own settled and in-flight rows, stamped epoch 7.
    It is not the parent's fixture: no Phase 16 executable runs, so the 6->7 history row is absent and the
    maintenance route discards the 3->4 backup. Starts the actual service, so it runs only on explicit request.
    """
    import pytest
    import warnings
    if not any(str(arg).endswith('::test_epoch_7_path_on_a_simulated_fixture_on_explicit_request')
               for arg in request.config.args):
        pytest.skip('Starts the actual service; name this test explicitly to run it.')
    from pebble import connector_effects, research_store, screen_session, source_ledger
    builder = _builder()
    home = tmp_path / 'home'
    shutil.copytree(OLDER[-1][1], home)
    with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite')) as db:
        db.executescript(source_ledger.RESEARCH_DDL + research_store.DDL + screen_session.SCREEN_DDL
                         + ';\n'.join(connector_effects.DDL) + ';')
        with db:
            added = builder.insert_settled(db, time.time() - 1800)
            for table, count in builder.insert_in_flight(db, time.time() - 100).items():
                added[table] = added.get(table, 0) + count
        db.execute(f'PRAGMA user_version={FROM_EPOCH}')
        rows = builder.table_rows(db)
    prior = {}
    for path in PRIOR_METADATA:
        for name, count in json.loads(path.read_text())['synthetic_rows'].items():
            prior[name] = prior.get(name, 0) + count
    upgrade = Upgrade([sys.executable, str(ROOT / 'backend/main.py')])
    upgrade.facts.update(
        home=home, prior_rows=prior, ids5=json.loads(PRIOR_METADATA[-2].read_text())['synthetic_ids'],
        ids6=json.loads(PRIOR_METADATA[-1].read_text())['synthetic_ids'],
        metadata={'synthetic_ids': builder.IDS, 'synthetic_rows': added, 'table_rows': rows,
                  'files_sha256': {p.relative_to(home).as_posix(): sha(p) for p in home.rglob('*') if p.is_file()}})
    for name, fn in (('copy', upgrade.copy_holds_rows), ('budget', upgrade.budget_refusal),
                     ('discard', upgrade.discard_oldest), ('upgrade', upgrade.upgrade),
                     ('core', lambda: upgrade.kept(CORE)), ('phase12', lambda: upgrade.kept(PHASE12)),
                     ('phase13', lambda: upgrade.kept(PHASE13)), ('phase14', upgrade.kept_phase14),
                     ('learning', lambda: upgrade.kept(LEARNING)), ('phase15', upgrade.kept_phase15),
                     ('research', upgrade.kept_research), ('screen', upgrade.kept_screen),
                     ('effects', upgrade.kept_effects), ('other', upgrade.other_rows), ('additive', upgrade.additive),
                     ('tables', upgrade.new_tables), ('backup', upgrade.backup), ('second', upgrade.second_start)):
        upgrade.check(name, fn)
    warnings.warn('simulated epoch-7 path: ' + json.dumps([{k: c.get(k) for k in ('name', 'passed', 'evidence', 'error')}
                                                           for c in upgrade.checks])[:12000])
    assert len(upgrade.checks) == EPOCH7_CHECKS - 2
    assert all(c['passed'] for c in upgrade.checks), [(c['name'], c.get('error')) for c in upgrade.checks
                                                      if not c['passed']]


def test_older_fixture_upgrades_on_explicit_request(request, tmp_path):
    """The six older-fixture checks against the actual source service, without the epoch-7 fixture.

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
                                                   for c in upgrade.checks])[:12000])
    assert all(c['passed'] for c in upgrade.checks), [c.get('error') for c in upgrade.checks if not c['passed']]
