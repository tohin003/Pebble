"""Phase 14 integrated adversarial suite: execution and recovery hardening.

Every case drives the checked-in App, its real Store/RunJournal/TimeLedger, the
authorization layers (tool_authz), the typed command plans (command_plans), the
egress + credential brokers, the terminal-output sanitizer (terminal_text), the
reconcile readback path and the platform registry, on temporary projects under
pytest tmp_path. A small number of cases need the real macOS sandbox, real shim
and real anchor; they are marked `macos`.

Signal safety (common.md / E1d): the only cases that reach a real anchor's
sandbox-instance kill(-1) sweep run it inside a fresh sandbox instance the test
itself created, after the anchor proves SIGURG scoping against an outside canary
(the test-only `interlock_abort_path` plan field). The `confined_host` fixture
adds that canary and interlock to every job plan and asserts, at teardown, that
no outside canary was reached or killed. This suite starts no new escape
payloads of its own: the setsid/double-fork/posix_spawn/closed-fd/anchor-kill
matrix lives in W1's test_containment.py and test_job_shim.py (mapped in
docs/HARNESS_PHASE14_THREAT_MODEL.md); here we check the App-level outcomes.

An expected refusal is a passing adversarial case. The one real gap W6 found
(F6-sync) was kept as a strict xfail with a patch proposal in
.runtime/phase14-claude/adversarial-note.md; the parent applied the patch and
turned the case into an ordinary regression test (final section).
"""

import copy
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import command_plans, job_shim, platform_providers, reconcile, terminal, terminal_text
from pebble.app import App
from pebble.context import encode_result
from pebble.credential_broker import CredentialBroker
from pebble.egress import EgressBroker
from pebble.permissions import DEFAULT, GROUP
from pebble.policy import PolicyError, Workspace, digest
from pebble.time_ledger import TimeLedger
from pebble.tool_authz import Actor, canonical_catalogue, catalogue_digest
from pebble.tool_authz import check as authz_check
from pebble.tool_authz import pin as authz_pin
from pebble.tool_catalogue import TOOLS

MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
macos = pytest.mark.skipif(not MAC, reason='Real sandbox-exec confinement is a macOS product surface.')
PROFILE = {'id': 'phase14-fixture', 'kind': 'openai', 'name': 'Phase 14 fixture', 'url': 'https://unused.invalid',
           'model': 'fixture', 'context': 32768, 'budget': 16384, 'tools': True}
GOAL = 'Run the hardened background check in this project.'
JOB = "print('phase14 job')\n"


# --- outside canary and the anchor's SIGURG interlock (identical to W1/W4) ---------

CANARY = '''import os, signal, sys, time
signal.signal(signal.SIGURG, lambda *a: open(sys.argv[1] + '.urg', 'a').write('x'))
end = time.time() + 120
while time.time() < end:
    open(sys.argv[1] + '.t', 'w').write(str(time.time())); os.replace(sys.argv[1] + '.t', sys.argv[1] + '.beat'); time.sleep(0.05)
'''


def outside_canary(folder):
    beat = Path(folder) / ('canary-' + uuid.uuid4().hex[:8])
    proc = subprocess.Popen([sys.executable, '-I', '-c', CANARY, str(beat)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(200):
        if Path(str(beat) + '.beat').exists():
            break
        time.sleep(0.02)
    return {'proc': proc, 'beat': Path(str(beat) + '.beat'), 'urg': Path(str(beat) + '.urg')}


def canary_result(canary):
    alive = False
    for _ in range(20):
        try:
            alive = time.time() - float(canary['beat'].read_text()) < 1.5
            break
        except (OSError, ValueError):
            time.sleep(0.05)
    canary['proc'].terminate()
    canary['proc'].wait(timeout=10)
    return canary['urg'].exists(), alive


def wait_for(predicate, seconds=25, step=0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


# --- controlled worker at the async_terminal boundary (never spawns) --------------

class Fixed:
    """A worker whose signature lacks jobdir/launcher, so ProcessJobs treats it as
    unsupervised: no real sandbox, no containment gate. It reports a dummy anchor
    PID (evidence only) and returns a fixed outcome."""

    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, payload, cancel, on_started, on_output, *, timeout=300, max_output=192000):
        self.calls.append(copy.deepcopy(payload))
        on_started({'pid': 999_000 + len(self.calls), 'isolation': self.result.get('isolation', 'fixture')})
        return dict(self.result)


def install(monkeypatch, worker):
    import pebble.async_terminal as at
    import pebble.process_jobs as pj
    monkeypatch.setattr(at, 'run_async_command', worker)
    if getattr(pj, 'run_async_command', None) is not None:
        monkeypatch.setattr(pj, 'run_async_command', worker, raising=False)


# --- the App host -----------------------------------------------------------------

class Host:
    def __init__(self, tmp_path, monkeypatch, worker=None, mode='ask'):
        from pebble.agent_manager import Agents
        self.tmp = tmp_path.resolve()
        self.home = self.tmp / 'data'
        self.project = self.tmp / 'project'
        self.project.mkdir(exist_ok=True)
        (self.project / 'job.py').write_text(JOB)
        monkeypatch.setattr(Agents, 'start', lambda self: None)
        if worker is not None:
            install(monkeypatch, worker)
        self.guard_action, self.guard_calls, self.events = 'allow', [], []
        self.monkeypatch = monkeypatch
        self.boot()
        self.permissions(mode)
        self.cid = self.app.new_chat(str(self.project))['id']

    def boot(self):
        app = App(self.home, self.tmp / 'resources', lambda *a: self.events.append(a))
        self.app = app
        self.monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(PROFILE))

        def handle(event, root, session, **kwargs):
            self.guard_calls.append({'event': event, **kwargs})
            return {'action': self.guard_action if event == 'pre_tool_use' else 'allow'}
        self.monkeypatch.setattr(app.guard, 'handle', handle)
        return app

    def restart(self):
        """A new service on the same data folder, as after a backend crash."""
        self.release()
        self.app.close()
        return self.boot()

    def permissions(self, mode='ask', **tools):
        self.app.setting('permissions', {'mode': mode, 'tools': {**DEFAULT['tools'], **tools}})

    def root(self, steps=24, seconds=600):
        app = self.app
        chat = app.chat(self.cid)
        config = app.runtime_config(chat, False, {'max_steps': steps, 'max_seconds': seconds})
        rid = app.runs.create(self.cid, GOAL, config)
        app.runs.transition(rid, 'running')
        return rid

    def own(self, rid):
        self.app.controller.live = None
        self.app.controller.enter(self.app.runs.get(rid, self.cid))
        self.app.active, self.app.active_run = self.cid, rid

    def release(self):
        self.app.active = self.app.active_run = None
        self.app.controller.live = None

    def stage(self, rid, command='python3 job.py', **extra):
        self.own(rid)
        try:
            return self.app.execute('StartCommand', {'command': command, **extra}, Workspace(self.project),
                                    self.cid, call_id='1:' + uuid.uuid4().hex[:12])
        finally:
            self.release()

    def admit(self, rid, command='python3 job.py', **extra):
        staged = self.stage(rid, command, **extra)
        assert staged.get('status') == 'awaiting_user_approval' and staged.get('action_id'), staged
        return staged['action_id'], self.app.approve(self.cid, staged['action_id'])

    def row(self, process_id):
        return self.app.store.query('SELECT * FROM process_jobs WHERE id=?', (process_id,))[0]

    def settled(self, process_id, seconds=25):
        assert wait_for(lambda: self.row(process_id)['state'] not in ('admitted', 'running'), seconds), \
            self.row(process_id)['state']
        return self.row(process_id)

    def close(self):
        self.release()
        self.app.active = self.app.active_run = None
        self.app.close()


@pytest.fixture
def host(tmp_path, monkeypatch):
    value = Host(tmp_path, monkeypatch)
    yield value
    value.close()


@pytest.fixture
def confined_host(tmp_path, monkeypatch):
    """Real shim+anchor. Every job plan gets an outside canary and the anchor's
    SIGURG interlock; teardown proves no outside process was reached or killed."""
    canaries, real = [], job_shim.write_plan

    def write_plan(jobdir, plan):
        canary = outside_canary(tmp_path)
        canaries.append(canary)
        return real(jobdir, {**plan, 'interlock_abort_path': str(canary['urg'])})
    monkeypatch.setattr(job_shim, 'write_plan', write_plan)
    value = Host(tmp_path, monkeypatch)
    (value.project / 'ok.py').write_text("print('contained ok')\n")
    (value.project / 'env.py').write_text(
        "import os, sys\nfor k, v in sorted(os.environ.items()):\n    print(k + '=' + v)\n"
        "print('argv=' + repr(sys.argv))\n")
    yield value
    value.close()
    assert [canary_result(c) for c in canaries] == [(False, True)] * len(canaries), \
        'A job sweep reached or killed a process outside its sandbox.'


# =====================================================================================
# 1. Containment escapes (App-level outcomes; escape payloads live in W1 tests)
# =====================================================================================

@macos
def test_contained_job_settles_certainly_and_spares_the_outside_canary(confined_host):
    host = confined_host
    rid = host.root()
    _, admitted = host.admit(rid, command='python3 ok.py', timeout=30)
    row = host.settled(admitted['process_id'])
    assert (row['state'], row['exit_code'], row['outcome_uncertain']) == ('succeeded', 0, 0), dict(row)
    assert row['contained'] == 1 and 'shim+anchor' in (row['isolation'] or '')
    # The canary assertion in the fixture teardown proves the kill(-1) sweep stayed
    # inside this job's sandbox instance (signal scoping), as E1d requires.


def test_containment_lost_outcome_is_uncertain_and_holds_the_project_gate(tmp_path, monkeypatch):
    """A supervisor that reports containment_lost settles uncertain, and the uncertain
    outcome holds the project gate: no further effect runs until it is inspected."""
    worker = Fixed({'state': 'unknown', 'exit_code': None,
                    'isolation': 'macOS sandbox · shim+anchor · containment lost', 'outcome_uncertain': True})
    host = Host(tmp_path, monkeypatch, worker=worker)
    try:
        rid = host.root()
        _, admitted = host.admit(rid, command='python3 job.py', timeout=30)
        row = host.settled(admitted['process_id'])
        assert (row['state'], row['outcome_uncertain']) == ('unknown', 1), dict(row)
        assert host.app.process_inspections.pending(str(Workspace(host.project).root))
        # The gate is held: an edit or another command in this project is refused.
        host.own(rid)
        try:
            with pytest.raises(PolicyError, match='uncertain command outcome'):
                host.app.perform_action({'kind': 'edit', '_native_reviewed': True, 'payload': {
                    'root': str(host.project), 'root_identity': list(Workspace(host.project).identity),
                    'path': 'job.py', 'before': JOB, 'after': 'x\n', 'baseline': digest(JOB.encode())}}, host.cid)
        finally:
            host.release()
    finally:
        host.close()


DEAD = 'f' * 32  # the owner token of a service that is gone
SPAWNING, SPAWNED, TEARDOWN = {'record': 'spawning'}, {'record': 'spawned', 'anchor_pid': 4242}, {'record': 'teardown'}


def exited(reason, code, contained):
    return {'record': 'exited', 'exit_code': code, 'contained': contained, 'reason': reason,
            'output_bytes': 0, 'dropped_bytes': 0, 'peak_memory_bytes': 0}


@pytest.fixture
def no_signals(monkeypatch):
    sent = []
    monkeypatch.setattr(os, 'kill', lambda *args: sent.append(('kill', args)))
    monkeypatch.setattr(os, 'killpg', lambda *args: sent.append(('killpg', args)))
    return sent


def plant_supervised(host, rid, records, *, state='running', hold=60.0, plan=True):
    """A supervised job row, folder and records exactly as a service that died would leave them."""
    app = host.app
    staged = host.stage(rid)
    aid = staged['action_id']
    # Approval had begun (receipt running, broker action running) when the service died.
    call_id = app.runs.begin_approval(host.cid, aid)['call_id']
    app.store.query("UPDATE actions SET status='running' WHERE id=?", (aid,))
    process_id = uuid.uuid4().hex
    folder = app.processes._jobdir_for(process_id)
    app.processes._jobs_folder()
    folder.mkdir(mode=0o700)
    if plan:
        job_shim.write_plan(folder, {'version': 1, 'argv': ['/usr/bin/true'], 'env': {}, 'cwd': str(host.project),
                                     'profile': '', 'launcher': [], 'limits': {}, 'secret': uuid.uuid4().hex,
                                     'isolation': 'recorded supervisor isolation'})
    stamp = time.time()
    (folder / 'records.jsonl').write_text(''.join(json.dumps({**r, 'ts': stamp + i * 0.25}) + '\n'
                                                  for i, r in enumerate(records)))
    reservation = app.time_ledger.reserve(rid, hold, 'process', aid) if hold else None
    ws, now = Workspace(host.project), time.time()
    row = {'id': process_id, 'action_id': aid, 'chat_id': host.cid, 'run_id': rid, 'call_id': call_id,
           'root': str(ws.root), 'root_identity': json.dumps(list(ws.identity)), 'payload_hash': 'a' * 64,
           'command': 'python3 job.py', 'network': 0, 'writable': 0, 'owner': DEAD, 'state': state,
           'exit_code': None, 'isolation': None, 'outcome_uncertain': 0, 'pid': None, 'stop_requested': 0,
           'output_bytes': 0, 'output_dropped': 0, 'timeout': 60.0, 'reason': None, 'created': now,
           'started': now, 'finished': None, 'updated': now, 'jobdir': str(folder), 'plan_hash': None,
           'contained': None, 'settled_by': None, 'reservation_id': reservation}
    app.store.query(f'INSERT INTO process_jobs({",".join(row)}) VALUES({",".join("?" * len(row))})',
                    tuple(row.values()))
    return SimpleNamespace(id=process_id, action_id=aid, call_id=call_id, hold=reservation, jobdir=folder)


@pytest.mark.parametrize('records, state, contained, uncertain', [
    # The job killed its own anchor: the shim saw no sweep and recorded containment_lost.
    ([SPAWNING, SPAWNED, exited('containment_lost', None, False), TEARDOWN], 'unknown', 0, 1),
    # The shim itself died after spawning: no exit record at all.
    ([SPAWNING, SPAWNED], 'unknown', 0, 1),
    # A contained, natural exit is the only certain outcome.
    ([SPAWNING, SPAWNED, exited('command_exit', 0, True), TEARDOWN], 'succeeded', 1, 0),
], ids=['containment_lost', 'shim_killed', 'contained_exit'])
def test_a_restarted_service_settles_supervised_jobs_only_from_records_and_never_signals(
        host, no_signals, records, state, contained, uncertain):
    rid = host.root()
    planted = plant_supervised(host, rid, records)
    app = host.restart()
    row = host.row(planted.id)
    assert (row['state'], row['contained'], row['outcome_uncertain'], row['settled_by']) == \
        (state, contained, uncertain, 'supervisor_record'), dict(row)
    assert no_signals == []  # recovery never signals the recorded anchor PID or anything else
    root = str(Workspace(host.project).root)
    assert app.process_inspections.pending(root) is bool(uncertain)  # the gate is held exactly when uncertain
    hold = app.time_ledger.reservation(planted.hold)
    if uncertain:
        assert (hold['state'], hold['charged']) == ('uncertain', 60.0)  # nothing refunded
    else:
        assert hold['state'] == 'settled' and hold['charged'] < 60.0
    evidence = app.processes.evidence(planted.id)
    assert evidence['contained'] is (True if contained else (False if any(r['record'] == 'exited' for r in records)
                                                              else None))
    if uncertain:
        # A fresh asynchronous start in the gated project is refused before anything runs.
        new_rid = host.root()
        staged = host.stage(new_rid)
        with pytest.raises(PolicyError, match='unknown outcome|Inspect'):
            app.approve(host.cid, staged['action_id'])
        assert app.store.query('SELECT count(*) AS n FROM process_jobs')[0]['n'] == 1


# =====================================================================================
# 2. Typed command plans (command_plans is the real plan boundary)
# =====================================================================================

@macos
def test_plan_tampered_after_review_is_refused_per_field(tmp_path):
    project = tmp_path / 'p'
    project.mkdir()
    (project / 'job.py').write_text(JOB)
    (project / 'sub').mkdir()
    root = str(Workspace(project).root)
    reviewed = command_plans.from_argv([{'argv': ['python3', 'job.py'], 'env': {'TZ': 'UTC'}}], root,
                                       writable=[], network={'mode': 'none'})
    # Each change, resealed so its hash is self-consistent, is refused by narrow and named.
    step = lambda p, **change: {**p, 'steps': [{**p['steps'][0], **change}]}
    cases = {
        'steps[0].argv': lambda p: reseal(step(p, argv=['python3', 'other.py'])),
        'steps[0].env': lambda p: reseal(step(p, env={'TZ': 'UTC', 'CI': '1'})),
        'steps[0].cwd': lambda p: reseal(step(p, cwd='sub')),
        'writable': lambda p: reseal({**p, 'writable': ['.']}),
        'resources.timeout': lambda p: reseal({**p, 'resources': {**p['resources'], 'timeout': p['resources']['timeout'] + 1}}),
        'resources.nproc': lambda p: reseal({**p, 'resources': {**p['resources'], 'nproc': 512}}),
    }
    for field, mutate in cases.items():
        with pytest.raises(PolicyError, match=r'\(' + field.replace('[', r'\[').replace(']', r'\]').replace('.', r'\.') + r'\)'):
            command_plans.narrow(reviewed, mutate(reviewed))
    # Narrowing the same fields is admitted.
    narrower = reseal({**step(reviewed, env={}), 'resources': {**reviewed['resources'], 'timeout': 1}})
    assert command_plans.narrow(reviewed, narrower)['plan_hash'] == narrower['plan_hash']
    # network broadening none -> broker is refused as network.mode.
    broker = reseal({**reviewed, 'network': {'mode': 'broker', 'destinations': [['api.example.com', 443]],
                                             'credentials': []}, 'risk': 'network'})
    with pytest.raises(PolicyError, match=r'\(network\.mode\)'):
        command_plans.narrow(reviewed, broker)


@macos
def test_executable_drift_between_review_and_spawn_is_refused(tmp_path):
    project = tmp_path / 'p'
    project.mkdir()
    (project / 'job.py').write_text(JOB)
    root = str(Workspace(project).root)
    reviewed = command_plans.from_argv([{'argv': ['python3', 'job.py']}], root)
    # verify() re-pins the real interpreter; a recorded identity that no longer
    # matches the on-disk binary is drift, named per field.
    tampered = copy.deepcopy(reviewed)
    tampered['steps'][0]['executable']['sha256'] = '0' * 64
    tampered['plan_hash'] = command_plans.plan_digest(tampered)
    tampered = command_plans.validate(tampered)
    with pytest.raises(PolicyError, match=r'steps\[0\]\.executable'):
        command_plans.verify(tampered, root)
    # The unchanged reviewed plan verifies cleanly.
    assert command_plans.verify(reviewed, root)['plan_hash'] == reviewed['plan_hash']


@macos
def test_pipeline_is_not_supported_by_the_legacy_worker(tmp_path):
    project = tmp_path / 'p'
    project.mkdir()
    (project / 'job.py').write_text(JOB)
    (project / 'count.py').write_text("import sys; sys.stdin.read()\n")
    root = str(Workspace(project).root)
    pipeline = command_plans.from_argv([{'argv': ['python3', 'job.py']}, {'argv': ['python3', 'count.py']}],
                                       root, pipeline='linear')
    with pytest.raises(PolicyError, match='one-step plan'):
        command_plans.legacy_payload(pipeline)
    # The synchronous legacy worker refuses a pipeline plan outright.
    with pytest.raises(PolicyError, match='contained plan worker'):
        terminal.run_command({'command': 'python3 job.py', 'root': root,
                              'root_identity': list(Workspace(project).identity),
                              'argv': ['python3', 'job.py'], 'plan': pipeline,
                              'plan_hash': pipeline['plan_hash']}, threading.Event())


def test_parser_ambiguity_strings_are_refused(tmp_path):
    project = tmp_path / 'p'
    project.mkdir()
    root = str(Workspace(project).root)
    for command in ('cat "a b"', 'echo *.py', "echo 'x'", 'cat ~/x', 'echo a\\ b', 'ls [ab]', 'echo ${X}'):
        with pytest.raises(PolicyError):
            command_plans.from_command(command, root)


SUCCEEDED = {'state': 'succeeded', 'exit_code': 0, 'isolation': 'fixture worker', 'outcome_uncertain': False}


@pytest.fixture
def worked(tmp_path, monkeypatch):
    """The real App with a controlled worker at the async_terminal boundary (spawns nothing)."""
    worker = Fixed(SUCCEEDED)
    value = Host(tmp_path, monkeypatch, worker=worker)
    value.worker = worker
    yield value
    value.close()


def reseal(plan):
    plan = dict(plan)
    plan['plan_hash'] = command_plans.plan_digest(plan)
    return command_plans.validate(plan)


def refused_without_effect(host, rid, aid):
    app = host.app
    assert app.store.query('SELECT status FROM actions WHERE id=?', (aid,))[0]['status'] == 'failed'
    assert not app.store.query('SELECT 1 FROM process_jobs WHERE action_id=?', (aid,))
    assert host.worker.calls == []
    assert not [t for t in app.runs.tools(rid) if t['state'] == 'uncertain']  # a refusal is never uncertain
    assert not app.store.query("SELECT 1 FROM run_time_reservations WHERE run_id=? AND state='open'", (rid,))


@macos
def test_a_stored_plan_edited_after_review_is_refused_at_approval(worked):
    host, app = worked, worked.app
    rid = host.root()
    aid = host.stage(rid)['action_id']
    payload = next(a for a in app.store.actions(host.cid) if a['id'] == aid)['payload']
    assert payload['planned'] is True and payload['plan']['plan_hash'] == payload['plan_hash']
    edited = copy.deepcopy(payload)
    edited['plan']['resources']['memory_mb'] = 16384  # widened in the store without resealing
    app.store.query('UPDATE actions SET payload=? WHERE id=?', (json.dumps(edited), aid))
    with pytest.raises(PolicyError, match='hash'):
        app.approve(host.cid, aid)
    refused_without_effect(host, rid, aid)


WIDENINGS = {
    'steps[0].argv': lambda p: reseal({**p, 'steps': [{**p['steps'][0], 'argv': ['python3', 'other.py']}]}),
    'steps[0].env': lambda p: reseal({**p, 'steps': [{**p['steps'][0], 'env': {'CI': '1'}}]}),
    'writable': lambda p: reseal({**p, 'writable': ['.']}),
    'network.mode': lambda p: reseal({**p, 'network': {'mode': 'broker', 'destinations': [['api.example.com', 443]],
                                                       'credentials': []}, 'risk': 'network'}),
    'resources.timeout': lambda p: reseal({**p, 'resources': {**p['resources'], 'timeout': 300}}),
    'resources.memory_mb': lambda p: reseal({**p, 'resources': {**p['resources'], 'memory_mb': 16384}}),
}


@macos
@pytest.mark.parametrize('field', sorted(WIDENINGS))
def test_a_requested_plan_wider_than_the_review_is_refused_per_field_at_admission(worked, monkeypatch, field):
    host, app = worked, worked.app
    app.setting('permissions', {'mode': 'ask', 'tools': {**DEFAULT['tools'], 'edits': False}})  # reviewed writable []
    rid = host.root()
    aid = host.stage(rid, timeout=30)['action_id']
    real_start = app.processes.start

    def start(action, cid, payload, *, timeout):
        widened = WIDENINGS[field](payload['plan'])
        return real_start(action, cid, {**payload, 'plan': widened, 'plan_hash': widened['plan_hash']},
                          timeout=timeout)
    monkeypatch.setattr(app.processes, 'start', start)
    pattern = r'\(' + field.replace('[', r'\[').replace(']', r'\]').replace('.', r'\.') + r'\)'
    with pytest.raises(PolicyError, match=pattern):
        app.approve(host.cid, aid)
    refused_without_effect(host, rid, aid)


@macos
def test_executable_drift_after_review_is_refused_at_spawn(worked):
    host, app = worked, worked.app
    rid = host.root()
    aid = host.stage(rid)['action_id']
    payload = next(a for a in app.store.actions(host.cid) if a['id'] == aid)['payload']
    # The reviewed pin no longer matches the interpreter on disk (as if it were replaced).
    drifted = copy.deepcopy(payload['plan'])
    drifted['steps'][0]['executable']['sha256'] = '0' * 64
    drifted = reseal(drifted)
    app.store.query('UPDATE actions SET payload=? WHERE id=?',
                    (json.dumps({**payload, 'plan': drifted, 'plan_hash': drifted['plan_hash']}), aid))
    with pytest.raises(PolicyError, match=r'steps\[0\]\.executable: sha256'):
        app.approve(host.cid, aid)
    refused_without_effect(host, rid, aid)


@macos
def test_ambiguous_command_strings_are_never_planned_at_proposal(worked):
    host, app = worked, worked.app
    rid = host.root()
    planned = host.stage(rid, command='python3 job.py')['action_id']
    unplanned = host.stage(rid, command='python3 "job.py"')['action_id']
    actions = {a['id']: a['payload'] for a in app.store.actions(host.cid)}
    assert actions[planned]['planned'] is True and 'plan' in actions[planned]
    # X03: a quoted string cannot be a plan; the App labels it unplanned and still
    # requires native review (a dangerous-class command), never auto-running it.
    assert actions[unplanned]['planned'] is False and 'plan' not in actions[unplanned]
    assert actions[unplanned]['requires_review'] is True
    assert host.worker.calls == []


# =====================================================================================
# 3. Credentials (real egress + credential brokers)
# =====================================================================================

SECRET = 'phase14-secret-' + uuid.uuid4().hex


@pytest.fixture(scope='module')
def tls(tmp_path_factory):
    if not MAC:
        pytest.skip('openssl TLS fixtures use /usr/bin/openssl on macOS.')
    folder = tmp_path_factory.mktemp('tls').resolve()
    (folder / 'ca.cnf').write_text('[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=Pebble P14 CA\n'
                                   '[v3_ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n'
                                   'subjectKeyIdentifier=hash\n')
    (folder / 'leaf.cnf').write_text('[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=localhost\n'
                                     '[v3_leaf]\nbasicConstraints=critical,CA:FALSE\n'
                                     'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n'
                                     'subjectAltName=DNS:localhost,IP:127.0.0.1\nsubjectKeyIdentifier=hash\n'
                                     'authorityKeyIdentifier=keyid\n')
    openssl = '/usr/bin/openssl'
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(folder), 'RANDFILE': str(folder / '.rnd')}
    for argv in ([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '2', '-keyout', 'ca.key',
                  '-out', 'ca.pem', '-config', 'ca.cnf', '-extensions', 'v3_ca'],
                 [openssl, 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'leaf.key', '-out', 'leaf.csr',
                  '-config', 'leaf.cnf'],
                 [openssl, 'x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-set_serial', '7',
                  '-days', '2', '-sha256', '-extfile', 'leaf.cnf', '-extensions', 'v3_leaf', '-out', 'leaf.pem']):
        result = subprocess.run(argv, cwd=folder, capture_output=True, text=True, timeout=60, env=env)
        assert result.returncode == 0, result.stderr
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / 'leaf.pem', folder / 'leaf.key')
    return SimpleNamespace(ca=str(folder / 'ca.pem'), context=context)


class HTTPSUpstream:
    def __init__(self, context, handlers):
        self.context, self.handlers, self.requests = context, dict(handlers), []
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(16)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(10)
        try:
            secure = self.context.wrap_socket(conn, server_side=True)
        except OSError:
            conn.close()
            return
        try:
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = secure.recv(65536)
                if not chunk:
                    return
                data += chunk
            head = data.split(b'\r\n\r\n', 1)[0].decode('latin-1')
            method, target, _ = head.split('\r\n')[0].split(' ')
            headers = [(k.strip(), v.strip()) for k, v in (ln.split(':', 1) for ln in head.split('\r\n')[1:] if ':' in ln)]
            request = {'method': method, 'target': target, 'headers': headers}
            self.requests.append(request)
            for piece in self.handlers.get(target.split('?', 1)[0], lambda r: [b'HTTP/1.1 404 x\r\nContent-Length: 0\r\n\r\n'])(request):
                if self.stop.is_set():
                    break
                secure.sendall(piece)
        except OSError:
            pass
        finally:
            secure.close()

    def close(self):
        self.stop.set()
        self.sock.close()


@pytest.fixture
def brokers():
    made = []

    def make(*args, **kwargs):
        made.append(EgressBroker(*args, **kwargs).start())
        return made[-1]
    yield make
    for broker in made:
        broker.close()


def route_spec(upstream, grant, methods=('GET',), prefix='/v1'):
    return {'api': {'origin': f'https://localhost:{upstream.port}', 'methods': list(methods), 'path_prefix': prefix,
                    'grant': grant}}


def ask(broker, path, timeout=10):
    sock = socket.create_connection(('127.0.0.1', broker.port), timeout=timeout)
    sock.sendall(f'GET {path} HTTP/1.1\r\nHost: broker\r\n\r\n'.encode())
    data = b''
    while True:
        try:
            chunk = sock.recv(65536)
        except OSError:
            break
        if not chunk:
            break
        data += chunk
    sock.close()
    head, _, body = data.partition(b'\r\n\r\n')
    headers = {}
    for line in head.decode('latin-1').split('\r\n')[1:]:
        key, _, value = line.partition(':')
        headers[key.strip().lower()] = value.strip()
    return int(head.split(b' ')[1]), headers, body, data


def reply(status=200, body=b'', headers=()):
    head = f'HTTP/1.1 {status} x\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in headers)
    return [head.encode() + f'Content-Length: {len(body)}\r\n\r\n'.encode() + body]


def settled(broker):
    assert wait_for(lambda: broker.log() and broker.log()[-1]['ended'] is not None, 5)
    return broker.log()[-1]


@macos
def test_the_credential_is_never_in_the_plan_profile_or_legacy_payload(tmp_path):
    """A broker credential reference carries only the vault ref, never the secret,
    through every representation a job could read."""
    project = tmp_path / 'p'
    project.mkdir()
    (project / 'job.py').write_text(JOB)
    root = str(Workspace(project).root)
    plan = command_plans.from_argv([{'argv': ['python3', 'job.py']}], root, network={
        'mode': 'broker', 'destinations': [['api.example.com', 443]],
        'credentials': [{'ref': 'vault:api-token', 'destination': ['api.example.com', 443],
                         'methods': ['GET'], 'path_prefix': '/v1/'}]})
    (tmp_path / 'scratch-home').mkdir()
    inputs = command_plans.profile_inputs(plan, str(tmp_path / 'scratch-home'), uuid.uuid4().hex)
    blobs = [json.dumps(plan), json.dumps(command_plans.review_view(plan)), json.dumps(inputs),
             json.dumps(command_plans.legacy_payload(plan))]
    assert not any(SECRET in blob for blob in blobs)
    # No credential value is representable in the plan at all: a grant is a ref plus scope.
    assert set(plan['network']['credentials'][0]) == {'ref', 'destination', 'methods', 'path_prefix'}
    plan_with_value = copy.deepcopy(plan)
    plan_with_value['network']['credentials'][0]['value'] = SECRET
    with pytest.raises(PolicyError):
        command_plans.validate(plan_with_value)
    # The profile's network section is the broker port only, never an open network grant.
    assert inputs['network'] == 'broker'


@macos
def test_a_backend_secret_never_reaches_the_job_its_records_or_any_model_visible_result(confined_host, monkeypatch):
    """F2: the backend's own environment holds a secret (as a shell launch would leave it).
    The supervised job's environment and argv, its plan, records and spool, the
    database, emitted events and the model's ReadProcess result never contain it."""
    host, app = confined_host, confined_host.app
    monkeypatch.setenv('OPENAI_API_KEY', SECRET)
    monkeypatch.setenv('PEBBLE_FIXTURE_TOKEN', SECRET)
    rid = host.root()
    _, admitted = host.admit(rid, command='python3 env.py', timeout=30)
    pid = admitted['process_id']
    row = host.settled(pid)
    assert (row['state'], row['exit_code']) == ('succeeded', 0), dict(row)
    raw = b''.join(bytes(r['data']) for r in app.store.query(
        'SELECT data FROM process_output WHERE process_id=? ORDER BY sequence', (pid,))).decode('utf-8', 'replace')
    assert 'HOME=' in raw and 'argv=' in raw, raw[:400]  # the probe ran and printed its environment
    names = {line.split('=', 1)[0] for line in raw.splitlines() if '=' in line and not line.startswith('argv=')}
    assert not names & {'OPENAI_API_KEY', 'PEBBLE_FIXTURE_TOKEN'} and SECRET not in raw
    jobdir = Path(row['jobdir'])
    for name in ('plan.json', 'records.jsonl', 'output.spool'):
        assert SECRET.encode() not in (jobdir / name).read_bytes(), name
    plan = json.loads((jobdir / 'plan.json').read_text())
    assert not {'OPENAI_API_KEY', 'PEBBLE_FIXTURE_TOKEN'} & (set(plan['env']) | set(plan['command_env']))
    with app.store.lock:
        dump = '\n'.join(app.store.db.iterdump())
    assert SECRET not in dump and SECRET not in json.dumps(host.events, default=str)
    host.own(rid)
    try:
        page = app.execute('ReadProcess', {'process_id': pid}, Workspace(host.project), host.cid,
                           call_id='2:' + uuid.uuid4().hex[:12])
    finally:
        host.release()
    assert 'HOME=' in page['untrusted_process_output'] and SECRET not in json.dumps(page)


@macos
def test_upstream_echo_of_the_credential_is_stopped(brokers, tls):
    upstream = HTTPSUpstream(tls.context, {})

    def echo_head(request):
        token = [v for k, v in request['headers'] if k.lower() == 'authorization'][0]
        return reply(200, b'ok', [('X-Echo', token)])
    upstream.handlers['/v1/echo'] = echo_head
    credentials = CredentialBroker(lambda ref: SECRET)
    grant = credentials.grant('vault:api', f'https://localhost:{upstream.port}', ['GET'], '/v1', time.time() + 60)
    broker = brokers(None, routes=route_spec(upstream, grant), credentials=credentials, loopback_targets=True,
                     ca_file=tls.ca)
    try:
        code, headers, _, raw = ask(broker, '/r/api/v1/echo')
        assert (code, headers.get('x-pebble-egress')) == (502, 'credential_reflected')
        assert SECRET.encode() not in raw
        assert SECRET not in (json.dumps(broker.log()) + json.dumps(credentials.log()) + repr(credentials))
    finally:
        upstream.close()
        credentials.close()


@macos
def test_cross_origin_redirect_is_refused(brokers, tls):
    upstream = HTTPSUpstream(tls.context, {'/v1/go': lambda r: reply(302, b'', [('Location', 'https://evil.invalid/x')])})
    credentials = CredentialBroker(lambda ref: SECRET)
    grant = credentials.grant('vault:api', f'https://localhost:{upstream.port}', ['GET'], '/v1', time.time() + 60)
    broker = brokers(None, routes=route_spec(upstream, grant), credentials=credentials, loopback_targets=True,
                     ca_file=tls.ca)
    try:
        code, headers, _, _ = ask(broker, '/r/api/v1/go')
        assert (code, headers.get('x-pebble-egress')) == (502, 'cross_origin_redirect')
        assert 'location' not in headers
    finally:
        upstream.close()
        credentials.close()


@macos
def test_revocation_mid_run_refuses_the_next_request(brokers, tls):
    upstream = HTTPSUpstream(tls.context, {'/v1/ok': lambda r: reply(200, b'fine')})
    credentials = CredentialBroker(lambda ref: SECRET)
    grant = credentials.grant('vault:api', f'https://localhost:{upstream.port}', ['GET'], '/v1', time.time() + 60)
    broker = brokers(None, routes=route_spec(upstream, grant), credentials=credentials, loopback_targets=True,
                     ca_file=tls.ca)
    try:
        assert ask(broker, '/r/api/v1/ok')[::2][:2] == (200, b'fine')
        assert credentials.revoke(grant) is True
        code, headers, _, _ = ask(broker, '/r/api/v1/ok')
        assert (code, headers.get('x-pebble-egress')) == (403, 'revoked')
        assert len(upstream.requests) == 1  # the refused request never left the broker
    finally:
        upstream.close()
        credentials.close()


# =====================================================================================
# 4. Active-time ledger (real store; injected clock)
# =====================================================================================

class Clock:
    def __init__(self, mono=1000.0, wall=5000.0):
        self.mono, self.wall = mono, wall

    def advance(self, seconds, *, sleep=False):
        self.mono += seconds
        self.wall += seconds + (seconds if sleep else 0)


def ledger_store(tmp_path, budget=600.0):
    from pebble.runtime import RunJournal
    from pebble.store import Store
    store = Store(tmp_path / 'data')
    runs = RunJournal(store, tmp_path / 'data')
    cid = store.new_chat(str(tmp_path))
    rid = runs.create(cid, 'ledger', {'workspace': str(tmp_path), 'limits': {'max_steps': 8, 'max_seconds': budget}})
    return store, runs, rid


def test_a_crash_loop_cannot_reduce_consumed_time(tmp_path):
    store, runs, rid = ledger_store(tmp_path)
    clock = Clock()
    boot = uuid.uuid4().hex
    consumed = 0.0
    for i in range(12):
        ledger = TimeLedger(store, boot_uuid=boot, clock=lambda: clock.mono, wall=lambda: clock.wall)
        ledger.recover()  # closes the prior dead instance's open slice, fully charged
        ledger.begin(rid)
        clock.advance(4.0)
        now = ledger.consumed(rid)
        assert now >= consumed, (i, now, consumed)  # never decreases across crashes
        consumed = now
    assert consumed >= 12 * 4.0  # at least the observed active time
    store.close()


def test_a_sleep_gap_is_not_charged(tmp_path):
    store, runs, rid = ledger_store(tmp_path)
    clock = Clock()
    ledger = TimeLedger(store, boot_uuid=uuid.uuid4().hex, clock=lambda: clock.mono, wall=lambda: clock.wall)
    ledger.begin(rid)
    before = ledger.consumed(rid)
    clock.advance(100.0, sleep=True)  # wall jumps 200, monotonic 100: a 100 s sleep
    gap = ledger.observe_sleep(rid)
    assert gap >= 90.0
    # The recorded sleep row is charged 0; consumed grows only by monotonic time.
    assert ledger.consumed(rid) - before < 1.0
    store.close()


def test_a_reservation_is_not_refunded_on_an_unknown_outcome(tmp_path):
    store, runs, rid = ledger_store(tmp_path)
    ledger = TimeLedger(store, boot_uuid=uuid.uuid4().hex)
    ledger.begin(rid)
    hold = ledger.reserve(rid, 120.0, 'process', 'action-1', narrow=True)
    consumed_before = ledger.consumed(rid)
    # An unknown outcome settles with no refund: the full hold stays charged.
    result = ledger.settle_reservation(hold, None, False)
    assert result['refund'] == 0.0 and result['state'] == 'uncertain'
    assert ledger.consumed(rid) - consumed_before >= 120.0
    store.close()


def test_resume_refuses_when_max_elapsed_or_consumed_is_exhausted(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    try:
        app = host.app
        chat = app.chat(host.cid)
        # A run whose ledger consumed already meets the budget cannot resume.
        config = app.runtime_config(chat, False, {'max_steps': 8, 'max_seconds': 60})
        rid = app.runs.create(host.cid, GOAL, config)
        app.runs.transition(rid, 'waiting_input')
        app.time_ledger.begin(rid)
        hold = app.time_ledger.reserve(rid, 60.0, 'process', 'a1', narrow=True)
        app.time_ledger.settle_reservation(hold, None, False)  # charges the full 60 s, uncertain
        app.time_ledger.end(rid, certain=False)
        assert app.time_ledger.consumed(rid) >= 60.0
        with pytest.raises(ValueError, match='exhausted its budget'):
            app.run_resume(host.cid, rid)
        # The elapsed mirror alone also exhausts the budget.
        other = app.runs.create(host.cid, GOAL, config)
        app.store.query('UPDATE runs SET state=?, elapsed=? WHERE id=?', ('waiting_input', 120.0, other))
        with pytest.raises(ValueError, match='exhausted its budget'):
            app.run_resume(host.cid, other)
    finally:
        host.close()


# =====================================================================================
# 5. Authorization (tool_authz layers at their real boundaries)
# =====================================================================================

def run_config_for(host, rid):
    return host.app.runs.get(rid)['config']


def test_each_layer_refuses_independently(host):
    app = host.app
    rid = host.root()
    config = run_config_for(host, rid)
    version = app.policy_version()
    base = dict(tool='Read', args={'file_path': 'job.py'}, actor=Actor.root(rid), run_config=config,
                catalogue_digest=app.tool_catalogue_digest, policy_version=version,
                policy=app.store.setting('permissions', DEFAULT), route=app.plugin_route(rid))
    # Baseline: a well-formed call is allowed.
    assert authz_check(**base).allowed
    # catalogue: a digest that no longer matches the pin.
    d = authz_check(**{**base, 'catalogue_digest': '0' * 64})
    assert not d.allowed and d.refusal_layer == 'catalogue'
    # schema: a string past maxLength (SearchFiles.query maxLength 256).
    d = authz_check(**{**base, 'tool': 'SearchFiles', 'args': {'query': 'x' * 257}})
    assert not d.allowed and d.refusal_layer == 'schema' and d.field == 'query'
    # actor: a child calling a root-only tool it was never admitted.
    d = authz_check(**{**base, 'tool': 'StartCommand', 'args': {'command': 'python3 job.py'},
                       'actor': Actor.child(rid, 'a' * 32, 1), 'admitted': lambda a: set()})
    assert not d.allowed and d.refusal_layer == 'actor'
    # route: a plugin call whose route was never reviewed or loaded in this run.
    d = authz_check(**{**base, 'tool': 'PluginCall', 'args': {'plugin_id': 'p' * 32, 'tool': 'x', 'digest': 'd' * 64,
                                                              'arguments': '{}'}})
    assert not d.allowed and d.refusal_layer == 'route' and len(d.refusals) == 1
    # policy: files narrowed in the current permissions, for a files tool.
    narrowed = {'mode': 'ask', 'tools': {**DEFAULT['tools'], 'files': False}}
    d = authz_check(**{**base, 'tool': 'Read', 'args': {'file_path': 'job.py'}, 'policy': narrowed})
    assert not d.allowed and d.refusal_layer == 'policy' and len(d.refusals) == 1


def events_of(app, rid, kind):
    return [json.loads(r['payload']) for r in app.store.query(
        'SELECT payload FROM run_events WHERE run_id=? AND kind=? ORDER BY sequence', (rid, kind))]


def test_a_catalogue_change_mid_run_is_refused(host, monkeypatch):
    app = host.app
    rid = host.root()
    read = next(t for t in TOOLS if t['function']['name'] == 'Read')
    # (a) A schema mutated in place while the run is live: App.execute refuses at the
    # catalogue layer, records a completed error receipt (never uncertain) and reads nothing.
    monkeypatch.setitem(read['function'], 'description', read['function']['description'] + ' (changed)')
    host.own(rid)
    try:
        with pytest.raises(PolicyError, match='canonical tool catalogue'):
            app.execute('Read', {'file_path': 'job.py'}, Workspace(host.project), host.cid, call_id='1:mutated')
    finally:
        host.release()
    receipt = next(t for t in app.runs.tools(rid) if t['call_id'] == '1:mutated')
    assert receipt['state'] == 'completed' and 'error' in receipt['result']
    refused = events_of(app, rid, 'tool_refused')
    assert refused and refused[-1]['refusal_layer'] == 'catalogue' and refused[-1]['allowed'] is False
    # (b) A restarted service computes a new startup digest; the run's old pin refuses.
    monkeypatch.setattr(app, 'tool_catalogue_digest', catalogue_digest(canonical_catalogue()))
    host.own(rid)
    try:
        with pytest.raises(PolicyError, match='re-pinned'):
            app.authorize('Read', {'file_path': 'job.py'}, Actor.root(rid), rid)
    finally:
        host.release()
    # Resume refuses too: the current configuration pin no longer matches the run's.
    app.runs.transition(rid, 'waiting_input')
    with pytest.raises(ValueError, match='changed'):
        app.run_resume(host.cid, rid)


def test_broadening_is_refused_while_narrowing_applies(host):
    app = host.app
    rid = host.root()  # pinned at the default (files on), ask mode, version v0
    config = run_config_for(host, rid)
    digest_now = app.tool_catalogue_digest
    # Narrowing (files revoked) applies immediately to a files tool.
    narrowed = {'mode': 'ask', 'tools': {**DEFAULT['tools'], 'files': False}}
    d = authz_check(tool='Read', args={'file_path': 'x'}, actor=Actor.root(rid), run_config=config,
                    catalogue_digest=digest_now, policy_version=app.policy_version() + 1, policy=narrowed)
    assert not d.allowed and d.refusal_layer == 'policy' and 'narrowed' in d.reason
    # Broadening (a tool pinned off, now on) does NOT take effect without a re-pin.
    pinned_off = {'mode': 'ask', 'tools': {**DEFAULT['tools'], 'web': False}}
    off_config = {**config, 'permissions': pinned_off,
                  'authz': authz_pin(catalogue_digest=digest_now, policy_version=app.policy_version())}
    broadened = {'mode': 'ask', 'tools': {**DEFAULT['tools'], 'web': True}}
    d = authz_check(tool='WebSearch', args={'query': 'x'}, actor=Actor.root(rid), run_config=off_config,
                    catalogue_digest=digest_now, policy_version=app.policy_version() + 1, policy=broadened)
    assert not d.allowed and d.refusal_layer == 'policy' and 'after this run started' in d.reason


def test_a_legacy_run_without_a_pin_is_refused(host):
    app = host.app
    legacy = {'permissions': app.store.setting('permissions', DEFAULT)}  # no 'authz' pin
    d = authz_check(tool='Read', args={'file_path': 'x'}, actor=Actor.root('a' * 32), run_config=legacy,
                    catalogue_digest=app.tool_catalogue_digest, policy_version=app.policy_version(),
                    policy=app.store.setting('permissions', DEFAULT))
    assert not d.allowed and d.refusal_layer in ('catalogue', 'policy')
    assert any('no pinned' in r for _, r, _ in d.refusals)


def test_a_child_calling_root_or_native_tools_is_refused(host):
    app = host.app
    rid = host.root()
    config = run_config_for(host, rid)
    base = dict(run_config=config, catalogue_digest=app.tool_catalogue_digest, policy_version=app.policy_version(),
                policy=app.store.setting('permissions', DEFAULT), admitted=lambda a: {'Read', 'StartCommand', 'approve'})
    child = Actor.child(rid, 'c' * 32, 1)
    # Even a corrupted admitted set that lists them cannot admit root-only or native routes.
    for tool, args in (('StartCommand', {'command': 'python3 job.py'}), ('approve', {})):
        d = authz_check(tool=tool, args=args, actor=child, **base)
        assert not d.allowed and 'actor' in [layer for layer, _, _ in d.refusals], (tool, d.refusals)
    # The same child may call an ordinary admitted read.
    assert authz_check(tool='Read', args={'file_path': 'job.py'}, actor=child, **base).allowed


class ScriptedChild:
    """A child provider at the PinnedProvider boundary; replays fixed turns."""

    def __init__(self, replies):
        self.replies, self.calls, self.offered = list(replies), 0, []

    def complete(self, messages, tools, *rest):
        self.offered.append(sorted(t['function']['name'] for t in tools))
        if self.calls >= len(self.replies):
            raise ValueError('Fixture: the child asked for more turns than scripted.')
        self.calls += 1
        return copy.deepcopy(self.replies[self.calls - 1])

    last_usage = {}

    def cancel(self):
        pass

    def close(self):
        pass


def tool_call(ident, name, args):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


@pytest.mark.parametrize('name, args, layer', [
    ('Read', {'file_path': 'job.py\x00.txt'}, 'schema'),   # NUL passes App.validate, not the strict schema
    ('StartCommand', {'command': 'python3 job.py'}, None),  # never offered: App.validate refuses first
])
def test_child_admission_refuses_at_the_real_child_loop(host, name, args, layer):
    app = host.app
    rid = host.root()
    node = app.agents.tree.spawn(host.cid, rid, None, 'Inspect job.py.', 'reader', ['Read'], {})
    app.agents.tree.claim(host.cid, rid, node['id'], 1, 'worker-a')
    provider = ScriptedChild([tool_call('c1', name, args), {'role': 'assistant', 'content': 'unreachable'}])
    app.agents.provider_factory = lambda *a, **k: provider
    app.agents.drive({'cid': host.cid, 'rid': rid, 'id': node['id'], 'generation': 1, 'owner': 'worker-a',
                      'cancel': threading.Event(), 'suspend': False})
    assert provider.offered and not any('StartCommand' in offered for offered in provider.offered)
    status = app.store.query('SELECT status FROM agent_nodes WHERE id=?', (node['id'],))[0]['status']
    assert status == 'failed' and provider.calls == 1  # the generation stopped at the refused call
    assert not app.store.actions(host.cid)
    assert not app.store.query('SELECT 1 FROM process_jobs WHERE run_id=?', (rid,))
    refused = [e for e in events_of(app, rid, 'tool_refused') if (e.get('actor') or {}).get('kind') == 'child']
    if layer:
        assert refused and refused[-1]['refusal_layer'] == layer, refused
    else:
        assert not refused  # refused before authorization ran; nothing was journaled as authorized
    assert not [e for e in events_of(app, rid, 'tool_authorized')
                if (e.get('actor') or {}).get('kind') == 'child' and e.get('tool') == name]


def test_the_real_runner_refuses_a_strict_schema_violation_before_execute(host, monkeypatch):
    """F5: App.validate admits a NUL-bearing path; the runner's strict-schema layer
    refuses it before App.execute, so no tool receipt or file read exists."""
    app = host.app
    requests = []

    def complete(messages, tools, cancel, token, effort):
        requests.append((copy.deepcopy(messages), sorted(t['function']['name'] for t in tools)))
        if len(requests) == 1:
            assert 'Read' in requests[0][1]
            return tool_call('nul', 'Read', {'file_path': 'job.py\x00.txt'})
        return {'role': 'assistant', 'content': 'The path was refused; nothing was read.'}
    monkeypatch.setattr(app.model, 'complete', complete)
    app.work(app.chat(host.cid), 'Read job.py.', False, [])
    run = app.runs.list(host.cid)[0]
    assert len(requests) == 2
    refused = events_of(app, run['id'], 'tool_refused')
    assert refused and (refused[-1]['refusal_layer'], refused[-1]['field']) == ('schema', 'file_path')
    assert refused[-1]['actor']['kind'] == 'root'
    # The runner journals the planned call first; execute never began it, so it has no result.
    [read] = [t for t in app.runs.tools(run['id']) if t['name'] == 'Read']
    assert (read['state'], read['result']) == ('planned', None), read
    tool_messages = [m for m in requests[1][0] if m.get('role') == 'tool']
    assert tool_messages and 'must not contain NUL' in tool_messages[-1]['content']


def test_the_runner_and_execute_boundaries_enforce_authorization(host):
    app = host.app
    rid = host.root()
    # Runner schema boundary: App.authorize(layers=('schema',)) refuses an over-long string.
    with pytest.raises(PolicyError, match='maxLength'):
        app.authorize('SearchFiles', {'query': 'x' * 257}, Actor.root(rid), rid, layers=('schema',))
    # execute boundary: a native-only route name is refused for a model/root caller.
    with pytest.raises(PolicyError):
        app.authorize('approve', {}, Actor.root(rid), rid)


# =====================================================================================
# 6. Output sanitizer (terminal_text) on the model/UI projection
# =====================================================================================

HOSTILE = (b'before'
           b'\x1b]52;c;ZXZpbA==\x07'       # OSC 52 clipboard write
           b'\x1b]8;;https://evil.invalid\x07link\x1b]8;;\x07'  # OSC 8 hyperlink
           b'\x1b[8mhidden\x1b[0m'         # CSI conceal
           b'\xe2\x80\xaegnikcart'          # U+202E RLO bidi
           b'\xc2\x9bX'                      # 8-bit C1 CSI
           b'after\n')


def test_hostile_terminal_output_is_projected_without_raw_control_sequences():
    out = terminal_text.project(HOSTILE, final=True)
    text = out['text']
    assert '\x1b' not in text and '\x9b' not in text
    assert 'ZXZpbA' not in text and 'evil.invalid' not in text  # OSC payloads removed
    assert '‮' not in text  # bidi control replaced by a visible marker
    assert out['controls_removed'] > 0 and out['bidi_marked'] >= 1
    assert 'before' in text and 'after' in text  # printable text survives


def seed_process(host, data, state='succeeded'):
    rid = host.root()
    pid = uuid.uuid4().hex
    now = time.time()
    ws = Workspace(host.project)
    host.app.store.query(
        'INSERT INTO process_jobs(id,action_id,chat_id,run_id,call_id,root,root_identity,payload_hash,command,network,'
        "writable,owner,state,outcome_uncertain,stop_requested,output_bytes,output_dropped,timeout,created,updated) "
        "VALUES(?,?,?,?,?,?,?,'h','c',0,0,'o',?,0,0,?,0,30,?,?)",
        (pid, uuid.uuid4().hex, host.cid, rid, '1:' + pid[:12], str(ws.root), json.dumps(list(ws.identity)),
         state, len(data), now, now))
    host.app.store.query('INSERT INTO process_output VALUES(?,?,?,?)', (pid, 0, 0, data))
    return rid, pid


def test_process_pages_sanitize_the_model_projection_but_keep_raw_bytes(host):
    rid, pid = seed_process(host, HOSTILE)
    page = host.app.process_read(host.cid, rid, pid, after=0, limit=8000)
    shown = page['untrusted_process_output']
    assert '\x1b' not in shown and '\x9b' not in shown and '‮' not in shown
    assert 'evil.invalid' not in shown and page['controls_removed'] > 0
    assert page['output_projection'] == 'terminal_text/1'
    # The raw bytes are preserved verbatim in storage.
    raw = b''.join(bytes(r['data']) for r in host.app.store.query(
        'SELECT data FROM process_output WHERE process_id=? ORDER BY sequence', (pid,)))
    assert raw == HOSTILE


# =====================================================================================
# 7. Readback (native-only; clean verdicts only; no replay)
# =====================================================================================

def seed_uncertain_edit(host, *, after='AFTER\n', missing=True, root=None):
    """An owned, applied edit action whose run tool is uncertain and whose run
    needs reconciliation, as a crashed approval would leave it."""
    app = host.app
    rid = host.root()
    root = root or str(Workspace(host.project).root)
    identity = list(Workspace(host.project).identity)
    path = 'created.txt'
    payload = {'root': root, 'root_identity': identity, 'path': path, 'before': '', 'after': after,
               'baseline': None}
    aid = app.store.action(host.cid, 'edit', payload)
    call_id = '1:' + uuid.uuid4().hex[:12]
    app.runs.begin_tool(rid, call_id, 'Write', {'file_path': path})
    app.runs.bind_action(rid, call_id, aid, 'edit')
    app.store.query("UPDATE actions SET status='applied' WHERE id=?", (aid,))
    app.runs.end_tool(rid, call_id, {'action_id': aid, 'error': 'uncertain'}, uncertain=True)
    app.store.query("UPDATE runs SET state='needs_reconciliation' WHERE id=?", (rid,))
    if not missing:
        (host.project / path).write_text(after)
    return rid, call_id, aid, path


def test_run_reconcile_readback_settles_only_a_clean_native_verdict(host):
    app = host.app
    rid, call_id, aid, path = seed_uncertain_edit(host, after='AFTER\n', missing=False)
    # A clean 'occurred' readback (the after-state is present) settles the tool.
    result = app.run_reconcile(host.cid, rid, call_id, 'readback', 'inspected')
    assert result['status'] == 'readback_verified' and result['effect'] == 'occurred'
    tool = next(t for t in app.runs.tools(rid) if t['call_id'] == call_id)
    assert tool['state'] == 'completed'
    # Nothing was replayed: the file is unchanged and the action stays applied.
    assert (host.project / path).read_text() == 'AFTER\n'
    assert app.store.query('SELECT status FROM actions WHERE id=?', (aid,))[0]['status'] == 'applied'


def test_run_reconcile_readback_refuses_diverged_and_leaves_it_uncertain(host):
    app = host.app
    rid, call_id, aid, path = seed_uncertain_edit(host, after='AFTER\n', missing=False)
    (host.project / path).write_text('SOMETHING ELSE\n')  # neither before nor after: diverged
    with pytest.raises(ValueError, match='clean occurred or not_occurred'):
        app.run_reconcile(host.cid, rid, call_id, 'readback', 'inspected')
    tool = next(t for t in app.runs.tools(rid) if t['call_id'] == call_id)
    assert tool['state'] == 'uncertain'


def test_run_reconcile_readback_refuses_a_call_with_no_owned_action(host):
    app = host.app
    rid, call_id, aid, path = seed_uncertain_edit(host, missing=False)
    with pytest.raises(ValueError, match='owned broker action'):
        app.run_reconcile(host.cid, rid, 'not-a-real-call', 'readback', 'inspected')


def test_run_reconcile_readback_refuses_a_call_id_from_another_run(host):
    app = host.app
    rid_a, call_a, aid_a, _ = seed_uncertain_edit(host, missing=False)
    rid_b, call_b, aid_b, _ = seed_uncertain_edit(host, missing=False)
    # call_b is owned under rid_b, not rid_a: the owner lookup is run-scoped.
    with pytest.raises(ValueError, match='owned broker action'):
        app.run_reconcile(host.cid, rid_a, call_b, 'readback', 'inspected')


def test_run_readback_is_read_only_and_native(host):
    app = host.app
    rid, call_id, aid, path = seed_uncertain_edit(host, missing=False)
    app.store.query("UPDATE runs SET state='paused' WHERE id=?", (rid,))  # run_readback requires an idle service
    before = app.store.query('SELECT status,payload FROM actions WHERE id=?', (aid,))
    result = app.run_readback(host.cid, rid)
    assert result['authorization'] == 'none' and isinstance(result['items'], list)
    # The verdict carries no content and the store is unchanged.
    item = next(i for i in result['items'] if i['call_id'] == call_id)
    assert (item['verdict'], item['basis'], item['action_id']) == ('occurred', 'after_present', aid)
    for item in result['items']:
        assert set(item) <= {'call_id', 'tool', 'action_id', 'kind', 'process_id', 'verdict', 'basis', 'authorization'}
    assert app.store.query('SELECT status,payload FROM actions WHERE id=?', (aid,)) == before
    tool = next(t for t in app.runs.tools(rid) if t['call_id'] == call_id)
    assert tool['state'] == 'uncertain'  # reading back settles nothing


def test_run_reconcile_readback_refuses_an_unknown_verdict_after_the_root_was_replaced(host):
    app = host.app
    rid, call_id, aid, path = seed_uncertain_edit(host, missing=False)
    moved = host.project.with_name('project-moved')
    host.project.rename(moved)
    host.project.mkdir()
    (host.project / path).write_text('AFTER\n')  # same bytes, but a different folder identity
    with pytest.raises(ValueError, match='clean occurred or not_occurred'):
        app.run_reconcile(host.cid, rid, call_id, 'readback', 'inspected')
    verdict = reconcile.readback(app, {'id': aid, 'chat_id': host.cid})
    assert (verdict['verdict'], verdict['evidence']['basis']) == ('unknown', 'root_replaced')


@pytest.mark.parametrize('records, plan, settles, effect', [
    ([SPAWNING, SPAWNED, exited('command_exit', 0, True), TEARDOWN], True, True, 'occurred'),
    ([], False, True, 'did_not_occur'),  # no plan and no record: provably nothing ran
    ([SPAWNING, SPAWNED, exited('stopped', -9, True), TEARDOWN], True, False, None),  # contained, partial possible
    ([SPAWNING, SPAWNED, exited('containment_lost', None, False), TEARDOWN], True, False, None),
], ids=['contained_exit', 'never_started', 'stopped_partial', 'containment_lost'])
def test_command_readback_settles_only_a_clean_supervised_verdict_and_never_replays(
        host, no_signals, records, plan, settles, effect):
    rid = host.root()
    planted = plant_supervised(host, rid, records, plan=plan)
    app = host.restart()  # the run now needs reconciliation and the StartCommand receipt is uncertain
    assert app.runs.get(rid)['state'] == 'needs_reconciliation'
    jobs_before = app.store.query('SELECT id,state,outcome_uncertain FROM process_jobs')
    if settles:
        result = app.run_reconcile(host.cid, rid, planted.call_id, 'readback', 'inspected')
        assert (result['status'], result['effect']) == ('readback_verified', effect)
        assert app.runs.get(rid)['state'] == 'paused'
    else:
        with pytest.raises(ValueError, match='clean occurred or not_occurred'):
            app.run_reconcile(host.cid, rid, planted.call_id, 'readback', 'inspected')
        tool = next(t for t in app.runs.tools(rid) if t['call_id'] == planted.call_id)
        assert tool['state'] == 'uncertain' and app.runs.get(rid)['state'] == 'needs_reconciliation'
    # Never replayed: no new job, no changed job row, no signal, no new supervisor folder.
    assert app.store.query('SELECT id,state,outcome_uncertain FROM process_jobs') == jobs_before
    assert no_signals == [] and sorted(p.name for p in (host.home / 'jobs').iterdir()) == [planted.jobdir.name]


def test_forged_action_ids_and_caller_verdicts_are_refused(host):
    app = host.app
    rid, call_id, aid, path = seed_uncertain_edit(host, missing=False)
    other = app.new_chat(str(host.project))['id']
    for action, basis in (({'id': 'f' * 32, 'chat_id': host.cid}, 'not_found'),
                          ({'id': 'not-hex', 'chat_id': host.cid}, 'not_found'),
                          ({'id': aid, 'chat_id': other}, 'forged'),
                          ({'id': aid, 'chat_id': host.cid, 'kind': 'command'}, 'forged'),
                          ({'id': aid, 'chat_id': host.cid, 'payload': {'path': 'elsewhere.txt'}}, 'forged')):
        verdict = reconcile.readback(app, action)
        assert (verdict['verdict'], verdict['evidence']['basis'], verdict['authorization']) == ('unknown', basis, 'none')
    # run_reconcile takes no verdict from its caller: the RPC rejects the extra field outright.
    with pytest.raises(TypeError):
        app.dispatch('run_reconcile', {'id': host.cid, 'run_id': rid, 'call_id': call_id, 'effect': 'readback',
                                       'note': 'x', 'verdict': {'verdict': 'occurred'}})
    # A clean verdict that names another action cannot settle this call either.
    fabricated = {'verdict': 'occurred', 'authorization': 'none',
                  'evidence': {'basis': 'after_present', 'action_id': 'e' * 32, 'run_id': rid, 'call_id': call_id}}
    with pytest.raises(ValueError, match='another action'):
        app.runs.settle_readback(rid, call_id, fabricated)
    assert next(t for t in app.runs.tools(rid) if t['call_id'] == call_id)['state'] == 'uncertain'


def test_readback_and_settlement_are_native_only(host):
    from pebble.agent_manager import ADMITTED
    app = host.app
    names = {'run_readback', 'run_reconcile', 'process_inspect'}
    catalogue = {t['function']['name'] for t in canonical_catalogue()}
    assert not names & catalogue and not names & ADMITTED and not names & set(GROUP)
    rid = host.root()
    # The model's root route refuses them at the catalogue layer; nothing is read or settled.
    host.own(rid)
    try:
        for name in sorted(names):
            with pytest.raises(PolicyError, match='canonical catalogue'):
                app.execute(name, {'id': host.cid, 'run_id': rid}, Workspace(host.project), host.cid,
                            call_id='1:' + name)
    finally:
        host.release()
    # A child, even with a corrupted admitted set naming them, is refused.
    config = run_config_for(host, rid)
    for name in sorted(names):
        d = authz_check(tool=name, args={}, actor=Actor.child(rid, 'c' * 32, 1), run_config=config,
                        catalogue_digest=app.tool_catalogue_digest, policy_version=app.policy_version(),
                        policy=app.store.setting('permissions', DEFAULT), admitted=lambda a: set(names))
        assert not d.allowed and d.refusal_layer == 'catalogue'
    # Both RPCs refuse while a run is active in this service.
    host.own(rid)
    try:
        for method in ('run_readback', 'run_reconcile'):
            params = {'id': host.cid, 'run_id': rid}
            if method == 'run_reconcile':
                params.update(call_id='x', effect='readback', note='x')
            with pytest.raises(ValueError, match='Stop the active run'):
                app.dispatch(method, params)
    finally:
        host.release()
    # The native shell: only the main and pet windows reach the RPC bridge, generated
    # views never do, and the pet window's list excludes readback and settlement.
    source = (Path(__file__).resolve().parents[2] / 'src-tauri' / 'src' / 'main.rs').read_text()
    gate = next(line for line in source.splitlines() if 'Generated views cannot invoke native agent controls' in line)
    assert '["main","pet"].contains(&window.label())' in gate
    allowed = next(line for line in source.splitlines() if line.lstrip().startswith('const ALLOWED'))
    pet = next(line for line in source.splitlines() if 'window.label()=="pet" && ![' in line)
    for name in ('run_readback', 'run_reconcile', 'process_inspect'):
        assert f'"{name}"' in allowed and f'"{name}"' not in pet


# =====================================================================================
# 8. Platform (Linux and Windows always refuse)
# =====================================================================================

@pytest.mark.parametrize('platform', ['linux', 'win32', 'cygwin', 'freebsd14'])
def test_monkeypatched_linux_or_windows_refuses_every_execution_kind(tmp_path, monkeypatch, platform):
    from pebble import async_terminal, containment, environments, git_workflows, mcp_transport, testing
    monkeypatch.setattr(sys, 'platform', platform)
    # Two gates bind the platform at import time; on that OS they would read it there too.
    monkeypatch.setattr(environments, 'PLATFORM', platform)
    monkeypatch.setattr(git_workflows, 'PLATFORM', platform)
    report = platform_providers.provider()
    assert report['supported'] is False and report['confinement'] is None
    for kind in platform_providers.KINDS:
        with pytest.raises(PolicyError, match='execution stays disabled'):
            platform_providers.require(kind)
    # Every real execution gate refuses before it reads its payload or spawns anything.
    spawned = []
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: spawned.append(a) or pytest.fail('spawned'))
    gates = {
        'command': lambda: terminal.run_command({}, threading.Event()),
        'tests': lambda: testing.run_tests({}, threading.Event()),
        'git': git_workflows.confinement,
        'install': environments.Environments._supported,
        'mcp_stdio': lambda: mcp_transport.stdio_config({'command': 'x'}),
    }
    assert set(gates) == set(platform_providers.KINDS)
    for kind, gate in gates.items():
        with pytest.raises(PolicyError):
            gate()
    assert async_terminal._supported() is False  # background commands
    assert containment.self_test(tmp_path / 'selftest') == {
        'contained': False, 'reason': 'unsupported_platform', 'elapsed': pytest.approx(0, abs=1)}
    assert spawned == []


def test_this_host_platform_is_reported_honestly():
    report = platform_providers.provider()
    if MAC:
        assert report == {'platform': 'darwin', 'supported': True, 'confinement': 'sandbox-exec', 'reason': None}
    else:
        assert report['supported'] is False


# =====================================================================================
# 9. Defect F6-sync, found by W6 as a strict xfail and fixed by the parent (W6 D1 applied
#    to terminal.run_command and testing.run_tests); kept as an ordinary regression case
# =====================================================================================

@macos
def test_defect_synchronous_command_output_reaches_the_model_unsanitized(host):
    """The real App: a reviewed synchronous RunCommand prints hostile terminal output.
    Its committed receipt (what the model is shown on its next turn) must not carry
    OSC 52/OSC 8/CSI/C1 or bidi controls, as ReadProcess pages already do not."""
    app = host.app
    (host.project / 'hostile.py').write_text('import sys\nsys.stdout.buffer.write(%r)\n' % HOSTILE)
    rid = host.root()
    host.own(rid)
    try:
        staged = app.execute('RunCommand', {'command': 'python3 hostile.py'}, Workspace(host.project), host.cid,
                             call_id='1:hostile')
    finally:
        host.release()
    result = app.approve(host.cid, staged['action_id'])
    assert result['exit_code'] == 0 and 'before' in result['output']  # the command really ran and printed
    receipt = next(t for t in app.runs.tools(rid) if t['call_id'] == '1:hostile')['result']
    shown = json.loads(encode_result(receipt))['result']['output']  # the model-visible projection, decoded
    assert '\x1b' not in shown and '\x9b' not in shown and '‮' not in shown
    assert 'evil.invalid' not in shown and 'ZXZpbA' not in shown


# =====================================================================================
# 10. Parent regression: a crash leaves the broker's action 'running' (it is marked
#     before the effect). Reconciling the outcome must also end that in-flight state,
#     or run_resume refuses forever ("Resolve this run's pending actions").
# =====================================================================================

@pytest.mark.parametrize('effect, how', [('occurred', 'readback'), ('did_not_occur', 'readback'),
                                         ('occurred', 'report'), ('did_not_occur', 'report')])
def test_reconciling_an_in_flight_action_ends_it_without_replay(host, effect, how):
    app = host.app
    rid, call_id, aid, path = seed_uncertain_edit(host, after='AFTER\n', missing=effect == 'did_not_occur')
    app.store.query("UPDATE actions SET status='running' WHERE id=?", (aid,))
    before = (host.project / path).read_bytes() if (host.project / path).exists() else None
    if how == 'readback':
        result = app.run_reconcile(host.cid, rid, call_id, 'readback', '')
        assert result['status'] == 'readback_verified' and result['effect'] == effect
    else:
        result = app.run_reconcile(host.cid, rid, call_id, effect, 'checked the project folder')
        assert result['status'] == 'user_reported' and result['verified'] is False
    status = app.store.query('SELECT status FROM actions WHERE id=?', (aid,))[0]['status']
    assert status == ('applied' if effect == 'occurred' else 'failed')
    assert app.runs.get(rid, host.cid)['state'] == 'paused'
    # The resume guard no longer sees an in-flight action, and nothing was performed again.
    action_ids = {t['result'].get('action_id') for t in app.runs.tools(rid) if isinstance(t.get('result'), dict)}
    assert not any(a['id'] in action_ids and a['status'] in {'pending', 'running'} for a in app.store.actions(host.cid))
    after = (host.project / path).read_bytes() if (host.project / path).exists() else None
    assert after == before


def test_reconciliation_never_rewrites_a_settled_action(host):
    app = host.app
    rid, call_id, aid, path = seed_uncertain_edit(host, after='AFTER\n', missing=True)
    app.store.query("UPDATE actions SET status='rejected' WHERE id=?", (aid,))
    app.run_reconcile(host.cid, rid, call_id, 'occurred', 'person says it happened')
    assert app.store.query('SELECT status FROM actions WHERE id=?', (aid,))[0]['status'] == 'rejected'
