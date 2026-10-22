"""Phase 19 B2: dashboards and host bindings (p19_dashboard_*).

A bound panel reads one of eight fixed, read-only, counts-only sources for the chat named by
the artifact registry row. Reads go through ArtifactInputs.decorate with a two-second cache and
store nothing; a native Refresh is the same read. Nothing here runs a model or opens a socket.
"""
import copy
import hashlib
import json
import re
import time
import uuid

import pytest

from pebble import view_bindings
from pebble.app import App
from pebble.artifact_inputs import model_result
from pebble.artifacts import validate
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError

CANARY = 'p19-dashboard-canary-' + 'beef' * 4
SOURCES = ('runs.recent', 'runs.usage', 'jobs.fires', 'agents.tree', 'artifacts.revisions', 'memory.stats',
           'git.summary', 'goals.progress')


@pytest.fixture
def app(tmp_path):
    events = []
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(copy.deepcopy(args)))
    value.model.complete = lambda *args, **kwargs: pytest.fail('No model inference in these tests.')
    value.events = events
    groups = value.store.setting('permissions', DEFAULT)['tools']
    value.setting('permissions', {'mode': 'ask', 'tools': dict.fromkeys(groups, True)})
    yield value
    value.active = value.active_run = None
    value.close()


class Owner:
    """A conversation's running owner, as the runner holds it while a tool executes."""

    def __init__(self, app, chat=None, goal='Render presentation data.'):
        self.app, self.chat = app, chat or app.new_chat()
        self.cid = self.chat['id']
        self.rid = app.runs.create(self.cid, goal, app.runtime_config(self.chat, False))
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

    def render(self, kind, data, title='Dashboard'):
        return self.tool('UIRender', title=title, kind=kind, data=data)


def dashboard(*bindings, inline=True):
    panels = [{'id': 'headline', 'title': 'Runs this week', 'kind': 'metric', 'data': {'value': 3, 'unit': 'runs'}}] if inline else []
    for index, (kind, binding) in enumerate(bindings):
        panels.append({'id': f'live{index}', 'title': 'Live counts', 'kind': kind, 'binding': binding})
    return {'columns': 2, 'panels': panels}


def finished_run(app, cid, state='completed', goal='Summarise ' + CANARY):
    rid = app.runs.create(cid, goal, app.runtime_config(app.chat(cid), False))
    app.runs.transition(rid, 'running')
    app.runs.transition(rid, state)
    return rid


def snapshot(app):
    with app.store.lock:
        names = [n for (n,) in app.store.db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                                    "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%'")]
        return {n: sorted(map(repr, app.store.db.execute(f'SELECT * FROM "{n}"').fetchall()))
                for n in names if not n.startswith('memory_search')}


def rows(resolved):
    return {r['item']: r['count'] for r in resolved['data']['rows']}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


# Allowlist and shape -------------------------------------------------------------------------------

def test_p19_dashboard_sources_are_exactly_eight_read_only_counts_only_sources():
    assert tuple(view_bindings.SOURCES) == SOURCES
    for name, source in view_bindings.SOURCES.items():
        assert set(source.kinds) <= {'metric', 'table', 'chart'}, name  # counts, never records
        assert not re.search(r'record|content|file|path|prompt|secret|credential|vault|message|text|token', name)
        assert set(view_bindings.FIELDS[name]) and all(re.fullmatch(r'[a-z_]+', f) for f in view_bindings.FIELDS[name])
    assert view_bindings.LAYERS == ('working', 'episodic', 'semantic', 'procedural', 'temporal', 'profile')


@pytest.mark.parametrize('binding', [
    {'source': 'memory.records'}, {'source': 'messages.recent'}, {'source': '../../etc/passwd'},
    {'source': '/Users/x/notes.md'}, {'source': 'RUNS.RECENT'}, {'source': 'runs.recent '}, {'source': ['runs.recent']},
    {'params': {}}, 'runs.recent', ['runs.recent'], None,
    {'source': 'runs.recent', 'params': {'chat_id': 'other'}}, {'source': 'runs.recent', 'params': {'path': '/etc/hosts'}},
    {'source': 'runs.recent', 'params': {'run_id': 'x'}}, {'source': 'runs.recent', 'params': []},
    {'source': 'runs.recent', 'params': {'limit': 10 ** 9}}, {'source': 'runs.recent', 'params': {'limit': 0}},
    {'source': 'runs.recent', 'params': {'limit': True}}, {'source': 'runs.recent', 'params': {'limit': 5.0}},
    {'source': 'jobs.fires', 'params': {'days': 91}}, {'source': 'runs.usage', 'params': {'limit': 3}},
    {'source': 'runs.recent', 'params': {'onload': 1}}, {'source': 'runs.recent', 'params': {'ｕｒｌ': 'x'}},
    {'source': 'runs.recent', 'url': 'https://evil.example/'}, {'source': 'runs.recent', 'chat_id': 'other'},
    {'source': 'runs.recent', 'ＳＴＹＬＥ': 'x'}, {'source': 'runs.recent', 'params': {'field': 'goal'}},
    {'source': 'runs.recent', 'params': {'field': 1}},
])
def test_p19_dashboard_bindings_to_records_paths_chats_or_extra_params_are_refused(app, binding):
    cid = app.new_chat()['id']
    with pytest.raises(ValueError):
        view_bindings.validate_binding(binding)
    with pytest.raises(ValueError):
        view_bindings.resolve(app, cid, binding)


def test_p19_dashboard_panel_kinds_must_suit_their_source():
    assert view_bindings.validate_binding({'source': 'git.summary'}, 'metric') == {'source': 'git.summary'}
    for kind in ('chart', 'plan', 'evidence', 'form', 'quiz'):
        with pytest.raises(ValueError):
            view_bindings.validate_binding({'source': 'git.summary'}, kind)
    # A metric may pick a counted field; other kinds never take one.
    assert view_bindings.validate_binding({'source': 'runs.recent', 'params': {'field': 'failed'}}, 'metric')
    with pytest.raises(ValueError):
        view_bindings.validate_binding({'source': 'runs.recent', 'params': {'field': 'failed'}}, 'table')


@pytest.mark.parametrize('change', [
    lambda d: d['panels'][0].update(binding={'source': 'runs.recent'}),  # both data and binding
    lambda d: d['panels'][0].pop('data'),  # neither
    lambda d: d.update(panels=[]), lambda d: d.update(panels=[dict(d['panels'][0], id=f'p{i}') for i in range(9)]),
    lambda d: d.update(columns=4), lambda d: d.update(columns=True), lambda d: d.update(caption='Top caption'),
    lambda d: d.update(title='x'), lambda d: d['panels'].append(copy.deepcopy(d['panels'][0])),  # duplicate panel ID
    lambda d: d['panels'][0].update(kind='form'), lambda d: d['panels'][0].update(kind='quiz'),
    lambda d: d['panels'][0].update(kind='html'), lambda d: d['panels'][0].update(style='color:red'),
    lambda d: d['panels'][0].update(ｓｔｙｌｅ='x'), lambda d: d['panels'][0].update(onclick='x()'),
    lambda d: d['panels'][0].update(id='__proto__'), lambda d: d['panels'][0].update(title='https://evil.example'),
    lambda d: d['panels'][0].update(title='<b>x</b>'), lambda d: d['panels'][0]['data'].update(value=True),
    lambda d: d['panels'][0]['data'].update(value='3'), lambda d: d['panels'][0]['data'].update(value=1e16),
    lambda d: d['panels'][0]['data'].update(delta='up'), lambda d: d['panels'][0]['data'].update(unit='x' * 25),
    lambda d: d['panels'][0]['data'].update(unit='javascript:x'), lambda d: d['panels'][0]['data'].update(trend='up'),
    lambda d: d['panels'][0].update(kind='table'),  # metric-shaped data under the table kind
    lambda d: d['panels'][1].update(binding={'source': 'memory.records'}),
    lambda d: d['panels'][1].update(binding={'source': 'runs.recent', 'params': {'chat_id': 'x'}}),
])
def test_p19_dashboard_shape_is_exact(change):
    value = dashboard(('table', {'source': 'runs.recent', 'params': {}}))
    assert validate('dashboard', json.dumps(value)) == value
    change(value)
    with pytest.raises(ValueError):
        validate('dashboard', json.dumps(value))


def test_p19_dashboard_inline_panels_reuse_their_kind_schema_and_cite_resolved_receipts(app):
    plan = {'steps': [{'id': 's1', 'title': 'Checked', 'status': 'completed', 'evidence_ids': ['1:read']}]}
    value = {'panels': [{'id': 'p', 'title': 'Plan', 'kind': 'plan', 'data': plan},
                        {'id': 'c', 'title': 'Trend', 'kind': 'chart', 'data': {'type': 'line', 'x_key': 'day', 'y_keys': ['n'],
                                                                              'rows': [{'day': 'Mon', 'n': 1}]}}]}
    with Owner(app) as owner:
        # The plan panel's receipt must be one of the owning run's resolved receipts, as for a plan view.
        with pytest.raises(PolicyError, match='resolved tool receipts'):
            owner.render('dashboard', value)
        assert not app.artifacts.list(owner.cid)


# Resolution: counts only, chat scoped, stamped ---------------------------------------------------

def seed(app, cid, other):
    for chat in (cid, other):
        finished_run(app, chat)
    finished_run(app, cid, 'failed')
    app.save_usage(cid, 'm1', {'input_tokens': 100, 'output_tokens': 20, 'cached_tokens': 40, 'note': CANARY})
    app.save_usage(cid, 'm2', {'input_tokens': None})
    app.save_usage(other, 'm3', {'input_tokens': 9000, 'output_tokens': 9000})
    app.memory_save(kind='semantic', content='Remember ' + CANARY + ' for the fixture.')
    now = time.time()
    with app.store.lock, app.store.db as db:
        for index, (chat, status, claimed) in enumerate(((cid, 'completed', now), (cid, 'failed', now),
                                                         (cid, 'completed', now - 30 * 86400), (other, 'completed', now))):
            db.execute("INSERT INTO job_fires(id,job_id,chat_id,revision,scheduled,missed,origin,status,pin_fingerprint,"
                       "claimed,updated) VALUES(?,?,?,1,?,0,'host',?,'fp',?,?)",
                       (uuid.uuid4().hex, f'job{index}', chat, claimed, status, claimed, claimed))
        rid = db.execute('SELECT id FROM runs WHERE chat_id=? LIMIT 1', (cid,)).fetchone()[0]
        db.execute("INSERT INTO agent_nodes(id,run_id,name,task,context,tools,limits,dependencies,write_paths,depth,"
                   "generation,status,evidence_ids,created,updated) VALUES(?,?,?,?,'[]','[]','{}','[]','[]',1,1,'completed','[]',?,?)",
                   (uuid.uuid4().hex, rid, 'helper ' + CANARY, 'Task ' + CANARY, now, now))
        goal = uuid.uuid4().hex
        db.execute("INSERT INTO goals VALUES(?,?,'human',?,1,'active',?,?)", (goal, cid, 'Goal ' + CANARY, now, now))
        for ident, status in (('c1', 'met'), ('c2', 'open'), ('c3', 'candidate')):
            db.execute("INSERT INTO goal_criteria VALUES(?,?,1,?,'human_check',?,'human','[]',NULL,NULL,?)",
                       (goal, ident, 'Criterion ' + CANARY, status, now))
        for ident, status in (('s1', 'completed'), ('s2', 'pending')):
            db.execute("INSERT INTO goal_steps VALUES(?,?,1,?,?,'[]','[]','[]',NULL,NULL,NULL,?)", (goal, ident, 'Step ' + CANARY, status, now))


def test_p19_dashboard_every_source_resolves_counts_only_for_this_chat_with_a_stamp(app):
    cid, other = app.new_chat()['id'], app.new_chat()['id']
    seed(app, cid, other)
    with Owner(app, app.chat(cid)) as owner:
        owner.render('table', {'columns': [{'id': 'a', 'label': CANARY}], 'rows': [{'a': CANARY}]}, title='Notes')
    for name, source in view_bindings.SOURCES.items():
        for kind in source.kinds:
            resolved = view_bindings.resolve(app, cid, {'source': name, 'params': {}}, kind)
            text = compact(resolved)
            assert CANARY not in text and other not in text and cid not in text, (name, kind)
            assert set(resolved) == {'data', 'stamp'} and set(resolved['stamp']) == {'source', 'as_of', 'digest'}
            assert resolved['stamp']['source'] == name and isinstance(resolved['stamp']['as_of'], float)
            digest = hashlib.sha256(json.dumps(resolved['data'], ensure_ascii=False, sort_keys=True,
                                               separators=(',', ':')).encode()).hexdigest()
            assert resolved['stamp']['digest'] == digest
    # The table shape is the default and holds host labels with counts.
    recent = view_bindings.resolve(app, cid, {'source': 'runs.recent'})
    assert recent['data']['columns'] == [{'id': 'item', 'label': 'Item'}, {'id': 'count', 'label': 'Count'}]
    assert rows(recent) == {'Completed': 1, 'Failed': 1, 'Running': 1}  # the owner above is still running
    assert rows(view_bindings.resolve(app, cid, {'source': 'runs.recent', 'params': {'limit': 1}})) == {'Running': 1}
    assert rows(view_bindings.resolve(app, cid, {'source': 'runs.usage'})) == {
        'Input tokens': 100, 'Output tokens': 20, 'Cached input tokens': 40, 'Replies without usage': 1}
    assert rows(view_bindings.resolve(app, cid, {'source': 'jobs.fires'})) == {'Completed': 1, 'Failed': 1}
    assert rows(view_bindings.resolve(app, cid, {'source': 'jobs.fires', 'params': {'days': 90}})) == {'Completed': 2, 'Failed': 1}
    assert rows(view_bindings.resolve(app, cid, {'source': 'agents.tree'})) == {'Completed': 1}
    assert rows(view_bindings.resolve(app, cid, {'source': 'artifacts.revisions'})) == {'Table views': 1}
    assert rows(view_bindings.resolve(app, cid, {'source': 'goals.progress'})) == {
        'Criteria met': 1, 'Criteria open': 1, 'Criteria unmet': 0, 'Steps completed': 1, 'Steps': 2}
    metric = view_bindings.resolve(app, cid, {'source': 'runs.usage', 'params': {'field': 'output_tokens'}}, 'metric')['data']
    assert metric['value'] == 20 and metric['unit'] == 'tokens' and set(metric) == {'value', 'unit', 'caption'}
    assert view_bindings.resolve(app, cid, {'source': 'artifacts.revisions', 'params': {'field': 'revisions'}}, 'metric')['data']['value'] == 1
    chart = view_bindings.resolve(app, cid, {'source': 'agents.tree'}, 'chart')['data']
    assert chart['type'] == 'bar' and chart['x_key'] == 'item' and chart['y_keys'] == ['count']
    # An unknown chat counts nothing, and a chat is never taken from a binding.
    for chat in (other + ' OR 1=1', uuid.uuid4().hex):
        assert view_bindings.resolve(app, chat, {'source': 'runs.recent'})['data']['rows'] == []
    for chat in (None, '', 7):
        with pytest.raises(ValueError):
            view_bindings.resolve(app, chat, {'source': 'runs.recent'})


def test_p19_dashboard_memory_counts_follow_the_six_layers_and_switches(app):
    cid = app.new_chat()['id']
    app.memory_save(kind='semantic', content='Remember ' + CANARY + ' for the fixture.')
    app.memory_save(kind='profile', content='Prefers short answers ' + CANARY)
    with app.store.lock:
        expected = dict(app.store.db.execute("SELECT kind,count(*) FROM memories WHERE scope IN ('global','chat:'||?) "
                                             'GROUP BY kind', (cid,)).fetchall())
    counted = rows(view_bindings.resolve(app, cid, {'source': 'memory.stats'}))
    assert counted == {kind.capitalize(): expected.get(kind, 0) for kind in view_bindings.LAYERS}
    app.setting('memory_layers', {kind: kind != 'profile' for kind in view_bindings.LAYERS})
    assert 'Profile' not in rows(view_bindings.resolve(app, cid, {'source': 'memory.stats'}))  # a disabled layer is not read
    app.setting('memory_enabled', False)
    off = view_bindings.resolve(app, cid, {'source': 'memory.stats'})
    assert off['data']['rows'] == [] and 'Memory is off.' in off['data']['caption']
    assert view_bindings.resolve(app, cid, {'source': 'memory.stats'}, 'metric')['data']['value'] == 0


def test_p19_dashboard_git_counts_need_a_project_and_never_echo_a_failure(app, tmp_path, monkeypatch):
    cid = app.new_chat()['id']
    empty = view_bindings.resolve(app, cid, {'source': 'git.summary'})
    assert empty['data']['rows'] == [] and 'No project folder' in empty['data']['caption']
    project = tmp_path / ('project-' + CANARY)
    project.mkdir()
    app.set_workspace(cid, str(project))
    seen = []

    def status(chat_id, offset=0, limit=100, cancel=None):
        seen.append((chat_id, offset, limit))
        return {'counts': {'staged': 1, 'unstaged': 2, 'untracked': 3, 'unmerged': 0}, 'ahead': 4, 'behind': None,
                'entries': [{'path': 'secret-' + CANARY}], 'root': str(project)}
    monkeypatch.setattr(app.git, 'status', status)
    resolved = view_bindings.resolve(app, cid, {'source': 'git.summary'})
    assert rows(resolved) == {'Staged': 1, 'Unstaged': 2, 'Untracked': 3, 'Unmerged': 0, 'Ahead': 4}
    assert CANARY not in compact(resolved) and seen == [(cid, 0, 1)]
    assert view_bindings.resolve(app, cid, {'source': 'git.summary'}, 'metric')['data']['value'] == 6

    def failing(*args, **kwargs):
        raise PolicyError('Git failed in ' + str(project))
    monkeypatch.setattr(app.git, 'status', failing)
    with pytest.raises(view_bindings.Unavailable):
        view_bindings.resolve(app, cid, {'source': 'git.summary'})
    entries = view_bindings.resolve_panels(app, cid, dashboard(('table', {'source': 'git.summary'})))
    assert entries == [{'panel_id': 'live0', 'source': 'git.summary', 'error': view_bindings.UNAVAILABLE}]
    assert CANARY not in compact(entries)


# The read path: decorate, registry scope, cache, nothing stored ----------------------------------

def test_p19_dashboard_reads_resolve_through_decorate_with_registry_scope_and_store_nothing(app, monkeypatch):
    cid, other = app.new_chat()['id'], app.new_chat()['id']
    finished_run(app, other)
    finished_run(app, other)
    with Owner(app, app.chat(cid)) as owner:
        result = owner.render('dashboard', dashboard(('table', {'source': 'runs.recent', 'params': {}}),
                                                     ('metric', {'source': 'runs.recent', 'params': {'field': 'running'}})))
    # Merge (i19): with parent patch P6 the owning run's tool result is model_result (plus execute's receipt_id):
    # panel identifiers, never the view data or host counts. The stored row is what a window reads.
    row = app.artifacts.get(cid, result['id'])
    assert 'bindings' not in row and row['data']['panels'][1] == {
        'id': 'live0', 'title': 'Live counts', 'kind': 'table', 'binding': {'source': 'runs.recent', 'params': {}}}
    assert {k: v for k, v in result.items() if k != 'receipt_id'} == model_result(row)
    assert 'bindings' not in result and 'data' not in result
    assert model_result(row) == {
        'id': row['id'], 'revision': 1, 'kind': 'dashboard', 'title': 'Dashboard', 'status': 'ready', 'evidence_ids': [],
        'data_hash': row['data_hash'], 'record_hash': row['record_hash'],
        'panels': [{'id': 'headline', 'kind': 'metric'}, {'id': 'live0', 'kind': 'table', 'source': 'runs.recent'},
                   {'id': 'live1', 'kind': 'metric', 'source': 'runs.recent'}],
        'message': 'Dashboard saved. Bound panels show live host counts to the person only; they are not sent to you.'}
    clock, calls = [100.0], []
    app.artifact_inputs.cache = view_bindings.Cache(clock=lambda: clock[0])
    resolve = view_bindings.resolve
    monkeypatch.setattr(view_bindings, 'resolve', lambda *args: calls.append(args[1]) or resolve(*args))
    before = snapshot(app)
    current = app.artifact_get(cid, row['id'])
    live = current['bindings']
    assert [(e['panel_id'], e['source']) for e in live] == [('live0', 'runs.recent'), ('live1', 'runs.recent')]
    assert all(set(e) == {'panel_id', 'source', 'as_of', 'digest', 'data'} for e in live)
    assert live[0]['data']['rows'] == [{'item': 'Running', 'count': 1}] and live[1]['data']['value'] == 1
    assert calls == [cid, cid]  # scope comes from the registry row: never the other chat's two runs
    # Reads within two seconds share one resolution; after that the host counts are read again.
    assert app.artifact_get(cid, row['id'])['bindings'] == live and len(calls) == 2
    clock[0] += view_bindings.CACHE_SECONDS
    assert app.artifact_get(cid, row['id'])['bindings'][0]['as_of'] >= live[0]['as_of'] and len(calls) == 4
    assert snapshot(app) == before  # nothing is stored per read
    # The chat state decorates through the same path and cache.
    assert app.chat(cid)['artifacts'][0]['bindings'][0]['data'] == live[0]['data'] and len(calls) == 4
    # A row claiming another chat resolves nothing; the registry decides.
    assert app.artifact_inputs.decorate({**current, 'chat_id': other})['bindings'] == []
    with pytest.raises(ValueError):
        app.artifact_get(other, row['id'])
    # Stopped and historical revisions show no live host data.
    app.artifact_stop(cid, row['id'], 1)
    assert app.artifact_get(cid, row['id'])['bindings'] == []
    assert [h['bindings'] for h in app.dispatch('artifact_history', {'chat_id': cid, 'id': row['id']})] == [[], []]


def test_p19_dashboard_refresh_is_a_cached_read_that_moves_no_revision(app):
    with Owner(app) as owner:
        row = owner.render('dashboard', dashboard(('table', {'source': 'runs.recent', 'params': {}})))
        static = owner.render('dashboard', dashboard())
        table = owner.render('table', {'columns': [{'id': 'a', 'label': 'A'}], 'rows': [{'a': 1}]})
        bound = owner.render('dashboard', dashboard(('metric', {'source': 'runs.recent'})))
        # Merge (i19): the tool result is the model view (P6); compare with the stored view a window reads.
        stored = app.artifacts.get(owner.cid, row['id'])
        before = snapshot(app)
        refreshed = app.artifact_inputs.refresh(owner.cid, row['id'], 1)
        assert snapshot(app) == before
        assert (refreshed['revision'], refreshed['owner_run_id'], refreshed['data']) == (1, owner.rid, stored['data'])
        assert [e['panel_id'] for e in refreshed['bindings']] == ['live0'] and 'refreshed_by' not in refreshed
        # The person's Refresh never changes the revision the owning run names in its update.
        assert owner.tool('UIUpdate', id=row['id'], revision=1, title='Runs', kind='dashboard', data=dashboard())['revision'] == 2
    for chat, ident, version in ((owner.cid, row['id'], 1), (owner.cid, row['id'], 3), (owner.cid, row['id'], '2'),
                                 (app.new_chat()['id'], row['id'], 2), (owner.cid, row['id'], 2),  # now static
                                 (owner.cid, static['id'], 1), (owner.cid, table['id'], 1), (owner.cid, 'f' * 32, 1)):
        with pytest.raises(ValueError):
            app.artifact_inputs.refresh(chat, ident, version)
    assert app.artifact_inputs.refresh(owner.cid, bound['id'], 1)['bindings'][0]['data']['value'] == 1
    app.artifact_stop(owner.cid, bound['id'], 1)
    with pytest.raises(ValueError, match='no longer active'):
        app.artifact_inputs.refresh(owner.cid, bound['id'], 2)


def test_p19_dashboard_cache_is_bounded_and_forgets_a_chat():
    clock = [0.0]
    cache = view_bindings.Cache(clock=lambda: clock[0], entries=3)
    for index in range(5):
        assert cache.get(('chat', 'table', str(index)), lambda i=index: i) == index
    assert len(cache.items) == 3
    assert cache.get(('chat', 'table', '4'), lambda: 'fresh') == 4
    clock[0] = 2.5
    assert cache.get(('chat', 'table', '4'), lambda: 'fresh') == 'fresh'
    cache.put(('other', 'table', 'x'), 1)
    cache.clear('chat')
    assert set(cache.items) == {('other', 'table', 'x')}


def test_p19_dashboard_exports_inline_panels_and_leaves_live_host_data_out(app):
    value = dashboard(('table', {'source': 'runs.recent', 'params': {}}))
    value['panels'].append({'id': 'table', 'title': 'Owners', 'kind': 'table',
                            'data': {'columns': [{'id': 'name', 'label': 'Name'}], 'rows': [{'name': 'Ada'}]}})
    with Owner(app) as owner:
        row = owner.render('dashboard', value, title='Release check')
    resolved = app.exports._resolve(owner.cid, {'kind': 'artifact', 'id': row['id']}, {'include_answers': False, 'paper': 'a4'})
    blocks = resolved['blocks']
    assert [b['text'] for b in blocks if b['type'] == 'heading'] == ['Release check', 'Runs this week', 'Live counts', 'Owners']
    tables = [b for b in blocks if b['type'] == 'table']
    assert tables[0]['columns'] == ['Value', 'Unit', 'Change'] and tables[0]['rows'] == [[3, 'runs', None]]
    assert tables[1]['rows'] == [['Ada']] and len(tables) == 2
    assert any('Live host data (runs.recent)' in b.get('text', '') for b in blocks)
    assert any('1 live panel(s) were left out' in note for note in resolved['notes'])
    assert app.exports.preview(owner.cid, {'kind': 'artifact', 'id': row['id']}, 'html')['source']['revision'] == 1
