"""Authoritative readback: each verdict for each action kind on real records, files and repositories.

A real Store and RunJournal own every broker action and tool receipt. Edits use
a temporary project through Workspace; Git uses a real temporary repository
through GitPlans' confined readback (macOS sandbox-exec), mirroring
test_git_plans; installs use Environments' own anchor under pytest tmp_path.
Fixture Git commands are unconfined test-author commands inside tmp_path with
hooks disabled. Readback must never write: every check compares a full
database dump before and after. Nothing here spawns a job or sends a signal.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import reconcile
from pebble.environments import Environments, tree
from pebble.policy import PolicyError, Workspace, digest
from pebble.process_jobs import PROCESS_DDL
from pebble.reconcile import readback, reconcile_run
from pebble.runtime import RunJournal
from pebble.store import Store

MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
macos = pytest.mark.skipif(not MAC, reason='Confined Git plans are a macOS sandbox-exec product surface.')


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
    git(path, 'config', 'user.name', 'Reviewed Author')
    git(path, 'config', 'user.email', 'author@example.invalid')
    return path.resolve()


def rev(repo, name='HEAD'):
    return git(repo, 'rev-parse', name).stdout.strip()


class Host:
    def __init__(self, tmp_path, repo=False):
        self.tmp = tmp_path.resolve()
        self.home = self.tmp / 'data'
        self.project = make_repo(self.tmp / 'project') if repo else self.tmp / 'project'
        self.project.mkdir(exist_ok=True)
        self.store = Store(self.home)
        self.runs = RunJournal(self.store, self.home)
        self.ws = Workspace(self.project)
        self.cid = self.store.new_chat(str(self.ws.root))
        self.rid = self.runs.create(self.cid, 'Reconcile fixture', {
            'workspace': str(self.ws.root), 'workspace_identity': list(self.ws.identity),
            'limits': {'max_steps': 8, 'max_seconds': 900.0}})
        self.app = SimpleNamespace(home=self.home, store=self.store, runs=self.runs)
        self.count = 0

    def action(self, kind, payload, tool, status='running', cid=None, owner_kind=None):
        """A broker action bound to a running root tool receipt, as App.propose leaves it."""
        self.count += 1
        cid = cid or self.cid
        call_id = f'{self.count}:call_{self.count}'
        self.runs.begin_tool(self.rid, call_id, tool, {'fixture': self.count})
        aid = self.store.action(cid, kind, payload)
        self.runs.bind_action(self.rid, call_id, aid, owner_kind or kind)
        self.store.query('UPDATE actions SET status=? WHERE id=?', (status, aid))
        return self.get(aid, cid), call_id

    def get(self, aid, cid=None):
        return next(a for a in self.store.actions(cid or self.cid) if a['id'] == aid)

    def applied(self, action, call_id, result):
        self.runs.commit_action(action['id'], action['payload'], result, action['kind'],
                                {'run_id': self.rid, 'call_id': call_id})
        return self.get(action['id'], action['chat_id'])

    def draft(self, name, content):
        return {**Workspace(self.project).draft(name, content), 'requires_review': False, 'permission_mode': 'ask'}

    def dump(self):
        with self.store.lock:
            return list(self.store.db.iterdump())

    def check(self, action, verdict, basis, **kw):
        before = self.dump()
        result = readback(self.app, action, **kw)
        assert self.dump() == before  # readback is read-only
        assert set(result) == {'verdict', 'evidence', 'authorization'} and result['authorization'] == 'none'
        assert (result['verdict'], result['evidence']['basis']) == (verdict, basis), result
        if verdict == 'unknown':
            assert result['evidence']['reason']
        return result['evidence']

    def close(self):
        self.runs.close()
        self.store.close()


@pytest.fixture
def host(tmp_path):
    host = Host(tmp_path)
    yield host
    host.close()


@pytest.fixture
def git_host(tmp_path):
    if not MAC:
        pytest.skip('Confined Git plans are a macOS sandbox-exec product surface.')
    from pebble.git_plans import GitPlans
    from pebble.git_workflows import GitWorkflows
    host = Host(tmp_path, repo=True)
    host.git = GitWorkflows(host.app)
    host.plans = host.app.git_plans = GitPlans(host.app, host.git)
    yield host
    host.git.close()
    host.close()


# Edits


def test_edit_verdicts_compare_current_bytes_with_recorded_hashes(host):
    (host.project / 'notes.txt').write_text('old\n')
    draft = host.draft('notes.txt', 'new\n')
    pending, _ = host.action('edit', draft, 'Write', status='pending')
    host.check(pending, 'not_occurred', 'never_performed')
    running, call = host.action('edit', draft, 'Write')
    facts = host.check(running, 'not_occurred', 'before_present')
    assert facts['current_hash'] == facts['before_hash'] == digest(b'old\n') and facts['path'] == 'notes.txt'
    assert facts['run_id'] == host.rid and facts['call_id'] == call and facts['action_status'] == 'running'
    Workspace.apply(draft)  # the effect happened, but its receipt never committed
    facts = host.check(running, 'occurred', 'after_present')
    assert facts['current_hash'] == digest(b'new\n') and facts['indistinguishable'] is False
    applied = host.applied(running, call, {'applied': 'notes.txt', 'hash': digest(b'new\n')})
    assert host.check(applied, 'occurred', 'after_present')['action_status'] == 'applied'
    (host.project / 'notes.txt').write_text('someone else\n')
    host.check(applied, 'diverged', 'neither_present')
    (host.project / 'notes.txt').unlink()
    assert host.check(applied, 'diverged', 'neither_present')['current_hash'] is None
    # Creating a file: missing means not_occurred; parents may not exist yet.
    created = host.draft('src/new.py', 'NEW = 1\n')
    assert created['baseline'] is None
    action, _ = host.action('edit', created, 'Write')
    host.check(action, 'not_occurred', 'absent_on_create')
    Workspace.apply(created)
    host.check(action, 'occurred', 'after_present')
    # A no-op edit's after-state is present either way, and says so.
    (host.project / 'same.txt').write_text('same\n')
    action, _ = host.action('edit', host.draft('same.txt', 'same\n'), 'Write')
    assert host.check(action, 'occurred', 'after_present')['indistinguishable'] is True


def test_edit_root_identity_paths_and_inconsistent_receipts_give_unknown(host):
    (host.project / 'a.txt').write_text('a\n')
    draft = host.draft('a.txt', 'b\n')
    for change in ({'baseline': 'f' * 64}, {'before': 'not the baseline\n'}):
        action, _ = host.action('edit', {**draft, **change}, 'Write')
        host.check(action, 'unknown', 'receipt_inconsistent')
    for change in ({'root_identity': 'x'}, {'after': None}, {'baseline': 'short'}, {'path': ''},
                   {'result': {'hash': 'nope'}}):
        action, _ = host.action('edit', {**draft, **change}, 'Write')
        host.check(action, 'unknown', 'malformed')
    action, _ = host.action('edit', {key: value for key, value in draft.items() if key != 'baseline'}, 'Write')
    host.check(action, 'unknown', 'malformed')
    private, _ = host.action('edit', {**draft, 'path': '.env'}, 'Write')
    assert 'Private credentials' in host.check(private, 'unknown', 'unreadable')['reason']
    os.symlink('a.txt', host.project / 'link.txt')
    linked, _ = host.action('edit', {**draft, 'path': 'link.txt'}, 'Write')
    assert 'Symbolic links' in host.check(linked, 'unknown', 'unreadable')['reason']
    genuine, _ = host.action('edit', draft, 'Write')
    host.check(genuine, 'not_occurred', 'before_present')
    moved = host.tmp / 'moved'
    os.rename(host.project, moved)
    shutil.copytree(moved, host.project, symlinks=True)  # same path and bytes, a different folder
    host.check(genuine, 'unknown', 'root_replaced')
    shutil.rmtree(host.project)
    host.check(genuine, 'unknown', 'root_unavailable')


def test_forged_or_unowned_actions_never_get_a_settling_verdict(host):
    (host.project / 'a.txt').write_text('a\n')
    draft = host.draft('a.txt', 'b\n')
    Workspace.apply(draft)  # the file even holds the after-state
    for forged in (None, 'x', {}, {'id': 'nope'}, {'id': uuid.uuid4().hex, 'chat_id': host.cid}):
        host.check(forged, 'unknown', 'not_found')
    genuine, call = host.action('edit', draft, 'Write')
    host.check(genuine, 'occurred', 'after_present')
    for change in ({'chat_id': None}, {'chat_id': 'f' * 32}, {'kind': 'command'},
                   {'payload': {**genuine['payload'], 'after': 'forged\n'}}):
        host.check({**genuine, **change}, 'unknown', 'forged')
    host.check({key: value for key, value in genuine.items() if key != 'chat_id'}, 'unknown', 'forged')
    host.check({'id': genuine['id'], 'chat_id': host.cid}, 'occurred', 'after_present')
    # A broker record no run tool receipt owns.
    aid = host.store.action(host.cid, 'edit', draft)
    host.store.query("UPDATE actions SET status='running' WHERE id=?", (aid,))
    host.check(host.get(aid), 'unknown', 'no_ownership')
    # Owned under another kind, or by a run of another conversation.
    mismatched, _ = host.action('edit', draft, 'Write', owner_kind='command')
    host.check(mismatched, 'unknown', 'ownership_mismatch')
    other = host.store.new_chat(str(host.ws.root))
    foreign, _ = host.action('edit', draft, 'Write', cid=other)
    host.check(foreign, 'unknown', 'ownership_mismatch')
    # The owning tool receipt now names a different action.
    host.store.query('UPDATE run_tools SET result=? WHERE run_id=? AND call_id=?',
                     (json.dumps({'action_id': 'f' * 32}), host.rid, call))
    host.check(genuine, 'unknown', 'ownership_mismatch')


def test_plugin_and_unsupported_kinds_are_unknown(host):
    plugin, _ = host.action('plugin', {'name': 'Fixture', 'tool': 'post'}, 'PluginCall')
    assert 'connector readback' in host.check(plugin, 'unknown', 'remote_effect')['reason']
    opened, _ = host.action('app', {'id': 'com.example.app'}, 'OpenApp')
    host.check(opened, 'unknown', 'unsupported_kind')


def test_unexpected_readback_failures_fail_closed_as_unknown(host, monkeypatch):
    (host.project / 'a.txt').write_text('a\n')
    action, _ = host.action('edit', host.draft('a.txt', 'b\n'), 'Write')

    def failing(error):
        def read(payload, base):
            raise error
        return read

    monkeypatch.setattr(reconcile, '_edit', failing(RuntimeError('unexpected')))
    assert host.check(action, 'unknown', 'readback_failed')['reason'].startswith('RuntimeError')
    monkeypatch.setattr(reconcile, '_edit', failing(sqlite3.OperationalError('database is locked')))
    assert 'locked' in host.check(action, 'unknown', 'unreadable')['reason']


# Commands and tests


def command(host, asynchronous=True):
    return {'root': str(host.ws.root), 'root_identity': list(host.ws.identity), 'command': 'ls part1',
            'argv': ['ls', 'part1'], 'network': False, 'risk': 'execution', 'requires_review': True,
            'writable': False, 'asynchronous': asynchronous, 'timeout': 300, 'permission_mode': 'ask'}


def job(host, action, call_id, state, uncertain=0, exit_code=None, run_id=None):
    with host.store.lock, host.store.db as db:
        db.executescript(PROCESS_DDL)
    process_id, now = uuid.uuid4().hex, time.time()
    row = {'id': process_id, 'action_id': action['id'], 'chat_id': action['chat_id'], 'run_id': run_id or host.rid,
           'call_id': call_id, 'root': str(host.ws.root), 'root_identity': json.dumps(list(host.ws.identity)),
           'payload_hash': 'a' * 64, 'command': 'ls part1', 'network': 0, 'writable': 0, 'owner': 'b' * 32,
           'state': state, 'exit_code': exit_code, 'isolation': None, 'outcome_uncertain': uncertain, 'pid': None,
           'stop_requested': 0, 'output_bytes': 0, 'output_dropped': 0, 'timeout': 300.0, 'reason': None,
           'created': now, 'started': None, 'finished': None, 'updated': now}
    host.store.query(f'INSERT INTO process_jobs({",".join(row)}) VALUES({",".join("?" * len(row))})', tuple(row.values()))
    return process_id


def test_command_admission_and_recorded_exit_verdicts(host):
    action, _ = host.action('command', command(host), 'StartCommand')
    host.check(action, 'not_occurred', 'no_admission_record')  # no process table yet
    with host.store.lock, host.store.db as db:
        db.executescript(PROCESS_DDL)
    host.check(action, 'not_occurred', 'no_admission_record')
    host.store.query("UPDATE actions SET status='applied' WHERE id=?", (action['id'],))
    host.check(action, 'unknown', 'record_missing')
    for state, uncertain, verdict, basis in (('refused', 0, 'not_occurred', 'refused_before_start'),
                                             ('admitted', 0, 'unknown', 'still_running'),
                                             ('running', 0, 'unknown', 'still_running'),
                                             ('failed', 0, 'occurred', 'exit_observed'),
                                             ('timed_out', 1, 'unknown', 'no_supervisor_record')):
        action, call = host.action('command', command(host), 'StartCommand')
        process_id = job(host, action, call, state, uncertain, exit_code=2 if state == 'failed' else None)
        facts = host.check(action, verdict, basis)
        assert facts['process_id'] == process_id and facts['process_state'] == state
        if state == 'failed':
            assert facts['exit_code'] == 2
    action, call = host.action('command', command(host), 'StartCommand')
    job(host, action, call, 'succeeded', run_id='c' * 32)
    host.check(action, 'unknown', 'binding_mismatch')
    sync, call = host.action('command', command(host, asynchronous=False), 'RunCommand')
    host.check(sync, 'unknown', 'no_supervisor_record')
    sync = host.applied(sync, call, {'exit_code': 0, 'output': 'secret output'})
    facts = host.check(sync, 'occurred', 'exit_receipt')
    assert facts['exit_code'] == 0 and 'secret output' not in json.dumps(facts)
    tests, _ = host.action('tests', {'root': str(host.ws.root), 'recipe': 'pytest'}, 'RunTests')
    host.check(tests, 'unknown', 'no_supervisor_record')


def test_uncertain_jobs_settle_only_from_a_typed_supervisor_record(host):
    action, call = host.action('command', command(host), 'StartCommand', status='applied')
    process_id = job(host, action, call, 'unknown', 1)
    calls = []

    def supervisor(record):
        def read(pid):
            calls.append(pid)
            if isinstance(record, Exception):
                raise record
            return record
        return read

    cases = ((None, 'unknown', 'no_supervisor_record'),
             ({'spawning': False}, 'not_occurred', 'no_spawn_record'),
             ({'spawning': True, 'spawned': True, 'exited': False, 'contained': True}, 'unknown', 'exit_unobserved'),
             ({'spawning': True, 'spawned': True, 'exited': True, 'contained': False}, 'unknown', 'containment_unproven'),
             ({'spawning': True, 'spawned': True, 'exited': True}, 'unknown', 'containment_unproven'),
             ({'spawning': True, 'spawned': True, 'exited': True, 'contained': True, 'exit_code': 0},
              'occurred', 'supervised_exit'),
             ({'exited': 'yes', 'contained': True}, 'unknown', 'supervisor_error'),
             ('exited', 'unknown', 'supervisor_error'),
             (OSError('socket gone'), 'unknown', 'supervisor_error'))
    for record, verdict, basis in cases:
        facts = host.check(action, verdict, basis, job_evidence=supervisor(record))
        if verdict == 'occurred':
            assert facts['exit_code'] == 0 and facts['partial_possible'] is True  # killed jobs may be partial
    assert calls == [process_id] * len(cases)
    host.check(action, 'unknown', 'no_supervisor_record')  # no callback at all


# Installs


def environment(host, state='ready', recorded=True, **row_changes):
    envs = host.app.environments = Environments(host.app)
    envs.anchor.mkdir(mode=0o700, exist_ok=True)
    os.chmod(envs.anchor, 0o700)
    ident = uuid.uuid4().hex
    path = envs.anchor / ident
    (path / 'work' / 'node_modules' / 'left-pad').mkdir(parents=True, mode=0o700)
    (path / 'work' / 'node_modules' / 'left-pad' / 'index.js').write_text('module.exports = 1;\n')
    info = os.lstat(path)
    evidence = {'dir_identity': [info.st_dev, info.st_ino], 'phase': 'fixture'}
    if recorded:
        evidence['tree_digest'] = tree(path)['digest']
    payload = {'operation': 'install_environment', 'kind': 'npm-ci', 'environment_id': ident, 'chat_id': host.cid,
               'root': str(host.ws.root), 'root_identity': list(host.ws.identity), 'manifest_digest': 'a' * 64,
               'lock_digest': 'b' * 64, 'toolchain': {}, 'registries': [], 'packages': 1, 'size_limit': 1_000_000,
               'timeout': 600, 'requires_review': True, 'network': 'allowlisted_registry_only', 'effects': [],
               'summary': 'Fixture install.', 'permission_mode': 'ask'}
    row = {'id': ident, 'chat_id': host.cid, 'root': payload['root'], 'root_identity': json.dumps(payload['root_identity']),
           'kind': 'npm-ci', 'manifest_digest': 'a' * 64, 'lock_digest': 'b' * 64, 'toolchain': '{}',
           'registries': '[]', 'state': state, 'size_bytes': None, 'file_count': None,
           'evidence': json.dumps(evidence), 'created': time.time(), 'updated': time.time(), **row_changes}
    host.store.query(f'INSERT INTO project_environments({",".join(row)}) VALUES({",".join("?" * len(row))})',
                     tuple(row.values()))
    action, _ = host.action('install', payload, 'PrepareEnvironment')
    return action, path


def test_install_verdicts_recheck_the_environment_row_and_tree(host):
    action, path = environment(host)
    facts = host.check(action, 'occurred', 'after_present')
    assert facts['current_tree'] == facts['recorded_tree'] and facts['environment_state'] == 'ready'
    (path / 'work' / 'node_modules' / 'left-pad' / 'index.js').write_text('module.exports = 2;\n')
    host.check(action, 'diverged', 'neither_present')
    shutil.rmtree(path)
    host.check(action, 'not_occurred', 'folder_absent')
    path.mkdir(mode=0o700)  # the same name, but not the folder the installer created
    host.check(action, 'unknown', 'folder_unverified')
    action, path = environment(host, state='uncertain', recorded=False)
    assert host.check(action, 'unknown', 'no_recorded_tree')['folder_present'] is True
    os.chmod(path.parent, 0o755)
    host.check(action, 'unknown', 'storage_unverified')
    os.chmod(path.parent, 0o700)
    action, _ = environment(host, state='installing')
    host.check(action, 'unknown', 'still_running')
    action, _ = environment(host, lock_digest='c' * 64)
    host.check(action, 'unknown', 'binding_mismatch')
    action, path = environment(host, state='failed')
    shutil.rmtree(path)
    assert host.check(action, 'not_occurred', 'folder_absent')['environment_state'] == 'failed'
    del host.app.environments
    action, _ = environment(host)
    del host.app.environments
    host.check(action, 'unknown', 'no_service')


def test_install_without_its_registration_did_not_run(host):
    Environments(host.app)  # the table exists, the row does not
    payload = {'operation': 'install_environment', 'environment_id': uuid.uuid4().hex, 'kind': 'npm-ci'}
    action, _ = host.action('install', payload, 'PrepareEnvironment')
    host.check(action, 'not_occurred', 'no_registration')
    host.store.query("UPDATE actions SET status='applied' WHERE id=?", (action['id'],))
    host.check(action, 'unknown', 'record_missing')
    action, _ = host.action('install', {**payload, 'environment_id': 'not-hex'}, 'PrepareEnvironment')
    host.check(action, 'unknown', 'malformed')


# Git


@macos
def test_git_verdicts_follow_repository_readback(git_host):
    host = git_host
    (host.project / 'app.py').write_text('print("changed")\n')
    plan = host.plans.prepare(host.cid, 'commit', {'paths': ['app.py'], 'message': 'Reconcile fixture'})
    action, call = host.action('git', {**plan, 'permission_mode': 'ask'}, 'ProposeGitChange')
    base = rev(host.project)
    assert host.check(action, 'not_occurred', 'no_receipt')['receipt_id'] == plan['plan_id']
    receipt = host.plans.execute(host.cid, plan, action_id=action['id'], run_id=host.rid)
    assert receipt['state'] == 'applied' and rev(host.project) != base
    committed = rev(host.project)
    facts = host.check(action, 'occurred', 'after_present')
    assert (facts['matches_receipt'], facts['matches_before'], facts['receipt_state']) == (True, False, 'applied')
    applied = host.applied(action, call, receipt)
    host.check(applied, 'occurred', 'after_present')
    (host.project / 'README.md').write_text('# moved by someone else\n')
    git(host.project, 'commit', '-q', '-am', 'Unrelated later commit')
    facts = host.check(applied, 'diverged', 'neither_present')
    assert (facts['matches_receipt'], facts['matches_before']) == (False, False)
    assert rev(host.project, 'HEAD^') == committed  # readback changed nothing
    moved = host.tmp / 'moved'
    os.rename(host.project, moved)
    shutil.copytree(moved, host.project, symlinks=True)
    assert 'replaced' in host.check(applied, 'unknown', 'repository_unverified')['reason']


@macos
def test_git_registration_and_proved_unchanged_receipts_did_not_occur(git_host, monkeypatch):
    from pebble.git_plans import GitPlans
    host = git_host
    (host.project / 'app.py').write_text('print("registered")\n')
    plan = host.plans.prepare(host.cid, 'commit', {'paths': ['app.py'], 'message': 'Registered only'})
    action, _ = host.action('git', {**plan, 'permission_mode': 'ask'}, 'ProposeGitChange')
    host.plans._insert(plan, action['id'], host.rid)  # registered, then the service died before any effect
    host.check(action, 'unknown', 'still_running')
    host.plans = host.app.git_plans = GitPlans(host.app, host.git)  # restart: executing becomes uncertain
    assert host.check(action, 'not_occurred', 'no_effect_started')['receipt_state'] == 'uncertain'
    # A compare-and-swap refusal after the effect began; readback proves nothing changed.
    plan = host.plans.prepare(host.cid, 'commit', {'paths': ['app.py'], 'message': 'CAS refusal'})
    action, _ = host.action('git', {**plan, 'permission_mode': 'ask'}, 'ProposeGitChange')
    base, original = rev(host.project), GitPlans._step

    def refuse(self, ctx, label, args, **kwargs):
        if label == 'update-ref':
            args = [*args[:-1], '1' * 40]
        return original(self, ctx, label, args, **kwargs)

    monkeypatch.setattr(GitPlans, '_step', refuse)
    with pytest.raises(PolicyError, match='update-ref failed'):
        host.plans.execute(host.cid, plan, action_id=action['id'], run_id=host.rid)
    monkeypatch.undo()
    host.store.query("UPDATE actions SET status='failed' WHERE id=?", (action['id'],))
    facts = host.check(host.get(action['id']), 'not_occurred', 'before_present')
    assert (facts['matches_receipt'], facts['matches_before'], facts['receipt_state']) == (False, True, 'failed')
    assert rev(host.project) == base
    # A receipt executed for a different action never settles this one.
    plan = host.plans.prepare(host.cid, 'commit', {'paths': ['app.py'], 'message': 'Other owner'})
    action, _ = host.action('git', {**plan, 'permission_mode': 'ask'}, 'ProposeGitChange')
    host.plans.execute(host.cid, plan, action_id='e' * 32, run_id=host.rid)
    host.check(action, 'unknown', 'binding_mismatch')
    (host.project / 'README.md').write_text('# again\n')
    plan = host.plans.prepare(host.cid, 'commit', {'paths': ['README.md'], 'message': 'Never executed'})
    action, _ = host.action('git', {**plan, 'permission_mode': 'ask'}, 'ProposeGitChange', status='applied')
    host.check(action, 'unknown', 'record_missing')
    action, _ = host.action('git', {**plan, 'plan_id': 'bad'}, 'ProposeGitChange')
    host.check(action, 'unknown', 'malformed')


# Run summaries


def test_reconcile_run_gives_content_free_verdicts_in_receipt_order(host, monkeypatch):
    (host.project / 'a.txt').write_text('private words\n')
    draft = host.draft('a.txt', 'after words\n')
    edit, edit_call = host.action('edit', draft, 'Write')
    Workspace.apply(draft)
    host.runs.end_tool(host.rid, edit_call, {'error': 'receipt lost'}, uncertain=True)
    host.runs.begin_tool(host.rid, '90:call_bare', 'UIRender', {'view': 1})
    started, start_call = host.action('command', command(host), 'StartCommand')
    started = host.applied(started, start_call, {'process_id': 'pending', 'state': 'admitted'})
    process_id = job(host, started, start_call, 'unknown', 1)
    supervisor = {'spawning': True, 'spawned': True, 'exited': True, 'contained': True, 'exit_code': 0}
    before = host.dump()
    items = reconcile_run(host.app, host.rid, job_evidence=lambda pid: supervisor if pid == process_id else None)
    assert host.dump() == before
    assert [(i['call_id'], i['tool'], i['kind'], i['verdict'], i['basis']) for i in items] == [
        (edit_call, 'Write', 'edit', 'occurred', 'after_present'),
        ('90:call_bare', 'UIRender', None, 'unknown', 'no_broker_action'),
        (start_call, 'StartCommand', 'command', 'occurred', 'supervised_exit')]
    assert items[2]['process_id'] == process_id and all(i['authorization'] == 'none' for i in items)
    text = json.dumps(items)
    for secret in ('a.txt', 'private words', 'after words', digest(b'after words\n'), str(host.project)):
        assert secret not in text
    assert [i['basis'] for i in reconcile_run(host.app, host.rid)][2] == 'no_supervisor_record'
    monkeypatch.setattr(reconcile, 'MAX_RUN_ITEMS', 1)
    assert [i['basis'] for i in reconcile_run(host.app, host.rid)] == ['after_present', 'no_broker_action',
                                                                       'readback_limit']
    for bad in ('nope', 'f' * 32, None):
        with pytest.raises(PolicyError, match='Run not found'):
            reconcile_run(host.app, bad)
