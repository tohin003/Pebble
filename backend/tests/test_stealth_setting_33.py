"""Phase 33 (1.1.0, D45-D47): Stealth Lockdown's setting. One switch, off by default, kept across restarts, told to every
window, and never treated as giving Pebble more access (it gives Pebble less: no screen capture, no focus)."""
import pytest

from pebble import settings_file
from pebble.app import App


@pytest.fixture
def app(tmp_path):
    a = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield a
    a.close()


def test_it_is_off_until_the_person_turns_it_on(app):
    assert settings_file.default('stealth') is False
    assert app.state()['settings']['stealth'] is False


def test_turning_it_on_is_kept_and_every_window_hears_of_it(app):
    events = []
    app.emit = lambda kind, payload=None: events.append(kind)
    assert app.setting('stealth', True) is True
    assert app.state()['settings']['stealth'] is True and 'settings' in events
    assert app.setting('stealth', False) is True and app.state()['settings']['stealth'] is False


@pytest.mark.parametrize('value', ['yes', 1, None, 'true'])
def test_only_true_or_false_is_accepted(app, value):
    with pytest.raises(ValueError):
        settings_file.validate('stealth', value)
    assert app.state()['settings']['stealth'] is False


def test_it_never_waits_for_a_confirmation_as_more_access_would():
    assert settings_file.widening('stealth', False, True) == []
    schema = settings_file.schema()['properties']['stealth']
    assert schema['type'] == 'boolean' and 'not hidden from a camera' in schema['description'].lower()
