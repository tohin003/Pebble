#!/usr/bin/env python3
"""Back up, rehearse, install, read back and roll back Pebble on this Mac (D20 §2.3-§2.7, §6.7).

Subcommands: plan, backup, rehearse, install, readback, rollback and drill. The
parent runs them in the order of docs/HARNESS_INSTALL_RUNBOOK.md. Two more only
read and print: backups (every backup root and what it can still do) and purge
(everything a full removal of Pebble touches; it never deletes anything).

Safety model:
- Dry run by default. Each subcommand checks its read-only preconditions and
  prints, as JSON, the exact actions it would take, changing nothing. --execute acts.
- --root DIR re-bases every absolute path under a staged root: /Applications
  becomes DIR/Applications and the user's home becomes DIR/home (the data home,
  the webview store and ~/PebbleBackups). Path.home() is then never consulted.
- Without --root, --execute also needs --i-am-the-parent-with-user-approval
  naming the backup stamp, and every --quiet-file must be quiet.
- Every step re-checks its preconditions and stops at the first mismatch, naming
  the exact paths. Nothing is deleted: a replaced bundle or data home is renamed
  into the backup root with a stamp (never over an existing path), and every
  record is a new file opened with O_EXCL.
- "Pebble is running" means a process whose executable lies inside the target
  bundle, read by exact path from libproc. Nothing is ever signalled; quitting
  is the user's action, and the tool waits (bounded) and reports.
- The live data home is never opened by SQLite while its service is stopped. A
  read-only open of a WAL database still creates -wal and -shm files, so the
  backup checks integrity on its copy with mode=ro&immutable=1 and runs
  migrations.inspect on a scratch copy of the database.

- Phase 22 (~/.pebble): backup also copies ~/.pebble when it exists and inspects
  whichever home holds the live database. --data-layout pebble-home rehearses and
  drills the one-time move on temporary homes (the real ~/.pebble and ~/Documents
  are write-protected in the rehearsal sandbox) and reads back both homes after an
  install. Rollback of a backup taken while the old data home was live restores the
  old app and the old data home and leaves ~/.pebble untouched.
- 1.0 (V1-006, V1-044): rollback of a backup taken while ~/.pebble/data was live
  moves ~/.pebble aside, restores it from the backup, moves the restored Documents
  copy index (runtime/mirror.json) aside so newer Memory files are never read as
  deletions, restores the app and the webview store, and leaves the old folder
  untouched. drill --source-pebble-home stages ~/.pebble as 1.0 finds it.

Exit codes: 0 done (or a dry run whose preconditions hold), 2 usage, 3 refused
before any change, 4 stopped after a change (the report's recovery says what to do).
"""

import argparse
import ctypes
import errno
import hashlib
import json
import os
import plistlib
import re
import selectors
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'backend') not in sys.path:
    sys.path.insert(0, str(ROOT / 'backend'))

from pebble import migrations  # noqa: E402  (inspect, verify_backup and restore_copy, read-only)
# T3 (the trust channel) is the authority for pebble.release/1: its shape, its schema and its signed verifier.
from pebble import attestation  # noqa: E402
from pebble.release_readiness import (EvidenceError, Missing, Root, Unsafe, bundle_digest,  # noqa: E402
                                      bundle_entries, parse_json, read_explicit, read_manifest, redact,
                                      run_bounded, trusted_tool)

IDENTIFIER, APP, EXECUTABLE = 'com.pebble.local', 'Pebble.app', 'pebble'
PLIST, SHELL = 'Contents/Info.plist', 'Contents/MacOS/pebble'
RESOURCES = 'Contents/Resources/resources'
BACKEND = RESOURCES + '/backend/pebble-backend'
DATA_HOME = 'Library/Application Support/' + IDENTIFIER   # main.rs:247 app_data_dir
WEBVIEW = '~/Library/WebKit/' + IDENTIFIER                # D20 §8 open check 3: confirm on this Mac
BACKUPS = 'PebbleBackups'
# V1-137, V1-173: a new backup root is '<stamp>-pre-install.noindex' (Spotlight skips a .noindex folder) and keeps the
# bundle as 'Pebble.app.backup', which is not a launchable bundle. Roots named '<stamp>-pre-phase21' with their bundle
# as 'Pebble.app' (Phases 21-23) still load: backup.json's app.copy names the bundle copy.
SUFFIX, LEGACY_SUFFIX = '-pre-install.noindex', '-pre-phase21'
BACKUP_NAME = re.compile(r'(\d{8}T\d{6}Z)(' + re.escape(SUFFIX) + '|' + re.escape(LEGACY_SUFFIX) + ')')
APP_COPY, LEGACY_APP_COPY, DATA_COPY, WEBVIEW_COPY = APP + '.backup', APP, IDENTIFIER, 'webview-store'
# Phase 22 (s22-storage): the shell's home is ~/.pebble. A Phase 22 backend moves the old data home into
# ~/.pebble/data once (backend/pebble/data_home.py) and writes only MOVED.md and pebble-moved.json into the old
# folder, which it never deletes. --data-layout pebble-home rehearses, drills and reads back that move; the default
# (legacy) is every earlier phase's behaviour, unchanged. A backup always copies ~/.pebble too when it exists.
PEBBLE_HOME, PEBBLE_COPY = '.pebble', 'pebble-home'
DATA_LAYOUTS = ('legacy', 'pebble-home')
MOVE_RECEIPT, MOVE_STATUS = '.pebble-moved-here.json', 'runtime/migration.json'
MOVE_FILES = frozenset({'MOVED.md', 'pebble-moved.json'})
# backend/pebble/data_mirror.py: the Documents copy index. A rollback moves the restored one aside (V1-006), so the
# restored Pebble starts a new index and never reads newer Memory files in ~/Documents/Pebble as deletions.
MIRROR_INDEX = 'runtime/mirror.json'
# What reading the old home may add beside its database: SQLite's sidecars and the service and upgrade locks.
READ_TOUCH = frozenset({'pebble.sqlite-wal', 'pebble.sqlite-shm', 'runtime-owner.lock', 'upgrade-owner.lock'})
MOVED, INSPECTION = APP + '.moved', 'inspection'
REHEARSAL_PARENT = Path('/private/tmp')
# V1-173: rehearsal and drill folders; purge also lists the Phase 21-23 names.
REHEARSAL_PREFIX, DRILL_PREFIX = 'pebble-rehearsal-', 'pebble-drill-'
SCRATCH_PREFIXES = (REHEARSAL_PREFIX, DRILL_PREFIX, 'pebble-p21-rehearsal-', 'pebble-p21-drill-')
LOGS = ('service.log', 'backend.log')
TRACEBACK = b'Traceback (most recent call last)'
# backend/main.py logs every handled request failure as 'ERROR:root:Request failed: <method>' followed by its traceback
# (logging.exception); those are answered errors, not crashes, so readback counts only the other tracebacks
# (Phase 21: the drill's own upgrade_backups probe, answered 'Unknown request.', had tripped the check).
HANDLED_TRACEBACK = re.compile(rb'^ERROR:root:Request failed: [^\n]*\n' + re.escape(TRACEBACK), re.M)
EXPECTED_EPOCH = migrations.SCHEMA_EPOCH
RUN_FORMAT, BACKUP_FORMAT = 'pebble.install-run/1', 'pebble.install-backup/1'
RELEASE_FORMAT, ENVELOPE_FORMAT = attestation.RELEASE_FORMAT, attestation.FORMAT
# Merge (i20): exactly T3's pebble.release/1 fields (attestation.RELEASE_KEYS), never a copy; sorted for reports.
RELEASE_KEYS = tuple(sorted(attestation.RELEASE_KEYS - {'format'}))
STAMP = re.compile(r'\d{8}T\d{6}Z')
HEX64 = re.compile(r'[0-9a-f]{64}')
VERSION = re.compile(r'\d{1,6}(?:\.\d{1,6}){0,3}')
LABEL = re.compile(r'[A-Za-z0-9_.-]{1,80}')
CONTROL = re.compile(r'[\x00-\x1f\x7f]')
MAX_FILE, MAX_RECORD, MAX_LOG_SCAN, MAX_OUTPUT = 16 << 30, 256_000_000, 32 << 20, 64 << 20
EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_STOPPED = 0, 2, 3, 4
DITTO, CODESIGN, SANDBOX, LIBPROC = '/usr/bin/ditto', '/usr/bin/codesign', '/usr/bin/sandbox-exec', '/usr/lib/libproc.dylib'
COPY_SECONDS, CODESIGN_SECONDS = 7200, 900
# The shell's backend environment allowlist (main.rs:258); TMPDIR is replaced by the rehearsal's own.
ENV_KEYS = ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR', '__CF_USER_TEXT_ENCODING')
KEYCHAIN_SERVICES = ('com.apple.SecurityServer', 'com.apple.securityd', 'com.apple.secd')
PEBBLE_NAMES = ('pebble', 'pebble-backend', 'llama-server')
# backend/main.py modes that share the bundled pebble-backend with the --home service.
WORKER_FLAGS = frozenset({'--export-worker', '--job-anchor', '--job-shim', '--job-canary', '--extract-pdf',
                          '--extract-image', '--extract-file', '--hook-worker', '--mod-worker', '--mcp-http-worker',
                          '--mcp-stdio-worker', '--mcp-schema-worker', '--mcp-media-image-worker', '--ax-worker',
                          '--browser-native-host'})  # Phase 22: Chrome's native messaging host (Pebble for Chrome)
REQUESTS = ({'id': 'install-rehearsal-state', 'method': 'state', 'params': {}},
            {'id': 'install-rehearsal-upgrade-backups', 'method': 'upgrade_backups', 'params': {}})
# A started App answers upgrade_backups with this; only Maintenance serves that route (maintenance.py:17).
KNOWN_ERRORS = frozenset({'Unknown request.'})
TOUCH = 'A read-only SQLite open can create -wal/-shm files when no service holds them; a dry run skips it.'
TREE_KEYS = ('mode', 'files', 'dirs', 'links')
_NOFOLLOW, _CLOEXEC = getattr(os, 'O_NOFOLLOW', 0), getattr(os, 'O_CLOEXEC', 0)
_RENAME_EXCL = 0x00000004  # <stdio.h> renamex_np: fail with EEXIST instead of replacing


def _sha_file(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


TOOL_SHA, MIGRATIONS_SHA = _sha_file(__file__), _sha_file(migrations.__file__)


class Stop(Exception):
    """A precondition or verification failed; nothing after it runs."""

    def __init__(self, code, message, **facts):
        super().__init__(f'{code}: {message}')
        self.code, self.message, self.facts = code, message, facts


class CopyFailed(Exception):
    pass


# ---- Small helpers ------------------------------------------------------------------------------


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc)


def stamp_at(seconds):
    return utc(seconds).strftime('%Y%m%dT%H%M%SZ')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def encode(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + '\n').encode()


def pick(mapping, *keys):
    return {key: mapping.get(key) for key in keys}


def plain(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def printable(value, limit=64, keep=('checks', 'actions')):
    """The run report as printed: long lists (file manifests) become counts; records keep them."""
    if isinstance(value, dict):
        return {key: [printable(item, limit, ()) for item in item_list] if key in keep and isinstance(item_list, list)
                else printable(item_list, limit, ()) for key, item_list in value.items()}
    if isinstance(value, list):
        return [printable(item, limit, ()) for item in value] if len(value) <= limit else {'omitted_items': len(value)}
    return value


def strict_json(data, what):
    """Strict JSON for this tool's own records, which can exceed the evidence parser's 8 MB bound."""
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate key')
            out[key] = value
        return out

    def constant(token):
        raise ValueError('non-finite number')
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise Stop('record_malformed', 'A record is not strict JSON.', record=what) from None


def write_new(path, value):
    """Write a new 0600 file. An existing path, including a link, is refused and never replaced."""
    data = encode(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    return digest(data)


def write_text_new(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)


def mkdir_new(path):
    os.mkdir(path, 0o700)  # FileExistsError: never an existing directory
    os.chmod(path, 0o700)


def _renamex():
    if sys.platform != 'darwin':
        return None
    try:
        function = ctypes.CDLL(None, use_errno=True).renamex_np
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    return function


def rename_new(source, destination):
    """Rename without ever replacing: renamex_np(RENAME_EXCL) on macOS, else a checked rename."""
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, 'destination exists', str(destination))
    function = _renamex()
    if function is None:
        os.rename(source, destination)
        return
    if function(os.fsencode(source), os.fsencode(destination), _RENAME_EXCL) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(source))


def existing(path):
    path = Path(path)
    while not os.path.lexists(path) and path != path.parent:
        path = path.parent
    return path


def overlaps(a, b):
    a, b = os.path.realpath(a), os.path.realpath(b)
    return os.path.commonpath([a, b]) in (a, b)


def _forms(path):
    forms = {os.path.abspath(path), os.path.realpath(path)}
    return forms | {'/System/Volumes/Data' + form for form in forms if not form.startswith('/System/Volumes/Data/')}


def _fold(text):
    return unicodedata.normalize('NFC', text).casefold()


# ---- Layout -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    root: object          # the staged root (a Path), or None for this Mac's real paths
    user_home: Path
    applications: Path
    webview_rel: str

    @classmethod
    def make(cls, root=None, webview=WEBVIEW):
        parts = webview.split('/') if isinstance(webview, str) else []
        if (len(parts) < 2 or parts[0] != '~' or any(part in ('', '.', '..') for part in parts[1:])
                or CONTROL.search(webview)):
            raise ValueError('--webview-store must be a path below ~/, such as ' + WEBVIEW)
        if root is None:
            return cls(None, Path.home(), Path('/Applications'), webview[2:])
        root = Path(os.path.abspath(root))
        if os.path.realpath(root) in ('/', '/System/Volumes/Data'):
            raise ValueError('--root must be a staged directory, not /.')
        return cls(root, root / 'home', root / 'Applications', webview[2:])

    @property
    def app(self):
        return self.applications / APP

    @property
    def data_home(self):
        return self.user_home / DATA_HOME

    @property
    def webview(self):
        return self.user_home / self.webview_rel

    @property
    def pebble_home(self):
        return self.user_home / PEBBLE_HOME  # Phase 22: main.rs pebble_home()

    @property
    def backups(self):
        return self.user_home / BACKUPS

    def backup_dirs(self, stamp):
        return [self.backups / (stamp + SUFFIX), self.backups / (stamp + LEGACY_SUFFIX)]

    def backup_dir(self, stamp):
        """The root of this stamp: an existing one under either name (the new name first), else the new name."""
        names = self.backup_dirs(stamp)
        return next((path for path in names if os.path.lexists(path)), names[0])

    def describe(self):
        return {'root': None if self.root is None else str(self.root), 'app': str(self.app),
                'data_home': str(self.data_home), 'webview_store': str(self.webview), 'backups': str(self.backups),
                'pebble_home': str(self.pebble_home)}


# ---- Manifests ----------------------------------------------------------------------------------


def _order(entry):
    return PurePosixPath(entry['path']).parts


def tree_manifest(path, hashes=True):
    """Files (bytes, sha256, mode), directories (mode) and links (target text) below path.

    Walked through release_readiness.Root: descriptor-anchored, no-follow and never
    across a mount. Unlike bundle_entries, private names such as runtime.key are
    hashed (never printed), because a data home must be restored byte for byte.
    """
    try:
        root = Root(str(path), 'tree')
    except EvidenceError as error:
        raise Stop('tree_unreadable', 'A directory to hash is missing, a link or unreadable.',
                   path=str(path), reason=error.code) from None
    files, dirs, links, problems = [], [], [], []
    with root:
        mode = stat.S_IMODE(os.fstat(root.fd).st_mode)
        try:
            listing = root.listing()
        except Unsafe as error:
            raise Stop('tree_unsafe', 'The directory changed, or holds a mount point, while it was walked.',
                       path=str(path), reason=error.code) from None
        for parts, info in listing or ():
            rel = '/'.join(parts)
            if stat.S_ISDIR(info.st_mode):
                dirs.append({'path': rel, 'mode': stat.S_IMODE(info.st_mode)})
            elif stat.S_ISLNK(info.st_mode):
                try:
                    fd = root.directory(parts[:-1])
                    try:
                        target = os.readlink(parts[-1], dir_fd=fd)
                    finally:
                        os.close(fd)
                except (Missing, Unsafe, OSError):
                    problems.append({'path': rel, 'code': 'changed_during_read'})
                    continue
                links.append({'path': rel, 'target': target})
            elif stat.S_ISREG(info.st_mode):
                sha, size = None, info.st_size
                if hashes:
                    try:
                        sha, size = root.digest(rel, MAX_FILE)
                    except Missing:
                        problems.append({'path': rel, 'code': 'changed_during_read'})
                        continue
                    except Unsafe as error:
                        problems.append({'path': rel, 'code': error.code})
                        continue
                files.append({'path': rel, 'bytes': size, 'sha256': sha, 'mode': stat.S_IMODE(info.st_mode)})
            else:
                problems.append({'path': rel, 'code': 'special_file'})
    for group in (files, dirs, links):
        group.sort(key=_order)
    body = {'mode': mode, 'files': files, 'dirs': dirs, 'links': links}
    return {**body, 'tree_sha256': digest(canonical(body).encode()) if hashes else None,
            'file_count': len(files), 'dir_count': len(dirs), 'link_count': len(links),
            'bytes': sum(entry['bytes'] for entry in files), 'problems': problems}


def same_tree(expected, actual):
    return not actual.get('problems') and all(expected.get(key) == actual.get(key) for key in TREE_KEYS)


def shape(manifest):
    return (manifest['mode'], [(e['path'], e['bytes'], e['mode']) for e in manifest['files']],
            manifest['dirs'], manifest['links'])


def tree_diff(expected, actual, limit=12):
    """Content-free difference: relative paths only."""
    def index(manifest):
        found = {}
        for kind in ('files', 'dirs', 'links'):
            for entry in manifest.get(kind) or ():
                found[(kind, entry['path'])] = entry
        return found
    a, b = index(expected), index(actual)
    missing = sorted(path for _, path in a.keys() - b.keys())
    extra = sorted(path for _, path in b.keys() - a.keys())
    changed = sorted(key[1] for key in a.keys() & b.keys() if a[key] != b[key])
    return {'missing': missing[:limit], 'extra': extra[:limit], 'changed': changed[:limit],
            'counts': {'missing': len(missing), 'extra': len(extra), 'changed': len(changed)},
            'root_mode_changed': 'mode' in expected and expected.get('mode') != actual.get('mode'),
            'problems': (actual.get('problems') or [])[:limit]}


def diff_facts(ok, expected, actual):
    return {} if ok else {'diff': tree_diff(expected, actual)}


def bundle_manifest(app):
    """The release_readiness walker over one .app: the app-manifest.json seam shared with T1."""
    try:
        root = Root(str(app), 'bundle')
    except EvidenceError as error:
        raise Stop('bundle_unreadable', 'A bundle to hash is missing, a link or unreadable.',
                   path=str(app), reason=error.code) from None
    with root:
        try:
            entries, links, problems = bundle_entries(root)
        except Unsafe as error:
            entries, links, problems = [], [], [(error.code, None)]
    return {'bundle_sha256': bundle_digest(entries), 'files': entries, 'links': links,
            'file_count': len(entries), 'bytes': sum(entry['bytes'] for entry in entries),
            'problems': [list(problem) for problem in problems]}


def same_bundle(expected, actual):
    """Equal file entries and, when the expectation records them, equal links; no walker problem."""
    return (not actual.get('problems') and expected.get('files') == actual.get('files')
            and (expected.get('links') is None or expected['links'] == actual.get('links')))


def bundle_intact(path, expected):
    try:
        return same_bundle(expected, bundle_manifest(path))
    except Stop:
        return False


def load_app_manifest(path, key):
    """T1's app-manifest.json: one app object {bundle_sha256, files[, links]}, or {"apps": {key: object}}."""
    if path is None:
        raise Stop('app_manifest_given', 'Pass --app-manifest.')
    try:
        data = read_explicit(path, 64_000_000, 'app_manifest')
        doc = parse_json(data, 'app_manifest')
    except EvidenceError as error:
        raise Stop('app_manifest_unreadable', 'app-manifest.json is missing or malformed.',
                   path=str(path), reason=error.code) from None
    item = doc
    if isinstance(doc, dict) and 'files' not in doc:
        apps = doc.get('apps')
        if isinstance(apps, dict):
            item = apps.get(key)
        elif isinstance(apps, list):
            item = next((entry for entry in apps if isinstance(entry, dict)
                         and key in (entry.get('name'), entry.get('key'), entry.get('label'))), None)
    if not isinstance(item, dict):
        raise Stop('app_manifest_shape', 'app-manifest.json has no app object for this key.', path=str(path), app_key=key)
    try:
        manifest = read_manifest(json.dumps(item).encode())
    except EvidenceError as error:
        raise Stop('app_manifest_invalid', 'The app object is not a valid bundle manifest.',
                   path=str(path), reason=error.code) from None
    if bundle_digest(manifest['files']) != manifest['bundle_sha256']:
        raise Stop('app_manifest_inconsistent', 'Its bundle_sha256 is not the digest of its own file list.', path=str(path))
    return {'path': str(path), 'sha256': digest(data), 'key': None if item is doc else key,
            'bundle_sha256': manifest['bundle_sha256'], 'files': manifest['files'], 'links': manifest.get('links')}


# ---- Identity, processes and leases -------------------------------------------------------------


def identify(app, what):
    """Info.plist identity and the shell and bundled backend hashes of one bundle."""
    try:
        root = Root(str(app), what)
    except EvidenceError as error:
        raise Stop(what + '_unreadable', f'The {what} bundle is missing, a link or unreadable.',
                   path=str(app), reason=error.code) from None
    with root:
        try:
            raw = root.read(PLIST, 1_000_000)
            shell = root.digest(SHELL, MAX_FILE)
            backend = root.digest(BACKEND, MAX_FILE)
        except (Missing, Unsafe) as error:
            raise Stop(what + '_incomplete', f'The {what} bundle lacks its Info.plist, shell or backend.',
                       path=str(app), reason=getattr(error, 'code', 'missing')) from None
    try:
        plist = plistlib.loads(raw)
    except Exception:  # plistlib raises several types for hostile input
        plist = None
    if not isinstance(plist, dict):
        raise Stop(what + '_plist', f'The {what} Info.plist is not a property list.', path=str(app / PLIST))

    def text(key):
        value = plist.get(key)
        return value if isinstance(value, str) and len(value) <= 200 and not CONTROL.search(value) else None
    return {'path': str(app), 'identifier': text('CFBundleIdentifier'), 'version': text('CFBundleShortVersionString'),
            'build': text('CFBundleVersion'), 'executable': text('CFBundleExecutable'),
            'shell_sha256': shell[0], 'shell_bytes': shell[1], 'backend_sha256': backend[0], 'backend_bytes': backend[1]}


def version_key(text):
    if not isinstance(text, str) or not VERSION.fullmatch(text):
        return None
    parts = [int(part) for part in text.split('.')]
    return tuple(parts + [0] * (4 - len(parts)))


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctl.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                            ctypes.c_size_t]
    libc.sysctl.restype = ctypes.c_int
    return libc


def _procargs(libc, pid, size):
    """argv of pid from sysctl KERN_PROCARGS2, or None when it cannot be read (another user, exited)."""
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    buffer, length = ctypes.create_string_buffer(size), ctypes.c_size_t(size)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(length), None, 0) != 0 or length.value < 4:
        return None
    raw = buffer.raw[:length.value]
    argc, rest = int.from_bytes(raw[:4], sys.byteorder), raw[4:]
    end = rest.find(b'\0')  # the exec path, then NUL padding, then argv
    if end < 0 or not 0 < argc <= 4096:
        return None
    args = rest[end:].lstrip(b'\0').split(b'\0')[:argc]
    return [arg.decode('utf-8', 'surrogateescape') for arg in args] if len(args) == argc else None


def libproc_lister():
    """(pid, executable or None, argv or None) for every process. The executable comes from
    proc_pidpath (the exact image, not argv); argv is read only for Pebble's executable names."""
    lib = ctypes.CDLL(LIBPROC, use_errno=True)
    lib.proc_listallpids.restype = ctypes.c_int
    lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidpath.restype = ctypes.c_int
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    libc = _libc()
    count = lib.proc_listallpids(None, 0)
    if count <= 0:
        raise OSError('The process list cannot be read.')
    capacity = count + 256
    pids = (ctypes.c_int * capacity)()
    written = lib.proc_listallpids(pids, ctypes.sizeof(pids))
    if written <= 0:
        raise OSError('The process list cannot be read.')
    buffer, rows = ctypes.create_string_buffer(4096), []
    for pid in pids[:min(written, capacity)]:
        if pid > 0:
            size = lib.proc_pidpath(pid, buffer, 4096)
            exe = buffer.raw[:size].decode('utf-8', 'surrogateescape') if size > 0 else None
            args = _procargs(libc, pid, 1 << 20) if exe and os.path.basename(exe) in PEBBLE_NAMES else None
            rows.append((pid, exe, args))
    return rows


def _rows(rows):
    for row in rows:
        yield row[0], row[1], (row[2] if len(row) > 2 else None)


def inside(app, rows):
    """Processes whose executable lies inside the bundle: an exact path prefix, never a name."""
    prefixes = tuple(_fold(form.rstrip('/') + '/Contents/') for form in _forms(app))
    return [{'pid': pid, 'executable': exe} for pid, exe, _ in _rows(rows)
            if isinstance(exe, str) and _fold(exe).startswith(prefixes)]


def exactly(path, rows):
    names = {_fold(form) for form in _forms(path)}
    return [{'pid': pid, 'executable': exe, 'role': role(args)} for pid, exe, args in _rows(rows)
            if isinstance(exe, str) and _fold(exe) in names]


def role(args):
    """service (the --home backend), worker (a backend worker mode) or unknown argv (counted as a service)."""
    if not isinstance(args, list):
        return 'unknown'
    return 'worker' if any(arg.split('=')[0] in WORKER_FLAGS for arg in args[1:]) else 'service'


def similar_outside(app, rows):
    found = {row['pid'] for row in inside(app, rows)}
    return [{'pid': pid, 'executable': exe} for pid, exe, _ in _rows(rows) if isinstance(exe, str) and pid not in found
            and os.path.basename(exe) in PEBBLE_NAMES]


def observe(env):
    try:
        return list(env.lister())
    except Exception as error:  # an unreadable process list never means "not running"
        raise Stop('process_list_unavailable', 'The process list could not be read; nothing was changed.',
                   reason=type(error).__name__) from None


def wait_quiet(run, app, seconds):
    """Wait, bounded, until no process runs from the bundle. Never signals anything."""
    env, start, polls = run.env, run.env.monotonic(), 0
    while True:
        found = inside(app, observe(env))
        polls += 1
        waited = env.monotonic() - start
        if not found:
            run.check('pebble_not_running', True, bundle=str(app), polls=polls, waited_seconds=round(waited, 1))
            return
        if waited >= seconds:
            run.check('pebble_not_running', False,
                      'Pebble is running from the target bundle. Ask the user to quit it (tray: Quit Pebble) and '
                      'retry; this tool never signals a process.',
                      bundle=str(app), processes=found[:16], waited_seconds=round(waited, 1))
        env.sleep(min(1.0, max(0.05, seconds - waited)))


def lease_holders(home):
    """Lock files a live service holds in the data home: flock probed read-only, never created."""
    import fcntl
    held = []
    for name in ('runtime-owner.lock', migrations.LOCK):
        try:
            fd = os.open(Path(home) / name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            continue
        except OSError:
            held.append(name)  # a link or an unreadable lock is not evidence of a stopped service
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            held.append(name)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return held


def check_unowned(run, home):
    holders = lease_holders(home) if os.path.isdir(home) and not os.path.islink(home) else []
    run.check('data_home_not_owned', not holders, 'A Pebble service holds the data home lock; wait until it stops.',
              data_home=str(home), locks=holders)


# ---- SQLite (copies only, except the read-only probes noted) -----------------------------------


def sqlite_header(database):
    """The 100-byte header read directly: no SQLite open, so nothing is created beside the file."""
    try:
        fd = os.open(database, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
    except FileNotFoundError:
        return {'exists': False}
    except OSError as error:
        return {'exists': True, 'valid': False, 'reason': type(error).__name__}
    try:
        head = os.read(fd, 100)
    finally:
        os.close(fd)
    if len(head) < 100 or head[:16] != b'SQLite format 3\x00':
        return {'exists': True, 'valid': False}
    return {'exists': True, 'valid': True, 'wal_mode': head[18:20] == b'\x02\x02',
            'user_version': int.from_bytes(head[60:64], 'big', signed=True)}


def sidecars(home):
    found = {}
    for suffix in migrations.SIDECARS:
        try:
            info = os.lstat(Path(home) / (migrations.DATABASE + suffix))
        except FileNotFoundError:
            continue
        found[suffix] = info.st_size if stat.S_ISREG(info.st_mode) else -1
    return found


def integrity(database):
    """PRAGMA integrity_check on a copy opened mode=ro&immutable=1 (as migrations.py:747-748)."""
    uri = Path(os.path.abspath(database)).as_uri() + '?mode=ro&immutable=1'
    try:
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as db:
            rows = db.execute('PRAGMA integrity_check').fetchall()
            version = db.execute('PRAGMA user_version').fetchone()[0]
    except sqlite3.Error as error:
        return {'integrity': 'unreadable', 'reason': type(error).__name__}
    return {'integrity': 'ok' if rows == [('ok',)] else 'failed', 'user_version': version}


def inspect_summary(report):
    database = report.get('database') or {}
    return {'state': report.get('state'), 'refusal': report.get('refusal'), 'schema_epoch': report.get('schema_epoch'),
            'database': pick(database, 'exists', 'user_version', 'rows', 'upgrade_history', 'current', 'fresh',
                             'refusal', 'integrity', 'bytes', 'tables', 'versions'),
            'backups': [pick(item, 'id', 'state', 'sha256', 'manifest_sha256', 'from_version', 'to_version', 'verified')
                        for item in report.get('backups') or () if isinstance(item, dict)],
            'budget': report.get('budget')}


def baseline_rows(doc):
    return ((doc.get('inspect') or {}).get('database') or {}).get('rows') or {}


def check_rows(run, doc, rows):
    """Every pre-existing table keeps its row count, except exact changes the parent accepted by name."""
    before = baseline_rows(doc)
    run.check('row_baseline_present', bool(before), 'backup.json records no row counts.')
    accepted = set(run.options.get('accept_rows') or ())
    changes = [{'table': label, 'before': count, 'after': rows.get(label)}
               for label, count in sorted(before.items()) if rows.get(label) != count]
    explained = [c for c in changes if (c['table'], c['before'], c['after']) in accepted]
    unexplained = [c for c in changes if c not in explained]
    run.results['row_changes'] = {'tables': len(before), 'accepted': explained, 'unexplained': unexplained,
                                  'unused_acceptances': sorted(list(item) for item in accepted
                                                               if not any((c['table'], c['before'], c['after']) == item
                                                                          for c in changes))}
    run.check('row_counts_unchanged', not unexplained, "A pre-existing table's row count changed.",
              changes=unexplained[:24], tables=len(before))


def check_current(run, report, doc, home):
    database = report.get('database') or {}
    run.results['inspect'] = inspect_summary(report)
    run.check('state_current', report.get('state') == 'current', 'The data home is not current.', home=str(home),
              state=report.get('state'), refusal=report.get('refusal') or database.get('refusal'))
    run.check('schema_epoch', database.get('user_version') == EXPECTED_EPOCH, 'The data home is not at the expected epoch.',
              home=str(home), user_version=database.get('user_version'), expected=EXPECTED_EPOCH)
    history = database.get('upgrade_history')
    run.check('upgrade_history', type(history) is int and history >= 1, 'upgrade_history has no row.',
              home=str(home), upgrade_history=history)
    if 'integrity' in database:
        run.check('integrity_ok', database.get('integrity') == 'ok', 'PRAGMA integrity_check failed.', home=str(home))
    check_rows(run, doc, database.get('rows') or {})


def check_new_upgrade_backups(run, home, doc, report):
    known = set(doc.get('upgrade_backup_ids') or ())
    new = sorted(item['id'] for item in report.get('backups') or ()
                 if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'] not in known)
    run.check('new_upgrade_backup', bool(new), 'No new entry appeared in upgrade-backups/.',
              path=str(Path(home) / migrations.BACKUPS))
    checked = []
    for backup_id in new:
        result = migrations.verify_backup(home, backup_id)
        checked.append({'id': backup_id, 'valid': result.get('valid'), 'reason': result.get('reason'),
                        'state': result.get('state')})
    run.results['new_upgrade_backups'] = checked
    run.check('upgrade_backups_valid', all(item['valid'] is True for item in checked),
              'verify_backup did not report every new upgrade backup valid.', backups=checked)
    return new


# ---- Phase 22: ~/.pebble ------------------------------------------------------------------------


def pebble_live(layout):
    """Whether ~/.pebble/data is the live data home: it holds the move's receipt, or a database while the old
    home holds none."""
    return live_pebble_home(layout.pebble_home, layout.data_home)


def live_pebble_home(pebble, old):
    """pebble_live's rule for any pair of folders (the drill applies it to its sources); old may be None."""
    data = Path(pebble) / 'data'
    database = data / migrations.DATABASE
    if not os.path.isfile(database) or os.path.islink(database):
        return False
    return os.path.lexists(data / MOVE_RECEIPT) or old is None or not os.path.lexists(Path(old) / migrations.DATABASE)


def moved_marks(folder):
    """The move's own files in a folder: MOVED.md and pebble-moved.json (a frozen old folder) or the receipt (a copy
    of ~/.pebble/data). Either means the folder must not be drilled as a live old data home (V1-044)."""
    return sorted(name for name in MOVE_FILES | {MOVE_RECEIPT} if os.path.lexists(Path(folder) / name))


def named_documents_folder(pebble):
    """The Documents folder a ~/.pebble copy's settings.json names (storage.documents_folder), so a rehearsal or drill
    launch on that copy can write-protect it; None when none is named or the file cannot be read."""
    try:
        doc = parse_json(read_explicit(Path(pebble) / 'settings.json', 1_000_000, 'settings'), 'settings')
    except EvidenceError:
        return None
    storage = doc.get('storage') if isinstance(doc, dict) else None
    folder = storage.get('documents_folder') if isinstance(storage, dict) else None
    if not isinstance(folder, str) or not folder or CONTROL.search(folder):
        return None
    return os.path.abspath(os.path.expanduser(folder))


def data_layout(run):
    layout = run.options.get('data_layout') or 'legacy'
    run.check('data_layout_known', layout in DATA_LAYOUTS, 'Use --data-layout legacy or pebble-home.', data_layout=layout)
    return layout


def without(manifest, names):
    """A tree manifest without the top-level entries named in `names`."""
    keep = lambda entry: entry['path'] not in names  # noqa: E731
    return {**manifest, **{kind: [e for e in manifest.get(kind) or () if keep(e)] for kind in ('files', 'dirs', 'links')}}


def check_old_folder(run, home, recorded):
    """After the move the old home keeps every recorded entry exactly; only MOVED.md, pebble-moved.json, SQLite's
    read sidecars and the service locks may be new."""
    names = MOVE_FILES | READ_TOUCH
    expected, actual = without(recorded, names), without(tree_manifest(home), names)
    ok = same_tree(expected, actual)
    run.check('old_folder_intact', ok, 'The old data home changed beyond MOVED.md and pebble-moved.json; the move '
              'must leave it as it was.', path=str(home), **diff_facts(ok, expected, actual))


def check_moved(run, pebble, doc, home):
    """The candidate's own record in ~/.pebble/runtime/migration.json: the old home moved (or ~/.pebble was live)."""
    path = Path(pebble) / MOVE_STATUS
    try:
        status = strict_json(read_explicit(path, MAX_RECORD, 'migration_status'), 'migration_status')
    except (EvidenceError, Stop):
        status = None
    status = status if isinstance(status, dict) else {}
    expected = ('current',) if doc.get('live_home') == 'pebble_home' else ('migrated', 'current')
    run.results['move'] = pick(status, 'state', 'reason', 'moved_at', 'legacy_changed', 'warning')
    run.check('home_moved', status.get('state') in expected and status.get('data') == str(Path(pebble) / 'data'),
              'The candidate did not move the old data home into ~/.pebble/data.', path=str(path),
              state=status.get('state'), reason=status.get('reason'))
    if doc.get('live_home') != 'pebble_home':
        run.check('old_folder_pointer', all(os.path.isfile(Path(home) / name) for name in sorted(MOVE_FILES)),
                  'The old data home has no MOVED.md and pebble-moved.json.', path=str(home))


def check_upgrade_backups_after_move(run, data, doc, report):
    """A move keeps the old home as its own rollback copy; a new upgrade backup is expected only when the
    candidate's schema epoch is newer than the backed-up database's."""
    if (doc.get('database') or {}).get('user_version') != EXPECTED_EPOCH:
        return check_new_upgrade_backups(run, data, doc, report)
    run.results['new_upgrade_backups'] = 'not_needed_same_epoch'
    return []


# ---- Release manifest (§6.7) ------------------------------------------------------------------


def release_verifier(roots=None, revoked=None):
    """T3's attestation.verify_release_manifest bound to trust roots: the pinned ones (attestation.pinned_trust)
    unless a test passes its own as arguments; no flag or environment variable can. The verifier takes the
    envelope bytes and every install fact T3 requires, and returns T3's ReleaseVerdict."""
    if roots is None and revoked is None:
        roots, revoked = attestation.pinned_trust()
    elif roots is None or revoked is None:
        raise ValueError('Pass roots and revoked together.')

    def verifier(data, **context):
        return attestation.verify_release_manifest(data, roots=roots, revoked=revoked, **context)

    def release_pinned():
        # Parent follow-up 3: any pinned root holding the release role requires an accepted signed manifest, even one
        # outside its window or revoked (T3 then refuses every manifest until a usable key is pinned).
        try:
            loaded, _ = attestation.load_roots(roots, revoked)
        except ValueError:
            return True  # an unreadable pin never relaxes the requirement
        return any('release' in root.roles for root in loaded.values())
    verifier.pinned = lambda now: attestation.pinned_keys(roots, revoked, gate='signed_updates', now=now)
    verifier.release_pinned = release_pinned
    return verifier


def release_key_pinned(verifier):
    """Whether this install's verifier pins a release key (an injected test verifier without that facet: no)."""
    probe = getattr(verifier, 'release_pinned', None)
    return bool(probe()) if probe is not None else False


def default_release_verifier():
    """The production verifier: T3's, against backend/pebble/trust_roots.py only."""
    return release_verifier()


def release_payload(path, verifier, require_signed):
    """(record, bytes) of one release manifest: a signed pebble.attestation/1 envelope, which release_check has T3
    verify with this install's facts, or an unsigned pebble.release/1 manifest in T3's exact shape."""
    if require_signed and verifier is None:
        raise Stop('release_verifier_unavailable', '--require-signed is set but no release verifier is available.',
                   path=str(path))
    try:
        data = read_explicit(path, 1_000_000, 'release_manifest')
        doc = parse_json(data, 'release_manifest')
    except EvidenceError as error:
        raise Stop('release_manifest_unreadable', 'The release manifest is missing or malformed.',
                   path=str(path), reason=error.code) from None
    form = doc.get('format') if isinstance(doc, dict) else None
    if form == ENVELOPE_FORMAT:
        if verifier is None:
            raise Stop('release_envelope_unverifiable', 'The release manifest is a signed envelope and no verifier is '
                       'available.', path=str(path))
        return {'path': str(path), 'sha256': digest(data), 'signed': True, 'payload': None}, data
    if form != RELEASE_FORMAT:
        raise Stop('release_manifest_format', 'The release manifest is neither pebble.release/1 nor a signed envelope.',
                   path=str(path))
    if require_signed:
        raise Stop('release_manifest_unsigned', '--require-signed refuses an unsigned release manifest.',
                   path=str(path))
    try:
        attestation.check_release_body(doc)  # T3's schema; an unsigned manifest authenticates nothing
    except ValueError as error:
        raise Stop('release_payload_invalid', 'The release payload is not the pebble.release/1 shape.', path=str(path),
                   reason=error.args[0] if error.args else 'format', fields=sorted(set(doc) ^ attestation.RELEASE_KEYS))
    return {'path': str(path), 'sha256': digest(data), 'signed': False, 'payload': pick(doc, *RELEASE_KEYS)}, data


def release_check(run, path, *, require_signed, bundle, backend, version, installed_backend, installed_version,
                  backup_sha, data_version):
    """The §6.7 check of one release manifest for this install. A signed manifest is decided by T3's
    verify_release_manifest with every fact it requires from this install; when T3 refuses, the install stops
    with T3's refusal code. Empty pinned roots refuse every signed manifest (unknown_key), so --require-signed
    fails closed. The host's own checks below then run on the payload, signed or not."""
    found, data = release_payload(path, run.env.verifier, require_signed)
    run.results['release'] = found
    pinned = getattr(run.env.verifier, 'pinned', None)
    now = utc(run.env.clock())
    if pinned is not None:
        found['release_keys_pinned'] = len(pinned(now))
    if found['signed']:
        try:
            verdict = run.env.verifier(data, app_bundle_sha256=bundle, backend_executable_sha256=backend,
                                       installed_backend_sha256=installed_backend, backup_record_sha256=backup_sha,
                                       installed_version=installed_version, installed_data_epoch=data_version,
                                       data_epoch=EXPECTED_EPOCH, now=now)
            accepted, code = verdict.accepted, verdict.code
        except Exception as error:  # an input T3 cannot judge (for example a version that is not X.Y.Z) refuses
            raise Stop('release_signature_invalid', 'The release envelope could not be verified.', path=str(path),
                       reason=getattr(error, 'code', None) or type(error).__name__) from None
        found['verdict'] = {key: value for key, value in verdict.as_dict().items() if key != 'manifest'}
        run.check(code or 'release_signed_manifest_accepted', accepted,
                  f'The trust channel refused the signed release manifest ({code}).', path=str(path),
                  key_id=verdict.key_id, envelope_sha256=verdict.envelope_sha256)
        found['payload'] = pick(verdict.manifest, *RELEASE_KEYS)
    check_release(run, found['payload'], bundle=bundle, backend=backend, version=version,
                  installed_backend=installed_backend, installed_version=installed_version, backup_sha=backup_sha,
                  data_version=data_version)
    return found


def check_release(run, payload, *, bundle, backend, version, installed_backend, installed_version, backup_sha,
                  data_version):
    run.check('release_app_bundle', payload['app_bundle_sha256'] == bundle,
              'The release manifest names another app bundle.', manifest=payload['app_bundle_sha256'], candidate=bundle)
    run.check('release_backend', payload['backend_executable_sha256'] == backend,
              'The release manifest names another backend.', manifest=payload['backend_executable_sha256'],
              candidate=backend)
    run.check('release_previous', payload['previous'] == installed_backend,
              'The release manifest does not replace the installed backend.', manifest=payload['previous'],
              installed=installed_backend)
    run.check('release_rollback', payload['rollback'] == backup_sha,
              "The release manifest's rollback is not this backup.json.", manifest=payload['rollback'], backup=backup_sha)
    run.check('release_version', payload['version'] == version,
              "The release version is not the candidate's Info.plist version.", manifest=payload['version'],
              candidate=version)
    run.check('release_version_higher', version_key(payload['version']) > version_key(installed_version),
              'The release version is not higher than the installed version.', manifest=payload['version'],
              installed=installed_version)
    run.check('release_data_epoch', payload['data_epoch'] == EXPECTED_EPOCH,
              'The release data epoch is not this source epoch.', manifest=payload['data_epoch'], expected=EXPECTED_EPOCH)
    run.check('release_min_data_epoch', type(data_version) is int
              and payload['min_data_epoch'] <= data_version <= payload['data_epoch'],
              "The backed-up database's epoch is outside the release's accepted range.",
              min_data_epoch=payload['min_data_epoch'], data_epoch=payload['data_epoch'], backup_user_version=data_version)


# ---- Rehearsal launch -------------------------------------------------------------------------


def sbpl_string(text):
    """A Seatbelt string literal. Not JSON: Seatbelt reads no \\u escapes (the 0.4.2 root cause)."""
    if not isinstance(text, str) or CONTROL.search(text):
        raise ValueError('Refusing a control character in a sandbox path.')
    return '"' + text.translate({ord('\\'): '\\\\', ord('"'): '\\"'}) + '"'


def protected_forms(paths):
    out = []
    for path in paths:
        for form in sorted(_forms(path)):
            if form not in out:
                out.append(form)
    return out


def sandbox_profile(protected, keychain=False):
    """The rehearsal profile: loopback-only network, protected paths read-only, no Keychain services."""
    lines = [';; Pebble install rehearsal (D20 2.3, scripts/pebble_install.py).',
             '(version 1)', '(allow default)', '(deny network*)',
             '(allow network* (local ip "localhost:*"))', '(allow network* (remote ip "localhost:*"))']
    if not keychain:
        lines += ['(deny mach-lookup (global-name ' + sbpl_string(name) + '))' for name in KEYCHAIN_SERVICES]
    lines += ['(deny file-write* (subpath ' + sbpl_string(path) + '))' for path in protected_forms(protected)]
    return '\n'.join(lines) + '\n'


def sandboxed_launcher(spec):
    """Run one backend under sandbox-exec, send the requests, close stdin and wait (bounded).

    The only signal this tool can send is SIGKILL through this child's own live,
    unreaped Popen handle when its bounded wait expires; never by PID or by name.
    """
    if not trusted_tool(SANDBOX):
        raise OSError('sandbox-exec is missing or not root-owned.')
    home = Path.home()
    # 1.0 (V1-044): a drill now starts the candidate on a copy of ~/.pebble, whose settings may name a Documents
    # folder; the real ~/Documents and ~/.pebble are never written by any rehearsal or drill launch.
    protected = list(spec['protected']) + [str(home / 'Library'), '/Applications', str(home / BACKUPS),
                                           str(home / 'Documents'), str(home / PEBBLE_HOME)]
    work = Path(spec['workdir'])
    profile = work / 'sandbox.sb'
    write_text_new(profile, sandbox_profile(protected, spec.get('keychain', False)))
    argv = [SANDBOX, '-f', str(profile), spec['backend'], '--home', spec['home'],
            *(['--legacy-home', spec['legacy_home']] if spec.get('legacy_home') else []), '--resources', spec['resources']]
    env = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
    env['TMPDIR'] = spec['tmpdir']
    log = os.open(work / 'backend.stderr.log', os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=env,
                                   cwd=str(work), close_fds=True, start_new_session=True)
    finally:
        os.close(log)
    deadline = time.monotonic() + spec['timeout']
    wanted = {request['id'] for request in spec['requests']}
    responses, events, timed_out, pending, total, closed = {}, Counter(), False, b'', 0, False
    try:
        process.stdin.write(b''.join(json.dumps(r).encode() + b'\n' for r in spec['requests']))
        process.stdin.flush()
    except OSError:
        pass  # an early exit is reported through its exit code
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            if not closed and wanted <= responses.keys():
                try:
                    process.stdin.close()
                except OSError:
                    pass
                closed = True
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            if not selector.select(min(left, 0.5)):
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_OUTPUT:
                timed_out = True
                break
            *lines, pending = (pending + chunk).split(b'\n')
            for line in lines:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get('id') in wanted:
                    responses[message['id']] = message
                elif isinstance(message, dict) and isinstance(message.get('event'), str):
                    events[message['event']] += 1
    if not closed:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        code = process.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()  # this child's live handle only; it has not been reaped
        code = process.wait(timeout=10)
    finally:
        process.stdout.close()
    return {'responses': responses, 'events': dict(events), 'exit': code, 'timed_out': timed_out}


def summarize_launch(result):
    """Content-free: response shapes and known error strings only, never chat or memory text."""
    result = result if isinstance(result, dict) else {}
    out = {'exit': result.get('exit'), 'timed_out': bool(result.get('timed_out')), 'responses': {}, 'events': {}}
    for name, count in (result.get('events') or {}).items():
        if isinstance(name, str) and LABEL.fullmatch(name) and len(out['events']) < 32:
            out['events'][name] = count
    for request in REQUESTS:
        message = (result.get('responses') or {}).get(request['id'])
        if not isinstance(message, dict):
            entry = {'answered': False}
        elif message.get('error') is not None:
            text = message['error']
            entry = {'answered': True, 'ok': False, 'error': text if text in KNOWN_ERRORS else redact(text)}
        else:
            value, entry = message.get('result'), {'answered': True, 'ok': True}
            if request['method'] == 'state' and isinstance(value, dict):
                entry.update(keys=sorted(key for key in value if isinstance(key, str))[:48],
                             maintenance='maintenance' in value)
            if request['method'] == 'upgrade_backups' and isinstance(value, dict):
                reason = value.get('reason')
                entry.update(reason=reason if isinstance(reason, str) and LABEL.fullmatch(reason) else None,
                             backups=[item.get('id') for item in value.get('backups') or () if isinstance(item, dict)][:16])
        out['responses'][request['method']] = entry
    return out


def launch(run, app, home, work, tmpdir, protected, legacy=None):
    spec = {'backend': str(app / BACKEND), 'resources': str(app / RESOURCES), 'home': str(home),
            'requests': [dict(request) for request in REQUESTS], 'timeout': run.options.get('launch_timeout', 180),
            'workdir': str(work), 'tmpdir': str(tmpdir), 'protected': [str(path) for path in protected],
            'keychain': bool(run.options.get('allow_keychain'))}
    if legacy is not None:
        spec['legacy_home'] = str(legacy)  # Phase 22: the candidate moves this copy into home/data, as on first launch
    try:
        result = run.env.launcher(spec)
    except Exception as error:
        raise Stop('launch_failed', 'The backend could not be launched.', backend=spec['backend'],
                   reason=type(error).__name__) from None
    summary = summarize_launch(result)
    run.results['launch'] = {**summary, 'backend': spec['backend'], 'home': spec['home']}
    run.check('launch_completed', not summary['timed_out'],
              'The backend did not finish within --launch-timeout; only its own live child was stopped.')
    state = summary['responses']['state']
    run.check('launch_state', state.get('ok') is True and not state.get('maintenance'),
              'The backend did not answer state as a started service.', response=state)
    run.check('launch_upgrade_backups', summary['responses']['upgrade_backups'].get('answered') is True,
              'The backend did not answer upgrade_backups.', response=summary['responses']['upgrade_backups'])
    run.check('launch_exit', summary['exit'] == 0, 'The backend did not exit cleanly after stdin closed.',
              exit=summary['exit'])


# ---- Logs ------------------------------------------------------------------------------------


def log_offsets(home):
    offsets = {}
    for name in LOGS:
        try:
            info = os.lstat(Path(home) / name)
        except FileNotFoundError:
            offsets[name] = None
            continue
        offsets[name] = {'bytes': info.st_size, 'inode': info.st_ino} if stat.S_ISREG(info.st_mode) else None
    return offsets


def backup_log_offsets(doc):
    sizes = {entry['path']: entry['bytes'] for entry in (doc.get('data_home') or {}).get('files') or ()}
    return {name: ({'bytes': sizes[name], 'inode': None} if name in sizes else None) for name in LOGS}


def pebble_log_offsets(doc):
    """Phase 22: ~/.pebble/logs/<name> as backed up; None (scan from the start) when there was no ~/.pebble."""
    sizes = {entry['path']: entry['bytes'] for entry in (doc.get('pebble_home') or {}).get('files') or ()}
    return {name: ({'bytes': sizes['logs/' + name], 'inode': None} if 'logs/' + name in sizes else None)
            for name in LOGS}


def new_tracebacks(home, offsets):
    found = {}
    for name in LOGS:
        before = (offsets or {}).get(name)
        try:
            fd = os.open(Path(home) / name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
        except FileNotFoundError:
            found[name] = {'exists': False, 'scanned': True, 'tracebacks': 0}
            continue
        except OSError as error:
            found[name] = {'exists': True, 'scanned': False, 'reason': type(error).__name__}
            continue
        try:
            info = os.fstat(fd)
            same = (isinstance(before, dict) and before.get('inode') in (None, info.st_ino)
                    and info.st_size >= before.get('bytes', 0))
            start = before['bytes'] if same else 0
            size = info.st_size - start
            if size > MAX_LOG_SCAN:
                found[name] = {'exists': True, 'scanned': False, 'new_bytes': size}
                continue
            os.lseek(fd, start, os.SEEK_SET)
            data, left = bytearray(), size
            while left > 0:
                chunk = os.read(fd, min(left, 1 << 20))
                if not chunk:
                    break
                data += chunk
                left -= len(chunk)
            text = bytes(data)
            handled = len(HANDLED_TRACEBACK.findall(text))
            found[name] = {'exists': True, 'scanned': True, 'from': start, 'new_bytes': len(data),
                           'tracebacks': text.count(TRACEBACK) - handled, 'handled_request_failures': handled}
        finally:
            os.close(fd)
    return found


# ---- Real effects ------------------------------------------------------------------------------


def real_copier(source, destination):
    if not trusted_tool(DITTO):
        raise CopyFailed('ditto is missing or not root-owned')
    outcome = run_bounded([DITTO, str(source), str(destination)], seconds=COPY_SECONDS)
    if outcome['timed_out'] or outcome['truncated'] or outcome['exit'] != 0:
        raise CopyFailed('ditto timed out' if outcome['timed_out'] else f'ditto exited {outcome["exit"]}')


def real_codesign(app):
    if not trusted_tool(CODESIGN):
        return {'exit': None, 'reason': 'codesign_untrusted'}
    outcome = run_bounded([CODESIGN, '--verify', '--deep', '--strict', '--verbose=2', str(app)],
                          seconds=CODESIGN_SECONDS)
    return {'exit': outcome['exit'], 'timed_out': outcome['timed_out']}


def real_free_bytes(path):
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


@dataclass
class Env:
    """Every effect on processes, copies, signatures, launches, disk space and time; tests inject fakes."""
    lister: object = libproc_lister
    copier: object = real_copier
    codesign: object = real_codesign
    launcher: object = sandboxed_launcher
    free_bytes: object = real_free_bytes
    verifier: object = None
    clock: object = time.time
    monotonic: object = time.monotonic
    sleep: object = time.sleep


# ---- Runs and steps ----------------------------------------------------------------------------


class Run:
    def __init__(self, command, layout, env, execute, options):
        self.command, self.layout, self.env, self.execute, self.options = command, layout, env, execute, options
        self.started = env.clock()
        self.checks, self.actions, self.results, self.recovery = [], [], {}, []
        self.changed = False
        self.report_dir = self.report_prefix = self.report_fixed = None
        self.always_record = False

    def stamp(self):
        return stamp_at(self.env.clock())

    def check(self, name, ok, message='', **facts):
        self.checks.append({'check': name, 'ok': bool(ok), **{key: plain(value) for key, value in facts.items()}})
        if not ok:
            raise Stop(name, message or ' '.join(name.split('_')), **{key: plain(value) for key, value in facts.items()})

    def steps(self, steps):
        """Run (op, fields, fn, kind) in order. kind is 'read', 'touch' (a read that SQLite may
        turn into new -wal/-shm files) or 'write'. A dry run lists every step and runs only the
        leading 'read' steps."""
        entries = []
        for op, fields, fn, kind in steps:
            entry = {'op': op, 'kind': kind, **{key: plain(value) for key, value in fields.items()}, 'state': 'planned'}
            entries.append(entry)
            self.actions.append(entry)
        live = True
        for entry, (op, fields, fn, kind) in zip(entries, steps):
            if not self.execute and (kind != 'read' or not live):
                live = False
                continue
            if kind == 'write':
                self.changed = True  # set first: a partial effect is a change
            entry['state'] = 'running'
            try:
                outcome = fn()
            except BaseException:
                entry['state'] = 'failed'
                raise
            entry['state'] = outcome if isinstance(outcome, str) else 'done'


def need_stamp(run):
    stamp = run.options.get('stamp')
    run.check('stamp_given', stamp is not None, 'Pass --stamp with the backup stamp (YYYYMMDDTHHMMSSZ).')
    return stamp


def check_identity(run, ident, what):
    run.check(what + '_identifier', ident['identifier'] == IDENTIFIER, f'The {what} bundle identifier is not {IDENTIFIER}.',
              path=ident['path'], identifier=ident['identifier'])
    run.check(what + '_executable', ident['executable'] == EXECUTABLE, f'The {what} bundle executable is not pebble.',
              path=ident['path'], executable=ident['executable'])
    run.check(what + '_version', version_key(ident['version']) is not None,
              f'The {what} version is not a dotted number.', path=ident['path'], version=ident['version'])


def check_candidate(run, candidate, manifest, name='candidate_matches_manifest'):
    actual = bundle_manifest(candidate)
    ok = same_bundle(manifest, actual)
    run.check(name, ok, 'The candidate tree differs from app-manifest.json.', path=str(candidate),
              manifest=manifest['path'], bundle_sha256=actual['bundle_sha256'], expected=manifest['bundle_sha256'],
              links_checked=manifest['links'] is not None, **diff_facts(ok, manifest, actual))
    return actual


def candidate_facts(candidate, manifest, tree, ident):
    return {'path': str(candidate), 'bundle_sha256': tree['bundle_sha256'], 'files': tree['files'],
            'links': tree['links'], 'file_count': tree['file_count'], 'bytes': tree['bytes'],
            'backend_sha256': ident['backend_sha256'], 'shell_sha256': ident['shell_sha256'],
            'version': ident['version'], 'build': ident['build'],
            'manifest_path': manifest['path'], 'manifest_sha256': manifest['sha256'], 'manifest_key': manifest['key']}


def codesign_check(run, app):
    try:
        result = run.env.codesign(app)
    except Exception as error:
        result = {'exit': None, 'reason': type(error).__name__}
    result = result if isinstance(result, dict) else {}
    run.check('codesign_verified', result.get('exit') == 0, 'codesign --verify --deep --strict failed.',
              path=str(app), exit=result.get('exit'))


def copy(run, source, destination):
    """ditto into a path that does not exist: ditto merges into an existing directory."""
    run.check('copy_destination_absent', not os.path.lexists(destination),
              'ditto would merge into an existing path; refusing.', source=str(source), destination=str(destination))
    try:
        run.env.copier(Path(source), Path(destination))
    except Exception as error:
        raise Stop('copy_failed', 'The copy did not complete.', source=str(source), destination=str(destination),
                   reason=str(error)[:200] or type(error).__name__) from None


def move(run, source, destination):
    run.check('move_destination_absent', not os.path.lexists(destination), 'Refusing to move onto an existing path.',
              source=str(source), destination=str(destination))
    try:
        rename_new(source, destination)
    except OSError as error:
        raise Stop('move_failed', 'The rename did not happen.', source=str(source), destination=str(destination),
                   reason=errno.errorcode.get(error.errno, type(error).__name__)) from None


def same_device(run, a, b, what):
    try:
        ok = os.lstat(a).st_dev == os.lstat(b).st_dev
    except OSError:
        ok = False
    run.check('same_volume', ok, f'{what} must move by rename on one volume; a cross-volume move would copy.',
              source=str(a), destination=str(b))


def present_dir(run, path, what, required=False):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        run.check(what + '_present', not required, f'The {what} is missing.', path=str(path))
        return False
    run.check(what + '_is_directory', stat.S_ISDIR(info.st_mode), f'The {what} is a link or not a directory.',
              path=str(path))
    return True


def check_data_home(run, home):
    present_dir(run, home, 'data_home', required=True)
    database = Path(home) / migrations.DATABASE
    header = sqlite_header(database)
    run.check('database_present', header.get('valid') is True, 'pebble.sqlite is missing or not a SQLite database.',
              path=str(database))
    side = sidecars(home)
    run.check('wal_checkpointed', side.get('-wal', 0) == 0,
              'pebble.sqlite-wal holds frames: Pebble did not checkpoint on quit. Ask the user to start and quit '
              'Pebble once more, then retry.', path=str(database) + '-wal', wal_bytes=side.get('-wal'))
    run.check('no_hot_journal', '-journal' not in side, 'pebble.sqlite-journal exists (an interrupted transaction).',
              path=str(database) + '-journal')
    return header


def check_backups_root(run, path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    run.check('backups_root_safe', stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
              and not info.st_mode & 0o022, 'The backups folder must be your own real directory, not writable by '
              'others.', path=str(path), mode=oct(stat.S_IMODE(info.st_mode)))


def check_space(run, path, need, sizes, purpose):
    free = run.env.free_bytes(existing(path))
    run.check('free_space', free >= need, f'Not enough free space for {purpose}.', path=str(existing(path)),
              free_bytes=free, needed_bytes=need, sizes=sizes)


def backup_record_ok(doc, stamp):
    """Whether backup.json is a complete record for this stamp (load_backup's rule; the backups listing reuses it)."""
    return (isinstance(doc, dict) and doc.get('format') == BACKUP_FORMAT and doc.get('stamp') == stamp
            and doc.get('complete') is True
            and all(isinstance(doc.get(key), dict) for key in ('installed', 'app', 'database', 'inspect'))
            and doc['app'].get('copy', LEGACY_APP_COPY) in (APP_COPY, LEGACY_APP_COPY)
            # Phase 22: the old data home may be absent only when ~/.pebble was the live home and was copied.
            and (isinstance(doc.get('data_home'), dict)
                 or doc.get('live_home') == 'pebble_home' and isinstance(doc.get('pebble_home'), dict)))


def app_copy(doc):
    """The bundle copy's name in its backup root: Pebble.app.backup from 1.0, Pebble.app in Phase 21-23 roots."""
    name = (doc.get('app') or {}).get('copy', LEGACY_APP_COPY) if isinstance(doc, dict) else None
    return name if name in (APP_COPY, LEGACY_APP_COPY) else APP_COPY


def data_copy(layout, B, doc):
    """Where the old data home's copy is: B/com.pebble.local, or, for an unchanged frozen old folder (V1-137), the
    verified copy of the earlier backup root that backup.json names. None when no old data home was recorded."""
    record = doc.get('data_home') if isinstance(doc, dict) else None
    if not isinstance(record, dict):
        return None
    reused = record.get('reused')
    if record.get('copy') == DATA_COPY or not isinstance(reused, dict):
        return B / DATA_COPY
    root = reused.get('root')
    if not isinstance(root, str) or not BACKUP_NAME.fullmatch(root) or root == B.name:
        raise Stop('backup_record_valid', "backup.json names no valid earlier backup for the old folder's copy.",
                   path=str(B / 'backup.json'))
    return layout.backups / root / DATA_COPY


def load_backup(run, layout, stamp):
    B = layout.backup_dir(stamp)
    try:
        info = os.lstat(B)
    except FileNotFoundError:
        info = None
    run.check('backup_root_present', info is not None and stat.S_ISDIR(info.st_mode),
              'No backup root exists for this stamp.', path=str(B))
    run.check('backup_root_private', info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
              'The backup root must be yours with mode 0700.', path=str(B), mode=oct(stat.S_IMODE(info.st_mode)))
    run.check('backup_root_unique', sum(os.path.lexists(path) for path in layout.backup_dirs(stamp)) == 1,
              'Two backup roots exist for this stamp; move one aside.', paths=layout.backup_dirs(stamp))
    try:
        data = read_explicit(B / 'backup.json', MAX_RECORD, 'backup.json')
    except EvidenceError as error:
        raise Stop('backup_record_unreadable', 'backup.json is missing or unreadable.', path=str(B / 'backup.json'),
                   reason=error.code) from None
    doc = strict_json(data, 'backup.json')
    run.check('backup_record_valid', backup_record_ok(doc, stamp), 'backup.json is not a complete record for this stamp.',
              path=str(B / 'backup.json'))
    sha = digest(data)
    run.results['backup_json_sha256'] = sha
    return B, doc, sha


def peek_backup(B):
    """backup.json read without checks, only to choose which steps to list; load_backup checks it before any step
    acts, and the caller then requires the same choice."""
    try:
        doc = strict_json(read_explicit(B / 'backup.json', MAX_RECORD, 'backup.json'), 'backup.json')
    except (EvidenceError, Stop):
        return {}
    return doc if isinstance(doc, dict) else {}


def check_backup_intact(run, B, doc, bundle_from=None, bundle=True, data=True, webview=True, old_folder=True):
    """Re-hash the backup copies against backup.json before anything relies on them. old_folder=False skips the old
    data home's copy, which a post-move rollback never uses."""
    if bundle:
        bundle_from = bundle_from or app_copy(doc)
        actual = bundle_manifest(B / bundle_from)
        ok = same_bundle(doc['app'], actual)
        run.check('backup_bundle_intact', ok, 'The backed-up bundle differs from backup.json.', path=str(B / bundle_from),
                  **diff_facts(ok, doc['app'], actual))
    if data and old_folder and doc.get('data_home') is not None:
        source = data_copy(run.layout, B, doc)
        actual = tree_manifest(source)
        ok = same_tree(doc['data_home'], actual)
        run.check('backup_data_intact', ok, 'The backed-up data home differs from backup.json.', path=str(source),
                  **diff_facts(ok, doc['data_home'], actual))
    if data and doc.get('pebble_home'):
        actual = tree_manifest(B / PEBBLE_COPY)
        ok = same_tree(doc['pebble_home'], actual)
        run.check('backup_pebble_home_intact', ok, 'The backed-up ~/.pebble differs from backup.json.',
                  path=str(B / PEBBLE_COPY), **diff_facts(ok, doc['pebble_home'], actual))
    if webview and doc.get('webview'):
        actual = tree_manifest(B / WEBVIEW_COPY)
        ok = same_tree(doc['webview'], actual)
        run.check('backup_webview_intact', ok, 'The backed-up webview store differs from backup.json.',
                  path=str(B / WEBVIEW_COPY), **diff_facts(ok, doc['webview'], actual))


def latest_record(run, B, prefix, command):
    best = None
    for name in sorted(os.listdir(B)):
        if not (name.startswith(prefix + '-') and name.endswith('.json')):
            continue
        try:
            doc = strict_json(read_explicit(B / name, MAX_RECORD, name), name)
        except (EvidenceError, Stop):
            continue
        if (isinstance(doc, dict) and doc.get('format') == RUN_FORMAT and doc.get('command') == command
                and doc.get('ok') is True and doc.get('execute') is True
                and (best is None or str(doc.get('finished_at')) > str(best[1].get('finished_at')))):
            best = (name, doc)
    run.check(prefix + '_record_present', best is not None, f'No passed {command} record exists in the backup root.',
              path=str(B))
    run.results[prefix + '_record'] = str(B / best[0])
    return best[1]


def check_prerequisite(run, command, path, backup_sha, bundle_sha, expect):
    run.check(command + '_report_given', path is not None,
              f'A real install needs --{"rehearsal" if command == "rehearse" else command}-report from a passed {command}.')
    try:
        data = read_explicit(path, MAX_RECORD, command + '_report')
    except EvidenceError as error:
        raise Stop(command + '_report_unreadable', 'The report is missing or unreadable.', path=str(path),
                   reason=error.code) from None
    report = strict_json(data, command + '_report')
    report = report if isinstance(report, dict) else {}
    results = report.get('results') if isinstance(report.get('results'), dict) else {}
    candidate = results.get('candidate') if isinstance(results.get('candidate'), dict) else {}
    run.check(command + '_report_passed', report.get('format') == RUN_FORMAT and report.get('command') == command
              and report.get('ok') is True and report.get('execute') is True,
              f'The {command} report is not a passed, executed {command}.', path=str(path))
    binds = candidate.get('bundle_sha256') == bundle_sha and (
        results.get('backup_json_sha256') == backup_sha if command == 'rehearse'
        else results.get('expected_backend_sha256') == expect)
    run.check(command + '_report_binds', binds, f'The {command} report is for another candidate or backup.',
              path=str(path), report_sha256=digest(data))
    run.results[command + '_report'] = {'path': str(path), 'sha256': digest(data)}


def backup_roots(layout):
    """(path, stamp) of every backup root under the backups folder under either name, newest first."""
    try:
        names = os.listdir(layout.backups)
    except OSError:
        return []
    found = [(layout.backups / name, BACKUP_NAME.fullmatch(name)) for name in names]
    return sorted(((path, match.group(1)) for path, match in found if match), key=lambda item: (item[1], item[0].name),
                  reverse=True)


def private_root(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700


def read_backup_record(path, stamp):
    """(doc, sha256) of a root's backup.json when it is a complete record, else (None, None). Never raises."""
    try:
        data = read_explicit(Path(path) / 'backup.json', MAX_RECORD, 'backup.json')
        doc = strict_json(data, 'backup.json')
    except (EvidenceError, Stop):
        return None, None
    return (doc, digest(data)) if backup_record_ok(doc, stamp) else (None, None)


def reusable_old_folder(layout, B, home, walk):
    """V1-137: an earlier backup's own copy of this frozen old folder, verified byte for byte, or None.

    Only the folder a move left behind (it holds MOVED.md and pebble-moved.json) qualifies; only a real copy is reused,
    never another reference; shapes are compared first, so a folder that changed is never hashed here."""
    if not all(os.path.isfile(Path(home) / name) for name in sorted(MOVE_FILES)):
        return None
    live = None
    for path, stamp in backup_roots(layout):
        if path == B or not private_root(path):
            continue
        doc, sha = read_backup_record(path, stamp)
        record = (doc or {}).get('data_home')
        if not isinstance(record, dict) or record.get('copy') != DATA_COPY or shape(record) != shape(walk):
            continue
        try:
            live = live or tree_manifest(home)
            copied = tree_manifest(path / DATA_COPY)
        except Stop:
            continue
        if same_tree(record, copied) and same_tree(live, copied):
            return {'stamp': stamp, 'root': path.name, 'backup_json_sha256': sha}
    return None


def rerun_command(layout, command, stamp, *extra):
    argv = ['.runtime/agent-env/bin/python', 'scripts/pebble_install.py', command, '--stamp', stamp, *extra, '--execute']
    argv += ['--root', str(layout.root)] if layout.root is not None else ['--i-am-the-parent-with-user-approval', stamp]
    return shlex.join(argv)


# ---- Commands ----------------------------------------------------------------------------------


def cmd_backup(run):
    """§2.4: identify, copy the bundle, the data home, the webview store and ~/.pebble (Phase 22), verify, inspect,
    record. The database inspected is the live one: ~/.pebble/data once Pebble moved there, else the data home."""
    o, layout = run.options, run.layout
    stamp = o.get('stamp') or run.stamp()
    B, app, home, web = layout.backup_dir(stamp), layout.app, layout.data_home, layout.webview
    pebble = layout.pebble_home
    expect = o.get('expect_backend')
    run.results.update(stamp=stamp, backup_root=str(B))
    s = {}

    def preconditions():
        run.check('expected_backend_given', expect is not None, "Pass --expect-backend-sha256 with the installed "
                  "backend's SHA-256 (plan reports it as observed.installed.backend_sha256).")
        wait_quiet(run, app, o['wait_seconds'] if run.execute else 0)
        check_unowned(run, home)
        check_unowned(run, pebble / 'data')
        s['installed'] = ident = identify(app, 'installed')
        check_identity(run, ident, 'installed')
        run.check('installed_backend', ident['backend_sha256'] == expect,
                  'The installed backend is not the expected build: stop and report.', path=str(app / BACKEND),
                  sha256=ident['backend_sha256'], expected=expect)
        run.check('backup_root_absent', not any(os.path.lexists(path) for path in layout.backup_dirs(stamp)),
                  'This backup root already exists. Choose a new stamp; existing evidence is never reused or '
                  'overwritten.', path=str(B))
        check_backups_root(run, layout.backups)
        s['live'] = pebble_live(layout)
        if s['live']:
            s['home_present'] = present_dir(run, home, 'data_home')
            s['header'] = check_data_home(run, pebble / 'data')
        else:
            s['header'] = check_data_home(run, home)
            s['home_present'] = True
        s['web'] = present_dir(run, web, 'webview_store')
        s['pebble'] = present_dir(run, pebble, 'pebble_home')
        live = pebble / 'data' if s['live'] else home
        walk = tree_manifest(home, hashes=False) if s['home_present'] else None
        sizes = {'app': tree_manifest(app, hashes=False)['bytes'],
                 'data_home': walk['bytes'] if walk else 0,
                 'webview': tree_manifest(web, hashes=False)['bytes'] if s['web'] else 0,
                 'pebble_home': tree_manifest(pebble, hashes=False)['bytes'] if s['pebble'] else 0,
                 'database': os.lstat(live / migrations.DATABASE).st_size}
        # V1-137: once ~/.pebble is live, the old folder is frozen; an earlier backup's verified copy of it is reused.
        s['reuse'] = reusable_old_folder(layout, B, home, walk) if s['live'] and walk else None
        run.results['old_folder'] = ({'copy': 'reused', 'from': s['reuse']['root']} if s['reuse']
                                     else {'copy': 'ditto' if s['home_present'] else 'absent'})
        need = (sizes['app'] + (1 if s['reuse'] else 2) * sizes['data_home'] + sizes['webview']
                + 2 * sizes['pebble_home'] + sizes['database'] + o['margin_bytes'])
        check_space(run, B, need, sizes, 'the backup, its inspection scratch copy and the later rehearsal copy')

    def make_backups():
        if os.path.lexists(layout.backups):
            return 'exists'
        mkdir_new(layout.backups)

    def make_root():
        mkdir_new(B)
        run.report_dir, run.report_prefix = B, 'backup-run'

    def copy_bundle():
        wait_quiet(run, app, 0)
        copy(run, app, B / APP_COPY)

    def verify_bundle():
        source, copied = bundle_manifest(app), bundle_manifest(B / APP_COPY)
        problems = source['problems'] + copied['problems']
        run.check('bundle_walk_clean', not problems, 'The bundle walker refused entries.', problems=problems[:12])
        ok = same_bundle(source, copied)
        run.check('bundle_copy_matches', ok, 'The bundle copy differs from the installed bundle.', source=str(app),
                  copy=str(B / APP_COPY), **diff_facts(ok, source, copied))
        codesign_check(run, app)
        codesign_check(run, B / APP_COPY)
        s['app'] = source

    def copy_home():
        if not s['home_present']:
            return 'absent'
        if s['reuse']:
            return 'reused'
        wait_quiet(run, app, 0)
        check_unowned(run, home)
        copy(run, home, B / DATA_COPY)

    def checked_database(top, copied, database):
        s['db'] = checked = integrity(database)
        run.check('integrity_ok', checked['integrity'] == 'ok',
                  'PRAGMA integrity_check failed on the copied database (mode=ro&immutable=1).', path=str(database),
                  **checked)
        run.check('copy_unchanged_by_check', shape(tree_manifest(top, hashes=False)) == shape(copied),
                  'The integrity check changed the copy.', path=str(top))

    def verify_home():
        if not s['home_present']:
            s['home'] = None
            return 'absent'
        target = layout.backups / s['reuse']['root'] / DATA_COPY if s['reuse'] else B / DATA_COPY
        source, copied = tree_manifest(home), tree_manifest(target)
        problems = source['problems'] + copied['problems']
        run.check('data_walk_clean', not problems, 'The data home holds entries that cannot be copied or hashed.',
                  problems=problems[:12])
        ok = same_tree(source, copied)
        run.check('data_copy_matches', ok, 'The data-home copy differs from the data home.', source=str(home),
                  copy=str(target), **diff_facts(ok, source, copied))
        if not s['live']:
            checked_database(B / DATA_COPY, copied, B / DATA_COPY / migrations.DATABASE)
        s['home'] = source

    def copy_pebble():
        if not s['pebble']:
            return 'absent'
        wait_quiet(run, app, 0)
        check_unowned(run, pebble / 'data')
        copy(run, pebble, B / PEBBLE_COPY)

    def verify_pebble():
        s['pebble_tree'] = None
        if not s['pebble']:
            return 'absent'
        source, copied = tree_manifest(pebble), tree_manifest(B / PEBBLE_COPY)
        problems = source['problems'] + copied['problems']
        run.check('pebble_home_walk_clean', not problems, '~/.pebble holds entries that cannot be copied or hashed.',
                  problems=problems[:12])
        ok = same_tree(source, copied)
        run.check('pebble_home_copy_matches', ok, 'The ~/.pebble copy differs from ~/.pebble.', source=str(pebble),
                  copy=str(B / PEBBLE_COPY), **diff_facts(ok, source, copied))
        if s['live']:
            checked_database(B / PEBBLE_COPY, copied, B / PEBBLE_COPY / 'data' / migrations.DATABASE)
        s['pebble_tree'] = source

    def copy_web():
        if not s['web']:
            return 'skipped'
        copy(run, web, B / WEBVIEW_COPY)

    def verify_web():
        s['webtree'] = None
        if not s['web']:
            return 'skipped'
        source, copied = tree_manifest(web), tree_manifest(B / WEBVIEW_COPY)
        ok = same_tree(source, copied)
        run.check('webview_copy_matches', ok, 'The webview-store copy differs.', source=str(web),
                  copy=str(B / WEBVIEW_COPY), **diff_facts(ok, source, copied))
        s['webtree'] = source

    def live_copy():
        """(folder of the live database's copy, its tree, the database's path in that tree)."""
        if s['live']:
            return B / PEBBLE_COPY / 'data', s['pebble_tree'], 'data/' + migrations.DATABASE
        return B / DATA_COPY, s['home'], migrations.DATABASE

    def inspect_scratch():
        scratch = B / INSPECTION
        mkdir_new(scratch)
        source, tree, name = live_copy()
        copy(run, source / migrations.DATABASE, scratch / migrations.DATABASE)
        if os.path.isdir(source / migrations.BACKUPS):
            copy(run, source / migrations.BACKUPS, scratch / migrations.BACKUPS)
        entry = next(f for f in tree['files'] if f['path'] == name)
        with Root(str(scratch), 'inspection') as root:
            run.check('inspection_copy_matches', root.digest(migrations.DATABASE, MAX_FILE)[0] == entry['sha256'],
                      'The scratch database copy differs.', path=str(scratch / migrations.DATABASE))
        report = migrations.inspect(scratch)
        run.check('inspect_readable', report.get('state') in ('fresh', 'current', 'upgrade_required', 'upgrade_pending'),
                  'migrations.inspect refused the copied database.', state=report.get('state'),
                  refusal=report.get('refusal') or (report.get('database') or {}).get('refusal'))
        s['inspect'] = inspect_summary(report)

    def write_record():
        source, live_tree, name = live_copy()
        entry = next(f for f in live_tree['files'] if f['path'] == name)
        tree = ('tree_sha256', 'file_count', 'dir_count', 'link_count', 'bytes', 'mode', 'files', 'dirs', 'links')
        record = {
            'format': BACKUP_FORMAT, 'stamp': stamp, 'complete': True,
            'created_at': utc(run.started).isoformat(), 'completed_at': utc(run.env.clock()).isoformat(),
            'layout': layout.describe(), 'installed': s['installed'], 'expected_backend_sha256': expect,
            'app': {'copy': APP_COPY, **pick(s['app'], 'bundle_sha256', 'file_count', 'bytes', 'files', 'links')},
            # V1-137: an unchanged frozen old folder names the earlier root whose verified copy it is (copy: null).
            'data_home': None if s['home'] is None else {'copy': None if s['reuse'] else DATA_COPY,
                                                         **({'reused': s['reuse']} if s['reuse'] else {}),
                                                         **pick(s['home'], *tree)},
            'database': {'path': name, 'sha256': entry['sha256'], 'bytes': entry['bytes'],
                         'integrity': s['db']['integrity'], 'user_version': s['db']['user_version'],
                         'wal_mode': s['header'].get('wal_mode'), 'sidecars': sidecars(source)},
            'webview': None if s['webtree'] is None else {'copy': WEBVIEW_COPY, 'source': str(web),
                                                          **pick(s['webtree'], *tree)},
            # Phase 22: ~/.pebble when it exists, and which home held the live database.
            'pebble_home': None if s['pebble_tree'] is None else {'copy': PEBBLE_COPY, 'source': str(pebble),
                                                                  **pick(s['pebble_tree'], *tree)},
            'live_home': 'pebble_home' if s['live'] else 'legacy',
            'inspect': s['inspect'], 'inspected_copy': INSPECTION,
            'upgrade_backup_ids': sorted(item['id'] for item in s['inspect']['backups'] if isinstance(item.get('id'), str)),
            'tool': {'sha256': TOOL_SHA, 'migrations_sha256': MIGRATIONS_SHA, 'schema_epoch': EXPECTED_EPOCH},
        }
        sha = write_new(B / 'backup.json', record)
        home_tree = s['home'] or {}
        run.results.update(backup_json=str(B / 'backup.json'), backup_json_sha256=sha,
                           app_bundle_sha256=s['app']['bundle_sha256'], data_tree_sha256=home_tree.get('tree_sha256'),
                           data_files=home_tree.get('file_count'), data_bytes=home_tree.get('bytes'),
                           webview_copied=s['webtree'] is not None, integrity=s['db']['integrity'],
                           user_version=s['db']['user_version'], inspect_state=s['inspect']['state'],
                           pebble_home_copied=s['pebble_tree'] is not None, live_home=record['live_home'],
                           old_folder_reused_from=(s['reuse'] or {}).get('root'))

    run.steps([
        ('check', {'what': 'preconditions', 'bundle': app, 'data_home': home, 'backup_root': B}, preconditions, 'read'),
        ('mkdir', {'path': layout.backups, 'mode': '0700', 'when': 'absent'}, make_backups, 'write'),
        ('mkdir', {'path': B, 'mode': '0700', 'exclusive': True}, make_root, 'write'),
        ('ditto', {'source': app, 'destination': B / APP_COPY}, copy_bundle, 'write'),
        ('verify', {'what': 'bundle manifests equal; codesign --verify --deep --strict on both',
                    'paths': [app, B / APP_COPY]}, verify_bundle, 'read'),
        ('ditto', {'source': home, 'destination': B / DATA_COPY,
                   'when': 'unless ~/.pebble is live and an earlier backup holds an identical copy of the frozen old '
                           'folder (results.old_folder)'}, copy_home, 'write'),
        ('verify', {'what': 'data-home manifests equal; PRAGMA integrity_check on the copy (mode=ro&immutable=1)',
                    'paths': [home, B / DATA_COPY]}, verify_home, 'read'),
        ('ditto', {'source': web, 'destination': B / WEBVIEW_COPY, 'when': 'the store exists'}, copy_web, 'write'),
        ('verify', {'what': 'webview-store manifests equal', 'paths': [web, B / WEBVIEW_COPY]}, verify_web, 'read'),
        ('ditto', {'source': pebble, 'destination': B / PEBBLE_COPY, 'when': '~/.pebble exists'}, copy_pebble, 'write'),
        ('verify', {'what': '~/.pebble manifests equal; integrity_check when it holds the live database',
                    'paths': [pebble, B / PEBBLE_COPY]}, verify_pebble, 'read'),
        ('inspect', {'what': 'migrations.inspect on a scratch copy of pebble.sqlite and upgrade-backups/',
                     'scratch': B / INSPECTION}, inspect_scratch, 'write'),
        ('write', {'path': B / 'backup.json', 'mode': '0600', 'open': 'O_EXCL'}, write_record, 'write'),
    ])


def cmd_rehearse(run):
    """§2.3: upgrade a copy of the backed-up home with the candidate's backend, then inspect and restore it."""
    o, layout = run.options, run.layout
    stamp = need_stamp(run)
    candidate = o.get('candidate')
    run.check('rehearsal_inputs_given', candidate is not None and o.get('app_manifest') is not None,
              'Pass --candidate and --app-manifest.')
    label = REHEARSAL_PREFIX + run.stamp()
    if o.get('rehearsal_dir'):
        work = Path(o['rehearsal_dir'])
    else:
        work = (layout.root if layout.root is not None else REHEARSAL_PARENT) / label
    home, restored, tmpdir = work / 'home', work / 'restored', work / 'tmp'
    # Phase 22 (--data-layout pebble-home): a temporary ~/.pebble beside the old home's copy; the candidate moves
    # the copy into it exactly as on first launch. Neither the real ~/.pebble nor ~/Documents is ever written.
    moving = data_layout(run) == 'pebble-home'
    pebble = work / 'pebble'
    s = {}

    def preconditions():
        B, s['doc'], s['sha'] = load_backup(run, layout, stamp)
        s['B'] = B
        run.report_dir, run.report_prefix = B, 'rehearsal'
        run.check('rehearsal_layout_supported', moving or s['doc'].get('data_home') is not None,
                  'This backup has no old data home: rehearse it with --data-layout pebble-home.')
        check_backup_intact(run, B, s['doc'], bundle=False, webview=False)
        s['manifest'] = load_app_manifest(o['app_manifest'], o.get('app_key', 'release'))
        s['tree'] = check_candidate(run, candidate, s['manifest'])
        s['ident'] = identify(candidate, 'candidate')
        check_identity(run, s['ident'], 'candidate')
        run.check('rehearsal_dir_absent', not os.path.lexists(work), 'The rehearsal directory must be new.', path=str(work))
        run.check('rehearsal_dir_parent', os.path.isdir(work.parent), 'The rehearsal directory parent is missing.',
                  path=str(work.parent))
        clashes = [str(p) for p in (layout.data_home, B, layout.app, candidate, layout.webview, layout.pebble_home)
                   if overlaps(work, p)]
        run.check('rehearsal_dir_separate', not clashes, 'The rehearsal directory overlaps a protected path.',
                  path=str(work), overlaps=clashes)
        sizes = {'data_home': (s['doc'].get('data_home') or {}).get('bytes', 0),
                 'pebble_home': (s['doc'].get('pebble_home') or {}).get('bytes', 0) if moving else 0}
        need = 2 * sum(sizes.values()) + o['margin_bytes']
        check_space(run, work.parent, need, sizes, 'the rehearsal copy and its upgrade backup')
        run.results.update(rehearsal_dir=str(work), candidate=candidate_facts(candidate, s['manifest'], s['tree'], s['ident']),
                           migrations_sha256=MIGRATIONS_SHA, data_layout='pebble-home' if moving else 'legacy')

    def make():
        mkdir_new(work)
        mkdir_new(tmpdir)

    def copy_home():
        if s['doc'].get('data_home') is None:
            return 'absent'
        copy(run, data_copy(layout, s['B'], s['doc']), home)  # V1-137: possibly an earlier root's verified copy

    def verify_copy():
        if s['doc'].get('data_home') is None:
            return 'absent'
        actual = tree_manifest(home)
        ok = same_tree(s['doc']['data_home'], actual)
        run.check('rehearsal_copy_matches', ok, 'The rehearsal copy differs from backup.json.', path=str(home),
                  **diff_facts(ok, s['doc']['data_home'], actual))

    def copy_pebble():
        if not moving or not s['doc'].get('pebble_home'):
            return 'skipped'
        copy(run, s['B'] / PEBBLE_COPY, pebble)
        actual = tree_manifest(pebble)
        ok = same_tree(s['doc']['pebble_home'], actual)
        run.check('rehearsal_pebble_copy_matches', ok, 'The ~/.pebble rehearsal copy differs from backup.json.',
                  path=str(pebble), **diff_facts(ok, s['doc']['pebble_home'], actual))

    def start():
        protected = [layout.user_home / 'Library', layout.applications, layout.backups, candidate]
        if not moving:
            return launch(run, candidate, home, work, tmpdir, protected)
        # The real ~/.pebble and ~/Documents stay read-only to the rehearsal (settings.json may name a folder there),
        # and so does any other folder the copied settings.json names for the Documents copies.
        protected += [layout.pebble_home, layout.user_home / 'Documents']
        protected += [Path(path) for path in (named_documents_folder(pebble),) if path]
        launch(run, candidate, pebble, work, tmpdir, protected,
               legacy=home if s['doc'].get('data_home') is not None else None)

    def recheck():
        check_candidate(run, candidate, s['manifest'], 'candidate_unchanged_by_launch')

    def inspect_copy():
        if not moving:
            report = migrations.inspect(home, deep=True)
            check_current(run, report, s['doc'], home)
            s['new'] = check_new_upgrade_backups(run, home, s['doc'], report)
            return
        data = pebble / 'data'
        report = migrations.inspect(data, deep=True)
        check_current(run, report, s['doc'], data)
        check_moved(run, pebble, s['doc'], home)
        if s['doc'].get('data_home') is not None and s['doc'].get('live_home') != 'pebble_home':
            check_old_folder(run, home, s['doc']['data_home'])
        s['new'] = check_upgrade_backups_after_move(run, data, s['doc'], report)
        s['restore_home'] = data

    def restore():
        if not s['new']:
            # Same schema epoch: no upgrade backup exists; the old home's verified copy is the rollback copy.
            run.results['restore_copy'] = 'not_needed_same_epoch'
            run.results['passed'] = True
            return 'skipped'
        backup_id = s['new'][-1]
        try:
            result = migrations.restore_copy(s.get('restore_home', home), backup_id, restored)
        except migrations.MigrationRefused as refusal:
            raise Stop('restore_copy_refused', 'restore_copy refused the new upgrade backup.', backup_id=backup_id,
                       reason=refusal.reason) from None
        run.results['restore_copy'] = pick(result, 'restored', 'backup_id', 'integrity', 'logical_match', 'tables', 'rows',
                                           'user_version', 'compatible')
        run.check('restore_copy_valid', result.get('restored') is True and result.get('logical_match') is True
                  and result.get('integrity') == 'ok', 'restore_copy did not produce a valid copy.', target=str(restored))
        rows = (migrations.inspect(restored).get('database') or {}).get('rows') or {}
        before = baseline_rows(s['doc'])
        run.check('restored_rows_match_backup', rows == before,
                  "The restored pre-upgrade copy does not have the backup's row counts.",
                  changes=[{'table': t, 'before': before.get(t), 'after': rows.get(t)}
                           for t in sorted(set(before) | set(rows)) if before.get(t) != rows.get(t)][:24])
        run.results['passed'] = True

    run.steps([
        ('check', {'what': 'backup intact; candidate equals app-manifest.json', 'backup_root': layout.backup_dir(stamp),
                   'candidate': candidate}, preconditions, 'read'),
        ('mkdir', {'path': work, 'mode': '0700', 'exclusive': True}, make, 'write'),
        ('ditto', {'source': layout.backup_dir(stamp) / DATA_COPY, 'destination': home}, copy_home, 'write'),
        ('verify', {'what': 'rehearsal copy equals backup.json', 'path': home}, verify_copy, 'read'),
        *([('ditto', {'source': layout.backup_dir(stamp) / PEBBLE_COPY, 'destination': pebble,
                      'when': 'backup.json recorded ~/.pebble'}, copy_pebble, 'write')] if moving else []),
        ('launch', {'backend': (candidate / BACKEND) if candidate else None, 'home': pebble if moving else home,
                    **({'legacy_home': home} if moving else {}),
                    'resources': (candidate / RESOURCES) if candidate else None,
                    'sandbox': 'sandbox-exec: loopback-only network, protected paths read-only, no Keychain services',
                    'requests': ['state', 'upgrade_backups'], 'then': 'close stdin and wait'}, start, 'write'),
        ('verify', {'what': 'candidate unchanged by the launch', 'path': candidate}, recheck, 'read'),
        ('inspect', {'what': 'inspect(copy, deep=True): current, epoch 9, rows unchanged; verify_backup'
                     + ('; the move record; the old home intact apart from MOVED.md' if moving else ''),
                     'home': pebble / 'data' if moving else home}, inspect_copy, 'read'),
        ('restore_copy', {'home': pebble / 'data' if moving else home, 'target': restored,
                          **({'when': 'a new upgrade backup exists'} if moving else {})}, restore, 'write'),
    ])


def cmd_install(run):
    """§2.5 and §6.7: re-hash, check the release manifest, move the old bundle aside, copy, verify, codesign."""
    o, layout = run.options, run.layout
    stamp = need_stamp(run)
    app, home, candidate = layout.app, layout.data_home, o.get('candidate')
    B = layout.backup_dir(stamp)
    moved = B / MOVED
    run.check('install_inputs_given', candidate is not None and o.get('app_manifest') is not None
              and o.get('expect_backend') is not None, 'Pass --candidate, --app-manifest and --expect-backend-sha256.')
    s = {}

    def preconditions():
        wait_quiet(run, app, o['wait_seconds'] if run.execute else 0)
        check_unowned(run, home)
        _, s['doc'], s['sha'] = load_backup(run, layout, stamp)
        run.report_dir, run.report_prefix = B, 'install'
        check_backup_intact(run, B, s['doc'])
        s['installed'] = ident = identify(app, 'installed')
        check_identity(run, ident, 'installed')
        run.check('installed_backend', ident['backend_sha256'] == o['expect_backend'] == s['doc']['installed'].get(
            'backend_sha256'), 'The installed backend differs from the expected hash or from backup.json.',
            path=str(app / BACKEND), sha256=ident['backend_sha256'], expected=o['expect_backend'])
        installed = bundle_manifest(app)
        ok = same_bundle(s['doc']['app'], installed)
        run.check('installed_unchanged_since_backup', ok, 'The installed bundle changed since the backup.', path=str(app),
                  **diff_facts(ok, s['doc']['app'], installed))
        # A rollback restores the backup's data home, so a change made after the backup would be lost.
        for path, recorded, what in ((home, s['doc']['data_home'], 'data_home'),
                                     (layout.webview, s['doc'].get('webview'), 'webview')):
            if recorded is None:
                continue
            live = tree_manifest(path)
            ok = same_tree(recorded, live)
            run.check(what + '_unchanged_since_backup', ok, f'The {what} changed after the backup (was Pebble opened?). '
                      'Take a new backup: a rollback would lose that change.', path=str(path),
                      **diff_facts(ok, recorded, live))
        # Phase 22: a database already in ~/.pebble/data (with no move receipt) would keep the new Pebble on the old
        # folder ("both homes have data"); a recorded ~/.pebble must still be what the backup holds.
        pebble = layout.pebble_home
        check_unowned(run, pebble / 'data')
        run.check('pebble_home_free_for_the_move', s['doc'].get('live_home') == 'pebble_home'
                  or not os.path.lexists(pebble / 'data' / migrations.DATABASE),
                  '~/.pebble/data already holds a database, so the new Pebble would keep using the old folder. Move '
                  '~/.pebble aside (never delete it), take a new backup and retry.', path=str(pebble / 'data'))
        if s['doc'].get('pebble_home') is not None:
            live = tree_manifest(pebble)
            ok = same_tree(s['doc']['pebble_home'], live)
            run.check('pebble_home_unchanged_since_backup', ok, '~/.pebble changed after the backup. Take a new backup.',
                      path=str(pebble), **diff_facts(ok, s['doc']['pebble_home'], live))
        s['manifest'] = manifest = load_app_manifest(o['app_manifest'], o.get('app_key', 'release'))
        s['tree'] = tree = check_candidate(run, candidate, manifest)
        s['ident'] = cid = identify(candidate, 'candidate')
        check_identity(run, cid, 'candidate')
        codesign_check(run, candidate)
        run.check('version_higher', version_key(cid['version']) > version_key(ident['version']),
                  'The candidate version must be higher than the installed version (bump it before the final trial).',
                  candidate=cid['version'], installed=ident['version'])
        run.results.update(candidate=candidate_facts(candidate, manifest, tree, cid),
                           previous=pick(ident, 'version', 'build', 'backend_sha256', 'shell_sha256'),
                           expected_backend_sha256=o['expect_backend'])
        release = o.get('release_manifest')
        # Parent follow-up 3: a pinned release key requires an accepted signed manifest, as --require-signed does; with
        # no release key pinned the flag alone decides, as before. A drill's own staged install is exempt: its
        # backup.json is the drill's, which no real release manifest can name (T3 binds `rollback` to the backup).
        pinned = release_key_pinned(run.env.verifier)
        drill = o.get('release_not_required') == 'drill' and layout.root is not None
        required = bool(o.get('require_signed')) or (pinned and not drill)
        run.results['release_required'] = {'required': required, 'require_signed': bool(o.get('require_signed')),
                                           'release_key_pinned': pinned, 'drill_exempt': pinned and drill}
        if release is None:
            run.check('release_manifest_given', not required, 'A signed release manifest is required (--require-signed, '
                      'or a pinned release key in backend/pebble/trust_roots.py); pass --release-manifest.',
                      release_key_pinned=pinned)
            run.results['release'] = None
        else:
            release_check(run, release, require_signed=required, bundle=tree['bundle_sha256'],
                          backend=cid['backend_sha256'], version=cid['version'],
                          installed_backend=ident['backend_sha256'], installed_version=ident['version'],
                          backup_sha=s['sha'], data_version=s['doc']['database'].get('user_version'))
        for command, key in (('rehearse', 'rehearsal_report'), ('drill', 'drill_report')):
            if o.get(key) is None and layout.root is not None:
                run.results[key] = 'not_required_under_root'
                continue
            check_prerequisite(run, command, o.get(key), s['sha'], tree['bundle_sha256'], o['expect_backend'])
        run.check('moved_destination_absent', not os.path.lexists(moved), 'The moved-aside name is taken: roll back '
                  'with --bundle-source moved, or take a new backup.', path=str(moved))
        same_device(run, app, B, 'The installed bundle')
        check_space(run, app.parent, tree['bytes'] + o['margin_bytes'], {'candidate': tree['bytes']}, 'the new bundle')
        run.results['logs'] = log_offsets(home)

    def move_old():
        wait_quiet(run, app, 0)
        check_unowned(run, home)
        move(run, app, moved)
        run.recovery = [f'The previous bundle is at {moved}. To restore it and the data home: '
                        + rerun_command(layout, 'rollback', stamp)]
        ident = identify(moved, 'moved')
        run.check('moved_bundle_identity', ident['backend_sha256'] == o['expect_backend'],
                  'The moved-aside bundle is not the backed-up one.', path=str(moved))

    def copy_new():
        try:
            copy(run, candidate, app)
        except Stop as stop:
            intact = bundle_intact(moved, s['doc']['app'])
            run.results['moved_bundle_intact'] = intact
            run.recovery = [f'The copy into {app} was interrupted ({stop.code}); {app} may be partial.',
                            f'The previous bundle is {"intact" if intact else "NOT intact"} at {moved} '
                            '(re-hashed against backup.json).',
                            'Roll back; the partial bundle is moved aside, never deleted: '
                            + rerun_command(layout, 'rollback', stamp)]
            raise Stop('copy_interrupted', 'The new bundle was not copied completely.', destination=str(app),
                       moved=str(moved), moved_bundle_intact=intact, cause=stop.code, cause_facts=stop.facts) from None

    def verify_new():
        actual = bundle_manifest(app)
        ok = same_bundle(s['manifest'], actual)
        run.check('installed_tree_matches', ok, 'The installed tree differs from app-manifest.json.', path=str(app),
                  **diff_facts(ok, s['manifest'], actual))
        ident = identify(app, 'installed_candidate')
        run.check('installed_backend_is_candidate', ident['backend_sha256'] == s['ident']['backend_sha256'],
                  'The installed backend is not the candidate backend.', path=str(app / BACKEND))

    def sign():
        codesign_check(run, app)
        run.recovery = []
        run.results['next'] = ('Ask the user to open the installed app and answer its prompts, then run: '
                               + rerun_command(layout, 'readback', stamp))

    run.steps([
        ('check', {'what': 'not running; backup intact; installed identity; candidate equals app-manifest.json; '
                           'release manifest; prerequisite reports', 'bundle': app, 'candidate': candidate},
         preconditions, 'read'),
        ('rename', {'source': app, 'destination': moved, 'exclusive': True}, move_old, 'write'),
        ('ditto', {'source': candidate, 'destination': app}, copy_new, 'write'),
        ('verify', {'what': 'installed tree equals app-manifest.json; backend is the candidate', 'path': app},
         verify_new, 'read'),
        ('codesign', {'argv': [CODESIGN, '--verify', '--deep', '--strict', '--verbose=2', app]}, sign, 'read'),
    ])


def cmd_readback(run):
    """§2.6 (mode install), or the post-rollback check of §2.7 step 6 (mode rollback)."""
    o, layout = run.options, run.layout
    stamp = need_stamp(run)
    mode = o.get('mode') or 'install'
    app, home = layout.app, layout.data_home
    # Phase 22: after a pebble-home install, both homes are read back: ~/.pebble (logs, the live database, the move
    # record) and the old data home (unchanged apart from MOVED.md and pebble-moved.json).
    moving = data_layout(run) == 'pebble-home' and mode == 'install'
    pebble = layout.pebble_home
    s = {}

    def records():
        B, s['doc'], _ = load_backup(run, layout, stamp)
        run.report_dir, run.report_prefix, run.always_record = B, 'readback-' + mode, True
        if mode == 'install':
            s['install'] = latest_record(run, B, 'install', 'install')
        # 1.0 (V1-006): after rolling back a backup taken while ~/.pebble was live, the restored home is ~/.pebble.
        s['post_move'] = mode == 'rollback' and s['doc'].get('live_home') == 'pebble_home'
        run.results['restored_home'] = ('pebble_home' if s['post_move'] else 'legacy') if mode == 'rollback' else None

    def processes():
        if o.get('skip_processes'):
            return 'skipped'
        rows = observe(run.env)
        shells, backends = exactly(app / SHELL, rows), exactly(app / BACKEND, rows)
        services = [row for row in backends if row['role'] != 'worker']
        known = {row['pid'] for row in shells + backends}
        run.results['processes'] = {
            'shell': shells, 'backend': services, 'workers': [row for row in backends if row['role'] == 'worker'][:16],
            'other_inside_bundle': [row for row in inside(app, rows) if row['pid'] not in known][:16],
            'similar_outside_bundle': similar_outside(app, rows)[:16]}
        run.check('one_shell', len(shells) == 1, 'Expected exactly one pebble process from the installed bundle.',
                  path=str(app / SHELL), found=len(shells))
        run.check('one_backend', len(services) == 1, 'Expected exactly one pebble-backend service from the installed '
                  'bundle (workers excluded; an unreadable argv counts as a service).', path=str(app / BACKEND),
                  found=len(services))

    def logs():
        offsets = (s['install'].get('results') or {}).get('logs') if mode == 'install' else backup_log_offsets(s['doc'])
        scan = new_tracebacks(home, offsets)
        if moving or s['post_move']:
            for name, entry in new_tracebacks(pebble / 'logs', pebble_log_offsets(s['doc'])).items():
                scan['pebble:' + name] = entry
        run.results['logs'] = scan
        run.check('logs_scanned', all(item.get('scanned') for item in scan.values()), 'A log could not be scanned.',
                  logs=scan)
        run.check('no_new_tracebacks', not sum(item.get('tracebacks', 0) for item in scan.values()),
                  'New tracebacks appeared in service.log or backend.log.', logs=scan)

    def bundle():
        if mode == 'install':
            facts = (s['install'].get('results') or {}).get('candidate')
            facts = facts if isinstance(facts, dict) else {}
            expected = load_app_manifest(o.get('app_manifest') or facts.get('manifest_path'),
                                         facts.get('manifest_key') or o.get('app_key', 'release'))
            run.check('app_manifest_unchanged', expected['sha256'] == facts.get('manifest_sha256'),
                      'app-manifest.json is not the one the install checked.', path=expected['path'])
        else:
            expected = s['doc']['app']
        actual = bundle_manifest(app)
        ok = same_bundle(expected, actual)
        run.check('installed_tree_matches', ok, 'The installed tree differs from its record.', path=str(app),
                  **diff_facts(ok, expected, actual))
        if mode == 'rollback':
            ident = identify(app, 'restored')
            want = o.get('expect_backend') or s['doc']['installed'].get('backend_sha256')
            run.check('restored_backend', ident['backend_sha256'] == want, 'The restored backend is not the backed-up one.',
                      path=str(app / BACKEND), sha256=ident['backend_sha256'], expected=want)

    def data():
        if moving:
            live = pebble / 'data'
            report = migrations.inspect(live)
            check_current(run, report, s['doc'], live)
            check_moved(run, pebble, s['doc'], home)
            if s['doc'].get('data_home') is not None and s['doc'].get('live_home') != 'pebble_home':
                check_old_folder(run, home, s['doc']['data_home'])
            check_upgrade_backups_after_move(run, live, s['doc'], report)
            run.results['passed'] = True
            return
        if s['post_move']:
            # The restored ~/.pebble/data at the backed-up epoch and rows, opened in place by the restored app; the
            # old folder still as backed up, apart from what reading it may add.
            live = pebble / 'data'
            report = migrations.inspect(live)
            database = report.get('database') or {}
            run.results['inspect'] = inspect_summary(report)
            run.check('restored_epoch', database.get('user_version') == s['doc']['database'].get('user_version'),
                      'The restored database is not at the backed-up epoch.', user_version=database.get('user_version'),
                      expected=s['doc']['database'].get('user_version'), home=str(live))
            check_rows(run, s['doc'], database.get('rows') or {})
            check_moved(run, pebble, s['doc'], home)
            if s['doc'].get('data_home') is not None and os.path.lexists(home):
                check_old_folder(run, home, s['doc']['data_home'])
            run.results['passed'] = True
            return
        report = migrations.inspect(home)
        if mode == 'install':
            check_current(run, report, s['doc'], home)
            check_new_upgrade_backups(run, home, s['doc'], report)
        else:
            database = report.get('database') or {}
            run.results['inspect'] = inspect_summary(report)
            run.check('restored_epoch', database.get('user_version') == s['doc']['database'].get('user_version'),
                      'The restored database is not at the backed-up epoch.', user_version=database.get('user_version'),
                      expected=s['doc']['database'].get('user_version'))
            check_rows(run, s['doc'], database.get('rows') or {})
        run.results['passed'] = True

    run.steps([
        ('check', {'what': 'backup.json and the passed install record', 'backup_root': layout.backup_dir(stamp)},
         records, 'read'),
        ('check', {'what': 'exactly one shell and one backend from the bundle', 'shell': app / SHELL,
                   'backend': app / BACKEND}, processes, 'read'),
        ('check', {'what': 'no new tracebacks since install', 'logs': [home / name for name in LOGS]
                   + ([pebble / 'logs' / name for name in LOGS] if moving else [])}, logs, 'read'),
        ('verify', {'what': 'installed tree equals app-manifest.json (rollback: backup.json)', 'path': app}, bundle,
         'read'),
        ('inspect', {'what': ('inspect(~/.pebble/data): current, epoch 9, rows equal backup.json; the move record; the '
                              'old data home intact apart from MOVED.md; a new upgrade backup only for a newer epoch')
                     if moving else 'inspect(H): current, epoch 9, upgrade_history >= 1, rows equal backup.json, a new '
                     'valid upgrade backup' if mode == 'install' else 'inspect(H, or ~/.pebble/data when the backup was '
                     'taken while ~/.pebble was live): backed-up epoch, rows equal backup.json',
                     'home': pebble / 'data' if moving else home, 'note': TOUCH}, data, 'touch'),
    ])


def restore_bundle(run, source, app, kind):
    """The backed-up bundle back in place: ditto from its copy, or the install's moved-aside bundle renamed back."""
    if kind == 'copy':
        copy(run, source, app)
    else:
        move(run, source, app)


def verify_restored_bundle(run, doc, app):
    actual = bundle_manifest(app)
    ok = same_bundle(doc['app'], actual)
    run.check('restored_bundle_matches', ok, 'The restored bundle differs from backup.json.', path=str(app),
              **diff_facts(ok, doc['app'], actual))
    ident = identify(app, 'restored')
    want = run.options.get('expect_backend') or doc['installed'].get('backend_sha256')
    run.check('restored_backend', ident['backend_sha256'] == want, 'The restored backend is not the backed-up one.',
              path=str(app / BACKEND), sha256=ident['backend_sha256'], expected=want)
    codesign_check(run, app)


def restore_webview(run, doc, B, web):
    if doc.get('webview') is None:
        return 'skipped'
    run.check('webview_parent_present', os.path.isdir(web.parent), 'The webview store parent is missing.',
              path=str(web.parent))
    copy(run, B / WEBVIEW_COPY, web)
    actual = tree_manifest(web)
    ok = same_tree(doc['webview'], actual)
    run.check('restored_webview_matches', ok, 'The restored webview store differs from backup.json.', path=str(web),
              **diff_facts(ok, doc['webview'], actual))


def cmd_rollback(run):
    """§2.7: move the failed bundle and the post-install home aside, restore data then bundle, re-hash.

    The home restored is the one that was live at the backup: the old data home (below), or ~/.pebble for a backup
    taken while ~/.pebble/data was live (rollback_pebble_home, V1-006)."""
    o, layout = run.options, run.layout
    stamp = need_stamp(run)
    app, home, web = layout.app, layout.data_home, layout.webview
    B = layout.backup_dir(stamp)
    peeked = peek_backup(B)
    if peeked.get('live_home') == 'pebble_home':
        return rollback_pebble_home(run, stamp, B, peeked)
    kind = o.get('bundle_source') or 'copy'
    source = B / (app_copy(peeked) if kind == 'copy' else MOVED)
    now = run.stamp()
    failed, after, web_after = B / f'{APP}.failed-{now}', B / f'{DATA_COPY}.after-{now}', B / f'{WEBVIEW_COPY}.after-{now}'
    s = {}

    def preconditions():
        wait_quiet(run, app, o['wait_seconds'] if run.execute else 0)
        check_unowned(run, home)
        _, s['doc'], _ = load_backup(run, layout, stamp)
        doc = s['doc']
        run.report_dir, run.report_prefix = B, 'rollback'
        # Phase 22: a backup taken while the old data home was live restores the old app and the old data home and
        # leaves ~/.pebble exactly as it is. (A ~/.pebble-live backup takes rollback_pebble_home; a record that changed
        # since this run chose its steps is refused here.)
        run.check('rollback_supported', doc.get('live_home') != 'pebble_home' and doc.get('data_home') is not None
                  and app_copy(doc) == app_copy(peeked),
                  'backup.json changed while this rollback was being planned, or records no old data home; rerun it.',
                  backup=str(B / 'backup.json'))
        check_unowned(run, layout.pebble_home / 'data')
        s['pebble'] = (shape(tree_manifest(layout.pebble_home, hashes=False))
                       if os.path.isdir(layout.pebble_home) and not os.path.islink(layout.pebble_home) else None)
        check_backup_intact(run, B, doc, bundle_from=app_copy(doc) if kind == 'copy' else MOVED)
        database = next((f for f in doc['data_home'].get('files') or () if f.get('path') == migrations.DATABASE), None)
        run.check('restore_source_is_backup_copy', database is not None and doc['data_home'].get('copy') == DATA_COPY
                  and database.get('sha256') == doc['database'].get('sha256'),
                  'The data source must be the ditto copy recorded in backup.json; an upgrade-backups SQLite file is '
                  'never put into a live home.', source=str(B / DATA_COPY))
        s['app'], s['home'] = os.path.lexists(app), os.path.lexists(home)
        s['web'] = doc.get('webview') is not None
        s['web_present'] = s['web'] and os.path.lexists(web)
        for path in (failed, after) + ((web_after,) if s['web_present'] else ()):
            run.check('aside_destination_absent', not os.path.lexists(path), 'A move-aside name is taken.', path=str(path))
        if s['app']:
            same_device(run, app, B, 'The failed bundle')
        if s['home']:
            same_device(run, home, B, 'The post-install data home')
        if s['web_present']:
            same_device(run, web, B, 'The webview store')
        if kind == 'moved':
            same_device(run, source, app.parent, 'The moved-aside bundle')
        run.check('restore_parents_present', os.path.isdir(app.parent) and os.path.isdir(home.parent),
                  'A restore destination parent is missing.', paths=[str(app.parent), str(home.parent)])
        sizes = {'data_home': doc['data_home']['bytes'], 'app': doc['app']['bytes'] if kind == 'copy' else 0,
                 'webview': (doc.get('webview') or {}).get('bytes') or 0}
        check_space(run, home.parent, sum(sizes.values()) + o['margin_bytes'], sizes, 'the restored data and bundle')
        run.results.update(failed_bundle=str(failed) if s['app'] else None, after_home=str(after) if s['home'] else None,
                           after_webview=str(web_after) if s['web_present'] else None, bundle_source=str(source))

    def move_failed():
        run.recovery = [f'Nothing is deleted. Backup copies stay in {B}; a moved-aside bundle is {failed}, a moved-aside '
                        f'home is {after}. Fix the cause, then rerun: '
                        + rerun_command(layout, 'rollback', stamp, *(('--bundle-source', 'moved') if kind == 'moved' else ()))]
        if not s['app']:
            return 'absent'
        wait_quiet(run, app, 0)
        move(run, app, failed)

    def move_home():
        if not s['home']:
            return 'absent'
        check_unowned(run, home)
        move(run, home, after)

    def verify_data():
        actual = tree_manifest(home)
        ok = same_tree(s['doc']['data_home'], actual)
        run.check('restored_data_matches', ok, 'The restored data home differs from backup.json.', path=str(home),
                  **diff_facts(ok, s['doc']['data_home'], actual))

    def move_web():
        if not s['web_present']:
            return 'absent'
        move(run, web, web_after)

    def finish():
        if s['pebble'] is not None:
            now = shape(tree_manifest(layout.pebble_home, hashes=False))
            run.check('pebble_home_untouched', now == s['pebble'], 'The rollback changed ~/.pebble.',
                      path=str(layout.pebble_home))
            run.results['pebble_home'] = {'path': str(layout.pebble_home), 'state': 'left untouched',
                                          'note': 'The restored app uses the restored old data home. A later install '
                                                  'of the new version uses ~/.pebble/data as it was, and its Data & '
                                                  'storage page reports that the old folder changed.'}
        run.recovery = []
        run.results['passed'] = True
        # Phase 22 (k22-identity): the vault helper copies secrets into one item but keeps the old per-item ones.
        run.results['keychain'] = ('The old per-item Keychain items are kept, so the restored build starts signed in, '
                                   'unless ChatGPT refreshed its token after the upgrade: then sign in once.')
        run.results['next'] = ('Ask the user to open the restored app, then run: '
                               + rerun_command(layout, 'readback', stamp, '--mode', 'rollback'))

    run.steps([
        ('check', {'what': 'not running; backup copies intact against backup.json', 'backup_root': B}, preconditions,
         'read'),
        ('rename', {'source': app, 'destination': failed, 'when': 'present'}, move_failed, 'write'),
        ('rename', {'source': home, 'destination': after, 'when': 'present'}, move_home, 'write'),
        ('ditto', {'source': B / DATA_COPY, 'destination': home}, lambda: copy(run, B / DATA_COPY, home), 'write'),
        ('verify', {'what': 'restored data home equals backup.json', 'path': home}, verify_data, 'read'),
        ('ditto' if kind == 'copy' else 'rename', {'source': source, 'destination': app},
         lambda: restore_bundle(run, source, app, kind), 'write'),
        ('verify', {'what': 'restored bundle equals backup.json; backend hash; codesign', 'path': app},
         lambda: verify_restored_bundle(run, s['doc'], app), 'read'),
        ('rename', {'source': web, 'destination': web_after, 'when': 'recorded and present'}, move_web, 'write'),
        ('ditto', {'source': B / WEBVIEW_COPY, 'destination': web, 'when': 'recorded'},
         lambda: restore_webview(run, s['doc'], B, web), 'write'),
        ('finish', {'what': 'report'}, finish, 'read'),
    ])


def rollback_pebble_home(run, stamp, B, peeked):
    """V1-006: roll back a backup taken while ~/.pebble/data was the live home (every Mac that ran 0.6.x).

    ~/.pebble is moved aside into B and restored from B/pebble-home, then verified against backup.json. The restored
    Documents copy index (runtime/mirror.json) is moved aside into B, so the restored Pebble starts a new index and never
    reads Memory files that the newer version wrote as deletions. The bundle and the webview store are restored as for
    any rollback. The old data home is never touched, and no Keychain item is read or changed."""
    o, layout = run.options, run.layout
    app, home, web, pebble = layout.app, layout.data_home, layout.webview, layout.pebble_home
    kind = o.get('bundle_source') or 'copy'
    source = B / (app_copy(peeked) if kind == 'copy' else MOVED)
    now = run.stamp()
    failed, after = B / f'{APP}.failed-{now}', B / f'{PEBBLE_COPY}.after-{now}'
    web_after, mirror_aside = B / f'{WEBVIEW_COPY}.after-{now}', B / f'{PEBBLE_COPY}.mirror-{now}.json'
    s = {}

    def preconditions():
        wait_quiet(run, app, o['wait_seconds'] if run.execute else 0)
        check_unowned(run, home)
        check_unowned(run, pebble / 'data')
        _, s['doc'], _ = load_backup(run, layout, stamp)
        doc = s['doc']
        run.report_dir, run.report_prefix = B, 'rollback'
        run.check('rollback_record_unchanged', doc.get('live_home') == 'pebble_home'
                  and isinstance(doc.get('pebble_home'), dict) and app_copy(doc) == app_copy(peeked),
                  'backup.json changed while this rollback was being planned; rerun it.', path=str(B / 'backup.json'))
        check_backup_intact(run, B, doc, bundle_from=app_copy(doc) if kind == 'copy' else MOVED, old_folder=False)
        name = 'data/' + migrations.DATABASE
        database = next((f for f in doc['pebble_home'].get('files') or () if f.get('path') == name), None)
        run.check('restore_source_is_backup_copy', doc['database'].get('path') == name and database is not None
                  and database.get('sha256') == doc['database'].get('sha256'),
                  'The data source must be the ditto copy of ~/.pebble recorded in backup.json; an upgrade-backups '
                  'SQLite file is never put into a live home.', source=str(B / PEBBLE_COPY))
        s['app'] = os.path.lexists(app)
        s['pebble'] = present_dir(run, pebble, 'pebble_home')
        s['web'] = doc.get('webview') is not None
        s['web_present'] = s['web'] and os.path.lexists(web)
        s['mirror'] = any(f.get('path') == MIRROR_INDEX for f in doc['pebble_home'].get('files') or ())
        # The old folder is never touched: its shape now, compared again at the end.
        s['old'] = shape(tree_manifest(home, hashes=False)) if os.path.isdir(home) and not os.path.islink(home) else None
        s['old_present'] = os.path.lexists(home)
        for path in (failed, after, mirror_aside) + ((web_after,) if s['web_present'] else ()):
            run.check('aside_destination_absent', not os.path.lexists(path), 'A move-aside name is taken.', path=str(path))
        if s['app']:
            same_device(run, app, B, 'The failed bundle')
        same_device(run, pebble if s['pebble'] else pebble.parent, B, '~/.pebble')
        if s['web_present']:
            same_device(run, web, B, 'The webview store')
        if kind == 'moved':
            same_device(run, source, app.parent, 'The moved-aside bundle')
        run.check('restore_parents_present', os.path.isdir(app.parent) and os.path.isdir(pebble.parent),
                  'A restore destination parent is missing.', paths=[str(app.parent), str(pebble.parent)])
        sizes = {'pebble_home': doc['pebble_home']['bytes'], 'app': doc['app']['bytes'] if kind == 'copy' else 0,
                 'webview': (doc.get('webview') or {}).get('bytes') or 0}
        check_space(run, pebble.parent, sum(sizes.values()) + o['margin_bytes'], sizes, 'the restored ~/.pebble and bundle')
        run.results.update(data_layout='pebble-home', failed_bundle=str(failed) if s['app'] else None,
                           after_pebble_home=str(after) if s['pebble'] else None,
                           after_webview=str(web_after) if s['web_present'] else None, bundle_source=str(source),
                           mirror_index=str(mirror_aside) if s['mirror'] else None)

    def move_failed():
        run.recovery = [f'Nothing is deleted. Backup copies stay in {B}; a moved-aside bundle is {failed}, the moved-aside '
                        f'~/.pebble is {after}. Fix the cause, then rerun: '
                        + rerun_command(layout, 'rollback', stamp, *(('--bundle-source', 'moved') if kind == 'moved' else ()))]
        if not s['app']:
            return 'absent'
        wait_quiet(run, app, 0)
        move(run, app, failed)

    def move_pebble():
        if not s['pebble']:
            return 'absent'
        check_unowned(run, pebble / 'data')
        move(run, pebble, after)

    def verify_pebble():
        actual = tree_manifest(pebble)
        ok = same_tree(s['doc']['pebble_home'], actual)
        run.check('restored_pebble_home_matches', ok, 'The restored ~/.pebble differs from backup.json.', path=str(pebble),
                  **diff_facts(ok, s['doc']['pebble_home'], actual))

    def reset_mirror():
        if not s['mirror']:
            return 'absent'
        move(run, pebble / MIRROR_INDEX, mirror_aside)
        expected, actual = without(s['doc']['pebble_home'], {MIRROR_INDEX}), tree_manifest(pebble)
        ok = same_tree(expected, actual) and os.path.isfile(mirror_aside) and not os.path.islink(mirror_aside)
        run.check('mirror_index_reset', ok, 'The restored ~/.pebble is not the backup without its Documents copy index.',
                  path=str(pebble / MIRROR_INDEX), aside=str(mirror_aside), **diff_facts(ok, expected, actual))

    def move_web():
        if not s['web_present']:
            return 'absent'
        move(run, web, web_after)

    def finish():
        now_shape = (shape(tree_manifest(home, hashes=False))
                     if os.path.isdir(home) and not os.path.islink(home) else None)
        run.check('old_folder_untouched', now_shape == s['old'] and os.path.lexists(home) == s['old_present'],
                  'The rollback changed the old data home.', path=str(home))
        run.recovery = []
        run.results['passed'] = True
        run.results['old_folder'] = {'path': str(home), 'state': 'left untouched' if s['old_present'] else 'absent'}
        run.results['documents_index'] = ('reset: the restored Pebble starts a new index of ~/Documents/Pebble, so a '
                                          'Memory or conversation file it does not know is never read as a deletion'
                                          if s['mirror'] else 'none recorded')
        run.results['keychain'] = ('No Keychain item was read or changed. The vault item is shared by both versions and '
                                   'is not part of the backup, so the newest sign-in stays. If the newer version '
                                   'shipped a re-pinned vault helper, the restored helper keeps the access given to it '
                                   'before; answer a Keychain prompt with Always Allow.')
        run.results['next'] = ('Ask the user to open the restored app, then run: '
                               + rerun_command(layout, 'readback', stamp, '--mode', 'rollback', '--data-layout',
                                               'pebble-home'))

    run.steps([
        ('check', {'what': 'not running; the bundle, ~/.pebble and webview copies intact against backup.json',
                   'backup_root': B}, preconditions, 'read'),
        ('rename', {'source': app, 'destination': failed, 'when': 'present'}, move_failed, 'write'),
        ('rename', {'source': pebble, 'destination': after, 'when': 'present'}, move_pebble, 'write'),
        ('ditto', {'source': B / PEBBLE_COPY, 'destination': pebble}, lambda: copy(run, B / PEBBLE_COPY, pebble), 'write'),
        ('verify', {'what': 'restored ~/.pebble equals backup.json', 'path': pebble}, verify_pebble, 'read'),
        ('rename', {'source': pebble / MIRROR_INDEX, 'destination': mirror_aside,
                    'when': 'backup.json recorded a Documents copy index'}, reset_mirror, 'write'),
        ('ditto' if kind == 'copy' else 'rename', {'source': source, 'destination': app},
         lambda: restore_bundle(run, source, app, kind), 'write'),
        ('verify', {'what': 'restored bundle equals backup.json; backend hash; codesign', 'path': app},
         lambda: verify_restored_bundle(run, s['doc'], app), 'read'),
        ('rename', {'source': web, 'destination': web_after, 'when': 'recorded and present'}, move_web, 'write'),
        ('ditto', {'source': B / WEBVIEW_COPY, 'destination': web, 'when': 'recorded'},
         lambda: restore_webview(run, s['doc'], B, web), 'write'),
        ('finish', {'what': 'the old data home untouched; report', 'old_data_home': home}, finish, 'read'),
    ])


def cmd_drill(run):
    """§2.7 drill: stage, backup, install, first launch, rollback on a new root; restored trees must equal originals.

    1.0 (V1-044): --source-pebble-home stages a copy of ~/.pebble as the live home, with --source-home as the frozen old
    folder beside it when there is one. The candidate then opens ~/.pebble/data in place (state current), as 1.0 does on
    a Mac that ran 0.6.x, and the drill's rollback is the post-move one. A source home that the move already left
    behind, or that is itself a copy of ~/.pebble/data, is refused without --source-pebble-home."""
    o, layout = run.options, run.layout
    R = layout.root
    run.check('drill_root_given', R is not None, 'drill needs --root naming a new staged directory.')
    names = ('source_app', 'candidate', 'app_manifest', 'expect_backend')
    run.check('drill_inputs_given', all(o.get(name) is not None for name in names)
              and (o.get('source_home') is not None or o.get('source_pebble_home') is not None),
              'Pass --source-app, --source-home and/or --source-pebble-home, --candidate, --app-manifest and '
              '--expect-backend-sha256.')
    source_app, source_home, source_web, candidate = (o['source_app'], o.get('source_home'), o.get('source_webview'),
                                                      o['candidate'])
    source_pebble = o.get('source_pebble_home')
    s = {'stamp': run.stamp()}
    moving = data_layout(run) == 'pebble-home'
    run.check('drill_pebble_home_layout', source_pebble is None or moving,
              "--source-pebble-home drills 1.0's in-place start of ~/.pebble: pass --data-layout pebble-home too.")
    run.results.update(stamp=s['stamp'], expected_backend_sha256=o['expect_backend'],
                       data_layout='pebble-home' if moving else 'legacy',
                       live_home='pebble_home' if source_pebble is not None else 'legacy')

    def preconditions():
        run.check('drill_root_absent', not os.path.lexists(R), 'The drill root must not exist yet.', path=str(R))
        run.check('drill_root_parent', os.path.isdir(R.parent), 'The drill root parent is missing.', path=str(R.parent))
        inputs = [path for path in (source_app, source_home, source_pebble, source_web, candidate, o['app_manifest'])
                  if path]
        run.check('drill_inputs_outside_root', not any(overlaps(path, R) for path in inputs),
                  'A drill input lies inside the drill root.', inputs=inputs)
        ident = identify(source_app, 'source')
        check_identity(run, ident, 'source')
        run.check('source_backend', ident['backend_sha256'] == o['expect_backend'], 'The source bundle is not the expected '
                  'installed build.', path=str(source_app / BACKEND), sha256=ident['backend_sha256'])
        if source_home is not None:
            present_dir(run, source_home, 'source_home', required=True)
        if source_pebble is None:
            marks = moved_marks(source_home)
            run.check('drill_source_not_moved', not marks, "This folder is not a live old data home: the move into "
                      "~/.pebble already left it behind, or it is a copy of ~/.pebble/data. The live home is ~/.pebble, "
                      "so drill with --source-pebble-home <backup>/pebble-home --data-layout pebble-home (and this "
                      "folder as --source-home only when it is the old folder).", path=str(source_home), found=marks)
        else:
            present_dir(run, source_pebble, 'source_pebble_home', required=True)
            run.check('drill_source_pebble_home_live', live_pebble_home(source_pebble, source_home) and (
                source_home is None or MOVE_RECEIPT not in moved_marks(source_home)),
                '--source-pebble-home must be a copy of a live ~/.pebble (data/pebble.sqlite with the move receipt, or '
                'no database in --source-home), and --source-home the old folder beside it.', path=str(source_pebble))
            s['documents'] = named_documents_folder(source_pebble)
        if source_web:
            present_dir(run, source_web, 'source_webview', required=True)
        s['manifest'] = load_app_manifest(o['app_manifest'], o.get('app_key', 'release'))
        tree = check_candidate(run, candidate, s['manifest'])
        cid = identify(candidate, 'candidate')
        check_identity(run, cid, 'candidate')
        run.check('version_higher', version_key(cid['version']) > version_key(ident['version']),
                  'The candidate version must be higher than the source version.', candidate=cid['version'],
                  installed=ident['version'])
        run.results['candidate'] = candidate_facts(candidate, s['manifest'], tree, cid)
        sizes = {'app': tree_manifest(source_app, hashes=False)['bytes'],
                 'data_home': tree_manifest(source_home, hashes=False)['bytes'] if source_home is not None else 0,
                 'pebble_home': tree_manifest(source_pebble, hashes=False)['bytes'] if source_pebble is not None else 0,
                 'webview': tree_manifest(source_web, hashes=False)['bytes'] if source_web else 0,
                 'candidate': tree['bytes']}
        need = (3 * (sizes['app'] + sizes['data_home'] + sizes['pebble_home'] + sizes['webview'])
                + 2 * sizes['candidate'] + o['margin_bytes'])
        check_space(run, R.parent, need, sizes, 'the staged trees, their backup, the install and the restore')

    def stage():
        mkdir_new(R)
        run.report_dir, run.report_fixed = R, 'drill-report.json'
        for directory in (layout.applications, layout.user_home, layout.user_home / 'Library', layout.data_home.parent):
            mkdir_new(directory)
        copy(run, source_app, layout.app)
        if source_home is not None:
            copy(run, source_home, layout.data_home)
        if source_pebble is not None:
            copy(run, source_pebble, layout.pebble_home)
        if source_web:
            parent = layout.user_home
            for part in Path(layout.webview_rel).parts[:-1]:
                parent = parent / part
                if not os.path.lexists(parent):
                    mkdir_new(parent)
            copy(run, source_web, layout.webview)
        s['app'] = bundle_manifest(layout.app)
        s['home'] = tree_manifest(layout.data_home) if source_home is not None else None
        s['pebble'] = tree_manifest(layout.pebble_home) if source_pebble is not None else None
        s['web'] = tree_manifest(layout.webview) if source_web else None
        ok = same_bundle(bundle_manifest(source_app), s['app'])
        run.check('staged_app_matches_source', ok, 'The staged bundle differs from its source.', path=str(layout.app))
        if source_home is not None:
            run.check('staged_home_matches_source', same_tree(tree_manifest(source_home), s['home']),
                      'The staged data home differs from its source.', path=str(layout.data_home))
        if source_pebble is not None:
            run.check('staged_pebble_home_matches_source', same_tree(tree_manifest(source_pebble), s['pebble']),
                      'The staged ~/.pebble differs from its source.', path=str(layout.pebble_home))
        if source_web:
            run.check('staged_webview_matches_source', same_tree(tree_manifest(source_web), s['web']),
                      'The staged webview store differs from its source.', path=str(layout.webview))

    def sub(command, **changes):
        options = {**run.options, 'wait_seconds': 0, 'stamp': s['stamp'], 'rehearsal_report': None, 'drill_report': None,
                   'release_manifest': None, 'require_signed': False, 'release_not_required': 'drill', 'quiet_files': [],
                   **changes}
        report, _ = perform(command, layout, run.env, True, options)
        run.results.setdefault('steps', {})[command] = {
            'state': report['state'], 'ok': report['ok'], 'error': report['error'], 'record': report.get('record')}
        run.check('drill_' + command, report['ok'], f'The drill {command} step failed.', error=report['error'])
        s[command] = report

    def first_launch():
        if o.get('no_launch'):
            return 'skipped'
        work = R / 'first-launch'
        mkdir_new(work)
        mkdir_new(work / 'tmp')
        protected = [layout.applications, layout.backups, candidate]
        if moving:
            # Phase 22: the shell's arguments on the staged root, --home ~/.pebble --legacy-home <data home>, passed even
            # when there is no old folder (main.rs). A Documents folder the staged settings.json names stays read-only.
            protected += [Path(path) for path in (s.get('documents'),) if path]
            launch(run, layout.app, layout.pebble_home, work, work / 'tmp', protected, legacy=layout.data_home)
        else:
            launch(run, layout.app, layout.data_home, work, work / 'tmp', protected)
        sub('readback', mode='install', skip_processes=True)
        run.results['first_launch'] = {'move': (s['readback'].get('results') or {}).get('move')}

    def compare():
        results = s['rollback'].get('results') or {}
        app_now = bundle_manifest(layout.app)
        bundle_ok = same_bundle(s['app'], app_now) and s['app']['links'] == app_now['links']
        run.check('drill_bundle_restored', bundle_ok, 'The restored bundle differs from the staged original.',
                  path=str(layout.app), **diff_facts(bundle_ok, s['app'], app_now))
        home_now = home_ok = None
        if s['home'] is not None:
            home_now = tree_manifest(layout.data_home)
            home_ok = same_tree(s['home'], home_now)
            run.check('drill_data_restored', home_ok, 'The restored (or, after a move, untouched) old data home differs '
                      'from the staged original.', path=str(layout.data_home), **diff_facts(home_ok, s['home'], home_now))
        else:
            home_ok = not os.path.lexists(layout.data_home)
            run.check('drill_data_restored', home_ok, 'An old data home appeared where the staged Mac had none.',
                      path=str(layout.data_home))
        pebble_now = pebble_ok = None
        if s['pebble'] is not None:
            # V1-006: ~/.pebble byte for byte as staged, apart from its Documents copy index, which is moved aside.
            expected, pebble_now = without(s['pebble'], {MIRROR_INDEX}), tree_manifest(layout.pebble_home)
            pebble_ok = same_tree(expected, pebble_now)
            run.check('drill_pebble_home_restored', pebble_ok, 'The restored ~/.pebble differs from the staged original '
                      '(apart from its Documents copy index).', path=str(layout.pebble_home),
                      **diff_facts(pebble_ok, expected, pebble_now))
            indexed, aside = any(f['path'] == MIRROR_INDEX for f in s['pebble']['files']), results.get('mirror_index')
            run.check('drill_mirror_index_reset', (aside is not None and os.path.isfile(aside)) if indexed else aside is None,
                      'The restored Documents copy index was not moved aside.', path=aside)
        web_ok = None
        if s['web'] is not None:
            web_now = tree_manifest(layout.webview)
            web_ok = same_tree(s['web'], web_now)
            run.check('drill_webview_restored', web_ok, 'The restored webview store differs from the staged original.',
                      path=str(layout.webview), **diff_facts(web_ok, s['web'], web_now))
        failed = results.get('failed_bundle')
        run.check('drill_failed_bundle_kept', failed is not None and bundle_intact(Path(failed), s['manifest']),
                  'The installed candidate was not kept intact beside the backup.', path=failed)
        if s['pebble'] is not None:
            # The post-install ~/.pebble (opened in place by the candidate) sits beside the backup.
            after = results.get('after_pebble_home')
            header = sqlite_header(Path(after) / 'data' / migrations.DATABASE) if after else {}
            run.check('drill_after_home_kept', header.get('valid') is True and (
                o.get('no_launch') or header.get('user_version') == EXPECTED_EPOCH),
                'The post-install ~/.pebble was not kept beside the backup.', path=after)
        else:
            after = results.get('after_home')
            header = sqlite_header(Path(after) / migrations.DATABASE) if after else {}
            # Phase 22: after a move the old home is not upgraded (the move copied it); ~/.pebble/data is, and stays.
            run.check('drill_after_home_kept', header.get('valid') is True and (
                o.get('no_launch') or moving or header.get('user_version') == EXPECTED_EPOCH),
                'The post-install data home was not kept beside the backup.', path=after)
            if moving and not o.get('no_launch'):
                moved = sqlite_header(layout.pebble_home / 'data' / migrations.DATABASE)
                untouched = (results.get('pebble_home') or {}).get('state') == 'left untouched'
                run.check('drill_pebble_home_kept', moved.get('valid') is True
                          and moved.get('user_version') == EXPECTED_EPOCH and untouched,
                          'The moved ~/.pebble/data was not left in place by the rollback.',
                          path=str(layout.pebble_home / 'data'))
        run.results['restored'] = {'bundle': bundle_ok, 'data_home': home_ok, 'pebble_home': pebble_ok, 'webview': web_ok,
                                   'bundle_sha256': app_now['bundle_sha256'],
                                   'data_tree_sha256': home_now['tree_sha256'] if home_now else None,
                                   'pebble_tree_sha256': pebble_now['tree_sha256'] if pebble_now else None}
        run.results['passed'] = True

    run.steps([
        ('check', {'what': 'new root; source identity; candidate equals app-manifest.json', 'root': R}, preconditions,
         'read'),
        ('stage', {'what': 'ditto the source bundle, data home, ~/.pebble and webview store into the staged root',
                   'app': layout.app if R else None, 'data_home': layout.data_home if R else None,
                   'pebble_home': layout.pebble_home if R and source_pebble is not None else None}, stage, 'write'),
        ('backup', {'what': 'backup on the staged root'}, lambda: sub('backup'), 'write'),
        ('install', {'what': 'install the candidate on the staged root'}, lambda: sub('install'), 'write'),
        ('launch', {'what': 'first launch of the installed backend on the staged home, then data readback',
                    'when': 'not --no-launch'}, first_launch, 'write'),
        ('rollback', {'what': 'rollback on the staged root (bundle from the backup copy)'},
         lambda: sub('rollback', bundle_source='copy'), 'write'),
        ('compare', {'what': 'restored trees equal the staged originals byte for byte'}, compare, 'read'),
    ])


def cmd_plan(run):
    """Read-only survey of this layout and the ordered runbook commands. Changes nothing."""
    o, layout, env = run.options, run.layout, run.env
    stamp = o.get('stamp') or run.stamp()
    observed = {}
    try:
        rows = observe(env)
        observed['processes_inside_bundle'] = inside(layout.app, rows)
        observed['similar_outside_bundle'] = similar_outside(layout.app, rows)[:16]
    except Stop as stop:
        observed['processes'] = {'error': stop.code}
    try:
        ident = identify(layout.app, 'installed')
        observed['installed'] = ident
        if o.get('expect_backend'):
            observed['installed_backend_matches'] = ident['backend_sha256'] == o['expect_backend']
    except Stop as stop:
        observed['installed'] = {'error': stop.code, 'path': str(layout.app)}
    home = layout.data_home
    observed['data_home'] = {'path': str(home), 'exists': os.path.isdir(home) and not os.path.islink(home)}
    if observed['data_home']['exists']:
        observed['data_home'].update(database=sqlite_header(home / migrations.DATABASE), sidecars=sidecars(home),
                                     locks_held=lease_holders(home))
        try:
            observed['data_home']['bytes'] = tree_manifest(home, hashes=False)['bytes']
        except Stop as stop:
            observed['data_home']['bytes'] = {'error': stop.code}
    observed['webview_store'] = {'path': str(layout.webview), 'exists': os.path.isdir(layout.webview)}
    observed['backups'] = sorted(path.name for path, _ in backup_roots(layout))
    observed['free_bytes'] = env.free_bytes(existing(layout.backups))
    _, recorded, live = runbook_basis(layout, stamp)
    run.results.update(stamp=stamp, observed=observed, steps=runbook(layout, stamp, o),
                       runbook_basis={'live_home': live, 'from': 'backup.json' if recorded else 'this Mac now'})


def runbook_basis(layout, stamp):
    """(backup.json or {}, whether it is a complete record, the live home): backup.json's live_home once the backup
    exists, else what this Mac shows now. Every Mac that ran 0.6.x has ~/.pebble live (V1-044)."""
    doc = peek_backup(layout.backup_dir(stamp))
    recorded = backup_record_ok(doc, stamp)
    live = (doc.get('live_home') or 'legacy') if recorded else ('pebble_home' if pebble_live(layout) else 'legacy')
    return doc, recorded, live


def runbook(layout, stamp, o):
    base = ['.runtime/agent-env/bin/python', 'scripts/pebble_install.py']
    where = ['--root', str(layout.root)] if layout.root is not None else []
    act = ['--execute'] + ([] if layout.root is not None else ['--i-am-the-parent-with-user-approval', stamp])
    expect = o.get('expect_backend') or "<the installed backend's SHA-256: plan's observed.installed.backend_sha256>"
    candidate = str(o.get('candidate') or '<candidate>/release/Pebble.app')
    manifest = str(o.get('app_manifest') or '<candidate>/app-manifest.json')
    B = layout.backup_dir(stamp)
    drill = str(REHEARSAL_PARENT / (DRILL_PREFIX + stamp))
    moving = ['--data-layout', 'pebble-home']  # Phase 22: the candidate moves the data home into ~/.pebble/data
    doc, recorded, live = runbook_basis(layout, stamp)
    post_move = live == 'pebble_home'
    # The drill stages what the backup holds: ~/.pebble as the live home when it was live (V1-044), the old folder
    # (beside it, or as the live home), and the webview store.
    if recorded:
        try:
            old = data_copy(layout, B, doc)
        except Stop:
            old = None
        web = doc.get('webview') is not None
    else:
        old = B / DATA_COPY if os.path.isdir(layout.data_home) or not post_move else None
        web = os.path.isdir(layout.webview)
    sources = ['--source-app', str(B / (app_copy(doc) if recorded else APP_COPY))]
    sources += ['--source-pebble-home', str(B / PEBBLE_COPY)] if post_move else []
    sources += ['--source-home', str(old)] if old is not None else []
    sources += ['--source-webview', str(B / WEBVIEW_COPY)] if web else []

    def command(*args, execute=False):
        return shlex.join(base + list(args) + where + (act if execute else []))
    pair = lambda *args: [command(*args), command(*args, execute=True)]  # noqa: E731
    return [
        {'who': 'user', 'step': 'Quit Pebble from the tray (Quit Pebble) and confirm the dialog.'},
        {'who': 'parent', 'step': 'backup (dry run, then execute); it also copies ~/.pebble when it exists; record '
                                  'backup.json SHA256 in the parent review',
         'commands': pair('backup', '--stamp', stamp, '--expect-backend-sha256', expect), 'record': str(B / 'backup.json')},
        {'who': 'parent', 'step': 'rehearse the upgrade (and, from an old data home, the move into ~/.pebble) on '
                                  'temporary copies (the user stays nearby)',
         'commands': pair('rehearse', '--stamp', stamp, '--candidate', candidate, '--app-manifest', manifest, *moving)},
        {'who': 'parent', 'step': 'drill backup, install, first launch and rollback on a new staged root: '
                                  + ('~/.pebble staged as the live home and opened in place, then the post-move '
                                     'rollback' if post_move else 'the old data home staged and moved on first launch'),
         'commands': [shlex.join(base + ['drill', '--root', drill, *sources, '--candidate', candidate, '--app-manifest',
                                         manifest, '--expect-backend-sha256', expect, *moving] + extra)
                      for extra in ([], ['--execute'])]},
        {'who': 'user', 'step': 'Approve the install. Pebble must still be quit.'},
        {'who': 'parent', 'step': 'install (add --release-manifest and --require-signed when a signed manifest exists)',
         'commands': pair('install', '--stamp', stamp, '--candidate', candidate, '--app-manifest', manifest,
                          '--expect-backend-sha256', expect, '--rehearsal-report', str(B / f'rehearsal-<stamp>.json'),
                          '--drill-report', drill + '/drill-report.json')},
        {'who': 'user', 'step': 'Open the installed Pebble; answer Keychain prompts with Always Allow (the first build with '
                                'the vault helper asks once per old item it reads; later updates ask nothing), notification '
                                'and permission prompts; check chats, memories, model and effort, and permissions.'},
        {'who': 'parent', 'step': 'readback while the installed app runs: ~/.pebble (database, logs, the move record) '
                                  'and the old data home (unchanged apart from MOVED.md)',
         'commands': [command('readback', '--stamp', stamp, *moving, execute=True)]},
        {'who': 'parent', 'step': 'only on a readback failure, a startup refusal or the user\'s request: quit, roll '
                                  'back, open the restored app, read back. '
                                  + ('The rollback restores the old app, ~/.pebble from the backup (the current one is '
                                     'moved aside) and the webview store, resets the Documents copy index and leaves '
                                     'the old folder untouched.' if post_move else
                                     'The rollback restores the old app and the old data home; ~/.pebble is left '
                                     'untouched.'),
         'commands': pair('rollback', '--stamp', stamp)
         + [command('readback', '--stamp', stamp, '--mode', 'rollback', *(moving if post_move else ()), execute=True)]},
        {'who': 'parent', 'step': f'keep {B} until the user releases it in writing; list every backup with: '
                                  + shlex.join(base + ['backups'] + where)},
    ]


def _reading_parser():
    """The options of the read-only subcommands: no --execute and no approval, because they never act."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--root', type=_path_arg, help='staged root: /Applications -> ROOT/Applications, ~ -> ROOT/home')
    parser.add_argument('--webview-store', default=WEBVIEW, help='below ~/ (default %(default)s)')
    return parser


def tree_size(path):
    """(bytes, problem code or None) of a folder walked without hashing; never raises."""
    try:
        walk = tree_manifest(path, hashes=False)
    except Stop as stop:
        return None, stop.facts.get('reason') or stop.code
    return walk['bytes'], ('entries_skipped' if walk['problems'] else None)


def copy_state(path, recorded, verify, bundle=False):
    """present / intact for one backup copy: intact compares shapes (paths, sizes, modes, links) with backup.json, or,
    with verify, SHA-256 too. Never raises."""
    if not os.path.lexists(path):
        return {'path': str(path), 'present': False, 'intact': False}
    try:
        if bundle:
            if verify:
                ok = same_bundle(recorded, bundle_manifest(path))
            else:
                walk = tree_manifest(path, hashes=False)
                links = {link['path'] for link in recorded.get('links') or ()}
                files = {entry['path']: entry['bytes'] for entry in recorded['files'] if entry['path'] not in links}
                ok = (not walk['problems'] and {entry['path']: entry['bytes'] for entry in walk['files']} == files
                      and {link['path'] for link in walk['links']} == links)
        else:
            walk = tree_manifest(path, hashes=verify)
            ok = same_tree(recorded, walk) if verify else not walk['problems'] and shape(walk) == shape(recorded)
    except (Stop, KeyError, TypeError):
        ok = False
    return {'path': str(path), 'present': True, 'intact': ok}


def passed_records(B, prefix, command):
    """Passed, executed run records of one command in a backup root, oldest first. Never raises."""
    found = []
    try:
        names = sorted(os.listdir(B))
    except OSError:
        return found
    for name in names:
        if not (name.startswith(prefix + '-') and name.endswith('.json')):
            continue
        try:
            doc = strict_json(read_explicit(Path(B) / name, MAX_RECORD, name), name)
        except (EvidenceError, Stop):
            continue
        if (isinstance(doc, dict) and doc.get('format') == RUN_FORMAT and doc.get('command') == command
                and doc.get('ok') is True and doc.get('execute') is True):
            found.append((str(doc.get('finished_at')), name, doc))
    return [item[2] for item in sorted(found, key=lambda item: item[:2])]


def describe_backup(layout, path, stamp, verify):
    """One backup root as the backups listing shows it: content-free facts only (paths, versions, sizes, states)."""
    entry = {'stamp': stamp, 'root': path.name, 'path': str(path), 'naming': 'pre-install' if path.name.endswith(SUFFIX)
             else 'pre-phase21', 'private': private_root(path)}
    entry['bytes'], problem = tree_size(path)
    if problem:
        entry['size_problem'] = problem
    doc, sha = read_backup_record(path, stamp)
    entry['record'] = 'complete' if doc else 'missing_or_invalid'
    entry['launchable_bundles'] = sorted(name for name in (os.listdir(path) if os.path.isdir(path) else ())
                                         if name.endswith('.app') and os.path.isdir(path / name))
    entry['indexed_by_spotlight'] = not path.name.endswith('.noindex')
    entry['holds_signing_password'] = os.path.lexists(path / PEBBLE_COPY / 'signing' / 'keychain-password')
    if doc is None:
        entry.update(live_home=None, versions=None, intact=False,
                     rollback={'capable': False, 'reason': 'backup.json is missing or not a complete record'})
        return entry
    installs = passed_records(path, 'install', 'install')
    rollbacks = passed_records(path, 'rollback', 'rollback')
    after = ((installs[-1].get('results') or {}).get('candidate') or {}) if installs else {}
    entry.update(backup_json_sha256=sha, created_at=doc.get('created_at'), live_home=doc.get('live_home') or 'legacy',
                 versions={'backed_up': (doc.get('installed') or {}).get('version'),
                           'installed_after': after.get('version') if isinstance(after, dict) else None},
                 rolled_back_at=rollbacks[-1].get('finished_at') if rollbacks else None)
    copies = {'app': copy_state(path / app_copy(doc), doc['app'], verify, bundle=True)}
    if doc.get('pebble_home'):
        copies['pebble_home'] = copy_state(path / PEBBLE_COPY, doc['pebble_home'], verify)
    if doc.get('data_home'):
        try:
            source = data_copy(layout, path, doc)
        except Stop:
            source = path / DATA_COPY
        copies['data_home'] = copy_state(source, doc['data_home'], verify)
        reused = doc['data_home'].get('reused')
        copies['data_home']['reused_from'] = reused.get('root') if isinstance(reused, dict) else None
    if doc.get('webview'):
        copies['webview'] = copy_state(path / WEBVIEW_COPY, doc['webview'], verify)
    entry['copies'] = copies
    entry['intact'] = all(item['intact'] for item in copies.values())
    entry['intact_check'] = 'sha256' if verify else 'shape (paths, sizes, modes; --verify re-hashes)'
    # What a rollback of this stamp restores, and whether those copies are here and intact.
    needs = ['app', 'pebble_home'] if entry['live_home'] == 'pebble_home' else ['app', 'data_home']
    needs += ['webview'] if doc.get('webview') else []
    usable = [name for name in needs if name in copies and copies[name]['intact']
              and not (name == 'data_home' and copies[name].get('reused_from'))]
    missing = sorted(set(needs) - set(usable))
    entry['rollback'] = {'capable': not missing, 'restores': needs, 'missing_or_changed': missing,
                         'command': rerun_command(layout, 'rollback', stamp)}
    return entry


def cmd_backups(run):
    """Read-only (V1-137): every backup root under the backups folder, newest first, with its stamp, versions, live
    home, size, whether its copies are intact and whether it can still roll back. Changes nothing; the person releases
    a backup by moving its folder to the Trash in Finder."""
    run.check('read_only_command', not run.execute, 'backups only lists; it has no --execute.')
    layout = run.layout
    roots = backup_roots(layout)
    try:
        others = sorted(name for name in os.listdir(layout.backups) if not BACKUP_NAME.fullmatch(name))
    except OSError:
        others = []
    entries = [describe_backup(layout, path, stamp, bool(run.options.get('verify'))) for path, stamp in roots]
    for entry in entries:
        entry['old_folder_copy_used_by'] = sorted(
            other['root'] for other in entries
            if ((other.get('copies') or {}).get('data_home') or {}).get('reused_from') == entry['root'])
    run.results.update(backups_folder=str(layout.backups), backups=entries, other_entries=others,
                       total_bytes=sum(entry['bytes'] or 0 for entry in entries),
                       note='This tool never deletes. Keep the newest backup that can roll back the installed version '
                            'until you are sure of it, and keep a backup whose old-folder copy another backup uses '
                            'while you may still rehearse or drill that one. Release a backup by moving its folder to '
                            'the Trash in Finder; it holds a copy of your data (and, when listed, the signing '
                            "keychain's password).")


# Everything a full removal touches (V1-061). The native host manifest folders mirror backend/pebble/browser_bridge.py
# BROWSER_FOLDERS and HOST_NAME (a test keeps them equal; D113, Phase 43: every browser Connect a browser can write).
HOST_NAME, HOST_FOLDERS = 'com.pebble.browser', ('Google/Chrome', 'Google/Chrome Beta', 'Google/Chrome Canary', 'Chromium',
                                                 'Comet', 'BraveSoftware/Brave-Browser', 'Microsoft Edge', 'Arc/User Data')
LIBRARY_ITEMS = (('Caches/' + IDENTIFIER, 'caches'), ('HTTPStorages/' + IDENTIFIER, 'web storage'),
                 ('HTTPStorages/' + IDENTIFIER + '.binarycookies', 'web cookies'),
                 ('Saved Application State/' + IDENTIFIER + '.savedState', 'window state'),
                 ('Preferences/' + IDENTIFIER + '.plist', 'macOS preferences'))
KEYCHAIN_ITEMS = (('com.pebble.local.vault', 'the vault item (account v1): every provider secret'),
                  ('com.pebble.local.providers', 'kept per-item provider secrets from before the vault'),
                  ('com.pebble.local.connectors', 'kept per-item connector credentials'),
                  ('com.pebble.local.plan-credentials', 'kept per-item plan credentials'),
                  ('com.pebble.local.browser-pairing', "Pebble for Chrome's pairing key"))


def cmd_purge(run):
    """Read-only (V1-061): everything a full removal of Pebble touches, with sizes. It never deletes, never moves and
    never reads a Keychain item; it prints the list and the order to remove things by hand."""
    run.check('read_only_command', not run.execute, 'purge only lists; remove the listed items yourself.')
    layout = run.layout
    home = layout.user_home
    items = []

    def add(kind, path, what, how):
        path = Path(path)
        exists = os.path.lexists(path)
        item = {'kind': kind, 'path': str(path), 'what': what, 'how': how, 'exists': exists,
                'link': os.path.islink(path)}
        if exists and not item['link'] and os.path.isdir(path):
            item['bytes'], problem = tree_size(path)
            if problem:
                item['size_problem'] = problem
        elif exists and not item['link']:
            item['bytes'] = os.lstat(path).st_size
        items.append(item)

    add('app', layout.app, 'the app', 'Move it to the Trash (Pebble must be quit).')
    add('folder', layout.pebble_home, "Pebble's folder: settings, the live database and its upgrade backups, logs, the "
        "browser profile and, when created, the signing identity's keychain password", 'Move it to the Trash after '
        'removing the signing identity (below).')
    add('folder', layout.data_home, 'the folder of Pebble 0.5 and earlier (frozen after the move into ~/.pebble)',
        'Move it to the Trash.')
    add('folder', layout.webview, "the app window's web storage (pet and quiz drafts)", 'Move it to the Trash.')
    add('folder', home / 'Documents' / 'Pebble', 'readable copies of conversations, memories, files and logs',
        'Move it to the Trash, or keep it: it is yours.')
    named = named_documents_folder(layout.pebble_home)
    if named and Path(named) != home / 'Documents' / 'Pebble':
        add('folder', named, 'readable copies, in the folder settings.json names', 'Move it to the Trash, or keep it.')
    add('folder', layout.backups, 'install backups (each holds a copy of your data)',
        'Release them in Finder; list them first with: pebble_install.py backups.')
    for rel, what in LIBRARY_ITEMS:
        add('library', home / 'Library' / rel, what, 'Move it to the Trash.')
    for folder in HOST_FOLDERS:
        add('browser', home / 'Library/Application Support' / folder / 'NativeMessagingHosts' / (HOST_NAME + '.json'),
            "Pebble for Chrome's native host manifest", 'Run scripts/install_native_host.py uninstall.')
    add('keychain_file', home / 'Library/Keychains/pebble-signing.keychain-db', "Pebble's own code-signing keychain",
        'Run scripts/pebble_signing.py remove (it also removes the certificate trust setting).')
    parent = layout.root if layout.root is not None else REHEARSAL_PARENT
    try:
        scratch = sorted(name for name in os.listdir(parent) if name.startswith(SCRATCH_PREFIXES))
    except OSError:
        scratch = []
    for name in scratch:
        add('scratch', Path(parent) / name, 'a rehearsal or drill folder (copies of your data)', 'Move it to the Trash.')
    manual = [{'kind': 'keychain_item', 'service': service, 'what': what, 'read': False,
               'how': 'Keychain Access: search for "com.pebble.local" and delete the item. This tool never queries '
                      'the Keychain.'} for service, what in KEYCHAIN_ITEMS]
    manual += [{'kind': 'system_setting', 'what': what, 'how': how} for what, how in (
        ('login item', 'System Settings > General > Login Items: remove Pebble.'),
        ('notifications', 'System Settings > Notifications: Pebble disappears once the app is gone.'),
        ('privacy grants', 'System Settings > Privacy & Security > Accessibility, Screen Recording, Files and Folders: '
                           'remove Pebble.'),
        ('Chrome extension', 'chrome://extensions: remove Pebble for Chrome.'),
        ('other app copies', 'Spotlight may list build copies of com.pebble.local; move any you made to the Trash.'))]
    run.results.update(deletes='nothing: this command only lists', items=items, manual=manual,
                       total_bytes=sum(item.get('bytes') or 0 for item in items if item['exists']),
                       order=['Quit Pebble from the tray.', 'Run scripts/install_native_host.py uninstall.',
                              'Run scripts/pebble_signing.py remove, if the signing identity was created.',
                              'Move the app to the Trash.', 'Move the listed folders and files to the Trash.',
                              'Delete the Keychain items by hand.', 'Remove Pebble in the System Settings listed.'])


COMMANDS = {'plan': cmd_plan, 'backup': cmd_backup, 'rehearse': cmd_rehearse, 'install': cmd_install,
            'readback': cmd_readback, 'rollback': cmd_rollback, 'drill': cmd_drill, 'backups': cmd_backups,
            'purge': cmd_purge}
READ_ONLY = frozenset({'plan', 'backups', 'purge'})


def quiet_state(path, window, now):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {'path': str(path), 'state': 'absent', 'quiet': True}
    age = now - info.st_mtime
    return {'path': str(path), 'state': 'present', 'age_seconds': round(age, 1), 'quiet': age >= window}


def staged_confinement(layout):
    """X20 F20-04: (None, None) when a staged --root is a staging area, else (reason, path). No link may lie between
    the root and any layout path, each existing layout path must resolve inside the root, and its Applications folder
    must not be a real install location. Real paths (no --root) are judged by the approval stamp instead."""
    root = Path(layout.root)
    if os.path.islink(root):
        return 'staged_root_is_link', str(root)
    base = os.path.realpath(root)
    for path in (layout.applications, layout.app, layout.user_home, layout.data_home, layout.webview, layout.backups,
                 layout.pebble_home):
        probe = root
        for part in Path(path).relative_to(root).parts:
            probe = probe / part
            if os.path.islink(probe):
                return 'staged_path_is_link', str(probe)
        found = existing(path)
        if found != root and root not in found.parents:
            continue  # nothing of it exists under the root yet (a new drill root)
        if os.path.commonpath([base, os.path.realpath(found)]) != base:
            return 'staged_path_outside_root', str(path)
    installs = {os.path.realpath(p) for p in ('/Applications', os.path.expanduser('~/Applications'))}
    if os.path.realpath(existing(layout.applications)) in installs:
        return 'staged_applications_installed', str(layout.applications)
    return None, None


def gate(run):
    """Real paths need the parent's approval naming the backup stamp; named live markers must be quiet."""
    o = run.options
    if run.layout.root is not None:
        # X20 F20-04: a staged root is exempt from the approval stamp only while it cannot reach real folders.
        reason, path = staged_confinement(run.layout)
        run.check('staged_root_confined', reason is None, 'A staged --root must be a plain folder whose Applications, '
                  'home and backups stay inside it without links; nothing was changed.', reason=reason, path=path)
    if run.execute and run.layout.root is None:
        stamp = o.get('stamp')
        run.check('stamp_given', stamp is not None, 'Real paths need an explicit --stamp.')
        run.check('parent_approval', o.get('approval') == stamp, 'Real paths need --i-am-the-parent-with-user-approval '
                  'naming this backup stamp, given only after the user approved.', expected_stamp=stamp,
                  given=o.get('approval'))
    if o.get('quiet_files'):
        now = run.env.clock()
        states = [quiet_state(Path(path), o.get('quiet_seconds', 300), now) for path in o['quiet_files']]
        run.check('quiet_markers', all(state['quiet'] for state in states),
                  'A live trial or worker marker changed recently; wait until it is quiet.', markers=states)
    elif run.execute and run.layout.root is None:
        run.results['warning'] = 'No --quiet-file was named: confirm that no live trial or worker is running.'


def record(run, error):
    state = ('refused' if not run.changed else 'stopped') if error else ('done' if run.execute else 'dry_run')
    return {'format': RUN_FORMAT, 'tool': 'scripts/pebble_install.py', 'tool_sha256': TOOL_SHA, 'command': run.command,
            'execute': run.execute, 'state': state, 'ok': error is None, 'layout': run.layout.describe(),
            'started_at': utc(run.started).isoformat(), 'finished_at': utc(run.env.clock()).isoformat(),
            'checks': run.checks, 'actions': run.actions, 'results': plain(run.results), 'recovery': run.recovery,
            'error': None if error is None else {'code': error.code, 'message': error.message, **error.facts}}


def perform(command, layout, env, execute, options):
    """One subcommand. Returns its report and exit code; executed runs keep their report as a new file."""
    run = Run(command, layout, env, execute, options)
    error = None
    try:
        if command not in READ_ONLY:
            gate(run)
        if layout.root is not None and command != 'drill':
            run.check('root_present', os.path.isdir(layout.root), 'The staged --root does not exist.', path=str(layout.root))
        COMMANDS[command](run)
    except Stop as stop:
        error = stop
    except Exception as exc:  # reported; the state still says whether anything changed
        error = Stop('unexpected_error', 'An unexpected error stopped this run.', reason=type(exc).__name__,
                     detail=str(exc)[:400])
    report = record(run, error)
    if execute and run.report_dir is not None and (error is None or run.changed or run.always_record):
        try:
            if run.report_fixed:
                path = Path(run.report_dir) / run.report_fixed
                sha = write_new(path, report)
            else:
                for index in range(100):
                    path = Path(run.report_dir) / (f'{run.report_prefix}-{run.stamp()}'
                                                   + (f'-{index}' if index else '') + '.json')
                    try:
                        sha = write_new(path, report)
                        break
                    except FileExistsError:
                        continue
                else:
                    raise FileExistsError(errno.EEXIST, 'no free report name', str(run.report_dir))
            report['record'] = {'path': str(path), 'sha256': sha}
        except OSError as exc:
            report['record'] = {'error': type(exc).__name__}
    code = EXIT_OK if error is None else EXIT_REFUSED if not run.changed else EXIT_STOPPED
    return report, code


# ---- CLI ----------------------------------------------------------------------------------------


def _stamp_arg(value):
    if not STAMP.fullmatch(value):
        raise argparse.ArgumentTypeError('use a UTC stamp such as 20261013T120000Z')
    return value


def _hex_arg(value):
    if not HEX64.fullmatch(value):
        raise argparse.ArgumentTypeError('use 64 lowercase hex digits')
    return value


def _path_arg(value):
    if not value or CONTROL.search(value):
        raise argparse.ArgumentTypeError('use a path without control characters')
    return Path(os.path.abspath(value))


def _bounded(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f'use {low}..{high}')
        return number
    return parse


def _row_arg(value):
    parts = value.rsplit(':', 2)
    if len(parts) != 3 or not LABEL.fullmatch(parts[0]) or not all(part.isdigit() for part in parts[1:]):
        raise argparse.ArgumentTypeError('use TABLE:BEFORE:AFTER with exact counts, such as memories:13:12')
    return parts[0], int(parts[1]), int(parts[2])


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--root', type=_path_arg, help='staged root: /Applications -> ROOT/Applications, ~ -> ROOT/home')
    common.add_argument('--execute', action='store_true', help='act; without it nothing changes')
    common.add_argument('--i-am-the-parent-with-user-approval', dest='approval', metavar='STAMP', type=_stamp_arg,
                        help='required with --execute on real paths; must name the backup stamp')
    common.add_argument('--quiet-file', dest='quiet_files', action='append', default=[], type=_path_arg,
                        help='a live trial or worker marker that must be absent or unmodified for --quiet-seconds')
    common.add_argument('--quiet-seconds', type=_bounded(1, 86400), default=300)
    common.add_argument('--webview-store', default=WEBVIEW, help='below ~/ (default %(default)s)')
    common.add_argument('--wait-seconds', type=_bounded(0, 900), default=30,
                        help='how long to wait for Pebble to be quit (never signalled)')
    common.add_argument('--margin-bytes', type=_bounded(0, 1 << 44), default=1 << 30)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    def add(name, text):
        return sub.add_parser(name, parents=[common], help=text, description=text)

    def candidate_args(command, required=True):
        command.add_argument('--candidate', type=_path_arg, required=required, help='the candidate Pebble.app')
        command.add_argument('--app-manifest', type=_path_arg, required=required, help="T1's app-manifest.json")
        command.add_argument('--app-key', default='release', help='app object key in app-manifest.json')

    plan = add('plan', 'Read-only survey and the ordered runbook commands.')
    plan.add_argument('--stamp', type=_stamp_arg)
    plan.add_argument('--expect-backend-sha256', dest='expect_backend', type=_hex_arg)
    candidate_args(plan, required=False)
    backup = add('backup', 'D20 2.4: back up the bundle, the data home and the webview store.')
    backup.add_argument('--stamp', type=_stamp_arg)
    backup.add_argument('--expect-backend-sha256', dest='expect_backend', type=_hex_arg, required=True)
    rehearse = add('rehearse', 'D20 2.3: upgrade a copy of the backed-up home with the candidate backend.')
    rehearse.add_argument('--stamp', type=_stamp_arg, required=True)
    candidate_args(rehearse)
    rehearse.add_argument('--rehearsal-dir', type=_path_arg)
    rehearse.add_argument('--launch-timeout', type=_bounded(5, 1800), default=180)
    rehearse.add_argument('--accept-row-change', dest='accept_rows', action='append', default=[], type=_row_arg)
    rehearse.add_argument('--allow-keychain', action='store_true',
                          help='do not deny Keychain service lookups. From Phase 22 the release candidate copies the provider '
                               'secrets it reads into its vault item (the old items are kept, so the installed build is '
                               'unaffected).')
    layout_help = ('legacy: the candidate uses the data home in place (before Phase 22); pebble-home: the candidate '
                   'moves it into ~/.pebble/data, as a Phase 22 shell does (default %(default)s)')
    rehearse.add_argument('--data-layout', choices=DATA_LAYOUTS, default='legacy', help=layout_help)
    install = add('install', 'D20 2.5 and 6.7: replace the installed bundle with the candidate.')
    install.add_argument('--stamp', type=_stamp_arg, required=True)
    candidate_args(install)
    install.add_argument('--expect-backend-sha256', dest='expect_backend', type=_hex_arg, required=True)
    install.add_argument('--release-manifest', type=_path_arg)
    install.add_argument('--require-signed', action='store_true')
    install.add_argument('--rehearsal-report', type=_path_arg)
    install.add_argument('--drill-report', type=_path_arg)
    readback = add('readback', 'D20 2.6 (or, with --mode rollback, the check after 2.7).')
    readback.add_argument('--stamp', type=_stamp_arg, required=True)
    readback.add_argument('--mode', choices=('install', 'rollback'), default='install')
    readback.add_argument('--expect-backend-sha256', dest='expect_backend', type=_hex_arg)
    readback.add_argument('--app-manifest', type=_path_arg, help='default: the one the install record names')
    readback.add_argument('--accept-row-change', dest='accept_rows', action='append', default=[], type=_row_arg)
    readback.add_argument('--data-layout', choices=DATA_LAYOUTS, default='legacy', help=layout_help)
    rollback = add('rollback', 'D20 2.7: restore the data home and the bundle from the backup.')
    rollback.add_argument('--stamp', type=_stamp_arg, required=True)
    rollback.add_argument('--bundle-source', choices=('copy', 'moved'), default='copy')
    rollback.add_argument('--expect-backend-sha256', dest='expect_backend', type=_hex_arg)
    drill = add('drill', 'D20 2.7 drill: backup, install and rollback end to end on a new staged root.')
    drill.add_argument('--source-app', type=_path_arg, required=True)
    drill.add_argument('--source-home', type=_path_arg,
                       help='the old data home to stage (with --source-pebble-home: the frozen old folder beside it)')
    drill.add_argument('--source-pebble-home', type=_path_arg,
                       help='1.0: a copy of ~/.pebble (B/pebble-home) to stage as the live home; needs --data-layout '
                            'pebble-home')
    drill.add_argument('--source-webview', type=_path_arg)
    candidate_args(drill)
    drill.add_argument('--expect-backend-sha256', dest='expect_backend', type=_hex_arg, required=True)
    drill.add_argument('--no-launch', action='store_true', help='skip the first launch of the installed backend')
    drill.add_argument('--launch-timeout', type=_bounded(5, 1800), default=180)
    drill.add_argument('--accept-row-change', dest='accept_rows', action='append', default=[], type=_row_arg)
    drill.add_argument('--allow-keychain', action='store_true',
                       help='do not deny Keychain service lookups. From Phase 22 the release candidate copies the provider '
                            'secrets it reads into its vault item (the old items are kept, so the installed build is '
                            'unaffected).')
    drill.add_argument('--data-layout', choices=DATA_LAYOUTS, default='legacy', help=layout_help)
    reading = _reading_parser()
    backups = sub.add_parser('backups', parents=[reading], help='Read-only: list every backup root and what it can '
                             'still do.', description='Read-only: list every backup root and what it can still do.')
    backups.add_argument('--verify', action='store_true', help='re-hash every copy (slow) instead of comparing shapes')
    purge_text = 'Read-only: list everything a full removal of Pebble touches. It never deletes anything.'
    sub.add_parser('purge', parents=[reading], help=purge_text, description=purge_text)
    return parser


OPTION_DEFAULTS = {
    'stamp': None, 'approval': None, 'quiet_files': [], 'quiet_seconds': 300, 'wait_seconds': 30,
    'margin_bytes': 1 << 30, 'expect_backend': None, 'candidate': None, 'app_manifest': None, 'app_key': 'release',
    'rehearsal_dir': None, 'launch_timeout': 180, 'accept_rows': [], 'allow_keychain': False,
    'release_manifest': None, 'require_signed': False, 'rehearsal_report': None, 'drill_report': None,
    'mode': 'install', 'bundle_source': 'copy', 'source_app': None, 'source_home': None, 'source_webview': None,
    'no_launch': False, 'skip_processes': False, 'data_layout': 'legacy', 'source_pebble_home': None, 'verify': False,
}


def main(argv=None, env=None, out=None):
    out = out or sys.stdout
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else EXIT_USAGE
    if args.command == 'drill' and args.root is None:
        out.write(json.dumps({'ok': False, 'error': {'code': 'drill_root_given',
                                                     'message': 'drill needs --root naming a new staged directory.'}}) + '\n')
        return EXIT_USAGE
    try:
        layout = Layout.make(args.root, args.webview_store)
    except ValueError as error:
        out.write(json.dumps({'ok': False, 'error': {'code': 'usage', 'message': str(error)}}) + '\n')
        return EXIT_USAGE
    options = {key: getattr(args, key, default) for key, default in OPTION_DEFAULTS.items()}
    options['skip_processes'] = False  # internal to the drill; no flag reaches it
    if env is None:
        env = Env(verifier=default_release_verifier())
    report, code = perform(args.command, layout, env, getattr(args, 'execute', False), options)
    out.write(json.dumps(printable(report), indent=2, sort_keys=True, ensure_ascii=True) + '\n')
    return code


if __name__ == '__main__':
    sys.exit(main())
