"""Phase 45 (1.9.5, F086 and F091 from audit round R3): the person's own words set the floor of what a site or window
request asks for (the flagship "search for the navior song and play it" asked for look and fill only and could not press
the video it found), and a refused action says what the session allows and how to ask the person for more (the run
stopped, and "Ask Pebble to finish" repeated the same refusal)."""
from types import SimpleNamespace

import pytest

from pebble import screen_session as ss

SHOP = 'https://shop.example'


def host(text):
    return SimpleNamespace(current_text=text, browser_bridge=SimpleNamespace(
        status=lambda: {'extension': {'connected': True, 'browser': 'chrome'}}))


def ask(app, **args):
    return ss.browser_request_proposal(app, {'sites': [SHOP], 'url': SHOP + '/item/1', 'why': 'to do the task', **args})


@pytest.mark.parametrize('text', ['On YouTube, search for the navior song and play it', 'Watch the trailer',
                                  'Click Subscribe on that channel', 'pick the cheapest kettle and add it to my cart'])
def test_words_that_need_pressing_raise_the_request_to_press(text):
    payload = ask(host(text))
    assert payload['abilities'] == ['look', 'fill', 'press'] and 'press' in payload['actions']
    assert 'press' in ask(host(text), abilities='look')['actions'], 'a narrower ask is raised too'


@pytest.mark.parametrize('text', ['Search YouTube for lofi playlists and tell me the top three',
                                  'Read the reviews of this kettle', 'Open the docs site', None])
def test_other_words_keep_what_the_model_asked(text):
    assert ask(host(text))['abilities'] == ['look', 'fill']
    assert ask(host(text), abilities='look')['abilities'] == ['look']


def test_a_refusal_names_what_is_allowed_and_the_way_to_ask():
    window = {'kind': 'app', 'target': {'read': None}, 'actions': ['focus', 'scroll', 'select', 'type']}
    fenced = {'kind': 'app', 'target': {'read': [SHOP], 'origins': [SHOP]}, 'actions': ['focus', 'scroll']}
    tab = {'kind': 'browser', 'target': {'origins': [SHOP]}, 'actions': ['focus', 'scroll', 'select', 'type']}
    assert ss.not_granted(window, 'press') == ('This session allows look and fill, not press. To press, ask the person '
                                               'again with ScreenRequest for the same window and abilities press; they '
                                               'decide.')
    assert 'BrowserRequest for the same site and abilities fill' in ss.not_granted(fenced, 'type')
    assert ss.not_granted(fenced, 'type').startswith('This session allows look, not type.')
    assert 'BrowserRequest for the same site' in ss.not_granted(tab, 'enter')


def test_a_malformed_level_still_meets_its_refusal():
    with pytest.raises(ss.ScreenRefused) as exc:
        ask(host('Play the video'), abilities=['press'])
    assert exc.value.code == 'invalid'
