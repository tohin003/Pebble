"""Native Git API mapping and root worktree-export staging through the admitted broker.

The App, controller execution, root journal, source provenance, permission
broker, native approval route, agent path leases and GitWorkflows on real
temporary Git repositories are the checked-in implementations. AgentGuard
admission is a recording fixture that allows; the root model provider is never
called. Root tools reach GitAPI through a test route equivalent to the parent's
App._execute wiring (GROUP permission, then AgentGuard pre_tool_use). Fixture
Git commands are unconfined test-author commands inside pytest temp folders
only. No hook, credential, network, remote or real project repository is used.
"""

import copy
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from pebble import git_api
from pebble.agent_manager import Agents
from pebble.app import App
from pebble.controller import CURRENT
from pebble.git_api import GROUP, ROOT_TOOLS, SCHEMAS, GitAPI
from pebble.git_workflows import REVIEW_SECONDS
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError, Workspace

MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
PROFILE = {'id': 'fixture-cloud', 'kind': 'openai', 'name': 'Fixture provider', 'url': 'https://unused.invalid',
           'model': 'fixture', 'context': 8192, 'budget': 4096, 'tools': True}
LIMITS = {'max_steps': 4, 'max_seconds': 60, 'model_tokens': 32768}


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
                             cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
    if check and process.returncode:
        raise AssertionError(f'fixture git {args} failed: {process.stderr}')
    return process


def make_repo(path):
    path.mkdir()
    git(path, 'init', '-q')
    (path / 'app.py').write_text('print("base")\n')
    (path / 'README.md').write_text('# Fixture\n')
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '-m', 'Initial fixture commit')
    return path.resolve()


class Host:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp, self.monkeypatch = tmp_path.resolve(), monkeypatch
        monkeypatch.setattr(Agents, 'start', lambda self: None)
        self.project = make_repo(self.tmp / 'project')
        self.profile, self.guard_calls, self.root_calls = copy.deepcopy(PROFILE), [], []
        self.app = App(self.tmp / 'data', self.tmp / 'resources', lambda *args: None)
        monkeypatch.setattr(self.app.model, 'current', lambda: copy.deepcopy(self.profile))
        monkeypatch.setattr(self.app.model, 'complete', self.root_complete)
        monkeypatch.setattr(self.app.guard, 'handle', self.guard)
        self.api = GitAPI(self.app)
        self.native = self.api.methods()
        original = self.app._execute

        def routed(name, args, ws, cid):
            # The parent's App._execute wiring, condensed: GROUP, then AgentGuard.
            if name in ROOT_TOOLS:
                app = self.app
                policy = app.task_policy or app.store.setting('permissions', DEFAULT)
                if not policy['tools'][GROUP[name]]:
                    raise PolicyError('This tool is disabled in Permissions.')
                decision = app.guard.handle('pre_tool_use', ws.root, app.guard_session(cid), tool=name, arguments=args)
                if decision['action'] != 'allow':
                    raise PolicyError('AgentGuard: ' + str(decision.get('reason')))
                return self.api.root_tool(cid, name, args, ws, app.cancel)
            return original(name, args, ws, cid)

        monkeypatch.setattr(self.app, '_execute', routed)
        self.app.store.set_setting('permissions', {'mode': 'ask', 'tools': dict(DEFAULT['tools'])})
        self.chat = self.app.new_chat(str(self.project))['id']

    def root_complete(self, *args, **kwargs):
        self.root_calls.append(args)
        raise AssertionError('Git API fixtures must not make a root model request.')

    def guard(self, event, root, session, **kwargs):
        self.guard_calls.append({'event': event, 'root': str(root), 'session': session, 'tool': kwargs.get('tool'),
                                 'arguments': copy.deepcopy(kwargs.get('arguments'))})
        return {'action': 'allow', 'reason': 'Fixture Guard admission'}

    def worktree(self):
        review = self.native['git_preview_create'](id=self.chat)
        created = self.native['git_create_worktree'](id=self.chat, review_id=review['review_id'])
        return created['worktree_id'], Path(created['path'])

    def start_root(self):
        app = self.app
        chat = app.store.query('SELECT * FROM chats WHERE id=?', (self.chat,))[0]
        config = app.runtime_config(chat, False, {'max_steps': 64, 'max_seconds': 300})
        rid = app.runs.create(self.chat, 'Integrate reviewed managed worktree changes.', config)
        app.runs.transition(rid, 'running')
        self.own(rid)
        return rid

    def own(self, rid):
        """The test thread acts as the root worker: journal owner and controller execution."""
        app = self.app
        app.controller.live = None
        app.controller.enter(app.runs.get(rid, self.chat))
        app.active, app.active_run = self.chat, rid

    def release(self):
        self.app.active = self.app.active_run = None
        self.app.controller.live = None
        CURRENT.set(None)

    def call(self, rid, name, args, call_id):
        self.own(rid)
        return self.app.execute(name, args, Workspace(self.project), self.chat, call_id=call_id)

    def receipt(self, rid, call_id):
        return next((t for t in self.app.runs.tools(rid) if t['call_id'] == call_id), None)

    def rows(self):
        return self.app.store.query('SELECT id,state FROM git_worktrees WHERE chat_id=?', (self.chat,))

    def close(self):
        self.release()
        self.app.close()


@pytest.fixture
def host(tmp_path, monkeypatch):
    if not MAC:
        pytest.skip('Confined Git workflows are a macOS sandbox-exec product surface.')
    made = Host(tmp_path, monkeypatch)
    yield made
    made.close()
    assert made.root_calls == []


def head(repo):
    return git(repo, 'rev-parse', 'HEAD').stdout.strip()


def test_schemas_groups_and_native_surface_are_self_contained():
    assert [item['function']['name'] for item in SCHEMAS] == ['ReadGitWorktrees', 'PreviewWorktreeExport',
                                                              'ReadWorktreeExport', 'IntegrateWorktreeFile']
    assert all(item['function']['parameters']['additionalProperties'] is False for item in SCHEMAS)
    assert GROUP == {'ReadGitWorktrees': 'files', 'PreviewWorktreeExport': 'files', 'ReadWorktreeExport': 'files',
                     'IntegrateWorktreeFile': 'edits'}
    assert git_api.EFFECT_TOOLS == {'IntegrateWorktreeFile'} and set(git_api.LABELS) == set(ROOT_TOOLS)
    assert git_api.NATIVE_EFFECTS == {'git_create_worktree', 'git_remove_worktree', 'git_forget_worktree'}
    text = json.dumps(SCHEMAS)
    for word in ('preview_id', 'review_id', 'approve', 'push'):
        assert word not in text.lower().replace('native approval', '')
    assert 'tool_catalogue' not in Path(git_api.__file__).read_text()


@pytest.mark.skipif(not MAC, reason='Confined Git workflows are a macOS sandbox-exec product surface.')
def test_native_reviews_map_core_tokens_and_effects_wait_for_idle_tasks(host):
    app, native = host.app, host.native
    assert {'git_status', 'git_worktrees', 'git_preview_create', 'git_create_worktree', 'git_preview_remove',
            'git_remove_worktree', 'git_export', 'git_diff', 'git_history', 'git_identity', 'git_inspect',
            'git_export_file', 'git_forget_worktree', 'git_capabilities'} == set(native)
    assert native['git_status'](id=host.chat)['clean'] is True
    assert native['git_identity'](id=host.chat)['root'] == str(host.project)
    assert native['git_capabilities']()['root_effects'] == ['IntegrateWorktreeFile']
    started = time.time()
    review = native['git_preview_create'](id=host.chat)
    assert review['review_id'] == review['summary']['preview_id'] and review['review_id'] in app.git.previews
    assert review['base'] == review['summary']['base_commit'] == head(host.project)
    assert review['root'] == review['summary']['repository']['root'] == str(host.project)
    assert started + REVIEW_SECONDS <= review['expires'] <= time.time() + REVIEW_SECONDS
    assert review['requires_native_review'] is True and review['authorization'] == 'none'
    # Effects wait for an idle service; a refused call leaves the core token unconsumed.
    app.active = host.chat
    with pytest.raises(PolicyError, match='Stop the current task'):
        native['git_create_worktree'](id=host.chat, review_id=review['review_id'])
    app.active = None
    host.monkeypatch.setattr(app.agents, 'busy', lambda: True)
    with pytest.raises(PolicyError, match='delegated workers'):
        native['git_create_worktree'](id=host.chat, review_id=review['review_id'])
    host.monkeypatch.setattr(app.agents, 'busy', lambda: False)
    app.closed = True
    try:
        with pytest.raises(PolicyError, match='shutting down'):
            native['git_create_worktree'](id=host.chat, review_id=review['review_id'])
    finally:
        app.closed = False
    assert review['review_id'] in app.git.previews and host.rows() == []
    other = app.new_chat(str(host.project))['id']
    with pytest.raises(PolicyError, match='Preview it again'):
        native['git_create_worktree'](id=other, review_id=review['review_id'])
    with pytest.raises(PolicyError, match='Preview it again'):
        native['git_create_worktree'](id=host.chat, review_id=review['review_id'])

    review = native['git_preview_create'](id=host.chat)
    created = native['git_create_worktree'](id=host.chat, review_id=review['review_id'])
    assert created['state'] == 'ready' and created['base'] == review['base'] and created['receipt']['detached'] is True
    [item] = native['git_worktrees'](id=host.chat)['worktrees']
    assert (item['id'], item['state'], item['dirty'], item['base'], item['path']) == (
        created['worktree_id'], 'ready', False, review['base'], created['path'])
    assert item['actions'] == ['export', 'preview_remove'] and item['verified'] is True and item['error'] is None
    assert native['git_worktrees'](id=other)['worktrees'] == []
    with pytest.raises(PolicyError, match='Conversation not found'):
        native['git_worktrees'](id='missing-chat')
    with pytest.raises(PolicyError, match='not found in this conversation'):
        native['git_preview_remove'](id=other, worktree_id=created['worktree_id'])
    removal = native['git_preview_remove'](id=host.chat, worktree_id=created['worktree_id'])
    assert removal['review_id'] == removal['summary']['preview_id'] and removal['root'] == str(host.project)
    assert removal['base'] == review['base'] and removal['expires'] <= time.time() + REVIEW_SECONDS
    app.active = host.chat
    with pytest.raises(PolicyError, match='Stop the current task'):
        native['git_remove_worktree'](id=host.chat, review_id=removal['review_id'])
    app.active = None
    removed = native['git_remove_worktree'](id=host.chat, review_id=removal['review_id'])
    assert removed['state'] == 'removed' and not Path(created['path']).exists()
    [item] = native['git_worktrees'](id=host.chat)['worktrees']
    assert item['state'] == 'removed' and item['actions'] == ['forget'] and item['present'] == {'folder': False, 'metadata': False}
    assert native['git_forget_worktree'](id=host.chat, worktree_id=created['worktree_id'])['forgotten'] == created['worktree_id']
    assert host.rows() == []


@pytest.mark.skipif(not MAC, reason='Confined Git workflows are a macOS sandbox-exec product surface.')
def test_listing_keeps_audit_and_chat_deletion_preserves_owned_worktree_data(host, monkeypatch):
    ident, tree = host.worktree()
    (tree / 'app.py').write_text('print("unsaved worktree work")\n')
    [item] = host.native['git_worktrees'](id=host.chat)['worktrees']
    assert item['dirty'] is True and item['change_count'] == 1 and item['actions'] == ['export']
    exported = host.native['git_export'](id=host.chat, worktree_id=ident, file_paths=['app.py'])
    assert exported['proposals'][0]['content'] == 'print("unsaved worktree work")\n' and exported['root'] == str(host.project)
    assert exported['skipped'] == [] and exported['authorization'] == 'none'
    with pytest.raises(PolicyError, match='still owns 1'):
        host.api.assert_chat_deletable(host.chat)
    with pytest.raises(PolicyError, match='managed worktree'):
        host.app.delete_chat(host.chat)
    assert (tree / 'app.py').read_text() == 'print("unsaved worktree work")\n' and len(host.rows()) == 1

    original = host.app.git.inspect

    def broken(*args, **kwargs):
        raise RuntimeError('fixture inspection failure')

    monkeypatch.setattr(host.app.git, 'inspect', broken)
    [item] = host.native['git_worktrees'](id=host.chat)['worktrees']
    assert item['state'] == 'ready' and item['verified'] is False and 'fixture inspection failure' in item['error']
    assert item['audit']['phase'] == 'created and verified' and item['dirty'] is None and item['actions'] == []
    monkeypatch.setattr(host.app.git, 'inspect', original)

    (tree / 'app.py').write_text('print("base")\n')
    removal = host.native['git_preview_remove'](id=host.chat, worktree_id=ident)
    assert host.native['git_remove_worktree'](id=host.chat, review_id=removal['review_id'])['state'] == 'removed'
    # A settled record whose metadata still exists also blocks deletion.
    ghost = uuid.uuid4().hex
    repo = host.app.git._repository(host.chat)
    host.app.store.query('INSERT INTO git_worktrees VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (
        ghost, host.chat, str(host.project), json.dumps(repo['identity']['root_identity']),
        json.dumps(repo['identity']['git_identity']), str(host.app.git.anchor / ghost), repo['commit'], repo['ref'], None,
        'failed', '{"phase":"creating"}', time.time(), time.time()))
    (host.project / '.git' / 'worktrees' / ghost).mkdir(parents=True)
    with pytest.raises(PolicyError, match='still owns 1'):
        host.api.assert_chat_deletable(host.chat)
    (host.project / '.git' / 'worktrees' / ghost).rmdir()
    check = host.api.assert_chat_deletable(host.chat)
    assert check['deletable'] is True and set(check['settled_records']) == {ident, ghost}
    assert set(host.api.forget_chat_records(host.chat)['forgotten']) == {ident, ghost} and host.rows() == []
    assert host.app.delete_chat(host.chat) is True


@pytest.mark.skipif(not MAC, reason='Confined Git workflows are a macOS sandbox-exec product surface.')
def test_active_record_whose_data_vanished_is_forgotten_only_natively(host):
    ident, tree = host.worktree()
    git(host.project, 'worktree', 'remove', '--force', str(tree))
    [item] = host.native['git_worktrees'](id=host.chat)['worktrees']
    assert item['state'] == 'ready' and item['verified'] is False and 'missing' in item['error']
    assert item['actions'] == ['forget'] and item['present'] == {'folder': False, 'metadata': False}
    with pytest.raises(PolicyError, match='still owns 1'):
        host.api.assert_chat_deletable(host.chat)
    host.app.active = host.chat
    with pytest.raises(PolicyError, match='Stop the current task'):
        host.native['git_forget_worktree'](id=host.chat, worktree_id=ident)
    host.app.active = None
    assert host.native['git_forget_worktree'](id=host.chat, worktree_id=ident)['forgotten'] == ident
    assert host.rows() == [] and host.api.assert_chat_deletable(host.chat)['settled_records'] == []


@pytest.mark.skipif(not MAC, reason='Confined Git workflows are a macOS sandbox-exec product surface.')
@pytest.mark.parametrize('suffix', ['read', 'write'])
@pytest.mark.parametrize('completed', [False, True])
def test_provider_sibling_cannot_supply_a_nested_integration_receipt(host, suffix, completed):
    ident, tree = host.worktree()
    content = 'print("from the worktree")\n'
    (tree / 'app.py').write_text(content)
    rid = host.start_root()
    manifest = host.call(rid, 'PreviewWorktreeExport', {'worktree_id': ident}, 'preview')
    export = {'export_id': manifest['export_id'], 'file_path': 'app.py'}
    host.call(rid, 'ReadWorktreeExport', export, 'inspect')
    name = 'Read' if suffix == 'read' else 'Write'
    args = {'file_path': 'app.py'}
    if suffix == 'write':
        args['content'] = content
    host.app.runs.response(rid, {'role': 'assistant', 'tool_calls': [
        {'id': 'integrate', 'function': {'name': 'IntegrateWorktreeFile', 'arguments': json.dumps(export)}},
        {'id': 'integrate:' + suffix, 'function': {'name': name, 'arguments': json.dumps(args)}},
    ]}, {}, steps=1, elapsed=0)
    sibling = '1:integrate:' + suffix
    if completed:
        # Commit a denied sibling outcome: integration must not adopt even a
        # completed error cache as a newly admitted host operation.
        host.app.runs.begin_tool(rid, sibling, name, args)
        host.app.runs.end_tool(rid, sibling, {'error': 'fixture denied sibling'})
    before = host.receipt(rid, sibling)
    guard_before = len(host.guard_calls)
    with pytest.raises(PolicyError, match='collides'):
        host.call(rid, 'IntegrateWorktreeFile', export, '1:integrate')
    assert host.receipt(rid, sibling) == before
    assert host.app.store.actions(host.chat) == []
    assert (host.project / 'app.py').read_text() == 'print("base")\n'
    assert not any(c['tool'] in {'Read', 'Write'} for c in host.guard_calls[guard_before:])


@pytest.mark.skipif(not MAC, reason='Confined Git workflows are a macOS sandbox-exec product surface.')
def test_root_stages_an_inspected_export_and_only_native_approval_writes(host):
    ident, tree = host.worktree()
    (tree / 'app.py').write_text('print("from the worktree")\n')
    rid = host.start_root()
    listing = host.call(rid, 'ReadGitWorktrees', {}, 'list')
    assert [(w['worktree_id'], w['state']) for w in listing['worktrees']] == [(ident, 'ready')]
    assert listing['authorization'] == 'none'
    manifest = host.call(rid, 'PreviewWorktreeExport', {'worktree_id': ident}, 'preview')
    [entry] = manifest['proposals']
    assert entry == {'path': 'app.py', 'operation': 'replace', 'base_hash': sha(b'print("base")\n'), 'base_oid': entry['base_oid'],
                     'content_hash': sha(b'print("from the worktree")\n'), 'bytes': 27}
    assert manifest['content_included'] is False and 'from the worktree' not in json.dumps(manifest)
    export = {'export_id': manifest['export_id'], 'file_path': 'app.py'}
    with pytest.raises(PolicyError, match='ReadWorktreeExport'):
        host.call(rid, 'IntegrateWorktreeFile', export, 'too-early')
    assert host.app.store.actions(host.chat) == [] and host.receipt(rid, 'too-early:read') is None
    page = host.call(rid, 'ReadWorktreeExport', export, 'inspect')
    assert page['untrusted_content'] == 'print("from the worktree")\n' and page['next_offset'] is None
    assert page['content_hash'] == entry['content_hash'] and page['applied'] is False and page['authorization'] == 'none'

    staged = host.call(rid, 'IntegrateWorktreeFile', export, 'integrate')
    assert staged['status'] == 'awaiting_user_approval' and staged['receipt_id'] == 'integrate'
    assert (staged['export_receipt_id'], staged['inspection_receipt_id'], staged['read_receipt_id'], staged['write_receipt_id']) == (
        'preview', 'inspect', 'integrate:read', 'integrate:write')
    assert (host.project / 'app.py').read_text() == 'print("base")\n'
    [action] = host.app.store.actions(host.chat)
    assert action['kind'] == 'edit' and action['status'] == 'pending' and action['payload']['permission_mode'] == 'ask'
    assert action['payload']['after'] == 'print("from the worktree")\n' and action['payload']['baseline'] == entry['base_hash']
    read, write, outer = (host.receipt(rid, c) for c in ('integrate:read', 'integrate:write', 'integrate'))
    assert read['name'] == 'Read' and read['state'] == 'completed' and read['result']['file_hash'] == entry['base_hash']
    assert write['name'] == 'Write' and write['arguments'] == {'file_path': 'app.py', 'content': 'print("from the worktree")\n'}
    assert write['result']['action_id'] == action['id'] == outer['result']['action_id'] and outer['state'] == 'completed'
    owners = host.app.store.query('SELECT run_id,call_id,kind FROM run_action_owners WHERE action_id=?', (action['id'],))
    assert owners == [{'run_id': rid, 'call_id': 'integrate:write', 'kind': 'edit'}]
    admitted = [c['tool'] for c in host.guard_calls if c['event'] == 'pre_tool_use' and c['session'] == rid]
    assert admitted[-3:] == ['IntegrateWorktreeFile', 'Read', 'Write']

    with pytest.raises(PolicyError, match='not a model tool'):
        host.api.root_tool(host.chat, 'ApproveAction', {'action_id': action['id']}, Workspace(host.project))
    host.release()
    assert host.app.approve(host.chat, action['id'])['applied'] == 'app.py'
    assert (host.project / 'app.py').read_text() == 'print("from the worktree")\n'
    write = host.receipt(rid, 'integrate:write')
    assert write['state'] == 'completed' and write['result']['status'] == 'applied' and write['result']['action_id'] == action['id']
    assert (tree / 'app.py').read_text() == 'print("from the worktree")\n'
    # The applied project no longer matches the export base, so the export cannot be staged again.
    with pytest.raises(PolicyError, match='project file changed'):
        host.call(rid, 'IntegrateWorktreeFile', export, 'again')
    assert len(host.app.store.actions(host.chat)) == 1


@pytest.mark.skipif(not MAC, reason='Confined Git workflows are a macOS sandbox-exec product surface.')
def test_integration_refuses_stale_base_changed_worktree_and_run_drift(host):
    ident, tree = host.worktree()
    (tree / 'app.py').write_text('print("worktree")\n')
    (tree / 'README.md').write_text('# Worktree readme\n')
    rid = host.start_root()
    export = host.call(rid, 'PreviewWorktreeExport', {'worktree_id': ident}, 'preview')['export_id']
    args = lambda path: {'export_id': export, 'file_path': path}
    host.call(rid, 'ReadWorktreeExport', args('app.py'), 'read-app')
    host.call(rid, 'ReadWorktreeExport', args('README.md'), 'read-readme')
    (host.project / 'README.md').write_text('# Parent edit after export\n')
    with pytest.raises(PolicyError, match='project file changed'):
        host.call(rid, 'IntegrateWorktreeFile', args('README.md'), 'stale')
    (tree / 'app.py').write_text('print("changed after inspection")\n')
    with pytest.raises(PolicyError, match='worktree file changed'):
        host.call(rid, 'IntegrateWorktreeFile', args('app.py'), 'changed')
    (tree / 'app.py').write_text('print("worktree")\n')
    host.profile['model'] = 'fixture-replacement'
    with pytest.raises(PolicyError, match='changed since this run started'):
        host.call(rid, 'IntegrateWorktreeFile', args('app.py'), 'route-drift')
    host.profile['model'] = PROFILE['model']
    host.app.store.set_setting('permissions', {'mode': 'ask', 'tools': {**DEFAULT['tools'], 'web': False}})
    with pytest.raises(PolicyError, match='changed since this run started'):
        host.call(rid, 'IntegrateWorktreeFile', args('app.py'), 'permission-drift')
    host.app.store.set_setting('permissions', {'mode': 'ask', 'tools': dict(DEFAULT['tools'])})
    assert host.app.store.actions(host.chat) == []
    for call_id in ('stale', 'changed', 'route-drift', 'permission-drift'):
        assert host.receipt(rid, call_id + ':read') is None and host.receipt(rid, call_id + ':write') is None
    staged = host.call(rid, 'IntegrateWorktreeFile', args('app.py'), 'staged')
    assert staged['status'] == 'awaiting_user_approval' and staged['content_hash'] == sha(b'print("worktree")\n')
    with pytest.raises(PolicyError, match='already awaiting review'):
        host.call(rid, 'IntegrateWorktreeFile', args('app.py'), 'twice')
    assert len(host.app.store.actions(host.chat)) == 1 and (host.project / 'app.py').read_text() == 'print("base")\n'


@pytest.mark.skipif(not MAC, reason='Confined Git workflows are a macOS sandbox-exec product surface.')
def test_new_files_skip_readback_and_skipped_or_recreated_paths_cannot_stage(host):
    ident, tree = host.worktree()
    (tree / 'pkg').mkdir()
    (tree / 'pkg' / 'feature.py').write_text('VALUE = 1\n')
    (tree / 'other.py').write_text('OTHER = 1\n')
    (tree / 'README.md').write_text('# Worktree readme\n')
    (host.project / 'README.md').write_text('# Dirty parent readme\n')
    rid = host.start_root()
    manifest = host.call(rid, 'PreviewWorktreeExport', {'worktree_id': ident}, 'preview')
    assert sorted(p['path'] for p in manifest['proposals']) == ['other.py', 'pkg/feature.py']
    assert {s['path']: s['reason'] for s in manifest['skipped']} == {'README.md': 'stale_parent_changed'}
    args = lambda path: {'export_id': manifest['export_id'], 'file_path': path}
    with pytest.raises(PolicyError, match='listed by a completed PreviewWorktreeExport'):
        host.call(rid, 'ReadWorktreeExport', args('README.md'), 'read-skipped')
    with pytest.raises(PolicyError, match='listed by a completed PreviewWorktreeExport'):
        host.call(rid, 'IntegrateWorktreeFile', args('README.md'), 'stage-skipped')
    for path, call_id in (('pkg/feature.py', 'read-feature'), ('other.py', 'read-other')):
        host.call(rid, 'ReadWorktreeExport', args(path), call_id)
    (host.project / 'other.py').write_text('PARENT = 1\n')
    with pytest.raises(PolicyError, match='project file changed'):
        host.call(rid, 'IntegrateWorktreeFile', args('other.py'), 'stage-recreated')
    staged = host.call(rid, 'IntegrateWorktreeFile', args('pkg/feature.py'), 'stage-feature')
    assert staged['status'] == 'awaiting_user_approval' and staged['operation'] == 'create' and staged['base_hash'] is None
    assert staged['read_receipt_id'] is None and host.receipt(rid, 'stage-feature:read') is None
    assert not (host.project / 'pkg').exists() and (host.project / 'other.py').read_text() == 'PARENT = 1\n'
    [action] = host.app.store.actions(host.chat)
    assert action['payload']['baseline'] is None and action['payload']['path'] == 'pkg/feature.py'
    host.release()
    host.app.approve(host.chat, action['id'])
    assert (host.project / 'pkg' / 'feature.py').read_text() == 'VALUE = 1\n'
    assert (host.project / 'README.md').read_text() == '# Dirty parent readme\n'


@pytest.mark.skipif(not MAC, reason='Confined Git workflows are a macOS sandbox-exec product surface.')
def test_child_write_leases_still_block_integration(host):
    ident, tree = host.worktree()
    (tree / 'app.py').write_text('print("worktree")\n')
    rid = host.start_root()
    export = host.call(rid, 'PreviewWorktreeExport', {'worktree_id': ident}, 'preview')['export_id']
    host.call(rid, 'ReadWorktreeExport', {'export_id': export, 'file_path': 'app.py'}, 'inspect')
    host.app.agents.tree.spawn(host.chat, rid, None, 'Hold the application file.', 'holder', ['Read'], LIMITS, ['app.py'])
    with pytest.raises(PolicyError, match='delegated path lease'):
        host.call(rid, 'IntegrateWorktreeFile', {'export_id': export, 'file_path': 'app.py'}, 'leased')
    assert host.app.store.actions(host.chat) == [] and host.receipt(rid, 'leased:read') is None
    assert host.receipt(rid, 'leased:write') is None and (host.project / 'app.py').read_text() == 'print("base")\n'


@pytest.mark.skipif(not MAC, reason='Confined Git workflows are a macOS sandbox-exec product surface.')
def test_model_routes_carry_no_native_control_and_export_ids_are_run_scoped(host):
    ident, tree = host.worktree()
    (tree / 'app.py').write_text('print("root view")\n')
    native_export = host.native['git_export'](id=host.chat, worktree_id=ident)
    pending = host.native['git_preview_create'](id=host.chat)
    rid = host.start_root()
    with pytest.raises(PolicyError, match='listed by a completed PreviewWorktreeExport'):
        host.call(rid, 'ReadWorktreeExport', {'export_id': native_export['export_id'], 'file_path': 'app.py'}, 'foreign-read')
    with pytest.raises(PolicyError, match='listed by a completed PreviewWorktreeExport'):
        host.call(rid, 'IntegrateWorktreeFile', {'export_id': native_export['export_id'], 'file_path': 'app.py'}, 'foreign-stage')
    for name, args in (('CreateGitWorktree', {'review_id': pending['review_id']}), ('RemoveGitWorktree', {'worktree_id': ident}),
                       ('ForgetGitWorktree', {'worktree_id': ident}), ('GitPush', {}), ('ApproveAction', {})):
        with pytest.raises(PolicyError, match='not a model tool'):
            host.api.root_tool(host.chat, name, args, Workspace(host.project))
    with pytest.raises(PolicyError, match='Stop the current task'):
        host.native['git_create_worktree'](id=host.chat, review_id=pending['review_id'])
    assert pending['review_id'] in host.app.git.previews and len(host.rows()) == 1
    host.call(rid, 'ReadGitWorktrees', {}, 'list')
    manifest = host.call(rid, 'PreviewWorktreeExport', {'worktree_id': ident, 'file_paths': json.dumps(['app.py'])}, 'preview')
    assert [p['path'] for p in manifest['proposals']] == ['app.py'] and 'root view' not in json.dumps(manifest)
    with pytest.raises(PolicyError, match='JSON array'):
        host.call(rid, 'PreviewWorktreeExport', {'worktree_id': ident, 'file_paths': 'app.py'}, 'bad-paths')
    with pytest.raises(PolicyError, match='Unexpected or missing'):
        host.call(rid, 'ReadWorktreeExport', {'export_id': manifest['export_id']}, 'missing-path')
    text = json.dumps([t['result'] for t in host.app.runs.tools(rid)])
    assert pending['review_id'] not in text and 'preview_id' not in text and 'review_id' not in text
    # Another conversation, a released root or a mismatched receipt owns nothing here.
    other = host.app.new_chat(str(host.project))['id']
    with pytest.raises(PolicyError, match='owning running root'):
        host.api.root_tool(other, 'ReadGitWorktrees', {}, Workspace(host.project))
    host.app.runs.begin_tool(rid, 'mismatch', 'ReadGitWorktrees', {})
    host.app.current_tool_call = 'mismatch'
    try:
        with pytest.raises(PolicyError, match='own running root receipt'):
            host.api.root_tool(host.chat, 'PreviewWorktreeExport', {'worktree_id': ident}, Workspace(host.project))
    finally:
        host.app.runs.end_tool(rid, 'mismatch', {'status': 'fixture'})
        host.app.current_tool_call = None
    host.release()
    with pytest.raises(PolicyError, match='owning running root'):
        host.api.root_tool(host.chat, 'ReadGitWorktrees', {}, Workspace(host.project))
    assert host.app.store.actions(host.chat) == [] and (host.project / 'app.py').read_text() == 'print("base")\n'


@pytest.mark.skipif(not MAC, reason='Confined Git workflows are a macOS sandbox-exec product surface.')
def test_export_pages_follow_the_route_window_and_replaced_roots_fail(host):
    ident, tree = host.worktree()
    body = ''.join(f'line {index:04}\n' for index in range(200))
    (tree / 'notes.txt').write_text(body)
    rid = host.start_root()
    export = host.call(rid, 'PreviewWorktreeExport', {'worktree_id': ident}, 'preview')['export_id']
    args = {'export_id': export, 'file_path': 'notes.txt'}
    first = host.call(rid, 'ReadWorktreeExport', {**args, 'limit': 6000}, 'page-1')
    assert len(first['untrusted_content']) == 600 and first['next_offset'] == 600 and first['total_characters'] == len(body)
    second = host.call(rid, 'ReadWorktreeExport', {**args, 'offset': 600, 'limit': 6000}, 'page-2')
    assert second['untrusted_content'] == body[600:1200]
    host.profile['budget'] = 32_000
    whole = host.call(rid, 'ReadWorktreeExport', {**args, 'limit': 6000}, 'page-large')
    assert whole['untrusted_content'] == body and whole['next_offset'] is None
    host.profile['budget'] = PROFILE['budget']
    with pytest.raises(PolicyError, match='past the end'):
        host.call(rid, 'ReadWorktreeExport', {**args, 'offset': len(body) + 1}, 'page-past')
    stale = Workspace(host.project)
    host.project.rename(host.tmp / 'project-original')
    make_repo(host.project)
    host.own(rid)
    with pytest.raises(PolicyError, match='renamed, replaced or symlinked'):
        host.app.execute('IntegrateWorktreeFile', args, stale, host.chat, call_id='replaced')
    assert host.app.store.actions(host.chat) == []
