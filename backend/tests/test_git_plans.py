"""Reviewed Git plans on real temporary repositories and real local bare remotes.

Fixture Git commands below are unconfined test-author commands, run only inside
pytest temporary folders with an isolated environment and hooks disabled. The
service under test runs every Git process through GitWorkflows' sandbox.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import git_plans, git_workflows
from pebble.git_plans import (OPERATIONS, TOOLS, GitPlanUncertain, GitPlans, SensitiveContent, branch_shape, destination,
                              message_arg, paths_arg, plan_hash, scan)
from pebble.git_workflows import GitWorkflows
from pebble.policy import PolicyError
from pebble.store import Store

MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
macos = pytest.mark.skipif(not MAC, reason='Confined Git plans are a macOS sandbox-exec product surface.')
# Built at runtime so this file never holds a literal credential.
AWS = 'AKIA' + 'Q7RZ' * 4
TOKEN = 'gh' + 'p_' + 'a1B2' * 9


def git(cwd, *args, check=True):
    cwd = Path(cwd).resolve()
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/var/empty', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', 'GIT_CEILING_DIRECTORIES': str(cwd.parent),
           'GIT_AUTHOR_NAME': 'Fixture Author', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
           'GIT_COMMITTER_NAME': 'Fixture Author', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
    process = subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'init.defaultBranch=main',
                              '-c', 'commit.gpgSign=false', '-c', 'core.fsmonitor=false', *args],
                             cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    if check and process.returncode:
        raise AssertionError(f'fixture git {args} failed: {process.stderr}')
    return process


def make_repo(path):
    path.mkdir()
    git(path, 'init', '-q')
    (path / 'app.py').write_text('print("base")\n')
    (path / 'README.md').write_text('# Fixture\n')
    (path / 'notes.txt').write_text('one\ntwo\nthree\n')
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'Initial fixture commit')
    return path.resolve()


def rev(repo, name='HEAD'):
    return git(repo, 'rev-parse', name).stdout.strip()


def index(repo):
    return git(repo, 'ls-files', '--stage').stdout


def status(repo):
    return git(repo, 'status', '--porcelain=v1', '--untracked-files=all').stdout


def blob(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def subcommand(argv):
    rest = argv[argv.index('--no-pager') + 1:]
    while rest and rest[0] == '-c':
        rest = rest[2:]
    if rest and rest[0].startswith('--git-dir='):
        rest = rest[1:]
    return rest[0] if rest else None


def row(host, ident):
    rows = host.app.store.query('SELECT * FROM git_plan_receipts WHERE id=?', (ident,))
    return {**rows[0], 'receipt': json.loads(rows[0]['receipt'])} if rows else None


def apply(host, operation, **args):
    plan = host.plans.prepare(host.chat, operation, args)
    return plan, host.plans.execute(host.chat, plan)


def bare(host, name='remote.git'):
    path = host.tmp / name
    git(host.tmp, 'init', '-q', '--bare', str(path))
    git(host.repo, 'remote', 'add', 'origin', str(path))
    git(host.repo, 'push', '-q', str(path), 'main:main')
    return path


@pytest.fixture
def host(tmp_path):
    if not MAC:
        pytest.skip('Confined Git plans are a macOS sandbox-exec product surface.')
    tmp = tmp_path.resolve()
    home = tmp / 'data'
    app = SimpleNamespace(home=home, store=Store(home))
    repo = make_repo(tmp / 'project')
    git(repo, 'config', 'user.name', 'Reviewed Author')
    git(repo, 'config', 'user.email', 'author@example.invalid')
    chat = app.store.new_chat(str(repo))
    workflows = GitWorkflows(app)
    yield SimpleNamespace(app=app, repo=repo, chat=chat, git=workflows, plans=GitPlans(app, workflows), tmp=tmp)
    workflows.close()
    app.store.close()


def test_tool_schema_and_pure_validators():
    schema = TOOLS[0]['function']
    assert schema['name'] == 'ProposeGitChange' and schema['parameters']['additionalProperties'] is False
    assert schema['parameters']['properties']['operation']['enum'] == list(OPERATIONS)
    assert set(schema['parameters']['properties']) == {'operation', 'paths', 'message', 'name', 'branch', 'remote',
                                                       'remote_branch', 'receipt_id', 'switch'}
    assert 'host broker' in schema['description'] and 'native review' in schema['description']
    for name in ('-x', 'a..b', 'a//b', 'a@{1}', 'x.lock', 'a/b.lock/c', 'x/', 'x.', 'HEAD', '.hidden', 'a/.b', 'a b',
                 'a~1', 'a^', 'a:b', 'a\\b', 'a*', 'a?', 'a[', '0' * 40, 'x' * 101, '', None, 7):
        with pytest.raises(PolicyError):
            branch_shape(name)
    assert branch_shape('feature/x-1.2_y') == 'feature/x-1.2_y'
    for url, code in (('https://user:secret@example.com/r.git', 'transport_unsupported'),
                      ('ssh://git@example.com/r.git', 'transport_unsupported'), ('git@example.com:r.git', 'transport_unsupported'),
                      ('git://example.com/r', 'transport_unsupported'), ('ext::sh -c touch% /tmp/x', 'transport_unsupported'),
                      ('../relative.git', 'remote_path_unsupported'), ('file://host/r.git', 'remote_path_unsupported'),
                      ('/a/../b.git', 'remote_path_unsupported'), ('file:///a/%2e%2e/b', 'remote_path_unsupported')):
        with pytest.raises(PolicyError, match=code) as caught:
            destination(url)
        assert 'secret' not in str(caught.value) and 'example.com' not in str(caught.value)
    assert 'Phase 14' in str(pytest.raises(PolicyError, destination, 'https://example.com/r').value)
    assert destination('/srv/r.git') == Path('/srv/r.git') and destination('file:///srv/r.git') == Path('/srv/r.git')
    data = f'ok\nkey = "{AWS}"\n-----BEGIN OPENSSH PRIVATE KEY-----\napi_key = "{"z" * 24}"\n{TOKEN}\n'.encode()
    found = scan('cfg.py', data)
    assert {(item['kind'], item['line']) for item in found} >= {('aws_access_key_id', 2), ('private_key', 3),
                                                                ('secret_assignment', 4), ('github_token', 5)}
    assert AWS not in json.dumps(found) and TOKEN not in json.dumps(found)
    assert [item['kind'] for item in scan('config/.env.local', b'')] == ['env_file']
    assert scan('src/app.py', b'password = short\n') == []
    assert message_arg('Subject') == 'Subject\n'
    for bad in ('', ' \nbody', 'x' * 201, 'a\0b', 'a\rb', 'x' * 4001, None):
        with pytest.raises(PolicyError):
            message_arg(bad)
    assert paths_arg('["b.txt", "a/./c.txt"]') == ['a/c.txt', 'b.txt']
    for bad in ('[]', 'not json', ['a', 'a'], ['../x'], ['/abs'], ['.env'], ['k/id.pem'], ['.git/config'],
                ['.gitmodules'], ['x'] * 65, [7]):
        with pytest.raises(PolicyError):
            paths_arg(bad)
    payload = {'plan_id': 'a' * 32, 'x': 1}
    assert plan_hash(payload) == plan_hash({**payload, 'plan_hash': 'ignored'})


@macos
def test_commit_selected_paths_only_preserves_other_work_exactly(host):
    repo, base = host.repo, rev(host.repo)
    (repo / 'app.py').write_text('print("selected")\n')
    (repo / 'README.md').write_text('# staged elsewhere\n')
    git(repo, 'add', 'README.md')
    (repo / 'notes.txt').write_text('unstaged elsewhere\n')
    (repo / 'scratch.txt').write_text('untracked\n')
    (repo / 'new.py').write_text('NEW = True\n')
    before_index, before_status = index(repo), status(repo)
    plan = host.plans.prepare(host.chat, 'commit', {'paths': '["app.py", "new.py"]', 'message': 'Selected change\n\nBody'})
    assert plan['operation'] == 'commit' and plan['requires_review'] is False and plan['destructive'] is False
    assert plan['hooks_run'] is False and plan['network'] is False and plan['sensitive_findings'] == []
    assert plan['authorization'] == 'none' and plan['plan_hash'] == plan_hash(plan)
    entries = {entry['path']: entry for entry in plan['bindings']['paths']}
    assert entries['app.py']['blob_oid'] == blob(b'print("selected")\n') and entries['new.py']['kind'] == 'add'
    assert entries['app.py']['sha256'] == hashlib.sha256(b'print("selected")\n').hexdigest()
    assert plan['bindings']['identity'] == {'name': 'Reviewed Author', 'email': 'author@example.invalid', 'source': 'repository'}
    assert index(repo) == before_index and rev(repo) == base  # prepare has no effect
    receipt = host.plans.execute(host.chat, plan, action_id='action-1', run_id='run-1')
    assert receipt['state'] == 'applied' and receipt['effect_started'] is True and receipt['readback']['verified'] is True
    new = rev(repo)
    assert new == receipt['result']['commit'] != base and rev(repo, 'HEAD^') == base
    assert git(repo, 'diff-tree', '-r', '--name-only', '--no-commit-id', 'HEAD').stdout.split() == ['app.py', 'new.py']
    tree = git(repo, 'ls-tree', '-r', 'HEAD').stdout
    assert f'100644 blob {blob(b"print(\"selected\")\n")}\tapp.py' in tree and f'blob {blob(b"NEW = True\n")}\tnew.py' in tree
    assert f'blob {blob(b"# Fixture\n")}\tREADME.md' in tree  # The staged README stayed out of the commit.
    author = git(repo, 'log', '-1', '--format=%an <%ae>|%cn <%ce>|%B').stdout
    assert author.startswith('Reviewed Author <author@example.invalid>|Reviewed Author <author@example.invalid>|Selected change\n\nBody')
    after = index(repo)
    assert [line for line in after.splitlines() if not line.endswith(('app.py', 'new.py'))] == \
        [line for line in before_index.splitlines() if not line.endswith(('app.py', 'new.py'))]
    assert status(repo) == 'M  README.md\n M notes.txt\n?? scratch.txt\n'
    assert before_status != status(repo) and (repo / 'notes.txt').read_text() == 'unstaged elsewhere\n'
    assert receipt['result']['index_sync'] == {'app.py': 'updated', 'new.py': 'updated'}
    stored = row(host, plan['plan_id'])
    assert stored['state'] == 'applied' and stored['action_id'] == 'action-1' and stored['run_id'] == 'run-1'
    view = host.plans.readback(host.chat, plan['plan_id'])
    assert view['verified'] is True and view['matches_receipt'] is True and view['matches_before'] is False
    assert view['authorization'] == 'none' and view['effect'] == 'none'
    listed = host.plans.receipts(host.chat)['items']
    assert [item['receipt_id'] for item in listed] == [plan['plan_id']] and listed[0]['state'] == 'applied'
    with pytest.raises(PolicyError, match='already executed'):
        host.plans.execute(host.chat, plan)


@macos
def test_identity_resolution_and_validation(host):
    (host.repo / 'app.py').write_text('print("identity")\n')
    git(host.repo, 'config', '--unset', 'user.name')
    with pytest.raises(PolicyError, match='Set user.name and user.email'):
        host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'x'})
    host.app.store.set_setting('git_identity', {'name': 'Settings Person', 'email': 'settings@example.invalid'})
    plan = host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'x'})
    assert plan['bindings']['identity']['source'] == 'settings'
    host.plans.execute(host.chat, plan)
    assert git(host.repo, 'log', '-1', '--format=%an <%ae>').stdout.strip() == 'Settings Person <settings@example.invalid>'
    for name in ('Evil <x>', 'Line\nBreak', ' padded', 'trailing.'):
        host.app.store.set_setting('git_identity', {'name': name, 'email': 'settings@example.invalid'})
        (host.repo / 'app.py').write_text(f'print({name!r})\n')
        with pytest.raises(PolicyError, match='Git author name'):
            host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'x'})


@macos
def test_hostile_hooks_and_hooks_path_never_run(host, monkeypatch):
    marker = host.tmp / 'hook-ran'
    script = f'#!/bin/sh\ntouch hook-ran-here\ntouch {marker}\nexit 1\n'
    names = ('pre-commit', 'post-commit', 'commit-msg', 'pre-push', 'post-checkout', 'post-merge', 'reference-transaction',
             'pre-merge-commit', 'post-index-change')
    for folder in (host.repo / '.git' / 'hooks', host.repo / '.husky'):
        folder.mkdir(exist_ok=True)
        for name in names:
            (folder / name).write_text(script)
            (folder / name).chmod(0o755)
    git(host.repo, 'add', '.husky')
    git(host.repo, 'commit', '-q', '-m', 'Add husky hooks')
    git(host.repo, 'config', 'core.hooksPath', '.husky')
    remote = bare(host)
    captured = []
    original = GitWorkflows._spawn

    def spy(self, argv, *args, **kwargs):
        captured.append([str(item) for item in argv])
        return original(self, argv, *args, **kwargs)

    monkeypatch.setattr(GitWorkflows, '_spawn', spy)
    (host.repo / 'app.py').write_text('print("hooked")\n')
    apply(host, 'commit', paths=['app.py'], message='Commit past hooks')
    apply(host, 'create_branch', name='topic', switch=True)
    (host.repo / 'topic.txt').write_text('topic\n')
    apply(host, 'commit', paths=['topic.txt'], message='Topic work')
    apply(host, 'switch_branch', name='main')
    apply(host, 'merge', name='topic')
    plan, receipt = apply(host, 'push', remote='origin', branch='main')
    assert receipt['state'] == 'applied' and git(remote, 'rev-parse', 'refs/heads/main').stdout.strip() == rev(host.repo)
    assert not marker.exists() and not (host.repo / 'hook-ran-here').exists() and not (remote / 'hook-ran-here').exists()
    trusted = str(git_workflows.confinement()['git'])
    assert captured and all(argv[0] == trusted and 'core.hooksPath=/dev/null' in argv for argv in captured)
    used = {subcommand(argv) for argv in captured}
    assert {'hash-object', 'commit-tree', 'update-ref', 'read-tree', 'bundle'} <= used
    assert not used & {'commit', 'push', 'checkout', 'switch', 'merge', 'reset', 'receive-pack', 'send-pack', 'ls-remote'}


@macos
@pytest.mark.parametrize('key,value', [
    ('includeIf.gitdir:/x/.path', '/tmp/pebble-plan-includeif'), ('core.fsmonitor', 'true'),
    ('credential.helper', 'helper-marker'), ('remote.origin.uploadpack', 'upload-marker'),
    ('remote.origin.receivepack', 'receive-marker'), ('url.ssh://evil/.insteadOf', '/srv'),
    ('commit.gpgSign', 'true'), ('filter.evil.clean', 'clean-marker'),
])
def test_hostile_repository_config_is_refused_by_the_allowlist(host, key, value):
    (host.repo / 'app.py').write_text('print("config")\n')
    plan = host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'Before hostile config'})
    git(host.repo, 'config', key, value)
    with pytest.raises(PolicyError, match='Unsupported repository Git configuration') as caught:
        host.plans.prepare(host.chat, 'stage', {'paths': ['app.py']})
    if value != 'true':
        assert value not in str(caught.value)
    base = rev(host.repo)
    with pytest.raises(PolicyError, match='Unsupported repository Git configuration'):
        host.plans.execute(host.chat, plan)
    stored = row(host, plan['plan_id'])
    assert stored['state'] == 'failed' and stored['receipt']['effect_started'] is False and rev(host.repo) == base


@macos
@pytest.mark.parametrize('drift', ['file', 'head', 'index', 'mode'])
def test_stale_plans_are_refused_before_any_effect(host, drift):
    repo = host.repo
    (repo / 'app.py').write_text('print("reviewed")\n')
    plan = host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'Reviewed bytes'})
    base, refs = rev(repo), git(repo, 'for-each-ref').stdout
    if drift == 'file':
        (repo / 'app.py').write_text('print("changed after review")\n')
    elif drift == 'head':
        (repo / 'README.md').write_text('# moved\n')
        git(repo, 'commit', '-q', '-m', 'Move HEAD after review', '--', 'README.md')
        base, refs = rev(repo), git(repo, 'for-each-ref').stdout
    elif drift == 'index':
        (repo / 'notes.txt').write_text('staged after review\n')
        git(repo, 'add', 'notes.txt')
    else:
        (repo / 'app.py').chmod(0o755)
    before = index(repo)
    with pytest.raises(PolicyError, match='changed after review') as caught:
        host.plans.execute(host.chat, plan)
    assert 'Nothing ran' in str(caught.value)
    stored = row(host, plan['plan_id'])
    assert stored['state'] == 'failed' and stored['receipt']['effect_started'] is False
    assert rev(repo) == base and git(repo, 'for-each-ref').stdout == refs and index(repo) == before


@macos
def test_tampered_or_foreign_payloads_are_refused(host):
    (host.repo / 'app.py').write_text('print("reviewed")\n')
    (host.repo / 'README.md').write_text('# other\n')
    plan = host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'Reviewed message'})
    base = rev(host.repo)
    for forged in ({**plan, 'message': 'Edited message\n'}, {**plan, 'paths': ['README.md']},
                   {**plan, 'requires_review': 'yes'}, {**plan, 'publication': True}, {**plan, 'plan_hash': '0' * 64},
                   {**plan, 'plan_version': 0}, {**plan, 'hooks_run': True}, json.dumps(plan), None, 'approved'):
        with pytest.raises(PolicyError):
            host.plans.execute(host.chat, forged)
    other = host.app.store.new_chat(str(host.repo))
    with pytest.raises(PolicyError, match='another conversation'):
        host.plans.execute(other, plan)
    assert rev(host.repo) == base and host.app.store.query('SELECT COUNT(*) AS n FROM git_plan_receipts')[0]['n'] == 0
    second = host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'Second review'})
    rehashed = {**second, 'paths': ['README.md']}
    rehashed['plan_hash'] = plan_hash(rehashed)
    with pytest.raises(PolicyError, match='changed after review'):
        host.plans.execute(host.chat, rehashed)  # A recomputed hash still meets re-derivation.
    assert row(host, second['plan_id'])['state'] == 'failed' and rev(host.repo) == base
    with pytest.raises(PolicyError, match='already executed'):
        host.plans.execute(host.chat, second)  # Any registered attempt retires the plan, like a review token.
    assert host.plans.execute(host.chat, plan)['state'] == 'applied'
    assert git(host.repo, 'log', '-1', '--format=%s').stdout.strip() == 'Reviewed message'


@macos
def test_broker_annotations_may_raise_but_never_lower_review(host):
    (host.repo / 'app.py').write_text('print("annotated")\n')
    plan = host.plans.prepare(host.chat, 'discard', {'paths': ['app.py']})
    assert plan['requires_review'] is True
    with pytest.raises(PolicyError, match='cannot drop its native review'):
        host.plans.execute(host.chat, {**plan, 'requires_review': False, 'permission_mode': 'full'})
    assert (host.repo / 'app.py').read_text() == 'print("annotated")\n'
    stored = {**host.plans.prepare(host.chat, 'commit', {'paths': '["app.py"]', 'message': 'Annotated', 'operation': 'commit',
                                                         'name': '', 'switch': False, 'branch': None}),
              'permission_mode': 'ask'}
    stored['requires_review'] = True  # As App.propose/coding_api store it after a hook review.
    receipt = host.plans.execute(host.chat, stored)
    assert receipt['state'] == 'applied' and receipt['broker'] == {'requires_review': True, 'permission_mode': 'ask'}
    with pytest.raises(PolicyError, match='does not match'):
        host.plans.prepare(host.chat, 'stage', {'paths': ['app.py'], 'operation': 'commit'})
    with pytest.raises(PolicyError, match='Unexpected argument for stage: name'):
        host.plans.prepare(host.chat, 'stage', {'paths': ['app.py'], 'name': 'x'})


@macos
def test_sensitive_content_is_refused_without_echoing_values(host):
    (host.repo / 'settings.py').write_text(f'AWS_KEY = "{AWS}"\nplain = 1\n')
    (host.repo / 'deploy.txt').write_text('-----BEGIN RSA PRIVATE KEY-----\nMIIB\n')
    for operation, args in (('stage', {'paths': ['settings.py', 'deploy.txt']}),
                            ('commit', {'paths': ['settings.py'], 'message': 'Leak'})):
        with pytest.raises(SensitiveContent) as caught:
            host.plans.prepare(host.chat, operation, args)
        text = str(caught.value)
        assert 'settings.py:1 aws_access_key_id' in text and AWS not in text
        assert all(set(item) == {'path', 'line', 'kind'} for item in caught.value.findings)
    assert status(host.repo) == '?? deploy.txt\n?? settings.py\n'
    # A secret committed and then removed outside Pebble is still refused on push.
    remote = bare(host)
    (host.repo / 'cfg.py').write_text(f'token = "{TOKEN}"\n')
    git(host.repo, 'add', 'cfg.py')
    git(host.repo, 'commit', '-q', '-m', 'Add token')
    (host.repo / 'cfg.py').write_text('token = None\n')
    git(host.repo, 'commit', '-qam', 'Remove token')
    with pytest.raises(SensitiveContent, match='cfg.py:1 github_token') as caught:
        host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main'})
    assert TOKEN not in str(caught.value)
    assert git(remote, 'rev-parse', 'refs/heads/main').stdout.strip() == rev(host.repo, 'HEAD~2')


@macos
def test_private_special_and_non_regular_paths_are_refused(host):
    (host.repo / '.env').write_text('TOKEN=x\n')
    (host.repo / 'keys').mkdir()
    (host.repo / 'keys' / 'deploy.pem').write_text('PEM\n')
    (host.repo / 'link.txt').symlink_to(host.repo / 'README.md')
    (host.repo / 'folder').mkdir()
    (host.repo / 'big.bin').write_bytes(b'0' * 2_000_001)
    for paths in (['.env'], ['keys/deploy.pem'], ['.git/config'], ['../outside'], ['.gitmodules'], ['sub/.gitmodules']):
        with pytest.raises(PolicyError, match='Private|outside|inside|submodule|.gitmodules'):
            host.plans.prepare(host.chat, 'stage', {'paths': paths})
    with pytest.raises(PolicyError, match='symbolic link'):
        host.plans.prepare(host.chat, 'stage', {'paths': ['link.txt']})
    with pytest.raises(PolicyError, match='not a regular file'):
        host.plans.prepare(host.chat, 'stage', {'paths': ['folder']})
    with pytest.raises(PolicyError, match='2,000,000-byte'):
        host.plans.prepare(host.chat, 'stage', {'paths': ['big.bin']})
    with pytest.raises(PolicyError, match='neither a file nor a tracked'):
        host.plans.prepare(host.chat, 'stage', {'paths': ['missing.txt']})
    with pytest.raises(PolicyError, match='Unexpected argument'):
        host.plans.prepare(host.chat, 'stage', {'paths': ['README.md'], 'message': 'extra'})
    with pytest.raises(PolicyError, match='Choose a Git plan operation'):
        host.plans.prepare(host.chat, 'rebase', {})
    with pytest.raises(PolicyError, match='Unexpected Git plan argument'):
        host.plans.tool(host.chat, 'ProposeGitChange', {'operation': 'stage', 'paths': '["README.md"]', 'force': True})
    with pytest.raises(PolicyError, match='Nothing to stage'):
        host.plans.tool(host.chat, 'ProposeGitChange', {'operation': 'stage', 'paths': '["README.md"]'})


@macos
def test_branch_names_are_validated_and_must_be_vacant(host):
    for name in ('-x', 'a..b', 'a//b', 'a@{1}', 'x.lock', 'x/', 'x.', 'HEAD', '.hidden', 'a/.b', 'a b', 'a~1', '0' * 40,
                 'x' * 101, 'a\nb'):
        with pytest.raises(PolicyError, match='plain branch name'):
            host.plans.prepare(host.chat, 'create_branch', {'name': name})
    for name in ('main', 'MAIN', 'main/sub'):
        with pytest.raises(PolicyError, match='already exists or conflicts'):
            host.plans.prepare(host.chat, 'create_branch', {'name': name})
    with pytest.raises(PolicyError, match='switch must be true or false'):
        host.plans.prepare(host.chat, 'create_branch', {'name': 'ok', 'switch': 'yes'})
    assert git(host.repo, 'for-each-ref', '--format=%(refname)').stdout == 'refs/heads/main\n'


@macos
def test_branch_create_switch_delete_and_undo(host):
    repo, base = host.repo, rev(host.repo)
    plan, receipt = apply(host, 'create_branch', name='topic', switch=True)
    assert plan['requires_review'] is False and receipt['state'] == 'applied'
    assert git(repo, 'symbolic-ref', 'HEAD').stdout.strip() == 'refs/heads/topic' and rev(repo, 'topic') == base
    (repo / 'topic.txt').write_text('topic\n')
    apply(host, 'commit', paths=['topic.txt'], message='Topic work')
    topic = rev(repo)
    (repo / 'app.py').write_text('print("dirty")\n')
    with pytest.raises(PolicyError, match='tracked path'):
        host.plans.prepare(host.chat, 'switch_branch', {'name': 'main'})
    git(repo, 'checkout', '-q', '--', 'app.py')
    (repo / 'untracked.txt').write_text('kept\n')
    plan, receipt = apply(host, 'switch_branch', name='main')
    assert git(repo, 'symbolic-ref', 'HEAD').stdout.strip() == 'refs/heads/main' and rev(repo) == base
    assert not (repo / 'topic.txt').exists() and (repo / 'untracked.txt').read_text() == 'kept\n'
    assert status(repo) == '?? untracked.txt\n'
    (repo / 'topic.txt').write_text('untracked collision\n')
    with pytest.raises(PolicyError, match='would be overwritten: topic.txt'):
        host.plans.prepare(host.chat, 'switch_branch', {'name': 'topic'})
    (repo / 'topic.txt').unlink()
    with pytest.raises(PolicyError, match='current branch'):
        host.plans.prepare(host.chat, 'delete_branch', {'name': 'main'})
    with pytest.raises(PolicyError, match='commits that HEAD does not contain'):
        host.plans.prepare(host.chat, 'delete_branch', {'name': 'topic'})
    apply(host, 'create_branch', name='merged')
    plan = host.plans.prepare(host.chat, 'delete_branch', {'name': 'merged'})
    assert plan['destructive'] is True and plan['requires_review'] is True and plan['bindings']['tip'] == base
    receipt = host.plans.execute(host.chat, plan)
    assert git(repo, 'rev-parse', '--verify', '--quiet', 'refs/heads/merged', check=False).returncode != 0
    undo = host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})
    assert undo['undo_of'] == receipt['receipt_id'] and undo['requires_review'] is False
    assert host.plans.execute(host.chat, undo)['state'] == 'applied' and rev(repo, 'merged') == base
    assert row(host, receipt['receipt_id'])['state'] == 'undone'
    with pytest.raises(PolicyError, match='Only applied'):
        host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})
    with pytest.raises(PolicyError, match='cannot itself be undone'):
        host.plans.prepare(host.chat, 'undo', {'receipt_id': undo['plan_id']})
    # Undo the switch back to topic, then undo creating a branch that is not current.
    plan, receipt = apply(host, 'switch_branch', name='topic')
    assert rev(repo) == topic and (repo / 'topic.txt').exists()
    apply(host, 'undo', receipt_id=receipt['receipt_id'])
    assert git(repo, 'symbolic-ref', 'HEAD').stdout.strip() == 'refs/heads/main' and not (repo / 'topic.txt').exists()
    plan, created = apply(host, 'create_branch', name='scratch')
    undo = host.plans.prepare(host.chat, 'undo', {'receipt_id': created['receipt_id']})
    assert undo['destructive'] is True and undo['requires_review'] is True
    host.plans.execute(host.chat, undo)
    assert git(repo, 'rev-parse', '--verify', '--quiet', 'refs/heads/scratch', check=False).returncode != 0


@macos
def test_fast_forward_three_way_and_conflicting_merges(host):
    repo, base = host.repo, rev(host.repo)
    git(repo, 'switch', '-q', '-c', 'ff')
    (repo / 'ff.txt').write_text('ff\n')
    git(repo, 'add', 'ff.txt')
    git(repo, 'commit', '-q', '-m', 'ff work')
    ff = rev(repo)
    git(repo, 'switch', '-q', 'main')
    plan, receipt = apply(host, 'merge', name='ff')
    assert plan['bindings']['merge_kind'] == 'fast_forward' and receipt['result']['commit'] == ff
    assert rev(repo) == ff and (repo / 'ff.txt').read_text() == 'ff\n' and status(repo) == ''
    plan = host.plans.prepare(host.chat, 'merge', {'name': 'ff'})
    assert plan['bindings']['merge_kind'] == 'up_to_date' and plan['effects'] == []
    assert host.plans.execute(host.chat, plan)['effect_started'] is False and rev(repo) == ff
    undo = host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})
    assert undo['requires_review'] is True
    host.plans.execute(host.chat, undo)
    assert rev(repo) == base and not (repo / 'ff.txt').exists() and status(repo) == ''
    # A clean three-way merge creates a reviewed merge commit with both parents.
    git(repo, 'switch', '-q', '-c', 'side')
    (repo / 'side.txt').write_text('side\n')
    git(repo, 'add', 'side.txt')
    git(repo, 'commit', '-q', '-m', 'side work')
    side = rev(repo)
    git(repo, 'switch', '-q', 'main')
    (repo / 'main.txt').write_text('main\n')
    git(repo, 'add', 'main.txt')
    git(repo, 'commit', '-q', '-m', 'main work')
    head = rev(repo)
    plan, receipt = apply(host, 'merge', name='side', message='Merge side for review')
    assert plan['bindings']['merge_kind'] == 'three_way' and plan['bindings']['merged_tree']
    merge = rev(repo)
    assert git(repo, 'rev-list', '--parents', '-n', '1', 'HEAD').stdout.split() == [merge, head, side]
    assert git(repo, 'log', '-1', '--format=%an|%s').stdout.strip() == 'Reviewed Author|Merge side for review'
    assert git(repo, 'rev-parse', 'HEAD^{tree}').stdout.strip() == plan['bindings']['merged_tree']
    assert (repo / 'side.txt').exists() and (repo / 'main.txt').exists() and status(repo) == ''
    with pytest.raises(PolicyError, match='Only fast-forward merges'):
        host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})
    # A conflicting merge is refused with its paths and leaves no merge state.
    git(repo, 'switch', '-q', '-c', 'clash')
    (repo / 'notes.txt').write_text('clash side\n')
    git(repo, 'commit', '-q', '-am', 'clash')
    git(repo, 'switch', '-q', 'main')
    (repo / 'notes.txt').write_text('main side\n')
    git(repo, 'commit', '-q', '-am', 'main clash')
    before, objects = rev(repo), sorted(os.listdir(repo / '.git' / 'objects'))
    with pytest.raises(PolicyError, match='conflicts in: notes.txt'):
        host.plans.prepare(host.chat, 'merge', {'name': 'clash'})
    assert rev(repo) == before and status(repo) == '' and not (repo / '.git' / 'MERGE_HEAD').exists()
    assert sorted(os.listdir(repo / '.git' / 'objects')) == objects  # Prepare wrote no objects.
    (repo / 'app.py').write_text('print("dirty")\n')
    with pytest.raises(PolicyError, match='tracked path'):
        host.plans.prepare(host.chat, 'merge', {'name': 'side'})


@macos
def test_merge_conflict_state_is_aborted_once_and_settles_uncertain(host, monkeypatch):
    repo = host.repo
    git(repo, 'switch', '-q', '-c', 'ff')
    (repo / 'ff.txt').write_text('ff\n')
    git(repo, 'add', 'ff.txt')
    git(repo, 'commit', '-q', '-m', 'ff work')
    git(repo, 'switch', '-q', 'main')
    plan = host.plans.prepare(host.chat, 'merge', {'name': 'ff'})
    original = GitPlans._apply_merge

    def leaves_state(self, plan, ctx):
        facts = original(self, plan, ctx)
        (repo / '.git' / 'MERGE_HEAD').write_text(rev(repo, 'ff') + '\n')
        return facts

    monkeypatch.setattr(GitPlans, '_apply_merge', leaves_state)
    with pytest.raises(GitPlanUncertain) as caught:
        host.plans.execute(host.chat, plan)
    abort = caught.value.receipt['recovery']['merge_abort']
    assert abort['attempted'] is True and caught.value.receipt['state'] == 'uncertain'
    assert row(host, plan['plan_id'])['state'] == 'uncertain' and not (repo / '.git' / 'MERGE_HEAD').exists()


@macos
def test_discard_preserves_blobs_and_undo_restores_bytes(host):
    repo = host.repo
    (repo / 'app.py').write_bytes(b'print("discard me")\n')
    (repo / 'app.py').chmod(0o640)
    (repo / 'README.md').unlink()
    (repo / 'notes.txt').write_text('staged\n')
    git(repo, 'add', 'notes.txt')
    with pytest.raises(PolicyError, match='no unstaged changes'):
        host.plans.prepare(host.chat, 'discard', {'paths': ['notes.txt']})
    (repo / 'loose.txt').write_text('untracked\n')
    with pytest.raises(PolicyError, match='tracked files only'):
        host.plans.prepare(host.chat, 'discard', {'paths': ['loose.txt']})
    plan = host.plans.prepare(host.chat, 'discard', {'paths': ['app.py', 'README.md']})
    assert plan['destructive'] is True and plan['requires_review'] is True
    previous = {item['path']: item['previous'] for item in plan['bindings']['paths']}
    assert previous['README.md'] is None and previous['app.py']['blob_oid'] == blob(b'print("discard me")\n')
    receipt = host.plans.execute(host.chat, plan)
    assert (repo / 'app.py').read_text() == 'print("base")\n' and (repo / 'README.md').read_text() == '# Fixture\n'
    assert git(repo, 'cat-file', 'blob', previous['app.py']['blob_oid']).stdout == 'print("discard me")\n'
    assert status(repo) == 'M  notes.txt\n?? loose.txt\n'
    undo = host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})
    assert undo['requires_review'] is False
    host.plans.execute(host.chat, undo)
    assert (repo / 'app.py').read_bytes() == b'print("discard me")\n' and not (repo / 'README.md').exists()
    assert oct((repo / 'app.py').stat().st_mode & 0o777) == oct(0o640)
    assert status(repo) == ' D README.md\n M app.py\nM  notes.txt\n?? loose.txt\n'
    plan, receipt = apply(host, 'discard', paths=['app.py'])
    (repo / 'app.py').write_text('newer work\n')
    with pytest.raises(PolicyError, match='changed after the discard'):
        host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})
    assert (repo / 'app.py').read_text() == 'newer work\n'


def remote_tip(remote, name='main'):
    result = git(remote, 'rev-parse', '--verify', '--quiet', 'refs/heads/' + name, check=False)
    return result.stdout.strip() or None


@macos
def test_push_to_local_bare_remote_reads_back_and_undo_restores_it(host):
    remote, old = bare(host), rev(host.repo)
    (host.repo / 'app.py').write_text('print("publish")\n')
    apply(host, 'commit', paths=['app.py'], message='Publish me')
    new = rev(host.repo)
    plan = host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main'})
    b = plan['bindings']
    assert plan['publication'] is True and plan['requires_review'] is True and plan['network'] is False
    assert (b['old_oid'], b['new_oid'], b['remote_ref'], b['remote_path']) == (old, new, 'refs/heads/main', str(remote))
    assert b['url_digest'] == hashlib.sha256(str(remote).encode()).hexdigest() and b['commits'] == 1
    assert remote_tip(remote) == old
    receipt = host.plans.execute(host.chat, plan)
    assert receipt['state'] == 'applied' and remote_tip(remote) == new
    assert git(remote, 'cat-file', '-t', new).stdout.strip() == 'commit'
    assert git(remote, 'fsck', '--connectivity-only', '--no-dangling').returncode == 0
    view = host.plans.readback(host.chat, receipt['receipt_id'])
    assert view['matches_receipt'] is True and {c['fact'] for c in view['expected']} >= {'remote_ref'}
    # New remote branch and remote_branch mapping.
    plan, receipt = apply(host, 'push', remote='origin', branch='main', remote_branch='published/main')
    assert plan['bindings']['old_oid'] is None and remote_tip(remote, 'published/main') == new
    undo = host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})
    assert undo['publication'] is True and undo['requires_review'] is True
    host.plans.execute(host.chat, undo)
    assert remote_tip(remote, 'published/main') is None and remote_tip(remote) == new
    # Undo of the first push is a lease-checked move back to the recorded old OID.
    first = [item for item in host.plans.receipts(host.chat)['items'] if item['operation'] == 'push'][-1]
    plan, receipt = apply(host, 'undo', receipt_id=first['receipt_id'])
    assert remote_tip(remote) == old and receipt['result']['to'] == old


@macos
def test_push_refuses_non_fast_forward_moved_remote_and_network_transports(host):
    remote = bare(host)
    other = host.tmp / 'other'
    git(host.tmp, 'clone', '-q', str(remote), str(other))
    (other / 'remote.txt').write_text('remote only\n')
    git(other, 'add', 'remote.txt')
    git(other, 'commit', '-q', '-m', 'Remote only')
    git(other, 'push', '-q', 'origin', 'main')
    moved = remote_tip(remote)
    (host.repo / 'app.py').write_text('print("local")\n')
    apply(host, 'commit', paths=['app.py'], message='Local only')
    with pytest.raises(PolicyError, match='non_fast_forward'):
        host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main', 'remote_branch': 'main'})
    plan = host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main', 'remote_branch': 'feature'})
    (other / 'feature.txt').write_text('raced\n')
    git(other, 'add', 'feature.txt')
    git(other, 'commit', '-q', '-m', 'Race the reviewed push')
    git(other, 'push', '-q', 'origin', 'HEAD:refs/heads/feature')
    raced = remote_tip(remote, 'feature')
    with pytest.raises(PolicyError, match='changed after review'):
        host.plans.execute(host.chat, plan)
    assert row(host, plan['plan_id'])['receipt']['effect_started'] is False
    assert remote_tip(remote, 'feature') == raced and remote_tip(remote) == moved
    for url in ('https://token:secret@example.com/r.git', 'ssh://git@example.com/r.git', 'git@example.com:r.git'):
        git(host.repo, 'remote', 'set-url', 'origin', url)
        with pytest.raises(PolicyError, match='transport_unsupported') as caught:
            host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main', 'remote_branch': 'x'})
        assert 'secret' not in str(caught.value) and 'example.com' not in str(caught.value)
    git(host.repo, 'remote', 'set-url', 'origin', str(remote))
    git(host.repo, 'remote', 'set-url', '--push', 'origin', 'https://example.com/r.git')
    with pytest.raises(PolicyError, match='transport_unsupported'):
        host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main', 'remote_branch': 'x'})
    with pytest.raises(PolicyError, match='not configured'):
        host.plans.prepare(host.chat, 'push', {'remote': 'missing', 'branch': 'main'})


@macos
def test_bare_remote_hooks_never_run_and_remote_shapes_are_checked(host, monkeypatch):
    remote, marker = bare(host), host.tmp / 'remote-hook-ran'
    for name in ('pre-receive', 'update', 'post-receive', 'reference-transaction', 'post-update'):
        (remote / 'hooks' / name).write_text(f'#!/bin/sh\ntouch {marker}\nexit 1\n')
        (remote / 'hooks' / name).chmod(0o755)
    (host.repo / 'app.py').write_text('print("hooked remote")\n')
    apply(host, 'commit', paths=['app.py'], message='Hooked remote')
    with pytest.raises(PolicyError, match='executable hooks'):
        host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main'})
    # Even with the hook refusal bypassed, the plumbing transfer never starts receive-pack.
    original = GitPlans._bare
    monkeypatch.setattr(GitPlans, '_bare', lambda self, ctx, path: lenient_facts(original, self, ctx, path))
    plan, receipt = apply(host, 'push', remote='origin', branch='main')
    assert receipt['state'] == 'applied' and remote_tip(remote) == rev(host.repo) and not marker.exists()
    monkeypatch.undo()
    for name in ('pre-receive', 'update', 'post-receive', 'reference-transaction', 'post-update'):
        (remote / 'hooks' / name).unlink()
    git(remote, 'config', 'receive.procReceiveRefs', 'refs/for')
    with pytest.raises(PolicyError, match='Unsupported remote Git configuration'):
        host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main', 'remote_branch': 'x'})
    git(remote, 'config', '--unset', 'receive.procReceiveRefs')
    work = host.tmp / 'work'
    git(host.tmp, 'init', '-q', str(work))
    for target, reason in ((work, 'working tree'), (host.tmp / 'missing.git', 'existing local folder')):
        git(host.repo, 'remote', 'set-url', 'origin', str(target))
        with pytest.raises(PolicyError, match=reason):
            host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main', 'remote_branch': 'x'})
    alias = host.tmp / 'alias.git'
    alias.symlink_to(remote, target_is_directory=True)
    git(host.repo, 'remote', 'set-url', 'origin', 'file://' + str(alias))
    with pytest.raises(PolicyError, match='canonical path'):
        host.plans.prepare(host.chat, 'push', {'remote': 'origin', 'branch': 'main', 'remote_branch': 'x'})


@macos
def test_conversion_attributes_remote_readback_and_remote_policies_fail_closed(host):
    (host.repo / '.gitattributes').write_text('*.txt text\n')
    git(host.repo, 'add', '.gitattributes')
    git(host.repo, 'commit', '-q', '-m', 'Text attributes')
    (host.repo / 'crlf.txt').write_bytes(b'one\r\ntwo\r\n')
    with pytest.raises(PolicyError, match='Git would convert crlf.txt'):
        host.plans.prepare(host.chat, 'stage', {'paths': ['crlf.txt']})
    (host.repo / 'crlf.txt').write_bytes(b'one\ntwo\n')
    assert host.plans.prepare(host.chat, 'stage', {'paths': ['crlf.txt']})['operation'] == 'stage'
    remote = bare(host)
    (host.repo / 'app.py').write_text('print("policy")\n')
    apply(host, 'commit', paths=['app.py'], message='Policy push')
    plan, receipt = apply(host, 'push', remote='origin', branch='main')
    git(remote, 'config', 'receive.denyNonFastForwards', 'true')
    with pytest.raises(PolicyError, match='denyNonFastForwards'):
        host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})
    git(remote, 'config', 'include.path', str(host.tmp / 'elsewhere.cfg'))
    view = host.plans.readback(host.chat, receipt['receipt_id'])
    remote_checks = [check for check in view['expected'] if check['fact'] == 'remote_ref']
    assert view['matches_receipt'] is False and remote_checks[0]['current'] == 'unavailable'
    assert remote_tip(remote) == rev(host.repo)


def lenient_facts(original, self, ctx, path):
    hooks = [hook for hook in (path / 'hooks').iterdir() if not hook.name.endswith('.sample')]
    for hook in hooks:
        hook.chmod(0o644)
    try:
        return original(self, ctx, path)
    finally:
        for hook in hooks:
            hook.chmod(0o755)


@macos
def test_undo_commit_is_soft_and_refused_after_the_ref_moves(host):
    repo, base = host.repo, rev(host.repo)
    (repo / 'app.py').write_text('print("undo me")\n')
    plan, receipt = apply(host, 'commit', paths=['app.py'], message='Undo me')
    committed, snapshot = rev(repo), index(repo)
    undo = host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})
    assert undo['destructive'] is True and undo['requires_review'] is True and undo['operation'] == 'undo'
    result = host.plans.execute(host.chat, undo)
    assert result['state'] == 'applied' and rev(repo) == base and index(repo) == snapshot
    assert status(repo) == 'M  app.py\n' and (repo / 'app.py').read_text() == 'print("undo me")\n'
    assert git(repo, 'cat-file', '-t', committed).stdout.strip() == 'commit'
    plan, receipt = apply(host, 'commit', paths=['app.py'], message='Commit again')
    (repo / 'README.md').write_text('# later\n')
    git(repo, 'commit', '-q', '-m', 'Later work', '--', 'README.md')
    with pytest.raises(PolicyError, match='no longer points at the reviewed commit'):
        host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})


@macos
def test_detached_and_unborn_heads(host, tmp_path):
    repo, base = host.repo, rev(host.repo)
    git(repo, 'switch', '-q', '--detach', base)
    (repo / 'app.py').write_text('print("detached")\n')
    with pytest.raises(PolicyError, match='HEAD is detached'):
        host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'x'})
    plan, receipt = apply(host, 'commit', paths=['app.py'], message='Rescue detached work', branch='rescue')
    assert git(repo, 'symbolic-ref', 'HEAD').stdout.strip() == 'refs/heads/rescue' and rev(repo, 'rescue^') == base
    apply(host, 'undo', receipt_id=receipt['receipt_id'])
    assert git(repo, 'symbolic-ref', '-q', 'HEAD', check=False).returncode != 0 and rev(repo) == base
    assert git(repo, 'rev-parse', '--verify', '--quiet', 'refs/heads/rescue', check=False).returncode != 0
    assert status(repo) == 'M  app.py\n'
    empty = tmp_path.resolve() / 'empty'
    empty.mkdir()
    git(empty, 'init', '-q')
    (empty / 'a.txt').write_text('a\n')
    chat = host.app.store.new_chat(str(empty))
    with pytest.raises(PolicyError, match='no commits yet'):
        host.plans.prepare(chat, 'stage', {'paths': ['a.txt']})


@macos
def test_stage_unstage_and_their_undo_touch_only_selected_entries(host):
    repo = host.repo
    (repo / 'app.py').write_text('print("stage me")\n')
    (repo / 'README.md').write_text('# staged elsewhere\n')
    git(repo, 'add', 'README.md')
    (repo / 'notes.txt').unlink()
    before = index(repo)
    plan, receipt = apply(host, 'stage', paths=['app.py', 'notes.txt'])
    assert {e['path']: e['kind'] for e in plan['bindings']['paths']} == {'app.py': 'add', 'notes.txt': 'delete'}
    assert status(repo) == 'M  README.md\nM  app.py\nD  notes.txt\n'
    assert git(repo, 'ls-files', '--stage', 'app.py').stdout.split()[1] == blob(b'print("stage me")\n')
    host.plans.execute(host.chat, host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']}))
    assert index(repo) == before and status(repo) == 'M  README.md\n M app.py\n D notes.txt\n'
    plan, receipt = apply(host, 'unstage', paths=['README.md'])
    assert status(repo) == ' M README.md\n M app.py\n D notes.txt\n' and (repo / 'README.md').read_text() == '# staged elsewhere\n'
    apply(host, 'undo', receipt_id=receipt['receipt_id'])
    assert index(repo) == before
    plan, receipt = apply(host, 'stage', paths=['app.py'])
    git(repo, 'add', 'notes.txt')
    (repo / 'app.py').write_text('print("restaged")\n')
    git(repo, 'add', 'app.py')
    with pytest.raises(PolicyError, match='changed after the stage'):
        host.plans.prepare(host.chat, 'undo', {'receipt_id': receipt['receipt_id']})


@macos
def test_restart_marks_executing_uncertain_without_retry(host, monkeypatch):
    (host.repo / 'app.py').write_text('print("crash")\n')
    plan = host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'Crash before settle'})

    class Crash(BaseException):
        pass

    original = GitPlans._settle

    def crash(self, receipt, state, **facts):
        if state == 'applied':
            raise Crash()
        return original(self, receipt, state, **facts)

    monkeypatch.setattr(GitPlans, '_settle', crash)
    with pytest.raises(Crash):
        host.plans.execute(host.chat, plan)
    monkeypatch.undo()
    committed = rev(host.repo)
    assert row(host, plan['plan_id'])['state'] == 'executing' and committed != plan['bindings']['head_commit']
    (host.repo / 'README.md').write_text('# second\n')
    second = host.plans.prepare(host.chat, 'stage', {'paths': ['README.md']})
    host.plans._insert(second, 'action-2', None)  # Registered, then the service died before any effect.
    calls = []
    spawn = GitWorkflows._spawn
    monkeypatch.setattr(GitWorkflows, '_spawn', lambda self, *args, **kwargs: calls.append(args) or spawn(self, *args, **kwargs))
    restarted = GitPlans(host.app, host.git)
    assert calls == []
    for ident in (plan['plan_id'], second['plan_id']):
        stored = row(host, ident)
        assert stored['state'] == 'uncertain' and stored['receipt']['interrupted'] == 'executing'
        with pytest.raises(PolicyError, match='already executed'):
            restarted.execute(host.chat, plan if ident == plan['plan_id'] else second)
    assert rev(host.repo) == committed and status(host.repo) == ' M README.md\n'
    view = restarted.readback(host.chat, plan['plan_id'])
    assert view['state'] == 'uncertain' and view['matches_receipt'] is True and view['matches_before'] is False
    assert restarted.readback(host.chat, second['plan_id'])['matches_receipt'] is None


@macos
def test_unproven_readback_after_an_effect_raises_uncertain(host, monkeypatch):
    (host.repo / 'app.py').write_text('print("unproven")\n')
    plan = host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'Unproven'})
    monkeypatch.setattr(GitPlans, '_prove_commit', lambda self, *args: (_ for _ in ()).throw(PolicyError('readback lost')))
    with pytest.raises(GitPlanUncertain) as caught:
        host.plans.execute(host.chat, plan)
    assert isinstance(caught.value, RuntimeError) and not isinstance(caught.value, PolicyError)
    receipt = caught.value.receipt
    assert receipt['state'] == 'uncertain' and receipt['effect_started'] is True and 'readback lost' in receipt['error']
    assert 'update-ref' in receipt['steps'] and receipt['recovery']['commit'] == rev(host.repo)
    assert row(host, plan['plan_id'])['state'] == 'uncertain'
    with pytest.raises(PolicyError, match='already executed'):
        host.plans.execute(host.chat, plan)
    with pytest.raises(PolicyError, match='Only applied'):
        host.plans.prepare(host.chat, 'undo', {'receipt_id': plan['plan_id']})


@macos
def test_failed_effect_that_readback_proves_unchanged_settles_failed(host, monkeypatch):
    (host.repo / 'app.py').write_text('print("cas")\n')
    plan = host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'CAS refusal'})
    base, original = rev(host.repo), GitPlans._step

    def refuse(self, ctx, label, args, **kwargs):
        if label == 'update-ref':
            args = [*args[:-1], '1' * 40]  # A stale old value: Git's compare-and-swap refuses.
        return original(self, ctx, label, args, **kwargs)

    monkeypatch.setattr(GitPlans, '_step', refuse)
    with pytest.raises(PolicyError, match='update-ref failed'):
        host.plans.execute(host.chat, plan)
    stored = row(host, plan['plan_id'])['receipt']
    assert stored['state'] == 'failed' and stored['effect_started'] is True and stored['proved_unchanged'] is True
    assert rev(host.repo) == base


@macos
def test_writer_lock_cancellation_and_replaced_root_refuse_before_effect(host, tmp_path):
    import shutil
    import threading
    (host.repo / 'app.py').write_text('print("locked")\n')
    plan = host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'Locked'})
    held = host.git._writer(host.git._repository(host.chat))
    try:
        with pytest.raises(PolicyError, match='Another managed worktree operation'):
            host.plans.execute(host.chat, plan)
    finally:
        held.release()
    assert row(host, plan['plan_id'])['receipt']['effect_started'] is False
    stop = threading.Event()
    stop.set()
    plan = host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'Cancelled'})
    with pytest.raises(InterruptedError):
        host.plans.execute(host.chat, plan, cancel=stop)
    assert row(host, plan['plan_id'])['state'] == 'failed'
    plan = host.plans.prepare(host.chat, 'commit', {'paths': ['app.py'], 'message': 'Replaced root'})
    base = rev(host.repo)
    moved = tmp_path.resolve() / 'moved'
    os.rename(host.repo, moved)
    shutil.copytree(moved, host.repo, symlinks=True)
    with pytest.raises(PolicyError, match='changed after review'):
        host.plans.execute(host.chat, plan)
    assert rev(host.repo) == base and rev(moved) == base
