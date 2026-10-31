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
import re

from .git_workflows import STR, tool
from .policy import PolicyError, Workspace
from .text_safety import plain_quotes

# Phase 46 (1.9.28, the lead's follow-up to w9-git): a person who said not to push ("don't push", "do not push yet",
# "keep it local", "without pushing", "No push.") gets no push plan from the model in that conversation, decided on the
# host. The newest message that speaks about pushing decides, so a later "now push it" lifts it, and a message that
# also asks for a push ("don't push main; push the tag") is left to the review card as before.
NO_PUSH = re.compile(
    r"\b(?:don'?t|do\s+not|never|not|shouldn'?t|must\s+not|won'?t|no\s+need\s+to)\s+(?:\w+\s+){0,2}?"
    r"(?:push|publish)(?:ed|ing)?\b"
    r"|\bno\s+(?:(?:branch(?:es)?|tags?)\s+)?push(?:ing|es)?\b|\bwithout\s+pushing\b"
    r"|\bnothing\s+(?:\w+\s+){0,2}?pushed\b"
    r"|\bkeep\s+(?:it|this|them|that|everything|all\s+of\s+(?:it|this)|the\s+\w+(?:\s+\w+)?)\s+local(?:ly)?\b"
    r"|\b(?:stays?|remains?)\s+local(?:ly)?\b", re.I)
PUSH_WORD = re.compile(r'\bpush(?:es|ed|ing)?\b', re.I)
# Phase 46 (1.9.28, the follow-ups security review 4: "Don't push yet; I'll publish the release notes myself" lifted the
# "don't push"): "publish" asks for a push only when what it publishes is Git's (a tag, a branch, commits, a ref, a
# version tag, or "to origin" / "to the remote"); "the API docs" or "to PyPI" never lifts a "don't push", in the same
# message or an older one. Nor does a push or publish the person says they will do themselves ("I'll push it").
GIT_PUBLISH = re.compile(r"\bpublish(?:es|ed|ing)?\b(?=(?:\s+[\w'`.-]+){0,3}?\s+(?:tags?|branch(?:es)?|commits?|refs?"
                         r"|v\d[\w.-]*|to\s+(?:the\s+)?(?:origin|upstream|remote))\b)", re.I)
SELF = re.compile(r"\b(?:i(?:['’]ll|\s+will|['’]m\s+going\s+to|\s+am\s+going\s+to)|we(?:['’]ll|\s+will))\s+"
                  r"(?:(?!(?:let|want|need|ask|have|get|you)\b)\w+\s+)?$", re.I)  # "I'll then push", not "I'll let you"
NEGATED = re.compile(r"\b(?:don'?t|do\s+not|never|not|no|without|nothing|won'?t|shouldn'?t|must\s+not)\s+"
                     r"(?:\w+\s+){0,3}?$", re.I)
# Phase 46 (1.9.28, the lead's dev check: private d-gw-92 and d-gw-109 k1 ended with the tag never published): a
# prohibition covers only what it names. "Do not push any branch" forbids branch pushes, not the tag push the same
# request asks for; "no tag push" or "don't push the tags" forbids tag pushes; "don't push main", "`main`" or "the
# main branch" that ref (a word that names a ref: a usual branch name, or one with a digit or a slash; "the feature
# branch" is every branch); a bare "don't push" (or one whose object is anything else: "it", "anything", "yet") every
# push.
SCOPE = re.compile(r"\s+(?:(?:any|the|a|an|my|your|our|these|those|other|either|neither|both)\s+)*"
                   r"(?:(?P<kind>branch(?:es)?|tags?)\b"
                   r"(?P<both>,?\s+(?:or|and|nor)\s+(?:(?:any|the)\s+)?(?:branch(?:es)?|tags?)\b)?"
                   r"|[`'\"](?P<quoted>[\w./-]{1,100})[`'\"]|(?P<named>[\w./-]{1,100})(?:\s+(?P<what>branch|tag)\b)?)",
                   re.I)
REF_WORD = re.compile(r'main|master|develop|development|dev|trunk|staging|production|prod|gh-pages|[\w.-]*[/\d][\w./-]*',
                      re.I)
# Phase 46 (1.9.28, the follow-ups security review 9: "don't push main or develop" covered only main): the names listed
# after the first one ("main or develop", "main, develop or release/2.0", "`main` and `develop`") are named too. After
# "or" or "nor" any word is a listed name; after "and" or a comma only a ref word is, as anything else starts a new
# clause ("don't push main, and keep notes.txt"); "or anything (else)" means every push.
ITEM = re.compile(r"(?P<joint>\s*,\s*(?:(?:or|and|nor)\s+)?|\s+(?:or|and|nor)\s+)(?:(?:the|any|a|an|my|your|our)\s+)*"
                  r"(?:[`'\"](?P<quoted>[\w./-]{1,100})[`'\"]"
                  r"|(?P<named>[\w./-]{1,100})(?:\s+(?P<what>branch(?:es)?|tags?)\b)?)", re.I)
WHOLE = frozenset({'it', 'them', 'anything', 'everything', 'all', 'other', 'others'})


NO_PUSH_REFUSAL = ('The person said not to push ("{said}"), so Pebble prepares no push in this conversation and nothing '
                   'was pushed. Finish the local work and say in your reply that pushing is left to them; if they ask for '
                   'the push in a later message, it can run then.')
NO_PUSH_SCOPED = ('The person said not to push ("{said}"), which covers this push ({what}), so Pebble prepared no plan '
                  'and nothing was pushed. A push they did not rule out (and asked for) can still run; leave this one to '
                  'them and say so in your reply.')


def _named(said, listed=False):
    """The scope one named item says: 'ref:<name>' for a ref word, a quoted name or a word listed after "or"; else
    'branches' or 'tags' for a description ("the feature branch"), or 'all'."""
    named = said.group('quoted') or said.group('named').rstrip('.')
    what = (said.group('what') or '').lower()
    if said.group('quoted') or REF_WORD.fullmatch(named) or listed and not what:
        return f'ref:{named}'
    return 'branches' if what.startswith('branch') else 'tags' if what.startswith('tag') else 'all'


def _scopes(text, found):
    """What one prohibition names: a set of 'all', 'branches', 'tags' and 'ref:<name>'."""
    words = found.group(0)
    if re.match(r'no\s+(?:branch|tag)', words, re.I):
        return {'branches' if 'branch' in words.lower() else 'tags'}
    kept = re.fullmatch(r'keep\s+the\s+(?:([\w./-]+)\s+)?(branch|tag)(?:es|s)?\s+local(?:ly)?', words, re.I)
    if kept:
        named = kept.group(1)
        return {f'ref:{named}' if named and REF_WORD.fullmatch(named)
                else 'branches' if kept.group(2).lower() == 'branch' else 'tags'}
    said = SCOPE.match(text, found.end()) if re.search(r'(?:push|publish)(?:ing)?$', words, re.I) else None
    if not said:
        return {'all'}
    if said.group('kind'):
        return {'all' if said.group('both') else
                'branches' if said.group('kind').lower().startswith('branch') else 'tags'}
    scopes = {_named(said)}
    while not said.group(0).endswith('.') and (item := ITEM.match(text, said.end())):  # Phase 46 (1.9.28, review 9)
        name = (item.group('quoted') or item.group('named').rstrip('.')).lower()
        listed = bool({'or', 'nor'} & set(item.group('joint').replace(',', ' ').lower().split()))
        if name in WHOLE:
            scopes.add('all')
        elif name in ('branch', 'branches', 'tag', 'tags'):
            scopes.add('branches' if name.startswith('branch') else 'tags')
        elif item.group('quoted') or item.group('what') or REF_WORD.fullmatch(name) or listed:
            scopes.add(_named(item, listed))
        else:
            break  # "and" or a comma before a word that names no ref starts a new clause
        said = item
    return scopes


def _asks(text):
    """Whether a message asks for a push: an un-negated push word, or a publish word whose object is Git's, that the
    person does not say they will do themselves."""
    for match in [*PUSH_WORD.finditer(text), *GIT_PUBLISH.finditer(text)]:
        before = text[max(0, match.start() - 60):match.start()]
        if not NEGATED.search(before) and not SELF.search(before):
            return True
    return False


def push_forbidden(messages):
    """The person's newest word on pushing when it forbids a push: (their phrase, the set of scopes it names), or None
    (no message forbids one, or the newest one about pushing asks for a push and forbids nothing narrower)."""
    # Phase 46 (1.9.28, the lead): "Don\u2019t push" as typed on a Mac (text_safety.plain_quotes)
    for text in reversed([plain_quotes(m) for m in messages if isinstance(m, str)]):
        found = list(NO_PUSH.finditer(text))
        asked = _asks(text)
        if found or asked:
            scopes = frozenset(scope for m in found for scope in _scopes(text, m))
            if not found or asked and 'all' in scopes:
                return None  # a request that also asks for a push is left to the review card, as before
            return found[0].group(0), scopes
    return None


def push_covered(scopes, payload):
    """Whether a publication plan pushes what the prohibition names (git_plans: the push operation moves or deletes one
    branch; a push command publishes named tags only)."""
    if 'all' in scopes:
        return 'a push'
    if payload.get('operation') == 'push':
        names = {payload.get('branch'), payload.get('remote_branch'), payload.get('remote')} - {None}
        named = sorted(name for name in names if f'ref:{name}' in scopes)
        return ('a branch push' if 'branches' in scopes else f'a push of {named[0]}' if named else None)
    argv = payload.get('argv') if isinstance(payload.get('argv'), list) else []
    names = {part for word in argv[1:] if isinstance(word, str) for part in word.split(':')}
    named = sorted(name for name in names if f'ref:{name}' in scopes)
    return 'a tag push' if 'tags' in scopes else f'a push of {named[0]}' if named else None


def pushes(args):
    """Whether ProposeGitChange arguments ask for a push (the typed push operation or a push command)."""
    if not isinstance(args, dict):
        return False
    if args.get('operation') == 'push':
        return True
    argv = args.get('argv')
    try:
        argv = json.loads(argv) if isinstance(argv, str) else argv
    except ValueError:
        return False
    first = argv[0] if isinstance(argv, list) and argv else None
    first = first[0] if isinstance(first, list) and first else first
    return args.get('operation') == 'command' and isinstance(argv, list) and (
        first == 'push' or (first == 'git' and len(argv) > 1 and argv[1] == 'push'))

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
            forbidden = self._no_push(cid) if pushes(args) else None
            if forbidden and 'all' in forbidden[1]:
                raise PolicyError(NO_PUSH_REFUSAL.format(said=forbidden[0]))
            payload = app.git_plans.prepare(cid, args.get('operation'), args, workspace=ws, cancel=app.cancel)
            if payload.get('publication') and payload.get('operation') != 'undo':
                forbidden = self._no_push(cid)  # any plan that would change a remote, against what the person named
                what = push_covered(forbidden[1], payload) if forbidden else None
                if what:
                    raise PolicyError((NO_PUSH_REFUSAL if 'all' in forbidden[1] else NO_PUSH_SCOPED)
                                      .format(said=forbidden[0], what=what))
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
            if payload.get('requires_review') is True and not app._reviewed(action):  # D141: or Pebble's check
                raise PolicyError('This Git plan needs native review before it runs.')
            root = payload.get('root')
            if not isinstance(root, str):
                raise PolicyError('This Git plan has no project root.')
            with app.store.lock:
                # Phase 46 (1.9.29): a plan in one of the project's linked worktrees changes the project's repository too
                for where in dict.fromkeys([root, payload.get('project') or root]):
                    if app.process_inspections.pending(where):
                        raise PolicyError('Inspect the prior uncertain command outcome before changing this repository.')
                    app.agents.assert_effect_paths(where)
            return app.git_plans.execute(cid, payload, action_id=action['id'],
                                         run_id=app.active_run if app.active == cid else None, cancel=app.cancel)
        if kind == 'install':
            if not policy['tools'].get('web') or not policy['tools'].get('files'):
                raise PolicyError('Network or project read access was disabled after this install was proposed.')
            if not app._reviewed(action):  # D141: or, in Full access, Pebble's check of what the person asked
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

    def _no_push(self, cid):
        """Phase 46 (1.9.28): the person's own words in this conversation that forbid pushing, or None."""
        rows = self.app.store.query("SELECT content FROM messages WHERE chat_id=? AND role='user' ORDER BY created", (cid,))
        return push_forbidden([row['content'] for row in rows])

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
