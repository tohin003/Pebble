"""Opt-in, local-only, bounded and redacted telemetry (Phase 18 E06; design study section 5.3).

Off by default. Only the native main-window routes (telemetry_get, telemetry_set,
telemetry_export, telemetry_clear; tool_authz.NATIVE_ONLY, never the pet
allowlist) can turn it on. Enabling records the consent text version, its digest
and the time; disabling deletes the queue and the state, so nothing remains.

There is no destination in Phase 18 and this module never opens a socket.
"Sharing" is a file the person saves through the native Save dialog: the shell
hands the backend a single-use destination token and the parent wires
`Telemetry.claim_destination` to resolve it, so a path never comes from the
webview. Network upload needs a person-chosen destination (Phase 20).

Schema pebble.telemetry/1 is an allowlist of typed, bounded fields; an unknown
or missing key refuses the whole event. There is no free text anywhere: no
prompts, model output, file contents, paths, URLs, memory text, account, route
fingerprint or credentials. Tool names are catalogue names; any other name is
collapsed to 'unlisted'. Durations and token counts are log2 buckets and the
time is the hour. The install ID is random, rotates on every enable and clear,
and is written only into an export's header.

Bounds: at most MAX_EVENTS events and MAX_BYTES of queue; the oldest are dropped
and counted. Events older than RETENTION_SECONDS are deleted. The queue is
<home>/telemetry/queue.jsonl (0600, folder 0700, outside pebble.sqlite), so it
is not in upgrade backups (pebble.sqlite only) or the memory archive, and no
schema epoch changes.
"""

import hashlib
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path

SCHEMA = 'pebble.telemetry/1'
EXPORT_SCHEMA = 'pebble.telemetry_export/1'
CONSENT_VERSION = 'pebble.telemetry.consent/1'
CONSENT_TEXT = (
    'Pebble can keep a private usage log on this Mac to help measure its reliability. It records only counts '
    'and rounded values: how each task ended, how many steps it took, which built-in tools it used and how '
    'often, approvals and questions, and rounded durations, token counts and memory or CPU use. It never '
    'records your messages, the model\'s replies, files, memory notes, web addresses, account details or '
    'credentials. Nothing is sent anywhere: you can save the log to a file you choose and share it yourself. '
    'At most 1,000 events or 256 KB are kept, for 30 days. Turning this off deletes the log.')
CONSENT_SHA256 = hashlib.sha256(CONSENT_TEXT.encode()).hexdigest()
DESTINATION = None                     # Phase 18: no network destination exists
ROUTES = ('telemetry_get', 'telemetry_set', 'telemetry_export', 'telemetry_clear')
MAX_EVENTS = 1000
MAX_BYTES = 256 * 1024
MAX_EVENT_BYTES = 8 * 1024
RETENTION_SECONDS = 30 * 86400
DIRECTORY, QUEUE, STATE = 'telemetry', 'queue.jsonl', 'state.json'
LOG2_MAX = 48
MAX_COUNT = 10_000
HOUR_MAX = 10 ** 7                     # hours since the epoch; about year 3100
CPU_BUCKETS = (0, 1, 2, 5, 10, 25, 50, 100, 200, 400, 800)
TOKEN = re.compile(r'[A-Za-z0-9_-]{16,256}')
INSTALL_ID = re.compile(r'[0-9a-f]{32}')
UNLISTED = 'unlisted'
# Literal copies, so the schema never changes silently; tests pin them to runtime.STATES.
RUN_STATES = ('budget_exhausted', 'completed', 'failed', 'interrupted', 'needs_reconciliation', 'paused', 'queued',
              'retry_wait', 'running', 'stopped', 'verification_required', 'waiting_approval', 'waiting_input')
ERROR_CODES = ('provider_auth', 'provider_unavailable', 'provider_rate_limited', 'provider_retries_exhausted',
               'run_failed', 'compaction_stalled', 'tool_refused', 'store_unavailable', 'sampler_unavailable',
               'internal')
ENVELOPE = frozenset({'schema', 'kind', 'hour', 'data'})
FIELDS = {
    'run_settled': {
        'state': {'type': 'enum', 'values': RUN_STATES},
        'steps': {'type': 'count'},
        'tools': {'type': 'tool_counts', 'names': 'catalogue tool names; any other name counts as ' + UNLISTED},
        'approvals': {'type': 'count'},
        'rejections': {'type': 'count'},
        'questions': {'type': 'count'},
        'uncertain': {'type': 'count'},
        'wall_ms_log2': {'type': 'log2'},
        'model_ms_log2': {'type': 'log2'},
        'input_tokens_log2': {'type': 'log2'},
        'output_tokens_log2': {'type': 'log2'},
    },
    'resource_sample': {
        'footprint_mb_log2': {'type': 'log2'},
        'cpu_percent_bucket': {'type': 'enum', 'values': CPU_BUCKETS},
    },
    'error': {
        'code': {'type': 'enum', 'values': ERROR_CODES},
    },
}
# observe_run's input: counts and raw numbers the host already holds. Any other key refuses the record.
RECORD_KEYS = frozenset({'state', 'steps', 'tools', 'approvals', 'rejections', 'questions', 'uncertain',
                         'wall_seconds', 'model_seconds', 'input_tokens', 'output_tokens'})
APPROVED = frozenset({'applied', 'running', 'failed', 'undone'})   # actions a person approved, whatever followed

_tool_names = None


class Refused(ValueError):
    """A telemetry event outside the schema. str() is a fixed reason code, never the offending value."""


def tool_names():
    """Catalogue and agent-effect tool names, plus 'unlisted' (computed once)."""
    global _tool_names
    if _tool_names is None:
        from .tool_catalogue import TOOLS
        from .agent_effects import SCHEMAS
        _tool_names = frozenset({t['function']['name'] for t in TOOLS} | {t['function']['name'] for t in SCHEMAS}
                                | {UNLISTED})
    return _tool_names


def log2_bucket(value):
    """None stays None; below 1 is 0; otherwise b with 2**(b-1) <= value < 2**b, capped at LOG2_MAX."""
    if value is None:
        return None
    if type(value) not in (int, float) or value != value or value < 0:
        raise Refused('bucket_value')
    if value < 1:
        return 0
    return min(LOG2_MAX, int(math.floor(math.log2(value))) + 1)


def cpu_bucket(percent):
    """The largest CPU_BUCKETS bound at or below `percent` (of one core)."""
    if type(percent) not in (int, float) or percent != percent or percent < 0:
        raise Refused('bucket_value')
    return max(b for b in CPU_BUCKETS if b <= percent)


def _count(value):
    if type(value) is not int or value < 0:
        raise Refused('count_value')
    return min(value, MAX_COUNT)


def _field(spec, value):
    kind = spec['type']
    if kind == 'enum':
        # Exact type match too: True is not 1 and 1.0 is not 1.
        if not any(type(value) is type(v) and value == v for v in spec['values']):
            raise Refused('enum_value')
    elif kind == 'count':
        if type(value) is not int or not 0 <= value <= MAX_COUNT:
            raise Refused('count_value')
    elif kind == 'log2':
        if value is not None and (type(value) is not int or not 0 <= value <= LOG2_MAX):
            raise Refused('log2_value')
    elif kind == 'tool_counts':
        names = tool_names()
        if type(value) is not dict or len(value) > len(names):
            raise Refused('tool_counts')
        for name, count in value.items():
            if type(name) is not str or name not in names:
                raise Refused('tool_name')
            if type(count) is not int or not 1 <= count <= MAX_COUNT:
                raise Refused('tool_count')
    else:  # pragma: no cover - FIELDS is fixed above
        raise Refused('field_type')


def validate_event(event):
    """Return a canonical copy of a pebble.telemetry/1 event, or raise Refused with a fixed reason code."""
    if type(event) is not dict or set(event) != ENVELOPE:
        raise Refused('envelope_keys')
    if event['schema'] != SCHEMA:
        raise Refused('schema')
    kind = event['kind']
    if type(kind) is not str or kind not in FIELDS:
        raise Refused('kind')
    if type(event['hour']) is not int or not 0 <= event['hour'] <= HOUR_MAX:
        raise Refused('hour')
    data, fields = event['data'], FIELDS[kind]
    if type(data) is not dict or set(data) != set(fields):
        raise Refused('data_keys')
    for key, spec in fields.items():
        _field(spec, data[key])
    canonical = {'schema': SCHEMA, 'kind': kind, 'hour': event['hour'],
                 'data': {key: (dict(sorted(data[key].items())) if type(data[key]) is dict else data[key])
                          for key in sorted(fields)}}
    if len(encode(canonical)) > MAX_EVENT_BYTES:
        raise Refused('event_size')
    return canonical


def encode(event):
    return json.dumps(event, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def run_data(record):
    """A run_settled data object from observe_run's record (RECORD_KEYS only)."""
    if type(record) is not dict or not set(record) <= RECORD_KEYS or 'state' not in record:
        raise Refused('record_keys')
    tools = record.get('tools') or {}
    if type(tools) in (list, tuple):
        names = list(tools)
        counted = {}
        for name in names:
            counted[name] = counted.get(name, 0) + 1
        tools = counted
    if type(tools) is not dict:
        raise Refused('tool_counts')
    known, collapsed = tool_names(), {}
    for name, count in tools.items():
        key = name if type(name) is str and name in known else UNLISTED
        collapsed[key] = min(MAX_COUNT, collapsed.get(key, 0) + _count(count))
    collapsed = {k: v for k, v in collapsed.items() if v}
    milliseconds = lambda key: None if record.get(key) is None else _nonnegative(record[key]) * 1000
    return {
        'state': record['state'],
        'steps': _count(record.get('steps', 0)),
        'tools': collapsed,
        'approvals': _count(record.get('approvals', 0)),
        'rejections': _count(record.get('rejections', 0)),
        'questions': _count(record.get('questions', 0)),
        'uncertain': _count(record.get('uncertain', 0)),
        'wall_ms_log2': log2_bucket(milliseconds('wall_seconds')),
        'model_ms_log2': log2_bucket(milliseconds('model_seconds')),
        'input_tokens_log2': log2_bucket(_tokens(record.get('input_tokens'))),
        'output_tokens_log2': log2_bucket(_tokens(record.get('output_tokens'))),
    }


def _nonnegative(value):
    if type(value) not in (int, float) or value != value or value < 0 or value == math.inf:
        raise Refused('number_value')
    return value


def _tokens(value):
    if value is not None and (type(value) is not int or value < 0):
        raise Refused('token_value')
    return value


def run_record(app, run_id, *, usage=None, wall_seconds=None):
    """Counts for one settled run, read from host records only (never text, arguments or results).

    For the parent's settlement hook: `usage` is the runner's usage dict (tokens and
    generation_seconds) and `wall_seconds` its elapsed time; both are optional.
    """
    run = app.runs.get(run_id)
    tools = app.store.query('SELECT name,state FROM run_tools WHERE run_id=?', (run_id,))
    actions = app.store.query('SELECT a.status FROM actions a JOIN run_action_owners o ON o.action_id=a.id '
                              'WHERE o.run_id=?', (run_id,))
    questions = app.store.query('SELECT COUNT(*) AS n FROM run_questions WHERE run_id=?', (run_id,))
    usage = usage if isinstance(usage, dict) else {}
    tokens = lambda key: usage.get(key) if type(usage.get(key)) is int and usage.get(key) >= 0 else None
    generation = usage.get('generation_seconds')
    elapsed = wall_seconds if wall_seconds is not None else run.get('elapsed')
    return {
        'state': run['state'], 'steps': int(run.get('steps') or 0),
        'tools': [row['name'] for row in tools],
        'approvals': sum(1 for row in actions if row['status'] in APPROVED),
        'rejections': sum(1 for row in actions if row['status'] == 'rejected'),
        'questions': int(questions[0]['n']) if questions else 0,
        'uncertain': sum(1 for row in tools if row['state'] == 'uncertain'),
        'wall_seconds': float(elapsed) if type(elapsed) in (int, float) and elapsed >= 0 else None,
        'model_seconds': float(generation) if type(generation) in (int, float) and generation >= 0 else None,
        'input_tokens': tokens('input_tokens'), 'output_tokens': tokens('output_tokens'),
    }


class Telemetry:
    """The person's opt-in telemetry queue for one Pebble home. Thread-safe; never opens a socket."""

    def __init__(self, home, *, clock=time.time):
        self.home = Path(home)
        self.root = self.home / DIRECTORY
        self.clock = clock
        self.lock = threading.Lock()
        # Parent wiring: resolves a single-use native Save-dialog token to a new file path, or raises.
        self.claim_destination = None

    # --- public API ------------------------------------------------------------------------

    def status(self):
        with self.lock:
            state = self._state()
            events = self._events(state) if state else []
            return self._status(state, events)

    def enable(self, consent_version):
        if consent_version != CONSENT_VERSION:
            raise ValueError('Review the current telemetry notice before turning it on.')
        with self.lock:
            state = self._state()
            if not state:
                self._folder(create=True)
                state = {'schema': SCHEMA, 'consent_version': CONSENT_VERSION, 'consent_sha256': CONSENT_SHA256,
                         'consented_at': int(self.clock()), 'install_id': secrets.token_hex(16),
                         'dropped': 0, 'expired': 0, 'refused': 0}
                self._write(QUEUE, b'')
                self._save(state)
            return self._status(state, self._events(state))

    def disable(self):
        """Turn telemetry off and delete everything it kept."""
        with self.lock:
            folder = self._folder(create=False)
            if folder is not None:
                for name in (QUEUE, STATE):
                    try:
                        os.unlink(folder / name)
                    except FileNotFoundError:
                        pass
                for leftover in folder.glob('.*.tmp'):
                    try:
                        os.unlink(leftover)
                    except OSError:
                        pass
                try:
                    os.rmdir(folder)
                except OSError:
                    pass  # something we did not create stays; it is never read
            return self._status(None, [])

    def clear(self):
        """Delete queued events and rotate the install ID; telemetry stays on if it was on."""
        with self.lock:
            state = self._state()
            if not state:
                return self._status(None, [])
            state.update(install_id=secrets.token_hex(16), dropped=0, expired=0, refused=0)
            self._write(QUEUE, b'')
            self._save(state)
            return self._status(state, [])

    def observe_run(self, record):
        """Queue one run_settled event when on; a no-op returning False when off.

        `record` is a dict of RECORD_KEYS, or a zero-argument callable returning one that is called only when
        telemetry is on (the settlement hook passes `lambda: run_record(...)`, so an install with telemetry off
        does no database work). Refused input returns False and is counted; an error raised by the callable
        itself propagates to the caller.
        """
        return self._observe('run_settled', lambda: run_data(record() if callable(record) else record))

    def observe_resource(self, footprint_bytes, cpu_percent):
        return self._observe('resource_sample', lambda: {
            'footprint_mb_log2': log2_bucket(None if footprint_bytes is None else _nonnegative(footprint_bytes) / 2 ** 20),
            'cpu_percent_bucket': cpu_bucket(cpu_percent)})

    def observe_error(self, code):
        return self._observe('error', lambda: {'code': code})

    def export(self, destination_token):
        """Save the queue to the file the person chose in the native Save dialog. Never uploads anything."""
        if type(destination_token) is not str or not TOKEN.fullmatch(destination_token):
            raise ValueError('Choose where to save the telemetry file with the Save dialog.')
        with self.lock:
            state = self._state()
            if not state:
                raise ValueError('Telemetry is off, so there is nothing to export.')
            if self.claim_destination is None:
                raise ValueError('Saving telemetry needs the native Save dialog, which is not connected yet.')
            events = self._events(state)
            target = Path(self.claim_destination(destination_token))
            if not target.is_absolute() or not target.parent.is_dir() or target.parent.is_symlink():
                raise ValueError('The chosen folder does not exist.')
            document = {
                'schema': EXPORT_SCHEMA, 'telemetry_schema': SCHEMA,
                'consent_version': state['consent_version'], 'consent_sha256': state['consent_sha256'],
                'consented_at': state['consented_at'], 'exported_at': int(self.clock()),
                'install_id': state['install_id'], 'events': events,
                'counts': {'events': len(events), 'dropped': state['dropped'], 'expired': state['expired'],
                           'refused': state['refused']},
                'limits': self._limits(), 'destination': DESTINATION, 'network_upload': False,
            }
            data = (json.dumps(document, sort_keys=True, indent=1, ensure_ascii=True) + '\n').encode()
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            except FileExistsError:
                raise ValueError('That file already exists; choose a new name. Nothing was overwritten.') from None
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            digest = hashlib.sha256(data).hexdigest()
            try:
                readback = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(readback, 'rb') as stream:
                    verified = hashlib.sha256(stream.read(len(data) + 1)).hexdigest() == digest
            except OSError:
                verified = False
            return {'path': str(target), 'sha256': digest, 'bytes': len(data), 'events': len(events),
                    'verified': verified, 'published': False, 'network_upload': False}

    # --- internals -------------------------------------------------------------------------

    def _observe(self, kind, build):
        with self.lock:
            try:
                state = self._state()
            except (OSError, ValueError):
                return False
            if not state:
                return False
            try:
                event = validate_event({'schema': SCHEMA, 'kind': kind, 'hour': int(self.clock() // 3600),
                                        'data': build()})
            except Refused:
                state['refused'] = min(MAX_COUNT, state['refused'] + 1)
                self._quiet_save(state)
                return False
            try:
                events = self._events(state)
                events.append(event)
                self._store(state, events)
            except (OSError, ValueError):
                return False
            return True

    def _limits(self):
        return {'max_events': MAX_EVENTS, 'max_bytes': MAX_BYTES, 'retention_days': RETENTION_SECONDS // 86400}

    def _status(self, state, events):
        kinds = {kind: 0 for kind in FIELDS}
        for event in events:
            kinds[event['kind']] += 1
        return {
            'schema': SCHEMA, 'enabled': bool(state),
            'consent_version': state['consent_version'] if state else None,
            'consented_at': state['consented_at'] if state else None,
            'current_consent_version': CONSENT_VERSION, 'consent_text': CONSENT_TEXT,
            'consent_sha256': CONSENT_SHA256,
            'install_id': state['install_id'] if state else None,
            'events': len(events), 'bytes': sum(len(encode(e)) + 1 for e in events), 'kinds': kinds,
            'dropped': state['dropped'] if state else 0, 'expired': state['expired'] if state else 0,
            'refused': state['refused'] if state else 0,
            'fields': {kind: sorted(fields) for kind, fields in FIELDS.items()},
            'limits': self._limits(), 'destination': DESTINATION, 'network_upload': False,
            'export': 'native_save_dialog',
        }

    def _folder(self, create):
        """The private telemetry folder, or None when absent. A link, file or shared folder is refused."""
        try:
            info = os.lstat(self.root)
        except FileNotFoundError:
            if not create:
                return None
            if not self.home.is_dir():
                raise ValueError('The Pebble data folder is missing.')
            os.mkdir(self.root, 0o700)
            info = os.lstat(self.root)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('The telemetry folder is not private to this user; telemetry stays off.')
        return self.root

    def _read(self, name, limit):
        folder = self._folder(create=False)
        if folder is None:
            return None
        try:
            fd = os.open(folder / name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            return None
        except OSError:  # a link (ELOOP) or an unreadable replacement
            raise ValueError('A telemetry file was replaced; telemetry stays off.') from None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('A telemetry file was replaced; telemetry stays off.')
            return stream.read(limit + 1)

    def _state(self):
        """The current consent record, or None (off). A stale consent or damaged record is off."""
        data = self._read(STATE, 4096)
        if data is None:
            return None
        try:
            state = json.loads(data)
        except ValueError:
            return None
        valid = (type(state) is dict and state.get('schema') == SCHEMA
                 and state.get('consent_version') == CONSENT_VERSION and state.get('consent_sha256') == CONSENT_SHA256
                 and type(state.get('consented_at')) is int and type(state.get('install_id')) is str
                 and INSTALL_ID.fullmatch(state['install_id'])
                 and all(type(state.get(k)) is int and state[k] >= 0 for k in ('dropped', 'expired', 'refused')))
        return state if valid else None

    def _events(self, state):
        """Valid, unexpired queued events, oldest first. Invalid lines are dropped and counted as refused."""
        data = self._read(QUEUE, 2 * MAX_BYTES)
        if not data:
            return []
        if len(data) > 2 * MAX_BYTES:
            state['refused'] = min(MAX_COUNT, state['refused'] + 1)
            return []
        now_hour, events = int(self.clock() // 3600), []
        for line in data.split(b'\n'):
            if not line:
                continue
            try:
                event = validate_event(json.loads(line))
            except (ValueError, RecursionError):
                state['refused'] = min(MAX_COUNT, state['refused'] + 1)
                continue
            if (now_hour - event['hour']) * 3600 > RETENTION_SECONDS:
                state['expired'] = min(MAX_COUNT, state['expired'] + 1)
                continue
            events.append(event)
        return events

    def _store(self, state, events):
        lines = [encode(e) + b'\n' for e in events]
        while lines and (len(lines) > MAX_EVENTS or sum(map(len, lines)) > MAX_BYTES):
            lines.pop(0)
            state['dropped'] = min(MAX_COUNT, state['dropped'] + 1)
        self._write(QUEUE, b''.join(lines))
        self._save(state)

    def _save(self, state):
        self._write(STATE, json.dumps(state, sort_keys=True).encode())

    def _quiet_save(self, state):
        try:
            self._save(state)
        except (OSError, ValueError):
            pass

    def _write(self, name, data):
        folder = self._folder(create=False)
        if folder is None:
            raise ValueError('Telemetry is off.')
        temporary = folder / ('.' + name + '.' + secrets.token_hex(8) + '.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, folder / name)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
