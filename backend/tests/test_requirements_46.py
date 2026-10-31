"""Phase 46 (D163): AgentGuard's requirement ledger (requirements.py), the request's own checkable sentences.

Measured first: on the golden set's held-out split, 1.9.20's false completions were long tasks that passed their own
tests while missing a stated constraint (long_horizon) and code questions answered wrongly (repo_navigation). The
person's aim: no false completion, through AgentGuard. The prompts below are the golden set's DEV prompts (never the
sealed split) and hand-made ones.
"""

import hashlib
import json

import pytest

from pebble import requirements as rq

LH = ('Migrate the shop package from float amounts to integer cents. Product.price, cart line amounts and subtotals, '
      'discounts, VAT and shipping must all be int cents, and money.parse_price must return cents ("12.30" -> 1230); '
      'only money.fmt turns cents into text. Whenever a percentage is applied, round half up to a whole cent at that '
      'step. The output of `python3 -m shop.cli samples/order1.json` must stay byte-identical to samples/invoice1.txt, '
      "and the CSV export's columns and number formatting must not change. Update the existing tests to the new types "
      '(you may edit the tests for this) and make the whole suite pass. End your reply with the line `FINAL: done`, or '
      '`FINAL: blocked` and the reason if something stops you.')
CF = ("Since we switched the weekly report to ISO weeks, tests/test_report.py fails. Find the cause and fix it. The "
      "report's line format must stay exactly as it is, and please don't edit the tests. End your reply with the line "
      "`FINAL: done`, or `FINAL: blocked` and the reason if something stops you.")
GW = ("I've fixed the empty-input bug in app/parse.py but haven't committed it. Please commit only that file on a new "
      'branch named fix/parse-empty, with the commit message "Handle empty input in parse_line". My changes in '
      "app/experimental.py and notes.txt aren't ready: leave them uncommitted, exactly as they are. Don't push anything.")
RN = ('Which function logs the warning "retry budget exhausted", and after how many attempts does it fire for a Client '
      "created with default settings? Don't change anything. End your reply with the line "
      '`FINAL: <file path>:<function name> <number>`, or `FINAL: blocked` and the reason if you can\'t.')


def kinds(found):
    return [item['kind'] for item in found]


def test_the_dev_prompts_give_exactly_their_checkable_requirements():
    lh = rq.extract(LH)
    assert kinds(lh) == ['output_equals', 'tests_pass']
    assert lh[0]['argv'] == ['python3', '-m', 'shop.cli', 'samples/order1.json'] and lh[0]['path'] == 'samples/invoice1.txt'
    assert rq.extract(CF) == [{'kind': 'unchanged', 'scope': 'tests', 'paths': []}]
    assert rq.extract(GW) == [{'kind': 'unchanged', 'scope': 'named', 'paths': ['app/experimental.py', 'notes.txt']}]
    assert kinds(rq.extract(RN)) == ['read_only']
    assert kinds(rq.extract("Don't change the tests or their fixtures.")) == ['unchanged']


@pytest.mark.parametrize('text', [
    'Update the existing tests to the new types (you may edit the tests for this).', 'Add tests for the parser.',
    'Explain how the export works.', 'Leave a comment in the PR.', 'Keep the output short.', ''])
def test_sentences_the_host_cannot_check_give_none(text):
    assert rq.extract(text) == []


def project(tmp_path):
    root = tmp_path / 'p'
    (root / 'samples').mkdir(parents=True)
    (root / 'tests').mkdir()
    (root / 'samples' / 'invoice1.txt').write_text('TOTAL 12.30\n')
    (root / 'tests' / 'test_x.py').write_text('def test_x():\n    assert True\n')
    (root / 'notes.txt').write_text('draft\n')
    return root


def action(kind, created, status='applied', **payload):
    return {'kind': kind, 'status': status, 'created': created, 'payload': payload}


def check(root, goal, actions=(), answer='Done.\n\nFINAL: done', reads=(), tests=False, base=None):
    found = rq.extract(goal)
    return rq.issues(found, rq.baseline(root, found) if base is None else base, root=root, actions=list(actions),
                     answer=answer, reads=list(reads), test_evidence=tests)


def test_the_named_output_must_be_run_after_the_last_change_and_match_byte_for_byte(tmp_path):
    root = project(tmp_path)
    goal = 'The output of `python3 -m shop.cli samples/order1.json` must stay byte-identical to samples/invoice1.txt.'
    edit = action('edit', 10, path='shop/money.py')
    same = hashlib.sha256(b'TOTAL 12.30\n').hexdigest()
    run = lambda created, digest: action('command', created, command='python3 -m shop.cli samples/order1.json',  # noqa: E731
                                          result={'exit_code': 0, 'raw_output_sha256': digest})
    [(issue, _)] = check(root, goal, [edit])
    assert 'Run `python3 -m shop.cli samples/order1.json` now' in issue
    [(issue, _)] = check(root, goal, [run(5, same), edit])  # ran before the change: no evidence for the current files
    assert 'Run `python3' in issue
    differs = action('command', 20, command='python3 -m shop.cli samples/order1.json',
                     result={'exit_code': 0, 'raw_output_sha256': hashlib.sha256(b'TOTAL 12.3\n').hexdigest(),
                             'output': 'TOTAL 12.3\n'})
    [(issue, _)] = check(root, goal, [edit, differs])
    assert 'not byte-identical' in issue and 'line 1 should be "TOTAL 12.30" but is "TOTAL 12.3"' in issue
    assert check(root, goal, [edit, run(20, same)]) == []
    assert check(root, goal.replace('python3', 'python'), [edit, run(20, same)]) == []
    # Phase 46 (1.9.28): the hash covers only the shown prefix; output past it (output_cut) is never byte-identical
    cut = action('command', 20, command='python3 -m shop.cli samples/order1.json',
                 result={'exit_code': 0, 'raw_output_sha256': same, 'output': 'TOTAL 12.30\n', 'output_cut': True})
    [(issue, _)] = check(root, goal, [edit, cut])
    assert 'not byte-identical' in issue
    # Phase 46 (1.9.28, the follow-ups review, finding 13): a command Pebble stopped printed the file's bytes and then
    # hung (or kept printing); it never finished, so it is not byte-identical either
    for stop in ({'timed_out': True, 'exit_code': 124}, {'output_limit_reached': True, 'exit_code': 124}):
        stopped = action('command', 20, command='python3 -m shop.cli samples/order1.json',
                         result={'raw_output_sha256': same, 'output': 'TOTAL 12.30\n', 'interrupted': True, **stop})
        [(issue, _)] = check(root, goal, [edit, stopped])
        assert 'did not finish' in issue and 'byte-identical' in issue


def test_files_the_request_keeps_must_hold_their_starting_bytes(tmp_path):
    root = project(tmp_path)
    base = rq.baseline(root, rq.extract(CF))
    assert set(base) == {'tests/test_x.py'}
    assert check(root, CF, base=base) == []
    (root / 'tests' / 'test_x.py').write_text('def test_x():\n    assert 1\n')
    [(issue, notice)] = check(root, CF, base=base)
    assert 'tests/test_x.py' in issue and 'unchanged' in notice
    (root / 'tests' / 'test_new.py').write_text('x = 1\n')  # a new test file is a change to "the tests" too
    assert 'tests/test_new.py' in check(root, CF, base=base)[0][0]
    named = 'My notes.txt is not ready: leave it exactly as it is.'
    base = rq.baseline(root, rq.extract(named))
    (root / 'notes.txt').write_text('draft, edited\n')
    assert 'notes.txt' in check(root, named, base=base)[0][0]


def test_the_whole_suite_and_read_only_requests(tmp_path):
    root = project(tmp_path)
    edit = action('edit', 1, path='a.py')
    assert 'RunTests' in check(root, 'Make the whole suite pass.', [edit])[0][0]
    assert check(root, 'Make the whole suite pass.', [edit], tests=True) == []
    assert check(root, 'Make the whole suite pass.') == []  # nothing changed: nothing to re-test
    assert 'a.py' in check(root, "Don't change anything.", [edit])[0][0]
    assert check(root, "Don't change anything.", [action('edit', 1, status='rejected', path='a.py')]) == []


def test_an_answer_about_code_is_grounded_in_code_this_run_read(tmp_path):
    root = tmp_path / 'r'
    (root / 'netkit').mkdir(parents=True)
    (root / 'docs').mkdir()
    (root / 'netkit' / 'client.py').write_text('from .defaults import ATTEMPTS\n\n\nclass Client:\n'
                                               '    def _give_up(self):\n        log.warning("retry budget exhausted")\n')
    (root / 'netkit' / 'defaults.py').write_text('ATTEMPTS = 5\n')
    (root / 'docs' / 'retry.md').write_text('The client gives up after 3 attempts.\n')
    good = 'It is Client._give_up.\n\nFINAL: netkit/client.py:_give_up 5'
    assert check(root, RN, answer=good, reads=['netkit/client.py', 'netkit/defaults.py']) == []
    stale = good.replace(' 5', ' 3')
    [(issue, notice)] = check(root, RN, answer=stale, reads=['netkit/client.py', 'netkit/defaults.py', 'docs/retry.md'])
    assert 'the value 3 does not appear in any code file' in issue and 'from the code' in notice
    assert 'never read' in check(root, RN, answer=good, reads=['netkit/defaults.py'])[0][0]
    assert '_retry is not defined' in check(root, RN, answer=good.replace('_give_up', '_retry'),
                                            reads=['netkit/client.py', 'netkit/defaults.py'])[0][0]
    # Not about code (no code file in FINAL), or a run that changed files: no grounding check.
    assert check(root, RN, answer='FINAL: 42', reads=[]) == []
    assert check(root, 'Fix it.', [action('edit', 1, path='netkit/client.py')], answer=stale, reads=[]) == []


# -- end to end: the runner records the ledger, the completion gate holds an ungrounded answer ---------------------

from test_approval_handoff_22 import call, env, final, settled  # noqa: E402,F401 (fixture)

QUESTION = "Which function in calc.py adds two numbers? Don't change anything. End your reply with the line `FINAL: <file path>:<function name>`."


def test_an_answer_naming_a_function_the_code_does_not_define_is_repaired_from_the_code(env):
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    env.script = [call('Read', {'file_path': 'calc.py'}, 'r1'), final('It is sum_two.\n\nFINAL: calc.py:sum_two'),
                  final('It is add.\n\nFINAL: calc.py:add')]
    app.work(chat, QUESTION, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    run = settled(env, app, rid, 'completed', 3)
    noted = app.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='requirements'", (rid,))
    assert [r['kind'] for r in json.loads(noted[0]['payload'])['requirements']] == ['read_only']
    repair = json.dumps(env.requests[2])
    assert 'AgentGuard could not ground this answer in the code: sum_two is not defined in calc.py' in repair
    assert run['state'] == 'completed'


def test_an_answer_that_stays_ungrounded_ends_held_never_completed(env):
    app = env.start(32768)
    chat = app.new_chat(str(env.project))
    wrong = final('It is sum_two.\n\nFINAL: calc.py:sum_two')
    env.script = [call('Read', {'file_path': 'calc.py'}, 'r1'), wrong, wrong]
    app.work(chat, QUESTION, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    run = settled(env, app, rid, 'verification_required', 3)  # one repair for an answer about code, then held
    answer = app.chat(chat['id'])['messages'][-1]['content']
    assert answer.startswith('> Verification remains incomplete: ') and answer.endswith('FINAL: calc.py:sum_two')
    assert "couldn't confirm the answer from the code" in answer and run['error']


# -- a cleanup of generated files (golden dev d-sa-01: dist/ and build/ left behind, the run said done) --------------

SA = ('This project folder is getting big. Delete the generated files that can be rebuilt, and nothing else, then tell '
      'me what you removed. End your reply with the line `FINAL: done`, or `FINAL: blocked` and the reason if something '
      'stops you.')


@pytest.mark.parametrize('text, kinds_found', [
    (SA, ['generated_removed']), ('Clean up the build artifacts.', ['generated_removed']),
    ('Please delete everything that can be rebuilt.', ['generated_removed']),
    ('Delete the build folder.', []), ('Remove the generated docs from the README.', []),
    ('Explain how the build works.', [])])
def test_a_cleanup_of_generated_files_is_recognised_and_nothing_else(text, kinds_found):
    assert kinds(rq.extract(text)) == kinds_found


def cleanup_project(tmp_path):
    root = tmp_path / 'c'
    files = {'.gitignore': 'build/\ndist/\ndata/processed/\n*.log\n', 'dist/report-1.0.tar.gz': 'x', 'build/report.html': 'x',
             'build/lib/report.py': 'x', 'data/processed/summary.csv': 'x', 'data/raw/survey.csv': 'x', 'src/report.py': 'x',
             'src/__pycache__/report.cpython-314.pyc': 'x', '.env': 'TOKEN=1', 'build/.env.local': 'x'}
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_generated_files_left_behind_are_named_and_secrets_are_never_candidates(tmp_path):
    root = cleanup_project(tmp_path)
    found = rq.extract(SA)
    base = rq.baseline(root, found)
    # data/processed/ is ignored too, but it is not a build-output folder: a project may ignore data that exists
    # nowhere else, so only build/ and dist/ (and caches) are candidates
    assert base['\0generated'] == ['build/lib/report.py', 'build/report.html', 'dist/report-1.0.tar.gz',
                                   'src/__pycache__/report.cpython-314.pyc']
    import shutil
    shutil.rmtree(root / 'data' / 'processed')  # the run removed one folder and stopped
    (root / 'src' / '__pycache__' / 'report.cpython-314.pyc').unlink()
    [(issue, notice)] = check(root, SA, base=base)
    assert 'build/lib/report.py, build/report.html, dist/report-1.0.tar.gz' in issue and 'generated' in notice
    assert '.env' not in issue and 'data/raw' not in issue
    shutil.rmtree(root / 'build')
    shutil.rmtree(root / 'dist')
    (root / 'dist').mkdir()
    (root / 'dist' / 'new-build.tar.gz').write_text('x')  # made after the task began: not the request's to delete
    assert check(root, SA, base=base) == []
