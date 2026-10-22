"""Phase 20 fault campaign (first piece): does Pebble settle honestly when its own dependencies fail?

Drives the *actual* backend over its JSON-RPC stdio contract, one owned temporary home per scenario,
with one owned loopback SSE fault provider on 127.0.0.1. Every model turn is scripted synthetic
output. No real model, external network, account, vault, Keychain or user data is used. The backend
is launched with main.rs's environment allowlist names, with HOME and TMPDIR pointed into the
scenario's own scratch folder, and with measure_resources' egress trap as its proxy (proxy-honouring
egress is refused on loopback and must be zero).

Scenarios (select a subset with --scenarios; each has its own time bound):

  f3-retry-after  429 with Retry-After of 1 s (waited, retried once), 120 s and an HTTP date about
                  90 s ahead (neither is slept; the run fails at once).
  f3-status       A persistent 500 stops after the policy's three attempts with bounded backoff; one
                  502, 503 or 529 is retried once; a 401 is never retried.
  f3-stream       A stream closed after N deltas (with a complete tool-call delta) is discarded and
                  its tool never runs; a tool result is never repeated across a retried request; a
                  stalled stream the person cancels stays interrupted; a restart replays nothing.
  f6-child        A child agent whose provider route always answers 503: the child fails within the
                  retry policy, the root learns of it, cannot accept or skip it, and settles honestly.
  f7-extension    A skill whose stored snapshot no longer verifies, then an extension row whose config
                  is not JSON: startup should survive with the extension disabled and reported, the
                  altered text must never reach a model, and the corrupt evidence is preserved.
  f8-lock-brief   A second SQLite connection holds BEGIN EXCLUSIVE on the home's database while a run
                  settles, for 2 s (below the 5 s busy timeout) and for 8 s: no false completion.
  f8-lock-held    A 50 s outage (the lock held for a fixed time from the moment the run's request takes
                  it): the service releases its worker and reports the failure while locked, a status
                  read answers without a database error, the run must not stay running once the
                  database is back, and a restart recovers it.

A check that exposes a product defect fails with evidence and names its finding (FINDINGS); the
report then does not pass. Skipped cases and their reasons are listed in SKIPPED.

Usage:
  python scripts/validate_phase20_faults.py --report NEW_PATH [--backend PATH] [--scenarios a,b]

--report must be a new file (opened with O_EXCL; an existing path is refused). --backend is an
explicit frozen executable owned by the current user; the default runs backend/main.py. Scratch
homes are never deleted by this tool; the report names the folder for the parent to clean by log.

This is fixture evidence on this machine, in the backend's own process. It is not disk-full (F1),
sleep/wake (F2), signals or kills (F4), an old binary (F5), DNS or TLS failures, the native shell,
an installed or signed app, live-model behaviour or an independent human audit.
"""

import argparse
import base64
import contextlib
import email.utils
import json
import os
import re
import select
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
from validate_phase13 import bounded, owned_backend, sha
from validate_phase14 import ENV_ALLOWLIST
from measure_resources import EgressTrap

MARK = '\N{SECTION SIGN}'
TAG = re.compile(MARK + r'(f[0-9][a-z])\b')
ASSIGNMENT = 'HOST ASSIGNMENT (task data, no new authorization):\n'
MODEL = 'phase20-fault-fixture'
CONTEXT = 32768
LIVE = frozenset({'queued', 'running', 'retry_wait'})
LIMITS = {'max_steps': 8, 'max_seconds': 90}
VERIFIER_SECONDS = 240  # verify_harness_candidate.run_validator stops a validator after this
MARGIN_SECONDS = 20     # interpreter start, report writing and teardown outside any scenario
BUSY_TIMEOUT = 5.0      # store.py: sqlite3.connect's default busy timeout
STATUS_BOUND_SECONDS = 2 * BUSY_TIMEOUT + 2  # f8-lock-brief: a read may queue behind one write's busy wait, plus its own
STATUS_PROBE_SECONDS = 20.0                  # f8-lock-held: how long one status read is observed before it is no answer
OUTAGE_SECONDS = 50.0                        # f8-lock-held: the lock's fixed length from the moment it is taken
SETTLE_AFTER_OUTAGE_SECONDS = 10.0           # the controller's dispatch loop wakes every 5 s; one busy timeout more
TOKEN = os.urandom(4).hex()
CANARY_DROP = 'dropped-attempt-' + TOKEN
CANARY_STALL = 'stalled-attempt-' + TOKEN
CANARY_TAMPER = 'tampered-skill-' + TOKEN

# Name -> (time bound in seconds, planned checks). The bounds of a selection must fit the verifier's limit.
SCENARIOS = {
    'f3-retry-after': (15, (
        'f3r_a_one_second_retry_after_is_waited_then_retried_once',
        'f3r_a_120_second_retry_after_is_never_slept_and_the_run_fails_at_once',
        'f3r_an_http_date_retry_after_beyond_30_seconds_is_parsed_and_never_slept',
    )),
    'f3-status': (20, (
        'f3s_a_persistent_500_stops_after_three_attempts_with_bounded_backoff',
        'f3s_one_502_503_or_529_is_retried_once_and_the_run_completes',
        'f3s_a_401_is_never_retried',
    )),
    'f3-stream': (20, (
        'f3t_a_stream_closed_after_n_deltas_is_discarded_and_its_tool_call_never_runs',
        'f3t_a_tool_result_is_never_repeated_across_a_retried_request',
        'f3t_a_cancelled_stalled_attempt_stays_interrupted_and_its_text_never_becomes_the_answer',
        'f3t_after_a_restart_settled_attempts_keep_their_states_and_nothing_is_replayed',
    )),
    'f6-child': (25, (
        'f6_a_child_provider_failure_stays_within_the_retry_policy_and_settles_the_child_failed',
        'f6_the_root_learns_of_the_failure_and_cannot_accept_or_skip_the_failed_child',
        'f6_the_root_settles_honestly_after_dismissing_the_failed_child',
    )),
    'f7-extension': (25, (
        'f7_skill_fixtures_installed_and_enabled',
        'f7_startup_survives_a_skill_snapshot_that_no_longer_verifies',
        'f7_a_skill_whose_snapshot_no_longer_verifies_is_disabled_and_reported_at_startup',
        'f7_the_altered_skill_is_refused_on_use_and_its_text_never_reaches_the_model',
        'f7_startup_survives_an_extension_row_with_corrupt_config_and_reports_it_disabled',
        'f7_the_corrupt_rows_and_other_data_are_preserved_as_evidence',
    )),
    'f8-lock-brief': (30, (
        'f8_a_lock_shorter_than_the_busy_timeout_only_delays_the_run',
        'f8_a_lock_longer_than_the_busy_timeout_settles_the_run_without_false_completion',
    )),
    'f8-lock-held': (80, (
        'f8_a_lock_held_through_settlement_keeps_waits_bounded',
        'f8_a_status_read_under_the_lock_answers_without_a_database_error',
        'f8_the_run_never_stays_running_after_the_service_has_settled_it',
        'f8_a_restart_keeps_a_settled_run_and_recovers_a_stranded_one_as_interrupted',
    )),
}
COMMON_FIRST = ('fixture_scratch_created_outside_the_checkout',)
COMMON_LAST = ('fault_provider_protocol_clean', 'no_proxy_honouring_egress_was_attempted',
               'backend_inputs_stable_during_campaign')

# Checks that exposed a product defect in this checkout (see docs/HARNESS_FAULT_CAMPAIGN.md). A failing check
# with an entry here is reported under `findings`; the parent's fix makes it pass. Nothing here is assumed:
# each entry is reported only when its check actually fails in this run.
FINDINGS = {
    'f7_startup_survives_an_extension_row_with_corrupt_config_and_reports_it_disabled': (
        'F7-1', 'One extension row whose config is not JSON stops backend startup (Extensions.__init__ -> list -> '
                'json.loads); the person cannot reach the app to remove it.'),
    'f7_a_skill_whose_snapshot_no_longer_verifies_is_disabled_and_reported_at_startup': (
        'F7-2', 'A skill whose stored snapshot no longer verifies stays enabled and unreported after startup; it is '
                'refused only when loaded.'),
    'f8_the_run_never_stays_running_after_the_service_has_settled_it': (
        'F8-1', 'When terminal journal writes fail under a held database lock, the run row stays running after the '
                'service released its worker; nothing retries the settlement once the database is free, and new '
                'runs are refused until a restart.'),
    'f8_a_status_read_under_the_lock_answers_without_a_database_error': (
        'F8-2', 'The status read writes: state() -> Memory.stats() -> expire() deletes and updates rows, so under a '
                'held lock a status read waits out write busy timeouts and fails with "database is locked".'),
}

SKIPPED = {
    'f3_dns_failure': ('providers.endpoint refuses http:// for a host that is not loopback, so the D20 profile '
                       'http://pebble-fault.invalid/ cannot be saved; the https:// form could send a resolver query '
                       'off this machine, which this campaign never does.'),
    'f3_stall_past_the_read_timeout': ('The provider stream timeout is 60 s (providers.py), which alone exceeds a '
                                       'scenario bound; f3-stream covers a stall the person cancels instead.'),
    'f3_tls_failure': 'Needs a TLS endpoint and trust setup outside loopback HTTP; not in this first piece.',
    'F1_disk_full': 'Parent step: an owned APFS sparse image (D20 section 5).',
    'F4_kill_mid_effect': 'Parent step: signals to owned live children (D20 section 5).',
    'F5_old_binary': 'Parent step: the backed-up 0.4.2 binary on a copied home (D20 section 5).',
}


class ScenarioTimeout(AssertionError):
    """The scenario's own time bound passed; later checks of that scenario are not run."""


def planned(selection):
    names = list(COMMON_FIRST)
    for scenario in selection:
        names += SCENARIOS[scenario][1]
    return tuple(names) + COMMON_LAST


def select_scenarios(value):
    """The validated selection, in campaign order; refuses unknown, repeated or over-budget selections."""
    if value is None:
        chosen = list(SCENARIOS)
    else:
        chosen = [part.strip() for part in value.split(',')]
        unknown = [part for part in chosen if part not in SCENARIOS]
        if not chosen or unknown:
            raise ValueError('Unknown scenario: ' + ', '.join(unknown or ['(empty)']) + '; choose from ' + ', '.join(SCENARIOS))
        if len(set(chosen)) != len(chosen):
            raise ValueError('Each scenario may be selected once.')
        chosen = [name for name in SCENARIOS if name in chosen]
    total = sum(SCENARIOS[name][0] for name in chosen) + MARGIN_SECONDS
    if total > VERIFIER_SECONDS:
        raise ValueError(f'The selected scenarios need up to {total} s, above the verifier limit of {VERIFIER_SECONDS} s.')
    return chosen


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def tool(name, ident, args, index=0):
    return {'index': index, 'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def route(messages):
    """(scenario tag, role) of one request: a child carries the host assignment; a root's tag is in its latest
    human goal (host-written context and recalled memory never count)."""
    for message in messages:
        content = message.get('content')
        if message.get('role') == 'user' and isinstance(content, str) and content.startswith(ASSIGNMENT):
            try:  # the host writes the assignment as ASCII JSON, so the mark arrives escaped
                task = json.loads(content[len(ASSIGNMENT):].split('\n', 1)[0]).get('task')
            except (ValueError, AttributeError):
                task = None
            found = TAG.search(task) if isinstance(task, str) else None
            return (found.group(1) if found else None), 'child'
    tag = None
    for message in messages:
        content = message.get('content')
        if (message.get('role') == 'user' and isinstance(content, str)
                and not content.startswith(('HOST ', 'RETRIEVED MEMORY'))):
            found = TAG.search(content)
            if found:
                tag = found.group(1)
    return tag, 'root'


def visible(messages):
    """What a script may react to: tool results by call id and host-written inputs, bounded."""
    results, host = {}, []
    for message in messages:
        content = message.get('content')
        if message.get('role') == 'tool':
            try:
                value = json.loads(content or 'null')
            except (TypeError, ValueError):
                value = {'unparsed': str(content)[:400]}
            results[message.get('tool_call_id')] = value
        elif message.get('role') == 'user' and isinstance(content, str) and content.startswith('HOST '):
            host.append(content[:1200])
    return results, host


class Locker:
    """A second SQLite connection on the home's database that holds BEGIN EXCLUSIVE for a bounded time.

    It releases after `hold` seconds, or earlier on release(), and never later than `cap` seconds."""

    def __init__(self, path, hold=None, cap=60.0):
        self.path, self.hold, self.cap = Path(path), hold, cap
        self.acquired, self.released, self.release_now = threading.Event(), threading.Event(), threading.Event()
        self.facts = {}

    def acquire(self, wait=20.0):
        threading.Thread(target=self._hold, name='phase20-locker', daemon=True).start()
        if not self.acquired.wait(wait):
            raise AssertionError('The external lock was not acquired: ' + str(self.facts.get('error')))

    def release(self):
        self.release_now.set()

    def _hold(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
            started = time.monotonic()
            db.execute('BEGIN EXCLUSIVE')
            self.facts['acquired_at'] = time.monotonic()
            self.facts['acquire_seconds'] = round(self.facts['acquired_at'] - started, 3)
            self.acquired.set()
            self.release_now.wait(min(self.cap, self.hold) if self.hold is not None else self.cap)
            db.execute('ROLLBACK')
        except Exception as exc:
            self.facts['error'] = bounded(exc, 300)
        finally:
            if db is not None:
                with contextlib.suppress(Exception):
                    db.close()
            self.facts['released_at'] = time.monotonic()
            if 'acquired_at' in self.facts:
                self.facts['held_seconds'] = round(self.facts['released_at'] - self.facts['acquired_at'], 2)
            self.released.set()


class FaultProvider:
    """Owned loopback SSE provider. Each (tag, role) has a script: one action per request, in arrival order.

    Actions: ('final', text) | ('call', name, call_id, args) | ('status', code, retry_after or None)
    | ('drop', [text parts], tool call or None): streams then closes without completion
    | ('stall', [text parts]): streams then holds the socket until the client goes away (bounded)
    | ('lock', locker, then): takes the external database lock, then performs `then`.
    A callable action receives (results, host_inputs) and returns one of the above."""

    STALL_SECONDS = 25

    def __init__(self):
        self.lock = threading.RLock()
        self.records, self.errors, self.anomalies = [], [], []
        self.scripts, self.counts = {}, {}
        self.stalled = {}
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    provider.serve(self, body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:
                    with provider.lock:
                        provider.errors.append(bounded(exc, 600))
                    with contextlib.suppress(Exception):
                        self.send_error(500)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name='phase20-provider', daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def start(self):
        self.thread.start()

    def stop(self):
        with self.lock:
            for event in self.stalled.values():
                event.set()
        self.server.shutdown()
        self.server.server_close()

    def script(self, tag, role, actions):
        with self.lock:
            self.scripts[(tag, role)] = list(actions)
            self.counts[(tag, role)] = 0

    def matching(self, **match):
        with self.lock:
            return [dict(r) for r in self.records if all(r.get(k) == v for k, v in match.items())]

    def serve(self, handler, body):
        messages = body.get('messages') or []
        if body.get('model') != MODEL:
            raise AssertionError('Unexpected model route reached the fault fixture.')
        raw = json.dumps(body, ensure_ascii=False)
        if '_pebble_' in raw:
            raise AssertionError('Host-private message fields reached the provider.')
        tag, role = route(messages)
        results, host = visible(messages)
        with self.lock:
            key = (tag, role)
            index = self.counts.get(key, 0)
            self.counts[key] = index + 1
            actions = self.scripts.get(key)
            action = actions[index] if actions is not None and index < len(actions) else None
            record = {'tag': tag, 'role': role, 'index': index, 'time': time.monotonic(),
                      'tools': sorted(t['function']['name'] for t in body.get('tools') or []
                                      if isinstance(t, dict) and isinstance(t.get('function'), dict)),
                      'tool_results': {k: compact(v)[:600] for k, v in results.items()},
                      'host_inputs': host[-6:], 'canaries': sorted(c for c in (CANARY_DROP, CANARY_STALL, CANARY_TAMPER)
                                                                     if c in raw)}
            self.records.append(record)
        if action is None:
            self.anomaly(tag, role, index, 'unscripted request')
            action = ('final', 'Synthetic fault fixture: no script for this request.')
        if callable(action):
            action = action(results, host)
        record['action'] = action[0] if action[0] != 'status' else 'status-' + str(action[1])
        try:
            self.perform(handler, action, tag, role, index)
        finally:
            record['ended'] = time.monotonic()

    def anomaly(self, tag, role, index, reason):
        with self.lock:
            self.anomalies.append({'tag': tag, 'role': role, 'index': index, 'reason': reason[:300]})

    @staticmethod
    def event(handler, payload):
        handler.wfile.write(b'data: ' + json.dumps(payload).encode() + b'\n\n')
        handler.wfile.flush()

    def open_stream(self, handler):
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.end_headers()

    def perform(self, handler, action, tag, role, index):
        kind = action[0]
        if kind == 'lock':
            action[1].acquire()
            return self.perform(handler, action[2], tag, role, index)
        if kind == 'status':
            code, delay = action[1], action[2]
            data = json.dumps({'error': {'message': f'Synthetic fault fixture HTTP {code}.'}}).encode()
            handler.send_response(code)
            handler.send_header('Content-Type', 'application/json')
            if delay is not None:
                handler.send_header('Retry-After', str(delay))
            handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
            return
        self.open_stream(handler)
        if kind in ('final', 'call'):
            delta = {'content': action[1]} if kind == 'final' else {'tool_calls': [tool(action[1], action[2], action[3])]}
            self.event(handler, {'choices': [{'delta': delta}]})
            self.event(handler, {'choices': [], 'usage': {'prompt_tokens': 400 + index, 'completion_tokens': 20}})
            handler.wfile.write(b'data: [DONE]\n\n')
            return
        if kind in ('drop', 'stall'):
            for part in action[1]:
                self.event(handler, {'choices': [{'delta': {'content': part}}]})
            if kind == 'drop':
                if len(action) > 2 and action[2]:
                    self.event(handler, {'choices': [{'delta': {'tool_calls': [action[2]]}}]})
                return  # HTTP/1.0: the connection closes here, without [DONE] or a finish reason
            released = threading.Event()
            with self.lock:
                self.stalled[tag] = released
            deadline = time.monotonic() + self.STALL_SECONDS
            while not released.wait(0.05):
                readable, _, _ = select.select([handler.connection], [], [], 0)
                if readable and not handler.connection.recv(1, socket.MSG_PEEK):
                    return  # the client closed the stream
                if time.monotonic() >= deadline:
                    self.anomaly(tag, role, index, 'stall was never interrupted by the client')
                    return
            return
        raise AssertionError('Unknown fixture action: ' + str(kind))


class FaultService:
    """One owned backend over JSON-RPC stdio. Every wait is clipped to the scenario's deadline.

    Launched from main.rs's environment allowlist names; HOME and TMPDIR point into owned scratch, and the egress
    trap's proxy variables (with NO_PROXY covering loopback) are added."""

    def __init__(self, command, home, scratch, deadline, label, extra_env=None):
        self.deadline, self.label = deadline, label
        self.events, self.responses = [], {}
        self.cond = threading.Condition()
        self.stopped, self.starting = False, True
        self.facts = {'label': label}
        for folder in ('user-home', 'tmp'):
            (scratch / folder).mkdir(mode=0o700, exist_ok=True)
        env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
        env.update(HOME=str(scratch / 'user-home'), TMPDIR=str(scratch / 'tmp'), **(extra_env or {}))
        self.stderr_path = scratch / (label + '.stderr.log')
        self.stderr = open(self.stderr_path, 'x+', encoding='utf-8')
        self.started = time.monotonic()
        self.process = subprocess.Popen([*command, '--home', str(home), '--resources', str(ROOT / 'src-tauri/resources')],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True,
                                        env=env, cwd=str(scratch))
        threading.Thread(target=self._read, name='phase20-service-reader', daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                item = json.loads(line)
                with self.cond:
                    if item.get('event'):
                        self.events.append({**item, 'at': time.monotonic()})
                    else:
                        self.responses[item.get('id')] = item
                    self.cond.notify_all()
        except (ValueError, OSError) as exc:
            self.facts['protocol_error'] = bounded(exc, 300)
        finally:
            with self.cond:
                self.stopped = True
                self.cond.notify_all()

    def stderr_tail(self, limit=2500):
        with contextlib.suppress(Exception):
            self.stderr.flush()
            return self.stderr_path.read_text(encoding='utf-8', errors='replace')[-limit:]
        return ''

    def rpc(self, method, wait=15.0, **params):
        ident = os.urandom(8).hex()
        try:
            self.process.stdin.write(json.dumps({'id': ident, 'method': method, 'params': params}) + '\n')
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            raise RuntimeError(f'Backend stopped before {method}; stderr: ' + self.stderr_tail()) from None
        limit = time.monotonic() + (45.0 if self.starting else wait)
        with self.cond:
            while ident not in self.responses:
                if self.stopped:
                    raise RuntimeError(f'Backend stopped during {method}; stderr: ' + self.stderr_tail())
                now = time.monotonic()
                if now >= self.deadline:
                    raise ScenarioTimeout(f'The scenario time bound passed during {method}.')
                if now >= limit:
                    raise RuntimeError(f'Backend timed out during {method}; stderr: ' + self.stderr_tail())
                self.cond.wait(min(limit, self.deadline) - now)
            item = self.responses.pop(ident)
        if self.starting:
            self.starting = False
            self.facts['startup_seconds'] = round(time.monotonic() - self.started, 2)
        if 'error' in item:
            raise RuntimeError(item['error'])
        return item['result']

    def seen(self, name, **data):
        with self.cond:
            return [e for e in self.events if e.get('event') == name
                    and all((e.get('data') or {}).get(k) == v for k, v in data.items())]

    def close(self):
        """Close stdin and wait; a backend that never exits is stopped through its live handle and recorded."""
        started = time.monotonic()
        with contextlib.suppress(Exception):
            self.process.stdin.close()
        try:
            self.process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.facts['stopped_through_live_handle'] = True
            self.process.kill()
            self.process.wait(timeout=5)
        self.facts['exit_code'] = self.process.returncode
        self.facts['exit_seconds'] = round(time.monotonic() - started, 2)
        with contextlib.suppress(Exception):
            self.process.stdout.close()
        with contextlib.suppress(Exception):
            self.stderr.close()
        return self.facts


class Campaign:
    def __init__(self, command, selection):
        self.command, self.selection = command, list(selection)
        self.planned = planned(self.selection)
        self.provider = FaultProvider()
        self.trap = EgressTrap()  # proxy-honouring egress from the backend is refused on loopback and recorded
        self.checks, self.names, self.facts = [], set(), {}
        self.observations = {'scenario_seconds': {}, 'run_states': {}, 'services': [], 'locks': {}}
        self.fatal = None
        self.stderr_tails = {}
        if str(ROOT / 'backend') not in sys.path:
            sys.path.insert(0, str(ROOT / 'backend'))
        from pebble.recovery import RetryPolicy  # the source policy the retries are graded against
        self.policy = RetryPolicy()

    # Check bookkeeping ---------------------------------------------------------------------

    def check(self, name, fn):
        assert name in self.planned, 'Unplanned check name: ' + name
        assert name not in self.names, 'Check names must be unique: ' + name
        self.names.add(name)
        try:
            evidence = fn() or {}
            self.checks.append({'name': name, 'passed': True, 'evidence': evidence})
            return True
        except ScenarioTimeout as exc:
            self.checks.append({'name': name, 'passed': False, 'error': bounded(exc, 600), 'time_bound': True})
            raise
        except Exception as exc:
            entry = {'name': name, 'passed': False, 'error': bounded(exc, 2400),
                     'trace': traceback.format_exc(limit=4)[-1500:]}
            if name in FINDINGS:
                entry['finding'] = FINDINGS[name][0]
            self.checks.append(entry)
            return False

    def finish(self):
        owner = {check: scenario for scenario in self.selection for check in SCENARIOS[scenario][1]}
        stopped = {**self.observations.get('scenario_errors', {}), **self.observations.get('time_bound_exceeded', {})}
        for name in self.planned:
            if name in self.names or name == 'backend_inputs_stable_during_campaign':
                continue
            self.names.add(name)
            reason = self.fatal or stopped.get(owner.get(name)) or 'an earlier step of its scenario stopped.'
            self.checks.append({'name': name, 'passed': False, 'not_run': True, 'error': 'Not run: ' + reason[:600]})

    def need(self, key):
        if key not in self.facts:
            raise AssertionError('Prerequisite was not established: ' + key)
        return self.facts[key]

    # Service helpers ------------------------------------------------------------------------

    def service(self, scenario, label):
        found = FaultService(self.command, scenario['home'], scenario['scratch'], scenario['deadline'], label,
                             extra_env=self.trap.environment())
        scenario['services'].append(found)
        return found

    def stop(self, scenario, service):
        facts = service.close()
        self.observations['services'].append(facts)
        if service in scenario['services']:
            scenario['services'].remove(service)
        return facts

    def profile(self, service):
        return service.rpc('model_save', name='Phase 20 fault fixture', url=self.provider.url, model=MODEL,
                           context=CONTEXT)['profile_id']

    @staticmethod
    def until(scenario, predicate, seconds, what, interval=0.1):
        limit = min(scenario['deadline'], time.monotonic() + seconds)
        while True:
            value = predicate()
            if value:
                return value
            if time.monotonic() >= limit:
                if time.monotonic() >= scenario['deadline']:
                    raise ScenarioTimeout('The scenario time bound passed waiting for ' + what + '.')
                raise AssertionError('Timed out waiting for ' + what + '.')
            time.sleep(interval)

    def send(self, service, tag, text, workspace=None):
        cid = service.rpc('new_chat', **({'workspace': str(workspace)} if workspace else {}))['id']
        started = time.monotonic()
        rid = service.rpc('send', id=cid, text=text + ' ' + MARK + tag, limits=LIMITS)['run_id']
        return {'cid': cid, 'rid': rid, 'tag': tag, 'sent': started}

    def settle(self, scenario, service, run, seconds=30, pending=LIVE):
        """Wait until the run leaves `pending` and the service holds no worker for it."""
        def done():
            state = service.rpc('run_get', id=run['cid'], run_id=run['rid'])['state']
            active = service.rpc('state')['active']
            return state if state not in pending and not active else None
        run['state'] = self.until(scenario, done, seconds, run['tag'] + ' to settle')
        run['settled_after'] = round(time.monotonic() - run['sent'], 2)
        self.observations['run_states'][run['tag']] = run['state']
        return run

    @staticmethod
    def events(service, run, kind=None):
        rows, after = [], 0
        while True:
            page = service.rpc('run_events', id=run['cid'], run_id=run['rid'], after=after, limit=200)
            rows += page
            if len(page) < 200:
                break
            after = page[-1]['sequence']
        return [r for r in rows if kind is None or r['kind'] == kind]

    @staticmethod
    def attempts(service, run):
        """Stream attempts in order: {attempt_id: {'step', 'attempt', 'state', 'text'}} from stream_read pages."""
        found, after = {}, 0
        while True:
            page = service.rpc('stream_read', id=run['cid'], run_id=run['rid'], after=after, limit=100)
            for event in page['events']:
                item = found.setdefault(event['attempt_id'], {'step': event['step'], 'attempt': event['attempt'],
                                                              'state': 'open', 'text': ''})
                if event['kind'] == 'delta':
                    item['text'] += event['text']
                elif event['kind'] == 'settle':
                    item['state'] = event['state']
            after = page['next_after']
            if not page['more']:
                return list(found.values())

    @staticmethod
    def answers(service, run):
        return [m.get('content') or '' for m in service.rpc('get_chat', id=run['cid'])['messages']
                if m.get('role') == 'assistant']

    def requests(self, tag, role='root'):
        return self.provider.matching(tag=tag, role=role)

    def retry_delays_within_policy(self, delays, attempts):
        policy = self.policy
        for number, delay in zip(attempts, delays):
            base = min(policy.max_delay, policy.initial_delay * 2 ** (number - 1))
            assert base * 0.8 - 1e-6 <= delay <= base * 1.2 + 1e-6, ('retry delay outside the policy', number, delay)

    # Campaign --------------------------------------------------------------------------------

    def run_all(self, scratch):
        self.facts['scratch'] = scratch

        def created():
            assert not scratch.resolve().is_relative_to(ROOT), 'Scratch must live outside this checkout and .runtime.'
            assert scratch.is_dir() and not any(scratch.iterdir()), 'Scratch must be a new, empty folder.'
            return {'outside_checkout': True}
        self.check('fixture_scratch_created_outside_the_checkout', created)
        runners = {'f3-retry-after': self.scenario_retry_after, 'f3-status': self.scenario_status,
                   'f3-stream': self.scenario_stream, 'f6-child': self.scenario_child,
                   'f7-extension': self.scenario_extension, 'f8-lock-brief': self.scenario_lock_brief,
                   'f8-lock-held': self.scenario_lock_held}
        try:
            self.provider.start()
            self.trap.start()
            for name in self.selection:
                folder = scratch / name
                folder.mkdir(mode=0o700)
                scenario = {'name': name, 'scratch': folder, 'home': folder / 'home', 'services': [],
                            'deadline': time.monotonic() + SCENARIOS[name][0]}
                started = time.monotonic()
                try:
                    runners[name](scenario)
                except ScenarioTimeout as exc:
                    self.observations.setdefault('time_bound_exceeded', {})[name] = bounded(exc, 300)
                except Exception as exc:
                    self.observations.setdefault('scenario_errors', {})[name] = bounded(exc, 1500)
                finally:
                    for service in list(scenario['services']):
                        with contextlib.suppress(Exception):
                            if not all(c['passed'] for c in self.checks):
                                self.stderr_tails[service.label] = service.stderr_tail(3000)
                            self.stop(scenario, service)
                    self.observations['scenario_seconds'][name] = round(time.monotonic() - started, 1)
            self.final_checks()
        except Exception as exc:
            self.fatal = bounded(exc, 3000)
        finally:
            with contextlib.suppress(Exception):
                self.provider.stop()
            with contextlib.suppress(Exception):
                self.trap.stop()
            self.finish()

    # f3-retry-after ----------------------------------------------------------------------------

    def scenario_retry_after(self, scenario):
        p = self.provider
        date = email.utils.formatdate(time.time() + 90, usegmt=True)
        p.script('f3a', 'root', [('status', 429, 1), ('final', 'Synthetic fault fixture: answered after the wait.')])
        p.script('f3b', 'root', [('status', 429, 120), ('final', 'Never requested.')])
        p.script('f3c', 'root', [('status', 429, date), ('final', 'Never requested.')])
        service = self.service(scenario, 'f3-retry-after')
        self.profile(service)
        runs = {}
        for tag in ('f3a', 'f3b', 'f3c'):
            runs[tag] = self.settle(scenario, service, self.send(service, tag, 'Reply with one short sentence about the fault fixture.'))

        def waited():
            run, requests = runs['f3a'], self.requests('f3a')
            retries = self.events(service, run, 'model_retry')
            assert run['state'] == 'completed', run['state']
            assert len(requests) == 2 and len(retries) == 1, (len(requests), len(retries))
            delay, gap = retries[0]['payload']['delay'], requests[1]['time'] - requests[0]['time']
            assert retries[0]['payload']['status'] == 429 and 1.0 <= delay <= self.policy.max_server_delay, retries[0]['payload']
            assert gap >= 1.0 - 0.05, ('the server delay was not waited', gap)
            return {'requests': 2, 'retry_delay': delay, 'observed_gap_seconds': round(gap, 2), 'state': run['state']}
        self.check('f3r_a_one_second_retry_after_is_waited_then_retried_once', waited)

        def refused(tag):
            def fn():
                run, requests = runs[tag], self.requests(tag)
                assert run['state'] == 'failed', run['state']
                assert len(requests) == 1, ('a server delay above 30 s was retried', len(requests))
                assert not self.events(service, run, 'model_retry'), 'A retry was scheduled.'
                assert run['settled_after'] < 10, ('the run waited', run['settled_after'])
                error = service.rpc('run_get', id=run['cid'], run_id=run['rid'])['error'] or ''
                assert 'HTTP 429' in error, error[:300]
                return {'requests': 1, 'settled_after_seconds': run['settled_after'], 'error': error[:160]}
            return fn
        self.check('f3r_a_120_second_retry_after_is_never_slept_and_the_run_fails_at_once', refused('f3b'))
        self.check('f3r_an_http_date_retry_after_beyond_30_seconds_is_parsed_and_never_slept', refused('f3c'))

    # f3-status ---------------------------------------------------------------------------------

    def scenario_status(self, scenario):
        p = self.provider
        p.script('f3d', 'root', [('status', 500, None)] * 3 + [('final', 'Never requested.')])
        for tag, code in (('f3e', 502), ('f3f', 503), ('f3g', 529)):
            p.script(tag, 'root', [('status', code, None), ('final', f'Synthetic fault fixture: recovered after {code}.')])
        p.script('f3h', 'root', [('status', 401, None), ('final', 'Never requested.')])
        service = self.service(scenario, 'f3-status')
        self.profile(service)
        runs = {}
        for tag in ('f3d', 'f3e', 'f3f', 'f3g', 'f3h'):
            runs[tag] = self.settle(scenario, service, self.send(service, tag, 'Reply with one short sentence about the fault fixture.'))

        def persistent():
            run, requests = runs['f3d'], self.requests('f3d')
            retries = self.events(service, run, 'model_retry')
            assert run['state'] == 'failed', run['state']
            assert len(requests) == self.policy.attempts == 3, len(requests)
            delays = [r['payload']['delay'] for r in retries]
            assert [r['payload']['attempt'] for r in retries] == [2, 3] and len(delays) == 2, delays
            self.retry_delays_within_policy(delays, (1, 2))
            gaps = [requests[i + 1]['time'] - requests[i]['time'] for i in range(2)]
            assert all(gap >= delay - 0.05 for gap, delay in zip(gaps, delays)), (gaps, delays)
            error = service.rpc('run_get', id=run['cid'], run_id=run['rid'])['error'] or ''
            assert 'HTTP 500' in error, error[:300]
            return {'requests': 3, 'retry_delays': delays, 'observed_gaps': [round(g, 2) for g in gaps]}
        self.check('f3s_a_persistent_500_stops_after_three_attempts_with_bounded_backoff', persistent)

        def once():
            seen = {}
            for tag, code in (('f3e', 502), ('f3f', 503), ('f3g', 529)):
                run, requests = runs[tag], self.requests(tag)
                retries = self.events(service, run, 'model_retry')
                assert run['state'] == 'completed', (tag, run['state'])
                assert len(requests) == 2 and len(retries) == 1, (tag, len(requests), len(retries))
                assert retries[0]['payload']['status'] == code, retries[0]['payload']
                self.retry_delays_within_policy([retries[0]['payload']['delay']], (1,))
                assert any(f'recovered after {code}' in text for text in self.answers(service, run)), tag
                seen[code] = round(retries[0]['payload']['delay'], 3)
            return {'retry_delay_by_status': seen}
        self.check('f3s_one_502_503_or_529_is_retried_once_and_the_run_completes', once)

        def unauthorized():
            run, requests = runs['f3h'], self.requests('f3h')
            assert run['state'] == 'failed', run['state']
            assert len(requests) == 1, ('401 was retried', len(requests))
            assert not self.events(service, run, 'model_retry')
            error = service.rpc('run_get', id=run['cid'], run_id=run['rid'])['error'] or ''
            assert 'HTTP 401' in error, error[:300]
            return {'requests': 1, 'error': error[:160]}
        self.check('f3s_a_401_is_never_retried', unauthorized)

    # f3-stream ---------------------------------------------------------------------------------

    def scenario_stream(self, scenario):
        p = self.provider
        diagram = {'title': 'Fault fixture', 'kind': 'diagram',
                   'data': json.dumps({'nodes': [{'id': 'a', 'label': 'Fault fixture'}], 'edges': []})}
        dropped = tool('UIRender', 'f3t-dropped', diagram)
        p.script('f3t', 'root', [('drop', ['Drafting the diagram ', CANARY_DROP + ' '], dropped),
                                 ('call', 'UIRender', 'f3t-render', diagram),
                                 ('status', 503, None),
                                 ('final', 'Synthetic fault fixture: the diagram is ready.')])
        p.script('f3u', 'root', [('stall', ['Thinking about ', CANARY_STALL + ' '])])
        service = self.service(scenario, 'f3-stream-1')
        self.profile(service)
        drop = self.settle(scenario, service, self.send(service, 'f3t', 'Draw a diagram of the fault fixture.'))
        drop_attempts, drop_tools = self.attempts(service, drop), service.rpc('run_get', id=drop['cid'], run_id=drop['rid'])['tools']

        def discarded():
            requests = self.requests('f3t')
            assert drop['state'] == 'completed', drop['state']
            assert len(requests) == 4, len(requests)
            assert [a['state'] for a in drop_attempts] == ['discarded', 'committed', 'discarded', 'committed'], \
                [(a['step'], a['attempt'], a['state']) for a in drop_attempts]
            assert CANARY_DROP in drop_attempts[0]['text'], 'The dropped deltas were not presented.'
            assert not any(t['call_id'].endswith('f3t-dropped') for t in drop_tools), 'The dropped tool call ran.'
            assert not any(CANARY_DROP in text for text in self.answers(service, drop)), 'Dropped text became the answer.'
            return {'attempt_states': [a['state'] for a in drop_attempts], 'receipts': len(drop_tools)}
        self.check('f3t_a_stream_closed_after_n_deltas_is_discarded_and_its_tool_call_never_runs', discarded)

        def once():
            requests = self.requests('f3t')
            renders = [t for t in drop_tools if t['name'] == 'UIRender']
            assert len(drop_tools) == 1 and len(renders) == 1 and renders[0]['state'] == 'completed', \
                [(t['name'], t['state']) for t in drop_tools]
            assert len(service.rpc('get_chat', id=drop['cid'])['artifacts']) == 1
            carried = [r['tool_results'] for r in requests[2:]]
            assert len(carried) == 2 and carried[0] == carried[1] and list(carried[0]) == ['f3t-render'], carried
            assert not requests[0]['tool_results'] and not requests[1]['tool_results']
            return {'receipts': 1, 'artifacts': 1, 'retried_request_tool_results': list(carried[0])}
        self.check('f3t_a_tool_result_is_never_repeated_across_a_retried_request', once)

        stall = self.send(service, 'f3u', 'Reply with one short sentence about the fault fixture.')

        def interrupted():
            self.until(scenario, lambda: any(CANARY_STALL in a['text'] for a in self.attempts(service, stall)),
                       10, 'the stalled deltas to be presented')
            service.rpc('cancel', id=stall['cid'], run_id=stall['rid'])
            self.settle(scenario, service, stall, 15)
            states = [a['state'] for a in self.attempts(service, stall)]
            assert stall['state'] == 'stopped', stall['state']
            assert states == ['interrupted'], states
            assert not any(CANARY_STALL in text for text in self.answers(service, stall)), 'Stalled text became the answer.'
            assert len(self.requests('f3u')) == 1
            return {'run': stall['state'], 'attempt_states': states}
        self.check('f3t_a_cancelled_stalled_attempt_stays_interrupted_and_its_text_never_becomes_the_answer', interrupted)

        before = len(self.provider.records)
        self.stop(scenario, service)
        service = self.service(scenario, 'f3-stream-2')

        def restarted():
            service.rpc('state')
            time.sleep(1.5)
            states = {}
            for run, expected in ((drop, ['discarded', 'committed', 'discarded', 'committed']), (stall, ['interrupted'])):
                got = [a['state'] for a in self.attempts(service, run)]
                state = service.rpc('run_get', id=run['cid'], run_id=run['rid'])['state']
                assert got == expected and state == run['state'], (run['tag'], got, state)
                assert not any(c in text for text in self.answers(service, run) for c in (CANARY_DROP, CANARY_STALL))
                states[run['tag']] = {'run': state, 'attempts': got}
            assert len(self.provider.records) == before, 'The restart sent a model request.'
            return states
        self.check('f3t_after_a_restart_settled_attempts_keep_their_states_and_nothing_is_replayed', restarted)

    # f6-child ----------------------------------------------------------------------------------

    def scenario_child(self, scenario):
        p = self.provider
        project = scenario['scratch'] / 'project'
        project.mkdir()
        (project / 'notes.txt').write_text('Synthetic notes for the fault fixture child.\n', encoding='utf-8')
        spawned = {}

        def spawn_result(results):
            value = results.get('f6-spawn') or {}
            if isinstance(value.get('id'), str):
                spawned.update(id=value['id'], generation=value.get('generation', 1))
            return spawned

        def wait(results, host):
            ident = spawn_result(results).get('id')
            return ('call', 'WaitForAgents', 'f6-wait', {'agent_ids': json.dumps([ident])}) if ident else \
                ('final', 'Synthetic fault fixture: the child was not spawned.')

        def accept(results, host):
            node = spawn_result(results)
            return ('call', 'ReviewAgentResult', 'f6-accept', {'agent_id': node.get('id', ''), 'generation': node.get('generation', 1)})

        def dismiss(results, host):
            node = spawn_result(results)
            return ('call', 'ReviewAgentResult', 'f6-dismiss', {'agent_id': node.get('id', ''), 'generation': node.get('generation', 1),
                                                                'decision': 'dismiss'})
        p.script('f6c', 'child', [('status', 503, None)] * 3 + [('final', 'Never requested.')])
        p.script('f6r', 'root', [
            ('call', 'SpawnAgent', 'f6-spawn', {'task': 'Summarise notes.txt in one line. ' + MARK + 'f6c', 'name': 'Fault child',
                                                'tools': json.dumps(['Read']), 'max_steps': 3, 'max_seconds': 30,
                                                'model_tokens': 100000}),
            wait,
            ('final', 'Synthetic fault fixture: finishing without a review.'),
            accept, dismiss,
            ('final', 'Synthetic fault fixture: the delegated child failed (its provider answered HTTP 503), so no child '
                      'evidence was used.')])
        service = self.service(scenario, 'f6-child')
        self.profile(service)
        # The root waits for its child (waiting_input) and is woken when the child settles; only then has it settled.
        run = self.settle(scenario, service, self.send(service, 'f6r', 'Delegate one child agent to summarise notes.txt in this '
                                                       'project, wait for it and review its result.', workspace=project),
                          25, pending=LIVE | {'waiting_input'})
        nodes = service.rpc('agent_list', chat_id=run['cid'], run_id=run['rid'])['nodes']
        roots = self.requests('f6r')

        def child():
            requests = self.requests('f6c', 'child')
            assert len(nodes) == 1, len(nodes)
            node = nodes[0]
            error = compact(node.get('result'))
            assert node['status'] == 'failed', node['status']
            assert len(requests) == self.policy.attempts, ('child attempts outside the policy', len(requests))
            assert 'HTTP 503' in error, error[:300]
            gaps = [requests[i + 1]['time'] - requests[i]['time'] for i in range(len(requests) - 1)]
            assert all(gap >= 0.4 - 0.05 for gap in gaps), gaps
            return {'child_requests': len(requests), 'status': node['status'], 'error': error[:200],
                    'observed_gaps': [round(g, 2) for g in gaps]}
        self.check('f6_a_child_provider_failure_stays_within_the_retry_policy_and_settles_the_child_failed', child)

        def learned():
            assert len(roots) >= 5, [r.get('action') for r in roots]
            # Only what the wait itself returned, or a host input after it, counts as being told.
            sources = roots[2]['host_inputs'] + [roots[2]['tool_results'].get('f6-wait', '')]
            told = next((text for text in sources if 'failed' in text), None)
            assert told, 'The woken root was not told the child failed: ' + compact(sources)[:400]
            gate = ' '.join(roots[3]['host_inputs'])
            assert 'HOST COMPLETION CHECK' in gate and 'Fault child' in gate, 'Completion was not held: ' + gate[:400]
            refusal = roots[4]['tool_results'].get('f6-accept') or ''
            assert '"error"' in refusal, 'Accepting the failed child was not refused: ' + refusal[:300]
            assert not any(n.get('review') == 'accepted' for n in nodes)
            at = told.index('failed')
            return {'told': told[max(0, at - 120):at + 40], 'completion_held_for_review': True, 'accept_refusal': refusal[:200]}
        self.check('f6_the_root_learns_of_the_failure_and_cannot_accept_or_skip_the_failed_child', learned)

        def honest():
            assert run['state'] == 'completed', run['state']
            assert nodes and nodes[0]['review'] == 'dismissed', nodes[0].get('review') if nodes else None
            assert len(roots) == 6, len(roots)
            assert all(r['action'] != 'status' and not r['action'].startswith('status-') for r in roots), 'The root saw a provider error.'
            answer = self.answers(service, run)[-1]
            assert 'child failed' in answer, answer[:300]
            return {'root_requests': len(roots), 'state': run['state'], 'review': nodes[0]['review']}
        self.check('f6_the_root_settles_honestly_after_dismissing_the_failed_child', honest)

    # f7-extension ------------------------------------------------------------------------------

    SKILLS = {'alpha': '---\nname: fault-fixture-alpha\ndescription: Fault fixture alpha guidance for the resilience '
                       'campaign.\n---\nAlpha guidance body.\n',
              'beta': '---\nname: fault-fixture-beta\ndescription: Fault fixture beta guidance for the resilience '
                      'campaign.\n---\nBeta guidance body.\n'}

    @staticmethod
    def reported_disabled(item):
        """Disabled, with a host-written reason the person can read in the extensions list."""
        config = item.get('config') if isinstance(item.get('config'), dict) else {}
        reason = item.get('error') or config.get('startup_error') or config.get('migration_error')
        return item.get('enabled') is False and isinstance(reason, str) and bool(reason.strip())

    @staticmethod
    def rows(home, sql, args=()):
        """Reads only, while the service is stopped (a read-only open of a closed WAL database is not portable)."""
        with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite', timeout=10)) as db:
            return db.execute(sql, args).fetchall()

    def scenario_extension(self, scenario):
        home = scenario['home']
        service = self.service(scenario, 'f7-install')
        ids = {}

        def installed():
            self.profile(service)
            for key, content in self.SKILLS.items():
                ids[key] = service.rpc('extension_add', kind='skill', name='Fault fixture ' + key, content=content)
                service.rpc('extension_toggle', id=ids[key], enabled=True)
            listed = {x['id']: x for x in service.rpc('extensions')}
            assert all(listed[i]['enabled'] for i in ids.values()), listed
            return {'skills': len(ids)}
        if not self.check('f7_skill_fixtures_installed_and_enabled', installed):
            return
        self.stop(scenario, service)
        # Alter the beta snapshot's main body while the service is stopped; the JSON stays valid.
        snapshot = json.loads(self.rows(home, 'SELECT snapshot FROM extension_bundles WHERE id=?', (ids['beta'],))[0][0])
        body = base64.b64decode(snapshot['bodies']['SKILL.md']).decode('utf-8')
        snapshot['bodies']['SKILL.md'] = base64.b64encode(body.replace('Beta guidance body.', 'Beta guidance body. '
                                                                       + CANARY_TAMPER).encode()).decode()
        with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite', timeout=10)) as db, db:
            db.execute('UPDATE extension_bundles SET snapshot=? WHERE id=?',
                       (json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':')), ids['beta']))
        service = self.service(scenario, 'f7-snapshot')
        listed = {}

        def survives():
            service.rpc('state')
            listed.update({x['id']: x for x in service.rpc('extensions')})
            assert set(ids.values()) <= set(listed), sorted(listed)
            return {'startup_seconds': service.facts.get('startup_seconds'), 'extensions_listed': len(listed)}
        if not self.check('f7_startup_survives_a_skill_snapshot_that_no_longer_verifies', survives):
            return

        def disabled():
            beta = listed[ids['beta']]
            assert self.reported_disabled(beta), ('the unverifiable skill is still listed as',
                                                  {'enabled': beta.get('enabled'), 'error': beta.get('error')})
            return {'enabled': beta['enabled']}
        self.check('f7_a_skill_whose_snapshot_no_longer_verifies_is_disabled_and_reported_at_startup', disabled)
        # The reviewed digest is still in the untouched config; only the stored snapshot was altered.
        digest = listed[ids['beta']]['config'].get('bundle_digest') or ''
        self.provider.script('f7r', 'root', [('call', 'LoadSkill', 'f7-load', {'id': ids['beta'], 'digest': digest}),
                                             ('final', 'Synthetic fault fixture: the beta skill could not be loaded.')])

        def refused():
            toggled = self._refusal(service, 'extension_toggle', id=ids['beta'], enabled=True)
            inspected = self._refusal(service, 'skill_inspect', id=ids['beta'])
            alpha = service.rpc('skill_inspect', id=ids['alpha'])
            assert 'Alpha guidance body.' in compact(alpha)
            run = self.settle(scenario, service, self.send(service, 'f7r', 'Use the fault fixture alpha and beta skills for '
                                                           'this note.'))
            requests = self.requests('f7r')
            receipts = service.rpc('run_get', id=run['cid'], run_id=run['rid'])['tools']
            load = [t for t in receipts if t['call_id'].endswith('f7-load')]
            assert requests and 'LoadSkill' in requests[0]['tools'], 'LoadSkill was not offered; the path was not exercised.'
            # A host refusal inside execute (a completed error receipt), not a schema refusal before it.
            assert len(load) == 1 and load[0]['state'] == 'completed', load
            assert isinstance(load[0]['result'], dict) and load[0]['result'].get('error'), load
            assert load[0]['result'].get('load_kind') is None
            assert len(requests) == 2 and '"error"' in requests[1]['tool_results'].get('f7-load', ''), requests[-1]['tool_results']
            assert all(CANARY_TAMPER not in r['canaries'] for r in self.provider.records), 'Altered skill text reached a model.'
            return {'toggle_refusal': toggled[:160], 'inspect_refusal': inspected[:160],
                    'load_error': str(load[0]['result']['error'])[:160], 'run': run['state'], 'sibling_skill_loads': True}
        self.check('f7_the_altered_skill_is_refused_on_use_and_its_text_never_reaches_the_model', refused)
        self.stop(scenario, service)
        # Corrupt the alpha row's config column while the service is stopped.
        with contextlib.closing(sqlite3.connect(home / 'pebble.sqlite', timeout=10)) as db, db:
            db.execute('UPDATE extensions SET config=? WHERE id=?', ('{"bundle_digest": "truncated', ids['alpha']))
        evidence_before = self._evidence(home, ids)
        service = self.service(scenario, 'f7-config')

        def config():
            try:
                service.rpc('state')
                listed = {x['id']: x for x in service.rpc('extensions')}
            except RuntimeError as exc:
                raise AssertionError('Startup did not survive: ' + str(exc)[:1500]) from None
            alpha = listed.get(ids['alpha'])
            assert alpha is not None and self.reported_disabled(alpha), ('the corrupt extension is listed as', alpha)
            assert ids['beta'] in listed
            self._refusal(service, 'extension_toggle', id=ids['alpha'], enabled=True)
            return {'enabled': alpha['enabled'], 'reported': True}
        self.check('f7_startup_survives_an_extension_row_with_corrupt_config_and_reports_it_disabled', config)
        self.stop(scenario, service)

        def preserved():
            after = self._evidence(home, ids)
            assert after == evidence_before, {k: (evidence_before[k], after[k]) for k in after if after[k] != evidence_before.get(k)}
            return {k: v for k, v in after.items() if k.startswith('count:')} | {'integrity': after['integrity']}
        self.check('f7_the_corrupt_rows_and_other_data_are_preserved_as_evidence', preserved)

    @staticmethod
    def _refusal(service, method, **params):
        try:
            service.rpc(method, **params)
        except RuntimeError as exc:
            text = str(exc)
            if text.startswith(('Backend timed out', 'Backend stopped')):
                raise AssertionError('No refusal was observed; the service failed: ' + text[:300]) from None
            return text[:300]
        raise AssertionError(method + ' was accepted.')

    def _evidence(self, home, ids):
        """The corrupt bytes themselves (never silently repaired), integrity, and other tables' row counts."""
        found = {'integrity': self.rows(home, 'PRAGMA integrity_check')[0][0],
                 'alpha_config_sha256': sha(self.rows(home, 'SELECT config FROM extensions WHERE id=?', (ids['alpha'],))[0][0]),
                 'beta_snapshot_sha256': sha(self.rows(home, 'SELECT snapshot FROM extension_bundles WHERE id=?', (ids['beta'],))[0][0])}
        for table in ('chats', 'messages', 'runs', 'run_tools', 'extensions', 'extension_bundles', 'memories'):
            with contextlib.suppress(sqlite3.OperationalError):
                found['count:' + table] = self.rows(home, f'SELECT count(*) FROM {table}')[0][0]
        return found

    # f8-lock-brief and f8-lock-held -------------------------------------------------------------

    def locked_run(self, scenario, service, locker, tag, seconds):
        """Send one run whose model request takes the external lock first; wait until it settles."""
        run = self.send(service, tag, 'Reply with one short sentence about the fault fixture.')
        worst = 0.0

        def done():
            nonlocal worst
            started = time.monotonic()
            state = service.rpc('state')
            worst = max(worst, time.monotonic() - started)
            return run['rid'] not in {r['id'] for r in state['runs'] if r['state'] in LIVE} and not state['active']
        self.until(scenario, done, seconds, tag + ' to settle', interval=0.2)
        assert locker.released.wait(max(0.0, min(15.0, scenario['deadline'] - time.monotonic()))), locker.facts
        assert 'error' not in locker.facts, locker.facts
        run['state'] = service.rpc('run_get', id=run['cid'], run_id=run['rid'])['state']
        run['worst_state_rpc_seconds'] = round(worst, 2)
        self.observations['run_states'][tag] = run['state']
        self.observations['locks'][tag] = dict(locker.facts)
        return run

    def scenario_lock_brief(self, scenario):
        db = scenario['home'] / 'pebble.sqlite'
        lockers = {'f8a': Locker(db, hold=2.0), 'f8b': Locker(db, hold=8.0)}
        for tag, locker in lockers.items():
            self.provider.script(tag, 'root', [('lock', locker, ('final', f'Synthetic fault fixture: answer {tag}.')),
                                               ('final', 'Never requested.')])
        service = self.service(scenario, 'f8-lock-brief')
        self.profile(service)

        def short():
            run = self.locked_run(scenario, service, lockers['f8a'], 'f8a', 20)
            assert run['state'] == 'completed', run['state']
            assert any('answer f8a' in text for text in self.answers(service, run))
            return {'state': run['state'], 'lock_seconds': lockers['f8a'].facts.get('held_seconds'),
                    'worst_state_rpc_seconds': run['worst_state_rpc_seconds']}
        self.check('f8_a_lock_shorter_than_the_busy_timeout_only_delays_the_run', short)

        def medium():
            run = self.locked_run(scenario, service, lockers['f8b'], 'f8b', 30)
            attempts = [a['state'] for a in self.attempts(service, run)]
            answered = any('answer f8b' in text for text in self.answers(service, run))
            if run['state'] == 'completed':
                assert answered and attempts == ['committed'], (attempts, answered)
            else:
                assert run['state'] == 'failed' and not answered and 'committed' not in attempts, \
                    (run['state'], attempts, answered)
            worst = run['worst_state_rpc_seconds']
            assert worst <= 2 * BUSY_TIMEOUT + 2, ('a status read waited too long', worst)
            return {'state': run['state'], 'attempts': attempts, 'answer_committed': answered,
                    'error': (service.rpc('run_get', id=run['cid'], run_id=run['rid'])['error'] or '')[:160],
                    'lock_seconds': lockers['f8b'].facts.get('held_seconds'), 'worst_state_rpc_seconds': worst}
        self.check('f8_a_lock_longer_than_the_busy_timeout_settles_the_run_without_false_completion', medium)

    def scenario_lock_held(self, scenario):
        """A database outage of OUTAGE_SECONDS from the moment the run's model request takes the lock. Its length
        does not depend on what the service does, so a fix that settles later (once the database is back) passes."""
        locker = Locker(scenario['home'] / 'pebble.sqlite', hold=None, cap=OUTAGE_SECONDS + STATUS_PROBE_SECONDS)
        self.provider.script('f8c', 'root', [('lock', locker, ('final', 'Synthetic fault fixture: answer f8c.')),
                                             ('final', 'Never requested.')])
        self.provider.script('f8d', 'root', [('final', 'Synthetic fault fixture: a later task.')])
        service = self.service(scenario, 'f8-lock-held-1')
        self.profile(service)
        stranded, outage = {}, {}

        def bounded_waits():
            run = self.send(service, 'f8c', 'Reply with one short sentence about the fault fixture.')
            stranded.update(run)
            try:
                self.until(scenario, locker.acquired.is_set, 15, 'the model request to take the external lock')
                ends = locker.facts['acquired_at'] + OUTAGE_SECONDS
                # No RPC while the run settles: the service announces idle for this run when it releases its worker.
                idle = lambda: service.seen('idle', run_id=run['rid'])
                self.until(scenario, idle, max(0.0, ends - time.monotonic()),
                           'the service to release its worker while the database is locked', interval=0.2)
                outage['worker_released_after'] = round(idle()[0]['at'] - locker.facts['acquired_at'], 1)
                # One status read with the worker released and the database still locked (checked below).
                started = time.monotonic()
                try:
                    service.rpc('state', wait=STATUS_PROBE_SECONDS)
                    outage['status_read'] = {'seconds': round(time.monotonic() - started, 2)}
                except RuntimeError as exc:
                    outage['status_read'] = {'seconds': round(time.monotonic() - started, 2), 'error': str(exc)[:200]}
                outage['status_read']['while_locked'] = not locker.released.is_set()
                time.sleep(max(0.0, ends - time.monotonic()))
            finally:
                locker.release()
            assert locker.released.wait(10) and 'error' not in locker.facts, locker.facts
            self.observations['locks']['f8c'] = dict(locker.facts)
            self.observations['outage'] = dict(outage)
            errors = [e for e in service.seen('error') if (e.get('data') or {}).get('chat_id') == run['cid']]
            assert errors, 'The run reported no failure while the database was locked.'
            return {'worker_released_after_seconds': outage['worker_released_after'],
                    'outage_seconds': locker.facts.get('held_seconds'), 'reported_error': errors[0]['data'].get('message', '')[:160]}
        self.check('f8_a_lock_held_through_settlement_keeps_waits_bounded', bounded_waits)

        def status_read():
            # Deterministic part only: the read answers within its observation window without a database error.
            # Its latency is evidence; it queues behind other threads' busy waits on the one shared connection.
            found = outage.get('status_read')
            assert found and found['while_locked'], ('Prerequisite: a status read while the database was locked', found)
            assert 'error' not in found, ('a status read failed while the database was locked', found)
            return found
        self.check('f8_a_status_read_under_the_lock_answers_without_a_database_error', status_read)

        def never_running():
            assert stranded.get('rid') and locker.released.is_set() and service.seen('idle', run_id=stranded['rid']), \
                'Prerequisite: the locked run was sent, its worker released under the lock, and the lock released.'

            def settled():
                return service.rpc('run_get', id=stranded['cid'], run_id=stranded['rid'])['state'] not in LIVE
            try:
                self.until(scenario, settled, SETTLE_AFTER_OUTAGE_SECONDS, 'the stranded run to settle once the database '
                           'is free', interval=0.5)
            except ScenarioTimeout:
                raise
            except AssertionError:
                state = service.rpc('run_get', id=stranded['cid'], run_id=stranded['rid'])
                stranded['state'] = state['state']
                other = service.rpc('new_chat')['id']
                try:
                    admitted = service.rpc('send', id=other, text='A new task after the lock. ' + MARK + 'f8d', limits=LIMITS)
                    admission = {'admitted': True, 'run_id': admitted.get('run_id')}
                except RuntimeError as refusal:
                    admission = {'admitted': False, 'error': str(refusal)[:200]}
                raise AssertionError({'run_state_with_the_service_idle': state['state'],
                                      'service_active': service.rpc('state')['active'],
                                      'last_run_events': [e['kind'] for e in self.events(service, stranded)][-6:],
                                      'new_task_after_the_lock': admission}) from None
            state = service.rpc('run_get', id=stranded['cid'], run_id=stranded['rid'])
            stranded['state'] = state['state']
            assert state['state'] == 'completed' or not any('answer f8c' in t for t in self.answers(service, stranded))
            return {'state': state['state'], 'error': (state.get('error') or '')[:160]}
        self.check('f8_the_run_never_stays_running_after_the_service_has_settled_it', never_running)
        with contextlib.suppress(Exception):
            # A later task the probe above admitted settles before the restart.
            self.until(scenario, lambda: not service.rpc('state')['active'], 15, 'the probe task')
        self.stop(scenario, service)
        service = self.service(scenario, 'f8-lock-held-2')

        def recovered():
            state = service.rpc('run_get', id=stranded['cid'], run_id=stranded['rid'])
            if stranded.get('state') in LIVE:
                assert state['state'] == 'interrupted', state['state']
            else:
                assert state['state'] == stranded.get('state'), (stranded.get('state'), state['state'])
            assert not service.rpc('state')['active']
            return {'before_restart': stranded.get('state'), 'after_restart': state['state']}
        self.check('f8_a_restart_keeps_a_settled_run_and_recovers_a_stranded_one_as_interrupted', recovered)

    # Final -------------------------------------------------------------------------------------

    def final_checks(self):
        def clean():
            with self.provider.lock:
                assert not self.provider.errors, self.provider.errors[-5:]
                assert not self.provider.anomalies, self.provider.anomalies[-5:]
                return {'provider_requests': len(self.provider.records)}
        self.check('fault_provider_protocol_clean', clean)

        def egress():
            found = self.trap.report()
            assert found['attempts'] == 0, found
            return found
        self.check('no_proxy_honouring_egress_was_attempted', egress)


def checkout_digests():
    paths = ['backend/main.py', *[p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'scripts/validate_phase20_faults.py', 'scripts/validate_phase13.py', 'scripts/validate_phase14.py',
             'scripts/validate_runtime.py', 'scripts/measure_resources.py']
    digests = {}
    for path in paths:
        try:
            digests[path] = sha((ROOT / path).read_bytes())
        except OSError:
            digests[path] = None
    return digests


def write_new(path, text):
    """Create the report; an existing path (or a link) is refused, never overwritten."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as out:
        out.write(text)


def build_report(campaign, *, backend, backend_sha, sources, sources_after, duration, scratch):
    results = campaign.checks
    changed = sorted(p for p in set(sources) | set(sources_after) if sources.get(p) != sources_after.get(p))
    stable = not changed and (backend_sha is None or backend_sha == (sha(backend.read_bytes()) if backend else None))
    stability = {'name': 'backend_inputs_stable_during_campaign', 'passed': stable,
                 'evidence': {'checkout_files_compared': len(sources), 'executable_compared': bool(backend)}}
    if not stable:
        stability['error'] = 'Backend inputs changed during the campaign: ' + json.dumps(changed[:20])
    results.append(stability)
    results.sort(key=lambda c: campaign.planned.index(c['name']))
    complete = [c['name'] for c in results] == list(campaign.planned)
    passed = complete and campaign.fatal is None and all(c['passed'] for c in results)
    with campaign.provider.lock:
        records = [{k: v for k, v in r.items() if k not in ('tool_results', 'host_inputs')} for r in campaign.provider.records]
        errors, anomalies = list(campaign.provider.errors), list(campaign.provider.anomalies)
    findings = [{'id': FINDINGS[c['name']][0], 'check': c['name'], 'summary': FINDINGS[c['name']][1],
                 'evidence': c.get('error')} for c in results if not c['passed'] and c['name'] in FINDINGS and not c.get('not_run')]
    return {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source',
        'passed': passed,
        'checks': [c['name'] for c in results if c['passed']],
        'failed': [c['name'] for c in results if not c['passed']],
        'findings': findings,
        'scenarios': campaign.selection,
        'scenario_bounds_seconds': {name: SCENARIOS[name][0] for name in campaign.selection},
        'planned_checks': list(campaign.planned), 'all_planned_checks_recorded': complete,
        'check_results': results, 'fatal_error': campaign.fatal,
        'observations': campaign.observations,
        'duration_seconds': duration, 'duration_budget_seconds': VERIFIER_SECONDS,
        'synthetic_provider_attempts': len(records), 'request_metadata': records,
        'provider_errors': errors, 'provider_anomalies': anomalies,
        'retry_policy': {k: getattr(campaign.policy, k) for k in ('attempts', 'initial_delay', 'max_delay', 'max_server_delay')},
        'skipped_cases': SKIPPED,
        'backend_sha256': backend_sha, 'backend_path': str(backend) if backend else None,
        'source_sha256': sources,
        'source_note': ('Digests of this checkout. In frozen mode they do not prove the executable was built from these '
                        'files; the candidate manifest is bound separately.'),
        'scratch_preserved': str(scratch),
        'backend_stderr_tail': campaign.stderr_tails if not passed else {},
        'limits': ('Owned loopback synthetic provider, one owned temporary home per scenario outside the checkout, the '
                   'actual service JSON-RPC launched from the main.rs environment allowlist names (HOME and TMPDIR '
                   'owned), and a second SQLite connection on the owned home. Fixture evidence on this machine: no '
                   'disk-full, sleep/wake, signal, old-binary, DNS or TLS case, no native shell, installed or signed '
                   'app, live model or independent human audit.'),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--backend', help='explicit frozen backend executable owned by the current user')
    parser.add_argument('--report', required=True, type=Path, help='new report file path; never overwritten')
    parser.add_argument('--scenarios', help='comma-separated subset of: ' + ', '.join(SCENARIOS))
    args = parser.parse_args(argv)
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; move it aside or choose a new path (earlier raw evidence, '
                     'including a failed run, is never overwritten).')
    try:
        selection = select_scenarios(args.scenarios)
    except ValueError as exc:
        parser.error(str(exc))
    backend = owned_backend(parser, args.backend) if args.backend else None
    command = [str(backend)] if backend else [sys.executable, str(ROOT / 'backend/main.py')]
    backend_sha = sha(backend.read_bytes()) if backend else None
    sources = checkout_digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase20-faults-')).resolve()
    if scratch.is_relative_to(ROOT):
        parser.error('The temporary folder is inside this checkout; set TMPDIR elsewhere.')
    started = time.monotonic()
    campaign = Campaign(command, selection)
    try:
        campaign.run_all(scratch)
    finally:
        duration = round(time.monotonic() - started, 1)
        sources_after = checkout_digests()
    report = build_report(campaign, backend=backend, backend_sha=backend_sha, sources=sources, sources_after=sources_after,
                          duration=duration, scratch=scratch)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_new(report_path, json.dumps(report, indent=2, default=str) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': report['passed'], 'passed_checks': len(report['checks']),
                      'failed': report['failed'], 'findings': [f['id'] for f in report['findings']],
                      'fatal_error': campaign.fatal, 'duration_seconds': duration,
                      'scenario_seconds': campaign.observations.get('scenario_seconds'),
                      'scratch_preserved': report['scratch_preserved']}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
