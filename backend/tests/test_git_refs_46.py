"""Phase 46: the Git read tools show the refs a task names and read history by a branch, tag, remote-tracking branch,
stash entry or HEAD~N.

Measured first (offline, on 1.9.26): ReadGitStatus showed HEAD, the branch and changed paths but no branch, tag,
remote-tracking branch or stash entry; ReadGitHistory and ReadGitDiff took only an exact 40-hex commit; and the reviewed
Git commands send `git branch -a`, `git stash list`, `git rev-parse` and `git log origin/main` to those read tools as
reads. So a task about another branch, a tag, origin/main or an existing stash had no way to find its commit. On the
golden held-out git_workflow family 1.9.26 ended 6 cells (sol) and 3 (Opus) honest_incomplete, where Codex and Claude
Code passed 15 of 15.
"""

import pytest

from test_git_workflows import git, head, host, macos  # noqa: F401  (host is the shared fixture)


@macos
def test_status_lists_branches_tags_remote_branches_and_stashes(host):  # noqa: F811
    base = head(host.repo)
    git(host.repo, 'branch', 'release/1.x')
    git(host.repo, 'tag', '-a', 'v1.0', '-m', 'Release 1.0')
    git(host.repo, 'update-ref', 'refs/remotes/origin/main', base)
    (host.repo / 'app.py').write_text('print("work in progress")\n')
    git(host.repo, 'stash', 'push', '-m', 'login fix')
    refs = host.git.tool(host.chat, 'ReadGitStatus', {})['refs']
    assert {b['name'] for b in refs['branches']} >= {'release/1.x'} and all(b['commit'] for b in refs['branches'])
    git(host.repo, 'tag', 'v0.9', base)
    refs = host.git.tool(host.chat, 'ReadGitStatus', {})['refs']
    # an annotated tag shows the commit it tags, not its own tag object (as the second opinion's patch 05 does)
    assert refs['tags'] == [{'name': 'v0.9', 'commit': base, 'annotated': False},
                            {'name': 'v1.0', 'commit': base, 'annotated': True}]
    assert refs['remote_branches'] == [{'name': 'origin/main', 'commit': base}]
    assert [(s['entry'], s['message'].endswith('login fix')) for s in refs['stashes']] == [('stash@{0}', True)]
    assert refs['refs_truncated'] is False


@macos
def test_history_and_diff_by_name_and_never_an_option_or_range(host):  # noqa: F811
    base = head(host.repo)
    git(host.repo, 'switch', '-q', '-c', 'feature')
    (host.repo / 'app.py').write_text('print("feature")\n')
    git(host.repo, 'commit', '-q', '-am', 'Feature change')
    feature = head(host.repo)
    git(host.repo, 'switch', '-q', '-')
    assert [c['commit'] for c in host.git.tool(host.chat, 'ReadGitHistory', {'commit': 'feature', 'limit': 2})['commits']] \
        == [feature, base]
    assert host.git.tool(host.chat, 'ReadGitDiff', {'scope': 'commit', 'commit': 'feature'})['files'] == ['app.py']
    (host.repo / 'app.py').write_text('print("stashed")\n')
    git(host.repo, 'stash', 'push', '-m', 'wip')
    stashed = host.git.tool(host.chat, 'ReadGitHistory', {'commit': 'stash@{0}', 'limit': 1})['commits'][0]
    assert stashed['subject'].endswith('wip')
    from pebble.policy import PolicyError
    for bad in ('--all', '-p', 'feature..HEAD', 'no-such-branch', 'stash@{9}', 'a' * 201):
        with pytest.raises(PolicyError):
            host.git.tool(host.chat, 'ReadGitHistory', {'commit': bad})


def test_named_revisions_are_described_above_8k_and_the_small_requests_are_unchanged(tmp_path):
    """The first wording of this change was in the catalogue itself, so the 4K/8K project-git requests changed bytes
    (test_tool_offering_24's golden fixtures, validator four_k_requests_unchanged); now only requests above 8K carry it."""
    from pebble.git_workflows import NAMED_REVISIONS, TOOLS
    from test_tool_offering_24 import LARGE, first

    def described(budget, folder):
        tools = first(tmp_path / folder, budget=budget, text='Show the git diff for calc.py.', project=True)[1]
        return {t['function']['name']: t['function']['description'] for t in tools if t['function']['name'] in NAMED_REVISIONS}

    above = described(LARGE, 'large')
    assert above and above == {name: NAMED_REVISIONS[name] for name in above}
    plain = {t['function']['name']: t['function']['description'] for t in TOOLS}
    small = described(8192, '8k')
    assert small and small == {name: plain[name] for name in small}
    assert not any('stash@{N}' in text for text in small.values())
