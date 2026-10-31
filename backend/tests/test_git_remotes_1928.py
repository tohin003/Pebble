"""Phase 46 (1.9.28, w9-git follow-up 2, D180): remotes, on real temporary repositories.

The person: "Do as how frontiers do for this or do better then them along with making the UX better and giving the user a
eaze of mind".

- `git remote`, `git remote -v` and `git remote get-url [--push] [--all] <name>` run as raw reads (RunCommand), with the
  credentials of any address hidden as ***.
- ["remote", "add" | "set-url" [--push] | "rename", ...] are reviewed commands: the card says in plain words where git
  fetch and git push go before and after, and Undo puts the previous setting back. An address with a password or token
  is refused with the credential-helper advice and never echoed; so are remote helpers, http:// and git://.
- Pushing to a network remote stays refused (transport_unsupported), and the card of a change to one says what that means.
"""

import json

import pytest

from pebble.git_commands import address, parse, read
from pebble.git_workflows import without_credentials
from pebble.policy import PolicyError
from test_git_plans import TOKEN, apply, bare, git, host, macos  # noqa: F401 (fixture)

SECRET = 'hunter2-pw'


def prepare(host, *argv):  # noqa: F811
    return host.plans.prepare(host.chat, 'command', {'argv': json.dumps(list(argv))})


def run(host, *argv):  # noqa: F811
    plan = prepare(host, *argv)
    return plan, host.plans.execute(host.chat, plan)


def undo(host, receipt):  # noqa: F811
    plan = host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})
    return plan, host.plans.execute(host.chat, plan)


def config(repo, key):
    return git(repo, 'config', '--get', key, check=False).stdout.strip() or None


# -- reads -----------------------------------------------------------------------------------------------------------

def test_remote_reads_are_raw_reads_and_changes_are_not():
    assert read('git remote') == ['remote'] and read('git remote -v') == ['remote', '-v']
    assert read('git remote get-url --push origin') == ['remote', 'get-url', '--push', 'origin']
    for command in ('git remote add up ../x.git', 'git remote set-url origin x', 'git remote show origin',
                    'git remote rename a b', 'git remote get-url', 'git remote remove origin', 'git remote prune origin'):
        assert read(command) is None, command  # the terminal refuses it and offers ProposeGitChange
    for argv in (['remote'], ['remote', '-v'], ['remote', 'get-url', 'origin']):
        with pytest.raises(PolicyError, match='reads; RunCommand runs "git remote -v"'):
            parse(argv)


@macos
def test_a_raw_remote_read_hides_the_credentials_in_every_address(host):  # noqa: F811
    git(host.repo, 'remote', 'add', 'origin', f'https://alice:{SECRET}@example.invalid/team/app.git')
    git(host.repo, 'remote', 'add', 'mirror', 'ssh://git@example.invalid/team/app.git')
    git(host.repo, 'remote', 'add', 'scp', 'git@example.invalid:team/app.git')
    seen = host.git.raw(host.chat, read('git remote -v'))
    assert SECRET not in json.dumps(seen) and 'alice' not in seen['output']
    assert seen['output'].count('https://***@example.invalid/team/app.git') == 2
    assert 'ssh://git@example.invalid/team/app.git' in seen['output'] and 'git@example.invalid:team/app.git' in seen['output']
    assert '2 address(es) carried a user name or password, shown as ***' in seen['note']
    assert 'remote", "set-url"' in seen['note']
    got = host.git.raw(host.chat, read('git remote get-url origin'))
    assert got['output'].strip() == 'https://***@example.invalid/team/app.git'
    assert without_credentials('see https://' + TOKEN + '@github.com/o/r and ssh://git@h/r') == (
        'see https://***@github.com/o/r and ssh://git@h/r', 1)


# -- what a reviewed change may set ------------------------------------------------------------------------------------

@pytest.mark.parametrize('value, match', [
    (f'https://alice:{SECRET}@github.com/o/r.git', 'carries a credential'),
    (f'https://{TOKEN}@github.com/o/r.git', 'carries a credential'),
    (f'ssh://git:{SECRET}@example.invalid/r.git', 'carries a credential'),
    (f'alice:{SECRET}@example.invalid:r.git', 'carries a credential'),
    (f'https://gitlab.example.invalid/r.git?private_token={SECRET}', 'carries a credential'),
    ('http://example.invalid/r.git', 'unencrypted'), ('git://example.invalid/r.git', 'unencrypted'),
    ('ext::sh -c touch% /tmp/pwned', 'can run programs'), ('fd::17', 'can run programs'),
    ('-oProxyCommand=evil', 'no leading'), (' ../x.git', 'no leading'), ('a\nb', 'no leading'),
])
def test_an_address_with_a_credential_a_helper_or_no_encryption_is_refused_and_never_echoed(value, match):
    with pytest.raises(PolicyError, match=match) as refused:
        address(value)
    assert SECRET not in str(refused.value) and TOKEN not in str(refused.value)
    if 'credential' in match:
        assert 'credential helper' in str(refused.value) and 'macOS Keychain' in str(refused.value)
    for good in ('https://github.com/o/r.git', 'git' + '@github.com:o/r.git', 'ssh://git@example.invalid:2222/r.git',
                 '../fork.git', '/srv/git/r.git', 'file:///srv/git/r.git'):  # built at run time: no literal address
        assert address(good) == good


# Restated in 1.9.29 (w20-git): remote prune left this list (reviewed: the remote-tracking branches its remote no longer
# has, bound at review, with Undo; test_git_review_1929); set-head, which reads the remote, takes its place.
@pytest.mark.parametrize('argv, match', [
    (['remote', 'remove', 'origin'], 'not a reviewed Git command'), (['remote', 'set-head', 'origin', '-a'], 'not a reviewed'),
    (['remote', 'add', '-f', 'up', '../x.git'], 'not available'), (['remote', 'add', '--mirror=push', 'up', 'x'], 'not available'),
    (['remote', 'set-url', '--add', 'origin', 'x'], 'not available'), (['remote', 'add', 'up'], 'takes a name and an address'),
    (['remote', 'add', 'a b', '../x.git'], 'remote name'), (['remote', 'rename', 'origin'], 'old name and the new one'),
])
def test_other_remote_commands_and_shapes_are_refused(argv, match):
    with pytest.raises(PolicyError, match=match):
        parse(argv)


# -- reviewed changes, in plain words, with Undo -----------------------------------------------------------------------

@macos
def test_add_says_where_fetch_and_push_go_and_undo_removes_it(host):  # noqa: F811
    bare(host)
    plan, result = run(host, 'remote', 'add', 'upstream', '../fork.git')
    # Phase 46 (1.9.28, the follow-ups security review 1, restated): the summary says where fetches and pushes go too
    assert plan['destructive'] and plan['requires_review'] and plan['summary'] == (
        f'Add the remote upstream: git fetch upstream and git push upstream go to the folder {host.tmp}/fork.git (not '
        'there yet) on this Mac.')
    card = '\n'.join(plan['effects'])
    assert 'Before: there is no remote named upstream, so git fetch upstream and git push upstream fail' in card
    assert f'After: git fetch upstream and git push upstream go to the folder {host.tmp}/fork.git (not there yet) on this Mac' in card
    assert 'Nothing is fetched or pushed now' in card and 'Undo removes the remote upstream again' in card
    assert result['state'] == 'applied' and config(host.repo, 'remote.upstream.url') == '../fork.git'
    assert config(host.repo, 'remote.upstream.fetch') == '+refs/heads/*:refs/remotes/upstream/*'
    _, done = undo(host, result)
    assert done['state'] == 'applied' and config(host.repo, 'remote.upstream.url') is None
    with pytest.raises(PolicyError, match='exists already'):
        prepare(host, 'remote', 'add', 'origin', '../other.git')
    with pytest.raises(PolicyError, match='looks like a token or key'):
        prepare(host, 'remote', 'add', 'up', f'https://github.com/{TOKEN}/r.git')


@macos
def test_set_url_to_a_network_address_says_the_push_is_the_persons_and_undo_puts_the_folder_back(host):  # noqa: F811
    remote = bare(host)
    plan, result = run(host, 'remote', 'set-url', 'origin', 'https://github.com/acme/app.git')
    card = '\n'.join(plan['effects'])
    assert f'Before: git fetch origin and git push origin go to the folder {remote} on this Mac' in card
    assert 'After: git fetch origin and git push origin go to github.com over https, repository acme/app.git' in card
    assert 'Pebble itself fetches and pushes only repositories in folders on this Mac, so a push to origin is yours ' \
           'to run: git push in your own terminal' in card and 'credential helper (the macOS Keychain)' in card
    assert config(host.repo, 'remote.origin.url') == 'https://github.com/acme/app.git'
    with pytest.raises(PolicyError, match='transport_unsupported'):  # pushing to a network remote stays as it was
        host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main'})
    _, done = undo(host, result)
    assert done['state'] == 'applied' and config(host.repo, 'remote.origin.url') == str(remote)
    # --push changes only where pushes go; its Undo removes the push address again
    plan, result = run(host, 'remote', 'set-url', '--push', 'origin', '../mirror.git')
    assert any(f'git fetch origin reads from the folder {remote} on this Mac, and git push origin goes to the folder '
               f'{host.tmp}/mirror.git (not there yet) on this Mac' in effect for effect in plan['effects'])
    undo(host, result)
    assert config(host.repo, 'remote.origin.pushurl') is None and config(host.repo, 'remote.origin.url') == str(remote)


@macos
def test_rename_says_which_branches_follow_and_undo_renames_back(host):  # noqa: F811
    bare(host)
    git(host.repo, 'fetch', '-q', 'origin')
    git(host.repo, 'branch', '-q', '--set-upstream-to', 'origin/main', 'main')
    plan, result = run(host, 'remote', 'rename', 'origin', 'upstream')
    card = '\n'.join(plan['effects'])
    assert 'After: the same addresses under the name upstream' in card and 'git fetch origin and git push origin stop working' in card
    assert 'Branches that follow origin follow upstream after: main' in card
    assert 'remote-tracking branch(es) origin/... become upstream/...' in card
    assert result['state'] == 'applied' and config(host.repo, 'branch.main.remote') == 'upstream'
    assert git(host.repo, 'rev-parse', '--verify', '-q', 'refs/remotes/upstream/main', check=False).returncode == 0
    _, done = undo(host, result)
    assert done['state'] == 'applied' and config(host.repo, 'branch.main.remote') == 'origin'


@macos
def test_undo_is_blocked_when_the_remote_changed_after_and_a_credential_address_is_fixed_without_echo(host):  # noqa: F811
    bare(host)
    _, result = run(host, 'remote', 'set-url', 'origin', '../moved.git')
    git(host.repo, 'remote', 'set-url', 'origin', '../elsewhere.git')  # changed outside Pebble
    with pytest.raises(PolicyError, match='changed after this command'):
        undo(host, result)
    git(host.repo, 'remote', 'set-url', 'origin', f'https://alice:{SECRET}@example.invalid/r.git')
    plan = prepare(host, 'remote', 'set-url', 'origin', 'https://example.invalid/r.git')
    assert SECRET not in json.dumps(plan) and 'alice' not in json.dumps(plan['effects'])
    assert any('Before: git fetch origin and git push origin go to example.invalid over https, repository r.git' in e
               for e in plan['effects'])
    assert any('the address it replaces held one, shown as ***; after this, .git/config no longer does' in e
               for e in plan['effects'])
    assert 'No Undo: the address it replaces held a credential' in plan['effects'][-1]
    result = host.plans.execute(host.chat, plan)
    assert result['state'] == 'applied' and SECRET not in json.dumps(result)
    assert config(host.repo, 'remote.origin.url') == 'https://example.invalid/r.git'
    with pytest.raises(PolicyError, match='neither keeps nor writes back'):
        undo(host, result)
