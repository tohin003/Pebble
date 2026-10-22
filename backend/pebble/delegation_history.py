"""Host-recorded delegation history: advice only, never authority.

The host (Agents), never a model, records one content-free row per delegation
outcome: admitted, refused, accepted or dismissed, with the task kind, a fixed
detail label and the child's step and provider-reported token counts. Rows are
keyed by a salted hash of the project scope, the run and the host event; no
task text, path, source, result, run or agent ID is stored. History is bounded
per scope and across scopes.

Advice may recommend solo work when recent delegations of a kind lost in this
project. It never forbids or admits anything: the admission gate in
delegation_policy reads only host budget numbers and never this module. This is
operational advice, not one of the six memory layers.
"""

import hashlib
import json
import secrets
import statistics
import time
from contextlib import contextmanager

from .delegation_policy import ADMISSION_UNITS, TASK_KINDS

# Proposed for the parent's epoch-6 migrations.REFERENCE_DDL; created here at startup.
HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS delegation_history(
  scope TEXT NOT NULL,event TEXT NOT NULL,run_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('read_only','write')),
  outcome TEXT NOT NULL CHECK(outcome IN ('admitted','refused','accepted','dismissed')),
  detail TEXT,child_steps INTEGER CHECK(child_steps IS NULL OR child_steps>=0),
  child_tokens INTEGER CHECK(child_tokens IS NULL OR child_tokens>=0),recorded REAL NOT NULL,
  PRIMARY KEY(scope,event));
CREATE INDEX IF NOT EXISTS delegation_history_scope ON delegation_history(scope,kind,recorded);
CREATE TABLE IF NOT EXISTS delegation_salt(id INTEGER PRIMARY KEY CHECK(id=1),salt TEXT NOT NULL);
"""
OUTCOMES = ('admitted', 'refused', 'accepted', 'dismissed')
# Fixed labels only: refusal limits and terminal child statuses.
DETAILS = frozenset(ADMISSION_UNITS) | {'file_set', 'question', 'unfit_limits',
                                        'completed', 'failed', 'interrupted', 'stopped', 'budget_exhausted'}
MAX_EVENTS = 64      # rows kept per project scope
MAX_SCOPES = 32      # project scopes kept; the least recently recorded go first
RECENT = 16          # newest rows per kind that advice reads
MIN_SETTLED = 2      # outcomes needed before advice recommends anything
MAX_COUNT = 1_000_000_000
NOTES = {
    'no_history': 'No delegation outcome of this kind is recorded for this project yet.',
    'insufficient_history': 'Too few settled delegations of this kind to compare.',
    'delegation_paid': 'Recent delegations of this kind in this project were mostly accepted.',
    'solo': 'Recent delegations of this kind in this project mostly lost (refused or dismissed). Solo work is recommended.',
}
NOTE_TAIL = ' History advice only: host records, never permission; it cannot admit or refuse a spawn.'


def summarize(kind, rows):
    """Advice from (outcome, child_steps, child_tokens) rows, newest first."""
    counts = {outcome: sum(1 for row in rows if row[0] == outcome) for outcome in OUTCOMES}
    accepted, settled = counts['accepted'], counts['accepted'] + counts['dismissed']
    if not rows:
        recommendation = 'no_history'
    elif (settled >= MIN_SETTLED and accepted * 2 < settled) or (counts['refused'] >= MIN_SETTLED and not accepted):
        recommendation = 'solo'
    elif accepted and accepted * 2 >= settled:
        recommendation = 'delegation_paid'
    else:
        recommendation = 'insufficient_history'

    def median(index):
        values = [row[index] for row in rows if row[0] == 'accepted' and row[index] is not None]
        return int(statistics.median(values)) if values else None

    return {'task_kind': kind, 'recent': len(rows), **counts, 'median_child_steps': median(1),
            'median_child_tokens': median(2), 'recommendation': recommendation,
            'note': NOTES[recommendation] + NOTE_TAIL, 'source': 'host_recorded', 'authority': 'none'}


class DelegationHistory:
    def __init__(self, store):
        self.store = store
        with store.lock, store.db as db:
            db.executescript(HISTORY_DDL)

    @contextmanager
    def _transaction(self):
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise ValueError('Delegation history writes require their own transaction.')
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def _salt(db, create):
        row = db.execute('SELECT salt FROM delegation_salt WHERE id=1').fetchone()
        if row:
            return row[0]
        if not create:
            return None
        salt = secrets.token_hex(32)
        db.execute('INSERT INTO delegation_salt VALUES(1,?)', (salt,))
        return salt

    @staticmethod
    def _key(salt, *parts):
        digest = hashlib.sha256(salt.encode())
        for part in parts:
            digest.update(b'\0' + str(part).encode())
        return digest.hexdigest()

    def _scope(self, db, rid, salt):
        row = db.execute('SELECT config FROM runs WHERE id=?', (rid,)).fetchone()
        if not row:
            raise ValueError('Root run not found.')
        config = json.loads(row[0])
        return self._key(salt, 'scope', config.get('workspace') or '', json.dumps(config.get('workspace_identity')))

    def record(self, rid, kind, outcome, event, detail=None, child_steps=None, child_tokens=None):
        """Record one host outcome once per (run, event, outcome); returns True when new.

        ``event`` is a host identifier (agent generation or refused call), hashed
        with the salt and never stored. Counts are host or provider numbers.
        """
        if kind not in TASK_KINDS or outcome not in OUTCOMES or detail is not None and detail not in DETAILS:
            raise ValueError('Delegation history accepts fixed host labels only.')
        if not isinstance(event, str) or not 1 <= len(event) <= 400:
            raise ValueError('Use a bounded host event identifier.')
        for value in (child_steps, child_tokens):
            if value is not None and (type(value) is not int or not 0 <= value <= MAX_COUNT):
                raise ValueError('Delegation history counts are bounded nonnegative integers.')
        with self._transaction() as db:
            salt = self._salt(db, True)
            scope = self._scope(db, rid, salt)
            inserted = db.execute('INSERT OR IGNORE INTO delegation_history VALUES(?,?,?,?,?,?,?,?,?)',
                                  (scope, self._key(salt, 'event', rid, event, outcome), self._key(salt, 'run', rid),
                                   kind, outcome, detail, child_steps, child_tokens, time.time())).rowcount
            db.execute('DELETE FROM delegation_history WHERE scope=? AND rowid NOT IN (SELECT rowid FROM delegation_history '
                       'WHERE scope=? ORDER BY recorded DESC,rowid DESC LIMIT ?)', (scope, scope, MAX_EVENTS))
            stale = db.execute('SELECT scope FROM delegation_history GROUP BY scope ORDER BY max(recorded) DESC,max(rowid) DESC '
                               'LIMIT -1 OFFSET ?', (MAX_SCOPES,)).fetchall()
            for (old,) in stale:
                db.execute('DELETE FROM delegation_history WHERE scope=?', (old,))
            return bool(inserted)

    def advice(self, rid, kind):
        """Content-free advice for this run's project and task kind. Never raises for an unknown history."""
        if kind not in TASK_KINDS:
            raise ValueError('Unknown delegated task kind.')
        with self.store.lock:
            db = self.store.db
            salt = self._salt(db, False)
            rows = []
            if salt:
                rows = db.execute('SELECT outcome,child_steps,child_tokens FROM delegation_history WHERE scope=? AND kind=? '
                                  'ORDER BY recorded DESC,rowid DESC LIMIT ?', (self._scope(db, rid, salt), kind, RECENT)).fetchall()
        return summarize(kind, rows)

    def forget(self, db, cid):
        """Caller owns the chat deletion transaction: drop rows recorded by its runs."""
        salt = self._salt(db, False)
        if not salt:
            return
        for (rid,) in db.execute('SELECT id FROM runs WHERE chat_id=?', (cid,)).fetchall():
            db.execute('DELETE FROM delegation_history WHERE run_key=?', (self._key(salt, 'run', rid),))
