"""Phase 19 source/frozen JSON-RPC proof: companion presence and intelligent UI.

Drives the *actual* service over its JSON-RPC stdio contract, launched from the main.rs environment
allowlist, with one owned loopback SSE provider and an owned temporary home outside the checkout.
Every model turn is scripted synthetic output; every quiz attempt, share, refresh, form answer and
setting is the native route a person (or the native shell) uses. No real model, external network,
account, vault or user data is used.

Scenarios (each tagged with an opaque marker, in its own conversation):

  s1  Static shell checks on src-tauri/src/main.rs: generated windows are refused before any route
      list and the shell-only routes are unchanged; both windows reach the Phase 19 routes while
      approval, stop, inspection and quiz sharing stay in the main window; the shell writes the share
      surface from the calling window; pet_gaze is a pet-only command that emits a quantized vector.
  c1  The model catalogue is the Phase 18 catalogue (no new tool); no Phase 19 route is a tool or
      reachable by a model actor; the six memory layers are exact.
  x1  A 4,096-token route asking for a dashboard, quiz and prototype: no new tool, kind or schema byte,
      and no UI guidance.
  x2  Above 4,096 tokens the dashboard kind (and the guidance menu) appear for a dashboard request
      (x2) and not for an ordinary table request (x3).
  v1  A scripted prototype with a link is refused with nothing stored; a prototype "Approve payment"
      button is stored as navigation data and creates no action, question or input.
  q1  A keyed quiz: its key never reaches a view, event, attempt or window-readable run record;
      forged, stale, cross-chat and malformed attempts are refused; grading is on the host and a
      replay changes nothing; only the main window's Share writes one human input to the owning run.
  d1  A bound dashboard shows counts only for its own chat with a stamp; a refresh never crosses
      chats or revisions and keeps the owner and the view.
  r1  A required form: stale, cross-chat and forged answers are refused and the question stays
      pending; the person's answer resumes the run.
  a1  Progress events carry the host activity class and never a path; the companion setting is
      validated and announced to every window.

Each check is recorded independently under a planned unique name with content-free evidence;
source and frozen runs record the same names. A check that needs a missing parent patch or track
module (named in integration_gaps) is recorded as pending, never as passed, and the report then does
not pass. backend_inputs_stable_during_campaign re-hashes the checkout, and in frozen mode the
executable too.

Usage:
  python scripts/validate_phase19.py --report PATH [--backend PATH] [--keep-scratch]

--report must be a new file. --backend is an explicit frozen executable owned by the current user;
the default runs backend/main.py from this checkout.

This is fixture evidence on this machine. It is not live-model quality, view quality, the native
shell or webviews themselves (the validator reads main.rs and stands in for the shell on its
routes), installed or signed desktop behaviour, pointer sampling, frame or CPU cost, other-platform
behaviour or an independent human security audit.
"""

import argparse
import contextlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT
from validate_phase13 import Reply, after_goal, bounded, calls, final, goal_index, history, owned_backend, sha
from validate_phase17 import Phase17Service as Phase19Service

MAIN_RS = ROOT / 'src-tauri/src/main.rs'
MARK = '\N{SECTION SIGN}'
MODEL_4K, MODEL_WIDE = 'phase19-4k-fixture', 'phase19-wide-fixture'
MODELS = frozenset({MODEL_4K, MODEL_WIDE})
PROFILES = {'4k': (MODEL_4K, 4096), 'wide': (MODEL_WIDE, 32768)}
TRANSIENT = frozenset({'queued', 'running', 'retry_wait'})
SETTLE_SECONDS = 60
VERIFIER_SECONDS = 240
LIMITS = {'max_steps': 12, 'max_seconds': 120}
TOKEN = os.urandom(4).hex()
CANARY = 'phase19-canary-' + TOKEN
GUIDANCE_ID = 'pebble-ui-guidance'

# The Phase 18 tested catalogue and its view schema bytes (pinned by a self-check against the checkout).
PHASE18_TOOLS = frozenset((
    'AnswerAgentQuestion', 'Append', 'AskParentQuestion', 'AskUserQuestion', 'CodeReferences', 'DelegateAgentTask',
    'DismissAgentEffect', 'Edit', 'ExecuteAgentEffect', 'FetchPluginData', 'Glob', 'Grep', 'IntegrateAgentPatch',
    'IntegrateWorktreeFile', 'ListProjectRecipes', 'LoadPluginData', 'LoadPluginTool', 'LoadSkill', 'MemoryPropose',
    'OpenApp', 'PluginCall', 'PrepareEnvironment', 'PreviewWorktreeExport', 'PromoteAgentQuestion',
    'ProposeGitChange', 'ProposePatch', 'Read', 'ReadAgentEffect', 'ReadAgentEffects', 'ReadAgentMessage',
    'ReadAgentPatch', 'ReadAgentTree', 'ReadAttachment', 'ReadGitDiff', 'ReadGitHistory', 'ReadGitPlanReceipt',
    'ReadGitStatus', 'ReadGitWorktrees', 'ReadPluginData', 'ReadPluginMedia', 'ReadProcess', 'ReadRunEvidence',
    'ReadSource', 'ReadURL', 'ReadWorktreeExport', 'RenderMod', 'RequestAgentEffect', 'ResearchPlan',
    'ResearchStatus', 'RetryAgent', 'ReviewAgentResult', 'RunCommand', 'RunPlan', 'RunRecipe', 'RunTests',
    'ScreenAct', 'ScreenObserve', 'SearchFiles', 'SearchPluginData', 'SearchPluginTools', 'SendAgentMessage',
    'SpawnAgent', 'SpawnWorktreeAgent', 'StartCommand', 'StartPreview', 'StopAgent', 'StopProcess', 'UIRender',
    'UIUpdate', 'UpdateGoal', 'UpdatePlan', 'WaitForAgents', 'WaitProcess', 'Weather', 'WebSearch', 'Write',
    'WriteProcessInput'))
# D72 (Phase 37): model tools added after Phase 18, each named with its phase. The whole-catalogue checks compare the
# current catalogue with PHASE18_TOOLS plus these, so any other addition still fails; PHASE18_TOOLS itself is unchanged,
# and the 4K offers are still checked against it alone.
LATER_TOOLS = frozenset({'RunCode'})  # Phase 37, 1.5.0: Pebble's Workshop, Code Mode
UI_SCHEMA_BYTES_18 = {'UIRender': 1108, 'UIUpdate': 702}
LEGACY_KINDS = ('quiz', 'diagram', 'html', 'form', 'table', 'chart', 'plan', 'evidence')
LAYERS = ('working', 'episodic', 'semantic', 'procedural', 'temporal', 'profile')
# Shell-only routes of the Phase 18 shell; Phase 19 adds none.
NATIVE_ONLY_18 = frozenset({'lifecycle_event', 'lifecycle_quit_summary', 'notification_native_pending',
                            'notification_native_delivered', 'export_destination_bind', 'export_native_ready',
                            'export_pdf_attach', 'export_pdf_failed', 'telemetry_destination_bind'})
# Assumed Phase 19 routes (Phase 17 study section 3.6 and the routes the B3 renderers call); sharing is main-only.
PHASE19_ROUTES = ('artifact_refresh', 'quiz_attempt', 'quiz_start', 'quiz_save', 'quiz_submit', 'quiz_share')
PET_PHASE19_ROUTES = ('artifact_refresh', 'quiz_attempt', 'quiz_start', 'quiz_save', 'quiz_submit')
MAIN_ONLY = ('approve', 'reject', 'artifact_stop', 'process_inspect', 'process_input_human', 'run_reconcile', 'quiz_share')
# What the shell adds to quiz_share for the main window (parent patch; the validator stands in for the shell).
SHELL_SHARE = {'surface': 'main'}
BINDING_SOURCES = frozenset({'runs.recent', 'runs.usage', 'jobs.fires', 'agents.tree', 'artifacts.revisions',
                             'memory.stats', 'git.summary', 'goals.progress'})
# Merge (i19): design study §2.5's four switches; ui_guidance (patch P3) is the fourth.
COMPANION = {'pointer': True, 'reactions': False, 'status_words': 'fixed', 'ui_guidance': True}

GOALS = {
    'x1': 'Build a dashboard view, a quiz and an app demo prototype for the release plan.',
    'x2': 'Build a dashboard view of this chat\'s recent runs.',
    'x3': 'Render a table view of the release plan.',
    'v1': 'Render an interactive prototype view of the checkout flow.',
    'q1': 'Render a practice quiz view about plants and planets, then a form view to ask how it went.',
    'd1': 'Render a dashboard view of this chat and a table view of the plan.',
    'r1': 'Render a form view to ask which scope to use, then continue.',
    'a1': 'Read the notes file and summarise it.',
}
SCRIPTS = {'x1': (), 'x2': (), 'x3': (), 'v1': (('render', 'hostile_prototype'), ('render', 'prototype')),
           'q1': (('render', 'quiz'), ('render', 'form')), 'd1': (('render', 'dashboard'), ('render', 'table')),
           'r1': (('render', 'form'),), 'a1': (('read', 'notes'),)}
FINALS = {'q1': 'The practice quiz is ready. FINAL: quiz saved'}
DEFAULT_FINAL = 'Synthetic fixture: the scripted steps finished; host receipts record each outcome.'
NOTES = 'notes-' + CANARY + '.md'
PROTOTYPE = {'viewport': 'phone', 'start': 'cart', 'theme': {'accent': 'teal', 'density': 'cozy', 'radius': 8},
             'screens': [{'id': 'cart', 'title': 'Cart', 'components': [
                 {'type': 'heading', 'text': 'Your cart'}, {'type': 'button', 'label': 'Approve payment', 'goto': 'done'}]},
                 {'id': 'done', 'title': 'Done', 'components': [{'type': 'card', 'title': 'Paid', 'body': 'Thanks.'}]}]}
HOSTILE_PROTOTYPE = json.loads(json.dumps(PROTOTYPE))
HOSTILE_PROTOTYPE['screens'][0]['components'][1]['href'] = 'https://evil.example/approve'
QUIZ = {'duration_minutes': 10, 'questions': [
    {'question': 'Which gas do plants absorb?', 'choices': ['Oxygen', 'Carbon dioxide'], 'answer': 1,
     'explanation': 'Plants take in carbon dioxide. ' + CANARY},
    {'question': 'Which planet is largest?', 'choices': ['Jupiter', 'Mars', 'Venus'], 'answer': 0,
     'explanation': 'Jupiter is the largest planet. ' + CANARY}]}
DASHBOARD = {'columns': 2, 'panels': [
    {'id': 'runs', 'title': 'Runs', 'kind': 'table', 'binding': {'source': 'runs.recent', 'params': {'limit': 10}}},
    {'id': 'note', 'title': 'Target', 'kind': 'metric', 'data': {'value': 3, 'unit': 'runs', 'caption': 'Weekly target'}}]}
TABLE = {'columns': [{'id': 'step', 'label': 'Step'}], 'rows': [{'step': 'Ship'}]}
FORM = {'fields': [{'id': 'scope', 'label': 'Which scope?', 'type': 'choice', 'required': True,
                    'options': [{'id': 'small', 'label': 'Small'}, {'id': 'large', 'label': 'Large'}]}]}

GAPS = {
    'routes': 'Parent patch: the Phase 19 native routes (' + ', '.join(PHASE19_ROUTES) + ') in app.py dispatch, '
              'tool_authz.NATIVE_ONLY and main.rs ALLOWED; the pet list gains all but quiz_share.',
    'migration': 'Parent patch: migrations.py epoch 9 creates the Phase 19 quiz and binding tables.',
    'catalogue': 'Parent patch: tool_catalogue.py and runner.py offer the dashboard and prototype kinds above 4096 only '
                 'for their requests (artifact_schema.offered_kinds).',
    'guidance': 'Parent patch P3: runner.py lists the UI guidance above 4096 for layout requests (ui_guidance.offer).',
    'activity': 'Parent patch P1: runner.progress adds activity=activity_kinds.classify(tool, kind).',
    'companion': 'Parent patch P6: app.py setting validates companion (companion_settings.validate), stores it, '
                 'reports it in state() and emits settings.',
    'gaze': 'Parent patch P8: main.rs pet_gaze.',
    'surface': 'Parent patch: main.rs rpc writes the calling window into quiz_share (never from the webview).',
    'redaction': 'Parent patch: app.py run_get applies quiz_grading.redact_quiz_arguments to tool arguments, and any '
                 'committed assistant message passes quiz_grading.redact_response (F1).',
}

PLANNED = (
    'fixture_scratch_created_outside_the_checkout',
    's1_generated_views_are_refused_before_any_route_list_and_shell_only_routes_are_unchanged',
    's1_both_windows_reach_the_phase19_routes_and_approval_stop_and_sharing_stay_in_the_main_window',
    's1_the_share_surface_comes_from_the_calling_window_never_from_the_webview',
    's1_pet_gaze_is_a_pet_only_shell_command_that_emits_a_quantized_vector',
    'c1_no_model_tool_is_added_and_no_phase19_route_is_a_tool_or_reachable_by_a_model_actor',
    'c1_the_six_memory_layers_are_exact',
    'loopback_profiles_saved_and_view_permissions_on',
    'x1_at_4096_tokens_view_tools_add_no_tool_kind_schema_byte_or_guidance',
    'x2_above_4096_new_kinds_and_guidance_are_offered_only_for_layout_requests',
    'v1_a_prototype_with_a_link_is_refused_and_nothing_is_stored',
    'v1_a_prototype_approve_button_is_stored_as_navigation_data_and_creates_no_action_question_or_input',
    'q1_the_quiz_key_never_reaches_a_view_an_event_an_attempt_or_a_window_readable_run_record',
    'q1_forged_stale_cross_chat_and_malformed_attempts_are_refused',
    'q1_grading_is_on_the_host_and_a_replayed_submission_changes_nothing',
    'q1_only_the_main_window_share_writes_one_human_input_to_the_owning_run',
    'd1_bound_panels_show_counts_only_for_their_own_chat_with_a_stamp',
    'd1_a_refresh_never_crosses_chats_or_revisions_and_keeps_the_owner_and_the_view',
    'r1_stale_cross_chat_and_forged_form_answers_are_refused_and_the_question_stays_pending',
    'r1_the_persons_form_answer_resumes_the_run',
    'a1_progress_events_carry_the_host_activity_class_and_never_a_path',
    'a1_the_companion_setting_is_validated_stored_and_announced',
    'native_phase19_routes_are_never_offered_to_models',
    'synthetic_provider_protocol_clean',
    'backend_inputs_stable_during_campaign',
)


class Pending(AssertionError):
    """A check waiting for a named parent patch or track module; never a pass."""

    def __init__(self, gap, detail=''):
        super().__init__(GAPS[gap] + (' ' + detail if detail else ''))
        self.gap = gap


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def tool_name(spec):
    function = spec.get('function') if isinstance(spec, dict) else None
    name = function.get('name') if isinstance(function, dict) else None
    return name if isinstance(name, str) else None


def shell(text):
    """What main.rs declares: route lists, the rpc() body and the command handlers."""
    def block(pattern, what):
        found = re.search(pattern, text, re.S)
        if not found:
            raise AssertionError('main.rs no longer has ' + what + '.')
        return found.group(1)
    strings = lambda chunk: re.findall(r'"([a-z_]+)"', chunk)
    start, end = text.find('async fn rpc('), text.find('fn compact(')
    if start < 0 or end < start:
        raise AssertionError('main.rs no longer has the rpc command.')
    gaze = text.find('fn pet_gaze(')
    body = text[gaze:text.index('\n}\n', gaze) + 2] if gaze >= 0 else ''
    return {'native_only': strings(block(r'const NATIVE_ONLY: &\[&str\] = &\[(.*?)\];', 'the NATIVE_ONLY list')),
            'allowed': strings(block(r'const ALLOWED: &\[&str\] = &\[(.*?)\];', 'the ALLOWED list')),
            'pet': strings(block(r'window\.label\(\)=="pet" && !\[(.*?)\]\.contains\(&method\.as_str\(\)\)', 'the pet list')),
            'rpc': text[start:end], 'handlers': re.findall(r'\w+', block(r'generate_handler!\[(.*?)\]', 'the handlers')),
            'gaze': body}


class Provider:
    """Owned loopback SSE provider. It records the offered tools and view schemas and scripts each scenario
    from the host results the run can see."""

    def __init__(self):
        self.lock = threading.RLock()
        self.records, self.errors, self.anomalies = [], [], []
        self.observed, self.emitted = {}, {}
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    reply = provider.respond(body)
                except Exception as exc:
                    with provider.lock:
                        provider.errors.append(bounded(exc))
                    with contextlib.suppress(Exception):
                        self.send_error(500)
                    return
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': reply.delta}]}).encode() + b'\n\n')
                    self.wfile.write(b'data: ' + json.dumps({'choices': [], 'usage': reply.usage}).encode() + b'\n\n')
                    self.wfile.write(b'data: [DONE]\n\n')
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def anomaly(self, tag, step, reason):
        with self.lock:
            self.anomalies.append({'scenario': tag, 'step': step, 'reason': reason[:300]})

    def matching(self, **match):
        with self.lock:
            return [dict(r) for r in self.records if all(r.get(k) == v for k, v in match.items())]

    def seen(self, tag, ident):
        with self.lock:
            return self.observed.get((tag, ident))

    def respond(self, body):
        model = body.get('model')
        if model not in MODELS:
            raise AssertionError('Unexpected model route reached the fixture.')
        raw = json.dumps(body, ensure_ascii=False)
        if '_pebble_' in raw:
            raise AssertionError('Host-private message fields reached the provider.')
        messages = body.get('messages') or []
        tools = [t for t in body.get('tools') or [] if tool_name(t)]
        index = goal_index(messages)
        found = re.search(MARK + '([a-z][0-9])', messages[index]['content']) if index is not None else None
        tag = found.group(1) if found else None
        step = sum(1 for m in after_goal(messages) if m.get('role') == 'assistant')
        views = {}
        for spec in tools:
            if tool_name(spec) in ('UIRender', 'UIUpdate'):
                kind = (spec['function'].get('parameters', {}).get('properties', {}).get('kind') or {})
                views[tool_name(spec)] = {'bytes': len(compact(spec).encode()), 'kinds': list(kind.get('enum') or [])}
        record = {'scenario': tag, 'model': model, 'step': step, 'tools': sorted(tool_name(t) for t in tools),
                  'views': views, 'guidance': GUIDANCE_ID in compact(messages), 'time': time.time()}
        with self.lock:
            self.records.append(record)
            self.observed.update({(tag, d['id']): d['result'] for d in history(messages)})
        reply = self.script(tag, step, messages)
        reply.usage = {'prompt_tokens': max(1, len(raw) // 16), 'completion_tokens': 30}
        with self.lock:
            record['emitted'] = ([c['function']['name'] for c in reply.delta['tool_calls']]
                                 if 'tool_calls' in reply.delta else 'content')
        return reply

    def script(self, tag, step, messages):
        if tag not in SCRIPTS:
            self.anomaly(tag, step, 'unscripted conversation')
            return Reply(final('Synthetic fixture: no script.'))
        with self.lock:
            done = {d['id'] for d in history(messages)} | self.emitted.get(tag, set())
        for index, item in enumerate(SCRIPTS[tag]):
            ident = f'{tag}-{index}-{item[1]}'
            if ident in done:
                continue
            with self.lock:
                self.emitted.setdefault(tag, set()).add(ident)
            name, args = self.call_for(item)
            return Reply(calls((name, ident, args)))
        return Reply(final(FINALS.get(tag, DEFAULT_FINAL)))

    @staticmethod
    def call_for(item):
        kind, what = item
        if kind == 'read':
            return 'Read', {'file_path': NOTES}
        data, title, view = {'hostile_prototype': (HOSTILE_PROTOTYPE, 'Checkout demo', 'prototype'),
                             'prototype': (PROTOTYPE, 'Checkout demo', 'prototype'), 'quiz': (QUIZ, 'Practice quiz', 'quiz'),
                             'dashboard': (DASHBOARD, 'This chat', 'dashboard'), 'table': (TABLE, 'Plan', 'table'),
                             'form': (FORM, 'Scope', 'form')}[what]
        return 'UIRender', {'title': title, 'kind': view, 'data': json.dumps(data, ensure_ascii=False)}


class Campaign:
    def __init__(self, command, backend):
        self.command, self.backend = command, backend
        self.provider = Provider()
        self.checks, self.names, self.facts, self.gaps, self.pending_gaps = [], set(), {}, set(), {}
        self.observations = {'run_states': {}, 'scenario_seconds': {}}
        self.fatal = None
        self.stderr_tails = []

    # Check bookkeeping ---------------------------------------------------------------------

    def check(self, name, fn):
        assert name in PLANNED, 'Unplanned check name: ' + name
        assert name not in self.names, 'Check names must be unique: ' + name
        self.names.add(name)
        try:
            evidence = fn() or {}
            self.checks.append({'name': name, 'passed': True, 'evidence': evidence})
            return True
        except Pending as exc:
            self.gaps.add(exc.gap)
            self.pending_gaps[name] = exc
            self.checks.append({'name': name, 'passed': False, 'pending': str(exc)[:600], 'gap': exc.gap})
            return False
        except Exception as exc:
            self.checks.append({'name': name, 'passed': False, 'error': bounded(exc, 2000),
                                'trace': traceback.format_exc(limit=4)[-1500:]})
            return False

    # Facts a later check needs, and the check that establishes each: a fact whose check is pending is pending.
    PRODUCERS = {'attempt': 'q1_forged_stale_cross_chat_and_malformed_attempts_are_refused',
                 'graded': 'q1_grading_is_on_the_host_and_a_replayed_submission_changes_nothing',
                 'dashboard': 'd1_bound_panels_show_counts_only_for_their_own_chat_with_a_stamp'}

    def finish(self):
        for name in PLANNED:
            if name in self.names or name == 'backend_inputs_stable_during_campaign':
                continue
            self.names.add(name)
            self.checks.append({'name': name, 'passed': False, 'not_run': True,
                                'error': 'Not run: ' + (self.fatal or 'an earlier step stopped the campaign.')[:600]})

    def passing(self):
        return all(c['passed'] for c in self.checks)

    def need(self, key):
        if key not in self.facts:
            waiting = self.pending_gaps.get(self.PRODUCERS.get(key))
            if waiting is not None:
                raise Pending(waiting.gap, '(prerequisite ' + key + ' is pending)')
            raise AssertionError('Prerequisite was not established: ' + key)
        return self.facts[key]

    def timed(self, name, scenario, *args):
        started = time.monotonic()
        try:
            return scenario(*args)
        finally:
            self.observations['scenario_seconds'][name] = round(time.monotonic() - started, 1)

    # Service helpers ------------------------------------------------------------------------

    @staticmethod
    def call(service, method, gap='routes', **params):
        """A native route; a route the service does not have is pending, by name."""
        try:
            return service.rpc(method, **params)
        except RuntimeError as exc:
            if str(exc) == 'Unknown request.':
                raise Pending(gap, '(the service answered Unknown request. for ' + method + ')') from None
            raise

    def refused(self, service, method, gap='routes', **params):
        try:
            result = self.call(service, method, gap, **params)
        except Pending:
            raise
        except RuntimeError as exc:
            text = str(exc)
            if text.startswith(('Backend timed out', 'Backend stopped')):
                raise AssertionError('No refusal was observed; the service failed: ' + text[:300]) from None
            return text[:200]
        raise AssertionError(method + ' was accepted: ' + compact(result)[:300])

    def store_rows(self, sql, args=()):
        path = self.need('home') / 'pebble.sqlite'
        with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, args)]

    def has_table(self, name):
        return bool(self.store_rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))

    def flow(self, service, cid, tag, on_wait=None, seconds=SETTLE_SECONDS):
        """Send one tagged human turn and wait until its run settles. A run that waits for input calls
        on_wait once (the person answering); any proposal is unexpected and rejected."""
        rid = service.rpc('send', id=cid, text=GOALS[tag] + ' ' + MARK + tag, limits=LIMITS)['run_id']
        deadline, waited = time.monotonic() + seconds, False
        while True:
            run = service.rpc('run_get', id=cid, run_id=rid)
            active = service.rpc('state')['active']
            if run['state'] == 'waiting_approval' and not active:
                pending = [a for a in service.rpc('get_chat', id=cid)['actions'] if a['status'] == 'pending']
                for action in pending:
                    self.provider.anomaly(tag, None, 'unexpected proposal: ' + str(action.get('kind')))
                    service.rpc('reject', id=cid, action_id=action['id'])
                if pending:
                    continue
            elif run['state'] == 'waiting_input' and not active and on_wait and not waited:
                waited = True
                on_wait(rid)
            elif run['state'] not in TRANSIENT and run['state'] not in ('waiting_approval', 'waiting_input') and not active:
                break
            elif run['state'] == 'waiting_input' and (waited or not on_wait) and not active:
                break
            if time.monotonic() > deadline:
                with contextlib.suppress(Exception):
                    service.rpc('cancel', id=cid, run_id=rid)
                raise AssertionError('Timed out waiting for ' + tag + ' to settle: ' + run['state'])
            time.sleep(0.1)
        self.observations['run_states'][tag] = run['state']
        return {'cid': cid, 'rid': rid, 'state': run['state']}

    def offered(self, tag, kind):
        """A view kind this conversation's requests offered; pending (catalogue) until the parent offers it."""
        kinds = {k for r in self.provider.matching(scenario=tag) for k in r['views'].get('UIRender', {}).get('kinds', ())}
        if kind not in kinds:
            raise Pending('catalogue', f'({tag} requests offered: {", ".join(sorted(kinds)) or "no UIRender"})')

    def view_id(self, tag, step, service=None, cid=None, rid=None):
        """The view ID the host returned for one scripted render: as the model saw it, or (for a run that waits
        before its next request) from the run's own tool receipt."""
        ident = f'{tag}-{step}-{SCRIPTS[tag][step][1]}'
        found = self.provider.seen(tag, ident)
        if found is None and service is not None:
            tools = service.rpc('run_get', id=cid, run_id=rid)['tools']
            found = next((t.get('result') for t in tools if t.get('call_id', '').endswith(ident)), None)
        if isinstance(found, dict) and 'current Pebble data version' in str(found.get('error', '')):
            raise Pending('migration', '(' + found['error'][:160] + ')')
        if not isinstance(found, dict) or not isinstance(found.get('id'), str):
            raise AssertionError(f'The {tag} view result was not visible: ' + compact(found)[:200])
        return found['id']

    # Campaign --------------------------------------------------------------------------------

    def run_all(self, scratch):
        home = scratch / 'home'
        self.facts.update(scratch=scratch, home=home)

        def created():
            assert not scratch.is_relative_to(ROOT), 'Scratch must live outside this checkout and .runtime.'
            for folder in (home, scratch / 'project'):
                folder.mkdir()
            (scratch / 'project' / NOTES).write_text('Synthetic notes for the presence fixture.\n')
            return {'outside_checkout': True, 'folders': 2}
        self.check('fixture_scratch_created_outside_the_checkout', created)
        try:
            self.timed('static', self.static_checks)
            self.timed('catalogue', self.catalogue_checks)
            self.provider.start()
            service = Phase19Service(self.command, home)
            try:
                if self.setup(service):
                    for name, scenario in (('exposure', self.scenario_exposure), ('prototype', self.scenario_prototype),
                                           ('quiz', self.scenario_quiz), ('dashboard', self.scenario_dashboard),
                                           ('form', self.scenario_form), ('presence', self.scenario_presence)):
                        self.timed(name, scenario, service)
            finally:
                if not self.passing():
                    with contextlib.suppress(Exception):
                        service.stderr.seek(0)
                        self.stderr_tails.append(service.stderr.read()[-4000:])
                service.close()
            self.final_checks()
        except Exception as exc:
            self.fatal = bounded(exc, 3000)
        finally:
            with contextlib.suppress(Exception):
                self.provider.stop()
            self.finish()

    # s1 and c1: declarations ----------------------------------------------------------------

    def static_checks(self):
        def parsed():
            if 'shell' not in self.facts:
                self.facts['shell'] = shell(MAIN_RS.read_text(encoding='utf-8'))
            return self.facts['shell']

        def generated():
            facts = parsed()
            rpc = facts['rpc']
            assert (rpc.index('if !["main","pet"].contains(&window.label())') < rpc.index('if NATIVE_ONLY.contains(&method.as_str())')
                    < rpc.index('if !ALLOWED.contains(&method.as_str())')), 'rpc() checks are out of order.'
            native = set(facts['native_only'])
            assert native == NATIVE_ONLY_18, sorted(native ^ NATIVE_ONLY_18)
            assert not native & set(facts['allowed']) and not native & set(facts['pet'])
            return {'native_only': len(native), 'generated_views_refused_first': True}
        self.check('s1_generated_views_are_refused_before_any_route_list_and_shell_only_routes_are_unchanged', generated)

        def windows():
            facts = parsed()
            allowed, pet = set(facts['allowed']), set(facts['pet'])
            assert {'artifact_get', 'artifact_history'} <= pet <= allowed, sorted(pet - allowed)
            assert not pet & set(MAIN_ONLY), sorted(pet & set(MAIN_ONLY))
            missing = sorted(set(PHASE19_ROUTES) - allowed) + sorted('pet:' + r for r in set(PET_PHASE19_ROUTES) - pet)
            if missing:
                raise Pending('routes', 'Missing: ' + ', '.join(missing))
            return {'allowed': len(allowed), 'pet': len(pet), 'main_only': list(MAIN_ONLY)}
        self.check('s1_both_windows_reach_the_phase19_routes_and_approval_stop_and_sharing_stay_in_the_main_window', windows)

        def surface():
            rpc = parsed()['rpc']
            if 'quiz_share' not in rpc or 'surface' not in rpc:
                raise Pending('surface')
            flat = ' '.join(rpc.split())
            assert re.search(r'"surface".{0,120}window\.label\(\)|window\.label\(\).{0,120}"surface"', flat), \
                'The share surface is not taken from the calling window.'
            assert rpc.index('surface') > rpc.index('if !ALLOWED.contains(&method.as_str())')
            assert rpc.index('surface') < rpc.index('backend.call(&method')
            return {'surface_from_window_label': True}
        self.check('s1_the_share_surface_comes_from_the_calling_window_never_from_the_webview', surface)

        def gaze():
            facts = parsed()
            body = facts['gaze']
            if not body:
                raise Pending('gaze')
            flat = body.replace(' ', '')
            assert 'pet_gaze' in facts['handlers'] and 'pet_gaze' not in facts['allowed']
            assert 'window.label()!="pet"' in flat or 'window.label()=="pet"' in flat, 'pet_gaze is not pet-only.'
            assert 'clamp(-1.0,1.0)' in flat, 'The gaze vector is not clamped to [-1, 1].'
            assert not re.search(r'emit[^;]*position\.(x|y)', body), 'Raw cursor coordinates leave the shell.'
            return {'registered': True, 'pet_only': True, 'clamped': True}
        self.check('s1_pet_gaze_is_a_pet_only_shell_command_that_emits_a_quantized_vector', gaze)

    def catalogue_checks(self):
        sys.path.insert(0, str(ROOT / 'backend'))

        def catalogue():
            from pebble import tool_authz
            from pebble.tool_catalogue import TOOLS
            names = [tool_name(t) for t in TOOLS]
            catalogue = PHASE18_TOOLS | LATER_TOOLS  # D72
            assert len(names) == len(set(names)) and set(names) == catalogue, sorted(set(names) ^ catalogue)
            folded = {n.casefold() for n in names}
            routes = set(PHASE19_ROUTES) | {'artifact_submit', 'artifact_stop', 'pet_gaze', 'companion'}
            assert not folded & {r.casefold() for r in routes}
            missing = sorted(set(PHASE19_ROUTES) - tool_authz.NATIVE_ONLY)
            if missing:
                raise Pending('routes', 'tool_authz.NATIVE_ONLY lacks ' + ', '.join(missing))
            return {'model_tools': len(names), 'phase19_routes_native_only': len(PHASE19_ROUTES)}
        self.check('c1_no_model_tool_is_added_and_no_phase19_route_is_a_tool_or_reachable_by_a_model_actor', catalogue)

        def layers():
            from pebble.memory import KINDS
            assert tuple(KINDS) == LAYERS, tuple(KINDS)
            return {'layers': list(LAYERS)}
        self.check('c1_the_six_memory_layers_are_exact', layers)

    # Setup ------------------------------------------------------------------------------------

    def setup(self, service):
        def profiles():
            for key, (model, context) in PROFILES.items():
                self.facts['profile:' + key] = service.rpc('model_save', name='Phase 19 ' + key + ' fixture',
                                                           url=self.provider.url, model=model, context=context)['profile_id']
            groups = service.rpc('state')['settings']['permissions']['tools']
            service.rpc('setting', key='permissions', value={'mode': 'ask', 'tools': dict.fromkeys(groups, True)})
            service.rpc('memory_save', kind='semantic', content='Remember ' + CANARY + ' for the presence fixture.')
            return {'profiles': len(PROFILES), 'permission_groups': len(groups)}
        return self.check('loopback_profiles_saved_and_view_permissions_on', profiles)

    def select(self, service, profile):
        service.rpc('model_select', profile_id=self.need('profile:' + profile))

    def conversation(self, service, profile, tag, **flow):
        cid = service.rpc('new_chat')['id']
        self.select(service, profile)
        return self.flow(service, cid, tag, **flow)

    # x1, x2 -----------------------------------------------------------------------------------

    def scenario_exposure(self, service):
        for tag, profile in (('x1', '4k'), ('x2', 'wide'), ('x3', 'wide')):
            self.facts['run:' + tag] = self.conversation(service, profile, tag)

        def small():
            records = self.provider.matching(scenario='x1')
            assert records and all(r['model'] == MODEL_4K for r in records)
            for record in records:
                assert set(record['tools']) <= PHASE18_TOOLS, sorted(set(record['tools']) - PHASE18_TOOLS)
                assert set(record['views']) == {'UIRender', 'UIUpdate'}, record['views']
                for name, limit in UI_SCHEMA_BYTES_18.items():
                    assert record['views'][name]['bytes'] <= limit, (name, record['views'][name]['bytes'])
                    assert tuple(record['views'][name]['kinds']) == LEGACY_KINDS, record['views'][name]['kinds']
                assert not record['guidance'], 'The UI guidance reached a 4,096-token route.'
            return {'requests': len(records), 'view_bytes': records[0]['views'] and
                    {n: v['bytes'] for n, v in records[0]['views'].items()}}
        self.check('x1_at_4096_tokens_view_tools_add_no_tool_kind_schema_byte_or_guidance', small)

        def wide():
            dashboard, ordinary = self.provider.matching(scenario='x2'), self.provider.matching(scenario='x3')
            assert dashboard and ordinary
            for record in ordinary:
                assert tuple(record['views'].get('UIRender', {}).get('kinds', ())) == LEGACY_KINDS, record['views']
                assert not record['guidance'], 'The UI guidance reached an ordinary table request.'
            kinds = dashboard[0]['views'].get('UIRender', {}).get('kinds', [])
            if 'dashboard' not in kinds:
                raise Pending('catalogue', '(offered kinds: ' + ', '.join(kinds) + ')')
            assert 'prototype' not in kinds, kinds
            if not dashboard[0]['guidance']:
                raise Pending('guidance')
            return {'dashboard_request_kinds': kinds, 'ordinary_request_kinds': list(LEGACY_KINDS), 'guidance_offered': True}
        self.check('x2_above_4096_new_kinds_and_guidance_are_offered_only_for_layout_requests', wide)

    # v1 -----------------------------------------------------------------------------------------

    def scenario_prototype(self, service):
        cid = service.rpc('new_chat')['id']
        self.select(service, 'wide')
        actions_before = len(self.store_rows('SELECT id FROM actions'))
        run = self.flow(service, cid, 'v1')

        def hostile():
            self.offered('v1', 'prototype')  # a refusal by the kind enum alone proves nothing about the validator
            seen = self.provider.seen('v1', 'v1-0-hostile_prototype')
            assert isinstance(seen, dict) and seen.get('error') and 'id' not in seen, compact(seen)[:300]
            rows = self.store_rows('SELECT data FROM artifacts WHERE chat_id=?', (cid,))
            assert not any('evil.example' in r['data'] for r in rows), 'The linked prototype was stored.'
            return {'refusal': seen['error'][:120], 'stored': 0}
        self.check('v1_a_prototype_with_a_link_is_refused_and_nothing_is_stored', hostile)

        def button():
            self.offered('v1', 'prototype')
            ident = self.view_id('v1', 1)
            row = service.rpc('artifact_get', chat_id=cid, id=ident)
            assert row['kind'] == 'prototype' and row['data']['screens'][0]['components'][1] == \
                {'type': 'button', 'label': 'Approve payment', 'goto': 'done'}, compact(row['data'])[:300]
            assert len(self.store_rows('SELECT id FROM actions')) == actions_before
            assert not self.store_rows('SELECT id FROM run_questions WHERE run_id=?', (run['rid'],))
            assert not self.store_rows("SELECT id FROM run_inbox WHERE run_id=? AND origin='human'", (run['rid'],))
            return {'run_state': run['state'], 'actions_added': 0, 'questions': 0, 'human_inputs': 0}
        self.check('v1_a_prototype_approve_button_is_stored_as_navigation_data_and_creates_no_action_question_or_input',
                   button)

    # q1 -----------------------------------------------------------------------------------------

    def scenario_quiz(self, service):
        """The run renders a keyed quiz, then waits on a required form: while it can still take input, the
        person takes, submits and shares the quiz, then answers the form so the run finishes."""
        cid, other = service.rpc('new_chat')['id'], service.rpc('new_chat')['id']
        self.select(service, 'wide')
        run = {}

        def waiting(rid):
            run['rid'] = rid
            self.quiz_checks(service, cid, other, rid)
            with contextlib.suppress(Exception):  # the form only lets the run finish; r1 proves form answers
                form = self.view_id('q1', 1, service, cid, rid)
                service.rpc('artifact_submit', chat_id=cid, id=form, revision=1, answers={'scope': {'option_id': 'small'}})
        finished = self.flow(service, cid, 'q1', on_wait=waiting)
        if 'rid' not in run:  # the run never waited: record the checks against the settled run
            self.quiz_checks(service, cid, other, finished['rid'])

    def quiz_checks(self, service, cid, other, rid):
        run = {'rid': rid}

        def prerequisites():
            if not self.has_table('quiz_attempts'):
                raise Pending('migration')
            ident = self.view_id('q1', 0)
            row = service.rpc('artifact_get', chat_id=cid, id=ident)
            self.call(service, 'quiz_start', chat_id=cid, id=ident, revision=row['revision'])  # pending until routed
            return ident, row

        def key():
            ident, row = prerequisites()
            readable = [row, service.rpc('artifact_history', chat_id=cid, id=ident),
                        self.call(service, 'quiz_attempt', chat_id=cid, id=ident, revision=row['revision'])]
            assert CANARY not in compact(readable) and '"explanation"' not in compact(readable[:2])
            assert all(set(q) == {'id', 'question', 'choices'} for q in row['data']['questions']), row['data']
            assert CANARY not in compact(service.events), 'An event carried the quiz key.'
            stored = self.store_rows('SELECT data FROM artifact_revisions WHERE artifact_id=?', (ident,))
            assert stored and not any(CANARY in r['data'] for r in stored)
            leaks = [method for method, params in (('run_get', {'id': cid, 'run_id': run['rid']}),
                                                   ('run_events', {'id': cid, 'run_id': run['rid']}), ('get_chat', {'id': cid}))
                     if CANARY in compact(service.rpc(method, **params))]
            if leaks:
                raise Pending('redaction', '(the quiz key is still readable through ' + ', '.join(leaks) + ')')
            return {'view_reads': 3, 'event_count': len(service.events), 'stored_revisions': len(stored)}
        self.check('q1_the_quiz_key_never_reaches_a_view_an_event_an_attempt_or_a_window_readable_run_record', key)

        def forged():
            ident, row = prerequisites()
            revision = row['revision']
            attempt = self.call(service, 'quiz_start', chat_id=cid, id=ident, revision=revision)['attempt_id']
            self.facts['attempt'] = (ident, revision, attempt)
            ids = [q['id'] for q in row['data']['questions']]
            answers = {ids[0]: 1, ids[1]: 0}
            refusals = 0
            for chat, ident_, version in ((other, attempt, revision), (cid, 'f' * 32, revision), (cid, attempt, revision + 1),
                                          (cid, attempt, 0), (cid, attempt.upper(), revision)):
                self.refused(service, 'quiz_submit', chat_id=chat, attempt_id=ident_, revision=version, answers=answers)
                refusals += 1
            for bad in ({ids[0]: 7, ids[1]: 0}, {ids[0]: True, ids[1]: 0}, {ids[0]: 1, ids[1]: 0, 'extra': 0}, [1, 0],
                        {ids[0]: '1', ids[1]: 0}):
                self.refused(service, 'quiz_submit', chat_id=cid, attempt_id=attempt, revision=revision, answers=bad)
                refusals += 1
            self.refused(service, 'quiz_start', chat_id=other, id=ident, revision=revision)
            assert not self.store_rows("SELECT id FROM run_inbox WHERE kind='artifact_result'")
            return {'refusals': refusals + 1}
        self.check('q1_forged_stale_cross_chat_and_malformed_attempts_are_refused', forged)

        def graded():
            ident, revision, attempt = self.need('attempt')
            ids = [q['id'] for q in service.rpc('artifact_get', chat_id=cid, id=ident)['data']['questions']]
            reply = self.call(service, 'quiz_submit', chat_id=cid, attempt_id=attempt, revision=revision,
                              answers={ids[0]: 1, ids[1]: 2})
            result = reply['result'] if isinstance(reply.get('result'), dict) else reply
            assert (result['score'], result['total']) == (1, 2), result
            assert {q['id']: q['correct'] for q in result['per_question']} == {ids[0]: True, ids[1]: False}
            before = self.store_rows('SELECT * FROM quiz_attempts ORDER BY id')
            for answers in ({ids[0]: 1, ids[1]: 2}, {ids[0]: 1, ids[1]: 0}):
                try:
                    again = self.call(service, 'quiz_submit', chat_id=cid, attempt_id=attempt, revision=revision, answers=answers)
                except RuntimeError:
                    continue
                again = again['result'] if isinstance(again.get('result'), dict) else again
                assert again['score'] == 1 and again['per_question'] == result['per_question'], again
            assert self.store_rows('SELECT * FROM quiz_attempts ORDER BY id') == before
            self.facts['graded'] = attempt
            return {'score': result['score'], 'total': result['total'], 'replays_changed_rows': 0}
        self.check('q1_grading_is_on_the_host_and_a_replayed_submission_changes_nothing', graded)

        def share():
            attempt = self.need('graded')
            assert not self.store_rows("SELECT id FROM run_inbox WHERE kind='artifact_result'")
            self.refused(service, 'quiz_share', chat_id=other, attempt_id=attempt, **SHELL_SHARE)
            # What the shell writes for the pet (or any other window) never reaches the run.
            pet = self.call(service, 'quiz_share', chat_id=cid, attempt_id=attempt, **{**SHELL_SHARE, 'surface': 'pet'})
            assert pet.get('shared') is not True and pet.get('status') != 'shared', pet
            assert not self.store_rows("SELECT id FROM run_inbox WHERE kind='artifact_result'")
            for _ in range(2):
                reply = self.call(service, 'quiz_share', chat_id=cid, attempt_id=attempt, **SHELL_SHARE)
                assert reply.get('shared') is True or reply.get('status') == 'shared', reply
            rows = self.store_rows("SELECT * FROM run_inbox WHERE kind='artifact_result'")
            assert len(rows) == 1 and rows[0]['origin'] == 'human' and rows[0]['run_id'] == run['rid'], rows
            assert CANARY not in rows[0]['payload']
            return {'inputs': 1, 'origin': 'human'}
        self.check('q1_only_the_main_window_share_writes_one_human_input_to_the_owning_run', share)

    # d1 -----------------------------------------------------------------------------------------

    def scenario_dashboard(self, service):
        cid, other = service.rpc('new_chat')['id'], service.rpc('new_chat')['id']
        self.select(service, 'wide')
        run = self.flow(service, cid, 'd1')

        def counts():
            self.offered('d1', 'dashboard')
            ident = self.view_id('d1', 0)
            row = service.rpc('artifact_get', chat_id=cid, id=ident)
            if row['kind'] != 'dashboard':
                raise AssertionError('The dashboard was not stored: ' + compact(row)[:200])
            bindings = row.get('bindings')
            entries = list(bindings.values()) if isinstance(bindings, dict) else list(bindings or [])
            assert entries, 'The bound panel shows no host counts.'
            text = compact(row)
            assert CANARY not in text and other not in text, 'A bound panel carried content or another chat.'
            stamps = [e.get('stamp') or {k: e.get(k) for k in ('source', 'as_of', 'digest')} for e in entries]
            assert all(isinstance(s, dict) and {'source', 'as_of', 'digest'} <= set(s) for s in stamps), stamps
            assert {s['source'] for s in stamps} <= BINDING_SOURCES
            self.facts['dashboard'] = (ident, row)
            return {'bound_panels': len(entries), 'sources': sorted({s['source'] for s in stamps})}
        self.check('d1_bound_panels_show_counts_only_for_their_own_chat_with_a_stamp', counts)

        def refresh():
            ident, row = self.need('dashboard')
            table = self.view_id('d1', 1)
            refreshed = self.call(service, 'artifact_refresh', chat_id=cid, id=ident, revision=row['revision'])
            assert refreshed['owner_run_id'] == row['owner_run_id'] == run['rid'] and refreshed['data'] == row['data']
            assert CANARY not in compact(refreshed)
            current = service.rpc('artifact_get', chat_id=cid, id=ident)['revision']
            refusals = []
            for chat, view, version in ((other, ident, current), (cid, ident, 0), (cid, ident, current + 5),
                                        (cid, table, 1), (cid, 'f' * 32, 1)):
                refusals.append(self.refused(service, 'artifact_refresh', chat_id=chat, id=view, revision=version))
            return {'revision_after_refresh': current, 'refusals': len(refusals)}
        self.check('d1_a_refresh_never_crosses_chats_or_revisions_and_keeps_the_owner_and_the_view', refresh)

    # r1 -----------------------------------------------------------------------------------------

    def scenario_form(self, service):
        cid, other = service.rpc('new_chat')['id'], service.rpc('new_chat')['id']
        self.select(service, 'wide')
        outcome = {}

        def answer(rid):
            def stale():
                ident = self.view_id('r1', 0, service, cid, rid)
                answers = {'scope': {'option_id': 'small'}}
                refusals = []
                for chat, revision, value in ((other, 1, answers), (cid, 2, answers), (cid, 0, answers),
                                              (cid, 1, {'scope': {'option_id': '__host_permission'}}),
                                              (cid, 1, {'approve': {'text': 'yes'}})):
                    refusals.append(self.refused(service, 'artifact_submit', chat_id=chat, id=ident, revision=revision,
                                                 answers=value))
                question = service.rpc('artifact_get', chat_id=cid, id=ident)['form_question']
                assert question['status'] == 'pending' and question['answer'] is None, question
                outcome['ident'] = ident
                return {'refusals': len(refusals)}
            self.check('r1_stale_cross_chat_and_forged_form_answers_are_refused_and_the_question_stays_pending', stale)
            if 'ident' in outcome:
                service.rpc('artifact_submit', chat_id=cid, id=outcome['ident'], revision=1,
                            answers={'scope': {'option_id': 'small'}})
        run = self.flow(service, cid, 'r1', on_wait=answer)

        def resumed():
            assert 'ident' in outcome, 'The form was not answered.'
            assert run['state'] == 'completed', run['state']
            question = service.rpc('artifact_get', chat_id=cid, id=outcome['ident'])['form_question']
            assert question['status'] == 'answered', question
            requests = self.provider.matching(scenario='r1')
            assert requests and requests[-1]['emitted'] == 'content'
            return {'run_state': run['state'], 'provider_requests': len(requests)}
        self.check('r1_the_persons_form_answer_resumes_the_run', resumed)

    # a1 -----------------------------------------------------------------------------------------

    def scenario_presence(self, service):
        cid = service.rpc('new_chat')['id']
        service.rpc('set_workspace', id=cid, path=str(self.need('scratch') / 'project'))
        self.select(service, 'wide')
        mark = len(service.events)
        run = self.flow(service, cid, 'a1')

        def activity():
            progress = [e.get('data') or {} for e in service.events[mark:] if e.get('event') == 'progress']
            progress = [p for p in progress if p.get('run_id') == run['rid']]
            reads = [p for p in progress if p.get('tool') == 'Read' and p.get('kind') == 'working']
            assert reads, 'No progress event for the scripted Read.'
            if not all('activity' in p for p in progress):
                raise Pending('activity')
            sys.path.insert(0, str(ROOT / 'backend'))
            from pebble.activity_kinds import KEYS
            assert all(p['activity'] in set(KEYS) | {None} for p in progress), sorted({str(p['activity']) for p in progress})
            assert all(CANARY not in str(p['activity']) for p in progress) and reads[0]['activity'] == 'reading'
            return {'progress_events': len(progress), 'read_activity': reads[0]['activity']}
        self.check('a1_progress_events_carry_the_host_activity_class_and_never_a_path', activity)

        def companion():
            mark_settings = len(service.events)
            try:
                service.rpc('setting', key='companion', value=dict(COMPANION))
            except RuntimeError as exc:
                if 'not supported' in str(exc):
                    raise Pending('companion') from None
                raise
            for bad in ({**COMPANION, 'extra': 1}, {**COMPANION, 'pointer': 'yes'}, {**COMPANION, 'status_words': 'loud'},
                        [True], None):
                self.refused(service, 'setting', key='companion', value=bad)
            stored = service.rpc('state')['settings'].get('companion')
            assert stored == COMPANION, stored
            assert any(e.get('event') == 'settings' for e in service.events[mark_settings:]), 'No settings event.'
            return {'stored': stored, 'refusals': 5}
        self.check('a1_the_companion_setting_is_validated_stored_and_announced', companion)

    # Final -------------------------------------------------------------------------------------

    def final_checks(self):
        def routes():
            with self.provider.lock:
                offered = {n for r in self.provider.records for n in r['tools']}
            facts = self.facts.get('shell') or {}
            names = set(PHASE19_ROUTES) | set(facts.get('allowed', ())) | set(facts.get('native_only', ()))
            leaked = sorted(n for n in offered if n in names or n.casefold() in {r.casefold() for r in names})
            assert offered and not leaked, leaked
            return {'tool_names_seen': len(offered), 'route_names_compared': len(names)}
        self.check('native_phase19_routes_are_never_offered_to_models', routes)

        def clean():
            with self.provider.lock:
                assert not self.provider.errors, self.provider.errors[-5:]
                assert not self.provider.anomalies, self.provider.anomalies[-5:]
                assert not [r for r in self.provider.records if r['model'] not in MODELS]
                return {'provider_attempts': len(self.provider.records)}
        self.check('synthetic_provider_protocol_clean', clean)


def checkout_digests():
    paths = ['backend/main.py', *[p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'src-tauri/src/main.rs', 'scripts/validate_phase19.py', 'scripts/validate_phase17.py',
             'scripts/validate_phase14.py', 'scripts/validate_phase13.py', 'scripts/validate_runtime.py']
    digests = {}
    for path in paths:
        try:
            digests[path] = sha((ROOT / path).read_bytes())
        except OSError:
            digests[path] = None
    return digests


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary home even on success')
    args = parser.parse_args()
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; move it aside or choose a new path (earlier raw evidence, '
                     'including a failed run, is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    command = [str(backend)] if backend else [sys.executable, str(ROOT / 'backend/main.py')]
    backend_before = sha(backend.read_bytes()) if backend else None
    sources = checkout_digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase19-')).resolve()
    if scratch.is_relative_to(ROOT):
        parser.error('The temporary folder is inside this checkout; set TMPDIR elsewhere.')
    started = time.monotonic()
    campaign = Campaign(command, backend)
    try:
        campaign.run_all(scratch)
    finally:
        duration = round(time.monotonic() - started, 1)
        sources_after = checkout_digests()
        backend_after = sha(backend.read_bytes()) if backend else None
    results = campaign.checks
    changed = sorted(p for p in set(sources) | set(sources_after) if sources.get(p) != sources_after.get(p))
    stable = not changed and backend_after == backend_before
    stability = {'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                 'evidence': {'checkout_files_compared': len(sources), 'executable_compared': bool(backend)}}
    if not stable:
        stability['error'] = ('Backend inputs changed during the campaign: '
                              + json.dumps({'checkout_files': changed[:20],
                                            'executable': bool(backend) and backend_after != backend_before}))
    results.append(stability)
    results.sort(key=lambda c: PLANNED.index(c['name']))
    complete = [c['name'] for c in results] == list(PLANNED)
    passed = complete and campaign.fatal is None and all(c['passed'] for c in results)
    keep = args.keep_scratch or not passed
    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    with campaign.provider.lock:
        records = [dict(r) for r in campaign.provider.records]
        errors, anomalies = list(campaign.provider.errors), list(campaign.provider.anomalies)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source',
        'passed': passed,
        'checks': [c['name'] for c in results if c['passed']],
        'failed': [c['name'] for c in results if not c['passed'] and 'pending' not in c],
        'pending': [c['name'] for c in results if 'pending' in c],
        'planned_checks': list(PLANNED), 'all_planned_checks_recorded': complete,
        'integration_gaps': {key: GAPS[key] for key in sorted(campaign.gaps)},
        'check_results': results, 'fatal_error': campaign.fatal,
        'observations': campaign.observations,
        'duration_seconds': duration, 'duration_budget_seconds': VERIFIER_SECONDS,
        'synthetic_provider_attempts': len(records), 'request_metadata': records,
        'provider_errors': errors, 'provider_anomalies': anomalies,
        'backend_sha256': backend_before,
        'backend_path': str(backend) if backend else None,
        'source_sha256': sources,
        'source_note': ('Digests of this checkout. In frozen mode they do not prove the executable was built from '
                        'these files; the candidate manifest is bound separately.'),
        'scratch_preserved': str(scratch) if keep else None,
        'backend_stderr_tail': campaign.stderr_tails if not passed else [],
        'limits': ('Owned loopback synthetic provider, an owned temporary home outside the checkout, the actual service '
                   'JSON-RPC launched from the main.rs environment allowlist, native main-window routes, and the shell '
                   'read as source (the validator stands in for it on quiz_share). Fixture evidence on this machine: no '
                   'live model, view quality, webview rendering, pointer sampling, frame or CPU cost, installed or signed '
                   'desktop, other platform or frontier evidence, and not an independent human security audit.'),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, 'x', encoding='utf-8') as out:
        out.write(json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']),
                      'failed': report['failed'], 'pending': report['pending'], 'fatal_error': campaign.fatal,
                      'duration_seconds': duration, 'scenario_seconds': campaign.observations.get('scenario_seconds'),
                      'scratch_preserved': report['scratch_preserved']}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())


# Development self-checks, run only by pytest on this file; the campaign never calls them. They pin this
# validator's constants to the checkout, parse main.rs and drive the scripted turns with synthetic host
# results. No service is started except by the explicit-request campaign test at the end.

def _backend_path():
    path = str(ROOT / 'backend')
    if path not in sys.path:
        sys.path.insert(0, path)


def _closed_provider():
    provider = Provider()
    provider.server.server_close()
    return provider


def _turn(provider, messages, model=MODEL_WIDE, tools=()):
    return provider.respond({'model': model, 'messages': messages, 'tools': list(tools)}).delta


def _goal(tag):
    return [{'role': 'system', 'content': 'synthetic'}, {'role': 'user', 'content': GOALS[tag] + ' ' + MARK + tag}]


def _answer(messages, delta, value):
    messages.append({'role': 'assistant', **delta})
    for call in delta.get('tool_calls', []):
        messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(value(call))})


def test_planned_names_are_unique_and_scripts_cover_every_scenario():
    assert len(PLANNED) == len(set(PLANNED)) and PLANNED[-1] == 'backend_inputs_stable_during_campaign'
    assert set(GOALS) == set(SCRIPTS) and set(FINALS) <= set(SCRIPTS)
    assert all(re.fullmatch(r'[a-z][0-9]', tag) for tag in SCRIPTS)
    assert set(GAPS) >= {'routes', 'migration', 'catalogue', 'guidance', 'activity', 'companion', 'gaze', 'surface'}


def test_pinned_catalogue_kinds_layers_and_sources_match_this_checkout():
    _backend_path()
    from pebble import artifact_schema, memory, view_bindings
    from pebble.runner import SMALL_BRIEF
    from pebble.tool_catalogue import TOOLS
    assert {tool_name(t) for t in TOOLS} == PHASE18_TOOLS | LATER_TOOLS  # D72
    if not {'UIRender', 'UIUpdate'} & set(SMALL_BRIEF):  # until a view brief lands, 4K offers the full copies
        views = {tool_name(t): len(compact(t).encode()) for t in TOOLS if tool_name(t) in UI_SCHEMA_BYTES_18}
        assert all(views[n] <= UI_SCHEMA_BYTES_18[n] for n in views), views
    assert artifact_schema.LEGACY_KINDS == LEGACY_KINDS and tuple(memory.KINDS) == LAYERS
    assert set(view_bindings.SOURCES) <= BINDING_SOURCES
    for name, source in (('prototype', PROTOTYPE), ('dashboard', DASHBOARD)):
        assert artifact_schema.validate_native(name, json.dumps(source))
    try:
        artifact_schema.validate_native('prototype', json.dumps(HOSTILE_PROTOTYPE))
    except ValueError:
        pass
    else:
        raise AssertionError('The hostile prototype fixture is accepted by this checkout.')


def test_static_shell_checks_record_passes_or_named_gaps_on_this_checkout():
    campaign = Campaign(['unused'], None)
    campaign.provider.server.server_close()
    campaign.static_checks()
    campaign.catalogue_checks()
    names = [c['name'] for c in campaign.checks]
    assert names == [n for n in PLANNED if n.startswith(('s1_', 'c1_'))]
    assert not [c for c in campaign.checks if not c['passed'] and 'pending' not in c], \
        json.dumps([c.get('error') for c in campaign.checks if 'error' in c])[:3000]
    assert campaign.checks[0]['passed'] and campaign.checks[-1]['passed']  # generated windows; six layers


def test_shell_parser_flags_list_and_gaze_violations():
    text = MAIN_RS.read_text(encoding='utf-8')
    facts = shell(text)
    assert set(facts['native_only']) == NATIVE_ONLY_18
    leaked = text.replace('&& !["state",', '&& !["state","approve","quiz_share",', 1)
    assert leaked != text and {'approve', 'quiz_share'} <= set(shell(leaked)['pet'])
    campaign = Campaign(['unused'], None)
    campaign.provider.server.server_close()
    campaign.facts['shell'] = shell(leaked)
    campaign.static_checks()
    windows = next(c for c in campaign.checks if c['name'].startswith('s1_both_windows'))
    assert not windows['passed'] and 'pending' not in windows and 'approve' in windows['error']
    # Merge (i19): main.rs now has the real pet_gaze (P8), so it is renamed first; the parser then reads the hostile one.
    gaze = text.replace('fn pet_gaze(', 'fn pet_gaze_reviewed(', 1) + \
        '\n#[tauri::command]\nfn pet_gaze(window:tauri::WebviewWindow) {\n    let _ = window.emit("pet-gaze", json!({"x":position.x}));\n}\n'
    campaign = Campaign(['unused'], None)
    campaign.provider.server.server_close()
    campaign.facts['shell'] = shell(gaze.replace('generate_handler![', 'generate_handler![pet_gaze,', 1))
    campaign.static_checks()
    found = next(c for c in campaign.checks if c['name'].startswith('s1_pet_gaze'))
    assert not found['passed'] and 'pending' not in found, found


def test_scripted_turns_render_each_view_once_then_finish():
    provider = _closed_provider()
    for tag, expected in (('v1', ['prototype', 'prototype']), ('q1', ['quiz', 'form']), ('d1', ['dashboard', 'table']),
                          ('r1', ['form'])):
        messages, kinds = _goal(tag), []
        while 'tool_calls' in (delta := _turn(provider, messages)):
            call = delta['tool_calls'][0]
            args = json.loads(call['function']['arguments'])
            kinds.append(args['kind'])
            assert call['id'].startswith(tag + '-') and call['function']['name'] == 'UIRender'
            _answer(messages, delta, lambda c: {'id': 'v-' + c['id'], 'revision': 1, 'status': 'ready'})
        assert kinds == expected and delta['content'] == FINALS.get(tag, DEFAULT_FINAL), (tag, kinds)
    messages = _goal('a1')
    delta = _turn(provider, messages)
    assert json.loads(delta['tool_calls'][0]['function']['arguments']) == {'file_path': NOTES}
    assert not provider.anomalies and not provider.errors
    assert provider.seen('q1', 'q1-0-quiz') == {'id': 'v-q1-0-quiz', 'revision': 1, 'status': 'ready'}


def test_unscripted_private_and_unknown_routes_are_flagged():
    provider = _closed_provider()
    _turn(provider, [{'role': 'user', 'content': 'Nothing scripted here. ' + MARK + 'z9'}])
    assert provider.anomalies[-1]['reason'] == 'unscripted conversation'
    for body in ({'model': 'another-model', 'messages': []},
                 {'model': MODEL_WIDE, 'messages': [{'role': 'user', 'content': 'x', '_pebble_goal': True}]}):
        try:
            provider.respond(body)
        except AssertionError:
            continue
        raise AssertionError('The provider accepted an unexpected request.')


def test_checks_are_planned_unique_and_pending_is_never_passed():
    campaign = Campaign(['unused'], None)
    campaign.provider.server.server_close()
    assert campaign.check('fixture_scratch_created_outside_the_checkout', lambda: campaign.need('missing')) is False
    assert 'Prerequisite' in campaign.checks[-1]['error']
    for bad in ('fixture_scratch_created_outside_the_checkout', 'an_unplanned_check'):
        try:
            campaign.check(bad, lambda: None)
        except AssertionError:
            continue
        raise AssertionError('A duplicate or unplanned check name was accepted.')

    class Missing:
        events = []

        @staticmethod
        def rpc(method, **params):
            raise RuntimeError('Unknown request.')
    assert campaign.check('q1_forged_stale_cross_chat_and_malformed_attempts_are_refused',
                          lambda: campaign.call(Missing, 'quiz_start')) is False
    assert campaign.checks[-1]['gap'] == 'routes' and 'quiz_start' in campaign.checks[-1]['pending']
    campaign.finish()
    recorded = {c['name']: c for c in campaign.checks}
    assert set(recorded) == set(PLANNED) - {'backend_inputs_stable_during_campaign'}
    assert recorded['synthetic_provider_protocol_clean']['not_run'] and not any(c['passed'] for c in campaign.checks)


def _campaign(request, tmp_path, node, *extra):
    import pytest
    if not any(str(arg).endswith('::' + node) for arg in request.config.args):
        pytest.skip('Starts the actual service; name this test explicitly to run it.')
    report = tmp_path / 'report.json'
    with (tmp_path / 'campaign.log').open('w') as sink:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--report', str(report), *extra], cwd=ROOT,
                              env={**os.environ, 'TMPDIR': str(tmp_path)}, stdout=sink, stderr=subprocess.STDOUT,
                              timeout=VERIFIER_SECONDS + 60)
    outcome = json.loads(report.read_text())
    import warnings
    warnings.warn('campaign evidence: ' + json.dumps({
        'seconds': outcome['duration_seconds'], 'pending': outcome['pending'], 'failed': outcome['failed'],
        'gaps': sorted(outcome['integration_gaps']), 'scenario_seconds': outcome['observations'].get('scenario_seconds'),
        **{c['name']: c.get('evidence') for c in outcome['check_results'] if c['passed']}}, default=str)[:20000])
    if outcome['failed'] or outcome['fatal_error']:
        warnings.warn('campaign diagnostics: ' + json.dumps({
            'run_states': outcome['observations'].get('run_states'),
            'errors': {c['name']: c.get('error') for c in outcome['check_results'] if c.get('error')},
            'anomalies': outcome['provider_anomalies'][-5:], 'provider_errors': outcome['provider_errors'][-5:],
            'stderr_tail': [tail[-2500:] for tail in outcome['backend_stderr_tail']]}, default=str)[:12000])
    assert outcome['kind'] == 'source' and outcome['all_planned_checks_recorded'] is True
    assert outcome['duration_seconds'] < VERIFIER_SECONDS
    return done, outcome


def test_source_campaign_records_only_passes_and_named_gaps_on_explicit_request(request, tmp_path):
    """Before the parent's Phase 19 patches: nothing fails, every gap is named, and the run does not pass."""
    done, outcome = _campaign(request, tmp_path, 'test_source_campaign_records_only_passes_and_named_gaps_on_explicit_request')
    assert not outcome['failed'] and outcome['fatal_error'] is None, json.dumps(
        {c['name']: c.get('error') for c in outcome['check_results'] if c.get('error')}, default=str)[:6000]
    assert set(outcome['checks']) | set(outcome['pending']) == set(PLANNED)
    assert all(c['gap'] in outcome['integration_gaps'] for c in outcome['check_results'] if 'pending' in c)
    assert (done.returncode == 0) == (not outcome['pending']) and outcome['passed'] is (not outcome['pending'])


def test_source_campaign_run_on_explicit_request(request, tmp_path):
    """The whole campaign in source mode: every planned check passes and none is pending."""
    done, outcome = _campaign(request, tmp_path, 'test_source_campaign_run_on_explicit_request')
    assert done.returncode == 0 and outcome['passed'] is True and not outcome['pending'], json.dumps(
        {'failed': outcome['failed'], 'pending': outcome['pending'], 'fatal': outcome['fatal_error'],
         'errors': {c['name']: c.get('error') or c.get('pending') for c in outcome['check_results'] if not c['passed']}},
        default=str)[:6000]
    assert outcome['checks'] == list(PLANNED)
