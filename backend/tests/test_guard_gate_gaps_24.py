"""Phase 24 (V1-093): the completion gate no longer passes with no test run.

Four 0.6.1 paths let a change to covered code complete with no test evidence: a passing lint, build or typecheck recipe
became "=== 1 passed ==="; a "no tests" substring anywhere in the request waived the demand ("...the docs folder has no
tests in it"); a change made by an approved command was invisible, because the mirror only changed through stage; and
an index that was never built made the stop gate allow. Now only test recipes are evidence (a lint pass only replaces
the same recipe's failure), Pebble decides the waiver from an imperative, the mirror is synced after a command and what
changed counts, and an unready index holds the run as "verification unavailable".

GuardAdapter and the vendored engine are the checked-in code; no model, network or shell.
"""

import json
import sys
from types import SimpleNamespace

import pytest

from pebble import guard as guard_module, testing
from pebble.guard import VENDOR, GuardAdapter, waived
from pebble.policy import Workspace

if VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)  # as GuardAdapter does, so the engine can be patched before an adapter exists

SOURCE = 'def add(a, b):\n    return a + b\n'
CHANGED = 'def add(a, b):\n    """Return the sum of a and b."""\n    return a + b\n'
TESTS = ('import unittest\n\nfrom calc import add\n\n\n'
         'class AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n')


def files(tmp_path):
    project = tmp_path / 'project'
    project.mkdir(parents=True)
    (project / 'calc.py').write_text(SOURCE)
    (project / 'test_calc.py').write_text(TESTS)
    return project


@pytest.fixture
def make(tmp_path):
    made = []

    def start(goal='Update calc.py so add documents its behaviour.'):
        project = files(tmp_path / f'case{len(made)}')
        guard = GuardAdapter(tmp_path / f'data{len(made)}')
        value = SimpleNamespace(guard=guard, project=project, root=Workspace(project).root, rid='run-gaps')
        assert guard.begin(value.root, value.rid, goal)['state'] == 'started'
        made.append(value)
        return value
    yield start
    for value in made:
        value.guard.close()


def edit(env, content=CHANGED):
    (env.project / 'calc.py').write_text(content)
    env.guard.stage(env.root, env.rid, 'calc.py', content)
    env.guard.handle('post_tool_use', env.root, env.rid, tool='Write',
                     arguments={'file_path': 'calc.py', 'content': content}, result='{}')


def stop(env):
    return env.guard.handle('stop', env.root, env.rid, last_assistant_message='Done; it works.')


def recipe(env, name, outcome='passed', kind='lint'):
    digest = testing.snapshot_digest(Workspace(env.project))
    env.guard.test_result(str(env.root), env.rid, name, {
        'exit_code': 0 if outcome == 'passed' else 1, 'outcome': outcome, 'kind': kind, 'recipe': name,
        'snapshot_digest': digest, 'tests': None if kind != 'test' else
        {'passed': 1, 'failed': 0, 'skipped': 0, 'errors': 0, 'total': 1}})


@pytest.mark.parametrize('name', ['npm-lint', 'npm-build', 'npm-typecheck', 'go-build'])
def test_a_lint_or_build_pass_is_not_test_evidence(make, name):
    env = make()
    edit(env)
    recipe(env, name, kind='build' if 'build' in name else 'lint')
    held = stop(env)
    assert held['action'] == 'block' and 'no tests were run' in held['reason'], held
    assert held['gate']['kind'] == 'no_test_run' and held['gate']['covering'] == ['test_calc.py']
    recipe(env, 'python-unittest', kind='test')
    assert stop(env)['action'] == 'allow'


def test_a_test_recipe_that_ran_no_tests_is_not_evidence_either(make):
    env = make()
    edit(env)
    digest = testing.snapshot_digest(Workspace(env.project))
    env.guard.test_result(str(env.root), env.rid, 'python-pytest', {
        'exit_code': 5, 'outcome': 'no_tests', 'kind': 'test', 'recipe': 'python-pytest', 'snapshot_digest': digest,
        'tests': {'passed': 0, 'failed': 0, 'skipped': 0, 'errors': 0, 'total': 0}})
    held = stop(env)  # the recipe path reports it as a failed command, as before
    assert held['action'] == 'block' and held['gate']['kind'] in {'no_test_run', 'failing_tests'}, held
    assert held['gate']['outcome'] == 'held'


@pytest.mark.parametrize('goal', [
    'Update calc.py so add documents its behaviour; the docs folder has no tests in it.',
    'Add tests for the helpers without tests, starting with calc.py.',
    'Document add in calc.py. There are no tests for the README.',
])
def test_an_incidental_no_tests_phrase_does_not_waive_the_test_run(make, goal):
    env = make(goal)
    edit(env)
    held = stop(env)
    assert held['action'] == 'block' and held['gate']['kind'] == 'no_test_run' and held['gate']['waived'] is False


@pytest.mark.parametrize('goal', [
    'Document add in calc.py. Do not run the tests.',
    "Document add in calc.py, but don't run tests.",
    'Document add in calc.py without running the tests.',
    'Skip the tests and document add in calc.py.',
])
def test_an_imperative_still_waives_it(make, goal):
    assert waived(goal)
    env = make(goal)
    edit(env)
    decision = stop(env)
    assert decision['action'] == 'allow' and decision['gate']['outcome'] == 'not_verified', decision


def test_a_change_an_approved_command_made_is_checked(make):
    env = make()
    (env.project / 'calc.py').write_text(CHANGED)  # the command rewrote the file (no Write, no stage)
    env.guard.handle('post_tool_use', env.root, env.rid, tool='Bash', arguments={'command': 'python3 tools/fmt.py'},
                     result=json.dumps({'exit_code': 0, 'output': 'formatted 1 file\n'}))
    held = stop(env)
    assert held['action'] == 'block' and 'no tests were run' in held['reason'], held
    mirror = env.guard.mirrors[str(env.root)].path
    assert (mirror / 'calc.py').read_text() == CHANGED


def test_a_read_only_command_never_counts_a_change_it_cannot_make(make):
    env = make()
    (env.project / 'calc.py').write_text(CHANGED)  # the person's own edit in their editor
    env.guard.handle('post_tool_use', env.root, env.rid, tool='Bash', arguments={'command': 'rg add .'},
                     result=json.dumps({'exit_code': 0, 'output': 'calc.py:1\n'}), writable=False)
    assert stop(env)['action'] == 'allow'


def test_an_index_that_is_not_ready_gives_verification_unavailable_not_allow(make, monkeypatch):
    from agentguard.repo.index import RepoIndex
    monkeypatch.setattr(guard_module, 'INDEX_WAIT', 0.3)

    def broken(self):
        raise RuntimeError('fixture: the index cannot be built')
    monkeypatch.setattr(RepoIndex, 'build', broken)
    env = make()
    edit(env)
    held = stop(env)
    assert held['action'] == 'block', held
    assert held['reason'].startswith('AgentGuard — completion gate: verification_unavailable')
    assert held['gate']['kind'] == 'unavailable' and held['gate']['outcome'] == 'unavailable'
    # Positive host evidence still counts without the index: a passing test run on the current files.
    recipe(env, 'python-unittest', kind='test')
    assert stop(env)['action'] == 'allow'


def test_an_unready_index_never_holds_a_run_that_changed_no_source(make, monkeypatch):
    from agentguard.repo.index import RepoIndex
    monkeypatch.setattr(guard_module, 'INDEX_WAIT', 0.3)
    monkeypatch.setattr(RepoIndex, 'build', lambda self: (_ for _ in ()).throw(RuntimeError('fixture')))
    env = make('Explain calc.py.')
    assert stop(env)['action'] == 'allow'


def test_a_project_without_tests_is_not_held_for_a_run_that_collected_none(tmp_path):
    """Phase 46 (golden dev d-mt-01 on gpt-6.1-sol): in a project with no tests at all, a run that collected none was
    reported as a failed test command, so every changed-code turn was held as "reported failures (1 failed)" through all
    its repairs. A run that collected no tests is no evidence either way: it never passes, and it is not a failure."""
    project = tmp_path / 'no-tests' / 'project'
    project.mkdir(parents=True)
    (project / 'calc.py').write_text(SOURCE)  # this project has no tests at all
    guard = GuardAdapter(tmp_path / 'no-tests' / 'data')
    env = SimpleNamespace(guard=guard, project=project, root=Workspace(project).root, rid='run-no-tests')
    assert guard.begin(env.root, env.rid, 'Update calc.py so add documents its behaviour.')['state'] == 'started'
    edit(env)
    digest = testing.snapshot_digest(Workspace(env.project))
    env.guard.test_result(str(env.root), env.rid, 'python-unittest', {
        'exit_code': 5, 'outcome': 'no_tests', 'kind': 'test', 'recipe': 'python-unittest', 'snapshot_digest': digest,
        'tests': {'passed': 0, 'failed': 0, 'skipped': 0, 'errors': 0, 'total': 0}})
    try:
        decision = stop(env)
    finally:
        guard.close()
    assert decision['action'] == 'allow' and decision['gate'].get('kind') != 'failing_tests', decision


def test_where_tests_cover_the_change_a_run_that_collected_none_is_held_for_them(make):
    env = make()
    edit(env)
    digest = testing.snapshot_digest(Workspace(env.project))
    env.guard.test_result(str(env.root), env.rid, 'python-unittest', {
        'exit_code': 5, 'outcome': 'no_tests', 'kind': 'test', 'recipe': 'python-unittest', 'snapshot_digest': digest,
        'tests': {'passed': 0, 'failed': 0, 'skipped': 0, 'errors': 0, 'total': 0}})
    held = stop(env)
    assert held['action'] == 'block' and held['gate']['kind'] == 'no_test_run' and held['gate']['covering'] == ['test_calc.py']
