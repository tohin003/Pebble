"""Phase 19 B2: host-graded quizzes (p19_quiz_*, p19_update_drift).

The model's quiz input is unchanged; the stored view holds public questions only and the
answer key sits in quiz_answer_keys, written in the same transaction. Attempts are native
task data bound to their chat, revision and deadline; a result reaches the owning run only
through the person's Share from the main window. The epoch-9 tables come from the DDL the
parent's migration adopts (quiz_grading.DDL), applied by this fixture.
"""
import copy
import json
import uuid

import pytest

from pebble import quiz_grading
from pebble.app import App
from pebble.artifact_inputs import model_result
from pebble.artifacts import validate
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError
from pebble.quiz_grading import QuizAttempts
from pebble.telemetry import run_record

CANARY = 'p19-quiz-canary-' + 'f00d' * 4


def quiz():
    return {'duration_minutes': 10, 'questions': [
        {'question': 'Which gas do plants absorb?', 'choices': ['Oxygen', 'Carbon dioxide'], 'answer': 1,
         'explanation': 'Plants take in carbon dioxide. ' + CANARY},
        {'question': 'Which planet is largest?', 'choices': ['Jupiter', 'Mars', 'Venus'], 'answer': 0,
         'explanation': 'Jupiter is the largest planet. ' + CANARY}]}


PUBLIC = {'duration_minutes': 10, 'questions': [
    {'id': 'q1', 'question': 'Which gas do plants absorb?', 'choices': ['Oxygen', 'Carbon dioxide']},
    {'id': 'q2', 'question': 'Which planet is largest?', 'choices': ['Jupiter', 'Mars', 'Venus']}]}


@pytest.fixture
def app(tmp_path):
    events = []
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(copy.deepcopy(args)))
    value.model.complete = lambda *args, **kwargs: pytest.fail('No model inference in these tests.')
    value.events = events
    groups = value.store.setting('permissions', DEFAULT)['tools']
    value.setting('permissions', {'mode': 'ask', 'tools': dict.fromkeys(groups, True)})
    with value.store.lock:
        value.store.db.executescript(quiz_grading.DDL)  # the parent's epoch-9 migration DDL
    yield value
    value.active = value.active_run = None
    value.close()


class Clock:
    def __init__(self):
        self.wall, self.mono = 1_800_000_000.0, 5_000.0


@pytest.fixture
def clock():
    return Clock()


def grader(app, clock, boot='boot-a'):
    return QuizAttempts(app, wall=lambda: clock.wall, clock=lambda: clock.mono, boot=boot)


class Owner:
    def __init__(self, app, chat=None):
        self.app, self.chat = app, chat or app.new_chat()
        self.cid = self.chat['id']
        self.rid = app.runs.create(self.cid, 'Teach photosynthesis.', app.runtime_config(self.chat, False))
        app.runs.transition(self.rid, 'running')
        self.calls = 0

    def __enter__(self):
        self.previous = self.app.active, self.app.active_run
        self.app.active, self.app.active_run = self.cid, self.rid
        return self

    def __exit__(self, *unused):
        self.app.active, self.app.active_run = self.previous

    def tool(self, name, **args):
        self.calls += 1
        if 'data' in args and not isinstance(args['data'], str):
            args['data'] = json.dumps(args['data'])
        return self.app.execute(name, args, None, self.cid, call_id=f'{self.calls}:{name.lower()}')

    def render(self, kind='quiz', data=None, title='Science check'):
        return self.tool('UIRender', title=title, kind=kind, data=quiz() if data is None else data)


def published(app):
    with Owner(app) as owner:
        row = owner.render()
    return owner, row


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def snapshot(app):
    with app.store.lock:
        names = [n for (n,) in app.store.db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                    "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'")]
        return {n: sorted(map(repr, app.store.db.execute(f'SELECT * FROM "{n}"').fetchall()))
                for n in names if not n.startswith('memory_search')}


def tables_holding(app, needle):
    return {name for name, rows in snapshot(app).items() if any(needle in row for row in rows)}


def inbox(app):
    return app.store.query("SELECT * FROM run_inbox WHERE kind='artifact_result'")


# Publish: split, atomicity and where the key never goes -------------------------------------------

def test_p19_quiz_the_model_input_is_unchanged_and_only_public_questions_are_stored(app):
    assert validate('quiz', quiz()) == quiz()  # the model's v1 input, answers and explanations included
    public, key = quiz_grading.split(validate('quiz', quiz()))
    assert public == PUBLIC and quiz_grading.is_public(public) and not quiz_grading.is_public(quiz())
    assert key == {'questions': [{'id': 'q1', 'answer': 1, 'explanation': quiz()['questions'][0]['explanation']},
                                 {'id': 'q2', 'answer': 0, 'explanation': quiz()['questions'][1]['explanation']}]}
    with pytest.raises(ValueError):
        validate('quiz', public)  # a keyless quiz is not a model input
    # Merge (i19): with parent patch P6 the UIRender tool result is the model view (model_result plus the
    # receipt_id execute adds); the stored row is what a window reads through artifact_get.
    owner, result = published(app)
    row = app.artifact_get(owner.cid, result['id'])
    assert row['kind'] == 'quiz' and row['data'] == PUBLIC and row['revision'] == 1
    stored = app.store.query('SELECT * FROM quiz_answer_keys')
    assert len(stored) == 1 and stored[0]['artifact_id'] == row['id'] and stored[0]['revision'] == 1
    assert stored[0]['chat_id'] == owner.cid and json.loads(stored[0]['key_json']) == key
    assert stored[0]['key_hash'] == quiz_grading._digest(key) and stored[0]['public_hash'] == quiz_grading._digest(PUBLIC)
    shown = model_result(row)
    assert set(shown) == {'id', 'revision', 'kind', 'title', 'status', 'evidence_ids', 'data_hash', 'record_hash',
                          'grading', 'duration_minutes', 'question_ids', 'message'}
    assert (shown['grading'], shown['duration_minutes'], shown['question_ids']) == ('host', 10, ['q1', 'q2'])
    assert {k: v for k, v in result.items() if k != 'receipt_id'} == shown and 'data' not in result
    assert CANARY not in compact(shown) and '"answer"' not in compact(row) and '"answer"' not in compact(result)


def test_p19_quiz_key_never_reaches_views_state_events_receipts_memory_handoff_telemetry_or_exports(app, clock):
    owner, row = published(app)
    cid, aid = owner.cid, row['id']
    windows = [app.artifact_get(cid, aid), app.dispatch('artifact_history', {'chat_id': cid, 'id': aid}),
               app.artifacts.list(cid), app.chat(cid)]
    text = compact(windows)
    assert CANARY not in text and '"answer"' not in text and '"explanation"' not in text
    assert app.artifact_get(cid, aid)['grading'] == 'host' and app.artifact_get(cid, aid)['attempts'] == []
    assert CANARY not in compact(app.events)
    # The receipt the model sees holds the public view only; run_tools.arguments keeps the model's own call.
    tools = app.runs.tools(owner.rid)
    assert CANARY not in compact([t['result'] for t in tools]) and CANARY in compact([t['arguments'] for t in tools])
    assert tables_holding(app, CANARY) == {'run_tools', 'quiz_answer_keys'}
    assert CANARY not in compact(app.run_handoff(cid, owner.rid))
    assert CANARY not in compact(run_record(app, owner.rid))
    # The window-readable copy of the call can be redacted (parent patch for run_get and run_events).
    redacted = [quiz_grading.redact_quiz_arguments(t['name'], t['arguments']) for t in tools]
    assert CANARY not in compact(redacted) and json.loads(redacted[0]['data']) == {
        'duration_minutes': 10, 'questions': [{k: q[k] for k in ('question', 'choices')} for q in quiz()['questions']]}
    # An open attempt never carries a result.
    attempts = grader(app, clock)
    opened = attempts.start(cid, aid, 1)
    assert CANARY not in compact([opened, attempts.attempt(cid, aid, 1)]) and opened['result'] is None
    # Exports leave the key out unless the person chooses to include it.
    options = {'include_answers': False, 'paper': 'a4'}
    hidden = app.exports._resolve(cid, {'kind': 'artifact', 'id': aid}, options)
    assert CANARY not in compact(hidden['blocks']) and 'Correct answer' not in compact(hidden['blocks'])
    assert any(CANARY in secret for secret in hidden['secrets'])  # the readback proves their absence
    shown = app.exports._resolve(cid, {'kind': 'artifact', 'id': aid}, {**options, 'include_answers': True})
    assert 'Correct answer: Carbon dioxide' in compact(shown['blocks']) and CANARY in compact(shown['blocks'])


def test_p19_quiz_publish_writes_the_view_and_its_key_together_or_nothing(app):
    with Owner(app) as owner:
        with app.store.lock:
            app.store.db.executescript('DROP TABLE quiz_attempts; DROP TABLE quiz_answer_keys;')
        with pytest.raises(PolicyError, match='current Pebble data version'):
            owner.render()
        assert not app.artifacts.list(owner.cid) and not app.store.query('SELECT * FROM artifact_revisions')
        with app.store.lock:
            app.store.db.executescript(quiz_grading.DDL)
        row = owner.render()
    assert app.artifact_get(owner.cid, row['id'])['data'] == PUBLIC and len(app.store.query('SELECT * FROM quiz_answer_keys')) == 1


def test_p19_quiz_a_failed_key_write_rolls_the_view_back(app):
    with app.store.lock:
        app.store.db.executescript("CREATE TRIGGER fail_key BEFORE INSERT ON quiz_answer_keys BEGIN SELECT RAISE(ABORT,'disk'); END;")
    with Owner(app) as owner:
        with pytest.raises(Exception):
            owner.render()
    for table in ('artifacts', 'artifact_revisions', 'artifact_heads', 'artifact_registry', 'quiz_answer_keys'):
        assert not app.store.query(f'SELECT * FROM {table}'), table


# Attempts ----------------------------------------------------------------------------------------

def test_p19_quiz_attempts_start_save_read_and_submit_once_on_the_host(app, clock):
    owner, row = published(app)
    cid, aid = owner.cid, row['id']
    attempts = grader(app, clock)
    assert attempts.attempt(cid, aid, 1) is None
    opened = attempts.start(cid, aid, 1)
    assert (opened['status'], opened['revision'], opened['answers'], opened['result']) == ('open', 1, {}, None)
    assert opened['deadline'] == clock.wall + 600 and opened['seconds_left'] == 600.0 and opened['origin'] == 'human'
    assert attempts.start(cid, aid, 1)['attempt_id'] == opened['attempt_id']  # resumed, never a second open attempt
    clock.mono += 30
    clock.wall += 30
    saved = attempts.save(cid, opened['attempt_id'], 1, {'q1': 1, 'q2': None})
    assert saved['answers'] == {'q1': 1} and saved['seconds_left'] == 570.0
    assert attempts.attempt(cid, aid, 1)['answers'] == {'q1': 1}
    graded = attempts.submit(cid, opened['attempt_id'], 1, {'q1': 1, 'q2': 2})
    explanations = [q['explanation'] for q in quiz()['questions']]
    assert graded['status'] == 'submitted' and graded['answers'] == {'q1': 1, 'q2': 2}
    assert graded['result'] == {'score': 1, 'total': 2, 'per_question': [
        {'id': 'q1', 'chosen': 1, 'correct': True, 'answer': 1, 'explanation': explanations[0]},
        {'id': 'q2', 'chosen': 2, 'correct': False, 'answer': 0, 'explanation': explanations[1]}]}
    summary = app.artifact_get(cid, aid)['attempts']
    assert summary == [{'attempt_id': opened['attempt_id'], 'status': 'submitted', 'started': opened['started'],
                        'deadline': opened['deadline'], 'score': 1, 'total': 2, 'shared': False}]
    assert CANARY not in compact(app.artifact_get(cid, aid))
    before = snapshot(app)
    assert attempts.submit(cid, opened['attempt_id'], 1, {'q1': 1, 'q2': 2}) == graded  # idempotent replay
    with pytest.raises(ValueError, match='already graded'):
        attempts.submit(cid, opened['attempt_id'], 1, {'q1': 1, 'q2': 0})
    with pytest.raises(ValueError, match='already submitted'):
        attempts.save(cid, opened['attempt_id'], 1, {'q1': 0})
    assert snapshot(app) == before
    retake = attempts.start(cid, aid, 1)
    assert retake['attempt_id'] != opened['attempt_id'] and retake['status'] == 'open'
    with pytest.raises(ValueError, match='attempt limit'):
        for _ in range(quiz_grading.MAX_ATTEMPTS):
            attempts.submit(cid, attempts.start(cid, aid, 1)['attempt_id'], 1, {})
    assert len(app.store.query('SELECT * FROM quiz_attempts')) == quiz_grading.MAX_ATTEMPTS
    assert not inbox(app)


def test_p19_quiz_deadline_holds_against_backward_wall_clock_jumps_and_late_answers_are_not_graded(app, clock):
    owner, row = published(app)
    cid, aid = owner.cid, row['id']
    attempts = grader(app, clock)
    first = attempts.start(cid, aid, 1)['attempt_id']
    attempts.save(cid, first, 1, {'q1': 1})
    clock.mono += 300
    clock.wall -= 3600  # set back by an hour within this boot: the attempt gains no time
    assert attempts.attempt(cid, aid, 1)['seconds_left'] == 300.0
    clock.mono += 300 + quiz_grading.GRACE_SECONDS
    late = attempts.submit(cid, first, 1, {'q1': 1, 'q2': 0})
    assert late['status'] == 'expired' and late['result'] is None and late['answers'] == {'q1': 1}
    before = snapshot(app)
    assert attempts.submit(cid, first, 1, {'q1': 1, 'q2': 0}) == late
    with pytest.raises(ValueError, match='Time ran out'):
        attempts.save(cid, first, 1, {'q1': 0})
    with pytest.raises(ValueError, match='Only a submitted'):
        attempts.share(cid, first, 'main')
    assert snapshot(app) == before
    # A forward wall jump alone also ends it; a submission inside the grace window still counts.
    second = attempts.start(cid, aid, 1)['attempt_id']
    clock.wall += 600 + quiz_grading.GRACE_SECONDS
    assert attempts.attempt(cid, aid, 1)['status'] == 'expired'
    third = attempts.start(cid, aid, 1)['attempt_id']
    clock.mono += 601
    assert attempts.submit(cid, third, 1, {'q1': 1})['result']['score'] == 1
    # Across boots monotonic time is not comparable: the recorded time used is a floor.
    fourth = attempts.start(cid, aid, 1)['attempt_id']
    clock.mono += 100
    assert attempts.attempt(cid, aid, 1)['seconds_left'] == 500.0
    rebooted = grader(app, clock, boot='boot-b')
    clock.wall -= 3600
    assert rebooted.attempt(cid, aid, 1)['seconds_left'] == 500.0
    assert {second, fourth} <= {r['id'] for r in app.store.query('SELECT id FROM quiz_attempts')}
    # The default clocks are read at each call, so the host clock in force is always the one used.
    live = QuizAttempts(app)
    assert live.wall() > 1_000_000_000 and live.clock() >= 0


@pytest.mark.parametrize('bad', [{'q1': 2}, {'q1': -1}, {'q1': True}, {'q1': 1.0}, {'q1': '1'}, {'q3': 0},
                                 {'__proto__': 0}, [1, 0], None, 'q1'])
def test_p19_quiz_answers_are_offered_choices_only(app, clock, bad):
    owner, row = published(app)
    attempts = grader(app, clock)
    ident = attempts.start(owner.cid, row['id'], 1)['attempt_id']
    for action in (attempts.save, attempts.submit):
        with pytest.raises(ValueError):
            action(owner.cid, ident, 1, bad)
    assert [r['status'] for r in app.store.query('SELECT status FROM quiz_attempts')] == ['open']


def test_p19_quiz_forged_stale_and_cross_chat_calls_are_refused_and_write_nothing(app, clock):
    owner, row = published(app)
    cid, aid, other = owner.cid, row['id'], app.new_chat()['id']
    attempts = grader(app, clock)
    ident = attempts.start(cid, aid, 1)['attempt_id']
    before = snapshot(app)
    forged = ident[:-1] + ('0' if ident[-1] != '0' else '1')
    for chat, attempt, revision in ((other, ident, 1), (cid, forged, 1), (cid, 'f' * 32, 1), (cid, ident.upper(), 1),
                                    (cid, '../' + ident, 1), (cid, None, 1), (cid, ident, 2), (cid, ident, 0),
                                    (cid, ident, True), (cid, ident, '1')):
        for action in (attempts.save, attempts.submit):
            with pytest.raises(ValueError):
                action(chat, attempt, revision, {'q1': 1})
    for chat, revision in ((other, 1), (cid, 2), (cid, 0), (cid, True), (cid, None)):
        with pytest.raises(ValueError):
            attempts.start(chat, aid, revision)
    for chat, attempt in ((other, ident), (cid, forged)):
        with pytest.raises(ValueError):
            attempts.share(chat, attempt, 'main')
    with pytest.raises(ValueError):
        attempts.attempt(other, aid, 1)
    assert snapshot(app) == before and not inbox(app)
    # Self-check quizzes (mod and earlier views) and other kinds have no host attempts.
    legacy = app.artifacts.render(cid, 'Self-check', 'quiz', quiz())
    table = app.artifacts.render(cid, 'Table', 'table', {'columns': [{'id': 'a', 'label': 'A'}], 'rows': []})
    assert app.artifact_get(cid, legacy['id'])['grading'] == 'self_check' and 'attempts' not in app.artifact_get(cid, legacy['id'])
    for view in (legacy, table):
        with pytest.raises(ValueError, match='not a host-graded quiz'):
            attempts.start(cid, view['id'], 1)


def test_p19_quiz_a_tampered_key_cannot_grade_or_export(app, clock):
    owner, row = published(app)
    app.store.query("UPDATE quiz_answer_keys SET key_json=replace(key_json,'\"answer\":1','\"answer\":0')")
    attempts = grader(app, clock)
    with pytest.raises(ValueError, match='integrity'):
        attempts.start(owner.cid, row['id'], 1)
    source = {'kind': 'artifact', 'id': row['id']}
    assert 'Correct answer' not in compact(app.exports._resolve(owner.cid, source, {'include_answers': False, 'paper': 'a4'})['blocks'])
    with pytest.raises(ValueError, match='answer key is unavailable'):
        app.exports._resolve(owner.cid, source, {'include_answers': True, 'paper': 'a4'})
    assert quiz_grading.export_key(app.store.db, app.new_chat()['id'], row['id'], 1, PUBLIC) is None


# Revision drift (section 3.7) ----------------------------------------------------------------------

def test_p19_update_drift_supersedes_open_attempts_and_keeps_graded_ones(app, clock):
    attempts = grader(app, clock)
    with Owner(app) as owner:
        row = owner.render()
        cid, aid = owner.cid, row['id']
        graded = attempts.start(cid, aid, 1)['attempt_id']
        attempts.submit(cid, graded, 1, {'q1': 1, 'q2': 0})
        clock.wall += 1
        open_one = attempts.start(cid, aid, 1)['attempt_id']
        attempts.save(cid, open_one, 1, {'q1': 0})
        with pytest.raises(PolicyError, match='stale'):
            owner.tool('UIUpdate', id=aid, revision=2, title='Science check', kind='quiz', data=quiz())
        updated = owner.tool('UIUpdate', id=aid, revision=1, title='Science check', kind='quiz', data=quiz())
        # Merge (i19): the tool result is the model view (P6); the stored revision is read as a window reads it.
        assert updated['revision'] == 2 and app.artifact_get(cid, aid)['data'] == PUBLIC and 'data' not in updated
    statuses = {r['id']: (r['status'], r['reason']) for r in app.store.query('SELECT id,status,reason FROM quiz_attempts')}
    assert statuses == {graded: ('submitted', None), open_one: ('superseded', 'updated')}
    with pytest.raises(ValueError, match='changed to revision 2'):
        attempts.submit(cid, open_one, 1, {'q1': 1, 'q2': 0})
    with pytest.raises(ValueError, match='changed to revision 2'):
        attempts.save(cid, open_one, 2, {'q1': 1})
    before = snapshot(app)
    kept = attempts.attempt(cid, aid, 1)  # the latest attempt on revision 1, with the person's draft kept
    assert (kept['attempt_id'], kept['status'], kept['answers'], kept['result']) == (open_one, 'superseded', {'q1': 0}, None)
    assert 'not graded' in kept['message'] and snapshot(app) == before
    assert attempts.attempt(cid, aid, 2) is None
    with pytest.raises(ValueError, match='changed to revision 2'):
        attempts.start(cid, aid, 1)
    fresh = attempts.start(cid, aid, 2)
    assert fresh['revision'] == 2 and len(app.store.query('SELECT * FROM quiz_answer_keys WHERE artifact_id=?', (aid,))) == 2
    # A native stop closes the open attempt on its revision, and no new attempt starts.
    app.artifact_stop(cid, aid, 2)
    assert app.store.query('SELECT status,reason FROM quiz_attempts WHERE id=?', (fresh['attempt_id'],)) == [
        {'status': 'superseded', 'reason': 'stopped'}]
    with pytest.raises(ValueError):
        attempts.start(cid, aid, 3)
    # The graded attempt on revision 1 stays the person's to share.
    assert attempts.share(cid, graded, 'main')['shared'] is True


def test_p19_update_drift_a_kind_change_ends_host_grading(app, clock):
    attempts = grader(app, clock)
    with Owner(app) as owner:
        row = owner.render()
        ident = attempts.start(owner.cid, row['id'], 1)['attempt_id']
        owner.tool('UIUpdate', id=row['id'], revision=1, title='Notes', kind='table',
                   data={'columns': [{'id': 'a', 'label': 'A'}], 'rows': []})
    assert app.store.query('SELECT status FROM quiz_attempts WHERE id=?', (ident,)) == [{'status': 'superseded'}]
    with pytest.raises(ValueError, match='not a host-graded quiz'):
        attempts.start(owner.cid, row['id'], 2)


# Share -------------------------------------------------------------------------------------------

def test_p19_quiz_share_needs_the_main_window_and_writes_one_human_input(app, clock):
    owner, row = published(app)
    cid, aid, rid = owner.cid, row['id'], owner.rid
    attempts = grader(app, clock)
    first = attempts.start(cid, aid, 1)['attempt_id']
    with pytest.raises(ValueError, match='Only a submitted'):
        attempts.share(cid, first, 'main')
    attempts.submit(cid, first, 1, {'q1': 1, 'q2': 1})
    assert not inbox(app)  # grading alone sends nothing
    for surface in (None, 'pet', 'Main', 'main ', 'generated', True):
        reply = attempts.share(cid, first, surface)
        assert reply['status'] == 'compose' and reply['shared'] is False and reply['reason'] == 'Share from the main window.'
        assert reply['text'].startswith('My result for the quiz "Science check" (revision 1): 1 of 2 correct.')
        assert 'Q2: incorrect (chose B)' in reply['text'] and CANARY not in compact(reply)
    assert not inbox(app)
    app.runs.transition(rid, 'waiting_input')
    shared = attempts.share(cid, first, 'main')
    assert shared == {'input_id': 'quiz-' + first, 'run_id': rid, 'status': 'shared', 'origin': 'human', 'shared': True}
    assert attempts.share(cid, first, 'main')['input_id'] == shared['input_id']
    assert attempts.share(cid, first, 'pet')['shared'] is True  # already shared: a report, never a second input
    rows = inbox(app)
    assert len(rows) == 1 and (rows[0]['origin'], rows[0]['run_id'], rows[0]['status']) == ('human', rid, 'pending')
    payload = json.loads(rows[0]['payload'])
    assert payload == {'artifact_id': aid, 'revision': 1, 'attempt_id': first, 'score': 1, 'total': 2,
                       'per_question': [{'id': 'q1', 'chosen': 1, 'correct': True}, {'id': 'q2', 'chosen': 1, 'correct': False}]}
    assert app.interactions.continuation(rid)['reason'] == 'artifact_result'
    assert any(e['kind'] == 'input_queued' and e['payload'] == {'input_id': 'quiz-' + first, 'kind': 'artifact_result', 'origin': 'human'}
               for e in app.runs.events(rid, 0, 200))
    assert app.artifact_get(cid, aid)['attempts'][0]['shared'] is True
    delivered = app.interactions.deliver(rid, {'messages': []}, 1, 0.0)
    assert 'HOST INPUT (human-origin; never a permission change)' in delivered['messages'][0]['content']
    assert 'artifact_result' in delivered['messages'][0]['content'] and CANARY not in compact(delivered)
    # When the owning run has finished, nothing is written: the person gets text to send themselves.
    app.runs.transition(rid, 'completed')
    second = attempts.start(cid, aid, 1)['attempt_id']
    attempts.submit(cid, second, 1, {'q1': 1, 'q2': 0})
    finished = attempts.share(cid, second, 'main')
    assert finished['status'] == 'compose' and finished['reason'] == 'The run that made this quiz has finished.'
    assert '2 of 2 correct' in finished['text'] and len(inbox(app)) == 1


def test_p19_quiz_forget_removes_only_this_chats_keys_and_attempts(app, clock):
    attempts = grader(app, clock)
    first, row = published(app)
    attempts.start(first.cid, row['id'], 1)
    app.runs.transition(first.rid, 'completed')
    second, other = published(app)
    attempts.start(second.cid, other['id'], 1)
    app.artifact_inputs.forget(first.cid)
    assert {r['chat_id'] for r in app.store.query('SELECT chat_id FROM quiz_answer_keys')} == {second.cid}
    assert {r['chat_id'] for r in app.store.query('SELECT chat_id FROM quiz_attempts')} == {second.cid}


def test_p19_quiz_release_v03_flow_holds_once_the_epoch9_tables_exist(app, clock):
    """test_release_v03 renders a ten-question quiz and deletes the chat; with the tables it keeps passing."""
    cid = app.new_chat()['id']
    data = {'duration_minutes': 30, 'questions': [{'question': '2+2?', 'choices': ['3', '4'], 'answer': 1,
                                                   'explanation': 'Arithmetic ' + CANARY}] * 10}
    app.execute('UIRender', {'title': 'Practice', 'kind': 'quiz', 'data': json.dumps(data)}, None, cid)
    view = app.chat(cid)['artifacts'][0]
    assert [q['id'] for q in view['data']['questions']] == ['q' + str(i) for i in range(1, 11)] and view['owner_run_id'] is None
    grader(app, clock).start(cid, view['id'], 1)
    app.delete_chat(cid)
    assert not app.artifacts.list(cid)
    assert not app.store.query('SELECT * FROM quiz_answer_keys') and not app.store.query('SELECT * FROM quiz_attempts')


def test_p19_quiz_window_records_redaction_helpers():
    call = {'title': 'Q', 'kind': 'quiz', 'data': json.dumps(quiz())}
    redacted = quiz_grading.redact_quiz_arguments('UIRender', call)
    assert CANARY not in compact(redacted) and redacted['title'] == 'Q' and call['data'] == json.dumps(quiz())
    for name, arguments in (('UIRender', {'kind': 'table', 'data': '{}'}), ('Read', {'kind': 'quiz'}), ('UIUpdate', 'x')):
        assert quiz_grading.redact_quiz_arguments(name, arguments) is arguments
    for broken in ('{', '{"questions":7}', '[]', json.dumps({'questions': ['x']})):
        assert quiz_grading.redact_quiz_arguments('UIUpdate', {'kind': 'quiz', 'data': broken})['data'] == '[quiz data withheld: answer key]'
    response = {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'a', 'type': 'function', 'function': {'name': 'UIRender', 'arguments': json.dumps(call)}},
        {'id': 'b', 'type': 'function', 'function': {'name': 'Read', 'arguments': json.dumps({'file_path': CANARY})}},
        {'id': 'c', 'type': 'function', 'function': {'name': 'UIRender', 'arguments': 'not json'}}]}
    cleaned = quiz_grading.redact_response(response)
    assert CANARY not in cleaned['tool_calls'][0]['function']['arguments']
    assert cleaned['tool_calls'][1:] == response['tool_calls'][1:] and response['tool_calls'][0]['function']['arguments'] == json.dumps(call)
    assert quiz_grading.redact_response({'role': 'assistant', 'content': 'x'}) == {'role': 'assistant', 'content': 'x'}
