"""Phase 46 (1.9.29, w20-git): golden held-out git_workflow, one task's three phrasings ended honestly on both models
(D212's counts: per cell a changing Git command typed in RunCommand refused by the terminal, two reviewed commands that
never applied, then an honest stop; Claude Code and Codex finish them in their terminals).

1. A reviewed command that Git refuses, or that stops part-way, says what Git said (its lines, credentials hidden), the
   state it left and the reviewed way on: --autostash or one stash-run-pop sequence for work in the way, how to finish a
   stash pop that conflicts, what a drifted plan's repository did. The typed operations and a stopped sequence of
   several commits name the reviewed --continue instead of "outside Pebble".
2. A changing Git command typed in RunCommand that ProposeGitChange's "command" operation takes as typed runs as that
   operation (the same plan, card or check, snapshots and Undo); one it does not take keeps the terminal's refusal with
   the operation's reason added. PEBBLE_1929_RAW_GIT_REVIEW=0 keeps 1.9.28's refusal.
3. Everyday changing forms of subcommands that were only reads: worktree prune, lock and unlock, a worktree deeper beside
   the project with an Undo that removes it, remote prune (and fetch --prune, which pruned nothing), stash save,
   branch -f, merge and pull --autostash; raw reads through -C <project>, -P and cat-file -p.
"""

import json
import re
import shutil

import pytest

from pebble.git_commands import content, parse, read, review, typed
from pebble.git_plans import drift_words, failure_hint, git_output, moved_hint
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError
from test_approval_handoff_22 import call, env, final, no_network  # noqa: F401 (fixtures)
from test_git_commands_46 import command, second_commit, wip
from test_git_plans import MAC, apply, bare, git, host, macos, rev  # noqa: F401 (fixture)
from test_git_private_cases_46 import cells  # noqa: F401 (fixture)
from test_git_requests_46 import finished


def prepare(host, *argv):  # noqa: F811
    return host.plans.prepare(host.chat, 'command', {'argv': json.dumps(list(argv))})


# -- 2: what a model types in the terminal ----------------------------------------------------------------------------

@pytest.mark.parametrize('text, argv', [
    ('git cherry-pick abc1234', ['cherry-pick', 'abc1234']),
    ('/usr/bin/git revert --no-edit abc1234', ['revert', '--no-edit', 'abc1234']),
    ('GIT_EDITOR=true git rebase --continue', ['rebase', '--continue']),
    ('GIT_EDITOR=: GIT_SEQUENCE_EDITOR=: git rebase --continue', ['rebase', '--continue']),
    ('git -c core.editor=true commit --amend --no-edit', ['commit', '--amend', '--no-edit']),
    ('git --no-pager stash push -u', ['stash', 'push', '-u']),
    ('git -P stash pop', ['stash', 'pop']),
    ('git stash', ['stash']),
    ('git worktree add ../hotfix -b hotfix main', ['worktree', 'add', '../hotfix', '-b', 'hotfix', 'main']),
])
def test_a_changing_git_command_the_reviewed_operation_takes_is_taken_as_typed(text, argv):
    assert review(text) == argv


@pytest.mark.parametrize('text', [
    'git status | cat', 'git commit -m x && git push', 'FOO=1 git commit -m x', 'GIT_EDITOR=vim git commit -m x',
    # Phase 46 (1.9.29, round 2): 'git -C .. commit -m x' left this list: -C naming another folder is kept for
    # GitWorkflows.worktree_view, which refuses ".." with the rule (test_git_worktrees_1929)
    'git -c core.hooksPath=/tmp/x commit -m x', 'git -c core.pager=less log', 'git -C .. -C ../x commit -m x',
    'git --git-dir=.git commit -m x', 'git commit -m "$(date)"', 'git', 'gitk', 'git commit --help',
    'sh -c "git commit -m x"'])
def test_anything_else_keeps_the_terminal_refusal(text, tmp_path):
    assert review(text, root=tmp_path) is None


def test_one_the_reviewed_operation_refuses_says_why_and_the_switch_turns_routing_off(monkeypatch):
    for text, words in (('git stash clear', 'stash clear is not available'),
                        ('git notes add -m x', 'outside reviewed Git commands'),
                        ('git rebase -i main', 'No editor or todo list opens'),
                        ('git worktree move ../a ../b', '["worktree", "prune"]')):
        with pytest.raises(PolicyError, match=re.escape(words)):
            review(text)
    monkeypatch.setenv('PEBBLE_1929_RAW_GIT_REVIEW', '0')
    assert review('git cherry-pick abc1234') is None and typed('git cherry-pick abc1234') == ['cherry-pick', 'abc1234']


def test_reads_drop_the_words_that_change_nothing_here(tmp_path, monkeypatch):
    monkeypatch.setenv('PEBBLE_RAW_GIT_READS', '1')
    root = tmp_path.resolve()
    assert read('git -P log --oneline -n 3') == ['log', '--oneline', '-n', '3']
    assert read('GIT_PAGER=cat git log -1') == ['log', '-1']
    assert read(f'git -C {root} status --short', root=root) == ['status', '--short']
    assert read('git -C . status', root=root) == ['status']
    # Phase 46 (1.9.29, round 2): -C naming another folder stays in front for GitWorkflows.worktree_view, which runs it
    # only in the project's own linked worktrees and refuses ".." with the rule; without a project it stays refused
    assert read('git -C .. status', root=root) == ['-C', '..', 'status'] and read(f'git -C {root} status') is None
    assert read('git cat-file -p HEAD') == ['cat-file', '-p', 'HEAD'] and content(['cat-file', '-p', 'HEAD'])
    assert read('git cat-file commit HEAD~1') == ['cat-file', 'commit', 'HEAD~1'] and not content(['cat-file', '-t', 'X'])
    with pytest.raises(PolicyError, match='Private paths'):
        read('git cat-file -p HEAD:.env')


# -- 3: the changing forms of the read subcommands --------------------------------------------------------------------

def test_the_changing_forms_everyday_tasks_use_parse_and_the_rest_say_why_not():
    assert parse(['stash', 'save', '-u', 'work', 'in', 'progress']).argv == ['stash', 'push', '--include-untracked', '-m',
                                                                             'work in progress']
    assert parse(['stash', 'save']).argv == ['stash', 'push']
    moved = parse(['branch', '-f', 'release', 'v1.2'])
    assert moved.argv == ['branch', '-f', 'release', 'v1.2'] and moved.destructive and moved.slots == [3]
    assert parse(['branch', '-d', '-f', 'old']).argv == ['branch', '-D', 'old']
    assert parse(['branch', '--force', '-m', 'a', 'b']).argv == ['branch', '-M', 'a', 'b']
    pruned = parse(['worktree', 'prune'])
    assert pruned.argv == ['worktree', 'prune', '-v'] and pruned.destructive
    assert parse(['worktree', 'prune', '--dry-run']).argv == ['worktree', 'prune', '-n', '-v']
    assert not parse(['worktree', 'prune', '-n']).destructive
    locked = parse(['worktree', 'lock', '--reason', 'on a USB disk', '../wt'])
    assert locked.argv == ['worktree', 'lock', '--reason', 'on a USB disk', '../wt'] and locked.folder == '../wt'
    assert parse(['worktree', 'add', '--lock', '-q', '../wt', '-b', 'x', 'main']).argv == [
        'worktree', 'add', '--lock', '-b', 'x', '../wt', 'main']
    assert parse(['remote', 'prune', 'origin']).argv == ['remote', 'prune', 'origin']
    assert parse(['remote', 'prune', '--dry-run', 'origin']).destructive is False
    assert parse(['merge', '--autostash', 'feature']).argv == ['merge', '--autostash', '--no-edit', 'feature']
    assert '--autostash' in parse(['pull', '--autostash', '--rebase']).argv
    for argv in (['worktree', 'move', 'a', 'b'], ['worktree', 'repair'], ['worktree', 'prune', 'x'],
                 ['worktree', 'add', '--track', '-b', 'x', '../x', 'origin/x'], ['remote', 'remove', 'origin'],
                 ['remote', 'prune'], ['stash', 'clear'], ['branch', '-f', '-u', 'origin/x', 'x'],
                 ['stash', 'save', '--', 'a.py']):
        with pytest.raises(PolicyError):
            parse(argv)


# -- 1: what a reviewed command says when Git refuses it or stops part-way -----------------------------------------------

def test_git_output_keeps_lines_hides_credentials_and_keeps_the_end_of_a_long_text():
    text = git_output(b'From https://user:secret@example.com/r.git\n * branch main\r\nok\x07')
    assert text == 'From https://***@example.com/r.git\n * branch main \nok'
    long = git_output(b'a' * 4000 + b'the end', 3000)
    assert long.endswith('the end') and '\n…\n' in long and len(long) < 3100


def test_the_hints_name_the_reviewed_way_past_gits_everyday_refusals():
    merged = failure_hint(['merge', 'feature'], 'error: Your local changes to the following files would be overwritten '
                                                'by merge:')
    assert '--autostash' in merged and '[["stash", "push"], ["merge", "feature"], ["stash", "pop"]]' in merged
    picked = failure_hint(['cherry-pick', 'abc1234'], 'error: your local changes would be overwritten by cherry-pick.')
    assert '--autostash' not in picked and '[["stash", "push"], ["cherry-pick", "abc1234"], ["stash", "pop"]]' in picked
    assert '"-u"' in failure_hint(['switch', 'main'], 'error: The following untracked working tree files would be '
                                                      'overwritten by checkout:')
    assert 'same command can run again' in failure_hint(['tag', 'v1'], 'fatal: something new')
    assert '["stash", "drop"]' in moved_hint(['stash', 'apply'], 1, 'CONFLICT', ['a.py'], True)
    assert 'operation undo' in moved_hint(['merge', 'x'], 1, 'error', [], True)
    words = drift_words(['bindings.dirty', 'bindings.status', 'fingerprint'])
    assert 'files in the working folder changed' in words and 'HEAD or the repository changed' in words


@macos
def test_a_rebase_git_refuses_says_what_git_said_and_autostash_finishes_it(host):  # noqa: F811
    git(host.repo, 'switch', '-q', '-c', 'feature')
    (host.repo / 'feature.txt').write_text('feature\n')
    git(host.repo, 'add', 'feature.txt')
    git(host.repo, 'commit', '-q', '-m', 'Add feature')
    git(host.repo, 'switch', '-q', 'main')
    fix = second_commit(host, message='Fix on main')
    git(host.repo, 'switch', '-q', 'feature')
    wip(host)  # unrelated work in progress, a tracked file among it
    with pytest.raises(PolicyError) as refused:
        command(host, 'rebase', 'main')
    text = str(refused.value)
    assert 'stopped and changed nothing (exit code' in text and 'unstaged changes' in text
    assert '--autostash' in text and '[["stash", "push"], ["rebase", "main"], ["stash", "pop"]]' in text
    _, rebased = command(host, 'rebase', '--autostash', 'main')
    assert rebased['state'] == 'applied' and git(host.repo, 'merge-base', 'feature', 'main').stdout.strip() == fix
    assert (host.repo / 'notes.txt').read_text().endswith('four (draft)\n') and (host.repo / 'scratch.md').exists()


@macos
def test_a_merge_takes_autostash_too(host):  # noqa: F811
    git(host.repo, 'switch', '-q', '-c', 'feature')
    (host.repo / 'notes.txt').write_text('one\ntwo\nthree\nfeature\n')
    git(host.repo, 'commit', '-q', '-am', 'Feature notes')
    git(host.repo, 'switch', '-q', 'main')
    (host.repo / 'notes.txt').write_text('one\ntwo\nthree\n\n')  # the person's edit of the file the merge changes
    with pytest.raises(PolicyError, match='--autostash'):
        command(host, 'merge', 'feature')
    _, merged = command(host, 'merge', '--autostash', 'feature')
    assert merged['state'] == 'applied' and git(host.repo, 'log', '-1', '--format=%s').stdout.strip() == 'Feature notes'


@macos
def test_a_stash_pop_that_conflicts_says_what_it_left_and_how_to_finish(host):  # noqa: F811
    (host.repo / 'app.py').write_text('print("draft")\n')
    command(host, 'stash', 'push')
    second_commit(host, text='print("main")\n', message='Main greeting')
    _, popped = command(host, 'stash', 'pop')
    facts = popped['result']
    assert popped['state'] == 'applied' and facts['exit_code'] == 1 and facts['unmerged'] == ['app.py']
    assert 'CONFLICT' in facts['output'] and '\n' in facts['output']
    assert 'kept the entry' in facts['next'] and '["stash", "drop"]' in facts['next'] and '["add", "--"' in facts['next']
    assert git(host.repo, 'stash', 'list').stdout.count('stash@') == 1
    (host.repo / 'app.py').write_text('print("main and draft")\n')  # the model's resolution
    _, staged = command(host, 'add', '--', 'app.py')
    _, dropped = command(host, 'stash', 'drop')
    assert staged['state'] == dropped['state'] == 'applied' and git(host.repo, 'stash', 'list').stdout == ''


@macos
def test_a_plan_whose_repository_moved_after_review_says_what_moved_and_the_one_step_on(host):  # noqa: F811
    changed = second_commit(host)
    plan = prepare(host, 'revert', changed)
    (host.repo / 'scratch.md').write_text('written after the review\n')
    with pytest.raises(PolicyError, match='changed after review') as refused:
        host.plans.execute(host.chat, plan)
    assert 'What changed: files in the working folder changed' in str(refused.value)
    assert 'The same command proposed again' in str(refused.value)
    assert command(host, 'revert', changed)[1]['state'] == 'applied'


@macos
def test_a_stopped_operation_is_finished_here_never_outside_pebble(host):  # noqa: F811
    git(host.repo, 'switch', '-q', '-c', 'feature')
    one = second_commit(host, text='print("feature")\n', message='Feature greeting')
    (host.repo / 'b.txt').write_text('b\n')
    git(host.repo, 'add', 'b.txt')
    git(host.repo, 'commit', '-q', '-m', 'Add b')
    two = rev(host.repo)
    git(host.repo, 'switch', '-q', 'main')
    second_commit(host, text='print("main")\n', message='Main greeting')
    command(host, 'merge', 'feature')  # stops on app.py
    for attempt in (lambda: host.plans.prepare(host.chat, 'stage', {'paths': '["app.py"]'}),
                    lambda: host.plans.prepare(host.chat, 'commit', {'paths': '["README.md"]', 'message': 'x'})):
        with pytest.raises(PolicyError) as refused:
            attempt()
        assert 'outside Pebble' not in str(refused.value) and '["merge", "--continue"]' in str(refused.value)
    command(host, 'merge', '--abort')
    # a cherry-pick of several commits stopped, then concluded with a plain commit: Git's sequencer stays behind
    git(host.repo, 'cherry-pick', one, two, check=False)
    (host.repo / 'app.py').write_text('print("main and feature")\n')
    git(host.repo, 'add', 'app.py')
    git(host.repo, 'commit', '-q', '--no-edit')
    with pytest.raises(PolicyError) as refused:
        prepare(host, 'tag', 'v1')
    text = str(refused.value)
    assert 'outside Pebble' not in text and '["cherry-pick", "--continue"]' in text and '"--quit"' in text
    _, done = command(host, 'cherry-pick', '--continue')
    assert done['state'] == 'applied' and done['result']['stopped'] is None and (host.repo / 'b.txt').exists()


@macos
def test_a_worktree_made_deeper_beside_the_project_is_removed_by_its_undo(host):  # noqa: F811
    wip(host)
    plan, made = command(host, 'worktree', 'add', '-b', 'hotfix', '../worktrees/hotfix', 'main')
    folder = host.tmp / 'worktrees' / 'hotfix'
    assert plan['bindings']['made'] == [str(host.tmp / 'worktrees')] and made['state'] == 'applied'
    assert (folder / 'app.py').read_text() == 'print("base")\n' and made['result']['undo'] == 'available'
    assert any('Undo removes it' in effect for effect in plan['effects'])
    _, undone = apply(host, 'undo', receipt_id=made['receipt_id'])
    assert undone['state'] == 'applied' and not folder.exists() and not (host.tmp / 'worktrees').exists()
    assert git(host.repo, 'branch', '--list', 'hotfix').stdout == ''
    assert len(git(host.repo, 'worktree', 'list').stdout.splitlines()) == 1
    assert (host.repo / 'notes.txt').read_text().endswith('four (draft)\n')
    git(host.repo, 'branch', 'side')
    _, again = command(host, 'worktree', 'add', '../project-side', 'side')
    (host.tmp / 'project-side' / 'new.txt').write_text('work\n')
    with pytest.raises(PolicyError, match='has changes or new files'):
        apply(host, 'undo', receipt_id=again['receipt_id'])
    assert (host.tmp / 'project-side' / 'new.txt').exists()
    (host.tmp / 'other').mkdir()
    git(host.tmp / 'other', 'init', '-q')
    for folder, words in (('../other/wt', 'inside another Git repository'), ('../../wt', 'inside the project or beside'),
                          ('../project-side', 'already exists')):
        with pytest.raises(PolicyError, match=words):
            prepare(host, 'worktree', 'add', '-b', 'x', folder, 'main')


@macos
def test_worktree_prune_forgets_only_worktrees_whose_folders_are_gone_and_a_lock_keeps_one(host):  # noqa: F811
    git(host.repo, 'branch', 'a')
    git(host.repo, 'branch', 'b')
    command(host, 'worktree', 'add', '../wt-a', 'a')
    command(host, 'worktree', 'add', '../wt-b', 'b')
    with pytest.raises(PolicyError, match='Nothing to prune'):
        prepare(host, 'worktree', 'prune')
    _, locked = command(host, 'worktree', 'lock', '--reason', 'kept on purpose', '../wt-b')
    assert locked['state'] == 'applied'
    shutil.rmtree(host.tmp / 'wt-a')
    shutil.rmtree(host.tmp / 'wt-b')
    plan, pruned = command(host, 'worktree', 'prune')
    assert plan['destructive'] is True and any('wt-a' in effect for effect in plan['effects'])
    assert pruned['state'] == 'applied'
    listed = git(host.repo, 'worktree', 'list', '--porcelain').stdout
    assert 'wt-a' not in listed and 'wt-b' in listed  # locked: kept
    command(host, 'worktree', 'unlock', '../wt-b')
    command(host, 'worktree', 'prune')
    assert 'wt-b' not in git(host.repo, 'worktree', 'list', '--porcelain').stdout


@macos
def test_remote_and_fetch_prune_delete_only_what_the_remote_no_longer_has_and_undo_restores_it(host):  # noqa: F811
    remote = bare(host)
    git(host.repo, 'push', '-q', str(remote), 'main:old-feature')
    git(host.repo, 'fetch', '-q', 'origin')
    gone = rev(host.repo, 'origin/old-feature')
    git(remote, 'branch', '-D', 'old-feature')  # deleted on origin by someone else
    plan, pruned = command(host, 'remote', 'prune', 'origin')
    assert plan['destructive'] is True and any('origin/old-feature' in effect for effect in plan['effects'])
    assert pruned['state'] == 'applied' and 'pruned' in pruned['result']['output']
    assert git(host.repo, 'rev-parse', '-q', '--verify', 'origin/old-feature', check=False).returncode != 0
    assert rev(host.repo, 'origin/main') == rev(host.repo)
    _, undone = apply(host, 'undo', receipt_id=pruned['receipt_id'])
    assert undone['state'] == 'applied' and rev(host.repo, 'origin/old-feature') == gone
    _, fetched = command(host, 'fetch', '--prune', 'origin')
    assert fetched['state'] == 'applied' and 'deleted' in fetched['result']['output']
    assert git(host.repo, 'rev-parse', '-q', '--verify', 'origin/old-feature', check=False).returncode != 0
    with pytest.raises(PolicyError, match='Nothing to prune'):
        prepare(host, 'remote', 'prune', 'origin')


@macos
def test_branch_force_moves_a_branch_that_is_not_checked_out_and_undo_puts_it_back(host):  # noqa: F811
    base = rev(host.repo)
    git(host.repo, 'branch', 'release')
    newer = second_commit(host)
    plan, moved = command(host, 'branch', '-f', 'release', 'main')
    assert plan['destructive'] is True and plan['bindings']['run'] == ['branch', '-f', 'release', newer]
    assert moved['state'] == 'applied' and rev(host.repo, 'release') == newer
    apply(host, 'undo', receipt_id=moved['receipt_id'])
    assert rev(host.repo, 'release') == base
    with pytest.raises(PolicyError, match='changed nothing'):  # Git refuses the checked-out branch
        command(host, 'branch', '-f', 'main', base)


# -- the private cases d-gw-200..205, replayed as test_git_private_cases_46 replays its own ---------------------------

REPLAYS = {  # the reviewed steps a capable model makes; ('raw', text) is what it types in RunCommand
    'd-gw-200': [('command', ['worktree', 'add', '-b', 'hotfix/login', '../worktrees/hotfix-login', 'main'])],
    'd-gw-201': [('raw', 'git stash save -u "monthly view draft"')],
    'd-gw-203': [('command', ['worktree', 'add', '../release-1x', 'release/1.x']),
                 ('command', ['worktree', 'lock', '--reason', 'kept for 1.x support', '../release-1x'])],
    'd-gw-204': [('command', ['merge', '--autostash', 'feature']),
                 ('write', ('app/report.py', "from app.money import total\n\n\ndef summary(items):\n"
                                             "    return 'Total: %s EUR (%d items)' % (total(items), len(items))\n")),
                 ('command', ['reset', '--', 'app/report.py']), ('command', ['stash', 'drop'])],
    'd-gw-205': [('command', ['branch', '-f', 'release', 'v1.1.0'])],
}


@pytest.mark.parametrize('case', sorted(REPLAYS))
def test_the_private_cases_pass_through_the_reviewed_route(case, cells):  # noqa: F811
    from test_git_private_cases_46 import DONE, SPLIT, grade, run, turn
    if not MAC or SPLIT is None or not (SPLIT / 'checkers' / (case + '.py')).is_file():
        pytest.skip('needs macOS sandbox-exec and the private golden-extra split (PEBBLE_GOLDEN_EXTRA)')
    host = cells(case)
    for kind, step in REPLAYS[case]:
        if kind == 'write':
            (host.repo / step[0]).write_text(step[1])
        else:
            argv = review(step, root=host.repo) if kind == 'raw' else step
            result = run(host, ('command', argv))
            if case == 'd-gw-204' and argv[0] == 'merge':  # Git put the edit back with a conflict: the way on is named
                assert result['result']['unmerged'] == ['app/report.py'] and '["stash", "drop"]' in result['result']['next']
    turn(host, DONE)
    assert grade(host, case) == ('passed', [])


# -- 2: RunCommand through the runner, as the golden harness drives it ----------------------------------------------

def project_repo(project):
    """main with work in progress (an unrelated edit of notes.txt) and a feature branch one commit ahead."""
    git(project, 'init', '-q')
    (project / 'app.py').write_text('print("base")\n')
    (project / 'notes.txt').write_text('one\n')
    git(project, 'add', '-A')
    git(project, 'commit', '-q', '-m', 'Initial import')
    git(project, 'config', 'user.name', 'Reviewed Author')
    git(project, 'config', 'user.email', 'author@example.invalid')
    git(project, 'switch', '-q', '-c', 'feature')
    (project / 'feature.txt').write_text('feature\n')
    git(project, 'add', 'feature.txt')
    git(project, 'commit', '-q', '-m', 'Add feature')
    git(project, 'switch', '-q', 'main')
    (project / 'notes.txt').write_text('one\ntwo (draft)\n')
    return rev(project, 'feature')


def auto(env, monkeypatch, script):  # noqa: F811
    app = env.start(32768)
    app.store.set_setting('permissions', {**DEFAULT, 'mode': 'auto'})
    monkeypatch.setattr(app.guard, 'handle', lambda *args, **kwargs: {'action': 'allow'})
    env.script = script
    return app


def ran(app, env, text):  # noqa: F811
    chat = app.new_chat(str(env.project))
    app.work(chat, text, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    return chat['id'], finished(app, chat['id'], rid)


def tool_result(request, ident):
    return next(json.loads(m['content']) for m in request if m.get('role') == 'tool' and m.get('tool_call_id') == ident)


@macos
def test_an_undoable_git_command_typed_in_run_command_runs_as_the_reviewed_command(env, monkeypatch):  # noqa: F811
    feature = project_repo(env.project)
    app = auto(env, monkeypatch, [call('RunCommand', {'command': f'git cherry-pick {feature}'}, 'c1'),
                                  final('Brought the commit onto main.\n\nFINAL: done')])
    cid, run = ran(app, env, 'Bring the "Add feature" commit from feature onto main. End your reply with FINAL: done.')
    assert run['state'] == 'completed', run['error']
    assert git(env.project, 'log', '-1', '--format=%s', 'main').stdout.strip() == 'Add feature'
    assert (env.project / 'notes.txt').read_text() == 'one\ntwo (draft)\n'
    shown = tool_result(env.requests[1], 'c1')
    assert shown['status'] == 'applied' and shown['kind'] == 'git' and shown['exit_code'] == 0 and shown['output']
    assert shown['reviewed_as']['tool'] == 'ProposeGitChange' and 'snapshot' not in json.dumps(shown)
    assert [a['status'] for a in app.store.actions(cid) if a['kind'] == 'git'] == ['applied']


@macos
def test_a_destructive_one_waits_for_its_card_and_the_reply_after_it_reads_gits_own_words(env, monkeypatch):  # noqa: F811
    project_repo(env.project)
    (env.project / 'app.py').write_text('print("main")\n')
    git(env.project, 'commit', '-q', '-m', 'Main change', '--', 'app.py')
    main = rev(env.project)
    git(env.project, 'switch', '-q', 'feature')  # the unrelated notes.txt edit comes along
    app = auto(env, monkeypatch, [call('RunCommand', {'command': 'git rebase --autostash main'}, 'c1'),
                                  final('Rebased feature onto main; your notes are as they were.\n\nFINAL: done')])
    cid, run = ran(app, env, 'Rebase my feature branch onto main. Keep my notes.txt edit. End with FINAL: done.')
    assert run['state'] == 'completed', run['error']
    assert git(env.project, 'merge-base', 'feature', 'main').stdout.strip() == main
    assert (env.project / 'notes.txt').read_text() == 'one\ntwo (draft)\n'
    after = json.dumps(env.requests[1])
    assert 'awaiting_user_approval' in after and '\\"exit_code\\":0' in after  # the receipt, Git's code on top
    assert [a['status'] for a in app.store.actions(cid) if a['kind'] == 'git'] == ['applied']


@macos
def test_a_push_typed_in_run_command_waits_for_its_card_even_where_the_guard_allows_it(env, monkeypatch):  # noqa: F811
    project_repo(env.project)
    remote = env.project.parent / 'remote.git'
    git(env.project.parent, 'init', '-q', '--bare', str(remote))
    git(env.project, 'remote', 'add', 'origin', str(remote))
    git(env.project, 'push', '-q', 'origin', 'main')
    before = git(remote, 'rev-parse', 'main').stdout.strip()
    git(env.project, 'commit', '-q', '--allow-empty', '-m', 'Local only')
    app = auto(env, monkeypatch, [call('RunCommand', {'command': 'git push origin main'}, 'c1'),
                                  final('Pushed.\n\nFINAL: done')])  # the guard allows everything here
    chat = app.new_chat(str(env.project))
    app.work(chat, 'Push main to origin. End your reply with FINAL: done.', False, [])
    run = app.runs.list(chat['id'])[0]
    [action] = [a for a in app.store.actions(chat['id']) if a['kind'] == 'git']
    assert run['state'] == 'waiting_approval' and action['status'] == 'pending'
    assert action['payload']['operation'] == 'push' and action['payload']['requires_review'] is True
    assert git(remote, 'rev-parse', 'main').stdout.strip() == before and len(env.requests) == 1


@macos
def test_a_git_command_the_reviewed_operation_refuses_is_refused_with_both_reasons(env, monkeypatch):  # noqa: F811
    project_repo(env.project)
    app = auto(env, monkeypatch, [call('RunCommand', {'command': 'git stash clear'}, 'c1'),
                                  final('The stash could not be cleared here.\n\nFINAL: blocked')])
    cid, run = ran(app, env, 'Clear my stash list. End your reply with FINAL: done.')
    shown = tool_result(env.requests[1], 'c1')
    assert 'Raw Git commands are outside the generic terminal broker' in shown['error']
    assert 'As a reviewed command it is not available either' in shown['error']
    assert 'stash clear is not available' in shown['error']
    assert not [a for a in app.store.actions(cid) if a['kind'] == 'git']


@macos
@pytest.mark.parametrize('switch, text, refused', [
    ('0', 'Bring the "Add feature" commit from feature onto main. End your reply with FINAL: done.',
     'Raw Git commands are outside the generic terminal broker'),
    ('1', 'Run git log and tell me what the last commit did. Don\'t change anything.', 'asks to change nothing'),
])
def test_routing_stays_off_with_its_switch_and_for_a_request_that_keeps_the_project(env, monkeypatch, switch, text,  # noqa: F811
                                                                                    refused):
    monkeypatch.setenv('PEBBLE_1929_RAW_GIT_REVIEW', switch)
    feature = project_repo(env.project)
    app = auto(env, monkeypatch, [call('RunCommand', {'command': f'git cherry-pick {feature}'}, 'c1'),
                                  final('Nothing was changed.\n\nFINAL: blocked')])
    cid, run = ran(app, env, text)
    assert refused in tool_result(env.requests[1], 'c1')['error']
    assert git(env.project, 'log', '-1', '--format=%s', 'main').stdout.strip() == 'Initial import'
    assert not [a for a in app.store.actions(cid) if a['kind'] == 'git']
