"""Phase 46 (1.9.29, w20-git, the brief's item 1): what the model reads after the golden harness's driver approves a
reviewed Git command that then does not apply cleanly, through App, the broker and a scripted model (cards on, as a
request that keeps a branch has them in Auto).

Measured on 1.9.28 first: an approved plan that failed (Git refused it, or its repository moved after review) left the
plan's own tool message as it was ("awaiting_user_approval", "Staged, not applied"), its broker action 'failed' with no
result, and the reason only in the run's HOST INPUT as one line ("git rebase stopped and changed nothing: error: cannot
rebase: You have unstaged changes. error: Please commit or stash them." or "The repository changed after review
(bindings.dirty, bindings.status). Nothing ran; prepare the Git plan again."), with no way past it named; a stash pop
that conflicted applied with exit code 1 and no next step. The golden harness records any ProposeGitChange that did
not apply as refused with no text (normalize.py keeps no output for that tool), so these cells read as blanks there.
Now each outcome carries Git's own lines, the state it left and the reviewed way on.
"""

import json
import time

import pytest

from test_approval_handoff_22 import call, env, final, no_network  # noqa: F401 (fixtures)
from test_git_plans import MAC, git, rev
from test_git_review_1929 import auto, project_repo

pytestmark = pytest.mark.skipif(not MAC, reason='Confined Git plans are a macOS sandbox-exec product surface.')


def command(ident, *argv):
    return call('ProposeGitChange', {'operation': 'command', 'argv': json.dumps(list(argv))}, ident)


def driven(app, env, text, before_approval=None, timeout=90):  # noqa: F811
    """Approve every card as approve_all does; `before_approval` runs once, after the first card and before its
    approval (what changed the repository between review and apply)."""
    chat = app.new_chat(str(env.project))
    app.work(chat, text, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = app.runs.get(rid)
        if app.active is None and run['state'] == 'waiting_approval':
            waiting = [a for a in app.store.actions(chat['id']) if a['status'] == 'pending']
            if waiting:
                if before_approval is not None:
                    before_approval()
                    before_approval = None
                try:
                    app.approve(chat['id'], waiting[0]['id'])
                except Exception:
                    pass  # the driver decided; Pebble's own refusal is what the run reads
                continue
        if app.active is None and run['state'] in ('completed', 'failed', 'held', 'budget_exhausted'):
            return chat['id'], run
        time.sleep(.02)
    pytest.fail(f'The run did not end: {app.runs.get(rid)["state"]}')


def after_decision(env):  # noqa: F811
    """What the model reads once the decision is in: each HOST INPUT payload of its next request (the action's
    resolution), as text."""
    found = []
    for message in env.requests[-1]:
        text = message.get('content')
        if isinstance(text, str) and text.startswith('HOST INPUT'):
            found.append(json.loads(text.split('\n', 1)[1])['payload'])
    assert found, 'no resolution reached the model'
    return '\n'.join(json.dumps(payload, ensure_ascii=False) for payload in found)


@pytest.fixture
def cards(monkeypatch):
    monkeypatch.setenv('PEBBLE_AUTO_RISKY_ONLY', '0')


@pytest.mark.usefixtures('cards')
def test_an_approved_command_git_refuses_reads_gits_words_and_the_way_past_them(env, monkeypatch):  # noqa: F811
    project_repo(env.project)
    (env.project / 'app.py').write_text('print("main")\n')
    git(env.project, 'commit', '-q', '-m', 'Main change', '--', 'app.py')
    git(env.project, 'switch', '-q', 'feature')  # notes.txt's unrelated edit comes along: a rebase refuses it
    app = auto(env, monkeypatch, [command('g1', 'rebase', 'main'), final('Rebase refused.\n\nFINAL: blocked')])
    cid, run = driven(app, env, 'Rebase my feature branch onto main. Keep my notes.txt edit. End with FINAL: done.')
    seen = after_decision(env)
    assert 'stopped and changed nothing (exit code 1)' in seen and 'unstaged changes' in seen
    assert '--autostash' in seen and 'stash' in seen and 'What to do' in seen
    assert [a['status'] for a in app.store.actions(cid) if a['kind'] == 'git'] == ['failed']


@pytest.mark.usefixtures('cards')
def test_an_approved_plan_whose_repository_moved_reads_what_moved_and_the_one_step_on(env, monkeypatch):  # noqa: F811
    feature = project_repo(env.project)
    app = auto(env, monkeypatch, [command('g1', 'cherry-pick', feature), final('Not applied.\n\nFINAL: blocked')])
    cid, run = driven(app, env, 'Bring the "Add feature" commit onto main. End with FINAL: done.',
                      before_approval=lambda: (env.project / 'later.txt').write_text('written after the review\n'))
    seen = after_decision(env)
    assert 'changed after review' in seen and 'What changed: files in the working folder changed' in seen
    assert 'The same command proposed again' in seen
    assert git(env.project, 'log', '-1', '--format=%s').stdout.strip() == 'Initial import'


@pytest.mark.usefixtures('cards')
def test_an_approved_stash_pop_that_conflicts_reads_the_conflict_and_how_to_finish(env, monkeypatch):  # noqa: F811
    project_repo(env.project)
    (env.project / 'app.py').write_text('print("draft")\n')
    git(env.project, 'stash', 'push', '-q', '--', 'app.py')
    (env.project / 'app.py').write_text('print("main")\n')
    git(env.project, 'commit', '-q', '-m', 'Main change', '--', 'app.py')
    app = auto(env, monkeypatch, [command('g1', 'stash', 'pop'), final('Popped with a conflict.\n\nFINAL: blocked')])
    cid, run = driven(app, env, 'Bring my stashed draft back. End with FINAL: done.')
    seen = after_decision(env)
    assert '"exit_code": 1' in seen and 'CONFLICT' in seen and 'kept the entry in the stash' in seen
    assert [a['status'] for a in app.store.actions(cid) if a['kind'] == 'git'] == ['applied']


@pytest.mark.usefixtures('cards')
def test_two_plans_in_one_response_the_second_reads_why_it_did_not_run(env, monkeypatch):  # noqa: F811
    project_repo(env.project)
    both = {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'g1', 'type': 'function', 'function': {'name': 'ProposeGitChange', 'arguments': json.dumps(
            {'operation': 'command', 'argv': json.dumps(['stash', 'push', '-u'])})}},
        {'id': 'g2', 'type': 'function', 'function': {'name': 'ProposeGitChange', 'arguments': json.dumps(
            {'operation': 'command', 'argv': json.dumps(['switch', '-c', 'hotfix', 'main'])})}}]}
    app = auto(env, monkeypatch, [both, final('Only the stash ran.\n\nFINAL: blocked')])
    cid, run = driven(app, env, 'Set my work aside and start a hotfix branch from main. End with FINAL: done.')
    seen = after_decision(env)
    assert 'changed after review' in seen and 'What changed:' in seen and 'The same command proposed again' in seen
    assert [a['status'] for a in app.store.actions(cid) if a['kind'] == 'git'] == ['applied', 'failed']
    assert git(env.project, 'stash', 'list').stdout.count('stash@') == 1 and rev(env.project, 'HEAD') == rev(
        env.project, 'main')
