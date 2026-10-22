"""scripts/package_release.py: the GitHub release assets from the verified candidate (Phase 24, t24-publish).

The candidate is a small bundle in tmp_path with its app manifest computed the way build_candidate_app.py computes it.
codesign and ditto are an injected runner: ditto's create and extract are done with zipfile (Pebble.app kept as the
top folder, modes kept). Nothing is uploaded and gh is never run; shasum -c is the real /usr/bin/shasum.
"""

import hashlib
import importlib.util
import json
import os
import plistlib
import re
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from pebble.release_readiness import Root, bundle_digest, bundle_entries

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('package_release_under_test', REPO / 'scripts' / 'package_release.py')
pr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pr)
CHANGELOG = """# Changelog

## [1.0.0] - 2026-10-22

### Added
- The first public release.

## [0.6.1] - 2026-10-15

- Earlier work.
"""


def put(path, data=b'', mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    path.chmod(mode)
    return path


def digest_of(app):
    with Root(str(app), 'test') as root:
        return bundle_digest(bundle_entries(root)[0])


def candidate(tmp_path, *, version='1.0.0', identifier='com.pebble.local', manifest_version=None):
    """C/release/Pebble.app and C/app-manifest.json, as build_candidate_app.py writes them."""
    C = tmp_path / 'C'
    app = C / 'release' / 'Pebble.app'
    info = {'CFBundleIdentifier': identifier, 'CFBundleShortVersionString': version, 'CFBundleExecutable': 'pebble',
            'LSMinimumSystemVersion': '12.0'}
    put(app / 'Contents/Info.plist', plistlib.dumps(info))
    put(app / 'Contents/MacOS/pebble', b'\xcf\xfa\xed\xfe shell', 0o755)
    put(app / 'Contents/Resources/resources/backend/pebble-backend', b'\xcf\xfa\xed\xfe backend', 0o755)
    put(app / 'Contents/Resources/resources/NOTICE.md', 'Pebble\nCopyright 2026 Tohin Majumder\n')
    digest = digest_of(app)
    manifest = {'release_app_bundle_sha256': digest,
                'apps': {'release': {'app_bundle_sha256': digest, 'identifier': identifier,
                                     'backend_executable_sha256': hashlib.sha256(b'\xcf\xfa\xed\xfe backend').hexdigest(),
                                     'info_plist': dict(info, CFBundleShortVersionString=manifest_version or version)}}}
    put(C / 'app-manifest.json', json.dumps(manifest))
    return C, app, digest


class Tools:
    """codesign and ditto, recorded; ditto's -c and -x done with zipfile."""

    def __init__(self, codesign_exit=0, extra_entry=None, alter_on_extract=False):
        self.calls, self.codesign_exit = [], codesign_exit
        self.extra_entry, self.alter_on_extract = extra_entry, alter_on_extract

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[0] == pr.CODESIGN:
            return self.codesign_exit, '' if not self.codesign_exit else 'invalid signature'
        if argv[:5] == [pr.DITTO, '-c', '-k', '--sequesterRsrc', '--keepParent']:
            source, target = Path(argv[5]), Path(argv[6])
            with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(source.rglob('*')):
                    name = (Path(source.name) / path.relative_to(source)).as_posix()
                    info = zipfile.ZipInfo(name + ('/' if path.is_dir() else ''))
                    info.external_attr = os.lstat(path).st_mode << 16
                    archive.writestr(info, b'' if path.is_dir() else path.read_bytes())
                if self.extra_entry:
                    archive.writestr(self.extra_entry, b'extra')
            return 0, ''
        if argv[:3] == [pr.DITTO, '-x', '-k']:
            with zipfile.ZipFile(argv[3]) as archive:
                for info in archive.infolist():
                    path = archive.extract(info, argv[4])
                    if not info.is_dir():
                        os.chmod(path, (info.external_attr >> 16) & 0o7777)
            if self.alter_on_extract:
                Path(argv[4], 'Pebble.app/Contents/MacOS/pebble').write_bytes(b'altered')
            return 0, ''
        return 1, 'unexpected command'


def tree(folder):
    return sorted((p.relative_to(folder).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in Path(folder).rglob('*'))


def test_the_zip_holds_pebble_app_at_its_top_and_sha256sums_verifies(tmp_path):
    C, app, digest = candidate(tmp_path)
    changelog = put(tmp_path / 'CHANGELOG.md', CHANGELOG)
    out, tools = tmp_path / 'release' / 'v1.0.0', Tools()
    done = pr.package(C, out, changelog=changelog, execute=True, runner=tools)
    assert done['mode'] == 'execute' and done['version'] == '1.0.0'
    assert sorted(p.name for p in out.iterdir()) == ['Pebble-macos-arm64.zip', 'RELEASE_NOTES.md', 'SHA256SUMS',
                                                     'release-manifest.json']
    archive = out / pr.ZIP
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
    assert names and all(name.startswith('Pebble.app/') for name in names)
    assert 'Pebble.app/Contents/MacOS/pebble' in names
    # SHA256SUMS: exactly the line install.sh checks, and the system's shasum agrees.
    sums = (out / 'SHA256SUMS').read_text()
    assert sums == '%s  %s\n' % (hashlib.sha256(archive.read_bytes()).hexdigest(), pr.ZIP)
    install_line = re.search(r"SUMS_LINE='([^']+)'", (REPO / 'install.sh').read_text()).group(1)
    assert re.fullmatch(install_line, sums.rstrip('\n'))
    checked = subprocess.run(['/usr/bin/shasum', '-a', '256', '-c', 'SHA256SUMS'], cwd=out, capture_output=True,
                             text=True)
    assert checked.returncode == 0 and checked.stdout.strip() == pr.ZIP + ': OK'
    # The tools: verify the signature, zip with ditto keeping the parent, and unpack it again to check the bundle.
    assert tools.calls[0] == [pr.CODESIGN, '--verify', '--deep', '--strict', str(app)]
    assert tools.calls[1] == [pr.DITTO, '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(archive)]
    assert tools.calls[2][:4] == [pr.DITTO, '-x', '-k', str(archive)] and len(tools.calls) == 3
    assert not os.path.exists(tools.calls[2][4])  # the private round-trip folder is gone
    # The record and the notes.
    record = json.loads((out / 'release-manifest.json').read_text())
    assert record['format'] == 'pebble.release-assets/1' and record['tag'] == 'v1.0.0'
    assert record['candidate']['release_app_bundle_sha256'] == digest == record['zip']['round_trip_bundle_sha256']
    for asset in record['assets']:
        data = (out / asset['name']).read_bytes()
        assert asset['sha256'] == hashlib.sha256(data).hexdigest() and asset['bytes'] == len(data)
    assert record['signing'] == {'identity': "the project's stable local signing identity", 'developer_id': False,
                                 'notarized': False, 'checksums_signed': False}
    notes = (out / 'RELEASE_NOTES.md').read_text()
    assert notes.startswith('# Pebble 1.0.0\n') and '### Added\n- The first public release.' in notes
    assert 'Earlier work' not in notes and pr.INSTALL in notes and 'shasum -a 256 -c SHA256SUMS' in notes
    for statement in ('not an Apple Developer ID', 'not notarized', 'integrity, not authenticity',
                      'Never turn Gatekeeper off', 'Open Anyway'):
        assert statement in notes, statement
    assert str(tmp_path) not in notes and '/' + 'Users' + '/' not in notes  # it is uploaded: no machine path
    # Nothing is uploaded: gh is printed, never run.
    assert not any(call[0] == 'gh' for call in tools.calls)
    create = done['gh_commands'][0]
    assert create[:4] == ['gh', 'release', 'create', 'v1.0.0'] and '--verify-tag' in create
    assert create[4:7] == [str(archive), str(out / 'SHA256SUMS'), str(out / 'RELEASE_NOTES.md')]
    assert create[create.index('--repo') + 1] == 'tohin003/Pebble'


def test_the_dry_run_writes_nothing_and_runs_no_tool(tmp_path):
    C, app, _ = candidate(tmp_path)
    changelog = put(tmp_path / 'CHANGELOG.md', CHANGELOG)
    before, tools = tree(tmp_path), Tools()
    plan = pr.package(C, tmp_path / 'out', changelog=changelog, runner=tools)
    assert plan['mode'] == 'plan' and tools.calls == [] and tree(tmp_path) == before
    assert plan['commands'][1] == [pr.DITTO, '-c', '-k', '--sequesterRsrc', '--keepParent', str(app),
                                   str(tmp_path / 'out' / pr.ZIP)]
    assert all(command[0].startswith('/usr/bin/') for command in plan['commands'])
    assert pr.main(['--candidate', str(C), '--out', str(tmp_path / 'out'), '--changelog', str(changelog)],
                   runner=tools) == 0
    assert tools.calls == [] and tree(tmp_path) == before


def test_a_candidate_whose_hashes_differ_from_its_app_manifest_is_refused(tmp_path, capsys):
    C, app, _ = candidate(tmp_path)
    changelog = put(tmp_path / 'CHANGELOG.md', CHANGELOG)
    put(app / 'Contents/Resources/resources/added-after-the-build.txt', 'changed')
    for execute in (False, True):
        tools = Tools()
        with pytest.raises(pr.Refused) as error:
            pr.package(C, tmp_path / 'out', changelog=changelog, execute=execute, runner=tools)
        assert error.value.code == 'candidate_differs_from_manifest' and tools.calls == []
    assert not (tmp_path / 'out').exists()
    assert pr.main(['--candidate', str(C), '--changelog', str(changelog), '--execute']) == 3
    assert json.loads(capsys.readouterr().out)['refused'] == 'candidate_differs_from_manifest'
    manifest = json.loads((C / 'app-manifest.json').read_text())
    manifest['apps']['release']['app_bundle_sha256'] = '0' * 64
    put(C / 'app-manifest.json', json.dumps(manifest))
    with pytest.raises(pr.Refused) as error:
        pr.package(C, tmp_path / 'out', changelog=changelog)
    assert error.value.code == 'app_manifest_inconsistent'


@pytest.mark.parametrize('arrange, code', [
    (dict(identifier='com.pebble.candidate'), 'bundle_identity_mismatch'),
    (dict(manifest_version='1.0.1'), 'version_unreadable'),
    (dict(version='1.0'), 'version_unreadable'),
])
def test_only_the_release_identity_and_its_recorded_version_are_packaged(tmp_path, arrange, code):
    C, _, _ = candidate(tmp_path, **arrange)
    with pytest.raises(pr.Refused) as error:
        pr.package(C, tmp_path / 'out', changelog=put(tmp_path / 'CHANGELOG.md', CHANGELOG))
    assert error.value.code == code


def test_the_changelog_must_describe_the_version(tmp_path):
    C, _, _ = candidate(tmp_path, version='1.0.2')
    with pytest.raises(pr.Refused) as error:
        pr.package(C, tmp_path / 'out', changelog=put(tmp_path / 'CHANGELOG.md', CHANGELOG))
    assert error.value.code == 'changelog_missing_version'
    with pytest.raises(pr.Refused) as error:
        pr.changelog_section(put(tmp_path / 'empty.md', '# Changelog\n\n## [1.0.2]\n\n## [1.0.1]\n- x\n'), '1.0.2')
    assert error.value.code == 'changelog_missing_version'
    assert pr.changelog_section(put(tmp_path / 'plain.md', '## 1.0.2\nText.\n'), '1.0.2') == 'Text.'


def test_the_real_changelog_describes_1_0_0_and_its_notes_hold_no_machine_path():
    section = pr.changelog_section(REPO / 'CHANGELOG.md', '1.0.0')
    notes = pr.release_notes('1.0.0', section, 'a' * 64)
    assert '/' + 'Users' + '/' not in notes and '.runtime' not in notes and 'reports/' not in notes
    assert 'Apache' in (REPO / 'CHANGELOG.md').read_text()


@pytest.mark.parametrize('tools, code', [
    (Tools(codesign_exit=1), 'codesign_failed'),
    (Tools(extra_entry='Other.app/Contents/Info.plist'), 'zip_layout'),
    (Tools(alter_on_extract=True), 'zip_round_trip_differs'),
])
def test_a_bad_signature_a_wrong_zip_layout_or_a_changed_round_trip_is_refused(tmp_path, tools, code):
    C, _, _ = candidate(tmp_path)
    out = tmp_path / 'out'
    with pytest.raises(pr.Refused) as error:
        pr.package(C, out, changelog=put(tmp_path / 'CHANGELOG.md', CHANGELOG), execute=True, runner=tools)
    assert error.value.code == code
    assert not (out / 'SHA256SUMS').exists() and not (out / 'RELEASE_NOTES.md').exists()
    if code == 'codesign_failed':
        assert not out.exists()


def test_an_existing_output_folder_is_refused(tmp_path):
    C, _, _ = candidate(tmp_path)
    (tmp_path / 'out').mkdir()
    with pytest.raises(pr.Refused) as error:
        pr.package(C, tmp_path / 'out', changelog=put(tmp_path / 'CHANGELOG.md', CHANGELOG))
    assert error.value.code == 'out_exists'
    assert stat.S_ISDIR((tmp_path / 'out').stat().st_mode) and list((tmp_path / 'out').iterdir()) == []
