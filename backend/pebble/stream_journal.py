"""Durable attempt-scoped replay of visible model text; presentation only.

A reconnecting client replays what each live provider attempt displayed and
how it ended. Records are untrusted model output: never canonical model or tool
context, evidence or approval, and nothing reconstructs tool calls or executes
effects from them. Only visible text deltas and fixed lifecycle metadata are
stored; tool-call fragments, reasoning, raw provider responses, errors, headers
and credentials never are. The canonical answer stays in RunJournal's committed
response and the transcript.

Only the host writes: begin -> delta* -> settle(committed|discarded|interrupted).
A run has at most one open attempt. Settlement is final; repeating the same
settlement replays its event and a different one is refused. Bounds reject;
nothing retained is pruned. Startup recovery runs only under RunJournal's
service ownership and appends `interrupted` settlements for open attempts.
"""

import hashlib
import re
import time
import uuid

from .runtime import LIVE


STREAM_VERSION = 1
STREAM_META = 'run_stream_meta'
STREAM_TABLES = ('run_stream_attempts', 'run_stream_events')
# Statement for statement what this build creates, for migrations.REFERENCE_DDL.
STREAM_DDL = '''
CREATE TABLE IF NOT EXISTS run_stream_meta(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS run_stream_attempts(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
  step INTEGER NOT NULL, attempt INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('open','committed','discarded','interrupted')),
  bytes INTEGER NOT NULL, deltas INTEGER NOT NULL, created REAL NOT NULL, settled REAL,
  UNIQUE(run_id,ordinal));
CREATE UNIQUE INDEX IF NOT EXISTS run_stream_one_open ON run_stream_attempts(run_id) WHERE state='open';
CREATE TABLE IF NOT EXISTS run_stream_events(
  run_id TEXT NOT NULL, sequence INTEGER NOT NULL, attempt_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('begin','delta','settle')),
  state TEXT CHECK(state IN ('open','committed','discarded','interrupted')),
  origin TEXT CHECK(origin IN ('host','recovery')),
  text TEXT, size INTEGER NOT NULL, sha256 TEXT, created REAL NOT NULL,
  PRIMARY KEY(run_id,sequence));
CREATE INDEX IF NOT EXISTS run_stream_events_attempt ON run_stream_events(attempt_id,kind,sequence);
'''

MAX_ATTEMPT_BYTES = 8_000_000   # UTF-8 text per attempt: complete_with_retry's own cap
MAX_ATTEMPT_DELTAS = 131_072    # delta events per attempt
MAX_ATTEMPTS = 128              # retained attempts per run; none is ever pruned
MAX_RUN_BYTES = 128_000_000     # retained text per run
MAX_RUN_EVENTS = 1_048_576      # events per run; one settlement slot is always reserved
MAX_STEP = 65_536
MAX_ATTEMPT = 64
MAX_PAGE = 100
PAGE_BYTES = 1_048_576          # delta text per page; a larger single delta pages alone
SETTLED = ('committed', 'discarded', 'interrupted')
NOTICE = ('Untrusted model text as it streamed. Only a committed attempt matches the saved answer; '
          'none of it is model context, evidence or approval.')
IDENT = re.compile(r'[A-Za-z0-9_-]{1,128}')
ATTEMPT_ID = re.compile(r'[0-9a-f]{32}')
DIGEST = re.compile(r'[0-9a-f]{64}')
EVENTS = ('SELECT e.sequence,e.attempt_id,e.kind,e.state,e.origin,e.text,e.sha256,e.created,'
          'a.step,a.attempt,a.bytes FROM run_stream_events e JOIN run_stream_attempts a '
          'ON a.id=e.attempt_id AND a.run_id=e.run_id WHERE e.run_id=? ')


class StreamCapacity(ValueError):
    """A stream bound was reached. Nothing retained was dropped."""


def _ident(value, label):
    if type(value) is not str or not IDENT.fullmatch(value):
        raise ValueError(f'Use a valid {label} identifier.')


def _attempt_id(value):
    if type(value) is not str or not ATTEMPT_ID.fullmatch(value):
        raise ValueError('Use a valid stream attempt identifier.')


def _count(value, ceiling, label):
    if type(value) is not int or not 1 <= value <= ceiling:
        raise ValueError(f'Use a whole {label} number from 1 to {ceiling:,}.')


def _text(value):
    if type(value) is not str or not value:
        raise ValueError('Stream text must be nonempty text.')
    if len(value) > MAX_ATTEMPT_BYTES:
        raise StreamCapacity(f'A stream attempt holds at most {MAX_ATTEMPT_BYTES:,} bytes of text.')
    try:
        return len(value.encode('utf-8'))
    except UnicodeEncodeError:
        raise ValueError('Stream text must be valid Unicode.') from None


def _event(row):
    sequence, aid, kind, state, origin, text, digest, created, step, attempt, total = row
    event = {'sequence': sequence, 'attempt_id': aid, 'kind': kind, 'step': step, 'attempt': attempt, 'created': created}
    if kind == 'delta':
        event['text'] = text
    elif kind == 'begin':
        event['state'] = 'open'
    else:
        event.update(state=state, origin=origin, bytes=total, sha256=digest)
    return event


class StreamJournal:
    def __init__(self, store, runs):
        self.store, self.runs = store, runs
        self._owned()
        with store.lock, store.db:
            if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='run_stream_meta'").fetchone():
                if [r[0] for r in store.db.execute('SELECT version FROM run_stream_meta LIMIT 2')] != [STREAM_VERSION]:
                    raise ValueError('This stream journal requires a compatible Pebble version.')
            store.db.executescript('BEGIN IMMEDIATE;' + STREAM_DDL +
                f'INSERT INTO run_stream_meta SELECT {STREAM_VERSION} WHERE NOT EXISTS(SELECT 1 FROM run_stream_meta);'
                'COMMIT;')
        self.recover()
        runs.stream = self

    def _owned(self):
        lease = getattr(self.runs, 'lease', None)
        if lease is None or lease.closed:
            raise ValueError('Stream records require this service to own the data directory.')

    @staticmethod
    def _head(db, rid):
        return db.execute('SELECT coalesce(max(sequence),0) FROM run_stream_events WHERE run_id=?', (rid,)).fetchone()[0]

    @staticmethod
    def _insert(db, rid, sequence, aid, kind, now, *, state=None, origin=None, text=None, size=0, digest=None):
        db.execute('INSERT INTO run_stream_events VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (rid, sequence, aid, kind, state, origin, text, size, digest, now))
        return _event(db.execute(EVENTS + 'AND e.sequence=?', (rid, sequence)).fetchone())

    def begin(self, run_id, step, attempt):
        """Open a host attempt on a live run; returns its opaque identifier."""
        _ident(run_id, 'run')
        _count(step, MAX_STEP, 'step')
        _count(attempt, MAX_ATTEMPT, 'attempt')
        aid, now = uuid.uuid4().hex, time.time()
        with self.store.lock, self.store.db as db:
            self._owned()
            run = db.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run:
                raise ValueError('Run not found.')
            if run[0] not in LIVE:
                raise ValueError('Only a live run can stream model text.')
            if db.execute("SELECT 1 FROM run_stream_attempts WHERE run_id=? AND state='open'", (run_id,)).fetchone():
                raise ValueError('Settle the open stream attempt before starting another.')
            count, ordinal = db.execute('SELECT count(*),coalesce(max(ordinal),0) FROM run_stream_attempts WHERE run_id=?',
                                        (run_id,)).fetchone()
            if count >= MAX_ATTEMPTS:
                raise StreamCapacity(f'This run reached its {MAX_ATTEMPTS} retained stream attempts.')
            head = self._head(db, run_id)
            if head + 2 > MAX_RUN_EVENTS:
                raise StreamCapacity('This run reached its stream event limit.')
            db.execute('INSERT INTO run_stream_attempts VALUES(?,?,?,?,?,?,0,0,?,NULL)',
                       (aid, run_id, ordinal + 1, step, attempt, 'open', now))
            self._insert(db, run_id, head + 1, aid, 'begin', now, state='open')
        return aid

    def append(self, attempt_id, text):
        """Durably add visible text to an open attempt; returns the delta event."""
        _attempt_id(attempt_id)
        size = _text(text)
        now = time.time()
        with self.store.lock, self.store.db as db:
            self._owned()
            row = db.execute('SELECT a.run_id,a.state,a.bytes,a.deltas,r.state FROM run_stream_attempts a '
                             'LEFT JOIN runs r ON r.id=a.run_id WHERE a.id=?', (attempt_id,)).fetchone()
            if not row:
                raise ValueError('Stream attempt not found.')
            rid, state, used, deltas, run_state = row
            if state != 'open':
                raise ValueError('This stream attempt is settled; its text is final.')
            if run_state not in LIVE:
                raise ValueError('Only a live run can stream model text.')
            if used + size > MAX_ATTEMPT_BYTES:
                raise StreamCapacity(f'A stream attempt holds at most {MAX_ATTEMPT_BYTES:,} bytes of text.')
            if deltas >= MAX_ATTEMPT_DELTAS:
                raise StreamCapacity(f'A stream attempt holds at most {MAX_ATTEMPT_DELTAS:,} text events.')
            total = db.execute('SELECT coalesce(sum(bytes),0) FROM run_stream_attempts WHERE run_id=?', (rid,)).fetchone()[0]
            if total + size > MAX_RUN_BYTES:
                raise StreamCapacity(f'A run retains at most {MAX_RUN_BYTES:,} bytes of streamed text.')
            head = self._head(db, rid)
            if head + 2 > MAX_RUN_EVENTS:
                raise StreamCapacity('This run reached its stream event limit.')
            db.execute('UPDATE run_stream_attempts SET bytes=bytes+?,deltas=deltas+1 WHERE id=?', (size, attempt_id))
            return self._insert(db, rid, head + 1, attempt_id, 'delta', now, text=text, size=size)

    def settle(self, attempt_id, state, *, expect_sha256=None):
        """Settle an attempt once. A commit can require the exact attempt text's SHA-256."""
        with self.store.lock, self.store.db as db:
            return self.settle_in(db, attempt_id, state, expect_sha256=expect_sha256)

    def settle_in(self, db, attempt_id, state, *, expect_sha256=None):
        """Caller owns the transaction, e.g. RunJournal.response's commit."""
        with self.store.lock:
            return self._settle(db, attempt_id, state, 'host', expect_sha256)

    def _settle(self, db, attempt_id, state, origin, expect):
        _attempt_id(attempt_id)
        if state not in SETTLED:
            raise ValueError('Settle a stream attempt as committed, discarded or interrupted.')
        if expect is not None and (state != 'committed' or type(expect) is not str or not DIGEST.fullmatch(expect)):
            raise ValueError('Only a commit takes the expected SHA-256 of its text.')
        self._owned()
        row = db.execute('SELECT a.run_id,a.state,r.state FROM run_stream_attempts a '
                         'LEFT JOIN runs r ON r.id=a.run_id WHERE a.id=?', (attempt_id,)).fetchone()
        if not row:
            raise ValueError('Stream attempt not found.')
        rid, current, run_state = row
        if current != 'open':
            if current != state:
                raise ValueError('This stream attempt already settled differently.')
            event = _event(db.execute(EVENTS + "AND e.attempt_id=? AND e.kind='settle'", (rid, attempt_id)).fetchone())
            if expect is not None and event['sha256'] != expect:
                raise ValueError('The streamed text does not match the committed response.')
            return event
        if state == 'committed' and run_state not in LIVE:
            raise ValueError('Only a live run can commit streamed text.')
        digest = hashlib.sha256()
        for (text,) in db.execute("SELECT text FROM run_stream_events WHERE attempt_id=? AND kind='delta' ORDER BY sequence",
                                  (attempt_id,)):
            digest.update(text.encode('utf-8'))
        digest = digest.hexdigest()
        if expect is not None and digest != expect:
            raise ValueError('The streamed text does not match the committed response.')
        now = time.time()
        db.execute('UPDATE run_stream_attempts SET state=?,settled=? WHERE id=?', (state, now, attempt_id))
        return self._insert(db, rid, self._head(db, rid) + 1, attempt_id, 'settle', now,
                            state=state, origin=origin, digest=digest)

    def interrupt_open(self, run_id):
        """Settle a run's open attempt as interrupted, e.g. after a failed presentation."""
        _ident(run_id, 'run')
        with self.store.lock, self.store.db as db:
            rows = db.execute("SELECT id FROM run_stream_attempts WHERE run_id=? AND state='open'", (run_id,)).fetchall()
            return [self._settle(db, aid, 'interrupted', 'host', None) for (aid,) in rows]

    def recover(self):
        """Startup, under service ownership: an open attempt was interrupted. Appends only."""
        with self.store.lock, self.store.db as db:
            self._owned()
            rows = db.execute("SELECT id FROM run_stream_attempts WHERE state='open' ORDER BY run_id,ordinal").fetchall()
            for (aid,) in rows:
                self._settle(db, aid, 'interrupted', 'recovery', None)
        return len(rows)

    def capacity(self, run_id):
        """What a run can still retain, so a caller can choose buffered display instead."""
        _ident(run_id, 'run')
        with self.store.lock:
            db = self.store.db
            count, used = db.execute('SELECT count(*),coalesce(sum(bytes),0) FROM run_stream_attempts WHERE run_id=?',
                                     (run_id,)).fetchone()
            head = self._head(db, run_id)
        return {'attempts': MAX_ATTEMPTS - count, 'bytes': MAX_RUN_BYTES - used, 'events': MAX_RUN_EVENTS - head}

    def read(self, chat_id, run_id, after=0, limit=50):
        """One page of a run's events after a cursor, scoped to its conversation."""
        _ident(chat_id, 'conversation')
        _ident(run_id, 'run')
        if type(after) is not int or not 0 <= after <= MAX_RUN_EVENTS:
            raise ValueError('Use a nonnegative whole stream cursor.')
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise ValueError(f'Use a stream page limit from 1 to {MAX_PAGE}.')
        with self.store.lock:
            db = self.store.db
            if not db.execute('SELECT 1 FROM runs WHERE id=? AND chat_id=?', (run_id, chat_id)).fetchone():
                raise ValueError('Run not found in this conversation.')
            head = self._head(db, run_id)
            if after > head:
                raise ValueError('This stream cursor is ahead of the run record.')
            last, used = after, 0
            for sequence, size in db.execute('SELECT sequence,size FROM run_stream_events WHERE run_id=? AND sequence>? '
                                             'ORDER BY sequence LIMIT ?', (run_id, after, limit)).fetchall():
                if last > after and used + size > PAGE_BYTES:
                    break
                last, used = sequence, used + size
            events = [_event(row) for row in db.execute(EVENTS + 'AND e.sequence>? AND e.sequence<=? ORDER BY e.sequence',
                                                        (run_id, after, last))] if last > after else []
        return {'chat_id': chat_id, 'run_id': run_id, 'after': after, 'events': events, 'next_after': last,
                'more': last < head, 'head': head, 'presentation_only': True, 'notice': NOTICE}

    @staticmethod
    def forget(db, chat_id):
        """Caller owns the transaction (RunJournal.forget), before it deletes the runs."""
        for table in ('run_stream_events', 'run_stream_attempts'):
            db.execute(f'DELETE FROM {table} WHERE run_id IN (SELECT id FROM runs WHERE chat_id=?)', (chat_id,))
