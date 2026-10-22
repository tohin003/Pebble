"""Child lifecycle cleanup, answer provenance and small-window tool staging.

The App, durable tree, native question routes, root tool journal and scheduler
threads are the checked-in implementations. Providers are deterministic
fixtures: no model HTTP, cloud billing, credential, shell, app launch or plugin
operation is used. Synchronous cases disable only the scheduler thread and act
through the same tree/manager calls that a worker makes.
"""

import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from pebble.agent_manager import Agents, CHILD, STAGE_LIMIT, STAGING_NOTE
from pebble.agent_tree import ACTIVE
from pebble.app import App
from pebble.context_manifest import assemble_context, estimate_tokens
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError, Workspace
from pebble.tool_catalogue import TOOLS


PROFILE = {'id': 'fixture-cloud', 'kind': 'openai', 'name': 'Fixture provider',
           'url': 'https://unused.invalid', 'model': 'fixture', 'context': 8192,
           'budget': 4096, 'tools': True}
QUESTIONS = [{'id': 'scope', 'title': 'Which source should I inspect?',
              'options': [{'id': 'small', 'label': 'This source'}]}]
ANSWER = {'scope': {'option_id': 'small'}}
CANONICAL = {'scope': {'option_id': 'small', 'text': 'This source'}}
TERMINAL = {'completed', 'failed', 'interrupted', 'stopped', 'budget_exhausted'}


def call(name, args, ident='tool'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(args)}}]}


def final(text='Finished scoped work.'):
    return {'role': 'assistant', 'content': text}


def eventually(predicate, message, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    pytest.fail(message)


def block_until(predicate, cancel, timeout=5):
    """Hold a fixture provider request open until a test condition holds."""
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if cancel.is_set():
            raise InterruptedError('Fixture request cancelled.')
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Fixture condition was not reached.')


def question_script(_provider, step, _messages, _tools, _cancel):
    return call('AskParentQuestion', {'questions': json.dumps(QUESTIONS)}, 'question') if step == 1 else final('Continued with the ordinary clarification answer.')


def start_root(app, project):
    for live in app.runs.list():
        if live['state'] in {'queued', 'running', 'retry_wait'}:
            app.runs.transition(live['id'], 'waiting_input')
    chat = app.new_chat(str(project))
    config = app.runtime_config(chat, False, {'max_steps': 64, 'max_seconds': 180})
    rid = app.runs.create(chat['id'], 'Execute only explicitly scoped lifecycle fixture work.', config)
    app.runs.transition(rid, 'running')
    # Holding the root owner keeps automatic continuation from calling a model.
    app.active, app.active_run = chat['id'], rid
    app.task_policy = copy.deepcopy(config['permissions'])
    return chat['id'], rid


def root_tool(app, project, cid, rid, name, args, ident):
    for live in app.runs.list():
        if live['id'] != rid and live['state'] in {'queued', 'running', 'retry_wait'}:
            app.runs.transition(live['id'], 'waiting_input')
    if app.runs.get(rid)['state'] == 'waiting_input':
        app.runs.transition(rid, 'running')
    app.active, app.active_run = cid, rid
    return app.execute(name, args, Workspace(project), cid, call_id=ident)


def native_status(app, question_id):
    return app.store.query('SELECT status FROM run_questions WHERE id=?', (question_id,))[0]['status']


def cancelled_by(app, question_id):
    rows = app.store.query('SELECT origin FROM agent_question_cancellations WHERE question_id=?', (question_id,))
    return rows[0]['origin'] if rows else None


def root_receipt(app, rid, call_id):
    return next(t for t in app.runs.tools(rid) if t['call_id'] == call_id)['result']


def ask_receipt(app, cid, rid, agent_id, generation=1):
    return next(r for r in app.agents.tree.receipts(cid, rid, agent_id, generation) if r['name'] == 'AskParentQuestion')['result']


def question_of(app, agent_id, status='pending'):
    rows = app.store.query('SELECT id FROM agent_parent_questions WHERE agent_id=? AND status=? ORDER BY created DESC', (agent_id, status))
    return rows[0]['id'] if rows else None


def checkpoint(app, agent_id, generation=1):
    return json.loads(app.store.query('SELECT messages FROM agent_checkpoints WHERE agent_id=? AND generation=?',
                                      (agent_id, generation))[0]['messages'])


def revoked_event(events, question_id):
    return any(len(event) == 2 and event[0] == 'question' and isinstance(event[1], dict)
               and event[1].get('question_id') == question_id and event[1].get('status') == 'cancelled'
               for event in list(events))


def settled(app, cid, rid, ident, state, timeout=5):
    def done():
        record = app.agents.tree.get(cid, rid, ident)
        if record['status'] in TERMINAL and record['status'] != state:
            pytest.fail('Unexpected child outcome: ' + json.dumps(record, default=str)[:2000])
        with app.agents.lock:
            owned = ident in app.agents.workers
        return record if record['status'] == state and not owned else None
    return eventually(done, 'Child did not reach ' + state + '.', timeout)


# Synchronous host: real App/tree/native routes without the scheduler thread.

@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(Agents, 'start', lambda self: None)
    events, root_calls = [], []
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(args))
    project = tmp_path / 'project'
    project.mkdir()

    def root_complete(*args, **kwargs):
        root_calls.append(args)
        raise AssertionError('Lifecycle fixtures must not make a root model request.')

    monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(PROFILE))
    monkeypatch.setattr(app.model, 'complete', root_complete)
    monkeypatch.setattr(app.guard, 'handle', lambda *args, **kwargs: {'action': 'allow', 'reason': 'Fixture Guard admission'})
    try:
        yield SimpleNamespace(app=app, project=project, events=events)
    finally:
        app.active = app.active_run = None
        app.close()
    assert root_calls == []


def claimed(app, cid, rid, name, tools, parent=None, paths=(), steps=2, seconds=10, tokens=16384):
    lease = {'parent_generation': 1, 'parent_owner': parent.owner} if parent else {}
    node = app.agents.tree.spawn(cid, rid, parent.id if parent else None, 'Complete the scoped ' + name + ' task.', name,
                                 list(tools), {'max_steps': steps, 'max_seconds': seconds, 'model_tokens': tokens}, list(paths), **lease)
    owner = 'worker-' + name
    app.agents.tree.claim(cid, rid, node['id'], 1, owner)
    return SimpleNamespace(id=node['id'], name=name, owner=owner)


def ask(app, cid, rid, node, call_id='1:ask', end=True):
    """The worker's AskParentQuestion sequence: running marker, question, receipt."""
    tree = app.agents.tree
    assert tree.begin_tool(cid, rid, node.id, 1, node.owner, call_id, 'AskParentQuestion', {'questions': json.dumps(QUESTIONS)}) is None
    asked = tree.ask_parent(cid, rid, node.id, 1, node.owner, call_id, QUESTIONS)
    if end:
        tree.end_tool(cid, rid, node.id, 1, node.owner, call_id, asked)
    return asked


@pytest.mark.parametrize('closure,status,origin,cascade', [
    ('failed', 'failed', 'host', True),
    ('budget_exhausted', 'budget_exhausted', 'host', True),
    ('released', 'interrupted', 'host', True),
    ('paused', 'interrupted', 'host', False),
    ('agent_stop', 'stopped', 'agent', True),
    ('human_stop', 'stopped', 'human', True),
])
def test_parent_closure_revokes_descendant_forms_with_origin_and_spares_reports_and_peers(host, closure, status, origin, cascade):
    app, project, tree = host.app, host.project, host.app.agents.tree
    (project / 'report.py').write_text('report = 1\n')
    cid, rid = start_root(app, project)
    parent = claimed(app, cid, rid, 'parent', ['Read', 'SpawnAgent', 'AskParentQuestion', 'AnswerAgentQuestion', 'StopAgent'],
                     steps=4, seconds=30, tokens=65536)
    peer = claimed(app, cid, rid, 'peer', ['AskParentQuestion'])
    grandchild = claimed(app, cid, rid, 'grandchild', ['AskParentQuestion'], parent=parent)
    report = claimed(app, cid, rid, 'report', ['Read'], parent=parent, paths=['report.py'])
    tree.begin_tool(cid, rid, report.id, 1, report.owner, '1:read', 'Read', {'file_path': 'report.py'})
    tree.end_tool(cid, rid, report.id, 1, report.owner, '1:read', {'path': 'report.py', 'untrusted_source': 'report = 1\n'})
    tree.settle(cid, rid, report.id, 1, 'completed', {'text': 'Report finished.'}, ['1:read'], report.owner)
    forms = {}
    for node in (parent, grandchild, peer):
        asked = ask(app, cid, rid, node)
        # The root oversees every question in its tree, including a grandchild's.
        forms[node.name] = root_tool(app, project, cid, rid, 'PromoteAgentQuestion', {'question_id': asked['question_id']}, 'promote-' + node.name)['question_id']

    if closure in ('failed', 'budget_exhausted'):
        tree.settle(cid, rid, parent.id, 1, closure, {'error': 'Fixture ' + closure + '.'}, owner=parent.owner)
    elif closure in ('released', 'paused'):
        tree.release(cid, rid, parent.id, 1, parent.owner, cascade=closure == 'released')
    elif closure == 'agent_stop':
        root_tool(app, project, cid, rid, 'StopAgent', {'agent_id': parent.id}, 'stop-parent')
    else:
        app.dispatch('agent_cancel', {'chat_id': cid, 'run_id': rid, 'agent_id': parent.id})

    assert tree.get(cid, rid, parent.id)['status'] == status
    own = forms['parent']
    assert native_status(app, own) == 'cancelled' and cancelled_by(app, own) == origin
    promoted = root_receipt(app, rid, 'promote-parent')
    assert promoted['status'] == 'cancelled' and promoted['answer_origin'] == origin
    assert ask_receipt(app, cid, rid, parent.id)['status'] == 'cancelled'
    descendant = forms['grandchild']
    if cascade:
        assert tree.get(cid, rid, grandchild.id)['status'] == 'stopped'
        assert native_status(app, descendant) == 'cancelled' and cancelled_by(app, descendant) == origin
        assert ask_receipt(app, cid, rid, grandchild.id)['status'] == 'cancelled'
    else:
        # A paused parent leaves its descendant retryable and its human form answerable.
        assert tree.get(cid, rid, grandchild.id)['status'] == 'waiting_input'
        assert native_status(app, descendant) == 'pending'
        app.question_answer(cid, descendant, 1, ANSWER)
        assert tree.get(cid, rid, grandchild.id)['status'] == 'running'
        assert ask_receipt(app, cid, rid, grandchild.id)['answer_origin'] == 'human'
    # A completed report is never marked stopped and keeps its result and evidence.
    kept = tree.get(cid, rid, report.id)
    assert kept['status'] == 'completed' and kept['result'] == {'text': 'Report finished.'} and kept['evidence_ids'] == ['1:read']
    if origin == 'host':
        assert app.store.query('SELECT status FROM agent_path_leases WHERE path=?', ('report.py',)) == [{'status': 'held'}]
    # The independent peer keeps waiting on its own still-answerable form.
    assert tree.get(cid, rid, peer.id)['status'] == 'waiting_input'
    assert native_status(app, forms['peer']) == 'pending' and cancelled_by(app, forms['peer']) is None
    with pytest.raises(ValueError, match='no longer accepting'):
        app.question_answer(cid, own, 1, ANSWER)
    assert app.question_cancel(cid, own, 1)['status'] == 'cancelled'
    assert cancelled_by(app, own) == origin, 'A later dismissal never rewrites the original provenance.'
    assert not app.store.query("SELECT * FROM agent_inputs WHERE agent_id=? AND origin='human'", (parent.id,))
    app.question_answer(cid, forms['peer'], 1, ANSWER)
    assert tree.get(cid, rid, peer.id)['status'] == 'running'
    assert ask_receipt(app, cid, rid, peer.id)['answer_origin'] == 'human'
    app.agents.wake()
    assert revoked_event(host.events, own)


def test_model_budget_denial_closes_live_descendants_and_their_forms_only(host):
    app, project, tree = host.app, host.project, host.app.agents.tree
    cid, rid = start_root(app, project)
    parent = claimed(app, cid, rid, 'parent', ['SpawnAgent', 'AskParentQuestion'], steps=4, seconds=30, tokens=32768)
    grandchild = claimed(app, cid, rid, 'grandchild', ['AskParentQuestion'], parent=parent)
    peer = claimed(app, cid, rid, 'peer', ['AskParentQuestion'])
    asked = ask(app, cid, rid, grandchild)
    form = root_tool(app, project, cid, rid, 'PromoteAgentQuestion', {'question_id': asked['question_id']}, 'promote-grandchild')['question_id']
    peer_question = ask(app, cid, rid, peer)['question_id']
    denied = tree.reserve_model(cid, rid, parent.id, 1, 40000, 0, 'over-budget')
    assert not denied['admitted'] and denied['status'] == 'budget_exhausted'
    assert tree.get(cid, rid, parent.id)['status'] == 'budget_exhausted'
    assert tree.get(cid, rid, grandchild.id)['status'] == 'stopped'
    assert native_status(app, form) == 'cancelled' and cancelled_by(app, form) == 'host'
    receipt = root_receipt(app, rid, 'promote-grandchild')
    assert receipt['status'] == 'cancelled' and receipt['answer_origin'] == 'host'
    assert not app.store.query('SELECT * FROM agent_dispatches WHERE actor=?', (parent.id,))
    assert tree.get(cid, rid, peer.id)['status'] == 'waiting_parent'
    assert [q['id'] for q in tree.parent_questions(cid, rid, rid)] == [peer_question]


@pytest.mark.parametrize('order', ['answer_before_end_tool', 'end_tool_before_answer'])
@pytest.mark.parametrize('path', ['agent', 'human'])
def test_answer_provenance_refreshes_only_the_correlated_ask_receipt(host, path, order):
    app, project, tree = host.app, host.project, host.app.agents.tree
    cid, rid = start_root(app, project)
    child = claimed(app, cid, rid, 'asker', ['AskParentQuestion', 'Read'])
    other = claimed(app, cid, rid, 'other', ['AskParentQuestion'])
    ask(app, cid, rid, other)
    asked = ask(app, cid, rid, child, end=order == 'end_tool_before_answer')
    qid = asked['question_id']
    if path == 'human':
        form = root_tool(app, project, cid, rid, 'PromoteAgentQuestion', {'question_id': qid}, 'promote')['question_id']
        with pytest.raises(PolicyError, match='no longer pending'):
            root_tool(app, project, cid, rid, 'AnswerAgentQuestion', {'question_id': qid, 'answers': json.dumps(ANSWER)}, 'agent-over-form')
        assert tree.parent_question(cid, rid, rid, qid)['status'] == 'promoted'
        assert native_status(app, form) == 'pending'
        app.question_answer(cid, form, 1, ANSWER)
    else:
        answered = root_tool(app, project, cid, rid, 'AnswerAgentQuestion', {'question_id': qid, 'answers': json.dumps(ANSWER)}, 'answer')
        assert answered['answer_origin'] == 'agent' and answered['escalated'] is False
    if order == 'answer_before_end_tool':
        # The answer committed first; the stale awaiting outcome must not win.
        stored = tree.end_tool(cid, rid, child.id, 1, child.owner, '1:ask', asked)
        assert stored['status'] == 'answered' and stored['answer_origin'] == path
    receipt = ask_receipt(app, cid, rid, child.id)
    assert receipt['question_id'] == qid and receipt['status'] == 'answered' and receipt['answer_origin'] == path
    assert receipt['origin'] == 'agent' and receipt['authorization'] == 'none'
    assert ask_receipt(app, cid, rid, other.id)['status'] == 'awaiting_parent'
    assert tree.parent_question(cid, rid, child.id, qid)['answer_origin'] == path
    worker = {'cid': cid, 'rid': rid, 'id': child.id, 'generation': 1, 'owner': child.owner, 'cancel': threading.Event()}
    waited = app.agents.wait_question(worker, qid, time.monotonic() + 5)
    assert waited == {'question_id': qid, 'status': 'answered', 'answers': CANONICAL, 'answer_origin': path,
                      'origin': 'agent', 'authorization': 'none'}
    human = app.store.query("SELECT id FROM agent_inputs WHERE agent_id=? AND origin='human'", (child.id,))
    if path == 'human':
        assert human == [{'id': 'native-' + form}]
        # An agent can neither relabel nor overwrite the human answer.
        for answers in (ANSWER, {'scope': {'text': 'Different agent answer.'}}):
            with pytest.raises(ValueError, match='no longer pending'):
                tree.answer_parent(cid, rid, rid, qid, answers)
        assert tree.parent_question(cid, rid, rid, qid)['answer_origin'] == 'human'
    else:
        assert human == [] and not app.store.query('SELECT * FROM run_questions WHERE run_id=?', (rid,))
        assert not app.store.query("SELECT * FROM run_inbox WHERE run_id=? AND origin='human'", (rid,))
        assert tree.answer_parent(cid, rid, rid, qid, ANSWER)['answer_origin'] == 'agent'


def test_root_inherits_orphaned_descendant_questions_without_widening_child_scope(host):
    app, project, tree = host.app, host.project, host.app.agents.tree
    other_cid, other_rid = start_root(app, project)
    cid, rid = start_root(app, project)
    parent = claimed(app, cid, rid, 'parent', ['SpawnAgent', 'AskParentQuestion', 'AnswerAgentQuestion', 'WaitForAgents'],
                     steps=4, seconds=30, tokens=65536)
    peer = claimed(app, cid, rid, 'peer', ['AskParentQuestion', 'AnswerAgentQuestion'])
    sibling = claimed(app, cid, rid, 'sibling', ['AskParentQuestion', 'AnswerAgentQuestion'], parent=parent)
    grandchild = claimed(app, cid, rid, 'grandchild', ['AskParentQuestion'], parent=parent)
    assert tree.wait(cid, rid, rid, 'root-wait', [parent.id])['status'] == 'awaiting_agent_results'
    gq = ask(app, cid, rid, grandchild)['question_id']
    # While its direct parent is live, the question stays in that parent's scope.
    assert tree.wait(cid, rid, rid, 'root-wait', [parent.id])['status'] == 'awaiting_agent_results'
    assert tree.parent_questions(cid, rid, rid) == []
    assert [(q['id'], q['escalated']) for q in tree.parent_questions(cid, rid, parent.id)] == [(gq, False)]
    assert tree.parent_question(cid, rid, rid, gq)['parent_id'] == parent.id
    for actor in (sibling, peer):
        with pytest.raises(ValueError, match='owner scope'):
            tree.answer_parent(cid, rid, actor.id, gq, ANSWER, generation=1, owner=actor.owner)

    tree.settle(cid, rid, parent.id, 1, 'completed', {'text': 'Parent finished first.'}, (), parent.owner)
    # Completion never cascades: the descendants continue under root oversight.
    assert tree.get(cid, rid, grandchild.id)['status'] == 'waiting_parent'
    assert tree.get(cid, rid, sibling.id)['status'] == 'running'
    woken = tree.wait(cid, rid, rid, 'root-wait', [parent.id])
    assert woken['status'] == 'ready' and woken['question_ids'] == [gq] and woken['agent_ids'] == [parent.id]
    assert app.store.query("SELECT origin FROM run_inbox WHERE run_id=? AND kind='agent_wait_ready'", (rid,)) == [{'origin': 'agent'}]
    assert [(q['id'], q['escalated'], q['parent_id']) for q in tree.parent_questions(cid, rid, rid)] == [(gq, True, parent.id)]
    assert {'AnswerAgentQuestion', 'PromoteAgentQuestion'} <= app.agents.root_tools(cid, rid)
    # The settled parent's old lease, a peer, a sibling and another tree stay out of scope.
    with pytest.raises(ValueError, match='new work'):
        tree.answer_parent(cid, rid, parent.id, gq, ANSWER, generation=1, owner=parent.owner)
    for actor in (sibling, peer):
        with pytest.raises(ValueError, match='owner scope'):
            tree.answer_parent(cid, rid, actor.id, gq, ANSWER, generation=1, owner=actor.owner)
    with pytest.raises(ValueError, match='owner scope'):
        tree.answer_parent(other_cid, other_rid, other_rid, gq, ANSWER)
    with pytest.raises(ValueError, match='conversation'):
        tree.answer_parent(other_cid, rid, rid, gq, ANSWER)
    with pytest.raises(ValueError, match='scope'):
        tree.parent_question(other_cid, other_rid, other_rid, gq)
    assert tree.parent_question(cid, rid, grandchild.id, gq)['status'] == 'pending'

    answered = root_tool(app, project, cid, rid, 'AnswerAgentQuestion', {'question_id': gq, 'answers': json.dumps(ANSWER)}, 'answer-orphan')
    assert answered['escalated'] is True and answered['answer_origin'] == 'agent' and answered['authorization'] == 'none'
    assert tree.get(cid, rid, grandchild.id)['status'] == 'running'
    receipt = ask_receipt(app, cid, rid, grandchild.id)
    assert receipt['status'] == 'answered' and receipt['answer_origin'] == 'agent'
    assert tree.parent_questions(cid, rid, rid) == []
    # The peer's own question still reaches the root as a direct, non-escalated one.
    pq = ask(app, cid, rid, peer)['question_id']
    assert [(q['id'], q['escalated']) for q in tree.parent_questions(cid, rid, rid)] == [(pq, False)]
    assert not app.store.query("SELECT * FROM agent_inputs WHERE origin='human'")


def test_restart_recovery_revokes_promoted_forms_of_interrupted_children_as_host(host):
    app, project = host.app, host.project
    cid, rid = start_root(app, project)
    child = claimed(app, cid, rid, 'recovered', ['AskParentQuestion'])
    qid = ask(app, cid, rid, child)['question_id']
    form = root_tool(app, project, cid, rid, 'PromoteAgentQuestion', {'question_id': qid}, 'promote-recovered')['question_id']
    restarted = Agents(app)  # Tree recovery plus the manager's startup sweep, as on service start.
    assert restarted.tree.get(cid, rid, child.id)['status'] == 'interrupted'
    assert native_status(app, form) == 'cancelled' and cancelled_by(app, form) == 'host'
    receipt = root_receipt(app, rid, 'promote-recovered')
    assert receipt['status'] == 'cancelled' and receipt['answer_origin'] == 'host'
    assert ask_receipt(app, cid, rid, child.id)['status'] == 'cancelled'
    with pytest.raises(ValueError, match='no longer accepting'):
        app.question_answer(cid, form, 1, ANSWER)
    assert app.question_cancel(cid, form, 1)['status'] == 'cancelled' and cancelled_by(app, form) == 'host'
    assert not app.store.query("SELECT * FROM agent_inputs WHERE origin='human'")


def test_window_detection_stages_only_large_sets_on_small_or_local_windows(host, monkeypatch):
    agents = host.app.agents
    large, narrow = {'tools': sorted(CHILD)}, {'tools': sorted(CHILD)[:STAGE_LIMIT]}
    assert len(CHILD) == 18  # Phase 16 admits ReadSource for research children (17 before)
    assert agents._window() == 4096 and agents.staged(large) and not agents.staged(narrow)
    monkeypatch.setattr(host.app.model, 'current', lambda: {**PROFILE, 'kind': 'gguf', 'context': 32768, 'budget': 32768})
    assert agents._window() == 4000 and agents.staged(large) and not agents.staged(narrow)
    monkeypatch.setattr(host.app.model, 'current', lambda: {**PROFILE, 'context': 32768, 'budget': 16384})
    assert agents._window() == 16384 and not agents.staged(large)


# Real scheduler and child threads with isolated fixture transports.

class FakePinnedProvider:
    """One independently owned child transport; never touches the root model."""

    def __init__(self, env, app, profile, owner_id, lane, observer=None):
        self.env, self.app = env, app
        self.profile, self.owner_id, self.lane = copy.deepcopy(profile), owner_id, lane
        self.agent_id = owner_id.split(':')[1]
        self.name = app.store.query('SELECT name FROM agent_nodes WHERE id=?', (self.agent_id,))[0]['name']
        self.stopped = threading.Event()
        self.closed = False
        self.cancel_calls = 0
        self.last_usage = {'input_tokens': 17, 'output_tokens': 9}
        self.requests = []
        self.observer = observer
        assert self.name in env.scripts, 'Every child must have an explicitly supplied fixture script.'
        with env.lock:
            env.providers[self.name] = self

    def complete(self, messages, tools, cancel, token, effort):
        with self.lane.admit(self.owner_id, self.profile, cancel):
            if cancel.is_set() or self.stopped.is_set():
                raise InterruptedError('Fixture child stopped.')
            projection = assemble_context(messages, tools, self.profile['budget'], output_reserve=256)
            if self.observer:
                self.observer(projection.manifest)
            with self.env.lock:
                self.requests.append({'messages': copy.deepcopy(projection.messages), 'tools': copy.deepcopy(tools),
                                      'manifest': copy.deepcopy(projection.manifest)})
                step = len(self.requests)
            script = self.env.scripts[self.name]
            result = script(self, step, messages, tools, cancel) if callable(script) else script[step - 1]
            return copy.deepcopy(result)

    def cancel(self):
        self.cancel_calls += 1
        self.stopped.set()
        self.lane.wake()

    def close(self):
        self.closed = True
        self.stopped.set()
        self.lane.wake()


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    events = []
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(args))
    value = SimpleNamespace(app=app, project=project, scripts={}, providers={}, lock=threading.RLock(),
                            root_calls=[], events=events)
    monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(PROFILE))

    def root_complete(*args, **kwargs):
        value.root_calls.append((args, kwargs))
        raise AssertionError('Executor fixtures must not make a root model request.')

    monkeypatch.setattr(app.model, 'complete', root_complete)
    monkeypatch.setattr(app.guard, 'handle', lambda *args, **kwargs: {'action': 'allow', 'reason': 'Fixture Guard admission'})
    app.agents.provider_factory = lambda *args, **kwargs: FakePinnedProvider(value, *args, **kwargs)
    permissions = copy.deepcopy(app.store.setting('permissions'))
    yield value
    try:
        assert app.store.setting('permissions') == permissions == DEFAULT
    finally:
        app.active = app.active_run = None
        app.close()
    assert not app.agents.busy(), 'Fixture teardown must join every child worker.'
    assert value.root_calls == []


def spawn(env, cid, rid, name, script, tools=('AskParentQuestion',), paths=(), task=None, steps=4, seconds=10, tokens=98304):
    env.scripts[name] = script
    args = {'task': task or 'Complete the scoped ' + name + ' task.', 'name': name,
            'write_paths': json.dumps(list(paths)), 'max_steps': steps, 'max_seconds': seconds, 'model_tokens': tokens}
    if tools is not None:
        args['tools'] = json.dumps(list(tools))
    return root_tool(env.app, env.project, cid, rid, 'SpawnAgent', args, 'spawn-' + name)


def child_of(app, cid, rid, parent_id, name):
    return eventually(lambda: next((n for n in app.agents.tree.list(cid, rid)
                                    if n['parent_id'] == parent_id and n['name'] == name), None),
                      'Parent did not spawn ' + name + '.')


@pytest.mark.parametrize('parked_state', ['waiting_input', 'waiting_approval'])
def test_child_delegates_while_root_waits_without_starting_root_inference(env, parked_state):
    app = env.app
    cid, rid = start_root(app, env.project)
    (env.project / 'notes.py').write_text('notes = True\n')

    def parent_script(provider, step, _messages, _tools, cancel):
        if step == 1:
            block_until(lambda: app.runs.get(rid)['state'] == parked_state, cancel)
            return call('SpawnAgent', {'task': 'Read the scoped notes.', 'name': 'parked-grandchild',
                        'tools': json.dumps(['Read']), 'max_steps': 2, 'max_seconds': 10,
                        'model_tokens': 16384}, 'spawn-grandchild')
        if step == 2:
            child = child_of(app, cid, rid, provider.agent_id, 'parked-grandchild')
            return call('WaitForAgents', {'agent_ids': json.dumps([child['id']])}, 'wait-grandchild')
        return final('Scoped delegated reading completed.')

    env.scripts['parked-grandchild'] = [call('Read', {'file_path': 'notes.py'}, 'read'), final('Read notes.py.')]
    parent = spawn(env, cid, rid, 'parked-parent', parent_script,
                   tools=('Read', 'SpawnAgent', 'WaitForAgents'), seconds=20)
    app.runs.transition(rid, parked_state)
    settled(app, cid, rid, parent['id'], 'completed')
    child = child_of(app, cid, rid, parent['id'], 'parked-grandchild')
    settled(app, cid, rid, child['id'], 'completed')
    assert app.runs.get(rid)['state'] == parked_state and env.root_calls == []
    assert app.agents.tree.receipts(cid, rid, child['id'], 1)[0]['result']['path'] == 'notes.py'


def test_failed_parent_cancels_inflight_descendant_request_and_form_while_peer_continues(env):
    app = env.app
    cid, rid = start_root(app, env.project)

    def inflight(provider, _step, _messages, _tools, cancel):
        block_until(provider.stopped.is_set, cancel, timeout=15)
        raise InterruptedError('Fixture request cancelled.')

    def doomed(provider, step, _messages, _tools, cancel):
        if step < 3:
            name = ('inflight-grandchild', 'asking-grandchild')[step - 1]
            return call('SpawnAgent', {'task': 'Handle one delegated detail.', 'name': name,
                        'tools': json.dumps(['AskParentQuestion']), 'max_steps': 2, 'max_seconds': 10,
                        'model_tokens': 16384}, 'spawn-' + str(step))
        block_until(lambda: app.store.query("SELECT 1 FROM agent_parent_questions WHERE parent_id=? AND status='promoted'",
                                            (provider.agent_id,)), cancel)
        raise RuntimeError('Fixture provider failure after delegation.')

    env.scripts.update({'inflight-grandchild': inflight, 'asking-grandchild': question_script})
    parent = spawn(env, cid, rid, 'doomed-parent', doomed, tools=('SpawnAgent', 'AskParentQuestion'), seconds=20)
    peer = spawn(env, cid, rid, 'steady-peer', question_script)
    peer_question = eventually(lambda: question_of(app, peer['id']), 'Peer did not ask the root.')
    inflight_node = child_of(app, cid, rid, parent['id'], 'inflight-grandchild')
    asking = child_of(app, cid, rid, parent['id'], 'asking-grandchild')
    eventually(lambda: env.providers.get('inflight-grandchild') and env.providers['inflight-grandchild'].requests,
               'The in-flight grandchild request did not start.')
    gq = eventually(lambda: question_of(app, asking['id']), 'Grandchild did not ask its parent.')
    assert gq not in [q['id'] for q in app.agents.tree.parent_questions(cid, rid, rid)], 'A live parent owns its child question.'
    assert app.agents.tree.parent_question(cid, rid, rid, gq)['parent_id'] == parent['id']
    form = root_tool(app, env.project, cid, rid, 'PromoteAgentQuestion', {'question_id': gq}, 'promote-grandchild')['question_id']

    failed = settled(app, cid, rid, parent['id'], 'failed')
    assert 'Fixture provider failure' in failed['result']['error']
    # Faster than the five-second heartbeat: the closure itself cancels the request.
    settled(app, cid, rid, inflight_node['id'], 'stopped', timeout=2.5)
    assert env.providers['inflight-grandchild'].cancel_calls > 0
    settled(app, cid, rid, asking['id'], 'stopped')
    assert native_status(app, form) == 'cancelled' and cancelled_by(app, form) == 'host'
    receipt = root_receipt(app, rid, 'promote-grandchild')
    assert receipt['status'] == 'cancelled' and receipt['answer_origin'] == 'host'
    eventually(lambda: revoked_event(env.events, form), 'The revoked native form was not published.')
    with pytest.raises(ValueError, match='no longer accepting'):
        app.question_answer(cid, form, 1, ANSWER)
    assert not app.store.query("SELECT * FROM agent_inputs WHERE origin='human'")

    assert app.agents.tree.get(cid, rid, peer['id'])['status'] == 'waiting_parent'
    assert env.providers['steady-peer'].cancel_calls == 0
    root_tool(app, env.project, cid, rid, 'AnswerAgentQuestion', {'question_id': peer_question, 'answers': json.dumps(ANSWER)}, 'answer-peer')
    assert settled(app, cid, rid, peer['id'], 'completed')['result']['text'] == 'Continued with the ordinary clarification answer.'
    assert env.providers['steady-peer'].cancel_calls == 0


def test_root_pause_keeps_descendants_retryable_and_root_answers_the_orphaned_retry(env):
    app = env.app
    cid, rid = start_root(app, env.project)

    def supervisor(_provider, step, _messages, _tools, _cancel):
        if step == 1:
            return call('SpawnAgent', {'task': 'Clarify the delegated detail.', 'name': 'pause-grandchild',
                        'tools': json.dumps(['AskParentQuestion']), 'max_steps': 3, 'max_seconds': 10,
                        'model_tokens': 32768}, 'spawn')
        return call('AskParentQuestion', {'questions': json.dumps(QUESTIONS)}, 'question')

    env.scripts['pause-grandchild'] = question_script
    parent = spawn(env, cid, rid, 'pause-parent', supervisor, tools=('SpawnAgent', 'AskParentQuestion'), seconds=20)
    pq = eventually(lambda: question_of(app, parent['id']), 'Parent did not ask the root.')
    grandchild = child_of(app, cid, rid, parent['id'], 'pause-grandchild')
    gq = eventually(lambda: question_of(app, grandchild['id']), 'Grandchild did not ask its parent.')
    form = root_tool(app, env.project, cid, rid, 'PromoteAgentQuestion', {'question_id': pq}, 'promote-pause')['question_id']

    app.agents.suspend(cid, rid)
    settled(app, cid, rid, parent['id'], 'interrupted')
    settled(app, cid, rid, grandchild['id'], 'interrupted')
    assert env.providers['pause-parent'].cancel_calls > 0
    assert native_status(app, form) == 'cancelled' and cancelled_by(app, form) == 'host'
    receipt = root_receipt(app, rid, 'promote-pause')
    assert receipt['status'] == 'cancelled' and receipt['answer_origin'] == 'host'
    assert ask_receipt(app, cid, rid, parent['id'])['status'] == 'cancelled'
    assert app.agents.tree.parent_question(cid, rid, rid, gq)['status'] == 'cancelled'
    eventually(lambda: revoked_event(env.events, form), 'The revoked native form was not published.')
    with pytest.raises(ValueError, match='no longer accepting'):
        app.question_answer(cid, form, 1, ANSWER)

    # Pause interrupted rather than stopped the descendant, so it remains retryable.
    assert app.agents.tree.retry(cid, rid, rid, grandchild['id'])['generation'] == 2
    app.agents.wake()
    orphan = eventually(lambda: next((q for q in app.agents.tree.parent_questions(cid, rid, rid)
                                      if q['agent_id'] == grandchild['id']), None), 'The orphaned question did not escalate to the root.')
    assert orphan['escalated'] is True and orphan['parent_id'] == parent['id'] and orphan['generation'] == 2
    answered = root_tool(app, env.project, cid, rid, 'AnswerAgentQuestion', {'question_id': orphan['id'], 'answers': json.dumps(ANSWER)}, 'answer-orphan')
    assert answered['escalated'] is True and answered['answer_origin'] == 'agent'
    record = settled(app, cid, rid, grandchild['id'], 'completed')
    assert record['generation'] == 2 and record['result']['text'] == 'Continued with the ordinary clarification answer.'
    result = json.loads(next(m['content'] for m in checkpoint(app, grandchild['id'], 2) if m['role'] == 'tool'))
    assert result['answer_origin'] == 'agent' and result['answers'] == CANONICAL
    assert app.agents.tree.get(cid, rid, parent['id'])['status'] == 'interrupted'
    assert not app.store.query("SELECT * FROM agent_inputs WHERE origin='human'")


@pytest.mark.parametrize('path', ['agent', 'human'])
def test_child_tool_result_and_receipt_keep_actual_answer_provenance(env, path):
    app = env.app
    cid, rid = start_root(app, env.project)
    child = spawn(env, cid, rid, 'provenance-child', question_script)
    qid = eventually(lambda: question_of(app, child['id']), 'Child did not ask the root.')
    if path == 'human':
        form = root_tool(app, env.project, cid, rid, 'PromoteAgentQuestion', {'question_id': qid}, 'promote')['question_id']
        with pytest.raises(PolicyError, match='no longer pending'):
            root_tool(app, env.project, cid, rid, 'AnswerAgentQuestion', {'question_id': qid, 'answers': json.dumps(ANSWER)}, 'agent-over-form')
        assert app.question_answer(cid, form, 1, ANSWER)['status'] == 'answered'
    else:
        root_tool(app, env.project, cid, rid, 'AnswerAgentQuestion', {'question_id': qid, 'answers': json.dumps(ANSWER)}, 'answer')
    settled(app, cid, rid, child['id'], 'completed')
    result = json.loads(next(m['content'] for m in checkpoint(app, child['id']) if m['role'] == 'tool'))
    assert result['answer_origin'] == path and result['answers'] == CANONICAL
    assert result['origin'] == 'agent' and result['authorization'] == 'none'
    receipt = ask_receipt(app, cid, rid, child['id'])
    assert receipt['status'] == 'answered' and receipt['answer_origin'] == path
    detail = app.dispatch('agent_get', {'chat_id': cid, 'run_id': rid, 'agent_id': child['id']})
    assert [(q['status'], q['answer_origin']) for q in detail['questions']] == [('answered', path)]
    human = app.store.query("SELECT origin,status FROM agent_inputs WHERE agent_id=?", (child['id'],))
    assert human == ([{'origin': 'human', 'status': 'delivered'}] if path == 'human' else [])


def test_small_window_stages_default_child_tools_and_keeps_delegation_and_communication(env):
    app = env.app
    cid, rid = start_root(app, env.project)
    task = 'Coordinate the delegated notes review and keep every clarification ordinary data. ' * 16

    def staged_parent(provider, step, _messages, tools, _cancel):
        names = {t['function']['name'] for t in tools}
        tree = app.agents.tree
        if step == 1:
            return call('SpawnAgent', {'task': 'Ask one scoped clarification.', 'name': 'staged-grandchild',
                        'tools': json.dumps(['AskParentQuestion']), 'max_steps': 2, 'max_seconds': 10,
                        'model_tokens': 16384}, 'spawn')
        if step == 2:
            return call('ReadRunEvidence', {}, 'discover')
        pending = tree.parent_questions(cid, rid, provider.agent_id)
        if pending and 'AnswerAgentQuestion' in names:
            return call('AnswerAgentQuestion', {'question_id': pending[0]['id'], 'answers': json.dumps(ANSWER)}, 'answer-' + str(step))
        kids = [n['id'] for n in tree.list(cid, rid) if n['parent_id'] == provider.agent_id and n['status'] in ACTIVE]
        if kids and 'WaitForAgents' in names:
            return call('WaitForAgents', {'agent_ids': json.dumps(kids)}, 'wait-' + str(step))
        return final('Delegated clarification handled within staged schemas.')

    def staged_grandchild(_provider, step, _messages, _tools, cancel):
        if step == 1:
            # Ask only after the parent's third request, so that request sees a live child.
            block_until(lambda: len(env.providers['staged-parent'].requests) >= 3, cancel)
            return call('AskParentQuestion', {'questions': json.dumps(QUESTIONS)}, 'question')
        return final('Grandchild continued with its parent answer.')

    env.scripts['staged-grandchild'] = staged_grandchild
    parent = spawn(env, cid, rid, 'staged-parent', staged_parent, tools=None, paths=('notes.py',), task=task,
                   steps=8, seconds=30, tokens=8 * 3 * PROFILE['context'])
    assert sorted(parent['tools']) == sorted(CHILD), 'Staging never narrows the admitted default capabilities.'
    settled(app, cid, rid, parent['id'], 'completed', timeout=15)
    grandchild = child_of(app, cid, rid, parent['id'], 'staged-grandchild')
    settled(app, cid, rid, grandchild['id'], 'completed')

    requests = env.providers['staged-parent'].requests
    offered = [{t['function']['name'] for t in request['tools']} for request in requests]
    assert offered[0] == {'Glob', 'Read', 'Grep', 'AskParentQuestion', 'SendAgentMessage', 'ProposePatch', 'SpawnAgent'}
    assert 'ReadRunEvidence' not in offered[1] and 'WaitForAgents' in offered[1]
    assert {'ReadRunEvidence', 'WaitForAgents', 'StopAgent', 'SendAgentMessage'} <= offered[2]
    assert any('AnswerAgentQuestion' in names for names in offered)
    assert all(len(names) < len(CHILD) for names in offered)
    full = [t for t in TOOLS if t['function']['name'] in CHILD]
    assert estimate_tokens([], requests[0]['tools']) < .6 * estimate_tokens([], full)
    for request in requests:
        goal = [m for m in request['messages'] if m.get('_pebble_current')]
        assert len(goal) == 1 and task.strip() in goal[0]['content'] and STAGING_NOTE in goal[0]['content']
        assert all(unit['protected'] and unit['selected'] for unit in request['manifest']['units'] if unit['category'] == 'goal')
    discovery = json.loads(next(m['content'] for m in checkpoint(app, parent['id']) if m['role'] == 'tool' and m['tool_call_id'] == 'discover'))
    assert discovery['status'] == 'not_executed' and discovery['tool'] == 'ReadRunEvidence'
    assert not any(r['name'] == 'ReadRunEvidence' for r in app.agents.tree.receipts(cid, rid, parent['id'], 1))
    assert [t['function']['name'] for t in env.providers['staged-grandchild'].requests[0]['tools']] == ['AskParentQuestion']
    answer = json.loads(next(m['content'] for m in checkpoint(app, grandchild['id']) if m['role'] == 'tool'))
    assert answer['answer_origin'] == 'agent' and answer['answers'] == CANONICAL


def test_small_window_keeps_explicit_narrow_pins_exact(env):
    app = env.app
    cid, rid = start_root(app, env.project)
    pins = ['Glob', 'Read', 'Grep', 'WebSearch', 'ReadURL', 'Weather']
    assert len(pins) == STAGE_LIMIT
    child = spawn(env, cid, rid, 'pinned-six', [final('Pinned schemas only.')], tools=pins)
    settled(app, cid, rid, child['id'], 'completed')
    request = env.providers['pinned-six'].requests[0]
    assert [t['function']['name'] for t in request['tools']] == [t['function']['name'] for t in TOOLS if t['function']['name'] in pins]
    goal = next(m for m in request['messages'] if m.get('_pebble_current'))
    assert STAGING_NOTE not in goal['content']


# Expired worker leases and the queued resume window.

def test_exiting_worker_with_expired_lease_interrupts_its_generation_and_branch(host, monkeypatch):
    app, project, tree = host.app, host.project, host.app.agents.tree
    (project / 'source.py').write_text('value = 1\n')
    cid, rid = start_root(app, project)
    parent = claimed(app, cid, rid, 'parent', ['Read', 'SpawnAgent', 'AskParentQuestion'], steps=4, seconds=30, tokens=65536)
    grandchild = claimed(app, cid, rid, 'grandchild', ['AskParentQuestion'], parent=parent)
    peer = claimed(app, cid, rid, 'peer', ['AskParentQuestion'])
    gq = ask(app, cid, rid, grandchild)['question_id']
    form = root_tool(app, project, cid, rid, 'PromoteAgentQuestion', {'question_id': gq}, 'promote-grandchild')['question_id']
    peer_question = ask(app, cid, rid, peer)['question_id']
    closed = []
    provider = SimpleNamespace(complete=lambda *args: call('Read', {'file_path': 'source.py'}, 'read'),
                               cancel=lambda: None, close=lambda: closed.append(True), last_usage={})
    app.agents.provider_factory = lambda *args, **kwargs: provider

    def expire_then_read(worker, _name, args, _call_id):
        # The lease lapses while the tool runs, so neither its receipt nor settlement can commit.
        app.store.query('UPDATE agent_claims SET expires=? WHERE agent_id=?', (time.time() - 1, worker['id']))
        return {'path': args['file_path'], 'untrusted_source': 'value = 1\n'}

    monkeypatch.setattr(app.agents, 'child_tool', expire_then_read)
    app.agents.drive({'cid': cid, 'rid': rid, 'id': parent.id, 'generation': 1, 'owner': parent.owner,
                      'cancel': threading.Event(), 'suspend': False})
    record = tree.get(cid, rid, parent.id)
    assert (record['status'], record['generation']) == ('interrupted', 1)
    assert (record['dispatches'], record['charged_tokens']) == (1, PROFILE['context'])
    assert tree.budget(cid, rid)['used']['model_tokens'] == PROFILE['context']
    assert [(r['call_id'], r['state']) for r in tree.receipts(cid, rid, parent.id, 1)] == [('1:read', 'uncertain')]
    assert not app.store.query('SELECT * FROM agent_claims WHERE agent_id=?', (parent.id,))
    assert tree.get(cid, rid, grandchild.id)['status'] == 'stopped'
    assert native_status(app, form) == 'cancelled' and cancelled_by(app, form) == 'host'
    assert tree.get(cid, rid, peer.id)['status'] == 'waiting_parent'
    assert [q['id'] for q in tree.parent_questions(cid, rid, rid)] == [peer_question]
    assert closed == [True]
    # Nothing replays; an explicit retry retires the uncertain read in a new generation.
    assert tree.retry(cid, rid, rid, parent.id)['generation'] == 2
    assert app.store.query('SELECT state FROM agent_receipts WHERE agent_id=?', (parent.id,)) == [{'state': 'interrupted'}]


def test_lease_fallback_never_takes_over_a_live_lease_fresh_owner_or_newer_generation(host):
    app, project, tree = host.app, host.project, host.app.agents.tree
    cid, rid = start_root(app, project)
    child = claimed(app, cid, rid, 'contested', ['AskParentQuestion'])
    stale = {'cid': cid, 'rid': rid, 'id': child.id, 'generation': 1, 'owner': child.owner}

    def claim():
        return app.store.query('SELECT owner,generation FROM agent_claims WHERE agent_id=?', (child.id,))

    # A live own lease belongs to the ordinary release path, not the fallback.
    assert tree.abandon(cid, rid, child.id, 1, child.owner) is None
    assert tree.get(cid, rid, child.id)['status'] == 'running'
    # Another owner's claim, fresh or expired, is never taken over by a stale exit.
    for expires in (time.time() + 30, time.time() - 1):
        app.store.query('UPDATE agent_claims SET owner=?,expires=? WHERE agent_id=?', ('fresh-owner', expires, child.id))
        app.agents._release(stale)
        app.agents._finish(stale, 'failed', {'error': 'Stale worker exit.'})
        assert tree.get(cid, rid, child.id)['status'] == 'running'
        assert claim() == [{'owner': 'fresh-owner', 'generation': 1}]

    app.store.query('UPDATE agent_claims SET owner=?,expires=? WHERE agent_id=?', (child.owner, time.time() + 30, child.id))
    tree.release(cid, rid, child.id, 1, child.owner)
    assert tree.retry(cid, rid, rid, child.id)['generation'] == 2
    tree.claim(cid, rid, child.id, 2, 'new-owner')
    newer = tree.ask_parent(cid, rid, child.id, 2, 'new-owner', '1:ask', QUESTIONS)['question_id']
    # The stale generation-one exit never touches the newer generation, even once its lease lapses.
    for expires in (time.time() + 30, time.time() - 1):
        app.store.query('UPDATE agent_claims SET expires=? WHERE agent_id=?', (expires, child.id))
        app.agents._release(stale)
        assert tree.abandon(cid, rid, child.id, 1, child.owner) is None
        record = tree.get(cid, rid, child.id)
        assert (record['generation'], record['status']) == (2, 'waiting_parent')
        assert claim() == [{'owner': 'new-owner', 'generation': 2}]
        assert tree.parent_question(cid, rid, rid, newer)['status'] == 'pending'
    # With no claim left, only the matching generation can close itself.
    app.store.query('DELETE FROM agent_claims WHERE agent_id=?', (child.id,))
    assert tree.abandon(cid, rid, child.id, 1, child.owner) is None
    closed = tree.abandon(cid, rid, child.id, 2, 'new-owner')
    assert (closed['status'], closed['generation']) == ('interrupted', 2)
    assert tree.parent_question(cid, rid, rid, newer)['status'] == 'cancelled'


@pytest.mark.parametrize('parked_state', ['queued', 'waiting_approval'])
def test_queued_resume_keeps_child_coordination_live_and_root_admission_running_only(host, parked_state):
    app, project, tree = host.app, host.project, host.app.agents.tree
    cid, rid = start_root(app, project)
    waiting = claimed(app, cid, rid, 'waiting', ['AskParentQuestion'])
    peer = claimed(app, cid, rid, 'peer', ['Read', 'SendAgentMessage'])
    assert tree.wait(cid, rid, rid, 'root-wait', [peer.id])['status'] == 'awaiting_agent_results'
    app.runs.transition(rid, 'waiting_input')
    app.runs.transition(rid, parked_state)
    worker = {'cid': cid, 'rid': rid, 'id': waiting.id, 'generation': 1, 'owner': waiting.owner,
              'cancel': threading.Event(), 'suspend': False}
    assert app.agents._check(worker)['status'] == 'running'
    assert app.agents._host_stopped(worker) is False
    renewed = tree.heartbeat(cid, rid, waiting.id, 1, waiting.owner)
    assert renewed['generation'] == 1 and renewed['expires'] > time.time()
    qid = ask(app, cid, rid, waiting)['question_id']
    assert app.agents._check(worker)['status'] == 'waiting_parent'
    assert tree.reserve_model(cid, rid, peer.id, 1, 1024, 0, 'queued-child')['admitted']
    tree.finish_model(cid, rid, peer.id, 1, 'queued-child')
    assert tree.begin_tool(cid, rid, peer.id, 1, peer.owner, '1:read', 'Read', {'file_path': 'notes.py'}) is None
    tree.end_tool(cid, rid, peer.id, 1, peer.owner, '1:read', {'path': 'notes.py', 'untrusted_source': 'notes'})
    assert tree.send(cid, rid, peer.id, rid, 'Ordinary progress note.', generation=1, owner=peer.owner)['origin'] == 'agent'
    # The ordinary question woke the registered root wait while the root was queued.
    ready = tree.wait(cid, rid, rid, 'root-wait', [peer.id])
    assert ready['status'] == 'ready' and ready['question_ids'] == [qid]
    assert app.store.query("SELECT origin FROM run_inbox WHERE run_id=? AND kind='agent_wait_ready'", (rid,)) == [{'origin': 'agent'}]
    # Root inference and new delegation still require a running root.
    with pytest.raises(ValueError, match='running root'):
        tree.reserve_model(cid, rid, rid, 0, 1024, 0, 'root-while-queued')
    with pytest.raises(ValueError, match='running root'):
        tree.spawn(cid, rid, None, 'Late delegated task.', 'late', ['Read'], {'max_steps': 1, 'max_seconds': 5, 'model_tokens': 4096})

    app.runs.transition(rid, 'running')
    answered = root_tool(app, project, cid, rid, 'AnswerAgentQuestion', {'question_id': qid, 'answers': json.dumps(ANSWER)}, 'answer-after-resume')
    assert answered['answer_origin'] == 'agent'
    assert app.agents.wait_question(worker, qid, time.monotonic() + 5)['answer_origin'] == 'agent'
    record = tree.get(cid, rid, waiting.id)
    assert (record['status'], record['generation']) == ('running', 1)
    assert app.store.query('SELECT owner,generation FROM agent_claims WHERE agent_id=?', (waiting.id,)) == [{'owner': waiting.owner, 'generation': 1}]


def test_heartbeat_detected_lease_expiry_interrupts_only_the_stale_branch(env, monkeypatch):
    app = env.app
    monkeypatch.setattr(app.agents, 'heartbeat_seconds', .05)
    cid, rid = start_root(app, env.project)

    def supervisor(_provider, step, _messages, _tools, _cancel):
        if step == 1:
            return call('SpawnAgent', {'task': 'Clarify the delegated detail.', 'name': 'stale-grandchild',
                        'tools': json.dumps(['AskParentQuestion']), 'max_steps': 2, 'max_seconds': 10,
                        'model_tokens': 16384}, 'spawn')
        return call('AskParentQuestion', {'questions': json.dumps(QUESTIONS)}, 'question')

    env.scripts['stale-grandchild'] = question_script
    parent = spawn(env, cid, rid, 'stale-parent', supervisor, tools=('SpawnAgent', 'AskParentQuestion'), seconds=20)
    peer = spawn(env, cid, rid, 'fresh-peer', question_script)
    pq = eventually(lambda: question_of(app, parent['id']), 'Parent did not ask the root.')
    grandchild = child_of(app, cid, rid, parent['id'], 'stale-grandchild')
    gq = eventually(lambda: question_of(app, grandchild['id']), 'Grandchild did not ask its parent.')
    peer_question = eventually(lambda: question_of(app, peer['id']), 'Peer did not ask the root.')
    own_form = root_tool(app, env.project, cid, rid, 'PromoteAgentQuestion', {'question_id': pq}, 'promote-stale-parent')['question_id']
    child_form = root_tool(app, env.project, cid, rid, 'PromoteAgentQuestion', {'question_id': gq}, 'promote-stale-grandchild')['question_id']
    before = app.agents.tree.get(cid, rid, parent['id'])
    with app.agents.lock:
        owner = app.agents.workers[parent['id']]['owner']
        peer_owner = app.agents.workers[peer['id']]['owner']
    # The live worker's own heartbeat discovers the lapsed lease.
    app.store.query('UPDATE agent_claims SET expires=? WHERE agent_id=? AND owner=?', (time.time() - 1, parent['id'], owner))

    record = settled(app, cid, rid, parent['id'], 'interrupted')
    assert record['generation'] == 1
    assert (record['dispatches'], record['charged_tokens']) == (before['dispatches'], before['charged_tokens'])
    assert not app.store.query('SELECT * FROM agent_claims WHERE agent_id=?', (parent['id'],))
    eventually(lambda: env.providers['stale-parent'].cancel_calls > 0, 'The expired worker transport was not cancelled.')
    assert native_status(app, own_form) == 'cancelled' and cancelled_by(app, own_form) == 'host'
    assert root_receipt(app, rid, 'promote-stale-parent')['answer_origin'] == 'host'
    settled(app, cid, rid, grandchild['id'], 'stopped')
    assert native_status(app, child_form) == 'cancelled' and cancelled_by(app, child_form) == 'host'
    # The independent peer keeps its own lease, generation, question and transport.
    assert app.store.query('SELECT owner,generation FROM agent_claims WHERE agent_id=?', (peer['id'],)) == [{'owner': peer_owner, 'generation': 1}]
    assert app.agents.tree.get(cid, rid, peer['id'])['status'] == 'waiting_parent'
    assert question_of(app, peer['id']) == peer_question
    assert env.providers['fresh-peer'].cancel_calls == 0
    root_tool(app, env.project, cid, rid, 'AnswerAgentQuestion', {'question_id': peer_question, 'answers': json.dumps(ANSWER)}, 'answer-peer')
    assert settled(app, cid, rid, peer['id'], 'completed')['generation'] == 1


@pytest.mark.parametrize('parked_state', ['queued', 'waiting_approval'])
def test_waiting_child_worker_survives_queued_resume_with_same_lease_and_question(env, monkeypatch, parked_state):
    app = env.app
    monkeypatch.setattr(app.agents, 'heartbeat_seconds', .05)
    checks, beats = [], []
    check, beat = app.agents._check, app.agents.tree.heartbeat

    def observed_check(worker):
        state = app.runs.get(worker['rid'])['state']
        try:
            node = check(worker)
        except BaseException as exc:
            checks.append((state, worker['id'], type(exc).__name__))
            raise
        checks.append((state, worker['id'], None))
        return node

    def observed_beat(cid_, rid_, ident, generation, owner, lease_seconds=30):
        state = app.runs.get(rid_)['state']
        try:
            lease = beat(cid_, rid_, ident, generation, owner, lease_seconds)
        except BaseException as exc:
            beats.append((state, ident, type(exc).__name__))
            raise
        beats.append((state, ident, None))
        return lease

    monkeypatch.setattr(app.agents, '_check', observed_check)
    monkeypatch.setattr(app.agents.tree, 'heartbeat', observed_beat)
    cid, rid = start_root(app, env.project)
    child = spawn(env, cid, rid, 'resume-child', question_script)
    qid = eventually(lambda: question_of(app, child['id']), 'Child did not ask the root.')
    with app.agents.lock:
        owner = app.agents.workers[child['id']]['owner']
    app.runs.transition(rid, 'waiting_input')
    app.runs.transition(rid, parked_state)

    def queued(log):
        return [entry for entry in list(log) if entry[0] == parked_state and entry[1] == child['id']]

    # Wait for the worker's own checks and lease renewals inside the queued window, not for elapsed time.
    eventually(lambda: len(queued(checks)) >= 2 and len(queued(beats)) >= 2,
               'The waiting worker did not check and renew while its root was queued.')
    assert all(entry[2] is None for entry in queued(checks) + queued(beats))
    with app.agents.lock:
        assert app.agents.workers[child['id']]['owner'] == owner
    record = app.agents.tree.get(cid, rid, child['id'])
    assert (record['status'], record['generation']) == ('waiting_parent', 1)
    lease = app.store.query('SELECT owner,generation,expires FROM agent_claims WHERE agent_id=?', (child['id'],))[0]
    assert (lease['owner'], lease['generation']) == (owner, 1) and lease['expires'] > time.time()
    assert question_of(app, child['id']) == qid
    assert env.providers['resume-child'].cancel_calls == 0

    app.runs.transition(rid, 'running')
    root_tool(app, env.project, cid, rid, 'AnswerAgentQuestion', {'question_id': qid, 'answers': json.dumps(ANSWER)}, 'answer-after-resume')
    record = settled(app, cid, rid, child['id'], 'completed')
    assert record['generation'] == 1 and record['result']['text'] == 'Continued with the ordinary clarification answer.'
    result = json.loads(next(m['content'] for m in checkpoint(app, child['id']) if m['role'] == 'tool'))
    assert result['answer_origin'] == 'agent' and result['answers'] == CANONICAL
