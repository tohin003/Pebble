"""Phase 46 (w1-git): the private git_workflow dev cases d-gw-101..123, offline. Each case's starting state is built by
the split's own materialize.py (a workspace with unrelated work in progress and a local bare origin), the reviewed Git
calls a capable model would make run through GitPlans and GitWorkflows exactly as ProposeGitChange and RunCommand run
them (the person's approval is the test's), and the case's own checker grades the result. The four operations that are
the person's decision (D159) are refused while git_decisions is off and pass with the shipped default, on since D174.

Needs the private split: PEBBLE_GOLDEN_EXTRA names its dev folder (never a default: the published tree holds no path
of the development Mac); skipped without it.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import settings_file
from pebble.git_commands import read
from pebble.git_plans import GitPlans
from pebble.git_workflows import GitWorkflows
from pebble.policy import PolicyError
from pebble.store import Store
from test_git_plans import MAC

SPLIT = Path(os.environ['PEBBLE_GOLDEN_EXTRA']) if os.environ.get('PEBBLE_GOLDEN_EXTRA') else None
pytestmark = pytest.mark.skipif(not MAC or SPLIT is None or not (SPLIT / 'checkers' / 'd-gw-101.py').is_file(),
                                reason='needs macOS sandbox-exec and the private golden-extra split')
DECIDED = {'d-gw-104': 'force_push', 'd-gw-112': 'delete_remote_branch', 'd-gw-117': 'repository_identity',
           'd-gw-118': 'clean'}


@pytest.fixture
def cells(tmp_path):
    made = []

    def make(case):
        cell = tmp_path / case
        subprocess.run([sys.executable, '-I', str(SPLIT / 'checkers' / 'materialize.py'), '--case', case, '--cell',
                        str(cell)], check=True, capture_output=True, timeout=120)
        home = tmp_path / ('data-' + case)
        app = SimpleNamespace(home=home, store=Store(home))
        app.store.set_setting('git_identity', {'name': 'Pebble Person', 'email': 'person@example.invalid'})
        repo = (cell / 'workspace').resolve()
        workflows = GitWorkflows(app)
        made.append((workflows, app))
        return SimpleNamespace(app=app, cell=cell, repo=repo, chat=app.store.new_chat(str(repo)), git=workflows,
                               plans=GitPlans(app, workflows), turns=[])
    yield make
    for workflows, app in made:
        workflows.close()
        app.store.close()


def plan(host, kind, args):
    if kind == 'command':
        return host.plans.prepare(host.chat, 'command', {'argv': json.dumps(args)})
    return host.plans.prepare(host.chat, kind, dict(args))


def run(host, *steps):
    for kind, args in steps:
        result = host.plans.execute(host.chat, plan(host, kind, args))
        assert result['state'] == 'applied', (kind, args, result)
    return result


def raw(host, text):
    argv = read(text)
    assert argv is not None, text
    return host.git.raw(host.chat, argv)['output']


def turn(host, reply):
    """The harness's record of a finished turn: the reply, and the working folder without .git (README, Cell record)."""
    index = len(host.turns)
    shutil.copytree(host.repo, host.cell / 'snapshots' / f'turn-{index}', ignore=shutil.ignore_patterns('.git'))
    host.turns.append({'step': index, 'session': 0, 'kind': 'prompt' if index == 0 else 'followup', 'human': '(case)',
                       'reply': reply, 'native_question': None, 'changed_paths': [], 'ended': 'completed',
                       'wall_seconds': 1.0})


def grade(host, case):
    (host.cell / 'transcript.json').write_text(json.dumps({'ended': 'completed', 'turns': host.turns}))
    (host.cell / 'actions.jsonl').write_text('')
    proc = subprocess.run([sys.executable, '-I', str(SPLIT / 'checkers' / (case + '.py')), '--cell', str(host.cell)],
                          capture_output=True, text=True, timeout=300)
    verdict = json.loads(proc.stdout.strip().split('\n')[-1])
    return verdict['outcome'], [check['name'] for check in verdict.get('checks', []) if not check['ok']]


DONE = 'Done as asked.\n\nFINAL: done'
STEPS = {
    'd-gw-101': [('commit', {'paths': ['README.md', 'app/report.py'], 'message': 'Polish the report'})],
    'd-gw-102': [('command', ['commit', '--amend', '-m', 'Show item count in report'])],
    'd-gw-103': [('command', ['reset', '--soft', 'HEAD~2']), ('command', ['commit', '-m', 'Improve the report summary'])],
    'd-gw-104': [('command', ['rebase', '--autostash', 'main']), ('command', ['push', '--force-with-lease', 'origin', 'feature'])],
    'd-gw-105': [('command', ['merge', '-X', 'ours', '-m', 'Merge feature into main', 'feature'])],
    'd-gw-106': [('command', ['cherry-pick', 'main~2', 'main'])],
    'd-gw-107': [('command', ['revert', 'HEAD~1'])],
    'd-gw-109': [('command', ['tag', 'v1.1.0', 'HEAD~1']), ('command', ['push', 'origin', 'v1.1.0'])],
    'd-gw-110': [('commit', {'paths': ['drafts/idea.md'], 'message': 'Add monthly view draft', 'branch': 'feature/monthly'}),
                 ('command', ['push', '-u', 'origin', 'feature/monthly'])],
    'd-gw-111': [('command', ['branch', '-d', 'feature/csv'])],
    'd-gw-112': [('command', ['branch', '-d', 'feature/csv']), ('command', ['push', 'origin', '--delete', 'feature/csv'])],
    'd-gw-113': [('command', ['restore', '--source', 'HEAD~1', '--staged', '--worktree', '--', 'app/report.py']),
                 ('command', ['commit', '-m', 'Restore the item-count report'])],
    'd-gw-115': [('command', ['mv', 'app/report.py', 'app/summary.py']), ('command', ['commit', '-m', 'Rename report module'])],
    'd-gw-117': [('command', ['config', 'user.name', 'Ada Lovelace']), ('command', ['config', 'user.email', 'ada@example.com']),
                 ('commit', {'paths': ['app/report.py'], 'message': 'Count items in the report'})],
    'd-gw-118': [('command', ['clean', '-f', '-d', '-X'])],
    'd-gw-120': [('command', ['fetch', 'origin']),
                 ('command', ['merge', '--no-ff', '-m', 'Merge colleague CSV export', 'origin/colleague/csv'])],
    'd-gw-121': [('command', ['reset', '--soft', 'HEAD~1'])],
}


@pytest.mark.parametrize('case', sorted(STEPS))
def test_one_turn_cases_pass_through_reviewed_git(cells, case):
    host, steps = cells(case), list(STEPS[case])
    if case in DECIDED:  # D159: refused while off (the steps before it ran); D174: on by default, so the rest runs
        host.app.store.set_setting('git_decisions', {**settings_file.default('git_decisions'), DECIDED[case]: False})
        done = 0
        with pytest.raises(PolicyError, match="person's decision"):
            for step in steps:
                run(host, step)
                done += 1
        host.app.store.set_setting('git_decisions', settings_file.default('git_decisions'))  # the shipped default
        steps = steps[done:]
    run(host, *steps)
    turn(host, DONE)
    assert grade(host, case) == ('passed', [])


def test_stash_with_untracked_files_then_pop_in_a_follow_up(cells):
    host = cells('d-gw-108')
    run(host, ('command', ['stash', 'push', '-u', '-m', 'monthly view draft']))
    turn(host, DONE)
    run(host, ('command', ['stash', 'pop']))
    turn(host, DONE)
    assert grade(host, 'd-gw-108') == ('passed', [])


def test_stage_then_unstage_in_a_follow_up(cells):
    host = cells('d-gw-114')
    run(host, ('stage', {'paths': ['app/report.py', 'notes.txt']}))
    turn(host, DONE)
    run(host, ('unstage', {'paths': ['notes.txt']}))
    turn(host, DONE)
    assert grade(host, 'd-gw-114') == ('passed', [])


def test_stop_tracking_a_file_and_ignore_it(cells):
    host = cells('d-gw-116')
    run(host, ('command', ['rm', '--cached', '--', 'config/local.ini']))
    (host.repo / '.gitignore').write_text('config/local.ini\n')  # the model's Write
    run(host, ('command', ['add', '--', '.gitignore']), ('command', ['commit', '-m', 'Stop tracking local settings']))
    turn(host, DONE)
    assert grade(host, 'd-gw-116') == ('passed', [])


def test_a_bisect_finds_the_commit_that_broke_the_test_and_leaves_the_repository_as_found(cells, monkeypatch):
    monkeypatch.setenv('PEBBLE_RAW_GIT_READS', '1')  # on by default (D173); pinned on here
    host = cells('d-gw-119')
    assert 'Speed up total' in raw(host, 'git log --oneline')
    result = run(host, ('command', ['bisect', 'start', 'main', 'main~5']))
    for _ in range(6):
        test = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'tests.test_money.MoneyTest.test_single'],
                              cwd=host.repo, capture_output=True, env={'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'})
        result = run(host, ('command', ['bisect', 'bad' if test.returncode else 'good']))
        found = re.search(r'([0-9a-f]{40}) is the first bad commit', result['result']['output'])
        if found:
            break
    subject = raw(host, f'git log -1 --format=%s {found.group(1)}').strip()
    run(host, ('command', ['bisect', 'reset']))
    turn(host, f'The first failing commit is "{subject}".\n\nFINAL: {subject}')
    assert grade(host, 'd-gw-119') == ('passed', [])


def test_read_questions_are_answered_from_read_only_git_in_the_terminal(cells, monkeypatch):
    monkeypatch.setenv('PEBBLE_RAW_GIT_READS', '1')  # on by default (D173); pinned on here
    host = cells('d-gw-122')
    commit = next(line.split(' ', 1)[0] for line in raw(host, 'git log --all --format="%H %s"').splitlines()
                  if line.endswith('Add CSV export'))
    branches = [line.strip('* ').strip() for line in raw(host, f'git branch --contains {commit}').splitlines() if line.strip()]
    turn(host, f'The commit is only on {branches[0]}.\n\nFINAL: {branches[0]}')
    assert branches == ['feature/csv'] and grade(host, 'd-gw-122') == ('passed', [])
    host = cells('d-gw-123')
    changed = [line for line in raw(host, 'git diff --name-only v1.1 v1.2').splitlines() if line]
    turn(host, 'Two files changed.\n\nFINAL: ' + ', '.join(changed))
    assert grade(host, 'd-gw-123') == ('passed', [])
