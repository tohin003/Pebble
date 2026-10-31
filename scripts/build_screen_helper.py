#!/usr/bin/env python3
"""Parent tool: build Pebble's screen helper once and pin it (Phase 45, layer 4).

The helper (src-tauri/helpers/screen/main.swift, protocol pebble-screen/1: README.md beside it) captures one window,
recognises text on the device and posts foreground keyboard and mouse input under a lease. The candidate builder
copies the pinned artifact unchanged into every release beside the vault helper (resources/helpers/), checks its
manifest, source and signature and never re-signs it; screen_helper.helper_path() finds it there.

Dry run by default: prints every command (argv, cwd, environment) and every path it would write, and writes nothing.
--execute needs --i-am-the-parent-with-user-approval <stamp> and the local identity of scripts/pebble_signing.py.

Steps, all commands run in a new work folder (default <root>/.runtime/screen-helper-<stamp>):
1. Record `xcrun swiftc --version` and the SDK version.
2. Copy main.swift into the work folder (its SHA-256 must equal the checkout's), then compile and link it twice with
   one fixed command and relative paths only (the README's: -O, Swift 5 mode, arm64 macOS 14, -file-compilation-dir .,
   no linker ad-hoc signature). The first result is moved aside, and the two must be byte-identical; neither may hold
   the work folder's, the checkout's or the home folder's path (V1-023).
3. Unlock the identity keychain and sign out/pebble-screen-helper:
   - identifier com.pebble.screen-helper;
   - the explicit designated requirement (identifier and certificate leaf);
   - the hardened runtime with no entitlements;
   - no timestamp.
   Then lock the keychain again, even when signing failed.
4. Check the signature: codesign --verify --strict; --display (identifier, cdhash, this identity as the only
   authority, no team, runtime, not ad hoc); and the designated requirement read back.
5. Write src-tauri/resources/helpers/pebble-screen-helper (0755) and pebble-screen-helper.json (0644), each new
   (O_EXCL), beside the pinned vault helper (which must be there: the candidate builder stages that folder only with
   it). The manifest records the SHA-256, the cdhash, the requirement, the signer, the source SHA-256 and the compiler.
   An existing pinned pair is refused; with --replace-pinned-helper it is moved into the work folder first and the new
   manifest records what it replaces. Then both pins are read back with the candidate builder's own checks, and the
   compiler's caches this run made (work/home, work/tmp; nothing else reads them) are removed; logs and builds stay.

Exit codes: 0 done (or a clean dry run), 2 usage, 3 refused before the pinned files changed, 4 stopped after.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOL = Path(__file__).resolve()
TOOL_ROOT = TOOL.parents[1]


def load_signing():
    """A private instance of scripts/pebble_signing.py (identity record, codesign argv, unlock and lock)."""
    spec = importlib.util.spec_from_file_location('screen_helper_signing', TOOL_ROOT / 'scripts' / 'pebble_signing.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


signing = load_signing()
XCRUN, CODESIGN = '/usr/bin/xcrun', signing.CODESIGN
SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
IDENTIFIER = signing.SCREEN_HELPER_IDENTIFIER
PROTOCOL = 'pebble-screen/1'
SWIFTC_FLAGS = ('-O', '-swift-version', '5', '-target', 'arm64-apple-macos14.0', '-file-compilation-dir', '.')
LDFLAGS = ('-Xlinker', '-no_adhoc_codesign')
SOURCE, OUTPUT = 'main.swift', 'out/pebble-screen-helper'  # relative to the work folder, every command's cwd
FOLDERS = ('logs', 'home', 'tmp', 'out', 'first', 'replaced')
STAMP = re.compile(r'\d{8}T\d{6}Z')
NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_STOPPED = 0, 2, 3, 4
PINNED = (signing.SCREEN_HELPER_BINARY, signing.SCREEN_HELPER_MANIFEST)


class Refused(Exception):
    def __init__(self, code, paths=(), detail=None):
        super().__init__(code)
        self.code, self.paths, self.detail = code, [str(path) for path in paths], detail

    def report(self):
        return {'refused': self.code, 'paths': self.paths, 'detail': self.detail}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fchmod(stream.fileno(), mode)
        os.fsync(stream.fileno())


def run_command(argv, cwd, env, stream):
    """One command, its output in stream, nothing inherited."""
    return subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                          start_new_session=True).returncode


class ScreenHelperBuild:
    def __init__(self, root, *, identity, work=None, replace=False, stamp=None, runner=run_command, unlock=None,
                 lock=None, home=None):
        self.root = Path(root).resolve()
        self.identity, self.replace, self.stamp, self.runner = identity, replace, stamp, runner
        work = Path(work) if work else self.root / '.runtime' / ('screen-helper-' + (stamp or '<stamp>'))
        self.work = Path(os.path.abspath(work)).parent.resolve() / Path(work).name
        self.unlock = unlock if unlock is not None else (lambda: signing.unlock(identity))
        self.lock = lock if lock is not None else (lambda: signing.lock(identity))
        self.home = Path(home) if home is not None else Path.home()
        self.source = self.root / signing.SCREEN_HELPER_SOURCE
        self.pinned = self.root / 'src-tauri/resources' / signing.HELPER_FOLDER
        self.logs = []

    def env(self):
        return {'PATH': SYSTEM_PATH, 'LANG': 'C', 'HOME': str(self.work / 'home'), 'TMPDIR': str(self.work / 'tmp') + '/'}

    def commands(self):
        out = self.work / OUTPUT
        builds = [('build-%d' % n, [XCRUN, 'swiftc', *SWIFTC_FLAGS, SOURCE, '-o', OUTPUT, *LDFLAGS]) for n in (1, 2)]
        sign = (signing.sign_argv(self.identity, out, IDENTIFIER, runtime=True) if self.identity is not None
                else ['<no identity>'])
        return ([('swiftc-version', [XCRUN, 'swiftc', '--version']), ('sdk-version', [XCRUN, '--show-sdk-version'])]
                + builds + [('sign', sign), ('verify', [CODESIGN, '--verify', '--strict', '--verbose=2', str(out)]),
                            ('display', [CODESIGN, '--display', '--verbose=4', str(out)]),
                            ('requirements', [CODESIGN, '--display', '--requirements', '-', str(out)])])

    def needles(self):
        """V1-023: the paths the shipped helper must not carry: the work folder, the checkout and the home folder."""
        return sorted({str(path) for path in (self.work, self.root, self.home) if len(str(path)) > 1})

    def preflight(self):
        problems = []
        if sys.platform != 'darwin':
            problems.append(Refused('unsupported_platform', [], sys.platform))
        missing = [tool for tool in (XCRUN, CODESIGN) if not os.access(tool, os.X_OK)]
        if missing:
            problems.append(Refused('tool_missing', missing))
        if not self.source.is_file():
            problems.append(Refused('source_missing', [self.source]))
        if self.identity is None:
            problems.append(Refused('identity_required', [], 'The screen helper ships signed with the local identity '
                                    '(pebble_signing.py), like the vault helper beside it.'))
        vault = [self.pinned / name for name in (signing.HELPER_BINARY, signing.HELPER_MANIFEST)]
        if not all(os.path.lexists(path) for path in vault):
            problems.append(Refused('vault_helper_missing', vault, 'The candidate builder stages resources/helpers only '
                                    'with the pinned vault helper in it; pin that first (scripts/build_vault_helper.py).'))
        existing = [self.pinned / name for name in PINNED if os.path.lexists(self.pinned / name)]
        if existing and not self.replace:
            problems.append(Refused('pinned_helper_exists', existing, 'A screen helper is already pinned. Use '
                                    '--replace-pinned-helper to rebuild it (a fix, or a new identity).'))
        if os.path.lexists(self.pinned) and not self.pinned.is_dir():
            problems.append(Refused('pinned_folder_not_directory', [self.pinned]))
        if os.path.lexists(self.work):
            problems.append(Refused('work_exists', [self.work]))
        elif not self.work.parent.is_dir():
            problems.append(Refused('work_parent_missing', [self.work.parent]))
        return problems

    def previous(self):
        """The pinned screen helper's manifest, read leniently (None when absent or unreadable)."""
        try:
            doc = json.loads((self.pinned / signing.SCREEN_HELPER_MANIFEST).read_bytes())
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def plan(self):
        return {'mode': 'plan', 'tool': str(TOOL), 'root': str(self.root), 'work': str(self.work),
                'identity': self.identity.describe() if self.identity else None,
                'commands': [{'label': label, 'argv': argv, 'cwd': str(self.work), 'env': self.env()}
                             for label, argv in self.commands()],
                'local_steps': ['copy ' + signing.SCREEN_HELPER_SOURCE + ' to main.swift in the work folder and compare '
                                'its SHA-256',
                                'move out/pebble-screen-helper to first/ after build 1',
                                'compare build 2 with build 1 byte for byte',
                                'check that neither build holds the work folder, the checkout or the home folder',
                                'unlock the identity keychain before sign; lock it after, even on failure',
                                'write the pinned helper and its manifest (O_EXCL)',
                                'read both pins back with pebble_signing.check_pinned_helper and '
                                'check_pinned_screen_helper'],
                'writes': [str(self.work)] + [str(self.pinned / name) for name in PINNED],
                'moves_aside_if_replacing': [str(self.pinned / name) for name in PINNED] if self.replace else [],
                'designated_requirement': self.identity.requirement(IDENTIFIER) if self.identity else None,
                'problems': [problem.report() for problem in self.preflight()]}

    # ---- Execution -----------------------------------------------------------------------------------------

    def _run(self, label, argv):
        log = self.work / 'logs' / (label + '.log')
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
        self.logs.append(str(log))
        with os.fdopen(fd, 'wb') as stream:
            stream.write(('$ %s\n' % shlex.join(argv)).encode())
            stream.flush()
            code = self.runner(list(argv), str(self.work), self.env(), stream)
        if code != 0:
            raise Refused('command_failed', [log], {'label': label, 'exit_code': code})
        return log.read_text(errors='replace')

    def _signature(self, shown, designated):
        out = str(self.work / OUTPUT)
        block = dict(shown.get(out) or {})
        expected = self.identity.requirement(IDENTIFIER)
        problems = [] if block else ['not_displayed']
        if block:
            if block['identifier'] != IDENTIFIER:
                problems.append('identifier')
            if block['authorities'] != [self.identity.name]:
                problems.append('authority')
            if block['team'] not in (None, 'not set'):
                problems.append('team')
            if not block['cdhash'] or not re.fullmatch(r'[0-9a-f]{40}', block['cdhash']):
                problems.append('cdhash')
            if 'runtime' not in block['flags']:
                problems.append('hardened_runtime')
            if {'adhoc', 'linker-signed'} & set(block['flags']):
                problems.append('adhoc_flag')
            block['designated'] = (designated.get(out) or {}).get('designated')
            if not signing.same_requirement(block['designated'], expected):
                problems.append('designated_requirement')
        if problems:
            raise Refused('signature_unexpected', [out], {'problems': problems, 'shown': block})
        return block

    def execute(self):
        problems = self.preflight()
        if problems:
            raise problems[0]
        os.mkdir(self.work, 0o700)
        for name in FOLDERS:
            os.mkdir(self.work / name, 0o700)
        commands = dict(self.commands())
        versions = {label: self._run(label, commands[label]).splitlines()[1:2]
                    for label in ('swiftc-version', 'sdk-version')}
        source = self.source.read_bytes()
        write_new(self.work / SOURCE, source, 0o600)
        if sha(self.work / SOURCE) != hashlib.sha256(source).hexdigest() or sha(self.source) != sha(self.work / SOURCE):
            raise Refused('source_changed', [self.source], 'The source changed while it was copied.')
        out, first = self.work / OUTPUT, self.work / 'first' / 'pebble-screen-helper'
        for n in (1, 2):
            self._run('build-%d' % n, commands['build-%d' % n])
            if n == 1:
                os.rename(out, first)
        unsigned = sha(out)
        if unsigned != sha(first):
            raise Refused('not_deterministic', [first, out], {'first': sha(first), 'second': unsigned})
        for path in (first, out):
            data = path.read_bytes()
            found = [needle for needle in self.needles() if needle.encode() in data]
            if found:
                raise Refused('machine_paths_in_helper', [path], {'needles': found})
        relocked = False
        try:
            self.unlock()
            self._run('sign', commands['sign'])
        except signing.Refused as error:
            raise Refused('signing_keychain_locked', [str(self.identity.keychain)],
                          {'code': error.code, 'message': error.message}) from None
        finally:
            try:
                self.lock()
                relocked = True
            except signing.Refused:
                pass  # reported below; the keychain locks itself after LOCK_SECONDS anyway
        self._run('verify', commands['verify'])
        block = self._signature(signing.parse_codesign_blocks(self._run('display', commands['display'])),
                                signing.parse_codesign_blocks(self._run('requirements', commands['requirements'])))
        data = out.read_bytes()
        previous = self.previous() if self.replace else None
        replaces = ({'replaces': {key: previous.get(key) for key in ('sha256', 'cdhash', 'approved_stamp')}}
                    if previous is not None else {})
        manifest = {
            'format': signing.SCREEN_HELPER_FORMAT, 'identifier': IDENTIFIER, 'protocol': PROTOCOL,
            'path': signing.HELPER_FOLDER + '/' + signing.SCREEN_HELPER_BINARY,
            'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'cdhash': block['cdhash'],
            'designated_requirement': block['designated'], 'hardened_runtime': True, 'flags': block['flags'],
            'identity': {'name': self.identity.name, 'sha1': self.identity.sha1},
            'source': {'path': signing.SCREEN_HELPER_SOURCE, 'sha256': hashlib.sha256(source).hexdigest()},
            'unsigned_sha256': unsigned, 'deterministic': {'builds': 2, 'equal': True}, 'machine_paths': 0,
            'build': {'swiftc': (versions['swiftc-version'] or [''])[0].strip()[:200],
                      'sdk_version': (versions['sdk-version'] or [''])[0].strip()[:40], 'flags': list(SWIFTC_FLAGS),
                      'ldflags': list(LDFLAGS), 'source': SOURCE, 'output': OUTPUT,
                      'cwd': 'the work folder; LC_UUID depends on the output path, so a rebuild uses these paths'},
            'built_at': now(), 'approved_stamp': self.stamp, **replaces}
        changed = []
        try:
            existing = [name for name in PINNED if os.path.lexists(self.pinned / name)]
            for name in existing:  # --replace-pinned-helper: kept as evidence, never deleted
                os.rename(self.pinned / name, self.work / 'replaced' / name)
                changed.append('moved aside ' + name)
            write_new(self.pinned / signing.SCREEN_HELPER_BINARY, data, 0o755)
            changed.append('wrote ' + signing.SCREEN_HELPER_BINARY)
            write_new(self.pinned / signing.SCREEN_HELPER_MANIFEST, (json.dumps(manifest, indent=2) + '\n').encode(),
                      0o644)
            changed.append('wrote ' + signing.SCREEN_HELPER_MANIFEST)
            signing.check_pinned_helper(self.root, self.identity)  # the vault helper's folder still passes its check
            signing.check_pinned_screen_helper(self.root, self.identity)
        except (OSError, signing.Refused) as error:
            raise Refused('pinning_stopped', [self.pinned], {'changed': changed, 'error': repr(error)[:400]}) from None
        report = {'mode': 'execute', 'work': str(self.work), 'pinned': str(self.pinned), 'manifest': manifest,
                  'logs': self.logs, 'replaced': existing, 'keychain_locked_after_signing': relocked}
        write_new(self.work / 'build.json', (json.dumps(report, indent=2) + '\n').encode(), 0o600)
        shutil.rmtree(self.work / 'home', ignore_errors=True)  # the compiler's caches; the logs and builds stay
        shutil.rmtree(self.work / 'tmp', ignore_errors=True)
        return report


def main(argv=None, runner=run_command, home=None, unlock=None, lock=None):
    """home, unlock and lock are for tests: where the identity record is read (and the home folder whose path the
    helper must not carry), and stand-ins for the keychain."""
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', type=Path, default=TOOL_ROOT, help='The checkout (default: this one).')
    parser.add_argument('--identity', default='auto', help="auto (default) or the local identity's certificate SHA-1.")
    parser.add_argument('--work', type=Path, help='A new work folder (default <root>/.runtime/screen-helper-<stamp>).')
    parser.add_argument('--replace-pinned-helper', action='store_true',
                        help='Move an existing pinned screen helper aside and pin a new one.')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--i-am-the-parent-with-user-approval', dest='approval', metavar='STAMP')
    args = parser.parse_args(argv)
    if args.approval is not None and not STAMP.fullmatch(args.approval):
        parser.error('the approval stamp is a UTC stamp such as 20261026T030000Z')
    if args.execute and not args.approval:
        print(json.dumps({'refused': 'approval_required', 'paths': [],
                          'detail': '--execute needs --i-am-the-parent-with-user-approval STAMP'}, indent=2))
        return EXIT_USAGE
    try:
        if args.identity == 'adhoc':
            raise Refused('identity_required', [], 'The screen helper cannot be built ad hoc.')
        try:
            identity = signing.load_identity(home, None if args.identity == 'auto' else args.identity)
        except signing.Refused as error:
            raise Refused('signing_identity_unreadable', [error.facts.get('path', '')], error.message) from None
        build = ScreenHelperBuild(args.root, identity=identity, work=args.work, replace=args.replace_pinned_helper,
                                  stamp=args.approval, runner=runner, unlock=unlock, lock=lock, home=home)
        if not args.execute:
            result = build.plan()
            print(json.dumps(result, indent=2))
            return EXIT_REFUSED if result['problems'] else EXIT_OK
        result = build.execute()
    except Refused as error:
        print(json.dumps(error.report(), indent=2))
        return EXIT_STOPPED if error.code == 'pinning_stopped' else EXIT_REFUSED
    print(json.dumps(result, indent=2))
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
