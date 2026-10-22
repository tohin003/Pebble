"""f24-locked (P0 for 1.0): a status read under another connection's held write lock answers at once again (F8-2).

Phase 24's settle_run (V1-074, V1-132) releases the lane and announces 'idle' before it writes its buffered failure notes
and re-raises a refused terminal write, which controller.drive then retries (F8-1). Under validate_phase20_faults'
f8-lock-held outage each of those two writes held Store.lock through a whole busy timeout after 'idle', and the idle
loop's expiry filled the gaps, so the `state` read the validator sends right after 'idle' queued for 10-21 s on the
store's lock (2 of 3 live runs past its 20 s window). Now both writes run under Store.no_wait (busy timeout 0): a held
database refuses them at once, the notes are dropped as before after a refusal, and the dispatch loop settles the run
within a second once the database is free.

The App, runner, controller and store are the checked-in code; the model is a scripted fake; no network or shell.
"""

import sqlite3
import threading
import time

import pytest

from pebble.app import App
from pebble.store import Store

LIVE = frozenset({'queued', 'running', 'retry_wait'})
BUSY_MS = 2000  # the store's busy timeout here (5000 in the service): the outage settles sooner, the property is the same


class Outage:
    """Another connection's BEGIN EXCLUSIVE on a home's pebble.sqlite (validate_phase20_faults' Locker), held from a
    thread from take() until release(), never longer than `cap` seconds."""

    def __init__(self, path, cap=90.0):
        self.path, self.cap = path, cap
        self.taken, self.released, self.release_now = threading.Event(), threading.Event(), threading.Event()
        self.error = None

    def take(self):
        threading.Thread(target=self._hold, name='f24-outage', daemon=True).start()
        assert self.taken.wait(20), self.error

    def release(self):
        self.release_now.set()
        if self.taken.is_set():
            assert self.released.wait(10)

    def _hold(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            db.execute('BEGIN EXCLUSIVE')
            self.taken.set()
            self.release_now.wait(self.cap)
            db.execute('ROLLBACK')
        except Exception as exc:
            self.error = repr(exc)
        finally:
            db.close()
            self.released.set()


class Events:
    """The service's emitted events with their arrival times; `snapshot(name, data)` runs inside emit."""

    def __init__(self, snapshot=None):
        self.items, self.changed, self.snapshot = [], threading.Condition(), snapshot

    def emit(self, name, data):
        seen = self.snapshot(name, data) if self.snapshot else None
        with self.changed:
            self.items.append({'event': name, 'data': data if isinstance(data, dict) else {}, 'at': time.monotonic(),
                               'seen': seen})
            self.changed.notify_all()

    def of(self, name, run_id):
        with self.changed:
            return [e for e in self.items if e['event'] == name and e['data'].get('run_id') == run_id]

    def wait(self, name, run_id, count, seconds):
        deadline = time.monotonic() + seconds
        with self.changed:
            while len(self.of(name, run_id)) < count:
                left = deadline - time.monotonic()
                assert left > 0, (name, count, [(e['event'], e['data'].get('run_id')) for e in self.items])
                self.changed.wait(min(left, 0.2))
        return self.of(name, run_id)


def eventually(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def answering(text):
    """A scripted model that answers `text` at once (no network)."""
    def complete(messages, tools, cancel, token, effort=False):
        return {'role': 'assistant', 'content': text}
    return complete


def scripted(tmp_path, events, complete):
    app = App(tmp_path / 'data', tmp_path / 'resources', events.emit)
    app.model_save(name='f24 fixture', url='http://127.0.0.1:9', model='f24-fixture', context=32768)
    app.model.complete = complete
    return app


def test_no_wait_refuses_at_once_under_a_held_lock_and_restores_the_busy_timeout(tmp_path):
    store = Store(tmp_path / 'home')
    outage = Outage(store.path)
    try:
        assert int(store.db.execute('PRAGMA busy_timeout').fetchone()[0]) == 5000
        outage.take()
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            with store.no_wait():
                busy = int(store.db.execute('PRAGMA busy_timeout').fetchone()[0])
                held = []  # Store.lock is held inside: another thread cannot take it
                other = threading.Thread(target=lambda: held.append(store.lock.acquire(timeout=0.1)))
                other.start()
                other.join()
                with store.no_wait():  # nested (Store.lock is re-entrant), restored to 0 for the outer block
                    pass
                assert int(store.db.execute('PRAGMA busy_timeout').fetchone()[0]) == 0
                store.query("INSERT OR REPLACE INTO settings VALUES('f24','1')")
        assert time.monotonic() - started < 1.0  # refused at once, not after the 5 s busy timeout
        assert busy == 0 and held == [False]
        assert int(store.db.execute('PRAGMA busy_timeout').fetchone()[0]) == 5000  # restored for everyone else
        assert not store.db.in_transaction  # the refused statement left no transaction on the shared connection
        assert store.read("SELECT value FROM settings WHERE key='f24'") == []  # a status read still answers
    finally:
        outage.release()
    with store.no_wait():
        store.query("INSERT OR REPLACE INTO settings VALUES('f24','2')")  # a free database takes the write
    assert store.setting('f24') == 2 and store.lock.acquire(blocking=False)
    store.lock.release()
    store.close()


def test_after_idle_a_status_read_never_queues_behind_the_workers_refused_writes(tmp_path, monkeypatch):
    """validate_phase20_faults f8-lock-held, in process: the model request takes the outage and answers; the run's
    writes are refused; then one `state` read right after the worker announces 'idle'. The idle pass is pushed out of
    the window: its expiry's own busy wait predates Phase 24 and is not this test's subject."""
    monkeypatch.setattr(App, 'IDLE_SECONDS', 3600)
    events = Events()
    outage = None

    def complete(messages, tools, cancel, token, effort=False):
        outage.take()
        return {'role': 'assistant', 'content': 'Synthetic fault fixture: answer f24.'}
    app = scripted(tmp_path, events, complete)
    outage = Outage(app.home / 'pebble.sqlite')
    try:
        with app.store.lock:
            app.store.db.execute(f'PRAGMA busy_timeout={BUSY_MS}')
        chat = app.new_chat()
        rid = app.send(chat['id'], 'Reply with one short sentence about the fault fixture.')['run_id']
        try:
            first = events.wait('idle', rid, 1, 90)[0]
            started = time.monotonic()
            state = app.dispatch('state', {})
            read = time.monotonic() - started
            idles = events.wait('idle', rid, 2, 3 * BUSY_MS / 1000 + 5)
            assert not outage.released.is_set() and outage.error is None  # the read came while the database was locked
            # Phase 20-23's reports: ~5 s; this tree before the fix: 10-21 s (4+ s here). Never a busy timeout now.
            assert read < 1.0, f'state waited {read:.2f} s behind the worker after it announced idle'
            # The worker's last act (controller.drive's refused fallback, its error, its second 'idle') follows at once.
            assert idles[1]['at'] - first['at'] < 1.0, idles[1]['at'] - first['at']
            assert events.of('error', rid)  # drive reported the refused terminal write
            assert {r['id']: r['state'] for r in state['runs']}[rid] in LIVE  # the read answered with the stored row
            assert rid in app.controller.unsettled and app.runs.get(rid)['state'] in LIVE
            assert not app.active and not app.active_run
        finally:
            outage.release()
        # F8-1 unchanged: once the database is free the dispatch loop settles the run within a second or two.
        assert eventually(lambda: app.runs.get(rid)['state'] not in LIVE, 5), app.runs.get(rid)['state']
        run = app.runs.get(rid)
        messages = app.chat(chat['id'])['messages']
        assert run['state'] == 'failed' and rid not in app.controller.unsettled
        assert not [m for m in messages if m['role'] == 'assistant' and 'answer f24' in m['content']]  # never shown
        app.model.complete = answering('Later.')
        follow = app.send(chat['id'], 'A later task.')['run_id']  # the lane and the one-owner row are free again
        assert eventually(lambda: app.runs.get(follow)['state'] not in LIVE and not app.active, 20)
        assert app.runs.get(follow)['state'] == 'completed'
    finally:
        app.active = app.active_run = None
        app.close()


def test_with_the_database_free_the_fallback_and_the_notes_are_written_at_once(tmp_path, monkeypatch):
    """Store.no_wait only refuses another connection's lock: with the database free, controller.drive's fallback
    settles a refused run itself (no retry loop) and settle_run's buffered notes are written, as before."""
    holder = {}

    def at_idle(name, data):
        """At each 'idle' of the run, inside emit: its stored state and whether it waits for the retry loop."""
        app, rid = holder.get('app'), holder.get('rid')
        if name == 'idle' and app is not None and isinstance(data, dict) and data.get('run_id') == rid:
            return app.runs.get(rid)['state'], rid in app.controller.unsettled
        return None
    events = Events(at_idle)
    app = holder['app'] = scripted(tmp_path, events, answering('Done.'))
    try:
        original, refused = app.runs.transition, []

        def transition(rid, state, **kwargs):
            if state not in LIVE and not refused:  # settle_run's terminal write meets a passing lock; then it frees
                refused.append(state)
                raise sqlite3.OperationalError('database is locked')
            return original(rid, state, **kwargs)
        monkeypatch.setattr(app.runs, 'transition', transition)
        close_run, teardown = app.extensions.close_run, []

        def failing_close_run(run_id):
            if run_id == holder.get('rid'):
                teardown.append(run_id)
                raise RuntimeError('a plugin teardown failure (buffered as a note)')
            return close_run(run_id)
        monkeypatch.setattr(app.extensions, 'close_run', failing_close_run)
        chat = app.new_chat()
        holder['rid'] = rid = app.send(chat['id'], 'Say hello.')['run_id']
        idles = events.wait('idle', rid, 2, 30)
        assert len(refused) == 1 and refused[0] not in LIVE and teardown == [rid]
        assert idles[1]['seen'] == ('failed', False)  # settled by drive's own fallback, never left to the retry loop
        run = app.runs.get(rid)
        assert run['state'] == 'failed' and 'database is locked' in (run['error'] or '')
        notes = [e for e in app.runs.events(rid, 0, 200) if e['kind'] == 'plugin_teardown_failed']
        assert [n['payload'] for n in notes] == [{'type': 'RuntimeError'}]
        with app.store.lock:
            assert int(app.store.db.execute('PRAGMA busy_timeout').fetchone()[0]) == 5000
    finally:
        app.active = app.active_run = None
        app.close()
