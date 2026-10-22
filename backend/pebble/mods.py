"""Native-reviewed, pinned data-only mods; no model installation or host RPC.

Workers receive bounded ordinary JSON and return validated native panes. Their
source is never evaluated in the application process or generated HTML.
"""

from contextlib import contextmanager
import hashlib
import json
import re
import threading
import time
import uuid

from . import mod_worker
from .artifact_schema import json_object, validate_native_title
from .artifacts import text, validate
from .interaction import SECRET
from .policy import PolicyError
from .runtime import STATES


MAX_MODS, MAX_PANES, MAX_DISPATCHES = 8, 4, 12
_DIGEST = re.compile(r'^[a-f0-9]{64}$')
_PANE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
_TOOL = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
_MOD_COLUMNS = 'id,name,manifest,digest,enabled,grant_digest,error,created,updated,grant_revision'
_REASONS = frozenset({'invalid', 'output', 'timeout', 'memory', 'memory-monitor', 'runtime', 'unsupported',
                      'invalid_artifacts', 'revoked', 'owner', 'revision', 'publication', 'cancelled', 'interrupted'})


class _PublicationFailure(PolicyError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('Mod publication failed its ' + reason + ' boundary.')


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(',', ':'))


def _pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError('Duplicate JSON keys.')
        value[key] = child
    return value


def manifest(value):
    """Validate an exact v1 manifest without executing its source."""
    try:
        if isinstance(value, str):
            if len(value.encode()) > mod_worker.INPUT_BYTES:
                raise ValueError()
            value = json.loads(value, object_pairs_hook=_pairs)
        if type(value) is not dict or set(value) != {'version', 'name', 'events', 'capabilities', 'source'}:
            raise ValueError()
        if type(value['version']) is not int or value['version'] != 1:
            raise ValueError()
        name, source, events = value['name'], value['source'], value['events']
        if (type(name) is not str or not name.strip() or len(name) > 80
                or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in name) or SECRET.search(name)
                or type(source) is not str or SECRET.search(source)
                or type(events) is not list or not 1 <= len(events) <= 4
                or any(type(event) is not str or event not in mod_worker.EVENTS for event in events)
                or len(set(events)) != len(events) or type(value['capabilities']) is not list
                or value['capabilities'] != ['ui.render']):
            raise ValueError()
        value = {'version': 1, 'name': name.strip(), 'events': sorted(events),
                 'capabilities': ['ui.render'], 'source': source}
        mod_worker._packet(source, 'manual', {}, 2)
        if len(_encode(value).encode()) > mod_worker.INPUT_BYTES:
            raise ValueError()
        return json.loads(_encode(value))
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise PolicyError('Use an exact bounded version 1 mod manifest with only ui.render capability.') from None


def _hash(value):
    return hashlib.sha256(_encode(value).encode()).hexdigest()


def _payload(event, value):
    try:
        value = json_object(value)
        mod_worker._tree(value)
        if len(_encode(value).encode()) > mod_worker.PAYLOAD_BYTES or SECRET.search(_encode(value)):
            raise ValueError()
        if event == 'run.state':
            if set(value) != {'state', 'steps', 'elapsed'} or value['state'] not in STATES:
                raise ValueError()
            for key in ('steps', 'elapsed'):
                if type(value[key]) not in (int, float) or not 0 <= value[key] <= 1_000_000:
                    raise ValueError()
        elif event == 'context.summary':
            if set(value) - {'tokens', 'messages', 'sources', 'compactions', 'budget', 'truncated'}:
                raise ValueError()
            for key, count in value.items():
                if key == 'truncated':
                    if type(count) is not bool:
                        raise ValueError()
                elif type(count) is not int or not 0 <= count <= 100_000_000:
                    raise ValueError()
        elif event == 'tool.result':
            if (set(value) != {'name', 'status', 'receipt_id'} or type(value['name']) is not str
                    or not _TOOL.fullmatch(value['name']) or value['status'] != 'completed'
                    or type(value['receipt_id']) is not str or not 1 <= len(value['receipt_id']) <= 350
                    or any(ord(c) < 32 for c in value['receipt_id'])):
                raise ValueError()
        elif event != 'manual':
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise PolicyError('Mod event input must be bounded ordinary data without credentials or raw host content.') from None


def validate_artifacts(specs):
    """Validate the entire worker batch before publishing any pane."""
    try:
        specs = mod_worker._panes(specs)
        if SECRET.search(_encode(specs)):
            raise ValueError()
        result = []
        for spec in specs:
            kind = spec['kind']
            if kind not in mod_worker.KINDS or kind in {'form', 'html', 'mcp_media'}:
                raise ValueError()
            # Mod dashboards are inline only: a host binding would read host data on the mod's behalf.
            if kind == 'dashboard' and any(type(p) is not dict or 'binding' in p for p in spec['data'].get('panels') or [None]):
                raise ValueError()
            # validate() accepts only the self-check (v1) quiz shape; a mod never gets a host-held key.
            data = validate(kind, spec['data'])
            title = validate_native_title(kind, text(spec['title'], 120))
            result.append({'id': spec['id'], 'title': title, 'kind': kind, 'data': data})
        return result
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
        raise PolicyError('Mod returned invalid declarative artifacts; no panes were published.') from None


class Mods:
    def __init__(self, app):
        self.app, self.store, self.artifacts = app, app.store, app.artifacts
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS mods_meta(version INTEGER NOT NULL)')
            db.execute('INSERT INTO mods_meta SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM mods_meta)')
            if db.execute('SELECT version FROM mods_meta').fetchall() != [(1,)]:
                raise PolicyError('This mod registry requires a compatible Pebble version.')
            db.execute('''CREATE TABLE IF NOT EXISTS mods(
                id TEXT PRIMARY KEY,name TEXT NOT NULL,manifest TEXT NOT NULL,digest TEXT NOT NULL,
                enabled INTEGER NOT NULL,grant_digest TEXT,error TEXT,created REAL NOT NULL,updated REAL NOT NULL,
                grant_revision INTEGER NOT NULL DEFAULT 0)''')
            if 'grant_revision' not in {row[1] for row in db.execute('PRAGMA table_info(mods)').fetchall()}:
                db.execute('ALTER TABLE mods ADD COLUMN grant_revision INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE TABLE IF NOT EXISTS mod_reviews(digest TEXT PRIMARY KEY,manifest TEXT NOT NULL,created REAL NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS mod_dispatches(
                id TEXT PRIMARY KEY,run_id TEXT NOT NULL,chat_id TEXT NOT NULL,mod_id TEXT NOT NULL,
                digest TEXT NOT NULL,event TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,
                created REAL NOT NULL,updated REAL NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS mod_dispatches_run ON mod_dispatches(run_id,created)')
            db.execute('''CREATE TABLE IF NOT EXISTS mod_panes(
                mod_id TEXT NOT NULL,run_id TEXT NOT NULL,pane_id TEXT NOT NULL,chat_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,revision INTEGER NOT NULL,
                PRIMARY KEY(mod_id,run_id,pane_id))''')
            db.execute("UPDATE mod_dispatches SET status='interrupted',reason='interrupted',updated=? WHERE status='running'", (time.time(),))

    @contextmanager
    def _transaction(self):
        with self.store.lock:
            if self.store.db.in_transaction:
                raise PolicyError('Mod operations require an outside transaction boundary.')
            with self.store.db as db:
                db.execute('BEGIN IMMEDIATE')
                yield db

    @staticmethod
    def status():
        return {'available': mod_worker.supported(), 'platform': 'macos', 'runtime': 'JavaScriptCore',
                'max_mods': MAX_MODS, 'max_panes': MAX_PANES, 'max_dispatches_per_run': MAX_DISPATCHES,
                'source_bytes': mod_worker.SOURCE_BYTES, 'payload_bytes': mod_worker.PAYLOAD_BYTES,
                'packet_bytes': mod_worker.INPUT_BYTES, 'authorization': 'native_digest_pinned_ui_render_only'}

    @staticmethod
    def _record(row):
        if not row:
            raise PolicyError('Mod not found.')
        ident, name, raw, digest, enabled, grant, error, created, updated, grant_revision = row
        item = manifest(raw)
        if (digest != _hash(item) or name != item['name'] or enabled not in (0, 1)
                or enabled and grant != digest or error is not None and error not in _REASONS
                or type(grant_revision) is not int or grant_revision < 0):
            raise PolicyError('Mod manifest or native grant integrity check failed.')
        return {'id': ident, 'name': name, 'manifest': item, 'digest': digest, 'enabled': bool(enabled),
                'grant_digest': grant, 'error': error, 'runtime_available': mod_worker.supported(),
                'created': created, 'updated': updated, 'grant_revision': grant_revision}

    def preview(self, value):
        item = manifest(value)
        digest = _hash(item)
        with self._transaction() as db:
            db.execute('INSERT INTO mod_reviews VALUES(?,?,?) ON CONFLICT(digest) DO UPDATE SET manifest=excluded.manifest,created=excluded.created', (digest, _encode(item), time.time()))
            db.execute('DELETE FROM mod_reviews WHERE digest NOT IN (SELECT digest FROM mod_reviews ORDER BY created DESC,digest LIMIT 16)')
        return {'manifest': item, 'digest': digest, 'enabled': False, 'review': 'Full source and ui.render capability require native review.', **self.status()}

    def install(self, value, digest):
        item = manifest(value)
        if type(digest) is not str or not _DIGEST.fullmatch(digest) or digest != _hash(item):
            raise PolicyError('The installed mod must match the exact native preview digest.')
        with self._transaction() as db:
            review = db.execute('SELECT manifest FROM mod_reviews WHERE digest=?', (digest,)).fetchone()
            if not review or review[0] != _encode(item):
                raise PolicyError('Review the complete mod manifest before installing it.')
            existing = db.execute('SELECT ' + _MOD_COLUMNS + ' FROM mods WHERE digest=?', (digest,)).fetchone()
            if existing:
                return self._record(existing)
            if db.execute('SELECT count(*) FROM mods').fetchone()[0] >= MAX_MODS:
                raise PolicyError('At most eight mods may be installed.')
            ident, now = uuid.uuid4().hex, time.time()
            db.execute('INSERT INTO mods(' + _MOD_COLUMNS + ') VALUES(?,?,?,?,0,NULL,NULL,?,?,0)', (ident, item['name'], _encode(item), digest, now, now))
            return self._record(db.execute('SELECT ' + _MOD_COLUMNS + ' FROM mods WHERE id=?', (ident,)).fetchone())

    def toggle(self, ident, enabled, digest):
        if type(enabled) is not bool:
            raise PolicyError('Use a native boolean enable choice.')
        with self._transaction() as db:
            item = self._record(db.execute('SELECT ' + _MOD_COLUMNS + ' FROM mods WHERE id=?', (ident,)).fetchone())
            if digest != item['digest']:
                raise PolicyError('Review this mod\'s exact current digest before changing its grant.')
            if enabled and not mod_worker.supported():
                raise PolicyError('The confined mod runtime is available only on macOS; no fallback runtime is used.')
            db.execute('UPDATE mods SET enabled=?,grant_digest=?,error=NULL,updated=?,grant_revision=grant_revision+1 WHERE id=? AND digest=?',
                       (enabled, digest if enabled else None, time.time(), ident, digest))
            return self._record(db.execute('SELECT ' + _MOD_COLUMNS + ' FROM mods WHERE id=?', (ident,)).fetchone())

    def list(self):
        with self._transaction() as db:
            return [self._record(row) for row in db.execute('SELECT ' + _MOD_COLUMNS + ' FROM mods ORDER BY created,id').fetchall()]

    def inspect(self, ident):
        with self._transaction() as db:
            return self._record(db.execute('SELECT ' + _MOD_COLUMNS + ' FROM mods WHERE id=?', (ident,)).fetchone())

    def fingerprint(self):
        return sorted([{'id': item['id'], 'digest': item['digest'], 'grant_digest': item['grant_digest']}
                       for item in self.list() if item['enabled']], key=lambda item: item['id'])

    def remove(self, ident):
        with self._transaction() as db:
            item = self._record(db.execute('SELECT ' + _MOD_COLUMNS + ' FROM mods WHERE id=?', (ident,)).fetchone())
            db.execute('DELETE FROM mods WHERE id=?', (ident,))
            db.execute('DELETE FROM mod_panes WHERE mod_id=?', (ident,))
        return {'id': item['id'], 'status': 'removed'}

    def forget(self, cid):
        with self._transaction() as db:
            db.execute('DELETE FROM mod_panes WHERE chat_id=?', (cid,))
            db.execute('DELETE FROM mod_dispatches WHERE chat_id=?', (cid,))

    @staticmethod
    def example():
        return manifest({'version': 1, 'name': 'Context counts', 'events': ['manual', 'context.summary'],
                         'capabilities': ['ui.render'], 'source': '''function draw(p) {
  pebble.render({id:"context",title:"Context counts",kind:"table",data:{
    columns:[{id:"metric",label:"Metric"},{id:"count",label:"Count"}],
    rows:[{metric:"Tokens",count:p.tokens || 0},{metric:"Messages",count:p.messages || 0},
          {metric:"Sources",count:p.sources || 0},{metric:"Compactions",count:p.compactions || 0}]}});
}
pebble.on("manual",draw);
pebble.on("context.summary",draw);'''})

    def _eligible(self, db, cid, rid, event, mod_id=None):
        self.artifacts._owner(db, cid, rid)
        try:
            config = json.loads(db.execute('SELECT config FROM runs WHERE id=?', (rid,)).fetchone()[0])
            pins = config.get('mods', [])
            if type(pins) is not list:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise PolicyError('The run has no valid native mod grant snapshot.') from None
        records = db.execute('SELECT ' + _MOD_COLUMNS + ' FROM mods' + (' WHERE id=?' if mod_id else '') + ' ORDER BY created,id', (mod_id,) if mod_id else ()).fetchall()
        eligible = []
        for row in records:
            item = self._record(row)
            pin = {'id': item['id'], 'digest': item['digest'], 'grant_digest': item['digest']}
            if item['enabled'] and pin in pins and event in item['manifest']['events']:
                eligible.append(item)
        return eligible

    def _finish(self, ident, mod_id, digest, reason, status='failed'):
        reason = reason if reason in _REASONS else 'runtime'
        with self._transaction() as db:
            db.execute('UPDATE mod_dispatches SET status=?,reason=?,updated=? WHERE id=?', (status, reason, time.time(), ident))
            if reason in {'timeout', 'memory', 'memory-monitor'}:
                db.execute('UPDATE mods SET enabled=0,grant_digest=NULL,error=?,updated=?,grant_revision=grant_revision+1 WHERE id=? AND digest=?', (reason, time.time(), mod_id, digest))
            elif status != 'cancelled':
                db.execute('UPDATE mods SET error=?,updated=? WHERE id=? AND digest=?', (reason, time.time(), mod_id, digest))
        return {'id': ident, 'mod_id': mod_id, 'status': status, 'reason': reason}

    def _publish(self, cid, rid, plans, cancel):
        rows = []
        with self._transaction() as db:
            try:
                self.artifacts._owner(db, cid, rid)
            except ValueError:
                raise _PublicationFailure('owner') from None
            for admission, item, specs in plans:
                marker = db.execute('SELECT mod_id,digest,status FROM mod_dispatches WHERE id=? AND run_id=? AND chat_id=?',
                                    (admission['id'], rid, cid)).fetchone()
                if marker != (item['id'], item['digest'], 'running'):
                    raise _PublicationFailure('revoked')
                live = self._eligible(db, cid, rid, admission['event'], item['id'])
                if not live or live[0]['digest'] != item['digest'] or live[0]['grant_revision'] != item['grant_revision']:
                    raise _PublicationFailure('revoked')
                mappings = db.execute('SELECT pane_id,artifact_id,revision FROM mod_panes WHERE mod_id=? AND run_id=?', (item['id'], rid)).fetchall()
                known = {pane: (aid, revision) for pane, aid, revision in mappings}
                if len(set(known) | {spec['id'] for spec in specs}) > MAX_PANES:
                    raise PolicyError('Mod exceeded its four-pane limit.')
                for spec in specs:
                    if cancel.is_set():
                        raise InterruptedError('Mod cancelled before publication.')
                    evidence = self.artifacts._evidence(db, rid, spec['kind'], spec['data'], None)
                    now = time.time()
                    if spec['id'] in known:
                        aid, expected = known[spec['id']]
                        current = self.artifacts._read(db, cid, aid)[-1]
                        if (current['owner_run_id'] != rid or current['revision'] != expected
                                or current['status'] in {'stopped', 'expired'} or current['kind'] not in mod_worker.KINDS):
                            raise _PublicationFailure('revision')
                        if (current['title'] == spec['title'] and current['kind'] == spec['kind']
                                and current['data_hash'] == self.artifacts._digest(spec['data'])
                                and json.loads(current['evidence_ids']) == evidence):
                            rows.append(self.artifacts._public(current))
                            continue
                        if expected >= self.artifacts.MAX_REVISIONS - 1:
                            raise PolicyError('Mod pane reached its content revision limit.')
                        db.execute('UPDATE artifacts SET title=?,kind=?,data=? WHERE id=? AND chat_id=?',
                                   (spec['title'], spec['kind'], self.artifacts._encode(spec['data']), aid, cid))
                        row = self.artifacts._append(db, aid=aid, cid=cid, revision=expected + 1, title=spec['title'],
                               kind=spec['kind'], data=spec['data'], owner=rid, evidence=evidence, status='ready',
                               created=current['created'], updated=now, previous=current['record_hash'])
                        db.execute('UPDATE mod_panes SET revision=? WHERE mod_id=? AND run_id=? AND pane_id=? AND revision=?',
                                   (row['revision'], item['id'], rid, spec['id'], expected))
                    else:
                        if db.execute('SELECT count(*) FROM artifacts WHERE chat_id=?', (cid,)).fetchone()[0] >= self.artifacts.MAX_ARTIFACTS:
                            raise PolicyError('This conversation has reached its artifact limit.')
                        aid = uuid.uuid4().hex
                        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?)', (aid, cid, spec['title'], spec['kind'], self.artifacts._encode(spec['data']), now))
                        row = self.artifacts._append(db, aid=aid, cid=cid, revision=1, title=spec['title'], kind=spec['kind'],
                                                    data=spec['data'], owner=rid, evidence=evidence, status='ready', created=now, updated=now)
                        db.execute('INSERT INTO mod_panes VALUES(?,?,?,?,?,?)', (item['id'], rid, spec['id'], cid, aid, 1))
                    rows.append(row)
                db.execute("UPDATE mod_dispatches SET status='completed',reason=NULL,updated=? WHERE id=?", (time.time(), admission['id']))
                db.execute('UPDATE mods SET error=NULL,updated=? WHERE id=? AND digest=?', (time.time(), item['id'], item['digest']))
            if cancel.is_set():
                raise InterruptedError('Mod cancelled before publication.')
        return rows

    def dispatch(self, cid, rid, event, payload, mod_id=None, cancel=None):
        cancel = cancel if cancel is not None else threading.Event()
        if cancel.is_set():
            raise InterruptedError('Mod cancelled before dispatch.')
        payload = _payload(event, payload)
        with self._transaction() as db:
            eligible = self._eligible(db, cid, rid, event, mod_id)
        if cancel.is_set():
            raise InterruptedError('Mod cancelled before dispatch.')
        if not mod_worker.supported():
            return {'status': 'unsupported', 'artifacts': [], 'dispatches': []}
        plans, outcomes = [], []
        try:
            for item in eligible:
                if cancel.is_set():
                    raise InterruptedError('Mod cancelled during dispatch.')
                with self._transaction() as db:
                    live = self._eligible(db, cid, rid, event, item['id'])
                    if not live or live[0]['grant_revision'] != item['grant_revision']:
                        continue
                    if db.execute('SELECT count(*) FROM mod_dispatches WHERE run_id=?', (rid,)).fetchone()[0] >= MAX_DISPATCHES:
                        outcomes.append({'mod_id': item['id'], 'status': 'budget_exhausted', 'reason': 'dispatch_limit'})
                        break
                    ident, now = uuid.uuid4().hex, time.time()
                    db.execute('INSERT INTO mod_dispatches VALUES(?,?,?,?,?,?,\'running\',NULL,?,?)', (ident, rid, cid, item['id'], item['digest'], event, now, now))
                admission = {'id': ident, 'mod_id': item['id'], 'event': event, 'status': 'running'}
                try:
                    specs = validate_artifacts(mod_worker.run(item['manifest']['source'], event, payload, cancel=cancel))
                    if cancel.is_set():
                        raise InterruptedError('Mod cancelled after execution.')
                    plans.append((admission, item, specs))
                except InterruptedError:
                    outcomes.append(self._finish(ident, item['id'], item['digest'], 'cancelled', 'cancelled'))
                    raise
                except mod_worker.ModWorkerError as error:
                    outcomes.append(self._finish(ident, item['id'], item['digest'], error.reason))
                except Exception:
                    outcomes.append(self._finish(ident, item['id'], item['digest'], 'invalid_artifacts'))
            if cancel.is_set():
                raise InterruptedError('Mod cancelled before publication.')
            rows = self._publish(cid, rid, plans, cancel) if plans else []
            outcomes.extend({'id': admission['id'], 'mod_id': item['id'], 'status': 'completed', 'reason': None}
                            for admission, item, _ in plans)
        except InterruptedError:
            for admission, item, _ in plans:
                self._finish(admission['id'], item['id'], item['digest'], 'cancelled', 'cancelled')
            raise InterruptedError('Mod dispatch cancelled; no panes were published.') from None
        except Exception as error:
            rows = []
            for admission, item, _ in plans:
                reason = error.reason if isinstance(error, _PublicationFailure) else 'publication'
                outcomes.append(self._finish(admission['id'], item['id'], item['digest'], reason))
        status = 'completed' if rows or any(o['status'] == 'completed' for o in outcomes) else 'failed' if outcomes else 'disabled'
        return {'status': status, 'artifacts': rows, 'dispatches': outcomes}
