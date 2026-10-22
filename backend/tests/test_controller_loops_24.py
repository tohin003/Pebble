"""Phase 24 (V1-012, V1-013): the continuation dispatcher survives a database error, and F8-1 settles promptly.

0.6.1's `pebble-continuations` loop had no try/except: one OperationalError ("database is locked") from claim() ended
the thread, and after that nothing settled refused terminal writes and no queued continuation ran until restart (a
stranded "running" row blocks every new task). And its F8-1 settle probe used a zero-wait BEGIN IMMEDIATE, so one of
Pebble's own short writes in flight read as an outage, and the retry came five seconds later, against the grader's
ten-second window. Now the loop records a content-free last_error and keeps serving, the probe waits up to half a
second, and the loop wakes every second while a run is unsettled. validate_phase20_faults' window is unchanged.

The App, controller and run journal are the checked-in code; no model, network or shell.
"""

import sqlite3
import threading
import time

import pytest

from pebble.app import App
from pebble.controller import SETTLE_PROBE_SECONDS


@pytest.fixture
def app(tmp_path):
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    value.home_path = tmp_path / 'data'
    yield value
    value.close()


def stranded(app):
    """A run whose terminal write the database refused (F8-1): the row says running, no worker owns it."""
    chat = app.new_chat(None)
    config = app.runtime_config(chat, False, {'max_steps': 4, 'max_seconds': 60})
    rid = app.runs.create(chat['id'], 'Fixture goal.', config)
    app.runs.transition(rid, 'running')
    app.controller.unsettled[rid] = 'The database refused the terminal write.'
    return rid


def eventually(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_the_dispatch_loop_survives_an_injected_operational_error(app, monkeypatch):
    controller = app.controller
    original, raised = controller.interactions.claim, threading.Event()

    def claim():
        if not raised.is_set():
            raised.set()
            raise sqlite3.OperationalError('database is locked')
        return original()
    monkeypatch.setattr(controller.interactions, 'claim', claim)
    controller.wake()
    assert raised.wait(10)
    time.sleep(0.3)
    assert controller.thread.is_alive(), 'one database error must not end the continuation thread'
    assert controller.last_error['type'] == 'OperationalError' and set(controller.last_error) == {'type', 'at'}
    rid = stranded(app)
    controller.wake()
    assert eventually(lambda: app.runs.get(rid)['state'] == 'failed', 5), app.runs.get(rid)['state']
    assert rid not in controller.unsettled


def test_settle_succeeds_with_one_of_pebbles_own_short_writes_in_flight(app):
    assert app.controller.close()  # this test drives settle_unsettled itself
    rid = stranded(app)
    began, done = threading.Event(), threading.Event()

    def writer():
        with app.store.lock:
            app.store.db.execute('BEGIN IMMEDIATE')
            app.store.db.execute("UPDATE settings SET value=value WHERE key='never-there'")
            began.set()
            time.sleep(0.3)
            app.store.db.commit()
        done.set()
    thread = threading.Thread(target=writer)
    thread.start()
    assert began.wait(5)
    assert 0.3 < SETTLE_PROBE_SECONDS
    app.controller.settle_unsettled()
    thread.join(5)
    assert done.is_set()
    assert app.runs.get(rid)['state'] == 'failed', 'a 300 ms write is not an outage'
    assert rid not in app.controller.unsettled


def test_an_unsettled_run_is_retried_every_second_after_the_outage_ends(app):
    rid = stranded(app)
    outage = sqlite3.connect(app.home_path / 'pebble.sqlite', timeout=5)
    outage.execute('BEGIN IMMEDIATE')  # another writer holds the database (the fault campaign's outage)
    try:
        app.controller.wake()  # as drive() does when it records the refused write
        time.sleep(1.5)
        assert app.runs.get(rid)['state'] == 'running'
    finally:
        outage.rollback()
        outage.close()
    ended = time.monotonic()
    assert eventually(lambda: app.runs.get(rid)['state'] == 'failed', 2.5), 'settled within a second or two'
    assert time.monotonic() - ended < 2.5
