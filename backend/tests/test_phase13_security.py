"""Phase 13 integrated adversarial suite: reviewed Git, child worktrees, recipes, installs, undo, egress.

Every case drives the checked-in App: the real Store, run journal, controller
execution, permission broker, native approval route, hook dispatch, agent tree
leases, GitWorkflows/GitPlans confined Git, ChildWorktrees, the recipe runner
and Environments, on real temporary Git repositories and projects under pytest
tmp_path. AgentGuard admission is a recording fixture that allows, the root
model is never called, and child providers are scripted fixtures. Fixture Git
commands are unconfined test-author commands with hooks disabled; the code
under test uses only its own confined paths. No network, credential, account or
real project repository is used. Cases that document an observed defect live in
the clearly named section at the end as strict expected failures.
"""

import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from pebble import egress, environments
from pebble.agent_manager import ADMITTED, Agents
from pebble.app import App
from pebble.coding_api import root_tool_names
from pebble.context_manifest import assemble_context
from pebble.controller import CURRENT, RunController
from pebble.egress import EgressBroker
from pebble.git_plans import GitPlanUncertain, SensitiveContent
from pebble.git_workflows import GitWorkflows
from pebble.permissions import DEFAULT, GROUP
from pebble.policy import PolicyError, Workspace, digest
from pebble.tool_catalogue import TOOLS

MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
macos = pytest.mark.skipif(not MAC, reason='Confined Git, recipes and installs are a macOS sandbox-exec product surface.')
PROFILE = {'id': 'fixture-cloud', 'kind': 'openai', 'name': 'Fixture provider', 'url': 'https://unused.invalid',
           'model': 'fixture', 'context': 8192, 'budget': 4096, 'tools': True}
BASE = 'VALUE = 1\n'
PASSING = ('import unittest\nfrom app import VALUE\n\n\nclass ValueTest(unittest.TestCase):\n'
           '    def test_value(self):\n        self.assertGreaterEqual(VALUE, 1)\n')
NATIVE_EFFECTS = ('git_plan_propose', 'agent_worktree_retire', 'agent_worktree_reconcile', 'environment_plan',
                  'environment_remove')
HOOKS = ('pre-commit', 'prepare-commit-msg', 'commit-msg', 'post-commit', 'pre-merge-commit', 'post-merge',
         'post-checkout', 'pre-push', 'reference-transaction', 'post-rewrite', 'post-index-change', 'pre-auto-gc')


class ProcessDied(BaseException):
    """Fault injection: the service stops mid-effect, before any outcome commits."""


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
    path.mkdir(parents=True)
    git(path, 'init', '-q')
    git(path, 'config', 'user.name', 'Fixture Author')
    git(path, 'config', 'user.email', 'fixture@example.invalid')
    (path / 'app.py').write_text(BASE)
    (path / 'README.md').write_text('# Fixture\n')
    (path / 'test_app.py').write_text(PASSING)
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'Initial fixture commit')
    return path.resolve()


def call(name, args, ident='tool'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def final(text):
    return {'role': 'assistant', 'content': text}


def eventually(predicate, message, timeout=20):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    pytest.fail(message)


class FakePinnedProvider:
    """One independently owned child transport; never touches the root model."""

    def __init__(self, host, app, profile, owner_id, lane, observer=None):
        self.host, self.profile, self.owner_id, self.lane = host, copy.deepcopy(profile), owner_id, lane
        self.agent_id = owner_id.split(':')[1]
        self.name = app.store.query('SELECT name FROM agent_nodes WHERE id=?', (self.agent_id,))[0]['name']
        self.observer, self.requests, self.last_usage = observer, [], {'input_tokens': 11, 'output_tokens': 7}
        self.stopped = threading.Event()
        host.providers.append(self.name)
        assert self.name in host.scripts, 'Every child needs an explicit fixture script.'

    def complete(self, messages, tools, cancel, token, effort):
        with self.lane.admit(self.owner_id, self.profile, cancel):
            if cancel.is_set() or self.stopped.is_set():
                raise InterruptedError('Fixture child stopped.')
            projection = assemble_context(messages, tools, self.profile['budget'], output_reserve=256)
            if self.observer:
                self.observer(projection.manifest)
            self.requests.append(copy.deepcopy(messages))
            return copy.deepcopy(self.host.scripts[self.name](self, len(self.requests), messages, tools, cancel))

    def cancel(self):
        self.stopped.set()
        self.lane.wake()

    def close(self):
        self.stopped.set()
        self.lane.wake()


class Host:
    def __init__(self, tmp_path, monkeypatch, mode='ask', scheduler=False):
        self.tmp, self.monkeypatch = tmp_path.resolve(), monkeypatch
        if not scheduler:
            monkeypatch.setattr(Agents, 'start', lambda self: None)
        self.project = make_repo(self.tmp / 'project')
        self.home = self.tmp / 'data'
        self.events, self.guard_calls, self.root_calls, self.providers, self.scripts = [], [], [], [], {}
        self.fixture_jobs, self.ws, self.rid, self.cid = [], None, None, None
        self.boot()
        self.permissions(mode)
        self.chat = self.app.new_chat(str(self.project))['id']

    def boot(self):
        app = App(self.home, self.tmp / 'resources', lambda *args: self.events.append(args))
        self.monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(PROFILE))
        self.monkeypatch.setattr(app.model, 'complete', self.root_complete)
        self.monkeypatch.setattr(app.guard, 'handle', self.guard)
        app.agents.provider_factory = lambda *args, **kwargs: FakePinnedProvider(self, *args, **kwargs)
        self.app = app

    def root_complete(self, *args, **kwargs):
        self.root_calls.append(args)
        raise AssertionError('Phase 13 security fixtures must not make a root model request.')

    def guard(self, event, root, session, **kwargs):
        self.guard_calls.append({'event': event, 'root': str(root), 'tool': kwargs.get('tool')})
        return {'action': 'allow', 'reason': 'Fixture Guard admission'}

    def permissions(self, mode='ask', **tools):
        self.app.store.set_setting('permissions', {'mode': mode, 'tools': {**DEFAULT['tools'], **tools}})

    # Root ownership: the test thread acts as the root worker.

    def root(self, chat=None):
        app, cid = self.app, chat or self.chat
        self.release()
        for live in app.runs.list():
            if live['state'] in {'queued', 'running', 'retry_wait'}:
                app.runs.transition(live['id'], 'waiting_input')
        row = app.store.query('SELECT * FROM chats WHERE id=?', (cid,))[0]
        config = app.runtime_config(row, False, {'max_steps': 64, 'max_seconds': 300})
        rid = app.runs.create(cid, 'Phase 13 adversarial fixture task.', config)
        app.runs.transition(rid, 'running')
        self.rid, self.cid, self.ws = rid, cid, Workspace(row['workspace'])
        self.own()
        return rid

    def own(self):
        app = self.app
        app.controller.live = None
        app.controller.enter(app.runs.get(self.rid, self.cid))
        app.active, app.active_run = self.cid, self.rid

    def release(self):
        self.app.active = self.app.active_run = None
        self.app.controller.live = None
        CURRENT.set(None)

    def call(self, name, args, call_id):
        self.own()
        return self.app.execute(name, args, self.ws, self.cid, call_id=call_id)

    def native_retire(self, agent_id, discard=False):
        """Retirement is native-only (Phase 9 removal contract): the trusted route, no model tool."""
        self.release()
        return self.app.coding.native_retire(self.cid, self.rid, agent_id, discard=discard)

    def approve(self, action_id):
        self.release()
        return self.app.approve(self.cid, action_id)

    def action(self, action_id):
        return next(a for a in self.app.store.actions(self.cid) if a['id'] == action_id)

    def gate(self, actions=None):
        actions = self.app.store.actions(self.cid) if actions is None else actions
        return RunController._current_test_evidence(actions, Workspace(self.project))

    # Repository facts (read-only fixture Git)

    def head(self, repo=None):
        return git(repo or self.project, 'rev-parse', 'HEAD').stdout.decode().strip()

    def state(self, repo=None, status=True):
        """Index, HEAD, refs and (unless drivers are configured) status; fixture Git is unconfined."""
        repo = Path(repo or self.project)
        index = repo / '.git' / 'index'
        facts = {'index': index.read_bytes() if index.exists() else None, 'head': (repo / '.git' / 'HEAD').read_bytes(),
                 'refs': git(repo, 'for-each-ref', '--format=%(objectname) %(refname)').stdout}
        if status:
            facts['status'] = git(repo, '--no-optional-locks', 'status', '--porcelain=v2', '-z', '--untracked-files=all').stdout
        return facts

    def plans(self):
        rows = self.app.store.query('SELECT id,state,operation,receipt FROM git_plan_receipts ORDER BY created,rowid')
        return [{**row, 'receipt': json.loads(row['receipt'])} for row in rows]

    def bare_remote(self):
        bare = self.tmp / 'remote.git'
        git(self.tmp, 'init', '--bare', '-q', str(bare))
        git(self.project, 'remote', 'add', 'origin', str(bare))
        return bare.resolve()

    # Children

    def spawn_worktree(self, name, tools=('Read',), call_id=None):
        return self.call('SpawnWorktreeAgent', {'task': 'Change files in your own checkout: ' + name, 'name': name,
                                                'tools': json.dumps(list(tools)), 'max_steps': 4, 'max_seconds': 60,
                                                'model_tokens': 32768}, call_id or 'spawn-' + name)

    def project_child(self, name, tools=('Read',), paths=()):
        node = self.app.agents.tree.spawn(self.cid, self.rid, None, 'Scoped project task ' + name, name, list(tools),
                                          {'max_steps': 4, 'max_seconds': 30, 'model_tokens': 16384}, list(paths))
        return self.claim(node, workspace=Workspace(self.project))

    def claim(self, node, workspace=None):
        owner = 'worker-' + node['id'][:8]
        self.app.agents.tree.claim(self.cid, self.rid, node['id'], node['generation'], owner)
        worker = {'cid': self.cid, 'rid': self.rid, 'id': node['id'], 'generation': node['generation'], 'owner': owner,
                  'cancel': threading.Event(), 'workspace': workspace}
        if workspace is None:
            worker['workspace'] = self.app.child_worktrees.workspace(worker)
        return worker

    def child_call(self, worker, name, args, call_id):
        """The worker's sequence: schema admission, running receipt, adapter, committed outcome."""
        app, tree = self.app, self.app.agents.tree
        node = tree.get(worker['cid'], worker['rid'], worker['id'])
        app.validate(name, args, app.agents.child_tools(worker, node, False))
        identity = (worker['cid'], worker['rid'], worker['id'], worker['generation'], worker['owner'], call_id)
        assert tree.begin_tool(*identity, name, args) is None
        try:
            result = app.agents.child_tool(worker, name, args, call_id)
        except (ValueError, OSError) as exc:
            result = {'error': str(exc)[:1000], 'status': 'rejected'}
        return tree.end_tool(*identity, result)

    def complete_child(self, worker, evidence):
        tree = self.app.agents.tree
        tree.settle(worker['cid'], worker['rid'], worker['id'], worker['generation'], 'completed',
                    {'text': 'Finished in my checkout.'}, evidence, worker['owner'])
        tree.review(worker['cid'], worker['rid'], worker['rid'], worker['id'], worker['generation'], evidence)

    def process(self, root=None, state='running', writable=1, uncertain=0):
        ident = 'job-' + uuid.uuid4().hex[:12]
        root, now = str(root or self.project), time.time()
        self.app.store.query(
            'INSERT INTO process_jobs(id,action_id,chat_id,run_id,call_id,root,root_identity,payload_hash,command,network,'
            "writable,owner,state,outcome_uncertain,stop_requested,output_bytes,output_dropped,timeout,created,updated) "
            "VALUES(?,?,?,?,?,?,?,'fixture','fixture command',0,?,'fixture-owner',?,?,0,0,0,30,?,?)",
            (ident, 'action-' + ident, self.cid, self.rid, 'call-' + ident, root, json.dumps(list(Workspace(root).identity)),
             writable, state, uncertain, now, now))
        self.fixture_jobs.append(ident)
        return ident

    def settle_jobs(self):
        for ident in self.fixture_jobs:
            self.app.store.query("UPDATE process_jobs SET state='succeeded' WHERE id=? AND state IN ('admitted','running')", (ident,))

    def restart(self):
        self.release()
        self.settle_jobs()
        self.app.close()
        self.boot()

    def close(self):
        self.release()
        self.settle_jobs()
        self.app.close()


@pytest.fixture
def make_host(tmp_path, monkeypatch):
    if not MAC:
        pytest.skip('Confined Git, recipes and installs are a macOS sandbox-exec product surface.')
    made = []

    def make(**kwargs):
        made.append(Host(tmp_path, monkeypatch, **kwargs))
        return made[-1]
    yield make
    for item in made:
        item.close()
        assert item.root_calls == []


@pytest.fixture
def host(make_host):
    return make_host()


# 1. Authority


@macos
def test_native_coding_routes_are_unreachable_from_model_and_child_tools(host):
    app = host.app
    methods = app.coding.methods()
    assert set(NATIVE_EFFECTS) <= set(methods)
    model = {item['function']['name'] for item in TOOLS}
    assert not set(methods) & (model | set(root_tool_names()) | set(GROUP) | ADMITTED)
    # The model schema gate refuses native-only and fixture-only arguments by shape.
    assert 'RetireAgentWorktree' not in model and 'RetireAgentWorktree' not in root_tool_names()
    for name, args in (('RetireAgentWorktree', {'agent_id': 'a' * 32, 'discard': True}),
                       ('PrepareEnvironment', {'kind': 'pip-hashes', 'loopback_targets': True}),
                       ('ProposeGitChange', {'operation': 'discard', 'paths': '["app.py"]', 'requires_review': False})):
        with pytest.raises(PolicyError, match='allowed schema'):
            App.validate(name, args, TOOLS)
    (host.project / 'app.py').write_text('VALUE = 2\n')
    rid = host.root()
    for index, (name, args) in enumerate((
            ('git_plan_propose', {'id': host.chat, 'operation': 'stage', 'paths': '["app.py"]'}),
            ('agent_worktree_retire', {'id': host.chat, 'run_id': rid, 'agent_id': 'a' * 32, 'discard': True}),
            ('environment_plan', {'id': host.chat, 'kind': 'pip-hashes'}))):
        # Native route names are not model tools: the catalogue layer refuses them first.
        with pytest.raises(PolicyError, match='disabled in Permissions|not in the canonical catalogue'):
            host.call(name, args, f'native-{index}')
    worker = host.project_child('reader')
    for name in (*NATIVE_EFFECTS, 'ProposeGitChange', 'PrepareEnvironment', 'RetireAgentWorktree'):
        with pytest.raises(PolicyError, match='outside child capabilities'):
            app.agents.child_tool(worker, name, {}, 'child-' + name)
    assert app.store.actions(host.chat) == [] and host.plans() == []
    # The trusted native route itself lands on a review card even in Full mode.
    host.permissions('full')
    host.release()
    staged = app.dispatch('git_plan_propose', {'id': host.chat, 'operation': 'stage', 'paths': '["app.py"]'})
    assert staged['status'] == 'awaiting_user_approval'
    assert host.action(staged['action_id'])['payload']['requires_review'] is True and host.plans() == []


@macos
def test_git_plans_wait_in_ask_and_auto_and_full_mode_runs_only_local_reviewless_plans(host):
    app = host.app
    bare = host.bare_remote()
    base = host.head()
    (host.project / 'app.py').write_text('VALUE = 2\n')
    commit = {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Reviewed change'}
    for mode in ('ask', 'auto'):
        host.permissions(mode)
        host.root()
        staged = host.call('ProposeGitChange', commit, 'commit-' + mode)
        assert staged['status'] == 'awaiting_user_approval' and staged['kind'] == 'git'
        payload = host.action(staged['action_id'])['payload']
        assert payload['requires_review'] is False and payload['permission_mode'] == mode
        assert host.head() == base and host.plans() == []
    host.permissions('full')
    host.root()
    applied = host.call('ProposeGitChange', commit, 'commit-full')
    assert applied['status'] == 'applied' and applied['result']['state'] == 'applied'
    assert applied['result']['readback']['verified'] is True and applied['result']['hooks_run'] is False
    new = host.head()
    assert new != base and git(host.project, 'show', new + ':app.py').stdout == b'VALUE = 2\n'
    assert [row['state'] for row in host.plans()] == ['applied']
    git(host.project, 'branch', 'merged-topic')
    (host.project / 'README.md').write_text('# Unsaved notes\n')
    pending = {}
    for operation, args in (('discard', {'paths': '["README.md"]'}), ('delete_branch', {'name': 'merged-topic'}),
                            ('push', {'remote': 'origin', 'branch': 'main'})):
        staged = host.call('ProposeGitChange', {'operation': operation, **args}, 'full-' + operation)
        assert staged['status'] == 'awaiting_user_approval', operation
        payload = host.action(staged['action_id'])['payload']
        assert payload['requires_review'] is True and (payload['destructive'] or payload['publication'])
        pending[operation] = staged['action_id']
    # A broker caller without the native review flag never runs them.
    for operation, action_id in pending.items():
        with pytest.raises(PolicyError, match='native review'):
            app.perform_action(host.action(action_id), host.chat)
    assert (host.project / 'README.md').read_text() == '# Unsaved notes\n'
    assert git(host.project, 'rev-parse', '--verify', '-q', 'merged-topic', check=False).returncode == 0
    assert git(bare, 'for-each-ref').stdout == b'' and len(host.plans()) == 1
    # Positive control: only the native approval route publishes the reviewed commit.
    result = host.approve(pending['push'])
    assert result['state'] == 'applied' and git(bare, 'rev-parse', 'refs/heads/main').stdout.decode().strip() == new
    # Network remotes are refused at preparation without echoing the URL or its credentials.
    git(host.project, 'remote', 'add', 'net', 'https://pebble-user:hunter2secret@example.invalid/repo.git')
    with pytest.raises(PolicyError, match='transport_unsupported') as caught:
        host.call('ProposeGitChange', {'operation': 'push', 'remote': 'net', 'branch': 'main'}, 'push-network')
    assert 'hunter2secret' not in str(caught.value) and 'example.invalid' not in str(caught.value)
    assert len(host.plans()) == 2


@macos
def test_dependency_installs_always_need_native_review_and_stored_fixture_fields_are_refused(host):
    app = host.app
    host.permissions('full')
    (host.project / 'requirements.txt').write_text('pebblefixture==1.0.0 --hash=sha256:' + 'a' * 64 + '\n')
    host.root()
    staged = host.call('PrepareEnvironment', {'kind': 'pip-hashes'}, 'install-1')
    assert staged['status'] == 'awaiting_user_approval' and staged['kind'] == 'install'
    action = host.action(staged['action_id'])
    payload = action['payload']
    assert payload['requires_review'] is True and payload['permission_mode'] == 'full'
    assert payload['network'] == 'allowlisted_registry_only' and all(port == 443 for _, port in payload['registries'])
    assert not {'ca_file', 'loopback_targets'} & set(payload)
    with pytest.raises(PolicyError, match='always need native review'):
        app.perform_action(action, host.chat)
    # A stored action edited to reach a loopback fixture registry is refused even after native approval.
    tampered = {**payload, 'loopback_targets': True, 'ca_file': str(host.tmp / 'ca.pem'), 'registries': [['localhost', 8443]]}
    app.store.query('UPDATE actions SET payload=? WHERE id=?', (json.dumps(tampered), action['id']))
    with pytest.raises(PolicyError, match='Fixture-only'):
        host.approve(action['id'])
    assert host.action(action['id'])['status'] == 'failed'
    assert app.store.query('SELECT count(*) AS n FROM project_environments')[0]['n'] == 0
    folder = host.home / 'environments'
    assert not folder.exists() or not any(folder.iterdir())
    native = app.dispatch('environment_plan', {'id': host.chat, 'kind': 'pip-hashes'})
    assert native['status'] == 'awaiting_user_approval' and host.action(native['action_id'])['payload']['requires_review'] is True
    assert app.store.query('SELECT count(*) AS n FROM project_environments')[0]['n'] == 0


@macos
def test_payload_edited_in_the_store_after_proposal_is_refused_at_approval_without_effect(host):
    app = host.app
    (host.project / 'app.py').write_text('VALUE = 2\n')
    (host.project / 'README.md').write_text('# Edited\n')
    host.root()
    commit = {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Reviewed message'}
    message = host.call('ProposeGitChange', commit, 'message')['action_id']
    paths = host.call('ProposeGitChange', commit, 'paths')['action_id']
    discard = host.call('ProposeGitChange', {'operation': 'discard', 'paths': '["README.md"]'}, 'discard')['action_id']
    edits = {message: lambda p: {**p, 'message': 'Unreviewed message\n'},
             paths: lambda p: {**p, 'paths': ['README.md', 'app.py']},
             discard: lambda p: {**p, 'requires_review': False}}
    for action_id, edit in edits.items():
        payload = host.action(action_id)['payload']
        assert 'plan_hash' in edit(payload) and edit(payload)['plan_hash'] == payload['plan_hash']
        app.store.query('UPDATE actions SET payload=? WHERE id=?', (json.dumps(edit(payload)), action_id))
    before = host.state()
    # The lowered review flag gets past the broker's review check, then the plan itself refuses.
    with pytest.raises(PolicyError, match='cannot drop its native review'):
        app.perform_action(host.action(discard), host.chat)
    for action_id, match in ((message, 'plan_hash'), (paths, 'plan_hash'), (discard, 'cannot drop its native review')):
        with pytest.raises(PolicyError, match=match):
            host.approve(action_id)
        assert host.action(action_id)['status'] == 'failed'
    assert host.state() == before and host.plans() == []
    assert (host.project / 'README.md').read_text() == '# Edited\n'


@macos
def test_hook_review_keeps_a_git_plan_pending_even_in_full_mode(host):
    app = host.app
    ident = app.extension_add(kind='hook', name='Review Git plans', content=json.dumps(
        {'version': 1, 'event': 'before_tool', 'review_patterns': ['ProposeGitChange']}))
    app.extension_toggle(ident, True)
    host.permissions('full')
    (host.project / 'app.py').write_text('VALUE = 2\n')
    base = host.head()
    host.root()
    staged = host.call('ProposeGitChange', {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Hooked'}, 'hooked')
    assert staged['status'] == 'awaiting_user_approval'
    action = host.action(staged['action_id'])
    assert action['payload']['requires_review'] is True and action['payload']['destructive'] is False
    with pytest.raises(PolicyError, match='native review'):
        app.perform_action(action, host.chat)
    assert host.head() == base and host.plans() == []
    assert host.approve(action['id'])['state'] == 'applied' and host.head() != base


@macos
def test_hook_review_never_lets_a_worktree_allocation_run_unreviewed(host):
    app = host.app
    ident = app.extension_add(kind='hook', name='Review allocations', content=json.dumps(
        {'version': 1, 'event': 'before_tool', 'review_patterns': ['AllocateChildWorktree']}))
    app.extension_toggle(ident, True)
    host.permissions('auto')
    rid = host.root()
    try:
        host.spawn_worktree('hooked')
    except PolicyError as exc:
        assert 'child was stopped' in str(exc)
    [action] = [item for item in app.store.actions(host.chat) if item['kind'] == 'worktree']
    [row] = app.store.query('SELECT state,agent_id FROM agent_worktrees')
    # Either it waits for native review or it fails closed; it never allocates under the hook's review.
    assert action['status'] in ('pending', 'failed') and row['state'] in ('proposed', 'rejected')
    assert app.store.query('SELECT count(*) AS n FROM git_worktrees')[0]['n'] == 0
    assert not app.git.anchor.exists() or not any(app.git.anchor.iterdir())
    assert not app.agents._worktree_ready(host.chat, rid, row['agent_id'])
    assert app.agents.tree.get(host.chat, rid, row['agent_id'])['status'] in ('queued', 'stopped') and host.providers == []


# 2. Stale and replaced state


@macos
@pytest.mark.parametrize('drift', ['file', 'head', 'index'])
def test_drift_between_proposal_and_approval_is_refused_with_a_failed_receipt(host, drift):
    (host.project / 'app.py').write_text('VALUE = 2\n')
    rid = host.root()
    staged = host.call('ProposeGitChange', {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Reviewed'}, 'commit')
    if drift == 'file':
        (host.project / 'app.py').write_text('VALUE = 3\n')
    else:
        (host.project / 'README.md').write_text('# Moved\n')
        git(host.project, 'add', 'README.md')
        if drift == 'head':
            git(host.project, 'commit', '-q', '-m', 'Concurrent commit')
    before = host.state()
    with pytest.raises(PolicyError, match='changed after review'):
        host.approve(staged['action_id'])
    assert host.state() == before
    [row] = host.plans()
    assert row['state'] == 'failed' and row['receipt']['effect_started'] is False
    assert host.action(staged['action_id'])['status'] == 'failed'
    assert host.app.runs.get(rid)['state'] == 'running', 'A definite refusal is not an uncertain effect.'


@macos
def test_project_root_renamed_and_recreated_at_the_same_path_refuses_every_broker_kind(host):
    (host.project / 'app.py').write_text('VALUE = 2\n')
    host.root()
    plan = host.call('ProposeGitChange', {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Reviewed'}, 'commit')
    host.call('Read', {'file_path': 'README.md'}, 'read-readme')
    edit = host.call('Write', {'file_path': 'README.md', 'content': '# Reviewed\n'}, 'write-readme')
    tests = host.call('RunTests', {'recipe': 'python-unittest'}, 'tests')
    assert {plan['status'], edit['status'], tests['status']} == {'awaiting_user_approval'}
    moved = host.tmp / 'project-moved'
    host.project.rename(moved)
    git(host.tmp, 'clone', '-q', '--no-hardlinks', str(moved), str(host.project))
    git(host.project, 'config', 'user.name', 'Fixture Author')
    git(host.project, 'config', 'user.email', 'fixture@example.invalid')
    (host.project / 'app.py').write_text('VALUE = 2\n')
    before = {'moved': host.state(moved), 'clone': host.state()}
    for action_id, match in ((plan['action_id'], 'changed after review|replaced'), (edit['action_id'], 'replaced'),
                             (tests['action_id'], 'replaced')):
        with pytest.raises(PolicyError, match=match):
            host.approve(action_id)
        assert host.action(action_id)['status'] == 'failed'
    assert {'moved': host.state(moved), 'clone': host.state()} == before
    assert (host.project / 'README.md').read_text() == (moved / 'README.md').read_text() == '# Fixture\n'
    assert [row['state'] for row in host.plans()] == ['failed']


@macos
def test_parent_repository_discovery_and_nested_repository_metadata_are_refused(host):
    app = host.app
    (host.project / 'pkg').mkdir()
    (host.project / 'pkg' / 'mod.py').write_text('X = 1\n')
    sub = app.new_chat(str(host.project / 'pkg'))['id']
    host.permissions('full')
    host.root(sub)
    outer = host.state()
    for index, args in enumerate(({'operation': 'stage', 'paths': '["mod.py"]'},
                                  {'operation': 'commit', 'paths': '["mod.py"]', 'message': 'Escape upward'})):
        with pytest.raises(PolicyError, match='not the root of a Git repository'):
            host.call('ProposeGitChange', args, f'parent-{index}')
    with pytest.raises(PolicyError, match='not the root of a Git repository'):
        host.call('ReadGitStatus', {}, 'parent-status')
    assert app.store.actions(sub) == [] and host.state() == outer
    # A repository nested inside the project keeps its metadata private to Git plans.
    inner = make_repo(host.project / 'vendor' / 'inner')
    host.root()
    outer = host.state()
    for index, path in enumerate(('vendor/inner/.git/config', 'vendor/inner/.git/HEAD')):
        with pytest.raises(PolicyError, match='Private credentials and Git metadata'):
            host.call('ProposeGitChange', {'operation': 'stage', 'paths': json.dumps([path])}, f'nested-meta-{index}')
    # The nested repository as its own project: plans name only it, never the containing one.
    nested = app.new_chat(str(inner))['id']
    host.root(nested)
    (inner / 'app.py').write_text('VALUE = 9\n')
    applied = host.call('ProposeGitChange', {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Inner only'}, 'inner')
    assert applied['status'] == 'applied' and applied['result']['readback']['verified'] is True
    assert git(inner, 'show', 'HEAD:app.py').stdout == b'VALUE = 9\n'
    assert host.state() == outer, 'The containing repository must not change.'


# 3. Malicious repository content


@macos
def test_repository_hooks_never_run_across_commit_switch_merge_and_push(host, monkeypatch):
    app = host.app
    markers = host.tmp / 'hook-markers'
    markers.mkdir()
    bare = host.bare_remote()
    for folder in (host.project / '.git' / 'hooks', host.project / '.husky'):
        folder.mkdir(exist_ok=True)
        for name in HOOKS:
            (folder / name).write_text(f'#!/bin/sh\necho ran >> "{markers}/{folder.name}-{name}"\nexit 0\n')
            (folder / name).chmod(0o755)
    git(host.project, 'add', '.husky')
    git(host.project, 'commit', '-q', '-m', 'Add repository hooks')
    git(host.project, 'config', 'core.hooksPath', '.husky')
    spawned = []
    original = GitWorkflows._spawn

    def spy(self, argv, *args, **kwargs):
        spawned.append([str(item) for item in argv])
        return original(self, argv, *args, **kwargs)
    monkeypatch.setattr(GitWorkflows, '_spawn', spy)
    host.permissions('full')
    host.root()
    steps = []

    def plan(args, call_id):
        result = host.call('ProposeGitChange', args, call_id)
        steps.append((args['operation'], result['status']))
        return result
    (host.project / 'app.py').write_text('VALUE = 2\n')
    plan({'operation': 'commit', 'paths': '["app.py"]', 'message': 'Commit with hooks present'}, 'commit')
    plan({'operation': 'create_branch', 'name': 'feature', 'switch': True}, 'branch')
    (host.project / 'app.py').write_text('VALUE = 3\n')
    plan({'operation': 'commit', 'paths': '["app.py"]', 'message': 'Feature work'}, 'feature-commit')
    plan({'operation': 'switch_branch', 'name': 'main'}, 'switch')
    (host.project / 'README.md').write_text('# Main work\n')
    plan({'operation': 'commit', 'paths': '["README.md"]', 'message': 'Main work'}, 'main-commit')
    merged = plan({'operation': 'merge', 'name': 'feature', 'message': 'Merge feature'}, 'merge')
    assert merged['result']['result']['merge_kind'] == 'three_way'
    push = plan({'operation': 'push', 'remote': 'origin', 'branch': 'main'}, 'push')
    assert steps == [('commit', 'applied'), ('create_branch', 'applied'), ('commit', 'applied'), ('switch_branch', 'applied'),
                     ('commit', 'applied'), ('merge', 'applied'), ('push', 'awaiting_user_approval')]
    assert host.approve(push['action_id'])['state'] == 'applied'
    assert git(bare, 'rev-parse', 'refs/heads/main').stdout.decode().strip() == host.head()
    assert (host.project / 'app.py').read_text() == 'VALUE = 3\n' and (host.project / 'README.md').read_text() == '# Main work\n'
    assert sorted(os.listdir(markers)) == [], 'No repository hook may run.'
    assert spawned and all('core.hooksPath=/dev/null' in argv and '--no-pager' in argv for argv in spawned)
    porcelain = {'commit', 'checkout', 'switch', 'push', 'pull', 'fetch', 'receive-pack', 'upload-pack', 'send-pack', 'rebase'}
    assert not any(porcelain & set(argv) for argv in spawned)


@macos
def test_attribute_drivers_never_run_and_driver_config_added_after_review_is_refused_by_name(host):
    marker = host.tmp / 'driver-ran'
    command = f'sh -c "echo ran > {marker}"'
    (host.project / '.gitattributes').write_text('* filter=evil diff=evil merge=evil\n')
    git(host.project, 'add', '.gitattributes')
    git(host.project, 'commit', '-q', '-m', 'Attributes naming undefined drivers')
    host.permissions('full')
    host.root()
    (host.project / 'app.py').write_text('VALUE = 2\n')
    applied = host.call('ProposeGitChange', {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Exact bytes'}, 'commit')
    assert applied['status'] == 'applied'
    assert git(host.project, 'cat-file', 'blob', host.head() + ':app.py').stdout == b'VALUE = 2\n'
    host.permissions('ask')
    host.root()
    (host.project / 'app.py').write_text('VALUE = 3\n')
    staged = host.call('ProposeGitChange', {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Later'}, 'later')
    git(host.project, 'config', 'filter.evil.clean', command)
    git(host.project, 'config', 'diff.evil.textconv', command)
    # No fixture status from here on: unconfined fixture Git would itself run the configured clean filter.
    before = host.state(status=False)
    with pytest.raises(PolicyError, match='Unsupported repository Git configuration') as caught:
        host.approve(staged['action_id'])
    assert command not in str(caught.value) and str(marker) not in str(caught.value)
    assert host.state(status=False) == before and [row['state'] for row in host.plans()] == ['applied', 'failed']
    with pytest.raises(PolicyError, match='Unsupported repository Git configuration'):
        host.call('ProposeGitChange', {'operation': 'stage', 'paths': '["app.py"]'}, 'again')
    with pytest.raises(PolicyError, match='Unsupported repository Git configuration'):
        host.call('ReadGitDiff', {'scope': 'unstaged'}, 'diff')
    assert not marker.exists(), 'Pebble never runs a filter or textconv driver.'


@macos
def test_symlinks_private_paths_and_secrets_are_refused_without_echoing_values(host):
    app = host.app
    outside = host.tmp / 'outside'
    outside.mkdir()
    (outside / 'victim.py').write_text('SAFE = True\n')
    (host.project / 'link.py').symlink_to(host.project / 'app.py')
    (host.project / 'linked').symlink_to(outside, target_is_directory=True)
    (host.project / '.env').write_text('PASSWORD=hunter2hunter2hunter2\n')
    token, key = 'ghp_' + 'A1b2C3d4' * 5, 'AKIA' + 'QZ7X' * 4
    (host.project / 'settings.py').write_text(f'GITHUB = "{token}"\nAWS = "{key}"\n')
    host.permissions('full')
    rid = host.root()
    for index, (paths, match) in enumerate(((['link.py'], 'symbolic link'), (['linked/victim.py'], 'symbolic link'),
                                            (['.env'], 'Private credentials'))):
        with pytest.raises(PolicyError, match=match):
            host.call('ProposeGitChange', {'operation': 'stage', 'paths': json.dumps(paths)}, f'bad-{index}')
    for operation in ('stage', 'commit'):
        args = {'operation': operation, 'paths': '["settings.py"]', **({'message': 'Leak'} if operation == 'commit' else {})}
        with pytest.raises(SensitiveContent) as caught:
            host.call('ProposeGitChange', args, 'secret-' + operation)
        assert token not in str(caught.value) and key not in str(caught.value)
        assert {(item['line'], item['kind']) for item in caught.value.findings} == {(1, 'github_token'), (2, 'aws_access_key_id')}
    assert app.store.actions(host.chat) == [] and host.plans() == []
    recorded = json.dumps([item['result'] for item in app.runs.tools(rid)])
    assert token not in recorded and key not in recorded and 'secret-commit' in json.dumps(app.runs.tools(rid))
    assert (outside / 'victim.py').read_text() == 'SAFE = True\n'


# 4. Conflicting writers


@macos
def test_worktree_child_never_touches_the_project_and_a_root_changed_path_exports_stale(host):
    app = host.app
    host.permissions('auto')
    rid = host.root()
    node = host.spawn_worktree('writer')
    binding = app.child_worktrees.binding(rid, node['id'])
    assert node['worktree']['status'] == 'applied' and binding['state'] == 'active'
    tree, project_before = Path(binding['path']), host.state()
    worker = host.claim(node)
    read = host.child_call(worker, 'Read', {'file_path': 'app.py'}, '1:read')
    written = host.child_call(worker, 'WriteWorktreeFile', {'file_path': 'app.py', 'content': 'VALUE = 10\n',
                                                            'expected_hash': read['file_hash']}, '2:write')
    created = host.child_call(worker, 'WriteWorktreeFile', {'file_path': 'pkg/new.py', 'content': 'NEW = 1\n'}, '3:new')
    assert written['project_changed'] is False and created['operation'] == 'create'
    assert host.state() == project_before and not (host.project / 'pkg').exists()
    # The root edits the same project path while the child's change sits in its checkout.
    host.call('Read', {'file_path': 'app.py'}, 'root-read')
    assert host.call('Write', {'file_path': 'app.py', 'content': 'VALUE = 20\n'}, 'root-write')['status'] == 'applied'
    assert (host.project / 'app.py').read_text() == 'VALUE = 20\n' and (tree / 'app.py').read_text() == 'VALUE = 10\n'
    with pytest.raises(PolicyError, match='live child agent'):
        host.call('PreviewWorktreeExport', {'worktree_id': binding['worktree_id']}, 'export-live')
    host.complete_child(worker, ['1:read'])
    manifest = host.call('PreviewWorktreeExport', {'worktree_id': binding['worktree_id']}, 'export')
    assert [item['path'] for item in manifest['proposals']] == ['pkg/new.py']
    assert {'path': 'app.py', 'reason': 'stale_parent_changed'} in manifest['skipped']
    # The native retire route never discards work that is not integrated without an explicit discard.
    with pytest.raises(PolicyError, match='not integrated'):
        host.native_retire(node['id'])
    assert tree.is_dir() and app.child_worktrees.binding(rid, node['id'])['state'] == 'active'
    assert (host.project / 'app.py').read_text() == 'VALUE = 20\n'


@macos
def test_worktree_changes_reach_the_project_only_through_inspected_root_integration(host):
    app = host.app
    host.permissions('auto')
    rid = host.root()
    node = host.spawn_worktree('integrator')
    worker = host.claim(node)
    binding = app.child_worktrees.binding(rid, node['id'])
    tree, ident = Path(binding['path']), binding['worktree_id']
    read = host.child_call(worker, 'Read', {'file_path': 'app.py'}, '1:read')
    host.child_call(worker, 'WriteWorktreeFile', {'file_path': 'app.py', 'content': 'VALUE = 4\n',
                                                  'expected_hash': read['file_hash']}, '2:edit')
    host.child_call(worker, 'WriteWorktreeFile', {'file_path': 'pkg/new.py', 'content': 'NEW = 1\n'}, '3:new')
    host.complete_child(worker, ['1:read'])
    manifest = host.call('PreviewWorktreeExport', {'worktree_id': ident}, 'export')
    assert sorted(item['path'] for item in manifest['proposals']) == ['app.py', 'pkg/new.py']
    assert manifest['content_included'] is False and manifest['authorization'] == 'none'
    with pytest.raises(PolicyError, match='ReadWorktreeExport'):
        host.call('IntegrateWorktreeFile', {'export_id': manifest['export_id'], 'file_path': 'app.py'}, 'blind')
    assert (host.project / 'app.py').read_text() == BASE
    for index, path in enumerate(('app.py', 'pkg/new.py')):
        host.call('ReadWorktreeExport', {'export_id': manifest['export_id'], 'file_path': path}, f'inspect-{index}')
        staged = host.call('IntegrateWorktreeFile', {'export_id': manifest['export_id'], 'file_path': path}, f'integrate-{index}')
        assert staged['status'] == 'applied' and staged['kind'] == 'edit'
    assert (host.project / 'app.py').read_text() == 'VALUE = 4\n' and (host.project / 'pkg' / 'new.py').read_text() == 'NEW = 1\n'
    retired = host.native_retire(node['id'])
    assert retired['state'] == 'retired'
    assert sorted(retired['integrated']) == ['app.py', 'pkg/new.py'] and retired['preserved'] == []
    assert not tree.exists() and not (host.project / '.git' / 'worktrees' / ident).exists()
    # Integration is an applied edit: no earlier evidence covers it until the project is tested again.
    assert not host.gate()


@macos
def test_two_worktree_children_each_write_only_their_own_checkout(make_host):
    host = make_host(mode='auto', scheduler=True)
    app = host.app
    rid = host.root()
    offered, nodes = {}, {}

    def allocated():
        # A sibling allocation adds Git worktree metadata; a write that inspects the repository at
        # that moment fails closed ("changed during inspection"). Write only after both exist.
        return len(nodes) == 2 and all(app.child_worktrees.binding(rid, n['id'])['state'] == 'active' for n in nodes.values())

    def script_for(name):
        def script(provider, step, messages, tools, cancel):
            offered[name] = {item['function']['name'] for item in tools}
            if step == 1:
                until = time.monotonic() + 20
                while not allocated() and not cancel.is_set() and time.monotonic() < until:
                    time.sleep(.02)
                return call('WriteWorktreeFile', {'file_path': 'notes.txt', 'content': name + ' notes\n'}, 'w1')
            if step == 2:
                return call('WriteWorktreeFile', {'file_path': name + '.py', 'content': f'OWNER = "{name}"\n'}, 'w2')
            return final(name + ' finished in its own worktree.')
        return script
    host.scripts.update(alpha=script_for('alpha'), beta=script_for('beta'))
    before = host.state()
    for name in ('alpha', 'beta'):
        nodes[name] = host.spawn_worktree(name)

    def done():
        records = [app.agents.tree.get(host.chat, rid, node['id']) for node in nodes.values()]
        return all(record['status'] == 'completed' for record in records) and not app.agents.busy()
    eventually(done, 'Both worktree children did not complete.')
    for name, other in (('alpha', 'beta'), ('beta', 'alpha')):
        binding = app.child_worktrees.binding(rid, nodes[name]['id'])
        tree = Path(binding['path'])
        receipts = app.agents.tree.receipts(host.chat, rid, nodes[name]['id'], 1)
        assert [(r['name'], r['state'], r['result'].get('operation')) for r in receipts] == [
            ('WriteWorktreeFile', 'completed', 'create')] * 2, [r['result'] for r in receipts]
        assert (tree / 'notes.txt').read_text() == name + ' notes\n' and (tree / (name + '.py')).is_file()
        assert not (tree / (other + '.py')).exists() and binding['writes'] == 2
        assert {'Read', 'WriteWorktreeFile', 'EditWorktreeFile'} <= offered[name]
        assert not offered[name] & {'Write', 'Edit', 'Append', 'ProposePatch', 'RunTests', 'ProposeGitChange', 'IntegrateWorktreeFile'}
        rows = app.store.query('SELECT DISTINCT worktree_id FROM agent_worktree_writes WHERE agent_id=?', (nodes[name]['id'],))
        assert rows == [{'worktree_id': binding['worktree_id']}]
    assert host.state() == before
    assert not any((host.project / name).exists() for name in ('notes.txt', 'alpha.py', 'beta.py'))
    assert sorted(host.providers) == ['alpha', 'beta']


@macos
def test_writable_processes_and_child_path_leases_block_tree_changing_git_plans(host):
    app = host.app
    git(host.project, 'checkout', '-q', '-b', 'feature')
    (host.project / 'README.md').write_text('# Feature\n')
    git(host.project, 'commit', '-q', '-am', 'Feature commit')
    git(host.project, 'checkout', '-q', 'main')
    host.permissions('full')
    host.root()
    head = (host.project / '.git' / 'HEAD').read_bytes()
    job = host.process(writable=1)
    with pytest.raises(PolicyError, match='writable background command'):
        host.call('ProposeGitChange', {'operation': 'switch_branch', 'name': 'feature'}, 'switch-process')
    host.settle_jobs()
    assert (host.project / '.git' / 'HEAD').read_bytes() == head and host.plans() == []
    holder = host.project_child('holder', paths=['app.py'])
    with pytest.raises(PolicyError, match='delegated path lease'):
        host.call('ProposeGitChange', {'operation': 'switch_branch', 'name': 'feature'}, 'switch-lease')
    with pytest.raises(PolicyError, match='delegated path lease'):
        host.call('Write', {'file_path': 'app.py', 'content': 'VALUE = 7\n'}, 'root-write')
    (host.project / 'app.py').write_text('VALUE = 2\n')
    discard = host.call('ProposeGitChange', {'operation': 'discard', 'paths': '["app.py"]'}, 'discard')
    assert discard['status'] == 'awaiting_user_approval'
    with pytest.raises(PolicyError, match='delegated path lease'):
        host.approve(discard['action_id'])
    assert (host.project / 'app.py').read_text() == 'VALUE = 2\n' and (host.project / '.git' / 'HEAD').read_bytes() == head
    assert host.plans() == [] and job in host.fixture_jobs
    # Once the child stops, its lease is revoked and a fresh reviewed plan applies.
    app.agents.tree.cancel(host.chat, host.rid, host.rid, holder['id'])
    again = host.call('ProposeGitChange', {'operation': 'discard', 'paths': '["app.py"]'}, 'discard-again')
    assert host.approve(again['action_id'])['state'] == 'applied'
    assert (host.project / 'app.py').read_text() == BASE


# 5. Children


@macos
def test_worktree_write_tools_are_host_assigned_by_root_spawn_only(host):
    app = host.app
    host.permissions('auto')
    rid = host.root()
    # The model schema gate: write tools and leases are never model-chosen for a checkout child.
    for name, args in (('SpawnAgent', {'task': 'x', 'workspace': 'worktree'}),
                       ('SpawnWorktreeAgent', {'task': 'x', 'write_paths': json.dumps(['app.py'])})):
        with pytest.raises(PolicyError, match='allowed schema'):
            App.validate(name, args, TOOLS)
    for name in ('SpawnAgent', 'SpawnWorktreeAgent'):
        with pytest.raises(PolicyError, match='host-assigned'):
            host.call(name, {'task': 'Sneak a writer in.', 'name': 'sneaky',
                             'tools': json.dumps(['Read', 'WriteWorktreeFile'])}, 'spawn-tools-' + name)
    with pytest.raises(PolicyError, match='omit write_paths'):
        host.call('SpawnWorktreeAgent', {'task': 'Lease and own a checkout.', 'name': 'both',
                                         'write_paths': json.dumps(['app.py'])}, 'spawn-paths')
    assert app.agents.tree.list(host.chat, rid) == []
    # A child can neither name the root-only spawn route nor smuggle a workspace argument.
    parent = host.project_child('parent', tools=['Read', 'SpawnAgent'])
    node = app.agents.tree.get(host.chat, rid, parent['id'])
    offered = app.agents.child_tools(parent, node, False)
    for name, args in (('SpawnWorktreeAgent', {'task': 'Give me a checkout.'}),
                       ('SpawnAgent', {'task': 'Give me a checkout.', 'workspace': 'worktree'}),
                       ('WriteWorktreeFile', {'file_path': 'x.py', 'content': 'x'})):
        with pytest.raises(PolicyError, match='allowed schema'):
            app.validate(name, args, offered)
    with pytest.raises(PolicyError, match='outside child capabilities'):
        app.agents.child_tool(parent, 'SpawnWorktreeAgent', {'task': 'Give me a checkout.'}, '1:spawn')
    with pytest.raises(PolicyError):
        app.child_worktrees.write(parent, 'WriteWorktreeFile', {'file_path': 'x.py', 'content': 'x\n'}, '2:write')
    assert app.store.query('SELECT count(*) AS n FROM agent_worktrees')[0]['n'] == 0
    assert not app.git.anchor.exists() or not any(app.git.anchor.iterdir())
    host.permissions('auto', edits=False)
    host.root()
    with pytest.raises(PolicyError, match='file read and edit access'):
        host.spawn_worktree('readonly')
    assert not (host.project / 'x.py').exists()


@macos
def test_worktree_child_is_not_dispatched_before_allocation_and_rejection_stops_it(make_host):
    host = make_host(mode='ask', scheduler=True)
    app = host.app
    rid = host.root()
    host.scripts['waiting'] = lambda *args: pytest.fail('A child without its allocated worktree must never run.')
    node = host.spawn_worktree('waiting')
    assert node['worktree']['status'] == 'awaiting_user_approval'
    [action] = [item for item in app.store.actions(host.chat) if item['kind'] == 'worktree']
    assert action['status'] == 'pending' and action['payload']['operation'] == 'allocate_child_worktree'
    app.agents.wake()
    time.sleep(1.0)  # Several scheduler passes over the queued node.
    assert app.agents.tree.get(host.chat, rid, node['id'])['status'] == 'queued'
    assert host.providers == [] and not app.store.query('SELECT 1 FROM agent_claims WHERE agent_id=?', (node['id'],))
    assert app.child_worktrees.binding(rid, node['id'])['state'] == 'proposed'
    host.release()
    app.reject(host.chat, action['id'])
    eventually(lambda: app.agents.tree.get(host.chat, rid, node['id'])['status'] == 'stopped', 'The rejected child was not stopped.')
    assert app.child_worktrees.binding(rid, node['id'])['state'] == 'rejected' and host.providers == []
    assert not app.git.anchor.exists() or not any(app.git.anchor.iterdir())


@macos
def test_stale_generations_cannot_write_and_worktree_children_cannot_be_retried(host):
    app = host.app
    host.permissions('auto')
    rid = host.root()
    node = host.spawn_worktree('stale')
    worker = host.claim(node)
    tree = Path(app.child_worktrees.binding(rid, node['id'])['path'])
    assert host.child_call(worker, 'WriteWorktreeFile', {'file_path': 'one.py', 'content': '1\n'}, '1:one')['operation'] == 'create'
    impostor = {**worker, 'owner': 'another-worker'}
    with pytest.raises(PolicyError, match='no longer owns'):
        app.child_worktrees.write(impostor, 'WriteWorktreeFile', {'file_path': 'two.py', 'content': '2\n'}, '2:two')
    app.agents.tree.release(host.chat, rid, node['id'], 1, worker['owner'])
    assert app.agents.tree.get(host.chat, rid, node['id'])['status'] == 'interrupted'
    with pytest.raises(PolicyError, match='cannot be retried'):
        host.call('RetryAgent', {'agent_id': node['id']}, 'retry')
    with pytest.raises(PolicyError):
        app.child_worktrees.write(worker, 'WriteWorktreeFile', {'file_path': 'two.py', 'content': '2\n'}, '3:two')
    with pytest.raises(PolicyError):
        app.child_worktrees.write({**worker, 'generation': 2}, 'WriteWorktreeFile', {'file_path': 'two.py', 'content': '2\n'}, '4:two')
    assert not (tree / 'two.py').exists() and app.child_worktrees.binding(rid, node['id'])['writes'] == 1


@macos
def test_live_child_worktrees_refuse_export_removal_and_forgetting(host):
    app = host.app
    host.permissions('auto')
    rid = host.root()
    node = host.spawn_worktree('live')
    worker = host.claim(node)
    ident = app.child_worktrees.binding(rid, node['id'])['worktree_id']
    host.child_call(worker, 'WriteWorktreeFile', {'file_path': 'draft.py', 'content': 'DRAFT = 1\n'}, '1:draft')
    with pytest.raises(PolicyError, match='live child agent'):
        host.call('PreviewWorktreeExport', {'worktree_id': ident}, 'export')
    with pytest.raises(PolicyError, match='settled or was stopped|Wait for the current task'):
        host.native_retire(node['id'])
    host.release()
    for method, params in (('git_export', {'id': host.chat, 'worktree_id': ident}),
                           ('git_preview_remove', {'id': host.chat, 'worktree_id': ident})):
        with pytest.raises(PolicyError, match='live child agent'):
            app.dispatch(method, params)
    # Forgetting an active record is refused by GitWorkflows itself before the child guard.
    with pytest.raises(PolicyError, match='Remove an active worktree|live child agent'):
        app.dispatch('git_forget_worktree', {'id': host.chat, 'worktree_id': ident})
    with pytest.raises(PolicyError, match='live child agent'):
        app.git._guard('forget', app.git._row(host.chat, ident))
    assert app.store.query('SELECT state FROM git_worktrees WHERE id=?', (ident,)) == [{'state': 'active'}]
    with pytest.raises(PolicyError, match='settled or was stopped'):
        app.dispatch('agent_worktree_retire', {'id': host.chat, 'run_id': rid, 'agent_id': node['id'], 'discard': True})
    assert app.child_worktrees.binding(rid, node['id'])['state'] == 'active'
    # Settled but not integrated: retire refuses without the native discard review.
    host.own()
    app.agents.tree.settle(host.chat, rid, node['id'], 1, 'completed', {'text': 'Done.'}, [], worker['owner'])
    host.release()
    with pytest.raises(PolicyError, match='true or false'):
        app.dispatch('agent_worktree_retire', {'id': host.chat, 'run_id': rid, 'agent_id': node['id'], 'discard': 'yes'})
    with pytest.raises(PolicyError, match='not integrated'):
        app.dispatch('agent_worktree_retire', {'id': host.chat, 'run_id': rid, 'agent_id': node['id']})
    receipt = app.dispatch('agent_worktree_retire', {'id': host.chat, 'run_id': rid, 'agent_id': node['id'], 'discard': True})
    assert receipt['state'] == 'retired' and receipt['discard_review'] is True
    [kept] = receipt['preserved']
    assert kept['path'] == 'draft.py' and git(host.project, 'cat-file', '-p', kept['blob_oid']).stdout == b'DRAFT = 1\n'
    assert not (host.project / 'draft.py').exists()


# 6. Completion and evidence


@macos
def test_failing_empty_spoofed_and_overflowing_runs_never_satisfy_completion(host):
    host.permissions('auto')
    host.root()
    sources = {
        'failing': 'import unittest\n\nclass T(unittest.TestCase):\n    def test_fails(self):\n        self.assertEqual(1, 2)\n',
        'empty': 'import unittest\n',
        'spoofed': ('import sys, unittest\nprint("Ran 9 tests in 0.001s\\n\\nOK")\nsys.stderr.write("Ran 9 tests in 0.001s\\n\\nOK\\n")\n\n'
                    'class T(unittest.TestCase):\n    def test_fails(self):\n        self.fail("real failure")\n'),
    }
    outcomes = {}
    for name, source in sources.items():
        (host.project / 'test_app.py').write_text(source)
        staged = host.call('RunTests', {'recipe': 'python-unittest'}, 'tests-' + name)
        action = host.action(staged['action_id'])
        outcomes[name] = action['payload']['result']['outcome']
        assert action['status'] == 'applied' and not host.gate([action]), name
    assert outcomes == {'failing': 'failed', 'empty': 'no_tests', 'spoofed': 'failed'}
    (host.project / 'test_app.py').write_text('import unittest\nprint("x" * 250000)\n\nclass T(unittest.TestCase):\n'
                                              '    def test_ok(self):\n        pass\n')
    with pytest.raises(PolicyError, match='output limit'):
        host.call('RunTests', {'recipe': 'python-unittest'}, 'tests-overflow')
    assert [a['status'] for a in host.app.store.actions(host.chat)][-1] == 'failed' and not host.gate()
    (host.project / 'test_app.py').write_text(PASSING)
    passed = host.action(host.call('RunTests', {'recipe': 'python-unittest'}, 'tests-pass')['action_id'])
    assert passed['payload']['result']['outcome'] == 'passed' and host.gate([passed])
    result = passed['payload']['result']
    for variant in ({'snapshot_truncated': True}, {'kind': 'build'}, {'cancelled': True}, {'outcome': 'error'},
                    {'snapshot_digest': '0' * 64}, {'exit_code': 1}):
        forged = {**passed, 'payload': {**passed['payload'], 'result': {**result, **variant}}}
        assert not host.gate([forged]), variant
    # A later applied edit makes the earlier passing run stale.
    host.call('Read', {'file_path': 'README.md'}, 'read-readme')
    assert host.call('Write', {'file_path': 'README.md', 'content': '# Later\n'}, 'write-readme')['status'] == 'applied'
    assert not host.gate()


@macos
def test_tests_of_staged_drafts_count_only_once_those_exact_drafts_are_applied(host):
    (host.project / 'test_app.py').write_text(PASSING.replace('assertGreaterEqual(VALUE, 1)', 'assertEqual(VALUE, 3)'))
    git(host.project, 'commit', '-q', '-am', 'Expect VALUE 3')
    host.root()
    host.call('Read', {'file_path': 'app.py'}, 'read-app')
    edit = host.call('Write', {'file_path': 'app.py', 'content': 'VALUE = 3\n'}, 'write-app')
    tests = host.call('RunTests', {'recipe': 'python-unittest'}, 'tests-draft')
    assert edit['status'] == tests['status'] == 'awaiting_user_approval'
    result = host.approve(tests['action_id'])
    assert result['outcome'] == 'passed' and result['drafts_included'] == ['app.py']
    assert (host.project / 'app.py').read_text() == BASE and not host.gate(), 'An unapplied draft is not project evidence.'
    host.approve(edit['action_id'])
    assert (host.project / 'app.py').read_text() == 'VALUE = 3\n' and host.gate()
    (host.project / 'app.py').write_text('VALUE = 4\n')
    assert not host.gate()


def worktree_fix(host):
    """A worktree child fixes app.py in its checkout and asks the root to test that checkout."""
    app = host.app
    (host.project / 'test_app.py').write_text(PASSING.replace('assertGreaterEqual(VALUE, 1)', 'assertEqual(VALUE, 3)'))
    git(host.project, 'commit', '-q', '-am', 'Expect VALUE 3')
    host.permissions('auto')
    rid = host.root()
    node = host.spawn_worktree('fixer')
    assert {'RequestAgentEffect', 'ReadAgentEffect'} <= set(app.agents.tree.get(host.chat, rid, node['id'])['tools'])
    worker = host.claim(node)
    tree = Path(app.child_worktrees.binding(rid, node['id'])['path'])
    read = host.child_call(worker, 'Read', {'file_path': 'app.py'}, '1:read')
    host.child_call(worker, 'WriteWorktreeFile', {'file_path': 'app.py', 'content': 'VALUE = 3\n',
                                                  'expected_hash': read['file_hash']}, '2:fix')
    request = host.child_call(worker, 'RequestAgentEffect', {'tool': 'RunTests', 'arguments': json.dumps(
        {'recipe': 'python-unittest', 'target': 'worktree'})}, '3:worktree')
    assert request['status'] == 'proposed', request
    return rid, worker, tree, request


@macos
def test_worktree_children_cannot_turn_their_checkout_into_project_test_evidence(host):
    app = host.app
    rid, worker, tree, request = worktree_fix(host)
    effect = app.store.query('SELECT * FROM agent_effects WHERE id=?', (request['request_id'],))[0]
    assert effect['workspace'] == str(tree) and json.loads(effect['workspace_identity']) == list(Workspace(tree).identity)
    # A project-target request from a worktree child is refused: its view is not the project.
    refused = host.child_call(worker, 'RequestAgentEffect', {'tool': 'RunTests', 'arguments': json.dumps(
        {'recipe': 'python-unittest'})}, '4:project')
    assert refused['status'] == 'rejected'
    assert (host.project / 'app.py').read_text() == BASE and not host.gate()
    # The root's own run tests the project, which still fails without the child's change.
    run = host.action(host.call('RunTests', {'recipe': 'python-unittest'}, 'root-tests')['action_id'])
    assert run['payload']['root'] == str(host.project) and run['payload']['result']['outcome'] == 'failed'
    assert not host.gate() and [a['payload']['root'] for a in app.store.actions(host.chat) if a['kind'] == 'tests'] == [str(host.project)]


# 7. Undo (X10)


@macos
def test_edit_undo_refuses_active_tasks_uncertain_commands_replaced_roots_and_changed_files(host):
    app = host.app
    host.permissions('auto')
    rid = host.root()
    host.call('Read', {'file_path': 'app.py'}, 'read-app')
    replaced = host.call('Write', {'file_path': 'app.py', 'content': 'VALUE = 5\n'}, 'write-app')['action_id']
    created = host.call('Write', {'file_path': 'notes.md', 'content': 'notes\n'}, 'write-notes')['action_id']
    with pytest.raises(PolicyError, match='current task'):
        app.undo(host.chat, replaced)
    host.release()
    job = host.process(writable=0, state='unknown', uncertain=1)
    with pytest.raises(PolicyError, match='uncertain command'):
        app.undo(host.chat, replaced)
    app.process_inspections.acknowledge(host.chat, rid, job, 'no_effect_observed', 'Fixture inspection.')
    (host.project / 'app.py').write_text('VALUE = 6\n')
    with pytest.raises(PolicyError, match='changed'):
        app.undo(host.chat, replaced)
    (host.project / 'notes.md').write_text('edited\n')
    with pytest.raises(PolicyError, match='changed after approval'):
        app.undo(host.chat, created)
    (host.project / 'app.py').write_text('VALUE = 5\n')
    (host.project / 'notes.md').write_text('notes\n')
    moved = host.tmp / 'project-moved'
    host.project.rename(moved)
    shutil.copytree(moved, host.project, symlinks=True)
    with pytest.raises(PolicyError, match='replaced'):
        app.undo(host.chat, replaced)
    shutil.rmtree(host.project)
    moved.rename(host.project)
    holder = app.agents.tree.spawn(host.chat, rid, None, 'Hold app.py.', 'holder', ['Read'],
                                   {'max_steps': 2, 'max_seconds': 30, 'model_tokens': 8192}, ['app.py'])
    with pytest.raises(PolicyError, match='delegated path lease'):
        app.undo(host.chat, replaced)
    app.agents.tree.cancel(host.chat, rid, rid, holder['id'])
    assert {host.action(replaced)['status'], host.action(created)['status']} == {'applied'}
    assert (host.project / 'app.py').read_text() == 'VALUE = 5\n' and (host.project / 'notes.md').read_text() == 'notes\n'
    # A successful undo writes, reads back and records a verified receipt.
    assert app.undo(host.chat, replaced) is True and app.undo(host.chat, created) is True
    for action_id, expected in ((replaced, digest(BASE.encode())), (created, None)):
        action = host.action(action_id)
        receipt = action['payload']['undo']
        assert action['status'] == 'undone' and receipt['verified'] is True and receipt['scope'] == 'local file only'
        assert receipt['expected_hash'] == receipt['readback_hash'] == expected
    assert (host.project / 'app.py').read_text() == BASE and not (host.project / 'notes.md').exists()
    with pytest.raises(PolicyError, match='Only applied file edits'):
        app.undo(host.chat, replaced)


@macos
def test_git_commit_undo_is_a_new_reviewed_plan_bound_to_its_receipt(host):
    app = host.app
    host.permissions('full')
    base = host.head()
    host.root()
    (host.project / 'app.py').write_text('VALUE = 2\n')
    commit = host.call('ProposeGitChange', {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Undo me'}, 'commit')
    receipt_id = commit['result']['receipt_id']
    host.release()
    with pytest.raises(PolicyError, match='Only applied file edits'):
        app.undo(host.chat, commit['action_id'])
    staged = host.call('ProposeGitChange', {'operation': 'undo', 'receipt_id': receipt_id}, 'undo')
    assert staged['status'] == 'awaiting_user_approval', 'History-moving undo needs native review in every mode.'
    payload = host.action(staged['action_id'])['payload']
    assert payload['undo_of'] == receipt_id and payload['destructive'] is True and payload['requires_review'] is True
    assert payload['bindings']['original_plan_hash'] == commit['result']['plan_hash']
    undone = host.approve(staged['action_id'])
    assert undone['state'] == 'applied' and undone['readback']['verified'] is True
    assert host.head() == base and git(host.project, 'show', ':app.py').stdout == b'VALUE = 2\n'
    states = {row['id']: row for row in host.plans()}
    assert states[receipt_id]['state'] == 'undone' and states[receipt_id]['receipt']['undone_by'] == undone['receipt_id']
    with pytest.raises(PolicyError, match='this one is undone'):
        host.call('ProposeGitChange', {'operation': 'undo', 'receipt_id': receipt_id}, 'undo-again')
    with pytest.raises(PolicyError, match='cannot itself be undone'):
        host.call('ProposeGitChange', {'operation': 'undo', 'receipt_id': undone['receipt_id']}, 'undo-undo')
    # An undo after the ref moved is refused at preparation and at approval.
    again = host.call('ProposeGitChange', {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Again'}, 'commit-2')['result']
    git(host.project, 'commit', '-q', '--allow-empty', '-m', 'Moved by someone else')
    with pytest.raises(PolicyError, match='no longer points'):
        host.call('ProposeGitChange', {'operation': 'undo', 'receipt_id': again['receipt_id']}, 'undo-moved')
    (host.project / 'README.md').write_text('# Third\n')
    third = host.call('ProposeGitChange', {'operation': 'commit', 'paths': '["README.md"]', 'message': 'Third'}, 'commit-3')['result']
    pending = host.call('ProposeGitChange', {'operation': 'undo', 'receipt_id': third['receipt_id']}, 'undo-third')
    git(host.project, 'commit', '-q', '--allow-empty', '-m', 'Moved before approval')
    before = host.state()
    with pytest.raises(PolicyError, match='changed after review'):
        host.approve(pending['action_id'])
    assert host.state() == before and {row['id']: row['state'] for row in host.plans()}[third['receipt_id']] == 'applied'


# 8. Unknown effects


@macos
def test_unproven_git_readback_after_an_effect_is_uncertain_and_needs_reconciliation(host, monkeypatch):
    app = host.app
    (host.project / 'app.py').write_text('VALUE = 2\n')
    base = host.head()
    rid = host.root()
    staged = host.call('ProposeGitChange', {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Unproven'}, 'commit')

    def unavailable(*args, **kwargs):
        raise PolicyError('Fixture: authoritative readback is unavailable.')
    monkeypatch.setattr(app.git_plans, '_observe', unavailable)
    with pytest.raises(GitPlanUncertain):
        host.approve(staged['action_id'])
    [row] = host.plans()
    assert row['state'] == 'uncertain' and row['receipt']['effect_started'] is True
    assert app.runs.get(rid)['state'] == 'needs_reconciliation'
    assert host.action(staged['action_id'])['status'] == 'failed' and host.head() != base
    with pytest.raises(PolicyError, match='no longer awaiting approval'):
        host.approve(staged['action_id'])
    with pytest.raises(PolicyError, match='already executed'):
        app.git_plans.execute(host.chat, host.action(staged['action_id'])['payload'])
    monkeypatch.undo()
    check = app.dispatch('git_plan_readback', {'id': host.chat, 'receipt_id': row['id']})
    assert check['verified'] is True and check['matches_receipt'] is True and check['effect'] == 'none'
    assert [item['state'] for item in host.plans()] == ['uncertain']


@macos
def test_restart_turns_an_executing_git_plan_uncertain_and_never_retries(host, monkeypatch):
    app = host.app
    (host.project / 'app.py').write_text('VALUE = 2\n')
    base = host.head()
    rid = host.root()
    staged = host.call('ProposeGitChange', {'operation': 'commit', 'paths': '["app.py"]', 'message': 'Interrupted'}, 'commit')
    real = app.git._git

    def dying(args, *rest, **kwargs):
        result = real(args, *rest, **kwargs)
        if args and args[0] == 'update-ref':
            raise ProcessDied('The service stopped right after the ref moved.')
        return result

    def no_settlement(*args, **kwargs):
        raise ProcessDied('The service stopped before settlement.')
    monkeypatch.setattr(app.git, '_git', dying)
    monkeypatch.setattr(app.git_plans, '_settle', no_settlement)
    with pytest.raises(ProcessDied):
        host.approve(staged['action_id'])
    [row] = host.plans()
    assert row['state'] == 'executing' and row['receipt']['effect_started'] is True
    moved = host.head()
    assert moved != base
    host.restart()
    [row] = host.plans()
    assert row['state'] == 'uncertain' and row['receipt']['interrupted'] == 'executing'
    assert host.head() == moved and git(host.project, 'rev-list', '--count', base + '..HEAD').stdout.strip() == b'1'
    assert host.app.runs.get(rid)['state'] == 'needs_reconciliation'
    with pytest.raises(PolicyError, match='no longer awaiting approval'):
        host.approve(staged['action_id'])
    with pytest.raises(PolicyError, match='already executed'):
        host.app.git_plans.execute(host.chat, host.action(staged['action_id'])['payload'])
    assert host.head() == moved and [item['state'] for item in host.plans()] == ['uncertain']


@macos
def test_interrupted_worktree_allocation_restarts_uncertain_and_its_child_never_runs(host, monkeypatch):
    app = host.app
    host.permissions('auto')
    rid = host.root()
    real = app.git._git

    def dying(args, *rest, **kwargs):
        result = real(args, *rest, **kwargs)
        if list(args[:2]) == ['worktree', 'add']:
            raise ProcessDied('The service stopped right after Git added the worktree.')
        return result
    monkeypatch.setattr(app.git, '_git', dying)
    with pytest.raises(ProcessDied):
        host.spawn_worktree('interrupted')
    [row] = app.store.query('SELECT * FROM agent_worktrees')
    assert row['state'] == 'allocating'
    [action] = [item for item in app.store.actions(host.chat) if item['kind'] == 'worktree']
    host.restart()
    app = host.app
    [row] = app.store.query('SELECT * FROM agent_worktrees')
    assert row['state'] == 'uncertain' and json.loads(row['evidence'])['interrupted'] == 'allocating'
    assert app.store.query('SELECT state FROM git_worktrees WHERE id=?', (row['worktree_id'],)) == [{'state': 'uncertain'}]
    assert sorted(path.name for path in app.git.anchor.iterdir()) == [row['worktree_id']]
    assert not app.agents._worktree_ready(host.chat, rid, row['agent_id'])
    # Even after an explicit resume, the dispatcher gate stops the child instead of running it.
    app.runs.transition(rid, 'running')
    assert not app.agents._worktree_ready(host.chat, rid, row['agent_id'])
    assert app.agents.tree.get(host.chat, rid, row['agent_id'])['status'] == 'stopped'
    with pytest.raises(PolicyError, match='no longer pending'):
        app.child_worktrees.allocate(action['payload'], action_id=action['id'])
    assert sorted(path.name for path in app.git.anchor.iterdir()) == [row['worktree_id']] and host.providers == []


# 9. Egress


class Upstream:
    """A loopback fixture listener that counts accepted connections and echoes bytes."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.port, self.accepted, self.stop = self.sock.getsockname()[1], 0, threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.accepted += 1
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn):
        conn.settimeout(5)
        try:
            while data := conn.recv(65536):
                conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


def connect(broker, target):
    sock = socket.create_connection(('127.0.0.1', broker.port), timeout=5)
    sock.sendall(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode())
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    return sock, head


def test_egress_refuses_unlisted_hosts_ip_literals_and_rebinding_to_loopback(monkeypatch):
    upstream = Upstream()
    answers = {'registry.pebble.test': ['127.0.0.1'], 'mapped.pebble.test': ['::ffff:127.0.0.1'],
               'mixed.pebble.test': ['93.184.216.34', '127.0.0.1'], 'zero.pebble.test': ['0.0.0.0']}
    resolved = []

    def fake(host, port):
        resolved.append(host)
        return answers[host]
    monkeypatch.setattr(egress, 'resolve', fake)
    broker = EgressBroker([(name, upstream.port) for name in answers]).start()
    try:
        for target, code in ((f'other.pebble.test:{upstream.port}', 403), (f'registry.pebble.test:{upstream.port + 1}', 403),
                             (f'127.0.0.1:{upstream.port}', 403), (f'[::1]:{upstream.port}', 403),
                             (f'localhost:{upstream.port}', 403),
                             *((f'{name}:{upstream.port}', 403) for name in answers)):
            sock, head = connect(broker, target)
            sock.close()
            assert head.startswith(b'HTTP/1.1 %d ' % code), (target, head)
            assert target.encode() not in head
        reasons = [record['reason'] for record in broker.log()]
        assert reasons == ['not_allowlisted', 'not_allowlisted', 'ip_literal', 'ip_literal', 'not_allowlisted',
                           'refused_address', 'refused_address', 'refused_address', 'refused_address']
        assert resolved == list(answers), 'Only allowlisted names are resolved, once each.'
        assert upstream.accepted == 0 and not any(record['allowed'] for record in broker.log())
        for bad in ('127.0.0.1', '*.npmjs.org', 'xn--bcher-kva.example'):
            with pytest.raises(PolicyError):
                EgressBroker([(bad, 443)])
    finally:
        broker.close()
        upstream.close()


CONFINED = r'''
import json, socket, sys
broker, allowed, second = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3])
out = {}
def attempt(name, family, address):
    try:
        conn = socket.socket(family)
        conn.settimeout(3)
        conn.connect(address)
        conn.close()
        out[name] = 'connected'
    except OSError as exc:
        out[name] = exc.errno
attempt('direct_second', socket.AF_INET, ('127.0.0.1', second))
attempt('direct_second6', socket.AF_INET6, ('::1', second))
attempt('direct_allowed', socket.AF_INET, ('127.0.0.1', allowed))
def tunnel(target):
    conn = socket.create_connection(('127.0.0.1', broker), timeout=5)
    conn.sendall(('CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n' % (target, target)).encode())
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = conn.recv(1)
        if not chunk:
            break
        head += chunk
    status = head.split(b' ')[1].decode()
    echo = None
    if status == '200':
        conn.sendall(b'through-broker')
        echo = conn.recv(64).decode()
    conn.close()
    return status, echo
out['tunnel_second'] = tunnel('localhost:%d' % second)
out['tunnel_allowed'] = tunnel('localhost:%d' % allowed)
print(json.dumps(out))
'''


@macos
def test_confined_install_step_cannot_reach_a_second_local_port(host):
    allowed, second = Upstream(), Upstream()
    envs = host.app.environments
    folder = host.tmp / 'install-step'
    folder.mkdir()
    broker = EgressBroker([('localhost', allowed.port)], loopback_targets=True).start()
    try:
        framework = str(environments.FRAMEWORK)
        profile = envs._profile(folder, [framework], [framework], broker.port)
        python = os.path.realpath(environments.PYTHON)
        result = envs._run([python, '-I', '-c', CONFINED, str(broker.port), str(allowed.port), str(second.port)], folder,
                           {'PATH': '/usr/bin:/bin', 'HOME': str(folder), 'TMPDIR': str(folder)}, profile,
                           threading.Event(), time.monotonic() + 60, folder, 10_000_000)
    finally:
        broker.close()
        allowed.close()
        second.close()
    assert result['code'] == 0, result['output']
    out = json.loads(result['output'].strip().splitlines()[-1])
    assert out['direct_second'] == out['direct_second6'] == out['direct_allowed'] == 1, 'Sandbox denial (EPERM), not routing.'
    assert out['tunnel_second'] == ['403', None] and out['tunnel_allowed'] == ['200', 'through-broker']
    assert second.accepted == 0 and allowed.accepted == 1
    assert [record['reason'] for record in broker.log() if not record['allowed']] == ['not_allowlisted']


# Regressions for defects W5 observed; the parent fixed D1-D4 in source and removed the strict xfail markers.


@macos
def test_defect_native_approval_of_a_worktree_allocation_allocates_the_child_checkout(host):
    app = host.app
    rid = host.root()
    node = host.spawn_worktree('approved')
    assert node['worktree']['status'] == 'awaiting_user_approval'
    receipt = host.approve(node['worktree']['action_id'])
    assert receipt['state'] == 'active' and app.child_worktrees.binding(rid, node['id'])['state'] == 'active'


@macos
def test_defect_worktree_targeted_test_requests_execute_on_the_requesters_checkout(host):
    app = host.app
    rid, worker, tree, request = worktree_fix(host)
    executed = host.call('ExecuteAgentEffect', {'request_id': request['request_id']}, 'exec-worktree')
    [tests] = [a for a in app.store.actions(host.chat) if a['kind'] == 'tests']
    assert executed['effect_status'] == 'applied' and tests['payload']['root'] == str(tree)
    assert tests['payload']['result']['outcome'] == 'passed' and not host.gate()


@macos
def test_defect_git_plans_refuse_paths_inside_a_nested_repository(host):
    make_repo(host.project / 'vendor' / 'inner')
    # Git itself never adds a nested repository's files: the dry run adds nothing, status shows one opaque entry.
    dry = git(host.project, 'add', '--dry-run', '--', 'vendor/inner/app.py')
    assert dry.stdout == b'' and git(host.project, 'status', '--porcelain=v1', '--untracked-files=all').stdout == b'?? vendor/inner/\n'
    host.root()
    before = host.state()
    staged = {}
    for operation in ('stage', 'commit'):
        args = {'operation': operation, 'paths': '["vendor/inner/app.py"]', **({'message': 'Cross'} if operation == 'commit' else {})}
        try:
            staged[operation] = host.call('ProposeGitChange', args, 'nested-' + operation)
        except PolicyError:
            pass
    assert host.state() == before
    assert staged == {}, 'A plan naming a path inside a nested repository must be refused at preparation.'


@macos
def test_defect_a_child_worktree_test_result_never_counts_as_project_evidence(host):
    app = host.app
    rid, worker, tree, request = worktree_fix(host)
    # The exact nested root call AgentEffects.execute makes for a worktree target (App.execute on the
    # verified worktree Workspace), issued directly so this case does not depend on DEFECT 2.
    host.own()
    staged = app.execute('RunTests', {'recipe': 'python-unittest'}, worker['workspace'], host.chat, call_id='nested:effect')
    tests = host.action(staged['action_id'])
    assert tests['payload']['root'] == str(tree) and tests['payload']['result']['outcome'] == 'passed'
    assert not host.gate()
    host.call('Read', {'file_path': 'app.py'}, 'root-read')
    assert host.call('Write', {'file_path': 'app.py', 'content': 'VALUE = 3\n'}, 'root-write')['status'] == 'applied'
    assert not host.gate(), 'A test of the child worktree must not satisfy the project completion gate.'
