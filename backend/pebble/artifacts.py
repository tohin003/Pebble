"""Revisioned local artifacts; display data cannot change host permissions."""
from contextlib import contextmanager
import hashlib
import json
import math
import time
import uuid

from .artifact_schema import NATIVE_KINDS, evidence_ids as validate_evidence_ids, json_object, referenced_evidence, validate_native, validate_native_title, validate_quiz
from .artifact_schema import DIAGRAM_KEYS, DIAGRAM_NODE_KEYS, diagram_variant
from .interaction import RunInteractions


def text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError('Invalid interactive view text.')
    return value


def validate(kind, data):
    if not isinstance(kind, str):
        raise ValueError('Unsupported view type.')
    if kind in NATIVE_KINDS:
        return validate_native(kind, data)
    data = json_object(data)
    if kind == 'quiz':
        # Phase 21 (F21-quiz): the same accepted shapes and data; a refusal names each field path, never a value.
        data = validate_quiz(data)
    elif kind == 'diagram':
        # Phase 46 (1.9.29, w25-genui-b): a diagram may name its layout, direction and node layers, subtitles and tones
        # (artifact_schema.CATALOG_SCHEMAS); Phase 18 diagrams validate exactly as before.
        if not {'nodes', 'edges'} <= set(data) <= {'nodes', 'edges', *DIAGRAM_KEYS} or not isinstance(data['nodes'], list) or not 1 <= len(data['nodes']) <= 40 or not isinstance(data['edges'], list) or len(data['edges']) > 80:
            raise ValueError('Diagram needs 1–40 nodes and at most 80 edges.')
        ids = set()
        for n in data['nodes']:
            if not isinstance(n, dict) or not {'id', 'label'} <= set(n) <= {'id', 'label', *DIAGRAM_NODE_KEYS}:
                raise ValueError('Diagram nodes need an ID and label.')
            text(n['id'], 60)
            text(n['label'], 180)
            if n['id'] in ids:
                raise ValueError('Diagram node IDs must be unique.')
            ids.add(n['id'])
        for e in data['edges']:
            if not isinstance(e, dict) or set(e) != {'from', 'to', 'label'} or not isinstance(e['from'], str) or not isinstance(e['to'], str) or e['from'] not in ids or e['to'] not in ids or not isinstance(e['label'], str) or len(e['label']) > 100:
                raise ValueError('Diagram edges must connect existing nodes.')
        if set(data) - {'nodes', 'edges'} or any(set(n) - {'id', 'label'} for n in data['nodes']):
            data = diagram_variant(data)
    elif kind == 'html':
        if not set(data).issubset({'html', 'css', 'script', 'language'}) or 'html' not in data:
            raise ValueError('Custom view accepts html, css, script and language only.')
        text(data['html'], 60000)
        for key in ('css', 'script'):
            if key in data and (not isinstance(data[key], str) or len(data[key]) > 40000):
                raise ValueError('Custom view source is too large.')
        if data.get('language', 'javascript') not in ('javascript', 'typescript'):
            raise ValueError('Choose JavaScript or TypeScript for a custom view.')
    else:
        raise ValueError('Unsupported view type.')
    return data


class Artifacts:
    MAX_ARTIFACTS = 30
    MAX_REVISIONS = 16
    LIFECYCLES = frozenset({'draft', 'ready', 'review', 'stopped', 'expired'})
    _COLUMNS = ('artifact_id', 'chat_id', 'revision', 'title', 'kind', 'data',
                'owner_run_id', 'evidence_ids', 'status', 'created', 'updated',
                'data_hash', 'previous_hash', 'record_hash')

    def __init__(self, store):
        self.store = store
        with self._transaction() as db:
            # Keep this six-column compatibility table for host MCP publishers.
            db.execute('CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, chat_id TEXT, title TEXT, kind TEXT, data TEXT, created REAL)')
            db.execute('CREATE INDEX IF NOT EXISTS artifacts_chat ON artifacts(chat_id)')
            db.execute('CREATE TABLE IF NOT EXISTS artifact_meta(version INTEGER NOT NULL)')
            db.execute('INSERT INTO artifact_meta SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM artifact_meta)')
            if db.execute('SELECT version FROM artifact_meta').fetchall() != [(1,)]:
                raise ValueError('This artifact database requires a compatible Pebble version.')
            db.execute('''CREATE TABLE IF NOT EXISTS artifact_revisions(
                artifact_id TEXT NOT NULL, chat_id TEXT NOT NULL, revision INTEGER NOT NULL,
                title TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
                owner_run_id TEXT, evidence_ids TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, data_hash TEXT NOT NULL,
                previous_hash TEXT NOT NULL, record_hash TEXT NOT NULL,
                PRIMARY KEY(artifact_id,revision))''')
            db.execute('CREATE TABLE IF NOT EXISTS artifact_heads(artifact_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, record_hash TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS artifact_registry(artifact_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, created REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS artifact_revisions_chat ON artifact_revisions(chat_id,artifact_id,revision)')
            for row in db.execute('SELECT id,chat_id,title,kind,data,created FROM artifacts ORDER BY created,id').fetchall():
                self._read(db, row[1], row[0])

    @contextmanager
    def _transaction(self):
        # Store.query commits unconditionally; never call it inside this boundary.
        with self.store.lock:
            if self.store.db.in_transaction:
                raise ValueError('Artifact operations require an outside transaction boundary.')
            with self.store.db as db:
                db.execute('BEGIN IMMEDIATE')
                yield db

    @staticmethod
    def _encode(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))

    @classmethod
    def _digest(cls, value):
        return hashlib.sha256(cls._encode(value).encode()).hexdigest()

    @staticmethod
    def _version(value):
        if type(value) is not int or value < 1:
            raise ValueError('Use a positive integer artifact revision.')
        return value

    @staticmethod
    def _owner(db, cid, run_id):
        if not isinstance(run_id, str) or not run_id or len(run_id) > 300:
            raise ValueError('Artifact updates need their owning run.')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone():
            raise ValueError('Artifact updates need their owning run.')
        run = db.execute('SELECT chat_id,state FROM runs WHERE id=?', (run_id,)).fetchone()
        if not run or run != (cid, 'running'):
            raise ValueError('Only this conversation\'s running owner can create or update its artifact.')

    @classmethod
    def _evidence(cls, db, run_id, kind, data, evidence_ids):
        explicit = validate_evidence_ids([] if evidence_ids is None else evidence_ids)
        evidence = validate_evidence_ids(list(dict.fromkeys(explicit + referenced_evidence(kind, data))))
        if evidence:
            if not run_id or not db.execute("SELECT 1 FROM sqlite_master WHERE name='run_tools'").fetchone():
                raise ValueError('Evidence must reference the owning run\'s resolved tool receipts.')
            for ident in evidence:
                receipt = db.execute('SELECT state,result FROM run_tools WHERE run_id=? AND call_id=?', (run_id, ident)).fetchone()
                try:
                    outcome = json.loads(receipt[1]) if receipt and receipt[1] else None
                    cls._encode(outcome)
                    resolved = (receipt and isinstance(outcome, dict)
                                and outcome.get('status') not in ('failed', 'uncertain', 'needs_reconciliation')
                                and RunInteractions.resolved_evidence({'state': receipt[0], 'result': outcome}))
                except (TypeError, ValueError, KeyError):
                    resolved = False
                if not resolved:
                    raise ValueError('Evidence must reference the owning run\'s resolved tool receipts.')
        return evidence

    @classmethod
    def _append(cls, db, *, aid, cid, revision, title, kind, data, owner, evidence, status, created, updated, previous=''):
        snapshot = dict(zip(cls._COLUMNS[:-1], (aid, cid, revision, title, kind,
                       cls._encode(data), owner, cls._encode(evidence), status,
                       created, updated, cls._digest(data), previous)))
        snapshot['record_hash'] = cls._digest(snapshot)
        db.execute('INSERT OR IGNORE INTO artifact_registry VALUES(?,?,?)', (aid, cid, created))
        db.execute('INSERT INTO artifact_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', tuple(snapshot[k] for k in cls._COLUMNS))
        db.execute('INSERT INTO artifact_heads VALUES(?,?,?) ON CONFLICT(artifact_id) DO UPDATE SET revision=excluded.revision,record_hash=excluded.record_hash', (aid, revision, snapshot['record_hash']))
        return cls._public(snapshot)

    @classmethod
    def _public(cls, snapshot):
        return {'id': snapshot['artifact_id'], **{key: value for key, value in snapshot.items() if key not in {'artifact_id', 'previous_hash'}},
                'data': json.loads(snapshot['data']), 'evidence_ids': json.loads(snapshot['evidence_ids'])}

    @classmethod
    def _legacy(cls, db, row):
        aid, cid, title, kind, raw, created = row
        title = text(title, 120)
        if type(created) not in (int, float) or not math.isfinite(created):
            raise ValueError('Artifact timestamp is invalid.')
        owner = None
        if kind == 'mcp_media':
            data = json_object(raw)
            if set(data) != {'run_id', 'snapshot_id'} or any(not isinstance(v, str) or not v or len(v) > 300 for v in data.values()):
                raise ValueError('Invalid host media artifact reference.')
            owner = data['run_id']
        else:
            data = validate(kind, raw)
        cls._append(db, aid=aid, cid=cid, revision=1, title=title, kind=kind, data=data,
                    owner=owner, evidence=[], status='review' if kind == 'html' else 'ready', created=created, updated=created)

    @classmethod
    def _read(cls, db, cid, aid):
        row = db.execute('SELECT id,chat_id,title,kind,data,created FROM artifacts WHERE id=? AND chat_id=?', (aid, cid)).fetchone()
        if not row:
            raise ValueError('Artifact not found in this conversation.')
        head = db.execute('SELECT revision,record_hash FROM artifact_heads WHERE artifact_id=?', (aid,)).fetchone()
        registry = db.execute('SELECT chat_id,created FROM artifact_registry WHERE artifact_id=?', (aid,)).fetchone()
        if not head:
            if registry or db.execute('SELECT 1 FROM artifact_revisions WHERE artifact_id=? LIMIT 1', (aid,)).fetchone():
                raise ValueError('Artifact history does not match its current revision.')
            cls._legacy(db, row)
            head = db.execute('SELECT revision,record_hash FROM artifact_heads WHERE artifact_id=?', (aid,)).fetchone()
            registry = db.execute('SELECT chat_id,created FROM artifact_registry WHERE artifact_id=?', (aid,)).fetchone()
        if registry != (cid, row[5]):
            raise ValueError('Artifact ownership does not match its immutable registry.')
        revisions = db.execute('SELECT * FROM artifact_revisions WHERE artifact_id=? ORDER BY revision', (aid,)).fetchall()
        if not 1 <= len(revisions) <= cls.MAX_REVISIONS or head[0] != len(revisions):
            raise ValueError('Artifact history does not match its current revision.')
        history, previous = [], ''
        for version, revision in enumerate(revisions, 1):
            snapshot = dict(zip(cls._COLUMNS, revision))
            try:
                data = json.loads(snapshot['data'])
                evidence = json.loads(snapshot['evidence_ids'])
                valid = (snapshot['chat_id'] == cid and snapshot['revision'] == version
                         and snapshot['previous_hash'] == previous and snapshot['status'] in cls.LIFECYCLES
                         and snapshot['data_hash'] == cls._digest(data)
                         and snapshot['record_hash'] == cls._digest({k: v for k, v in snapshot.items() if k != 'record_hash'}))
                validate_evidence_ids(evidence)
            except (ValueError, TypeError, RecursionError):
                valid = False
            if not valid:
                raise ValueError('Artifact revision integrity check failed. This view cannot be opened.')
            previous = snapshot['record_hash']
            history.append(snapshot)
        latest = history[-1]
        try:
            materialized_matches = (row[2] == latest['title'] and row[3] == latest['kind']
                                    and row[5] == latest['created'] and cls._digest(json.loads(row[4])) == latest['data_hash'])
        except (ValueError, TypeError, RecursionError):
            materialized_matches = False
        if head[1] != previous or not materialized_matches:
            raise ValueError('Artifact current data does not match its immutable revision.')
        return history

    def render(self, cid, title, kind, data, run_id=None, evidence_ids=None):
        title = text(title, 120)
        data = validate(kind, data)
        if kind in NATIVE_KINDS:
            title = validate_native_title(kind, title)
        aid = uuid.uuid4().hex
        with self._transaction() as db:
            if run_id is not None:
                self._owner(db, cid, run_id)
            evidence = self._evidence(db, run_id, kind, data, evidence_ids)
            count = db.execute('SELECT count(*) FROM artifacts WHERE chat_id=?', (cid,)).fetchone()[0]
            if count >= self.MAX_ARTIFACTS:
                raise ValueError('This chat already has 30 views. Use a new conversation.')
            now = time.time()
            db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?)', (aid, cid, title, kind, self._encode(data), now))
            result = self._append(db, aid=aid, cid=cid, revision=1, title=title, kind=kind, data=data,
                                  owner=run_id, evidence=evidence, status='review' if kind == 'html' else 'ready', created=now, updated=now)
        return {**result, 'message': 'Artifact saved. HTML uses a bounded static preview; executable presentation code requires a reviewed isolated mod.'}

    def update(self, cid, artifact_id, expected_revision, title, kind, data, run_id, evidence_ids=None):
        expected_revision = self._version(expected_revision)
        title, data = text(title, 120), validate(kind, data)
        if kind in NATIVE_KINDS:
            title = validate_native_title(kind, title)
        with self._transaction() as db:
            current = self._read(db, cid, artifact_id)[-1]
            if current['revision'] != expected_revision:
                raise ValueError('This artifact revision is stale. Refresh it before updating.')
            if current['kind'] == 'mcp_media' or current['owner_run_id'] != run_id or run_id is None:
                raise ValueError('Only an artifact\'s owning run can update it.')
            self._owner(db, cid, run_id)
            if current['status'] in {'stopped', 'expired'}:
                raise ValueError('This artifact is no longer accepting updates.')
            if expected_revision >= self.MAX_REVISIONS - 1:
                raise ValueError('This artifact has reached its content revision limit; its final revision is reserved for native stop.')
            evidence = self._evidence(db, run_id, kind, data, evidence_ids)
            now = time.time()
            # The lock and BEGIN IMMEDIATE keep the revision read and CAS atomic.
            changed = db.execute('UPDATE artifact_heads SET revision=revision WHERE artifact_id=? AND revision=?', (artifact_id, expected_revision)).rowcount
            if changed != 1:
                raise ValueError('This artifact revision is stale. Refresh it before updating.')
            db.execute('UPDATE artifacts SET title=?,kind=?,data=? WHERE id=? AND chat_id=?', (title, kind, self._encode(data), artifact_id, cid))
            result = self._append(db, aid=artifact_id, cid=cid, revision=expected_revision + 1, title=title, kind=kind, data=data,
                                  owner=run_id, evidence=evidence, status='review' if kind == 'html' else 'ready',
                                  created=current['created'], updated=now, previous=current['record_hash'])
        return result

    def get(self, cid, artifact_id, revision=None):
        if revision is not None:
            self._version(revision)
        with self._transaction() as db:
            history = self._read(db, cid, artifact_id)
            if revision is not None and revision > len(history):
                raise ValueError('Artifact revision not found.')
            return self._public(history[-1] if revision is None else history[revision - 1])

    def history(self, cid, artifact_id):
        with self._transaction() as db:
            return [self._public(row) for row in self._read(db, cid, artifact_id)]

    def _retire(self, cid, artifact_id, expected_revision, status):
        expected_revision = self._version(expected_revision)
        with self._transaction() as db:
            current = self._read(db, cid, artifact_id)[-1]
            if current['revision'] != expected_revision:
                raise ValueError('This artifact revision is stale. Refresh it before stopping.')
            if current['status'] in {'stopped', 'expired'}:
                return self._public(current)
            if expected_revision >= self.MAX_REVISIONS:
                raise ValueError('Artifact history has exhausted its native stop revision.')
            return self._append(db, aid=artifact_id, cid=cid, revision=expected_revision + 1, title=current['title'], kind=current['kind'],
                                data=json.loads(current['data']), owner=current['owner_run_id'], evidence=json.loads(current['evidence_ids']),
                                status=status, created=current['created'], updated=time.time(), previous=current['record_hash'])

    def stop(self, cid, artifact_id, expected_revision):
        return self._retire(cid, artifact_id, expected_revision, 'stopped')

    def expire(self, cid, artifact_id, expected_revision):
        return self._retire(cid, artifact_id, expected_revision, 'expired')

    def list(self, cid):
        with self._transaction() as db:
            rows = db.execute('SELECT id FROM artifacts WHERE chat_id=? ORDER BY created,id', (cid,)).fetchall()
            return [self._public(self._read(db, cid, row[0])[-1]) for row in rows]

    def forget(self, cid):
        with self._transaction() as db:
            db.execute('DELETE FROM artifact_heads WHERE artifact_id IN (SELECT id FROM artifacts WHERE chat_id=?)', (cid,))
            db.execute('DELETE FROM artifact_revisions WHERE chat_id=?', (cid,))
            db.execute('DELETE FROM artifact_registry WHERE chat_id=?', (cid,))
            db.execute('DELETE FROM artifacts WHERE chat_id=?', (cid,))
