"""Package the verified release candidate as the GitHub release assets (Phase 24, t24-publish). Dry run by default.

  package_release.py --candidate C [--out release/vX.Y.Z] [--changelog CHANGELOG.md] [--execute]

C is a scripts/build_candidate_app.py output. Before anything is written, C/release/Pebble.app must hash to
C/app-manifest.json's release_app_bundle_sha256 (release_readiness.bundle_digest over the whole bundle), its
Info.plist must name com.pebble.local and the version the manifest records, and CHANGELOG.md must have a section for
that version. With --execute it then checks codesign --verify --deep --strict and writes, into a new folder:

- Pebble-macos-arm64.zip: /usr/bin/ditto -c -k --sequesterRsrc --keepParent, so Pebble.app is at the top. The zip is
  checked twice: its entries (Pebble.app/, plus ditto's __MACOSX/ for extended attributes, and nothing else), and a
  round trip (ditto -x -k into a private folder, then the same bundle digest as the manifest);
- SHA256SUMS: one line, "<sha256>  Pebble-macos-arm64.zip", the line install.sh checks;
- RELEASE_NOTES.md: the version's CHANGELOG section, how to install and check the download, and the honest
  statements (a stable local signing identity, not a Developer ID, not notarized; SHA-256 gives integrity, not
  authenticity, because SHA256SUMS is not signed);
- release-manifest.json: the candidate's digests and every asset's SHA-256 and size.

It never uploads and never calls gh or the network: the commands for the parent, to run after the person's explicit
go, are printed. Every tool is called by its absolute /usr/bin path, with a fixed environment.
Exit codes: 0 done (or planned), 3 refused, 2 usage.
"""

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from pebble.release_readiness import EvidenceError, Root, bundle_digest, bundle_entries  # noqa: E402

CODESIGN, DITTO = '/usr/bin/codesign', '/usr/bin/ditto'
ENV = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'C'}
FORMAT = 'pebble.release-assets/1'
REPOSITORY = 'tohin003/Pebble'
IDENTIFIER = 'com.pebble.local'
APP = 'Pebble.app'
ZIP = 'Pebble-macos-arm64.zip'  # install.sh's ASSET: a fixed name, so releases/latest/download/ always finds it
SUMS, NOTES, MANIFEST = 'SHA256SUMS', 'RELEASE_NOTES.md', 'release-manifest.json'
INSTALL = 'curl -fsSL https://raw.githubusercontent.com/%s/main/install.sh | bash' % REPOSITORY
STATEMENTS = (
    'Pebble is signed with the project\'s own stable signing identity, not an Apple Developer ID, and it is not '
    'notarized by Apple.',
    'SHA256SUMS lists the zip\'s SHA-256. Checking it shows that the download arrived intact. SHA256SUMS is not '
    'signed and comes from the same release, so it does not prove who made the file (integrity, not authenticity).',
    'A zip downloaded in a browser is quarantined, so macOS may refuse the first launch. Allow Pebble alone: '
    'Control-click Pebble.app, choose Open, then Open again (or System Settings, Privacy & Security, Open Anyway). '
    'Never turn Gatekeeper off. The one-line installer downloads with curl, which does not quarantine.',
)
VERSION = re.compile(r'\d+\.\d+\.\d+')


class Refused(Exception):
    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


def run(argv):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=dict(ENV))
    return result.returncode, (result.stdout + result.stderr)[-4000:]


def sha_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def app_digest(app):
    """release_readiness.bundle_digest of the bundle at app; refused when the walk finds a problem."""
    try:
        with Root(str(app), 'candidate_app') as root:
            entries, _, problems = bundle_entries(root)
    except EvidenceError as error:
        raise Refused('candidate_app_unreadable', error.code) from None
    return bundle_digest(entries), len(entries), len(problems)


def verified_app(candidate):
    """The release app of candidate C, refused unless the bundle on disk is exactly the one its manifest records."""
    candidate = Path(os.path.abspath(candidate))
    manifest_path, app = candidate / 'app-manifest.json', candidate / 'release' / APP
    try:
        data = manifest_path.read_bytes()
        manifest = json.loads(data)
        expected = manifest['release_app_bundle_sha256']
        release = manifest['apps']['release']
        info = release['info_plist']
    except (OSError, ValueError, KeyError, TypeError):
        raise Refused('app_manifest_unreadable', str(manifest_path)) from None
    if release.get('app_bundle_sha256') != expected:
        raise Refused('app_manifest_inconsistent', str(manifest_path))
    actual, files, problems = app_digest(app)
    if problems or actual != expected:
        raise Refused('candidate_differs_from_manifest', {'expected': expected, 'actual': actual, 'problems': problems})
    try:
        plist = plistlib.loads((app / 'Contents' / 'Info.plist').read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise Refused('info_plist_unreadable', str(app)) from None
    identifier = plist.get('CFBundleIdentifier')
    if identifier != IDENTIFIER or info.get('CFBundleIdentifier', IDENTIFIER) != IDENTIFIER:
        raise Refused('bundle_identity_mismatch', identifier)
    version = plist.get('CFBundleShortVersionString')
    if not isinstance(version, str) or not VERSION.fullmatch(version) or info.get('CFBundleShortVersionString') != version:
        raise Refused('version_unreadable', version)
    return {'app': app, 'version': version, 'app_manifest_sha256': hashlib.sha256(data).hexdigest(),
            'release_app_bundle_sha256': expected, 'files': files,
            'backend_executable_sha256': release.get('backend_executable_sha256'),
            'minimum_macos': plist.get('LSMinimumSystemVersion')}


def changelog_section(path, version):
    """The text under CHANGELOG.md's "## [version]" (or "## version") heading, up to the next "## " heading."""
    try:
        lines = Path(path).read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        raise Refused('changelog_unreadable', str(path)) from None
    heading = re.compile(r'^## \[?%s\]?(?:\s|$)' % re.escape(version))
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise Refused('changelog_missing_version', version)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('## ')), len(lines))
    body = '\n'.join(lines[start + 1:end]).strip()
    if not body:
        raise Refused('changelog_missing_version', version)
    return body


def release_notes(version, section, zip_sha):
    return '\n'.join([
        '# Pebble %s' % version,
        '',
        section,
        '',
        '## Install',
        '',
        'On a Mac with Apple silicon, in Terminal:',
        '',
        '    ' + INSTALL,
        '',
        'The installer checks the download against SHA256SUMS, keeps any earlier Pebble.app in ~/PebbleBackups, '
        'never uses sudo and never changes Gatekeeper. `--dry-run` shows what it would do; `--uninstall` moves the '
        'app aside and lists what Pebble keeps on your Mac.',
        '',
        'To install by hand, download %s and SHA256SUMS into one folder, check the zip with '
        '`shasum -a 256 -c SHA256SUMS` (it should say `%s: OK`), unpack it, and drag Pebble.app to Applications.'
        % (ZIP, ZIP),
        '',
        '## Signing and checksums',
        '',
    ] + ['- ' + statement for statement in STATEMENTS] + [
        '',
        'SHA-256 of %s: `%s`' % (ZIP, zip_sha),
        '',
    ])


def gh_commands(out, version):
    tag = 'v' + version
    return [
        ['gh', 'release', 'create', tag, str(out / ZIP), str(out / SUMS), str(out / NOTES), '--repo', REPOSITORY,
         '--verify-tag', '--title', 'Pebble ' + version, '--notes-file', str(out / NOTES)],
        ['gh', 'release', 'view', tag, '--repo', REPOSITORY, '--json', 'assets'],
    ]


def after_upload(out):
    return ['curl --proto =https -fsSL https://github.com/%s/releases/latest/download/%s | diff - %s'
            % (REPOSITORY, SUMS, out / SUMS),
            'In a new macOS user account (or a clean staged HOME), run the one-line installer and open Pebble once '
            '(the publishing gate before any announcement).']


def zip_layout(path):
    """(entry count, unexpected top-level names) of the zip: Pebble.app/ and ditto's __MACOSX/ only."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    top = sorted({name.split('/', 1)[0] for name in names})
    return len(names), [name for name in top if name not in (APP, '__MACOSX')], APP in top


def write_new(path, data, mode=0o644):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)


def package(candidate, out=None, *, changelog=None, execute=False, runner=run):
    found = verified_app(candidate)
    app, version = found['app'], found['version']
    section = changelog_section(changelog or ROOT / 'CHANGELOG.md', version)
    out = Path(os.path.abspath(out)) if out else ROOT / 'release' / ('v' + version)
    if os.path.lexists(out):
        raise Refused('out_exists', str(out))
    plan = {'app': str(app), 'version': version, 'out': str(out), 'assets': [ZIP, SUMS, NOTES], 'record': MANIFEST,
            'release_app_bundle_sha256': found['release_app_bundle_sha256'],
            'commands': [[CODESIGN, '--verify', '--deep', '--strict', str(app)],
                         [DITTO, '-c', '-k', '--sequesterRsrc', '--keepParent', str(app), str(out / ZIP)],
                         [DITTO, '-x', '-k', str(out / ZIP), '<private folder>']],
            'gh_commands': gh_commands(out, version), 'after_upload': after_upload(out),
            'note': 'Run the gh commands only after the person\'s explicit go; this tool never uploads.'}
    if not execute:
        return dict(plan, mode='plan')
    code, output = runner([CODESIGN, '--verify', '--deep', '--strict', str(app)])
    if code:
        raise Refused('codesign_failed', {'exit_code': code, 'output': output[-400:]})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(mode=0o755)
    archive = out / ZIP
    code, output = runner(plan['commands'][1])
    if code or not archive.is_file():
        raise Refused('command_failed', {'argv': plan['commands'][1][:5], 'exit_code': code, 'output': output[-400:]})
    entries, unexpected, has_app = zip_layout(archive)
    if unexpected or not has_app:
        raise Refused('zip_layout', {'unexpected': unexpected, 'has_app': has_app})
    staging = Path(tempfile.mkdtemp(prefix='pebble-release-'))
    try:
        code, output = runner([DITTO, '-x', '-k', str(archive), str(staging)])
        if code:
            raise Refused('command_failed', {'argv': [DITTO, '-x', '-k'], 'exit_code': code, 'output': output[-400:]})
        round_trip, _, problems = app_digest(staging / APP)
    finally:
        shutil.rmtree(staging, ignore_errors=True)  # only this run's own private folder
    if problems or round_trip != found['release_app_bundle_sha256']:
        raise Refused('zip_round_trip_differs', {'expected': found['release_app_bundle_sha256'], 'actual': round_trip})
    zip_sha, zip_bytes = sha_file(archive), archive.stat().st_size
    sums = ('%s  %s\n' % (zip_sha, ZIP)).encode()
    write_new(out / SUMS, sums)
    notes = release_notes(version, section, zip_sha).encode()
    write_new(out / NOTES, notes)
    record = {
        'format': FORMAT, 'version': version, 'tag': 'v' + version, 'repository': REPOSITORY,
        'candidate': {'path': str(Path(os.path.abspath(candidate))), 'identifier': IDENTIFIER,
                      'app_manifest_sha256': found['app_manifest_sha256'],
                      'release_app_bundle_sha256': found['release_app_bundle_sha256'],
                      'backend_executable_sha256': found['backend_executable_sha256'],
                      'minimum_macos': found['minimum_macos'], 'files': found['files']},
        'assets': [{'name': ZIP, 'sha256': zip_sha, 'bytes': zip_bytes},
                   {'name': SUMS, 'sha256': hashlib.sha256(sums).hexdigest(), 'bytes': len(sums)},
                   {'name': NOTES, 'sha256': hashlib.sha256(notes).hexdigest(), 'bytes': len(notes)}],
        'zip': {'entries': entries, 'top': APP, 'round_trip_bundle_sha256': round_trip,
                'made_with': ' '.join(plan['commands'][1][:5])},
        'signing': {'identity': 'the project\'s stable local signing identity', 'developer_id': False,
                    'notarized': False, 'checksums_signed': False},
        'statements': list(STATEMENTS), 'gh_commands': plan['gh_commands'], 'after_upload': plan['after_upload'],
    }
    write_new(out / MANIFEST, (json.dumps(record, indent=2) + '\n').encode())
    return dict(record, mode='execute', out=str(out))


def main(argv=None, runner=run):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--candidate', required=True, type=Path, help='The candidate build folder C.')
    parser.add_argument('--out', type=Path, help='A new folder (default: release/v<version>).')
    parser.add_argument('--changelog', type=Path, help='Default: CHANGELOG.md.')
    parser.add_argument('--execute', action='store_true', help='Write the assets (default: print the plan only).')
    args = parser.parse_args(argv)
    try:
        result = package(args.candidate, args.out, changelog=args.changelog, execute=args.execute, runner=runner)
    except Refused as error:
        print(json.dumps({'refused': error.code, 'detail': error.detail}, default=str))
        return 3
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
