"""Native-reviewed immutable plugin manifests. Integrity labels grant no authority.

No package code is extracted or executed. Dependency recipes describe existing,
explicitly hashed local files; provisioning remains a separate human operation.
"""

import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path

from .mcp_content import identifier
from .mcp_transport import endpoint, stdio_config
from .permissions import DEFAULT
from .policy import PRIVATE, PolicyError

MAX_BUNDLE = 96_000
MAX_DEPENDENCIES = 8
MAX_FILES = 24
MAX_HISTORY = 16
MAX_REVISION = 1_000_000_000
MAX_PREVIEWS = 4
REVIEW_SECONDS = 300
TRUST = 'unverified_digest_only'
RUNTIME_FIELDS = {'auth_binding', 'schemas', 'protocol', 'catalogue_digest',
                  'content_catalogue', 'content_digest'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'))


def sha(value):
    return hashlib.sha256(value).hexdigest()


def _complexity(value, depth=0, count=None):
    count = count if count is not None else [0]
    count[0] += 1
    if depth > 12 or count[0] > 2000:
        raise PolicyError('Plugin bundle exceeds its JSON complexity budget.')
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise PolicyError('Plugin bundle keys must be strings.')
        for child in value.values():
            _complexity(child, depth + 1, count)
    elif isinstance(value, list):
        for child in value:
            _complexity(child, depth + 1, count)
    elif value is not None and type(value) not in {str, int, float, bool}:
        raise PolicyError('Plugin bundle contains an unsupported JSON value.')


def _object_pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise PolicyError('Duplicate plugin bundle keys are not allowed.')
        value[key] = child
    return value


def _json(value):
    try:
        if isinstance(value, str):
            if len(value.encode()) > MAX_BUNDLE:
                raise ValueError()
            value = json.loads(value, object_pairs_hook=_object_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        _complexity(value)
        text = canonical(value)
        if len(text.encode()) > MAX_BUNDLE:
            raise ValueError()
        # A detached snapshot prevents callers from mutating an approved review.
        return json.loads(text)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise PolicyError('Use a complete bounded version 1 JSON plugin bundle.') from None


def _exact(value, keys, note):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise PolicyError(note)


def _text(value, limit, note, empty=False):
    if (not isinstance(value, str) or len(value) > limit or
            not empty and not value.strip() or any(ord(c) < 32 for c in value)):
        raise PolicyError(note)


def _absolute(value):
    if (not isinstance(value, str) or not value or len(value) > 1000 or
            '\\' in value or '%' in value or any(ord(c) < 32 for c in value)):
        raise PolicyError('Dependency files require a plain absolute local path.')
    path = Path(value)
    if not path.is_absolute() or '..' in value.split('/') or '.' in value.split('/'):
        raise PolicyError('Dependency traversal and relative paths are blocked.')
    if str(path) != value:
        raise PolicyError('Dependency paths must use their exact canonical spelling.')
    for part in path.parts[1:]:
        lower = part.lower()
        if (part.startswith('.') or lower in PRIVATE or lower in {'credentials', 'secrets'}
                or lower.endswith(('.pem', '.key', '.p12', '.pfx', '.gguf'))):
            raise PolicyError('Private files are outside plugin package access.')
    if path == Path.home() or path == Path(path.anchor):
        raise PolicyError('Choose a regular dependency file.')
    if any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)()
           for p in (path, *path.parents)):
        raise PolicyError('Dependency symlinks and junctions are blocked.')
    return path


def _read(path, limit, material=False):
    """Stream a hash through no-follow descriptors; detect concurrent edits."""
    path = _absolute(path)
    handles, file_fd = [], None
    try:
        if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'):
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(path.anchor, flags)
            handles.append(parent)
            for part in path.parts[1:-1]:
                parent = os.open(part, flags, dir_fd=parent)
                handles.append(parent)
            file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
        else:
            file_fd = os.open(path, os.O_RDONLY)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise PolicyError('Dependency is not regular or exceeds its file budget.')
        digest, size, parts = hashlib.sha256(), 0, []
        with os.fdopen(file_fd, 'rb') as source:
            file_fd = None
            while True:
                block = source.read(min(65536, limit + 1 - size))
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise PolicyError('Dependency exceeds its file budget.')
                digest.update(block)
                if material:
                    parts.append(block)
            after = os.fstat(source.fileno())
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after):
            raise PolicyError('Dependency changed while reading; preview it again.')
        # Reject replacement after the opened read as well.
        current = _absolute(str(path)).stat()
        if identity(before) != identity(current):
            raise PolicyError('Dependency changed while reading; preview it again.')
        return {'bytes': size, 'sha256': digest.hexdigest(), 'body': b''.join(parts) if material else None}
    except (OSError, ValueError) as error:
        if isinstance(error, PolicyError):
            raise
        raise PolicyError('A declared dependency file is missing or unreadable.') from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for handle in reversed(handles):
            os.close(handle)


def verify_bundle(value, expected=None):
    bundle = _json(value)
    _exact(bundle, {'version', 'manifest', 'dependencies', 'digest'}, 'Unsupported plugin bundle fields.')
    if type(bundle['version']) is not int or bundle['version'] != 1:
        raise PolicyError('Only version 1 plugin bundles are supported.')
    manifest = bundle['manifest']
    _exact(manifest, {'package_id', 'release', 'name', 'description', 'config'}, 'Unsupported plugin manifest fields.')
    if not isinstance(manifest['package_id'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}', manifest['package_id']):
        raise PolicyError('Use a bounded lowercase package identity.')
    if not isinstance(manifest['release'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+_-]{0,39}', manifest['release']):
        raise PolicyError('Use a bounded explicit release label.')
    _text(manifest['name'], 80, 'Use a bounded plugin name.')
    _text(manifest['description'], 1000, 'Use a bounded plugin description.', empty=True)
    config = manifest['config']
    if not isinstance(config, dict) or config.get('transport') not in {'http', 'stdio'}:
        raise PolicyError('Choose an explicit HTTP or stdio plugin transport.')
    common = {'transport', 'tools', 'resources', 'prompts', 'read_only'}
    _exact(config, common | ({'protocol_era'} if 'protocol_era' in config else set()) |
           ({'url'} if config['transport'] == 'http' else {'command', 'args', 'read_files'}),
           'Package configs require exact capabilities and transport fields; credentials and runtime reviews are forbidden.')
    if 'protocol_era' in config and config['protocol_era'] not in {'legacy', 'modern'}:
        raise PolicyError('Use the explicit legacy or modern MCP protocol era.')
    if type(config['read_only']) is not bool:
        raise PolicyError('read_only must be a boolean label; it does not authorize effects.')
    for key, kind in [('tools', 'tool'), ('resources', 'resource'), ('prompts', 'prompt')]:
        names = config[key]
        if (not isinstance(names, list) or len(names) > 8 or
                any(not isinstance(n, str) for n in names) or len(set(names)) != len(names)):
            raise PolicyError('Allowlist at most eight distinct capability identifiers.')
        for name in names:
            if kind == 'tool':
                if not re.fullmatch(r'[\w.-]{1,100}', name):
                    raise PolicyError('Use an exact tool allowlist name.')
            else:
                identifier(kind, name)
    if not any(config[key] for key in ('tools', 'resources', 'prompts')):
        raise PolicyError('A plugin must allowlist a tool, resource or prompt.')
    if config['transport'] == 'http':
        endpoint(config['url'])
    else:
        _absolute(config['command'])
        if (not isinstance(config['args'], list) or len(config['args']) > 16 or
                any(not isinstance(x, str) or len(x) > 1000 or '\x00' in x for x in config['args']) or
                not isinstance(config['read_files'], list) or len(config['read_files']) > 8 or
                any(not isinstance(x, str) for x in config['read_files']) or
                len(set(config['read_files'])) != len(config['read_files']) or config['command'] in config['read_files']):
            raise PolicyError('Use literal bounded args and distinct explicit launch files.')
        for name in config['read_files']:
            _absolute(name)
    recipes = bundle['dependencies']
    if not isinstance(recipes, list) or len(recipes) > MAX_DEPENDENCIES:
        raise PolicyError('Declare at most eight dependency recipes.')
    names, paths, total = set(), set(), 0
    for recipe in recipes:
        _exact(recipe, {'name', 'description', 'files'}, 'Dependencies contain only a name, description and exact file hashes.')
        _text(recipe['name'], 80, 'Use a bounded dependency name.')
        _text(recipe['description'], 1000, 'Use a bounded dependency recipe description.', empty=True)
        if recipe['name'] in names:
            raise PolicyError('Dependency recipe names must be distinct.')
        names.add(recipe['name'])
        if not isinstance(recipe['files'], list) or not 1 <= len(recipe['files']) <= 8:
            raise PolicyError('Each dependency recipe pins one to eight existing files.')
        for item in recipe['files']:
            _exact(item, {'path', 'bytes', 'sha256'}, 'Dependency files require exact path, byte count and SHA256.')
            _absolute(item['path'])
            limit = 100_000_000 if config['transport'] == 'stdio' and item['path'] == config['command'] else 96_000
            if (type(item['bytes']) is not int or not 0 <= item['bytes'] <= limit or
                    not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
                raise PolicyError('Use bounded dependency sizes and canonical SHA256 hashes.')
            if item['path'] in paths:
                raise PolicyError('Dependency files must be distinct across recipes.')
            paths.add(item['path']); total += item['bytes']
    if len(paths) > MAX_FILES or total > 100_000_000:
        raise PolicyError('Dependency recipes exceed their combined file budget.')
    if config['transport'] == 'stdio' and not {config['command'], *config['read_files']} <= paths:
        raise PolicyError('Every local executable and launch source requires an explicit dependency hash.')
    spec = {key: bundle[key] for key in ('version', 'manifest', 'dependencies')}
    calculated = sha(canonical(spec).encode())
    if bundle['digest'] != calculated or expected is not None and calculated != expected:
        raise PolicyError('Plugin bundle checksum changed; preview a complete reviewed version.')
    return bundle


def make_bundle(manifest, dependencies=None):
    """Fixture/authoring helper; validation never grants publisher trust."""
    value = _json({'version': 1, 'manifest': manifest, 'dependencies': dependencies or []})
    return verify_bundle({**value, 'digest': sha(canonical(value).encode())})


class Packages:
    def __init__(self, app):
        self.app, self.store = app, app.store
        self.lock, self.previews = threading.RLock(), {}
        self.boundary()
        with self.store.lock, self.store.db:
            self.store.db.execute('CREATE TABLE IF NOT EXISTS plugin_packages(plugin_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,revision INTEGER NOT NULL,digest TEXT NOT NULL)')
            self.store.db.execute('CREATE TABLE IF NOT EXISTS plugin_package_revisions(plugin_id TEXT,revision INTEGER,bundle TEXT NOT NULL,snapshot TEXT NOT NULL,source TEXT NOT NULL,operation TEXT NOT NULL,source_revision INTEGER,created REAL,PRIMARY KEY(plugin_id,revision))')

    def boundary(self):
        # Store.query/setting/get commits even SELECT. Check before every read.
        with self.store.lock:
            if self.store.db.in_transaction:
                raise PolicyError('Plugin package operations require an outside transaction boundary.')

    def _native(self):
        self.boundary()
        if self.app.active or getattr(self.app, 'closed', False):
            raise PolicyError('Stop the current task before changing plugin packages.')
        if not self.store.setting('permissions', DEFAULT)['tools']['plugins']:
            raise PolicyError('Plugin access is disabled in Permissions.')

    def _rows(self, sql, args=()):
        self.boundary()
        with self.store.lock:
            if self.store.db.in_transaction:
                raise PolicyError('Plugin package operations require an outside transaction boundary.')
            cur = self.store.db.execute(sql, args)
            names = [item[0] for item in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def _state(self, id):
        rows = self._rows('SELECT * FROM plugin_packages WHERE plugin_id=?', (id,))
        if not rows:
            raise PolicyError('Choose an installed managed plugin package.')
        state = rows[0]
        if type(state['revision']) is not int or not 1 <= state['revision'] <= MAX_REVISION:
            raise PolicyError('Stored plugin package revision is invalid.')
        rows = self._rows('SELECT * FROM plugin_package_revisions WHERE plugin_id=? AND revision=?', (id, state['revision']))
        if not rows:
            raise PolicyError('Stored plugin package history is missing.')
        record = self._record(rows[0], state['digest'])
        if record['bundle']['manifest']['package_id'] != state['package_id']:
            raise PolicyError('Stored plugin package identity changed.')
        return state, record

    def _record(self, row, expected=None):
        if (type(row['revision']) is not int or not 1 <= row['revision'] <= MAX_REVISION or
                row['operation'] not in {'install', 'update', 'rollback'} or
                (row['operation'] == 'rollback' and
                 (type(row['source_revision']) is not int or not 1 <= row['source_revision'] < row['revision'])) or
                (row['operation'] != 'rollback' and row['source_revision'] is not None)):
            raise PolicyError('Stored plugin revision metadata is invalid.')
        bundle = verify_bundle(row['bundle'], expected)
        snapshot = _json(row['snapshot'])
        _exact(snapshot, {'name', 'content', 'config'}, 'Stored plugin package snapshot is invalid.')
        manifest = bundle['manifest']
        if snapshot['name'] != manifest['name'] or snapshot['content'] != canonical(manifest['config']):
            raise PolicyError('Stored plugin package snapshot changed.')
        config = snapshot['config']
        if not isinstance(config, dict) or set(config) - set(manifest['config']) - {'launch_files', 'package_digest'}:
            raise PolicyError('Stored plugin package snapshot contains forbidden runtime state.')
        if config.get('package_digest') != bundle['digest']:
            raise PolicyError('Stored plugin package checksum binding changed.')
        if {k: v for k, v in config.items() if k not in {'launch_files', 'package_digest'}} != manifest['config']:
            raise PolicyError('Stored plugin package snapshot config changed.')
        expected_launch = {file['path']: file['sha256'] for recipe in bundle['dependencies'] for file in recipe['files']
                           if manifest['config']['transport'] == 'stdio' and file['path'] in [manifest['config']['command'], *manifest['config']['read_files']]}
        if config.get('launch_files', {}) != expected_launch:
            raise PolicyError('Stored plugin launch file hashes changed.')
        source = _json(row['source'])
        _exact(source, {'kind', 'label'}, 'Stored plugin source labels are invalid.')
        if source['kind'] not in {'native-paste', 'local-file'}:
            raise PolicyError('Stored plugin source kind is invalid.')
        _text(source['label'], 1000, 'Stored plugin source label is invalid.')
        return {**row, 'bundle': bundle, 'snapshot': snapshot, 'source': source}

    def _item(self, id):
        item = self.app.extensions.get(id)
        if item['kind'] != 'plugin':
            raise PolicyError('Choose an installed plugin.')
        return item

    def _base(self, id):
        state, record = self._state(id)
        item = self._item(id)
        self._matches(item, record['snapshot'])
        return sha(canonical({'item': {key: item[key] for key in ('id', 'kind', 'name', 'content', 'config', 'enabled', 'created')}, 'state': state}).encode())

    @staticmethod
    def _matches(item, snapshot):
        config = {key: value for key, value in item['config'].items() if key not in RUNTIME_FIELDS}
        if item['name'] != snapshot['name'] or item['content'] != snapshot['content'] or config != snapshot['config']:
            raise PolicyError('Installed plugin differs from its immutable package snapshot.')

    def _dependencies(self, bundle):
        config = bundle['manifest']['config']
        files = [file for recipe in bundle['dependencies'] for file in recipe['files']]
        if files and not self.store.setting('permissions', DEFAULT)['tools']['files']:
            raise PolicyError('Dependency inspection requires file access in Permissions.')
        for file in files:
            self._file_scope(file['path'])
            try:
                observed = _read(file['path'], 100_000_000 if config['transport'] == 'stdio' and file['path'] == config['command'] else 96_000)
            except PolicyError:
                raise PolicyError('Declared dependency is missing, blocked or unreadable: ' + file['path']) from None
            if observed['bytes'] != file['bytes'] or observed['sha256'] != file['sha256']:
                raise PolicyError('Declared dependency changed; preview a newly pinned package: ' + file['path'])
        return files

    def _file_scope(self, path):
        # App storage is private even when a test/custom installation uses a
        # non-hidden folder. User application/vault directories are not recipes.
        value = _absolute(path)
        blocked = [Path(self.app.home).absolute(), Path.home() / 'Library', Path.home() / 'AppData']
        if any(value.is_relative_to(root) for root in blocked):
            raise PolicyError('Private application storage is outside plugin package access.')

    def _compile(self, bundle):
        self._dependencies(bundle)
        manifest = bundle['manifest']
        config = dict(manifest['config'])
        if config['transport'] == 'stdio':
            policy = self.store.setting('permissions', DEFAULT)['tools']
            if not policy['files'] or not policy['terminal']:
                raise PolicyError('Local MCP packages require terminal and file access in Permissions.')
            config = stdio_config(config)
            expected = {file['path']: file['sha256'] for recipe in bundle['dependencies'] for file in recipe['files']}
            if any(expected.get(path) != digest for path, digest in config['launch_files'].items()):
                raise PolicyError('Local launch dependency changed during review.')
        config['package_digest'] = bundle['digest']
        return {'name': manifest['name'], 'content': canonical(manifest['config']), 'config': config}

    def _preview(self, bundle, source, id=None, operation=None, source_revision=None):
        self._native()
        bundle = verify_bundle(bundle)
        snapshot = self._compile(bundle)
        base = self._base(id) if id is not None else None
        if id is not None and self._state(id)[0]['package_id'] != bundle['manifest']['package_id']:
            raise PolicyError('Updates must retain the reviewed package identity.')
        if id is None and self._rows('SELECT 1 FROM plugin_packages WHERE package_id=?', (bundle['manifest']['package_id'],)):
            raise PolicyError('This package identity is already installed; review an update.')
        ident = uuid.uuid4().hex
        with self.lock:
            now = time.monotonic()
            self.previews = {key: value for key, value in self.previews.items() if value['deadline'] > now}
            if len(self.previews) >= MAX_PREVIEWS:
                raise PolicyError('At most four pending package reviews are retained.')
            self.previews[ident] = {'bundle': bundle, 'snapshot': snapshot, 'source': source, 'id': id,
                                    'base': base, 'operation': operation or ('update' if id else 'install'),
                                    'source_revision': source_revision, 'deadline': now + REVIEW_SECONDS}
        return json.loads(canonical({'preview_id': ident, 'plugin_id': id, 'digest': bundle['digest'], 'manifest': bundle['manifest'],
                'dependencies': bundle['dependencies'], 'source': source, 'trust': TRUST,
                'authorization': 'none', 'requires_native_review': True, 'expires_in': REVIEW_SECONDS,
                'will_install_disabled': True, 'operation': operation or ('update' if id else 'install'),
                'source_revision': source_revision}))

    def preview(self, bundle, id=None):
        with self.app.task_lock:
            return self._preview(bundle, {'kind': 'native-paste', 'label': 'Native JSON review'}, id)

    def import_bundle(self, path, id=None):
        with self.app.task_lock:
            self._native()
            if not self.store.setting('permissions', DEFAULT)['tools']['files']:
                raise PolicyError('Local bundle import requires file access in Permissions.')
            if not isinstance(path, str) or Path(path).suffix.lower() != '.json':
                raise PolicyError('Choose a real local JSON plugin bundle.')
            self._file_scope(path)
            body = _read(path, MAX_BUNDLE, material=True)['body']
            try:
                bundle = body.decode('utf-8')
            except UnicodeError:
                raise PolicyError('Plugin bundles require UTF-8 JSON.') from None
            return self._preview(bundle, {'kind': 'local-file', 'label': path}, id)

    def preview_rollback(self, id, revision):
        with self.app.task_lock:
            self._native()
            state, _ = self._state(id)
            if type(revision) is not int or not 1 <= revision < state['revision']:
                raise PolicyError('Choose an earlier retained package revision.')
            rows = self._rows('SELECT * FROM plugin_package_revisions WHERE plugin_id=? AND revision=?', (id, revision))
            if not rows:
                raise PolicyError('This rollback revision is unavailable.')
            record = self._record(rows[0])
            return self._preview(record['bundle'], record['source'], id, 'rollback', revision)

    def _consume(self, preview_id, id, operation):
        self._native()
        with self.lock:
            review = self.previews.pop(preview_id, None) if isinstance(preview_id, str) else None
        if (not review or review['deadline'] <= time.monotonic() or review['id'] != id or review['operation'] != operation):
            raise PolicyError('Package review expired or changed; preview it again.')
        bundle = verify_bundle(review['bundle'])
        if id is not None and self._base(id) != review['base']:
            raise PolicyError('Installed package changed after review; preview it again.')
        if self._compile(bundle) != review['snapshot']:
            raise PolicyError('Package dependencies changed after review.')
        return review

    def _write(self, id, review, revision, install=False):
        self.boundary()
        bundle, snapshot = review['bundle'], review['snapshot']
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Publish packages outside an application transaction.')
            with db:
                if install:
                    db.execute('INSERT INTO extensions VALUES(?,?,?,?,?,?,?)',
                               (id, 'plugin', snapshot['name'], snapshot['content'], canonical(snapshot['config']), 0, time.time()))
                else:
                    db.execute('UPDATE extensions SET name=?,content=?,config=?,enabled=0 WHERE id=?',
                               (snapshot['name'], snapshot['content'], canonical(snapshot['config']), id))
                db.execute('INSERT INTO plugin_package_revisions VALUES(?,?,?,?,?,?,?,?)',
                           (id, revision, canonical(bundle), canonical(snapshot), canonical(review['source']),
                            review['operation'], review['source_revision'], time.time()))
                db.execute('INSERT OR REPLACE INTO plugin_packages VALUES(?,?,?,?)',
                           (id, bundle['manifest']['package_id'], revision, bundle['digest']))
                # Retain current and previous snapshots, so reaching the storage
                # bound never makes the latest update irreversible.
                db.execute('DELETE FROM plugin_package_revisions WHERE plugin_id=? AND revision NOT IN (SELECT revision FROM plugin_package_revisions WHERE plugin_id=? ORDER BY revision DESC LIMIT ?)',
                           (id, id, MAX_HISTORY))
        return {'id': id, 'revision': revision, 'digest': bundle['digest'], 'enabled': False,
                'trust': TRUST, 'authorization': 'none'}

    def install(self, preview_id):
        with self.app.task_lock:
            review = self._consume(preview_id, None, 'install')
            if len(self.app.extensions.list()) >= 50:
                raise PolicyError('Remove an extension before adding more than 50.')
            if self._rows('SELECT 1 FROM plugin_packages WHERE package_id=?', (review['bundle']['manifest']['package_id'],)):
                raise PolicyError('This package identity is already installed.')
            return self._write(uuid.uuid4().hex, review, 1, install=True)

    def _replace(self, id, preview_id, operation):
        with self.app.task_lock:
            review = self._consume(preview_id, id, operation)
            state, old = self._state(id)
            if state['revision'] >= MAX_REVISION:
                raise PolicyError('Package revision counter reached its supported limit.')
            if operation == 'update' and old['bundle']['manifest']['release'] == review['bundle']['manifest']['release']:
                raise PolicyError('An update requires a different explicit release label.')
            auth = getattr(self.app, 'connector_auth', None)
            if auth is None:
                raise PolicyError('The host connector revocation boundary is unavailable.')
            # Old native sign-in reviews may have the same destination/capability
            # digest as a new release. Invalidate them as part of the update.
            with auth.lock:
                auth.previews = {key: value for key, value in auth.previews.items()
                                 if value['value'].get('plugin_id') != id}
                pending = bool(auth.pending and auth.pending['value'].get('plugin_id') == id)
            if pending:
                auth.cancel()
            # This host operation intentionally precedes publication and its
            # transaction. Failure may disable/revoke the old grant, but never
            # publishes new code with an old authenticated binding.
            auth.disconnect(id)
            metadata = auth.metadata(id)
            if metadata.get('state') != 'disconnected' or metadata.get('storage_cleanup_pending'):
                raise PolicyError('Retry secure connector cleanup before publishing this package.')
            self._native()
            self._matches(self._item(id), old['snapshot'])
            self._dependencies(review['bundle'])
            self.app.extensions.close_plugin(id)
            return self._write(id, review, state['revision'] + 1)

    def update(self, id, preview_id):
        return self._replace(id, preview_id, 'update')

    def rollback(self, id, preview_id):
        return self._replace(id, preview_id, 'rollback')

    def validate(self, id):
        """Gate native enablement AND every managed plugin effect/data request."""
        self.boundary()
        if not self._rows('SELECT 1 FROM plugin_packages WHERE plugin_id=?', (id,)):
            if self._item(id)['config'].get('package_digest'):
                raise PolicyError('Managed plugin package state is missing; reinstall a reviewed package.')
            return {'managed': False, 'authorization': 'none'}
        state, record = self._state(id)
        self._matches(self._item(id), record['snapshot'])
        self._dependencies(record['bundle'])
        return {'managed': True, 'revision': state['revision'], 'digest': state['digest'],
                'trust': TRUST, 'authorization': 'none'}

    def inspect(self, id):
        self.boundary()
        state, record = self._state(id)
        return {**state, 'manifest': record['bundle']['manifest'], 'dependencies': record['bundle']['dependencies'],
                'source': record['source'], 'trust': TRUST, 'authorization': 'none'}

    def history(self, id):
        self.boundary()
        self._state(id)
        rows = self._rows('SELECT * FROM plugin_package_revisions WHERE plugin_id=? ORDER BY revision DESC LIMIT ?', (id, MAX_HISTORY + 1))
        if len(rows) > MAX_HISTORY:
            raise PolicyError('Stored plugin history exceeds its revision budget.')
        records = [self._record(row) for row in rows]
        return [{'revision': row['revision'], 'release': row['bundle']['manifest']['release'], 'digest': row['bundle']['digest'],
                 'operation': row['operation'], 'source_revision': row['source_revision'], 'source': row['source'],
                 'created': row['created'], 'trust': TRUST, 'authorization': 'none'} for row in records]

    def forget(self, id):
        """Host cleanup after extension removal and successful grant cleanup."""
        with self.app.task_lock:
            self.boundary()
            if self.app.active:
                raise PolicyError('Stop the current task before removing package history.')
            if self._rows('SELECT 1 FROM extensions WHERE id=?', (id,)):
                raise PolicyError('Remove the extension through the host before removing its package history.')
            with self.store.lock, self.store.db:
                self.store.db.execute('DELETE FROM plugin_package_revisions WHERE plugin_id=?', (id,))
                self.store.db.execute('DELETE FROM plugin_packages WHERE plugin_id=?', (id,))
        return True

    def close(self):
        with self.lock:
            self.previews.clear()
