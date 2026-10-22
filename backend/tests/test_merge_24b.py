"""Phase 24 wave-B merge (m24b): the seams t24-data, t24-agent and t24-ux-settings reach only through the parent.

Each track tested its own module; these check what the merge wired and decided:
- the five Data & storage decision routes are dispatched, allowed by the shell for the main window only (never the pet,
  never shell-only), listed in tool_authz.NATIVE_ONLY and never a model tool;
- the destructive four are refused by the host without their confirmation (confirm: true; the forget resolve takes the
  exact question id), Delete all and Delete all now keep a conversation whose task is running, and Remove old sign-in
  copies removes only the kept per-item copies, never the current vault item, on a fake Keychain that holds both;
- a cloud request after an on-device exchange carries no on-device action result, and the runner's evidence line is why
  (a control without the withheld ids leaks the canary); the run activity keeps the notice after a reload;
- a project at ~/Documents cannot reach the Documents copies through a command (the job sandbox denies them), while a
  protected folder that holds the job's own temporary folder is left to that grant;
- AgentGuard's last walk of a project is the runner's listing; an Edit refusal says missing or several; a runtime
  command's transcript carries the command id prefix.
Scripted fakes only: no network, model, real Keychain, helper process or browser.
"""

import base64
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import data_home, data_mirror, tool_authz
from pebble import vault as vault_module
from pebble.app import App
from pebble.data_home import resolve
from pebble.data_mirror import DataServices
from pebble.policy import PolicyError, Workspace
from pebble.store import LISTED_CHATS
from pebble.tool_catalogue import TOOLS
from pebble.vault import GET_ALL, LEGACY_SERVICE, PING, PUT_PREFIX, PUT_SUFFIX, VAULT_ACCOUNT, VAULT_SERVICE, Vault

ROOT = Path(__file__).resolve().parents[2]
MAIN_RS = ROOT / 'src-tauri' / 'src' / 'main.rs'
DECISIONS = ('storage_trash_chat', 'storage_trash_all', 'storage_purge_all', 'storage_forget_resolve',
             'storage_cleanup_legacy')
DESTRUCTIVE = ('storage_trash_all', 'storage_purge_all', 'storage_forget_resolve', 'storage_cleanup_legacy')
CONNECTORS = b'com.pebble.local.connectors'


def rust_list(source, pattern):
    match = re.search(pattern, source, re.S)
    assert match, pattern
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    monkeypatch.setenv('AGENTGUARD_HOME', str(tmp_path / 'guard-unused'))
    data_home.deactivate()
    yield home
    data_home.deactivate()


@pytest.fixture
def env(fake_home):
    """An App on a structured ~/.pebble home, so Data & storage is available; its storage service is driven by hand."""
    layout = resolve(str(fake_home / '.pebble'), None, env={})
    data_home.ensure_layout(layout)
    report = data_home.migrate(layout)
    data_home.activate(layout, Path(report['data']), report)
    events = []
    original = DataServices.start
    DataServices.start = lambda self: None
    try:
        app = App(Path(report['data']), fake_home / 'resources', lambda *event: events.append(event))
    finally:
        DataServices.start = original
    app.storage.opener = lambda path: None
    yield SimpleNamespace(app=app, services=app.storage, events=events, home=fake_home)
    app.active = app.active_run = None
    app.storage.stop()
    app.close()


def chat(app, tag):
    cid = app.store.new_chat()
    app.store.message(cid, 'user', f'Plan {tag}')
    app.store.message(cid, 'assistant', f'Here is the {tag} plan.')
    return cid


def listed(app):
    return {r['id'] for r in app.store.query(LISTED_CHATS)}


# ---- 2b: the four destructive routes ---------------------------------------------------------------------------------

def test_the_decision_routes_are_main_window_routes_only(env):
    routes = env.app._route_table()
    assert set(DECISIONS) <= set(routes)
    source = MAIN_RS.read_text(encoding='utf-8')
    allowed = rust_list(source, r'const ALLOWED: &\[&str\] = &\[(.*?)\];')
    pet = rust_list(source, r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains')
    shell_only = rust_list(source, r'const NATIVE_ONLY: &\[&str\] = &\[(.*?)\];')
    assert set(DECISIONS) <= allowed, 'the main window reaches them'
    assert not set(DECISIONS) & pet, 'never the pet'
    assert not set(DECISIONS) & shell_only, 'not shell-only routes either (validate_phase19/20 pin that list)'
    assert set(DECISIONS) <= tool_authz.NATIVE_ONLY
    assert not {t['function']['name'].casefold() for t in TOOLS} & {r.casefold() for r in DECISIONS}, 'no model tool'
    # The shell's two refusals use the plain sentence (t24-ux-settings 4.2), and the window treats it as "unsupported".
    rpc = source[source.index('async fn rpc('):source.index('fn compact(')]
    assert rpc.count('"Pebble can\'t do that from this window."') == 2 and 'Invalid local request.' not in rpc
    assert "can't do that from this window" in (ROOT / 'src' / 'activity.ts').read_text(encoding='utf-8')


def test_the_host_refuses_each_destructive_route_without_its_confirmation(env):
    alpha, beta = chat(env.app, 'alpha'), chat(env.app, 'beta')
    env.app.dispatch('storage_trash_chat', {'chat_id': beta})
    calls = []
    env.services.vault = lambda: calls.append('vault') or pytest.fail('the vault was reached without a confirmation')
    for method in ('storage_trash_all', 'storage_purge_all', 'storage_cleanup_legacy'):
        for params in ({}, {'confirm': False}, {'confirm': 'true'}, {'confirm': 1}, {'confirm': None}):
            with pytest.raises(ValueError, match='Confirm'):
                env.app.dispatch(method, params)
    assert listed(env.app) == {alpha} and set(env.app.store.trashed()) == {beta} and calls == []
    assert env.app.store.query('SELECT 1 FROM messages WHERE chat_id=?', (beta,)), 'nothing was deleted'
    # The forget resolve's confirmation is the exact id of an open question.
    learned = env.app.memory.put('semantic', 'The ferry leaves at noon.', 'global', None, origin='user')
    token = '0123456789abcdef'
    env.services.manifest['confirm'][token] = {'kind': 'semantic', 'ids': [learned], 'since': time.time(),
                                               'whole': True}
    for params in ({'id': 'fedcba9876543210', 'accept': True}, {'id': token.upper(), 'accept': True},
                   {'id': token[:-1], 'accept': True}, {'id': token, 'accept': 'yes'}, {'id': [token], 'accept': True}):
        with pytest.raises(ValueError):
            env.app.dispatch('storage_forget_resolve', params)
    assert env.app.store.query('SELECT 1 FROM memories WHERE id=?', (learned,)), 'nothing was forgotten'
    env.app.dispatch('storage_forget_resolve', {'id': token, 'accept': True})
    assert not env.app.store.query('SELECT 1 FROM memories WHERE id=?', (learned,))
    with pytest.raises(ValueError, match='no longer open'):
        env.app.dispatch('storage_forget_resolve', {'id': token, 'accept': True})


def test_delete_all_and_delete_all_now_keep_a_conversation_whose_task_is_running(env):
    running, idle, other = chat(env.app, 'running'), chat(env.app, 'idle'), chat(env.app, 'other')
    env.app.active = running
    moved = env.app.dispatch('storage_trash_all', {'confirm': True})
    assert moved['moved'] == 2 and [k['chat_id'] for k in moved['kept']] == [running]
    assert listed(env.app) == {running} and set(env.app.store.trashed()) == {idle, other}
    # A task running in a conversation already in Recently deleted (a scheduled run starts through the controller,
    # not send): Delete all now keeps it, with every row, and deletes it once the task has stopped.
    env.app.active = idle
    result = env.app.dispatch('storage_purge_all', {'confirm': True})
    assert result['deleted'] == 0 and {k['chat_id'] for k in result['kept']} == {idle, other}
    assert all('Stop the current task' in k['reason'] for k in result['kept'])
    assert env.app.store.query('SELECT 1 FROM messages WHERE chat_id=?', (idle,)) and idle in env.app.store.trashed()
    env.app.active = None
    assert env.app.dispatch('storage_purge_all', {'confirm': True}) == {'deleted': 2, 'kept': []}
    assert not env.app.store.query('SELECT 1 FROM chats WHERE id IN (?,?)', (idle, other))
    assert listed(env.app) == {running}


class RecordingKeychain:
    """The login Keychain's generic passwords by (service, account): every call recorded, never the Security
    framework. It holds both the kept per-item copies and the current vault item."""

    def __init__(self):
        self.items, self.calls = {}, []

    def read(self, service, account):
        self.calls.append(('read', service, account))
        return self.items.get((service, account))

    def exists(self, service, account):
        self.calls.append(('exists', service, account))
        return (service, account) in self.items

    def write(self, service, account, raw):
        self.calls.append(('write', service, account))
        self.items[(service, account)] = raw

    def remove(self, service, account):
        self.calls.append(('remove', service, account))
        return self.items.pop((service, account), None) is not None


class HelperProcess:
    """pebble_vault.c as vault.py sees it: its one item is the keychain's (VAULT_SERVICE, VAULT_ACCOUNT) entry."""

    def __init__(self, keychain):
        self.keychain = keychain

    def __call__(self, path, request, timeout):
        body = request[:-1]
        if body == PING:
            return 0, b'{"ok":true,"version":1}\n'
        item = self.keychain.items.get((VAULT_SERVICE, VAULT_ACCOUNT))
        if body == GET_ALL:
            if item is None:
                return 0, b'{"ok":true,"found":false}\n'
            return 0, b'{"ok":true,"found":true,"map_b64":"' + base64.b64encode(item) + b'"}\n'
        if body.startswith(PUT_PREFIX) and body.endswith(PUT_SUFFIX):
            self.keychain.items[(VAULT_SERVICE, VAULT_ACCOUNT)] = base64.b64decode(body[len(PUT_PREFIX):-len(PUT_SUFFIX)])
            return 0, b'{"ok":true}\n'
        return 2, b'{"ok":false,"error":"bad_request"}\n'


def test_remove_old_sign_in_copies_never_touches_the_current_vault_item(env, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(vault_module, 'run_helper', lambda *a, **k: pytest.fail('the real helper was started'))
    keychain = RecordingKeychain()
    secret, token = 'sk-live-0123456789abcdef-SECRET', 'oauth-refresh-0123456789'
    # The kept per-item copies a migration leaves (t24-auth: kept for a rollback before the vault), and the vault item.
    for service, account, value in ((LEGACY_SERVICE, b'profile-1', secret), (LEGACY_SERVICE, b'oauth:p1:g1:0', token),
                                    (CONNECTORS, b'github', 'ghp_' + 'x' * 36)):
        keychain.items[(service, account)] = value.encode()
    current = {'format': vault_module.FORMAT, 'services': {
        LEGACY_SERVICE.decode(): {'secrets': {'profile-1': secret, 'oauth:p1:g1:0': token},
                                  'migrated': {'profile-1': 'a' * 64, 'oauth:p1:g1:0': 'b' * 64}},
        CONNECTORS.decode(): {'secrets': {'github': 'ghp_' + 'x' * 36}, 'migrated': {'github': 'c' * 64}},
        # A map that lists the vault's own service is never a reason to delete the vault item.
        VAULT_SERVICE.decode(): {'secrets': {}, 'migrated': {VAULT_ACCOUNT.decode(): 'superseded'}}}}
    keychain.items[(VAULT_SERVICE, VAULT_ACCOUNT)] = json.dumps(current, sort_keys=True, separators=(',', ':')).encode()
    store = vault_module.HelperStore(vault_module.VaultHelper(Path('/fake/pebble-vault'), run=HelperProcess(keychain)))
    env.services.vault = lambda: Vault(keychain=keychain, owner=True, helper=store)

    with pytest.raises(ValueError, match='Confirm'):
        env.app.dispatch('storage_cleanup_legacy', {})
    assert keychain.calls == [], 'no Keychain call before the confirmation'
    result = env.app.dispatch('storage_cleanup_legacy', {'confirm': True})
    assert (result['removed'], result['absent'], result['failed']) == (3, 0, 0)
    assert VAULT_SERVICE.decode() not in result['services']
    assert set(keychain.items) == {(VAULT_SERVICE, VAULT_ACCOUNT)}, 'only the kept copies went'
    assert not [c for c in keychain.calls if c[0] == 'remove' and c[1] == VAULT_SERVICE], 'the vault item was never touched'
    assert not [c for c in keychain.calls if c[0] == 'read'], 'no item data was read'
    # Every sign-in and key is still served from the vault item.
    vault = Vault(keychain=keychain, owner=True, helper=store)
    assert vault.get('profile-1') == secret and vault.get('oauth:p1:g1:0') == token
    # Without the vault helper the per-item items ARE the current sign-ins: refused, and nothing is removed.
    keychain.items[(LEGACY_SERVICE, b'profile-1')] = secret.encode()
    before, keychain.calls[:] = dict(keychain.items), []
    env.services.vault = lambda: Vault(keychain=keychain, owner=False)
    with pytest.raises(ValueError, match='not in use'):
        env.app.dispatch('storage_cleanup_legacy', {'confirm': True})
    assert keychain.items == before and not [c for c in keychain.calls if c[0] == 'remove']


# ---- 2: V1-002 on the merged source: the runner's evidence line -------------------------------------------------------

CANARY = {'turn': 'locker-9K2M-turn', 'reply': 'locker-9K2M-reply', 'action': 'locker-9K2M-action'}
DEVICE = {'id': 'device', 'kind': 'openai', 'name': 'On this Mac', 'url': 'http://127.0.0.1:9/v1', 'model': 'local',
          'context': 32768, 'budget': 32768, 'budget_version': 2, 'context_mode': 'full', 'tools': True}
LUNA = {'id': 'luna', 'name': 'Luna', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
        'model': 'gpt-6-luna', 'context': 32768, 'budget': 32768, 'tools': True, 'context_mode': 'custom',
        'budget_version': 2, 'context_version': 1, 'connection_id': 'luna'}
SETTLED = {'completed', 'failed', 'stopped', 'budget_exhausted', 'paused', 'interrupted', 'waiting_input',
           'waiting_approval', 'needs_reconciliation', 'verification_required'}


class Lines:
    def __init__(self, text):
        item = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': text}]}
        shell = {'status': 'in_progress', 'output': []}
        events = [{'type': 'response.created', 'response': shell}, {'type': 'response.in_progress', 'response': shell},
                  {'type': 'response.output_text.delta', 'output_index': 0, 'item_id': 'msg_1', 'delta': text},
                  {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                  {'type': 'response.completed', 'response': {'status': 'completed', 'output': [],
                                                              'usage': {'input_tokens': 100, 'output_tokens': 5}}}]
        self.lines = [b'data: ' + json.dumps(e).encode() + b'\n' for e in events]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def readline(self, limit=-1):
        return self.lines.pop(0) if self.lines else b''


class Http:
    """The cloud route's transport: each request body exactly as it would leave the Mac."""

    def __init__(self):
        self.bodies = []

    def open(self, req, timeout=None):
        self.bodies.append(bytes(req.data))
        return Lines('Done.')


@pytest.fixture
def routed(tmp_path, monkeypatch):
    events = []
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *event: events.append(event))
    app.store.set_setting('model_path', '')
    app.store.set_setting('model_profiles', [dict(DEVICE), dict(LUNA)])
    app.model.auth = SimpleNamespace(access=lambda pid: 'scripted-token')
    app.model.http = Http()
    cloud = app.model.complete

    def complete(messages, tools, cancel, token, effort):
        if app.model.current()['id'] == 'device':  # the on-device model, scripted
            token('Noted ' + CANARY['reply'] + '.')
            return {'role': 'assistant', 'content': 'Noted ' + CANARY['reply'] + '.'}
        return cloud(messages, tools, cancel, token, effort)
    monkeypatch.setattr(app.model, 'complete', complete)
    app.events = events
    yield app
    deadline = time.monotonic() + 10
    while app.active and time.monotonic() < deadline:
        time.sleep(0.02)
    app.close()


def settled_send(app, cid, text, profile):
    app.model_select(profile)
    rid = app.send(cid, text)['run_id']
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if app.runs.get(rid)['state'] in SETTLED and not app.active:
            break
        time.sleep(0.02)
    assert app.runs.get(rid)['state'] == 'completed', app.runs.get(rid)
    return rid


def on_device_exchange(app):
    cid = app.new_chat()['id']
    rid = settled_send(app, cid, 'My locker code is ' + CANARY['turn'] + '.', 'device')
    aid = app.store.action(cid, 'command', {'command': 'cat notes.txt', 'result': {'stdout': CANARY['action']}})
    app.store.query("UPDATE actions SET status='applied' WHERE id=?", (aid,))
    app.store.query('INSERT INTO run_action_owners VALUES(?,?,?,?)', (aid, rid, 'call-1', 'command'))
    return cid, rid


def leaked(body):
    return sorted(name for name, canary in CANARY.items() if canary.encode() in body)


def test_a_cloud_request_carries_no_on_device_action_result_and_the_runner_line_is_why(routed, monkeypatch):
    cid, device_run = on_device_exchange(routed)
    cloud_run = settled_send(routed, cid, 'What did the command print?', 'luna')
    body = routed.model.http.bodies[-1]
    assert b'What did the command print?' in body and leaked(body) == []
    # After a reload the run activity still says so, content-free, with the label in its detail.
    notes = [i for i in routed.dispatch('run_activity', {'run_id': cloud_run})['items'] if i['kind'] == 'note'
             and i['label'] == 'Earlier on-device messages were not shared']
    assert len(notes) == 1 and notes[0]['detail'].startswith('Earlier on-device messages were not shared with Luna')
    assert CANARY['turn'] not in json.dumps(notes)
    # Control: the same send with the withheld action ids not passed (the turns still withheld) carries the result,
    # so the runner's evidence line (t24-data §7.3, applied at merge) is what keeps it out.
    real = data_mirror.route_view
    monkeypatch.setattr(data_mirror, 'route_view', lambda *args: {**real(*args), 'actions': []})
    settled_send(routed, cid, 'And once more?', 'luna')
    assert leaked(routed.model.http.bodies[-1]) == ['action']


# ---- t24-data §7.2: the job sandbox and the Documents copies ---------------------------------------------------------

FW = Path('/Library/Frameworks/Python.framework')
APP_PY = (Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python').resolve()
real_sandbox = pytest.mark.skipif(
    sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file() or not APP_PY.is_file(),
    reason='actual macOS confinement (test_sandbox_profiles uses the same interpreter)')
# Each argument is "read:<path>", "write:<path>" (append, creating the file) or "exec:<path>"; one line per argument.
PROBE = '''
import subprocess, sys
from pathlib import Path
for op, name in (arg.split(':', 1) for arg in sys.argv[1:]):
    try:
        if op == 'read':
            print('READ', name, Path(name).read_text().strip())
        elif op == 'write':
            with open(name, 'a') as handle:
                handle.write('x')
            print('WROTE', name)
        else:
            done = subprocess.run([name, 'ran'], capture_output=True, text=True, timeout=20)
            print('EXEC', name, done.returncode, done.stdout.strip())
    except OSError:
        print('DENIED', op, name)
'''


def folder(path):
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def probe(profile, cwd, temp, *ops):
    """Run PROBE under the profile, as a job runs (HOME and TMPDIR are its temporary folder); its output lines."""
    out = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, str(APP_PY), '-I', '-c', PROBE, *ops], cwd=cwd,
                         env={'PATH': '/usr/bin:/bin', 'HOME': str(temp), 'TMPDIR': str(temp)},
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout.splitlines()


def old_rule(folders, *, files=(), reads=(), writes=()):
    """The first merge rule, kept only as this test's control: a protected folder holding any granted path was not
    denied at all."""
    from pebble.data_home import protected_paths_below
    held = [str(g).casefold() for g in (*files, *reads, *writes)]
    below = sorted({str(p) for f in folders for p in protected_paths_below(f)
                    if not any(g == str(p).casefold() or g.startswith(str(p).casefold().rstrip('/') + '/') for g in held)})
    return '(deny file-read-data file-write* ' + ' '.join(f'(subpath {json.dumps(p)})' for p in below) + ')' if below else ''


@real_sandbox
def test_a_pebble_home_inside_a_project_reopens_only_the_jobs_own_paths(fake_home, monkeypatch):
    from pebble import sandbox_profiles
    project = folder(fake_home / 'code')
    pebble_home = folder(project / 'pebble-home')
    monkeypatch.setenv('PEBBLE_HOME', str(pebble_home))  # a custom data home inside the project (protected_roots)
    data = folder(pebble_home / 'data')
    temp = folder(data / 'jobs' / 'job-1-home')
    environment = folder(data / 'environments' / 'env-1' / 'bin')  # a managed environment the job may run
    # A script, not a copied system binary: macOS kills a copied Apple binary at launch (-9) wherever it runs.
    # /bin/sh reads it, so running it needs the re-allowed read inside the protected folder.
    (environment / 'tool').write_text('#!/bin/sh\necho "$1"\n')
    (environment / 'tool').chmod(0o755)
    (data / 'pebble.db').write_text('DB-CANARY')  # a stand-in for the database beside the job's folder
    (temp / 'notes.txt').write_text('TEMP-OK')
    (temp / '.env').write_text('ENV-CANARY')
    (project / 'main.py').write_text('PROJECT-OK')
    grants = dict(executable=APP_PY, exec_paths=[FW, environment], read_paths=[FW], project_root=project, temp=temp,
                  writable_paths=[project])
    profile = sandbox_profiles.build(job_marker=uuid.uuid4().hex, **grants)
    lines = profile.split('\n')
    q = json.dumps
    deny = f'(deny file-read-data file-write* (subpath {q(str(pebble_home))}))'
    # file-read-data: the operation the deny took away (a later file-read* allow would not override it in SBPL).
    reopened = f'(allow file-read-data (subpath {q(str(environment))}) (subpath {q(str(temp))}))'
    written = f'(allow file-write* (subpath {q(str(temp))}))'
    private = next(i for i, line in enumerate(lines) if '.git' in line and line.startswith('(deny file-read-data'))
    assert lines.index(deny) < lines.index(reopened) < lines.index(written) < private, 'the private deny still wins'
    ops = ('read:main.py', f'read:{temp}/notes.txt', f'write:{temp}/out.txt', f'read:{temp}/.env',
           f'exec:{environment}/tool', f'read:{data}/pebble.db', f'write:{data}/pebble.db', f'write:{data}/planted.txt')
    out = probe(profile, project, temp, *ops)
    assert out == ['READ main.py PROJECT-OK', f'READ {temp}/notes.txt TEMP-OK', f'WROTE {temp}/out.txt',
                   f'DENIED read {temp}/.env', f'EXEC {environment}/tool 0 ran', f'DENIED read {data}/pebble.db',
                   f'DENIED write {data}/pebble.db', f'DENIED write {data}/planted.txt'], out
    assert (data / 'pebble.db').read_text() == 'DB-CANARY' and not (data / 'planted.txt').exists()
    # Control: the deny alone (no re-allow lines) closes the job's own temporary folder and tool too, so the re-allow
    # lines are what reopen exactly those paths.
    exact = sandbox_profiles._protected_rules
    monkeypatch.setattr(sandbox_profiles, '_protected_rules', lambda *a, **k: exact(*a, **k).split('\n')[0])
    alone = probe(sandbox_profiles.build(job_marker=uuid.uuid4().hex, **grants), project, temp,
                  f'read:{temp}/notes.txt', f'exec:{environment}/tool')
    assert alone[0] == f'DENIED read {temp}/notes.txt' and not alone[1].endswith(' 0 ran'), alone
    # Control (fail-before): under the first merge rule the whole data home stayed open, the database included.
    monkeypatch.setattr(sandbox_profiles, '_protected_rules', old_rule)
    before = sandbox_profiles.build(job_marker=uuid.uuid4().hex, **grants)
    assert deny not in before
    assert f'READ {data}/pebble.db DB-CANARY' in probe(before, project, temp, f'read:{data}/pebble.db')


@real_sandbox
def test_a_read_grant_inside_the_documents_copies_reopens_only_that_subpath(fake_home, tmp_path):
    from pebble.sandbox_profiles import build
    documents = folder(fake_home / 'Documents')
    copies = documents / 'Pebble'
    exports, memory = folder(copies / 'Exports'), folder(copies / 'Memory')
    (exports / 'report.csv').write_text('EXPORT-OK')
    (memory / 'Facts & knowledge.md').write_text('COPY-CANARY')
    temp = folder(tmp_path / 'job-temp')
    profile = build(executable=APP_PY, exec_paths=[FW], read_paths=[FW, exports], project_root=documents, temp=temp,
                    writable_paths=[documents], job_marker=uuid.uuid4().hex)
    assert f'(allow file-read-data (subpath {json.dumps(str(exports))}))' in profile.split('\n')
    assert '(allow file-write* (subpath ' + json.dumps(str(exports)) not in profile, 'read was granted, not write'
    out = probe(profile, documents, temp, 'read:Pebble/Exports/report.csv', 'write:Pebble/Exports/new.csv',
                'read:Pebble/Memory/Facts & knowledge.md', 'write:Pebble/Memory/planted.md')
    assert out == ['READ Pebble/Exports/report.csv EXPORT-OK', 'DENIED write Pebble/Exports/new.csv',
                   'DENIED read Pebble/Memory/Facts & knowledge.md', 'DENIED write Pebble/Memory/planted.md'], out
    assert not (exports / 'new.csv').exists() and not (memory / 'planted.md').exists()


def test_a_command_in_a_documents_project_cannot_reach_the_copies(fake_home, tmp_path):
    from pebble.sandbox_profiles import build
    documents = folder(fake_home / 'Documents')
    copies = documents / 'Pebble'
    folder(copies / 'Memory')
    (copies / 'Memory' / 'Facts & knowledge.md').write_text('COPY-CANARY')
    (documents / 'notes.txt').write_text('PROJECT-OK')
    temp = folder(tmp_path / 'job-temp')
    profile = build(executable=APP_PY, exec_paths=[FW], read_paths=[FW], project_root=documents, temp=temp,
                    writable_paths=[documents], job_marker=uuid.uuid4().hex)
    rules = [line for line in profile.split('\n') if 'Pebble' in line]
    assert rules == [f'(deny file-read-data file-write* (subpath {json.dumps(str(copies))}))'], 'nothing re-allowed there'
    # Outside ~/Documents nothing changes.
    plain = folder(tmp_path / 'code')
    assert 'Pebble' not in build(executable=APP_PY, exec_paths=[FW], read_paths=[FW], project_root=plain, temp=temp,
                                 job_marker=uuid.uuid4().hex)
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file() or not APP_PY.is_file():
        return  # the real confinement below needs macOS's sandbox (test_sandbox_profiles uses the same interpreter)
    out = probe(profile, documents, temp, 'read:notes.txt', 'read:Pebble/Memory/Facts & knowledge.md',
                'write:Pebble/planted.md')
    assert out == ['READ notes.txt PROJECT-OK', 'DENIED read Pebble/Memory/Facts & knowledge.md',
                   'DENIED write Pebble/planted.md'], out
    assert not (copies / 'planted.md').exists()


# ---- t24-agent §5.1 and §5.2 ---------------------------------------------------------------------------------------------

@pytest.fixture
def app(tmp_path):
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *event: None)
    try:
        yield value
    finally:
        value.active = value.active_run = None
        value.close()


def test_agentguards_last_walk_is_the_runners_listing(app, tmp_path):
    assert app.guard.guard, app.guard.error
    project = tmp_path / 'project'
    (project / 'src').mkdir(parents=True)
    (project / 'src' / 'main.py').write_text('print(1)\n')
    (project / 'README.md').write_text('# x\n')
    root = Workspace(project).root
    assert app.guard.listing(root) is None, 'nothing synced yet'
    app.guard.begin(root, 'run-1', 'Fix the bug.')
    assert app.guard.listing(root) == {'files': Workspace(project).inventory()['files'], 'truncated': False}
    (project / 'src' / 'new.py').write_text('x = 1\n')
    app.guard.begin(root, 'run-2', 'And the next one.')  # session_start syncs again
    assert 'src/new.py' in app.guard.listing(root)['files']


def test_an_edit_refusal_says_whether_the_text_was_missing_or_found_several_times(app, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'notes.txt').write_text('alpha\nbeta\nbeta\n')
    cid = app.new_chat(str(project))['id']
    ws = Workspace(project)
    with pytest.raises(PolicyError, match=r'^Edit text must match exactly once, but it was not found in notes\.txt\.'):
        app._execute('Edit', {'file_path': 'notes.txt', 'old_string': 'gamma', 'new_string': 'x'}, ws, cid)
    with pytest.raises(PolicyError, match=r'^Edit text must match exactly once, but it matches 2 places in notes\.txt\.'):
        app._execute('Edit', {'file_path': 'notes.txt', 'old_string': 'beta', 'new_string': 'x'}, ws, cid)
    with pytest.raises(PolicyError, match=r'^Edit text must match exactly once\. Read the file again\.$'):
        app._execute('Edit', {'file_path': 'notes.txt', 'old_string': '', 'new_string': 'x'}, ws, cid)


def test_a_runtime_commands_transcript_carries_the_command_id_prefix(app):
    from pebble.runner import COMMAND_ID_PREFIX, command_transcripts
    cid = app.new_chat()['id']
    app.store.message(cid, 'user', 'Plan a trip.')
    app.send(cid, '/help')
    messages = app.chat(cid)['messages']
    commands = [m for m in messages if m['id'].startswith(COMMAND_ID_PREFIX)]
    assert [m['role'] for m in commands] == ['user', 'assistant'] and commands[0]['content'] == '/help'
    assert command_transcripts(messages) == {m['id'] for m in commands}
    routes = {r['message_id']: r['route_class'] for r in app.store.query('SELECT * FROM message_routes')}
    assert {routes[m['id']] for m in commands} == {'device'}, 'test_runtime_commands pins the class'
