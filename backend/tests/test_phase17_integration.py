"""Phase 17 parent integration: regressions for defects found while merging and evaluating the Phase 17 tracks.

Only the root model (Providers.complete) and the approved test action's sandboxed subprocess (run_tests) are
scripted, as in test_guard_resume.py. No model HTTP, network, shell or credential is used.
"""

import copy
import json
import time
from types import SimpleNamespace

import pytest

import pebble.app as app_module
from pebble.app import App
from pebble.providers import Providers

SOURCE = 'def add(a, b):\n    return a + b\n'
CHANGED = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'
TESTS = ('import unittest\n\nfrom calc import add\n\n\n'
         'class AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n')
PASSED = {'exit_code': 0, 'output': 'test_add (test_calc.AddTest.test_add) ... ok\n\n' + '-' * 70
          + '\nRan 1 test in 0.001s\n\nOK\n'}


def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text(SOURCE)
    (project / 'test_calc.py').write_text(TESTS)
    value = SimpleNamespace(project=project, requests=[], script=[], results=[PASSED], apps=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        assert len(value.requests) <= len(value.script), 'Unscripted root model request.'
        response = copy.deepcopy(value.script[len(value.requests) - 1])
        if response.get('content'):
            token(response['content'])
        return response

    monkeypatch.setattr(Providers, 'complete', complete)
    monkeypatch.setattr(app_module, 'run_tests', lambda payload, cancel: copy.deepcopy(value.results.pop(0)))
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    value.apps.append(app)
    value.app = app
    yield value
    for app in value.apps:
        if not app.closed:
            app.active = app.active_run = None
            app.close()


def settled(env, rid, state, requests, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = env.app.runs.get(rid)
        if run['state'] == state and env.app.active is None and len(env.requests) == requests:
            return run
        time.sleep(.02)
    pytest.fail(f'Run did not settle as {state}: {env.app.runs.get(rid)["state"]} after {len(env.requests)}')


def test_a_resumed_turn_starts_on_its_own_line_so_a_final_line_survives(env):
    # Phase 18 Q3 finding H: continuation turns were joined with no separator, so 'Waiting for your approval.'
    # and the next turn's 'FINAL: written' fused into one line and a line-anchored reader lost the final line.
    app = env.app
    chat = app.new_chat(str(env.project))
    env.script = [call('Read', {'file_path': 'calc.py'}, 'read'),
                  call('Write', {'file_path': 'calc.py', 'content': CHANGED}, 'write'),
                  call('RunTests', {'recipe': 'python-unittest'}, 'tests'),
                  {'role': 'assistant', 'content': 'Waiting for your approval.'},
                  {'role': 'assistant', 'content': 'FINAL: written'}]
    app.work(chat, 'Document add in calc.py and run the tests.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, rid, 'waiting_approval', 4)
    for kind in ('edit', 'tests'):
        action = next(a for a in app.store.actions(chat['id']) if a['kind'] == kind and a['status'] == 'pending')
        app.approve(chat['id'], action['id'])
    settled(env, rid, 'completed', 5)
    answer = [m for m in app.chat(chat['id'])['messages'] if m['role'] == 'assistant'][-1]['content']
    assert answer == 'Waiting for your approval.\nFINAL: written'
    assert answer.splitlines()[-1] == 'FINAL: written'
