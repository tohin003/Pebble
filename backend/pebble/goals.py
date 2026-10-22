"""Human goals, completion criteria and an acyclic step graph (Phase 17, R08).

A goal is an operational record owned by one conversation, never one of the six
memory layers. Only native or human routes create goals, add or edit criteria
and steps, check human criteria and settle a goal: the chat-scoped methods below
are reached from the trusted windows (``methods()``) and from typed ``/goal`` and
``/criteria`` commands. A model sees the current goal as a HOST GOAL message
(``host_message``) and, through ``agent_update`` (the UpdateGoal tool), may only
change the status of existing steps, completing one with resolved, current
receipts from its own run, or propose criteria as candidates that the person
accepts or rejects.

Blockers are derived from host records: pending questions, pending reviews and
failed or uncertain outcomes, plus text the person wrote. A goal is done only
when every accepted criterion is met. A human check needs a native click; an
evidence criterion needs resolved, current receipts from non-stale runs linked
to the goal. Receipts are evidence of recorded outcomes, not proof of semantic
correctness. Nothing here runs a model, tool or shell, and nothing replays an effect.
"""

import json
import re
import time
import unicodedata
import uuid

from .interaction import ID, SECRET, RunInteractions, bounded_text
from .runtime import LIVE, encoded


# Proposed for the parent's epoch-7 migrations.REFERENCE_DDL; created here at startup.
GOAL_DDL = """
CREATE TABLE IF NOT EXISTS goals(
  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin='human'),
  title TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1),
  state TEXT NOT NULL CHECK(state IN ('active','paused','blocked','done','abandoned')),
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS goals_chat ON goals(chat_id,created);
CREATE UNIQUE INDEX IF NOT EXISTS goals_one_current ON goals(chat_id) WHERE state IN ('active','paused','blocked');
CREATE TABLE IF NOT EXISTS goal_criteria(
  goal_id TEXT NOT NULL, id TEXT NOT NULL, ordinal INTEGER NOT NULL, text TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('human_check','evidence')),
  status TEXT NOT NULL CHECK(status IN ('candidate','open','met','unmet','rejected')),
  origin TEXT NOT NULL CHECK(origin IN ('human','agent')), evidence_ids TEXT NOT NULL,
  checked_by TEXT CHECK(checked_by IS NULL OR checked_by IN ('human','host')),
  proposed_run_id TEXT, updated REAL NOT NULL, PRIMARY KEY(goal_id,id));
CREATE TABLE IF NOT EXISTS goal_steps(
  goal_id TEXT NOT NULL, id TEXT NOT NULL, ordinal INTEGER NOT NULL, title TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','in_progress','blocked','completed')),
  depends_on TEXT NOT NULL, criteria TEXT NOT NULL, evidence_ids TEXT NOT NULL,
  blocker_kind TEXT CHECK(blocker_kind IS NULL OR blocker_kind IN ('question','approval','failure','text')),
  blocker_ref TEXT, run_id TEXT, updated REAL NOT NULL, PRIMARY KEY(goal_id,id));
CREATE TABLE IF NOT EXISTS goal_events(
  goal_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN ('human','agent','host')), run_id TEXT,
  payload TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(goal_id,sequence));
CREATE INDEX IF NOT EXISTS goal_events_run ON goal_events(run_id,kind);
CREATE TRIGGER IF NOT EXISTS goal_events_append_only BEFORE UPDATE ON goal_events
  BEGIN SELECT RAISE(ABORT,'Goal events are append-only.'); END;
"""

CURRENT = ('active', 'paused', 'blocked')
TERMINAL = ('done', 'abandoned')
CRITERION_KINDS = ('human_check', 'evidence')
STEP_STATUSES = ('pending', 'in_progress', 'blocked', 'completed')
MAX_GOALS = 100        # per conversation; history is never erased to admit more
MAX_CRITERIA = 12      # accepted criteria per goal
MAX_CANDIDATES = 5     # agent proposals waiting for the person
MAX_STEPS = 20
MAX_DEPENDS = 8
MAX_EVIDENCE = 8
MAX_EVENTS = 4000
SETTLE_RESERVE = 16    # history kept free for the person's own events once agent and host events stop (X17 F5)
MAX_BLOCKERS = 50
SNAPSHOT_LIMIT = 15_000  # bytes; below handoff.MAX_GOAL_RECORD (16,000), with an explicit omission record
MAX_AUDITS = 8         # runs whose source witnesses are refreshed before judging evidence
TITLE, TEXT, BLOCKER_TEXT = 240, 500, 300
# Receipts that record a request, an admission or a host projection, never an outcome.
NOT_EVIDENCE = frozenset({'StartCommand', 'StopProcess', 'AskUserQuestion', 'AskParentQuestion', 'UpdatePlan',
                          'UpdateGoal', 'ReadRunEvidence', 'MemoryPropose',
                          # Child admissions, requests and tree projections (X17 F10): a queued child is no outcome.
                          'SpawnAgent', 'SpawnWorktreeAgent', 'DelegateAgentTask', 'SendAgentMessage', 'RetryAgent',
                          'AnswerAgentQuestion', 'PromoteAgentQuestion', 'ReadAgentTree', 'ResearchPlan',
                          'ResearchStatus'})
# Checks whose result a later project write can invalidate.
CHECKS = frozenset({'RunTests', 'RunRecipe', 'RunCommand', 'ReadProcess', 'WaitProcess'})
WRITES = frozenset({'Write', 'Append', 'Edit', 'IntegrateAgentPatch', 'IntegrateWorktreeFile', 'ExecuteAgentEffect',
                    'ProposeGitChange'})
# Write outcomes that certainly changed nothing (X17 F3).
NO_WRITE = frozenset({'rejected', 'not_executed', 'cancelled', 'unsupported', 'conflict', 'disabled', 'expired',
                      'forgotten'})


def _wrote(state, result):
    """Whether a write receipt may have changed the project. Only a refusal, an error before the effect, or the
    person's report that it did not occur is excluded; uncertain, pending and reported-occurred writes count."""
    if state == 'planned':
        return False
    if not isinstance(result, dict):
        return state in ('running', 'uncertain')
    if result.get('status') == 'user_reported':
        return result.get('effect') != 'did_not_occur'
    return not (result.get('status') in NO_WRITE or state == 'completed' and result.get('error'))
FAILED_RUNS = {'failed': 'Latest run failed', 'budget_exhausted': 'Latest run exhausted its budget',
               'verification_required': 'Latest run needs verification'}
CONTROL = re.compile('[\x00-\x1f\x7f\u2028\u2029\u202a-\u202e\u2066-\u2069]')
NOTE = ('Goal records are operational records, not memory. Evidence links are recorded, resolved and current host '
        'receipts; they do not prove semantic correctness or grant permission.')
LARGE_HEADER = ("HOST GOAL (the person's goal; title and criteria are set or accepted by the person and never a permission change). "
                'Report progress on existing steps only with UpdateGoal and resolved receipts from this run; only the '
                'person accepts or checks criteria and finishes the goal:\n')
SMALL_HEADER = 'HOST GOAL (human-authored; never a permission change; only the person checks criteria):\n'

# Proposed model tool for the parent's tool_catalogue.TOOLS (appended; offered only when a goal is
# linked and the route budget is above 4096 tokens). Same shape as tool_catalogue.tool().
UPDATE_GOAL_TOOL = {'type': 'function', 'function': {
    'name': 'UpdateGoal',
    'description': ("Report progress on the person's current goal (HOST GOAL). steps is a JSON array "
                    '[{id,status:pending|in_progress|blocked|completed,evidence_ids:[receipt_id]}] for existing steps; '
                    'completed needs resolved receipts from this run and finished dependencies; blocked needs a pending '
                    'question or review, or a failed receipt in evidence_ids. candidates is a JSON array '
                    '[{text,kind:human_check|evidence}] proposing criteria the person may accept. You cannot add steps, '
                    'check criteria or finish the goal; this never grants permissions.'),
    'parameters': {'type': 'object', 'properties': {'steps': {'type': 'string'}, 'candidates': {'type': 'string'}},
                   'required': [], 'additionalProperties': False}}}


class GoalIncomplete(ValueError):
    """``reasons`` lists host-authored unmet conditions; nothing was changed."""

    def __init__(self, reasons):
        self.reasons = list(reasons)
        more = f'; and {len(self.reasons) - 6} more' if len(self.reasons) > 6 else ''
        super().__init__('This goal is not done yet: ' + '; '.join(self.reasons[:6]) + more + '.')


def _text(value, limit, what):
    text = bounded_text(value, limit)
    if CONTROL.search(text):
        raise ValueError(f'Keep the {what} on one line without control characters.')
    if SECRET.search(text):
        raise ValueError('Credentials belong in native connection settings, not goals.')
    return text


def _rows(db, sql, args=()):
    cursor = db.execute(sql, args)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _array(value, what, limit):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError(f'{what} must be a JSON array.') from None
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        raise ValueError(f'{what} must be a JSON array of 1 to {limit} items.')
    return value


def _ids(value, what, limit):
    if value is None:
        return []
    if (isinstance(value, (list, tuple)) and len(value) <= limit and len(set(value)) == len(value)
            and all(isinstance(v, str) and ID.fullmatch(v) for v in value)):
        return list(value)
    raise ValueError(f'{what} must list up to {limit} unique IDs.')


def _revision(value):
    if type(value) is not int:
        raise ValueError('Send the goal revision you reviewed.')
    return value


def _key(text):
    """Comparison form of criterion text: NFKC, no format characters (zero-width, directional marks, soft hyphen),
    single spaces, casefolded, so a rejected or duplicate criterion cannot return looking identical (X17 F6)."""
    text = ''.join(c for c in unicodedata.normalize('NFKC', text) if unicodedata.category(c) != 'Cf')
    return ' '.join(text.split()).casefold()


def _step_number(call_id):
    head = call_id.split(':', 1)[0]
    return int(head) if head.isdigit() else None


def _graph(steps):
    """Refuse unknown or cyclic dependencies and steps running ahead of their dependencies."""
    by_id = {s['id']: s for s in steps}
    for step in steps:
        for dep in step['depends_on']:
            if dep == step['id']:
                raise ValueError('A step cannot depend on itself.')
            if dep not in by_id:
                raise ValueError(f"Step {step['id']} depends on an unknown step ({dep}).")
    remaining = {s['id']: set(s['depends_on']) for s in steps}
    while remaining:
        ready = [ident for ident, deps in remaining.items() if not deps & set(remaining)]
        if not ready:
            raise ValueError('Step dependencies cannot form a cycle (' + ', '.join(sorted(remaining)) + ').')
        for ident in ready:
            remaining.pop(ident)
    if sum(s['status'] == 'in_progress' for s in steps) > 1:
        raise ValueError('Keep one step in progress at a time.')
    for step in steps:
        if step['status'] in ('in_progress', 'completed'):
            open_deps = [d for d in step['depends_on'] if by_id[d]['status'] != 'completed']
            if open_deps:
                raise ValueError(f"Step {step['id']} cannot be {step['status'].replace('_', ' ')} while "
                                 + ', '.join(open_deps) + ' is not completed.')


def forget(db, cid):
    """Delete a conversation's goal records; the caller owns the transaction (RunInteractions.forget)."""
    owned = 'SELECT id FROM goals WHERE chat_id=?'
    for table in ('goal_events', 'goal_steps', 'goal_criteria'):
        db.execute(f'DELETE FROM {table} WHERE goal_id IN ({owned})', (cid,))
    db.execute('DELETE FROM goals WHERE chat_id=?', (cid,))


class Goals:
    def __init__(self, app):
        self.app, self.store = app, app.store
        with self.store.lock:
            self.store.db.executescript(GOAL_DDL)

    # Records -----------------------------------------------------------------------------------

    @staticmethod
    def _chat(db, cid):
        if not isinstance(cid, str) or not db.execute('SELECT 1 FROM chats WHERE id=?', (cid,)).fetchone():
            raise ValueError('Conversation not found.')

    @staticmethod
    def _goal(db, cid, gid):
        rows = _rows(db, 'SELECT * FROM goals WHERE id=? AND chat_id=?', (gid, cid)) if isinstance(gid, str) else []
        if not rows:
            raise ValueError('Goal not found in this conversation.')
        return rows[0]

    @staticmethod
    def _current_id(db, cid):
        row = db.execute("SELECT id FROM goals WHERE chat_id=? AND state IN ('active','paused','blocked')", (cid,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _criteria(db, gid):
        rows = _rows(db, 'SELECT * FROM goal_criteria WHERE goal_id=? ORDER BY ordinal', (gid,))
        for row in rows:
            row['evidence_ids'] = json.loads(row['evidence_ids'])
        return rows

    @staticmethod
    def _steps(db, gid):
        rows = _rows(db, 'SELECT * FROM goal_steps WHERE goal_id=? ORDER BY ordinal', (gid,))
        for row in rows:
            for key in ('depends_on', 'criteria', 'evidence_ids'):
                row[key] = json.loads(row[key])
        return rows

    @staticmethod
    def _linked(db, gid):
        """Linked runs, newest first."""
        return [r[0] for r in db.execute("SELECT e.run_id FROM goal_events e JOIN runs r ON r.id=e.run_id WHERE "
                                         "e.goal_id=? AND e.kind='run_linked' ORDER BY r.created DESC", (gid,))]

    @staticmethod
    def _event(db, gid, kind, origin, payload, run_id=None, required=True):
        sequence = db.execute('SELECT coalesce(max(sequence),0)+1 FROM goal_events WHERE goal_id=?', (gid,)).fetchone()[0]
        # The person can always finish or abandon a goal: each goal has at most one terminal event, so history
        # stays bounded. Agent and host events stop short of the cap, leaving room for the person's own changes.
        terminal = origin == 'human' and (kind == 'completed' or kind == 'state' and payload.get('to') in TERMINAL)
        limit = MAX_EVENTS if origin == 'human' else MAX_EVENTS - SETTLE_RESERVE
        if sequence > limit and not terminal:
            if required:
                raise ValueError('This goal reached its history limit. Settle it and start a new goal.')
            return False
        db.execute('INSERT INTO goal_events VALUES(?,?,?,?,?,?,?)',
                   (gid, sequence, kind, origin, run_id, encoded(payload, 16000), time.time()))
        return True

    def _emit(self, cid, gid):
        try:
            row = self.store.query('SELECT revision,state FROM goals WHERE id=?', (gid,))
            if row:
                self.app.emit('goal', {'chat_id': cid, 'goal_id': gid, **row[0]})
        except Exception:
            pass  # Presentation only; the committed record is authoritative.

    def _mutate(self, cid, gid, revision, change):
        """A human route: ``change(db, goal)`` in one transaction with compare-and-swap on ``revision``."""
        with self.store.lock, self.store.db as db:
            goal = self._goal(db, cid, gid)
            if revision is not None and (type(revision) is not int or revision != goal['revision']):
                raise ValueError(f"This goal changed (revision {goal['revision']}). Refresh it before editing.")
            if goal['state'] in TERMINAL:
                raise ValueError(f"This goal is {goal['state']}; its record is closed.")
            changed = change(db, goal)
            db.execute('UPDATE goals SET revision=revision+1,updated=? WHERE id=?', (time.time(), gid))
        self._emit(cid, gid)
        return {**self.get(cid, gid), 'changed': changed}

    # Evidence and blockers ---------------------------------------------------------------------

    def _context(self, db, goal):
        cid = goal['chat_id']
        chat = db.execute('SELECT workspace FROM chats WHERE id=?', (cid,)).fetchone()
        identity = None
        if chat and chat[0]:
            try:
                from .policy import Workspace
                identity = list(Workspace(chat[0]).identity)
            except Exception:  # Missing, replaced or unsupported project folder.
                identity = 'unavailable'
        runs = {r['id']: r for r in _rows(db, 'SELECT id,state,config,created FROM runs WHERE chat_id=?', (cid,))}
        def began(rid, call_id):
            """The journal row of a tool's start: one insertion order across every run (X17 F4)."""
            row = db.execute("SELECT rowid FROM run_events WHERE run_id=? AND kind='tool_started' "
                             "AND json_extract(payload,'$.call_id')=? ORDER BY rowid LIMIT 1", (rid, call_id)).fetchone()
            return row[0] if row else None
        writes = []
        marks = ','.join('?' * len(WRITES))
        for rid, call_id, state, result in db.execute(
                f'SELECT t.run_id,t.call_id,t.state,t.result FROM run_tools t JOIN runs r ON r.id=t.run_id '
                f'WHERE r.chat_id=? AND t.name IN ({marks})', (cid, *sorted(WRITES))):
            if _wrote(state, json.loads(result) if result else None):
                writes.append((rid, runs[rid]['created'], _step_number(call_id), began(rid, call_id)))
        sources = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_sources'").fetchone())
        return {'identity': identity, 'runs': runs, 'writes': writes, 'linked': self._linked(db, goal['id']),
                'sources': sources, 'began': began}

    @staticmethod
    def _problem(db, ctx, rid, call_id):
        """Why a receipt is not resolved, current evidence for this goal; None when it is."""
        label = f'receipt {call_id}'
        run = ctx['runs'].get(rid)
        if run is None:
            return f'{label} is not from this conversation'
        if rid not in ctx['linked']:
            return f'{label} is from a run not linked to this goal'
        if run['state'] == 'needs_reconciliation' or db.execute(
                "SELECT 1 FROM run_tools WHERE run_id=? AND state='uncertain' LIMIT 1", (rid,)).fetchone():
            return f'{label} is from a run with an uncertain outcome'
        if json.loads(run['config']).get('workspace_identity') != ctx['identity']:
            return f'{label} is from a run whose project folder changed or was replaced'
        row = db.execute('SELECT name,state,result FROM run_tools WHERE run_id=? AND call_id=?', (rid, call_id)).fetchone()
        if not row:
            return f'{label} was not found in its run'
        name, state, raw = row
        result = json.loads(raw) if raw else None
        if name in NOT_EVIDENCE:
            return f'{label} ({name}) records a request or admission, not an outcome'
        if not RunInteractions.resolved_evidence({'state': state, 'result': result}):
            return f'{label} has no resolved, successful outcome'
        outcome = result.get('result', result)
        if any(isinstance(v, dict) and (v.get('outcome_uncertain') or v.get('live')) for v in (result, outcome)):
            return f'{label} has an uncertain or unfinished process outcome'
        if ctx['sources'] and db.execute("SELECT 1 FROM run_sources WHERE run_id=? AND receipt_id=? AND "
                                         "status!='current' LIMIT 1", (rid, call_id)).fetchone():
            return f'{label} read a file that has changed since'
        if name in CHECKS:
            step, check_began = _step_number(call_id), ctx['began'](rid, call_id)
            for write_rid, created, write_step, write_began in ctx['writes']:
                if check_began is not None and write_began is not None:
                    later = write_began > check_began
                else:  # no journal start event: the previous conservative rule
                    later = (write_step is None or step is None or write_step >= step) if write_rid == rid else created > run['created']
                if later:
                    return f'{label} ({name}) predates a later project write'
        return None

    @staticmethod
    def _refs(criterion, steps):
        """(receipt refs, waiting reason) proving an evidence criterion."""
        if criterion['evidence_ids']:
            return [tuple(r) for r in criterion['evidence_ids']], None
        linked = [s for s in steps if criterion['id'] in s['criteria']]
        if not linked:
            return [], 'link a step to it or choose receipts in the Goal panel'
        unfinished = [s['id'] for s in linked if s['status'] != 'completed']
        if unfinished:
            return [], 'waiting for step ' + ', '.join(unfinished)
        return list(dict.fromkeys(tuple(r) for s in linked for r in s['evidence_ids'])), None

    @staticmethod
    def _blockers(db, steps, ctx):
        items, seen = [], set()

        def add(kind, ref, label, run_id=None, step_id=None, detail=None, origin=None):
            if (kind, ref, run_id) in seen or len(items) >= MAX_BLOCKERS:
                return
            seen.add((kind, ref, run_id))
            item = {'kind': kind, 'ref': ref, 'label': label, 'run_id': run_id, 'step_id': step_id,
                    'detail': detail, 'detail_origin': origin}
            items.append({k: v for k, v in item.items() if v is not None})

        records = {rid: RunInteractions.pending_records(db, rid) for rid in ctx['linked']}
        questions = {q for r in records.values() for q, _ in r['questions']}
        actions = {a for r in records.values() for a in r['actions']}
        for step in steps:
            kind, ref = step['blocker_kind'], step['blocker_ref']
            if step['status'] != 'blocked' or not kind:
                continue
            if kind == 'text':
                add('text', step['id'], 'Blocked by you', step_id=step['id'], detail=ref, origin='human')
            elif kind == 'failure':
                add('failure', ref, 'Step reported a failed outcome', step['run_id'], step['id'])
            elif kind == 'question' and ref in questions:
                add('question', ref, 'Needs your answer', step['run_id'], step['id'])
            elif kind == 'approval' and ref in actions:
                add('approval', ref, 'Needs your review', step['run_id'], step['id'])
        for rid in ctx['linked']:
            pending = records[rid]
            for ident, title in pending['questions']:
                add('question', ident, 'Needs your answer', rid, detail=title[:160], origin='agent')
            for ident in pending['actions']:
                add('approval', ident, 'Needs your review', rid)
            for call_id in pending['uncertain']:
                add('failure', call_id, 'Outcome uncertain; inspect it before continuing', rid)
            if ctx['runs'][rid]['state'] == 'needs_reconciliation' and not pending['uncertain']:
                add('failure', rid, 'Run needs reconciliation', rid)
        if ctx['linked']:
            latest = ctx['linked'][0]
            state = ctx['runs'][latest]['state']
            if state in FAILED_RUNS:
                add('failure', latest, FAILED_RUNS[state], latest)
        return items

    def _evaluate(self, db, goal):
        """Derived criterion statuses, step evidence currency and blockers; persists host-derived changes."""
        gid = goal['id']
        criteria, steps = self._criteria(db, gid), self._steps(db, gid)
        for criterion in criteria:
            criterion['detail'] = None
            criterion['evidence'] = [list(r) for r in criterion['evidence_ids']]
        if goal['state'] in TERMINAL:
            for step in steps:
                step['evidence_current'] = None
            return {'criteria': criteria, 'steps': steps, 'blockers': [], 'linked': self._linked(db, gid)}
        ctx = self._context(db, goal)
        now = time.time()
        for criterion in criteria:
            if criterion['kind'] != 'evidence' or criterion['status'] in ('candidate', 'rejected'):
                continue
            if criterion['status'] == 'unmet' and criterion['checked_by'] == 'human':
                criterion['detail'] = 'marked not met by you'
                continue
            refs, waiting = self._refs(criterion, steps)
            problems = [p for p in (self._problem(db, ctx, rid, call) for rid, call in refs) if p]
            status = 'open' if not refs else 'unmet' if problems else 'met'
            checked = 'host' if status == 'met' else None
            criterion['detail'] = waiting or (problems[0] if problems else None)
            criterion['evidence'] = [list(r) for r in refs]
            if (status, checked) != (criterion['status'], criterion['checked_by']):
                db.execute('UPDATE goal_criteria SET status=?,checked_by=?,updated=? WHERE goal_id=? AND id=?',
                           (status, checked, now, gid, criterion['id']))
                self._event(db, gid, 'criterion_evaluated', 'host', {'id': criterion['id'], 'status': status,
                            'reason': criterion['detail']}, required=False)
                criterion['status'], criterion['checked_by'] = status, checked
        for step in steps:
            step['evidence_current'] = (None if step['status'] != 'completed' else
                                        not any(self._problem(db, ctx, rid, call) for rid, call in step['evidence_ids']))
        blockers = self._blockers(db, steps, ctx)
        if goal['state'] in ('active', 'blocked'):
            derived = 'blocked' if blockers else 'active'
            if derived != goal['state']:
                db.execute('UPDATE goals SET state=? WHERE id=?', (derived, gid))
                self._event(db, gid, 'state', 'host', {'from': goal['state'], 'to': derived,
                            'blockers': len(blockers)}, required=False)
                goal['state'] = derived
        return {'criteria': criteria, 'steps': steps, 'blockers': blockers, 'linked': ctx['linked'], 'ctx': ctx}

    def _audit(self, cid, gid, extra=()):
        """Best-effort fresh source witnesses for runs whose receipts back this goal (as /context does)."""
        provenance = getattr(self.app, 'provenance', None)
        if provenance is None:
            return
        with self.store.lock:
            db = self.store.db
            runs = {r[0] for r in extra}
            for criterion in self._criteria(db, gid):
                runs.update(r[0] for r in criterion['evidence_ids'])
            for step in self._steps(db, gid):
                runs.update(r[0] for r in step['evidence_ids'])
            rows = _rows(db, 'SELECT id,config FROM runs WHERE chat_id=? AND id IN (%s) ORDER BY created DESC LIMIT ?'
                         % ','.join('?' * len(runs)), (cid, *sorted(runs), MAX_AUDITS)) if runs else []
        from .policy import Workspace
        for row in rows:
            root = json.loads(row['config']).get('workspace')
            if root:
                try:
                    provenance.audit(row['id'], Workspace(root))
                except Exception:
                    pass  # Unchanged witnesses keep their recorded status; a missing root makes runs stale.

    # Views -------------------------------------------------------------------------------------

    @staticmethod
    def _progress(view):
        accepted = [c for c in view['criteria'] if c['status'] not in ('candidate', 'rejected')]
        return {'criteria_met': sum(c['status'] == 'met' for c in accepted), 'criteria_total': len(accepted),
                'candidates': sum(c['status'] == 'candidate' for c in view['criteria']),
                'steps_completed': sum(s['status'] == 'completed' for s in view['steps']),
                'steps_total': len(view['steps'])}

    def _snapshot(self, goal, view):
        done = {s['id'] for s in view['steps'] if s['status'] == 'completed'}
        active = {(b['kind'], b['ref']) for b in view['blockers']}
        criteria = [{'id': c['id'], 'text': c['text'], 'kind': c['kind'], 'status': c['status'], 'origin': c['origin'],
                     'checked_by': c['checked_by'], 'evidence': [{'run_id': r, 'call_id': k} for r, k in c['evidence']],
                     'detail': c['detail'], 'proposed_run_id': c['proposed_run_id'], 'updated': c['updated']}
                    for c in view['criteria']]
        steps = []
        for s in view['steps']:
            blocker = None
            if s['blocker_kind']:
                key = (s['blocker_kind'], s['id'] if s['blocker_kind'] == 'text' else s['blocker_ref'])
                blocker = {'kind': s['blocker_kind'], 'ref': s['blocker_ref'], 'active': key in active}
            steps.append({'id': s['id'], 'title': s['title'], 'status': s['status'], 'depends_on': s['depends_on'],
                          'criteria': s['criteria'], 'evidence': [{'run_id': r, 'call_id': k} for r, k in s['evidence_ids']],
                          'evidence_current': s['evidence_current'], 'blocker': blocker, 'run_id': s['run_id'],
                          'ready': s['status'] == 'pending' and set(s['depends_on']) <= done, 'updated': s['updated']})
        return {'id': goal['id'], 'chat_id': goal['chat_id'], 'origin': goal['origin'], 'title': goal['title'],
                'revision': goal['revision'], 'state': goal['state'], 'created': goal['created'],
                'updated': goal['updated'], 'criteria': criteria, 'steps': steps, 'blockers': view['blockers'],
                'progress': self._progress(view), 'runs': view['linked'][:20], 'authorization': 'none', 'note': NOTE}

    def get(self, cid, gid):
        self._audit(cid, gid)
        with self.store.lock, self.store.db as db:
            goal = self._goal(db, cid, gid)
            return self._snapshot(goal, self._evaluate(db, goal))

    def current(self, cid):
        with self.store.lock:
            self._chat(self.store.db, cid)
            gid = self._current_id(self.store.db, cid)
        return self.get(cid, gid) if gid else None

    def list(self, cid, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Use a goal list limit from 1 to 100.')
        result = []
        with self.store.lock, self.store.db as db:
            self._chat(db, cid)
            for goal in _rows(db, 'SELECT * FROM goals WHERE chat_id=? ORDER BY created DESC LIMIT ?', (cid, limit)):
                view = self._evaluate(db, goal)
                blocker = view['blockers'][0] if view['blockers'] else None
                result.append({'id': goal['id'], 'title': goal['title'], 'state': goal['state'],
                               'revision': goal['revision'], 'created': goal['created'], 'updated': goal['updated'],
                               'progress': self._progress(view), 'blockers': len(view['blockers']),
                               'blocker': blocker and {'kind': blocker['kind'], 'label': blocker['label']}})
        return result

    def blockers(self, cid, gid=None):
        """Host-derived blockers of a goal (default: the current goal, else the latest run)."""
        if gid is None:
            with self.store.lock:
                self._chat(self.store.db, cid)
                gid = self._current_id(self.store.db, cid)
        if gid:
            snapshot = self.get(cid, gid)
            return {'goal_id': gid, 'items': snapshot['blockers'], 'authorization': 'none'}
        with self.store.lock, self.store.db as db:
            latest = db.execute('SELECT id,state FROM runs WHERE chat_id=? ORDER BY created DESC LIMIT 1', (cid,)).fetchone()
            if not latest:
                return {'goal_id': None, 'items': [], 'authorization': 'none'}
            ctx = {'linked': [latest[0]], 'runs': {latest[0]: {'state': latest[1]}}}
            return {'goal_id': None, 'run_id': latest[0], 'items': self._blockers(db, [], ctx), 'authorization': 'none'}

    # Human and native routes -------------------------------------------------------------------

    def create(self, cid, title):
        title = _text(title, TITLE, 'goal title')
        with self.store.lock, self.store.db as db:
            self._chat(db, cid)
            if self._current_id(db, cid):
                raise ValueError('This conversation already has a current goal. Mark it done or abandon it first.')
            if db.execute('SELECT count(*) FROM goals WHERE chat_id=?', (cid,)).fetchone()[0] >= MAX_GOALS:
                raise ValueError(f'This conversation already holds {MAX_GOALS} goals. Start a new conversation.')
            gid, now = uuid.uuid4().hex, time.time()
            db.execute('INSERT INTO goals VALUES(?,?,?,?,?,?,?,?)', (gid, cid, 'human', title, 1, 'active', now, now))
            self._event(db, gid, 'created', 'human', {'title': title})
        self._emit(cid, gid)
        return self.get(cid, gid)

    def set_state(self, cid, gid, state, revision=None):
        if state == 'done':
            return self.complete(cid, gid, revision)
        if state not in ('active', 'paused', 'abandoned'):
            raise ValueError('Choose active, paused, done or abandoned.')

        def change(db, goal):
            old = goal['state']
            if state == 'paused' and old == 'paused':
                raise ValueError('This goal is already paused.')
            if state == 'active' and old != 'paused':
                raise ValueError('Only a paused goal can be resumed.')
            db.execute('UPDATE goals SET state=? WHERE id=?', (state, gid))
            self._event(db, gid, 'state', 'human', {'from': old, 'to': state})
        return self._mutate(cid, gid, revision, change)

    def complete(self, cid, gid, revision=None):
        self._audit(cid, gid)
        with self.store.lock, self.store.db as db:
            goal = self._goal(db, cid, gid)
            if revision is not None and (type(revision) is not int or revision != goal['revision']):
                raise ValueError(f"This goal changed (revision {goal['revision']}). Refresh it before editing.")
            if goal['state'] in TERMINAL:
                raise ValueError(f"This goal is {goal['state']}; its record is closed.")
            view = self._evaluate(db, goal)
            accepted = [c for c in view['criteria'] if c['status'] not in ('candidate', 'rejected')]
            candidates = sum(c['status'] == 'candidate' for c in view['criteria'])
            reasons = [] if accepted else ['add at least one completion criterion']
            if candidates:
                reasons.append(f'accept or reject {candidates} proposed criteri{"on" if candidates == 1 else "a"}')
            for c in accepted:
                if c['status'] == 'met' and c['checked_by'] == ('human' if c['kind'] == 'human_check' else 'host'):
                    continue
                need = 'your check' if c['kind'] == 'human_check' else c['detail'] or 'current receipts'
                reasons.append(f"criterion {c['id']} is {c['status']} ({need})")
            if view['blockers']:
                reasons.append(f"resolve {len(view['blockers'])} blocker{'s' if len(view['blockers']) != 1 else ''}")
            if any(view['ctx']['runs'][rid]['state'] in LIVE for rid in view['linked']):
                reasons.append('wait for or stop the running task')
            if reasons:
                raise GoalIncomplete(reasons)
            record = [{'id': c['id'], 'kind': c['kind'], 'checked_by': c['checked_by'], 'evidence': c['evidence']}
                      for c in accepted]
            db.execute("UPDATE goals SET state='done',revision=revision+1,updated=? WHERE id=?", (time.time(), gid))
            self._event(db, gid, 'completed', 'human', {'from': goal['state'], 'criteria': record})
        self._emit(cid, gid)
        return {**self.get(cid, gid), 'changed': gid}

    def add_criterion(self, cid, gid, text, kind='human_check', revision=None):
        text = _text(text, TEXT, 'criterion')
        if kind not in CRITERION_KINDS:
            raise ValueError('Choose a human_check or evidence criterion.')

        def change(db, goal):
            criteria = self._criteria(db, gid)
            if sum(c['status'] not in ('candidate', 'rejected') for c in criteria) >= MAX_CRITERIA:
                raise ValueError(f'A goal holds up to {MAX_CRITERIA} criteria.')
            if any(_key(c['text']) == _key(text) and c['status'] != 'rejected' for c in criteria):
                raise ValueError('This goal already has that criterion.')
            ordinal = max((c['ordinal'] for c in criteria), default=0) + 1
            ident = f'c{ordinal}'
            db.execute('INSERT INTO goal_criteria VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (gid, ident, ordinal, text, kind, 'open', 'human', '[]', None, None, time.time()))
            self._event(db, gid, 'criterion_added', 'human', {'id': ident, 'kind': kind, 'text': text})
            return ident
        return self._mutate(cid, gid, revision, change)

    def _criterion(self, db, gid, ident):
        found = next((c for c in self._criteria(db, gid) if c['id'] == ident), None)
        if not found:
            raise ValueError('Criterion not found in this goal.')
        return found

    def edit_criterion(self, cid, gid, ident, text, revision=None):
        text = _text(text, TEXT, 'criterion')

        def change(db, goal):
            old = self._criterion(db, gid, ident)
            if old['status'] == 'rejected':
                raise ValueError('This criterion was rejected. Add a new one instead.')
            if any(c['id'] != ident and c['status'] != 'rejected' and _key(c['text']) == _key(text)
                   for c in self._criteria(db, gid)):
                raise ValueError('This goal already has that criterion.')
            status = 'candidate' if old['status'] == 'candidate' else 'open'
            db.execute("UPDATE goal_criteria SET text=?,status=?,evidence_ids='[]',checked_by=NULL,updated=? "
                       'WHERE goal_id=? AND id=?', (text, status, time.time(), gid, ident))
            self._event(db, gid, 'criterion_edited', 'human', {'id': ident, 'from': old['text'], 'to': text})
            return ident
        return self._mutate(cid, gid, revision, change)

    def check_criterion(self, cid, gid, ident, met=True, evidence=None, revision=None):
        """A native click: a human check records the person's judgment; an evidence check asks the host."""
        if type(met) is not bool:
            raise ValueError('met must be a boolean.')
        refs = None
        if evidence is not None:
            if (not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_EVIDENCE
                    or any(not isinstance(e, dict) or set(e) != {'run_id', 'call_id'} or not isinstance(e['run_id'], str)
                           or not ID.fullmatch(e['run_id']) or not isinstance(e['call_id'], str)
                           or not 1 <= len(e['call_id']) <= 320 for e in evidence)):
                raise ValueError(f'Choose 1 to {MAX_EVIDENCE} receipts as {{run_id, call_id}}.')
            refs = list(dict.fromkeys((e['run_id'], e['call_id']) for e in evidence))
        self._audit(cid, gid, refs or ())

        def change(db, goal):
            criterion = self._criterion(db, gid, ident)
            if criterion['status'] == 'candidate':
                raise ValueError('Accept this proposed criterion before checking it.')
            if criterion['status'] == 'rejected':
                raise ValueError('This criterion was rejected.')
            now = time.time()
            if criterion['kind'] == 'human_check' or not met:
                if criterion['kind'] == 'human_check' and refs is not None:
                    raise ValueError('A human check records your judgment and takes no receipts.')
                status = 'met' if met else 'unmet'
                db.execute("UPDATE goal_criteria SET status=?,checked_by='human',evidence_ids='[]',updated=? "
                           'WHERE goal_id=? AND id=?', (status, now, gid, ident))
                self._event(db, gid, 'criterion_checked', 'human', {'id': ident, 'status': status, 'checked_by': 'human'})
                return ident
            criterion['evidence_ids'] = [list(r) for r in refs] if refs else []
            found, waiting = self._refs(criterion, self._steps(db, gid))
            if not found:
                raise ValueError(f'Criterion {ident} has no evidence yet: {waiting}.')
            ctx = self._context(db, goal)
            problems = [p for p in (self._problem(db, ctx, rid, call) for rid, call in found) if p]
            if problems:
                raise ValueError(f'Criterion {ident} is not met: {problems[0]}.')
            db.execute("UPDATE goal_criteria SET status='met',checked_by='host',evidence_ids=?,updated=? "
                       'WHERE goal_id=? AND id=?', (encoded(criterion['evidence_ids']), now, gid, ident))
            self._event(db, gid, 'criterion_checked', 'human', {'id': ident, 'status': 'met', 'checked_by': 'host',
                        'evidence': [list(r) for r in found]})
            return ident
        return self._mutate(cid, gid, revision, change)

    def review_criterion(self, cid, gid, ident, decision, revision=None):
        if decision not in ('accept', 'reject'):
            raise ValueError('Accept or reject the proposed criterion.')

        def change(db, goal):
            criterion = self._criterion(db, gid, ident)
            if criterion['status'] != 'candidate':
                raise ValueError('Only a proposed criterion can be accepted or rejected.')
            criteria = self._criteria(db, gid)
            if decision == 'accept' and sum(c['status'] not in ('candidate', 'rejected') for c in criteria) >= MAX_CRITERIA:
                raise ValueError(f'A goal holds up to {MAX_CRITERIA} criteria.')
            status = 'open' if decision == 'accept' else 'rejected'
            db.execute('UPDATE goal_criteria SET status=?,updated=? WHERE goal_id=? AND id=?', (status, time.time(), gid, ident))
            self._event(db, gid, 'criterion_' + decision + 'ed', 'human', {'id': ident})
            return ident
        return self._mutate(cid, gid, revision, change)

    def _step_refs(self, db, gid, criteria):
        known = {c['id']: c for c in self._criteria(db, gid)}
        for ident in criteria:
            item = known.get(ident)
            if not item or item['status'] in ('candidate', 'rejected'):
                raise ValueError(f'Criterion {ident} is not an accepted criterion of this goal.')
            if item['kind'] != 'evidence':
                raise ValueError('Steps can only prove evidence criteria; a human check needs your click.')

    def add_step(self, cid, gid, title, depends_on=None, criteria=None, revision=None):
        title = _text(title, TITLE, 'step title')
        depends_on = _ids(depends_on, 'depends_on', MAX_DEPENDS)
        criteria = _ids(criteria, 'criteria', MAX_CRITERIA)

        def change(db, goal):
            steps = self._steps(db, gid)
            if len(steps) >= MAX_STEPS:
                raise ValueError(f'A goal holds up to {MAX_STEPS} steps.')
            ordinal = max((s['ordinal'] for s in steps), default=0) + 1
            ident = f's{ordinal}'
            self._step_refs(db, gid, criteria)
            _graph(steps + [{'id': ident, 'status': 'pending', 'depends_on': depends_on}])
            db.execute('INSERT INTO goal_steps VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (gid, ident, ordinal, title, 'pending', encoded(depends_on), encoded(criteria), '[]',
                        None, None, None, time.time()))
            self._event(db, gid, 'step_added', 'human', {'id': ident, 'title': title, 'depends_on': depends_on,
                        'criteria': criteria})
            return ident
        return self._mutate(cid, gid, revision, change)

    def edit_step(self, cid, gid, ident, title=None, depends_on=None, criteria=None, revision=None):
        if title is None and depends_on is None and criteria is None:
            raise ValueError('Change the title, dependencies or criteria.')
        title = None if title is None else _text(title, TITLE, 'step title')
        depends_on = None if depends_on is None else _ids(depends_on, 'depends_on', MAX_DEPENDS)
        criteria = None if criteria is None else _ids(criteria, 'criteria', MAX_CRITERIA)

        def change(db, goal):
            steps = self._steps(db, gid)
            step = next((s for s in steps if s['id'] == ident), None)
            if not step:
                raise ValueError('Step not found in this goal.')
            update = {'title': step['title'] if title is None else title,
                      'depends_on': step['depends_on'] if depends_on is None else depends_on,
                      'criteria': step['criteria'] if criteria is None else criteria}
            self._step_refs(db, gid, update['criteria'])
            _graph([{**s, **update} if s['id'] == ident else s for s in steps])
            db.execute('UPDATE goal_steps SET title=?,depends_on=?,criteria=?,updated=? WHERE goal_id=? AND id=?',
                       (update['title'], encoded(update['depends_on']), encoded(update['criteria']), time.time(), gid, ident))
            self._event(db, gid, 'step_edited', 'human', {'id': ident, **update})
            return ident
        return self._mutate(cid, gid, revision, change)

    def block_step(self, cid, gid, ident, text=None, revision=None):
        """The person blocks a step with their own reason, or clears any blocker (the step returns to pending)."""
        text = None if text is None else _text(text, BLOCKER_TEXT, 'blocker')

        def change(db, goal):
            steps = self._steps(db, gid)
            step = next((s for s in steps if s['id'] == ident), None)
            if not step:
                raise ValueError('Step not found in this goal.')
            if text is None:
                if step['status'] != 'blocked':
                    raise ValueError('This step is not blocked.')
                status, kind = 'pending', None
            else:
                if step['status'] == 'completed':
                    raise ValueError('This step is completed.')
                status, kind = 'blocked', 'text'
            _graph([{**s, 'status': status} if s['id'] == ident else s for s in steps])
            db.execute("UPDATE goal_steps SET status=?,blocker_kind=?,blocker_ref=?,evidence_ids='[]',updated=? "
                       'WHERE goal_id=? AND id=?', (status, kind, text, time.time(), gid, ident))
            self._event(db, gid, 'step_blocked' if text else 'step_unblocked', 'human', {'id': ident, 'text': text})
            return ident
        return self._mutate(cid, gid, revision, change)

    def continuation_text(self, cid, gid):
        with self.store.lock:
            goal = self._goal(self.store.db, cid, gid)
        if goal['state'] not in ('active', 'blocked'):
            raise ValueError('Resume this goal before continuing it.' if goal['state'] == 'paused'
                             else f"This goal is {goal['state']}.")
        return (f"Continue working toward the goal \"{goal['title']}\". Use the HOST GOAL record for its criteria, "
                'steps and blockers.')

    def continue_goal(self, cid, gid):
        """Native "Continue": a new run in this conversation, linked to the goal at its first model turn."""
        return self.app.send(cid, self.continuation_text(cid, gid))

    def methods(self):
        """Trusted native routes for the parent's dispatcher (pet: goal_list and goal_create only)."""
        return {
            'goal_list': lambda chat_id, limit=20: self.list(chat_id, limit),
            'goal_get': lambda chat_id, goal_id: self.get(chat_id, goal_id),
            'goal_create': lambda chat_id, title: self.create(chat_id, title),
            'goal_state': lambda chat_id, goal_id, revision, state:
                self.set_state(chat_id, goal_id, state, _revision(revision)),
            'goal_criterion_add': lambda chat_id, goal_id, revision, text, kind='human_check':
                self.add_criterion(chat_id, goal_id, text, kind, _revision(revision)),
            'goal_criterion_edit': lambda chat_id, goal_id, revision, criterion_id, text:
                self.edit_criterion(chat_id, goal_id, criterion_id, text, _revision(revision)),
            'goal_criterion_check': lambda chat_id, goal_id, revision, criterion_id, met=True, evidence=None:
                self.check_criterion(chat_id, goal_id, criterion_id, met, evidence, _revision(revision)),
            'goal_criterion_review': lambda chat_id, goal_id, revision, criterion_id, decision:
                self.review_criterion(chat_id, goal_id, criterion_id, decision, _revision(revision)),
            'goal_step_add': lambda chat_id, goal_id, revision, title, depends_on=None, criteria=None:
                self.add_step(chat_id, goal_id, title, depends_on, criteria, _revision(revision)),
            'goal_step_edit': lambda chat_id, goal_id, revision, step_id, title=None, depends_on=None, criteria=None:
                self.edit_step(chat_id, goal_id, step_id, title, depends_on, criteria, _revision(revision)),
            'goal_step_block': lambda chat_id, goal_id, revision, step_id, text=None:
                self.block_step(chat_id, goal_id, step_id, text, _revision(revision)),
            'goal_blockers': lambda chat_id, goal_id=None: self.blockers(chat_id, goal_id),
            'goal_continue': lambda chat_id, goal_id: self.continue_goal(chat_id, goal_id),
        }

    # Host and agent routes ---------------------------------------------------------------------

    def link_run(self, rid):
        """Host: link a run to its conversation's active goal; idempotent. Returns the goal ID or None."""
        with self.store.lock, self.store.db as db:
            run = db.execute('SELECT chat_id FROM runs WHERE id=?', (rid,)).fetchone()
            goal = run and db.execute("SELECT id FROM goals WHERE chat_id=? AND state IN ('active','blocked')",
                                      (run[0],)).fetchone()
            if not goal:
                return None
            if not db.execute("SELECT 1 FROM goal_events WHERE goal_id=? AND kind='run_linked' AND run_id=?",
                              (goal[0], rid)).fetchone():
                if not self._event(db, goal[0], 'run_linked', 'host', {'run_id': rid}, run_id=rid, required=False):
                    return None
            return goal[0]

    def offer(self, rid):
        """Whether UpdateGoal applies to this run (the parent also requires a budget above 4096 tokens)."""
        return self.link_run(rid) is not None

    def host_message(self, rid, small=False):
        """Bounded HOST GOAL content for the runner, or None when no active goal applies."""
        gid = self.link_run(rid)
        if not gid:
            return None
        with self.store.lock, self.store.db as db:
            cid = db.execute('SELECT chat_id FROM runs WHERE id=?', (rid,)).fetchone()[0]
            goal = self._goal(db, cid, gid)
            view = self._evaluate(db, goal)
        width, title_width, limit = (60, 120, 480) if small else (TEXT, TITLE, 6000)

        def clip(text, size=width):
            return text if len(text) <= size else text[:size - 1] + '…'
        criteria = sorted((c for c in view['criteria'] if c['status'] not in ('candidate', 'rejected')),
                          key=lambda c: c['status'] == 'met')
        steps = sorted(view['steps'], key=lambda s: s['status'] == 'completed')
        payload = {'goal_id': gid, 'title': clip(goal['title'], title_width), 'state': goal['state'],
                   'revision': goal['revision'],
                   'criteria': [{'id': c['id'], 'kind': c['kind'], 'status': c['status'], 'text': clip(c['text']),
                                 # X17 O1: an agent proposal the person accepted says so (large form only).
                                 **({'origin': 'accepted_proposal'} if not small and c.get('origin') == 'agent' else {})}
                                for c in criteria],
                   'steps': [{k: v for k, v in {'id': s['id'], 'title': clip(s['title']), 'status': s['status'],
                              'depends_on': s['depends_on'] or None,
                              'blocked_by': s['blocker_kind'] if s['status'] == 'blocked' else None}.items()
                              if v is not None} for s in steps],
                   'blockers': [b['kind'] if small else {'kind': b['kind'], 'label': b['label']} for b in view['blockers'][:8]],
                   'candidates_waiting': self._progress(view)['candidates']}
        omitted = {'criteria': 0, 'steps': 0}

        def size():
            return len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
        while size() > limit and (payload['criteria'] or payload['steps']):
            key = 'steps' if len(payload['steps']) >= len(payload['criteria']) and payload['steps'] else 'criteria'
            payload[key].pop()
            omitted[key] += 1
            payload['omitted'] = omitted
        return (SMALL_HEADER if small else LARGE_HEADER) + json.dumps(payload, ensure_ascii=False, separators=(',', ':'))

    def snapshot_for_run(self, rid):
        """The goal a run was linked to, for run_get and handoff packets; None when unlinked."""
        with self.store.lock, self.store.db as db:
            row = db.execute("SELECT e.goal_id,g.chat_id FROM goal_events e JOIN goals g ON g.id=e.goal_id "
                             "WHERE e.run_id=? AND e.kind='run_linked' ORDER BY e.created DESC LIMIT 1", (rid,)).fetchone()
            if not row:
                return None
            goal = self._goal(db, row[1], row[0])
            snapshot = self._snapshot(goal, self._evaluate(db, goal))
        keep = ('id', 'origin', 'title', 'revision', 'state', 'progress', 'authorization', 'note')
        # Accepted criteria in full; agent candidates and rejected criteria as counts only (X17 F9), so an agent
        # cannot grow the record past the handoff's cap with proposals.
        criteria = [c for c in snapshot['criteria'] if c['status'] not in ('candidate', 'rejected')]
        record = {**{k: snapshot[k] for k in keep},
                  'criteria': [{k: c[k] for k in ('id', 'kind', 'status', 'origin', 'text', 'evidence')} for c in criteria],
                  'candidates': sum(c['status'] == 'candidate' for c in snapshot['criteria']),
                  'rejected_criteria': sum(c['status'] == 'rejected' for c in snapshot['criteria']),
                  'steps': [{k: s[k] for k in ('id', 'title', 'status', 'depends_on', 'criteria', 'evidence')}
                            for s in snapshot['steps']],
                  'blockers': [{k: b[k] for k in ('kind', 'ref', 'label')} for b in snapshot['blockers']]}

        def size():
            return len(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())
        while size() > SNAPSHOT_LIMIT and (record['steps'] or record['criteria'] or record['blockers']):
            key = max(('blockers', 'steps', 'criteria'), key=lambda k: len(record[k]))
            record[key].pop()
            record.setdefault('omitted', {'criteria': 0, 'steps': 0, 'blockers': 0})[key] += 1
        return record

    def agent_update(self, rid, steps=None, candidates=None, call_id=None):
        """The UpdateGoal tool: status of existing steps and criterion proposals; nothing else."""
        updates = None if steps in (None, '') else _array(steps, 'steps', MAX_STEPS)
        proposals = None if candidates in (None, '') else _array(candidates, 'candidates', 3)
        if updates is None and proposals is None:
            raise ValueError('Send steps, candidates or both.')
        parsed, seen = [], set()
        for item in updates or ():
            if not isinstance(item, dict) or set(item) - {'id', 'status', 'evidence_ids'}:
                raise ValueError('Agents update existing step status only (id, status, evidence_ids). '
                                 'Titles, dependencies and criteria are human-authored.')
            ident, status, evidence = item.get('id'), item.get('status'), item.get('evidence_ids', [])
            if not isinstance(ident, str) or ident in seen or status not in STEP_STATUSES:
                raise ValueError('Step updates need unique step IDs and a supported status.')
            if (not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE or len(set(evidence)) != len(evidence)
                    or any(not isinstance(e, str) or not 1 <= len(e) <= 320 for e in evidence)):
                raise ValueError(f'evidence_ids lists up to {MAX_EVIDENCE} unique receipt IDs from this run.')
            seen.add(ident)
            parsed.append((ident, status, evidence))
        offered = []
        for item in proposals or ():
            if not isinstance(item, dict) or set(item) - {'text', 'kind'} or item.get('kind', 'human_check') not in CRITERION_KINDS:
                raise ValueError('Criterion candidates accept text and kind (human_check or evidence).')
            offered.append((_text(item.get('text'), TEXT, 'criterion'), item.get('kind', 'human_check')))
        if len({_key(t) for t, _ in offered}) != len(offered):
            raise ValueError('Propose each criterion once.')
        with self.store.lock, self.store.db as db:
            run = db.execute('SELECT chat_id,state FROM runs WHERE id=?', (rid,)).fetchone()
            if not run or run[1] != 'running':
                raise ValueError('Only a running agent can update its goal.')
            gid = self._current_id(db, run[0])
            if not gid:
                raise ValueError('This conversation has no current goal.')
            goal = self._goal(db, run[0], gid)
            if goal['state'] == 'paused':
                raise ValueError('The person paused this goal; only they can resume it.')
            ctx = self._context(db, goal)
            if rid not in ctx['linked']:
                raise ValueError('This run is not linked to the current goal.')
            current = {s['id']: s for s in self._steps(db, gid)}
            pending = RunInteractions.pending_records(db, rid)
            changed = []
            for ident, status, evidence in parsed:
                step = current.get(ident)
                if step is None:
                    raise ValueError(f'Step {ident} is not part of this goal. Agents cannot add steps.')
                if step['status'] == 'blocked' and step['blocker_kind'] == 'text':
                    raise ValueError(f'The person blocked step {ident}; only they can clear it.')
                if call_id in evidence:
                    raise ValueError('A goal update cannot cite itself as evidence.')
                blocker, refs = (None, None), []
                if status == 'completed':
                    if not evidence:
                        raise ValueError('Completed steps need resolved, current receipts from this run.')
                    for call in evidence:
                        problem = self._problem(db, ctx, rid, call)
                        if problem:
                            raise ValueError(f'Step {ident} cannot complete: {problem}.')
                    refs = [[rid, call] for call in evidence]
                elif status == 'blocked':
                    if evidence:
                        for call in evidence:
                            row = db.execute('SELECT state,result FROM run_tools WHERE run_id=? AND call_id=?', (rid, call)).fetchone()
                            if not row:
                                raise ValueError('Evidence must reference this run\'s tool receipts.')
                            if RunInteractions.resolved_evidence({'state': row[0], 'result': json.loads(row[1]) if row[1] else None}):
                                raise ValueError(f'Blocked steps cite failed receipts; {call} succeeded.')
                        blocker = ('failure', evidence[0])
                    elif pending['questions']:
                        blocker = ('question', pending['questions'][0][0])
                    elif pending['actions']:
                        blocker = ('approval', pending['actions'][0])
                    else:
                        raise ValueError('A blocked step needs a host-recorded blocker: a pending question or review '
                                         'in this run, or a failed receipt from this run in evidence_ids.')
                elif evidence:
                    raise ValueError('Only completed or blocked steps cite receipts.')
                current[ident] = {**step, 'status': status, 'evidence_ids': refs, 'blocker_kind': blocker[0],
                                  'blocker_ref': blocker[1], 'run_id': rid}
                changed.append({'id': ident, 'status': status, 'evidence_ids': evidence, 'blocker_kind': blocker[0]})
            _graph(list(current.values()))
            now = time.time()
            for item in changed:
                step = current[item['id']]
                db.execute('UPDATE goal_steps SET status=?,evidence_ids=?,blocker_kind=?,blocker_ref=?,run_id=?,updated=? '
                           'WHERE goal_id=? AND id=?', (step['status'], encoded(step['evidence_ids']), step['blocker_kind'],
                                                        step['blocker_ref'], rid, now, gid, step['id']))
            criteria = self._criteria(db, gid)
            proposed = []
            if offered:
                if sum(c['status'] == 'candidate' for c in criteria) + len(offered) > MAX_CANDIDATES:
                    raise ValueError('Wait for the person to review proposed criteria before proposing more.')
                ordinal = max((c['ordinal'] for c in criteria), default=0)
                for text, kind in offered:
                    old = next((c for c in criteria if _key(c['text']) == _key(text)), None)
                    if old:
                        raise ValueError('The person already rejected that criterion.' if old['status'] == 'rejected'
                                         else 'This goal already has that criterion.')
                    ordinal += 1
                    db.execute('INSERT INTO goal_criteria VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (gid, f'c{ordinal}', ordinal, text, kind, 'candidate', 'agent', '[]', None, rid, now))
                    proposed.append(f'c{ordinal}')
            if changed:
                self._event(db, gid, 'steps_updated', 'agent', {'steps': changed}, run_id=rid)
            if proposed:
                self._event(db, gid, 'criteria_proposed', 'agent', {'ids': proposed}, run_id=rid)
            db.execute('UPDATE goals SET revision=revision+1,updated=? WHERE id=?', (now, gid))
            revision = goal['revision'] + 1
        self._emit(run[0], gid)
        return {'status': 'updated', 'goal_id': gid, 'revision': revision,
                'steps': [{'id': c['id'], 'status': c['status']} for c in changed], 'candidates': proposed,
                'authorization': 'none',
                'note': 'Proposed criteria wait for the person. Only they accept or check criteria and finish the goal.'}
