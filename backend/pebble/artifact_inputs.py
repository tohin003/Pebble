"""Native artifact inputs share the durable question/inbox transaction.

This API is exposed only through the native main-window RPC boundary. Generated
documents and mod workers never receive an input or permission capability.
"""
import json
import time
import uuid

from . import quiz_grading, view_bindings
from .artifacts import text, validate
from .artifact_schema import validate_native_title, NATIVE_KINDS, schema_error
from .interaction import question_items


def _static_html(data):
    """A model-published html view is static (F8): a script is refused and never stored."""
    if data.get('script', '').strip():
        raise ValueError('Custom views are static: inline scripts are not stored. Use a native widget or a reviewed mod.')
    return {key: value for key, value in data.items() if key not in ('script', 'language')}


def model_result(row):
    """What the owning model receives for quiz v2, dashboard and prototype renders.

    Identifiers, counts and integrity hashes only: never the answer key, a resolved
    binding value or the whole view echoed back. Other kinds keep their full row.
    """
    kind, data = row.get('kind'), row.get('data')
    base = {key: row[key] for key in ('id', 'revision', 'kind', 'title', 'status', 'evidence_ids', 'data_hash', 'record_hash')
            if key in row}
    if kind == 'quiz' and quiz_grading.is_public(data):
        return {**base, 'grading': 'host', 'duration_minutes': data['duration_minutes'],
                'question_ids': [q['id'] for q in data['questions']],
                'message': 'Quiz saved. The host keeps its answer key and grades attempts; a result reaches you only '
                           'if the person shares it.'}
    if kind == 'dashboard' and isinstance(data, dict):
        return {**base, 'panels': [{'id': p['id'], 'kind': p['kind'], **({'source': p['binding']['source']} if 'binding' in p else {})}
                                   for p in data['panels']],
                'message': 'Dashboard saved. Bound panels show live host counts to the person only; they are not sent to you.'}
    if kind == 'prototype' and isinstance(data, dict):
        return {**base, 'start': data['start'], 'screens': [s['id'] for s in data['screens']],
                'message': 'Mock-up saved. Its controls only switch between its own screens.'}
    return row


class ArtifactInputs:
    def __init__(self, app):
        self.app, self.store, self.artifacts = app, app.store, app.artifacts
        self.cache = view_bindings.Cache()
        self.store.query('''CREATE TABLE IF NOT EXISTS artifact_form_links(
            artifact_id TEXT NOT NULL, revision INTEGER NOT NULL,
            question_id TEXT NOT NULL UNIQUE, question_revision INTEGER NOT NULL,
            PRIMARY KEY(artifact_id,revision))''')

    def decorate(self, row):
        row = dict(row)
        if row['kind'] == 'form':
            links = self.store.query('SELECT question_id FROM artifact_form_links WHERE artifact_id=? AND revision=?', (row['id'], row['revision']))
            row['form_question'] = self.app.interactions.question(links[0]['question_id'], row['chat_id']) if links else None
        elif row['kind'] == 'dashboard':
            row['bindings'] = self._bindings(row)
        elif row['kind'] == 'quiz':
            # Mod and earlier quizzes keep their answers with the view: a labelled self-check.
            row['grading'] = 'host' if quiz_grading.is_public(row['data']) else 'self_check'
            if row['grading'] == 'host':
                with self.store.lock:
                    row['attempts'] = quiz_grading.summary_in(self.store.db, row['id'], row['revision'])
        return row

    def _bindings(self, row):
        # Scope comes from the immutable registry row only, never from the caller or the binding.
        with self.store.lock:
            registry = self.store.db.execute('SELECT chat_id FROM artifact_registry WHERE artifact_id=?', (row['id'],)).fetchone()
            head = self.store.db.execute('SELECT revision FROM artifact_heads WHERE artifact_id=?', (row['id'],)).fetchone()
        if (not registry or registry[0] != row.get('chat_id') or not head or head[0] != row['revision']
                or row['status'] in {'stopped', 'expired'}):
            return []  # Historical and stopped revisions show no live host data.
        return view_bindings.resolve_panels(self.app, registry[0], row['data'], self.cache)

    def refresh(self, cid, aid, revision):
        """Native Refresh (host chrome): the decorated current revision, read like any other.

        It appends no revision and writes nothing, so the owning run's revision never moves
        under it; the two-second read cache bounds how often host sources are queried.
        """
        row = self.artifacts.get(cid, aid)
        if type(revision) is not int or row['revision'] != revision:
            raise ValueError(f'This view is stale: it changed to revision {row["revision"]}. Open the new revision.')
        if row['kind'] != 'dashboard' or not any('binding' in panel for panel in row['data']['panels']):
            raise ValueError('Only a dashboard with live host panels can be refreshed.')
        if row['status'] != 'ready':
            raise ValueError('This dashboard is no longer active, so its host data is not refreshed.')
        return self.decorate(row)

    def _link(self, db, aid, revision):
        return db.execute('''SELECT l.question_id,l.question_revision,q.status
            FROM artifact_form_links l JOIN run_questions q ON q.id=l.question_id
            WHERE l.artifact_id=? AND l.revision=?''', (aid, revision)).fetchone()

    def _bound(self, db, current):
        link = self._link(db, current['artifact_id'], current['revision'])
        if not link:
            raise ValueError('This artifact has no native input capability.')
        question = db.execute('SELECT run_id,revision,items FROM run_questions WHERE id=?', (link[0],)).fetchone()
        fields = json.loads(current['data'])['fields']
        expected = question_items([{'id': f['id'], 'title': f['label'], 'options': f.get('options', [])} for f in fields])
        if not question or question[0] != current['owner_run_id'] or question[1] != link[1] or json.loads(question[2]) != expected:
            raise ValueError('The form does not match its native question capability.')
        return link

    def publish(self, cid, title, kind, data, rid, call_id, artifact_id=None, revision=None, evidence_ids=None):
        try:
            title = text(title, 120)
        except ValueError:
            # Phase 21 (F21-quiz): name the argument and its shape, so one corrected call can succeed.
            raise ValueError('Tool argument title must be non-empty text of at most 120 characters.') from None
        try:
            data = validate(kind, data)
            if kind == 'html':
                data = _static_html(data)
        except ValueError as exc:
            # The exact schema rides only a failure, never every request.
            raise ValueError(schema_error(kind, exc)) from None
        if kind in NATIVE_KINDS:
            title = validate_native_title(kind, title)
        # The model's quiz input is unchanged; the stored view holds only its public half.
        data, key = quiz_grading.split(data) if kind == 'quiz' else (data, None)
        # Legacy standalone non-input renders remain compatible. A live run
        # supplies its ID positionally so acknowledgement-failure tests still
        # exercise the durable effect boundary.
        if artifact_id is None and key is None and kind != 'form':
            return self.artifacts.render(cid, title, kind, data, rid, evidence_ids)
        aid = artifact_id or uuid.uuid4().hex
        with self.artifacts._transaction() as db:
            if rid is not None or artifact_id is not None or kind == 'form':
                self.artifacts._owner(db, cid, rid)
            evidence = self.artifacts._evidence(db, rid, kind, data, evidence_ids)
            current = self.artifacts._read(db, cid, aid)[-1] if artifact_id else None
            if current:
                self.artifacts._version(revision)
                if current['revision'] != revision:
                    raise ValueError(f'This artifact revision is stale; it is now revision {current["revision"]}. Refresh before updating.')
                if current['owner_run_id'] != rid or current['kind'] == 'mcp_media':
                    raise ValueError('Only the running owner can update this artifact.')
                if current['status'] in {'stopped', 'expired'}:
                    raise ValueError('This artifact has been stopped.')
                if revision >= self.artifacts.MAX_REVISIONS - 1:
                    raise ValueError('The last artifact revision is reserved for native stop.')
                if current['kind'] == 'form' and self._bound(db, current)[2] == 'pending':
                    raise ValueError('Resolve the current form before replacing it.')
                # An update closes this revision's open quiz attempts, ungraded and visibly.
                quiz_grading.supersede_in(db, aid, current['revision'], 'updated')
            elif db.execute('SELECT count(*) FROM artifacts WHERE chat_id=?', (cid,)).fetchone()[0] >= self.artifacts.MAX_ARTIFACTS:
                raise ValueError('This conversation already has 30 artifacts.')
            now, version = time.time(), revision + 1 if current else 1
            if current:
                db.execute('UPDATE artifacts SET title=?,kind=?,data=? WHERE id=? AND chat_id=?', (title, kind, self.artifacts._encode(data), aid, cid))
            else:
                db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?)', (aid, cid, title, kind, self.artifacts._encode(data), now))
            result = self.artifacts._append(db, aid=aid, cid=cid, revision=version, title=title, kind=kind,
                data=data, owner=rid, evidence=evidence, status='review' if kind == 'html' else 'ready',
                created=current['created'] if current else now, updated=now, previous=current['record_hash'] if current else '')
            if kind == 'form':
                items = [{'id': f['id'], 'title': f['label'], 'options': f.get('options', [])} for f in data['fields']]
                qid = self.app.interactions.create_question_in(db, rid, call_id, items, True)
                db.execute('INSERT INTO artifact_form_links VALUES(?,?,?,1)', (aid, version, qid))
                result.update(question_id=qid, required=True, status='awaiting_user_input')
            if key is not None:
                # Same transaction: the public revision never exists without its host-held key.
                quiz_grading.store_key_in(db, aid, version, cid, data, key)
        return result

    def submit(self, cid, aid, revision, answers):
        with self.artifacts._transaction() as db:
            current = self.artifacts._read(db, cid, aid)[-1]
            self.artifacts._version(revision)
            if current['revision'] != revision or current['kind'] != 'form' or current['status'] != 'ready':
                raise ValueError('This form is stale or stopped. Refresh it before answering.')
            link = self._bound(db, current)
            # The core rechecks pending state, chat/run ownership, exact answers,
            # credentials and idempotent replay inside the same transaction.
            return self.app.interactions.answer_in(db, cid, link[0], link[1], answers)

    def stop(self, cid, aid, revision):
        with self.artifacts._transaction() as db:
            current = self.artifacts._read(db, cid, aid)[-1]
            self.artifacts._version(revision)
            if current['revision'] != revision:
                raise ValueError('This artifact revision is stale. Refresh before stopping.')
            cancellation = None
            if current['status'] in {'stopped', 'expired'}:
                return self.artifacts._public(current), None
            link = self._bound(db, current) if current['kind'] == 'form' else None
            if link and link[2] == 'pending':
                cancellation = self.app.interactions.cancel_question_in(db, cid, link[0], link[1])
            quiz_grading.supersede_in(db, aid, revision, 'stopped')
            result = self.artifacts._append(db, aid=aid, cid=cid, revision=revision+1, title=current['title'], kind=current['kind'],
                data=json.loads(current['data']), owner=current['owner_run_id'], evidence=json.loads(current['evidence_ids']),
                status='stopped', created=current['created'], updated=time.time(), previous=current['record_hash'])
        return result, cancellation

    def forget(self, cid):
        # Call before artifact rows are removed, otherwise links become orphans.
        self.store.query('DELETE FROM artifact_form_links WHERE artifact_id IN (SELECT id FROM artifacts WHERE chat_id=?)', (cid,))
        with self.store.lock, self.store.db as db:
            quiz_grading.forget_in(db, cid)
        self.cache.clear(cid)
