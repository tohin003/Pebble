"""Phase 42 (1.8.0): Pebble asks for one app window (ScreenRequest); only the person's review grants it, and the screen
tools then follow that session without UI words in the message; the apps Pebble never operates; the Companion offers
the request only with Screen control on; the Accessibility routes are the main window's."""
import plistlib
import uuid
from types import SimpleNamespace

import pytest

from pebble import ax_worker, permissions, runner, screen_session as ss, tool_authz
from pebble.screen_session import ScreenRefused
from pebble.store import Store
from test_screen_session import BUNDLE, TEAM, TITLE, App, Clock, FakeAX, Window, compose, make


def bundle(tmp_path, name, bundle_id):
    path = tmp_path / 'apps' / f'{name}.app'
    (path / 'Contents').mkdir(parents=True)
    (path / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier': bundle_id, 'CFBundleName': name}))
    return str(path)


@pytest.fixture
def env(tmp_path):
    clock, ax = Clock(), FakeAX()
    running = App(4242, 1_700_000_000.25)
    ax.apps.append(running)
    window = running.add(Window('win-1', TITLE, compose()))
    store = Store(tmp_path / 'home')
    sessions = make(store, ax, clock)
    path = bundle(tmp_path, 'Notes', BUNDLE)
    rows = [{'pid': 4242, 'start': running.start, 'executable': path + '/Contents/MacOS/Notes', 'bundle_path': path}]
    yield SimpleNamespace(clock=clock, ax=ax, running=running, window=window, store=store, sessions=sessions,
                          host=SimpleNamespace(screens=sessions), rows=rows, tmp=tmp_path,
                          chat=uuid.uuid4().hex, run=uuid.uuid4().hex)
    store.close()


def reader(entry):
    return lambda path: {'name': entry[0], 'bundle_id': entry[1], 'team_id': entry[2]}


def ask(env, **args):
    return ss.request_proposal(env.host, {'app': 'Notes', 'why': 'to fix the typo you mentioned', **args},
                               lister=lambda: env.rows, reader=reader(('Notes', BUNDLE, TEAM)))


def refused(code, fn, *args, **kwargs):
    with pytest.raises(ScreenRefused) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code, (exc.value.code, str(exc.value))
    return exc.value


def test_a_request_names_the_running_app_and_its_focused_window_and_grants_nothing(env):
    payload = ask(env)
    assert (payload['app'], payload['bundle_id'], payload['team_id']) == ('Notes', BUNDLE, TEAM)
    assert payload['window'] == {'title_sha256': ax_worker.sha256(TITLE), 'ax_identifier': None}
    assert payload['title'] == TITLE and payload['abilities'] == ['look', 'fill'] and payload['ttl'] == 900
    assert payload['actions'] == ['focus', 'scroll', 'expand', 'collapse', 'select', 'toggle', 'type']
    assert env.sessions.sessions(env.chat) == [], 'nothing is granted until the person approves'
    assert ask(env, abilities='look')['actions'] == ['focus', 'scroll', 'expand', 'collapse']
    assert ask(env)['abilities'] == ['look', 'fill'], 'none named: the documented default'
    assert ask(env, abilities='press')['abilities'] == ['look', 'fill', 'press']
    assert 'press' in ask(env, abilities='press')['actions'] and 'press' not in ask(env)['actions']


@pytest.mark.parametrize('name, bundle_id', [('Terminal', 'com.apple.Terminal'), ('Ghostty', 'com.mitchellh.ghostty'),
                                             ('Disk Utility', 'com.apple.DiskUtility'), ('Pebble', 'com.pebble.local'),
                                             ('Keychain Access', 'com.apple.keychainaccess')])
def test_apps_pebble_never_operates_are_refused_before_any_window_is_read(env, name, bundle_id):
    path = bundle(env.tmp, name, bundle_id)
    rows = [{'pid': 4242, 'start': env.running.start, 'executable': path + '/Contents/MacOS/x', 'bundle_path': path}]
    before = len(env.ax.calls)
    refused('unsupported_target', ss.request_proposal, env.host, {'app': name, 'why': 'to look'},
            lister=lambda: rows, reader=reader((name, bundle_id, None)))
    assert len(env.ax.calls) == before


def test_several_windows_without_a_front_one_take_the_frontmost_and_a_named_one_is_found(env):
    # Phase 43 (1.9.0): 1.8.0 refused here (Comet, several windows, none focused: a dead end); now the frontmost window
    # (AX lists front to back) is taken and named on the card, which asks the person before anything happens.
    env.window.focused = False
    env.running.add(Window('win-2', 'Shopping list — Notes', compose(), focused=False))
    assert ask(env)['title'] == TITLE
    assert ask(env, window='shopping')['title'] == 'Shopping list — Notes'
    error = refused('window_not_found', ask, env, window='no such window')
    assert TITLE in str(error) and 'untrusted' in str(error), 'the titles come back as data'


def test_an_app_that_is_not_running_or_malformed_requests_are_refused(env):
    error = refused('not_running', ss.request_proposal, env.host, {'app': 'Pages', 'why': 'to read it'},
                    lister=lambda: env.rows, reader=reader(('Notes', BUNDLE, TEAM)))
    assert 'open it' in str(error)
    for args in ({'app': '', 'why': 'x'}, {'app': 'Notes', 'why': ''}, {'app': 'Notes', 'why': 'x', 'abilities': 'all'},
                 {'app': 'Notes', 'why': 'x', 'abilities': ['look']}, {'app': 'Notes\nrm -rf', 'why': 'x'}):
        refused('invalid', ss.request_proposal, env.host, args, lister=lambda: env.rows,
                reader=reader(('Notes', BUNDLE, TEAM)))


def test_an_approved_request_grants_the_window_to_the_run_and_the_tools_follow_without_ui_words(env):
    payload = ask(env)
    result = ss.grant_requested(env.host, env.chat, env.run, payload)
    assert result['granted'] is True and result['session_id'] in env.sessions.requested
    session = env.sessions.session_for_run(env.run)
    assert session['id'] == result['session_id'] and session['kind'] == 'app'
    tools = {'screen': True}
    assert ss.offered_tools(budget=32768, tools=tools, live_session=True, text='fix the typo', requested=True) \
        == ss.TOOL_NAMES
    assert ss.offered_tools(budget=32768, tools=tools, live_session=True, text='fix the typo') == frozenset()
    assert ss.offered_tools(budget=8192, tools=tools, live_session=True, text='x', requested=True) == frozenset()
    assert ss.offered_tools(budget=32768, tools={'screen': False}, live_session=True, text='x', requested=True) \
        == frozenset()
    view = env.sessions.observe(result['session_id'], env.run)
    assert view['state'] == 'active' and view['elements']


def test_the_companion_offers_the_request_only_with_screen_control_on(monkeypatch):
    monkeypatch.setattr(ss, '_default_browser', lambda: None)  # Phase 44: no browser the window route opens
    on = SimpleNamespace(task_policy={'tools': {'screen': True}})
    off = SimpleNamespace(task_policy={'tools': {'screen': False}})
    assert runner.with_screen_request(on, [], ws=None, large=True) == [ss.REQUEST_SCHEMA]
    assert runner.with_screen_request(on, [ss.REQUEST_SCHEMA], ws=None, large=True) == [ss.REQUEST_SCHEMA]
    assert runner.with_screen_request(on, [], ws=object(), large=True) == [ss.REQUEST_SCHEMA], 'D132: in a project too'
    assert runner.with_screen_request(on, [], ws=None, large=False) == []
    assert runner.with_screen_request(off, [], ws=None, large=True) == []
    monkeypatch.setenv('PEBBLE_COMPANION_TOOLS', '0')
    assert runner.with_screen_request(on, [], ws=None, large=True) == []
    assert 'ScreenRequest' in runner.tool_sentences({'ScreenRequest'})
    assert 'ScreenRequest' not in runner.tool_sentences({'Shell', 'ReadFile', 'Screenshot'})


def test_the_request_is_a_root_only_screen_tool_and_the_accessibility_routes_are_the_main_windows():
    assert 'ScreenRequest' in tool_authz.ROOT_ONLY and permissions.GROUP['ScreenRequest'] == 'screen'
    assert {'app_control_status', 'app_control_request', 'diag_ax_probe'} <= tool_authz.NATIVE_ONLY
    from pebble.tool_catalogue import TOOLS
    assert sum(t['function']['name'] == 'ScreenRequest' for t in TOOLS) == 1


def test_a_window_on_another_desktop_says_so(env):
    sid = env.sessions.grant(env.chat, env.run, {'kind': 'app', 'bundle_id': BUNDLE, 'team_id': TEAM,
                                                 'window': {'title_sha256': ax_worker.sha256(TITLE),
                                                            'ax_identifier': None}}, ['focus'], 600)['session_id']

    def gone(*args):
        raise ax_worker.AXError('not_found', 'The window is closed, minimized or on another desktop.')
    env.ax.tree = gone
    with pytest.raises(ScreenRefused) as exc:
        env.sessions.observe(sid, env.run)
    assert 'another desktop' in str(exc.value)
