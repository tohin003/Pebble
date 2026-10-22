"""Phase 24 (t24-browser, V1-007): a tab or window the person grants can reach the model.

In 0.6.1 a grant needed a live run and bound to it, never checked the 'screen' permission (off by default), and the
model had no way to learn the session. Here:
- a grant made while no task can use it is a pending grant that the run of the person's next message takes
  (ScreenSessions.grant_pending / bind_pending), with its own expiry;
- the grant route checks the 'screen' permission in the same step: off, it answers with an enable action and records
  nothing; the person's one confirmation turns it on and records the grant together (grant_route);
- ScreenSessions.session_for_run(run_id) gives the runner (t24-agent) {id, kind, origin, actions, expires_at}.

The App routes reach grant_route through the parent's app.py patch; the two tests that need it are strict xfails until
it lands. Scripted models, fake AX and a fake Chrome only: no network, model, Keychain or real browser.
"""

import copy
import inspect
import json
import re
import socket
import time
import uuid
from types import SimpleNamespace

import pytest

from pebble import browser_bridge as bb
from pebble import screen_session
from pebble.app import App
from pebble.browser_bridge import BrowserBridge, BrowserRouter, ChromeAdapter, ChromeTabs
from pebble.permissions import DEFAULT
from pebble.policy import PolicyError
from pebble.screen_session import AppAdapter, ScreenRefused, ScreenSessions, grant_route
from test_browser_bridge_22 import KEY, PAIRING_ID, ChromePeer, FakeExtension, row, until
from test_browser_session import ORIGIN, compose as page_compose
from test_screen_session import TITLE, App as AXApp, Clock, FakeAX, Window, compose, target as app_target


@pytest.fixture
def tmp_path(short_tmp_path):
    """This file's bridges need a socket path under 104 bytes, whatever pytest's basetemp (conftest.short_tmp_path)."""
    return short_tmp_path


WIDE = {'id': 'wide', 'kind': 'openai', 'name': 'Wide', 'url': 'https://unused.invalid', 'model': 'fixture',
        'context': 32768, 'budget': 16384, 'tools': True}
LOOK = ['focus', 'scroll', 'expand', 'collapse']
PATCHED_GRANT = 'grant_route' in inspect.getsource(App.screen_session_grant)
PATCHED_SEND = 'bind_pending' in inspect.getsource(App.send)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('These tests must not open network connections.')
        return connect(self, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The App on a flat home, a fake-AX app window for screen sessions and a fixture model profile above 8K."""
    events = []
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *event: events.append(event))
    monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(WIDE))
    clock, ax = Clock(), FakeAX()
    window_app = AXApp(4242, 1_700_000_000.25)
    ax.apps.append(window_app)
    window_app.add(Window('win-1', TITLE, compose()))
    app.screens = ScreenSessions(app.store, {'app': AppAdapter(ax, processes=ax.processes)}, clock=clock.monotonic,
                                 wall=clock.wall, sleep=clock.sleep)
    yield SimpleNamespace(app=app, ax=ax, clock=clock, events=events)
    app.active = app.active_run = None
    app.close()


def screen(app, on):
    policy = app.store.setting('permissions', DEFAULT)
    app.setting('permissions', {**policy, 'tools': {**policy['tools'], 'screen': on}})


def screen_on(app):
    return app.store.setting('permissions', DEFAULT)['tools'].get('screen') is True


def start(app, chat, state='running'):
    """A live root run of this conversation, pinned with the permissions in force now. Only one run may be running on
    the device (runs_one_owner), so others wait on the person ('waiting_input'), which still counts as live."""
    rid = app.runs.create(chat['id'], 'Press Save in the Notes window.', app.runtime_config(chat, False))
    app.runs.transition(rid, 'running')
    if state != 'running':
        app.runs.transition(rid, state)
    return rid


# ---- the screen permission, checked in the same step -------------------------------------------------------------

def test_a_grant_with_screen_control_off_is_answered_with_an_enable_action_and_records_nothing(host):
    app = host.app
    chat = app.new_chat()
    version = app.policy_version()
    assert not screen_on(app), 'the screen permission is off by default'
    reply = grant_route(app, chat['id'], app_target(), ['focus', 'press'], 600)
    assert reply['status'] == 'needs_screen_permission' and reply['granted'] is False
    assert reply['enable'] == {'permission': 'screen', 'enable_screen': True, 'available': True,
                               'label': 'Turn on Screen control and grant'}
    assert reply['message'].startswith('Screen control is off in Permissions, so Pebble cannot use this window yet.')
    assert app.screens.sessions(chat['id']) == [] and not screen_on(app) and app.policy_version() == version
    assert host.ax.calls == [], 'nothing was pinned or read for an offer'
    # A malformed grant, or screenshots for anything but a Chrome tab, refuses before any offer.
    with pytest.raises(ScreenRefused):
        grant_route(app, chat['id'], {'kind': 'app'}, ['focus'], 600)
    with pytest.raises(PolicyError, match='only for a Chrome tab'):
        grant_route(app, chat['id'], app_target(), ['focus'], 600, screenshots=True)
    with pytest.raises(ScreenRefused):
        grant_route(app, chat['id'], app_target(), ['focus'], 600, enable_screen='yes')
    # While a task runs the permission cannot change: the offer says so and the confirmation refuses.
    other = app.new_chat()
    app.active, app.active_run = other['id'], start(app, other)
    try:
        busy = grant_route(app, chat['id'], app_target(), ['focus'], 600)
        assert busy['enable']['available'] is False and screen_session.SCREEN_BUSY in busy['message']
        with pytest.raises(PolicyError, match='once the current task finishes'):
            grant_route(app, chat['id'], app_target(), ['focus'], 600, enable_screen=True)
    finally:
        app.active = app.active_run = None
    assert app.screens.sessions(chat['id']) == [] and not screen_on(app)


def test_the_confirmation_turns_screen_control_on_and_records_the_grant_together(host):
    app = host.app
    chat = app.new_chat()
    version = app.policy_version()
    # A grant that cannot be made (the window is gone) leaves the permission off: neither stays.
    with pytest.raises(ScreenRefused) as exc:
        grant_route(app, chat['id'], app_target(title='No such window'), ['focus'], 600, enable_screen=True)
    assert exc.value.code == 'window_not_found' and not screen_on(app) and app.policy_version() == version
    result = grant_route(app, chat['id'], app_target(), LOOK, 600, enable_screen=True)
    assert result['screen_enabled'] is True and result['pending'] is True and result['run_id'] is None
    assert screen_on(app) and app.policy_version() == version + 1, 'the same path as the Settings switch'
    assert ('settings', {}) in host.events
    [listed] = app.screens.sessions(chat['id'])
    assert listed['pending'] is True and listed['run_id'] is None and listed['state'] == 'active'
    # Already on: a plain grant, no second permission change.
    again = grant_route(app, chat['id'], app_target(), LOOK, 300)
    assert again['screen_enabled'] is False and app.policy_version() == version + 1


# ---- pending grants bind to the conversation's next run --------------------------------------------------------

def test_a_pending_grant_binds_to_the_next_run_of_its_conversation_only(host):
    app = host.app
    screen(app, True)
    chat, elsewhere = app.new_chat(), app.new_chat()
    result = grant_route(app, chat['id'], app_target(), LOOK, 600)
    sid = result['session_id']
    assert result['pending'] is True and result['screen_enabled'] is False
    with pytest.raises(ScreenRefused) as exc:
        app.screens.observe(sid, uuid.uuid4().hex)
    assert exc.value.code == 'wrong_run', 'no run owns a pending grant'
    other_run = start(app, elsewhere, 'waiting_input')
    assert app.screens.bind_pending(elsewhere['id'], other_run) == [], "another conversation's run never takes it"
    rid = start(app, chat)
    assert not app.screens.live_session(rid) and app.screens.session_for_run(rid) is None
    assert app.screens.bind_pending(chat['id'], rid) == [sid]
    assert app.screens.bind_pending(chat['id'], rid) == [], 'idempotent'
    [listed] = app.screens.sessions(chat['id'])
    assert (listed['run_id'], listed['pending']) == (rid, False)
    assert app.screens.live_session(rid) is True
    assert app.screens.session_for_run(rid)['id'] == sid
    assert app.screens.observe(sid, rid)['observation_id']
    with pytest.raises(ScreenRefused) as exc:
        app.screens.observe(sid, other_run)
    assert exc.value.code == 'wrong_run'
    with pytest.raises(ScreenRefused):
        app.screens.bind_pending('not-a-chat', rid)


def test_a_pending_grant_has_its_own_expiry_and_never_survives_a_restart(host):
    app, clock = host.app, host.clock
    screen(app, True)
    chat = app.new_chat()
    short = grant_route(app, chat['id'], app_target(), LOOK, 60)['session_id']
    clock.advance(61)
    rid = start(app, chat)
    assert app.screens.bind_pending(chat['id'], rid) == []
    assert {s['session_id']: s['state'] for s in app.screens.sessions(chat['id'])} == {short: 'expired'}
    kept = grant_route(app, chat['id'], app_target(), LOOK, 600)['session_id']
    restarted = ScreenSessions(app.store, app.screens.adapters, clock=clock.monotonic, wall=clock.wall)
    assert restarted.recovered['ended_sessions'] == 1
    assert restarted.bind_pending(chat['id'], rid) == []
    assert {s['session_id']: s['state'] for s in restarted.sessions(chat['id'])}[kept] == 'restarted'


def test_a_live_run_takes_the_grant_only_when_it_was_started_with_screen_control(host):
    app = host.app
    first, second = app.new_chat(), app.new_chat()
    pinned_off = start(app, second, 'waiting_input')  # started while screen control was off; now waits on the person
    screen(app, True)
    pinned_on = start(app, first)
    bound = grant_route(app, first['id'], app_target(), LOOK, 600)
    assert bound['pending'] is False and bound['run_id'] == pinned_on
    assert app.screens.session_for_run(pinned_on)['id'] == bound['session_id']
    waiting = grant_route(app, second['id'], app_target(), LOOK, 600)
    assert waiting['pending'] is True and app.screens.session_for_run(pinned_off) is None, \
        'a run pinned without screen control could never use it, so the next run gets it'


# ---- session_for_run ---------------------------------------------------------------------------------------------

def test_session_for_run_returns_the_live_grant_and_nothing_else(host):
    app, clock = host.app, host.clock
    screen(app, True)
    chat = app.new_chat()
    rid = start(app, chat)
    result = grant_route(app, chat['id'], app_target(), ['focus', 'type', 'press'], 900)
    sid = result['session_id']
    assert app.screens.session_for_run(rid) == {'id': sid, 'kind': 'app', 'origin': None,
                                                 'actions': ['focus', 'type', 'press'],
                                                 'expires_at': clock.wall() + 900}
    assert app.screens.session_for_run(uuid.uuid4().hex) is None and app.screens.session_for_run('x') is None
    app.screens.handoff(sid)
    assert app.screens.session_for_run(rid) is None, "the person's turn is not offered"
    app.screens.resume(sid)
    assert app.screens.session_for_run(rid)['id'] == sid, 'handed back'
    clock.advance(901)
    assert app.screens.session_for_run(rid) is None, 'expired'
    newer = grant_route(app, chat['id'], app_target(), LOOK, 600)['session_id']
    assert app.screens.session_for_run(rid)['id'] == newer
    app.screens.revoke(newer)
    assert app.screens.session_for_run(rid) is None


def test_session_for_run_names_a_tabs_one_site(tmp_path):
    class Pinned:
        def pin(self, target):
            return {'tab_id': target['tab_id']}
    from pebble.store import Store
    store = Store(tmp_path / 'home')
    try:
        sessions = ScreenSessions(store, {'browser': Pinned()})
        chat, run = uuid.uuid4().hex, uuid.uuid4().hex
        tab = {'kind': 'browser', 'profile': 'chrome', 'tab_id': '5', 'origins': [ORIGIN]}
        sid = sessions.grant(chat, run, tab, ['focus'], 300)['session_id']
        assert sessions.session_for_run(run) == {'id': sid, 'kind': 'browser', 'origin': ORIGIN, 'actions': ['focus'],
                                                 'expires_at': pytest.approx(time.time() + 300, abs=5)}
    finally:
        store.close()


# ---- the parent's app.py patches (strict xfail until they land at merge) ------------------------------------------

@pytest.mark.xfail(not PATCHED_GRANT, strict=True, reason='parent patch P1: App.screen_session_grant -> grant_route')
def test_the_main_window_route_answers_with_the_offer_and_records_a_pending_grant(host):
    app = host.app
    chat = app.new_chat()
    reply = app.dispatch('screen_session_grant', {'id': chat['id'], 'target': app_target(), 'actions': LOOK})
    assert reply['status'] == 'needs_screen_permission'
    result = app.dispatch('screen_session_grant', {'id': chat['id'], 'target': app_target(), 'actions': LOOK,
                                                   'ttl': 600, 'enable_screen': True})
    assert result['pending'] is True and result['screen_enabled'] is True


@pytest.mark.xfail(not PATCHED_SEND, strict=True, reason='parent patch P2: App.send binds pending grants')
def test_the_persons_next_message_binds_the_pending_grant(host, monkeypatch):
    app = host.app
    screen(app, True)
    chat = app.new_chat()
    sid = grant_route(app, chat['id'], app_target(), LOOK, 600)['session_id']
    monkeypatch.setattr(app.controller, 'launch', lambda *args, **kwargs: None)  # the run is not executed here
    try:
        rid = app.send(chat['id'], 'Press Save in the Notes window.')['run_id']
        assert app.screens.session_for_run(rid)['id'] == sid
    finally:
        app.active = app.active_run = None


def note_patch_p1(self, id, target, actions, ttl=1800, screenshots=False, enable_screen=False):
    """The note's patch P1 body for App.screen_session_grant, verbatim (the relative import made absolute)."""
    from pebble.screen_session import grant_route
    return grant_route(self, id, target, actions, ttl, screenshots=screenshots, enable_screen=enable_screen)


def test_with_patch_p1_the_phase16_and_phase22_route_contracts_hold(tmp_path, monkeypatch):
    monkeypatch.setattr(App, 'screen_session_grant', note_patch_p1)
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *event: None)  # its own blocked AX and Pebble browser
    monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(WIDE))
    try:
        cid = app.new_chat()['id']
        # test_phase22_merge: screenshots only for a Chrome tab, refused before anything else.
        app.active, app.active_run = cid, 'a' * 32
        with pytest.raises(PolicyError, match='only for a Chrome tab'):
            app.dispatch('screen_session_grant', {'id': cid, 'target': {'kind': 'browser', 'profile': 'pebble',
                                                  'tab_id': '42', 'origins': ['https://example.com']},
                                                  'actions': ['focus'], 'screenshots': True})
        app.active = app.active_run = None
        # validate_phase16 s1: screen control on, a run waiting on the person; app windows and Pebble's browser still
        # refuse (E-N2, E-B1) and nothing is recorded.
        screen(app, True)
        chat = app.chat(cid)
        start(app, chat, 'waiting_input')
        for target, marker in ((app_target(), 'E-N2'), ({'kind': 'browser', 'profile': 'pebble', 'tab_id': 'T1',
                                                         'origins': ['https://www.example.com']}, 'E-B1')):
            with pytest.raises(PolicyError) as exc:
                app.dispatch('screen_session_grant', {'id': cid, 'target': target, 'actions': ['focus']})
            assert marker in str(exc.value), str(exc.value)
        assert app.dispatch('screen_sessions', {'id': cid}) == []
    finally:
        app.active = app.active_run = None
        app.close()


# ---- end to end, with a scripted model: share -> grant -> ScreenObserve -> press -> approval card ------------------

def tool_call(name, args, ident):
    return {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def test_a_shared_and_granted_chrome_tab_reaches_the_model_and_a_press_waits_for_approval(tmp_path, monkeypatch):
    browser_events = []
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *event: browser_events.append(event))
    home = tmp_path / 'c'  # short: the bridge socket path must fit 103 bytes
    home.mkdir(mode=0o700)
    bridge = BrowserBridge(home, on_revoke=lambda tab_id=None: bb.revoke_chrome_sessions(app.screens, tab_id),
                           user_home=tmp_path / 'u', pairing=bb.MemoryPairing((PAIRING_ID, KEY, 1_800_000_000.0)),
                           notify=lambda data: app.emit('browser', data))  # as the parent's App patch wires it
    tabs = ChromeTabs(bridge, timeout=2.0)
    app.browser_bridge, app.chrome_tabs = bridge, tabs
    app.screens = ScreenSessions(app.store, {'browser': BrowserRouter({'chrome': ChromeAdapter(tabs)})})
    peer = ChromePeer()
    ext = FakeExtension(home, peer)
    ext.add(peer.add_tab(page_compose()), 5)
    seen, script = [], []

    def complete(messages, tools, cancel, token, effort):
        seen.append(sorted(t['function']['name'] for t in tools))
        if script:
            return script.pop(0)(messages)
        token('Done.')
        return {'role': 'assistant', 'content': 'Done.'}
    monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(WIDE))
    monkeypatch.setattr(app.model, 'complete', complete)
    # Parent patch P2 (App.send binds pending grants) stands in here for app.work's own run creation.
    created = app.runs.create

    def create(cid, *args, **kwargs):
        rid = created(cid, *args, **kwargs)
        app.screens.bind_pending(cid, rid)
        return rid
    monkeypatch.setattr(app.runs, 'create', create)
    try:
        assert bridge.start()['bridge']['listening']
        ext.connect()
        until(lambda: bridge.connected)
        # 1. Share, in Chrome. The main window hears of it (site and scope only).
        ext.share(row())
        until(lambda: bridge.share('5') is not None)
        until(lambda: any(e[0] == 'browser' and e[1].get('kind') == 'shared' for e in browser_events))
        shared = next(e[1] for e in browser_events if e[0] == 'browser' and e[1].get('kind') == 'shared')
        assert shared == {'kind': 'shared', 'tabs': [{'tab_id': '5', 'origin': ORIGIN, 'scope': 'tab'}], 'shared': 1}
        # 2. Grant, in Pebble, with no task running and screen control off: one confirmation does both.
        chat = app.new_chat()
        tab = {'kind': 'browser', 'profile': 'chrome', 'tab_id': '5', 'origins': [ORIGIN]}
        assert grant_route(app, chat['id'], tab, ['focus', 'scroll', 'press'], 900)['status'] == 'needs_screen_permission'
        granted = grant_route(app, chat['id'], tab, ['focus', 'scroll', 'press'], 900, enable_screen=True)
        sid = granted['session_id']
        assert granted['pending'] is True and granted['screen_enabled'] is True and granted['screenshots'] is False
        until(lambda: any(c.get('action') == 'grant' and c['session_id'] == sid for c in ext.controls))

        # 3. The person's next message: the model observes the tab, then asks to press Send.
        def observe(messages):
            return tool_call('ScreenObserve', {'session_id': sid}, 'c1')

        def press(messages):
            result = next(m['content'] for m in reversed(messages) if m.get('role') == 'tool')
            observation = re.search(r'"observation_id":\s*"([0-9a-f]{20})"', result).group(1)
            send = re.search(r'"ref":\s*"(e\d+@[0-9a-f]{20})",\s*"role":\s*"Button",\s*"label":\s*"Send"', result).group(1)
            return tool_call('ScreenAct', {'session_id': sid, 'observation_id': observation, 'element': send,
                                           'action': 'press'}, 'c2')
        script[:] = [observe, press]
        app.work(chat, 'Press the Send button in my Chrome tab.', False, [])
        rid = app.runs.list(chat['id'])[0]['id']
        assert {'ScreenObserve', 'ScreenAct'} <= set(seen[0]), seen[0]
        # Observe, press, then one closing turn while the approval waits (runner H1b: the reply continues after it).
        assert len(seen) >= 2 and script == [], 'the model observed, then asked to press'
        # 4. The press is an approval card in the main window; nothing was clicked.
        [card] = [a for a in app.store.actions(chat['id']) if a['kind'] == 'screen']
        assert card['status'] == 'pending' and card['payload']['requires_review'] is True
        assert card['payload']['review']['element']['label'] == 'Send'
        assert app.runs.get(rid)['state'] == 'waiting_approval'
        assert not any(c[1] == 'Input.dispatchMouseEvent' for c in ext.calls)
        assert any(c[1] in ('DOMSnapshot.captureSnapshot', 'Accessibility.getFullAXTree') for c in ext.calls)
        assert app.screens.session_for_run(rid)['id'] == sid
    finally:
        app.active = app.active_run = None
        ext.close()
        app.close()
