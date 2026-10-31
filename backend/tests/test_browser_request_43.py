"""Phase 43 (1.9.0, decision 3): BrowserRequest. Pebble asks for sites in the person's browser; only the person's review
grants them; the approval opens Pebble's own tab at the first address and a screen session on it for the run that
asked, over the real bridge socket (test_browser_owned_43's fake extension); a failure on the way ends the grant."""
from types import SimpleNamespace

import pytest

from pebble import screen_session as ss
from pebble.policy import PolicyError
from pebble.screen_session import ScreenRefused
from test_browser_owned_43 import DOCS, SHOP, env, tmp_path, until  # noqa: F401 (env and tmp_path are fixtures)

W_SHOP, W_DOCS = 'https://www.shop.example', 'https://www.docs.example'  # Phase 44: the www twins


def host(connected=True, browser='chrome'):  # Phase 44 (D117): Comet takes the window route (test_browser_window_44)
    return SimpleNamespace(browser_bridge=SimpleNamespace(
        status=lambda: {'extension': {'connected': connected, 'browser': browser}}))


def ask(app, **args):
    return ss.browser_request_proposal(app, {'sites': [SHOP], 'url': SHOP + '/item/1',
                                             'why': 'to add the kettle to your basket', **args})


def test_a_request_names_exact_sites_and_the_first_address_and_grants_nothing():
    payload = ask(host(), read=['docs.example'])
    # Phase 44 (1.9.1): each site with its www twin, one site to a person (test_browser_window_44)
    assert (payload['sites'], payload['read'], payload['url']) == ([SHOP, W_SHOP], [SHOP, W_SHOP, DOCS, W_DOCS],
                                                                   SHOP + '/item/1')
    assert payload['abilities'] == ['look', 'fill'] and 'press' not in payload['actions']
    assert payload['ttl'] == ss.REQUEST_TTL and payload['browser'] == 'chrome' and payload['route'] == 'tab'
    assert ask(host(), sites=['shop.example/'])['sites'] == [SHOP, W_SHOP], 'a bare host gets https'
    assert ask(host(), sites=SHOP + ', ' + DOCS)['sites'] == [SHOP, W_SHOP, DOCS, W_DOCS], \
        'one string, as the model sends it'
    assert 'press' in ask(host(), abilities='press')['actions']


@pytest.mark.parametrize('args', [{'sites': []}, {'sites': ''}, {'sites': ['ftp://shop.example']},
                                  {'sites': ['http://shop.example']}, {'url': 'https://elsewhere.example/'},
                                  {'why': ''}, {'abilities': 'everything'}, {'sites': [SHOP] * 9},
                                  {'read': ['not a site!']}, {'sites': ['https://shop.example/basket']}])
def test_malformed_requests_are_refused(args):
    with pytest.raises(ScreenRefused) as exc:
        ask(host(), **args)
    assert exc.value.code == 'invalid', (args, exc.value.code)


def test_without_a_connected_extension_the_request_says_which_switch_to_turn_on(monkeypatch):
    monkeypatch.setattr(ss, '_default_browser', lambda: None)  # Phase 44: no default browser the window route opens
    for app in (host(connected=False), SimpleNamespace()):
        with pytest.raises(ScreenRefused) as exc:
            ask(app)
        assert exc.value.code == 'unsupported_target' and 'Let Pebble open its own tabs here' in str(exc.value)


def chrome(env):
    # Phase 44 (D117): the fake extension starts as Comet, whose debugger is blocked; Pebble's own tab is Chrome's
    env.ext.send({'type': 'browser', 'browser': 'chrome'})
    until(lambda: env.bridge.link.browser == 'chrome')


def test_an_approved_request_opens_pebbles_tab_and_a_session_on_it(env):
    chrome(env)
    app = SimpleNamespace(browser_bridge=env.bridge, screens=env.sessions)
    payload = ss.browser_request_proposal(app, {'sites': [SHOP], 'read': [DOCS], 'url': SHOP + '/item/1',
                                                'why': 'to buy a kettle', 'abilities': 'press'})
    result = ss.grant_browser_requested(app, env.chat, env.run, payload)
    assert result['granted'] is True and result['session_id'] in env.sessions.requested
    target = env.sessions.sessions(env.chat)[0]['target']
    assert (target['profile'], target['origins'], target['read']) == ('chrome', [SHOP, W_SHOP],
                                                                      [SHOP, W_SHOP, DOCS, W_DOCS])
    until(lambda: env.bridge.access(result['tab_id']) is not None)
    view = env.sessions.observe(result['session_id'], env.run)
    assert view['window']['origin'] == SHOP and view['version'] == 2
    assert env.bridge.site_grant(result['grant_id'])['act'] == [SHOP, W_SHOP]


def test_a_tab_that_cannot_open_ends_the_grant_again(env, monkeypatch):
    chrome(env)
    app = SimpleNamespace(browser_bridge=env.bridge, screens=env.sessions)
    payload = ss.browser_request_proposal(app, {'sites': [SHOP], 'url': SHOP + '/item/1', 'why': 'to look'})
    ended = []
    real_end = env.bridge.end_site_grant

    def boom(grant_id, url, **kw):
        raise PolicyError('The tab could not be opened.')
    monkeypatch.setattr(env.bridge, 'open_tab', boom)
    monkeypatch.setattr(env.bridge, 'end_site_grant', lambda gid: ended.append(gid) or real_end(gid))
    with pytest.raises(PolicyError):
        ss.grant_browser_requested(app, env.chat, env.run, payload)
    assert len(ended) == 1 and env.bridge.site_grant(ended[0]) is None and env.sessions.sessions(env.chat) == []
