"""Phase 24 (V1-094, V1-053, V1-141): AgentGuard's one-shot challenges and command checks hit what they should.

0.6.1 sent Pebble's read-only tools other than five, and RunTests and RunRecipe, through scope checks (after five
unrelated edits RunTests was refused once with a scope message); challenged real modules beyond the 1,500-file mirror
cut as "could not be verified"; let Append skip the evidence engine; screened typed process input with the challenge
ledger, so retrying "DROP TABLE users;" was allowed; and asked for a review of read-only commands such as
`rg "DROP TABLE" migrations`, with no reason on the card and a "narrowed" refusal though nothing ran.

GuardAdapter and the vendored engine are the checked-in code; no model, network or shell.
"""

from types import SimpleNamespace

import pytest

from pebble.guard import GuardAdapter
from pebble.policy import Workspace


def make_project(root, files):
    root.mkdir(parents=True)
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


@pytest.fixture
def make(tmp_path):
    made = []

    def start(files, goal):
        project = make_project(tmp_path / f'case{len(made)}' / 'project', files)
        guard = GuardAdapter(tmp_path / f'data{len(made)}')
        value = SimpleNamespace(guard=guard, project=project, root=Workspace(project).root, rid='run-challenges')
        assert guard.begin(value.root, value.rid, goal)['state'] == 'started'
        made.append(value)
        return value
    yield start
    for value in made:
        value.guard.close()


def pre(env, tool, **arguments):
    return env.guard.handle('pre_tool_use', env.root, env.rid, tool=tool, arguments=arguments)


AUTH = {'auth.py': 'def validate_login(name, password):\n    return bool(name and password)\n',
        'test_auth.py': 'import unittest\nfrom auth import validate_login\n\n\nclass T(unittest.TestCase):\n'
                        '    def test_ok(self):\n        self.assertTrue(validate_login("a", "b"))\n',
        **{f'billing_{n}.py': f'RATE_{n} = {n}\n' for n in range(7)}}


def test_run_tests_is_never_scope_challenged_while_writes_still_are(make):
    env = make(AUTH, 'Fix validate_login in auth.py.')  # AgentGuard grounds scope from the symbol
    for n in range(5):  # five edits to files the task does not reach, recorded after their approval
        content = f'RATE_{n} = {n + 1}\n'
        env.guard.stage(env.root, env.rid, f'billing_{n}.py', content)
        env.guard.handle('post_tool_use', env.root, env.rid, tool='Write',
                         arguments={'file_path': f'billing_{n}.py', 'content': content}, result='{}')
    for tool in ('RunTests', 'RunRecipe'):
        assert pre(env, tool, recipe='python-unittest')['action'] == 'allow', tool
    for tool, arguments in (('ReadGitDiff', {'file_path': 'billing_1.py'}), ('ListProjectRecipes', {}),
                            ('SearchFiles', {'query': 'RATE'}), ('ReadGitStatus', {})):
        assert pre(env, tool, **arguments)['action'] == 'allow', tool
    challenged = pre(env, 'Write', file_path='billing_6.py', content='RATE_6 = 7\n')
    assert challenged['action'] == 'challenge' and any(f['category'] == 'scope' for f in challenged['findings'])


def big_package(count=1601):
    # A manifest makes AgentGuard judge module imports at all; without one it has no opinion.
    files = {'main.py': 'import pkg\n', 'pkg/__init__.py': '', 'requirements.txt': 'requests==2.32.0\n'}
    files.update({f'pkg/mod_{n:04d}.py': f'VALUE = {n}\n' for n in range(count)})
    return files


def test_a_real_module_beyond_the_mirror_cut_is_not_challenged(make):
    env = make(big_package(), 'Use pkg.mod_1599 in main.py.')
    mirror = env.guard.mirrors[str(env.root)]
    assert mirror.truncated and not (mirror.path / 'pkg' / 'mod_1599.py').exists()
    decision = pre(env, 'Write', file_path='main.py', content='from pkg.mod_1599 import VALUE\nprint(VALUE)\n')
    assert decision['action'] == 'allow', decision
    assert decision['findings'], 'the finding is still recorded, only not raised'


def test_on_a_whole_mirror_an_invented_module_is_still_challenged(make):
    env = make(big_package(3), 'Use pkg.mod_0002 in main.py.')
    assert not env.guard.mirrors[str(env.root)].truncated
    decision = pre(env, 'Write', file_path='main.py', content='from pkg.mod_0009 import VALUE\nprint(VALUE)\n')
    assert decision['action'] == 'challenge', decision


def test_append_is_checked_by_the_evidence_engine(make):
    env = make({'util.py': 'def helper():\n    return 1\n', 'main.py': 'import util\n'}, 'Call the helper from main.py.')
    added = '\nfrom util import missing_name\nmissing_name()\n'
    decision = pre(env, 'Append', file_path='main.py', content=added)
    assert decision['action'] == 'challenge' and 'missing_name' in decision['reason'], decision
    assert pre(env, 'Append', file_path='main.py', content='\nfrom util import helper\nhelper()\n')['action'] == 'allow'


@pytest.mark.parametrize('text', ['DROP TABLE users;', 'rm -rf /', 'shutil.rmtree(".")',
                                  'os.system("rm -rf ~")', 'git push --force origin main'])
def test_typed_input_is_screened_statelessly_and_a_retry_is_refused_again(make, text):
    env = make({'a.py': 'x = 1\n'}, 'Clean up the scratch tables.')
    refusals = [env.guard.screen_input(text) for _ in range(8)]
    assert all(r and r.endswith('Nothing was typed.') for r in refusals), refusals
    assert env.guard.screen_input('SELECT count(*) FROM users;') is None
    assert env.guard.screen_input('print(sum([1, 2]))') is None


def test_a_read_only_command_is_not_sent_to_review_and_a_review_says_why(make):
    env = make({'migrations/001.sql': 'CREATE TABLE users(id INTEGER);\n'}, 'Audit the migrations.')
    command = 'rg "DROP TABLE" migrations'
    allowed = env.guard.handle('pre_tool_use', env.root, env.rid, tool='Bash', arguments={'command': command},
                               read_only=True)
    assert allowed['action'] == 'allow', allowed
    review = env.guard.handle('pre_tool_use', env.root, env.rid, tool='Bash',
                              arguments={'command': 'sqlite3 app.db "DROP TABLE users"'})
    assert review['action'] == 'request_review'
    assert GuardAdapter.review_reason(review) == 'AgentGuard asked for your review: Irreversible command: destructive SQL.'
    narrowed = env.guard.handle('pre_tool_use', env.root, env.rid, tool='Bash', arguments={'command': 'rm -rf $OUT'})
    assert narrowed['action'] == 'modify'
    refusal = GuardAdapter.refusal(narrowed)
    assert refusal.startswith('AgentGuard: nothing ran.') and 'narrowed' not in refusal
