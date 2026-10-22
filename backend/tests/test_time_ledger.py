"""Cross-crash active-time ledger: advance charging, crash/reboot recovery, sleep, reservations.

Every clock is a monkeypatched fake owned by the test, so nothing sleeps for
ledger time. A real Store and RunJournal supply the run and its pinned budget.
Nothing here spawns a process or sends a signal.
"""

import json
import math
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from pebble import time_ledger as tl
from pebble.policy import PolicyError
from pebble.runtime import RunJournal
from pebble.store import Store
from pebble.time_ledger import BOOT, LEDGER_DDL, TimeLedger, boot_session


class Clock:
    """Fake monotonic and wall clocks; sleep advances wall time only, as on macOS."""

    def __init__(self, mono=100.0, wall=1_700_000_000.0):
        self.lock = threading.Lock()
        self.m, self.w = mono, wall

    def mono(self):
        with self.lock:
            return self.m

    def wall(self):
        with self.lock:
            return self.w

    def advance(self, seconds, sleep=0.0):
        with self.lock:
            self.m += seconds
            self.w += seconds + sleep

    def reboot(self, mono=1.0):
        with self.lock:
            self.m = mono  # monotonic restarts at boot; wall time continues


@pytest.fixture
def env(tmp_path):
    home = tmp_path / 'data'
    store = Store(home)
    runs = RunJournal(store, home)
    cid = store.new_chat(None)
    rid = runs.create(cid, 'Ledger fixture', {'limits': {'max_steps': 8, 'max_seconds': 100.0}})
    clock = Clock()

    def ledger(boot='boot-a', **kw):
        return TimeLedger(store, boot, clock.mono, clock.wall, **kw)

    yield SimpleNamespace(store=store, runs=runs, cid=cid, rid=rid, clock=clock, ledger=ledger)
    runs.close()
    store.close()


def rows(env, run_id=None):
    return env.store.query('SELECT * FROM run_time_ledger WHERE run_id=? ORDER BY opened_mono,rowid', (run_id or env.rid,))


def states(env):
    return [(row['state'], round(row['charged'], 6)) for row in rows(env)]


def coverage_is_disjoint(env):
    spans = sorted((r['opened_mono'], r['opened_mono'] + r['charged']) for r in rows(env) if r['state'] in tl.COVERING)
    return all(later[0] >= earlier[1] - 1e-9 for earlier, later in zip(spans, spans[1:]))


def test_ddl_runs_in_fresh_sqlite_is_idempotent_and_enforces_one_open_slice():
    db = sqlite3.connect(':memory:')
    db.executescript(LEDGER_DDL)
    db.executescript(LEDGER_DDL)
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
    assert {'run_time_ledger', 'run_time_reservations', 'run_time_ledger_open', 'run_time_ledger_run',
            'run_time_reservations_run'} <= names
    insert = 'INSERT INTO run_time_ledger VALUES(?,?,?,?,?,?,?,?)'
    db.execute(insert, ('r' * 32, 's1', 'b', 1.0, 1.0, 10.0, 'open', None))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ('r' * 32, 's2', 'b', 1.0, 1.0, 10.0, 'open', None))
    db.execute(insert, ('q' * 32, 's3', 'b', 1.0, 1.0, 10.0, 'open', None))  # another run may be open
    for bad in (('r' * 32, 's4', 'b', 1.0, 1.0, -1.0, 'used', 2.0), ('r' * 32, 's5', 'b', 1.0, 1.0, 1.0, 'forged', 2.0)):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, bad)
    hold = 'INSERT INTO run_time_reservations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)'
    db.execute(hold, ('h1', 'r' * 32, 'process', 'a1', 5.0, 'b', 1.0, 1.0, 'open', None, 0.0, None))
    for bad in (('h2', 'r' * 32, 'process', 'a1', 5.0, 'b', 1.0, 1.0, 'open', None, 0.0, None),
                ('h3', 'r' * 32, 'shell', 'a3', 5.0, 'b', 1.0, 1.0, 'open', None, 0.0, None),
                ('h4', 'r' * 32, 'child', 'a4', 0.0, 'b', 1.0, 1.0, 'open', None, 0.0, None)):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(hold, bad)
    db.close()


def test_begin_charges_a_full_slice_in_advance_in_one_transaction(env, monkeypatch):
    ledger = env.ledger()
    opened = ledger.begin(env.rid)
    assert opened['charged'] == 10.0 and opened['consumed'] == 10.0 == ledger.consumed(env.rid)
    assert states(env) == [('open', 10.0)] and rows(env)[0]['boot_uuid'] == 'boot-a'
    with pytest.raises(PolicyError, match='already has an open'):
        ledger.begin(env.rid)
    assert ledger.consumed(env.rid) == 10.0
    # A second instance on the same boot finds a slice its dead predecessor left: it stays whole.
    other = env.ledger()
    other.begin(env.rid)
    assert states(env) == [('crash', 10.0), ('open', 10.0)] and other.consumed(env.rid) == 20.0
    other.end(env.rid)
    # The charge and the open slice commit together or not at all.
    monkeypatch.setattr(TimeLedger, '_consumed', staticmethod(lambda db, run_id: (_ for _ in ()).throw(RuntimeError())))
    with pytest.raises(RuntimeError):
        other.begin(env.rid)
    monkeypatch.undo()
    assert [r['state'] for r in rows(env)] == ['crash', 'ended'] and env.rid not in other._active
    with env.store.lock:
        env.store.db.execute('BEGIN')
        try:
            with pytest.raises(PolicyError, match='could not be verified'):
                other.begin(env.rid)
        finally:
            env.store.db.rollback()


def test_heartbeat_renews_before_exhaustion_and_charges_a_late_overrun(env):
    ledger, clock = env.ledger(), env.clock
    with pytest.raises(PolicyError, match='no open active-time interval'):
        ledger.heartbeat(env.rid)
    ledger.begin(env.rid)
    # The root's own remaining time counts the prepaid advance it has not spent yet.
    assert ledger.remaining(env.rid) == 100.0 and ledger.usage(env.rid)['advance_unused'] == 10.0
    clock.advance(3)
    assert ledger.heartbeat(env.rid)['charged'] == 0.0 and ledger.consumed(env.rid) == 10.0
    assert ledger.remaining(env.rid) == pytest.approx(97.0)
    clock.advance(3)  # 4 s of advance left, within the 5 s lead
    renewed = ledger.heartbeat(env.rid)
    assert renewed['charged'] == pytest.approx(6.0) and renewed['consumed'] == pytest.approx(16.0)
    assert states(env) == [('used', 6.0), ('open', 10.0)] and ledger.remaining(env.rid) == pytest.approx(94.0)
    clock.advance(20)  # the ticker stalled past the advance: the overrun is charged when observed
    assert ledger.remaining(env.rid) == pytest.approx(84.0)  # unobserved overrun: it lags until the next heartbeat
    late = ledger.heartbeat(env.rid)
    assert late['charged'] == pytest.approx(20.0) and late['consumed'] == pytest.approx(36.0)
    assert states(env) == [('used', 6.0), ('used', 10.0), ('late', 10.0), ('open', 10.0)]
    assert ledger.remaining(env.rid) == pytest.approx(74.0)  # exactly 100 - 26 observed seconds
    clock.advance(2)
    assert ledger.end(env.rid) == pytest.approx(8.0)
    assert ledger.consumed(env.rid) == pytest.approx(28.0)  # exactly the 28 observed seconds
    assert coverage_is_disjoint(env)
    with pytest.raises(PolicyError, match='no open active-time interval'):
        ledger.heartbeat(env.rid)
    # Only the instance that opened the interval renews it.
    ledger.begin(env.rid)
    with pytest.raises(PolicyError, match='in this service'):
        env.ledger().heartbeat(env.rid)


def test_end_refunds_only_when_certain_and_charges_an_observed_overrun(env):
    ledger, clock = env.ledger(), env.clock
    assert ledger.end(env.rid) == 0.0  # nothing open
    ledger.begin(env.rid)
    clock.advance(3)
    assert ledger.end(env.rid, certain=False) == 0.0 and ledger.consumed(env.rid) == 10.0
    ledger.begin(env.rid)
    clock.advance(4)
    assert ledger.end(env.rid, certain=True) == pytest.approx(6.0) and ledger.consumed(env.rid) == pytest.approx(14.0)
    ledger.begin(env.rid)
    clock.advance(14)  # no heartbeat at all
    assert ledger.end(env.rid) == 0.0 and ledger.consumed(env.rid) == pytest.approx(28.0)
    # Another boot's slice, or a clock that went backwards, is never refunded.
    ledger.begin(env.rid)
    clock.reboot()
    rebooted = env.ledger('boot-b')
    assert rebooted.end(env.rid) == 0.0 and ledger.consumed(env.rid) == pytest.approx(38.0)
    with pytest.raises(PolicyError, match='true or false'):
        ledger.end(env.rid, certain='yes')


def test_crash_loop_never_reduces_consumed_or_undercharges_observed_time(env):
    clock, observed, last, boot = env.clock, 0.0, 0.0, 'boot-0'
    steps = (0.5, 3.0, 7.0, 9.9, 12.0, 26.0, 41.0)
    for iteration in range(20):
        if iteration % 6 == 5:
            boot = f'boot-{iteration}'
            clock.reboot(1.0 + iteration)
        ledger = env.ledger(boot)
        ledger.recover()
        value = ledger.consumed(env.rid)
        assert value >= last - 1e-9 and value >= observed - 1e-9
        last = value
        ledger.begin(env.rid)
        left = steps[iteration % len(steps)]
        while left > 0:
            step = min(4.0, left)  # the ticker's cadence stays inside the 5 s lead
            clock.advance(step)
            left -= step
            observed += step
            value = ledger.consumed(env.rid)
            assert value >= observed - 1e-9 and value >= last - 1e-9
            last = value
            value = ledger.heartbeat(env.rid)['consumed']
            assert value >= observed - 1e-9 and value >= last - 1e-9
            last = value
        del ledger  # crash: no end, no refund
    final = env.ledger(boot)
    counts = final.recover()
    consumed = final.consumed(env.rid)
    assert counts['crash'] + counts['reboot'] == 1
    assert observed - 1e-9 <= consumed <= observed + 20 * 10.0  # at most one slice over per crash
    assert consumed >= last - 1e-9
    recovered = [r['state'] for r in rows(env) if r['state'] in ('crash', 'reboot')]
    assert len(recovered) == 20 and recovered.count('reboot') == 3
    assert not [r for r in rows(env) if r['state'] == 'open']


def test_recover_charges_open_slices_in_full_and_marks_reboots(env):
    first = env.ledger('boot-a')
    first.begin(env.rid)
    env.clock.advance(2)
    same = env.ledger('boot-a')
    assert same.recover() == {'crash': 1, 'reboot': 0, 'reservations_uncertain': 0}
    assert states(env) == [('crash', 10.0)] and same.consumed(env.rid) == 10.0
    same.begin(env.rid)
    assert same.recover() == {'crash': 0, 'reboot': 0, 'reservations_uncertain': 0}  # its own live interval
    env.clock.reboot()
    rebooted = env.ledger('boot-b')
    assert rebooted.recover()['reboot'] == 1
    assert states(env) == [('crash', 10.0), ('reboot', 10.0)]
    assert rebooted.consumed(env.rid) == 20.0
    assert all(r['closed'] is not None for r in rows(env))


def test_sleep_gap_is_recorded_and_never_charged(env):
    ledger, clock = env.ledger(), env.clock
    assert ledger.observe_sleep(env.rid) == 0.0  # nothing open
    ledger.begin(env.rid)
    clock.advance(3, sleep=600)
    assert ledger.observe_sleep(env.rid) == pytest.approx(600.0)
    assert ledger.observe_sleep(env.rid) == 0.0  # recorded once
    assert ledger.heartbeat(env.rid)['charged'] == 0.0  # monotonic advanced 3 s: the slice still covers it
    assert ledger.consumed(env.rid) == 10.0
    clock.advance(1, sleep=0.5)  # NTP-sized drift is noise; it stays in the next measured gap
    assert ledger.observe_sleep(env.rid) == 0.0
    clock.advance(0, sleep=120)
    assert ledger.heartbeat(env.rid)['charged'] == 0.0  # the heartbeat observes the 120.5 s gap first
    with clock.lock:
        clock.w -= 3600  # a wall clock set back is not sleep
    assert ledger.observe_sleep(env.rid) == 0.0
    assert ledger.end(env.rid) == pytest.approx(6.0)
    assert ledger.consumed(env.rid) == pytest.approx(4.0)  # active time only
    slept = [r for r in rows(env) if r['state'] == 'sleep_observed']
    assert [r['closed'] - r['opened'] for r in slept] == pytest.approx([600.0, 120.5])
    assert all(r['charged'] == 0 for r in slept)
    assert ledger.usage(env.rid)['sleep_observed_seconds'] == pytest.approx(720.5)


def test_reservations_never_exceed_the_unreserved_pool(env, monkeypatch):
    ledger = env.ledger()
    ledger.begin(env.rid)  # budget 100, consumed 10
    first = ledger.reserve(env.rid, 60, 'process', 'action-1')
    with pytest.raises(PolicyError, match='30.0 unreserved seconds'):
        ledger.reserve(env.rid, 31, 'process', 'action-2')
    second = ledger.reserve(env.rid, 30, 'child', 'agent-1')
    usage = ledger.usage(env.rid)
    assert (usage['reserved'], usage['unreserved'], usage['consumed']) == (90.0, 0.0, 10.0)
    assert usage['authorization'] == 'none' and usage['reservations'] == {'open': 2}
    with pytest.raises(PolicyError, match='unreserved'):
        ledger.reserve(env.rid, 50, 'process', 'action-3', narrow=True)
    assert ledger.reserve(env.rid, 60, 'process', 'action-1') == first  # idempotent while open
    with pytest.raises(PolicyError, match='different time reservation'):
        ledger.reserve(env.rid, 59, 'process', 'action-1')
    ledger.settle_reservation(second, 0, True)
    with pytest.raises(PolicyError, match='never reopened'):
        ledger.reserve(env.rid, 30, 'child', 'agent-1')
    narrowed = ledger.reserve(env.rid, 50, 'process', 'action-3', narrow=True)
    assert ledger.reservation(narrowed)['seconds'] == 30.0 and ledger.usage(env.rid)['unreserved'] == 0.0
    for args in ((env.rid, 5, 'shell', 'x'), (env.rid, 5, 'process', ''), (env.rid, 5, 'process', 'a\nb'),
                 (env.rid, float('nan'), 'process', 'x'), (env.rid, -1, 'process', 'x'), (env.rid, 0, 'process', 'x'),
                 (env.rid, 1e9, 'process', 'x'), (env.rid, True, 'process', 'x'), ('not-a-run', 5, 'process', 'x'),
                 ('f' * 32, 5, 'process', 'x')):
        with pytest.raises(PolicyError):
            ledger.reserve(*args)
    monkeypatch.setattr(tl, 'MAX_OPEN', 2)
    ledger.settle_reservation(narrowed, 1, True)
    ledger.settle_reservation(first, 0, True)
    ledger.reserve(env.rid, 1, 'process', 'p-a')
    ledger.reserve(env.rid, 1, 'process', 'p-b')
    with pytest.raises(PolicyError, match='at most 2 open'):
        ledger.reserve(env.rid, 1, 'process', 'p-c')


def test_settlement_refunds_only_when_certain_and_credits_proven_overlap(env):
    ledger, clock = env.ledger(), env.clock
    ledger.begin(env.rid)  # covers [100, 110]
    clock.advance(1)
    overlapped = ledger.reserve(env.rid, 60, 'process', 'p1')
    strict = ledger.reserve(env.rid, 20, 'process', 'p1-strict')
    clock.advance(5)
    ledger.heartbeat(env.rid)  # used [100, 106], open [106, 116]
    clock.advance(3)
    settled = ledger.settle_reservation(overlapped, 7, True)
    # The job's 7 s lie inside root time already charged: nothing is charged twice.
    assert settled == {'reservation_id': overlapped, 'state': 'settled', 'charged': pytest.approx(0.0),
                       'refund': pytest.approx(60.0), 'credited': pytest.approx(7.0), 'duplicate': False}
    assert ledger.consumed(env.rid) == pytest.approx(16.0)
    settled = ledger.settle_reservation(strict, 7, True, credit=False)  # opting out refunds only unused time
    assert (settled['charged'], settled['refund'], settled['credited']) == (7.0, 13.0, 0.0)
    assert ledger.consumed(env.rid) == pytest.approx(23.0)
    solo = ledger.reserve(env.rid, 40, 'process', 'p2')
    clock.advance(1)
    ledger.end(env.rid)  # ended [106, 110]; slices total 10
    clock.advance(30)
    settled = ledger.settle_reservation(solo, 25, True)
    assert settled['charged'] == pytest.approx(25.0) and settled['refund'] == pytest.approx(15.0)
    assert settled['credited'] == 0.0 and ledger.consumed(env.rid) == pytest.approx(42.0)
    unknown = ledger.reserve(env.rid, 20, 'process', 'p3')
    settled = ledger.settle_reservation(unknown, 3, False)
    assert (settled['state'], settled['charged'], settled['refund']) == ('uncertain', 20.0, 0.0)
    assert ledger.consumed(env.rid) == pytest.approx(62.0)
    again = ledger.settle_reservation(unknown, 0, True)
    assert again['duplicate'] is True and again['charged'] == 20.0 and ledger.consumed(env.rid) == pytest.approx(62.0)
    early = ledger.reserve(env.rid, 10, 'child', 'c1')
    # Used time longer than the window cannot be credited.
    assert ledger.settle_reservation(early, 5, True)['charged'] == 5.0
    unknown_used = ledger.reserve(env.rid, 5, 'child', 'c2')
    assert ledger.settle_reservation(unknown_used, None, False)['charged'] == 5.0
    for args in (('f' * 32, 1, True), ('bad', 1, True), (unknown_used, None, True), (unknown_used, 1, 'yes'),
                 (unknown_used, -1, True), (unknown_used, float('inf'), False)):
        with pytest.raises(PolicyError):
            ledger.settle_reservation(*args)
    with pytest.raises(PolicyError):
        ledger.settle_reservation(unknown_used, 1, True, credit='no')


def test_open_holds_charge_provisionally_and_reboot_settles_them_uncertain(env):
    ledger, clock = env.ledger(), env.clock
    ledger.begin(env.rid)
    hold = ledger.reserve(env.rid, 50, 'process', 'p')
    clock.advance(2)
    ledger.end(env.rid)  # consumed 2, and the hold's window so far was all root time
    assert ledger.remaining(env.rid) == pytest.approx(98.0)
    clock.advance(30)  # the job may have run alone since the root stopped
    usage = ledger.usage(env.rid)
    assert usage['provisional'] == pytest.approx(30.0) and usage['remaining'] == pytest.approx(68.0)
    assert usage['reserved'] == 50.0 and usage['unreserved'] == pytest.approx(48.0)
    clock.advance(100)
    assert ledger.remaining(env.rid) == pytest.approx(48.0)  # never more than the hold
    same_boot = env.ledger('boot-a')
    assert same_boot.recover()['reservations_uncertain'] == 0  # a same-boot job may still be reattached
    assert same_boot.reservation(hold)['state'] == 'open'
    clock.reboot()
    rebooted = env.ledger('boot-b')
    assert rebooted.recover()['reservations_uncertain'] == 1
    record = rebooted.reservation(hold)
    assert (record['state'], record['charged'], record['used']) == ('uncertain', 50.0, None)
    assert rebooted.consumed(env.rid) == pytest.approx(52.0) and rebooted.remaining(env.rid) == pytest.approx(48.0)


def test_two_threads_heartbeating_one_run_never_double_open(env):
    ledger, clock = env.ledger(), env.clock
    ledger.begin(env.rid)
    clock.advance(7)  # 3 s of advance left
    barrier, results, errors = threading.Barrier(8), [], []

    def beat():
        try:
            barrier.wait(5)
            results.append(ledger.heartbeat(env.rid)['charged'])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=beat) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == [] and sorted(results) == [0.0] * 7 + [pytest.approx(7.0)]
    assert states(env) == [('used', 7.0), ('open', 10.0)] and ledger.consumed(env.rid) == pytest.approx(17.0)
    stop = threading.Event()

    def churn():
        try:
            while not stop.is_set():
                ledger.heartbeat(env.rid)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=churn) for _ in range(2)]
    for thread in threads:
        thread.start()
    for _ in range(160):
        clock.advance(0.25)
        time.sleep(0.0005)
    stop.set()
    for thread in threads:
        thread.join(10)
    assert errors == []
    ledger.heartbeat(env.rid)
    assert len([r for r in rows(env) if r['state'] == 'open']) == 1 and coverage_is_disjoint(env)
    assert ledger.consumed(env.rid) >= 47.0 - 1e-9  # 7 + 40 observed seconds


def test_concurrent_reservations_never_exceed_the_pool(env):
    ledger = env.ledger()
    ledger.begin(env.rid)
    barrier, granted, refused = threading.Barrier(8), [], []

    def take(index):
        barrier.wait(5)
        try:
            granted.append(ledger.reserve(env.rid, 20, 'child', f'agent-{index}'))
        except PolicyError:
            refused.append(index)

    threads = [threading.Thread(target=take, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(granted) == 4 and len(refused) == 4
    usage = ledger.usage(env.rid)
    assert usage['reserved'] == 80.0 and usage['reserved'] <= usage['budget'] - usage['consumed']


def test_legacy_checkpoint_elapsed_is_charged_once_as_a_baseline(env):
    env.store.query('UPDATE runs SET elapsed=? WHERE id=?', (37.5, env.rid))
    ledger = env.ledger()
    assert ledger.begin(env.rid)['consumed'] == pytest.approx(47.5)
    env.clock.advance(1)
    ledger.end(env.rid)
    ledger.begin(env.rid)
    assert [r['state'] for r in rows(env)].count('baseline') == 1
    assert ledger.usage(env.rid)['baseline_seconds'] == 37.5


def test_budget_comes_from_the_pinned_run_config(env):
    ledger = env.ledger()
    assert ledger.remaining(env.rid) == 100.0
    env.store.query('UPDATE runs SET config=? WHERE id=?', (json.dumps({'limits': {'max_seconds': float('inf')}}), env.rid))
    with pytest.raises(PolicyError, match='no valid time budget'):
        ledger.remaining(env.rid)
    with pytest.raises(PolicyError, match='Run not found'):
        ledger.remaining('e' * 32)


def test_ticker_renews_from_a_thread_and_reports_exhaustion_once(env):
    ledger, clock, hits = env.ledger(), env.clock, []
    with pytest.raises(ValueError):
        tl.Ticker(ledger, env.rid, interval=6.0)
    with pytest.raises(ValueError):
        tl.Ticker(ledger, env.rid, on_exhausted='not callable')
    ledger.begin(env.rid)
    ticker = ledger.ticker(env.rid, interval=0.002, on_exhausted=hits.append)
    observed = 0.0
    deadline = time.monotonic() + 20
    while not hits and time.monotonic() < deadline:
        clock.advance(1.0)
        observed += 1.0
        time.sleep(0.01)
    assert hits == [env.rid] and ticker.exhausted and ticker.error is None
    assert ticker.stop() and ticker.error is None
    assert len([r for r in rows(env) if r['state'] == 'open']) == 1
    ledger.end(env.rid)
    assert ledger.consumed(env.rid) >= observed - 1e-9 >= 90.0 - 1e-9  # exhaustion came no earlier than one slice
    # A ticker whose interval was closed under it stops with the ledger's refusal.
    ledger.begin(env.rid)
    ticker = ledger.ticker(env.rid, interval=0.002)
    ledger.end(env.rid)
    clock.advance(1)
    ticker.thread.join(5)
    assert not ticker.thread.is_alive() and ticker.error == 'PolicyError'


@pytest.mark.skipif(sys.platform != 'darwin', reason='kern.bootsessionuuid is a macOS sysctl.')
def test_boot_session_reads_this_boot_identity(env):
    first, second = boot_session(), boot_session()
    assert first and BOOT.fullmatch(first) and first == second
    ledger = TimeLedger(env.store)
    assert ledger.boot_uuid == first and ledger.slice_seconds == 10.0 and ledger.lead == 5.0


def test_invalid_configuration_and_clocks_are_refused(env, monkeypatch):
    for kw in ({'slice_seconds': 0}, {'slice_seconds': float('nan')}, {'slice_seconds': 7200}, {'lead': 10},
               {'lead': 0}, {'slice_seconds': True}):
        with pytest.raises(ValueError):
            env.ledger(**kw)
    with pytest.raises(ValueError):
        TimeLedger(env.store, 'boot-a', clock=None)
    for boot in ('', 'x' * 129, 'bad\nboot', 7):
        with pytest.raises(PolicyError, match='boot session'):
            TimeLedger(env.store, boot)
    monkeypatch.setattr(tl, 'boot_session', lambda: None)
    with pytest.raises(PolicyError, match='boot session'):
        TimeLedger(env.store)
    broken = TimeLedger(env.store, 'boot-a', clock=lambda: math.nan, wall=time.time)
    with pytest.raises(PolicyError, match='invalid time'):
        broken.begin(env.rid)
    assert rows(env) == []
    ledger = env.ledger()
    for bad in (None, '', 'A' * 32, 'g' * 32, 7, 'e' * 32):  # the last is well formed, but no run owns it
        with pytest.raises(PolicyError, match='Run not found'):
            ledger.begin(bad)
    assert rows(env) == [] and ledger.usage(env.rid)['open'] is False
