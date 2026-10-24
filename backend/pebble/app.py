import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from . import VERSION
from .store import Store, LISTED_CHATS, error_kind, friendly_error
from .policy import Workspace, PolicyError, installed_apps
# Phase 16 D03: launches are pinned to the reviewed bundle and signing team, re-read before opening.
from .native_apps import launch as launch_app, pin as pin_app, describe as describe_launch
from .providers import Providers
from .memory import Memory
from .permissions import DEFAULT, GROUP, OPTIONAL_TOOLS, automatic, validate as validate_permissions
from .web import weather, search
from .guard import GuardAdapter
from .sources import fetch_url
from .testing import run_tests
from .terminal import inspect_command, run_command
from .vault import Vault
from .extensions import Extensions
from .hooks import HookInterrupted
from .artifacts import Artifacts
from .artifact_schema import evidence_argument
from .chatgpt import ChatGPT
from .preview import Previews
from .views import Views
from .runtime import RunJournal, RunLimits, RESUMABLE, EFFECTS, extension_fingerprint, claim_owner
from .runtime_commands import CATALOGUE, command as runtime_command
from .controller import RunController
from .provenance import SourceProvenance
from .search_index import SearchIndex


from .tool_catalogue import TOOLS, CORE_TOOLS  # TOOLS: re-exported (tests import it from here)
from .tool_authz import Actor, canonical_catalogue, catalogue_digest, check as authorize_tool, pin as authz_pin
from . import companion_settings, quiz_grading, ui_guidance


class PlanVault(Vault):
    """Typed-plan credentials, kept apart from provider keys in their own keychain service."""
    service = b"com.pebble.local.plan-credentials"


def plan_guard_text(raw):
    """AgentGuard judges a typed plan as the commands it would run (joined argv), never as JSON."""
    import shlex
    try:
        steps = json.loads(raw).get('steps') if isinstance(raw, str) else None
        return ' | '.join(shlex.join([str(a) for a in step.get('argv', [])]) for step in steps if isinstance(step, dict))
    except (TypeError, ValueError, AttributeError):
        return str(raw)[:2000]


class App:
    def __init__(self, home, resources, emit):
        self.home, self.resources, self.emit = home, resources, emit
        try:
            self._initialize()
        except BaseException:
            self._abort_startup()
            raise

    def _initialize(self):
        home, resources, emit = self.home, self.resources, self.emit
        if home.is_symlink():
            raise ValueError('The Pebble data directory must not be a symbolic link.')
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._startup_lease = claim_owner(home)
        from .migrations import prepare
        self._upgrade = prepare(home)
        self.store = Store(home)
        self.memory = Memory(self.store)
        self.extensions = Extensions(self.store)
        old_policy = self.store.setting("permissions")
        if old_policy is None:
            self.store.set_setting("permissions", validate_permissions(DEFAULT))
        else:
            # Fill required groups added since the value was saved. An omitted optional group already
            # means off, so it is not written: an upgrade keeps the person's saved value exactly.
            tools = {**{k: v for k, v in DEFAULT["tools"].items() if k not in OPTIONAL_TOOLS}, **old_policy["tools"]}
            validate_permissions({"mode": old_policy["mode"], "tools": tools})
            if tools != old_policy["tools"]:
                self.store.set_setting("permissions", {"mode": old_policy["mode"], "tools": tools})
        self.model = Providers(home, resources, self.store, emit)
        self.chatgpt = ChatGPT(self.model)
        self.model.auth = self.chatgpt
        from .images import Images
        self.images = Images(self.store)
        from .attachments import Files
        self.files = Files(self.store, home)
        self.artifacts = Artifacts(self.store)
        self.previews = Previews()
        self.views = Views()
        self.guard = GuardAdapter(home)
        self.cancel = threading.Event()
        self.active = None
        self.chatgpt.can_activate = lambda: not self.active and not self.closed
        self.task_lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.sources = {}
        self._handoffs = {}  # (chat_id, plan_fingerprint) -> reviewed handoff in progress; never persisted.
        self.closed = False
        self.task_policy = None
        # Canonical schemas pinned at startup. A later in-place schema mutation
        # fails the catalogue layer instead of silently changing validation.
        self.tool_catalogue_digest = catalogue_digest(canonical_catalogue())
        self.current_text = ""
        self.worker = None
        self.active_run = None
        self.current_tool_call = None
        self.pause_requested = threading.Event()
        self.runs = RunJournal(self.store, home, lease=self._startup_lease)
        self._startup_lease = None  # RunJournal now owns the same live descriptor.
        from .time_ledger import TimeLedger
        self.time_ledger = TimeLedger(self.store)
        # A dead service's open slices stay fully charged (crash/reboot) and are never refunded.
        self.time_ledger_recovery = self.time_ledger.recover()
        from .token_calibration import DDL as CALIBRATION_DDL
        with self.store.lock:
            self.store.db.executescript(CALIBRATION_DDL)
        # Phase 16: the research source ledger (operational evidence, never a memory layer), Pebble's own
        # browser (refuses to start until experiment E-B1) and scoped screen sessions (the AX backend refuses
        # every call until E-N2).
        from .source_ledger import SourceLedger
        from .browser_session import BrowserAdapter, BrowserProfile, PebbleBrowser, launcher_from_evidence
        from .screen_session import AppAdapter, ScreenSessions
        from . import ax_worker
        self.ledger = SourceLedger(self.store)
        # Phase 18 §7.2 patch 1: per-App web seams, so an evaluation driver can install fixtures on its own App only.
        self.web_search = search
        self.web_weather = weather
        from .research_store import ResearchStore
        self.research = ResearchStore(self.ledger)
        from .telemetry import Telemetry
        # Phase 18 E06: off by default, local only, no network destination; saved only through the native Save
        # dialog's single-use token (claimed through Exports, which App creates below).
        self.telemetry = Telemetry(home)
        self.telemetry.claim_destination = lambda token: self.exports.claim_file_destination('telemetry', token)
        # Phase 22 (b22-browser): Pebble's own browser starts only with the parent's recorded, digest-pinned E-B1 run
        # (no record: BlockedLauncher with the same refusal as before). The person's own Chrome tabs, shared from
        # Pebble for Chrome and paired through the Keychain, sit behind the same screen sessions.
        self.browser = PebbleBrowser(BrowserProfile(home), launcher=launcher_from_evidence(home))
        from .browser_bridge import (BrowserBridge, BrowserRouter, ChromeAdapter, ChromeTabs, keychain_pairing,
                                     own_executable, revoke_chrome_sessions)
        extension_folder = next((p for p in (Path(resources) / 'browser-extension',
                                             Path(__file__).resolve().parents[2] / 'browser-extension')
                                 if (p / 'manifest.json').is_file()), None)
        self.browser_bridge = BrowserBridge(
            home, extension_folder=extension_folder, pairing=keychain_pairing(),
            host_paths={own_executable()},  # the bundled pebble-backend is also the native host (--browser-native-host)
            on_revoke=lambda tab_id=None: revoke_chrome_sessions(self.screens, tab_id),
            notify=lambda data: self.emit('browser', data))  # Phase 24 (V1-043): pairing waits, a tab was shared
        self.chrome_tabs = ChromeTabs(self.browser_bridge)
        self.screens = ScreenSessions(self.store, {'app': AppAdapter(ax_worker.BlockedBackend()),
                                                   'browser': BrowserRouter({'pebble': BrowserAdapter(self.browser),
                                                                             'chrome': ChromeAdapter(self.chrome_tabs)})})
        self.provenance = SourceProvenance(self.store, self.runs)
        from .stream_journal import StreamJournal
        from .process_jobs import ProcessJobs
        self.streams = StreamJournal(self.store, self.runs)
        # Phase 22 (t22-stream): the run_activity timeline and live reasoning summaries (display only; summary text
        # stays in this process's memory). Events go through self.emit at call time.
        from .run_activity import ActivityFeed
        self.activity = ActivityFeed(self.store, self.runs, lambda event, data: self.emit(event, data), label=self.tool_label)
        # Reattachment runs after the upgrade completes (see resume() below), so a
        # future epoch's preservation check never sees reattachment changes.
        self.processes = ProcessJobs(self, resume=False)
        # Asynchronous commands require the containment self-test (sandbox-instance
        # sweep of escaped descendants), run once on first need; failure keeps them disabled.
        self.processes.containment = self.containment_status
        # Typed plans with credential references: values come from the OS vault over the shim's
        # pipe after spawn, never through the environment, argv, job folder or records.
        self.processes.fetch = self.plan_credential
        from .process_reviews import ProcessInspections
        self.process_inspections = ProcessInspections(self)
        self._process_guard_observed = set()
        self.search = SearchIndex(self.store)
        self.controller = RunController(self)
        self.interactions = self.controller.interactions
        from .exports import Exports
        # Phase 17 (A2): settles an interrupted render to failed and an interrupted save to uncertain; never retries.
        self.exports = Exports(home, self.store, self.artifacts, runs=self.runs, interactions=self.interactions, emit=emit)
        from .artifact_inputs import ArtifactInputs
        from .mods import Mods
        self.artifact_inputs = ArtifactInputs(self)
        # Phase 19 (epoch 9): host-held quiz answer keys and human quiz attempts, created at construction like
        # GOAL_DDL so upgrade completion sees the full schema. Attempts expire lazily when read, never at startup.
        from .quiz_grading import DDL as QUIZ_DDL, QuizAttempts
        with self.store.lock:
            self.store.db.executescript(QUIZ_DDL)
        self.quizzes = QuizAttempts(self)
        self.mods = Mods(self)
        self.views.guard = self.view_binding_valid
        from .memory_learning import Learning
        self.learning = Learning(self)
        from .connector_auth import ConnectorAuth
        self.connector_auth = ConnectorAuth(self)
        self.extensions.auth = self.connector_auth
        from .connector_effects import ConnectorEffects
        self.extensions.effects = ConnectorEffects(self.store)  # once per process; recovers admitted -> unknown
        from .plugin_packages import Packages
        self.packages = Packages(self)
        self.extensions.packages = self.packages
        from .mcp_media import Media, connector_secret_filter
        self.plugin_media = Media(self.store, home, review=self.review_plugin_media, scrub=connector_secret_filter(self.connector_auth.redact))
        self.extensions.media = self.plugin_media
        self.extensions.media_origin = self.plugin_media_origin
        self.extensions.publish_media = self.publish_plugin_media
        self.model.context_observer = self.observe_context
        self.model.context_transform = self.compact_context
        # Phase 36 (W6): long-task sealing (context_seal) is on; PEBBLE_CONTEXT_SEAL=0 turns it off, for the 2.0.0
        # re-benchmark's registered control arm (D55).
        self.context_seal_enabled = os.environ.get('PEBBLE_CONTEXT_SEAL', '1') != '0'
        # Phase 37 (1.5.0, Pebble's Workshop): RunCode above 8K; PEBBLE_CODE_MODE=0 keeps 1.4.0's requests.
        self.code_mode_enabled = os.environ.get('PEBBLE_CODE_MODE', '1') != '0'
        self.step_tools = None  # the runner's offered schemas for the step being executed (RunCode's inner calls)
        from .agent_manager import Agents
        self.agents = Agents(self)
        from .git_workflows import GitWorkflows
        from .jobs import Jobs
        from .lifecycle import Lifecycle
        from .notifications import NotificationAPI, Notifications
        self.git = GitWorkflows(self)
        # Phase 17 (A1): created before upgrade completion (their epoch-8 tables exist) and before jobs.start(),
        # so notices from startup recovery already get delivery rows. Jobs resolves both per call.
        self.lifecycle = Lifecycle(self)
        self.notifications = Notifications(self)
        self.jobs = Jobs(self)
        from .job_api import JobAPI
        from .git_api import GitAPI
        self.job_api = JobAPI(self)
        self.notification_api = NotificationAPI(self)
        self.git_api = GitAPI(self)
        from .child_worktrees import ChildWorktrees
        from .git_plans import GitPlans
        from .environments import Environments
        from .coding_api import CodingAPI
        self.child_worktrees = ChildWorktrees(self, self.git)
        self.git_plans = GitPlans(self, self.git)
        self.environments = Environments(self)
        self.coding = CodingAPI(self)
        from .goals import Goals
        self.goals = Goals(self)  # Phase 17 (A3): person-created goals; only runs GOAL_DDL at construction
        self.exports.goals = self.goals
        from .rewind import Rewinds
        self.rewinds = Rewinds(self)  # Phase 20 X10: run checkpoints and rewind; constructs no table
        from .context_inspector import ContextInspector
        self.context_inspector = ContextInspector(self)  # Phase 20 C20-context (C09): main window only; constructs no table
        from .data_mirror import DataServices
        # Phase 22 s22-storage: ~/.pebble's settings.json and PEBBLE.md, the ~/Documents/Pebble copies and Recently
        # deleted. Inert on a flat --home (tests, validators). Constructs no table; starts after the upgrade completed.
        self.storage = DataServices(self)
        self.upgrade_receipt = self._upgrade.complete()
        self.processes.resume()  # reattach or settle supervised jobs a dead service left live
        self.agents.start()
        self.controller.start()
        self.jobs.start()
        self.storage.start()  # after upgrade completion: a settings.json edit never lands inside an upgrade startup
        if self.storage.active:
            # Phase 22 (merge): only Pebble's own data home (~/.pebble, where the native host looks) listens for Chrome;
            # a flat --home (tests, validators, evaluation drivers) binds no socket. Starting reads no Keychain item:
            # the pairing record is read at the first browser_extension_status. A failure shows there; never raises.
            self.browser_bridge.start(report=False)
        else:
            self.browser_bridge.error = "Pebble for Chrome connects only to Pebble's own data folder (~/.pebble)."
        self._routes = self._route_table()  # V1-132: built once, after every subsystem exists
        # V1-170: close() sets this, so the idle loop ends at once instead of after up to 5 s.
        self._idle_stop = threading.Event()
        self._idle_thread = threading.Thread(target=self.idle, name='pebble-idle', daemon=True)
        self._idle_thread.start()

    _containment_lock = threading.Lock()
    _containment_result = None  # per service process: a machine/launcher fact, not per home

    def containment_status(self):
        """Once per process, on first need, never from model input; fails closed."""
        with App._containment_lock:
            if App._containment_result is None:
                try:
                    from .containment import self_test
                    folder = Path(self.home) / 'containment-self-test'
                    folder.mkdir(mode=0o700, exist_ok=True)
                    result = self_test(folder)
                    contained = result.get('contained') is True
                    reason = ('Process containment verified on this machine.' if contained else
                              'Process containment self-test failed (' + str(result.get('reason'))[:200] +
                              '); background commands stay disabled.')
                except Exception as exc:
                    contained, reason = False, ('Process containment self-test could not run (' +
                                                type(exc).__name__ + '); background commands stay disabled.')
                App._containment_result = {'contained': contained, 'reason': reason}
            return dict(App._containment_result)

    def _abort_startup(self):
        """Release initialized owners without masking the startup failure.

        A live effect owner keeps its database and lease until it exits. Most
        failures occur before background workers start; partial scheduler
        startup follows the same ownership rule as normal shutdown.
        """
        self.closed = True
        stop = getattr(self, '_idle_stop', None)
        if stop is not None:
            stop.set()
        upgrade = getattr(self, '_upgrade', None)
        if upgrade is not None:
            try:
                upgrade.abort()
            except BaseException:
                pass
        idle_cancel = getattr(self, '_idle_cancel', None)
        if idle_cancel is not None:
            idle_cancel.set()
        for name, methods in (
            ('browser_bridge', ('close',)), ('storage', ('stop',)),  # Phase 22: Chrome lets go; the copy thread stops
            ('processes', ('close',)), ('jobs', ('close',)), ('git', ('close',)), ('environments', ('close',)), ('agents', ('close',)),
            ('packages', ('close',)), ('connector_auth', ('close',)),
            ('controller', ('close',)), ('chatgpt', ('cancel',)),
            ('previews', ('close',)), ('views', ('close',)),
            ('model', ('cancel', 'unload')), ('extensions', ('close',)),
            ('guard', ('close',)), ('runs', ('close',)), ('store', ('close',)),
        ):
            owner = getattr(self, name, None)
            if owner is None:
                continue
            try:
                for method in methods:
                    if getattr(owner, method)() is False:
                        return
            except BaseException:
                # Preserve the original exception and never close the database
                # underneath a component whose cleanup could not be confirmed.
                return
        lease = getattr(self, '_startup_lease', None)
        if lease is not None:
            lease.close()

    @property
    def cancel(self):
        controller = getattr(self, 'controller', None)
        return controller.live.cancel if controller and controller.live else self._idle_cancel

    @cancel.setter
    def cancel(self, value):
        self._idle_cancel = value

    @property
    def pause_requested(self):
        controller = getattr(self, 'controller', None)
        return controller.live.pause if controller and controller.live else self._idle_pause

    @pause_requested.setter
    def pause_requested(self, value):
        self._idle_pause = value

    @property
    def task_policy(self):
        controller = getattr(self, 'controller', None)
        execution = controller.current() if controller else None
        return execution.policy if execution else getattr(self, '_standalone_policy', None)

    @task_policy.setter
    def task_policy(self, value):
        controller = getattr(self, 'controller', None)
        execution = controller.current() if controller else None
        if execution:
            execution.policy = value
        else:
            self._standalone_policy = value

    @property
    def current_text(self):
        controller = getattr(self, 'controller', None)
        execution = controller.current() if controller else None
        return execution.goal if execution else getattr(self, '_standalone_text', '')

    @current_text.setter
    def current_text(self, value):
        controller = getattr(self, 'controller', None)
        execution = controller.current() if controller else None
        if execution:
            execution.goal = value
        else:
            self._standalone_text = value

    @property
    def current_tool_call(self):
        controller = getattr(self, 'controller', None)
        execution = controller.current() if controller else None
        return execution.current_call if execution else getattr(self, '_standalone_call', None)

    @current_tool_call.setter
    def current_tool_call(self, value):
        controller = getattr(self, 'controller', None)
        execution = controller.current() if controller else None
        if execution:
            execution.current_call = value
        else:
            self._standalone_call = value

    IDLE_SECONDS = 5  # V1-170: between idle maintenance passes; close() ends the wait at once

    def idle(self):
        while not self._idle_stop.wait(self.IDLE_SECONDS):
            if self.closed:
                return
            try:
                with self.task_lock:
                    if self.closed or self.active or self.agents.busy():
                        continue
                    self.model.idle()
                    try:
                        self.memory.expire()
                        self.learning.maintain()
                        self.learning.consolidate()  # Phase 15 M07: bounded second maintenance stage
                        self.memory.maintenance_error = None
                    except Exception as exc:
                        # Personalization failure cannot stop the service or
                        # start inference. Explicit inspection can retry later.
                        self.memory.maintenance_error = type(exc).__name__
            except Exception as exc:
                # V1-012: one failed pass (a locked or full database, a model that will not unload) never ends this
                # loop. Content-free; the next pass retries.
                logging.getLogger(__name__).warning('Idle pass failed (%s); retrying.', error_kind(exc))

    def chat(self, cid):
        rows = self.store.query("SELECT * FROM chats WHERE id=?", (cid,))
        if not rows:
            raise ValueError("Conversation not found.")
        actions = self.store.actions(cid)
        metrics = {
            r["message_id"]: json.loads(r["payload"])
            for r in self.store.query("SELECT * FROM metrics WHERE chat_id=?", (cid,))
        }
        messages = self.store.query(
            "SELECT * FROM messages WHERE chat_id=? ORDER BY created", (cid,)
        )
        # Present older host-generated execution records in human language. Raw
        # evidence remains in the action ledger; model prose is never rewritten.
        attached = self.images.metadata(cid) + self.files.metadata(cid)
        # V1-075: attachments and legacy evidence are looked up, not searched for every message.
        by_message, evidence = {}, None
        for item in attached:
            by_message.setdefault(item["message_id"], []).append(item)
        produced = self._message_runs(cid)
        for message in messages:
            message["attachments"] = by_message.get(message["id"], [])
            if message["id"] in metrics:
                message["usage"] = metrics[message["id"]]
            if message["role"] == "assistant" and message["id"] in produced:
                message["run_id"] = produced[message["id"]]  # Phase 22 (u22-shell P2): its run's run_activity
            if message["content"].startswith("**Action evidence** · Approved "):
                if evidence is None:
                    # Each result encoded once, by length: {length: {encoding: (index, action)}}. The first action in
                    # created order whose encoding ends the message wins, as in the earlier search.
                    evidence = {}
                    for index, action in enumerate(actions):
                        result = action["payload"].get("result")
                        if result:
                            encoded = json.dumps(result)[:5000]
                            evidence.setdefault(len(encoded), {}).setdefault(encoded, (index, action))
                content = message["content"]
                found = [table[content[-size:]] for size, table in evidence.items() if content[-size:] in table]
                if found:
                    action = min(found, key=lambda pair: pair[0])[1]
                    message["content"] = self.action_summary(
                        action["kind"], action["payload"], action["payload"]["result"]
                    )
        runs = self.runs.list(cid)
        # Phase 24 (V1-121, t24-ux-shell): each settled run of the newest 20 carries the counts its receipt shows, as
        # activity.ts tally() counts the run_activity items, so a reopened reply reads "Worked for 42 s · 6 steps", not
        # "Thought for". None when the record cannot be read (the window then says "Took 42 s").
        for run in runs[:20]:
            if run['state'] not in ('queued', 'running', 'retry_wait'):
                run['counts'] = self._run_counts(run['id'], run['updated'])
        questions = self.interactions.questions(cid)
        question_runs = {q['run_id'] for q in questions}
        focused = next((r for r in runs if r['state'] in {'queued', 'running', 'retry_wait'}), None)
        focused = focused or next((r for r in runs if r['id'] in question_runs), None)
        focused = focused or next((r for r in runs if r['state'] == 'waiting_approval'), None)
        focused = focused or (runs[0] if runs else None)
        from .data_mirror import is_trashed
        return {
            **rows[0],
            "trashed": is_trashed(self.store, cid),  # V1-018: in Recently deleted (the pet drops such a saved chat)
            "messages": messages,
            "actions": actions,
            "artifacts": [self.artifact_inputs.decorate(a) for a in self.artifacts.list(cid)],
            "runs": runs,
            "questions": questions,
            "plan": self.interactions.plan(focused['id']) if focused else None,
        }

    def _message_runs(self, cid):
        """{assistant message id: run id} for a conversation (Phase 22). A run writes one reply message, whose id its
        checkpoint carries; each settled run's id is read once (SQLite's JSON reader, no checkpoint is loaded here)
        and kept, so reopening a chat costs nothing more. A failure only leaves messages untagged."""
        known = self.__dict__.setdefault('_run_messages', {})
        try:
            runs = self.store.query('SELECT id,state FROM runs WHERE chat_id=?', (cid,))
            states = {r['id']: r['state'] for r in runs}
            missing = [r['id'] for r in runs if r['id'] not in known]
            for start in range(0, len(missing), 200):
                chunk = missing[start:start + 200]
                for row in self.store.query("SELECT id,json_extract(checkpoint,'$.message_id') AS mid FROM runs "
                                            'WHERE id IN (%s)' % ','.join('?' * len(chunk)), chunk):
                    mid = row['mid'] if isinstance(row['mid'], str) else None
                    if mid or states[row['id']] not in ('queued', 'running', 'retry_wait'):
                        known[row['id']] = mid  # a live run may not have written its message id yet
            if len(known) > 20_000:
                known.clear()
            return {known[r['id']]: r['id'] for r in runs if known.get(r['id'])}
        except Exception:
            return {}

    def _run_counts(self, run_id, updated):
        """{steps, retries, waited} for one settled run, from its run_activity items (tool, check and agent rows are
        steps; retry rows retries; approval durations the seconds waited for the person). Kept per (run, last
        transition): a resumed run that settles again has a new `updated`, so it is counted again (m24c)."""
        known = self.__dict__.setdefault('_run_counts_cache', {})
        if (run_id, updated) in known:
            return known[(run_id, updated)]
        try:
            _, items, _ = self.activity.items(run_id)
        except Exception:
            return None
        counts = {'steps': sum(1 for i in items if i['kind'] in ('tool', 'check', 'child')),
                  'retries': sum(1 for i in items if i['kind'] == 'retry'),
                  'waited': round(sum(i['duration'] or 0 for i in items if i['kind'] == 'approval'), 3)}
        if len(known) >= 512:
            known.clear()
        known[(run_id, updated)] = counts
        return counts

    def dispatch(self, method, params):
        # V1-132: the route table is built once, at the end of _initialize; a call before then builds it here.
        methods = self.__dict__.get('_routes') or self._route_table()
        if method not in methods:
            raise ValueError("Unknown request.")
        if method == 'chatgpt_start' and not self.active:
            # Phase 24 (t24-auth, i8-code-07): the discovery fetch (up to 20 s per socket operation, with its retries)
            # runs before task_lock; start() then reuses the validated copy, so no sign-in network wait holds a lock.
            # While a task runs, chatgpt_start refuses first, as before, with no fetch.
            self.chatgpt.prefetch()
        # Scheduled admission owns the same lock as foreground admission. A
        # native configuration change must not pass its idle check just before
        # a scheduled root claims the lane with the previous configuration.
        if method in {'set_workspace', 'set_model', 'model_load', 'model_unload',
                      'model_save', 'model_save_many', 'model_select', 'model_effort',
                      'model_remove', 'setting', 'chatgpt_start',
                      'chatgpt_sign_out', 'extension_add', 'extension_import',
                      'extension_toggle', 'extension_remove', 'mod_install',
                      'mod_toggle', 'mod_remove'}:
            with self.task_lock:
                return methods[method](**params)
        return methods[method](**params)

    def _route_table(self):
        methods = {
            "state": self.state,
            "new_chat": self.new_chat,
            "get_chat": lambda **p: self.chat(p["id"]),
            "set_workspace": self.set_workspace,
            "send": self.send,
            "cancel": self.stop,
            "model_load": lambda **p: self.model.load() or self.model.status(),
            "model_unload": self.unload,
            "set_model": self.set_model,
            "approve": self.approve,
            "reject": self.reject,
            "apps": lambda **p: installed_apps(),
            "attach": self.attach,
            "attach_bytes": self.attach_bytes,
            "attach_url": self.attach_url,
            "remove_source": self.remove_source,
            "delete_chat": self.delete_chat,
            "chat_rename": self.chat_rename,  # Phase 24 V1-125: main window only (NATIVE_ONLY; main.rs ALLOWED, never the pet)
            "setting": self.setting,
            "open_app": self.open_app,
            "undo": self.undo,
            "model_profiles": lambda **p: self.model.profiles(),
            "model_discover": self.model.discover,
            "model_save": self.model_save,
            "model_save_many": self.model_save_many,
            "model_select": self.model_select,
            'model_effort': self.model_effort,
            "model_remove": self.model_remove,
            'model_handoff_preview': self.model_handoff_preview,
            'model_handoff_approve': self.model_handoff_approve,
            'model_handoff_start': self.model_handoff_start,
            "chatgpt_start": self.chatgpt_start,
            "chatgpt_status": lambda **p: self.chatgpt.status(),
            "chatgpt_cancel": lambda **p: self.chatgpt.cancel() or True,
            "chatgpt_sign_out": self.chatgpt_sign_out,
            'view_stage': self.view_stage,
            'view_stop': self.views.stop,
            'artifact_get': self.artifact_get,
            'artifact_history': lambda chat_id, id: [self.artifact_inputs.decorate(a) for a in self.artifacts.history(chat_id, id)],
            'artifact_submit': self.artifact_submit,
            'artifact_stop': self.artifact_stop,
            'artifact_view_open': self.artifact_view_open,
            # Phase 19 native view routes: human task data, never model tools (tool_authz.NATIVE_ONLY). main.rs lets
            # both windows reach all but quiz_share, whose `surface` it writes from the calling window's label.
            'artifact_refresh': self.artifact_refresh,
            'quiz_attempt': lambda chat_id, id, revision: self.quizzes.attempt(chat_id, id, revision),
            'quiz_start': lambda chat_id, id, revision: self.quiz_route('start', chat_id, id, revision),
            'quiz_save': lambda chat_id, attempt_id, revision, answers: self.quiz_route('save', chat_id, attempt_id, revision, answers),
            'quiz_submit': lambda chat_id, attempt_id, revision, answers: self.quiz_route('submit', chat_id, attempt_id, revision, answers),
            'quiz_share': self.quiz_share,
            'mods': lambda **p: {'items': self.mods.list(), 'runtime': self.mods.status()},
            'mod_example': lambda **p: self.mods.example(),
            'mod_preview': lambda **p: self.mods.preview(**p),
            'mod_install': lambda **p: self.mod_operation('install', **p),
            'mod_toggle': lambda **p: self.mod_operation('toggle', **p),
            'mod_remove': lambda **p: self.mod_operation('remove', **p),
            "memory_list": self.memory_list,
            "memory_save": self.memory_save,
            "memory_delete": self.memory_delete,
            "memory_clear": self.memory_clear,
            'memory_candidates': lambda id=None, limit=100: self.learning.candidates(id, limit),
            'memory_maintain': self.memory_maintain,
            'memory_resolve': self.memory_resolve,
            'memory_export': self.memory_export,
            'memory_recall': self.memory_recall,
            "extensions": lambda **p: self.extensions.list(),
            'skill_inspect': lambda id, digest=None, reference=None, offset=0, limit=3000: self.extensions.inspect_skill(id, digest, reference, offset, limit),
            'hook_events': self.hook_events,
            "extension_add": self.extension_add,
            "extension_import": self.extension_import,
            "extension_toggle": self.extension_toggle,
            "extension_remove": self.extension_remove,
            'plugin_package_preview': lambda **p: self.packages.preview(**p),
            'plugin_package_import': lambda **p: self.packages.import_bundle(**p),
            'plugin_package_install': lambda **p: self.packages.install(**p),
            'plugin_package_update': lambda **p: self.packages.update(**p),
            'plugin_package_history': lambda **p: self.packages.history(**p),
            'plugin_package_inspect': lambda **p: self.packages.inspect(**p),
            'plugin_package_preview_rollback': lambda **p: self.packages.preview_rollback(**p),
            'plugin_package_rollback': lambda **p: self.packages.rollback(**p),
            'media_artifact': self.media_artifact,
            'media_download': self.media_download,
            'connector_discover_interop': lambda **p: self.connector_operation('discover_interop',**p),
            'connector_preview_registration': lambda **p: self.connector_operation('preview_registration',**p),
            'connector_register_client': lambda **p: self.connector_operation('register_client',**p),
            'connector_metadata': lambda id: self.connector_auth.metadata(id),
            'connector_discover': lambda **p: self.connector_operation('discover',**p),
            'connector_preview': lambda **p: self.connector_operation('preview',**p),
            'connector_start': lambda **p: self.connector_operation('start',**p),
            'connector_status': lambda **p: self.connector_auth.status(),
            'connector_cancel': lambda **p: self.connector_auth.cancel(),
            'connector_save_bearer': lambda **p: self.connector_operation('save_bearer',**p),
            'connector_save_environment': lambda **p: self.connector_operation('save_environment',**p),
            'connector_save_environment_route': lambda **p: self.connector_operation('save_environment_route',**p),
            'connector_send_confirm': self.connector_send_confirm,
            'research_sources': self.research_sources,
            'screen_windows': lambda target: self.screens.windows(target),
            'screen_session_grant': self.screen_session_grant,
            'screen_sessions': lambda id: self.screens.sessions(id),
            'screen_handoff_done': lambda session_id: self.screens.resume(session_id),
            'screen_session_revoke': lambda session_id: self.screen_session_revoke(session_id),
            'browser_status': lambda **p: self.browser.status(),
            'browser_start': lambda **p: self.browser.start(),
            'browser_stop': lambda **p: self.browser_stop(),
            'browser_tabs': lambda **p: self.browser.tabs(),
            'browser_open_tab': lambda url: self.browser.open_tab(url),
            'browser_profile_forget': lambda **p: self.browser_profile_forget(),
            # Phase 22 (b22-browser) Pebble for Chrome: main window only (tool_authz.NATIVE_ONLY, main.rs ALLOWED).
            'browser_extension_status': lambda **p: self.browser_bridge.status(),
            'browser_extension_pause': lambda **p: self.browser_extension_pause(),
            'browser_extension_resume': lambda **p: self.browser_extension_resume(),
            'browser_extension_stop': lambda **p: self.browser_bridge.stop_all('pebble'),
            'browser_extension_unpair': lambda **p: self.browser_bridge.unpair(),
            'connector_disconnect': lambda **p: self.connector_operation('disconnect',**p),
            "terminal_propose": self.terminal_propose,
            "open_url": self.open_url,
            "run_list": self.run_list,
            "run_get": self.run_get,
            # Phase 22: the activity timeline from host records (activity contract); display data only.
            'run_activity': lambda run_id, after=0: self.activity.read(run_id, after),
            "run_handoff": self.run_handoff,
            "run_compact": self.run_compact,
            "run_context_archive": self.run_context_archive,
            "search_stats": self.search_stats,
            "run_events": self.run_events,
            "run_pause": self.run_pause,
            "run_resume": self.run_resume,
            "run_reconcile": self.run_reconcile,
            "run_readback": self.run_readback,
            "memory_contests": self.memory_contests,
            "memory_resolve_contest": self.memory_resolve_contest,
            "memory_relations": self.memory_relations,
            "memory_forget_preview": self.memory_forget_preview,
            "memory_archive_export_to": self.memory_archive_export_to,
            "memory_archive_preview_file": self.memory_archive_preview_file,
            "memory_archive_import_file": self.memory_archive_import_file,
            "run_steer": self.run_steer,
            "question_answer": self.question_answer,
            "question_cancel": self.question_cancel,
            'agent_list': lambda chat_id, run_id: self.agents.list(chat_id, run_id),
            'process_read': self.process_read,
            'process_terminal': self.process_terminal,
            'process_input_human': self.process_input_human,
            'plan_credentials': self.plan_credentials,
            'plan_credential_save': self.plan_credential_save,
            'plan_credential_delete': self.plan_credential_delete,
            'process_stop': self.process_stop,
            'process_list': self.process_list,
            'process_dismiss': self.process_dismiss,
            'process_inspect': lambda id, run_id, process_id, observation, note: self.process_inspections.acknowledge(id, run_id, process_id, observation, note),
            'stream_read': lambda id, run_id, after=0, limit=50: self.streams.read(id, run_id, after, limit),
            'agent_get': lambda chat_id, run_id, agent_id: self.agents.get(chat_id, run_id, agent_id),
            'agent_steer': lambda chat_id, run_id, agent_id, text, input_id=None: self.agents.steer(chat_id, run_id, agent_id, text, input_id),
            'agent_cancel': lambda chat_id, run_id, agent_id: self.agents.cancel(chat_id, run_id, run_id, agent_id, origin='human'),
            "command_catalogue": lambda **p: CATALOGUE,
            'export_preview': lambda chat_id, source, format, options=None: self.exports.preview(chat_id, source, format, options),
            'export_render': lambda chat_id, plan_id: self.exports.render(chat_id, plan_id),
            'export_cancel': lambda chat_id, plan_id: self.exports.cancel(chat_id, plan_id),
            'export_save': lambda chat_id, export_id, destination_token: self.exports.save(chat_id, export_id, destination_token),
            'export_list': lambda chat_id: self.exports.list(chat_id),
            'export_get': lambda chat_id, export_id: self.exports.get(chat_id, export_id),
            'export_discard': lambda chat_id, export_id: self.export_discard(chat_id, export_id),
            # Native shell only: main.rs reaches these through native_call, never through rpc's ALLOWED.
            'export_destination_bind': lambda chat_id, export_id, path, replace=False: self.exports.bind_destination(chat_id, export_id, path, replace),
            'export_native_ready': lambda pdf=False: self.exports.native_ready(pdf),
            'export_pdf_attach': lambda chat_id, export_id, attach_token, sha256: self.exports.pdf_attach(chat_id, export_id, attach_token, sha256),
            'export_pdf_failed': lambda chat_id, export_id, attach_token, reason='': self.exports.pdf_failed(chat_id, export_id, attach_token, reason),
            # Phase 18 E06 telemetry (main-window Settings only; never the pet).
            'telemetry_get': lambda **p: self.telemetry.status(),
            'telemetry_set': self.telemetry_set,
            'telemetry_export': lambda destination_token: self.telemetry.export(destination_token),
            'telemetry_clear': lambda **p: self.telemetry.clear(),
            # Native shell only: the Save dialog's path becomes a single-use telemetry token.
            'telemetry_destination_bind': lambda path, replace=False: self.exports.bind_file_destination('telemetry', path, replace),
        }
        methods.update(self.job_api.methods)
        methods.update(self.notification_api.methods)         # main-window Settings: in ALLOWED, never the pet
        methods.update(self.notification_api.native_methods)  # Rust shell only: never in ALLOWED
        methods.update(self.git_api.methods())
        methods.update(self.coding.methods())
        methods.update(self.goals.methods())
        methods.update(self.rewinds.methods())  # Phase 20 X10: main window only (NATIVE_ONLY; main.rs writes surface)
        methods.update(self.context_inspector.methods())  # Phase 20 C20-context: main window only (NATIVE_ONLY; main.rs writes surface)
        methods.update(self.storage.methods())  # Phase 22 s22-storage: main window only (NATIVE_ONLY; main.rs ALLOWED, never the pet)
        methods.update(self.storage.decision_methods())  # Phase 24 (t24-data): main window only (NATIVE_ONLY; main.rs ALLOWED, never the pet)
        methods.update(self.controller.held_methods())  # Phase 24 (V1-029): main window only (NATIVE_ONLY; main.rs writes surface)
        from .native_host_setup import routes as connect_chrome_routes
        methods.update(connect_chrome_routes(self))  # Phase 24 V1-043 Connect Chrome: main window only (NATIVE_ONLY; main.rs ALLOWED, never the pet)
        return methods

    def state(self):
        read = self.store.read  # V1-011: the read-only connection; these two reads never hold Store.lock
        return {
            'job_notifications': read('SELECT id,chat_id,title,created FROM job_notifications WHERE acknowledged IS NULL ORDER BY created DESC LIMIT 100'),
            # Phase 22 (s22-storage): conversations in Recently deleted are hidden until restored or deleted for good.
            # V1-011: one set lookup instead of three correlated scans per conversation.
            "chats": read(LISTED_CHATS),
            "model": self.model.status(),
            "guard": {"available": bool(self.guard.guard), "error": self.guard.error},
            "active": self.active,
            "active_run": self.active_run,
            "runs": self.runs.list(),
            "settings": {
                # Permissions are reported with every group (an omitted optional group is off). The companion's
                # presence switches (Phase 19) read as their defaults when absent or malformed.
                k: validate_permissions(self.store.setting(k, v)) if k == "permissions"
                else companion_settings.read(self.store.setting(k, None)) if k == companion_settings.KEY
                else self.store.setting(k, v)
                for k, v in [
                    ("theme", "system"),
                    ("reduced_motion", False),
                    ("stealth", False),  # Phase 33: Stealth Lockdown
                    ("memory_enabled", True),
                    ('memory_layers', {k: True for k in ('working','episodic','semantic','procedural','temporal','profile')}),
                    ("permissions", DEFAULT),
                    (companion_settings.KEY, companion_settings.DEFAULT),
                ]
            },
            "data_dir": str(self.home),
            "platform": sys.platform,
            "version": VERSION,
            "memory": self.memory.stats(),
            "profiles": self.model.profiles(),
        }

    def new_chat(self, workspace=None):
        if workspace:
            workspace = str(Workspace(workspace).root)
        return self.chat(self.store.new_chat(workspace))

    def set_workspace(self, id, path):
        if self.active or self.agents.busy():
            raise ValueError("Stop the current task before changing its project.")
        if self.store.query("SELECT 1 FROM process_jobs WHERE chat_id=? AND state IN ('admitted','running') LIMIT 1", (id,)):
            raise ValueError('Stop owned commands before changing their project.')
        root = str(Workspace(path).root) if path else None
        self.store.query("UPDATE chats SET workspace=? WHERE id=?", (root, id))
        return self.chat(id)

    CHANGE_WAIT = 5.0  # merge round 2 (E3): seconds a model or permission change waits for settled children

    def _settled_for_change(self):
        """No task runs and no delegated worker remains. When the only workers left belong to children that already
        settled (their threads are releasing), the change waits for them inside the call, at most CHANGE_WAIT
        seconds, then refuses as before; a running task or a live child refuses at once (merge round 2, E3)."""
        deadline = time.monotonic() + self.CHANGE_WAIT
        while not self.active and self.agents.busy() and self.agents.releasing() and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self.active and not self.agents.busy()

    def set_model(self, path):
        if not self._settled_for_change():
            raise ValueError("Stop the current task before changing models.")
        return self.model.add_gguf(path)

    def model_save(self, **params):
        if not self._settled_for_change():
            raise ValueError("Stop the current task before changing models.")
        return self.model.save(**params)

    def model_save_many(self, **params):
        if not self._settled_for_change():
            raise ValueError('Stop the current task before changing models.')
        return self.model.save_many(**params)

    def model_select(self, profile_id):
        if not self._settled_for_change():
            raise ValueError("Stop the current task before changing models.")
        return self.model.select(profile_id)

    def model_effort(self, effort):
        if not self._settled_for_change():
            raise ValueError('Stop the current task before changing effort.')
        return self.model.effort(effort)

    def model_remove(self, profile_id):
        if not self._settled_for_change():
            raise ValueError("Stop the current task before changing models.")
        p = next((p for p in self.model.profiles() if p['id'] == profile_id), None)
        if p and p['kind'] == 'chatgpt' and len([x for x in self.model.profiles() if x.get('connection_id',x['id'])==p.get('connection_id',p['id'])])==1:
            self.chatgpt.sign_out(p.get('connection_id',p['id']))
        return self.model.remove(profile_id)

    def chatgpt_start(self, profile_id=None):
        if self.active:
            raise ValueError('Stop the current task before signing in.')
        return self.chatgpt.start(profile_id)

    def chatgpt_sign_out(self, profile_id):
        if self.active:
            raise ValueError('Stop the current task before signing out.')
        p = next((p for p in self.model.profiles() if p['id'] == profile_id), None)
        if not p or p['kind'] != 'chatgpt':
            raise ValueError('ChatGPT account not found.')
        return self.chatgpt.sign_out(p.get('connection_id',p['id']))

    def memory_list(self, kind=None, query=None, offset=0, limit=300):
        if kind == "working":
            return [
                {
                    "id": r["chat_id"],
                    "kind": "working",
                    "origin": "broker",
                    "scope": "chat:" + r["chat_id"],
                    "content": r["payload"],
                    "updated": r["updated"],
                }
                for r in self.store.query(
                    "SELECT * FROM checkpoints ORDER BY updated DESC LIMIT 100"
                )
            ]
        return self.memory.list(kind, query, offset, limit)

    def memory_save(self, kind, content, id=None, scope="global", expires=None):
        if kind in {"working", "episodic"}:
            raise ValueError(
                "Task context and experience are recorded from actual conversations."
            )
        cid, workspace = None, "global"
        if scope == "project":
            chat = self.chat(id)
            if not chat["workspace"]:
                raise ValueError("Connect a project before saving project memory.")
            workspace, cid = chat["workspace"], None  # V1-021: a project memory belongs to the project, not to the chat it was saved from
        elif scope != "global":
            raise ValueError("Choose personal or project memory.")
        if kind == "temporal" and expires is None:
            raise ValueError("Time-sensitive memories need an expiry date.")
        ident = self.memory.put(kind, content, workspace, cid, expires=expires)
        self.memory.relate_saved(ident)  # V1-035: a contradicting record the person saved earlier is contested
        return ident

    def memory_delete(self, id, kind=None):
        if self.active:
            raise ValueError("Stop the current task before forgetting memories.")
        result = self.learning.forget(id, working=kind == 'working')
        self.memory.discard_recall_index()  # forgotten text leaves the trigram index at once
        return result

    def memory_maintain(self, limit=16):
        with self.task_lock:
            if self.active:
                raise ValueError('Wait for the current task before running manual maintenance.')
            return self.learning.maintain(limit)

    def memory_resolve(self, candidate_id, accept):
        with self.task_lock:
            if self.active:
                raise ValueError('Wait for the current task before resolving a memory conflict.')
            return self.learning.resolve(candidate_id, accept)

    def memory_export(self):
        # Native inspection only; generated views/model tools cannot export it.
        return {'version': 1, 'memories': self.memory.list(), 'candidates': self.learning.candidates(),
            'limits': {'memories': 300, 'candidates': 100}, 'note': 'Bounded inspection; not a complete archive export.'}

    # Phase 15 native memory routes (main window only; never a model tool, mod, view or child route).

    def memory_contests(self):
        from . import memory_graph
        with self.store.lock:
            db = self.store.db
            ids = [r[0] for r in db.execute("SELECT memory_id FROM memory_evidence WHERE status='contested' "
                                            "ORDER BY memory_id LIMIT 100").fetchall()]
            def describe(mid):
                row = db.execute('SELECT id,kind,content,scope,origin,created FROM memories WHERE id=?', (mid,)).fetchone()
                if not row:
                    return None
                # V1-035: a person-saved contest reads its claim's label ("name"), not its internal person:<id> slot.
                label = db.execute("SELECT CASE WHEN e.candidate_id LIKE 'person:%' THEN c.label ELSE e.slot END "
                                   'FROM memory_evidence e LEFT JOIN memory_claims c ON c.memory_id=e.memory_id '
                                   'WHERE e.memory_id=?', (mid,)).fetchone()
                return {'memory_id': row[0], 'kind': row[1], 'label': label[0] if label else None,
                        'content': (row[2] or '')[:2000], 'scope': row[3], 'origin': row[4], 'created': row[5]}
            groups = []
            for mid in ids:
                record = describe(mid)
                if record is None:
                    continue
                others = [describe(other) for other, _ in memory_graph.open_contradictions(db, mid)]
                groups.append({**record, 'counterparts': [o for o in others if o]})
        return {'groups': groups, 'authorization': 'none'}

    def _memory_quiet(self, what):
        if self.active:
            raise ValueError('Wait for the current task before ' + what + '.')

    def memory_resolve_contest(self, memory_id, decision):
        with self.task_lock:
            self._memory_quiet('resolving a memory conflict')
            before = {r['id'] for r in self.store.query('SELECT id FROM memories')}
            result = self.learning.resolve_contest(memory_id, decision)
            after = {r['id'] for r in self.store.query('SELECT id FROM memories')}
        return {**(result if isinstance(result, dict) else {}), 'memory_id': memory_id, 'decision': decision,
                'forgotten': sorted(before - after), 'authorization': 'none'}

    def memory_relations(self, memory_id=None):
        return {'relations': self.learning.relations(memory_id, 100), 'authorization': 'none'}

    def memory_forget_preview(self, text=None, ids=None, id=None, limit=5):
        """V1-034: records for the main window's Forget card, by request text or memory IDs. A read with no
        model call and no authority; memory_delete stays the only forgetting route."""
        return self.memory.forget_preview(text, ids, id, limit)

    @staticmethod
    def _archive_path(path, new):
        if not isinstance(path, str) or not path.startswith('/') or '\0' in path or len(path) > 4096:
            raise ValueError('Choose the archive file with the native dialog.')
        target = Path(path)
        if not target.parent.is_dir() or target.parent.is_symlink():
            raise ValueError('The archive folder does not exist.')
        if new and os.path.lexists(target):
            raise ValueError('That file already exists; choose a new name. Nothing was overwritten.')
        if not new and (target.is_symlink() or not target.is_file()):
            raise ValueError('Choose an existing archive file.')
        return target

    def memory_archive_export_to(self, path):
        from . import memory_portable
        target = self._archive_path(path, new=True)
        with self.task_lock:
            self._memory_quiet('exporting memory')
            exported = memory_portable.export(self.learning)
        data = memory_portable.encode(exported['archive'])
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
        return {'path': str(target), 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data),
                'counts': exported['counts'], 'refused': len(exported['refused']), 'authorization': 'none'}

    def _read_archive(self, path):
        from . import memory_portable
        import stat
        target = self._archive_path(path, new=False)
        try:
            # No-follow, non-blocking: a link or FIFO swapped in after the check is refused, never read.
            descriptor = os.open(target, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        except OSError:
            raise ValueError('Choose an existing archive file.') from None
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Choose an existing archive file.')
            data = stream.read(memory_portable.MAX_ARCHIVE_BYTES + 1)
        if len(data) > memory_portable.MAX_ARCHIVE_BYTES:
            raise ValueError('The archive is larger than the import limit. Nothing was imported.')
        return data

    def memory_archive_preview_file(self, path):
        from . import memory_portable
        data = self._read_archive(path)
        with self.task_lock, self.store.lock:
            state = memory_portable.local_state(self.learning, self.store.db)
        plan = memory_portable.public(memory_portable.preview(data, state))
        skipped = plan['records']['skip']
        # The contract's plain counts, beside the archive layer's full preview.
        counts = {'add': plan['records']['import'], 'revalidate': plan['needs_revalidation'],
                  'tombstones': plan['tombstones']['rows'], 'skip_disabled': skipped.get('layer_disabled', 0)
                  + skipped.get('source_layer_disabled', 0), 'skip_tombstoned': skipped.get('forgotten', 0)
                  + skipped.get('watermark', 0), 'skip_duplicate': skipped.get('already_present', 0),
                  'expired': skipped.get('expired', 0), 'skip_other': sum(n for r, n in skipped.items() if r not in (
                      'layer_disabled', 'source_layer_disabled', 'forgotten', 'watermark', 'already_present', 'expired'))}
        return {**plan, 'counts': counts, 'authorization': 'none'}

    def memory_archive_import_file(self, path, plan_digest):
        from . import memory_portable
        data = self._read_archive(path)
        with self.task_lock:
            self._memory_quiet('importing memory')
            return {'applied': memory_portable.apply(self.learning, data, plan_digest), 'authorization': 'none'}

    def message_route(self, run_id=None):
        """The route class a stored message is produced under (C10): the run's pinned route, or the
        selected profile outside a run. None (stored as no row, so ``unknown``) when not attributable."""
        from . import model_handoff
        try:
            route = self.runs.get(run_id)['config']['route'] if run_id else self.model.current()
            return model_handoff.route_class(route) if route else None
        except Exception:
            return None

    # Reviewed model handoff (C10): main window only; never a model tool, mod, view or child route.
    HANDOFF_TTL = 3600
    HANDOFF_LIMIT = 4
    HANDOFF_TEXT = ('Continue the task from the conversation above. It was handed off from another model '
                    'route after a reviewed handoff; history the person did not release is not included.')

    def _handoff_profile(self, profile_id):
        profile = next((p for p in self.model.profiles() if p['id'] == profile_id), None)
        if profile is None:
            raise ValueError('Model profile not found.')
        return profile

    def _handoff(self, id, plan_fingerprint):
        entry = self._handoffs.get((id, plan_fingerprint))
        if entry is None or time.monotonic() - entry['created'] > self.HANDOFF_TTL:
            self._handoffs.pop((id, plan_fingerprint), None)
            raise ValueError('Review the handoff again; this preview is no longer available. Nothing was sent.')
        return entry

    def model_handoff_preview(self, id, run_id, profile_id, release=()):
        from . import model_handoff
        self.chat(id)
        run = self.runs.get(run_id, id)
        route = (run.get('config') or {}).get('route')
        if not route:
            raise ValueError('This run has no recorded model route to hand off from.')
        target = model_handoff.destination(self._handoff_profile(profile_id))
        rows = self.store.query('SELECT m.id,m.role,m.content,r.route_class FROM messages m LEFT JOIN message_routes r '
                                'ON r.message_id=m.id WHERE m.chat_id=? ORDER BY m.created', (id,))
        history = [{'id': r['id'], 'role': r['role'] if r['role'] in ('user', 'assistant') else 'host',
                    'content': r['content'] or '', 'route_class': r['route_class'] or model_handoff.UNKNOWN}
                   for r in rows]
        planned = model_handoff.plan(history, model_handoff.route_class(route), target, release=release)
        record = model_handoff.preview(planned, actor=model_handoff.NATIVE)
        now = time.monotonic()
        for key in [k for k, v in self._handoffs.items() if now - v['created'] > self.HANDOFF_TTL]:
            self._handoffs.pop(key, None)
        while len(self._handoffs) >= self.HANDOFF_LIMIT:
            self._handoffs.pop(min(self._handoffs, key=lambda k: self._handoffs[k]['created']))
        self._handoffs[(id, planned.fingerprint)] = {'plan': planned, 'preview': record, 'run_id': run_id,
                                                     'profile_id': profile_id, 'created': now}
        counts = {}
        for item in planned.items:
            role = counts.setdefault(item['role'], {})
            role[item['route_class']] = role.get(item['route_class'], 0) + 1
        return {'plan_fingerprint': planned.fingerprint, 'source_class': planned.source_class,
                'destination_class': planned.destination_class,
                'destination_fingerprint_prefix': planned.destination_fingerprint[:19],
                'counts': counts, 'send_count': record['send']['count'], 'bytes': record['send']['bytes'],
                'withheld_ids': record['withheld']['ids'], 'withheld_by_class': record['withheld']['by_class'],
                'released_classes': record['released_classes'], 'crosses_class': record['crosses_class'],
                'profile_id': profile_id, 'statement': record['statement'], 'status': record['status'],
                'authorization': 'none'}

    def model_handoff_approve(self, id, plan_fingerprint):
        from . import model_handoff
        entry = self._handoff(id, plan_fingerprint)
        entry['approval'] = model_handoff.approve(entry['preview'], actor=model_handoff.NATIVE)
        entry['bound'] = model_handoff.bind(entry['plan'], entry['plan'].destination_fingerprint)
        plan = entry['plan']
        self.runs.note(entry['run_id'], 'model_handoff_approved', {
            'plan_fingerprint': plan.fingerprint, 'source_class': plan.source_class,
            'destination_class': plan.destination_class, 'sent': len(plan.items), 'withheld': len(plan.withheld),
            'released_classes': list(plan.released)})
        return {'approved': True, 'plan_fingerprint': plan_fingerprint, 'authorization': 'none'}

    def model_handoff_start(self, id, plan_fingerprint, text=None):
        from . import model_handoff
        if text is not None and (not isinstance(text, str) or not text.strip() or len(text.encode()) > 100000):
            raise ValueError('Enter a message under 100 KB.')
        if text is not None and text.strip().startswith('/'):
            raise ValueError('Start a handoff with a task message, not a runtime command. Nothing was sent.')
        with self.task_lock:
            if self.active or self.agents.busy():
                raise ValueError('Stop the current task before starting a handoff.')
            entry = self._handoff(id, plan_fingerprint)
            if 'approval' not in entry:
                raise model_handoff.ModelHandoffError('not_approved')
            source = self.chat(id)
            try:
                profile = self._handoff_profile(entry['profile_id'])
            except ValueError:
                raise model_handoff.ModelHandoffError('destination_changed') from None
            cleared = model_handoff.check_at_start(entry['bound'], model_handoff.destination(profile)['fingerprint'],
                                                   entry['approval'])
            self._handoffs.pop((id, plan_fingerprint), None)  # One start per approval.
            allowed = [entry['plan'].destination_class, *entry['plan'].released]
            self.model_select(entry['profile_id'])
            current = self.model.current()
            if not current or model_handoff.destination(current)['fingerprint'] != entry['bound'].destination_fingerprint:
                raise model_handoff.ModelHandoffError('destination_changed')
            classes = [item['route_class'] for item in entry['bound'].packet['plan']['items']]
            cid = self.store.new_chat(source['workspace'])
            base = time.time()
            reviewed = []
            with self.store.lock, self.store.db as db:
                # The new conversation holds only the cleared items, each with the class it came from.
                for index, (item, origin) in enumerate(zip(cleared['items'], classes)):
                    mid = uuid.uuid4().hex
                    reviewed.append(mid)
                    role = item['role'] if item['role'] in ('user', 'assistant') else 'assistant'
                    db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (mid, cid, role, item['content'], base + index * 1e-6))
                    db.execute('INSERT INTO message_routes VALUES(?,?,?)', (mid, cid, origin))
                db.execute('UPDATE chats SET title=? WHERE id=?', (('Handoff: ' + (source['title'] or ''))[:54], cid))
            # Recall in this conversation keeps the handoff's classes (memory is never a side channel).
            chats = self.store.setting('model_handoff_chats', {}) or {}
            # Phase 24 (parent, t24-sec-recheck-arch-02): a release covers what the person reviewed: these messages and the
            # source conversation's records, not every later on-device turn or on-device record (data_mirror.route_view,
            # recall_for_route).
            released_runs = [r['id'] for r in self.store.query('SELECT id FROM runs WHERE chat_id=?', (id,))]
            chats[cid] = {'allowed': allowed, 'source_chat': id, 'released_messages': reviewed,
                          'released_runs': released_runs}
            self.store.set_setting('model_handoff_chats', chats)
            self.runs.note(entry['run_id'], 'model_handoff_started', {
                'plan_fingerprint': plan_fingerprint, 'chat_id': cid, 'binding': cleared['binding'],
                'sent': len(cleared['items']), 'withheld': cleared['withheld']})
            started = self.send(cid, text.strip() if text else self.HANDOFF_TEXT)
        return {'chat_id': cid, 'run_id': started['run_id'], 'sent': len(cleared['items']),
                'withheld': cleared['withheld'], 'destination_class': cleared['destination_class'],
                'authorization': 'none'}

    def _memory_origin_run(self, item, cid=None):
        """The run a recalled record came from, '' when it came from a conversation without a known
        run, or None for records the person saved (and host notes)."""
        ident = item.get('id') if isinstance(item, dict) else None
        if not isinstance(ident, str):
            return None
        if ident.startswith('episode-'):
            return ident[len('episode-'):]
        if ident.startswith('outcome-'):
            try:
                return json.loads(item.get('content') or '{}').get('run_id') or ''
            except ValueError:
                return ''
        rows = self.store.query('SELECT run_id FROM memory_evidence WHERE memory_id=?', (ident,))
        if rows and rows[0]['run_id'] != 'person':  # V1-035 person rows are judged as unlinked records below
            # An archive the person imported natively is not tied to a model route.
            return None if rows[0]['run_id'] == 'imported' else rows[0]['run_id']
        row = self.store.query('SELECT chat_id,origin FROM memories WHERE id=?', (ident,))
        # V1-021: a capture kept after its conversation was deleted (chat_id NULL) stays unattributed (fail closed).
        if row and (row[0]['chat_id'] or row[0]['origin'] == 'direct-user') and (row[0]['origin'] not in ('user', 'direct-user', 'imported')
                                          or row[0]['origin'] == 'direct-user' and row[0]['chat_id'] != cid):
            return ''  # A capture typed in another conversation is unattributed here (C10, W9 F3).
        return None

    def _run_chat(self, run_id):
        try:
            return self.runs.get(run_id)['chat_id']
        except Exception:
            return None

    def recall_for_route(self, cid, items, run_id=None):
        """C10: in a handed-off conversation, recall keeps only records from runs whose route class the person cleared in
        the reviewed handoff; records the person saved stay. Phase 24 (V1-002): in every other conversation a cloud request
        never receives a record learned in an on-device run (what was learned on this Mac stays on this Mac); everything
        else there is unchanged. The pinned working record counts as the run it was derived from."""
        from . import model_handoff
        from .data_mirror import working_origin
        scope = (self.store.setting('model_handoff_chats', {}) or {}).get(cid)
        if scope:
            allowed = set(scope['allowed'])
        else:
            destination = self.message_route(run_id)
            if destination is None or destination == model_handoff.DEVICE:
                return items
            allowed = None  # every class but the device's
        kept, withheld, classes = [], 0, {}
        for item in items:
            origin = self._memory_origin_run(item, cid)
            if origin is None and isinstance(item, dict) and item.get('kind') == 'working':
                origin = working_origin(self.store, cid)  # V1-002: the record of an earlier, maybe on-device, run
            if origin is None:
                kept.append(item)
                continue
            if origin not in classes:
                try:
                    route = self.runs.get(origin)['config']['route'] if origin else None
                    classes[origin] = model_handoff.route_class(route) if route else model_handoff.UNKNOWN
                except Exception:
                    classes[origin] = model_handoff.UNKNOWN
            if allowed is None:
                keep = classes[origin] != model_handoff.DEVICE
            else:
                keep = classes[origin] in allowed
                if keep and classes[origin] == model_handoff.DEVICE and scope.get('source_chat'):
                    # Phase 24 (parent, t24-sec-recheck-arch-02/-03): a released device class covers the on-device
                    # records of the runs the person reviewed (the source conversation's runs at the handoff), never one
                    # learned on this Mac later or in another conversation (a project shares its records across them).
                    runs = scope.get('released_runs')
                    keep = origin in runs if isinstance(runs, list) else self._run_chat(origin) == scope['source_chat']
            if keep:
                kept.append(item)
            else:
                withheld += 1
        if withheld and run_id:
            self.runs.note(run_id, 'handoff_recall_withheld', {'count': withheld})
        return kept

    def memory_recall(self, id, text, budget=2000):
        """Native inspection of scoped recall; no model tool or permission grant."""
        if not isinstance(text, str) or len(text) > 8000:
            raise ValueError('Use a recall query of at most 8,000 characters.')
        chat = self.chat(id)
        return self.memory.recall(text, chat['id'], chat['workspace'], budget)

    def memory_clear(self):
        if self.active:
            raise ValueError("Stop the current task before forgetting memories.")
        return self.memory.clear()

    def extension_add(self, **params):
        with self.task_lock:
            if self.active:
                raise ValueError("Stop the current task before changing extensions.")
            return self.extensions.add(**params)

    def extension_import(self, path):
        with self.task_lock:
            if self.active:
                raise ValueError("Stop the current task before changing extensions.")
            return self.extensions.import_skill(path)

    def extension_toggle(self, id, enabled):
        with self.task_lock:
            self.extensions.boundary()
            if self.active:
                raise ValueError("Stop the current task before changing extensions.")
            if (
                enabled
                and not self.store.setting("permissions", DEFAULT)["tools"]["plugins"]
                and self.extensions.get(id)["kind"] == "plugin"
            ):
                raise PolicyError("Plugin access is disabled in Permissions.")
            item = self.extensions.get(id)
            if enabled and item['kind'] == 'plugin' and item['config'].get('transport') == 'stdio':
                policy = self.store.setting('permissions', DEFAULT)['tools']
                if not policy['terminal'] or not policy['files']:
                    raise PolicyError('Local MCP launch requires terminal and file access in Permissions.')
            if enabled and item['kind']=='plugin':self.packages.validate(id)
            return self.extensions.toggle(id, enabled)

    def extension_remove(self, id):
        with self.task_lock:
            self.extensions.boundary()
            if self.active:
                raise ValueError("Stop the current task before changing extensions.")
            if self.extensions.get(id)['kind']=='plugin':
                self.connector_auth.disconnect(id)
                metadata=self.connector_auth.metadata(id)
                if metadata.get('state')=='revoked' or metadata.get('storage_cleanup_pending'):
                    raise PolicyError('Retry secure credential cleanup before removing this plugin.')
            result=self.extensions.remove(id)
            self.packages.forget(id)
            self.store.query('DELETE FROM connector_grants WHERE plugin_id=?',(id,))
            return result

    def connector_operation(self, operation, **params):
        if operation not in {'discover','preview','start','save_bearer','save_environment','save_environment_route','disconnect','discover_interop','preview_registration','register_client'}:
            raise PolicyError('Unknown native connector operation.')
        with self.task_lock:
            self.connector_auth.boundary()
            self.connector_auth.idle()
            if operation!='disconnect' and not self.store.setting('permissions',DEFAULT)['tools']['plugins']:
                raise PolicyError('Plugin access is disabled in Permissions.')
            return getattr(self.connector_auth,operation)(**params)

    def connector_send_confirm(self, id, action_id, recipients_sha256):
        """Native only: the person confirms a pending connector effect's exact reviewed recipients."""
        with self.action_lock:
            self.chat(id)
            return self.extensions.effects.confirm(id, action_id, recipients_sha256)

    def research_sources(self, id, run_id):
        """Native only (D01): a run's recorded sources (snippets apart from pages and PDFs, with hashes and dates),
        its claim conflicts and plan. Titles and URLs are untrusted page data; no page text is returned."""
        self.runs.get(run_id, id)
        sources = self.ledger.sources(run_id)
        return {'sources': sources[:200], 'omitted_sources': max(0, len(sources) - 200),
                'conflicts': self.ledger.conflicts(run_id)[:100], 'authorization': 'none'}

    # Phase 16 native screen and browser routes (main window only; never a model tool, mod, view or child route).

    def screen_session_grant(self, id, target, actions, ttl=1800, screenshots=False, enable_screen=False):
        """Phase 24 (V1-007): grant one window or tab to this conversation's live run when that run can use it, else
        to the conversation's next run (a pending grant). With Screen control off the answer offers to turn it on;
        enable_screen=True (the person's one confirmation) does it together with the grant. See grant_route."""
        from .screen_session import grant_route
        return grant_route(self, id, target, actions, ttl, screenshots=screenshots, enable_screen=enable_screen)

    def browser_stop(self):
        from .browser_session import revoke_browser_sessions
        return {'revoked_sessions': revoke_browser_sessions(self.screens), 'stopped': self.browser.stop()}

    def browser_profile_forget(self):
        from .browser_session import forget_browser_profile
        return forget_browser_profile(self.browser, self.screens)

    def screen_session_revoke(self, session_id):
        """End access. For a Chrome tab the extension also drops the grant at once (Phase 22)."""
        from .browser_bridge import end_chrome_session
        return end_chrome_session(self.screens, self.browser_bridge, session_id)

    def browser_extension_pause(self):
        from .browser_bridge import pause_chrome_sessions
        return pause_chrome_sessions(self.screens, self.browser_bridge)

    def browser_extension_resume(self):
        from .browser_bridge import resume_chrome_sessions
        return resume_chrome_sessions(self.screens, self.browser_bridge)

    def browser_screenshot_parts(self, session_id, run_id):
        """Phase 22 (runner P8): image parts after a ScreenObserve of a Chrome grant that allowed screenshots."""
        from .browser_bridge import screenshot_for_session
        try:
            return screenshot_for_session(self.screens, self.browser_bridge, self.chrome_tabs, session_id, run_id)
        except Exception:  # a screenshot is extra evidence; it never fails the observation
            return None

    def plugin_media_origin(self, payload, run_id, kind, identifier):
        if not run_id or run_id != self.active_run:
            raise PolicyError('MCP media requires the current effect owner.')
        candidates = [t for t in self.runs.tools(run_id) if t['state']=='running'
            and t['name'] in {'PluginCall','FetchPluginData'} and isinstance(t.get('arguments'),dict)
            and t['arguments'].get('plugin_id')==payload['plugin_id']]
        if self.current_tool_call:
            candidates=[t for t in candidates if t['call_id']==self.current_tool_call]
        if len(candidates)!=1:raise PolicyError('MCP media receipt owner is ambiguous.')
        return {'plugin_id':payload['plugin_id'],'run_id':run_id,'kind':kind,'identifier':identifier,
            'digest':payload['digest'],'receipt_id':candidates[0]['call_id']}

    def review_plugin_media(self, origin):
        if origin['run_id'] != self.active_run:return False
        receipt=next((t for t in self.runs.tools(origin['run_id']) if t['call_id']==origin['receipt_id']),None)
        if not receipt or receipt['state']!='running' or receipt['arguments'].get('plugin_id')!=origin['plugin_id']:return False
        if origin['kind']=='tool':
            self.extensions.checked_plugin(origin['plugin_id'],origin['digest'])
            routes=self.extensions.loaded_plugin_tools(origin['run_id'])
            return any(r['plugin_id']==origin['plugin_id'] and r['tool']==origin['identifier'] and r['digest']==origin['digest'] for r in routes)
        routes=self.extensions.content.loaded(origin['run_id'])
        return any(r['plugin_id']==origin['plugin_id'] and r['kind']==origin['kind'] and r['identifier']==origin['identifier'] and r['digest']==origin['digest'] for r in routes)

    def media_scope(self, chat_id, run_id):
        self.chat(chat_id)
        if self.runs.get(run_id)['chat_id']!=chat_id:raise PolicyError('This media belongs to another conversation.')
        policy=self.store.setting('permissions',DEFAULT)['tools']
        if not policy['plugins'] or not policy['views']:raise PolicyError('Enable plugin and view access to inspect connector media.')

    def publish_plugin_media(self, metadata):
        policy=self.task_policy or self.store.setting('permissions',DEFAULT)
        if not policy['tools']['views']:return
        cid=self.runs.get(metadata['origin']['run_id'])['chat_id']
        count=self.store.query('SELECT count(*) AS n FROM artifacts WHERE chat_id=?',(cid,))[0]['n']
        if count>=30:return
        aid=uuid.uuid4().hex
        data={'run_id':metadata['origin']['run_id'],'snapshot_id':metadata['id']}
        title='Connector image' if metadata['rendered'] else 'Connector attachment'
        self.store.query('INSERT INTO artifacts VALUES(?,?,?,?,?,?)',(aid,cid,title,'mcp_media',json.dumps(data),time.time()))
        self.emit('artifact',{'chat_id':cid,'id':aid,'title':title,'kind':'mcp_media','status':'ready'})

    def media_artifact(self, chat_id, run_id, snapshot_id):
        self.media_scope(chat_id,run_id)
        result=self.plugin_media.native_artifact(run_id,snapshot_id)
        return {k:v for k,v in result.items() if k!='preview_path'}

    def media_download(self, chat_id, run_id, snapshot_id):
        import base64
        self.media_scope(chat_id,run_id)
        result=self.plugin_media.download(run_id,snapshot_id)
        return {**result,'data':base64.b64encode(result['data']).decode(),'encoding':'base64'}

    def terminal_propose(self, id, command, network=False):
        chat = self.chat(id)
        if self.active:
            raise PolicyError(
                "Wait for the current task before starting a terminal command."
            )
        return self.execute(
            "RunCommand",
            {"command": command, "network": network},
            Workspace(chat["workspace"]) if chat["workspace"] else None,
            id,
            actor=Actor.native(),
        )

    def open_url(self, url):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        owned_preview = parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and any(parsed.port == s.server_port for s in self.previews.servers.values())
        with self.connector_auth.lock:
            owned_connector = bool(self.connector_auth.pending and self.connector_auth.pending.get('authorization_url')==url)
        if (
            (parsed.scheme != "https" and not owned_preview and not owned_connector)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or len(url) > 3000
        ):
            raise ValueError("Only public HTTPS links can be opened.")
        from .sources import public_addresses

        if not owned_preview and not owned_connector:
            public_addresses(parsed.hostname)
        import webbrowser

        webbrowser.open(url, new=2)
        return True

    def unload(self):
        if self.active or self.agents.busy():
            raise ValueError("Stop the current task before unloading the model.")
        self.model.unload()
        return self.model.status()

    def policy_version(self):
        return self.store.setting('permissions_version', 0)

    def setting(self, key, value):
        if key == "permissions":
            if not self._settled_for_change():
                raise ValueError("Stop the task before changing permissions.")
            revised = validate_permissions(value)
            previous = self.store.setting(key, DEFAULT)
            if any(previous['tools'].get(name, False) and not revised['tools'][name]
                   for name in ('files', 'terminal', 'edits', 'web')):
                for row in self.store.query("SELECT DISTINCT run_id FROM process_jobs WHERE state IN ('admitted','running')"):
                    self.processes.cancel_run(row['run_id'])
            # One transaction: the version can never lag a permission change
            # (a lag would fail closed in tool_authz anyway).
            with self.store.lock, self.store.db as db:
                row = db.execute("SELECT value FROM settings WHERE key='permissions_version'").fetchone()
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                           ('permissions_version', json.dumps((json.loads(row[0]) if row else 0) + 1)))
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(revised)))
            self.emit('settings',{})
            return True
        if key == 'memory_layers':
            from .memory import KINDS
            if not isinstance(value, dict) or set(value) != set(KINDS) or any(type(v) is not bool for v in value.values()):
                raise ValueError('Supply one boolean for each of the six memory layers.')
            self.store.set_setting(key, value)
            self.learning.disable_run(self.active_run, value)
            self.emit('settings', {})  # F10: every window re-reads settings after any change
            return True
        if (
            key == "theme"
            and value in ("system", "light", "dark")
            or key in {"reduced_motion", "memory_enabled", "stealth"}
            and isinstance(value, bool)
        ):
            self.store.set_setting(key, value)
            if key == 'memory_enabled' and not value:
                self.learning.disable_run(self.active_run)
            self.emit('settings', {})  # F10: the pet follows theme and reduced-motion changes made in the workspace
            return True
        if key == 'notifications':
            self.notifications.save_settings(value)
            self.emit('settings', {})
            return True
        if key == companion_settings.KEY:
            # Phase 19 P6: presentation switches only (pointer gaze, reactions, status words, UI guidance offer);
            # exact keys and types, then every window re-reads settings. main.rs keeps it out of the pet's reach.
            self.store.set_setting(key, companion_settings.validate(value))
            self.emit('settings', {})
            return True
        raise ValueError("Setting is not supported.")

    def telemetry_set(self, enabled, consent_version=None):
        """Phase 18 E06: the person's opt-in (with the consent version shown) or opt-out, which deletes the queue."""
        if type(enabled) is not bool:
            raise ValueError('Choose on or off.')
        status = self.telemetry.enable(consent_version) if enabled else self.telemetry.disable()
        self.emit('settings', {})
        return status

    def chat_rename(self, id, title):
        """Phase 24 (V1-125): the person renames a conversation in the main window. Only its title changes (not its
        order, messages or Documents copy name until the next copy pass); a conversation in Recently deleted is
        refused like any other change to it."""
        if not isinstance(title, str) or not ' '.join(title.split()):
            raise ValueError('Enter a name for the conversation.')
        title = ' '.join(title.split())[:200]
        self.chat(id)  # "Conversation not found."
        from .data_mirror import refuse_if_trashed
        refuse_if_trashed(self.store, id)
        self.store.query('UPDATE chats SET title=? WHERE id=?', (title, id))
        return {'id': id, 'title': title}

    def delete_chat(self, id):
        # Match scheduler admission's lock order; retain the chat until all
        # checks have passed, including owned worktree preservation.
        with self.jobs.lock, self.task_lock:
            return self._delete_chat(id)

    def _delete_chat(self, id):
        if self.active:
            raise ValueError("Stop the current task first.")
        if self.agents.busy():
            raise ValueError('Stop delegated workers before deleting their conversation.')
        if self.store.query("SELECT 1 FROM process_jobs WHERE chat_id=? AND state IN ('admitted','running') LIMIT 1", (id,)):
            raise ValueError('Stop owned commands before deleting their conversation.')
        if self.store.query('SELECT 1 FROM process_jobs j WHERE j.chat_id=? AND j.outcome_uncertain=1 '
                'AND NOT EXISTS(SELECT 1 FROM process_inspections i WHERE i.process_id=j.id) LIMIT 1', (id,)):
            # Phase 24 (V1-108, t24-ux-settings): plain words; 'Record an inspection' stays (test_process_reviews pins it).
            raise PolicyError('A command in this conversation stopped before Pebble could see what it changed. '
                              'Record an inspection of it in Details › Activity, then delete the conversation.')
        coding = getattr(self, 'coding', None)  # absent only in partial test hosts
        if coding is not None:
            coding.assert_chat_deletable(id)
        effects = getattr(getattr(self, 'extensions', None), 'effects', None)  # absent only in partial test hosts
        if effects is not None and effects.unresolved_in_chat(id):
            # Phase 24 (V1-108): 'Reconcile the uncertain connector effects' stays (test_phase16_security pins it).
            raise PolicyError('A connector action in this conversation has an unknown result. Reconcile the uncertain '
                              'connector effects in Details › Activity (say whether it happened), then delete the conversation.')
        # V1-064 (Phase 24): every subsystem forgets first (each forget is idempotent), and the conversation's own rows go
        # last, in one transaction. A failure on the way leaves the conversation listed; deleting it again completes.
        run_ids = [row['id'] for row in self.store.query('SELECT id FROM runs WHERE chat_id=?', (id,))]
        forgotten_processes = self.store.query('SELECT id FROM process_jobs WHERE chat_id=?', (id,))
        self.processes.forget(id)
        observed = getattr(self, '_process_guard_observed', None)
        if observed is not None:
            observed.difference_update(row['id'] for row in forgotten_processes)
        self.store.query('DELETE FROM process_inspections WHERE chat_id=?', (id,))
        self.git_api.forget_chat_records(id)
        if coding is not None:
            coding.forget_chat_records(id)
        self.jobs.forget(id)
        self.agents.forget(id)
        if effects is not None:
            effects.forget_chat(id)
        # Phase 16 research and screen records leave with their chat; Phase 22 reasoning summaries (memory only) too.
        for owner in ('ledger', 'research', 'screens', 'activity'):
            forget = getattr(getattr(self, owner, None), 'forget_chat', None)
            if forget is not None:
                forget(id)
        self.memory.forget_chat(id)  # t24-memory: the person's own "Remember that" records stay, unlinked
        guard = getattr(self, 'guard', None)
        if guard is not None and hasattr(guard, 'forget'):
            guard.forget(run_ids + [id])  # V1-020 (t24-gate P9): raises when a database refuses; the chat stays listed
        storage = getattr(self, 'storage', None)  # absent only in partial test hosts
        copies = storage.forget_chat(id) if storage is not None else None  # V1-016: Pebble's own Documents copies
        self.artifact_inputs.forget(id)
        self.views.revoke(chat_id=id)
        self.mods.forget(id)
        self.artifacts.forget(id)
        exports = getattr(self, 'exports', None)  # absent only in partial test hosts
        if exports is not None:
            exports.forget(id)
        self.images.forget(id)
        self.files.forget(id)
        for run_id in run_ids:
            self.plugin_media.forget(run_id)
        self.runs.forget(id)
        from .store import TRASH_KEY
        with self.store.lock, self.store.db as db:
            for table in ("messages", "message_routes", "actions"):
                db.execute(f"DELETE FROM {table} WHERE chat_id=?", (id,))
            for key in ('model_handoff_chats', TRASH_KEY):
                row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
                value = json.loads(row[0]) if row else None
                if isinstance(value, dict) and id in value:
                    value.pop(id)
                    db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))
            db.execute("DELETE FROM chats WHERE id=?", (id,))
        handoffs = getattr(self, '_handoffs', {})  # absent only in partial test hosts
        for key in [k for k in handoffs if k[0] == id]:
            handoffs.pop(key, None)
        self.sources.pop(id, None)
        checkpoint = getattr(self.store, 'checkpoint', None)
        if callable(checkpoint):
            try:  # merge: best effort, as DataServices._checkpoint; the deletion has already committed
                checkpoint()  # V1-057: the deleted text leaves the write-ahead log too (False while busy)
            except Exception as exc:
                logging.getLogger(__name__).warning('Checkpoint after a deletion failed (%s).', error_kind(exc))
        if storage is not None:
            storage.chat_deleted(id, copies)  # V1-156: the windows clear their quiz drafts of these artifacts
        return True

    def export_discard(self, chat_id, export_id):
        result = self.exports.discard(chat_id, export_id)
        self.storage.forget_export(chat_id, export_id)  # V1-016: its Documents copy goes with it
        return result

    def attach(self, id, path):
        from .attachments import attach
        return attach(self, id, path)

    def attach_bytes(self, id, name, data_base64, upload=None, offset=0, final=True):
        from .attachments import attach_bytes
        return attach_bytes(self, id, name, data_base64, upload=upload, offset=offset, final=final)

    def attach_url(self, id, url):
        if not self.store.setting("permissions", DEFAULT)["tools"]["web"]:
            raise PolicyError("Web access is disabled in Permissions.")
        self.chat(id)
        return self.add_source(id, url, fetch_url(url))

    def add_source(self, id, name, text):
        items = self.sources.setdefault(id, [])
        if len(items) >= 10:
            raise ValueError("Use up to ten sources per message.")
        item = {"id": uuid.uuid4().hex, "name": name, "text": text}
        items.append(item)
        return {"id": item["id"], "name": name, "characters": len(text)}

    def remove_source(self, id, source_id):
        self.sources[id] = [s for s in self.sources.get(id, []) if s["id"] != source_id]
        self.images.remove(id,source_id)
        self.files.remove(id,source_id)
        return True

    def runtime_config(self, chat, deliberate, limits=None, watchdog=None):
        profile = self.model.current() if hasattr(self.model, 'current') else None
        route = {k: profile.get(k) for k in ('id', 'kind', 'url', 'model', 'path', 'context', 'budget', 'effort')} if profile else None
        if route:
            # Opaque pin includes account/connection identity without publishing it.
            route['identity'] = __import__('hashlib').sha256(json.dumps(profile, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        workspace_identity = None
        if chat['workspace']:
            try:
                workspace_identity = list(Workspace(chat['workspace']).identity)
            except (FileNotFoundError, NotADirectoryError):
                pass  # The owned runner records setup failure; it cannot adopt a later replacement.
        config = {
            'workspace': chat['workspace'],
            'workspace_identity': workspace_identity,
            'route': route, 'effort': deliberate,
            'permissions': validate_permissions(self.store.setting('permissions', DEFAULT)),
            'extensions': extension_fingerprint(self.extensions.list()),
            'mods': self.mods.fingerprint(),
            'limits': RunLimits.parse(limits).as_dict(),
            'authz': authz_pin(catalogue_digest=self.tool_catalogue_digest, policy_version=self.policy_version()),
        }
        if watchdog is not None:
            # Phase 22: an explicit stream watchdog budget (recovery.StreamBudget, never above the request timeout).
            # Absent means the default, so every earlier run's config, and its resume check, is unchanged.
            from .recovery import StreamBudget
            config['stream_watchdog'] = StreamBudget.parse(watchdog).as_dict()
        return config

    def run_list(self, id):
        self.chat(id)
        return self.runs.list(id)

    def run_get(self, id, run_id):
        run = self.runs.get(run_id, id)
        # Inspectable host records, without duplicating an entire large prompt.
        return {k: v for k, v in run.items() if k != 'checkpoint'} | {
            # Phase 19 (X19 F1): both windows read this; a quiz call's answers and explanations stay with the host.
            'tools': [{**t, 'arguments': quiz_grading.redact_quiz_arguments(t['name'], t['arguments'])}
                      for t in self.runs.tools(run_id)],
            'plan': self.interactions.plan(run_id),
            'questions': [q for q in self.interactions.questions(id, pending=False) if q['run_id'] == run_id],
            'continuation': self.interactions.continuation(run_id),
            'context': self.runs.context_manifest(run_id),
            'sources': self.provenance.records(run_id),
            'handoff': self.runs.handoff(run_id, manifest_only=True),
            'compactions': self.runs.compactions(run_id),
            'goal': self.goals.snapshot_for_run(run_id),
        }

    def context_evidence(self, rid, seconds=True):
        """Full host packet and remaining budgets for the evidence spine (C03). Content-free on failure."""
        from .compaction import evidence_packet
        from .handoff import HandoffError
        run = self.runs.get(rid)
        budget = {'steps_left': max(0, run['config']['limits']['max_steps'] - run['steps'])}
        if seconds:
            try:
                budget['seconds_left'] = max(0.0, self.time_ledger.remaining(rid))
            except Exception:
                pass  # The spine reports the budget it was given.
        # Read-only: only ask when a tree exists (budget() would create the ledger).
        if self.store.query('SELECT 1 FROM agent_trees WHERE run_id=?', (rid,)):
            try:
                budget['child_capacity'] = self.agents.tree.budget(run['chat_id'], rid)['child_capacity']
            except Exception:
                pass
        questions = [self.interactions.question(r['id']) for r in self.store.query(
            'SELECT id FROM run_questions WHERE run_id=? ORDER BY created,id', (rid,))]
        try:
            packet = evidence_packet(run, self.runs.tools(rid),
                inputs=self.store.query('SELECT * FROM run_inbox WHERE run_id=? ORDER BY sequence', (rid,)),
                plan=self.interactions.plan(rid), questions=questions, sources=self.provenance.records(rid))
        except HandoffError as exc:
            self.runs.note(rid, 'spine_packet_fallback', {'type': type(exc).__name__, 'reason': getattr(exc, 'reason', None)})
            packet = None  # The spine is labelled receipts_only.
        return packet, budget

    def compact_context(self, projection, messages, tools, context, reserve, counter, method):
        from .compaction import CompactionStalled, compact_window
        execution = self.controller.current()
        if not execution:
            return projection
        committed = self._committed_compaction(execution, messages, tools, context, reserve, counter, method)
        if committed is not None:
            return committed
        packet, budget = self.context_evidence(execution.run_id)
        try:
            compacted = compact_window(messages, projection, self.runs.get(execution.run_id),
                self.runs.tools(execution.run_id), tools, context, reserve, counter, method,
                packet=packet, budget=budget)
        except CompactionStalled as exc:
            self.runs.note(execution.run_id, 'compaction_stalled', exc.manifest.get('compaction_guard', {'reason': exc.reason}))
            raise
        if compacted:
            execution.compaction_generation = self.runs.compact_checkpoint(execution.run_id, compacted)
            projection = compacted.projection
            self.emit('progress', {'chat_id': execution.chat_id, 'run_id': execution.run_id,
                'kind': 'done', 'label': 'Archived older context; kept exact instructions and receipt evidence',
                'generation': execution.compaction_generation, 'activity': None})
            try:
                self.hook_event('compaction', data={'generation': execution.compaction_generation})
            except Exception as exc:
                self.runs.note(execution.run_id, 'hook_observer_failed', {'event': 'compaction', 'type': type(exc).__name__})
        execution.projected_messages = projection.messages
        return projection

    def _committed_compaction(self, execution, messages, tools, context, reserve, counter, method):
        """Phase 34 (the Phase 33 trial 1 race): a transport retry regenerates from the same pre-compaction messages
        (recovery.complete_with_retry). Compacting them again would build a new evidence spine with the time left now,
        so a retry in a later second differed from the committed generation and compact_checkpoint failed the run
        ("The model checkpoint changed during compaction"). When these messages are exactly the newest compaction's
        input and the checkpoint is still its output, the retry sends what was committed: the same generation, the
        same archive, no new spine. Anything else compacts as before."""
        from .compaction import fingerprint
        from .context_manifest import ContextBudgetExceeded, assemble_context
        latest = self.runs.latest_compaction(execution.run_id)
        if latest is None or fingerprint(messages) != latest[1].get('before_fingerprint'):
            return None
        snapshot = self.runs.get(execution.run_id).get('checkpoint') or {}
        committed = snapshot.get('messages')
        if not committed or fingerprint(committed) != latest[1].get('after_fingerprint'):
            return None
        try:
            projection = assemble_context(committed, tools, context, reserve, counter=counter, counter_method=method)
        except ContextBudgetExceeded:
            return None
        if projection.manifest['omitted']['messages']:
            return None  # the committed window no longer fits whole: compact as before
        execution.compaction_generation = latest[0]
        execution.projected_messages = projection.messages
        self.runs.note(execution.run_id, 'compaction_reused', {'generation': latest[0]})
        return projection

    def run_context_archive(self, id, run_id, generation):
        self.runs.get(run_id, id)
        return self._without_withheld(quiz_grading.redact_archive(self.runs.context_archive(run_id, generation)),
                                      self._withheld_texts(run_id))  # X20 F20-02

    def search_stats(self, id):
        chat = self.chat(id)
        if not chat['workspace']:
            return None
        try:
            return self.search.describe(Workspace(chat['workspace']))
        except (OSError, PolicyError):
            return None

    def run_compact(self, id, run_id, target_tokens=None):
        from .context_manifest import assemble_context
        from .compaction import compact_window
        with self.task_lock:
            if self.active:
                raise ValueError('Pause the active run before manually compacting its window.')
            run = self.runs.get(run_id, id)
            snapshot = run['checkpoint'] or {}
            settled_text = snapshot.get('phase') == 'tools' and snapshot.get('messages') and snapshot['messages'][-1].get('role') == 'assistant' and not snapshot['messages'][-1].get('tool_calls')
            if snapshot.get('phase') != 'model' and not settled_text:
                raise ValueError('This checkpoint is between tools. Resume it to a model boundary before manual compaction.')
            profile = self.model.current()
            budget = profile.get('budget', profile['context']) if profile and profile['kind'] != 'gguf' else 2800
            target = target_tokens if target_tokens is not None else budget
            if type(target) is not int or not 512 <= target <= budget:
                raise ValueError('Choose an input target from 512 tokens up to this model budget.')
            messages, tools = snapshot['messages'], snapshot['available']
            counter = lambda ms, ts: self.model.token_budget.count(self.model.wire_context(ms), ts)
            method = 'o200k_local_estimate'
            projection = assemble_context(messages, tools, target, counter=counter, counter_method=method)
            packet, evidence_budget = self.context_evidence(run_id, seconds=False)
            compacted = compact_window(messages, projection, run, self.runs.tools(run_id), tools, target, counter=counter,
                                       method=method, packet=packet, budget=evidence_budget)
            if not compacted:
                return {'status': 'already_fits', 'manifest': projection.manifest}
            generation = self.runs.compact_checkpoint(run_id, compacted)
            return {'status': 'compacted', 'generation': generation, 'manifest': compacted.manifest}

    def research_budget(self):
        """The route's context budget, as the runner computes it (4096 for the local lane)."""
        profile = self.model.current() if hasattr(self.model, 'current') else None
        return profile.get('budget', profile['context']) if profile and profile['kind'] != 'gguf' else 4096

    def research_used(self, rid, exclude=None):
        """Parent counts for the research caps (design study §3.6): root and child WebSearch/ReadURL attempts,
        failures included. The ledger alone undercounts searches whose results were all seen before."""
        from .research_store import web_calls
        children = self.store.query("SELECT agent_id, generation, call_id, name, arguments, result FROM agent_receipts "
                                    "WHERE run_id=? AND name IN ('WebSearch','ReadURL')", (rid,))
        for row in children:
            for key in ('arguments', 'result'):
                row[key] = json.loads(row[key]) if row[key] else None
        return web_calls(self.runs.tools(rid) + children, exclude=exclude or (None, None, self.current_tool_call))

    def research_check(self, rid, answer):
        """Phase 16 D01/D02 completion claim check (controller.completion). Content-free summary."""
        prior = self.store.query("SELECT count(*) AS n FROM run_events WHERE run_id=? AND kind='research_check'",
                                 (rid,))[0]['n']
        # Merge (i20): the claims of the draft the last research repair answered (its content-free counts).
        last = self.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='research_check' "
                                "ORDER BY sequence DESC LIMIT 1", (rid,))
        try:
            counts = json.loads(last[0]['payload']).get('counts') if last else None
            claims = sum(n for n in counts.values() if type(n) is int and n > 0) if isinstance(counts, dict) else 0
        except (TypeError, ValueError, AttributeError):
            claims = 0
        return self.research.research_check(rid, answer, budget=self.research_budget(), prior_repairs=prior,
                                            prior_claims=claims,
                                            used=self.research_used(rid, exclude=(None, None, None)), now=time.time())

    def research_progress(self, rid):
        """Phase 20 D02: the coverage note for a planned run above 8K (None otherwise). Labels and counts only."""
        return self.research.research_progress(rid, budget=self.research_budget(),
                                               used=self.research_used(rid, exclude=(None, None, None)),
                                               now=time.time())

    def source_audit(self, rid, ws):
        """C02: bounded, content-free settlement audit of the files this run read (latest view per path)."""
        from .source_audit import OVER_BUDGET, audit, path_id, reads_from_witnesses
        from .policy import digest
        rows = self.store.query('SELECT path,root_dev,root_ino,disk_hash,ordinal FROM run_sources s WHERE run_id=? '
            'AND ordinal=(SELECT max(ordinal) FROM run_sources WHERE run_id=s.run_id AND path=s.path)', (rid,))
        paths = {path_id(rid, (r['root_dev'], r['root_ino']), r['path']): r['path'] for r in rows}

        def rehash(read, remaining):
            name = paths[read['path_id']]
            try:
                if ws.path(name, create=True).stat().st_size > remaining:
                    return OVER_BUDGET  # Not read (W9 O2).
            except FileNotFoundError:
                return None
            data = ws.snapshot(name, missing_ok=True)  # Bounded; a replaced root raises.
            if data is None:
                return None
            return OVER_BUDGET if len(data) > remaining else (digest(data), len(data))
        result = audit(reads_from_witnesses(rows, rid), rehash, root=list(ws.identity))
        self.runs.note(rid, 'source_audit', result)  # IDs, states and counts only.
        return result

    def run_handoff(self, id, run_id, refresh=True):
        """Trusted native inspection; packets are evidence, never execution input."""
        from .handoff import make_handoff, HandoffBudgetExceeded
        if type(refresh) is not bool:
            raise ValueError('refresh must be a boolean.')
        # One store lock prevents tool/inbox/plan mutations halfway through the
        # packet. File checks are bounded but cannot promise future freshness.
        with self.store.lock:
            run = self.runs.get(run_id, id)
            if not refresh:
                return self.runs.handoff(run_id)
            ws = None
            if run['config']['workspace']:
                try:
                    ws = Workspace(run['config']['workspace'])
                except (OSError, PolicyError):
                    pass
                self.provenance.audit(run_id, ws)
            settlement = None
            if ws is not None:
                try:
                    settlement = self.source_audit(run_id, ws)
                except Exception as exc:  # The packet stays valid without the optional audit.
                    self.runs.note(run_id, 'source_audit_failed', {'type': type(exc).__name__})
            questions = [self.interactions.question(row['id']) for row in self.store.query('SELECT id FROM run_questions WHERE run_id=? ORDER BY created,id', (run_id,))]
            try:
                packet = make_handoff(run, self.runs.tools(run_id),
                    inputs=self.store.query('SELECT * FROM run_inbox WHERE run_id=? ORDER BY sequence', (run_id,)),
                    plan=self.interactions.plan(run_id), questions=questions,
                    sources=self.provenance.records(run_id), audit=settlement,
                    goal_record=self.goals.snapshot_for_run(run_id))
            except HandoffBudgetExceeded as exc:
                self.runs.note(run_id, 'handoff_budget_exceeded', exc.manifest)
                raise
            self.runs.save_handoff(run_id, packet)
            return self.runs.handoff(run_id)

    def observe_context(self, manifest):
        execution = self.controller.current()
        if execution:
            self.runs.note(execution.run_id, 'context_projected', {'step': self.runs.get(execution.run_id)['steps'], 'manifest': manifest})
            self.hook_event('context_assembled', data={'context_status': manifest.get('status', 'projected')})
            self.observe_mods('context.summary', self.mod_context_summary(manifest, execution.compaction_generation))

    def hook_events(self, id, run_id=None, limit=100):
        self.chat(id)
        if run_id:
            self.runs.get(run_id, id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Inspect at most 100 hook records.')
        return self.store.query('SELECT a.* FROM extension_hook_audit a JOIN runs r ON r.id=a.run_id WHERE r.chat_id=?' + (' AND a.run_id=?' if run_id else '') + ' ORDER BY a.id DESC LIMIT ?', (id, run_id, limit) if run_id else (id, limit))

    def hook_event(self, event, text='', data=None, run_id=None):
        rid = run_id or self.active_run
        policy = self.task_policy or self.store.setting('permissions', DEFAULT)
        if rid:
            run = self.runs.get(rid)
            if extension_fingerprint(self.extensions.list()) != run['config']['extensions']:
                raise PolicyError('Enabled extensions changed during this run; start a reviewed new run.')
        result = self.extensions.hooks.dispatch(self.extensions.list(), event, text, run_id=rid,
            cancel=self.cancel, data=data, workers_allowed=policy['tools']['terminal'])
        if result['decision'] == 'deny':
            raise PolicyError('Blocked by hook: ' + result['blocked_by'])
        if result['decision'] == 'allow' and rid and event == 'run_start':
            self.observe_mods('run.state', {'state': 'running', 'steps': run['steps'], 'elapsed': run['elapsed']})
        if rid and event == 'after_tool' and (data or {}).get('tool') != 'RenderMod':
            self.observe_mods('tool.result', {'name': data['tool'], 'status': 'completed', 'receipt_id': data['call_id']})
        return result

    WITHHELD_TEXT = '[A draft the host held or withdrew; it was not shown as the reply.]'

    def _withheld_texts(self, run_id):
        """X20 F20-02: this run's held or withdrawn turn-end texts (reply_withheld steps, and the turn-end a checkpoint
        still marks held_reply), which window routes replace. The journal itself keeps them for audit."""
        steps, texts = set(), set()
        for row in self.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='reply_withheld'", (run_id,)):
            try:
                step = json.loads(row['payload']).get('step')
            except (TypeError, ValueError, AttributeError):
                continue
            if type(step) is int:
                steps.add(step)
        if steps:
            for row in self.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='response_committed'",
                                        (run_id,)):
                try:
                    event = json.loads(row['payload'])
                    content = event['response'].get('content') if event.get('step') in steps else None
                except (TypeError, ValueError, AttributeError, KeyError):
                    continue
                if isinstance(content, str) and content:
                    texts.add(content)
        checkpoint = self.runs.get(run_id)['checkpoint'] or {}
        if checkpoint.get('held_reply'):
            last = next((m for m in reversed(checkpoint.get('messages') or [])
                         if isinstance(m, dict) and m.get('role') == 'assistant'), None)
            if last and not last.get('tool_calls') and isinstance(last.get('content'), str) and last['content']:
                texts.add(last['content'])
        return texts

    @classmethod
    def _without_withheld(cls, value, texts):
        """A copy of a window-readable record whose held or withdrawn assistant texts read as one host line."""
        if not texts:
            return value
        if isinstance(value, dict):
            if value.get('role') == 'assistant' and isinstance(value.get('content'), str) and value['content'] in texts:
                value = {**value, 'content': cls.WITHHELD_TEXT}
            return {key: cls._without_withheld(item, texts) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._without_withheld(item, texts) for item in value]
        return value

    def run_events(self, id, run_id, after=0, limit=100):
        self.runs.get(run_id, id)
        texts = self._withheld_texts(run_id)  # X20 F20-02: held and withdrawn drafts are never read back by a window
        # Phase 19 (X19 F1): a committed response carries the model's quiz call; windows read it without the key.
        return [{**e, 'payload': self._without_withheld(quiz_grading.redact_archive(e['payload']), texts)}
                for e in self.runs.events(run_id, after, limit)]

    def run_pause(self, id, run_id=None):
        with self.task_lock:
            if self.active != id or not self.active_run:
                candidates = [r for r in self.runs.list(id) if r['state'] in {'waiting_input', 'waiting_approval'} and (not run_id or r['id'] == run_id)]
                if not candidates:
                    raise ValueError('This conversation has no active or waiting run.')
                run_id = candidates[0]['id']
                self.processes.cancel_run(run_id)
                self.agents.suspend(id, run_id)
                self.runs.transition(run_id, 'paused')
                self.emit('run', {'chat_id': id, 'run_id': run_id, 'state': 'paused'})
                return {'run_id': run_id, 'status': 'paused'}
            if run_id and run_id != self.active_run:
                raise ValueError('This run does not own the active worker.')
            run_id = self.active_run
            self.processes.cancel_run(run_id)
            self.agents.suspend(id, run_id)
            self.pause_requested.set()
            self.cancel.set()
            self.cancel_root_inference(run_id)
        return {'run_id': run_id, 'status': 'pause_requested'}

    def run_resume(self, id, run_id=None, limits=None, automatic=False):
        with self.task_lock:
            if self.closed:
                raise ValueError('The local service is shutting down.')
            if self.active:
                raise ValueError('Stop the active run before resuming another one.')
            chat = self.chat(id)
            from .data_mirror import refuse_if_trashed
            refuse_if_trashed(self.store, id)  # V1-018: restore it first (code trashed_conversation)
            if run_id is None:
                candidates = [r for r in self.runs.list(id) if r['state'] in RESUMABLE]
                if not candidates:
                    raise ValueError('This conversation has no resumable run.')
                run_id = candidates[0]['id']
            run = self.runs.get(run_id, id)
            if automatic and run['state'] not in {'waiting_input', 'waiting_approval'}:
                raise ValueError('This run requires an explicit resume.')
            config = run['config']
            current = self.runtime_config(chat, config['effort'], config['limits'], config.get('stream_watchdog'))
            if config.get('explicit_skill'):
                current['explicit_skill'] = self.extensions.select_skill(config['explicit_skill']['id'])
            # Revoking optional presentation cannot block a pending answer.
            # Newly enabled code still requires a new reviewed run snapshot.
            if all(pin in config.get('mods', []) for pin in current['mods']):
                current['mods'] = config.get('mods', [])
            if current != {**config, 'mods': config.get('mods', [])}:
                raise ValueError('The workspace, model route, permissions, or enabled extensions changed. Restore the original configuration, start a new task, or start a reviewed handoff to the new route.')
            snapshot = self.runs.resume_snapshot(run_id)
            if snapshot.get('messages') and ('available' not in snapshot or not any(m.get('_pebble_checkpoint') for m in snapshot['messages'])):
                raise ValueError('This checkpoint lacks its tool/context manifest. Start a fresh task rather than inventing missing execution state.')
            action_ids = {t['result'].get('action_id') for t in self.runs.tools(run_id) if isinstance(t.get('result'), dict)}
            if any(a['id'] in action_ids and a['status'] in {'pending', 'running'} for a in self.store.actions(id)):
                raise ValueError('Resolve this run\'s pending actions before resuming it.')
            if limits is not None:
                revised = RunLimits.parse(limits).as_dict()
                config = {**config, 'limits': revised}
            if run['steps'] >= config['limits']['max_steps'] or max(run['elapsed'], self.time_ledger.consumed(run['id'])) >= config['limits']['max_seconds']:
                raise ValueError('This run exhausted its budget. Supply larger explicit run limits to resume.')
            if limits is not None:
                self.store.query('UPDATE runs SET config=? WHERE id=?', (json.dumps(config), run_id))
            if self.interactions.required_pending(run_id):
                if automatic or run['state'] == 'waiting_input':
                    raise ValueError('Answer or cancel this run\'s required question first.')
                self.runs.transition(run_id, 'waiting_input')
                self.emit('run', {'chat_id': id, 'run_id': run_id, 'state': 'waiting_input'})
                return {'run_id': run_id, 'status': 'waiting_input'}
            self.runs.transition(run_id, 'queued')
            # Phase 32: the runner tells a resume after a stop or restart from one after a decision (resumed_from).
            self.controller.launch(chat, run['goal'], config['effort'], snapshot.get('sources', []), run_id,
                                   {**snapshot, 'resumed_from': run['state']})
        return {'run_id': run_id, 'status': 'queued'}

    def run_readback(self, id, run_id):
        """Read-only authoritative verdicts for a run's uncertain outcomes; settles nothing."""
        with self.task_lock:
            if self.active:
                raise ValueError('Stop the active run before reading back its outcomes.')
            self.runs.get(run_id, id)
        from .reconcile import reconcile_run
        return {'items': reconcile_run(self, run_id, job_evidence=getattr(self.processes, 'evidence', None)),
                'authorization': 'none'}

    def run_reconcile(self, id, run_id, call_id, effect, note):
        with self.task_lock:
            if self.active:
                raise ValueError('Stop the active run before reconciling an outcome.')
            self.runs.get(run_id, id)
            if effect != 'readback':
                return self.runs.reconcile(run_id, call_id, effect, note)
            # A native request to settle from the host's own fresh readback; the
            # verdict is recomputed here, never taken from the caller.
            from .reconcile import readback
            owned = self.store.query('SELECT action_id FROM run_action_owners WHERE run_id=? AND call_id=?', (run_id, call_id))
            if not owned:
                raise ValueError('Only an owned broker action can settle from readback. Inspect it and report the outcome instead.')
            verdict = readback(self, {'id': owned[0]['action_id'], 'chat_id': id},
                               job_evidence=getattr(self.processes, 'evidence', None))
            return self.runs.settle_readback(run_id, call_id, verdict)

    def run_steer(self, id, text, run_id=None, input_id=None):
        with self.task_lock:
            self.chat(id)
            if run_id is None:
                candidates = [r for r in self.runs.list(id) if r['state'] in {'running', 'queued', 'retry_wait', 'waiting_input', 'waiting_approval', 'paused'}]
                if not candidates:
                    raise ValueError('This conversation has no run to steer.')
                run_id = candidates[0]['id']
            self.runs.get(run_id, id)
            result = self.interactions.steer(run_id, text, input_id)
            self.controller.wake()
        self.controller.dispatch_now(run_id)  # Phase 22 (f22-approval): as approve
        return result

    def view_stage(self, **params):
        raise ValueError('Open a saved artifact revision for a static preview. Executable views use reviewed mods.')

    def artifact_get(self, chat_id, id, revision=None):
        return self.artifact_inputs.decorate(self.artifacts.get(chat_id, id, revision))

    def _view_permission(self):
        if not self.store.setting('permissions', DEFAULT)['tools']['views']:
            raise PolicyError('Interactive views are disabled in Permissions.')

    def artifact_submit(self, chat_id, id, revision, answers):
        self._view_permission()
        result = self.artifact_inputs.submit(chat_id, id, revision, answers)
        self.emit('question', {'chat_id': chat_id, **result, 'question': self.interactions.question(result['question_id'], chat_id)})
        self.emit('artifact', {'chat_id': chat_id, 'id': id, 'revision': revision, 'status': 'answered'})
        self.controller.wake()
        self.controller.dispatch_now(result['run_id'])  # Phase 22 (f22-approval): as approve
        return result

    def artifact_stop(self, chat_id, id, revision):
        result, cancellation = self.artifact_inputs.stop(chat_id, id, revision)
        self.views.revoke(artifact_id=id)
        if cancellation:
            if cancellation['required'] and self.active_run == cancellation['run_id']:
                self.stop(chat_id)
            self.emit('question', {'chat_id': chat_id, **cancellation, 'question': self.interactions.question(cancellation['question_id'], chat_id)})
            self.controller.wake()
            self.controller.dispatch_now(cancellation['run_id'])  # V1-069: as artifact_submit
        self.emit('artifact', {'chat_id': chat_id, **result})
        return self.artifact_inputs.decorate(result)

    def artifact_refresh(self, chat_id, id, revision):
        """Native Refresh of a live dashboard: the decorated current revision (2 s read cache); writes nothing."""
        return self.artifact_inputs.refresh(chat_id, id, revision)

    def quiz_route(self, operation, chat_id, *args):
        """Human quiz attempts (B2): start, save and submit change only the attempt record, never task data.

        No event: the calling renderer reads the reply, and an `artifact` event would move the main canvas.
        """
        self._view_permission()
        return getattr(self.quizzes, operation)(chat_id, *args)

    def quiz_share(self, chat_id, attempt_id, surface=None):
        """The person's Share: main.rs writes `surface` from the calling window; only 'main' writes task input."""
        self._view_permission()
        result = self.quizzes.share(chat_id, attempt_id, surface)
        if result.get('status') == 'shared':
            self.controller.wake()  # the owning run may be waiting for this human input
            self.controller.dispatch_now(result.get('run_id'))  # V1-069: as artifact_submit
        return result

    def view_binding_valid(self, binding):
        if not self.store.setting('permissions', DEFAULT)['tools']['views']:
            return False
        row = self.artifacts.get(binding['chat_id'], binding['artifact_id'])
        if row['revision'] != binding['revision'] or row['data_hash'] != binding['data_hash'] or row['status'] in {'stopped', 'expired'}:
            return False
        if row['owner_run_id']:
            run = self.runs.get(row['owner_run_id'], row['chat_id'])
            if run['state'] not in {'running', 'completed', 'waiting_input', 'waiting_approval'}:
                return False
        return True

    def artifact_view_open(self, chat_id, id, revision):
        self._view_permission()
        row = self.artifacts.get(chat_id, id)
        if type(revision) is not int or row['revision'] != revision or row['kind'] != 'html' or row['status'] != 'review':
            raise ValueError('Open the current active HTML revision for native review.')
        if row['data'].get('script', '').strip():
            raise ValueError('Inline DOM scripts are unavailable. Use a native widget or an explicitly reviewed mod.')
        binding = {'chat_id': chat_id, 'artifact_id': id, 'revision': revision, 'data_hash': row['data_hash']}
        if not self.view_binding_valid(binding):
            raise ValueError('This artifact owner is no longer active. Resume the run before opening it.')
        self.views.revoke(artifact_id=id)
        return {**self.views.stage(row['data']['html'], row['data'].get('css', ''), binding=binding), 'artifact_id': id, 'revision': revision}

    def mod_operation(self, operation, **params):
        with self.task_lock:
            revocation = operation == 'remove' or operation == 'toggle' and params.get('enabled') is False
            if self.closed or self.active and not revocation:
                raise ValueError('Stop the active run before installing or enabling reviewed mods.')
            if not revocation:
                self._view_permission()
            if 'id' in params:
                params['ident'] = params.pop('id')
            return getattr(self.mods, operation)(**params)

    @staticmethod
    def mod_context_summary(manifest, compactions=0):
        def count(value):
            return int(value) if type(value) in (int, float) and 0 <= value <= 100_000_000 else 0
        summary = manifest.get('summary', {})
        return {'tokens': count(manifest.get('input_tokens')), 'budget': count(manifest.get('limits', {}).get('input')),
            'messages': count(manifest.get('messages', {}).get('selected')), 'sources': count(summary.get('source', {}).get('selected_units')),
            'compactions': count(compactions), 'truncated': bool(manifest.get('omitted', {}).get('messages'))}

    def observe_mods(self, event, payload):
        rid = self.active_run
        if not rid or not (self.task_policy or self.store.setting('permissions', DEFAULT))['tools']['views']:
            return
        try:
            run = self.runs.get(rid)
            if run['state'] != 'running' or not run['config'].get('mods'):
                return
            packet = self.mods.dispatch(run['chat_id'], rid, event, payload, cancel=self.cancel)
            for row in packet['artifacts']:
                self.views.revoke(artifact_id=row['id'])
                self.emit('artifact', {'chat_id': run['chat_id'], **row})
        except InterruptedError:
            raise
        except Exception as exc:
            self.runs.note(rid, 'mod_observer_failed', {'event': event, 'type': type(exc).__name__})

    def question_answer(self, id, question_id, revision, answers):
        result = self.interactions.answer(id, question_id, revision, answers)
        self.emit('question', {'chat_id': id, **result, 'question': self.interactions.question(question_id, id)})
        self.controller.wake()
        self.agents.wake()
        self.controller.dispatch_now(result['run_id'])  # Phase 22 (f22-approval): as approve
        return result

    def question_cancel(self, id, question_id, revision):
        result = self.interactions.cancel_question(id, question_id, revision)
        child = self.agents.question_metadata(question_id).get('requester_agent_id')
        if child:
            self.agents.cancel(id, result['run_id'], result['run_id'], child, origin='human')
        if result['required'] and self.active_run == result['run_id']:
            self.stop(id)
        self.emit('question', {'chat_id': id, **result, 'question': self.interactions.question(question_id, id)})
        self.controller.wake()
        self.agents.wake()
        self.controller.dispatch_now(result['run_id'])  # V1-069: as question_answer
        return result

    def send(self, id, text, deliberate=None, source_ids=None, effort=None, limits=None, watchdog=None):
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 100000:
            raise ValueError("Enter a message under 100 KB.")
        from .data_mirror import refuse_if_trashed
        refuse_if_trashed(self.store, id)  # V1-018: Recently deleted takes no new messages (code trashed_conversation)
        explicit_skill = None
        submitted_text = text
        if re.match(r'^/skill(?:\s|$)', text.strip(), re.I):
            selection = re.match(r'''^/skill\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s+([\s\S]+)$''', text.strip(), re.I)
            if not selection:
                raise ValueError('Use /skill "name or ID" followed by the task.')
            explicit_skill = self.extensions.select_skill(next(p for p in selection.groups()[:3] if p))
            text = selection[4]
        elif text.strip().startswith('/'):
            if source_ids:
                raise ValueError('Runtime commands do not accept attached sources.')
            answer = runtime_command(self, id, text)
            from .model_handoff import DEVICE
            from .runner import COMMAND_ID_PREFIX  # V1-091: host transcripts stay out of model history and constraints
            mid = self.store.message(id, 'user', text.strip(), COMMAND_ID_PREFIX + uuid.uuid4().hex, route_class=DEVICE)
            reply = self.store.message(id, 'assistant', answer, COMMAND_ID_PREFIX + uuid.uuid4().hex, route_class=DEVICE)
            self.emit('complete', {'chat_id': id, 'id': reply, 'seconds': 0})
            return {'message_id': mid, 'command': True}
        with self.task_lock:
            if self.active:
                if explicit_skill:
                    raise ValueError('Select a skill for a new task after the active run settles.')
                if self.active == id and self.active_run:
                    if source_ids:
                        raise ValueError('Follow-up input during a run accepts text only. Attach sources to a new task.')
                    result = self.interactions.steer(self.active_run, text)
                    mid = self.store.message(id, 'user', text.strip(), route_class=self.message_route(self.active_run))
                    return {'message_id': mid, **result, 'steering': True}
                raise ValueError("One task can run at a time on this device.")
            chat = self.chat(id)
            if hasattr(self.model, 'current') and self.model.current():
                from .capabilities import choose
                deliberate = choose(self.model.current(), effort if effort is not None else deliberate)
            selected_sources=[s for s in self.sources.get(id,[]) if s['id'] in (source_ids or [])]
            if any(s.get('kind')=='image' for s in selected_sources):
                from .images import vision
                if not vision(self.model.current()):
                    raise ValueError('Choose a vision-capable online model to send images. This local text-only model cannot view screenshots.')
            config = self.runtime_config(chat, deliberate, limits, watchdog)
            if explicit_skill:
                if not config['permissions']['tools']['files']:
                    raise PolicyError('Skill loading requires file permission.')
                explicit_skill = self.extensions.select_skill(explicit_skill['id'])
                if explicit_skill['id'] == ui_guidance.ID:
                    # Phase 19 P3: `/skill pebble-ui-guidance` pins the guidance, so it needs a window above 4K.
                    profile = self.model.current() if hasattr(self.model, 'current') else None
                    ui_guidance.admit(profile.get('budget', profile['context']) if profile and profile['kind'] != 'gguf' else 4096)
                config['explicit_skill'] = explicit_skill
            try:
                run_id = self.runs.create(id, text.strip(), config, selected_sources)
            except Exception as exc:
                # V1-067: a full disk, or a previous task whose final write is still waiting (F8-1), said in words.
                raise friendly_error(exc) or exc
            self.active, self.active_run = id, run_id
            self.cancel.clear()
            self.pause_requested.clear()
            try:
                mid = self.store.message(id, "user", submitted_text.strip(), route_class=self.message_route(run_id))
                self.images.bind(id,mid,[s['id'] for s in selected_sources if s.get('kind')=='image'])
                self.files.bind(id, mid, [s['id'] for s in selected_sources if s.get('kind') != 'image'])
                for s in self.sources.get(id,[]):
                    if s not in selected_sources:
                        self.images.remove(id,s['id'])
                        self.files.remove(id,s['id'])
                sources = [
                    s for s in self.sources.pop(id, []) if s["id"] in (source_ids or [])
                ]
                self.screens.bind_pending(id, run_id)  # Phase 24 (V1-007): a grant made while no task ran is this run's
                self.controller.launch(chat, text.strip(), deliberate, sources, run_id)
            except Exception as exc:
                shown = friendly_error(exc) or exc
                try:
                    self.runs.transition(run_id, 'failed', error=str(shown))
                except Exception:
                    # V1-067: the database refused this write too (a full disk). The dispatch loop settles the run once
                    # writes succeed again (F8-1), and no task stays active that nothing runs.
                    self.controller.unsettled[run_id] = str(shown)
                    self.controller.wake()
                finally:
                    self.active, self.active_run = None, None
                raise shown
            return {"message_id": mid, 'run_id': run_id}

    def stop(self, id=None, run_id=None):
        with self.task_lock:
            if id and self.active and self.active != id:
                raise ValueError('Another conversation owns the active worker.')
            if not self.active and id:
                waiting = [r for r in self.runs.list(id) if r['state'] in {'waiting_input', 'waiting_approval', 'paused'} and (not run_id or run_id == r['id'])]
                if waiting:
                    self.processes.cancel_run(waiting[0]['id'])
                    self.agents.cancel(id, waiting[0]['id'], waiting[0]['id'], waiting[0]['id'], origin='human')
                    self.runs.transition(waiting[0]['id'], 'stopped')
                    self.emit('run', {'chat_id': id, 'run_id': waiting[0]['id'], 'state': 'stopped'})
                    return True
            if run_id and self.active_run != run_id:
                raise ValueError('This run is not active.')
            self.pause_requested.clear()
            # V1-011: the worker and its inference see Stop first; the bookkeeping writes follow, so a slow or locked
            # database never delays it.
            self.cancel.set()
            self.cancel_root_inference(self.active_run)
            if self.active_run:
                self.processes.cancel_run(self.active_run)
                self.agents.cancel(self.active, self.active_run, self.active_run, self.active_run, origin='human')
        return True

    def cancel_root_inference(self, run_id):
        profile = self.model.current() or {}
        if profile.get('kind') == 'gguf':
            # A root waiting for the local lane cannot interrupt its current child owner.
            self.agents.lane.cancel_local('root:' + str(run_id), self.model.cancel)
        else:
            self.model.cancel()

    def work(self, chat, text, deliberate, sources, run_id=None, resume=None):
        from .data_mirror import route_view
        from .runner import run
        # V1-002: a cloud request never carries this conversation's on-device turns, or the results of the actions an
        # on-device run took; they reach a cloud route only through the reviewed handoff (C10). Here, where every caller
        # meets (the windows, scheduled jobs, resumes, handoff starts, internal callers), before the request is built.
        route = self.runs.get(run_id)['config'].get('route') if run_id else (
            self.model.current() if hasattr(self.model, 'current') else None)
        view = route_view(self.store, chat['id'], chat.get('messages') or [], route)
        if view['withheld'] or view['actions']:
            chat = {**chat, 'messages': view['messages'], 'withheld_actions': view['actions']}
            if run_id and resume is None:
                self.note_withheld(chat['id'], run_id, view, route)
        return run(self, chat, text, deliberate, sources, run_id, resume,
                   app_inventory=installed_apps, weather_lookup=weather)

    def note_withheld(self, cid, run_id, view, route):
        """V1-002: one plain notice in the run, with what the window needs to open the reviewed handoff (C10) that can
        release the withheld turns: the on-device run it starts from and the selected profile. Content-free."""
        from .data_mirror import withheld_notice
        profiles = self.model.profiles() if hasattr(self.model, 'profiles') else []
        profile = next((p for p in profiles if p.get('id') == (route or {}).get('id')), None)
        label = withheld_notice((profile or {}).get('name') or str(view['destination']).split('://', 1)[-1])
        handoff = {'source_run_id': view['source_run_id'], 'profile_id': (route or {}).get('id'),
                   'withheld': len(view['withheld']), 'actions': len(view['actions'])}
        self.runs.note(run_id, 'route_history_withheld', {**handoff, 'destination_class': view['destination'], 'label': label})
        self.emit('progress', {'chat_id': cid, 'run_id': run_id, 'label': label, 'kind': 'note', 'activity': 'note',
                               'handoff': handoff})

    def save_usage(self, cid, mid, usage, emit=True):
        self.store.query(
            "INSERT OR REPLACE INTO metrics VALUES(?,?,?)",
            (mid, cid, json.dumps(usage)),
        )
        if emit:
            self.emit("usage", {"chat_id": cid, "id": mid, "usage": usage})

    def finish(self, cid, mid, answer, usage, started, route="model"):
        usage = {
            **usage,
            "route": route,
            "seconds": round(time.monotonic() - started, 1),
        }
        self.store.message(cid, "assistant", answer, mid, route_class=self.message_route(self.active_run))
        # Phase 22: a model reply's usage event carries numbers the provider reported (usage['reported']); when it
        # reported none, the event is not sent (the stored record keeps its unknown fields as before). A host route
        # (memory, weather, app) still says it used no model tokens.
        self.save_usage(cid, mid, usage, emit=route != "model" or bool(usage.get("reported")))
        self.emit("complete", {"chat_id": cid, "id": mid, "seconds": usage["seconds"]})

    @staticmethod
    def validate(name, args, available):
        spec = next(
            (
                t["function"]["parameters"]
                for t in available
                if t["function"]["name"] == name
            ),
            None,
        )
        if not spec or not isinstance(args, dict):
            raise PolicyError("Tool request does not match its allowed schema.")
        if not set(spec["required"]).issubset(args) or not set(args).issubset(spec["properties"]):
            raise PolicyError(App._argument_refusal(name, spec, args))
        for key, value in args.items():
            prop = spec["properties"][key]
            valid = (
                (isinstance(value, str) and len(value) <= 96000)
                if prop["type"] == "string"
                else (
                    type(value) is bool
                    if prop["type"] == "boolean"
                    else type(value) == int and prop.get('minimum', 1) <= value <= prop.get('maximum', 10000)
                )
            )
            if 'enum' in prop and value not in prop['enum']:
                raise PolicyError(f'Tool argument {key} must be one of: '+', '.join(map(str,prop['enum']))+'. Use the required value on your next call.')
            if not valid:
                raise PolicyError(App._argument_refusal(name, spec, args, key))

    @staticmethod
    def _argument_refusal(name, spec, args, key=None):
        """Phase 21 (F21-quiz): name each refused argument and its expected shape, never its value."""
        if key is None:
            def label(k):
                return k if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', k) else json.dumps(k[:64])
            problems = [f'{k} is required' for k in spec['required'] if k not in args]
            problems += [f'{label(k)} is not an argument of {name}' for k in args if k not in spec['properties']][:8]
            return ('Tool request does not match its allowed schema: ' + '; '.join(problems)
                    + '. Its arguments are ' + ', '.join(spec['properties']) + '.')
        prop, value = spec['properties'][key], args[key]
        if prop['type'] == 'string':
            expected = 'a string of at most 96000 characters'
            if isinstance(value, (dict, list)):
                shape = 'object' if isinstance(value, dict) else 'array'
                expected += f', not a JSON {shape}: send the {shape} JSON-encoded as one string'
        elif prop['type'] == 'boolean':
            expected = 'true or false'
        else:
            expected = f"an integer from {prop.get('minimum', 1)} to {prop.get('maximum', 10000)}"
        return f'Tool argument {key} is invalid: it must be {expected}.'

    @staticmethod
    def tool_label(name, args):
        return {
            "Read": "Reading " + args.get("file_path", ""),
            "Glob": "Finding project files",
            "Grep": "Searching project source",
            'SearchFiles': 'Finding relevant source',
            'CodeReferences': 'Following Python definitions and references',
            'RunCode': 'Running a script: ' + str(args.get('purpose', ''))[:120],
            'ResearchPlan': 'Planning research',
            'ResearchStatus': 'Checking research progress',
            'ReadSource': 'Reading a recorded source',
            "Write": "Drafting " + args.get("file_path", ""),
            'Append': 'Extending ' + args.get('file_path', ''),
            'StartPreview': 'Starting a local static preview',
            "Edit": "Preparing an edit to " + args.get("file_path", ""),
            "RunTests": "Requesting isolated tests",
            'RunRecipe': 'Requesting an isolated project recipe',
            "OpenApp": "Preparing to open " + args.get("name", ""),
            "WebSearch": "Searching the web",
            "ReadURL": "Reading a public web source",
            "Weather": "Looking up current weather",
            "RunCommand": "Reviewing terminal command",
            'StartCommand': 'Starting a background command',
            'ReadProcess': 'Reading command output',
            'WaitProcess': 'Waiting for command output',
            'StopProcess': 'Stopping an owned command',
            'RunPlan': 'Proposing a typed command plan',
            'WriteProcessInput': 'Typing into an interactive command',
            "LoadSkill": "Loading skill guidance",
            "SearchPluginTools": "Finding reviewed plugin tools",
            "LoadPluginTool": "Loading a reviewed plugin schema",
            "PluginCall": "Preparing plugin call",
            'AskUserQuestion': 'Preparing a clarification form',
            'UpdatePlan': 'Updating the task plan',
            'UpdateGoal': 'Updating goal progress',
            'ProposeGitChange': 'Preparing a reviewed Git plan',
            'ReadGitPlanReceipt': 'Reading back a Git plan receipt',
            'ListProjectRecipes': 'Finding project test and build recipes',
            'PrepareEnvironment': 'Preparing a dependency install for review',
        }.get(name, name)

    def tool_hook(self, name, args):
        review = (
            self.hook_event('before_tool', name + ' ' + json.dumps(args),
                {'tool': name, 'arguments_hash': __import__('hashlib').sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()})['decision'] == 'review'
        )
        if review and name in {
            "Glob",
            "Grep",
            'SearchFiles',
            'CodeReferences',
            "Read",
            'ReadGitStatus', 'ReadGitDiff', 'ReadGitHistory',
            'ReadGitWorktrees', 'PreviewWorktreeExport', 'ReadWorktreeExport',
            'ReadGitPlanReceipt', 'ListProjectRecipes',
            "ReadAttachment",
            "LoadSkill",
            "SearchPluginTools",
            "LoadPluginTool",
            "SearchPluginData","LoadPluginData","ReadPluginData","ReadPluginMedia",
            "WebSearch",
            "ReadURL",
            "Weather",
        }:
            raise PolicyError(
                "A hook requires review for this read. This preview cannot stage read approvals, so the tool was stopped. Adjust the rule or perform the read yourself."
            )
        return review

    def authorize(self, name, args, actor, rid=None, admitted=None, call_id=None, layers=None):
        """R05 layers for one call: PolicyError on refusal, else the Decision.

        The actor is host state. A native call outside a run is checked against a
        transient pin of the current catalogue and policy (schema, route and
        policy still apply). The receipt is journaled before the effect.
        """
        if actor is None:
            raise PolicyError('This tool call has no host actor; nothing ran.')
        current, version = self.store.setting('permissions', DEFAULT), self.policy_version()
        run_config = self.runs.get(rid)['config'] if rid else {
            'permissions': current, 'authz': authz_pin(catalogue_digest=self.tool_catalogue_digest, policy_version=version)}
        decision = authorize_tool(tool=name, args=args, actor=actor, run_config=run_config,
            catalogue_digest=self.tool_catalogue_digest, policy_version=version, policy=current,
            route=self.plugin_route(rid), admitted=admitted,
            **({'layers': layers} if layers is not None else {}))
        if rid:
            self.runs.note(rid, 'tool_authorized' if decision.allowed else 'tool_refused',
                {'call_id': call_id or self.current_tool_call, **decision.receipt()})
        return decision.require()

    def plugin_route(self, rid):
        """tool_authz resolver over the reviewed registry and this run's loaded routes."""
        def resolve(request):
            pid, kind, target, digest = (request[k] for k in ('plugin_id', 'kind', 'target', 'digest'))
            if kind == 'tool':
                item = self.extensions.checked_plugin(pid, digest)     # PolicyError when changed or unreviewed
                if request['via'] == 'PluginCall' and target not in item['config']['tools']:
                    return None
                if not any(s['name'] == target for s in item['config']['schemas']):
                    return None
                reviewed = item['config']['catalogue_digest']
                rows = self.store.query('SELECT 1 FROM extension_tool_routes WHERE run_id=? AND plugin_id=? '
                    'AND tool=? AND digest=?', (rid, pid, target, digest)) if rid else []
            else:
                item = self.extensions.content.checked(pid, digest)    # PolicyError when changed or unreviewed
                if not any(s.get('uri' if kind == 'resource' else 'name') == target
                           for s in item['config'].get('content_catalogue', {}).get(kind, [])):
                    return None
                reviewed = item['config']['content_digest']
                rows = self.store.query('SELECT 1 FROM extension_data_routes WHERE run_id=? AND plugin_id=? '
                    'AND kind=? AND identifier=? AND digest=?', (rid, pid, kind, target, digest)) if rid else []
            return {'plugin_id': pid, 'kind': kind, 'target': target, 'digest': reviewed,
                    'reviewed': True, 'loaded': bool(rows)}
        return resolve

    def guard_session(self, cid):
        return self.active_run if self.active == cid and self.active_run else cid

    def execute(self, name, args, ws, cid, call_id=None, actor=None):
        rid = self.active_run if self.active == cid else None
        if rid and self.cancel.is_set():
            raise InterruptedError('Stopped by you.')
        call_id = call_id or self.current_tool_call or 'host-' + uuid.uuid4().hex
        if rid:
            with self.store.lock:
                self.agents.tool_budget(rid, call_id)
                cached = self.runs.begin_tool(rid, call_id, name, args)
            if cached is not None:
                return cached
        previous_call = self.current_tool_call
        self.current_tool_call = call_id
        try:
            # Independent R05 layers on every call. A refusal raises PolicyError into the
            # existing path below: a completed {'error'} receipt, never an uncertain effect.
            # The strict canonical-schema layer runs where model arguments enter
            # (runner and child admission); host paths keep their own checks.
            # Plugin route identity is verified for run-owned calls; run-less host
            # calls keep the deeper plugin checks they always had.
            self.authorize(name, args, actor or (Actor.root(rid) if rid else Actor.native()), rid, call_id=call_id,
                           layers=('catalogue', 'route', 'actor', 'policy') if rid else ('catalogue', 'actor', 'policy'))
            result = self._execute(name, args, ws, cid)
        except Exception as exc:
            if rid and not any(t['call_id'] == call_id and t['state'] == 'completed' for t in self.runs.tools(rid)):
                self.runs.end_tool(rid, call_id, {'error': str(exc)},
                    # Phase 37: a script that failed mid-way may have made edits (each in its own receipt), so its
                    # own receipt is uncertain too; RunCode stays out of EFFECTS, which names effects by tool.
                    uncertain=(name in EFFECTS or name == 'RunCode') and not isinstance(exc, (PolicyError, HookInterrupted)))
            raise
        finally:
            self.current_tool_call = previous_call
        if rid:
            if isinstance(result, dict):
                result = {**result, 'receipt_id': call_id}
            # T7 F11: an act whose outcome is unknown keeps its result, is journaled uncertain and stops the
            # run before another model step; native readback settles it (needs_reconciliation).
            uncertain = name == 'ScreenAct' and isinstance(result, dict) and result.get('needs_reconciliation') is True
            self.runs.end_tool(rid, call_id, result, uncertain=uncertain)
            if uncertain:
                raise RuntimeError('The screen act has an uncertain outcome. Check the window before resuming.')
            try:
                self.hook_event('after_tool', data={'tool': name, 'call_id': call_id, 'state': 'completed'})
            except Exception as exc:
                self.runs.note(rid, 'hook_observer_failed', {'event': 'after_tool', 'type': type(exc).__name__})
        return result

    def _execute(self, name, args, ws, cid):
        from .agent_manager import ROOT as AGENT_TOOLS
        if name in AGENT_TOOLS:
            if not self.active_run or self.active != cid:
                raise PolicyError('Agent controls require an owning root run.')
            if self.tool_hook(name, args):
                raise PolicyError('A hook requires review for this agent operation.')
            try:
                return self.agents.tool(cid, self.active_run, self.active_run, name, args, self.current_tool_call)
            except ValueError as exc:
                raise PolicyError(str(exc)) from exc
        if name in CORE_TOOLS:
            if not self.active_run or self.active != cid:
                raise PolicyError('This runtime tool requires an owning run.')
            if self.tool_hook(name, args):
                raise PolicyError('A hook requires review for this runtime input.')
            if name == 'AskUserQuestion':
                question = self.interactions.create_question(self.active_run, self.current_tool_call,
                    json.loads(args['questions']), args['required'])
                self.emit('question', {'chat_id': cid, 'run_id': self.active_run, 'question_id': question['id'], 'status': 'pending', 'question': question})
                return {'question_id': question['id'], 'revision': question['revision'], 'required': question['required'], 'status': 'awaiting_user_input'}
            if name == 'ReadRunEvidence':
                from .compaction import archived_results, evidence_packet, read_archived_page
                profile = self.model.current()
                budget = profile.get('budget', profile['context']) if profile and profile['kind'] != 'gguf' else 4096
                ident = args.get('receipt_id')
                match = re.fullmatch(r'(input|page):([A-Za-z0-9_.:-]{1,128}?)(?:@(\d{1,6}))?', ident) if isinstance(ident, str) else None
                if match:
                    # C03/C04 through the unchanged schema: receipt_id "input:<id>" pages an exact earlier
                    # human turn; "page:<generation>:<index>" an archived result page. "@<offset>" continues.
                    kind, value, start = match[1], match[2], int(match[3] or 0)
                    if start > 200000:
                        raise PolicyError('Use a character offset of at most 200,000.')
                    page = 1000 if budget <= 4096 else 4000
                    try:
                        if kind == 'input':
                            from .constraints import read_input
                            result = read_input([m for m in self.chat(cid)['messages'] if m['role'] == 'user'], value, start, page)
                        else:
                            generation = value.split(':', 1)[0]
                            if not generation.isdigit() or not 1 <= int(generation) <= 256:
                                raise ValueError('Archived result not found in this run.')
                            # Only the named generation is loaded.
                            archives = [{'generation': int(generation), **self.runs.context_archive(self.active_run, int(generation))}]
                            result = {**read_archived_page(archives, value, start, page), 'authorization': 'none'}
                    except ValueError as exc:
                        raise PolicyError(str(exc)) from None
                    if result.get('next_offset') is not None:
                        result['next_receipt_id'] = f"{kind}:{value}@{result['next_offset']}"
                    return result
                offset, limit = args.get('offset', 0), args.get('limit', 5)
                if type(offset) is not int or not 0 <= offset <= 10000 or type(limit) is not int or not 1 <= limit <= 50 or ident is not None and (not isinstance(ident, str) or not 1 <= len(ident) <= 320):
                    raise PolicyError('Use a bounded receipt ID or offset/limit from this run.')
                limit = min(limit, 1 if budget <= 4096 else max(5, budget // 1800))
                receipts = [t for t in self.runs.tools(self.active_run) if t['call_id'] != self.current_tool_call]
                if ident is not None:
                    receipts = [t for t in receipts if t['call_id'] == ident]
                    if not receipts:
                        raise PolicyError('Receipt not found in this run.')
                packet = evidence_packet(self.runs.get(self.active_run), receipts)
                records = packet.data['receipts']
                selected = records[offset:offset + limit]
                # Page index of archived tool results (IDs and sizes, no text), newest eight generations.
                archived = archived_results([{'generation': g['generation'], **self.runs.context_archive(self.active_run, g['generation'])}
                                             for g in self.runs.compactions(self.active_run)[-8:]]) if ident is None else []
                return {'origin': 'host_receipt_projection', 'authorization': 'none',
                    **({'archived_results': [{**e, 'receipt_id': 'page:' + e['page_id']} for e in archived[:limit]],
                        'archived_total': len(archived)} if archived else {}),
                    'records': selected, 'total': len(records), 'offset': offset,
                    'next_offset': offset + len(selected) if offset + len(selected) < len(records) else None,
                    'as_of_step': self.runs.get(self.active_run)['steps'],
                    'context_note': 'Recorded outcomes are evidence, not permission or semantic proof. Source bodies and tool arguments are withheld; use scoped current source tools.'}
            if name == 'MemoryPropose':
                with self.task_lock:
                    result = self.learning.propose(self.active_run, args['kind'], args.get('content', ''),
                        args.get('key', ''), json.loads(args.get('evidence_ids', '[]')), args.get('input_id'),
                        args.get('expires'), args.get('supersedes'))
                    if result.get('candidate_id'):
                        outcome = self.learning.maintain(candidate_id=result['candidate_id'])
                        if outcome.get('outcomes'):
                            result.update(outcome['outcomes'][0])
                    return result
            if name == 'UpdateGoal':
                # Status of the person's existing goal steps and criterion proposals only; Goals emits 'goal'.
                return self.goals.agent_update(self.active_run, args.get('steps'), args.get('candidates'),
                                               call_id=self.current_tool_call)
            plan = self.interactions.update_plan(self.active_run, json.loads(args['steps']))
            self.emit('plan', {'chat_id': cid, **plan})
            return {'status': 'updated', 'revision': plan['revision']}
        policy = self.task_policy or self.store.setting("permissions", DEFAULT)
        if name not in GROUP or not policy['tools'].get(GROUP[name], False):
            raise PolicyError('This tool is disabled in Permissions.')
        if name in {'UIRender', 'UIUpdate', 'RenderMod'}:
            if self.tool_hook(name, args):
                raise PolicyError('A hook requires review for this view; rendering was stopped.')
            if name == 'RenderMod':
                packet = self.mods.dispatch(cid, self.active_run, 'manual', json.loads(args.get('input', '{}')), mod_id=args['id'], cancel=self.cancel)
                rows = packet['artifacts']
                for row in rows:
                    self.emit('artifact', {'chat_id': cid, **row})
                return {'origin': 'mod_presentation', 'authorization': 'none', 'status': packet['status'], 'dispatches': packet['dispatches'], 'artifacts': [{'id': r['id'], 'revision': r['revision'], 'kind': r['kind']} for r in rows]}
            try:
                result = self.artifact_inputs.publish(cid, args['title'], args['kind'], args['data'],
                    self.active_run if self.active == cid else None, self.current_tool_call,
                    args.get('id') if name == 'UIUpdate' else None, args.get('revision'), evidence_argument(args.get('evidence_ids')))
            except ValueError as exc:
                # Input/owner/schema/CAS failures roll back atomically. They
                # permit correction; effect/reporting/commit failures still
                # retain the normal uncertain-effect boundary.
                raise PolicyError(str(exc)) from exc
            self.views.revoke(artifact_id=result['id'])
            self.emit('artifact', {'chat_id': cid, **result})
            if result.get('question_id'):
                question = self.interactions.question(result['question_id'], cid)
                self.emit('question', {'chat_id': cid, 'run_id': self.active_run, 'question_id': question['id'], 'status': 'pending', 'question': question})
            # Phase 19 P6: windows get the full public row; the model and its receipt get identifiers only for a
            # host-graded quiz, a dashboard and a prototype (never the answer key or host counts). Other kinds unchanged.
            from .artifact_inputs import model_result
            return model_result(result)
        policy = self.task_policy or self.store.setting("permissions", DEFAULT)
        if name not in GROUP or not policy["tools"].get(GROUP[name], False):
            raise PolicyError("This tool is disabled in Permissions.")
        hook_review = self.tool_hook(name, args)
        if name == 'RunCode':
            # Phase 37 (1.5.0): each call the script makes goes back through validate, authorize and execute with its
            # own nested receipt (code_mode.run), so the guard, edit_scope and approvals apply to it as to a direct call.
            if not self.active_run or self.active != cid:
                raise PolicyError('RunCode runs only inside its owning root run.')
            if hook_review:
                raise PolicyError('A hook requires review for this script; it did not run.')
            from . import code_mode
            return code_mode.run(self, args, ws, cid, self.current_tool_call, self.step_tools or [])
        if name in {'ReadProcess', 'WaitProcess', 'StopProcess', 'WriteProcessInput'}:
            if not self.active_run or self.active != cid or not policy['tools']['files']:
                raise PolicyError('Process tools require their owning root run and project access.')
            if hook_review:
                raise PolicyError('A hook requires review for this process operation.')
            if name == 'StopProcess':
                return self.process_stop(cid, self.active_run, args['process_id'])
            if name == 'WriteProcessInput':
                return self.model_process_input(cid, args, ws)
            requested_limit = args.get('limit', 4000)
            if type(requested_limit) is not int or not 1 <= requested_limit <= 8000:
                raise PolicyError('Read from 1 to 8000 process output bytes.')
            profile = self.model.current() or {}
            context_budget = profile.get('budget', profile.get('context', 4096)) if profile.get('kind') != 'gguf' else 4096
            return self.process_read(cid, self.active_run, args['process_id'],
                args.get('after', 0), min(requested_limit, 500 if context_budget <= 4096 else 4000),
                args.get('wait_seconds', 2) if name == 'WaitProcess' else 0)
        if name == 'ReadAttachment':
            if hook_review:
                raise PolicyError('A hook requires review for this attachment read.')
            return self.files.read(cid, args['attachment_id'], args.get('offset', 0), args.get('limit', 12000), args.get('representation', 'text'))
        if name == 'StartPreview':
            if hook_review:
                raise PolicyError('A hook requires review for this preview; it was stopped.')
            if not re.search(r'\b(localhost|locally|preview)\b|\brun\b.*\b(website|web|site)\b', self.current_text, re.I):
                raise PolicyError('A local preview requires an explicit request in the current task.')
            return self.previews.start(ws)
        if name == "LoadSkill":
            loaded = {t['arguments'].get('id') for t in self.runs.tools(self.active_run) if t['name'] == 'LoadSkill' and isinstance(t.get('result'), dict) and t['result'].get('load_kind') == 'instructions'} if self.active_run else set()
            if self.active_run:
                explicit = self.runs.get(self.active_run)['config'].get('explicit_skill')
                if explicit:
                    loaded.add(explicit['id'])
            if not args.get('reference') and args['id'] not in loaded and len(loaded) >= 4:
                raise PolicyError('A run loads at most four complete skill bundles.')
            profile = self.model.current()
            budget = profile.get('budget', profile['context']) if profile and profile['kind'] != 'gguf' else 4096
            if args['id'] == ui_guidance.ID:
                ui_guidance.admit(budget, args.get('reference'))  # Phase 19 P3: nothing at 4K; references above 8K
            limit = min(args.get('limit', 3000), 500 if budget <= 4096 else 3000)
            return self.extensions.load_skill(args['id'], args.get('digest'), args.get('reference'), args.get('offset', 0), limit)
        if name == 'SearchPluginTools':
            return self.extensions.search_plugin_tools(args['query'], args.get('limit', 4))
        if name == 'SearchPluginData':
            return self.extensions.content.search(args['query'],args.get('limit',4))
        if name == 'LoadPluginData':
            return self.extensions.content.load(args['plugin_id'],args['kind'],args['identifier'],args['digest'],self.active_run)
        if name == 'FetchPluginData':
            return self.propose(cid,'plugin',self.extensions.content.payload(args['plugin_id'],args['kind'],args['identifier'],args['digest'],args['arguments'],self.active_run))
        if name == 'ReadPluginData':
            if not self.active_run:raise PolicyError('MCP archive reads require an owning run.')
            profile=self.model.current();budget=profile.get('budget',profile['context']) if profile and profile['kind']!='gguf' else 4096
            return self.extensions.content.read(self.active_run,args['snapshot_id'],args.get('offset',0),min(args.get('limit',1000),500 if budget<=4096 else 4000))
        if name == 'ReadPluginMedia':
            if not self.active_run:raise PolicyError('Media reads require an owning run.')
            ids=json.loads(args['snapshot_ids'])
            if not isinstance(ids,list) or not 1<=len(ids)<=4 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):raise PolicyError('Select one to four media snapshot IDs.')
            parts=self.plugin_media.model_content(self.active_run,ids,self.model.current())
            return {'selected_snapshot_ids':ids,'untrusted_media':[p['text'] for p in parts if p['type']=='text'],
                'images_sent':sum(p['type']=='image_url' for p in parts),'authorization':'none','freshness':'historical_snapshot'}
        if name == 'LoadPluginTool':
            return self.extensions.load_plugin_tool(args['plugin_id'], args['tool'], args['digest'], self.active_run)
        if name == "PluginCall":
            # Phase 16 D05: host-side effect classification (send gate, recipients, idempotency) before proposal.
            payload = self.extensions.plugin_payload(
                args["plugin_id"], args["tool"], args["arguments"], self.active_run, args.get("digest"))
            return self.propose(cid, "plugin", self.extensions.classify_payload(payload, run_id=self.active_run))
        if name in {'ResearchPlan', 'ResearchStatus'}:
            # Phase 16 D01/D02: one deterministic plan per run with fixed caps; status re-checks claims.
            from .research import ResearchError
            rid = self.active_run
            if not rid or self.active != cid:
                raise PolicyError('Research tools require an owning root run.')
            budget = self.research_budget()
            try:
                if name == 'ResearchPlan':
                    allowed, admission = 'SpawnAgent' in getattr(self, 'step_tool_names', ()), None
                    if allowed:
                        try:
                            admission = self.agents.tree.admission_preview(cid, rid, 'read_only')
                        except ValueError:
                            allowed = False
                    # Primary hosts come only from human-origin text (the request), never page or model text.
                    return self.research.research_plan(rid, args, budget=budget, today=time.strftime('%Y-%m-%d'),
                                                       human_text=self.current_text, delegation_allowed=allowed,
                                                       admission=admission)
                return self.research.research_status(rid, args, budget=budget, used=self.research_used(rid),
                                                     now=time.time())
            except ResearchError as exc:
                raise PolicyError('Call ResearchPlan first.' if exc.reason == 'no_plan' else str(exc)) from exc
        if name == 'ReadSource':
            rid = self.active_run
            if not rid:
                raise PolicyError('ReadSource reads a page this run already recorded.')
            budget = self.research_budget()
            limit = min(args.get('limit', 3000), 3000 if budget <= 8192 else 12000)
            return self.ledger.page(rid, args['source_id'], args.get('offset', 0), limit, args['expected_sha256'])
        if name in {"WebSearch", "ReadURL", "Weather"}:
            # Phase 16 D01/D02: inside a run, searches and pages go through the source ledger (numbered S<n>
            # sources, hardened fetch, ranked window with explicit omissions) under fixed per-run caps.
            if name == "Weather":
                return self.web_weather(args["city"])
            rid = self.active_run
            if not rid:  # direct execute() outside a run keeps the legacy receipts
                if name == "WebSearch":
                    return self.web_search(args["query"])
                return {"url": args["url"], "retrieved_at": time.time(), "untrusted_source": fetch_url(args["url"])[:6500]}
            from . import research
            from .source_ledger import window_chars
            budget = self.research_budget()
            refusal = self.research.admit(rid, 'search' if name == 'WebSearch' else 'read', budget,
                                          self.research_used(rid), now=time.time())
            if refusal:
                return {'status': 'refused', 'error': research.refusal_text(refusal), 'refusal': refusal,
                        'authorization': 'none'}
            if name == "WebSearch":
                found = self.web_search(args["query"])
                found["untrusted_search_results"] = self.ledger.record_search(rid, args["query"],
                                                                              found["untrusted_search_results"])
                return found
            return self.ledger.read(rid, args["url"], window_chars=window_chars(budget), query_terms=(self.current_text,))
        if name in ('ScreenObserve', 'ScreenAct'):
            # Phase 16 D04: a granted window or tab only; commits wait for native review; nothing is retried.
            policy = self.task_policy or self.store.setting('permissions', DEFAULT)
            if not policy['tools'].get('screen', False):
                raise PolicyError('Screen control is disabled in Permissions.')
            if name == 'ScreenObserve':
                return self.screens.observe(args['session_id'], self.active_run)
            result = self.screens.act(args['session_id'], self.active_run, args['observation_id'], args['element'],
                                      args['action'], args.get('text'), expect=args.get('expect'))
            if result.get('status') == 'needs_native_review':
                return self.propose(cid, 'screen', {**result['proposal'], 'requires_review': True})  # every mode
            if result.get('needs_reconciliation') and self.active_run:
                self.runs.note(self.active_run, 'screen_act_uncertain', {'session_id': args['session_id']})
            return result
        if name == "OpenApp":
            if self.active and not re.search(
                r"\b(open|launch|start)\b", self.current_text, re.I
            ):
                raise PolicyError(
                    "App opening needs an explicit request to open an app. Use a live lookup to answer current-information questions."
                )
            matches = [
                a for a in installed_apps() if a["name"].lower() == args["name"].lower()
            ]
            if len(matches) != 1:
                return {
                    "error": "Choose an exact installed app name. Available: "
                    + ", ".join(a["name"] for a in installed_apps())[:1500]
                }
            return self.propose(
                cid, "app", {**pin_app(matches[0]["id"]), "requires_review": hook_review}
            )
        if not ws:
            raise PolicyError("Select a project folder first.")
        if name in {'Write', 'Append', 'Edit'} and self.active_run:
            self.agents.assert_root_path(cid, self.active_run, args['file_path'])
        guard_read_only = False
        if name in {'RunCommand', 'StartCommand'}:
            try:
                guard_read_only = inspect_command(args["command"], ws.root, args.get("network", False))['risk'] == 'read-only'
            except PolicyError:
                pass  # the broker refuses it below, with its own reason
        decision = self.guard.handle(
            "pre_tool_use",
            ws.root,
            self.guard_session(cid),
            tool="Bash" if name in {'RunCommand', 'StartCommand', 'RunPlan'} else name,
            arguments=({'command': plan_guard_text(args.get('plan'))} if name == 'RunPlan' else args),
            read_only=guard_read_only,
        )
        if (
            decision["action"] not in {"allow", "request_review"}
            or decision["action"] == "request_review"
            and name not in {'RunCommand', 'StartCommand', 'RunPlan'}
        ):
            raise PolicyError(self.guard.refusal(decision))
        if name in {'ReadGitStatus', 'ReadGitDiff', 'ReadGitHistory'}:
            return self.git.tool(cid, name, args, ws, self.cancel)
        if name in {'ReadGitWorktrees', 'PreviewWorktreeExport', 'ReadWorktreeExport', 'IntegrateWorktreeFile'}:
            if hook_review:
                raise PolicyError('The hook requires review for this worktree operation; nothing was staged.')
            return self.git_api.root_tool(cid, name, args, ws, self.cancel)
        from .coding_api import root_tool_names
        if name in root_tool_names():
            return self.coding.root_tool(cid, name, args, ws, hook_review)
        if name == 'RunPlan':
            return self.stage_plan(cid, args, ws, policy, hook_review, decision)
        if name in {'RunCommand', 'StartCommand'}:
            if name == 'StartCommand' and (not self.active_run or self.active != cid):
                raise PolicyError('Asynchronous commands require an owning root run.')
            if not policy["tools"]["files"]:
                raise PolicyError("Terminal commands require project read access.")
            data = inspect_command(args["command"], ws.root, args.get("network", False))
            if name == 'StartCommand':
                timeout = args.get('timeout', 300)
                if type(timeout) is not int or not 1 <= timeout <= 300:
                    raise PolicyError('Choose a command timeout from 1 to 300 seconds.')
                data.update(asynchronous=True, timeout=timeout)
            if data["network"] and not policy["tools"]["web"]:
                raise PolicyError("Network access is disabled in Permissions.")
            data["writable"] = policy["tools"]["edits"] and data["risk"] != "read-only"
            # X03: a typed plan pins argv, executable identity, cwd and resources.
            # Strings a plan cannot represent unambiguously stay legacy and are
            # labelled unplanned; destination-scoped network awaits typed destinations.
            from . import command_plans
            if not data['network']:
                try:
                    plan = command_plans.from_command(data['command'], ws.root, writable=data['writable'],
                                                      asynchronous=name == 'StartCommand')
                except PolicyError:
                    plan = None
                if plan is not None:
                    if name == 'StartCommand' and data['timeout'] < plan['resources']['timeout']:
                        plan = dict(plan, resources=dict(plan['resources'], timeout=data['timeout']))
                        plan['plan_hash'] = command_plans.plan_digest(plan)
                        plan = command_plans.validate(plan)
                    data.update(command_plans.legacy_payload(plan), plan=plan)
            data['planned'] = 'plan' in data
            if data['writable']:
                self.agents.assert_effect_paths(str(ws.root))
            command_hook = self.hook_event('before_command', data['command'], {'tool': 'RunCommand'})['decision']
            data["requires_review"] = (
                data["requires_review"]
                or hook_review
                or command_hook == "review"
                or decision["action"] != "allow"
            )
            if decision["action"] == "request_review":
                data['guard_review'] = self.guard.review_reason(decision)  # V1-141: why, on the approval card
            return self.propose(cid, "command", data)
        if name == "Glob":
            profile = self.model.current()
            budget = profile.get('budget', profile['context']) if profile and profile['kind'] != 'gguf' else 4096
            limit = min(args.get('limit', 100), 10 if budget <= 4096 else 100)
            result = self.search.glob(ws, args['pattern'], scope=(cid, self.active_run if self.active == cid else None),
                cursor=args.get('cursor'), limit=limit, cancel=self.cancel)
        elif name == 'SearchFiles':
            requested_limit = args.get('limit', 10)
            if type(requested_limit) is not int or not 1 <= requested_limit <= 50:
                raise PolicyError('Use a ranked search limit from 1 to 50.')
            mode = args.get('mode', 'files')
            if not isinstance(mode, str) or mode not in {'files', 'symbols'}:
                raise PolicyError('Choose files or symbols for ranked search.')
            profile = self.model.current()
            budget = profile.get('budget', profile['context']) if profile and profile['kind'] != 'gguf' else 4096
            discovery = self.search.symbols if mode == 'symbols' else self.search.search
            result = discovery(ws, args['query'], path=args.get('path'),
                scope=(cid, self.active_run if self.active == cid else None), cursor=args.get('cursor'),
                limit=min(requested_limit, 2 if budget <= 4096 else 10), cancel=self.cancel)
        elif name == 'CodeReferences':
            from . import code_references
            profile = self.model.current()
            budget = profile.get('budget', profile['context']) if profile and profile['kind'] != 'gguf' else 4096
            result = code_references.run(ws, args, scope=(cid, self.active_run if self.active == cid else None),
                budget=budget, cancel=self.cancel)
        elif name == "Read":
            offset = args.get("offset", 1)
            char_offset = args.get('char_offset', 0)
            requested_limit = args.get('limit')
            if type(offset) is not int or offset < 1 or type(char_offset) is not int or not 0 <= char_offset <= 96000 or requested_limit is not None and (type(requested_limit) is not int or requested_limit < 1):
                raise PolicyError('Use a positive line offset and a character offset from 0 to 96000.')
            source_text = ws.read(args["file_path"])
            lines = source_text.splitlines(keepends=True)
            from .policy import digest
            expected_hash = args.get('expected_hash')
            if char_offset and expected_hash is None:
                raise PolicyError('Continue a character page with expected_hash from its first page.')
            if expected_hash is not None and expected_hash != digest(source_text.encode('utf-8')):
                raise PolicyError('The source changed between pages. Read from the beginning for a consistent view.')
            profile = self.model.current() if hasattr(self.model,'current') else None
            large = profile and profile['kind'] != 'gguf' and profile.get('budget', profile['context'])>8192
            limit = min(args.get("limit", 200 if large else 100), 2000 if large else 150)
            selected_text = ''.join(lines[offset - 1:offset - 1 + limit])
            from .context import focused_excerpt
            working_budget = profile.get('budget', profile['context']) if profile and profile['kind'] != 'gguf' else 4096
            excerpt = focused_excerpt(selected_text[char_offset:], 96000 if large else 5000,
                token_limit=320 if working_budget <= 4096 else min(32000, working_budget // 4))
            witness = self.provenance.observe(self.active_run, self.current_tool_call, ws, args['file_path'], source_text) if self.active_run and self.active == cid else None
            result = {
                "path": args["file_path"],
                "start_line": offset,
                "total_lines": len(lines),
                "file_hash": digest(source_text.encode()),
                'untrusted_source': excerpt,
                'char_offset': char_offset,
                'next_char_offset': char_offset + len(excerpt) if char_offset + len(excerpt) < len(selected_text) else None,
                'excerpt_complete': char_offset + len(excerpt) >= len(selected_text),
                'excerpt_note': 'Character paging is relative to the selected line span. Keep offset and limit unchanged when following next_char_offset. Full-view hash is a freshness witness, not proof that the full file was shown.',
                **({'source': witness} if witness else {}),
            }
            # JSON escaping and metadata count toward the page budget too.
            output_limit = min(95000, working_budget - 512) if large else 6000
            if len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) > output_limit:
                low, high = 0, len(excerpt)
                while low < high:
                    end = (low + high + 1) // 2
                    candidate = {**result, 'untrusted_source': excerpt[:end],
                        'next_char_offset': char_offset + end if char_offset + end < len(selected_text) else None,
                        'excerpt_complete': char_offset + end >= len(selected_text)}
                    if len(json.dumps(candidate, ensure_ascii=False, separators=(',', ':'))) <= output_limit:
                        low = end
                    else:
                        high = end - 1
                result.update(untrusted_source=excerpt[:low], next_char_offset=char_offset + low if char_offset + low < len(selected_text) else None,
                    excerpt_complete=char_offset + low >= len(selected_text))
        elif name == "Grep":
            profile = self.model.current()
            budget = profile.get('budget', profile['context']) if profile and profile['kind'] != 'gguf' else 4096
            limit = min(args.get('limit', 30), 2 if budget <= 4096 else 30)
            result = self.search.grep(ws, args['pattern'], scope=(cid, self.active_run if self.active == cid else None),
                cursor=args.get('cursor'), limit=limit, cancel=self.cancel)
            if budget <= 4096:
                from .context import focused_excerpt
                for match in result['untrusted_matches']:
                    original = match['text']
                    start = max(0, match['column'] - match['text_start'] - 8)
                    match['text'] = focused_excerpt(original[start:], 250, token_limit=60)
                    match['text_start'] += start
                    match['text_truncated'] = match['text_truncated'] or start > 0 or len(match['text']) < len(original)
        elif name in ("Write", "Edit", 'Append'):
            path = args["file_path"]
            if self.active_run and self.active == cid:
                self.provenance.require_current_read(self.active_run, ws, path)
            if name == 'Append':
                content = ws.read(path) + args['content']
            elif name == "Edit":
                content = ws.read(path)
                old = args["old_string"]
                if not old or content.count(old) != 1:
                    # V1-084: say whether the text was missing or found several times (the child worktree tool does).
                    found = content.count(old) if old else 0
                    raise PolicyError(
                        "Edit text must match exactly once. Read the file again." if not old else
                        f"Edit text must match exactly once, but it was not found in {path}. Read the file again and "
                        "copy the exact text." if not found else
                        f"Edit text must match exactly once, but it matches {found} places in {path}. Include more of "
                        "the surrounding lines so it matches one place.")
                content = content.replace(old, args["new_string"], 1)
            else:
                content = args["content"]
            data = ws.draft(path, content)
            path = data['path']
            if self.active_run and self.active == cid:
                self.provenance.observe(self.active_run, self.current_tool_call, ws, data['path'], content)
            data["requires_review"] = hook_review
            if self.active_run and self.active == cid and policy['mode'] != 'full':
                # Phase 34 (W1): a test file the request did not ask to change waits for the person, in Auto mode too
                # (Full access is the person's choice to let every supported action run without review).
                from .edit_scope import review_reason
                scope = review_reason(path, self.current_text)
                if scope:
                    data['requires_review'] = True
                    data['scope_review'] = scope
            self.guard.stage(ws.root, self.guard_session(cid), path, content)
            # Replace only this run's earlier draft in this exact project.
            # A same-named foreign/unbound action is not ours to invalidate.
            owning_run = self.active_run if self.active == cid else None
            for action in self.store.actions(cid):
                if (
                    owning_run
                    and action["kind"] == "edit"
                    and action["status"] == "pending"
                    and isinstance(action['payload'], dict)
                    and action['payload'].get('path') == path
                    and action['payload'].get('root') == data['root']
                    and action['payload'].get('root_identity') == data['root_identity']
                    and self.store.query('SELECT run_id FROM run_action_owners WHERE action_id=?',
                                         (action['id'],)) == [{'run_id': owning_run}]
                ):
                    self.store.query(
                        "UPDATE actions SET status='superseded' WHERE id=? AND status='pending'",
                        (action["id"],),
                    )
            result = self.propose(cid, "edit", data)
            if data.get('scope_review') and result.get('status') == 'awaiting_user_approval':
                from .edit_scope import MODEL_NOTE
                result['scope_review'] = MODEL_NOTE  # Phase 34 (W1): why it waits, and the way to check instead
            if result.get('status') == 'applied':
                ws.drafts.pop(path, None)
        elif name in ("RunTests", 'RunRecipe'):
            from .recipes import REGISTRY, bindings, resolve, review_bindings
            plan = resolve(args["recipe"], ws, mount=None)
            # The host, not the model, picks a ready managed environment whose
            # lockfile still matches this project; mount_spec re-verifies it.
            mount = self.environments.mount_for(cid, ws, REGISTRY[plan['recipe']].needs)
            if mount is not None:
                plan = resolve(plan['recipe'], ws, mount=mount)
            check = None
            if name == 'RunTests' and args.get('check') is not None:
                from .testing import check_payload
                check = check_payload(plan['recipe'], args['check'])  # Phase 34 (W1): the isolated copy only
            try:
                # Manifests and lockfiles under review (Phase 20 G20-recipes): learning data only. An unbindable
                # file (a link, or above the 2 MB snapshot limit) leaves the run reviewable but teaches nothing.
                bound = bindings(plan['recipe'], ws)[0]
            except PolicyError:
                bound = None
            result = self.propose(
                cid,
                "tests",
                {
                    "root": str(ws.root),
                    'root_identity': list(ws.identity),
                    "recipe": plan['recipe'],
                    'plan': plan,
                    'mount': mount,
                    "drafts": list(ws.drafts.values()),
                    'bound_files': bound,
                    'reviewed_files': review_bindings(plan['recipe'], ws),  # X20 F20-03: checked per file before launch
                    "requires_review": hook_review,
                    **({'check': check} if check is not None else {}),
                },
            )
        else:
            raise PolicyError("Tool is unavailable.")
        self.guard.handle(
            "post_tool_use",
            ws.root,
            self.guard_session(cid),
            tool=name,
            arguments=args,
            result=json.dumps(result),
        )
        return result

    def propose(self, cid, kind, payload):
        policy = self.task_policy or self.store.setting("permissions", DEFAULT)
        payload = {**payload, "permission_mode": policy["mode"]}
        aid = self.store.action(cid, kind, payload)
        execution = self.controller.current()
        owner = {'run_id': execution.run_id, 'call_id': execution.current_call} if execution and execution.chat_id == cid and execution.current_call else None
        if owner:
            self.runs.bind_action(owner['run_id'], owner['call_id'], aid, kind)
        if automatic(policy, kind) and not payload.get("requires_review"):
            action = next(a for a in self.store.actions(cid) if a["id"] == aid)
            self.store.query("UPDATE actions SET status='running' WHERE id=?", (aid,))
            receipt_committed = False
            try:
                result = self.perform_action(action, cid)
                self.runs.commit_action(aid, payload, result, kind, owner)
                receipt_committed = True
                if owner and kind == 'edit':
                    self.provenance.applied(owner['run_id'], aid, payload, result)
                self.emit(
                    "actions", {"chat_id": cid, "actions": self.store.actions(cid)}
                )
                return {
                    "status": "applied",
                    "action_id": aid,
                    "kind": kind,
                    "result": result,
                }
            except Exception:
                if not receipt_committed:
                    self.store.query(
                        "UPDATE actions SET status='failed' WHERE id=?", (aid,)
                    )
                self.emit(
                    "actions", {"chat_id": cid, "actions": self.store.actions(cid)}
                )
                raise
        self.emit("actions", {"chat_id": cid, "actions": self.store.actions(cid)})
        return {"status": "awaiting_user_approval", "action_id": aid, "kind": kind}

    def open_app(self, id, app_id):
        self.chat(id)
        if not self.store.setting("permissions", DEFAULT)["tools"]["apps"]:
            raise PolicyError("App launches are disabled in Permissions.")
        app = next((a for a in installed_apps() if a["id"] == app_id), None)
        if not app:
            raise PolicyError("Select an installed app.")
        review = self.tool_hook("OpenApp", {"name": app["name"]})
        return self.propose(id, "app", {**pin_app(app["id"]), "requires_review": review})

    def approve(self, id, action_id):
        # Phase 22 (f22-approval): a decision that leaves its run ready returns after the continuation was dispatched
        # (RunController.dispatch_now), once the action lock is released; the decision itself is unchanged.
        decided = {}
        try:
            return self._approve(id, action_id, decided)
        finally:
            self.controller.dispatch_now(decided.get('run_id'))

    def _approve(self, id, action_id, decided):
        with self.task_lock:
            if self.active:
                raise PolicyError(
                    "Wait for the current task to finish before approving an action."
                )
            self.cancel.clear()
            self.pause_requested.clear()
            self.active = id
            self.active_run = None
        with self.action_lock:
            action = next(
                (a for a in self.store.actions(id) if a["id"] == action_id), None
            )
            if not action or action["status"] != "pending":
                with self.task_lock:
                    self.active, self.active_run = None, None
                self.controller.wake()
                raise PolicyError("This action is no longer awaiting approval.")
            owner = None
            receipt_committed = False
            try:
                owner = self.runs.begin_approval(id, action_id)
                self.active_run = owner['run_id'] if owner else None
                self.store.query(
                    "UPDATE actions SET status='running' WHERE id=?", (action_id,)
                )
                self.emit("actions", {"chat_id": id, "actions": self.store.actions(id)})
                result = self.perform_action({**action, '_native_reviewed': True}, id)
                receipt = self.runs.commit_action(action_id, action['payload'], result, action['kind'], owner)
                receipt_committed = True
                if owner:
                    if action['kind'] == 'edit':
                        self.provenance.applied(owner['run_id'], action_id, action['payload'], result)
                    if action['kind'] == 'screen' and isinstance(result, dict) and result.get('needs_reconciliation'):
                        # Dispatched once with an unknown outcome: inspect before resuming, never retry.
                        self.runs.transition(owner['run_id'], 'needs_reconciliation',
                                             error='The approved screen act has an uncertain outcome. Inspect it before resuming.')
                    if self.cancel.is_set():
                        self.runs.transition(owner['run_id'], 'paused' if self.pause_requested.is_set() else 'stopped')
                    self.interactions.action_resolution(id, action_id, receipt)
                    try:
                        self.hook_event('approval_resolved', data={'action_id': action_id, 'kind': action['kind'], 'state': 'applied'})
                    except Exception as exc:
                        self.runs.note(owner['run_id'], 'hook_observer_failed', {'event': 'approval_resolved', 'type': type(exc).__name__})
                self.store.message(
                    id,
                    "assistant",
                    self.action_summary(action["kind"], action["payload"], result),
                    route_class=self.message_route(owner['run_id']) if owner else None,
                )
                self.update_checkpoint(id)
                return result
            except Exception as exc:
                if not receipt_committed:
                    self.store.query(
                        "UPDATE actions SET status='failed' WHERE id=?", (action_id,)
                    )
                if owner and receipt_committed:
                    self.runs.transition(owner['run_id'], 'interrupted', error='The action result is saved, but its follow-up update failed. Resume this run to continue from the committed receipt.')
                elif owner:
                    self.runs.end_tool(owner['run_id'], owner['call_id'],
                        {'action_id': action_id, 'error': str(exc)}, uncertain=not isinstance(exc, (PolicyError, HookInterrupted)))
                    if not isinstance(exc, (PolicyError, HookInterrupted)):
                        self.runs.transition(owner['run_id'], 'needs_reconciliation', error='The approved action has an uncertain outcome. Inspect it before resuming.')
                raise
            finally:
                with self.task_lock:
                    self.active, self.active_run = None, None
                    self.emit("actions", {"chat_id": id, "actions": self.store.actions(id)})
                    self.emit("idle", {"chat_id": id, 'run_id': owner['run_id'] if owner else None})
                if action['kind'] == 'plugin':
                    try:
                        self.extensions.close_run(owner['run_id'] if owner else 'native-review')
                    except Exception as exc:
                        if owner:
                            self.runs.note(owner['run_id'], 'plugin_teardown_failed', {'type': type(exc).__name__})
                        else:
                            self.emit('progress', {'chat_id': id, 'kind': 'review', 'label': 'Plugin teardown needs attention; committed receipts are preserved.', 'activity': None})
                decided['run_id'] = owner['run_id'] if owner else None
                self.controller.wake()

    def perform_action(self, action, cid):
        self.agents.effects.admit_action(cid, action)
        from .coding_api import KIND_GROUP, KIND_TOOL
        group = {
            "edit": "edits",
            "app": "apps",
            "tests": "tests",
            "command": "terminal",
            "plugin": "plugins",
            "screen": "screen",
            **KIND_GROUP,
        }.get(action["kind"])
        policy = self.task_policy or self.store.setting("permissions", DEFAULT)
        if not group or not policy["tools"].get(group, False):
            raise PolicyError("This action is disabled in Permissions.")
        tool = {'edit': 'Write', 'app': 'OpenApp', 'tests': 'RunTests', 'command': 'RunCommand', 'plugin': 'PluginCall',
                'screen': 'ScreenAct', **KIND_TOOL}[action['kind']]
        if action['kind']=='plugin' and action['payload'].get('data_kind'):tool='FetchPluginData'
        admission = self.hook_event('before_tool', tool + ' ' + json.dumps(action['payload'], ensure_ascii=False),
            {'tool': tool, 'arguments_hash': __import__('hashlib').sha256(json.dumps(action['payload'], sort_keys=True).encode()).hexdigest()})
        if admission['decision'] == 'review' and action.get('_native_reviewed') is not True:
            raise PolicyError('The hook requires native review before this effect.')
        if action['kind'] in KIND_GROUP:
            return self.coding.perform(action, cid, policy)
        if action["kind"] == "edit":
            with self.store.lock:
                if self.process_inspections.pending(action['payload']['root']):
                    raise PolicyError('Inspect the prior uncertain command outcome before editing this project.')
                self.agents.assert_effect_paths(action['payload']['root'], action['payload']['path'])
                return Workspace.apply(action["payload"])
        if action["kind"] == "app":
            return launch_app(action["payload"])  # an unpinned (pre-Phase-16) payload is refused
        if action['kind'] == 'screen':
            return self.screens.commit(action['payload']['proposal_id'])
        if action["kind"] == "plugin":
            if action['payload'].get('transport') == 'stdio' and (not policy['tools']['terminal'] or not policy['tools']['files']):
                raise PolicyError('Local MCP terminal/file access was disabled after proposal.')
            if action['payload'].get('data_kind'):
                return self.extensions.content.call(action['payload'],self.active_run,self.cancel)
            return self.extensions.plugin_call(action["payload"], self.active_run, self.cancel, action_id=action['id'])
        if action["kind"] == "command":
            plan = action['payload'].get('plan')
            if plan is not None:
                from . import command_plans
                requested = command_plans.validate(plan)
                if requested['writable'] and not (action['payload'].get('writable') and policy['tools']['edits']):
                    requested = dict(requested, writable=[])  # write access withdrawn after proposal: narrow only
                    requested['plan_hash'] = command_plans.plan_digest(requested)
                plan = command_plans.narrow(plan, requested)
            if action['payload'].get('risk') != 'read-only' and self.process_inspections.pending(action['payload']['root']):
                raise PolicyError('Inspect the prior uncertain command outcome before running another effect in this project.')
            if action['payload'].get('writable') and policy['tools']['edits']:
                self.agents.assert_effect_paths(action['payload']['root'])
            if not policy["tools"]["files"]:
                raise PolicyError(
                    "Project read access was disabled after this command was proposed."
                )
            if action["payload"].get("network") and not policy["tools"]["web"]:
                raise PolicyError(
                    "Network access was disabled after this command was proposed."
                )
            self.hook_event('before_command', action['payload']['command'], {'tool': 'RunCommand'})
            if action['payload'].get('asynchronous') is True:
                if self.process_inspections.pending(action['payload']['root']):
                    raise PolicyError('A prior command in this project has an unknown outcome. Inspect and reconcile it before starting another asynchronous command.')
                run = self.runs.get(self.active_run, cid) if self.active_run else None
                if not run:
                    raise PolicyError('An asynchronous command needs its approved owning run.')
                execution = self.controller.current()
                remaining = (execution.process_deadline - time.monotonic()
                    if execution and execution.process_deadline is not None else
                    run['config']['limits']['max_seconds'] - run['elapsed'])
                if remaining < 1:
                    raise PolicyError('The run has no command execution time remaining.')
                command_payload = {key:value for key,value in action['payload'].items() if key != 'timeout'}
                if plan is not None:
                    command_payload.update(plan=plan, plan_hash=plan['plan_hash'])
                admitted = self.processes.start(action, cid, {**command_payload,
                    'writable': bool(action['payload'].get('writable') and policy['tools']['edits'])},
                    timeout=min(action['payload']['timeout'], remaining))
                return self._await_start(cid, admitted)
            result = run_command(
                {
                    **action["payload"],
                    **({'plan': plan, 'plan_hash': plan['plan_hash']} if plan is not None else {}),
                    "writable": action["payload"].get("writable", False)
                    and policy["tools"]["edits"],
                },
                self.cancel,
            )
            self.guard.handle(
                "post_tool_use",
                action["payload"]["root"],
                self.guard_session(cid),
                tool="Bash",
                arguments={"command": action["payload"]["command"]},
                result=json.dumps(result),  # GuardAdapter decodes it (V1-027); kept a string, as observers expect
                # V1-027: a read-only command skips the post-command sync.
                writable=bool(action["payload"].get("writable") and policy["tools"]["edits"]),
            )
            return result
        with self.store.lock:
            if self.process_inspections.pending(action['payload']['root']):
                raise PolicyError('Inspect the prior uncertain command outcome before running tests in this project.')
            self.agents.assert_effect_paths(action['payload']['root'])
        mount = action['payload'].get('mount')
        if mount is not None:
            # The environment may have changed between proposal and approval.
            fresh = self.environments.mount_spec(cid, mount.get('environment_id'), Workspace(action['payload']['root']))
            if fresh.get('tree_digest') != mount.get('tree_digest') or fresh.get('root') != mount.get('root'):
                raise PolicyError('The managed environment changed after these tests were proposed. Propose them again.')
        from . import diagnostics
        # Phase 20 G20-diag: run_tests offers hash-bound diagnostics only inside this capture; the result is unchanged.
        with diagnostics.capture() as captured:
            result = run_tests(action["payload"], self.cancel)
        if result.get('kind') != 'check':  # Phase 34 (W1): an ad hoc check is never AgentGuard test evidence
            self.guard.test_result(
                action["payload"]["root"], self.guard_session(cid), action["payload"]["recipe"], result
            )
        diagnostics.commit(self.runs, self.active_run, captured, action['id'])  # one run event; never raises
        return result

    def update_checkpoint(self, cid):
        goals = self.store.query(
            "SELECT content FROM messages WHERE chat_id=? AND role='user' ORDER BY created DESC LIMIT 1",
            (cid,),
        )
        if goals:
            # V1-079 (t24-agent D): with a run, memory.checkpoint re-derives the record from that run and ignores the
            # actions, so they are read only for a chat without any run (its legacy record).
            has_run = self.store.query('SELECT 1 FROM runs WHERE chat_id=? LIMIT 1', (cid,))
            self.memory.checkpoint(cid, goals[0]["content"], () if has_run else self.store.actions(cid))

    def process_list(self, id):
        self.chat(id)
        return self.store.query('SELECT p.id,p.run_id,p.state,p.outcome_uncertain, '
            'EXISTS(SELECT 1 FROM process_inspections i WHERE i.process_id=p.id) AS inspection_recorded '
            'FROM process_jobs p WHERE p.chat_id=? ORDER BY p.rowid DESC LIMIT 128', (id,))

    def process_dismiss(self, id, run_id, process_id):
        self.runs.get(run_id, id)
        rows = self.store.query('SELECT outcome_uncertain FROM process_jobs WHERE id=? AND run_id=? AND chat_id=?', (process_id, run_id, id))
        if rows and rows[0]['outcome_uncertain'] and not self.store.query('SELECT 1 FROM process_inspections WHERE process_id=?', (process_id,)):
            raise PolicyError('Record an inspection before dismissing an uncertain outcome.')
        result = self.processes.dismiss(id, run_id, process_id)
        self.store.query('DELETE FROM process_inspections WHERE process_id=?', (process_id,))
        observed = getattr(self, '_process_guard_observed', None)
        if observed is not None:
            observed.discard(process_id)
        return result

    def process_read(self, id, run_id, process_id, after=0, limit=4000, wait_seconds=0):
        self.runs.get(run_id, id)
        policy = self.store.setting('permissions', DEFAULT)
        if not policy['tools']['files'] or not policy['tools']['terminal']:
            raise PolicyError('Process output requires project and terminal access.')
        result = self.processes.read(id, run_id, process_id, after, limit, wait_seconds)
        if result['state'] not in {'admitted', 'running'} and process_id not in self._process_guard_observed:
            rows = self.store.query('SELECT root,command FROM process_jobs WHERE id=? AND run_id=? AND chat_id=?', (process_id, run_id, id))
            if rows:
                try:
                    observed = {key: result[key] for key in ('process_id', 'state', 'exit_code', 'outcome_uncertain', 'output_complete')}
                    if result.get('output_complete') and not result.get('more') and isinstance(result.get('untrusted_process_output'), str):
                        observed['stdout'] = result['untrusted_process_output']  # read by Pebble's parser only; AgentGuard gets a host summary
                    # A JSON string, as before (GuardAdapter decodes it; test_process_api and test_process_reviews read it).
                    self.guard.handle('post_tool_use', rows[0]['root'], run_id, tool='Bash',
                        arguments={'command': rows[0]['command']}, result=json.dumps(observed))
                    self._process_guard_observed.add(process_id)
                except Exception as exc:
                    self.runs.note(run_id, 'process_guard_observer_failed', {'type': type(exc).__name__})
        return result

    def process_stop(self, id, run_id, process_id):
        self.runs.get(run_id, id)
        return self.processes.stop(id, run_id, process_id)

    def _await_start(self, cid, admitted, seconds=15.0):
        """After native approval, return once the command has actually started (or settled), so the
        run continues with a running process, as the Phase 12 contract expects. Bounded: a slow start
        still returns the admitted job. Holds no lock while waiting."""
        process_id = admitted.get('process_id') if isinstance(admitted, dict) else None
        if not process_id or admitted.get('state') != 'admitted':
            return admitted
        # Only the supervised launch (shim, sandbox, anchor) has start latency worth waiting for.
        rows = self.store.query('SELECT jobdir FROM process_jobs WHERE id=? AND chat_id=?', (process_id, cid))
        if not rows or rows[0]['jobdir'] is None:
            return admitted
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            rows = self.store.query('SELECT state FROM process_jobs WHERE id=? AND chat_id=?', (process_id, cid))
            if not rows or rows[0]['state'] != 'admitted':
                break
            time.sleep(0.05)
        rows = self.store.query('SELECT state FROM process_jobs WHERE id=? AND chat_id=?', (process_id, cid))
        return {**admitted, 'state': rows[0]['state']} if rows else admitted

    def process_terminal(self, id, run_id, process_id):
        """Native: whether a live interactive job is waiting with its terminal echo on or off."""
        self.runs.get(run_id, id)
        return self.processes.terminal(id, run_id, process_id)

    def process_input_human(self, id, run_id, process_id, text=None, key=None):
        """Native main-window keyboard input for an interactive job; journaled by length and hash only."""
        self.runs.get(run_id, id)
        policy = self.store.setting('permissions', DEFAULT)
        if not policy['tools']['terminal']:
            raise PolicyError('Terminal access is disabled in Permissions.')
        return self.processes.write_input(id, run_id, process_id, text=text, key=key, origin='human')

    def model_process_input(self, cid, args, ws):
        """WriteProcessInput: the root's own interactive job only; text is screened like a command."""
        text, key = args.get('text'), args.get('key')
        if (text is None) == (key is None):
            raise PolicyError('Send either text or one key.')
        if text is not None:
            if ws is None:
                raise PolicyError('Select a project folder first.')
            # V1-053: a stateless screen of typed input; a retry never lifts it (the engine's per-session state did).
            refusal = self.guard.screen_input(text)
            if refusal:
                raise PolicyError('AgentGuard: ' + refusal)
        return self.processes.write_input(cid, self.active_run, args['process_id'], text=text, key=key, origin='model')

    def stage_plan(self, cid, args, ws, policy, hook_review, decision):
        """RunPlan: a typed plan (pipelines, destinations, credentials, terminal) always waits for native review."""
        if not self.active_run or self.active != cid:
            raise PolicyError('Typed plans run in the background and need their owning root run.')
        if not policy['tools']['files']:
            raise PolicyError('Typed plans require project read access.')
        try:
            spec = json.loads(args['plan'])
        except (TypeError, ValueError):
            raise PolicyError('plan must be JSON: {"steps": [{"argv": [...]}], ...}.') from None
        if not isinstance(spec, dict) or not isinstance(spec.get('steps'), list):
            raise PolicyError('plan must be a JSON object with a steps list.')
        from . import command_plans
        from .process_jobs import binding
        fields = {key: value for key, value in spec.items() if key != 'steps'}
        plan = command_plans.from_argv(spec['steps'], ws.root, **fields)
        if plan['network']['mode'] != 'none' and not policy['tools']['web']:
            raise PolicyError('Network access is disabled in Permissions.')
        if plan['writable'] and not policy['tools']['edits']:
            raise PolicyError('Project edits are disabled in Permissions, so a plan cannot request writable paths.')
        missing = [c['ref'] for c in plan['network']['credentials'] if c['ref'] not in self.plan_credentials()['names']]
        if missing:
            raise PolicyError('Add these command credentials in Settings first: ' + ', '.join(missing[:8]) + '.')
        data = {**binding(plan), 'plan': plan, 'planned': True, 'typed': True, 'asynchronous': True,
                'timeout': plan['resources']['timeout'], 'requires_review': True}
        if plan['writable']:
            self.agents.assert_effect_paths(str(ws.root))
        self.hook_event('before_command', data['command'], {'tool': 'RunPlan'})
        return self.propose(cid, 'command', data)

    # Typed-plan credentials: names in settings, values only in the OS vault.

    def plan_credentials(self):
        names = self.store.setting('plan_credentials', [])
        return {'names': sorted(n for n in names if isinstance(n, str)) if isinstance(names, list) else [],
                'authorization': 'none'}

    def plan_credential_save(self, name, secret):
        from .command_plans import REF
        if not isinstance(name, str) or not REF.fullmatch(name) or not isinstance(secret, str):
            raise ValueError('Name the credential with letters, digits, dot, dash, underscore or colon.')
        PlanVault().set('plan:' + name, secret)
        names = set(self.plan_credentials()['names']) | {name}
        self.store.set_setting('plan_credentials', sorted(names))
        return self.plan_credentials()

    def plan_credential_delete(self, name):
        names = set(self.plan_credentials()['names'])
        if name not in names:
            raise ValueError('No such command credential.')
        PlanVault().delete('plan:' + name)
        self.store.set_setting('plan_credentials', sorted(names - {name}))
        return self.plan_credentials()

    def plan_credential(self, ref):
        """fetch(ref) -> bytearray for a reviewed plan's credential reference. Worker threads call it; never model input."""
        if not isinstance(ref, str) or ref not in self.plan_credentials()['names']:
            raise PolicyError('This credential reference is not a saved command credential.')
        value = PlanVault().get('plan:' + ref)
        if not value:
            raise PolicyError('This command credential is not in the vault.')
        return bytearray(value, 'utf-8')

    @staticmethod
    def action_summary(kind, payload, result):
        if kind in ('git', 'worktree', 'install'):
            from .coding_api import CodingAPI
            return CodingAPI.describe(kind, payload, result)
        if kind == "app":
            return describe_launch(result)
        if kind == 'screen':
            from .screen_session import describe as describe_screen
            return describe_screen(result, payload.get('review') if isinstance(payload, dict) else None)
        if kind == "edit":
            # Phase 20 X10: a rewind write says what happened; the receipt is an ordinary reviewed edit.
            if isinstance(payload, dict) and isinstance(payload.get('rewind'), dict):
                step = payload['rewind'].get('step')
                return ("Rewound `" + result["applied"] + "` to how it was "
                        + ("before that task" if step == 0 else "after step " + str(step) + " of that task")
                        + ". Commands and other actions were not undone.")
            return (
                "Applied the edit to `"
                + result["applied"]
                + "`. You can undo it in Activity & review."
            )
        if kind == "command":
            if result.get('process_id'):
                return 'Started `' + payload['command'] + '`. Process ' + result['process_id'] + ' is ' + result['state'] + '; inspect its output and terminal outcome before claiming completion.'
            return (
                "Ran `"
                + payload["command"]
                + "` with exit code "
                + str(result["exit_code"])
                + ".\n\n```text\n"
                + result.get("output", "")[:4000].replace("```", "` ` `")
                + "\n```"
            )
        if kind == "plugin":
            from .connector_effects import summary as effect_summary
            described = effect_summary(payload, result)
            if described:
                return described
            return (
                "**"
                + payload["name"]
                + " / "
                + payload["tool"]
                + "** returned the following untrusted result:\n\n```text\n"
                + result.get("untrusted_plugin_output", "")[:5000].replace(
                    "```", "` ` `"
                )
                + "\n```"
            )
        return (
            "The isolated test run finished with exit code "
            + str(result["exit_code"])
            + ". Review the output in Activity & review."
        )

    def _unstage_draft(self, cid, action_id, payload):
        """Phase 24 (V1-028, t24-gate P7): a rejected or undone edit leaves AgentGuard's mirror before the next check.
        Never fails the person's decision: the controller reconciles the same drafts at the next completion check."""
        try:
            owners = self.store.query('SELECT run_id FROM run_action_owners WHERE action_id=?', (action_id,))
            self.guard.unstage(payload['root'], owners[0]['run_id'] if owners else cid, payload['path'])
        except Exception as exc:
            logging.getLogger(__name__).warning('AgentGuard unstage failed (%s); the next check reconciles it.',
                                                error_kind(exc))

    def reject(self, id, action_id):
        with self.action_lock:
            action = next((a for a in self.store.actions(id) if a['id'] == action_id), None)
            if not action or action['status'] != 'pending':
                raise PolicyError('This action is no longer awaiting approval.')
            self.store.query(
                "UPDATE actions SET status='rejected' WHERE id=? AND chat_id=? AND status='pending'",
                (action_id, id),
            )
            if action['kind'] == 'edit' and isinstance(action['payload'], dict) and action['payload'].get('path'):
                self._unstage_draft(id, action_id, action['payload'])  # V1-028: the rejected draft leaves the mirror
            owner = self.interactions.action_resolution(id, action_id, {'action_id': action_id, 'kind': action['kind'], 'status': 'rejected'})
            if action['kind'] == 'screen' and isinstance(action['payload'], dict):
                self.screens.reject(action['payload'].get('proposal_id'))
            if action['kind'] == 'worktree' and isinstance(action['payload'], dict) \
                    and action['payload'].get('operation') == 'allocate_child_worktree':
                # The queued child is then stopped by its dispatcher gate.
                self.child_worktrees.reject(action['payload']['worktree_id'], 'Declined in native review.')
                self.agents.wake()
            self.emit("actions", {"chat_id": id, "actions": self.store.actions(id)})
            self.controller.wake()
        self.controller.dispatch_now(owner)  # Phase 22 (f22-approval): as approve
        return True

    def undo(self, id, action_id):
        """Native-only local undo of one applied file edit.

        The reverse write is a fresh effect: it re-checks root identity, the
        applied hash, pending uncertain commands, child leases and writable
        processes, then reads the file back and records the receipt on the
        original action. It never undoes an effect outside this file."""
        from .policy import digest
        with self.task_lock:
            if self.active:
                raise PolicyError('Wait for the current task to finish before undoing an edit.')
        with self.action_lock:
            action = next(
                (a for a in self.store.actions(id) if a["id"] == action_id), None
            )
            if not action or action["kind"] != "edit" or action["status"] != "applied":
                raise PolicyError("Only applied file edits can be undone.")
            payload = action["payload"]
            if not isinstance(payload.get('result'), dict) or not isinstance(payload['result'].get('hash'), str):
                raise PolicyError('This edit has no applied hash receipt; undo is blocked.')
            reverse = {
                **payload,
                "baseline": payload["result"]["hash"],
                "after": payload["before"],
            }
            ws = Workspace(payload["root"])
            if payload.get('root_identity') is not None and payload['root_identity'] != list(ws.identity):
                raise PolicyError('The project folder was replaced after this edit; undo is blocked.')
            with self.store.lock:
                if self.process_inspections.pending(payload['root']):
                    raise PolicyError('Inspect the prior uncertain command outcome before undoing an edit in this project.')
                self.agents.assert_effect_paths(payload['root'], payload['path'])
            if payload["baseline"] is None:
                path = ws.path(payload["path"])
                if digest(ws.snapshot(payload['path'])) != reverse["baseline"]:
                    raise PolicyError("File changed after approval; undo is blocked.")
                path.unlink()
                readback = ws.snapshot(payload['path'], missing_ok=True)
                restored = None
            else:
                Workspace.apply(reverse)
                readback = ws.snapshot(payload['path'], missing_ok=True)
                restored = digest(payload['before'].encode('utf-8'))
            observed = digest(readback) if readback is not None else None
            receipt = {'undone_at': time.time(), 'expected_hash': restored, 'readback_hash': observed,
                       'verified': observed == restored, 'scope': 'local file only'}
            self.store.query(
                "UPDATE actions SET status='undone',payload=? WHERE id=? AND status='applied'",
                (json.dumps({**payload, 'undo': receipt}), action_id),
            )
            if action['kind'] == 'edit' and isinstance(action['payload'], dict) and action['payload'].get('path'):
                self._unstage_draft(id, action_id, action['payload'])  # V1-028: the undone edit leaves the mirror too
            owner = self.store.query('SELECT run_id FROM run_action_owners WHERE action_id=?', (action_id,))
            if owner:
                self.runs.note(owner[0]['run_id'], 'action_undone', {'action_id': action_id, 'path': payload['path'],
                                                                     'verified': receipt['verified']})
            self.emit("actions", {"chat_id": id, "actions": self.store.actions(id)})
            if not receipt['verified']:
                raise PolicyError('Undo wrote the file, but readback did not match the original content. Inspect it.')
            return True

    def close(self):
        self.closed = True
        stop = getattr(self, '_idle_stop', None)
        if stop is not None:
            stop.set()  # V1-170: the idle loop ends now, not up to 5 s later
        storage = getattr(self, 'storage', None)
        if storage is not None:
            storage.stop()  # Phase 22: no Documents copy or settings.json pass starts while Pebble closes
        self.cancel.set()
        bridge = getattr(self, 'browser_bridge', None)
        if bridge is not None:
            try:
                bridge.close()  # Phase 22: Chrome lets go of every shared tab when Pebble quits
            except Exception:
                pass
        if not self.processes.close():
            return
        if not self.jobs.close():
            return
        self.git.close()
        self.environments.close()
        if not self.agents.close():
            return
        self.packages.close()
        self.connector_auth.close()
        browser = getattr(self, 'browser', None)
        if browser is not None:
            try:
                browser.stop()  # closing the pipe also makes the browser exit
            except Exception:
                pass
        if not self.controller.close():
            return
        try:
            self.chatgpt.flush()  # Phase 24 (t24-auth): a renewed sign-in the vault refused earlier is saved now if it can be
        except Exception:
            pass
        self.chatgpt.cancel()
        self.previews.close()
        self.views.close()
        self.cancel.set()
        self.model.cancel()
        self.model.unload()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=5)
            if self.worker.is_alive():
                return
        if self.active:
            # Approval RPCs may own an in-flight effect outside the model thread.
            # Keep the DB and cross-process lease until that owner exits.
            return
        self.extensions.close()
        self.guard.close()
        self.runs.close()
        self.store.close()
