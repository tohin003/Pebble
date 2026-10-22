"""Phase 24 (t24-install): scripts/pebble_install.py for the 1.0 install over 0.6.x, on staged roots only.

V1-006: one-command rollback of a backup taken while ~/.pebble was live. V1-044: drill --source-pebble-home and the
runbook that emits it. V1-137: the read-only backups listing, the new root names (old ones still load) and the reuse
of an unchanged frozen old folder. V1-061: the read-only purge listing. V1-173: no Phase 21 names in what the tool
writes. test_pebble_install's guards apply here too: Path.home() is an empty sentinel the tool never consults on a
staged root, the OS vault raises, and every effect is the in-process fake.
"""
import hashlib
import io
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

import test_pebble_install as tpi
from pebble import browser_bridge, migrations
from test_pebble_install import no_vault, real_home, stage  # noqa: F401  (autouse guards and the staged Mac)

tool = tpi.tool
STAMP, LATER, OLDER = tpi.STAMP, '20261013T130000Z', '20261012T120000Z'
MOVING = ('--data-layout', 'pebble-home')


def pebble(stage):
    return stage.root / 'home/.pebble'


def root_of(stage, stamp, suffix='-pre-install.noindex'):
    return stage.root / 'home/PebbleBackups' / (stamp + suffix)


def mac_06(fakes, stage):
    """A Mac that ran 0.6.x: the Phase 22 shell moved the old data home into ~/.pebble/data on its first launch; the
    old folder holds MOVED.md and pebble-moved.json, and ~/.pebble/runtime/mirror.json is the Documents copy index."""
    tpi.first_launch_moving(fakes, stage)
    fakes.launches.clear()
    assert (stage.home / 'MOVED.md').exists() and (pebble(stage) / 'runtime/mirror.json').exists()


def backup(fakes, stage, stamp=STAMP, execute=True):
    return tpi.call(fakes, 'backup', '--root', stage.root, '--stamp', stamp, '--expect-backend-sha256', tpi.OLD_SHA,
                    *(['--execute'] if execute else []))


def without_index(top):
    found = tpi.tree(top)
    found.pop('runtime/mirror.json', None)
    return found


# ---- V1-006: the post-move rollback ------------------------------------------------------------------------------


def test_a_0_6_mac_rolls_back_in_one_command(stage):
    fakes = tpi.Fakes()
    mac_06(fakes, stage)
    old_folder = tpi.snapshot(stage.home)  # with mtimes: the old folder must stay exactly as it is
    app, web = tpi.tree(stage.app), tpi.tree(stage.web)
    code, report = backup(fakes, stage)
    assert code == 0 and report['results']['live_home'] == 'pebble_home', report['error']
    B = stage.backup
    copy = tpi.tree(B / 'pebble-home')
    assert copy == tpi.tree(pebble(stage))
    assert tpi.install(fakes, stage)[0] == 0
    # 1.0's first launch opens ~/.pebble/data in place, then changes ~/.pebble as any newer version would.
    tpi.first_launch_moving(fakes, stage)
    tpi.put(pebble(stage) / 'data/exports/made-in-1.0.md', b'made after the install', 0o600)
    tpi.put(pebble(stage) / 'runtime/mirror.json', b'{"format": "a newer index"}', 0o600)
    upgraded = tpi.tree(pebble(stage))

    before = tpi.snapshot(stage.tmp)
    code, report = tpi.call(fakes, 'rollback', '--root', stage.root, '--stamp', STAMP)  # dry run
    assert code == 0 and report['state'] == 'dry_run' and tpi.snapshot(stage.tmp) == before, report['error']
    ops = [(action['op'], action.get('source'), action.get('destination')) for action in report['actions']]
    assert ('ditto', str(B / 'pebble-home'), str(pebble(stage))) in ops
    assert ('ditto', str(B / 'Pebble.app.backup'), str(stage.app)) in ops

    code, report = tpi.call(fakes, 'rollback', '--root', stage.root, '--stamp', STAMP, '--execute')
    assert code == 0 and report['results']['passed'] is True, report['error']
    results = report['results']
    # ~/.pebble is the backup's byte for byte, apart from its Documents copy index, moved aside into B.
    expected = dict(copy)
    index = expected.pop('runtime/mirror.json')
    assert tpi.tree(pebble(stage)) == expected
    aside = Path(results['mirror_index'])
    assert aside.parent == B and hashlib.sha256(aside.read_bytes()).hexdigest() == index[3]
    # The post-install ~/.pebble is kept beside the backup; the old folder, app and webview store are as backed up.
    assert tpi.tree(Path(results['after_pebble_home'])) == upgraded
    assert tpi.snapshot(stage.home) == old_folder and results['old_folder']['state'] == 'left untouched'
    assert tpi.tree(stage.app) == app and tpi.tree(stage.web) == web
    assert tpi.tree(Path(results['failed_bundle'])) == tpi.tree(stage.candidate)
    assert '--data-layout pebble-home' in results['next'] and 'never read' in results['documents_index']
    kept = json.loads(Path(report['record']['path']).read_text())
    assert kept['ok'] is True and Path(report['record']['path']).name.startswith('rollback-')

    # The user opens the restored app: one shell and one backend from it, ~/.pebble/data at the backed-up rows.
    tpi.first_launch_moving(fakes, stage)
    fakes.rows = [(1, str(stage.app / tool.SHELL)), (2, str(stage.app / tool.BACKEND))]
    code, report = tpi.readback(fakes, stage, '--mode', 'rollback')  # follows the backup's live home by itself
    assert code == 0 and report['results']['passed'] is True, report['error']
    assert report['results']['restored_home'] == 'pebble_home' and report['results']['move']['state'] == 'current'
    assert report['results']['inspect']['database']['user_version'] == migrations.SCHEMA_EPOCH
    assert report['results']['logs']['pebble:backend.log']['scanned'] is True
    tpi.put(stage.home / 'surprise.txt', b'the old folder changed')
    code, report = tpi.readback(fakes, stage, '--mode', 'rollback', *MOVING)
    assert report['error']['code'] == 'old_folder_intact'


def test_a_post_move_rollback_refuses_a_changed_backup_before_moving_anything(stage):
    fakes = tpi.Fakes()
    mac_06(fakes, stage)
    assert backup(fakes, stage)[0] == 0 and tpi.install(fakes, stage)[0] == 0
    tpi.put(stage.backup / 'pebble-home/settings.json', b'{"changed": true}', 0o600)
    before = tpi.snapshot(stage.tmp)
    code, report = tpi.call(fakes, 'rollback', '--root', stage.root, '--stamp', STAMP, '--execute')
    assert code == 3 and report['error']['code'] == 'backup_pebble_home_intact'
    assert report['error']['diff']['changed'] == ['settings.json'] and tpi.snapshot(stage.tmp) == before


def test_a_post_move_rollback_never_needs_the_old_folder_copy(stage):
    fakes = tpi.Fakes()
    mac_06(fakes, stage)
    assert backup(fakes, stage)[0] == 0 and tpi.install(fakes, stage)[0] == 0
    os.rename(stage.backup / 'com.pebble.local', stage.tmp / 'released-old-folder-copy')  # the person released it
    fakes.rows = [(1, str(stage.app / tool.SHELL))]
    code, report = tpi.call(fakes, 'rollback', '--root', stage.root, '--stamp', STAMP, '--execute')
    assert code == 3 and report['error']['code'] == 'pebble_not_running'  # still never signals: the user quits
    fakes.rows = []
    code, report = tpi.call(fakes, 'rollback', '--root', stage.root, '--stamp', STAMP, '--execute')
    assert code == 0 and report['results']['passed'] is True, report['error']


# ---- V1-044: the drill stages ~/.pebble --------------------------------------------------------------------------


def drill_06(fakes, stage, root, *sources, execute=True):
    return tpi.call(fakes, 'drill', '--root', root, '--source-app', stage.backup / 'Pebble.app.backup', *sources,
                    '--source-webview', stage.backup / 'webview-store', '--candidate', stage.candidate, '--app-manifest',
                    stage.manifest, '--expect-backend-sha256', tpi.OLD_SHA, *MOVING, *(['--execute'] if execute else []))


def test_the_drill_opens_dot_pebble_in_place_and_its_rollback_passes(stage):
    fakes = tpi.Fakes()
    mac_06(fakes, stage)
    assert backup(fakes, stage)[0] == 0
    B, root = stage.backup, stage.tmp / 'drill'
    code, report = drill_06(fakes, stage, root, '--source-pebble-home', B / 'pebble-home', '--source-home',
                            B / 'com.pebble.local')
    assert code == 0 and report['results']['passed'] is True, report['error']
    results = report['results']
    # The candidate opened ~/.pebble/data in place (state current), launched as the shell does.
    assert results['first_launch']['move']['state'] == 'current' and results['live_home'] == 'pebble_home'
    [spec] = fakes.launches
    assert spec['home'] == str(root / 'home/.pebble')
    assert spec['legacy_home'] == str(root / 'home/Library/Application Support/com.pebble.local')
    rollback = json.loads(Path(results['steps']['rollback']['record']['path']).read_text())
    assert rollback['results']['data_layout'] == 'pebble-home' and rollback['results']['passed'] is True
    checks = {check['check']: check['ok'] for check in report['checks']}
    assert checks['drill_pebble_home_restored'] and checks['drill_mirror_index_reset'] and checks['drill_data_restored']
    # Independent of the tool's manifests: the staged Mac is as it was, apart from the moved-aside copy index.
    assert without_index(root / 'home/.pebble') == without_index(B / 'pebble-home')
    assert not (root / 'home/.pebble/runtime/mirror.json').exists()
    assert tpi.tree(root / 'home/Library/Application Support/com.pebble.local') == tpi.tree(B / 'com.pebble.local')
    assert tpi.tree(root / 'Applications/Pebble.app') == tpi.tree(B / 'Pebble.app.backup')
    assert results['restored']['pebble_home'] is True and results['restored']['pebble_tree_sha256']
    assert json.loads((root / 'drill-report.json').read_text())['ok'] is True


def test_a_moved_source_home_is_refused_without_source_pebble_home(stage):
    fakes = tpi.Fakes()
    mac_06(fakes, stage)
    assert backup(fakes, stage)[0] == 0
    B = stage.backup
    # The runbook's 0.6.1 drill (the frozen old folder) and the Phase 23 workaround (a copy of ~/.pebble/data).
    for source, marks in ((B / 'com.pebble.local', ['MOVED.md', 'pebble-moved.json']),
                          (B / 'pebble-home/data', ['.pebble-moved-here.json'])):
        root, copies = stage.tmp / ('drill-' + source.name), len(fakes.copies)
        code, report = drill_06(fakes, stage, root, '--source-home', source)
        assert code == 3 and report['error']['code'] == 'drill_source_not_moved', report['error']
        assert report['error']['found'] == marks and not root.exists() and len(fakes.copies) == copies
    code, report = tpi.call(fakes, 'drill', '--root', stage.tmp / 'drill-x', '--source-app', B / 'Pebble.app.backup',
                            '--source-pebble-home', B / 'pebble-home', '--candidate', stage.candidate, '--app-manifest',
                            stage.manifest, '--expect-backend-sha256', tpi.OLD_SHA, '--execute')
    assert code == 3 and report['error']['code'] == 'drill_pebble_home_layout'
    code, report = drill_06(fakes, stage, stage.tmp / 'drill-y', '--source-pebble-home', B / 'pebble-home',
                            '--source-home', B / 'pebble-home/data')
    assert code == 3 and report['error']['code'] == 'drill_source_pebble_home_live'


def runbook_steps(fakes, stage):
    code, report = tpi.call(fakes, 'plan', '--root', stage.root, '--stamp', STAMP, '--expect-backend-sha256',
                            tpi.OLD_SHA, '--candidate', stage.candidate, '--app-manifest', stage.manifest)
    assert code == 0, report['error']
    steps = report['results']['steps']
    drill = next(step for step in steps if step['step'].startswith('drill'))
    rollback = next(step for step in steps if step['step'].startswith('only on a readback failure'))
    return report['results']['runbook_basis'], shlex.split(drill['commands'][1]), rollback


def test_the_runbook_drills_dot_pebble_when_it_is_the_live_home(stage):
    fakes = tpi.Fakes()
    basis, argv, rollback = runbook_steps(fakes, stage)  # an old-folder Mac: the drill moves the old home
    assert basis == {'live_home': 'legacy', 'from': 'this Mac now'} and '--source-pebble-home' not in argv
    mac_06(fakes, stage)
    basis, argv, rollback = runbook_steps(fakes, stage)  # before the backup: what this Mac shows
    assert basis == {'live_home': 'pebble_home', 'from': 'this Mac now'} and '--source-pebble-home' in argv
    assert backup(fakes, stage)[0] == 0
    basis, argv, rollback = runbook_steps(fakes, stage)
    assert basis == {'live_home': 'pebble_home', 'from': 'backup.json'}
    B = stage.backup
    assert argv[argv.index('--source-pebble-home') + 1] == str(B / 'pebble-home')
    assert argv[argv.index('--source-home') + 1] == str(B / 'com.pebble.local')
    assert argv[argv.index('--source-app') + 1] == str(B / 'Pebble.app.backup')
    assert argv[argv.index('--data-layout') + 1] == 'pebble-home' and argv[-1] == '--execute'
    assert argv[argv.index('--root') + 1] == '/private/tmp/pebble-drill-' + STAMP  # V1-173: no Phase 21 names
    assert 'restores the old app, ~/.pebble' in rollback['step']
    assert shlex.split(rollback['commands'][2])[-5:-3] == ['--data-layout', 'pebble-home']  # then --root R --execute
    # The emitted drill, run as written (on a test root instead of /private/tmp), passes.
    argv[argv.index('--root') + 1] = str(stage.tmp / 'runbook-drill')
    code, report = tpi.call(fakes, *argv[2:])
    assert code == 0 and report['results']['first_launch']['move']['state'] == 'current', report['error']


# ---- V1-137: backups listing, names, and the frozen old folder -------------------------------------------------


def as_phase23_root(stage, stamp):
    """Rename a fresh root to what the 0.6.1 tool wrote: '<stamp>-pre-phase21' with its bundle as 'Pebble.app'."""
    new, old = root_of(stage, stamp), root_of(stage, stamp, '-pre-phase21')
    os.rename(new, old)
    os.rename(old / 'Pebble.app.backup', old / 'Pebble.app')
    doc = json.loads((old / 'backup.json').read_text())
    doc['app']['copy'] = 'Pebble.app'
    (old / 'backup.json').write_text(json.dumps(doc))
    return old


def test_backups_lists_every_root_and_changes_nothing(stage):
    fakes = tpi.Fakes()
    assert backup(fakes, stage, OLDER)[0] == 0  # an old-folder backup, as Phase 21-23 named it
    old = as_phase23_root(stage, OLDER)
    mac_06(fakes, stage)
    assert backup(fakes, stage)[0] == 0
    assert backup(fakes, stage, LATER)[0] == 0
    before, copies = tpi.snapshot(stage.tmp), len(fakes.copies)
    code, report = tpi.call(fakes, 'backups', '--root', stage.root)
    assert code == 0 and report['state'] == 'dry_run' and tpi.snapshot(stage.tmp) == before, report['error']
    assert 'record' not in report and len(fakes.copies) == copies
    entries = {entry['stamp']: entry for entry in report['results']['backups']}
    assert [entry['stamp'] for entry in report['results']['backups']] == [LATER, STAMP, OLDER]  # newest first
    legacy, moved, later = entries[OLDER], entries[STAMP], entries[LATER]
    assert (legacy['naming'], legacy['launchable_bundles'], legacy['indexed_by_spotlight']) == (
        'pre-phase21', ['Pebble.app'], True)
    assert (moved['naming'], moved['launchable_bundles'], moved['indexed_by_spotlight']) == ('pre-install', [], False)
    assert legacy['live_home'] == 'legacy' and moved['live_home'] == later['live_home'] == 'pebble_home'
    assert legacy['versions'] == {'backed_up': '0.4.2', 'installed_after': None}
    for entry in entries.values():
        assert entry['record'] == 'complete' and entry['intact'] is True and entry['bytes'] > 0
        assert entry['rollback']['capable'] is True and entry['private'] is True
    assert legacy['rollback']['restores'] == ['app', 'data_home', 'webview']
    assert moved['rollback']['restores'] == ['app', 'pebble_home', 'webview']
    # The later backup reused the frozen old folder's verified copy instead of copying it again.
    assert later['copies']['data_home']['reused_from'] == moved['root'] and moved['old_folder_copy_used_by'] == [
        later['root']]
    assert report['results']['total_bytes'] == sum(entry['bytes'] for entry in entries.values())
    # A changed copy is reported (shape now; a same-size change needs --verify) and the rollback is no longer offered.
    tpi.put(old / 'com.pebble.local/runtime.key', b'y' * 32, 0o600)
    code, report = tpi.call(fakes, 'backups', '--root', stage.root)
    legacy = next(entry for entry in report['results']['backups'] if entry['stamp'] == OLDER)
    assert legacy['intact'] is True
    code, report = tpi.call(fakes, 'backups', '--root', stage.root, '--verify')
    legacy = next(entry for entry in report['results']['backups'] if entry['stamp'] == OLDER)
    assert legacy['intact'] is False and legacy['rollback'] == {**legacy['rollback'], 'capable': False,
                                                                 'missing_or_changed': ['data_home']}
    # Read-only: there is no --execute, and perform refuses one.
    assert tool.main(['backups', '--root', str(stage.root), '--execute'], env=fakes.env(), out=io.StringIO()) == 2
    report, code = tool.perform('backups', tool.Layout.make(stage.root), fakes.env(), True, dict(tool.OPTION_DEFAULTS))
    assert code == 3 and report['error']['code'] == 'read_only_command'


def test_new_and_old_backup_names_both_load(stage):
    fakes = tpi.Fakes()
    assert backup(fakes, stage, OLDER)[0] == 0
    old = as_phase23_root(stage, OLDER)
    assert backup(fakes, stage)[0] == 0
    for stamp, root, bundle in ((OLDER, old, 'Pebble.app'), (STAMP, stage.backup, 'Pebble.app.backup')):
        code, report = tpi.call(fakes, 'rollback', '--root', stage.root, '--stamp', stamp)
        assert code == 0 and report['state'] == 'dry_run', report['error']
        assert report['results']['bundle_source'] == str(root / bundle)
    # A stamp under both names is ambiguous and refused; a new backup never takes an old root's stamp.
    shutil.copytree(old, root_of(stage, OLDER), symlinks=True)  # test fixture only
    code, report = tpi.call(fakes, 'rollback', '--root', stage.root, '--stamp', OLDER)
    assert code == 3 and report['error']['code'] == 'backup_root_unique'
    code, report = backup(fakes, stage, OLDER)
    assert code == 3 and report['error']['code'] == 'backup_root_absent'


def test_an_unchanged_frozen_old_folder_is_not_copied_again(stage):
    fakes = tpi.Fakes()
    mac_06(fakes, stage)
    assert backup(fakes, stage)[0] == 0
    first = stage.backup
    code, report = backup(fakes, stage, LATER, execute=False)
    assert code == 0 and report['results']['old_folder'] == {'copy': 'reused', 'from': first.name}, report['error']
    sources = len(fakes.copies)
    code, report = backup(fakes, stage, LATER)
    assert code == 0 and report['results']['old_folder_reused_from'] == first.name, report['error']
    assert str(stage.home) not in [source for source, _ in fakes.copies[sources:]]  # the old folder was not copied
    later = root_of(stage, LATER)
    doc = json.loads((later / 'backup.json').read_text())
    assert not (later / 'com.pebble.local').exists() and doc['data_home']['copy'] is None
    assert doc['data_home']['reused'] == {'stamp': STAMP, 'root': first.name,
                                          'backup_json_sha256': hashlib.sha256((first / 'backup.json').read_bytes()).hexdigest()}
    assert doc['data_home']['tree_sha256'] == json.loads((first / 'backup.json').read_text())['data_home']['tree_sha256']
    # The later backup rehearses from the earlier root's copy and installs as usual.
    code, report = tpi.call(fakes, 'rehearse', '--root', stage.root, '--stamp', LATER, '--candidate', stage.candidate,
                            '--app-manifest', stage.manifest, '--rehearsal-dir', stage.tmp / 'rehearsal', *MOVING,
                            '--execute')
    assert code == 0 and report['results']['move']['state'] == 'current', report['error']
    assert tpi.tree(stage.tmp / 'rehearsal/home') == tpi.tree(first / 'com.pebble.local')
    # An old folder that changed is copied again.
    tpi.put(stage.home / 'note.txt', b'written by hand', 0o600)
    code, report = backup(fakes, stage, '20261013T140000Z')
    assert code == 0 and report['results']['old_folder_reused_from'] is None
    assert (root_of(stage, '20261013T140000Z') / 'com.pebble.local/note.txt').exists()


def test_an_old_folder_that_was_never_moved_is_always_copied(stage):
    fakes = tpi.Fakes()
    assert backup(fakes, stage)[0] == 0
    assert backup(fakes, stage, LATER)[0] == 0  # the old folder is still the live home: never reused
    assert (root_of(stage, LATER) / 'com.pebble.local').is_dir()


# ---- V1-061: purge lists, never deletes ---------------------------------------------------------------------------


def test_purge_lists_everything_a_full_removal_touches_and_changes_nothing(stage):
    fakes = tpi.Fakes()
    mac_06(fakes, stage)
    assert backup(fakes, stage)[0] == 0
    home = stage.root / 'home'
    documents = tpi.put(home / 'Documents/Pebble/Conversations/a.md', b'# a conversation').parents[1]
    host = tpi.put(home / 'Library/Application Support/Google/Chrome/NativeMessagingHosts/com.pebble.browser.json', b'{}')
    keychain = tpi.put(home / 'Library/Keychains/pebble-signing.keychain-db', b'keychain')
    scratch = tpi.put(stage.root / 'pebble-drill-20261001T000000Z/marker', b'x').parent
    before = tpi.snapshot(stage.tmp)
    code, report = tpi.call(fakes, 'purge', '--root', stage.root)
    assert code == 0 and report['state'] == 'dry_run' and tpi.snapshot(stage.tmp) == before, report['error']
    assert 'record' not in report and report['results']['deletes'] == 'nothing: this command only lists'
    items = {item['path']: item for item in report['results']['items']}
    for path in (stage.app, pebble(stage), stage.home, stage.web, documents, home / 'PebbleBackups', host, keychain,
                 scratch):
        assert items[str(path)]['exists'] is True and items[str(path)]['bytes'] > 0, path
    assert items[str(home / 'Library/Caches/com.pebble.local')]['exists'] is False
    services = [item['service'] for item in report['results']['manual'] if item['kind'] == 'keychain_item']
    assert 'com.pebble.local.vault' in services and all(
        item['read'] is False for item in report['results']['manual'] if item['kind'] == 'keychain_item')
    assert report['results']['total_bytes'] >= items[str(pebble(stage))]['bytes']
    assert tool.main(['purge', '--root', str(stage.root), '--execute'], env=fakes.env(), out=io.StringIO()) == 2
    report, code = tool.perform('purge', tool.Layout.make(stage.root), fakes.env(), True, dict(tool.OPTION_DEFAULTS))
    assert code == 3 and report['error']['code'] == 'read_only_command'


def test_purge_names_the_same_chrome_host_as_the_app():
    assert tool.HOST_NAME == browser_bridge.HOST_NAME
    assert tool.HOST_FOLDERS == tuple(browser_bridge.HOST_FOLDERS.values())


# ---- Safety properties for the new paths --------------------------------------------------------------------------


def test_the_new_paths_never_delete_replace_or_signal(stage, monkeypatch):
    calls = []

    def guard(owner, name):
        original = getattr(owner, name)

        def wrapper(*args, **kwargs):
            if sys._getframe(1).f_code.co_filename == str(tpi.SCRIPT):
                calls.append(name)
            return original(*args, **kwargs)
        monkeypatch.setattr(owner, name, wrapper)
    for owner, names in ((os, ('remove', 'unlink', 'rmdir', 'removedirs', 'replace', 'truncate', 'kill', 'killpg')),
                         (shutil, ('rmtree', 'move')), (Path, ('unlink', 'rmdir', 'replace'))):
        for name in names:
            guard(owner, name)
    fakes = tpi.Fakes()
    mac_06(fakes, stage)
    assert backup(fakes, stage)[0] == 0 and backup(fakes, stage, LATER)[0] == 0
    assert drill_06(fakes, stage, stage.tmp / 'drill', '--source-pebble-home', stage.backup / 'pebble-home')[0] == 0
    assert tpi.install(fakes, stage)[0] == 0
    assert tpi.call(fakes, 'rollback', '--root', stage.root, '--stamp', STAMP, '--execute')[0] == 0
    assert tpi.call(fakes, 'backups', '--root', stage.root, '--verify')[0] == 0
    assert tpi.call(fakes, 'purge', '--root', stage.root)[0] == 0
    assert calls == []


def test_the_handled_failure_pattern_matches_the_timestamped_log_line():
    # t24-core keeps 'ERROR:root:Request failed: <method>' as the line's start and appends a timestamp at its end.
    line = b'ERROR:root:Request failed: state 2026-10-22T09:00:00.123+00:00\nTraceback (most recent call last):\n'
    assert len(tool.HANDLED_TRACEBACK.findall(line)) == 1
    assert not tool.HANDLED_TRACEBACK.findall(b'Traceback (most recent call last):\n')


def test_what_the_tool_writes_carries_no_phase_21_name():
    text = tpi.SCRIPT.read_text()
    assert "'-pre-phase21'" in text and 'LEGACY_SUFFIX' in text  # only as the old name that still loads
    for stale in ('pebble-p21-rehearsal-', '/private/tmp/pebble-p21-drill-', 'phase21-candidate', 'desktop-release-v042'):
        assert text.count(stale) == (1 if stale == 'pebble-p21-rehearsal-' else 0), stale  # purge lists old scratch
