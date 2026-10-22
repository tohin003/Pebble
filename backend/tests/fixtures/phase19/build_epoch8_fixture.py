"""Build the synthetic epoch-8 upgrade fixture with the preserved Phase 18 candidate (parent step, run once).

    python .runtime/phase19-baseline/build_epoch8_fixture.py

1. Verify the preserved epoch-7 fixture (.runtime/phase17-epoch7-fixture) against its own record and
   the preserved Phase 18 executable against EXPECTED_OLD (the Phase 18 candidate, reports/
   harness-candidate-manifest.json). Nothing runs when either differs.
2. Copy the epoch-7 fixture into a private temporary home. Start the Phase 18 candidate on it: it waits
   in upgrade maintenance because the backup budget is full, so remove the oldest backup through the
   native route (confirmed by the manifest hash the service lists and the epoch-7 record) and retry in
   place. The candidate upgrades epoch 7 to 8 once and settles the epoch-7 in-flight screen and
   connector rows fail-closed. Three backups remain (epochs 5->6, 6->7 and 7->8), so the Phase 19
   upgrade must wait in maintenance too.
3. Insert settled synthetic rows through the epoch-8 columns: Phase 17 lifecycle, notification, export
   and goal records, two personal settings, and the artifact chains Phase 19 changes how it reads (two
   keyed v1 quizzes, one updated then stopped, a static html view that stored a script, a table).
   Start the candidate once more: it must change none of them.
4. Insert the in-flight rows a crash leaves (an export render and an export save), checkpoint the WAL,
   record file hashes, row counts, IDs and every input's hash, and copy the home to
   .runtime/phase19-epoch8-fixture with its record .runtime/phase19-epoch8-fixture.json. Neither may
   exist beforehand.

No model, network, account, credential or user data is used; every row is synthetic and every ID is
a fixed builder ID. scripts/validate_phase19_upgrade.py reads IDS, SETTLED, IN_FLIGHT and SETTLES_TO
from this file and checks the builder's rows against the current backend's own readers.
"""
import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / '.runtime/phase17-epoch7-fixture'
SOURCE_META = ROOT / '.runtime/phase17-epoch7-fixture.json'
OLD = ROOT / '.runtime/phase18-candidate/pebble-backend/pebble-backend'
EXPECTED_OLD = 'a6f8749a165ca09c5bce57abe012923a03d0b1c43cf15e84e3c0e613ea6e0f9a'
OUT = ROOT / '.runtime/phase19-epoch8-fixture'
META = ROOT / '.runtime/phase19-epoch8-fixture.json'
SOURCE_EPOCH, EPOCH = 7, 8
BACKUPS = 'upgrade-backups'
SIDECARS = frozenset({'pebble.sqlite-shm', 'pebble.sqlite-wal'})
HISTORY = 'SELECT backup_id,from_version,to_version,completed FROM upgrade_history ORDER BY id'

# Fixed builder IDs (32 hex characters, prefix e18).
IDS = {name: prefix + '0' * 28 for name, prefix in (
    ('quiz', 'e180'), ('stopped_quiz', 'e181'), ('html', 'e182'), ('table', 'e183'),
    ('sleep', 'e184'), ('zone', 'e185'), ('notice', 'e186'), ('banner', 'e187'), ('delivery', 'e188'), ('job', 'e189'),
    ('export', 'e18a'), ('save', 'e18b'), ('plan', 'e18c'), ('goal', 'e18d'),
    ('rendering', 'e18e'), ('writing', 'e18f'), ('inflight_plan', 'e190'))}
ARTIFACTS = ('quiz', 'stopped_quiz', 'html', 'table')
# The epoch-7 fixture's in-flight rows, settled fail-closed by the Phase 18 candidate's startup.
EPOCH7_SETTLED = {'live_session': ('screen_sessions', 'session_id', 'restarted'),
                  'admitted_act': ('screen_actions', 'action_id', 'unknown'),
                  'proposed_act': ('screen_actions', 'action_id', 'expired'),
                  'admitted_effect': ('connector_effects', 'action_id', 'unknown')}
# This fixture's in-flight rows: (table, pre-upgrade state, the state Phase 19 startup must settle them to).
IN_FLIGHT = {'rendering': ('export_records', 'rendering'), 'writing': ('export_saves', 'writing')}
SETTLES_TO = {'rendering': 'failed', 'writing': 'uncertain'}
SETTINGS = {'reduced_motion': True, 'theme': 'dark'}
QUESTION = {'question': 'Which gas do plants absorb?', 'choices': ['Oxygen', 'Carbon dioxide'], 'answer': 1,
            'explanation': 'Plants take in carbon dioxide for photosynthesis.'}
QUIZ_V1 = {'duration_minutes': 10, 'questions': [
    QUESTION, {'question': 'Which planet is largest?', 'choices': ['Jupiter', 'Mars', 'Venus'], 'answer': 0,
               'explanation': 'Jupiter is the largest planet in the solar system.'}]}
QUIZ_V2 = {'duration_minutes': 5, 'questions': [QUESTION]}
HTML = {'html': '<h1>Release notes</h1><p>Static text only.</p>', 'css': 'h1{color:teal}',
        'script': 'document.title = "stored before Phase 19"', 'language': 'javascript'}
TABLE = {'columns': [{'id': 'item', 'label': 'Item'}, {'id': 'state', 'label': 'State'}],
         'rows': [{'item': 'Fixture table', 'state': 'kept'}]}
ARTIFACT_COLUMNS = ('artifact_id', 'chat_id', 'revision', 'title', 'kind', 'data', 'owner_run_id', 'evidence_ids',
                    'status', 'created', 'updated', 'data_hash', 'previous_hash', 'record_hash')


def sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def encode(value):
    """artifacts.Artifacts._encode, also exports.canonical."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def files_of(home):
    return {p.relative_to(home).as_posix(): sha(p) for p in sorted(home.rglob('*'))
            if p.is_file() and not p.is_symlink() and p.name not in SIDECARS}


def owner_run(db):
    row = db.execute("SELECT id,chat_id FROM runs WHERE state='completed' ORDER BY id LIMIT 1").fetchone()
    if not row:
        raise SystemExit('The epoch-7 fixture has no completed run to own the synthetic rows.')
    return row


def insert_artifact(db, ident, chat, owner, kind, title, revisions, created):
    """One artifact chain exactly as artifacts.Artifacts._append writes it: revisions [(data, status, updated)]."""
    previous = ''
    for number, (data, status, updated) in enumerate(revisions, 1):
        snapshot = dict(zip(ARTIFACT_COLUMNS[:-1], (ident, chat, number, title, kind, encode(data), owner, encode([]),
                                                     status, created, updated, digest(data), previous)))
        snapshot['record_hash'] = digest(snapshot)
        db.execute(f'INSERT INTO artifact_revisions VALUES({",".join("?" * len(ARTIFACT_COLUMNS))})',
                   tuple(snapshot[c] for c in ARTIFACT_COLUMNS))
        previous = snapshot['record_hash']
    db.execute('INSERT INTO artifact_registry VALUES(?,?,?)', (ident, chat, created))
    db.execute('INSERT INTO artifact_heads VALUES(?,?,?)', (ident, len(revisions), previous))
    db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?)', (ident, chat, title, kind, encode(revisions[-1][0]), created))


def insert_settled(db, now):
    """Settled synthetic epoch-8 rows no startup step touches. Returns the rows added per table."""
    rid, chat = owner_run(db)
    ids, added = IDS, {}

    def insert(table, **row):
        db.execute(f'INSERT INTO {table}({",".join(row)}) VALUES({",".join("?" * len(row))})', tuple(row.values()))
        added[table] = added.get(table, 0) + 1

    insert('lifecycle_observations', id=ids['sleep'], kind='sleep', source='clock', observed=now - 900,
           started=now - 1500, seconds=600.0, zone=None)
    insert('lifecycle_observations', id=ids['zone'], kind='timezone_changed', source='native', observed=now - 880,
           started=None, seconds=0.0, zone='Asia/Kolkata')
    insert('host_notices', id=ids['notice'], chat_id=None, job_id=None, kind='wake_summary',
           fingerprint=hashlib.sha256(b'phase19-synthetic-wake').hexdigest(), title='While this Mac slept',
           body='While this Mac slept (about 10 minutes), 1 schedule was due.', created=now - 870,
           acknowledged=now - 860)
    insert('native_banners', id=ids['banner'], scope='job', job_id=ids['job'], chat_id=chat, members=1, version=1,
           state='delivered', delivered_version=1, attempts=1, reason=None, opened=now - 850, posted=now - 849,
           updated=now - 849)
    insert('native_deliveries', notification_id=ids['delivery'], job_id=ids['job'], chat_id=chat, category='completed',
           state='delivered', reason=None, banner_id=ids['banner'], banner_version=1, attempts=1, created=now - 850,
           posted=now - 849, updated=now - 849)
    source = {'kind': 'artifact', 'id': ids['table'], 'revision': 1}
    insert('export_records', id=ids['export'], chat_id=chat, plan_id=ids['plan'], format='csv', source=packed(source),
           state='verified', receipt=packed({'status': 'verified', 'format': 'csv', 'checks': []}),
           created=now - 840, updated=now - 839)
    insert('export_saves', id=ids['save'], export_id=ids['export'], chat_id=chat, state='saved',
           path='/private/tmp/pebble-phase19-synthetic-export.csv',
           sha256=hashlib.sha256(b'phase19-synthetic-export').hexdigest(), verified=1, created=now - 830,
           updated=now - 829)
    insert('goals', id=ids['goal'], chat_id=chat, origin='human', title='Synthetic Phase 19 fixture goal', revision=2,
           state='active', created=now - 820, updated=now - 800)
    insert('goal_criteria', goal_id=ids['goal'], id='c1', ordinal=1, text='The person reviewed the quiz results.',
           kind='human_check', status='open', origin='human', evidence_ids='[]', checked_by=None,
           proposed_run_id=None, updated=now - 810)
    insert('goal_steps', goal_id=ids['goal'], id='s1', ordinal=1, title='Publish the practice quiz', status='completed',
           depends_on='[]', criteria='["c1"]', evidence_ids='[]', blocker_kind=None, blocker_ref=None, run_id=rid,
           updated=now - 805)
    insert('goal_events', goal_id=ids['goal'], sequence=1, kind='created', origin='human', run_id=None, payload='{}',
           created=now - 820)
    for key, value in SETTINGS.items():
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))
        added['settings'] = added.get('settings', 0) + 1
    # Artifact chains (Phase 7 tables): keyed v1 quizzes, a stored script and a table, read back by Phase 19.
    chains = {'quiz': ('quiz', 'Practice quiz', [(QUIZ_V1, 'ready', now - 790), (QUIZ_V2, 'ready', now - 780)]),
              'stopped_quiz': ('quiz', 'Stopped quiz', [(QUIZ_V1, 'ready', now - 770), (QUIZ_V2, 'ready', now - 760),
                                                        (QUIZ_V2, 'stopped', now - 750)]),
              'html': ('html', 'Release notes', [(HTML, 'review', now - 740)]),
              'table': ('table', 'Fixture table', [(TABLE, 'ready', now - 730)])}
    for name, (kind, title, revisions) in chains.items():
        insert_artifact(db, ids[name], chat, rid, kind, title, revisions, revisions[0][2])
        for table, count in (('artifacts', 1), ('artifact_registry', 1), ('artifact_heads', 1),
                             ('artifact_revisions', len(revisions))):
            added[table] = added.get(table, 0) + count
    return added


def insert_in_flight(db, now):
    """The rows a crash leaves mid-export, after the last candidate start. Returns the rows added per table."""
    _, chat = owner_run(db)
    receipt = {'export_id': IDS['rendering'], 'plan_id': IDS['inflight_plan'], 'chat_id': chat, 'format': 'html',
               'status': 'rendering', 'title': 'Fixture table', 'file': None, 'checks': [], 'failed_checks': [],
               'readback': None, 'reason': None, 'created': now - 60, 'completed': None, 'published': False,
               'authorization': 'none'}
    db.execute('INSERT INTO export_records VALUES(?,?,?,?,?,?,?,?,?)',
               (IDS['rendering'], chat, IDS['inflight_plan'], 'html',
                packed({'kind': 'artifact', 'id': IDS['table'], 'revision': 1}), 'rendering', encode(receipt), now - 60,
                now - 60))
    db.execute('INSERT INTO export_saves VALUES(?,?,?,?,?,?,?,?,?)',
               (IDS['writing'], IDS['export'], chat, 'writing', '/private/tmp/pebble-phase19-synthetic-inflight.csv',
                None, 0, now - 50, now - 50))
    return {'export_records': 1, 'export_saves': 1}


def states(db):
    return {name: db.execute(f'SELECT state FROM {table} WHERE id=?', (IDS[name],)).fetchone()[0]
            for name, (table, _) in IN_FLIGHT.items()}


def rows_of(db):
    """{table: sorted rows} for every data table (full-text structures excluded)."""
    names = [n for (n, s) in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")
             if not (s or '').upper().startswith('CREATE VIRTUAL TABLE') and not n.startswith('memory_search')]
    return {n: sorted(map(repr, db.execute(f'SELECT * FROM "{n}"').fetchall())) for n in names}


def table_rows(db):
    return {name: count for name, count in ((n, db.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0])
                                            for n in rows_of(db))}


def verify_inputs():
    """The epoch-7 fixture matches its record and the Phase 18 executable matches the candidate."""
    if os.path.lexists(OUT) or os.path.lexists(META):
        raise SystemExit('The epoch-8 fixture or its record already exists; earlier evidence is never replaced.')
    record = json.loads(SOURCE_META.read_text())
    if record.get('epoch') != SOURCE_EPOCH or record.get('synthetic_only') is not True or record.get('credentials_used'):
        raise SystemExit('The epoch-7 record is not a synthetic, credential-free epoch-7 fixture.')
    recorded = {k: v for k, v in record['files_sha256'].items() if Path(k).name not in SIDECARS}
    if files_of(SOURCE) != recorded:
        raise SystemExit('The epoch-7 fixture differs from its record.')
    info = os.lstat(OLD)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or sha(OLD) != EXPECTED_OLD):
        raise SystemExit('The preserved Phase 18 executable is missing, writable by others or not the candidate.')
    return record


def upgrade_with_candidate(home, record):
    """The Phase 18 candidate's own 7 -> 8 upgrade, through its native maintenance routes."""
    sys.path.insert(0, str(ROOT / 'scripts'))
    from validate_runtime import Service
    service = Service([str(OLD)], home)
    try:
        view = service.rpc('state').get('maintenance')
        if not view or view.get('reason') != 'backup_budget_exhausted':
            raise SystemExit('The candidate did not wait for the full backup budget: ' + json.dumps(view)[:300])
        listing = {item['id']: item for item in view['backups']}
        oldest = sorted(listing)[0]
        confirmation = listing[oldest]['manifest_sha256']
        if record['files_sha256'].get(f'{BACKUPS}/{oldest}.manifest.json') != confirmation:
            raise SystemExit('The candidate lists another manifest hash than the epoch-7 record.')
        if service.rpc('upgrade_discard_backup', backup_id=oldest, manifest_sha256=confirmation)['discarded'] != oldest:
            raise SystemExit('The oldest backup was not discarded.')
        if service.rpc('upgrade_retry') != {'ready': True} or service.rpc('state')['active'] is not None:
            raise SystemExit('The candidate did not finish the upgrade in place.')
    finally:
        service.close()
    return oldest


def restart_unchanged(home):
    sys.path.insert(0, str(ROOT / 'scripts'))
    from validate_runtime import Service
    with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite')) as db:
        before = rows_of(db)
    service = Service([str(OLD)], home)
    try:
        if service.rpc('state')['active'] is not None:
            raise SystemExit('The candidate started a run on the fixture.')
    finally:
        service.close()
    with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite')) as db:
        changed = sorted(t for t, rows in rows_of(db).items() if before.get(t) != rows)
    if changed:
        raise SystemExit('The candidate changed settled synthetic rows: ' + ', '.join(changed))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.parse_args()
    record = verify_inputs()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase19-epoch8-')).resolve()
    home = scratch / 'home'
    shutil.copytree(SOURCE, home)
    discarded = upgrade_with_candidate(home, record)
    with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite')) as db:
        if db.execute('PRAGMA user_version').fetchone() != (EPOCH,):
            raise SystemExit('The candidate did not leave the copy at epoch 8.')
        if [row[1:3] for row in db.execute(HISTORY).fetchall()][-1] != (SOURCE_EPOCH, EPOCH):
            raise SystemExit('The upgrade history does not end with 7 -> 8.')
        settled_states = {name: db.execute(f'SELECT state FROM {table} WHERE {key}=?',
                                           (record['synthetic_ids'][name],)).fetchone()
                          for name, (table, key, _) in EPOCH7_SETTLED.items()}
        if settled_states != {name: (spec[2],) for name, spec in EPOCH7_SETTLED.items()}:
            raise SystemExit('The epoch-7 in-flight rows did not settle fail-closed: ' + json.dumps(settled_states))
        now = time.time()
        with db:
            settled = insert_settled(db, now - 3600)
    restart_unchanged(home)
    with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite')) as db:
        with db:
            flying = insert_in_flight(db, time.time())
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        counts = table_rows(db)
        if states(db) != {name: spec[1] for name, spec in IN_FLIGHT.items()}:
            raise SystemExit('The in-flight rows are not in flight.')
    for name in SIDECARS:
        path = home / name
        if path.is_file() and not path.stat().st_size:
            path.unlink()
    if (home / 'pebble.sqlite-wal').exists():
        raise SystemExit('The fixture still has WAL frames after the checkpoint.')
    files = files_of(home)
    shutil.copytree(home, OUT)
    if files_of(OUT) != files:
        raise SystemExit('The copied fixture differs from the built home; the scratch home is kept at ' + str(scratch))
    synthetic = dict(settled)
    for table, count in flying.items():
        synthetic[table] = synthetic.get(table, 0) + count
    metadata = {
        'created': datetime.now(timezone.utc).isoformat(), 'epoch': EPOCH, 'synthetic_only': True,
        'credentials_used': False, 'inference_calls': 0,
        'source_fixture': SOURCE.relative_to(ROOT).as_posix(), 'source_metadata_sha256': sha(SOURCE_META),
        'old_executable_sha256': EXPECTED_OLD, 'builder_sha256': sha(Path(__file__)),
        'discarded_backup': discarded, 'files_sha256': files, 'table_rows': counts,
        'synthetic_rows': synthetic, 'settled_rows': settled, 'in_flight_rows': flying,
        'synthetic_ids': {**IDS, **{name: record['synthetic_ids'][name] for name in EPOCH7_SETTLED}},
        'in_flight': {name: spec[1] for name, spec in IN_FLIGHT.items()}, 'settles_to': SETTLES_TO,
        'note': ('Built by the preserved Phase 18 candidate from the epoch-7 fixture: one native 7 -> 8 upgrade '
                 '(oldest backup removed through upgrade_discard_backup), then settled synthetic Phase 17 rows, '
                 'v1 quiz, html and table artifact chains and two settings, a restart that changed nothing, then '
                 'an in-flight export render and save. No model, network, account or credential.'),
    }
    with open(META, 'x', encoding='utf-8') as out:
        out.write(json.dumps(metadata, indent=2, sort_keys=True) + '\n')
    shutil.rmtree(scratch, ignore_errors=True)
    print(json.dumps({'fixture': OUT.relative_to(ROOT).as_posix(), 'files': len(files), 'epoch': EPOCH,
                      'synthetic_rows': synthetic}, indent=2))


if __name__ == '__main__':
    main()
