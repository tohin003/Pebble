"""Phase 18 pilot 1 regressions (parent): defects the first live pilot found that no deterministic test caught.

See `.runtime/phase18-live-review.md` ("Pilot 1"). Only the root model (Providers.complete) and the approved test
action's sandboxed subprocess are scripted, as in test_phase17_integration.py; the baseline uses a fake model
surface, as in test_eval_baseline.py. No model HTTP, network, shell or credential is used.
"""

import copy
import json
import re
import socket
import time
from types import SimpleNamespace

import pytest

import pebble.app as app_module
from pebble import eval_baseline as eb
from pebble import eval_corpus_18 as ec
from pebble import evaluation as ev
from pebble import recipes
from pebble.app import App
from pebble.policy import Workspace
from pebble.providers import Providers

SOURCE = 'def add(a, b):\n    return a + b\n'
TESTS = ('import unittest\n\nfrom calc import add\n\n\n'
         'class AddTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('Pilot-fix tests must not open network connections.')
        return connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))


def say_and_call(text, name, args, ident):
    return {'role': 'assistant', 'content': text, 'tool_calls': [{
        'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text(SOURCE)
    (project / 'test_calc.py').write_text(TESTS)
    value = SimpleNamespace(project=project, requests=[], script=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        assert len(value.requests) <= len(value.script), 'Unscripted root model request.'
        response = copy.deepcopy(value.script[len(value.requests) - 1])
        if response.get('content'):
            token(response['content'])
        return response

    monkeypatch.setattr(Providers, 'complete', complete)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    value.app = app
    yield value
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


# -- finding 1: the web seam check must see the merged seam ---------------------------------------

def test_the_web_seam_check_sees_the_merged_app_and_installs_per_instance(tmp_path):
    # Pilot 1 invocation 2 was refused (web_fixture_seam_missing): the check read only App.__init__, while the
    # merged seam is set in App._initialize. Every earlier test mocked the check either way.
    assert ev._web_seam_ready() is True
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        assert 'web_search' in vars(app) and 'web_weather' in vars(app)
        fixture = SimpleNamespace(search=lambda query: {}, fetch=lambda url, *, max_bytes: {})
        ev._install_web_18(app, fixture)
        assert app.web_search is fixture.search and app.ledger._fetch is fixture.fetch
        with pytest.raises(ev.EvaluationError, match='weather_not_in_fixture'):
            app.web_weather('Kolkata')
    finally:
        app.close()


# -- finding 5: each step's text starts on its own line (product) ---------------------------------

def test_each_later_step_starts_on_its_own_line_so_a_bare_final_line_survives(env):
    # A narrated tool step followed by a final step that is only the FINAL line used to read
    # 'Reading calc.py.FINAL: done': run-together for the person, and no line-anchored FINAL line.
    app = env.app
    chat = app.new_chat(str(env.project))
    env.script = [say_and_call('Reading calc.py.', 'Read', {'file_path': 'calc.py'}, 'read'),
                  say_and_call('It already adds.', 'Read', {'file_path': 'test_calc.py'}, 'read2'),
                  final('FINAL: done')]
    app.work(chat, 'Check that add in calc.py adds two numbers.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, rid, 'completed', 3)
    answer = [m for m in app.chat(chat['id'])['messages'] if m['role'] == 'assistant'][-1]['content']
    assert answer == 'Reading calc.py.\nIt already adds.\nFINAL: done'
    assert [v.strip() for v in ev.FINAL_LINE.findall(answer)] == ['done']


def test_a_step_without_text_adds_no_blank_line(env):
    app = env.app
    chat = app.new_chat(str(env.project))
    env.script = [say_and_call(None, 'Read', {'file_path': 'calc.py'}, 'read'),
                  final('It adds.\nFINAL: done')]
    app.work(chat, 'Check that add in calc.py adds two numbers.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, rid, 'completed', 2)
    answer = [m for m in app.chat(chat['id'])['messages'] if m['role'] == 'assistant'][-1]['content']
    assert answer == 'It adds.\nFINAL: done'


# -- finding 4: both arms are graded on everything they showed -----------------------------------

class FakeModel:
    def __init__(self, replies):
        self.replies, self.last_usage = list(replies), {}
        self.context_transform = self.context_observer = None

    def current(self):
        return dict(ev.SCRIPTED_PROFILE)

    def complete(self, messages, tools, cancel, token, effort):
        self.offered = tools
        self.last_usage = {'input_tokens': 10, 'output_tokens': 2, 'cached_tokens': 0}
        return copy.deepcopy(self.replies.pop(0) if self.replies else final('FINAL: done'))


def baseline_answer(tmp_path, replies):
    root = tmp_path / 'ws'
    root.mkdir()
    (root / 'a.txt').write_text('hello\n')
    task = SimpleNamespace(id='t1', prompt='Read a.txt. End with `FINAL: <word>`.', answer='typed')
    return eb.run_baseline(SimpleNamespace(model=FakeModel(replies)), task, root,
                           {'max_steps': 6, 'max_seconds': 60}, answers={})


def test_the_baseline_is_graded_on_every_step_it_showed_joined_like_pebble(tmp_path):
    # Pilot 1: the baseline was graded on its last reply only, Pebble on its whole reply.
    evidence = baseline_answer(tmp_path, [say_and_call('Reading a.txt.', 'read_file', {'path': 'a.txt'}, 'r1'),
                                          final('FINAL: hello')])
    assert evidence.answer == 'Reading a.txt.\nFINAL: hello' and evidence.state == 'completed'


def test_an_early_final_line_counts_against_the_baseline_too(tmp_path):
    evidence = baseline_answer(tmp_path, [say_and_call('FINAL: hi', 'read_file', {'path': 'a.txt'}, 'r1'),
                                          final('FINAL: hello')])
    assert evidence.answer == 'FINAL: hi\nFINAL: hello'
    assert len(ev.FINAL_LINE.findall(evidence.answer)) == 2  # single_final_line fails in both arms alike


# -- finding 2: auto resolves to a declared recipe for every task with a tests grant ---------------

def test_auto_resolves_to_a_declared_recipe_for_every_task_with_a_tests_grant(tmp_path):
    # Pilot 1 code-weighted: RunTests auto resolved to python-pytest and all three requests were rejected, while the
    # baseline's run_tests could only pick the declared python-unittest. 18a.2 declares both Python recipes.
    tested = [t for t in ec.CORPUS if 'tests' in t.tools]
    assert len(tested) == 8 and all('tests' in t.allowed_kinds for t in tested)
    for task in tested:
        root = tmp_path / task.id
        root.mkdir()
        for name, text in task.files:
            (root / name).write_text(text)
        if not any(re.fullmatch(r'test.*\.py', name) for name, _ in task.files):
            # The delegation task's child writes the test file the grader requires.
            (root / next(p for p in task.allowed_paths if p.startswith('test'))).write_text(TESTS)
        for choice in ('auto',) + task.allowed_recipes:
            assert recipes.resolve(choice, Workspace(root))['recipe'] in task.allowed_recipes, (task.id, choice)


# -- r19-shapes: the baseline arm of ui-quiz can write the file its prompt asks for -----------------

def test_the_baseline_gets_the_write_tool_only_where_its_prompt_asks_for_a_file(tmp_path):
    # Without it the baseline's write_file was refused ("Unknown tool.") and it could never pass ui-quiz.
    quiz = ec.BY_ID['ui-quiz']
    assert 'edits' not in quiz.tools and ec.baseline_grants(quiz) == tuple(quiz.tools) + ('edits',)
    root = tmp_path / 'ws'
    root.mkdir()
    model = FakeModel([final('FINAL: done')])
    eb.run_baseline(SimpleNamespace(model=model), quiz, root, {'max_steps': 2, 'max_seconds': 60}, answers={},
                    grants=ec.baseline_grants(quiz))
    offered = {t['function']['name'] for t in model.offered}
    assert 'write_file' in offered and 'run_tests' not in offered
    model = FakeModel([final('FINAL: done')])
    eb.run_baseline(SimpleNamespace(model=model), quiz, root, {'max_steps': 2, 'max_seconds': 60}, answers={})
    assert 'write_file' not in {t['function']['name'] for t in model.offered}


# -- Phase 19 H1b: the model sees that a pending change is not applied (pilot 2, finding 7) ----------

def test_a_pending_change_is_shown_to_the_model_as_staged_not_done_and_the_receipt_is_unchanged(env):
    # The model keeps its staged-draft workflow (historical validators pin it) but is told plainly that the change
    # is not applied, so it does not end its turn as if it were. Only the model's view carries the note.
    from pebble.runner import APPROVAL_NOTE
    app = env.app
    chat = app.new_chat(str(env.project))
    env.script = [say_and_call('Writing the notes.', 'Write', {'file_path': 'notes.md', 'content': '# Notes\n'}, 'write'),
                  final('notes.md is staged and awaits your approval.')]
    app.work(chat, 'Write notes.md describing add in calc.py.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, rid, 'waiting_approval', 2)
    shown = [json.loads(m['content']) for m in env.requests[1] if m.get('role') == 'tool']
    assert shown[-1]['status'] == 'awaiting_user_approval' and shown[-1]['note'] == APPROVAL_NOTE
    assert len(APPROVAL_NOTE) <= 200 and 'not applied' in APPROVAL_NOTE
    stored = [t['result'] for t in app.runs.tools(rid) if t['name'] == 'Write']
    assert stored and all('note' not in r for r in stored)


def test_after_an_approval_the_model_is_told_to_continue_the_same_reply_without_repeating_it(env):
    # Phase 19 H1c (pilot 3): the final line was written before the approval and again after it. One reply spans the
    # approval: the note says it continues then, and the resume line says the person already sees its first part.
    from pebble.runner import APPROVAL_NOTE
    app = env.app
    chat = app.new_chat(str(env.project))
    env.script = [say_and_call('Writing the notes.', 'Write', {'file_path': 'notes.md', 'content': '# Notes\n'}, 'write'),
                  final('notes.md is staged for your approval.'),
                  final('It is applied.\nFINAL: written')]
    app.work(chat, 'Write notes.md describing add in calc.py.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, rid, 'waiting_approval', 2)
    assert 'final answer yet' in APPROVAL_NOTE and 'continues' in APPROVAL_NOTE and len(APPROVAL_NOTE) <= 200
    [edit] = [a for a in app.store.actions(chat['id']) if a['status'] == 'pending']
    app.approve(chat['id'], edit['id'])
    settled(env, rid, 'completed', 3)
    resumed = [m['content'] for m in env.requests[2] if m.get('role') == 'user' and str(m.get('content')).startswith('HOST RESUME')]
    assert resumed and resumed[-1].endswith('continue it without repeating it.')
    answer = [m for m in app.chat(chat['id'])['messages'] if m['role'] == 'assistant' and m.get('content')][-1]['content']
    assert [v.strip() for v in ev.FINAL_LINE.findall(answer)] == ['written']
