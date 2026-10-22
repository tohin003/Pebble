"""The contained plan executor, stage B (R07): an interactive plan's PTY under shim + anchor.

Design study §8c: the shim allocates the PTY, the profile grants ioctl, read and
write on exactly that slave literal plus /dev/tty, the command gets setsid and
TIOCSCTTY, input arrives only through the secret-gated 'input' control op,
model input is refused while ECHO is off, human input is journaled by length
and SHA-256 only, stop hangs the terminal up before the sweep, and a restarted
host's input stays refused until it proves it owns the job.

Signal safety: every plan gets its own outside canary and the anchor's SIGURG
interlock (`scoped`, shared with test_plan_executor). The crash case SIGKILLs
only the backend interpreter this test started, by live handle; the job's own
teardown is the anchor's instance sweep.
"""

import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import signal
import time
from pathlib import Path

import pytest

from pebble import async_terminal, command_plans, job_shim, pty_input
from pebble.policy import PolicyError
from pebble.terminal import CommandOutcomeUncertain

from test_plan_executor import LAUNCHER, Host, canary_result, macos, outside_canary, project, scoped, script  # noqa: F401

TESTS = Path(__file__).resolve().parent
BACKEND = TESTS.parent
HUMAN_SECRET = 'hunter2-w8-' + secrets.token_hex(8)


# --- pty_input: encoding, re-validation, receipts and limits --------------------------------

def test_text_and_keys_encode_to_exact_bytes():
    assert pty_input.encode(text='hello wörld\tx\n') == ('text', 'hello wörld\tx\n'.encode())
    expected = {'enter': b'\r', 'tab': b'\t', 'backspace': b'\x7f', 'ctrl_c': b'\x03', 'ctrl_d': b'\x04',
                'up': b'\x1b[A', 'down': b'\x1b[B', 'right': b'\x1b[C', 'left': b'\x1b[D'}
    assert {key: pty_input.encode(key=key) for key in expected} == {key: ('key', value) for key, value in expected.items()}
    for kind, data in [('text', b'plain\n')] + [('key', value) for value in expected.values()]:
        assert pty_input.check(kind, data) == data


@pytest.mark.parametrize('text', ['\x1b[A', '\x1b', 'a\x1b]52;c;U0VDUkVU\x07', '\x9b2J', '\x07', '\x00', '\x7f', '\r',
                                  'a‮b', 'zero​width', 'nb sp', '\ud800', '\x08', '', 'x' * 1025,
                                  'é' * 513])
def test_raw_escapes_controls_and_oversize_text_are_refused(text):
    with pytest.raises(PolicyError):
        pty_input.encode(text=text)


def test_shim_revalidation_refuses_anything_but_text_or_one_key():
    for kind, data in (('text', b'\x1b[A'), ('text', b'\xff'), ('text', b'a\x00'), ('key', b'\x1b[2J'), ('key', b'x'),
                       ('key', b'\x1b[A\x1b[A'), ('raw', b'a'), ('text', b''), ('text', b'x' * 1025), ('text', 'str')):
        with pytest.raises(PolicyError):
            pty_input.check(kind, data)
    for kwargs in ({}, {'text': 'a', 'key': 'enter'}, {'key': 'escape'}, {'key': 'f1'}, {'key': b'\r'}, {'text': b'a'}):
        with pytest.raises(PolicyError):
            pty_input.encode(**kwargs)


def test_receipts_hold_length_and_hash_never_text():
    data = HUMAN_SECRET.encode()
    receipt = pty_input.receipt('human', 'text', data)
    assert receipt == {'origin': 'human', 'kind': 'text', 'length': len(data),
                       'sha256': hashlib.sha256(data).hexdigest()}
    assert HUMAN_SECRET not in json.dumps(receipt)
    assert pty_input.receipt('model', 'key', b'\x03', 'ctrl_c')['key'] == 'ctrl_c'


def test_limiter_enforces_write_total_and_rate():
    now = [100.0]
    limiter = pty_input.Limiter(clock=lambda: now[0])
    assert limiter.refusal(1025) == 'write_limit' and limiter.refusal(0) == 'write_limit'
    for _ in range(4):
        assert limiter.refusal(1024) is None
        limiter.spend(1024)
    assert limiter.refusal(1) == 'rate_limit'  # a fifth write inside one second
    now[0] += 0.99
    assert limiter.refusal(1) == 'rate_limit'
    now[0] += 0.02
    for _ in range(28):
        while limiter.refusal(1024) == 'rate_limit':
            now[0] += 0.25
        assert limiter.refusal(1024) is None
        limiter.spend(1024)
    assert limiter.total == 32 * 1024 and limiter.refusal(1) == 'total_limit'
    now[0] += 60
    assert limiter.refusal(1) == 'total_limit'  # the per-process budget never refills


# --- a live interactive job through run_plan and the shim's control socket --------------------

class Interactive:
    """One interactive plan run by async_terminal.run_plan in a thread; input goes straight to its shim."""

    def __init__(self, project, tmp_path, name, code, *, on_disconnect='terminate', token=None, timeout=60):
        script(project, name + '.py', code)
        self.plan = command_plans.from_argv([{'argv': ['python3', name + '.py'], 'stdin': 'pty'}], str(project),
                                            interactive=True, on_disconnect=on_disconnect,
                                            resources={'timeout': timeout})
        self.jobdir = Path(tmp_path) / 'jobs' / name
        self.token = token or secrets.token_hex(16)
        self.cancel = threading.Event()
        self.chunks, self.started, self.outcome = [], [], {}
        self.thread = threading.Thread(target=self._run)
        self.thread.start()

    def _run(self):
        try:
            self.outcome['result'] = async_terminal.run_plan(
                self.plan, self.cancel, self.started.append, self.chunks.append, launcher=LAUNCHER, jobdir=self.jobdir,
                input_token=self.token)
        except BaseException as exc:
            self.outcome['error'] = exc

    def output(self):
        return b''.join(list(self.chunks))

    def wait_output(self, needle, seconds=30):
        end = time.monotonic() + seconds
        while needle not in self.output() and time.monotonic() < end and self.thread.is_alive():
            time.sleep(0.03)
        assert needle in self.output(), (needle, self.output()[-400:], self.outcome, self.records())
        return self.output()

    def handle(self):
        return job_shim.attach(self.jobdir)

    def send(self, data, *, origin='model', kind='text', owner=None):
        return self.handle().input(data, origin=origin, kind=kind, owner=self.token if owner is None else owner)

    def key(self, name, **kwargs):
        time.sleep(0.27)  # stay under 4 writes per second
        return self.send(pty_input.encode(key=name)[1], kind='key', **kwargs)

    def records(self):
        return job_shim.read_records(self.jobdir) if self.jobdir.exists() else []

    def finish(self, seconds=60):
        self.thread.join(seconds)
        assert not self.thread.is_alive(), self.records()
        if 'error' in self.outcome:
            raise self.outcome['error']
        return self.outcome['result']

    def stop(self):
        self.cancel.set()
        return self.finish()


LINE = "line = input('> ')\nprint('GOT', line, flush=True)\n"
KEYS = ("import os, termios, tty\n"
        "print('RAW', flush=True)\n"
        "old = termios.tcgetattr(0)\n"
        "tty.setraw(0)\n"
        "data = b''\n"
        "while len(data) < 15:\n"
        "    data += os.read(0, 64)\n"
        "termios.tcsetattr(0, termios.TCSANOW, old)\n"
        "print('KEYS', data.hex(), flush=True)\n"
        "try:\n"
        "    input('LINE> ')\n"
        "except EOFError:\n"
        "    print('EOF', flush=True)\n"
        "try:\n"
        "    input('INT> ')\n"
        "except KeyboardInterrupt:\n"
        "    print('INTERRUPTED', flush=True)\n")


@macos
def test_line_input_round_trip_and_status_reports_echo(project, scoped, tmp_path):
    job = Interactive(project, tmp_path, 'line', LINE)
    job.wait_output(b'> ')
    status = job.handle().status()
    assert status['ok'] is True and status['echo'] is True and status['started'] is True and status['owned'] is True
    assert job.send(b'hello world\n') == {'ok': True, 'written': 12, 'echo': True}
    result = job.finish()
    out = job.output()
    assert (result['state'], result['exit_code'], result['outcome_uncertain']) == ('succeeded', 0, False), out
    assert b'GOT hello world' in out and b'hello world\r\n' in out  # echoed by the terminal, then printed
    assert 'macOS sandbox · shim+anchor · typed plan' in result['isolation'] and len(job.started) == 1
    inputs = [r for r in job.records() if r['record'] == 'input']
    assert [(r['origin'], r['kind'], r['length'], r['echo']) for r in inputs] == [('model', 'text', 12, True)]
    assert inputs[0]['sha256'] == hashlib.sha256(b'hello world\n').hexdigest()
    assert len(scoped) == 1


@macos
def test_named_keys_arrive_as_their_exact_sequences(project, scoped, tmp_path):
    job = Interactive(project, tmp_path, 'keys', KEYS)
    job.wait_output(b'RAW')
    time.sleep(0.2)  # let the job enter raw mode
    # Raw mode clears ECHO, so the guard refuses the model; a person at the native window may type.
    assert job.key('up') == {'ok': False, 'error': 'echo_off', 'echo': False}
    for name in ('up', 'down', 'left', 'right', 'tab', 'backspace', 'enter'):
        assert job.key(name, origin='human') == {'ok': True, 'written': len(pty_input.KEYS[name]), 'echo': False}, name
    job.wait_output(b'LINE> ')
    assert job.key('ctrl_d')['ok'] is True  # EOF on an empty line
    job.wait_output(b'INT> ')
    assert job.key('ctrl_c')['ok'] is True  # SIGINT to the terminal's foreground job
    result = job.finish()
    out = job.output()
    assert result['state'] == 'succeeded', out
    assert b'KEYS ' + b'\x1b[A\x1b[B\x1b[D\x1b[C\t\x7f\r'.hex().encode() in out
    assert b'EOF' in out and b'INTERRUPTED' in out
    assert len(scoped) == 1


@macos
def test_ctrl_c_interrupts_the_job_even_when_the_backend_ignores_sigint(project, scoped, tmp_path):
    # A background shell starts commands with SIGINT ignored; the job must still get default dispositions.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        job = Interactive(project, tmp_path, 'ignored', "try:\n    input('INT> ')\nexcept KeyboardInterrupt:\n"
                                                          "    print('INTERRUPTED', flush=True)\n", timeout=30)
    finally:
        signal.signal(signal.SIGINT, previous)
    job.wait_output(b'INT> ')
    assert job.key('ctrl_c')['ok'] is True
    result = job.finish()
    out = job.output()
    assert result['state'] == 'succeeded' and b'INTERRUPTED' in out, out


@macos
def test_shim_refuses_raw_escapes_strangers_and_bursts_without_touching_the_terminal(project, scoped, tmp_path):
    job = Interactive(project, tmp_path, 'refuse', LINE)
    job.wait_output(b'> ')
    for data, kind in ((b'\x1b[2J', 'text'), (b'\x1b]52;c;eA==\x07', 'text'), (b'\x1bX', 'key'), (b'ok', 'raw'),
                       (b'y' * 1025, 'text'), (b'\xff\n', 'text')):
        assert job.send(data, kind=kind) == {'ok': False, 'error': 'bad_input'}, (data, kind)
    assert job.send(b'x\n', owner=secrets.token_hex(16)) == {'ok': False, 'error': 'not_owner'}
    assert job.send(b'x\n', origin='child') == {'ok': False, 'error': 'bad_origin'}
    assert job.handle().claim(secrets.token_hex(16)) == {'ok': False, 'error': 'host_attached'}
    for text in (b'a', b'b', b'c', b'd'):
        assert job.send(text)['ok'] is True
    assert job.send(b'e') == {'ok': False, 'error': 'rate_limit'}  # the fifth write within one second
    time.sleep(1.05)
    assert job.send(b'\n')['ok'] is True
    result = job.finish()
    assert result['state'] == 'succeeded'
    assert b'GOT abcd' in job.output() and b'\x1b[2J' not in job.output()
    assert [r['length'] for r in job.records() if r['record'] == 'input'] == [1, 1, 1, 1, 1]
    assert len(scoped) == 1


GETPASS = ("import getpass\n"
           "secret = getpass.getpass('PW: ')\n"
           "print('LEN', len(secret), flush=True)\n"
           "print('ECHO-BACK', input('NAME> '), flush=True)\n")


@macos
def test_getpass_prompt_refuses_model_input_and_journals_human_input_by_hash(project, scoped, tmp_path):
    host = Host(tmp_path, project)
    try:
        script(project, 'pw.py', GETPASS)
        plan = command_plans.from_argv([{'argv': ['python3', 'pw.py'], 'stdin': 'pty'}], str(project),
                                       interactive=True)
        view = host.start(host.payload(plan))
        pid = view['process_id']
        args = (host.cid, host.rid, pid)
        wait = time.monotonic() + 30
        while b'PW: ' not in host.text(pid) and time.monotonic() < wait:
            time.sleep(0.03)
        assert b'PW: ' in host.text(pid), host.row(pid)
        jobdir = Path(host.row(pid)['jobdir'])
        assert job_shim.attach(jobdir).status()['echo'] is False  # the supervisor sees ECHO off from the master
        assert host.jobs.terminal(*args) == {'process_id': pid, 'interactive': True, 'live': True, 'echo': False,
                                             'input_bytes': 0, 'owned': True, 'authorization': 'none'}
        with pytest.raises(PolicyError, match='not echoing'):
            host.jobs.write_input(*args, text='model guess\n', origin='model')
        for origin in ('child', 'mod', 'view', 'root', None):
            with pytest.raises(PolicyError, match='Terminal input comes only from'):
                host.jobs.write_input(*args, text='x\n', origin=origin)
        with pytest.raises(PolicyError):
            host.jobs.write_input(*args, text='\x1b[A', origin='human')  # raw ESC: the host encodes keys itself
        receipt = host.jobs.write_input(*args, text=HUMAN_SECRET + '\n', origin='human')
        assert receipt == {'process_id': pid, 'sequence': 2, 'origin': 'human', 'kind': 'text',
                           'length': len(HUMAN_SECRET) + 1, 'echo': False, 'delivered': True}
        wait = time.monotonic() + 30
        while b'NAME> ' not in host.text(pid) and time.monotonic() < wait:
            time.sleep(0.03)
        assert job_shim.attach(jobdir).status()['echo'] is True
        host.jobs.write_input(*args, text='visible\n', origin='model')
        row = host.settled(pid)
        out = host.text(pid)
        assert (row['state'], row['outcome_uncertain'], row['contained']) == ('succeeded', 0, 1), (row['reason'], out)
        assert f'LEN {len(HUMAN_SECRET)}'.encode() in out and b'ECHO-BACK visible' in out
        # The secret was never echoed, stored, recorded or journaled; only its length and hash were.
        secret = HUMAN_SECRET.encode()
        assert secret not in out
        for path in [p for p in jobdir.rglob('*') if p.is_file()]:
            assert secret not in path.read_bytes(), path
        events = [json.loads(r['payload']) for r in host.store.query(
            "SELECT payload FROM run_events WHERE run_id=? AND kind='process_input' ORDER BY sequence", (host.rid,))]
        assert HUMAN_SECRET not in json.dumps(events)
        assert [(e['sequence'], e['phase']) for e in events] == [(1, 'requested'), (1, 'refused'), (2, 'requested'),
                                                                 (2, 'delivered'), (3, 'requested'), (3, 'delivered')]
        human = events[2]
        assert (human['origin'], human['length'], human['sha256']) == (
            'human', len(secret) + 1, hashlib.sha256(secret + b'\n').hexdigest())
        assert events[1]['reason'] == 'echo_off' and events[3]['echo'] is False
        # After settlement, and for non-interactive jobs, input is refused.
        with pytest.raises(PolicyError, match='no live interactive terminal'):
            host.jobs.write_input(*args, text='late\n', origin='human')
        assert host.jobs.terminal(*args) == {'process_id': pid, 'interactive': False, 'live': False, 'echo': None,
                                             'input_bytes': None, 'owned': False, 'authorization': 'none'}
    finally:
        assert host.close() is True
    assert len(scoped) == 1


@macos
def test_host_input_limits_and_stop_refusal(project, scoped, tmp_path):
    host = Host(tmp_path, project)
    try:
        script(project, 'wait.py', "import time\nprint('READY', flush=True)\ntime.sleep(30)\n")
        plan = command_plans.from_argv([{'argv': ['python3', 'wait.py'], 'stdin': 'pty'}], str(project),
                                       interactive=True)
        pid = host.start(host.payload(plan))['process_id']
        args = (host.cid, host.rid, pid)
        wait = time.monotonic() + 30
        while b'READY' not in host.text(pid) and time.monotonic() < wait:
            time.sleep(0.03)
        with pytest.raises(PolicyError, match='at most 1024 bytes'):
            host.jobs.write_input(*args, text='z' * 1025, origin='model')
        for _ in range(4):
            assert host.jobs.write_input(*args, text='a', origin='model')['delivered'] is True
        with pytest.raises(PolicyError, match='At most 4 terminal inputs per second'):
            host.jobs.write_input(*args, text='b', origin='model')
        host.jobs.stop(host.cid, host.rid, pid)
        with pytest.raises(PolicyError, match='stopping or has ended'):
            host.jobs.write_input(*args, text='c', origin='human')
        row = host.settled(pid)
        assert (row['state'], row['contained']) == ('cancelled', 1)
        records = [r['record'] for r in job_shim.read_records(Path(row['jobdir']))]
        assert records.index('hangup') < records.index('exited')  # hung up first, then swept
        plain = command_plans.from_argv([{'argv': ['python3', 'wait.py']}], str(project))
        other = host.start(host.payload(plain))['process_id']
        wait = time.monotonic() + 30
        while b'READY' not in host.text(other) and time.monotonic() < wait:
            time.sleep(0.03)
        with pytest.raises(PolicyError, match='no live interactive terminal'):
            host.jobs.write_input(host.cid, host.rid, other, text='x', origin='model')
        host.jobs.stop(host.cid, host.rid, other)
        host.settled(other)
    finally:
        assert host.close() is True
    assert len(scoped) == 2


OTHER = ("import errno, os, sys\n"
         "print('OWN', os.isatty(0) and os.isatty(1) and os.isatty(2), flush=True)\n"
         "print('SIZE', tuple(os.get_terminal_size(0)), flush=True)\n"
         "for path in (sys.argv[1], '/dev/ptmx'):\n"
         "    try:\n"
         "        fd = os.open(path, os.O_RDWR | os.O_NOCTTY)\n"
         "        os.write(fd, b'INJECTED-BY-JOB')\n"
         "        print('OPENED', path, flush=True)\n"
         "    except OSError as exc:\n"
         "        print('REFUSED', path, errno.errorcode.get(exc.errno), flush=True)\n"
         "fd = os.open('/dev/tty', os.O_RDWR)\n"
         "os.write(fd, b'via-dev-tty\\n')\n"
         "print('DONE', flush=True)\n")


@macos
def test_the_job_reaches_its_own_terminal_and_no_other_pty(project, scoped, tmp_path):
    other_master, other_slave = os.openpty()  # stands in for one of the user's terminal sessions
    try:
        other = os.ttyname(other_slave)
        script(project, 'other.py', OTHER)
        plan = command_plans.from_argv([{'argv': ['python3', 'other.py', other], 'stdin': 'pty'}], str(project),
                                       interactive=True)
        jobdir = tmp_path / 'jobs' / 'other'
        chunks = []
        result = async_terminal.run_plan(plan, threading.Event(), lambda info: None, chunks.append, launcher=LAUNCHER,
                                         jobdir=jobdir, input_token=secrets.token_hex(16))
        out = b''.join(chunks)
        assert result['state'] == 'succeeded', out
        assert b'OWN True' in out and b'via-dev-tty' in out and b'DONE' in out
        assert b'SIZE (80, 24)' in out  # the fixed window the shim set on the master
        assert f'REFUSED {other} EPERM'.encode() in out and b'REFUSED /dev/ptmx EPERM' in out, out
        os.set_blocking(other_master, False)
        try:
            leaked = os.read(other_master, 256)
        except BlockingIOError:
            leaked = b''
        assert leaked == b''
        profile = json.loads((jobdir / 'plan.json').read_text())['profile']
        assert 'ttys' not in profile and profile.count('pebble-slot: pty-slave') == 1  # bound only in the shim
    finally:
        os.close(other_master)
        os.close(other_slave)
    assert len(scoped) == 1


HUP = ("import os, signal, time\n"
       "def hup(*_):\n"
       "    open(os.path.join(os.environ['HOME'], 'hup'), 'w').write('hup')\n"
       "    time.sleep(30)\n"
       "signal.signal(signal.SIGHUP, hup)\n"
       "print('READY', flush=True)\n"
       "time.sleep(30)\n")


@macos
def test_stop_hangs_the_terminal_up_before_the_sweep(project, scoped, tmp_path):
    job = Interactive(project, tmp_path, 'hup', HUP)
    job.wait_output(b'READY')
    result = job.stop()
    assert (result['state'], result['outcome_uncertain']) == ('cancelled', True)
    assert (job.jobdir.with_name('hup-home') / 'hup').read_text() == 'hup'  # SIGHUP arrived from the master's close
    records = [r['record'] for r in job.records()]
    assert records.index('hangup') < records.index('exited')
    exited = next(r for r in job.records() if r['record'] == 'exited')
    assert exited['contained'] is True and exited['reason'] == 'stopped'
    assert job.send(b'late\n') is None  # the shim is gone: nothing listens
    assert len(scoped) == 1


# --- restart: input stays refused until the new host proves it owns the job -------------------

ASK = "print('PROMPT', flush=True)\nline = input('> ')\nprint('GOT', line, flush=True)\n"


def crash_backend(tmp, project, urg, report):
    """Child-interpreter body: admit one interactive 'continue' job, report it once prompted, then wait to die."""
    real = job_shim.write_plan
    job_shim.write_plan = lambda jobdir, plan: real(jobdir, {**plan, 'interlock_abort_path': urg})
    host = Host(Path(tmp), Path(project))
    plan = command_plans.from_argv([{'argv': ['python3', 'ask.py'], 'stdin': 'pty'}], project, interactive=True,
                                   on_disconnect='continue', resources={'timeout': 120})
    pid = host.start(host.payload(plan), timeout=120)['process_id']
    end = time.monotonic() + 60
    while b'> ' not in host.text(pid) and time.monotonic() < end:
        time.sleep(0.03)
    Path(report).write_text(json.dumps({'cid': host.cid, 'rid': host.rid, 'process_id': pid,
                                        'jobdir': host.row(pid)['jobdir']}))
    time.sleep(120)


@macos
def test_restarted_host_input_stays_refused_until_it_claims_the_terminal(project, tmp_path):
    script(project, 'ask.py', ASK)
    canary = outside_canary(tmp_path)
    report, log = tmp_path / 'crash.json', tmp_path / 'crash.log'
    code = ('import sys\nsys.path[:0] = [sys.argv[1], sys.argv[2]]\nimport test_pty_input as suite\n'
            'suite.crash_backend(*sys.argv[3:])\n')
    with open(log, 'wb') as err:
        backend = subprocess.Popen([sys.executable, '-c', code, str(BACKEND), str(TESTS), str(tmp_path), str(project),
                                    str(canary['urg']), str(report)], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=err)
    try:
        end = time.monotonic() + 90
        while not report.exists() and time.monotonic() < end and backend.poll() is None:
            time.sleep(0.05)
        assert report.exists(), log.read_text()[-3000:]
    finally:
        backend.kill()  # our own child, by live handle: the "backend crash"
        backend.wait(timeout=30)
    facts = json.loads(report.read_text())
    jobdir, pid = Path(facts['jobdir']), facts['process_id']
    host = Host(tmp_path, project, fresh=False, resume=False)
    try:
        host.cid, host.rid = facts['cid'], facts['rid']
        handle = job_shim.attach(jobdir)
        assert handle.alive(), 'an interactive continue job outlives its backend'
        assert handle.status()['owned'] is True  # still owned by the dead host's token
        with pytest.raises(PolicyError, match='no live interactive terminal'):
            host.jobs.write_input(host.cid, host.rid, pid, text='early\n', origin='model')
        # Holding the jobdir secret is not enough: the dead host's token is not known here.
        assert handle.input(b'rogue\n', origin='model', kind='text', owner=secrets.token_hex(16)) == {
            'ok': False, 'error': 'not_owner'}
        assert host.jobs.resume() == {'reattached': [pid], 'settled': []}
        assert 'claim' in [r['record'] for r in handle.records()]
        receipt = host.jobs.write_input(host.cid, host.rid, pid, text='after\n', origin='model')
        assert receipt['delivered'] is True and receipt['echo'] is True
        row = host.settled(pid)
        out = host.text(pid)
        assert (row['state'], row['outcome_uncertain'], row['contained'], row['settled_by']) == (
            'succeeded', 0, 1, 'supervisor_record'), (row['reason'], out)
        assert b'GOT after' in out and b'rogue' not in out and b'early' not in out
        claim = next(r for r in handle.records() if r['record'] == 'claim')
        assert set(claim) == {'record', 'ts'}  # the new owner token is never written
    finally:
        assert host.close() is True
    assert canary_result(canary) == (False, True)


def test_write_input_refuses_uncertain_delivery_without_repeating(tmp_path, monkeypatch, project):
    """A lost supervisor reply is journaled uncertain and raised as such; nothing is retried."""
    from pebble import process_jobs
    host = Host(tmp_path, project)
    try:
        live = process_jobs._Live('a' * 32, host.cid, host.rid)
        live.interactive, live.started, live.input_token, live.jobdir = True, True, 'b' * 32, tmp_path / 'nowhere'
        host.jobs._live[live.process_id] = live
        now = time.time()
        host.store.query('INSERT INTO process_jobs(id,action_id,chat_id,run_id,call_id,root,root_identity,'
                         'payload_hash,command,network,writable,owner,state,outcome_uncertain,stop_requested,'
                         'output_bytes,output_dropped,timeout,created,updated) VALUES(?,?,?,?,?,?,?,?,?,0,0,?,?,0,0,0,0,'
                         '60,?,?)', (live.process_id, 'c' * 32, host.cid, host.rid, 'call', str(project), '[1,2]',
                                     'd' * 64, 'x', host.jobs.owner, 'running', now, now))
        sent = []
        monkeypatch.setattr(job_shim.JobHandle, 'input', lambda self, *a, **k: sent.append(a) or None)
        with pytest.raises(CommandOutcomeUncertain, match='may or may not have reached'):
            host.jobs.write_input(host.cid, host.rid, live.process_id, key='enter', origin='human')
        assert len(sent) == 1
        events = [json.loads(r['payload']) for r in host.store.query(
            "SELECT payload FROM run_events WHERE run_id=? AND kind='process_input' ORDER BY sequence", (host.rid,))]
        assert [(e['phase'], e.get('key')) for e in events] == [('requested', 'enter'), ('uncertain', None)]
        del host.jobs._live[live.process_id]
        host.store.query("UPDATE process_jobs SET state='unknown' WHERE id=?", (live.process_id,))
    finally:
        host.close()
