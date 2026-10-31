"""Phase 46 (1.9.28, the final dev check's d-gw-135): ReadGitStatus shows where each remote's fetch and push go.

Measured first (the final dev check, Opus, 2026-10-29 at about 00:06): "Where do `git fetch origin` and `git push origin`
go in this repository? Don't change anything." ended `FINAL: blocked` in all three phrasings. A read-only request is
offered only the read tools, Read refuses .git/config, and ReadGitStatus named the upstream (origin/main) but no address,
so Pebble had no way to see it. Codex and Claude Code run `git remote -v`. The addresses show as `git remote -v` shows
them, with user names and passwords as *** (D180's rule for remote output).
"""

from test_git_workflows import git, host, macos  # noqa: F401  (host is the shared fixture)


@macos
def test_status_shows_each_remotes_fetch_and_push_address(host):  # noqa: F811
    git(host.repo, 'remote', 'add', 'origin', 'https://example.org/owner/ledger.git')
    git(host.repo, 'remote', 'add', 'fork', 'git@example.org:me/ledger.git')
    git(host.repo, 'remote', 'set-url', '--push', 'fork', 'ssh://git@example.org/me/ledger-push.git')
    status = host.git.tool(host.chat, 'ReadGitStatus', {})
    assert status['remotes'] == [
        {'name': 'fork', 'fetch': 'git@example.org:me/ledger.git', 'push': 'ssh://git@example.org/me/ledger-push.git'},
        {'name': 'origin', 'fetch': 'https://example.org/owner/ledger.git', 'push': 'https://example.org/owner/ledger.git'}]


@macos
def test_a_password_or_token_in_a_remote_address_is_never_shown(host):  # noqa: F811
    secret = 'tok' + 'en-for-a-test-only'
    git(host.repo, 'remote', 'add', 'origin', f'https://me:{secret}@example.org/owner/ledger.git')
    status = host.git.tool(host.chat, 'ReadGitStatus', {})
    assert status['remotes'] == [{'name': 'origin', 'fetch': 'https://***@example.org/owner/ledger.git',
                                  'push': 'https://***@example.org/owner/ledger.git'}]
    assert secret not in repr(status)


@macos
def test_a_repository_without_remotes_shows_none(host):  # noqa: F811
    assert host.git.tool(host.chat, 'ReadGitStatus', {})['remotes'] == []
