"""Sandbox-instance containment: the anchor sweep, the startup canary and the launcher.

Signal safety: every test that reaches a kill(-1) broadcast runs it only inside
the fresh sandbox instance that the test's own anchor created, and the anchor
proves scoping with SIGURG (interlock_abort_path) against an outside canary
before any SIGKILL. No test signals a PID it did not create in that test.
"""
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from pebble import containment, sandbox_profiles
from pebble.policy import PolicyError

ROOT = Path(__file__).resolve().parents[2]  # project root; ROOT/backend holds the package
PY = Path(sys.executable).resolve()
FW = Path('/Library/Frameworks/Python.framework')
FRAMEWORK = FRAMEWORK_READS = [ROOT / 'backend', Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), FW]
# Mirrors backend/main.py: the launcher prefix serves both the anchor and the self-test's outside canary.
BOOT = ('import sys; from pebble import containment; sys.exit(containment.canary_main(sys.argv) '
        "if sys.argv[1:2] == ['--job-canary'] else containment.anchor_main(sys.argv))")

macos = pytest.mark.skipif(sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
                           reason='actual macOS confinement')


def make_launcher():
    # A test launcher that dispatches anchor_main from the checkout.
    return [str(PY), '-c', BOOT], FRAMEWORK_READS


class Anchored:
    """Runs one anchor in a fresh sandbox instance; collects status + output."""

    def __init__(self, returncode, status, output, beats, stderr=b'', canary_reached=None, canary_alive=None):
        self.returncode = returncode
        self.status = status
        self.output = output
        self.beats = beats
        self.stderr = stderr
        self.canary_reached = canary_reached  # the SIGURG interlock reached the outside canary
        self.canary_alive = canary_alive      # the outside canary's heartbeat stayed fresh

    def message(self, event, phase=None):
        for m in self.status:
            if m.get('event') == event and (phase is None or m.get('phase') == phase):
                return m
        return None

    def age(self, name, now=None):
        return containment._age(self.beats / (name + '.beat'), now or time.time())


def run_anchor(tmp_path, *, reviewed, executable, exec_paths, read_paths, timeout=None, writable=(),
               stop_after=None, close_lifeline_after=None, env_extra=None, stop_on=None):
    """Launch the anchor with `reviewed` as the command; drive the lifeline; return Anchored.

    Every anchor sweeps with kill(-1), so every run gets its own outside canary
    and the anchor's SIGURG interlock checks that canary's receipt file.
    """
    canary = start_outside_canary(tmp_path)
    try:
        run = _run_anchor(tmp_path, reviewed, executable, exec_paths, read_paths, timeout, writable, stop_after,
                          close_lifeline_after, env_extra, Path(str(canary['beat']) + '.urg'), stop_on)
    finally:
        alive = stop_outside_canary(canary)
    run.canary_alive = alive
    run.canary_reached = Path(str(canary['beat']) + '.urg').exists()
    return run


def _run_anchor(tmp_path, reviewed, executable, exec_paths, read_paths, timeout, writable, stop_after,
                close_lifeline_after, env_extra, abort_path, stop_on):
    beats = (tmp_path / ('beats-' + uuid.uuid4().hex[:8]))
    beats.mkdir()
    beats = beats.resolve()
    project = (tmp_path / ('proj-' + uuid.uuid4().hex[:8]))
    project.mkdir()
    project = project.resolve()
    marker = uuid.uuid4().hex
    exec_all = list(dict.fromkeys([FW, *exec_paths]))  # the launcher python re-execs inside the framework
    profile = sandbox_profiles.build(executable=executable, exec_paths=exec_all,
                                     read_paths=list(read_paths), project_root=project, temp=beats,
                                     writable_paths=[beats, *writable], job_marker=marker, private_deny=False)
    spec = {'mode': 'command', 'argv': reviewed, 'cwd': str(project), 'heartbeat_dir': str(beats),
            'interlock_abort_path': str(abort_path),
            'env': {'PATH': '/usr/bin:/bin', 'HOME': str(beats), 'TMPDIR': str(beats), 'LANG': 'en_US.UTF-8',
                    'PYTHONPATH': str(ROOT / 'backend'), 'BEATS': str(beats), **(env_extra or {})}}
    if timeout is not None:
        spec['timeout'] = timeout
    prefix, _ = make_launcher()

    lifeline_r, lifeline_w = os.pipe()
    status_r, status_w = os.pipe()
    out_r, out_w = os.pipe()
    for fd in (lifeline_r, status_w, out_w):
        os.set_inheritable(fd, True)
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(beats), 'TMPDIR': str(beats), 'LANG': 'en_US.UTF-8',
           'PYTHONPATH': str(ROOT / 'backend'), 'PEBBLE_ANCHOR_SPEC_FD': str(lifeline_r),
           'PEBBLE_ANCHOR_STATUS_FD': str(status_w), 'PEBBLE_ANCHOR_OUT_FD': str(out_w)}
    argv = ['/usr/bin/sandbox-exec', '-p', profile, *prefix, '--job-anchor', str(beats)]
    # The test holds the lifeline write end, as the shim does; closing it is
    # the anchor's view of a dead shim.
    holder = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, pass_fds=(lifeline_r, status_w, out_w),
                              start_new_session=True, close_fds=True)
    os.close(lifeline_r)
    os.close(status_w)
    os.close(out_w)
    body = json.dumps(spec).encode()
    os.write(lifeline_w, ('%d\n' % len(body)).encode() + body)
    output = bytearray()
    os.set_blocking(out_r, False)
    os.set_blocking(status_r, False)
    status = bytearray()
    started = time.monotonic()
    stopped = closed = False
    while True:
        try:
            chunk = os.read(out_r, 65536)
            if chunk:
                output += chunk
        except (BlockingIOError, OSError):
            pass
        try:
            s = os.read(status_r, 65536)
            if s:
                status += s
        except (BlockingIOError, OSError):
            pass
        now = time.monotonic()
        due = stop_after is not None and now - started >= stop_after or stop_on is not None and stop_on in output
        if due and not stopped and not closed:
            os.write(lifeline_w, b'\x00')
            stopped = True
        if close_lifeline_after is not None and not closed and (
                now - started >= close_lifeline_after if type(close_lifeline_after) is float
                else close_lifeline_after in output):
            os.close(lifeline_w)
            closed = True
        if holder.poll() is not None and now - started > 0.2:
            break
        if now - started > 40:
            holder.kill()
            break
        time.sleep(0.02)
    if not closed:
        try:
            os.close(lifeline_w)
        except OSError:
            pass
    holder.wait(timeout=10)
    # Drain the rest.
    for fd, sink in ((out_r, output), (status_r, status)):
        while True:
            try:
                chunk = os.read(fd, 65536)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            sink += chunk
    os.close(out_r)
    os.close(status_r)
    err = holder.stderr.read() if holder.stderr else b''
    msgs = [json.loads(line) for line in status.decode('utf-8', 'replace').splitlines() if line.strip()]
    return Anchored(holder.returncode, msgs, bytes(output), beats, err)


# --- launcher ---------------------------------------------------------------

def test_launcher_source_shape():
    prefix, reads = containment.launcher(False)
    assert prefix == [sys.executable, str(ROOT / 'backend' / 'main.py')]
    assert (ROOT / 'backend') in [Path(r) for r in reads]


def test_launcher_frozen_shape(monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(Path(sys.executable).parent), raising=False)
    prefix, reads = containment.launcher(True)
    assert prefix == [str(Path(sys.executable).resolve())]
    assert Path(sys.executable).resolve().parent in [Path(r) for r in reads]


# --- anchor: a normal command, output and a clean sweep ---------------------

def assert_scoped(run):
    # The SIGURG interlock ran first and the outside canary neither received it
    # nor lost its heartbeat to the SIGKILL sweep.
    sweep = run.message('sweep', 'complete')
    assert sweep is not None and sweep['reason'] != 'interlock_refused', run.status
    assert run.canary_reached is False, 'SIGURG interlock reached the outside canary'
    assert run.canary_alive is True, 'the outside canary was killed'


@macos
def test_anchor_streams_output_and_sweeps_on_exit(tmp_path):
    reviewed = [str(PY), '-I', '-c', "import sys; sys.stdout.write('HELLO\\n'); sys.stdout.flush()"]
    run = run_anchor(tmp_path, reviewed=reviewed, executable=PY, exec_paths=[], read_paths=FRAMEWORK_READS)
    assert run.returncode == 0, run.stderr.decode('utf-8', 'replace')[:1500]
    assert b'HELLO' in run.output, run.stderr.decode('utf-8', 'replace')[:1500]
    assert run.message('exit')['reason'] == 'command_exit'
    assert run.message('exit')['code'] == 0
    assert run.message('sweep', 'starting') is not None
    assert run.message('sweep', 'complete')['thorough'] is True
    assert_scoped(run)


@macos
def test_anchor_reports_nonzero_exit(tmp_path):
    reviewed = [str(PY), '-I', '-c', 'raise SystemExit(7)']
    run = run_anchor(tmp_path, reviewed=reviewed, executable=PY, exec_paths=[], read_paths=FRAMEWORK_READS)
    assert run.message('exit')['code'] == 7
    assert_scoped(run)


@macos
def test_anchor_reports_an_unstartable_command(tmp_path):
    # An executable outside every exec rule: the exec-status pipe reports it.
    planted = tmp_path / 'planted'
    planted.write_bytes(Path('/bin/echo').read_bytes())
    planted.chmod(0o755)
    run = run_anchor(tmp_path, reviewed=[str(planted)], executable=PY, exec_paths=[], read_paths=FRAMEWORK_READS)
    assert run.message('exit')['reason'] == 'anchor_spawn_failed'
    assert_scoped(run)


# --- the escapee matrix: each must be gone after the sweep ------------------

ESCAPEES = {
    'setsid_double_fork': (
        "import os, sys\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork() == 0:\n"
        "        os.execv(sys.executable, [sys.executable, '-I', '-c', BEAT, 'escapee'])\n"
        "    os._exit(0)\n"
        "os.wait()\n"),
    'setpgid': (
        "import os, sys\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setpgid(0, 0)\n"
        "    os.execv(sys.executable, [sys.executable, '-I', '-c', BEAT, 'escapee'])\n"),
    'posix_spawn_setsid': (
        "import os, sys\n"
        "attr = os.posix_spawn(sys.executable, [sys.executable, '-I', '-c', BEAT, 'escapee'], os.environ,\n"
        "                      setsid=True)\n"),
    'nohup': (
        "import os, subprocess, sys\n"
        "subprocess.Popen(['/usr/bin/nohup', sys.executable, '-I', '-c', BEAT, 'escapee'],\n"
        "                 stdout=open(os.path.join(os.environ['BEATS'], 'nohup.out'), 'w'),\n"
        "                 stderr=subprocess.STDOUT, env=dict(os.environ))\n"),
    'signal_ignorer': (
        "import os, sys\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    os.execv(sys.executable, [sys.executable, '-I', '-c',\n"
        "        'import signal,sys;'\n"
        "        'signal.signal(signal.SIGTERM, signal.SIG_IGN);'\n"
        "        'signal.signal(signal.SIGHUP, signal.SIG_IGN);' + BEAT, 'escapee'])\n"),
    'closed_fds': (
        "import os, sys\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    os.closerange(0, 3)\n"
        "    os.execv(sys.executable, [sys.executable, '-I', '-c', BEAT, 'escapee'])\n"),
}

BEAT_SRC = (
    "import os, time\n"
    "p = os.path.join(os.environ['BEATS'], 'escapee.beat')\n"
    "end = time.time() + 25\n"
    "while time.time() < end:\n"
    "    open(p + '.t', 'w').write(str(time.time())); os.replace(p + '.t', p); time.sleep(0.05)\n")


def escapee_command(body):
    header = "BEAT = %r\n" % BEAT_SRC
    # SPAWNED only once the escapee has really beaten, so triggers keyed to it
    # never race the escapee's start-up.
    waiter = (
        "import os, time\n"
        "for _ in range(1500):\n"
        "    if os.path.exists(os.path.join(os.environ['BEATS'], 'escapee.beat')):\n"
        "        print('SPAWNED', flush=True)\n"
        "        break\n"
        "    time.sleep(0.01)\n"
        "else:\n"
        "    print('NOBEAT', flush=True)\n")
    return [str(PY), '-I', '-c', header + body + waiter + "time.sleep(60)\n"]


@macos
@pytest.mark.parametrize('name', sorted(ESCAPEES))
@pytest.mark.parametrize('trigger', ['exit', 'cancel', 'timeout'])
def test_escapee_is_swept(tmp_path, name, trigger):
    cmd = escapee_command(ESCAPEES[name])
    kw = dict(reviewed=cmd, executable=PY, exec_paths=[], read_paths=FRAMEWORK_READS)
    if trigger == 'exit':
        kw['reviewed'] = cmd[:-1] + [cmd[-1].replace('time.sleep(60)\n', 'pass\n')]
    elif trigger == 'cancel':
        kw['stop_on'] = b'SPAWNED'
    elif trigger == 'timeout':
        kw['timeout'] = 4.0
    run = run_anchor(tmp_path, **kw)
    assert b'SPAWNED' in run.output, 'escapee never ran: ' + run.output.decode('utf-8', 'replace')[:300]
    assert_scoped(run)
    expected = {'exit': 'command_exit', 'cancel': 'stop_requested', 'timeout': 'deadline'}[trigger]
    assert run.message('exit')['reason'] == expected
    time.sleep(0.8)
    escapee_age = run.age('escapee')
    assert escapee_age is not None and escapee_age > 0.7, 'escapee outlived the sweep'


@macos
def test_lifeline_eof_triggers_sweep(tmp_path):
    cmd = escapee_command(ESCAPEES['setsid_double_fork'])
    run = run_anchor(tmp_path, reviewed=cmd, executable=PY, exec_paths=[], read_paths=FRAMEWORK_READS,
                     close_lifeline_after=b'SPAWNED')
    assert run.message('exit')['reason'] == 'lifeline_eof'
    assert_scoped(run)
    time.sleep(0.8)
    assert run.age('escapee') > 0.7


# --- a second job's canary (a different instance) survives ------------------

@macos
def test_other_job_survives_the_sweep(tmp_path):
    other = run_other_job(tmp_path)
    try:
        cmd = escapee_command(ESCAPEES['setsid_double_fork'])
        run = run_anchor(tmp_path, reviewed=cmd, executable=PY, exec_paths=[], read_paths=FRAMEWORK_READS,
                         stop_on=b'SPAWNED')
        assert b'SPAWNED' in run.output
        assert_scoped(run)
        time.sleep(0.8)
        now = time.time()
        assert run.age('escapee', now) > 0.7
        assert containment._age(Path(str(other['beat']) + '.beat'), now) < 1.0, 'the other job was swept'
    finally:
        other['proc'].terminate()
        try:
            other['proc'].wait(timeout=5)
        except Exception:
            other['proc'].kill()


def run_other_job(tmp_path):
    """A second, independent sandbox instance that heartbeats throughout."""
    beats = (tmp_path / ('other-' + uuid.uuid4().hex[:8]))
    beats.mkdir()
    beats = beats.resolve()
    project = (tmp_path / ('otherproj-' + uuid.uuid4().hex[:8]))
    project.mkdir()
    project = project.resolve()
    profile = sandbox_profiles.build(executable=PY, exec_paths=[FW], read_paths=FRAMEWORK_READS,
                                     project_root=project, temp=beats, writable_paths=[beats],
                                     job_marker=uuid.uuid4().hex, private_deny=False)
    beat = beats / 'job'
    script = ("import os, time\n"
              "p = %r\n"
              "end = time.time() + 40\n"
              "while time.time() < end:\n"
              "    open(p + '.t', 'w').write(str(time.time())); os.replace(p + '.t', p + '.beat'); time.sleep(0.05)\n" % str(beat))
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(beats), 'TMPDIR': str(beats), 'PYTHONPATH': str(ROOT / 'backend')}
    proc = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile, str(PY), '-I', '-c', script],
                            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(200):
        if Path(str(beat) + '.beat').exists():
            break
        time.sleep(0.02)
    return {'proc': proc, 'beat': beat}


def start_outside_canary(tmp_path):
    beat = (tmp_path / ('canary-' + uuid.uuid4().hex[:8]))
    script = ("import os, signal, sys, time\n"
              "signal.signal(signal.SIGURG, lambda *a: open(sys.argv[1] + '.urg', 'a').write('x'))\n"
              "end = time.time() + 40\n"
              "while time.time() < end:\n"
              "    open(sys.argv[1] + '.t', 'w').write(str(time.time())); os.replace(sys.argv[1] + '.t', sys.argv[1] + '.beat'); time.sleep(0.05)\n")
    proc = subprocess.Popen([str(PY), '-I', '-c', script, str(beat)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(200):
        if Path(str(beat) + '.beat').exists():
            break
        time.sleep(0.02)
    return {'proc': proc, 'beat': beat}


def stop_outside_canary(canary):
    beat = Path(str(canary['beat']) + '.beat')
    alive = beat.exists() and (time.time() - float(beat.read_text())) < 1.5
    try:
        canary['proc'].terminate()
        canary['proc'].wait(timeout=5)
    except Exception:
        canary['proc'].kill()
    return alive


# --- startup canary ---------------------------------------------------------

@macos
def test_self_test_reports_contained(tmp_path, monkeypatch):
    monkeypatch.setattr(containment, 'launcher', lambda frozen: make_launcher())
    result = containment.self_test(tmp_path / 'home')
    assert set(result) == {'contained', 'reason', 'elapsed'}
    assert result['contained'] is True, result
    assert result['reason'] == 'escapees_contained'
    assert result['elapsed'] < 30


def test_self_test_fails_closed_off_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(containment.sys, 'platform', 'win32')
    result = containment.self_test(tmp_path / 'home')
    assert result['contained'] is False and result['reason'] == 'unsupported_platform'


def test_self_test_fails_closed_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(containment, 'launcher', lambda frozen: (_ for _ in ()).throw(RuntimeError('boom')))
    result = containment.self_test(tmp_path / 'home')
    assert result['contained'] is False and result['reason'].startswith('error:')


# --- spec parsing -----------------------------------------------------------

def read_spec(data):
    r, w = os.pipe()
    os.write(w, data)
    os.close(w)
    try:
        return containment._read_spec(r)
    finally:
        os.close(r)


@pytest.mark.parametrize('data', [
    b'not-a-number\n', b'5\nhello', b'0\n', b'99999999\n{}', b'2\n{}', b'7\n[1, 2]', b'',
    b'12\n{"argv": []}', b'%d\n' % (containment.SPEC_LIMIT + 1), b'1' * 40,
], ids=repr)
def test_read_spec_refuses_malformed_input(data):
    assert read_spec(data) is None


def test_read_spec_accepts_a_framed_spec():
    body = json.dumps({'argv': ['/bin/echo'], 'cwd': '/'}).encode()
    assert read_spec(b'%d\n' % len(body) + body) == {'argv': ['/bin/echo'], 'cwd': '/'}


def test_anchor_main_without_fds_refuses(monkeypatch):
    for name in ('PEBBLE_ANCHOR_SPEC_FD', 'PEBBLE_ANCHOR_STATUS_FD', 'PEBBLE_ANCHOR_OUT_FD'):
        monkeypatch.delenv(name, raising=False)
    assert containment.anchor_main(['--job-anchor']) == 2
