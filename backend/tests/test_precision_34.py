"""Phase 34 (1.2.0, the beat plan's precision release): W1 check without writing, and the W2-W4 writing rules.

W1 has two halves. RunTests takes a check: one extra test module the host adds to the isolated copy only, under a name
the recipe's own discovery runs (testing.CHECK_FILES). The project never sees it, and its run is kind 'check', which is
never test evidence: not for the completion gate, AgentGuard, diagnostics or memory lessons. And a test file the
request did not ask to change waits for the person, in Ask and Auto mode (edit_scope), with the host's reason on the
approval card and a note to the model that names the check instead.

The App runner, broker, recipes and sandboxed test runner are the checked-in code; only the root model
(Providers.complete) is scripted. No model HTTP, network or credential is used.
"""
import copy
import json
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from pebble import controller as controller_module
from pebble import edit_scope, memory_facts, runner, testing
from pebble.app import App
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError, Workspace
from pebble.providers import Providers
from pebble.testing import CHECK_FILES, check_payload, run_tests, snapshot_digest
from test_high_fd_waits_21 import close_guard_databases

SOURCE = 'def mean(values):\n    return sum(values) / len(values)\n'
FIXED = 'def mean(values):\n    return sum(values) / len(values) if values else 0.0\n'
TESTS = ('import unittest\n\nfrom stats import mean\n\n\n'
         'class MeanTest(unittest.TestCase):\n    def test_mean(self):\n        self.assertEqual(mean([1, 3]), 2)\n')
CHECK = ('import unittest\n\nfrom stats import mean\n\n\n'
         'class EmptyCheck(unittest.TestCase):\n    def test_empty(self):\n        self.assertEqual(mean([]), 0.0)\n')
EXTRA_TEST = ('import unittest\n\nfrom stats import mean\n\n\n'
              'class ExtraTest(unittest.TestCase):\n    def test_empty(self):\n        self.assertEqual(mean([]), 0.0)\n')
macos = pytest.mark.skipif(sys.platform != 'darwin', reason='macOS sandbox')


# -- edit_scope: which edits wait for the person ----------------------------------------------------------------------

@pytest.mark.parametrize('path, expected', [
    ('tests/test_parser.py', True), ('test_parser.py', True), ('pkg/tests/fixtures/data.json', True),
    ('src/__tests__/parser.js', True), ('parser_test.go', True), ('parser.test.ts', True), ('parser.spec.js', True),
    ('parser.py', False), ('docs/testing.md', False), ('latest.py', False), ('contest/entry.py', False),
])
def test_test_files_are_recognised_by_path(path, expected):
    assert edit_scope.is_test_file(path) is expected


@pytest.mark.parametrize('text, waits', [
    # No test changes asked for: the edit waits.
    ('Make mean return 0.0 for an empty list.', True),
    ('Fix the bug so the tests pass.', True),
    ('Could you fix it so the check in test_parser.py passes? It should also accept tuples.', True),
    ("Fix the bug, but don't change the tests.", True),
    ('Refactor the parser without changing the tests.', True),
    ('Never modify the tests; just repair parse().', True),
    ('', True),
    # Test changes asked for: the edit goes ahead under the person's mode.
    ('Add a test for the empty list.', False),
    ('Please update the unit tests for the new API.', False),
    ('Improve the error messages and add tests covering them.', False),
    ('Write tests for parse().', False),
    ('Add a case to test_parser.py for negative numbers.', False),
    ('Extend the existing test suite with a tuple case.', False),
    # Phase 46 (dev d-cb-01): a framework's name, and the file named with its folder
    ('Also add unittest tests for it in tests/test_parser.py.', False),
    ('Add pytest tests for parse().', False),
    ('Add a case to tests/test_parser.py for negative numbers.', False),
    ("Don't add unittest tests; just repair parse().", True),
    # Phase 46 (private dev d-lh-91's last turn): a behaviour that needs a new test
    ('Every behaviour we added today needs a test in tests/ (add the missing ones), and the suite must pass.', False),
    ('The release needs the tests to pass first.', True),
    ("This fix doesn't need a test.", True),
])
def test_only_a_request_for_test_changes_lets_a_test_edit_through(text, waits):
    reason = edit_scope.review_reason('test_parser.py', text)
    assert (reason == edit_scope.REASON) is waits and (reason is None) is not waits


def test_an_ordinary_file_never_waits_on_this_rule():
    assert edit_scope.review_reason('parser.py', 'Make parse accept tuples.') is None


def test_naming_another_test_file_is_not_a_request_to_change_this_one():
    assert edit_scope.review_reason('tests/test_other.py', 'Add a case to test_parser.py.') == edit_scope.REASON


# -- the check payload ---------------------------------------------------------------------------------------------

def test_the_host_names_the_check_file_and_binds_its_text():
    check = check_payload('python-unittest', CHECK)
    assert check['path'] == CHECK_FILES['python-unittest'] == 'test_pebble_check.py'
    assert check['content'] == CHECK and len(check['sha256']) == 64
    assert check_payload('node-test', 'import test from "node:test";\n')['path'] == 'pebble-check.test.mjs'


@pytest.mark.parametrize('recipe, content, message', [
    ('python-pytest', CHECK, 'python-unittest or node-test'),
    ('npm-test', CHECK, 'python-unittest or node-test'),
    ('python-unittest', '', 'source text'),
    ('python-unittest', '   \n', 'source text'),
    ('python-unittest', None, 'source text'),
    ('python-unittest', 'x' * (testing.CHECK_LIMIT + 1), 'limited'),
    ('python-unittest', 'import os\x00', 'limited'),
])
def test_a_check_outside_its_bounds_is_refused(recipe, content, message):
    with pytest.raises(PolicyError, match=message):
        check_payload(recipe, content)


@pytest.mark.parametrize('mutate', [
    lambda c: {**c, 'path': 'test_other.py'},
    lambda c: {**c, 'content': c['content'] + '# changed\n'},
    lambda c: {**c, 'sha256': '0' * 64},
    lambda c: {k: v for k, v in c.items() if k != 'sha256'},
    lambda c: {**c, 'extra': True},
    lambda c: 'text',
])
def test_a_changed_check_is_refused_before_anything_runs(project, mutate):
    payload = {'root': str(project), 'recipe': 'python-unittest', 'drafts': [],
               'check': mutate(check_payload('python-unittest', CHECK))}
    with pytest.raises(PolicyError, match='check'):
        run_tests(payload, threading.Event())


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'stats.py').write_text(SOURCE)
    (root / 'test_stats.py').write_text(TESTS)
    return root


# -- the check run, in the real sandbox ---------------------------------------------------------------------------------

@macos
def test_a_check_runs_in_the_isolated_copy_only_and_is_kind_check(project):
    before = {p.name: p.read_bytes() for p in project.iterdir()}
    payload = {'root': str(project), 'recipe': 'python-unittest', 'drafts': [],
               'check': check_payload('python-unittest', CHECK)}
    result = run_tests(payload, threading.Event())
    # The check found the bug (mean([]) divides by zero), and the project's own test ran beside it.
    assert result['exit_code'] != 0 and 'test_empty' in result['output'] and 'ZeroDivisionError' in result['output']
    assert result['kind'] == 'check' and result['check']['path'] == 'test_pebble_check.py'
    assert result['check']['sha256'] == payload['check']['sha256'] and 'does not count' in result['check']['note']
    # The project did not change, and the snapshot digest is the project's own (the check is not part of it).
    assert {p.name: p.read_bytes() for p in project.iterdir()} == before
    assert result['snapshot_digest'] == snapshot_digest(Workspace(project))
    assert 'test_pebble_check.py' not in result['drafts_included']


@macos
def test_a_check_sees_the_staged_drafts(project):
    draft = Workspace(project).draft('stats.py', FIXED)
    payload = {'root': str(project), 'recipe': 'python-unittest', 'drafts': [draft],
               'check': check_payload('python-unittest', CHECK)}
    result = run_tests(payload, threading.Event())
    assert result['exit_code'] == 0 and result['outcome'] == 'passed' and result['kind'] == 'check', result['output']
    assert (project / 'stats.py').read_text() == SOURCE


@macos
def test_a_project_file_with_the_checks_name_stops_the_check(project):
    (project / 'test_pebble_check.py').write_text('# the project owns this name\n')
    payload = {'root': str(project), 'recipe': 'python-unittest', 'drafts': [],
               'check': check_payload('python-unittest', CHECK)}
    with pytest.raises(PolicyError, match='already has test_pebble_check.py'):
        run_tests(payload, threading.Event())
    assert (project / 'test_pebble_check.py').read_text() == '# the project owns this name\n'


@macos
def test_a_run_without_a_check_is_unchanged(project):
    result = run_tests({'root': str(project), 'recipe': 'python-unittest', 'drafts': []}, threading.Event())
    assert result['kind'] == 'test' and 'check' not in result and result['outcome'] == 'passed'


# -- never evidence -------------------------------------------------------------------------------------------------

def test_a_passing_check_never_satisfies_the_completion_gate(project):
    ws = Workspace(project)
    passing = {'exit_code': 0, 'outcome': 'passed', 'kind': 'test', 'snapshot_digest': snapshot_digest(ws),
               'tests': {'passed': 2, 'total': 2}}
    action = lambda result: {'kind': 'tests', 'status': 'applied', 'created': 1.0,  # noqa: E731
                             'payload': {'root': str(ws.root), 'root_identity': list(ws.identity), 'result': result}}
    evidence = controller_module.RunController._current_test_evidence
    assert evidence([action(passing)], ws) is True
    assert evidence([action({**passing, 'kind': 'check', 'check': {'path': 'test_pebble_check.py'}})], ws) is False


def test_a_failing_check_teaches_no_lesson():
    check = {'exit_code': 1, 'kind': 'check', 'output': 'FAIL: test_empty (test_pebble_check.EmptyCheck.test_empty)'}
    real = {'exit_code': 1, 'kind': 'test', 'output': 'FAIL: test_mean (test_stats.MeanTest.test_mean)'}
    receipt = lambda: {'state': 'completed', 'name': 'RunTests', 'arguments': {'recipe': 'python-unittest'},  # noqa: E731
                       'result': {'action_id': 'a1'}}
    action = lambda result: {'id': 'a1', 'kind': 'tests', 'status': 'applied',  # noqa: E731
                             'payload': {'recipe': 'python-unittest', 'result': result}}
    assert memory_facts.failure_signatures(receipt(), action(check)) == []
    assert memory_facts.failure_signatures(receipt(), action(real)) != []
    assert memory_facts.failure_observations('completed', [receipt()], [action(check)]) == []
    assert memory_facts.failure_observations('completed', [receipt()], [action(real)]) != []


# -- the App: scripted model, real broker ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('These tests must not open network connections.')
        return connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)


def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


@pytest.fixture
def env(tmp_path, monkeypatch, project):
    value = SimpleNamespace(project=project, requests=[], script=[], apps=[], guard_results=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        assert len(value.requests) <= len(value.script), 'Unscripted root model request.'
        response = copy.deepcopy(value.script[len(value.requests) - 1])
        if response.get('content'):
            token(response['content'])
        return response

    monkeypatch.setattr(Providers, 'complete', complete)

    def start(mode='auto', context=32768):
        app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
        value.apps.append(app)
        app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': mode})
        app.model_save(name='p34 fixture', url='http://127.0.0.1:9', model='p34-fixture-%d' % context, context=context)
        real = app.guard.test_result

        def spy(root, cid, recipe, result):
            value.guard_results.append(result.get('kind'))
            return real(root, cid, recipe, result)
        monkeypatch.setattr(app.guard, 'test_result', spy)
        return app

    value.start = start
    yield value
    for app in value.apps:
        if not app.closed:
            app.active = app.active_run = None
            app.close()
    close_guard_databases(tmp_path)


def settle(env, app, cid, requests, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = app.runs.list(cid)
        if runs and app.active is None and len(env.requests) == requests and runs[0]['state'] not in (
                'queued', 'running', 'retry_wait'):
            return app.runs.get(runs[0]['id'])
        time.sleep(.02)
    pytest.fail(f'The run did not settle after {requests} requests ({len(env.requests)} made).')


def tool_result(env, request, call_id):
    message = next(m for m in env.requests[request] if m.get('role') == 'tool' and m.get('tool_call_id') == call_id)
    return json.loads(message['content'])


def test_an_unrequested_test_file_waits_for_the_person_in_auto_mode(env):
    app = env.start('auto')
    chat = app.new_chat(str(env.project))
    env.script = [call('Read', {'file_path': 'stats.py'}, 'read'),
                  call('Write', {'file_path': 'stats.py', 'content': FIXED}, 'fix'),
                  call('Write', {'file_path': 'test_extra.py', 'content': EXTRA_TEST}, 'extra'),
                  final('mean returns 0.0 for an empty list; the extra test waits for you.\nFINAL: staged')]
    app.work(chat, 'Make mean return 0.0 for an empty list.', False, [])
    settle(env, app, chat['id'], 4)
    actions = {a['payload']['path']: a for a in app.store.actions(chat['id']) if a['kind'] == 'edit'}
    assert actions['stats.py']['status'] == 'applied' and 'scope_review' not in actions['stats.py']['payload']
    held = actions['test_extra.py']
    assert held['status'] == 'pending' and held['payload']['scope_review'] == edit_scope.REASON
    assert held['payload']['requires_review'] is True and not (env.project / 'test_extra.py').exists()
    told = tool_result(env, 3, 'extra')
    assert told['status'] == 'awaiting_user_approval' and told['scope_review'] == edit_scope.MODEL_NOTE


@pytest.mark.parametrize('mode, request_text', [
    ('auto', 'Make mean return 0.0 for an empty list, and add a test for it.'),
    ('full', 'Make mean return 0.0 for an empty list.'),
])
def test_a_requested_test_or_full_access_applies_as_before(env, mode, request_text):
    app = env.start(mode)
    chat = app.new_chat(str(env.project))
    env.script = [call('Write', {'file_path': 'test_extra.py', 'content': EXTRA_TEST}, 'extra'),
                  final('Added.\nFINAL: done')]
    app.work(chat, request_text, False, [])
    settle(env, app, chat['id'], 2)
    action = next(a for a in app.store.actions(chat['id']) if a['kind'] == 'edit')
    assert action['status'] == 'applied' and 'scope_review' not in action['payload']
    assert (env.project / 'test_extra.py').read_text() == EXTRA_TEST


@macos
def test_a_check_through_the_app_is_reviewed_text_and_never_reaches_agentguard(env):
    app = env.start('auto')
    chat = app.new_chat(str(env.project))
    env.script = [call('RunTests', {'recipe': 'python-unittest', 'check': CHECK}, 'check'),
                  call('RunTests', {'recipe': 'python-unittest'}, 'tests'),
                  final('mean([]) raises ZeroDivisionError; the project tests pass.\nFINAL: done')]
    app.work(chat, 'Does mean handle an empty list?', False, [])
    settle(env, app, chat['id'], 3)
    runs = [a for a in app.store.actions(chat['id']) if a['kind'] == 'tests']
    assert [a['status'] for a in runs] == ['applied', 'applied']
    checked, tested = runs
    assert checked['payload']['check'] == check_payload('python-unittest', CHECK)
    assert checked['payload']['result']['kind'] == 'check' and 'check' not in tested['payload']
    assert tested['payload']['result']['kind'] == 'test'
    # Only the project's own run reached AgentGuard.
    assert env.guard_results == ['test']
    told = tool_result(env, 1, 'check')
    assert told['result']['kind'] == 'check' and 'does not count' in told['result']['check']['note']
    assert sorted(p.name for p in env.project.iterdir()) == ['stats.py', 'test_stats.py']


def test_a_check_where_the_tests_use_another_recipe_is_refused_with_its_reason(env, project):
    (project / 'test_stats.py').unlink()
    (project / 'Makefile').write_text('test:\n\t@echo ok\n')
    app = env.start('auto')
    chat = app.new_chat(str(env.project))
    env.script = [call('RunTests', {'recipe': 'auto', 'check': CHECK}, 'check'),
                  final('No check ran.\nFINAL: done')]
    app.work(chat, 'Check mean with an empty list.', False, [])
    settle(env, app, chat['id'], 2)
    told = tool_result(env, 1, 'check')
    assert 'python-unittest or node-test' in told['error'] and 'make-test' in told['error'], told
    assert not [a for a in app.store.actions(chat['id']) if a['kind'] == 'tests']


# -- W1-W4 in the prompt ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize('context, precise', [(32768, True), (4096, False)])
def test_the_writing_rules_go_above_8k_and_the_test_rule_with_the_completion_rule(env, context, precise):
    app = env.start('auto', context)
    chat = app.new_chat(str(env.project))
    env.script = [final('mean divides the sum by the count.\nFINAL: done')]
    app.work(chat, 'Explain mean in one sentence.', False, [])
    settle(env, app, chat['id'], 1)
    system = env.requests[0][0]['content']
    assert (runner.PRECISION in system) is precise
    assert (runner.TEST_SCOPE in system) is precise  # the completion rule itself is above 4K only
    if precise:
        assert system.index('run them with RunTests before your final answer') < system.index(runner.TEST_SCOPE)


def test_the_rules_are_general():
    """No task, fixture or grader of corpus 24a is named (validate_phase34 checks the full lists)."""
    text = ' '.join([runner.PRECISION, runner.TEST_SCOPE, edit_scope.REASON, edit_scope.MODEL_NOTE, testing.CHECK_NOTE])
    for word in ('FINAL', 'pages/', 'distractor', 'protected', 'quantile', 'ferry', 'haiku', 'minutes'):
        assert word.lower() not in text.lower(), word
