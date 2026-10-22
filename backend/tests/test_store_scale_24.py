"""Phase 24 (t24-core): the store at scale and under failure.

V1-011 (state at 1,000 conversations, Stop beside a blocked status read), V1-057 (deleted text leaves the file),
V1-067 (database failures in words), V1-073 (Store.query rolls back), V1-075 (chat() presents legacy evidence with one
encoding per action) and V1-132 (the route table is built once). Synthetic homes only; no model, network or Keychain.
Tests marked "app.py patch" need the parent's app.py seam patches from .runtime/notes/t24-core-note.md.
"""

import errno
import json
import sqlite3
import statistics
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from pebble.app import App
from pebble.store import (DISK_FULL, LISTED_CHATS, STORE_MESSAGES, TASK_RECORDING, TRASH_KEY, VISIBLE_CHATS,
                          StoreRefusal, error_kind, friendly_error)

# The chat list of app.py state() in 0.6.1, kept here to prove the V1-011 rewrite returns exactly its rows.
OLD_LISTED_CHATS = ("SELECT * FROM chats WHERE (EXISTS(SELECT 1 FROM messages WHERE messages.chat_id=chats.id) OR "
                    "EXISTS(SELECT 1 FROM actions WHERE actions.chat_id=chats.id) OR EXISTS(SELECT 1 FROM jobs WHERE "
                    "jobs.chat_id=chats.id)) AND " + VISIBLE_CHATS + " ORDER BY updated DESC")
CHATS, TURNS = 1000, 20
STATE_SECONDS = 0.05
STOP_SECONDS = 1.0

# Every state call waits (at most 10 s) until the gate file exists, then answers as usual: a blocked status read.
GATED_STATE = '''
import time
from pebble.app import App
_gate, _state = Path({gate!r}), App.state
def _gated_state(self):
    until = time.monotonic() + 10
    while not _gate.exists() and time.monotonic() < until:
        time.sleep(0.01)
    return _state(self)
App.state = _gated_state
'''


def median_seconds(call, runs=5):
    took = []
    for _ in range(runs):
        started = time.perf_counter()
        call()
        took.append(time.perf_counter() - started)
    return statistics.median(took)


def build_scale(app):
    """1,000 conversations of 20 turns; 40 listed only by an action and 20 only by a (paused) job; 60 empty ones and
    15 in Recently deleted, which are not listed. Distinct update times. Returns how many are listed."""
    base = time.time() - 100_000
    chats, messages, actions, jobs, trash = [], [], [], [], {}

    def chat(label):
        cid = uuid.uuid4().hex
        chats.append((cid, label, None, base + len(chats)))
        return cid
    for n in range(CHATS):
        cid = chat(f'Conversation {n}')
        for turn in range(TURNS):
            messages.append((uuid.uuid4().hex, cid, 'user' if turn % 2 == 0 else 'assistant',
                             f'Turn {turn} of conversation {n}. ' + 'Synthetic text. ' * 10, base + n + turn / 100))
    for n in range(40):
        actions.append((uuid.uuid4().hex, chat(f'Action only {n}'), 'edit', 'applied',
                        json.dumps({'path': 'a.py', 'result': {'applied': 'a.py'}}), base))
    for n in range(20):
        jobs.append((uuid.uuid4().hex, chat(f'Job only {n}'), 'human', 'Fixture job', 'Fixture task', '{}', 0, 'paused',
                     1, '{}', 'fixture', base, None, None, None, None, None, base, base))
    for n in range(60):
        chat(f'Empty {n}')
    for n in range(15):
        cid = chat(f'Deleted {n}')
        messages.append((uuid.uuid4().hex, cid, 'user', 'Hidden.', base))
        trash[cid] = {'deleted': base, 'origin': 'app', 'title': f'Deleted {n}'}
    with app.store.lock, app.store.db as db:
        db.executemany('INSERT INTO chats VALUES(?,?,?,?)', chats)
        db.executemany('INSERT INTO messages VALUES(?,?,?,?,?)', messages)
        db.executemany('INSERT INTO actions VALUES(?,?,?,?,?,?)', actions)
        db.executemany('INSERT INTO jobs VALUES(' + ','.join('?' * 19) + ')', jobs)
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (TRASH_KEY, json.dumps(trash)))
    return CHATS + 40 + 20


@pytest.fixture
def app(tmp_path):
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield value
    value.active = value.active_run = None
    value.close()


@pytest.fixture
def scale(app):
    return SimpleNamespace(app=app, listed=build_scale(app))


@pytest.fixture
def scale_home(tmp_path):
    """The same home, closed again so that a service process can own it."""
    home = tmp_path / 'scale'
    owner = App(home, tmp_path / 'resources', lambda *args: None)
    try:
        listed = build_scale(owner)
    finally:
        owner.close()
    return SimpleNamespace(home=home, listed=listed)


# ---- V1-011: the chat list ----------------------------------------------------------------------------------

def test_the_listed_chats_query_returns_the_old_rows_in_under_50ms_at_1000_chats(scale):
    store = scale.app.store
    old = store.query(OLD_LISTED_CHATS)
    assert len(old) == scale.listed
    assert store.query(LISTED_CHATS) == old
    assert store.read(LISTED_CHATS) == old  # the read-only connection sees the same committed rows
    took = median_seconds(lambda: store.read(LISTED_CHATS))
    assert took < STATE_SECONDS, f'{took * 1000:.1f} ms'


def test_state_lists_the_same_chats_in_under_50ms_at_1000_chats(scale):
    """app.py patch (state)."""
    app = scale.app
    old = app.store.query(OLD_LISTED_CHATS)
    assert app.state()['chats'] == old
    took = median_seconds(app.state)
    assert took < STATE_SECONDS, f'state() took {took * 1000:.1f} ms at {CHATS} conversations'


def test_store_reads_never_wait_for_the_store_lock_and_see_only_committed_rows(app):
    store, cid = app.store, app.new_chat()['id']
    assert store.read('SELECT id FROM chats WHERE id=?', (cid,)) == [{'id': cid}]
    held, release = threading.Event(), threading.Event()

    def hold():
        with store.lock:
            held.set()
            release.wait(10)
    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        assert store.read(LISTED_CHATS) == []  # an empty conversation is not listed
        assert time.monotonic() - started < 1.0
    finally:
        release.set()
        holder.join(5)
    with store.lock:
        store.db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (uuid.uuid4().hex, cid, 'user', 'Hello', time.time()))
        assert store.read(LISTED_CHATS) == []  # uncommitted on the read-write connection
        store.db.commit()
    assert [row['id'] for row in store.read(LISTED_CHATS)] == [cid]


def test_read_connections_are_per_thread_read_only_and_closed_with_the_store(tmp_path):
    from pebble.store import Store
    store = Store(tmp_path / 'store')
    mine = store.read_connection()
    assert store.read_connection() is mine
    other = []
    worker = threading.Thread(target=lambda: other.append(store.read_connection()))
    worker.start()
    worker.join(5)
    assert other and other[0] is not mine
    with pytest.raises(sqlite3.OperationalError):
        mine.execute("INSERT INTO settings VALUES('written','1')")
    assert store.query("SELECT * FROM settings WHERE key='written'") == []
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        mine.execute('SELECT 1')
    refused = []

    def first_use_after_close():
        try:
            store.read_connection()
        except sqlite3.ProgrammingError as exc:
            refused.append(exc)
    late = threading.Thread(target=first_use_after_close)
    late.start()
    late.join(5)
    assert refused
    assert not (tmp_path / 'store' / 'pebble.sqlite-wal').exists()  # the read-write connection closed last


# ---- V1-011: Stop through the real backend/main.py -----------------------------------------------------------

def test_stop_is_answered_within_1s_while_a_state_call_is_blocked(tmp_path, pebble_service):
    gate = tmp_path / 'gate'
    service = pebble_service(tmp_path / 'data', GATED_STATE.format(gate=str(gate)))
    service.call('command_catalogue', timeout=60)  # the service is up
    state, _ = service.send('state')
    time.sleep(0.2)  # the status read has started and waits on the gate
    cancel, sent = service.send('cancel')
    answered = service.reply(cancel, timeout=3)
    assert answered is not None, 'Stop waited behind the blocked state call'
    assert answered[0].get('result') is True and answered[1] - sent < STOP_SECONDS
    assert service.reply(state, timeout=0) is None  # still blocked: Stop did not wait for it
    gate.write_text('open')
    listed = service.reply(state, timeout=15)
    assert listed is not None and listed[0]['result']['chats'] == []
    assert service.close() == 0


def test_stop_after_the_windows_status_reads_is_answered_within_1s_at_1000_chats(scale_home, pebble_service):
    """After a reply both windows read state; then the person presses Stop. In 0.6.1 Stop waited on the reader thread
    behind every one of those reads (about 0.8 s each here). With the app.py state patch, each read takes milliseconds
    and never holds the store lock that Stop needs."""
    service = pebble_service(scale_home.home)
    service.call('command_catalogue', timeout=60)
    states = [service.send('state')[0] for _ in range(3)]  # the main window, the pet, and one more poll
    cancel, sent = service.send('cancel')
    answered = service.reply(cancel, timeout=30)
    assert answered is not None and answered[0].get('result') is True
    assert answered[1] - sent < STOP_SECONDS, f'Stop took {answered[1] - sent:.2f} s'
    for state in states:
        listed = service.reply(state, timeout=30)
        assert listed is not None and len(listed[0]['result']['chats']) == scale_home.listed
    assert service.close() == 0


# ---- V1-073: Store.query rolls back what it opened ------------------------------------------------------------

def test_query_rolls_back_after_an_integrity_error(app):
    cid = app.new_chat()['id']
    with pytest.raises(sqlite3.IntegrityError):
        app.store.query('INSERT INTO chats VALUES(?,?,?,?)', (cid, 'Duplicate', None, time.time()))
    assert not app.store.db.in_transaction
    assert app.artifacts.list(cid) == []  # refused ('outside transaction boundary') while the transaction stayed open


def test_query_rolls_back_after_database_is_locked(app):
    cid = app.new_chat()['id']
    other = sqlite3.connect(app.home / 'pebble.sqlite', timeout=0)
    try:
        other.execute('BEGIN IMMEDIATE')  # another process holds the write lock
        with app.store.lock:
            previous = app.store.db.execute('PRAGMA busy_timeout').fetchone()[0]
            app.store.db.execute('PRAGMA busy_timeout=50')
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                app.store.query("INSERT INTO settings VALUES('blocked','true')")
            assert not app.store.db.in_transaction
        finally:
            with app.store.lock:
                app.store.db.execute(f'PRAGMA busy_timeout={previous}')
        other.rollback()
    finally:
        other.close()
    assert app.artifacts.list(cid) == []
    assert app.store.setting('blocked') is None


def test_query_leaves_a_transaction_the_caller_opened_to_the_caller(app):
    store = app.store
    with store.lock:
        store.db.execute("INSERT INTO settings VALUES('outer','1')")
        with pytest.raises(sqlite3.IntegrityError):
            store.query("INSERT INTO settings VALUES('outer','2')")
        assert store.db.in_transaction  # unchanged: the caller's open transaction is not rolled back under it
        store.db.rollback()
    assert store.setting('outer') is None


# ---- V1-057: deleted text leaves pebble.sqlite -------------------------------------------------------------

def canary_hits(home, canary):
    found = 0
    for name in ('pebble.sqlite', 'pebble.sqlite-wal'):
        path = home / name
        if path.exists():
            found += path.read_bytes().count(canary.encode())
    return found


def test_a_conversation_deleted_for_good_leaves_no_byte_of_it_in_the_database_file(app):
    assert app.store.db.execute('PRAGMA secure_delete').fetchone()[0] == 1
    canary = 'pebble-canary-' + uuid.uuid4().hex
    kept, cid = app.new_chat()['id'], app.new_chat()['id']
    app.store.message(kept, 'user', 'An unrelated conversation that stays.')
    for turn in range(12):
        app.store.message(cid, 'user', f'{canary} question {turn}')
        # Long replies use overflow pages, which a deletion frees whole.
        app.store.message(cid, 'assistant', f'{canary} answer {turn}. ' + 'A long reply. ' * 700 + canary)
        app.store.message(kept, 'assistant', f'Interleaved turn {turn}.')
    assert app.store.checkpoint('TRUNCATE')
    assert canary_hits(app.home, canary) >= 36
    app.delete_chat(cid)
    assert app.store.checkpoint('TRUNCATE')
    assert canary_hits(app.home, canary) == 0
    assert len(app.store.query('SELECT id FROM messages WHERE chat_id=?', (kept,))) == 13


def test_checkpoint_refuses_unknown_modes_and_never_ends_an_open_transaction(app):
    with pytest.raises(ValueError):
        app.store.checkpoint('VACUUM')
    with app.store.lock:
        app.store.db.execute("INSERT INTO settings VALUES('open','1')")
        assert app.store.checkpoint() is False
        assert app.store.db.in_transaction
        app.store.db.rollback()
    assert app.store.checkpoint('PASSIVE') is True


# ---- V1-067: database failures in words --------------------------------------------------------------------

def test_a_full_disk_is_said_in_words(app):
    cid = app.new_chat()['id']
    with app.store.lock:
        pages = app.store.db.execute('PRAGMA page_count').fetchone()[0]
        app.store.db.execute(f'PRAGMA max_page_count={pages}')
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            app.store.message(cid, 'user', 'x' * 200_000)
        assert not app.store.db.in_transaction
    finally:
        with app.store.lock:
            app.store.db.execute('PRAGMA max_page_count=1073741823')
    refusal = friendly_error(caught.value)
    assert isinstance(refusal, StoreRefusal) and refusal.code == DISK_FULL
    assert str(refusal) == STORE_MESSAGES[DISK_FULL] and 'database' not in str(refusal)
    assert error_kind(caught.value) == 'OperationalError/SQLITE_FULL'


def test_friendly_error_words_only_the_known_failures():
    assert friendly_error(OSError(errno.ENOSPC, 'No space left on device')).code == DISK_FULL
    assert friendly_error(OSError(errno.EDQUOT, 'Disc quota exceeded')).code == DISK_FULL
    one_owner = sqlite3.IntegrityError("UNIQUE constraint failed: index 'runs_one_owner'")
    assert friendly_error(one_owner).code == TASK_RECORDING
    wrapped = ValueError('Could not record the run.')
    wrapped.__cause__ = one_owner
    assert friendly_error(wrapped).code == TASK_RECORDING
    for other in (sqlite3.IntegrityError('UNIQUE constraint failed: chats.id'), sqlite3.OperationalError('database is locked'),
                  ValueError('Stop the current task first.'), OSError(errno.EACCES, 'Permission denied')):
        assert friendly_error(other) is None
    refusal = StoreRefusal(DISK_FULL)
    assert friendly_error(refusal) is refusal and isinstance(refusal, ValueError)
    assert error_kind(ValueError('secret text')) == 'ValueError'


def test_send_beside_a_stranded_live_run_says_the_previous_task_is_still_being_recorded(app):
    """app.py patch (send)."""
    chat = app.new_chat()
    app.runs.create(chat['id'], 'A task whose terminal write the database refused.', app.runtime_config(chat, False))
    with pytest.raises(StoreRefusal) as caught:
        app.send(chat['id'], 'A new task.')
    assert caught.value.code == TASK_RECORDING and 'constraint' not in str(caught.value)
    assert app.active is None and app.active_run is None


# ---- V1-075 and V1-132: the request path ---------------------------------------------------------------------

def test_chat_presents_legacy_evidence_with_one_encoding_per_action(app):
    """app.py patch (chat). Legacy '**Action evidence** · Approved' messages read as their action's summary; the
    first action in created order wins, as before."""
    cid, count = app.new_chat()['id'], 2000
    rows = []
    for n in range(count):
        result = {'applied': f'src/file_{n}.py'}
        app.store.action(cid, 'edit', {'path': f'src/file_{n}.py', 'result': result})
        rows.append((uuid.uuid4().hex, cid, 'assistant',
                     '**Action evidence** · Approved edit\n\n' + json.dumps(result)[:5000], 1_000 + n))
    shared = {'applied': 'src/file_0.py'}  # a later action with the same result as the first
    app.store.action(cid, 'edit', {'path': 'src/file_0.py', 'result': shared, 'rewind': {'step': 0}})
    with app.store.lock, app.store.db as db:
        db.executemany('INSERT INTO messages VALUES(?,?,?,?,?)', rows)
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (uuid.uuid4().hex, cid, 'assistant', 'Ordinary prose.', 5_000))
    started = time.perf_counter()
    chat = app.chat(cid)
    took = time.perf_counter() - started
    texts = [m['content'] for m in chat['messages']]
    assert texts[:count] == [App.action_summary('edit', {}, {'applied': f'src/file_{n}.py'}) for n in range(count)]
    assert texts[-1] == 'Ordinary prose.'
    assert took < 0.5, f'chat() took {took:.2f} s with {count} evidence messages and {count + 1} actions'


def test_the_route_table_is_built_once(app, monkeypatch):
    """app.py patch (dispatch)."""
    built, original = [], app.git_api.methods
    monkeypatch.setattr(app.git_api, 'methods', lambda: built.append(1) or original())
    for _ in range(3):
        assert app.dispatch('command_catalogue', {})
    assert built == []
    with pytest.raises(ValueError, match='Unknown request'):
        app.dispatch('no_such_route', {})
