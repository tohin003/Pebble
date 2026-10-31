"""Phase 45 (1.9.16, F233 and F238 from audit round R10): Pebble's reply said "Chess repeatedly rejects clicks on the
board" when every refusal was Pebble's own check, and Activity showed the model's words for a refused act ("HOST
CAPABILITIES", "element refs", "pixel act"). A refused screen act tells the model who stopped it, keeps its code with
the step, and Activity words it for the person."""
from pebble import run_activity as ra, runner
from pebble.policy import Workspace
from pebble.screen_session import REFUSALS, ScreenRefused


def test_the_model_reads_that_pebbles_own_check_stopped_the_act(tmp_path):
    exc = ScreenRefused('stale_ui', 'The spot changed since Pebble looked at it. Observe again.')
    said = runner.tool_error(exc, Workspace(tmp_path))
    assert said.startswith("Not done: Pebble's own check stopped this act, and nothing reached the app. ")
    assert said.endswith(REFUSALS['stale_ui'] + ' The spot changed since Pebble looked at it. Observe again.')
    assert runner.tool_error(ValueError('Enter a message.'), Workspace(tmp_path)) == 'Enter a message.'


def row(code, error='This observation has no screenshot of the window, so a pixel act has nothing to aim at.'):
    feed, rows = ra.ActivityFeed.__new__(ra.ActivityFeed), []
    feed.label = lambda name, args: 'Acting in the app window'
    tool = {'name': 'ScreenAct', 'file_path': None, 'arg_name': None, 'error': error, 'refused_code': code,
            'status': None, 'state': 'completed'}
    feed._tool(lambda *item: rows.append(item), tool, 1, 0.0, (1.0, False), False, False)
    return rows[0]


def test_activity_words_a_refused_act_for_the_person():
    assert "'$.refused'" in ra.TOOLS_SQL
    seq, created, kind, label, detail, duration, status = row('no_screenshot')
    assert status == 'failed' and detail == "Pebble didn't click: its last look had no picture of the window to aim at."
    assert not any(word in detail for word in ('HOST', 'pixel act', 'element ref', 'observation'))
    assert row('some_new_code')[4] == "Pebble's check stopped this step; nothing reached the app."
    assert row(None, 'The script stopped.')[4] == 'The script stopped.', 'other failures read as before'
    assert set(ra.SCREEN_REFUSED) <= set(REFUSALS), 'every worded code is a real refusal'


def test_an_inapplicable_action_names_what_applies(tmp_path):
    """R10: "select" on a Chess square was refused with no way on; the refusal now names the actions that apply."""
    from test_screen_session import env as _env, grant, ref, refused  # noqa: F401
    import uuid
    from types import SimpleNamespace
    from test_screen_session import App, Clock, FakeAX, Store, Window, compose, make, target
    clock, ax = Clock(), FakeAX()
    app = App(4242, 1_700_000_000.25)
    ax.apps.append(app)
    app.add(Window('win-1', 'Compose', compose()))
    store = Store(tmp_path / 'home')
    try:
        env = SimpleNamespace(clock=clock, ax=ax, store=store, sessions=make(store, ax, clock), chat=uuid.uuid4().hex,
                              run=uuid.uuid4().hex)
        sid = env.sessions.grant(env.chat, env.run, target(title='Compose'), ['press', 'focus', 'select', 'type'],
                                 600)['session_id']
        view = env.sessions.observe(sid, env.run)
        error = refused('action_not_applicable', env.sessions.act, sid, env.run, view['observation_id'],
                        ref(env, sid, view, 'w/send'), 'select')
        assert str(error).endswith('On this element use: press, focus.'), str(error)
    finally:
        store.close()


def test_words_sent_with_scroll_or_press_say_what_to_use(tmp_path):
    """R10: four acts sent words with scroll or press and were told only "Only type takes text."."""
    import uuid
    from types import SimpleNamespace
    from test_screen_session import App, Clock, FakeAX, Store, Window, compose, make, ref, refused, target
    clock, ax = Clock(), FakeAX()
    app = App(4242, 1_700_000_000.25)
    ax.apps.append(app)
    app.add(Window('win-1', 'Compose', compose()))
    store = Store(tmp_path / 'home')
    try:
        env = SimpleNamespace(clock=clock, ax=ax, store=store, sessions=make(store, ax, clock), chat=uuid.uuid4().hex,
                              run=uuid.uuid4().hex)
        sid = env.sessions.grant(env.chat, env.run, target(title='Compose'), ['press', 'scroll', 'type'], 600)['session_id']
        view = env.sessions.observe(sid, env.run)
        scrolled = refused('invalid', env.sessions.act, sid, env.run, view['observation_id'],
                           ref(env, sid, view, 'w/body'), 'scroll', 'down')
        assert 'scroll_at (x, y, dy)' in str(scrolled)
        pressed = refused('invalid', env.sessions.act, sid, env.run, view['observation_id'],
                          ref(env, sid, view, 'w/send'), 'press', 'hello')
        assert 'act with type on that field' in str(pressed)
    finally:
        store.close()
