"""Phase 22 (s22-storage): the one-time move from the old app-data folder into ~/.pebble/data.

Under a fake HOME only. The old home is built by the repository's own App (epoch 9) or by test_migrations'
epoch-0 fixture. The move must copy, verify (integrity_check, per-table rows and digests, a SHA-256 per file),
switch, then write MOVED.md; never delete the old folder; stay on the old home after any refusal, failure or
interruption; and be idempotent. Existing upgrade backups keep working after the move.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from pebble import data_home, migrations
from pebble.app import App
from pebble.data_home import resolve
from pebble.runtime import claim_owner

from test_migrations import legacy_home

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    monkeypatch.setenv('AGENTGUARD_HOME', str(tmp_path / 'guard-unused'))  # App's GuardAdapter sets it; restored after
    yield home
    data_home.deactivate()


@pytest.fixture(autouse=True)
def no_vault(monkeypatch):
    from pebble import vault

    def refuse(self, *args, **kwargs):
        raise AssertionError('The storage tests must never reach the OS vault.')
    for name in ('get', 'set', 'delete', '_find'):
        monkeypatch.setattr(vault.Vault, name, refuse)


def old_folder(home):
    folder = home / 'Library' / 'Application Support' / 'com.pebble.local'
    folder.parent.mkdir(parents=True, exist_ok=True)
    return folder


def build_old_home(home):
    """An epoch-9 home made by App, with the kinds of files a real one holds."""
    legacy = old_folder(home)
    app = App(legacy, home / 'resources', lambda *event: None)
    try:
        cid = app.store.new_chat()
        app.store.message(cid, 'user', 'Plan a trip to Lisbon')
        app.store.message(cid, 'assistant', 'Here is a plan for Lisbon.')
        app.memory.put('semantic', 'Lisbon is the capital of Portugal.')
    finally:
        app.close()
    (legacy / 'attachments' / 'abc123.pdf').write_bytes(b'%PDF attachment')
    (legacy / 'runtime.key').write_bytes(b'k' * 32)
    os.chmod(legacy / 'runtime.key', 0o600)
    profile = legacy / 'browser' / 'profile'
    profile.mkdir(parents=True)
    (profile / 'Preferences').write_text('{}')
    os.symlink('profile/Preferences', legacy / 'browser' / 'link')
    (legacy / 'service.log').write_text('the old service log\n')
    (legacy / 'backend.log').write_text('')
    return legacy, cid


def tree(top, skip=()):
    out = {}
    for folder, dirs, files in os.walk(top):
        for name in dirs + files:
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, top)
            if rel in skip:
                continue
            info = os.lstat(path)
            entry = [stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode)]
            if stat.S_ISREG(info.st_mode):
                entry.append(hashlib.sha256(Path(path).read_bytes()).hexdigest())
            elif stat.S_ISLNK(info.st_mode):
                entry.append(os.readlink(path))
            out[rel] = entry
    return out


SQLITE_SIDE = ('pebble.sqlite-wal', 'pebble.sqlite-shm', 'runtime-owner.lock', 'upgrade-owner.lock')


def rows(home):
    with closing(sqlite3.connect((Path(home) / 'pebble.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
        return data_home.database_summary(db)


def move(home, legacy, **options):
    layout = resolve(str(home / '.pebble'), str(legacy), env={})
    data_home.ensure_layout(layout)
    return layout, data_home.migrate(layout, **options)


def test_first_launch_moves_the_old_home_verified_and_leaves_it_in_place(fake_home):
    legacy, cid = build_old_home(fake_home)
    before = tree(legacy, skip=SQLITE_SIDE)
    old_rows = rows(legacy)
    layout, report = move(fake_home, legacy)
    data = fake_home / '.pebble' / 'data'
    assert report['state'] == 'migrated' and report['data'] == str(data) and report['pointer'] == 'written'
    assert report['database']['integrity'] == 'ok' and report['database']['user_version'] == migrations.SCHEMA_EPOCH
    assert report['database']['row_counts']['chats'] == 1 and report['database']['row_counts']['messages'] == 2
    assert sorted(report['left_in_old_folder']) == ['backend.log', 'service.log']
    assert stat.S_IMODE(os.lstat(data).st_mode) == 0o700
    # The copy: same rows and digests per table; every other file byte for byte with its mode; links as links.
    assert rows(data) == old_rows
    copied = tree(data, skip=SQLITE_SIDE + (data_home.MIGRATED, 'pebble.sqlite'))
    expected = {k: v for k, v in before.items() if k not in ('pebble.sqlite', 'service.log', 'backend.log')}
    assert copied == expected
    assert os.readlink(data / 'browser' / 'link') == 'profile/Preferences'
    assert not (data / 'service.log').exists() and not (data / 'runtime-owner.lock').exists()
    receipt = json.loads((data / data_home.MIGRATED).read_text())
    assert receipt['from'] == str(legacy) and receipt['files'] == len([k for k, v in expected.items()
                                                                       if v[0] == stat.S_IFREG])
    # The old folder: unchanged apart from MOVED.md and the pointer (and SQLite's own sidecars and locks).
    after = tree(legacy, skip=SQLITE_SIDE)
    assert {k: v for k, v in after.items() if k not in (data_home.MOVED, data_home.POINTER)} == before
    moved = (legacy / data_home.MOVED).read_text()
    assert str(data) in moved and 'Nothing here was deleted' in moved and '`service.log`' in moved
    pointer = json.loads((legacy / data_home.POINTER).read_text())
    assert pointer['home'] == str(fake_home / '.pebble') and pointer['data'] == str(data)
    assert json.loads((fake_home / '.pebble/runtime/migration.json').read_text())['state'] == 'migrated'
    assert not any(p.name.startswith(data_home.STAGING) for p in (fake_home / '.pebble').iterdir())
    # The service starts on the new home with everything in it.
    app = App(data, fake_home / 'resources', lambda *event: None)
    try:
        state = app.state()
        assert state['data_dir'] == str(data) and [c['id'] for c in state['chats']] == [cid]
        assert [m['content'] for m in app.chat(cid)['messages']] == ['Plan a trip to Lisbon', 'Here is a plan for Lisbon.']
    finally:
        app.close()
    # Idempotent: a second launch keeps using it and copies nothing.
    again = data_home.migrate(layout)
    assert again['state'] == 'current' and again['data'] == str(data) and 'pointer' not in again
    assert again['legacy_changed'] is False


def test_committed_wal_frames_are_part_of_the_copy(fake_home):
    legacy, db = legacy_home(old_folder(fake_home).parent, name='com.pebble.local', keep_open=True)
    try:
        db.execute("INSERT INTO chats VALUES('f' || hex(randomblob(15)),'late',NULL,1.0)")
        assert os.path.getsize(legacy / 'pebble.sqlite-wal') > 0
        expected = rows(legacy)
        layout, report = move(fake_home, legacy)
    finally:
        db.close()
    assert report['state'] == 'migrated', report
    assert rows(layout.data) == expected and expected['tables']['chats']['rows'] >= 1


def test_an_older_epoch_moves_then_upgrades_in_the_new_home_and_upgrade_backups_keep_working(fake_home):
    legacy = legacy_home(old_folder(fake_home).parent, name='com.pebble.local')
    # An earlier upgrade made a backup in the old home; it must stay valid after the move.
    App(legacy, fake_home / 'resources', lambda *event: None).close()
    before = migrations.inspect(legacy)
    old_ids = [b['id'] for b in before['backups']]
    assert old_ids and before['state'] == 'current'
    layout, report = move(fake_home, legacy)
    assert report['state'] == 'migrated'
    data = layout.data
    for backup_id in old_ids:
        assert migrations.verify_backup(data, backup_id)['valid'] is True
    restored = migrations.restore_copy(data, old_ids[0], fake_home / 'restored')
    assert restored['restored'] is True and restored['logical_match'] is True
    App(data, fake_home / 'resources', lambda *event: None).close()
    assert migrations.inspect(data)['state'] == 'current'

    # A home still at epoch 0 moves as it is and is upgraded, with its own new backup, in ~/.pebble/data.
    other = fake_home / 'second'
    other.mkdir()
    old = legacy_home(other, name='old')
    layout = resolve(str(other / '.pebble'), str(old), env={})
    data_home.ensure_layout(layout)
    assert data_home.migrate(layout)['state'] == 'migrated'
    assert rows(layout.data)['user_version'] == 0
    App(layout.data, fake_home / 'resources', lambda *event: None).close()
    inspected = migrations.inspect(layout.data)
    assert inspected['state'] == 'current' and inspected['backups']
    assert rows(old)['user_version'] == 0, 'the old folder stays as it was, for an older Pebble'


def assert_stayed(layout, report, legacy, reason):
    assert report['state'] in ('refused', 'failed') and report['reason'] == reason, report
    assert report['data'] == str(legacy)
    assert not layout.data.exists()
    assert not (legacy / data_home.MOVED).exists() and not (legacy / data_home.POINTER).exists()
    assert not any(p.name.startswith(data_home.STAGING) for p in layout.root.iterdir())
    status = json.loads((layout.root / 'runtime' / 'migration.json').read_text())
    assert status['reason'] == reason and status['message']


def test_a_running_old_service_or_a_pending_upgrade_refuses_the_move(fake_home, monkeypatch):
    legacy, _ = build_old_home(fake_home)
    lease = claim_owner(legacy)
    try:
        layout, report = move(fake_home, legacy)
    finally:
        lease.close()
    assert_stayed(layout, report, legacy, 'legacy_in_use')
    monkeypatch.setattr(migrations, 'inspect', lambda home, *a, **k: {'state': 'upgrade_pending'})
    assert_stayed(layout, data_home.migrate(layout), legacy, 'legacy_upgrade_pending')


def test_live_worktrees_or_environments_and_low_space_refuse_the_move(fake_home):
    legacy, cid = build_old_home(fake_home)
    with closing(sqlite3.connect(legacy / 'pebble.sqlite')) as db:
        db.execute("INSERT INTO git_worktrees VALUES('w1',?,'/project','r','g',?, 'abc',NULL,NULL,'active','{}',1,1)",
                   (cid, str(legacy / 'git-worktrees' / 'w1')))
        db.commit()
    layout, report = move(fake_home, legacy)
    assert_stayed(layout, report, legacy, 'anchored_records')
    assert report['anchored'] == {'git_worktrees': 1, 'environments': 0}
    with closing(sqlite3.connect(legacy / 'pebble.sqlite')) as db:
        db.execute("UPDATE git_worktrees SET state='removed'")
        db.commit()
    report = data_home.migrate(layout, free_bytes=lambda path: 1024)
    assert_stayed(layout, report, legacy, 'not_enough_space')
    assert report['needed_bytes'] > report['free_bytes'] == 1024
    assert data_home.migrate(layout)['state'] == 'migrated'


def test_both_homes_with_data_keep_the_old_one(fake_home):
    legacy, _ = build_old_home(fake_home)
    layout = resolve(str(fake_home / '.pebble'), str(legacy), env={})
    data_home.ensure_layout(layout)
    App(layout.data, fake_home / 'resources', lambda *event: None).close()
    report = data_home.migrate(layout)
    assert report['state'] == 'conflict' and report['reason'] == 'both_homes_have_data' and report['data'] == str(legacy)
    assert not (legacy / data_home.MOVED).exists()


def test_a_copy_that_does_not_verify_is_never_used(fake_home, monkeypatch):
    legacy, _ = build_old_home(fake_home)
    real = data_home.database_summary
    calls = []

    def drifting(db):
        summary = real(db)
        calls.append(1)
        if len(calls) == 2:  # the copy's summary
            summary['tables']['chats']['rows'] += 1
        return summary
    monkeypatch.setattr(data_home, 'database_summary', drifting)
    before = tree(legacy, skip=SQLITE_SIDE)
    layout, report = move(fake_home, legacy)
    assert_stayed(layout, report, legacy, 'verify_failed')
    assert report['what'] == 'tables' and report['tables'] == ['chats']
    assert tree(legacy, skip=SQLITE_SIDE) == before


class Crash(BaseException):
    """A process killed mid-step: no Python cleanup beyond releasing its locks."""


@pytest.mark.parametrize('step', ['staging', 'copied', 'verified', 'switch'])
def test_an_interrupted_move_stays_on_the_old_home_and_the_next_launch_finishes_it(fake_home, step):
    legacy, cid = build_old_home(fake_home)
    layout = resolve(str(fake_home / '.pebble'), str(legacy), env={})
    data_home.ensure_layout(layout)

    def crash(at):
        if at == step:
            raise Crash(at)
    with pytest.raises(Crash):
        data_home.migrate(layout, checkpoint=crash)
    assert not layout.data.exists(), 'nothing switched: the old home is still the one to use'
    assert not (legacy / data_home.MOVED).exists()
    # The locks were released, so the old Pebble or the next launch can own the old home.
    claim_owner(legacy).close()
    report = data_home.migrate(layout)
    assert report['state'] == 'migrated' and report['swept_staging'] == 1
    assert not any(p.name.startswith(data_home.STAGING) for p in layout.root.iterdir())
    app = App(layout.data, fake_home / 'resources', lambda *event: None)
    try:
        assert [c['id'] for c in app.state()['chats']] == [cid]
    finally:
        app.close()


def test_an_interruption_after_the_switch_is_repaired_without_copying_again(fake_home):
    legacy, _ = build_old_home(fake_home)
    layout = resolve(str(fake_home / '.pebble'), str(legacy), env={})
    data_home.ensure_layout(layout)

    def crash(at):
        if at == 'switched':
            raise Crash(at)
    with pytest.raises(Crash):
        data_home.migrate(layout, checkpoint=crash)
    assert (layout.data / data_home.MIGRATED).exists() and not (legacy / data_home.POINTER).exists()
    report = data_home.migrate(layout)
    assert report['state'] == 'current' and report['pointer'] == 'written'
    assert (legacy / data_home.MOVED).exists()


def test_after_a_move_the_old_copy_is_never_moved_again_and_later_changes_are_reported(fake_home):
    legacy, _ = build_old_home(fake_home)
    layout, report = move(fake_home, legacy)
    assert report['state'] == 'migrated'
    # An older Pebble ran on the old folder again (a rollback): the new home keeps being used, and it is said.
    with closing(sqlite3.connect(legacy / 'pebble.sqlite')) as db:
        db.execute("INSERT INTO chats VALUES('e' || hex(randomblob(15)),'made after a rollback',NULL,2.0)")
        db.commit()
    again = data_home.migrate(layout)
    assert again['state'] == 'current' and again['legacy_changed'] is True
    # If ~/.pebble/data disappears, the older copy is not moved a second time: a fresh start, and a warning.
    shutil.rmtree(layout.data)
    report = data_home.migrate(layout)
    assert report['state'] == 'fresh' and report['warning'] == 'data_missing_after_move'
    assert report['data'] == str(layout.data) and not layout.data.exists()


def test_the_move_cost_for_a_larger_home(fake_home):
    """An old home with 1,000 messages and 16 MB of attachments: the verified copy's duration, measured."""
    import time
    legacy, cid = build_old_home(fake_home)
    with closing(sqlite3.connect(legacy / 'pebble.sqlite')) as db:
        db.executemany('INSERT INTO messages VALUES(?,?,?,?,?)',
                       [(f'm{index:06d}', cid, 'user', 'x' * 400, float(index)) for index in range(1000)])
        db.commit()
    for index in range(16):
        (legacy / 'attachments' / f'{index:032x}.bin').write_bytes(os.urandom(1 << 20))
    started = time.perf_counter()
    layout, report = move(fake_home, legacy)
    seconds = time.perf_counter() - started
    assert report['state'] == 'migrated' and report['database']['row_counts']['messages'] == 1002
    print(f"\nmove cost: {seconds:.3f} s for {report['files']} files, {report['bytes'] / (1 << 20):.1f} MB and "
          f"{report['database']['rows']} database rows")
    assert seconds < 60


def test_main_moves_the_old_home_and_serves_from_dot_pebble(fake_home, tmp_path):
    legacy, cid = build_old_home(fake_home)
    resources = tmp_path / 'resources'
    resources.mkdir()
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'TMPDIR', 'USER', 'LOGNAME')}
    env['HOME'] = str(fake_home)
    request = json.dumps({'id': 's', 'method': 'state', 'params': {}}) + '\n'
    out = subprocess.run([sys.executable, str(BACKEND / 'main.py'), '--home', str(fake_home / '.pebble'),
                          '--legacy-home', str(legacy), '--resources', str(resources)], input=request, text=True,
                         capture_output=True, env=env, cwd=str(tmp_path), timeout=120)
    lines = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    events = [line['data'] for line in lines if line.get('event') == 'storage_migration']
    assert events[0] == {'state': 'checking'} and events[-1]['state'] == 'migrated', (lines, out.stderr[-2000:])
    state = next(line['result'] for line in lines if line.get('id') == 's')
    assert state['data_dir'] == str(fake_home / '.pebble' / 'data') and [c['id'] for c in state['chats']] == [cid]
    assert (fake_home / '.pebble' / 'logs' / 'backend.log').exists()
    assert (legacy / data_home.MOVED).exists() and (legacy / 'pebble.sqlite').exists()
    argv = [sys.executable, str(BACKEND / 'main.py'), '--home', str(fake_home / '.pebble'), '--legacy-home', str(legacy),
            '--resources', str(resources)]
    again = subprocess.run(argv, input=request, text=True, capture_output=True, env=env, cwd=str(tmp_path), timeout=120)
    lines = [json.loads(line) for line in again.stdout.splitlines() if line.strip()]
    assert not [line for line in lines if line.get('event') == 'storage_migration'], 'a later launch announces nothing'
    assert next(line['result'] for line in lines if line.get('id') == 's')['data_dir'] == str(fake_home / '.pebble' / 'data')
    # An unusable ~/.pebble (here a file) keeps the service on the old home, and says why.
    other = tmp_path / 'other-home'
    other.mkdir()
    old, _ = build_old_home(other)
    (other / '.pebble').write_text('not a folder')
    env['HOME'] = str(other)
    out = subprocess.run([sys.executable, str(BACKEND / 'main.py'), '--home', str(other / '.pebble'), '--legacy-home',
                          str(old), '--resources', str(resources)], input=request, text=True, capture_output=True, env=env,
                         cwd=str(tmp_path), timeout=120)
    lines = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    events = [line['data'] for line in lines if line.get('event') == 'storage_migration']
    assert events == [{'state': 'refused', 'reason': 'home_unusable', 'message': data_home.REASONS['home_unusable'],
                       'warning': None}], (lines, out.stderr[-2000:])
    assert next(line['result'] for line in lines if line.get('id') == 's')['data_dir'] == str(old)
    env['HOME'] = str(fake_home)
    # A flat --home (tests, validators) is exactly as before: the database in it, nothing in HOME.
    flat = tmp_path / 'flat'
    out = subprocess.run([sys.executable, str(BACKEND / 'main.py'), '--home', str(flat), '--resources', str(resources)],
                         input=request, text=True, capture_output=True, env=env, cwd=str(tmp_path), timeout=120)
    lines = [json.loads(line) for line in out.stdout.splitlines() if line.strip()]
    assert not [line for line in lines if line.get('event') == 'storage_migration']
    state = next(line['result'] for line in lines if line.get('id') == 's')
    assert state['data_dir'] == str(flat) and (flat / 'pebble.sqlite').exists() and (flat / 'backend.log').exists()
    assert not (flat / 'settings.json').exists() and not (flat / 'data').exists() and not (flat / 'logs').exists()
    assert not data_home.marked(flat)
