"""Phase 45 (1.9.2, D128): the person's YouTube task on 1.9.1 (the person's Comet, 01:33 on 2026-10-26). The window
route opened the video, but the readback looked only for words the watch page did not show among its first rows, a
second press on a stale "Play (k)" paused the song, the model opened the same search again with OpenURL while its
BrowserRequest waited, and the completion check never saw Comet's own "Audio playing" title."""
import uuid
from types import SimpleNamespace

import pytest

from pebble import open_url, screen_session as ss
from pebble.screen_session import ScreenRefused
from pebble.store import Store
from test_browser_window_44 import COMET, SHOP, fenced, page
from test_screen_m1_43 import FakeAX43, Window43
from test_screen_session import ALL, App, Clock, el, make

PLAYING = 'Navior (Official Video) - YouTube - Audio playing - Comet - Tohin'


def watch(rows=None):
    return page(SHOP + '/watch?v=1', rows if rows is not None else [
        el('AXButton', '/1/0/0', title='Play (k)'), el('AXButton', '/1/0/1', title='Save to playlist')])


@pytest.fixture
def env(tmp_path):
    clock, ax = Clock(), FakeAX43()
    app = App(4242, 1_700_000_000.25, bundle_id=COMET)
    ax.apps.append(app)
    window = app.add(Window43(7001, 'Shop', watch()))
    store = Store(tmp_path / 'home')
    sessions = make(store, ax, clock)
    yield SimpleNamespace(clock=clock, ax=ax, app=app, window=window, store=store, sessions=sessions,
                          chat=uuid.uuid4().hex, run=uuid.uuid4().hex)
    store.close()


def ref(view, label):
    return next(e['ref'] for e in view['elements'] if e.get('label') == label)


def test_the_browsers_audio_title_is_shown_as_media_playing(env):
    sid = env.sessions.grant(env.chat, env.run, fenced(), ALL, 600)['session_id']
    quiet = env.sessions.observe(sid, env.run)
    assert 'media' not in quiet['window'] and quiet.get('note') is None
    env.window.title = PLAYING
    loud = env.sessions.observe(sid, env.run)
    assert loud['window']['media'] == 'playing' and loud['note'] == ss.PLAYING_NOTE
    assert ss.audible({'title': PLAYING}) and not ss.audible({'title': 'Audio playing tips - YouTube - Comet'})


def test_play_or_pause_is_refused_while_the_page_plays_unless_the_person_asked_to_pause(env):
    sid = env.sessions.grant(env.chat, env.run, fenced(), ALL, 600)['session_id']
    env.window.title = PLAYING
    view = env.sessions.observe(sid, env.run)
    with pytest.raises(ScreenRefused) as exc:
        env.sessions.act(sid, env.run, view['observation_id'], ref(view, 'Play (k)'), 'press', routine=True)
    assert exc.value.code == 'already_playing' and 'it is playing' in str(exc.value)
    assert not [c for c in env.ax.calls if c[0] == 'perform']
    view = env.sessions.observe(sid, env.run)
    other = env.sessions.act(sid, env.run, view['observation_id'], ref(view, 'Save to playlist'), 'press',
                             routine=True)
    assert other['status'] == 'needs_native_review', 'other buttons still work as before'
    view = env.sessions.observe(sid, env.run)
    asked = env.sessions.act(sid, env.run, view['observation_id'], ref(view, 'Play (k)'), 'press', routine=True,
                             pause_ok=True)
    assert asked['status'] == 'needs_native_review'


def test_play_is_pressed_as_before_when_nothing_plays(env):
    sid = env.sessions.grant(env.chat, env.run, fenced(), ALL, 600)['session_id']
    view = env.sessions.observe(sid, env.run)
    assert env.sessions.act(sid, env.run, view['observation_id'], ref(view, 'Play (k)'), 'press',
                            routine=True)['status'] == 'needs_native_review'


def test_a_pressed_link_that_opened_another_page_on_the_sites_occurred():
    target = fenced()
    link = {'role': 'AXLink', 'path': '/1/0/0'}
    before = {'title': 'navior song - YouTube - Comet', 'url': SHOP + '/results?q=navior', 'origin': SHOP}
    post = {'kind': 'text_appears', 'text': 'Subscribe'}
    landed = {'window': {'title': 'Navior (Official Video) - YouTube - Comet', 'url': SHOP + '/watch?v=1',
                         'origin': SHOP}, 'elements': []}
    assert ss.evaluate('app', target, post, before, link, landed, None) == ('occurred', 'navigated')
    stayed = {'window': before, 'elements': []}
    assert ss.evaluate('app', target, post, before, link, stayed, None) == ('not_observed', 'text_absent')
    button = {'role': 'AXButton', 'path': '/1/0/1'}
    assert ss.evaluate('app', target, post, before, button, landed, None) == ('not_observed', 'text_absent'), \
        'only a link press is read back by its landing'
    away = {'window': {'title': 'Bank', 'url': 'https://bank.example/', 'origin': 'https://bank.example'},
            'elements': []}
    assert ss.evaluate('app', target, post, before, link, away, None) == ('not_observed', 'text_absent')


def test_the_expected_words_count_in_the_new_window_title_only_when_new():
    target, link = fenced(), {'role': 'AXButton', 'path': '/1/0/1'}
    post = {'kind': 'text_appears', 'text': 'Official Video'}
    before = {'title': 'navior song - YouTube - Comet', 'url': SHOP + '/results', 'origin': SHOP}
    after = {'window': {'title': 'Navior (Official Video) - YouTube - Comet', 'url': SHOP + '/results',
                        'origin': SHOP}, 'elements': []}
    assert ss.evaluate('app', target, post, before, link, after, None) == ('occurred', 'text_in_title')
    same = {**before, 'title': 'Navior (Official Video) - YouTube - Comet'}
    assert ss.evaluate('app', target, post, same, link, after, None) == ('not_observed', 'text_absent')
    plain = {k: v for k, v in target.items() if k not in ('origins', 'read')}
    assert ss.evaluate('app', plain, post, before, link, after, None) == ('not_observed', 'text_absent'), \
        'an unfenced app window keeps the old rule'


def test_audible_reads_the_runs_fenced_windows_fresh(env):
    sid = env.sessions.grant(env.chat, env.run, fenced(), ALL, 600)['session_id']
    assert sid and env.sessions.audible(env.run) is False
    env.window.title = PLAYING
    assert env.sessions.audible(env.run) is True
    assert env.sessions.audible(uuid.uuid4().hex) is False and env.sessions.audible('not a run') is False


def grant(run, status='pending', sites=(SHOP,), read=None):
    return {'kind': 'browser_grant', 'status': status,
            'payload': {'run_id': run, 'sites': list(sites), 'read': list(read if read is not None else sites)}}


def test_openurl_is_refused_for_a_site_the_run_already_asked_browser_access_for():
    run = uuid.uuid4().hex
    asked = open_url.browser_request_for([grant(run)], run, SHOP + '/results?search_query=navior+song')
    assert asked and 'BrowserRequest' in asked and SHOP in asked
    assert open_url.browser_request_for([grant(run, 'applied')], run, SHOP + '/')
    for actions, url in (([grant(uuid.uuid4().hex)], SHOP + '/'), ([grant(run, 'rejected')], SHOP + '/'),
                         ([grant(run)], 'https://docs.example/'), ([], SHOP + '/'),
                         ([{'kind': 'url', 'status': 'applied', 'payload': {'run_id': run}}], SHOP + '/')):
        assert open_url.browser_request_for(actions, run, url) is None, (actions, url)
    assert open_url.browser_request_for([grant(run)], None, SHOP + '/') is None
    assert open_url.browser_request_for([grant(run)], run, 'http://shop.example/') is None


def test_a_fenced_window_on_another_desktop_comes_forward_once_the_grant_allows_it(env):
    sid = env.sessions.grant(env.chat, env.run, fenced(), ALL, 600)['session_id']
    env.window.listed = False  # on another desktop: Accessibility still answers, out of date
    first = env.sessions.observe(sid, env.run)
    assert first['status'] == 'needs_bring_forward' and first['reason'] == 'off_space', \
        'a fenced window off this desktop is not read stale'
    env.sessions.approve_raise(sid)  # BrowserRequest's own window: the person's review covered it
    view = env.sessions.observe(sid, env.run)
    assert env.ax.raises, 'the window was brought forward before reading'
    assert view.get('observation_id')


def test_an_unfenced_app_window_off_the_desktop_with_rows_is_read_as_before(env):
    plain = {k: v for k, v in fenced().items() if k not in ('origins', 'read')}
    sid = env.sessions.grant(env.chat, env.run, plain, ALL, 600)['session_id']
    env.window.listed = False
    view = env.sessions.observe(sid, env.run)
    assert view['status'] == 'off_space' and not env.ax.raises
