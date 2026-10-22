"""Owned source/frozen verification of Phase 10 pre-upgrade SQLite safety.

Seeds a synthetic Phase 9-era home in a temporary directory and starts the actual
service (backend/main.py, or a frozen --backend) on it. The source module's
read-only inspector checks the backup and upgrade state; the service restarts
without a second backup or effect; the backup is restored offline into a new
disposable directory that the service then opens; and future-epoch or
incompatible databases are refused before mutation. No installed service, live
home, keychain, credential, model or external destination is used: the only
provider is an owned loopback recorder that must stay unused.
"""
import argparse
import contextlib
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT, Service

sys.path.insert(0, str(ROOT / 'backend'))
from pebble import migrations  # noqa: E402  read-only inspector and offline restore helper

DATABASE, BACKUPS = migrations.DATABASE, migrations.BACKUPS
KINDS = ('working', 'episodic', 'semantic', 'procedural', 'temporal', 'profile')
SENTINEL = 'phase10-migration-sentinel-91d2'
NOW = time.time()
CHAT, WAL_CHAT, RUN, RUN2, JOB, FIRE, ACTION, APPLIED = (uuid.UUID(int=n).hex for n in range(101, 109))
OLD_TOOLS = {'files': True, 'edits': False, 'tests': True, 'apps': False, 'web': False, 'terminal': False}
RESULT = {'exit_code': 0, 'output': 'done'}
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
CREATE TABLE jobs(
  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin='human'),
  label TEXT NOT NULL, task TEXT NOT NULL, schedule TEXT NOT NULL, monitor INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','paused','needs_review','finished')),
  revision INTEGER NOT NULL, pin TEXT NOT NULL, pin_fingerprint TEXT NOT NULL,
  anchor REAL NOT NULL, next_due REAL, last_scheduled REAL, last_fingerprint TEXT,
  review_reason TEXT, deferred_reason TEXT, created REAL NOT NULL, updated REAL NOT NULL);
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
CREATE TABLE field_notes(id INTEGER PRIMARY KEY, body TEXT, data BLOB);
'''
PRESERVED = {
    'chats': 'SELECT * FROM chats ORDER BY id', 'messages': 'SELECT * FROM messages ORDER BY id',
    'actions': 'SELECT * FROM actions ORDER BY id', 'memories': 'SELECT * FROM memories ORDER BY id',
    'checkpoints': 'SELECT * FROM checkpoints', 'metrics': 'SELECT * FROM metrics',
    'grants': 'SELECT * FROM connector_grants', 'unknown data': 'SELECT * FROM field_notes ORDER BY id',
    'job pins': 'SELECT id,pin,pin_fingerprint,revision,task FROM jobs',
    'run configurations': 'SELECT id,goal,config,checkpoint,steps,elapsed FROM runs ORDER BY id',
    'tool arguments': 'SELECT run_id,call_id,name,arguments FROM run_tools ORDER BY run_id',
    'other settings': "SELECT * FROM settings WHERE key NOT IN ('permissions','model_profiles') ORDER BY key",
}


def encoded(value):
    # runtime.encoded: the exact receipt text RunJournal.recover writes.
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def rows(port):
    profile = {'id': 'fixture-route', 'name': 'Loopback recorder', 'kind': 'openai',
               'url': f'http://127.0.0.1:{port}/v1', 'model': 'fixture-model', 'context': 8192}
    settings = {'permissions': {'mode': 'ask', 'tools': OLD_TOOLS}, 'memory_enabled': True,
                'memory_initialized': True, 'theme': 'dark', 'model_profiles': [profile]}
    config = json.dumps({'limits': {'max_steps': 4, 'max_seconds': 30}, 'permissions': settings['permissions']})
    return {
        'chats': [(CHAT, 'Legacy ' + SENTINEL, None, NOW)],
        'messages': [(uuid.UUID(int=140).hex, CHAT, 'user', 'Hello ' + SENTINEL, NOW),
                     (uuid.UUID(int=141).hex, CHAT, 'assistant', 'Reply', NOW + 1)],
        'settings': [(key, json.dumps(value)) for key, value in settings.items()],
        'actions': [(ACTION, CHAT, 'command', 'pending', json.dumps({'command': 'echo ' + SENTINEL}), NOW),
                    (APPLIED, CHAT, 'command', 'applied', json.dumps({'command': 'echo done', 'result': RESULT}), NOW)],
        'memories': [('memory-' + kind, kind, f'{kind} {SENTINEL}', 'global', CHAT, 'user', NOW, NOW,
                      NOW + 86400 if kind == 'temporal' else None) for kind in KINDS],
        'checkpoints': [(CHAT, json.dumps({'goal': 'legacy'}), NOW)],
        'metrics': [(uuid.UUID(int=141).hex, CHAT, json.dumps({'calls': 1}))],
        'runs': [(RUN, CHAT, 'Legacy goal', 'running', config, None, 1, 0.5, None, NOW, NOW),
                 (RUN2, CHAT, 'Applied goal', 'waiting_approval', config, None, 1, 0.5, None, NOW, NOW)],
        'run_events': [(RUN, 1, 'created', '{}', NOW)],
        'run_tools': [(RUN, '1:call', 'RunCommand', '{"command":"echo"}', 'running', json.dumps({'action_id': ACTION})),
                      (RUN2, '1:call', 'RunCommand', '{"command":"echo done"}', 'running', json.dumps({'action_id': APPLIED}))],
        'run_action_owners': [(ACTION, RUN, '1:call', 'command'), (APPLIED, RUN2, '1:call', 'command')],
        'connector_grants': [('fixture-plugin', 'grant-1', 3, 'connected', json.dumps({'client_id': 'fixture-client'}), NOW)],
        'connector_registrations': [('registration-1', 'fixture-plugin', 'https://issuer.invalid', 'https://resource.invalid',
                                     'b' * 64, 'c' * 64, 'admitted', None, '{}', NOW)],
        'jobs': [(JOB, CHAT, 'human', 'Legacy job', 'Check the monitor', json.dumps({'kind': 'interval', 'seconds': 3600}),
                  0, 'paused', 2, json.dumps({'config': {}, 'workspace': None}), 'f' * 64, NOW, None, None, None, None,
                  None, NOW, NOW)],
        'job_fires': [(FIRE, JOB, CHAT, 2, NOW - 60, 0, 'host', 'claimed', None, None, None, 'f' * 64, None, None,
                       NOW - 60, NOW - 60)],
        'field_notes': [(1, 'note ' + SENTINEL, b'\x00\x01binary'), (2, 'second note', None)],
    }


def seed(home, port, keep_open=False):
    home.mkdir(mode=0o700)
    db = sqlite3.connect(home / DATABASE, isolation_level=None, check_same_thread=False)
    db.execute('PRAGMA journal_mode=WAL')
    if keep_open:
        db.execute('PRAGMA wal_autocheckpoint=0')  # committed frames stay in the WAL
    db.executescript(LEGACY)
    db.execute('BEGIN')
    for table, values in rows(port).items():
        db.executemany(f'INSERT INTO {table} VALUES({",".join("?" * len(values[0]))})', values)
    db.execute('COMMIT')
    if keep_open:
        return db
    db.close()
    return None


def read(path, sql, args=(), immutable=False):
    uri = path.as_uri() + ('?mode=ro&immutable=1' if immutable else '?mode=ro')
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as db:
        return db.execute(sql, args).fetchall()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def operational(home):
    return read(home / DATABASE,
                'SELECT (SELECT count(*) FROM run_events),(SELECT count(*) FROM job_notifications),'
                '(SELECT count(*) FROM job_fires),(SELECT count(*) FROM upgrade_history),'
                "(SELECT group_concat(s) FROM (SELECT id||':'||status AS s FROM actions ORDER BY id)),"
                "(SELECT group_concat(s) FROM (SELECT id||':'||state AS s FROM runs ORDER BY id))")[0]


def stopped(service):
    with contextlib.suppress(OSError):
        service.close()


def refused_start(command, home):
    """Start the service on a database it must refuse; return its stderr tail."""
    service = Service(command, home)
    try:
        service.rpc('state')
    except (RuntimeError, OSError):
        pass
    else:
        stopped(service)
        raise AssertionError('The service started on a database it must refuse.')
    service.process.wait(timeout=30)
    service.stderr.seek(0)
    text = service.stderr.read()[-4000:]
    code = service.process.returncode
    stopped(service)
    return code, text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    command = [str(args.backend.resolve())] if args.backend else [sys.executable, str(ROOT / 'backend/main.py')]
    if not migrations.supported():
        raise SystemExit('Pre-upgrade safety needs POSIX descriptor-relative file operations; this platform is not supported.')
    if args.report and args.report.exists():
        raise SystemExit('Refusing to overwrite an existing report: ' + str(args.report))
    checks, requests = [], []

    def check(name, condition):
        if not condition:
            raise AssertionError('Phase 10 migration check failed: ' + name)
        if name in checks:
            raise AssertionError('Duplicate check name: ' + name)
        checks.append(name)

    class Recorder(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_GET(self):
            requests.append('GET')
            self.send_error(503)

        def do_POST(self):
            requests.append('POST')
            self.send_error(503)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    backup_count = 0
    try:
        with tempfile.TemporaryDirectory(prefix='pebble-phase10-migrations-') as scratch:
            scratch = Path(scratch)
            home = scratch / 'home'
            writer = seed(home, port, keep_open=True)
            try:
                writer.execute('INSERT INTO chats VALUES(?,?,?,?)', (WAL_CHAT, 'Committed only in the WAL', None, NOW))
                check('legacy WAL fixture holds committed frames outside the main file',
                      (home / (DATABASE + '-wal')).stat().st_size > 0)
                before = {name: read(home / DATABASE, sql) for name, sql in PRESERVED.items()}
                report = migrations.inspect(home)
                check('legacy fixture inspects as upgrade_required at epoch 0 with no backup',
                      report['state'] == 'upgrade_required' and report['database']['user_version'] == 0
                      and report['backups'] == [] and report['database']['memory_layers'] == dict.fromkeys(KINDS, 1))
                service = Service(command, home)
                try:
                    service.rpc('state')
                    report = migrations.inspect(home, deep=True)
                    [backup] = report['backups']
                    backup_count = len(report['backups'])
                    check('service startup completed one verified upgrade backup and stamped the epoch',
                          report['state'] == 'current' and report['database']['user_version'] == migrations.SCHEMA_EPOCH
                          and backup['state'] == 'completed' and backup['verified'])
                    copy = home / BACKUPS / (backup['id'] + '.sqlite')
                    check('backup is a self-contained SQLite online-backup copy including committed WAL frames',
                          copy.read_bytes()[18:20] == b'\x01\x01'
                          and read(copy, 'SELECT count(*) FROM chats WHERE id=?', (WAL_CHAT,), immutable=True) == [(1,)]
                          and read(copy, 'PRAGMA user_version', immutable=True) == [(0,)])
                    verified = migrations.verify_backup(home, backup['id'])
                    check('deep backup verification matches its manifest and all six memory layers',
                          verified['valid'] and verified['memory_layers'] == dict.fromkeys(KINDS, 1))
                    names = sorted(p.name for p in (home / BACKUPS).iterdir())
                    texts = [(home / BACKUPS / name).read_text() for name in names if name.endswith('.json')]
                    check('backup files are private and their records hold no row values or unknown names',
                          (home / BACKUPS).stat().st_mode & 0o777 == 0o700
                          and all((home / BACKUPS / name).stat().st_mode & 0o777 == 0o400 for name in names)
                          and len(texts) == 2 and not any(SENTINEL in t or 'field_notes' in t for t in texts)
                          and SENTINEL not in json.dumps(report))
                    chat = service.rpc('get_chat', id=CHAT)
                    check('conversation, messages and broker actions are served unchanged after upgrade',
                          chat['title'] == 'Legacy ' + SENTINEL
                          and [m['content'] for m in chat['messages']] == ['Hello ' + SENTINEL, 'Reply']
                          and {a['id']: a['status'] for a in chat['actions']} == {ACTION: 'pending', APPLIED: 'applied'})
                    after = {name: read(home / DATABASE, sql) for name, sql in PRESERVED.items()}
                    for name in PRESERVED:
                        check(f'{name} preserved exactly across upgrade', after[name] == before[name])
                    permissions = json.loads(read(home / DATABASE, "SELECT value FROM settings WHERE key='permissions'")[0][0])
                    check('permission mode and every earlier tool flag preserved; only new tools take defaults',
                          permissions['mode'] == 'ask' and all(permissions['tools'][k] == v for k, v in OLD_TOOLS.items()))
                    states = dict(read(home / DATABASE, 'SELECT id,state FROM runs'))
                    tools = {r: (s, res) for r, s, res in read(home / DATABASE, 'SELECT run_id,state,result FROM run_tools')}
                    check('interrupted runs settle fail-closed without replaying their tools',
                          states == {RUN: 'needs_reconciliation', RUN2: 'interrupted'}
                          and tools[RUN] == ('uncertain', json.dumps({'action_id': ACTION})))
                    check('only an action applied before the upgrade is read back as completed',
                          tools[RUN2] == ('completed', encoded({'action_id': APPLIED, 'status': 'applied', 'result': RESULT})))
                    check('interrupted registration and claimed fire settle as unknown and needs_review, not replayed',
                          read(home / DATABASE, 'SELECT state FROM connector_registrations') == [('unknown',)]
                          and read(home / DATABASE, 'SELECT status,run_id FROM job_fires') == [('needs_review', None)])
                    check('upgrade history links the epoch stamp to its backup',
                          read(home / DATABASE, 'SELECT backup_id,from_version,to_version FROM upgrade_history')
                          == [(backup['id'], 0, migrations.SCHEMA_EPOCH)])
                    check('no provider request during upgrade startup', requests == [])
                finally:
                    stopped(service)
            finally:
                writer.close()
            settled = operational(home)
            service = Service(command, home)
            try:
                service.rpc('state')
                report = migrations.inspect(home)
                check('restart creates no second backup and keeps the same verified file',
                      [(b['id'], b['sha256'], b['state']) for b in report['backups']]
                      == [(backup['id'], backup['sha256'], 'completed')] and report['state'] == 'current')
                check('restart repeats no recovery, notification, fire or history effect', operational(home) == settled)
                check('no provider request after restart', requests == [])
            finally:
                stopped(service)

            live = digest(home / DATABASE)
            restored = scratch / 'restored'
            result = migrations.restore_copy(home, backup['id'], restored)
            check('offline restore into a new directory matches the manifest and restores no credentials',
                  result['logical_match'] and result['integrity'] == 'ok' and result['credentials_restored'] is False
                  and result['user_version'] == 0 and digest(home / DATABASE) == live)
            service = Service(command, restored)
            try:
                check('service opens the restored disposable copy with its conversation',
                      service.rpc('get_chat', id=CHAT)['title'] == 'Legacy ' + SENTINEL)
            finally:
                stopped(service)
            report = migrations.inspect(restored)
            check('restored copy upgrades itself with its own backup; the source home is unchanged',
                  report['state'] == 'current' and [b['state'] for b in report['backups']] == ['completed']
                  and digest(home / DATABASE) == live)

            for name, statement in (('a future-epoch database', 'PRAGMA user_version=99'),
                                    ('an incompatible core table', 'ALTER TABLE chats ADD COLUMN pinned INTEGER')):
                refused = scratch / name.replace(' ', '-')
                seed(refused, port)
                with contextlib.closing(sqlite3.connect(refused / DATABASE)) as db:
                    db.execute(statement)
                    db.commit()
                before = digest(refused / DATABASE)
                code, text = refused_start(command, refused)
                check(f'service refuses {name} before any mutation',
                      code != 0 and 'Pebble stopped this upgrade' in text and digest(refused / DATABASE) == before
                      and not (refused / BACKUPS).exists())
            check('no provider request in any probe', requests == [])
    finally:
        server.shutdown()
        server.server_close()
    paths = ['backend/main.py', *[str(p.relative_to(ROOT)) for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'scripts/validate_runtime.py', 'scripts/validate_phase10_migrations.py']
    report = {'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if args.backend else 'source',
              'passed': True, 'checks': checks, 'check_count': len(checks), 'backup_count': backup_count,
              'synthetic_provider_attempts': len(requests), 'schema_epoch': migrations.SCHEMA_EPOCH,
              'source_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths},
              'backend_sha256': hashlib.sha256(args.backend.read_bytes()).hexdigest() if args.backend else None,
              'inspector': 'source pebble.migrations: read-only inspect/verify and offline restore into new disposable directories',
              'limits': ('Synthetic legacy fixture in temporary macOS homes only. SQLite database scope: attachments, '
                         'worktrees, other files and OS-vault records are not backed up or restored. No installed '
                         'service, live home, real model, signing, Windows or other-platform proof.')}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with open(args.report, 'x') as handle:  # never replace a historical report
            handle.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
