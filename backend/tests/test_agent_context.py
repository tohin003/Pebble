"""Small-route agent tool discovery through the production root request path.

The App root loop, Agents scheduler and tree, durable waits, native steering,
GuardAdapter and Providers projection are the checked-in code. Requests use
the production assembler, compaction transform and bundled o200k counter on a
4K route (3072 input after the 1024-token output reserve). Only the provider
stream bytes and the child transport are scripted: no model HTTP, network,
credential, shell or plugin operation occurs. These tests check offered
schemas, protected context and fit. They do not assess model quality.
"""

import copy
import hashlib
import itertools
import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble.agent_manager import NOTE_PREFIX, Agents
from pebble.app import App
from pebble.delegation_policy import schema_note
from pebble.policy import Workspace
from pebble.providers import Providers
from pebble.runner import AGENT_BRIEF, SMALL_BRIEF
from pebble.token_budget import TokenBudget
from pebble.tool_catalogue import AGENT_ROOT_TOOLS, TOOLS


RESOURCES = Path(__file__).resolve().parents[2] / 'src-tauri' / 'resources'
ROUTE = {'id': 'route-4k', 'kind': 'openai', 'name': '4K fixture route', 'url': 'https://unused.invalid/v1',
         'model': 'fixture-4k', 'context': 4096, 'budget': 4096, 'budget_version': 2,
         'context_mode': 'full', 'tools': True}
LIMITS = {'context': 3072, 'output_reserve': 64, 'input': 3008}
GOAL = 'Delegate a read-only review of calc.py to a child agent, then report its findings.'
STEER = 'Keep the delegated review read-only and cite its receipt IDs.'
SOURCE = 'def add(a, b):\n    return a + b\n'
QUESTIONS = [{'id': 'scope', 'title': 'Which source should I review?', 'options': [{'id': 'calc', 'label': 'calc.py'}]}]
ANSWER = {'scope': {'option_id': 'calc'}}
OWNER = 'stage-fixture-owner'
# Non-agent schemas for this explicit read-only goal: reads and ranked
# SearchFiles, ordinary questions, plan, evidence and tests. File effects stay
# natively granted but are not shown; any call naming one fails schema admission.
BASE = {'AskUserQuestion', 'UpdatePlan', 'ReadRunEvidence', 'Glob', 'Read', 'Grep', 'SearchFiles', 'RunTests'}
# The shared catalogue as imported; offered 4K copies never change it.
CATALOGUE = copy.deepcopy(TOOLS)
SPECS = {t['function']['name']: t['function'] for t in CATALOGUE}
DEFAULT_NOTE = schema_note(64)  # RunLimits default max_steps
EFFECTS = {'Write', 'Append', 'Edit', 'RunCommand'}
REJECTED = 'does not match its allowed schema'
# Per stage: discovery tools that must be offered, and tools that must not be.
STAGES = {
    'idle': ({'ReadAgentTree', 'SpawnAgent'}, AGENT_ROOT_TOOLS - {'ReadAgentTree', 'SpawnAgent'}),
    'active': ({'ReadAgentTree', 'WaitForAgents'},
               {'AnswerAgentQuestion', 'PromoteAgentQuestion', 'ReviewAgentResult', 'IntegrateAgentPatch', 'ReadAgentPatch', 'RetryAgent'}),
    'questions': ({'ReadAgentTree', 'AnswerAgentQuestion', 'PromoteAgentQuestion'},
                  {'ReviewAgentResult', 'IntegrateAgentPatch', 'RetryAgent'}),
    'review': ({'ReadAgentTree', 'ReviewAgentResult', 'ReadAgentPatch'},
               {'AnswerAgentQuestion', 'PromoteAgentQuestion', 'IntegrateAgentPatch'}),
    'patches': ({'ReadAgentTree', 'ReadAgentPatch', 'IntegrateAgentPatch'},
                {'AnswerAgentQuestion', 'PromoteAgentQuestion', 'ReviewAgentResult'}),
}


class Stream:
    """The OpenAI-compatible SSE lines a provider would return."""

    def __init__(self, events):
        self.lines = [b'data: ' + json.dumps(e).encode() + b'\n' for e in events] + [b'data: [DONE]\n']

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self.lines)


def tools_call(*calls):
    return [{'choices': [{'delta': {'tool_calls': [
        {'index': i, 'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
        for i, (name, args, ident) in enumerate(calls)]}, 'finish_reason': 'tool_calls'}]}]


def text(content):
    return [{'choices': [{'delta': {'content': content}, 'finish_reason': 'stop'}]}]


class ChildProvider:
    """A pinned child transport. Gates order child progress against root waits."""

    def __init__(self, env, app, profile, owner_id, lane, observer=None):
        self.env, self.lane, self.owner_id = env, lane, owner_id
        self.profile = copy.deepcopy(profile)
        self.last_usage = {}
        self.stopped = threading.Event()

    def complete(self, messages, tools, cancel, token, effort):
        # Keyed by committed tool results, so a re-driven generation replays safely.
        results = [m for m in messages if m['role'] == 'tool']
        if not results:
            self.wait(self.env.ask, cancel)
            response = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'ask', 'type': 'function',
                'function': {'name': 'AskParentQuestion', 'arguments': json.dumps({'questions': json.dumps(QUESTIONS)})}}]}
        elif len(results) == 1:
            self.wait(self.env.read, cancel)
            response = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'read', 'type': 'function',
                'function': {'name': 'Read', 'arguments': json.dumps({'file_path': 'calc.py'})}}]}
        else:
            response = {'role': 'assistant', 'content': 'calc.py defines add(a, b), returning a + b.'}
        with self.lane.admit(self.owner_id, self.profile, cancel):
            return response

    def wait(self, gate, cancel):
        while not gate.wait(.05):
            if cancel.is_set() or self.stopped.is_set():
                raise InterruptedError('Fixture child stopped.')

    def cancel(self):
        self.stopped.set()
        self.lane.wake()

    def close(self):
        self.stopped.set()
        self.lane.wake()


def make_route(tmp_path, monkeypatch, scheduler):
    assert (RESOURCES / 'tokenizer').is_dir(), 'The bundled tokenizer cache keeps o200k counting offline.'
    TokenBudget(RESOURCES)
    env = SimpleNamespace(requests=[], manifests=[], script=None, app=None, cid=None, rid=None,
                          ask=threading.Event(), read=threading.Event(), lock=threading.Lock())

    def request(provider, url, headers, body=None, timeout=30):
        assert url == ROUTE['url'] + '/chat/completions' and 'Authorization' not in headers
        with env.lock:
            index = len(env.requests)
            # Each durable continuation runs on a newly launched root worker.
            env.requests.append({'body': copy.deepcopy(body), 'manifest': copy.deepcopy(env.manifests[-1]),
                                 'thread': threading.current_thread()})
        return Stream(env.script(env, index))

    monkeypatch.setattr(Providers, 'request', request)
    if not scheduler:
        # Stage fixtures claim their own nodes; no worker may drive them.
        monkeypatch.setattr(Agents, 'start', lambda self: None)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text(SOURCE)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    app.store.set_setting('model_profiles', [dict(ROUTE)])
    app.store.set_setting('active_profile', ROUTE['id'])
    app.agents.provider_factory = lambda *args, **kwargs: ChildProvider(env, *args, **kwargs)
    observe = app.model.context_observer

    def observer(manifest):
        with env.lock:
            env.manifests.append(copy.deepcopy(manifest))
        observe(manifest)

    app.model.context_observer = observer
    env.app, env.project = app, project
    chat = app.new_chat(str(project))
    env.chat, env.cid = chat, chat['id']
    return env


@pytest.fixture
def route(tmp_path, monkeypatch):
    env = make_route(tmp_path, monkeypatch, scheduler=True)
    yield env
    finish(env)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    env = make_route(tmp_path, monkeypatch, scheduler=False)
    yield env
    finish(env)


def finish(env):
    env.ask.set()
    env.read.set()
    env.app.active = env.app.active_run = None
    env.app.close()
    assert not env.app.agents.busy(), 'Teardown must join every child worker.'


def diagnosis(env):
    run = env.app.runs.get(env.rid)
    last = env.manifests[-1] if env.manifests else {}
    return {'state': run['state'], 'error': run['error'], 'requests': len(env.requests),
            'last_projection': {'status': last.get('status'), 'input_tokens': last.get('input_tokens'),
                                'tools': (last.get('tools') or {}).get('count')}}


def until(env, predicate, label, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        if env.app.runs.get(env.rid)['state'] in {'failed', 'budget_exhausted', 'stopped', 'needs_reconciliation'}:
            break
        time.sleep(.02)
    pytest.fail(label + ': ' + json.dumps(diagnosis(env)))


def exact_schemas(record):
    """Compact 4K text on copies only: every admission constraint stays exact,
    each description is no longer than the catalogue's, the SpawnAgent budget
    note appears once, and the shared catalogue is unchanged."""
    for spec in record['body']['tools']:
        fn = spec['function']
        expected = copy.deepcopy(SPECS[fn['name']]['parameters'])
        if fn['name'] in AGENT_BRIEF or fn['name'] in SMALL_BRIEF:
            for field in expected.get('properties', {}).values():
                if isinstance(field, dict):
                    field.pop('description', None)
        assert fn['parameters'] == expected, fn['name']
        assert len(fn['description']) <= len(SPECS[fn['name']]['description']), fn['name']
        if fn['name'] == 'SpawnAgent':
            assert fn['description'].count(NOTE_PREFIX) == 1 and fn['description'].endswith(DEFAULT_NOTE)
    assert TOOLS == CATALOGUE


def check(record, stage):
    """One captured provider request: stage schemas, grants and 4K fit."""
    body, manifest = record['body'], record['manifest']
    names = [t['function']['name'] for t in body['tools']]
    offered = set(names)
    required, forbidden = STAGES[stage]
    assert len(names) == len(offered)
    base = BASE - {'Glob', 'Grep'} if stage == 'review' else BASE
    assert offered - AGENT_ROOT_TOOLS == base and not offered & EFFECTS, names
    exact_schemas(record)
    assert required <= offered and not offered & forbidden, (stage, sorted(offered & AGENT_ROOT_TOOLS))
    assert body['max_tokens'] == 1024
    assert manifest['limits'] == LIMITS and manifest['dispatchable'], manifest['input_tokens']
    assert manifest['input_tokens']['after'] <= LIMITS['input']
    assert manifest['estimation']['window_method'] == 'o200k_local_estimate'
    assert manifest['tools']['count'] == len(names)
    assert all(unit['selected'] for unit in manifest['units'] if unit['protected'])
    goal = [unit for unit in manifest['units'] if unit['category'] == 'goal']
    assert len(goal) == 1 and goal[0]['protected'] and goal[0]['selected']
    assert [m['content'] for m in body['messages'] if m['role'] == 'user' and m['content'] == GOAL] == [GOAL]
    return offered & AGENT_ROOT_TOOLS


def tool_result(record, ident):
    [message] = [m for m in record['body']['messages'] if m['role'] == 'tool' and m['tool_call_id'] == ident]
    return json.loads(message['content'])


def native_grants_intact(app, rid):
    """Schema narrowing never edits the native or pinned run permission snapshot."""
    native, pinned = app.store.setting('permissions'), app.runs.get(rid)['config']['permissions']
    assert native == pinned and native['mode'] == 'ask'
    assert all(native['tools'][group] for group in ('files', 'edits', 'tests', 'terminal'))


def test_small_route_keeps_latest_source_and_question_without_delegation(route):
    """Discovery schemas must fit ordinary 4K tasks as well as agent stages."""
    app = route.app
    goal = 'Inspect the fixture files, then ask me for scope. Preserve 漢字. Never publish anything.'
    source = ''.join(f'old-source-0-{i}: {hashlib.sha256(str(i).encode()).hexdigest()[:31]}\n'
                     for i in range(40))
    (route.project / 'large.txt').write_text(source)

    def script(env, index):
        if index == 0:
            return tools_call(('Read', {'file_path': 'large.txt', 'limit': 40}, 'large-read'))
        assert index == 1, diagnosis(env)
        return tools_call(('AskUserQuestion', {'questions': json.dumps(QUESTIONS), 'required': True}, 'scope'))

    route.script = script
    route.rid = app.runs.create(route.cid, goal, app.runtime_config(route.chat, False), [])
    app.work(route.chat, goal, False, [], run_id=route.rid)
    until(route, lambda: app.runs.get(route.rid)['state'] == 'waiting_input' and app.active is None,
          'Ordinary source inspection did not retain room for its question')
    assert len(route.requests) == 2
    for record in route.requests:
        names = {t['function']['name'] for t in record['body']['tools']}
        assert 'SearchFiles' in names and not names & AGENT_ROOT_TOOLS
        exact_schemas(record)
        manifest = record['manifest']
        assert manifest['limits'] == LIMITS and manifest['dispatchable']
        assert manifest['input_tokens']['after'] <= LIMITS['input']
        assert all(u['selected'] for u in manifest['units'] if u['protected'])
        assert record['body']['max_tokens'] == 1024
        assert any(m['role'] == 'user' and m['content'] == goal for m in record['body']['messages'])
    result = tool_result(route.requests[1], 'large-read')
    assert result['untrusted_source'].startswith('old-source-0-0: ')
    assert result['file_hash'] == hashlib.sha256(source.encode()).hexdigest()
    assert not result['excerpt_complete'] and result['next_char_offset'] > 0
    native_grants_intact(app, route.rid)


def test_small_route_local_source_search_pages_without_web_schemas(route):
    app = route.app
    goal = 'Find every rare-search-target occurrence using paged source search.'
    for number in range(3):
        (route.project / f'target-{number}.txt').write_text('rare-search-target\n')
    seen = []

    def script(env, index):
        cursor = None
        if index:
            [last] = [m for m in env.requests[index]['body']['messages']
                      if m['role'] == 'tool' and m['tool_call_id'] == f'page-{index-1}']
            page = json.loads(last['content'])
            seen.extend(row['path'] for row in page['untrusted_matches'])
            cursor = page['cursor']
            if cursor is None:
                return text('Found the three source matches.')
        return tools_call(('Grep', {'pattern': 'rare-search-target', 'limit': 1,
                                   **({'cursor': cursor} if cursor else {})}, f'page-{index}'))

    route.script = script
    route.rid = app.runs.create(route.cid, goal, app.runtime_config(route.chat, False), [])
    app.work(route.chat, goal, False, [], run_id=route.rid)
    until(route, lambda: app.runs.get(route.rid)['state'] == 'completed' and app.active is None,
          'Local paged search did not fit the ordinary 4K route')
    assert seen == [f'target-{number}.txt' for number in range(3)]
    assert len(route.requests) == 4
    for record in route.requests:
        names = {t['function']['name'] for t in record['body']['tools']}
        assert {'SearchFiles', 'Grep', 'Read'} <= names
        assert not names & {'WebSearch', 'ReadURL', 'Weather'}
        exact_schemas(record)
        assert record['manifest']['dispatchable']
        assert record['manifest']['input_tokens']['after'] <= LIMITS['input']
    native_grants_intact(app, route.rid)


def test_small_route_explicit_web_research_keeps_web_schemas(staged):
    app = staged.app
    goal = 'Research matching occurrences in web sources.'
    staged.script = lambda env, index: text('Reported the research scope.')
    staged.rid = app.runs.create(staged.cid, goal, app.runtime_config(staged.chat, False), [])
    app.work(staged.chat, goal, False, [], run_id=staged.rid)
    [record] = staged.requests
    names = {t['function']['name'] for t in record['body']['tools']}
    assert {'WebSearch', 'ReadURL', 'Weather'} <= names
    exact_schemas(record)
    assert record['manifest']['dispatchable']
    native_grants_intact(app, staged.rid)


def agent_state(app, cid, rid):
    rows = app.store.query('SELECT id,status,review,generation FROM agent_nodes WHERE run_id=? ORDER BY id', (rid,))
    questions = app.store.query('SELECT id,status FROM agent_parent_questions WHERE run_id=? ORDER BY id', (rid,))
    patches = app.store.query('SELECT id,status FROM agent_patches WHERE run_id=? ORDER BY id', (rid,))
    return rows, questions, patches


def stage_tree(env, stage):
    """Reach a stage through the real tree and child tool APIs, without a worker."""
    app, cid, rid = env.app, env.cid, env.rid
    if stage == 'idle':
        return None
    tree = app.agents.tree
    node = tree.spawn(cid, rid, None, 'Review calc.py read-only and report findings.', 'reviewer',
                      ['Read', 'AskParentQuestion', 'ProposePatch'], {},
                      write_paths=['calc.py'] if stage == 'patches' else [])
    ident = node['id']
    tree.claim(cid, rid, ident, 1, OWNER)
    if stage == 'questions':
        tree.ask_parent(cid, rid, ident, 1, OWNER, '1:ask', QUESTIONS)
    if stage in {'review', 'patches'}:
        worker = {'cid': cid, 'rid': rid, 'id': ident, 'generation': 1, 'owner': OWNER,
                  'cancel': threading.Event(), 'workspace': Workspace(env.project)}
        for call_id, name, args in [('1:read', 'Read', {'file_path': 'calc.py'}),
                                    ('2:patch', 'ProposePatch', {'file_path': 'calc.py', 'content': SOURCE + '\n'})][:1 + (stage == 'patches')]:
            tree.begin_tool(cid, rid, ident, 1, OWNER, call_id, name, args)
            tree.end_tool(cid, rid, ident, 1, OWNER, call_id, app.agents.child_tool(worker, name, args, call_id))
        tree.settle(cid, rid, ident, 1, 'completed', {'text': 'calc.py defines add.'}, ['1:read'], OWNER)
    if stage == 'patches':
        tree.review(cid, rid, rid, ident, 1, ['1:read'])
    return ident


UNOFFERED = {
    'idle': lambda child: ('ReviewAgentResult', {'agent_id': 'missing', 'generation': 1}),
    'active': lambda child: ('AnswerAgentQuestion', {'question_id': 'missing', 'answers': '{}'}),
    'questions': lambda child: ('ReviewAgentResult', {'agent_id': child, 'generation': 1}),
    'review': lambda child: ('IntegrateAgentPatch', {'patch_id': 'missing'}),
    'patches': lambda child: ('AnswerAgentQuestion', {'question_id': 'missing', 'answers': '{}'}),
}


@pytest.mark.parametrize('stage', list(STAGES))
def test_small_route_offers_stage_tools_that_fit_and_rejects_unoffered_calls(staged, stage):
    app = staged.app
    staged.rid = app.runs.create(staged.cid, GOAL, app.runtime_config(staged.chat, False), [])
    app.runs.transition(staged.rid, 'running')
    child = stage_tree(staged, stage)
    before = agent_state(app, staged.cid, staged.rid)
    name, args = UNOFFERED[stage](child)
    assert name in STAGES[stage][1]
    staged.script = lambda env, index: tools_call(
        (name, args, 'unoffered'), ('Write', {'file_path': 'calc.py', 'content': 'changed = True\n'}, 'unoffered-write')
    ) if index == 0 else text('Reported the delegated state.')
    app.work(staged.chat, GOAL, False, [], run_id=staged.rid)
    state = app.runs.get(staged.rid)['state']
    assert state not in {'failed', 'budget_exhausted'}, diagnosis(staged)
    assert len(staged.requests) >= 2, diagnosis(staged)
    offered = [check(record, stage) for record in staged.requests]
    assert all(tools == set(app.agents.root_tools(staged.cid, staged.rid)) for tools in offered)
    # Unoffered coordination and file-effect schemas fail admission at the
    # model boundary with no effect, while the native grants stay intact.
    for ident in ('unoffered', 'unoffered-write'):
        assert REJECTED in tool_result(staged.requests[1], ident)['error']
        assert app.store.query('SELECT state FROM run_tools WHERE run_id=? AND call_id=?',
                               (staged.rid, '1:' + ident)) == [{'state': 'planned'}]
    assert agent_state(app, staged.cid, staged.rid) == before
    assert (staged.project / 'calc.py').read_text() == SOURCE and not app.store.actions(staged.cid)
    native_grants_intact(app, staged.rid)


def test_small_route_budget_exhaustion_settles_before_a_staged_provider_request(staged):
    app = staged.app
    config = app.runtime_config(staged.chat, False, {'max_steps': 1, 'max_seconds': 180})
    staged.rid = app.runs.create(staged.cid, GOAL, config, [])
    app.runs.transition(staged.rid, 'running')
    app.agents.tree.budget(staged.cid, staged.rid)
    for index in range(3):
        dispatch = 'spent-' + str(index)
        assert app.agents.tree.reserve_model(staged.cid, staged.rid, staged.rid, 0, 4096, 0, dispatch)['admitted']
        app.agents.tree.finish_model(staged.cid, staged.rid, staged.rid, 0, dispatch, failed=True)
    staged.script = lambda env, index: pytest.fail('An exhausted tree must not reach the provider.')
    app.work(staged.chat, GOAL, False, [], run_id=staged.rid)
    run = app.runs.get(staged.rid)
    assert run['state'] == 'budget_exhausted' and 'model budget' in run['error']
    assert staged.requests == [] and staged.manifests == []


@pytest.mark.parametrize('goal,effects', [
    (GOAL, False),
    ('Use a child agent to review calc.py without editing files.', False),
    ("Don't modify anything; delegate a review of calc.py to an agent.", False),
    ('Delegate a review of calc.py to a child agent, then fix any bug it finds.', True),
    ('Delegate a read-only review of calc.py, then fix the bug it finds.', True),
])
def test_small_route_hides_file_effect_schemas_only_for_explicit_read_only_goals(staged, goal, effects):
    app = staged.app
    staged.rid = app.runs.create(staged.cid, goal, app.runtime_config(staged.chat, False), [])
    staged.script = lambda env, index: text('Reported the review plan.')
    app.work(staged.chat, goal, False, [], run_id=staged.rid)
    assert app.runs.get(staged.rid)['state'] == 'completed', diagnosis(staged)
    [record] = staged.requests
    offered = {t['function']['name'] for t in record['body']['tools']}
    # An explicit edit request, even beside "read-only", keeps every effect schema.
    assert offered - AGENT_ROOT_TOOLS == (BASE | EFFECTS if effects else BASE)
    assert offered & AGENT_ROOT_TOOLS == {'ReadAgentTree', 'SpawnAgent'}
    assert record['manifest']['dispatchable']
    exact_schemas(record)
    native_grants_intact(app, staged.rid)


def test_budget_note_is_added_once_per_step_on_large_routes_and_resumed_snapshots(staged):
    """Large routes keep SpawnAgent in `available` across steps; the note must not accumulate."""
    app = staged.app
    app.store.set_setting('model_profiles', [{**ROUTE, 'context': 16384, 'budget': 16384}])
    staged.rid = app.runs.create(staged.cid, GOAL, app.runtime_config(staged.chat, False), [])
    staged.script = lambda env, index: tools_call(('ReadRunEvidence', {}, 'evidence')) if index < 2 \
        else text('Reported the evidence.')
    app.work(staged.chat, GOAL, False, [], run_id=staged.rid)
    # Two tool steps then the answer; a host completion repair could add more steps.
    assert len(staged.requests) >= 3, diagnosis(staged)
    for record in staged.requests:
        [spawn] = [t['function'] for t in record['body']['tools'] if t['function']['name'] == 'SpawnAgent']
        # Large routes show the full catalogue text plus exactly one host note.
        assert spawn['description'] == SPECS['SpawnAgent']['description'] + DEFAULT_NOTE
        assert spawn['parameters'] == SPECS['SpawnAgent']['parameters']
    # The durable snapshot keeps the unannotated schema; an older annotated one is replaced, not extended.
    available = app.runs.get(staged.rid)['checkpoint']['available']
    [stored] = [t for t in available if t['function']['name'] == 'SpawnAgent']
    assert stored['function']['description'] == SPECS['SpawnAgent']['description']
    old = copy.deepcopy(stored)
    old['function']['description'] += schema_note(12)
    [again] = app.agents.annotate_tools(staged.rid, [old])
    assert again['function']['description'] == SPECS['SpawnAgent']['description'] + DEFAULT_NOTE
    assert TOOLS == CATALOGUE


def flow(env, index):
    """Root responses; each must be offered by the stage the request shows."""
    app, cid, rid = env.app, env.cid, env.rid

    def child():
        return app.agents.tree.list(cid, rid)[0]['id']

    if index == 0:
        return tools_call(('SpawnAgent', {'task': 'Review calc.py read-only and report findings with receipt IDs.',
                                          'name': 'reviewer', 'tools': json.dumps(['Read', 'AskParentQuestion'])}, 'spawn'))
    if index == 1:
        return tools_call(('WaitForAgents', {'agent_ids': json.dumps([child()])}, 'wait-steer'))
    if index == 2:
        env.ask.set()
        deadline = time.monotonic() + 10
        while not app.agents.tree.parent_questions(cid, rid, rid) and time.monotonic() < deadline:
            time.sleep(.02)
        return tools_call(('WaitForAgents', {'agent_ids': json.dumps([child()])}, 'wait-question'))
    if index == 3:
        question = app.agents.tree.parent_questions(cid, rid, rid)[0]
        return tools_call(('IntegrateAgentPatch', {'patch_id': question['id']}, 'unoffered'),
                          ('Write', {'file_path': 'calc.py', 'content': 'changed = True\n'}, 'unoffered-write'),
                          ('AnswerAgentQuestion', {'question_id': question['id'], 'answers': json.dumps(ANSWER)}, 'answer'))
    if index == 4:
        return tools_call(('WaitForAgents', {'agent_ids': json.dumps([child()])}, 'wait-result'))
    if index == 5:
        return tools_call(('ReadAgentTree', {'agent_id': child()}, 'tree'))
    if index == 6:
        node = app.agents.tree.get(cid, rid, child())
        return tools_call(('ReviewAgentResult', {'agent_id': node['id'], 'generation': node['generation'],
                                                 'evidence_ids': json.dumps(node['evidence_ids'])}, 'review'))
    return text('The reviewer read calc.py: add(a, b) returns a + b. Its receipts were accepted.')


@pytest.mark.parametrize('heavy_ids', [False, True])
@pytest.mark.parametrize('interrupt_tree', [False, True])
def test_small_route_discovers_each_stage_across_durable_waits_steering_and_resume(route, monkeypatch, interrupt_tree, heavy_ids):
    app = route.app
    if heavy_ids:
        # Valid unique identifiers with costly tokenization exercise the
        # protected receipt/checkpoint floor rather than relying on lucky IDs.
        sequence = itertools.count(1)
        monkeypatch.setattr(uuid, 'uuid4', lambda: uuid.UUID(hex='a1' * 12 + f'{next(sequence):08x}'))
    if interrupt_tree:
        execute = app.execute

        def interrupt_after_tree(name, *args, **kwargs):
            result = execute(name, *args, **kwargs)
            if name == 'ReadAgentTree':
                app.pause_requested.set()
                app.cancel.set()
            return result

        monkeypatch.setattr(app, 'execute', interrupt_after_tree)
    route.rid = app.runs.create(route.cid, GOAL, app.runtime_config(route.chat, False), [])
    route.script = flow
    app.work(route.chat, GOAL, False, [], run_id=route.rid)
    # The first durable wait parks the root with no polling inference.
    until(route, lambda: len(route.requests) == 2 and app.runs.get(route.rid)['state'] == 'waiting_input'
          and app.active is None, 'Root did not park on its first durable wait')
    app.run_steer(route.cid, STEER, route.rid)
    until(route, lambda: len(route.requests) == 5 and app.runs.get(route.rid)['state'] == 'waiting_input'
          and app.active is None, 'Root did not resume and park on the child result')
    route.read.set()
    if interrupt_tree:
        until(route, lambda: app.runs.get(route.rid)['state'] == 'paused' and app.active is None,
              'Root did not pause after the committed tree receipt')
        monkeypatch.setattr(app, 'execute', execute)
        app.run_resume(route.cid, route.rid)
    until(route, lambda: app.runs.get(route.rid)['state'] == 'completed' and app.active is None,
          'Root did not resume, review and complete')
    stages = ['idle', 'active', 'active', 'questions', 'active', 'review', 'review', 'idle']
    assert len(route.requests) == len(stages)
    offered = [check(record, stage) for record, stage in zip(route.requests, stages)]
    if heavy_ids:
        assert all(record['manifest']['input_tokens']['protected_floor'] is None
                   or record['manifest']['input_tokens']['protected_floor'] <= LIMITS['input'] - 64
                   for record in route.requests)
    # Resumed requests restore a checkpoint, yet show the current stage's tools.
    resumes = [i for i in range(1, len(stages)) if route.requests[i]['thread'] is not route.requests[i - 1]['thread']]
    assert resumes == ([2, 5, 6] if interrupt_tree else [2, 5]) and offered[5] != offered[4]
    for record in route.requests[2:]:
        # Exact human steering stays protected inbox input on every later request.
        assert any(m['role'] == 'user' and STEER in m['content'] for m in record['body']['messages'])
        inbox = [unit for unit in record['manifest']['units'] if unit['category'] == 'inbox']
        assert inbox and all(unit['protected'] and unit['selected'] for unit in inbox)
    for ident in ('unoffered', 'unoffered-write'):
        assert REJECTED in tool_result(route.requests[4], ident)['error']
        assert app.store.query('SELECT state FROM run_tools WHERE run_id=? AND call_id LIKE ?',
                               (route.rid, '%:' + ident)) == [{'state': 'planned'}]
    assert not app.store.query('SELECT 1 FROM agent_patches WHERE run_id=?', (route.rid,))
    assert (route.project / 'calc.py').read_text() == SOURCE and not app.store.actions(route.cid)
    native_grants_intact(app, route.rid)
    # The small-window tree index is a bounded projection of the child's records.
    tree = tool_result(route.requests[6], 'tree')
    assert 'projection_note' in tree and set(tree['detail']) == {
        'id', 'name', 'parent_id', 'generation', 'status', 'review', 'evidence_ids',
        'result_preview', 'patches', 'receipts'}
    assert all(set(r) == {'call_id', 'name', 'state'} for r in tree['detail']['receipts'])
    assert 'untrusted_source' not in json.dumps(tree) and 'context_manifest' not in json.dumps(tree)
    # The 4K context copy drops only the host-enforced ledger; the receipt keeps it.
    [receipt] = [t['result'] for t in app.runs.tools(route.rid) if t['call_id'].endswith(':tree')]
    assert 'budget' not in tree and 'runtime' not in tree
    assert {k for k in receipt if k not in tree} <= {'budget', 'runtime'} and tree['nodes'] == receipt['nodes']
    [node] = app.agents.tree.list(route.cid, route.rid)
    assert node['status'] == 'completed' and node['review'] == 'accepted'
    parked = [e for e in app.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='state'", (route.rid,))
              if json.loads(e['payload'])['to'] == 'waiting_input']
    assert len(parked) == 2
