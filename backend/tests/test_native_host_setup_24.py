"""Phase 24 (t24-browser, V1-043): Connect Chrome from Pebble's main window.

The planner names exactly the two files HARNESS_BROWSER_EXTENSION.md §5 documents, writes them only with the digest
of the plan the person confirmed, never replaces a file Pebble did not write, and its status says whether the helper
is present or missing. Every path is under the test's own folder: the person's Library is never read or written.
"""

import importlib.util
import json
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import browser_bridge as bb
from pebble import native_host_setup as setup
from pebble.policy import PolicyError

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('install_native_host', ROOT / 'scripts/install_native_host.py')
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


@pytest.fixture
def mac(tmp_path):
    """A data home, a user home where Chrome has run once, and Pebble's backend executable."""
    data = tmp_path / 'home' / '.pebble' / 'data'
    data.mkdir(parents=True, mode=0o700)
    user = tmp_path / 'home'
    (user / 'Library/Application Support/Google/Chrome').mkdir(parents=True)
    backend = tmp_path / 'Applications/Pebble.app/Contents/Resources/resources/backend/pebble-backend'
    backend.parent.mkdir(parents=True)
    backend.write_text('#!/bin/sh\nexit 0\n')
    os.chmod(backend, 0o755)
    return SimpleNamespace(root=tmp_path, data=data, user=user, program=[backend],
                           launcher=data / 'chrome' / 'pebble-native-host', manifest=bb.host_manifest_path(user))


def files(root):
    return {p for p in Path(root).rglob('*') if p.is_file()}


def plan(env):
    return setup.plan(env.data, user_home=env.user, program=env.program)


def install(env, confirm):
    return setup.install(env.data, user_home=env.user, program=env.program, confirm=confirm)


def test_the_planner_lists_exactly_the_two_documented_files_and_writes_nothing(mac):
    before = files(mac.root)
    shown = plan(mac)
    assert [(f['role'], f['path'], f['mode'], f['state']) for f in shown['files']] == [
        ('launcher', str(mac.launcher), '0700', 'absent'),
        ('manifest', str(mac.user / 'Library/Application Support/Google/Chrome/NativeMessagingHosts/com.pebble.browser.json'),
         '0644', 'absent')]
    assert shown['ready'] is True and shown['installed'] is False and shown['problems'] == []
    assert shown['extension_id'] == bb.EXTENSION_ID and 'writes a third file' in shown['never']
    assert len(shown['plan_sha256']) == 64 and plan(mac)['plan_sha256'] == shown['plan_sha256'], 'stable'
    assert files(mac.root) == before, 'planning writes nothing'


def test_install_refuses_without_the_confirmed_plan_and_writes_nothing(mac):
    before = files(mac.root)
    for confirm in (None, '', 'yes', True, '0' * 64):
        with pytest.raises(setup.SetupRefused) as exc:
            install(mac, confirm)
        assert exc.value.code in ('confirm', 'plan_changed'), confirm
        assert 'Nothing was written' in str(exc.value) or 'nothing was written' in str(exc.value)
    assert isinstance(setup.SetupRefused('x', 'y'), PolicyError), 'the routes surface it as a refusal'
    # A plan shown for another backend path is not this plan.
    other = setup.plan(mac.data, user_home=mac.user, program=[mac.root / 'elsewhere'])['plan_sha256']
    with pytest.raises(setup.SetupRefused) as exc:
        install(mac, other)
    assert exc.value.code == 'plan_changed'
    assert files(mac.root) == before and not (mac.data / 'chrome').exists()


def test_one_confirmation_writes_exactly_the_two_files_and_status_reports_them(mac):
    before = files(mac.root)
    result = install(mac, plan(mac)['plan_sha256'])
    assert result['installed'] is True and result['written'] == [str(mac.launcher), str(mac.manifest)]
    assert files(mac.root) - before == {mac.launcher, mac.manifest}, 'nothing but the two files (no backup, no temp)'
    assert stat.S_IMODE(os.stat(mac.launcher).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(mac.launcher.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(mac.manifest).st_mode) == 0o644
    doc = json.loads(mac.manifest.read_text())
    assert doc == {'name': bb.HOST_NAME, 'description': setup.DESCRIPTION, 'path': str(mac.launcher), 'type': 'stdio',
                   'allowed_origins': [bb.EXTENSION_ORIGIN]}
    script = mac.launcher.read_text()
    assert script.startswith('#!/bin/sh\n# pebble.native-host/1')
    assert f'exec /usr/bin/env -i HOME="$HOME" PATH=/usr/bin:/bin {mac.program[0]} --browser-native-host --home ' \
           f'{mac.data} "$@"' in script
    # The bridge's own read-only check and the parent's script agree it is installed.
    assert bb.installed_hosts(mac.user)['chrome'] is True
    assert [f['state'] for f in plan(mac)['files']] == ['current', 'current']
    # A second confirmation of the current plan writes nothing.
    again = install(mac, plan(mac)['plan_sha256'])
    assert again['written'] == [] and again['installed'] is True and files(mac.root) - before == {mac.launcher, mac.manifest}
    # Missing again (the person removed the manifest): status says so.
    mac.manifest.unlink()
    missing = plan(mac)
    assert missing['installed'] is False and [f['state'] for f in missing['files']] == ['current', 'absent']


def test_the_script_and_the_app_build_the_same_bytes():
    layout = tool.Layout(None, home='/Users/p/.pebble/data', backend='/Applications/Pebble.app/x/pebble-backend')
    assert tool.launcher_text(layout) == setup.launcher_text([layout.backend], layout.data_home)
    assert tool.manifest_bytes(layout) == setup.manifest_bytes(layout.launcher)
    assert (tool.DESCRIPTION, tool.LAUNCHER_MARK) == (setup.DESCRIPTION, setup.LAUNCHER_MARK)


def test_a_file_pebble_did_not_write_is_never_replaced(mac):
    mac.manifest.parent.mkdir(parents=True)
    foreign = '{"name": "com.pebble.browser", "path": "/somewhere/else", "allowed_origins": ["chrome-extension://x/"]}'
    mac.manifest.write_text(foreign)
    shown = plan(mac)
    assert shown['files'][1]['state'] == 'foreign' and shown['ready'] is False
    assert shown['problems'][0]['code'] == 'not_pebbles' and str(mac.manifest) in shown['problems'][0]['message']
    with pytest.raises(setup.SetupRefused) as exc:
        install(mac, shown['plan_sha256'])
    assert exc.value.code == 'not_pebbles'
    assert mac.manifest.read_text() == foreign and not mac.launcher.exists(), 'nothing was written'
    # Pebble's own older manifest (another launcher path) is Pebble's to update.
    mac.manifest.write_text(json.dumps({**setup.manifest_doc('/old/home/chrome/pebble-native-host')}, indent=2))
    shown = plan(mac)
    assert shown['files'][1]['state'] == 'update' and shown['ready'] is True
    assert install(mac, shown['plan_sha256'])['installed'] is True
    assert json.loads(mac.manifest.read_text())['path'] == str(mac.launcher)


def test_what_stands_in_the_way_is_named_and_nothing_is_written(mac, tmp_path):
    (mac.user / 'Library/Application Support/Google/Chrome').rmdir()
    shown = plan(mac)
    assert [p['code'] for p in shown['problems']] == ['chrome_not_found'] and not shown['ready']
    with pytest.raises(setup.SetupRefused) as exc:
        install(mac, shown['plan_sha256'])
    assert exc.value.code == 'chrome_not_found' and not (mac.data / 'chrome').exists()
    (mac.user / 'Library/Application Support/Google/Chrome').mkdir()
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    os.symlink(elsewhere, mac.manifest.parent)
    shown = plan(mac)
    assert 'hosts_folder' in [p['code'] for p in shown['problems']]
    with pytest.raises(setup.SetupRefused):
        install(mac, shown['plan_sha256'])
    assert list(elsewhere.iterdir()) == []
    os.unlink(mac.manifest.parent)
    missing = setup.plan(mac.data, user_home=mac.user, program=[tmp_path / 'no-backend'])
    assert [p['code'] for p in missing['problems']] == ['no_backend']


ROUTES = ('browser_connect_status', 'browser_connect_install')
MAIN_RS = ROOT / 'src-tauri' / 'src' / 'main.rs'


def rust_list(source, pattern):
    match = re.search(pattern, source, re.S)
    assert match, pattern
    return set(re.findall(r'"([a-z_]+)"', match.group(1)))


def wired():
    from pebble import tool_authz
    source = MAIN_RS.read_text()
    return set(ROUTES) <= tool_authz.NATIVE_ONLY and set(ROUTES) <= rust_list(source, r'const ALLOWED: &\[&str\] = &\[(.*?)\];')


@pytest.mark.xfail(not wired(), strict=True, reason='parent patches P5 (tool_authz.NATIVE_ONLY) and P6 (main.rs ALLOWED)')
def test_the_connect_routes_are_main_window_only_and_never_model_tools():
    from pebble import tool_authz
    from pebble.tool_catalogue import TOOLS
    source = MAIN_RS.read_text()
    assert set(ROUTES) <= tool_authz.NATIVE_ONLY
    assert set(ROUTES) <= rust_list(source, r'const ALLOWED: &\[&str\] = &\[(.*?)\];')
    assert not set(ROUTES) & rust_list(source, r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains'), 'never the pet'
    assert not set(ROUTES) & rust_list(source, r'const NATIVE_ONLY: &\[&str\] = &\[(.*?)\];')
    assert not {t['function']['name'].casefold() for t in TOOLS} & set(ROUTES), 'no new model tool'


def test_on_the_real_app_a_flat_home_reports_and_writes_nothing(tmp_path):
    from pebble.app import App
    app = App(tmp_path / 'flat', tmp_path / 'resources', lambda *event: None)
    try:
        routes = setup.routes(app)
        assert routes['browser_connect_status']()['problems'] == [{'code': 'not_pebble_home', 'message': setup.NOT_HERE}]
        with pytest.raises(setup.SetupRefused):
            routes['browser_connect_install'](confirm='0' * 64)
        if wired():  # the parent applies P4 (App.dispatch) with P5 and P6
            assert app.dispatch('browser_connect_status', {})['available'] is False
            with pytest.raises(setup.SetupRefused):
                app.dispatch('browser_connect_install', {'confirm': '0' * 64})
    finally:
        app.close()
    assert not (tmp_path / 'flat' / 'chrome' / 'pebble-native-host').exists()


def test_the_routes_follow_pebbles_own_data_home_only(mac):
    flat = SimpleNamespace(storage=SimpleNamespace(active=False), home=mac.data)
    routes = setup.routes(flat, user_home=mac.user, program=mac.program)
    assert set(routes) == {'browser_connect_status', 'browser_connect_install'}
    status = routes['browser_connect_status']()
    assert status['available'] is False and status['problems'][0]['code'] == 'not_pebble_home' and status['files'] == []
    with pytest.raises(setup.SetupRefused) as exc:
        routes['browser_connect_install'](confirm=plan(mac)['plan_sha256'])
    assert exc.value.code == 'not_pebble_home' and not mac.launcher.exists()
    live = setup.routes(SimpleNamespace(storage=SimpleNamespace(active=True), home=mac.data), user_home=mac.user,
                        program=mac.program)
    shown = live['browser_connect_status']()
    assert shown['available'] is True and shown['installed'] is False
    with pytest.raises(setup.SetupRefused):
        live['browser_connect_install']()
    assert live['browser_connect_install'](confirm=shown['plan_sha256'])['installed'] is True
    assert live['browser_connect_status']()['installed'] is True
