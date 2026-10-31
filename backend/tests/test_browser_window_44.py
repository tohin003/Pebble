"""Phase 44 (1.9.1, D117): BrowserRequest's window route. Comet refuses the extension's debugger on every site (measured,
.runtime/phase44-dev/native/ex1), so there, and wherever no extension is connected, the person's one review opens the
first address in a new window of their Chromium-family browser and grants a screen session on that window through
Accessibility, fenced to the granted sites by the page's own address."""
import plistlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import ax_worker, screen_session as ss
from pebble.screen_session import AppAdapter, ScreenRefused
from pebble.store import Store
from test_screen_m1_43 import FakeAX43, Window43
from test_screen_session import ALL, TEAM, App, Clock, el, make

SHOP, DOCS = 'https://shop.example', 'https://docs.example'
COMET = 'ai.perplexity.comet'


class Backend:
    def __init__(self, available=True):
        self.available = available

    def hello(self):
        return {'available': self.available, 'window_numbers': True}


def host(connected=True, browser='comet', ax=True):
    return SimpleNamespace(
        browser_bridge=SimpleNamespace(status=lambda: {'extension': {'connected': connected, 'browser': browser}}),
        screens=SimpleNamespace(adapters={'app': SimpleNamespace(backend=Backend(ax))}, revision=0))


def ask(app, **args):
    return ss.browser_request_proposal(app, {'sites': SHOP, 'read': DOCS, 'url': SHOP + '/item/1',
                                             'why': 'to add the kettle to your basket', **args})


# ---- the route follows what the browser allows -------------------------------------------------------------------------

def test_comet_takes_the_window_route_and_chrome_keeps_pebbles_own_tab(monkeypatch):
    monkeypatch.setattr(ss, '_default_browser', lambda: 'chrome')
    window = ask(host(browser='comet'))
    assert (window['route'], window['browser'], window['browser_name']) == ('window', 'comet', 'Comet')
    assert (window['sites'], window['read']) == (
        [SHOP, 'https://www.shop.example'], [SHOP, 'https://www.shop.example', DOCS, 'https://www.docs.example'])
    tab = ask(host(browser='chrome'))
    assert tab['route'] == 'tab' and 'browser_name' not in tab


def test_without_an_extension_the_default_browser_gets_the_window_route(monkeypatch):
    monkeypatch.setattr(ss, '_default_browser', lambda: 'comet')
    assert ask(host(connected=False))['route'] == 'window'
    monkeypatch.setattr(ss, '_default_browser', lambda: None)
    with pytest.raises(ScreenRefused) as exc:
        ask(host(connected=False))
    assert exc.value.code == 'unsupported_target' and 'Comet, Google Chrome' in str(exc.value)


def test_the_window_route_needs_accessibility_and_says_where_to_allow_it():
    with pytest.raises(ScreenRefused) as exc:
        ask(host(browser='comet', ax=False))
    assert exc.value.code == 'ax_unavailable' and 'Privacy & Security' in str(exc.value)


def test_the_default_browser_is_read_from_launchservices():
    def plist(*handlers):
        return lambda: plistlib.dumps({'LSHandlers': list(handlers)})
    https = {'LSHandlerURLScheme': 'https', 'LSHandlerRoleAll': 'ai.perplexity.comet'}
    assert ss._default_browser(plist({'LSHandlerURLScheme': 'mailto', 'LSHandlerRoleAll': 'x'}, https)) == 'comet'
    assert ss._default_browser(plist({'LSHandlerURLScheme': 'https', 'LSHandlerRoleAll': 'com.apple.safari'})) is None
    assert ss._default_browser(plist()) is None
    assert ss._default_browser(lambda: b'not a plist') is None


# ---- fenced app targets ----------------------------------------------------------------------------------------------

def fenced(number=7001, title='YouTube', act=(SHOP,), read=(SHOP, DOCS)):
    return {'kind': 'app', 'bundle_id': COMET, 'team_id': TEAM,
            'window': {'title_sha256': ax_worker.sha256(title), 'ax_identifier': None, 'window_number': number},
            'origins': list(act), 'read': list(read)}


def test_an_app_target_may_name_its_sites_and_a_plain_one_keeps_its_shape():
    assert ss.validate_target(fenced()) == fenced()
    plain = {k: v for k, v in fenced().items() if k not in ('origins', 'read')}
    assert ss.validate_target(plain) == plain
    for bad in ({**fenced(), 'read': [DOCS]}, {**fenced(), 'origins': ['https://shop.example/x']},
                {**fenced(), 'origins': []}, {k: v for k, v in fenced().items() if k != 'read'}):
        with pytest.raises(ScreenRefused) as exc:
            ss.validate_target(bad)
        assert exc.value.code == 'invalid', bad


def page(url, rows=None):
    """A Chromium window: the tab strip and toolbar (the browser's own), then the page's web area and its rows."""
    return [el('AXRadioButton', '/0/0', title='Bank statement - other tab'),
            el('AXTextField', '/0/1', title='Address and search bar', value='https://bank.example/statement'),
            el('AXWebArea', '/1/0', title='Shop', href=url),
            *(rows if rows is not None else [el('AXLink', '/1/0/0', title='Kettle', href=SHOP + '/item/1'),
                                             el('AXButton', '/1/0/1', title='Add to basket')])]


def test_the_fence_shows_only_the_page_and_only_on_a_granted_site():
    window = {'title': 'Shop'}
    shown, rows = AppAdapter._fence(fenced(), window, page(SHOP + '/item/1?ref=a'))
    assert (shown['origin'], shown['url']) == (SHOP, SHOP + '/item/1?ref=a')
    assert [r['path'] for r in rows] == ['/1/0', '/1/0/0', '/1/0/1'], 'the browser tabs and toolbar are never read'
    assert AppAdapter._fence(fenced(), window, page('https://bank.example/statement')) == (
        {**window, 'url': None, 'origin': None}, [])
    assert AppAdapter._fence(fenced(), window, page(DOCS + '/guide'))[0]['origin'] == DOCS, 'a read site reads'
    assert AppAdapter._fence(fenced(), window, page(SHOP + '/login')) == ({**window, 'url': None, 'origin': SHOP}, [])
    assert AppAdapter._fence(fenced(), window, page(SHOP)[:2]) == ({**window, 'url': None, 'origin': None}, [])


@pytest.fixture
def env(tmp_path):
    clock, ax = Clock(), FakeAX43()
    app = App(4242, 1_700_000_000.25, bundle_id=COMET)
    ax.apps.append(app)
    window = app.add(Window43(7001, 'Shop', page(SHOP + '/item/1')))
    store = Store(tmp_path / 'home')
    sessions = make(store, ax, clock)
    yield SimpleNamespace(clock=clock, ax=ax, app=app, window=window, store=store, sessions=sessions,
                          chat=uuid.uuid4().hex, run=uuid.uuid4().hex)
    store.close()


def test_a_fenced_window_is_observed_by_its_page_and_hidden_off_the_sites(env):
    sid = env.sessions.grant(env.chat, env.run, fenced(), ALL, 600)['session_id']
    view = env.sessions.observe(sid, env.run)
    assert (view['window']['origin'], view['status']) == (SHOP, 'ready')
    assert [e.get('label') for e in view['elements']] == ['Shop', 'Kettle', 'Add to basket']
    env.window.elements = page('https://bank.example/statement')
    away = env.sessions.observe(sid, env.run)
    assert (away['status'], away['elements'], away['window']['origin']) == ('away', [], None)
    assert 'outside the sites' in away['note']
    env.window.elements = page(SHOP + '/checkout/login')
    turn = env.sessions.observe(sid, env.run)
    assert (turn['status'], turn['elements'], turn['window']['origin']) == ('handback', [], SHOP)


def test_a_read_only_site_in_a_fenced_window_is_never_acted_on(env):
    env.window.elements = page(DOCS + '/guide', [el('AXLink', '/1/0/0', title='Next page', href=DOCS + '/guide/2')])
    sid = env.sessions.grant(env.chat, env.run, fenced(), ALL, 600)['session_id']
    view = env.sessions.observe(sid, env.run)
    ref = next(e['ref'] for e in view['elements'] if e.get('label') == 'Next page')
    with pytest.raises(ScreenRefused) as exc:
        env.sessions.act(sid, env.run, view['observation_id'], ref, 'press', routine=True)
    assert exc.value.code == 'action_not_granted' and 'only read' in str(exc.value)
    assert not [c for c in env.ax.calls if c[0] == 'perform']


def test_a_link_on_a_granted_act_site_is_routine_navigation_in_full_access(env):
    sid = env.sessions.grant(env.chat, env.run, fenced(), ALL, 600)['session_id']
    view = env.sessions.observe(sid, env.run)
    ref = next(e['ref'] for e in view['elements'] if e.get('label') == 'Kettle')
    reviewed = env.sessions.act(sid, env.run, view['observation_id'], ref, 'press')
    assert reviewed['status'] == 'needs_native_review'
    view = env.sessions.observe(sid, env.run)
    ref = next(e['ref'] for e in view['elements'] if e.get('label') == 'Kettle')
    routine = env.sessions.act(sid, env.run, view['observation_id'], ref, 'press', routine=True)
    assert routine['status'] != 'needs_native_review'
    assert ('perform', '/1/0/0', 'press', None) in env.ax.calls


# ---- opening the window ----------------------------------------------------------------------------------------------

class Adapter:
    """AppAdapter.windows for the browser: not running, or its windows, then one more after the open."""

    def __init__(self, before, after):
        self.rows, self.after = before, after

    def windows(self, query):
        assert query == {'kind': 'app', 'bundle_id': COMET, 'team_id': TEAM}
        if self.rows is None:
            raise ScreenRefused('not_running')
        return self.rows


def row(number, title='YouTube'):
    return {'window_number': number, 'title': title, 'title_sha256': ax_worker.sha256(title), 'ax_identifier': None}


def opening(adapter, ran, tmp_path):
    bundle = tmp_path / 'Comet.app'
    (bundle / 'Contents/MacOS').mkdir(parents=True, exist_ok=True)
    (bundle / 'Contents/MacOS/Comet').write_text('')
    (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'Comet'}))
    app = SimpleNamespace(screens=SimpleNamespace(adapters={'app': adapter}))
    payload = {'browser': 'comet', 'url': SHOP + '/item/1', 'sites': [SHOP], 'read': [SHOP, DOCS]}
    clock = Clock()

    def run(argv):
        ran.append(argv)
        adapter.rows = adapter.after
        return 0
    return lambda: ss.open_browser_window(
        app, payload, find=lambda name: {'id': str(bundle), 'bundle_id': COMET, 'team_id': TEAM, 'name': name},
        run=run, sleep=clock.sleep, clock=clock.monotonic)


def test_a_running_browser_opens_a_new_window_and_the_target_names_it(tmp_path):
    ran = []
    target = opening(Adapter([row(11)], [row(11), row(12)]), ran, tmp_path)()
    assert ran == [[str(tmp_path / 'Comet.app/Contents/MacOS/Comet'), '--new-window', SHOP + '/item/1']]
    assert target == fenced(number=12, title='YouTube', read=(SHOP, DOCS))
    assert ss.validate_target(target) == target


def test_a_stopped_browser_is_started_by_launchservices_with_the_same_address(tmp_path):
    ran = []
    target = opening(Adapter(None, [row(5)]), ran, tmp_path)()
    assert ran == [['/usr/bin/open', '-a', str(tmp_path / 'Comet.app'), '--args', '--new-window', SHOP + '/item/1']]
    assert target['window']['window_number'] == 5


def test_no_new_window_or_two_new_ones_name_nothing(tmp_path):
    for after in ([row(11)], [row(11), row(12), row(13)]):
        with pytest.raises(ScreenRefused) as exc:
            opening(Adapter([row(11)], after), [], tmp_path)()
        assert exc.value.code == 'window_not_found'


def test_an_approved_window_request_grants_a_fenced_session_on_the_new_window(env, monkeypatch):
    target = fenced()
    monkeypatch.setattr(ss, 'open_browser_window', lambda app, payload: target)
    app = SimpleNamespace(screens=env.sessions)
    payload = {'route': 'window', 'browser': 'comet', 'sites': [SHOP], 'read': [SHOP, DOCS], 'url': SHOP + '/item/1',
               'actions': ALL, 'ttl': 600}
    result = ss.grant_browser_requested(app, env.chat, env.run, payload)
    assert result['granted'] is True and result['route'] == 'window' and result['session_id'] in env.sessions.requested
    assert env.sessions.sessions(env.chat)[0]['target'] == target
    assert env.sessions.observe(result['session_id'], env.run)['window']['origin'] == SHOP


# ---- a browser whose own policy refuses the debugger -----------------------------------------------------------------

def test_a_policy_refused_debugger_is_said_plainly_and_the_next_request_takes_the_window_route(monkeypatch):
    import shutil
    import tempfile
    from test_browser_owned_43 import make, until
    short = Path(tempfile.mkdtemp(prefix='pw44-', dir='/tmp'))  # the bridge's socket path has a length limit
    env = make(short, browser='brave')  # not known in advance: learned from the refusal
    try:
        gid = env.bridge.grant_sites(env.run, [SHOP], [SHOP], 600)['grant_id']
        tab_id = env.bridge.open_tab(gid, SHOP + '/item/1')['tab_id']
        until(lambda: env.bridge.access(tab_id) is not None)
        target = {'kind': 'browser', 'profile': 'chrome', 'tab_id': tab_id, 'origins': [SHOP]}
        sid = env.sessions.grant(env.chat, env.run, target, ALL, 600)['session_id']
        real = env.ext._handle
        env.ext._handle = lambda message: {'ok': False, 'error': {
            'code': 'cdp_error', 'message': 'Host access is restricted by policy.'}} if message['op'] == 'attach' \
            else real(message)
        with pytest.raises(ScreenRefused) as exc:
            env.sessions.observe(sid, env.run)
        assert exc.value.code == 'unsupported_target' and 'BrowserRequest' in str(exc.value)
        assert 'could not be read' not in str(exc.value)
        assert env.bridge.blocked == {'brave'} and env.bridge.status()['extension']['debugger_blocked'] is True
        monkeypatch.setattr(ss, 'capabilities', lambda app, run_id=None: {'native': {'ax': True}})
        app = SimpleNamespace(browser_bridge=env.bridge)
        payload = ss.browser_request_proposal(app, {'sites': SHOP, 'url': SHOP + '/item/1', 'why': 'to look again'})
        assert (payload['route'], payload['browser'], payload['browser_name']) == ('window', 'brave', 'Brave Browser')
    finally:
        env.ext.close()
        env.bridge.close()
        env.store.close()
        shutil.rmtree(short, ignore_errors=True)


def test_browser_request_is_offered_without_an_extension_when_the_window_route_can_open_one(monkeypatch):
    from pebble import runner
    on = SimpleNamespace(task_policy={'tools': {'screen': True}})
    monkeypatch.setattr(ss, '_default_browser', lambda: 'comet')
    assert runner.with_screen_request(on, [], ws=None, large=True) == [ss.REQUEST_SCHEMA, ss.BROWSER_REQUEST_SCHEMA]
    monkeypatch.setattr(ss, '_default_browser', lambda: None)
    assert runner.with_screen_request(on, [], ws=None, large=True) == [ss.REQUEST_SCHEMA]
    sentences = runner.tool_sentences({'OpenURL', 'BrowserRequest'})
    assert 'instead of OpenURL' in sentences and 'never plays, presses or fills' in sentences
    assert 'before operating any window' not in sentences, 'OpenURL is no longer the first step for a video'


def test_a_site_and_its_www_twin_are_one_site_and_search_words_build_the_first_address(monkeypatch):
    monkeypatch.setattr(ss, '_default_browser', lambda: None)
    payload = ss.browser_request_proposal(host(browser='chrome'), {
        'sites': 'youtube.com', 'site': 'YouTube', 'query': 'navior song', 'why': 'to play the song asked for'})
    assert payload['url'] == 'https://www.youtube.com/results?search_query=navior+song'
    assert payload['sites'] == ['https://youtube.com', 'https://www.youtube.com'], 'youtube.com sends visits to www'
    assert ss._twin('https://www.youtube.com') == 'https://youtube.com'
    assert ss._twin('https://music.youtube.com') is None and ss._twin('http://localhost:8080') is None
    with pytest.raises(ScreenRefused) as exc:  # an unknown site never reaches the network for its search page
        ss.browser_request_proposal(host(browser='chrome'), {'sites': 'https://example.org', 'query': 'kettles',
                                                             'site': 'https://example.org', 'why': 'to look'})
    assert exc.value.code == 'invalid' and 'site and query' in str(exc.value)
