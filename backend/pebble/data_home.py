"""Where Pebble keeps its data (Phase 22, s22-storage), and the one-time move into it.

Layout of a structured home (the shell passes ``--home ~/.pebble``):

    ~/.pebble/              internal and important, mode 0700 (like ~/.claude)
        README.md           what each item is and what is safe to delete (regenerated)
        settings.json       the person's settings (settings_file.py), with settings.schema.json
        PEBBLE.md           standing preferences, read like Settings -> Memory -> Your preferences
        mcp.json            a read-only view of the connectors (regenerated; edits are never applied)
        skills/             a place to keep skill folders for Settings -> Extensions -> Import
        data/               the live database and everything it refers to (never delete)
        backups/  runtime/  cache/  logs/
    ~/Documents/Pebble/     readable copies for the person (data_mirror.py)

Resolution: ``--home``, then ``PEBBLE_HOME``, then ``~/.pebble``. A ``--home`` that is not ``~/.pebble``,
carries no layout marker and came without ``--legacy-home`` is a flat home exactly as before Phase 22:
the database sits directly in it and nothing here touches it (tests and validators keep working).

The move from the old home (``~/Library/Application Support/com.pebble.local``) runs once, before App,
under the old home's service lease and its upgrade-owner lock. It copies into a staging folder inside
``~/.pebble``, verifies the copy (PRAGMA integrity_check, row counts and a content digest per table, and
a SHA-256 per copied file), renames the staging folder to ``data/`` (the switch) and then writes
``MOVED.md`` and ``pebble-moved.json`` in the old folder. The old folder is never deleted. A refusal or
a failure leaves the service on the old home and is reported on the Data & storage page; an interrupted
move leaves only a staging folder, which the next launch removes before it tries again.
"""

import errno
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import stat
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FORMAT = 'pebble-home/1'
MARKER = '.pebble-home.json'
DEFAULT_NAME = '.pebble'
ENV = 'PEBBLE_HOME'
LEGACY_IDENTIFIER = 'com.pebble.local'
DATA, BACKUPS, RUNTIME, CACHE, LOGS, SKILLS = 'data', 'backups', 'runtime', 'cache', 'logs', 'skills'
FOLDERS = (BACKUPS, RUNTIME, CACHE, LOGS, SKILLS)
README, SETTINGS, SCHEMA, INSTRUCTIONS, MCP = 'README.md', 'settings.json', 'settings.schema.json', 'PEBBLE.md', 'mcp.json'
DATABASE = 'pebble.sqlite'
SIDECARS = ('-wal', '-shm', '-journal')
MIGRATED = '.pebble-moved-here.json'
STAGING, STAGING_MARKER = '.data-moving-', '.pebble-staging.json'
MOVED, POINTER = 'MOVED.md', 'pebble-moved.json'
MOVE_FORMAT, POINTER_FORMAT, STATUS_FORMAT = 'pebble-home-move/1', 'pebble-home-pointer/1', 'pebble-home-status/1'
STATUS = 'migration.json'  # in runtime/
LOCKS = ('runtime-owner.lock', 'upgrade-owner.lock')
# Left in the old folder: service locks and SQLite sidecars (the database is copied through SQLite's online
# backup, which folds committed WAL frames in), logs of the old service, the pointer files, and folders whose
# records name absolute paths in the old home (Git worktrees and dependency environments) or scratch.
LEFT_FILES = frozenset({'service.log', 'backend.log', MOVED, POINTER})
LEFT_DIRS = frozenset({'git-worktrees', 'environments', 'containment-self-test'})
LIVE_WORKTREES = "SELECT count(*) FROM git_worktrees WHERE state IN ('creating','active','removing','uncertain')"
LIVE_ENVIRONMENTS = "SELECT count(*) FROM project_environments WHERE state IN ('installing','ready','uncertain')"
FREE_RESERVE = 256 << 20
CHUNK = 1 << 20
_NOFOLLOW, _CLOEXEC = getattr(os, 'O_NOFOLLOW', 0), getattr(os, 'O_CLOEXEC', 0)

REASONS = {
    'legacy_in_use': 'Another Pebble service is using the old folder.',
    'legacy_unsafe': 'The old folder is a link, shared with other users or not yours.',
    'legacy_upgrade_pending': 'An interrupted upgrade must finish in the old folder first.',
    'legacy_refused': 'The old database did not pass its checks.',
    'legacy_unsupported': 'This system cannot check the old database safely.',
    'anchored_records': 'Managed Git worktrees or dependency environments live in the old folder. Remove them '
                        '(Settings -> Coding), then restart Pebble.',
    'not_enough_space': 'There is not enough free space for a verified copy.',
    'both_homes_have_data': 'Both the old folder and ~/.pebble/data hold a database, so Pebble kept using the old one.',
    'copy_failed': 'A file could not be copied.',
    'verify_failed': 'The copy did not match the original, so it was not used.',
    'legacy_changed_during_copy': 'The old folder changed while it was being copied.',
    'data_folder_not_empty': '~/.pebble/data already holds files.',
    'switch_failed': 'The checked copy could not be put in place.',
    'unexpected_error': 'The move stopped unexpectedly.',
    'home_unusable': '~/.pebble is a link, a file or not yours, so Pebble kept using its old folder.',
}


class DataHomeError(OSError):
    """A path Pebble will not use (a link, another user's folder, a non-directory)."""


class _Refused(Exception):
    def __init__(self, reason, **facts):
        super().__init__(reason)
        self.reason, self.facts = reason, facts


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='seconds')


# ---- Layout ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    root: Path
    structured: bool
    source: str                 # '--home', 'PEBBLE_HOME', 'default' or 'discovered'
    legacy: object = None       # the old home (a Path) a structured home may move from, else None

    @property
    def data(self):
        return self.root / DATA if self.structured else self.root

    @property
    def is_default(self):
        return same_path(self.root, Path.home() / DEFAULT_NAME)

    def describe(self):
        return {'root': str(self.root), 'data': str(self.data), 'structured': self.structured, 'source': self.source,
                'legacy': str(self.legacy) if self.legacy else None, 'default': self.is_default}


def same_path(a, b):
    a, b = os.path.abspath(a), os.path.abspath(b)
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def marked(root):
    """Whether `root` is a structured home: its marker is a regular file (never followed through a link)."""
    try:
        return stat.S_ISREG(os.lstat(Path(root) / MARKER).st_mode)
    except OSError:
        return False


def legacy_default(user_home=None):
    """The shell's previous app_data_dir() on macOS (main.rs before Phase 22)."""
    return Path(user_home or Path.home()) / 'Library' / 'Application Support' / LEGACY_IDENTIFIER


def resolve(home=None, legacy=None, *, env=None, user_home=None):
    """--home, then PEBBLE_HOME, then ~/.pebble. Only --home may name a flat (pre-Phase 22) home."""
    env = os.environ if env is None else env
    user_home = Path(user_home) if user_home is not None else Path.home()
    default = user_home / DEFAULT_NAME
    if home:
        root, source = Path(home), '--home'
    elif env.get(ENV):
        root, source = Path(env[ENV]), ENV
    else:
        root, source = default, 'default'
    root = Path(os.path.abspath(os.path.expanduser(str(root))))
    old = Path(os.path.abspath(os.path.expanduser(str(legacy)))) if legacy else None
    if old is None and source == 'default' and sys.platform == 'darwin':
        old = legacy_default(user_home)
    structured = source != '--home' or legacy is not None or same_path(root, default) or marked(root)
    if structured and not marked(root) and os.path.lexists(root / DATABASE):
        structured = False  # an existing flat home stays flat: it is never reinterpreted
    return Layout(root, structured, source, old if structured else None)


# The layout main.py resolved for this process, keyed by the data folder App receives. Store and the storage
# services read it; a test App on a flat --home finds nothing and stays exactly as before.
_registry_lock = threading.Lock()
_registry = {}


def _key(path):
    return os.path.normcase(os.path.abspath(path))


def activate(layout, data, report=None):
    global _generation
    with _registry_lock:
        _registry[_key(data)] = (layout, dict(report or {}))
        _generation += 1  # the protected folders changed: resolve them again


def deactivate(data=None):
    global _generation
    with _registry_lock:
        if data is None:
            _registry.clear()
            _mirrors.clear()
        else:
            _registry.pop(_key(data), None)
            _mirrors.pop(_key(data), None)
        _generation += 1


# Phase 24 (V1-003): the folders of the readable copies (~/Documents/Pebble or the one settings.json names). Pebble's
# own file, terminal and connector paths refuse them like ~/.pebble: a tool could otherwise read every conversation and
# memory copy, or delete one, which the storage service reads as the person's deletion (a trash or a forget). The
# storage service registers every root it writes and every root it wrote before (retired); the default root is always
# protected. The person's own choices are unaffected: inside_protected() (the storage folder settings.json may name,
# export destinations) still means Pebble's internal folders only.
_mirrors = {}  # data folder key ('' for none) -> absolute copies roots


def default_mirror(user_home=None):
    return Path(user_home or Path.home()) / 'Documents' / 'Pebble'


def protect_mirror(root, owner=None):
    """Register a Documents copies root (current or retired) as a protected root; True when it was not yet registered.
    `owner` is the data folder whose storage service wrote it, so deactivate(data) drops its roots too."""
    global _generation
    path = Path(os.path.abspath(os.path.expanduser(str(root))))
    key = _key(owner) if owner is not None else ''
    with _registry_lock:
        roots = _mirrors.setdefault(key, set())
        if path in roots:
            return False
        roots.add(path)
        _generation += 1  # the protected folders changed: resolve them again
    return True


def mirror_roots():
    """Every copies root Pebble's tools must refuse: the default ~/Documents/Pebble and each registered root."""
    roots = [default_mirror()]
    with _registry_lock:
        for paths in _mirrors.values():
            roots += sorted(paths)
    return _unique(roots)


def _unique(paths, seen=None):
    seen, out = set() if seen is None else seen, []
    for path in paths:
        key = _key(path)
        if key not in seen:
            seen.add(key)
            out.append(Path(os.path.abspath(path)))
    return out


def for_data(data):
    """(Layout, migration report) for the folder App runs on, or (None, None) for a flat home."""
    with _registry_lock:
        found = _registry.get(_key(data))
    if found:
        return found
    path = Path(os.path.abspath(data))
    if path.name == DATA and marked(path.parent):
        layout = Layout(path.parent, True, 'discovered', None)
        return layout, read_status(layout)
    return None, None


def protected_roots(mirrors=True):
    """Folders Pebble's own file-write, terminal and connector paths must refuse: ~/.pebble, PEBBLE_HOME, the old
    app-data folder and every home this process activated, then (unless `mirrors` is False) every Documents copies
    root (mirror_roots())."""
    roots = [Path.home() / DEFAULT_NAME, legacy_default()]
    if os.environ.get(ENV):
        roots.append(Path(os.path.expanduser(os.environ[ENV])))
    with _registry_lock:
        for layout, _ in _registry.values():
            roots += [layout.root, layout.data] + ([layout.legacy] if layout.legacy else [])
    seen = set()
    out = _unique(roots, seen)
    return out + _unique(mirror_roots(), seen) if mirrors else out


def inside_protected(path, resolved=False):
    """True when `path` (resolved, links followed) is one of Pebble's internal folders (protected_roots(mirrors=False))
    or lies inside one: what settings.json's storage folder and an export destination may not be. The Documents copies
    roots are refused to tools (tool_refused, protected_below) but stay the person's to choose. Compared
    case-insensitively, as APFS is by default. `resolved`: the caller already resolved `path` (a Workspace root), so
    it is not resolved again."""
    try:
        target = _target(path, resolved)
    except (OSError, ValueError):
        return True
    return any(_within(target, base) for base in _resolved(internal=True)[0])


# Merge (i22): inside the protected roots, the folders Pebble's own reviewed features hand to tools as project trees:
# each data folder's managed Git worktrees and dependency environments (git_workflows, child_worktrees and
# environments root their Workspaces there), and ~/.pebble/skills, the person's own skill copies that Settings ->
# Extensions -> Import reads through a Workspace. Only folders strictly inside them qualify; the rest stays refused.
# 'guard-projects': guard.Guard mirrors each project's broker-approved files there and stages every Write/Edit/Append
# into that copy through policy.Workspace (guard._stage); without it every edit fails once the data folder is
# ~/.pebble/data (v22-validator r2, s5).
WORK_AREAS = ('git-worktrees', 'environments', 'guard-projects')


def _folded(path):
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path)))).casefold()


def _within(target, base):
    return target == base or target.startswith(base.rstrip(os.sep) + os.sep)


def work_areas():
    datas, roots = [Path.home() / DEFAULT_NAME / DATA, legacy_default()], [Path.home() / DEFAULT_NAME]
    if os.environ.get(ENV):
        roots.append(Path(os.path.expanduser(os.environ[ENV])))
        datas.append(roots[-1] / DATA)
    with _registry_lock:
        for key, (layout, _) in _registry.items():
            datas.append(Path(key))
            roots.append(layout.root)
    return [data / name for data in datas for name in WORK_AREAS] + [root / SKILLS for root in roots]


def tool_refused(path, resolved=False):
    """Whether Pebble's own file, terminal and connector paths must refuse `path` as a project folder: inside a
    protected root (a Documents copies root included) and not strictly inside one of work_areas()."""
    try:
        target = _target(path, resolved)
    except (OSError, ValueError):
        return True
    if not any(_within(target, base) for base in _resolved()[0]):
        return False
    return not any(target.startswith(prefix) for prefix in _resolved(areas=True)[1])


def protected_below(folder, resolved=False):
    """The protected roots strictly inside `folder`, resolved and case-folded: what a project rooted at `folder` still
    refuses (a project at ~/Library/Application Support holds the old app-data folder; one at ~/Documents holds the
    Documents copies)."""
    try:
        base = _target(folder, resolved)
    except (OSError, ValueError):
        return []
    return [root for root in _resolved()[0] if root != base and _within(root, base)]


def protected_paths_below(folder):
    """protected_below(folder) as resolved paths in their own case (not case-folded), for rules that match paths
    exactly, such as a command sandbox's deny list (Phase 24, V1-003)."""
    try:
        base = _folded(folder)
    except (OSError, ValueError):
        return []
    out = []
    for root in protected_roots():
        real = os.path.realpath(os.path.abspath(root))
        if real.casefold() != base and _within(real.casefold(), base):
            out.append(Path(real))
    return out


def project_folder(folder, resolved=False):
    """(tool_refused(folder), protected_below(folder)) in one pass over the protected folders: a Workspace root."""
    try:
        target = _target(folder, resolved)
    except (OSError, ValueError):
        return True, []
    roots = _resolved()[0]
    if any(_within(target, base) for base in roots) and not any(
            target.startswith(prefix) for prefix in _resolved(areas=True)[1]):
        return True, []
    return False, [root for root in roots if root != target and _within(root, target)]


def _target(path, resolved):
    return str(path).casefold() if resolved else _folded(path)


# f22 follow-up: every Workspace checks its root against the protected folders, and resolving them took six of the
# seven realpath calls a Workspace made (113-131 us against main's 27). Their resolved, case-folded forms are kept per
# (user home, PEBBLE_HOME, generation); activate, deactivate, ensure_layout and migrate start a new generation. Each
# use also rechecks a cheap anchor of every folder it resolved (_anchor()), so a folder that appears, goes, becomes a
# link or moves under a relinked parent is resolved again: the answers stay those of an uncached resolve, with no
# time-based staleness.
_generation = 0
# (key, roots, their anchors, folded roots, work areas, their anchors, folded area prefixes, folded internal roots)
_cached = None


def _invalidate():
    global _generation
    with _registry_lock:
        _generation += 1


def _anchor(path):
    """What a resolved form of `path` depends on, read with one or two lstat calls: the nearest existing entry at or
    above it (levels up, device, inode, type) and, for a link, the device and inode it points at now."""
    up = 0  # `path` is absolute (protected_roots() and work_areas() build absolute paths)
    while True:
        try:
            info = os.lstat(path)
            break
        except OSError:
            parent = os.path.dirname(path)
            if parent == path:
                return (up, None)
            path, up = parent, up + 1
    mark = (up, info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.stat(path)
            mark += (target.st_dev, target.st_ino)
        except OSError:
            mark += (None,)
    return mark


def _anchors(paths):
    return tuple(_anchor(p) for p in paths)


def _resolved(areas=False, internal=False):
    """(folded protected roots, folded work-area prefixes or None unless `areas`), resolved now or still current.
    `internal`: only Pebble's internal folders (protected_roots(mirrors=False)), which come first in the list."""
    global _cached
    key = (os.path.expanduser('~'), os.environ.get(ENV), _generation)
    entry = _cached
    if entry is None or entry[0] != key or _anchors(entry[1]) != entry[2]:
        count = len(protected_roots(mirrors=False))
        roots = tuple(str(r) for r in protected_roots())
        marks = _anchors(roots)  # read before resolving: a change in between is seen on the next use
        folded = tuple(_folded(r) for r in roots)
        entry = (key, roots, marks, folded, None, None, None, folded[:count])
    if areas and (entry[4] is None or _anchors(entry[4]) != entry[5]):
        listed = tuple(str(a) for a in work_areas())
        marks = _anchors(listed)
        entry = entry[:4] + (listed, marks, tuple(_folded(a).rstrip(os.sep) + os.sep for a in listed), entry[7])
    _cached = entry
    return entry[7] if internal else entry[3], entry[6]


def inside_any(path, roots):
    """True when `path` (resolved, case-folded) is one of `roots` (as protected_below returns them) or inside one."""
    try:
        target = _folded(path)
    except (OSError, ValueError):
        return True
    return any(_within(target, root) for root in roots)


# ---- Files ----------------------------------------------------------------------------------------


def private_dir(path, mode=0o700):
    """Create `path` (and parents) or check it: a real directory, never a link; ours get `mode`."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        os.makedirs(path, mode=mode, exist_ok=True)
        info = os.lstat(path)
        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid():
            os.chmod(path, mode)
            info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DataHomeError(f'{path} is a link or not a folder, so Pebble left it alone.')
    if info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) != mode:
        os.chmod(path, mode)
    return path


def write_atomic(path, data, mode=0o600):
    """Replace `path` with `data` through a new same-folder file and rename; a link is never written through."""
    path = Path(path)
    if os.path.islink(path):
        raise DataHomeError(f'{path} is a link, so Pebble did not write it.')
    temp = path.with_name('.' + path.name + '.tmp-' + secrets.token_hex(6))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, mode)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return path


def refresh(path, data, mode=0o600):
    """write_atomic only when the content differs, so an unchanged file keeps its date."""
    try:
        with open(path, 'rb') as existing:
            if existing.read(len(data) + 1) == data:
                return False
    except OSError:
        pass
    write_atomic(path, data, mode)
    return True


def read_json(path, limit=1 << 20):
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        with os.fdopen(fd, 'rb') as source:
            fd = None
            value = json.loads(source.read(limit + 1))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def write_json(path, value, mode=0o600):
    return write_atomic(path, (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + '\n').encode(), mode)


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---- The internal folder --------------------------------------------------------------------------


def ensure_layout(layout):
    """Create ~/.pebble's folders and generated files. data/ is left to the move or to the first start."""
    if not layout.structured:
        return
    _invalidate()  # f22 follow-up: the folders below may be created now
    root = private_dir(layout.root)
    if not marked(root):
        write_json(root / MARKER, {'format': FORMAT, 'created': iso(time.time()),
                                   'note': 'Marks this folder as a Pebble home. Do not delete it.'})
    for name in FOLDERS:
        private_dir(root / name)
    refresh(root / README, internal_readme(layout).encode())
    refresh(root / SKILLS / README, SKILLS_README.encode())
    from .settings_file import schema
    refresh(root / SCHEMA, (json.dumps(schema(), indent=2) + '\n').encode())
    if not os.path.lexists(root / INSTRUCTIONS):
        write_atomic(root / INSTRUCTIONS, PEBBLE_MD_TEMPLATE.encode())


PEBBLE_MD_TEMPLATE = """<!--
PEBBLE.md: what Pebble should know about you and how you like to work.

Write plain sentences below this comment, for example "I prefer short answers" or "My name is Sam".
Pebble reads this file the same way it reads Settings -> Memory -> Your preferences: as information
about you, never as a permission. It cannot turn on tools or skip an approval. While memory (or the
"Your preferences" layer) is off, Pebble does not read it. Up to 4,000 characters are used: a longer
file is used up to its last full line within 4,000 characters, and Settings -> Data & storage says so.
Text inside comments like this one is ignored.
-->
"""

SKILLS_README = """# skills

Keep skill folders here if you like (each with its `SKILL.md`). Pebble never loads anything from this folder
by itself. To use a skill, open Pebble -> Settings -> Extensions -> Import and pick its folder: Pebble checks
it, pins an exact copy in its database and adds it switched off until you turn it on.

Safe to delete: yes. Skills you already imported keep working; this folder only holds your own copies.
"""


def internal_readme(layout):
    documents = 'your Documents folder (~/Documents/Pebble)' if layout.is_default else 'the folder named in settings.json'
    return f"""# Pebble's internal folder

This folder holds Pebble's settings and its database. It is like `~/.claude` for Claude Code: important,
private to you (only your user can open it) and not synced by iCloud. Your readable copies of conversations,
memories, attachments and exports are in {documents}; see the README there.

Pebble's own tools (file edits, terminal commands and connectors) are never allowed to change this folder, or
to read or change your Documents copies. Passwords and sign-ins are not here: they stay in the macOS Keychain.

| Item | What it is | Safe to delete? | What happens if deleted |
| --- | --- | --- | --- |
| `data/` | The live database (`pebble.sqlite` and its `-wal`/`-shm` files), attachments, exports, the browser profile, AgentGuard data, upgrade backups (`data/upgrade-backups/`), the local model's `model.log` and the service locks | **No** | Every conversation, memory, setting and history is lost. Quit Pebble before copying it anywhere |
| `settings.json` | Your settings (theme, permissions, memory switches, notifications, storage). `settings.schema.json` describes every key | Yes, with Pebble closed | Pebble writes it again from the settings in its database |
| `PEBBLE.md` | What Pebble should know about you, read like Settings -> Memory -> Your preferences | Yes | Pebble stops using it and forgets the preference it came from; an empty template comes back at the next start |
| `mcp.json` | A read-only list of your connectors (no passwords). Edits here are not applied | Yes | Pebble writes it again |
| `skills/` | Your own copies of skill folders to import | Yes | Imported skills keep working |
| `backups/` | Reserved for backups. The install tool keeps its backups in `~/PebbleBackups` instead (`scripts/pebble_install.py backups` lists them); upgrade backups of the database are in `data/upgrade-backups/` | Only if you no longer need them | You cannot restore from them |
| `runtime/` | Small working files: the last move report, the settings sync state and the Documents copy index | Yes, with Pebble closed | Pebble starts them again; a settings change or a forget waiting for your confirmation is dropped |
| `cache/` | Rebuildable caches | Yes | Pebble rebuilds what it needs |
| `logs/` | `service.log` and `backend.log` for troubleshooting, with their earlier parts (`backend.log.1` to `.3`, `service.log.1`) | Yes | Nothing; new logs start |
| `signing/` (only after Pebble's signing identity was created) | The password of Pebble's own code-signing keychain and the identity's record, used only to build Pebble on this Mac | **No** | New builds cannot be signed as the same Pebble, so macOS asks again for Keychain and privacy access after the next update |
| `README.md`, `.pebble-home.json` | This file and the marker that makes this a Pebble home | Do not delete the marker | Without the marker Pebble may not recognise this folder |

## Rules Pebble follows

- A change in `settings.json` that gives Pebble more access (for example turning on a tool group, choosing
  Auto or Full access, turning memory back on, or a new Documents location) is not applied until you confirm
  it in Pebble -> Settings -> Data & storage. Until then `settings.json` shows the setting in effect.
- If `settings.json` cannot be read or has a wrong value, Pebble keeps the last good settings and shows the
  problem in the app. Unknown keys are kept as they are.
- Files that Pebble writes here or in Documents are copies. Editing them does not change Pebble, except
  `settings.json` and `PEBBLE.md`, which are read as described above.

## Where the data came from

Pebble 0.5 and earlier kept everything in `~/Library/Application Support/{LEGACY_IDENTIFIER}`. On the first
launch of a newer version Pebble copies that folder to `data/`, checks the copy, switches to it and leaves a
`MOVED.md` in the old folder. The old folder is never deleted: once you are happy, you can delete it yourself.
"""


# ---- Status ---------------------------------------------------------------------------------------


def read_status(layout):
    if layout is None or not layout.structured:
        return None
    return read_json(layout.root / RUNTIME / STATUS)


def _save_status(layout, report):
    try:
        private_dir(layout.root / RUNTIME)
        write_json(layout.root / RUNTIME / STATUS, {'format': STATUS_FORMAT, **report})
    except OSError:
        pass


# ---- The move -------------------------------------------------------------------------------------


def move_pending(layout):
    """Whether this launch would try the move: the old home has a database, ~/.pebble/data has none and no
    receipt, and the old home has no pointer from an earlier move."""
    if not layout.structured or layout.legacy is None:
        return False
    return (os.path.lexists(layout.legacy / DATABASE) and not os.path.lexists(layout.data / MIGRATED)
            and not os.path.lexists(layout.data / DATABASE) and not os.path.lexists(layout.legacy / POINTER))


def migrate(layout, *, checkpoint=None, free_bytes=None, clock=time.time):
    """Decide where App runs, moving the old home into ~/.pebble/data once. Returns a report whose 'data'
    is the folder to use: ~/.pebble/data, or the old home when a move was refused or failed."""
    report = {'checked_at': iso(clock())}
    if not layout.structured:
        return {**report, 'state': 'not_applicable', 'data': str(layout.root)}
    try:
        report.update(_decide(layout, checkpoint or (lambda step: None), free_bytes or _free_bytes, clock))
    except _Refused as refusal:
        report.update(state='refused', reason=refusal.reason, data=str(layout.legacy), **refusal.facts)
    except Exception as error:  # never stop startup here: stay where the database already is
        old = layout.legacy is not None and os.path.lexists(layout.legacy / DATABASE)
        report.update(state='failed', reason='unexpected_error', error=type(error).__name__,
                      data=str(layout.legacy if old and not os.path.lexists(layout.data / MIGRATED) else layout.data))
    _invalidate()  # f22 follow-up: the move may have created, switched or left folders
    if report.get('reason'):
        report['message'] = REASONS.get(report['reason'], report['reason'])
    _save_status(layout, report)
    return report


def _free_bytes(path):
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def _decide(layout, checkpoint, free_bytes, clock):
    root, data, legacy = layout.root, layout.data, layout.legacy
    swept = _sweep_staging(root)
    done = read_json(data / MIGRATED)
    out = {'swept_staging': swept} if swept else {}
    if done is not None and done.get('format') == MOVE_FORMAT:
        out.update(state='current', data=str(data), moved_at=done.get('moved_at'), moved_from=done.get('from'))
        if legacy is not None and done.get('from') == str(legacy) and os.path.isdir(legacy):
            pointer = read_json(legacy / POINTER)
            if pointer is None:
                out['pointer'] = _write_pointer(layout, done)
            out['legacy_changed'] = _legacy_changed(legacy, done)
        return out
    has_old = legacy is not None and os.path.lexists(legacy / DATABASE)
    has_new = os.path.lexists(data / DATABASE)
    if not has_old:
        return {**out, 'state': 'current' if has_new else 'fresh', 'data': str(data)}
    pointer = read_json(legacy / POINTER)
    if pointer is not None:
        # The old folder was moved before. Never move the old copy a second time: its data is older.
        out.update(state='current' if has_new else 'fresh', data=str(data), moved_to=pointer.get('home'))
        if not has_new:
            out['warning'] = 'data_missing_after_move'
        return out
    if has_new:
        return {**out, 'state': 'conflict', 'reason': 'both_homes_have_data', 'data': str(legacy)}
    return {**out, **_move(layout, checkpoint, free_bytes, clock)}


def _sweep_staging(root):
    """Remove staging folders an interrupted move left inside ~/.pebble; only folders carrying our marker."""
    removed = 0
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        path = root / name
        if not name.startswith(STAGING) or not os.path.isdir(path) or os.path.islink(path):
            continue
        # Ours while copying (the staging marker) or just before the switch (the receipt replaced it).
        marker = read_json(path / STAGING_MARKER) or read_json(path / MIGRATED)
        if marker is None or marker.get('format') != MOVE_FORMAT:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += not os.path.lexists(path)
    return removed


def _move(layout, checkpoint, free_bytes, clock):
    from . import migrations
    from .runtime import claim_owner
    root, data, legacy = layout.root, layout.data, layout.legacy
    try:
        info = os.lstat(legacy)
    except OSError:
        raise _Refused('legacy_unsafe') from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise _Refused('legacy_unsafe')
    lease = directory = lock = staging = None
    started = clock()
    try:
        try:
            lease = claim_owner(legacy)  # the old service's lease: no Pebble service can run there meanwhile
        except ValueError:
            raise _Refused('legacy_in_use') from None
        try:
            directory = migrations._Directory.open(legacy, 0o022)
            if directory is None:
                raise _Refused('legacy_unsafe')
            lock = migrations._lock(directory, migrations.Limits())  # the existing upgrade-owner lock
        except migrations.MigrationRefused as refusal:
            raise _Refused('legacy_in_use' if refusal.reason == 'upgrade_owned' else 'legacy_unsafe') from None
        checkpoint('locked')
        inspection = migrations.inspect(legacy)
        if inspection.get('state') not in ('current', 'upgrade_required', 'fresh'):
            reason = {'upgrade_pending': 'legacy_upgrade_pending', 'unsupported': 'legacy_unsupported'}.get(
                inspection.get('state'), 'legacy_refused')
            raise _Refused(reason, legacy_state=inspection.get('state'))
        anchored = _anchored(legacy / DATABASE)
        if any(anchored.values()):
            raise _Refused('anchored_records', anchored=anchored)
        plan = _plan(legacy)
        database_bytes = sum(_size(legacy / (DATABASE + s)) for s in ('', '-wal'))
        need = plan['bytes'] + database_bytes + FREE_RESERVE
        free = free_bytes(root)
        if free < need:
            raise _Refused('not_enough_space', free_bytes=free, needed_bytes=need)
        staging = root / (STAGING + secrets.token_hex(8))
        os.mkdir(staging, 0o700)
        write_json(staging / STAGING_MARKER, {'format': MOVE_FORMAT, 'from': str(legacy), 'started': iso(started)})
        checkpoint('staging')
        try:
            files = _copy_tree(legacy, staging, plan)
        except OSError as error:
            raise _Failed('copy_failed', error=type(error).__name__) from None
        checkpoint('copied')
        database = _copy_database(legacy / DATABASE, staging / DATABASE)
        checkpoint('verified')
        _verify_tree(legacy, staging, plan, files)
        old = _database_identity(legacy)
        receipt = {'format': MOVE_FORMAT, 'from': str(legacy), 'to': str(data), 'home': str(root),
                   'moved_at': iso(clock()), 'files': len(files), 'links': len(plan['links']),
                   'dirs': len(plan['dirs']), 'bytes': plan['bytes'], 'left_in_old_folder': plan['left'],
                   'skipped_special': plan['special'], 'files_sha256': _manifest_digest(files),
                   'database': database, 'old_database': old}
        os.unlink(staging / STAGING_MARKER)
        write_json(staging / MIGRATED, receipt)
        _fsync_dir(staging)
        if os.path.lexists(data):
            if os.path.islink(data) or not os.path.isdir(data) or os.listdir(data):
                raise _Failed('data_folder_not_empty')
            os.rmdir(data)
        checkpoint('switch')
        try:
            os.rename(staging, data)
        except OSError as error:
            raise _Failed('switch_failed', error=type(error).__name__) from None
        staging = None
        _fsync_dir(root)
        checkpoint('switched')
        result = {'state': 'migrated', 'data': str(data), 'moved_at': receipt['moved_at'], 'moved_from': str(legacy),
                  'files': receipt['files'], 'bytes': receipt['bytes'], 'database': database,
                  'left_in_old_folder': plan['left'], 'seconds': round(clock() - started, 3)}
        result['pointer'] = _write_pointer(layout, receipt)
        return result
    except _Failed as failure:
        _discard(staging)
        return {'state': 'failed', 'reason': failure.reason, 'data': str(legacy), **failure.facts}
    except _Refused:
        _discard(staging)
        raise
    except Exception as error:
        _discard(staging)
        return {'state': 'failed', 'reason': 'unexpected_error', 'error': type(error).__name__, 'data': str(legacy)}
    finally:
        if lock is not None:
            os.close(lock)
        if directory is not None:
            directory.close()
        if lease is not None:
            lease.close()


class _Failed(Exception):
    def __init__(self, reason, **facts):
        super().__init__(reason)
        self.reason, self.facts = reason, facts


def _discard(staging):
    if staging is not None and os.path.isdir(staging) and not os.path.islink(staging):
        shutil.rmtree(staging, ignore_errors=True)


def _size(path):
    try:
        info = os.lstat(path)
    except OSError:
        return 0
    return info.st_size if stat.S_ISREG(info.st_mode) else 0


def _anchored(database):
    counts = {'git_worktrees': 0, 'environments': 0}
    try:
        db = sqlite3.connect(Path(database).as_uri() + '?mode=ro', uri=True)
    except sqlite3.Error:
        return counts
    try:
        for key, sql in (('git_worktrees', LIVE_WORKTREES), ('environments', LIVE_ENVIRONMENTS)):
            try:
                counts[key] = db.execute(sql).fetchone()[0]
            except sqlite3.OperationalError:
                pass  # the table is absent: an older or a fresh database
    finally:
        db.close()
    return counts


def _plan(legacy):
    """Everything to copy below the old home, never following a link; counts and sizes only."""
    plan = {'dirs': [], 'files': [], 'links': [], 'left': [], 'special': [], 'bytes': 0}

    def walk(base, rel):
        with os.scandir(base) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                path = rel + (entry.name,)
                name = '/'.join(path)
                if not rel and (entry.name in LOCKS or entry.name in LEFT_FILES or entry.name in LEFT_DIRS
                                or entry.name in (DATABASE, *(DATABASE + s for s in SIDECARS))):
                    if entry.name not in LOCKS and not entry.name.startswith(DATABASE):
                        plan['left'].append(name)
                    continue
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    plan['links'].append(name)
                elif stat.S_ISDIR(info.st_mode):
                    plan['dirs'].append(name)
                    walk(entry.path, path)
                elif stat.S_ISREG(info.st_mode):
                    plan['files'].append(name)
                    plan['bytes'] += info.st_size
                else:
                    plan['special'].append(name)  # sockets and pipes belong to a running process

    walk(legacy, ())
    return plan


def _copy_tree(legacy, staging, plan):
    for name in plan['dirs']:
        mode = stat.S_IMODE(os.lstat(legacy / name).st_mode)
        os.mkdir(staging / name, 0o700)
        os.chmod(staging / name, mode | 0o700)
    for name in plan['links']:
        os.symlink(os.readlink(legacy / name), staging / name)
    files = {}
    for name in plan['files']:
        source, target = legacy / name, staging / name
        fd = os.open(source, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
        with os.fdopen(fd, 'rb') as reader:
            before = os.fstat(reader.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OSError(errno.EINVAL, 'not a regular file')
            out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
            digest, total = hashlib.sha256(), 0
            with os.fdopen(out, 'wb') as writer:
                while chunk := reader.read(CHUNK):
                    digest.update(chunk)
                    writer.write(chunk)
                    total += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            after = os.fstat(reader.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or total != after.st_size:
            raise _Failed('legacy_changed_during_copy', path=name)
        os.chmod(target, stat.S_IMODE(before.st_mode))
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns), follow_symlinks=False)
        files[name] = {'sha256': digest.hexdigest(), 'bytes': total, 'mode': stat.S_IMODE(before.st_mode)}
    for name in reversed(plan['dirs']):
        os.utime(staging / name, ns=(os.lstat(legacy / name).st_atime_ns, os.lstat(legacy / name).st_mtime_ns))
    return files


def _hash(path):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
    with os.fdopen(fd, 'rb') as reader:
        while chunk := reader.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_tree(legacy, staging, plan, files):
    """Every copied file hashed again on both sides, every link and folder present: the copy is exact."""
    for name, entry in files.items():
        if _hash(legacy / name) != entry['sha256']:
            raise _Failed('legacy_changed_during_copy', path=name)
        if _hash(staging / name) != entry['sha256']:
            raise _Failed('verify_failed', what='file', path=name)
    for name in plan['links']:
        if os.readlink(legacy / name) != os.readlink(staging / name):
            raise _Failed('verify_failed', what='link', path=name)
    for name in plan['dirs']:
        if not os.path.isdir(staging / name) or os.path.islink(staging / name):
            raise _Failed('verify_failed', what='folder', path=name)


def _manifest_digest(files):
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(json.dumps([name, files[name]['sha256'], files[name]['bytes']]).encode() + b'\n')
    return digest.hexdigest()


def _database_identity(legacy):
    out = {}
    for suffix in ('', '-wal'):
        try:
            info = os.lstat(legacy / (DATABASE + suffix))
        except OSError:
            out[suffix or 'db'] = None
            continue
        out[suffix or 'db'] = {'bytes': info.st_size, 'mtime_ns': info.st_mtime_ns, 'inode': info.st_ino}
    return out


def _legacy_changed(legacy, done):
    """Whether the old database changed after the move, as it does when an older Pebble ran on it again."""
    recorded = (done or {}).get('old_database')
    if not isinstance(recorded, dict):
        return None
    now = _database_identity(legacy)
    return any(_comparable(now.get(k)) != _comparable(recorded.get(k)) for k in ('db', '-wal'))


def _comparable(entry):
    return None if not entry else (entry.get('bytes'), entry.get('mtime_ns'))


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _encode(value):
    if value is None:
        return b'N'
    if isinstance(value, bytes):
        return b'B' + str(len(value)).encode() + b':' + value
    if isinstance(value, int):
        return b'I' + str(value).encode() + b';'
    if isinstance(value, float):
        return b'F' + value.hex().encode() + b';'
    data = str(value).encode('utf-8', 'surrogatepass')
    return b'T' + str(len(data)).encode() + b':' + data


def database_summary(db):
    """Per table: row count and a digest of every row in rowid order; the epoch. Counts and hashes only."""
    tables = {}
    names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite~_%' "
                                      "ESCAPE '~' ORDER BY name")]
    for name in names:
        source = _quote(name)
        try:
            try:
                cursor = db.execute(f'SELECT * FROM {source} ORDER BY rowid')
            except sqlite3.OperationalError:
                cursor = db.execute(f'SELECT * FROM {source}')
            digest, rows = hashlib.sha256(), 0
            for row in cursor:
                rows += 1
                digest.update(b'R')
                for value in row:
                    digest.update(_encode(value))
            tables[name] = {'rows': rows, 'sha256': digest.hexdigest()}
        except sqlite3.OperationalError:
            tables[name] = {'rows': None, 'sha256': None}  # a virtual table whose module is unavailable
    return {'user_version': db.execute('PRAGMA user_version').fetchone()[0], 'tables': tables}


def _open_source(path):
    """The old database, read-only. A WAL database whose -shm is missing may need a read-write open; the
    service lease keeps every writer out, so that open changes no data (SQLite may checkpoint on close)."""
    try:
        db = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)
        db.execute('SELECT count(*) FROM sqlite_master').fetchone()
        return db, 'read_only'
    except sqlite3.Error:
        db = sqlite3.connect(str(path))
        return db, 'read_write'


def _copy_database(source_path, target_path):
    """SQLite's online backup (committed WAL frames included), then integrity_check and an exact per-table
    comparison of row counts and row digests between the old database and the copy."""
    try:
        source, opened = _open_source(source_path)
    except sqlite3.Error as error:
        raise _Failed('copy_failed', what='database', error=type(error).__name__) from None
    try:
        try:
            source.execute('BEGIN')
            before = database_summary(source)
            target = sqlite3.connect(str(target_path))
            try:
                source.backup(target)
            finally:
                target.close()
            source.execute('ROLLBACK')
        except sqlite3.Error as error:
            raise _Failed('copy_failed', what='database', error=type(error).__name__) from None
    finally:
        source.close()
    os.chmod(target_path, 0o600)
    try:
        copy = sqlite3.connect(str(target_path))
        try:
            integrity = copy.execute('PRAGMA integrity_check').fetchall()
            after = database_summary(copy)
        finally:
            copy.close()
    except sqlite3.Error as error:
        raise _Failed('verify_failed', what='database', error=type(error).__name__) from None
    if integrity != [('ok',)]:
        raise _Failed('verify_failed', what='integrity_check')
    if after != before:
        changed = sorted(name for name in set(before['tables']) | set(after['tables'])
                         if before['tables'].get(name) != after['tables'].get(name))
        raise _Failed('verify_failed', what='tables', tables=changed[:24],
                      user_version=[before['user_version'], after['user_version']])
    return {'integrity': 'ok', 'user_version': after['user_version'], 'tables': len(after['tables']),
            'rows': sum(t['rows'] or 0 for t in after['tables'].values()), 'opened': opened,
            'row_counts': {name: t['rows'] for name, t in after['tables'].items()}}


def _write_pointer(layout, receipt):
    """MOVED.md for the person and pebble-moved.json for tools, in the old folder; the only files written there."""
    legacy = layout.legacy
    pointer = {'format': POINTER_FORMAT, 'home': str(layout.root), 'data': str(layout.data),
               'moved_at': receipt.get('moved_at'), 'files': receipt.get('files'),
               'files_sha256': receipt.get('files_sha256'), 'database': receipt.get('database'),
               'old_database': receipt.get('old_database')}
    try:
        write_json(legacy / POINTER, pointer)
        write_atomic(legacy / MOVED, moved_text(layout, receipt).encode())
        return 'written'
    except OSError:
        return 'failed'


def moved_text(layout, receipt):
    left = receipt.get('left_in_old_folder') or []
    kept = ''.join(f'- `{name}`\n' for name in left) or '- nothing\n'
    return f"""# Pebble's data moved

On {receipt.get('moved_at')} Pebble copied this folder to `{layout.data}`, checked the copy (every file's
SHA-256, and the database's integrity and row counts) and switched to it. Pebble no longer uses this folder.

- Pebble's internal folder is now `{layout.root}` (see its README.md).
- Your readable copies (conversations, memories, attachments) are in `~/Documents/Pebble`.

Nothing here was deleted or changed, except that this file and `{POINTER}` were added. These items were not
copied because they belong to the old service or name paths inside this folder:

{kept}
If you go back to an older Pebble, it uses this folder as it was when the data moved; anything you do in that
older version stays here. When you are happy with the new location, you can delete this whole folder yourself.
"""
