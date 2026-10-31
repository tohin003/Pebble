"""Phase 46 (1.9.28, w9-git follow-up 2, D179): private and credential files in Git, on real temporary repositories.

The person: "this should be like the frontiers like how they handle this kind of files by there own and they never upload
such risky and high security files to git, i want the same or better level than the frontiers".

- ["rm", "--cached", "--", <private files>] is a reviewed untrack plan: the index entries go without the file being read
  (it stays on disk), each path joins .gitignore (staged with it), the card says the secret is still in the history and
  should be changed, and Undo restores both.
- A commit that would record a private file it adds or changes is refused, naming the file and that fix; so is a commit
  whose added lines hold a high-confidence secret (HIGH: key blocks, cloud keys, provider tokens), naming the file,
  line and kind, never the value. A whole-tree stash never takes a private file. A push whose new commits add or change
  a private file or a secret is refused (the scan pushes always had), now with the fix.
"""

import json
import os

import pytest

from pebble.git_plans import HIGH, SECRETS, SensitiveContent, added_secrets, ignore_line, scan
from pebble.policy import PolicyError
from test_git_plans import AWS, TOKEN, apply, bare, git, host, macos, rev, status  # noqa: F401 (fixture)

# Synthetic values in each provider's documented shape, built here so no file holds one whole
SAMPLES = {'google_api_key': 'AI' + 'za' + 'Sy' + 'B' * 33, 'google_oauth_secret': 'GOC' + 'SPX-' + 'q' * 28,
           'stripe_live_key': 'sk' + '_live_' + '4eC39HqLyjWDarjtT1zdp7dc', 'openai_key': 'sk-' + 'proj-' + 'A1b2' * 12,
           'anthropic_key': 'sk-' + 'ant-api03-' + 'Ab1_' * 22, 'npm_token': 'np' + 'm_' + 'a1B2c3' * 6,
           'pypi_token': 'pyp' + 'i-AgEIcHlwaS5vcmc' + 'x' * 60, 'sendgrid_key': 'S' + 'G.' + 'a' * 22 + '.' + 'b' * 43,
           'huggingface_token': 'h' + 'f_' + 'abcdefghij' * 3 + 'abcd', 'digitalocean_token': 'do' + 'p_v1_' + 'a1' * 32,
           'shopify_token': 'shp' + 'at_' + 'a1b2c3d4' * 4,
           'slack_webhook': 'https://hooks.' + 'slack.com/services/T0123ABCD/B0123ABCD/' + 'abcdEFGH' * 3,
           'azure_storage_key': 'Account' + 'Key=' + 'A' * 86 + '==', 'aws_access_key_id': AWS, 'github_token': TOKEN,
           'private_key': '-----BEGIN ' + 'OPENSSH PRIVATE KEY-----'}
# Look-alikes the high-confidence kinds leave alone: the generic assignment and hashes, IDs and docs placeholders
ORDINARY = ['password = "' + 'correct-horse-battery-staple"', 'api_key = "' + '0123456789abcdef' * 2 + '"',
            'sha = "5d41402abc4b2a76b9719d911017c592"', 'AKIA' + 'EXAMPLE', 'sk-...', 'sk_test_' + 'a' * 24,
            'token: ${GITHUB_TOKEN}', 'AIza... (your key)', '-----BEGIN PUBLIC KEY-----', 'hf_hub_download(repo_id)',
            'npm_config_cache=/tmp', 'SG.example', 'uuid = "123e4567-e89b-12d3-a456-426614174000"']


def prepare(host, *argv):  # noqa: F811
    return host.plans.prepare(host.chat, 'command', {'argv': json.dumps(list(argv))})


def run(host, *argv):  # noqa: F811
    plan = prepare(host, *argv)
    return plan, host.plans.execute(host.chat, plan)


def committed_env(host):  # noqa: F811
    """The common mistake: .env committed long ago, and .gitignore tracked without it."""
    (host.repo / '.gitignore').write_text('__pycache__/\n')
    (host.repo / '.env').write_text('DB_PASSWORD=hunter2-but-longer\n')
    git(host.repo, 'add', '-f', '.gitignore', '.env')
    git(host.repo, 'commit', '-q', '-m', 'Add settings')
    return rev(host.repo)


def tracked(repo):
    return git(repo, 'ls-files').stdout.split()


# -- the untrack plan --------------------------------------------------------------------------------------------------

@macos
def test_a_committed_env_is_untracked_unread_ignored_and_the_card_says_to_change_the_secret(host):  # noqa: F811
    first = committed_env(host)
    remote = bare(host)
    git(host.repo, 'fetch', '-q', 'origin')
    secret = (host.repo / '.env').read_bytes()
    os.chmod(host.repo / '.env', 0)  # Pebble never reads it: an unreadable file plans and runs the same
    try:
        plan, result = run(host, 'rm', '--cached', '--', '.env')
    finally:
        os.chmod(host.repo / '.env', 0o600)
    assert plan['operation'] == 'untrack' and plan['destructive'] and plan['requires_review']
    card = ' '.join([plan['summary'], *plan['effects']])
    assert 'Pebble never reads it' in card and 'on disk' in card and 'Add /.env to .gitignore' in card
    assert f'still in this repository\'s history (1 commit(s); first in {first[:12]} "Add settings")' in card
    assert 'and on the remote already (origin/main)' in card and 'change it (a new key or password' in card
    assert 'hunter2' not in json.dumps(plan) + json.dumps(result)
    assert result['state'] == 'applied' and result['readback']['kept_on_disk'] == ['.env']
    assert (host.repo / '.env').read_bytes() == secret and '.env' not in tracked(host.repo)
    assert (host.repo / '.gitignore').read_text() == '__pycache__/\n/.env\n'
    assert status(host.repo) == 'D  .env\nM  .gitignore\n'  # staged together; the file itself is ignored now
    assert remote.exists()
    _, done = run(host, 'commit', '-m', 'Stop tracking .env')
    assert done['state'] == 'applied' and status(host.repo) == ''
    assert git(host.repo, 'show', '--name-status', '--format=', 'HEAD').stdout == 'D\t.env\nM\t.gitignore\n'


@macos
def test_undo_tracks_it_again_and_puts_gitignore_back(host):  # noqa: F811
    committed_env(host)
    entry = git(host.repo, 'ls-files', '-s', '.env').stdout
    _, result = run(host, 'rm', '--cached', '--', '.env')
    undo = host.plans.prepare(host.chat, 'undo', {'receipt_id': result['receipt_id']})
    assert any('the file on disk is not read or changed' in e for e in undo['effects'])
    assert host.plans.execute(host.chat, undo)['state'] == 'applied'
    assert git(host.repo, 'ls-files', '-s', '.env').stdout == entry
    assert (host.repo / '.gitignore').read_text() == '__pycache__/\n' and status(host.repo) == ''
    # once a commit records it, the untracking is history: Undo is refused
    _, result = run(host, 'rm', '--cached', '--', '.env')
    run(host, 'commit', '-m', 'Stop tracking .env')
    with pytest.raises(PolicyError, match='A commit or a branch switch came after this'):
        host.plans.prepare(host.chat, 'undo', {'receipt_id': result['receipt_id']})


@macos
def test_a_staged_new_private_file_is_unstaged_and_ignored_and_gitignore_with_own_changes_stays_unstaged(host):  # noqa: F811
    (host.repo / '.gitignore').write_text('build/\n')
    git(host.repo, 'add', '.gitignore')
    git(host.repo, 'commit', '-q', '-m', 'Ignore build')
    (host.repo / '.gitignore').write_text('build/\n*.log\n')  # the person's own unstaged edit
    (host.repo / 'config').mkdir()
    (host.repo / 'config' / 'credentials.json').write_text('{"k": 1}\n')
    git(host.repo, 'add', '-f', 'config/credentials.json')
    plan, result = run(host, 'rm', '--cached', '--', 'config/credentials.json')
    assert any('staged but never committed, so no commit holds it' in e for e in plan['effects'])
    assert any('changes of yours that are not staged, so this stays unstaged' in e for e in plan['effects'])
    assert result['state'] == 'applied' and status(host.repo) == ' M .gitignore\n'
    assert (host.repo / '.gitignore').read_text() == 'build/\n*.log\n/config/credentials.json\n'


@pytest.mark.parametrize('argv, match', [
    (['rm', '--cached', '--', '.env', 'README.md'], 'with a command of their own'),
    (['rm', '--cached', '-r', '--', '.env'], 'with a command of their own'),
    (['rm', '--', '.env'], 'Private credentials'),
    (['rm', '.env'], 'Private credentials'),
    (['rm', '--cached', '--', '.git/config'], 'Private credentials'),
    (['rm', '--cached', '--', 'missing.pem'], 'not tracked or staged'),
])
@macos
def test_untrack_refusals(host, argv, match):  # noqa: F811
    committed_env(host)
    before = (rev(host.repo), status(host.repo))
    with pytest.raises(PolicyError, match=match):
        prepare(host, *argv)
    with pytest.raises(PolicyError, match='with a command of their own'):
        host.plans.prepare(host.chat, 'command', {'argv': json.dumps([['rm', '--cached', '--', '.env'],
                                                                       ['commit', '-m', 'x']])})
    assert (rev(host.repo), status(host.repo)) == before


def test_an_ignore_line_matches_exactly_its_path():
    assert ignore_line('.env') == '/.env' and ignore_line('a b/k[1].pem') == '/a b/k\\[1].pem'
    assert ignore_line('x*.key') == '/x\\*.key' and ignore_line('odd.env ') == '/odd.env\\ '


# -- commits ---------------------------------------------------------------------------------------------------------

@macos
def test_a_commit_that_adds_or_changes_a_private_file_names_it_and_the_fix(host):  # noqa: F811
    committed_env(host)
    (host.repo / '.env').write_text('DB_PASSWORD=rotated-value-here\n')
    git(host.repo, 'add', '.env')
    for argv in (['commit', '-m', 'Update settings'], ['commit', '-a', '-m', 'Update']):
        with pytest.raises(PolicyError, match=r'record 1 private file\(s\) \(.env\).*\["rm", "--cached", "--", ".env"\]'):
            prepare(host, *argv)
    git(host.repo, 'reset', '-q')
    with pytest.raises(PolicyError, match=r'record 1 private file\(s\) \(.env\)'):
        prepare(host, 'commit', '-a', '-m', 'Update')  # -a would take the unstaged change
    (host.repo / 'notes.txt').write_text('changed\n')
    assert prepare(host, 'commit', '-m', 'Notes', '--', 'notes.txt')['operation'] == 'command'  # only notes.txt


@macos
def test_a_commit_whose_added_lines_hold_a_secret_names_file_line_and_kind_never_the_value(host):  # noqa: F811
    (host.repo / 'settings.py').write_text(f'DEBUG = False\nAWS_KEY = "{AWS}"\n')
    git(host.repo, 'add', 'settings.py')
    with pytest.raises(SensitiveContent) as caught:
        prepare(host, 'commit', '-m', 'Add settings')
    text = str(caught.value)
    assert 'settings.py:2 aws_access_key_id' in text and AWS not in text and 'environment variable' in text
    assert caught.value.findings == [{'path': 'settings.py', 'line': 2, 'kind': 'aws_access_key_id'}]
    # -a, a path and --amend take the lines from the working tree or the commit being replaced
    git(host.repo, 'reset', '-q')
    git(host.repo, 'add', 'settings.py')
    git(host.repo, 'commit', '-q', '-m', 'Outside Pebble')  # committed outside Pebble, not pushed
    with pytest.raises(SensitiveContent, match='settings.py:2 aws_access_key_id'):
        prepare(host, 'commit', '--amend', '-m', 'Add settings')
    (host.repo / 'app.py').write_text(f'TOKEN = "{TOKEN}"\n')
    for argv in (['commit', '-a', '-m', 'x'], ['commit', '-m', 'x', '--', 'app.py']):
        with pytest.raises(SensitiveContent, match='app.py:1 github_token'):
            prepare(host, *argv)
    # the generic assignment heuristic is not a refusal for a commit's lines
    (host.repo / 'app.py').write_text('password = "correct-horse-battery-staple"\n')
    assert prepare(host, 'commit', '-a', '-m', 'x')['operation'] == 'command'


def test_the_high_confidence_kinds_find_each_provider_shape_and_leave_look_alikes_alone():
    assert [kind for kind, _ in HIGH] == [kind for kind, _ in SECRETS if kind != 'secret_assignment']
    for kind, value in SAMPLES.items():
        diff = f'diff --git a/c.py b/c.py\n+++ b/c.py\n@@ -0,0 +7,2 @@\n+x = 1\n+key = "{value}"\n'.encode()
        found = added_secrets(diff)
        assert {'path': 'c.py', 'line': 8, 'kind': kind} in found, (kind, found)
        assert value not in json.dumps(found)
    for text in ORDINARY:
        assert added_secrets(f'+++ b/c.py\n@@ -1 +1 @@\n+{text}\n'.encode()) == [], text
    assert added_secrets(f'+++ b/c.py\n@@ -1 +1 @@\n-key = "{AWS}"\n+key = None\n'.encode()) == []  # a removed line
    assert [item['kind'] for item in scan('cfg.py', SAMPLES['stripe_live_key'].encode())] == ['stripe_live_key']


# -- stash and push --------------------------------------------------------------------------------------------------

@macos
def test_a_whole_tree_stash_never_takes_a_private_file(host):  # noqa: F811
    (host.repo / '.env').write_text('K=1\n')
    (host.repo / 'notes.txt').write_text('draft\n')
    (host.repo / 'idea.md').write_text('idea\n')
    with pytest.raises(PolicyError, match=r'would reach 1 private file.*"stash", "push", "-u", "--"'):
        prepare(host, 'stash', 'push', '-u')
    assert prepare(host, 'stash', 'push')['operation'] == 'command'  # without -u the untracked .env stays out
    _, result = run(host, 'stash', 'push', '-u', '--', 'notes.txt', 'idea.md')
    assert result['state'] == 'applied' and (host.repo / '.env').read_text() == 'K=1\n'


@macos
def test_a_push_whose_new_commits_add_a_private_file_or_a_secret_is_refused_with_the_fix(host):  # noqa: F811
    bare(host)
    published = rev(host.repo)
    (host.repo / '.env').write_text('K=1\n')
    git(host.repo, 'add', '-f', '.env')
    git(host.repo, 'commit', '-q', '-m', 'Oops')  # committed outside Pebble
    with pytest.raises(SensitiveContent, match=rf'\.env env_file.*\["reset", "--soft", "{published}"\] \(the commit '
                                               r'origin/main is at.*\["rm", "--cached", "--", "<path>"\]'):
        host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main'})
    # the fix the refusal names: rewrite the unpushed commit without it, then the push goes
    run(host, 'reset', '--soft', published)
    run(host, 'rm', '--cached', '--', '.env')
    run(host, 'commit', '-m', 'Ignore .env')
    plan = host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main'})
    assert host.plans.execute(host.chat, plan)['state'] == 'applied'
    assert git(host.tmp / 'remote.git', 'ls-tree', '-r', '--name-only', 'main').stdout.split() == [
        '.gitignore', 'README.md', 'app.py', 'notes.txt']
