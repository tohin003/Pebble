"""Phase 21 F21-gate: the latest run of a recipe on the current files decides the completion gate.

Claim set C1 (delegation, 3 of 3 deleg-code-test runs): the tests failed once, the failure was fixed and the
latest run passed, yet the run settled verification_required on "The python-unittest recipe ... reported
failures". Every project test run of a run tree (the root's own, and each child request the root executes) is
recorded in the root's one AgentGuard task, and the vendored gate held completion when any recorded outcome had
ever failed. Now a host recipe run replaces that recipe's earlier runs once the project still has the exact files
it tested. A failing latest run, a pass on files changed since, a pass on unapplied drafts, another recipe's pass
and a command that only prints the recipe's name still hold completion.

The App, broker, run journal, durable tree, child scheduler, effect requests, controller completion check,
diagnostics, GuardAdapter and vendored AgentGuard engine are the checked-in code. Only the model transports
(root and child) and the sandboxed test subprocess (run_tests) are scripted. The scripted runner decides the
outcome from the exact files it was given and offers diagnostics as the real runner does. No model HTTP,
network, shell or credential is used, and no fixture returns a Guard decision.
"""

import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest

import pebble.app as app_module
from pebble import diagnostics, recipes, testing
from pebble.app import App
from pebble.controller import CURRENT
from pebble.guard import RECIPE_COMMAND, GuardAdapter
from pebble.permissions import DEFAULT
from pebble.policy import Workspace
from pebble.providers import Providers


GOAL = 'Fix total() in pricing.py so the unit tests in test_pricing.py pass.'
ORIGINAL = 'def total(prices):\n    raise NotImplementedError\n'
BROKEN = 'def total(prices):\n    return sum(prices) + undefined_fee\n'
FIXED = 'def total(prices):\n    return sum(prices)\n'
REWORDED = 'def total(prices):\n    """The sum of prices."""\n    return sum(prices)\n'
TESTS = ('import unittest\n\nfrom pricing import total\n\n\nclass TotalTest(unittest.TestCase):\n'
         '    def test_sum(self):\n        self.assertEqual(total([2, 3]), 5)\n\n'
         '    def test_empty(self):\n        self.assertEqual(total([]), 0)\n')
RULE, LINE = '=' * 70, '-' * 70
HELD = 'The python-unittest recipe (test, run by unittest) reported failures'
UNITTEST = {'tool': 'RunTests', 'arguments': json.dumps({'recipe': 'python-unittest'}), 'purpose': 'Check the fix.'}
CHILD_TOOLS = ['Read', 'RequestAgentEffect', 'ReadAgentEffect']
SMALL, LARGE = 4096, 32768


def profile(budget):
    return {'id': 'fixture-cloud', 'kind': 'openai', 'name': 'Fixture provider', 'url': 'https://unused.invalid',
            'model': 'fixture', 'context': max(budget, 8192), 'budget': budget, 'tools': True}


def call(name, args, ident):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


def eventually(predicate, message, timeout=15):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    pytest.fail(message)


def block_until(predicate, cancel, timeout=15):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if cancel.is_set():
            raise InterruptedError('Fixture request cancelled.')
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Fixture condition was not reached.')


def project_files(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'pricing.py').write_text(ORIGINAL)
    (project / 'test_pricing.py').write_text(TESTS)
    return project


# -- the scripted test subprocess -------------------------------------------------------------------------------

def unittest_output(root, broken):
    """What `python -m unittest` prints for these fixture files: an ERROR raised inside pricing.py, or OK."""
    head = ('test_empty (test_pricing.TotalTest.test_empty) ... ' + ('ERROR' if broken else 'ok') + '\n'
            'test_sum (test_pricing.TotalTest.test_sum) ... ' + ('ERROR' if broken else 'ok') + '\n\n')
    if not broken:
        return head + LINE + '\nRan 2 tests in 0.001s\n\nOK\n'
    block = ''.join(
        f'{RULE}\nERROR: {name} (test_pricing.TotalTest.{name})\n{LINE}\nTraceback (most recent call last):\n'
        f'  File "{root}/test_pricing.py", line {line}, in {name}\n    self.assertEqual(total({args}), {value})\n'
        f'  File "{root}/pricing.py", line 2, in total\n    return sum(prices) + undefined_fee\n'
        "NameError: name 'undefined_fee' is not defined\n\n"
        for name, line, args, value in (('test_empty', 11, '[]', 0), ('test_sum', 8, '[2, 3]', 5)))
    return head + block + LINE + '\nRan 2 tests in 0.001s\n\nFAILED (errors=2)\n'


def scripted_run_tests(payload, cancel, seen=None):
    """run_tests at the broker boundary: the snapshot, outcome and diagnostics come from the exact files tested."""
    assert payload['recipe'] == 'python-unittest'
    ws = Workspace(payload['root'])
    drafts = payload.get('drafts') or []
    hashes = {}
    manifest = testing._collect(ws, drafts, None, hashes)  # the snapshot fields run_tests records, without copying
    drafted = {d['path']: d['after'] for d in drafts}
    source = drafted.get('pricing.py', (ws.root / 'pricing.py').read_text())
    broken = 'undefined_fee' in source or 'NotImplementedError' in source
    output = unittest_output(ws.root, broken)
    code = 1 if broken else 0
    plan = payload.get('plan') or {'recipe': 'python-unittest', 'kind': 'test'}
    diagnostics.offer(plan, code, output, ws.root, hashes, ws)  # the project folder stands in for the snapshot
    outcome, tests = recipes.classify('test', 'python-unittest', code, output)
    if seen is not None:
        seen.append({'outcome': outcome, 'digest': manifest['snapshot_digest']})
    return {'exit_code': code, 'output': output, 'output_projection': 'terminal_text/1', 'controls_removed': 0,
            'output_bytes': len(output), 'output_truncated': False, 'outcome': outcome, 'tests': tests,
            'recipe': 'python-unittest', 'kind': 'test', **manifest, 'executable_sha256': '0' * 64,
            'pinned': True, 'environment': None, 'isolation': testing.ISOLATION}


def recipe_result(project, recipe='python-unittest', drafts=(), **changes):
    """A host recipe result for the project as it is now (plus drafts), classified like the real runner."""
    if recipe == 'python-unittest':
        result = scripted_run_tests({'recipe': recipe, 'root': str(project), 'drafts': list(drafts)}, None)
    else:  # another procedure over the same snapshot: passes unless the fixture code is broken
        manifest = testing.snapshot_manifest(Workspace(project), list(drafts))
        broken = 'undefined_fee' in (project / 'pricing.py').read_text()
        kind = recipes.REGISTRY[recipe].kind
        result = {'exit_code': int(broken), 'outcome': 'failed' if broken else 'passed', 'kind': kind,
                  'recipe': recipe, **manifest,
                  'tests': None if kind != 'test' else recipes._tests(0 if broken else 2, int(broken), 0, 0, 2)}
    return {**result, **changes}


# -- the adapter and engine alone ------------------------------------------------------------------------------

@pytest.fixture
def adapter(tmp_path):
    project = project_files(tmp_path)
    guard = GuardAdapter(tmp_path / 'data')
    assert guard.guard is not None, guard.error
    root, rid = Workspace(project).root, 'run-f21-gate'
    assert guard.begin(root, rid, GOAL)['state'] == 'started'
    value = SimpleNamespace(guard=guard, project=project, root=root, rid=rid)
    yield value
    guard.close()


def write(env, content):
    """A Write the broker applied: the project file changes and AgentGuard records it."""
    (env.project / 'pricing.py').write_text(content)
    env.guard.stage(env.root, env.rid, 'pricing.py', content)
    env.guard.handle('post_tool_use', env.root, env.rid, tool='Write',
                     arguments={'file_path': 'pricing.py', 'content': content}, result='{}')


def ran(env, recipe='python-unittest', **changes):
    env.guard.test_result(str(env.root), env.rid, recipe, recipe_result(env.project, recipe, **changes))


def stop(env):
    scoped = env.guard.roots[(str(env.root), env.rid)]
    eventually(lambda: env.guard.guard.workspace(scoped).index.is_built, 'The AgentGuard index was not built.')
    return env.guard.handle('stop', env.root, env.rid, last_assistant_message='Done; the tests pass.')


def outcomes(env):
    return [o.passed for o in env.guard._task((str(env.root), env.rid)).verification.outcomes]


def test_a_passing_rerun_on_the_current_files_clears_the_earlier_failure_and_keeps_the_history(adapter):
    write(adapter, BROKEN)
    ran(adapter)
    held = stop(adapter)
    assert held['action'] == 'block' and HELD in held['reason'], held
    write(adapter, FIXED)
    ran(adapter)
    assert stop(adapter)['action'] == 'allow'
    # The gate read only the latest run; the engine's own record still holds every run.
    assert outcomes(adapter) == [False, True]
    assert stop(adapter)['action'] == 'allow'


def test_a_failing_latest_run_still_holds_whatever_came_before(adapter):
    write(adapter, FIXED)
    ran(adapter)
    assert stop(adapter)['action'] == 'allow'
    write(adapter, BROKEN)
    ran(adapter)
    held = stop(adapter)
    assert held['action'] == 'block' and held['reason'].startswith('AgentGuard — completion gate: verification_failed')
    assert HELD in held['reason'], held
    # Fail, pass, fail: the newest run decides, and it failed.
    write(adapter, FIXED)
    ran(adapter)
    write(adapter, BROKEN)
    ran(adapter)
    assert stop(adapter)['action'] == 'block'
    assert outcomes(adapter) == [True, False, True, False]


def test_a_pass_on_files_changed_since_does_not_clear_the_failure_until_the_current_files_pass(adapter):
    write(adapter, BROKEN)
    ran(adapter)
    write(adapter, FIXED)
    ran(adapter)
    write(adapter, REWORDED)  # an edit after the passing run: it no longer describes the project
    held = stop(adapter)
    assert held['action'] == 'block' and HELD in held['reason'], held
    ran(adapter)
    assert stop(adapter)['action'] == 'allow'
    # The person (or a command) changes the file outside the run: the pass is stale again, the failure holds again.
    (adapter.project / 'pricing.py').write_text(FIXED + '\n')
    assert stop(adapter)['action'] == 'block'


def test_a_pass_on_unapplied_drafts_or_an_incomplete_snapshot_never_clears_a_failure(adapter):
    write(adapter, BROKEN)
    ran(adapter)
    draft = Workspace(adapter.project).draft('pricing.py', FIXED)
    ran(adapter, drafts=[draft])  # the draft passed, but the project still has the broken file
    assert stop(adapter)['action'] == 'block'
    write(adapter, FIXED)
    ran(adapter, snapshot_truncated=True)  # the snapshot left files out: not whole-project evidence
    assert stop(adapter)['action'] == 'block'
    ran(adapter, snapshot_digest=None)
    assert stop(adapter)['action'] == 'block'
    ran(adapter)
    assert stop(adapter)['action'] == 'allow'


@pytest.mark.parametrize('recipe', ['python-pytest', 'npm-lint'])
def test_another_recipe_never_clears_a_failure_and_each_recipe_is_decided_by_its_own_latest_run(adapter, recipe):
    write(adapter, BROKEN)
    ran(adapter)
    write(adapter, FIXED)
    ran(adapter, recipe)
    held = stop(adapter)
    assert held['action'] == 'block' and HELD in held['reason'], held
    ran(adapter)
    assert stop(adapter)['action'] == 'allow'
    # A lint or build recipe is decided the same way: its own newest run on the current files.
    write(adapter, BROKEN)
    ran(adapter, recipe)
    write(adapter, FIXED)
    assert stop(adapter)['action'] == 'block'
    ran(adapter)
    assert stop(adapter)['action'] == 'block'
    ran(adapter, recipe)
    assert stop(adapter)['action'] == 'allow'


def test_a_command_that_prints_the_recipe_name_can_neither_clear_nor_be_cleared(adapter):
    """Only the host's own recipe results, which carry the snapshot they tested, replace earlier runs."""
    command = RECIPE_COMMAND + 'python-unittest'
    write(adapter, BROKEN)
    ran(adapter)
    write(adapter, FIXED)
    adapter.guard.handle('post_tool_use', adapter.root, adapter.rid, tool='Bash', arguments={'command': command},
                         result={'stdout': '=== 2 passed ===\n', 'exit_code': 0})
    assert stop(adapter)['action'] == 'block'
    ran(adapter)
    assert stop(adapter)['action'] == 'allow'
    adapter.guard.handle('post_tool_use', adapter.root, adapter.rid, tool='Bash', arguments={'command': command},
                         result={'stdout': '=== 1 failed ===\n', 'exit_code': 1})
    assert stop(adapter)['action'] == 'block'
    ran(adapter)
    assert stop(adapter)['action'] == 'block'


def test_a_reopened_task_starts_without_earlier_runs(adapter):
    write(adapter, BROKEN)
    ran(adapter)
    write(adapter, FIXED)
    ran(adapter)
    assert stop(adapter)['action'] == 'allow'
    # A fresh task (a new prompt) or a task rebuilt after a restart never inherits earlier test runs.
    assert adapter.guard.begin(adapter.root, adapter.rid, GOAL)['state'] == 'started'
    write(adapter, FIXED + '\n')
    held = stop(adapter)
    assert held['action'] == 'block' and 'no tests were run' in held['reason'], held
    ran(adapter)
    assert stop(adapter)['action'] == 'allow' and outcomes(adapter) == [True]


# -- a run tree: the test thread is the root worker, children run on the real scheduler ------------------------

class Child:
    """One scripted child transport. It takes its inference admission like a real child, then never holds the
    lane while its script waits for the root."""

    def __init__(self, tree, app, profile_, owner_id, lane, observer=None):
        self.tree, self.profile, self.owner_id, self.lane = tree, copy.deepcopy(profile_), owner_id, lane
        self.agent_id = owner_id.split(':')[1]
        self.name = app.store.query('SELECT name FROM agent_nodes WHERE id=?', (self.agent_id,))[0]['name']
        self.requests, self.last_usage = [], {'input_tokens': 11, 'output_tokens': 7}
        self.stopped, self.closed = threading.Event(), False
        assert self.name in tree.scripts, 'Every child needs an explicit fixture script.'

    def complete(self, messages, tools, cancel, token, effort):
        if cancel.is_set() or self.stopped.is_set():
            raise InterruptedError('Fixture child stopped.')
        with self.lane.admit(self.owner_id, self.profile, cancel):
            self.requests.append(copy.deepcopy(messages))
        return copy.deepcopy(self.tree.scripts[self.name](self, len(self.requests), messages, cancel))

    def cancel(self):
        self.stopped.set()
        self.lane.wake()

    def close(self):
        self.closed = True
        self.stopped.set()
        self.lane.wake()


class Tree:
    def __init__(self, tmp_path, monkeypatch, budget):
        self.project = project_files(tmp_path)
        self.scripts, self.runs, self.calls = {}, [], 0
        monkeypatch.setattr(app_module, 'run_tests', lambda payload, cancel: scripted_run_tests(payload, cancel, self.runs))
        self.app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
        assert self.app.guard.guard is not None, self.app.guard.error
        monkeypatch.setattr(self.app.model, 'current', lambda: profile(budget))
        monkeypatch.setattr(self.app.model, 'complete', lambda *a, **k: pytest.fail('The root model is the test thread.'))
        self.app.agents.provider_factory = lambda *args, **kwargs: Child(self, *args, **kwargs)
        self.app.store.set_setting('permissions', {'mode': 'auto', 'tools': copy.deepcopy(DEFAULT['tools'])})
        chat = self.app.new_chat(str(self.project))
        config = self.app.runtime_config(chat, False, {'max_steps': 64, 'max_seconds': 300})
        self.cid, self.rid = chat['id'], self.app.runs.create(chat['id'], GOAL, config)
        self.app.runs.transition(self.rid, 'running')
        self.ws = Workspace(self.project)
        self.own()
        assert self.app.guard.begin(self.ws.root, self.rid, GOAL)['state'] == 'started'  # as the runner does

    def own(self):
        app = self.app
        app.controller.live = None
        app.controller.enter(app.runs.get(self.rid, self.cid))
        app.active, app.active_run = self.cid, self.rid

    def root(self, name, args):
        self.own()
        self.calls += 1
        return self.app.execute(name, args, Workspace(self.project), self.cid, call_id=f'{self.calls}:root')

    def spawn(self, name, script, tools, paths=()):
        self.scripts[name] = script
        # model_tokens is omitted: the host fits it to the route's window beside the root reserve.
        return self.root('SpawnAgent', {'task': 'Complete the scoped ' + name + ' task for pricing.py.', 'name': name,
                                        'tools': json.dumps(tools), 'write_paths': json.dumps(list(paths)),
                                        'max_steps': 6, 'max_seconds': 60})

    def node(self, child):
        return self.app.agents.tree.get(self.cid, self.rid, child['id'])

    def settled(self, child):
        def done():
            record = self.node(child)
            assert record['status'] not in {'failed', 'stopped', 'interrupted', 'budget_exhausted'}, record
            with self.app.agents.lock:
                owned = child['id'] in self.app.agents.workers
            return record if record['status'] == 'completed' and not owned else None
        return eventually(done, 'The child did not complete: ' + str(self.node(child)))

    def effects(self, child):
        return self.app.store.query('SELECT id,status FROM agent_effects WHERE agent_id=? ORDER BY created', (child['id'],))

    def integrate(self, child):
        """Accept a settled patch child's evidence and integrate its one patch through the root broker."""
        self.settled(child)
        self.root('ReviewAgentResult', {'agent_id': child['id'], 'generation': 1, 'decision': 'accept'})
        [patch] = self.app.dispatch('agent_get', {'chat_id': self.cid, 'run_id': self.rid,
                                                  'agent_id': child['id']})['patches']
        assert self.root('IntegrateAgentPatch', {'patch_id': patch['id']})['status'] == 'applied'

    def execute(self, child, count):
        """Run the child's count-th effect request as the root's own call."""
        rows = eventually(lambda: (lambda r: r if len(r) >= count else None)(self.effects(child)),
                          'The child did not request its test run.')
        executed = self.root('ExecuteAgentEffect', {'request_id': rows[count - 1]['id']})
        assert executed['status'] == 'applied', executed
        return executed

    def dismiss(self, child):
        self.settled(child)
        self.root('ReviewAgentResult', {'agent_id': child['id'], 'generation': 1, 'decision': 'dismiss'})

    def completion(self):
        self.own()
        return self.app.controller.completion(self.rid, Workspace(self.project), 'Fixed total(); the tests pass.')

    def diagnostic_errors(self):
        """Error counts of the recipe diagnostics records this root run committed, oldest first."""
        return [json.loads(row['payload'])['counts']['error'] for row in self.app.store.query(
            'SELECT payload FROM run_events WHERE run_id=? AND kind=? ORDER BY sequence', (self.rid, diagnostics.EVENT))]

    def guard_stops(self):
        return [json.loads(row['payload']) for row in self.app.store.query(
            "SELECT payload FROM run_events WHERE run_id=? AND kind='guard_stop' ORDER BY sequence", (self.rid,))]

    def close(self):
        self.app.active = self.app.active_run = None
        self.app.controller.live = None
        CURRENT.set(None)
        self.app.close()


@pytest.fixture
def tree(tmp_path, monkeypatch):
    made = []

    def make(budget):
        made.append(Tree(tmp_path, monkeypatch, budget))
        return made[-1]
    yield make
    for item in made:
        item.close()
        assert not item.app.agents.busy(), 'Fixture teardown must join every child worker.'


def patcher(content):
    def script(_child, step, _messages, _cancel):
        if step == 1:
            return call('Read', {'file_path': 'pricing.py'}, 'read')
        if step == 2:
            return call('ProposePatch', {'file_path': 'pricing.py', 'content': content}, 'patch')
        return final('Proposed pricing.py; the parent verifies and integrates it.')
    return script


def receipt_id(messages):
    return json.loads(next(m['content'] for m in reversed(messages) if m['role'] == 'tool'))['request_id']


def requester(tree_, runs, before_rerun=None):
    """Requests the unit tests from the root; with runs=2 it asks again once before_rerun is set."""
    def script(child, step, messages, cancel):
        if step == 1:
            return call('RequestAgentEffect', UNITTEST, 'request-1')
        if step == 2 and runs == 2:
            first = receipt_id(messages)
            block_until(lambda: before_rerun.is_set() and tree_.app.store.query(
                'SELECT status FROM agent_effects WHERE id=?', (first,))[0]['status'] == 'applied', cancel)
            return call('RequestAgentEffect', UNITTEST, 'request-2')
        if step == runs + 1:
            request = receipt_id(messages)
            block_until(lambda: tree_.app.store.query(
                'SELECT status FROM agent_effects WHERE id=?', (request,))[0]['status'] in {'applied', 'failed'}, cancel)
            return call('ReadAgentEffect', {'request_id': request}, 'read')
        return final('The root ran the unit tests I requested; its receipt is the evidence.')
    return script


def assert_completed(tree_):
    gate = tree_.completion()
    assert gate == {'state': 'completed', 'issues': [], 'repairable': True}, gate
    assert tree_.guard_stops() == []


def assert_held(tree_):
    gate = tree_.completion()
    assert gate['state'] == 'verification_required', gate
    assert any(HELD in issue for issue in gate['issues']), gate['issues']
    assert tree_.guard_stops() and 'verification_failed' in tree_.guard_stops()[-1]['reason']
    return gate


@pytest.mark.parametrize('budget', [SMALL, LARGE])
@pytest.mark.parametrize('passing_run', ['child', 'root'])
def test_a_delegated_fix_whose_latest_run_passes_completes(tree, budget, passing_run):
    t = tree(budget)
    rerun = threading.Event()
    t.integrate(t.spawn('writer', patcher(BROKEN), ['Read', 'ProposePatch'], ['pricing.py']))
    check = t.spawn('tester', requester(t, 2 if passing_run == 'child' else 1, rerun), CHILD_TOOLS)
    t.execute(check, 1)
    assert [r['outcome'] for r in t.runs] == ['failed']
    t.integrate(t.spawn('fixer', patcher(FIXED), ['Read', 'ProposePatch'], ['pricing.py']))
    if passing_run == 'child':
        rerun.set()
        t.execute(check, 2)  # the child's own request, run by the root on the integrated fix
    else:
        assert t.root('RunTests', {'recipe': 'python-unittest'})['status'] == 'applied'  # the root, after integration
    assert [r['outcome'] for r in t.runs] == ['failed', 'passed']
    assert t.runs[-1]['digest'] == testing.snapshot_digest(Workspace(t.project))
    # Both runs left hash-bound diagnostics in the root run; the newer, clean record supersedes the failing one.
    assert t.diagnostic_errors() == [2, 0]
    t.dismiss(check)
    assert (t.project / 'pricing.py').read_text() == FIXED
    assert_completed(t)


@pytest.mark.parametrize('budget', [SMALL, LARGE])
def test_a_delegated_change_whose_latest_run_fails_still_holds_with_the_recipe_and_diagnostics(tree, budget):
    t = tree(budget)
    t.integrate(t.spawn('writer', patcher(FIXED), ['Read', 'ProposePatch'], ['pricing.py']))
    check = t.spawn('tester', requester(t, 1), CHILD_TOOLS)
    t.execute(check, 1)  # the child's run passes
    t.dismiss(check)
    t.integrate(t.spawn('regressor', patcher(BROKEN), ['Read', 'ProposePatch'], ['pricing.py']))
    assert t.root('RunTests', {'recipe': 'python-unittest'})['status'] == 'applied'  # the root's later run fails
    assert [r['outcome'] for r in t.runs] == ['passed', 'failed'] and t.diagnostic_errors() == [0, 2]
    gate = assert_held(t)
    # The diagnostics check agrees: the newest record of the recipe has an error in the file this run edited.
    assert any('new error diagnostic' in issue and 'python-unittest' in issue for issue in gate['issues']), gate
    assert gate['repairable'] is True


@pytest.mark.parametrize('budget', [SMALL, LARGE])
def test_a_delegated_pass_followed_by_another_edit_needs_a_run_on_the_current_files(tree, budget):
    t = tree(budget)
    rerun = threading.Event()
    t.integrate(t.spawn('writer', patcher(BROKEN), ['Read', 'ProposePatch'], ['pricing.py']))
    check = t.spawn('tester', requester(t, 2, rerun), CHILD_TOOLS)
    t.execute(check, 1)
    t.integrate(t.spawn('fixer', patcher(FIXED), ['Read', 'ProposePatch'], ['pricing.py']))
    rerun.set()
    t.execute(check, 2)
    t.dismiss(check)
    # The root edits again after the passing run, without testing the result.
    t.root('Read', {'file_path': 'pricing.py'})
    assert t.root('Write', {'file_path': 'pricing.py', 'content': REWORDED})['status'] == 'applied'
    assert_held(t)
    assert t.root('RunTests', {'recipe': 'python-unittest'})['status'] == 'applied'
    assert [r['outcome'] for r in t.runs] == ['failed', 'passed', 'passed']
    gate = t.completion()
    assert gate['state'] == 'completed', gate


# -- the runner end to end: the root model is scripted, the run settles itself ---------------------------------

@pytest.fixture
def solo(tmp_path, monkeypatch):
    project = project_files(tmp_path)
    value = SimpleNamespace(project=project, requests=[], script=[], runs=[], apps=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        index = len(value.requests) - 1
        response = copy.deepcopy(value.script[index] if index < len(value.script) else final('Done.'))
        if response.get('content'):
            token(response['content'])
        return response

    monkeypatch.setattr(Providers, 'complete', complete)
    monkeypatch.setattr(app_module, 'run_tests', lambda payload, cancel: scripted_run_tests(payload, cancel, value.runs))

    def start(budget):
        app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
        value.apps.append(app)
        assert app.guard.guard is not None, app.guard.error
        app.store.set_setting('permissions', {'mode': 'auto', 'tools': copy.deepcopy(DEFAULT['tools'])})
        if budget > SMALL:
            app.model_save(name='Fixture', url='http://127.0.0.1:9', model='fixture', context=budget)
            assert app.model.current()['context'] == budget
        return app

    value.start = start
    yield value
    for app in value.apps:
        if not app.closed:
            app.active = app.active_run = None
            app.close()


def settle(app, chat):
    rid = app.runs.list(chat['id'])[0]['id']

    def done():
        run = app.runs.get(rid)
        return run if run['state'] not in {'queued', 'running', 'retry_wait'} and app.active is None else None
    return eventually(done, 'The run did not settle.', timeout=30)


def solo_script(second):
    return [call('Read', {'file_path': 'pricing.py'}, 'read-1'),
            call('Write', {'file_path': 'pricing.py', 'content': BROKEN}, 'write-1'),
            call('RunTests', {'recipe': 'python-unittest'}, 'tests-1'),
            call('Read', {'file_path': 'pricing.py'}, 'read-2'),
            call('Write', {'file_path': 'pricing.py', 'content': second}, 'write-2'),
            call('RunTests', {'recipe': 'python-unittest'}, 'tests-2'),
            final('Fixed total() in pricing.py; the unit tests pass.')]


@pytest.mark.parametrize('budget', [SMALL, LARGE])
def test_the_runner_settles_completed_after_a_failed_run_is_fixed_and_rerun(solo, budget):
    app = solo.start(budget)
    chat = app.new_chat(str(solo.project))
    solo.script = solo_script(FIXED)
    app.work(chat, GOAL, False, [])
    run = settle(app, chat)
    assert [r['outcome'] for r in solo.runs] == ['failed', 'passed']
    assert run['state'] == 'completed', (run['state'], run['error'])
    assert len(solo.requests) == len(solo.script)  # no completion repair was asked for
    assert not [m for m in solo.requests[-1] if 'HOST COMPLETION CHECK' in str(m.get('content'))]
    assert (solo.project / 'pricing.py').read_text() == FIXED


@pytest.mark.parametrize('budget', [SMALL, LARGE])
def test_native_approvals_and_continuations_keep_the_latest_run_record(solo, budget):
    """Ask mode: each round's edit and tests are approved natively between turns, and the run resumes its task."""
    app = solo.start(budget)
    app.store.set_setting('permissions', {'mode': 'ask', 'tools': copy.deepcopy(DEFAULT['tools'])})
    chat = app.new_chat(str(solo.project))
    proposal = lambda n, content: [call('Read', {'file_path': 'pricing.py'}, f'read-{n}'),  # noqa: E731
                                   call('Write', {'file_path': 'pricing.py', 'content': content}, f'write-{n}'),
                                   call('RunTests', {'recipe': 'python-unittest'}, f'tests-{n}'),
                                   final('Proposed a change to pricing.py and a unit-test run.')]
    solo.script = proposal(1, BROKEN) + proposal(2, FIXED) + [final('Fixed total() in pricing.py; the tests pass.')]
    app.work(chat, GOAL, False, [])
    rid = app.runs.list(chat['id'])[0]['id']

    def at(state, requests):
        eventually(lambda: app.runs.get(rid)['state'] == state and app.active is None
                   and len(solo.requests) >= requests, f'The run did not reach {state}.', timeout=30)
        return app.runs.get(rid)

    def approve_round():
        for kind in ('edit', 'tests'):
            action = next(a for a in app.store.actions(chat['id']) if a['kind'] == kind and a['status'] == 'pending')
            app.approve(chat['id'], action['id'])

    at('waiting_approval', 4)
    approve_round()
    at('waiting_approval', 8)
    approve_round()
    run = at('completed', 9)
    assert [r['outcome'] for r in solo.runs] == ['failed', 'passed']
    assert run['state'] == 'completed' and not run['error']
    assert app.store.query("SELECT 1 FROM run_events WHERE run_id=? AND kind='guard_task_rebuilt'", (rid,)) == []
    assert (solo.project / 'pricing.py').read_text() == FIXED


@pytest.mark.parametrize('budget', [SMALL, LARGE])
def test_the_runner_still_settles_verification_required_when_the_latest_run_fails(solo, budget):
    app = solo.start(budget)
    chat = app.new_chat(str(solo.project))
    solo.script = solo_script(BROKEN.replace('undefined_fee', 'undefined_fee * 1'))
    app.work(chat, GOAL, False, [])
    run = settle(app, chat)
    assert [r['outcome'] for r in solo.runs] == ['failed', 'failed']
    assert run['state'] == 'verification_required', (run['state'], run['error'])
    assert HELD in run['error']
    repairs = [m['content'] for m in solo.requests[-1] if 'HOST COMPLETION CHECK' in str(m.get('content'))]
    assert repairs and HELD in repairs[-1]
