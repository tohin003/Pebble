"""Phase 45 (1.9.9, audit round R5, F154): a capture lane that macOS refuses says so, the pixel act's refusal says what
the person can do, and a window on another desktop comes forward first (once, after the person's review) when a
screenshot is wanted and capture is allowed; a lane that is off still says nothing."""
from pebble import screen_helper
from test_screen_layer4_45 import env, grant, look  # noqa: F401 (env is a fixture)
from test_screen_session import refused


def test_a_lane_macos_refuses_says_so_and_the_refusal_says_what_helps(env, monkeypatch):
    monkeypatch.setattr(screen_helper, 'switches', lambda: {'capture': True, 'foreground_input': True})
    env.lane.capture_on = False  # the helper's preflight: macOS has not allowed screen recording
    sid = grant(env)
    view = look(env, sid)
    assert 'screenshot' not in view and view['screenshot_unavailable'] == 'capture_not_permitted'
    error = refused('no_screenshot', env.sessions.act_pixels, sid, env.run, view['observation_id'], 'click_at', x=1, y=1)
    assert 'Screen & System Audio Recording' in str(error) and 'press element refs' in str(error)


def test_a_lane_that_is_off_still_says_nothing(env, monkeypatch):
    monkeypatch.setenv('PEBBLE_SCREEN_CAPTURE', '0')  # D135: on by default; this lane is switched off
    env.lane.capture_on = False
    view = look(env, grant(env))
    assert 'screenshot' not in view and 'screenshot_unavailable' not in view


def test_a_window_on_another_desktop_comes_forward_first_when_a_screenshot_is_wanted(env):
    env.window.listed = False
    sid = grant(env)
    assert env.sessions.observe(sid, env.run)['status'] == 'off_space', 'rows only: read where it is'
    answer = look(env, sid)
    assert (answer['status'], answer['reason'], answer['observation_id']) == ('needs_bring_forward', 'off_space', None)
    env.sessions.raise_window(sid)  # the person approved bringing it forward
    env.window.listed = True
    assert 'screenshot' in look(env, sid)
    env.lane.capture_on = False
    env.window.listed = False
    raised = len(env.ax.raises)
    assert look(env, sid)['status'] != 'off_space' and len(env.ax.raises) == raised + 1, \
        'approved once: it comes forward again, capture or not (F155)'


def test_without_capture_or_approval_a_readable_window_elsewhere_is_read_where_it_is(env):
    env.lane.capture_on = False
    env.window.listed = False
    sid = grant(env)
    assert look(env, sid)['status'] == 'off_space' and not env.ax.raises, 'nothing to bring it forward for'


def test_a_requested_window_on_another_desktop_comes_forward_without_a_second_card(env):
    # F155: the review of a window Pebble asked for covers bringing it forward; readable rows elsewhere still lag
    from types import SimpleNamespace
    from pebble.screen_session import grant_requested
    from test_screen_layer4_45 import target
    host = SimpleNamespace(screens=env.sessions)
    payload = {**target(), 'actions': ['focus', 'press'], 'ttl': 600, 'app': 'Calculator', 'title': 'Calculator'}
    sid = grant_requested(host, env.chat, env.run, payload)['session_id']
    env.window.listed = False
    view = env.sessions.observe(sid, env.run)
    assert view.get('status') != 'needs_bring_forward' and env.ax.raises, 'brought forward at once, no card'
    env.window.listed, env.window.minimized = True, True
    answer = env.sessions.observe(sid, env.run)
    assert (answer['status'], answer['reason']) == ('needs_bring_forward', 'minimized'), 'a minimized window still asks'


def test_a_board_square_is_pressed_like_a_keypad_key():
    # F166 (audit round R5): the Mac's Chess names its squares "white pawn, e2" and "e4"
    from pebble import screen_session as ss
    for label in ('white pawn, e2', 'e4', 'black knight, g8', 'h1'):
        assert ss.classify('press', {'role': 'AXButton', 'title': label}, kind='app')[0] == 'input', label
    for label in ('Delete game', 'e9', 'white pawn, e2 and resign'):
        assert ss.classify('press', {'role': 'AXButton', 'title': label}, kind='app')[0] == 'commit', label


def test_an_unchanged_window_says_a_selection_may_not_show_and_the_second_press_is_due():
    from pebble import screen_session as ss
    assert 'first of two presses' in ss.UNCHANGED_ROWS and 'do not repeat this one' in ss.UNCHANGED_ROWS
    text = ss.describe({'action': 'press', 'verdict': 'not_observed', 'basis': 'target_unchanged'},
                       {'element': {'label': 'white pawn, e2'}})
    assert 'a selection may not show there' in text and 'did not observe' not in text


def test_a_keypad_press_that_changes_the_display_is_seen_to_work(tmp_path):
    # F167 (audit round R5): Calculator's "1" landed (the display changed) while the key itself did not, and Pebble
    # stopped on "no visible effect"; a change anywhere in an app window's rows now counts
    from test_screen_session import App, Clock, FakeAX, Store, TITLE, Window, el, grant, make, act
    from types import SimpleNamespace
    import uuid
    clock, ax = Clock(), FakeAX()
    app = App(4242, 1_700_000_000.25)
    ax.apps.append(app)
    window = app.add(Window('win-1', TITLE, [el('AXStaticText', 'w/display', value='0', frame=[10, 10, 200, 40]),
                                              el('AXButton', 'w/k7', title='7', frame=[10, 60, 40, 40]),
                                              el('AXButton', 'w/k8', title='8', frame=[60, 60, 40, 40])]))

    def seven(_app, win, _text):
        win.find('w/display')['value'] = '7'
    window.hooks[('w/k7', 'press')] = seven
    store = Store(tmp_path / 'home')
    try:
        env = SimpleNamespace(clock=clock, ax=ax, app=app, window=window, store=store,
                              sessions=make(store, ax, clock), chat=uuid.uuid4().hex, run=uuid.uuid4().hex)
        sid = grant(env)
        pressed = act(env, sid, 'w/k7', 'press')
        assert (pressed['verdict'], pressed['basis'], pressed['outcome']) == ('occurred', 'window_changed', 'confirmed')
        idle = act(env, sid, 'w/k8', 'press')  # nothing changes anywhere
        assert idle['verdict'] == 'not_observed' and idle['basis'] in ('target_unchanged', 'value_unchanged')
        assert 'first of two presses' in idle['message']
    finally:
        store.close()


def test_a_refused_raise_still_lets_pebble_look_and_a_read_during_the_desktop_switch_is_retried(env, monkeypatch):
    # F167 (audit round R5): behind the full-screen workspace, Accessibility refused to raise Calculator's window and
    # the whole look failed with "The window could not be read."
    from types import SimpleNamespace
    from pebble.ax_worker import AXError
    from pebble.screen_session import grant_requested
    from test_screen_layer4_45 import target
    host = SimpleNamespace(screens=env.sessions)
    payload = {**target(), 'actions': ['focus', 'press'], 'ttl': 600, 'app': 'Calculator', 'title': 'Calculator'}
    sid = grant_requested(host, env.chat, env.run, payload)['session_id']
    env.window.listed = False

    def refuse(*args, **kwargs):
        raise AXError('cannot_complete')
    monkeypatch.setattr(env.ax, 'raise_window', refuse)
    view = env.sessions.observe(sid, env.run)
    assert view.get('observation_id') and view['status'] == 'off_space', 'read where it is, not an error'
    adapter = env.sessions.adapters['app']
    real, calls = adapter.snapshot, []

    def flaky(*args, **kwargs):  # the look's first read works; the read right after the raise fails once
        calls.append(1)
        if len(calls) == 2:
            raise AXError('cannot_complete')
        return real(*args, **kwargs)
    monkeypatch.setattr(adapter, 'snapshot', flaky)
    assert env.sessions.observe(sid, env.run).get('observation_id') and len(calls) >= 3, 'tried again after the switch'


def test_a_click_on_the_window_an_act_returned_says_to_look_again(env):
    """Audit round R10: Calculator's key press returned the window after it (D140), Pebble then tried a key by position
    on that observation and was told capture needed switching on, though it was on."""
    from test_screen_layer4_45 import consented
    sid = consented(env)
    view = look(env, sid)
    ref = next(e['ref'] for e in view['elements'] if e.get('label') == 'Bold')
    pressed = env.sessions.act(sid, env.run, view['observation_id'], ref, 'toggle')
    after = pressed['observation']['observation_id']
    error = refused('no_screenshot', env.sessions.act_pixels, sid, env.run, after, 'click_at', x=1, y=1)
    assert 'Call ScreenObserve for a fresh look with a screenshot' in str(error) and 'HOST CAPABILITIES' not in str(error)
