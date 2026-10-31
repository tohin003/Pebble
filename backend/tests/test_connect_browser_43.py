"""Phase 43 (1.9.0, decision 8): Connect a browser. Pebble writes its connection file only for the browsers the person
ticks: the launcher plus one host manifest per ticked browser, in that browser's own NativeMessagingHosts folder, with
the one confirmed digest. Without a choice it is Chrome alone, exactly as Phase 24's Connect Chrome. Every path is under
the test's own folder: the person's Library is never read or written, and nothing inside a browser profile is listed.
"""

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import browser_bridge as bb
from pebble import native_host_setup as setup


@pytest.fixture
def mac(tmp_path):
    """A data home, a user home where Chrome and Comet have run (Brave has not), and Pebble's backend executable."""
    data = tmp_path / 'home' / '.pebble' / 'data'
    data.mkdir(parents=True, mode=0o700)
    user = tmp_path / 'home'
    for folder in ('Google/Chrome', 'Comet', 'Arc/User Data'):
        (user / 'Library/Application Support' / folder).mkdir(parents=True)
    backend = tmp_path / 'Applications/Pebble.app/Contents/Resources/resources/backend/pebble-backend'
    backend.parent.mkdir(parents=True)
    backend.write_text('#!/bin/sh\nexit 0\n')
    os.chmod(backend, 0o755)
    return SimpleNamespace(root=tmp_path, data=data, user=user, program=[backend],
                           launcher=data / 'chrome' / 'pebble-native-host')


def files(root):
    return {p for p in Path(root).rglob('*') if p.is_file()}


def manifest(mac, browser):
    return bb.host_manifest_path(mac.user, browser)


def test_every_browser_has_its_own_support_folder_and_the_chrome_family_map_is_unchanged():
    assert bb.BROWSER_FOLDERS['comet'] == 'Comet' and bb.BROWSER_FOLDERS['arc'] == 'Arc/User Data'
    assert bb.BROWSER_FOLDERS['brave'] == 'BraveSoftware/Brave-Browser' and bb.BROWSER_FOLDERS['edge'] == 'Microsoft Edge'
    assert {k: bb.BROWSER_FOLDERS[k] for k in bb.HOST_FOLDERS} == bb.HOST_FOLDERS, 'the same folders for the Chrome family'
    assert set(bb.BROWSER_NAMES) == set(bb.BROWSER_FOLDERS)
    assert bb.browser_id('comet') == 'comet' and bb.browser_id('safari') == 'unknown' and bb.browser_id(None) == 'unknown'


def test_without_a_choice_the_plan_is_chrome_alone_as_before(mac):
    shown = setup.plan(mac.data, user_home=mac.user, program=mac.program)
    assert [(f['role'], f['browser']) for f in shown['files']] == [('launcher', None), ('manifest', 'chrome')]
    assert shown['browser'] == 'chrome' and shown['browsers'] == ['chrome'] and shown['ready'] is True


def test_the_plan_names_one_manifest_per_ticked_browser_and_writes_nothing(mac):
    before = files(mac.root)
    shown = setup.plan(mac.data, user_home=mac.user, program=mac.program, browsers=['comet', 'chrome'])
    assert shown['browsers'] == ['chrome', 'comet'], 'in a fixed order, whatever the order ticked'
    assert [(f['role'], f['browser'], f['path']) for f in shown['files']] == [
        ('launcher', None, str(mac.launcher)), ('manifest', 'chrome', str(manifest(mac, 'chrome'))),
        ('manifest', 'comet', str(mac.user / 'Library/Application Support/Comet/NativeMessagingHosts/com.pebble.browser.json'))]
    # Phase 44 (D118): Comet alone also writes Chrome's file, where Comet reads (measured)
    alone = setup.plan(mac.data, user_home=mac.user, program=mac.program, browsers=['comet'])
    assert [(f['browser'], f['path']) for f in alone['files'][1:]] == [
        ('comet', str(manifest(mac, 'comet'))), ('comet', str(manifest(mac, 'chrome')))]
    assert shown['ready'] is True and files(mac.root) == before
    choices = {c['id']: c for c in shown['choices']}
    assert set(choices) == set(bb.BROWSER_FOLDERS)
    assert (choices['comet']['present'], choices['arc']['present'], choices['brave']['present']) == (True, True, False)
    assert not any(c['installed'] for c in choices.values())
    other = setup.plan(mac.data, user_home=mac.user, program=mac.program, browsers=['comet'])['plan_sha256']
    assert other != shown['plan_sha256'], 'a plan for other browsers is another plan'


def test_a_browser_that_never_ran_here_is_named_and_nothing_is_written(mac):
    shown = setup.plan(mac.data, user_home=mac.user, program=mac.program, browsers=['brave'])
    assert [p['code'] for p in shown['problems']] == ['browser_not_found'] and 'Brave' in shown['problems'][0]['message']
    with pytest.raises(setup.SetupRefused) as exc:
        setup.install(mac.data, user_home=mac.user, program=mac.program, confirm=shown['plan_sha256'], browsers=['brave'])
    assert exc.value.code == 'browser_not_found' and not mac.launcher.exists()


def test_one_confirmation_writes_exactly_the_ticked_browsers_files(mac):
    before = files(mac.root)
    shown = setup.plan(mac.data, user_home=mac.user, program=mac.program, browsers=['comet', 'arc'])
    with pytest.raises(setup.SetupRefused) as exc:  # the digest of another plan
        setup.install(mac.data, user_home=mac.user, program=mac.program, confirm=shown['plan_sha256'], browsers=['comet'])
    assert exc.value.code == 'plan_changed' and files(mac.root) == before
    done = setup.install(mac.data, user_home=mac.user, program=mac.program, confirm=shown['plan_sha256'],
                         browsers=['arc', 'comet'])
    comet, arc, chrome = manifest(mac, 'comet'), manifest(mac, 'arc'), manifest(mac, 'chrome')
    assert done['installed'] is True and done['written'] == [str(mac.launcher), str(comet), str(chrome), str(arc)]
    # Phase 44 (D118): Comet reads Chrome's folder (measured), so connecting Comet writes that file as well
    assert files(mac.root) - before == {mac.launcher, comet, chrome, arc}
    assert stat.S_IMODE(os.stat(comet).st_mode) == 0o644 and stat.S_IMODE(os.stat(mac.launcher).st_mode) == 0o700
    assert json.loads(comet.read_text()) == {'name': bb.HOST_NAME, 'description': setup.DESCRIPTION, 'path': str(mac.launcher),
                                             'type': 'stdio', 'allowed_origins': [bb.EXTENSION_ORIGIN]}
    hosts = bb.browser_hosts(mac.user)
    assert (hosts['comet']['host'], hosts['arc']['host'], hosts['chrome']['host']) == (True, True, True)
    assert (hosts['comet']['support'], hosts['brave']['support']) == (True, False)
    assert bb.installed_hosts(mac.user) == {'chrome': True, 'chrome-beta': False, 'chrome-canary': False, 'chromium': False}
    again = setup.plan(mac.data, user_home=mac.user, program=mac.program, browsers=['chrome', 'comet'])
    assert [f['state'] for f in again['files']] == ['current', 'current', 'current'], 'Chrome\'s file is already there'


def test_a_file_pebble_did_not_write_in_one_browser_stops_the_whole_install(mac):
    comet = manifest(mac, 'comet')
    comet.parent.mkdir(parents=True)
    comet.write_text('{"name": "com.pebble.browser", "path": "/somewhere/else"}')
    shown = setup.plan(mac.data, user_home=mac.user, program=mac.program, browsers=['chrome', 'comet'])
    assert shown['ready'] is False and shown['problems'][0]['code'] == 'not_pebbles'
    with pytest.raises(setup.SetupRefused):
        setup.install(mac.data, user_home=mac.user, program=mac.program, confirm=shown['plan_sha256'],
                      browsers=['chrome', 'comet'])
    assert not manifest(mac, 'chrome').exists() and comet.read_text().startswith('{"name": "com.pebble.browser", "path": "/somewhere')


def test_only_known_browsers_can_be_ticked(mac):
    for bad in ([], ['safari'], ['chrome', 'chrome'], 'chrome', [None]):
        with pytest.raises(setup.SetupRefused) as exc:
            setup.plan(mac.data, user_home=mac.user, program=mac.program, browsers=bad)
        assert exc.value.code == 'browsers', bad
    assert not mac.launcher.exists()


def test_the_routes_take_the_ticked_browsers(mac):
    live = setup.routes(SimpleNamespace(storage=SimpleNamespace(active=True), home=mac.data), user_home=mac.user,
                        program=mac.program)
    assert live['browser_connect_status']()['browsers'] == ['chrome'], 'the default is unchanged'
    shown = live['browser_connect_status'](browsers=['comet'])
    assert [f['browser'] for f in shown['files']] == [None, 'comet', 'comet']  # Phase 44: and Chrome's folder (D118)
    done = live['browser_connect_install'](confirm=shown['plan_sha256'], browsers=['comet'])
    assert done['installed'] is True and manifest(mac, 'comet').exists() and manifest(mac, 'chrome').exists()
    flat = setup.routes(SimpleNamespace(storage=SimpleNamespace(active=False), home=mac.data), user_home=mac.user,
                        program=mac.program)
    assert flat['browser_connect_status'](browsers=['comet'])['available'] is False
