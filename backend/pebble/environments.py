"""Managed dependency environments installed only from lockfiles (X08).

An environment is a host-generated folder <app home>/environments/<32-hex id>.
A pinned toolchain installs it under macOS sandbox-exec: reads are limited to
the toolchain, the folder and system paths, writes to the folder, and network
to one EgressBroker port that admits only the reviewed registry hosts.
Lifecycle scripts never run: npm uses --ignore-scripts, and pip installs only
hash-pinned binary wheels without dependency resolution, so no setup.py runs.

Manifests, lockfiles, registry responses and installer output are untrusted
data, never approval. plan() returns data only. install() re-verifies the
project root, file digests and toolchain identities, registers its row before
every effect and never retries: an install whose outcome was not observed
settles `uncertain`, and startup turns `installing` into `uncertain`. Only a
folder this module created and can still identify by inode is ever deleted.
"""

import base64
import binascii
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .egress import EgressBroker, allowlist, network_profile
from .permissions import DEFAULT
from .policy import PolicyError, Workspace, digest

PLATFORM = sys.platform
SANDBOX = Path('/usr/bin/sandbox-exec')
FRAMEWORK = Path('/Library/Frameworks/Python.framework')
PYTHON = FRAMEWORK / 'Versions' / '3.14' / 'bin' / 'python3'
NODE_ROOT = Path.home() / '.nvm' / 'versions' / 'node'
IDENT = re.compile(r'[0-9a-f]{32}')
SHA256 = re.compile(r'[0-9a-f]{64}')
FILES = {'npm-ci': ('package.json', 'package-lock.json'), 'pip-hashes': ('requirements.txt', 'requirements.txt')}
PUBLIC = {'npm-ci': (('registry.npmjs.org', 443),), 'pip-hashes': (('pypi.org', 443), ('files.pythonhosted.org', 443))}
STATES = ('installing', 'ready', 'failed', 'uncertain', 'removed')
LIVE = ('installing', 'ready', 'uncertain')
REMOVABLE = ('ready', 'failed', 'uncertain')
MAX_LOCK, MAX_MANIFEST, MAX_CA = 8_000_000, 1_000_000, 1_000_000
MAX_NPM_PACKAGES, MAX_PIP_PACKAGES, MAX_REQUIREMENT_LINES = 20_000, 1_000, 20_000
MAX_REGISTRIES = 4
SIZE_LIMIT, MIN_SIZE_LIMIT = 1 << 30, 1_000_000
TIMEOUT = 600
MAX_PER_CHAT, MAX_TOTAL = 4, 16
MAX_OUTPUT, OUTPUT_CHARS = 192_000, 16_000
FULL_TREE_FILES, MAX_TREE_FILES, PARTIAL_HASH_BYTES = 50_000, 200_000, 1_000_000
MAX_TOOL_BYTES, MAX_TOOL_TREE_FILES = 512_000_000, 20_000
EGRESS_BYTES, EGRESS_SLACK = 512_000_000, 64_000_000
CHECK_SECONDS, DRAIN_SECONDS = 2.0, 2.0
PAYLOAD = frozenset({'operation', 'kind', 'environment_id', 'chat_id', 'root', 'root_identity', 'manifest_digest',
                     'lock_digest', 'toolchain', 'registries', 'packages', 'size_limit', 'timeout', 'requires_review',
                     'network', 'effects', 'summary'})
TEST_ONLY = frozenset({'ca_file', 'loopback_targets'})
# App.propose annotates every stored payload with the policy mode; it grants nothing here.
BROKER = frozenset({'permission_mode'})
NPM_NAME = re.compile(r'(?:@[a-z0-9][a-z0-9._~-]{0,100}/)?[a-z0-9._~-][a-z0-9._~-]{0,100}', re.I)
SRI = re.compile(r'(sha1|sha256|sha384|sha512)-([A-Za-z0-9+/]+={0,2})')
REQUIREMENT = re.compile(r'([A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?)==([A-Za-z0-9][A-Za-z0-9.+!_-]{0,62})')
HASH = re.compile(r'--hash=sha256:[0-9a-f]{64}')
SOURCES = (('git', 'git sources'), ('file', 'file sources'), ('link', 'links or workspaces'), ('http', 'plain http'),
           ('host', 'another registry host'), ('other', 'unsupported sources'), ('unresolved', 'no resolved URL'),
           ('integrity', 'no sha512 integrity'), ('malformed', 'malformed entries'))


class InstallOutcomeUncertain(RuntimeError):
    """An installer started but its outcome was not observed. Nothing is retried."""


class _NotStarted(Exception):
    pass


class _Unobserved(Exception):
    def __init__(self, reason, output=''):
        super().__init__(reason)
        self.reason, self.output = reason, output


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def message(exc):
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', type(exc).__name__ + ': ' + str(exc))[:500]


def lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _load(text):
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _text(data):
    """Installer output: untrusted text, control characters removed, last OUTPUT_CHARS kept."""
    text = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', ' ', data.decode('utf-8', 'replace'))
    return text[-OUTPUT_CHARS:], len(text) > OUTPUT_CHARS


def authority(host, port):
    name = f'[{host}]' if ':' in host else host
    return name if port == 443 else f'{name}:{port}'


# Identities

def file_identity(path, limit=MAX_TOOL_BYTES):
    """Pin one toolchain file: realpath, sha256, size, mtime, device, inode, owner and mode.

    Same shape as recipes.pin (IDENTITY), so a mounted interpreter re-pins equal.
    """
    path = Path(path)
    try:
        real = Path(os.path.realpath(path, strict=True))
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise PolicyError(f'Toolchain file {path.name} is missing or unreadable.') from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o002
                or info.st_size > limit):
            raise PolicyError(f'Toolchain file {path.name} is not a protected regular file.')
        hasher, total = hashlib.sha256(), 0
        while chunk := os.read(fd, 1 << 20):
            hasher.update(chunk)
            total += len(chunk)
            if total > limit:
                raise PolicyError(f'Toolchain file {path.name} exceeds its bound.')
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if total != info.st_size or (info.st_mtime_ns, info.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
        raise PolicyError(f'Toolchain file {path.name} changed while it was pinned.')
    return {'path': str(path), 'realpath': str(real), 'sha256': hasher.hexdigest(), 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'dev': info.st_dev, 'ino': info.st_ino, 'uid': info.st_uid,
            'mode': info.st_mode}


def _file_digest(path, size):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise PolicyError('An environment file changed while it was measured.')
        hasher, total = hashlib.sha256(), 0
        while chunk := os.read(fd, 1 << 20):
            hasher.update(chunk)
            total += len(chunk)
        if total != size:
            raise PolicyError('An environment file changed while it was measured.')
        return hasher.hexdigest()
    finally:
        os.close(fd)


def tree(root, full_files=FULL_TREE_FILES, max_files=MAX_TREE_FILES, partial_bytes=PARTIAL_HASH_BYTES):
    """Bounded digest of a folder that never follows links.

    Entries in path order: [relpath, size, sha256] for files and
    [relpath, 'symlink', target] for links. Above full_files files every file
    is [relpath, size], content is hashed only for files that fit in the first
    partial_bytes (in path order) and the result is labelled partial. Special
    files and more than max_files entries are refused.
    """
    root = Path(root)
    entries, stack, size = [], [''], 0
    while stack:
        folder = stack.pop()
        with os.scandir(root / folder if folder else root) as listing:
            for entry in listing:
                rel = f'{folder}/{entry.name}' if folder else entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    stack.append(rel)
                    continue
                if stat.S_ISREG(info.st_mode):
                    entries.append((rel, info.st_size))
                    size += info.st_size
                elif stat.S_ISLNK(info.st_mode):
                    entries.append((rel, None))
                else:
                    raise PolicyError('The environment contains a special file (device, socket or FIFO).')
                if len(entries) > max_files:
                    raise PolicyError(f'The environment has more than {max_files} files.')
    entries.sort()
    partial = len(entries) > full_files
    hasher, budget, links = hashlib.sha256(b'pebble-tree-v1\n'), partial_bytes, []
    for rel, length in entries:
        if length is None:
            target = os.readlink(root / rel)
            links.append([rel, target])
            item = [rel, 'symlink', target]
        elif not partial:
            item = [rel, length, _file_digest(root / rel, length)]
        elif length <= budget:
            budget -= length
            item = [rel, length, _file_digest(root / rel, length)]
        else:
            item = [rel, length]
        # ensure_ascii keeps undecodable names (surrogate escapes) encodable.
        hasher.update(json.dumps(item, separators=(',', ':')).encode() + b'\n')
    return {'digest': hasher.hexdigest(), 'files': len(entries), 'bytes': size, 'partial': partial, 'links': links}


def _exceeds(root, limit):
    """Cheap in-flight size check; vanished entries are ignored."""
    stack, size, count = [str(root)], 0, 0
    while stack:
        try:
            with os.scandir(stack.pop()) as listing:
                for entry in listing:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    count += 1
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(entry.path)
                    else:
                        size += info.st_size
                    if size > limit or count > MAX_TREE_FILES:
                        return True
        except OSError:
            continue
    return False


def _exited(process):
    """Observe the leader's exit without reaping it, so its group ID stays pinned."""
    if not hasattr(os, 'waitid'):
        return process.poll() is not None
    try:
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _reap(process):
    # The leader is still unreaped, so this group ID cannot belong to anyone else.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# Lockfile and requirement policy (pure data checks)

def _json(data, label):
    try:
        return json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise PolicyError(f'{label} is not valid UTF-8 JSON.') from None


def _integrity(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        return False
    strong = False
    for token in value.split():
        match = SRI.fullmatch(token)
        if not match:
            return False
        if match[1] == 'sha512':
            try:
                if len(base64.b64decode(match[2], validate=True)) != 64:
                    return False
            except (binascii.Error, ValueError):
                return False
            strong = True
    return strong


def _source(resolved, authorities):
    """None for an https URL on an allowed registry, else the refusal category."""
    if not isinstance(resolved, str) or not resolved:
        return 'unresolved'
    lower = resolved.lower()
    if lower.startswith(('git+', 'git:', 'github:', 'gitlab:', 'bitbucket:', 'gist:', 'ssh:')):
        return 'git'
    if lower.startswith('file:'):
        return 'file'
    if lower.startswith('link:'):
        return 'link'
    if lower.startswith('http:'):
        return 'http'
    if not lower.startswith('https://') or len(resolved) > 2048 or re.search(r'[\s\x00-\x1f\x7f\\]', resolved):
        return 'other'
    parts = urlsplit(resolved)
    if parts.netloc not in authorities or not parts.path.startswith('/') or parts.fragment:
        return 'host'
    return None


def _label(key, index):
    name = key.rsplit('node_modules/', 1)[-1] if isinstance(key, str) else ''
    return name if NPM_NAME.fullmatch(name) else f'entry {index}'


def _refusal(prefix, problems):
    parts = []
    for key, label in SOURCES:
        names = sorted(set(problems.get(key, ())))
        if names:
            shown = ', '.join(names[:10]) + (f' and {len(names) - 10} more' if len(names) > 10 else '')
            parts.append(f'{label}: {shown}')
    return prefix + '; '.join(parts) + '.'


def check_npm(manifest, lock, registries):
    """Validate package.json and a v2/v3 package-lock.json; return the package count.

    Refusals name packages only, never URLs or other lockfile text.
    """
    if not isinstance(_json(manifest, 'package.json'), dict):
        raise PolicyError('package.json must be a JSON object.')
    data = _json(lock, 'package-lock.json')
    if not isinstance(data, dict):
        raise PolicyError('package-lock.json must be a JSON object.')
    version = data.get('lockfileVersion')
    if type(version) is not int or version not in (2, 3):
        raise PolicyError('Only package-lock.json lockfileVersion 2 or 3 is supported. Regenerate it with npm 7 or newer.')
    packages = data.get('packages')
    if not isinstance(packages, dict) or not isinstance(packages.get(''), dict):
        raise PolicyError('package-lock.json has no packages section with a root entry.')
    if len(packages) - 1 > MAX_NPM_PACKAGES:
        raise PolicyError(f'package-lock.json lists more than {MAX_NPM_PACKAGES} packages.')
    authorities = {authority(host, port) for host, port in registries}
    problems, count = {}, 0
    for index, (key, entry) in enumerate(packages.items()):
        if key == '':
            continue
        count += 1
        name = _label(key, index)
        if not isinstance(entry, dict):
            kind = 'malformed'
        elif not key.startswith('node_modules/') or 'link' in entry:
            kind = 'link'
        else:
            kind = _source(entry.get('resolved'), authorities)
            if kind is None and not _integrity(entry.get('integrity')):
                kind = 'integrity'
        if kind:
            problems.setdefault(kind, []).append(name)
    # npm 7+ ignores the v2 compatibility tree, but it must not smuggle sources either.
    stack, seen = [data.get('dependencies', {})], 0
    while stack:
        level = stack.pop()
        if not isinstance(level, dict):
            problems.setdefault('malformed', []).append('dependencies')
            continue
        for name, entry in level.items():
            seen += 1
            if seen > 2 * MAX_NPM_PACKAGES:
                raise PolicyError('package-lock.json dependencies are too large.')
            label = name if NPM_NAME.fullmatch(name) else 'dependency entry'
            if not isinstance(entry, dict):
                problems.setdefault('malformed', []).append(label)
                continue
            spec = entry.get('version')
            if isinstance(spec, str) and re.match(r'(?i)(git|file:|link:|http|github:|gitlab:|bitbucket:|gist:|ssh:)', spec):
                problems.setdefault(_source(spec, authorities) or 'other', []).append(label)
            if 'resolved' in entry and (kind := _source(entry['resolved'], authorities)):
                problems.setdefault(kind, []).append(label)
            if 'dependencies' in entry:
                stack.append(entry['dependencies'])
    if problems:
        raise PolicyError(_refusal('The npm lockfile was refused. Packages with ', problems))
    if count == 0:
        raise PolicyError('package-lock.json lists no packages to install.')
    return count


def _why(token):
    lower = token.lower()
    if lower == '-e' or lower.startswith(('-e', '--editable')):
        return 'editable installs are refused'
    if lower.startswith(('-r', '-c', '--requirement', '--constraint')):
        return 'includes and constraint files are refused'
    if lower.startswith(('-i', '--index-url', '--extra-index-url', '-f', '--find-links', '--no-index', '--trusted-host')):
        return 'index and source options are refused'
    if lower.startswith('-'):
        return 'options other than --hash are refused'
    if '://' in token or '@' in token:
        return 'URL requirements are refused'
    if '/' in token or '\\' in token or token.startswith('.') or lower.endswith(('.whl', '.zip', '.tar.gz')):
        return 'path requirements are refused'
    return 'use an exact name==version pin without extras or markers'


def check_requirements(data):
    """Validate a hash-pinned requirements.txt; return the package count.

    Each logical line is name==version plus one or more --hash=sha256:<hex>.
    Refusals name line numbers and reasons, never line content.
    """
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise PolicyError('requirements.txt must be UTF-8 text.') from None
    lines = text.splitlines()
    if '\x00' in text or len(lines) > MAX_REQUIREMENT_LINES:
        raise PolicyError('requirements.txt is not a bounded text file.')
    logical, buffer, start = [], '', None
    for number, line in enumerate(lines, 1):
        start = start or number
        if line.endswith('\\'):
            buffer += line[:-1] + ' '
            continue
        logical.append((start, buffer + line))
        buffer, start = '', None
    if start is not None:
        logical.append((start, buffer))
    names, problems = set(), []
    for number, line in logical:
        content = re.sub(r'(^|\s)#.*$', '', line).strip()
        if not content:
            continue
        tokens = content.split()
        match = REQUIREMENT.fullmatch(tokens[0])
        if not match:
            problems.append(f'line {number}: {_why(tokens[0])}')
        elif len(tokens) == 1 or not all(HASH.fullmatch(token) for token in tokens[1:]):
            problems.append(f'line {number}: every pin needs --hash=sha256:<64 hex> and no other options')
        else:
            key = re.sub(r'[-_.]+', '-', match[1]).lower()
            if key in names:
                problems.append(f'line {number}: duplicate package')
            names.add(key)
    if problems:
        more = f' and {len(problems) - 10} more' if len(problems) > 10 else ''
        raise PolicyError('requirements.txt was refused: ' + '; '.join(problems[:10]) + more + '.')
    if not names:
        raise PolicyError('requirements.txt lists no hash-pinned packages.')
    if len(names) > MAX_PIP_PACKAGES:
        raise PolicyError(f'requirements.txt lists more than {MAX_PIP_PACKAGES} packages.')
    return len(names)


# Shared with migrations.REFERENCE_DDL (schema epoch 4); keep them identical.
ENVIRONMENT_DDL = """
CREATE TABLE IF NOT EXISTS project_environments(id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,
  root TEXT NOT NULL,root_identity TEXT NOT NULL,kind TEXT NOT NULL,manifest_digest TEXT NOT NULL,
  lock_digest TEXT NOT NULL,toolchain TEXT NOT NULL,registries TEXT NOT NULL,state TEXT NOT NULL,
  size_bytes INTEGER,file_count INTEGER,evidence TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS project_environments_chat ON project_environments(chat_id,state);
"""

class Environments:
    required_permissions = ('files', 'tests', 'web')

    def __init__(self, app):
        self.app, self.store = app, app.store
        self.lock = threading.RLock()
        self.installing = threading.Lock()
        self.removing = threading.Lock()
        self.processes, self.brokers = set(), set()
        self.closed = False
        self.anchor = Path(app.home).resolve() / 'environments'
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Managed environments require an outside transaction boundary.')
            with db:
                for statement in ENVIRONMENT_DDL.split(';'):
                    if statement.strip():
                        db.execute(statement)
                # An installing row means the outcome was never observed. Nothing is retried.
                for ident, evidence in db.execute("SELECT id,evidence FROM project_environments WHERE state='installing'").fetchall():
                    kept = {key: value for key, value in _load(evidence).items() if key == 'dir_identity'}
                    db.execute("UPDATE project_environments SET state='uncertain',evidence=?,updated=? WHERE id=? AND state='installing'",
                               (canonical({**kept, 'phase': 'interrupted', 'interrupted': 'installing',
                                           'note': 'The service stopped before this install was confirmed. Nothing was retried.'}),
                                time.time(), ident))

    # Storage and scope

    def _rows(self, sql, args=()):
        with self.store.lock:
            if self.store.db.in_transaction:
                raise PolicyError('Managed environments require an outside transaction boundary.')
            cursor = self.store.db.execute(sql, args)
            names = [item[0] for item in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _write(self, sql, args):
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Managed environments require an outside transaction boundary.')
            with db:
                db.execute(sql, args)

    def _mark(self, ident, state, evidence, size=None, files=None):
        self._write('UPDATE project_environments SET state=?,evidence=?,size_bytes=?,file_count=?,updated=? WHERE id=?',
                    (state, canonical(evidence), size, files, time.time(), ident))

    def _row(self, chat_id, ident):
        if not isinstance(ident, str) or not IDENT.fullmatch(ident):
            raise PolicyError('Choose a managed environment ID.')
        rows = self._rows('SELECT * FROM project_environments WHERE id=? AND chat_id=?', (ident, chat_id))
        if not rows:
            raise PolicyError('Managed environment not found in this conversation.')
        row = rows[0]
        return {**row, 'root_identity': json.loads(row['root_identity']), 'toolchain': json.loads(row['toolchain']),
                'registries': json.loads(row['registries']), 'evidence': _load(row['evidence'])}

    def _public(self, row):
        evidence = _load(row['evidence'])
        return {'environment_id': row['id'], 'kind': row['kind'], 'state': row['state'], 'root': row['root'],
                'path': str(self.anchor / row['id']), 'host_generated': True, 'manifest_digest': row['manifest_digest'],
                'lock_digest': row['lock_digest'], 'registries': json.loads(row['registries']),
                'size_bytes': row['size_bytes'], 'file_count': row['file_count'], 'tree_digest': evidence.get('tree_digest'),
                'reason': evidence.get('reason'), 'phase': evidence.get('phase'), 'created': row['created'],
                'updated': row['updated']}

    def _permit(self, *groups):
        tools = self.store.setting('permissions', DEFAULT)['tools']
        if not all(tools.get(group) for group in groups):
            raise PolicyError('Managed environments require ' + ' and '.join(groups) + ' access in Permissions.')

    @staticmethod
    def _supported():
        if PLATFORM != 'darwin' or not SANDBOX.is_file():
            raise PolicyError('Managed dependency environments currently require macOS sandbox-exec. Other platforms stay disabled until their isolation is implemented and tested.')

    def _chat(self, chat_id):
        if not isinstance(chat_id, str) or not 1 <= len(chat_id) <= 128:
            raise PolicyError('Choose a conversation.')
        rows = self._rows('SELECT workspace FROM chats WHERE id=?', (chat_id,))
        if not rows:
            raise PolicyError('Conversation not found.')
        return rows[0]

    def _workspace(self, chat_id, ws):
        chat = self._chat(chat_id)
        if not isinstance(ws, Workspace):
            raise PolicyError('A verified project workspace is required.')
        if chat['workspace'] != str(ws.root):
            raise PolicyError("This is not the conversation's selected project folder.")
        info = lstat(ws.root)
        if info is None or (info.st_dev, info.st_ino) != tuple(ws.identity):
            raise PolicyError('The selected workspace was replaced. Start a fresh task.')
        return ws

    def _anchor_check(self):
        info = lstat(self.anchor)
        if (info is None or not stat.S_ISDIR(info.st_mode) or os.path.realpath(self.anchor) != str(self.anchor)
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise PolicyError('The managed environment storage folder is missing, shared or replaced.')

    def _ensure_anchor(self):
        try:
            os.mkdir(self.anchor, 0o700)
        except FileExistsError:
            pass
        self._anchor_check()

    def _overlap(self, root):
        if self.anchor == root or self.anchor.is_relative_to(root) or root.is_relative_to(self.anchor):
            raise PolicyError('Managed environment storage overlaps the selected project.')

    def _capacity(self, chat_id):
        rows = self._rows("SELECT chat_id FROM project_environments WHERE state IN ('installing','ready','uncertain')")
        if sum(row['chat_id'] == chat_id for row in rows) >= MAX_PER_CHAT:
            raise PolicyError('Remove a managed environment before creating more than four in a conversation.')
        if len(rows) >= MAX_TOTAL:
            raise PolicyError('Remove a managed environment before creating more than sixteen.')

    def _present(self, ident):
        if not isinstance(ident, str) or not IDENT.fullmatch(ident):
            return False
        try:
            return os.path.lexists(self.anchor / ident)
        except (OSError, ValueError):
            return True

    def _read(self, ws, name, limit):
        """One bounded regular file at the project root, opened without following links."""
        try:
            root_fd = os.open(ws.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise PolicyError('The project folder is unavailable.') from None
        try:
            info = os.fstat(root_fd)
            if (info.st_dev, info.st_ino) != tuple(ws.identity):
                raise PolicyError('The selected workspace was replaced. Start a fresh task.')
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            except FileNotFoundError:
                raise PolicyError(f'{name} is required at the project root.') from None
            except OSError:
                raise PolicyError(f'{name} must be a regular file, not a symbolic link.') from None
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                    raise PolicyError(f'{name} must be a regular file of at most {limit // 1_000_000} MB.')
                chunks, size = [], 0
                while chunk := os.read(fd, limit + 1 - size):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        raise PolicyError(f'{name} must be a regular file of at most {limit // 1_000_000} MB.')
                after = os.fstat(fd)
            finally:
                os.close(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise PolicyError(f'{name} changed during the read. Try again.')
            return b''.join(chunks)
        finally:
            os.close(root_fd)

    def _inspect(self, ws, kind, registries):
        manifest_name, lock_name = FILES[kind]
        if kind == 'npm-ci':
            manifest, lock = self._read(ws, manifest_name, MAX_MANIFEST), self._read(ws, lock_name, MAX_LOCK)
            count = check_npm(manifest, lock, registries)
        else:
            manifest = lock = self._read(ws, manifest_name, MAX_MANIFEST)
            count = check_requirements(manifest)
        return {'manifest_digest': digest(manifest), 'lock_digest': digest(lock), 'packages': count,
                'files': {manifest_name: manifest, lock_name: lock}}

    # Toolchains

    @staticmethod
    def _discover(kind):
        if kind == 'pip-hashes':
            bundled = sorted((PYTHON.parent.parent / 'lib' / 'python3.14' / 'ensurepip' / '_bundled').glob('pip-*-py3-none-any.whl'))
            if not PYTHON.is_file() or len(bundled) != 1:
                raise PolicyError('Install the python.org Python 3.14 framework (with its bundled pip) to create pip environments.')
            return {'python': PYTHON, 'pip_wheel': bundled[0]}
        versions = []
        for node in NODE_ROOT.glob('v*/bin/node'):
            match = re.fullmatch(r'v(\d{1,4})\.(\d{1,4})\.(\d{1,4})', node.parent.parent.name)
            if match and node.is_file():
                versions.append((tuple(map(int, match.groups())), node))
        if not versions:
            raise PolicyError('Install Node.js with npm (under ~/.nvm) to create npm environments.')
        node = max(versions)[1]
        package = node.parent.parent / 'lib' / 'node_modules' / 'npm'
        return {'node': node, 'npm_cli': package / 'bin' / 'npm-cli.js', 'npm_package': package}

    @staticmethod
    def _package_pin(path):
        real = Path(os.path.realpath(path))
        info = lstat(real)
        if info is None or not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o002:
            raise PolicyError('The npm installation folder is missing or not protected.')
        try:
            measured = tree(real, full_files=MAX_TOOL_TREE_FILES, max_files=MAX_TOOL_TREE_FILES)
            version = _json(real.joinpath('package.json').read_bytes()[:MAX_MANIFEST], 'npm package.json').get('version')
        except (OSError, AttributeError):
            raise PolicyError('The npm installation could not be pinned.') from None
        return {'path': str(path), 'realpath': str(real), 'tree_digest': measured['digest'], 'files': measured['files'],
                'bytes': measured['bytes'], 'dev': info.st_dev, 'ino': info.st_ino, 'uid': info.st_uid,
                'version': version if isinstance(version, str) and len(version) < 64 else None}

    def _pin(self, kind):
        paths = self._discover(kind)
        return {name: self._package_pin(path) if name == 'npm_package' else file_identity(path)
                for name, path in paths.items()}

    # Payload checks

    @staticmethod
    def _registries(kind, registries, loopback):
        pairs = allowlist(PUBLIC[kind] if registries is None else registries, loopback)
        if len(pairs) > MAX_REGISTRIES:
            raise PolicyError('Choose at most four registry hosts.')
        if not loopback and any(port != 443 for _, port in pairs):
            raise PolicyError('Registry egress is limited to HTTPS port 443.')
        return [[host, port] for host, port in pairs]

    @staticmethod
    def _ca(value):
        """Test-only trust anchor for local fixture registries; never model-exposed."""
        if value is None:
            return None
        info = lstat(value) if isinstance(value, str) and os.path.isabs(value) else None
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CA or os.path.realpath(value) != value:
            raise PolicyError('ca_file must be an absolute path to a bounded regular file.')
        return value

    @staticmethod
    def _limits(size_limit, timeout):
        if type(size_limit) is not int or not MIN_SIZE_LIMIT <= size_limit <= SIZE_LIMIT:
            raise PolicyError('Choose an environment size limit from 1 MB to 1 GiB.')
        if type(timeout) not in (int, float) or not 1 <= timeout <= TIMEOUT:
            raise PolicyError('Choose an install timeout from 1 to 600 seconds.')

    def _payload(self, payload):
        keys = set(payload) if isinstance(payload, dict) else set()
        if (not PAYLOAD <= keys or keys - PAYLOAD - TEST_ONLY - BROKER
                or payload.get('permission_mode', 'ask') not in ('ask', 'auto', 'full')):
            raise PolicyError('The environment install payload is malformed.')
        loopback = payload.get('loopback_targets', False)
        if (payload['operation'] != 'install_environment' or not isinstance(payload['kind'], str)
                or payload['kind'] not in FILES or type(loopback) is not bool or not isinstance(payload['chat_id'], str)
                or not isinstance(payload['environment_id'], str) or not IDENT.fullmatch(payload['environment_id'])
                or not isinstance(payload['root'], str) or not isinstance(payload['root_identity'], list)
                or len(payload['root_identity']) != 2 or any(type(v) is not int for v in payload['root_identity'])
                or any(not isinstance(payload[k], str) or not SHA256.fullmatch(payload[k]) for k in ('manifest_digest', 'lock_digest'))
                or not isinstance(payload['toolchain'], dict) or type(payload['packages']) is not int
                or payload['requires_review'] is not True or payload['network'] != 'allowlisted_registry_only'):
            raise PolicyError('The environment install payload is malformed.')
        if self._registries(payload['kind'], payload['registries'], loopback) != payload['registries']:
            raise PolicyError('The environment install payload is malformed.')
        self._limits(payload['size_limit'], payload['timeout'])
        self._ca(payload.get('ca_file'))
        return payload

    # Public API

    def plan(self, chat_id, ws, kind, registries=None, *, size_limit=SIZE_LIMIT, timeout=TIMEOUT, ca_file=None,
             loopback_targets=False):
        """Immutable review data for one install; no effects.

        registries defaults to the kind's public hosts on 443. size_limit,
        timeout, ca_file and loopback_targets are host-only (fixtures); models
        never choose registries.
        """
        self._permit(*self.required_permissions)
        self._supported()
        if kind not in FILES:
            raise PolicyError('Choose environment kind npm-ci or pip-hashes.')
        if type(loopback_targets) is not bool:
            raise PolicyError('loopback_targets must be a boolean.')
        pairs = self._registries(kind, registries, loopback_targets)
        self._limits(size_limit, timeout)
        ca_file = self._ca(ca_file)
        ws = self._workspace(chat_id, ws)
        self._overlap(ws.root)
        if lstat(self.anchor) is not None:
            self._anchor_check()
        self._capacity(chat_id)
        facts = self._inspect(ws, kind, pairs)
        toolchain = self._pin(kind)
        ident = uuid.uuid4().hex
        path = self.anchor / ident
        manifest_name, lock_name = FILES[kind]
        hosts = ', '.join(authority(host, port) for host, port in pairs)
        if kind == 'npm-ci':
            command = 'npm ci --ignore-scripts (no lifecycle scripts)'
            summary = f'Install {facts["packages"]} npm packages from package-lock.json into a managed environment.'
        else:
            command = 'pip install --require-hashes --no-deps --only-binary=:all: into a new venv (no setup.py)'
            summary = f'Install {facts["packages"]} hash-pinned Python wheels from requirements.txt into a managed environment.'
        files = ' and '.join(dict.fromkeys((manifest_name, lock_name)))
        payload = {'operation': 'install_environment', 'kind': kind, 'environment_id': ident, 'chat_id': chat_id,
                   'root': str(ws.root), 'root_identity': list(ws.identity), 'manifest_digest': facts['manifest_digest'],
                   'lock_digest': facts['lock_digest'], 'toolchain': toolchain, 'registries': pairs,
                   'packages': facts['packages'], 'size_limit': size_limit, 'timeout': timeout, 'requires_review': True,
                   'network': 'allowlisted_registry_only',
                   'effects': [f'Create folder {path}', f'Copy {files} into it',
                               f'Download packages only from {hosts} through a host egress broker',
                               f'Run {command} under the macOS sandbox, writing only inside that folder'],
                   'summary': summary}
        if ca_file is not None:
            payload['ca_file'] = ca_file
        if loopback_targets:
            payload['loopback_targets'] = True
        return payload

    def install(self, payload, cancel):
        """Run one reviewed install exactly once and return its receipt.

        Refusals before any effect raise PolicyError and register nothing.
        A finished installer settles `ready` or `failed` (a failed folder is
        removed). Cancellation, timeout or any ambiguity after a process
        started settles `uncertain` and raises InstallOutcomeUncertain.
        """
        payload = self._payload(payload)
        self._permit(*self.required_permissions)
        self._supported()
        if not self.installing.acquire(blocking=False):
            raise PolicyError('Another managed environment install is running. Try again when it finishes.')
        try:
            return self._install(payload, cancel)
        finally:
            self.installing.release()

    def _install(self, p, cancel):
        ident, kind, chat_id = p['environment_id'], p['kind'], p['chat_id']
        try:
            ws = Workspace(p['root'])
        except OSError:
            raise PolicyError('The reviewed project folder is unavailable.') from None
        if str(ws.root) != p['root'] or list(ws.identity) != p['root_identity']:
            raise PolicyError('The reviewed project folder was replaced before install.')
        ws = self._workspace(chat_id, ws)
        facts = self._inspect(ws, kind, p['registries'])
        if (facts['manifest_digest'], facts['lock_digest'], facts['packages']) != (p['manifest_digest'], p['lock_digest'], p['packages']):
            raise PolicyError('The project manifest or lockfile changed after review. Plan the install again.')
        toolchain = self._pin(kind)
        if toolchain != p['toolchain']:
            raise PolicyError('The pinned toolchain changed after review. Plan the install again.')
        if cancel.is_set() or self.closed:
            raise PolicyError('Stopped before the install started. Nothing was changed.')
        self._overlap(ws.root)
        self._ensure_anchor()
        if self._rows('SELECT 1 FROM project_environments WHERE id=?', (ident,)):
            raise PolicyError('This reviewed install was already attempted. Plan a new one.')
        self._capacity(chat_id)
        path = self.anchor / ident
        if os.path.lexists(path):
            raise PolicyError('The reviewed environment folder already exists. Plan a new install.')
        now = time.time()
        # Registration precedes the first effect; restart never replays it.
        self._write('INSERT INTO project_environments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (ident, chat_id, p['root'], canonical(p['root_identity']), kind, p['manifest_digest'], p['lock_digest'],
                     canonical(toolchain), canonical(p['registries']), 'installing', None, None,
                     canonical({'phase': 'registered before the folder was created'}), now, now))
        try:
            os.mkdir(path, 0o700)
        except OSError as exc:
            self._mark(ident, 'failed', {'phase': 'folder not created', 'error': message(exc)})
            raise PolicyError('The environment folder could not be created. Nothing was installed.') from None
        evidence = {}
        try:
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise PolicyError('The new environment folder did not verify.')
            evidence['dir_identity'] = [info.st_dev, info.st_ino]
            self._mark(ident, 'installing', {**evidence, 'phase': 'folder created; installer not started'})
            for name in ('work', 'home', 'tmp', 'cache'):
                os.mkdir(path / name, 0o700)
            for name, data in facts['files'].items():
                fd = os.open(path / 'work' / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as out:
                    out.write(data)
        except Exception as exc:
            if 'dir_identity' not in evidence:
                self._mark(ident, 'uncertain', {'phase': 'folder created but not verified', 'error': message(exc),
                                                'note': 'Nothing was deleted. Inspect it natively.'})
                raise
            self._settle_failed(ident, path, evidence, 'preparation_failed', {'error': message(exc)})
            raise
        return self._execute(p, ident, path, evidence, toolchain, cancel)

    def _execute(self, p, ident, path, evidence, toolchain, cancel):
        broker, ran, live, outputs, truncated = None, 0, False, [], False
        try:
            broker = EgressBroker(p['registries'], loopback_targets=p.get('loopback_targets', False),
                                  max_bytes=min(EGRESS_BYTES, p['size_limit'] + EGRESS_SLACK),
                                  lifetime_seconds=min(3600, p['timeout'] + 30))
            with self.lock:
                if self.closed:
                    raise _NotStarted()
                self.brokers.add(broker)
            broker.start()
            deadline = time.monotonic() + p['timeout']
            result = None
            for label, argv, env, reads, executables in self._steps(p, path, toolchain, broker):
                if label == 'pip install':
                    self._check_venv(path, toolchain)
                profile = self._profile(path, reads, executables, broker.port)
                live = True
                result = self._run(argv, path / 'work', env, profile, cancel, deadline, path, p['size_limit'])
                live, ran = False, ran + 1
                truncated = truncated or result['output_truncated']
                outputs.append(f'$ {label}\n{result["output"]}')
                if result['overflow'] or result['code'] != 0:
                    break
        except _NotStarted:
            # Every earlier step was observed to its end and nothing is running.
            self._settle_failed(ident, path, evidence, 'stopped_between_steps' if ran else 'stopped_before_start',
                                {'output': _tail(outputs), 'egress_log': broker.log() if broker else []})
            raise PolicyError('The install stopped before an installer step started. Nothing is running and the folder was removed.') from None
        except _Unobserved as exc:
            outputs.append(exc.output)
            self._settle_uncertain(ident, evidence, exc.reason, outputs, broker)
            raise InstallOutcomeUncertain(f'The installer was stopped ({exc.reason}) before its outcome was observed. '
                                          'The environment is marked uncertain and nothing was retried.') from None
        except Exception as exc:
            if not live:
                self._settle_failed(ident, path, evidence, 'step_refused' if ran else 'not_started',
                                    {'error': message(exc), 'output': _tail(outputs),
                                     'egress_log': broker.log() if broker else []})
                raise
            self._settle_uncertain(ident, evidence, message(exc), outputs, broker)
            raise InstallOutcomeUncertain('The install failed in an unobserved way after the installer started. '
                                          'The environment is marked uncertain and nothing was retried.') from exc
        finally:
            if broker is not None:
                broker.close()
                with self.lock:
                    self.brokers.discard(broker)
        egress_log = broker.log()
        combined = '\n'.join(item for item in outputs if item)
        common = {'exit_code': result['code'], 'output': combined[-OUTPUT_CHARS:],
                  'output_truncated': truncated or len(combined) > OUTPUT_CHARS, 'egress_log': egress_log}
        if result['overflow']:
            return self._settle_failed(ident, path, evidence, 'size_limit', common)
        if result['code'] != 0:
            return self._settle_failed(ident, path, evidence, 'installer_failed', common)
        try:
            measured = tree(path)
            issue = self._audit(path, p['kind'], toolchain, measured)
        except PolicyError as exc:
            return self._settle_failed(ident, path, evidence, 'invalid_tree', {**common, 'error': str(exc)})
        except OSError as exc:
            self._settle_uncertain(ident, evidence, message(exc), outputs, None, egress_log)
            raise InstallOutcomeUncertain('The installed environment could not be measured. It is marked uncertain.') from None
        if measured['bytes'] > p['size_limit']:
            return self._settle_failed(ident, path, evidence, 'size_limit', {**common, 'size_bytes': measured['bytes'],
                                                                             'file_count': measured['files']})
        if issue:
            return self._settle_failed(ident, path, evidence, 'invalid_tree', {**common, 'error': issue})
        settled = {**evidence, **common, 'phase': 'installed and verified', 'size_bytes': measured['bytes'],
                   'file_count': measured['files'], 'tree_digest': measured['digest'], 'tree_partial': measured['partial']}
        self._mark(ident, 'ready', settled, measured['bytes'], measured['files'])
        return {'environment_id': ident, 'state': 'ready', 'kind': p['kind'], 'path': str(path), 'exit_code': result['code'],
                'output': settled['output'], 'output_truncated': settled['output_truncated'], 'egress_log': egress_log,
                'size_bytes': measured['bytes'], 'file_count': measured['files'], 'tree_digest': measured['digest'],
                'tree_partial': measured['partial'], 'lock_digest': p['lock_digest'], 'authorization': 'none'}

    def _settle_failed(self, ident, path, evidence, reason, details):
        removed, error = self._discard(path, evidence)
        record = {**evidence, **details, 'phase': 'failed', 'reason': reason, 'folder_removed': removed,
                  'note': 'Nothing is retried automatically.'}
        if error:
            record['removal_error'] = error
        self._mark(ident, 'failed', record, details.get('size_bytes'), details.get('file_count'))
        return {'environment_id': ident, 'state': 'failed', 'reason': reason, 'exit_code': details.get('exit_code'),
                'output': details.get('output', ''), 'egress_log': details.get('egress_log', []),
                'size_bytes': details.get('size_bytes'), 'file_count': details.get('file_count'),
                'folder_removed': removed, 'authorization': 'none'}

    def _settle_uncertain(self, ident, evidence, reason, outputs, broker, egress_log=None):
        try:
            log = broker.log() if broker is not None else egress_log or []
        except Exception:
            log = []
        self._mark(ident, 'uncertain', {**evidence, 'phase': 'installer stopped before its outcome was observed',
                                        'reason': reason, 'output': _tail(outputs), 'egress_log': log,
                                        'note': 'Nothing was retried. Inspect or remove this environment.'})

    def _discard(self, path, evidence):
        """Remove only this module's verified folder: exact anchor child, same inode."""
        try:
            self._anchor_check()
            info = lstat(path)
            if info is None:
                return True, None
            if (not stat.S_ISDIR(info.st_mode) or os.path.realpath(path) != str(path) or info.st_uid != os.getuid()
                    or evidence.get('dir_identity') != [info.st_dev, info.st_ino]):
                return False, 'The folder could not be verified as this environment; it was left in place.'
            if not shutil.rmtree.avoids_symlink_attacks:
                return False, 'This platform cannot delete folders without following links.'
            shutil.rmtree(path)
            return not os.path.lexists(path), None
        except (OSError, PolicyError) as exc:
            return False, message(exc)

    @staticmethod
    def _check_venv(path, toolchain):
        python = path / 'venv' / 'bin' / 'python3'
        if os.path.realpath(python) != toolchain['python']['realpath']:
            raise PolicyError('The new venv interpreter does not resolve to the pinned framework Python.')

    def _audit(self, path, kind, toolchain, measured):
        """Links must stay inside the folder, except venv interpreters bound to the pinned Python."""
        root = str(path)
        for rel, _ in measured['links']:
            resolved = os.path.realpath(path / rel)
            if resolved == root or resolved.startswith(root + os.sep):
                continue
            if kind == 'pip-hashes' and rel.startswith('venv/bin/') and resolved == toolchain['python']['realpath']:
                continue
            return 'The environment contains a link outside its folder.'
        if kind == 'pip-hashes':
            if os.path.realpath(path / 'venv' / 'bin' / 'python3') != toolchain['python']['realpath']:
                return 'The venv interpreter does not resolve to the pinned framework Python.'
            if self._venv_config(path) != str(Path(toolchain['python']['realpath']).parent):
                return 'pyvenv.cfg does not bind the pinned framework Python.'
        elif lstat(path / 'work' / 'node_modules') is None:
            return 'npm ci produced no node_modules folder.'
        return None

    @staticmethod
    def _venv_config(path):
        """pyvenv.cfg binds the venv to its base interpreter: `home` must be the pinned bin folder."""
        try:
            text = (path / 'venv' / 'pyvenv.cfg').read_text('utf-8')[:8192]
        except (OSError, UnicodeError):
            return None
        values = {}
        for line in text.splitlines():
            key, sep, value = line.partition('=')
            if sep:
                values[key.strip()] = value.strip()
        if values.get('include-system-site-packages', 'false').lower() != 'false':
            return None
        return values.get('home')

    def _steps(self, p, path, toolchain, broker):
        base = {'HOME': str(path / 'home'), 'TMPDIR': str(path / 'tmp'), 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8',
                'HTTPS_PROXY': broker.url, 'https_proxy': broker.url, 'NO_PROXY': '', 'no_proxy': ''}
        host, port = p['registries'][0]
        ca = p.get('ca_file')
        reads = [ca] if ca else []
        if p['kind'] == 'npm-ci':
            node, cli = toolchain['node']['realpath'], toolchain['npm_cli']['realpath']
            prefix = str(Path(toolchain['node']['path']).parent.parent)
            env = {**base, 'PATH': f'{Path(node).parent}:/usr/bin:/bin', 'NODE_OPTIONS': '', 'NO_UPDATE_NOTIFIER': '1',
                   'npm_config_registry': f'https://{authority(host, port)}/', 'npm_config_https_proxy': broker.url,
                   'npm_config_proxy': broker.url, 'npm_config_noproxy': '', 'npm_config_strict_ssl': 'true',
                   'npm_config_userconfig': str(path / 'home' / '.npmrc'), 'npm_config_globalconfig': '/dev/null',
                   'npm_config_cache': str(path / 'cache'), 'npm_config_ignore_scripts': 'true',
                   'npm_config_audit': 'false', 'npm_config_fund': 'false', 'npm_config_update_notifier': 'false',
                   'npm_config_replace_registry_host': 'never', 'npm_config_maxsockets': '4',
                   'npm_config_progress': 'false', 'npm_config_color': 'false', 'npm_config_offline': 'false',
                   'npm_config_prefer_offline': 'false', 'npm_config_fetch_retries': '1', 'npm_config_logs_max': '1'}
            if ca:
                env['npm_config_cafile'] = ca
            argv = [node, cli, 'ci', '--ignore-scripts', '--no-audit', '--no-fund', '--no-update-notifier',
                    '--cache', str(path / 'cache')]
            yield 'npm ci', argv, env, [prefix, *reads], [node]
            return
        python = toolchain['python']['realpath']
        venv = path / 'venv'
        env = {**base, 'PATH': f'{venv / "bin"}:/usr/bin:/bin', 'PYTHONNOUSERSITE': '1', 'PIP_CONFIG_FILE': os.devnull,
               'PIP_INDEX_URL': f'https://{authority(host, port)}/simple/', 'PIP_NO_INPUT': '1',
               'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PIP_RETRIES': '1', 'PIP_TIMEOUT': '60', 'PIP_PROGRESS_BAR': 'off'}
        yield ('python -m venv', [python, '-I', '-m', 'venv', '--without-pip', str(venv)], env, [str(FRAMEWORK), *reads],
               [str(FRAMEWORK)])
        # pip runs from the pinned bundled wheel itself, never from code the install could have placed.
        wheel = toolchain['pip_wheel']['realpath']
        argv = [str(venv / 'bin' / 'python3'), '-I', f'{wheel}/pip', 'install', '--no-input', '--disable-pip-version-check',
                '--require-hashes', '--no-deps', '--only-binary=:all:', '--no-cache-dir', '--use-deprecated=legacy-certs',
                *(['--cert', ca] if ca else []), '-r', str(path / 'work' / 'requirements.txt')]
        yield 'pip install', argv, env, [str(FRAMEWORK), *reads], [str(FRAMEWORK)]

    @staticmethod
    def _profile(path, reads, executables, port):
        quote = lambda value: json.dumps(str(value))
        ancestors = set()
        for item in [path, *reads, *executables]:
            ancestors.update(Path(item).parents)
        handles = ' '.join('(literal ' + quote(item) + ')' for item in sorted(ancestors, key=str))
        readable = ' '.join('(subpath ' + quote(item) + ')' for item in [path, *reads, *executables])
        runnable = ' '.join('(subpath ' + quote(item) + ')' for item in executables)
        # Toolchain, the environment folder and dyld only; no home, keychain or vault.
        # Network: the broker's loopback port alone; it admits only the reviewed registries.
        return f'''(version 1) (deny default)
      (allow process-fork) (allow process-exec {runnable})
      (allow sysctl-read) (allow file-read-metadata)
      (allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo") (global-name "com.apple.system.notification_center"))
      (allow file-read* {handles} {readable} (subpath "/System") (subpath "/usr/lib") (subpath "/usr/share")
        (subpath "/private/var/db/dyld") (subpath "/private/var/db/timezone") (literal "/private/etc/localtime")
        (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))
      (allow file-write* (subpath {quote(path)}) (literal "/dev/null"))
      {network_profile(port)}'''

    def _run(self, argv, cwd, env, profile, cancel, deadline, folder, size_limit):
        if cancel.is_set() or self.closed:
            raise _NotStarted()
        with self.lock:
            if self.closed:
                raise _NotStarted()
            process = subprocess.Popen([str(SANDBOX), '-p', profile, *map(str, argv)], cwd=str(cwd), env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            self.processes.add(process)
        kept, total, overflow = bytearray(), 0, False
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            check, exited_at = time.monotonic() + CHECK_SECONDS, None
            while True:
                if cancel.is_set() or self.closed:
                    raise _Unobserved('cancelled' if cancel.is_set() else 'service_closed')
                now = time.monotonic()
                if now > deadline:
                    raise _Unobserved('timeout')
                if now >= check:
                    check = now + CHECK_SECONDS
                    if _exceeds(folder, size_limit):
                        overflow = True
                        break
                if selector.get_map():
                    for key, _ in selector.select(timeout=0.05):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        kept += chunk
                        if len(kept) > MAX_OUTPUT:
                            del kept[:len(kept) - MAX_OUTPUT]
                else:
                    time.sleep(0.02)
                if _exited(process):
                    if not selector.get_map():
                        break
                    exited_at = exited_at or now
                    if now - exited_at > DRAIN_SECONDS:
                        raise _Unobserved('a descendant kept the installer output open after it exited')
            # close() kills live groups itself, so an exit seen after a stop is never an observed outcome.
            if cancel.is_set() or self.closed:
                raise _Unobserved('cancelled' if cancel.is_set() else 'service_closed')
        except _Unobserved as exc:
            exc.output = _text(bytes(kept))[0]
            raise
        finally:
            selector.close()
            # Leave the live set before reaping, so close() never signals a reaped group.
            with self.lock:
                self.processes.discard(process)
            _reap(process)
            process.stdout.close()
        text, cut = _text(bytes(kept))
        return {'code': None if overflow else process.returncode, 'overflow': overflow, 'output': text,
                'output_bytes': total, 'output_truncated': cut or total > len(kept)}

    def mount_spec(self, chat_id, env_id, ws):
        """Read-only mount data for the test runner, after full re-verification."""
        self._permit('files', 'tests')
        self._supported()
        row = self._row(chat_id, env_id)
        if row['state'] != 'ready':
            raise PolicyError('Only a ready managed environment can be mounted.')
        ws = self._workspace(chat_id, ws)
        if row['root'] != str(ws.root) or row['root_identity'] != list(ws.identity):
            raise PolicyError('This environment belongs to a different or replaced project folder.')
        lock_name = FILES[row['kind']][1]
        lock = self._read(ws, lock_name, MAX_LOCK if row['kind'] == 'npm-ci' else MAX_MANIFEST)
        if digest(lock) != row['lock_digest']:
            raise PolicyError(f'{lock_name} changed after this environment was installed. Install a fresh environment.')
        path = self.anchor / row['id']
        self._anchor_check()
        info = lstat(path)
        if (info is None or not stat.S_ISDIR(info.st_mode) or os.path.realpath(path) != str(path)
                or info.st_uid != os.getuid() or row['evidence'].get('dir_identity') != [info.st_dev, info.st_ino]):
            raise PolicyError('The managed environment folder is missing or was replaced.')
        try:
            measured = tree(path)
        except OSError:
            raise PolicyError('The managed environment could not be measured.') from None
        if measured['digest'] != row['evidence'].get('tree_digest') or self._audit(path, row['kind'], row['toolchain'], measured):
            raise PolicyError('The environment files changed after install. Remove it and install a fresh one.')
        # Exactly the recipe runner's mount fields (recipes.MOUNT_KEYS); the runner mounts them read-only.
        spec = {'environment_id': row['id'], 'root': str(path), 'tree_digest': measured['digest']}
        if row['kind'] == 'npm-ci':
            modules = path / 'work' / 'node_modules'
            return {**spec, 'kind': 'node_modules', 'reads': [str(path)], 'links': {'node_modules': str(modules)},
                    'executable': None, 'env': {'NODE_PATH': str(modules)}}
        # The venv python is a link; pyvenv.cfg `home` binds it to the pinned framework interpreter,
        # whose identity must still equal the one pinned at install.
        executable = file_identity(path / 'venv' / 'bin' / 'python3')
        if {k: v for k, v in executable.items() if k != 'path'} != {k: v for k, v in row['toolchain']['python'].items() if k != 'path'}:
            raise PolicyError('The framework Python behind this venv changed after install. Install a fresh environment.')
        return {**spec, 'kind': 'python-env', 'reads': [str(path), str(FRAMEWORK)], 'links': {},
                'executable': executable, 'env': {'VIRTUAL_ENV': str(path / 'venv')}}

    def remove(self, chat_id, env_id):
        """Delete one verified environment folder, then settle `removed`."""
        self._permit('files')
        with self.removing:
            row = self._row(chat_id, env_id)
            if row['state'] not in REMOVABLE:
                raise PolicyError('Only ready, failed or uncertain environments can be removed.')
            path = self.anchor / row['id']
            evidence = row['evidence']
            if lstat(self.anchor) is None or lstat(path) is None:
                self._mark(row['id'], 'removed', {**evidence, 'phase': 'removed', 'previous': row['state'],
                                                  'note': 'No folder remained.'}, row['size_bytes'], row['file_count'])
                return {'environment_id': row['id'], 'state': 'removed', 'deleted': False, 'authorization': 'none'}
            self._anchor_check()
            info = os.lstat(path)
            if (not stat.S_ISDIR(info.st_mode) or os.path.realpath(path) != str(path) or info.st_uid != os.getuid()
                    or evidence.get('dir_identity') != [info.st_dev, info.st_ino]):
                raise PolicyError('This folder cannot be verified as the environment Pebble created. It is never deleted; inspect it natively.')
            self._mark(row['id'], 'uncertain', {**evidence, 'phase': 'removal registered before deletion',
                                                'previous': row['state']}, row['size_bytes'], row['file_count'])
            removed, error = self._discard(path, evidence)
            if not removed:
                self._mark(row['id'], 'uncertain', {**evidence, 'phase': 'removal incomplete', 'previous': row['state'],
                                                    'error': error, 'note': 'Nothing is retried automatically.'})
                raise PolicyError('The environment folder could not be fully removed. It is marked uncertain.')
            self._mark(row['id'], 'removed', {**evidence, 'phase': 'removed', 'previous': row['state']},
                       row['size_bytes'], row['file_count'])
        return {'environment_id': row['id'], 'state': 'removed', 'deleted': True, 'authorization': 'none'}

    def mount_for(self, chat_id, ws, needs):
        """Host choice for RunTests: the newest ready environment of the kind a
        recipe needs, for this exact project root, whose lockfile and tree still
        verify. A stale or unverifiable environment is skipped, never repaired."""
        kind = {'node_modules': 'npm-ci', 'python-env': 'pip-hashes'}.get(needs)
        if kind is None:
            return None
        rows = self._rows("SELECT id FROM project_environments WHERE chat_id=? AND root=? AND kind=? AND state='ready' "
                          'ORDER BY created DESC LIMIT 8', (chat_id, str(ws.root), kind))
        for row in rows:
            try:
                return self.mount_spec(chat_id, row['id'], ws)
            except PolicyError:
                continue
        return None

    def list(self, chat_id):
        self._chat(chat_id)
        rows = self._rows('SELECT * FROM project_environments WHERE chat_id=? ORDER BY created DESC LIMIT 100', (chat_id,))
        return {'items': [self._public(row) for row in rows], 'authorization': 'none'}

    def assert_chat_deletable(self, chat_id):
        """Refuse while any owned environment is live or still has a folder on disk."""
        rows = self._rows('SELECT id,state FROM project_environments WHERE chat_id=?', (chat_id,))
        blocking = [row['id'] for row in rows if row['state'] in LIVE or self._present(row['id'])]
        if blocking:
            raise PolicyError(f'This conversation still owns {len(blocking)} managed environment(s) that are installing, '
                              'ready, uncertain or still on disk. Remove them before deleting it.')
        return {'deletable': True, 'settled_records': [row['id'] for row in rows], 'authorization': 'none'}

    def forget_chat_records(self, chat_id):
        """Delete only settled rows that own no folder."""
        settled = self.assert_chat_deletable(chat_id)['settled_records']
        for ident in settled:
            self._write("DELETE FROM project_environments WHERE id=? AND chat_id=? AND state IN ('failed','removed')",
                        (ident, chat_id))
        return {'forgotten': settled, 'authorization': 'none'}

    def close(self):
        with self.lock:
            self.closed = True
            brokers = list(self.brokers)
            # Members of this set are unreaped leaders, so their group IDs are still owned.
            for process in self.processes:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        for broker in brokers:
            broker.close()


def _tail(outputs):
    return '\n'.join(item for item in outputs if item)[-OUTPUT_CHARS:]
