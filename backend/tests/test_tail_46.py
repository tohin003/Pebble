"""Phase 46 (1.9.28, w10-tail): the wasted model requests behind gate 5's p95 on Opus (fix-research-1.9.28/tail.md),
and the fixes of w10's security review (workers/w10-tail-security-review.md, follow-up 1).

- F1: RunCode's description names the text's key and open() in its limits (the lead's wording); F1-1: a pebble.read with
  no line range returns the whole file, one with a range the whole range, never one page taken for the file.
- F2: with auto, one RunTests call runs the project's own tests (the run that counts) and the check alone, each in its
  own copy (F2-1: no code of the check in the counted run); a check with no test of its own never reads as passed, a
  node check included (F2-2).
- F3: the files the run's own applied Git plan changed or removed are read again by the host when its result returns,
  only where the plan's own before/after snapshot explains the change; a stale-only repair names the files, each
  JSON-quoted (F3-1); Git lookups only for the files the host would read (F3-2); HEAD's version is no exemption after
  a command (F3-4).
- F5: outside Ask mode an earlier response's staged edit no longer keeps the turn going past a staged command; a policy
  without a mode keeps the batch review (F5-1).
- F7: the Anthropic stream runs inside the stream watchdog, its first-event budget growing with the request (F7-1).
- F8 (PEBBLE_1928_CACHE_BREAKPOINT, off by default): a second Anthropic cache breakpoint on the system's shared part.

Scripted models and transports only: no network, model or credential.
"""
import contextlib
import copy
import importlib
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import code_mode, providers, recipes, responses, runner, testing, tool_catalogue
from pebble.app import App
from pebble.controller import RunController
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError, Workspace
from pebble.providers import THINKING_BINDING_BETA, Providers
from pebble.recovery import REQUEST_TIMEOUT, StreamBudget, StreamStalled
from test_anthropic_thinking_binding_28 import FINAL as CLAUDE_FINAL, run as claude_run
from test_approval_handoff_22 import call, env, final, no_network, pending, settled  # noqa: F401 (fixtures)
from test_git_plans import MAC, git
from test_git_requests_46 import command, finished
from test_golden_dev_46 import kinds
from test_high_fd_waits_21 import close_guard_databases

macos = pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
                           reason='macOS sandbox')
git_macos = pytest.mark.skipif(not MAC, reason='Confined Git plans are a macOS sandbox-exec product surface.')


def with_node():
    try:
        recipes._node()
        return True
    except PolicyError:
        return False


node = pytest.mark.skipif(sys.platform != 'darwin' or not with_node(), reason='Node and the macOS sandbox')


# -- F1: what a pebble call returns ----------------------------------------------------------------------------------

def test_runcode_names_the_texts_key_and_open_in_its_limits():
    # restated by the lead (follow-up 1): the sentence of its own took test_context_cost_24's bound to 2.76; folded
    assert "pebble.read(file_path='a.py')['untrusted_source'] (the text)" in code_mode.DESCRIPTION
    assert code_mode.DESCRIPTION.split(' After a task')[0].endswith('60 s; no network or open().')
    assert 'pebble.make_tool' in code_mode.DESCRIPTION
    assert code_mode.SPEC['function']['description'] == code_mode.DESCRIPTION


def test_the_wording_stays_when_tool_making_is_off(monkeypatch):
    monkeypatch.setenv('PEBBLE_TOOL_MAKING', '0')
    reloaded = importlib.reload(code_mode)
    try:
        assert "['untrusted_source'] (the text)" in reloaded.DESCRIPTION and 'make_tool' not in reloaded.DESCRIPTION
        assert reloaded.DESCRIPTION.endswith('no network or open().')
    finally:
        monkeypatch.delenv('PEBBLE_TOOL_MAKING')
        importlib.reload(code_mode)


ORDERS = 'id,amount\n' + ''.join(f'{n},{n}\n' for n in range(1, 901))  # 901 lines: one page above 8K is 200


def script_task(tmp_path, files, source):
    """One RunCode script through the real App, worker and sandbox (test_code_mode_37's harness, with own files)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
    import validate_phase34 as v34
    from test_code_mode_37 import Scripted
    folder = v34.project(tmp_path / 'project')
    for name, text in files.items():
        (folder / name).write_text(text)
    app = v34.app_for(tmp_path, 'auto', 32768)
    try:
        with Scripted() as scripted:
            scripted.script = [v34.call('RunCode', {'code': source, 'purpose': 'Read the data'}, 'rc1')] + [
                v34.final('Done.')] * 3
            chat = app.new_chat(str(folder))
            app.work(chat, 'Total the amount column of orders.csv.', False, [])
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                runs = app.runs.list(chat['id'])
                if runs and app.active is None and runs[0]['state'] not in ('queued', 'running', 'retry_wait'):
                    break
                time.sleep(.02)
            message = next(m for m in scripted.requests[1] if m.get('role') == 'tool' and m.get('tool_call_id') == 'rc1')
            return json.loads(message['content']), app.runs.tools(app.runs.list(chat['id'])[0]['id'])
    finally:
        v34.close(app)


@macos
def test_a_script_reads_a_long_file_whole(tmp_path):
    result, receipts = script_task(tmp_path, {'orders.csv': ORDERS}, '''
data = pebble.read(file_path="orders.csv")
rows = data["untrusted_source"].splitlines()[1:]
print(len(rows), sum(int(row.split(",")[1]) for row in rows), data["pages"], data["excerpt_complete"])
''')
    # 1.9.28 first form: 199 rows and 19900, a confident wrong total from the first page
    assert result['error'] is None and result['untrusted_printed'].split() == ['900', '405450', '2', 'True'], result
    pages = [r for r in receipts if r['call_id'].startswith('1:rc1:c') and r['name'] == 'Read']
    assert len(pages) == 2 == result['by_tool']['Read'] and pages[1]['arguments']['offset'] == 201
    assert pages[0]['result']['untrusted_source'].count('\n') == 200  # the finding: one Read is one 200-line page
    assert pages[1]['arguments']['expected_hash'] == pages[0]['result']['file_hash']  # bound to the first page


@macos
def test_a_script_that_asks_for_a_range_gets_that_range_and_its_own_page(tmp_path):
    result, _ = script_task(tmp_path, {'orders.csv': ORDERS}, '''
part = pebble.read(file_path="orders.csv", offset=101, limit=50)
lines = part["untrusted_source"].splitlines()
print(len(lines), lines[0], lines[-1], part.get("pages"), part["total_lines"])
''')
    assert result['error'] is None and result['untrusted_printed'].split() == ['50', '100,100', '149,149', 'None', '901']


def fake_reads(text, page_chars, file_hash='h1', changed_at=None):
    """A stand-in for the direct-call path that pages `text` as the Read tool does (lines, then characters)."""
    lines, calls = text.splitlines(keepends=True), []

    def one(name, args):
        calls.append(dict(args))
        offset, limit, char = args.get('offset', 1), args.get('limit', 200), args.get('char_offset', 0)
        span = ''.join(lines[offset - 1:offset - 1 + limit])
        excerpt = span[char:char + page_chars]
        digest = 'h2' if changed_at is not None and len(calls) > changed_at else file_hash
        return {'result': {'path': args['file_path'], 'start_line': offset, 'total_lines': len(lines),
                           'file_hash': digest, 'untrusted_source': excerpt, 'char_offset': char,
                           'next_char_offset': char + len(excerpt) if char + len(excerpt) < len(span) else None,
                           'excerpt_complete': char + len(excerpt) >= len(span)}}
    return one, calls


def test_whole_reads_follow_lines_and_characters_and_are_bounded():
    text = ''.join(f'line {n:05d} ' + 'x' * 90 + '\n' for n in range(1, 2501))
    one, calls = fake_reads(text, page_chars=60_000)
    first = one('Read', {'file_path': 'big.txt'})
    whole = code_mode._whole_read(one, {'file_path': 'big.txt'}, first)['result']
    assert whole['untrusted_source'] == text and whole['excerpt_complete'] and whole['next_char_offset'] is None
    assert [c.get('offset', 1) for c in calls[:3]] == [1, 201, 201] and calls[2]['char_offset'] == 60_000
    # a script that pages itself gets its page; a range is completed by characters only
    assert code_mode._whole_read(one, {'file_path': 'big.txt', 'char_offset': 5}, first) is first
    ranged = code_mode._whole_read(one, {'file_path': 'big.txt', 'offset': 3, 'limit': 1000},
                                   one('Read', {'file_path': 'big.txt', 'offset': 3, 'limit': 1000}))['result']
    assert ranged['untrusted_source'] == ''.join(text.splitlines(keepends=True)[2:1002])
    # larger than a script may receive: refused, never cut
    one, _ = fake_reads(text * 5, page_chars=90_000)
    refused = code_mode._whole_read(one, {'file_path': 'big.txt'}, one('Read', {'file_path': 'big.txt'}))
    assert 'larger than a script may receive' in refused['error'] and 'result' not in refused
    # a file that changed between pages: refused
    one, _ = fake_reads(text, page_chars=60_000, changed_at=1)
    assert 'changed' in code_mode._whole_read(one, {'file_path': 'big.txt'}, one('Read', {'file_path': 'big.txt'}))[
        'error']


def test_a_one_page_file_is_the_reads_own_result_unchanged():
    one, calls = fake_reads('a,b\n1,2\n', page_chars=60_000)
    first = one('Read', {'file_path': 'small.csv'})
    assert code_mode._whole_read(one, {'file_path': 'small.csv'}, first) is first and len(calls) == 1


# -- F2: the project's own tests and the check, two launches in one call -----------------------------------------------

BUGGY = 'def mean(values):\n    return sum(values) / len(values)\n'
STATS = 'def mean(values):\n    return sum(values) / len(values) if values else 0.0\n'
UNIT = ('import unittest\n\nfrom stats import mean\n\n\nclass MeanTest(unittest.TestCase):\n'
        '    def test_mean(self):\n        self.assertEqual(mean([1, 3]), 2)\n')
EMPTY_CASE = ('import unittest\n\nfrom stats import mean\n\n\nclass EmptyTest(unittest.TestCase):\n'
              '    def test_empty(self):\n        self.assertEqual(mean([]), 0.0)\n')
CHECK = ('import unittest\n\nfrom stats import mean\n\n\nclass EmptyCheck(unittest.TestCase):\n'
         '    def test_empty(self):\n        self.assertEqual(mean([]), 0.0)\n')
EMPTY = 'from stats import mean\n\n\ndef test_empty():\n    assert mean([]) == 0.0\n'  # pytest's style: none for unittest
# w10's review, F2-1: code at the check's module level that changes the shared run, here two ways at once
PATCHING = ('import unittest\nimport stats\n\nstats.mean = lambda values: sum(values) / len(values) if values else 0.0\n'
            "open('stats.py', 'w').write(" + repr(STATS) + ')\n\n\nclass Probe(unittest.TestCase):\n'
            '    def test_probe(self):\n        self.assertEqual(stats.mean([]), 0.0)\n')


def project(tmp_path, files, name='project'):
    root = tmp_path / name
    root.mkdir()
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    return root


def both(root, check, recipe='python-unittest', check_recipe='python-unittest'):
    """A reviewed payload as App.execute proposes it for RunTests(auto, check): the project's plan and the check's."""
    ws = Workspace(root)
    plan, probe = recipes.resolve(recipe, ws), recipes.resolve(check_recipe, ws)
    return {'root': str(root), 'root_identity': list(ws.identity), 'recipe': recipe, 'plan': plan, 'mount': None,
            'drafts': [], 'reviewed_files': recipes.review_bindings(recipe, ws),
            'check': testing.check_payload(check_recipe, check),
            'check_run': {'recipe': check_recipe, 'plan': probe, 'mount': None,
                          'reviewed_files': recipes.review_bindings(check_recipe, ws)}}


def counts_as_evidence(root, result):
    ws = Workspace(root)
    applied = {'kind': 'tests', 'status': 'applied', 'created': 1.0,
               'payload': {'root': str(ws.root), 'root_identity': list(ws.identity), 'result': result}}
    return RunController._current_test_evidence([applied], ws)


@macos
def test_a_check_can_never_decide_the_projects_evidence(tmp_path):
    # test_stats_empty.py sorts after test_pebble_check.py, so in one shared run the check is imported first
    root = project(tmp_path, {'stats.py': BUGGY, 'test_stats.py': UNIT, 'test_stats_empty.py': EMPTY_CASE})
    # the finding reproduced: in one shared launch (1.9.27's single run, which w10's first form counted) the buggy
    # project's own failing test passes, and the check's -v line is there: r1 set counted. It stays kind 'check'.
    shared = testing.run_tests({'root': str(root), 'recipe': 'python-unittest', 'drafts': [],
                                'check': testing.check_payload('python-unittest', PATCHING)}, threading.Event())
    assert shared['outcome'] == 'passed' and shared['check']['tests_run'] == 1 and shared['kind'] == 'check'
    result = testing.run_tests(both(root, PATCHING), threading.Event())
    # 1.9.28 first form: one launch, the check imported first patched mean and rewrote stats.py in the copy, the run
    # passed and counted. Now the project's own run, without the check, fails on the bug and is the evidence
    assert result['kind'] == 'test' and result['outcome'] == 'failed' and 'ZeroDivisionError' in result['output']
    assert 'test_pebble_check' not in result['output'] and counts_as_evidence(root, result) is False
    checked = result['check']
    assert checked['outcome'] == 'passed' and checked['tests_run'] == 1 and checked['recipe'] == 'python-unittest'
    assert checked['note'] == testing.CHECK_RUN_NOTE.format(path='test_pebble_check.py', recipe='python-unittest')
    assert (root / 'stats.py').read_text() == BUGGY and not (root / 'test_pebble_check.py').exists()


@macos
def test_the_projects_run_counts_on_its_own_and_the_check_runs_alone(tmp_path):
    root = project(tmp_path, {'stats.py': STATS, 'test_stats.py': UNIT})
    result = testing.run_tests(both(root, CHECK), threading.Event())
    assert result['kind'] == 'test' and result['outcome'] == 'passed' and counts_as_evidence(root, result) is True
    assert result['tests']['total'] == 1  # the project's own test, and nothing of the check
    # alone (-p): the check's copy ran only the check, not the project's test beside it
    assert result['check']['outcome'] == 'passed' and result['check']['tests'] == {
        'passed': 1, 'failed': 0, 'skipped': 0, 'errors': 0, 'total': 1}
    failing = testing.run_tests(both(root, CHECK.replace('0.0)', '1.0)')), threading.Event())
    assert failing['outcome'] == 'passed' and failing['check']['outcome'] == 'failed'  # the probe fails apart


@macos
def test_a_check_that_collects_nothing_says_ran_0_tests_and_never_passes(tmp_path):
    root = project(tmp_path, {'stats.py': STATS, 'test_stats.py': UNIT})
    result = testing.run_tests(both(root, EMPTY), threading.Event())
    assert result['outcome'] == 'passed'  # the project's own run
    checked = result['check']
    assert checked['outcome'] == 'no_tests' and checked['tests_run'] == 0 and 'Ran 0 tests' in checked['output']
    assert checked['note'].startswith('Ran 0 tests from test_pebble_check.py: it holds no test python-unittest collects')
    # an explicit recipe keeps 1.9.27's single launch, kind 'check', and says the same
    single = testing.run_tests({'root': str(root), 'recipe': 'python-unittest', 'drafts': [],
                                'check': testing.check_payload('python-unittest', EMPTY)}, threading.Event())
    assert single['kind'] == 'check' and single['outcome'] == 'no_tests' and single['check']['note'].startswith('Ran 0')


@node
def test_a_node_check_with_no_test_never_reads_as_passed(tmp_path):
    root = project(tmp_path, {'add.mjs': 'export const add = (a, b) => a + b;\n',
                              'add.test.mjs': 'import test from "node:test";\nimport assert from "node:assert";\n'
                                              'import { add } from "./add.mjs";\n'
                                              'test("add", () => assert.equal(add(1, 2), 3));\n'})
    # w10's review, F2-2 (Node v24): a file that defines no test runs as one passing test named after the file
    result = testing.run_tests(both(root, 'import assert from "node:assert";\n', 'node-test', 'node-test'),
                               threading.Event())
    assert result['outcome'] == 'passed' and result['check']['outcome'] == 'no_tests'
    assert result['check']['tests_run'] == 0 and result['check']['note'].startswith('Ran 0 tests from pebble-check')
    one = testing.run_tests(both(root, 'import test from "node:test";\ntest("probe", () => {});\n', 'node-test',
                                 'node-test'), threading.Event())
    assert one['check']['outcome'] == 'passed' and one['check']['tests_run'] == 1


@macos
@pytest.mark.parametrize('change', [
    lambda p: {k: v for k, v in p.items() if k != 'check'},
    lambda p: {**p, 'check_run': {**p['check_run'], 'recipe': 'python-pytest'}},
    lambda p: {**p, 'check_run': {k: v for k, v in p['check_run'].items() if k != 'reviewed_files'}},
    lambda p: {**p, 'check_run': {**p['check_run'], 'plan': {**p['check_run']['plan'], 'recipe': 'node-test'}}},
    lambda p: {**p, 'plan': None, 'mount': None},
    lambda p: {**p, 'check_run': 'python-unittest'},
])
def test_a_malformed_check_run_is_refused_before_anything_runs(tmp_path, change):
    root = project(tmp_path, {'stats.py': STATS, 'test_stats.py': UNIT})
    with pytest.raises(PolicyError, match='malformed|pinned'):
        testing.run_tests(change(both(root, CHECK)), threading.Event())


@macos
def test_a_project_file_with_the_checks_name_stops_the_call_before_anything_runs(tmp_path, monkeypatch):
    root = project(tmp_path, {'stats.py': STATS, 'test_stats.py': UNIT,
                              'test_pebble_check.py': '# the project owns it\n'})
    launched = []
    monkeypatch.setattr(testing, '_launch', lambda *args: launched.append(args) or (0, b'', 0))
    with pytest.raises(PolicyError, match='already has test_pebble_check.py'):
        testing.run_tests(both(root, CHECK), threading.Event())
    assert launched == []


def test_the_runtests_description_says_both_run_and_only_the_projects_counts():
    spec = next(t for t in tool_catalogue.TOOLS if t['function']['name'] == 'RunTests')
    text = spec['function']['description']
    assert text.startswith(tool_catalogue.RUN_TESTS) and 'unittest.TestCase classes' in text
    assert "With auto the project's own tests also run, and count; the check runs alone" in text
    assert text.endswith('never changes the project and never counts.')
    assert tool_catalogue.without_check(spec)['function']['description'] == tool_catalogue.RUN_TESTS  # 4K, 8K as before


@pytest.fixture
def checked(tmp_path, monkeypatch):
    """The App with only the root model scripted: the real broker, recipes and sandboxed test runner."""
    value = SimpleNamespace(requests=[], script=[], apps=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        value.requests.append(copy.deepcopy(messages))
        assert len(value.requests) <= len(value.script), 'Unscripted root model request.'
        response = copy.deepcopy(value.script[len(value.requests) - 1])
        if response.get('content'):
            token(response['content'])
        return response
    monkeypatch.setattr(Providers, 'complete', complete)

    def start():
        app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
        value.apps.append(app)
        app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'auto'})
        app.model_save(name='tail fixture', url='http://127.0.0.1:9', model='tail-fixture', context=32768)
        return app
    value.start = start
    yield value
    for app in value.apps:
        if not app.closed:
            app.active = app.active_run = None
            app.close()
    close_guard_databases(tmp_path)


def settle(value, app, cid, requests, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = app.runs.list(cid)
        if runs and app.active is None and len(value.requests) == requests and runs[0]['state'] not in (
                'queued', 'running', 'retry_wait'):
            return app.runs.get(runs[0]['id'])
        time.sleep(.02)
    pytest.fail(f'The run did not settle after {requests} requests ({len(value.requests)} made).')


def fix_and_check(checked, root):
    app = checked.start()
    chat = app.new_chat(str(root))
    checked.script = [call('Read', {'file_path': 'stats.py'}, 'read'),
                      call('Write', {'file_path': 'stats.py', 'content': STATS}, 'fix'),
                      call('RunTests', {'recipe': 'auto', 'check': CHECK}, 'check'),
                      final('mean returns 0.0 for an empty list, and the tests pass.\nFINAL: done')]
    app.work(chat, 'Make mean return 0.0 for an empty list.', False, [])
    run = settle(checked, app, chat['id'], 4)  # one RunTests call: no plain run and no repair after the check
    assert run['state'] == 'completed', (run['state'], run['error'])
    [action] = [a for a in app.store.actions(chat['id']) if a['kind'] == 'tests']
    return action


@macos
def test_with_auto_a_unittest_project_counts_its_own_run_and_reports_the_check(checked, tmp_path):
    # tests.py: unittest finds it and pytest's own patterns do not, so auto chooses python-unittest itself
    root = project(tmp_path, {'stats.py': BUGGY, 'tests.py': UNIT})
    action = fix_and_check(checked, root)
    payload = action['payload']
    assert payload['recipe'] == 'python-unittest' and payload['check_run']['recipe'] == 'python-unittest'
    result = action['payload']['result']
    assert result['kind'] == 'test' and result['outcome'] == 'passed' and result['check']['outcome'] == 'passed'


@macos
def test_with_auto_a_pytest_project_runs_its_tests_and_the_check_in_one_call(checked, tmp_path):
    root = project(tmp_path, {'stats.py': BUGGY, 'test_stats.py': UNIT})
    if recipes.resolve('auto', Workspace(root))['recipe'] != 'python-pytest':
        pytest.skip('pytest is not installed for the framework Python here')
    action = fix_and_check(checked, root)  # d-cf-01's shape: the check was refused, then two more requests
    payload = action['payload']
    assert payload['recipe'] == 'python-pytest' and payload['check_run']['recipe'] == 'python-unittest'
    result = action['payload']['result']
    assert result['kind'] == 'test' and result['recipe'] == 'python-pytest' and result['outcome'] == 'passed'
    assert result['check']['outcome'] == 'passed' and result['check']['tests_run'] == 1


# -- F3: stale sources after the run's own Git step ------------------------------------------------------------------

STASH = ('Set my uncommitted work aside, including the new untracked file drafts/idea.md, in a stash named "monthly '
         'view draft". Don\'t commit anything and don\'t push. End your reply with the line `FINAL: done`.')
STASHED = final('Your uncommitted work, drafts/idea.md included, is in the stash "monthly view draft".\n\nFINAL: done')
RENAME = ('Rename app/report.py to app/summary.py with git mv and commit it as "Rename report module". Don\'t push. '
          'End your reply with the line `FINAL: done`.')
RENAMED = final('app/report.py is now app/summary.py, committed as "Rename report module".\n\nFINAL: done')


def money_repo(project):
    git(project, 'init', '-q')
    (project / 'app').mkdir()
    (project / 'app' / 'money.py').write_text('def total(items):\n    return sum(items)\n')
    (project / 'app' / 'report.py').write_text("from app.money import total\n\n\ndef summary(items):\n"
                                               "    return 'Total: %s' % total(items)\n")
    git(project, 'add', '-A')
    git(project, 'commit', '-q', '-m', 'Initial import')
    git(project, 'config', 'user.name', 'Reviewed Author')
    git(project, 'config', 'user.email', 'author@example.invalid')
    (project / 'drafts').mkdir()
    (project / 'drafts' / 'idea.md').write_text('# Monthly view\n\nGroup totals by month.\n')


def git_run(env, monkeypatch, text, script, prepare=None):  # noqa: F811
    money_repo(env.project)
    if prepare:
        prepare(env.project)
    app = env.start(32768)
    app.store.set_setting('permissions', {**DEFAULT, 'mode': 'auto'})
    monkeypatch.setattr(app.guard, 'handle', lambda *args, **kwargs: {'action': 'allow'})
    env.script = script
    chat = app.new_chat(str(env.project))
    app.work(chat, text, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    run = finished(app, chat['id'], rid)
    assert run['state'] == 'completed', (run['state'], run['error'])
    return app, rid


def events(app, rid, kind):
    return [json.loads(r['payload']) for r in app.store.query(
        'SELECT payload FROM run_events WHERE run_id=? AND kind=? ORDER BY sequence', (rid, kind))]


def user_texts(request):
    return [m['content'] for m in request if m.get('role') == 'user' and isinstance(m.get('content'), str)]


def repair_text(request):
    return next(t for t in reversed(user_texts(request)) if t.startswith('HOST COMPLETION CHECK:'))


@git_macos
@pytest.mark.parametrize('settle_on, recheck_on, requests', [(True, True, 2), (False, True, 3), (False, False, 4)])
def test_a_file_the_runs_own_stash_removed_is_settled_by_the_host(env, monkeypatch, settle_on, recheck_on,  # noqa: F811
                                                                    requests):
    if not settle_on:
        monkeypatch.setattr(runner, 'settle_git_sources', lambda *args: '')
    if not recheck_on:
        monkeypatch.setattr(runner, 'recheck_sources', lambda *args: ('', False))
    script = [command('s', 'stash', 'push', '-u', '-m', 'monthly view draft'), STASHED]
    script += [] if settle_on else [STASHED] if recheck_on else [call('Read', {'file_path': 'drafts/idea.md'}, 'r'),
                                                                 STASHED]
    app, rid = git_run(env, monkeypatch, STASH, script)
    assert len(env.requests) == requests and 'monthly view draft' in git(env.project, 'stash', 'list').stdout
    gone = json.dumps('drafts/idea.md') + ': ' + runner.GONE
    if settle_on:
        # the resume after the decision carries the host's absence witness: the reply is written once, and checked
        resumed = '\n'.join(user_texts(env.requests[1]))
        assert runner.GIT_REREAD_NOTE.strip() in resumed and gone in resumed
        assert not events(app, rid, 'reply_withheld') and events(app, rid, 'source_recheck')[0]['after'] == 'git'
    elif recheck_on:
        repair = repair_text(env.requests[2])
        assert gone in repair and runner.REPAIR_ADVICE not in repair
    else:
        repair = repair_text(env.requests[2])
        assert '"drafts/idea.md" (removed)' in repair and runner.STALE_REPAIR in repair
        assert runner.REPAIR_ADVICE not in repair  # d-gw-108: "change the code or the command" sent it guessing


@git_macos
def test_a_file_the_runs_own_git_mv_moved_is_settled_by_the_host(env, monkeypatch):  # noqa: F811
    script = [command('m', ['mv', 'app/report.py', 'app/summary.py'], ['commit', '-m', 'Rename report module']),
              RENAMED]
    app, rid = git_run(env, monkeypatch, RENAME, script)
    assert len(env.requests) == 2 and not (env.project / 'app' / 'report.py').exists()
    assert git(env.project, 'log', '-1', '--format=%s').stdout.strip() == 'Rename report module'
    assert json.dumps('app/report.py') + ': ' + runner.GONE in '\n'.join(user_texts(env.requests[1]))
    assert not events(app, rid, 'reply_withheld')


@git_macos
def test_a_file_a_command_wrote_after_the_plan_is_never_settled_by_the_host(env, monkeypatch):  # noqa: F811
    script_text = ("python3 -c \"import os; os.makedirs('drafts', exist_ok=True); "
                   "open('drafts/idea.md', 'w').write('rewritten by a script')\"")
    script = [command('s', 'stash', 'push', '-u', '-m', 'monthly view draft'),
              call('RunCommand', {'command': script_text, 'network': False}, 'c'), STASHED,
              call('Read', {'file_path': 'drafts/idea.md'}, 'r'), STASHED]
    app, rid = git_run(env, monkeypatch, STASH, script)
    assert len(env.requests) == 5 and (env.project / 'drafts' / 'idea.md').read_text() == 'rewritten by a script'
    # the host read the file once, when the stash removed it; the script's file is the model's own Read to make
    host_reads = [t for t in app.runs.tools(rid) if str(t['call_id']).startswith('host:recheck_')]
    assert [t['arguments']['file_path'] for t in host_reads] == ['drafts/idea.md']
    assert 'error' in host_reads[0]['result']  # the absence witness, before the script ran
    repair = repair_text(env.requests[3])
    assert '"drafts/idea.md" (changed)' in repair and runner.GIT_REREAD_NOTE.strip() not in repair
    assert runner.RECHECK_NOTE.strip() not in repair


@git_macos
def test_heads_version_written_by_a_command_beside_an_unrelated_plan_is_no_exemption(env, monkeypatch):  # noqa: F811
    # w10's review, F3-4: a branch created (no file changes), then a script puts HEAD's bytes back into a file the run
    # read with uncommitted changes. 1.9.27's git_restored let the host read it again; the model reads it itself now
    edited = 'def total(items):\n    return sum(items)  # draft\n'
    restore = "python3 -c \"open('app/money.py', 'w').write('def total(items):\\n    return sum(items)\\n')\""
    script = [command('b', 'branch', 'spare'), call('RunCommand', {'command': restore, 'network': False}, 'c'),
              final('Done.\n\nFINAL: done'), call('Read', {'file_path': 'app/money.py'}, 'r'),
              final('app/money.py holds the committed version again.\n\nFINAL: done')]
    app, rid = git_run(env, monkeypatch, 'Create the branch spare, then restore app/money.py as committed. End your '
                       'reply with the line `FINAL: done`.', script,
                       prepare=lambda root: (root / 'app' / 'money.py').write_text(edited))
    assert len(env.requests) == 5 and git(env.project, 'diff', '--quiet', 'HEAD').returncode == 0
    assert not [t for t in app.runs.tools(rid) if str(t['call_id']).startswith('host:recheck_')]
    assert '"app/money.py" (changed)' in repair_text(env.requests[3])


def test_an_external_change_stays_stale_and_its_repair_names_the_file(env):  # noqa: F811
    # validate_phase15 s1, offline: a file the run read changes before settlement, with no Git plan of the run
    import re
    stale_completion = re.compile(r'source file\(s\) read in this run (?:changed|can no longer be checked)', re.I)
    (env.project / 'facts.txt').write_text('release: Aurora\n')

    def changed_then_reply():
        (env.project / 'facts.txt').write_text('release: Borealis\n')  # the person edits it before the turn ends
        return final('The release name is Aurora.')
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    env.script = [call('Read', {'file_path': 'facts.txt'}, 'read'), changed_then_reply,
                  call('Read', {'file_path': 'facts.txt'}, 'again'), final('The release name is now Borealis.')]
    app.work(chat, 'Read facts.txt and report the release name.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    settled(env, app, rid, 'completed', 4)
    repair = repair_text(env.requests[2])
    assert stale_completion.search(repair) and '"facts.txt" (changed)' in repair and runner.REPAIR_ADVICE not in repair
    assert not [t for t in app.runs.tools(rid) if str(t['call_id']).startswith('host:recheck_')]  # never exempted


def test_a_file_name_never_starts_a_line_of_the_hosts_message():
    # w10's review, F3-1: a repository file whose name holds a newline and text of its own
    name = 'notes.md\nLINE-TWO-MARKER (approved).md'
    app = SimpleNamespace(stale_sources=lambda rid, ws: [(name, 'changed'), ('gone.txt', 'deleted')])
    named = runner.stale_named(app, 'run', None)
    assert '\n' not in named and json.dumps(name) + ' (changed)' in named and '"gone.txt" (removed)' in named


def test_git_lookups_never_exceed_what_the_host_would_read(tmp_path):
    # w10's review, F3-2: git_explained is asked about at most RECHECK_FILES small changed and RECHECK_ABSENT removed
    for n in range(30):
        (tmp_path / f'f{n}.txt').write_text('x')
    (tmp_path / 'big.txt').write_text('x' * runner.RECHECK_BYTES)
    stale = [('big.txt', 'changed')] + [(f'f{n}.txt', 'changed') for n in range(30)] + [
        (f'gone{n}.txt', 'deleted') for n in range(20)]
    asked = []
    app = SimpleNamespace(stale_sources=lambda rid, ws: stale, git_restored=lambda *args: True,
                          git_explained=lambda rid, cid, ws, files: asked.append(list(files)) or [],
                          runs=SimpleNamespace(note=lambda *args: None))
    assert runner.settle_git_sources(app, 'chat', 'run', Workspace(tmp_path), 32768) == ''
    assert asked == [[(f'f{n}.txt', 'changed') for n in range(runner.RECHECK_FILES)]
                     + [(f'gone{n}.txt', 'deleted') for n in range(runner.RECHECK_ABSENT)]]
    asked.clear()
    assert runner.recheck_sources(app, 'chat', 'run', Workspace(tmp_path), 32768) == ('', False) and asked == []


def test_each_host_re_read_has_its_own_receipt():
    reads = []
    app = SimpleNamespace(execute=lambda name, args, ws, cid, call_id=None: reads.append(call_id) or {
        'path': args['file_path'], 'total_lines': 1, 'untrusted_source': 'x'},
        runs=SimpleNamespace(tools=lambda rid: [{'call_id': 'host:recheck_1'}, {'call_id': '1:read'}]))
    views, every = runner.reread_sources(app, 'chat', 'run', None, 32768, [('a.txt', 'changed'), ('b.txt', 'changed')])
    assert every and len(views) == 2 and reads == ['host:recheck_2', 'host:recheck_3']  # never the first one's cache


# -- F5: a staged command after an earlier response's staged edit ----------------------------------------------------

EXTRA = ('import unittest\n\nfrom calc import add\n\n\nclass ExtraTest(unittest.TestCase):\n'
         '    def test_zero(self):\n        self.assertEqual(add(0, 0), 0)\n')
PROBE = 'python3 -c "from calc import add; print(add(2, 3))"'


def test_outside_ask_a_staged_command_after_an_earlier_held_edit_waits_at_once(env):  # noqa: F811
    app = env.start(32768)
    app.store.set_setting('permissions', {**DEFAULT, 'mode': 'auto'})  # the golden harness's mode
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    # d-lh-91 turn 4: a test edit the request did not ask for waits for review (edit_scope), then a reviewed command
    env.script = [call('Write', {'file_path': 'test_extra.py', 'content': EXTRA}, 'w'),
                  call('RunCommand', {'command': PROBE, 'network': False}, 'c'),
                  final('add(2, 3) is 5; the extra test waits for you.\nFINAL: done')]
    app.work(chat, 'Check what add(2, 3) gives with a command. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 2)  # 1.9.27: a third request wrote the reply, held until the decision
    assert pending(app, cid, 'edit')['payload'].get('scope_review')
    assert 'staged_stop' in kinds(app, rid) and 'reply_withheld' not in kinds(app, rid)
    app.approve(cid, pending(app, cid, 'edit')['id'])
    app.approve(cid, pending(app, cid, 'command')['id'])
    settled(env, app, rid, 'completed', 3)
    resumed = json.dumps(env.requests[2])
    assert 'The decision you waited for is in' in resumed and 'was not shown to the person' not in resumed


def test_in_ask_mode_staged_edits_keep_their_batch_review_and_held_reply(env):  # noqa: F811
    app = env.start(32768)  # Ask: every edit waits in one batch review (H20, validate_phase20 a1), as in 1.9.27
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.script = [call('Write', {'file_path': 'NOTES.md', 'content': '# Notes\n'}, 'w'),
                  call('RunCommand', {'command': 'touch made.txt', 'network': False}, 'c'),
                  final('NOTES.md and made.txt wait for your review.\nFINAL: staged')]
    app.work(chat, 'Write NOTES.md, then create made.txt with a command. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    settled(env, app, rid, 'waiting_approval', 3)
    assert 'staged_stop' not in kinds(app, rid) and 'reply_withheld' in kinds(app, rid)


@pytest.mark.parametrize('policy, stops', [
    ({'mode': 'auto'}, True), ({'mode': 'full'}, True), ({'mode': 'ask'}, False), ({}, False), (None, False),
    ({'mode': 'Auto'}, False), ('auto', False)])
def test_only_auto_and_full_let_a_staged_stop_pass_an_earlier_edit(policy, stops):
    assert runner.stop_beside_edits(policy) is stops  # w10's review, F5-1: closed when the mode is unknown


# -- F7: the Anthropic stream inside the watchdog --------------------------------------------------------------------

def sse(events):
    return [b'data: ' + json.dumps(event).encode() + b'\n' for event in events]


class Quiet:
    """A streamed response that sends `lines`, then nothing until the watchdog aborts it; `pause` before the rest."""

    def __init__(self, lines=(), rest=(), pause=0.0, hold=True):
        self.lines, self.rest, self.pause, self.hold = list(lines), list(rest), pause, hold
        self.gate, self.aborted = threading.Event(), False

    def abort(self):
        self.aborted = True
        self.gate.set()

    def __iter__(self):
        yield from self.lines
        if self.pause:
            self.gate.wait(self.pause)
        yield from self.rest
        if self.hold:
            self.gate.wait(10)


def claude(tmp_path, monkeypatch, responses, kind='anthropic', url='https://api.anthropic.com/v1'):
    sent = []

    @contextlib.contextmanager
    def request(provider, target, headers, body=None, timeout=30):
        sent.append(copy.deepcopy(body))
        yield responses.pop(0)
    monkeypatch.setattr(Providers, 'request', request)
    monkeypatch.setattr(Providers, 'key', lambda self, p: 'fixture-key')
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    app.store.set_setting('model_profiles', [{
        'id': 'a', 'kind': kind, 'name': 'Claude fixture', 'url': url, 'model': 'claude-opus-5-5', 'context': 200_000,
        'budget': 200_000, 'budget_version': 2, 'context_mode': 'full', 'tools': True}])
    app.store.set_setting('active_profile', 'a')
    return app, sent


MESSAGES = [{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'hi', '_pebble_current': True}]


def test_a_silent_claude_request_is_abandoned_at_its_first_event_budget(tmp_path, monkeypatch):
    silent = Quiet()
    app, _ = claude(tmp_path, monkeypatch, [silent])
    try:
        app.model.stream_budget = StreamBudget(1.0)
        began = time.monotonic()
        with pytest.raises(StreamStalled) as stalled:
            app.model.complete(copy.deepcopy(MESSAGES), [], threading.Event(), lambda part: None)
        assert stalled.value.phase == 'first_event' and stalled.value.retryable and silent.aborted
        assert time.monotonic() - began < 5
    finally:
        app.close()


def test_a_claude_stream_that_started_is_not_cut_by_the_first_event_budget(tmp_path, monkeypatch):
    events = sse(CLAUDE_FINAL)
    slow = Quiet(lines=events[:1], rest=events[1:], pause=1.5, hold=False)  # message_start at once, the text later
    app, _ = claude(tmp_path, monkeypatch, [slow])
    try:
        app.model.stream_budget = StreamBudget(1.0)
        reply = app.model.complete(copy.deepcopy(MESSAGES), [], threading.Event(), lambda part: None)
        assert reply['content'] == 'add returns a + b.' and not slow.aborted
    finally:
        app.close()


def test_the_first_event_budget_grows_with_the_request_up_to_1_9_27s_wait():
    # w10's review, F7-1: 20 s by default plus 1 s per 2,000 input tokens, never above REQUEST_TIMEOUT (60 s)
    budget = StreamBudget().resolved('medium')
    assert providers.first_event_budget(budget, 0).first_event == 20.0
    assert providers.first_event_budget(budget, 32_000).first_event == 36.0
    assert providers.first_event_budget(budget, 200_000).first_event == REQUEST_TIMEOUT == 60.0
    assert providers.first_event_budget(budget, None).first_event == REQUEST_TIMEOUT  # an unknown size: 1.9.27's wait
    assert providers.first_event_budget(budget, float('nan')).first_event == REQUEST_TIMEOUT
    assert providers.first_event_budget(budget, 3000.0).first_event == 21.5  # the estimate may be a float
    # Providers.last_input_estimate is the context manifest's count: the request as sent is its 'after'
    manifest = {'before': 90_000, 'after': 8000, 'tools_only': None}
    assert providers.first_event_budget(budget, manifest).first_event == 24.0
    assert providers.first_event_budget(budget, {'before': 9000}).first_event == REQUEST_TIMEOUT
    assert providers.first_event_budget(budget, 32_000).idle == budget.idle == 90.0
    assert providers.first_event_budget(StreamBudget(REQUEST_TIMEOUT, 90.0), 0).first_event == 60.0  # loopback
    assert providers.first_event_budget(StreamBudget(5.0, 90.0), 4000).first_event == 7.0  # a run's own budget, grown
    # the ChatGPT route keeps its own 20 s (responses.stream_budget is unchanged)
    chatgpt = {'kind': 'chatgpt', 'url': 'https://chatgpt.com/backend-api/codex', 'model': 'gpt-6.1-sol'}
    assert responses.stream_budget(SimpleNamespace(stream_budget=None), chatgpt).first_event == 20.0


@pytest.mark.parametrize('tokens, pause, dropped', [(6000, 2.5, False), (0, 2.5, True)])
def test_a_slow_first_event_within_the_grown_budget_is_never_dropped(tmp_path, monkeypatch, tokens, pause, dropped):
    late = Quiet(rest=sse(CLAUDE_FINAL), pause=pause, hold=False)  # no line at all for 2.5 s
    app, _ = claude(tmp_path, monkeypatch, [late])
    try:
        provider = app.model
        provider.stream_budget, provider.last_input_estimate = StreamBudget(1.0), tokens  # 1 + 3 s, or 1 s
        opened = provider.open_stream(provider.current(), '/messages', {}, {}, 'default', threading.Event())
        with opened as (response, lines):
            if dropped:
                with pytest.raises(StreamStalled):
                    list(lines)
            else:
                assert len(list(lines)) == len(CLAUDE_FINAL) and not late.aborted
    finally:
        app.close()


def test_other_routes_stream_as_before(tmp_path, monkeypatch):
    plain = sse([{'choices': [{'delta': {'content': 'ok'}}]}])
    app, _ = claude(tmp_path, monkeypatch, [plain], kind='openai', url='https://gateway.example.test/v1')
    try:
        with app.model.open_stream(app.model.current(), '/chat/completions', {}, {}, 'default',
                                   threading.Event()) as (response, lines):
            assert lines is response is plain  # no watchdog, no helper thread: the 1.9.27 path
    finally:
        app.close()


def test_a_claude_run_retries_a_stalled_request_after_its_budget(tmp_path, monkeypatch):
    from test_stream_watchdog_22 import notes, settle as settled_run
    app, sent = claude(tmp_path, monkeypatch, [Quiet(), sse(CLAUDE_FINAL)])
    try:
        cid = app.new_chat(None)['id']
        began = time.monotonic()
        rid = app.send(cid, 'What does add do?', watchdog={'first_event_seconds': 1})['run_id']
        run = settled_run(app, rid)
        budget = providers.first_event_budget(StreamBudget(1.0), app.model.last_input_estimate).first_event
        assert run['state'] == 'completed' and len(sent) == 2 and time.monotonic() - began < budget + 10
        assert notes(app, rid, 'model_attempt_failed') == [{'attempt': 1, 'status': None, 'type': 'StreamStalled'}]
    finally:
        app.active = app.active_run = None
        app.close()


# -- F8: the second cache breakpoint, behind PEBBLE_1928_CACHE_BREAKPOINT ---------------------------------------------

def test_the_breakpoint_switch_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv('PEBBLE_1928_CACHE_BREAKPOINT', raising=False)
    for _, body in claude_run(tmp_path, monkeypatch, 'https://api.anthropic.com/v1', tool_turns=2):
        assert isinstance(body['system'], str) and json.dumps(body).count('cache_control') == 1


def test_the_breakpoint_splits_the_system_the_same_way_on_every_step(tmp_path, monkeypatch):
    monkeypatch.setenv('PEBBLE_1928_CACHE_BREAKPOINT', '1')
    sent = claude_run(tmp_path, monkeypatch, 'https://api.anthropic.com/v1', tool_turns=2)
    assert len(sent) >= 3
    first = sent[0][1]['system']
    shared, rest = first
    assert shared['cache_control'] == {'type': 'ephemeral'} and 'cache_control' not in rest
    assert shared['text'].startswith(runner.large_system(tool_catalogue.SYSTEM)) and shared['text'].endswith(
        runner.PRECISION)
    for headers, body in sent:
        # the same split each step, so the prefix the thinking blocks are bound to never changes between steps
        assert body['system'] == first and json.dumps(body).count('cache_control') == 2
        assert body['cache_control'] == {'type': 'ephemeral'}
        assert body['thinking'] == {'type': 'adaptive', 'block_binding': {'prefix_mismatch_behavior': 'drop_block'}}
        assert headers['anthropic-beta'] == THINKING_BINDING_BETA


@pytest.mark.parametrize('url', ['https://gateway.example.test/v1', 'http://127.0.0.1:9/v1'])
def test_gateways_never_get_the_breakpoint(tmp_path, monkeypatch, url):
    monkeypatch.setenv('PEBBLE_1928_CACHE_BREAKPOINT', '1')
    for _, body in claude_run(tmp_path, monkeypatch, url):
        assert isinstance(body['system'], str) and 'cache_control' not in json.dumps(body)
