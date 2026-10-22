"""Child-owned managed Git worktrees: ownership ledger, allocation and write adapter.

A root SpawnAgent(..., workspace="worktree") stages one broker action of kind
`worktree`. Its payload (propose) is data that pins the run, agent, generation,
repository fingerprint, branch digest and a host-generated worktree ID. Only
App.perform_action calls allocate, after native approval or an automatic
policy. allocate re-validates every binding before GitWorkflows.create_bound
adds a detached checkout at the exact HEAD commit; uncommitted project work is
never copied or discarded.

The owning child generation, holding a live lease, reads through a verified
Workspace on its own worktree and writes there with WriteWorktreeFile or
EditWorktreeFile. Writes never touch the project, .git metadata or another
worktree. The root integrates them through the existing export and root Write
broker path. A guard registered in GitWorkflows refuses export, removal and
forgetting while the child is live. Rows are registered before every effect,
and an uncertain allocation, write or retirement is never replayed. Model
arguments, child output and repository content are data; none grants authority.
"""

import json
import os
import re
import secrets
import stat
import time
from pathlib import Path, PurePosixPath

from .agent_effects import root_workspace
from .agent_tree import ACTIVE, TERMINAL, identifier, normalized_path
from .git_workflows import (IDENT, STR, WRITE_SECONDS, blob_oid, canonical, clean, display, excerpt, fingerprint,
                            message, private, tool)
from .interaction import SECRET
from .policy import MAX_FILE, PolicyError, Workspace, digest
from .process_jobs import live_writer

STATES = ('proposed', 'allocating', 'active', 'failed', 'rejected', 'uncertain', 'retiring', 'retired')
OPEN = ('proposed', 'allocating', 'active', 'uncertain', 'retiring')
SETTLED = ('failed', 'rejected', 'retired')
MAX_CHILD_WORKTREES = 4
MAX_PATHS, MAX_BYTES, MAX_WRITES = 64, 4_000_000, 128
MAX_PROBE, MAX_PRESERVE = 16_000_000, 256
HASH = re.compile(r'[0-9a-f]{64}')
TEMP = '.pebble-write-'
# Git metadata and the control files that change how Git reads worktree content.
GIT_CONTROL = ('.git', '.gitmodules', '.gitattributes')
PAYLOAD_KEYS = frozenset({'operation', 'run_id', 'agent_id', 'generation', 'worktree_id', 'root', 'root_identity',
                          'fingerprint', 'refs', 'base_commit', 'path', 'effects', 'disclosure', 'requires_review'})
# Host broker metadata (App.propose adds permission_mode; a hook may require
# review). Never part of the binding; any other added key is refused.
BROKER_KEYS = frozenset({'permission_mode', 'requires_review'})
DISCLOSURE = ('Detached checkout at the exact HEAD commit; uncommitted project changes are not copied; the child '
              'writes only inside this folder, never the project; the root reviews and integrates its changes; the '
              'folder is retained after the run until integration or a separate native discard review.')
NOTE = 'Written only in your own managed worktree; the project is unchanged. The root reviews and integrates it.'
TOOLS = [
    tool('WriteWorktreeFile', 'Write one complete UTF-8 text file (at most 96 KB) in your own managed Git worktree. '
         'The write lands only in that worktree, never in the project; the root reviews it and integrates it into the '
         'project. Replacing an existing file needs your Read of its current content in this generation. expected_hash, '
         'when given, is that file_hash, or "absent" for a new file.',
         {'file_path': STR, 'content': STR, 'expected_hash': STR}, ['file_path', 'content']),
    tool('EditWorktreeFile', 'Replace the single exact occurrence of old_string with new_string in one file of your '
         'own managed Git worktree. The edit lands only in that worktree, never in the project; the root reviews it and '
         'integrates it into the project. Needs your Read of the current file in this generation; expected_hash, when '
         'given, is its file_hash.',
         {'file_path': STR, 'old_string': STR, 'new_string': STR, 'expected_hash': STR},
         ['file_path', 'old_string', 'new_string']),
]
TOOL_ARGS = {item['function']['name']: frozenset(item['function']['parameters']['properties']) for item in TOOLS}
REQUIRED = {item['function']['name']: frozenset(item['function']['parameters']['required']) for item in TOOLS}
GROUP = dict.fromkeys(TOOL_ARGS, 'edits')


class Unconfirmed(PolicyError):
    """The worktree file may have changed, but its readback did not confirm the write."""


def target(value):
    """A normalized worktree-relative file path outside private and Git metadata."""
    try:
        path = normalized_path(value)
        path.encode('utf-8')
    except ValueError:
        raise PolicyError('Use a normalized relative file path inside your worktree, outside private files and Git metadata.') from None
    if any(part.lower() in GIT_CONTROL for part in path.split('/')) or private(path):
        raise PolicyError('Private credentials and Git metadata are outside worktree writes.')
    return path


def bound_payload(payload):
    return {key: value for key, value in payload.items() if key not in BROKER_KEYS}


def change_kind(entry):
    if entry['kind'] != 'changed':
        return entry['kind']
    codes = {entry.get('index'), entry.get('worktree')}
    return 'deleted' if 'D' in codes else 'added' if 'A' in codes else 'modified'


def _open_root(root, identity):
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise PolicyError('The folder is missing, replaced or a symbolic link.') from None
    info = os.fstat(fd)
    if [info.st_dev, info.st_ino] != list(identity):
        os.close(fd)
        raise PolicyError('The folder was replaced. Nothing was written.')
    return fd


def _parents(root, identity, parts, create):
    """No-follow descriptors from the exact root to the parent folder of parts[-1].

    Returns None when a parent is missing and create is False. A symbolic link
    or a non-folder parent component is refused.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds, done = [_open_root(root, identity)], False
    try:
        for part in parts[:-1]:
            try:
                fds.append(os.open(part, flags, dir_fd=fds[-1]))
            except FileNotFoundError:
                if not create:
                    return None
                os.mkdir(part, 0o755, dir_fd=fds[-1])
                fds.append(os.open(part, flags, dir_fd=fds[-1]))
        done = True
        return fds
    except OSError:
        raise PolicyError('A parent folder of this path is a symbolic link or not a folder.') from None
    finally:
        if not done:
            for fd in reversed(fds):
                os.close(fd)


def _leaf(parent, name, limit=MAX_FILE):
    """(bytes, stat) of one bounded regular file, or (None, None) when absent."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None, None
    except OSError:
        raise PolicyError('The target is a symbolic link or not readable.') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise PolicyError('The target is not a regular file within the 96 KB source limit.')
        chunks, size = [], 0
        while chunk := os.read(fd, limit + 1 - size):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise PolicyError('The target exceeds the 96 KB source limit.')
        return b''.join(chunks), info
    finally:
        os.close(fd)


def _probe(root, identity, name, limit=MAX_PROBE):
    """('absent'|'file'|'other', bytes) for one relative path, through no-follow descriptors."""
    parts = name.split('/')
    os.close(_open_root(root, identity))  # A replaced root refuses; it is never a path result.
    try:
        fds = _parents(root, identity, parts, False)
    except PolicyError:
        return 'other', None
    if fds is None:
        return 'absent', None
    try:
        data, _ = _leaf(fds[-1], parts[-1], limit)
        return ('absent', None) if data is None else ('file', data)
    except PolicyError:
        return 'other', None
    finally:
        for fd in reversed(fds):
            os.close(fd)


# Shared with migrations.REFERENCE_DDL (schema epoch 4); keep them identical.
WORKTREE_DDL = """
CREATE TABLE IF NOT EXISTS agent_worktrees(worktree_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,agent_id TEXT NOT NULL,generation INTEGER NOT NULL,action_id TEXT,
  workspace TEXT NOT NULL,root_identity TEXT NOT NULL,base_commit TEXT NOT NULL,state TEXT NOT NULL,
  evidence TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(run_id,agent_id));
CREATE INDEX IF NOT EXISTS agent_worktrees_chat ON agent_worktrees(chat_id,state);
CREATE TABLE IF NOT EXISTS agent_worktree_writes(id TEXT PRIMARY KEY,worktree_id TEXT NOT NULL,
  run_id TEXT NOT NULL,agent_id TEXT NOT NULL,generation INTEGER NOT NULL,call_id TEXT NOT NULL,
  path TEXT NOT NULL,before_hash TEXT,after_hash TEXT,bytes INTEGER NOT NULL,created REAL NOT NULL,
  UNIQUE(worktree_id,call_id));
CREATE INDEX IF NOT EXISTS agent_worktree_writes_tree ON agent_worktree_writes(worktree_id,generation);
"""

# Allocation runs either automatically inside the running root's own tool call
# or from native approval while that root is parked waiting for the review.
APPROVABLE = ('running', 'waiting_approval')

class ChildWorktrees:
    def __init__(self, app, git):
        self.app, self.git, self.store = app, git, app.store
        self.tree = app.agents.tree
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise PolicyError('Child worktrees require an outside transaction boundary.')
            with db:
                for statement in WORKTREE_DDL.split(';'):
                    if statement.strip():
                        db.execute(statement)
                # An allocating/retiring row means its outcome was never observed
                # here. Git's own record and the disk decide; nothing is retried.
                for row in self._select(db, "SELECT * FROM agent_worktrees WHERE state IN ('allocating','retiring')"):
                    found = db.execute('SELECT state FROM git_worktrees WHERE id=?', (row['worktree_id'],)).fetchone()
                    git_state, present = found[0] if found else None, self._present(row)
                    if row['state'] == 'allocating':
                        state = 'failed' if git_state in (None, 'failed') and not any(present.values()) else 'uncertain'
                    else:
                        state = 'retired' if git_state in (None, 'removed') and not any(present.values()) else 'uncertain'
                    db.execute('UPDATE agent_worktrees SET state=?,evidence=?,updated=? WHERE worktree_id=?',
                               (state, canonical({**self._carry(row), 'phase': 'interrupted', 'interrupted': row['state'],
                                                  'git_state': git_state, 'present': present, 'previous': self._previous(row),
                                                  'note': 'The service stopped before this effect was confirmed. Nothing was retried.'}),
                                time.time(), row['worktree_id']))
        git.guards.append(self.guard)

    # Storage

    @staticmethod
    def _select(db, sql, args=()):
        cursor = db.execute(sql, args)
        names = [item[0] for item in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _rows(self, sql, args=()):
        with self.store.lock:
            return self._select(self.store.db, sql, args)

    def _one(self, sql, args=()):
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    @staticmethod
    def _carry(row):
        try:
            binding = json.loads(row['evidence']).get('binding')
        except (TypeError, ValueError, AttributeError):
            binding = None
        return {'binding': binding} if binding else {}

    @staticmethod
    def _previous(row):
        # Registered facts (for example preserved blob receipts) survive a later settlement.
        try:
            evidence = json.loads(row['evidence'])
        except (TypeError, ValueError):
            return None
        return {key: value for key, value in evidence.items() if key != 'binding'} if isinstance(evidence, dict) else None

    def _set(self, db, row, state, evidence, expect, strict=True):
        cursor = db.execute('UPDATE agent_worktrees SET state=?,evidence=?,updated=? WHERE worktree_id=? AND state=?',
                            (state, canonical({**self._carry(row), **evidence}), time.time(), row['worktree_id'], expect))
        if strict and cursor.rowcount != 1:
            raise PolicyError('This child worktree changed state. Nothing was retried.')

    def _settle(self, row, state, evidence, expect):
        # Settlement after an outcome never masks it: a concurrent change wins.
        with self.tree._transaction() as db:
            self._set(db, row, state, evidence, expect, strict=False)

    def _present(self, row):
        path = self.git.anchor / row['worktree_id']
        admin = Path(row['workspace']) / '.git' / 'worktrees' / row['worktree_id']
        try:
            return {'folder': os.path.lexists(path), 'metadata': os.path.lexists(admin)}
        except (OSError, ValueError):
            return {'folder': True, 'metadata': True}

    def _public(self, row):
        with self.store.lock:
            writes, total, paths = self.store.db.execute(
                'SELECT count(*),coalesce(sum(bytes),0),count(DISTINCT path) FROM agent_worktree_writes WHERE worktree_id=?',
                (row['worktree_id'],)).fetchone()
        return {'worktree_id': row['worktree_id'], 'run_id': row['run_id'], 'chat_id': row['chat_id'],
                'agent_id': row['agent_id'], 'generation': row['generation'], 'action_id': row['action_id'],
                'state': row['state'], 'path': str(self.git.anchor / row['worktree_id']), 'workspace': row['workspace'],
                'base_commit': row['base_commit'], 'evidence': json.loads(row['evidence']), 'writes': writes,
                'bytes_written': total, 'changed_paths': paths, 'created': row['created'], 'updated': row['updated'],
                'origin': 'child_worktree', 'authorization': 'none'}

    # Scope checks

    @staticmethod
    def _ids(*values):
        for value in values:
            try:
                identifier(value)
            except ValueError:
                raise PolicyError('Use bounded host identifiers.') from None

    def _run(self, cid, rid):
        try:
            return self.app.runs.get(rid, cid)
        except ValueError:
            raise PolicyError('Root run not found in this conversation.') from None

    @staticmethod
    def _permitted(config, *groups):
        tools = ((config or {}).get('permissions') or {}).get('tools') or {}
        if not all(tools.get(group) is True for group in groups):
            raise PolicyError('Child worktrees require ' + ' and '.join(groups) + ' access in the root permission snapshot.')

    @staticmethod
    def _root(config, row=None):
        """The exact pinned project root Workspace of the root run, or PolicyError."""
        ws = root_workspace(config.get('workspace'))
        if config.get('workspace_identity') is None or list(ws.identity) != list(config['workspace_identity']):
            raise PolicyError('The root project folder was renamed or replaced since the root started. Start a fresh task.')
        if row is not None and (row['workspace'] != str(ws.root) or json.loads(row['root_identity']) != list(ws.identity)):
            raise PolicyError('The project root was replaced after this child worktree was bound.')
        return ws

    @staticmethod
    def _node_in(db, rid, agent_id, generation):
        node = db.execute('SELECT generation,status FROM agent_nodes WHERE id=? AND run_id=?', (agent_id, rid)).fetchone()
        if not node:
            raise PolicyError('Agent not found in this root tree.')
        if node[0] != generation:
            raise PolicyError('This agent generation is stale.')
        if node[1] not in ACTIVE:
            raise PolicyError('This agent has already settled or stopped.')

    @staticmethod
    def _running_in(db, cid, rid, allowed=('running',)):
        root = db.execute('SELECT state FROM runs WHERE id=? AND chat_id=?', (rid, cid)).fetchone()
        if not root or root[0] not in allowed:
            raise PolicyError('Only a running root can stage or allocate a child worktree.')

    def _live_in(self, db, row, owner):
        """The owning generation's live lease, inside the caller's transaction."""
        try:
            self.tree._root(db, row['chat_id'], row['run_id'], True)
            node = self.tree._node(db, row['run_id'], row['agent_id'])
            self.tree._lease(db, node, row['generation'], owner)
        except PolicyError:
            raise
        except ValueError as exc:
            raise PolicyError(str(exc)) from None
        if node['status'] not in ACTIVE:
            raise PolicyError('This child is no longer live.')
        current = db.execute('SELECT state,generation FROM agent_worktrees WHERE worktree_id=?', (row['worktree_id'],)).fetchone()
        if not current or tuple(current) != ('active', row['generation']):
            raise PolicyError('This child worktree is no longer active.')

    def _hidden(self, repo, row, folder, identity, evidence, cancel):
        """Files this child wrote whose bytes differ from the base commit while Git lists no change.

        Ignore rules (for example a later .gitignore edit) or attribute
        normalization can hide them from status, export and a forced removal.
        """
        listed = {entry['path'] for entry in evidence['changes']}
        ignored = {entry['path'] for entry in evidence['ignored']}
        paths = sorted({item['path'] for item in self._rows('SELECT DISTINCT path FROM agent_worktree_writes WHERE worktree_id=?',
                                                            (row['worktree_id'],))} - listed)
        base = self.git._tree(repo, row['base_commit'], paths, cancel) if paths else {}
        hidden = []
        for name in paths:
            kind, data = _probe(folder, identity, name)
            entry = base.get(name)
            if (kind == 'absent' and entry is None) or (kind == 'file' and entry is not None and entry['type'] == 'blob'
                                                        and entry['mode'] in ('100644', '100755') and blob_oid(data) == entry['oid']):
                continue
            hidden.append({'path': name, 'kind': 'ignored' if name in ignored else 'normalized'})
        return hidden

    def _owned(self, worker, cancel=None, groups=('files',)):
        """(row, git row, worktree Workspace, repository) for the live owning generation, verified."""
        if not isinstance(worker, dict):
            raise PolicyError('Child worktree access requires the host worker identity.')
        cid, rid, ident, generation = (worker.get(k) for k in ('cid', 'rid', 'id', 'generation'))
        self._ids(cid, rid, ident)
        row = self._one('SELECT * FROM agent_worktrees WHERE run_id=? AND agent_id=? AND chat_id=?', (rid, ident, cid))
        if not row:
            raise PolicyError('This child has no managed worktree.')
        if row['state'] != 'active':
            raise PolicyError('This child worktree is not active.')
        if type(generation) is not int or generation != row['generation']:
            raise PolicyError('This child generation does not own the worktree.')
        with self.tree._transaction() as db:
            self._live_in(db, row, worker.get('owner'))
        run = self._run(cid, rid)
        self._permitted(run['config'], *groups)
        ws = self._root(run['config'], row)
        repo = self.git._repository(cid, ws, cancel)
        git_row = self.git._row(cid, row['worktree_id'])
        if git_row['state'] != 'active':
            raise PolicyError('The managed worktree record is not active. Inspect it natively.')
        self.git._verify(repo, git_row, cancel, ignored=False)
        tree_ws = Workspace(self.git.anchor / row['worktree_id'])
        if list(tree_ws.identity) != git_row['path_identity']:
            raise PolicyError('The managed worktree folder was replaced.')
        return row, git_row, tree_ws, repo

    # Proposal and allocation (broker kind 'worktree')

    def propose(self, cid, rid, agent_id, generation):
        """Stage a data-only allocation payload and its 'proposed' row."""
        self._ids(cid, rid, agent_id)
        if type(generation) is not int or generation < 1:
            raise PolicyError('Use the exact positive child generation.')
        run = self._run(cid, rid)
        if run['state'] != 'running':
            raise PolicyError('Only a running root can stage or allocate a child worktree.')
        config = run['config']
        self._permitted(config, 'files', 'edits')
        ws = self._root(config)
        with self.store.lock:
            self._node_in(self.store.db, rid, agent_id, generation)
        bound = self.git.bind_create(cid, ws)
        if bound['root'] != config['workspace'] or bound['root_identity'] != list(config['workspace_identity']):
            raise PolicyError('The repository root does not match the root run project.')
        ident = bound['worktree_id']
        admin = Path(bound['root']) / '.git' / 'worktrees' / ident
        payload = {'operation': 'allocate_child_worktree', 'run_id': rid, 'agent_id': agent_id, 'generation': generation,
                   'worktree_id': ident, 'root': bound['root'], 'root_identity': bound['root_identity'],
                   'fingerprint': bound['fingerprint'], 'refs': bound['refs'], 'base_commit': bound['base_commit'],
                   'path': bound['path'], 'effects': ['Create folder ' + bound['path'], 'Add Git metadata ' + str(admin)],
                   'disclosure': DISCLOSURE, 'requires_review': False}
        binding = {'fingerprint': bound['fingerprint'], 'refs': bound['refs'], 'path': bound['path'],
                   'payload': fingerprint(bound_payload(payload))}
        now = time.time()
        with self.tree._transaction() as db:
            self._running_in(db, cid, rid)
            self._node_in(db, rid, agent_id, generation)
            if db.execute('SELECT 1 FROM agent_worktrees WHERE run_id=? AND agent_id=?', (rid, agent_id)).fetchone():
                raise PolicyError('This child already has a managed worktree record.')
            live = db.execute('SELECT count(*) FROM agent_worktrees WHERE run_id=? AND state IN (?,?,?,?,?)', (rid, *OPEN)).fetchone()[0]
            if live >= MAX_CHILD_WORKTREES:
                raise PolicyError(f'A root tree holds at most {MAX_CHILD_WORKTREES} unsettled child worktrees. Integrate and retire one first.')
            db.execute("INSERT INTO agent_worktrees VALUES(?,?,?,?,?,NULL,?,?,?,'proposed',?,?,?)",
                       (ident, rid, cid, agent_id, generation, bound['root'], canonical(bound['root_identity']),
                        bound['base_commit'], canonical({'phase': 'proposed', 'binding': binding}), now, now))
        return payload

    def _receipt(self, row):
        evidence = json.loads(row['evidence'])
        return {'worktree_id': row['worktree_id'], 'state': row['state'], 'path': str(self.git.anchor / row['worktree_id']),
                'base_commit': row['base_commit'], 'run_id': row['run_id'], 'agent_id': row['agent_id'],
                'generation': row['generation'], 'action_id': row['action_id'], 'detached': True, 'branch_created': False,
                'hooks_run': False, 'dirty_work_copied': False,
                'parent_status_unchanged': evidence.get('parent_status_unchanged'),
                'parent_index_unchanged': evidence.get('parent_index_unchanged'),
                'origin': 'child_worktree', 'authorization': 'none'}

    def allocate(self, payload, action_id=None, cancel=None):
        """Perform one stored, approved allocation exactly once; re-raise on error."""
        if not isinstance(payload, dict) or payload.get('operation') != 'allocate_child_worktree':
            raise PolicyError('This is not a child worktree allocation.')
        ident = payload.get('worktree_id')
        if not isinstance(ident, str) or not IDENT.fullmatch(ident):
            raise PolicyError('Managed worktree IDs are host-generated.')
        if action_id is not None and (not isinstance(action_id, str) or not 1 <= len(action_id) <= 128):
            raise PolicyError('Use the host broker action ID.')
        row = self._one('SELECT * FROM agent_worktrees WHERE worktree_id=?', (ident,))
        if not row:
            raise PolicyError('Child worktree proposal not found.')
        binding = json.loads(row['evidence']).get('binding') or {}
        if (set(payload) - PAYLOAD_KEYS - BROKER_KEYS or type(payload.get('requires_review', False)) is not bool
                or fingerprint(bound_payload(payload)) != binding.get('payload') or payload.get('run_id') != row['run_id']
                or payload.get('agent_id') != row['agent_id'] or payload.get('generation') != row['generation']
                or payload.get('root') != row['workspace'] or payload.get('root_identity') != json.loads(row['root_identity'])
                or payload.get('base_commit') != row['base_commit'] or payload.get('path') != str(self.git.anchor / ident)):
            raise PolicyError('The allocation payload does not match its stored proposal.')
        if row['state'] == 'active' and action_id is not None and row['action_id'] == action_id:
            return self._receipt(row)  # The same action already settled; nothing runs again.
        if row['state'] != 'proposed':
            raise PolicyError('This child worktree proposal is no longer pending. Nothing was retried.')
        cid, rid = row['chat_id'], row['run_id']
        try:
            run = self._run(cid, rid)
            if run['state'] not in APPROVABLE:
                raise PolicyError('Only a running root can stage or allocate a child worktree.')
            self._permitted(run['config'], 'files', 'edits')
            ws = self._root(run['config'], row)
            with self.store.lock:
                self._node_in(self.store.db, rid, row['agent_id'], row['generation'])
            repo = self.git._repository(cid, ws, cancel)
            if (repo['fingerprint'] != binding['fingerprint'] or repo['commit'] != row['base_commit']
                    or self.git._refs(repo, cancel) != binding['refs']):
                raise PolicyError('The repository HEAD, branches or metadata changed after this proposal. Nothing was created.')
            with self.tree._transaction() as db:
                self._running_in(db, cid, rid, APPROVABLE)
                self._node_in(db, rid, row['agent_id'], row['generation'])
                # Registration precedes the physical effect; restart never replays it.
                moved = db.execute("UPDATE agent_worktrees SET state='allocating',action_id=?,evidence=?,updated=? "
                                   "WHERE worktree_id=? AND state='proposed'",
                                   (action_id, canonical({**self._carry(row), 'phase': 'allocation registered before Git ran'}),
                                    time.time(), ident)).rowcount
                if moved != 1:
                    raise PolicyError('This child worktree proposal is no longer pending. Nothing was retried.')
        except Exception as exc:
            self._settle(row, 'failed', {'phase': 'refused before Git ran', 'error': message(exc)}, 'proposed')
            raise
        try:
            receipt = self.git.create_bound(cid, ident, binding['fingerprint'], binding['refs'], workspace=ws, cancel=cancel)
            if receipt['path'] != binding['path'] or receipt['base_commit'] != row['base_commit']:
                raise PolicyError('Git created a worktree that does not match the proposal.')
        except Exception as exc:
            found = self._one('SELECT state FROM git_worktrees WHERE id=?', (ident,))
            git_state, present = found['state'] if found else None, self._present(row)
            state = 'failed' if git_state in (None, 'failed') and not any(present.values()) else 'uncertain'
            self._settle(row, state, {'phase': 'allocation failed', 'git_state': git_state, 'present': present,
                                      'error': message(exc), 'note': 'Nothing is retried automatically.'}, 'allocating')
            raise
        self._settle(row, 'active', {'phase': 'allocated and verified', 'action_id': action_id,
                                     'parent_status_unchanged': receipt['parent_status_unchanged'],
                                     'parent_index_unchanged': receipt['parent_index_unchanged']}, 'allocating')
        return self._receipt(self._one('SELECT * FROM agent_worktrees WHERE worktree_id=?', (ident,)))

    def reject(self, worktree_id, reason):
        """A native rejection, or an owner closing before allocation: proposed -> rejected."""
        if not isinstance(worktree_id, str) or not IDENT.fullmatch(worktree_id):
            raise PolicyError('Managed worktree IDs are host-generated.')
        if not isinstance(reason, str) or not reason.strip():
            raise PolicyError('Give a short rejection reason.')
        with self.tree._transaction() as db:
            rows = self._select(db, 'SELECT * FROM agent_worktrees WHERE worktree_id=?', (worktree_id,))
            if not rows:
                raise PolicyError('Child worktree proposal not found.')
            if rows[0]['state'] != 'rejected':
                if rows[0]['state'] != 'proposed':
                    raise PolicyError('Only a pending child worktree proposal can be rejected.')
                self._set(db, rows[0], 'rejected', {'phase': 'rejected', 'reason': clean(reason, 500).strip()}, 'proposed')
        return self._public(self._one('SELECT * FROM agent_worktrees WHERE worktree_id=?', (worktree_id,)))

    def binding(self, rid, agent_id):
        row = self._one('SELECT * FROM agent_worktrees WHERE run_id=? AND agent_id=?', (rid, agent_id))
        return self._public(row) if row else None

    def bindings(self, cid, rid=None):
        if rid is None:
            rows = self._rows('SELECT * FROM agent_worktrees WHERE chat_id=? ORDER BY created DESC LIMIT 100', (cid,))
        else:
            rows = self._rows('SELECT * FROM agent_worktrees WHERE chat_id=? AND run_id=? ORDER BY created DESC LIMIT 100', (cid, rid))
        return [self._public(row) for row in rows]

    # The owning child's read view and write adapter

    def workspace(self, worker, cancel=None):
        """A verified Workspace on the child's own worktree (private paths and links stay denied)."""
        return self._owned(worker, cancel if cancel is not None else worker.get('cancel') if isinstance(worker, dict) else None)[2]

    def write(self, worker, name, args, call_id):
        """WriteWorktreeFile/EditWorktreeFile for the owning generation; a receipt, never authority."""
        if name not in TOOL_ARGS:
            raise PolicyError('This is not a child worktree write tool.')
        if not isinstance(worker, dict):
            raise PolicyError('Child worktree access requires the host worker identity.')
        if not isinstance(args, dict) or set(args) - TOOL_ARGS[name] or not REQUIRED[name] <= set(args):
            raise PolicyError('Unexpected or missing worktree write argument.')
        self._ids(call_id)
        path = target(args['file_path'])
        expected = args.get('expected_hash')
        if expected is not None and (not isinstance(expected, str) or not (expected == 'absent' or HASH.fullmatch(expected))):
            raise PolicyError('expected_hash must be a file_hash from your Read, or "absent".')
        supplied = args['content'] if name == 'WriteWorktreeFile' else args['new_string']
        for value in [supplied] + ([args['old_string']] if name == 'EditWorktreeFile' else []):
            if not isinstance(value, str) or len(value) > MAX_FILE:
                raise PolicyError('Worktree writes take bounded text.')
            try:
                value.encode('utf-8')
            except UnicodeError:
                raise PolicyError('Worktree files must be valid UTF-8 text.') from None
        if name == 'EditWorktreeFile' and not args['old_string']:
            raise PolicyError('old_string must not be empty.')
        if SECRET.search(supplied):
            raise PolicyError('Credentials cannot be written by a child.')
        row, git_row, tree_ws, repo = self._owned(worker, worker.get('cancel'), ('files', 'edits'))
        tree_ws.path(path, create=True)  # The project Workspace's private and symlink rules.
        root, identity, parts = self.git.anchor / row['worktree_id'], git_row['path_identity'], path.split('/')
        # An ignored file would be invisible to export and integration checks.
        # check-ignore refuses literal pathspec magic; target() already refuses
        # ':' so no magic can be expressed, and the pathname matches literally.
        ignored = self.git._git(['check-ignore', '-q', '--', path], repo_root=repo['root'],
                                admin=repo['git_dir'] / 'worktrees' / row['worktree_id'], work_tree=root,
                                cancel=worker.get('cancel'), check=False, env={'GIT_LITERAL_PATHSPECS': '0'})
        if ignored['code'] == 0:
            raise PolicyError('Git ignores this path, so the root could not export it for integration. Choose an unignored path.')
        if ignored['code'] != 1:
            raise PolicyError('Git could not classify this worktree path: ' + excerpt(ignored['stderr']))
        write_id = fingerprint({'worktree_id': row['worktree_id'], 'call_id': call_id, 'tool': name, 'arguments': args})[:32]
        # The store lock spans registration, the effect and settlement, so no
        # settle, stop, retry or retirement interleaves with this write.
        with self.store.lock:
            with self.tree._transaction() as db:
                self._live_in(db, row, worker.get('owner'))
                stored = self._stored(db, row, call_id, write_id)
                if stored:
                    return stored
                if live_writer(db, str(root), identity):
                    raise PolicyError('A writable background command owns this worktree. Wait for it first.')
                fds = _parents(root, identity, parts, False)
                try:
                    before = _leaf(fds[-1], parts[-1])[0] if fds else None
                finally:
                    for fd in reversed(fds or []):
                        os.close(fd)
                data = self._content(name, args, before)
                before_hash = digest(before) if before is not None else None
                if expected is not None and expected != (before_hash or 'absent'):
                    raise PolicyError('The worktree file changed: expected_hash does not match. Read it again.')
                if before is not None and not self._seen(db, row, path, before_hash):
                    raise PolicyError('Read the current file in your worktree before replacing it.')
                self._bounds(db, row, path, len(data))
                db.execute('INSERT INTO agent_worktree_writes VALUES(?,?,?,?,?,?,?,?,NULL,?,?)',
                           (write_id, row['worktree_id'], row['run_id'], row['agent_id'], row['generation'], call_id,
                            path, before_hash, len(data), time.time()))
            try:
                self._replace(root, identity, parts, before, data)
            except Unconfirmed:
                raise  # Registered without after_hash: never replayed.
            except Exception:
                # Raised before the replacement: certainly no file change.
                with self.tree._transaction() as db:
                    db.execute('DELETE FROM agent_worktree_writes WHERE id=? AND after_hash IS NULL', (write_id,))
                raise
            with self.tree._transaction() as db:
                db.execute('UPDATE agent_worktree_writes SET after_hash=? WHERE id=?', (digest(data), write_id))
                return self._write_receipt(self._select(db, 'SELECT * FROM agent_worktree_writes WHERE id=?', (write_id,))[0])

    @staticmethod
    def _write_receipt(row):
        return {'worktree_id': row['worktree_id'], 'path': row['path'], 'call_id': row['call_id'],
                'generation': row['generation'], 'operation': 'create' if row['before_hash'] is None else 'replace',
                'before_hash': row['before_hash'], 'after_hash': row['after_hash'], 'bytes': row['bytes'],
                'unchanged': row['before_hash'] == row['after_hash'], 'project_changed': False,
                'origin': 'child_worktree', 'authorization': 'none', 'note': NOTE}

    def _stored(self, db, row, call_id, write_id):
        old = self._select(db, 'SELECT * FROM agent_worktree_writes WHERE worktree_id=? AND call_id=?', (row['worktree_id'], call_id))
        if not old:
            return None
        if old[0]['id'] != write_id or old[0]['generation'] != row['generation']:
            raise PolicyError('This call ID already wrote different arguments. Writes are never replayed.')
        if old[0]['after_hash'] is None:
            raise PolicyError('This write was registered but never confirmed. It is never replayed; inspect the worktree changes.')
        return self._write_receipt(old[0])

    @staticmethod
    def _content(name, args, before):
        if name == 'WriteWorktreeFile':
            text = args['content']
        else:
            if before is None:
                raise PolicyError('EditWorktreeFile needs an existing file; use WriteWorktreeFile to create one.')
            try:
                current = before.decode('utf-8')
            except UnicodeError:
                raise PolicyError('The worktree file is not UTF-8 text.') from None
            count = current.count(args['old_string'])
            if count != 1:
                raise PolicyError(f'old_string must match exactly once in the current file; it matched {count} times.')
            text = current.replace(args['old_string'], args['new_string'], 1)
        try:
            data = text.encode('utf-8')
        except UnicodeError:
            raise PolicyError('Worktree files must be valid UTF-8 text.') from None
        if len(data) > MAX_FILE or '\0' in text:
            raise PolicyError('A worktree file is UTF-8 text of at most 96 KB.')
        return data

    @staticmethod
    def _seen(db, row, path, file_hash):
        """This generation read exactly these bytes, or wrote them itself."""
        for (result,) in db.execute("SELECT result FROM agent_receipts WHERE run_id=? AND agent_id=? AND generation=? "
                                    "AND name='Read' AND state='completed' AND result IS NOT NULL",
                                    (row['run_id'], row['agent_id'], row['generation'])):
            try:
                value = json.loads(result)
            except ValueError:
                continue
            if (isinstance(value, dict) and value.get('file_hash') == file_hash and isinstance(value.get('path'), str)
                    and PurePosixPath(value['path']).as_posix() == path):
                return True
        return bool(db.execute('SELECT 1 FROM agent_worktree_writes WHERE worktree_id=? AND generation=? AND path=? AND after_hash=?',
                               (row['worktree_id'], row['generation'], path, file_hash)).fetchone())

    @staticmethod
    def _bounds(db, row, path, size):
        paths = {item for (item,) in db.execute('SELECT DISTINCT path FROM agent_worktree_writes WHERE worktree_id=?', (row['worktree_id'],))}
        if path not in paths and len(paths) >= MAX_PATHS:
            raise PolicyError(f'A child worktree accepts at most {MAX_PATHS} changed paths.')
        total = db.execute('SELECT coalesce(sum(bytes),0) FROM agent_worktree_writes WHERE worktree_id=?', (row['worktree_id'],)).fetchone()[0]
        if total + size > MAX_BYTES:
            raise PolicyError(f'This child worktree reached its {MAX_BYTES:,}-byte write budget.')
        count = db.execute('SELECT count(*) FROM agent_worktree_writes WHERE worktree_id=? AND generation=?',
                           (row['worktree_id'], row['generation'])).fetchone()[0]
        if count >= MAX_WRITES:
            raise PolicyError(f'This child generation reached its {MAX_WRITES}-write limit.')

    @staticmethod
    def _replace(root, identity, parts, before, data):
        """Atomic same-folder replacement through no-follow descriptors, read back."""
        fds = _parents(root, identity, parts, True)
        parent, leaf, temp = fds[-1], parts[-1], None
        try:
            current, info = _leaf(parent, leaf)
            if current != before:
                raise PolicyError('The worktree file changed during this write. Nothing was written.')
            if current == data:
                return False
            mode = info.st_mode & 0o777 if info else 0o644
            temp = TEMP + secrets.token_hex(8)
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
                out.flush()
                os.fchmod(out.fileno(), mode)
                os.fsync(out.fileno())
            if _leaf(parent, leaf)[0] != before:
                raise PolicyError('The worktree file changed during this write. Nothing was written.')
            os.replace(temp, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            temp = None
            try:
                os.fsync(parent)
                final = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                readback = _leaf(parent, leaf)[0]
            except (OSError, PolicyError) as exc:
                raise Unconfirmed('The worktree write could not be read back: ' + message(exc)) from None
            if not stat.S_ISREG(final.st_mode) or readback != data or final.st_mode & 0o777 != mode:
                raise Unconfirmed('The worktree file did not read back as written. Inspect it; nothing is retried.')
            return True
        finally:
            if temp is not None:
                try:
                    os.unlink(temp, dir_fd=parent)
                except OSError:
                    pass
            for fd in reversed(fds):
                os.close(fd)

    # Root review views

    def changes(self, cid, rid, agent_id, cancel=None):
        """Changed paths in the child's worktree; names and kinds only, never contents."""
        self._ids(cid, rid, agent_id)
        row = self._one('SELECT * FROM agent_worktrees WHERE chat_id=? AND run_id=? AND agent_id=?', (cid, rid, agent_id))
        if not row:
            raise PolicyError('This child has no managed worktree.')
        if row['state'] != 'active':
            raise PolicyError('This child worktree is not active.')
        run = self._run(cid, rid)
        self._permitted(run['config'], 'files')
        repo = self.git._repository(cid, self._root(run['config'], row), cancel)
        git_row = self.git._row(cid, row['worktree_id'])
        evidence = self.git._verify(repo, git_row, cancel)
        # Written files that Git ignores or normalizes away stay visible to the root.
        entries = evidence['changes'] + self._hidden(repo, row, self.git.anchor / row['worktree_id'], git_row['path_identity'],
                                                     evidence, cancel)
        visible = [entry for entry in entries if not private(entry['path'])]
        return {'worktree_id': row['worktree_id'], 'clean': evidence['clean'] and len(entries) == len(evidence['changes']),
                'changes': [{'path': display(entry['path']), 'kind': change_kind(entry)} for entry in visible[:200]],
                'change_count': len(visible), 'private_omitted': len(entries) - len(visible),
                'truncated': evidence['truncated'] or len(visible) > 200, 'origin': 'child_worktree', 'authorization': 'none'}

    def guard(self, operation, row):
        """GitWorkflows ownership guard: refuses only; unbound worktrees pass unchanged."""
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            raise PolicyError('Managed worktree guard received an unknown record.')
        with self.store.lock:
            db = self.store.db
            bound = db.execute('SELECT run_id,agent_id,generation,state FROM agent_worktrees WHERE worktree_id=?', (row['id'],)).fetchone()
            node = bound and db.execute('SELECT status,generation,review FROM agent_nodes WHERE id=? AND run_id=?',
                                        (bound[1], bound[0])).fetchone()
        if not bound:
            return
        if bound[3] in ('allocating', 'retiring'):
            raise PolicyError('A child worktree allocation or retirement is in progress. Wait for it, then inspect.')
        if node and node[0] in ACTIVE:
            raise PolicyError('This managed worktree belongs to a live child agent. Settle or stop the child before export, removal or forgetting.')
        if operation == 'export' and (not node or node[1] != bound[2] or node[0] != 'completed' or node[2] != 'accepted'):
            raise PolicyError('Export a child worktree only after its child completed and the root accepted that result.')

    # Retirement, closure and deletion

    def retire(self, cid, rid, agent_id, discard_review=False, cancel=None):
        """Remove a settled child's worktree once integrated, or after a native discard review."""
        self._ids(cid, rid, agent_id)
        self.git._permit('files', 'edits')
        row = self._one('SELECT * FROM agent_worktrees WHERE chat_id=? AND run_id=? AND agent_id=?', (cid, rid, agent_id))
        if not row:
            raise PolicyError('This child has no managed worktree.')
        if row['state'] != 'active':
            raise PolicyError('Only an active child worktree can be retired.')
        with self.store.lock:
            node = self.store.db.execute('SELECT status FROM agent_nodes WHERE id=? AND run_id=?', (agent_id, rid)).fetchone()
        if not node or node[0] not in TERMINAL:
            raise PolicyError('Retire a child worktree only after its child settled or was stopped.')
        ws = self._root(self._run(cid, rid)['config'], row)
        repo = self.git._repository(cid, ws, cancel)
        ident = row['worktree_id']
        path, admin = self.git.anchor / ident, repo['git_dir'] / 'worktrees' / ident
        try:
            git_row = self.git._row(cid, ident)
        except PolicyError:
            git_row = None  # Forgotten after a native removal.
        if (git_row is None or git_row['state'] == 'removed') and not any(self._present(row).values()):
            self._settle(row, 'retired', {'phase': 'removed through native Git review'}, 'active')
            return {'worktree_id': ident, 'state': 'retired', 'path': str(path), 'integrated': [], 'integrated_count': 0,
                    'preserved': [], 'discard_review': False, 'ignored_deleted': 0, 'origin': 'child_worktree',
                    'authorization': 'none'}
        if git_row['state'] != 'active':
            raise PolicyError('The managed worktree record is not active. Inspect it natively.')
        writer = self.git._writer(repo)
        try:
            evidence = self.git._verify(repo, git_row, cancel)
            if evidence['truncated']:
                raise PolicyError('The worktree status exceeded its bound. Inspect it natively.')
            integrated, pending, blocked = [], [], []
            # Written files that Git ignores or normalizes away would otherwise vanish with --force.
            for entry in evidence['changes'] + self._hidden(repo, row, path, git_row['path_identity'], evidence, cancel):
                name = entry['path']
                mine = _probe(path, git_row['path_identity'], name)
                theirs = _probe(ws.root, ws.identity, name)
                if entry['kind'] == 'unmerged' or entry.get('submodule') or mine[0] == 'other':
                    blocked.append(name)
                elif mine[0] == theirs[0] == 'absent' or (mine[0] == theirs[0] == 'file' and digest(mine[1]) == digest(theirs[1])):
                    integrated.append(name)
                else:
                    pending.append((name, mine[1]))
            if blocked:
                raise PolicyError(f'{len(blocked)} worktree path(s) are not regular files Pebble can compare or preserve: '
                                  + ', '.join(display(n) for n in blocked[:20]) + '. Inspect them natively.')
            if pending and discard_review is not True:
                raise PolicyError(f'{len(pending)} worktree change(s) are not integrated into the project: '
                                  + ', '.join(display(n) for n, _ in pending[:20])
                                  + '. Integrate them first, or discard them through a native review that keeps recoverable blobs.')
            preserved = self._preserve(repo, path, pending, cancel) if pending else []
            facts = {'integrated': [display(n) for n in integrated[:200]], 'integrated_count': len(integrated),
                     'preserved': preserved, 'discard_review': bool(pending), 'ignored_deleted': len(evidence['ignored'])}
            with self.tree._transaction() as db:
                self._set(db, row, 'retiring', {'phase': 'retirement registered before Git ran', **facts}, 'active')
                if db.execute("UPDATE git_worktrees SET state='removing',evidence=?,updated=? WHERE id=? AND state='active'",
                              (canonical({'phase': 'child retirement registered before Git ran', 'previous': 'active'}),
                               time.time(), ident)).rowcount != 1:
                    raise PolicyError('The managed worktree record changed. Nothing was removed.')
            try:
                result = self.git._git(['worktree', 'remove', '--force', str(path)], repo_root=repo['root'],
                                       writes=[repo['git_dir'] / 'worktrees', path], cancel=cancel, timeout=WRITE_SECONDS, check=False)
            except Exception as exc:
                state = self.git._settle(ident, path, admin, 'removing', exc)
                self._settle(row, 'retired' if state == 'removed' else 'uncertain',
                             {'phase': 'retirement interrupted', 'error': message(exc), **facts}, 'retiring')
                raise
            if result['code'] != 0:
                error = PolicyError('Git refused to remove the child worktree: ' + excerpt(result['stderr']))
                try:
                    intact = self.git._verify(repo, git_row, cancel)['path_identity'] == evidence['path_identity']
                except Exception:
                    intact = False
                if intact:
                    self.git._mark(ident, 'active', {'phase': 'child retirement refused; folder verified intact', 'error': message(error)})
                    self._settle(row, 'active', {'phase': 'retirement refused; folder verified intact', 'error': message(error),
                                                 **facts}, 'retiring')
                else:
                    state = self.git._settle(ident, path, admin, 'removing', error)
                    self._settle(row, 'retired' if state == 'removed' else 'uncertain',
                                 {'phase': 'retirement refused', 'error': message(error), **facts}, 'retiring')
                raise error
            state = 'retired' if self.git._settle(ident, path, admin, 'removing', None) == 'removed' else 'uncertain'
            self._settle(row, state, {'phase': 'retired' if state == 'retired' else 'removal not confirmed',
                                      'present': self._present(row), **facts}, 'retiring')
        finally:
            writer.release()
        return {'worktree_id': ident, 'state': state, 'path': str(path), **facts, 'origin': 'child_worktree',
                'authorization': 'none'}

    def _preserve(self, repo, folder, pending, cancel):
        """Write each non-integrated file into the project object database and prove the exact bytes."""
        files = [(name, data) for name, data in pending if data is not None]
        if len(files) > MAX_PRESERVE:
            raise PolicyError(f'At most {MAX_PRESERVE} files are preserved by a discard review. Inspect the worktree natively.')
        oids = []
        if files:
            result = self.git._git(['hash-object', '-w', '--no-filters', '--', *[str(folder / name) for name, _ in files]],
                                   repo_root=repo['root'], reads=[folder], writes=[repo['git_dir'] / 'objects'], cancel=cancel,
                                   timeout=WRITE_SECONDS)
            oids = result['stdout'].decode('ascii', 'replace').split()
            if len(oids) != len(files) or any(oid != blob_oid(data) for oid, (_, data) in zip(oids, files)):
                raise PolicyError('Git did not preserve the exact worktree bytes. Nothing was removed.')
        return ([{'path': display(name), 'blob_oid': oid, 'sha256': digest(data), 'bytes': len(data)}
                 for oid, (name, data) in zip(oids, files)]
                + [{'path': display(name), 'deleted': True, 'blob_oid': None, 'sha256': None} for name, data in pending if data is None])

    def close_run(self, rid):
        """After root closure: reject pending proposals; active worktrees stay for human review."""
        with self.tree._transaction() as db:
            rows = self._select(db, "SELECT * FROM agent_worktrees WHERE run_id=? AND state='proposed'", (rid,))
            for row in rows:
                self._set(db, row, 'rejected', {'phase': 'rejected', 'reason': 'The root closed before allocation.'}, 'proposed')
        return len(rows)

    def assert_chat_deletable(self, cid):
        blocking = [row['worktree_id'] for row in self._rows('SELECT * FROM agent_worktrees WHERE chat_id=?', (cid,))
                    if row['state'] in ('allocating', 'retiring', 'uncertain')
                    or (row['state'] == 'active' and any(self._present(row).values()))]
        if blocking:
            raise PolicyError(f'This conversation still owns {len(blocking)} child worktree record(s) that are in flight, '
                              'uncertain or hold a folder. Integrate and retire them, or inspect them natively, before deleting it.')
        return {'deletable': True, 'authorization': 'none'}

    def forget_chat_records(self, cid):
        """Delete only settled rows: failed, rejected or retired (and their write receipts)."""
        self.assert_chat_deletable(cid)
        with self.tree._transaction() as db:
            for row in self._select(db, 'SELECT * FROM agent_worktrees WHERE chat_id=?', (cid,)):
                if row['state'] == 'proposed':
                    self._set(db, row, 'rejected', {'phase': 'rejected', 'reason': 'The conversation was deleted before allocation.'}, 'proposed')
                elif row['state'] == 'active' and not any(self._present(row).values()):
                    self._set(db, row, 'retired', {'phase': 'neither folder nor Git metadata remains'}, 'active')
            ids = [ident for (ident,) in db.execute("SELECT worktree_id FROM agent_worktrees WHERE chat_id=? AND state IN (?,?,?)",
                                                    (cid, *SETTLED))]
            for ident in ids:
                db.execute('DELETE FROM agent_worktree_writes WHERE worktree_id=?', (ident,))
                db.execute('DELETE FROM agent_worktrees WHERE worktree_id=?', (ident,))
        return {'forgotten': ids, 'authorization': 'none'}

    def reconcile(self, cid):
        """Native inspection: settle uncertain rows that own nothing on disk. Never retries or deletes."""
        settled = []
        with self.tree._transaction() as db:
            for row in self._select(db, "SELECT * FROM agent_worktrees WHERE chat_id=? AND state='uncertain'", (cid,)):
                if any(self._present(row).values()):
                    continue
                evidence = json.loads(row['evidence'])
                allocation = evidence.get('interrupted') == 'allocating' or evidence.get('phase') == 'allocation failed'
                self._set(db, row, 'failed' if allocation else 'retired',
                          {'phase': 'inspected: neither folder nor Git metadata remains', 'previous': self._previous(row)}, 'uncertain')
                settled.append(row['worktree_id'])
        return {'settled': settled, 'authorization': 'none'}
