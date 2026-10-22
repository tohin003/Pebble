import hashlib
import json
import os
import re
import shutil
import sys
import threading
import weakref
from collections import OrderedDict
from pathlib import Path

VENDOR = str(Path(__file__).resolve().parents[1] / "vendor")
# AgentGuard records only commands it recognises as test runs, and parses this one's `=== N failed ===` summary.
RECIPE_COMMAND = 'python -m pytest  # pebble recipe '
# Phase 24 (V1-027): a test command run through the terminal reaches AgentGuard only as a host summary under this
# pseudo-command (exit code plus the counts Pebble's own parser read), never as its output; the tested code prints that.
TERMINAL_COMMAND = 'python -m pytest  # pebble terminal '
# A terminal test run whose output was not read as a pass or a failure: recorded, but neither evidence nor a test run.
UNREAD_COMMAND = 'pebble terminal: test output not read as evidence'
# Merge round 2 (E2): the runner each registry toolchain stands for, so a recipe outcome is named as what ran.
RUNNERS = {'python': 'unittest', 'python-pytest': 'pytest', 'node': 'node', 'npm': 'npm', 'go': 'go', 'cargo': 'cargo',
           'make': 'make'}
FAILED_RECIPE = re.compile('The test run reported failures — `' + re.escape(RECIPE_COMMAND) + r'([a-z0-9-]+)`'
                           r'(?:: ([^\n]*?))?\. The task is not complete while its own tests are failing\.')
NAMED_RECIPE = re.compile(re.escape(RECIPE_COMMAND) + r'([a-z0-9-]+)')
NAMED_TERMINAL = re.compile(re.escape(TERMINAL_COMMAND) + r'([a-z0-9-]+)')
# Phase 24 (V1-027, V1-094): the runner family a terminal test command and a test recipe stand for. A passing recipe of
# the same family on the current files supersedes a failing terminal run of that family.
TERMINAL_PARSERS = {'unittest': 'python-unittest', 'pytest': 'python-pytest', 'npm': 'npm-test', 'jest': 'npm-test',
                    'vitest': 'npm-test', 'mocha': 'npm-test', 'go': 'go-test', 'cargo': 'cargo-test'}
RECIPE_FAMILIES = {'python-unittest': 'unittest', 'python-pytest': 'pytest', 'npm-test': 'npm', 'go-test': 'go',
                   'cargo-test': 'cargo', 'node-test': 'node', 'make-test': 'make'}
HOST_SUMMARY = re.compile(r'(?:\d+ (?:passed|failed)|no tests ran|test command exited with status -?\d+)')
# Phase 24 (V1-094): Pebble's project tools that change nothing in the project. They keep their names in AgentGuard's
# record and never enter its validation (scope, evidence), so a search or a test run is never challenged. Test recipes
# run on an isolated snapshot.
READ_ONLY_TOOLS = frozenset({'Read', 'Glob', 'Grep', 'SearchFiles', 'CodeReferences', 'ReadGitStatus', 'ReadGitDiff',
                             'ReadGitHistory', 'ReadGitWorktrees', 'PreviewWorktreeExport', 'ReadWorktreeExport',
                             'ListProjectRecipes', 'RunTests', 'RunRecipe'})
SOURCE_SUFFIXES = {'.py', '.ts', '.tsx', '.js', '.jsx', '.go', '.rs', '.java'}
# Phase 24 (V1-005, V1-031): one mirror per project root, the four most recently used kept.
MAX_MIRRORS = 4
MIRROR_BYTES = 12_000_000
# The first mutating check and the stop gate wait this long for the index, outside every adapter lock. A run that
# changed source files and still has no index is "verification unavailable", never an allow.
INDEX_WAIT = 15.0
BUILD_LIMIT = 300.0
REINDEX_FULL = 64  # more changed files than this refresh the whole index instead of one file at a time
ENDED_TASKS = 256  # task ids remembered after their session ended (ids only; no state)
# Phase 24 (V1-020): embedded retention for agentguard.db, shorter than AgentGuard's standalone defaults (14/30/60/270
# days). Prompts live in tasks, which go with their session summaries.
RETENTION = {'raw_events_days': 7, 'decisions_days': 14, 'verifications_days': 30, 'session_summaries_days': 30,
             'metrics_days': 14, 'maintenance_interval_hours': 1.0}
# Phase 24 (V1-093): Pebble decides the test waiver: an imperative not to run the tests, not any "no tests" substring
# ("a folder has no tests in it", "add tests for the helpers without tests").
WAIVER = re.compile(
    r"(?:^|[.;:!?\n(]|\b(?:and|but|then|so)\b)\s*(?:please\s+)?(?:(?:do\s+not|don['’]?t|dont|never)\s+(?:run|execute)\s+"
    r"(?:the\s+|any\s+)?(?:unit\s+)?tests?\b|skip\s+(?:running\s+)?(?:the\s+|any\s+)?(?:unit\s+)?tests?\b|"
    r"no\s+need\s+to\s+run\s+(?:the\s+|any\s+)?tests?\b)|\bwithout\s+running\s+(?:the\s+|any\s+)?(?:unit\s+)?tests?\b",
    re.I)
# Phase 24 (V1-053): typed process input is screened statelessly; a retry of the same text is refused again. Pebble's
# own REPL patterns beside AgentGuard's command patterns. Narrowing only: the reviewed interactive plan and the sandbox
# remain the boundary.
REPL_RISKS = (
    (re.compile(r'\bshutil\s*\.\s*rmtree\s*\('), 'recursive delete'),
    (re.compile(r'\b(?:os\s*\.\s*(?:system|popen)|subprocess\s*\.\s*\w+)\s*\([^\n]*\brm\s+-[a-zA-Z]*[rR]'),
     'recursive delete through a shell'),
    (re.compile(r'\bos\s*\.\s*(?:removedirs|rmdir)\s*\(\s*["\'](?:/|~)'), 'deleting a root or home folder'),
)
MISSING = object()


def recipe_label(recipe):
    """'npm-lint recipe (lint, run by npm)': the procedure and runner one recipe outcome came from."""
    from . import recipes
    try:
        toolchain = recipes.REGISTRY[recipe].toolchain
        return f'{recipe} recipe ({recipes.procedure(recipe)}, run by {RUNNERS.get(toolchain, toolchain)})'
    except (KeyError, ValueError):
        return 'project recipe'


def named(value):
    """AgentGuard's verdict names the host's recipe pseudo-command as a test run; say what actually failed. The
    sentence is shorter than AgentGuard's, so the completion check it reaches at 4K does not grow."""
    if isinstance(value, str):
        value = FAILED_RECIPE.sub(lambda m: f'The {recipe_label(m[1])} reported failures' + (f': {m[2]}' if m[2] else '')
                                  + '. The task is not complete while it is failing.', value)
        value = NAMED_TERMINAL.sub(lambda m: f'terminal {m[1]} run', value)
        return NAMED_RECIPE.sub(lambda m: recipe_label(m[1]), value)
    if isinstance(value, dict):
        return {key: named(item) for key, item in value.items()}
    if isinstance(value, list):
        return [named(item) for item in value]
    return value


def waived(prompt):
    """Whether the person's request tells Pebble not to run the tests (an anchored imperative)."""
    return bool(WAIVER.search(prompt or ''))


def _digest(root, name):
    """sha256 of one project file as it is on disk now, None when missing or unreadable."""
    from .policy import Workspace
    try:
        data = Workspace(root).snapshot(name, missing_ok=True)
    except (ValueError, OSError):
        return None
    return hashlib.sha256(data).hexdigest() if data is not None else None


class _Mirror:
    """One project root's broker-filtered copy, shared by every session on it, with one session's drafts written in."""

    def __init__(self, root, path):
        self.root, self.path = root, path
        self.lock = threading.RLock()  # the mirror's files; taken after GuardAdapter.lock, never before it
        self.signatures = {}           # project-relative name -> (mtime_ns, size, inode) when last copied
        self.copied = False
        self.truncated = False
        self.owner = None              # the (root, session) whose drafts are written in the mirror now
        self.ready = threading.Event() # the index is built and every queued file is refreshed
        self.cond = threading.Lock()
        self.pending = set()
        self.worker = None
        self.indexed = False
        self.broken = False            # the index could not be built
        self.slow = False              # a mutating check already waited the full bound in this generation
        self.removed = False
        self.names = None              # V1-031: the project's names at the last sync (the runner's listing)
        self.names_truncated = False


class GuardAdapter:
    # Phase 21 (F21-fd P2): the adapters in this process not yet closed (see close()).
    _open = weakref.WeakSet()
    _open_lock = threading.Lock()

    def __init__(self, home):
        self.lock = threading.RLock()
        # Phase 24 (V1-172, in part): AgentGuard's database location stays process-global, as before. Two Apps in one
        # process still share the newest App's database (test_merge_21 pins that binding); the adapter now records the
        # databases its own workspaces use, so forget() and retention reach the right one.
        os.environ["AGENTGUARD_HOME"] = str(home / "guard")
        if VENDOR not in sys.path:
            sys.path.insert(0, VENDOR)  # once per process, not once per App
        self.error = None
        self.home = home
        self.closed = False
        with GuardAdapter._open_lock:
            others = [a for a in GuardAdapter._open if a is not self and not a.closed]
            GuardAdapter._open.add(self)
        self.roots = {}
        # Host run -> vendored task id. The engine keeps changed files, test
        # outcomes and gate state only in memory, keyed by that task.
        self.tasks = OrderedDict()
        # Phase 21 F21-gate, Phase 24: host run -> (task id, [run records]), oldest first, for the host test results
        # recorded in that task (recipes and terminal test commands). The engine's own outcome list is never edited.
        self.recipe_runs = {}
        self.mirrors = OrderedDict()  # project root -> _Mirror, least recently used first
        self.overlays = {}            # (root, session) -> {name: staged draft text}
        self.bases = {}               # (root, session) -> {name: sha256 of the project file when first staged}
        self.prompts = {}             # (root, session) -> the request, to ground the task once the index exists
        self.ungrounded = set()
        self.databases = set()        # paths of the AgentGuard databases this adapter's workspaces wrote to
        try:
            from agentguard.core.engine import Guard
            from agentguard.core.config import Settings
            from agentguard.core.events import AgentEvent

            self.event_type = AgentEvent
            settings = Settings()
            settings.index.scoped_only = True
            settings.index.max_files = 1500
            settings.index.max_file_bytes = 96000
            settings.index.exclude_dirs += [".runtime", "models"]
            # V1-031: never wait for the index at SessionStart (on the path to the first model request); this adapter
            # waits at the first mutating check and at the stop gate instead.
            settings.index.session_start_wait = 0.0
            for name, value in RETENTION.items():
                setattr(settings.retention, name, value)
            # The runner bounds completion repairs, then settles the run as
            # verification_required. The vendored per-task cap would instead
            # mute the gate, turning still-missing evidence into an allow.
            settings.challenge.max_stop_blocks_per_task = 1_000_000
            self.guard = Guard(settings)
        except Exception as exc:
            self.guard = None
            self.error = f"{type(exc).__name__}: {exc}"
        # V1-005: mirrors a crashed service left behind hold copies of project files. The startup lease makes this
        # service their only owner; another open adapter on the same home (a test) keeps its own.
        if not any(Path(a.home) == Path(home) for a in others):
            shutil.rmtree(Path(home) / 'guard-projects', ignore_errors=True)

    # -- mirrors ----------------------------------------------------------------------------------------------------

    def _mirror_path(self, root):
        return self.home / "guard-projects" / hashlib.sha256(str(root).encode()).hexdigest()[:24]

    def _attach(self, key, fresh=False):
        """The root's mirror, created or synced from the project as needed. Copies under the mirror's own lock only."""
        evicted = []
        with self.lock:
            mirror = self.mirrors.get(key[0])
            created = mirror is None or mirror.removed
            if created:
                mirror = self.mirrors[key[0]] = _Mirror(key[0], self._mirror_path(key[0]))
                while len(self.mirrors) > MAX_MIRRORS:
                    _, old = self.mirrors.popitem(last=False)
                    evicted.append(self._forget_root(old))
            self.mirrors.move_to_end(key[0])
            self.roots[key] = mirror.path
            skip = set(self.overlays.get(mirror.owner, ())) if mirror.owner else set()
        for old in evicted:
            self._remove(old)
        changed = set()
        with mirror.lock:
            if created or fresh or not mirror.copied:
                changed = self._sync(mirror, skip)
        if created:
            self._reindex(mirror, set())
        elif changed:
            self._reindex(mirror, changed)
        return mirror, changed

    def _sync(self, mirror, skip):
        """Copy what changed in the project since the last sync into the mirror; the changed names. Caller owns
        mirror.lock. A staged draft (skip) stays in the mirror; its project file is still compared."""
        from .policy import Workspace
        try:
            ws = Workspace(mirror.root)
            inventory = ws.inventory()
        except (ValueError, OSError):
            mirror.names = None
            return set()
        mirror.names, mirror.names_truncated = list(inventory['files']), bool(inventory['truncated'])
        if not mirror.copied:
            shutil.rmtree(mirror.path, ignore_errors=True)
        mirror.path.mkdir(parents=True, exist_ok=True)
        seen, changed, total, truncated = {}, set(), 0, inventory['truncated']
        for name in inventory['files']:
            try:
                info = os.lstat(ws.root / name)
            except OSError:
                continue
            if total + info.st_size > MIRROR_BYTES:
                truncated = True
                break
            total += info.st_size
            signature = (info.st_mtime_ns, info.st_size, info.st_ino)
            if mirror.signatures.get(name) == signature:
                seen[name] = signature
                continue
            if name not in skip:
                try:
                    content = ws.read(name)
                except (ValueError, OSError, UnicodeError):
                    (mirror.path / name).unlink(missing_ok=True)
                    continue
                target = mirror.path / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            seen[name] = signature
            changed.add(name)
        for name in set(mirror.signatures) - set(seen):
            changed.add(name)
            if name not in skip:
                (mirror.path / name).unlink(missing_ok=True)
        mirror.signatures, mirror.truncated, mirror.copied = seen, truncated, True
        return changed

    def _restore(self, mirror, name):
        """Put one project file (as it is on disk) back into the mirror, in place of a draft. Caller owns mirror.lock."""
        from .policy import Workspace
        target = mirror.path / name
        try:
            content = Workspace(mirror.root).read(name)
        except (ValueError, OSError, UnicodeError):
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def _switch(self, mirror, key):
        """Write key's drafts into the mirror in place of another session's. Caller owns self.lock and mirror.lock."""
        names = set()
        if mirror.owner is not None and mirror.owner != key:
            for name in self.overlays.get(mirror.owner, {}):
                self._restore(mirror, name)
                names.add(name)
        for name, content in self.overlays.get(key, {}).items():
            target = mirror.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            names.add(name)
        mirror.owner = key
        if names:
            self._reindex(mirror, names)

    def _reindex(self, mirror, names):
        """Build the root's index (first time) or refresh the named files, in the background."""
        if not self.guard or mirror.removed:
            return
        with mirror.cond:
            mirror.pending |= set(names)
            mirror.ready.clear()
            mirror.slow = False
            if mirror.worker is not None:
                return
            mirror.worker = threading.Thread(target=self._index_worker, args=(mirror,), name='pebble-guard-index',
                                             daemon=True)
            worker = mirror.worker
        worker.start()

    def _index_worker(self, mirror):
        try:
            while True:
                with mirror.cond:
                    names, mirror.pending = mirror.pending, set()
                    if not names and mirror.indexed or mirror.removed or self.closed:
                        mirror.worker = None
                        mirror.ready.set()
                        return
                ws = self.guard.workspace(mirror.path)
                self._remember_database(ws)
                if not ws.index.ready(timeout=BUILD_LIMIT):
                    mirror.broken = not ws.index.is_built
                mirror.indexed = True
                if ws.index.is_built:
                    if len(names) > REINDEX_FULL:
                        ws.index.refresh()
                    else:
                        for name in names:
                            ws.index.refresh_path(name)
        except Exception:
            mirror.broken = True
            with mirror.cond:
                mirror.worker = None
                mirror.ready.set()

    def _remember_database(self, ws):
        try:
            self.databases.add(str(ws.store.db.path))
        except AttributeError:
            pass

    def _forget_root(self, mirror):
        """Drop every session on an evicted root (LRU of four). Caller owns self.lock; files go in _remove."""
        mirror.removed = True
        for key in [k for k in self.roots if k[0] == mirror.root]:
            self._drop_key(key)
        return mirror

    def _remove(self, mirror):
        mirror.removed = True
        if self.guard:
            try:
                self.guard.evict(mirror.path)
            except Exception:
                pass
        with mirror.lock:
            shutil.rmtree(mirror.path, ignore_errors=True)

    def _drop_key(self, key, keep_task=True):
        """Release one session's adapter state. Caller owns self.lock. Its drafts leave the mirror."""
        self.recipe_runs.pop(key, None)
        self.prompts.pop(key, None)
        self.bases.pop(key, None)
        self.ungrounded.discard(key)
        self.roots.pop(key, None)
        overlay = self.overlays.pop(key, {})
        task = self.tasks.pop(key, None)
        if keep_task and task is not None:
            self.tasks[key] = task  # the id only, so a caller can still tell which task the session had
            while len(self.tasks) > ENDED_TASKS:
                self.tasks.popitem(last=False)
        mirror = self.mirrors.get(key[0])
        if mirror is not None and mirror.owner == key:
            with mirror.lock:
                for name in overlay:
                    self._restore(mirror, name)
                mirror.owner = None
            if overlay:
                self._reindex(mirror, set(overlay))

    @staticmethod
    def _wait(mirror, event, kwargs, changed):
        """Wait for the root's index where a verdict depends on it, outside every adapter lock (V1-031): the stop gate,
        a mutating check, and a command whose changes are being re-indexed. Reads and host results never wait."""
        tool = kwargs.get('tool')
        if event == 'stop':
            timeout = INDEX_WAIT
        elif event == 'pre_tool_use' and tool not in READ_ONLY_TOOLS and not kwargs.get('read_only') or changed:
            timeout = 0.0 if mirror.slow else INDEX_WAIT  # one full wait per index generation, not one per tool
        else:
            return mirror.ready.is_set()
        if not mirror.ready.wait(timeout) and event != 'stop':
            mirror.slow = True
        return mirror.ready.is_set()

    # -- events -----------------------------------------------------------------------------------------------------

    def handle(self, event, root, cid, **kwargs):
        if not self.guard:
            return {
                "action": "block",
                "reason": "AgentGuard is unavailable. Protected project actions are disabled.",
            }
        host = kwargs.pop('host_summary', False)
        writable = kwargs.pop('writable', None)
        key = (str(Path(root)), cid)
        # Index only broker-approved source copies. AgentGuard's repository-root discovery
        # must never expand a user's selected subfolder or follow a project symlink.
        mirror, _ = self._attach(key, fresh=event == 'session_start')
        changed = set()
        if event == 'post_tool_use' and kwargs.get('tool') == 'Bash' and not host and writable is not False:
            # V1-093: a change an approved command made reaches AgentGuard like an edit (the mirror only changes
            # through stage otherwise). Every project file changed since the last sync counts for this session.
            with self.lock:
                skip = set(self.overlays.get(mirror.owner, ())) if mirror.owner else set()
            with mirror.lock:
                changed = self._sync(mirror, skip)
            if changed:
                self._reindex(mirror, changed)
        idle = False
        if event == 'stop':
            with self.lock:
                _, state = self._live(key)
                idle = state is None or not state.changed_files  # nothing to verify: no wait for the index
        if not idle:
            self._wait(mirror, event, kwargs, changed)
        with self.lock:
            return self._handle(event, Path(root), cid, mirror, host=host, changed=changed, **kwargs)

    def _handle(self, event, root, cid, mirror, host=False, changed=(), **kwargs):
        key = (str(root), cid)
        if mirror.owner != key and (self.overlays.get(key) or mirror.owner in self.overlays):
            with mirror.lock:
                self._switch(mirror, key)
        kwargs = self._normalize(event, kwargs)
        run = None
        if event == 'post_tool_use' and kwargs.get('tool') == 'Bash':
            kwargs, run = self._terminal(key, root, kwargs, host)
        if changed:
            self._touch(key, changed)
        state = outcomes = unavailable = None
        if event == 'stop':
            state, outcomes, unavailable = self._before_stop(key, root, mirror)
        elif event == 'pre_tool_use' and key in self.ungrounded and not kwargs.get('raw', {}).get('host_read_only'):
            _, live = self._live(key)
            if live is not None:
                self._ground(key, self.guard.workspace(mirror.path), live)
        if state is not None:
            # The gate reads only the outcomes it judges, and only their commands as "seen": AgentGuard's
            # "a test command ran but its output was unreadable, so pass" never applies to Pebble, where every test
            # result is a host summary and an unread one is no evidence (handoff §6.6, V1-093).
            reading = outcomes if outcomes is not None else list(state.verification.outcomes)
            history = (state.verification.outcomes, state.verification.commands_seen)
            state.verification.outcomes = reading
            state.verification.commands_seen = [o.command for o in reading]
        seen = self._outcome_count(key)
        try:
            result = self.guard.handle(
                self.event_type(
                    event=event,
                    agent="pebble",
                    workspace=str(mirror.path),
                    session_id=cid,
                    **kwargs,
                )
            )
        finally:
            if state is not None:
                state.verification.outcomes, state.verification.commands_seen = history
        if run is not None:
            self._record_run(key, seen, run)
        if event == 'user_prompt':
            self._remember_database(self.guard.workspace(mirror.path))
        decision = result.model_dump(mode="json")
        if event == 'pre_tool_use' and mirror.truncated and decision.get('action') == 'challenge':
            decision = self._absence_on_a_cut_mirror(decision)
        if event != 'stop':
            return decision
        gate = self._gate(key, root, mirror, state, outcomes, result, unavailable)
        if gate['kind'] == 'unavailable' and decision.get('action') == 'allow':
            decision.update(action='block', level=decision.get('level', 0),
                            reason='AgentGuard — completion gate: verification_unavailable\n\nThe project index was not '
                                   'ready, so the changed files could not be checked.')
        decision['gate'] = gate
        return named(decision)  # E2: a recipe outcome names its procedure

    @staticmethod
    def _normalize(event, kwargs):
        """Pebble's tool names and arguments as AgentGuard reads them (V1-094)."""
        tool = kwargs.get('tool')
        if not tool:
            return kwargs
        kwargs = dict(kwargs)
        raw = dict(kwargs.get('raw') or {})
        args = dict(kwargs.get('arguments') or {})
        read_only = kwargs.pop('read_only', None) is True
        if tool in READ_ONLY_TOOLS or tool == 'Bash' and read_only:
            raw['host_read_only'] = True
            if event != 'pre_tool_use' and tool != 'Bash':
                # The vendored core refreshes/touches any event carrying file_path.
                # Read-only tools must not be recorded as file mutations by this host.
                args.pop('file_path', None)
        if tool in {'SearchFiles', 'CodeReferences'}:
            # These host capabilities have Grep's read-only effects. Normalize at
            # the adapter boundary while retaining their actual host identity.
            raw['host_tool'], tool = tool, 'Grep'
        elif tool == 'Append':
            # V1-094: an append is an edit that adds text at the end, so the evidence engine checks what it adds.
            raw['host_tool'], tool = tool, 'Edit'
            args = {'file_path': args.get('file_path'), 'old_string': '', 'new_string': args.get('content', '')}
        kwargs.update(tool=tool, arguments=args, raw=raw)
        return kwargs

    def _absence_on_a_cut_mirror(self, decision):
        """V1-094: beyond the 1,500-file (or 12 MB) cut the mirror lacks real modules, so "could not be verified" and
        "not found" are no evidence there. A challenge made only of such findings is allowed (and stays recorded)."""
        findings = decision.get('findings') or []
        absence = {'insufficient_evidence', 'contradicted'}
        if findings and all(f.get('category') in {'evidence', 'assumption'} and f.get('verdict') in absence
                            for f in findings):
            return {**decision, 'action': 'allow', 'reason': 'AgentGuard could not check this against the whole '
                    'project (more files than it mirrors), so its evidence finding was not raised.'}
        return decision

    # -- terminal test outcomes (V1-027, V1-026) ----------------------------------------------------------------------

    @staticmethod
    def _decoded(result):
        if isinstance(result, str):
            try:
                value = json.loads(result)
            except ValueError:
                return {'stdout': result}
            return value if isinstance(value, dict) else {'stdout': result}
        return dict(result) if isinstance(result, dict) else {}

    def _terminal(self, key, root, kwargs, host):
        """A terminal Bash result: decoded, and for a test command replaced by a host summary. (kwargs, run record)."""
        from agentguard.verify import runners
        result = self._decoded(kwargs.get('result'))
        command = (kwargs.get('arguments') or {}).get('command')
        kwargs = {**kwargs, 'result': result}
        if host or not isinstance(command, str) or not runners.is_test_command(command):
            return kwargs, None
        family = runners.runner_for_command(command)
        verdict = self._terminal_verdict(family, result)
        if verdict is None:
            return {**kwargs, 'arguments': {'command': UNREAD_COMMAND}, 'result': {'exit_code': None}}, None
        outcome, count = verdict
        normalized = ' '.join(command.split())[:200]
        run = {'key': 'terminal:' + normalized, 'family': family, 'evidence': True, 'digest': self._snapshot(root),
               'label': 'terminal test run ' + json.dumps(normalized[:120])}
        return {**kwargs, 'arguments': {'command': TERMINAL_COMMAND + family},
                'result': {'stdout': f'Host terminal outcome: {outcome}\n=== {count} {outcome} ===\n',
                           'exit_code': 0 if outcome == 'passed' else 1}}, run

    @staticmethod
    def _terminal_verdict(family, result):
        """('passed'|'failed', count) from the exit code and Pebble's own parser, or None when it proves nothing."""
        from . import recipes
        recipe = TERMINAL_PARSERS.get(family, 'npm-test')
        exit_code = result.get('exit_code', result.get('exitCode'))
        text = '\n'.join(str(result[k]) for k in ('stdout', 'stderr', 'output') if isinstance(result.get(k), str))
        if type(exit_code) is not int:
            return None
        parsed = recipes.parse(recipe, text) if text else None
        failed = (parsed['failed'] + parsed['errors']) if parsed else 0
        if exit_code != 0 or result.get('interrupted'):
            return 'failed', max(failed, 1)
        if result.get('outcome_uncertain') or result.get('output_complete') is False:
            return None
        outcome, tests = recipes.classify('test', recipe, 0, text)
        if outcome == 'passed':
            return 'passed', tests['passed']
        if outcome == 'failed':
            return 'failed', max(failed, 1)
        return None

    def _outcome_count(self, key):
        _, state = self._live(key)
        return len(state.verification.outcomes) if state is not None else 0

    def _record_run(self, key, seen, run):
        task, state = self._live(key)
        if state is None or len(state.verification.outcomes) <= seen:
            return
        task_id, runs = self.recipe_runs.get(key, (None, []))
        runs = runs if task_id == task else []
        runs.append({**run, 'outcome': state.verification.outcomes[-1]})
        self.recipe_runs[key] = (task, runs)

    def _touch(self, key, names):
        _, state = self._live(key)
        if state is None:
            return
        scoped = self.roots.get(key)
        index = self.guard.workspace(scoped).index
        for name in names:
            path = index.normalize(name)
            if not path:
                continue
            if (Path(key[0]) / name).exists():
                state.touch(path)
            else:
                state.deleted_files.add(path)

    # -- the completion gate ------------------------------------------------------------------------------------------

    def _before_stop(self, key, root, mirror):
        """(task state, the outcomes the gate reads or None, whether the index is unavailable for a change)."""
        task, state = self._live(key)
        if state is None:
            return None, None, False
        ws = self.guard.workspace(mirror.path)
        self._ground(key, ws, state)
        outcomes = self._gate_outcomes(key, root, state)
        unavailable = not ws.index.is_built and any(
            Path(p).suffix in SOURCE_SUFFIXES for p in state.changed_files)
        if unavailable:
            # Positive host evidence still counts: a passing test run on the project's current files.
            reading = outcomes if outcomes is not None else state.verification.outcomes
            evidence = [r for r in self._runs(key) if r['evidence'] and r['outcome'].passed is True and r['digest']]
            current = self._snapshot(root) if evidence else None
            if current and any(r['digest'] == current for r in evidence) and not any(o.passed is False for o in reading):
                unavailable = False
        return state, outcomes, unavailable

    def _runs(self, key):
        task, _ = self._live(key)
        task_id, runs = self.recipe_runs.get(key, (None, ()))
        return list(runs) if task_id == task and task is not None else []

    def _ground(self, key, ws, state):
        """A task opened before the index existed gets its intent and scope from the index once it does."""
        if key not in self.ungrounded or not ws.index.is_built:
            return
        from agentguard import complexity, intent
        prompt = self.prompts.get(key, '')
        try:
            spec = intent.extract(prompt, ws.index)
            spec.complexity = complexity.assess(spec, ws.index)
        except Exception:
            return
        spec.tests_waiver = waived(prompt)
        state.spec = spec
        self.ungrounded.discard(key)

    def _gate_outcomes(self, key, root, state):
        """The outcomes the completion gate reads, or None when that is the engine's own list (Phase 21 F21-gate).

        Every project test run of a run tree is recorded in one task, and the vendored gate fails while any recorded
        outcome failed. So the latest run of a recipe (or of one terminal command) replaces that key's earlier
        failures when it passed and the project still has the exact files it tested; a terminal failure is also
        replaced by the latest run of a test recipe of the same runner that came after it and passed on the current
        files. A failing latest run, a pass on files changed since or on unapplied drafts, an incomplete snapshot,
        another runner's pass and a command that only names the recipe replace nothing. A pass on files changed
        since is no evidence either (dropped), and a lint or build pass never is (V1-093): it only replaces the same
        recipe's failure."""
        runs = self._runs(key)
        if not runs:
            return None
        current = MISSING

        def now():
            nonlocal current
            if current is MISSING:
                current = self._snapshot(root)
            return current

        latest = {}
        for i, run in enumerate(runs):
            latest[run['key']] = i

        def clears(i):
            run = runs[i]
            return run['outcome'].passed is True and bool(run['digest']) and run['digest'] == now()

        drop = set()
        for i, run in enumerate(runs):
            outcome = run['outcome']
            if outcome.passed is True:
                if not run['evidence'] or run['digest'] and now() and run['digest'] != now():
                    drop.add(id(outcome))
            elif outcome.passed is False:
                if latest[run['key']] != i and clears(latest[run['key']]):
                    drop.add(id(outcome))
                elif run['family'] and not run['key'].startswith(RECIPE_COMMAND):
                    later = {latest[r['key']] for r in runs[i + 1:] if r['key'].startswith(RECIPE_COMMAND)
                             and r['family'] == run['family'] and r['evidence']}
                    if any(j > i and clears(j) for j in later):
                        drop.add(id(outcome))
        if not drop:
            return None
        return [o for o in state.verification.outcomes if id(o) not in drop]

    def _gate(self, key, root, mirror, state, outcomes, result, unavailable):
        """The verdict as structured fields (V1-026): the controller builds every sentence from these, never from text
        the tested code printed. Paths are project-relative names; nothing else here comes from the project."""
        gate = {'kind': 'pass', 'files_changed': 0, 'changed': [], 'covering': [], 'problems': [], 'failed': None,
                'tests': {'passed': 0, 'failed': 0, 'runner': None}, 'snapshot_current': None, 'waived': False,
                'untrusted_summary': None}
        if state is None:
            gate['outcome'] = 'untracked'
            return gate
        from agentguard.verify import completion_gate, runners, static
        ws = self.guard.workspace(mirror.path)
        index = ws.index
        changed = sorted(p for p in state.changed_files if completion_gate._is_source(p, index))
        reading = outcomes if outcomes is not None else list(state.verification.outcomes)
        runs = {id(r['outcome']): r for r in self._runs(key)}
        passing = [o for o in reading if o.passed is True]
        failing = next((o for o in reversed(reading) if o.passed is False), None)
        gate.update(files_changed=len(changed), changed=changed[:4], waived=bool(state.spec.tests_waived))
        gate['tests'] = {'passed': sum(max(o.total - o.failed, 1) for o in passing),
                         'failed': sum(max(o.failed, 1) for o in reading if o.passed is False),
                         'runner': self._runner(failing or (passing[-1] if passing else None), runs)}
        digests = [runs[id(o)]['digest'] for o in passing if id(o) in runs and runs[id(o)]['digest']]
        if digests:
            current = self._snapshot(root)
            gate['snapshot_current'] = bool(current) and all(d == current for d in digests)
        subjects = {f.subject for f in result.findings}
        if unavailable:
            gate['kind'] = 'unavailable'
        elif result.action == 'allow':
            gate['kind'] = 'pass'
        elif 'unparsable-on-completion' in subjects:
            gate['kind'] = 'unparsable'
            gate['problems'] = [{'path': p.path, 'line': p.line, 'message': p.message[:120]}
                                for p in static.check_files(index.root, state.touched_files)[:3]]
        elif 'failing-tests-on-completion' in subjects and failing is not None:
            gate['kind'] = 'failing_tests'
            run = runs.get(id(failing))
            gate['failed'] = {'label': run['label'] if run else self._label(failing.command),
                              'count': max(failing.failed, 1), 'runner': gate['tests']['runner']}
            if failing.summary and not HOST_SUMMARY.fullmatch(failing.summary.strip()):
                gate['untrusted_summary'] = failing.summary[:200]
        elif 'no-test-run' in subjects:
            gate['kind'] = 'no_test_run'
            gate['covering'] = runners.affected_tests(index, set(changed))[:4]
        else:
            gate['kind'] = 'other'
        gate['outcome'] = ('unavailable' if gate['kind'] == 'unavailable' else 'held' if gate['kind'] != 'pass'
                           else 'no_changes' if not changed
                           else 'verified' if passing and gate['snapshot_current'] is not False
                           else 'not_verified')
        return gate

    @staticmethod
    def _label(command):
        match = NAMED_RECIPE.match(command or '')
        if match:
            return recipe_label(match[1])
        return 'test run'

    @staticmethod
    def _runner(outcome, runs):
        if outcome is None:
            return None
        run = runs.get(id(outcome))
        if run and run.get('family'):
            return run['family']
        match = NAMED_RECIPE.match(outcome.command or '')
        if match:
            return RECIPE_FAMILIES.get(match[1], match[1])
        return outcome.runner if outcome.runner != 'unknown' else None

    @staticmethod
    def receipt(gate):
        """The content-free summary of one completion check, for the run's receipt (V1-095): counts, the runner's
        host name and the verdict; never a path, a command or test output."""
        if not isinstance(gate, dict):
            return None
        tests = gate.get('tests') if isinstance(gate.get('tests'), dict) else {}
        return {'files_changed': int(gate.get('files_changed') or 0),
                'tests': {'passed': int(tests.get('passed') or 0), 'failed': int(tests.get('failed') or 0),
                          'runner': tests.get('runner') if isinstance(tests.get('runner'), str) else None},
                'snapshot_current': gate.get('snapshot_current') if isinstance(gate.get('snapshot_current'), bool)
                else None,
                'outcome': gate.get('outcome') if isinstance(gate.get('outcome'), str) else 'untracked',
                'reason': gate.get('kind') if isinstance(gate.get('kind'), str) else None}

    # -- sessions -----------------------------------------------------------------------------------------------------

    def begin(self, root, cid, prompt, resume=False, changed=()):
        """Open or continue the AgentGuard task for one host run.

        The vendored user_prompt handler always opens a fresh task, which
        would discard changed files and test outcomes observed before a wait
        or native approval. A same-service continuation therefore keeps its
        live task. If that state is gone (service restart or bounded engine
        eviction), a new task is opened from the original goal and only the
        broker-recorded ``changed`` paths are restored as scope. Earlier test
        outcomes are not restored, so the gate needs fresh evidence. A fresh
        task also takes ``changed`` as its own scope (V1-029: the unverified
        paths a held run left), so a follow-up request is held the same way.

        Phase 24 (V1-031): nothing here waits for the index, so the first model request never does. The root's one
        mirror is synced incrementally (a full copy only the first time a service sees the root); the index builds
        or refreshes in the background, and the first mutating check or the stop gate waits for it.
        AgentGuard's planning budget (the user_prompt decision's additional context) is withheld on purpose: it is
        not measured against Pebble's own context budgets (V1-142).
        """
        if not self.guard:
            return {"state": "unavailable"}
        key = (str(Path(root)), cid)
        with self.lock:
            continued = resume and self._task(key) is not None
        if continued:
            self._attach(key, fresh=True)  # what changed during the wait reaches the index, not the task's scope
            return {"state": "continued"}
        # Dispatch through handle(), which hosts and tests may intercept.
        self.handle("session_start", root, cid)
        # The engine grounds the task's scope only if the index is built when it handles the prompt. Read the index
        # before that (it only ever turns built): read after, a build finishing in between left the task unscoped for
        # the whole run and the scope check never fired (Phase 25).
        with self.lock:
            scoped = self.roots.get(key)
            built = bool(scoped) and self.guard.workspace(scoped).index.is_built
        self.handle("user_prompt", root, cid, prompt_text=prompt)
        with self.lock:
            self.tasks.pop(key, None)
            self.recipe_runs.pop(key, None)
            scoped = self.roots.get(key)
            if not scoped:
                return {"state": "untracked"}
            ws = self.guard.workspace(scoped)
            task = ws.resolve_task_id(cid)
            state = ws.task_state(task, cid) if task else None
            if state is None:
                return {"state": "untracked"}
            self.tasks[key] = task
            self.prompts[key] = (prompt or '')[:2000]
            state.spec.tests_waiver = waived(prompt)
            if not built:
                self.ungrounded.add(key)
            self._prune(key)
            restored = set()
            for name in changed:
                path = ws.index.normalize(name)
                if path:
                    state.touch(path)
                    restored.add(path)
            if not resume:
                return {"state": "started", **({"carried_paths": len(restored)} if restored else {})}
            return {"state": "rebuilt", "restored_paths": len(restored),
                    "verification": "not_restored"}

    def _prune(self, current):
        """Release adapter state for sessions whose task the engine no longer holds (it keeps eight per root)."""
        for key in [k for k in self.roots if k != current and k in self.tasks and self._task(k) is None]:
            self._drop_key(key)

    def end(self, root, session):
        """Phase 24 (V1-005, V1-020): a run reached a terminal state (not a wait). AgentGuard closes the session
        (ended_at, its tasks closed, retention maintenance) and forgets its in-memory state; this adapter releases the
        session's drafts and records. The root's mirror stays for the next run (LRU of four). Idempotent."""
        if not self.guard:
            return {'ended': 0}
        with self.lock:
            keys = [k for k in list(self.roots) + list(self.tasks) if k[1] == session
                    and (root is None or k[0] == str(Path(root)))]
            keys = list(dict.fromkeys(keys))
            mirrors = {k: self.mirrors.get(k[0]) for k in keys}
        ended = 0
        for key in keys:
            mirror = mirrors[key]
            if mirror is not None and not mirror.removed and key in self.roots:
                try:
                    self.guard.handle(self.event_type(event='session_end', agent='pebble', workspace=str(mirror.path),
                                                      session_id=session))
                    ended += 1
                except Exception:
                    pass
            with self.lock:
                self._drop_key(key)
        return {'ended': ended}

    def listing(self, root):
        """Phase 24 (V1-031): the names of the root's last sync (begin() syncs at session_start and on resume), for
        the runner's project listing, so a message walks the tree once. None without a synced mirror."""
        with self.lock:
            mirror = self.mirrors.get(str(Path(root)))
        if mirror is None or mirror.removed or mirror.names is None:
            return None
        return {'files': list(mirror.names), 'truncated': mirror.names_truncated}

    def forget(self, session_ids):
        """Phase 24 (V1-020): a deleted conversation's sessions (its run ids and its own id). Every AgentGuard row they
        left (prompts, events, decisions, findings, challenges, verifications, tasks, sessions) is deleted from each
        database this adapter used and from the one under this App's home, and their in-memory state is released.
        Raises when a database refused, so the caller can report the deletion as incomplete."""
        ids = {s for s in session_ids if isinstance(s, str) and s}
        if not ids:
            return {'sessions': 0}
        with self.lock:
            for key in [k for k in list(self.roots) + list(self.tasks) if k[1] in ids]:
                self._drop_key(key, keep_task=False)
                self.tasks.pop(key, None)
            if self.guard:
                for scoped in {m.path for m in self.mirrors.values()}:
                    ws = self.guard._workspaces.get(str(Path(scoped).resolve()))
                    if ws is not None:
                        for session in ids:
                            ws.end_session(session)
        removed = {}
        if self.guard is None:
            return {'sessions': len(ids), 'rows': removed}
        from agentguard.core.store import Database
        paths = set(self.databases) | {str(self.home / 'guard' / 'agentguard.db')}
        for path in sorted(paths):
            if not Path(path).is_file():
                continue
            with Database._registry_lock:
                db = Database._instances.get(path)
            temporary = db is None
            if temporary:
                db = Database(Path(path), self.guard.settings)
            try:
                for table, count in db.forget_sessions(sorted(ids)).items():
                    removed[table] = removed.get(table, 0) + count
            finally:
                if temporary:
                    db.close()
        return {'sessions': len(ids), 'rows': removed}

    def maintain(self, force=False):
        """Retention for the databases this adapter used (the embedded tiers in RETENTION); counts removed."""
        removed = {}
        if self.guard is None:
            return removed
        from agentguard.core.store import Database
        for path in sorted(self.databases):
            with Database._registry_lock:
                db = Database._instances.get(path)
            if db is not None:
                for table, count in (db.maintain(force=force) or {}).items():
                    removed[table] = removed.get(table, 0) + count
        return removed

    def _task(self, key):
        scoped, task = self.roots.get(key), self.tasks.get(key)
        if not scoped or not task:
            return None
        return self.guard.workspace(scoped).task_state(task, key[1])

    def _live(self, key):
        """(task id, TaskState) the engine's handlers resolve for this host run, or (None, None)."""
        scoped = self.roots.get(key)
        if not self.guard or not scoped:
            return None, None
        ws = self.guard.workspace(scoped)
        task = ws.resolve_task_id(key[1])
        state = ws.task_state(task, key[1]) if task else None
        return (task, state) if state is not None else (None, None)

    @staticmethod
    def _snapshot(root):
        """The digest a test snapshot of the project's files would record now, or None."""
        from .testing import snapshot_digest
        from .policy import Workspace

        try:
            return snapshot_digest(Workspace(root))
        except (ValueError, OSError):  # PolicyError is a ValueError, e.g. the project now exceeds 24 MB
            return None

    # -- drafts -------------------------------------------------------------------------------------------------------

    def stage(self, root, cid, name, content):
        with self.lock:
            return self._stage(root, cid, name, content)

    def _stage(self, root, cid, name, content):
        from .policy import Workspace

        key = (str(Path(root)), cid)
        mirror = self.mirrors.get(key[0]) if key in self.roots else None
        if mirror is None or mirror.removed:
            return
        name = Path(name).as_posix()
        bases = self.bases.setdefault(key, {})
        if name not in bases:
            bases[name] = _digest(key[0], name)
        with mirror.lock:
            if mirror.owner != key:
                self._switch(mirror, key)
            target = Workspace(mirror.path).path(name, create=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            mirror.owner = key
        self.overlays.setdefault(key, {})[name] = content

    def unstage(self, root, cid, path):
        """Phase 24 (V1-028): a draft that never reached the project (rejected, undone, or replaced and then refused)
        leaves the mirror, and stops counting as this session's change unless the project file still differs from
        how it was before the session first staged it. {'unstaged': whether a draft was held, 'changed': whether the
        path still counts}."""
        if not self.guard:
            return {'unstaged': False, 'changed': False}
        key, name = (str(Path(root)), cid), Path(path).as_posix()
        with self.lock:
            mirror = self.mirrors.get(key[0])
            overlay = self.overlays.get(key, {})
            held = overlay.pop(name, None) is not None
            _, state = self._live(key)
            rel = self.guard.workspace(self.roots[key]).index.normalize(name) if state is not None else ''
            if not held and (state is None or rel not in state.changed_files) and name not in self.bases.get(key, {}):
                return {'unstaged': False, 'changed': False}  # nothing of this session's to release
            if mirror is not None and not mirror.removed:
                other = mirror.owner is not None and mirror.owner != key and name in self.overlays.get(mirror.owner, {})
                if not other:
                    with mirror.lock:
                        self._restore(mirror, name)
                    self._reindex(mirror, {name})
            if state is None:
                return {'unstaged': held, 'changed': False}
            base = self.bases.get(key, {}).get(name, MISSING)
            if base is not MISSING and _digest(key[0], name) == base:
                state.touched_files.discard(rel)
                state.deleted_files.discard(rel)
                self.bases[key].pop(name, None)
            return {'unstaged': held, 'changed': rel in state.changed_files}

    # -- host test results ----------------------------------------------------------------------------------------------

    def test_result(self, root, cid, recipe, result):
        # F21-gate: note which engine outcome this host result became, with the snapshot it tested.
        key = (str(Path(root)), cid)
        with self.lock:
            seen = self._outcome_count(key)
        decision = self._test_result(root, cid, recipe, result)
        with self.lock:
            digest = result.get('snapshot_digest')
            usable = isinstance(digest, str) and not result.get('snapshot_truncated')
            legacy = result.get('outcome') is None
            self._record_run(key, seen, {
                'key': (RECIPE_COMMAND + str(recipe)) if not legacy else 'legacy:' + str(recipe),
                'family': RECIPE_FAMILIES.get(str(recipe)), 'digest': digest if usable else None,
                'evidence': result.get('kind', 'test') == 'test',
                'label': recipe_label(recipe) if not legacy else 'test run'})
        return decision

    def _test_result(self, root, cid, recipe, result):
        if result.get("outcome") is not None:
            return self._recipe_result(root, cid, recipe, result)
        # A result without the host's classification: classified here the same way, and only the summary is sent.
        from . import recipes
        parser = 'python-unittest' if recipe == 'python-unittest' else 'node-test'
        command = "python -m unittest discover -v" if recipe == "python-unittest" else "node --test"
        exit_code = result.get("exit_code")
        outcome, tests = recipes.classify('test', parser, exit_code if type(exit_code) is int else 1,
                                          result.get("output") or '')
        failed = (tests['failed'] + tests['errors']) if tests else 0
        if outcome == 'passed' and exit_code == 0:
            summary = f"=== {tests['passed']} passed ==="
        elif exit_code != 0 or outcome == 'failed':
            summary = f"=== {max(failed, 1)} failed ==="
        else:
            summary = "=== no tests ran ==="
        return self.handle(
            "post_tool_use",
            root,
            cid,
            tool="Bash",
            arguments={"command": command},
            result={"stdout": f"Host test outcome for {recipe}\n{summary}\n", "exit_code": exit_code},
            host_summary=True,
        )

    def _recipe_result(self, root, cid, recipe, result):
        # Host-classified recipe outcome. Only `passed` with a parsed positive
        # count is success evidence; zero tests, unparsed output, failures and
        # truncated runs are reported to AgentGuard as a failed command even
        # when the framework exited 0, so they can never satisfy completion.
        tests = result.get("tests") if isinstance(result.get("tests"), dict) else {}
        total = tests.get("total") if type(tests.get("total")) is int else 0
        # output_truncated shortens only the display excerpt; the host
        # classified the outcome from the complete capture.
        passed = (
            result.get("outcome") == "passed"
            and result.get("exit_code") == 0
            and (result.get("kind", "test") != "test" or total > 0)
        )
        # AgentGuard reads only this host summary, never the raw test stdout,
        # which the tested code controls and could use to print fake counts.
        failed = sum(tests.get(k, 0) for k in ("failed", "errors") if type(tests.get(k)) is int)
        if passed:
            summary = f"=== {max(total, 1)} passed ==="
        elif result.get("outcome") == "no_tests":
            summary = "=== no tests ran ==="
        else:
            summary = f"=== {max(failed, 1)} failed ==="
        return self.handle(
            "post_tool_use",
            root,
            cid,
            tool="Bash",
            arguments={"command": RECIPE_COMMAND + str(recipe)},
            result={"stdout": f"Host recipe outcome for {recipe}: {result.get('outcome')}\n{summary}\n",
                    "exit_code": 0 if passed else (result.get("exit_code") or 1)},
            host_summary=True,
        )

    # -- host sentences ---------------------------------------------------------------------------------------------

    def screen_input(self, text):
        """Phase 24 (V1-053): a refusal sentence for text typed into a process, or None. Stateless, so retrying the same
        text is refused again; AgentGuard's challenge ledger is not consulted."""
        if not isinstance(text, str):
            return None
        if not self.guard:
            return 'AgentGuard is unavailable, so typed input is disabled. Nothing was typed.'
        from agentguard.validate import checks
        findings = checks.risky_command(self.event_type(event='pre_tool_use', tool='Bash',
                                                        arguments={'command': text}))
        if findings:
            what = findings[0].summary.removeprefix('Irreversible command: ')
            return f'This input looks irreversible ({what}), so it needs a reviewed command instead. Nothing was typed.'
        for pattern, what in REPL_RISKS:
            if pattern.search(text):
                return f'This input looks irreversible ({what}), so it needs a reviewed command instead. Nothing was typed.'
        return None

    @staticmethod
    def review_reason(decision):
        """V1-141: why AgentGuard asked for a person's review of a command, for the approval card (host words)."""
        for finding in (decision or {}).get('findings') or []:
            if finding.get('category') == 'risk' and isinstance(finding.get('summary'), str):
                return 'AgentGuard asked for your review: ' + finding['summary'][:160] + '.'
        return 'AgentGuard asked for your review of this command.'

    @staticmethod
    def refusal(decision):
        """V1-141: the tool error for a pre-tool decision that stops the action. Nothing ran in any of these cases."""
        action = (decision or {}).get('action')
        if action == 'modify':
            suggested = ((decision.get('updated_arguments') or {}).get('command'))
            return ('AgentGuard: nothing ran. This command can expand to an unintended target; propose a narrower '
                    'command' + (' such as ' + json.dumps(suggested[:200]) if isinstance(suggested, str) else '') + '.')
        if action == 'request_review':
            return 'AgentGuard: nothing ran. This tool cannot be staged for review: ' + \
                GuardAdapter.review_reason(decision).removeprefix('AgentGuard asked for your review: ')
        return 'AgentGuard: ' + ((decision or {}).get('reason') or str(action))

    def close(self):
        self.closed = True
        for mirror in list(self.mirrors.values()):
            mirror.removed = True
        for mirror in list(self.mirrors.values()):
            worker = mirror.worker
            if worker is not None:
                worker.join(timeout=1)
        if self.guard:
            self.guard.close()
        with GuardAdapter._open_lock:
            GuardAdapter._open.discard(self)
            last = not GuardAdapter._open
        if self.guard and last:
            # Database.shared() keeps one connection per AGENTGUARD_HOME for the process, and ProjectStore.close() owns
            # nothing; the one under this App's own home is this App's: close it with the App. AGENTGUARD_HOME is
            # process-global, so another open adapter's workspaces may be bound to it: then it stays open, as before.
            from agentguard.core.store import Database
            with Database._registry_lock:
                for key in [k for k in Database._instances if Path(k).is_relative_to(self.home)]:
                    try:
                        Database._instances.pop(key).close()
                    except Exception:
                        pass  # as Database.reset_shared(): shutdown continues to the run journal and store
        for path in set(self.roots.values()) | {m.path for m in self.mirrors.values()}:
            shutil.rmtree(path, ignore_errors=True)
        self.mirrors.clear()
        self.tasks.clear()
        self.recipe_runs.clear()
        self.overlays.clear()
        self.bases.clear()
        self.prompts.clear()
