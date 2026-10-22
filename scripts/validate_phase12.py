"""Phase 12 source/frozen JSON-RPC proof: asynchronous commands and stream replay.

Drives the *actual* service over its JSON-RPC stdio contract (validate_runtime.
Service) with one owned loopback SSE provider and a disposable temporary home and
projects. Every model turn is scripted synthetic output. Approval is the native
`approve` RPC a person uses. Tiny fixture scripts run under the actual macOS
confinement and end by themselves within 25 seconds even if teardown fails. No
real model, external network, account, credential, keychain or user data is used.

Scenarios (each tagged with an opaque marker; all but x2 in a new conversation).
Every uncertain outcome gates further commands in its project until a native
inspection, so p2, p3, p4, k1, u1/u2 and w1 each own a separate project:

  p1  StartCommand stages one pending asynchronous command; nothing runs before
      native approval; approval returns admission, not completion; the resumed
      root waits, pages split UTF-8 output exactly and completes after observing
      the terminal outcome.
  p2  the root stops a live process after its first effect: cancelled, outcome
      uncertain, the effect stays, the process group and its in-group child stop.
      A grandchild that leaves the group (setsid, pipes closed) is recorded as an
      observation, not a pass/fail claim: see the threat model residuals.
  p3  a two-second timeout stops the process with an uncertain outcome.
  p4  an output burst hits the output limit; retained output stays bounded and
      the model follows host-issued next_after cursors past 10,000 bytes.
  w1  a live writable process exclusively owns its project: the root's Write and
      a second writable StartCommand are refused without new approvals while it
      runs (it waits on a release file the fixture writes only after both
      refusals), and the same Write is staged for review once it has settled.
  x2  another run of the p1 conversation cannot read, wait on or stop p1; native
      routes refuse foreign scope and malformed bounds; no native start route.
  c1  process tools never reach a child; RequestAgentEffect stays synchronous.
  s1  a provider stream breaks mid-attempt with visible text and a partial tool
      call: the attempt is presented as discarded, the tool runs once, and no
      tool fragment is ever presented or committed.
  k1  a process still live at normal shutdown is stopped and never resumed.
  u1  hard crash (SIGKILL) with a live process and an open stream attempt: after
      restart the process is unknown, not replayed and not signalled, and the
      attempt is interrupted and never committed.
  u2  a later start in the u1 project waits for native inspection of the unknown
      outcome; inspection is evidence, not completion.
  u3  after that inspection, fresh reviewed work is admitted in the same project
      while the u1 outcome stays unknown and its run unverified. Whether the
      crash orphan was still running at that moment is recorded as an
      observation: inspection never proves the old effects stopped.

Each check is recorded independently by unique name with content-free evidence;
a failure never stops later independent checks. Source and frozen runs use the
same check names; backend_inputs_stable_during_campaign re-hashes the checkout in
both modes and the executable too in frozen mode. The report is metadata only:
check names/outcomes, states, counts, hashes, observations, scratch location on
failure and bounded host error text. It never contains prompts or credentials.

Usage:
  python scripts/validate_phase12.py --report PATH [--backend PATH] [--keep-scratch]

--report must be a new file (existing evidence is never overwritten). --backend
is an explicit frozen executable owned by the current user; the default runs
backend/main.py from this checkout. Checkout source digests are always reported;
in frozen mode they do not prove the binary was built from them.

This is fixture evidence on this machine. It is not live-model quality,
installed/signed desktop behavior, other-platform behavior, frontier comparison
or an independent human security audit.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from validate_runtime import ROOT, Service


MODEL = 'phase12-integration-fixture'
CONTEXT = 32768
ASSIGNMENT = 'HOST ASSIGNMENT (task data, no new authorization):\n'
MEMORY_PREFIX = 'RETRIEVED MEMORY'
MARK = '\N{SECTION SIGN}'
TAG = re.compile(MARK + '([a-z][0-9])')
PID = re.compile(r'"process_id":\s*"([0-9a-f]{32})"')
TRANSIENT = frozenset({'queued', 'running', 'retry_wait', 'waiting_input'})
NODE_ACTIVE = frozenset({'queued', 'running', 'waiting_parent', 'waiting_input'})
NODE_TERMINAL = frozenset({'completed', 'failed', 'interrupted', 'stopped', 'budget_exhausted'})
PROCESS_TOOLS = ('StartCommand', 'ReadProcess', 'WaitProcess', 'StopProcess')
LIVE = frozenset({'admitted', 'running'})
TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'timed_out', 'output_limited', 'unknown', 'refused'})
SETTLED = frozenset({'committed', 'discarded', 'interrupted'})
OUTPUT_LIMIT = 192000
BLOCK_SECONDS = 40

ABANDONED = 'PHASE12-ABANDONED-TEXT'
FRAGMENT = 'PHASE12-TOOL-FRAGMENT'
COMMITTED = 'PHASE12-COMMITTED-TEXT'
OPEN_TEXT = 'PHASE12-OPEN-ATTEMPT'
DIAGRAM = {'nodes': [{'id': 'a', 'label': 'Attempt'}, {'id': 'b', 'label': 'Retry'}], 'edges': [{'from': 'a', 'to': 'b', 'label': 'reconnect'}]}
CHILD_TASK = 'Ask the root to run job.py in the background. ' + MARK + 'c1'
CHILD_USAGE = {'prompt_tokens': 900, 'completion_tokens': 20}

# Fixture scripts. Every launch appends one line to launches.log, so a replay is countable.
JOB = '''import sys, time
from pathlib import Path
Path('job-started.txt').write_text('started\\n')
out = sys.stdout.buffer
out.write(b'PHASE12-START\\n')
out.flush()
time.sleep(0.6)
for part in (b'caf\\xc3', b'\\xa9 \\xe2\\x98', b'\\x83 \\xf0\\x9f', b'\\x99', b'\\x82 na\\xc3\\xafve\\n'):
    out.write(part)
    out.flush()
    time.sleep(0.15)
time.sleep(0.6)
out.write(b'PHASE12-DONE\\n')
out.flush()
Path('job-done.txt').write_text('done\\n')
'''
EXPECTED_JOB = 'PHASE12-START\ncaf\u00e9 \u2603 \U0001f642 na\u00efve\nPHASE12-DONE\n'
SLOW = '''import subprocess, sys, time
from pathlib import Path
Path('partial-effect.txt').write_text('written before stop\\n')
subprocess.Popen([sys.executable, 'beat.py', 'child-beat.txt', '20'])
subprocess.Popen([sys.executable, 'beat.py', 'escaped-beat.txt', '12'], start_new_session=True,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print('PHASE12-EFFECT-COMMITTED', flush=True)
end = time.monotonic() + 20
while time.monotonic() < end:
    with open('parent-beat.txt', 'a') as handle:
        handle.write('.')
    time.sleep(0.1)
'''
BEAT = '''import sys, time
end = time.monotonic() + float(sys.argv[2])
while time.monotonic() < end:
    with open(sys.argv[1], 'a') as handle:
        handle.write('.')
    time.sleep(0.1)
'''
BURST = '''import sys, time
out = sys.stdout.buffer
line = b'x' * 99 + b'\\n'
for _ in range(3000):
    out.write(line)
out.flush()
time.sleep(20)
'''
CRASHJOB = '''import time
print('PHASE12-CRASH-JOB-RUNNING', flush=True)
end = time.monotonic() + 25
while time.monotonic() < end:
    with open('crash-beat.txt', 'a') as handle:
        handle.write('.')
    time.sleep(0.1)
'''
GATE = '''import time
from pathlib import Path
end = time.monotonic() + 20
while time.monotonic() < end and not Path('release.txt').exists():
    with open('gate-beat.txt', 'a') as handle:
        handle.write('.')
    time.sleep(0.1)
print('PHASE12-GATE-RELEASED', flush=True)
'''
NOTES = 'Notes kept by the person.\n'
W1_DRAFT = 'Notes rewritten while a background writer ran.\n'
FIXTURES = {'job.py': JOB, 'slow.py': SLOW, 'beat.py': BEAT, 'burst.py': BURST, 'crashjob.py': CRASHJOB,
            'gate.py': GATE}
HEARTBEATS = ('parent-beat.txt', 'child-beat.txt', 'escaped-beat.txt', 'timeout-beat.txt', 'close-beat.txt',
              'crash-beat.txt', 'gate-beat.txt')
# Fact key -> folder. Keys never collide with scenario tags, whose facts share the dict.
PROJECTS = {'alpha': 'project', 'pi2': 'p2-project', 'pi3': 'p3-project', 'pi4': 'p4-project',
            'kappa': 'kappa-project', 'upsilon': 'upsilon-project', 'omega': 'omega-project'}

GOALS = {tag: text + ' ' + MARK + tag for tag, text in {
    'p1': 'Run job.py as a long-running background command and report its output.',
    'p2': 'Start slow.py in the background and stop it once its first effect is committed.',
    'p3': 'Run beat.py in the background with a two second limit.',
    'p4': 'Run burst.py in the background and inspect its output.',
    'x2': 'Inspect the earlier background process from this conversation.',
    'c1': 'Delegate the background check to a child agent and review its report.',
    's1': 'Draw a diagram of the retry flow.',
    'k1': 'Start beat.py in the background and leave it running.',
    'u1': 'Run crashjob.py in the background and watch it.',
    'u2': 'Run job.py in the background again.',
    'u3': 'Run job.py in the background now that the earlier outcome was inspected.',
    'w1': 'Start gate.py in the background, then update notes.txt while it runs.',
}.items()}


def launch(name):
    return ('import sys as _launch_sys\n'
            "with open('launches.log', 'a') as _launch_log:\n"
            "    _launch_log.write(' '.join([" + repr(name) + "] + _launch_sys.argv[1:]) + '\\n')\n")


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode('utf-8')).hexdigest()


def bounded(exc, limit=1500):
    return (type(exc).__name__ + ': ' + str(exc))[:limit]


def tool(name, ident, args, index=0):
    return {'index': index, 'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}


def calls(*items):
    return {'tool_calls': [tool(name, ident, args, index) for index, (name, ident, args) in enumerate(items)]}


def final(text):
    return {'content': text}


def usage(step):
    return {'prompt_tokens': 2000 + step, 'completion_tokens': 30}


class Reply:
    """One scripted SSE response. mode: complete, truncate (no end marker) or block."""

    def __init__(self, delta, usage=None, mode='complete'):
        self.delta, self.usage, self.mode = delta, usage, mode


def user_text(message):
    content = message.get('content')
    return content if message.get('role') == 'user' and isinstance(content, str) else None


def goal_index(messages):
    """Index of the latest tagged human goal; later context belongs to this run.
    Host-generated user messages may quote tagged task data and are never goals."""
    index = None
    for position, message in enumerate(messages):
        text = user_text(message)
        if text and not text.startswith((MEMORY_PREFIX, 'HOST ')) and TAG.search(text):
            index = position
    return index


def after_goal(messages):
    index = goal_index(messages)
    return messages if index is None else messages[index + 1:]


def result(messages, call_id):
    """The host tool result for one raw call id, decoded; None if absent."""
    for message in reversed(messages):
        if message.get('role') == 'tool' and message.get('tool_call_id') == call_id:
            try:
                value = json.loads(message.get('content') or 'null')
            except (TypeError, ValueError):
                return {'unparsed': True}
            return value if isinstance(value, dict) else {'value_type': type(value).__name__}
    return None


def history(messages):
    done = []
    for message in after_goal(messages):
        if message.get('role') != 'assistant':
            continue
        for call in message.get('tool_calls') or []:
            try:
                args = json.loads(call['function'].get('arguments') or '{}')
            except ValueError:
                args = {}
            done.append({'name': call['function']['name'], 'id': call['id'],
                         'args': args if isinstance(args, dict) else {}, 'result': result(messages, call['id'])})
    return done


def last_call(messages):
    assistants = [m for m in after_goal(messages) if m.get('role') == 'assistant']
    if not assistants:
        return None, None
    tool_calls = assistants[-1].get('tool_calls') or []
    if not tool_calls:
        return 'final', None
    return tool_calls[-1]['function']['name'], tool_calls[-1]['id']


def admitted_pid(messages):
    """The process id the host reported back after native approval, if any."""
    found = None
    for message in after_goal(messages):
        text = user_text(message)
        if text and text.startswith(('HOST INPUT', 'HOST ACTION UPDATES')):
            hits = PID.findall(text)
            if hits:
                found = hits[-1]
    return found


def outcome_summary(value):
    if not isinstance(value, dict):
        return {'absent': True}
    inner = value['result'] if isinstance(value.get('result'), dict) else {}
    return {'error': str(value['error'])[:300] if value.get('error') else None,
            'status': value.get('status'), 'state': value.get('state'),
            'action_present': bool(value.get('action_id')),
            'process_id_present': bool(value.get('process_id') or inner.get('process_id')),
            'has_output': 'untrusted_process_output' in value}


def spawn_summary(value):
    if not isinstance(value, dict):
        return {'absent': True}
    return {'error': str(value['error'])[:300] if value.get('error') else None, 'agent_id': value.get('agent_id')}


def stream_events(page):
    if isinstance(page, list):
        return page
    if not isinstance(page, dict):
        raise AssertionError('The stream page is not an object.')
    for key in ('events', 'items', 'records'):
        if isinstance(page.get(key), list):
            return page[key]
    raise AssertionError(('No event list in the stream page.', sorted(page)))


def attempts(events):
    groups = {}
    for event in events:
        ident = event.get('attempt_id', event.get('attempt'))
        if ident is None:
            continue
        group = groups.setdefault(ident, {'text': '', 'state': None})
        if isinstance(event.get('text'), str):
            group['text'] += event['text']
        if event.get('state') in SETTLED:
            group['state'] = event['state']
    return groups


def size(path):
    return path.stat().st_size if path.exists() else 0


def quiet(*paths, seconds=1.5):
    before = [size(p) for p in paths]
    time.sleep(seconds)
    return before == [size(p) for p in paths]


def launches(root, line):
    log = root / 'launches.log'
    return sum(1 for entry in log.read_text().splitlines() if entry == line) if log.exists() else 0


def rejected(service, method, **params):
    try:
        service.rpc(method, **params)
    except RuntimeError as exc:
        text = str(exc)
        if text.startswith(('Backend timed out', 'Backend stopped')):
            raise AssertionError('No refusal was observed; the service failed instead: ' + text[:300]) from None
        return text[:300]
    raise AssertionError('Expected a refusal from ' + method + ' ' + json.dumps(sorted(params)))


class Fixture:
    """Owned loopback SSE provider. Scripts are stateless per request: the step
    is the number of assistant turns after the latest tagged goal, so retries and
    resumes stay aligned; repeated attempts of one step are counted."""

    def __init__(self):
        self.lock = threading.RLock()
        self.records, self.errors, self.anomalies = [], [], []
        self.seen, self.counts, self.shared = {}, {}, {}
        self.blocked, self.release = threading.Event(), threading.Event()
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
                    self.wfile.flush()
                    if reply.mode == 'block':
                        fixture.blocked.set()
                        fixture.release.wait(BLOCK_SECONDS)
                        return
                    if reply.mode == 'truncate':
                        return  # the connection closes without an end marker
                    if reply.usage is not None:
                        self.wfile.write(b'data: ' + json.dumps({'choices': [], 'usage': reply.usage}).encode() + b'\n\n')
                    self.wfile.write(b'data: [DONE]\n\n')
                except OSError:
                    pass  # a crashed service closed its end

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def start(self):
        self.thread.start()

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    def state(self, tag):
        with self.lock:
            return self.seen.setdefault(tag, {})

    def attempts(self, *tags):
        with self.lock:
            return sum(1 for r in self.records if r['scenario'] in tags)

    def anomaly(self, tag, role, step, reason):
        with self.lock:
            self.anomalies.append({'scenario': tag, 'role': role, 'step': step, 'reason': reason[:300]})

    @staticmethod
    def classify(messages):
        for message in messages:
            text = user_text(message)
            if text and text.startswith(ASSIGNMENT):
                node, _ = json.JSONDecoder().raw_decode(text[len(ASSIGNMENT):])
                found = TAG.search(str(node.get('task', '')))
                return 'child', found.group(1) if found else None
        index = goal_index(messages)
        return 'root', TAG.search(messages[index]['content']).group(1) if index is not None else None

    def respond(self, body):
        if body.get('model') != MODEL:
            raise AssertionError('Unexpected model route reached the fixture.')
        if '_pebble_' in json.dumps(body):
            raise AssertionError('Host-private message fields reached the provider.')
        messages = body.get('messages') or []
        names = sorted(t['function']['name'] for t in body.get('tools', []))
        role, tag = self.classify(messages)
        step = sum(1 for m in (messages if role == 'child' else after_goal(messages)) if m.get('role') == 'assistant')
        with self.lock:
            key = (role, tag, step)
            self.counts[key] = attempt = self.counts.get(key, 0) + 1
            record = {'scenario': tag, 'role': role, 'step': step, 'attempt': attempt, 'tools': names}
            self.records.append(record)
        script = getattr(self, 'script_' + ('child' if role == 'child' else str(tag)), None)
        if script is None:
            self.anomaly(tag, role, step, 'unscripted conversation')
            reply = Reply(final('Synthetic fixture: no script.'))
        else:
            reply = script(step, attempt, messages, names)
        with self.lock:
            record['emitted'] = ([c['function']['name'] for c in reply.delta['tool_calls']]
                                 if 'tool_calls' in reply.delta else 'content')
            record['mode'] = reply.mode
        return reply

    def completion_check(self, tag, messages):
        last = user_text(messages[-1]) if messages else None
        if last and last.startswith('HOST COMPLETION CHECK'):
            seen = self.state(tag)
            with self.lock:
                seen['completion_repairs'] = seen.get('completion_repairs', 0) + 1
            return True
        return False

    def unexpected(self, tag, step, messages, text='Synthetic fixture finished.'):
        if not self.completion_check(tag, messages):
            self.anomaly(tag, 'root', step, 'step beyond script')
        return Reply(final(text))

    # Process scenarios --------------------------------------------------------

    def _process(self, tag, step, messages, names, command, timeout, stop_marker=None,
                 page_limit=4000, page_until=None, skip_ahead=False, wait_bound=30):
        seen = self.state(tag)
        if step == 0:
            with self.lock:
                seen['offered'] = sorted(set(PROCESS_TOOLS) & set(names))
            return Reply(calls(('StartCommand', tag + '-start', {'command': command, 'timeout': timeout})), usage(step))
        done = history(messages)
        pid = admitted_pid(messages)
        if pid is None:
            if step == 1:
                start = next((d for d in done if d['name'] == 'StartCommand'), None)
                with self.lock:
                    seen['start'] = outcome_summary(start and start['result'])
                return Reply(final('The background command is waiting for native approval.'), usage(step))
            return self.unexpected(tag, step, messages)
        self.completion_check(tag, messages)
        with self.lock:
            seen['model_pid'] = pid
        observed = [d for d in done if d['name'] in ('WaitProcess', 'ReadProcess') and d['args'].get('process_id') == pid]
        failed = [d for d in observed if not isinstance(d['result'], dict) or d['result'].get('error')
                  or 'untrusted_process_output' not in d['result']]
        if failed:
            with self.lock:
                seen['read_refused'] = {'tool': failed[-1]['name'], 'args': {k: v for k, v in failed[-1]['args'].items()
                                                                            if k != 'process_id'},
                                        'error': str((failed[-1]['result'] or {}).get('error'))[:300]}
            self.anomaly(tag, 'root', step, 'process read refused')
            return Reply(final('The background process could not be read.'), usage(step))
        stop = next((d for d in done if d['name'] == 'StopProcess'), None)
        if stop:
            with self.lock:
                seen['stop'] = outcome_summary(stop['result'])
        terminal = next((i for i, d in enumerate(observed) if d['result'].get('state') in TERMINAL), None)
        if terminal is not None:
            pages = [d for d in observed[terminal + 1:] if d['name'] == 'ReadProcess']
            last = pages[-1]['result'] if pages else None
            enough = last is not None and (not last.get('more') or page_until is not None
                                           and type(last.get('next_after')) is int and last['next_after'] >= page_until)
            if not enough:
                after = last.get('next_after', 0) if last else 0
                return Reply(calls(('ReadProcess', f'{tag}-page-{step}', {'process_id': pid, 'after': after,
                                                                          'limit': page_limit})), usage(step))
            text = ''.join(p['result'].get('untrusted_process_output') or '' for p in pages)
            with self.lock:
                seen['terminal'] = {k: observed[terminal]['result'].get(k)
                                    for k in ('state', 'exit_code', 'outcome_uncertain', 'output_truncated')}
                seen['pages'] = [{'after': p['args'].get('after'), 'next_after': p['result'].get('next_after'),
                                  'more': p['result'].get('more')} for p in pages]
                seen['text'] = text
            return Reply(final('Observed the terminal outcome of the background command.'), usage(step))
        waited = [d for d in observed if d['name'] == 'WaitProcess']
        if len(waited) >= wait_bound:
            self.anomaly(tag, 'root', step, 'wait bound reached before a terminal state')
            return Reply(final('The background command did not settle within the fixture bound.'), usage(step))
        text = ''.join(d['result'].get('untrusted_process_output') or '' for d in waited)
        if stop_marker and stop_marker in text and not stop:
            return Reply(calls(('StopProcess', tag + '-stop', {'process_id': pid})), usage(step))
        previous = waited[-1]['result'] if waited else {}
        after = (previous.get('output_bytes', previous.get('next_after', 0)) if skip_ahead
                 else previous.get('next_after', 0))
        args = {'process_id': pid, 'after': after, 'wait_seconds': 5}
        if skip_ahead:
            args['limit'] = 1
        return Reply(calls(('WaitProcess', f'{tag}-wait-{step}', args)), usage(step))

    def script_p1(self, step, attempt, messages, names):
        return self._process('p1', step, messages, names, 'python3 job.py', 60, page_limit=7)

    def script_p2(self, step, attempt, messages, names):
        return self._process('p2', step, messages, names, 'python3 slow.py', 60, stop_marker='PHASE12-EFFECT-COMMITTED')

    def script_p3(self, step, attempt, messages, names):
        return self._process('p3', step, messages, names, 'python3 beat.py timeout-beat.txt 20', 2)

    def script_p4(self, step, attempt, messages, names):
        # The host clamps model pages (4,000 bytes, or 500 in a small window); 12,000 still crosses 10,000.
        return self._process('p4', step, messages, names, 'python3 burst.py', 60, page_limit=8000,
                             page_until=12000, skip_ahead=True)

    def script_k1(self, step, attempt, messages, names):
        if step == 0:
            return Reply(calls(('StartCommand', 'k1-start', {'command': 'python3 beat.py close-beat.txt 25',
                                                              'timeout': 120})), usage(step))
        if admitted_pid(messages) is None:
            if step == 1:
                return Reply(final('The background command is waiting for native approval.'), usage(step))
            return self.unexpected('k1', step, messages)
        self.completion_check('k1', messages)
        return Reply(final('Leaving the background command running.'), usage(step))

    def script_u1(self, step, attempt, messages, names):
        if step == 0:
            return Reply(calls(('StartCommand', 'u1-start', {'command': 'python3 crashjob.py', 'timeout': 120})), usage(step))
        pid = admitted_pid(messages)
        if pid is None:
            if step == 1:
                return Reply(final('The background command is waiting for native approval.'), usage(step))
            return self.unexpected('u1', step, messages)
        if not any(d['name'] == 'WaitProcess' for d in history(messages)):
            return Reply(calls(('WaitProcess', 'u1-wait', {'process_id': pid, 'wait_seconds': 1})), usage(step))
        with self.lock:
            self.state('u1')['blocked_step'] = step
        return Reply({'content': OPEN_TEXT + ' still watching'}, mode='block')

    def script_w1(self, step, attempt, messages, names):
        """While the gated writer is live: Read, then a refused Write and a refused
        second writer; release the gate; observe the outcome; Write again."""
        seen = self.state('w1')
        if step == 0:
            return Reply(calls(('StartCommand', 'w1-start', {'command': 'python3 gate.py', 'timeout': 60})), usage(step))
        pid = admitted_pid(messages)
        if pid is None:
            if step == 1:
                return Reply(final('The background command is waiting for native approval.'), usage(step))
            return self.unexpected('w1', step, messages)
        if step >= 40:
            self.anomaly('w1', 'root', step, 'writer script exceeded its bound')
            return Reply(final('Stopping the synthetic writer script at its bound.'))
        self.completion_check('w1', messages)
        done = history(messages)
        called = [d['name'] for d in done]
        if 'Read' not in called:
            return Reply(calls(('Read', 'w1-read', {'file_path': 'notes.txt'})), usage(step))
        if 'Write' not in called:
            return Reply(calls(('Write', 'w1-write-live', {'file_path': 'notes.txt', 'content': W1_DRAFT})), usage(step))
        if called.count('StartCommand') < 2:
            return Reply(calls(('StartCommand', 'w1-second', {'command': 'python3 job.py', 'timeout': 60})), usage(step))
        with self.lock:
            if 'live_refusals' not in seen:
                seen['live_refusals'] = {'write': outcome_summary(result(messages, 'w1-write-live')),
                                         'second': outcome_summary(result(messages, 'w1-second'))}
                root = self.shared.get('w1_root')
                if root:
                    (Path(root) / 'release.txt').write_text('released by the fixture after both refusals\n')
        observed = [d for d in done if d['name'] in ('WaitProcess', 'ReadProcess') and d['args'].get('process_id') == pid]
        if any(not isinstance(d['result'], dict) or d['result'].get('error') for d in observed):
            self.anomaly('w1', 'root', step, 'process read refused')
            return Reply(final('The background process could not be read.'), usage(step))
        terminal = [d for d in observed if d['result'].get('state') in TERMINAL]
        if not terminal:
            waits = [d for d in observed if d['name'] == 'WaitProcess']
            after = waits[-1]['result'].get('next_after', 0) if waits else 0
            return Reply(calls(('WaitProcess', f'w1-wait-{step}', {'process_id': pid, 'after': after,
                                                                   'wait_seconds': 5})), usage(step))
        if not any(d['name'] == 'ReadProcess' for d in observed):
            return Reply(calls(('ReadProcess', 'w1-outcome', {'process_id': pid})), usage(step))
        if called.count('Write') < 2:
            with self.lock:
                seen['terminal'] = {k: terminal[0]['result'].get(k) for k in ('state', 'exit_code', 'outcome_uncertain')}
            return Reply(calls(('Write', 'w1-write-settled', {'file_path': 'notes.txt', 'content': W1_DRAFT})), usage(step))
        with self.lock:
            seen['settled_write'] = outcome_summary(result(messages, 'w1-write-settled'))
        return Reply(final('The notes update is staged for review now that the writer has settled.'), usage(step))

    def script_u3(self, step, attempt, messages, names):
        return self._process('u3', step, messages, names, 'python3 job.py', 60)

    def script_u2(self, step, attempt, messages, names):
        if step == 0:
            return Reply(calls(('StartCommand', 'u2-start', {'command': 'python3 job.py', 'timeout': 60})), usage(step))
        if step == 1:
            return Reply(final('The background command is waiting for native approval.'), usage(step))
        return self.unexpected('u2', step, messages)

    def script_x2(self, step, attempt, messages, names):
        seen = self.state('x2')
        with self.lock:
            pid = self.shared.get('p1_pid')
        plan = [('ReadProcess', {'process_id': pid}), ('WaitProcess', {'process_id': pid, 'wait_seconds': 6}),
                ('WaitProcess', {'process_id': pid, 'wait_seconds': 1}), ('StopProcess', {'process_id': pid})]
        if step == 0:
            with self.lock:
                seen['offered'] = sorted(set(PROCESS_TOOLS) & set(names))
            if not pid:
                return Reply(final('No earlier process is known.'), usage(step))
        if 0 < step <= len(plan):
            with self.lock:
                seen.setdefault('results', []).append(outcome_summary(result(messages, f'x2-{step - 1}')))
        if step < len(plan):
            name, args = plan[step]
            return Reply(calls((name, f'x2-{step}', args)), usage(step))
        if step == len(plan):
            return Reply(final('The earlier process belongs to another run.'), usage(step))
        return self.unexpected('x2', step, messages)

    # Child prohibition ---------------------------------------------------------

    def script_c1(self, step, attempt, messages, names):
        seen = self.state('c1')
        if step == 0:
            with self.lock:
                seen['root_offered'] = sorted(set(PROCESS_TOOLS) & set(names))
            return Reply(calls(
                ('SpawnAgent', 'c-process', {'task': CHILD_TASK, 'name': 'Process child',
                                             'tools': json.dumps(['Read', 'StartCommand'])}),
                ('SpawnAgent', 'c-effects', {'task': CHILD_TASK, 'name': 'Effect requester',
                                             'tools': json.dumps(['Read', 'RequestAgentEffect', 'ReadAgentEffect'])})),
                usage(step))
        if step >= 12:
            self.anomaly('c1', 'root', step, 'delegation script exceeded its bound')
            return Reply(final('Stopping the synthetic delegation script at its bound.'))
        name, ident = last_call(messages)
        if name == 'SpawnAgent':
            process, effects = result(messages, 'c-process'), result(messages, 'c-effects')
            with self.lock:
                seen['process_spawn'], seen['effects_spawn'] = spawn_summary(process), spawn_summary(effects)
            admitted = [r['agent_id'] for r in (process, effects) if isinstance(r, dict) and r.get('agent_id')]
            if not admitted:
                return Reply(final('No child was admitted.'), usage(step))
            return Reply(calls(('WaitForAgents', f'c-wait-{step}', {'agent_ids': json.dumps(admitted)})), usage(step))
        if name in ('WaitForAgents', 'ReviewAgentResult', 'final'):
            return Reply(calls(('ReadAgentTree', f'c-tree-{step}', {})), usage(step))
        if name == 'ReadAgentTree':
            tree = result(messages, ident) or {}
            nodes = [n for n in tree.get('nodes', []) if isinstance(n, dict)]
            reviews = [n for n in nodes if n.get('status') in NODE_TERMINAL and n.get('review') == 'pending']
            active = [n['id'] for n in nodes if n.get('status') in NODE_ACTIVE]
            if reviews:
                items = []
                for index, node in enumerate(reviews):
                    args = {'agent_id': node['id'], 'generation': node['generation']}
                    if node['status'] == 'completed' and node.get('evidence_ids'):
                        args['evidence_ids'] = json.dumps(node['evidence_ids'])
                    else:
                        args['decision'] = 'dismiss'
                    items.append(('ReviewAgentResult', f'c-review-{step}-{index}', args))
                return Reply(calls(*items), usage(step))
            if active:
                return Reply(calls(('WaitForAgents', f'c-wait-{step}', {'agent_ids': json.dumps(active)})), usage(step))
            return Reply(final('The child could not start anything; nothing was run.'), usage(step))
        return self.unexpected('c1', step, messages)

    def script_child(self, step, attempt, messages, names):
        seen = self.state('c1')
        with self.lock:
            seen['child_offered'] = sorted(set(seen.get('child_offered', [])) | set(names))
        plan = [('c-start', 'RequestAgentEffect', {'tool': 'StartCommand', 'purpose': 'fixture',
                                                   'arguments': json.dumps({'command': 'python3 job.py'})}),
                ('c-async', 'RequestAgentEffect', {'tool': 'RunCommand', 'purpose': 'fixture', 'arguments': json.dumps(
                    {'command': 'python3 job.py', 'asynchronous': True, 'timeout': 60})}),
                ('c-direct', 'StartCommand', {'command': 'python3 job.py'})]
        if 0 < step <= len(plan):
            ident = plan[step - 1][0]
            with self.lock:
                seen.setdefault('child_results', {})[ident] = outcome_summary(result(messages, ident))
        if step < len(plan):
            ident, name, args = plan[step]
            return Reply(calls((name, ident, args)), CHILD_USAGE)
        if step == len(plan):
            return Reply(final('Nothing could be started from this child. Parent verification required.'))
        self.anomaly('c1', 'child', step, 'child step beyond script')
        return Reply(final('Done.'))

    # Stream interruption ----------------------------------------------------------

    def script_s1(self, step, attempt, messages, names):
        seen = self.state('s1')
        if step == 0 and attempt == 1:
            # Visible text plus a partial tool call, then the connection drops.
            return Reply({'content': ABANDONED + ' ', 'tool_calls': [
                {'index': 0, 'id': 's-fragment', 'type': 'function',
                 'function': {'name': 'UIRender', 'arguments': '{"title": "' + FRAGMENT}}]}, mode='truncate')
        if step == 0:
            with self.lock:
                seen['offered'] = 'UIRender' in names
            return Reply(calls(('UIRender', 's-view', {'title': 'Retry flow', 'kind': 'diagram',
                                                       'data': json.dumps(DIAGRAM)})), usage(step))
        if step == 1:
            with self.lock:
                seen['view'] = outcome_summary(result(messages, 's-view'))
            return Reply(final(COMMITTED + ': one view was rendered.'), usage(step))
        return self.unexpected('s1', step, messages)


class Campaign:
    def __init__(self, command, backend):
        self.command, self.backend = command, backend
        self.fixture = Fixture()
        self.checks, self.names = [], set()
        self.facts, self.observations = {}, {}
        self.fatal = None
        self.stderr_tails = []

    # Check bookkeeping ---------------------------------------------------------

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

    # Service helpers -------------------------------------------------------------

    def boot(self, home):
        return Service(self.command, home)

    def close(self, service, keep_tail):
        if keep_tail:
            try:
                service.stderr.seek(0)
                self.stderr_tails.append(service.stderr.read()[-4000:])
            except Exception as exc:
                self.stderr_tails.append('stderr unavailable: ' + bounded(exc))
        service.close()

    def crash(self, service):
        """SIGKILL: no shutdown code runs, exactly like a hard crash."""
        service.process.kill()
        service.process.wait(timeout=10)
        for stream in (service.process.stdin, service.process.stdout):
            try:
                stream.close()
            except Exception:
                pass
        try:
            service.stderr.seek(0)
            self.stderr_tails.append('crash tail: ' + service.stderr.read()[-2000:])
            service.stderr.close()
        except Exception:
            pass

    @staticmethod
    def until(predicate, seconds, label):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f'Timed out after {seconds}s waiting for {label}.')

    def diagnostics(self, run, tag):
        with self.fixture.lock:
            return {'state': run and run.get('state'), 'run_error': run and (run.get('error') or '')[:300],
                    'tools': [[t['call_id'], t['name'], t['state']] for t in (run or {}).get('tools', [])][-20:],
                    'provider_errors': self.fixture.errors[-5:],
                    'anomalies': [a for a in self.fixture.anomalies if a['scenario'] in (tag, 'c1')][-5:]}

    def settle(self, service, cid, rid, tag, seconds=120):
        seen = {}

        def probe():
            seen['run'] = run = service.rpc('run_get', id=cid, run_id=rid)
            if run['state'] in TRANSIENT or service.rpc('state')['active']:
                return None
            return run
        try:
            return self.until(probe, seconds, tag + ' to settle')
        except AssertionError:
            raise AssertionError(json.dumps(self.diagnostics(seen.get('run'), tag))) from None

    def send(self, service, tag, workspace=None, cid=None, steps=48):
        if cid is None:
            cid = service.rpc('new_chat')['id']
            if workspace:
                service.rpc('set_workspace', id=cid, path=str(workspace))
        rid = service.rpc('send', id=cid, text=GOALS[tag], limits={'max_steps': steps, 'max_seconds': 300})['run_id']
        return cid, rid

    def process_scenario(self, service, tag, workspace, markers=(), steps=48):
        """Run to native review, record the pre-approval world, then approve natively."""
        cid, rid = self.send(service, tag, workspace, steps=steps)
        fact = self.facts[tag] = {'cid': cid, 'rid': rid}
        fact['first_state'] = self.settle(service, cid, rid, tag)['state']
        pending = [a for a in service.rpc('get_chat', id=cid)['actions'] if a['status'] == 'pending' and a['kind'] == 'command']
        fact['pending'] = [{'asynchronous': a['payload'].get('asynchronous'), 'timeout': a['payload'].get('timeout'),
                            'argv': a['payload'].get('argv')} for a in pending]
        fact['before_approval'] = {'processes': len(service.rpc('process_list', id=cid)),
                                   'markers': {m: (workspace / m).exists() for m in markers}}
        if len(pending) != 1:
            raise AssertionError(('Expected exactly one pending asynchronous command.', fact['first_state'], fact['pending']))
        fact['action_id'] = pending[0]['id']
        fact['approved_at'] = time.monotonic()
        fact['admitted'] = service.rpc('approve', id=cid, action_id=pending[0]['id'])
        fact['after_admission'] = {m: (workspace / m).exists() for m in markers}
        fact['pid'] = fact['admitted'].get('process_id') if isinstance(fact['admitted'], dict) else None
        return {'first_state': fact['first_state'], 'pending': fact['pending'],
                'admitted_state': fact['admitted'].get('state') if isinstance(fact['admitted'], dict) else None}

    def read(self, service, fact, after=0, limit=8000, wait_seconds=0):
        return service.rpc('process_read', id=fact['cid'], run_id=fact['rid'], process_id=fact['pid'],
                           after=after, limit=limit, wait_seconds=wait_seconds)

    def watch(self, service, fact, seconds=40):
        """Native polling until the process is terminal; records when."""
        deadline = time.monotonic() + seconds
        while True:
            page = self.read(service, fact, limit=1, wait_seconds=1)
            if page.get('state') in TERMINAL:
                fact['terminal_after'] = round(time.monotonic() - fact['approved_at'], 2)
                fact['terminal'] = {k: page.get(k) for k in ('state', 'exit_code', 'outcome_uncertain', 'output_truncated')}
                fact['isolation_present'] = isinstance(page.get('isolation'), str) and bool(page['isolation'])
                return page
            if time.monotonic() > deadline:
                raise AssertionError(('The process never reached a terminal state.', page.get('state')))

    def resumed(self, service, fact, tag, seconds=120):
        cid, rid = fact['cid'], fact['rid']
        self.until(lambda: service.rpc('run_get', id=cid, run_id=rid)['state'] != 'waiting_approval', 30,
                   tag + ' to resume after native approval')
        fact['run'] = run = self.settle(service, cid, rid, tag, seconds)
        return run

    def read_all(self, service, fact, limit=8000, bound=400):
        after, parts, pages = 0, [], []
        for _ in range(bound):
            page = self.read(service, fact, after=after, limit=limit)
            pages.append(page)
            parts.append(page.get('untrusted_process_output') or '')
            if not page.get('more'):
                return ''.join(parts), pages
            following = page.get('next_after')
            assert type(following) is int and following > after, ('A page with more output did not advance.', after, following)
            after = following
        raise AssertionError('Native paging did not terminate within its bound.')

    def stream(self, service, cid, rid, after=0, limit=100):
        return service.rpc('stream_read', id=cid, run_id=rid, after=after, limit=limit)

    # Campaign --------------------------------------------------------------------

    def run(self, scratch):
        home = scratch / 'home'
        home.mkdir()
        self.facts['home'] = home
        for key, folder in PROJECTS.items():
            root = scratch / folder
            root.mkdir()
            for name, body in FIXTURES.items():
                (root / name).write_text(launch(name) + body)
            (root / 'notes.txt').write_text(NOTES)
            self.facts[key] = root
        self.fixture.start()
        try:
            service = self.boot(home)
            try:
                self.phase_a(service)
            finally:
                self.close(service, keep_tail=not self.passing())
            self.after_shutdown()
            service = self.boot(home)
            crashed = False
            try:
                crashed = self.phase_b(service)
            finally:
                self.fixture.release.set()
                if not crashed:
                    self.close(service, keep_tail=not self.passing())
            service = self.boot(home)
            try:
                self.phase_c(service)
            finally:
                self.close(service, keep_tail=not self.passing())
        except Exception as exc:
            self.fatal = bounded(exc, 3000)
        finally:
            self.fixture.stop()
            self.quiesce()

    def phase_a(self, service):
        alpha, kappa = self.facts['alpha'], self.facts['kappa']
        seen = self.fixture.state

        def profile():
            service.rpc('model_save', name='Phase 12 fixture', url=self.fixture.url, model=MODEL, context=CONTEXT)
            service.rpc('setting', key='memory_enabled', value=False)  # memory is not under test here
            state = service.rpc('state')
            self.facts['permissions'] = state['settings']['permissions']
            assert state['guard']['available'] is True, 'The real vendored AgentGuard engine must be available.'
            return {'guard_available': True, 'permission_mode': state['settings']['permissions']['mode']}
        if not self.check('loopback_profile_saved_and_actual_guard_available', profile):
            return

        def platform():
            assert sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file(), (
                'Confined asynchronous commands need macOS sandbox-exec; elsewhere they must fail closed.')
            return {'platform': sys.platform}
        self.check('platform_supports_confined_async_terminal', platform)

        # p1: review, admission, observation and exact UTF-8 paging.
        if self.check('p1_reaches_native_review_and_is_approved',
                      lambda: self.process_scenario(service, 'p1', alpha, markers=('job-started.txt', 'job-done.txt'))):
            with self.fixture.lock:
                self.fixture.shared['p1_pid'] = self.facts['p1'].get('pid')

        def offered():
            assert seen('p1').get('offered') == sorted(PROCESS_TOOLS), seen('p1').get('offered')
            return {'offered': seen('p1')['offered']}
        self.check('p1_process_tools_offered_to_the_root_run', offered)

        def before():
            fact = self.need('p1')
            assert fact['first_state'] == 'waiting_approval', fact['first_state']
            assert fact['pending'] == [{'asynchronous': True, 'timeout': 60, 'argv': ['python3', 'job.py']}], fact['pending']
            assert fact['before_approval'] == {'processes': 0, 'markers': {'job-started.txt': False, 'job-done.txt': False}}, \
                fact['before_approval']
            assert seen('p1').get('start', {}).get('status') == 'awaiting_user_approval', seen('p1').get('start')
            return {'pending': 1, 'processes_before_approval': 0}
        self.check('p1_nothing_runs_before_native_approval', before)

        def admission():
            fact = self.need('p1')
            admitted = fact['admitted']
            assert isinstance(fact['pid'], str) and re.fullmatch(r'[0-9a-f]{32}', fact['pid']), admitted
            assert admitted.get('state') in LIVE and admitted.get('exit_code') is None, admitted
            assert fact['after_admission']['job-done.txt'] is False, 'Approval waited for the command to finish.'
            assert 'pid' not in admitted, 'A control PID reached the native admission result.'
            return {'admitted_state': admitted['state'], 'done_at_admission': False}
        self.check('p1_native_approval_returns_admission_not_completion', admission)

        def p1_observed():
            fact = self.need('p1')
            run = self.resumed(service, fact, 'p1')
            assert seen('p1').get('model_pid') == fact['pid'], 'The resumed root did not see the admitted process id.'
            terminal = seen('p1').get('terminal') or {}
            assert terminal.get('state') == 'succeeded' and terminal.get('exit_code') == 0 \
                and terminal.get('outcome_uncertain') is False, terminal
            assert run['state'] == 'completed', (run['state'], (run.get('error') or '')[:300])
            return {'run_state': run['state'], 'terminal': terminal,
                    'completion_repairs': seen('p1').get('completion_repairs', 0)}
        self.check('p1_resumed_root_observes_success_and_completes', p1_observed)

        def utf8():
            fact = self.need('p1')
            text = seen('p1').get('text')
            pages = seen('p1').get('pages') or []
            assert text == EXPECTED_JOB, ('Model pages did not reassemble the exact output.', sha(text or ''), sha(EXPECTED_JOB))
            assert len(pages) > 1 and all(type(p['next_after']) is int for p in pages), pages
            native, _ = self.read_all(service, fact, limit=5)
            assert native == EXPECTED_JOB and '\ufffd' not in native
            self.facts['p1_text_sha'] = sha(native)
            return {'model_pages': len(pages), 'text_sha256': sha(native)}
        self.check('p1_paged_output_reassembles_exact_split_utf8', utf8)

        # p2: stop after an effect (own project: a cancelled outcome gates its project).
        pi2, pi3, pi4 = self.facts['pi2'], self.facts['pi3'], self.facts['pi4']

        def p2_stop():
            self.process_scenario(service, 'p2', pi2, markers=('partial-effect.txt',))
            fact = self.need('p2')
            page = self.watch(service, fact)
            fact['group_quiet'] = quiet(pi2 / 'parent-beat.txt', pi2 / 'child-beat.txt')
            self.observations['escaped_session_descendant_outlived_stop'] = not quiet(pi2 / 'escaped-beat.txt', seconds=0.6)
            self.resumed(service, fact, 'p2')
            assert page['state'] == 'cancelled' and page['outcome_uncertain'] is True and page.get('exit_code') == -9, \
                fact['terminal']
            stop = seen('p2').get('stop') or {}
            assert not stop.get('error') and stop.get('state') != 'succeeded', stop
            return {'terminal': fact['terminal'], 'terminal_after_seconds': fact['terminal_after']}
        self.check('p2_stop_reports_cancelled_with_uncertain_outcome', p2_stop)

        def p2_effect():
            assert (pi2 / 'partial-effect.txt').read_text() == 'written before stop\n'
            return {'effect_retained': True}
        self.check('p2_partial_effect_is_retained_after_stop', p2_effect)

        def p2_group():
            fact = self.need('p2')
            assert fact.get('group_quiet') is True, 'The process or its in-group child kept running after the observed stop.'
            return {'group_quiet': True}
        self.check('p2_process_group_and_in_group_child_stop', p2_group)

        # p3: timeout.
        def p3():
            self.process_scenario(service, 'p3', pi3)
            fact = self.need('p3')
            page = self.watch(service, fact)
            beat_quiet = quiet(pi3 / 'timeout-beat.txt')
            self.resumed(service, fact, 'p3')
            assert page['state'] == 'timed_out' and page['outcome_uncertain'] is True, fact['terminal']
            assert fact['terminal_after'] < 12, fact['terminal_after']
            assert beat_quiet, 'The timed-out process kept running.'
            return {'terminal': fact['terminal'], 'terminal_after_seconds': fact['terminal_after']}
        self.check('p3_timeout_stops_the_process_with_uncertain_outcome', p3)

        # p4: output burst.
        def p4_limit():
            self.process_scenario(service, 'p4', pi4)
            fact = self.need('p4')
            page = self.watch(service, fact, seconds=30)
            self.resumed(service, fact, 'p4')
            assert page['state'] == 'output_limited' and page['outcome_uncertain'] is True, fact['terminal']
            assert fact['terminal_after'] < 15, fact['terminal_after']
            text, pages = self.read_all(service, fact)
            assert 0 < len(text.encode()) <= OUTPUT_LIMIT, len(text.encode())
            assert pages[-1].get('output_truncated') is True or (pages[-1].get('coverage') or {}).get('stored_complete') is False, \
                'Truncated output was not flagged.'
            self.facts['p4_text_sha'] = sha(text)
            return {'stored_bytes': len(text.encode()), 'pages': len(pages), 'terminal_after_seconds': fact['terminal_after']}
        self.check('p4_output_limit_stops_the_process_and_bounds_retained_output', p4_limit)

        def p4_cursor():
            refused = seen('p4').get('read_refused')
            assert not refused, ('A host-issued cursor was refused on the model route.', refused)
            pages = seen('p4').get('pages') or []
            furthest = max((p['next_after'] for p in pages if type(p['next_after']) is int), default=0)
            assert furthest > 10000, ('The model never paged past 10,000 bytes.', furthest)
            return {'model_pages': len(pages), 'furthest_cursor': furthest}
        self.check('p4_model_follows_next_after_beyond_ten_thousand_bytes', p4_cursor)

        # w1: a live writable process exclusively owns its project until it settles.
        omega = self.facts['omega']

        def w1_exclusive():
            with self.fixture.lock:
                self.fixture.shared['w1_root'] = str(omega)
            self.process_scenario(service, 'w1', omega)
            fact = self.need('w1')
            page = self.watch(service, fact, seconds=40)
            self.resumed(service, fact, 'w1')
            refusals = seen('w1').get('live_refusals') or {}
            write, second = refusals.get('write') or {}, refusals.get('second') or {}
            assert write.get('error') and not write.get('action_present'), ('A Write was staged inside a live writer.', write)
            assert second.get('error') and not second.get('action_present'), ('A second writer was staged.', second)
            commands = [a for a in service.rpc('get_chat', id=fact['cid'])['actions'] if a['kind'] == 'command']
            assert [a['status'] for a in commands] == ['applied'], [a['status'] for a in commands]
            assert (omega / 'notes.txt').read_text() == NOTES, 'The project changed while the writer was live.'
            assert page['state'] == 'succeeded' and page['outcome_uncertain'] is False, fact['terminal']
            assert launches(omega, 'job.py') == 0, 'The refused second writer ran.'
            return {'terminal': fact['terminal'], 'refused': ['Write', 'StartCommand']}
        self.check('w1_live_writable_process_blocks_root_edits_and_other_writers', w1_exclusive)

        def w1_settled():
            fact = self.need('w1')
            write = seen('w1').get('settled_write') or {}
            assert write.get('status') == 'awaiting_user_approval' and write.get('action_present'), write
            edits = [a for a in service.rpc('get_chat', id=fact['cid'])['actions'] if a['kind'] == 'edit']
            assert [(a['status'], a['payload'].get('path')) for a in edits] == [('pending', 'notes.txt')], \
                [(a['status'], a['payload'].get('path')) for a in edits]
            assert (omega / 'notes.txt').read_text() == NOTES, 'An edit applied without native approval.'
            return {'pending_edits': 1}
        self.check('w1_root_edits_resume_only_after_the_writer_settles', w1_settled)

        # x2: another run of the p1 conversation.
        def x2():
            p1 = self.need('p1')
            cid, rid = self.send(service, 'x2', cid=p1['cid'], steps=12)
            self.facts['x2'] = {'cid': cid, 'rid': rid}
            self.facts['x2']['run'] = self.settle(service, cid, rid, 'x2')
            results = seen('x2').get('results') or []
            assert len(results) >= 3, results
            assert all(r.get('error') and not r.get('has_output') for r in results[:3]), results
            assert self.read(service, p1)['state'] == 'succeeded'
            return {'refused_reads_and_waits': 3}
        self.check('x2_other_run_cannot_read_or_wait_on_a_process', x2)

        def x2_stop():
            p1, fact = self.need('p1'), self.need('x2')
            results = seen('x2').get('results') or []
            run = fact['run']
            assert run['state'] != 'needs_reconciliation', (
                'A refused StopProcess for a process outside this run was recorded as an uncertain effect.')
            assert len(results) == 4 and results[3].get('error') and not results[3].get('has_output'), results
            assert self.read(service, p1)['state'] == 'succeeded'
            return {'run_state': run['state']}
        self.check('x2_foreign_stop_is_a_clean_refusal_not_an_uncertain_effect', x2_stop)

        def native_scope():
            p1, p2, x2_fact = self.need('p1'), self.need('p2'), self.need('x2')
            baseline = self.read(service, p1, limit=10)
            assert baseline.get('untrusted_process_output'), 'The owning scope could not read its own process.'
            refusals = 0
            for cid, rid, pid in [(p2['cid'], p2['rid'], p1['pid']), (p2['cid'], p1['rid'], p1['pid']),
                                  (p1['cid'], x2_fact['rid'], p1['pid']), (p1['cid'], p1['rid'], p2['pid'])]:
                rejected(service, 'process_read', id=cid, run_id=rid, process_id=pid, after=0, limit=10, wait_seconds=0)
                rejected(service, 'process_stop', id=cid, run_id=rid, process_id=pid)
                refusals += 2
            for field, value in [('after', -1), ('after', True), ('after', 1.5), ('limit', 0), ('limit', 8001),
                                 ('limit', True), ('wait_seconds', 6), ('wait_seconds', -1), ('wait_seconds', True),
                                 ('wait_seconds', float('nan')), ('wait_seconds', float('inf'))]:
                params = {'id': p1['cid'], 'run_id': p1['rid'], 'process_id': p1['pid'], 'after': 0, 'limit': 10,
                          'wait_seconds': 0, field: value}
                rejected(service, 'process_read', **params)
                refusals += 1
            assert self.read(service, p1)['state'] == 'succeeded'
            return {'refusals': refusals}
        self.check('native_process_routes_refuse_foreign_scope_and_malformed_bounds', native_scope)

        def no_native_start():
            p1 = self.need('p1')
            before = len(service.rpc('get_chat', id=p1['cid'])['actions'])
            processes = len(service.rpc('process_list', id=p1['cid']))
            for method in ('process_start', 'start_process', 'process_resume', 'process_retry'):
                rejected(service, method, id=p1['cid'], run_id=p1['rid'], command='python3 job.py')
            rejected(service, 'terminal_propose', id=p1['cid'], command='python3 job.py', asynchronous=True)
            assert len(service.rpc('get_chat', id=p1['cid'])['actions']) == before
            assert len(service.rpc('process_list', id=p1['cid'])) == processes
            return {'refused_routes': 5}
        self.check('no_native_route_starts_or_resumes_a_process', no_native_start)

        # c1: child prohibition through the real scheduler.
        def c1():
            cid, rid = self.send(service, 'c1', alpha, steps=16)
            self.facts['c1'] = {'cid': cid, 'rid': rid}
            self.settle(service, cid, rid, 'c1', seconds=120)
            c1_seen = seen('c1')
            assert c1_seen.get('process_spawn', {}).get('error') and not c1_seen['process_spawn'].get('agent_id'), \
                ('A child with StartCommand was admitted.', c1_seen.get('process_spawn'))
            with self.fixture.lock:
                child_records = [r for r in self.fixture.records if r['role'] == 'child' and r['scenario'] == 'c1']
            assert child_records, 'The effect-requesting child never ran, so its prohibition is unproven.'
            assert not any(set(r['tools']) & set(PROCESS_TOOLS) for r in child_records), 'A process tool was offered to a child.'
            results = c1_seen.get('child_results') or {}
            assert all(v.get('error') for v in results.values()), ('A child process request was accepted.', results)
            assert not [a for a in service.rpc('get_chat', id=cid)['actions'] if a['kind'] == 'command']
            assert not service.rpc('process_list', id=cid)
            return {'child_attempts': len(child_records), 'child_requests_refused': len(results)}
        self.check('c1_process_tools_never_reach_a_child', c1)

        # s1: stream interruption with a partial tool call.
        def s1():
            cid, rid = self.send(service, 's1', steps=12)
            self.facts['s1'] = {'cid': cid, 'rid': rid}
            run = self.settle(service, cid, rid, 's1')
            chat = service.rpc('get_chat', id=cid)
            views = [t for t in run['tools'] if t['name'] == 'UIRender']
            with self.fixture.lock:
                first = [r for r in self.fixture.records if r['scenario'] == 's1' and r['step'] == 0]
            assert seen('s1').get('offered') is True, 'UIRender was not offered, so tool replay is unattributable.'
            assert len(first) == 2 and first[0]['mode'] == 'truncate', [(r['attempt'], r['mode']) for r in first]
            assert run['state'] == 'completed', run['state']
            assert len(views) == 1 and len(chat['artifacts']) == 1, (len(views), len(chat['artifacts']))
            return {'step0_attempts': len(first), 'views': 1}
        self.check('s1_reconnect_completes_with_exactly_one_tool_effect', s1)

        def s1_transcript():
            fact = self.need('s1')
            messages = service.rpc('get_chat', id=fact['cid'])['messages']
            text = json.dumps(messages)
            assert ABANDONED not in text and FRAGMENT not in text, 'Abandoned attempt content reached the transcript.'
            assert COMMITTED in messages[-1]['content']
            return {'messages': len(messages)}
        self.check('s1_abandoned_text_never_reaches_the_transcript', s1_transcript)

        def s1_stream():
            fact = self.need('s1')
            page = self.stream(service, fact['cid'], fact['rid'])
            events = stream_events(page)
            groups = attempts(events)
            dropped = [g for g in groups.values() if ABANDONED in g['text']]
            kept = [g for g in groups.values() if COMMITTED in g['text']]
            assert dropped and all(g['state'] in {'discarded', 'interrupted'} for g in dropped), groups
            assert kept and all(g['state'] == 'committed' for g in kept), groups
            assert all(g['state'] in SETTLED for g in groups.values()), 'An attempt was left open after the run settled.'
            assert page.get('more') is False, page.get('more')
            self.facts['s1_stream_sha'] = sha(json.dumps(events, sort_keys=True))
            return {'events': len(events), 'attempts': len(groups),
                    'states': sorted(str(g['state']) for g in groups.values())}
        self.check('s1_stream_presents_the_abandoned_attempt_only_as_settled_noncommitted', s1_stream)

        def s1_fragments():
            fact = self.need('s1')
            text = json.dumps(self.stream(service, fact['cid'], fact['rid']))
            for forbidden in (FRAGMENT, '"arguments"', self.fixture.url, 'Authorization', 'api_key'):
                assert forbidden not in text, 'The presentation stream contains: ' + forbidden
            return {'bytes': len(text)}
        self.check('s1_stream_never_contains_tool_fragments_or_credentials', s1_fragments)

        def s1_paging():
            fact = self.need('s1')
            events = stream_events(self.stream(service, fact['cid'], fact['rid']))
            after, paged = 0, []
            for _ in range(len(events) + 2):
                page = self.stream(service, fact['cid'], fact['rid'], after, 1)
                paged += stream_events(page)
                if not page.get('more'):
                    break
                assert type(page.get('next_after')) is int and page['next_after'] > after, page.get('next_after')
                after = page['next_after']
            assert paged == events, 'Paged stream events differ from one full page.'
            return {'pages': len(paged)}
        self.check('s1_stream_paging_is_monotonic_and_complete', s1_paging)

        def stream_scope():
            s1_fact, p1 = self.need('s1'), self.need('p1')
            self.stream(service, s1_fact['cid'], s1_fact['rid'], 0, 1)
            refusals = 0
            for cid, rid in [(p1['cid'], s1_fact['rid']), (s1_fact['cid'], p1['rid'])]:
                rejected(service, 'stream_read', id=cid, run_id=rid)
                refusals += 1
            for field, value in [('limit', 0), ('limit', 101), ('limit', True), ('after', -1), ('after', True), ('after', 'x')]:
                rejected(service, 'stream_read', **{'id': s1_fact['cid'], 'run_id': s1_fact['rid'], field: value})
                refusals += 1
            return {'refusals': refusals}
        self.check('stream_read_refuses_foreign_scope_and_malformed_bounds', stream_scope)

        # k1: a process left live for normal shutdown, in its own project.
        def k1():
            self.process_scenario(service, 'k1', kappa)
            fact = self.need('k1')
            self.resumed(service, fact, 'k1')
            fact['live_before_shutdown'] = self.read(service, fact).get('state')
            assert fact['live_before_shutdown'] in LIVE, fact['live_before_shutdown']
            assert not quiet(kappa / 'close-beat.txt', seconds=0.6), 'The k1 process was not running before shutdown.'
            fact['launches'] = launches(kappa, 'beat.py close-beat.txt 25')
            return {'state_before_shutdown': fact['live_before_shutdown'], 'run_state': fact['run']['state']}
        self.check('k1_process_live_before_normal_shutdown', k1)
        self.facts['records_before_shutdown'] = len(self.fixture.records)

    def after_shutdown(self):
        kappa = self.facts['kappa']

        def stopped():
            fact = self.need('k1')
            assert fact.get('live_before_shutdown') in LIVE, 'k1 was not live at shutdown; shutdown cancellation is unproven.'
            assert quiet(kappa / 'close-beat.txt'), 'A live process kept running after normal service shutdown.'
            return {'quiet_after_shutdown': True}
        self.check('k1_normal_shutdown_stops_the_live_process', stopped)

    def phase_b(self, service):
        kappa, upsilon = self.facts['kappa'], self.facts['upsilon']

        def outcomes():
            expected = {'p1': 'succeeded', 'p2': 'cancelled', 'p3': 'timed_out', 'p4': 'output_limited'}
            states = {}
            for tag, state in expected.items():
                page = self.read(service, self.need(tag), limit=1)
                states[tag] = page.get('state')
            assert states == expected, states
            p1_text, _ = self.read_all(service, self.need('p1'))
            p4_text, _ = self.read_all(service, self.need('p4'))
            assert sha(p1_text) == self.need('p1_text_sha') and sha(p4_text) == self.need('p4_text_sha')
            return {'states': states}
        self.check('restart_preserves_settled_process_outcomes_and_output', outcomes)

        def stream_kept():
            fact = self.need('s1')
            events = stream_events(self.stream(service, fact['cid'], fact['rid']))
            assert sha(json.dumps(events, sort_keys=True)) == self.need('s1_stream_sha'), 'Restart changed presentation events.'
            return {'events': len(events)}
        self.check('restart_preserves_the_presentation_stream', stream_kept)

        def k1_after():
            fact = self.need('k1')
            page = self.read(service, fact, limit=1)
            assert page.get('state') in {'cancelled', 'unknown'} and page.get('outcome_uncertain') is True, \
                (page.get('state'), page.get('outcome_uncertain'))
            assert quiet(kappa / 'close-beat.txt'), 'Restart resumed the stopped process.'
            assert launches(kappa, 'beat.py close-beat.txt 25') == fact['launches'], 'Restart relaunched the command.'
            return {'state': page['state']}
        self.check('k1_restart_reports_an_uncertain_outcome_and_never_resumes', k1_after)

        def no_replay():
            before = self.need('records_before_shutdown')
            lines = [(key, line) for key in PROJECTS for line in
                     ('job.py', 'slow.py', 'burst.py', 'gate.py', 'beat.py timeout-beat.txt 20', 'beat.py close-beat.txt 25')]
            counts = {f'{key}:{line}': launches(self.facts[key], line) for key, line in lines}
            time.sleep(6)  # the continuation dispatcher wakes at least every five seconds
            assert len(self.fixture.records) == before, 'Restart dispatched a model attempt.'
            assert counts == {f'{key}:{line}': launches(self.facts[key], line) for key, line in lines}, \
                'Restart relaunched a command.'
            return {'provider_attempts': before, 'launched_commands': sum(counts.values())}
        self.check('restart_replays_no_provider_attempt_or_command', no_replay)

        crashed = False

        def crash():
            nonlocal crashed
            self.process_scenario(service, 'u1', upsilon)
            fact = self.need('u1')
            if not fact.get('pid'):
                raise AssertionError('u1 was not admitted.')
            if not self.fixture.blocked.wait(BLOCK_SECONDS):
                raise AssertionError('The u1 root never reached its open stream attempt.')

            def visible():
                events = stream_events(self.stream(service, fact['cid'], fact['rid']))
                return any(OPEN_TEXT in (e.get('text') or '') for e in events)
            try:
                fact['open_attempt_visible'] = bool(self.until(visible, 10, 'the open attempt to be presented'))
            except AssertionError:
                fact['open_attempt_visible'] = False
            fact['beat_alive_at_crash'] = not quiet(upsilon / 'crash-beat.txt', seconds=0.6)
            fact['state_at_crash'] = self.read(service, fact, limit=1).get('state')
            fact['records_at_crash'] = self.fixture.attempts('u1')
            fact['launches'] = launches(upsilon, 'crashjob.py')
            self.crash(service)
            crashed = True
            self.fixture.release.set()
            assert fact['state_at_crash'] in LIVE, fact['state_at_crash']
            assert fact['open_attempt_visible'], 'The open attempt was not durably presented before the crash.'
            return {'state_at_crash': fact['state_at_crash'], 'beat_alive_at_crash': fact['beat_alive_at_crash']}
        self.check('u1_hard_crash_with_a_live_process_and_an_open_attempt', crash)
        return crashed

    def phase_c(self, service):
        upsilon = self.facts['upsilon']

        def unknown():
            fact = self.need('u1')
            page = self.read(service, fact, limit=4000)
            assert page.get('state') == 'unknown' and page.get('outcome_uncertain') is True and page.get('exit_code') is None, \
                (page.get('state'), page.get('outcome_uncertain'), page.get('exit_code'))
            assert 'PHASE12-CRASH-JOB-RUNNING' in (page.get('untrusted_process_output') or ''), 'Output seen before the crash was lost.'
            time.sleep(2)
            assert launches(upsilon, 'crashjob.py') == fact['launches'] == 1, 'Recovery relaunched the command.'
            return {'state': 'unknown', 'launches': 1}
        self.check('u1_crash_recovers_the_process_as_unknown_not_replayed', unknown)

        def no_control():
            fact = self.need('u1')
            alive_before = not quiet(upsilon / 'crash-beat.txt', seconds=0.6)
            refusal = rejected(service, 'process_stop', id=fact['cid'], run_id=fact['rid'], process_id=fact['pid'])
            alive_after = not quiet(upsilon / 'crash-beat.txt', seconds=1.0)
            self.observations['crash_orphan_alive_after_restart'] = alive_before
            self.observations['crash_orphan_alive_after_stop_request'] = alive_after
            assert alive_after or not alive_before, 'The orphan stopped right after a stop request: a recorded PID may have been signalled.'
            return {'refused': bool(refusal), 'orphan_alive_before_stop': alive_before, 'orphan_alive_after_stop': alive_after}
        self.check('u1_unknown_process_has_no_pid_control', no_control)

        def interrupted():
            fact = self.need('u1')
            groups = attempts(stream_events(self.stream(service, fact['cid'], fact['rid'])))
            opened = [g for g in groups.values() if OPEN_TEXT in g['text']]
            assert opened and all(g['state'] == 'interrupted' for g in opened), groups
            messages = service.rpc('get_chat', id=fact['cid'])['messages']
            assert OPEN_TEXT not in json.dumps(messages), 'Open attempt text reached the transcript.'
            return {'attempts': len(groups)}
        self.check('u1_open_attempt_recovered_as_interrupted_never_committed', interrupted)

        def not_resumed():
            fact = self.need('u1')
            time.sleep(6)
            run = service.rpc('run_get', id=fact['cid'], run_id=fact['rid'])
            assert run['state'] not in TRANSIENT, run['state']
            assert self.fixture.attempts('u1') == fact['records_at_crash'], 'Recovery dispatched another model attempt.'
            return {'run_state': run['state']}
        self.check('u1_run_is_not_resumed_or_replayed_after_crash', not_resumed)

        def inspection():
            u1 = self.need('u1')
            cid, rid = self.send(service, 'u2', upsilon, steps=8)
            self.settle(service, cid, rid, 'u2')
            pending = [a for a in service.rpc('get_chat', id=cid)['actions'] if a['status'] == 'pending' and a['kind'] == 'command']
            assert len(pending) == 1, pending
            rejected(service, 'approve', id=cid, action_id=pending[0]['id'])
            assert not service.rpc('process_list', id=cid), 'A new start ran before the unknown outcome was inspected.'
            receipt = service.rpc('process_inspect', id=u1['cid'], run_id=u1['rid'], process_id=u1['pid'],
                                  observation='still_uncertain', note='Synthetic fixture inspection; outcome still unverified.')
            assert receipt.get('authorization') == 'none' and receipt.get('completion_verified') is False, receipt
            assert self.read(service, u1, limit=1).get('state') == 'unknown', 'Inspection rewrote the recorded outcome.'
            return {'blocked_before_inspection': True, 'inspection_recorded': True}
        self.check('u2_new_start_waits_for_native_inspection_of_an_unknown_outcome', inspection)

        def fresh_work():
            u1 = self.need('u1')
            self.process_scenario(service, 'u3', upsilon)
            fact = self.need('u3')
            self.observations['fresh_work_admitted_while_crash_orphan_alive'] = not quiet(
                upsilon / 'crash-beat.txt', seconds=0.6)
            page = self.watch(service, fact)
            self.resumed(service, fact, 'u3')
            assert page['state'] == 'succeeded', fact['terminal']
            assert self.read(service, u1, limit=1).get('state') == 'unknown', 'Fresh work rewrote the old outcome.'
            old_run = service.rpc('run_get', id=u1['cid'], run_id=u1['rid'])
            assert old_run['state'] != 'completed', 'The crashed run was treated as verified.'
            assert launches(upsilon, 'crashjob.py') == 1, 'Fresh work replayed the unknown command.'
            return {'fresh_state': page['state'], 'old_run_state': old_run['state']}
        self.check('u3_inspection_permits_fresh_reviewed_work_but_never_verifies_the_unknown_outcome', fresh_work)

        def deletion():
            k1 = self.need('k1')
            page = self.read(service, k1, limit=1)
            assert page.get('outcome_uncertain') is True, 'k1 has no uncertain outcome to protect.'
            rejected(service, 'delete_chat', id=k1['cid'])
            after = self.read(service, k1, limit=1)
            assert after.get('state') == page.get('state'), 'A refused deletion changed the recorded outcome.'
            return {'state': page.get('state'), 'deletion_refused': True}
        self.check('uninspected_uncertain_outcome_blocks_conversation_deletion', deletion)

        def clean():
            with self.fixture.lock:
                assert not self.fixture.errors, self.fixture.errors[-5:]
                assert not self.fixture.anomalies, self.fixture.anomalies[-5:]
            return {'provider_attempts': len(self.fixture.records)}
        self.check('synthetic_provider_protocol_clean', clean)

    def quiesce(self):
        """Wait (bounded) until every fixture heartbeat is quiet before cleanup."""
        roots = [self.facts[k] for k in PROJECTS if k in self.facts]
        paths = [root / name for root in roots for name in HEARTBEATS]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if quiet(*paths, seconds=1.5):
                self.observations['fixture_processes_quiesced'] = True
                return
        self.observations['fixture_processes_quiesced'] = False


def owned_backend(parser, value):
    given = Path(value)
    try:
        info = os.lstat(given)
    except OSError as exc:
        parser.error('--backend is not readable: ' + bounded(exc))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        parser.error('--backend must be a regular file, not a symbolic link.')
    if hasattr(os, 'getuid') and info.st_uid != os.getuid():
        parser.error('--backend must be owned by the current user.')
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        parser.error('--backend must not be group or world writable.')
    if not os.access(given, os.X_OK):
        parser.error('--backend must be executable.')
    return given.resolve()


def checkout_digests():
    """Digests of the source backend and this validator; a vanished file maps to None."""
    paths = ['backend/main.py', *[p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'scripts/validate_phase12.py', 'scripts/validate_runtime.py']
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
    parser.add_argument('--keep-scratch', action='store_true', help='keep the temporary home/projects even on success')
    args = parser.parse_args()
    report_path = args.report.expanduser()
    if os.path.lexists(report_path):
        parser.error('--report already exists; move it aside or choose a new path (earlier raw evidence, '
                     'including a failed run, is never overwritten).')
    backend = owned_backend(parser, args.backend) if args.backend else None
    command = [str(backend)] if backend else [sys.executable, str(ROOT / 'backend/main.py')]
    backend_before = sha(backend.read_bytes()) if backend else None
    sources = checkout_digests()
    scratch = Path(tempfile.mkdtemp(prefix='pebble-phase12-')).resolve()
    started = time.monotonic()
    campaign = Campaign(command, backend)
    try:
        campaign.run(scratch)
    finally:
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
    keep = args.keep_scratch or not passed or not campaign.observations.get('fixture_processes_quiesced')
    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    with campaign.fixture.lock:
        records = [dict(r) for r in campaign.fixture.records]
        errors, anomalies = list(campaign.fixture.errors), list(campaign.fixture.anomalies)
    report = {
        'date': datetime.now(timezone.utc).isoformat(), 'kind': 'frozen' if backend else 'source',
        'passed': passed,
        # The parent verifier contract: unique names of checks that actually passed.
        'checks': [c['name'] for c in results if c['passed']],
        'failed': [c['name'] for c in results if not c['passed']],
        'check_results': results, 'fatal_error': campaign.fatal,
        # Measured residuals. These are recorded facts, not pass/fail claims; see
        # docs/HARNESS_PHASE12_THREAT_MODEL.md for what is and is not guaranteed.
        'observations': campaign.observations,
        'duration_seconds': round(time.monotonic() - started, 1),
        'synthetic_provider_attempts': len(records), 'request_metadata': records,
        'provider_errors': errors, 'provider_anomalies': anomalies,
        'backend_sha256': backend_before,
        'backend_path': str(backend) if backend else None,
        'source_sha256': sources,
        'source_note': ('Digests of this checkout. In frozen mode they do not prove the executable was built from '
                        'these files; the candidate manifest is bound separately.'),
        'scratch_preserved': str(scratch) if keep else None,
        'backend_stderr_tail': campaign.stderr_tails if not passed else [],
        'limits': ('Owned loopback synthetic provider, temporary home/projects, actual service JSON-RPC, native '
                   'approval, the actual vendored AgentGuard and actual macOS confinement of tiny self-terminating '
                   'fixture scripts on this machine. No live model, account, credential, billing, installer, '
                   'signed/installed desktop, other-platform or frontier evidence. Fixture evidence, not an '
                   'independent human security audit.'),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, 'x', encoding='utf-8') as out:
        out.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'kind': report['kind'], 'passed': passed, 'passed_checks': len(report['checks']),
                      'failed': report['failed'], 'fatal_error': campaign.fatal,
                      'observations': campaign.observations,
                      'scratch_preserved': report['scratch_preserved']}, indent=2))
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
