"""Phase 16 D03: pinned app identity and honest launch receipts.

Every bundle here is a tiny fake under tmp_path, and every signer, opener and
process lister is injected: nothing launches a real app or runs codesign. The
only real OS reads are libproc lookups of this test process itself.
"""

import os
import plistlib
import subprocess
import sys
import time

import pytest

from pebble import native_apps
from pebble.policy import PolicyError


TEAM = 'ABCDE12345'


@pytest.fixture(autouse=True)
def fresh_cache():
    native_apps._cache.clear()
    yield
    native_apps._cache.clear()


def bundle(root, name, bundle_id, *, version='1.0', background=False, display=None):
    path = root / f'{name}.app'
    (path / 'Contents/MacOS').mkdir(parents=True, exist_ok=True)
    info = {'CFBundleIdentifier': bundle_id, 'CFBundleName': name, 'CFBundleShortVersionString': version}
    if display:
        info['CFBundleDisplayName'] = display
    if background:
        info['LSBackgroundOnly'] = True
    (path / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    (path / 'Contents/MacOS' / name).write_bytes(b'\xcf\xfa\xed\xfe' + name.encode())
    return path


class Signer:
    def __init__(self, result=('team', TEAM)):
        self.result, self.calls = result, []

    def __call__(self, path):
        self.calls.append(str(path))
        return self.result


class Clock:
    def __init__(self):
        self.now, self.sleeps = 100.0, 0

    def monotonic(self):
        return self.now

    def wall(self):
        return 1_800_000_000.0 + self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds


@pytest.fixture
def apps(tmp_path):
    root = tmp_path / 'Applications'
    root.mkdir()
    notes = bundle(root, 'Notes', 'com.example.notes', version='2.1')
    bundle(root, 'Agent', 'com.example.agent', background=True)
    bundle(root, 'Atlas', 'com.example.atlas', display='Atlas Maps')
    broken = root / 'Broken.app'
    (broken / 'Contents').mkdir(parents=True)
    (broken / 'Contents/Info.plist').write_bytes(b'not a plist')
    return root, notes


def test_identity_reads_bundle_version_and_team(apps):
    root, notes = apps
    signer = Signer()
    entry = native_apps.pin(str(notes), roots=[root], signer=signer)
    assert entry == {'id': str(notes), 'name': 'Notes', 'bundle_id': 'com.example.notes', 'version': '2.1',
                     'team_id': TEAM, 'signature': 'team'}
    native_apps.pin(str(notes), roots=[root], signer=signer)
    assert len(signer.calls) == 1  # cached until the plist or executable changes


def test_inventory_lists_foreground_bundles_only(apps):
    root, notes = apps
    listed = native_apps.inventory(roots=[root], signer=Signer())
    assert [e['name'] for e in listed] == ['Atlas Maps', 'Notes']
    assert all(set(e) == {'id', 'name', 'bundle_id', 'version', 'team_id', 'signature'} for e in listed)
    signer = Signer()
    quick = native_apps.inventory(roots=[root], signer=signer, team=False)
    assert signer.calls == [] and {e['signature'] for e in quick} == {'unknown'}
    assert {e['team_id'] for e in quick} == {None}


def test_pin_refuses_paths_outside_the_inventory(apps, tmp_path):
    root, notes = apps
    elsewhere = bundle(tmp_path / 'Downloads', 'Notes', 'com.example.notes')
    with pytest.raises(PolicyError, match='inventory'):
        native_apps.pin(str(elsewhere), roots=[root], signer=Signer())
    with pytest.raises(PolicyError, match='inventory'):
        native_apps.pin(str(root / 'Agent.app'), roots=[root], signer=Signer())
    with pytest.raises(PolicyError, match='cannot be read'):
        native_apps.pin(str(root / 'Broken.app'), roots=[root], signer=Signer())


def launch(entry, root, clock, *, signer=None, opener=None, lister=None):
    return native_apps.launch(entry, roots=[root], signer=signer or Signer(), opener=opener,
                              lister=lister or (lambda: []), clock=clock.monotonic, wall=clock.wall,
                              sleep=clock.sleep)


def test_legacy_entry_without_pin_is_refused_before_opening(apps):
    root, notes = apps
    opened = []
    with pytest.raises(PolicyError, match='predates identity pinning'):
        launch({'id': str(notes), 'name': 'Notes'}, root, Clock(), opener=opened.append)
    assert opened == []


def test_bundle_replaced_after_review_is_refused(apps):
    root, notes = apps
    entry = native_apps.pin(str(notes), roots=[root], signer=Signer())
    bundle(root, 'Notes', 'com.attacker.lookalike.notes')
    opened = []
    with pytest.raises(PolicyError, match='changed after review'):
        launch(entry, root, Clock(), opener=lambda argv: opened.append(argv) or 0)
    assert opened == []


@pytest.mark.parametrize('now', [('team', 'ZZZZZ99999'), ('unsigned', None)])
def test_signing_team_drift_is_refused(apps, now):
    root, notes = apps
    entry = native_apps.pin(str(notes), roots=[root], signer=Signer())
    native_apps._cache.clear()
    opened = []
    with pytest.raises(PolicyError, match='changed after review'):
        launch(entry, root, Clock(), signer=Signer(now), opener=lambda argv: opened.append(argv) or 0)
    assert opened == []


def test_unreadable_signature_is_refused(apps):
    root, notes = apps
    entry = native_apps.pin(str(notes), roots=[root], signer=Signer(('unknown', None)))
    with pytest.raises(PolicyError, match='signing identity could not be read'):
        launch(entry, root, Clock(), signer=Signer(('unknown', None)), opener=lambda argv: 0)


def test_launch_opens_the_pinned_path_and_observes_running(apps):
    root, notes = apps
    entry = native_apps.pin(str(notes), roots=[root], signer=Signer())
    clock, opened, polls = Clock(), [], []

    def lister():
        polls.append(clock.now)
        if len(polls) < 3:  # first poll is the before-launch check
            return []
        return [{'pid': 777, 'start': 5.0, 'executable': str(notes / 'Contents/MacOS/Notes'),
                 'bundle_path': str(notes)}]

    result = launch(entry, root, clock, opener=lambda argv: opened.append(argv) or 0, lister=lister)
    assert opened == [['/usr/bin/open', '-a', str(notes)]]
    assert result['launch_observed'] is True and result['readback'] == 'running'
    assert result['already_running'] is False and result['observed_at'] == clock.wall()
    assert result['proves'] == 'launch_request_only' and result['authorization'] == 'none'
    assert result['bundle_id'] == 'com.example.notes' and result['team_id'] == TEAM
    assert 'pid' not in result and 777 not in result.values()
    assert native_apps.describe(result) == 'macOS accepted the request to open Notes (running: observed).'


def test_launch_not_observed_within_readback_window(apps, tmp_path):
    root, notes = apps
    entry = native_apps.pin(str(notes), roots=[root], signer=Signer())
    other = bundle(tmp_path / 'Volumes', 'Notes', 'com.example.notes')  # another registered copy is running
    clock = Clock()
    lister = lambda: [{'pid': 9, 'start': 1.0, 'executable': str(other / 'Contents/MacOS/Notes'),
                       'bundle_path': str(other)}]
    result = launch(entry, root, clock, opener=lambda argv: 0, lister=lister)
    assert result['launch_observed'] is False and result['readback'] == 'not_running'
    assert result['observed_at'] is None
    assert clock.now - 100.0 >= native_apps.LAUNCH_READBACK_SECONDS
    assert clock.sleeps == pytest.approx(native_apps.LAUNCH_READBACK_SECONDS / native_apps.READBACK_INTERVAL, abs=1)
    assert native_apps.describe(result) == 'macOS accepted the request to open Notes (running: not observed).'


def test_launch_readback_unavailable_is_unknown(apps):
    root, notes = apps
    entry = native_apps.pin(str(notes), roots=[root], signer=Signer())

    def lister():
        raise native_apps.Unavailable('no libproc')

    result = launch(entry, root, Clock(), opener=lambda argv: 0, lister=lister)
    assert result['launch_observed'] is None and result['already_running'] is None
    assert result['readback'] == 'unavailable'
    assert native_apps.describe(result) == 'macOS accepted the request to open Notes (running: unknown).'


def test_already_running_and_version_change_are_reported(apps):
    root, notes = apps
    entry = native_apps.pin(str(notes), roots=[root], signer=Signer())
    bundle(root, 'Notes', 'com.example.notes', version='2.2.1')  # an update keeps bundle and team
    row = {'pid': 5, 'start': 1.0, 'executable': str(notes / 'Contents/MacOS/Notes'), 'bundle_path': str(notes)}
    result = launch(entry, root, Clock(), opener=lambda argv: 0, lister=lambda: [row])
    assert result['already_running'] is True and result['launch_observed'] is True
    assert result['version'] == '2.2.1' and result['version_changed'] is True


@pytest.mark.parametrize('code,message', [(1, 'did not accept'), (None, 'did not finish')])
def test_open_failure_raises_without_receipt(apps, code, message):
    root, notes = apps
    entry = native_apps.pin(str(notes), roots=[root], signer=Signer())
    with pytest.raises(PolicyError, match=message):
        launch(entry, root, Clock(), opener=lambda argv: code)


def test_describe_never_claims_operation():
    assert native_apps.describe({'opened': 'Calculator'}) == \
        'macOS accepted the request to open Calculator (running: unknown).'
    for observed in (True, False, None):
        text = native_apps.describe({'requested': 'Mail', 'launch_observed': observed})
        assert text.startswith('macOS accepted the request to open Mail') and 'Opened' not in text


def test_operation_issue_refuses_launch_as_proof_of_operation():
    receipts = [{'requested': 'Notes', 'launch_observed': True}]
    ask = 'Open Notes and write a shopping list'
    assert 'only opened' in native_apps.operation_issue(ask, receipts)
    assert native_apps.operation_issue('Open Notes please', receipts) is None
    assert native_apps.operation_issue(ask, []) is None
    assert native_apps.operation_issue(ask, receipts, [{'class': 'input', 'verdict': 'unknown'}])
    assert native_apps.operation_issue(ask, receipts, [{'class': 'input', 'verdict': 'occurred'}]) is None


def test_codesign_output_parsing(monkeypatch, tmp_path):
    outputs = iter([
        (0, b'Executable=/x\nIdentifier=com.example\nTeamIdentifier=ABCDE12345\n'),
        (0, b'Identifier=com.example\nTeamIdentifier=not set\n'),
        (1, b'/x: code object is not signed at all\n'),
        (1, b'/x: something else failed\n'),
        (0, b'TeamIdentifier=bad team\n'),
    ])
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        code, err = next(outputs)
        return subprocess.CompletedProcess(argv, code, None, err)

    monkeypatch.setattr(native_apps.subprocess, 'run', fake_run)
    results = [native_apps.codesign_team(tmp_path / 'X.app') for _ in range(5)]
    assert results == [('team', 'ABCDE12345'), ('unsigned', None), ('unsigned', None), ('unknown', None),
                       ('unknown', None)]
    assert seen[0][:3] == ['/usr/bin/codesign', '-d', '--verbose=2']


def test_bundle_of_executable_paths():
    assert native_apps._bundle_of('/Applications/Mail.app/Contents/MacOS/Mail') == '/Applications/Mail.app'
    helper = '/Applications/Big.app/Contents/Frameworks/Helper.app/Contents/MacOS/Helper'
    assert native_apps._bundle_of(helper) == '/Applications/Big.app/Contents/Frameworks/Helper.app'
    assert native_apps._bundle_of('/usr/bin/python3') is None


def test_instances_filter_by_bundle_identity(apps):
    root, notes = apps
    atlas = root / 'Atlas.app'
    rows = [{'pid': 1, 'start': 1.0, 'bundle_path': str(notes)}, {'pid': 2, 'start': 2.0, 'bundle_path': str(atlas)},
            {'pid': 3, 'start': 3.0, 'bundle_path': str(root / 'Missing.app')}]
    signer = Signer()
    found = native_apps.instances('com.example.notes', lister=lambda: rows, signer=signer)
    assert found == [{'pid': 1, 'start': 1.0, 'bundle_id': 'com.example.notes', 'team_id': TEAM,
                      'signature': 'team', 'path': str(notes)}]
    assert signer.calls == [str(notes)]  # other running bundles are never signature-checked


@pytest.mark.skipif(sys.platform != 'darwin', reason='libproc is macOS only')
def test_libproc_reads_this_process_only():
    path = native_apps.pid_path(os.getpid())
    assert path and os.path.isabs(path) and os.path.exists(path)
    start = native_apps.process_start(os.getpid())
    assert start is not None and time.time() - 7 * 86_400 < start <= time.time()
    assert native_apps.pid_path(-1) is None
