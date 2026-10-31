"""Phase 45: website and app control in a project conversation. 1.9.5 (F077) told the model they worked only without a
project; since D132 (the person, 2026-10-26) BrowserRequest, ScreenRequest, OpenURL and AppAction are offered in a project
conversation too, and OpenApp of a name that is no installed app says how to reach a website in either."""
from types import SimpleNamespace

import pytest

from pebble import runner
from pebble import screen_session as ss
from pebble.app import App


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield a
    a.close()


def test_the_sentence_that_sent_the_person_elsewhere_is_gone():
    assert not hasattr(runner, 'OUTSIDE_PROJECT') and not hasattr(runner, 'OUTSIDE_TASK')
    assert 'website and app control work in every conversation' in runner.PEBBLE_GUIDE


def test_a_project_conversation_is_offered_the_requests(monkeypatch):
    monkeypatch.setattr(runner, 'companion_tools_enabled', lambda: True)
    host = SimpleNamespace(task_policy={'tools': {'screen': True}},
                           browser_bridge=SimpleNamespace(status=lambda: {'extension': {'connected': True}}))
    names = [t['function']['name'] for t in runner.with_screen_request(host, [], ws=object(), large=True)]
    assert names == [ss.REQUEST_NAME, ss.BROWSER_REQUEST_NAME]
    host.task_policy = {'tools': {'screen': False}}
    assert runner.with_screen_request(host, [], ws=object(), large=True) == [], 'Screen control off: nothing'


def test_openapp_of_a_website_name_points_to_the_browser_in_either_conversation(app, monkeypatch):
    monkeypatch.setattr('pebble.app.installed_apps', lambda: [{'id': 'safari', 'name': 'Safari'}])
    chat = app.new_chat()
    for ws in (None, object()):
        plain = app._execute('OpenApp', {'name': 'Wikipedia'}, ws, chat['id'])
        assert plain['error'].startswith("No installed app is named 'Wikipedia'.")
        assert 'BrowserRequest' in plain['error'] and 'Installed apps: Safari' in plain['error']
        assert 'new conversation' not in plain['error']
