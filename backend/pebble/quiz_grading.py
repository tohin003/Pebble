"""Host-side quiz grading (quiz v2): the answer key stays with the host.

The model's quiz input is unchanged. Publishing splits it: the stored, public revision
holds only {duration_minutes, questions: [{id, question, choices}]}, and each answer and
explanation go to quiz_answer_keys in the same transaction. Nothing that returns an
artifact (artifact_get, artifact_history, chat state, the tool result or its receipt,
exports without the include-answers choice) ever reads that table.

Attempts are human task data. Only native routes start, save, submit, read or share
them, every call names its chat, and a shared result reaches the owning run as one
origin=human `artifact_result` input. Effects are never replayed: a repeated submit or
share returns the stored outcome and writes nothing.

Tables (schema epoch 9) are created by the host's migration DDL, not lazily here.
"""
import hashlib
import json
import time
import uuid

from .interaction import ID
from .runtime import LIVE, RESUMABLE

DDL = '''
CREATE TABLE IF NOT EXISTS quiz_answer_keys(
  artifact_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1), chat_id TEXT NOT NULL,
  key_json TEXT NOT NULL, key_hash TEXT NOT NULL, public_hash TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY(artifact_id,revision));
CREATE INDEX IF NOT EXISTS quiz_answer_keys_chat ON quiz_answer_keys(chat_id);
CREATE TABLE IF NOT EXISTS quiz_attempts(
  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, artifact_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1),
  origin TEXT NOT NULL CHECK(origin='human'),
  status TEXT NOT NULL CHECK(status IN ('open','submitted','expired','superseded')),
  clock_id TEXT NOT NULL, started REAL NOT NULL, started_mono REAL NOT NULL, limit_seconds INTEGER NOT NULL,
  elapsed_mark REAL NOT NULL, draft TEXT NOT NULL, saved REAL, answers TEXT, result TEXT, reason TEXT,
  submitted REAL, share_input_id TEXT, shared REAL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS quiz_attempts_view ON quiz_attempts(artifact_id,revision,status);
CREATE INDEX IF NOT EXISTS quiz_attempts_chat ON quiz_attempts(chat_id,started);
CREATE UNIQUE INDEX IF NOT EXISTS quiz_attempts_one_open ON quiz_attempts(artifact_id,revision) WHERE status='open';
'''
TABLES = ('quiz_answer_keys', 'quiz_attempts')
GRACE_SECONDS = 2.0  # a submission sent as the timer ends still counts
MAX_ATTEMPTS = 20  # per artifact revision
_INSTANCE = 'process:' + uuid.uuid4().hex


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_encode(value).encode()).hexdigest()


def available(db):
    """Whether this database has the epoch-9 quiz tables."""
    return len(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)", TABLES).fetchall()) == 2


def split(data):
    """Validated quiz input -> (public revision data, host-only key). Question IDs are q1..qN."""
    public, key = {'duration_minutes': data['duration_minutes'], 'questions': []}, {'questions': []}
    for index, question in enumerate(data['questions'], 1):
        ident = 'q' + str(index)
        public['questions'].append({'id': ident, 'question': question['question'], 'choices': list(question['choices'])})
        key['questions'].append({'id': ident, 'answer': question['answer'], 'explanation': question['explanation']})
    return public, key


def is_public(data):
    """A host-graded quiz revision as stored: question objects are exactly {id, question, choices}."""
    return (isinstance(data, dict) and set(data) == {'duration_minutes', 'questions'} and isinstance(data['questions'], list)
            and bool(data['questions']) and all(isinstance(q, dict) and set(q) == {'id', 'question', 'choices'}
                                                for q in data['questions']))


def store_key_in(db, artifact_id, revision, chat_id, public, key):
    """Caller owns the publish transaction: the key and its public revision commit together."""
    if not available(db):
        raise ValueError('Host-graded quizzes need the current Pebble data version. Nothing was saved.')
    if not is_public(public):
        raise ValueError('A host-graded quiz stores public questions only.')
    db.execute('INSERT INTO quiz_answer_keys VALUES(?,?,?,?,?,?,?)', (artifact_id, revision, chat_id, _encode(key),
               _digest(key), _digest(public), time.time()))


def export_key(db, chat_id, artifact_id, revision, public):
    """The key for an export of this chat's revision; None when absent or failing its integrity check."""
    if not available(db):
        return None
    row = db.execute('SELECT key_json,key_hash,public_hash FROM quiz_answer_keys WHERE artifact_id=? AND revision=? AND chat_id=?',
                     (artifact_id, revision, chat_id)).fetchone()
    key = json.loads(row[0]) if row else None
    return key if row and _digest(key) == row[1] and _digest(public) == row[2] else None


def grade(public, key, answers):
    """Score answers {question_id: choice index} against the key; a missing answer is wrong."""
    expected = {item['id']: item for item in key['questions']}
    per_question, score = [], 0
    for question in public['questions']:
        chosen, item = answers.get(question['id']), expected[question['id']]
        correct = chosen is not None and chosen == item['answer']
        score += correct
        per_question.append({'id': question['id'], 'chosen': chosen, 'correct': correct,
                             'answer': item['answer'], 'explanation': item['explanation']})
    return {'score': score, 'total': len(public['questions']), 'per_question': per_question}


_PUBLIC_ARGUMENTS = ('title', 'kind', 'id', 'revision', 'evidence_ids')


def _public_text(value):
    return value if isinstance(value, str) else '[withheld]'


def redact_quiz_arguments(name, arguments):
    """A UIRender/UIUpdate quiz call with only its public fields, for window-readable records.

    An allowlist (Phase 21, F21-Q1): the title, kind, view id, revision and evidence IDs, and from the data only
    duration_minutes and each question's question and choice texts. A refused call can carry its key anywhere (a
    renamed field, an answers list, questions outside data, a flagged choice), so nothing else is copied. A valid
    call reads exactly as before.
    """
    if name not in ('UIRender', 'UIUpdate') or not isinstance(arguments, dict) or arguments.get('kind') != 'quiz':
        return arguments
    try:
        data = json.loads(arguments['data']) if isinstance(arguments.get('data'), str) else arguments.get('data')
        minutes = data['duration_minutes']
        data = {'duration_minutes': minutes if type(minutes) in (int, float) else None,
                'questions': [{'question': _public_text(q.get('question')),
                               'choices': [_public_text(c) for c in q['choices']]
                               if isinstance(q.get('choices'), list) else '[withheld]'}
                              for q in data['questions']]}
        # Encoded inside the try: a refused call's NaN or Infinity (json.loads accepts both) withholds the data
        # here rather than raising, which left redact_response's copy of the call unredacted.
        data = _encode(data)
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        data = '[quiz data withheld: answer key]'
    return {**{k: arguments[k] for k in _PUBLIC_ARGUMENTS if k in arguments}, 'data': data}


def redact_response(response):
    """A committed assistant message whose quiz tool calls are redacted the same way."""
    if not isinstance(response, dict) or not isinstance(response.get('tool_calls'), list):
        return response
    calls = []
    for call in response['tool_calls']:
        try:
            function = call['function']
            arguments = json.loads(function['arguments'])
            redacted = redact_quiz_arguments(function['name'], arguments)
            if redacted is not arguments:
                call = {**call, 'function': {**function, 'arguments': _encode(redacted)}}
        except (TypeError, ValueError, KeyError, RecursionError):
            pass
        calls.append(call)
    return {**response, 'tool_calls': calls}


def redact_archive(value, _depth=0):
    """A window-readable run record (an event payload or a context archive) with every quiz call redacted.

    Assistant messages with tool_calls and {name, arguments} receipts are redacted at any depth; the stored
    record is unchanged (recovery reads it), only this copy for a window is.
    """
    if _depth > 64:
        return value
    if isinstance(value, dict):
        if isinstance(value.get('tool_calls'), list):
            value = redact_response(value)
        if isinstance(value.get('name'), str) and isinstance(value.get('arguments'), dict):
            value = {**value, 'arguments': redact_quiz_arguments(value['name'], value['arguments'])}
        return {key: redact_archive(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_archive(item, _depth + 1) for item in value]
    return value


def supersede_in(db, artifact_id, revision, reason):
    """Close this revision's open attempts, ungraded, when the view changes or stops."""
    if available(db):
        db.execute("UPDATE quiz_attempts SET status='superseded',reason=?,updated=? WHERE artifact_id=? AND revision=? "
                   "AND status='open'", (reason, time.time(), artifact_id, revision))


def summary_in(db, artifact_id, revision):
    """Attempt summaries for a decorated row: never answers, per-question results or the key."""
    if not available(db):
        return []
    rows = db.execute('SELECT id,status,started,limit_seconds,result,shared FROM quiz_attempts WHERE artifact_id=? '
                      'AND revision=? ORDER BY started DESC LIMIT 5', (artifact_id, revision)).fetchall()
    items = []
    for ident, status, started, limit, result, shared in rows:
        scored = json.loads(result) if result else None
        items.append({'attempt_id': ident, 'status': status, 'started': started, 'deadline': started + limit,
                      'score': scored['score'] if scored else None, 'total': scored['total'] if scored else None,
                      'shared': shared is not None})
    return items


def forget_in(db, chat_id):
    if available(db):
        db.execute('DELETE FROM quiz_attempts WHERE chat_id=?', (chat_id,))
        db.execute('DELETE FROM quiz_answer_keys WHERE chat_id=?', (chat_id,))


class QuizAttempts:
    """Native quiz routes. Every method names its chat; the artifact registry decides ownership."""

    _COLUMNS = ('id', 'chat_id', 'artifact_id', 'revision', 'origin', 'status', 'clock_id', 'started', 'started_mono',
                'limit_seconds', 'elapsed_mark', 'draft', 'saved', 'answers', 'result', 'reason', 'submitted',
                'share_input_id', 'shared', 'updated')

    def __init__(self, app, *, wall=None, clock=None, boot=None):
        self.app, self.store, self.artifacts = app, app.store, app.artifacts
        # Looked up at each call, so an injected or adjusted clock is always the one in force.
        self.wall = wall or (lambda: time.time())
        self.clock = clock or (lambda: time.monotonic())
        if boot is None:
            boot = getattr(getattr(app, 'time_ledger', None), 'boot_uuid', None)
        # Monotonic readings compare only within one boot (or, when the boot is unknown, one process).
        self.clock_id = 'boot:' + boot if boot else _INSTANCE

    # Shared checks

    def _view(self, db, cid, aid):
        # _read verifies the registry: the artifact belongs to this chat or nothing is returned.
        current = self.artifacts._read(db, cid, aid)[-1]
        data = json.loads(current['data'])
        if current['kind'] != 'quiz' or not is_public(data):
            raise ValueError('This view is not a host-graded quiz.')
        return current, data

    def _key(self, db, aid, revision, public):
        row = db.execute('SELECT key_json,key_hash,public_hash FROM quiz_answer_keys WHERE artifact_id=? AND revision=?',
                         (aid, revision)).fetchone()
        key = json.loads(row[0]) if row else None
        if not row or _digest(key) != row[1] or _digest(public) != row[2]:
            raise ValueError('This quiz\'s answer key failed its integrity check. It cannot be graded.')
        return key

    def _row(self, db, cid, attempt_id):
        if type(attempt_id) is not str or not ID.fullmatch(attempt_id):
            raise ValueError('Quiz attempt not found in this conversation.')
        row = db.execute('SELECT ' + ','.join(self._COLUMNS) + ' FROM quiz_attempts WHERE id=? AND chat_id=?',
                         (attempt_id, cid)).fetchone()
        if not row:
            raise ValueError('Quiz attempt not found in this conversation.')
        return dict(zip(self._COLUMNS, row))

    def _elapsed(self, row):
        """Seconds used. Within one clock domain a backward wall-clock jump cannot shorten it."""
        elapsed = self.wall() - row['started']
        if row['clock_id'] == self.clock_id:
            elapsed = max(elapsed, self.clock() - row['started_mono'])
        return max(elapsed, row['elapsed_mark'], 0.0)

    @staticmethod
    def _answers(public, value):
        ids = {q['id']: len(q['choices']) for q in public['questions']}
        if type(value) is not dict or set(value) - set(ids):
            raise ValueError('Answer only this quiz\'s questions.')
        for ident, choice in value.items():
            if choice is not None and (type(choice) is not int or not 0 <= choice < ids[ident]):
                raise ValueError('Choose one of the offered answers.')
        return {q['id']: value[q['id']] for q in public['questions'] if value.get(q['id']) is not None}

    def _observe(self, db, row, now):
        """Record time used on an open attempt; past its deadline it expires, ungraded, with its draft kept."""
        if row['status'] != 'open':
            return None
        elapsed = self._elapsed(row)
        if elapsed >= row['limit_seconds'] + GRACE_SECONDS:
            db.execute("UPDATE quiz_attempts SET status='expired',reason='time',elapsed_mark=max(elapsed_mark,?),updated=? "
                       "WHERE id=? AND status='open'", (elapsed, now, row['id']))
            row.update(status='expired', reason='time', elapsed_mark=max(row['elapsed_mark'], elapsed))
        elif elapsed > row['elapsed_mark']:
            db.execute('UPDATE quiz_attempts SET elapsed_mark=? WHERE id=? AND elapsed_mark<?', (elapsed, row['id'], elapsed))
            row['elapsed_mark'] = elapsed
        return elapsed

    def _drift(self, db, cid, row):
        """The view's current revision; an open attempt on a replaced or stopped revision is superseded."""
        current, public = self._view(db, cid, row['artifact_id'])
        if row['status'] == 'open' and (current['revision'] != row['revision'] or current['status'] != 'ready'):
            reason = 'updated' if current['revision'] != row['revision'] else 'stopped'
            supersede_in(db, row['artifact_id'], row['revision'], reason)
            row.update(status='superseded', reason=reason)
        return current, public

    def _public_attempt(self, row, elapsed=None):
        item = {'attempt_id': row['id'], 'artifact_id': row['artifact_id'], 'revision': row['revision'], 'origin': 'human',
                'status': row['status'], 'started': row['started'], 'deadline': row['started'] + row['limit_seconds'],
                'answers': json.loads(row['answers'] or row['draft']), 'result': None, 'shared': row['shared'] is not None}
        if row['status'] == 'open' and elapsed is not None:
            item['seconds_left'] = max(0.0, round(row['limit_seconds'] - elapsed, 1))
        elif row['status'] == 'submitted':
            item['result'] = json.loads(row['result'])
        elif row['status'] == 'expired':
            item['message'] = 'Time ran out before this attempt was submitted, so it was not graded. Your answers were kept.'
        elif row['status'] == 'superseded':
            item['message'] = 'This quiz changed or stopped during the attempt, so it was not graded. Your answers were kept.'
        return item

    # Native routes

    def attempt(self, chat_id, id, revision):
        """The open attempt on this revision, else its latest attempt, else None. Reads only time used."""
        with self.artifacts._transaction() as db:
            current, public = self._view(db, chat_id, id)
            if type(revision) is not int or not 1 <= revision <= current['revision']:
                raise ValueError('Quiz revision not found.')
            ident = db.execute("SELECT id FROM quiz_attempts WHERE chat_id=? AND artifact_id=? AND revision=? "
                               "ORDER BY status='open' DESC,started DESC LIMIT 1", (chat_id, id, revision)).fetchone()
            if not ident:
                return None
            row = self._row(db, chat_id, ident[0])
            self._drift(db, chat_id, row)
            return self._public_attempt(row, self._observe(db, row, self.wall()))

    def start(self, chat_id, id, revision):
        """Begin, or resume, the one open attempt on the current revision."""
        now = self.wall()
        with self.artifacts._transaction() as db:
            current, public = self._view(db, chat_id, id)
            if type(revision) is not int or current['revision'] != revision:
                raise ValueError(f'This view changed to revision {current["revision"]}. Open the new revision to start.')
            if current['status'] != 'ready':
                raise ValueError('This quiz is no longer accepting attempts.')
            self._key(db, id, revision, public)
            open_row = db.execute("SELECT id FROM quiz_attempts WHERE artifact_id=? AND revision=? AND status='open'",
                                  (id, revision)).fetchone()
            if open_row:
                row = self._row(db, chat_id, open_row[0])
                elapsed = self._observe(db, row, now)
                if row['status'] == 'open':
                    return self._public_attempt(row, elapsed)
            if db.execute('SELECT count(*) FROM quiz_attempts WHERE artifact_id=? AND revision=?', (id, revision)).fetchone()[0] >= MAX_ATTEMPTS:
                raise ValueError('This quiz has reached its attempt limit.')
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO quiz_attempts(' + ','.join(self._COLUMNS) + ') VALUES(' + ','.join('?' * len(self._COLUMNS)) + ')',
                       (ident, chat_id, id, revision, 'human', 'open', self.clock_id, now, self.clock(),
                        public['duration_minutes'] * 60, 0.0, '{}', None, None, None, None, None, None, None, now))
            return self._public_attempt(self._row(db, chat_id, ident), 0.0)

    def _open(self, db, chat_id, attempt_id, revision, answers):
        row = self._row(db, chat_id, attempt_id)
        current, public = self._drift(db, chat_id, row)
        if row['status'] == 'superseded' or type(revision) is not int or revision != row['revision']:
            raise ValueError(f'This view changed to revision {current["revision"]}. Your answers were kept and were not used.')
        return row, public, self._answers(public, answers)

    def save(self, chat_id, attempt_id, revision, answers):
        """Store a draft in place of browser storage. A closed attempt refuses it."""
        now = self.wall()
        with self.artifacts._transaction() as db:
            row, public, answers = self._open(db, chat_id, attempt_id, revision, answers)
            if row['status'] != 'open':
                raise ValueError('This attempt was already submitted.' if row['status'] == 'submitted'
                                 else 'Time ran out for this attempt; your earlier answers were kept.')
            elapsed = self._observe(db, row, now)
            if row['status'] == 'open':
                draft = _encode(answers)
                db.execute("UPDATE quiz_attempts SET draft=?,saved=?,updated=? WHERE id=? AND status='open'", (draft, now, now, row['id']))
                row['draft'] = draft
            return self._public_attempt(row, elapsed)

    def submit(self, chat_id, attempt_id, revision, answers):
        """Grade once, on the host. A repeat returns the stored outcome and writes nothing."""
        now = self.wall()
        with self.artifacts._transaction() as db:
            row, public, answers = self._open(db, chat_id, attempt_id, revision, answers)
            if row['status'] == 'open':
                key = self._key(db, row['artifact_id'], row['revision'], public)
                self._observe(db, row, now)
                if row['status'] == 'open':
                    result = grade(public, key, answers)
                    db.execute("UPDATE quiz_attempts SET status='submitted',answers=?,result=?,submitted=?,updated=? "
                               "WHERE id=? AND status='open'", (_encode(answers), _encode(result), now, now, row['id']))
                    row.update(status='submitted', answers=_encode(answers), result=_encode(result), submitted=now)
                return self._public_attempt(row)
            if row['status'] == 'submitted' and json.loads(row['answers']) != answers:
                raise ValueError('This attempt was already graded. Start a new attempt to try again.')
            return self._public_attempt(row)

    def share(self, chat_id, attempt_id, surface=None):
        """The person's explicit Share: one human-origin artifact_result input, or host text to compose.

        `surface` is written by the native shell from the calling window's label, never taken
        from the webview. Only exactly 'main' writes the input. Any other surface, or an owning
        run that can no longer take input, writes nothing and gets text the person may send.
        """
        with self.artifacts._transaction() as db:
            row = self._row(db, chat_id, attempt_id)
            current, _ = self._drift(db, chat_id, row)
            if row['status'] != 'submitted':
                raise ValueError('Only a submitted, graded attempt can be shared.')
            owner = current['owner_run_id']  # Only the owning run may update a view, so every revision shares it.
            if row['share_input_id']:
                return {'status': 'shared', 'shared': True, 'input_id': row['share_input_id'], 'run_id': owner, 'origin': 'human'}
            result = json.loads(row['result'])
            payload = {'artifact_id': row['artifact_id'], 'revision': row['revision'], 'attempt_id': row['id'],
                       'score': result['score'], 'total': result['total'],
                       'per_question': [{'id': q['id'], 'chosen': q['chosen'], 'correct': q['correct']} for q in result['per_question']]}
            state = db.execute('SELECT state FROM runs WHERE id=? AND chat_id=?', (owner, chat_id)).fetchone() if owner else None
            if surface != 'main' or not state or state[0] not in LIVE | RESUMABLE:
                reason = ('Share from the main window.' if surface != 'main' else 'The run that made this quiz has finished.')
                return {'status': 'compose', 'shared': False, 'reason': reason, 'text': self._compose(current['title'], payload)}
            delivered = self.app.interactions.artifact_result_in(db, chat_id, owner, payload, 'quiz-' + row['id'])
            now = self.wall()
            db.execute('UPDATE quiz_attempts SET share_input_id=?,shared=?,updated=? WHERE id=?', (delivered['input_id'], now, now, row['id']))
            return {**delivered, 'status': 'shared', 'shared': True}

    @staticmethod
    def _compose(title, payload):
        lines = [f'My result for the quiz "{title}" (revision {payload["revision"]}): {payload["score"]} of {payload["total"]} correct.']
        for number, item in enumerate(payload['per_question'], 1):
            picked = 'no answer' if item['chosen'] is None else 'chose ' + chr(65 + item['chosen'])
            lines.append(f'Q{number}: {"correct" if item["correct"] else "incorrect"} ({picked})')
        return '\n'.join(lines)[:2000]
