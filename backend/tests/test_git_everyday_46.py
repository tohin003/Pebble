"""Phase 46 (w1-git): the everyday Git operations a person asks for that reviewed Git commands still could not do, on
real temporary repositories and local bare remotes (test_git_plans' sandboxed fixtures).

Measured first (offline, 1.9.26 plus the refs view and spellings already in the checkout): a branch push spelled as a
command ("push -u origin feature", "push --force-with-lease ...", "push origin --delete old") was refused as "not a
tag"; the typed push refused any non-fast-forward with "integrate them outside Pebble" and set no upstream and no
remote-tracking branch; config, clean and bisect were outside reviewed commands; raw `git status` / `git log` in the
terminal were refused. Forced pushes, remote branch deletion, config user.* and clean -f are the person's decision
(D159): they run only where the person's `git_decisions` setting turns them on; all on by default since D174, so
each test turns its switch off first to show the refusal.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import runner, settings_file
from pebble.git_commands import parse, read
from pebble.policy import PolicyError
from test_git_commands_46 import command, second_commit, wip
from test_git_plans import apply, bare, git, host, macos, remote_tip, rev  # noqa: F401 (fixture)


def prepare(host, *argv):  # noqa: F811
    return host.plans.prepare(host.chat, 'command', {'argv': json.dumps(list(argv))})


def decide(host, **on):  # noqa: F811
    host.app.store.set_setting('git_decisions', {**settings_file.default('git_decisions'), **on})


# -- the parser ------------------------------------------------------------------------------------------------------

def test_push_forms_name_one_branch_and_carry_force_lease_delete_and_upstream():
    off = {'force': False, 'lease': None, 'delete': False, 'upstream': False}
    for argv, push, names in (
            (['push', '-u', 'origin', 'feature'], {**off, 'upstream': True}, ['feature']),
            (['push', '--set-upstream', 'origin', 'HEAD'], {**off, 'upstream': True}, ['HEAD']),
            (['push', '--force-with-lease', 'origin', 'feature'], {**off, 'force': True}, ['feature']),
            (['push', '--force-with-lease=feature:abc1234', 'origin', 'feature'],
             {**off, 'force': True, 'lease': 'feature:abc1234'}, ['feature']),
            (['push', '-f', 'origin', 'feature'], {**off, 'force': True}, ['feature']),
            (['push', 'origin', '+feature'], {**off, 'force': True}, ['feature']),
            (['push', 'origin', '--delete', 'old'], {**off, 'delete': True}, ['old']),
            (['push', '-d', 'origin', 'old'], {**off, 'delete': True}, ['old']),
            (['push', 'origin', ':old'], {**off, 'delete': True}, ['old']),
            (['push', 'origin', 'refs/heads/a:refs/heads/b'], off, ['a:b']),
            (['push'], off, [])):
        parsed = parse(argv)
        assert (parsed.push, parsed.names, parsed.publication) == (push, names, True), argv
        assert parsed.destructive is (push['force'] or push['delete']), argv
    for argv in (['push', '--mirror', 'origin'], ['push', '--prune', 'origin', 'x'], ['push', 'origin', 'a:b:c'],
                 ['push', '--delete', '--force', 'origin', 'x'], ['push', '-u', 'origin', ':x'],
                 ['push', '--force-with-lease=', 'origin', 'x'], ['push', 'origin', '../x']):
        with pytest.raises(PolicyError):
            parse(argv)


def test_config_clean_and_bisect_parse_only_their_safe_forms():
    assert parse(['config', 'user.name', 'Ada Lovelace']).argv == ['config', '--local', 'user.name', 'Ada Lovelace']
    assert parse(['config', '--local', 'User.Email', 'ada@example.com']).argv == ['config', '--local', 'user.email',
                                                                                   'ada@example.com']
    assert parse(['config', '--unset', 'user.email']).argv == ['config', '--local', '--unset', 'user.email']
    for argv in (['config', '--global', 'user.name', 'x'], ['config', 'core.hooksPath', '/tmp/x'], ['config', 'user.name'],
                 ['config', 'user.name', '<x>'], ['config', '--file', '/tmp/c', 'user.name', 'x'],
                 ['config', 'alias.st', '!sh'], ['config', '--unset', 'user.name', 'x']):
        with pytest.raises(PolicyError):
            parse(argv)
    cleaned = parse(['clean', '-fdx'])
    assert cleaned.argv == ['clean', '-f', '-d', '-x'] and cleaned.destructive and cleaned.worktree
    listed = parse(['clean', '-nd'])
    assert listed.argv == ['clean', '-n', '-d'] and not listed.destructive
    assert parse(['clean', '-f', '--', 'build']).paths == ['build']
    for argv in (['clean', '-d'], ['clean', '-i'], ['clean', '-ffd'], ['clean', '-f', '-x', '-X'], ['clean', '-f', '--', '.env']):
        with pytest.raises(PolicyError):
            parse(argv)
    assert parse(['bisect', 'start', 'HEAD', 'v1.0']).argv == ['bisect', 'start', 'HEAD', 'v1.0']
    assert parse(['bisect', 'bad']).midway and parse(['bisect', 'good', 'abc1234']).revisions == ['abc1234']
    assert parse(['bisect', 'reset']).argv == ['bisect', 'reset']
    for argv in (['bisect', 'run', 'make', 'test'], ['bisect', 'replay', 'log'], ['bisect', 'visualize'], ['bisect'],
                 ['bisect', 'reset', 'main'], ['bisect', 'bad', 'a', 'b'], ['bisect', 'start', '--no-checkout']):
        with pytest.raises(PolicyError):
            parse(argv)


# -- read-only Git typed in the terminal -----------------------------------------------------------------------------

@pytest.fixture
def raw_reads(monkeypatch):
    """Read-only Git in the project terminal (on by default since D173); pinned on whatever the environment says."""
    monkeypatch.setenv('PEBBLE_RAW_GIT_READS', '1')


@pytest.mark.usefixtures('raw_reads')
@pytest.mark.parametrize('text, argv', [
    ('git status', ['status']),
    ('git --no-pager log --oneline -n 5', ['log', '--oneline', '-n', '5']),
    ('git branch -a --contains abc1234', ['branch', '-a', '--contains', 'abc1234']),
    ('git branch -vv', ['branch', '-vv']),
    ('git tag -l "v1.*"', ['tag', '-l', 'v1.*']),
    ('git tag', ['tag']),
    ('git stash list', ['stash', 'list']),
    ('git show v1.2 --stat', ['show', 'v1.2', '--stat']),
    ('git blame -- app/money.py', ['blame', '--', 'app/money.py']),
    ('git log origin/main..HEAD', ['log', 'origin/main..HEAD']),
    ('git rev-parse --abbrev-ref HEAD', ['rev-parse', '--abbrev-ref', 'HEAD']),
    ('git for-each-ref --format="%(refname:short)" refs/heads', ['for-each-ref', '--format=%(refname:short)', 'refs/heads']),
])
def test_read_only_git_typed_in_the_terminal_is_admitted(text, argv):
    assert read(text) == argv


# Restated in 1.9.29 (w20-git): `git cat-file -p HEAD` left this list; it prints an object as git show does, so it is a
# content read behind the same private-history check (test_git_review_1929). `git cat-file --batch` takes its place.
@pytest.mark.parametrize('text', ['git commit -m x', 'git branch feature', 'git branch -D old', 'git tag v2', 'git stash',
                                  'git -C .. status', 'git -c core.pager=less log', 'git reflog expire --all',
                                  'git cat-file --batch', 'git push origin main', 'ls -la', 'git status | head', 'gitk',
                                  'git', 'git log $(echo x)'])
def test_anything_else_is_left_to_the_terminal_refusal(text):
    assert read(text) is None


@pytest.mark.parametrize('text', ['git show HEAD --output=/tmp/x', 'git diff --no-index a b', 'git log --ext-diff -p',
                                  'git show HEAD:.env', 'git log -p -- .env', 'git grep -f patterns.txt',
                                  'git blame --contents notes.txt app.py', 'git log -O order.txt', 'git show /etc/passwd',
                                  'git diff ../other'])
@pytest.mark.usefixtures('raw_reads')
def test_reads_with_options_or_paths_pebble_does_not_run_are_refused(text):
    with pytest.raises(PolicyError):
        read(text)


def test_raw_reads_are_on_by_default_and_turn_off_with_their_switch(monkeypatch):
    # D173 (the person, 2026-10-28): read-only Git in the project terminal runs without asking, as in Codex and Claude Code
    monkeypatch.delenv('PEBBLE_RAW_GIT_READS', raising=False)
    assert read('git status') == ['status']
    monkeypatch.setenv('PEBBLE_RAW_GIT_READS', '0')
    assert read('git status') is None


@macos
def test_a_raw_read_runs_confined_and_read_only_and_keeps_private_contents_out(host):  # noqa: F811
    shown = host.git.raw(host.chat, ['log', '--oneline', '-n', '1'])
    assert shown['exit_code'] == 0 and 'Initial fixture commit' in shown['output'] and shown['trust']
    assert 'main' in host.git.raw(host.chat, ['branch', '-a'])['output']
    assert 'print("base")' in host.git.raw(host.chat, ['show', 'HEAD:app.py'])['output']
    # the sandbox is the second line: even a write admitted by mistake cannot change the repository
    refused = host.git.raw(host.chat, ['tag', 'sneaky'])
    assert refused['exit_code'] != 0 and git(host.repo, 'tag').stdout == ''
    host.git.raw(host.chat, ['log', '-1', '--output=out.txt'])
    assert not (host.repo / 'out.txt').exists()
    (host.repo / '.env.example').write_text('TOKEN=placeholder\n')
    git(host.repo, 'add', '.env.example')
    git(host.repo, 'commit', '-q', '-m', 'Add an env example')
    with pytest.raises(PolicyError, match='private files'):
        host.git.raw(host.chat, ['show', 'HEAD'])
    assert 'Add an env example' in host.git.raw(host.chat, ['log', '--oneline'])['output']  # names only still run


# -- pushes: upstream, forced with a lease, deletion -----------------------------------------------------------------

@macos
def test_a_new_branch_is_pushed_with_its_upstream_and_remote_tracking_branch(host):  # noqa: F811
    remote = bare(host)
    git(host.repo, 'switch', '-q', '-c', 'feature')
    tip = second_commit(host, message='Feature work')
    wip(host)
    plan, pushed = command(host, 'push', '-u', 'origin', 'feature')
    assert plan['operation'] == 'push' and plan['publication'] is True and plan['destructive'] is False
    assert plan['bindings']['upstream'] == {'branch': 'feature', 'remote': 'origin', 'merge': 'refs/heads/feature'}
    assert pushed['state'] == 'applied' and remote_tip(remote, 'feature') == tip
    assert rev(host.repo, 'origin/feature') == tip
    assert git(host.repo, 'rev-parse', '--abbrev-ref', 'feature@{upstream}').stdout.strip() == 'origin/feature'
    # Undo takes the branch off the remote and drops its remote-tracking branch; the upstream setting stays
    _, undone = apply(host, 'undo', receipt_id=pushed['receipt_id'])
    assert undone['state'] == 'applied' and remote_tip(remote, 'feature') is None
    assert git(host.repo, 'rev-parse', '--verify', '-q', 'origin/feature', check=False).returncode != 0
    # a later plain push goes to the recorded upstream
    plan = prepare(host, 'push')
    assert (plan['operation'], plan['remote'], plan['branch'], plan['remote_branch']) == ('push', 'origin', 'feature',
                                                                                         'feature')


@macos
def test_a_rebased_branch_is_force_pushed_only_when_turned_on_and_only_over_what_was_seen(host):  # noqa: F811
    remote = bare(host)
    git(host.repo, 'switch', '-q', '-c', 'feature')
    (host.repo / 'feature.txt').write_text('feature\n')
    git(host.repo, 'add', 'feature.txt')
    git(host.repo, 'commit', '-q', '-m', 'Add feature')
    git(host.repo, 'push', '-q', 'origin', 'feature')  # the remote and origin/feature have the branch as it was
    old = rev(host.repo)
    git(host.repo, 'switch', '-q', 'main')
    fix = second_commit(host, message='Fix on main')
    git(host.repo, 'switch', '-q', 'feature')
    command(host, 'rebase', 'main')
    new = rev(host.repo)
    with pytest.raises(PolicyError, match='non_fast_forward'):
        prepare(host, 'push', 'origin', 'feature')
    decide(host, force_push=False)  # D174: on by default; off here to show the refusal
    with pytest.raises(PolicyError, match="person's decision"):
        prepare(host, 'push', '--force-with-lease', 'origin', 'feature')
    decide(host, force_push=True)
    with pytest.raises(PolicyError, match='lease expects'):
        prepare(host, 'push', f'--force-with-lease=feature:{fix}', 'origin', 'feature')
    plan = prepare(host, 'push', '--force-with-lease', 'origin', 'feature')
    b = plan['bindings']
    assert plan['operation'] == 'push' and plan['destructive'] is True and plan['requires_review'] is True
    assert (b['old_oid'], b['new_oid'], b['force'], b['dropped']) == (old, new, True, 1)
    pushed = host.plans.execute(host.chat, plan)
    assert pushed['state'] == 'applied' and remote_tip(remote, 'feature') == new and rev(host.repo, 'origin/feature') == new
    _, undone = apply(host, 'undo', receipt_id=pushed['receipt_id'])
    assert undone['state'] == 'applied' and remote_tip(remote, 'feature') == old and rev(host.repo, 'origin/feature') == old
    # someone else pushes after review: the lease holds; and what nobody here fetched is never dropped
    plan = prepare(host, 'push', '--force-with-lease', 'origin', 'feature')
    other = host.tmp / 'other'
    git(host.tmp, 'clone', '-q', str(remote), str(other))
    git(other, 'switch', '-q', 'feature')
    (other / 'theirs.txt').write_text('theirs\n')
    git(other, 'add', 'theirs.txt')
    git(other, 'commit', '-q', '-m', 'Theirs')
    git(other, 'push', '-q', 'origin', 'feature')
    theirs = remote_tip(remote, 'feature')
    with pytest.raises(PolicyError, match='changed after review'):
        host.plans.execute(host.chat, plan)
    with pytest.raises(PolicyError, match='never fetched'):
        prepare(host, 'push', '--force-with-lease', 'origin', 'feature')
    assert remote_tip(remote, 'feature') == theirs


@macos
def test_a_remote_branch_is_deleted_only_when_turned_on_never_the_default_and_undo_recreates_it(host):  # noqa: F811
    remote = bare(host)
    git(host.repo, 'branch', 'old-feature')
    git(host.repo, 'push', '-q', 'origin', 'old-feature')
    tip = rev(host.repo, 'old-feature')
    decide(host, delete_remote_branch=False)  # D174: on by default; off here to show the refusal
    with pytest.raises(PolicyError, match="person's decision"):
        prepare(host, 'push', 'origin', '--delete', 'old-feature')
    decide(host, delete_remote_branch=True)
    with pytest.raises(PolicyError, match='default branch'):
        prepare(host, 'push', 'origin', ':main')
    plan, deleted = command(host, 'push', 'origin', '--delete', 'old-feature')
    assert plan['operation'] == 'push' and plan['destructive'] is True and plan['bindings']['new_oid'] is None
    assert deleted['state'] == 'applied' and remote_tip(remote, 'old-feature') is None
    assert git(host.repo, 'rev-parse', '--verify', '-q', 'origin/old-feature', check=False).returncode != 0
    assert rev(host.repo, 'old-feature') == tip  # the local branch stays
    _, undone = apply(host, 'undo', receipt_id=deleted['receipt_id'])
    assert undone['state'] == 'applied' and remote_tip(remote, 'old-feature') == tip
    assert rev(host.repo, 'origin/old-feature') == tip


# -- config, clean, bisect -------------------------------------------------------------------------------------------

@macos
def test_the_repository_identity_is_set_only_when_turned_on_and_undo_puts_it_back(host):  # noqa: F811
    decide(host, repository_identity=False)  # D174: on by default; off here to show the refusal
    with pytest.raises(PolicyError, match="person's decision"):
        prepare(host, 'config', 'user.name', 'Ada Lovelace')
    decide(host, repository_identity=True)
    plan, done = command(host, 'config', 'user.name', 'Ada Lovelace')
    assert plan['bindings']['config'] == {'key': 'user.name', 'before': 'Reviewed Author'}
    assert any('(now "Reviewed Author")' in effect for effect in plan['effects'])
    assert done['state'] == 'applied' and done['result']['config']['after'] == 'Ada Lovelace'
    assert git(host.repo, 'config', '--local', 'user.name').stdout.strip() == 'Ada Lovelace'
    _, undone = apply(host, 'undo', receipt_id=done['receipt_id'])
    assert undone['state'] == 'applied' and git(host.repo, 'config', '--local', 'user.name').stdout.strip() == 'Reviewed Author'


@macos
def test_clean_lists_the_exact_files_first_and_removes_only_those_when_turned_on(host):  # noqa: F811
    (host.repo / '.gitignore').write_text('build/\n')
    git(host.repo, 'add', '.gitignore')
    git(host.repo, 'commit', '-q', '-m', 'Ignore build output')
    wip(host)
    (host.repo / 'build').mkdir()
    (host.repo / 'build' / 'app.bin').write_bytes(b'\x00binary')
    (host.repo / 'build' / 'log.txt').write_text('log\n')
    (host.repo / 'tmp.out').write_text('stray\n')
    listed = prepare(host, 'clean', '-n', '-d')
    assert sorted(listed['bindings']['clean']['paths']) == ['scratch.md', 'tmp.out'] and listed['destructive'] is False
    decide(host, clean=False)  # D174: on by default; off here to show the refusal
    with pytest.raises(PolicyError, match="person's decision"):
        prepare(host, 'clean', '-fdX')
    decide(host, clean=True)
    plan, cleaned = command(host, 'clean', '-f', '-d', '-X')
    assert plan['bindings']['clean']['paths'] == ['build/'] and plan['bindings']['run'] == ['clean', '-f', '-d', '-X', '--',
                                                                                           'build/']
    assert any('build/ (2 files)' in effect for effect in plan['effects'])
    assert cleaned['state'] == 'applied' and not (host.repo / 'build').exists()
    assert (host.repo / 'scratch.md').read_text() == 'ideas\n' and (host.repo / 'tmp.out').exists()
    _, undone = apply(host, 'undo', receipt_id=cleaned['receipt_id'])
    assert undone['state'] == 'applied' and (host.repo / 'build' / 'app.bin').read_bytes() == b'\x00binary'
    (host.repo / '.env.local').write_text('TOKEN=1\n')
    with pytest.raises(PolicyError, match='private files'):
        prepare(host, 'clean', '-f')


@macos
def test_bisect_is_a_reviewed_sequence_that_finds_the_first_bad_commit(host):  # noqa: F811
    good = rev(host.repo)
    commits = [second_commit(host, text=f'print({n})\n', message=f'Step {n}') for n in range(1, 6)]
    _, started = command(host, 'bisect', 'start', 'HEAD', good)
    assert started['result']['stopped'] == 'bisect' and 'bisect' in started['result']['next']
    with pytest.raises(PolicyError, match='bisect is in progress'):
        prepare(host, 'tag', 'v9')
    marked = started
    for _ in range(5):
        value = int((host.repo / 'app.py').read_text().strip()[6:-1])
        _, marked = command(host, 'bisect', 'bad' if value >= 3 else 'good')
        if 'is the first bad commit' in marked['result']['output']:
            break
    assert commits[2] in marked['result']['output'] and marked['result']['undo'] == 'unavailable'
    _, reset = command(host, 'bisect', 'reset')
    assert reset['state'] == 'applied' and git(host.repo, 'symbolic-ref', 'HEAD').stdout.strip() == 'refs/heads/main'
    assert rev(host.repo) == commits[-1]
    with pytest.raises(PolicyError, match='No bisect'):
        prepare(host, 'bisect', 'good')


# -- the setting, the words, the offer -------------------------------------------------------------------------------

def test_git_decisions_is_a_person_setting_all_on_that_widens_only_with_confirmation(tmp_path):
    # D174 (the person, 2026-10-28): all on by default, so runs that need them do not halt; turning one off is free,
    # turning one back on is a widening that waits for the person's confirmation
    default = settings_file.default('git_decisions')
    assert default == {name: True for name in settings_file.GIT_DECISIONS} and 'git_decisions' in settings_file.KEYS
    off = {**default, 'force_push': False}
    on = {**off, 'force_push': True}
    assert settings_file.widening('git_decisions', off, on) == ['the Git decision "force_push" turns on']
    assert settings_file.widening('git_decisions', on, off) == []
    assert set(settings_file.schema()['properties']['git_decisions']['required']) == set(settings_file.GIT_DECISIONS)
    for bad in ({}, {'force_push': 'yes'}, {**on, 'push_everything': True}, None, True):
        with pytest.raises(ValueError):
            settings_file.validate('git_decisions', bad)
    from pebble.app import App
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        assert app.state()['settings']['git_decisions'] == default
        assert app.dispatch('setting', {'key': 'git_decisions', 'value': off}) is True
        assert app.state()['settings']['git_decisions'] == off
        assert app.dispatch('setting', {'key': 'git_decisions', 'value': on}) is True
        assert app.state()['settings']['git_decisions'] == on
        with pytest.raises(ValueError):
            app.dispatch('setting', {'key': 'git_decisions', 'value': {'force_push': True}})
    finally:
        app.close()


@pytest.mark.parametrize('text, offered', [
    ('Remove the untracked build output.', True), ('Set user.name and user.email for this repository.', True),
    ('Find which of the last commits broke the test.', True), ('Clean up the merged branches.', True),
    ('Use bisect to find the regression.', True), ('Was the fix pushed?', True),
    ('Review my uncommitted changes and point out any bugs.', False), ('Rename the helper function.', False)])
def test_everyday_git_words_offer_the_git_tools(text, offered):
    assert bool(runner.EVERYDAY_GIT.search(text)) is offered


def test_a_refused_git_write_offers_the_reviewed_git_tools_from_the_next_step():
    from pebble.app import App
    run = SimpleNamespace(active_run='run-1')
    App.offer_git_tools(run, 'git commit -m "Fix it"')
    assert {'ProposeGitChange', 'ReadGitPlanReceipt', 'ReadGitStatus'} <= run.found_tools['run-1']
    other = SimpleNamespace(active_run='run-2')
    App.offer_git_tools(other, 'python3 -m unittest')
    assert not getattr(other, 'found_tools', {}).get('run-2')


@macos
@pytest.mark.usefixtures('raw_reads')
def test_the_terminal_runs_git_reads_confined_and_a_refused_write_offers_reviewed_git(tmp_path):
    from test_tool_offering_24 import LARGE, names, requests_for

    def prepare_repo(app, cid):
        root = Path(app.chat(cid)['workspace'])
        git(root, 'init', '-q')
        git(root, 'add', '-A')
        git(root, 'commit', '-q', '-m', 'Initial calc')

    def call(name, args, ident):
        return {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}

    def replies(step, messages, tools):
        if step == 1:
            return call('RunCommand', {'command': 'git log --oneline -n 1'}, 'call-1')
        if step == 2:
            # Restated in 1.9.29 (w20-git): a raw write the reviewed command operation takes now runs as that operation
            # (test_git_review_1929); one it does not take is still refused and still offers the reviewed tools
            return call('RunCommand', {'command': 'git notes add -m "Nothing"'}, 'call-2')
        return {'role': 'assistant', 'content': 'Done.\nFINAL: done'}

    seen = requests_for(tmp_path, budget=LARGE, text='Show me the last change.', project=True, prepare=prepare_repo,
                        replies=replies)
    assert 'Initial calc' in json.dumps(seen[1][0])
    assert 'ProposeGitChange' not in names(seen[1][1]) and 'ProposeGitChange' in names(seen[2][1])


# -- the lead's review of w1-git's patch (an independent security review, 2026-10-28: no critical or high finding) -------

def test_raw_reads_that_list_names_leave_private_paths_out():
    # F1: ReadGitStatus leaves private paths out; a raw `git status` or `--stat` must not show their names either
    from pebble.git_workflows import without_private_names
    text = ('?? .env\n M app.py\n?? secrets/\n .env.local | 2 +-\n README.md | 1 +\n{old.txt => .env}\n'
            '    Rotate the logs\n 3 files changed')
    kept, withheld = without_private_names(text)
    assert withheld == 4 and kept == ' M app.py\n README.md | 1 +\n    Rotate the logs\n 3 files changed'
    assert without_private_names('nothing to commit, working tree clean') == ('nothing to commit, working tree clean', 0)


@pytest.mark.usefixtures('raw_reads')
@pytest.mark.parametrize('text', ['git show HEAD:../../etc/passwd', 'git show :../x', 'git log -- a/../../x',
                                  'git show HEAD:/etc/hosts', 'git show HEAD:~/notes'])
def test_a_path_after_a_colon_or_through_dot_dot_stays_in_the_project(text):
    # F2: Git refused these and the sandbox reads only the repository; this is the third layer
    with pytest.raises(PolicyError, match='stay in the selected project'):
        read(text)


@pytest.mark.usefixtures('raw_reads')
def test_help_and_revision_ranges(monkeypatch):
    # F3: --help opens Git's help viewer, so it is left to the terminal's refusal; ".." in a range is no path
    assert read('git log --help') is None and read('git status --help') is None
    assert read('git log main..feature') == ['log', 'main..feature']
    assert read('git diff HEAD~2..HEAD -- src') == ['diff', 'HEAD~2..HEAD', '--', 'src']


def test_bound_paths_after_dashes_stay_literal_explicitly():
    # F4: a reviewed clean runs its bound paths after "--" with literal pathspecs set, not inherited
    from pebble.git_plans import GitPlans
    assert GitPlans._command_env({'run': ['clean', '-f', '--', 'build/out.txt']})['GIT_LITERAL_PATHSPECS'] == '1'
    assert GitPlans._command_env({'run': ['stash', 'push', '--include-untracked']})['GIT_LITERAL_PATHSPECS'] == '0'
