"""Phase 45 (1.9.17, F248 from audit round R11): "In Chess, select the white knight on g1 …" was refused twice ("App
opening needs an explicit request to open an app"), even after the person answered "Yes, open Chess". A request that
names the app asks to use it; one that names no app and asks to open none is still refused."""
import pytest

from pebble import app as app_module
from pebble.policy import PolicyError
from test_runner_honesty_24 import env  # noqa: F401


@pytest.fixture
def no_launch(monkeypatch):
    monkeypatch.setattr(app_module, 'installed_apps', lambda: [{'name': 'Chess', 'id': '/System/Applications/Chess.app'},
                                                              {'name': 'Calculator', 'id': '/System/Applications/Calculator.app'}])


def opened(e, text, name):
    chat = e.app.new_chat(None)
    e.app.active, e.app.current_text = chat['id'], text
    try:
        return e.app.execute('OpenApp', {'name': name}, None, chat['id'])
    finally:
        e.app.active, e.app.current_text = None, ''


def test_a_request_that_names_the_app_may_open_it(env, no_launch):
    e = env()
    for text, name in (('UXTEST: In Chess, select the white knight on g1 by clicking it, then click f3.', 'Chess'),
                       ('Use Calculator to multiply 1,248 by 0.85, then add 37.4.', 'Calculator')):
        result = opened(e, text, name)
        assert 'error' not in result, (text, result)


def test_a_request_that_names_no_app_still_may_not(env, no_launch):
    e = env()
    with pytest.raises(PolicyError, match='explicit request to open an app'):
        opened(e, 'What is 1,248 times 0.85?', 'Calculator')
    with pytest.raises(PolicyError, match='Ask the person whether to open it'):
        opened(e, 'Move the white knight to f3 in my game.', 'Chess')
