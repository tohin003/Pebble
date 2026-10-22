"""The per-job shim: records, containment lifecycle, governors and reattach.

Signal safety: every shim run here sweeps with the anchor's kill(-1), so every
job gets its own outside canary, and the plan's interlock_abort_path makes the
anchor prove scoping with SIGURG against that canary before any SIGKILL. The
sweep runs only inside the fresh sandbox instance this test's shim created.
The only PID a test signals directly is a shim it started in that same test.
"""
import ast
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from pebble import async_terminal, containment, job_shim, sandbox_profiles
from pebble.terminal import inspect_command

ROOT = Path(__file__).resolve().parents[2]
PY = Path(sys.executable).resolve()
FW = Path('/Library/Frameworks/Python.framework')
READS = [ROOT / 'backend', Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), FW]
BOOT = ("import sys\n"
        "from pebble import job_shim, containment\n"
        "a = sys.argv\n"
        "sys.exit(job_shim.main(a) if '--job-shim' in a else containment.anchor_main(a))\n")
PREFIX = [str(PY), '-c', BOOT]

macos = pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
                           reason='actual macOS confinement')

BEAT = ("import os, time\n"
        "p = os.path.join(os.environ['BEATS'], 'escapee.beat')\n"
        "end = time.time() + 30\n"
        "while time.time() < end:\n"
        "    open(p + '.t', 'w').write(str(time.time())); os.replace(p + '.t', p); time.sleep(0.05)\n")

ESCAPEE = ("import os, sys, time\n"
           "BEAT = %r\n"
           "if os.fork() == 0:\n"
           "    os.setsid()\n"
           "    if os.fork() == 0:\n"
           "        os.execv(sys.executable, [sys.executable, '-I', '-c', BEAT, 'e'])\n"
           "    os._exit(0)\n"
           "os.wait()\n"
           "for _ in range(400):\n"
           "    if os.path.exists(os.path.join(os.environ['BEATS'], 'escapee.beat')):\n"
           "        break\n"
           "    time.sleep(0.01)\n"
           "print('READY', flush=True)\n"
           "time.sleep(60)\n" % BEAT)


# --- outside canary ---------------------------------------------------------

def start_canary(tmp_path):
    beat = tmp_path / ('canary-' + uuid.uuid4().hex[:8])
    script = ("import os, signal, sys, time\n"
              "signal.signal(signal.SIGURG, lambda *a: open(sys.argv[1] + '.urg', 'a').write('x'))\n"
              "end = time.time() + 60\n"
              "while time.time() < end:\n"
              "    open(sys.argv[1] + '.t', 'w').write(str(time.time())); os.replace(sys.argv[1] + '.t', sys.argv[1] + '.beat'); time.sleep(0.05)\n")
    proc = subprocess.Popen([str(PY), '-I', '-c', script, str(beat)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(200):
        if Path(str(beat) + '.beat').exists():
            break
        time.sleep(0.02)
    return {'proc': proc, 'beat': beat, 'urg': Path(str(beat) + '.urg'), 'result': None}


def finish_canary(canary):
    """(reached, alive): did SIGURG reach it, and is its heartbeat still fresh? Idempotent."""
    if canary['result'] is None:
        beat = Path(str(canary['beat']) + '.beat')
        alive = beat.exists() and (time.time() - float(beat.read_text())) < 1.5
        try:
            canary['proc'].terminate()
            canary['proc'].wait(timeout=5)
        except Exception:
            canary['proc'].kill()
        canary['result'] = (canary['urg'].exists(), alive)
    return canary['result']


def assert_scoped(job):
    reached, alive = finish_canary(job['canary'])
    assert reached is False, 'the SIGURG interlock reached the outside canary'
    assert alive is True, 'the outside canary was killed'


# --- jobs -------------------------------------------------------------------

def make_job(tmp_path, command_src='print("HELLO", flush=True)', *, limits=None, on_disconnect='terminate',
             env_extra=None):
    sub = tmp_path / uuid.uuid4().hex[:8]
    jobdir = sub / 'job'
    home = sub / 'home'
    project = sub / 'project'
    for d in (home, project):
        d.mkdir(parents=True)
    home, project = home.resolve(), project.resolve()
    canary = start_canary(tmp_path)
    profile = sandbox_profiles.build(executable=PY, exec_paths=[FW, *READS], read_paths=READS,
                                     project_root=project, temp=home, writable_paths=[home, project],
                                     job_marker=uuid.uuid4().hex, private_deny=False)
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'TMPDIR': str(home), 'LANG': 'en_US.UTF-8',
           'BEATS': str(home), 'CANARY_URG': str(canary['urg']), **(env_extra or {})}
    plan = {'version': 1, 'argv': [str(PY), '-I', '-c', command_src], 'cwd': str(project),
            'env': {**env, 'PYTHONPATH': str(ROOT / 'backend')}, 'command_env': env, 'profile': profile,
            'launcher': PREFIX,
            'limits': {'timeout': 60, 'output_limit': 192_000, 'nofile': 4096, 'nproc': 512, **(limits or {})},
            'on_disconnect': on_disconnect, 'secret': uuid.uuid4().hex,
            'interlock_abort_path': str(canary['urg'])}
    job_shim.write_plan(jobdir, plan)
    return {'jobdir': jobdir, 'home': home, 'project': project, 'plan': plan, 'canary': canary}


class RunningShim:
    def __init__(self, job):
        self.job = job
        self.jobdir = job['jobdir']
        self.lifeline_r, self.lifeline_w = os.pipe()
        os.set_inheritable(self.lifeline_r, True)
        env = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(ROOT / 'backend'),
               'PEBBLE_SHIM_LIFELINE_FD': str(self.lifeline_r)}
        self.proc = subprocess.Popen([*PREFIX, '--job-shim', str(self.jobdir)], env=env, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     pass_fds=(self.lifeline_r,), start_new_session=True)
        os.close(self.lifeline_r)
        self.handle = job_shim.attach(self.jobdir)

    def wait_record(self, kind, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for record in self.handle.records():
                if record.get('record') == kind:
                    return record
            time.sleep(0.03)
        return None

    def wait_beat(self, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not (self.job['home'] / 'escapee.beat').exists():
            time.sleep(0.05)

    def exited(self):
        return next(r for r in self.handle.records() if r['record'] == 'exited')

    def close_lifeline(self):
        try:
            os.close(self.lifeline_w)
        except OSError:
            pass

    def kill(self):
        self.proc.send_signal(signal.SIGKILL)  # our own child, started in this test

    def wait(self, timeout=40):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()

    def cleanup(self):
        self.close_lifeline()
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        finish_canary(self.job['canary'])


def run_to_end(job, timeout=40):
    shim = RunningShim(job)
    try:
        shim.wait_record('teardown', timeout=timeout)
        shim.wait()
    finally:
        shim.cleanup()
    return shim


def age(path):
    return containment._age(Path(path), time.time())


# --- records and a clean run ------------------------------------------------

@macos
def test_records_output_and_contained_exit(tmp_path):
    job = make_job(tmp_path, 'print("HELLO", flush=True)')
    shim = run_to_end(job)
    kinds = [r['record'] for r in shim.handle.records()]
    assert kinds == ['spawning', 'spawned', 'exited', 'teardown']
    assert all(type(r['ts']) is float for r in shim.handle.records())
    exited = shim.exited()
    assert (exited['exit_code'], exited['contained'], exited['reason']) == (0, True, 'command_exit')
    assert b'HELLO' in shim.handle.read_spool(0, 65536)
    assert shim.handle.read_spool(2, 3) == b'LLO'
    assert shim.handle.settlement() == ('contained', 0)
    assert shim.handle.alive() is False  # the lock is free: the shim is gone
    assert shim.handle.status() is None  # and its control socket is closed
    assert_scoped(job)


@macos
def test_nonzero_exit_is_contained(tmp_path):
    job = make_job(tmp_path, 'raise SystemExit(5)')
    shim = run_to_end(job)
    assert (shim.exited()['exit_code'], shim.exited()['contained']) == (5, True)
    assert_scoped(job)


def test_plan_is_private_and_immutable(tmp_path):
    jobdir = job_shim.write_plan(tmp_path / 'job', {'argv': ['x'], 'env': {}, 'cwd': '/', 'profile': 'p',
                                                     'launcher': ['x'], 'limits': {}, 'secret': 's'})
    assert jobdir.stat().st_mode & 0o777 == 0o700
    assert (jobdir / 'plan.json').stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        job_shim.write_plan(jobdir, {'argv': ['y']})


@pytest.mark.parametrize('plan', [None, {'argv': ['x']}, 'not json'])
def test_shim_refuses_a_bad_plan(tmp_path, plan):
    jobdir = tmp_path / 'job'
    jobdir.mkdir(mode=0o700)
    if plan is not None:
        (jobdir / 'plan.json').write_text(plan if isinstance(plan, str) else json.dumps(plan))
    assert job_shim.main(['-c', '--job-shim', str(jobdir)]) == 2
    assert job_shim.main(['-c', '--job-shim']) == 2
    assert not (jobdir / 'records.jsonl').exists()  # nothing was spawned


def test_second_shim_refuses_a_held_lock(tmp_path):
    import fcntl
    jobdir = job_shim.write_plan(tmp_path / 'job', {'argv': ['x'], 'env': {}, 'cwd': '/', 'profile': 'p',
                                                     'launcher': ['x'], 'limits': {}, 'secret': 's'})
    fd = os.open(jobdir / 'shim.lock', os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert job_shim.attach(jobdir).alive() is True
        assert job_shim.main(['-c', '--job-shim', str(jobdir)]) == 3
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert job_shim.attach(jobdir).alive() is False


# --- containment across every trigger ---------------------------------------

@macos
@pytest.mark.parametrize('trigger', ['exit', 'cancel', 'timeout', 'shim_kill', 'host_loss'])
def test_escapee_is_swept_across_triggers(tmp_path, trigger):
    src = ESCAPEE if trigger != 'exit' else ESCAPEE.replace('time.sleep(60)\n', 'pass\n')
    # 10 s leaves the escapee time to start on a loaded machine before the deadline sweeps it.
    job = make_job(tmp_path, src, limits={'timeout': 10.0 if trigger == 'timeout' else 60})
    shim = RunningShim(job)
    try:
        shim.wait_beat()
        assert (job['home'] / 'escapee.beat').exists(), 'precondition: the escapee never started'
        if trigger == 'cancel':
            assert shim.handle.stop() == {'ok': True}
        elif trigger == 'shim_kill':
            shim.kill()  # the anchor sees lifeline EOF and sweeps on its own
        elif trigger == 'host_loss':
            shim.close_lifeline()  # on_disconnect=terminate: stop through the anchor
        if trigger != 'shim_kill':
            assert shim.wait_record('teardown') is not None
        shim.wait()
        # With no shim left to report, judge by heartbeat: the anchor's sweep
        # (after its 0.5 s SIGURG interlock) must stop the escapee.
        end = time.monotonic() + 8
        while time.monotonic() < end and (age(job['home'] / 'escapee.beat') or 0) <= 0.8:
            time.sleep(0.1)
        escapee_age = age(job['home'] / 'escapee.beat')
    finally:
        shim.cleanup()
    assert escapee_age is not None and escapee_age > 0.8, 'escapee outlived the sweep'
    assert_scoped(job)
    if trigger != 'shim_kill':
        expected = {'exit': 'command_exit', 'cancel': 'stopped', 'timeout': 'timed_out',
                    'host_loss': 'host_disconnect'}[trigger]
        assert shim.exited()['reason'] == expected
        assert shim.exited()['contained'] is True
    else:
        # A shim SIGKILL leaves no exited record: nothing claims a settlement.
        assert 'exited' not in {r['record'] for r in shim.handle.records()}
        assert shim.handle.settlement() == ('unknown', None)
        assert shim.handle.alive() is False


# --- containment lost -------------------------------------------------------

@macos
def test_anchor_killed_by_the_job_is_uncertain(tmp_path):
    # The command broadcasts SIGKILL across its own instance (sparing itself),
    # which kills the anchor, then exits shortly after. Scoping is proven with
    # SIGURG against the outside canary first, exactly as the anchor does.
    src = ("import ctypes, os, signal, sys, time\n"
           "libc = ctypes.CDLL(None, use_errno=True)\n"
           "libc.__kill.argtypes = [ctypes.c_int] * 3\n"
           "signal.signal(signal.SIGURG, signal.SIG_IGN)\n"
           "libc.__kill(-1, signal.SIGURG, 0); time.sleep(0.5)\n"
           "if os.path.exists(os.environ['CANARY_URG']):\n"
           "    sys.exit(3)\n"
           "for _ in range(3):\n"
           "    libc.__kill(-1, signal.SIGKILL, 0); time.sleep(0.1)\n"
           "time.sleep(2)\n")
    job = make_job(tmp_path, src, limits={'timeout': 20})
    shim = run_to_end(job)
    exited = shim.exited()
    assert exited['contained'] is False and exited['reason'] == 'containment_lost'
    assert shim.handle.settlement() == ('unknown', None)
    assert_scoped(job)


# --- governors --------------------------------------------------------------

@macos
def test_output_spool_limit(tmp_path):
    src = ("import os, sys, time\n"
           "sys.stdout.buffer.write(b'x' * 1_000_000); sys.stdout.flush()\n"
           "open(os.path.join(os.environ['BEATS'], 'after.txt'), 'w').write('after')\n"
           "time.sleep(30)\n")
    job = make_job(tmp_path, src, limits={'output_limit': 5000, 'timeout': 30})
    shim = run_to_end(job)
    assert shim.handle.read_spool(0, 10_000_000) == b'x' * 5000
    exited = shim.exited()
    assert exited['reason'] == 'output_limited' and exited['output_bytes'] == 5000
    assert exited['dropped_bytes'] > 0
    assert not (job['home'] / 'after.txt').exists()
    assert_scoped(job)


@macos
def test_rlimit_nofile(tmp_path):
    src = ("import os\n"
           "held = []\n"
           "try:\n"
           "    while True:\n"
           "        held.append(os.open('/dev/null', os.O_RDONLY))\n"
           "except OSError:\n"
           "    print('NOFILE', len(held), flush=True)\n")
    job = make_job(tmp_path, src, limits={'nofile': 48, 'timeout': 20})
    shim = run_to_end(job)
    out = shim.handle.read_spool(0, 65536).decode('utf-8', 'replace')
    found = re.search(r'NOFILE (\d+)', out)
    assert found and int(found.group(1)) <= 48, out
    assert_scoped(job)


@macos
def test_rlimit_fsize(tmp_path):
    src = ("import os, signal\n"
           "signal.signal(signal.SIGXFSZ, signal.SIG_IGN)\n"
           "path = os.path.join(os.environ['BEATS'], 'big.bin')\n"
           "try:\n"
           "    with open(path, 'wb') as f:\n"
           "        for _ in range(200):\n"
           "            f.write(b'x' * 65536); f.flush()\n"
           "    print('WROTE_ALL', flush=True)\n"
           "except OSError:\n"
           "    print('FSIZE_REFUSED', flush=True)\n")
    job = make_job(tmp_path, src, limits={'fsize_bytes': 500_000, 'timeout': 20})
    shim = run_to_end(job)
    out = shim.handle.read_spool(0, 65536).decode('utf-8', 'replace')
    assert 'FSIZE_REFUSED' in out and 'WROTE_ALL' not in out, out
    assert (job['home'] / 'big.bin').stat().st_size <= 500_000
    assert_scoped(job)


@macos
def test_rlimit_nproc_bounds_a_fork_bomb(tmp_path):
    src = ("import os, time\n"
           "n = 0\n"
           "try:\n"
           "    for _ in range(300):\n"
           "        if os.fork() == 0:\n"
           "            time.sleep(20); os._exit(0)\n"
           "        n += 1\n"
           "except OSError:\n"
           "    print('NPROC_STOP', n, flush=True)\n")
    job = make_job(tmp_path, src, limits={'nproc': 8, 'timeout': 20})
    shim = run_to_end(job)
    out = shim.handle.read_spool(0, 65536).decode('utf-8', 'replace')
    found = re.search(r'NPROC_STOP (\d+)', out)
    # Per-UID NPROC is approximate (current count + N); the bomb is still bounded.
    assert found and int(found.group(1)) < 300, out
    assert shim.exited()['contained'] is True  # the sleeping children were swept
    assert_scoped(job)


@macos
def test_rlimit_cpu(tmp_path):
    # Jobs run at Darwin background priority, so under load 1 CPU-second can take tens of wall seconds
    # (26.6 s in Phase 14 trial 4). The decisive proof is the exit: a CPU-limit kill, not the timeout.
    job = make_job(tmp_path, 'x = 0\nwhile True:\n    x += 1\n', limits={'cpu_seconds': 1, 'timeout': 60})
    began = time.monotonic()
    shim = run_to_end(job, timeout=90)
    elapsed = time.monotonic() - began
    exited = shim.exited()
    assert elapsed < 60  # stopped before its wall-clock timeout
    assert exited['reason'] == 'command_exit'  # a timeout would record 'timed_out'
    assert exited['exit_code'] in (-signal.SIGXCPU, -signal.SIGKILL), exited
    assert_scoped(job)


@macos
def test_memory_budget_is_enforced(tmp_path):
    src = ("import time\n"
           "blocks = []\n"
           "for _ in range(400):\n"
           "    blocks.append(bytearray(8_000_000)); time.sleep(0.02)\n"
           "print('NEVER_STOPPED', flush=True)\n"
           "time.sleep(30)\n")
    budget = 200_000_000
    job = make_job(tmp_path, src, limits={'memory_bytes': budget, 'timeout': 40})
    shim = run_to_end(job, timeout=60)
    exited = shim.exited()
    assert exited['reason'] == 'memory', exited
    assert exited['contained'] is True
    assert b'NEVER_STOPPED' not in shim.handle.read_spool(0, 65536)
    # The governor samples every 150 ms, so it overshoots by up to one interval
    # of allocation (here about 8 MB per 20 ms); record what was observed.
    overshoot = exited['peak_memory_bytes'] - budget
    assert 0 < overshoot < 200_000_000, exited
    assert_scoped(job)


@macos
def test_footprint_reads_a_plausible_value_for_this_process():
    # Pins RI_PHYS_FOOTPRINT_OFFSET: a wrong field reads a timestamp or zero.
    own = job_shim._footprint(os.getpid())
    assert 1_000_000 < own < 64_000_000_000, own
    ballast = bytearray(64_000_000)
    ballast[::4096] = b'x' * len(ballast[::4096])  # touch every page
    assert job_shim._footprint(os.getpid()) > own + 32_000_000
    assert job_shim._footprint_tree(os.getpid()) >= job_shim._footprint(os.getpid())
    del ballast


# --- reattach ---------------------------------------------------------------

@macos
def test_attach_continue_keeps_running_then_settles(tmp_path):
    job = make_job(tmp_path, ESCAPEE, limits={'timeout': 6.0}, on_disconnect='continue')
    shim = RunningShim(job)
    try:
        shim.wait_beat()
        shim.close_lifeline()  # the "host" dies
        time.sleep(1.0)
        reattached = job_shim.attach(job['jobdir'])  # a restarted host
        assert reattached.alive() is True
        status = reattached.status()
        assert status['ok'] is True and status['exit_seen'] is False
        assert age(job['home'] / 'escapee.beat') < 1.0  # still running under 'continue'
        assert {'record': 'host_disconnect', 'action': 'continue'}.items() <= next(
            r for r in reattached.records() if r['record'] == 'host_disconnect').items()
        assert shim.wait_record('teardown') is not None  # ran to the deadline, then stopped
        shim.wait()
        time.sleep(1.0)
        assert age(job['home'] / 'escapee.beat') > 0.8
    finally:
        shim.cleanup()
    assert reattached.alive() is False
    assert reattached.settlement() == ('contained', -signal.SIGKILL)  # the leader, stopped by the sweep
    assert shim.exited()['reason'] == 'timed_out'
    assert_scoped(job)


@macos
def test_attach_stop_ends_a_continued_job(tmp_path):
    job = make_job(tmp_path, ESCAPEE, limits={'timeout': 60}, on_disconnect='continue')
    shim = RunningShim(job)
    try:
        shim.wait_beat()
        shim.close_lifeline()
        time.sleep(0.5)
        assert job_shim.attach(job['jobdir']).stop() == {'ok': True}
        assert shim.wait_record('teardown') is not None
        shim.wait()
        time.sleep(1.0)
        assert age(job['home'] / 'escapee.beat') > 0.8
    finally:
        shim.cleanup()
    assert shim.exited()['reason'] == 'stopped' and shim.exited()['contained'] is True
    assert_scoped(job)


@macos
def test_control_socket_requires_the_secret(tmp_path):
    job = make_job(tmp_path, ESCAPEE, limits={'timeout': 20})
    shim = RunningShim(job)
    try:
        assert shim.wait_record('spawned') is not None
        sock_path = job_shim.control_path(job['jobdir'])  # per-user private folder (macOS sun_path limit)
        assert sock_path.stat().st_mode & 0o777 == 0o600
        for request in (b'{"secret":"wrong","op":"stop"}\n', b'not json\n', b'{"op":"stop"}\n'):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3.0)
                sock.connect(str(sock_path))
                sock.sendall(request)
                assert json.loads(sock.recv(4096).split(b'\n')[0]) == {'ok': False, 'error': 'unauthorized'}
        assert shim.handle.status()['stopping'] is False  # none of those stopped it
        assert shim.handle._control('reboot') == {'ok': False, 'error': 'bad_op'}
        assert shim.handle.stop() == {'ok': True}
        shim.wait_record('teardown')
        shim.wait()
    finally:
        shim.cleanup()
    assert_scoped(job)


# --- run_async_command through the shim path --------------------------------

@pytest.fixture
def scoped_plans(tmp_path, monkeypatch):
    """Give each shim plan written by run_async_command an outside canary interlock."""
    canaries = []
    real = job_shim.write_plan

    def write_plan(jobdir, plan):
        canary = start_canary(tmp_path)
        canaries.append(canary)
        return real(jobdir, {**plan, 'interlock_abort_path': str(canary['urg'])})

    monkeypatch.setattr(job_shim, 'write_plan', write_plan)
    yield canaries
    for canary in canaries:
        assert finish_canary(canary) == (False, True)


def shim_payload(tmp_path, source, command='python3 probe.py', writable=False):
    project = tmp_path / 'proj'
    project.mkdir()
    (project / 'probe.py').write_text(source)
    return {**inspect_command(command, project.resolve(), False), 'writable': writable}


@macos
def test_run_async_command_shim_path_succeeds(tmp_path, scoped_plans):
    payload = shim_payload(tmp_path, "print('SHIMMED', flush=True)\n")
    started, chunks = [], []
    result = async_terminal.run_async_command(
        payload, threading.Event(), started.append, chunks.append, timeout=60, launcher=(PREFIX, READS))
    assert result == {'state': 'succeeded', 'exit_code': 0, 'outcome_uncertain': False,
                      'isolation': 'macOS sandbox · shim+anchor · selected project · network denied'}
    assert b'SHIMMED' in b''.join(chunks)
    assert len(started) == 1 and set(started[0]) == {'pid', 'isolation'} and started[0]['pid'] > 0
    assert len(scoped_plans) == 1


@macos
def test_run_async_command_shim_path_failure_and_cancel(tmp_path, scoped_plans):
    payload = shim_payload(tmp_path, "import time\nprint('GO', flush=True)\ntime.sleep(60)\n")
    cancel, chunks = threading.Event(), []

    def on_output(chunk):
        chunks.append(chunk)
        if b'GO' in b''.join(chunks):
            cancel.set()

    began = time.monotonic()
    result = async_terminal.run_async_command(payload, cancel, lambda info: None, on_output, timeout=60,
                                              launcher=(PREFIX, READS))
    assert time.monotonic() - began < 30
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('cancelled', -signal.SIGKILL, True)


@macos
def test_run_async_command_shim_path_timeout(tmp_path, scoped_plans):
    payload = shim_payload(tmp_path, "import time\ntime.sleep(60)\n")
    result = async_terminal.run_async_command(payload, threading.Event(), lambda info: None, lambda c: None,
                                              timeout=1.5, launcher=(PREFIX, READS))
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('timed_out', -signal.SIGKILL, True)


@macos
@pytest.mark.parametrize('stage', ['on_started', 'on_output'])
def test_run_async_command_shim_path_callback_failure_stops_as_unknown(tmp_path, scoped_plans, stage):
    payload = shim_payload(tmp_path, "import time\nprint('GO', flush=True)\ntime.sleep(60)\n")

    def broken(arg):
        raise RuntimeError('ledger unavailable')

    began = time.monotonic()
    result = async_terminal.run_async_command(
        payload, threading.Event(), broken if stage == 'on_started' else (lambda info: None),
        broken if stage == 'on_output' else (lambda chunk: None), timeout=60, launcher=(PREFIX, READS))
    assert time.monotonic() - began < 30
    assert (result['state'], result['outcome_uncertain']) == ('unknown', True)


@macos
def test_run_async_command_shim_path_base_exception_propagates_after_teardown(tmp_path, scoped_plans):
    payload = shim_payload(tmp_path, "import time\nprint('GO', flush=True)\ntime.sleep(60)\n")

    def stopping(chunk):
        raise SystemExit('manager is stopping')

    began = time.monotonic()
    with pytest.raises(SystemExit):
        async_terminal.run_async_command(payload, threading.Event(), lambda info: None, stopping, timeout=60,
                                         launcher=(PREFIX, READS))
    assert time.monotonic() - began < 30


@macos
def test_run_async_command_shim_path_folder_drift_is_unknown(tmp_path, scoped_plans, monkeypatch):
    real, seen = async_terminal._identity, []

    def drifting(root):
        seen.append(root)
        return real(root) if len(seen) == 1 else (0, 0)

    monkeypatch.setattr(async_terminal, '_identity', drifting)
    payload = shim_payload(tmp_path, "import time\nprint('GO', flush=True)\ntime.sleep(60)\n")
    result = async_terminal.run_async_command(payload, threading.Event(), lambda info: None, lambda c: None,
                                              timeout=60, launcher=(PREFIX, READS))
    assert len(seen) == 2
    assert (result['state'], result['outcome_uncertain']) == ('unknown', True)


@macos
def test_command_exec_refused_inside_the_sandbox_is_a_certain_failure(tmp_path):
    # The command lives in the writable home: exec is denied there, so the
    # anchor's exec-status pipe proves it never ran (127), and the sweep is clean.
    job = make_job(tmp_path)
    planted = job['home'] / 'planted'
    planted.write_bytes(Path('/bin/echo').read_bytes())
    planted.chmod(0o755)
    plan = json.loads((job['jobdir'] / 'plan.json').read_text())
    (job['jobdir'] / 'plan.json').unlink()
    job_shim.write_plan(job['jobdir'], {**plan, 'argv': [str(planted), 'never']})
    shim = run_to_end(job)
    exited = shim.exited()
    assert (exited['reason'], exited['exit_code'], exited['contained']) == ('anchor_spawn_failed', 127, True)
    assert b'never' not in shim.handle.read_spool(0, 65536)
    assert_scoped(job)


@pytest.mark.parametrize('records, never', [
    ([{'record': 'spawning'}, {'record': 'exited', 'reason': 'anchor_spawn_failed:boom'}], True),
    ([{'record': 'spawning'}], False),  # B3: spawning without proof of either outcome
    ([{'record': 'spawning'}, {'record': 'spawned'}, {'record': 'exited', 'reason': 'anchor_spawn_failed'}], False),
    ([], False),
])
def test_never_ran_requires_exec_status_proof(records, never):
    assert async_terminal._never_ran(records) is never


def test_shim_result_mapping():
    class Handle:
        def __init__(self, exited):
            self.rows = [{'record': 'spawned'}, {'record': 'exited', **exited}]

        def records(self):
            return self.rows

    out = async_terminal._Output(lambda c: None, 10)
    label = 'iso'
    cases = [
        ({'reason': 'command_exit', 'exit_code': 0, 'contained': True}, ('succeeded', 0, False)),
        ({'reason': 'command_exit', 'exit_code': 3, 'contained': True}, ('failed', 3, False)),
        ({'reason': 'command_exit', 'exit_code': 0, 'contained': False}, ('unknown', 0, True)),
        ({'reason': 'stopped', 'exit_code': -9, 'contained': True}, ('cancelled', -9, True)),
        ({'reason': 'timed_out', 'exit_code': -9, 'contained': True}, ('timed_out', -9, True)),
        ({'reason': 'output_limited', 'exit_code': -9, 'contained': True}, ('output_limited', -9, True)),
        ({'reason': 'memory', 'exit_code': -9, 'contained': True}, ('unknown', -9, True)),
        ({'reason': 'containment_lost', 'exit_code': None, 'contained': False}, ('unknown', None, True)),
        ({'reason': 'anchor_spawn_failed', 'exit_code': 127, 'contained': True}, ('failed', 127, False)),
    ]
    for exited, (state, code, uncertain) in cases:
        result = async_terminal._shim_result(Handle(exited), label, True, out, False)
        assert (result['state'], result['exit_code'], result['outcome_uncertain']) == (state, code, uncertain), exited
    tainted = async_terminal._shim_result(Handle(cases[0][0]), label, True, out, True)
    assert (tainted['state'], tainted['outcome_uncertain']) == ('unknown', True)
    assert async_terminal._shim_result(Handle(cases[0][0]), label, False, out, False)['state'] == 'unknown'


@macos
def test_run_async_command_keeps_a_caller_jobdir_the_job_cannot_read(tmp_path, scoped_plans):
    jobdir = tmp_path / 'jobs' / 'job-1'
    probe = ("import os\n"
             f"jobdir = {str(jobdir)!r}\n"
             "for label, action in (('list', lambda: os.listdir(jobdir)),\n"
             "                      ('plan', lambda: open(os.path.join(jobdir, 'plan.json')).read()),\n"
             "                      ('records', lambda: open(os.path.join(jobdir, 'records.jsonl')).read())):\n"
             "    try:\n"
             "        action(); print('LEAK', label, flush=True)\n"
             "    except OSError:\n"
             "        print('DENIED', label, flush=True)\n"
             "open(os.path.join(os.environ['HOME'], 'mine.txt'), 'w').write('home is writable')\n")
    payload = shim_payload(tmp_path, probe)
    chunks = []
    result = async_terminal.run_async_command(payload, threading.Event(), lambda info: None, chunks.append,
                                              timeout=60, jobdir=jobdir, launcher=(PREFIX, READS))
    text = b''.join(chunks).decode()
    assert result['state'] == 'succeeded', (result, text)
    assert 'LEAK' not in text and text.count('DENIED') == 3, text
    # The caller's jobdir is retained: a later host reattaches to its evidence.
    handle = job_shim.attach(jobdir)
    assert handle.alive() is False and handle.settlement() == ('contained', 0)
    assert jobdir.with_name('job-1-home').joinpath('mine.txt').read_text() == 'home is writable'


def test_run_async_command_shim_path_pre_spawn_cancel_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(async_terminal, '_supported', lambda: True)
    monkeypatch.setattr(async_terminal.subprocess, 'Popen', lambda *a, **k: pytest.fail('spawned'))
    payload = shim_payload(tmp_path, "print('x')\n")
    cancel = threading.Event()
    cancel.set()
    calls = []
    result = async_terminal.run_async_command(payload, cancel, calls.append, calls.append,
                                              launcher=(PREFIX, READS))
    assert result['state'] == 'cancelled' and result['outcome_uncertain'] is False and calls == []


def test_run_async_command_refuses_a_bad_on_disconnect(tmp_path):
    payload = shim_payload(tmp_path, "print('x')\n")
    with pytest.raises(Exception, match='on_disconnect'):
        async_terminal.run_async_command(payload, threading.Event(), print, print,
                                         launcher=(PREFIX, READS), on_disconnect='linger')


# --- no stored-PID signalling -----------------------------------------------

SIGNAL_CALLS = {'kill', 'killpg', '__kill', 'send_signal', 'terminate', 'pidfd_send_signal'}


def test_new_modules_never_signal_a_stored_pid():
    """The only signal call in the new modules is the anchor's kill(-1) broadcast.

    No os.kill, killpg, send_signal or terminate appears in job_shim.py or
    sandbox_profiles.py at all, and containment.py's only one targets the
    literal -1 (the sandbox-instance broadcast). Canary helpers used only by
    self_test terminate a child they started in the same call, by handle.
    """
    for name in ('job_shim', 'sandbox_profiles', 'containment'):
        tree = ast.parse((ROOT / 'backend' / 'pebble' / (name + '.py')).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
            if called not in SIGNAL_CALLS:
                continue
            if called == '__kill':
                target = node.args[0]
                assert isinstance(target, ast.UnaryOp) and isinstance(target.op, ast.USub) \
                    and isinstance(target.operand, ast.Constant) and target.operand.value == 1, \
                    f'{name}: __kill target is not the -1 broadcast'
            elif called in ('kill', 'terminate') and name == 'containment' and isinstance(func, ast.Attribute) \
                    and isinstance(func.value, ast.Name) and func.value.id in ('canary', 'proc'):
                continue  # self_test's own live child, by handle
            else:
                pytest.fail(f'{name}: unexpected signal call {called} at line {node.lineno}')
