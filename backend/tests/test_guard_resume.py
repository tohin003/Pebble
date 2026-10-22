"""AgentGuard task continuity across durable waits, native approvals and restart.

The App runner, native question and approval routes, run journal, broker,
GuardAdapter and vendored AgentGuard engine are the checked-in code. Only the
root model (Providers.complete) and the approved test action's sandboxed
subprocess (run_tests) are scripted. No model HTTP, network, shell or
credential is used. No fixture returns a Guard decision: every verdict comes
from the engine's own completion gate over the task state it tracked.
"""

import copy
import json
import time
from types import SimpleNamespace

import pytest

import pebble.app as app_module
from pebble.app import App
from pebble.guard import GuardAdapter
from pebble.permissions import DEFAULT
from pebble.policy import Workspace
from pebble.providers import Providers


GOAL = 'Update calc.py so add documents its behaviour.'
SOURCE = 'def add(a, b):\n    return a + b\n'
CHANGED = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'
TESTS = ('import unittest\n\nfrom calc import add\n\n\n'
         'class AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n')
RULE = '-' * 70
PASSED = {'exit_code': 0, 'output': 'test_add (test_calc.AddTest.test_add) ... ok\n\n'
          + RULE + '\nRan 1 test in 0.001s\n\nOK\n'}
FAILED = {'exit_code': 1, 'output': 'test_add (test_calc.AddTest.test_add) ... FAIL\n\n'
          + RULE + '\nRan 1 test in 0.001s\n\nFAILED (failures=1)\n'}
ITEMS = [{'id': 'style', 'title': 'Which docstring style?', 'options': [{'id': 'short', 'label': 'One line'}]}]


def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text='Updated calc.py.'):
    return {'role': 'assistant', 'content': text}


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text(SOURCE)
    (project / 'test_calc.py').write_text(TESTS)
    value = SimpleNamespace(project=project, home=tmp_path / 'data', requests=[], script=[],
                            results=[], test_roots=[], apps=[])

    # Class-level patches also cover an App constructed after a simulated
    # restart, whose continuation dispatcher starts inside App.__init__.
    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        assert len(value.requests) <= len(value.script), 'Unscripted root model request.'
        response = copy.deepcopy(value.script[len(value.requests) - 1])
        if response.get('content'):
            token(response['content'])
        return response

    def run_tests(payload, cancel):
        value.test_roots.append(payload['root'])
        assert payload['recipe'] == 'python-unittest'
        return copy.deepcopy(value.results.pop(0))

    monkeypatch.setattr(Providers, 'complete', complete)
    monkeypatch.setattr(app_module, 'run_tests', run_tests)

    def start(mode='ask'):
        app = App(value.home, tmp_path / 'resources', lambda *args: None)
        value.apps.append(app)
        assert app.guard.guard is not None, app.guard.error
        if app.store.setting('permissions')['mode'] != mode:
            app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': mode})
        return app

    value.start = start
    yield value
    for app in value.apps:
        if not app.closed:
            app.active = app.active_run = None
            app.close()


def settled(env, app, rid, state, requests, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = app.runs.get(rid)
        if run['state'] == state and app.active is None and len(env.requests) == requests:
            return run
        time.sleep(.02)
    pytest.fail(f'Run did not settle as {state} after {requests} requests: '
                f'{app.runs.get(rid)["state"]} after {len(env.requests)} ({app.runs.get(rid)["error"]})')


def notes(app, rid, kind):
    return [json.loads(row['payload']) for row in app.store.query(
        'SELECT payload FROM run_events WHERE run_id=? AND kind=? ORDER BY sequence', (rid, kind))]


def pending(app, cid, kind):
    return next(a for a in app.store.actions(cid) if a['kind'] == kind and a['status'] == 'pending')


def guard_key(app, rid):
    return next(key for key in app.guard.tasks if key[1] == rid)


def proposal_script(after):
    return [call('Read', {'file_path': 'calc.py'}, 'read'),
            call('Write', {'file_path': 'calc.py', 'content': CHANGED}, 'write'),
            call('RunTests', {'recipe': 'python-unittest'}, 'tests'),
            final('Proposed the documented function and a test run.'), *after]


@pytest.mark.parametrize('outcome', ['failed', 'passed'])
def test_native_approval_continuation_keeps_the_guard_task_and_test_outcome(env, outcome):
    app = env.start()
    chat = app.new_chat(str(env.project))
    env.results = [FAILED if outcome == 'failed' else PASSED]
    env.script = proposal_script([final()] * 3)
    app.work(chat, GOAL, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, app, rid, 'waiting_approval', 4)
    key = guard_key(app, rid)
    task = app.guard.tasks[key]
    # Approvals run outside the model turn but are attributed to the run.
    app.approve(chat['id'], pending(app, chat['id'], 'edit')['id'])
    app.approve(chat['id'], pending(app, chat['id'], 'tests')['id'])
    assert env.test_roots == [str(Workspace(env.project).root)]
    if outcome == 'passed':
        settled(env, app, rid, 'completed', 5)
        assert notes(app, rid, 'guard_stop') == []
    else:
        # The failed approved test still reaches the resumed completion gate.
        # It keeps speaking after the runner's two repairs instead of muting.
        run = settled(env, app, rid, 'verification_required', 7)
        stops = notes(app, rid, 'guard_stop')
        assert len(stops) == 3 and all('verification_failed' in stop['reason'] for stop in stops)
        assert 'test run reported failures' in run['error']
    assert app.guard.tasks[key] == task
    assert not notes(app, rid, 'guard_task_rebuilt')
    assert (env.project / 'calc.py').read_text() == CHANGED


def test_required_question_continuation_still_requires_tests_for_changes_before_the_wait(env):
    app = env.start('auto')
    chat = app.new_chat(str(env.project))
    env.results = [PASSED]
    env.script = [call('Read', {'file_path': 'calc.py'}, 'read'),
                  call('Write', {'file_path': 'calc.py', 'content': CHANGED}, 'write'),
                  call('AskUserQuestion', {'questions': json.dumps(ITEMS), 'required': True}, 'ask'),
                  final(), call('RunTests', {'recipe': 'python-unittest'}, 'tests'), final()]
    app.work(chat, GOAL, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, app, rid, 'waiting_input', 3)
    assert (env.project / 'calc.py').read_text() == CHANGED
    task = app.guard.tasks[guard_key(app, rid)]
    question = app.interactions.questions(chat['id'])[0]
    app.question_answer(chat['id'], question['id'], 1, {'style': {'option_id': 'short'}})
    settled(env, app, rid, 'completed', 6)
    stops = notes(app, rid, 'guard_stop')
    assert len(stops) == 1 and 'incomplete' in stops[0]['reason'] and 'no tests were run' in stops[0]['reason']
    repair = [m['content'] for m in env.requests[4] if 'HOST COMPLETION CHECK' in str(m.get('content'))]
    assert len(repair) == 1 and 'no tests were run' in repair[0]
    assert app.guard.tasks[guard_key(app, rid)] == task
    assert len(env.test_roots) == 1


def test_restart_rebuilds_guard_scope_from_receipts_without_claiming_prior_tests(env):
    first = env.start()
    chat = first.new_chat(str(env.project))
    env.results = [PASSED, PASSED]
    env.script = proposal_script([final(), call('RunTests', {'recipe': 'python-unittest'}, 'retest'),
                                  final('Waiting for the new test approval.'), final()])
    first.work(chat, GOAL, False, [])
    rid = first.runs.list(chat['id'])[0]['id']
    settled(env, first, rid, 'waiting_approval', 4)
    # Stop dispatch first, so the approved continuation is still queued when
    # the service stops. The live task saw a passing test before shutdown.
    assert first.controller.close()
    first.approve(chat['id'], pending(first, chat['id'], 'edit')['id'])
    first.approve(chat['id'], pending(first, chat['id'], 'tests')['id'])
    assert first.guard._task(guard_key(first, rid)).verification.all_passed
    assert first.interactions.continuation(rid)['state'] == 'queued'
    first.close()
    second = env.start()
    settled(env, second, rid, 'waiting_approval', 7)
    [rebuilt] = notes(second, rid, 'guard_task_rebuilt')
    assert rebuilt == {'state': 'rebuilt', 'restored_paths': 1, 'verification': 'not_restored'}
    stops = notes(second, rid, 'guard_stop')
    assert len(stops) == 1 and 'incomplete' in stops[0]['reason'] and 'no tests were run' in stops[0]['reason']
    second.approve(chat['id'], pending(second, chat['id'], 'tests')['id'])
    settled(env, second, rid, 'completed', 8)
    assert len(notes(second, rid, 'guard_stop')) == 1 and len(env.test_roots) == 2
    assert len(notes(second, rid, 'guard_task_rebuilt')) == 1


# -- Phase 24 (V1-029): a run held at verification_required -----------------------------------------------------------
# Resume used to repeat the verdict with no repair (the snapshot's repair count was spent), and a follow-up message
# completed and hid the hold. The person now decides in the main window: run the tests (through the normal approval),
# ask Pebble to finish (exactly one fresh repair), or accept it as unverified (recorded with the person as origin).
# No assertion above changed: these tests are added beside them.

def held_run(env, mode='auto'):
    app = env.start(mode)
    chat = app.new_chat(str(env.project))
    env.script = [call('Read', {'file_path': 'calc.py'}, 'read'),
                  call('Write', {'file_path': 'calc.py', 'content': CHANGED}, 'write'),
                  final('Documented add().'), final('Done.'), final('Done.')]
    app.work(chat, GOAL, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    run = settled(env, app, rid, 'verification_required', 5)
    assert 'no tests were run' in run['error'] and len(notes(app, rid, 'guard_stop')) == 3
    return app, chat['id'], rid


def repairs(request):
    return [m for m in request if 'HOST COMPLETION CHECK' in str(m.get('content'))]


def test_ask_pebble_to_finish_resumes_a_held_run_with_exactly_one_repair(env):
    app, cid, rid = held_run(env)
    env.script += [final('Done.'), final('Done.')]
    assert app.controller.held(cid, rid, 'finish', surface='main')['status'] == 'queued'
    run = settled(env, app, rid, 'verification_required', 7)
    resumed, repaired = env.requests[5], env.requests[6]
    asked = [m['content'] for m in resumed if m.get('role') == 'user' and 'HOST INPUT (human-origin' in str(m.get('content'))]
    assert asked and 'The person asked you to finish this task.' in asked[-1]
    assert len(repairs(repaired)) == len(repairs(resumed)) + 1, 'exactly one fresh repair after the resume'
    assert len(notes(app, rid, 'guard_stop')) == 5 and 'no tests were run' in run['error']
    assert notes(app, rid, 'held_decision') == [{'decision': 'finish', 'origin': 'human', 'surface': 'main'}]


def test_run_the_tests_stages_the_covering_recipe_through_the_normal_approval(env, monkeypatch):
    from pebble import recipes
    ran = []

    def run_tests(payload, cancel):  # the host picks the recipe ('auto' here); any test recipe passes the fixture
        from pebble import testing
        ran.append(payload['recipe'])
        env.test_roots.append(payload['root'])
        return {**copy.deepcopy(PASSED), 'outcome': 'passed', 'kind': 'test', 'recipe': payload['recipe'],
                'tests': {'passed': 1, 'failed': 0, 'skipped': 0, 'errors': 0, 'total': 1},
                'snapshot_digest': testing.snapshot_digest(Workspace(payload['root']))}
    monkeypatch.setattr(app_module, 'run_tests', run_tests)
    app = env.start('ask')
    chat = app.new_chat(str(env.project))
    env.script = [call('Read', {'file_path': 'calc.py'}, 'read'),
                  call('Write', {'file_path': 'calc.py', 'content': CHANGED}, 'write'),
                  final('Proposed the documented function.'), final('Done.'), final('Done.'), final('Done.'),
                  final('The tests pass on the documented function.')]
    app.work(chat, GOAL, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, app, rid, 'waiting_approval', 3)
    app.approve(chat['id'], pending(app, chat['id'], 'edit')['id'])
    settled(env, app, rid, 'verification_required', 6)
    staged = app.controller.held(chat['id'], rid, 'tests', surface='main')
    assert staged['status'] == 'waiting_approval' and env.test_roots == []
    tests = pending(app, chat['id'], 'tests')
    assert tests['id'] == staged['action_id'] and recipes.REGISTRY[tests['payload']['recipe']].kind == 'test'
    assert app.store.query('SELECT run_id FROM run_action_owners WHERE action_id=?', (tests['id'],)) == [{'run_id': rid}]
    assert app.runs.get(rid)['state'] == 'waiting_approval'
    app.approve(chat['id'], tests['id'])
    run = settled(env, app, rid, 'completed', 7)
    assert env.test_roots == [str(Workspace(env.project).root)] and ran == [tests['payload']['recipe']]
    assert not run['error'] and len(notes(app, rid, 'guard_stop')) == 3


def test_accept_as_unverified_records_the_person_and_never_claims_verification(env):
    import sqlite3
    app, cid, rid = held_run(env)
    decided = app.controller.held(cid, rid, 'accept', surface='main')
    assert decided == {'run_id': rid, 'status': 'completed', 'verified': False, 'origin': 'human'}
    run = app.runs.get(rid)
    assert run['state'] == 'completed' and run['error'] == 'You accepted this task as unverified.'
    assert notes(app, rid, 'held_decision') == [{'decision': 'accept', 'origin': 'human', 'surface': 'main'}]
    items = app.activity.read(rid)['items']
    assert any(i['kind'] == 'note' and i['label'] == 'You accepted this as unverified' for i in items)
    checks = [i['label'] for i in items if i['kind'] == 'check']
    assert checks and all(label.startswith('Not verified · ') for label in checks), checks
    with sqlite3.connect(env.home / 'guard' / 'agentguard.db') as db:
        assert db.execute('SELECT ended_at FROM sessions WHERE id=?', (rid,)).fetchone()[0] is not None
    assert len(env.requests) == 5, 'accepting asks the model nothing'


def test_a_follow_up_run_keeps_the_unverified_path_and_is_held(env, monkeypatch):
    app, cid, rid = held_run(env)
    assert app.controller.carried_paths(cid, 'next-run') == ['calc.py']
    begin = app.guard.begin

    def carried(root, session, prompt, resume=False, changed=()):
        # The runner's side (t24-agent): a fresh run starts with the held run's unverified paths.
        if not resume:
            changed = app.controller.carried_paths(cid, session)
        return begin(root, session, prompt, resume=resume, changed=changed)
    monkeypatch.setattr(app.guard, 'begin', carried)
    env.script += [final('You are welcome.'), final('You are welcome.'), final('You are welcome.')]
    app.work(app.chat(cid), 'Thanks!', False, [])
    follow = app.runs.list(cid)[0]['id']
    assert follow != rid
    run = settled(env, app, follow, 'verification_required', 8)
    assert 'no tests were run' in run['error']


def test_held_decisions_are_main_window_only_and_only_for_a_held_run(env):
    from pebble.policy import PolicyError
    app, cid, rid = held_run(env)
    for surface in (None, 'pet', 'Main', 'main ', 'view', ''):
        with pytest.raises(PolicyError, match='main window'):
            app.controller.held(cid, rid, 'accept', surface=surface)
    with pytest.raises(ValueError, match='Choose tests, finish or accept'):
        app.controller.held(cid, rid, 'retry', surface='main')
    assert set(app.controller.held_methods()) == {'run_held_tests', 'run_held_finish', 'run_held_accept'}
    assert app.runs.get(rid)['state'] == 'verification_required' and notes(app, rid, 'held_decision') == []
    app.controller.held(cid, rid, 'accept', surface='main')
    with pytest.raises(ValueError, match='not waiting on verification'):
        app.controller.held(cid, rid, 'finish', surface='main')


def test_adapter_continuation_keeps_task_and_restart_restores_scope_only(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text(SOURCE)
    (project / 'test_calc.py').write_text(TESTS)
    root, rid, home = Workspace(project).root, 'run-fixture', tmp_path / 'data'
    guard = GuardAdapter(home)
    try:
        assert guard.guard is not None, guard.error
        assert guard.begin(root, rid, GOAL)['state'] == 'started'
        guard.stage(root, rid, 'calc.py', CHANGED)
        guard.handle('post_tool_use', root, rid, tool='Write',
                     arguments={'file_path': 'calc.py', 'content': CHANGED}, result='{}')
        guard.test_result(str(root), rid, 'python-unittest', FAILED)
        assert guard.begin(root, rid, GOAL, resume=True, changed=['calc.py']) == {'state': 'continued'}
        # Repeated completion attempts keep reporting the same failed test.
        for _ in range(3):
            decision = guard.handle('stop', root, rid, last_assistant_message='Done.')
            assert decision['action'] == 'block' and 'verification_failed' in decision['reason']
        guard.test_result(str(root), rid, 'python-unittest', PASSED)
    finally:
        guard.close()
    (project / 'calc.py').write_text(CHANGED)
    restarted = GuardAdapter(home)
    try:
        assert restarted.begin(root, rid, GOAL, resume=True, changed=['calc.py', './calc.py']) == {
            'state': 'rebuilt', 'restored_paths': 1, 'verification': 'not_restored'}
        decision = restarted.handle('stop', root, rid, last_assistant_message='Done.')
        assert decision['action'] == 'block' and 'incomplete' in decision['reason']
        restarted.test_result(str(root), rid, 'python-unittest', PASSED)
        assert restarted.handle('stop', root, rid, last_assistant_message='Done.')['action'] == 'allow'
    finally:
        restarted.close()
