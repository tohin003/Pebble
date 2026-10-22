"""Phase 24 wave-A merge (m24a): the seams the eight wave-A tracks reach only through the parent.

Each track tested its own module; these check the wiring the merge added in app.py, main.rs, tool_authz.py and
run_activity.py, and the reconciliations the merge note records:
- every new main-window route is dispatched, allowed by the shell for the main window only (attach_bytes also for the
  pet, like attach), listed in tool_authz.NATIVE_ONLY where it is a native route, and never a model tool;
- the shell carries main.py's content-free error code to bridge.ts and drops it for its own native calls;
- chatgpt_start fetches discovery before task_lock, and not at all while a task runs (it refuses first, as before);
- a failing AgentGuard unstage never fails the person's Reject;
- the run activity says "Waiting for the network" for a network retry and names a sleep stall.
Scripted fakes only: no network, model, Keychain or real browser.
"""

import re
from pathlib import Path

import pytest

from pebble import tool_authz
from pebble.app import App
from pebble.tool_catalogue import TOOLS

ROOT = Path(__file__).resolve().parents[2]
MAIN_RS = ROOT / 'src-tauri' / 'src' / 'main.rs'
MAIN_ONLY = ('memory_forget_preview', 'run_held_tests', 'run_held_finish', 'run_held_accept',
             'browser_connect_status', 'browser_connect_install')
NEW_ROUTES = MAIN_ONLY + ('attach_bytes',)


def rust_list(source, pattern):
    match = re.search(pattern, source, re.S)
    assert match, pattern
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


@pytest.fixture
def app(tmp_path):
    events = []
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *event: events.append(event))
    value.events = events
    try:
        yield value
    finally:
        value.active = value.active_run = None
        value.close()


def test_the_new_routes_are_dispatched_and_the_shell_allows_them_to_the_main_window_only(app):
    routes = app._route_table()
    assert set(NEW_ROUTES) <= set(routes)
    source = MAIN_RS.read_text(encoding='utf-8')
    allowed = rust_list(source, r'const ALLOWED: &\[&str\] = &\[(.*?)\];')
    pet = rust_list(source, r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains')
    shell_only = rust_list(source, r'const NATIVE_ONLY: &\[&str\] = &\[(.*?)\];')
    assert set(NEW_ROUTES) <= allowed and not set(NEW_ROUTES) & shell_only
    assert not set(MAIN_ONLY) & pet, 'never the pet'
    assert {'attach', 'attach_url', 'attach_bytes'} <= pet, 'paste reaches the pet like attach'
    assert set(MAIN_ONLY) <= tool_authz.NATIVE_ONLY and 'attach_bytes' not in tool_authz.NATIVE_ONLY
    assert not {t['function']['name'].casefold() for t in TOOLS} & set(NEW_ROUTES), 'no new model tool'
    inject = next(line for line in source.splitlines() if 'map.insert("surface"' in line)
    assert all(f'method=="{name}"' in inject for name in ('run_held_tests', 'run_held_finish', 'run_held_accept'))
    # Historical anchors other phases' tests edit stay intact (test_validate_phase21, test_validate_phase20).
    assert '"apps","attach","attach_url"' in source and '"open_url","attach",' in source


def test_the_shell_passes_the_error_code_to_the_window_and_drops_it_for_native_calls():
    source = MAIN_RS.read_text(encoding='utf-8')
    assert 'format!("{code}\\u{1f}{error}")' in source and 'value.get("code").and_then(Value::as_str)' in source
    native = source[source.index('fn native_call('):]
    native = native[:native.index('\n}\n')]
    assert re.search(r'\{\s*if !NATIVE_ONLY\.contains\(&method\)', native), 'the native-only guard stays first'
    assert "split_once('\\u{1f}')" in native
    spawn = source[source.index('fn spawn_backend('):]
    assert 'home.join("logs").join("service.log")' in spawn and '"service.log.1"' in spawn
    bridge = (ROOT / 'src' / 'bridge.ts').read_text(encoding='utf-8')
    assert 'class RpcError extends Error' in bridge and '\\u001f' in bridge


def test_chatgpt_start_fetches_discovery_before_task_lock_and_not_while_a_task_runs(app, monkeypatch):
    seen = []
    monkeypatch.setattr(app.chatgpt, 'prefetch', lambda: seen.append(('prefetch', app.task_lock._is_owned())))
    monkeypatch.setattr(app.chatgpt, 'start',
                        lambda profile_id=None: seen.append(('start', app.task_lock._is_owned())) or {'state': 'waiting'})
    assert app.dispatch('chatgpt_start', {}) == {'state': 'waiting'}
    assert seen == [('prefetch', False), ('start', True)]
    seen.clear()
    app.active = 'another-chat'
    with pytest.raises(ValueError, match='Stop the current task before signing in.'):
        app.dispatch('chatgpt_start', {})
    assert seen == [], 'no sign-in network call while a task runs'


def test_a_failing_guard_unstage_never_fails_the_persons_reject(app, monkeypatch, tmp_path):
    cid = app.new_chat()['id']
    project = tmp_path / 'project'
    project.mkdir()
    called = []

    def broken(root, session, path):
        called.append((root, session, path))
        raise OSError('mirror unavailable')
    monkeypatch.setattr(app.guard, 'unstage', broken)
    aid = app.store.action(cid, 'edit', {'root': str(project), 'path': 'notes.txt', 'baseline': None, 'after': 'x'})
    assert app.reject(cid, aid) is True
    assert [a['status'] for a in app.store.actions(cid) if a['id'] == aid] == ['rejected']
    assert called == [(str(project), cid, 'notes.txt')]


def test_the_forget_preview_route_reads_and_changes_nothing(app):
    ident = app.dispatch('memory_save', {'kind': 'semantic', 'content': 'My name is Alex.'})
    before = app.store.query('SELECT id,content,updated FROM memories ORDER BY id')
    preview = app.dispatch('memory_forget_preview', {'text': 'Please forget my name.'})
    assert preview['authorization'] == 'none' and ident in [item['id'] for item in preview['items']]
    assert app.store.query('SELECT id,content,updated FROM memories ORDER BY id') == before


def test_the_activity_names_a_network_wait_and_a_sleep_stall(app):
    cid = app.new_chat(None)['id']
    rid = app.runs.create(cid, 'goal text', app.runtime_config(app.chat(cid), False))
    app.runs.transition(rid, 'running')
    app.runs.note(rid, 'model_attempt_started', {'step': 1, 'attempt': 1})
    app.runs.note(rid, 'model_attempt', {'step': 1, 'attempt': 1, 'outcome': 'stalled', 'stall': 'sleep',
                                         'seconds': 12.0, 'sent': True})
    app.runs.note(rid, 'model_retry', {'step': 1, 'attempt': 2, 'delay': 1.0, 'network': True})
    items = app.dispatch('run_activity', {'run_id': rid})['items']
    model = [i for i in items if i['kind'] == 'model']
    retry = [i for i in items if i['kind'] == 'retry']
    assert model[0]['label'] == 'The model stopped sending' and model[0]['status'] == 'failed'
    assert retry[0]['label'] == 'Waiting for the network (attempt 2)'
    assert retry[0]['detail'] == 'The Mac slept while waiting for the model, so Pebble asked again; nothing had run.'
    # Without the runner's network mark the label is the Phase 22 one.
    app.runs.note(rid, 'model_retry', {'step': 1, 'attempt': 3, 'delay': 2.0})
    labels = [i['label'] for i in app.dispatch('run_activity', {'run_id': rid})['items'] if i['kind'] == 'retry']
    assert labels == ['Waiting for the network (attempt 2)', 'Reconnecting to the model (attempt 3)']
