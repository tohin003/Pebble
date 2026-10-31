"""Phase 45 (1.9.11, F178): a removed kept tool can be put back for ten minutes, from Pebble's own re-checked copy;
then it is gone for good."""
import pytest

from pebble import workshop
from pebble.policy import PolicyError


class Store:
    def __init__(self):
        self.values = {}

    def setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


class App:
    def __init__(self):
        self.store = Store()


def kept(name):
    spec = {'name': name, 'purpose': 'Converts Celsius to Fahrenheit.', 'code': 'def run(c):\n    return c * 9 / 5 + 32\n'}
    return {'spec': spec, 'digest': workshop.digest(spec), 'kept_by': 'pebble', 'state': 'ok'}


@pytest.fixture
def app():
    a = App()
    a.store.set_setting(workshop.SETTING, {'tools': [kept('uxtest_c_to_f')], 'pending': []})
    return a


def test_a_removed_tool_is_listed_and_put_back_from_pebbles_own_copy(app):
    listed = workshop.remove(app, 'uxtest_c_to_f')
    assert listed['tools'] == [] and [r['name'] for r in listed['removed']] == ['uxtest_c_to_f']
    assert 590 <= listed['removed'][0]['seconds_left'] <= 600
    back = workshop.undo_remove(app, 'uxtest_c_to_f')
    assert [t['name'] for t in back['tools']] == ['uxtest_c_to_f'] and back['removed'] == []
    assert workshop.toolbox(app)['tools'][0]['digest'] == kept('uxtest_c_to_f')['digest']


def test_after_ten_minutes_it_is_gone_for_good(app, monkeypatch):
    workshop.remove(app, 'uxtest_c_to_f')
    later = workshop.time.time() + workshop.UNDO_SECONDS + 1
    monkeypatch.setattr(workshop.time, 'time', lambda: later)
    assert workshop.listing(app)['removed'] == []
    assert app.store.values[workshop.SETTING]['removed'] == [], 'the old copy is dropped from storage'
    with pytest.raises(PolicyError, match='can no longer be put back'):
        workshop.undo_remove(app, 'uxtest_c_to_f')


def test_a_changed_copy_or_a_new_tool_of_the_same_name_is_never_replaced(app):
    workshop.remove(app, 'uxtest_c_to_f')
    box = app.store.values[workshop.SETTING]
    box['removed'][0]['record']['spec']['code'] = 'def run(c):\n    return 0\n'  # tampered while removed
    with pytest.raises(PolicyError, match='changed after it was removed'):
        workshop.undo_remove(app, 'uxtest_c_to_f')
    app.store.set_setting(workshop.SETTING, {'tools': [kept('uxtest_c_to_f')], 'pending': []})
    workshop.remove(app, 'uxtest_c_to_f')
    box = app.store.values[workshop.SETTING]
    box['tools'] = [kept('uxtest_c_to_f')]
    with pytest.raises(PolicyError, match='in the Toolbox again'):
        workshop.undo_remove(app, 'uxtest_c_to_f')


def test_put_back_is_the_main_windows_only():
    from pathlib import Path
    from pebble.tool_authz import NATIVE_ONLY
    shell = (Path(__file__).resolve().parents[2] / 'src-tauri/src/main.rs').read_text()
    allowed = shell[shell.index('const ALLOWED: &[&str] = &['):]
    pet = shell[shell.index('if window.label()=="pet" && !['):]
    assert '"toolbox_undo_remove"' in allowed[:allowed.index('];')]
    assert '"toolbox_undo_remove"' not in pet[:pet.index('].contains(')]
    assert 'toolbox_undo_remove' in NATIVE_ONLY
