"""Phase 46: reviewed Git commands (ProposeGitChange operation `command`) on real temporary repositories.

Measured first: on the golden set 2.0's held-out git_workflow family Pebble 1.9.20 passed 1 of 15 cells and Codex and
Claude Code 15 of 15; Pebble's terminal refuses raw Git and the typed plans had no revert, cherry-pick, tag, stash,
amend, uncommit, rebase or pull. The fixtures and helpers are test_git_plans' (sandboxed Git, isolated environment).
"""

import json

import pytest

from pebble.git_commands import parse
from pebble.git_plans import OPERATIONS, TOOLS
from pebble.policy import PolicyError
from test_git_plans import MAC, apply, bare, git, host, rev, status  # noqa: F401 (fixture)


def command(host, *argv):
    return apply(host, 'command', argv=json.dumps(list(argv)))


def files(repo):
    return {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob('*') if p.is_file() and '.git' not in p.parts}


def wip(host):
    """The person's unrelated work in progress: an unstaged edit and an untracked file."""
    (host.repo / 'notes.txt').write_text('one\ntwo\nthree\nfour (draft)\n')
    (host.repo / 'scratch.md').write_text('ideas\n')


def second_commit(host, text='print("changed")\n', message='Change the greeting'):
    (host.repo / 'app.py').write_text(text)
    git(host.repo, 'add', 'app.py')
    git(host.repo, 'commit', '-q', '-m', message)
    return rev(host.repo)


def test_the_schema_offers_command_and_the_parser_refuses_what_it_cannot_review():
    assert 'command' in OPERATIONS and 'argv' in TOOLS[0]['function']['parameters']['properties']
    assert 'revert' in TOOLS[0]['function']['description']
    # Phase 46 (w1-git): a forced or deleting branch push, config user.* and clean -f now parse; git_plans refuses them
    # while the person's git_decisions switch is off (D159; test_git_everyday_46). These stay refused by the parser.
    for argv in (['push', '--mirror', 'origin'], ['rebase', '-i', 'main'], ['config', '--global', 'user.name', 'x'],
                 ['-c', 'core.hooksPath=/tmp/x', 'commit', '-m', 'x'], ['commit', '--amend'], ['add', '../x'],
                 ['clean', '-ffd'], ['push', 'origin', 'a:b:c'], ['rebase', '--exec', 'make', 'main'],
                 ['config', 'core.hooksPath', '/tmp/x'], ['bisect', 'run', 'make']):
        with pytest.raises(PolicyError):
            parse(argv)
    assert parse(['revert', 'HEAD']).argv == ['revert', '--no-edit', 'HEAD']
    assert parse(['commit', '--amend', '--no-edit']).destructive and parse(['push', 'origin', 'v1']).publication


def test_revert_keeps_unrelated_work_in_progress_and_undo_restores_the_branch(host):
    base = rev(host.repo)
    changed = second_commit(host)
    wip(host)
    before = files(host.repo)
    plan, result = command(host, 'revert', changed)
    assert plan['destructive'] is False and plan['bindings']['resolved'] == {changed: changed}
    assert result['state'] == 'applied' and result['result']['exit_code'] == 0
    head = rev(host.repo)
    assert head != changed and git(host.repo, 'log', '-1', '--format=%s').stdout.startswith('Revert "Change the greeting"')
    assert (host.repo / 'app.py').read_text() == 'print("base")\n'
    after = files(host.repo)
    assert after['notes.txt'] == before['notes.txt'] and after['scratch.md'] == before['scratch.md']
    assert git(host.repo, 'log', '-1', '--format=%an <%ae>').stdout.strip() == 'Reviewed Author <author@example.invalid>'
    undo_plan, undone = apply(host, 'undo', receipt_id=result['receipt_id'])
    assert undo_plan['destructive'] is True and undone['state'] == 'applied'
    assert rev(host.repo) == changed and files(host.repo) == before
    assert base != changed


def test_tags_are_created_and_pushed_only_when_the_remote_has_their_commit(host):
    remote = bare(host)
    _, result = command(host, 'tag', '-a', 'v1.0.0', '-m', 'Release 1.0.0')
    assert result['state'] == 'applied'
    assert git(host.repo, 'cat-file', '-t', 'v1.0.0').stdout.strip() == 'tag'
    plan, pushed = command(host, 'push', 'origin', 'v1.0.0')
    assert plan['publication'] is True and pushed['state'] == 'applied'
    assert git(remote, 'rev-parse', 'v1.0.0^{commit}').stdout.strip() == rev(host.repo)
    second_commit(host)
    command(host, 'tag', 'v1.1.0')
    with pytest.raises(PolicyError, match='does not have yet'):
        host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['push', 'origin', 'v1.1.0'])})
    # Phase 46 (w1-git): a branch spelled as a Git command is the reviewed push operation, not a refused tag push
    plan = host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['push', 'origin', 'main'])})
    assert plan['operation'] == 'push' and plan['bindings']['new_oid'] == rev(host.repo)


def test_stash_sets_work_aside_and_pop_brings_it_back_and_undo_restores_both(host):
    wip(host)
    before = files(host.repo)
    _, stashed = command(host, 'stash', 'push', '-u', '-m', 'draft notes')
    assert stashed['state'] == 'applied' and status(host.repo) == ''
    assert git(host.repo, 'stash', 'list').stdout.count('draft notes') == 1
    undo_plan, undone = apply(host, 'undo', receipt_id=stashed['receipt_id'])
    assert undone['state'] == 'applied' and files(host.repo) == before
    assert git(host.repo, 'stash', 'list').stdout == ''
    _, again = command(host, 'stash', 'push', '-u', '-m', 'draft notes')
    _, popped = command(host, 'stash', 'pop')
    assert popped['state'] == 'applied' and files(host.repo) == before and git(host.repo, 'stash', 'list').stdout == ''


def test_amend_and_uncommit_rewrite_only_when_asked_and_undo_puts_history_back(host):
    changed = second_commit(host, message='Chnage the greeting')
    plan, amended = command(host, 'commit', '--amend', '-m', 'Change the greeting')
    assert plan['destructive'] is True and amended['state'] == 'applied'
    assert git(host.repo, 'log', '-1', '--format=%s').stdout.strip() == 'Change the greeting' and rev(host.repo) != changed
    apply(host, 'undo', receipt_id=amended['receipt_id'])
    assert rev(host.repo) == changed
    plan, uncommitted = command(host, 'reset', '--soft', 'HEAD~1')
    assert plan['destructive'] is True and uncommitted['state'] == 'applied'
    assert git(host.repo, 'diff', '--cached', '--name-only').stdout.strip() == 'app.py'


def test_cherry_pick_and_rebase_onto_main(host):
    git(host.repo, 'switch', '-q', '-c', 'feature')
    (host.repo / 'feature.txt').write_text('feature\n')
    git(host.repo, 'add', 'feature.txt')
    git(host.repo, 'commit', '-q', '-m', 'Add feature')
    feature = rev(host.repo)
    git(host.repo, 'switch', '-q', 'main')
    fix = second_commit(host, message='Fix on main')
    _, picked = command(host, 'cherry-pick', feature)
    assert picked['state'] == 'applied' and (host.repo / 'feature.txt').read_text() == 'feature\n'
    git(host.repo, 'reset', '-q', '--hard', fix)
    git(host.repo, 'switch', '-q', 'feature')
    plan, rebased = command(host, 'rebase', 'main')
    assert plan['destructive'] is True and rebased['state'] == 'applied'
    assert git(host.repo, 'merge-base', 'feature', 'main').stdout.strip() == fix


def test_a_conflicting_revert_stops_and_is_finished_with_add_and_continue(host):
    changed = second_commit(host, text='print("one")\n', message='One')
    second_commit(host, text='print("two")\n', message='Two')
    _, stopped = command(host, 'revert', changed)
    assert stopped['state'] == 'applied' and stopped['result']['stopped'] == 'revert'
    assert stopped['result']['unmerged'] == ['app.py'] and '--continue' in stopped['result']['next']
    with pytest.raises(PolicyError, match='revert is stopped'):
        host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['tag', 'v2'])})
    (host.repo / 'app.py').write_text('print("base")\n')
    command(host, 'add', '--', 'app.py')
    _, done = command(host, 'revert', '--continue')
    assert done['state'] == 'applied' and done['result']['stopped'] is None
    assert git(host.repo, 'log', '-1', '--format=%s').stdout.startswith('Revert "One"')


def test_pull_fast_forwards_from_a_local_bare_remote(host, tmp_path):
    remote = bare(host)
    other = tmp_path / 'other'
    git(tmp_path, 'clone', '-q', str(remote), str(other))
    (other / 'app.py').write_text('print("upstream")\n')
    git(other, 'commit', '-q', '-am', 'Upstream change')
    git(other, 'push', '-q', 'origin', 'main')
    wip(host)
    _, pulled = command(host, 'pull', '--ff-only', 'origin', 'main')
    assert pulled['state'] == 'applied' and (host.repo / 'app.py').read_text() == 'print("upstream")\n'
    assert (host.repo / 'scratch.md').read_text() == 'ideas\n'


def test_hooks_never_run_for_a_command(host, tmp_path):
    marker = tmp_path / 'hook-ran'
    hook = host.repo / '.git' / 'hooks' / 'post-commit'
    hook.write_text(f'#!/bin/sh\ntouch {marker}\n')
    hook.chmod(0o755)
    changed = second_commit(host) if False else None
    (host.repo / 'app.py').write_text('print("x")\n')
    command(host, 'add', '--', 'app.py')
    _, committed = command(host, 'commit', '-m', 'Hook test')
    assert committed['state'] == 'applied' and not marker.exists() and changed is None


def test_a_relative_local_remote_as_the_golden_fixtures_add_it_is_resolved_from_the_repository(host):
    remote = host.tmp / 'remote.git'
    git(host.tmp, 'init', '-q', '--bare', str(remote))
    git(host.repo, 'remote', 'add', 'origin', '../remote.git')  # materialize.build_history's shape
    git(host.repo, 'switch', '-q', '-c', 'feature')
    second_commit(host)
    plan, pushed = apply(host, 'push', remote='origin', branch='feature')
    assert pushed['state'] == 'applied' and plan['bindings']['remote_path'] == str(remote)
    assert git(remote, 'rev-parse', 'feature').stdout.strip() == rev(host.repo)
    _, fetched = command(host, 'fetch', 'origin')
    assert fetched['state'] == 'applied' and rev(host.repo, 'origin/feature') == rev(host.repo)


def test_a_linked_worktree_is_made_beside_the_project_with_review_and_removed_again(host):
    plan, made = command(host, 'worktree', 'add', '-b', 'hotfix', '../project-hotfix', 'main')
    folder = host.tmp / 'project-hotfix'
    assert plan['destructive'] is True and plan['bindings']['folder'] == str(folder)
    assert any('outside the project' in effect for effect in plan['effects'])
    assert made['state'] == 'applied' and (folder / 'app.py').read_text() == 'print("base")\n'
    listed = git(host.repo, 'worktree', 'list', '--porcelain').stdout
    assert f'worktree {folder}\n' in listed and 'branch refs/heads/hotfix' in listed
    # Restated in 1.9.29 (w20-git): a worktree a plan made has an Undo that removes it and its new branch while both are
    # as the plan left them (test_git_review_1929); removing it with its own command stays as it was
    assert made['result']['undo'] == 'available' and made['result']['worktree']['branch'] == 'refs/heads/hotfix'
    _, removed = command(host, 'worktree', 'remove', '../project-hotfix')
    assert removed['state'] == 'applied' and not folder.exists()
    for argv in (['worktree', 'add', '../../elsewhere', 'main'], ['worktree', 'add', '.git/x', 'main'],
                 ['worktree', 'add', '../data', 'main']):
        with pytest.raises(PolicyError):
            host.plans.prepare(host.chat, 'command', {'argv': json.dumps(argv)})


# -- the security review's findings (2026-10-27), each pinned ------------------------------------------------------

def test_a_command_that_changes_the_ignore_rules_has_no_undo_and_copies_no_ignored_file(host):
    (host.repo / '.gitignore').write_text('local.cfg\n')
    git(host.repo, 'add', '.gitignore')
    git(host.repo, 'commit', '-q', '-m', 'Ignore local settings')
    secret = b'token = not-a-real-one-but-private\n'
    (host.repo / 'local.cfg').write_bytes(secret)
    _, moved = command(host, 'mv', '--', '.gitignore', 'gitignore.bak')
    assert moved['state'] == 'applied' and moved['result']['undo'] == 'unavailable'
    assert (host.repo / 'local.cfg').read_bytes() == secret
    assert git(host.repo, 'cat-file', '-e', git(host.repo, 'hash-object', 'local.cfg').stdout.strip(), check=False).returncode != 0
    with pytest.raises(PolicyError, match='no complete snapshot'):
        host.plans.prepare(host.chat, 'undo', {'receipt_id': moved['receipt_id']})


def test_checkout_of_a_name_that_is_no_branch_is_refused_instead_of_restoring_a_path(host):
    (host.repo / 'src').mkdir()
    (host.repo / 'src' / 'mod.py').write_text('x = 1\n')
    git(host.repo, 'add', 'src')
    git(host.repo, 'commit', '-q', '-m', 'Add src')
    (host.repo / 'src' / 'mod.py').write_text('x = 2  # unsaved work\n')
    with pytest.raises(PolicyError, match='does not name a commit'):
        host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['checkout', 'src'])})
    assert (host.repo / 'src' / 'mod.py').read_text() == 'x = 2  # unsaved work\n'
    assert parse(['checkout', 'main']).argv == ['checkout', 'main', '--']


def test_pseudo_refs_reflog_forms_and_unchecked_reset_paths_are_refused():
    # Restated in Phase 46 (1.9.28, w9-git): ORIG_HEAD and HEAD@{1} name one commit each and now run only as the
    # commit ID bound at review, with both shown on the card (the second opinion's bound for F3a; pinned in
    # test_git_routes_1928). FETCH_HEAD (several commits), MERGE_HEAD, dates and ^{/text} stay refused here.
    for argv in (['merge', 'FETCH_HEAD'], ['revert', 'REVERT_HEAD'], ['cherry-pick', 'MERGE_HEAD~1'],
                 ['merge', 'main@{1.hour.ago}'], ['reset', '--hard', 'HEAD@{-1}'], ['revert', 'HEAD^{/fix}'],
                 ['reset', 'HEAD', '/etc/passwd'], ['reset', 'HEAD', '.git/config'], ['reset', 'HEAD', '../x']):
        with pytest.raises(PolicyError):
            parse(argv)
    staged = parse(['reset', 'HEAD~3', 'src'])
    assert staged.argv == ['reset', 'HEAD~3', '--', 'src'] and 'HEAD~3' in staged.summary and staged.paths == ['src']


def test_what_runs_is_the_commit_bound_at_review_and_a_moved_name_is_drift(host):
    git(host.repo, 'switch', '-q', '-c', 'feature')
    second_commit(host, message='Feature change')
    picked = rev(host.repo)
    git(host.repo, 'switch', '-q', 'main')
    plan = host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['cherry-pick', 'feature'])})
    assert plan['bindings']['run'] == ['cherry-pick', picked]
    (host.repo / 'other.txt').write_text('moved\n')
    git(host.repo, 'switch', '-q', 'feature')
    git(host.repo, 'add', 'other.txt')
    git(host.repo, 'commit', '-q', '-m', 'Moves feature')
    git(host.repo, 'switch', '-q', 'main')
    with pytest.raises(PolicyError, match='changed after review'):
        host.plans.execute(host.chat, plan)


def test_merge_options_in_repository_config_are_refused(host):
    git(host.repo, 'config', 'branch.main.mergeOptions', '-s ours')
    with pytest.raises(PolicyError, match='mergeoptions'):
        host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['merge', 'main'])})


def test_positions_not_values_name_the_folder_and_the_remote(host):
    plan = host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['worktree', 'add', '-b', 'X', 'X', 'main'])})
    assert plan['bindings']['run'] == ['worktree', 'add', '-b', 'X', str(host.repo / 'X'), rev(host.repo)]
    bare(host)
    # the second "origin" is what to push (Phase 46: a branch push), never the remote again
    with pytest.raises(PolicyError, match='Branch origin does not exist locally'):
        host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['push', 'origin', 'origin'])})


def test_an_annotated_tag_holding_a_secret_is_not_pushed(host):
    bare(host)
    from test_git_plans import AWS
    git(host.repo, 'tag', '-a', 'v9', '-m', 'Release notes: key ' + AWS)
    with pytest.raises(PolicyError):
        host.plans.prepare(host.chat, 'command', {'argv': json.dumps(['push', 'origin', 'v9'])})


def test_an_edit_after_the_command_blocks_its_undo(host):
    changed = second_commit(host)
    (host.repo / 'notes.txt').write_text('one\ntwo\nthree\nfour\n')
    _, reverted = command(host, 'revert', changed)
    (host.repo / 'notes.txt').write_text('one\ntwo\nthree\nfour\nfive (later)\n')
    with pytest.raises(PolicyError, match='changed after this command'):
        host.plans.prepare(host.chat, 'undo', {'receipt_id': reverted['receipt_id']})
    assert (host.repo / 'notes.txt').read_text().endswith('five (later)\n')
