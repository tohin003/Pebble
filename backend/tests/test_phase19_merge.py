"""Phase 19 merge (i19): the parent patches hold together on the merged source.

Design study (docs/HARNESS_PHASE19_DESIGN_STUDY.md, D19): P1 the host activity class on every progress event
(section 2.2); P2 the view schemas and kind gating (3.1); P3 the UI guidance offer and its window limits (5);
P6 the companion setting (2.5), the model-facing view results and window-readable run records (3.3); P7 schema
epoch 9 (3.5); P8 the native commands and capability grants (2.4, 3.6); P11 the verifier suites. The track tests
and test_phase19_security.py cover the tracks' own modules; these cover the parent seams between them.

Every pin was measured on this merge. A 4K pin may only move down, and a re-pin needs a comment. No model
inference and no network: every model turn is a local fixture function.
"""
import copy
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from pebble import companion_settings, migrations, quiz_grading, ui_guidance
from pebble.app import App
from pebble.artifact_schema import LEGACY_KINDS, SCHEMAS
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError
from pebble.runner import SMALL_BRIEF
from pebble.token_budget import TokenBudget
from pebble.tool_catalogue import TOOLS

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / 'src-tauri' / 'resources'
CANARY = 'i19-merge-canary-' + 'beef' * 4
VIEWS = ('UIRender', 'UIUpdate')
# Phase 18 offered the catalogue copies of the view tools unchanged at 4K (it had no view brief). Its UIRender
# text, for the token bound below; the reconstruction is checked against the Phase 18 bytes.
PHASE18_UIRENDER = (
    'ui.render: Save a revisioned native widget. data is a JSON object string. form:{fields:[{id,label,type:"text"|'
    '"choice",required:true,options?:[{id,label}]}]} (1–3 required ordinary questions, free text always available); '
    'suspends work until native input. table:{columns:[{id,label}],rows:[objects]}. chart:{type:"bar"|"line",x_key,'
    'y_keys:[keys],rows:[objects]}. plan:{steps:[{id,title,status,evidence_ids?}]} completed steps need resolved '
    'receipt IDs. evidence:{items:[{id,label,evidence_id,summary?}]}. quiz:{duration_minutes,questions:[{question,'
    'choices,answer,explanation}]}. diagram:{nodes:[{id,label}],edges:[{from,to,label}]}. html:{html,css}: bounded '
    'static preview only. No credentials, approvals, host controls or inline scripts.')
PHASE18_BYTES = {'UIRender': 1108, 'UIUpdate': 702}
# The merged 4K copies: the brief, the Phase 18 eight kinds and no property descriptions (compact JSON bytes).
PINNED_4K_BYTES = {'UIRender': 515, 'UIUpdate': 518}
# The bundled o200k count of both 4K copies together (TokenBudget, framing removed); Phase 18's was larger.
PINNED_4K_O200K = 331
# Above 4K (compact bytes): an ordinary view request, a dashboard request and a prototype request. The ordinary
# UIRender copy is 90 bytes over Phase 18's 1108 for P4's wording (quiz answers stay with Pebble; scripts never run).
# Phase 21 (F21-quiz) adds 37 for the quiz types C1's ui-quiz refusals showed were missing (duration range, choices
# as strings, a 0-based answer index). The 4K copies, their o200k count and UIUpdate are unchanged.
PINNED_WIDE_BYTES = {'ordinary': {'UIRender': 1235, 'UIUpdate': 702},
                     'dashboard': {'UIRender': 1713, 'UIUpdate': 1180},
                     'prototype': {'UIRender': 1669, 'UIUpdate': 1136}}


@pytest.fixture
def app(tmp_path):
    events = []
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(copy.deepcopy(args)))
    value.model.complete = lambda *args, **kwargs: pytest.fail('No model inference here.')
    value.events = events
    groups = value.store.setting('permissions', DEFAULT)['tools']
    value.setting('permissions', {'mode': 'ask', 'tools': dict.fromkeys(groups, True)})
    yield value
    value.active = value.active_run = None
    value.close()


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def select(app, budget):
    app.model_save(name='Merge fixture', url='http://127.0.0.1:9', model='fixture-%d' % budget, context=budget)
    assert app.model.current()['budget'] == budget


def requests(app, budget, text, script=None):
    """Run one task on a local fixture model; returns the chat and every request as (tools by name, messages)."""
    select(app, budget)
    seen = []

    def complete(messages, tools, cancel, token, effort):
        seen.append(({t['function']['name']: copy.deepcopy(t) for t in tools}, copy.deepcopy(messages)))
        reply = script(len(seen)) if script else None
        if reply is None:
            token('Done.')
            return {'role': 'assistant', 'content': 'Done.'}
        return reply
    app.model.complete = complete
    chat = app.new_chat()
    app.work(app.chat(chat['id']), text, False, [])
    assert seen, 'the fixture model was never asked'
    return chat['id'], seen


def call(name, **arguments):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'c1', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


def guidance_in(messages):
    return any(m.get('_pebble_skill') and ui_guidance.ID in m['content'] for m in messages)


def sizes(tools):
    return {name: len(compact(tools[name]).encode()) for name in VIEWS}


def catalogue(name):
    return next(t for t in TOOLS if t['function']['name'] == name)


def phase18_copies():
    specs = {}
    for name in VIEWS:
        spec = copy.deepcopy(catalogue(name))
        if name == 'UIRender':
            spec['function']['description'] = PHASE18_UIRENDER
        spec['function']['parameters']['properties']['kind']['enum'] = list(LEGACY_KINDS)
        specs[name] = spec
    return specs


# P2: view schemas and kind gating ---------------------------------------------------------------------------

def test_p2_at_4096_view_tools_are_briefs_with_phase18_kinds_and_no_guidance(app):
    _, seen = requests(app, 4096, 'Build a dashboard view, a quiz and an app demo prototype for the release plan.')
    for tools, messages in seen:
        for name in VIEWS:
            spec = tools[name]
            assert spec['function']['description'] == SMALL_BRIEF[name]
            assert tuple(spec['function']['parameters']['properties']['kind']['enum']) == LEGACY_KINDS
        assert sizes(tools) == PINNED_4K_BYTES
        assert all(PINNED_4K_BYTES[n] < PHASE18_BYTES[n] for n in VIEWS)
        assert not guidance_in(messages) and 'LoadSkill' not in tools


def test_p2_at_4096_dashboard_and_prototype_words_alone_offer_no_view_tool(app):
    # Phase 18's keyword gate is unchanged at 4K: these requests offered no view tool then and offer none now.
    for text in ('Build a dashboard of my week.', 'Sketch a wireframe mock-up for a recipe app.'):
        _, seen = requests(app, 4096, text)
        assert all(not set(VIEWS) & set(tools) for tools, _ in seen), text


def test_p2_the_4k_copies_cost_fewer_tokens_than_phase18s(app):
    old = phase18_copies()
    assert sizes(old) == PHASE18_BYTES, 'the Phase 18 reconstruction is exact'
    _, seen = requests(app, 4096, 'Make a quiz about Python lists.')
    new = {name: seen[0][0][name] for name in VIEWS}
    if not (RESOURCES / 'tokenizer').is_dir():
        pytest.skip('Bundled o200k resources are absent; the byte pins still apply.')
    counter = TokenBudget(RESOURCES)
    framing = counter.count([], [])
    for name in VIEWS:
        assert counter.count([], [new[name]]) - framing < counter.count([], [old[name]]) - framing, name
    assert counter.count([], list(new.values())) - framing == PINNED_4K_O200K
    assert PINNED_4K_O200K < counter.count([], list(old.values())) - framing


@pytest.mark.parametrize('case,text,kinds', [
    ('ordinary', 'Make a table of the fruits I like.', LEGACY_KINDS),
    ('dashboard', 'Build a dashboard of my runs this week.', LEGACY_KINDS + ('dashboard',)),
    ('prototype', 'Sketch a clickable prototype of a recipe app.', LEGACY_KINDS + ('prototype',)),
])
def test_p2_above_4096_new_kinds_join_only_for_their_words_with_their_exact_schema(app, case, text, kinds):
    _, seen = requests(app, 16384, text)
    for tools, _ in seen:
        for name in VIEWS:
            spec = tools[name]
            assert tuple(spec['function']['parameters']['properties']['kind']['enum']) == kinds
            added = ''.join(' ' + SCHEMAS[k] for k in kinds if k not in LEGACY_KINDS)
            assert spec['function']['description'] == catalogue(name)['function']['description'] + added
        assert sizes(tools) == PINNED_WIDE_BYTES[case]
    # The canonical catalogue admits both new kinds; the per-request copy narrows them.
    assert catalogue('UIRender')['function']['parameters']['properties']['kind']['enum'][-2:] == ['dashboard', 'prototype']


def test_p2_a_4k_model_cannot_publish_a_new_kind_even_by_naming_it(app):
    def script(step):
        if step == 1:
            return call('UIRender', title='Runs', kind='dashboard', data=json.dumps(
                {'panels': [{'id': 'p1', 'title': 'Runs', 'kind': 'metric', 'data': {'value': 1}}]}))
    cid, seen = requests(app, 4096, 'Make a quiz about Python lists.', script)
    assert not app.artifacts.list(cid)
    tool_messages = [m for m in seen[1][1] if m['role'] == 'tool']
    assert tool_messages and 'error' in json.loads(tool_messages[-1]['content'])


# P3: the guidance offer and its window limits -----------------------------------------------------------------

def test_p3_the_guidance_is_offered_only_above_4096_for_layout_requests_with_its_switch_on(app):
    _, seen = requests(app, 16384, 'Build a dashboard of my runs this week.')
    assert guidance_in(seen[0][1]) and 'LoadSkill' in seen[0][0]
    assert not any(m.get('_pebble_required_skill') for m in seen[0][1]), 'never pinned until the model loads it'
    for budget, text in ((16384, 'Fix the failing parser test.'), (16384, 'Make a table of the fruits I like.'),
                         (4096, 'Build a dashboard view and a quiz for the release plan.')):
        _, seen = requests(app, budget, text)
        assert not guidance_in(seen[0][1]) and 'LoadSkill' not in seen[0][0], (budget, text)
    app.setting('companion', {**companion_settings.DEFAULT, 'ui_guidance': False})
    _, seen = requests(app, 16384, 'Build a dashboard of my runs this week.')
    assert not guidance_in(seen[0][1]) and 'LoadSkill' not in seen[0][0]


def test_p3_loading_or_selecting_the_guidance_is_refused_at_4096_and_its_references_at_8192(app):
    chat = app.new_chat()
    pinned = {'id': ui_guidance.ID, 'digest': ui_guidance.PINNED_DIGEST}
    page = {**pinned, 'reference': 'references/dashboards.md'}
    select(app, 4096)
    for args in (pinned, page):
        with pytest.raises(PolicyError, match='above 8,192'):
            app.execute('LoadSkill', dict(args), None, chat['id'])
    with pytest.raises(PolicyError, match='above 8,192'):
        app.send(chat['id'], '/skill pebble-ui-guidance Outline a dashboard of my week')
    assert not app.runs.list(chat['id']) and not app.chat(chat['id'])['messages']
    select(app, 8192)  # Phase 19 merge (parent): the guidance is admitted only above 8K
    for args in (pinned, page):
        with pytest.raises(PolicyError, match='above 8,192'):
            app.execute('LoadSkill', dict(args), None, chat['id'])
    select(app, 16384)
    assert app.execute('LoadSkill', dict(pinned), None, chat['id'])['untrusted_skill'] == ui_guidance.SKILL_MD
    assert app.execute('LoadSkill', dict(page), None, chat['id'])['untrusted_source'] == ui_guidance.DASHBOARDS


# P1: the host activity class --------------------------------------------------------------------------------

def test_p1_every_progress_event_carries_the_hosts_class_last(app):
    def script(step):
        if step == 1:
            return call('UIRender', title='Fruit', kind='table',
                        data=json.dumps({'columns': [{'id': 'a', 'label': 'A'}], 'rows': [{'a': 1}]}))
    requests(app, 16384, 'Make a table of the fruits I like.', script)
    progress = [e[1] for e in app.events if e[0] == 'progress']
    assert progress and all(list(p)[-1] == 'activity' for p in progress)
    working = [p for p in progress if p.get('tool') == 'UIRender' and p['kind'] == 'working']
    assert working and all(p['activity'] == 'viewing' for p in working)
    assert all(p['activity'] is None for p in progress if p['kind'] != 'working')
    assert {p['activity'] for p in progress if p['kind'] == 'working' and 'tool' not in p} == {'thinking'}


# P6: model-facing results, window-readable run records and the companion setting -----------------------------

def quiz():
    return {'duration_minutes': 5, 'questions': [
        {'question': 'Which gas do plants absorb?', 'choices': ['Oxygen', 'Carbon dioxide'], 'answer': 1,
         'explanation': 'Plants take in carbon dioxide. ' + CANARY}]}


def test_p6_a_quiz_run_keeps_the_key_from_the_model_result_and_every_window_readable_run_record(app):
    def script(step):
        if step == 1:
            return call('UIRender', title='Science check', kind='quiz', data=json.dumps(quiz()))
    cid, seen = requests(app, 16384, 'Make a quiz about plants.', script)
    rid = app.runs.list(cid)[0]['id']
    shown = next(m for m in seen[1][1] if m['role'] == 'tool')['content']  # the model's own call is its input
    result = json.loads(shown)
    assert result['grading'] == 'host' and result['question_ids'] == ['q1'] and 'data' not in result
    assert CANARY not in shown and '"answer"' not in shown
    for method, params in (('run_get', {'id': cid, 'run_id': rid}), ('run_events', {'id': cid, 'run_id': rid}),
                           ('get_chat', {'id': cid}), ('state', {})):
        assert CANARY not in compact(app.dispatch(method, params)), method
    # The stored journal keeps the model's own call (recovery reads it); only the window copies are redacted.
    raw = app.store.query('SELECT payload FROM run_events WHERE run_id=?', (rid,))
    assert CANARY in compact(raw)
    assert CANARY not in compact([t['result'] for t in app.runs.tools(rid)])


def test_p6_archives_and_event_payloads_are_redacted_at_any_depth_and_other_records_are_unchanged():
    arguments = {'title': 'Q', 'kind': 'quiz', 'data': json.dumps(quiz())}
    archive = {'messages': [{'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'c1', 'type': 'function', 'function': {'name': 'UIRender', 'arguments': json.dumps(arguments)}}]}],
        'receipts': [{'name': 'UIUpdate', 'arguments': {**arguments, 'id': 'a', 'revision': 1}}],
        'nested': [[{'name': 'Read', 'arguments': {'file_path': CANARY}}]]}
    redacted = quiz_grading.redact_archive(archive)
    assert compact(redacted).count(CANARY) == 1 and redacted['nested'] == archive['nested']
    assert CANARY in compact(archive), 'the input is never mutated'
    plain = {'messages': [{'role': 'assistant', 'content': 'x', 'tool_calls': [
        {'id': 'c', 'type': 'function', 'function': {'name': 'UIRender', 'arguments': json.dumps({'kind': 'table'})}}]}]}
    assert quiz_grading.redact_archive(plain) == plain


def test_p6_the_companion_setting_is_reported_validated_and_announced(app):
    assert app.state()['settings']['companion'] == companion_settings.DEFAULT
    value = {'pointer': False, 'reactions': True, 'status_words': 'fixed', 'ui_guidance': False}
    app.events.clear()
    assert app.dispatch('setting', {'key': 'companion', 'value': value}) is True
    assert app.state()['settings']['companion'] == value and [e[0] for e in app.events] == ['settings']
    with pytest.raises(ValueError):
        app.setting('companion', {k: v for k, v in value.items() if k != 'ui_guidance'})
    assert app.state()['settings']['companion'] == value
    # A malformed stored row reads as the defaults instead of breaking state().
    app.store.query('INSERT OR REPLACE INTO settings VALUES(?,?)', ('companion', '{"pointer": "on"}'))
    assert app.state()['settings']['companion'] == companion_settings.DEFAULT


# P7: schema epoch 9 -----------------------------------------------------------------------------------------

ARTIFACT_TABLES = ('artifact_revisions', 'artifact_heads', 'artifact_registry', 'artifact_form_links')


def test_p7_epoch_9_rules_cover_every_column_with_no_transition(app):
    assert migrations.SCHEMA_EPOCH == 9
    with app.store.lock:
        for table in (*quiz_grading.TABLES, *ARTIFACT_TABLES):
            columns = {row[1] for row in app.store.db.execute(f'PRAGMA table_info({table})')}
            keep = migrations.PRESERVED[table]
            assert set(keep.keys) | set(keep.stable) == columns and not set(keep.keys) & set(keep.stable), table
            assert (keep.exempt, keep.missing, keep.transition) == (None, None, None), table
    assert set(quiz_grading.TABLES) <= set(migrations.NO_NEW)
    assert not set(ARTIFACT_TABLES) & set(migrations.NO_NEW), 'startup may materialize legacy artifact rows'
    assert all('CREATE TABLE IF NOT EXISTS ' + t in migrations.REFERENCE_DDL for t in quiz_grading.TABLES)


def test_p7_an_epoch_8_home_upgrades_to_9_with_its_v1_quiz_byte_for_byte_and_empty_quiz_tables(tmp_path):
    home, resources = tmp_path / 'home', tmp_path / 'resources'
    first = App(home, resources, lambda *args: None)
    try:
        cid = first.new_chat()['id']
        legacy = {'duration_minutes': 5, 'questions': [{'question': 'Two plus two?', 'choices': ['3', '4'], 'answer': 1,
                                                         'explanation': 'Four.'}]}
        aid = first.artifacts.render(cid, 'Self-check', 'quiz', legacy, None)['id']  # a Phase 18 (v1) quiz
        before = compact(first.artifact_get(cid, aid))
    finally:
        first.close()
    # Make it an epoch-8 database: no quiz tables and the Phase 18 user_version.
    db = sqlite3.connect(home / 'pebble.sqlite')
    try:
        db.executescript('DROP TABLE quiz_attempts; DROP TABLE quiz_answer_keys; PRAGMA user_version=8;')
        rows = {t: db.execute(f'SELECT * FROM {t} ORDER BY 1').fetchall() for t in ('artifacts', *ARTIFACT_TABLES)}
    finally:
        db.close()
    second = App(home, resources, lambda *args: None)
    try:
        assert second.upgrade_receipt['completed']
        assert compact(second.artifact_get(cid, aid)) == before and second.artifact_get(cid, aid)['grading'] == 'self_check'
        with second.store.lock:
            assert second.store.db.execute('PRAGMA user_version').fetchone()[0] == 9
            for table in quiz_grading.TABLES:
                assert second.store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
            assert {t: second.store.db.execute(f'SELECT * FROM {t} ORDER BY 1').fetchall() for t in rows} == rows
    finally:
        second.close()


# P8 and P11: native grants and verifier suites ---------------------------------------------------------------

def test_p8_the_gaze_command_is_granted_and_the_webviews_cannot_read_the_cursor():
    capabilities = json.loads((ROOT / 'src-tauri' / 'capabilities' / 'main.json').read_text())['permissions']
    assert 'allow-pet-gaze' in capabilities and 'core:window:deny-cursor-position' in capabilities
    assert '"pet_gaze"' in (ROOT / 'src-tauri' / 'build.rs').read_text()
    assert 'commands.allow = ["pet_gaze"]' in (ROOT / 'src-tauri' / 'permissions' / 'autogenerated' / 'pet_gaze.toml').read_text()
    source = (ROOT / 'src-tauri' / 'src' / 'main.rs').read_text()
    assert 'fn pet_expand(app:tauri::AppHandle, expanded:bool, sidebar:Option<bool>, status:Option<bool>)' in source
    assert 'else if status.unwrap_or(false) {(176.0,136.0)}' in source
    # The pet's settings gate is unchanged: it may change permissions only, never the companion setting.
    assert 'window.label()=="pet" && method=="setting" && params.get("key").and_then(|x|x.as_str())!=Some("permissions")' in source


def test_p11_the_verifier_runs_the_phase19_suites_in_their_own_namespace():
    spec = importlib.util.spec_from_file_location('verify_i19', ROOT / 'scripts' / 'verify_harness_candidate.py')
    verify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verify)
    # Merge (i20): Phase 20 appends 'phase20' after it, so the check is by position, as test_phase16_evidence does.
    rank = verify.PHASES.index('phase19')
    assert verify.PHASES[rank - 1:rank + 1] == ('phase18', 'phase19')
    assert verify.SUITES['phase19'] == 'validate_phase19.py' and verify.SUITES['phase19-upgrade'] == 'validate_phase19_upgrade.py'
    own = [s for s in verify.SUITES if s.startswith('phase19')]
    assert all((ROOT / 'scripts' / verify.SUITES[s]).is_file() for s in own)
    assert not set(own) & set(verify.default_suites('phase18'))
    assert verify.default_suites('phase19') == verify.default_suites('phase18') + own
