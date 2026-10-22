"""Supervised process jobs: real shim runs, restart reattachment, recovery from records, time holds.

Signal safety: every job that reaches a real anchor gets an outside canary and the anchor's SIGURG
interlock (a test-only plan field), so the sandbox-instance kill(-1) sweep proves its scoping first.
The only process a test signals directly is one it started itself (a backend interpreter or a
canary), by live handle. Recovery never signals anything: synthetic cases replace os.kill and
os.killpg with recorders and assert that neither was called.
"""

import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import command_plans, job_shim, migrations, reconcile
from pebble import process_jobs as pj
from pebble.policy import PolicyError, Workspace
from pebble.process_jobs import PROCESS_DDL, ProcessJobs
from pebble.runtime import RunJournal
from pebble.store import Store
from pebble.terminal import inspect_command
from pebble.time_ledger import TimeLedger

TESTS = Path(__file__).resolve().parent
BACKEND = TESTS.parent
MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
macos = pytest.mark.skipif(not MAC, reason='actual macOS confinement')
DEAD = 'f' * 32  # the owner token of a service that is gone
CANARY = '''import os, signal, sys, time
signal.signal(signal.SIGURG, lambda *a: open(sys.argv[1] + '.urg', 'a').write('x'))
end = time.time() + 120
while time.time() < end:
    open(sys.argv[1] + '.t', 'w').write(str(time.time())); os.replace(sys.argv[1] + '.t', sys.argv[1] + '.beat'); time.sleep(0.05)
'''


# --- outside canaries and the anchor's SIGURG interlock -----------------------------------

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
    """(reached, alive): did SIGURG reach it, and was its heartbeat fresh? Then end it by its handle."""
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


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    """Every plan written in this process gets its own outside canary and the anchor's interlock."""
    canaries, real = [], job_shim.write_plan

    def write_plan(jobdir, plan):
        canary = outside_canary(tmp_path)
        canaries.append(canary)
        return real(jobdir, {**plan, 'interlock_abort_path': str(canary['urg'])})
    monkeypatch.setattr(job_shim, 'write_plan', write_plan)
    yield canaries
    assert [canary_result(c) for c in canaries] == [(False, True)] * len(canaries), \
        'A job sweep reached or killed a process outside its sandbox.'


@pytest.fixture
def no_signals(monkeypatch):
    sent = []
    monkeypatch.setattr(os, 'kill', lambda *args: sent.append(('kill', args)))
    monkeypatch.setattr(os, 'killpg', lambda *args: sent.append(('killpg', args)))
    return sent


def wait_for(predicate, seconds=30, step=0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


# --- hosts and workers ------------------------------------------------------------------

class FakeShim:
    """Takes the supervisor arguments, as the real worker does, but never spawns anything."""

    def __init__(self, result=None, chunks=(), hold=False):
        self.result = result or {'state': 'succeeded', 'exit_code': 0, 'isolation': 'fake supervisor',
                                 'outcome_uncertain': False}
        self.chunks, self.hold = list(chunks), hold
        self.calls, self.release = [], threading.Event()

    def __call__(self, payload, cancel, on_started, on_output, *, timeout, max_output, jobdir=None, launcher=None,
                 on_disconnect='terminate'):
        self.calls.append({'payload': payload, 'timeout': timeout, 'jobdir': jobdir, 'launcher': launcher,
                           'on_disconnect': on_disconnect})
        on_started({'pid': 4242, 'isolation': 'fake supervisor'})
        for chunk in self.chunks:
            on_output(chunk)
        if self.hold:
            while not self.release.wait(0.02) and not cancel.is_set():
                pass
            if cancel.is_set():
                return {'state': 'cancelled', 'exit_code': None, 'isolation': 'fake supervisor',
                        'outcome_uncertain': True}
        return dict(self.result)


class Host:
    """A real Store, RunJournal and TimeLedger with the process manager under test."""

    def __init__(self, tmp_path, *, worker=None, resume=True, fresh=True, home=None, budget=900.0):
        self.tmp = tmp_path.resolve()
        self.home = home or self.tmp / 'data'
        self.project = self.tmp / 'project'
        self.project.mkdir(exist_ok=True)
        self.store = Store(self.home)
        self.runs = RunJournal(self.store, self.home)
        self.ledger = TimeLedger(self.store)
        self.ledger.recover()
        self.events = []
        self.app = SimpleNamespace(home=self.home, store=self.store, runs=self.runs, time_ledger=self.ledger,
                                   active=None, active_run=None, closed=False,
                                   emit=lambda kind, payload: self.events.append((kind, payload)))
        self.ws = Workspace(self.project)
        self.cid = self.rid = None
        if fresh:
            self.cid = self.store.new_chat(str(self.ws.root))
            self.rid = self.runs.create(self.cid, 'Supervised process fixture', {
                'workspace': str(self.ws.root), 'workspace_identity': list(self.ws.identity),
                'limits': {'max_steps': 8, 'max_seconds': budget}})
        self.jobs = ProcessJobs(self.app, worker=worker, resume=resume)
        self.jobs.containment = lambda: {'contained': True, 'reason': 'fixture'}
        self.count = 0

    def payload(self, script='probe.py', **changes):
        checked = inspect_command('python3 ' + script, str(self.ws.root), False)
        return {**checked, 'requires_review': True, 'writable': False, 'asynchronous': True, **changes}

    def plan_payload(self, script='probe.py', **fields):
        plan = command_plans.from_argv([{'argv': ['python3', script]}], str(self.ws.root), **fields)
        return {**command_plans.legacy_payload(plan), 'plan': plan, 'requires_review': True, 'asynchronous': True}

    def action(self, payload, reviewed=300, status='running'):
        self.count += 1
        call_id = f'{self.count}:call_{uuid.uuid4().hex[:8]}'
        self.runs.begin_tool(self.rid, call_id, 'StartCommand', {'command': payload['command']})
        aid = self.store.action(self.cid, 'command', {**payload, 'timeout': reviewed, 'permission_mode': 'ask'})
        self.runs.bind_action(self.rid, call_id, aid, 'command')
        self.store.query('UPDATE actions SET status=? WHERE id=?', (status, aid))
        return next(a for a in self.store.actions(self.cid) if a['id'] == aid), call_id

    def start(self, payload, timeout=60, reviewed=300):
        action, _ = self.action(payload, reviewed)
        self.app.active, self.app.active_run = self.cid, self.rid
        return self.jobs.start(action, self.cid, payload, timeout=timeout), action

    def row(self, process_id):
        return self.store.query('SELECT * FROM process_jobs WHERE id=?', (process_id,))[0]

    def text(self, process_id):
        return b''.join(bytes(r['data']) for r in self.store.query(
            'SELECT data FROM process_output WHERE process_id=? ORDER BY sequence', (process_id,)))

    def hold(self, process_id):
        rows = self.store.query('SELECT r.* FROM run_time_reservations r JOIN process_jobs p '
                                'ON p.reservation_id=r.reservation_id WHERE p.id=?', (process_id,))
        return rows[0] if rows else None

    def settled(self, process_id, seconds=40):
        assert wait_for(lambda: self.row(process_id)['state'] not in ('admitted', 'running'), seconds), \
            self.diagnose(process_id)
        return self.row(process_id)

    def diagnose(self, process_id):
        row = self.row(process_id)
        jobdir = Path(row['jobdir']) if row['jobdir'] else None
        return {'row': {k: row[k] for k in ('state', 'reason', 'exit_code', 'settled_by', 'contained')},
                'records': job_shim.read_records(jobdir) if jobdir else None,
                'spool': job_shim.attach(jobdir).read_spool(0, 4000) if jobdir else None}

    def close(self):
        self.app.active = self.app.active_run = None
        closed = self.jobs.close()
        self.runs.close()
        self.store.close()
        return closed


def record_lines(*records):
    stamp = time.time()
    return ''.join(json.dumps({**r, 'ts': stamp + i * 0.25}) + '\n' for i, r in enumerate(records))


def plant(host, records=(), *, spool=b'', stored=b'', plan=True, state='running', jobdir=None, hold=60.0,
          mkdir=True):
    """A supervised row and job folder exactly as a previous service that died would leave them."""
    payload = host.payload()
    action, call_id = host.action(payload)
    process_id = uuid.uuid4().hex
    folder = host.jobs._jobdir_for(process_id) if jobdir is None else jobdir
    host.jobs._jobs_folder()
    if mkdir:
        folder.mkdir(mode=0o700, exist_ok=True)
    if plan:
        job_shim.write_plan(folder, {'version': 1, 'argv': ['/usr/bin/true'], 'env': {}, 'cwd': str(host.ws.root),
                                     'profile': '', 'launcher': [], 'limits': {}, 'secret': uuid.uuid4().hex,
                                     'isolation': 'recorded supervisor isolation'})
    if records:
        (folder / 'records.jsonl').write_text(record_lines(*records))
    if spool:
        (folder / 'output.spool').write_bytes(spool)
    reservation = host.ledger.reserve(host.rid, hold, 'process', action['id']) if hold else None
    now = time.time()
    row = {'id': process_id, 'action_id': action['id'], 'chat_id': host.cid, 'run_id': host.rid, 'call_id': call_id,
           'root': str(host.ws.root), 'root_identity': json.dumps(list(host.ws.identity)), 'payload_hash': 'a' * 64,
           'command': payload['command'], 'network': 0, 'writable': 0, 'owner': DEAD, 'state': state,
           'exit_code': None, 'isolation': None, 'outcome_uncertain': 0, 'pid': None, 'stop_requested': 0,
           'output_bytes': len(stored), 'output_dropped': 0, 'timeout': 60.0, 'reason': None, 'created': now,
           'started': now if state == 'running' else None, 'finished': None, 'updated': now,
           'jobdir': str(folder), 'plan_hash': None, 'contained': None, 'settled_by': None,
           'reservation_id': reservation}
    host.store.query(f'INSERT INTO process_jobs({",".join(row)}) VALUES({",".join("?" * len(row))})',
                     tuple(row.values()))
    if stored:
        host.store.query('INSERT INTO process_output VALUES(?,?,?,?)', (process_id, 0, 0, stored))
    return SimpleNamespace(id=process_id, jobdir=folder, action=action, call_id=call_id, hold=reservation)


SPAWNING, SPAWNED, TEARDOWN = {'record': 'spawning'}, {'record': 'spawned', 'anchor_pid': 4242}, {'record': 'teardown'}


def exited(reason='command_exit', code=0, contained=True):
    return {'record': 'exited', 'exit_code': code, 'contained': contained, 'reason': reason,
            'output_bytes': 0, 'dropped_bytes': 0, 'peak_memory_bytes': 0}


@pytest.fixture
def recovering(tmp_path):
    """A host whose manager has not resumed yet: rows planted now are what a dead service left."""
    host = Host(tmp_path, worker=FakeShim(), resume=False)
    yield host
    host.close()


# --- recovery from a gone supervisor's records -------------------------------------------------

@pytest.mark.parametrize('records, state, uncertain, code, contained, refunded', [
    ([SPAWNING, SPAWNED, exited('command_exit', 0), TEARDOWN], 'succeeded', 0, 0, 1, True),
    ([SPAWNING, SPAWNED, exited('command_exit', 3), TEARDOWN], 'failed', 0, 3, 1, True),
    ([SPAWNING, SPAWNED, exited('stopped', -9), TEARDOWN], 'cancelled', 1, -9, 1, False),
    # The service went away: unknown, not an owned cancellation (Phase 12 crash contract); still contained.
    ([SPAWNING, SPAWNED, exited('host_disconnect', -9), TEARDOWN], 'unknown', 1, None, 1, False),
    ([SPAWNING, SPAWNED, exited('timed_out', -9), TEARDOWN], 'timed_out', 1, -9, 1, False),
    ([SPAWNING, SPAWNED, exited('memory', -9), TEARDOWN], 'unknown', 1, None, 1, False),
    ([SPAWNING, SPAWNED, exited('containment_lost', None, False), TEARDOWN], 'unknown', 1, None, 0, False),
    ([SPAWNING, SPAWNED, exited('command_exit', 0, False), TEARDOWN], 'unknown', 1, None, 0, False),
    ([SPAWNING, SPAWNED], 'unknown', 1, None, 0, False),       # the supervisor itself was killed
    ([SPAWNING], 'unknown', 1, None, 0, False),                # B3: spawning without proof either way
    ([SPAWNING, exited('anchor_spawn_failed:exec_error', None, False), TEARDOWN], 'refused', 0, None, 0, True),
])
def test_gone_supervisor_settles_only_from_its_records(recovering, no_signals, records, state, uncertain, code,
                                                       contained, refunded):
    host = recovering
    job = plant(host, records, spool=b'stored prefix|after the crash\n', stored=b'stored prefix|')
    assert host.row(job.id)['state'] == 'running' and host.row(job.id)['owner'] == DEAD  # deferred until resume()
    assert host.jobs.resume() == {'reattached': [], 'settled': [job.id]}
    row = host.row(job.id)
    assert (row['state'], row['outcome_uncertain'], row['exit_code'], row['contained']) == (state, uncertain, code,
                                                                                           contained), row
    assert row['settled_by'] == 'supervisor_record' and row['owner'] == host.jobs.owner
    assert row['isolation'] == 'recorded supervisor isolation'
    # The spool continues the stored prefix exactly: nothing replayed, skipped or duplicated.
    assert host.text(job.id) == b'stored prefix|after the crash\n' and row['output_bytes'] == 30
    hold = host.ledger.reservation(job.hold)
    if refunded:
        assert hold['state'] == 'settled' and hold['charged'] < 60.0
    else:
        assert (hold['state'], hold['charged']) == ('uncertain', 60.0)
    home = job.jobdir.with_name(job.jobdir.name + '-home')
    assert job.jobdir.exists() and (job_shim.read_records(job.jobdir) == [] if not records else True)
    assert not home.exists()
    assert host.jobs.resume() == {'reattached': [], 'settled': []}  # idempotent
    assert no_signals == []


def test_missing_plan_proves_nothing_ran_but_a_plan_without_records_stays_unknown(recovering, no_signals):
    host = recovering
    never = plant(host, plan=False, mkdir=False)
    unclear = plant(host, plan=True)
    host.jobs.resume()
    first, second = host.row(never.id), host.row(unclear.id)
    assert (first['state'], first['outcome_uncertain'], first['settled_by']) == ('refused', 0, 'supervisor_record')
    assert 'nothing ran' in first['reason']
    assert (second['state'], second['outcome_uncertain'], second['contained']) == ('unknown', 1, 0)
    assert host.ledger.reservation(never.hold)['state'] == 'settled'
    assert host.ledger.reservation(unclear.hold)['state'] == 'uncertain'
    assert host.jobs.evidence(never.id)['spawning'] is False and host.jobs.evidence(unclear.id)['spawning'] is None
    assert no_signals == []


def test_unsupervised_and_mismatched_rows_are_unknown_without_replay(tmp_path, no_signals):
    host = Host(tmp_path, worker=FakeShim(), resume=False)
    try:
        foreign = plant(host, [SPAWNING, SPAWNED, exited()], jobdir=host.tmp / 'elsewhere')
        legacy = plant(host)
        host.store.query('UPDATE process_jobs SET jobdir=NULL WHERE id=?', (legacy.id,))
        host.close()
        host = Host(tmp_path, worker=FakeShim(), resume=True, fresh=False)
        host.cid, host.rid = foreign.action['chat_id'], host.store.query('SELECT run_id FROM process_jobs')[0]['run_id']
        for job in (foreign, legacy):
            row = host.row(job.id)
            assert (row['state'], row['outcome_uncertain'], row['settled_by']) == ('unknown', 1, 'recovery'), row
            assert host.ledger.reservation(job.hold)['state'] == 'uncertain'
        assert host.text(foreign.id) == b''  # a folder that is not this job's is never read
        assert host.jobs.resume() == {'reattached': [], 'settled': []}
    finally:
        host.close()
    assert no_signals == []


def test_resume_is_deferred_until_called_and_never_runs_twice(recovering):
    host = recovering
    job = plant(host, [SPAWNING, SPAWNED, exited()])
    time.sleep(0.2)
    assert host.row(job.id)['state'] == 'running'  # resume=False: startup changed nothing yet
    with pytest.raises(PolicyError, match='no live control owner'):
        host.jobs.stop(host.cid, host.rid, job.id)
    assert host.jobs.resume()['settled'] == [job.id]
    assert host.row(job.id)['state'] == 'succeeded'


def test_alive_supervisor_is_reattached_followed_and_stopped_only_through_its_socket(recovering, no_signals):
    host = recovering
    job = plant(host, [SPAWNING, SPAWNED], spool=b'before restart\n', stored=b'before ')
    lock = os.open(job.jobdir / 'shim.lock', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a supervisor that is still alive holds this
    try:
        assert host.jobs.resume() == {'reattached': [job.id], 'settled': []}
        assert wait_for(lambda: host.text(job.id) == b'before restart\n', 10), host.text(job.id)
        with open(job.jobdir / 'output.spool', 'ab') as spool:
            spool.write(b'after restart\n')
        assert wait_for(lambda: host.text(job.id) == b'before restart\nafter restart\n', 10)
        view = host.jobs.list(host.cid, host.rid)[0]
        assert view['live'] and view['controllable'] and view['state'] == 'running'
        stopped = host.jobs.stop(host.cid, host.rid, job.id)
        assert stopped['stop'] == 'requested' and host.row(job.id)['stop_requested'] == 1
        time.sleep(0.3)
        assert host.row(job.id)['state'] == 'running'  # only the supervisor's record settles it
        with open(job.jobdir / 'records.jsonl', 'a') as records:
            records.write(record_lines(exited('stopped', -9), TEARDOWN))
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
    row = host.settled(job.id)
    assert (row['state'], row['exit_code'], row['outcome_uncertain'], row['contained'], row['settled_by']) == (
        'cancelled', -9, 1, 1, 'supervisor_record')
    assert host.ledger.reservation(job.hold)['state'] == 'uncertain'
    assert no_signals == []


def test_reattached_admitted_job_records_its_observed_start(recovering):
    host = recovering
    job = plant(host, [SPAWNING], state='admitted')
    lock = os.open(job.jobdir / 'shim.lock', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        host.jobs.resume()
        time.sleep(0.2)
        assert host.row(job.id)['state'] == 'admitted'
        with open(job.jobdir / 'records.jsonl', 'a') as records:
            records.write(record_lines(SPAWNED))
        assert wait_for(lambda: host.row(job.id)['state'] == 'running', 10)
        assert host.row(job.id)['pid'] == 4242  # diagnostic evidence only
        with open(job.jobdir / 'records.jsonl', 'a') as records:
            records.write(record_lines(exited('command_exit', 0), TEARDOWN))
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
    assert host.settled(job.id)['state'] == 'succeeded'


def test_evidence_is_content_free_and_drives_reconcile_readback(recovering):
    host = recovering
    job = plant(host, [SPAWNING, SPAWNED, exited('command_exit', 0), TEARDOWN], spool=b'secret output\n')
    host.store.query("UPDATE process_jobs SET state='unknown',outcome_uncertain=1,settled_by='shutdown' WHERE id=?",
                     (job.id,))
    evidence = host.jobs.evidence(job.id)
    assert evidence == {'spawning': True, 'spawned': True, 'exited': True, 'contained': True, 'exit_code': 0,
                        'reason': 'command_exit', 'alive': False, 'settled_by': 'shutdown', 'authorization': 'none'}
    assert 'secret' not in json.dumps(evidence) and str(job.jobdir) not in json.dumps(evidence)
    verdict = reconcile.readback(host.app, {'id': job.action['id'], 'chat_id': host.cid},
                                 job_evidence=host.jobs.evidence)
    assert (verdict['verdict'], verdict['evidence']['basis']) == ('occurred', 'supervised_exit')
    lost = plant(host, [SPAWNING, SPAWNED, exited('containment_lost', None, False)])
    host.store.query("UPDATE process_jobs SET state='unknown',outcome_uncertain=1 WHERE id=?", (lost.id,))
    verdict = reconcile.readback(host.app, {'id': lost.action['id'], 'chat_id': host.cid},
                                 job_evidence=host.jobs.evidence)
    assert (verdict['verdict'], verdict['evidence']['basis']) == ('unknown', 'containment_unproven')
    for bad in (None, 'x', 'A' * 32, uuid.uuid4().hex):
        assert host.jobs.evidence(bad) is None


def test_dismiss_and_forget_keep_folders_of_a_live_supervisor_and_remove_settled_ones(recovering):
    host = recovering
    job = plant(host, [SPAWNING, SPAWNED, exited(), TEARDOWN], state='succeeded')
    lock = os.open(job.jobdir / 'shim.lock', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(PolicyError, match='live supervisor'):
            host.jobs.dismiss(host.cid, host.rid, job.id)
        with pytest.raises(PolicyError, match='live supervisor'):
            host.jobs.forget(host.cid)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
    assert job.jobdir.exists()
    assert host.jobs.dismiss(host.cid, host.rid, job.id) == {'process_id': job.id, 'dismissed': True}
    assert not job.jobdir.exists()


# --- admission: gate, folder, reservations, plan binding ---------------------------------------

def test_supervised_admission_requires_the_containment_gate(tmp_path):
    host = Host(tmp_path, worker=FakeShim())
    try:
        (host.project / 'probe.py').write_text("print('x')\n")
        for status, match in ((None, 'containment is verified'), ({'contained': None, 'reason': None}, 'verified'),
                              ({'contained': False, 'reason': 'Process containment self-test failed (x).'},
                               r'disabled: Process containment self-test failed \(x\)\. Nothing was started')):
            host.jobs.containment = (lambda value=status: value) if status is not None else None
            with pytest.raises(PolicyError, match=match):
                host.start(host.payload())
        host.jobs.containment = lambda: (_ for _ in ()).throw(RuntimeError('self-test crashed'))
        with pytest.raises(PolicyError, match='containment is verified'):
            host.start(host.payload())
        assert host.store.query('SELECT count(*) AS n FROM process_jobs') == [{'n': 0}] and host.jobs.worker.calls == []
        assert host.store.query("SELECT count(*) AS n FROM run_time_reservations WHERE state='open'") == [{'n': 0}]
        host.jobs.containment = lambda: {'contained': True, 'reason': 'Process containment verified.'}
        view, _ = host.start(host.payload())
        assert view['state'] == 'admitted'
    finally:
        host.close()


def test_supervised_job_gets_its_folder_launcher_and_reviewed_on_disconnect(tmp_path):
    worker = FakeShim()
    host = Host(tmp_path, worker=worker)
    try:
        (host.project / 'probe.py').write_text("print('x')\n")
        view, _ = host.start(host.payload())
        row = host.settled(view['process_id'])
        [call] = worker.calls
        assert call['jobdir'] == row['jobdir'] == str(host.home.resolve() / 'jobs' / view['process_id'][:12])
        assert call['on_disconnect'] == 'terminate' and call['launcher'] == host.jobs._launcher
        prefix, reads = call['launcher']
        assert prefix[-1].endswith('backend/main.py') and reads  # the source launcher dispatches --job-shim
        folder = host.home.resolve() / 'jobs'
        assert folder.is_dir() and folder.stat().st_mode & 0o777 == 0o700
        planned = host.plan_payload(on_disconnect='continue')
        view, _ = host.start(planned)
        row = host.settled(view['process_id'])
        assert worker.calls[-1]['on_disconnect'] == 'continue' and row['plan_hash'] == planned['plan_hash']
        assert 'jobdir' not in host.jobs.list(host.cid)[0]  # the host path never reaches a view
    finally:
        host.close()


def test_job_folder_must_be_private_and_a_long_data_folder_still_works(tmp_path):
    host = Host(tmp_path, worker=FakeShim())
    try:
        (host.project / 'probe.py').write_text("print('x')\n")
        elsewhere = host.tmp / 'elsewhere'
        elsewhere.mkdir()
        (host.home / 'jobs').symlink_to(elsewhere)
        with pytest.raises(PolicyError, match='job folder is missing, shared or replaced'):
            host.start(host.payload())
        (host.home / 'jobs').unlink()
        (host.home / 'jobs').mkdir(mode=0o755)
        (host.home / 'jobs').chmod(0o755)
        with pytest.raises(PolicyError, match='job folder is missing, shared or replaced'):
            host.start(host.payload())
        assert host.store.query('SELECT count(*) AS n FROM process_jobs') == [{'n': 0}]
    finally:
        host.close()
    (tmp_path / 'long').mkdir()
    # The control socket lives in the per-user private temp folder (macOS
    # sun_path limit), so a long service data folder no longer blocks admission.
    long = Host(tmp_path / 'long', worker=FakeShim(), home=tmp_path / ('d' * 64))
    try:
        (long.project / 'probe.py').write_text("print('x')\n")
        from pebble.job_shim import SOCKET_LIMIT, control_path
        assert len(str(control_path(long.home / 'jobs' / ('0' * 12))).encode()) <= SOCKET_LIMIT
        long.start(long.payload())
        assert long.store.query('SELECT count(*) AS n FROM process_jobs') == [{'n': 1}]
    finally:
        long.close()


def test_admission_reserves_from_the_run_pool_and_refunds_only_a_certain_outcome(tmp_path):
    worker = FakeShim(hold=True)
    host = Host(tmp_path, worker=worker)
    try:
        (host.project / 'probe.py').write_text("print('x')\n")
        filler = host.ledger.reserve(host.rid, 830, 'child', 'filler')
        view, action = host.start(host.payload(), timeout=60)  # 70 s unreserved: the review's 60 s fit
        usage = host.ledger.usage(host.rid)
        assert usage['reserved'] == 890.0 and host.hold(view['process_id'])['seconds'] == 60.0
        worker.release.set()
        row = host.settled(view['process_id'])
        hold = host.hold(view['process_id'])
        assert row['state'] == 'succeeded' and hold['state'] == 'settled' and hold['charged'] < 5.0
        worker.release.clear()
        stopped, _ = host.start(host.payload(), timeout=60)
        assert host.row(stopped['process_id'])['timeout'] == 60.0
        host.jobs.stop(host.cid, host.rid, stopped['process_id'])
        row = host.settled(stopped['process_id'])
        hold = host.hold(stopped['process_id'])
        assert row['state'] == 'cancelled' and (hold['state'], hold['charged']) == ('uncertain', 60.0)
        host.ledger.settle_reservation(filler, 0, True)
        host.ledger.reserve(host.rid, host.ledger.usage(host.rid)['unreserved'] - 30, 'child', 'filler-2')
        worker.hold = False
        short, _ = host.start(host.payload(), timeout=60)
        assert host.row(short['process_id'])['timeout'] == pytest.approx(30.0)  # narrowed to the pool
        assert worker.calls[-1]['timeout'] == pytest.approx(30.0)
        host.settled(short['process_id'])
        host.ledger.reserve(host.rid, host.ledger.usage(host.rid)['unreserved'] - 0.5, 'child', 'filler-3')
        with pytest.raises(PolicyError, match='no unreserved time'):
            host.start(host.payload(), timeout=60)
        assert len(worker.calls) == 3
    finally:
        worker.release.set()
        host.close()


def test_refused_admission_releases_its_hold_in_full(tmp_path):
    worker = FakeShim(hold=True)
    host = Host(tmp_path, worker=worker)
    try:
        (host.project / 'probe.py').write_text("print('x')\n")
        host.start(host.payload())
        host.start(host.payload())
        with pytest.raises(PolicyError, match='At most 2'):
            host.start(host.payload())
        refused = host.store.query("SELECT * FROM run_time_reservations WHERE kind='process' ORDER BY opened")[-1]
        assert (refused['state'], refused['charged']) == ('settled', 0.0)
        assert host.store.query('SELECT count(*) AS n FROM process_jobs') == [{'n': 2}]
    finally:
        worker.release.set()
        host.close()


def test_plan_hash_binding_and_unenforceable_plan_fields_are_refused(tmp_path):
    host = Host(tmp_path, worker=FakeShim())
    try:
        (host.project / 'probe.py').write_text("print('x')\n")
        (host.project / 'src').mkdir()
        good = host.plan_payload()
        cases = [
            ({**good, 'plan_hash': 'b' * 64}, 'changed after review'),
            ({**good, 'plan_hash': 'not-a-hash'}, 'malformed'),
            ({key: value for key, value in good.items() if key != 'plan'}, 'changed after review'),
            ({**good, 'command': 'python3  probe.py'}, 'changed after review'),
        ]
        for payload, match in cases:
            action, _ = host.action(good)
            host.app.active, host.app.active_run = host.cid, host.rid
            with pytest.raises(PolicyError, match=match):
                host.jobs.start(action, host.cid, payload, timeout=60)
        # A requested plan may only narrow the reviewed one; a wider request is refused.
        wide = host.plan_payload(resources={'timeout': 120})
        action, _ = host.action(good)
        with pytest.raises(PolicyError, match='widens or changes'):
            host.jobs.start(action, host.cid, wide, timeout=60)
        for fields, match in (({'writable': ['src']}, 'subpath write'), ({'process_model': 'single'}, 'process model')):
            planned = host.plan_payload(**fields)
            action, _ = host.action(planned)
            with pytest.raises(PolicyError, match=match):
                host.jobs.start(action, host.cid, planned, timeout=60)
        cwd = command_plans.from_argv([{'argv': ['python3', 'probe.py'], 'cwd': 'src'}], str(host.ws.root))
        planned = {**command_plans.legacy_payload(cwd), 'plan': cwd, 'requires_review': True, 'asynchronous': True}
        action, _ = host.action(planned)
        with pytest.raises(PolicyError, match='folder'):
            host.jobs.start(action, host.cid, planned, timeout=60)
        assert host.store.query('SELECT count(*) AS n FROM process_jobs') == [{'n': 0}]
        view, _ = host.start(good)
        assert host.row(view['process_id'])['plan_hash'] == good['plan_hash']
        timed = host.plan_payload()
        action, _ = host.action(timed)
        with pytest.raises(PolicyError, match='reviewed timeout'):
            host.jobs.start(action, host.cid, timed, timeout=61)  # beyond the plan's 60 s
    finally:
        host.close()


def test_output_pages_are_a_sanitized_projection_of_unchanged_raw_bytes(tmp_path):
    raw = b'\x1b[31mRED\x1b[0m \x1b]52;c;U0VDUkVU\x07done\r\nnext\xe2\x80\xae\n'
    worker = FakeShim(chunks=[raw[:9], raw[9:], b'\x1b[2J'], hold=True)
    host = Host(tmp_path, worker=worker)
    try:
        (host.project / 'probe.py').write_text("print('x')\n")
        view, _ = host.start(host.payload())
        pid = view['process_id']
        assert wait_for(lambda: len(host.text(pid)) == len(raw) + 4, 10)
        assert host.text(pid) == raw + b'\x1b[2J'  # stored exactly as written
        page = host.jobs.read(host.cid, host.rid, pid, limit=8000)
        assert page['untrusted_process_output'] == 'RED done\nnext⟦U+202E⟧\n', page
        assert page['controls_removed'] >= 4 and page['output_projection'] == 'terminal_text/1'
        assert 'U0VDUkVU' not in json.dumps(page) and '\x1b' not in page['untrusted_process_output']
        cursor, text = 0, ''
        for _ in range(200):  # one-byte pages never split a sequence and always progress
            page = host.jobs.read(host.cid, host.rid, pid, after=cursor, limit=1)
            assert page['next_after'] > cursor or not page['more']
            text, cursor = text + page['untrusted_process_output'], page['next_after']
            if not page['more']:
                break
        assert text == 'RED done\nnext⟦U+202E⟧\n' and cursor == len(raw) + 4
        tail = host.jobs.read(host.cid, host.rid, pid, after=len(raw), limit=8000, wait_seconds=5)
        assert tail['next_after'] == len(raw) + 4 and tail['untrusted_process_output'] == ''
        started = time.monotonic()
        quiet = host.jobs.read(host.cid, host.rid, pid, after=len(raw) + 4, wait_seconds=0.3)
        assert quiet['next_after'] == len(raw) + 4 and time.monotonic() - started >= 0.25  # waited: nothing new
    finally:
        worker.release.set()
        host.close()


def test_schema_v2_columns_are_added_to_an_older_table_and_match_the_reference(tmp_path):
    old = PROCESS_DDL.replace(''.join(f', {name} {kind}' for name, kind in pj.V2_COLUMNS), '')
    assert old != PROCESS_DDL and 'jobdir' not in old
    home = tmp_path / 'data'
    store = Store(home)
    with store.lock, store.db as db:
        db.executescript(old)
        db.execute('INSERT INTO process_meta VALUES(1)')
    store.query("INSERT INTO process_jobs(id,action_id,chat_id,run_id,call_id,root,root_identity,payload_hash,command,"
                "network,writable,owner,state,outcome_uncertain,stop_requested,output_bytes,output_dropped,timeout,"
                "created,updated) VALUES(?,?,?,?,?,?,?,?,?,0,0,?,'succeeded',0,0,0,0,60,1,1)",
                ('a' * 32, 'b' * 32, 'c' * 32, 'd' * 32, '1:x', '/p', '[1,2]', 'e' * 64, 'ls', DEAD))
    store.close()
    host = Host(tmp_path, worker=FakeShim())
    try:
        columns = {r['name'] for r in host.store.query('PRAGMA table_info(process_jobs)')}
        assert {name for name, _ in pj.V2_COLUMNS} <= columns
        assert host.store.query("SELECT state,jobdir,contained,settled_by FROM process_jobs WHERE id=?", ('a' * 32,)) == [
            {'state': 'succeeded', 'jobdir': None, 'contained': None, 'settled_by': None}]
        memory = sqlite3.connect(':memory:')
        memory.executescript(PROCESS_DDL)
        fresh = memory.execute("SELECT sql FROM sqlite_master WHERE name='process_jobs'").fetchone()[0]
        memory.close()
        altered = host.store.query("SELECT sql FROM sqlite_master WHERE name='process_jobs'")[0]['sql']
        # migrations compares tables by columns, CHECK clauses and tail: the additive path matches the reference.
        assert migrations._clauses(migrations._normal(altered)) == migrations._clauses(migrations._normal(fresh))
    finally:
        host.close()


def test_epoch_4_process_table_needs_exactly_the_proposed_additive_entry(tmp_path, monkeypatch):
    """Evidence for the parent's migrations.ADDITIVE patch: an older table is refused without it."""
    old = PROCESS_DDL.replace(''.join(f', {name} {kind}' for name, kind in pj.V2_COLUMNS), '')
    db = sqlite3.connect(tmp_path / 'epoch4.sqlite')
    try:
        db.executescript(old)
        schema = migrations._schema(db)
    finally:
        db.close()
    found = migrations._compatibility(schema, migrations._reference())
    if ('incompatible_subsystem', 'process_jobs') in found['refusals']:  # before the parent's patch
        monkeypatch.setitem(migrations.ADDITIVE, 'process_jobs', {name for name, _ in pj.V2_COLUMNS})
        found = migrations._compatibility(schema, migrations._reference())
    assert not [r for r in found['refusals'] if r[1] in ('process_jobs', 'process_meta', 'process_output')], found
    assert sorted(found['missing_columns']['process_jobs']) == sorted(name for name, _ in pj.V2_COLUMNS)
    monkeypatch.setitem(migrations.ADDITIVE, 'process_jobs', {'jobdir'})  # anything less is refused
    assert ('incompatible_subsystem', 'process_jobs') in migrations._compatibility(
        schema, migrations._reference())['refusals']


def test_unsupervised_workers_get_no_supervisor_arguments_or_gate(tmp_path):
    calls = []

    def legacy(payload, cancel, on_started, on_output, *, timeout, max_output):
        calls.append(timeout)
        on_started({'pid': 7, 'isolation': 'legacy fixture'})
        return {'state': 'succeeded', 'exit_code': 0, 'isolation': 'legacy fixture', 'outcome_uncertain': False}

    host = Host(tmp_path, worker=legacy)
    try:
        host.jobs.containment = None  # the gate guards only the supervised path
        (host.project / 'probe.py').write_text("print('x')\n")
        view, _ = host.start(host.payload())
        row = host.settled(view['process_id'])
        assert row['state'] == 'succeeded' and row['jobdir'] is None and calls == [60]
        assert not (host.home / 'jobs').exists()
    finally:
        host.close()


# --- real supervised jobs ------------------------------------------------------------------------

@macos
def test_real_supervised_job_runs_contained_and_refunds_its_hold(tmp_path, scoped):
    host = Host(tmp_path)
    try:
        (host.project / 'probe.py').write_text("print('HELLO', flush=True)\n")
        view, _ = host.start(host.payload())
        row = host.settled(view['process_id'])
        assert (row['state'], row['exit_code'], row['outcome_uncertain'], row['contained'], row['settled_by']) == (
            'succeeded', 0, 0, 1, 'worker'), host.diagnose(view['process_id'])
        assert host.text(view['process_id']) == b'HELLO\n' and 'shim+anchor' in row['isolation']
        jobdir = Path(row['jobdir'])
        assert [r['record'] for r in job_shim.read_records(jobdir)] == ['spawning', 'spawned', 'exited', 'teardown']
        assert not jobdir.with_name(jobdir.name + '-home').exists()  # contained: scratch removed, records kept
        hold = host.hold(view['process_id'])
        assert hold['state'] == 'settled' and hold['charged'] < hold['seconds']
        assert host.jobs.evidence(view['process_id'])['contained'] is True
    finally:
        host.close()
    assert len(scoped) == 1


@macos
def test_real_supervised_stop_is_contained_and_keeps_its_whole_hold(tmp_path, scoped):
    host = Host(tmp_path)
    try:
        (host.project / 'probe.py').write_text("import time\nprint('GO', flush=True)\ntime.sleep(60)\n")
        view, _ = host.start(host.payload())
        pid = view['process_id']
        assert wait_for(lambda: host.text(pid) == b'GO\n', 30), host.diagnose(pid)
        host.jobs.stop(host.cid, host.rid, pid)
        row = host.settled(pid)
        assert (row['state'], row['exit_code'], row['outcome_uncertain'], row['contained']) == (
            'cancelled', -9, 1, 1), host.diagnose(pid)
        assert (host.hold(pid)['state'], host.hold(pid)['charged']) == ('uncertain', 60.0)
    finally:
        host.close()


def crash_backend(tmp, urg, script, mode, report):
    """Child-interpreter body: admit one supervised job, report its first stored output, then wait to die."""
    real = job_shim.write_plan
    job_shim.write_plan = lambda jobdir, plan: real(jobdir, {**plan, 'interlock_abort_path': urg})
    host = Host(Path(tmp))
    (host.project / 'probe.py').write_text(script)
    payload = host.plan_payload(on_disconnect='continue') if mode == 'continue' else host.payload()
    view, action = host.start(payload)
    pid = view['process_id']
    wait_for(lambda: host.text(pid), 60)
    Path(report).write_text(json.dumps({'cid': host.cid, 'rid': host.rid, 'process_id': pid,
                                        'action_id': action['id'], 'stored': host.text(pid).decode()}))
    time.sleep(120)


def crash(tmp_path, script, mode):
    """Run a backend in a child interpreter, then SIGKILL it (our own child, by live handle) mid-job."""
    canary = outside_canary(tmp_path)
    report, log = tmp_path / 'crash.json', tmp_path / 'crash.log'
    code = ('import sys\nsys.path[:0] = [sys.argv[1], sys.argv[2]]\nimport test_process_recovery as suite\n'
            'suite.crash_backend(*sys.argv[3:])\n')
    with open(log, 'wb') as err:
        proc = subprocess.Popen([sys.executable, '-c', code, str(BACKEND), str(TESTS), str(tmp_path),
                                 str(canary['urg']), script, mode, str(report)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
    try:
        assert wait_for(report.exists, 90), log.read_text()[-3000:]
    finally:
        proc.kill()
        proc.wait(timeout=30)
    return json.loads(report.read_text()), canary


def jobdir_of(host, process_id):
    return Path(host.row(process_id)['jobdir'])


@macos
def test_backend_crash_with_continue_reattaches_and_output_continues(tmp_path):
    script = "import time\nprint('FIRST', flush=True)\ntime.sleep(5)\nprint('SECOND', flush=True)\n"
    facts, canary = crash(tmp_path, script, 'continue')
    assert facts['stored'] == 'FIRST\n'
    host = Host(tmp_path, fresh=False, resume=False)
    try:
        host.cid, host.rid, pid = facts['cid'], facts['rid'], facts['process_id']
        assert job_shim.attach(jobdir_of(host, pid)).alive(), 'the supervisor should outlive its backend'
        assert host.jobs.resume() == {'reattached': [pid], 'settled': []}
        assert host.jobs.list(host.cid, host.rid)[0]['controllable'] is True
        row = host.settled(pid)
        assert (row['state'], row['exit_code'], row['outcome_uncertain'], row['contained'], row['settled_by']) == (
            'succeeded', 0, 0, 1, 'supervisor_record'), host.diagnose(pid)
        assert host.text(pid) == b'FIRST\nSECOND\n'  # continued from the stored prefix, nothing doubled
        records = job_shim.read_records(jobdir_of(host, pid))
        assert {'record': 'host_disconnect', 'action': 'continue'}.items() <= next(
            r for r in records if r['record'] == 'host_disconnect').items()
        hold = host.hold(pid)
        assert hold['state'] == 'settled' and hold['charged'] < hold['seconds']
    finally:
        host.close()
    assert canary_result(canary) == (False, True)


@macos
def test_backend_crash_with_terminate_settles_the_swept_job_from_its_record(tmp_path):
    script = "import time\nprint('FIRST', flush=True)\ntime.sleep(60)\n"
    facts, canary = crash(tmp_path, script, 'terminate')
    host = Host(tmp_path, fresh=False, resume=False)
    try:
        host.cid, host.rid, pid = facts['cid'], facts['rid'], facts['process_id']
        assert wait_for(lambda: not job_shim.attach(jobdir_of(host, pid)).alive(), 40)  # lifeline EOF: it stops
        assert host.jobs.resume() == {'reattached': [], 'settled': [pid]}
        row = host.row(pid)
        # The service went away: unknown with no exit code (Phase 12 crash contract), yet the sweep was contained.
        assert (row['state'], row['exit_code'], row['outcome_uncertain'], row['contained'], row['settled_by']) == (
            'unknown', None, 1, 1, 'supervisor_record'), host.diagnose(pid)
        assert host.text(pid) == b'FIRST\n'
        assert (host.hold(pid)['state'], host.hold(pid)['charged']) == ('uncertain', 60.0)
    finally:
        host.close()
    assert canary_result(canary) == (False, True)


@macos
def test_backend_crash_while_a_continued_job_finishes_settles_it_certainly(tmp_path):
    script = "import time\nprint('FIRST', flush=True)\ntime.sleep(1)\nprint('LAST', flush=True)\n"
    facts, canary = crash(tmp_path, script, 'continue')
    host = Host(tmp_path, fresh=False, resume=False)
    try:
        host.cid, host.rid, pid = facts['cid'], facts['rid'], facts['process_id']
        assert wait_for(lambda: not job_shim.attach(jobdir_of(host, pid)).alive(), 40)
        assert host.jobs.resume() == {'reattached': [], 'settled': [pid]}
        row = host.row(pid)
        assert (row['state'], row['exit_code'], row['outcome_uncertain'], row['contained']) == (
            'succeeded', 0, 0, 1), host.diagnose(pid)
        assert host.text(pid) == b'FIRST\nLAST\n'
        assert host.hold(pid)['state'] == 'settled'
    finally:
        host.close()
    assert canary_result(canary) == (False, True)
