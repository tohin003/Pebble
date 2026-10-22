"""Run-owned file witnesses; freshness is evidence, never authorization.

Only host file tools create witnesses. A handoff/model cannot refresh one by
claiming a hash. Audit notices changes; effect admission checks the target again.
Workspace snapshots enforce the same private-path and symlink restrictions as
reads. This ledger covers local file views, not web truth or external effects.
"""

import copy
import json
import time
from pathlib import Path

from .policy import PolicyError, digest


MAX_PATHS = 256
MAX_WITNESSES = 4096


class SourceProvenance:
    def __init__(self, store, journal):
        self.store, self.journal = store, journal
        with store.lock, store.db as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS run_sources(
              run_id TEXT NOT NULL, receipt_id TEXT NOT NULL, path TEXT NOT NULL,
              file_hash TEXT NOT NULL, disk_hash TEXT, basis TEXT NOT NULL,
              status TEXT NOT NULL, checked REAL NOT NULL, current_hash TEXT,
              root_dev INTEGER NOT NULL, root_ino INTEGER NOT NULL,
              ordinal INTEGER NOT NULL,
              PRIMARY KEY(run_id,receipt_id,path));
            CREATE INDEX IF NOT EXISTS run_sources_path ON run_sources(run_id,path);
            ''')
        journal.source_provenance = self

    def observe(self, rid, call_id, ws, path, text):
        run = self.journal.get(rid)
        if run['config']['workspace'] != str(ws.root):
            raise PolicyError('Source evidence belongs to this run\'s selected workspace.')
        ws.path(path, create=True)
        path = Path(path).as_posix()
        receipt = next((t for t in self.journal.tools(rid) if t['call_id'] == call_id), None)
        if not receipt or receipt['name'] not in {'Read', 'Write', 'Edit', 'Append'} or receipt['state'] != 'running':
            raise PolicyError('A file witness requires its owning admitted tool.')
        actual = ws.snapshot(path, missing_ok=True)
        current_hash = digest(actual) if actual is not None else None
        draft = ws.drafts.get(path)
        basis = 'draft' if draft and text == draft['after'] else 'disk'
        expected = draft['baseline'] if basis == 'draft' else digest(text.encode('utf-8'))
        state = 'current' if current_hash == expected else 'deleted' if actual is None else 'changed'
        with self.store.lock, self.store.db as db:
            count = db.execute('SELECT count(*),count(DISTINCT path) FROM run_sources WHERE run_id=?', (rid,)).fetchone()
            known = db.execute('SELECT 1 FROM run_sources WHERE run_id=? AND path=?', (rid, path)).fetchone()
            if count[0] >= MAX_WITNESSES or not known and count[1] >= MAX_PATHS:
                raise PolicyError('This run reached its bounded source ledger. Split the remaining work into a new task.')
            ordinal = db.execute('SELECT coalesce(max(ordinal),0)+1 FROM run_sources WHERE run_id=?', (rid,)).fetchone()[0]
            db.execute('INSERT INTO run_sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (rid, call_id, path, digest(text.encode('utf-8')), expected, basis, state,
                 time.time(), current_hash, *ws.identity, ordinal))
            self.journal.event(db, rid, 'source_observed', {'receipt_id': call_id, 'path': path, 'basis': basis, 'status': state})
        return {'path': path, 'file_hash': digest(text.encode('utf-8')), 'disk_hash': expected, 'basis': basis, 'status': state}

    def records(self, rid):
        rows = self.store.query('SELECT run_id,receipt_id,path,file_hash,disk_hash,basis,status,checked,current_hash FROM run_sources WHERE run_id=? ORDER BY ordinal', (rid,))
        return rows

    def latest(self, rid):
        rows = self.store.query('SELECT run_id,receipt_id,path,file_hash,disk_hash,basis,status,checked,current_hash FROM run_sources s WHERE run_id=? AND ordinal=(SELECT max(ordinal) FROM run_sources WHERE run_id=s.run_id AND path=s.path) ORDER BY ordinal', (rid,))
        return rows

    def _inspect(self, row, ws):
        if ws is None or (row['root_dev'], row['root_ino']) != ws.identity:
            return 'unreadable', None
        try:
            data = ws.snapshot(row['path'], missing_ok=True)
        except (OSError, PolicyError, UnicodeError):
            return 'unreadable', None
        current = digest(data) if data is not None else None
        return ('current' if current == row['disk_hash'] else 'deleted' if data is None else 'changed'), current

    def audit(self, rid, ws, path=None):
        # A snapshot per path, shared by that path's historical witnesses.
        rows = self.store.query('SELECT * FROM run_sources WHERE run_id=?' + (' AND path=?' if path else '') + ' ORDER BY path,ordinal', (rid, path) if path else (rid,))
        snapshots, changes = {}, []
        for row in rows:
            key = (row['path'], row['root_dev'], row['root_ino'])
            if key not in snapshots:
                copy = {**row, 'disk_hash': None}
                state, current = self._inspect(copy, ws)
                snapshots[key] = (state == 'unreadable', current)
            unreadable, current = snapshots[key]
            state = 'unreadable' if unreadable else 'current' if current == row['disk_hash'] else 'deleted' if current is None else 'changed'
            if state != row['status'] or current != row['current_hash']:
                changes.append({'receipt_id': row['receipt_id'], 'path': row['path'], 'status': state, 'current_hash': current})
        now = time.time()
        with self.store.lock, self.store.db as db:
            db.execute('UPDATE run_sources SET checked=? WHERE run_id=?' + (' AND path=?' if path else ''),
                (now, rid, path) if path else (now, rid))
            for item in changes:
                db.execute('UPDATE run_sources SET status=?,current_hash=?,checked=? WHERE run_id=? AND receipt_id=? AND path=?',
                    (item['status'], item['current_hash'], now, rid, item['receipt_id'], item['path']))
            if changes:
                self.journal.event(db, rid, 'sources_changed', {'changes': changes})
        return changes

    def restore_drafts(self, rid, ws):
        """Restore only this run's pending broker views on unchanged roots/bases."""
        run = self.journal.get(rid)
        owners = {t['result'].get('action_id') for t in self.journal.tools(rid)
                  if t['state'] == 'completed' and isinstance(t.get('result'), dict)}
        restored = 0
        for action in self.store.actions(run['chat_id']):
            data = action['payload']
            if action['id'] not in owners or action['kind'] != 'edit' or action['status'] != 'pending' or data.get('root') != str(ws.root):
                continue
            rows = self.store.query('SELECT root_dev,root_ino FROM run_sources WHERE run_id=? AND path=? AND file_hash=?',
                (rid, data['path'], digest(data['after'].encode('utf-8'))))
            if data.get('root_identity') != list(ws.identity) or not any((r['root_dev'], r['root_ino']) == ws.identity for r in rows):
                continue
            try:
                actual = ws.snapshot(data['path'], missing_ok=True)
            except (OSError, PolicyError):
                continue
            if (digest(actual) if actual is not None else None) == data['baseline']:
                ws.drafts[data['path']] = copy.deepcopy(data)
                restored += 1
        return restored

    def require_current_read(self, rid, ws, path):
        ws.path(path, create=True)
        path = Path(path).as_posix()
        # New files need no invented prior read. Existing files and drafts do.
        if ws.snapshot(path, missing_ok=True) is None and path not in ws.drafts:
            return
        self.audit(rid, ws, path)
        rows = self.store.query("SELECT s.*,t.name,t.state,t.result AS tool_result FROM run_sources s JOIN run_tools t ON t.run_id=s.run_id AND t.call_id=s.receipt_id WHERE s.run_id=? AND s.path=? AND t.state='completed' ORDER BY s.checked DESC", (rid, path))
        # A broker-confirmed prior draft also establishes the model's exact view.
        draft = ws.drafts.get(path)
        expected = digest(draft['after'].encode('utf-8')) if draft else digest(ws.snapshot(path))
        if not any(r['status'] == 'current' and r['file_hash'] == expected and r['tool_result'] and not json.loads(r['tool_result']).get('error') for r in rows):
            raise PolicyError('The file has no current source witness in this run. Read it again before changing it: ' + path)

    def applied(self, rid, aid, payload, result):
        """Refresh only after authoritative same-run applied-file readback."""
        from .policy import Workspace
        ws = Workspace(payload['root'])
        data = ws.snapshot(payload['path'])
        actual = digest(data)
        after = digest(payload['after'].encode('utf-8'))
        if actual != after or result.get('hash') != actual:
            return
        with self.store.lock, self.store.db as db:
            rows = db.execute('SELECT s.receipt_id FROM run_sources s JOIN run_tools t ON t.run_id=s.run_id AND t.call_id=s.receipt_id WHERE s.run_id=? AND s.path=? AND s.file_hash=? AND s.basis=\'draft\' AND t.result IS NOT NULL', (rid, payload['path'], after)).fetchall()
            for (receipt_id,) in rows:
                stored = db.execute('SELECT result FROM run_tools WHERE run_id=? AND call_id=?', (rid, receipt_id)).fetchone()
                if json.loads(stored[0]).get('action_id') == aid:
                    db.execute("UPDATE run_sources SET disk_hash=?,basis='disk',status='current',current_hash=?,checked=? WHERE run_id=? AND receipt_id=? AND path=?",
                        (actual, actual, time.time(), rid, receipt_id, payload['path']))
                    self.journal.event(db, rid, 'source_applied', {'receipt_id': receipt_id, 'path': payload['path'], 'hash': actual})

    @staticmethod
    def forget(db, cid):
        db.execute('DELETE FROM run_sources WHERE run_id IN (SELECT id FROM runs WHERE chat_id=?)', (cid,))
