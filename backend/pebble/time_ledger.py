"""Cross-crash active-time ledger and shared root-pool time reservations (R09).

Active time is charged before it is spent. begin() opens a run's interval and
charges one full slice in the same IMMEDIATE transaction. heartbeat() renews
the advance once at most `lead` seconds of it remain: the elapsed part of the
current slice is settled exactly and a full slice is charged from now, so the
charge always covers the present plus at most one slice. Coverage is measured
on time.monotonic, which on macOS is mach_absolute_time: it excludes sleep and
restarts at boot. Sleep is therefore never charged; observe_sleep() records a
wall-minus-monotonic gap as evidence only.

Only a certain end refunds. end() returns the unused part of the open slice
(and charges an observed overrun). recover() at startup keeps every open slice
fully charged and closes it as `crash`, or as `reboot` when its boot session
differs. A crash over-charges by at most one slice and, while heartbeats arrive
within the lead (Ticker does this), never under-charges, so a crash loop can
never reduce consumed time.

Reservations hold process or child time against the run's pool. Admission keeps
the sum of open holds within max_seconds minus consumed. A certain settlement
charges the used time less the part provably already charged as root active
time on this boot; an unknown outcome charges the whole hold and refunds nothing.
Rows are registered before effects and never deleted here. Every value returned
is a number, state or identifier, never task content or authority.
"""

import contextlib
import ctypes
import ctypes.util
import json
import math
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from .policy import PolicyError


SLICE_SECONDS = 10.0
MAX_SLICE = 3600.0
MAX_SECONDS = 86_400.0  # RunLimits' per-run maximum; holds and settlements never exceed it
SLEEP_MIN = 2.0  # wall-minus-monotonic drift below this is clock noise, not sleep
MAX_OPEN, MAX_HOLDS = 64, 1024  # time reservations per run: open, and retained in total
KINDS = ('process', 'child')
# Rows whose [opened_mono, opened_mono + charged] interval is charged root active time.
COVERING = ('open', 'used', 'late', 'ended', 'crash', 'reboot')
STATES = COVERING + ('sleep_observed', 'baseline')
HEX = re.compile(r'[0-9a-f]{32}')
BOOT = re.compile(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}')
UNVERIFIED = 'The active-time ledger could not be verified, so nothing was charged or reserved.'
LEDGER_DDL = '''
CREATE TABLE IF NOT EXISTS run_time_ledger(
  run_id TEXT NOT NULL, slice_id TEXT PRIMARY KEY, boot_uuid TEXT NOT NULL, opened REAL NOT NULL,
  opened_mono REAL NOT NULL, charged REAL NOT NULL CHECK(charged >= 0),
  state TEXT NOT NULL CHECK(state IN ('open','used','late','ended','crash','reboot','sleep_observed','baseline')),
  closed REAL);
CREATE UNIQUE INDEX IF NOT EXISTS run_time_ledger_open ON run_time_ledger(run_id) WHERE state='open';
CREATE INDEX IF NOT EXISTS run_time_ledger_run ON run_time_ledger(run_id,opened_mono);
CREATE TABLE IF NOT EXISTS run_time_reservations(
  reservation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('process','child')),
  ref TEXT NOT NULL, seconds REAL NOT NULL CHECK(seconds > 0), boot_uuid TEXT NOT NULL, opened REAL NOT NULL,
  opened_mono REAL NOT NULL, state TEXT NOT NULL CHECK(state IN ('open','settled','uncertain')),
  used REAL CHECK(used IS NULL OR used >= 0), charged REAL NOT NULL CHECK(charged >= 0), closed REAL,
  UNIQUE(run_id,kind,ref));
CREATE INDEX IF NOT EXISTS run_time_reservations_run ON run_time_reservations(run_id,state);
'''


def boot_session():
    """This boot's identity (macOS kern.bootsessionuuid, Linux boot_id), or None when unknown."""
    value = None
    try:
        if sys.platform == 'darwin':
            libc = ctypes.CDLL(ctypes.util.find_library('c') or '/usr/lib/libSystem.B.dylib', use_errno=True)
            call = libc.sysctlbyname
            call.argtypes = (ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
                             ctypes.c_size_t)
            call.restype = ctypes.c_int
            size = ctypes.c_size_t(0)
            if call(b'kern.bootsessionuuid', None, ctypes.byref(size), None, 0) == 0 and 0 < size.value <= 128:
                buffer = ctypes.create_string_buffer(size.value)
                if call(b'kern.bootsessionuuid', buffer, ctypes.byref(size), None, 0) == 0:
                    value = buffer.value.decode('ascii', 'replace')
        elif sys.platform.startswith('linux'):
            value = Path('/proc/sys/kernel/random/boot_id').read_text('ascii')[:128].strip()
    except (OSError, AttributeError, ValueError):
        return None
    return value if isinstance(value, str) and BOOT.fullmatch(value) else None


def _label(value, limit=200):
    return (isinstance(value, str) and 1 <= len(value) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _run_id(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise PolicyError('Run not found.')
    return value


def _amount(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= MAX_SECONDS:
        raise PolicyError(f'Choose a finite {label} from 0 to {int(MAX_SECONDS)} seconds.')
    return float(value)


def _union(intervals):
    total, end = 0.0, None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total, end = total + stop - start, stop
        elif stop > end:
            total, end = total + stop - end, stop
    return total


class TimeLedger:
    """Durable advance-charged active time per run, plus reservations against its pool.

    Lock order: Store.lock only. Every write is one IMMEDIATE transaction and
    refuses to join a caller's open transaction.
    """

    def __init__(self, store, boot_uuid=None, clock=time.monotonic, wall=time.time, slice_seconds=SLICE_SECONDS,
                 *, lead=None):
        if not callable(clock) or not callable(wall):
            raise ValueError('The ledger clocks must be callable.')
        if type(slice_seconds) not in (int, float) or not math.isfinite(slice_seconds) or not 0 < slice_seconds <= MAX_SLICE:
            raise ValueError(f'Choose a slice from above 0 to {int(MAX_SLICE)} seconds.')
        lead = slice_seconds / 2 if lead is None else lead
        if type(lead) not in (int, float) or not math.isfinite(lead) or not 0 < lead < slice_seconds:
            raise ValueError('The renewal lead must be positive and shorter than a slice.')
        boot = boot_session() if boot_uuid is None else boot_uuid
        if not _label(boot, 128):
            raise PolicyError("This device's boot session could not be identified, so active time cannot be "
                              'ledgered. Pass an explicit boot identity; a new random one per service start is safe.')
        self.store, self.boot_uuid, self.clock, self.wall = store, boot, clock, wall
        self.slice_seconds, self.lead = float(slice_seconds), float(lead)
        self._active = set()  # runs this instance opened; any other open slice belongs to a dead instance
        with store.lock:
            if store.db.in_transaction:
                raise PolicyError(UNVERIFIED)
            with store.db as db:
                db.executescript('BEGIN IMMEDIATE;' + LEDGER_DDL + 'COMMIT;')

    @contextlib.contextmanager
    def _transaction(self):
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError(UNVERIFIED)
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def _now(self):
        mono, wall = self.clock(), self.wall()
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (mono, wall)):
            raise PolicyError('The clock returned an invalid time. ' + UNVERIFIED)
        return float(mono), float(wall)

    @staticmethod
    def _open(db, run_id):
        return db.execute("SELECT slice_id,boot_uuid,opened,opened_mono,charged FROM run_time_ledger "
                          "WHERE run_id=? AND state='open'", (run_id,)).fetchone()

    def _insert(self, db, run_id, opened, mono, charged, state, closed=None):
        slice_id = uuid.uuid4().hex
        db.execute('INSERT INTO run_time_ledger(run_id,slice_id,boot_uuid,opened,opened_mono,charged,state,closed) '
                   'VALUES(?,?,?,?,?,?,?,?)', (run_id, slice_id, self.boot_uuid, opened, mono, charged, state, closed))
        return slice_id

    @staticmethod
    def _close(db, slice_id, state, charged, wall):
        db.execute("UPDATE run_time_ledger SET state=?,charged=?,closed=? WHERE slice_id=? AND state='open'",
                   (state, charged, wall, slice_id))

    @staticmethod
    def _consumed(db, run_id):
        slices = db.execute('SELECT coalesce(sum(charged),0) FROM run_time_ledger WHERE run_id=?', (run_id,)).fetchone()[0]
        holds = db.execute("SELECT coalesce(sum(charged),0) FROM run_time_reservations WHERE run_id=? AND state!='open'",
                           (run_id,)).fetchone()[0]
        return float(slices) + float(holds)

    @staticmethod
    def _budget(db, run_id):
        """The run's pinned max_seconds, read inside the caller's transaction."""
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
            raise PolicyError('Run not found.')
        row = db.execute('SELECT config FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise PolicyError('Run not found.')
        try:
            seconds = json.loads(row[0])['limits']['max_seconds']
        except (TypeError, ValueError, KeyError):
            seconds = None
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 1 <= seconds <= MAX_SECONDS:
            raise PolicyError('The run has no valid time budget. ' + UNVERIFIED)
        return float(seconds)

    def _covered(self, db, run_id, start, end):
        """Seconds of [start, end] on this boot covered by the run's charged root slices (their union)."""
        if end <= start:
            return 0.0
        rows = db.execute('SELECT opened_mono,charged FROM run_time_ledger WHERE run_id=? AND boot_uuid=? AND state IN '
                          f"({','.join('?' * len(COVERING))}) AND opened_mono<? AND opened_mono+charged>?",
                          (run_id, self.boot_uuid, *COVERING, end, start)).fetchall()
        return _union((max(start, s), min(end, s + c)) for s, c in rows)

    def _holds(self, db, run_id, mono):
        """(sum of open holds, sum of their provisional charges at mono).

        An open hold is provisionally charged for every second of its window not
        already charged as root active time, up to the hold: the job is assumed
        to have run throughout. A hold from another boot counts in full.
        """
        total = provisional = 0.0
        for seconds, boot, start in db.execute("SELECT seconds,boot_uuid,opened_mono FROM run_time_reservations "
                                               "WHERE run_id=? AND state='open'", (run_id,)).fetchall():
            total += seconds
            if boot != self.boot_uuid or mono < start:
                provisional += seconds
            else:
                provisional += min(seconds, max(0.0, mono - start - self._covered(db, run_id, start, mono)))
        return total, provisional

    def _unused(self, db, run_id, mono):
        """The open slice's prepaid advance not yet spent at mono (0.0 when none or not comparable)."""
        row = self._open(db, run_id)
        if not row or row[1] != self.boot_uuid or mono < row[3]:
            return 0.0
        return max(0.0, min(row[4], row[3] + row[4] - mono))

    def _stale(self, db, row, wall):
        self._close(db, row[0], 'crash' if row[1] == self.boot_uuid else 'reboot', row[4], wall)

    @staticmethod
    def _elapsed(db, run_id):
        """The run's checkpointed pre-ledger elapsed time; a journal that lacks the run refuses."""
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone():
            return 0.0  # an isolated ledger without the run journal
        row = db.execute('SELECT elapsed FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise PolicyError('Run not found.')
        value = row[0]
        return float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else 0.0

    def _sleep(self, db, run_id, row, mono, wall):
        """Record a wall-minus-monotonic gap since the latest exact clock pair; never charged."""
        if row[1] != self.boot_uuid or mono < row[3]:
            return 0.0
        ref_wall, ref_mono = row[2], row[3]
        later = db.execute("SELECT closed,opened_mono FROM run_time_ledger WHERE run_id=? AND boot_uuid=? AND "
                           "state='sleep_observed' AND opened_mono>=? AND opened_mono<=? ORDER BY opened_mono DESC, "
                           'rowid DESC LIMIT 1', (run_id, self.boot_uuid, ref_mono, mono)).fetchone()
        if later:
            ref_wall, ref_mono = later
        gap = (wall - ref_wall) - (mono - ref_mono)
        if gap < SLEEP_MIN:
            return 0.0  # noise, or a wall clock set back: monotonic charging is unaffected either way
        self._insert(db, run_id, wall - gap, mono, 0.0, 'sleep_observed', wall)
        return gap

    # Active intervals

    def begin(self, run_id):
        """Open the run's active interval and charge one full slice in advance, atomically."""
        _run_id(run_id)
        with self.store.lock:
            with self._transaction() as db:
                mono, wall = self._now()
                elapsed = self._elapsed(db, run_id)
                old = self._open(db, run_id)
                if old:
                    if run_id in self._active:
                        raise PolicyError('This run already has an open active-time interval.')
                    self._stale(db, old, wall)  # an earlier service died without recovery: never refunded
                elif elapsed and not db.execute('SELECT 1 FROM run_time_ledger WHERE run_id=? LIMIT 1',
                                                (run_id,)).fetchone():
                    # Time a pre-ledger journal checkpointed is charged once, as this run's baseline.
                    self._insert(db, run_id, wall, mono, elapsed, 'baseline', wall)
                slice_id = self._insert(db, run_id, wall, mono, self.slice_seconds, 'open')
                consumed = self._consumed(db, run_id)
            self._active.add(run_id)
        return {'run_id': run_id, 'slice_id': slice_id, 'charged': self.slice_seconds, 'consumed': consumed}

    def heartbeat(self, run_id):
        """Charge the next slice once the current advance has at most `lead` seconds left.

        A late heartbeat charges the observed overrun as `late` before the next
        slice. Returns {'run_id', 'charged': seconds newly charged (0.0 while
        still covered), 'consumed'}.
        """
        _run_id(run_id)
        with self._transaction() as db:
            mono, wall = self._now()
            row = self._open(db, run_id)
            if not row or run_id not in self._active:
                raise PolicyError('This run has no open active-time interval in this service.')
            self._sleep(db, run_id, row, mono, wall)
            slice_id, boot, _, start, charged = row
            if boot != self.boot_uuid or mono < start:
                # Not comparable with this clock: the old slice stays whole and charging restarts now.
                self._close(db, slice_id, 'reboot' if boot != self.boot_uuid else 'used', charged, wall)
                added = self.slice_seconds
            else:
                left = start + charged - mono
                if left > self.lead:
                    return {'run_id': run_id, 'charged': 0.0, 'consumed': self._consumed(db, run_id)}
                if left >= 0:
                    # Still covered: the elapsed part is settled exactly, then a full slice from now.
                    self._close(db, slice_id, 'used', mono - start, wall)
                else:
                    self._close(db, slice_id, 'used', charged, wall)
                    self._insert(db, run_id, wall + left, start + charged, -left, 'late', wall)
                added = self.slice_seconds - left
            self._insert(db, run_id, wall, mono, self.slice_seconds, 'open')
            return {'run_id': run_id, 'charged': added, 'consumed': self._consumed(db, run_id)}

    def end(self, run_id, certain=True):
        """Close the open interval; a certain end refunds the unused advance and charges any overrun.

        Returns the refund in seconds. Nothing open returns 0.0. An uncertain end,
        another boot's slice or an unordered clock keeps the full charge.
        """
        _run_id(run_id)
        if type(certain) is not bool:
            raise PolicyError('certain must be true or false.')
        refund = 0.0
        with self.store.lock:
            with self._transaction() as db:
                mono, wall = self._now()
                row = self._open(db, run_id)
                if row:
                    slice_id, boot, _, start, charged = row
                    final = charged
                    if certain and boot == self.boot_uuid and mono >= start:
                        final = mono - start
                        refund = max(0.0, charged - final)
                    db.execute("UPDATE run_time_ledger SET state='ended',charged=?,closed=? WHERE slice_id=? "
                               "AND state='open'", (final, wall, slice_id))
            self._active.discard(run_id)
        return refund

    def observe_sleep(self, run_id):
        """Record a wall-minus-monotonic gap as `sleep_observed`. Never charged; returns the gap (0.0 if none)."""
        _run_id(run_id)
        with self._transaction() as db:
            mono, wall = self._now()
            row = self._open(db, run_id)
            return self._sleep(db, run_id, row, mono, wall) if row else 0.0

    def recover(self):
        """Startup, after the service lease and before any begin: settle what a dead service left open.

        Open slices stay fully charged, closed as `crash`, or `reboot` from another
        boot. Holds from another boot cannot still run: they settle `uncertain`
        with the full hold charged. Same-boot holds stay open for reattachment or
        reconciliation. Returns counts only.
        """
        counts = {'crash': 0, 'reboot': 0, 'reservations_uncertain': 0}
        with self.store.lock:
            with self._transaction() as db:
                _, wall = self._now()
                for row in db.execute("SELECT slice_id,boot_uuid,opened,opened_mono,charged,run_id FROM run_time_ledger "
                                      "WHERE state='open'").fetchall():
                    if row[5] not in self._active:
                        self._stale(db, row, wall)
                        counts['crash' if row[1] == self.boot_uuid else 'reboot'] += 1
                counts['reservations_uncertain'] = db.execute(
                    "UPDATE run_time_reservations SET state='uncertain',charged=seconds,closed=? "
                    "WHERE state='open' AND boot_uuid!=?", (wall, self.boot_uuid)).rowcount
        return counts

    # Readings

    def consumed(self, run_id):
        """Every second charged to the run: slices (with the open advance) plus settled holds."""
        _run_id(run_id)
        with self.store.lock:
            return self._consumed(self.store.db, run_id)

    def remaining(self, run_id):
        """The root's own live budget, for its deadline and max_seconds checks.

        max_seconds - consumed - provisional charges of open holds + the open
        slice's prepaid advance not yet spent (the root may still use it).
        Reservation admission uses the stricter unreserved headroom instead.
        """
        _run_id(run_id)
        with self.store.lock:
            db = self.store.db
            mono, _ = self._now()
            return (self._budget(db, run_id) - self._consumed(db, run_id) - self._holds(db, run_id, mono)[1]
                    + self._unused(db, run_id, mono))

    def usage(self, run_id):
        """Content-free accounting for the controller and UI."""
        _run_id(run_id)
        with self.store.lock:
            db = self.store.db
            mono, _ = self._now()
            budget, consumed = self._budget(db, run_id), self._consumed(db, run_id)
            held, provisional = self._holds(db, run_id, mono)
            unused = self._unused(db, run_id, mono)
            states = dict(db.execute('SELECT state,count(*) FROM run_time_ledger WHERE run_id=? GROUP BY state',
                                     (run_id,)).fetchall())
            seconds = dict(db.execute('SELECT state,coalesce(sum(charged),0) FROM run_time_ledger WHERE run_id=? '
                                      'GROUP BY state', (run_id,)).fetchall())
            slept = db.execute("SELECT coalesce(sum(closed-opened),0) FROM run_time_ledger WHERE run_id=? "
                               "AND state='sleep_observed'", (run_id,)).fetchone()[0]
            holds = dict(db.execute('SELECT state,count(*) FROM run_time_reservations WHERE run_id=? GROUP BY state',
                                    (run_id,)).fetchall())
        return {'run_id': run_id, 'budget': budget, 'consumed': consumed, 'reserved': held,
                'provisional': provisional, 'advance_unused': unused,
                'remaining': budget - consumed - provisional + unused,
                'unreserved': budget - consumed - held, 'open': bool(states.get('open')),
                'crashes': states.get('crash', 0), 'reboots': states.get('reboot', 0),
                'late_seconds': float(seconds.get('late', 0.0)), 'baseline_seconds': float(seconds.get('baseline', 0.0)),
                'sleep_observed_seconds': float(slept), 'reservations': holds, 'slice_seconds': self.slice_seconds,
                'authorization': 'none'}

    # Reservations

    def reserve(self, run_id, seconds, kind, ref, *, narrow=False, minimum=1.0):
        """Hold time for a process or child against the run's pool before it starts.

        Admission keeps the sum of open holds within max_seconds - consumed, in one
        IMMEDIATE transaction. narrow=True grants min(seconds, headroom) when that
        is at least `minimum`. The same (kind, ref) with the same request returns
        its open hold; a settled one is never reopened. Returns the reservation ID.
        """
        _run_id(run_id)
        seconds, minimum = _amount(seconds, 'reservation'), _amount(minimum, 'minimum reservation')
        if seconds <= 0 or minimum <= 0 or type(narrow) is not bool:
            raise PolicyError('A time reservation needs a positive length. Nothing was reserved.')
        if kind not in KINDS or not _label(ref):
            raise PolicyError('A time reservation needs a process or child kind and a short reference. '
                              'Nothing was reserved.')
        with self._transaction() as db:
            mono, wall = self._now()
            old = db.execute('SELECT reservation_id,state,seconds FROM run_time_reservations WHERE run_id=? AND kind=? '
                             'AND ref=?', (run_id, kind, ref)).fetchone()
            if old:
                if old[1] != 'open':
                    raise PolicyError('This time reservation was already settled; it is never reopened. '
                                      'Nothing was reserved.')
                if old[2] > seconds or (not narrow and old[2] != seconds):
                    raise PolicyError('This reference already holds a different time reservation. Nothing was reserved.')
                return old[0]
            budget = self._budget(db, run_id)
            count, open_count, held = db.execute(
                "SELECT count(*),coalesce(sum(state='open'),0),coalesce(sum(CASE WHEN state='open' THEN seconds END),0) "
                'FROM run_time_reservations WHERE run_id=?', (run_id,)).fetchone()
            if open_count >= MAX_OPEN or count >= MAX_HOLDS:
                raise PolicyError(f'A run keeps at most {MAX_OPEN} open and {MAX_HOLDS} total time reservations. '
                                  'Nothing was reserved.')
            headroom = budget - self._consumed(db, run_id) - held
            granted = min(seconds, headroom) if narrow else seconds
            if granted > headroom or granted < (min(minimum, seconds) if narrow else seconds) or granted <= 0:
                raise PolicyError(f'The run has {max(0.0, headroom):.1f} unreserved seconds left in its time budget. '
                                  'Nothing was reserved.')
            reservation_id = uuid.uuid4().hex
            db.execute('INSERT INTO run_time_reservations(reservation_id,run_id,kind,ref,seconds,boot_uuid,opened,'
                       'opened_mono,state,used,charged,closed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (reservation_id, run_id, kind, ref, granted, self.boot_uuid, wall, mono, 'open', None, 0.0, None))
        return reservation_id

    def settle_reservation(self, reservation_id, used, certain, *, credit=True):
        """Settle one hold exactly once; a repeat returns the existing record unchanged.

        certain: charge `used`, less (with credit) the part provably already
        charged as root active time on this boot, a lower bound of the job's
        overlap with it; the rest of the hold is refunded. Not certain: charge
        the whole hold (or `used` if larger); nothing is refunded. `used` may be
        None only when not certain. Pass a monotonic duration: wall time
        includes sleep and forfeits the credit.
        """
        if not isinstance(reservation_id, str) or not HEX.fullmatch(reservation_id):
            raise PolicyError('Time reservation not found.')
        if type(certain) is not bool or type(credit) is not bool or (certain and used is None):
            raise PolicyError('A certain settlement needs the used seconds.')
        used = None if used is None else _amount(used, 'used time')
        with self._transaction() as db:
            mono, wall = self._now()
            row = db.execute('SELECT run_id,seconds,boot_uuid,opened_mono,state,charged FROM run_time_reservations '
                             'WHERE reservation_id=?', (reservation_id,)).fetchone()
            if not row:
                raise PolicyError('Time reservation not found.')
            run_id, seconds, boot, start, state, charged = row
            if state != 'open':
                return {'reservation_id': reservation_id, 'state': state, 'charged': charged,
                        'refund': max(0.0, seconds - charged), 'credited': 0.0, 'duplicate': True}
            credited = 0.0
            if certain:
                window = mono - start
                if credit and boot == self.boot_uuid and 0 <= used <= window:
                    covered = self._covered(db, run_id, start, mono)
                    credited = max(0.0, min(used, covered, used + covered - window))
                charged, state = used - credited, 'settled'
            else:
                charged, state = max(seconds, used or 0.0), 'uncertain'
            db.execute("UPDATE run_time_reservations SET state=?,used=?,charged=?,closed=? WHERE reservation_id=? "
                       "AND state='open'", (state, used, charged, wall, reservation_id))
        return {'reservation_id': reservation_id, 'state': state, 'charged': charged,
                'refund': max(0.0, seconds - charged), 'credited': credited, 'duplicate': False}

    def reservation(self, reservation_id):
        if not isinstance(reservation_id, str) or not HEX.fullmatch(reservation_id):
            raise PolicyError('Time reservation not found.')
        with self.store.lock:
            row = self.store.db.execute('SELECT reservation_id,run_id,kind,ref,seconds,state,used,charged FROM '
                                        'run_time_reservations WHERE reservation_id=?', (reservation_id,)).fetchone()
        if not row:
            raise PolicyError('Time reservation not found.')
        return dict(zip(('reservation_id', 'run_id', 'kind', 'ref', 'seconds', 'state', 'used', 'charged'), row))

    def ticker(self, run_id, interval=None, on_exhausted=None):
        return Ticker(self, run_id, interval, on_exhausted).start()


class Ticker:
    """Heartbeats one run's open interval from a daemon thread until stop().

    The interval defaults to half the lead, so the advance is renewed before it
    runs out even when a tick is late. on_exhausted(run_id) is called once when
    remaining() reaches zero. A ledger or callback error stops the ticker and is
    kept as a type name in `error`; the owning runner decides what follows.
    """

    def __init__(self, ledger, run_id, interval=None, on_exhausted=None):
        _run_id(run_id)
        interval = ledger.lead / 2 if interval is None else interval
        if type(interval) not in (int, float) or not math.isfinite(interval) or not 0 < interval <= ledger.lead:
            raise ValueError('The heartbeat interval must be positive and at most the renewal lead.')
        if on_exhausted is not None and not callable(on_exhausted):
            raise ValueError('on_exhausted must be callable.')
        self.ledger, self.run_id, self.interval, self.on_exhausted = ledger, run_id, float(interval), on_exhausted
        self.stopped = threading.Event()
        self.exhausted = False
        self.error = None
        self.thread = threading.Thread(target=self._loop, name='pebble-ledger-' + run_id[:8], daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _loop(self):
        while not self.stopped.wait(self.interval):
            try:
                self.ledger.heartbeat(self.run_id)
                if self.on_exhausted and not self.exhausted and self.ledger.remaining(self.run_id) <= 0:
                    self.exhausted = True
                    self.on_exhausted(self.run_id)
            except Exception as exc:
                self.error = type(exc).__name__
                return

    def stop(self, timeout=5.0):
        self.stopped.set()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout)
        return not self.thread.is_alive()
