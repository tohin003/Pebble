"""Phase 22 (b22-browser): scripts/install_native_host.py on a staged root only.

Every run uses --root under the test's own folder except the approval test, which
must refuse before anything is read. The person's Library is never read or written.
"""

import importlib.util
import io
import json
import os
import stat
from pathlib import Path

import pytest

from pebble import browser_bridge as bb

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('install_native_host', ROOT / 'scripts/install_native_host.py')
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
STAMP = '20261014T120000Z'


def run(*argv):
    out = io.StringIO()
    code = tool.main(list(argv), out=out, clock=lambda: 1_791_979_200.0)
    text = out.getvalue()
    return code, json.loads(text) if text else None  # argparse reports usage errors on stderr only


@pytest.fixture
def staged(tmp_path):
    root = tmp_path / 'root'
    home = root / 'home' / tool.DATA_HOME
    home.mkdir(parents=True, mode=0o700)
    backend = root / tool.BACKEND
    backend.parent.mkdir(parents=True)
    backend.write_text('#!/bin/sh\nexit 0\n')
    os.chmod(backend, 0o755)
    (root / 'home' / 'Library/Application Support/Google/Chrome').mkdir(parents=True)
    return root


def manifest_path(root, browser='chrome'):
    return bb.host_manifest_path(root / 'home', browser)


def test_plan_is_the_default_and_changes_nothing(staged):
    before = sorted(str(p) for p in staged.rglob('*'))
    code, report = run('--root', str(staged))
    assert code == tool.EXIT_OK and report['state'] == 'dry_run' and report['command'] == 'plan'
    assert [a['action'] for a in report['actions']] == ['ensure_private_folder', 'write_launcher', 'ensure_hosts_folder',
                                                         'write_manifest']
    assert sorted(str(p) for p in staged.rglob('*')) == before
    assert report['results']['manifest']['allowed_origins'] == [bb.EXTENSION_ORIGIN]
    assert set(report['never']) == {'edits Chrome preferences', 'installs or enables the extension', 'reads a credential'}


def test_7_4_6_the_host_manifest_lists_exactly_one_allowed_origin_on_the_dry_run(staged):
    code, report = run('plan', '--root', str(staged), '--browser', 'chrome')
    assert code == tool.EXIT_OK and report['state'] == 'dry_run'
    origins = report['results']['manifest']['allowed_origins']
    assert origins == ['chrome-extension://ilmkbniehiecgoodnbkkcjgaelebehbj/'] and len(origins) == 1
    assert json.loads(tool.manifest_bytes(tool.Layout(staged)))['allowed_origins'] == origins
    assert not any(path.name == 'com.pebble.browser.json' for path in staged.rglob('*')), 'nothing written'


def test_install_writes_the_launcher_and_manifest_and_reads_them_back(staged):
    code, report = run('install', '--root', str(staged), '--execute', '--stamp', STAMP)
    assert code == tool.EXIT_OK and report['state'] == 'done', report['error']
    path = manifest_path(staged)
    doc = json.loads(path.read_text())
    home = staged / 'home' / tool.DATA_HOME
    launcher = home / 'chrome' / 'pebble-native-host'
    assert doc == {'name': 'com.pebble.browser', 'description': tool.DESCRIPTION, 'path': str(launcher), 'type': 'stdio',
                   'allowed_origins': ['chrome-extension://ilmkbniehiecgoodnbkkcjgaelebehbj/']}
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert stat.S_IMODE(os.stat(launcher).st_mode) == 0o700 and stat.S_IMODE(os.stat(launcher.parent).st_mode) == 0o700
    script = launcher.read_text()
    assert script.startswith('#!/bin/sh\n# pebble.native-host/1')
    assert 'exec /usr/bin/env -i HOME="$HOME" PATH=/usr/bin:/bin' in script
    assert f"{staged / tool.BACKEND} --browser-native-host --home '{home}' \"$@\"" in script
    assert bb.installed_hosts(staged / 'home')['chrome'] is True
    again_code, again = run('install', '--root', str(staged), '--execute', '--stamp', '20261014T120001Z')
    assert again_code == tool.EXIT_OK and again['actions'] == [{'action': 'ensure_private_folder', 'path': str(launcher.parent)}]
    assert again['results']['unchanged'] == [str(path)], 'an identical install changes nothing'
    assert any('uninstall' in line for line in report['recovery'])


def test_a_different_manifest_or_launcher_is_backed_up_before_it_is_replaced(staged):
    path = manifest_path(staged)
    path.parent.mkdir(parents=True)
    path.write_text('{"name": "com.pebble.browser", "path": "/old/host", "allowed_origins": ["chrome-extension://old/"]}')
    code, report = run('install', '--root', str(staged), '--execute', '--stamp', STAMP)
    assert code == tool.EXIT_OK, report['error']
    backups = report['results']['backups']
    assert len(backups) == 1 and Path(backups[0]['backup']).read_text().startswith('{"name": "com.pebble.browser", "path": "/old')
    assert Path(backups[0]['backup']).parent.name == STAMP
    code, report = run('install', '--root', str(staged), '--execute', '--stamp', STAMP, '--backend',
                       str(staged / 'Applications/Other.app/pebble-backend'))
    assert code == tool.EXIT_REFUSED, 'a backend outside the staged layout or missing refuses'


def test_uninstall_removes_only_what_it_wrote_and_backs_it_up(staged):
    assert run('install', '--root', str(staged), '--execute', '--stamp', STAMP)[0] == tool.EXIT_OK
    code, report = run('uninstall', '--root', str(staged), '--stamp', '20261014T130000Z')
    assert code == tool.EXIT_OK and report['state'] == 'dry_run' and manifest_path(staged).exists()
    code, report = run('uninstall', '--root', str(staged), '--execute', '--stamp', '20261014T130000Z')
    assert code == tool.EXIT_OK and report['state'] == 'done', report['error']
    assert not manifest_path(staged).exists()
    assert not (staged / 'home' / tool.DATA_HOME / 'chrome/pebble-native-host').exists()
    assert len(report['results']['backups']) == 1
    foreign = manifest_path(staged)
    foreign.write_text('{"name": "com.pebble.browser", "path": "/somewhere/else"}')
    code, report = run('uninstall', '--root', str(staged), '--execute', '--stamp', '20261014T140000Z')
    assert code == tool.EXIT_REFUSED and report['error']['code'] == 'manifest_is_ours'
    assert foreign.read_text() == '{"name": "com.pebble.browser", "path": "/somewhere/else"}', 'never removed'


def test_links_and_missing_folders_refuse_before_any_change(staged, tmp_path):
    hosts = manifest_path(staged).parent
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    os.symlink(elsewhere, hosts)
    code, report = run('install', '--root', str(staged), '--execute', '--stamp', STAMP)
    assert code == tool.EXIT_REFUSED and report['error']['code'] == 'staged_path_has_no_link'
    assert list(elsewhere.iterdir()) == []
    os.unlink(hosts)
    code, report = run('install', '--root', str(staged), '--execute', '--stamp', STAMP, '--browser', 'chromium')
    assert code == tool.EXIT_REFUSED and report['error']['code'] == 'browser_support_exists'
    assert not (staged / 'home' / tool.DATA_HOME / 'chrome').exists(), 'refused before the first change'


def test_real_paths_need_the_parents_approval_before_anything_is_read(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('nothing may be read before the approval holds')
    monkeypatch.setattr(tool, '_read_small', forbidden)
    monkeypatch.setattr(tool.os, 'lstat', forbidden)
    monkeypatch.setattr(tool.os.path, 'islink', forbidden)
    code, report = run('install', '--execute')
    assert code == tool.EXIT_REFUSED and report['error']['code'] == 'stamp_given'
    code, report = run('install', '--execute', '--stamp', STAMP)
    assert code == tool.EXIT_REFUSED and report['error']['code'] == 'parent_approval'
    code, report = run('uninstall', '--execute', '--stamp', STAMP, '--i-am-the-parent-with-user-approval', '20261014T120001Z')
    assert code == tool.EXIT_REFUSED and report['error']['code'] == 'parent_approval'
    assert report['actions'] == []


def test_status_reports_each_browser_and_the_extension_folder(staged):
    code, report = run('status', '--root', str(staged))
    assert code == tool.EXIT_OK and report['results']['manifests']['chrome'] == {'state': 'absent'}
    assert report['results']['extension_folder'].endswith('browser-extension')
    run('install', '--root', str(staged), '--execute', '--stamp', STAMP)
    assert run('status', '--root', str(staged))[1]['results']['manifests']['chrome'] == {'state': 'installed'}


def test_usage_errors(staged):
    assert run('install', '--stamp', 'yesterday')[0] == tool.EXIT_USAGE
    assert run('install', '--root', str(staged), '--dev-python', '/usr/bin/python3')[0] == tool.EXIT_USAGE
    assert run('frobnicate')[0] == tool.EXIT_USAGE


def test_the_development_launcher_runs_main_py_in_host_mode(staged):
    # Running it end to end needs the parent's main.py patch P3 (--browser-native-host); this pins its command line.
    python = ROOT / '.runtime/agent-env/bin/python'
    code, report = run('install', '--root', str(staged), '--execute', '--stamp', STAMP, '--dev-python', str(python),
                       '--dev-source', str(ROOT))
    assert code == tool.EXIT_OK, report['error']
    launcher = staged / 'home' / tool.DATA_HOME / 'chrome/pebble-native-host'
    # The launcher shell-quotes each path (the checkout's own path has spaces and parentheses); same exact command.
    import shlex
    main = ROOT / 'backend/main.py'
    assert f"{shlex.quote(str(python))} -E -s {shlex.quote(str(main))} --browser-native-host" in launcher.read_text()
