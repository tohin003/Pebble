"""Child worktree ownership ledger, allocation payloads and the write adapter.

The App, the durable agent tree and its leases, the root run journal,
GitWorkflows and its sandbox-exec confined Git on real temporary repositories
are the checked-in implementations. The child's Read goes through
Agents.child_tool with a recording AgentGuard admission that allows. The root
model is never called and the child scheduler is disabled, so the test thread
acts as the worker. Fixture Git commands are unconfined test-author commands
inside pytest temporary folders only. No hook, credential, network, remote or
real project repository is used.
"""

import copy
import hashlib
import importlib.util
import json
import stat
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from pebble import child_worktrees
from pebble.agent_manager import Agents
from pebble.app import App
from pebble.child_worktrees import TOOL_ARGS, TOOLS, ChildWorktrees
from pebble.git_workflows import GitWorkflows
from pebble.permissions import DEFAULT
from pebble.policy import MAX_FILE, PolicyError, Workspace

MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
macos = pytest.mark.skipif(not MAC, reason='Confined Git workflows are a macOS sandbox-exec product surface.')
PROFILE = {'id': 'fixture-cloud', 'kind': 'openai', 'name': 'Fixture provider', 'url': 'https://unused.invalid',
           'model': 'fixture', 'context': 8192, 'budget': 4096, 'tools': True}
LIMITS = {'max_steps': 4, 'max_seconds': 30, 'model_tokens': 16384}
BASE = b'print("base")\n'


class ProcessDied(BaseException):
    """Fault injection: the service stops mid-effect, before any outcome commits."""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(cwd, *args, check=True):
    cwd = Path(cwd).resolve()
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/var/empty', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', 'GIT_CEILING_DIRECTORIES': str(cwd.parent),
           'GIT_AUTHOR_NAME': 'Fixture Author', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
           'GIT_COMMITTER_NAME': 'Fixture Author', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
    process = subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'init.defaultBranch=main',
                              '-c', 'commit.gpgSign=false', '-c', 'core.fsmonitor=false', *args],
                             cwd=cwd, env=env, capture_output=True, timeout=60)
    if check and process.returncode:
        raise AssertionError(f'fixture git {args} failed: {process.stderr!r}')
    return process


def make_repo(path):
    path.mkdir()
    git(path, 'init', '-q')
    (path / 'app.py').write_bytes(BASE)
    (path / 'README.md').write_text('# Fixture\n')
    (path / 'tool.sh').write_text('#!/bin/sh\necho base\n')
    (path / 'tool.sh').chmod(0o755)
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'Initial fixture commit')
    return path.resolve()


def head(repo):
    return git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()


def parallel_siblings(monkeypatch):
    """Inert placeholders for sibling Phase 13 modules still written in parallel.

    Only a module whose source is missing is replaced, for one test; once it
    exists the real module loads. These cases never reach Git plans or managed
    environments.
    """
    for name, owner in (('pebble.git_plans', 'GitPlans'), ('pebble.environments', 'Environments')):
        if importlib.util.find_spec(name) is None:
            module = types.ModuleType(name)
            setattr(module, owner, type(owner, (), {'__init__': lambda self, *args: None}))
            monkeypatch.setitem(sys.modules, name, module)


class Host:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.monkeypatch = tmp_path.resolve(), monkeypatch
        monkeypatch.setattr(Agents, 'start', lambda self: None)
        parallel_siblings(monkeypatch)
        self.project = make_repo(self.tmp / 'project')
        self.home, self.guard_calls, self.root_calls = self.tmp / 'data', [], []
        self.boot()
        self.permissions()
        self.chat = self.app.new_chat(str(self.project))['id']

    def boot(self):
        self.app = App(self.home, self.tmp / 'resources', lambda *args: None)
        self.monkeypatch.setattr(self.app.model, 'current', lambda: copy.deepcopy(PROFILE))
        self.monkeypatch.setattr(self.app.model, 'complete', self.root_complete)
        self.monkeypatch.setattr(self.app.guard, 'handle', self.guard)
        # The parent's App wiring owns one instance; construct it only where absent.
        self.cw = getattr(self.app, 'child_worktrees', None) or ChildWorktrees(self.app, self.app.git)
        assert self.cw.guard in self.app.git.guards
        self.tree = self.app.agents.tree

    def root_complete(self, *args, **kwargs):
        self.root_calls.append(args)
        raise AssertionError('Child worktree fixtures must not make a root model request.')

    def guard(self, event, root, session, **kwargs):
        self.guard_calls.append({'event': event, 'root': str(root), 'tool': kwargs.get('tool')})
        return {'action': 'allow', 'reason': 'Fixture Guard admission'}

    def permissions(self, **tools):
        self.app.store.set_setting('permissions', {'mode': 'ask', 'tools': {**DEFAULT['tools'], **tools}})

    def root(self):
        app = self.app
        for live in app.runs.list():
            if live['state'] in {'queued', 'running', 'retry_wait'}:
                app.runs.transition(live['id'], 'waiting_input')
        chat = app.store.query('SELECT * FROM chats WHERE id=?', (self.chat,))[0]
        config = app.runtime_config(chat, False, {'max_steps': 64, 'max_seconds': 300})
        rid = app.runs.create(self.chat, 'Coordinate child worktree work.', config)
        app.runs.transition(rid, 'running')
        return rid

    def child(self, rid, name='writer', claim=True):
        node = self.tree.spawn(self.chat, rid, None, 'Complete the scoped ' + name + ' task.', name, ['Read'], dict(LIMITS))
        worker = {'cid': self.chat, 'rid': rid, 'id': node['id'], 'generation': 1, 'owner': 'worker-' + name,
                  'cancel': threading.Event(), 'workspace': None}
        if claim:
            self.tree.claim(self.chat, rid, node['id'], 1, worker['owner'])
        return worker

    def allocate(self, rid, worker, action_id=None):
        payload = self.cw.propose(self.chat, rid, worker['id'], worker['generation'])
        return payload, self.cw.allocate(payload, action_id or 'action-' + worker['id'][:8])

    def ready(self, name='writer'):
        """A running root and one child that is claimed only after its worktree is active."""
        rid = self.root()
        worker = self.child(rid, name, claim=False)
        _, receipt = self.allocate(rid, worker)
        self.tree.claim(self.chat, rid, worker['id'], 1, worker['owner'])
        worker['workspace'] = self.cw.workspace(worker)
        return rid, worker, Path(receipt['path'])

    def read(self, worker, path, call_id):
        """The child's own Read: running receipt, the real adapter on its worktree, committed outcome."""
        identity = (worker['cid'], worker['rid'], worker['id'], worker['generation'], worker['owner'], call_id)
        assert self.tree.begin_tool(*identity, 'Read', {'file_path': path}) is None
        result = self.app.agents.child_tool(worker, 'Read', {'file_path': path}, call_id)
        return self.tree.end_tool(*identity, result)

    def write(self, worker, path, content, call_id, **extra):
        return self.cw.write(worker, 'WriteWorktreeFile', {'file_path': path, 'content': content, **extra}, call_id)

    def edit(self, worker, path, old, new, call_id, **extra):
        return self.cw.write(worker, 'EditWorktreeFile', {'file_path': path, 'old_string': old, 'new_string': new, **extra}, call_id)

    def settle(self, worker, evidence):
        return self.tree.settle(worker['cid'], worker['rid'], worker['id'], worker['generation'], 'completed',
                                {'text': 'Done in my worktree.'}, evidence, worker['owner'])

    def accept(self, worker, evidence):
        return self.tree.review(worker['cid'], worker['rid'], worker['rid'], worker['id'], worker['generation'], evidence)

    def row(self, ident):
        return self.app.store.query('SELECT * FROM agent_worktrees WHERE worktree_id=?', (ident,))[0]

    def git_row(self, ident):
        rows = self.app.store.query('SELECT * FROM git_worktrees WHERE id=?', (ident,))
        return rows[0] if rows else None

    def writes(self):
        return self.app.store.query('SELECT count(*) AS n FROM agent_worktree_writes')[0]['n']

    def project_state(self, names=('app.py', 'README.md', 'tool.sh', 'scratch.txt')):
        index = (self.project / '.git' / 'index').read_bytes()
        status = git(self.project, '--no-optional-locks', 'status', '--porcelain=v2', '-z', '--untracked-files=all').stdout
        return {'index': index, 'status': status, 'head': (self.project / '.git' / 'HEAD').read_bytes(),
                'files': {name: (self.project / name).read_bytes() for name in names if (self.project / name).exists()}}

    def restart(self):
        self.app.close()
        self.boot()

    def close(self):
        self.app.close()


@pytest.fixture
def host(tmp_path, monkeypatch):
    if not MAC:
        pytest.skip('Confined Git workflows are a macOS sandbox-exec product surface.')
    made = Host(tmp_path, monkeypatch)
    yield made
    made.close()
    assert made.root_calls == []


def test_write_tools_are_closed_schemas_and_paths_stay_outside_metadata():
    assert [item['function']['name'] for item in TOOLS] == ['WriteWorktreeFile', 'EditWorktreeFile']
    for item in TOOLS:
        function = item['function']
        assert function['parameters']['additionalProperties'] is False
        assert 'only in that worktree, never in the project' in function['description']
        assert 'the root reviews it and integrates it into the project' in function['description']
    assert TOOL_ARGS == {'WriteWorktreeFile': {'file_path', 'content', 'expected_hash'},
                         'EditWorktreeFile': {'file_path', 'old_string', 'new_string', 'expected_hash'}}
    assert TOOLS[1]['function']['parameters']['required'] == ['file_path', 'old_string', 'new_string']
    for bad in ('.git', '.git/config', 'src/.git/HEAD', '.GIT/HEAD', '.gitmodules', 'pkg/.GitModules', '.gitattributes',
                'pkg/.GITATTRIBUTES', '.env', '.env.local',
                'keys/server.pem', 'secrets/token.txt', '../escape.py', '/tmp/abs.py', 'a//b.py', './a.py', 'a\\b.py', '', None):
        with pytest.raises(PolicyError):
            child_worktrees.target(bad)
    assert child_worktrees.target('pkg/new_module.py') == 'pkg/new_module.py'


@macos
def test_allocation_is_a_detached_checkout_that_leaves_dirty_project_work_untouched(host):
    base = head(host.project)
    (host.project / 'app.py').write_text('print("dirty project work")\n')
    (host.project / 'scratch.txt').write_text('untracked project notes\n')
    (host.project / 'README.md').write_text('# Staged project change\n')
    git(host.project, 'add', 'README.md')
    before = host.project_state()
    rid = host.root()
    worker = host.child(rid, claim=False)
    payload = host.cw.propose(host.chat, rid, worker['id'], 1)
    ident = payload['worktree_id']
    assert json.loads(json.dumps(payload)) == payload
    assert set(payload) == {'operation', 'run_id', 'agent_id', 'generation', 'worktree_id', 'root', 'root_identity',
                            'fingerprint', 'refs', 'base_commit', 'path', 'effects', 'disclosure', 'requires_review'}
    assert payload['operation'] == 'allocate_child_worktree' and payload['requires_review'] is False
    assert (payload['run_id'], payload['agent_id'], payload['generation']) == (rid, worker['id'], 1)
    assert payload['base_commit'] == base and payload['root'] == str(host.project)
    assert payload['path'] == str(host.app.git.anchor / ident)
    assert payload['disclosure'].startswith('Detached checkout at the exact HEAD commit; uncommitted project changes are not copied;')
    assert host.row(ident)['state'] == 'proposed' and host.row(ident)['action_id'] is None
    assert host.git_row(ident) is None and not Path(payload['path']).exists()
    for forged in ({**payload, 'base_commit': '0' * 40}, {**payload, 'refs': '0' * 64}, {**payload, 'generation': 2},
                   {**payload, 'approved': True}, {**payload, 'requires_review': 'no'},
                   {key: value for key, value in payload.items() if key != 'effects'}):
        with pytest.raises(PolicyError, match='does not match'):
            host.cw.allocate(forged, 'action-forged')
    assert host.row(ident)['state'] == 'proposed'

    # The broker stores the payload with its own metadata (App.propose adds permission_mode).
    stored = {**payload, 'permission_mode': 'ask'}
    receipt = host.cw.allocate(stored, 'action-1')
    tree = Path(receipt['path'])
    assert receipt['state'] == 'active' and receipt['authorization'] == 'none' and receipt['origin'] == 'child_worktree'
    assert receipt['dirty_work_copied'] is False and receipt['detached'] is True and receipt['branch_created'] is False
    assert receipt['parent_status_unchanged'] is True and receipt['parent_index_unchanged'] is True
    assert (tree / 'app.py').read_bytes() == BASE and (tree / 'README.md').read_text() == '# Fixture\n'
    assert not (tree / 'scratch.txt').exists()
    assert (host.project / '.git' / 'worktrees' / ident / 'HEAD').read_text().strip() == base
    assert host.project_state() == before
    assert host.row(ident)['state'] == 'active' and host.row(ident)['action_id'] == 'action-1'
    assert host.git_row(ident)['state'] == 'active'
    # The same approved action reads back its receipt; nothing runs again.
    assert host.cw.allocate(stored, 'action-1') == receipt
    with pytest.raises(PolicyError, match='no longer pending'):
        host.cw.allocate(stored, 'action-2')
    with pytest.raises(PolicyError, match='does not match'):
        host.cw.allocate({**stored, 'path': str(host.tmp / 'elsewhere')}, 'action-1')
    assert sorted(path.name for path in host.app.git.anchor.iterdir()) == [ident]
    public = host.cw.binding(rid, worker['id'])
    assert public['state'] == 'active' and public['writes'] == 0 and public['authorization'] == 'none'
    assert [item['worktree_id'] for item in host.cw.bindings(host.chat, rid)] == [ident]
    assert host.cw.binding(rid, 'unknown-agent') is None
    assert host.cw.changes(host.chat, rid, worker['id'])['clean'] is True


@macos
def test_proposal_refusals_cap_duplicates_and_root_state(host):
    rid = host.root()
    first = host.child(rid, 'first', claim=False)
    with pytest.raises(PolicyError, match='stale'):
        host.cw.propose(host.chat, rid, first['id'], 2)
    with pytest.raises(PolicyError, match='not found'):
        host.cw.propose(host.chat, rid, 'missing-agent', 1)
    with pytest.raises(PolicyError, match='Root run not found'):
        host.cw.propose('another-chat', rid, first['id'], 1)
    stopped = host.child(rid, 'stopped', claim=False)
    host.tree.cancel(host.chat, rid, rid, stopped['id'])
    with pytest.raises(PolicyError, match='settled or stopped'):
        host.cw.propose(host.chat, rid, stopped['id'], 1)

    host.cw.propose(host.chat, rid, first['id'], 1)
    with pytest.raises(PolicyError, match='already has'):
        host.cw.propose(host.chat, rid, first['id'], 1)
    peers = [host.child(rid, 'peer' + str(index), claim=False) for index in range(4)]
    proposals = [host.cw.propose(host.chat, rid, peer['id'], 1) for peer in peers[:3]]
    with pytest.raises(PolicyError, match='at most 4 unsettled'):
        host.cw.propose(host.chat, rid, peers[3]['id'], 1)
    rejected = host.cw.reject(proposals[0]['worktree_id'], 'Declined in native review.')
    assert rejected['state'] == 'rejected' and rejected['evidence']['reason'] == 'Declined in native review.'
    assert host.cw.reject(proposals[0]['worktree_id'], 'Again.')['state'] == 'rejected'
    with pytest.raises(PolicyError, match='no longer pending'):
        host.cw.allocate(proposals[0], 'action-rejected')
    last = host.cw.propose(host.chat, rid, peers[3]['id'], 1)

    late = host.child(rid, 'late', claim=False)
    host.app.runs.transition(rid, 'waiting_input')
    with pytest.raises(PolicyError, match='running root'):
        host.cw.propose(host.chat, rid, late['id'], 1)
    # A proposal staged while running cannot allocate once the root stopped running.
    with pytest.raises(PolicyError, match='running root'):
        host.cw.allocate(last, 'action-late')
    assert host.row(last['worktree_id'])['state'] == 'failed' and host.git_row(last['worktree_id']) is None
    assert not host.app.git.anchor.exists() or not any(host.app.git.anchor.iterdir())


@macos
def test_proposal_and_allocation_require_files_and_edits(host):
    host.permissions(edits=False)
    rid = host.root()
    worker = host.child(rid, claim=False)
    with pytest.raises(PolicyError, match='edits access in the root permission snapshot'):
        host.cw.propose(host.chat, rid, worker['id'], 1)
    host.permissions()  # A later setting never rewrites this root's snapshot.
    with pytest.raises(PolicyError, match='edits access in the root permission snapshot'):
        host.cw.propose(host.chat, rid, worker['id'], 1)
    assert host.app.store.query('SELECT count(*) AS n FROM agent_worktrees')[0]['n'] == 0

    rid = host.root()
    worker = host.child(rid, claim=False)
    payload = host.cw.propose(host.chat, rid, worker['id'], 1)
    host.permissions(edits=False)  # Revoked in Permissions after the proposal.
    with pytest.raises(PolicyError, match='edits access'):
        host.cw.allocate(payload, 'action-revoked')
    assert host.row(payload['worktree_id'])['state'] == 'failed' and host.git_row(payload['worktree_id']) is None
    assert not Path(payload['path']).exists()


@macos
def test_head_or_branch_drift_after_proposal_is_refused_before_any_effect(host):
    rid = host.root()
    moved_head, moved_refs = host.child(rid, 'head', claim=False), host.child(rid, 'refs', claim=False)
    first = host.cw.propose(host.chat, rid, moved_head['id'], 1)
    second = host.cw.propose(host.chat, rid, moved_refs['id'], 1)
    git(host.project, 'branch', 'feature')
    with pytest.raises(PolicyError, match='changed after this proposal'):
        host.cw.allocate(second, 'action-refs')
    (host.project / 'app.py').write_text('print("next")\n')
    git(host.project, 'commit', '-q', '-am', 'Move HEAD')
    with pytest.raises(PolicyError, match='changed after this proposal'):
        host.cw.allocate(first, 'action-head')
    for payload in (first, second):
        ident = payload['worktree_id']
        assert host.row(ident)['state'] == 'failed' and 'Nothing was created' in host.row(ident)['evidence']
        assert host.git_row(ident) is None and not Path(payload['path']).exists()
        assert not (host.project / '.git' / 'worktrees' / ident).exists()


@macos
def test_write_and_edit_land_only_in_the_child_worktree(host):
    rid, worker, tree = host.ready()
    before = host.project_state()
    created = host.write(worker, 'pkg/feature.py', 'VALUE = 1\n', 'w1')
    assert created == {'worktree_id': tree.name, 'path': 'pkg/feature.py', 'call_id': 'w1', 'generation': 1,
                       'operation': 'create', 'before_hash': None, 'after_hash': sha(b'VALUE = 1\n'), 'bytes': 10,
                       'unchanged': False, 'project_changed': False, 'origin': 'child_worktree', 'authorization': 'none',
                       'note': child_worktrees.NOTE}
    assert (tree / 'pkg' / 'feature.py').read_bytes() == b'VALUE = 1\n'
    assert stat.S_IMODE((tree / 'pkg' / 'feature.py').stat().st_mode) == 0o644
    read = host.read(worker, 'app.py', 'r1')
    assert read['file_hash'] == sha(BASE) and 'base' in read['untrusted_source']
    edited = host.edit(worker, 'app.py', '"base"', '"child"', 'e1')
    assert edited['operation'] == 'replace' and edited['before_hash'] == sha(BASE)
    assert (tree / 'app.py').read_bytes() == b'print("child")\n'
    script = host.read(worker, 'tool.sh', 'r2')
    mode = stat.S_IMODE((tree / 'tool.sh').stat().st_mode)
    assert mode & 0o100  # Checked out executable (under the service umask).
    host.write(worker, 'tool.sh', '#!/bin/sh\necho child\n', 'w2', expected_hash=script['file_hash'])
    assert stat.S_IMODE((tree / 'tool.sh').stat().st_mode) == mode
    # The generation's own write vouches for exactly the bytes it wrote.
    host.edit(worker, 'pkg/feature.py', 'VALUE = 1', 'VALUE = 2', 'e2')
    with pytest.raises(PolicyError, match='matched 0 times'):
        host.edit(worker, 'app.py', 'missing text', 'x', 'e3')
    host.write(worker, 'twice.py', 'x = 1\nx = 1\n', 'w3')
    with pytest.raises(PolicyError, match='matched 2 times'):
        host.edit(worker, 'twice.py', 'x = 1', 'x = 2', 'e4')
    with pytest.raises(PolicyError, match='existing file'):
        host.edit(worker, 'absent.py', 'a', 'b', 'e5')
    assert host.project_state() == before
    assert not (host.project / 'pkg').exists() and not (host.project / 'twice.py').exists()

    changes = host.cw.changes(host.chat, rid, worker['id'])
    assert changes['clean'] is False and changes['change_count'] == 4 and changes['private_omitted'] == 0
    assert sorted(changes['changes'], key=lambda item: item['path']) == [
        {'path': 'app.py', 'kind': 'modified'}, {'path': 'pkg/feature.py', 'kind': 'untracked'},
        {'path': 'tool.sh', 'kind': 'modified'}, {'path': 'twice.py', 'kind': 'untracked'}]
    assert 'VALUE' not in json.dumps(changes) and changes['authorization'] == 'none'
    view = host.cw.workspace(worker)
    assert view.root == tree and view.snapshot('pkg/feature.py') == b'VALUE = 2\n'
    with pytest.raises(PolicyError):
        view.snapshot('.git')
    public = host.cw.binding(rid, worker['id'])
    assert (public['writes'], public['changed_paths']) == (5, 4) and public['bytes_written'] == 10 + 15 + 21 + 10 + 12


@macos
def test_replacing_a_file_needs_a_fresh_read_of_its_exact_bytes(host):
    rid, worker, tree = host.ready()
    with pytest.raises(PolicyError, match='Read the current file'):
        host.write(worker, 'app.py', 'print("blind")\n', 'w1')
    first = host.read(worker, 'app.py', 'r1')
    (tree / 'app.py').write_text('print("changed underneath")\n')
    with pytest.raises(PolicyError, match='Read the current file'):
        host.write(worker, 'app.py', 'print("stale")\n', 'w2')
    second = host.read(worker, 'app.py', 'r2')
    with pytest.raises(PolicyError, match='expected_hash'):
        host.write(worker, 'app.py', 'print("stale")\n', 'w3', expected_hash=first['file_hash'])
    with pytest.raises(PolicyError, match='expected_hash must be'):
        host.write(worker, 'app.py', 'print("x")\n', 'w4', expected_hash='not-a-hash')
    assert host.write(worker, 'app.py', 'print("fresh")\n', 'w5', expected_hash=second['file_hash'])['before_hash'] == second['file_hash']
    with pytest.raises(PolicyError, match='expected_hash'):
        host.write(worker, 'new.py', 'y = 1\n', 'w6', expected_hash=second['file_hash'])
    assert host.write(worker, 'new.py', 'y = 1\n', 'w7', expected_hash='absent')['operation'] == 'create'
    assert host.writes() == 2 and (host.project / 'app.py').read_bytes() == BASE


@macos
def test_private_git_metadata_and_symlink_escapes_are_refused(host):
    rid, worker, tree = host.ready()
    outside = host.tmp / 'outside'
    outside.mkdir()
    (outside / 'victim.py').write_text('safe\n')
    (tree / 'linked').symlink_to(outside, target_is_directory=True)
    (tree / 'link.py').symlink_to(outside / 'victim.py')
    control = (tree / '.git').read_bytes()
    paths = ('.git', '.git/config', 'pkg/.git/HEAD', '.gitmodules', '.env', 'secrets/token.txt', 'server.pem',
             'linked/victim.py', 'linked/new.py', 'link.py', 'app.py/inner.py', '../escape.py', '/tmp/abs.py')
    for index, path in enumerate(paths):
        with pytest.raises(PolicyError):
            host.write(worker, path, 'pwned\n', 'w' + str(index))
    assert (outside / 'victim.py').read_text() == 'safe\n' and sorted(p.name for p in outside.iterdir()) == ['victim.py']
    assert (tree / '.git').read_bytes() == control and not (tree / '.gitmodules').exists() and not (tree / '.env').exists()
    view = host.cw.workspace(worker)
    for path in ('linked/victim.py', 'link.py', '.git'):
        with pytest.raises(PolicyError):
            view.snapshot(path)
    with pytest.raises(PolicyError, match='Credentials'):
        host.write(worker, 'cfg.py', 'KEY = "sk-' + 'a' * 30 + '"\n', 'secret')
    with pytest.raises(PolicyError, match='UTF-8'):
        host.write(worker, 'bad.py', 'x = "\ud800"\n', 'surrogate')
    with pytest.raises(PolicyError, match='96 KB|bounded'):
        host.write(worker, 'big.py', 'a' * (MAX_FILE + 1), 'big')
    with pytest.raises(PolicyError, match='Unexpected or missing'):
        host.cw.write(worker, 'WriteWorktreeFile', {'file_path': 'x.py', 'content': 'x', 'authorization': 'granted'}, 'extra')
    with pytest.raises(PolicyError, match='not a child worktree write tool'):
        host.cw.write(worker, 'Write', {'file_path': 'x.py', 'content': 'x'}, 'other')
    assert host.writes() == 0


@macos
def test_stale_generation_impostor_or_expired_lease_cannot_write(host):
    rid, worker, tree = host.ready()
    host.write(worker, 'one.py', '1\n', 'w1')
    with pytest.raises(PolicyError, match='no longer owns'):
        host.write({**worker, 'owner': 'another-worker'}, 'two.py', '2\n', 'w2')
    with pytest.raises(PolicyError, match='does not own'):
        host.write({**worker, 'generation': 2}, 'two.py', '2\n', 'w3')
    host.tree.heartbeat(host.chat, rid, worker['id'], 1, worker['owner'], lease_seconds=1)
    time.sleep(1.2)
    with pytest.raises(PolicyError, match='no longer owns'):
        host.write(worker, 'two.py', '2\n', 'w4')
    with pytest.raises(PolicyError, match='no longer owns'):
        host.cw.workspace(worker)
    host.tree.abandon(host.chat, rid, worker['id'], 1, worker['owner'])
    host.tree.retry(host.chat, rid, rid, worker['id'])
    retried = {**worker, 'generation': 2, 'owner': 'worker-retry'}
    host.tree.claim(host.chat, rid, worker['id'], 2, retried['owner'])
    with pytest.raises(PolicyError, match='does not own'):
        host.write(retried, 'two.py', '2\n', 'w5')
    with pytest.raises(PolicyError, match='does not own'):
        host.cw.workspace(retried)
    with pytest.raises(PolicyError, match='stale'):
        host.write(worker, 'two.py', '2\n', 'w6')
    assert not (tree / 'two.py').exists() and host.writes() == 1


@macos
def test_call_id_is_idempotent_and_an_unconfirmed_write_is_never_replayed(host, monkeypatch):
    rid, worker, tree = host.ready()
    first = host.write(worker, 'notes.py', 'A = 1\n', 'w1')
    (tree / 'notes.py').write_text('A = 99\n')
    assert host.write(worker, 'notes.py', 'A = 1\n', 'w1') == first
    assert (tree / 'notes.py').read_text() == 'A = 99\n'
    with pytest.raises(PolicyError, match='different arguments'):
        host.write(worker, 'notes.py', 'A = 2\n', 'w1')
    with pytest.raises(PolicyError, match='different arguments'):
        host.write(worker, 'other.py', 'A = 1\n', 'w1')
    assert host.writes() == 1

    original = ChildWorktrees._replace

    def dying(*args):
        original(*args)
        raise ProcessDied('The service stopped after the replacement, before settlement.')
    monkeypatch.setattr(ChildWorktrees, '_replace', staticmethod(dying))
    with pytest.raises(ProcessDied):
        host.write(worker, 'later.py', 'B = 1\n', 'w2')
    monkeypatch.setattr(ChildWorktrees, '_replace', staticmethod(original))
    assert host.app.store.query("SELECT after_hash FROM agent_worktree_writes WHERE call_id='w2'")[0]['after_hash'] is None
    (tree / 'later.py').write_text('B = 7\n')
    with pytest.raises(PolicyError, match='never confirmed'):
        host.write(worker, 'later.py', 'B = 1\n', 'w2')
    assert (tree / 'later.py').read_text() == 'B = 7\n'


@macos
def test_write_bounds_per_worktree_and_generation(host, monkeypatch):
    monkeypatch.setattr(child_worktrees, 'MAX_PATHS', 2)
    monkeypatch.setattr(child_worktrees, 'MAX_BYTES', 40)
    monkeypatch.setattr(child_worktrees, 'MAX_WRITES', 4)
    rid, worker, tree = host.ready()
    host.write(worker, 'a.py', 'a = 1\n', 'w1')
    host.write(worker, 'b.py', 'b = 1\n', 'w2')
    with pytest.raises(PolicyError, match='at most 2 changed paths'):
        host.write(worker, 'c.py', 'c\n', 'w3')
    host.write(worker, 'a.py', 'a = 22\n', 'w4')
    with pytest.raises(PolicyError, match='40-byte write budget'):
        host.write(worker, 'b.py', 'b' * 30 + '\n', 'w5')
    host.write(worker, 'b.py', 'b = 2\n', 'w6')
    with pytest.raises(PolicyError, match='4-write limit'):
        host.write(worker, 'a.py', 'a\n', 'w7')
    assert not (tree / 'c.py').exists() and (tree / 'b.py').read_text() == 'b = 2\n' and host.writes() == 4
    assert child_worktrees.MAX_FILE == MAX_FILE == 96_000


@macos
def test_ignore_rules_and_attributes_cannot_hide_child_work_from_review_or_retirement(host, monkeypatch):
    (host.project / '.gitignore').write_text('*.log\n')
    (host.project / '.gitattributes').write_text('* text=auto\n')
    git(host.project, 'add', '.gitignore', '.gitattributes')
    git(host.project, 'commit', '-q', '-m', 'Ignore logs and normalize text')
    rid, worker, tree = host.ready()
    with pytest.raises(PolicyError, match='Git ignores this path'):
        host.write(worker, 'notes.log', 'hidden\n', 'w1')
    with pytest.raises(PolicyError, match='Git metadata'):
        host.write(worker, '.gitattributes', '* -text\n', 'w2')
    assert not (tree / 'notes.log').exists() and (tree / '.gitattributes').read_text() == '* text=auto\n'
    host.write(worker, 'kept.txt', 'kept\n', 'w3')
    rules = host.read(worker, '.gitignore', 'r1')
    host.write(worker, '.gitignore', '*.log\nkept.txt\n', 'w4', expected_hash=rules['file_hash'])
    readme = host.read(worker, 'README.md', 'r2')
    host.write(worker, 'README.md', '# Fixture\r\n', 'w5', expected_hash=readme['file_hash'])  # Line endings only.
    changes = host.cw.changes(host.chat, rid, worker['id'])
    found = {item['path']: item['kind'] for item in changes['changes']}
    assert changes['clean'] is False and changes['change_count'] == 3
    assert found == {'.gitignore': 'modified', 'kept.txt': 'ignored', 'README.md': 'modified'}
    # Status is untrusted: where Git omits a written change (normalization, rules or a quirk),
    # the write ledger and the base tree still expose it before any forced removal.
    real = host.app.git._verify

    def omitting(repo, row, cancel=None, ignored=True):
        evidence = real(repo, row, cancel, ignored)
        return {**evidence, 'changes': [entry for entry in evidence['changes'] if entry['path'] != 'README.md']}
    monkeypatch.setattr(host.app.git, '_verify', omitting)
    found = {item['path']: item['kind'] for item in host.cw.changes(host.chat, rid, worker['id'])['changes']}
    assert found == {'.gitignore': 'modified', 'kept.txt': 'ignored', 'README.md': 'normalized'}
    host.settle(worker, ['r1'])
    with pytest.raises(PolicyError, match=r'3 worktree change\(s\) are not integrated.*kept\.txt'):
        host.cw.retire(host.chat, rid, worker['id'])
    receipt = host.cw.retire(host.chat, rid, worker['id'], discard_review=True)
    preserved = {item['path']: item for item in receipt['preserved']}
    assert set(preserved) == {'.gitignore', 'kept.txt', 'README.md'} and not tree.exists()
    assert git(host.project, 'cat-file', '-p', preserved['README.md']['blob_oid']).stdout == b'# Fixture\r\n'


@macos
def test_a_live_writable_process_over_the_worktree_excludes_child_writes(host):
    rid, worker, tree = host.ready()
    now = time.time()
    # A writable admission whose root covers the managed worktree storage (nested scope).
    host.app.store.query(
        'INSERT INTO process_jobs(id,action_id,chat_id,run_id,call_id,root,root_identity,payload_hash,command,network,writable,'
        "owner,state,outcome_uncertain,stop_requested,output_bytes,output_dropped,timeout,created,updated) "
        "VALUES('job-1','action-job-1',?,?,'call-job-1',?,'[0,0]','fixture','fixture command',0,1,'fixture-owner','running',0,0,0,0,30,?,?)",
        (host.chat, rid, str(host.app.git.anchor), now, now))
    try:
        with pytest.raises(PolicyError, match='writable background command'):
            host.write(worker, 'blocked.py', 'x = 1\n', 'w1')
        assert not (tree / 'blocked.py').exists() and host.writes() == 0
    finally:
        host.app.store.query("UPDATE process_jobs SET state='succeeded' WHERE id='job-1'")
    assert host.write(worker, 'blocked.py', 'x = 1\n', 'w1')['operation'] == 'create'


@macos
def test_guard_blocks_a_live_child_and_allows_export_after_acceptance(host):
    rid, worker, tree = host.ready()
    ident = tree.name
    host.read(worker, 'app.py', 'r1')
    host.edit(worker, 'app.py', '"base"', '"child"', 'e1')
    core = host.app.git
    record = core._row(host.chat, ident)
    with pytest.raises(PolicyError, match='live child agent'):
        core.preview_export(host.chat, ident)
    with pytest.raises(PolicyError, match='live child agent'):
        core.preview_remove(host.chat, ident)
    for operation in ('remove', 'forget', 'export'):
        with pytest.raises(PolicyError, match='live child agent'):
            core._guard(operation, record)
    host.settle(worker, ['r1'])
    with pytest.raises(PolicyError, match='accepted'):
        core.preview_export(host.chat, ident)
    core._guard('remove', record)
    core._guard('forget', record)
    host.accept(worker, ['r1'])
    export = core.preview_export(host.chat, ident)
    assert [item['path'] for item in export['proposals']] == ['app.py']
    assert export['proposals'][0]['content'] == 'print("child")\n' and export['authorization'] == 'none'
    exact = core.export_file(host.chat, export['export_id'], 'app.py')
    assert exact['base_hash'] == sha(BASE) and exact['worktree_id'] == ident
    # No longer guarded, GitWorkflows itself still never removes unexported work.
    with pytest.raises(PolicyError, match='modified or untracked'):
        core.preview_remove(host.chat, ident)
    with pytest.raises(PolicyError, match='unknown record'):
        host.cw.guard('remove', None)


@macos
def test_human_managed_worktree_is_unaffected_by_the_child_guard(host):
    rid, worker, tree = host.ready()
    core = host.app.git
    created = core.create(host.chat, core.preview_create(host.chat)['preview_id'])
    human, folder = created['worktree_id'], Path(created['path'])
    (folder / 'app.py').write_text('print("human")\n')
    export = core.preview_export(host.chat, human)
    assert [item['path'] for item in export['proposals']] == ['app.py']
    (folder / 'app.py').write_bytes(BASE)
    assert core.remove(host.chat, core.preview_remove(host.chat, human)['preview_id'])['state'] == 'removed'
    assert core.forget(host.chat, human)['forgotten'] == human
    assert host.cw.binding(rid, worker['id'])['state'] == 'active' and tree.is_dir()
    with pytest.raises(PolicyError, match='live child agent'):
        core.preview_export(host.chat, tree.name)


@macos
def test_retire_requires_integration_then_removes_the_worktree(host):
    rid, worker, tree = host.ready()
    ident = tree.name
    host.read(worker, 'app.py', 'r1')
    host.edit(worker, 'app.py', '"base"', '"child"', 'e1')
    host.write(worker, 'pkg/new.py', 'NEW = True\n', 'w1')
    with pytest.raises(PolicyError, match='settled or was stopped'):
        host.cw.retire(host.chat, rid, worker['id'])
    host.settle(worker, ['r1'])
    host.accept(worker, ['r1'])
    with pytest.raises(PolicyError, match=r'2 worktree change\(s\) are not integrated.*app\.py'):
        host.cw.retire(host.chat, rid, worker['id'])
    assert tree.is_dir() and host.row(ident)['state'] == 'active' and host.git_row(ident)['state'] == 'active'

    # The root's integration route: exact export, a Workspace draft bound to it, then the apply step.
    core, project = host.app.git, Workspace(host.project)
    export = core.preview_export(host.chat, ident)
    for proposal in export['proposals']:
        exact = core.export_file(host.chat, export['export_id'], proposal['path'])
        draft = project.draft(exact['path'], exact['content'])
        assert GitWorkflows.check_draft(exact, draft)
        Workspace.apply(draft)
    receipt = host.cw.retire(host.chat, rid, worker['id'])
    assert receipt['state'] == 'retired' and sorted(receipt['integrated']) == ['app.py', 'pkg/new.py']
    assert receipt['preserved'] == [] and receipt['discard_review'] is False and receipt['authorization'] == 'none'
    assert not tree.exists() and not (host.project / '.git' / 'worktrees' / ident).exists()
    assert host.row(ident)['state'] == 'retired' and host.git_row(ident)['state'] == 'removed'
    assert (host.project / 'app.py').read_text() == 'print("child")\n'
    assert (host.project / 'pkg' / 'new.py').read_text() == 'NEW = True\n'
    with pytest.raises(PolicyError, match='Only an active'):
        host.cw.retire(host.chat, rid, worker['id'])


@macos
def test_discard_review_preserves_readable_blobs_before_removal(host):
    rid, worker, tree = host.ready()
    ident = tree.name
    host.read(worker, 'app.py', 'r1')
    host.edit(worker, 'app.py', '"base"', '"child"', 'e1')
    host.write(worker, 'pkg/new.py', 'NEW = True\n', 'w1')
    (tree / 'README.md').unlink()
    host.settle(worker, ['r1'])
    with pytest.raises(PolicyError, match='not integrated'):
        host.cw.retire(host.chat, rid, worker['id'], discard_review='yes')
    receipt = host.cw.retire(host.chat, rid, worker['id'], discard_review=True)
    assert receipt['state'] == 'retired' and receipt['discard_review'] is True and receipt['integrated'] == []
    preserved = {item['path']: item for item in receipt['preserved']}
    assert preserved['README.md'] == {'path': 'README.md', 'deleted': True, 'blob_oid': None, 'sha256': None}
    for path, data in (('app.py', b'print("child")\n'), ('pkg/new.py', b'NEW = True\n')):
        item = preserved[path]
        assert item['sha256'] == sha(data) and item['bytes'] == len(data)
        assert git(host.project, 'cat-file', '-t', item['blob_oid']).stdout.strip() == b'blob'
        assert git(host.project, 'cat-file', '-p', item['blob_oid']).stdout == data
    assert not tree.exists() and host.git_row(ident)['state'] == 'removed'
    assert json.loads(host.row(ident)['evidence'])['preserved'] == receipt['preserved']
    assert (host.project / 'app.py').read_bytes() == BASE and not (host.project / 'pkg').exists()


@macos
def test_restart_turns_an_unconfirmed_allocation_into_uncertain_without_retry(host, monkeypatch):
    rid = host.root()
    worker = host.child(rid, claim=False)
    payload = host.cw.propose(host.chat, rid, worker['id'], 1)
    ident, real = payload['worktree_id'], host.app.git._git

    def dying(args, *rest, **kwargs):
        result = real(args, *rest, **kwargs)
        if list(args[:2]) == ['worktree', 'add']:
            raise ProcessDied('The service stopped right after Git ran.')
        return result
    monkeypatch.setattr(host.app.git, '_git', dying)
    with pytest.raises(ProcessDied):
        host.cw.allocate(payload, 'action-1')
    assert host.row(ident)['state'] == 'allocating' and host.git_row(ident)['state'] == 'creating'
    with pytest.raises(PolicyError, match='in progress'):
        host.cw.guard('export', {'id': ident})

    host.restart()
    row = host.row(ident)
    evidence = json.loads(row['evidence'])
    assert row['state'] == 'uncertain' and evidence['interrupted'] == 'allocating'
    assert evidence['git_state'] == 'uncertain' and evidence['present'] == {'folder': True, 'metadata': True}
    assert host.git_row(ident)['state'] == 'uncertain'
    assert sorted(path.name for path in host.app.git.anchor.iterdir()) == [ident]
    with pytest.raises(PolicyError, match='no longer pending'):
        host.cw.allocate(payload, 'action-1')
    with pytest.raises(PolicyError, match='uncertain'):
        host.cw.assert_chat_deletable(host.chat)
    # The still-queued child blocks native cleanup; once it is stopped, native removal and inspection settle it.
    with pytest.raises(PolicyError, match='live child agent'):
        host.app.git.preview_remove(host.chat, ident)
    host.tree.cancel(host.chat, rid, rid, worker['id'])
    core = host.app.git
    assert core.remove(host.chat, core.preview_remove(host.chat, ident)['preview_id'])['state'] == 'removed'
    assert host.cw.reconcile(host.chat)['settled'] == [ident] and host.row(ident)['state'] == 'failed'
    assert host.cw.forget_chat_records(host.chat)['forgotten'] == [ident]
    assert sorted(path.name for path in host.app.git.anchor.iterdir()) == []


@macos
def test_restart_during_a_discard_retirement_keeps_blob_receipts_and_never_retries(host, monkeypatch):
    rid, worker, tree = host.ready()
    ident = tree.name
    host.read(worker, 'app.py', 'r1')
    host.write(worker, 'draft.py', 'DRAFT = 1\n', 'w1')
    host.settle(worker, ['r1'])
    real = host.app.git._git

    def dying(args, *rest, **kwargs):
        result = real(args, *rest, **kwargs)
        if list(args[:2]) == ['worktree', 'remove']:
            raise ProcessDied('The service stopped right after Git removed the worktree.')
        return result
    monkeypatch.setattr(host.app.git, '_git', dying)
    with pytest.raises(ProcessDied):
        host.cw.retire(host.chat, rid, worker['id'], discard_review=True)
    assert host.row(ident)['state'] == 'retiring' and host.git_row(ident)['state'] == 'removing'
    with pytest.raises(PolicyError, match='in progress'):
        host.cw.guard('remove', {'id': ident})
    registered = json.loads(host.row(ident)['evidence'])['preserved']
    assert [item['path'] for item in registered] == ['draft.py'] and not tree.exists()

    host.restart()
    row = host.row(ident)
    evidence = json.loads(row['evidence'])
    assert row['state'] == 'uncertain' and evidence['interrupted'] == 'retiring' and evidence['git_state'] == 'uncertain'
    assert evidence['present'] == {'folder': False, 'metadata': False} and evidence['previous']['preserved'] == registered
    with pytest.raises(PolicyError, match='Only an active'):
        host.cw.retire(host.chat, rid, worker['id'], discard_review=True)
    assert host.cw.reconcile(host.chat)['settled'] == [ident] and host.row(ident)['state'] == 'retired'
    assert json.loads(host.row(ident)['evidence'])['previous']['previous']['preserved'] == registered
    assert git(host.project, 'cat-file', '-p', registered[0]['blob_oid']).stdout == b'DRAFT = 1\n'
    assert host.cw.assert_chat_deletable(host.chat)['deletable'] is True


@macos
def test_replaced_project_root_is_refused_at_allocation_and_at_write(host):
    rid = host.root()
    first, second = host.child(rid, 'first', claim=False), host.child(rid, 'second', claim=False)
    _, receipt = host.allocate(rid, first)
    host.tree.claim(host.chat, rid, first['id'], 1, first['owner'])
    first['workspace'] = host.cw.workspace(first)
    pending = host.cw.propose(host.chat, rid, second['id'], 1)
    tree, moved = Path(receipt['path']), host.tmp / 'project-moved'
    host.project.rename(moved)
    host.project.mkdir()
    (host.project / 'app.py').write_text('print("replacement")\n')
    with pytest.raises(PolicyError, match='renamed or replaced'):
        host.cw.allocate(pending, 'action-2')
    assert host.row(pending['worktree_id'])['state'] == 'failed' and host.git_row(pending['worktree_id']) is None
    assert not Path(pending['path']).exists()
    with pytest.raises(PolicyError, match='renamed or replaced'):
        host.write(first, 'new.py', 'x = 1\n', 'w1')
    with pytest.raises(PolicyError, match='renamed or replaced'):
        host.cw.workspace(first)
    with pytest.raises(PolicyError, match='renamed or replaced'):
        host.cw.changes(host.chat, rid, first['id'])
    # The same path as a symbolic link to the original directory is refused too.
    (host.project / 'app.py').unlink()
    host.project.rmdir()
    host.project.symlink_to(moved, target_is_directory=True)
    with pytest.raises(PolicyError, match='not a plain directory|symbolic link'):
        host.write(first, 'new.py', 'x = 1\n', 'w2')
    assert not (tree / 'new.py').exists() and not (moved / 'new.py').exists() and host.writes() == 0


@macos
def test_root_closure_rejects_pending_proposals_and_deletion_waits_for_retirement(host):
    rid = host.root()
    kept, waiting = host.child(rid, 'kept', claim=False), host.child(rid, 'waiting', claim=False)
    _, receipt = host.allocate(rid, kept)
    pending = host.cw.propose(host.chat, rid, waiting['id'], 1)
    host.tree.cancel(host.chat, rid, rid, rid)
    host.app.runs.transition(rid, 'stopped')
    assert host.cw.close_run(rid) == 1
    assert host.row(pending['worktree_id'])['state'] == 'rejected'
    assert host.row(receipt['worktree_id'])['state'] == 'active' and Path(receipt['path']).is_dir()
    with pytest.raises(PolicyError, match='hold a folder'):
        host.cw.assert_chat_deletable(host.chat)
    with pytest.raises(PolicyError, match='hold a folder'):
        host.cw.forget_chat_records(host.chat)
    retired = host.cw.retire(host.chat, rid, kept['id'])
    assert retired['state'] == 'retired' and retired['integrated_count'] == 0 and not Path(receipt['path']).exists()
    assert host.cw.assert_chat_deletable(host.chat)['deletable'] is True
    assert sorted(host.cw.forget_chat_records(host.chat)['forgotten']) == sorted([pending['worktree_id'], receipt['worktree_id']])
    assert host.app.store.query('SELECT count(*) AS n FROM agent_worktrees')[0]['n'] == 0
