"""Host-owned, durable agent relationships. Children never become root runs.

This module grants no effects. Worker code supplies actor identity, generation,
lease owner, dispatch estimates, provider usage reports and receipt outcomes;
model arguments cannot. Child admissions keep the root's bounded completion
reserve (delegation_policy); agent_usage records provider-reported usage per
settled dispatch, apart from the conservative reservation charges.
Model-requested spawns also pass the Phase 15 admission gate (measured floors,
duplicate work); waits return compact settled-child entries, review refusals
name the evidence they would accept, and agent_rounds counts review refusals
and root steers per child generation for the ping-pong limit.
"""

import json
import math
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import PurePosixPath

from .delegation_policy import (APPROVAL_STEPS, PRICE_NOTE, RESERVATION_NOTE, USAGE_FIELDS, admission as fit_admission,
                                aggregate_usage, check_child_fit, child_capacity, child_denial, child_floor_refusal,
                                delegation_advice, floor_parts, refuse, resolve_child_limits, root_reserve,
                                same_question, task_kind, validate_usage, window as reservation_window)
from .interaction import SECRET, question_items
from .permissions import GROUP, automatic
from .policy import PRIVATE
from .runtime import encoded


TERMINAL = {'completed', 'failed', 'interrupted', 'stopped', 'budget_exhausted'}
ACTIVE = {'queued', 'running', 'waiting_parent', 'waiting_input'}
# Root states that keep existing child work live. A resume passes through
# 'queued'; root inference and spawn still require 'running'.
ROOT_LIVE = {'queued', 'running', 'waiting_input', 'waiting_approval', 'retry_wait'}
READ_TOOLS = {'Glob', 'Read', 'Grep', 'ReadAttachment', 'WebSearch', 'ReadURL',
              'Weather', 'ReadRunEvidence', 'ReadSource'}
AGENT_TOOLS = {'SpawnAgent', 'SendAgentMessage', 'WaitForAgents', 'DelegateAgentTask',
               'AskParentQuestion', 'AnswerAgentQuestion', 'ProposePatch', 'ReadAgentTree', 'StopAgent'}
# Durable effect requests and their read-only outcome copies. Neither performs
# an effect; only the root executes a request through its own journal and broker.
EFFECT_TOOLS = {'RequestAgentEffect', 'ReadAgentEffect'}
EFFECT_GROUPS = ('tests', 'terminal', 'apps')
# Writes into the child's own host-allocated managed worktree only. The host
# assigns them with SpawnAgent workspace="worktree"; a model cannot request them.
WORKTREE_TOOLS = {'WriteWorktreeFile', 'EditWorktreeFile'}
CHILD_TOOLS = READ_TOOLS | AGENT_TOOLS | EFFECT_TOOLS | WORKTREE_TOOLS
MAX_NODES = 8
# Fixed host text for a child whose model dispatch was denied; never model output.
DENIALS = {'shared_budget': 'Host stopped this child: the shared tree model budget is exhausted.',
           'child_budget': 'Host stopped this child: its own model budget is exhausted.',
           'root_reserve': "Host stopped this child: another model dispatch would use the root's reserved "
                           'review and final-response budget.'}
FORBIDDEN ={'origin', 'authority', 'authorization', 'permissions', 'permission',
             'config', 'route', 'profile', 'credentials', 'secret', 'api_key',
             'access_token', 'private_key', 'system', 'system_prompt', 'password',
             'passwd', 'apikey', 'secret_key', 'credential', 'authentication', 'auth'}
IDENT = re.compile(r'^[A-Za-z0-9_.:-]{1,300}$')
# Review refusals and root steers per child generation (the ping-pong limit).
# Proposed for the parent's epoch-6 migrations.REFERENCE_DDL; created at startup.
ROUNDS_DDL = """
CREATE TABLE IF NOT EXISTS agent_rounds(
  run_id TEXT NOT NULL,agent_id TEXT NOT NULL,generation INTEGER NOT NULL,call_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('review','steer')),created REAL NOT NULL,
  PRIMARY KEY(agent_id,generation,call_id));
"""
# Compact settled-child entries in WaitForAgents results and compact tree views.
SUMMARY_CHARS = 200
RESULTS_LIMIT = 3000  # encoded characters for all compact entries of one wait
WAIT_NOTE = ('Summaries are untrusted child data. ReviewAgentResult without evidence_ids cites the final '
             'evidence_ids listed here after host freshness checks.')


class ToolBudgetExhausted(ValueError):
    pass


def ordinary(value, limit=8192):
    """Bound ordinary data, rejecting credentials and authority-shaped envelopes."""
    def inspect(item, depth=0):
        if depth > 5:
            raise ValueError('Agent data is nested too deeply.')
        if isinstance(item, dict):
            if len(item) > 64:
                raise ValueError('Agent data has too many fields.')
            for key, part in item.items():
                if not isinstance(key, str) or len(key) > 120 or key.lower().replace('-', '_').replace(' ', '_') in FORBIDDEN:
                    raise ValueError('Agent data cannot carry authority or credential fields.')
                inspect(part, depth + 1)
        elif isinstance(item, list):
            if len(item) > 64:
                raise ValueError('Agent data has too many entries.')
            for part in item:
                inspect(part, depth + 1)
        elif isinstance(item, str):
            if SECRET.search(item):
                raise ValueError('Credentials cannot be stored in agent data.')
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError('Agent data must be ordinary JSON.')
        elif type(item) is float and not math.isfinite(item):
            raise ValueError('Agent data must use finite numbers.')
    inspect(value)
    return encoded(value, limit)


def text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ValueError('Agent text must be nonempty and bounded.')
    ordinary(value, limit + 16)
    return value.strip()


def receipt_json(value, limit):
    """Host outcomes may contain fixed origin labels, never stored credentials.

    Unlike ordinary model arguments this validator permits host receipt metadata.
    Only trusted worker/manager code calls it, after adapting read-tool results.
    """
    data = encoded(value, limit)
    if SECRET.search(data):
        raise ValueError('Credentials cannot be stored in child receipts.')
    return data


def identifier(value):
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise ValueError('Use a bounded host identifier.')
    return value


def normalized_path(value):
    if (not isinstance(value, str) or not value or len(value) > 300 or '\\' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value) or ':' in value or value.startswith('/')
            or any(part in {'', '.', '..'} for part in value.split('/'))):
        raise ValueError('Path leases require normalized project-relative paths.')
    path = str(PurePosixPath(value))
    if any(part.lower() in PRIVATE or part.lower().startswith('.env')
           or part.lower().endswith(('.pem', '.key', '.p12', '.pfx', '.gguf')) for part in path.split('/')):
        raise ValueError('Private and repository metadata paths cannot be leased.')
    return path


def conflict(a, b):
    return a == b or a.startswith(b + '/') or b.startswith(a + '/')


class Tree:
    def __init__(self, app):
        self.app, self.store = app, app.store
        with self.store.lock, self.store.db as db:
            db.executescript('''
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS agent_trees(
              run_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,config TEXT NOT NULL,
              tools TEXT NOT NULL,max_steps INTEGER NOT NULL,max_seconds REAL NOT NULL,
              max_tokens INTEGER NOT NULL,allocated_steps INTEGER NOT NULL DEFAULT 0,
              allocated_seconds REAL NOT NULL DEFAULT 0,allocated_tokens INTEGER NOT NULL DEFAULT 0,
              dispatches INTEGER NOT NULL DEFAULT 0,charged_tokens INTEGER NOT NULL DEFAULT 0,
              completed_steps INTEGER NOT NULL DEFAULT 0,child_seconds REAL NOT NULL DEFAULT 0,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_nodes(
              id TEXT PRIMARY KEY,run_id TEXT NOT NULL,parent_id TEXT,name TEXT NOT NULL,
              task TEXT NOT NULL,context TEXT NOT NULL,tools TEXT NOT NULL,limits TEXT NOT NULL,
              dependencies TEXT NOT NULL,write_paths TEXT NOT NULL,depth INTEGER NOT NULL,
              generation INTEGER NOT NULL,status TEXT NOT NULL,result TEXT,evidence_ids TEXT NOT NULL,
              review TEXT NOT NULL DEFAULT 'pending',dispatches INTEGER NOT NULL DEFAULT 0,
              charged_tokens INTEGER NOT NULL DEFAULT 0,elapsed REAL NOT NULL DEFAULT 0,
              created REAL NOT NULL,updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS agent_nodes_tree ON agent_nodes(run_id,created);
            CREATE TABLE IF NOT EXISTS agent_claims(
              agent_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,generation INTEGER NOT NULL,
              owner TEXT NOT NULL,expires REAL NOT NULL,heartbeat REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_path_leases(
              run_id TEXT NOT NULL,path TEXT NOT NULL,agent_id TEXT NOT NULL,
              generation INTEGER NOT NULL,baseline TEXT,status TEXT NOT NULL,
              PRIMARY KEY(run_id,path));
            CREATE TABLE IF NOT EXISTS agent_dispatches(
              run_id TEXT NOT NULL,id TEXT NOT NULL,actor TEXT NOT NULL,generation INTEGER NOT NULL,
              input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,status TEXT NOT NULL,
              started REAL NOT NULL,elapsed REAL NOT NULL DEFAULT 0,
              PRIMARY KEY(run_id,id));
            CREATE TABLE IF NOT EXISTS agent_receipts(
              run_id TEXT NOT NULL,agent_id TEXT NOT NULL,generation INTEGER NOT NULL,
              call_id TEXT NOT NULL,name TEXT NOT NULL,arguments TEXT NOT NULL,
              state TEXT NOT NULL,result TEXT,PRIMARY KEY(agent_id,generation,call_id));
            CREATE TABLE IF NOT EXISTS agent_messages(
              id TEXT PRIMARY KEY,run_id TEXT NOT NULL,sequence INTEGER NOT NULL,
              sender TEXT NOT NULL,target TEXT NOT NULL,generation INTEGER NOT NULL,
              body TEXT NOT NULL,kind TEXT NOT NULL,correlation_id TEXT,idempotency_key TEXT NOT NULL,
              acked INTEGER NOT NULL DEFAULT 0,revoked INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,
              UNIQUE(run_id,sequence),UNIQUE(run_id,sender,idempotency_key));
            CREATE TABLE IF NOT EXISTS agent_waits(
              id TEXT PRIMARY KEY,run_id TEXT NOT NULL,actor TEXT NOT NULL,call_id TEXT NOT NULL,
              agent_ids TEXT NOT NULL,cursor INTEGER NOT NULL,status TEXT NOT NULL,
              result TEXT,notified INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,
              UNIQUE(run_id,actor,call_id));
            CREATE TABLE IF NOT EXISTS agent_parent_questions(
              id TEXT PRIMARY KEY,run_id TEXT NOT NULL,agent_id TEXT NOT NULL,
              generation INTEGER NOT NULL,parent_id TEXT NOT NULL,call_id TEXT NOT NULL,
              items TEXT NOT NULL,status TEXT NOT NULL,answer TEXT,created REAL NOT NULL,
              answer_origin TEXT,UNIQUE(agent_id,generation,call_id));
            CREATE TABLE IF NOT EXISTS agent_mail_reads(
              message_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,cursor INTEGER NOT NULL,
              call_id TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS agent_usage(
              run_id TEXT NOT NULL,dispatch_id TEXT NOT NULL,actor TEXT NOT NULL,generation INTEGER NOT NULL,
              attempt INTEGER NOT NULL,outcome TEXT NOT NULL,state TEXT NOT NULL,input_tokens INTEGER,
              output_tokens INTEGER,cached_tokens INTEGER,recorded REAL NOT NULL,
              PRIMARY KEY(run_id,dispatch_id));
            ''' + ROUNDS_DDL + 'COMMIT;')
            if 'completed_steps' not in {r[1] for r in db.execute('PRAGMA table_info(agent_trees)')}:
                db.execute('ALTER TABLE agent_trees ADD COLUMN completed_steps INTEGER NOT NULL DEFAULT 0')
            # Host answer provenance: 'agent' (parent/root answer) or 'human' (native form).
            if 'answer_origin' not in {r[1] for r in db.execute('PRAGMA table_info(agent_parent_questions)')}:
                db.execute('ALTER TABLE agent_parent_questions ADD COLUMN answer_origin TEXT')
        # The parent manager revokes native forms of closed agents in the same transaction.
        self.on_close = None
        self.recover()

    @contextmanager
    def _transaction(self):
        with self.store.lock:
            db = self.store.db
            if db.in_transaction:
                raise ValueError('Agent tree mutations require their own transaction.')
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def _row(cursor):
        row = cursor.fetchone()
        return dict(zip([d[0] for d in cursor.description], row)) if row else None

    def _root(self, db, cid, rid, active=False):
        row = self._row(db.execute('SELECT * FROM runs WHERE id=? AND chat_id=?', (rid, cid)))
        if not row:
            raise ValueError('Root run not found in this conversation.')
        if active and row['state'] not in ROOT_LIVE:
            raise ValueError('Resume the owning root explicitly before operating its tree.')
        return row

    def _ensure(self, db, root):
        rid, config = root['id'], json.loads(root['config'])
        tree = self._row(db.execute('SELECT * FROM agent_trees WHERE run_id=?', (rid,)))
        if tree:
            # Resume may enlarge budgets, but never replace the permission/route owner.
            snapshot = json.loads(tree['config'])
            if any(snapshot.get(k) != config.get(k) for k in ('workspace', 'workspace_identity', 'permissions', 'route', 'extensions', 'mods')):
                raise ValueError('Agent tree owner configuration changed.')
            # Only the host run-resume path can edit these root limits. Child
            # allocations/dispatches remain charged after an explicit increase.
            revised = config['limits']
            budget = tree['max_tokens'] // (tree['max_steps'] * 3)
            if (tree['max_steps'], tree['max_seconds']) != (revised['max_steps'], revised['max_seconds']):
                db.execute('UPDATE agent_trees SET max_steps=?,max_seconds=?,max_tokens=? WHERE run_id=?',
                           (revised['max_steps'], revised['max_seconds'], revised['max_steps'] * 3 * budget, rid))
                tree = self._row(db.execute('SELECT * FROM agent_trees WHERE run_id=?', (rid,)))
            return tree
        limits = config['limits']
        steps, seconds = limits['max_steps'], limits['max_seconds']
        profile = config.get('route') or {}
        budget = profile.get('context') or profile.get('budget') or 4096
        budget = min(10_000_000, max(1024, int(budget)))
        caps = {tool for tool in CHILD_TOOLS if tool not in GROUP or config['permissions']['tools'].get(GROUP[tool], False)}
        if not config['permissions']['tools'].get('edits', False):
            caps.discard('ProposePatch')
        if not (config['permissions']['tools'].get('edits', False) and config['permissions']['tools'].get('files', False)):
            caps -= WORKTREE_TOOLS
        # A request is useful only when the root itself may run tests, terminal
        # commands or apps. The capability never includes those direct tools.
        if not any(config['permissions']['tools'].get(group, False) for group in EFFECT_GROUPS):
            caps -= EFFECT_TOOLS
        db.execute('INSERT INTO agent_trees(run_id,chat_id,config,tools,max_steps,max_seconds,max_tokens,created) VALUES(?,?,?,?,?,?,?,?)',
                   (rid, root['chat_id'], root['config'], encoded(sorted(caps)), steps,
                    seconds, steps * 3 * budget, time.time()))
        # An older run may first enter this feature after prior root responses.
        # Their retry counts/tokens were not measured by this ledger. Charge the
        # documented worst-case three admissions/window per committed response;
        # these entries are estimates and never provider billing or replay IDs.
        previous = root['steps'] * 3
        db.execute('UPDATE agent_trees SET dispatches=?,charged_tokens=?,completed_steps=? WHERE run_id=?', (previous, previous * budget, root['steps'], rid))
        for index in range(previous):
            db.execute("INSERT INTO agent_dispatches VALUES(?,?,?,0,?,0,'estimated',?,0)", (rid, 'pre-tree-' + str(index + 1), rid, budget, time.time()))
        return self._row(db.execute('SELECT * FROM agent_trees WHERE run_id=?', (rid,)))

    def _node(self, db, rid, ident):
        row = self._row(db.execute('SELECT * FROM agent_nodes WHERE id=? AND run_id=?', (ident, rid)))
        if not row:
            raise ValueError('Agent not found in this root tree.')
        return row

    def _ledger(self, db, root, tree):
        """Host budget numbers for delegation_policy; no task, result or profile content.

        In-flight dispatches count as used steps and active child seconds, so
        concurrent admissions see each other inside the immediate transaction.
        """
        rid, now = root['id'], time.time()
        active = db.execute("SELECT coalesce(sum(max(0,?-started)),0) FROM agent_dispatches WHERE run_id=? AND status='running' AND actor!=?", (now, rid, rid)).fetchone()[0]
        pending = db.execute("SELECT count(*) FROM agent_dispatches WHERE run_id=? AND status='running'", (rid,)).fetchone()[0]
        receipts = (db.execute('SELECT count(*) FROM agent_receipts WHERE run_id=?', (rid,)).fetchone()[0]
                    + db.execute("SELECT count(*) FROM run_tools WHERE run_id=? AND state!='planned'", (rid,)).fetchone()[0])
        nodes = db.execute('SELECT count(*) FROM agent_nodes WHERE run_id=?', (rid,)).fetchone()[0]
        return {'max_steps': tree['max_steps'], 'max_seconds': tree['max_seconds'], 'max_tokens': tree['max_tokens'],
                'window': reservation_window(tree['max_tokens'], tree['max_steps']),
                'used_steps': tree['completed_steps'] + pending, 'dispatches': tree['dispatches'],
                'charged_tokens': tree['charged_tokens'], 'used_seconds': root['elapsed'] + tree['child_seconds'] + active,
                'tool_receipts': receipts, 'allocated_steps': tree['allocated_steps'],
                'allocated_seconds': tree['allocated_seconds'], 'allocated_tokens': tree['allocated_tokens'],
                'nodes_left': max(0, MAX_NODES - nodes)}

    @staticmethod
    def _reserve(tree):
        return root_reserve({'max_steps': tree['max_steps'], 'max_seconds': tree['max_seconds'],
                             'window': reservation_window(tree['max_tokens'], tree['max_steps'])})

    @staticmethod
    def _own(db, rid, node):
        """A child's view for grandchild advice: its limits bound any grandchild."""
        limits = json.loads(node['limits'])
        used = db.execute("SELECT count(*) FROM agent_dispatches WHERE run_id=? AND actor=? AND status IN ('completed','running')", (rid, node['id'])).fetchone()[0]
        return {'limits': limits, 'steps_left': max(0, limits['max_steps'] - used),
                'can_spawn': node['depth'] < 2 and 'SpawnAgent' in json.loads(node['tools'])}

    @staticmethod
    def _attempt(db, rid, dispatch_id, actor, generation):
        """One-based admission order of a dispatch within its actor generation."""
        return db.execute("SELECT count(*) FROM agent_dispatches WHERE run_id=? AND actor=? AND generation=? AND status!='estimated' "
                          'AND rowid<=(SELECT rowid FROM agent_dispatches WHERE run_id=? AND id=?)',
                          (rid, actor, generation, rid, dispatch_id)).fetchone()[0]

    @staticmethod
    def _usage_record(db, rid, dispatch_id, status):
        row = db.execute('SELECT state,input_tokens,output_tokens,cached_tokens FROM agent_usage WHERE run_id=? AND dispatch_id=?',
                         (rid, dispatch_id)).fetchone()
        if row:
            return row[0], dict(zip(USAGE_FIELDS, row[1:]))
        return ('pending' if status == 'running' else 'unrecorded'), dict.fromkeys(USAGE_FIELDS)

    def _usage(self, db, rid, actor=None):
        """Provider-reported usage per dispatch, aggregated without raw content.

        Every admitted dispatch is one attempt. A dispatch without a settled
        usage row is 'pending' while in flight and 'unrecorded' once it ended
        (interruption, restart, or admission before this ledger), so its usage
        stays unknown rather than zero. Pre-tree root steps were never measured
        here; their per-message metrics live with the conversation.
        """
        query = ('SELECT d.id,d.actor,d.generation,d.status,u.attempt,u.state,u.input_tokens,u.output_tokens,u.cached_tokens '
                 'FROM agent_dispatches d LEFT JOIN agent_usage u ON u.run_id=d.run_id AND u.dispatch_id=d.id '
                 "WHERE d.run_id=? AND d.status!='estimated'")
        params = [rid]
        if actor is not None:
            query, params = query + ' AND d.actor=?', params + [actor]
        order, items = {}, []
        for ident, who, generation, status, attempt, state, *counts in db.execute(query + ' ORDER BY d.rowid', params).fetchall():
            order[who, generation] = order.get((who, generation), 0) + 1
            if state is None:
                state = 'pending' if status == 'running' else 'unrecorded'
            items.append({'dispatch_id': ident, 'actor': who, 'generation': generation,
                          'attempt': attempt or order[who, generation], 'outcome': status, 'state': state,
                          **dict(zip(USAGE_FIELDS, counts))})
        groups = {}
        for item in items:
            groups.setdefault((item['actor'], item['generation']), []).append(item)
        depth = dict(db.execute('SELECT id,depth FROM agent_nodes WHERE run_id=?', (rid,)).fetchall())
        tracked = bool(db.execute('SELECT 1 FROM agent_trees WHERE run_id=?', (rid,)).fetchone())
        pre_tree = 0
        if actor in (None, rid):
            pre_tree = db.execute("SELECT count(*) FROM agent_dispatches WHERE run_id=? AND status='estimated'", (rid,)).fetchone()[0] // 3
        summary = aggregate_usage(items)
        summary.update(
            tracked=tracked, pre_tree_root_steps=pre_tree, complete=summary['complete'] and tracked and not pre_tree,
            by_actor=[{'actor': who, 'role': 'root' if who == rid else 'child', 'depth': 0 if who == rid else depth.get(who),
                       'generation': generation, **aggregate_usage(group)} for (who, generation), group in groups.items()],
            source='provider_reported_per_dispatch', cost_usd=None, price_note=PRICE_NOTE, reservation_note=RESERVATION_NOTE)
        if not tracked:
            summary['totals'] = dict.fromkeys(USAGE_FIELDS)
        if actor is not None:
            summary['attempts_detail'] = [{k: v for k, v in item.items() if k != 'actor'} for item in items]
        return summary

    @staticmethod
    def _public(row):
        result = dict(row)
        for key in ('context', 'tools', 'limits', 'dependencies', 'write_paths', 'evidence_ids', 'result'):
            if key in result:
                result[key] = json.loads(result[key]) if result[key] is not None else None
        result.update(agent_id=result['id'], origin='agent', authorization='none')
        return result

    def _actor(self, db, root, actor, *, live=False):
        if actor is None or actor == root['id']:
            return root['id'], None
        node = self._node(db, root['id'], actor)
        if live and node['status'] not in {'running', 'waiting_parent', 'waiting_input'}:
            raise ValueError('This agent cannot perform new work.')
        return actor, node

    def _worker_actor(self, db, root, actor, generation, owner, capability):
        actor, node = self._actor(db, root, actor, live=True)
        if node:
            self._lease(db, node, generation, owner)
            if capability not in json.loads(node['tools']):
                raise ValueError('This child lacks the requested coordination capability.')
        return actor, node

    def spawn(self, cid, rid, parent_id, task, name, tools, limits,
              write_paths=(), dependencies=(), context=None, parent_generation=None, parent_owner=None,
              admission=False):
        """Create one queued child. ``admission`` applies the Phase 15 gate for
        model-requested spawns (Agents.tool): duplicate work, then the measured
        floor (delegation_policy.admission), inside this same transaction.
        Refusals raise DelegationRefused and write nothing."""
        task, name = text(task, 8000), text(name, 80)
        context = {} if context is None else context
        if not isinstance(context, dict):
            raise ValueError('Agent context must be an ordinary object.')
        context = ordinary(context)
        if not isinstance(tools, list) or len(tools) > 32 or any(not isinstance(t, str) for t in tools) or len(set(tools)) != len(tools):
            raise ValueError('Provide a bounded, unique list of child tools.')
        if not isinstance(limits, dict) or set(limits) - {'max_steps', 'max_seconds', 'model_tokens'}:
            raise ValueError('Child limits accept max_steps, max_seconds and model_tokens.')
        if not isinstance(write_paths, (list, tuple)) or len(write_paths) > 8:
            raise ValueError('Lease up to eight project paths.')
        paths = [normalized_path(p) for p in write_paths]
        if len(set(paths)) != len(paths):
            raise ValueError('Path leases must be unique.')
        if not isinstance(dependencies, (list, tuple)) or len(dependencies) > 8 or len(set(dependencies)) != len(dependencies):
            raise ValueError('Dependencies must be unique agents in this tree.')
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            from .process_jobs import live_writer
            config = json.loads(root['config'])
            if paths and live_writer(db, config['workspace'], config.get('workspace_identity')):
                raise ValueError('Wait for the writable background command before leasing child write paths.')
            if parent_id in (None, rid) and root['state'] != 'running':
                raise ValueError('Only a running root can spawn work.')
            tree = self._ensure(db, root)
            actor, parent = self._worker_actor(db, root, parent_id, parent_generation, parent_owner, 'SpawnAgent')
            depth = parent['depth'] + 1 if parent else 1
            if depth > 2 or db.execute('SELECT count(*) FROM agent_nodes WHERE run_id=?', (rid,)).fetchone()[0] >= MAX_NODES:
                raise ValueError('Agent trees allow eight child nodes and depth two.')
            caps = set(json.loads(parent['tools'] if parent else tree['tools'])) & CHILD_TOOLS
            if set(tools) - caps:
                raise ValueError('Child tools must be a subset of the parent capabilities.')
            worktree = bool(set(tools) & WORKTREE_TOOLS)
            kind = task_kind(paths, worktree)
            # Name the child that already holds this work before any budget arithmetic.
            hint = self._duplicate(db, rid, paths, task, name) if admission else None
            ledger = self._ledger(db, root, tree)
            parent_limits = json.loads(parent['limits']) if parent else None
            # Omitted limits are host-fitted beside the root reserve. Explicit
            # limits are kept exactly as requested or refused; never changed.
            budget, sources = resolve_child_limits(ledger, limits, parent_limits)
            steps, seconds, tokens = budget['max_steps'], budget['max_seconds'], budget['model_tokens']
            if (steps + tree['allocated_steps'] > tree['max_steps']
                    or seconds + tree['allocated_seconds'] > tree['max_seconds']
                    or tokens + tree['allocated_tokens'] > tree['max_tokens']):
                raise ValueError('Child allocations cannot multiply the root budget.')
            if parent_limits and (steps > parent_limits['max_steps'] or seconds > parent_limits['max_seconds']
                                  or tokens > parent_limits['model_tokens']):
                raise ValueError('A grandchild cannot exceed its parent budget.')
            check_child_fit(ledger, budget, sources)
            gate = None
            if admission:
                # Host numbers only: the ledger, the root permission mode and the host
                # task kind. A model estimate or delegation history never enters here.
                approvals = self._approvals(config, kind, worktree)
                own = self._own(db, rid, parent) if parent else None
                refusal = fit_admission(ledger, kind, approvals, own) or child_floor_refusal(kind, steps)
                if refusal:
                    raise refuse(refusal)
                gate = {'task_kind': kind, 'shared_steps_needed': sum(floor_parts(ledger, kind, approvals, own).values()),
                        'shared_steps_left': ledger['max_steps'] - ledger['used_steps']}
            for dependency in dependencies:
                dep = self._node(db, rid, dependency)
                if dep['status'] in TERMINAL - {'completed'}:
                    raise ValueError('A dependency has already failed or stopped.')
                if dependency == actor:
                    raise ValueError('An agent cannot depend on its active parent.')
            for (other,) in db.execute("SELECT path FROM agent_path_leases WHERE run_id=? AND status='held'", (rid,)):
                if any(conflict(path, other) for path in paths):
                    raise ValueError('A project path is already leased by this tree.')
            ident, now = uuid.uuid4().hex, time.time()
            budget = {'max_steps': steps, 'max_seconds': float(seconds), 'model_tokens': tokens}
            db.execute('INSERT INTO agent_nodes(id,run_id,parent_id,name,task,context,tools,limits,dependencies,write_paths,depth,generation,status,evidence_ids,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,\'queued\',\'[]\',?,?)',
                       (ident, rid, parent['id'] if parent else None, name, task, context,
                        encoded(tools), encoded(budget), encoded(list(dependencies)), encoded(paths), depth, now, now))
            db.execute('UPDATE agent_trees SET allocated_steps=allocated_steps+?,allocated_seconds=allocated_seconds+?,allocated_tokens=allocated_tokens+? WHERE run_id=?', (steps, seconds, tokens, rid))
            for path in paths:
                db.execute("INSERT INTO agent_path_leases VALUES(?,?,?,1,NULL,'held') ON CONFLICT(run_id,path) DO UPDATE SET agent_id=excluded.agent_id,generation=excluded.generation,baseline=NULL,status='held'", (rid, path, ident))
            # Content-free advice for the spawning actor's next choice; not stored.
            after = self._ledger(db, root, self._row(db.execute('SELECT * FROM agent_trees WHERE run_id=?', (rid,))))
            own = self._own(db, rid, self._node(db, rid, parent['id'])) if parent else None
            result = {**self._public(self._node(db, rid, ident)), 'limit_sources': sources,
                      'delegation': delegation_advice(after, own)}
            if gate:
                result['admission'] = gate
            if hint:
                result['duplicate_hint'] = hint
            return result

    @staticmethod
    def _approvals(config, kind, worktree):
        """Native approvals the integration needs under the root's own permission mode."""
        if kind != 'write':
            return 0
        policy = config['permissions']
        steps = 0 if automatic(policy, 'edit') else APPROVAL_STEPS
        if worktree and not automatic(policy, 'worktree'):
            steps += APPROVAL_STEPS  # the managed checkout allocation is reviewed too
        return steps

    def _duplicate(self, db, rid, paths, task, name):
        """Refuse a spawn whose leased paths, or whose question under the same name, another
        child already holds, naming it. The same question under another name is admitted
        with a hint naming the other child; returns that hint or None."""
        for held, holder in db.execute("SELECT path,agent_id FROM agent_path_leases WHERE run_id=? AND status='held' ORDER BY path",
                                       (rid,)).fetchall():
            matches = [path for path in paths if conflict(path, held)]
            if matches:
                overlap = sorted(set(matches) | {held})
                node = self._node(db, rid, holder)
                raise refuse({'kind': 'duplicate_work', 'limit': 'file_set', 'agent_id': holder,
                              'generation': node['generation'], 'status': node['status'], 'review': node['review'],
                              'paths': overlap[:8]})
        hint = None
        for ident, other, label, status, review, generation in db.execute(
                'SELECT id,task,name,status,review,generation FROM agent_nodes WHERE run_id=? ORDER BY created,id', (rid,)).fetchall():
            # A live child, or a completed one awaiting review, still owns its question.
            if not (status in ACTIVE or status == 'completed' and review == 'pending') or not same_question(task, other):
                continue
            if label.casefold().split() == name.casefold().split():
                raise refuse({'kind': 'duplicate_work', 'limit': 'question', 'agent_id': ident, 'generation': generation,
                              'status': status, 'review': review})
            hint = hint or {'agent_id': ident, 'status': status, 'review': review,
                            'note': 'Another child holds a matching task; prefer WaitForAgents on it over duplicate work.'}
        return hint

    def get(self, cid, rid, ident):
        with self.store.lock:
            self._root(self.store.db, cid, rid)
            return self._public(self._node(self.store.db, rid, ident))

    def list(self, cid, rid):
        with self.store.lock:
            self._root(self.store.db, cid, rid)
            rows = self.store.db.execute('SELECT id FROM agent_nodes WHERE run_id=? ORDER BY created,id', (rid,)).fetchall()
            return [self._public(self._node(self.store.db, rid, ident)) for (ident,) in rows]

    def budget(self, cid, rid):
        with self._transaction() as db:
            root = self._root(db, cid, rid)
            tree = self._ensure(db, root)
            estimate = db.execute("SELECT count(*),coalesce(sum(input_tokens+output_tokens),0) FROM agent_dispatches WHERE run_id=? AND status='estimated'", (rid,)).fetchone()
            ledger = self._ledger(db, root, tree)
            return {'root_limits': {'max_steps': tree['max_steps'], 'max_seconds': tree['max_seconds'],
                                    'model_tokens': tree['max_tokens'], 'max_dispatches': tree['max_steps'] * 3, 'max_tool_receipts': tree['max_steps'] * 8},
                    'allocated': {'steps': tree['allocated_steps'], 'seconds': tree['allocated_seconds'],
                                  'model_tokens': tree['allocated_tokens']},
                    'used': {'dispatches': tree['dispatches'], 'model_tokens': tree['charged_tokens'],
                             'steps': tree['completed_steps'], 'child_seconds': tree['child_seconds']},
                    'pre_tree_estimate': {'dispatches': estimate[0], 'model_tokens': estimate[1]},
                    # Children cannot consume root_reserve; child_capacity is what they still share.
                    'root_reserve': root_reserve(ledger), 'child_capacity': child_capacity(ledger),
                    'delegation': delegation_advice(ledger), 'usage': self._usage(db, rid),
                    'accounting': 'conservative_dispatch_reservation', 'origin': 'agent', 'authorization': 'none'}

    def admission_preview(self, cid, rid, kind='read_only'):
        """The Phase 15 gate's answer for one more root child of ``kind``: None when it fits, else its
        typed refusal. Host numbers only; nothing is spawned or reserved (it ensures the tree like advice())."""
        with self._transaction() as db:
            root = self._root(db, cid, rid)
            ledger = self._ledger(db, root, self._ensure(db, root))
            return fit_admission(ledger, kind, self._approvals(json.loads(root['config']), kind, False))

    def advice(self, cid, rid, actor=None):
        """Content-free delegation advice for the root, or for a child's grandchildren."""
        with self._transaction() as db:
            root = self._root(db, cid, rid)
            ledger = self._ledger(db, root, self._ensure(db, root))
            own = None if actor in (None, rid) else self._own(db, rid, self._node(db, rid, actor))
            return delegation_advice(ledger, own)

    def usage(self, cid, rid, actor=None):
        """Provider-reported usage for the whole tree, or one actor with its
        generations and attempts. Host IDs, counts and states only."""
        with self.store.lock:
            db = self.store.db
            self._root(db, cid, rid)
            if actor not in (None, rid):
                self._node(db, rid, actor)
            return self._usage(db, rid, actor)

    def _generation(self, node, generation):
        if type(generation) is not int or node['generation'] != generation:
            raise ValueError('This agent generation is stale.')

    def _lease(self, db, node, generation, owner):
        self._generation(node, generation)
        lease = self._row(db.execute('SELECT * FROM agent_claims WHERE agent_id=?', (node['id'],)))
        if not lease or lease['owner'] != owner or lease['generation'] != generation or lease['expires'] <= time.time():
            raise ValueError('The worker no longer owns this agent lease.')
        return lease

    def claim(self, cid, rid, ident, generation, owner, lease_seconds=30):
        identifier(owner)
        if type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 300:
            raise ValueError('Choose a one to 300 second lease.')
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            self._ensure(db, root)
            node = self._node(db, rid, ident)
            self._generation(node, generation)
            old = db.execute('SELECT owner,expires FROM agent_claims WHERE agent_id=?', (ident,)).fetchone()
            if old and old[1] > time.time():
                raise ValueError('Another worker already owns this agent.')
            if node['status'] != 'queued':
                raise ValueError('Only queued work can be claimed; interrupted work requires explicit retry.')
            for dep in json.loads(node['dependencies']):
                if self._node(db, rid, dep)['status'] != 'completed':
                    raise ValueError('Agent dependencies have not completed.')
            now = time.time()
            db.execute('INSERT OR REPLACE INTO agent_claims VALUES(?,?,?,?,?,?)', (ident, rid, generation, owner, now + lease_seconds, now))
            db.execute("UPDATE agent_nodes SET status='running',updated=? WHERE id=? AND generation=? AND status='queued'", (now, ident, generation))
            return {'agent_id': ident, 'generation': generation, 'owner': owner, 'expires': now + lease_seconds, 'status': 'running', 'origin': 'agent', 'authorization': 'none'}

    def heartbeat(self, cid, rid, ident, generation, owner, lease_seconds=30):
        if type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds) or not 1 <= lease_seconds <= 300:
            raise ValueError('Invalid lease duration.')
        with self._transaction() as db:
            self._root(db, cid, rid, True)
            node = self._node(db, rid, ident)
            self._lease(db, node, generation, owner)
            if node['status'] not in ACTIVE:
                raise ValueError('Settled work cannot renew its lease.')
            now = time.time()
            db.execute('UPDATE agent_claims SET expires=?,heartbeat=? WHERE agent_id=?', (now + lease_seconds, now, ident))
            return {'agent_id': ident, 'generation': generation, 'expires': now + lease_seconds, 'origin': 'agent', 'authorization': 'none'}

    def release(self, cid, rid, ident, generation, owner, cascade=True):
        """Interrupt this worker's generation. Host-wide pauses pass cascade=False,
        so descendants stay individually interrupted and retryable."""
        with self._transaction() as db:
            self._root(db, cid, rid)
            node = self._node(db, rid, ident)
            self._lease(db, node, generation, owner)
            return self._interrupt_in(db, cid, rid, ident, generation, owner, cascade)

    def abandon(self, cid, rid, ident, generation, owner, cascade=True):
        """Exit fallback when release failed: interrupt only this owner's
        generation whose lease expired, or which no claim holds any longer.
        A live lease, another owner's claim or a newer generation is never
        taken over; those return None."""
        with self._transaction() as db:
            self._root(db, cid, rid)
            node = self._node(db, rid, ident)
            if node['generation'] != generation or node['status'] not in {'running', 'waiting_parent', 'waiting_input'}:
                return None
            claim = db.execute('SELECT owner,generation,expires FROM agent_claims WHERE agent_id=?', (ident,)).fetchone()
            if claim and (claim[0] != owner or claim[1] != generation or claim[2] > time.time()):
                return None
            return self._interrupt_in(db, cid, rid, ident, generation, owner, cascade)

    def _interrupt_in(self, db, cid, rid, ident, generation, owner, cascade):
        now = time.time()
        db.execute('DELETE FROM agent_claims WHERE agent_id=? AND owner=? AND generation=?', (ident, owner, generation))
        db.execute("UPDATE agent_nodes SET status='interrupted',updated=? WHERE id=? AND generation=? AND status IN ('running','waiting_parent','waiting_input')", (now, ident, generation))
        db.execute("UPDATE agent_receipts SET state='uncertain' WHERE agent_id=? AND generation=? AND state='running'", (ident, generation))
        # An unfinished dispatch keeps its charges and frees its step slot, as in recovery.
        for dispatch, started in db.execute("SELECT id,started FROM agent_dispatches WHERE run_id=? AND actor=? AND generation=? AND status='running'", (rid, ident, generation)).fetchall():
            elapsed = max(0., now - started)
            db.execute("UPDATE agent_dispatches SET status='interrupted',elapsed=? WHERE run_id=? AND id=?", (elapsed, rid, dispatch))
            db.execute('UPDATE agent_trees SET child_seconds=child_seconds+? WHERE run_id=?', (elapsed, rid))
            db.execute('UPDATE agent_nodes SET elapsed=elapsed+? WHERE id=?', (elapsed, ident))
        self._ended(db, cid, rid, ident, cascade)
        self._wake(db, rid)
        return self._public(self._node(db, rid, ident))

    def reserve_model(self, cid, rid, actor, generation, input_tokens, output_tokens, dispatch_id):
        identifier(dispatch_id)
        if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens)) or input_tokens + output_tokens < 1:
            raise ValueError('Dispatch token reservations require conservative nonnegative estimates.')
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            tree = self._ensure(db, root)
            actor, node = self._actor(db, root, actor, live=True)
            if node:
                self._generation(node, generation)
                if node['status'] != 'running':
                    raise ValueError('Only running children can dispatch models.')
                claim = db.execute('SELECT generation,expires FROM agent_claims WHERE agent_id=?', (actor,)).fetchone()
                if not claim or claim[0] != generation or claim[1] <= time.time():
                    raise ValueError('Model dispatch requires a live worker claim.')
            elif generation not in (None, 0):
                raise ValueError('The root model actor uses generation zero.')
            elif root['state'] != 'running':
                raise ValueError('Only a running root may dispatch its own model.')
            old = self._row(db.execute('SELECT * FROM agent_dispatches WHERE run_id=? AND id=?', (rid, dispatch_id)))
            if old:
                if (old['actor'], old['generation'], old['input_tokens'], old['output_tokens']) != (actor, generation or 0, input_tokens, output_tokens):
                    raise ValueError('Dispatch identity conflicts with its reservation.')
                raise ValueError('This dispatch was already admitted; never replay it.')
            ledger = self._ledger(db, root, tree)
            charged = input_tokens + output_tokens
            reason = None
            if (tree['dispatches'] >= tree['max_steps'] * 3 or root['steps'] >= tree['max_steps']
                    or ledger['used_steps'] >= tree['max_steps']
                    or tree['charged_tokens'] + charged > tree['max_tokens']
                    or ledger['used_seconds'] >= tree['max_seconds']):
                reason = 'shared_budget'
            elif node:
                limits = json.loads(node['limits'])
                node_steps = db.execute("SELECT count(*) FROM agent_dispatches WHERE run_id=? AND actor=? AND status IN ('completed','running')", (rid, actor)).fetchone()[0]
                if (node['dispatches'] >= limits['max_steps'] * 3
                        or node_steps >= limits['max_steps']
                        or node['charged_tokens'] + charged > limits['model_tokens']
                        or node['elapsed'] >= limits['max_seconds']):
                    reason = 'child_budget'
                else:
                    # Children, grandchildren and retried generations never spend
                    # the root's bounded review/verification/final-response share.
                    reason = child_denial(ledger, charged)
            if reason:
                # Raising would rollback this marker, so exhaustion is an explicit denial receipt.
                if node:
                    db.execute("UPDATE agent_nodes SET status='budget_exhausted',updated=? WHERE id=?", (time.time(), actor))
                    # A host note only where the child never reported; a child result is never replaced.
                    db.execute('UPDATE agent_nodes SET result=? WHERE id=? AND result IS NULL',
                               (receipt_json({'text': DENIALS[reason], 'reason': reason, 'reported_by': 'host'}, 2000), actor))
                    db.execute('DELETE FROM agent_claims WHERE agent_id=?', (actor,))
                    self._ended(db, cid, rid, actor, True)
                    self._wake(db, rid)
                return {'admitted': False, 'status': 'budget_exhausted', 'reason': reason, 'origin': 'agent', 'authorization': 'none'}
            attempt = 1 + db.execute("SELECT count(*) FROM agent_dispatches WHERE run_id=? AND actor=? AND generation=? AND status!='estimated'",
                                     (rid, actor, generation or 0)).fetchone()[0]
            db.execute("INSERT INTO agent_dispatches VALUES(?,?,?,?,?,?,'running',?,0)", (rid, dispatch_id, actor, generation or 0, input_tokens, output_tokens, time.time()))
            db.execute('UPDATE agent_trees SET dispatches=dispatches+1,charged_tokens=charged_tokens+? WHERE run_id=?', (charged, rid))
            if node:
                db.execute('UPDATE agent_nodes SET dispatches=dispatches+1,charged_tokens=charged_tokens+? WHERE id=?', (charged, actor))
            return {'admitted': True, 'dispatch_id': dispatch_id, 'attempt': attempt, 'charged_tokens': charged,
                    'accounting': 'conservative_dispatch_reservation', 'origin': 'agent', 'authorization': 'none'}

    def finish_model(self, cid, rid, actor, generation, dispatch_id, elapsed_seconds=None, failed=False, usage=None):
        """Settle one admitted dispatch exactly once.

        ``usage`` is the provider's report for this attempt only, or None when
        it reported nothing. Its validated counts are recorded once in
        agent_usage, unknown counts as null. Reservation charges are never
        refunded or replaced by usage. A repeated, late or post-interruption
        settlement changes nothing and returns the existing record.
        """
        if elapsed_seconds is not None and (type(elapsed_seconds) not in (int, float) or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0):
            raise ValueError('Model elapsed time must be nonnegative.')
        counts, state = validate_usage(usage)
        with self._transaction() as db:
            root = self._root(db, cid, rid)
            actor, node = self._actor(db, root, actor)
            row = self._row(db.execute('SELECT * FROM agent_dispatches WHERE run_id=? AND id=? AND actor=? AND generation=?', (rid, dispatch_id, actor, generation or 0)))
            if not row:
                raise ValueError('Model dispatch not found for this generation.')
            if row['status'] != 'running':
                recorded_state, recorded = self._usage_record(db, rid, dispatch_id, row['status'])
                return {'dispatch_id': dispatch_id, 'status': row['status'], 'elapsed': row['elapsed'], 'duplicate': True,
                        'usage': recorded, 'usage_state': recorded_state, 'origin': 'agent', 'authorization': 'none'}
            elapsed = max(0, time.time() - row['started'])
            if elapsed_seconds is not None:
                elapsed = max(elapsed, elapsed_seconds)
            outcome = 'failed' if failed else 'completed'
            db.execute('UPDATE agent_dispatches SET status=?,elapsed=? WHERE run_id=? AND id=?', (outcome, elapsed, rid, dispatch_id))
            if not failed:
                db.execute('UPDATE agent_trees SET completed_steps=completed_steps+1 WHERE run_id=?', (rid,))
            if node:
                db.execute('UPDATE agent_trees SET child_seconds=child_seconds+? WHERE run_id=?', (elapsed, rid))
                db.execute('UPDATE agent_nodes SET elapsed=elapsed+? WHERE id=?', (elapsed, actor))
            attempt = self._attempt(db, rid, dispatch_id, actor, generation or 0)
            # The running->settled transition above happens once, so this row is written once.
            db.execute('INSERT OR IGNORE INTO agent_usage(run_id,dispatch_id,actor,generation,attempt,outcome,state,'
                       'input_tokens,output_tokens,cached_tokens,recorded) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (rid, dispatch_id, actor, generation or 0, attempt, outcome, state, counts['input_tokens'],
                        counts['output_tokens'], counts['cached_tokens'], time.time()))
            return {'dispatch_id': dispatch_id, 'status': outcome, 'elapsed': elapsed, 'attempt': attempt,
                    'charged_tokens': row['input_tokens'] + row['output_tokens'], 'usage': counts, 'usage_state': state,
                    'accounting': 'conservative_dispatch_reservation', 'origin': 'agent', 'authorization': 'none'}

    def begin_tool(self, cid, rid, ident, generation, owner, call_id, name, arguments):
        identifier(call_id)
        args = ordinary(arguments, 100000 if name == 'ProposePatch' else 200000 if name in WORKTREE_TOOLS else 16000)
        with self._transaction() as db:
            self._root(db, cid, rid, True)
            node = self._node(db, rid, ident)
            self._lease(db, node, generation, owner)
            if node['status'] != 'running' or name not in json.loads(node['tools']):
                raise ValueError('Child tool is outside this agent capability or state.')
            old = self._row(db.execute('SELECT * FROM agent_receipts WHERE agent_id=? AND generation=? AND call_id=?', (ident, generation, call_id)))
            if old:
                if old['name'] != name or old['arguments'] != args:
                    raise ValueError('Child receipt identity conflicts.')
                if old['state'] == 'completed':
                    return json.loads(old['result'])
                raise ValueError('A running or uncertain child tool cannot be replayed.')
            limits = json.loads(node['limits'])
            tree = self._ensure(db, self._root(db, cid, rid))
            own_calls = db.execute('SELECT count(*) FROM agent_receipts WHERE agent_id=?', (ident,)).fetchone()[0]
            all_calls = db.execute('SELECT count(*) FROM agent_receipts WHERE run_id=?', (rid,)).fetchone()[0] + db.execute("SELECT count(*) FROM run_tools WHERE run_id=? AND state!='planned'", (rid,)).fetchone()[0]
            if own_calls >= limits['max_steps'] * 8 or all_calls >= tree['max_steps'] * 8:
                raise ToolBudgetExhausted('The shared tree or child tool receipt budget is exhausted.')
            held = self._reserve(tree)['tool_receipts']
            if all_calls >= tree['max_steps'] * 8 - held:
                # Root review, verification and integration receipts stay admissible.
                raise ToolBudgetExhausted('The tool receipt budget children share is exhausted; the root keeps '
                                          + str(held) + ' receipts for review and its final answer.')
            db.execute("INSERT INTO agent_receipts VALUES(?,?,?,?,?,?,'running',NULL)", (rid, ident, generation, call_id, name, args))
            return None

    def end_tool(self, cid, rid, ident, generation, owner, call_id, result):
        value = receipt_json(result, 32000)
        with self._transaction() as db:
            self._root(db, cid, rid)
            node = self._node(db, rid, ident)
            if node['generation'] != generation or node['status'] in TERMINAL:
                return {'ignored': True, 'status': node['status'], 'origin': 'agent', 'authorization': 'none'}
            self._lease(db, node, generation, owner)
            old = db.execute('SELECT state,result,name FROM agent_receipts WHERE agent_id=? AND generation=? AND call_id=?', (ident, generation, call_id)).fetchone()
            if not old:
                raise ValueError('Child tool receipt not found.')
            if old[2] == 'AskParentQuestion':
                # The parent or a native form may resolve the question before this commit.
                value = encoded(self._ask_receipt(db, ident, generation, call_id, json.loads(value)))
            if old[0] == 'completed':
                if old[1] != value:
                    raise ValueError('A committed child receipt cannot be replaced.')
                return json.loads(value)
            if old[0] != 'running':
                raise ValueError('Uncertain tool results require host reconciliation.')
            db.execute("UPDATE agent_receipts SET state='completed',result=? WHERE agent_id=? AND generation=? AND call_id=?", (value, ident, generation, call_id))
            return json.loads(value)

    def receipts(self, cid, rid, ident, generation):
        with self.store.lock:
            self._root(self.store.db, cid, rid)
            self._generation(self._node(self.store.db, rid, ident), generation)
            cur = self.store.db.execute('SELECT call_id,name,state,result FROM agent_receipts WHERE run_id=? AND agent_id=? AND generation=? ORDER BY rowid LIMIT 200', (rid, ident, generation))
            return [{'call_id': r[0], 'name': r[1], 'state': r[2], 'result': json.loads(r[3]) if r[3] else None, 'origin': 'agent', 'authorization': 'none'} for r in cur]

    def _evidence(self, db, rid, ident, generation, evidence_ids):
        if not isinstance(evidence_ids, (list, tuple)) or len(evidence_ids) > 8 or len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError('Use up to eight unique child evidence IDs.')
        for call_id in evidence_ids:
            row = db.execute('SELECT state,result,name FROM agent_receipts WHERE run_id=? AND agent_id=? AND generation=? AND call_id=?', (rid, ident, generation, call_id)).fetchone()
            if not row or row[0] != 'completed' or not row[1]:
                raise ValueError('Evidence must be this generation\'s completed child receipts.')
            if row[2] in EFFECT_TOOLS:
                # The root owns effect evidence in its own receipt; a child copy is data.
                raise ValueError('Effect request receipts are data copies, not child evidence. The root verifies its own effect receipt.')
            value = json.loads(row[1])
            if not self.app.interactions.resolved_evidence({'state': row[0], 'result': value}):
                raise ValueError('Evidence must contain resolved successful outcomes.')

    def settle(self, cid, rid, ident, generation, status, result, evidence_ids=(), owner=None):
        if status not in TERMINAL - {'interrupted', 'stopped'}:
            raise ValueError('Workers settle completed, failed or budget_exhausted work.')
        value = receipt_json(result, 16000)
        with self._transaction() as db:
            self._root(db, cid, rid)
            node = self._node(db, rid, ident)
            if node['generation'] != generation or node['status'] in TERMINAL:
                return {**self._public(node), 'ignored': True}
            self._lease(db, node, generation, owner)
            self._evidence(db, rid, ident, generation, evidence_ids)
            if db.execute("SELECT 1 FROM agent_receipts WHERE agent_id=? AND generation=? AND state IN ('running','uncertain')", (ident, generation)).fetchone():
                raise ValueError('Commit tool outcomes before settling this child.')
            db.execute('UPDATE agent_nodes SET status=?,result=?,evidence_ids=?,updated=? WHERE id=? AND generation=? AND status IN (\'running\',\'waiting_parent\',\'waiting_input\')', (status, value, encoded(list(evidence_ids)), time.time(), ident, generation))
            db.execute('DELETE FROM agent_claims WHERE agent_id=?', (ident,))
            self._ended(db, cid, rid, ident, status != 'completed')
            self._wake(db, rid)
            return self._public(self._node(db, rid, ident))

    def _can_manage(self, root, actor, target):
        if actor != root['id'] and target['parent_id'] != actor:
            raise ValueError('Only the owning parent may manage this child.')

    def cancel(self, cid, rid, actor, target, generation=None, owner=None, origin='agent'):
        with self._transaction() as db:
            return self.cancel_in(db, cid, rid, actor, target, generation, owner, origin)

    def cancel_in(self, db, cid, rid, actor, target, generation=None, owner=None, origin='agent', live_only=False):
        """Caller owns the transaction, including native-question cancellation.

        origin ('human', 'agent' or 'host') labels revoked native forms. A host
        closure passes live_only: ended nodes keep their reports, leases and mail.
        """
        root = self._root(db, cid, rid)
        actor, _ = self._worker_actor(db, root, actor, generation, owner, 'StopAgent')
        if target == rid:
            if actor != rid:
                raise ValueError('Children cannot stop the root.')
            targets = [r[0] for r in db.execute('SELECT id FROM agent_nodes WHERE run_id=?', (rid,))]
        else:
            node = self._node(db, rid, target)
            self._can_manage(root, actor, node)
            targets = [target]
            for ident in targets:
                targets.extend(r[0] for r in db.execute('SELECT id FROM agent_nodes WHERE run_id=? AND parent_id=?', (rid, ident)))
        if live_only:
            targets = [ident for ident in targets if self._node(db, rid, ident)['status'] in ACTIVE]
        for ident in targets:
            db.execute("UPDATE agent_nodes SET status='stopped',updated=? WHERE id=? AND status IN ('queued','running','waiting_parent','waiting_input')", (time.time(), ident))
            db.execute('DELETE FROM agent_claims WHERE agent_id=?', (ident,))
            db.execute("UPDATE agent_path_leases SET status='revoked' WHERE agent_id=?", (ident,))
            db.execute('UPDATE agent_messages SET revoked=1 WHERE run_id=? AND (sender=? OR target=?) AND acked=0', (rid, ident, ident))
            self._drop_questions(db, ident)
            db.execute("UPDATE agent_waits SET status='cancelled' WHERE actor=? AND status='waiting'", (ident,))
        self._closed(db, rid, targets, origin)
        self._wake(db, rid)
        return {'agent_ids': targets, 'status': 'stopped', 'origin': 'agent', 'authorization': 'none'}

    def _ended(self, db, cid, rid, ident, cascade):
        """Close a child that can no longer answer or supervise, in the caller's transaction.

        Unless it completed, its live descendants stop with host provenance;
        completed descendant reports are never marked stopped. A completed
        parent's live descendants continue and escalate questions to the root.
        """
        self._drop_questions(db, ident)
        db.execute("UPDATE agent_waits SET status='cancelled' WHERE actor=? AND status='waiting'", (ident,))
        if cascade:
            self.cancel_in(db, cid, rid, rid, ident, origin='host', live_only=True)
        self._closed(db, rid, [ident], 'host')

    def _closed(self, db, rid, ids, origin):
        hook = getattr(self, 'on_close', None)
        if hook and ids:
            hook(db, rid, ids, origin)

    def _drop_questions(self, db, ident=None):
        """Cancel open questions of closed agents and refresh each asking receipt."""
        query = "SELECT id FROM agent_parent_questions WHERE status IN ('pending','promoted')"
        rows = db.execute(query + ' AND agent_id=?', (ident,)) if ident else db.execute(query)
        for (question_id,) in rows.fetchall():
            db.execute("UPDATE agent_parent_questions SET status='cancelled' WHERE id=?", (question_id,))
            self._refresh_ask(db, question_id)

    @staticmethod
    def _ask_receipt(db, ident, generation, call_id, result):
        """Recompute an AskParentQuestion outcome from its correlated durable question."""
        if not isinstance(result, dict) or not result.get('question_id'):
            return result
        row = db.execute('SELECT status,answer_origin FROM agent_parent_questions WHERE id=? AND agent_id=? AND generation=? AND call_id=?',
                         (result['question_id'], ident, generation, call_id)).fetchone()
        if not row or row[0] not in {'answered', 'cancelled'}:
            return result
        outcome = {**result, 'status': row[0]}
        if row[0] == 'answered' and row[1]:
            outcome['answer_origin'] = row[1]
        return outcome

    def _refresh_ask(self, db, question_id):
        """Bring only the asking child's committed receipt up to its question state."""
        row = db.execute('SELECT agent_id,generation,call_id FROM agent_parent_questions WHERE id=?', (question_id,)).fetchone()
        receipt = row and db.execute("SELECT result FROM agent_receipts WHERE agent_id=? AND generation=? AND call_id=? AND name='AskParentQuestion' AND state='completed'", row).fetchone()
        if receipt and receipt[0]:
            value = encoded(self._ask_receipt(db, *row, json.loads(receipt[0])))
            db.execute('UPDATE agent_receipts SET result=? WHERE agent_id=? AND generation=? AND call_id=?', (value, *row))

    def _related(self, root, sender, target):
        sid = sender['id'] if sender else root['id']
        tid = target['id'] if target else root['id']
        return (sid == tid or not sender and target['parent_id'] is None
                or not target and sender['parent_id'] is None
                or sender and target and (sender['parent_id'] == tid or target['parent_id'] == sid or sender['parent_id'] == target['parent_id']))

    def send(self, cid, rid, actor, target, body, kind='message', correlation_id=None,
             idempotency_key=None, generation=None, owner=None):
        body = ordinary(body, 8192)
        if kind not in {'message', 'task', 'result', 'question', 'answer'}:
            raise ValueError('Unsupported agent message kind.')
        key = identifier(idempotency_key or uuid.uuid4().hex)
        if correlation_id is not None:
            identifier(correlation_id)
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            actor, sender = self._worker_actor(db, root, actor, generation, owner, 'SendAgentMessage')
            target, recipient = self._actor(db, root, target)
            if recipient and recipient['status'] in TERMINAL:
                raise ValueError('Settled agents cannot receive new messages.')
            if not self._related(root, sender, recipient):
                raise ValueError('Messaging is limited to parents, children and direct peers.')
            generation = sender['generation'] if sender else 0
            old = self._row(db.execute('SELECT * FROM agent_messages WHERE run_id=? AND sender=? AND idempotency_key=?', (rid, actor, key)))
            if old:
                if (old['target'], old['body'], old['kind'], old['correlation_id'], old['generation']) != (target, body, kind, correlation_id, generation):
                    raise ValueError('Message idempotency key conflicts.')
                return self._message(old)
            if db.execute('SELECT count(*) FROM agent_messages WHERE run_id=?', (rid,)).fetchone()[0] >= 128:
                raise ValueError('This tree reached its 128-message limit.')
            ident = uuid.uuid4().hex
            sequence = db.execute('SELECT coalesce(max(sequence),0)+1 FROM agent_messages WHERE run_id=?', (rid,)).fetchone()[0]
            db.execute('INSERT INTO agent_messages VALUES(?,?,?,?,?,?,?,?,?,?,0,0,?)', (ident, rid, sequence, actor, target, generation, body, kind, correlation_id, key, time.time()))
            self._wake(db, rid)
            return self._message(self._row(db.execute('SELECT * FROM agent_messages WHERE id=?', (ident,))))

    @staticmethod
    def _message(row):
        return {**row, 'body': json.loads(row['body']), 'origin': 'agent', 'authorization': 'none'}

    def deliver(self, cid, rid, actor, after=0, limit=20):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('Use a nonnegative cursor and one to twenty messages per boundary.')
        with self.store.lock:
            db = self.store.db
            root = self._root(db, cid, rid)
            actor, _ = self._actor(db, root, actor)
            cur = db.execute('SELECT * FROM agent_messages WHERE run_id=? AND target=? AND sequence>? AND acked=0 AND revoked=0 ORDER BY sequence LIMIT ?', (rid, actor, after, limit))
            names = [d[0] for d in cur.description]
            items = [self._message(dict(zip(names, r))) for r in cur.fetchall()]
            return {'messages': items, 'cursor': items[-1]['sequence'] if items else after, 'origin': 'agent', 'authorization': 'none'}

    @staticmethod
    def _mail_text(body):
        value = json.loads(body)
        return value if isinstance(value, str) else encoded(value)

    @staticmethod
    def _mail_cursor(db, message_id):
        row = db.execute('SELECT cursor FROM agent_mail_reads WHERE message_id=?', (message_id,)).fetchone()
        return row[0] if row else 0

    def read_mail(self, cid, rid, message_id, offset=None, limit=1200):
        """One root-mail page at the host read cursor; changes no state.

        RunJournal.end_tool commits the cursor, and the final page's ack, with
        the completed ReadAgentMessage receipt (resolve_mail_receipt).
        """
        if (not isinstance(message_id, str) or offset is not None and (type(offset) is not int or offset < 0)
                or type(limit) is not int or not 1 <= limit <= 6000):
            raise ValueError('Use a message ID and a nonnegative offset.')
        with self.store.lock:
            db = self.store.db
            self._root(db, cid, rid, True)
            row = self._row(db.execute('SELECT * FROM agent_messages WHERE id=? AND run_id=? AND target=? AND revoked=0', (message_id, rid, rid)))
            if not row:
                raise ValueError('Message not found in this root mailbox.')
            if row['acked']:
                raise ValueError('This message was already read in full.')
            cursor = self._mail_cursor(db, message_id)
            if offset is not None and offset != cursor:
                raise ValueError('Read mail in order: continue this message at offset ' + str(cursor) + '.')
            text = self._mail_text(row['body'])
            page = text[cursor:cursor + limit]
            final = cursor + len(page) >= len(text)
            return {'message_id': message_id, 'sender': row['sender'], 'kind': row['kind'], 'correlation_id': row['correlation_id'],
                    'offset': cursor, 'next_offset': None if final else cursor + len(page), 'total_characters': len(text),
                    'untrusted_body': page, 'status': 'read', 'acknowledged': False, 'origin': 'agent', 'authorization': 'none'}

    @classmethod
    def resolve_mail_receipt(cls, db, rid, call_id, result):
        """Runtime end_tool commits a root mail page with its completed receipt.

        Only a running ReadAgentMessage receipt whose page matches the stored
        body at the current cursor advances it; the final page acknowledges
        the message. Otherwise nothing changes and the receipt is stale. The
        result is updated in place, so the root sees the committed outcome.
        """
        if not isinstance(result, dict) or result.get('status') != 'read' or not isinstance(result.get('message_id'), str):
            return result
        tool = db.execute('SELECT name,state FROM run_tools WHERE run_id=? AND call_id=?', (rid, call_id)).fetchone()
        if not tool or tool != ('ReadAgentMessage', 'running'):
            return result
        ident, page = result['message_id'], result.get('untrusted_body')
        row = db.execute('SELECT body,acked FROM agent_messages WHERE id=? AND run_id=? AND target=? AND revoked=0', (ident, rid, rid)).fetchone()
        cursor = cls._mail_cursor(db, ident)
        text = cls._mail_text(row[0]) if row else ''
        if not row or row[1] or result.get('offset') != cursor or not isinstance(page, str) or text[cursor:cursor + len(page)] != page:
            result.update(status='stale', acknowledged=False, untrusted_body=None, next_offset=None, cursor=cursor,
                          error='This mail page was not committed. Read again at the host cursor.')
            return result
        end = cursor + len(page)
        db.execute('INSERT INTO agent_mail_reads VALUES(?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET cursor=excluded.cursor,'
                   'call_id=excluded.call_id,updated=excluded.updated', (ident, rid, end, call_id, time.time()))
        if end >= len(text):
            db.execute('UPDATE agent_messages SET acked=1 WHERE id=? AND acked=0', (ident,))
        result.update(acknowledged=end >= len(text), next_offset=None if end >= len(text) else end)
        return result

    def ack(self, cid, rid, actor, ids):
        if not isinstance(ids, (list, tuple)) or len(ids) > 20 or any(not isinstance(i, str) for i in ids):
            raise ValueError('Acknowledge up to twenty own message IDs.')
        with self._transaction() as db:
            root = self._root(db, cid, rid)
            actor, _ = self._actor(db, root, actor)
            for ident in ids:
                if not db.execute('SELECT 1 FROM agent_messages WHERE id=? AND run_id=? AND target=? AND revoked=0', (ident, rid, actor)).fetchone():
                    raise ValueError('Message does not belong to this recipient.')
            for ident in ids:
                db.execute('UPDATE agent_messages SET acked=1 WHERE id=?', (ident,))
            return {'acked': list(ids), 'origin': 'agent', 'authorization': 'none'}

    @staticmethod
    def _addressed(db, rid, actor):
        """Pending questions an actor must handle, oldest first.

        Children handle only their direct children's questions. The root also
        inherits questions whose direct parent has ended, so none is orphaned.
        """
        if actor != rid:
            return db.execute("SELECT * FROM agent_parent_questions WHERE run_id=? AND parent_id=? AND status='pending' ORDER BY created", (rid, actor))
        ended = sorted(TERMINAL)
        return db.execute("SELECT q.* FROM agent_parent_questions q LEFT JOIN agent_nodes p ON p.id=q.parent_id AND p.run_id=q.run_id "
                          "WHERE q.run_id=? AND q.status='pending' AND (q.parent_id=? OR p.status IN (" + ','.join('?' * len(ended)) + ')) ORDER BY q.created',
                          (rid, rid, *ended))

    def _predicate(self, db, rid, actor, ids, cursor):
        settled = [ident for ident in ids if self._node(db, rid, ident)['status'] in TERMINAL]
        questions = [r[0] for r in self._addressed(db, rid, actor).fetchall()]
        messages = [r[0] for r in db.execute('SELECT id FROM agent_messages WHERE run_id=? AND target=? AND sequence>? AND acked=0 AND revoked=0 ORDER BY sequence LIMIT 20', (rid, actor, cursor))]
        followups = [r[0] for r in db.execute("SELECT id FROM run_inbox WHERE run_id=? AND origin='human' AND status='pending' ORDER BY sequence LIMIT 20", (rid,))] if actor == rid else []
        # Like questions, a live child's unstarted effect request needs the root's
        # explicit ExecuteAgentEffect or DismissAgentEffect before another wait.
        effects = self._effect_requests(db, rid) if actor == rid else []
        if settled or questions or effects or messages or followups:
            ready = {'status': 'ready', 'agent_ids': settled, 'question_ids': questions, 'message_ids': messages,
                     'input_ids': followups, 'reason': 'human_followup' if followups else 'parent_question' if questions
                     else 'agent_effect_request' if effects else 'child_settled' if settled else 'agent_message'}
            if effects:
                ready['effect_ids'] = effects
            if settled:
                # Enough to review without ReadAgentTree: status, final evidence, patches, a bounded summary.
                ready.update(self._settled_results(db, rid, settled))
            return ready
        return None

    @staticmethod
    def _effect_requests(db, rid):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_effects'").fetchone():
            return []
        return [r[0] for r in db.execute(
            "SELECT e.id FROM agent_effects e JOIN agent_nodes n ON n.id=e.agent_id AND n.run_id=e.run_id AND n.generation=e.generation "
            "WHERE e.run_id=? AND e.status='proposed' AND n.status IN ('running','waiting_parent','waiting_input') ORDER BY e.created LIMIT 8", (rid,))]

    def notify_in(self, db, rid, target, body, correlation_id, key):
        """Host notice to one live child in the caller's transaction.

        It is ordinary root-to-child agent mail, never human input or authority.
        Idempotent per key; a full tree mailbox skips it (the child can still read).
        """
        node = db.execute('SELECT status FROM agent_nodes WHERE id=? AND run_id=?', (target, rid)).fetchone()
        if not node or node[0] in TERMINAL:
            return None
        body, key = ordinary(body, 2000), identifier(key)
        identifier(correlation_id)
        old = db.execute('SELECT id FROM agent_messages WHERE run_id=? AND sender=? AND idempotency_key=?', (rid, rid, key)).fetchone()
        if old:
            return old[0]
        if db.execute('SELECT count(*) FROM agent_messages WHERE run_id=?', (rid,)).fetchone()[0] >= 128:
            return None
        ident = uuid.uuid4().hex
        sequence = db.execute('SELECT coalesce(max(sequence),0)+1 FROM agent_messages WHERE run_id=?', (rid,)).fetchone()[0]
        db.execute('INSERT INTO agent_messages VALUES(?,?,?,?,?,0,?,?,?,?,0,0,?)', (ident, rid, sequence, rid, target, body, 'result', correlation_id, key, time.time()))
        self._wake(db, rid)
        return ident

    def wait(self, cid, rid, actor, call_id, ids, after=0, generation=None, owner=None):
        identifier(call_id)
        if not isinstance(ids, list) or len(ids) > 8 or len(set(ids)) != len(ids) or type(after) is not int or after < 0:
            raise ValueError('Wait on up to eight unique related agents with a nonnegative cursor.')
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            actor, caller = self._worker_actor(db, root, actor, generation, owner, 'WaitForAgents')
            for ident in ids:
                target = self._node(db, rid, ident)
                if ident == actor or actor != rid and not self._related(root, caller, target):
                    raise ValueError('Wait targets must belong to this parent or peer scope.')
            previous = self._row(db.execute('SELECT * FROM agent_waits WHERE run_id=? AND actor=? AND call_id=?', (rid, actor, call_id)))
            if previous:
                if previous['agent_ids'] != encoded(ids) or previous['cursor'] != after:
                    raise ValueError('Wait call identity conflicts.')
                return self._wait_receipt(previous)
            ready = self._predicate(db, rid, actor, ids, after)
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO agent_waits VALUES(?,?,?,?,?,?,?,?,0,?)', (ident, rid, actor, call_id, encoded(ids), after, 'ready' if ready else 'waiting', encoded(ready) if ready else None, time.time()))
            if caller and not ready:
                db.execute("UPDATE agent_nodes SET status='waiting_parent',updated=? WHERE id=? AND status='running'", (time.time(), actor))
            return self._wait_receipt(self._row(db.execute('SELECT * FROM agent_waits WHERE id=?', (ident,))))

    @staticmethod
    def _wait_receipt(row):
        result = json.loads(row['result']) if row['result'] else {'status': 'awaiting_agent_results'}
        if row['status'] == 'cancelled':
            result = {'status': 'cancelled'}
        return {**result, 'required': result['status'] == 'awaiting_agent_results',
                'wait_id': row['id'], 'call_id': row['call_id'], 'origin': 'agent', 'authorization': 'none'}

    @classmethod
    def resolve_wait_receipt(cls, db, rid, call_id, result):
        """Runtime end_tool calls this inside its own transaction to close the wait race."""
        if not isinstance(result, dict) or not result.get('wait_id'):
            return result
        row = cls._row(db.execute('SELECT * FROM agent_waits WHERE id=? AND run_id=? AND actor=? AND call_id=?', (result['wait_id'], rid, rid, call_id)))
        return {**result, **cls._wait_receipt(row)} if row else result

    @staticmethod
    def has_pending_wait(db, rid):
        return bool(db.execute("SELECT 1 FROM agent_waits WHERE run_id=? AND actor=? AND status='waiting' LIMIT 1", (rid, rid)).fetchone())

    def _wake(self, db, rid):
        cur = db.execute("SELECT * FROM agent_waits WHERE run_id=? AND status='waiting'", (rid,))
        names = [d[0] for d in cur.description]
        for values in cur.fetchall():
            row = dict(zip(names, values))
            ready = self._predicate(db, rid, row['actor'], json.loads(row['agent_ids']), row['cursor'])
            if not ready:
                continue
            db.execute("UPDATE agent_waits SET status='ready',result=? WHERE id=? AND status='waiting'", (encoded(ready), row['id']))
            if row['actor'] != rid:
                if not db.execute("SELECT 1 FROM agent_parent_questions WHERE agent_id=? AND status IN ('pending','promoted')", (row['actor'],)).fetchone():
                    db.execute("UPDATE agent_nodes SET status='running' WHERE id=? AND status='waiting_parent'", (row['actor'],))
                continue
            run = db.execute('SELECT state FROM runs WHERE id=?', (rid,)).fetchone()
            if not run or run[0] not in ROOT_LIVE:
                continue
            # One correlated notification per wait, whether result wins before or after end_tool.
            receipt = self._wait_receipt({**row, 'status': 'ready', 'result': encoded(ready)})
            self.app.interactions._input(db, rid, 'agent_wait_ready', 'agent', receipt, 'agent-wait-' + row['id'])
            db.execute('UPDATE agent_waits SET notified=1 WHERE id=?', (row['id'],))
            old = db.execute("SELECT result FROM run_tools WHERE run_id=? AND call_id=? AND state='completed'", (rid, row['call_id'])).fetchone()
            if old and old[0]:
                db.execute('UPDATE run_tools SET result=? WHERE run_id=? AND call_id=?', (encoded(self.resolve_wait_receipt(db, rid, row['call_id'], json.loads(old[0]))), rid, row['call_id']))
            if run[0] == 'waiting_input' and self.app.interactions._ready(db, rid):
                self.app.interactions._queue(db, rid, 'agent_wait_ready')

    def ask_parent(self, cid, rid, ident, generation, owner, call_id, items):
        identifier(call_id)
        items = question_items(items)
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            node = self._node(db, rid, ident)
            self._lease(db, node, generation, owner)
            if node['status'] != 'running':
                raise ValueError('Only running children may ask their parent.')
            if db.execute('SELECT 1 FROM agent_parent_questions WHERE agent_id=? AND generation=? AND call_id=?', (ident, generation, call_id)).fetchone():
                raise ValueError('This tool already requested a parent question.')
            if db.execute("SELECT count(*) FROM agent_parent_questions WHERE run_id=? AND status IN ('pending','promoted')", (rid,)).fetchone()[0] >= 8:
                raise ValueError('Resolve pending parent questions first.')
            qid = uuid.uuid4().hex
            db.execute("INSERT INTO agent_parent_questions(id,run_id,agent_id,generation,parent_id,call_id,items,status,created) VALUES(?,?,?,?,?,?,?,'pending',?)", (qid, rid, ident, generation, node['parent_id'] or root['id'], call_id, encoded(items), time.time()))
            db.execute("UPDATE agent_nodes SET status='waiting_parent',updated=? WHERE id=?", (time.time(), ident))
            self._wake(db, rid)
            return {'question_id': qid, 'agent_id': ident, 'generation': generation, 'status': 'awaiting_parent', 'origin': 'agent', 'authorization': 'none'}

    def parent_questions(self, cid, rid, actor):
        """Questions this actor should handle; `escalated` marks root-inherited ones."""
        with self.store.lock:
            db = self.store.db
            root = self._root(db, cid, rid)
            actor, _ = self._actor(db, root, actor)
            cur = self._addressed(db, rid, actor)
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
            return [{**row, 'items': json.loads(row['items']), 'escalated': row['parent_id'] != actor,
                     'origin': 'agent', 'authorization': 'none'} for row in rows]

    def parent_question(self, cid, rid, actor, question_id):
        with self.store.lock:
            db = self.store.db
            root = self._root(db, cid, rid)
            actor, _ = self._actor(db, root, actor)
            question = self._row(db.execute('SELECT * FROM agent_parent_questions WHERE id=? AND run_id=?', (question_id, rid)))
            # The root oversees every question in its own tree; children see only their own.
            if not question or actor not in {question['agent_id'], question['parent_id'], rid}:
                raise ValueError('Question not found in this child or parent scope.')
            return {**question, 'items': json.loads(question['items']),
                    'answer': json.loads(question['answer']) if question['answer'] else None,
                    'origin': 'agent', 'authorization': 'none'}

    def answer_parent(self, cid, rid, actor, question_id, answers, generation=None, owner=None):
        """Agent-origin answer. A child answers its direct children; the root may
        answer any pending question in its tree. A promoted native form or a
        human answer is never overwritten."""
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            actor, _ = self._worker_actor(db, root, actor, generation, owner, 'AnswerAgentQuestion')
            question = self._row(db.execute('SELECT * FROM agent_parent_questions WHERE id=? AND run_id=?', (question_id, rid)))
            if not question or question['parent_id'] != actor and actor != rid:
                raise ValueError('Parent question not found in this owner scope.')
            answer = encoded(self.app.interactions.canonical_answer(json.loads(question['items']), answers))
            if question['status'] == 'answered' and question['answer'] == answer and question['answer_origin'] in (None, 'agent'):
                return {'question_id': question_id, 'status': 'answered', 'answer_origin': 'agent', 'origin': 'agent', 'authorization': 'none'}
            child = self._node(db, rid, question['agent_id'])
            if question['status'] != 'pending' or child['status'] in TERMINAL or child['generation'] != question['generation']:
                raise ValueError('This parent question is no longer pending.')
            db.execute("UPDATE agent_parent_questions SET status='answered',answer=?,answer_origin='agent' WHERE id=? AND status='pending'", (answer, question_id))
            self._refresh_ask(db, question_id)
            if not db.execute("SELECT 1 FROM agent_waits WHERE actor=? AND status='waiting'", (child['id'],)).fetchone():
                db.execute("UPDATE agent_nodes SET status='running',updated=? WHERE id=? AND status='waiting_parent'", (time.time(), child['id']))
            return {'question_id': question_id, 'agent_id': child['id'], 'status': 'answered', 'answers': json.loads(answer),
                    'answer_origin': 'agent', 'escalated': question['parent_id'] != actor, 'origin': 'agent', 'authorization': 'none'}

    def assert_path(self, cid, rid, actor, path, generation=None, baseline=None):
        path = normalized_path(path)
        if baseline is not None and (not isinstance(baseline, str) or not re.fullmatch(r'[0-9a-f]{64}|absent', baseline)):
            raise ValueError('Path baseline must be a host SHA256 or absent marker.')
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            actor, node = self._actor(db, root, actor, live=True)
            if not node:
                for (held,) in db.execute("SELECT path FROM agent_path_leases WHERE run_id=? AND status='held'", (rid,)):
                    if conflict(path, held):
                        raise ValueError('Root writes require explicit parent path release for child integration.')
                return {'path': path, 'agent_id': actor, 'generation': 0, 'origin': 'agent', 'authorization': 'none'}
            self._generation(node, generation)
            leases = db.execute("SELECT path,baseline FROM agent_path_leases WHERE run_id=? AND agent_id=? AND generation=? AND status='held'", (rid, actor, generation)).fetchall()
            lease = next(((p, b) for p, b in leases if path == p), None)
            if not lease:
                raise ValueError('Child proposal lies outside its exact path lease.')
            if baseline is not None:
                if lease[1] is not None and lease[1] != baseline:
                    raise ValueError('Child path baseline changed.')
                db.execute('UPDATE agent_path_leases SET baseline=? WHERE run_id=? AND path=?', (baseline, rid, path))
            return {'path': path, 'agent_id': actor, 'generation': generation, 'baseline': baseline or lease[1], 'origin': 'agent', 'authorization': 'none'}

    def release_paths(self, cid, rid, actor, ident, generation):
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            actor, _ = self._actor(db, root, actor, live=True)
            node = self._node(db, rid, ident)
            self._generation(node, generation)
            self._can_manage(root, actor, node)
            if node['status'] not in TERMINAL:
                raise ValueError('Settle or stop the child before releasing integration paths.')
            db.execute("UPDATE agent_path_leases SET status='released' WHERE run_id=? AND agent_id=? AND generation=?", (rid, ident, generation))
            return {'agent_id': ident, 'generation': generation, 'status': 'released', 'origin': 'agent', 'authorization': 'none'}

    def review(self, cid, rid, actor, ident, generation, evidence_ids):
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            actor, _ = self._actor(db, root, actor, live=True)
            node = self._node(db, rid, ident)
            self._generation(node, generation)
            self._can_manage(root, actor, node)
            final = json.loads(node['evidence_ids'])

            def refused(reason, rejected=()):
                # Every refusal names the IDs this generation would accept (none unless completed).
                return refuse({'kind': 'review', 'limit': 'final_evidence', 'reason': reason, 'agent_id': ident,
                               'generation': generation, 'status': node['status'],
                               'accepted_evidence_ids': final if node['status'] == 'completed' else [],
                               'rejected_evidence_ids': [str(item)[:120] for item in rejected][:8]})

            if node['status'] != 'completed':
                raise refused('Parent acceptance requires completed child evidence; this child is ' + node['status'] + '.')
            if not evidence_ids:
                raise refused('Parent acceptance requires completed child evidence.')
            listed = list(evidence_ids) if isinstance(evidence_ids, (list, tuple)) else []
            outside = [item for item in listed if item not in final]
            if outside:
                raise refused('Review must reference the child final result evidence.', outside)
            try:
                self._evidence(db, rid, ident, generation, evidence_ids)
            except ValueError as exc:
                raise refused(str(exc), listed) from None
            db.execute("UPDATE agent_nodes SET review='accepted',updated=? WHERE id=? AND generation=?", (time.time(), ident, generation))
            return {'agent_id': ident, 'generation': generation, 'review': 'accepted', 'evidence_ids': list(evidence_ids), 'origin': 'agent', 'authorization': 'none'}

    def default_evidence(self, cid, rid, ident, generation):
        """The child's final evidence IDs, less Read receipts that a later Read of the
        same path in this generation superseded. Host records only; order kept."""
        with self.store.lock:
            db = self.store.db
            self._root(db, cid, rid)
            node = self._node(db, rid, ident)
            self._generation(node, generation)
            reads = []
            for call_id, result in db.execute("SELECT call_id,result FROM agent_receipts WHERE run_id=? AND agent_id=? AND generation=? "
                                              "AND name='Read' AND state='completed' ORDER BY rowid", (rid, ident, generation)).fetchall():
                value = json.loads(result) if result else None
                reads.append((call_id, value.get('path') if isinstance(value, dict) else None))
            superseded = {call_id for index, (call_id, path) in enumerate(reads)
                          if path and any(later == path for _, later in reads[index + 1:])}
            return [item for item in json.loads(node['evidence_ids']) if item not in superseded]

    def dismiss(self, cid, rid, actor, ident, generation):
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            actor, _ = self._actor(db, root, actor, live=True)
            node = self._node(db, rid, ident)
            self._generation(node, generation)
            self._can_manage(root, actor, node)
            if node['status'] not in TERMINAL:
                raise ValueError('Settle or stop work before dismissing its outcome.')
            if node['review'] == 'accepted':
                raise ValueError('An accepted result cannot later be dismissed.')
            db.execute("UPDATE agent_nodes SET review='dismissed',updated=? WHERE id=? AND generation=?", (time.time(), ident, generation))
            # A dismissed proposal can never be integrated, so its settled generation's
            # leases end here and the root may take the work over. RetryAgent reacquires.
            db.execute("UPDATE agent_path_leases SET status='released' WHERE run_id=? AND agent_id=? AND generation=? AND status='held'",
                       (rid, ident, generation))
            return {'agent_id': ident, 'generation': generation, 'review': 'dismissed', 'origin': 'agent', 'authorization': 'none'}

    def rounds(self, cid, rid, ident, generation):
        """Review refusals and root steers already recorded on this child generation."""
        with self.store.lock:
            db = self.store.db
            self._root(db, cid, rid)
            self._generation(self._node(db, rid, ident), generation)
            return db.execute('SELECT count(*) FROM agent_rounds WHERE run_id=? AND agent_id=? AND generation=?',
                              (rid, ident, generation)).fetchone()[0]

    def record_round(self, cid, rid, ident, generation, call_id, kind):
        """Record one review refusal or root steer once per call; returns the new count."""
        if kind not in ('review', 'steer'):
            raise ValueError('Rounds are review refusals or root steers.')
        if not isinstance(call_id, str) or not 1 <= len(call_id) <= 400:
            raise ValueError('Use a bounded host call identifier.')
        with self._transaction() as db:
            self._root(db, cid, rid)
            self._generation(self._node(db, rid, ident), generation)
            db.execute('INSERT OR IGNORE INTO agent_rounds VALUES(?,?,?,?,?,?)', (rid, ident, generation, call_id, kind, time.time()))
            return db.execute('SELECT count(*) FROM agent_rounds WHERE run_id=? AND agent_id=? AND generation=?',
                              (rid, ident, generation)).fetchone()[0]

    def cost(self, cid, rid, ident, generation):
        """Content-free cost of one child generation: completed model steps and
        provider-reported tokens (None when any attempt reported no count)."""
        with self.store.lock:
            db = self.store.db
            self._root(db, cid, rid)
            node = self._node(db, rid, ident)
            steps = db.execute("SELECT count(*) FROM agent_dispatches WHERE run_id=? AND actor=? AND generation=? AND status='completed'",
                               (rid, ident, generation)).fetchone()[0]
            rows = db.execute("SELECT u.input_tokens,u.output_tokens FROM agent_dispatches d LEFT JOIN agent_usage u "
                              "ON u.run_id=d.run_id AND u.dispatch_id=d.id WHERE d.run_id=? AND d.actor=? AND d.generation=? "
                              "AND d.status!='estimated'", (rid, ident, generation)).fetchall()
            known = all(value is not None for row in rows for value in row)
            return {'task_kind': task_kind(json.loads(node['write_paths']), bool(set(json.loads(node['tools'])) & WORKTREE_TOOLS)),
                    'status': node['status'], 'steps': steps, 'tokens': sum(sum(row) for row in rows) if known else None}

    def _brief(self, db, rid, node, tables):
        """One compact, bounded entry: host IDs, counts and an untrusted summary prefix."""
        ident, generation = node['id'], node['generation']
        patches, paths = [], set()
        if 'agent_patches' in tables:
            for patch_id, path, status in db.execute('SELECT id,path,status FROM agent_patches WHERE run_id=? AND agent_id=? AND generation=? '
                                                     'ORDER BY created,id', (rid, ident, generation)).fetchall():
                paths.add(path)
                if status == 'proposed':
                    patches.append(patch_id)
        if 'agent_worktree_writes' in tables:
            paths |= {r[0] for r in db.execute('SELECT DISTINCT path FROM agent_worktree_writes WHERE run_id=? AND agent_id=? AND generation=?',
                                               (rid, ident, generation))}
        report = json.loads(node['result']) if node['result'] else None
        if isinstance(report, dict):
            report = report.get('text') or report.get('error')
        summary = '' if report is None else report if isinstance(report, str) else encoded(report)
        item = {'agent_id': ident, 'name': node['name'], 'generation': generation, 'status': node['status'],
                'review': node['review'], 'evidence_ids': json.loads(node['evidence_ids']), 'changed_paths': len(paths),
                'patch_ids': patches[:8], 'untrusted_summary': summary[:SUMMARY_CHARS]}
        if len(summary) > SUMMARY_CHARS:
            item['summary_truncated'] = True
        return item

    @staticmethod
    def _tables(db):
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('agent_patches','agent_worktree_writes')")}

    @staticmethod
    def _bounded(items, limit=RESULTS_LIMIT):
        """Shrink summaries, then drop them, then entries, until the encoding fits."""
        for chars in (SUMMARY_CHARS, 80, 0):
            for item in items:
                summary = item.pop('untrusted_summary', None)
                if summary is None:
                    continue
                if len(summary) > chars:
                    item['summary_truncated'] = True
                if chars:
                    item['untrusted_summary'] = summary[:chars]
            if len(encoded(items)) <= limit:
                return items, 0
        kept = list(items)
        while kept and len(encoded(kept)) > limit:
            kept.pop()
        return kept, len(items) - len(kept)

    @staticmethod
    def _small_root(db, rid):
        """The root's pinned route is a 4K window (the runner's rule): its wait payloads keep the
        compact Phase 14 form and the small brief's ReadAgentTree inspection, so protected inbox
        units stay within the small-window floor."""
        row = db.execute('SELECT config FROM runs WHERE id=?', (rid,)).fetchone()
        try:
            route = json.loads(row[0]).get('route') or {}
        except (TypeError, ValueError, AttributeError):
            return True
        budget = 4096 if route.get('kind') == 'gguf' else route.get('budget') or route.get('context') or 4096
        return budget <= 4096

    def _settled_results(self, db, rid, ids):
        if self._small_root(db, rid):
            return {}
        tables = self._tables(db)
        items, omitted = self._bounded([self._brief(db, rid, self._node(db, rid, ident), tables) for ident in ids])
        packet = {'results': items, 'results_note': WAIT_NOTE}
        if omitted:
            packet['omitted_results'] = omitted
        return packet

    def compact(self, cid, rid):
        """Bounded index of every node in this tree: no task, context, tools,
        limits or receipts. Native agent_get keeps full records."""
        with self.store.lock:
            db = self.store.db
            self._root(db, cid, rid)
            tables = self._tables(db)
            rows = db.execute('SELECT id FROM agent_nodes WHERE run_id=? ORDER BY created,id', (rid,)).fetchall()
            nodes = []
            for (ident,) in rows:
                node = self._node(db, rid, ident)
                item = self._brief(db, rid, node, tables)
                item['parent_id'] = node['parent_id']
                if node['status'] not in TERMINAL:
                    item.pop('untrusted_summary', None)
                nodes.append(item)
            items, omitted = self._bounded(nodes, 6000)
            return {'nodes': items, 'omitted_nodes': omitted, 'origin': 'agent', 'authorization': 'none'}

    def delegate(self, cid, rid, actor, ident, task=None, dependencies=None, generation=None, owner=None):
        """Reassign only unclaimed queued work; its stable generation is not a retry."""
        if task is not None:
            task = text(task, 8000)
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            actor, caller = self._worker_actor(db, root, actor, generation, owner, 'DelegateAgentTask')
            node = self._node(db, rid, ident)
            if actor != rid and not self._related(root, caller, node):
                raise ValueError('Delegation is scoped to parents and peers.')
            if node['status'] != 'queued' or db.execute('SELECT 1 FROM agent_claims WHERE agent_id=?', (ident,)).fetchone():
                raise ValueError('Running or claimed tasks cannot be reassigned.')
            deps = json.loads(node['dependencies']) if dependencies is None else dependencies
            if not isinstance(deps, list) or len(deps) > 8 or len(set(deps)) != len(deps):
                raise ValueError('Use bounded unique dependencies.')
            graph = {r[0]: json.loads(r[1]) for r in db.execute('SELECT id,dependencies FROM agent_nodes WHERE run_id=?', (rid,))}
            if any(d not in graph for d in deps):
                raise ValueError('Dependencies must belong to this tree.')
            graph[ident] = deps
            def visit(current, visiting, done):
                if current in visiting:
                    raise ValueError('Agent dependencies cannot form a cycle.')
                if current in done:
                    return
                visiting.add(current)
                for dep in graph[current]:
                    visit(dep, visiting, done)
                visiting.remove(current)
                done.add(current)
            done = set()
            for current in graph:
                visit(current, set(), done)
            db.execute('UPDATE agent_nodes SET task=?,dependencies=?,updated=? WHERE id=? AND status=\'queued\'', (task or node['task'], encoded(deps), time.time(), ident))
            return self._public(self._node(db, rid, ident))

    def retry(self, cid, rid, actor, ident):
        """Explicit parent retry creates a new generation without replenishing budgets."""
        with self._transaction() as db:
            root = self._root(db, cid, rid, True)
            actor, _ = self._actor(db, root, actor, live=True)
            node = self._node(db, rid, ident)
            self._can_manage(root, actor, node)
            if node['status'] != 'interrupted':
                raise ValueError('Only interrupted children may be retried explicitly.')
            if set(json.loads(node['tools'])) & WORKTREE_TOOLS:
                # The worktree is pinned to the interrupted generation.
                raise ValueError('A worktree child cannot be retried: its worktree belongs to the interrupted generation. Integrate or retire it, then spawn a new child.')
            # Children admit only reads and durable proposal/coordination tools.
            # Preserve unknown historical results, explicitly retire them, and
            # start a new generation; never replay the old receipt identity.
            for (name,) in db.execute("SELECT name FROM agent_receipts WHERE agent_id=? AND state='uncertain'", (ident,)):
                if name not in CHILD_TOOLS:
                    raise ValueError('Reconcile unsafe child tools before retrying.')
            paths = json.loads(node['write_paths'])
            from .process_jobs import live_writer
            config = json.loads(root['config'])
            if paths and live_writer(db, config['workspace'], config.get('workspace_identity')):
                raise ValueError('Wait for the writable background command before reacquiring child write paths.')
            held = db.execute("SELECT path FROM agent_path_leases WHERE run_id=? AND status='held' AND agent_id!=?", (rid, ident)).fetchall()
            if any(conflict(path, other) for path in paths for (other,) in held):
                raise ValueError('Retry cannot reacquire a conflicting path lease.')
            generation = node['generation'] + 1
            db.execute("UPDATE agent_receipts SET state='interrupted' WHERE agent_id=? AND state='uncertain'", (ident,))
            db.execute("UPDATE agent_nodes SET generation=?,status='queued',result=NULL,evidence_ids='[]',review='pending',updated=? WHERE id=?", (generation, time.time(), ident))
            for path in paths:
                db.execute("INSERT INTO agent_path_leases VALUES(?,?,?,?,NULL,'held') ON CONFLICT(run_id,path) DO UPDATE SET agent_id=excluded.agent_id,generation=excluded.generation,status='held'", (rid, path, ident, generation))
            db.execute('DELETE FROM agent_claims WHERE agent_id=?', (ident,))
            return self._public(self._node(db, rid, ident))

    def recover(self):
        with self._transaction() as db:
            now = time.time()
            rows = db.execute("SELECT run_id,id,actor,started FROM agent_dispatches WHERE status='running'").fetchall()
            for rid, ident, actor, started in rows:
                elapsed = max(0., now - started)
                db.execute("UPDATE agent_dispatches SET status='interrupted',elapsed=? WHERE run_id=? AND id=?", (elapsed, rid, ident))
                if actor != rid:
                    db.execute('UPDATE agent_trees SET child_seconds=child_seconds+? WHERE run_id=?', (elapsed, rid))
                    db.execute('UPDATE agent_nodes SET elapsed=elapsed+? WHERE id=?', (elapsed, actor))
            db.execute("UPDATE agent_nodes SET status='interrupted',updated=? WHERE status IN ('running','waiting_parent','waiting_input')", (now,))
            db.execute("UPDATE agent_receipts SET state='uncertain' WHERE state='running'")
            db.execute('DELETE FROM agent_claims')
            db.execute("UPDATE agent_path_leases SET status='revoked' WHERE status='held' AND agent_id IN (SELECT id FROM agent_nodes WHERE status='interrupted')")
            db.execute("UPDATE agent_waits SET status='cancelled' WHERE status='waiting'")
            self._drop_questions(db)

    def forget(self, db, cid):
        """Caller owns the chat deletion transaction; no Store.query calls here."""
        for table in ('agent_claims', 'agent_path_leases', 'agent_usage', 'agent_dispatches', 'agent_receipts', 'agent_mail_reads',
                      'agent_messages', 'agent_waits', 'agent_parent_questions', 'agent_rounds', 'agent_nodes', 'agent_trees'):
            db.execute(f'DELETE FROM {table} WHERE run_id IN (SELECT id FROM runs WHERE chat_id=?)', (cid,))
