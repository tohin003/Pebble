"""Sleep, wake, wall-clock and time-zone observation for the scheduler (Phase 17, D06).

The backend detects lifecycle changes itself from three clocks sampled on each
scheduler pass, so every rule here is testable with injected clocks:

- wall: time.time(), which a person, NTP or a time-zone change can move;
- awake: a monotonic clock that stops while the machine sleeps (time.monotonic,
  which is mach_absolute_time on macOS and CLOCK_MONOTONIC on Linux);
- total: a monotonic clock that keeps counting through sleep (CLOCK_MONOTONIC
  on macOS, CLOCK_BOOTTIME on Linux).

Between two samples, total minus awake is time asleep, and wall minus total is
a wall-clock change. The native shell may also report NSWorkspace will-sleep
and did-wake and NSSystemTimeZoneDidChange through a Rust-only call path; those
hints only speed detection, and a will-sleep hint expires on its own. A wake
window yields at most one coalesced summary (see Jobs); the existing missed-slot
coalescing still decides what fires. Observations hold kinds, numbers and a
validated zone name, never task content. Nothing here starts, cancels, resumes
or replays a run.

Phase 24 (t24-auth, V1-068): a wake (did_wake, or a sleep the clocks show) is
also passed to open model streams (recovery.note_wake): a stream that sends
nothing within recovery.WAKE_GRACE after it is abandoned as a retryable stall,
which only asks the model again (a model request never runs a tool).
"""

import json
import math
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from .jobs import iso, time_zone
from .recovery import note_wake


SLEEP_MIN = 2.0  # seconds asleep below this is clock noise, as in time_ledger
CLOCK_JUMP = 120.0  # wall minus sleep-including monotonic beyond +/- this is a clock change
HINT_HOLD = 60.0  # awake seconds after a native will-sleep before the hint lapses
MAX_OBSERVATIONS = 256
KINDS = ('sleep', 'clock_forward', 'clock_backward', 'will_sleep', 'did_wake', 'timezone_changed')
# Rust-only lifecycle_event payloads: each kind takes exactly these fields.
EVENTS = {'will_sleep': set(), 'did_wake': set(), 'timezone_changed': {'timezone'}}
LIFECYCLE_DDL = '''
CREATE TABLE IF NOT EXISTS lifecycle_observations(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('sleep','clock_forward','clock_backward','will_sleep','did_wake','timezone_changed')),
  source TEXT NOT NULL CHECK(source IN ('clock','native')),
  observed REAL NOT NULL, started REAL, seconds REAL NOT NULL, zone TEXT);
'''  # ordered by rowid: the wall clock it records can move backwards


def system_clocks():
    """(wall, awake, total). awake excludes sleep; total includes it where the OS offers such a clock."""
    wall, awake = time.time(), time.monotonic()
    if sys.platform == 'darwin':
        total = time.clock_gettime(time.CLOCK_MONOTONIC)  # Darwin's CLOCK_MONOTONIC keeps counting asleep
    elif hasattr(time, 'CLOCK_BOOTTIME'):
        total = time.clock_gettime(time.CLOCK_BOOTTIME)
    else:
        total = awake  # no sleep-including clock: only native hints can report sleep
    return wall, awake, total


class Lifecycle:
    """Clock-derived sleep and clock-change detection plus native hints.

    Jobs.tick calls observe() under Jobs.lock. Lock order: Jobs.lock, then
    Lifecycle.lock, then Store.lock. event() never takes Jobs.lock.
    """

    def __init__(self, app, clocks=None):
        self.app, self.store = app, app.store
        self.clocks = clocks or system_clocks
        self.lock = threading.Lock()
        self.previous = None  # last (wall, awake, total)
        self.hint = None  # (awake, wall) at a native will-sleep
        self.native_window = None  # (started, ended) from native hints when no clock gap was seen
        self.last_sleep_end = None  # wall time at the end of the last clock-detected sleep
        with self.store.lock:
            self.store.db.executescript(LIFECYCLE_DDL)

    def _sample(self):
        values = self.clocks()
        if (not isinstance(values, tuple) or len(values) != 3
                or not all(type(v) in (int, float) and math.isfinite(v) for v in values)):
            raise ValueError('The lifecycle clocks returned an invalid sample.')
        return tuple(float(v) for v in values)

    def _record(self, kind, source, observed, seconds, started=None, zone=None):
        row = {'id': uuid.uuid4().hex, 'kind': kind, 'source': source, 'observed': observed,
               'started': started, 'seconds': float(seconds), 'zone': zone}
        with self.store.lock, self.store.db as db:
            db.execute('INSERT INTO lifecycle_observations(id,kind,source,observed,started,seconds,zone) '
                       'VALUES(?,?,?,?,?,?,?)', tuple(row.values()))
            excess = db.execute('SELECT count(*) FROM lifecycle_observations').fetchone()[0] - MAX_OBSERVATIONS
            if excess > 0:
                db.execute('DELETE FROM lifecycle_observations WHERE id IN (SELECT id FROM lifecycle_observations '
                           'ORDER BY rowid LIMIT ?)', (excess,))
        return row

    def _emit(self, payload):
        try:
            self.app.emit('lifecycle', payload)
        except Exception:
            pass  # presentation cannot change lifecycle evidence

    def _ledger(self):
        """Sleep is evidence for the active root's time ledger, never charged."""
        ledger, run_id = getattr(self.app, 'time_ledger', None), getattr(self.app, 'active_run', None)
        if ledger is None or not isinstance(run_id, str):
            return
        try:
            ledger.observe_sleep(run_id)
        except Exception:
            pass  # the ledger's own checks decide; a refusal never blocks scheduling

    @staticmethod
    def _streams():
        """A wake for open model streams: each must send a line soon or is asked again (recovery.note_wake)."""
        try:
            note_wake()
        except Exception:
            pass  # presentation of a wake never blocks scheduling

    def observe(self):
        """One sample. Returns {'sleeping', 'wakes', 'changes'}; each wake and change is reported once."""
        wakes, changes = [], []
        with self.lock:
            wall, awake, total = self._sample()
            previous, self.previous = self.previous, (wall, awake, total)
            if previous is not None:
                d_wall, d_awake, d_total = wall - previous[0], awake - previous[1], total - previous[2]
                if d_awake >= 0 and d_total >= 0:  # a monotonic regression is a new baseline, nothing inferred
                    slept = d_total - d_awake
                    if slept >= SLEEP_MIN:
                        wakes.append(self._record('sleep', 'clock', wall, slept, started=previous[0]))
                        self.last_sleep_end, self.hint, self.native_window = wall, None, None
                    drift = d_wall - d_total
                    if abs(drift) > CLOCK_JUMP:
                        changes.append(self._record('clock_forward' if drift > 0 else 'clock_backward', 'clock',
                                                    wall, drift, started=previous[0]))
            if not wakes and self.native_window is not None:
                started, ended = self.native_window
                wakes.append(self._record('sleep', 'native', ended, ended - started, started=started))
                self.native_window = None
            if self.hint is not None and awake - self.hint[0] >= HINT_HOLD:
                self.hint = None  # no sleep followed the hint
            sleeping = self.hint is not None
        if wakes:
            self._ledger()
            self._streams()
        for item in wakes + changes:
            self._emit({'kind': item['kind'], 'seconds': item['seconds']})
        return {'sleeping': sleeping,
                'wakes': [{'id': w['id'], 'started': w['started'], 'ended': w['observed'], 'seconds': w['seconds'],
                           'source': w['source']} for w in wakes],
                'changes': [{'id': c['id'], 'kind': c['kind'], 'seconds': c['seconds']} for c in changes]}

    def sleeping(self):
        with self.lock:
            return self.hint is not None

    def event(self, kind, **fields):
        """Rust-only native hint. It never changes a job or a run by itself."""
        allowed = EVENTS.get(kind) if isinstance(kind, str) else None
        if allowed is None:
            raise ValueError('Unknown lifecycle event.')
        if set(fields) != allowed:
            raise ValueError(f'The {kind} lifecycle event accepts ' + (', '.join(sorted(allowed)) or 'no fields') + '.')
        zone = None
        if kind == 'timezone_changed':
            zone = fields['timezone']
            if not isinstance(zone, str) or len(zone) > 64:
                raise ValueError('Use an IANA time zone name such as Europe/London.')
            time_zone(zone)
        with self.lock:
            wall, awake, _ = self._sample()
            if kind == 'will_sleep':
                self.hint = (awake, wall)
            elif kind == 'did_wake':
                started = self.hint[1] if self.hint else None
                self.hint = None
                # The clock may already have reported this sleep; never count it twice.
                if (started is not None and wall - started >= SLEEP_MIN
                        and (self.last_sleep_end is None or self.last_sleep_end < started)):
                    self.native_window = (started, wall)
            row = self._record(kind, 'native', wall, 0.0, zone=zone)
        self._emit({'kind': kind, 'seconds': 0.0, **({'timezone': zone} if zone else {})})
        if kind == 'did_wake':
            self._streams()
        jobs = getattr(self.app, 'jobs', None)
        if kind != 'will_sleep' and jobs is not None:
            jobs.wake()
        return {'accepted': True, 'kind': kind, 'observation_id': row['id']}

    def status(self, limit=20):
        rows = self.store.query('SELECT kind,source,observed,started,seconds,zone FROM lifecycle_observations '
                                'ORDER BY rowid DESC LIMIT ?', (limit,))
        _, awake, total = self._sample()
        return {'sleeping': self.sleeping(), 'sleep_clock': total != awake,
                'observations': [{**row, 'observed': iso(row['observed']), 'started': iso(row['started'])}
                                 for row in rows]}

    def quit_summary(self):
        """Text for the native quit confirmation; counts and the next reviewed time only."""
        rows = self.store.query("SELECT schedule,next_due FROM jobs WHERE state='active' AND next_due IS NOT NULL "
                                'ORDER BY next_due,id LIMIT 64')
        if not rows:
            return {'active': 0, 'next_due': None, 'message': None}
        first, count = rows[0], len(rows)
        schedule, due = json.loads(first['schedule']), first['next_due']
        if schedule.get('kind') == 'daily':
            zone = schedule['timezone']
            local = datetime.fromtimestamp(due, time_zone(zone))
            when = local.strftime('%H:%M') + ' ' + zone
        else:
            when = datetime.fromtimestamp(due, timezone.utc).strftime('%Y-%m-%d %H:%M') + ' UTC'
        noun = 'schedule' if count == 1 else 'schedules'
        return {'active': count, 'next_due': iso(due),
                'message': f'{count} {noun} will not fire until Pebble runs; next at {when}.'}
