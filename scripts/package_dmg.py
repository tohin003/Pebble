"""Create the drag-to-Applications DMG from a verified candidate build, without Finder automation (V1-133).

  package_dmg.py --candidate C [--out release] [--execute]

C is a scripts/build_candidate_app.py output. Before anything is written, the release app C/release/Pebble.app must
hash to the app manifest's release_app_bundle_sha256 (release_readiness.bundle_digest over the whole bundle) and pass
codesign --verify --deep --strict. The DMG is made from a ditto copy in a new private staging folder, verified with
hdiutil verify, and recorded with its SHA-256 next to it (<dmg>.json). Plan mode, the default, prints the commands and
the target and writes nothing. Every tool is called by its absolute /usr/bin path, with a fixed environment.

The DMG is not signed and the app is not notarized: a Developer ID is an external input (D17). The record says so.
Exit codes: 0 done (or planned), 3 refused.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from pebble.release_readiness import EvidenceError, Root, bundle_digest, bundle_entries  # noqa: E402

CODESIGN, DITTO, HDIUTIL = '/usr/bin/codesign', '/usr/bin/ditto', '/usr/bin/hdiutil'
ENV = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'C'}
SCHEMA = 'pebble.dmg/1'
STATEMENT = ('Built from the verified candidate named here. Not signed with a Developer ID and not notarized: '
             'macOS asks the person to confirm the first launch. SHA-256 gives integrity, not authenticity.')


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


def verified_app(candidate):
    """(app path, version, manifest digest, app digest) of the candidate's release app, refused unless the bundle on
    disk is exactly the one its app manifest records."""
    candidate = Path(os.path.abspath(candidate))
    manifest_path, app = candidate / 'app-manifest.json', candidate / 'release' / 'Pebble.app'
    try:
        data = manifest_path.read_bytes()
        manifest = json.loads(data)
        expected = manifest['release_app_bundle_sha256']
        info = manifest['apps']['release']['info_plist']
    except (OSError, ValueError, KeyError, TypeError):
        raise Refused('app_manifest_unreadable', str(manifest_path)) from None
    if manifest['apps']['release'].get('app_bundle_sha256') != expected:
        raise Refused('app_manifest_inconsistent', str(manifest_path))
    try:
        with Root(str(app), 'candidate_app') as root:
            entries, _, problems = bundle_entries(root)
    except EvidenceError as error:
        raise Refused('candidate_app_unreadable', error.code) from None
    if problems or bundle_digest(entries) != expected:
        raise Refused('candidate_differs_from_manifest', {'expected': expected, 'actual': bundle_digest(entries),
                                                          'problems': len(problems)})
    version = info.get('CFBundleShortVersionString')
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise Refused('version_unreadable', version)
    return app, version, hashlib.sha256(data).hexdigest(), expected


def package(candidate, out, *, execute=False, runner=run, arch=None):
    app, version, manifest_sha, app_sha = verified_app(candidate)
    out = Path(os.path.abspath(out))
    target = out / ('Pebble_%s_%s.dmg' % (version, arch or platform.machine()))
    if os.path.lexists(target) or os.path.lexists(str(target) + '.json'):
        raise Refused('target_exists', str(target))
    plan = {'app': str(app), 'version': version, 'target': str(target),
            'commands': [[CODESIGN, '--verify', '--deep', '--strict', str(app)],
                         [DITTO, str(app), '<staging>/Pebble.app'],
                         [HDIUTIL, 'create', '-srcfolder', '<staging>', '-volname', 'Pebble', '-fs', 'HFS+',
                          '-format', 'UDZO', '-imagekey', 'zlib-level=9', str(target)],
                         [HDIUTIL, 'verify', str(target)]]}
    if not execute:
        return dict(plan, mode='plan')
    out.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='pebble-dmg-'))
    try:
        for argv in plan['commands']:
            argv = [arg.replace('<staging>', str(staging)) for arg in argv]
            code, output = runner(argv)
            if code:
                raise Refused('command_failed', {'argv': argv[:3], 'exit_code': code, 'output': output[-400:]})
            if argv[0] == DITTO:
                (staging / 'Applications').symlink_to('/Applications')
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    record = {'schema': SCHEMA, 'dmg': target.name, 'sha256': sha_file(target), 'bytes': target.stat().st_size,
              'version': version, 'app_bundle_sha256': app_sha, 'app_manifest_sha256': manifest_sha,
              'developer_id': False, 'notarized': False, 'statement': STATEMENT}
    fd = os.open(str(target) + '.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(record, indent=2) + '\n')
    return dict(record, mode='execute', target=str(target))


def main(argv=None, runner=run):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--candidate', required=True, type=Path, help='The candidate build folder C.')
    parser.add_argument('--out', type=Path, default=ROOT / 'release')
    parser.add_argument('--execute', action='store_true', help='Write the DMG (default: print the plan only).')
    args = parser.parse_args(argv)
    if platform.system() != 'Darwin':
        print(json.dumps({'refused': 'unsupported_platform'}))
        return 3
    try:
        result = package(args.candidate, args.out, execute=args.execute, runner=runner)
    except Refused as error:
        print(json.dumps({'refused': error.code, 'detail': error.detail}))
        return 3
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
