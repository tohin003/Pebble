#!/usr/bin/env python3
"""Parent tool: build Pebble's Keychain vault helper once and pin it (Phase 22, k22-identity).

The helper (src-tauri/helpers/pebble-vault/pebble_vault.c) is the only code that reads and writes the installed
app's one vault item. The candidate builder copies the pinned artifact unchanged into every release, so its code
hash, and with it the Keychain's memory of "Always Allow", never changes (docs/HARNESS_SIGNING_IDENTITY.md). Build it
again only to rotate the identity or fix the helper: each rebuild costs every Mac one more Keychain prompt.

Dry run by default: prints every command (argv, cwd, environment) and every path it would write, and writes nothing.
--execute needs --i-am-the-parent-with-user-approval <stamp> and the local identity of scripts/pebble_signing.py.

Steps, all commands run in a new work folder (default <root>/.runtime/vault-helper-<stamp>):
1. Record `xcrun clang --version` and the SDK version.
2. Compile and link twice with one fixed command:
   - `clang -c` to obj/pebble_vault.o, then a link to out/pebble-vault without the linker's ad-hoc signature;
   - the first result is moved aside, and the two must be byte-identical.
   The linker derives LC_UUID from the content and the output path, so both builds use the same relative paths.
   -no_uuid is not used, because crash reporting and debuggers key on LC_UUID. Nothing else in the binary depends on
   the build: no __DATE__ or __TIME__, no debug information, no source path.
3. Unlock the identity keychain and sign out/pebble-vault:
   - identifier com.pebble.vault-helper;
   - the explicit designated requirement (identifier and certificate leaf);
   - the hardened runtime with no entitlements (no DYLD_* injection, library validation);
   - no timestamp.
   Then lock the keychain again, even when signing failed.
4. Check the signature: codesign --verify --strict; --display (identifier, cdhash, this identity as the only
   authority, no team, runtime, not ad hoc); and the designated requirement read back.
5. Write src-tauri/resources/helpers/pebble-vault (0755) and pebble-vault.json (0644), each new (O_EXCL).
   - pebble-vault.json records the SHA-256, the cdhash, the requirement, the signer, the source SHA-256 and the
     compiler.
   - An existing pinned pair is refused. With --replace-pinned-helper it is moved into the work folder first.
   - A replacement costs every Mac one Keychain prompt (D5), so it needs its own approval: a stamp equal to the
     pinned helper's approved_stamp is refused, and the new manifest records what it replaces (SHA-256, cdhash and
     approval stamp of the old pin).

Exit codes: 0 done (or a clean dry run), 2 usage, 3 refused before the pinned files changed, 4 stopped after.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOL = Path(__file__).resolve()
TOOL_ROOT = TOOL.parents[1]


def load_signing():
    """A private instance of scripts/pebble_signing.py (identity record, codesign argv, unlock and lock)."""
    spec = importlib.util.spec_from_file_location('vault_helper_signing', TOOL_ROOT / 'scripts' / 'pebble_signing.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


signing = load_signing()
XCRUN, CODESIGN = '/usr/bin/xcrun', signing.CODESIGN
SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
IDENTIFIER = signing.IDENTIFIERS['helper']
CFLAGS = ('-std=c11', '-Os', '-arch', 'arm64', '-mmacosx-version-min=12.0', '-Wall', '-Wextra', '-Werror',
          '-Wno-deprecated-declarations', '-fstack-protector-strong', '-D_FORTIFY_SOURCE=2')
LDFLAGS = ('-arch', 'arm64', '-mmacosx-version-min=12.0', '-Wl,-dead_strip', '-Wl,-no_adhoc_codesign',
           '-framework', 'Security', '-framework', 'CoreFoundation')
OBJECT, OUTPUT = 'obj/pebble_vault.o', 'out/pebble-vault'  # relative to the work folder, every command's cwd
FOLDERS = ('logs', 'home', 'tmp', 'obj', 'out', 'first', 'replaced')
STAMP = re.compile(r'\d{8}T\d{6}Z')
NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_STOPPED = 0, 2, 3, 4


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


class HelperBuild:
    def __init__(self, root, *, identity, work=None, replace=False, stamp=None, runner=run_command, unlock=None,
                 lock=None):
        self.root = Path(root).resolve()
        self.identity, self.replace, self.stamp, self.runner = identity, replace, stamp, runner
        work = Path(work) if work else self.root / '.runtime' / ('vault-helper-' + (stamp or '<stamp>'))
        self.work = Path(os.path.abspath(work)).parent.resolve() / Path(work).name
        self.unlock = unlock if unlock is not None else (lambda: signing.unlock(identity))
        self.lock = lock if lock is not None else (lambda: signing.lock(identity))
        self.source = self.root / signing.HELPER_SOURCE
        self.pinned = self.root / 'src-tauri/resources' / signing.HELPER_FOLDER
        self.logs = []

    def env(self):
        return {'PATH': SYSTEM_PATH, 'LANG': 'C', 'HOME': str(self.work / 'home'), 'TMPDIR': str(self.work / 'tmp') + '/'}

    def commands(self):
        out = self.work / OUTPUT
        compile_link = []
        for n in (1, 2):
            compile_link += [('compile-%d' % n, [XCRUN, 'clang', *CFLAGS, '-c', '-o', OBJECT, str(self.source)]),
                             ('link-%d' % n, [XCRUN, 'clang', *LDFLAGS, '-o', OUTPUT, OBJECT])]
        sign = (signing.sign_argv(self.identity, out, IDENTIFIER, runtime=True) if self.identity is not None
                else ['<no identity>'])
        return ([('clang-version', [XCRUN, 'clang', '--version']), ('sdk-version', [XCRUN, '--show-sdk-version'])]
                + compile_link + [('sign', sign), ('verify', [CODESIGN, '--verify', '--strict', '--verbose=2', str(out)]),
                                  ('display', [CODESIGN, '--display', '--verbose=4', str(out)]),
                                  ('requirements', [CODESIGN, '--display', '--requirements', '-', str(out)])])

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
            problems.append(Refused('identity_required', [], 'The helper serves only callers signed by its own '
                                    'certificate, so it must be signed with the local identity (pebble_signing.py).'))
        existing = [self.pinned / name for name in (signing.HELPER_BINARY, signing.HELPER_MANIFEST)
                    if os.path.lexists(self.pinned / name)]
        if existing and not self.replace:
            problems.append(Refused('pinned_helper_exists', existing, 'Rebuilding changes the helper\'s code hash, so '
                                    'every Mac asks once more for the vault item. Use --replace-pinned-helper only to '
                                    'rotate the identity or fix the helper.'))
        previous = self.previous() if existing and self.replace else None
        if previous is not None and self.stamp is not None and previous.get('approved_stamp') == self.stamp:
            problems.append(Refused('replacement_needs_new_approval', [self.pinned / signing.HELPER_MANIFEST],
                                    'A replacement costs every Mac one Keychain prompt (D5), so it needs its own '
                                    'approval stamp, not the one the pinned helper was built with.'))
        if os.path.lexists(self.pinned) and not self.pinned.is_dir():
            problems.append(Refused('pinned_folder_not_directory', [self.pinned]))
        if os.path.lexists(self.work):
            problems.append(Refused('work_exists', [self.work]))
        elif not self.work.parent.is_dir():
            problems.append(Refused('work_parent_missing', [self.work.parent]))
        return problems

    def previous(self):
        """The pinned helper's manifest, read leniently (None when absent or unreadable): what a replacement replaces."""
        try:
            doc = json.loads((self.pinned / signing.HELPER_MANIFEST).read_bytes())
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def plan(self):
        return {'mode': 'plan', 'tool': str(TOOL), 'root': str(self.root), 'work': str(self.work),
                'identity': self.identity.describe() if self.identity else None,
                'commands': [{'label': label, 'argv': argv, 'cwd': str(self.work), 'env': self.env()}
                             for label, argv in self.commands()],
                'local_steps': ['move out/pebble-vault and obj/pebble_vault.o to first/ after build 1',
                                'compare build 2 with build 1 byte for byte',
                                'unlock the identity keychain before sign; lock it after, even on failure',
                                'write the pinned helper and its manifest (O_EXCL)'],
                'writes': [str(self.work)] + [str(self.pinned / name) for name in
                                              (signing.HELPER_BINARY, signing.HELPER_MANIFEST)],
                'moves_aside_if_replacing': [str(self.pinned / name) for name in
                                             (signing.HELPER_BINARY, signing.HELPER_MANIFEST)] if self.replace else [],
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
        versions = {label: self._run(label, commands[label]).splitlines()[1:2] for label in ('clang-version', 'sdk-version')}
        out, first = self.work / OUTPUT, self.work / 'first' / 'pebble-vault'
        for n in (1, 2):
            self._run('compile-%d' % n, commands['compile-%d' % n])
            self._run('link-%d' % n, commands['link-%d' % n])
            if n == 1:
                os.rename(out, first)
                os.rename(self.work / OBJECT, self.work / 'first' / 'pebble_vault.o')
        unsigned = sha(out)
        if unsigned != sha(first):
            raise Refused('not_deterministic', [first, out], {'first': sha(first), 'second': unsigned})
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
            'format': signing.HELPER_FORMAT, 'identifier': IDENTIFIER,
            'path': signing.HELPER_FOLDER + '/' + signing.HELPER_BINARY,
            'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'cdhash': block['cdhash'],
            'designated_requirement': block['designated'], 'hardened_runtime': True, 'flags': block['flags'],
            'identity': {'name': self.identity.name, 'sha1': self.identity.sha1},
            'source': {'path': signing.HELPER_SOURCE, 'sha256': sha(self.source)},
            'unsigned_sha256': unsigned, 'deterministic': {'builds': 2, 'equal': True},
            'build': {'clang': (versions['clang-version'] or [''])[0].strip()[:200],
                      'sdk_version': (versions['sdk-version'] or [''])[0].strip()[:40], 'cflags': list(CFLAGS),
                      'ldflags': list(LDFLAGS), 'object': OBJECT, 'output': OUTPUT,
                      'cwd': 'the work folder; LC_UUID depends on the output path, so a rebuild uses these paths'},
            'built_at': now(), 'approved_stamp': self.stamp, **replaces}
        changed = []
        try:
            existing = [name for name in (signing.HELPER_BINARY, signing.HELPER_MANIFEST)
                        if os.path.lexists(self.pinned / name)]
            for name in existing:  # --replace-pinned-helper: kept as evidence, never deleted
                os.rename(self.pinned / name, self.work / 'replaced' / name)
                changed.append('moved aside ' + name)
            if not self.pinned.is_dir():
                os.mkdir(self.pinned, 0o755)
                changed.append('created ' + signing.HELPER_FOLDER)
            write_new(self.pinned / signing.HELPER_BINARY, data, 0o755)
            changed.append('wrote ' + signing.HELPER_BINARY)
            write_new(self.pinned / signing.HELPER_MANIFEST, (json.dumps(manifest, indent=2) + '\n').encode(), 0o644)
            changed.append('wrote ' + signing.HELPER_MANIFEST)
            signing.check_pinned_helper(self.root, self.identity)
        except (OSError, signing.Refused) as error:
            raise Refused('pinning_stopped', [self.pinned], {'changed': changed, 'error': repr(error)[:400]}) from None
        report = {'mode': 'execute', 'work': str(self.work), 'pinned': str(self.pinned), 'manifest': manifest,
                  'logs': self.logs, 'replaced': existing, 'keychain_locked_after_signing': relocked}
        write_new(self.work / 'build.json', (json.dumps(report, indent=2) + '\n').encode(), 0o600)
        return report


def main(argv=None, runner=run_command, home=None, unlock=None, lock=None):
    """home, unlock and lock are for tests: where the identity record is read, and stand-ins for the keychain."""
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', type=Path, default=TOOL_ROOT, help='The checkout (default: this one).')
    parser.add_argument('--identity', default='auto', help="auto (default) or the local identity's certificate SHA-1.")
    parser.add_argument('--work', type=Path, help='A new work folder (default <root>/.runtime/vault-helper-<stamp>).')
    parser.add_argument('--replace-pinned-helper', action='store_true',
                        help='Move an existing pinned helper aside and pin a new one (identity rotation or a fix).')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--i-am-the-parent-with-user-approval', dest='approval', metavar='STAMP')
    args = parser.parse_args(argv)
    if args.approval is not None and not STAMP.fullmatch(args.approval):
        parser.error('the approval stamp is a UTC stamp such as 20261014T093000Z')
    if args.execute and not args.approval:
        print(json.dumps({'refused': 'approval_required', 'paths': [],
                          'detail': '--execute needs --i-am-the-parent-with-user-approval STAMP'}, indent=2))
        return EXIT_USAGE
    try:
        if args.identity == 'adhoc':
            raise Refused('identity_required', [], 'The helper cannot be built ad hoc.')
        try:
            identity = signing.load_identity(home, None if args.identity == 'auto' else args.identity)
        except signing.Refused as error:
            raise Refused('signing_identity_unreadable', [error.facts.get('path', '')], error.message) from None
        build = HelperBuild(args.root, identity=identity, work=args.work, replace=args.replace_pinned_helper,
                            stamp=args.approval, runner=runner, unlock=unlock, lock=lock)
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
