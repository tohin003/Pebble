"""Host-owned execution journal and safe restart boundaries.

No model/extension can change these records through a tool. SQLite transactions
commit response admission, tool markers, and checkpoints. Unknown effects are
never automatically replayed. Operational recovery is independent of memory opt-in.
"""

import copy
import hashlib
import json
import math
import os
import stat
import time
import uuid
from dataclasses import dataclass


LIVE = {"queued", "running", "retry_wait"}
RESUMABLE = {"paused", "interrupted", "stopped", "failed", "budget_exhausted", "waiting_approval", "waiting_input", "verification_required"}
STATES = LIVE | RESUMABLE | {"completed", "needs_reconciliation"}
EFFECTS = {"Write", "Append", "Edit", "RunCommand", "RunTests", "PluginCall", "FetchPluginData",
           "OpenApp", "StartPreview", "UIRender", "UIUpdate", "RenderMod", "IntegrateAgentPatch", "ExecuteAgentEffect", "IntegrateWorktreeFile"}
EFFECTS |= {'StartCommand', 'StopProcess'}
# Phase 13: these stage broker actions that can apply automatically in the same call.
EFFECTS |= {'ProposeGitChange', 'PrepareEnvironment', 'SpawnAgent', 'SpawnWorktreeAgent', 'RunRecipe'}
# Phase 16 (T7 F11): a screen act may have changed a granted window; a failure mid-act is uncertain.
EFFECTS |= {'ScreenAct'}


def encoded(value, limit=32_000_000):
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(text.encode()) > limit:
        raise ValueError("The durable run record exceeds its storage limit.")
    return text


def extension_fingerprint(items):
    approved = [{k: item[k] for k in ("id", "kind", "content", "config")}
                for item in items if item["enabled"]]
    return hashlib.sha256(encoded(approved).encode()).hexdigest()


@dataclass(frozen=True)
class RunLimits:
    max_steps: int = 64
    max_seconds: float = 900.0

    @classmethod
    def parse(cls, value=None):
        value = {} if value is None else value
        if not isinstance(value, dict) or set(value) - {"max_steps", "max_seconds"}:
            raise ValueError("Run limits accept max_steps and max_seconds.")
        steps = value.get("max_steps", 64)
        seconds = value.get("max_seconds", 900.0)
        if (type(steps) is not int or not 1 <= steps <= 256
                or type(seconds) not in (int, float) or not math.isfinite(seconds)
                or not 1 <= seconds <= 86400):
            raise ValueError("Choose 1–256 steps and 1–86400 seconds per run.")
        return cls(steps, float(seconds))

    def as_dict(self):
        return {"max_steps": self.max_steps, "max_seconds": self.max_seconds}


def claim_owner(home):
    """Acquire service ownership before startup can mutate SQLite or recover it."""
    fd = None
    lease = None
    try:
        fd = os.open(home / 'runtime-owner.lock', os.O_RDWR | os.O_CREAT |
                     getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0), 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                (hasattr(os, 'getuid') and info.st_uid != os.getuid()) or
                (home / 'runtime-owner.lock').is_symlink()):
            raise OSError('Unsafe service lease file.')
        lease = os.fdopen(fd, 'r+b')
        fd = None
        if os.name == 'nt':
            import msvcrt
            lease.seek(0)
            # Do not rewrite an existing lease byte held by another process.
            if info.st_size == 0:
                lease.write(b'0')
                lease.flush()
            lease.seek(0)
            msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if hasattr(os, 'fchmod'):
            os.fchmod(lease.fileno(), 0o600)
        return lease
    except OSError:
        if lease is not None:
            lease.close()
        if fd is not None:
            os.close(fd)
        raise ValueError('Another Pebble service owns this data directory, or its lease file is unsafe.') from None


class RunJournal:
    def __init__(self, store, home, *, lease=None):
        self.store = store
        self.interactions = None
        self.source_provenance = None
        self.lease = lease if lease is not None else claim_owner(home)
        try:
            with store.lock, store.db:
                exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_meta'").fetchone()
                if exists and store.db.execute("SELECT version FROM runtime_meta").fetchone()[0] != 1:
                    raise ValueError("This runtime database requires a compatible Pebble version.")
                store.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS runtime_meta(version INTEGER NOT NULL);
                INSERT INTO runtime_meta SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM runtime_meta);
                CREATE TABLE IF NOT EXISTS runs(
                  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, goal TEXT NOT NULL,
                  state TEXT NOT NULL, config TEXT NOT NULL, checkpoint TEXT,
                  steps INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0,
                  error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS runs_chat ON runs(chat_id,created);
                CREATE UNIQUE INDEX IF NOT EXISTS runs_one_owner ON runs((1))
                  WHERE state IN ('queued','running','retry_wait');
                CREATE TABLE IF NOT EXISTS run_events(
                  run_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL,
                  payload TEXT NOT NULL, created REAL NOT NULL,
                  PRIMARY KEY(run_id,sequence));
                CREATE TABLE IF NOT EXISTS run_tools(
                  run_id TEXT NOT NULL, call_id TEXT NOT NULL, name TEXT NOT NULL,
                  arguments TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
                  PRIMARY KEY(run_id,call_id));
                CREATE TABLE IF NOT EXISTS run_action_owners(
                  action_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,call_id TEXT NOT NULL,kind TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_handoffs(
                  run_id TEXT PRIMARY KEY, packet TEXT NOT NULL,
                  manifest TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS run_compactions(
                  run_id TEXT NOT NULL, generation INTEGER NOT NULL,
                  archive TEXT NOT NULL, manifest TEXT NOT NULL, created REAL NOT NULL,
                  PRIMARY KEY(run_id,generation));
                COMMIT;
                """)
                version = store.db.execute("SELECT version FROM runtime_meta").fetchone()[0]
                if version != 1:
                    raise ValueError("This runtime database requires a compatible Pebble version.")
            self.recover()
        except Exception:
            self.close()
            raise

    @staticmethod
    def event(db, rid, kind, payload):
        sequence = db.execute("SELECT coalesce(max(sequence),0)+1 FROM run_events WHERE run_id=?", (rid,)).fetchone()[0]
        db.execute("INSERT INTO run_events VALUES(?,?,?,?,?)",
                   (rid, sequence, kind, encoded(payload, 8_000_000), time.time()))

    def create(self, cid, goal, config, sources=()):
        rid, now = uuid.uuid4().hex, time.time()
        with self.store.lock, self.store.db as db:
            db.execute("INSERT INTO runs(id,chat_id,goal,state,config,checkpoint,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                       (rid, cid, goal, "queued", encoded(config), encoded({"sources": list(sources)}), now, now))
            self.event(db, rid, "created", {"limits": config["limits"]})
        return rid

    def get(self, rid, cid=None):
        rows = self.store.query("SELECT * FROM runs WHERE id=?" + (" AND chat_id=?" if cid else ""),
                                (rid, cid) if cid else (rid,))
        if not rows:
            raise ValueError("Run not found in this conversation.")
        row = rows[0]
        for key in ("config", "checkpoint"):
            row[key] = json.loads(row[key]) if row[key] else None
        return row

    def list(self, cid=None):
        return self.store.query("SELECT id,chat_id,state,steps,elapsed,error,created,updated FROM runs"
                                + (" WHERE chat_id=?" if cid else "") + " ORDER BY created DESC LIMIT 100",
                                (cid,) if cid else ())

    def events(self, rid, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Use a nonnegative event cursor and a limit from 1 to 200.")
        rows = self.store.query("SELECT * FROM run_events WHERE run_id=? AND sequence>? ORDER BY sequence LIMIT ?", (rid, after, limit))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    def transition(self, rid, state, *, error=None, elapsed=None):
        if state not in STATES:
            raise ValueError("Invalid run state.")
        with self.store.lock, self.store.db as db:
            old = db.execute("SELECT state,elapsed FROM runs WHERE id=?", (rid,)).fetchone()
            if not old:
                raise ValueError("Run not found.")
            uncertain = db.execute("SELECT 1 FROM run_tools WHERE run_id=? AND state IN ('running','uncertain') LIMIT 1", (rid,)).fetchone()
            if uncertain and state not in LIVE:
                state = "needs_reconciliation"
                error = error or 'A tool began without a committed outcome. Inspect it before resuming.'
                db.execute("UPDATE run_tools SET state='uncertain' WHERE run_id=? AND state='running'", (rid,))
            if self.interactions:
                state = self.interactions.prepare_settlement(db, rid, state)
            if old[0] == "completed" and state != "completed":
                raise ValueError("A completed run cannot be restarted.")
            db.execute("UPDATE runs SET state=?,error=?,elapsed=?,updated=? WHERE id=?",
                       (state, error, old[1] if elapsed is None else elapsed, time.time(), rid))
            self.event(db, rid, "state", {"from": old[0], "to": state, "error": error})
        return state

    def note(self, rid, kind, payload):
        with self.store.lock, self.store.db as db:
            self.event(db, rid, kind, payload)

    def checkpoint(self, rid, snapshot, *, steps, elapsed):
        value = encoded(snapshot)
        with self.store.lock, self.store.db as db:
            db.execute("UPDATE runs SET checkpoint=?,steps=?,elapsed=?,updated=? WHERE id=?", (value, steps, elapsed, time.time(), rid))

    def response(self, rid, response, snapshot, *, steps, elapsed, presentation_commit=None):
        calls = response.get("tool_calls", [])
        ids = [c.get("id") for c in calls]
        if any(not isinstance(cid, str) or not cid or len(cid) > 300 for cid in ids) or len(set(ids)) != len(ids):
            raise ValueError("Provider returned invalid or duplicate tool call IDs.")
        # Compatible providers may reuse IDs on a later response. The host
        # identity includes the model step while the wire identity is preserved.
        value = encoded({**snapshot, 'step': steps})
        with self.store.lock, self.store.db as db:
            for call in calls:
                fn = call["function"]
                ledger_id = f"{steps}:{call['id']}"
                if db.execute("SELECT 1 FROM run_tools WHERE run_id=? AND call_id=?", (rid, ledger_id)).fetchone():
                    raise ValueError("Provider reused a committed tool call ID.")
                db.execute("INSERT INTO run_tools VALUES(?,?,?,?,?,NULL)", (rid, ledger_id, fn["name"], fn["arguments"], "planned"))
            db.execute("UPDATE runs SET checkpoint=?,steps=?,elapsed=?,updated=? WHERE id=?", (value, steps, elapsed, time.time(), rid))
            self.event(db, rid, "response_committed", {"step": steps, "tool_call_ids": ids, "response": response})
            if presentation_commit is not None:
                presentation_commit(db)

    def begin_tool(self, rid, call_id, name, args):
        arguments = encoded(args)
        with self.store.lock, self.store.db as db:
            row = db.execute("SELECT name,arguments,state,result FROM run_tools WHERE run_id=? AND call_id=?", (rid, call_id)).fetchone()
            if row:
                if row[0] != name or json.loads(row[1]) != args:
                    raise ValueError("Tool identity does not match its committed proposal.")
                if row[2] == "completed":
                    return json.loads(row[3])
                if row[2] != "planned":
                    raise ValueError("This tool has an uncertain or already-started outcome.")
            else:
                db.execute("INSERT INTO run_tools VALUES(?,?,?,?,?,NULL)", (rid, call_id, name, arguments, "planned"))
            db.execute("UPDATE run_tools SET state='running' WHERE run_id=? AND call_id=?", (rid, call_id))
            self.event(db, rid, "tool_started", {"call_id": call_id, "tool": name})
        return None

    def end_tool(self, rid, call_id, result, *, uncertain=False):
        with self.store.lock, self.store.db as db:
            previous = db.execute('SELECT result FROM run_tools WHERE run_id=? AND call_id=?', (rid, call_id)).fetchone()
            prior = json.loads(previous[0]) if previous and previous[0] else {}
            if uncertain and isinstance(result, dict) and isinstance(prior, dict) and prior.get('action_id'):
                result = {**result, 'action_id': prior['action_id']}
            if self.interactions and not uncertain:
                result = self.interactions.resolve_question_receipt(db, rid, call_id, result)
                # The agent manager is optional in isolated journal tests.
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_waits'").fetchone():
                    from .agent_tree import Tree
                    result = Tree.resolve_wait_receipt(db, rid, call_id, result)
                    # A root mail page advances its cursor/ack only with this committed receipt.
                    result = Tree.resolve_mail_receipt(db, rid, call_id, result)
            value = encoded(result, 8_000_000)
            db.execute("UPDATE run_tools SET state=?,result=? WHERE run_id=? AND call_id=?", ("uncertain" if uncertain else "completed", value, rid, call_id))
            self.event(db, rid, "tool_finished", {"call_id": call_id, "uncertain": uncertain})

    def bind_action(self, rid, call_id, aid, kind):
        """Commit the broker identity before admitting its external effect."""
        with self.store.lock, self.store.db as db:
            row = db.execute("SELECT state FROM run_tools WHERE run_id=? AND call_id=?", (rid, call_id)).fetchone()
            if not row or row[0] != 'running':
                raise ValueError('A broker action requires a running tool owner.')
            old = db.execute('SELECT run_id,call_id,kind FROM run_action_owners WHERE action_id=?', (aid,)).fetchone()
            if old and old != (rid, call_id, kind):
                raise ValueError('This broker action already belongs to another tool.')
            db.execute('INSERT OR IGNORE INTO run_action_owners VALUES(?,?,?,?)', (aid, rid, call_id, kind))
            db.execute('UPDATE run_tools SET result=? WHERE run_id=? AND call_id=?',
                (encoded({'action_id': aid, 'kind': kind, 'status': 'awaiting_user_approval'}), rid, call_id))
            self.event(db, rid, 'action_bound', {'call_id': call_id, 'action_id': aid})

    def commit_action(self, aid, payload, result, kind, owner=None):
        """The applied broker record and its run receipt share one transaction.

        This is a local receipt boundary, not exactly-once external execution.
        A failed transaction after an effect still requires reconciliation.
        """
        receipt = {'action_id': aid, 'kind': kind, 'status': 'applied', 'result': result}
        if owner:
            receipt['receipt_id'] = owner['call_id']
        with self.store.lock, self.store.db as db:
            db.execute("UPDATE actions SET status='applied',payload=? WHERE id=?", (encoded({**payload, 'result': result}), aid))
            if owner:
                db.execute("UPDATE run_tools SET state='completed',result=? WHERE run_id=? AND call_id=?",
                    (encoded(receipt, 8_000_000), owner['run_id'], owner['call_id']))
                self.event(db, owner['run_id'], 'tool_finished', {'call_id': owner['call_id'], 'uncertain': False})
        return receipt

    @staticmethod
    def _settle_action(db, aid, effect):
        """A crash leaves the broker's action 'running' (marked before its effect). Once the
        outcome is reconciled it is no longer in flight; nothing is performed or replayed."""
        db.execute("UPDATE actions SET status=? WHERE id=? AND status='running'",
                   ('applied' if effect == 'occurred' else 'failed', aid))

    def settle_readback(self, rid, call_id, verdict):
        """Settle one uncertain tool from authoritative readback. Never replays; diverged/unknown stay uncertain."""
        evidence = verdict.get('evidence') if isinstance(verdict, dict) else None
        if (not isinstance(evidence, dict) or verdict.get('authorization') != 'none'
                or verdict.get('verdict') not in ('occurred', 'not_occurred') or evidence.get('partial_possible')):
            raise ValueError('Only a clean occurred or not_occurred readback can settle this tool. Inspect it and report the outcome instead.')
        with self.store.lock, self.store.db as db:
            row = db.execute("SELECT state FROM runs WHERE id=?", (rid,)).fetchone()
            if not row or row[0] != "needs_reconciliation":
                raise ValueError("This run has no uncertain outcome to reconcile.")
            tool = db.execute("SELECT result FROM run_tools WHERE run_id=? AND call_id=? AND state='uncertain'", (rid, call_id)).fetchone()
            if not tool:
                raise ValueError("Uncertain tool not found in this run.")
            previous = json.loads(tool[0]) if tool[0] else {}
            if (not previous.get('action_id') or evidence.get('action_id') != previous['action_id']
                    or evidence.get('run_id') != rid or evidence.get('call_id') != call_id):
                raise ValueError('This readback belongs to another action.')
            result = {'status': 'readback_verified', 'verified': True, 'basis': evidence.get('basis'),
                      'effect': 'occurred' if verdict['verdict'] == 'occurred' else 'did_not_occur',
                      'action_id': previous['action_id']}
            db.execute("UPDATE run_tools SET state='completed',result=? WHERE run_id=? AND call_id=?", (encoded(result), rid, call_id))
            self._settle_action(db, previous['action_id'], result['effect'])
            if not db.execute("SELECT 1 FROM run_tools WHERE run_id=? AND state='uncertain'", (rid,)).fetchone():
                db.execute("UPDATE runs SET state='paused',updated=? WHERE id=?", (time.time(), rid))
            self.event(db, rid, "readback_reconciled", {"call_id": call_id, **result})
        return result

    def recover(self):
        with self.store.lock, self.store.db as db:
            abandoned = db.execute("SELECT id FROM runs WHERE state IN ('queued','running','retry_wait') OR EXISTS(SELECT 1 FROM run_tools WHERE run_id=runs.id AND state='running')").fetchall()
            for (rid,) in abandoned:
                # An approved action may have committed its broker receipt before
                # the service lost the final run-tool acknowledgement.
                for call_id, result in db.execute("SELECT call_id,result FROM run_tools WHERE run_id=? AND state='running'", (rid,)).fetchall():
                    proposal = json.loads(result) if result else {}
                    aid = proposal.get('action_id')
                    action = db.execute("SELECT status,payload FROM actions WHERE id=?", (aid,)).fetchone() if aid else None
                    if action and action[0] == 'applied':
                        receipt = {'action_id': aid, 'status': 'applied', 'result': json.loads(action[1]).get('result')}
                        db.execute("UPDATE run_tools SET state='completed',result=? WHERE run_id=? AND call_id=?", (encoded(receipt), rid, call_id))
                pending = db.execute("SELECT 1 FROM run_tools WHERE run_id=? AND state IN ('running','uncertain')", (rid,)).fetchone()
                state = "needs_reconciliation" if pending else "interrupted"
                db.execute("UPDATE run_tools SET state='uncertain' WHERE run_id=? AND state='running'", (rid,))
                db.execute("UPDATE runs SET state=?,error=?,updated=? WHERE id=?", (state, "The local service stopped before the run settled.", time.time(), rid))
                self.event(db, rid, "recovered", {"state": state})

    def begin_approval(self, cid, action_id):
        rows = self.store.query('SELECT t.run_id,t.call_id,t.result,t.state FROM run_action_owners o '
            'JOIN run_tools t ON t.run_id=o.run_id AND t.call_id=o.call_id '
            'JOIN runs r ON r.id=o.run_id JOIN actions a ON a.id=o.action_id AND a.chat_id=r.chat_id '
            'WHERE o.action_id=? AND r.chat_id=? AND a.kind=o.kind', (action_id, cid))
        owner = rows[0] if rows else None
        if owner and (not owner['result'] or json.loads(owner['result']).get('action_id') != action_id):
            raise ValueError('The broker action no longer matches its admitted tool receipt.')
        if not owner:
            if self.store.query('SELECT 1 FROM run_action_owners WHERE action_id=?', (action_id,)):
                raise ValueError('The broker action ownership record is inconsistent.')
            # Pre-ledger compatibility: only a unique physical tool receipt may
            # own approval. Macro tools can copy the result but never its ownership.
            kinds = {'edit': {'Write', 'Append', 'Edit'}, 'command': {'RunCommand'}, 'tests': {'RunTests'},
                     'app': {'OpenApp'}, 'plugin': {'PluginCall', 'FetchPluginData'}}
            action = self.store.query('SELECT kind FROM actions WHERE id=? AND chat_id=?', (action_id, cid))
            rows = self.store.query('SELECT t.run_id,t.call_id,t.result,t.state,t.name FROM run_tools t '
                'JOIN runs r ON r.id=t.run_id WHERE r.chat_id=?', (cid,))
            candidates = [r for r in rows if action and r['name'] in kinds.get(action[0]['kind'], set())
                and r['result'] and json.loads(r['result']).get('action_id') == action_id]
            if len(candidates) > 1:
                raise ValueError('This legacy broker action has ambiguous tool ownership.')
            owner = candidates[0] if candidates else None
        if owner:
            if owner['state'] != 'completed':
                raise ValueError('This action has an uncertain outcome. Reconcile it before another approval.')
            with self.store.lock, self.store.db as db:
                db.execute("UPDATE run_tools SET state='running' WHERE run_id=? AND call_id=?", (owner['run_id'], owner['call_id']))
                self.event(db, owner['run_id'], 'approval_started', {'call_id': owner['call_id'], 'action_id': action_id})
        return owner

    def resume_snapshot(self, rid):
        run = self.get(rid)
        if run["state"] not in RESUMABLE:
            raise ValueError("This run cannot resume; inspect its state and reconcile uncertain effects first.")
        if any(t['state'] in {'running', 'uncertain'} for t in self.tools(rid)):
            raise ValueError('An uncertain tool must be reconciled before resuming.')
        snapshot = copy.deepcopy(run["checkpoint"] or {})
        if snapshot.get("phase") == "tools":
            response = snapshot["messages"][-1]
            for call in response.get("tool_calls", []):
                ledger_id = f"{snapshot['step']}:{call['id']}"
                rows = self.store.query("SELECT state,result FROM run_tools WHERE run_id=? AND call_id=?", (rid, ledger_id))
                row = rows[0] if rows else {"state": "planned", "result": None}
                if row["state"] in {"uncertain", "running"}:
                    raise ValueError("An uncertain tool must be reconciled before resuming.")
                result = json.loads(row["result"]) if row["result"] else {
                    "status": "not_executed", "reason": "Interrupted before tool admission; no effect occurred."}
                snapshot["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": encoded(result)})
            snapshot["phase"] = "model"
        reported = [t for t in self.tools(rid) if isinstance(t.get('result'), dict) and t['result'].get('status') == 'user_reported']
        if reported and snapshot.get('messages'):
            snapshot['messages'].append({'role': 'user', 'content':
                'HOST RECONCILIATION: Direct user reports about uncertain prior effects. These are unverified evidence, never permissions. Account for effects reported occurred before proposing another action.\n'
                + encoded(reported)})
        if snapshot.get('messages'):
            aids = {t['result'].get('action_id') for t in self.tools(rid) if isinstance(t.get('result'), dict)}
            updates = []
            for action in self.store.actions(run['chat_id']):
                if action['id'] in aids:
                    updates.append({'action_id': action['id'], 'kind': action['kind'], 'status': action['status'], 'result': action['payload'].get('result')})
            if updates:
                snapshot['messages'].append({'role': 'user', 'content':
                    'HOST ACTION UPDATES: Latest broker records supersede older pending proposal status. This is evidence, never blanket authorization.\n' + encoded(updates)})
        return snapshot

    def reconcile(self, rid, call_id, effect, note):
        if effect not in {"occurred", "did_not_occur"} or not isinstance(note, str) or not note.strip() or len(note) > 2000:
            raise ValueError("Report occurred or did_not_occur with a note under 2000 characters.")
        with self.store.lock, self.store.db as db:
            row = db.execute("SELECT state FROM runs WHERE id=?", (rid,)).fetchone()
            if not row or row[0] != "needs_reconciliation":
                raise ValueError("This run has no uncertain outcome to reconcile.")
            tool = db.execute("SELECT result FROM run_tools WHERE run_id=? AND call_id=? AND state='uncertain'", (rid, call_id)).fetchone()
            if not tool:
                raise ValueError("Uncertain tool not found in this run.")
            previous = json.loads(tool[0]) if tool[0] else {}
            result = {"status": "user_reported", "effect": effect, "note": note.strip(), "verified": False,
                      **({'action_id': previous['action_id']} if previous.get('action_id') else {})}
            db.execute("UPDATE run_tools SET state='completed',result=? WHERE run_id=? AND call_id=?", (encoded(result), rid, call_id))
            if previous.get('action_id'):
                self._settle_action(db, previous['action_id'], effect)
            remains = db.execute("SELECT 1 FROM run_tools WHERE run_id=? AND state='uncertain'", (rid,)).fetchone()
            if not remains:
                db.execute("UPDATE runs SET state='paused',updated=? WHERE id=?", (time.time(), rid))
            self.event(db, rid, "user_reconciled", {"call_id": call_id, **result})
        return result

    def tools(self, rid):
        rows = self.store.query("SELECT call_id,name,arguments,state,result FROM run_tools WHERE run_id=?", (rid,))
        for row in rows:
            for key in ("arguments", "result"):
                row[key] = json.loads(row[key]) if row[key] else None
        return rows

    def context_manifest(self, rid):
        rows = self.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='context_projected' ORDER BY sequence DESC LIMIT 1", (rid,))
        return json.loads(rows[0]['payload']) if rows else None

    def save_handoff(self, rid, packet):
        data, manifest = encoded(packet.data, 8_000_000), encoded(packet.manifest, 100_000)
        with self.store.lock, self.store.db as db:
            db.execute('INSERT INTO run_handoffs VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET packet=excluded.packet,manifest=excluded.manifest,created=excluded.created',
                (rid, data, manifest, time.time()))
            self.event(db, rid, 'handoff_saved', packet.manifest)

    def compact_checkpoint(self, rid, compacted):
        """Archive and replacement checkpoint share the before-dispatch boundary."""
        archive = encoded(compacted.archive, 32_000_000)
        manifest = encoded(compacted.manifest, 8_000_000)
        with self.store.lock, self.store.db as db:
            rows = db.execute('SELECT generation,manifest FROM run_compactions WHERE run_id=? ORDER BY generation DESC LIMIT 1', (rid,)).fetchone()
            duplicate = bool(rows and json.loads(rows[1])['before_fingerprint'] == compacted.manifest['before_fingerprint'] and json.loads(rows[1])['after_fingerprint'] == compacted.manifest['after_fingerprint'])
            snapshot = db.execute('SELECT checkpoint FROM runs WHERE id=?', (rid,)).fetchone()
            if not snapshot:
                raise ValueError('Run not found.')
            snapshot = json.loads(snapshot[0])
            settled_text = snapshot.get('phase') == 'tools' and snapshot.get('messages') and snapshot['messages'][-1].get('role') == 'assistant' and not snapshot['messages'][-1].get('tool_calls')
            if snapshot.get('phase') != 'model' and not settled_text:
                raise ValueError('Compaction requires a committed model boundary.')
            from .compaction import fingerprint
            current = fingerprint(snapshot.get('messages', []))
            if current != compacted.manifest['before_fingerprint'] and not (duplicate and current == compacted.manifest['after_fingerprint']):
                raise ValueError('The model checkpoint changed during compaction. Nothing was retired.')
            used = db.execute('SELECT coalesce(sum(length(CAST(archive AS BLOB))),0) FROM run_compactions WHERE run_id=?', (rid,)).fetchone()[0]
            generation = rows[0] if duplicate else rows[0] + 1 if rows else 1
            if not duplicate and (generation > 256 or used + len(archive.encode()) > 128_000_000):
                raise ValueError('This run reached its context archive budget. Start a reviewed handoff.')
            snapshot['messages'] = compacted.projection.messages
            snapshot['phase'] = 'model'
            snapshot['compaction_generation'] = generation
            if not duplicate:
                db.execute('INSERT INTO run_compactions VALUES(?,?,?,?,?)', (rid, generation, archive, manifest, time.time()))
            db.execute('UPDATE runs SET checkpoint=?,updated=? WHERE id=?', (encoded(snapshot), time.time(), rid))
            if not duplicate:
                self.event(db, rid, 'context_compacted', {'generation': generation, 'manifest': compacted.manifest})
            return generation

    def seal(self, rid, archive, manifest, generation):
        """Phase 36 (W6): commit one seal's archive as the run's next compaction generation, so ReadRunEvidence pages it
        like any other archived result. The caller built its stubs for `generation`; a stale one is refused."""
        archive_text = encoded(archive, 32_000_000)
        manifest_text = encoded(manifest, 8_000_000)
        with self.store.lock, self.store.db as db:
            latest = db.execute('SELECT max(generation) FROM run_compactions WHERE run_id=?', (rid,)).fetchone()[0] or 0
            if generation != latest + 1:
                raise ValueError('The seal was built for another archive generation. Nothing was sealed.')
            used = db.execute('SELECT coalesce(sum(length(CAST(archive AS BLOB))),0) FROM run_compactions WHERE run_id=?',
                              (rid,)).fetchone()[0]
            if generation > 256 or used + len(archive_text.encode()) > 128_000_000:
                raise ValueError('This run reached its context archive budget. Nothing was sealed.')
            db.execute('INSERT INTO run_compactions VALUES(?,?,?,?,?)', (rid, generation, archive_text, manifest_text, time.time()))
            self.event(db, rid, 'context_sealed', {'generation': generation, 'manifest': manifest})
        return generation

    def next_generation(self, rid):
        rows = self.store.query('SELECT max(generation) AS g FROM run_compactions WHERE run_id=?', (rid,))
        return (rows[0]['g'] or 0) + 1

    def latest_compaction(self, rid):
        """(generation, manifest) of the run's newest committed compaction, or None."""
        rows = self.store.query('SELECT generation,manifest FROM run_compactions WHERE run_id=? '
                                'ORDER BY generation DESC LIMIT 1', (rid,))
        return (rows[0]['generation'], json.loads(rows[0]['manifest'])) if rows else None

    def compactions(self, rid):
        return self.store.query('SELECT generation,length(CAST(manifest AS BLOB)) AS manifest_bytes,created FROM run_compactions WHERE run_id=? ORDER BY generation', (rid,))

    def context_archive(self, rid, generation):
        if type(generation) is not int or not 1 <= generation <= 256:
            raise ValueError('Choose an archive generation from 1 to 256.')
        rows = self.store.query('SELECT archive,manifest,created FROM run_compactions WHERE run_id=? AND generation=?', (rid, generation))
        if not rows:
            raise ValueError('Context archive not found in this run.')
        return {'archive': json.loads(rows[0]['archive']), 'manifest': json.loads(rows[0]['manifest']), 'created': rows[0]['created']}

    def handoff(self, rid, manifest_only=False):
        rows = self.store.query('SELECT ' + ('manifest,created' if manifest_only else 'packet,manifest,created') + ' FROM run_handoffs WHERE run_id=?', (rid,))
        if not rows:
            return None
        row = rows[0]
        return {'manifest': json.loads(row['manifest']), 'created': row['created']} | ({} if manifest_only else {'data': json.loads(row['packet'])})

    def forget(self, cid):
        with self.store.lock, self.store.db as db:
            if self.interactions:
                self.interactions.forget(db, cid)
            if self.source_provenance:
                self.source_provenance.forget(db, cid)
            if getattr(self, 'stream', None):
                self.stream.forget(db, cid)
            for table in ("run_events", "run_tools", "run_action_owners", "run_handoffs", "run_compactions"):
                db.execute(f"DELETE FROM {table} WHERE run_id IN (SELECT id FROM runs WHERE chat_id=?)", (cid,))
            for table in ('extension_hook_audit', 'extension_hook_budget', 'extension_tool_routes','extension_data_routes','extension_data_snapshots'):
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    db.execute(f'DELETE FROM {table} WHERE run_id IN (SELECT id FROM runs WHERE chat_id=?)', (cid,))
            db.execute("DELETE FROM runs WHERE chat_id=?", (cid,))

    def close(self):
        if not self.lease.closed:
            if os.name == "nt":
                import msvcrt
                self.lease.seek(0)
                msvcrt.locking(self.lease.fileno(), msvcrt.LK_UNLCK, 1)
            self.lease.close()
