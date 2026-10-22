"""Phase 22 merge (i22): the five tracks' parent patches, wired together.

- The shell and the host agree on the new main-window routes (run_activity, Pebble for Chrome, Data & storage); the
  pet reaches none of them and none is a model tool.
- The Chrome bridge listens only in Pebble's own data home and reads no Keychain item at startup; a flat --home
  (tests, validators, evaluation drivers) binds nothing.
- Pebble's own data folders are outside tool access, except the trees Pebble's reviewed features keep there (managed
  worktrees, dependency environments, the person's skill copies), which keep working after the move into ~/.pebble.
- The Documents copies never hold a held or withdrawn draft or a reasoning summary; summaries never reach a file.
- A screenshot follows a batch's tool results, never sits between them; a never-sent root attempt costs zero tokens.
- The candidate stages Pebble for Chrome without its tests; the native host installer follows the moved data home.

Fake HOME, scripted providers and injected runners only: no network, model, Keychain or real ~/.pebble.
"""

import json
import os
import plistlib
import re
import shlex
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import browser_bridge, data_home, tool_authz
from pebble.app import App
from pebble.data_mirror import DataServices
from pebble.policy import PolicyError, Workspace
from pebble.screen_session import ScreenRefused, validate_target
from pebble.terminal import inspect_command
from pebble.tool_catalogue import TOOLS
from test_reasoning_summary_22 import answer, env, no_network, published, settle, tool  # noqa: F401 (fixtures)


@pytest.fixture
def tmp_path(short_tmp_path):
    """This file's bridges need a socket path under 104 bytes, whatever pytest's basetemp (conftest.short_tmp_path)."""
    return short_tmp_path


ROOT = Path(__file__).resolve().parents[2]
MAIN_RS = ROOT / 'src-tauri' / 'src' / 'main.rs'
CHROME_ROUTES = ('browser_extension_status', 'browser_extension_pause', 'browser_extension_resume',
                 'browser_extension_stop', 'browser_extension_unpair')
STORAGE_ROUTES = ('storage_summary', 'storage_reveal', 'storage_delete', 'storage_set', 'storage_settings_resolve',
                  'storage_restore_chat', 'storage_purge_chat', 'storage_refresh')
NOT_PROTECTED = "Pebble's own data folders"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / 'h'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    monkeypatch.setenv('AGENTGUARD_HOME', str(tmp_path / 'guard-unused'))
    data_home.deactivate()
    yield home
    data_home.deactivate()


@pytest.fixture
def no_keychain(monkeypatch):
    """The App's pairing store and every vault call are refused: nothing here may reach the Keychain."""
    from pebble import vault

    def refuse(self, *args, **kwargs):
        raise AssertionError('The merge tests must never reach the OS vault.')
    for name in ('get', 'set', 'delete', '_find'):
        monkeypatch.setattr(vault.Vault, name, refuse)
    reads = []

    class Counted(browser_bridge.MemoryPairing):
        def load(self):
            reads.append('load')
            return super().load()
    monkeypatch.setattr(browser_bridge, 'keychain_pairing', Counted)
    return reads


def rust_list(source, pattern):
    match = re.search(pattern, source, re.S)
    assert match, pattern
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


# 1. One set of main-window routes ------------------------------------------------------------------------------------

def test_the_shell_and_the_host_agree_on_the_new_main_window_routes():
    source = MAIN_RS.read_text()
    allowed = rust_list(source, r'const ALLOWED: &\[&str\] = &\[(.*?)\];')
    pet = rust_list(source, r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains')
    shell_only = rust_list(source, r'const NATIVE_ONLY: &\[&str\] = &\[(.*?)\];')
    new = {'run_activity', *CHROME_ROUTES, *STORAGE_ROUTES}
    assert new <= allowed and not new & pet and not new & shell_only
    assert set(CHROME_ROUTES + STORAGE_ROUTES) <= tool_authz.NATIVE_ONLY
    names = {t['function']['name'].casefold() for t in TOOLS}
    assert not names & {r.casefold() for r in new}, 'no new model tool'
    # The pet never asks for the timeline (it reads run_get and run_events only).
    assert 'run_activity' not in (ROOT / 'src/Pet.vue').read_text()


# 2. The Chrome bridge: only Pebble's own data home, no Keychain at startup --------------------------------------------

def test_a_flat_home_binds_no_bridge_and_reads_no_keychain(tmp_path, fake_home, no_keychain):
    app = App(tmp_path / 'flat', tmp_path / 'resources', lambda *event: None)
    try:
        assert not app.storage.active
        assert not app.browser_bridge.server.listening and not (tmp_path / 'flat' / 'chrome').exists()
        assert no_keychain == [], 'starting reads no pairing record'
        status = app.dispatch('browser_extension_status', {})
        assert no_keychain == ['load'], 'the first status reads it once'
        assert status['bridge'] == {'listening': False, 'error': "Pebble for Chrome connects only to Pebble's own data "
                                                                 'folder (~/.pebble).'}
        with pytest.raises(ValueError, match='managed by a test or a tool'):
            app.dispatch('storage_summary', {})
        # Every Chrome control route answers on a flat home and changes nothing there.
        assert app.dispatch('browser_extension_pause', {}) == {'paused': True, 'sessions': 0}
        assert app.dispatch('browser_extension_resume', {}) == {'paused': False, 'sessions': 0}
        assert app.dispatch('browser_extension_stop', {})['revoked_sessions'] == 0
    finally:
        app.close()
    assert app.browser_bridge.error


def test_b(tmp_path, fake_home, no_keychain):  # short name: the bridge socket path must fit 103 bytes
    layout = data_home.resolve(str(fake_home / '.pebble'), None, env={})
    data_home.ensure_layout(layout)
    report = data_home.migrate(layout)
    data = Path(report['data'])
    data_home.activate(layout, data, report)
    events = []
    app = App(data, tmp_path / 'resources', lambda *event: events.append(event))
    try:
        socket_path = data / 'chrome' / 'bridge.sock'
        assert len(os.fsencode(str(socket_path))) <= browser_bridge.MAX_SOCKET_PATH, 'shorten this test name'
        assert app.storage.active and app.browser_bridge.server.listening and socket_path.exists()
        assert no_keychain == [] and app.browser_bridge.record is None, 'startup read no pairing record'
        summary = app.dispatch('storage_summary', {})
        assert summary['home'] and 'categories' in summary
        # Recently deleted hides a conversation from the list until it is restored (s22 P1).
        cid = app.store.new_chat()
        app.store.message(cid, 'user', 'Plan a trip')
        assert cid in [c['id'] for c in app.state()['chats']]
        app.store.trash_chat(cid, 'documents')
        assert cid not in [c['id'] for c in app.state()['chats']]
        assert app.dispatch('storage_restore_chat', {'chat_id': cid}) == {'restored': cid}
        assert cid in [c['id'] for c in app.state()['chats']]
        assert ('storage', {'changed': 'recently_deleted'}) in events
    finally:
        app.close()
    assert not socket_path.exists(), 'quitting Pebble closes the bridge and removes its socket'
    assert app.storage.thread is None


def test_chrome_targets_validate_and_screenshots_are_only_for_chrome(tmp_path, fake_home, no_keychain):
    target = {'kind': 'browser', 'profile': 'chrome', 'tab_id': '42', 'origins': ['https://example.com']}
    assert validate_target(target) == target
    with pytest.raises(ScreenRefused):
        validate_target({**target, 'profile': 'default'})
    app = App(tmp_path / 'flat', tmp_path / 'resources', lambda *event: None)
    try:
        cid = app.new_chat(None)['id']
        app.active, app.active_run = cid, 'a' * 32
        with pytest.raises(PolicyError, match='only for a Chrome tab'):
            app.dispatch('screen_session_grant', {'id': cid, 'target': {**target, 'profile': 'pebble'},
                                                  'actions': ['look'], 'screenshots': True})
        # A screenshot is extra evidence: without a grant there is none, and asking never raises.
        assert app.browser_screenshot_parts('s' * 32, 'a' * 32) is None
    finally:
        app.active = app.active_run = None
        app.close()


# 3. Pebble's own data folders are outside tool access; its reviewed trees there keep working -------------------------

def folder(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_pebbles_data_folders_refuse_tools_but_its_managed_trees_and_skill_copies_work(tmp_path, fake_home):
    pebble = fake_home / '.pebble'
    legacy = data_home.legacy_default()
    for refused in (pebble, folder(pebble / 'signing'), folder(pebble / 'data'), folder(pebble / 'data/git-worktrees'),
                    folder(legacy)):
        with pytest.raises(PolicyError, match=NOT_PROTECTED):
            Workspace(folder(refused))
    for allowed in (pebble / 'data/git-worktrees/w1', pebble / 'data/environments/e1', pebble / 'skills/demo',
                    legacy / 'git-worktrees/w2'):
        assert Workspace(folder(allowed)).root == folder(allowed).resolve()
    # A managed worktree of an activated home elsewhere (PEBBLE_HOME-style) works the same way.
    other = data_home.resolve(str(tmp_path / 'elsewhere'), str(tmp_path / 'old'), env={})
    data_home.activate(other, other.data)
    assert Workspace(folder(other.data / 'git-worktrees' / 'w3')).root.name == 'w3'
    with pytest.raises(PolicyError, match=NOT_PROTECTED):
        Workspace(folder(other.root / 'logs'))
    # A project that holds one of them refuses paths into it, and nothing else.
    support = folder(fake_home / 'Library' / 'Application Support')
    (legacy / 'pebble.sqlite').write_text('db')
    folder(support / 'Other').joinpath('notes.txt').write_text('ok')
    project = Workspace(support)
    with pytest.raises(PolicyError, match=NOT_PROTECTED):
        project.path(legacy.name + '/pebble.sqlite')
    assert project.read('Other/notes.txt') == 'ok'
    # The skill import reads ~/.pebble/skills/<folder> through a Workspace (s22's documented flow).
    from pebble.skill_bundle import import_bundle
    skill = folder(pebble / 'skills' / 'tidy')
    (skill / 'SKILL.md').write_text('---\nname: tidy\ndescription: Keep notes tidy.\n---\nSort the notes.\n')
    assert import_bundle(skill / 'SKILL.md')['metadata']['name'] == 'tidy'
    # Commands: a path into ~/.pebble or the old folder is refused; the project's own paths are not.
    code = folder(tmp_path / 'code')
    for command in ('cat ~/.pebble/settings.json', 'cat ' + shlex.quote(str(pebble / 'signing' / 'keychain-password')),
                    'ls ' + shlex.quote(str(legacy))):
        with pytest.raises(PolicyError):
            inspect_command(command, code)
    assert inspect_command('ls src', code)['argv'] == ['ls', 'src']
    # The native Save dialog cannot drop an export into ~/.pebble (settings.json, PEBBLE.md live there).
    from pebble.exports import ExportError, Exports
    exports = Exports.__new__(Exports)
    exports.home = folder(tmp_path / 'flat-data')
    with pytest.raises(ExportError, match="outside Pebble's own data"):
        exports._destination(str(pebble / 'report.pdf'), 'pdf', False)
    assert exports._destination(str(code / 'report.pdf'), 'pdf', False) == code / 'report.pdf'


def test_the_terminal_and_sandbox_rules_name_pebbles_folder_where_they_are_deny_by_default():
    for name in ('hooks.py', 'mod_worker.py', 'mcp_transport.py'):
        assert '.pebble' in (ROOT / 'backend/pebble' / name).read_text().split('deny file-read-data file-write*')[-1][:200]
    # Merge judgement: the job sandbox keeps its Phase 14 deny list. Its temp folder is <data>/jobs/<id>-home and
    # managed environments live in <data>/environments, so a .pebble rule there would deny every job after the move.
    from pebble import sandbox_profiles
    assert '.pebble' not in sandbox_profiles.PRIVATE_DENY


# 4. Documents copies and reasoning summaries ---------------------------------------------------------------------------

def files_holding(base, marker):
    found = []
    for directory, _, names in os.walk(base):
        for name in names:
            path = Path(directory) / name
            try:
                if marker.encode() in path.read_bytes():
                    found.append(str(path))
            except OSError:
                continue
    return found


def test_documents_copies_never_hold_a_held_draft_or_a_reasoning_summary(env, tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'h'))
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    held = 'notes.md is staged.\nHELD-DRAFT-MARKER'
    after = 'You declined, so notes.md was not written.\nFINAL: declined'
    env.script = [answer(None, calls=tool('Write', {'file_path': 'notes.md', 'content': '# Notes\n'}, 'write')),
                  answer(held, ['HELD-SUMMARY-MARKER']), answer(after, ['SHOWN-SUMMARY-MARKER'])]
    app.work(chat, 'Write notes.md describing add in calc.py. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    assert app.runs.get(rid)['state'] == 'waiting_approval'
    layout = data_home.Layout(tmp_path / 'pebble-home', True, '--home', None)
    data_home.ensure_layout(layout)
    services = DataServices(app, layout)
    copies = folder(tmp_path / 'Conversations')

    def copy_text():
        services._refresh_conversations(copies)
        [name] = [p for p in copies.iterdir() if p.name != 'README.md']
        return name.read_text()
    text = copy_text()
    assert 'HELD-DRAFT-MARKER' not in text and 'SUMMARY-MARKER' not in text
    # Defence in depth: even a message row holding the held draft is copied as the window routes show it.
    app.store.message(cid, 'assistant', held)
    text = copy_text()
    assert 'HELD-DRAFT-MARKER' not in text and 'A draft the host held or withdrew' in text
    action = next(a for a in app.store.actions(cid) if a['status'] == 'pending')
    app.reject(cid, action['id'])
    settle(app, rid, 'completed')
    assert 'SHOWN-SUMMARY-MARKER' in published(env)  # shown live, in this window only
    text = copy_text()
    assert 'You declined' in text and 'HELD-DRAFT-MARKER' not in text and 'SUMMARY-MARKER' not in text
    services._refresh_memory(folder(tmp_path / 'Memory'))
    services._refresh_logs(folder(tmp_path / 'Logs'))
    # No file anywhere (database, journals, copies, settings.json, logs) holds a summary.
    assert files_holding(tmp_path, 'SUMMARY-MARKER') == []


# 5. The runner: a screenshot follows the batch; a never-sent root attempt costs nothing ------------------------------

def test_a_screenshot_follows_the_batchs_tool_results():
    source = (ROOT / 'backend/pebble/runner.py').read_text()
    loop = source.index('            for call in calls:')
    observed = source.index("observed_screen = args.get('session_id')")
    appended = source.index("'name': 'pebble_screenshot'")
    research = source.index('# Phase 20 D02: after research steps')
    assert loop < observed < appended < research
    # The screenshot block sits at the loop statement's own indentation (after the loop), never inside its body.
    assert "\n            shot_parts = getattr(app, 'browser_screenshot_parts', None)\n" in source
    assert "\n            if observed_screen is not None and callable(shot_parts):\n" in source


def test_a_never_sent_root_attempt_is_recorded_as_zero_tokens():
    from pebble.agent_manager import Agents
    from pebble.recovery import ProviderFailure, StreamStalled
    finished = []

    class Lane:
        @contextmanager
        def admit(self, key, profile, cancel):
            yield

    class Tree:
        def reserve_model(self, *args):
            return {'admitted': True}

        def finish_model(self, *args, failed, usage):
            finished.append((failed, usage))

    model = SimpleNamespace(current=lambda: {'kind': 'chatgpt', 'context': 8192}, last_usage=None)
    agents = SimpleNamespace(
        lane=Lane(), tree=Tree(), estimate=lambda messages, tools: (8192, 0),
        app=SimpleNamespace(model=model, runs=SimpleNamespace(get=lambda rid, cid: {'state': 'running'})),
        store=SimpleNamespace(lock=threading.Lock(), db=SimpleNamespace(
            execute=lambda sql, args: SimpleNamespace(fetchone=lambda: (1,)))))
    attempt = lambda: Agents.root_attempt(agents, 'c' * 32, 'r' * 32, [], [], threading.Event())  # noqa: E731
    with pytest.raises(StreamStalled):
        with attempt():
            raise StreamStalled('connect', 2.0, unsent=True)
    with pytest.raises(ProviderFailure):
        with attempt():
            raise ProviderFailure('connection lost', retryable=True)
    with attempt():
        model.last_usage = {'input_tokens': 10, 'output_tokens': 2, 'cached_tokens': 0}
    assert finished == [(True, {'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0}), (True, None),
                        (False, {'input_tokens': 10, 'output_tokens': 2, 'cached_tokens': 0})]


# 6. Packaging, the vault helper and the native host ----------------------------------------------------------------------

def test_only_the_release_apps_frozen_backend_uses_the_vault_helper(tmp_path):
    from pebble import vault
    assert vault.OWNER_IDENTIFIER == 'com.pebble.local'
    assert vault.installed_owner() is False, 'a source run (tests, validators, the evaluation driver) is never the owner'
    assert vault.Vault(keychain=SimpleNamespace(lib=None)).helper_store() is None
    for identifier in ('com.pebble.local', 'com.pebble.candidate'):
        contents = folder(tmp_path / identifier / 'Pebble.app' / 'Contents')
        (contents / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': identifier}))
        exe = folder(contents / 'Resources/resources/backend') / 'pebble-backend'
        exe.write_bytes(b'')
        assert (vault.bundle_identifier(exe) == vault.OWNER_IDENTIFIER) is (identifier == 'com.pebble.local')
        assert vault.helper_path(exe) == contents / 'Resources/resources/helpers/pebble-vault'

def test_the_candidate_stages_pebble_for_chrome_without_its_tests(tmp_path):
    import test_build_candidate_app as tb
    checkout = tb.make_checkout(tmp_path)
    extension = checkout.root / 'browser-extension'
    tb.put(extension / 'manifest.json', '{"manifest_version": 3}\n')
    tb.put(extension / 'lib' / 'worker.mjs', 'export {};\n')
    tb.put(extension / 'worker.test.mjs', 'import "node:test";\n')
    sources = tb.snap(checkout)
    assert {'browser-extension/manifest.json', 'browser-extension/lib/worker.mjs'} <= set(sources)
    plan = tb.build(checkout).plan()
    assert plan['browser_extension'] and not plan['problems']
    stage_writes = next(step['writes'] for step in plan['steps'] if step['step'] == 'stage')
    staged = str(checkout.out / 'stage/resources/browser-extension')
    assert staged + '/manifest.json' in stage_writes and staged + '/worker.test.mjs' not in stage_writes
    result = tb.build(checkout, tb.FakeRunner(checkout)).execute()
    assert result['result'] == 'built'
    resources = checkout.out / 'stage/resources/browser-extension'
    assert sorted(str(p.relative_to(resources)) for p in resources.rglob('*')) == ['lib', 'lib/worker.mjs', 'manifest.json']
    stage = json.loads((checkout.out / 'stage/stage-manifest.json').read_text())
    assert stage['sources']['browser-extension/manifest.json'] == 'browser-extension/manifest.json'


def test_the_native_host_installer_follows_the_moved_data_home(tmp_path):
    import test_install_native_host_22 as th
    root = tmp_path / 'root'
    old = folder(root / 'home' / th.tool.DATA_HOME)
    backend = root / th.tool.BACKEND
    backend.parent.mkdir(parents=True)
    backend.write_text('#!/bin/sh\nexit 0\n')
    os.chmod(backend, 0o755)
    folder(root / 'home' / 'Library/Application Support/Google/Chrome')
    code, report = th.run('plan', '--root', str(root))
    assert code == th.tool.EXIT_OK and report['layout']['data_home'] == str(old), report['error']
    moved = folder(root / 'home' / '.pebble' / 'data')
    (moved / 'pebble.sqlite').write_text('db')
    code, report = th.run('install', '--root', str(root), '--execute', '--stamp', th.STAMP)
    assert code == th.tool.EXIT_OK, report['error']
    launcher = moved / 'chrome' / 'pebble-native-host'
    assert report['layout']['data_home'] == str(moved) and f'--home {moved}' in launcher.read_text()
    assert json.loads(th.manifest_path(root).read_text())['path'] == str(launcher)
    code, report = th.run('plan', '--root', str(root), '--home', '/home/' + th.tool.DATA_HOME)  # re-based under --root
    assert report['layout']['data_home'] == str(old), '--home still wins'
