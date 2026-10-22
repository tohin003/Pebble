"""Phase 24 (V1-028): a draft that never reached the project is not "a change that still needs tests".

AgentGuard tracked proposals while Pebble tracks effects: in 0.6.1 a draft was staged and touched at proposal time, and
reject(), undo() and a refused replacement never told the guard. In Ask mode (the default) a person who rejected the
only edit got two wasted repair turns and then "verification required ... 1 file(s) changed and no tests were run"
for a file unchanged on disk. Now GuardAdapter.unstage restores the mirror from disk and releases the path unless the
project file still differs from how it was before the session first staged it; the controller reconciles AgentGuard
with the broker's records (rejected, superseded, failed, undone) before every stop check.

The App, runner, broker, controller, GuardAdapter and vendored engine are the checked-in code; only the model is
scripted. No model HTTP, network or shell.
"""

import copy
import json
import time
from types import SimpleNamespace

import pytest

from pebble.app import App
from pebble.controller import CURRENT
from pebble.guard import GuardAdapter
from pebble.permissions import DEFAULT
from pebble.policy import Workspace
from pebble.providers import Providers

GOAL = 'Update calc.py so add documents its behaviour.'
SOURCE = 'def add(a, b):\n    return a + b\n'
CHANGED = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'
OTHER = 'def add(a, b):\n    # the sum\n    return a + b\n'
BROKEN = 'def add(a, b)\n    return a + b\n'
TESTS = ('import unittest\n\nfrom calc import add\n\n\n'
         'class AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n')


def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


def files(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text(SOURCE)
    (project / 'test_calc.py').write_text(TESTS)
    return project


@pytest.fixture
def env(tmp_path, monkeypatch):
    value = SimpleNamespace(project=files(tmp_path), requests=[], script=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        assert len(value.requests) <= len(value.script), 'Unscripted root model request.'
        response = copy.deepcopy(value.script[len(value.requests) - 1])
        if response.get('content'):
            token(response['content'])
        return response

    monkeypatch.setattr(Providers, 'complete', complete)
    value.app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    assert value.app.guard.guard is not None, value.app.guard.error
    yield value
    value.app.active = value.app.active_run = None
    CURRENT.set(None)
    value.app.close()


def mode(app, name):
    app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': name})


def settled(env, rid, state, requests, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = env.app.runs.get(rid)
        if run['state'] == state and env.app.active is None and len(env.requests) == requests:
            return run
        time.sleep(.02)
    run = env.app.runs.get(rid)
    pytest.fail(f'Run did not settle as {state} after {requests} requests: {run["state"]} after '
                f'{len(env.requests)} ({run["error"]})')


def notes(app, rid, kind):
    return [json.loads(row['payload']) for row in app.store.query(
        'SELECT payload FROM run_events WHERE run_id=? AND kind=? ORDER BY sequence', (rid, kind))]


@pytest.mark.parametrize('draft', [CHANGED, BROKEN])
def test_the_person_rejects_the_only_edit_and_the_run_completes_without_a_guard_stop(env, draft):
    app = env.app
    mode(app, 'ask')
    chat = app.new_chat(str(env.project))
    env.script = [call('Read', {'file_path': 'calc.py'}, 'read'),
                  call('Write', {'file_path': 'calc.py', 'content': draft}, 'write'),
                  final('Proposed the documented function.'),
                  final('You declined the change, so calc.py is unchanged.')]
    app.work(chat, GOAL, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, rid, 'waiting_approval', 3)
    edit = next(a for a in app.store.actions(chat['id']) if a['kind'] == 'edit' and a['status'] == 'pending')
    app.reject(chat['id'], edit['id'])
    run = settled(env, rid, 'completed', 4)
    assert notes(app, rid, 'guard_stop') == [] and not run['error']
    assert not [m for m in env.requests[-1] if 'HOST COMPLETION CHECK' in str(m.get('content'))]
    assert (env.project / 'calc.py').read_text() == SOURCE
    mirror = app.guard.mirrors[str(Workspace(env.project).root)].path
    assert (mirror / 'calc.py').read_text() == SOURCE, 'the rejected draft left the mirror'


def own(app, cid, rid):
    app.controller.live = None
    app.controller.enter(app.runs.get(rid, cid))
    app.active, app.active_run = cid, rid


def release(app):
    app.active = app.active_run = None
    app.controller.live = None
    CURRENT.set(None)


def test_an_undone_edit_no_longer_counts_at_the_next_completion_check(env):
    app = env.app
    mode(app, 'auto')
    chat = app.new_chat(str(env.project))
    config = app.runtime_config(chat, False, {'max_steps': 16, 'max_seconds': 300})
    rid = app.runs.create(chat['id'], GOAL, config)
    app.runs.transition(rid, 'running')
    ws = Workspace(env.project)
    own(app, chat['id'], rid)
    assert app.guard.begin(ws.root, rid, GOAL)['state'] == 'started'  # as the runner does
    app.execute('Read', {'file_path': 'calc.py'}, Workspace(env.project), chat['id'], call_id='1:read')
    written = app.execute('Write', {'file_path': 'calc.py', 'content': CHANGED}, Workspace(env.project), chat['id'],
                          call_id='2:write')
    assert written['status'] == 'applied' and (env.project / 'calc.py').read_text() == CHANGED
    held = app.controller.completion(rid, Workspace(env.project), 'Documented add().')
    assert held['state'] == 'verification_required' and 'no tests were run' in held['issues'][0]
    assert held['notices'] == ['Pebble changed calc.py but did not run the tests that cover it.']
    release(app)
    app.undo(chat['id'], written['action_id'])
    assert (env.project / 'calc.py').read_text() == SOURCE
    own(app, chat['id'], rid)
    # The source audit (another check) asks for a fresh read of a file that changed after it was read; AgentGuard
    # itself no longer counts the undone edit.
    app.execute('Read', {'file_path': 'calc.py'}, Workspace(env.project), chat['id'], call_id='3:read')
    gate = app.controller.completion(rid, Workspace(env.project), 'Undone.')
    assert gate == {'state': 'completed', 'issues': [], 'repairable': True}, gate
    assert len(notes(app, rid, 'guard_stop')) == 1  # only the check before the undo
    release(app)


@pytest.fixture
def adapter(tmp_path):
    project = files(tmp_path)
    guard = GuardAdapter(tmp_path / 'data')
    value = SimpleNamespace(guard=guard, project=project, root=Workspace(project).root, rid='run-drafts')
    assert guard.begin(value.root, value.rid, GOAL)['state'] == 'started'
    value.mirror = guard.mirrors[str(value.root)].path
    yield value
    guard.close()


def propose(env, content):
    env.guard.stage(env.root, env.rid, 'calc.py', content)
    env.guard.handle('post_tool_use', env.root, env.rid, tool='Write',
                     arguments={'file_path': 'calc.py', 'content': content}, result='{}')


def stop(env):
    return env.guard.handle('stop', env.root, env.rid, last_assistant_message='Done.')


def test_a_superseded_draft_then_a_refused_replacement_leaves_nothing_behind(adapter):
    propose(adapter, CHANGED)
    propose(adapter, BROKEN)  # replaces the first draft, still unapplied
    assert (adapter.mirror / 'calc.py').read_text() == BROKEN
    held = stop(adapter)
    assert held['action'] == 'block' and held['gate']['kind'] == 'unparsable'
    assert adapter.guard.unstage(adapter.root, adapter.rid, 'calc.py') == {'unstaged': True, 'changed': False}
    assert (adapter.mirror / 'calc.py').read_text() == SOURCE
    decision = stop(adapter)
    assert decision['action'] == 'allow' and decision['gate']['outcome'] == 'no_changes', decision
    assert adapter.guard.unstage(adapter.root, adapter.rid, 'calc.py') == {'unstaged': False, 'changed': False}


def test_an_applied_change_stays_a_change_when_a_later_draft_is_rejected(adapter):
    propose(adapter, CHANGED)
    (adapter.project / 'calc.py').write_text(CHANGED)  # the person approved it: the broker applied the draft
    propose(adapter, OTHER)  # a second draft of the same file, then rejected
    assert adapter.guard.unstage(adapter.root, adapter.rid, 'calc.py') == {'unstaged': True, 'changed': True}
    assert (adapter.mirror / 'calc.py').read_text() == CHANGED, 'the mirror shows the project, not the rejected draft'
    held = stop(adapter)
    assert held['action'] == 'block' and 'no tests were run' in held['reason']


def test_an_undo_releases_the_path_when_the_project_is_back_as_it_was(adapter):
    propose(adapter, CHANGED)
    (adapter.project / 'calc.py').write_text(CHANGED)
    assert stop(adapter)['action'] == 'block'
    (adapter.project / 'calc.py').write_text(SOURCE)  # undo wrote the original bytes back (a new mtime)
    assert adapter.guard.unstage(adapter.root, adapter.rid, 'calc.py') == {'unstaged': True, 'changed': False}
    assert stop(adapter)['action'] == 'allow'


def test_another_sessions_drafts_never_show_in_this_sessions_mirror(adapter):
    propose(adapter, BROKEN)
    other = 'run-other'
    assert adapter.guard.begin(adapter.root, other, 'Explain calc.py.')['state'] == 'started'
    adapter.guard.handle('pre_tool_use', adapter.root, other, tool='Read', arguments={'file_path': 'calc.py'})
    assert (adapter.mirror / 'calc.py').read_text() == SOURCE, 'switching sessions swaps the drafts out'
    assert adapter.guard.handle('stop', adapter.root, other, last_assistant_message='Explained.')['action'] == 'allow'
    held = stop(adapter)  # the first session's draft is back for its own check
    assert (adapter.mirror / 'calc.py').read_text() == BROKEN and held['gate']['kind'] == 'unparsable'
