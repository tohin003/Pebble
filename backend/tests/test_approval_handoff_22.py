"""Phase 22 (f22-approval): a decision that leaves its run ready returns only after the run has left its wait.

validate_phase20 (a grader) approves an edit, then the test run, then polls run_get, state and get_chat. A run that
reads waiting_approval while state()['active'] is empty and no action is pending counts as settled there, and so does
the person's view of it. App.approve used to return once its receipt was committed and only wake the continuation
thread. That thread's claim and run_resume then raced the caller's next reads, and the merged Phase 22 source lost the
race every time: its Workspace() now resolves Pebble's protected data folders, so the runtime_config check inside
run_resume takes 0.75-0.93 ms more (measured under the validator). The deciding call now dispatches the continuation
it queued (RunController.dispatch_now), with the dispatch loop's own task_lock, oldest-first claim and
run_resume(automatic=True). The run is queued with App.active set, or carries the reason it could not continue,
before the decision returns.

The person's other answers to a waiting run (question_answer, run_steer and artifact_submit) make the same promise
(follow-up).

Here the continuation thread's claim finds nothing until a test releases it, so the old window stays open on every
run: before the fix each test fails at its first check after the decision. The App runner, broker, approval and input
routes, run journal and continuation records are the checked-in code. Only the root model (Providers.complete) and the
approved test action's sandboxed subprocess (run_tests) are scripted. No model HTTP, network, shell or credential is
used.

Follow-up 2: the Workspace cost. data_home keeps the resolved protected folders, and a Workspace resolves its own root
once, as on main. The last tests check that every answer equals an uncached resolve while ~/.pebble appears and
becomes a link, a missing folder's parent is relinked, PEBBLE_HOME, the activated homes and HOME change. They also
pin the cost as a count of realpath calls rather than a time.
"""

import copy
import json
import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

import pebble.app as app_module
from pebble import data_home
from pebble.app import App
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError, Workspace
from pebble.providers import Providers
from test_high_fd_waits_21 import close_guard_databases

SOURCE = 'def add(a, b):\n    return a + b\n'
CHANGED = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'
TESTS = ('import unittest\n\nfrom calc import add\n\n\n'
         'class AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n')
PASSED = {'exit_code': 0, 'output': 'test_add (test_calc.AddTest.test_add) ... ok\n\n' + '-' * 70
          + '\nRan 1 test in 0.001s\n\nOK\n'}
NOTES = '# Notes\n\n`add(a, b)` returns the sum of a and b.\n'
TRANSIENT = {'queued', 'running', 'retry_wait'}  # validate_phase20's TRANSIENT
ITEMS = [{'id': 'style', 'title': 'Which note style?', 'options': [{'id': 'short', 'label': 'One line'}]}]
FORM = {'fields': [{'id': 'scope', 'label': 'Which scope?', 'type': 'choice', 'required': True,
                    'options': [{'id': 'small', 'label': 'Small'}, {'id': 'large', 'label': 'Large'}]}]}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('These tests must not open network connections.')
        return connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))


def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text(SOURCE)
    (project / 'test_calc.py').write_text(TESTS)
    value = SimpleNamespace(project=project, home=tmp_path / 'data', requests=[], script=[], results=[], tested=[],
                            apps=[], release=threading.Event())

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        assert len(value.requests) <= len(value.script), 'Unscripted root model request.'
        step = value.script[len(value.requests) - 1]
        response = step() if callable(step) else copy.deepcopy(step)
        if response.get('content'):
            token(response['content'])
        return response

    def run_tests(payload, cancel):
        value.tested.append(payload['root'])
        return copy.deepcopy(value.results.pop(0))

    monkeypatch.setattr(Providers, 'complete', complete)
    monkeypatch.setattr(app_module, 'run_tests', run_tests)

    def start(context=32768):
        app = App(value.home, tmp_path / 'resources', lambda *args: None)
        value.apps.append(app)
        if app.store.setting('permissions')['mode'] != 'ask':
            app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'ask'})
        if not app.model.current() or app.model.current().get('budget') != context:
            app.model_save(name='f22 fixture', url='http://127.0.0.1:9', model='f22-fixture-%d' % context,
                           context=context)
        assert app.model.current()['budget'] == context
        return app

    value.start = start
    yield value
    value.release.set()
    for app in value.apps:
        if not app.closed:
            app.active = app.active_run = None
            app.close()
    close_guard_databases(tmp_path)  # resource cleanup only (F21-fd)


def released(env, response):
    """A resumed request that answers once the test has read the state the decision returned with."""
    def step():
        assert env.release.wait(10), 'The test never released the resumed request.'
        return copy.deepcopy(response)
    return step


def hold_dispatch_thread(app, monkeypatch):
    """The continuation thread's claim finds nothing until the returned gate is set: the window between a decision
    and that thread's claim, held open. Every other caller's claim is the real one."""
    gate, claim = threading.Event(), app.controller.interactions.claim

    def held():
        if threading.current_thread() is app.controller.thread and not gate.is_set():
            return None
        return claim()

    monkeypatch.setattr(app.controller.interactions, 'claim', held)
    return gate


def observed(app, cid, rid):
    """What validate_phase20's settle loop reads after the decisions, in its order: run_get, state, get_chat."""
    state = app.run_get(cid, rid)['state']
    active = app.state()['active']
    waiting = [a['kind'] for a in app.chat(cid)['actions'] if a['status'] == 'pending']
    return state, active, waiting


def settled(env, app, rid, state, requests, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = app.runs.get(rid)
        if run['state'] == state and app.active is None and len(env.requests) == requests:
            return run
        time.sleep(.02)
    pytest.fail(f'Run did not settle as {state} after {requests} requests: '
                f'{app.runs.get(rid)["state"]} after {len(env.requests)} ({app.runs.get(rid)["error"]})')


def pending(app, cid, kind):
    return next(a for a in app.store.actions(cid) if a['kind'] == kind and a['status'] == 'pending')


def replies(app, cid):
    return [m['content'] for m in app.chat(cid)['messages'] if m['role'] == 'assistant' and 'FINAL:' in m['content']]


@pytest.mark.parametrize('context', [32768, 4096])  # validate_phase20 a1 (held reply) and a3 (the 4K path)
def test_the_last_approval_returns_after_its_run_left_waiting_approval(env, monkeypatch, context):
    app = env.start(context)
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.results = [PASSED]
    env.script = [call('Read', {'file_path': 'calc.py'}, 'read'),
                  call('Write', {'file_path': 'calc.py', 'content': CHANGED}, 'write'),
                  call('RunTests', {'recipe': 'python-unittest'}, 'tests'),
                  final('The change and the test run await your approval.\nFINAL: staged'),
                  released(env, final('The docstring is applied and the tests passed.\nFINAL: done'))]
    app.work(chat, 'Document add in calc.py and run the tests. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 4)
    gate = hold_dispatch_thread(app, monkeypatch)
    app.approve(cid, pending(app, cid, 'edit')['id'])
    # The test run still awaits a decision: nothing was queued, so nothing was dispatched and the lane is free.
    assert observed(app, cid, rid) == ('waiting_approval', None, ['tests'])
    assert app.interactions.continuation(rid) is None
    app.approve(cid, pending(app, cid, 'tests')['id'])
    state, active, waiting = observed(app, cid, rid)
    # Before the fix: ('waiting_approval', None, []), which the validator and the person read as settled.
    assert state in TRANSIENT and active == cid and waiting == [], (state, active, waiting)
    assert app.interactions.continuation(rid)['state'] == 'dispatched'
    gate.set()
    app.controller.wake()  # the released loop finds nothing more to claim
    env.release.set()
    run = settled(env, app, rid, 'completed', 5)
    # Exactly one resumed request and one reply; the approved effects ran once each and were never replayed.
    assert run['error'] is None and len(env.requests) == 5
    assert env.tested == [str(env.project.resolve())] and (env.project / 'calc.py').read_text() == CHANGED
    assert [r.splitlines()[-1] for r in replies(app, cid) if 'FINAL: done' in r] == ['FINAL: done']
    assert app.interactions.continuation(rid)['state'] == 'dispatched' and app.interactions.continuation(rid)['error'] is None
    assert [a['status'] for a in app.store.actions(cid)] == ['applied', 'applied']


def test_a_rejection_that_leaves_the_run_ready_returns_after_it_left_waiting_approval(env, monkeypatch):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.script = [call('Write', {'file_path': 'notes.md', 'content': NOTES}, 'write'),
                  final('notes.md is staged for your approval.\nFINAL: staged'),
                  released(env, final('You declined the change, so notes.md was not written.\nFINAL: declined'))]
    app.work(chat, 'Write notes.md describing add in calc.py. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 2)
    gate = hold_dispatch_thread(app, monkeypatch)
    assert app.reject(cid, pending(app, cid, 'edit')['id']) is True
    state, active, waiting = observed(app, cid, rid)
    assert state in TRANSIENT and active == cid and waiting == [], (state, active, waiting)
    gate.set()
    env.release.set()
    run = settled(env, app, rid, 'completed', 3)
    assert run['error'] is None and not (env.project / 'notes.md').exists()
    assert [a['status'] for a in app.store.actions(cid)] == ['rejected']


def test_a_continuation_that_cannot_start_is_recorded_before_the_decision_returns(env, monkeypatch):
    # The decision's own effect stands; the refusal is the dispatch loop's own (a blocked continuation with its reason
    # on the run), recorded on the deciding call instead of after it. Nothing is retried or replayed.
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.script = [call('Write', {'file_path': 'notes.md', 'content': NOTES}, 'write'),
                  final('notes.md is staged for your approval.\nFINAL: staged')]
    app.work(chat, 'Write notes.md describing add in calc.py. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 2)
    gate = hold_dispatch_thread(app, monkeypatch)
    # The reviewed run configuration no longer matches (the permission version moved after the proposal).
    app.store.set_setting('permissions_version', app.policy_version() + 1)
    assert app.approve(cid, pending(app, cid, 'edit')['id'])['applied'] == 'notes.md'
    run = app.run_get(cid, rid)
    assert run['state'] == 'waiting_approval' and app.state()['active'] is None
    assert run['continuation']['state'] == 'blocked' and 'changed' in (run['error'] or ''), run['error']
    gate.set()
    app.controller.wake()
    time.sleep(.3)
    assert app.interactions.continuation(rid)['state'] == 'blocked' and len(env.requests) == 2
    assert (env.project / 'notes.md').read_text() == NOTES


# ---- The person's other answers to a waiting run (follow-up 1) --------------------------------------------------------

def test_an_answer_to_a_required_question_returns_after_the_run_left_waiting_input(env, monkeypatch):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.script = [call('AskUserQuestion', {'questions': json.dumps(ITEMS), 'required': True}, 'ask'),
                  released(env, final('One line it is.\nFINAL: answered'))]
    app.work(chat, 'Ask me which note style I want. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_input', 1)
    gate = hold_dispatch_thread(app, monkeypatch)
    question = app.interactions.questions(cid)[0]
    assert app.question_answer(cid, question['id'], question['revision'], {'style': {'option_id': 'short'}})['run_id'] == rid
    state, active, _ = observed(app, cid, rid)
    assert state in TRANSIENT and active == cid, (state, active)  # without its dispatch_now: ('waiting_input', None)
    gate.set()
    env.release.set()
    run = settled(env, app, rid, 'completed', 2)
    assert run['error'] is None and app.interactions.continuation(rid)['state'] == 'dispatched'


def test_steering_a_waiting_run_returns_after_the_run_left_waiting_input(env, monkeypatch):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.script = [call('AskUserQuestion', {'questions': json.dumps(ITEMS), 'required': False}, 'ask'),
                  final('I asked which note style you prefer.\nFINAL: asked'),
                  released(env, final('Noted: one line.\nFINAL: steered'))]
    app.work(chat, 'Ask me which note style I want. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_input', 2)  # the turn ended with the optional question still open
    gate = hold_dispatch_thread(app, monkeypatch)
    assert app.run_steer(cid, 'Use one line.', rid)['status'] == 'queued'
    state, active, _ = observed(app, cid, rid)
    assert state in TRANSIENT and active == cid, (state, active)  # without its dispatch_now: ('waiting_input', None)
    gate.set()
    env.release.set()
    settled(env, app, rid, 'waiting_input', 3)  # the optional question is still open after the resumed turn
    assert any('Use one line.' in str(m.get('content')) for m in env.requests[2])
    assert app.interactions.continuation(rid)['state'] == 'dispatched'


def test_a_form_answer_returns_after_the_run_left_waiting_input(env, monkeypatch):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.script = [call('UIRender', {'title': 'Scope', 'kind': 'form', 'data': json.dumps(FORM)}, 'form'),
                  released(env, final('Small scope it is.\nFINAL: answered'))]
    app.work(chat, 'Draw a native UI form to clarify scope.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_input', 1)
    gate = hold_dispatch_thread(app, monkeypatch)
    form = app.artifacts.list(cid)[0]
    assert app.artifact_submit(cid, form['id'], 1, {'scope': {'option_id': 'small'}})['run_id'] == rid
    state, active, _ = observed(app, cid, rid)
    assert state in TRANSIENT and active == cid, (state, active)  # without its dispatch_now: ('waiting_input', None)
    gate.set()
    env.release.set()
    run = settled(env, app, rid, 'completed', 2)
    assert run['error'] is None and app.interactions.continuation(rid)['state'] == 'dispatched'


# ---- The Workspace cost: cached protected folders, answers unchanged (follow-up 2) ---------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    value = tmp_path / 'home'
    value.mkdir()
    monkeypatch.setenv('HOME', str(value))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    data_home.deactivate()
    yield value
    data_home.deactivate()


def reference(path):
    """(inside_protected, tool_refused, protected_below) as the merged Phase 22 source answered them: everything
    resolved from scratch, now."""
    target = os.path.realpath(os.path.abspath(os.path.expanduser(str(path)))).casefold()
    roots = [os.path.realpath(r).casefold() for r in data_home.protected_roots()]
    inside = any(target == base or target.startswith(base.rstrip(os.sep) + os.sep) for base in roots)
    areas = [os.path.realpath(a).casefold().rstrip(os.sep) + os.sep for a in data_home.work_areas()]
    refused = inside and not any(target.startswith(prefix) for prefix in areas)
    return inside, refused, [r for r in roots if r != target and r.startswith(target.rstrip(os.sep) + os.sep)]


def answered_now(path):
    found = (data_home.inside_protected(path), data_home.tool_refused(path), data_home.protected_below(path))
    one_pass = (found[1], [] if found[1] else found[2])
    assert data_home.project_folder(path) == one_pass
    assert data_home.project_folder(os.path.realpath(path), resolved=True) == one_pass  # a Workspace's resolved root
    return found


def test_the_cached_protected_folders_answer_as_an_uncached_resolve_through_every_change(home, tmp_path, monkeypatch):
    elsewhere = tmp_path / 'elsewhere'
    probes = [home / '.pebble' / 'settings.json', home / '.pebble' / 'data' / 'git-worktrees' / 'wt' / 'src',
              home / '.pebble' / 'skills' / 'mine', home / 'Library/Application Support/com.pebble.local/x', home,
              tmp_path / 'project', elsewhere / 'linked' / 'sub', elsewhere / 'lib/Application Support/com.pebble.local/y',
              tmp_path / 'custom' / 'data', tmp_path / 'activated' / 'data', tmp_path / 'old-home' / 'x',
              tmp_path / 'other-home' / '.pebble' / 'z']

    def same():
        for probe in probes:
            assert answered_now(probe) == reference(probe), probe

    same()  # nothing exists yet; the cache is now warm
    (home / '.pebble').mkdir()
    same()
    # ~/.pebble becomes a link (Pebble refuses it as a home, but its target stays protected), with no activation.
    (home / '.pebble').rmdir()
    (elsewhere / 'linked').mkdir(parents=True)
    os.symlink(elsewhere / 'linked', home / '.pebble')
    same()
    assert data_home.tool_refused(elsewhere / 'linked' / 'sub')
    # The missing old app-data folder's parent becomes a link: its resolved form moves with it.
    (elsewhere / 'lib' / 'Application Support').mkdir(parents=True)
    os.symlink(elsewhere / 'lib', home / 'Library')
    same()
    assert data_home.tool_refused(elsewhere / 'lib/Application Support/com.pebble.local/y')
    monkeypatch.setenv('PEBBLE_HOME', str(tmp_path / 'custom'))
    same()
    assert data_home.tool_refused(tmp_path / 'custom' / 'data')
    data_home.activate(data_home.Layout(tmp_path / 'activated', True, '--home', tmp_path / 'old-home'),
                       tmp_path / 'activated' / 'data')
    same()
    assert data_home.tool_refused(tmp_path / 'old-home' / 'x')
    data_home.deactivate()
    same()
    assert not data_home.inside_protected(tmp_path / 'old-home' / 'x')
    (tmp_path / 'other-home').mkdir()
    monkeypatch.setenv('HOME', str(tmp_path / 'other-home'))
    same()
    assert data_home.tool_refused(tmp_path / 'other-home' / '.pebble' / 'z')
    assert not data_home.inside_protected(elsewhere / 'linked' / 'sub')


def test_a_workspace_resolves_its_root_once_as_on_main(home, tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (home / '.pebble').mkdir()
    Workspace(project)
    calls, real = [], os.path.realpath

    def counted(path, *args, **kwargs):
        calls.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, 'realpath', counted)
    Workspace(project)
    assert len(calls) == 1, calls  # its own resolve(strict=True); the merged Phase 22 source made 7
    data_home.activate(data_home.Layout(tmp_path / 'activated', True, '--home', None), tmp_path / 'activated' / 'data')
    calls.clear()
    Workspace(project)
    assert len(calls) > 1  # a new generation: the protected folders are resolved once more
    calls.clear()
    Workspace(project)
    assert len(calls) == 1
    # The refusals themselves are unchanged: a data folder is refused, a project that holds one keeps it out.
    with pytest.raises(PolicyError, match="Pebble's own data folders"):
        Workspace(home / '.pebble')
    holder = Workspace(tmp_path)._protected
    assert holder == reference(tmp_path)[2] and os.path.realpath(home / '.pebble').casefold() in holder
