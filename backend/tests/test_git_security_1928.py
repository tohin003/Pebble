"""Phase 46 (1.9.28, w9-git follow-up 1): the independent security review of w9-git's first patch (w9-git-security-
review.md, F1-F9 and its notes) and the lead's "don't push" rule, each pinned on real temporary repositories.

Reproduced first on the first patch with the reviewer's own scripts (s1-s14): while a merge was stopped, --theirs or a
restore of a folder overwrote an unconflicted file's unstaged work and the card promised an Undo that did not exist
(F1); an NFD folder name and eleven credential file names were staged by add (F2, F3); a rebase -X ours dropped the
person's own commit with a card that did not say so (F4); a tag named ORIG_HEAD ran in ORIG_HEAD's place (F5); ORIG_HEAD
inside a later range escaped the first-command rule, and no card line or commit list showed what a range covers (F6, F7);
commit -a during a stopped merge committed the conflict markers (F9).
"""

import json
import unicodedata

import pytest

from pebble import shell_policy
from pebble.coding_api import CodingAPI, push_covered, push_forbidden, pushes
from pebble.git_commands import parse
from pebble.git_plans import RANGE_SHOWN
from pebble.git_workflows import private
from pebble.policy import PolicyError, Workspace
from test_git_commands_46 import command, second_commit, wip
from test_git_plans import bare, git, host, macos, rev  # noqa: F401 (fixture)


def prepare(host, *argv):  # noqa: F811
    return host.plans.prepare(host.chat, 'command', {'argv': json.dumps(list(argv))})


def sequence(host, *items):  # noqa: F811
    return host.plans.prepare(host.chat, 'command', {'argv': json.dumps([list(item) for item in items])})


def stopped_merge(host, files=('src/app.py', 'src/util.py')):  # noqa: F811
    """The review's s1 shape: src/app.py conflicts, then the person edits the unconflicted src/util.py."""
    for name in files:
        (host.repo / name).parent.mkdir(parents=True, exist_ok=True)
        (host.repo / name).write_text('x = 1\n')
    git(host.repo, 'add', '-A')
    git(host.repo, 'commit', '-q', '-m', 'src')
    git(host.repo, 'switch', '-q', '-c', 'feature')
    (host.repo / files[0]).write_text('x = 2  # feature\n')
    git(host.repo, 'commit', '-qam', 'feature change')
    git(host.repo, 'switch', '-q', 'main')
    (host.repo / files[0]).write_text('x = 3  # main\n')
    git(host.repo, 'commit', '-qam', 'main change')
    assert git(host.repo, 'merge', 'feature', check=False).returncode != 0
    (host.repo / files[1]).write_text('x = 1\n# PERSON UNSTAGED WORK\n')


# -- F1: while stopped, only exact conflicted paths; no Undo promised; no private file through a folder --------------

@macos
def test_a_side_or_restore_while_stopped_takes_only_exact_conflicted_paths_and_promises_no_undo(host):  # noqa: F811
    stopped_merge(host)
    for argv in (['checkout', '--theirs', '--', 'src'], ['checkout', '--theirs', '--', 'src/util.py'],
                 ['restore', '--theirs', '--', 'src'], ['checkout', 'HEAD', '--', 'src'],
                 ['restore', '--source', 'HEAD', '--', 'src/util.py'], ['restore', '--', 'src/util.py'],
                 ['checkout', '--ours', '--', 'src']):
        with pytest.raises(PolicyError, match=r'takes only the files Git marked as conflicted.*src/app\.py'):
            prepare(host, *argv)
    assert (host.repo / 'src' / 'util.py').read_text().endswith('# PERSON UNSTAGED WORK\n')
    plan = prepare(host, 'checkout', '--theirs', '--', 'src/app.py')
    assert plan['bindings']['unmerged'] == ['src/app.py'] and plan['bindings']['run'][-2:] == ['--', 'src/app.py']
    assert any('no Undo while the merge is stopped' in effect for effect in plan['effects'])
    assert not any('Undo restores' in effect for effect in plan['effects'])
    done = host.plans.execute(host.chat, plan)
    assert done['state'] == 'applied' and done['result']['undo'] == 'unavailable'
    assert (host.repo / 'src' / 'app.py').read_text() == 'x = 2  # feature\n'
    assert (host.repo / 'src' / 'util.py').read_text().endswith('# PERSON UNSTAGED WORK\n')
    # rm and the sequence card say the same while stopped; staged-only restores touch no file and stay available
    assert any('no Undo while the merge is stopped' in e for e in prepare(host, 'rm', '--', 'README.md')['effects'])
    card = sequence(host, ['checkout', '--ours', '--', 'src/app.py'], ['add', '--', 'src/app.py'])['effects']
    assert any('no Undo while the merge is stopped' in effect for effect in card)
    assert prepare(host, 'restore', '--staged', '--', 'src/util.py')['bindings']['run'][-1] == 'src/util.py'


@macos
def test_a_conflicted_path_given_in_nfd_binds_gits_own_spelling(host):  # noqa: F811
    stopped_merge(host, files=(unicodedata.normalize('NFC', 'café/app.py'), 'src/util.py'))
    plan = prepare(host, 'checkout', '--theirs', '--', unicodedata.normalize('NFD', 'café/app.py'))
    assert plan['bindings']['unmerged'] == [unicodedata.normalize('NFC', 'café/app.py')]


@macos
def test_a_private_file_is_never_overwritten_deleted_or_set_aside_through_a_folder(host):  # noqa: F811
    (host.repo / 'config').mkdir()
    (host.repo / 'config' / '.env').write_text('KEY=base\n')
    (host.repo / 'config' / 'app.ini').write_text('x\n')
    git(host.repo, 'add', '-f', 'config')
    git(host.repo, 'commit', '-qm', 'config')
    (host.repo / 'config' / '.env').write_text('KEY=the person\'s local secret\n')
    for argv in (['checkout', 'HEAD', '--', 'config'], ['restore', '--', 'config'], ['rm', '-r', '--', 'config'],
                 ['rm', '-r', '--cached', '--', 'config'], ['stash', 'push', '--', 'config']):
        with pytest.raises(PolicyError, match='private file') as refused:
            prepare(host, *argv)
        assert '.env' not in str(refused.value).split('(')[0], argv  # the count only, never the name
    assert prepare(host, 'restore', '--', 'config/app.ini')['bindings']['run'][-1] == 'config/app.ini'
    # s14: during a stopped merge the folder is refused before anything else
    git(host.repo, 'stash', '-q')
    git(host.repo, 'switch', '-q', '-c', 'feature')
    (host.repo / 'config' / '.env').write_text('KEY=theirs\n')
    git(host.repo, 'commit', '-qam', 'f')
    git(host.repo, 'switch', '-q', 'main')
    (host.repo / 'config' / '.env').write_text('KEY=ours\n')
    git(host.repo, 'commit', '-qam', 'm')
    git(host.repo, 'merge', 'feature', check=False)
    with pytest.raises(PolicyError):
        prepare(host, 'checkout', '--theirs', '--', 'config')
    assert (host.repo / 'config' / '.env').read_text().startswith('<<<<<<<')


@macos
def test_a_command_that_can_stop_on_a_conflict_promises_no_undo_for_that_case(host):  # noqa: F811
    git(host.repo, 'switch', '-q', '-c', 'feature')
    second_commit(host, message='Feature')
    git(host.repo, 'switch', '-q', 'main')
    wip(host)
    card = prepare(host, 'rebase', '--autostash', 'feature')['effects']
    assert any('Undo restores them' in e and 'none if Git stops on a conflict' in e for e in card)


# -- F2 and F3: Git's own expansion, Unicode form, every credential name; and nothing private already staged -----------

@macos
def test_an_nfd_folder_name_and_a_plain_commit_never_take_a_private_file(host):  # noqa: F811
    folder = host.repo / unicodedata.normalize('NFC', 'café')
    folder.mkdir()
    (folder / '.env').write_text('K=1\n')
    (folder / 'a.txt').write_text('a\n')
    for argv in (['add', '--', unicodedata.normalize('NFD', 'café')], ['add', unicodedata.normalize('NFD', 'café')],
                 ['add', '--', unicodedata.normalize('NFC', 'café')]):
        with pytest.raises(PolicyError, match='private file'):
            prepare(host, *argv)
    git(host.repo, 'add', '-f', '--', 'café/.env')  # staged outside Pebble, before the request
    # follow-up 2 (D179): the refusal names the file and the fix the model can offer, the untrack plan
    with pytest.raises(PolicyError, match=r'record 1 private file\(s\) \(café/.env\).*\["rm", "--cached", "--", '
                                          r'"café/.env"\]; then commit'):
        prepare(host, 'commit', '-m', 'wip')


CREDENTIALS = ('prod.env', 'id_rsa', 'id_ed25519', 'credentials.json', 'secrets.yaml', 'service-account.json',
               '.git-credentials', 'kubeconfig', 'terraform.tfstate', 'terraform.tfstate.backup', '.htpasswd',
               '.docker/config.json', '.kube/config', 'client_secret_123.apps.googleusercontent.com.json',
               'deploy/my-service-account-key.json', 'keys/release.jks')
NOT_PRIVATE = ('secrets.md', 'app/secrets.py', 'credentials_test.py', 'terraform.tfvars', 'env.py', 'config.json',
               'docker/config.json', 'README.md', 'kubeconfig.md', 'environment.yml', 'src/envelope.py')


@pytest.mark.parametrize('name', CREDENTIALS)
def test_credential_files_are_private_to_git_through_the_products_one_list(name):
    assert private(name) and shell_policy.PROTECTED_NAME.search(name), name


@pytest.mark.parametrize('name', NOT_PRIVATE)
def test_ordinary_files_stay_ordinary(name):
    assert not private(name), name


@macos
def test_add_dot_never_stages_a_credential_file_and_names_none(host):  # noqa: F811
    (host.repo / '.docker').mkdir()
    for name in CREDENTIALS[:11] + ('.docker/config.json',):
        (host.repo / name).write_text('secret\n')
    (host.repo / 'notes.txt').write_text('one\ntwo\nthree\nfour\n')
    for argv in (['add', '.'], ['add', '-A']):
        with pytest.raises(PolicyError, match=r'reach 12 private file') as refused:
            prepare(host, *argv)
        assert not any(name in str(refused.value) for name in ('id_rsa', 'prod.env', 'tfstate', 'kubeconfig'))
    assert prepare(host, 'add', '-u')['bindings']['run'] == ['add', '--update']
    assert git(host.repo, 'diff', '--cached', '--name-only').stdout == ''


# -- F4: which side wins under -X, in words ----------------------------------------------------------------------------

@macos
def test_a_strategy_side_says_which_side_wins_and_what_happens_to_the_persons_commits(host):  # noqa: F811
    git(host.repo, 'switch', '-q', '-c', 'feature')
    (host.repo / 'notes.txt').write_text('one\nTWO (my feature work)\nthree\n')
    git(host.repo, 'commit', '-qam', 'My feature work')
    git(host.repo, 'switch', '-q', 'main')
    (host.repo / 'notes.txt').write_text('one\ntwo (upstream)\nthree\n')
    git(host.repo, 'commit', '-qam', 'Upstream change')
    git(host.repo, 'switch', '-q', 'feature')
    card = ' '.join(prepare(host, 'rebase', '-X', 'ours', 'main')['effects'])
    assert "main's version wins" in card and 'dropped from the branch' in card
    assert "your commits' version wins" in ' '.join(prepare(host, 'rebase', '-X', 'theirs', 'main')['effects'])
    picked = rev(host.repo, 'feature')
    git(host.repo, 'switch', '-q', 'main')
    assert 'the picked version wins' in ' '.join(prepare(host, 'cherry-pick', '-X', 'theirs', picked)['effects'])
    assert 'the undo wins' in ' '.join(prepare(host, 'revert', '-X', 'theirs', 'HEAD')['effects'])
    assert 'not undone' in ' '.join(prepare(host, 'revert', '-X', 'ours', 'HEAD')['effects'])


# -- F5 and F6: the special ref only, ranges held to the same rules -------------------------------------------------------

@macos
def test_orig_head_and_a_reflog_entry_resolve_only_to_the_special_ref(host):  # noqa: F811
    base = rev(host.repo)
    lost = second_commit(host, message='Lost work')
    with pytest.raises(PolicyError, match='ORIG_HEAD is not set'):
        prepare(host, 'reset', '--hard', 'ORIG_HEAD')
    git(host.repo, 'reset', '-q', '--hard', base)
    plan = prepare(host, 'reset', '--keep', 'ORIG_HEAD')
    assert plan['bindings']['run'] == ['reset', '--keep', lost]
    assert any(f'ORIG_HEAD is commit {lost[:12]} "Lost work"' in e and 'where the last reset' in e for e in plan['effects'])
    git(host.repo, 'tag', 'ORIG_HEAD', base)  # s4b: a tag of that name (a clone brings tags)
    with pytest.raises(PolicyError, match='also named ORIG_HEAD'):
        prepare(host, 'reset', '--hard', 'ORIG_HEAD')
    git(host.repo, 'tag', '-d', 'ORIG_HEAD')
    git(host.repo, 'tag', 'main', lost)  # a tag and a branch both called main
    with pytest.raises(PolicyError, match='more than one ref'):
        prepare(host, 'branch', 'recovered', 'main@{1}')
    plan = prepare(host, 'branch', 'recovered', 'refs/heads/main@{1}')
    assert plan['bindings']['run'] == ['branch', 'recovered', lost]
    assert any("refs/heads/main's reflog" in effect for effect in plan['effects'])
    for argv in (['merge', 'FETCH_HEAD@{0}'], ['reset', '--hard', 'MERGE_HEAD@{0}'], ['reset', '--hard', 'AUTO_MERGE@{0}'],
                 ['revert', 'CHERRY_PICK_HEAD@{0}'], ['checkout', 'FETCH_HEAD@{0}', '--', 'notes.txt'],
                 ['reset', '--hard', 'ORIG_HEAD@{1}']):
        with pytest.raises(PolicyError):
            parse(argv)


@macos
def test_orig_head_in_a_range_is_bound_shown_and_kept_to_the_first_command(host):  # noqa: F811
    base = rev(host.repo)
    one = second_commit(host, text='print("one")\n', message='One')
    two = second_commit(host, text='print("two")\n', message='Two')
    git(host.repo, 'reset', '-q', '--hard', base)  # ORIG_HEAD is Two
    plan = prepare(host, 'cherry-pick', 'HEAD..ORIG_HEAD')
    assert plan['bindings']['run'] == ['cherry-pick', one, two]
    assert any(f'ORIG_HEAD is commit {two[:12]} "Two"' in e for e in plan['effects'])
    assert any(f'HEAD..ORIG_HEAD covers 2 commit(s), applied in this order: {one[:12]} "One"; {two[:12]} "Two"' in e
               for e in plan['effects'])
    with pytest.raises(PolicyError, match='only in the first command of a sequence'):
        sequence(host, ['reset', '--hard', base], ['cherry-pick', 'ORIG_HEAD~1..ORIG_HEAD'])
    git(host.repo, 'tag', 'ORIG_HEAD', base)
    with pytest.raises(PolicyError, match='also named ORIG_HEAD'):
        prepare(host, 'cherry-pick', 'HEAD..ORIG_HEAD')


# -- F7: a range lists what it covers ---------------------------------------------------------------------------------

@macos
def test_a_range_card_lists_its_commits_bounded_with_a_count(host):  # noqa: F811
    git(host.repo, 'switch', '-q', '-c', 'side')
    for number in range(3):
        (host.repo / f'side{number}.txt').write_text(f'{number}\n')
        git(host.repo, 'add', '-A')
        git(host.repo, 'commit', '-qm', f'side {number}')
    git(host.repo, 'switch', '-q', 'main')
    git(host.repo, 'merge', '-q', '--no-ff', '--no-edit', 'side')
    plan = prepare(host, 'revert', '-m', '1', 'HEAD~1..HEAD')
    line = next(e for e in plan['effects'] if 'HEAD~1..HEAD covers' in e)
    assert 'covers 4 commit(s), reverted in this order' in line and '"side 0"' in line
    for number in range(RANGE_SHOWN + 2):
        second_commit(host, text=f'print({number})\n', message=f'Step {number}')
    line = next(e for e in prepare(host, 'cherry-pick', 'side..main')['effects'] if 'covers' in e)
    # the merge and the 12 steps, oldest first: the card names the first RANGE_SHOWN and counts the rest
    assert 'covers 13 commit(s), applied in this order' in line and line.count('; ') == RANGE_SHOWN
    assert line.endswith('; and 3 more') and '"Step 8"' in line and '"Step 9"' not in line


# -- F8 (pre-existing): a ref named like a short commit ID ---------------------------------------------------------------

@macos
def test_a_ref_named_like_a_short_commit_id_never_runs_in_that_commits_place(host):  # noqa: F811
    good = second_commit(host, text='print("good fix")\n', message='Good fix the person saw in the log')
    git(host.repo, 'reset', '-q', '--hard', 'HEAD~1')
    git(host.repo, 'switch', '-q', '-c', 'tmp')
    evil = second_commit(host, text='print("other")\n', message='Other')
    git(host.repo, 'switch', '-q', 'main')
    short = next(good[:n] for n in range(7, 41) if any(c.isdigit() for c in good[:n]))  # Git's abbreviation, with a digit
    git(host.repo, 'branch', short, evil)  # the reviewer's s9: a branch named like the good commit's short ID
    for argv in (['cherry-pick', short], ['cherry-pick', f'HEAD..{short}']):
        with pytest.raises(PolicyError, match='not the commit whose ID starts with it'):
            prepare(host, *argv)
    assert prepare(host, 'cherry-pick', good)['bindings']['run'] == ['cherry-pick', good]
    git(host.repo, 'branch', '-D', short)
    assert prepare(host, 'cherry-pick', short)['bindings']['run'] == ['cherry-pick', good]  # the plain abbreviation
    git(host.repo, 'branch', 'decade', evil)  # a word made of hex letters is an ordinary branch name
    assert prepare(host, 'cherry-pick', 'decade')['bindings']['run'] == ['cherry-pick', evil]


# -- F9: commit -a while stopped -----------------------------------------------------------------------------------------

@macos
def test_commit_all_while_stopped_never_commits_conflict_markers(host):  # noqa: F811
    stopped_merge(host)
    (host.repo / 'README.md').write_text('# Fixture\nstaged by the person\n')
    git(host.repo, 'add', 'README.md')
    for argv in (['commit', '--no-edit', '-a'], ['commit', '-a', '-m', 'merge']):
        with pytest.raises(PolicyError, match='conflict markers'):
            prepare(host, *argv)
    (host.repo / 'src' / 'app.py').write_text('x = 4  # resolved\n')
    command(host, 'add', '--', 'src/app.py')
    plan = prepare(host, 'commit', '--no-edit')
    assert any('Conclude the stopped merge with what is staged' in effect for effect in plan['effects'])


# -- the review's notes ------------------------------------------------------------------------------------------------

def test_text_and_paths_equal_to_a_reflog_name_are_no_revisions_and_a_dropped_positional_is_refused():
    assert parse(['commit', '-m', 'ORIG_HEAD']).argv == ['commit', '-m', 'ORIG_HEAD']
    assert parse(['add', '--', 'ORIG_HEAD']).argv == ['add', '--', 'ORIG_HEAD']
    assert parse(['tag', '-a', 'v1', '-m', 'HEAD@{1}']).argv[-1] == 'v1'
    with pytest.raises(PolicyError, match='Take one side'):
        parse(['checkout', '--ours', 'HEAD', '--', 'a.txt'])


# -- "don't push": refused on the host, nothing else affected --------------------------------------------------------

@pytest.mark.parametrize('messages, forbidden', [
    (['Commit it on main. Don\'t push.'], "Don't push"), (['Do not push yet.'], 'Do not push'),
    (['Keep it local for now.'], 'Keep it local'), (['Squash them without pushing.'], 'without pushing'),
    (['Commit only notes.txt. No push.'], 'No push'), (['Please make sure nothing gets pushed.'], 'nothing gets pushed'),
    (['Commit, don\'t push.', 'Also tag it v2.'], "don't push"), (["Don't push anything."], "Don't push"),
    (['Tag the current commit v1.4.0 and push only that tag.'], None), (['Commit and push it.'], None),
    (['Merge feature into main.'], None), (['Push the tag, but don\'t push yet the rest.'], None),
    (['Commit, don\'t push.', 'Now push it to origin.'], None), (["Rebase it but don't force-push."], None),
])
def test_a_request_that_forbids_pushing_is_read_from_the_persons_newest_word_on_it(messages, forbidden):
    found = push_forbidden(messages)
    assert (found is None) if forbidden is None else (found[0].lower() == forbidden.lower() and found[1] == {'all'}), (
        messages, found)


# Phase 46 (1.9.28, the lead's dev check): a prohibition covers only what it names
TAG_PUSH = {'operation': 'command', 'argv': ['push', 'origin', 'v1.4.0']}
MAIN_PUSH = {'operation': 'push', 'remote': 'origin', 'branch': 'main', 'remote_branch': 'main'}
FEATURE_PUSH = {'operation': 'push', 'remote': 'origin', 'branch': 'feature/csv', 'remote_branch': 'feature/csv'}


@pytest.mark.parametrize('message, scopes, refused', [
    ("Don't push.", {'all'}, (True, True, True)),
    ('Do not push any branch.', {'branches'}, (False, True, True)),
    ("Don't push branches; the tag is fine.", {'branches'}, (False, True, True)),
    ('No branch push; notes.txt untouched.', {'branches'}, (False, True, True)),
    ('Do not push tags.', {'tags'}, (True, False, False)), ('No tag push.', {'tags'}, (True, False, False)),
    ('Do not push branches or tags.', {'all'}, (True, True, True)),
    ("Don't push main; push the tag instead.", {'ref:main'}, (False, True, False)),
    ("Don't push the main branch.", {'ref:main'}, (False, True, False)),
    ("Don't push `feature/csv`.", {'ref:feature/csv'}, (False, False, True)),
    ("Don't push v1.4.0 yet.", {'ref:v1.4.0'}, (True, False, False)),
    ("Don't push the feature branch.", {'branches'}, (False, True, True)),  # a description, not a name: every branch
    ('Keep the main branch local.', {'ref:main'}, (False, True, False)),
    ("Don't push any branch. Don't push tags either.", {'branches', 'tags'}, (True, True, True)),
    ("Don't push to origin.", {'all'}, (True, True, True)), ("Don't push it until I check.", {'all'}, (True, True, True)),
])
def test_a_prohibition_covers_only_what_it_names(message, scopes, refused):
    said, found = push_forbidden([message])
    assert found == scopes, (message, found)
    assert tuple(bool(push_covered(found, payload)) for payload in (TAG_PUSH, MAIN_PUSH, FEATURE_PUSH)) == refused


# The private cases the lead's dev check found (both passed on 1.9.27): each phrasing publishes its tag, no branch
D_GW_92 = ('Mark the current commit on main as release v1.4.0 with an annotated tag whose message is "Release 1.4.0", '
           'then publish only that tag to origin. Do not push any branch. End your reply with the line `FINAL: done`, '
           'or `FINAL: blocked` and the reason if something stops you.')
TAG_ONLY = {
    'd-gw-92/k1': D_GW_92, 'd-gw-92/k2': D_GW_92, 'd-gw-92/k3': D_GW_92,  # the case's three phrasings are one text
    'd-gw-109/k1': 'Tag the commit "Show item count in report" (not the latest one) as v1.1.0, and publish only that tag '
                   'to origin. Don\'t push any branch, and keep my uncommitted notes.txt and drafts/idea.md as they are. '
                   'End your reply with the line `FINAL: done`, or `FINAL: blocked` and the reason if something stops you.',
    'd-gw-109/k2': 'Tag "Show item count in report" as v1.1.0 and push just that tag to origin. No branch push; notes.txt '
                   'and drafts/idea.md untouched. Last line: `FINAL: done` (or `FINAL: blocked` + reason).',
    'd-gw-109/k3': 'We shipped the version from the commit "Show item count in report" (the one before the latest). Could '
                   'you tag that commit v1.1.0 and push only the tag to origin? No branches, please, and leave my '
                   'uncommitted notes.txt and drafts/idea.md alone. Finish with `FINAL: done`, or `FINAL: blocked` and '
                   'why if you get stuck.',
}


@pytest.mark.parametrize('cell', sorted(TAG_ONLY))
def test_a_tag_only_request_publishes_its_tag_and_no_branch(cell):
    found = push_forbidden([TAG_ONLY[cell]])
    tag = {'operation': 'command', 'argv': ['push', 'origin', 'v1.1.0' if '109' in cell else 'v1.4.0']}
    assert found is None or not push_covered(found[1], tag), (cell, found)
    if cell != 'd-gw-109/k3':  # k3's "No branches, please" names no push; the review card shows the branch push
        assert push_covered(found[1], MAIN_PUSH) == 'a branch push'


def test_only_push_arguments_count_as_a_push():
    assert pushes({'operation': 'push', 'remote': 'origin'}) and pushes({'operation': 'command', 'argv': '["push", "origin", "v1"]'})
    assert pushes({'operation': 'command', 'argv': ['git', 'push']})
    for args in ({'operation': 'commit', 'paths': '["a.py"]'}, {'operation': 'command', 'argv': '["commit", "-m", "x"]'},
                 {'operation': 'command', 'argv': '[["add", "-A"], ["commit", "-m", "x"]]'},
                 {'operation': 'command', 'argv': '["fetch", "origin"]'}, {'operation': 'command', 'argv': 'not json'}):
        assert not pushes(args), args


@macos
def test_a_push_plan_is_refused_on_the_host_when_the_person_said_not_to_push_and_nothing_else_is(host):  # noqa: F811
    from types import SimpleNamespace
    bare(host)
    git(host.repo, 'tag', 'v1.0')
    staged = []
    app = SimpleNamespace(store=host.app.store, git_plans=host.plans, cancel=None, active=None, active_run=None,
                          propose=lambda cid, kind, payload: staged.append(payload) or payload)
    api, ws = CodingAPI(app), Workspace(host.repo)
    host.app.store.message(host.chat, 'user', 'Tag the release and commit the notes. Don\'t push anything.')
    for args in ({'operation': 'command', 'argv': '["push", "origin", "v1.0"]'},
                 {'operation': 'push', 'remote': 'origin', 'branch': 'main'}):
        with pytest.raises(PolicyError, match=r'said not to push \("Don\'t push"\).*pushing is left to them'):
            api.root_tool(host.chat, 'ProposeGitChange', args, ws, False)
    assert not staged
    wip(host)
    api.root_tool(host.chat, 'ProposeGitChange', {'operation': 'command', 'argv': '["add", "--", "notes.txt"]'}, ws, False)
    api.root_tool(host.chat, 'ProposeGitChange', {'operation': 'command', 'argv': '["tag", "v1.1"]'}, ws, False)
    assert len(staged) == 2
    host.app.store.message(host.chat, 'user', 'Now push the tag v1.0 to origin.')
    api.root_tool(host.chat, 'ProposeGitChange', {'operation': 'command', 'argv': '["push", "origin", "v1.0"]'}, ws, False)
    assert staged[-1]['publication'] is True


@macos
@pytest.mark.parametrize('cell', ['d-gw-92/k1', 'd-gw-109/k1', 'd-gw-109/k2'])
def test_a_tag_only_request_stages_the_tag_push_and_refuses_a_branch_push_on_the_host(host, cell):  # noqa: F811
    from types import SimpleNamespace
    bare(host)
    git(host.repo, 'tag', 'v1.1.0')
    git(host.repo, 'tag', 'v1.4.0')
    second_commit(host)  # main has a commit origin lacks, so a branch push would publish it
    staged = []
    app = SimpleNamespace(store=host.app.store, git_plans=host.plans, cancel=None, active=None, active_run=None,
                          propose=lambda cid, kind, payload: staged.append(payload) or payload)
    api, ws = CodingAPI(app), Workspace(host.repo)
    host.app.store.message(host.chat, 'user', TAG_ONLY[cell])
    tag = 'v1.1.0' if '109' in cell else 'v1.4.0'
    api.root_tool(host.chat, 'ProposeGitChange', {'operation': 'command', 'argv': json.dumps(['push', 'origin', tag])},
                  ws, False)
    assert staged[-1]['publication'] is True and staged[-1]['argv'] == ['push', 'origin', tag]
    for args in ({'operation': 'push', 'remote': 'origin', 'branch': 'main'},
                 {'operation': 'command', 'argv': '["push", "origin", "main"]'}):
        with pytest.raises(PolicyError, match=r'which covers this push \(a branch push\).*nothing was pushed'):
            api.root_tool(host.chat, 'ProposeGitChange', args, ws, False)
    assert len(staged) == 1
