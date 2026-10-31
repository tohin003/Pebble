"""Durable host inputs, native questions, plans and continuation intent.

Only the host writes human-origin input. A model can request a question but
cannot answer one, approve an effect, or choose an input's origin.
"""

import copy
import json
import re
import time
import uuid

from .runtime import LIVE, RESUMABLE, encoded


ID = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
RESERVED = {'__proto__', 'prototype', 'constructor', '__text'}
SECRET = re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})\b')
SECRET_REQUEST = re.compile(r'\b(?:enter|paste|provide|send|type|submit)\b.{0,40}\b(?:password|api[ -]?key|secret[ -]?key|access[ -]?token|private[ -]?key)\b', re.I)



def answer_words(items, answers):
    """Phase 45 (F249, audit round R11): the person's answer as they chose it, for the conversation (it showed as
    "Clarification answer (native form): {"open": {"option_id": "yes", ...}}"): the chosen option or typed text, with
    each question's title when there are several."""
    said = [(item.get('title') or '', (answers.get(item.get('id')) or {}).get('text') or '')
            for item in items if isinstance(item, dict) and isinstance(answers.get(item.get('id')), dict)]
    said = [(title, text) for title, text in said if text]
    if len(said) == 1:
        return said[0][1]
    return '\n'.join(f'{title}: {text}' if title else text for title, text in said) or 'Answered.'


# Phase 46 (1.9.28, golden held-out parallel_work: one sol cell ended `interrupted`): the inbox stores each input under
# INPUT_BYTES, and an approved command's or test run's receipt alone can hold 16,000 bytes of output, so a reviewed
# command that printed more than about 15,600 bytes failed here after its receipt was committed and App._approve ended
# the run "interrupted". A host or agent input copies a result the host keeps whole (the tool receipt, and HOST ACTION
# UPDATES on resume), so it is shortened to fit instead of refused; a person's own input stays exact.
INPUT_BYTES = 16000
LEFT_OUT = ('[... {count} {unit} left out of this copy; the whole result is in the tool receipt '
            '(HOST ACTION UPDATES for an approved action) ...]')


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode())


def _cut(value, keep, depth=0):
    if isinstance(value, str) and len(value) > 2 * keep + 200:
        return value[:keep] + '\n' + LEFT_OUT.format(count=len(value) - 2 * keep, unit='characters') + '\n' + value[-keep:]
    if depth > 8 or not isinstance(value, (dict, list)):
        return value
    if isinstance(value, dict):
        return {key: _cut(item, keep, depth + 1) for key, item in value.items()}
    items, ends = [_cut(item, keep, depth + 1) for item in value], max(2, keep // 40)
    if len(items) <= 2 * ends + 1:
        return items
    return items[:ends] + [LEFT_OUT.format(count=len(items) - 2 * ends, unit='items')] + items[-ends:]


def fitted(payload, limit=INPUT_BYTES):
    """A host or agent input that fits the inbox: unchanged when it does; else its long texts and lists keep their start
    and end around a marker, shorter each round; else only its short top-level fields."""
    if _size(payload) <= limit:
        return payload
    if isinstance(payload, dict):
        for keep in (4000, 1000, 250, 60):
            cut = {**_cut(payload, keep), 'shortened_for_input': True}
            if _size(cut) <= limit:
                return cut
    short = {}
    for key, value in payload.items() if isinstance(payload, dict) else ():
        if (value is None or isinstance(value, (bool, int, float)) or isinstance(value, str) and len(value) <= 200) \
                and _size({**short, key: value, 'shortened_for_input': True}) <= limit:
            short[key] = value
    return {**short, 'shortened_for_input': True}

class WaitingForInput(Exception):
    pass


def bounded_text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'Enter nonempty text under {limit} characters.')
    return value.strip()


def question_items(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise ValueError('Ask one to three questions at a time.')
    result, ids = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {'id', 'title', 'options'}:
            raise ValueError('Questions accept id, title and optional options.')
        ident = item.get('id')
        if not isinstance(ident, str) or not ID.fullmatch(ident) or ident in ids or ident in RESERVED:
            raise ValueError('Question item IDs must be unique short identifiers.')
        ids.add(ident)
        title = bounded_text(item.get('title'), 500)
        if SECRET_REQUEST.search(title) or SECRET.search(title):
            raise ValueError('Credentials belong in native connection settings, not questions.')
        options = item.get('options', [])
        if not isinstance(options, list) or len(options) > 6:
            raise ValueError('Use up to six options; free text is always available.')
        choices, option_ids = [], set()
        for option in options:
            if not isinstance(option, dict) or set(option) - {'id', 'label', 'description'}:
                raise ValueError('Options accept id, label and optional description.')
            oid = option.get('id')
            if not isinstance(oid, str) or not ID.fullmatch(oid) or oid in option_ids or oid in RESERVED:
                raise ValueError('Option IDs must be unique short identifiers.')
            option_ids.add(oid)
            label = bounded_text(option.get('label'), 120)
            description = bounded_text(option['description'], 300) if 'description' in option else ''
            if SECRET.search(label + description) or SECRET_REQUEST.search(label + description):
                raise ValueError('Credentials belong in native connection settings, not questions.')
            choices.append({'id': oid, 'label': label, 'description': description})
        result.append({'id': ident, 'title': title, 'options': choices, 'allow_free_text': True})
    return result


class RunInteractions:
    def __init__(self, store, journal):
        self.store, self.journal = store, journal
        with store.lock, store.db as db:
            db.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS run_inbox(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
              kind TEXT NOT NULL, origin TEXT NOT NULL, payload TEXT NOT NULL,
              status TEXT NOT NULL, created REAL NOT NULL, delivered_step INTEGER,
              UNIQUE(run_id,sequence));
            CREATE TABLE IF NOT EXISTS run_questions(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, call_id TEXT NOT NULL,
              revision INTEGER NOT NULL, required INTEGER NOT NULL, status TEXT NOT NULL,
              items TEXT NOT NULL, answer TEXT, created REAL NOT NULL, answered REAL,
              UNIQUE(run_id,call_id));
            CREATE TABLE IF NOT EXISTS run_continuations(
              run_id TEXT PRIMARY KEY, reason TEXT NOT NULL, state TEXT NOT NULL,
              error TEXT, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS run_plans(
              run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, steps TEXT NOT NULL,
              updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS run_inbox_pending ON run_inbox(run_id,status,sequence);
            CREATE INDEX IF NOT EXISTS run_questions_owner ON run_questions(run_id,status);
            COMMIT;
            ''')
            # A dispatched continuation that crashed is not silently restarted.
            db.execute("UPDATE run_continuations SET state=CASE WHEN EXISTS(SELECT 1 FROM runs WHERE id=run_id AND state IN ('waiting_input','waiting_approval')) THEN 'queued' ELSE 'blocked' END,error='Continuation interrupted; inspect the run before resuming.' WHERE state='dispatching'")
        journal.interactions = self

    @staticmethod
    def _input(db, rid, kind, origin, payload, ident=None):
        ident = ident or uuid.uuid4().hex
        data = encoded(payload if origin == 'human' else fitted(payload), INPUT_BYTES)
        old = db.execute('SELECT run_id,kind,origin,payload FROM run_inbox WHERE id=?', (ident,)).fetchone()
        if old:
            if old != (rid, kind, origin, data):
                raise ValueError('This input identifier already belongs to a different input.')
            return ident
        if db.execute("SELECT count(*) FROM run_inbox WHERE run_id=? AND status='pending'", (rid,)).fetchone()[0] >= 100:
            raise ValueError('This run has too many pending inputs. Wait for it to consume them.')
        seq = db.execute('SELECT coalesce(max(sequence),0)+1 FROM run_inbox WHERE run_id=?', (rid,)).fetchone()[0]
        db.execute('INSERT INTO run_inbox VALUES(?,?,?,?,?,?,?, ?,NULL)', (ident, rid, seq, kind, origin, data, 'pending', time.time()))
        return ident

    @staticmethod
    def _queue(db, rid, reason):
        db.execute("INSERT INTO run_continuations VALUES(?,?,'queued',NULL,?) ON CONFLICT(run_id) DO UPDATE SET reason=excluded.reason,state='queued',error=NULL,updated=excluded.updated", (rid, reason, time.time()))

    def steer(self, rid, text, input_id=None):
        text = bounded_text(text, 8000)
        if input_id is not None and (not isinstance(input_id, str) or not ID.fullmatch(input_id)):
            raise ValueError('Use a short input identifier.')
        with self.store.lock, self.store.db as db:
            state = db.execute('SELECT state FROM runs WHERE id=?', (rid,)).fetchone()
            if not state or state[0] not in LIVE | RESUMABLE:
                raise ValueError('This run cannot accept follow-up input.')
            ident = self._input(db, rid, 'steering', 'human', {'text': text}, input_id)
            self.journal.event(db, rid, 'input_queued', {'input_id': ident, 'kind': 'steering', 'origin': 'human'})
            if getattr(self, 'agents', None):
                self.agents.tree._wake(db, rid)
            if state[0] in {'waiting_input', 'waiting_approval'} and self._ready(db, rid):
                self._queue(db, rid, 'human_followup')
        return {'input_id': ident, 'run_id': rid, 'status': 'queued'}

    def artifact_result_in(self, db, cid, rid, payload, ident):
        """Caller owns the transaction: a result the person chose to share from a view, delivered once.

        The host builds the payload; its origin is always human and it never changes a permission.
        """
        state = db.execute('SELECT state FROM runs WHERE id=? AND chat_id=?', (rid, cid)).fetchone()
        if not state or state[0] not in LIVE | RESUMABLE:
            raise ValueError('This run cannot accept a shared result.')
        ident = self._input(db, rid, 'artifact_result', 'human', payload, ident)
        self.journal.event(db, rid, 'input_queued', {'input_id': ident, 'kind': 'artifact_result', 'origin': 'human'})
        if getattr(self, 'agents', None):
            self.agents.tree._wake(db, rid)
        if state[0] in {'waiting_input', 'waiting_approval'} and self._ready(db, rid):
            self._queue(db, rid, 'artifact_result')
        return {'input_id': ident, 'run_id': rid, 'status': 'queued', 'origin': 'human'}

    def create_question(self, rid, call_id, items, required):
        with self.store.lock, self.store.db as db:
            ident = self.create_question_in(db, rid, call_id, items, required)
        return self.question(ident)

    def create_question_in(self, db, rid, call_id, items, required):
        """Caller owns the transaction; artifact publication shares this boundary."""
        if type(required) is not bool:
            raise ValueError('required must be a boolean.')
        items = question_items(items)
        state = db.execute('SELECT state FROM runs WHERE id=?', (rid,)).fetchone()
        if not state or state[0] != 'running':
            raise ValueError('Only a running agent can request a question.')
        if db.execute('SELECT 1 FROM run_questions WHERE run_id=? AND call_id=?', (rid, call_id)).fetchone():
            raise ValueError('This tool already created a question.')
        if db.execute("SELECT count(*) FROM run_questions WHERE run_id=? AND status='pending'", (rid,)).fetchone()[0] >= 4:
            raise ValueError('Resolve existing questions before asking more.')
        ident = uuid.uuid4().hex
        db.execute('INSERT INTO run_questions VALUES(?,?,?,1,?,?,?,NULL,?,NULL)', (ident, rid, call_id, required, 'pending', encoded(items), time.time()))
        self.journal.event(db, rid, 'question_requested', {'question_id': ident, 'required': required, 'revision': 1})
        return ident

    def question(self, ident, cid=None):
        rows = self.store.query('SELECT q.*,r.chat_id FROM run_questions q JOIN runs r ON r.id=q.run_id WHERE q.id=?' + (' AND r.chat_id=?' if cid else ''), (ident, cid) if cid else (ident,))
        if not rows:
            raise ValueError('Question not found in this conversation.')
        item = rows[0]
        item['items'] = json.loads(item['items'])
        item['answer'] = json.loads(item['answer']) if item['answer'] else None
        item['required'] = bool(item['required'])
        if getattr(self, 'agents', None):
            item.update(self.agents.question_metadata(ident))
        return item

    def questions(self, cid, pending=True):
        rows = self.store.query("SELECT q.id FROM run_questions q JOIN runs r ON r.id=q.run_id WHERE r.chat_id=?" + (" AND q.status='pending'" if pending else '') + ' ORDER BY q.created LIMIT 50', (cid,))
        return [self.question(r['id'], cid) for r in rows]

    @staticmethod
    def canonical_answer(items, answers):
        if not isinstance(answers, dict) or set(answers) != {i['id'] for i in items}:
            raise ValueError('Answer each requested question exactly once.')
        result = {}
        for item in items:
            answer = answers[item['id']]
            if not isinstance(answer, dict) or set(answer) not in ({'option_id'}, {'text'}):
                raise ValueError('Choose an option or enter free text.')
            if 'option_id' in answer:
                option = next((o for o in item['options'] if o['id'] == answer['option_id']), None)
                if not option:
                    raise ValueError('Choose an offered option or use free text.')
                result[item['id']] = {'option_id': option['id'], 'text': option['label']}
            else:
                text = bounded_text(answer['text'], 2000)
                if SECRET.search(text):
                    raise ValueError('Use native connection settings for credentials. This answer was not saved.')
                result[item['id']] = {'text': text}
        return result

    def answer(self, cid, ident, revision, answers):
        with self.store.lock, self.store.db as db:
            return self.answer_in(db, cid, ident, revision, answers)

    def answer_in(self, db, cid, ident, revision, answers):
        row = db.execute('SELECT q.run_id,q.revision,q.status,q.items,q.answer,r.state FROM run_questions q JOIN runs r ON r.id=q.run_id WHERE q.id=? AND r.chat_id=?', (ident, cid)).fetchone()
        if not row:
            raise ValueError('Question not found in this conversation.')
        rid, version, status, items, old_answer, state = row
        if type(revision) is not int or revision != version:
            raise ValueError('This question revision is stale. Refresh it before answering.')
        answer = encoded(self.canonical_answer(json.loads(items), answers))
        message = {'id': 'answer-' + ident, 'role': 'user', 'content': answer_words(json.loads(items), json.loads(answer))}
        if status == 'answered' and answer == old_answer:
            return {'question_id': ident, 'run_id': rid, 'status': 'answered', 'message': message}
        if status != 'pending' or state not in LIVE | RESUMABLE:
            raise ValueError('This question is no longer accepting answers.')
        if state in {'stopped', 'failed', 'budget_exhausted', 'verification_required'}:
            raise ValueError('Resume this stopped run explicitly before answering.')
        if getattr(self, 'agents', None):
            self.agents.relay_answer_in(db, rid, ident, answer)
        db.execute("UPDATE run_questions SET status='answered',answer=?,answered=? WHERE id=?", (answer, time.time(), ident))
        self._refresh_question_receipt(db, rid, ident)
        self._input(db, rid, 'question_answer', 'human', {'question_id': ident, 'revision': version, 'answers': json.loads(answer)}, 'answer-' + ident)
        if getattr(self, 'agents', None):
            self.agents.tree._wake(db, rid)
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (message['id'], cid, 'user', message['content'], time.time()))
        db.execute('UPDATE chats SET updated=? WHERE id=?', (time.time(), cid))
        self.journal.event(db, rid, 'question_answered', {'question_id': ident, 'revision': version, 'origin': 'human'})
        if state == 'waiting_input' and self._ready(db, rid):
            self._queue(db, rid, 'question_answered')
        return {'question_id': ident, 'run_id': rid, 'status': 'answered', 'message': message}

    def cancel_question(self, cid, ident, revision):
        with self.store.lock, self.store.db as db:
            return self.cancel_question_in(db, cid, ident, revision)

    def cancel_question_in(self, db, cid, ident, revision):
        row = db.execute('SELECT q.run_id,q.revision,q.required,q.status,r.state FROM run_questions q JOIN runs r ON r.id=q.run_id WHERE q.id=? AND r.chat_id=?', (ident, cid)).fetchone()
        if not row or type(revision) is not int or revision != row[1]:
            raise ValueError('Question not found or revision is stale.')
        rid, _, required, status, state = row
        if status == 'cancelled':
            return {'question_id': ident, 'run_id': rid, 'status': 'cancelled', 'required': bool(required)}
        if status != 'pending':
            raise ValueError('This question is no longer pending.')
        if getattr(self, 'agents', None):
            self.agents.relay_answer_in(db, rid, ident, None, cancelled=True)
        db.execute("UPDATE run_questions SET status='cancelled',answered=? WHERE id=?", (time.time(), ident))
        self._refresh_question_receipt(db, rid, ident)
        self._input(db, rid, 'question_cancelled', 'human', {'question_id': ident, 'required': bool(required)}, 'cancel-' + ident)
        if getattr(self, 'agents', None):
            self.agents.tree._wake(db, rid)
        if required:
            db.execute("UPDATE runs SET state='stopped',updated=? WHERE id=? AND state='waiting_input'", (time.time(), rid))
            db.execute("UPDATE run_continuations SET state='cancelled',updated=? WHERE run_id=?", (time.time(), rid))
        elif state == 'waiting_input' and self._ready(db, rid):
            self._queue(db, rid, 'optional_question_cancelled')
        self.journal.event(db, rid, 'question_cancelled', {'question_id': ident, 'required': bool(required)})
        return {'question_id': ident, 'run_id': rid, 'status': 'cancelled', 'required': bool(required)}

    @staticmethod
    def _required(db, rid):
        return bool(db.execute("SELECT 1 FROM run_questions WHERE run_id=? AND required=1 AND status='pending' LIMIT 1", (rid,)).fetchone())

    @staticmethod
    def resolve_question_receipt(db, rid, call_id, result):
        if not isinstance(result, dict) or not result.get('question_id'):
            return result
        row = db.execute('SELECT status FROM run_questions WHERE id=? AND run_id=? AND call_id=?', (result['question_id'], rid, call_id)).fetchone()
        if row and row[0] in {'answered', 'cancelled'}:
            origin = 'human'
            if row[0] == 'cancelled' and db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_question_cancellations'").fetchone():
                cancellation = db.execute('SELECT origin FROM agent_question_cancellations WHERE question_id=? AND run_id=?', (result['question_id'], rid)).fetchone()
                if cancellation:
                    origin = cancellation[0]
            return {**result, 'status': row[0], 'answer_origin': origin}
        return result

    @classmethod
    def _refresh_question_receipt(cls, db, rid, ident):
        row = db.execute("SELECT t.call_id,t.result FROM run_tools t JOIN run_questions q ON q.run_id=t.run_id AND q.call_id=t.call_id WHERE q.id=? AND t.run_id=? AND t.state='completed'", (ident, rid)).fetchone()
        if row and row[1]:
            refreshed = cls.resolve_question_receipt(db, rid, row[0], json.loads(row[1]))
            db.execute('UPDATE run_tools SET result=? WHERE run_id=? AND call_id=?', (encoded(refreshed), rid, row[0]))

    @classmethod
    def _ready(cls, db, rid):
        if cls._required(db, rid):
            return False
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_waits'").fetchone():
            from .agent_tree import Tree
            if Tree.has_pending_wait(db, rid):
                return False
        for (result,) in db.execute('SELECT result FROM run_tools WHERE run_id=? AND result IS NOT NULL', (rid,)).fetchall():
            receipt = json.loads(result)
            aid = receipt.get('action_id') if isinstance(receipt, dict) else None
            if aid and db.execute("SELECT 1 FROM actions WHERE id=? AND status IN ('pending','running')", (aid,)).fetchone():
                return False
        return True

    def required_pending(self, rid):
        with self.store.lock:
            return self._required(self.store.db, rid)

    @staticmethod
    def pending_records(db, rid):
        """Host records holding a run: pending questions, pending broker actions, uncertain receipts.

        Read-only; the caller owns the transaction. Question titles are agent-authored text.
        """
        questions = [(ident, json.loads(items)[0]['title']) for ident, items in db.execute(
            "SELECT id,items FROM run_questions WHERE run_id=? AND status='pending' ORDER BY created,id", (rid,))]
        owned = {r[0] for r in db.execute('SELECT action_id FROM run_action_owners WHERE run_id=?', (rid,))}
        for (result,) in db.execute('SELECT result FROM run_tools WHERE run_id=? AND result IS NOT NULL', (rid,)).fetchall():
            receipt = json.loads(result)
            if isinstance(receipt, dict) and isinstance(receipt.get('action_id'), str):
                owned.add(receipt['action_id'])
        actions = [r[0] for r in db.execute("SELECT id FROM actions WHERE id IN (%s) AND status IN ('pending','running') "
                                            'ORDER BY created,id' % ','.join('?' * len(owned)), sorted(owned))] if owned else []
        uncertain = [r[0] for r in db.execute("SELECT call_id FROM run_tools WHERE run_id=? AND state='uncertain' ORDER BY rowid", (rid,))]
        return {'questions': questions, 'actions': actions, 'uncertain': uncertain}

    def prepare_settlement(self, db, rid, state):
        if state == 'completed':
            pending = db.execute("SELECT 1 FROM run_inbox WHERE run_id=? AND status='pending' LIMIT 1", (rid,)).fetchone()
            questions = db.execute("SELECT 1 FROM run_questions WHERE run_id=? AND status='pending' LIMIT 1", (rid,)).fetchone()
            if pending or questions:
                state = 'waiting_input'
                if pending and self._ready(db, rid):
                    self._queue(db, rid, 'pending_input_at_settlement')
        if state in {'stopped', 'paused', 'failed', 'budget_exhausted', 'needs_reconciliation', 'verification_required'}:
            db.execute("UPDATE run_continuations SET state='cancelled',updated=? WHERE run_id=? AND state='queued'", (time.time(), rid))
        elif state == 'waiting_input' and self._ready(db, rid) and db.execute("SELECT 1 FROM run_inbox WHERE run_id=? AND status='pending' LIMIT 1", (rid,)).fetchone():
            self._queue(db, rid, 'input_arrived_before_wait')
        return state

    def deliver(self, rid, snapshot, steps, elapsed):
        with self.store.lock, self.store.db as db:
            rows = db.execute("SELECT id,kind,origin,payload FROM run_inbox WHERE run_id=? AND status='pending' ORDER BY sequence LIMIT 20", (rid,)).fetchall()
            if not rows:
                return snapshot
            updated = copy.deepcopy(snapshot)
            for ident, kind, origin, payload in rows:
                updated['messages'].append({'role': 'user', 'content': 'HOST INPUT (' + origin + '-origin; never a permission change):\n' + encoded({'input_id': ident, 'kind': kind, 'payload': json.loads(payload)}), '_pebble_inbox': ident})
                db.execute("UPDATE run_inbox SET status='delivered',delivered_step=? WHERE id=?", (steps, ident))
            db.execute('UPDATE runs SET checkpoint=?,steps=?,elapsed=?,updated=? WHERE id=?', (encoded(updated), steps, elapsed, time.time(), rid))
            self.journal.event(db, rid, 'inputs_delivered', {'input_ids': [r[0] for r in rows], 'step': steps})
        return updated

    def action_resolution(self, cid, aid, receipt):
        with self.store.lock, self.store.db as db:
            rows = db.execute('SELECT t.run_id,t.call_id,t.result,r.state FROM run_tools t JOIN runs r ON r.id=t.run_id WHERE r.chat_id=?', (cid,)).fetchall()
            matches = [r for r in rows if r[2] and json.loads(r[2]).get('action_id') == aid]
            if len({r[0] for r in matches}) > 1:
                raise ValueError('A broker action cannot resolve receipts in multiple runs.')
            owner = matches[0] if matches else None
            if not owner or owner[3] not in LIVE | RESUMABLE:
                return None
            rid, call_id, _, state = owner
            for same_rid, same_call, previous, _ in matches:
                # Nested effects own admission; macro receipts mirror their
                # settled result while preserving their own correlation fields.
                value = {**json.loads(previous), **receipt, 'receipt_id': same_call}
                db.execute("UPDATE run_tools SET result=? WHERE run_id=? AND call_id=? AND state='completed'",
                           (encoded(value), same_rid, same_call))
            self._input(db, rid, 'action_resolution', 'host', receipt, 'action-' + aid)
            self.journal.event(db, rid, 'action_resolved', {'action_id': aid, 'status': receipt['status']})
            if state in {'waiting_approval', 'waiting_input'} and self._ready(db, rid):
                self._queue(db, rid, 'action_resolved')
        return rid

    def claim(self):
        with self.store.lock, self.store.db as db:
            row = db.execute("SELECT c.run_id,r.chat_id FROM run_continuations c JOIN runs r ON r.id=c.run_id WHERE c.state='queued' ORDER BY c.updated LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE run_continuations SET state='dispatching',updated=? WHERE run_id=?", (time.time(), row[0]))
            return {'run_id': row[0], 'chat_id': row[1]}

    def dispatched(self, rid, error=None):
        with self.store.lock, self.store.db as db:
            # The worker may already have enqueued a newer continuation.
            db.execute("UPDATE run_continuations SET state=?,error=?,updated=? WHERE run_id=? AND state='dispatching'", ('blocked' if error else 'dispatched', error, time.time(), rid))
            if error:
                self.journal.event(db, rid, 'continuation_blocked', {'error': error})
                # Surface the reason on the run itself: a parked run with nothing left to
                # review would otherwise look like it is still waiting. The next state
                # transition replaces it.
                db.execute("UPDATE runs SET error=?,updated=? WHERE id=?", (str(error)[:500], time.time(), rid))

    def continuation(self, rid):
        rows = self.store.query('SELECT * FROM run_continuations WHERE run_id=?', (rid,))
        return rows[0] if rows else None

    def plan(self, rid):
        rows = self.store.query('SELECT * FROM run_plans WHERE run_id=?', (rid,))
        if not rows:
            return None
        return {**rows[0], 'steps': json.loads(rows[0]['steps'])}

    def update_plan(self, rid, steps):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
            raise ValueError('Plans need one to twenty steps.')
        ids, active, normalized = set(), 0, []
        receipts = {t['call_id']: t for t in self.journal.tools(rid)}
        for step in steps:
            if not isinstance(step, dict) or set(step) - {'id', 'title', 'status', 'evidence_ids'}:
                raise ValueError('Plan steps accept id, title, status and evidence_ids.')
            ident, status = step.get('id'), step.get('status')
            if not isinstance(ident, str) or not ID.fullmatch(ident) or ident in ids or status not in {'pending', 'in_progress', 'blocked', 'completed'}:
                raise ValueError('Plan steps need unique IDs and a supported status.')
            ids.add(ident)
            active += status == 'in_progress'
            evidence = step.get('evidence_ids', [])
            if not isinstance(evidence, list) or len(evidence) > 8 or any(not isinstance(e, str) or e not in receipts for e in evidence):
                raise ValueError('Evidence must reference this run\'s tool receipts.')
            if status == 'completed':
                if not evidence or any(not self.resolved_evidence(receipts[e]) for e in evidence):
                    raise ValueError('Completed steps need committed, resolved evidence.')
            normalized.append({'id': ident, 'title': bounded_text(step.get('title'), 240), 'status': status, 'evidence_ids': evidence})
        if active > 1:
            raise ValueError('Keep one active step in this serial run.')
        with self.store.lock, self.store.db as db:
            state = db.execute('SELECT state FROM runs WHERE id=?', (rid,)).fetchone()
            if not state or state[0] != 'running':
                raise ValueError('Only a running agent can update its plan.')
            old = db.execute('SELECT revision FROM run_plans WHERE run_id=?', (rid,)).fetchone()
            version = old[0] + 1 if old else 1
            db.execute('INSERT INTO run_plans VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET revision=excluded.revision,steps=excluded.steps,updated=excluded.updated', (rid, version, encoded(normalized), time.time()))
            self.journal.event(db, rid, 'plan_updated', {'revision': version, 'steps': normalized})
        return self.plan(rid)

    @staticmethod
    def resolved_evidence(receipt):
        result = receipt.get('result')
        if receipt['state'] != 'completed' or not isinstance(result, dict) or result.get('error') or result.get('status') in {'awaiting_user_approval', 'awaiting_user_input', 'rejected', 'cancelled', 'not_executed', 'user_reported', 'unsupported', 'conflict', 'pending', 'disabled', 'forgotten', 'expired', 'evidence_too_large'}:
            return False
        outcome = result.get('result', result)
        return not isinstance(outcome, dict) or not outcome.get('error') and outcome.get('exit_code', 0) == 0

    def forget(self, db, cid):
        for table in ('run_inbox', 'run_questions', 'run_continuations', 'run_plans'):
            db.execute(f'DELETE FROM {table} WHERE run_id IN (SELECT id FROM runs WHERE chat_id=?)', (cid,))
        # Goals are chat-scoped operational records; deleting the conversation deletes them too.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'").fetchone():
            from .goals import forget
            forget(db, cid)
