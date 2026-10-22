"""Actual Phase 10 fixture -> current service upgrade and downgrade refusal.

Uses the parent-owned synthetic epoch-1 fixture and preserved old candidate.
Never touches an installed home, account, credential or model. Each mode gets
its own private copy. The fixture intentionally contains manual synthetic
working/episodic rows; they are preservation fixtures, not learned evidence.
"""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from validate_runtime import ROOT, Service
sys.path.insert(0, str(ROOT / 'backend'))
from pebble.migrations import SCHEMA_EPOCH


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(home, statement):
    with sqlite3.connect((home / 'pebble.sqlite').as_uri() + '?mode=ro', uri=True) as db:
        return db.execute(statement).fetchall()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if args.report.exists() or args.report.is_symlink():
        raise ValueError('Preserve existing upgrade evidence; select a new report path.')
    old = ROOT / '.runtime/harness-candidate/pebble-backend/pebble-backend'
    fixture = ROOT / '.runtime/phase11-epoch1-fixture'
    metadata = json.loads((ROOT / '.runtime/phase11-epoch1-fixture.json').read_text())
    assert metadata['synthetic_only'] and not metadata['credentials_used'] and metadata['inference_calls'] == 0
    assert metadata['old_executable_sha256'] == sha(old)
    assert fixture.is_dir() and not fixture.is_symlink()
    assert not any(path.is_symlink() for path in fixture.rglob('*'))
    assert read(fixture, 'PRAGMA user_version') == [(1,)]
    executable = args.backend.resolve(strict=True) if args.backend else ROOT / 'backend/main.py'
    command = [str(executable)] if args.backend else [sys.executable, str(executable)]
    checks = []
    with tempfile.TemporaryDirectory(prefix='pebble-phase11-upgrade-') as directory:
        home = Path(directory) / 'home'
        shutil.copytree(fixture, home)
        before = {key:read(home, statement) for key,statement in {
            'memories':'SELECT * FROM memories ORDER BY id', 'checkpoints':'SELECT * FROM checkpoints ORDER BY chat_id',
            'messages':'SELECT * FROM messages ORDER BY id', 'chats':'SELECT * FROM chats ORDER BY id',
            'permissions':"SELECT * FROM settings WHERE key='permissions'",
        }.items()}
        assert {row[1] for row in before['memories']} >= {'working','episodic','semantic','procedural','temporal','profile'}
        service = Service(command, home)
        try:
            state = service.rpc('state')
            assert state['active'] is None
            assert read(home, 'PRAGMA user_version') == [(SCHEMA_EPOCH,)]
            assert read(home, 'SELECT count(*) FROM agent_usage') == [(0,)]
            checks.append('actual service upgrades preserved Phase 10 epoch1 fixture to current epoch with empty measured-usage ledger')
        finally:
            service.close()
        for key,statement in {
            'memories':'SELECT * FROM memories ORDER BY id','checkpoints':'SELECT * FROM checkpoints ORDER BY chat_id',
            'messages':'SELECT * FROM messages ORDER BY id','chats':'SELECT * FROM chats ORDER BY id',
            'permissions':"SELECT * FROM settings WHERE key='permissions'",
        }.items():
            assert read(home, statement) == before[key], key
        checks += ['all six synthetic memory layers and checkpoint survive upgrade exactly',
                   'conversation rows and explicit file permissions survive upgrade exactly']
        history = read(home,'SELECT backup_id,from_version,to_version FROM upgrade_history WHERE from_version=1 AND to_version=' + str(SCHEMA_EPOCH))
        assert len(history) == 1
        sys.path.insert(0, str(ROOT / 'backend'))
        from pebble import migrations
        backup = home / migrations.BACKUPS / (history[0][0] + '.sqlite')
        assert backup.is_file() and not backup.is_symlink()
        with sqlite3.connect(backup.as_uri()+'?mode=ro&immutable=1',uri=True) as db:
            assert db.execute('PRAGMA user_version').fetchall() == [(1,)]
            assert db.execute('SELECT * FROM memories ORDER BY id').fetchall() == before['memories']
        checks.append('verified pre-upgrade backup retains exact epoch1 memory rows')
        service = Service(command, home)
        try:
            assert service.rpc('state')['active'] is None
        finally:
            service.close()
        assert read(home,'SELECT backup_id,from_version,to_version FROM upgrade_history WHERE from_version=1 AND to_version=' + str(SCHEMA_EPOCH)) == history
        checks.append('second current-service startup does not create another upgrade or replay work')
        before_db = sha(home/'pebble.sqlite')
        refused = subprocess.run([str(old),'--home',str(home),'--resources',str(ROOT/'src-tauri/resources')],
                                 input='',capture_output=True,text=True,timeout=30)
        assert refused.returncode != 0
        assert 'Pebble stopped this upgrade: The database was written by a newer Pebble.' in refused.stderr
        assert sha(home/'pebble.sqlite') == before_db
        checks.append('preserved Phase 10 executable refuses current epoch before mutating database')
    result = {'passed':True,'kind':'frozen' if args.backend else 'source','date':datetime.now(timezone.utc).isoformat(),
              'backend_sha256':sha(executable),'legacy_backend_sha256':sha(old),'checks':checks,
              'fixture_metadata_sha256':sha(ROOT/'.runtime/phase11-epoch1-fixture.json'),
              'source_sha256':{p:sha(ROOT/p) for p in ('scripts/validate_phase11_upgrade.py','backend/pebble/migrations.py')},
              'synthetic_only':True,'inference_calls':0,'credentials_used':False}
    descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'kind':result['kind'],'checks':len(checks),'passed':True}))


if __name__ == '__main__':
    main()
