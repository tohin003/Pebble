"""Phase 19 B2: generated UI has no native authority (experiment E4) and a 4K window pays nothing new.

Views are data drawn by host components. Nothing generated becomes an RPC, URL, script, style,
handler, approval, answer or task input; the only routes that change task data are native chrome.
The view schemas reach a 4K window as one short brief, and a kind's exact schema only on a failure.
"""
import copy
import json

import pytest

from pebble import artifact_schema, quiz_grading
from pebble.app import App
from pebble.artifact_inputs import model_result
from pebble.artifact_schema import LEGACY_KINDS, NEW_KINDS, REJECTED_KEYS, SCHEMAS, UI_BRIEF, offered_kinds, schema_error
from pebble.mods import validate_artifacts
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError
from pebble.tool_catalogue import TOOLS

UI_SCHEMA_BYTES_18 = {'UIRender': 1108, 'UIUpdate': 702}  # Phase 18 compact bytes of the 4K copies


@pytest.fixture
def app(tmp_path):
    events = []
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(copy.deepcopy(args)))
    value.model.complete = lambda *args, **kwargs: pytest.fail('No model inference in these tests.')
    value.events = events
    groups = value.store.setting('permissions', DEFAULT)['tools']
    value.setting('permissions', {'mode': 'ask', 'tools': dict.fromkeys(groups, True)})
    with value.store.lock:
        value.store.db.executescript(quiz_grading.DDL)
    yield value
    value.active = value.active_run = None
    value.close()


class Owner:
    def __init__(self, app):
        self.app, self.chat = app, app.new_chat()
        self.cid = self.chat['id']
        self.rid = app.runs.create(self.cid, 'Draw views.', app.runtime_config(self.chat, False))
        app.runs.transition(self.rid, 'running')
        self.calls = 0

    def __enter__(self):
        self.previous = self.app.active, self.app.active_run
        self.app.active, self.app.active_run = self.cid, self.rid
        return self

    def __exit__(self, *unused):
        self.app.active, self.app.active_run = self.previous

    def render(self, kind, data, title='Generated view'):
        self.calls += 1
        args = {'title': title, 'kind': kind, 'data': data if isinstance(data, str) else json.dumps(data)}
        return self.app.execute('UIRender', args, None, self.cid, call_id=f'{self.calls}:uirender')


def prototype():
    return {'start': 'home', 'screens': [
        {'id': 'home', 'title': 'Home', 'components': [{'type': 'button', 'label': 'Approve payment', 'goto': 'done'},
                                                       {'type': 'input', 'label': 'Card number'}]},
        {'id': 'done', 'title': 'Done', 'components': [{'type': 'heading', 'text': 'Payment approved'}]}]}


def dashboard(binding=None):
    panel = {'id': 'p1', 'title': 'Runs', 'kind': 'metric', 'data': {'value': 3}}
    if binding is not None:
        panel = {'id': 'p1', 'title': 'Runs', 'kind': 'table', 'binding': binding}
    return {'panels': [panel]}


QUIZ = {'duration_minutes': 5, 'questions': [{'question': 'Two plus two?', 'choices': ['3', '4'], 'answer': 1,
                                              'explanation': 'Two and two make four.'}]}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def authority_rows(app):
    return {table: app.store.query(f'SELECT count(*) AS n FROM {table}')[0]['n']
            for table in ('actions', 'run_questions', 'run_inbox', 'artifact_form_links', 'run_continuations')}


def keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(v) for v in value)) if value else set()
    return set()


# p19_ui_schema_4k --------------------------------------------------------------------------------

def test_p19_ui_schema_4k_brief_lists_only_the_legacy_kinds():
    assert len(UI_BRIEF) <= 200
    assert all(kind in UI_BRIEF for kind in LEGACY_KINDS) and not any(kind in UI_BRIEF for kind in NEW_KINDS)
    assert set(SCHEMAS) == set(LEGACY_KINDS) | set(NEW_KINDS) and all(len(text) <= 800 for text in SCHEMAS.values())


@pytest.mark.parametrize('budget', [2048, 4096, True, None, 4096.5, '8192'])
def test_p19_ui_schema_4k_offers_nothing_new_at_4096_or_below(budget):
    assert offered_kinds(budget, 'Build a dashboard, a KPI scorecard and an app prototype mock-up.') == LEGACY_KINDS


def test_p19_ui_schema_4k_new_kinds_are_gated_by_their_own_words_above_4096():
    assert offered_kinds(8192, 'Draw a chart of the release.') == LEGACY_KINDS
    assert offered_kinds(8192, 'Build a dashboard for the release.') == LEGACY_KINDS + ('dashboard',)
    assert offered_kinds(8192, 'Sketch a wireframe click-through.') == LEGACY_KINDS + ('prototype',)
    assert offered_kinds(16384, 'A KPI scorecard and an app demo') == LEGACY_KINDS + NEW_KINDS
    assert offered_kinds(8192, None) == LEGACY_KINDS


def test_p19_ui_schema_4k_brief_copies_fit_the_phase18_bytes():
    for name, limit in UI_SCHEMA_BYTES_18.items():
        spec = copy.deepcopy(next(t for t in TOOLS if t['function']['name'] == name))
        spec['function']['description'] = UI_BRIEF
        properties = spec['function']['parameters']['properties']
        properties['kind'] = {**properties['kind'], 'enum': list(offered_kinds(4096, 'dashboard prototype'))}
        for field in properties.values():
            field.pop('description', None)
        assert len(compact(spec).encode()) <= limit, (name, len(compact(spec).encode()))


def test_p19_ui_schema_4k_a_failure_returns_only_that_kinds_exact_schema(app):
    assert schema_error('dashboard', ValueError('Bad panel.')) == 'Bad panel. Schema: ' + SCHEMAS['dashboard']
    assert schema_error('dashboard', schema_error('dashboard', 'x')).count('Schema: ') == 1
    assert schema_error('mcp_media', 'x') == 'x'
    with Owner(app) as owner:
        for kind, data in (('dashboard', {'panels': []}), ('prototype', {'start': 'x', 'screens': []}),
                           ('quiz', {'duration_minutes': 5, 'questions': []}), ('table', {'columns': []}),
                           ('html', {'html': '<p>x</p>', 'script': 'run()'})):
            with pytest.raises(PolicyError) as caught:
                owner.render(kind, data)
            message = str(caught.value)
            assert message.endswith('Schema: ' + SCHEMAS[kind]), (kind, message)
            assert all(SCHEMAS[other] not in message for other in SCHEMAS if other != kind)
        with pytest.raises(PolicyError) as caught:
            owner.render('table', {'columns': [{'id': 'a', 'label': 'A'}], 'rows': []}, title='')
        assert 'Schema: ' not in str(caught.value)  # only the data schema rides a failure


# E4: authority escape ----------------------------------------------------------------------------

def test_e4_generated_views_create_no_question_input_action_or_wait(app):
    with Owner(app) as owner:
        before = authority_rows(app)
        rows = [owner.render('prototype', prototype()), owner.render('dashboard', dashboard({'source': 'runs.recent'})),
                owner.render('dashboard', dashboard()), owner.render('quiz', QUIZ)]
        assert authority_rows(app) == before
    for row in rows:
        assert not {'question_id', 'required'} & set(row) and row['status'] == 'ready'
        assert not {'question_id', 'required', 'bindings'} & set(model_result(row))
    # The Approve button is navigation data only; the run never waits on generated presentation.
    # Merge (i19): the tool result is the model view (P6), so the stored prototype is read as a window reads it.
    stored = app.artifact_get(owner.cid, rows[0]['id'])
    assert stored['data']['screens'][0]['components'][0] == {'type': 'button', 'label': 'Approve payment', 'goto': 'done'}
    assert app.runs.get(owner.rid)['state'] == 'running'


@pytest.mark.parametrize('kind,data', [
    ('prototype', {**prototype(), 'href': 'https://evil.example'}),
    ('prototype', {'start': 'home', 'screens': [{'id': 'home', 'title': 'Home', 'components': [
        {'type': 'button', 'label': 'Pay', 'goto': 'home', 'onclick': 'window.__TAURI__.invoke("approve")'}]}]}),
    ('prototype', {'start': 'home', 'screens': [{'id': 'home', 'title': 'Home', 'style': 'display:none', 'components': []}]}),
    ('prototype', {'start': 'home', 'screens': [{'id': 'home', 'title': 'Home', 'components': [
        {'type': 'button', 'label': 'Approve', 'goto': 'https://evil.example/approve'}]}]}),
    ('prototype', {'start': 'home', 'screens': [{'id': 'home', 'title': 'Home', 'components': [
        {'type': 'heading', 'text': '<script>parent.postMessage("approve","*")</script>'}]}]}),
    ('dashboard', dashboard({'source': 'memory.records'})), ('dashboard', dashboard({'source': '/etc/passwd'})),
    ('dashboard', dashboard({'source': 'runs.recent', 'params': {'chat_id': 'another-chat'}})),
    ('dashboard', {'panels': [{'id': 'p1', 'title': 'Form', 'kind': 'form', 'data': {'fields': []}}]}),
    ('dashboard', {'panels': [{**dashboard()['panels'][0], 'ｏｎｃｌｉｃｋ': 'x()'}]}),
    ('html', {'html': '<p>x</p>', 'script': 'window.__TAURI__.invoke("approve")'}),
    ('quiz', {'duration_minutes': 5, 'questions': [{'id': 'q1', 'question': 'Two plus two?', 'choices': ['3', '4']}]}),
])
def test_e4_hostile_generated_data_is_refused_before_anything_is_stored(app, kind, data):
    with Owner(app) as owner:
        before = authority_rows(app)
        with pytest.raises(PolicyError):
            owner.render(kind, data)
        assert not app.artifacts.list(owner.cid) and authority_rows(app) == before


def test_e4_stored_views_carry_no_link_style_markup_handler_or_script_field(app):
    with Owner(app) as owner:
        rows = [owner.render('prototype', prototype()), owner.render('dashboard', dashboard({'source': 'runs.recent'})),
                owner.render('quiz', QUIZ), owner.render('html', {'html': '<p>Static</p>', 'script': '  ', 'language': 'javascript'})]
    for row in rows[:3]:  # the html kind's own field is its static markup
        stored = app.artifact_get(owner.cid, row['id'])
        found = {k.casefold() for k in keys(stored['data'])}
        assert not found & REJECTED_KEYS and not any(k.startswith('on') for k in found), (row['kind'], found)
    assert app.artifact_get(owner.cid, rows[3]['id'])['data'] == {'html': '<p>Static</p>'}  # F8: no script or language kept
    assert not {'answer', 'explanation'} & keys(app.artifact_get(owner.cid, rows[2]['id']))


def test_e4_mods_publish_inline_dashboards_prototypes_and_self_check_quizzes_only():
    accepted = validate_artifacts([{'id': 'a', 'title': 'Board', 'kind': 'dashboard', 'data': dashboard()},
                                   {'id': 'b', 'title': 'Demo', 'kind': 'prototype', 'data': prototype()},
                                   {'id': 'c', 'title': 'Self-check', 'kind': 'quiz', 'data': QUIZ}])
    assert [p['kind'] for p in accepted] == ['dashboard', 'prototype', 'quiz'] and accepted[2]['data'] == QUIZ
    public, _ = quiz_grading.split(QUIZ)
    linked = prototype()
    linked['screens'][0]['components'][0]['href'] = 'https://evil.example'
    for kind, data in (('dashboard', dashboard({'source': 'runs.recent'})), ('dashboard', {'panels': []}),
                       ('dashboard', {'panels': 'p1'}), ('prototype', linked), ('quiz', public),
                       ('quiz', {**QUIZ, 'version': 2}), ('form', {'fields': [{'id': 'a', 'label': 'Approve?', 'type': 'text'}]}),
                       ('html', {'html': '<p>x</p>'}), ('mcp_media', {'run_id': 'r', 'snapshot_id': 's'})):
        with pytest.raises(PolicyError):
            validate_artifacts([{'id': 'pane', 'title': 'Pane', 'kind': kind, 'data': data}])


def test_e4_model_facing_results_name_identifiers_never_host_counts_or_the_key(app):
    with Owner(app) as owner:
        results = (owner.render('quiz', QUIZ), owner.render('dashboard', dashboard({'source': 'runs.usage'})),
                   owner.render('prototype', prototype()))
        table = owner.render('table', {'columns': [{'id': 'a', 'label': 'A'}], 'rows': [{'a': 1}]})
    # Merge (i19): with parent patch P6 app.execute returns model_result (plus its receipt_id) for these kinds, so
    # the stored rows are read back to rebuild it, and the tool results must equal it exactly.
    quiz, board, demo = (app.artifacts.get(owner.cid, r['id']) for r in results)
    for result, row in zip(results, (quiz, board, demo)):
        assert {k: v for k, v in result.items() if k != 'receipt_id'} == model_result(row) and 'data' not in result
    assert 'data' in table  # earlier kinds keep their full row as the tool result
    base = {'id', 'revision', 'kind', 'title', 'status', 'evidence_ids', 'data_hash', 'record_hash', 'message'}
    assert set(model_result(quiz)) == base | {'grading', 'duration_minutes', 'question_ids'}
    assert set(model_result(board)) == base | {'panels'} and model_result(board)['panels'] == [{'id': 'p1', 'kind': 'table', 'source': 'runs.usage'}]
    assert model_result(demo) == {**{k: demo[k] for k in base - {'message'}}, 'start': 'home', 'screens': ['home', 'done'],
                                  'message': 'Mock-up saved. Its controls only switch between its own screens.'}
    assert model_result(table) is table  # earlier kinds keep their full row
    assert 'Two and two' not in compact([model_result(quiz), quiz, results, app.events])


def test_e4_quiz_routes_are_human_task_data_bound_to_their_chat(app):
    attempts = quiz_grading.QuizAttempts(app)
    with Owner(app) as owner:
        row = owner.render('quiz', QUIZ)
    other = app.new_chat()['id']
    with pytest.raises(ValueError):
        attempts.start(other, row['id'], 1)
    ident = attempts.start(owner.cid, row['id'], 1)['attempt_id']
    graded = attempts.submit(owner.cid, ident, 1, {'q1': 1})
    assert graded['origin'] == 'human' and graded['result']['score'] == 1
    # Without the person's Share from the main window nothing reaches the run.
    assert attempts.share(owner.cid, ident)['status'] == 'compose'
    assert not app.store.query("SELECT 1 FROM run_inbox WHERE kind='artifact_result'")
    assert attempts.share(owner.cid, ident, 'main')['shared'] is True
    assert [r['origin'] for r in app.store.query("SELECT origin FROM run_inbox WHERE kind='artifact_result'")] == ['human']


def test_e4_the_shared_text_and_view_schemas_stay_inert():
    # The schema texts and brief are host text: no link, markup or template.
    for text in (*SCHEMAS.values(), UI_BRIEF):
        assert not artifact_schema.REJECTED_TEXT.search(artifact_schema._fold(text)), text
