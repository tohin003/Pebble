"""Phase 46 (1.9.29, w20-git round 2): golden held-out git_workflow, one task's three phrasings on both models ended
honestly after one raw `git -C <folder>` was refused (D212's counts: in each failing cell the folder is a linked
worktree of the project's own repository under the project's parent folder, deeper than beside it; on Opus the command
changes the repository, on sol it reads).

`git -C <folder>` (and `cd <folder> && git ...`) in RunCommand, and argv ["-C", "<folder>", ...] in ProposeGitChange,
run in that folder when GitWorkflows.worktree_view proves it is the project or one of its own linked worktrees: a
two-way link with the project's .git, inside the project or at most three folders below its parent folder, no symbolic
link or other repository on the way, nothing protected or private. Reads run as the raw reads run; changes are the
reviewed command bound to that worktree (card or check, snapshots, Undo, drift). Any other folder is refused with the
rule. PEBBLE_1929_RAW_GIT_REVIEW=0 keeps 1.9.28's refusal for RunCommand.
"""

import json
import os
import re

import pytest

from pebble.git_commands import located, read, review, typed
from pebble.git_plans import failure_hint
from pebble.git_workflows import WORKTREE_RULE
from pebble.policy import PolicyError
from test_approval_handoff_22 import call, env, final, no_network  # noqa: F401 (fixtures)
from test_git_plans import MAC, apply, git, host, macos, rev  # noqa: F401 (fixture)
from test_git_private_cases_46 import cells  # noqa: F401 (fixture)
from test_git_review_1929 import auto, project_repo, ran, tool_result

RULE = "-C works for this project's own folder and its linked worktrees"


def hotfix(host):  # noqa: F811
    """A linked worktree of the project's repository in ../worktrees/hotfix on a new branch hotfix, as git worktree add
    makes it, and a commit on main the hotfix branch does not have."""
    git(host.repo, 'worktree', 'add', '-q', '-b', 'hotfix', '../worktrees/hotfix', 'main')
    (host.repo / 'app.py').write_text('print("fixed")\n')
    git(host.repo, 'commit', '-q', '-am', 'Fix the greeting')
    return host.tmp / 'worktrees' / 'hotfix', rev(host.repo)


def in_worktree(host, folder, *argv):  # noqa: F811
    return apply(host, 'command', argv=json.dumps(['-C', folder, *argv]))


# -- what a model types ------------------------------------------------------------------------------------------------

def test_git_dash_c_and_cd_name_the_folder_in_front_and_the_switch_keeps_the_refusal(tmp_path, monkeypatch):
    root = tmp_path.resolve() / 'project'
    root.mkdir()
    assert review('git -C ../worktrees/hotfix commit -m "Fix it"', root=root) == [
        '-C', '../worktrees/hotfix', 'commit', '-m', 'Fix it']
    assert review('cd ../worktrees/hotfix && git cherry-pick abc1234', root=root) == [
        '-C', '../worktrees/hotfix', 'cherry-pick', 'abc1234']
    assert read('git -C ../worktrees/hotfix status --short', root=root) == ['-C', '../worktrees/hotfix', 'status', '--short']
    assert read('cd ../worktrees/hotfix && git log --oneline -3', root=root) == [
        '-C', '../worktrees/hotfix', 'log', '--oneline', '-3']
    assert read(f'cd {root} && git status', root=root) == ['status'] and read('git -C . log -1', root=root) == ['log', '-1']
    for text in ('git -C a -C b status', 'cd a && git -C b status', 'cd a && git status && rm x', 'cd a; git status',
                 'cd a && ls', 'git -C ../wt -c core.pager=less log', 'git -C -P log', f'cd ../wt && git -C {root} log',
                 f'git -C ../wt -C {root} log'):  # a -C after another is read from there by Git: refused
        assert read(text, root=root) is None and review(text, root=root) is None
    with pytest.raises(PolicyError, match='Git paths must stay'):
        read('git -C ../wt diff -- ../other.txt', root=root)
    monkeypatch.setenv('PEBBLE_1929_RAW_GIT_REVIEW', '0')
    assert read('git -C ../worktrees/hotfix status', root=root) is None
    assert review('git -C ../worktrees/hotfix commit -m x', root=root) is None
    assert typed('cd ../wt && git status', root=root) is None and read('git -C . status', root=root) == ['status']


def test_proposegitchange_argv_takes_the_folder_off_its_front():
    assert located('["-C", "../wt", "commit", "-m", "x"]') == ('../wt', '["commit", "-m", "x"]')
    assert located(['git', '-C', '../wt', 'add', 'a.py']) == ('../wt', '["git", "add", "a.py"]')
    assert located('[["-C", "../wt", "add", "a.py"], ["-C", "../wt", "commit", "-m", "x"]]') == (
        '../wt', '[["add", "a.py"], ["commit", "-m", "x"]]')
    assert located('["commit", "-m", "x"]') == (None, '["commit", "-m", "x"]') and located('not json') == (None, 'not json')
    with pytest.raises(PolicyError, match='the same "-C"'):
        located('[["-C", "../wt", "add", "a.py"], ["commit", "-m", "x"]]')
    assert 'linked worktree /tmp/p/worktrees/hotfix' in failure_hint(
        ['switch', 'hotfix'], "fatal: 'hotfix' is already used by worktree at '/tmp/p/worktrees/hotfix'")
    assert '["-C", "/tmp/p/worktrees/hotfix", ...]' in failure_hint(
        ['switch', 'hotfix'], "fatal: 'hotfix' is already checked out at '/tmp/p/worktrees/hotfix'")


# -- reviewed commands in the worktree ---------------------------------------------------------------------------------

@macos
def test_a_reviewed_command_runs_in_the_projects_linked_worktree_bound_to_it_and_undo_restores_it(host):  # noqa: F811
    folder, fix = hotfix(host)
    before = rev(host.repo, 'hotfix')
    (host.repo / 'notes.txt').write_text('one\ntwo\nthree\nfour (draft)\n')  # the person's work in the project
    plan, picked = in_worktree(host, '../worktrees/hotfix', 'cherry-pick', fix)
    assert plan['worktree'] == plan['root'] == str(folder) and plan['argv'] == ['cherry-pick', fix]
    assert plan['summary'].startswith(f'In the linked worktree {folder} (on branch hotfix): Apply {fix}')
    assert any('linked worktree' in effect for effect in plan['effects']) and plan['destructive'] is False
    assert picked['state'] == 'applied' and picked['result']['exit_code'] == 0
    assert rev(host.repo, 'hotfix') != before and (folder / 'app.py').read_text() == 'print("fixed")\n'
    assert rev(host.repo, 'main') == fix and git(host.repo, 'symbolic-ref', 'HEAD').stdout.strip() == 'refs/heads/main'
    assert (host.repo / 'notes.txt').read_text().endswith('four (draft)\n')  # the project's own work stays
    assert host.plans.readback(host.chat, picked['receipt_id'])['matches_receipt'] is True
    _, undone = apply(host, 'undo', receipt_id=picked['receipt_id'])
    assert undone['state'] == 'applied' and rev(host.repo, 'hotfix') == before
    assert (folder / 'app.py').read_text() == 'print("base")\n' and rev(host.repo, 'main') == fix
    # a commit of a change made in the worktree, by its absolute path, and its state read back
    (folder / 'README.md').write_text('# Fixture\n\nHotfix notes.\n')
    _, committed = in_worktree(host, str(folder), 'commit', '-am', 'Hotfix notes')
    assert committed['state'] == 'applied' and git(folder, 'log', '-1', '--format=%s').stdout.strip() == 'Hotfix notes'
    assert git(folder, 'status', '--porcelain').stdout == '' and rev(host.repo, 'main') == fix
    shown = host.git.raw(host.chat, ['-C', '../worktrees/hotfix', 'log', '--oneline', '-2'])
    assert shown['root'] == str(folder) and shown['branch'] == 'hotfix' and 'Hotfix notes' in shown['output']


@macos
def test_a_sequence_drift_and_what_runs_from_the_project_folder(host):  # noqa: F811
    folder, fix = hotfix(host)
    (folder / 'notes.txt').write_text('one\ntwo\nthree\nhotfix\n')
    plan, done = apply(host, 'command', argv=json.dumps([['-C', str(folder), 'add', '--', 'notes.txt'],
                                                         ['-C', str(folder), 'commit', '-m', 'Hotfix notes']]))
    assert plan['summary'].startswith('In the linked worktree') and done['state'] == 'applied'
    assert git(folder, 'log', '-1', '--format=%s').stdout.strip() == 'Hotfix notes'
    plan = host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['-C', '../worktrees/hotfix', 'merge', 'main'])})
    (folder / 'scratch.md').write_text('written after the review\n')
    with pytest.raises(PolicyError, match='changed after review'):
        host.plans.execute(host.chat, plan)
    assert rev(host.repo, 'hotfix') != fix
    for argv, words in ((['push', 'origin', 'hotfix'], 'runs from the project folder'),
                        (['fetch', 'origin'], 'fetch there, then merge'),
                        (['worktree', 'add', '../x', '-b', 'x', 'main'], 'run it from the project folder'),
                        (['config', 'user.name', 'X'], 'run it from the project folder')):
        with pytest.raises(PolicyError, match=words):
            host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['-C', str(folder), *argv])})
    _, merged = in_worktree(host, '../worktrees/hotfix', 'merge', 'main')  # proposed again, as the drift words say
    assert merged['state'] == 'applied' and (folder / 'app.py').read_text() == 'print("fixed")\n'


@macos
def test_only_the_projects_own_linked_worktrees_are_taken(host):  # noqa: F811
    folder, _ = hotfix(host)
    tmp = host.tmp
    (tmp / 'plain').mkdir()
    (tmp / 'other').mkdir()
    git(tmp / 'other', 'init', '-q')
    git(tmp / 'other', 'commit', '-q', '--allow-empty', '-m', 'Other')
    git(tmp / 'other', 'worktree', 'add', '-q', '-b', 'side', '../worktrees/other-side')
    git(host.repo, 'worktree', 'add', '-q', '-b', 'deep', '../a/b/c/deep', 'main')  # four folders below the parent
    git(host.repo, 'worktree', 'add', '-q', '-b', 'kept', '../worktrees/secrets', 'main')  # a private name
    (tmp / 'fake').mkdir()  # a crafted one-way link: it names the hotfix worktree's record, which names hotfix
    (tmp / 'fake' / '.git').write_text(f'gitdir: {host.repo}/.git/worktrees/hotfix\n')
    os.symlink(tmp / 'worktrees', tmp / 'links')
    for given, words in (('../plain', 'not a linked worktree of this project'),
                         ('../worktrees/other-side', "not a linked worktree of this project's repository"),
                         ('../fake', 'a one-way link'),
                         ('../a/b/c/deep', 'outside the folders Git runs in here'),
                         ('../links/hotfix', 'a symbolic link or a file is on the way'),
                         ('../worktrees/secrets', 'private or protected'),
                         ('../other', 'not a linked worktree of this project'),
                         ('~/hotfix', 'plain path'), ('/', 'outside the folders'), ('..', 'outside the folders'),
                         (str(host.repo / '.git' / 'worktrees' / 'hotfix'), 'outside the folders')):
        with pytest.raises(PolicyError, match=re.escape(words)) as refused:
            host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['-C', given, 'status'])})
        assert RULE in str(refused.value) or words == 'runs from the project folder'
        with pytest.raises(PolicyError, match=re.escape(words)):
            host.git.raw(host.chat, ['-C', given, 'status'])
    # a record whose commondir points at another repository's .git
    (host.repo / '.git' / 'worktrees' / 'hotfix' / 'commondir').write_text(f'{tmp / "other" / ".git"}\n')
    with pytest.raises(PolicyError, match='points elsewhere'):
        host.git.raw(host.chat, ['-C', str(folder), 'status'])
    (host.repo / '.git' / 'worktrees' / 'hotfix' / 'commondir').write_text('../..\n')
    assert host.git.raw(host.chat, ['-C', str(folder), 'status', '--short'])['exit_code'] == 0
    assert WORKTREE_RULE.startswith(RULE)


@macos
def test_a_worktree_on_the_shells_protected_list_is_refused(host, monkeypatch):  # noqa: F811
    monkeypatch.setenv('HOME', str(host.tmp))  # the project's parent folder is the home folder here
    git(host.repo, 'worktree', 'add', '-q', '-b', 'kept', '../Library/Keychains/kept', 'main')
    with pytest.raises(PolicyError, match='private or protected'):
        host.git.raw(host.chat, ['-C', '../Library/Keychains/kept', 'log', '-1'])
    with pytest.raises(PolicyError, match='private or protected'):
        host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['-C', '../Library/Keychains/kept', 'tag', 'v9'])})


# -- the private cases d-gw-206..208, replayed as test_git_review_1929 replays d-gw-200..205 ---------------------------

REPLAYS = {  # what a capable model types in RunCommand; its reply quotes what the reads printed
    'd-gw-206': ['git -C ../worktrees/hotfix-average commit -am "Handle an empty list in average"',
                 'git -C ../worktrees/hotfix-average rev-parse --short HEAD',
                 'git -C ../worktrees/hotfix-average status --short'],
    'd-gw-207': ['git -C ../worktrees/release-1x cherry-pick {main}', 'git -C ../worktrees/release-1x log --format=%s'],
    'd-gw-208': ['git worktree add -b revert/item-count ../worktrees/revert-item-count main',
                 'cd ../worktrees/revert-item-count && git revert --no-edit HEAD',
                 'git -C ../worktrees/revert-item-count log -1 --format=%s'],
}


@pytest.mark.parametrize('case', sorted(REPLAYS))
def test_the_worktree_cases_pass_through_the_reviewed_route(case, cells):  # noqa: F811
    from test_git_private_cases_46 import SPLIT, grade, turn
    if not MAC or SPLIT is None or not (SPLIT / 'checkers' / (case + '.py')).is_file():
        pytest.skip('needs macOS sandbox-exec and the private golden-extra split (PEBBLE_GOLDEN_EXTRA)')
    host = cells(case)
    said = []
    for text in REPLAYS[case]:
        text = text.format(main=rev(host.repo, 'main'))
        reads = read(text, root=host.repo)  # as App tries it: a read first, then the reviewed command
        if reads is not None:
            said.append(host.git.raw(host.chat, reads)['output'])
            continue
        plan = host.plans.prepare(host.chat, 'command', {'argv': json.dumps(review(text, root=host.repo))})
        assert host.plans.execute(host.chat, plan)['state'] == 'applied', text
    turn(host, 'Done. What Git printed in the worktree:\n' + '\n'.join(said) + '\n\nFINAL: done')
    assert grade(host, case) == ('passed', [])


# -- through RunCommand and ProposeGitChange, as the golden harness drives them ---------------------------------------

def project_worktree(env):  # noqa: F811
    """project_repo's repository with a linked worktree ../worktrees/release on a branch release made at main."""
    feature = project_repo(env.project)
    git(env.project, 'worktree', 'add', '-q', '-b', 'release', '../worktrees/release', 'main')
    return feature, env.project.parent.resolve() / 'worktrees' / 'release'


@macos
def test_git_dash_c_in_run_command_reads_and_changes_the_worktree_under_review(env, monkeypatch):  # noqa: F811
    feature, folder = project_worktree(env)
    app = auto(env, monkeypatch, [
        call('RunCommand', {'command': f'git -C ../worktrees/release cherry-pick {feature}'}, 'c1'),
        call('RunCommand', {'command': 'cd ../worktrees/release && git log --oneline -1'}, 'c2'),
        call('RunCommand', {'command': 'git -C ../elsewhere status'}, 'c3'),
        final('Picked the feature commit onto release in its worktree.\n\nFINAL: done')])
    checked, effect_paths = [], app.agents.assert_effect_paths
    monkeypatch.setattr(app.agents, 'assert_effect_paths', lambda root, path=None: (checked.append(root),
                                                                                    effect_paths(root, path))[1])
    cid, run = ran(app, env, 'In the release worktree (../worktrees/release), bring the "Add feature" commit onto the '
                             'release branch and show its last commit. End your reply with FINAL: done.')
    assert checked == [str(folder), str(env.project.resolve())]  # the broker's checks: the worktree and its project
    assert run['state'] == 'completed', run['error']
    assert git(folder, 'log', '-1', '--format=%s').stdout.strip() == 'Add feature' and (folder / 'feature.txt').exists()
    assert git(env.project, 'log', '-1', '--format=%s', 'main').stdout.strip() == 'Initial import'
    assert (env.project / 'notes.txt').read_text() == 'one\ntwo (draft)\n'
    picked = tool_result(env.requests[1], 'c1')
    assert picked['status'] == 'applied' and picked['exit_code'] == 0 and picked['reviewed_as']['tool'] == 'ProposeGitChange'
    assert json.loads(picked['reviewed_as']['argv'])[:2] == ['-C', '../worktrees/release']
    shown = tool_result(env.requests[2], 'c2')
    assert shown['exit_code'] == 0 and 'Add feature' in shown['output'] and shown['root'] == str(folder)
    assert RULE in tool_result(env.requests[3], 'c3')['error']
    [action] = [a for a in app.store.actions(cid) if a['kind'] == 'git']
    assert action['status'] == 'applied' and action['payload']['worktree'] == str(folder)


@macos
def test_a_worktree_plan_never_explains_a_project_file_that_changed(env, monkeypatch):  # noqa: F811
    from pebble.policy import Workspace
    _, folder = project_worktree(env)
    git(env.project, 'switch', '-q', '-c', 'fix', 'main')
    (env.project / 'app.py').write_text('print("fixed")\n')
    git(env.project, 'commit', '-q', '-m', 'Fix the greeting', '--', 'app.py')
    fix = rev(env.project)
    git(env.project, 'switch', '-q', 'main')
    app = auto(env, monkeypatch, [call('Read', {'file_path': 'app.py'}, 'c1'),
                                  call('RunCommand', {'command': f'git -C ../worktrees/release cherry-pick {fix}'}, 'c2'),
                                  final('Picked the fix onto release.\n\nFINAL: done')])
    cid, run = ran(app, env, 'Read app.py, then bring the "Fix the greeting" commit onto release in its worktree '
                             '(../worktrees/release). End your reply with FINAL: done.')
    assert run['state'] == 'completed', run['error']
    assert (folder / 'app.py').read_text() == 'print("fixed")\n' and [
        a['payload'].get('worktree') for a in app.store.actions(cid) if a['kind'] == 'git'] == [str(folder)]
    (env.project / 'app.py').write_text('print("fixed")\n')  # the project's file now holds the worktree's new bytes
    rid = app.runs.list(cid)[0]['id']
    assert app.git_explained(rid, cid, Workspace(str(env.project)), [('app.py', 'changed')]) == []


@macos
def test_proposegitchange_takes_the_worktree_and_the_switch_keeps_the_terminal_refusal(env, monkeypatch):  # noqa: F811
    feature, folder = project_worktree(env)
    (folder / 'notes.txt').write_text('one\nrelease note\n')  # made in the worktree beforehand
    app = auto(env, monkeypatch, [
        call('ProposeGitChange', {'operation': 'command',
                                  'argv': json.dumps(['-C', str(folder), 'commit', '-am', 'Release note'])}, 'c1'),
        call('Write', {'file_path': str(folder / 'later.txt'), 'content': 'x\n'}, 'c2'),
        final('Committed the release note in the release worktree.\n\nFINAL: done')])
    cid, run = ran(app, env, 'Commit the release note in the release worktree. End your reply with FINAL: done.')
    assert run['state'] == 'completed', run['error']
    assert git(folder, 'log', '-1', '--format=%s').stdout.strip() == 'Release note'
    assert tool_result(env.requests[1], 'c1')['status'] == 'applied'
    assert 'error' in tool_result(env.requests[2], 'c2') and not (folder / 'later.txt').exists()  # file tools: the project
    monkeypatch.setenv('PEBBLE_1929_RAW_GIT_REVIEW', '0')
    env.requests.clear()
    env.script = [call('RunCommand', {'command': f'git -C ../worktrees/release cherry-pick {feature}'}, 'c1'),
                  final('Nothing was changed.\n\nFINAL: blocked')]
    ran(app, env, 'Bring the "Add feature" commit onto release in its worktree. End your reply with FINAL: done.')
    assert 'Raw Git commands are outside the generic terminal broker' in tool_result(env.requests[1], 'c1')['error']
    assert git(folder, 'log', '-1', '--format=%s').stdout.strip() == 'Release note'


def test_a_worktree_is_added_locked_with_its_reason_in_one_reviewed_command(host):
    # the lead, 1.9.29 (final check d-gw-203 phrasing 1 stopped on "--reason" in worktree add, which Git allows with --lock)
    git(host.repo, 'branch', 'release/1.x')
    plan, done = apply(host, 'command', argv=json.dumps(['worktree', 'add', '--lock', '--reason', 'kept for 1.x support',
                                                        '../release-1x', 'release/1.x']))
    listed = git(host.repo, 'worktree', 'list', '--porcelain')
    text = listed.stdout if hasattr(listed, 'stdout') else str(listed)
    text = text.decode() if isinstance(text, bytes) else text
    assert 'release-1x' in text and 'locked kept for 1.x support' in text, text
    with pytest.raises(PolicyError, match='only with --lock'):
        review('git worktree add --reason why ../other release/1.x', root=host.repo)
