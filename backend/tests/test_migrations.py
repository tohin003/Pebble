"""Pre-upgrade safety on real temporary SQLite/WAL homes; never a live home or credential.

App runs prepare() before Store and complete() after every subsystem constructor,
so App startups here exercise the integrated path. Forged changes between the two
use the documented seam: Upgrade.complete is wrapped to mutate the live database
first, exactly where a faulty or hostile startup step would act.
"""

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import closing
from types import SimpleNamespace

import pytest

from pebble import migrations
from pebble.agent_effects import AgentEffects
from pebble.app import App
from pebble.memory import KINDS
from pebble.migrations import BACKUPS, DATABASE, Limits, MigrationRefused
from pebble.permissions import DEFAULT
from pebble.runtime import encoded


pytestmark = pytest.mark.skipif(not migrations.supported(), reason='needs POSIX descriptor-relative file operations')

SENTINEL = 'sentinel-user-text-5c1f'
NOW = time.time()
CHAT, OTHER, WAL_CHAT, RUN, RUN2, JOB, FIRE, EFFECT, ACTION, APPLIED = (uuid.UUID(int=n).hex for n in range(1, 11))
OLD_TOOLS = {'files': True, 'edits': False, 'tests': True, 'apps': False, 'web': False, 'terminal': False}
APPLIED_RESULT = {'exit_code': 0, 'output': 'done'}
PROFILES = [
    {'id': 'fixture-route', 'name': 'Fixture route', 'kind': 'openai', 'url': 'http://127.0.0.1:9/v1',
     'model': 'fixture-model', 'context': 8192},
    {'id': 'capacity-route', 'name': 'Capacity route', 'kind': 'openai', 'url': 'https://api.openai.com/v1',
     'model': 'gpt-6-sol', 'context': 131072},
]
SETTINGS = {'permissions': {'mode': 'ask', 'tools': OLD_TOOLS}, 'memory_enabled': True,
            'memory_initialized': True, 'theme': 'dark', 'model_profiles': PROFILES}
CONFIG = json.dumps({'limits': {'max_steps': 4, 'max_seconds': 30}, 'permissions': SETTINGS['permissions']})
GRANT = {'kind': 'oauth', 'resource': 'https://resource.invalid', 'issuer': 'https://issuer.invalid',
         'client_id': 'fixture-client', 'scopes': ['read'], 'environment_names': [], 'endpoints': {},
         'base_digest': 'e' * 64}
JOBS = '''CREATE TABLE jobs(
  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin='human'),
  label TEXT NOT NULL, task TEXT NOT NULL, schedule TEXT NOT NULL, monitor INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','paused','needs_review','finished')),
  revision INTEGER NOT NULL, pin TEXT NOT NULL, pin_fingerprint TEXT NOT NULL,
  anchor REAL NOT NULL, next_due REAL, last_scheduled REAL, last_fingerprint TEXT,
  review_reason TEXT, deferred_reason TEXT, created REAL NOT NULL, updated REAL NOT NULL);'''
# A Phase 9-era database: core, six memory layers, owned receipts, reviewed pins
# and grants, older additive shapes, and data tables this build does not know.
LEGACY = '''
CREATE TABLE chats(id TEXT PRIMARY KEY,title TEXT,workspace TEXT,updated REAL);
CREATE TABLE messages(id TEXT PRIMARY KEY,chat_id TEXT,role TEXT,content TEXT,created REAL);
CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE actions(id TEXT PRIMARY KEY,chat_id TEXT,kind TEXT,status TEXT,payload TEXT,created REAL);
CREATE TABLE memories(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
  scope TEXT NOT NULL, chat_id TEXT, origin TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL, expires REAL);
CREATE INDEX memories_scope ON memories(scope,kind,updated);
CREATE VIRTUAL TABLE memory_search USING fts5(id UNINDEXED,content,tokenize='unicode61');
CREATE TRIGGER memory_insert AFTER INSERT ON memories BEGIN
  INSERT INTO memory_search(id,content) VALUES(new.id,new.content); END;
CREATE TRIGGER memory_delete AFTER DELETE ON memories BEGIN
  DELETE FROM memory_search WHERE id=old.id; END;
CREATE TRIGGER memory_update AFTER UPDATE OF content ON memories BEGIN
  DELETE FROM memory_search WHERE id=old.id;
  INSERT INTO memory_search(id,content) VALUES(new.id,new.content); END;
CREATE TABLE checkpoints(chat_id TEXT PRIMARY KEY,payload TEXT,updated REAL);
CREATE TABLE metrics(message_id TEXT PRIMARY KEY,chat_id TEXT,payload TEXT);
CREATE TABLE runtime_meta(version INTEGER NOT NULL);
INSERT INTO runtime_meta VALUES(1);
CREATE TABLE runs(
  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, goal TEXT NOT NULL,
  state TEXT NOT NULL, config TEXT NOT NULL, checkpoint TEXT,
  steps INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0,
  error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE run_events(
  run_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(run_id,sequence));
CREATE TABLE run_tools(
  run_id TEXT NOT NULL, call_id TEXT NOT NULL, name TEXT NOT NULL,
  arguments TEXT NOT NULL, state TEXT NOT NULL, result TEXT, PRIMARY KEY(run_id,call_id));
CREATE TABLE run_action_owners(
  action_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,call_id TEXT NOT NULL,kind TEXT NOT NULL);
CREATE TABLE connector_grants(plugin_id TEXT PRIMARY KEY,grant_id TEXT,revision INTEGER,state TEXT,metadata TEXT,updated REAL);
CREATE TABLE connector_registrations(registration_id TEXT PRIMARY KEY,plugin_id TEXT,issuer TEXT,resource TEXT,base_digest TEXT,plan_digest TEXT,state TEXT,grant_id TEXT,metadata TEXT,updated REAL);
CREATE TABLE job_meta(version INTEGER NOT NULL);
INSERT INTO job_meta VALUES(1);
''' + JOBS + '''
CREATE TABLE job_fires(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, chat_id TEXT NOT NULL, revision INTEGER NOT NULL,
  scheduled REAL NOT NULL, missed INTEGER NOT NULL, origin TEXT NOT NULL CHECK(origin='host'),
  status TEXT NOT NULL CHECK(status IN ('claimed','bound','running','waiting','attention','completed',
    'failed','cancelled','needs_review','blocked','dismissed')),
  reason TEXT, run_id TEXT UNIQUE, run_state TEXT, pin_fingerprint TEXT NOT NULL,
  result_fingerprint TEXT, label_message_id TEXT, claimed REAL NOT NULL, updated REAL NOT NULL,
  UNIQUE(job_id,scheduled,revision));
CREATE TABLE mods_meta(version INTEGER NOT NULL);
INSERT INTO mods_meta VALUES(1);
CREATE TABLE mods(
  id TEXT PRIMARY KEY,name TEXT NOT NULL,manifest TEXT NOT NULL,digest TEXT NOT NULL,
  enabled INTEGER NOT NULL,grant_digest TEXT,error TEXT,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE memory_candidates(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, chat_id TEXT NOT NULL,
  kind TEXT NOT NULL, scope TEXT NOT NULL, slot TEXT NOT NULL,
  content TEXT NOT NULL, fingerprint TEXT NOT NULL, evidence TEXT NOT NULL,
  input_id TEXT, expires REAL, supersedes TEXT, status TEXT NOT NULL,
  reason TEXT, memory_id TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE agent_effects(
  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,chat_id TEXT NOT NULL,agent_id TEXT NOT NULL,
  generation INTEGER NOT NULL,call_id TEXT NOT NULL,tool TEXT NOT NULL,arguments TEXT NOT NULL,
  args_hash TEXT NOT NULL,purpose TEXT,workspace TEXT,route_identity TEXT,binding TEXT NOT NULL,
  permission_mode TEXT NOT NULL,status TEXT NOT NULL,root_call_id TEXT,effect_call_id TEXT,
  action_id TEXT,outcome TEXT,reason TEXT,created REAL NOT NULL,updated REAL NOT NULL,
  UNIQUE(agent_id,generation,call_id));
CREATE TABLE field_notes(id INTEGER PRIMARY KEY, body TEXT, data BLOB);
CREATE TABLE ledger(entry TEXT PRIMARY KEY, amount REAL) WITHOUT ROWID;
'''
ROWS = {
    'chats': [(CHAT, 'Legacy ' + SENTINEL, None, NOW), (OTHER, 'Second', None, NOW)],
    'messages': [(uuid.UUID(int=40).hex, CHAT, 'user', 'Hello ' + SENTINEL, NOW),
                 (uuid.UUID(int=41).hex, CHAT, 'assistant', 'Reply', NOW + 1)],
    'settings': [(key, json.dumps(value)) for key, value in SETTINGS.items()],
    'actions': [(ACTION, CHAT, 'command', 'pending', json.dumps({'command': 'echo ' + SENTINEL}), NOW),
                (APPLIED, CHAT, 'command', 'applied', json.dumps({'command': 'echo done', 'result': APPLIED_RESULT}), NOW)],
    'memories': [('memory-' + kind, kind, f'{kind} {SENTINEL}', 'global', CHAT, 'user', NOW, NOW,
                  NOW + 86400 if kind == 'temporal' else None) for kind in KINDS],
    'checkpoints': [(CHAT, json.dumps({'goal': 'legacy'}), NOW)],
    'metrics': [(uuid.UUID(int=41).hex, CHAT, json.dumps({'calls': 1}))],
    # A running root whose pending action never ran, and a waiting root whose action had applied.
    'runs': [(RUN, CHAT, 'Legacy goal', 'running', CONFIG, None, 1, 0.5, None, NOW, NOW),
             (RUN2, CHAT, 'Applied goal', 'waiting_approval', CONFIG, None, 1, 0.5, None, NOW, NOW)],
    'run_events': [(RUN, 1, 'created', '{}', NOW)],
    'run_tools': [(RUN, '1:call', 'RunCommand', json.dumps({'command': 'echo'}), 'running', json.dumps({'action_id': ACTION})),
                  (RUN2, '1:call', 'RunCommand', json.dumps({'command': 'echo done'}), 'running', json.dumps({'action_id': APPLIED}))],
    'run_action_owners': [(ACTION, RUN, '1:call', 'command'), (APPLIED, RUN2, '1:call', 'command')],
    'connector_grants': [('fixture-plugin', 'grant-1', 3, 'connected', json.dumps(GRANT), NOW)],
    'connector_registrations': [('registration-1', 'fixture-plugin', 'https://issuer.invalid', 'https://resource.invalid',
                                 'b' * 64, 'c' * 64, 'admitted', None, '{}', NOW)],
    'jobs': [(JOB, CHAT, 'human', 'Legacy job', 'Check the monitor', json.dumps({'kind': 'interval', 'seconds': 3600}), 0,
              'paused', 2, json.dumps({'config': {}, 'workspace': None}), 'f' * 64, NOW, None, None, None, None, None, NOW, NOW)],
    'job_fires': [(FIRE, JOB, CHAT, 2, NOW - 60, 0, 'host', 'claimed', None, None, None, 'f' * 64, None, None, NOW - 60, NOW - 60)],
    'agent_effects': [(EFFECT, RUN, CHAT, 'agent-1', 1, 'child-call', 'RunTests', json.dumps({'recipe': 'node-test'}), 'a' * 64,
                       None, None, None, 'd' * 64, 'ask', 'proposed', None, None, None, None, None, NOW, NOW)],
    'field_notes': [(1, 'note ' + SENTINEL, b'\x00\x01binary'), (2, 'second note', None)],
    'ledger': [('alpha', 1.5), ('beta', -2.25)],
}
UNKNOWN_NOTES = migrations._label('field_notes', migrations._reference()['tables'])


def legacy_home(tmp_path, name='home', keep_open=False, journal='WAL'):
    home = tmp_path / name
    home.mkdir(mode=0o700)
    db = sqlite3.connect(home / DATABASE, isolation_level=None, check_same_thread=False)
    db.execute(f'PRAGMA journal_mode={journal}')
    if keep_open:
        db.execute('PRAGMA wal_autocheckpoint=0')  # committed frames stay in the WAL
    db.executescript(LEGACY)
    db.execute('BEGIN')
    for table, values in ROWS.items():
        db.executemany(f'INSERT INTO {table} VALUES({",".join("?" * len(values[0]))})', values)
    db.execute('COMMIT')
    if keep_open:
        return home, db
    db.close()
    return home


def boot(home):
    return App(home, home.parent / 'resources', lambda *unused: None)


def read(home, sql, args=()):
    with closing(sqlite3.connect((home / DATABASE).as_uri() + '?mode=ro', uri=True)) as db:
        return db.execute(sql, args).fetchall()


def version(home):
    return read(home, 'PRAGMA user_version')[0][0]


def history(home):
    return read(home, 'SELECT backup_id,from_version,to_version FROM upgrade_history ORDER BY id')


def fingerprint(home):
    wal = home / (DATABASE + '-wal')
    data = (home / DATABASE).read_bytes() + b'|' + (wal.read_bytes() if wal.exists() else b'')
    return hashlib.sha256(data).hexdigest()


def entries(home):
    return sorted(os.listdir(home / BACKUPS)) if (home / BACKUPS).exists() else []


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mutate(home, statement, args=()):
    with closing(sqlite3.connect(home / DATABASE, isolation_level=None)) as db:
        if ';' in statement:
            db.executescript(statement)
        else:
            db.execute(statement, args)


# Drift: the manual reference must equal what this build creates.

def test_reference_schema_is_exactly_what_a_fresh_app_creates(tmp_path):
    home = tmp_path / 'data'
    app = boot(home)
    app.close()
    assert app.upgrade_receipt['state'] == 'fresh' and app.upgrade_receipt['completed']
    with closing(sqlite3.connect(home / DATABASE)) as db:
        schema = migrations._schema(db)
        versions, problems = migrations._versions(db, schema)
    reference = migrations._reference()
    compat = migrations._compatibility(schema, reference)
    assert (compat['refusals'], compat['missing_tables'], compat['missing_columns'], compat['unknown_tables']) == ([], [], {}, [])
    # Any subsystem DDL change fails here and forces an explicit compatibility/epoch decision.
    assert set(schema['tables']) == set(reference['tables']) and 'upgrade_history' in schema['tables']
    for key, table in reference['tables'].items():
        if not table['virtual'] and key not in reference['shadows']:
            have = schema['tables'][key]
            assert (have['columns'], have['uniques'], have['checks'], have['tail']) == (
                table['columns'], table['uniques'], table['checks'], table['tail']), key
    assert schema['indexes'] == reference['indexes'] and schema['triggers'] == reference['triggers']
    assert not problems and versions == {'runtime_meta': [1], 'job_meta': [1], 'mods_meta': [1],
                                         'artifact_meta': [1], 'search_index_meta': [1],
                                         'run_stream_meta': [1], 'process_meta': [1]}
    assert version(home) == migrations.SCHEMA_EPOCH and history(home) == [(None, 0, migrations.SCHEMA_EPOCH)]


def test_fresh_home_has_no_backup_and_restart_is_current(tmp_path):
    home = tmp_path / 'data'
    boot(home).close()
    assert not (home / BACKUPS).exists()
    app = boot(home)
    app.close()
    assert app.upgrade_receipt['state'] == 'current' and app.upgrade_receipt['backup_id'] is None
    assert not (home / BACKUPS).exists() and history(home) == [(None, 0, migrations.SCHEMA_EPOCH)]
    assert migrations.inspect(home)['state'] == 'current'


# The integrated legacy upgrade

def test_legacy_upgrade_backs_up_wal_state_and_preserves_records_without_replay(tmp_path):
    home, writer = legacy_home(tmp_path, keep_open=True)
    queries = {table: f'SELECT * FROM {table} ORDER BY 1' for table in
               ('chats', 'messages', 'actions', 'memories', 'checkpoints', 'metrics', 'connector_grants',
                'field_notes', 'ledger')}
    queries.update(pins='SELECT id,pin,pin_fingerprint,revision,task FROM jobs',
                   configs='SELECT id,goal,config,checkpoint,steps,elapsed FROM runs ORDER BY id',
                   arguments='SELECT run_id,call_id,name,arguments FROM run_tools ORDER BY run_id',
                   settings="SELECT * FROM settings WHERE key NOT IN ('permissions','model_profiles') ORDER BY key")
    try:
        writer.execute('INSERT INTO chats VALUES(?,?,?,?)', (WAL_CHAT, 'Only in the WAL', None, NOW))
        assert (home / (DATABASE + '-wal')).stat().st_size > 0
        before = {name: read(home, sql) for name, sql in queries.items()}
        app = boot(home)
        try:
            receipt = app.upgrade_receipt
            assert app.chat(CHAT)['title'] == 'Legacy ' + SENTINEL
        finally:
            app.close()
    finally:
        writer.close()
    assert {name: read(home, sql) for name, sql in queries.items()} == before
    assert ROWS['run_events'][0] in read(home, 'SELECT * FROM run_events')  # recovery only appends
    # Recovery settled interrupted work fail-closed; nothing was replayed or invented.
    assert dict(read(home, 'SELECT id,state FROM runs')) == {RUN: 'needs_reconciliation', RUN2: 'interrupted'}
    tools = {run: (state, result) for run, state, result in read(home, 'SELECT run_id,state,result FROM run_tools')}
    assert tools[RUN] == ('uncertain', json.dumps({'action_id': ACTION}))
    assert tools[RUN2] == ('completed', encoded({'action_id': APPLIED, 'status': 'applied', 'result': APPLIED_RESULT}))
    assert read(home, 'SELECT status FROM actions WHERE id=?', (ACTION,)) == [('pending',)]
    assert read(home, 'SELECT state FROM connector_registrations') == [('unknown',)]
    assert read(home, 'SELECT status FROM job_fires') == [('needs_review',)]
    assert read(home, 'SELECT status,workspace_identity,binding_version FROM agent_effects') == [('invalidated', None, None)]
    assert 'grant_revision' in {r[1] for r in read(home, 'PRAGMA table_info(mods)')}
    # Required groups added since are filled; an omitted optional group (screen) stays omitted, meaning off.
    from pebble.permissions import OPTIONAL_TOOLS
    assert json.loads(read(home, "SELECT value FROM settings WHERE key='permissions'")[0][0]) == {
        'mode': 'ask', 'tools': {**{k: v for k, v in DEFAULT['tools'].items() if k not in OPTIONAL_TOOLS}, **OLD_TOOLS}}

    [backup] = migrations.inspect(home)['backups']
    assert receipt['state'] == 'backed_up' and receipt['completed'] and receipt['backup_id'] == backup['id']
    assert backup['state'] == 'completed' and receipt['verification']['unknown_tables_retained'] == 2
    copy = home / BACKUPS / (backup['id'] + '.sqlite')
    assert copy.read_bytes()[18:20] == b'\x01\x01'  # a self-contained rollback-journal file
    with closing(sqlite3.connect(copy.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        assert db.execute('SELECT count(*) FROM chats WHERE id=?', (WAL_CHAT,)).fetchone()[0] == 1
        assert db.execute('PRAGMA user_version').fetchone()[0] == 0
    assert version(home) == migrations.SCHEMA_EPOCH and history(home) == [(backup['id'], 0, migrations.SCHEMA_EPOCH)]
    texts = [p.read_text() for p in (home / BACKUPS).glob('*.json')] + [json.dumps(migrations.inspect(home)), json.dumps(receipt)]
    assert len(texts) == 4 and not any(SENTINEL in text or 'field_notes' in text for text in texts)

    def operational():
        return read(home, 'SELECT (SELECT count(*) FROM run_events),(SELECT count(*) FROM job_notifications),'
                          '(SELECT count(*) FROM runs),(SELECT group_concat(status) FROM (SELECT status FROM actions ORDER BY id))')

    settled = operational()
    app = boot(home)
    app.close()
    assert app.upgrade_receipt['state'] == 'current'
    assert operational() == settled and history(home) == [(backup['id'], 0, migrations.SCHEMA_EPOCH)]
    assert [(b['id'], b['sha256'], b['state']) for b in migrations.inspect(home)['backups']] == [
        (backup['id'], backup['sha256'], 'completed')]


def test_profile_migration_replay_matches_providers(tmp_path):
    home = legacy_home(tmp_path)
    app = boot(home)
    try:
        app.model.profiles()  # the host's own normalization, after verified completion
    finally:
        app.close()
    stored = json.loads(read(home, "SELECT value FROM settings WHERE key='model_profiles'")[0][0])
    assert stored == [migrations._profile_migration(p) for p in PROFILES] and stored != PROFILES
    assert stored[1]['context'] == 1_050_000 and stored[0]['connection_id'] == 'fixture-route'
    assert migrations._profiles(PROFILES, stored)


def test_exact_host_normalization_between_prepare_and_complete_is_accepted(tmp_path, monkeypatch):
    home = legacy_home(tmp_path)
    normalized = json.dumps([migrations._profile_migration(p) for p in PROFILES])
    real = migrations.Upgrade.complete

    def normalizing(self):
        mutate(home, "UPDATE settings SET value=? WHERE key='model_profiles'", (normalized,))
        return real(self)

    monkeypatch.setattr(migrations.Upgrade, 'complete', normalizing)
    app = boot(home)
    app.close()
    assert app.upgrade_receipt['completed'] and version(home) == migrations.SCHEMA_EPOCH


# Forged startup changes are refused; the epoch stays and the backup stays valid.

def profile_change(change):
    def apply(db):
        profiles = json.loads(db.execute("SELECT value FROM settings WHERE key='model_profiles'").fetchone()[0])
        db.execute("UPDATE settings SET value=? WHERE key='model_profiles'", (json.dumps(change(profiles)),))
    return apply


def profile_field(field, value):
    return profile_change(lambda profiles: [dict(profiles[0], **{field: value})] + profiles[1:])


def setting(key, value):
    return lambda db: db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))


def sql(statement, *args):
    return lambda db: db.execute(statement, args)


OUTCOME = encoded(AgentEffects._bounded(APPLIED_RESULT, 16000), 64000)
VERIFIED = json.dumps({'action_id': ACTION, 'status': 'applied', 'result': {'exit_code': 0, 'verified': True}})
FORGERIES = [
    ('same-id route', profile_field('url', 'https://attacker.invalid/v1'), 'settings'),
    ('same-id model', profile_field('model', 'other-model'), 'settings'),
    ('same-id kind', profile_field('kind', 'chatgpt'), 'settings'),
    ('same-id client', profile_field('client_id', 'other-client'), 'settings'),
    ('same-id account', profile_field('subject', 'other-account'), 'settings'),
    ('same-id connection', profile_field('connection_id', 'other-connection'), 'settings'),
    ('same-id capability', profile_field('tools', True), 'settings'),
    ('same-id key status', profile_field('has_api_key', True), 'settings'),
    ('duplicate profile', profile_change(lambda profiles: profiles + profiles[:1]), 'settings'),
    ('deleted profile', profile_change(lambda profiles: profiles[1:]), 'settings'),
    ('added profile', profile_change(lambda profiles: profiles + [dict(profiles[0], id='added-route')]), 'settings'),
    ('permission enabled', setting('permissions', {'mode': 'ask', 'tools': {**DEFAULT['tools'], **OLD_TOOLS, 'edits': True}}), 'settings'),
    ('permission mode', setting('permissions', {'mode': 'full', 'tools': {**DEFAULT['tools'], **OLD_TOOLS}}), 'settings'),
    ('non-default new tool', setting('permissions', {'mode': 'ask', 'tools': {**DEFAULT['tools'], **OLD_TOOLS, 'views': False}}), 'settings'),
    ('added setting', setting('memory_layers', dict.fromkeys(KINDS, True)), 'settings'),
    ('proposed effect applied', sql("UPDATE agent_effects SET status='applied',action_id=?,outcome=? WHERE id=?",
                                    APPLIED, OUTCOME, EFFECT), 'agent_effects'),
    ('uncertain receipt verified', sql("UPDATE run_tools SET state='completed',result=? WHERE run_id=?", VERIFIED, RUN), 'run_tools'),
    ('pending action applied', sql("UPDATE actions SET status='applied',payload=? WHERE id=?",
                                   json.dumps({'command': 'echo', 'result': {'verified': True}}), ACTION), 'actions'),
    ('pending action rejected unowned', sql("UPDATE actions SET status='rejected' WHERE id=?", ACTION), 'actions'),
    ('run completed', sql("UPDATE runs SET state='completed' WHERE id=?", RUN), 'runs'),
    ('run checkpoint replaced', sql('UPDATE runs SET checkpoint=? WHERE id=?', '{"messages":[]}', RUN), 'runs'),
    ('fire bound', sql("UPDATE job_fires SET status='bound',run_id=? WHERE id=?", RUN, FIRE), 'job_fires'),
    ('job re-pinned', sql('UPDATE jobs SET pin_fingerprint=? WHERE id=?', '0' * 64, JOB), 'jobs'),
    ('paused job activated', sql("UPDATE jobs SET state='active' WHERE id=?", JOB), 'jobs'),
    ('grant re-pinned', sql('UPDATE connector_grants SET revision=revision+1'), 'connector_grants'),
    ('registration promoted', sql("UPDATE connector_registrations SET state='registered'"), 'connector_registrations'),
    ('forged action', sql('INSERT INTO actions VALUES(?,?,?,?,?,?)', uuid.UUID(int=99).hex, CHAT, 'command', 'applied', '{}', NOW), 'actions'),
    ('forged enabled extension', sql("INSERT INTO extensions VALUES('forged','plugin','Forged','','{}',1,0)"), 'extensions'),
    ('lost message', sql("DELETE FROM messages WHERE role='user'"), 'messages'),
    ('lost memory layer', sql("DELETE FROM memories WHERE kind='profile'"), 'memories'),
    ('lost run event', sql('DELETE FROM run_events'), 'run_events'),
    ('changed unknown data', sql('DELETE FROM field_notes WHERE id=1'), UNKNOWN_NOTES),
]


@pytest.mark.parametrize('name, change, table', FORGERIES, ids=[f[0] for f in FORGERIES])
def test_forged_startup_change_is_refused_and_backup_retained(tmp_path, monkeypatch, name, change, table):
    home = legacy_home(tmp_path)
    real = migrations.Upgrade.complete

    def forged(self):
        with closing(sqlite3.connect(home / DATABASE, isolation_level=None)) as db:
            change(db)
        return real(self)

    monkeypatch.setattr(migrations.Upgrade, 'complete', forged)
    with pytest.raises(MigrationRefused) as caught:
        boot(home)
    monkeypatch.undo()
    assert caught.value.reason == 'preservation_failed' and table in caught.value.detail.split(', ')
    assert 'field_notes' not in str(caught.value) and SENTINEL not in str(caught.value)
    assert version(home) == 0 and read(home, "SELECT count(*) FROM sqlite_master WHERE name='upgrade_history'") == [(0,)]
    report = migrations.inspect(home)
    assert report['state'] == 'upgrade_pending' and [b['state'] for b in report['backups']] == ['pending']
    assert migrations.verify_backup(home, report['backups'][0]['id'])['valid']


def tool_row(state, result):
    return (RUN, '1:call', 'RunCommand', '{}', state, result if isinstance(result, str) else json.dumps(result))


READBACK = encoded({'action_id': APPLIED, 'status': 'applied', 'result': APPLIED_RESULT})
REVOKED = {'action_id': ACTION, 'kind': 'tests', 'status': 'rejected', 'revoked': True, 'request_id': EFFECT, 'reason': 'closed'}
TOOL_CASES = [
    ('unacknowledged effect stays unknown', 'running', {'action_id': ACTION}, 'uncertain', {'action_id': ACTION}, 'pending', 0),
    ('applied action read back exactly', 'running', {'action_id': APPLIED}, 'completed', READBACK, 'pending', 0),
    ('pending action invented as applied', 'running', {'action_id': ACTION}, 'completed', VERIFIED, 'pending', 1),
    ('applied action with invented evidence', 'running', {'action_id': APPLIED}, 'completed',
     {'action_id': APPLIED, 'status': 'applied', 'result': {'exit_code': 0, 'verified': True}}, 'pending', 1),
    ('revocation of a pending action', 'completed', {'action_id': ACTION, 'status': 'pending'}, 'completed', REVOKED, 'rejected', 0),
    ('revocation turned into success', 'completed', {'action_id': ACTION, 'status': 'pending'}, 'completed',
     {'action_id': ACTION, 'status': 'applied'}, 'applied', 1),
    ('uncertain receipt completed', 'uncertain', {'action_id': ACTION}, 'completed', {'action_id': ACTION, 'status': 'applied'}, 'pending', 1),
]


def effect_row(status, action, outcome):
    if outcome == 'exact':
        outcome = OUTCOME
    elif isinstance(outcome, dict):
        outcome = encoded(AgentEffects._bounded(outcome, 16000), 64000)
    return (EFFECT, RUN, CHAT, 'agent-1', 1, 'child-call', 'RunTests', '{}', 'a' * 64, None, None, None, 'd' * 64,
            'ask', status, 'root-call', 'effect-call', action, outcome, None, NOW, NOW, None, 2)


EFFECT_CASES = [
    ('awaiting action read back as applied', 'awaiting_approval', APPLIED, 'applied', APPLIED, 'exact', 0),
    ('applied with invented outcome', 'awaiting_approval', APPLIED, 'applied', APPLIED, {'exit_code': 0, 'verified': True}, 1),
    ('pending action claimed applied', 'awaiting_approval', ACTION, 'applied', ACTION, 'exact', 1),
    ('proposed effect applied', 'proposed', None, 'applied', APPLIED, 'exact', 1),
    ('proposed effect invalidated', 'proposed', None, 'invalidated', None, None, 0),
    ('uncertain effect reported without receipt', 'uncertain', ACTION, 'user_reported', ACTION, {'note': 'done'}, 1),
    ('awaiting effect revoked', 'awaiting_approval', ACTION, 'revoked', ACTION, None, 0),
    ('invalidated effect resurrected', 'invalidated', None, 'proposed', None, None, 1),
]


def receipt_pair(tmp_path, old_rows, new_rows):
    """Two reference databases, `new` with the backup `old` attached as b, as at completion."""
    paths = tmp_path / 'old.sqlite', tmp_path / 'new.sqlite'
    for path, rows in zip(paths, (old_rows, new_rows)):
        with closing(sqlite3.connect(path)) as db:
            db.executescript(migrations.REFERENCE_DDL)
            for table, values in rows.items():
                db.executemany(f'INSERT INTO {table} VALUES({",".join("?" * len(values[0]))})', values)
            db.commit()
    db = sqlite3.connect(paths[1])
    db.execute('ATTACH DATABASE ? AS b', (str(paths[0]),))
    return db


def actions(pending_status):
    return [(APPLIED, CHAT, 'tests', 'applied', json.dumps({'result': APPLIED_RESULT}), NOW),
            (ACTION, CHAT, 'tests', pending_status, '{}', NOW)]


@pytest.mark.parametrize('name, old_state, old_result, new_state, new_result, action_status, bad', TOOL_CASES,
                         ids=[c[0] for c in TOOL_CASES])
def test_recovered_receipts_need_an_exact_pre_existing_basis(tmp_path, name, old_state, old_result, new_state,
                                                             new_result, action_status, bad):
    db = receipt_pair(tmp_path, {'actions': actions('pending'), 'run_tools': [tool_row(old_state, old_result)]},
                      {'actions': actions(action_status), 'run_tools': [tool_row(new_state, new_result)]})
    with closing(db):
        assert migrations._tool_changes(db) == bad


@pytest.mark.parametrize('name, old_status, old_action, new_status, new_action, outcome, bad', EFFECT_CASES,
                         ids=[c[0] for c in EFFECT_CASES])
def test_recovered_effects_need_an_exact_pre_existing_outcome(tmp_path, name, old_status, old_action, new_status,
                                                             new_action, outcome, bad):
    db = receipt_pair(tmp_path, {'actions': actions('pending'), 'agent_effects': [effect_row(old_status, old_action, None)]},
                      {'actions': actions('pending'), 'agent_effects': [effect_row(new_status, new_action, outcome)]})
    with closing(db):
        assert migrations._effect_changes(db) == bad


# Refusal before any mutation

REFUSALS = [
    ('future epoch', f'PRAGMA user_version={migrations.SCHEMA_EPOCH + 1}', 'future_version'),
    ('negative epoch', 'PRAGMA user_version=-1', 'unknown_version'),
    ('extra core column', 'ALTER TABLE chats ADD COLUMN pinned INTEGER', 'incompatible_core'),
    ('core without primary key', 'DROP TABLE messages; CREATE TABLE messages(id TEXT,chat_id TEXT,role TEXT,content TEXT,created REAL);',
     'incompatible_core'),
    ('core check constraint', 'DROP TABLE settings; CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT CHECK(length(value)<4000));',
     'incompatible_core'),
    ('unknown subsystem column', 'ALTER TABLE runs ADD COLUMN priority INTEGER', 'incompatible_subsystem'),
    ('weakened job origin', 'DROP TABLE jobs; ' + JOBS.replace(" CHECK(origin='human')", ''), 'incompatible_subsystem'),
    ('unknown trigger', 'CREATE TRIGGER chats_audit AFTER INSERT ON chats BEGIN SELECT 1; END;', 'unknown_trigger'),
    ('changed memory trigger', 'DROP TRIGGER memory_delete; CREATE TRIGGER memory_delete AFTER DELETE ON memories BEGIN SELECT 1; END;',
     'unknown_trigger'),
    ('unknown unique constraint', 'CREATE UNIQUE INDEX chats_title ON chats(title)', 'unknown_constraint'),
    ('future runtime version', 'UPDATE runtime_meta SET version=2', 'subsystem_version'),
    ('runs without runtime version', 'DROP TABLE runtime_meta', 'missing_runtime_version'),
    ('unknown version marker', 'CREATE TABLE plugin_meta(version INTEGER); INSERT INTO plugin_meta VALUES(7);', 'unknown_runtime_version'),
    ('view in a known table name', 'CREATE VIEW git_worktrees AS SELECT 1 AS id', 'incompatible_object'),
]


@pytest.mark.parametrize('name, statement, reason', REFUSALS, ids=[r[0] for r in REFUSALS])
def test_incompatible_database_is_refused_without_changes(tmp_path, name, statement, reason):
    home = legacy_home(tmp_path)
    mutate(home, statement)
    before = fingerprint(home)
    with pytest.raises(MigrationRefused) as caught:
        migrations.prepare(home)
    assert caught.value.reason == reason
    assert fingerprint(home) == before and entries(home) == []


def corrupt(home, name):
    if name is None:
        (home / DATABASE).write_bytes(os.urandom(8192))
        return
    page = read(home, 'PRAGMA page_size')[0][0]
    root = read(home, 'SELECT rootpage FROM sqlite_master WHERE name=?', (name,))[0][0]
    with open(home / DATABASE, 'r+b') as handle:
        handle.seek((root - 1) * page)
        handle.write(b'\xff' * page)


@pytest.mark.parametrize('target', ['messages', 'memories_scope', None], ids=['table page', 'index page', 'not a database'])
def test_corrupt_database_is_refused_without_changes(tmp_path, target):
    home = legacy_home(tmp_path)
    corrupt(home, target)
    before = fingerprint(home)
    with pytest.raises(MigrationRefused) as caught:
        migrations.prepare(home)
    assert caught.value.reason == 'corrupt'
    assert fingerprint(home) == before and entries(home) == []


def test_app_refuses_before_store_or_subsystems_mutate(tmp_path):
    home = legacy_home(tmp_path)
    mutate(home, f'PRAGMA user_version={migrations.SCHEMA_EPOCH + 1}')
    before = fingerprint(home)
    with pytest.raises(MigrationRefused) as caught:
        boot(home)
    assert caught.value.reason == 'future_version'
    assert fingerprint(home) == before and entries(home) == [] and version(home) == migrations.SCHEMA_EPOCH + 1


def link_case(tmp_path, case):
    """(directory handed to prepare, homes that must stay untouched)."""
    if case == 'symlinked home':
        real = legacy_home(tmp_path)
        (tmp_path / 'link').symlink_to(real)
        return tmp_path / 'link', [real]
    if case in ('symlinked database', 'journal without database'):
        home = tmp_path / 'home'
        home.mkdir(mode=0o700)
        if case == 'journal without database':
            (home / (DATABASE + '-wal')).write_bytes(b'stale')
            return home, []
        elsewhere = legacy_home(tmp_path, 'elsewhere')
        (home / DATABASE).symlink_to(elsewhere / DATABASE)
        return home, [elsewhere]
    home = legacy_home(tmp_path)
    if case == 'hard-linked database':
        os.link(home / DATABASE, tmp_path / 'second-name.sqlite')
    elif case == 'symlinked wal':
        (home / (DATABASE + '-wal')).symlink_to(tmp_path / 'absent')
    elif case == 'symlinked backup directory':
        (tmp_path / 'outside').mkdir()
        (home / BACKUPS).symlink_to(tmp_path / 'outside')
    elif case == 'shared home':
        os.chmod(home, 0o777)
    return home, [home]


@pytest.mark.parametrize('case', ['symlinked home', 'symlinked database', 'journal without database', 'hard-linked database',
                                  'symlinked wal', 'symlinked backup directory', 'shared home'])
def test_links_shared_or_foreign_paths_are_refused(tmp_path, case):
    target, protected = link_case(tmp_path, case)
    before = [fingerprint(home) for home in protected]
    with pytest.raises(MigrationRefused) as caught:
        migrations.prepare(target)
    assert caught.value.reason == 'unsafe_path'
    assert [fingerprint(home) for home in protected] == before
    assert not any(home.joinpath(BACKUPS).is_dir() and not home.joinpath(BACKUPS).is_symlink() for home in protected)
    assert not (tmp_path / 'outside').exists() or os.listdir(tmp_path / 'outside') == []


# Backup tampering, publication failure and ownership

def prepared(home):
    upgrade = migrations.prepare(home)
    assert upgrade.state == 'backed_up'
    folder = home / BACKUPS
    return upgrade, folder / (upgrade.backup + '.sqlite'), folder / (upgrade.backup + '.manifest.json')


def rewrite(path, change):
    os.chmod(path, 0o600)
    path.write_bytes(change(path.read_bytes()))
    os.chmod(path, 0o400)


def flip(data):
    middle = len(data) // 2
    return data[:middle] + bytes([data[middle] ^ 1]) + data[middle + 1:]


def recount(data):
    manifest = json.loads(data)
    manifest['logical']['tables']['chats']['rows'] += 1
    return json.dumps(manifest).encode()


def replace_with_link(path, tmp_path):
    shutil.copy2(path, tmp_path / 'copy')
    path.unlink()
    path.symlink_to(tmp_path / 'copy')


TAMPERING = {
    'backup bytes': (lambda data, manifest, tmp: rewrite(data, flip), 'backup_tampered', 'pending_backup_invalid'),
    'manifest counts': (lambda data, manifest, tmp: rewrite(manifest, recount), 'backup_tampered', 'pending_backup_invalid'),
    'backup symlink': (lambda data, manifest, tmp: replace_with_link(data, tmp), 'backup_tampered', 'backup_tampered'),
    'manifest hard link': (lambda data, manifest, tmp: os.link(manifest, tmp / 'second'), 'backup_tampered', 'backup_tampered'),
}


@pytest.mark.parametrize('case', sorted(TAMPERING))
def test_tampered_pending_backup_blocks_completion_and_restart(tmp_path, case):
    home = legacy_home(tmp_path)
    tamper, at_completion, at_restart = TAMPERING[case]
    upgrade, data, manifest = prepared(home)
    tamper(data, manifest, tmp_path)
    with pytest.raises(MigrationRefused) as caught:
        upgrade.complete()
    assert caught.value.reason == at_completion and version(home) == 0
    for start in (migrations.prepare, boot):
        with pytest.raises(MigrationRefused) as caught:
            start(home)
        assert caught.value.reason == at_restart and version(home) == 0


def test_publication_failure_leaves_no_backup_and_retry_succeeds(tmp_path, monkeypatch):
    home = legacy_home(tmp_path)
    before = fingerprint(home)
    real = os.link

    def failing(source, target, **kwargs):
        if str(target).endswith('.manifest.json'):
            raise OSError(errno.EIO, 'injected publication failure')
        return real(source, target, **kwargs)

    monkeypatch.setattr(os, 'link', failing)
    with pytest.raises(MigrationRefused) as caught:
        migrations.prepare(home)
    monkeypatch.undo()
    assert caught.value.reason == 'backup_failed' and entries(home) == [] and fingerprint(home) == before
    upgrade = migrations.prepare(home)
    try:
        assert upgrade.state == 'backed_up' and len(entries(home)) == 2
    finally:
        upgrade.abort()


def test_publication_never_replaces_an_existing_path(tmp_path):
    folder = tmp_path / 'private'
    folder.mkdir(mode=0o700)
    directory = migrations._Directory.open(folder, 0o077)
    try:
        migrations._publish(directory, 'record.json', {'value': 1})
        with pytest.raises(FileExistsError):
            migrations._publish(directory, 'record.json', {'value': 2})
    finally:
        directory.close()
    assert json.loads((folder / 'record.json').read_text()) == {'value': 1} and os.listdir(folder) == ['record.json']


def test_one_process_owns_an_upgrade_and_pending_backups_cannot_be_discarded(tmp_path):
    home = legacy_home(tmp_path)
    first = migrations.prepare(home)
    try:
        for attempt in (lambda: migrations.prepare(home, Limits(lock_wait=0.1)),
                        lambda: migrations.discard_backup(home, first.backup, '0' * 64, Limits(lock_wait=0.1))):
            with pytest.raises(MigrationRefused) as caught:
                attempt()
            assert caught.value.reason == 'upgrade_owned'
    finally:
        first.abort()
    [pending] = migrations.inspect(home)['backups']
    with pytest.raises(MigrationRefused) as caught:
        migrations.discard_backup(home, first.backup, pending['manifest_sha256'])
    assert caught.value.reason == 'backup_pending' and len(entries(home)) == 2
    second = migrations.prepare(home)  # an interrupted preflight reuses its backup
    try:
        assert second.state == 'resumed' and second.backup == first.backup and len(entries(home)) == 2
    finally:
        second.abort()
    with pytest.raises(MigrationRefused) as caught:
        second.complete()
    assert caught.value.reason == 'not_prepared'


def test_busy_database_is_refused_within_its_bound(tmp_path):
    home = legacy_home(tmp_path, journal='DELETE')
    blocker = sqlite3.connect(home / DATABASE, isolation_level=None)
    blocker.execute('BEGIN EXCLUSIVE')
    try:
        before, started = fingerprint(home), time.monotonic()
        with pytest.raises(MigrationRefused) as caught:
            migrations.prepare(home, Limits(busy=0.2, deadline=5))
        assert caught.value.reason == 'busy' and time.monotonic() - started < 3
        assert fingerprint(home) == before and entries(home) == []
    finally:
        blocker.execute('ROLLBACK')
        blocker.close()
    upgrade = migrations.prepare(home)
    upgrade.abort()
    assert upgrade.state == 'backed_up'


class Ticks:
    def __init__(self, step):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def test_deadline_bounds_preflight_and_leaves_no_partial_backup(tmp_path):
    home = legacy_home(tmp_path)
    before = fingerprint(home)
    with pytest.raises(MigrationRefused) as caught:
        migrations.prepare(home, Limits(deadline=1.0), clock=Ticks(1.0))
    assert caught.value.reason == 'deadline' and entries(home) == [] and fingerprint(home) == before


def test_zero_deadline_refuses_before_creating_a_fresh_home(tmp_path):
    home = tmp_path / 'new-home'
    with pytest.raises(MigrationRefused) as caught:
        migrations.prepare(home, Limits(deadline=0))
    assert caught.value.reason == 'deadline' and not home.exists()


@pytest.mark.parametrize('limits', [Limits(max_database_bytes=4096), Limits(max_backup_bytes=4096), Limits(max_backups=0)],
                         ids=['database size', 'retained bytes', 'retained count'])
def test_size_and_retention_budgets_refuse_before_writing(tmp_path, limits):
    home = legacy_home(tmp_path)
    before = fingerprint(home)
    with pytest.raises(MigrationRefused) as caught:
        migrations.prepare(home, limits)
    assert caught.value.reason == ('database_too_large' if limits.max_database_bytes == 4096 else 'backup_budget_exhausted')
    assert entries(home) == [] and fingerprint(home) == before


def test_insufficient_free_space_refuses(tmp_path, monkeypatch):
    home = legacy_home(tmp_path)
    monkeypatch.setattr(os, 'fstatvfs', lambda fd: SimpleNamespace(f_bavail=1, f_frsize=1))
    with pytest.raises(MigrationRefused) as caught:
        migrations.prepare(home)
    assert caught.value.reason == 'insufficient_space' and entries(home) == []


def test_retained_backups_are_never_deleted_to_make_room(tmp_path, monkeypatch):
    home = legacy_home(tmp_path)
    boot(home).close()
    [first] = migrations.inspect(home)['backups']
    data = home / BACKUPS / (first['id'] + '.sqlite')
    digest = sha(data)
    old_epoch = migrations.SCHEMA_EPOCH
    monkeypatch.setattr(migrations, 'SCHEMA_EPOCH', old_epoch + 1)  # a later build needs another upgrade
    with pytest.raises(MigrationRefused) as caught:
        migrations.prepare(home, Limits(max_backups=1))
    assert caught.value.reason == 'backup_budget_exhausted' and sha(data) == digest and version(home) == old_epoch
    with pytest.raises(MigrationRefused) as caught:
        migrations.discard_backup(home, first['id'], '0' * 64)
    assert caught.value.reason == 'backup_tampered' and sha(data) == digest
    assert migrations.discard_backup(home, first['id'], first['manifest_sha256']) == {'discarded': first['id'], 'bytes': first['bytes']}
    assert entries(home) == []
    upgrade = migrations.prepare(home, Limits(max_backups=1))
    upgrade.abort()
    assert upgrade.state == 'backed_up' and upgrade.version == old_epoch


def test_interrupted_publication_leftovers_are_swept_under_ownership(tmp_path):
    home = legacy_home(tmp_path)
    boot(home).close()
    [backup] = migrations.inspect(home)['backups']
    data = home / BACKUPS / (backup['id'] + '.sqlite')
    os.link(data, home / BACKUPS / '.tmp-0123456789abcdef.sqlite')  # stopped between link(2) and unlink
    partial = home / BACKUPS / '.tmp-fedcba9876543210.json'
    partial.write_bytes(b'{}')
    partial.chmod(0o600)
    upgrade = migrations.prepare(home)
    upgrade.abort()
    assert upgrade.state == 'current' and upgrade.cleaned == 2 and data.stat().st_nlink == 1
    assert entries(home) == sorted(backup['id'] + suffix for suffix in ('.sqlite', '.manifest.json', '.complete.json'))
    assert migrations.verify_backup(home, backup['id'])['valid']


# Interrupted completion and repeated startup

def test_stop_before_the_epoch_stamp_resumes_with_the_same_backup(tmp_path, monkeypatch):
    home = legacy_home(tmp_path)

    def stopped(*unused):
        raise OSError(errno.EIO, 'injected stop before the epoch stamp')

    monkeypatch.setattr(migrations, '_stamp', stopped)
    with pytest.raises(MigrationRefused) as caught:
        boot(home)
    monkeypatch.undo()
    assert caught.value.reason == 'io_error'
    report = migrations.inspect(home)
    [pending] = report['backups']
    assert report['state'] == 'upgrade_pending' and report['database']['user_version'] == 0 and pending['state'] == 'pending'
    app = boot(home)
    app.close()
    assert app.upgrade_receipt['state'] == 'resumed' and app.upgrade_receipt['backup_id'] == pending['id']
    [done] = migrations.inspect(home)['backups']
    assert (done['id'], done['sha256'], done['state']) == (pending['id'], pending['sha256'], 'completed')
    assert version(home) == migrations.SCHEMA_EPOCH and history(home) == [(pending['id'], 0, migrations.SCHEMA_EPOCH)]


def test_lost_receipt_is_recovered_from_history_without_another_backup(tmp_path, monkeypatch):
    home = legacy_home(tmp_path)
    real = migrations._publish

    def failing(directory, name, value):
        if name.endswith('.complete.json'):
            raise OSError(errno.EIO, 'injected receipt failure')
        return real(directory, name, value)

    monkeypatch.setattr(migrations, '_publish', failing)
    with pytest.raises(MigrationRefused) as caught:
        boot(home)
    monkeypatch.undo()
    [pending] = migrations.inspect(home)['backups']
    assert caught.value.reason == 'receipt_failed' and pending['state'] == 'pending'
    assert version(home) == migrations.SCHEMA_EPOCH and history(home) == [(pending['id'], 0, migrations.SCHEMA_EPOCH)]
    upgrade = migrations.prepare(home)
    upgrade.abort()
    assert upgrade.state == 'current' and upgrade.recovered == 1
    app = boot(home)
    app.close()
    assert app.upgrade_receipt['state'] == 'current'
    assert [(b['id'], b['state']) for b in migrations.inspect(home)['backups']] == [(pending['id'], 'completed')]


# Snapshot consistency under concurrent WAL writes

def test_backup_is_one_snapshot_while_another_connection_writes(tmp_path):
    home, writer = legacy_home(tmp_path, keep_open=True)
    writer.execute('BEGIN')
    for n in range(200):
        writer.execute('INSERT INTO chats VALUES(?,?,?,?)', (f'base-{n}', 'base', None, NOW))
    writer.execute('COMMIT')
    raw = tmp_path / 'raw.sqlite'
    raw.write_bytes((home / DATABASE).read_bytes())  # what a file copy without the WAL would hold
    with closing(sqlite3.connect(raw)) as copy:
        try:
            found = copy.execute("SELECT count(*) FROM chats WHERE id LIKE 'base-%'").fetchone()[0]
        except sqlite3.DatabaseError:
            found = 0
    assert found == 0
    stop, written, failures = threading.Event(), [0], []

    def churn():
        try:
            while not stop.is_set():
                n = written[0]
                writer.execute('BEGIN IMMEDIATE')
                writer.execute('INSERT INTO chats VALUES(?,?,?,?)', (f'churn-{n}', 'churn', None, NOW))
                writer.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (f'churn-message-{n}', f'churn-{n}', 'user', 'm', NOW))
                writer.execute('COMMIT')
                written[0] += 1
        except Exception as exc:  # surfaced below
            failures.append(exc)

    thread = threading.Thread(target=churn)
    thread.start()
    upgrade = None
    try:
        deadline = time.monotonic() + 10
        while written[0] < 5 and time.monotonic() < deadline and not failures:
            time.sleep(0.01)
        upgrade = migrations.prepare(home, Limits(step_pages=1))
    finally:
        stop.set()
        thread.join(10)
    try:
        assert not failures and written[0] >= 5
        path = home / BACKUPS / (upgrade.backup + '.sqlite')
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as copy:
            base = copy.execute("SELECT count(*) FROM chats WHERE id LIKE 'base-%'").fetchone()[0]
            churned = copy.execute("SELECT count(*) FROM chats WHERE id LIKE 'churn-%'").fetchone()[0]
            torn = copy.execute("SELECT (SELECT count(*) FROM chats c WHERE c.id LIKE 'churn-%' AND NOT EXISTS("
                                "SELECT 1 FROM messages m WHERE m.chat_id=c.id)) + (SELECT count(*) FROM messages m "
                                "WHERE m.chat_id LIKE 'churn-%' AND NOT EXISTS(SELECT 1 FROM chats c WHERE c.id=m.chat_id))"
                                ).fetchone()[0]
        assert base == 200 and 5 <= churned <= written[0] and torn == 0
        assert migrations.verify_backup(home, upgrade.backup)['valid']
    finally:
        if upgrade is not None:
            upgrade.abort()
        writer.close()


# Offline restore and inspection

def test_restore_into_a_new_directory_without_touching_the_live_home(tmp_path):
    home = legacy_home(tmp_path)
    boot(home).close()
    [backup] = migrations.inspect(home)['backups']
    live, listing = fingerprint(home), sorted(os.listdir(home))
    target = tmp_path / 'restored'
    result = migrations.restore_copy(home, backup['id'], target)
    assert result['restored'] and result['logical_match'] and result['integrity'] == 'ok' and result['compatible']
    assert result['user_version'] == 0 and result['memory_layers'] == dict.fromkeys(KINDS, 1)
    assert result['credentials_restored'] is False and 'OS vault' in result['credentials']
    assert stat.S_IMODE(target.stat().st_mode) == 0o700 and os.listdir(target) == [DATABASE]
    assert fingerprint(home) == live and sorted(os.listdir(home)) == listing
    for bad in (target, home / 'nested-restore', home, tmp_path / 'missing-parent' / 'copy'):
        with pytest.raises(MigrationRefused) as caught:
            migrations.restore_copy(home, backup['id'], bad)
        assert caught.value.reason == 'restore_target'
    app = boot(target)  # a disposable home upgrades like any legacy database
    try:
        assert app.upgrade_receipt['state'] == 'backed_up' and app.chat(CHAT)['title'] == 'Legacy ' + SENTINEL
    finally:
        app.close()
    assert fingerprint(home) == live


def test_inspection_reports_counts_hashes_and_versions_only(tmp_path):
    home = legacy_home(tmp_path)
    report = migrations.inspect(home, deep=True)
    database = report['database']
    assert report['state'] == 'upgrade_required' and report['backups'] == [] and database['user_version'] == 0
    assert database['integrity'] == 'ok' and database['memory_layers'] == dict.fromkeys(KINDS, 1)
    assert database['tables']['unknown'] == 2 and database['rows']['chats'] == 2 and database['rows'][UNKNOWN_NOTES] == 2
    assert not any(word in json.dumps(report) for word in (SENTINEL, 'field_notes', 'ledger'))
    boot(home).close()
    [backup] = migrations.inspect(home, deep=True)['backups']
    assert backup['verified'] and backup['state'] == 'completed'
    verified = migrations.verify_backup(home, backup['id'])
    assert verified['valid'] and verified['memory_layers'] == dict.fromkeys(KINDS, 1)
    assert stat.S_IMODE((home / BACKUPS).stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o400 for p in (home / BACKUPS).iterdir())
    assert migrations.verify_backup(home, '../escape')['reason'] == 'backup_missing'


@pytest.mark.parametrize('field, value', [('deadline', -1), ('busy', float('nan')), ('max_backups', 1.5),
                                          ('step_pages', 0), ('max_backup_bytes', True)])
def test_limits_are_finite_and_typed(field, value):
    with pytest.raises(ValueError):
        Limits(**{field: value})
