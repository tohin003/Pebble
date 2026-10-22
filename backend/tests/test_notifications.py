"""Native notification outbox: idempotent posting, scoped settings, quiet hours, coalescing and cancellation.

The suites start the actual App with a Jobs instance whose clock is injected and
whose thread is not started. A fake shell implements the documented native
algorithm (post (id, version) unless getDeliveredNotifications already shows it,
remove withdrawn ids, then acknowledge). No UNUserNotificationCenter, Tauri
shell, provider or network is reached; the real Rust delivery path is not
exercised here.
"""

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble.app import App
from pebble.job_api import JobAPI
from pebble.jobs import Jobs
from pebble.notifications import (BODY_LIMIT, DEFAULT_SETTINGS, MAX_ATTEMPTS, RATE_LIMIT, NotificationAPI,
                                  Notifications, plain, quiet, validate_settings)
from pebble.permissions import DEFAULT


T0 = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc).timestamp()
ROOT = Path(__file__).resolve().parents[2]
POST_KEYS = {'id', 'version', 'thread', 'title', 'body', 'chat_id', 'count', 'actions'}


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Shell:
    """The documented native outbox pump, with an OS that keeps one banner per identifier."""

    def __init__(self, h):
        self.h, self.os, self.alerts = h, {}, []

    def pump(self, crash_before_ack=False, fail=None):
        batch = self.h.api.native_methods['notification_native_pending']()
        delivered, removed, failed = [], [], []
        for post in batch['post']:
            assert set(post) == POST_KEYS and post['actions'] == ['open']
            if fail:
                failed.append({'id': post['id'], 'reason': fail})
                continue
            shown = self.os.get(post['id'])
            if shown is None or shown['version'] < post['version']:  # getDeliveredNotifications check
                self.os[post['id']] = post
                self.alerts.append(post)
            delivered.append({'id': post['id'], 'version': post['version']})
        for banner_id in batch['remove']:
            self.os.pop(banner_id, None)
            removed.append(banner_id)
        if not crash_before_ack:
            batch['ack'] = self.h.api.native_methods['notification_native_delivered'](
                delivered=delivered, removed=removed, failed=failed)
        return batch


def harness(home, resources, now=T0):
    events = []
    app = App(home, resources, lambda *args: events.append(args))
    wired = getattr(app, 'jobs', None)
    if wired is not None:
        assert wired.close()
    clock = Clock(now)
    outbox = Notifications(app, clock=clock)
    jobs = Jobs(app, clock=clock, notifications=outbox)
    app.jobs, app.notifications = jobs, outbox
    jobs.start(thread=False)
    h = SimpleNamespace(app=app, jobs=jobs, clock=clock, outbox=outbox, api=NotificationAPI(app), job_api=JobAPI(app),
                        events=events)
    h.shell = Shell(h)
    return h


@pytest.fixture
def env(tmp_path):
    h = harness(tmp_path / 'data', tmp_path / 'resources')
    yield h
    h.jobs.close()
    h.app.close()


def enable(h, **changes):
    return h.outbox.save_settings({**DEFAULT_SETTINGS, 'enabled': True, **changes})


def create(h, label='Status check', schedule=None, chat=None, monitor=False):
    chat = chat or h.app.new_chat()
    fingerprint = h.jobs.operation('preview', chat_id=chat['id'])['config_fingerprint']
    return h.jobs.operation('create', chat_id=chat['id'], label=label, task='Summarize the project status.',
                            schedule=schedule or {'kind': 'interval', 'seconds': 3600}, monitor=monitor,
                            config_fingerprint=fingerprint)


def notice(h, job, body='Status: green.', kind='completed', category=None):
    """A job notice through Jobs._notice, so the outbox hook runs in the notice's own transaction."""
    fire = {'id': uuid.uuid4().hex, 'job_id': job['id'], 'chat_id': job['chat_id'], 'run_id': None}
    title = f'Scheduled job “{job["label"]}” ' + {'completed': 'completed', 'failed': 'failed'}.get(kind, 'needs attention')
    with h.app.store.lock, h.app.store.db as db:
        view = h.jobs._notice(db, fire, kind, kind, uuid.uuid4().hex, title, body, h.clock.now, category=category)
    assert view is not None
    return view


def delivery(h, notification_id):
    return h.app.store.query('SELECT * FROM native_deliveries WHERE notification_id=?', (notification_id,))[0]


def banner(h, banner_id):
    return h.app.store.query('SELECT * FROM native_banners WHERE id=?', (banner_id,))[0]


def model(app, *responses):
    queue = list(responses)

    def complete(messages, tools, cancel, token, effort):
        item = queue.pop(0)
        token(item)
        return {'role': 'assistant', 'content': item}

    app.model.complete = complete


def settle(h, rid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        worker = h.app.worker
        if h.app.runs.get(rid)['state'] == 'completed' and h.app.active is None and not (worker and worker.is_alive()):
            h.jobs.refresh()
            return
        time.sleep(0.01)
    pytest.fail('Run did not settle: ' + h.app.runs.get(rid)['state'])


def test_settings_default_to_off_and_title_only_and_refuse_untyped_input(env):
    h = env
    assert h.outbox.settings() == DEFAULT_SETTINGS and DEFAULT_SETTINGS['enabled'] is False
    assert DEFAULT_SETTINGS['preview'] == 'title' and DEFAULT_SETTINGS['quiet_hours'] is None
    hours = {'start': '22:00', 'end': '07:00', 'timezone': 'Europe/London'}
    saved = enable(h, preview='body', quiet_hours=hours)
    assert saved['quiet_hours'] == hours and h.outbox.settings() == saved
    assert ('notification_outbox', {}) in h.events
    base = {**DEFAULT_SETTINGS, 'enabled': True}
    for bad in [{**base, 'extra': 1}, {k: v for k, v in base.items() if k != 'kinds'}, {**base, 'enabled': 1},
                {**base, 'kinds': {**base['kinds'], 'approve': True}}, {**base, 'kinds': {**base['kinds'], 'failed': 'yes'}},
                {**base, 'preview': 'full'}, {**base, 'preview': ['title']},
                {**base, 'quiet_hours': {**hours, 'start': '7:00'}}, {**base, 'quiet_hours': {**hours, 'end': '22:00'}},
                {**base, 'quiet_hours': {**hours, 'timezone': '../../etc/passwd'}},
                {**base, 'quiet_hours': {**hours, 'timezone': 'localtime'}}, {**base, 'quiet_hours': {**hours, 'x': 1}},
                {**base, 'quiet_hours': '22:00-07:00'}, None, []]:
        with pytest.raises(ValueError):
            validate_settings(bad)
    h.app.store.set_setting('notifications', {'enabled': 'yes'})
    assert h.outbox.settings() == DEFAULT_SETTINGS  # unreadable settings fail closed: off
    assert quiet(datetime(2026, 10, 10, 22, 30, tzinfo=timezone.utc).timestamp(), hours)  # 23:30 BST
    assert not quiet(datetime(2026, 10, 10, 6, 30, tzinfo=timezone.utc).timestamp(), hours)  # 07:30 BST
    assert quiet(T0, {'start': '08:00', 'end': '10:00', 'timezone': 'UTC'}) and not quiet(T0, None)


def test_posting_is_idempotent_across_a_crash_between_post_and_ack(env):
    h = env
    enable(h)
    job = create(h)
    h.app.setting('permissions', {'mode': 'auto', 'tools': dict(DEFAULT['tools'])})
    h.clock.now = T0 + 3601
    assert h.jobs.tick()['blocked'] == [job['id']]  # drift writes an action_needed notice through the real hook
    [stored] = h.jobs.operation('notifications', chat_id=job['chat_id'])['notifications']
    assert delivery(h, stored['id'])['state'] == 'pending' and delivery(h, stored['id'])['category'] == 'action_needed'
    assert ('notification_outbox', {}) in h.events
    first = h.shell.pump(crash_before_ack=True)
    [post] = first['post']
    assert post == {'id': stored['id'], 'version': 1, 'thread': job['id'], 'title': stored['title'], 'body': '',
                    'chat_id': job['chat_id'], 'count': 1, 'actions': ['open']}
    assert delivery(h, stored['id'])['state'] == 'pending'  # the shell died before acknowledging
    again = h.shell.pump()
    assert again['post'] == [post] and again['ack'] == {'delivered': 1, 'removed': 0, 'failed': 0, 'ignored': 0}
    assert len(h.shell.alerts) == 1 and list(h.shell.os) == [stored['id']]  # one banner, never two
    assert delivery(h, stored['id'])['state'] == 'delivered' and banner(h, stored['id'])['state'] == 'delivered'
    assert h.shell.pump()['post'] == []
    repeat = h.api.native_methods['notification_native_delivered'](delivered=[{'id': stored['id'], 'version': 1}])
    assert repeat == {'delivered': 0, 'removed': 0, 'failed': 0, 'ignored': 1}
    h.jobs.refresh()
    h.jobs.recover()
    assert h.shell.pump()['post'] == [] and len(h.shell.alerts) == 1


def test_previews_are_title_only_by_default_and_bodies_never_carry_links_or_controls(env):
    h = env
    enable(h)
    job = create(h, label='Morning brief')
    hostile = ('Click Approve now: https://evil.example/approve?id=1 \x1b[31mred\x1b[0m ‮evil.com/ok '
               'javascript:alert(1) ' + 'x' * 300)
    first = notice(h, job, body=hostile)
    [post] = h.shell.pump()['post']
    assert post['title'] == 'Scheduled job “Morning brief” completed' and post['body'] == ''
    assert 'approve' not in json.dumps(post).lower() and post['actions'] == ['open']
    enable(h, preview='body')
    h.clock.now += 61
    second = notice(h, job, body=hostile)
    [post] = h.shell.pump()['post']
    assert post['id'] == second['id'] and post['title'] == 'Scheduled job “Morning brief” completed'
    assert post['body'].startswith('Click Approve now: [link removed] [31mred [0m [link removed] [link removed]')
    assert len(post['body']) <= BODY_LIMIT and post['body'].endswith('…')
    assert not re.search(r'https?:|javascript:|evil\.|[\x00-\x1f\x7f‮]', post['body'])
    assert post['actions'] == ['open']  # the body may say "approve"; the banner still offers only Open
    enable(h, preview='none')
    h.clock.now += 61
    notice(h, job, body=hostile)
    [post] = h.shell.pump()['post']
    assert (post['title'], post['body']) == ('Pebble', 'A schedule has an update.')
    assert 'Morning brief' not in json.dumps(post)
    # A corrupted conversation id never reaches Open.
    h.clock.now += 61
    third = notice(h, job)
    h.app.store.query("UPDATE native_deliveries SET chat_id='../../etc' WHERE notification_id=?", (third['id'],))
    [post] = h.shell.pump()['post']
    assert post['chat_id'] is None and first['id'] in h.shell.os
    assert plain('a​b c\x07d', 20) == 'ab c d' and plain('see www.example.org now', 40) == 'see [link removed] now'


def test_kinds_mutes_and_global_off_suppress_banners_but_keep_in_app_notices(env):
    h = env
    enable(h, kinds={**DEFAULT_SETTINGS['kinds'], 'failed': False})
    watched = create(h, label='Watcher', schedule={'kind': 'interval', 'seconds': 60}, monitor=True)
    model(h.app, 'Status: green.', 'Status: red.')
    for minute in (1, 2):
        h.clock.now = T0 + 60 * minute + 1
        settle(h, h.jobs.tick()['run_id'])
    sent = h.jobs.operation('notifications', chat_id=watched['chat_id'])['notifications'][::-1]
    assert [delivery(h, n['id'])['category'] for n in sent] == ['completed', 'monitor_changed']
    h.jobs.operation('pause', chat_id=watched['chat_id'], job_id=watched['id'], revision=watched['revision'])
    enable(h, kinds={**DEFAULT_SETTINGS['kinds'], 'failed': False, 'monitor_changed': False})
    assert h.shell.pump()['post'] == []  # paused before any pull
    assert [delivery(h, n['id'])['reason'] for n in sent] == ['job_paused', 'job_paused']
    loud, muted = create(h, label='Loud'), create(h, label='Muted')
    failure = notice(h, loud, kind='failed', body='The provider rejected the request.')
    assert h.api.methods['notification_mute'](job_id=muted['id'], muted=True) == {'job_id': muted['id'], 'muted': True}
    hushed = notice(h, muted)
    shown = notice(h, loud)
    posts = h.shell.pump()['post']
    assert [p['id'] for p in posts] == [shown['id']]
    assert delivery(h, failure['id'])['reason'] == 'kind_off' and delivery(h, hushed['id'])['reason'] == 'muted'
    for job_id, value in [(uuid.uuid4().hex, True), (muted['id'], 1), ('../' + muted['id'], True), (None, True)]:
        with pytest.raises(ValueError):
            h.api.methods['notification_mute'](job_id=job_id, muted=value)
    status = h.api.methods['notification_status']()
    assert {j['job_id']: j['muted'] for j in status['jobs']}[muted['id']] is True and status['actions'] == ['open']
    h.api.methods['notification_mute'](job_id=muted['id'], muted=False)
    assert h.outbox.mutes() == []
    h.clock.now += 61
    enable(h, enabled=False)
    off = notice(h, loud)
    batch = h.shell.pump()
    assert (batch['post'], batch['remove'], batch['enabled']) == ([], [], False)
    assert delivery(h, off['id'])['reason'] == 'disabled'
    pending = h.jobs.operation('notifications', pending=True, limit=200)['pending']
    assert pending == 6  # every notice stays in the app; only banners were withheld


def test_quiet_hours_defer_and_summarize_and_never_drop(env):
    h = env
    hours = {'start': '22:00', 'end': '07:00', 'timezone': 'Europe/London'}
    enable(h, quiet_hours=hours)
    one, two = create(h, label='One'), create(h, label='Two')
    h.clock.now = datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc).timestamp()  # 23:00 BST
    deferred = [notice(h, one), notice(h, two), notice(h, one, kind='failed')]
    batch = h.shell.pump()
    assert batch['post'] == [] and batch['quiet_hours'] is True
    assert h.api.methods['notification_status']()['deferred'] == 3
    # Thirty-three hours later, outside quiet hours and past the stale window: summarized, not dropped.
    h.clock.now += 33 * 3600  # 08:00 BST two days on
    [post] = h.shell.pump()['post']
    assert post['title'] == 'While quiet hours were on' and post['body'] == '3 schedule updates are waiting in Pebble.'
    assert post['count'] == 3 and post['chat_id'] is None and post['thread'] == 'pebble-summary'
    assert {delivery(h, n['id'])['state'] for n in deferred} == {'delivered'}
    assert {delivery(h, n['id'])['banner_id'] for n in deferred} == {post['id']}
    # A notice outside quiet hours that waited past the stale window is withheld as noise, still in the app.
    old = notice(h, two)
    enable(h)
    h.clock.now += 25 * 3600
    h.shell.pump()
    assert delivery(h, old['id'])['reason'] == 'stale'
    # A single deferred notice posts as itself.
    enable(h, quiet_hours=hours)
    h.clock.now = datetime(2026, 10, 13, 23, 0, tzinfo=timezone.utc).timestamp()
    alone = notice(h, one)
    assert h.shell.pump()['post'] == []
    h.clock.now += 8 * 3600
    [post] = h.shell.pump()['post']
    assert post['id'] == alone['id'] and post['title'] == 'Scheduled job “One” completed'


def test_one_job_coalesces_within_a_minute_and_jobs_share_a_rate_limit(env):
    h = env
    enable(h)
    busy = create(h, label='Busy')
    first = notice(h, busy)
    h.shell.pump()
    for step in (10, 20):
        h.clock.now += 10
        notice(h, busy)
        [post] = h.shell.pump()['post']
        assert post['id'] == first['id']
    assert post['version'] == 3 and post['count'] == 3 and post['title'] == 'Scheduled job “Busy”: 3 updates'
    assert list(h.shell.os) == [first['id']] and h.shell.os[first['id']]['version'] == 3  # one banner, updated in place
    h.clock.now += 61
    later = notice(h, busy)
    [post] = h.shell.pump()['post']
    assert post['id'] == later['id'] and post['count'] == 1
    # Two banners are in this ten-minute window; one more job banner fits, then one summary.
    others = [create(h, label=f'Job {index}') for index in range(4)]
    ids = []
    for job in others:
        h.clock.now += 1
        ids.append(notice(h, job)['id'])
    posts = h.shell.pump()['post']
    assert RATE_LIMIT == 3 and [p['id'] for p in posts[:1]] == ids[:1]
    [summary] = posts[1:]
    assert summary['title'] == 'More schedule updates' and summary['body'] == '3 more schedule updates are waiting in Pebble.'
    assert summary['count'] == 3 and summary['chat_id'] is None
    h.clock.now += 5
    notice(h, others[0])
    [update] = h.shell.pump()['post']
    assert update['id'] == ids[0] and update['count'] == 2  # same job within a minute joins its own banner
    h.clock.now += 601
    fresh = notice(h, others[1])
    [post] = h.shell.pump()['post']
    assert post['id'] == fresh['id'] and post['count'] == 1


def test_pause_delete_and_acknowledge_withdraw_pending_and_shown_banners(env):
    h = env
    enable(h)
    paused, acked, deleted = create(h, label='Paused'), create(h, label='Acked'), create(h, label='Deleted')
    shown = [notice(h, job) for job in (paused, acked, deleted)]
    h.shell.pump()
    assert set(h.shell.os) == {n['id'] for n in shown}
    h.clock.now += 5
    waiting = notice(h, paused)
    h.jobs.operation('pause', chat_id=paused['chat_id'], job_id=paused['id'], revision=paused['revision'])
    batch = h.shell.pump()
    assert batch['post'] == [] and batch['remove'] == [shown[0]['id']]
    assert delivery(h, waiting['id'])['state'] == 'suppressed' and banner(h, shown[0]['id'])['state'] == 'removed'
    h.job_api.methods['notification_ack'](id=acked['chat_id'], notification_id=shown[1]['id'])
    assert h.shell.pump()['remove'] == [shown[1]['id']]
    h.jobs.operation('delete', chat_id=deleted['chat_id'], job_id=deleted['id'], revision=deleted['revision'])
    assert h.shell.pump()['remove'] == [shown[2]['id']]
    assert h.shell.os == {} and h.shell.pump() == {'post': [], 'remove': [], 'quiet_hours': False, 'enabled': True,
                                                   'ack': {'delivered': 0, 'removed': 0, 'failed': 0, 'ignored': 0}}
    # A removal the shell never confirms is retried a bounded number of times, then reported.
    h.clock.now += 601  # outside the rate window of the three banners above
    other = create(h, label='Unconfirmed')
    item = notice(h, other)
    h.shell.pump()
    h.jobs.operation('ack', notification_ids=[item['id']])
    for _ in range(MAX_ATTEMPTS):
        assert h.api.native_methods['notification_native_pending']()['remove'] == [item['id']]
    assert h.api.native_methods['notification_native_pending']()['remove'] == []
    assert banner(h, item['id'])['state'] == 'failed' and banner(h, item['id'])['reason'] == 'removal_unconfirmed'


def test_failures_and_unacknowledged_posts_stop_without_retry_storms(env):
    h = env
    enable(h)
    job = create(h)
    denied = notice(h, job)
    h.shell.pump(fail='not_authorized')
    assert banner(h, denied['id'])['state'] == 'failed' and delivery(h, denied['id'])['state'] == 'failed'
    assert h.shell.pump()['post'] == []
    status = h.api.methods['notification_status']()
    assert status['last_failure']['reason'] == 'not_authorized' and status['outbox']['failed'] == 1
    h.clock.now += 61
    silent = notice(h, job)
    for _ in range(MAX_ATTEMPTS):
        assert [p['id'] for p in h.api.native_methods['notification_native_pending']()['post']] == [silent['id']]
    assert h.api.native_methods['notification_native_pending']()['post'] == []
    assert banner(h, silent['id'])['reason'] == 'no_acknowledgement'
    delivered = h.api.native_methods['notification_native_delivered']
    for bad in [dict(delivered={'id': silent['id'], 'version': 1}), dict(delivered=[{'id': silent['id']}]),
                dict(delivered=[{'id': silent['id'], 'version': True}]), dict(delivered=[{'id': silent['id'], 'version': 0}]),
                dict(removed=['../x']), dict(failed=[{'id': silent['id'], 'reason': 'approve'}]),
                dict(delivered=[{'id': 'a' * 32, 'version': 1}] * 201)]:
        with pytest.raises(ValueError):
            delivered(**bad)
    assert delivered(delivered=[{'id': uuid.uuid4().hex, 'version': 1}], removed=[uuid.uuid4().hex],
                     failed=[{'id': uuid.uuid4().hex, 'reason': 'error'}]) == {
        'delivered': 0, 'removed': 0, 'failed': 0, 'ignored': 3}
    with pytest.raises(ValueError, match='accepts'):
        delivered(delivered=[], posted=[])


def test_routes_are_native_only_and_absent_from_webview_pet_and_model_surfaces(env):
    h = env
    assert set(h.api.methods) == {'notification_status', 'notification_mute'}
    assert set(h.api.native_methods) == {'lifecycle_event', 'lifecycle_quit_summary', 'notification_native_pending',
                                         'notification_native_delivered'}
    source = (ROOT / 'src-tauri' / 'src' / 'main.rs').read_text()
    allowed = re.search(r'const ALLOWED: &\[&str\] = &\[(.*?)\];', source).group(1)
    pet = re.search(r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains', source).group(1)
    for name in h.api.native_methods:
        assert f'"{name}"' not in allowed and f'"{name}"' not in pet
    for name in h.api.methods:
        assert f'"{name}"' not in pet  # Settings in the main window only
    from pebble.tool_catalogue import TOOLS
    tools = [tool['function']['name'].lower() for tool in TOOLS]
    assert tools and not any('notif' in name or 'lifecycle' in name or 'banner' in name for name in tools)
    assert not any('approve' in name for name in list(h.api.methods) + list(h.api.native_methods))
    with pytest.raises(ValueError, match='accepts'):
        h.api.methods['notification_status'](chat_id='a' * 32)
    # Without an App.lifecycle the routes fail closed (the state before the parent wired it).
    lifecycle, h.app.lifecycle = h.app.lifecycle, None
    try:
        with pytest.raises(ValueError, match='Lifecycle observation is unavailable'):
            h.api.native_methods['lifecycle_event'](kind='did_wake')
        status = h.api.methods['notification_status']()
        assert set(status) == {'settings', 'quiet_now', 'outbox', 'deferred', 'last_failure', 'jobs', 'actions', 'lifecycle'}
        assert status['lifecycle'] is None and status['outbox'] == {'pending': 0, 'delivered': 0, 'suppressed': 0, 'failed': 0}
    finally:
        h.app.lifecycle = lifecycle
    # Phase 17 merge: App wires a Lifecycle, so the shell's hint is accepted and Settings reads its status.
    assert h.api.native_methods['lifecycle_event'](kind='did_wake')['accepted'] is True
    status = h.api.methods['notification_status']()
    assert set(status['lifecycle']) == {'sleeping', 'sleep_clock', 'observations'}
    assert status['outbox'] == {'pending': 0, 'delivered': 0, 'suppressed': 0, 'failed': 0}


def test_new_tables_match_a_fresh_app_once_added_to_the_epoch_seven_reference(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing
    from pebble import migrations
    from pebble.lifecycle import LIFECYCLE_DDL, Lifecycle
    from pebble.notifications import NOTIFY_DDL
    instance = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        Lifecycle(instance)
        Notifications(instance)  # what the parent's App wiring creates during construction
    finally:
        instance.close()
    with closing(sqlite3.connect(tmp_path / 'data' / migrations.DATABASE)) as db:
        schema = migrations._schema(db)
    # The parent's epoch-7 REFERENCE_DDL adds exactly these, before upgrade_history.
    monkeypatch.setattr(migrations, 'REFERENCE_DDL', migrations.REFERENCE_DDL + LIFECYCLE_DDL + NOTIFY_DDL)
    migrations._reference.cache_clear()
    try:
        reference = migrations._reference()
    finally:
        monkeypatch.undo()
        migrations._reference.cache_clear()
    owned = ('lifecycle_observations', 'native_deliveries', 'native_banners', 'host_notices')
    for key in owned:
        have, want = schema['tables'][key], reference['tables'][key]
        assert (have['columns'], have['uniques'], have['checks'], have['tail']) == (
            want['columns'], want['uniques'], want['checks'], want['tail']), key
    assert {name: index for name, index in reference['indexes'].items() if index[0] in owned} == {
        name: index for name, index in schema['indexes'].items() if index[0] in owned}
    assert not any(migrations.MARKER.search(key) for key in owned)
    compat = migrations._compatibility(schema, reference)
    assert (compat['refusals'], compat['missing_tables'], compat['unknown_tables']) == ([], [], [])


def test_host_notices_list_and_acknowledge_through_the_job_routes(env):
    h = env
    chat, other = h.app.new_chat(), h.app.new_chat()
    mine = create(h, label='Mine', chat=chat)
    with h.app.store.lock, h.app.store.db as db:
        scoped = h.outbox.host_notice(db, 'late_skipped', chat['id'], mine['id'], 'f' * 64, 'Skipped', 'A late time.', T0)
        shared = h.outbox.host_notice(db, 'wake_summary', None, None, 'e' * 64, 'Schedules while asleep', 'Counts only.', T0 + 1)
        assert h.outbox.host_notice(db, 'wake_summary', None, None, 'e' * 64, 'Again', 'Again.', T0 + 2) is None
        with pytest.raises(ValueError):
            h.outbox.host_notice(db, 'approve', None, None, 'd' * 64, 'x', 'y', T0)
    listed = h.job_api.methods['job_list'](id=chat['id'])['notifications']
    assert [(n['id'], n['source'], n['body_origin']) for n in listed] == [(shared['id'], 'host', 'host'),
                                                                         (scoped['id'], 'host', 'host')]
    assert all(type(n['created']) is int for n in listed)
    assert [n['id'] for n in h.job_api.methods['job_list'](id=other['id'])['notifications']] == [shared['id']]
    ack = h.job_api.methods['notification_ack']
    with pytest.raises(ValueError, match='not found'):
        ack(id=other['id'], notification_id=scoped['id'])  # another conversation's notice
    with pytest.raises(ValueError, match='not found'):
        ack(id=chat['id'], notification_id=uuid.uuid4().hex)
    assert ack(id=other['id'], notification_id=shared['id']) == {'acknowledged': 1, 'already_acknowledged': 0}
    assert ack(id=chat['id'], notification_id=shared['id']) == {'acknowledged': 0, 'already_acknowledged': 1}
    assert [n['id'] for n in h.job_api.methods['job_list'](id=chat['id'])['notifications']] == [scoped['id']]
    job_notice = notice(h, mine)
    assert ack(id=chat['id'], notification_id=job_notice['id']) == {'acknowledged': 1, 'already_acknowledged': 0}
    h.app.delete_chat(chat['id'])
    assert h.app.store.query('SELECT count(*) AS n FROM host_notices WHERE chat_id=?', (chat['id'],))[0]['n'] == 0
