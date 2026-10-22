"""Phase 13 coding routes: reviewed Git plans, child worktrees, recipes, installs.

Parent-owned integration over GitPlans, ChildWorktrees, recipes and
Environments. Root tools only *prepare* immutable payloads and stage them in
the existing permission broker (`App.propose`). Effects run only from
`App.perform_action` after native approval or the user's automatic policy, and
each adapter re-validates its own bindings there. Native RPCs are reachable
only through the trusted main-window allowlist. Nothing here grants authority
to source text, repository data, children, mods or generated views.
"""

import json

from .git_workflows import STR, tool
from .policy import PolicyError, Workspace

SCHEMAS = [
    tool('ReadGitPlanReceipt', 'Read back one reviewed Git plan receipt from this conversation against the current repository: refs, commit, index and remote facts. Read-only evidence; it never retries or repairs.',
         {'receipt_id': STR}, ['receipt_id']),
    tool('ListProjectRecipes', 'List the test/build/lint recipes Pebble can run for this project, with availability and the reason each was detected. Read-only; use an id with RunTests.',
         {}, []),
    tool('PrepareEnvironment', 'Propose a lockfile-pinned dependency install into a managed environment outside the project (kind npm-ci or pip-hashes). Network goes only to the allowlisted registry; install scripts never run. Always needs the human\'s native review.',
         {'kind': {'type': 'string', 'enum': ['npm-ci', 'pip-hashes']}}, ['kind']),
]
ARGUMENTS = {item['function']['name']: frozenset(item['function']['parameters']['properties']) for item in SCHEMAS}
KIND_TOOL = {'worktree': 'AllocateChildWorktree', 'git': 'ProposeGitChange', 'install': 'PrepareEnvironment'}
KIND_GROUP = {'worktree': 'edits', 'git': 'edits', 'install': 'tests'}
# Removing a worktree stays native-only (Phase 9 contract): retirement and
# discard run only from the trusted agent_worktree_retire route.
WORKTREE_OPERATIONS = {'allocate_child_worktree'}
TREE_EFFECT_GIT = {'switch_branch', 'merge', 'discard', 'undo'}


def root_tool_names():
    from .git_plans import TOOLS as PLAN_TOOLS
    return frozenset(ARGUMENTS) | frozenset(t['function']['name'] for t in PLAN_TOOLS)


def schemas():
    from .git_plans import TOOLS as PLAN_TOOLS
    return list(PLAN_TOOLS) + SCHEMAS


class CodingAPI:
    def __init__(self, app):
        self.app = app

    # Root tools (inside App._execute, after permission, hook and Guard gates)

    def root_tool(self, cid, name, args, ws, hook_review):
        app = self.app
        rid = app.active_run if app.active == cid else None
        if name == 'ProposeGitChange':
            if ws is None:
                raise PolicyError('Select a project folder first.')
            payload = app.git_plans.prepare(cid, args.get('operation'), args, workspace=ws, cancel=app.cancel)
            payload = {**payload, 'requires_review': bool(payload.get('requires_review') or hook_review)}
            return app.propose(cid, 'git', payload)
        if name == 'ReadGitPlanReceipt':
            return app.git_plans.readback(cid, args['receipt_id'])
        if name == 'ListProjectRecipes':
            if ws is None:
                raise PolicyError('Select a project folder first.')
            from .recipes import inspect
            return inspect(ws)
        if name == 'PrepareEnvironment':
            if ws is None:
                raise PolicyError('Select a project folder first.')
            policy = app.task_policy or app.store.setting('permissions', None)
            if not policy['tools'].get('web') or not policy['tools'].get('files'):
                raise PolicyError('Dependency installs need project read and web access in Permissions.')
            payload = app.environments.plan(cid, ws, args['kind'])
            return app.propose(cid, 'install', {**payload, 'requires_review': True})
        raise PolicyError('Tool is unavailable.')

    # Broker execution (App.perform_action)

    def perform(self, action, cid, policy):
        app, kind, payload = self.app, action['kind'], action['payload']
        if not isinstance(payload, dict):
            raise PolicyError('This action payload is malformed.')
        if kind == 'worktree':
            if not policy['tools'].get('files'):
                raise PolicyError('Project read access was disabled after this proposal.')
            operation = payload.get('operation')
            if operation == 'allocate_child_worktree':
                return app.child_worktrees.allocate(payload, action_id=action['id'], cancel=app.cancel)
            raise PolicyError('Unknown worktree operation.')
        if kind == 'git':
            if payload.get('requires_review') is True and action.get('_native_reviewed') is not True:
                raise PolicyError('This Git plan needs native review before it runs.')
            root = payload.get('root')
            if not isinstance(root, str):
                raise PolicyError('This Git plan has no project root.')
            with app.store.lock:
                if app.process_inspections.pending(root):
                    raise PolicyError('Inspect the prior uncertain command outcome before changing this repository.')
                app.agents.assert_effect_paths(root)
            return app.git_plans.execute(cid, payload, action_id=action['id'],
                                         run_id=app.active_run if app.active == cid else None, cancel=app.cancel)
        if kind == 'install':
            if not policy['tools'].get('web') or not policy['tools'].get('files'):
                raise PolicyError('Network or project read access was disabled after this install was proposed.')
            if action.get('_native_reviewed') is not True:
                raise PolicyError('Dependency installs always need native review.')
            if {'ca_file', 'loopback_targets'} & set(payload):
                raise PolicyError('Fixture-only install fields are never accepted from a stored action.')
            result = app.environments.install(payload, app.cancel)
            state = result.get('state') if isinstance(result, dict) else None
            # The environment row keeps full evidence; the broker records a
            # definite failure as failed and anything unproven as uncertain.
            if state == 'failed':
                raise PolicyError('The dependency install failed and its environment was removed: '
                                  + str(result.get('reason') or 'see the environment record'))
            if state != 'ready':
                raise RuntimeError('The dependency install outcome is uncertain; inspect the environment record.')
            return result
        raise PolicyError('This action is disabled in Permissions.')

    # Native RPCs (`id` is the conversation)

    def methods(self):
        app = self.app
        return {
            'git_plan_propose': self.native_git_plan,
            'git_plan_receipts': lambda id, limit=50: app.git_plans.receipts(self._chat(id), limit),
            'git_plan_readback': lambda id, receipt_id: app.git_plans.readback(self._chat(id), receipt_id),
            'agent_worktrees': lambda id, run_id=None: app.child_worktrees.bindings(self._chat(id), run_id),
            'agent_worktree_retire': self.native_retire,
            'agent_worktree_reconcile': lambda id: self.app.child_worktrees.reconcile(self._chat(id)),
            'project_recipes': lambda id: self._recipes(id),
            'environment_list': lambda id: app.environments.list(self._chat(id)),
            'environment_plan': self.native_environment,
            'environment_remove': self.native_environment_remove,
        }

    def _chat(self, id):
        self.app.chat(id)
        return id

    def _workspace(self, id):
        chat = self.app.chat(id)
        if not chat['workspace']:
            raise PolicyError('Choose a project folder for this conversation first.')
        return Workspace(chat['workspace'])

    def _idle(self):
        """Native effect routes run only while no root or child work executes."""
        app = self.app
        if app.active or app.agents.busy():
            raise PolicyError('Wait for the current task to finish first.')

    def _recipes(self, id):
        from .recipes import inspect
        return inspect(self._workspace(id))

    def native_git_plan(self, id, operation, **args):
        app = self.app
        with app.task_lock:
            self._idle()
            payload = app.git_plans.prepare(id, operation, {**args, 'operation': operation},
                                            workspace=self._workspace(id), cancel=app.cancel)
            # A natively initiated plan still lands on its review card first.
            return app.propose(id, 'git', {**payload, 'requires_review': True})

    def native_retire(self, id, run_id, agent_id, discard=False):
        app = self.app
        if type(discard) is not bool:
            raise PolicyError('discard must be true or false.')
        with app.task_lock:
            self._idle()
            app.runs.get(run_id, id)
            return app.child_worktrees.retire(id, run_id, agent_id, discard_review=discard, cancel=app.cancel)

    def native_environment(self, id, kind):
        app = self.app
        with app.task_lock:
            self._idle()
            payload = app.environments.plan(id, self._workspace(id), kind)
            return app.propose(id, 'install', {**payload, 'requires_review': True})

    def native_environment_remove(self, id, environment_id):
        app = self.app
        with app.task_lock:
            self._idle()
            return app.environments.remove(self._chat(id), environment_id)

    # Conversation lifecycle

    def assert_chat_deletable(self, id):
        self.app.child_worktrees.assert_chat_deletable(id)
        self.app.environments.assert_chat_deletable(id)
        for row in self.app.store.query("SELECT id FROM git_plan_receipts WHERE chat_id=? AND state IN ('executing','uncertain')", (id,)):
            # Read-only inspection: deletion proceeds only when current
            # repository facts match either the recorded before or after state.
            check = self.app.git_plans.readback(id, row['id'])
            if not (check.get('matches_receipt') or check.get('matches_before')):
                raise PolicyError('A reviewed Git plan in this conversation has an uncertain outcome that readback cannot settle. Inspect the repository first.')

    def forget_chat_records(self, id):
        self.app.child_worktrees.forget_chat_records(id)
        self.app.environments.forget_chat_records(id)
        self.app.store.query('DELETE FROM git_plan_receipts WHERE chat_id=?', (id,))

    @staticmethod
    def describe(kind, payload, result):
        """Plain summary text for the action history message."""
        if kind == 'git':
            facts = result.get('result') if isinstance(result, dict) and isinstance(result.get('result'), dict) else {}
            return ('Git ' + str(payload.get('operation')) + ' ' + str(result.get('state') if isinstance(result, dict) else '')
                    + ': ' + str(payload.get('summary', ''))[:300] + ' ' + json.dumps(
                        {k: facts.get(k) for k in ('commit', 'ref', 'branch_ref', 'remote_ref', 'new_oid', 'to') if k in facts})[:300])
        if kind == 'worktree':
            return 'Managed child worktree ' + str(payload.get('operation')) + ' completed.'
        state = result.get('state') if isinstance(result, dict) else None
        label = 'Managed dependency environment ' + str(payload.get('environment_id', ''))
        if state == 'ready':
            return label + ' installed and verified (tree ' + str(result.get('tree_digest', ''))[:12] + ').'
        return label + ' was not installed: ' + str(result.get('reason') if isinstance(result, dict) else 'unknown') + '.'
