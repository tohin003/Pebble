"""settings.json: the person's settings as a readable, validated file (Phase 22, s22-storage).

Inventory. These are every person-facing setting that exists, with the validators the app already uses:
``theme``, ``reduced_motion``, ``memory_enabled``, ``memory_layers``, ``permissions``, ``companion``,
``notifications`` and ``stealth`` (App.setting; Stealth Lockdown, Phase 33), plus ``storage`` (the Documents copies, new in
Phase 22). Models, effort
and accounts stay with Providers and the Keychain; telemetry needs the consent notice the app shows;
connectors and skills are reviewed in the app; internal state (permissions_version, memory_initialized,
model_handoff_chats, notification_mutes, plan_credentials, recently_deleted...) never leaves the database.

The database's ``settings`` table stays the applied copy that the rest of Pebble reads. The file is the
person's source for it:
- The app changes a setting: Store.set_setting updates the file (atomically), once the first sync ran.
- The person edits the file: the next sync validates it. A change that narrows or keeps authority is
  applied through App.setting, so its side effects run (stopping commands when a tool group turns off).
  A change that widens permissions or authority waits as a pending confirmation that only the main
  window's Data & storage page can accept (host-owned authorization); until then the file shows the
  setting in effect.
- An invalid file (bad JSON, a wrong value) changes nothing: the last good settings stay, the error is
  shown in the app, and the file is left as the person wrote it. Unknown keys are kept.

Which side changed is decided by the file's last known digest (runtime/settings-state.json): a file
whose digest Pebble wrote or read last is followed by the database; any other file is an edit. Losing
that state is safe: the file is then treated as an edit and compared value by value.

Lock order: the sync lock guards only file I/O. Database values are read before it is taken, so a
thread holding Store.lock that calls Store.set_setting can never wait on a thread that holds the sync lock.
"""

import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path

from . import data_home

KEYS = ('theme', 'reduced_motion', 'memory_enabled', 'memory_layers', 'permissions', 'companion', 'notifications',
        'stealth', 'storage')
STORAGE = 'storage'
STORAGE_DEFAULT = {'documents_mirror': True, 'documents_folder': None}
LAYERS = ('working', 'episodic', 'semantic', 'procedural', 'temporal', 'profile')
THEMES = ('system', 'light', 'dark')
MODES = {'ask': 0, 'auto': 1, 'full': 2}
PREVIEWS = {'none': 0, 'title': 1, 'body': 2}
SCHEMA_REF = './settings.schema.json'
STATE = 'settings-state.json'  # in runtime/
STATE_FORMAT = 'pebble-settings-sync/1'
MAX_FILE = 256 * 1024
MAX_PENDING = 16
CONTROL = re.compile(r'[\x00-\x1f\x7f]')


# ---- Values ---------------------------------------------------------------------------------------


def default(key):
    if key == 'theme':
        return 'system'
    if key in ('reduced_motion', 'stealth'):
        return False
    if key == 'memory_enabled':
        return True
    if key == 'memory_layers':
        return {k: True for k in LAYERS}
    if key == 'permissions':
        from .permissions import DEFAULT, validate
        return validate(DEFAULT)
    if key == 'companion':
        from . import companion_settings
        return dict(companion_settings.DEFAULT)
    if key == 'notifications':
        from .notifications import DEFAULT_SETTINGS, validate_settings
        return validate_settings(DEFAULT_SETTINGS)
    if key == STORAGE:
        return dict(STORAGE_DEFAULT)
    raise KeyError(key)


def validate(key, value):
    """A fresh, normalized copy of one setting, or ValueError with a message the person can act on."""
    if key == 'theme':
        if value not in THEMES:
            raise ValueError('theme is "system", "light" or "dark".')
        return value
    if key in ('reduced_motion', 'memory_enabled', 'stealth'):
        if type(value) is not bool:
            raise ValueError(f'{key} is true or false.')
        return value
    if key == 'memory_layers':
        if not isinstance(value, dict) or set(value) != set(LAYERS) or any(type(v) is not bool for v in value.values()):
            raise ValueError('memory_layers needs true or false for each of: ' + ', '.join(LAYERS) + '.')
        return {k: value[k] for k in LAYERS}
    if key == 'permissions':
        from .permissions import validate as permissions
        return permissions(value)
    if key == 'companion':
        from . import companion_settings
        return companion_settings.validate(value)
    if key == 'notifications':
        from .notifications import validate_settings
        return validate_settings(value)
    if key == STORAGE:
        return validate_storage(value)
    raise ValueError(f'{key} is not a Pebble setting.')


def validate_storage(value):
    if not isinstance(value, dict) or set(value) != set(STORAGE_DEFAULT):
        raise ValueError('storage needs exactly documents_mirror (true or false) and documents_folder (a path or null).')
    if type(value['documents_mirror']) is not bool:
        raise ValueError('storage.documents_mirror is true or false.')
    folder = value['documents_folder']
    if folder is not None:
        if not isinstance(folder, str) or not folder or len(folder) > 1024 or CONTROL.search(folder):
            raise ValueError('storage.documents_folder is a full folder path or null.')
        expanded = os.path.expanduser(folder)
        if not os.path.isabs(expanded):
            raise ValueError('storage.documents_folder must be a full path, such as ~/Documents/Pebble.')
        if data_home.inside_protected(expanded):
            raise ValueError("storage.documents_folder cannot be inside Pebble's internal folders.")
        library = os.path.realpath(Path.home() / 'Library').casefold()
        target = os.path.realpath(expanded).casefold()
        if target == library or target.startswith(library + os.sep):
            raise ValueError('storage.documents_folder cannot be inside ~/Library.')
    return {'documents_mirror': value['documents_mirror'], 'documents_folder': folder}


def current(values, key):
    """The applied value from a raw database snapshot; an absent or unreadable value reads as its default."""
    raw = values.get(key)
    if raw is None:
        return default(key)
    try:
        return validate(key, raw)
    except ValueError:
        return default(key)


def applied(store):
    """Every setting as the database applies it (absent or unreadable values read as defaults)."""
    raw = snapshot(store)
    return {k: current(raw, k) for k in KEYS}


def snapshot(store):
    """Raw database values for KEYS, read with Store's own lock (never under the sync lock)."""
    marks = ','.join('?' * len(KEYS))
    rows = store.query(f'SELECT key,value FROM settings WHERE key IN ({marks})', KEYS)
    out = {}
    for row in rows:
        try:
            out[row['key']] = json.loads(row['value'])
        except (TypeError, ValueError):
            pass
    return out


def widening(key, old, new):
    """Plain reasons a change from `old` to `new` gives Pebble more access; empty when it does not."""
    reasons = []
    if key == 'permissions':
        if MODES[new['mode']] > MODES[old['mode']]:
            reasons.append(f'access changes from {old["mode"]} to {new["mode"]}')
        reasons += [f'the "{tool}" tool group turns on' for tool, on in new['tools'].items()
                    if on and not old['tools'].get(tool, False)]
    elif key == 'memory_enabled':
        if new and not old:
            reasons.append('memory turns on')
    elif key == 'memory_layers':
        reasons += [f'the "{layer}" memory layer turns on' for layer in LAYERS if new[layer] and not old[layer]]
    elif key == 'notifications':
        if new['enabled'] and not old['enabled']:
            reasons.append('notifications turn on')
        if PREVIEWS[new['preview']] > PREVIEWS[old['preview']]:
            reasons.append(f'notifications show more ({old["preview"]} to {new["preview"]})')
        reasons += [f'"{kind}" notifications turn on' for kind, on in new['kinds'].items()
                    if on and not old['kinds'].get(kind, False) and new['enabled']]
    elif key == STORAGE:
        if new['documents_mirror'] and not old['documents_mirror']:
            reasons.append('copies in Documents turn on')
        if new['documents_folder'] != old['documents_folder'] and new['documents_mirror']:
            reasons.append('copies go to ' + (new['documents_folder'] or 'the default ~/Documents/Pebble'))
    return reasons


# ---- The file -------------------------------------------------------------------------------------


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _strict(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f'the key "{key}" appears twice')
        seen[key] = value
    return seen


def _constant(token):
    raise ValueError(f'{token} is not allowed')


class Loaded:
    """One read of settings.json: the document, the validated values present in it, and what is wrong."""

    def __init__(self, exists=False, data=None, document=None, values=None, problems=None, error=None):
        self.exists, self.data, self.document = exists, data, document
        self.values, self.problems, self.error = values or {}, problems or {}, error
        self.digest = digest(data) if data is not None else None
        self.unknown = [k for k in (document or {}) if k not in KEYS and k != '$schema']

    @property
    def valid(self):
        return self.document is not None and not self.problems


def load(path):
    try:
        fd = os.open(path, os.O_RDONLY | data_home._NOFOLLOW | data_home._CLOEXEC)
    except FileNotFoundError:
        return Loaded()
    except OSError as error:
        return Loaded(True, b'', error=f'settings.json cannot be opened ({type(error).__name__}).')
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return Loaded(True, b'', error='settings.json is not a regular file.')
        with os.fdopen(fd, 'rb') as source:
            fd = None
            data = source.read(MAX_FILE + 1)
    finally:
        if fd is not None:
            os.close(fd)
    if len(data) > MAX_FILE:
        return Loaded(True, data, error='settings.json is larger than 256 KB.')
    try:
        document = json.loads(data.decode('utf-8'), object_pairs_hook=_strict, parse_constant=_constant)
    except UnicodeDecodeError:
        return Loaded(True, data, error='settings.json is not UTF-8 text.')
    except json.JSONDecodeError as error:
        return Loaded(True, data, error=f'settings.json is not valid JSON (line {error.lineno}, column {error.colno}: '
                                        f'{error.msg}).')
    except ValueError as error:
        return Loaded(True, data, error=f'settings.json is not valid: {error}.')
    if not isinstance(document, dict):
        return Loaded(True, data, error='settings.json must hold one JSON object.')
    values, problems = {}, {}
    for key in KEYS:
        if key in document:
            try:
                values[key] = validate(key, document[key])
            except ValueError as error:
                problems[key] = str(error)
    error = None
    if problems:
        error = 'settings.json has values Pebble cannot use, so it kept your last good settings: ' + ' '.join(
            f'{key}: {message}' for key, message in problems.items())
    return Loaded(True, data, document, values, problems, error)


def render(values, document=None):
    """settings.json text: $schema, the settings in a fixed order, then any unknown keys exactly as found."""
    out = {'$schema': SCHEMA_REF}
    for key in KEYS:
        out[key] = values[key]
    for key, value in (document or {}).items():
        if key not in out:
            out[key] = value
    return (json.dumps(out, indent=2, ensure_ascii=False) + '\n').encode()


def schema():
    """settings.schema.json (JSON Schema 2020-12). Unknown keys are allowed and kept."""
    flag = {'type': 'boolean'}
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': 'pebble-settings.schema.json',
        'title': 'Pebble settings',
        'description': 'Your Pebble settings. A change that gives Pebble more access waits for your confirmation in '
                       'Settings -> Data & storage. An invalid file changes nothing. Unknown keys are kept.',
        'type': 'object',
        'additionalProperties': True,
        'properties': {
            '$schema': {'type': 'string'},
            'theme': {'description': 'Colour theme.', 'enum': list(THEMES)},
            'reduced_motion': {**flag, 'description': 'Stop animations and the companion\'s eye movement.'},
            # Phase 33: privacy, not access. Turning it on gives Pebble nothing more, so it needs no confirmation.
            'stealth': {**flag, 'description': 'Stealth Lockdown: keep Pebble out of screen capture, screen sharing, the '
                                              'Dock and Cmd-Tab, and never take focus from the app in front. Not hidden '
                                              'from a camera, a capture card or a process list.'},
            'memory_enabled': {**flag, 'description': 'Whether Pebble remembers and recalls anything. Turning it on '
                                                       'needs your confirmation in the app.'},
            'memory_layers': {'description': 'Each memory layer on or off.', 'type': 'object',
                              'additionalProperties': False, 'required': list(LAYERS),
                              'properties': {k: flag for k in LAYERS}},
            'permissions': {'description': 'What Pebble may do. ask asks before every change; auto and full ask less.',
                            'type': 'object', 'additionalProperties': False, 'required': ['mode', 'tools'],
                            'properties': {'mode': {'enum': list(MODES)},
                                           'tools': {'type': 'object', 'additionalProperties': flag}}},
            'companion': {'description': 'Presentation only: pointer gaze, reactions, status words, UI guidance.',
                          'type': 'object', 'additionalProperties': False,
                          'required': ['pointer', 'reactions', 'status_words', 'ui_guidance'],
                          'properties': {'pointer': flag, 'reactions': flag, 'ui_guidance': flag,
                                         'status_words': {'enum': ['rotate', 'fixed', 'off']}}},
            'notifications': {'description': 'macOS notifications for scheduled work.', 'type': 'object',
                              'additionalProperties': False, 'required': ['enabled', 'kinds', 'preview', 'quiet_hours'],
                              'properties': {'enabled': flag, 'preview': {'enum': list(PREVIEWS)},
                                             'kinds': {'type': 'object', 'additionalProperties': flag},
                                             'quiet_hours': {'type': ['object', 'null'],
                                                             'properties': {'start': {'type': 'string'},
                                                                            'end': {'type': 'string'},
                                                                            'timezone': {'type': 'string'}}}}},
            'storage': {'description': 'Readable copies in your Documents folder.', 'type': 'object',
                        'additionalProperties': False, 'required': ['documents_mirror', 'documents_folder'],
                        'properties': {'documents_mirror': flag,
                                       'documents_folder': {'type': ['string', 'null'],
                                                            'description': 'null means ~/Documents/Pebble.'}}},
        },
    }


# ---- Sync ------------------------------------------------------------------------------------------


class SettingsSync:
    """Keeps settings.json and the database's settings in step for one structured home."""

    def __init__(self, root, store):
        self.root = Path(root)
        self.path = self.root / data_home.SETTINGS
        self.state_path = self.root / data_home.RUNTIME / STATE
        self.store = store
        self.lock = threading.RLock()
        self.ready = False
        self.error = self.write_error = None
        self.problems, self.unknown, self.waiting = {}, [], {}
        self.applied, self.own = [], []
        self.checked = None
        state = data_home.read_json(self.state_path) or {}
        self.known = state.get('digest') if isinstance(state.get('digest'), str) else None
        self.pending = [p for p in state.get('pending') or () if self._pending_ok(p)][:MAX_PENDING]

    @staticmethod
    def _pending_ok(item):
        return (isinstance(item, dict) and item.get('key') in KEYS and isinstance(item.get('id'), str)
                and isinstance(item.get('reasons'), list) and 'value' in item)

    def _save_state(self):
        try:
            data_home.private_dir(self.state_path.parent)
            data_home.write_json(self.state_path, {'format': STATE_FORMAT, 'digest': self.known, 'pending': self.pending})
        except OSError as error:
            self.write_error = type(error).__name__

    def _write(self, data):
        data_home.write_atomic(self.path, data)
        self.known = digest(data)
        self.write_error = None
        self._save_state()

    # Store.set_setting calls this after its database write. It reads the database before taking the sync lock
    # and never raises: the database write already happened, and the next sync reconciles any failure here.
    def written(self, key, value):
        if key not in KEYS or not self.ready:
            return
        try:
            database = applied(self.store)
            database[key] = validate(key, value)
        except Exception:
            return
        with self.lock:
            try:
                loaded = load(self.path)
                if loaded.exists and loaded.document is None:
                    return  # an unreadable file is the person's to fix; the next valid read reconciles
                if loaded.document is not None and loaded.digest != self.known:
                    # The person edited the file and the next sync has not read it yet: change only this key and
                    # keep the known digest, so their other edits are still compared value by value.
                    document = {k: v for k, v in loaded.document.items() if k != '$schema'}
                    document[key] = database[key]
                    data = (json.dumps({'$schema': SCHEMA_REF, **document}, indent=2, ensure_ascii=False) + '\n').encode()
                    data_home.write_atomic(self.path, data)
                    self.own = (self.own + [digest(data)])[-8:]  # ours: a sync in progress does not see a new edit
                else:
                    self._write(render(database, loaded.document))
            except OSError as error:
                self.write_error = type(error).__name__

    def sync(self, apply):
        """Startup (after App's upgrade completed) and every poll. `apply(key, value)` applies one change through
        the host; it raises ValueError while it cannot (a task is running), and the change is retried."""
        database = applied(self.store)
        with self.lock:
            loaded = load(self.path)
            self.checked = time.time()
            if not loaded.exists:
                self.error, self.problems, self.unknown = None, {}, []
                return self._finish(database, None, 'created')
            self.unknown = loaded.unknown
            if not loaded.valid:
                self.error, self.problems = loaded.error, loaded.problems
                self.ready = True
                return {'state': 'invalid', 'error': self.error}
            self.error, self.problems = None, {}
            if loaded.digest == self.known:
                # The file is as Pebble last wrote it: the database leads (App.setting('permissions') writes its
                # row directly, so this is also where that change reaches the file).
                if any(k not in loaded.values or loaded.values[k] != database[k] for k in KEYS):
                    return self._finish(database, loaded, 'rewritten')
                self.ready = True
                return {'state': 'unchanged'}
            changes, queued = [], []
            for key, value in loaded.values.items():
                if value == database[key]:
                    continue
                reasons = widening(key, database[key], value)
                if reasons:
                    queued.append(self._queue(key, value, reasons))
                else:
                    changes.append((key, value))
            seen = loaded.digest
        done, waiting = [], {}
        for key, value in changes:
            try:
                apply(key, value)
                done.append(key)
            except ValueError as error:
                waiting[key] = str(error)[:300]
        with self.lock:
            self.waiting = waiting
            self.applied = (self.applied + [{'key': k, 'at': time.time()} for k in done])[-16:]
            if waiting:
                self._save_state()
                self.ready = True
                return {'state': 'waiting', 'applied': done, 'pending': [q['id'] for q in queued], 'waiting': waiting}
        database = applied(self.store)
        with self.lock:
            now = load(self.path)
            if now.digest not in (seen, self.known) and now.digest not in self.own:
                self.ready = True
                return {'state': 'changed_again', 'applied': done}  # a newer edit: the next poll takes it
            return self._finish(database, now if now.document is not None else loaded, 'applied',
                                applied=done, pending=[q['id'] for q in queued])

    def _finish(self, database, loaded, state, **facts):
        try:
            self._write(render(database, loaded.document if loaded else None))
        except OSError as error:
            self.write_error = type(error).__name__
        self.ready = True
        return {'state': state, **facts}

    def _queue(self, key, value, reasons):
        same = next((p for p in self.pending if p['key'] == key and p['value'] == value), None)
        if same is not None:
            return same  # the same edit seen again keeps its id, so an open confirmation stays valid
        item = {'id': secrets.token_hex(8), 'key': key, 'value': value, 'reasons': reasons, 'created': time.time()}
        self.pending = [p for p in self.pending if p['key'] != key][-(MAX_PENDING - 1):] + [item]
        self._save_state()
        return item

    def resolve(self, ident, accept, apply):
        """The main window's answer to one pending change. Accepting applies it through the host."""
        with self.lock:
            item = next((p for p in self.pending if p['id'] == ident), None)
        if item is None:
            raise ValueError('That settings change is no longer waiting. Reopen Data & storage.')
        if accept:
            apply(item['key'], validate(item['key'], item['value']))
        with self.lock:
            self.pending = [p for p in self.pending if p['id'] != ident]
            self._save_state()
        return {'key': item['key'], 'applied': bool(accept)}

    def status(self):
        with self.lock:
            return {'path': str(self.path), 'schema': str(self.root / data_home.SCHEMA), 'ok': self.error is None,
                    'error': self.error, 'problems': dict(self.problems), 'unknown_keys': list(self.unknown)[:32],
                    'write_error': self.write_error, 'waiting': dict(self.waiting),
                    'pending': [{'id': p['id'], 'key': p['key'], 'reasons': list(p['reasons'])[:12],
                                 'created': p.get('created')} for p in self.pending],
                    'checked': self.checked}
