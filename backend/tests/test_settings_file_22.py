"""Phase 22 (s22-storage): ~/.pebble/settings.json as the person's validated source for their settings.

Under a fake HOME. The app's own changes reach the file; the person's edits are validated and applied
through App.setting; a change that widens permissions or authority waits for the main window's
confirmation; an invalid file changes nothing and the error is shown; unknown keys are kept.
"""

import json
import threading
from pathlib import Path

import pytest

from pebble import data_home, settings_file
from pebble.app import App
from pebble.data_home import resolve
from pebble.data_mirror import DataServices
from pebble.permissions import DEFAULT


@pytest.fixture(autouse=True)
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('PEBBLE_HOME', raising=False)
    monkeypatch.setenv('AGENTGUARD_HOME', str(tmp_path / 'guard-unused'))
    yield home
    data_home.deactivate()


@pytest.fixture(autouse=True)
def no_vault(monkeypatch):
    from pebble import vault

    def refuse(self, *args, **kwargs):
        raise AssertionError('The storage tests must never reach the OS vault.')
    for name in ('get', 'set', 'delete', '_find'):
        monkeypatch.setattr(vault.Vault, name, refuse)


@pytest.fixture
def pebble(fake_home):
    opened = []
    layout = resolve(str(fake_home / '.pebble'), None, env={})
    data_home.ensure_layout(layout)
    report = data_home.migrate(layout)
    data_home.activate(layout, Path(report['data']), report)
    events = []
    # Merge (i22): App now starts its own storage service (s22 P1). These tests drive their own instance and its
    # first pass, so the App's background passes stay off; one writer, as in the product.
    original = DataServices.start
    DataServices.start = lambda self: None
    try:
        app = App(Path(report['data']), fake_home / 'resources', lambda *event: events.append(event))
    finally:
        DataServices.start = original
    services = DataServices(app, opener=opened.append)
    app.test_events, app.test_opened = events, opened
    yield app, services, fake_home / '.pebble' / 'settings.json'
    services.stop()
    app.close()


def read(path):
    return json.loads(path.read_text())


def edit(path, **changes):
    doc = read(path)
    for key, value in changes.items():
        doc[key] = value
    path.write_text(json.dumps(doc, indent=2))


def test_the_inventory_covers_every_person_setting_with_the_apps_validators():
    assert settings_file.KEYS == ('theme', 'reduced_motion', 'memory_enabled', 'memory_layers', 'permissions',
                                  'companion', 'notifications', 'stealth', 'storage')  # Phase 33: stealth
    for key in settings_file.KEYS:
        assert settings_file.validate(key, settings_file.default(key)) == settings_file.default(key)
    schema = settings_file.schema()
    assert set(settings_file.KEYS) <= set(schema['properties']) and schema['additionalProperties'] is True
    for key, bad in (('theme', 'purple'), ('reduced_motion', 'yes'), ('stealth', 'yes'), ('permissions', {'mode': 'god', 'tools': {}}),
                     ('memory_layers', {'working': True}), ('storage', {'documents_mirror': True}),
                     ('notifications', {}), ('companion', {'pointer': 1})):
        with pytest.raises(ValueError):
            settings_file.validate(key, bad)


def test_the_file_appears_after_startup_and_follows_the_apps_changes(pebble):
    app, services, path = pebble
    app.dispatch('setting', {'key': 'theme', 'value': 'dark'})
    assert not path.exists(), 'nothing is written before the first sync, so an offline edit is never overwritten'
    services.tick(launch=True)
    assert services.problems == {}, services.problems
    doc = read(path)
    assert doc['$schema'] == './settings.schema.json' and doc['theme'] == 'dark'
    assert doc['permissions'] == settings_file.validate('permissions', DEFAULT)
    assert list(doc)[1:] == list(settings_file.KEYS)
    app.dispatch('setting', {'key': 'reduced_motion', 'value': True})
    assert read(path)['reduced_motion'] is True, 'the app writes through at once'
    # App.setting('permissions') writes its row directly; the next pass brings it to the file.
    revised = {'mode': 'ask', 'tools': {**DEFAULT['tools'], 'web': False}}
    app.dispatch('setting', {'key': 'permissions', 'value': revised})
    services.tick()
    assert read(path)['permissions']['tools']['web'] is False
    assert services.settings.status()['ok'] is True


def test_a_narrowing_edit_is_applied_through_the_app(pebble):
    app, services, path = pebble
    services.tick(launch=True)
    version = app.store.setting('permissions_version', 0)
    doc = read(path)
    edit(path, permissions={'mode': 'ask', 'tools': {**doc['permissions']['tools'], 'terminal': False}},
         theme='light', memory_enabled=False)
    result = services.tick()
    assert result['settings']['state'] == 'applied'
    assert app.store.setting('permissions')['tools']['terminal'] is False
    assert app.store.setting('permissions_version') == version + 1, 'the same path as the Settings switch'
    assert app.store.setting('theme') == 'light' and app.store.setting('memory_enabled') is False
    assert ('settings', {}) in app.test_events
    assert services.settings.status()['pending'] == []


@pytest.mark.parametrize('key,widened,reason', [
    ('permissions', {'mode': 'full', 'tools': dict(DEFAULT['tools'])}, 'access changes from ask to full'),
    ('permissions', {'mode': 'ask', 'tools': {**DEFAULT['tools'], 'screen': True}}, 'the "screen" tool group turns on'),
    ('memory_layers', {**{k: True for k in settings_file.LAYERS}}, 'the "profile" memory layer turns on'),
    ('notifications', {'enabled': True, 'kinds': {'completed': True, 'failed': True, 'action_needed': True,
                                                  'monitor_changed': True}, 'preview': 'body', 'quiet_hours': None},
     'notifications turn on'),
    ('storage', {'documents_mirror': True, 'documents_folder': '~/Elsewhere'}, 'copies go to ~/Elsewhere'),
])
def test_a_widening_edit_waits_for_the_main_windows_confirmation(pebble, key, widened, reason):
    app, services, path = pebble
    if key == 'memory_layers':
        app.dispatch('setting', {'key': key, 'value': {**{k: True for k in settings_file.LAYERS}, 'profile': False}})
    services.tick(launch=True)
    before = settings_file.applied(app.store)[key]
    edit(path, **{key: widened})
    result = services.tick()
    assert settings_file.applied(app.store)[key] == before, 'not applied before the person confirms'
    pending = services.settings.status()['pending']
    assert [p['key'] for p in pending] == [key] and reason in pending[0]['reasons']
    assert ('storage', {'changed': 'settings_pending'}) in app.test_events, 'the windows learn that a change waits'
    assert result['settings']['pending'] == [pending[0]['id']]
    assert read(path)[key] == before, 'the file shows the setting in effect meanwhile'
    services.tick()
    assert [p['id'] for p in services.settings.status()['pending']] == [pending[0]['id']], 'stable while it waits'
    summary = services.methods()['storage_settings_resolve'](id=pending[0]['id'], accept=True)
    assert settings_file.applied(app.store)[key] == settings_file.validate(key, widened)
    assert summary['settings_file']['pending'] == [] and read(path)[key] == settings_file.validate(key, widened)


def test_a_dismissed_widening_is_dropped_and_a_stale_id_is_refused(pebble):
    app, services, path = pebble
    services.tick(launch=True)
    edit(path, memory_enabled=True)  # already on: no change at all
    services.tick()
    assert services.settings.status()['pending'] == []
    app.dispatch('setting', {'key': 'memory_enabled', 'value': False})
    edit(path, memory_enabled=True)
    services.tick()
    [item] = services.settings.status()['pending']
    services.methods()['storage_settings_resolve'](id=item['id'], accept=False)
    assert app.store.setting('memory_enabled') is False and services.settings.status()['pending'] == []
    with pytest.raises(ValueError, match='no longer waiting'):
        services.methods()['storage_settings_resolve'](id=item['id'], accept=True)
    assert app.store.setting('memory_enabled') is False


def test_an_invalid_file_keeps_the_last_good_settings_and_says_why(pebble):
    app, services, path = pebble
    services.tick(launch=True)
    broken = '{"theme": "light",\n  "reduced_motion": tru\n}\n'
    path.write_text(broken)
    services.tick()
    status = services.settings.status()
    assert status['ok'] is False and 'line 2' in status['error'] and app.store.setting('theme') is None
    assert ('storage', {'changed': 'settings_invalid'}) in app.test_events
    app.dispatch('setting', {'key': 'reduced_motion', 'value': True})
    assert path.read_text() == broken, "the person's broken file is theirs to fix; it is never overwritten"
    # A wrong value: nothing in the file applies, even a valid change beside it.
    edit_doc = settings_file.render(settings_file.applied(app.store))
    doc = json.loads(edit_doc)
    doc.update(theme='purple', reduced_motion=False)
    path.write_text(json.dumps(doc))
    services.tick()
    status = services.settings.status()
    assert status['problems'] == {'theme': 'theme is "system", "light" or "dark".'}
    assert app.store.setting('reduced_motion') is True
    # Duplicate keys and non-finite numbers are invalid JSON for Pebble.
    path.write_text('{"theme": "dark", "theme": "light"}')
    services.tick()
    assert 'appears twice' in services.settings.status()['error']
    doc['theme'] = 'dark'
    path.write_text(json.dumps(doc))
    services.tick()
    assert services.settings.status()['ok'] is True and app.store.setting('theme') == 'dark'
    assert app.store.setting('reduced_motion') is False


def test_unknown_keys_are_kept(pebble):
    app, services, path = pebble
    services.tick(launch=True)
    edit(path, my_note='keep me', zzz={'nested': [1, 2]})
    services.tick()
    app.dispatch('setting', {'key': 'theme', 'value': 'dark'})
    services.tick()
    doc = read(path)
    assert doc['my_note'] == 'keep me' and doc['zzz'] == {'nested': [1, 2]} and doc['theme'] == 'dark'
    assert services.settings.status()['unknown_keys'] == ['my_note', 'zzz']


def test_an_edit_made_while_pebble_was_closed_is_read_at_the_next_start(pebble, fake_home):
    app, services, path = pebble
    services.tick(launch=True)
    services.stop()
    app.close()
    edit(path, theme='dark', permissions={'mode': 'auto', 'tools': dict(DEFAULT['tools'])})
    data = fake_home / '.pebble' / 'data'
    # Phase 30 (parent): as in the fixture, this App's own background passes stay off and the test drives its pass.
    # With them on, the App's first background pass could apply the file before the assertion below (the pre-trial
    # full suite failed here once, and 8 of 25 isolated runs did); the product keeps one writer.
    original = DataServices.start
    DataServices.start = lambda self: None
    try:
        again = App(data, fake_home / 'resources', lambda *event: None)
    finally:
        DataServices.start = original
    try:
        assert again.store.setting('theme') is None, 'Store never applies the file during an upgrade startup'
        services = DataServices(again)
        services.tick(launch=True)
        assert again.store.setting('theme') == 'dark'
        assert again.store.setting('permissions')['mode'] == 'ask'
        assert [p['key'] for p in services.settings.status()['pending']] == ['permissions']
    finally:
        again.close()
    pebble[0].close = lambda: None  # the fixture's app is already closed


def test_a_change_waits_while_a_task_runs(pebble):
    app, services, path = pebble
    services.tick(launch=True)
    doc = read(path)
    edit(path, permissions={'mode': 'ask', 'tools': {**doc['permissions']['tools'], 'web': False}})
    app.active = 'a' * 32
    try:
        result = services.tick()
        assert result['settings']['state'] == 'waiting' and 'permissions' in result['settings']['waiting']
        assert app.store.setting('permissions')['tools']['web'] is True
        assert read(path)['permissions']['tools']['web'] is False, 'the edit is kept for the retry'
    finally:
        app.active = None
    services.tick()
    assert app.store.setting('permissions')['tools']['web'] is False


def test_the_sync_never_reads_the_database_while_holding_its_file_lock(pebble, monkeypatch):
    app, services, path = pebble
    sync = services.settings
    seen = []
    real = settings_file.snapshot

    def watched(store):
        seen.append(sync.lock._is_owned())
        return real(store)
    monkeypatch.setattr(settings_file, 'snapshot', watched)
    services.tick(launch=True)
    edit(path, theme='dark')
    services.tick()
    app.dispatch('setting', {'key': 'reduced_motion', 'value': True})
    assert seen and not any(seen)
    # A thread inside Store.lock writing a setting while another thread syncs: neither waits forever.
    done = []

    def writer():
        with app.store.lock:
            app.store.set_setting('theme', 'light')
        done.append('writer')
    threads = [threading.Thread(target=writer), threading.Thread(target=lambda: (services.tick(), done.append('sync')))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert sorted(done) == ['sync', 'writer']


def test_storage_settings_validate_paths(fake_home):
    ok = settings_file.validate('storage', {'documents_mirror': True, 'documents_folder': '~/Notes/Pebble'})
    assert ok['documents_folder'] == '~/Notes/Pebble'
    for folder, message in (('relative/path', 'full path'), (str(fake_home / '.pebble' / 'x'), 'internal'),
                            (str(fake_home / 'Library' / 'Pebble'), 'Library'), ('\x00', 'full folder path')):
        with pytest.raises(ValueError, match=message):
            settings_file.validate('storage', {'documents_mirror': True, 'documents_folder': folder})
    assert settings_file.widening('storage', {'documents_mirror': True, 'documents_folder': None},
                                  {'documents_mirror': False, 'documents_folder': None}) == []


def test_no_model_tool_reaches_storage_or_settings():
    from pebble.tool_catalogue import TOOLS
    names = {tool['function']['name'] if 'function' in tool else tool.get('name') for tool in TOOLS}
    assert not any('storage' in str(n).lower() or 'setting' in str(n).lower() for n in names)
