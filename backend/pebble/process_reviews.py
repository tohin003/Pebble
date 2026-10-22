"""Native inspection acknowledgements; never a model tool or effect approval."""
import time

from .policy import PolicyError, Workspace
from .process_jobs import _identity, _nested, _resolved

REVIEW_DDL = '''
CREATE TABLE IF NOT EXISTS process_inspections(
  process_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, chat_id TEXT NOT NULL,
  observation TEXT NOT NULL, note TEXT NOT NULL, created REAL NOT NULL);
'''


class ProcessInspections:
    def __init__(self, app):
        self.app = app
        with app.store.lock, app.store.db:
            app.store.db.executescript(REVIEW_DDL)

    def pending(self, root):
        try:
            identity = list(Workspace(root).identity)
        except (OSError, PolicyError, ValueError):
            identity = None
        rows = self.app.store.query("""SELECT p.root,p.root_identity FROM process_jobs p
            WHERE p.outcome_uncertain=1
            AND NOT EXISTS(SELECT 1 FROM process_inspections i WHERE i.process_id=p.id)
            """)
        base = _resolved(root)
        return any(row['root'] == root or identity is not None and _identity(row['root_identity']) == identity
                   or _nested(_resolved(row['root']), base) for row in rows)

    def acknowledge(self, chat_id, run_id, process_id, observation, note):
        if any(not isinstance(value, str) or not 1 <= len(value) <= 128
               for value in (chat_id, run_id, process_id)):
            raise PolicyError('Use the scoped conversation, run and process identities.')
        if not isinstance(observation, str) or observation not in {'effect_observed', 'no_effect_observed', 'still_uncertain'}:
            raise PolicyError('Choose the observed effect or an explicitly uncertain inspection.')
        if not isinstance(note, str) or not note.strip() or len(note.encode()) > 2000:
            raise PolicyError('Record an inspection note of at most 2000 bytes.')
        self.app.runs.get(run_id, chat_id)
        with self.app.store.lock, self.app.store.db as db:
            row = db.execute('SELECT outcome_uncertain FROM process_jobs WHERE id=? AND chat_id=? AND run_id=?',
                             (process_id, chat_id, run_id)).fetchone()
            if not row or row[0] != 1:
                raise PolicyError('Only an owned uncertain outcome can be acknowledged.')
            previous = db.execute('SELECT observation,note FROM process_inspections WHERE process_id=?',
                                  (process_id,)).fetchone()
            if previous and previous != (observation, note.strip()):
                raise PolicyError('This inspection is already recorded. Prior evidence is retained.')
            if not previous:
                db.execute('INSERT INTO process_inspections VALUES(?,?,?,?,?,?)',
                           (process_id, run_id, chat_id, observation, note.strip(), time.time()))
                self.app.runs.event(db, run_id, 'process_inspected',
                    {'process_id': process_id, 'origin': 'human', 'observation': observation,
                     'authorization': 'none', 'completion_verified': False})
        return {'process_id': process_id, 'inspection_recorded': True,
                'authorization': 'none', 'completion_verified': False}
