"""Phase 15 source/frozen JSON-RPC proof: memory contests, archives, isolation, model handoff, constraints, delegation.

Drives the *actual* service over its JSON-RPC stdio contract with one owned
loopback SSE provider, disposable temporary homes and owned projects under a
private temporary root (never under .runtime). The service is launched from an
explicit allowlisted environment, as src-tauri/src/main.rs launches it. Every
model turn is scripted synthetic output; every memory, archive and handoff
decision is the native main-window RPC a person uses. No real model, external
network, account, credential or user data is used.

Scenarios (each tagged with an opaque marker, in its own conversation and project):

  m1  Two config files in one project give one key two values. Both learned
      records are contested and withheld from recall behind a host conflict note
      naming both IDs; memory_contests lists the group; memory_resolve_contest
      (keep) forgets the counterpart, which is never learned again from the same
      source, including after a service restart.
  m2  memory_archive_export_to writes new files (an existing path is refused). A
      second home that already learned the forgotten counterpart, with its
      profile layer disabled, previews the archive: counts only, the tombstone
      forgets the local copy and the disabled layer is skipped. Import is refused
      when the file or the local state changed after the preview, and when one
      byte is flipped. The accepted import applies the tombstone first, fills no
      disabled layer, and the counterpart is never learned again.
  m3  A fact learned in project A is recalled there (positive control) but never
      in project B, nor in a folder that replaced A at the same path with
      identical content, natively and in model requests.
  h1  A run on a loopback (device) route B, then a run on a cloud-classified route
      A: an owned local port on the unspecified address that refuses
      connections, so nothing leaves this machine and the run fails at transport.
      The preview from A's run to B is counts only and withholds A's history.
      Changing B's profile between approval and start is refused and nothing is
      sent; an approved start creates a new run on B, which receives the released
      device history and never the withheld text.
  c1  On an 8K-window profile, a 2,400-character early human constraint (past
      the old 2,200-character cut) survives repeated compaction over three later
      runs: every later model request carries it verbatim or with the host's
      explicit shortened/omitted marker, and the constraints unit never carries
      assistant text.
  d1  Three conversations (fixture tags d1, d2, d3). A spawn whose child budget
      is below the step floor, and a spawn whose shared pool is below the floor,
      are refused with numbers and spawn nothing. WaitForAgents returns a compact
      settled-child entry; review rounds past the ping-pong limit are refused
      until the root accepts with the host's default evidence.
  s1  A file the run read is changed by the validator while the run is mid-turn.
      Settlement records it as stale (the handoff's source audit) and the
      completion check reports it.

Each check is recorded independently by unique name with content-free evidence;
a failure never stops later independent checks, and a native route that does not
exist fails its checks with the route named. Source and frozen runs use the same
check names; backend_inputs_stable_during_campaign re-hashes the checkout in
both modes and the executable too in frozen mode.

Usage:
  python scripts/validate_phase15.py --report PATH [--backend PATH] [--keep-scratch]

--report must be a new file. --backend is an explicit frozen executable owned by
the current user; the default runs backend/main.py from this checkout.

This is fixture evidence on this machine. It is not live-model quality,
installed/signed desktop behavior, other-platform behavior, frontier comparison
or an independent human security audit.
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
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT
from validate_phase13 import (ASSIGNMENT, Reply, after_goal, bounded, calls, final, goal_index, history,
                              owned_backend, permissions, rejected, sha, user_text)
from validate_phase14 import AllowlistedService

MODEL = 'phase15-integration-fixture'
MODEL_SMALL = 'phase15-small-window-fixture'
MODEL_B = 'phase15-route-b-fixture'
MODEL_A = 'phase15-route-a-unreachable'
MODELS = frozenset({MODEL, MODEL_SMALL, MODEL_B})
CONTEXT, SMALL_CONTEXT, CHANGED_CONTEXT = 32768, 8192, 16384
MARK = '\N{SECTION SIGN}'
TAG = re.compile(MARK + '([a-z][0-9])')
TRANSIENT = frozenset({'queued', 'running', 'retry_wait', 'waiting_input'})
LAYERS = ('working', 'episodic', 'semantic', 'procedural', 'temporal', 'profile')
SETTLE_SECONDS = 60
HOLD_SECONDS = 60
VERIFIER_SECONDS = 240
TOKEN = uuid.uuid4().hex[:8]
M3_CODENAME = 'falcon-' + TOKEN
M2_PROFILE = 'PHASE15-M2-PROFILE-' + TOKEN
H1_DEVICE = 'PHASE15-H1-DEVICE-' + TOKEN
H1_CLOUD = 'PHASE15-H1-CLOUD-' + TOKEN
C1_HEAD = 'PHASE15-C1-HEAD-' + TOKEN
C1_TAIL = 'PHASE15-C1-TAIL-' + TOKEN
C1_ASSISTANT = 'PHASE15-C1-ASSISTANT-' + TOKEN
D3_TAIL = 'PHASE15-D3-TAIL-' + TOKEN
# Plain ASCII without quotes or backslashes, so its JSON form is the same text; longer than the old 2,200 cut.
CONSTRAINT = (C1_HEAD + ' ' + ' '.join(f'Constraint clause {n} states that the archive budget for section {n} stays '
                                       f'fixed at {n * 7} units.' for n in range(1, 27)) + ' ' + C1_TAIL)
C1_TURN = CONSTRAINT + ' ' + MARK + 'c1'  # the exact human turn the campaign sends
CONSTRAINTS_PREFIX = 'HOST HUMAN CONSTRAINTS'
FORMS = ('absent', 'omitted', 'archived', 'shortened', 'verbatim')  # weakest to strongest carriage
SPINE_PREFIX = 'HOST EVIDENCE'
C1_READS = 6
D3_BOGUS_REVIEWS = 4
D3_REPORT = 'D3 child report: ' + 'the delegated notes were read and summarised line by line. ' * 6 + D3_TAIL
COMPACT_KEYS = frozenset({'agent_id', 'name', 'generation', 'status', 'review', 'evidence_ids', 'changed_paths',
                          'patch_ids', 'untrusted_summary', 'summary_truncated'})
HEAVY_KEYS = frozenset({'task', 'context', 'tools', 'limits', 'receipts', 'result', 'dependencies', 'write_paths'})
NATIVE_ONLY = ('memory_contests', 'memory_resolve_contest', 'memory_relations', 'memory_archive_export_to',
               'memory_archive_preview_file', 'memory_archive_import_file', 'model_handoff_preview',
               'model_handoff_approve', 'model_handoff_start')
STALE_COMPLETION = re.compile(r'source file\(s\) read in this run (?:changed|can no longer be checked)', re.I)
WATCH = {'m3': M3_CODENAME, 'h1_device': H1_DEVICE, 'h1_cloud': H1_CLOUD, 'c1_head': C1_HEAD, 'c1_tail': C1_TAIL,
         'd3_tail': D3_TAIL}
GOALS = {
    'm1': 'Read dev.yaml and prod.yaml and compare the configured port.',
    'm1_again': 'Read prod.yaml again and report the configured port.',
    'm2': 'Read prod.yaml and report the configured port.',
    'm3': 'Read notes.yaml and tell me the project codename.',
    'h1_device': 'Say hello on this route. ' + H1_DEVICE,
    'h1_cloud': 'Continue the same task on the other route. ' + H1_CLOUD,
    'c1': 'Read big.txt in pages and summarise it, pass %d.',
    'd1': 'Use a child agent to read notes.txt and report its first line.',
    'd2': 'Use a child agent to read notes.txt and report its first line.',
    'd3': 'Delegate reading notes.txt to a child agent, wait for it, then review its result.',
    's1': 'Read facts.txt and report the release name.',
}
CHILD_TASK = 'Read notes.txt and report its first line.'
M1_FILES = {'dev.yaml': 'port: 8080\n', 'prod.yaml': 'port: 9090\n'}
S1_BEFORE, S1_AFTER = 'Release name: Aurora\n', 'Release name: Borealis\n'
BIG = ''.join(f'Line {n:04d} of the long reference text keeps the small window busy until it compacts.\n'
              for n in range(1, 901))
NOTES = 'first line of the delegated notes\nsecond line\n'


def readonly(home):
    return contextlib.closing(sqlite3.connect((home / 'pebble.sqlite').as_uri() + '?mode=ro', uri=True, timeout=10))


def semantic(service):
    """Learned semantic records with their quoted source line and learning status."""
    found = []
    for row in service.rpc('memory_list', kind='semantic'):
        try:
            body = json.loads(row['content'])
        except (TypeError, ValueError):
            body = None
        found.append({'id': row['id'], 'quote': body.get('quote') if isinstance(body, dict) else None,
                      'status': row.get('learning_status') or row.get('status'), 'scope': row['scope'],
                      'origin': row['origin']})
    return found


def quoted(facts, quote):
    return [fact for fact in facts if fact['quote'] == quote]


def flip(data, marker):
    """One byte changed inside a known ASCII marker: the JSON stays valid, its section checksum does not."""
    at = data.index(marker.encode()) + 1
    return data[:at] + bytes([ord('Q') if data[at] != ord('Q') else ord('R')]) + data[at + 1:]


def shortened_exactly(entry):
    """An explicit shortened marker whose head and tail are exact spans of the c1 human turn (as verify_unit checks)."""
    mark = entry.get('shortened')
    span = mark.get('omitted_chars') if isinstance(mark, dict) else None
    if not (isinstance(span, list) and len(span) == 2 and all(type(n) is int for n in span)):
        return False
    start, end = span
    return (mark.get('total_chars') == len(C1_TURN) and 0 <= start < end <= len(C1_TURN)
            and entry.get('text_head') == C1_TURN[:start] and entry.get('text_tail') == C1_TURN[end:]
            and C1_HEAD in entry['text_head'])


def constraint_form(messages, input_id=None):
    """How one request carries the c1 constraint: 'verbatim', or the host's explicit 'shortened',
    'archived' or 'omitted' marker naming its input, or 'absent'."""
    texts = [m['content'] for m in messages if isinstance(m.get('content'), str)]
    units = [t for t in texts if t.startswith(CONSTRAINTS_PREFIX)]
    found = {'form': 'absent', 'id_named': False, 'spine': any(t.startswith(SPINE_PREFIX) for t in texts),
             'unit': bool(units), 'leak': any(C1_ASSISTANT in t for t in units)}
    if any(CONSTRAINT in t for t in texts):
        found['form'] = 'verbatim'
    for text in units:
        try:
            body = json.loads(text[text.index('\n') + 1:])
        except ValueError:
            continue
        named = []
        for entry in body.get('turns') or [] if isinstance(body, dict) else []:
            if not isinstance(entry, dict):
                continue
            if entry.get('text') == C1_TURN:
                named.append(('verbatim', entry.get('input_id') == input_id))
            elif shortened_exactly(entry):
                named.append(('shortened', entry.get('input_id') == input_id))
            elif input_id and entry.get('input_id') == input_id and entry.get('archived_chars') == len(C1_TURN):
                named.append(('archived', True))
        omitted = body.get('omitted') if isinstance(body, dict) else None
        if (isinstance(omitted, dict) and input_id
                and input_id in (omitted.get('first_input_id'), omitted.get('last_input_id'))):
            named.append(('omitted', True))
        for form, by_id in named:
            if FORMS.index(form) > FORMS.index(found['form']):
                found['form'] = form
            found['id_named'] = found['id_named'] or by_id
    return found


class Fixture:
    """Owned loopback SSE provider. Scripts decide from the host results they can see; the campaign shares
    only scenario phases (which files to read, which recall run is current)."""

    def __init__(self):
        self.lock = threading.RLock()
        self.records, self.errors, self.anomalies = [], [], []
        self.seen, self.shared, self.counts = {}, {'phase': None}, {}
        self.holding = {'s1': threading.Event()}
        self.release = {'s1': threading.Event()}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    reply = fixture.respond(body)
                except Exception as exc:
                    with fixture.lock:
                        fixture.errors.append(bounded(exc))
                    try:
                        self.send_error(500)
                    except Exception:
                        pass
                    return
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': reply.delta}]}).encode() + b'\n\n')
                    if reply.usage is not None:
                        self.wfile.write(b'data: ' + json.dumps({'choices': [], 'usage': reply.usage}).encode() + b'\n\n')
                    self.wfile.write(b'data: [DONE]\n\n')
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def start(self):
        self.thread.start()

    def stop(self):
        for event in self.release.values():
            event.set()
        self.server.shutdown()
        self.server.server_close()

    def state(self, tag):
        with self.lock:
            return self.seen.setdefault(tag, {})

    def share(self, **values):
        with self.lock:
            self.shared.update(values)

    def requests(self, **match):
        with self.lock:
            return [dict(r) for r in self.records if all(r.get(k) == v for k, v in match.items())]

    def anomaly(self, tag, step, reason):
        with self.lock:
            self.anomalies.append({'scenario': tag, 'step': step, 'reason': reason[:300]})

    @staticmethod
    def classify(messages):
        for message in messages:
            text = user_text(message)
            if text and text.startswith(ASSIGNMENT):
                node, _ = json.JSONDecoder().raw_decode(text[len(ASSIGNMENT):])
                found = TAG.search(str(node.get('task', '')))
                return 'child', found.group(1) if found else None
        index = goal_index(messages)
        found = TAG.search(messages[index]['content']) if index is not None else None
        return 'root', found.group(1) if found else None

    def respond(self, body):
        model = body.get('model')
        if model not in MODELS:
            raise AssertionError('Unexpected model route reached the fixture.')
        raw = json.dumps(body)
        if '_pebble_' in raw:
            raise AssertionError('Host-private message fields reached the provider.')
        messages = body.get('messages') or []
        names = sorted(t['function']['name'] for t in body.get('tools', []))
        role, tag = self.classify(messages)
        step = sum(1 for m in (messages if role == 'child' else after_goal(messages)) if m.get('role') == 'assistant')
        text = json.dumps(messages)
        present = sorted(name for name, marker in WATCH.items() if marker in text)
        # Which message carried each watched marker (role, name, a short prefix): the channel, not the content.
        sources = {name: sorted({f"{m.get('role')}:{m.get('name') or ''}:{str(m.get('content'))[:20]}"
                                 for m in messages if WATCH[name] in json.dumps(m)})[:4] for name in present}
        with self.lock:
            record = {'scenario': tag, 'role': role, 'model': model, 'step': step, 'tools': names,
                      'phase': self.shared.get('phase'), 'time': time.time(), 'markers': present,
                      'marker_sources': sources}
            self.records.append(record)
        if model == MODEL_B:
            script = self.route_b
        else:
            script = getattr(self, ('child_' if role == 'child' else 'script_') + str(tag), None)
        if script is None:
            self.anomaly(tag, step, 'unscripted conversation')
            reply = Reply(final('Synthetic fixture: no script.'))
        else:
            reply = script(step, messages, names)
        # Plausible provider-reported counts, so calibration from reported usage stays realistic.
        reply.usage = {'prompt_tokens': max(1, len(raw) // 4), 'completion_tokens': 30}
        with self.lock:
            record['emitted'] = ([c['function']['name'] for c in reply.delta['tool_calls']]
                                 if 'tool_calls' in reply.delta else 'content')
        return reply

    # Memory scripts -------------------------------------------------------------------

    def _read_then_answer(self, tag, messages, files):
        if not any(d['name'] == 'Read' for d in history(messages)):
            return Reply(calls(*[('Read', f'{tag}-read-{index}', {'file_path': name}) for index, name in enumerate(files)]))
        return Reply(final('Read ' + ' and '.join(files) + '.'))

    def script_m1(self, step, messages, names):
        with self.lock:
            files = list(self.shared.get('m1_files') or M1_FILES)
        return self._read_then_answer('m1', messages, files)

    def script_m2(self, step, messages, names):
        return self._read_then_answer('m2', messages, ['prod.yaml'])

    def script_m3(self, step, messages, names):
        with self.lock:
            learning = self.shared.get('phase') == 'm3-learn'
        if learning:
            return self._read_then_answer('m3', messages, ['notes.yaml'])
        return Reply(final('Answered from this conversation only.'))

    # Handoff, constraints, delegation and source scripts -------------------------------

    def route_b(self, step, messages, names):
        return Reply(final('Route B continued the task.'))

    def script_c1(self, step, messages, names):
        index = goal_index(messages)
        goal = messages[index]['content'] if index is not None else ''
        with self.lock:
            request = self.counts[goal] = self.counts.get(goal, -1) + 1
            input_id = self.shared.get('c1_input_id')
        if C1_HEAD in goal:
            return Reply(final('Noted the constraints. ' + C1_ASSISTANT))
        found = re.search(r'pass (\d+)', goal)
        number = int(found.group(1)) if found else None
        form = constraint_form(messages, input_id)
        with self.lock:
            self.state('c1').setdefault('forms', []).append({'pass': number, 'request': request,
                                                             'time': time.time(), **form})
        if request < C1_READS:
            return Reply(calls(('Read', f'c1-read-{number}-{request}',
                                {'file_path': 'big.txt', 'offset': 1 + 150 * (request % 6), 'limit': 150})))
        return Reply(final('Summarised the pages. ' + C1_ASSISTANT))

    def _spawn_once(self, tag, step, messages, extra):
        if step == 0:
            return Reply(calls(('SpawnAgent', tag + '-spawn', {'task': CHILD_TASK + ' ' + MARK + tag, 'name': 'reader',
                                                               **extra})))
        return Reply(final('Continuing without a child.'))

    def script_d1(self, step, messages, names):
        return self._spawn_once('d1', step, messages, {'max_steps': 1})

    def script_d2(self, step, messages, names):
        return self._spawn_once('d2', step, messages, {})

    def script_d3(self, step, messages, names):
        seen, done = self.state('d3'), history(messages)
        spawn = next((d for d in done if d['name'] == 'SpawnAgent'), None)
        if spawn is None:
            return Reply(calls(('SpawnAgent', 'd3-spawn', {'task': CHILD_TASK + ' ' + MARK + 'd3', 'name': 'reader'})))
        result = spawn['result'] if isinstance(spawn['result'], dict) else {}
        agent, generation = result.get('agent_id'), result.get('generation') or 1
        if not agent:
            self.anomaly('d3', step, 'spawn admitted no child: ' + str(result.get('error'))[:200])
            return Reply(final('No child was admitted.'))
        waits = [d for d in done if d['name'] == 'WaitForAgents']
        with self.lock:
            settled = seen.get('child_final')
        if not waits or not settled and len(waits) < 4:
            return Reply(calls(('WaitForAgents', f'd3-wait-{len(waits)}', {'agent_ids': json.dumps([agent])})))
        reviews = [d for d in done if d['name'] == 'ReviewAgentResult']
        bogus = [d for d in reviews if d['args'].get('evidence_ids')]
        if len(bogus) < D3_BOGUS_REVIEWS:
            number = len(bogus) + 1
            return Reply(calls(('ReviewAgentResult', f'd3-review-{number}', {
                'agent_id': agent, 'generation': generation, 'evidence_ids': json.dumps([f'd3-not-a-receipt-{number}'])})))
        if len(reviews) == len(bogus):
            return Reply(calls(('ReviewAgentResult', 'd3-accept', {'agent_id': agent, 'generation': generation})))
        if step > 16:
            self.anomaly('d3', step, 'step beyond script')
        return Reply(final('Reviewed and accepted the child result.'))

    def child_d3(self, step, messages, names):
        if step == 0:
            return Reply(calls(('Read', 'd3-child-read', {'file_path': 'notes.txt'})))
        with self.lock:
            self.state('d3')['child_final'] = True
        return Reply(final(D3_REPORT))

    def script_s1(self, step, messages, names):
        seen = self.state('s1')
        checks = [text[:600] for text in (user_text(m) for m in after_goal(messages))
                  if text and text.startswith('HOST COMPLETION CHECK')]
        with self.lock:
            seen['completion_checks'] = checks[-4:]
        if step == 0:
            return Reply(calls(('Read', 's1-read', {'file_path': 'facts.txt'})))
        if not self.release['s1'].is_set():
            # Held until the validator changed the file, so the change lands between the read and settlement.
            self.holding['s1'].set()
            released = self.release['s1'].wait(HOLD_SECONDS)
            with self.lock:
                seen['released'] = released
        return Reply(final('The release name is Aurora.'))


class Campaign:
    def __init__(self, command, backend):
        self.command, self.backend = command, backend
        self.fixture = Fixture()
        self.checks, self.names = [], set()
        self.facts, self.observations = {}, {'run_states': {}, 'scenario_seconds': {}}
        self.fatal = None
        self.stderr_tails = []
        self.reservation = None

    # Check bookkeeping -----------------------------------------------------------------

    def check(self, name, fn):
        assert name not in self.names, 'Check names must be unique: ' + name
        self.names.add(name)
        try:
            evidence = fn() or {}
            self.checks.append({'name': name, 'passed': True, 'evidence': evidence})
            return True
        except Exception as exc:
            self.checks.append({'name': name, 'passed': False, 'error': bounded(exc, 2000),
                                'trace': traceback.format_exc(limit=4)[-1500:]})
            return False

    def passing(self):
        return all(c['passed'] for c in self.checks)

    def need(self, key):
        if key not in self.facts:
            raise AssertionError('Prerequisite was not established: ' + key)
        return self.facts[key]

    def seen(self, tag):
        return self.fixture.state(tag)

    # Service helpers --------------------------------------------------------------------

    def boot(self, home):
        return AllowlistedService(self.command, home)

    def close(self, service, keep_tail):
        if keep_tail:
            try:
                service.stderr.seek(0)
                self.stderr_tails.append(service.stderr.read()[-4000:])
            except Exception as exc:
                self.stderr_tails.append('stderr unavailable: ' + bounded(exc))
        service.close()

    @staticmethod
    def call(service, method, **params):
        """A native route; a route the service does not have fails clearly, by name."""
        try:
            return service.rpc(method, **params)
        except RuntimeError as exc:
            if str(exc) == 'Unknown request.':
                raise AssertionError('The native route is missing: ' + method) from None
            raise

    @staticmethod
    def refused(service, method, **params):
        """An expected refusal; a missing route is not one."""
        error = rejected(service, method, **params)
        if error == 'Unknown request.':
            raise AssertionError('The native route is missing: ' + method)
        return error

    @staticmethod
    def until(predicate, seconds, label):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(f'Timed out after {seconds}s waiting for {label}.')

    def settle(self, service, cid, rid, tag, seconds=SETTLE_SECONDS):
        seen = {}

        def probe():
            seen['run'] = run = service.rpc('run_get', id=cid, run_id=rid)
            if run['state'] in TRANSIENT or service.rpc('state')['active']:
                return None
            return run
        try:
            run = self.until(probe, seconds, tag + ' to settle')
        except AssertionError:
            with self.fixture.lock:
                detail = {'state': (seen.get('run') or {}).get('state'), 'provider_errors': self.fixture.errors[-3:],
                          'anomalies': [a for a in self.fixture.anomalies if a['scenario'] == tag][-3:]}
            with contextlib.suppress(Exception):
                service.rpc('cancel', id=cid, run_id=rid)
            raise AssertionError(json.dumps(detail)) from None
        self.observations['run_states'].setdefault(tag, []).append(run['state'])
        return run

    def mode(self, service, mode):
        deadline = time.monotonic() + 10
        while True:
            try:
                return service.rpc('setting', key='permissions', value=permissions(mode))
            except RuntimeError as exc:
                if not str(exc).startswith('Stop ') or time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def new_chat(self, service, root):
        cid = service.rpc('new_chat')['id']
        service.rpc('set_workspace', id=cid, path=str(root))
        return cid

    def send(self, service, tag, text, root, chat=None, steps=24):
        self.mode(service, 'ask')
        cid = chat or self.new_chat(service, root)
        rid = service.rpc('send', id=cid, text=text + ' ' + MARK + tag,
                          limits={'max_steps': steps, 'max_seconds': 300})['run_id']
        return {'cid': cid, 'rid': rid}

    def run(self, service, tag, text, root, chat=None, steps=24):
        fact = self.send(service, tag, text, root, chat, steps)
        fact['state'] = self.settle(service, fact['cid'], fact['rid'], tag)['state']
        return fact

    @staticmethod
    def receipts(service, fact, name):
        return [t for t in service.rpc('run_get', id=fact['cid'], run_id=fact['rid'])['tools'] if t['name'] == name]

    def memory_state(self, service, home):
        """Everything an archive import could change in one home, without content."""
        rows = sorted((r['id'], r.get('learning_status') or r.get('status')) for r in service.rpc('memory_list'))
        with readonly(home) as db:
            counts = [db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                      for table in ('memory_tombstones', 'memory_tombstone_blooms', 'memory_scope_salts')]
        return rows, counts

    def timed(self, name, scenario, *args):
        started = time.monotonic()
        try:
            return scenario(*args)
        finally:
            self.observations['scenario_seconds'][name] = round(time.monotonic() - started, 1)

    # Campaign ------------------------------------------------------------------------------

    def run_all(self, scratch):
        home, home2 = scratch / 'home', scratch / 'home2'
        home.mkdir()
        home2.mkdir()
        self.facts['home'], self.facts['home2'] = home, home2
        self.check('fixture_projects_created_outside_runtime', lambda: self.projects(scratch))
        # Route A's port: owned by the validator and never listening, so every connection is refused locally.
        self.reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.reservation.bind(('127.0.0.1', 0))
        self.facts['cloud_url'] = 'https://0.0.0.0:%d' % self.reservation.getsockname()[1]
        self.fixture.start()
        try:
            service = self.boot(home)
            try:
                if self.setup(service, primary=True):
                    for name, scenario in (('m1', self.scenario_m1), ('m3', self.scenario_m3),
                                           ('m2_export', self.scenario_m2_export), ('s1', self.scenario_s1),
                                           ('d1', self.scenario_d1), ('c1', self.scenario_c1),
                                           ('h1', self.scenario_h1)):
                        self.timed(name, scenario, service)
            finally:
                self.close(service, keep_tail=not self.passing())
            service = self.boot(home)
            try:
                self.timed('m1_restart', self.scenario_m1_restart, service)
            finally:
                self.close(service, keep_tail=not self.passing())
            service = self.boot(home2)
            try:
                if self.setup(service, primary=False):
                    self.timed('m2_import', self.scenario_m2_import, service)
            finally:
                self.close(service, keep_tail=not self.passing())
            self.final_checks()
        except Exception as exc:
            self.fatal = bounded(exc, 3000)
        finally:
            self.fixture.stop()
            with contextlib.suppress(Exception):
                self.reservation.close()

    def projects(self, scratch):
        assert not scratch.is_relative_to(ROOT), 'Fixture projects must live outside this checkout and .runtime.'
        layout = {'m1': M1_FILES, 'm3a': {'notes.yaml': f'codename: {M3_CODENAME}\n'},
                  'm3b': {'notes.yaml': 'team: platform\n'}, 'h1': {'notes.txt': 'handoff notes\n'},
                  'c1': {'big.txt': BIG}, 'd1': {'notes.txt': NOTES}, 'd2': {'notes.txt': NOTES},
                  'd3': {'notes.txt': NOTES}, 's1': {'facts.txt': S1_BEFORE}}
        for name, files in layout.items():
            root = scratch / 'projects' / name
            root.mkdir(parents=True)
            for file, text in files.items():
                (root / file).write_text(text)
            self.facts['root:' + name] = root.resolve()
        (scratch / 'archives').mkdir()
        self.facts['archives_dir'] = (scratch / 'archives').resolve()
        return {'projects': len(layout)}

    def setup(self, service, primary):
        def profile():
            status = service.rpc('model_save', name='Phase 15 fixture', url=self.fixture.url, model=MODEL, context=CONTEXT)
            service.rpc('setting', key='memory_enabled', value=True)
            state = service.rpc('state')
            assert state['guard']['available'] is True, 'The real vendored AgentGuard engine must be available.'
            assert state['settings']['memory_enabled'] is True
            self.facts['profile:main' if primary else 'profile:home2'] = status['profile_id']
            return {'guard_available': True, 'memory_enabled': True}
        name = ('loopback_profile_saved_memory_enabled_and_actual_guard_available' if primary
                else 'second_home_profile_saved_and_memory_enabled')
        return self.check(name, profile)

    # m1 ------------------------------------------------------------------------------------

    def scenario_m1(self, service):
        root = self.need('root:m1')

        def contested():
            self.fixture.share(m1_files=list(M1_FILES))
            fact = self.run(service, 'm1', GOALS['m1'], root)
            facts = semantic(service)
            keep, other = quoted(facts, 'port: 8080'), quoted(facts, 'port: 9090')
            assert len(keep) == 1 and len(other) == 1, ('Expected one learned fact per source.', [f['quote'] for f in facts])
            assert keep[0]['status'] == other[0]['status'] == 'contested', (keep[0]['status'], other[0]['status'])
            fact.update(keep=keep[0]['id'], other=other[0]['id'])
            self.facts['m1'] = fact
            return {'statuses': [keep[0]['status'], other[0]['status']], 'sources': sorted(M1_FILES)}
        if not self.check('m1_conflicting_facts_from_two_sources_are_both_contested', contested):
            return

        def withheld():
            fact = self.need('m1')
            recall = service.rpc('memory_recall', id=fact['cid'], text='configured port in dev.yaml and prod.yaml',
                                 budget=4000)
            items = recall['items']
            assert not {i.get('id') for i in items} & {fact['keep'], fact['other']}, 'A contested record was recalled.'
            assert not any(text.strip() in json.dumps(items) for text in M1_FILES.values()), \
                'Contested source text was recalled.'
            notes = [i for i in items if i.get('kind') == 'recall_note' and i.get('note') == 'conflict']
            assert notes, ('No host conflict note replaced the contested records.', recall['diagnostics'].get('withheld'))
            self.facts['m1_notes'] = notes
            return {'conflict_notes': len(notes), 'withheld': recall['diagnostics'].get('withheld')}
        self.check('m1_contested_records_are_withheld_from_recall_behind_a_host_conflict_note', withheld)

        def named():
            fact, notes = self.need('m1'), self.need('m1_notes')
            both = {fact['keep'], fact['other']}
            assert any(both <= set(n.get('memory_ids') or []) or all(i in json.dumps(n) for i in both) for n in notes), \
                ('The conflict note does not name both record IDs.', [sorted(n) for n in notes])
            return {'named': 2}
        self.check('m1_recall_conflict_note_names_both_ids', named)

        def contests():
            fact = self.need('m1')
            groups = self.call(service, 'memory_contests')['groups']
            pairs = {(g['memory_id'], c['memory_id']) for g in groups for c in g.get('counterparts', [])}
            assert (fact['keep'], fact['other']) in pairs or (fact['other'], fact['keep']) in pairs, \
                ('The contest group does not pair both records.', len(groups))
            assert all(len(g['content']) <= 2000 and all(len(c['content']) <= 2000 for c in g['counterparts'])
                       for g in groups), 'Contest content is not bounded.'
            return {'groups': len(groups), 'paired': True}
        self.check('m1_memory_contests_lists_the_group_with_both_records', contests)

        def keep():
            fact = self.need('m1')
            result = self.call(service, 'memory_resolve_contest', memory_id=fact['keep'], decision='keep')
            assert fact['other'] in result['forgotten'] and fact['keep'] not in result['forgotten'], result
            assert result.get('status') == 'active' and result.get('authorization') == 'none', result
            facts = semantic(service)
            assert [f['status'] for f in facts if f['id'] == fact['keep']] == ['active']
            assert not [f for f in facts if f['id'] == fact['other']] and not quoted(facts, 'port: 9090')
            groups = self.call(service, 'memory_contests')['groups']
            assert not {g['memory_id'] for g in groups} & {fact['keep'], fact['other']}, 'The contest is still listed.'
            self.facts['m1_kept'] = True
            return {'forgotten': len(result['forgotten']), 'kept_status': 'active'}
        if not self.check('m1_keep_forgets_the_counterpart_and_clears_the_contest', keep):
            return

        def recalled():
            fact = self.need('m1')
            recall = service.rpc('memory_recall', id=fact['cid'], text='configured port in dev.yaml', budget=4000)
            assert fact['keep'] in {i.get('id') for i in recall['items']}, recall['diagnostics'].get('records')
            return {'recalled': True}
        self.check('m1_kept_record_is_recalled_after_resolution', recalled)

        def relearn():
            fact = self.need('m1')
            self.fixture.share(m1_files=['prod.yaml'])
            again = self.run(service, 'm1', GOALS['m1_again'], root, chat=fact['cid'])
            # The model's Read; above 8K the host's prefetch also reads the named prod.yaml (Phase 46), and a forgotten
            # fact must not return through either read.
            reads = [t for t in self.receipts(service, again, 'Read') if not t['call_id'].startswith('host:')]
            assert [t['state'] for t in reads] == ['completed'], 'prod.yaml was not read.'
            assert not quoted(semantic(service), 'port: 9090'), 'The forgotten fact was learned again.'
            return {'reread': 'prod.yaml', 'relearned': False}
        self.check('m1_forgotten_counterpart_is_not_relearned_from_the_same_source', relearn)

    def scenario_m1_restart(self, service):
        def after():
            fact = self.need('m1')
            self.need('m1_kept')
            facts = semantic(service)
            assert [f['status'] for f in facts if f['id'] == fact['keep']] == ['active']
            assert not [f for f in facts if f['id'] == fact['other']] and not quoted(facts, 'port: 9090')
            groups = self.call(service, 'memory_contests')['groups']
            assert not {g['memory_id'] for g in groups} & {fact['keep'], fact['other']}
            self.fixture.share(m1_files=['prod.yaml'])
            self.run(service, 'm1', GOALS['m1_again'], self.need('root:m1'), chat=fact['cid'])
            assert not quoted(semantic(service), 'port: 9090'), 'The forgotten fact returned after a restart.'
            return {'restarted': True, 'relearned': False}
        self.check('m1_forgotten_counterpart_stays_forgotten_after_restart', after)

    # m3 ------------------------------------------------------------------------------------

    def recall_has(self, service, cid):
        return M3_CODENAME in json.dumps(service.rpc('memory_recall', id=cid, text='project codename', budget=4000)['items'])

    def model_run_sees(self, service, phase, root):
        self.fixture.share(phase=phase)
        cid = self.new_chat(service, root)
        native = self.recall_has(service, cid)
        self.run(service, 'm3', GOALS['m3'], root, chat=cid)
        requests = self.fixture.requests(phase=phase, scenario='m3')
        assert requests, 'The recall run made no model request.'
        # Recall only: above 8K the host's prefetch reads the named notes.yaml into its own message (Phase 46), and the
        # folder's own file read fresh is not a recall, so a marker counts only from another message.
        recalled = any(any(not s.startswith('user:pebble_prefetch:') for s in (r.get('marker_sources') or {}).get('m3', ()))
                       for r in requests)
        return native, recalled, len(requests)

    def scenario_m3(self, service):
        root_a, root_b = self.need('root:m3a'), self.need('root:m3b')

        def learned():
            self.fixture.share(phase='m3-learn')
            self.run(service, 'm3', GOALS['m3'], root_a)
            mine = quoted(semantic(service), 'codename: ' + M3_CODENAME)
            assert len(mine) == 1 and mine[0]['status'] == 'active', mine
            native, model, requests = self.model_run_sees(service, 'm3-recall-a', root_a)
            assert native and model, ('Positive control failed: project A does not recall its own fact.', native, model)
            self.facts['m3'] = mine[0]['id']
            return {'native_recall': native, 'model_request_carries_fact': model, 'requests': requests}
        if not self.check('m3_fact_learned_in_project_a_is_recalled_there', learned):
            return

        def other():
            self.need('m3')
            native, model, requests = self.model_run_sees(service, 'm3-recall-b', root_b)
            assert not native and not model, ('A project A fact reached project B.', native, model)
            return {'native_recall': native, 'model_request_carries_fact': model, 'requests': requests}
        self.check('m3_fact_from_project_a_is_never_recalled_in_project_b', other)

        def replaced():
            self.need('m3')
            os.rename(root_a, root_a.with_name('m3a-replaced-original'))
            root_a.mkdir()
            (root_a / 'notes.yaml').write_text(f'codename: {M3_CODENAME}\n')  # identical content, new folder identity
            native, model, requests = self.model_run_sees(service, 'm3-recall-replaced', root_a)
            assert not native and not model, ('A fact from the replaced folder was recalled.', native, model)
            return {'same_path': True, 'native_recall': native, 'model_request_carries_fact': model, 'requests': requests}
        self.check('m3_fact_from_project_a_is_never_recalled_in_a_replaced_folder_at_the_same_path', replaced)
        self.fixture.share(phase=None)

    # m2 ------------------------------------------------------------------------------------

    def scenario_m2_export(self, service):
        folder = self.need('archives_dir')

        def exported():
            self.need('m1_kept')  # the forgotten counterpart left a tombstone
            service.rpc('memory_save', kind='profile', content=M2_PROFILE + ' prefers short answers.', scope='global')
            first, second = folder / 'm2-first.json', folder / 'm2-second.json'
            results = [self.call(service, 'memory_archive_export_to', path=str(path)) for path in (first, second)]
            for result, path in zip(results, (first, second)):
                data = path.read_bytes()
                assert result['path'] == str(path) and result['sha256'] == sha(data) and result['bytes'] == len(data), result
                assert result['counts']['records'] >= 2 and result.get('authorization') == 'none', result['counts']
            before = first.read_bytes()
            error = rejected(service, 'memory_archive_export_to', path=str(first))
            assert first.read_bytes() == before, 'An existing archive was overwritten.'
            self.facts['archives'] = {'first': first, 'second': second}
            return {'counts': results[0]['counts'], 'existing_path_refused': error[:120]}
        if not self.check('m2_archive_exported_to_new_paths_and_an_existing_path_is_refused', exported):
            return

        def contents():
            archive = json.loads(self.need('archives')['first'].read_bytes())
            records = archive['sections']['records']
            quotes = []
            for record in records:
                with contextlib.suppress(TypeError, ValueError, AttributeError):
                    quotes.append(json.loads(record['content']).get('quote'))
            assert 'port: 8080' in quotes and 'port: 9090' not in quotes, 'The forgotten record travelled.'
            assert archive['sections']['tombstones'], 'The archive carries no tombstone.'
            assert any(r['kind'] == 'profile' and M2_PROFILE in r['content'] for r in records)
            return {'records': len(records), 'tombstones': len(archive['sections']['tombstones'])}
        self.check('m2_archive_carries_the_tombstone_but_never_the_forgotten_record', contents)

    def scenario_m2_import(self, service):
        home2, root = self.need('home2'), self.need('root:m1')
        archives = self.need('archives')

        def learned():
            layers = dict.fromkeys(LAYERS, True)
            layers['profile'] = False
            service.rpc('setting', key='memory_layers', value=layers)
            cid = self.new_chat(service, root)  # the same project path as the first home
            self.run(service, 'm2', GOALS['m2'], root, chat=cid)
            mine = quoted(semantic(service), 'port: 9090')
            assert len(mine) == 1 and mine[0]['status'] == 'active', mine
            self.facts['m2'] = {'cid': cid, 'local_other': mine[0]['id'], 'layers': layers}
            return {'local_counterpart_learned': True, 'profile_layer': 'disabled'}
        if not self.check('m2_second_home_learned_the_counterpart_before_import', learned):
            return

        def preview():
            m2 = self.need('m2')
            plan = self.call(service, 'memory_archive_preview_file', path=str(archives['first']))
            counts = plan['counts']
            text = json.dumps(plan)
            assert M2_PROFILE not in text and 'port: 8080' not in text and 'port: 9090' not in text, 'The preview carries content.'
            assert counts['add'] >= 1 and counts['skip_disabled'] >= 1 and counts['tombstones'] >= 1, counts
            assert m2['local_other'] in plan['forgets_local'], 'The preview does not list the local copy it forgets.'
            assert plan.get('authorization') == 'none' and re.fullmatch(r'[0-9a-f]{64}', plan['plan_digest'])
            self.facts['m2_digest'] = plan['plan_digest']
            return {'counts': counts, 'forgets_local': len(plan['forgets_local'])}
        self.check('m2_preview_is_counts_only_and_lists_forgetting_and_the_disabled_layer', preview)

        def file_changed():
            self.need('m2')
            plan = self.call(service, 'memory_archive_preview_file', path=str(archives['second']))
            before = self.memory_state(service, home2)
            archives['second'].write_bytes(archives['first'].read_bytes())  # another valid archive, after the preview
            error = self.refused(service, 'memory_archive_import_file', path=str(archives['second']),
                                 plan_digest=plan['plan_digest'])
            assert self.memory_state(service, home2) == before, 'A refused import changed memory.'
            return {'refused': error[:160]}
        self.check('m2_import_is_refused_when_the_file_changed_after_preview', file_changed)

        def state_changed():
            m2 = self.need('m2')
            plan = self.call(service, 'memory_archive_preview_file', path=str(archives['first']))
            before = self.memory_state(service, home2)
            service.rpc('setting', key='memory_layers', value={**m2['layers'], 'semantic': False})
            try:
                error = self.refused(service, 'memory_archive_import_file', path=str(archives['first']),
                                     plan_digest=plan['plan_digest'])
            finally:
                service.rpc('setting', key='memory_layers', value=m2['layers'])
            assert self.memory_state(service, home2) == before, 'A refused import changed memory.'
            return {'refused': error[:160]}
        self.check('m2_import_is_refused_when_local_state_changed_after_preview', state_changed)

        def tampered():
            self.need('m2')
            path = archives['first'].with_name('m2-tampered.json')
            path.write_bytes(flip(archives['first'].read_bytes(), M2_PROFILE))
            before = self.memory_state(service, home2)
            previewed = self.refused(service, 'memory_archive_preview_file', path=str(path))
            imported = self.refused(service, 'memory_archive_import_file', path=str(path),
                                    plan_digest=self.facts.get('m2_digest', '0' * 64))
            assert self.memory_state(service, home2) == before, 'A tampered archive changed memory.'
            assert 'checksum' in previewed, previewed
            return {'preview_refused': previewed[:160], 'import_refused': imported[:160]}
        self.check('m2_tampered_archive_is_refused', tampered)

        def imported():
            m2 = self.need('m2')
            plan = self.call(service, 'memory_archive_preview_file', path=str(archives['first']))
            applied = self.call(service, 'memory_archive_import_file', path=str(archives['first']),
                                plan_digest=plan['plan_digest'])['applied']
            assert applied['forgotten'] >= 1 and applied['imported'] >= 1, applied
            facts = semantic(service)
            assert not [f for f in facts if f['id'] == m2['local_other']] and not quoted(facts, 'port: 9090')
            kept = quoted(facts, 'port: 8080')
            assert kept and all(f['status'] == 'needs_revalidation' for f in kept), kept
            assert not [r for r in service.rpc('memory_list', kind='profile') if M2_PROFILE in r['content']], \
                'The disabled profile layer was filled.'
            self.facts['m2_imported'] = True
            return {'imported': applied['imported'], 'forgotten': applied['forgotten'],
                    'digest_unchanged_since_first_preview': plan['plan_digest'] == self.facts.get('m2_digest')}
        if not self.check('m2_import_applies_the_tombstone_first_and_fills_no_disabled_layer', imported):
            return

        def not_relearned():
            m2 = self.need('m2')
            self.run(service, 'm2', GOALS['m2'], root, chat=m2['cid'])
            assert not quoted(semantic(service), 'port: 9090'), 'The forgotten fact was learned again after import.'
            return {'relearned': False}
        self.check('m2_forgotten_record_stays_forgotten_after_import', not_relearned)

    # h1 ------------------------------------------------------------------------------------

    def scenario_h1(self, service):
        try:
            self._h1(service)
        finally:
            with contextlib.suppress(Exception):
                service.rpc('model_select', profile_id=self.facts['profile:main'])

    def _h1(self, service):
        root = self.need('root:h1')

        def runs():
            b = service.rpc('model_save', name='Phase 15 route B', url=self.fixture.url, model=MODEL_B,
                            context=CONTEXT)['profile_id']
            cid = self.new_chat(service, root)
            first = self.run(service, 'h1', GOALS['h1_device'], root, chat=cid)
            device = [m['id'] for m in service.rpc('get_chat', id=cid)['messages']]
            a = service.rpc('model_save', name='Phase 15 route A', url=self.need('cloud_url'), model=MODEL_A,
                            context=CONTEXT)['profile_id']
            second = self.run(service, 'h1', GOALS['h1_cloud'], root, chat=cid)
            messages = service.rpc('get_chat', id=cid)['messages']
            cloud = [m['id'] for m in messages if m['id'] not in device]
            assert any(H1_DEVICE in m['content'] for m in messages if m['id'] in device), 'Route B history is missing.'
            assert any(H1_CLOUD in m['content'] for m in messages if m['id'] in cloud), 'Route A history is missing.'
            self.facts['h1'] = {'cid': cid, 'a': a, 'b': b, 'device_ids': device, 'cloud_ids': cloud,
                                'device_run': first['rid'], 'cloud_run': second['rid']}
            return {'route_b_state': first['state'], 'route_a_state': second['state'], 'device_messages': len(device),
                    'cloud_messages': len(cloud)}
        if not self.check('h1_runs_on_device_route_b_then_cloud_route_a_recorded', runs):
            return

        def preview():
            h = self.need('h1')
            plan = self.call(service, 'model_handoff_preview', id=h['cid'], run_id=h['cloud_run'], profile_id=h['b'])
            text = json.dumps(plan)
            assert H1_DEVICE not in text and H1_CLOUD not in text, 'The preview carries message content.'
            assert plan['source_class'].startswith('cloud:') and plan['destination_class'] == 'device', \
                (plan['source_class'], plan['destination_class'])
            assert set(h['cloud_ids']) <= set(plan['withheld_ids']), 'Cross-class history is not withheld.'
            assert not set(h['device_ids']) & set(plan['withheld_ids']), 'Same-class history was withheld.'
            counts = plan['counts']
            assert all(isinstance(v, dict) and all(type(n) is int for n in v.values()) for v in counts.values()), counts
            assert set(cls for v in counts.values() for cls in v) == {'device'}, counts
            assert sum(n for v in counts.values() for n in v.values()) == len(h['device_ids']), counts
            h['plan'] = plan['plan_fingerprint']
            return {'source_class': 'cloud', 'destination_class': plan['destination_class'], 'counts': counts,
                    'withheld': len(plan['withheld_ids'])}
        if not self.check('h1_preview_is_counts_only_and_withholds_the_cross_class_history', preview):
            return

        def changed():
            h = self.need('h1')
            approved = self.call(service, 'model_handoff_approve', id=h['cid'], plan_fingerprint=h['plan'])
            assert approved['approved'] is True and approved['plan_fingerprint'] == h['plan'], approved
            service.rpc('model_save', name='Phase 15 route B', url=self.fixture.url, model=MODEL_B,
                        context=CHANGED_CONTEXT, profile_id=h['b'])
            service.rpc('model_select', profile_id=h['a'])
            sent = len(self.fixture.requests(model=MODEL_B))
            chats = {c['id'] for c in service.rpc('state')['chats']}
            error = self.refused(service, 'model_handoff_start', id=h['cid'], plan_fingerprint=h['plan'])
            time.sleep(1.0)
            state = service.rpc('state')
            assert len(self.fixture.requests(model=MODEL_B)) == sent, 'Route B received a request after a refused start.'
            assert state['active'] is None and {c['id'] for c in state['chats']} == chats, 'A refused start created work.'
            assert 'destination_changed' in error, error
            return {'refused': error[:160], 'requests_after_refusal': 0}
        self.check('h1_changed_destination_between_approval_and_start_is_refused_and_nothing_is_sent', changed)

        def started():
            h = self.need('h1')
            plan = self.call(service, 'model_handoff_preview', id=h['cid'], run_id=h['cloud_run'], profile_id=h['b'])
            self.call(service, 'model_handoff_approve', id=h['cid'], plan_fingerprint=plan['plan_fingerprint'])
            service.rpc('model_select', profile_id=h['a'])  # the person is on route A; start must route to B itself
            mark = len(self.fixture.requests(model=MODEL_B))
            result = self.call(service, 'model_handoff_start', id=h['cid'], plan_fingerprint=plan['plan_fingerprint'])
            assert result['run_id'] not in (h['device_run'], h['cloud_run']), 'The handoff did not create a new run.'
            run = self.settle(service, result['chat_id'], result['run_id'], 'h1')
            after = self.fixture.requests(model=MODEL_B)[mark:]
            assert after, 'The new run never reached route B.'
            self.facts['h1_after'] = after
            return {'new_run': True, 'state': run['state'], 'route_b_requests': len(after), 'sent': result.get('sent'),
                    'withheld': result.get('withheld')}
        if not self.check('h1_approved_start_creates_a_new_run_on_route_b', started):
            return

        def never():
            after = self.need('h1_after')
            leaks = [r['marker_sources']['h1_cloud'] for r in after if 'h1_cloud' in r['markers']]
            assert not leaks, ('Route B received withheld text through these messages.', leaks[:3])
            return {'requests_checked': len(after)}
        self.check('h1_route_b_never_receives_withheld_text', never)

        def released():
            after = self.need('h1_after')
            assert any('h1_device' in r['markers'] for r in after), 'Route B did not receive the released history.'
            return {'released_history_seen': True}
        self.check('h1_route_b_receives_the_released_device_history', released)

    # c1 ------------------------------------------------------------------------------------

    def scenario_c1(self, service):
        try:
            self._c1(service)
        finally:
            with contextlib.suppress(Exception):
                service.rpc('model_select', profile_id=self.facts['profile:main'])

    def _c1(self, service):
        root = self.need('root:c1')

        def recorded():
            service.rpc('model_save', name='Phase 15 small window', url=self.fixture.url, model=MODEL_SMALL,
                        context=SMALL_CONTEXT)
            cid = self.new_chat(service, root)
            first = self.run(service, 'c1', CONSTRAINT, root, chat=cid, steps=6)
            ids = [m['id'] for m in service.rpc('get_chat', id=cid)['messages']
                   if m['role'] == 'user' and C1_HEAD in m['content']]
            assert len(ids) == 1 and len(CONSTRAINT) > 2200, (len(ids), len(CONSTRAINT))
            self.fixture.share(c1_input_id=ids[0])
            self.facts['c1'] = {'cid': cid, 'input_id': ids[0]}
            return {'chars': len(CONSTRAINT), 'state': first['state']}
        if not self.check('c1_long_early_human_constraint_recorded_on_the_small_window_profile', recorded):
            return

        def compaction():
            c = self.need('c1')
            runs = []
            for number in (1, 2, 3):
                fact = self.run(service, 'c1', GOALS['c1'] % number, root, chat=c['cid'], steps=C1_READS + 4)
                run = service.rpc('run_get', id=c['cid'], run_id=fact['rid'])
                runs.append({'pass': number, 'state': run['state'],
                             'compactions': [row['created'] for row in run.get('compactions') or []]})
            self.facts['c1_runs'] = runs
            total = sum(len(r['compactions']) for r in runs)
            assert total >= 2, ('Compaction did not repeat on the small window.', [len(r['compactions']) for r in runs])
            return {'compactions': [len(r['compactions']) for r in runs], 'states': [r['state'] for r in runs]}
        self.check('c1_repeated_compaction_on_the_small_window_profile', compaction)

        def carried():
            runs = self.need('c1_runs')
            forms = [f for f in self.seen('c1').get('forms', []) if f['pass'] in (1, 2, 3)]
            assert forms, 'No later request was recorded.'
            absent = [(f['pass'], f['request']) for f in forms if f['form'] == 'absent']
            assert not absent, ('A later model request lost the constraint.', len(absent), 'of', len(forms),
                                'requests; with a constraints unit:', sum(1 for f in forms if f['unit']), absent[:6])
            first = {r['pass']: min(r['compactions']) for r in runs if r['compactions']}
            after = [f for f in forms if f['pass'] in first and f['time'] >= first[f['pass']]]
            assert after, 'No request was recorded after a compaction.'
            return {'requests': len(forms), 'after_compaction': len(after),
                    'forms': sorted({f['form'] for f in forms}), 'input_id_named': all(f['id_named'] for f in forms
                                                                                       if f['form'] != 'verbatim')}
        self.check('c1_every_later_model_request_carries_the_constraint_verbatim_or_marked', carried)

        def human_only():
            self.need('c1_runs')
            forms = [f for f in self.seen('c1').get('forms', []) if f['pass'] in (1, 2, 3)]
            assert forms and not any(f['leak'] for f in forms), 'Assistant text entered the human constraints unit.'
            return {'requests_with_unit': sum(1 for f in forms if f['unit'])}
        self.check('c1_constraints_unit_holds_no_assistant_text', human_only)

    # d1 ------------------------------------------------------------------------------------

    def refusal(self, service, fact, limit):
        [receipt] = self.receipts(service, fact, 'SpawnAgent')
        result = receipt['result'] if isinstance(receipt['result'], dict) else {}
        refusal = result.get('refusal') or {}
        assert result.get('status') == 'refused' and refusal.get('kind') == 'admission', result.get('error') or result
        assert refusal.get('limit') == limit, refusal
        needed, available = refusal.get('needed'), refusal.get('available')
        assert type(needed) is int and type(available) is int and needed > available, refusal
        assert str(needed) in result['error'] and str(available) in result['error'], result['error']
        with readonly(self.need('home')) as db:
            nodes = db.execute('SELECT count(*) FROM agent_nodes WHERE run_id=?', (fact['rid'],)).fetchone()[0]
        assert nodes == 0, 'A refused spawn created a child.'
        return {'limit': limit, 'needed': needed, 'available': available, 'children': 0}

    def scenario_d1(self, service):
        self.check('d1_spawn_with_a_child_budget_below_the_step_floor_is_refused_with_numbers',
                   lambda: self.refusal(service, self.run(service, 'd1', GOALS['d1'], self.need('root:d1')), 'child_steps'))
        self.check('d1_spawn_when_the_shared_pool_is_below_the_floor_is_refused_with_numbers',
                   lambda: self.refusal(service, self.run(service, 'd2', GOALS['d2'], self.need('root:d2'), steps=5),
                                        'shared_steps'))

        def compact():
            fact = self.run(service, 'd3', GOALS['d3'], self.need('root:d3'))
            self.facts['d3'] = fact
            waits = [t['result'] for t in self.receipts(service, fact, 'WaitForAgents') if isinstance(t['result'], dict)]
            ready = [w for w in waits if w.get('results')]
            assert ready, ('No wait returned settled-child results.', [w.get('status') for w in waits])
            packet = ready[-1]
            entry = packet['results'][0]
            assert set(entry) <= COMPACT_KEYS and not set(entry) & HEAVY_KEYS, sorted(entry)
            assert len(entry.get('untrusted_summary', '')) <= 200 and entry.get('summary_truncated') is True, entry
            assert packet.get('results_note') and len(json.dumps(packet['results'])) <= 3000
            assert D3_TAIL not in json.dumps(packet), 'The wait carried the whole child report.'
            return {'entry_keys': sorted(entry), 'results_chars': len(json.dumps(packet['results']))}
        self.check('d1_wait_for_agents_returns_a_compact_settled_child_entry', compact)

        def ping_pong():
            fact = self.need('d3')
            reviews = {t['call_id'].rsplit(':', 1)[-1]: t['result'] for t in self.receipts(service, fact, 'ReviewAgentResult')}

            def error(name):
                value = reviews.get(name)
                return str(value.get('error') if isinstance(value, dict) else value)
            bogus = [error(f'd3-review-{n}') for n in range(1, D3_BOGUS_REVIEWS + 1)]
            assert len([n for n in reviews if n.startswith('d3-review-')]) == D3_BOGUS_REVIEWS, sorted(reviews)
            assert not any('Ping-pong limit' in e for e in bogus[:2]), bogus[:2]
            assert all('Ping-pong limit (review_rounds)' in e for e in bogus[2:]), bogus[2:]
            accepted = reviews.get('d3-accept')
            assert isinstance(accepted, dict) and accepted.get('review') == 'accepted', accepted
            return {'refused_before_limit': 2, 'refused_past_limit': len(bogus) - 2,
                    'accepted_with': accepted.get('evidence_source')}
        self.check('d1_review_rounds_past_the_ping_pong_limit_are_refused_until_the_root_accepts', ping_pong)

    # s1 ------------------------------------------------------------------------------------

    def scenario_s1(self, service):
        root = self.need('root:s1')

        def changed():
            fact = self.send(service, 's1', GOALS['s1'], root)
            assert self.fixture.holding['s1'].wait(30), 'The s1 model turn after the read never started.'
            run = service.rpc('run_get', id=fact['cid'], run_id=fact['rid'])
            # The model's Read (Phase 46: above 8K the host's prefetch reads the named facts.txt first, host:prefetch_1)
            reads = [t for t in run['tools'] if t['name'] == 'Read' and not t['call_id'].startswith('host:')]
            assert run['state'] == 'running' and [t['state'] for t in reads] == ['completed'], (run['state'], reads)
            (root / 'facts.txt').write_text(S1_AFTER)
            self.fixture.release['s1'].set()
            fact['state'] = self.settle(service, fact['cid'], fact['rid'], 's1')['state']
            self.facts['s1'] = fact
            return {'read_before_change': True, 'state': fact['state']}
        if not self.check('s1_file_read_then_changed_before_settlement', changed):
            return

        def audited():
            fact = self.need('s1')
            handoff = self.call(service, 'run_handoff', id=fact['cid'], run_id=fact['rid'], refresh=False)
            assert handoff, 'No handoff was saved at settlement.'
            data = handoff['data']
            audit = data.get('source_audit') or {}
            assert (audit.get('counts') or {}).get('changed', 0) >= 1, ('No settlement source audit names the change.',
                                                                        audit.get('counts'))
            stale = [s for s in data['unresolved']['stale_sources'] if s.get('basis') == 'settlement_audit']
            assert stale and all(s['status'] in ('changed', 'deleted', 'unreadable') for s in stale), stale
            return {'audit_counts': audit['counts'], 'stale_sources': len(stale)}
        self.check('s1_settlement_audit_records_the_changed_file_as_stale', audited)

        def completion():
            fact = self.need('s1')
            texts = list(self.seen('s1').get('completion_checks') or [])
            texts += [m['content'] for m in service.rpc('get_chat', id=fact['cid'])['messages'] if m['role'] == 'assistant']
            assert any(STALE_COMPLETION.search(t) for t in texts), 'Completion did not report the stale source.'
            return {'reported': True, 'state': fact['state']}
        self.check('s1_completion_reports_the_stale_source', completion)

    # Campaign-wide ------------------------------------------------------------------------------

    def final_checks(self):
        def native_only():
            with self.fixture.lock:
                offered = {name for r in self.fixture.records for name in r['tools']}
            leaked = sorted(n for n in offered if n in NATIVE_ONLY or re.search(r'contest|archive|handoff', n, re.I))
            assert offered and not leaked, leaked
            return {'tool_names_seen': len(offered)}
        self.check('native_memory_and_handoff_routes_are_never_offered_to_models', native_only)

        def clean():
            with self.fixture.lock:
                assert not self.fixture.errors, self.fixture.errors[-5:]
                assert not self.fixture.anomalies, self.fixture.anomalies[-5:]
                assert not [r for r in self.fixture.records if r['model'] not in MODELS]
            return {'provider_attempts': len(self.fixture.records)}
        self.check('synthetic_provider_protocol_clean', clean)


def checkout_digests():
    paths = ['backend/main.py', *[p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'src-tauri/src/main.rs', 'scripts/validate_phase15.py', 'scripts/validate_phase14.py',
             'scripts/validate_phase13.py', 'scripts/validate_runtime.py']
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
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary homes/projects even on success')
    args = parser.parse_args()
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; move it aside or choose a new path (earlier raw evidence, '
                     'including a failed run, is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    command = [str(backend)] if backend else [sys.executable, str(ROOT / 'backend/main.py')]
    backend_before = sha(backend.read_bytes()) if backend else None
    sources = checkout_digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase15-')).resolve()
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
    passed = bool(results) and campaign.fatal is None and all(c['passed'] for c in results)
    keep = args.keep_scratch or not passed
    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    with campaign.fixture.lock:
        records = [dict(r) for r in campaign.fixture.records]
        errors, anomalies = list(campaign.fixture.errors), list(campaign.fixture.anomalies)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source',
        'passed': passed,
        'checks': [c['name'] for c in results if c['passed']],
        'failed': [c['name'] for c in results if not c['passed']],
        'check_results': results, 'fatal_error': campaign.fatal,
        'observations': campaign.observations,
        'duration_seconds': duration,
        'duration_budget_seconds': VERIFIER_SECONDS,
        'synthetic_provider_attempts': len(records), 'request_metadata': records,
        'provider_errors': errors, 'provider_anomalies': anomalies,
        'backend_sha256': backend_before,
        'backend_path': str(backend) if backend else None,
        'source_sha256': sources,
        'source_note': ('Digests of this checkout. In frozen mode they do not prove the executable was built from '
                        'these files; the candidate manifest is bound separately.'),
        'scratch_preserved': str(scratch) if keep else None,
        'backend_stderr_tail': campaign.stderr_tails if not passed else [],
        'limits': ('Owned loopback synthetic provider, temporary homes and owned projects outside .runtime, actual '
                   'service JSON-RPC launched from the main.rs environment allowlist and native main-window routes. '
                   'The cloud-classified route is an owned local port that refuses connections; no cloud route, live '
                   'model, network, account, credential, installer, signed/installed desktop, other-platform or '
                   'frontier evidence. Fixture evidence, not an independent human security audit.'),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, 'x', encoding='utf-8') as out:
        out.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']),
                      'failed': report['failed'], 'fatal_error': campaign.fatal, 'duration_seconds': duration,
                      'scenario_seconds': campaign.observations.get('scenario_seconds'),
                      'scratch_preserved': report['scratch_preserved']}, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())


# Development self-checks, run only by pytest on this file; the campaign never
# calls them. They use the real backend modules for extraction, constraints and
# archive checksums, and drive the scripted turns with synthetic host results.
# No service is started (except by the explicit-request test at the end).

def _backend_path():
    path = str(ROOT / 'backend')
    if path not in sys.path:
        sys.path.insert(0, path)


def _closed_fixture():
    fixture = Fixture()
    fixture.server.server_close()
    return fixture


def _turn(fixture, messages, model=MODEL, names=()):
    body = {'model': model, 'messages': messages, 'tools': [{'function': {'name': name}} for name in names]}
    return fixture.respond(body).delta


def _answer(messages, delta, value):
    messages.append({'role': 'assistant', **delta})
    for call in delta.get('tool_calls', []):
        messages.append({'role': 'tool', 'tool_call_id': call['id'],
                         'content': json.dumps(value(call) if callable(value) else value)})


def _goal(tag, text):
    return [{'role': 'system', 'content': 'synthetic'}, {'role': 'user', 'content': text + ' ' + MARK + tag}]


def test_fixture_files_give_the_expected_keyed_claims():
    _backend_path()
    from pebble import memory_facts
    claims = {}
    for name, text in {**M1_FILES, 'notes.yaml': f'codename: {M3_CODENAME}\n'}.items():
        [fact] = memory_facts.extract(text, name)
        assert fact['form'] == 'config_value' and fact['quote'] == text.strip(), fact
        report = {'path': name, 'key': fact['key'], 'quote': fact['quote'], 'form': fact['form']}
        claims[name] = memory_facts.fact_claim(report)
        assert not memory_facts.credential_shaped(text)
    # Two root config files with one key: the same claim, different values (a contest, not a duplicate).
    assert claims['dev.yaml']['entity'] == claims['prod.yaml']['entity'] == ''
    assert claims['dev.yaml']['attribute'] == claims['prod.yaml']['attribute'] == 'port'
    assert claims['dev.yaml']['value'] != claims['prod.yaml']['value']
    assert claims['notes.yaml']['attribute'] == 'codename'


def test_constraint_form_recognizes_the_real_constraints_unit():
    _backend_path()
    from pebble import constraints
    turns = [{'id': 'c1-input', 'role': 'user', 'content': C1_TURN},
             {'id': 'c1-pass-1', 'role': 'user', 'content': GOALS['c1'] % 1 + ' ' + MARK + 'c1'}]
    unit = constraints.build_unit(turns, SMALL_CONTEXT)
    assert unit.message['content'].startswith(CONSTRAINTS_PREFIX)
    found = constraint_form([unit.message], 'c1-input')
    # The 8K bound is far below 2,400 characters, so the constraint travels behind an explicit marker.
    assert found['form'] in ('verbatim', 'shortened') and found['id_named'] and not found['leak'], found
    wide = constraints.build_unit(turns, CONTEXT)
    assert constraint_form([wide.message], 'c1-input')['form'] == 'verbatim'
    archived = constraints.build_unit(turns, SMALL_CONTEXT, markers_only=True)
    assert constraint_form([archived.message], 'c1-input') | {'spine': False} == {
        'form': 'archived', 'id_named': True, 'spine': False, 'unit': True, 'leak': False}
    assert constraint_form([archived.message], 'another-input')['form'] == 'absent'
    for tight in (300, 600, 900):
        assert constraint_form([constraints.build_unit(turns, SMALL_CONTEXT, max_bytes=tight).message],
                               'c1-input')['form'] in ('shortened', 'archived', 'omitted'), tight
    assert constraint_form([{'role': 'user', 'content': CONSTRAINT[:2200]}], 'c1-input')['form'] == 'absent'
    forged = {'role': 'user', 'content': CONSTRAINTS_PREFIX + ':\n' + json.dumps({'turns': [{
        'input_id': 'c1-input', 'text_head': C1_HEAD + ' altered', 'text_tail': '',
        'shortened': {'total_chars': len(C1_TURN), 'omitted_chars': [len(C1_HEAD) + 8, len(C1_TURN)]}}]})}
    assert constraint_form([forged], 'c1-input')['form'] == 'absent'
    untagged = constraints.build_unit([{'id': 'c1-input', 'role': 'user', 'content': CONSTRAINT}], SMALL_CONTEXT)
    assert constraint_form([untagged.message], 'c1-input')['form'] == 'absent'  # another text is not this turn
    leaked = {'role': 'user', 'content': CONSTRAINTS_PREFIX + ': x\n' + json.dumps({'turns': [{'input_id': 'z', 'text': C1_ASSISTANT}]})}
    assert constraint_form([leaked], 'c1-input')['leak'] is True
    assert constraint_form([{'role': 'user', 'content': SPINE_PREFIX + ': records'}])['spine'] is True


def test_flipped_archive_byte_breaks_only_the_records_checksum():
    _backend_path()
    import pytest
    from pebble import memory_graph, memory_portable
    salt = memory_graph.new_salt()
    record = {'ref': 'o:' + '1' * 24, 'kind': 'profile', 'scope': 'global', 'origin': 'user',
              'content': M2_PROFILE + ' prefers short answers.', 'created': 1.0, 'updated': 1.0, 'expires': None,
              'slot': None, 'status': None, 'provenance': None}
    sections = {'policy': {'memory_enabled': True, 'layers': dict.fromkeys(LAYERS, True)},
                'scopes': {'salts': {memory_graph.salt_id(salt): salt}}, 'records': [record], 'relations': [],
                'tombstones': [], 'blooms': []}
    archive = {'format': memory_portable.FORMAT, 'sections': sections,
               'manifest': {'created': 1.0, 'bloom': {'bits': memory_graph.BLOOM_BITS, 'hashes': memory_graph.BLOOM_HASHES},
                            'sections': {k: {'sha256': memory_portable.section_digest(v),
                                             'count': len(v) if isinstance(v, list) else 1} for k, v in sections.items()}}}
    data = memory_portable.encode(archive)
    assert memory_portable.validate(memory_portable.decode(data))['records'] == [record]
    tampered = flip(data, M2_PROFILE)
    assert len(tampered) == len(data) and sum(a != b for a, b in zip(data, tampered)) == 1
    with pytest.raises(memory_portable.ArchiveError) as refused:
        memory_portable.validate(memory_portable.decode(tampered))
    assert refused.value.reason == 'checksum_mismatch' and 'checksum' in str(refused.value)


def test_memory_and_route_scripts_follow_their_scenarios():
    fixture = _closed_fixture()
    messages = _goal('m1', GOALS['m1'])
    delta = _turn(fixture, messages)
    assert [json.loads(c['function']['arguments'])['file_path'] for c in delta['tool_calls']] == list(M1_FILES)
    _answer(messages, delta, {'untrusted_source': 'port'})
    assert 'content' in _turn(fixture, messages)
    fixture.share(m1_files=['prod.yaml'])
    again = _turn(fixture, _goal('m1', GOALS['m1_again']))
    assert [json.loads(c['function']['arguments'])['file_path'] for c in again['tool_calls']] == ['prod.yaml']
    fixture.share(phase='m3-learn')
    assert _turn(fixture, _goal('m3', GOALS['m3']))['tool_calls'][0]['function']['name'] == 'Read'
    fixture.share(phase='m3-recall-b')
    assert 'content' in _turn(fixture, _goal('m3', GOALS['m3']) + [{'role': 'user', 'content': 'RETRIEVED MEMORY ' + M3_CODENAME}])
    assert fixture.records[-1]['markers'] == ['m3'] and fixture.records[-1]['phase'] == 'm3-recall-b'
    handoff = [{'role': 'user', 'content': GOALS['h1_device'] + ' ' + MARK + 'h1'}, {'role': 'assistant', 'content': 'hi'},
               {'role': 'user', 'content': 'Continue the task from the conversation above.'}]
    assert 'content' in _turn(fixture, handoff, model=MODEL_B)
    assert fixture.records[-1]['markers'] == ['h1_device'] and fixture.records[-1]['scenario'] == 'h1'
    assert not fixture.anomalies and not fixture.errors


def test_constraint_and_delegation_scripts_follow_their_scenarios():
    fixture = _closed_fixture()
    assert C1_ASSISTANT in _turn(fixture, _goal('c1', CONSTRAINT), model=MODEL_SMALL)['content']
    messages = _goal('c1', GOALS['c1'] % 1)
    reads = 0
    while True:
        delta = _turn(fixture, messages, model=MODEL_SMALL)
        if 'content' in delta:
            break
        reads += 1
        _answer(messages, delta, {'untrusted_source': 'page'})
    assert reads == C1_READS and [f['form'] for f in fixture.seen['c1']['forms']] == ['absent'] * (C1_READS + 1)
    # d1/d2: one spawn, then a final answer.
    first = _turn(fixture, _goal('d1', GOALS['d1']))
    assert json.loads(first['tool_calls'][0]['function']['arguments'])['max_steps'] == 1
    assert 'max_steps' not in json.loads(_turn(fixture, _goal('d2', GOALS['d2']))['tool_calls'][0]['function']['arguments'])
    # d3: spawn, wait, four refused reviews, then accept with the host default and finish.
    messages, emitted = _goal('d3', GOALS['d3']), []

    def result(call):
        name = call['function']['name']
        if name == 'SpawnAgent':
            return {'agent_id': 'child-1', 'generation': 1}
        if name == 'WaitForAgents':
            return {'status': 'ready'}
        return {'error': 'refused'} if json.loads(call['function']['arguments']).get('evidence_ids') else {'review': 'accepted'}
    child = [{'role': 'user', 'content': ASSIGNMENT + json.dumps({'task': CHILD_TASK + ' ' + MARK + 'd3'})}]
    assert _turn(fixture, child)['tool_calls'][0]['function']['name'] == 'Read'
    child.append({'role': 'assistant', 'content': 'read'})
    assert D3_TAIL in _turn(fixture, child)['content']
    while True:
        delta = _turn(fixture, messages)
        if 'content' in delta:
            break
        emitted.append(delta['tool_calls'][0]['function']['name'])
        _answer(messages, delta, result)
    assert emitted == ['SpawnAgent', 'WaitForAgents'] + ['ReviewAgentResult'] * (D3_BOGUS_REVIEWS + 1)
    assert not fixture.anomalies and not fixture.errors


def test_source_script_reads_holds_until_released_and_records_completion_checks():
    fixture = _closed_fixture()
    messages = _goal('s1', GOALS['s1'])
    delta = _turn(fixture, messages)
    assert delta['tool_calls'][0]['function']['name'] == 'Read'
    _answer(messages, delta, {'untrusted_source': S1_BEFORE})
    fixture.release['s1'].set()
    assert 'content' in _turn(fixture, messages)
    messages.append({'role': 'assistant', 'content': 'done'})
    messages.append({'role': 'user', 'content': 'HOST COMPLETION CHECK: 1 source file(s) read in this run changed or '
                                                'were deleted afterwards.'})
    assert 'content' in _turn(fixture, messages)
    assert STALE_COMPLETION.search(fixture.seen['s1']['completion_checks'][-1])


def test_unscripted_private_and_unknown_routes_are_flagged():
    fixture = _closed_fixture()
    _turn(fixture, _goal('z9', 'Nothing scripted here.'))
    assert fixture.anomalies and fixture.anomalies[-1]['reason'] == 'unscripted conversation'
    for body in ({'model': MODEL_A, 'messages': []},
                 {'model': MODEL, 'messages': [{'role': 'user', 'content': 'x', '_pebble_current': True}]}):
        try:
            fixture.respond(body)
        except AssertionError:
            continue
        raise AssertionError('The fixture accepted an unexpected request.')


def test_flip_changes_one_byte_inside_the_marker():
    data = b'{"content":"' + M2_PROFILE.encode() + b' prefers"}'
    tampered = flip(data, M2_PROFILE)
    assert json.loads(tampered) and tampered != data and sum(a != b for a, b in zip(data, tampered)) == 1


def test_checks_are_independent_and_names_unique():
    campaign = Campaign(['unused'], None)
    campaign.fixture.server.server_close()
    assert campaign.check('first', lambda: campaign.need('missing')) is False
    assert 'Prerequisite' in campaign.checks[-1]['error']
    assert campaign.check('second', lambda: {'ok': True}) is True
    try:
        campaign.check('first', lambda: None)
    except AssertionError:
        pass
    else:
        raise AssertionError('A duplicate check name was accepted.')

    class Missing:
        @staticmethod
        def rpc(method, **params):
            raise RuntimeError('Unknown request.')
    assert campaign.check('route', lambda: Campaign.call(Missing, 'memory_contests')) is False
    assert 'native route is missing: memory_contests' in campaign.checks[-1]['error']


def test_source_campaign_run_on_explicit_request(request, tmp_path):
    """The whole campaign in source mode, as a subprocess with its scratch under tmp_path.

    It starts the actual service, so it runs only when this node ID is named explicitly; the default
    collection skips it.
    """
    import pytest
    if not any(str(arg).endswith('::test_source_campaign_run_on_explicit_request') for arg in request.config.args):
        pytest.skip('Starts the actual service; name this test explicitly to run it.')
    report, log = tmp_path / 'report.json', tmp_path / 'campaign.log'
    with log.open('w') as sink:
        done = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--report', str(report)], cwd=ROOT,
                              env={**os.environ, 'TMPDIR': str(tmp_path)}, stdout=sink, stderr=subprocess.STDOUT,
                              timeout=VERIFIER_SECONDS + 60)
    outcome = json.loads(report.read_text())
    import warnings
    warnings.warn('campaign evidence: ' + json.dumps({'seconds': outcome['duration_seconds'], **{
        c['name']: c.get('evidence') for c in outcome['check_results'] if c['passed']}})[:9000])  # in the -q summary
    assert done.returncode == 0 and outcome['passed'] is True, json.dumps(
        {'failed': outcome['failed'], 'fatal': outcome['fatal_error'],
         'errors': {c['name']: c.get('error') for c in outcome['check_results'] if not c['passed']}})[:6000]
    assert outcome['kind'] == 'source' and len(outcome['checks']) == len(set(outcome['checks']))
    assert outcome['duration_seconds'] < VERIFIER_SECONDS
