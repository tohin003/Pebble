"""Phase 46 (1.9.28, w9-git): the git_workflow refusal map's F3 as the second opinion's security review bounds it
(fix-research-1.9.28/git-recovery.md 1.5 and second-opinion.md 3.6), on real temporary repositories.

Measured first (1.9.27, map1.jsonl: 117 reviewed argv on golden-extra d-gw-106): a reflog entry or ORIG_HEAD ("Use a
plain revision"), --ours/--theirs and commit --no-edit while a merge is stopped, "." as a path ("Paths must stay inside
the selected project", which reads as a scope refusal), revert A..B, -X on rebase and cherry-pick, an interactive rebase
(its refusal named only the allowed options) and `git worktree list` (refused everywhere, and pointed at
ReadGitWorktrees, which lists only Pebble's own worktrees) each cost a refused attempt, or ended the run where the model
gave up. Each now runs inside the existing review path: one reviewed command, its revisions bound at review as commit
IDs, the before-snapshot Undo. Repositories with submodules stay unsupported, with a refusal that says to stop trying.
"""

import json

import pytest

from pebble import git_api
from pebble.git_commands import BOUND, parse, read
from pebble.git_plans import paths_arg
from pebble.git_workflows import NAMED_REVISIONS, REVISION, SUBMODULES, with_named_revisions
from pebble.policy import PolicyError
from test_git_commands_46 import command, second_commit, wip
from test_git_plans import apply, git, host, macos, rev  # noqa: F401 (fixture)


def prepare(host, *argv):  # noqa: F811
    return host.plans.prepare(host.chat, 'command', {'argv': json.dumps(list(argv))})


@pytest.fixture
def raw_reads(monkeypatch):
    monkeypatch.setenv('PEBBLE_RAW_GIT_READS', '1')


# -- a: reflog entries and ORIG_HEAD, bound at review ------------------------------------------------------------------

def test_reflog_entries_and_orig_head_parse_only_where_they_run_as_the_bound_commit():
    for argv, name in ((['reset', '--keep', 'ORIG_HEAD'], 'ORIG_HEAD'), (['reset', '--hard', 'HEAD@{1}'], 'HEAD@{1}'),
                       (['branch', 'recovered', 'main@{1}'], 'main@{1}'),
                       (['checkout', 'HEAD@{2}', '--', 'notes.txt'], 'HEAD@{2}'), (['revert', 'ORIG_HEAD'], 'ORIG_HEAD'),
                       (['cherry-pick', 'HEAD@{1}~1'], 'HEAD@{1}~1'), (['merge', 'ORIG_HEAD'], 'ORIG_HEAD'),
                       (['switch', '--detach', 'HEAD@{3}'], 'HEAD@{3}'), (['tag', 'v0.9', 'main@{2}^'], 'main@{2}^'),
                       (['checkout', 'stash@{0}', '--', 'notes.txt'], 'stash@{0}')):
        parsed = parse(argv)
        assert name in parsed.revisions and parsed.argv.index(name) in parsed.slots, argv
    assert parse(['checkout', 'HEAD@{1}']).slots == [1]  # no branch: detached at the bound commit, not a name
    assert parse(['stash', 'pop', 'stash@{1}']).argv == ['stash', 'pop', 'stash@{1}']  # an entry; the stash list is bound
    for argv in (['merge', 'FETCH_HEAD'], ['revert', 'MERGE_HEAD'], ['cherry-pick', 'CHERRY_PICK_HEAD~1'],
                 ['merge', 'main@{1.hour.ago}'], ['merge', 'main@{upstream}'], ['switch', '@{-1}'], ['switch', '-'],
                 ['switch', 'HEAD@{1}'], ['rebase', 'main', 'HEAD@{1}'], ['cherry-pick', 'HEAD@{2}..HEAD'],
                 ['branch', '-u', 'main@{1}'], ['reset', '--hard', 'HEAD@{1}..'], ['revert', 'a..b@{1}']):
        with pytest.raises(PolicyError):
            parse(argv)
    assert BOUND.fullmatch('ORIG_HEAD~2') and not BOUND.fullmatch('FETCH_HEAD') and not BOUND.fullmatch('HEAD@{-1}')


def test_read_tools_take_a_reflog_entry_too():
    for value in ('HEAD@{1}', 'main@{2}~1', '@{3}', 'stash@{0}', 'ORIG_HEAD', 'origin/main'):
        assert REVISION.fullmatch(value), value
    for value in ('main@{1.day.ago}', '@{-1}', 'HEAD@{u}', '--all', 'a b'):
        assert not REVISION.fullmatch(value), value


@macos
def test_orig_head_and_a_reflog_entry_run_as_the_commit_bound_at_review_and_show_both(host):  # noqa: F811
    base = rev(host.repo)
    lost = second_commit(host, message='Work that was reset away')
    git(host.repo, 'reset', '-q', '--hard', base)  # the person's mistake: ORIG_HEAD and main@{1} name the lost commit
    wip(host)
    plan = prepare(host, 'branch', 'recovered', 'main@{1}')
    assert plan['bindings']['run'] == ['branch', 'recovered', lost]
    assert any(f'main@{{1}} is commit {lost[:12]}' in effect for effect in plan['effects'])
    assert host.plans.execute(host.chat, plan)['state'] == 'applied' and rev(host.repo, 'recovered') == lost
    plan, moved = command(host, 'reset', '--keep', 'ORIG_HEAD')
    assert plan['bindings']['run'] == ['reset', '--keep', lost] and plan['destructive'] is True
    assert any(f'ORIG_HEAD is commit {lost[:12]}' in effect for effect in plan['effects'])
    assert moved['state'] == 'applied' and rev(host.repo) == lost and moved['result']['undo'] == 'available'
    assert (host.repo / 'notes.txt').read_text().endswith('four (draft)\n')  # the work in progress stays
    # in a sequence only the first command's is bound at review (and shown); a later command's would be bound at its turn
    sequence = host.plans.prepare(host.chat, 'command', {'argv': json.dumps([['branch', 'keep', 'ORIG_HEAD'],
                                                                             ['tag', 'v0.1', 'keep']])})
    assert any(f'ORIG_HEAD is commit {base[:12]}' in effect for effect in sequence['effects'])
    with pytest.raises(PolicyError, match='only in the first command of a sequence'):
        host.plans.prepare(host.chat, 'command', {'argv': json.dumps([['tag', 'v0.1'], ['branch', 'keep', 'HEAD@{1}']])})
    # the reflog moves after review: the plan is refused before anything runs
    plan = prepare(host, 'reset', '--hard', 'HEAD@{2}')
    git(host.repo, 'branch', '-q', 'elsewhere')
    git(host.repo, 'switch', '-q', 'elsewhere')
    with pytest.raises(PolicyError, match='changed after review'):
        host.plans.execute(host.chat, plan)
    assert rev(host.repo) == lost


# -- b: revert ranges and -X on rebase, cherry-pick and revert ---------------------------------------------------------

def test_conflict_side_options_and_revert_ranges_parse():
    assert parse(['rebase', '-X', 'theirs', 'main']).argv == ['rebase', '-X', 'theirs', 'main']
    assert parse(['cherry-pick', '--strategy-option=ours', 'abc1234']).argv == ['cherry-pick', '-X', 'ours', 'abc1234']
    assert parse(['revert', '-X', 'theirs', 'abc1234']).argv == ['revert', '-X', 'theirs', '--no-edit', 'abc1234']
    ranged = parse(['revert', 'main~2..main'])
    assert ranged.argv == ['revert', '--no-edit', 'main~2..main'] and ranged.revisions == [] and not ranged.slots
    for argv in (['rebase', '-X', 'patience', 'main'], ['cherry-pick', '-X', 'subtree=x', 'abc1234'],
                 ['revert', '-s', 'ours', 'abc1234']):
        with pytest.raises(PolicyError):
            parse(argv)


@macos
def test_a_revert_range_runs_newest_first_as_the_commits_bound_at_review(host):  # noqa: F811
    one = second_commit(host, text='print("one")\n', message='One')
    two = second_commit(host, text='print("two")\n', message='Two')  # changes the line One wrote
    wip(host)
    plan, reverted = command(host, 'revert', 'HEAD~2..HEAD')
    assert plan['bindings']['run'] == ['revert', '--no-edit', two, one]  # oldest first would conflict on app.py
    assert reverted['state'] == 'applied' and reverted['result']['stopped'] is None
    assert (host.repo / 'app.py').read_text() == 'print("base")\n'
    assert git(host.repo, 'log', '-2', '--format=%s').stdout.split('\n')[:2] == ['Revert "One"', 'Revert "Two"']
    _, undone = apply(host, 'undo', receipt_id=reverted['receipt_id'])
    assert undone['state'] == 'applied' and rev(host.repo) == two


@macos
def test_a_cherry_pick_and_a_rebase_take_the_side_the_request_names(host):  # noqa: F811
    git(host.repo, 'switch', '-q', '-c', 'feature')
    picked = second_commit(host, text='print("feature")\n', message='Feature greeting')
    git(host.repo, 'switch', '-q', 'main')
    second_commit(host, text='print("main")\n', message='Main greeting')
    _, done = command(host, 'cherry-pick', '-X', 'theirs', picked)  # both changed the greeting: the picked one wins
    assert done['state'] == 'applied' and done['result']['stopped'] is None
    assert (host.repo / 'app.py').read_text() == 'print("feature")\n'
    git(host.repo, 'reset', '-q', '--hard', 'HEAD~1')
    git(host.repo, 'switch', '-q', 'feature')
    _, rebased = command(host, 'rebase', '-X', 'ours', 'main')  # in a rebase "ours" is the branch rebased onto
    assert rebased['state'] == 'applied' and rebased['result']['stopped'] is None
    assert (host.repo / 'app.py').read_text() == 'print("main")\n'


# -- c: a stopped conflict: one side, a file from a branch, Git's prepared message -------------------------------------

def test_conflict_commands_parse_as_midway_commands():
    side = parse(['checkout', '--theirs', 'config.py'])
    assert side.argv == ['checkout', '--theirs', '--', 'config.py'] and side.side == '--theirs'
    assert side.midway and side.destructive and side.paths == ['config.py']
    assert parse(['restore', '--ours', '--', 'a.py', 'b.py']).argv == ['restore', '--ours', '--', 'a.py', 'b.py']
    assert parse(['restore', '--worktree', '--theirs', 'a.py']).side == '--theirs'
    assert parse(['checkout', 'feature', '--', 'config.py']).midway
    assert parse(['restore', '--source', 'feature', 'config.py']).midway
    assert parse(['commit', '--no-edit']).argv == ['commit', '--no-edit'] and parse(['commit', '--no-edit']).midway
    for argv in (['checkout', '--ours'], ['checkout', '--ours', '--theirs', 'a.py'], ['checkout', '--ours', '-b', 'x', 'a'],
                 ['restore', '--theirs', '--source', 'main', 'a.py'], ['restore', '--ours', '--staged', 'a.py'],
                 ['checkout', '--theirs', '--', '.env'], ['restore', '--ours', '../x']):
        with pytest.raises(PolicyError):
            parse(argv)


def conflict(host):
    git(host.repo, 'switch', '-q', '-c', 'feature')
    second_commit(host, text='print("feature")\n', message='Feature greeting')
    git(host.repo, 'switch', '-q', 'main')
    second_commit(host, text='print("main")\n', message='Main greeting')
    _, stopped = command(host, 'merge', 'feature')
    assert stopped['result']['stopped'] == 'merge' and stopped['result']['unmerged'] == ['app.py']
    return stopped


@macos
def test_a_stopped_merge_is_settled_with_a_side_and_finished_with_the_prepared_message(host):  # noqa: F811
    with pytest.raises(PolicyError, match='Nothing is stopped'):
        prepare(host, 'checkout', '--theirs', 'app.py')
    with pytest.raises(PolicyError, match='--no-edit takes the message Git prepared'):
        prepare(host, 'commit', '--no-edit')
    wip(host)
    conflict(host)
    plan, taken = command(host, 'checkout', '--theirs', 'app.py')
    assert plan['bindings']['run'] == ['checkout', '--theirs', '--', 'app.py'] and plan['destructive'] is True
    assert any('theirs side of the stopped merge' in effect for effect in plan['effects'])
    assert taken['state'] == 'applied' and (host.repo / 'app.py').read_text() == 'print("feature")\n'
    command(host, 'add', '--', 'app.py')
    _, merged = command(host, 'commit', '--no-edit')
    assert merged['state'] == 'applied' and merged['result']['stopped'] is None
    assert git(host.repo, 'log', '-1', '--format=%P %s').stdout.count(' ') >= 2  # two parents
    # Git's prepared message; it names the commit ID the reviewed merge was bound to ("Merge commit '...'"), as every
    # reviewed merge without -m has since Phase 46
    assert git(host.repo, 'log', '-1', '--format=%s').stdout.startswith('Merge commit ')
    assert (host.repo / 'notes.txt').read_text().endswith('four (draft)\n') and (host.repo / 'scratch.md').exists()


@macos
def test_a_stopped_merge_takes_a_file_from_a_branch_or_a_commit(host):  # noqa: F811
    conflict(host)
    _, taken = command(host, 'checkout', 'feature', '--', 'app.py')
    assert taken['state'] == 'applied' and (host.repo / 'app.py').read_text() == 'print("feature")\n'
    # Restated in follow-up 1 (the w9 security review, F1): taking feature's version resolved app.py, so it is no longer
    # a conflicted file, and while the merge is stopped only conflicted files may be overwritten (no Undo exists then)
    with pytest.raises(PolicyError, match='none is conflicted now'):
        prepare(host, 'restore', '--source', 'main', '--', 'app.py')
    command(host, 'add', '--', 'app.py')
    _, done = command(host, 'merge', '--continue')
    assert done['state'] == 'applied' and done['result']['stopped'] is None


# -- d: refusals that name the way, and "." as a path ------------------------------------------------------------------

def test_an_interactive_rebase_or_a_fixup_is_answered_with_the_reviewed_sequences():
    for argv in (['rebase', '-i', 'HEAD~3'], ['rebase', '--interactive', '--autosquash', 'main'],
                 ['commit', '--fixup', 'abc1234'], ['commit', '--fixup=abc1234'], ['commit', '--squash', 'abc1234']):
        with pytest.raises(PolicyError) as refused:
            parse(argv)
        text = str(refused.value)
        assert '["switch", "--detach", "X"]' in text and '["reset", "--soft", "HEAD~N"]' in text, argv
        assert '["commit", "--amend", "--no-edit"]' in text


@macos
def test_the_reword_sequence_the_refusal_names_rewords_an_older_commit_and_keeps_the_work(host):  # noqa: F811
    older = second_commit(host, text='print("one")\n', message='Cuont items')
    second_commit(host, text='print("two")\n', message='Later work')
    wip(host)
    _, done = apply(host, 'command', argv=json.dumps([['switch', '--detach', older],
                                                      ['commit', '--amend', '-m', 'Count items'],
                                                      ['rebase', '--autostash', '--onto', 'HEAD', older, 'main']]))
    assert done['state'] == 'applied' and done['result']['failed_at'] is None, done
    assert git(host.repo, 'log', '-2', '--format=%s').stdout.split('\n')[:2] == ['Later work', 'Count items']
    assert git(host.repo, 'symbolic-ref', 'HEAD').stdout.strip() == 'refs/heads/main'
    assert (host.repo / 'notes.txt').read_text().endswith('four (draft)\n')


def test_dot_as_a_path_is_add_all_or_a_refusal_that_says_what_works():
    for argv, canonical in ((['add', '.'], ['add', '--all']), (['add', '--', '.'], ['add', '--all']),
                            (['add', '-A', '.'], ['add', '--all']), (['add', '-u', '.'], ['add', '--update']),
                            (['add', './'], ['add', '--all'])):
        assert parse(argv).argv == canonical and parse(argv).paths == [], argv
    for argv in (['restore', '.'], ['restore', '--staged', '.'], ['checkout', '--', '.'], ['rm', '-r', '--cached', '.'],
                 ['reset', '.'], ['reset', '--', '.'], ['reset', 'HEAD', '.'], ['stash', 'push', '--', '.'],
                 ['mv', '.', 'elsewhere'], ['add', '-N', '.'], ['clean', '-n', '--', '.']):
        with pytest.raises(PolicyError, match=r'"\." \(the whole project\) is not a reviewed path'):
            parse(argv)
    with pytest.raises(PolicyError, match='Paths must stay inside the selected project'):
        paths_arg(['.'])  # the typed operations keep their contract and their words


@macos
def test_add_dot_stages_every_change_and_no_form_of_add_stages_a_private_file(host):  # noqa: F811
    wip(host)
    (host.repo / 'config').mkdir()
    (host.repo / 'config' / 'app.toml').write_text('debug = false\n')
    plan, staged = command(host, 'add', '.')
    assert plan['bindings']['run'] == ['add', '--all'] and staged['state'] == 'applied'
    assert set(git(host.repo, 'diff', '--cached', '--name-only').stdout.split()) == {'config/app.toml', 'notes.txt',
                                                                                    'scratch.md'}
    git(host.repo, 'reset', '-q')
    (host.repo / 'config' / 'deploy.pem').write_text('not a real key\n')
    for argv in (['add', '.'], ['add', '-A'], ['add', '--', 'config'], ['add', 'config']):
        with pytest.raises(PolicyError, match='private file') as refused:
            prepare(host, *argv)
        assert 'deploy.pem' not in str(refused.value)  # names stay out, as ReadGitStatus leaves them out
    assert prepare(host, 'add', '-u')['bindings']['run'] == ['add', '--update']  # tracked files only: nothing private
    assert prepare(host, 'add', '--', 'config/app.toml')['bindings']['run'] == ['add', '--', 'config/app.toml']
    git(host.repo, 'add', '-f', 'config/deploy.pem')
    git(host.repo, 'commit', '-q', '-m', 'A key someone committed')
    (host.repo / 'config' / 'deploy.pem').write_text('rotated\n')
    for argv in (['add', '-u'], ['commit', '-a', '-m', 'Everything'], ['commit', '-m', 'Config', '--', 'config']):
        with pytest.raises(PolicyError, match='private file'):
            prepare(host, *argv)


# -- e: worktrees ------------------------------------------------------------------------------------------------------

@pytest.mark.usefixtures('raw_reads')
def test_worktree_list_is_a_raw_read_and_the_reviewed_refusal_points_there():
    assert read('git worktree list') == ['worktree', 'list']
    assert read('git worktree list --porcelain') == ['worktree', 'list', '--porcelain']
    for text in ('git worktree add ../x main', 'git worktree prune', 'git worktree remove ../x', 'git worktree',
                 'git worktree list --expire now', 'git worktree lock x'):
        assert read(text) is None, text
    with pytest.raises(PolicyError, match='RunCommand "git worktree list" lists them') as refused:
        parse(['worktree', 'list'])
    assert 'ReadGitWorktrees' not in str(refused.value)
    with pytest.raises(PolicyError, match='RunCommand also runs "git reflog ..."'):
        parse(['reflog'])


@macos
def test_worktree_list_runs_confined_and_shows_a_linked_worktree(host):  # noqa: F811
    command(host, 'worktree', 'add', '-b', 'hotfix', '../project-hotfix', 'main')
    shown = host.git.raw(host.chat, ['worktree', 'list', '--porcelain'])
    assert shown['exit_code'] == 0 and f'worktree {host.tmp / "project-hotfix"}' in shown['output']
    assert 'branch refs/heads/hotfix' in shown['output']


def test_read_git_worktrees_says_above_8k_how_linked_worktrees_are_listed_and_made():
    spec = next(item for item in git_api.SCHEMAS if item['function']['name'] == 'ReadGitWorktrees')
    assert 'native-only' in spec['function']['description']  # 4K and 8K keep 1.9.27's text (golden fixtures)
    large = with_named_revisions(spec)['function']['description']
    assert large == NAMED_REVISIONS['ReadGitWorktrees'] and 'native-only' not in large
    assert 'RunCommand "git worktree list"' in large and 'ProposeGitChange' in large


# -- submodules: still unsupported, and said once -----------------------------------------------------------------------

@macos
@pytest.mark.usefixtures('raw_reads')
def test_a_repository_with_submodules_says_that_git_stops_here_for_every_git_tool(host):  # noqa: F811
    (host.repo / '.gitmodules').write_text('[submodule "lib"]\n\tpath = lib\n\turl = ../lib.git\n')
    calls = (lambda: host.git.status(host.chat), lambda: host.git.raw(host.chat, ['log', '-1']),
             lambda: prepare(host, 'add', '-A'), lambda: host.plans.prepare(host.chat, 'stage', {'paths': '["app.py"]'}))
    for call in calls:
        with pytest.raises(PolicyError) as refused:
            call()
        assert str(refused.value) == SUBMODULES
    assert 'do not try another Git tool' in SUBMODULES and 'left for the person' in SUBMODULES
