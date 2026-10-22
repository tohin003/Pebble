"""Native-only route adapter over App.jobs.

Routes are reached from native RPC only; no model tool maps to them. They
convert the UI's epoch-second DTOs to the core's UTC ISO schedules and back,
refuse authority fields by name, and scope every read and write to one real
conversation. Each route takes Jobs.lock and then App.task_lock before calling
Jobs.operation, the same order as a scheduler pass. Configuration preview,
creation and resume are therefore serialized with foreground admission.
Nothing here starts inference, writes human input, approves an action or
resumes a run. When App.notifications is attached, job_list also returns the
conversation's quiet host notices (and global wake or clock summaries) with
`source: host`, and notification_ack acknowledges either kind by exact id.
"""

import functools
import math

from .jobs import NATIVE_ONLY, STALE, ident, iso, utc_instant


FORBIDDEN = NATIVE_ONLY | {'policy', 'policies', 'grant', 'grants', 'capabilities', 'scopes'}
ROUTES = {
    'job_list': ({'id'}, set()),
    'job_get': ({'id', 'job_id'}, set()),
    'job_preview': ({'id', 'schedule', 'limits'}, {'effort'}),
    'job_create': ({'id', 'name', 'task', 'schedule', 'monitor', 'limits', 'config_fingerprint'}, {'effort'}),
    'job_pause': ({'id', 'job_id', 'revision'}, set()),
    'job_preview_resume': ({'id', 'job_id', 'revision'}, set()),
    'job_resume': ({'id', 'job_id', 'revision', 'config_fingerprint'}, set()),
    'job_delete': ({'id', 'job_id', 'revision'}, set()),
    'notification_ack': ({'id', 'notification_id'}, set()),
}
SCHEDULE_KEYS = {'once': {'kind', 'at'}, 'interval': {'kind', 'seconds'}, 'daily': {'kind', 'time', 'timezone'}}
POLICY_KEYS = {'catch_up', 'grace_seconds'}  # optional on every kind; the core validates them
STATES = {'active': 'active', 'paused': 'paused', 'needs_review': 'needs_review', 'finished': 'completed'}
RESUMABLE = ('paused', 'needs_review')
# A blocked fire is reported through the job's review_reason while it needs review.
ERROR_STATUSES = ('waiting', 'attention', 'failed', 'needs_review')
MAX_EPOCH = 253402300799  # 9999-12-31T23:59:59Z


def epoch_in(value):
    """UI epoch seconds to core UTC ISO. Whole seconds only; a boolean is not a number."""
    if (type(value) not in (int, float) or not math.isfinite(value) or value != int(value)
            or not 0 <= value <= MAX_EPOCH):
        raise ValueError('Give a one-time job as whole UTC epoch seconds.')
    return iso(int(value))


def epoch_out(text):
    return None if text is None else int(utc_instant(text))


def schedule_in(value):
    if not isinstance(value, dict) or not isinstance(value.get('kind'), str):
        raise ValueError('A schedule needs a kind: once, interval or daily.')
    keys = SCHEDULE_KEYS.get(value['kind'])
    if keys is None or set(value) - POLICY_KEYS != keys:
        raise ValueError('Use once {at}, interval {seconds} or daily {time, timezone}.')
    policy = {key: value[key] for key in POLICY_KEYS if key in value}
    if value['kind'] == 'once':
        return {'kind': 'once', 'at': epoch_in(value['at']), **policy}
    return {**{key: value[key] for key in keys}, **policy}  # the core validates types, bounds and zones


def schedule_out(value):
    if value.get('kind') == 'once':
        return {'kind': 'once', 'at': epoch_out(value['at']), **{k: value[k] for k in POLICY_KEYS if k in value}}
    return dict(value)


def host_dto(row):
    return {**{k: row[k] for k in ('id', 'job_id', 'fire_id', 'run_id', 'kind', 'status', 'title', 'body', 'body_origin',
                                   'source')}, 'created': epoch_out(row['created'])}


def notice_dto(row):
    return {**{k: row[k] for k in ('id', 'job_id', 'fire_id', 'run_id', 'kind', 'status', 'title', 'body', 'body_origin')},
            'created': epoch_out(row['created'])}


def fire_dto(row):
    return {**{k: row[k] for k in ('id', 'revision', 'missed', 'origin', 'status', 'reason', 'run_id', 'run_state')},
            'scheduled': epoch_out(row['scheduled']), 'claimed': epoch_out(row['claimed']),
            'updated': epoch_out(row['updated'])}


class JobAPI:
    """`methods` maps each public route to a callable taking the native request params."""

    def __init__(self, app):
        self.app = app
        self.methods = {route: functools.partial(self._route, route) for route in ROUTES}

    @property
    def jobs(self):
        # Resolved per call: a test or restart may attach a different Jobs instance.
        jobs = getattr(self.app, 'jobs', None)
        if jobs is None:
            raise ValueError('Scheduled jobs are unavailable.')
        return jobs

    def _route(self, route, /, **params):
        forbidden = sorted(set(params) & FORBIDDEN)
        if forbidden:
            raise ValueError('Origin, configuration, policy and grants are computed by the host when you review '
                             'a job. Remove: ' + ', '.join(forbidden) + '.')
        required, optional = ROUTES[route]
        if not required <= set(params) or set(params) - required - optional:
            raise ValueError(f'{route} accepts ' + ', '.join(sorted(required | optional)) + '.')
        jobs = self.jobs
        # Scheduler order: Jobs.lock, then App.task_lock. Never take task_lock first.
        with jobs.lock, self.app.task_lock:
            self._chat(params['id'])
            return getattr(self, '_' + route)(jobs, **params)

    def _chat(self, chat_id):
        ident(chat_id, 'Conversation not found.')
        if not self.app.store.query('SELECT 1 FROM chats WHERE id=?', (chat_id,)):
            raise ValueError('Conversation not found.')

    def _last_run(self, chat_id, job_id):
        """Bounded readback; the latest bound run stays visible after its fire settles."""
        store = self.app.store
        latest = store.query('SELECT status,reason FROM job_fires WHERE job_id=? AND chat_id=? '
                             'ORDER BY scheduled DESC,claimed DESC LIMIT 1', (job_id, chat_id))
        ran = store.query('SELECT f.run_id,f.run_state,r.state AS current FROM job_fires f '
                          'LEFT JOIN runs r ON r.id=f.run_id AND r.chat_id=f.chat_id '
                          'WHERE f.job_id=? AND f.chat_id=? AND f.run_id IS NOT NULL '
                          'ORDER BY f.scheduled DESC,f.claimed DESC LIMIT 1', (job_id, chat_id))
        error = latest[0]['reason'] if latest and latest[0]['status'] in ERROR_STATUSES else None
        if not ran:
            return None, None, error
        return ran[0]['run_id'], ran[0]['current'] or ran[0]['run_state'], error

    def _dto(self, view):
        run_id, run_state, error = self._last_run(view['chat_id'], view['id'])
        if view['state'] == 'needs_review' and view['review_reason']:
            error = view['review_reason']
        return {'id': view['id'], 'name': view['label'], 'task': view['task'],
                'schedule': schedule_out(view['schedule']), 'state': STATES[view['state']],
                'revision': view['revision'], 'next_fire': epoch_out(view['next_due']), 'error': error,
                'last_run_id': run_id, 'last_run_state': run_state, 'monitor': view['monitor'],
                'config': view['config']}

    def _reviewable(self, jobs, chat_id, job_id, revision):
        job = jobs.operation('get', chat_id=chat_id, job_id=job_id)['job']
        if type(revision) is not int or revision != job['revision']:
            raise ValueError(STALE)
        if job['state'] not in RESUMABLE:
            raise ValueError('Only a paused job or one awaiting review can be resumed.')
        # The same settings feed the preview and the re-pin, so their fingerprints agree.
        pinned = job['config']['effort']
        return {'effort': pinned if isinstance(pinned, str) else None, 'limits': job['config']['limits']}

    def _job_list(self, jobs, id):
        jobs.refresh()  # readback of authoritative run states; never dispatches inference
        listed = jobs.operation('list', chat_id=id)
        notices = [notice_dto(notice) for notice in
                   jobs.operation('notifications', chat_id=id, pending=True, limit=200)['notifications']]
        outbox = getattr(self.app, 'notifications', None)
        if outbox is not None:
            hosted = [host_dto(notice) for notice in outbox.host_list(id)]
            notices = sorted(notices + hosted, key=lambda notice: -notice['created'])[:200]
        scheduler = listed['scheduler']
        return {'jobs': [self._dto(view) for view in listed['jobs']],
                'notifications': notices,
                'service': {'running': bool(scheduler['running'] and not scheduler['closing']),
                            'execution': 'while_service_running', 'last_error': scheduler['last_error']}}

    def _job_get(self, jobs, id, job_id):
        detail = jobs.operation('get', chat_id=id, job_id=job_id)
        return {'job': self._dto(detail['job']), 'fires': [fire_dto(fire) for fire in detail['fires']],
                'drift': detail['drift']}

    def _job_preview(self, jobs, id, schedule, limits, effort=None):
        preview = jobs.operation('preview', chat_id=id, schedule=schedule_in(schedule), limits=limits, effort=effort)
        return {'config_fingerprint': preview['config_fingerprint'], 'config': preview['config'],
                'schedule': schedule_out(preview['schedule']),
                'next_times': [epoch_out(time) for time in preview['next_times']]}

    def _job_create(self, jobs, id, name, task, schedule, monitor, limits, config_fingerprint, effort=None):
        return self._dto(jobs.operation('create', chat_id=id, label=name, task=task, schedule=schedule_in(schedule),
                                        monitor=monitor, limits=limits, effort=effort,
                                        config_fingerprint=config_fingerprint))

    def _job_pause(self, jobs, id, job_id, revision):
        return self._dto(jobs.operation('pause', chat_id=id, job_id=job_id, revision=revision))

    def _job_preview_resume(self, jobs, id, job_id, revision):
        preview = jobs.operation('preview', chat_id=id, **self._reviewable(jobs, id, job_id, revision))
        return {'config_fingerprint': preview['config_fingerprint'], 'config': preview['config']}

    def _job_resume(self, jobs, id, job_id, revision, config_fingerprint):
        settings = self._reviewable(jobs, id, job_id, revision)
        # Re-pin the reviewed current configuration; task and schedule are unchanged.
        # This schedules future fires only: an unresolved fire still blocks, and no run is touched.
        view = jobs.operation('update', chat_id=id, job_id=job_id, revision=revision,
                              config_fingerprint=config_fingerprint, **settings)
        if view['state'] == 'paused':
            view = jobs.operation('resume', chat_id=id, job_id=job_id, revision=view['revision'])
        return self._dto(view)

    def _job_delete(self, jobs, id, job_id, revision):
        return jobs.operation('delete', chat_id=id, job_id=job_id, revision=revision)

    def _notification_ack(self, jobs, id, notification_id):
        outbox = getattr(self.app, 'notifications', None)
        hosted = outbox.host_ack(id, notification_id) if outbox is not None else None
        if hosted is not None:
            return hosted
        return jobs.operation('ack', notification_ids=[notification_id], chat_id=id)
