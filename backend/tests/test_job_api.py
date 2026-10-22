"""Native JobAPI routes: epoch DTOs, scoped reads, reviewed re-pins and lock order.

Each test starts the actual App. The parent-wired scheduler, if present, is
closed and replaced by a Jobs instance with an injected clock whose thread is
not started. A fake model counts requests; no provider transport, credential,
shell command, app launch or external notification is reached.
"""

import json
import threading
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import pebble.app as app_module
from pebble.app import App
from pebble.job_api import ROUTES, JobAPI
from pebble.jobs import Jobs, iso
from pebble.permissions import DEFAULT


T0 = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc).timestamp()
EVERY_MINUTE = {'kind': 'interval', 'seconds': 60}
TASK = 'Summarize the project status.'
LIMITS = {'max_steps': 12, 'max_seconds': 300}
PUBLIC = {'job_list', 'job_get', 'job_preview', 'job_create', 'job_pause', 'job_preview_resume', 'job_resume',
          'job_delete', 'notification_ack'}
DTO = {'id', 'name', 'task', 'schedule', 'state', 'revision', 'next_fire', 'error', 'last_run_id', 'last_run_state',
       'monitor', 'config'}


class Crash(BaseException):
    """Simulated process loss; production code must not swallow it."""


def crash(*args, **kwargs):
    raise Crash()


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class TrackedLock:
    """Records wait/held order per thread around the real lock object."""

    def __init__(self, name, inner, log):
        self.name, self.inner, self.log = name, inner, log

    def acquire(self, blocking=True, timeout=-1):
        self.log.append((threading.current_thread().name, self.name, 'wait'))
        held = self.inner.acquire(blocking, timeout)
        if held:
            self.log.append((threading.current_thread().name, self.name, 'held'))
        return held

    def release(self):
        self.inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


@pytest.fixture
def env(tmp_path):
    events, requests = [], []
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(args))
    wired = getattr(app, 'jobs', None)
    if wired is not None:
        assert wired.close()  # the parent-wired scheduler; these tests drive their own clock
    clock = Clock(T0)
    app.jobs = Jobs(app, clock=clock, poll=300)
    app.jobs.start(thread=False)

    def complete(messages, tools, cancel, token, effort):
        requests.append(messages)
        token('Status: green.')
        return {'role': 'assistant', 'content': 'Status: green.'}

    app.model.complete = complete
    api = JobAPI(app)
    yield SimpleNamespace(app=app, jobs=app.jobs, api=api, clock=clock, events=events, requests=requests,
                          call=lambda route, **params: api.methods[route](**params))
    app.jobs.close()
    app.close()


def preview(h, chat_id, schedule=EVERY_MINUTE, limits=LIMITS, **extra):
    return h.call('job_preview', id=chat_id, schedule=schedule, limits=limits, **extra)


def create(h, chat_id, schedule=EVERY_MINUTE, name='Status check', task=TASK, monitor=False, limits=LIMITS):
    reviewed = preview(h, chat_id, schedule, limits)
    return h.call('job_create', id=chat_id, name=name, task=task, schedule=schedule, monitor=monitor, limits=limits,
                  config_fingerprint=reviewed['config_fingerprint'])


def listed(h, chat_id):
    return h.call('job_list', id=chat_id)


def access(h, mode):
    h.app.setting('permissions', {'mode': mode, 'tools': dict(DEFAULT['tools'])})


def settle(h, rid, state='completed'):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        worker = h.app.worker
        if h.app.runs.get(rid)['state'] == state and h.app.active is None and not (worker and worker.is_alive()):
            h.jobs.refresh()
            return
        time.sleep(0.01)
    pytest.fail(f'Run did not settle as {state}: {h.app.runs.get(rid)["state"]}')


def wait_until(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def try_acquire(lock, out, timeout):
    held = lock.acquire(timeout=timeout)
    out.append(held)
    if held:
        lock.release()


def test_routes_are_exactly_the_native_methods_without_a_generic_operation(env):
    assert set(env.api.methods) == PUBLIC == set(ROUTES)
    assert 'job_operation' not in env.api.methods and all(callable(m) for m in env.api.methods.values())
    from pebble.tool_catalogue import TOOLS
    names = [tool['function']['name'].lower() for tool in TOOLS]
    assert names and not any('job' in name or 'notification' in name for name in names)
    with pytest.raises(ValueError, match='Unknown request'):
        env.app.dispatch('job_operation', {'operation': 'list'})


def test_authority_unknown_and_missing_fields_are_refused_before_any_change(env):
    chat = env.app.new_chat()
    reviewed = preview(env, chat['id'])
    base = dict(id=chat['id'], name='Status', task=TASK, schedule=EVERY_MINUTE, monitor=False, limits=LIMITS,
                config_fingerprint=reviewed['config_fingerprint'])
    for field, value in [('origin', 'human'), ('config', {}), ('policy', {'mode': 'full'}), ('grants', ['terminal']),
                         ('permissions', DEFAULT), ('workspace', '/'), ('run_id', 'a' * 32), ('auto_approve', True)]:
        with pytest.raises(ValueError, match='computed by the host'):
            env.call('job_create', **base, **{field: value})
    for field, value in [('chat_id', chat['id']), ('label', 'Status'), ('job_operation', 'create'), ('fire_id', 'a' * 32)]:
        with pytest.raises(ValueError, match='accepts'):
            env.call('job_create', **base, **{field: value})
    for missing in ('monitor', 'limits', 'config_fingerprint', 'id'):
        with pytest.raises(ValueError, match='accepts'):
            env.call('job_create', **{k: v for k, v in base.items() if k != missing})
    for route, params in [('job_list', {'id': chat['id'], 'policy': {}}),
                          ('job_preview', {'id': chat['id'], 'schedule': EVERY_MINUTE, 'limits': None, 'grants': []}),
                          ('notification_ack', {'id': chat['id'], 'notification_id': 'a' * 32, 'origin': 'host'})]:
        with pytest.raises(ValueError, match='computed by the host'):
            env.call(route, **params)
    for field, value in [('monitor', 1), ('limits', {'max_steps': True}), ('name', ''), ('task', {'goal': TASK})]:
        with pytest.raises(ValueError):
            env.call('job_create', **{**base, field: value})
    assert listed(env, chat['id'])['jobs'] == [] and env.requests == []


def test_once_schedules_convert_between_epoch_dtos_and_core_utc_iso(env):
    chat = env.app.new_chat()
    once = {'kind': 'once', 'at': int(T0) + 300}
    reviewed = preview(env, chat['id'], once)
    assert set(reviewed) == {'config_fingerprint', 'config', 'schedule', 'next_times'}
    assert reviewed['schedule'] == once and reviewed['next_times'] == [int(T0) + 300]
    job = create(env, chat['id'], once)
    assert set(job) == DTO and job['schedule'] == once and job['next_fire'] == int(T0) + 300
    assert job['state'] == 'active' and job['name'] == 'Status check' and job['revision'] == 1
    assert job['last_run_id'] is None and job['last_run_state'] is None and job['error'] is None
    assert job['config']['limits'] == {'max_steps': 12, 'max_seconds': 300.0} and job['monitor'] is False
    stored = json.loads(env.app.store.query('SELECT schedule FROM jobs WHERE id=?', (job['id'],))[0]['schedule'])
    assert stored == {'kind': 'once', 'at': iso(T0 + 300)}
    assert preview(env, chat['id'], {'kind': 'once', 'at': T0 + 600.0})['schedule'] == {'kind': 'once', 'at': int(T0) + 600}
    for at in (True, False, float('nan'), float('inf'), T0 + 300.5, iso(T0 + 300), None, -1, 10 ** 20, [T0]):
        with pytest.raises(ValueError, match='whole UTC epoch seconds'):
            preview(env, chat['id'], {'kind': 'once', 'at': at})
    for at in (int(T0) - 60, int(T0) + 400 * 86400):
        with pytest.raises(ValueError, match='future UTC time'):
            preview(env, chat['id'], {'kind': 'once', 'at': at})
    for schedule in ({'kind': 'once', 'at': int(T0) + 300, 'timezone': 'UTC'}, {'kind': 'interval', 'seconds': True},
                     {'kind': 'cron', 'expr': '* * * * *'}, [once]):
        with pytest.raises(ValueError):
            preview(env, chat['id'], schedule)
    interval = preview(env, chat['id'], EVERY_MINUTE)
    assert interval['schedule'] == EVERY_MINUTE and interval['next_times'][:2] == [int(T0) + 60, int(T0) + 120]
    assert all(type(value) is int for value in interval['next_times'])


def test_create_refuses_a_preview_after_permission_model_or_workspace_change(env, tmp_path, monkeypatch):
    chat = env.app.new_chat()

    def refused(change):
        reviewed = preview(env, chat['id'])
        change()
        with pytest.raises(ValueError, match='Preview the job again'):
            env.call('job_create', id=chat['id'], name='Status', task=TASK, schedule=EVERY_MINUTE, monitor=False,
                     limits=LIMITS, config_fingerprint=reviewed['config_fingerprint'])

    refused(lambda: access(env, 'auto'))
    profile = {'id': 'fixture-cloud', 'kind': 'anthropic', 'url': 'https://provider.invalid/v1',
               'model': 'fixture-model', 'context': 32000, 'budget': 32000, 'effort': 'default'}
    refused(lambda: monkeypatch.setattr(env.app.model, 'current', lambda: dict(profile)))
    project = tmp_path / 'project'
    project.mkdir()
    refused(lambda: env.app.set_workspace(chat['id'], str(project)))
    assert listed(env, chat['id'])['jobs'] == []
    job = create(env, chat['id'])
    assert job['config']['permissions']['mode'] == 'auto' and job['config']['model']['id'] == 'fixture-cloud'
    assert job['config']['workspace'] == str(project.resolve()) and job['config']['workspace_pinned'] is True
    assert env.requests == [] and env.app.runs.list(chat['id']) == []


def test_resume_is_revision_bound_and_repins_freshly_reviewed_native_config(env):
    chat = env.app.new_chat()
    original = preview(env, chat['id'])['config_fingerprint']
    job = create(env, chat['id'])
    access(env, 'full')
    env.clock.now = T0 + 61
    assert env.jobs.tick()['blocked'] == [job['id']]
    [current] = listed(env, chat['id'])['jobs']
    assert current['state'] == 'needs_review' and 'permissions' in current['error'] and current['next_fire'] is None
    revision = current['revision']
    for stale in (revision + 1, revision - 1, True, float(revision), None):
        with pytest.raises(ValueError, match='changed since you reviewed'):
            env.call('job_preview_resume', id=chat['id'], job_id=job['id'], revision=stale)
    reviewed = env.call('job_preview_resume', id=chat['id'], job_id=job['id'], revision=revision)
    assert set(reviewed) == {'config_fingerprint', 'config'} and reviewed['config_fingerprint'] != original
    assert reviewed['config']['permissions']['mode'] == 'full' and reviewed['config']['limits'] == job['config']['limits']
    with pytest.raises(ValueError, match='Preview the job again'):
        env.call('job_resume', id=chat['id'], job_id=job['id'], revision=revision, config_fingerprint=original)
    with pytest.raises(ValueError, match='changed since you reviewed'):
        env.call('job_resume', id=chat['id'], job_id=job['id'], revision=revision + 1,
                 config_fingerprint=reviewed['config_fingerprint'])
    resumed = env.call('job_resume', id=chat['id'], job_id=job['id'], revision=revision,
                       config_fingerprint=reviewed['config_fingerprint'])
    assert resumed['state'] == 'active' and resumed['revision'] == revision + 1
    assert resumed['config']['permissions']['mode'] == 'full' and resumed['next_fire'] == int(T0) + 120
    assert resumed['error'] is None  # the reviewed drift no longer reads as an error
    with pytest.raises(ValueError, match='Only a paused job'):
        env.call('job_preview_resume', id=chat['id'], job_id=job['id'], revision=resumed['revision'])
    with pytest.raises(ValueError, match='Only a paused job'):
        env.call('job_resume', id=chat['id'], job_id=job['id'], revision=resumed['revision'],
                 config_fingerprint=reviewed['config_fingerprint'])
    # A paused job stays paused through the core re-pin, then resumes at the new revision.
    paused = env.call('job_pause', id=chat['id'], job_id=job['id'], revision=resumed['revision'])
    assert paused['state'] == 'paused' and paused['next_fire'] is None
    access(env, 'ask')
    fresh = env.call('job_preview_resume', id=chat['id'], job_id=job['id'], revision=paused['revision'])
    assert fresh['config']['permissions']['mode'] == 'ask' and fresh['config_fingerprint'] != reviewed['config_fingerprint']
    again = env.call('job_resume', id=chat['id'], job_id=job['id'], revision=paused['revision'],
                     config_fingerprint=fresh['config_fingerprint'])
    assert again['state'] == 'active' and again['revision'] == paused['revision'] + 2
    pin = json.loads(env.app.store.query('SELECT pin FROM jobs WHERE id=?', (job['id'],))[0]['pin'])
    assert pin['config']['permissions']['mode'] == 'ask' and pin['config']['limits'] == job['config']['limits']
    assert env.requests == [] and env.app.runs.list(chat['id']) == []


def test_resume_schedules_future_fires_without_touching_an_unresolved_run(env, monkeypatch):
    chat = env.app.new_chat()
    job = create(env, chat['id'])
    monkeypatch.setattr(env.app.controller, 'launch', crash)
    env.clock.now = T0 + 61
    with pytest.raises(Crash):
        env.jobs.tick()
    monkeypatch.undo()
    rid = env.app.store.query('SELECT run_id FROM job_fires WHERE job_id=?', (job['id'],))[0]['run_id']
    env.app.runs.transition(rid, 'interrupted')  # what restart recovery records for a bound, unlaunched run
    before = env.app.runs.get(rid)
    [current] = listed(env, chat['id'])['jobs']
    assert current['last_run_id'] == rid and current['last_run_state'] == 'interrupted'
    assert 'interrupted' in current['error']
    paused = env.call('job_pause', id=chat['id'], job_id=job['id'], revision=current['revision'])
    reviewed = env.call('job_preview_resume', id=chat['id'], job_id=job['id'], revision=paused['revision'])
    resumed = env.call('job_resume', id=chat['id'], job_id=job['id'], revision=paused['revision'],
                       config_fingerprint=reviewed['config_fingerprint'])
    assert resumed['state'] == 'active' and resumed['last_run_id'] == rid
    after = env.app.runs.get(rid)
    assert after['state'] == 'interrupted' and after['config'] == before['config']
    assert (after['steps'], after['elapsed']) == (before['steps'], before['elapsed'])
    env.clock.now = T0 + 181
    result = env.jobs.tick()
    assert result['launched'] is None and result['deferred'] == {job['id']: 'previous_fire_needs_attention'}
    assert len(env.app.runs.list(chat['id'])) == 1 and env.requests == []


def test_list_is_scoped_and_reports_the_last_settled_run(env):
    chat, other = env.app.new_chat(), env.app.new_chat()
    mine = create(env, chat['id'], {'kind': 'once', 'at': int(T0) + 300})
    theirs = create(env, other['id'], {'kind': 'interval', 'seconds': 3600})
    env.clock.now = T0 + 301
    rid = env.jobs.tick()['run_id']
    settle(env, rid)
    view = listed(env, chat['id'])
    [job] = view['jobs']
    assert job['id'] == mine['id'] and job['state'] == 'completed' and job['next_fire'] is None
    assert job['last_run_id'] == rid and job['last_run_state'] == 'completed' and job['error'] is None
    [notice] = view['notifications']
    assert notice['kind'] == 'completed' and notice['run_id'] == rid and notice['body_origin'] == 'run_output'
    assert notice['created'] == int(T0) + 301 and type(notice['created']) is int
    assert view['service'] == {'running': False, 'execution': 'while_service_running', 'last_error': None}
    others = listed(env, other['id'])
    assert [j['id'] for j in others['jobs']] == [theirs['id']] and others['notifications'] == []
    assert others['jobs'][0]['last_run_id'] is None
    assert len(env.requests) == 1
    listed(env, chat['id'])
    listed(env, other['id'])
    assert len(env.requests) == 1 and len(env.app.runs.list(chat['id'])) == 1  # list never dispatches inference
    with pytest.raises(ValueError, match='Only a paused job'):
        env.call('job_preview_resume', id=chat['id'], job_id=mine['id'], revision=job['revision'])
    for chat_id in (uuid.uuid4().hex, 'not-a-chat', None, 7):
        with pytest.raises(ValueError, match='Conversation not found'):
            env.call('job_list', id=chat_id)
    with pytest.raises(ValueError, match='not found'):
        env.call('job_get', id=other['id'], job_id=mine['id'])
    audit = env.call('job_get', id=chat['id'], job_id=mine['id'])
    assert audit['job'] == job and audit['drift'] == []
    assert audit['fires'][0]['run_id'] == rid and audit['fires'][0]['scheduled'] == int(T0) + 300


def test_notification_ack_is_scoped_and_idempotent(env):
    chat, other = env.app.new_chat(), env.app.new_chat()
    create(env, chat['id'])
    access(env, 'auto')
    env.clock.now = T0 + 61
    env.jobs.tick()
    [notice] = listed(env, chat['id'])['notifications']
    assert notice['kind'] == 'action_needed' and notice['created'] == int(T0) + 61 and notice['body_origin'] == 'host'
    for chat_id, forged in [(other['id'], notice['id']), (chat['id'], uuid.uuid4().hex),
                            (chat['id'], notice['id'].upper()), (chat['id'], [notice['id']])]:
        with pytest.raises(ValueError, match='not found'):
            env.call('notification_ack', id=chat_id, notification_id=forged)
    assert len(listed(env, chat['id'])['notifications']) == 1
    acked = {'acknowledged': 1, 'already_acknowledged': 0}
    assert env.call('notification_ack', id=chat['id'], notification_id=notice['id']) == acked
    again = {'acknowledged': 0, 'already_acknowledged': 1}
    assert env.call('notification_ack', id=chat['id'], notification_id=notice['id']) == again
    assert listed(env, chat['id'])['notifications'] == []


def test_routes_add_no_human_input_model_request_or_effect(env, monkeypatch):
    def forbid(name):
        def refuse(*args, **kwargs):
            raise AssertionError(name + ' must not run from a job route.')
        return refuse

    for name in ('run_command', 'launch_app', 'run_tests', 'fetch_url', 'search', 'weather'):
        monkeypatch.setattr(app_module, name, forbid(name))
    from pebble import sources  # Phase 16: the ledger's hardened fetch is a network path too
    monkeypatch.setattr(sources, 'fetch_document', forbid('fetch_document'))
    chat = env.app.new_chat()
    memories = env.app.store.query('SELECT count(*) AS n FROM memories')[0]['n']
    start = len(env.events)
    job = create(env, chat['id'])
    paused = env.call('job_pause', id=chat['id'], job_id=job['id'], revision=job['revision'])
    reviewed = env.call('job_preview_resume', id=chat['id'], job_id=job['id'], revision=paused['revision'])
    resumed = env.call('job_resume', id=chat['id'], job_id=job['id'], revision=paused['revision'],
                       config_fingerprint=reviewed['config_fingerprint'])
    listed(env, chat['id'])
    env.call('job_get', id=chat['id'], job_id=job['id'])
    deleted = env.call('job_delete', id=chat['id'], job_id=job['id'], revision=resumed['revision'])
    assert deleted == {'job_id': job['id'], 'deleted': True}
    assert env.requests == [] and env.app.chat(chat['id'])['messages'] == []
    assert env.app.store.actions(chat['id']) == [] and env.app.runs.list() == []
    assert env.app.store.query('SELECT count(*) AS n FROM run_inbox')[0]['n'] == 0
    assert env.app.store.query('SELECT count(*) AS n FROM memories')[0]['n'] == memories
    assert not any(args[0] in {'run', 'token', 'complete', 'progress', 'question'} for args in env.events[start:])


def test_routes_take_jobs_lock_before_task_lock_and_wait_for_foreground_admission(env, monkeypatch):
    chat = env.app.new_chat()
    create(env, chat['id'], {'kind': 'interval', 'seconds': 3600})
    log, results = [], {}
    monkeypatch.setattr(env.jobs, 'lock', TrackedLock('jobs', env.jobs.lock, log))
    monkeypatch.setattr(env.app, 'task_lock', TrackedLock('task', env.app.task_lock, log))

    def route(key):
        results[key] = preview(env, chat['id'])

    # Foreground admission owns the task lock: the route already holds Jobs.lock and waits.
    with env.app.task_lock:
        worker = threading.Thread(target=route, args=('first',), name='route-first')
        worker.start()
        assert wait_until(lambda: ('route-first', 'task', 'wait') in log)
        probe = []
        prober = threading.Thread(target=try_acquire, args=(env.jobs.lock, probe, 0.1), name='scheduler-probe')
        prober.start()
        prober.join(2)
        assert probe == [False] and 'first' not in results  # a scheduler pass queues behind the route
    worker.join(5)
    assert not worker.is_alive() and results['first']['config_fingerprint']
    order = [(lock, step) for thread, lock, step in log if thread == 'route-first']
    assert order[:4] == [('jobs', 'wait'), ('jobs', 'held'), ('task', 'wait'), ('task', 'held')]

    # A scheduler pass owns Jobs.lock: the waiting route holds no task lock.
    with env.jobs.lock:
        worker = threading.Thread(target=route, args=('second',), name='route-second')
        worker.start()
        assert wait_until(lambda: ('route-second', 'jobs', 'wait') in log)
        free = []
        foreground = threading.Thread(target=try_acquire, args=(env.app.task_lock, free, 0.5), name='foreground')
        foreground.start()
        foreground.join(2)
        assert free == [True] and 'second' not in results
    worker.join(5)
    assert not worker.is_alive() and results['second']['config_fingerprint'] == results['first']['config_fingerprint']
    assert not any(thread == 'route-second' and lock == 'task' and step == 'held'
                   for thread, lock, step in log[:log.index(('route-second', 'jobs', 'held'))])

    # Mixed routes and scheduler passes finish without deadlock.
    barrier, errors = threading.Barrier(4), []

    def repeat(action):
        try:
            barrier.wait(5)
            for _ in range(5):
                action()
        except Exception as exc:
            errors.append(exc)

    actions = (lambda: listed(env, chat['id']), env.jobs.tick, lambda: preview(env, chat['id']), env.jobs.refresh)
    threads = [threading.Thread(target=repeat, args=(action,)) for action in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not any(thread.is_alive() for thread in threads) and errors == []
    assert env.requests == []


def test_pause_and_delete_are_revision_bound_and_scoped(env):
    chat, other = env.app.new_chat(), env.app.new_chat()
    job = create(env, chat['id'])
    for revision in (2, 0, True, '1', 1.0):
        with pytest.raises(ValueError, match='changed since you reviewed'):
            env.call('job_pause', id=chat['id'], job_id=job['id'], revision=revision)
    with pytest.raises(ValueError, match='not found'):
        env.call('job_pause', id=other['id'], job_id=job['id'], revision=1)
    paused = env.call('job_pause', id=chat['id'], job_id=job['id'], revision=1)
    assert paused['state'] == 'paused' and paused['revision'] == 2 and paused['next_fire'] is None
    with pytest.raises(ValueError, match='changed since you reviewed'):
        env.call('job_delete', id=chat['id'], job_id=job['id'], revision=1)
    with pytest.raises(ValueError, match='not found'):
        env.call('job_delete', id=other['id'], job_id=job['id'], revision=2)
    assert env.call('job_delete', id=chat['id'], job_id=job['id'], revision=2) == {'job_id': job['id'], 'deleted': True}
    assert listed(env, chat['id'])['jobs'] == []


# Phase 17: the optional catch-up keys pass through the epoch DTOs unchanged; the DTO shape does not grow.

def test_catch_up_keys_round_trip_through_the_epoch_dtos(env):
    chat = env.app.new_chat()
    policy = {'catch_up': 'skip_if_late', 'grace_seconds': 3600}
    once = {'kind': 'once', 'at': int(T0) + 300, **policy}
    reviewed = preview(env, chat['id'], once)
    assert reviewed['schedule'] == once and reviewed['next_times'] == [int(T0) + 300]
    job = create(env, chat['id'], once)
    assert set(job) == DTO and job['schedule'] == once
    daily = {'kind': 'daily', 'time': '09:00', 'timezone': 'Asia/Kolkata', **policy}
    assert create(env, chat['id'], daily, name='Standup')['schedule'] == daily
    assert create(env, chat['id'], {**EVERY_MINUTE, 'catch_up': 'latest'}, name='Plain')['schedule'] == EVERY_MINUTE
    for schedule in ({**once, 'timezone': 'UTC'}, {**EVERY_MINUTE, 'catch_up': 'skip_if_late', 'grace_seconds': 30},
                     {**EVERY_MINUTE, 'grace_seconds': 600}, {**EVERY_MINUTE, 'catch_up': 'replay'}):
        with pytest.raises(ValueError):
            preview(env, chat['id'], schedule)
    assert sorted(j['name'] for j in listed(env, chat['id'])['jobs']) == ['Plain', 'Standup', 'Status check']
    assert 'notifications' in listed(env, chat['id']) and env.requests == []
