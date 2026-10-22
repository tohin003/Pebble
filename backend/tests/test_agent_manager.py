"""Real child executor threads with deterministic, isolated provider transports.

No model HTTP, cloud billing, shell, app launch, credential or plugin operation
is used. The scheduler, durable tree, native RPC/input paths, receipt journals,
root broker and project file application are the checked-in implementations.
"""

import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from pebble.app import App
from pebble.context_manifest import assemble_context
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError, Workspace, digest


PROFILE = {'id': 'fixture-cloud', 'kind': 'openai', 'name': 'Fixture provider',
           'url': 'https://unused.invalid', 'model': 'fixture', 'context': 8192,
           'budget': 4096, 'tools': True}
QUESTIONS = [{'id': 'scope', 'title': 'Which source should I inspect?',
              'options': [{'id': 'small', 'label': 'This source'}]}]
ANSWER = {'scope': {'option_id': 'small'}}


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
                                      'manifest': copy.deepcopy(projection.manifest),
                                      'thread': threading.current_thread().name})
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
                            guard_calls=[], root_calls=[], events=events, guard_action='allow')
    monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(PROFILE))

    def root_complete(*args, **kwargs):
        value.root_calls.append((args, kwargs))
        raise AssertionError('Executor fixtures must not make a root model request.')

    def guard(event, root, ident, **kwargs):
        value.guard_calls.append({'event': event, 'root': str(root), 'run_id': ident, **copy.deepcopy(kwargs)})
        return {'action': value.guard_action if kwargs.get('tool') == 'Write' else 'allow',
                'reason': 'Fixture Guard admission'}

    monkeypatch.setattr(app.model, 'complete', root_complete)
    monkeypatch.setattr(app.guard, 'handle', guard)
    app.agents.provider_factory = lambda *args, **kwargs: FakePinnedProvider(value, *args, **kwargs)
    permissions = copy.deepcopy(app.store.setting('permissions'))
    profiles = copy.deepcopy(app.store.setting('model_profiles', []))
    yield value
    try:
        assert app.store.setting('permissions') == permissions == DEFAULT
        assert app.store.setting('model_profiles', []) == profiles
    finally:
        app.active = app.active_run = None
        app.close()
    assert not app.agents.busy(), 'Fixture teardown must join every child worker.'
    assert value.root_calls == [], 'Native answers must not dispatch the root model in these fixtures.'


def root(env, chat=None):
    app = env.app
    for live in app.runs.list():
        if live['state'] in {'queued', 'running', 'retry_wait'}:
            app.runs.transition(live['id'], 'waiting_input')
    chat = chat or app.new_chat(str(env.project))
    config = app.runtime_config(chat, False, {'max_steps': 64, 'max_seconds': 180})
    rid = app.runs.create(chat['id'], 'Execute only explicitly scoped child fixture work.', config)
    app.runs.transition(rid, 'running')
    # Hold the root owner, so automatic continuation dispatch cannot call a model.
    app.active, app.active_run = chat['id'], rid
    app.task_policy = copy.deepcopy(config['permissions'])
    return chat['id'], rid


def root_tool(env, cid, rid, name, args, ident=None):
    app = env.app
    for live in app.runs.list():
        if live['id'] != rid and live['state'] in {'queued', 'running', 'retry_wait'}:
            app.runs.transition(live['id'], 'waiting_input')
    if app.runs.get(rid)['state'] == 'waiting_input':
        app.runs.transition(rid, 'running')
    app.active, app.active_run = cid, rid
    return app.execute(name, args, Workspace(env.project), cid, call_id=ident or name + '-' + str(time.monotonic_ns()))


def spawn(env, cid, rid, name, script, tools=('Read',), paths=(), task=None, context=None,
          steps=4, seconds=8, tokens=32768):
    env.scripts[name] = script
    return root_tool(env, cid, rid, 'SpawnAgent', {
        'task': task or 'Complete the scoped ' + name + ' task.', 'name': name,
        'tools': json.dumps(list(tools)), 'write_paths': json.dumps(list(paths)),
        'context': json.dumps(context or {}), 'max_steps': steps,
        'max_seconds': seconds, 'model_tokens': tokens,
    })


def node(env, cid, rid, child):
    return env.app.agents.tree.get(cid, rid, child['id'])


def settled(env, cid, rid, child, state='completed'):
    def terminal():
        record = node(env, cid, rid, child)
        if record['status'] in {'completed', 'failed', 'stopped', 'interrupted', 'budget_exhausted'} and record['status'] != state:
            pytest.fail('Unexpected child outcome: ' + str(record))
        with env.app.agents.lock:
            owned = child['id'] in env.app.agents.workers
        return record if record['status'] == state and not owned else None
    return eventually(terminal, 'Child did not settle: ' + str(node(env, cid, rid, child)))


def parent_question(env, cid, rid, child):
    return eventually(lambda: next((q for q in env.app.agents.tree.parent_questions(cid, rid, rid)
                                    if q['agent_id'] == child['id']), None),
                      'Child did not ask its parent: ' + str(node(env, cid, rid, child)))


def question_script(_provider, step, _messages, _tools, _cancel):
    return call('AskParentQuestion', {'questions': json.dumps(QUESTIONS)}, 'question') if step == 1 else final('Continued with the ordinary clarification answer.')


def checkpoint(env, child):
    return json.loads(env.app.store.query('SELECT messages FROM agent_checkpoints WHERE agent_id=? AND generation=?',
                                         (child['id'], child['generation']))[0]['messages'])


def patch_child(env, cid, rid, name='patch-worker', path='source.py'):
    original, replacement = 'value = 1\n', 'value = 2\n'
    (env.project / path).write_text(original)
    child = spawn(env, cid, rid, name, [
        call('Read', {'file_path': path}, 'read'),
        call('ProposePatch', {'file_path': path, 'content': replacement}, 'patch'),
        final('Proposed one replacement; the parent must verify and integrate it.'),
    ], tools=('Read', 'ProposePatch'), paths=(path,))
    record = settled(env, cid, rid, child)
    detail = env.app.dispatch('agent_get', {'chat_id': cid, 'run_id': rid, 'agent_id': child['id']})
    assert len(detail['patches']) == 1
    return child, record, detail['patches'][0]['id'], original, replacement


def test_child_threads_have_independent_contexts_capabilities_and_own_receipts(env):
    cid, rid = root(env)
    (env.project / 'alpha.py').write_text('alpha = 1\n')
    (env.project / 'beta.py').write_text('beta = 2\n')
    alpha = spawn(env, cid, rid, 'alpha', [call('Read', {'file_path': 'alpha.py'}, 'read'), final('Alpha result.')],
                  task='Read only alpha.py.', context={'hint': 'alpha-only-context'})
    beta = spawn(env, cid, rid, 'beta', [call('Read', {'file_path': 'beta.py'}, 'read'), final('Beta result.')],
                 task='Read only beta.py.', context={'hint': 'beta-only-context'})
    a, b = settled(env, cid, rid, alpha), settled(env, cid, rid, beta)
    assert a['evidence_ids'] == b['evidence_ids'] == ['1:read']
    assert a['result']['text'] == 'Alpha result.' and b['result']['text'] == 'Beta result.'
    for name, child, own, foreign in [('alpha', alpha, 'alpha', 'beta'), ('beta', beta, 'beta', 'alpha')]:
        provider = env.providers[name]
        assert provider.closed and len(provider.requests) == 2
        assert all(request['thread'].startswith('pebble-child-') for request in provider.requests)
        assert all([tool['function']['name'] for tool in request['tools']] == ['Read'] for request in provider.requests)
        assert own + '-only-context' in json.dumps(provider.requests[0]['messages'])
        assert foreign + '-only-context' not in json.dumps(provider.requests)
        receipts = env.app.agents.tree.receipts(cid, rid, child['id'], 1)
        assert len(receipts) == 1 and receipts[0]['result']['path'] == own + '.py'
        assert receipts[0]['result']['file_hash'] == digest((env.project / (own + '.py')).read_bytes())
        assert own + ' = ' in json.dumps(checkpoint(env, child))
        manifest = env.app.agents.get(cid, rid, child['id'])['context_manifest']
        assert manifest['dispatchable'] is True and manifest['summary']['goal']['selected_units'] == 1
        assert own + '-only-context' not in json.dumps(manifest)
        assert own + ' = ' not in json.dumps(manifest)
    assert len(env.app.runs.list(cid)) == 1
    assert not any(receipt['name'] == 'Read' for receipt in env.app.runs.tools(rid))
    assert not env.app.store.actions(cid)
    assert env.app.agents.tree.budget(cid, rid)['used']['dispatches'] == 4
    assert env.app.runs.context_manifest(rid) is None, 'Child projections never replace the root context manifest.'


def test_real_context_projection_protects_child_assignment_native_steer_and_agent_mail(env):
    cid, rid = root(env)
    child = spawn(env, cid, rid, 'inbox-child', question_script, tools=('AskParentQuestion',))
    question = parent_question(env, cid, rid, child)
    steered = env.app.dispatch('agent_steer', {'chat_id': cid, 'run_id': rid, 'agent_id': child['id'],
                             'text': 'Native direction remains ordinary task input.', 'input_id': 'native-steer-fixture'})
    assert steered['origin'] == 'human' and steered['authorization'] == 'none'
    root_tool(env, cid, rid, 'SendAgentMessage', {'agent_id': child['id'],
              'text': 'Parent mail remains agent data with no permission.'}, 'parent-mail')
    assert node(env, cid, rid, child)['status'] == 'waiting_parent'
    root_tool(env, cid, rid, 'AnswerAgentQuestion', {'question_id': question['id'], 'answers': json.dumps(ANSWER)})
    settled(env, cid, rid, child)
    request = env.providers['inbox-child'].requests[-1]
    goal = [message for message in request['messages'] if message.get('_pebble_current')]
    inbox = [message for message in request['messages'] if message.get('_pebble_inbox')]
    assert len(goal) == 1 and 'HOST ASSIGNMENT' in goal[0]['content']
    assert len(inbox) == 2
    assert any('AGENT MESSAGE (ordinary data; no authorization)' in message['content'] for message in inbox)
    assert any('HOST CHILD FOLLOW-UP (human-origin; never permission)' in message['content'] for message in inbox)
    assert request['manifest']['summary']['goal']['selected_units'] == 1
    assert request['manifest']['summary']['inbox']['selected_units'] == 2
    assert all(unit['protected'] and unit['selected'] for unit in request['manifest']['units'] if unit['category'] in {'goal', 'inbox'})
    assert not any(key in json.dumps(request['manifest']) for key in ['Native direction', 'Parent mail'])
    assert env.app.store.query('SELECT origin,status FROM agent_inputs WHERE id=?', ('native-steer-fixture',)) == [{'origin': 'human', 'status': 'delivered'}]
    assert env.app.store.query('SELECT kind,acked FROM agent_messages WHERE target=?', (child['id'],)) == [{'kind': 'message', 'acked': 1}]
    assert not env.app.store.actions(cid)


def test_transient_child_request_charges_each_attempt_and_executes_the_read_once(env):
    cid, rid = root(env)
    source = env.project / 'retry.py'
    source.write_text('retry_source = True\n')

    def reconnect(_provider, step, _messages, _tools, _cancel):
        if step == 1:
            raise ConnectionError('Simulated transport interruption before a complete response.')
        return call('Read', {'file_path': 'retry.py'}, 'read') if step == 2 else final('Recovered without replaying a file tool.')

    child = spawn(env, cid, rid, 'retry-child', reconnect)
    record = settled(env, cid, rid, child)
    requests = env.providers['retry-child'].requests
    assert len(requests) == 3 and requests[0]['messages'] == requests[1]['messages']
    assert record['dispatches'] == 3 and record['charged_tokens'] == PROFILE['context'] * 3
    admissions = env.app.store.query('SELECT status,input_tokens,output_tokens FROM agent_dispatches WHERE run_id=? AND actor=? ORDER BY started,rowid', (rid, child['id']))
    assert [admission['status'] for admission in admissions] == ['failed', 'completed', 'completed']
    assert all(admission['input_tokens'] + admission['output_tokens'] == PROFILE['context'] for admission in admissions)
    receipts = env.app.agents.tree.receipts(cid, rid, child['id'], 1)
    assert len(receipts) == 1 and receipts[0]['call_id'] == '1:read' and receipts[0]['name'] == 'Read'
    assert receipts[0]['result']['untrusted_source'] == 'retry_source = True\n'
    assert len([item for item in env.guard_calls if item['event'] == 'pre_tool_use' and item.get('tool') == 'Read']) == 1
    assert env.app.agents.tree.budget(cid, rid)['used']['model_tokens'] == PROFILE['context'] * 3
    assert source.read_text() == 'retry_source = True\n' and not env.app.store.actions(cid)


def test_native_permissions_and_model_mutation_are_blocked_with_waiting_root_and_live_child(env):
    cid, rid = root(env)
    child = spawn(env, cid, rid, 'configuration-child', question_script, tools=('AskParentQuestion',))
    parent_question(env, cid, rid, child)
    env.app.runs.transition(rid, 'waiting_input')
    env.app.active = env.app.active_run = None
    assert env.app.agents.busy(), 'The delegated worker alone must keep native configuration locked.'
    before = env.app.runs.get(rid)['config']
    changed_permissions = copy.deepcopy(DEFAULT)
    changed_permissions['mode'] = 'full'
    for method, params in [
        ('setting', {'key': 'permissions', 'value': changed_permissions}),
        ('model_select', {'profile_id': 'other-profile'}),
        ('model_save', {'kind': 'openai', 'name': 'Other route', 'url': 'https://unused.invalid', 'model': 'other'}),
        ('model_unload', {}),
    ]:
        with pytest.raises(ValueError, match='[Ss]top the (current )?task'):
            env.app.dispatch(method, params)
    assert env.app.runs.get(rid)['config'] == before
    assert env.app.store.setting('permissions') == DEFAULT
    assert node(env, cid, rid, child)['status'] == 'waiting_parent'
    assert env.providers['configuration-child'].cancel_calls == 0
    assert not env.app.store.actions(cid)


@pytest.mark.parametrize('answer_origin', ['human', 'agent'])
def test_parent_or_native_clarification_resumes_child_without_permission_authority(env, answer_origin):
    cid, rid = root(env)
    child = spawn(env, cid, rid, 'clarifying-child', question_script, tools=('AskParentQuestion',))
    question = parent_question(env, cid, rid, child)
    sibling = spawn(env, cid, rid, 'independent-sibling', [final('Independent work completed while the child waited.')])
    settled(env, cid, rid, sibling)
    assert node(env, cid, rid, child)['status'] == 'waiting_parent'
    assert env.app.runs.get(rid)['state'] == 'running'
    if answer_origin == 'human':
        promoted = root_tool(env, cid, rid, 'PromoteAgentQuestion', {'question_id': question['id']}, 'promote')
        native = env.app.interactions.question(promoted['question_id'], cid)
        assert native['required'] is False and native['requester_agent_id'] == child['id']
        assert native['requester_agent_name'] == 'clarifying-child'
        assert node(env, cid, rid, child)['status'] == 'waiting_input'
        assert env.app.runs.get(rid)['state'] == 'running'
        assert env.app.question_answer(cid, native['id'], native['revision'], ANSWER)['status'] == 'answered'
    else:
        answered = root_tool(env, cid, rid, 'AnswerAgentQuestion', {'question_id': question['id'], 'answers': json.dumps(ANSWER)})
        assert answered['origin'] == 'agent' and answered['authorization'] == 'none'
        assert not env.app.interactions.questions(cid)
    record = settled(env, cid, rid, child)
    assert record['result']['text'] == 'Continued with the ordinary clarification answer.'
    messages = checkpoint(env, child)
    tool_result = json.loads(next(message['content'] for message in messages if message['role'] == 'tool'))
    assert tool_result['status'] == 'answered'
    assert tool_result['origin'] == 'agent' and tool_result['authorization'] == 'none'
    human_inputs = env.app.store.query('SELECT origin,status,body FROM agent_inputs WHERE agent_id=?', (child['id'],))
    if answer_origin == 'human':
        assert len(human_inputs) == 1 and human_inputs[0]['origin'] == 'human'
        assert human_inputs[0]['status'] == 'delivered'
        assert any('HOST CHILD FOLLOW-UP (human-origin; never permission)' in (message.get('content') or '') for message in messages)
        inbox = env.app.store.query('SELECT origin,payload FROM run_inbox WHERE run_id=?', (rid,))
        assert len(inbox) == 1 and inbox[0]['origin'] == 'human'
        assert env.app.interactions.question(native['id'], cid)['status'] == 'answered'
    else:
        assert human_inputs == []
        assert not env.app.store.query('SELECT * FROM run_inbox WHERE run_id=?', (rid,))
        assert not env.app.store.query('SELECT * FROM messages WHERE chat_id=?', (cid,))
    assert not env.app.store.actions(cid)
    assert env.app.store.setting('permissions')['mode'] == 'ask'


def test_stopped_child_rejects_late_native_answer_and_revokes_its_form(env):
    cid, rid = root(env)
    child = spawn(env, cid, rid, 'stopped-child', question_script, tools=('AskParentQuestion',))
    question = parent_question(env, cid, rid, child)
    promoted = root_tool(env, cid, rid, 'PromoteAgentQuestion', {'question_id': question['id']}, 'promote-stop')
    cancelled = env.app.dispatch('agent_cancel', {'chat_id': cid, 'run_id': rid, 'agent_id': child['id']})
    assert cancelled['agent_ids'] == [child['id']] and cancelled['status'] == 'stopped'
    settled(env, cid, rid, child, 'stopped')
    assert env.app.interactions.question(promoted['question_id'], cid)['status'] == 'cancelled'
    with pytest.raises(ValueError, match='no longer accepting|no longer live'):
        env.app.question_answer(cid, promoted['question_id'], 1, ANSWER)
    assert not env.app.store.query('SELECT * FROM agent_inputs WHERE agent_id=?', (child['id'],))
    assert not env.app.store.query('SELECT * FROM run_inbox WHERE run_id=?', (rid,))
    assert len(env.providers['stopped-child'].requests) == 1
    assert env.providers['stopped-child'].cancel_calls > 0


@pytest.mark.parametrize('change_when', ['before_review', 'after_review'])
def test_parent_rejects_stale_child_source_and_stale_patch_baseline(env, change_when):
    cid, rid = root(env)
    child, record, patch_id, original, _replacement = patch_child(env, cid, rid)
    assert (env.project / 'source.py').read_text() == original
    review_args = {'agent_id': child['id'], 'generation': 1, 'evidence_ids': json.dumps(record['evidence_ids'])}
    if change_when == 'after_review':
        assert root_tool(env, cid, rid, 'ReviewAgentResult', review_args)['review'] == 'accepted'
    changed = 'value = 99  # changed outside the proposal\n'
    (env.project / 'source.py').write_text(changed)
    if change_when == 'before_review':
        with pytest.raises(PolicyError, match='stale'):
            root_tool(env, cid, rid, 'ReviewAgentResult', review_args)
        assert node(env, cid, rid, child)['review'] == 'pending'
    else:
        with pytest.raises(PolicyError, match='changed since this proposal'):
            root_tool(env, cid, rid, 'IntegrateAgentPatch', {'patch_id': patch_id})
    assert (env.project / 'source.py').read_text() == changed
    assert env.app.store.query('SELECT status FROM agent_patches WHERE id=?', (patch_id,)) == [{'status': 'proposed'}]
    assert not env.app.store.actions(cid)


def test_child_patch_is_data_until_parent_review_guard_and_native_ask_mode_approval(env):
    cid, rid = root(env)
    child, record, patch_id, original, replacement = patch_child(env, cid, rid)
    assert (env.project / 'source.py').read_text() == original
    assert not env.app.store.actions(cid)
    proposal = next(receipt for receipt in env.app.agents.tree.receipts(cid, rid, child['id'], 1) if receipt['name'] == 'ProposePatch')
    assert proposal['result']['applied'] is False and proposal['result']['authorization'] == 'none'
    with pytest.raises(PolicyError, match='Accept the settled child evidence'):
        root_tool(env, cid, rid, 'IntegrateAgentPatch', {'patch_id': patch_id})
    accepted = root_tool(env, cid, rid, 'ReviewAgentResult', {'agent_id': child['id'], 'generation': 1,
                         'evidence_ids': json.dumps(record['evidence_ids'])})
    assert accepted['review'] == 'accepted'
    integrated = root_tool(env, cid, rid, 'IntegrateAgentPatch', {'patch_id': patch_id}, 'integrate')
    assert integrated['status'] == 'awaiting_user_approval'
    assert (env.project / 'source.py').read_text() == original
    action = env.app.store.actions(cid)[0]
    assert action['kind'] == 'edit' and action['status'] == 'pending'
    assert action['payload']['permission_mode'] == 'ask'
    assert action['payload']['baseline'] == digest(original.encode())
    assert action['payload']['after'] == replacement
    assert any(item['event'] == 'pre_tool_use' and item.get('tool') == 'Write' and item['run_id'] == rid for item in env.guard_calls)
    assert env.app.store.query('SELECT status FROM agent_patches WHERE id=?', (patch_id,)) == [{'status': 'broker_proposed'}]
    # The native broker performs the only project write, retaining its root receipt.
    env.app.active = env.app.active_run = None
    applied = env.app.approve(cid, action['id'])
    assert applied['applied'] == 'source.py'
    assert (env.project / 'source.py').read_text() == replacement
    assert env.app.store.actions(cid)[0]['status'] == 'applied'
    receipt = next(item for item in env.app.runs.tools(rid) if item['call_id'] == 'integrate')
    assert receipt['result']['status'] == 'applied'
    assert receipt['result']['action_id'] == action['id']
    with pytest.raises(PolicyError, match='already integrated'):
        root_tool(env, cid, rid, 'IntegrateAgentPatch', {'patch_id': patch_id})
    assert len(env.app.store.actions(cid)) == 1


def test_guard_denial_blocks_parent_patch_broker_and_keeps_project_unchanged(env):
    cid, rid = root(env)
    child, record, patch_id, original, _replacement = patch_child(env, cid, rid)
    root_tool(env, cid, rid, 'ReviewAgentResult', {'agent_id': child['id'], 'generation': 1,
              'evidence_ids': json.dumps(record['evidence_ids'])})
    env.guard_action = 'block'
    with pytest.raises(PolicyError, match='AgentGuard'):
        root_tool(env, cid, rid, 'IntegrateAgentPatch', {'patch_id': patch_id})
    assert (env.project / 'source.py').read_text() == original
    assert not env.app.store.actions(cid)
    assert any(item.get('tool') == 'Write' and item['event'] == 'pre_tool_use' for item in env.guard_calls)


@pytest.mark.parametrize('path', ['leased.py', './leased.py'])
def test_root_write_is_blocked_while_a_child_holds_the_exact_path_lease(env, path):
    cid, rid = root(env)
    source = env.project / 'leased.py'
    source.write_text('original = True\n')
    child = spawn(env, cid, rid, 'lease-holder', question_script, tools=('AskParentQuestion',), paths=('leased.py',))
    parent_question(env, cid, rid, child)
    with pytest.raises(PolicyError, match='delegated path lease|leased by a child'):
        root_tool(env, cid, rid, 'Write', {'file_path': path, 'content': 'changed = True\n'})
    assert source.read_text() == 'original = True\n'
    assert not env.app.store.actions(cid)
    assert not any(item.get('tool') == 'Write' for item in env.guard_calls)


@pytest.mark.parametrize('method', ['agent_list', 'agent_get', 'agent_steer', 'agent_cancel'])
def test_native_agent_rpc_rejects_cross_chat_scope_without_delivery_or_cancellation(env, method):
    cid, rid = root(env)
    child = spawn(env, cid, rid, 'scoped-child', question_script, tools=('AskParentQuestion',))
    parent_question(env, cid, rid, child)
    foreign = env.app.new_chat(str(env.project))['id']
    args = {'chat_id': foreign, 'run_id': rid}
    if method != 'agent_list':
        args['agent_id'] = child['id']
    if method == 'agent_steer':
        args['text'] = 'Unrelated chat task input.'
    with pytest.raises(ValueError, match='conversation'):
        env.app.dispatch(method, args)
    assert node(env, cid, rid, child)['status'] == 'waiting_parent'
    assert not env.app.store.query('SELECT * FROM agent_inputs WHERE agent_id=?', (child['id'],))
    assert env.providers['scoped-child'].cancel_calls == 0
    packet = env.app.dispatch('agent_list', {'chat_id': cid, 'run_id': rid})
    assert packet['root_run_id'] == rid and packet['runtime'] == {'local_lane': 1, 'cloud_limit': 3}


@pytest.mark.parametrize('forbidden', ['Write', 'RunCommand', 'OpenApp', 'PluginCall'])
def test_child_provider_cannot_expand_tools_into_private_or_native_effects(env, forbidden):
    cid, rid = root(env)
    source = env.project / 'untouched.py'
    source.write_text('unchanged = True\n')
    arguments = {'Write': {'file_path': 'untouched.py', 'content': 'bad = True'},
                 'RunCommand': {'command': 'touch unwanted'}, 'OpenApp': {'name': 'Terminal'},
                 'PluginCall': {'server': 'private', 'tool': 'credentials', 'arguments': '{}'}}[forbidden]
    child = spawn(env, cid, rid, 'forbidden-child', [call(forbidden, arguments)], tools=('Read',))
    record = settled(env, cid, rid, child, 'failed')
    assert 'allowed schema' in record['result']['error']
    assert source.read_text() == 'unchanged = True\n'
    assert not (env.project / 'unwanted').exists()
    assert not env.app.store.actions(cid)
    assert not env.app.agents.tree.receipts(cid, rid, child['id'], 1)
    assert not env.app.store.query('SELECT * FROM agent_patches WHERE run_id=?', (rid,))
    assert not any(item.get('tool') == forbidden for item in env.guard_calls)


@pytest.mark.parametrize('target_scope,operation', [('branch', 'agent_cancel'), ('root', 'agent_cancel'), ('root', 'app_stop')])
def test_cancellation_cascades_owned_descendants_and_preserves_unrelated_workers(env, target_scope, operation):
    cid, rid = root(env)
    env.scripts['grandchild'] = question_script

    def supervisor(_provider, step, _messages, _tools, _cancel):
        if step == 1:
            return call('SpawnAgent', {'task': 'Clarify the descendant task.', 'name': 'grandchild',
                        'tools': json.dumps(['AskParentQuestion']), 'max_steps': 2, 'max_seconds': 6, 'model_tokens': 16384}, 'spawn')
        return call('AskParentQuestion', {'questions': json.dumps(QUESTIONS)}, 'question')

    owner = spawn(env, cid, rid, 'supervisor', supervisor, tools=('SpawnAgent', 'AskParentQuestion'), seconds=10)
    parent_question(env, cid, rid, owner)
    descendant = eventually(lambda: next((item for item in env.app.agents.tree.list(cid, rid) if item['parent_id'] == owner['id']), None), 'Supervisor did not dispatch its grandchild.')
    eventually(lambda: node(env, cid, rid, descendant)['status'] == 'waiting_parent', 'Grandchild did not reach its question wait.')
    sibling = spawn(env, cid, rid, 'same-root-peer', question_script, tools=('AskParentQuestion',))
    sibling_question = parent_question(env, cid, rid, sibling)
    other_cid, other_rid = root(env)
    other = spawn(env, other_cid, other_rid, 'other-root-peer', question_script, tools=('AskParentQuestion',))
    other_question = parent_question(env, other_cid, other_rid, other)
    target = owner['id'] if target_scope == 'branch' else rid
    expected = {owner['id'], descendant['id']} | ({sibling['id']} if target_scope == 'root' else set())
    if operation == 'agent_cancel':
        result = env.app.dispatch('agent_cancel', {'chat_id': cid, 'run_id': rid, 'agent_id': target})
        assert set(result['agent_ids']) == expected
    else:
        env.app.active, env.app.active_run = cid, rid
        assert env.app.dispatch('cancel', {'id': cid, 'run_id': rid}) is True
    for child in [owner, descendant] + ([sibling] if target_scope == 'root' else []):
        settled(env, cid, rid, child, 'stopped')
    assert node(env, other_cid, other_rid, other)['status'] == 'waiting_parent'
    assert env.providers['other-root-peer'].cancel_calls == 0
    if target_scope == 'branch':
        assert node(env, cid, rid, sibling)['status'] == 'waiting_parent'
        assert env.providers['same-root-peer'].cancel_calls == 0
        root_tool(env, cid, rid, 'AnswerAgentQuestion', {'question_id': sibling_question['id'], 'answers': json.dumps(ANSWER)})
        settled(env, cid, rid, sibling)
    if operation != 'app_stop':
        root_tool(env, other_cid, other_rid, 'AnswerAgentQuestion', {'question_id': other_question['id'], 'answers': json.dumps(ANSWER)})
        settled(env, other_cid, other_rid, other)
    assert env.providers['other-root-peer'].cancel_calls == 0
    assert env.app.runs.get(other_rid)['state'] == 'running'
    assert not env.app.store.actions(cid) and not env.app.store.actions(other_cid)
