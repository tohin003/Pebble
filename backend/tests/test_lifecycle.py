"""Lifecycle: injected sleep, wake and clock changes, catch-up policy, and DST in London, Kolkata and Lord_Howe.

The integration suites start the actual App. The parent-wired scheduler is closed
and replaced by a Jobs instance whose wall clock and lifecycle clocks come from
one simulated machine, so sleep, clock jumps and native hints are exact. A fake
model answers. No provider, credential, shell command or OS notification is
reached, and nothing here sleeps the real machine: the macOS clock semantics are
only sanity-checked (see test_system_clocks_are_finite_and_ordered).
"""

import sys
import time
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from pebble.app import App
from pebble.jobs import Jobs, daily_slot, duration, iso, latest_slot, machine, next_slot, parse_schedule
from pebble.lifecycle import CLOCK_JUMP, HINT_HOLD, MAX_OBSERVATIONS, SLEEP_MIN, Lifecycle, system_clocks
from pebble.notifications import Notifications
from pebble.permissions import DEFAULT
from pebble.store import Store


T0 = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc).timestamp()
EVERY_MINUTE = {'kind': 'interval', 'seconds': 60}
try:
    for name in ('Europe/London', 'Asia/Kolkata', 'Australia/Lord_Howe'):
        ZoneInfo(name)
    ZONES = True
except Exception:
    ZONES = False
needs_zones = pytest.mark.skipif(not ZONES, reason='IANA time zone data is unavailable')


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


class Machine:
    """One simulated computer: the wall clock can jump, awake stops while asleep, total counts through sleep."""

    def __init__(self, wall, sleep_clock=True):
        self.wall, self.awake, self.total, self.sleep_clock = float(wall), 5000.0, 9000.0, sleep_clock

    def __call__(self):
        return self.wall

    def clocks(self):
        return self.wall, self.awake, self.total if self.sleep_clock else self.awake

    def run(self, seconds):
        self.wall += seconds
        self.awake += seconds
        self.total += seconds

    def sleep(self, seconds):
        self.wall += seconds
        self.total += seconds

    def jump(self, seconds):
        self.wall += seconds

    def at(self, moment):
        """Run (awake) until the wall clock reads `moment`."""
        self.run(moment - self.wall)


def bare(tmp_path, machine, **extra):
    """A minimal host for unit tests: a real Store and an event log."""
    events = []
    app = SimpleNamespace(store=Store(tmp_path / 'data'), emit=lambda *args: events.append(args), time_ledger=None,
                          active_run=None, jobs=None, **extra)
    return Lifecycle(app, clocks=machine.clocks), app, events


def harness(home, resources, now):
    events = []
    app = App(home, resources, lambda *args: events.append(args))
    wired = getattr(app, 'jobs', None)
    if wired is not None:
        assert wired.close()  # the parent-wired scheduler; these tests drive their own machine
    clock = Machine(now)
    lifecycle = Lifecycle(app, clocks=clock.clocks)
    outbox = Notifications(app, clock=clock)
    jobs = Jobs(app, clock=clock, lifecycle=lifecycle, notifications=outbox)
    app.jobs = jobs
    jobs.start(thread=False)
    return SimpleNamespace(app=app, jobs=jobs, clock=clock, lifecycle=lifecycle, outbox=outbox, events=events)


@pytest.fixture
def make(tmp_path):
    made = []

    def build(now=T0):
        h = harness(tmp_path / f'data{len(made)}', tmp_path / 'resources', now)
        made.append(h)
        return h
    yield build
    for h in made:
        h.jobs.close()
        h.app.close()


def model(app, *responses):
    queue = list(responses)

    def complete(messages, tools, cancel, token, effort):
        if not queue:
            raise AssertionError('Unexpected model request.')
        item = queue.pop(0)
        token(item)
        return {'role': 'assistant', 'content': item}

    app.model.complete = complete


def create(h, schedule, label='Status check', task='Summarize the project status.'):
    chat = h.app.new_chat()
    fingerprint = h.jobs.operation('preview', chat_id=chat['id'])['config_fingerprint']
    return h.jobs.operation('create', chat_id=chat['id'], label=label, task=task, schedule=schedule,
                            config_fingerprint=fingerprint)


def settle(h, rid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        worker = h.app.worker
        if h.app.runs.get(rid)['state'] == 'completed' and h.app.active is None and not (worker and worker.is_alive()):
            h.jobs.refresh()
            return
        time.sleep(0.01)
    pytest.fail('Run did not settle: ' + h.app.runs.get(rid)['state'])


def fires(h, job_id):
    return h.app.store.query('SELECT * FROM job_fires WHERE job_id=? ORDER BY scheduled', (job_id,))


def hosted(h, kind=None):
    rows = h.app.store.query('SELECT * FROM host_notices ORDER BY created,rowid')
    return [row for row in rows if kind is None or row['kind'] == kind]


def job_row(h, job_id):
    return h.app.store.query('SELECT * FROM jobs WHERE id=?', (job_id,))[0]


# Pure detection with injected clocks

def test_clock_samples_classify_sleep_clock_jumps_and_noise(tmp_path):
    clock = Machine(T0)
    lifecycle, app, events = bare(tmp_path, clock)
    assert lifecycle.observe() == {'sleeping': False, 'wakes': [], 'changes': []}  # the first sample is a baseline
    clock.run(5)
    assert lifecycle.observe()['wakes'] == []
    before = clock.wall
    clock.sleep(3600)
    clock.run(1)
    seen = lifecycle.observe()
    [wake] = seen['wakes']
    assert (wake['seconds'], wake['started'], wake['ended'], wake['source']) == (3600.0, before, clock.wall, 'clock')
    assert seen['changes'] == []  # the wall clock moved with the sleep-including clock
    assert lifecycle.observe()['wakes'] == []  # reported once
    clock.sleep(SLEEP_MIN - 0.5)
    assert lifecycle.observe() == {'sleeping': False, 'wakes': [], 'changes': []}  # clock noise, not sleep
    clock.jump(CLOCK_JUMP - 1)
    assert lifecycle.observe()['changes'] == []
    clock.jump(CLOCK_JUMP + 1)
    [forward] = lifecycle.observe()['changes']
    assert forward['kind'] == 'clock_forward' and forward['seconds'] == CLOCK_JUMP + 1
    clock.jump(-7200)
    [backward] = lifecycle.observe()['changes']
    assert backward['kind'] == 'clock_backward' and backward['seconds'] == -7200
    clock.awake -= 50  # a regressing monotonic clock: a new baseline, nothing inferred
    assert lifecycle.observe() == {'sleeping': False, 'wakes': [], 'changes': []}
    clock.sleep(600)
    clock.jump(300)  # NTP corrects the wall clock right after wake
    seen = lifecycle.observe()
    assert [w['seconds'] for w in seen['wakes']] == [600.0] and [c['seconds'] for c in seen['changes']] == [300.0]
    rows = app.store.query('SELECT kind,source,seconds,zone FROM lifecycle_observations ORDER BY rowid')
    assert [(r['kind'], r['source'], r['seconds']) for r in rows] == [
        ('sleep', 'clock', 3600.0), ('clock_forward', 'clock', CLOCK_JUMP + 1), ('clock_backward', 'clock', -7200.0),
        ('sleep', 'clock', 600.0), ('clock_forward', 'clock', 300.0)]
    assert all(r['zone'] is None for r in rows)
    assert [args[0] for args in events] == ['lifecycle'] * 5 and all(set(args[1]) == {'kind', 'seconds'} for args in events)
    for _ in range(MAX_OBSERVATIONS + 10):
        clock.sleep(10)
        lifecycle.observe()
    assert app.store.query('SELECT count(*) AS n FROM lifecycle_observations')[0]['n'] == MAX_OBSERVATIONS
    for invalid in [(T0, 1.0), (T0, float('nan'), 1.0), (T0, True, 1.0), None, (T0, 1.0, float('inf'))]:
        broken = Lifecycle(app, clocks=lambda value=invalid: value)
        with pytest.raises(ValueError, match='invalid sample'):
            broken.observe()


def test_native_hints_speed_detection_expire_and_never_double_count(tmp_path):
    clock = Machine(T0)
    woken = []
    lifecycle, app, _ = bare(tmp_path, clock)
    app.jobs = SimpleNamespace(wake=lambda: woken.append(True))
    lifecycle.observe()
    assert lifecycle.event('will_sleep')['accepted'] is True and woken == []
    assert lifecycle.sleeping() and lifecycle.observe()['sleeping'] is True
    clock.run(HINT_HOLD - 1)
    assert lifecycle.observe()['sleeping'] is True
    clock.run(1)
    assert lifecycle.observe()['sleeping'] is False  # no sleep followed the hint; it lapses
    # Clock detection first, then the late native did-wake: one wake only.
    lifecycle.event('will_sleep')
    clock.sleep(1800)
    seen = lifecycle.observe()
    assert seen['sleeping'] is False and [w['source'] for w in seen['wakes']] == ['clock']
    lifecycle.event('did_wake')
    assert lifecycle.observe()['wakes'] == [] and woken == [True]
    # Native did-wake first, then the clock sample: still one wake, from the clock.
    lifecycle.event('will_sleep')
    clock.sleep(900)
    lifecycle.event('did_wake')
    seen = lifecycle.observe()
    assert [(w['source'], w['seconds']) for w in seen['wakes']] == [('clock', 900.0)]
    assert lifecycle.observe()['wakes'] == []
    # A did-wake without a will-sleep carries no window.
    lifecycle.event('did_wake')
    assert lifecycle.observe()['wakes'] == []
    for kind, fields in [('approve', {}), ('will_sleep', {'timezone': 'UTC'}), ('timezone_changed', {}),
                         ('timezone_changed', {'timezone': '../../etc/passwd'}), ('timezone_changed', {'timezone': 'localtime'}),
                         ('timezone_changed', {'timezone': 7}), ('timezone_changed', {'timezone': 'A/' + 'b' * 70}),
                         ('did_wake', {'seconds': 10}), (None, {})]:
        with pytest.raises(ValueError):
            lifecycle.event(kind, **fields)
    lifecycle.event('timezone_changed', timezone='Asia/Kolkata')
    [row] = app.store.query("SELECT zone,source FROM lifecycle_observations WHERE kind='timezone_changed'")
    assert row == {'zone': 'Asia/Kolkata', 'source': 'native'} and len(woken) == 4


def test_without_a_sleep_clock_only_native_hints_identify_sleep(tmp_path):
    clock = Machine(T0, sleep_clock=False)
    lifecycle, _, _ = bare(tmp_path, clock)
    lifecycle.observe()
    clock.sleep(900)
    seen = lifecycle.observe()
    assert seen['wakes'] == [] and [c['kind'] for c in seen['changes']] == ['clock_forward']
    lifecycle.event('will_sleep')
    clock.sleep(900)
    lifecycle.event('did_wake')
    seen = lifecycle.observe()
    assert [(w['source'], w['seconds']) for w in seen['wakes']] == [('native', 900.0)]
    assert [c['kind'] for c in seen['changes']] == ['clock_forward']  # documented: indistinguishable without hints
    assert lifecycle.status()['sleep_clock'] is False


def test_sleep_is_recorded_as_ledger_evidence_for_the_active_root(tmp_path):
    clock, calls = Machine(T0), []

    def observe_sleep(run_id):
        calls.append(run_id)
        raise ValueError('refused')  # a ledger refusal never blocks lifecycle observation

    lifecycle, _, _ = bare(tmp_path, clock, )
    lifecycle.app.time_ledger = SimpleNamespace(observe_sleep=observe_sleep)
    lifecycle.observe()
    clock.sleep(600)
    assert lifecycle.observe()['wakes'] and calls == []  # no active root
    lifecycle.app.active_run = 'a' * 32
    clock.sleep(600)
    assert lifecycle.observe()['wakes'] and calls == ['a' * 32]


def test_system_clocks_are_finite_and_ordered():
    first = system_clocks()
    second = system_clocks()
    assert all(isinstance(v, float) and v == v and abs(v) != float('inf') for v in first + second)
    assert second[1] >= first[1] and second[2] >= first[2]
    if sys.platform == 'darwin':
        # Darwin's CLOCK_MONOTONIC counts sleep and time.monotonic does not, so total >= awake.
        # This is a sanity check of that assumption only; no real sleep is exercised.
        assert second[2] >= second[1] - 1.0


# Scheduler integration

def test_will_sleep_defers_due_fires_and_wake_runs_the_latest_once(make):
    h = make()
    job = create(h, EVERY_MINUTE)
    h.clock.at(T0 + 30)
    h.jobs.tick()
    h.lifecycle.event('will_sleep')
    h.clock.at(T0 + 61)
    result = h.jobs.tick()
    assert result['busy'] == 'system_sleep' and result['launched'] is None
    assert job_row(h, job['id'])['deferred_reason'] == 'system_sleep' and fires(h, job['id']) == []
    h.clock.sleep(3 * 3600)
    model(h.app, 'Status: green.')
    result = h.jobs.tick()
    settle(h, result['run_id'])
    [fire] = fires(h, job['id'])
    assert fire['scheduled'] == T0 + 10860 and fire['missed'] == 180
    [summary] = hosted(h, 'wake_summary')
    assert summary['chat_id'] is None and summary['job_id'] is None
    assert summary['title'] == f'Schedules while {machine()} slept'
    assert summary['body'] == (f'While {machine()} slept (about 3 h), 1 schedule was due. 1 runs once, at its latest '
                               'due time. 180 earlier times were combined, not replayed.')
    for _ in range(3):
        h.clock.run(5)
        assert h.jobs.tick()['launched'] is None
    assert len(fires(h, job['id'])) == 1 and len(hosted(h, 'wake_summary')) == 1
    assert len(h.app.runs.list()) == 1


def test_skip_if_late_consumes_a_late_slot_without_a_run_and_reports_it(make):
    h = make(utc(2026, 10, 12, 6))
    policy = {'catch_up': 'skip_if_late', 'grace_seconds': 7200}
    late = create(h, {'kind': 'daily', 'time': '09:00', 'timezone': 'Europe/London', **policy}, label='Morning brief')
    prompt = create(h, {'kind': 'interval', 'seconds': 3600}, label='Hourly')
    assert late['schedule'] == {'kind': 'daily', 'time': '09:00', 'timezone': 'Europe/London', **policy}
    assert prompt['schedule'] == {'kind': 'interval', 'seconds': 3600}  # the default policy stores no keys
    assert late['next_due'] == '2026-10-12T08:00:00Z'
    h.jobs.tick()
    h.clock.sleep(5 * 3600)  # wakes at 11:00Z, three hours after 09:00 BST
    model(h.app, 'Hourly result.')
    result = h.jobs.tick()
    assert result['skipped_late'] == [late['id']] and result['launched']
    settle(h, result['run_id'])
    assert fires(h, late['id']) == []
    row = job_row(h, late['id'])
    assert row['last_scheduled'] == utc(2026, 10, 12, 8) and iso(row['next_due']) == '2026-10-13T08:00:00Z'
    [skip] = hosted(h, 'late_skipped')
    assert skip['job_id'] == late['id'] and skip['title'] == 'Scheduled job “Morning brief” skipped a late time'
    assert skip['body'] == ('The time 2026-10-12T08:00:00Z was more than 2 h late, so it did not run, as this schedule '
                            'asks. Next: 2026-10-13T08:00:00Z.')
    [summary] = hosted(h, 'wake_summary')
    assert summary['body'] == (f'While {machine()} slept (about 5 h), 2 schedules were due. 1 runs once, at its latest '
                               'due time. 4 earlier times were combined, not replayed. 1 was skipped because it was '
                               'later than its late limit.')
    assert [f['missed'] for f in fires(h, prompt['id'])] == [4]
    h.jobs.operation('delete', chat_id=prompt['chat_id'], job_id=prompt['id'], revision=prompt['revision'])
    # Within the grace window the slot runs.
    h.clock.at(utc(2026, 10, 13, 9, 30))
    model(h.app, 'Morning.')
    result = h.jobs.tick()
    assert result['skipped_late'] == [] and result['launched']
    settle(h, result['run_id'])
    assert [iso(f['scheduled']) for f in fires(h, late['id'])] == ['2026-10-13T08:00:00Z']
    # A late one-time job finishes without a run.
    once = create(h, {'kind': 'once', 'at': iso(h.clock.wall + 600), **policy}, label='Once')
    h.clock.sleep(3 * 3600)
    assert h.jobs.tick()['skipped_late'] == [once['id']]
    assert job_row(h, once['id'])['state'] == 'finished' and fires(h, once['id']) == []
    for bad in [{'catch_up': 'replay_all'}, {'catch_up': 'latest', 'grace_seconds': 60},
                {'catch_up': 'skip_if_late', 'grace_seconds': 59}, {'catch_up': 'skip_if_late', 'grace_seconds': True},
                {'catch_up': 'skip_if_late', 'grace_seconds': 86401}, {'catch_up': ['latest']}]:
        with pytest.raises(ValueError):
            parse_schedule({**EVERY_MINUTE, **bad}, T0)
    assert parse_schedule({**EVERY_MINUTE, 'catch_up': 'latest'}, T0) == EVERY_MINUTE
    assert parse_schedule({**EVERY_MINUTE, 'catch_up': 'skip_if_late'}, T0)['grace_seconds'] == 7200


def test_backward_clock_waits_quietly_and_forward_jump_coalesces(make):
    h = make()
    job = create(h, EVERY_MINUTE)
    h.jobs.tick()
    model(h.app, 'First.', 'Second.', 'Third.')
    h.clock.at(T0 + 61)
    settle(h, h.jobs.tick()['run_id'])
    h.clock.jump(-7200)
    assert h.jobs.tick()['launched'] is None
    [moved] = hosted(h, 'clock_changed')
    assert moved['body'].startswith('The system clock moved back by about 2 h.') and moved['chat_id'] is None
    for _ in range(3):
        h.clock.run(60)
        assert h.jobs.tick()['launched'] is None  # wall time is still before next_due
    assert len(hosted(h, 'clock_changed')) == 1
    h.clock.at(T0 + 121)  # awake time catches the wall clock up; nothing is replayed on the way
    settle(h, h.jobs.tick()['run_id'])
    h.clock.jump(3600)
    settle(h, h.jobs.tick()['run_id'])
    assert [(f['scheduled'] - T0, f['missed']) for f in fires(h, job['id'])] == [(60, 0), (120, 0), (3720, 59)]
    assert len(hosted(h, 'clock_changed')) == 1  # a forward jump is coalesced, not announced


@needs_zones
def test_london_gap_is_skipped_with_one_quiet_notice_and_the_fold_runs_once(make):
    london = ZoneInfo('Europe/London')
    assert daily_slot(date(2026, 3, 29), '01:30', london) is None
    gap = {'kind': 'daily', 'time': '01:30', 'timezone': 'Europe/London'}
    assert iso(next_slot(gap, utc(2026, 3, 28, 12), 0)) == '2026-03-30T00:30:00Z'
    h = make(utc(2026, 3, 27, 12))
    job = create(h, gap, label='Night check')
    assert job['next_due'] == '2026-03-28T01:30:00Z'
    h.jobs.tick()
    model(h.app, 'Night one.', 'Night three.', 'Fold.')
    h.clock.at(utc(2026, 3, 28, 1, 31))
    settle(h, h.jobs.tick()['run_id'])
    h.clock.at(utc(2026, 3, 29, 1, 29))
    h.jobs.tick()
    assert hosted(h, 'dst_skipped') == []  # the would-be time has not passed yet
    h.clock.at(utc(2026, 3, 29, 1, 31))
    assert h.jobs.tick()['launched'] is None
    [skipped] = hosted(h, 'dst_skipped')
    assert skipped['title'] == 'Scheduled job “Night check” skipped 2026-03-29' and skipped['job_id'] == job['id']
    assert skipped['body'] == ('Europe/London has no 01:30 on 2026-03-29 because the clocks moved forward, so this daily '
                               'schedule did not run that day. Next: 2026-03-30T00:30:00Z.')
    for hour in (5, 12, 23):
        h.clock.at(utc(2026, 3, 29, hour))
        h.jobs.tick()
    assert len(hosted(h, 'dst_skipped')) == 1
    h.clock.at(utc(2026, 3, 30, 0, 31))
    settle(h, h.jobs.tick()['run_id'])
    assert [iso(f['scheduled']) for f in fires(h, job['id'])] == ['2026-03-28T01:30:00Z', '2026-03-30T00:30:00Z']
    # The October fold: 01:30 happens at 00:30Z (BST) and again at 01:30Z (GMT); it runs once, at the first.
    fold = {'kind': 'daily', 'time': '01:30', 'timezone': 'Europe/London'}
    assert iso(daily_slot(date(2026, 10, 25), '01:30', london)) == '2026-10-25T00:30:00Z'
    h.clock.at(utc(2026, 10, 24, 12))
    h.jobs.operation('delete', chat_id=job['chat_id'], job_id=job['id'], revision=job['revision'])
    folded = create(h, fold, label='Fold check')
    h.clock.at(utc(2026, 10, 25, 1, 45))
    settle(h, h.jobs.tick()['run_id'])
    h.clock.run(30)
    assert h.jobs.tick()['launched'] is None
    [fire] = fires(h, folded['id'])
    assert iso(fire['scheduled']) == '2026-10-25T00:30:00Z' and fire['missed'] == 0
    assert job_row(h, folded['id'])['next_due'] == utc(2026, 10, 26, 1, 30)
    # Deleting the March job removed its notice, and a fold day is not a skipped day.
    assert hosted(h, 'dst_skipped') == []


@needs_zones
def test_kolkata_has_no_dst_and_device_zone_changes_do_not_move_a_reviewed_time(make):
    kolkata = {'kind': 'daily', 'time': '09:00', 'timezone': 'Asia/Kolkata'}
    slot, slots = utc(2026, 1, 1), []
    for _ in range(366):
        slot = next_slot(kolkata, slot, 0)
        slots.append(slot)
    assert all(b - a == 86400 for a, b in zip(slots, slots[1:]))
    assert {datetime.fromtimestamp(s, timezone.utc).strftime('%H:%M') for s in slots} == {'03:30'}
    h = make(utc(2026, 3, 28, 12))
    job = create(h, kolkata, label='India standup')
    h.jobs.tick()
    for zone in ('Europe/London', 'America/New_York', 'Australia/Lord_Howe'):
        h.lifecycle.event('timezone_changed', timezone=zone)
        h.jobs.tick()
    assert job_row(h, job['id'])['next_due'] == utc(2026, 3, 29, 3, 30)
    zones = [r['zone'] for r in h.app.store.query(
        "SELECT zone FROM lifecycle_observations WHERE kind='timezone_changed' ORDER BY rowid")]
    assert zones == ['Europe/London', 'America/New_York', 'Australia/Lord_Howe']
    model(h.app, 'Day one.', 'Day two.')
    for day in (29, 30):  # London changes its offset on the 29th; Kolkata does not move
        h.clock.at(utc(2026, 3, day, 3, 31))
        settle(h, h.jobs.tick()['run_id'])
    assert [iso(f['scheduled']) for f in fires(h, job['id'])] == ['2026-03-29T03:30:00Z', '2026-03-30T03:30:00Z']
    assert hosted(h, 'dst_skipped') == []
    assert h.lifecycle.quit_summary() == {'active': 1, 'next_due': '2026-03-31T03:30:00Z',
                                          'message': '1 schedule will not fire until Pebble runs; next at 09:00 Asia/Kolkata.'}


@needs_zones
def test_lord_howe_half_hour_gap_and_fold(make):
    lord_howe = ZoneInfo('Australia/Lord_Howe')
    # April 5, 2026: 02:00 LHDT (+11) falls back to 01:30 LHST (+10:30), so 01:45 happens twice.
    assert iso(daily_slot(date(2026, 4, 5), '01:45', lord_howe)) == '2026-04-04T14:45:00Z'
    fold = {'kind': 'daily', 'time': '01:45', 'timezone': 'Australia/Lord_Howe'}
    first = next_slot(fold, utc(2026, 4, 4), 0)
    assert iso(first) == '2026-04-04T14:45:00Z'
    assert latest_slot(fold, utc(2026, 4, 4, 15, 20), first, 0) == (first, 0)  # after both 01:45 instants
    assert iso(next_slot(fold, first, 0)) == '2026-04-05T15:15:00Z'
    # October 4, 2026: 02:00 LHST jumps to 02:30 LHDT, so 02:15 does not exist that day.
    assert daily_slot(date(2026, 10, 4), '02:15', lord_howe) is None
    gap = {'kind': 'daily', 'time': '02:15', 'timezone': 'Australia/Lord_Howe'}
    h = make(utc(2026, 10, 2, 12))
    job = create(h, gap, label='Island check')
    assert job['next_due'] == '2026-10-02T15:45:00Z'
    h.jobs.tick()
    model(h.app, 'Before.', 'After.')
    h.clock.at(utc(2026, 10, 2, 15, 46))
    settle(h, h.jobs.tick()['run_id'])
    h.clock.at(utc(2026, 10, 3, 15, 46))
    assert h.jobs.tick()['launched'] is None
    [skipped] = hosted(h, 'dst_skipped')
    assert skipped['body'].startswith('Australia/Lord_Howe has no 02:15 on 2026-10-04 because the clocks moved forward')
    assert skipped['body'].endswith('Next: 2026-10-04T15:15:00Z.')
    h.clock.at(utc(2026, 10, 4, 15, 16))
    settle(h, h.jobs.tick()['run_id'])
    assert [iso(f['scheduled']) for f in fires(h, job['id'])] == ['2026-10-02T15:45:00Z', '2026-10-04T15:15:00Z']


def test_quit_summary_counts_only_active_schedules(make):
    h = make()
    assert h.lifecycle.quit_summary() == {'active': 0, 'next_due': None, 'message': None}
    first = create(h, {'kind': 'interval', 'seconds': 3600})
    create(h, {'kind': 'once', 'at': iso(T0 + 600)})
    paused = create(h, EVERY_MINUTE)
    h.jobs.operation('pause', chat_id=paused['chat_id'], job_id=paused['id'], revision=paused['revision'])
    assert h.lifecycle.quit_summary() == {'active': 2, 'next_due': iso(T0 + 600),
                                          'message': '2 schedules will not fire until Pebble runs; next at '
                                                     '2026-10-10 08:10 UTC.'}
    assert first['state'] == 'active' and duration(3 * 3600 + 600) == '3 h 10 min' and duration(50 * 3600) == '2 days 2 h'


def test_without_collaborators_the_scheduler_records_no_lifecycle_or_host_rows(tmp_path):
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        wired = getattr(app, 'jobs', None)
        if wired is not None:
            assert wired.close()
        clock = Machine(T0)
        jobs = Jobs(app, clock=clock)
        app.jobs = jobs
        jobs.start(thread=False)
        assert jobs.lifecycle is getattr(app, 'lifecycle', None) and jobs.notifications is getattr(app, 'notifications', None)
        chat = app.new_chat()
        fingerprint = jobs.operation('preview', chat_id=chat['id'])['config_fingerprint']
        jobs.operation('create', chat_id=chat['id'], label='Plain', task='Summarize.', schedule=EVERY_MINUTE,
                       config_fingerprint=fingerprint)
        clock.sleep(3600)
        app.setting('permissions', {'mode': 'auto', 'tools': dict(DEFAULT['tools'])})
        result = jobs.tick()
        assert result['blocked'] and result['skipped_late'] == []
        if getattr(app, 'lifecycle', None) is None:
            tables = {r['name'] for r in app.store.query("SELECT name FROM sqlite_master WHERE type='table'")}
            assert not tables & {'lifecycle_observations', 'native_deliveries', 'native_banners', 'host_notices'}
    finally:
        app.close()
