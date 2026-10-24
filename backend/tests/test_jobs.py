"""Scheduled jobs: reviewed native pins, host-origin fires, no replay and quiet outboxes.

The suites start the actual App, controller and runner. The scheduler thread is
not started; each test drives Jobs.tick() with an injected clock. A fake model
returns deterministic answers. No model HTTP request, credential, shell command
or external notification occurs.
"""

import copy
import json
import sqlite3
import threading
import time
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from pebble.app import App
from pebble.jobs import (MAX_FIRES, MAX_INTERVAL, Jobs, daily_slot, iso, latest_slot, next_slot,
                         parse_schedule, run_status)
from pebble.permissions import DEFAULT


T0 = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc).timestamp()
EVERY_MINUTE = {'kind': 'interval', 'seconds': 60}
TASK = 'Summarize the project status.'
ITEMS = [{'id': 'scope', 'title': 'Which scope should I use?', 'options': [
    {'id': 'project', 'label': 'This project'}, {'id': 'all', 'label': 'All projects'}]}]

try:
    ZoneInfo('America/New_York')
    ZoneInfo('UTC')
    ZONES = True
except Exception:
    ZONES = False
needs_zones = pytest.mark.skipif(not ZONES, reason='IANA time zone data is unavailable')


class Crash(BaseException):
    """Simulated process loss; production code must not swallow it."""


def crash(*args, **kwargs):
    raise Crash()


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


def harness(home, resources, now=T0):
    events = []
    # Phase 34 (trial 2): the App's own scheduler never starts here. Started, its thread ticked on the real clock before
    # close() and, after a restart, fired the long-overdue slot of a job these tests created on their own clock (a
    # second fire in test_claim_without_a_bound_run_needs_review_after_restart under parallel load; reproduced every
    # time with 0.3 s before close()). These tests drive their own Jobs, whose start() runs the same recovery.
    real_start = Jobs.start
    Jobs.start = lambda self, thread=True: None
    try:
        app = App(home, resources, lambda *args: events.append(args))
    finally:
        Jobs.start = real_start
    wired = getattr(app, 'jobs', None)
    if wired is not None:
        assert wired.close()  # the parent-wired scheduler; these tests drive their own clock
    clock = Clock(now)
    jobs = Jobs(app, clock=clock)
    app.jobs = jobs
    jobs.start(thread=False)
    return SimpleNamespace(app=app, jobs=jobs, clock=clock, events=events)


def shutdown(h):
    h.jobs.close()
    h.app.close()


@pytest.fixture
def env(tmp_path):
    h = harness(tmp_path / 'data', tmp_path / 'resources')
    yield h
    shutdown(h)


def review(h, chat_id, **settings):
    return h.jobs.operation('preview', chat_id=chat_id, **settings)['config_fingerprint']


def create(h, chat_id, schedule=EVERY_MINUTE, label='Status check', task=TASK, **extra):
    settings = {key: extra[key] for key in ('effort', 'limits') if key in extra}
    return h.jobs.operation('create', chat_id=chat_id, label=label, task=task, schedule=schedule,
                            config_fingerprint=review(h, chat_id, **settings), **extra)


def inspect(h, job):
    return h.jobs.operation('get', chat_id=job['chat_id'], job_id=job['id'])


def fires(h, job_id):
    return h.app.store.query('SELECT * FROM job_fires WHERE job_id=? ORDER BY scheduled', (job_id,))


def notices(h, **filters):
    return h.jobs.operation('notifications', pending=False, **filters)['notifications'][::-1]  # oldest first


def inbox(h, rid):
    return h.app.store.query('SELECT count(*) AS n FROM run_inbox WHERE run_id=?', (rid,))[0]['n']


def model(app, *responses):
    queue, seen = list(responses), []

    def complete(messages, tools, cancel, token, effort):
        seen.append(copy.deepcopy(messages))
        if not queue:
            raise AssertionError('Unexpected model request.')
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            return item
        token(item)
        return {'role': 'assistant', 'content': item}

    app.model.complete = complete
    return seen


def ask():
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'question', 'type': 'function', 'function': {
        'name': 'AskUserQuestion', 'arguments': json.dumps({'questions': json.dumps(ITEMS), 'required': True})}}]}


def settle(h, rid, state='completed'):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        worker = h.app.worker
        if h.app.runs.get(rid)['state'] == state and h.app.active is None and not (worker and worker.is_alive()):
            h.jobs.refresh()
            return h.app.runs.get(rid)
        time.sleep(0.01)
    pytest.fail(f'Run did not settle as {state}: {h.app.runs.get(rid)["state"]}')


def fire_at(h, now):
    h.clock.now = now
    return h.jobs.tick()


def test_interval_slots_coalesce_a_backlog_into_the_latest_slot():
    assert next_slot(EVERY_MINUTE, T0 + 30, T0) == T0 + 60
    assert next_slot(EVERY_MINUTE, T0 + 60, T0) == T0 + 120
    assert next_slot(EVERY_MINUTE, T0 - 7200, T0) == T0 + 60  # a clock behind the anchor
    assert latest_slot(EVERY_MINUTE, T0 + 3605, T0 + 60, T0) == (T0 + 3600, 59)
    assert latest_slot(EVERY_MINUTE, T0 + 59, T0 + 60, T0) == (None, 0)
    once = {'kind': 'once', 'at': iso(T0 + 300)}
    assert next_slot(once, T0, T0) == T0 + 300 and next_slot(once, T0 + 300, T0) is None
    assert latest_slot(once, T0 + 9999, T0 + 300, T0) == (T0 + 300, 0)


@needs_zones
def test_daily_slots_skip_dst_gaps_and_run_a_repeated_hour_once():
    zone = ZoneInfo('America/New_York')
    assert daily_slot(date(2026, 3, 8), '02:30', zone) is None
    assert iso(daily_slot(date(2026, 3, 7), '02:30', zone)) == '2026-03-07T07:30:00Z'
    gap = {'kind': 'daily', 'time': '02:30', 'timezone': 'America/New_York'}
    assert iso(next_slot(gap, utc(2026, 3, 7, 12), 0)) == '2026-03-09T06:30:00Z'
    fold = {'kind': 'daily', 'time': '01:30', 'timezone': 'America/New_York'}
    first = next_slot(fold, utc(2026, 11, 1, 0), 0)
    assert iso(first) == '2026-11-01T05:30:00Z'  # the earlier, daylight-time 01:30
    assert iso(next_slot(fold, first, 0)) == '2026-11-02T06:30:00Z'
    # Waking after both wall-clock 01:30 instants still yields the earliest, once.
    assert latest_slot(fold, utc(2026, 11, 1, 7), first, 0) == (first, 0)
    daily = {'kind': 'daily', 'time': '09:00', 'timezone': 'UTC'}
    start = next_slot(daily, T0, T0)
    assert iso(start) == '2026-10-10T09:00:00Z'
    latest, missed = latest_slot(daily, start + 3 * 86400 + 3600, start, T0)
    assert iso(latest) == '2026-10-13T09:00:00Z' and missed == 3


def test_schedule_and_request_validation_rejects_untyped_or_unbounded_input(env):
    h = env
    for schedule in [
        {'kind': 'interval', 'seconds': True}, {'kind': 'interval', 'seconds': 60.0},
        {'kind': 'interval', 'seconds': float('nan')}, {'kind': 'interval', 'seconds': 59},
        {'kind': 'interval', 'seconds': MAX_INTERVAL + 1}, {'kind': 'interval', 'seconds': 60, 'permissions': {}},
        {'kind': 'daily', 'time': '24:00', 'timezone': 'UTC'}, {'kind': 'daily', 'time': '9:30', 'timezone': 'UTC'},
        {'kind': 'daily', 'time': '09:30', 'timezone': 'Mars/Olympus_Mons'},
        {'kind': 'daily', 'time': '09:30', 'timezone': '../../etc/passwd'},
        {'kind': 'daily', 'time': '09:30', 'timezone': 'localtime'},
        {'kind': 'once', 'at': '2026-10-10T09:00:00'}, {'kind': 'once', 'at': '2026-10-10T09:00:00+01:00'},
        {'kind': 'once', 'at': '2026-10-10T07:00:00Z'}, {'kind': 'once', 'at': '2028-10-10T09:00:00Z'},
        {'kind': 'once', 'at': '2026-02-30T09:00:00Z'}, {'kind': 'once', 'at': 1_800_000_000},
        {'kind': 'cron', 'expr': '* * * * *'}, [], None,
    ]:
        with pytest.raises(ValueError):
            parse_schedule(schedule, T0)
    assert parse_schedule({'kind': 'once', 'at': '2026-10-10T09:00Z'}, T0) == {'kind': 'once', 'at': '2026-10-10T09:00:00Z'}
    assert parse_schedule({'kind': 'interval', 'seconds': MAX_INTERVAL}, T0)['seconds'] == MAX_INTERVAL

    chat = h.app.new_chat()
    base = dict(chat_id=chat['id'], label='Status', task='Summarize.', schedule=EVERY_MINUTE,
                config_fingerprint=review(h, chat['id']))
    for field, value in [('permissions', DEFAULT), ('origin', 'human'), ('credentials', 'secret'), ('workspace', '/tmp'),
                         ('run_id', 'a' * 32), ('config', {}), ('auto_approve', True), ('mode', 'full')]:
        with pytest.raises(ValueError, match='computed by the host'):
            h.jobs.operation('create', **base, **{field: value})
    for field, value in [('label', ''), ('label', 'two\nlines'), ('label', 'x' * 81), ('label', 7), ('task', ''),
                         ('task', {'goal': 'x'}), ('task', 'x' * 4001), ('task', 'run\x00now'),
                         ('task', json.dumps({'task': 'go', 'permissions': {'mode': 'full'}})), ('monitor', 1),
                         ('limits', {'max_steps': True}), ('limits', {'max_seconds': float('inf')}), ('effort', 3),
                         ('config_fingerprint', 'f' * 64), ('config_fingerprint', True), ('extra', 1)]:
        with pytest.raises(ValueError):
            h.jobs.operation('create', **{**base, field: value})
    with pytest.raises(ValueError, match='Unknown job operation'):
        h.jobs.operation('approve_all')
    assert h.jobs.operation('list')['jobs'] == []
    for invalid in (float('nan'), float('inf'), True, None):
        h.clock.now = invalid
        with pytest.raises(ValueError):
            h.jobs.tick()
    h.clock.now = T0


def test_creation_pins_the_reviewed_native_configuration_and_starts_nothing(env):
    h = env
    chat = h.app.new_chat()
    reviewed = h.jobs.operation('preview', chat_id=chat['id'], schedule=EVERY_MINUTE)
    assert reviewed['next_times'][:2] == [iso(T0 + 60), iso(T0 + 120)]
    assert reviewed['config']['permissions'] == h.app.store.setting('permissions')
    h.app.setting('permissions', {'mode': 'auto', 'tools': dict(DEFAULT['tools'])})
    with pytest.raises(ValueError, match='Preview the job again'):
        h.jobs.operation('create', chat_id=chat['id'], label='Status', task=TASK, schedule=EVERY_MINUTE,
                         config_fingerprint=reviewed['config_fingerprint'])
    created = create(h, chat['id'])
    assert created['origin'] == 'human' and created['state'] == 'active' and created['revision'] == 1
    assert created['next_due'] == iso(T0 + 60) and created['config']['permissions']['mode'] == 'auto'
    pin = json.loads(h.app.store.query('SELECT pin FROM jobs WHERE id=?', (created['id'],))[0]['pin'])
    assert pin['config'] == h.app.runtime_config(h.app.chat(chat['id']), pin['config']['effort'], pin['config']['limits'])
    assert h.app.runs.list(chat['id']) == [] and fires(h, created['id']) == []


def test_due_fire_binds_a_host_run_before_launch_and_records_completion(env):
    h = env
    chat = h.app.new_chat()
    created = create(h, chat['id'])
    seen = model(h.app, 'Status: green.')
    observed, launch = [], h.app.controller.launch

    def spy(chat_value, goal, effort, sources, rid, resume=None):
        bound = h.app.store.query('SELECT status,origin,revision,pin_fingerprint FROM job_fires WHERE run_id=?', (rid,))
        observed.append((bound, h.app.runs.get(rid)['state'], [e['kind'] for e in h.app.runs.events(rid)],
                         goal, sources, resume))
        return launch(chat_value, goal, effort, sources, rid, resume)

    h.app.controller.launch = spy
    assert fire_at(h, T0 + 59)['launched'] is None
    result = fire_at(h, T0 + 61)
    rid = result['run_id']
    bound, state, kinds, goal, sources, resume = observed[0]
    assert bound == [{'status': 'bound', 'origin': 'host', 'revision': 1, 'pin_fingerprint': created['config_fingerprint']}]
    assert state == 'queued' and kinds == ['created', 'scheduled_trigger']
    assert goal == TASK and sources == [] and resume is None
    run = settle(h, rid)
    pin = json.loads(h.app.store.query('SELECT pin FROM jobs WHERE id=?', (created['id'],))[0]['pin'])
    assert run['config'] == pin['config'] and run['goal'] == TASK
    trigger = next(e for e in h.app.runs.events(rid) if e['kind'] == 'scheduled_trigger')['payload']
    assert trigger['origin'] == 'host' and trigger['job_origin'] == 'human' and trigger['authorization'] == 'none'
    assert trigger['scheduled'] == iso(T0 + 60) and trigger['fire_id'] == result['launched']
    # The transcript gains a host label and the answer, never a fabricated user message.
    messages = h.app.chat(chat['id'])['messages']
    assert [m['role'] for m in messages] == ['assistant', 'assistant']
    assert 'Scheduled job' in messages[0]['content'] and 'not by a new message' in messages[0]['content']
    assert inbox(h, rid) == 0 and any(TASK in str(m.get('content')) for m in seen[0])
    [fire] = fires(h, created['id'])
    assert fire['status'] == 'completed' and fire['run_state'] == 'completed'
    assert h.jobs.scheduled_run(rid)['job_id'] == created['id'] and h.jobs.scheduled_run(uuid.uuid4().hex) is None
    [notice] = notices(h)
    assert notice['kind'] == 'completed' and notice['body'].startswith('Status: green.')
    assert notice['body_origin'] == 'run_output' and notice['run_id'] == rid
    assert any(args[0] == 'job_notification' for args in h.events)


def test_sleep_coalesces_missed_slots_into_one_latest_fire(env):
    h = env
    chat = h.app.new_chat()
    created = create(h, chat['id'])
    model(h.app, 'Status: green.')
    settle(h, fire_at(h, T0 + 3605)['run_id'])
    assert h.jobs.tick()['launched'] is None
    [fire] = fires(h, created['id'])
    assert fire['scheduled'] == T0 + 3600 and fire['missed'] == 59
    assert inspect(h, created)['job']['next_due'] == iso(T0 + 3660)
    assert len(h.app.runs.list(chat['id'])) == 1
    assert '59 earlier scheduled times were combined' in h.app.chat(chat['id'])['messages'][0]['content']


def test_clock_moving_backward_never_reclaims_a_slot(env):
    h = env
    chat = h.app.new_chat()
    created = create(h, chat['id'])
    model(h.app, 'First.', 'Second.')
    settle(h, fire_at(h, T0 + 61)['run_id'])
    for now in (T0 - 7200, T0 + 30, T0 + 61, T0 + 119):
        assert fire_at(h, now)['launched'] is None
    assert inspect(h, created)['job']['next_due'] == iso(T0 + 120)
    settle(h, fire_at(h, T0 + 120)['run_id'])
    assert [f['scheduled'] for f in fires(h, created['id'])] == [T0 + 60, T0 + 120]


def test_live_work_defers_fires_without_steering_other_runs(env, monkeypatch):
    h = env
    chat, other = h.app.new_chat(), h.app.new_chat()
    created = create(h, chat['id'])
    h.clock.now = T0 + 61
    h.app.active = other['id']
    assert h.jobs.tick()['busy'] == 'foreground_run'
    h.app.active = None
    monkeypatch.setattr(h.app.agents, 'busy', lambda: True)
    assert h.jobs.tick()['busy'] == 'delegated_agents'
    monkeypatch.undo()
    with h.app.agents.lane.local('root:elsewhere', threading.Event()):
        assert h.jobs.tick()['busy'] == 'inference_lane'
    with h.app.action_lock:
        assert h.jobs.tick()['busy'] == 'approval_in_progress'
    waiting = h.app.runs.create(other['id'], 'Earlier task', h.app.runtime_config(other, False), [])
    assert h.jobs.tick()['busy'] == 'live_or_waiting_run'
    h.app.runs.transition(waiting, 'waiting_approval')
    assert h.jobs.tick()['busy'] == 'live_or_waiting_run'
    assert inspect(h, created)['job']['deferred_reason'] == 'live_or_waiting_run'
    assert fires(h, created['id']) == [] and h.app.runs.list(chat['id']) == []
    assert h.app.runs.get(waiting)['state'] == 'waiting_approval' and inbox(h, waiting) == 0
    h.app.runs.transition(waiting, 'stopped')
    model(h.app, 'Status: green.')
    settle(h, fire_at(h, T0 + 185)['run_id'])
    [fire] = fires(h, created['id'])
    assert fire['scheduled'] == T0 + 180 and fire['missed'] == 2


def test_configuration_drift_blocks_until_a_fresh_native_review(env):
    h = env
    chat = h.app.new_chat()
    created = create(h, chat['id'])
    h.app.setting('permissions', {'mode': 'full', 'tools': dict(DEFAULT['tools'])})
    result = fire_at(h, T0 + 61)
    assert result['launched'] is None and result['blocked'] == [created['id']]
    current = inspect(h, created)
    assert current['job']['state'] == 'needs_review' and current['drift'] == ['permissions']
    [fire] = current['fires']
    assert fire['status'] == 'blocked' and fire['run_id'] is None
    [notice] = notices(h)
    assert notice['kind'] == 'action_needed' and notice['body_origin'] == 'host'
    assert h.app.runs.list(chat['id']) == []
    revision = current['job']['revision']
    with pytest.raises(ValueError, match='Review the changed configuration'):
        h.jobs.operation('resume', chat_id=chat['id'], job_id=created['id'], revision=revision)
    with pytest.raises(ValueError, match='Preview the job again'):
        h.jobs.operation('update', chat_id=chat['id'], job_id=created['id'], revision=revision,
                         config_fingerprint=created['config_fingerprint'])
    updated = h.jobs.operation('update', chat_id=chat['id'], job_id=created['id'], revision=revision,
                               config_fingerprint=review(h, chat['id']))
    assert updated['state'] == 'active' and updated['revision'] == revision + 1
    assert updated['config']['permissions']['mode'] == 'full' and updated['next_due'] == iso(T0 + 120)
    model(h.app, 'Status: green.')
    run = settle(h, fire_at(h, T0 + 121)['run_id'])
    assert run['config']['permissions']['mode'] == 'full'
    assert [f['revision'] for f in fires(h, created['id'])] == [1, 2]


def test_a_replaced_project_folder_requires_review(env, tmp_path):
    h = env
    project = tmp_path / 'project'
    project.mkdir()
    chat = h.app.new_chat(str(project))
    created = create(h, chat['id'])
    assert created['config']['workspace_pinned'] is True
    project.rename(tmp_path / 'previous')  # keep the old inode alive so it cannot be reused
    project.mkdir()
    assert fire_at(h, T0 + 61)['blocked'] == [created['id']]
    assert inspect(h, created)['drift'] == ['workspace_identity'] and h.app.runs.list(chat['id']) == []


def test_concurrent_ticks_and_database_constraints_admit_one_fire(env):
    h = env
    chat = h.app.new_chat()
    created = create(h, chat['id'])
    model(h.app, 'Status: green.')
    h.clock.now = T0 + 61
    barrier, results = threading.Barrier(4), []

    def worker():
        barrier.wait()
        results.append(h.jobs.tick())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    launched = [r['run_id'] for r in results if r['launched']]
    assert len(results) == 4 and len(launched) == 1
    settle(h, launched[0])
    [fire] = fires(h, created['id'])
    insert = ('INSERT INTO job_fires(id,job_id,chat_id,revision,scheduled,missed,origin,status,pin_fingerprint,'
              'claimed,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)')

    def row(scheduled, status, origin='host'):
        return (uuid.uuid4().hex, created['id'], chat['id'], 1, scheduled, 0, origin, status,
                created['config_fingerprint'], T0, T0)

    with pytest.raises(sqlite3.IntegrityError):
        h.app.store.query(insert, row(fire['scheduled'], 'completed'))  # same job, slot and revision
    with pytest.raises(sqlite3.IntegrityError):
        h.app.store.query(insert, row(T0 + 999, 'completed', origin='human'))  # fires are host-origin only
    live = row(T0 + 999, 'running')
    h.app.store.query(insert, live)
    with pytest.raises(sqlite3.IntegrityError):
        h.app.store.query(insert, row(T0 + 1999, 'waiting'))  # at most one live fire per job
    h.app.store.query('DELETE FROM job_fires WHERE id=?', (live[0],))
    assert len(h.app.runs.list(chat['id'])) == 1


def test_claim_without_a_bound_run_needs_review_after_restart(tmp_path, monkeypatch):
    home, resources = tmp_path / 'data', tmp_path / 'resources'
    first = harness(home, resources)
    chat = first.app.new_chat()
    created = create(first, chat['id'])
    monkeypatch.setattr(first.jobs, '_bind', crash)
    with pytest.raises(Crash):
        fire_at(first, T0 + 61)
    [fire] = fires(first, created['id'])
    assert fire['status'] == 'claimed' and fire['run_id'] is None
    shutdown(first)
    second = harness(home, resources, now=T0 + 62)
    try:
        [fire] = fires(second, created['id'])
        assert fire['status'] == 'needs_review' and 'not be replayed' in fire['reason']
        [notice] = notices(second)
        assert notice['kind'] == 'action_needed' and notice['status'] == 'needs_review'
        assert second.app.runs.list(chat['id']) == []
        assert fire_at(second, T0 + 119)['launched'] is None
        model(second.app, 'Status: green.')
        settle(second, fire_at(second, T0 + 121)['run_id'])
        assert [(f['scheduled'], f['status']) for f in fires(second, created['id'])] == [
            (T0 + 60, 'needs_review'), (T0 + 120, 'completed')]
    finally:
        shutdown(second)


def test_bound_run_lost_on_restart_needs_attention_and_never_resumes(tmp_path, monkeypatch):
    home, resources = tmp_path / 'data', tmp_path / 'resources'
    first = harness(home, resources)
    chat = first.app.new_chat()
    created = create(first, chat['id'])
    monkeypatch.setattr(first.app.controller, 'launch', crash)
    with pytest.raises(Crash):
        fire_at(first, T0 + 61)
    [fire] = fires(first, created['id'])
    rid = fire['run_id']
    assert fire['status'] == 'bound' and first.app.runs.get(rid)['state'] == 'queued'
    shutdown(first)
    second = harness(home, resources, now=T0 + 62)
    try:
        assert second.app.runs.get(rid)['state'] == 'interrupted'
        [fire] = fires(second, created['id'])
        assert fire['status'] == 'attention' and fire['run_state'] == 'interrupted'
        [notice] = notices(second)
        assert notice['kind'] == 'action_needed' and notice['run_id'] == rid
        result = fire_at(second, T0 + 121)
        assert result['launched'] is None and result['deferred'] == {created['id']: 'previous_fire_needs_attention'}
        assert second.app.runs.get(rid)['state'] == 'interrupted' and inbox(second, rid) == 0
        with pytest.raises(ValueError, match='not found'):
            second.jobs.operation('dismiss_fire', chat_id=chat['id'], job_id=created['id'], fire_id=uuid.uuid4().hex)
        second.jobs.operation('dismiss_fire', chat_id=chat['id'], job_id=created['id'], fire_id=fire['id'])
        model(second.app, 'Status: green.')
        settle(second, second.jobs.tick()['run_id'])
        assert second.app.runs.get(rid)['state'] == 'interrupted'
        assert [f['status'] for f in fires(second, created['id'])] == ['dismissed', 'completed']
    finally:
        shutdown(second)


def test_waiting_scheduled_run_needs_attention_and_cancel_owns_only_its_run(env):
    h = env
    chat, other = h.app.new_chat(), h.app.new_chat()
    asking = create(h, chat['id'], task='Draw a diagram after clarifying the scope')
    later = create(h, other['id'], schedule={'kind': 'interval', 'seconds': 120})
    model(h.app, ask(), 'Status: green.')
    rid = fire_at(h, T0 + 61)['run_id']
    settle(h, rid, 'waiting_input')
    [fire] = fires(h, asking['id'])
    assert fire['status'] == 'waiting' and fire['run_state'] == 'waiting_input'
    assert [n['kind'] for n in notices(h)] == ['action_needed']
    # Nothing answers for the person or grants approval in advance.
    assert h.app.interactions.questions(chat['id']) and inbox(h, rid) == 0
    assert fire_at(h, T0 + 125)['busy'] == 'live_or_waiting_run' and fires(h, later['id']) == []
    current = inspect(h, asking)['job']
    h.jobs.operation('update', chat_id=chat['id'], job_id=asking['id'], revision=current['revision'],
                     task='Different task', config_fingerprint=review(h, chat['id']))
    run = h.app.runs.get(rid)
    assert run['goal'] == 'Draw a diagram after clarifying the scope' and run['state'] == 'waiting_input'
    assert fires(h, asking['id'])[0]['revision'] == 1 and fires(h, asking['id'])[0]['status'] == 'waiting'
    with pytest.raises(ValueError, match='not found'):
        h.jobs.operation('cancel_fire', chat_id=other['id'], job_id=asking['id'], fire_id=fire['id'])
    h.jobs.operation('cancel_fire', chat_id=chat['id'], job_id=asking['id'], fire_id=fire['id'])
    assert h.app.runs.get(rid)['state'] == 'stopped' and fires(h, asking['id'])[0]['status'] == 'cancelled'
    settle(h, h.jobs.tick()['run_id'])
    assert fires(h, later['id'])[0]['status'] == 'completed'


def test_monitor_notifies_only_for_changed_results_and_failures(env):
    h = env
    chat = h.app.new_chat()
    watched = create(h, chat['id'], monitor=True)
    model(h.app, 'Status: green.', 'Status:   green.\n', 'Status: red.', ValueError('provider rejected the request'))
    for minute in (1, 2, 3, 4):
        settle(h, fire_at(h, T0 + 60 * minute + 1)['run_id'], 'failed' if minute == 4 else 'completed')
    rows = fires(h, watched['id'])
    assert [f['status'] for f in rows] == ['completed', 'completed', 'completed', 'failed']
    assert rows[0]['result_fingerprint'] == rows[1]['result_fingerprint'] != rows[2]['result_fingerprint']
    sent = notices(h)
    assert [n['kind'] for n in sent] == ['completed', 'completed', 'failed']
    assert 'result changed' in sent[1]['title'] and sent[1]['body'].startswith('Status: red.')
    assert [n['fire_id'] for n in sent] == [rows[0]['id'], rows[2]['id'], rows[3]['id']]


def test_acknowledgement_is_idempotent_and_rejects_forged_ids(env):
    h = env
    chat, other = h.app.new_chat(), h.app.new_chat()
    create(h, chat['id'])
    h.app.setting('permissions', {'mode': 'auto', 'tools': dict(DEFAULT['tools'])})
    fire_at(h, T0 + 61)
    [notice] = h.jobs.operation('notifications', chat_id=chat['id'])['notifications']
    forged = uuid.uuid4().hex
    for ids in ([forged], [notice['id'], forged], [notice['id'], notice['id']], [], notice['id'], [7]):
        with pytest.raises(ValueError):
            h.jobs.operation('ack', notification_ids=ids)
    with pytest.raises(ValueError):
        h.jobs.operation('ack', notification_ids=[notice['id']], chat_id=other['id'])
    assert h.jobs.operation('notifications', chat_id=chat['id'])['pending'] == 1
    assert h.jobs.operation('ack', notification_ids=[notice['id']]) == {'acknowledged': 1, 'already_acknowledged': 0}
    assert h.jobs.operation('ack', notification_ids=[notice['id']]) == {'acknowledged': 0, 'already_acknowledged': 1}
    assert h.jobs.operation('notifications', chat_id=chat['id'])['notifications'] == []
    [stored] = notices(h, chat_id=chat['id'])
    assert stored['acknowledged'] == iso(T0 + 61)
    h.jobs.refresh()
    h.jobs.recover()
    assert len(notices(h)) == 1  # durable per fire/status/fingerprint; never re-sent


def test_forged_and_cross_conversation_ids_change_nothing(env):
    h = env
    chat, other = h.app.new_chat(), h.app.new_chat()
    mine, theirs = create(h, chat['id']), create(h, other['id'])
    for operation, extra in [('get', {}), ('pause', {'revision': 1}), ('resume', {'revision': 1}),
                             ('delete', {'revision': 1}),
                             ('update', {'revision': 1, 'config_fingerprint': mine['config_fingerprint']})]:
        with pytest.raises(ValueError, match='not found'):
            h.jobs.operation(operation, chat_id=other['id'], job_id=mine['id'], **extra)
    for forged in ('../' + mine['id'], mine['id'].upper(), mine['id'][:-1], 7, None):
        with pytest.raises(ValueError, match='not found'):
            h.jobs.operation('get', chat_id=chat['id'], job_id=forged)
    with pytest.raises(ValueError, match='not found'):
        h.jobs.operation('get', chat_id=uuid.uuid4().hex, job_id=mine['id'])
    h.app.setting('permissions', {'mode': 'auto', 'tools': dict(DEFAULT['tools'])})
    fire_at(h, T0 + 61)
    [their_fire] = fires(h, theirs['id'])
    for operation in ('dismiss_fire', 'cancel_fire'):
        with pytest.raises(ValueError, match='not found'):
            h.jobs.operation(operation, chat_id=chat['id'], job_id=mine['id'], fire_id=their_fire['id'])
    assert inspect(h, mine)['job']['revision'] == 1 and inspect(h, theirs)['job']['state'] == 'needs_review'
    assert fires(h, theirs['id'])[0]['status'] == 'blocked'


def test_pause_resume_and_update_use_revisions_without_backlog(env):
    h = env
    chat = h.app.new_chat()
    created = create(h, chat['id'])
    paused = h.jobs.operation('pause', chat_id=chat['id'], job_id=created['id'], revision=1)
    assert paused['state'] == 'paused' and paused['revision'] == 2 and paused['next_due'] is None
    assert fire_at(h, T0 + 3 * 3600)['launched'] is None and fires(h, created['id']) == []
    for stale in (1, True, 2.0):
        with pytest.raises(ValueError, match='changed since you reviewed'):
            h.jobs.operation('resume', chat_id=chat['id'], job_id=created['id'], revision=stale)
    resumed = h.jobs.operation('resume', chat_id=chat['id'], job_id=created['id'], revision=2)
    assert resumed['revision'] == 3 and resumed['next_due'] == iso(T0 + 3 * 3600 + 60)
    assert h.jobs.tick()['launched'] is None  # nothing replays for the paused period
    updated = h.jobs.operation('update', chat_id=chat['id'], job_id=created['id'], revision=3, label='Renamed',
                               schedule={'kind': 'interval', 'seconds': 300}, config_fingerprint=review(h, chat['id']))
    assert updated['revision'] == 4 and updated['label'] == 'Renamed'
    assert updated['next_due'] == iso(T0 + 3 * 3600 + 300)
    model(h.app, 'Status: green.')
    settle(h, fire_at(h, T0 + 3 * 3600 + 301)['run_id'])
    [fire] = fires(h, created['id'])
    assert fire['revision'] == 4 and fire['scheduled'] == T0 + 3 * 3600 + 300
    assert fire['pin_fingerprint'] == updated['config_fingerprint']
    with pytest.raises(ValueError, match='changed since you reviewed'):
        h.jobs.operation('delete', chat_id=chat['id'], job_id=created['id'], revision=3)
    assert h.jobs.operation('delete', chat_id=chat['id'], job_id=created['id'], revision=4)['deleted'] is True
    with pytest.raises(ValueError, match='not found'):
        inspect(h, created)
    assert fires(h, created['id']) == [] and len(h.app.runs.list(chat['id'])) == 1


def test_close_is_bounded_and_no_fire_starts_after_close(env):
    h = env
    chat = h.app.new_chat()
    created = create(h, chat['id'])
    h.clock.now = T0 + 61
    entered, refresh, results = threading.Event(), h.jobs.refresh, []

    def observed_refresh(*args, **kwargs):
        entered.set()
        return refresh(*args, **kwargs)

    h.jobs.refresh = observed_refresh
    with h.app.task_lock:
        worker = threading.Thread(target=lambda: results.append(h.jobs.tick()))
        worker.start()
        assert entered.wait(2)
        assert h.jobs.close(timeout=0.2) is False  # the pass still holds the scheduler lock
    worker.join(5)
    assert results[0]['launched'] is None and results[0]['busy'] == 'service_closing'
    assert fires(h, created['id']) == [] and h.app.runs.list(chat['id']) == []
    assert h.jobs.close() is True and h.jobs.close() is True
    assert h.jobs.tick()['busy'] == 'service_closing'
    with pytest.raises(ValueError, match='shutting down'):
        create(h, chat['id'])
    h.jobs.start()
    assert h.jobs.thread is None


def test_once_job_fires_once_then_finishes(env):
    h = env
    chat = h.app.new_chat()
    created = create(h, chat['id'], schedule={'kind': 'once', 'at': iso(T0 + 300)})
    assert created['next_due'] == iso(T0 + 300)
    assert fire_at(h, T0 + 299)['launched'] is None
    model(h.app, 'Done once.')
    settle(h, fire_at(h, T0 + 3600)['run_id'])
    current = inspect(h, created)['job']
    assert current['state'] == 'finished' and current['next_due'] is None
    assert fire_at(h, T0 + 86400)['launched'] is None and len(fires(h, created['id'])) == 1
    with pytest.raises(ValueError):
        h.jobs.operation('resume', chat_id=chat['id'], job_id=created['id'], revision=current['revision'])
    with pytest.raises(ValueError, match='no future time'):
        h.jobs.operation('update', chat_id=chat['id'], job_id=created['id'], revision=current['revision'],
                         config_fingerprint=review(h, chat['id']))
    renewed = h.jobs.operation('update', chat_id=chat['id'], job_id=created['id'], revision=current['revision'],
                               schedule={'kind': 'once', 'at': iso(T0 + 90000)}, config_fingerprint=review(h, chat['id']))
    assert renewed['state'] == 'active' and renewed['next_due'] == iso(T0 + 90000)


def test_history_is_bounded_and_prunes_only_settled_acknowledged_rows(env):
    h = env
    chat = h.app.new_chat()
    created = create(h, chat['id'])
    insert = ('INSERT INTO job_fires(id,job_id,chat_id,revision,scheduled,missed,origin,status,pin_fingerprint,'
              'claimed,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)')
    ids = []
    for index in range(MAX_FIRES):
        ids.append(uuid.uuid4().hex)
        h.app.store.query(insert, (ids[-1], created['id'], chat['id'], 1, T0 - 100_000 + index, 0, 'host',
                                   'completed', created['config_fingerprint'], T0, T0))
    h.app.store.query('INSERT INTO job_notifications(id,job_id,fire_id,chat_id,run_id,kind,status,fingerprint,title,body,'
                      'created,acknowledged) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (uuid.uuid4().hex, created['id'], ids[0], chat['id'], None, 'completed', 'completed', 'f' * 64,
                       'Unread', 'Unread result', T0, None))
    model(h.app, 'Status: green.')
    settle(h, fire_at(h, T0 + 61)['run_id'])
    kept = {row['id'] for row in fires(h, created['id'])}
    assert len(kept) == MAX_FIRES and ids[0] in kept and ids[1] not in kept
    # Unsettled review rows are never pruned; the job defers instead of growing.
    h.app.store.query("UPDATE job_fires SET status='needs_review' WHERE job_id=?", (created['id'],))
    result = fire_at(h, T0 + 121)
    assert result['launched'] is None and result['deferred'] == {created['id']: 'history_full'}
    assert inspect(h, created)['job']['deferred_reason'] == 'history_full'
    assert len(fires(h, created['id'])) == MAX_FIRES and len(h.app.runs.list(chat['id'])) == 1


def test_forgetting_a_conversation_removes_only_its_jobs(env):
    h = env
    chat, other = h.app.new_chat(), h.app.new_chat()
    mine, theirs = create(h, chat['id']), create(h, other['id'])
    h.app.setting('permissions', {'mode': 'auto', 'tools': dict(DEFAULT['tools'])})
    fire_at(h, T0 + 61)
    h.jobs.forget(chat['id'])
    assert [job['id'] for job in h.jobs.operation('list')['jobs']] == [theirs['id']]
    assert fires(h, mine['id']) == [] and [n['job_id'] for n in notices(h)] == [theirs['id']]


def test_no_model_tool_reaches_jobs_and_root_states_never_auto_resume():
    from pebble.tool_catalogue import TOOLS
    names = [tool['function']['name'] for tool in TOOLS]
    assert names and not any('job' in name.lower() or 'schedul' in name.lower() for name in names)
    assert {state: run_status(state) for state in (
        'queued', 'running', 'retry_wait', 'waiting_input', 'waiting_approval', 'paused', 'interrupted',
        'needs_reconciliation', 'verification_required', 'budget_exhausted', 'failed', 'stopped', 'completed', None)} == {
        'queued': 'running', 'running': 'running', 'retry_wait': 'running', 'waiting_input': 'waiting',
        'waiting_approval': 'waiting', 'paused': 'attention', 'interrupted': 'attention',
        'needs_reconciliation': 'attention', 'verification_required': 'attention', 'budget_exhausted': 'attention',
        'failed': 'failed', 'stopped': 'cancelled', 'completed': 'completed', None: 'cancelled'}


@needs_zones
def test_daily_job_skips_the_dst_gap_and_runs_the_repeated_hour_once(env):
    h = env
    chat = h.app.new_chat()
    h.clock.now = utc(2026, 3, 7, 12)
    gap = create(h, chat['id'], schedule={'kind': 'daily', 'time': '02:30', 'timezone': 'America/New_York'})
    assert gap['next_due'] == '2026-03-09T06:30:00Z'
    assert fire_at(h, utc(2026, 3, 8, 7, 31))['launched'] is None
    h.jobs.operation('delete', chat_id=chat['id'], job_id=gap['id'], revision=gap['revision'])
    h.clock.now = utc(2026, 10, 31, 12)
    fold = create(h, chat['id'], schedule={'kind': 'daily', 'time': '01:30', 'timezone': 'America/New_York'})
    model(h.app, 'Ran once.')
    settle(h, fire_at(h, utc(2026, 11, 1, 6, 45))['run_id'])  # after both 01:30 wall-clock instants
    assert h.jobs.tick()['launched'] is None
    [fire] = fires(h, fold['id'])
    assert iso(fire['scheduled']) == '2026-11-01T05:30:00Z' and fire['missed'] == 0
    assert inspect(h, fold)['job']['next_due'] == '2026-11-02T06:30:00Z'


def test_incompatible_jobs_schema_is_refused(env):
    h = env
    h.app.store.query('UPDATE job_meta SET version=2')
    try:
        with pytest.raises(ValueError, match='compatible Pebble version'):
            Jobs(h.app)
    finally:
        h.app.store.query('UPDATE job_meta SET version=1')


# Phase 17: the optional catch-up policy rides in the schedule and changes nothing by default.

def test_catch_up_policy_is_validated_and_stored_only_when_it_is_not_the_default(env):
    h = env
    chat = h.app.new_chat()
    skip = {**EVERY_MINUTE, 'catch_up': 'skip_if_late', 'grace_seconds': 600}
    assert create(h, chat['id'], schedule={**EVERY_MINUTE, 'catch_up': 'latest'})['schedule'] == EVERY_MINUTE
    stored = create(h, chat['id'], schedule=skip, label='Skipper')
    assert stored['schedule'] == skip
    assert json.loads(h.app.store.query('SELECT schedule FROM jobs WHERE id=?', (stored['id'],))[0]['schedule']) == skip
    assert h.jobs.operation('preview', chat_id=chat['id'], schedule=skip)['schedule'] == skip
    for bad in [{**EVERY_MINUTE, 'catch_up': 'all'}, {**EVERY_MINUTE, 'grace_seconds': 600},
                {**EVERY_MINUTE, 'catch_up': 'skip_if_late', 'grace_seconds': 600.0},
                {**EVERY_MINUTE, 'catch_up': 'skip_if_late', 'permissions': {}}]:
        with pytest.raises(ValueError):
            create(h, chat['id'], schedule=bad)
    updated = h.jobs.operation('update', chat_id=chat['id'], job_id=stored['id'], revision=1, schedule=EVERY_MINUTE,
                               config_fingerprint=review(h, chat['id']))
    assert updated['schedule'] == EVERY_MINUTE and updated['revision'] == 2
    # Without a lifecycle or outbox attached, a late default-policy backlog still fires once at its latest slot.
    model(h.app, 'Status: green.', 'Status: green.')
    result = fire_at(h, T0 + 3 * 3600 + 5)
    assert result['skipped_late'] == [] and result['launched']
    settle(h, result['run_id'])
    settle(h, h.jobs.tick()['run_id'])
    assert sorted(f['missed'] for f in h.app.store.query('SELECT missed FROM job_fires')) == [179, 179]
