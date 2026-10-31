#!/usr/bin/env python3
"""Install, check or remove Pebble for Chrome's native messaging host (Phase 22, b22-browser).

Subcommands: plan (the default), install, uninstall and status. The parent runs them
after the person approves (docs/HARNESS_BROWSER_EXTENSION.md §5). From Phase 24 (V1-043)
the person can also do it in Pebble (Details > Browser > Connect Chrome); both build the
same bytes (backend/pebble/native_host_setup.py), so each one's status reads the other's
files as installed.

What install writes, and nothing else:
- <data home>/chrome/pebble-native-host, a 0700 launcher that runs Pebble's backend
  as the host (`pebble-backend --browser-native-host --home <data home>`) with a
  cleared environment;
- ~/Library/Application Support/<browser>/NativeMessagingHosts/com.pebble.browser.json,
  the host manifest, whose allowed_origins names only the pinned extension ID.
  Phase 43 (1.9.0, Connect a browser): --browser takes every browser Pebble connects
  (browser_bridge.BROWSER_FOLDERS: chrome, comet, brave, edge, arc, chromium, chrome-beta,
  chrome-canary); status reports each of them.

Safety model (as scripts/pebble_install.py):
- Dry run by default: every subcommand checks its read-only preconditions and prints,
  as JSON, the exact actions it would take. --execute acts.
- --root DIR re-bases the person's home under DIR/home and /Applications under
  DIR/Applications; Path.home() is then never consulted.
- Without --root, --execute also needs --i-am-the-parent-with-user-approval naming
  --stamp. That check runs first, before anything is read.
- A manifest or launcher this tool did not write is never overwritten or removed:
  it is backed up (O_EXCL, into <data home>/chrome/backups/<stamp>/) and, for
  uninstall, left alone. Every write is a temporary file, fsync and rename, then
  read back.
- It never edits Chrome's preferences, never installs or enables the extension (the
  person loads it unpacked), and never reads a credential.

Exit codes: 0 done (or a dry run whose preconditions hold), 2 usage, 3 refused before
any change, 4 stopped after a change (the report's recovery says what to do).
"""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'backend') not in sys.path:
    sys.path.insert(0, str(ROOT / 'backend'))

from pebble.browser_bridge import (BRIDGE_DIR, BROWSER_FOLDERS, EXTENSION_ORIGIN, HOST_NAME, HOST_SCRIPT,  # noqa: E402
                                   decode_body, FrameError)
from pebble import native_host_setup as setup  # noqa: E402  (Phase 24: one builder for the launcher and manifest)

IDENTIFIER = 'com.pebble.local'
DATA_HOME = 'Library/Application Support/' + IDENTIFIER       # main.rs app_data_dir (before Phase 22)
# Phase 22 (s22-storage): the shell passes --home ~/.pebble and the backend moves the old data home into its data/
# folder once, verified. Without --home this tool uses that folder when it holds the moved database (merge, i22).
PEBBLE_DATA = '.pebble/data'
DATABASE = 'pebble.sqlite'
BACKEND = 'Applications/Pebble.app/Contents/Resources/resources/backend/pebble-backend'
SUPPORT = 'Library/Application Support'
RUN_FORMAT, LAUNCHER_MARK = 'pebble.native-host-run/1', setup.LAUNCHER_MARK
DESCRIPTION = setup.DESCRIPTION
STAMP = re.compile(r'\d{8}T\d{6}Z')
CONTROL = re.compile(r'[\x00-\x1f\x7f]')
EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_STOPPED = 0, 2, 3, 4
MAX_MANIFEST = 65_536
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


TOOL_SHA = _sha(Path(__file__).read_bytes())


class Stop(Exception):
    def __init__(self, code, message, **facts):
        super().__init__(f'{code}: {message}')
        self.code, self.message, self.facts = code, message, facts


class Layout:
    """Every path this tool touches, re-based under --root when given."""

    def __init__(self, root, home=None, backend=None, browsers=('chrome',), dev_python=None, dev_source=None):
        self.root = Path(root) if root is not None else None
        user = (self.root / 'home') if self.root is not None else Path.home()
        apps = (self.root / BACKEND) if self.root is not None else Path('/') / BACKEND
        self.user_home = user
        self.given_home = bool(home)
        self.data_home = Path(home) if home else user / DATA_HOME
        if self.root is not None and home:
            self.data_home = self.root / Path(home).relative_to('/') if Path(home).is_absolute() else self.root / home
        self.backend = Path(backend) if backend else apps
        self.dev_python = Path(dev_python) if dev_python else None
        self.dev_source = Path(dev_source) if dev_source else None
        self.browsers = tuple(dict.fromkeys(browsers))
        self._derive()

    def _derive(self):
        self.bridge = self.data_home / BRIDGE_DIR
        self.launcher = self.bridge / HOST_SCRIPT
        self.backups = self.bridge / 'backups'

    def settle(self):
        """Without --home: ~/.pebble/data when it is a plain folder holding Pebble's database (the Phase 22 move
        happened), else the old app-data folder. gate() calls this only after the approval check, so nothing is read
        before it holds. Pass --home when Pebble kept its old folder although ~/.pebble/data holds a database."""
        if self.given_home:
            return
        moved = self.user_home / PEBBLE_DATA
        try:
            folder, database = os.lstat(moved), os.lstat(moved / DATABASE)
        except OSError:
            return
        if stat.S_ISDIR(folder.st_mode) and stat.S_ISREG(database.st_mode):
            self.data_home = moved
            self._derive()

    def support(self, browser):
        return self.user_home / SUPPORT / BROWSER_FOLDERS[browser]

    def manifest(self, browser):
        return self.support(browser) / 'NativeMessagingHosts' / f'{HOST_NAME}.json'

    def describe(self):
        return {'root': str(self.root) if self.root else None, 'data_home': str(self.data_home),
                'launcher': str(self.launcher), 'backend': str(self.backend),
                'dev': {'python': str(self.dev_python), 'source': str(self.dev_source)} if self.dev_python else None,
                'manifests': {b: str(self.manifest(b)) for b in self.browsers}}


def launcher_text(layout):
    """The launcher: a cleared environment, then Pebble's backend in host mode with this data home."""
    if layout.dev_python is not None:
        program = [layout.dev_python, '-E', '-s', layout.dev_source / 'backend/main.py']
    else:
        program = [layout.backend]
    return setup.launcher_text(program, layout.data_home)


def manifest_doc(layout):
    return setup.manifest_doc(layout.launcher)


def manifest_bytes(layout):
    return setup.manifest_bytes(layout.launcher)


def _read_small(path):
    """Bytes of a regular file that is not a link, or None when absent; Stop when it is anything else."""
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        raise Stop('path_not_regular', 'A path is a link or cannot be read; nothing was changed.', path=str(path))
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MANIFEST:
            raise Stop('path_not_regular', 'A path is not a small regular file; nothing was changed.', path=str(path))
        return handle.read()


def _ours(raw, layout):
    """True for a manifest this tool wrote for this launcher."""
    try:
        doc = decode_body(raw)
    except FrameError:
        return False
    return doc.get('name') == HOST_NAME and doc.get('path') == str(layout.launcher)


class Run:
    def __init__(self, command, layout, execute, options, clock=time.time):
        self.command, self.layout, self.execute, self.options, self.clock = command, layout, execute, options, clock
        self.started, self.checks, self.actions, self.results, self.recovery = clock(), [], [], {}, []
        self.changed = False

    def check(self, name, ok, message, **facts):
        self.checks.append({'check': name, 'ok': bool(ok), **facts})
        if not ok:
            raise Stop(name, message, **facts)

    def act(self, action, fn=None, **facts):
        self.actions.append({'action': action, **{k: str(v) for k, v in facts.items()}})
        if self.execute and fn is not None:
            result = fn()
            self.changed = True
            return result
        return None


def gate(run):
    """Real paths need the parent's approval naming the stamp; a staged root must stay inside itself."""
    o, layout = run.options, run.layout
    if layout.root is None:
        if run.execute:
            stamp = o.get('stamp')
            run.check('stamp_given', stamp is not None, 'Real paths need an explicit --stamp.')
            run.check('parent_approval', o.get('approval') == stamp, 'Real paths need --i-am-the-parent-with-user-approval '
                      'naming this stamp, given only after the person approved.', expected_stamp=stamp,
                      given=o.get('approval'))
        layout.settle()
        return
    root = Path(layout.root)
    run.check('staged_root_plain', not os.path.islink(root) and root.is_dir(), 'A staged --root must be a plain folder.',
              path=str(root))
    layout.settle()  # then the settled data home is held to the staged root like every other path
    base = os.path.realpath(root)
    paths = [layout.data_home, layout.backend, *(layout.manifest(b) for b in layout.browsers)]
    for path in paths:
        probe = root
        for part in Path(path).relative_to(root).parts:
            probe = probe / part
            run.check('staged_path_has_no_link', not os.path.islink(probe), 'A staged path holds a link; nothing was '
                      'changed.', path=str(probe))
        found = Path(path)
        while not os.path.lexists(found):
            found = found.parent
        run.check('staged_path_inside_root', os.path.commonpath([base, os.path.realpath(found)]) == base,
                  'A staged path leaves the root; nothing was changed.', path=str(path))


def _private_dir(path):
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Stop('bridge_folder_not_private', "Pebble's Chrome folder is not private; nothing more was changed.",
                   path=str(path))


def _write(path, data, mode):
    """A temporary file in the same folder, fsync, chmod, rename; then read back."""
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{int(time.time() * 1e6)}')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    if _read_small(path) != data:
        raise Stop('readback_mismatch', 'A file did not read back as written.', path=str(path))


def _backup(run, path, data, stamp):
    folder = run.layout.backups / stamp
    target = folder / path.name

    def save():
        os.makedirs(run.layout.backups, mode=0o700, exist_ok=True)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    run.act('backup', save, source=path, backup=target, sha256=_sha(data))
    run.results.setdefault('backups', []).append({'source': str(path), 'backup': str(target), 'sha256': _sha(data)})


def _program_ready(run):
    layout = run.layout
    if layout.dev_python is not None:
        run.check('dev_python_executable', os.access(layout.dev_python, os.X_OK), 'The development Python is not '
                  'executable.', path=str(layout.dev_python))
        run.check('dev_source_present', (layout.dev_source / 'backend/main.py').is_file(), 'The development source has '
                  'no backend/main.py.', path=str(layout.dev_source))
    else:
        run.check('backend_executable', layout.backend.is_file() and os.access(layout.backend, os.X_OK),
                  "Pebble's backend is not installed where the launcher expects it.", path=str(layout.backend))


def do_install(run):
    layout, stamp = run.layout, run.options['stamp']
    run.check('data_home_exists', layout.data_home.is_dir() and not layout.data_home.is_symlink(),
              "Pebble's data folder does not exist yet; open Pebble once first.", path=str(layout.data_home))
    _program_ready(run)
    for browser in layout.browsers:
        run.check('browser_support_exists', layout.support(browser).is_dir(), f'{browser} has not been run for this user '
                  "(its support folder is missing); open it once first.", browser=browser, path=str(layout.support(browser)))
        hosts = layout.manifest(browser).parent
        run.check('hosts_folder_plain', not os.path.islink(hosts) and (not os.path.lexists(hosts) or hosts.is_dir()),
                  'The NativeMessagingHosts folder is a link or not a folder.', path=str(hosts))
    script = launcher_text(layout).encode('utf-8')
    run.act('ensure_private_folder', lambda: _private_dir(layout.bridge), path=layout.bridge)
    old = _read_small(layout.launcher)
    if old is not None and old != script:
        _backup(run, layout.launcher, old, stamp)
    if old != script:
        run.act('write_launcher', lambda: _write(layout.launcher, script, 0o700), path=layout.launcher,
                sha256=_sha(script))
    data = manifest_bytes(layout)
    for browser in layout.browsers:
        path = layout.manifest(browser)
        current = _read_small(path)
        if current == data:
            run.results.setdefault('unchanged', []).append(str(path))
            continue
        if current is not None:
            _backup(run, path, current, stamp)
        run.act('ensure_hosts_folder', lambda p=path.parent: os.makedirs(p, mode=0o755, exist_ok=True), path=path.parent)
        run.act('write_manifest', lambda p=path: _write(p, data, 0o644), path=path, sha256=_sha(data))
    run.results.update(manifest=manifest_doc(layout), launcher_sha256=_sha(script), manifest_sha256=_sha(data))
    run.recovery = [f'{sys.executable} scripts/install_native_host.py uninstall --stamp {stamp} --execute'
                    + (f' --root {layout.root}' if layout.root else f' --i-am-the-parent-with-user-approval {stamp}')]


def do_uninstall(run):
    layout, stamp = run.layout, run.options['stamp']
    found = []
    for browser in layout.browsers:  # every check first, so a foreign manifest stops the run before any change
        path = layout.manifest(browser)
        current = _read_small(path)
        if current is None:
            run.results.setdefault('absent', []).append(str(path))
            continue
        run.check('manifest_is_ours', _ours(current, layout), 'That host manifest was not written by this tool for this '
                  'launcher, so nothing was changed.', path=str(path))
        found.append((path, current))
    script = _read_small(layout.launcher)
    if script is not None:
        lines = script.splitlines()
        run.check('launcher_is_ours', len(lines) > 1 and lines[1].startswith(LAUNCHER_MARK.encode()),
                  'That launcher was not written by this tool, so nothing was changed.', path=str(layout.launcher))
    for path, current in found:
        _backup(run, path, current, stamp)
        run.act('remove_manifest', lambda p=path: os.unlink(p), path=path)
    if script is not None:
        run.act('remove_launcher', lambda: os.unlink(layout.launcher), path=layout.launcher)
    if run.execute:
        gone = all(not os.path.lexists(layout.manifest(b)) for b in layout.browsers)
        run.check('readback_absent', gone, 'A manifest is still present after removal.')


def do_status(run):
    layout = run.layout
    rows = {}
    for browser in BROWSER_FOLDERS:
        path = layout.manifest(browser)
        try:
            current = _read_small(path)
        except Stop:
            rows[browser] = {'state': 'not_a_file'}
            continue
        if current is None:
            rows[browser] = {'state': 'absent'}
        elif current == manifest_bytes(layout):
            rows[browser] = {'state': 'installed'}
        else:
            rows[browser] = {'state': 'ours_but_different' if _ours(current, layout) else 'foreign', 'sha256': _sha(current)}
    script = _read_small(layout.launcher)
    run.results.update(manifests=rows, launcher='absent' if script is None else
                       'current' if script == launcher_text(layout).encode() else 'different',
                       extension_folder=str(ROOT / 'browser-extension'), extension_origin=EXTENSION_ORIGIN)


COMMANDS = {'plan': do_install, 'install': do_install, 'uninstall': do_uninstall, 'status': do_status}


def perform(command, layout, execute, options, clock=time.time):
    run = Run(command, layout, execute and command not in ('plan', 'status'), options, clock)
    error = None
    try:
        gate(run)  # first: on real paths nothing is read before the approval holds
        COMMANDS[command](run)
    except Stop as stop:
        error = stop
    state = ('refused' if not run.changed else 'stopped') if error else ('done' if run.execute else 'dry_run')
    report = {'format': RUN_FORMAT, 'tool': 'scripts/install_native_host.py', 'tool_sha256': TOOL_SHA, 'command': command,
              'execute': run.execute, 'state': state, 'ok': error is None, 'layout': layout.describe(),
              'started_at': datetime.fromtimestamp(run.started, timezone.utc).isoformat(),
              'checks': run.checks, 'actions': run.actions, 'results': run.results, 'recovery': run.recovery,
              'never': ['edits Chrome preferences', 'installs or enables the extension', 'reads a credential'],
              'error': None if error is None else {'code': error.code, 'message': error.message, **error.facts}}
    code = EXIT_OK if error is None else EXIT_STOPPED if run.changed else EXIT_REFUSED
    return report, code


def _stamp_arg(value):
    if not STAMP.fullmatch(value):
        raise argparse.ArgumentTypeError('use a UTC stamp such as 20261014T120000Z')
    return value


def _path_arg(value):
    if not value or CONTROL.search(value):
        raise argparse.ArgumentTypeError('use a path without control characters')
    return os.path.abspath(value)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', nargs='?', default='plan', choices=sorted(COMMANDS))
    parser.add_argument('--root', type=_path_arg, help='staged root: ~ -> ROOT/home, /Applications -> ROOT/Applications')
    parser.add_argument('--execute', action='store_true', help='act; without it nothing changes')
    parser.add_argument('--stamp', type=_stamp_arg, help='names the backups and the approval')
    parser.add_argument('--i-am-the-parent-with-user-approval', dest='approval', metavar='STAMP', type=_stamp_arg,
                        help='required with --execute on real paths; must name --stamp')
    parser.add_argument('--browser', dest='browsers', action='append', choices=sorted(BROWSER_FOLDERS),
                        help='repeatable; default chrome')
    parser.add_argument('--home', type=_path_arg, help="Pebble's data folder (default ~/" + PEBBLE_DATA + ' once Pebble '
                        'moved its data there, else ~/' + DATA_HOME + ')')
    parser.add_argument('--backend', type=_path_arg, help='the pebble-backend executable (default the installed app)')
    parser.add_argument('--dev-python', type=_path_arg, help='development: run backend/main.py with this Python')
    parser.add_argument('--dev-source', type=_path_arg, help='development: the source checkout holding backend/main.py')
    return parser


def main(argv=None, out=None, clock=time.time):
    out = out or sys.stdout
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else EXIT_USAGE
    if (args.dev_python is None) != (args.dev_source is None):
        out.write(json.dumps({'ok': False, 'error': {'code': 'usage', 'message': 'Give --dev-python and --dev-source '
                                                                                'together.'}}) + '\n')
        return EXIT_USAGE
    stamp = args.stamp or datetime.fromtimestamp(clock(), timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    layout = Layout(args.root, args.home, args.backend, tuple(args.browsers or ('chrome',)), args.dev_python,
                    args.dev_source)
    options = {'stamp': stamp if (args.stamp or args.root or not args.execute) else None, 'approval': args.approval}
    report, code = perform(args.command, layout, args.execute, options, clock)
    report['results'].setdefault('stamp', stamp)
    out.write(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + '\n')
    return code


if __name__ == '__main__':
    sys.exit(main())
