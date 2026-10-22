"""Durable scheduled jobs created and changed only through reviewed native requests.

A person previews a job's pinned runtime configuration and then creates it
through a native request. Task text is model input only: permissions,
credentials, origin and configuration are computed by the host and are never
accepted from a request field or task JSON. Each fire is a host-origin trigger.
It binds a new ordinary root run in the same transaction as its claim, then
starts that run through RunController.launch under App.task_lock, so the usual
Guard, broker, provider and deadline paths apply. A claimed fire without a
bound run, or a bound run interrupted by a restart, needs review and is never
replayed. Notifications form a local durable outbox for native inspection.

Optional collaborators (Phase 17) attach through the constructor or as
App.lifecycle and App.notifications. lifecycle.py reports sleep, wake and
wall-clock changes; a will-sleep hint defers fires as `system_sleep`, and each
wake yields one coalesced, host-authored summary. notifications.py receives
each new notice in its own transaction and decides whether it becomes an
operating-system banner. A schedule may carry `catch_up: skip_if_late` with a
grace window; otherwise a late backlog still fires once at its latest slot.
Nothing here sends e-mail or network traffic.
"""

import hashlib
import json
import math
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .policy import Workspace
from .runtime import RunJournal, RunLimits, encoded


VERSION = 1
MIN_INTERVAL, MAX_INTERVAL = 60, 30 * 86400
ONCE_HORIZON = 366 * 86400
MAX_JOBS = 64
MAX_FIRES = 50  # retained history per job
MAX_PENDING_NOTICES = 200
MAX_NOTICES = 1000
MAX_MISSED = 100_000
LABEL_LIMIT, TASK_LIMIT, PREVIEW_LIMIT = 80, 4000, 600
CATCH_UP = ('latest', 'skip_if_late')
GRACE_DEFAULT, GRACE_MIN, GRACE_MAX = 7200, 60, 86400  # seconds a skip_if_late slot may be late and still fire
LIVE = ('claimed', 'bound', 'running', 'waiting')
TRACKED = ('bound', 'running', 'waiting', 'attention')
PRUNABLE = ('completed', 'failed', 'cancelled', 'blocked', 'dismissed')
ROOT_LIVE = ('queued', 'running', 'retry_wait', 'waiting_input', 'waiting_approval')
CHILD_LIVE = ('queued', 'running', 'waiting_parent', 'waiting_input')
HEX = re.compile(r'[0-9a-f]{32}')
FINGERPRINT = re.compile(r'[0-9a-f]{64}')
HHMM = re.compile(r'([01][0-9]|2[0-3]):([0-5][0-9])')
INSTANT = re.compile(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?(?:Z|\+00:00)')
ZONE_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_+-]{0,29}(?:/[A-Za-z0-9][A-Za-z0-9_+-]{0,29}){0,2}')
# Request fields that only the host may compute. They are refused by name, and
# a task that is itself a JSON object carrying one of them is refused too.
NATIVE_ONLY = frozenset({
    'permissions', 'permission', 'mode', 'tools', 'config', 'runtime_config', 'pin', 'pin_fingerprint',
    'credentials', 'credential', 'token', 'api_key', 'secret', 'password', 'route', 'profile', 'provider',
    'workspace', 'extensions', 'mods', 'origin', 'authority', 'authorization', 'approval', 'approved',
    'auto_approve', 'run_id', 'state', 'next_due', 'anchor'})
OPERATIONS = {
    'preview': ({'chat_id'}, {'schedule', 'effort', 'limits'}),
    'create': ({'chat_id', 'label', 'task', 'schedule', 'config_fingerprint'}, {'monitor', 'effort', 'limits'}),
    'update': ({'chat_id', 'job_id', 'revision', 'config_fingerprint'},
               {'label', 'task', 'schedule', 'monitor', 'effort', 'limits'}),
    'pause': ({'chat_id', 'job_id', 'revision'}, set()),
    'resume': ({'chat_id', 'job_id', 'revision'}, set()),
    'delete': ({'chat_id', 'job_id', 'revision'}, set()),
    'list': (set(), {'chat_id'}),
    'get': ({'chat_id', 'job_id'}, set()),
    'notifications': (set(), {'chat_id', 'pending', 'limit'}),
    'ack': ({'notification_ids'}, {'chat_id'}),
    'cancel_fire': ({'chat_id', 'job_id', 'fire_id'}, set()),
    'dismiss_fire': ({'chat_id', 'job_id', 'fire_id'}, set()),
}
ATTENTION = {
    'waiting_input': 'The scheduled run is waiting for your answer in its conversation.',
    'waiting_approval': 'The scheduled run is waiting for your approval in its conversation.',
    'paused': 'The scheduled run is paused. Resume or stop it from its conversation.',
    'interrupted': 'The scheduled run was interrupted, for example by a service restart. It will not resume automatically.',
    'needs_reconciliation': 'A tool in the scheduled run has an uncertain outcome. Reconcile it before resuming.',
    'verification_required': 'The scheduled run ended without the verification it needs. Review it before relying on the result.',
    'budget_exhausted': 'The scheduled run reached its step or time limit. Review it before resuming with larger limits.',
}
DRIFT = {
    'workspace': 'project folder', 'workspace_identity': 'project folder identity', 'route': 'model route',
    'effort': 'effort', 'permissions': 'permissions', 'extensions': 'enabled extensions', 'mods': 'enabled mods',
    'limits': 'run limits', 'conversation': 'conversation', 'workspace_unavailable': 'project folder availability',
    'configuration_unavailable': 'runtime configuration', 'authz': 'tool catalogue',
}
MISSING = 'Job not found in this conversation.'
STALE = 'This job changed since you reviewed it. Reload it and try again.'
REVIEW_CHANGED = 'The reviewed configuration no longer matches the current settings. Preview the job again and review the result.'
SCHEMA = '''
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS job_meta(version INTEGER NOT NULL);
INSERT INTO job_meta SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM job_meta);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin='human'),
  label TEXT NOT NULL, task TEXT NOT NULL, schedule TEXT NOT NULL, monitor INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','paused','needs_review','finished')),
  revision INTEGER NOT NULL, pin TEXT NOT NULL, pin_fingerprint TEXT NOT NULL,
  anchor REAL NOT NULL, next_due REAL, last_scheduled REAL, last_fingerprint TEXT,
  review_reason TEXT, deferred_reason TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state,next_due);
CREATE INDEX IF NOT EXISTS jobs_chat ON jobs(chat_id,created);
CREATE TABLE IF NOT EXISTS job_fires(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, chat_id TEXT NOT NULL, revision INTEGER NOT NULL,
  scheduled REAL NOT NULL, missed INTEGER NOT NULL, origin TEXT NOT NULL CHECK(origin='host'),
  status TEXT NOT NULL CHECK(status IN ('claimed','bound','running','waiting','attention','completed',
    'failed','cancelled','needs_review','blocked','dismissed')),
  reason TEXT, run_id TEXT UNIQUE, run_state TEXT, pin_fingerprint TEXT NOT NULL,
  result_fingerprint TEXT, label_message_id TEXT, claimed REAL NOT NULL, updated REAL NOT NULL,
  UNIQUE(job_id,scheduled,revision));
CREATE UNIQUE INDEX IF NOT EXISTS job_fires_one_live ON job_fires(job_id)
  WHERE status IN ('claimed','bound','running','waiting');
CREATE INDEX IF NOT EXISTS job_fires_status ON job_fires(status);
CREATE TABLE IF NOT EXISTS job_notifications(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, fire_id TEXT NOT NULL, chat_id TEXT NOT NULL,
  run_id TEXT, kind TEXT NOT NULL CHECK(kind IN ('completed','failed','action_needed')),
  status TEXT NOT NULL, fingerprint TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  created REAL NOT NULL, acknowledged REAL, UNIQUE(fire_id,status,fingerprint));
CREATE INDEX IF NOT EXISTS job_notifications_pending ON job_notifications(acknowledged,created);
COMMIT;
'''


# Pure schedule helpers. Times are POSIX seconds; callers inject `now`.

def iso(epoch):
    return None if epoch is None else datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def utc_instant(value):
    match = INSTANT.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ValueError('Give a one-time job a UTC instant such as 2026-10-10T09:30:00Z.')
    try:
        moment = datetime(*(int(part or 0) for part in match.groups()), tzinfo=timezone.utc)
    except ValueError:
        raise ValueError('Give a valid calendar date and time.') from None
    return moment.timestamp()


def time_zone(name):
    if (not isinstance(name, str) or not ZONE_NAME.fullmatch(name)
            or name.lower() in {'localtime', 'posixrules', 'factory'}):
        raise ValueError('Use an IANA time zone name such as Europe/London.')
    try:
        return ZoneInfo(name)
    except Exception:  # unknown or malformed zone, or a non-TZif file in the zone tree
        raise ValueError('Use an IANA time zone name such as Europe/London.') from None


def catch_up(value):
    """The optional late-slot policy. The default, latest, is stored as no keys at all."""
    policy = value.get('catch_up', 'latest')
    if not isinstance(policy, str) or policy not in CATCH_UP:
        raise ValueError('Choose catch_up latest or skip_if_late.')
    if 'grace_seconds' in value and policy != 'skip_if_late':
        raise ValueError('grace_seconds applies only to catch_up skip_if_late.')
    grace = value.get('grace_seconds', GRACE_DEFAULT)
    if type(grace) is not int or not GRACE_MIN <= grace <= GRACE_MAX:
        raise ValueError('Choose a whole-second late limit from 60 seconds to one day.')
    return {} if policy == 'latest' else {'catch_up': 'skip_if_late', 'grace_seconds': grace}


def parse_schedule(value, now):
    if not isinstance(value, dict) or not isinstance(value.get('kind'), str):
        raise ValueError('A schedule needs a kind: once, interval or daily.')
    kind = value['kind']
    keys = {'once': {'kind', 'at'}, 'interval': {'kind', 'seconds'}, 'daily': {'kind', 'time', 'timezone'}}.get(kind)
    if keys is None or set(value) - {'catch_up', 'grace_seconds'} != keys:
        raise ValueError('Use once {at}, interval {seconds} or daily {time, timezone}.')
    policy = catch_up(value)
    if kind == 'once':
        at = utc_instant(value['at'])
        if not now < at <= now + ONCE_HORIZON:
            raise ValueError('Choose a future UTC time within one year.')
        return {'kind': 'once', 'at': iso(at), **policy}
    if kind == 'interval':
        seconds = value['seconds']
        if type(seconds) is not int or not MIN_INTERVAL <= seconds <= MAX_INTERVAL:
            raise ValueError('Choose a whole-second interval from 60 seconds to 30 days.')
        return {'kind': 'interval', 'seconds': seconds, **policy}
    if not isinstance(value['time'], str) or not HHMM.fullmatch(value['time']):
        raise ValueError('Give a daily time as HH:MM in 24-hour local time.')
    time_zone(value['timezone'])
    return {'kind': 'daily', 'time': value['time'], 'timezone': value['timezone'], **policy}


def late(schedule, slot, now):
    """True when a skip_if_late schedule's slot is past its grace window."""
    return schedule.get('catch_up') == 'skip_if_late' and now - slot > schedule['grace_seconds']


def gap_days(schedule, created, now):
    """(day, would-be instant) for today and yesterday in the job's zone when its time fell in a DST gap.

    Only days whose would-be instant passed after the job existed qualify.
    """
    if schedule.get('kind') != 'daily':
        return []
    zone, clock = time_zone(schedule['timezone']), schedule['time']
    today, found = datetime.fromtimestamp(now, zone).date(), []
    for day in (today - timedelta(days=1), today):
        if daily_slot(day, clock, zone) is None:
            # fold=0 maps a missing wall time with the offset before the change, so after the gap.
            moment = datetime(day.year, day.month, day.day, int(clock[:2]), int(clock[3:]), tzinfo=zone).timestamp()
            if created < moment <= now:
                found.append((day, moment))
    return found


def daily_slot(day, clock, zone):
    """The UTC instant of a local wall time, or None when that time does not exist."""
    wall = datetime(day.year, day.month, day.day, int(clock[:2]), int(clock[3:]), tzinfo=zone)
    # fold=0 selects the earlier instant of a repeated hour, so it runs once.
    instant = wall.astimezone(timezone.utc)
    if instant.astimezone(zone).replace(tzinfo=None) != wall.replace(tzinfo=None):
        return None  # skipped by a DST gap; this day has no slot
    return instant.timestamp()


def next_slot(schedule, after, anchor):
    """The first slot strictly after `after`, or None when none remains."""
    kind = schedule['kind']
    if kind == 'once':
        at = utc_instant(schedule['at'])
        return at if at > after else None
    if kind == 'interval':
        seconds = schedule['seconds']
        steps = max(1, math.floor((after - anchor) / seconds) + 1)
        while anchor + steps * seconds <= after:
            steps += 1
        return anchor + steps * seconds
    zone = time_zone(schedule['timezone'])
    day = datetime.fromtimestamp(after, zone).date() - timedelta(days=1)
    for offset in range(10):
        slot = daily_slot(day + timedelta(days=offset), schedule['time'], zone)
        if slot is not None and slot > after:
            return slot
    return None


def latest_slot(schedule, now, first, anchor):
    """Coalesce due slots from `first` through `now` into the latest one.

    Returns (slot, missed) where missed counts the earlier slots folded in, or
    (None, 0) when nothing is due. A backlog never becomes several fires.
    """
    if first is None or first > now:
        return None, 0
    kind = schedule['kind']
    if kind == 'once':
        return first, 0
    if kind == 'interval':
        seconds = schedule['seconds']
        latest = max(first, anchor + math.floor((now - anchor) / seconds) * seconds)
        return latest, min(MAX_MISSED, round((latest - first) / seconds))
    zone = time_zone(schedule['timezone'])
    day = datetime.fromtimestamp(now, zone).date() + timedelta(days=1)
    latest = first
    for offset in range(10):
        slot = daily_slot(day - timedelta(days=offset), schedule['time'], zone)
        if slot is not None and slot <= now:
            latest = max(first, slot)
            break
    if latest - first > 400 * 86400:
        return latest, min(MAX_MISSED, round((latest - first) / 86400))
    missed, slot = 0, first
    while slot is not None and slot < latest:
        slot = next_slot(schedule, slot, anchor)
        missed += 1
    return latest, missed


def run_status(state):
    """Map a root run state to its fire status. Nothing here resumes a run."""
    if state is None:
        return 'cancelled'
    if state in ('queued', 'running', 'retry_wait'):
        return 'running'
    if state in ('waiting_input', 'waiting_approval'):
        return 'waiting'
    if state in ('completed', 'failed'):
        return state
    if state == 'stopped':
        return 'cancelled'
    return 'attention'


def clean_label(value):
    value = value.strip() if isinstance(value, str) else ''
    if (not value or len(value) > LABEL_LIMIT
            or any(ord(c) < 32 or 127 <= ord(c) < 160 or c in '\u2028\u2029' for c in value)):
        raise ValueError(f'Use a one-line label of 1 to {LABEL_LIMIT} characters.')
    return value


def clean_task(value):
    if not isinstance(value, str):
        raise ValueError('Give the task as plain text.')
    value = value.strip()
    if (not value or len(value) > TASK_LIMIT or len(value.encode()) > 4 * TASK_LIMIT
            or any((ord(c) < 32 and c not in '\n\r\t') or ord(c) == 127 for c in value)):
        raise ValueError(f'Give a task of up to {TASK_LIMIT} characters without control characters.')
    if value.startswith('{'):
        try:
            data = json.loads(value)
        except ValueError:
            data = None
        if isinstance(data, dict) and {str(key).lower() for key in data} & NATIVE_ONLY:
            raise ValueError('Task text cannot carry permissions, credentials or configuration; '
                             'the host computes them when you review the job.')
    return value


def ident(value, message):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError(message)
    return value


def digest(*parts):
    return hashlib.sha256('\x1f'.join(str(part) for part in parts).encode()).hexdigest()


def preview(text):
    text = (text or '').strip()
    return text if len(text) <= PREVIEW_LIMIT else text[:PREVIEW_LIMIT - 1] + '…'


def marks(values):
    return ','.join("'" + value + "'" for value in values)  # host constants only


def duration(seconds):
    """Short human duration for host-authored notices."""
    seconds = max(0, int(round(seconds)))
    if seconds < 90:
        return f'{seconds} s'
    minutes = seconds // 60
    if minutes < 60:
        return f'{minutes} min'
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f'{hours} h' + (f' {minutes} min' if minutes else '')
    days, hours = divmod(hours, 24)
    return f'{days} days' + (f' {hours} h' if hours else '')


def machine():
    return 'your Mac' if sys.platform == 'darwin' else 'this computer'


def plural(count, one, many):
    return one if count == 1 else many


def summary(pin):
    config = pin['config']
    route = config.get('route')
    return {
        'workspace': config.get('workspace'), 'workspace_pinned': pin.get('workspace') is not None,
        'model': {k: route.get(k) for k in ('id', 'kind', 'model')} if route else None,
        'effort': config.get('effort'), 'permissions': config.get('permissions'),
        'extensions': config.get('extensions'), 'mods': len(config.get('mods') or []),
        'limits': config.get('limits'),
    }


def fire_view(row):
    return {'id': row['id'], 'job_id': row['job_id'], 'chat_id': row['chat_id'], 'revision': row['revision'],
            'scheduled': iso(row['scheduled']), 'missed': row['missed'], 'origin': row['origin'],
            'status': row['status'], 'reason': row['reason'], 'run_id': row['run_id'], 'run_state': row['run_state'],
            'pin_fingerprint': row['pin_fingerprint'], 'result_fingerprint': row['result_fingerprint'],
            'claimed': iso(row['claimed']), 'updated': iso(row['updated'])}


def notice_view(row):
    return {**{k: row[k] for k in ('id', 'job_id', 'fire_id', 'chat_id', 'run_id', 'kind', 'status', 'title', 'body')},
            # Completion and failure text come from the run; they are data, never instructions.
            'body_origin': 'run_output' if row['kind'] in ('completed', 'failed') else 'host',
            'created': iso(row['created']), 'acknowledged': iso(row['acknowledged'])}


def lane_busy(lane):
    if lane is None:
        return False
    if hasattr(lane, 'idle'):
        return not lane.idle()
    owners, queues = getattr(lane, '_owners', {}), getattr(lane, '_queues', {})
    condition = getattr(lane, '_condition', None)
    if condition is None:
        return any(owners.values()) or any(queues.values())
    with condition:
        return any(owners.values()) or any(queues.values())


def _rows(db, sql, args=()):
    cursor = db.execute(sql, args)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _one(db, sql, args=()):
    rows = _rows(db, sql, args)
    return rows[0] if rows else None


class Jobs:
    """Scheduler and native operations. Never call it while holding App.task_lock,
    except wake(); its lock order is Jobs.lock, then App.task_lock, then Store.lock."""

    def __init__(self, app, clock=None, poll=5.0, lifecycle=None, notifications=None):
        if type(poll) not in (int, float) or not math.isfinite(poll) or not 0.05 <= poll <= 300:
            raise ValueError('Choose a 0.05 to 300 second job poll interval.')
        self.app, self.store = app, app.store
        self.clock = clock or time.time
        # Not `_notifications`: operation() dispatches the 'notifications' operation to that method name.
        self._attached = {'lifecycle': lifecycle, 'notifications': notifications}
        self.poll = float(poll)
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.changed = threading.Event()
        self.thread = None
        self.started = False
        self.last_error = None
        with self.store.lock, self.store.db as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_meta'").fetchone():
                row = db.execute('SELECT version FROM job_meta').fetchone()
                if not row or row[0] != VERSION:
                    raise ValueError('This jobs database requires a compatible Pebble version.')
            db.executescript(SCHEMA)

    @property
    def lifecycle(self):
        # Resolved per call, like JobAPI.jobs: the parent may attach App.lifecycle after construction.
        owner = self._attached['lifecycle']
        return owner if owner is not None else getattr(self.app, 'lifecycle', None)

    @property
    def notifications(self):
        owner = self._attached['notifications']
        return owner if owner is not None else getattr(self.app, 'notifications', None)

    # Lifecycle

    def start(self, thread=True):
        with self.lock:
            if self.started or self.closing.is_set():
                return
            self.started = True
            self.recover()
            if thread:
                self.thread = threading.Thread(target=self._loop, name='pebble-jobs', daemon=True)
                self.thread.start()
                self.changed.set()

    def wake(self):
        self.changed.set()

    def close(self, timeout=2.0):
        """Stop new fires. Returns False if a scheduler pass still holds its lock."""
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('Choose a finite close timeout.')
        self.closing.set()
        self.changed.set()
        deadline = time.monotonic() + timeout
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                return False
        if not self.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        self.lock.release()
        return True

    def _loop(self):
        while not self.closing.is_set():
            self.changed.wait(self.poll)
            self.changed.clear()
            if self.closing.is_set():
                return
            try:
                self.tick()
            except Exception as exc:
                # Bounded diagnostic; the next pass retries from durable state.
                self.last_error = type(exc).__name__

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('The scheduler clock returned an invalid time.')
        return float(value)

    def _open(self):
        if self.closing.is_set() or getattr(self.app, 'closed', False):
            raise ValueError('The local service is shutting down.')

    def _emit(self, kind, payload):
        try:
            self.app.emit(kind, payload)
        except Exception:
            pass  # Presentation cannot change durable job state.

    def _publish(self, notices):
        published = False
        for notice in notices:
            if notice:
                self._emit('job_notification', notice)
                published = True
        if published:
            self._outbox()

    def _outbox(self):
        """Tell the native outbox pump to pull; it derives every decision from durable state."""
        outbox = self.notifications
        if outbox is not None:
            outbox.changed()

    # Scheduling

    def tick(self, now=None):
        """One scheduler pass. Starts at most one root run."""
        outcome = {'launched': None, 'run_id': None, 'busy': None, 'deferred': {}, 'blocked': [], 'failed': [],
                   'skipped_late': []}
        with self.lock:
            now = self._now() if now is None else now
            if type(now) not in (int, float) or not math.isfinite(now):
                raise ValueError('The scheduler clock returned an invalid time.')
            if self.closing.is_set():
                outcome['busy'] = 'service_closing'
                return outcome
            self.refresh(now)
            asleep = self._housekeep(now)
            due = self.store.query("SELECT * FROM jobs WHERE state='active' AND next_due IS NOT NULL AND next_due<=? "
                                   "ORDER BY next_due,id LIMIT ?", (now, MAX_JOBS))
            if not due:
                return outcome
            with self.app.task_lock:
                # A native will-sleep hint defers like live work; the slots stay due and coalesce on wake.
                busy = 'system_sleep' if asleep else self._busy()
                if busy:
                    outcome['busy'] = busy
                    for job in due:
                        self._defer_or_skip(job, busy, now, outcome)
                    return outcome
                for index, job in enumerate(due):
                    result = self._fire(job, now)
                    if result['status'] == 'launched':
                        outcome.update(launched=result['fire_id'], run_id=result['run_id'])
                        # One root owner at a time; later due jobs coalesce and wait.
                        for later in due[index + 1:]:
                            self._defer_or_skip(later, 'foreground_run', now, outcome)
                        break
                    if result['status'] == 'deferred':
                        outcome['deferred'][job['id']] = result['reason']
                    elif result['status'] == 'blocked':
                        outcome['blocked'].append(job['id'])
                    elif result['status'] == 'failed':
                        outcome['failed'].append(result['fire_id'])
                    elif result['status'] == 'skipped_late':
                        outcome['skipped_late'].append(job['id'])
        return outcome

    def _busy(self):
        """Any live root, child, lane or approval defers every fire. Nothing is steered."""
        app = self.app
        if self.closing.is_set() or getattr(app, 'closed', False):
            return 'service_closing'
        controller = getattr(app, 'controller', None)
        if app.active or app.active_run or (controller is not None and controller.live is not None):
            return 'foreground_run'
        worker = getattr(app, 'worker', None)
        if worker is not None and worker.is_alive():
            return 'foreground_run'
        agents = getattr(app, 'agents', None)
        if agents is not None and agents.busy():
            return 'delegated_agents'
        if agents is not None and lane_busy(getattr(agents, 'lane', None)):
            return 'inference_lane'
        action_lock = getattr(app, 'action_lock', None)
        if action_lock is not None and action_lock.locked():
            return 'approval_in_progress'
        with self.store.lock:
            db = self.store.db
            if db.execute(f'SELECT 1 FROM runs WHERE state IN ({marks(ROOT_LIVE)}) LIMIT 1').fetchone():
                return 'live_or_waiting_run'
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                   "AND name IN ('run_continuations','agent_nodes')")}
            if 'run_continuations' in tables and db.execute(
                    "SELECT 1 FROM run_continuations WHERE state IN ('queued','dispatching') LIMIT 1").fetchone():
                return 'continuation_pending'
            if 'agent_nodes' in tables and db.execute(
                    f'SELECT 1 FROM agent_nodes WHERE status IN ({marks(CHILD_LIVE)}) LIMIT 1').fetchone():
                return 'delegated_agents'
        return None

    def _blocker(self, db, job_id):
        row = db.execute(f'SELECT status FROM job_fires WHERE job_id=? AND status IN ({marks(LIVE + ("attention",))}) '
                         'LIMIT 1', (job_id,)).fetchone()
        if row:
            return 'previous_fire_needs_attention' if row[0] == 'attention' else 'previous_fire_live'
        if db.execute('SELECT count(*) FROM job_notifications WHERE acknowledged IS NULL').fetchone()[0] >= MAX_PENDING_NOTICES:
            return 'notification_outbox_full'
        return None

    def _make_room(self, db, job_id):
        """Prune only settled rows without unacknowledged notifications; otherwise refuse."""
        excess = db.execute('SELECT count(*) FROM job_fires WHERE job_id=?', (job_id,)).fetchone()[0] - MAX_FIRES + 1
        if excess <= 0:
            return None
        victims = [row[0] for row in db.execute(
            f'SELECT id FROM job_fires WHERE job_id=? AND status IN ({marks(PRUNABLE)}) AND NOT EXISTS('
            'SELECT 1 FROM job_notifications n WHERE n.fire_id=job_fires.id AND n.acknowledged IS NULL) '
            'ORDER BY scheduled,claimed LIMIT ?', (job_id, excess))]
        if len(victims) < excess:
            return 'history_full'
        for fire_id in victims:
            db.execute('DELETE FROM job_notifications WHERE fire_id=?', (fire_id,))
            db.execute('DELETE FROM job_fires WHERE id=?', (fire_id,))
        return None

    def _defer(self, job_id, reason):
        self.store.query('UPDATE jobs SET deferred_reason=? WHERE id=? AND deferred_reason IS NOT ?',
                         (reason, job_id, reason))

    def _defer_or_skip(self, job, reason, now, outcome):
        """A skip starts no run, so a skip_if_late slot past its grace is consumed even while others wait."""
        schedule = json.loads(job['schedule'])
        if schedule.get('catch_up') == 'skip_if_late' and reason != 'service_closing':
            latest, missed = latest_slot(schedule, now, job['next_due'], job['anchor'])
            if latest is not None and late(schedule, latest, now):
                if self._skip_late(job, schedule, latest, missed, now)['status'] == 'skipped_late':
                    outcome['skipped_late'].append(job['id'])
                    return
        self._defer(job['id'], reason)

    def _fire(self, job, now):
        if self.closing.is_set() or getattr(self.app, 'closed', False):
            return {'status': 'deferred', 'reason': 'service_closing'}
        with self.store.lock:
            reason = self._blocker(self.store.db, job['id'])
        if reason:
            self._defer(job['id'], reason)
            return {'status': 'deferred', 'reason': reason}
        schedule = json.loads(job['schedule'])
        latest, missed = latest_slot(schedule, now, job['next_due'], job['anchor'])
        if latest is None:
            return {'status': 'skipped'}
        if late(schedule, latest, now):
            return self._skip_late(job, schedule, latest, missed, now)
        try:
            chat = self.app.chat(job['chat_id'])
        except ValueError:
            chat = None
        drift = self._drift(job, chat)
        if drift:
            self._block(job, latest, missed, drift, now)
            return {'status': 'blocked'}
        claim = self._claim(job, latest, missed, now)
        if claim['status'] != 'claimed':
            return claim
        fire_id = claim['fire_id']
        try:
            rid = self._bind(job, fire_id, chat, latest, missed, now)
        except Exception as exc:
            self._fail_fire(fire_id, 'The scheduled run could not be recorded (' + type(exc).__name__
                            + '). Nothing was started.', now)
            return {'status': 'failed', 'fire_id': fire_id}
        config = json.loads(job['pin'])['config']
        try:
            if self.closing.is_set() or getattr(self.app, 'closed', False):
                raise ValueError('The local service is shutting down.')
            self.app.controller.launch(chat, job['task'], config['effort'], [], rid)
        except Exception as exc:
            self._launch_failed(rid, exc)
            self._fail_fire(fire_id, 'The scheduled run could not start: ' + preview(str(exc) or type(exc).__name__), now)
            return {'status': 'failed', 'fire_id': fire_id, 'run_id': rid}
        with self.store.lock, self.store.db as db:
            db.execute("UPDATE job_fires SET status='running',updated=? WHERE id=? AND status='bound'", (now, fire_id))
        self._emit('job', {'chat_id': job['chat_id'], 'job_id': job['id'], 'fire_id': fire_id, 'run_id': rid,
                           'status': 'running', 'origin': 'host'})
        return {'status': 'launched', 'fire_id': fire_id, 'run_id': rid}

    def _claim(self, job, latest, missed, now):
        with self.store.lock, self.store.db as db:
            current = _one(db, 'SELECT * FROM jobs WHERE id=?', (job['id'],))
            if (not current or current['state'] != 'active' or current['revision'] != job['revision']
                    or current['next_due'] is None or current['next_due'] > now):
                return {'status': 'skipped'}
            schedule = json.loads(current['schedule'])
            if current['last_scheduled'] is not None and latest <= current['last_scheduled']:
                # Scheduled instants strictly increase; an earlier slot is never re-claimed.
                following = next_slot(schedule, max(now, current['last_scheduled']), current['anchor'])
                db.execute('UPDATE jobs SET next_due=?,state=?,updated=? WHERE id=?',
                           (following, 'active' if following is not None else 'finished', now, job['id']))
                return {'status': 'skipped'}
            reason = self._blocker(db, job['id']) or self._make_room(db, job['id'])
            if reason:
                db.execute('UPDATE jobs SET deferred_reason=? WHERE id=?', (reason, job['id']))
                return {'status': 'deferred', 'reason': reason}
            fire_id = uuid.uuid4().hex
            try:
                db.execute('INSERT INTO job_fires(id,job_id,chat_id,revision,scheduled,missed,origin,status,'
                           'pin_fingerprint,claimed,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (fire_id, job['id'], current['chat_id'], current['revision'], latest, missed, 'host',
                            'claimed', current['pin_fingerprint'], now, now))
            except sqlite3.IntegrityError:
                return {'status': 'skipped'}
            following = next_slot(schedule, max(latest, now), current['anchor'])
            db.execute('UPDATE jobs SET last_scheduled=?,next_due=?,state=?,deferred_reason=NULL,updated=? WHERE id=?',
                       (latest, following, 'active' if following is not None else 'finished', now, job['id']))
        return {'status': 'claimed', 'fire_id': fire_id}

    def _skip_late(self, job, schedule, latest, missed, now):
        """skip_if_late past its grace: consume the slot without a run, so it is never claimed later."""
        notice = None
        with self.store.lock, self.store.db as db:
            current = _one(db, 'SELECT * FROM jobs WHERE id=?', (job['id'],))
            if (not current or current['state'] != 'active' or current['revision'] != job['revision']
                    or current['next_due'] is None or current['next_due'] > now):
                return {'status': 'skipped'}
            if current['last_scheduled'] is not None and latest <= current['last_scheduled']:
                following = next_slot(schedule, max(now, current['last_scheduled']), current['anchor'])
                db.execute('UPDATE jobs SET next_due=?,state=?,updated=? WHERE id=?',
                           (following, 'active' if following is not None else 'finished', now, job['id']))
                return {'status': 'skipped'}
            following = next_slot(schedule, max(latest, now), current['anchor'])
            state = 'active' if following is not None else 'finished'
            db.execute("UPDATE jobs SET last_scheduled=?,next_due=?,state=?,deferred_reason='skipped_late',updated=? "
                       'WHERE id=?', (latest, following, state, now, job['id']))
            outbox = self.notifications
            if outbox is not None:
                body = (f'The time {iso(latest)} was more than {duration(schedule["grace_seconds"])} late, so it did not '
                        'run, as this schedule asks.')
                if missed:
                    body += f' {missed} earlier ' + plural(missed, 'time was', 'times were') + ' skipped with it.'
                body += f' Next: {iso(following)}.' if following is not None else ' No later time remains.'
                notice = outbox.host_notice(db, 'late_skipped', job['chat_id'], job['id'],
                                            digest('late', job['id'], job['revision'], latest),
                                            f'Scheduled job “{job["label"]}” skipped a late time', body, now)
        self._publish([notice])
        self._emit('job', {'chat_id': job['chat_id'], 'job_id': job['id'], 'state': state, 'skipped': iso(latest)})
        return {'status': 'skipped_late', 'scheduled': latest, 'missed': missed}

    def _bind(self, job, fire_id, chat, scheduled, missed, now):
        """The run row, its journal entries, the transcript label and the binding commit together."""
        config = json.loads(job['pin'])['config']
        rid, mid, wall = uuid.uuid4().hex, uuid.uuid4().hex, time.time()
        label = f'Scheduled job “{job["label"]}” started for {iso(scheduled)}. This run was started by its reviewed schedule, not by a new message from you.'
        if missed:
            label += f' {missed} earlier scheduled time' + (' was' if missed == 1 else 's were') + ' combined into this run.'
        trigger = {'origin': 'host', 'job_origin': 'human', 'authorization': 'none', 'job_id': job['id'],
                   'fire_id': fire_id, 'revision': job['revision'], 'scheduled': iso(scheduled), 'missed': missed,
                   'pin_fingerprint': job['pin_fingerprint']}
        with self.store.lock, self.store.db as db:
            row = db.execute('SELECT status,run_id FROM job_fires WHERE id=?', (fire_id,)).fetchone()
            if not row or row[0] != 'claimed' or row[1] is not None:
                raise ValueError('The scheduled fire is no longer claimable.')
            # Same row shape as RunJournal.create, inside this binding transaction.
            # The runs_one_owner index still refuses a second live root.
            db.execute('INSERT INTO runs(id,chat_id,goal,state,config,checkpoint,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                       (rid, chat['id'], job['task'], 'queued', encoded(config), encoded({'sources': []}), wall, wall))
            RunJournal.event(db, rid, 'created', {'limits': config['limits']})
            RunJournal.event(db, rid, 'scheduled_trigger', trigger)
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (mid, chat['id'], 'assistant', label, wall))
            db.execute('UPDATE chats SET updated=? WHERE id=?', (wall, chat['id']))
            db.execute("UPDATE job_fires SET status='bound',run_id=?,run_state='queued',label_message_id=?,updated=? "
                       "WHERE id=?", (rid, mid, now, fire_id))
        return rid

    def _launch_failed(self, rid, exc):
        app, controller = self.app, self.app.controller
        if controller.live is not None and controller.live.run_id == rid:
            controller.live = None
        if app.active_run == rid:
            app.active, app.active_run = None, None
        try:
            if app.runs.get(rid)['state'] == 'queued':
                app.runs.transition(rid, 'failed', error=preview(str(exc)) or type(exc).__name__)
        except Exception:
            pass  # Restart recovery classifies a still-live row as interrupted.

    def _fail_fire(self, fire_id, reason, now):
        with self.store.lock, self.store.db as db:
            fire = _one(db, 'SELECT f.*,j.label FROM job_fires f JOIN jobs j ON j.id=f.job_id WHERE f.id=?', (fire_id,))
            if not fire or fire['status'] not in ('claimed', 'bound'):
                return
            run = db.execute('SELECT state FROM runs WHERE id=?', (fire['run_id'],)).fetchone() if fire['run_id'] else None
            db.execute("UPDATE job_fires SET status='failed',reason=?,run_state=?,updated=? WHERE id=?",
                       (reason, run[0] if run else None, now, fire_id))
            notice = self._notice(db, fire, 'failed', 'failed', digest('start_failed', reason),
                                  f'Scheduled job “{fire["label"]}” could not start', reason, now)
        self._publish([notice])

    def _block(self, job, latest, missed, drift, now):
        reason = ('Changed since review: ' + ', '.join(DRIFT.get(key, key) for key in drift)
                  + '. Preview and update the job to review the current configuration.')
        with self.store.lock, self.store.db as db:
            current = _one(db, 'SELECT * FROM jobs WHERE id=?', (job['id'],))
            if (not current or current['state'] != 'active' or current['revision'] != job['revision']
                    or current['last_scheduled'] is not None and latest <= current['last_scheduled']):
                return
            notice = None
            if not self._make_room(db, job['id']):
                fire = {'id': uuid.uuid4().hex, 'job_id': job['id'], 'chat_id': job['chat_id'], 'run_id': None}
                db.execute('INSERT INTO job_fires(id,job_id,chat_id,revision,scheduled,missed,origin,status,reason,'
                           'pin_fingerprint,claimed,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (fire['id'], job['id'], job['chat_id'], job['revision'], latest, missed, 'host', 'blocked',
                            reason, job['pin_fingerprint'], now, now))
                notice = self._notice(db, fire, 'blocked', 'action_needed', digest('drift', *drift),
                                      f'Scheduled job “{job["label"]}” needs review', reason, now)
            db.execute("UPDATE jobs SET state='needs_review',review_reason=?,last_scheduled=?,next_due=NULL,"
                       'deferred_reason=NULL,updated=? WHERE id=?', (reason, latest, now, job['id']))
        self._publish([notice])
        self._emit('job', {'chat_id': job['chat_id'], 'job_id': job['id'], 'state': 'needs_review'})

    def _drift(self, job, chat):
        if chat is None:
            return ['conversation']
        pinned = json.loads(job['pin'])
        try:
            current, _ = self._pin(chat, pinned['config']['effort'], pinned['config']['limits'])
        except Exception:
            return ['workspace_unavailable' if chat.get('workspace') else 'configuration_unavailable']
        keys = set(pinned['config']) | set(current['config'])

        def value(config, key):
            # A permission change already reports as 'permissions'; the authz pin
            # adds only the tool-catalogue identity (a missing pin still drifts).
            if key == 'authz' and isinstance(config.get(key), dict):
                return config[key].get('catalogue_digest')
            return config.get(key)
        changed = sorted(key for key in keys if value(pinned['config'], key) != value(current['config'], key))
        if pinned['workspace'] != current['workspace'] and 'workspace' not in changed and 'workspace_identity' not in changed:
            changed.append('workspace_identity')
        return changed

    def refresh(self, now=None):
        """Settle tracked fires from authoritative run states and emit notifications."""
        with self.lock:
            now = self._now() if now is None else now
            settled = []
            with self.store.lock, self.store.db as db:
                fires = _rows(db, f'''SELECT f.*,j.label,j.monitor,j.last_fingerprint,r.state AS current,
                    r.error AS run_error,r.updated AS run_updated FROM job_fires f JOIN jobs j ON j.id=f.job_id
                    LEFT JOIN runs r ON r.id=f.run_id WHERE f.status IN ({marks(TRACKED)})''')
                for fire in fires:
                    status = run_status(fire['current'])
                    if status == fire['status']:
                        if fire['current'] != fire['run_state']:
                            db.execute('UPDATE job_fires SET run_state=?,updated=? WHERE id=?',
                                       (fire['current'], now, fire['id']))
                        continue
                    settled.append((fire, status, self._settle(db, fire, status, now)))
            self._publish([notice for _, _, notice in settled])
            for fire, status, _ in settled:
                self._emit('job', {'chat_id': fire['chat_id'], 'job_id': fire['job_id'], 'fire_id': fire['id'],
                                   'run_id': fire['run_id'], 'status': status})

    def _settle(self, db, fire, status, now):
        current, label = fire['current'], fire['label']
        reason, result, notice, category = fire['reason'], fire['result_fingerprint'], None, None
        if status in ('waiting', 'attention'):
            reason = ATTENTION.get(current, 'The scheduled run needs your attention in its conversation.')
            notice = ('action_needed', digest(status, current, fire['run_updated']),
                      f'Scheduled job “{label}” needs attention', reason)
        elif status == 'completed':
            text = self._completion_text(db, fire)
            result, reason = digest(' '.join(text.split())), None
            db.execute('UPDATE jobs SET last_fingerprint=? WHERE id=?', (result, fire['job_id']))
            # A monitor stays quiet while its normalized result is unchanged.
            if not fire['monitor'] or fire['last_fingerprint'] != result:
                changed = fire['monitor'] and fire['last_fingerprint'] is not None
                category = 'monitor_changed' if changed else None
                notice = ('completed', result, f'Scheduled job “{label}” ' + ('result changed' if changed else 'completed'),
                          preview(text) or 'The run completed without a text answer.')
        elif status == 'failed':
            reason = preview(fire['run_error'] or 'The scheduled run failed.')
            notice = ('failed', digest('failed', fire['run_error']), f'Scheduled job “{label}” failed', reason)
        elif status == 'cancelled':
            reason = 'The scheduled run was stopped.' if current else 'The scheduled run record was removed.'
        db.execute('UPDATE job_fires SET status=?,run_state=?,reason=?,result_fingerprint=?,updated=? WHERE id=?',
                   (status, current, reason, result, now, fire['id']))
        return self._notice(db, fire, status, *notice, now, category=category) if notice else None

    def _completion_text(self, db, fire):
        row = db.execute('SELECT checkpoint,created,updated FROM runs WHERE id=?', (fire['run_id'],)).fetchone()
        if not row:
            return ''
        try:
            snapshot = json.loads(row[0]) if row[0] else {}
        except ValueError:
            snapshot = {}
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        if isinstance(snapshot.get('message_id'), str):
            found = db.execute("SELECT content FROM messages WHERE id=? AND chat_id=? AND role='assistant'",
                               (snapshot['message_id'], fire['chat_id'])).fetchone()
            if found and isinstance(found[0], str):
                return found[0]
        found = db.execute("SELECT content FROM messages WHERE chat_id=? AND role='assistant' AND id IS NOT ? "
                           'AND created>=? AND created<=? ORDER BY created DESC LIMIT 1',
                           (fire['chat_id'], fire['label_message_id'], row[1], row[2])).fetchone()
        if found and isinstance(found[0], str):
            return found[0]
        return snapshot['answer'] if isinstance(snapshot.get('answer'), str) else ''

    def _notice(self, db, fire, status, kind, fingerprint, title, body, now, category=None):
        """Durable per fire/status/fingerprint; a repeat is ignored, never re-sent.

        An attached native outbox records its delivery row in this same transaction.
        """
        row = {'id': uuid.uuid4().hex, 'job_id': fire['job_id'], 'fire_id': fire['id'], 'chat_id': fire['chat_id'],
               'run_id': fire.get('run_id'), 'kind': kind, 'status': status, 'fingerprint': fingerprint,
               'title': title[:200], 'body': preview(body), 'created': now, 'acknowledged': None}
        cursor = db.execute(f'INSERT OR IGNORE INTO job_notifications({",".join(row)}) VALUES({",".join("?" * len(row))})',
                            tuple(row.values()))
        if cursor.rowcount != 1:
            return None
        outbox = self.notifications
        if outbox is not None:
            outbox.enqueue(db, row, category or kind, now)
        excess = db.execute('SELECT count(*) FROM job_notifications').fetchone()[0] - MAX_NOTICES
        if excess > 0:
            db.execute('DELETE FROM job_notifications WHERE id IN (SELECT id FROM job_notifications '
                       'WHERE acknowledged IS NOT NULL ORDER BY created LIMIT ?)', (excess,))
        return notice_view(row)

    # Lifecycle observation and quiet host notices (Phase 17)

    def _housekeep(self, now):
        """One lifecycle sample plus informational notices. Returns True while a will-sleep hint holds.

        Evidence only: a failure here is kept in last_error and never forces or blocks a fire.
        """
        lifecycle, outbox, notices, asleep = self.lifecycle, self.notifications, [], False
        try:
            if lifecycle is not None:
                seen = lifecycle.observe()
                asleep = seen['sleeping'] is True
                if outbox is not None:
                    notices += [self._wake_notice(outbox, window, now) for window in seen['wakes']]
                    notices += [self._clock_notice(outbox, change, now) for change in seen['changes']
                                if change['kind'] == 'clock_backward']
            if outbox is not None:
                notices += self._gap_notices(outbox, now)
        except Exception as exc:
            self.last_error = type(exc).__name__
        self._publish(notices)
        return asleep

    def _wake_notice(self, outbox, window, now):
        """One coalesced summary per wake: counts only, so no other conversation's text appears."""
        rows = self.store.query("SELECT schedule,next_due,anchor FROM jobs WHERE state='active' AND next_due IS NOT NULL "
                                'AND next_due<=? ORDER BY next_due,id LIMIT ?', (now, MAX_JOBS))
        ran = skipped = folded = 0
        for job in rows:
            schedule = json.loads(job['schedule'])
            latest, missed = latest_slot(schedule, now, job['next_due'], job['anchor'])
            if latest is None or latest <= window['started']:
                continue  # nothing came due while asleep; an earlier deferral is not a sleep effect
            if late(schedule, latest, now):
                skipped += 1
            else:
                ran, folded = ran + 1, folded + missed
        count = ran + skipped
        if not count:
            return None
        body = (f'While {machine()} slept (about {duration(window["seconds"])}), {count} '
                + plural(count, 'schedule was', 'schedules were') + ' due.')
        if ran:
            body += f' {ran} ' + plural(ran, 'runs once, at its', 'run once, at their') + ' latest due time.'
        if folded:
            body += f' {folded} earlier ' + plural(folded, 'time was', 'times were') + ' combined, not replayed.'
        if skipped:
            body += (f' {skipped} ' + plural(skipped, 'was', 'were') + ' skipped because '
                     + plural(skipped, 'it was later than its', 'they were later than their') + ' late limit.')
        with self.store.lock, self.store.db as db:
            return outbox.host_notice(db, 'wake_summary', None, None, digest('wake', window['id']),
                                      f'Schedules while {machine()} slept', body, now)

    def _clock_notice(self, outbox, change, now):
        if not self.store.query("SELECT 1 FROM jobs WHERE state='active' LIMIT 1"):
            return None
        body = (f'The system clock moved back by about {duration(-change["seconds"])}. Schedules wait until the clock '
                'reaches their next time again; nothing runs twice.')
        with self.store.lock, self.store.db as db:
            return outbox.host_notice(db, 'clock_changed', None, None, digest('clock', change['id']),
                                      'The system clock moved back', body, now)

    def _gap_notices(self, outbox, now):
        """A daily time that a DST gap removed today or yesterday gets one quiet notice per job and day."""
        notices = []
        for job in self.store.query("SELECT id,chat_id,label,schedule,anchor FROM jobs WHERE state='active' "
                                    'ORDER BY created,id LIMIT ?', (MAX_JOBS,)):
            schedule = json.loads(job['schedule'])
            for day, moment in gap_days(schedule, job['anchor'], now):
                following = next_slot(schedule, moment, job['anchor'])
                body = (f'{schedule["timezone"]} has no {schedule["time"]} on {day.isoformat()} because the clocks '
                        'moved forward, so this daily schedule did not run that day.'
                        + (f' Next: {iso(following)}.' if following is not None else ''))
                with self.store.lock, self.store.db as db:
                    notices.append(outbox.host_notice(
                        db, 'dst_skipped', job['chat_id'], job['id'],
                        digest('dst', job['id'], day.isoformat(), schedule['time'], schedule['timezone']),
                        f'Scheduled job “{job["label"]}” skipped {day.isoformat()}', body, now))
        return notices

    def recover(self):
        """Service start: an unbound claim never started a run and is not replayed."""
        with self.lock:
            now, notices = self._now(), []
            reason = ('The service stopped after claiming this scheduled time and before recording its run. '
                      'Nothing was started, and it will not be replayed automatically.')
            with self.store.lock, self.store.db as db:
                for fire in _rows(db, "SELECT f.*,j.label FROM job_fires f JOIN jobs j ON j.id=f.job_id "
                                      "WHERE f.status='claimed'"):
                    db.execute("UPDATE job_fires SET status='needs_review',reason=?,updated=? WHERE id=?",
                               (reason, now, fire['id']))
                    notices.append(self._notice(db, fire, 'needs_review', 'action_needed', digest('unbound', fire['id']),
                                                f'Scheduled job “{fire["label"]}” needs review', reason, now))
            self._publish(notices)
            # Bound runs were already classified by RunJournal.recover.
            self.refresh(now)

    # Native operations

    def operation(self, operation, **params):
        """The only mutation entry point. The parent routes it from native RPC only."""
        if not isinstance(operation, str) or operation not in OPERATIONS:
            raise ValueError('Unknown job operation.')
        forbidden = sorted(set(params) & NATIVE_ONLY)
        if forbidden:
            raise ValueError('Permissions, credentials, origin and runtime configuration are computed by the host '
                             'when you review a job. Remove: ' + ', '.join(forbidden) + '.')
        required, optional = OPERATIONS[operation]
        if not required <= set(params) or set(params) - required - optional:
            raise ValueError(f'The {operation} job operation accepts ' + ', '.join(sorted(required | optional)) + '.')
        with self.lock:
            return getattr(self, '_' + operation)(**params)

    def _chat(self, chat_id):
        return self.app.chat(ident(chat_id, 'Conversation not found.'))

    def _effort(self, effort):
        if effort is not None and (not isinstance(effort, str) or not 1 <= len(effort) <= 32):
            raise ValueError('Choose a named effort level or leave it unset.')
        profile = self.app.model.current() if hasattr(self.app.model, 'current') else None
        if profile:
            from .capabilities import choose
            return choose(profile, effort)
        return False if effort is None else effort

    @staticmethod
    def _limits(limits):
        return RunLimits.parse(limits).as_dict()

    def _pin(self, chat, effort, limits):
        config = self.app.runtime_config(chat, effort, limits)
        identity = None
        if chat.get('workspace'):
            root = Workspace(chat['workspace'])
            identity = {'root': str(root.root), 'device': root.identity[0], 'inode': root.identity[1]}
        pin = {'config': config, 'workspace': identity}
        text = json.dumps(pin, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        return pin, hashlib.sha256(text.encode()).hexdigest()

    def _job_row(self, chat_id, job_id):
        ident(chat_id, MISSING)
        ident(job_id, MISSING)
        rows = self.store.query('SELECT * FROM jobs WHERE id=? AND chat_id=?', (job_id, chat_id))
        if not rows:
            raise ValueError(MISSING)
        return rows[0]

    def _revised(self, chat_id, job_id, revision):
        job = self._job_row(chat_id, job_id)
        if type(revision) is not int or revision != job['revision']:
            raise ValueError(STALE)
        return job

    def _fire_row(self, chat_id, job_id, fire_id):
        self._job_row(chat_id, job_id)
        ident(fire_id, 'Fire not found for this job.')
        rows = self.store.query('SELECT * FROM job_fires WHERE id=? AND job_id=? AND chat_id=?', (fire_id, job_id, chat_id))
        if not rows:
            raise ValueError('Fire not found for this job.')
        return rows[0]

    def _view(self, row, pending=0, current=None):
        return {'id': row['id'], 'chat_id': row['chat_id'], 'origin': row['origin'], 'label': row['label'],
                'task': row['task'], 'schedule': json.loads(row['schedule']), 'monitor': bool(row['monitor']),
                'state': row['state'], 'revision': row['revision'], 'next_due': iso(row['next_due']),
                'last_scheduled': iso(row['last_scheduled']), 'review_reason': row['review_reason'],
                'deferred_reason': row['deferred_reason'], 'config': summary(json.loads(row['pin'])),
                'config_fingerprint': row['pin_fingerprint'], 'pending_notifications': pending,
                'current_fire': current, 'created': iso(row['created']), 'updated': iso(row['updated'])}

    def _changed(self, job_id):
        row = self.store.query('SELECT * FROM jobs WHERE id=?', (job_id,))[0]
        self._emit('job', {'chat_id': row['chat_id'], 'job_id': job_id, 'state': row['state'], 'revision': row['revision']})
        self._outbox()  # a pause withdraws this job's pending and shown banners
        self.wake()
        return self._view(row)

    def _write(self, job_id, revision, assignments, args=()):
        now = self._now()
        with self.store.lock, self.store.db as db:
            cursor = db.execute(f'UPDATE jobs SET {assignments},revision=revision+1,deferred_reason=NULL,updated=? '
                                'WHERE id=? AND revision=?', (*args, now, job_id, revision))
            if cursor.rowcount != 1:
                raise ValueError(STALE)

    def _preview(self, chat_id, schedule=None, effort=None, limits=None):
        chat = self._chat(chat_id)
        now = self._now()
        pin, fingerprint = self._pin(chat, self._effort(effort), self._limits(limits))
        result = {'config': summary(pin), 'config_fingerprint': fingerprint}
        if schedule is not None:
            normalized, times, after = parse_schedule(schedule, now), [], now
            while len(times) < 5:
                after = next_slot(normalized, after, now)
                if after is None:
                    break
                times.append(iso(after))
            result.update(schedule=normalized, next_times=times)
        return result

    def _create(self, chat_id, label, task, schedule, config_fingerprint, monitor=False, effort=None, limits=None):
        self._open()
        label, task = clean_label(label), clean_task(task)
        if type(monitor) is not bool:
            raise ValueError('monitor must be true or false.')
        if not isinstance(config_fingerprint, str) or not FINGERPRINT.fullmatch(config_fingerprint):
            raise ValueError('Preview the job and pass its config_fingerprint.')
        now = self._now()
        normalized = parse_schedule(schedule, now)
        chat = self._chat(chat_id)
        pin, fingerprint = self._pin(chat, self._effort(effort), self._limits(limits))
        if fingerprint != config_fingerprint:
            raise ValueError(REVIEW_CHANGED)
        first = next_slot(normalized, now, now)
        if first is None:
            raise ValueError('This schedule has no future time.')
        job_id = uuid.uuid4().hex
        with self.store.lock, self.store.db as db:
            if db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= MAX_JOBS:
                raise ValueError(f'Keep at most {MAX_JOBS} scheduled jobs. Delete one first.')
            db.execute('INSERT INTO jobs(id,chat_id,origin,label,task,schedule,monitor,state,revision,pin,pin_fingerprint,'
                       'anchor,next_due,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (job_id, chat['id'], 'human', label, task, encoded(normalized), int(monitor), 'active', 1,
                        encoded(pin), fingerprint, now, first, now, now))
        return self._changed(job_id)

    def _update(self, chat_id, job_id, revision, config_fingerprint, **changes):
        self._open()
        job = self._revised(chat_id, job_id, revision)
        if not isinstance(config_fingerprint, str) or not FINGERPRINT.fullmatch(config_fingerprint):
            raise ValueError('Preview the job and pass its config_fingerprint.')
        label = clean_label(changes['label']) if 'label' in changes else job['label']
        task = clean_task(changes['task']) if 'task' in changes else job['task']
        monitor = changes['monitor'] if 'monitor' in changes else bool(job['monitor'])
        if type(monitor) is not bool:
            raise ValueError('monitor must be true or false.')
        now = self._now()
        pinned = json.loads(job['pin'])['config']
        effort = self._effort(changes['effort']) if 'effort' in changes else pinned['effort']
        limits = self._limits(changes['limits']) if 'limits' in changes else pinned['limits']
        if 'schedule' in changes:
            schedule, anchor = parse_schedule(changes['schedule'], now), now
        else:
            schedule, anchor = json.loads(job['schedule']), job['anchor']
        pin, fingerprint = self._pin(self._chat(chat_id), effort, limits)
        if fingerprint != config_fingerprint:
            raise ValueError(REVIEW_CHANGED)
        floor = now if job['last_scheduled'] is None else max(now, job['last_scheduled'])
        following = next_slot(schedule, floor, anchor)
        if following is None:
            raise ValueError('This schedule has no future time. Choose a new schedule.')
        # A live fire keeps its own pin and run; an update never steers it.
        state = 'paused' if job['state'] == 'paused' else 'active'
        self._write(job_id, revision, 'label=?,task=?,schedule=?,monitor=?,state=?,pin=?,pin_fingerprint=?,anchor=?,'
                    'next_due=?,last_fingerprint=CASE WHEN task=? THEN last_fingerprint END,review_reason=NULL',
                    (label, task, encoded(schedule), int(monitor), state, encoded(pin), fingerprint, anchor,
                     None if state == 'paused' else following, task))
        return self._changed(job_id)

    def _pause(self, chat_id, job_id, revision):
        self._open()
        job = self._revised(chat_id, job_id, revision)
        if job['state'] not in ('active', 'needs_review'):
            raise ValueError('Only an active job or one awaiting review can be paused.')
        self._write(job_id, revision, "state='paused',next_due=NULL")
        return self._changed(job_id)

    def _resume(self, chat_id, job_id, revision):
        self._open()
        job = self._revised(chat_id, job_id, revision)
        if job['state'] not in ('paused', 'needs_review'):
            raise ValueError('Only a paused job or one awaiting review can be resumed.')
        try:
            chat = self.app.chat(chat_id)
        except ValueError:
            chat = None
        drift = self._drift(job, chat)
        if drift:
            raise ValueError('Review the changed configuration first (' + ', '.join(DRIFT.get(k, k) for k in drift)
                             + '). Preview and update the job to accept the current configuration.')
        now = self._now()
        floor = now if job['last_scheduled'] is None else max(now, job['last_scheduled'])
        # Slots missed while paused are skipped, not replayed.
        following = next_slot(json.loads(job['schedule']), floor, job['anchor'])
        if following is None:
            raise ValueError('This job has no future time. Update its schedule.')
        self._write(job_id, revision, "state='active',next_due=?,review_reason=NULL", (following,))
        return self._changed(job_id)

    def _delete(self, chat_id, job_id, revision):
        self._open()
        self._revised(chat_id, job_id, revision)
        with self.store.lock, self.store.db as db:
            if db.execute(f'SELECT 1 FROM job_fires WHERE job_id=? AND status IN ({marks(LIVE)}) LIMIT 1',
                          (job_id,)).fetchone():
                raise ValueError('Stop or settle this job’s scheduled run before deleting the job.')
            if db.execute('DELETE FROM jobs WHERE id=? AND revision=?', (job_id, revision)).rowcount != 1:
                raise ValueError(STALE)
            db.execute('DELETE FROM job_notifications WHERE job_id=?', (job_id,))
            db.execute('DELETE FROM job_fires WHERE job_id=?', (job_id,))
            if self.notifications is not None:
                db.execute('DELETE FROM host_notices WHERE job_id=?', (job_id,))
        self._emit('job', {'chat_id': chat_id, 'job_id': job_id, 'state': 'deleted'})
        self._outbox()
        return {'job_id': job_id, 'deleted': True}

    def _list(self, chat_id=None):
        where, args = '', ()
        if chat_id is not None:
            where, args = ' WHERE chat_id=?', (ident(chat_id, 'Conversation not found.'),)
        rows = self.store.query('SELECT * FROM jobs' + where + ' ORDER BY created,id LIMIT ?', args + (MAX_JOBS,))
        pending = {r['job_id']: r['n'] for r in self.store.query(
            'SELECT job_id,count(*) AS n FROM job_notifications WHERE acknowledged IS NULL GROUP BY job_id')}
        current = {r['job_id']: fire_view(r) for r in self.store.query(
            f'SELECT * FROM job_fires WHERE status IN ({marks(TRACKED)})')}
        return {'jobs': [self._view(row, pending.get(row['id'], 0), current.get(row['id'])) for row in rows],
                'scheduler': {'running': bool(self.thread and self.thread.is_alive()),
                              'closing': self.closing.is_set(), 'last_error': self.last_error}}

    def _get(self, chat_id, job_id):
        row = self._job_row(chat_id, job_id)
        try:
            chat = self.app.chat(chat_id)
        except ValueError:
            chat = None
        fires = self.store.query('SELECT * FROM job_fires WHERE job_id=? ORDER BY scheduled DESC,claimed DESC LIMIT ?',
                                 (job_id, MAX_FIRES))
        pending = self.store.query('SELECT count(*) AS n FROM job_notifications WHERE job_id=? AND acknowledged IS NULL',
                                   (job_id,))[0]['n']
        return {'job': self._view(row, pending), 'fires': [fire_view(f) for f in fires], 'drift': self._drift(row, chat)}

    def _notifications(self, chat_id=None, pending=True, limit=100):
        if type(pending) is not bool:
            raise ValueError('pending must be true or false.')
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Inspect 1 to 200 notifications.')
        clauses, args = [], []
        if chat_id is not None:
            clauses.append('chat_id=?')
            args.append(ident(chat_id, 'Conversation not found.'))
        filters = clauses + (['acknowledged IS NULL'] if pending else [])
        rows = self.store.query('SELECT * FROM job_notifications' + (' WHERE ' + ' AND '.join(filters) if filters else '')
                                + ' ORDER BY created DESC,rowid DESC LIMIT ?', (*args, limit))
        count = self.store.query('SELECT count(*) AS n FROM job_notifications WHERE '
                                 + ' AND '.join(clauses + ['acknowledged IS NULL']), tuple(args))[0]['n']
        return {'notifications': [notice_view(r) for r in rows], 'pending': count}

    def _ack(self, notification_ids, chat_id=None):
        self._open()
        if (not isinstance(notification_ids, list) or not 1 <= len(notification_ids) <= 100
                or len(set(map(repr, notification_ids))) != len(notification_ids)):
            raise ValueError('Acknowledge 1 to 100 distinct notifications.')
        for value in notification_ids:
            ident(value, 'Notification not found.')
        if chat_id is not None:
            ident(chat_id, 'Notification not found.')
        now, marks_ = self._now(), ','.join('?' * len(notification_ids))
        with self.store.lock, self.store.db as db:
            found = _rows(db, f'SELECT id,chat_id FROM job_notifications WHERE id IN ({marks_})', tuple(notification_ids))
            if len(found) != len(notification_ids) or (chat_id is not None and any(r['chat_id'] != chat_id for r in found)):
                raise ValueError('Notification not found.')
            changed = db.execute(f'UPDATE job_notifications SET acknowledged=? WHERE acknowledged IS NULL AND id IN ({marks_})',
                                 (now, *notification_ids)).rowcount
            excess = db.execute('SELECT count(*) FROM job_notifications').fetchone()[0] - MAX_NOTICES
            if excess > 0:
                db.execute('DELETE FROM job_notifications WHERE id IN (SELECT id FROM job_notifications '
                           'WHERE acknowledged IS NOT NULL ORDER BY created LIMIT ?)', (excess,))
        if changed:
            self._outbox()  # an acknowledged notice's banner is withdrawn
        return {'acknowledged': changed, 'already_acknowledged': len(notification_ids) - changed}

    def _cancel_fire(self, chat_id, job_id, fire_id):
        self._open()
        fire = self._fire_row(chat_id, job_id, fire_id)
        if fire['status'] not in ('running', 'waiting', 'attention') or not fire['run_id']:
            raise ValueError('This fire has no scheduled run that can be stopped.')
        # Only the linked run is addressed; App.stop refuses any other active owner.
        self.app.stop(chat_id, fire['run_id'])
        self.refresh()
        return {'fire_id': fire_id, 'run_id': fire['run_id'], 'status': 'stop_requested'}

    def _dismiss_fire(self, chat_id, job_id, fire_id):
        self._open()
        fire = self._fire_row(chat_id, job_id, fire_id)
        if fire['status'] not in ('attention', 'needs_review'):
            raise ValueError('Only a fire that needs attention or review can be dismissed.')
        reason = ((fire['reason'] or '') + ' Dismissed natively; the linked run, if any, is unchanged.').strip()
        with self.store.lock, self.store.db as db:
            if db.execute("UPDATE job_fires SET status='dismissed',reason=?,updated=? WHERE id=? AND status=?",
                          (reason, self._now(), fire_id, fire['status'])).rowcount != 1:
                raise ValueError(STALE)
        self.wake()
        return fire_view(self.store.query('SELECT * FROM job_fires WHERE id=?', (fire_id,))[0])

    # Integration helpers

    def scheduled_run(self, rid):
        """Association for runner/UI decisions; None for ordinary runs."""
        rows = self.store.query('SELECT id,job_id,revision,scheduled,origin,pin_fingerprint FROM job_fires WHERE run_id=?',
                                (rid,))
        if not rows:
            return None
        row = rows[0]
        return {'fire_id': row['id'], 'job_id': row['job_id'], 'revision': row['revision'],
                'scheduled': iso(row['scheduled']), 'origin': row['origin'], 'pin_fingerprint': row['pin_fingerprint']}

    def forget(self, cid):
        """Delete-chat support. The caller deletes the conversation's runs afterwards."""
        with self.lock, self.store.lock, self.store.db as db:
            for table in ('job_notifications', 'job_fires', 'jobs'):
                db.execute(f'DELETE FROM {table} WHERE chat_id=?', (cid,))
            if self.notifications is not None:
                db.execute('DELETE FROM host_notices WHERE chat_id=?', (cid,))
        self._outbox()
