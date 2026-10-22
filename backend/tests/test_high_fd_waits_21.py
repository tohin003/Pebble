"""Phase 21 track F21-fd: every wait works on descriptors numbered above FD_SETSIZE (1024).

select.select refuses any descriptor numbered 1024 or above. The full backend suite failed the screen-session worker
round trip once earlier test files had left about 1,100 descriptors open, and a long-running backend (connectors,
MCP servers, jobs, SQLite connections) can reach that range too; a job shim keeps the host's numbers (pass_fds). The
product waits now use selectors (kqueue on macOS, which also waits on a PTY master). Each test here first holds
enough descriptors that every new pipe, socket, PTY and kqueue is numbered above 1024, then drives a real wait: the
AX worker client, the job shim's credential read, event loop and PTY write (in this process, without sandbox-exec),
and a child provider's non-blocking connect. Timeouts, EOF handling and error codes are the ones they always were.

Descriptor census: this file closes everything it opens, and its first test prints how many descriptors were open
when the file started. Run another test file first (`pytest <file> backend/tests/test_high_fd_waits_21.py -rP`)
and subtract this file's count when run alone to get what that file left open. close_guard_databases is the
cleanup the Phase 20 test files use for the AgentGuard connections their Apps leave open.
"""
import errno
import fcntl
import os
import resource
import select
import selectors
import socket
import socketserver
import stat
import sys
import threading
import time
import tty
import urllib.error
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import agent_provider, ax_worker, job_shim
from pebble.agent_provider import PinnedProvider
from pebble.ax_worker import AXError
from pebble.screen_session import AppAdapter, ScreenSessions
from pebble.store import Store

import test_screen_session as screen

FD_SETSIZE = 1024
HOLD = 1100
SECRET = uuid.uuid4().hex


def close_guard_databases(root):
    """Close AgentGuard's cached connection for every AGENTGUARD_HOME under root; returns how many were closed.

    Database.shared() keeps one SQLite connection per AGENTGUARD_HOME for the life of the process (three
    descriptors: the database, its WAL and its shared memory), and App.close() leaves it open (ProjectStore.close
    owns nothing). A test file whose Apps live under its own folders closes theirs here; nothing else is touched.
    """
    store = sys.modules.get('agentguard.core.store')
    if store is None:
        return 0
    with store.Database._registry_lock:
        mine = [key for key in store.Database._instances if Path(key).is_relative_to(root)]
        for key in mine:
            store.Database._instances.pop(key).close()
    return len(mine)


def describe_socket(fd):
    """'family state' for a socket, read through a duplicate so the original is never closed."""
    try:
        copy = os.dup(fd)
    except OSError:
        return '?'
    try:
        sock = socket.socket(fileno=copy)
    except OSError:
        os.close(copy)
        return '?'
    with sock:
        try:
            sock.getpeername()
            state = 'connected'
        except OSError:
            state = 'unconnected'
        if sock.family in (socket.AF_INET, socket.AF_INET6) and state == 'unconnected':
            state = 'listening' if sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) else state
        return f'{sock.family.name} {state}'


def descriptor_path(fd):
    try:
        return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b'\0'))
    except (AttributeError, OSError):
        return None


def open_descriptors():
    """{descriptor: kind} for this process (macOS lists them in /dev/fd)."""
    found = {}
    for name in os.listdir('/dev/fd'):
        fd = int(name)
        try:
            mode = os.fstat(fd).st_mode
        except OSError:
            continue  # listdir's own descriptor, closed by now
        if stat.S_ISFIFO(mode):
            kind = 'pipe'
        elif stat.S_ISSOCK(mode):
            kind = 'socket ' + describe_socket(fd)
        elif stat.S_ISCHR(mode):
            kind = 'device'
        elif stat.S_ISREG(mode) or stat.S_ISDIR(mode):
            path = descriptor_path(fd)
            kind = 'file ' + Path(path).name if path else 'file'
        else:
            kind = 'other'  # kqueue and the like
        found[fd] = kind
    return found


@pytest.fixture(scope='module', autouse=True)
def census():
    # Reported, not asserted: in the full suite, other files' background threads may open descriptors meanwhile.
    before = open_descriptors()
    print(f'open descriptors when {Path(__file__).name} started: {len(before)} '
          f'{dict(Counter(before.values()).most_common(12))}')
    yield
    left = {fd: kind for fd, kind in open_descriptors().items() if fd not in before}
    print(f'descriptors opened by {Path(__file__).name} and still open: {len(left)} {dict(Counter(left.values()))}')


@pytest.fixture
def high():
    """Hold HOLD descriptors, so that every descriptor opened next is numbered above FD_SETSIZE."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    current = open_descriptors()
    need = max(max(current), len(current)) + HOLD + 256
    if soft != resource.RLIM_INFINITY and soft < need:
        resource.setrlimit(resource.RLIMIT_NOFILE, (need if hard == resource.RLIM_INFINITY else min(need, hard), hard))
    held = []
    try:
        for _ in range(HOLD):
            held.append(os.open(os.devnull, os.O_RDONLY))
        probe = os.pipe()
        for fd in probe:
            os.close(fd)
        assert min(probe) >= FD_SETSIZE
        yield
    finally:
        for fd in held:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def close_all(*fds):
    for fd in fds:
        try:
            if isinstance(fd, int):
                os.close(fd)
            elif fd is not None:
                fd.close()
        except OSError:
            pass


def test_select_itself_refuses_these_descriptors(high):
    # The defect's cause, shown in this process: the regime every other test here runs in.
    read, write = os.pipe()
    try:
        assert read >= FD_SETSIZE
        with pytest.raises(ValueError):
            select.select([read], [], [], 0)
    finally:
        close_all(read, write)


# ---- the AX worker client -------------------------------------------------------------------------------------

def test_screen_session_round_trip_through_the_worker_above_1024(tmp_path, high):
    # test_screen_session's end-to-end case, with the worker's pipes numbered above 1024.
    clock, ax = screen.Clock(), screen.FakeAX()
    app = screen.App(4242, 1_700_000_000.25)
    window = app.add(screen.Window('win-1', screen.TITLE, screen.compose() + [
        screen.el('AXSecureTextField', 'w/pin', title='Search', value='hunter2', frame=[0, 500, 50, 20])]))
    ax.apps.append(app)
    procs = []
    client = ax_worker.WorkerClient(['unused'], spawn=lambda argv: procs.append(screen.PipeProc(ax)) or procs[-1],
                                    timeout=5)
    store = Store(tmp_path / 'home')
    try:
        sessions = ScreenSessions(store, {'app': AppAdapter(client, processes=ax.processes)},
                                  clock=clock.monotonic, wall=clock.wall, sleep=clock.sleep)
        sid = sessions.grant(uuid.uuid4().hex, 'f' * 32, screen.target(), screen.ALL, 300)['session_id']
        assert sessions.observe(sid, 'f' * 32)['state'] == 'handoff'
        sessions.resume(sid)
        view = sessions.observe(sid, 'f' * 32)
        assert procs and procs[0].stdout.fileno() >= FD_SETSIZE
        record = sessions.observation(sid, view['observation_id'])
        to = next(e['index'] for e in record['elements'] if e['path'] == 'w/to')
        result = sessions.act(sid, 'f' * 32, view['observation_id'], f"e{to}@{view['observation_id']}", 'type',
                              'ivan@example.com')
        assert result['verdict'] == 'occurred' and window.find('w/to')['value'] == 'ivan@example.com'
        assert len(procs) == 1  # one worker served every call: no wait failed and killed it
    finally:
        client.close()
        store.close()


class MuteWorker:
    """A worker over real pipes that never answers, or (exits=True) has already closed its output."""

    def __init__(self, exits=False):
        host_in, worker_out = os.pipe()
        worker_in, host_out = os.pipe()
        self.stdout, self.stdin = os.fdopen(host_in, 'rb'), os.fdopen(host_out, 'wb')
        self.ends = [worker_in, worker_out]
        if exits:
            os.close(worker_out)
            self.ends.remove(worker_out)

    def poll(self):
        return None

    def kill(self):
        close_all(*self.ends)
        self.ends = []

    def wait(self, timeout=None):
        return 0


@pytest.mark.parametrize('op', ['windows', 'perform'])
def test_worker_timeout_and_exit_keep_their_error_codes_above_1024(high, op):
    workers, numbers = [], []

    def spawn(argv, exits=False):
        workers.append(MuteWorker(exits))
        numbers.extend((workers[-1].stdin.fileno(), workers[-1].stdout.fileno()))
        return workers[-1]

    def call(client):
        if op == 'windows':
            return client.windows(7, 1.5)
        return client.perform(7, 1.5, 'w1', 'p', 'press', None)

    client = ax_worker.WorkerClient(['unused'], spawn=spawn, timeout=0.3)
    try:
        started = time.monotonic()
        with pytest.raises(AXError) as silent:
            call(client)
        assert time.monotonic() - started >= 0.25  # the wait ran to its deadline
        assert workers[0].stdout.closed and client.proc is None  # the failure killed the worker
        client.spawn = lambda argv: spawn(argv, exits=True)
        with pytest.raises(AXError) as gone:
            call(client)
        assert len(workers) == 2 and min(numbers) >= FD_SETSIZE
        if op == 'perform':  # an act whose answer never came may have happened: uncertain, never retried
            assert (silent.value.code, silent.value.detail) == ('uncertain',
                                                                'The accessibility worker did not answer in time.')
            assert (gone.value.code, gone.value.detail) == ('uncertain', 'The accessibility worker exited.')
        else:
            assert (silent.value.code, silent.value.detail) == ('timeout',
                                                                'The accessibility worker did not answer in time.')
            assert (gone.value.code, gone.value.detail) == ('protocol', 'The accessibility worker exited.')
    finally:
        client.close()
        for worker in workers:
            worker.kill()
            close_all(worker.stdin, worker.stdout)


# ---- the job shim's waits, in this process --------------------------------------------------------------------

def make_shim(tmp_path, **plan):
    jobdir = tmp_path / 'job'
    jobdir.mkdir()
    return job_shim._Shim(jobdir, {'secret': SECRET, 'limits': {}, **plan}, None)


def frame(*items, end=True):
    data = bytearray(job_shim.CREDENTIAL_MAGIC)
    for ref, value in items:
        data += len(ref).to_bytes(2, 'big') + ref + len(value).to_bytes(4, 'big') + value
    return bytes(data + (b'\x00\x00' if end else b''))


def test_shim_reads_credentials_above_1024(tmp_path, high, monkeypatch):
    shim = make_shim(tmp_path)
    writers = []
    try:
        def pipe(data, close):
            read, write = os.pipe()
            assert read >= FD_SETSIZE
            monkeypatch.setenv('PEBBLE_SHIM_CREDENTIAL_FD', str(read))
            os.write(write, data)
            if close:
                os.close(write)
            else:
                writers.append(write)

        pipe(frame((b'vault:a', b'one'), (b'vault:b', b'two')), close=False)
        found = shim._read_credentials(['vault:a', 'vault:b'])
        assert {ref: bytes(value) for ref, value in found.items()} == {'vault:a': b'one', 'vault:b': b'two'}
        # A frame cut short by EOF, or still incomplete at the deadline, is refused; neither is a wait failure.
        pipe(frame((b'vault:a', b'one'), end=False), close=True)
        with pytest.raises(job_shim._Unstarted, match='credentials_unavailable'):
            shim._read_credentials(['vault:a'])
        monkeypatch.setattr(job_shim, 'CREDENTIAL_SECONDS', 0.3)
        pipe(frame((b'vault:a', b'one'), end=False), close=False)
        started = time.monotonic()
        with pytest.raises(job_shim._Unstarted, match='credentials_unavailable'):
            shim._read_credentials(['vault:a'])
        assert time.monotonic() - started >= 0.25
    finally:
        close_all(*writers)
        shim._close()


def test_shim_event_loop_serves_every_source_above_1024(tmp_path, high):
    shim = make_shim(tmp_path, pty=True, on_disconnect='continue')
    status_w = out_w = host_w = None
    clients, terminal = [], {}
    try:
        shim.status_r, status_w = os.pipe()
        shim.out_r, out_w = os.pipe()
        shim.host_lifeline, host_w = os.pipe()
        shim.master, terminal['slave'] = os.openpty()
        for fd in (shim.status_r, shim.out_r, shim.host_lifeline, shim.master):
            os.set_blocking(fd, False)
        shim._open_control()
        served, client = socket.socketpair()  # a control connection already accepted
        served.setblocking(False)
        shim.conns[served] = b''
        dialled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # a connection the loop must accept first
        clients += [client, dialled]
        dialled.connect(str(job_shim.control_path(shim.jobdir)))
        assert min(shim.status_r, shim.out_r, shim.host_lifeline, shim.master, shim.control.fileno(),
                   served.fileno(), dialled.fileno()) >= FD_SETSIZE
        request = (('{"secret": "%s", "op": "status"}\n' % SECRET).encode())
        for sock in clients:
            sock.sendall(request)
        os.write(out_w, b'out-bytes')
        os.write(status_w, b'{"event": "started", "steps": 1}\n')
        os.write(terminal['slave'], b'pty-bytes')
        os.close(host_w)  # the host went away
        host_w = None
        ticks = []

        def complete():
            ticks.append(time.monotonic())
            if len(ticks) == 2:
                os.close(terminal.pop('slave'))  # the job's terminal closed: the master now reads EIO
            return len(ticks) == 4

        shim._complete = complete
        shim._loop()
        # Tick 1 drained output, status, the PTY and the lifeline, served one connection and accepted another;
        # tick 2 served the accepted one; tick 3 saw the PTY hang up; tick 4 had nothing and waited SAMPLE_SECONDS.
        assert ticks[3] - ticks[2] >= job_shim.SAMPLE_SECONDS * 0.8
        spool = (shim.jobdir / job_shim.SPOOL).read_bytes()
        assert b'out-bytes' in spool and b'pty-bytes' in spool
        assert shim.master is None and shim.host_lifeline is None and shim.started and not shim.conns
        records = [r['record'] for r in job_shim.read_records(shim.jobdir)]
        assert 'started' in records and 'host_disconnect' in records
        for sock in clients:
            sock.settimeout(5)
            assert sock.recv(4096).startswith(b'{"ok": true, "exit_seen": false')
    finally:
        close_all(status_w, out_w, host_w, terminal.get('slave'), shim.host_lifeline, *clients)
        shim.host_lifeline = None
        shim._close()


def test_f21_fd_1_job_handle_alive_closes_its_lock_descriptor(tmp_path):
    # Host side, every poll of a live job (process_jobs._follow polls every 0.05 s): test_process_recovery alone
    # left 178 shim.lock descriptors open this way. Finding F21-FD-1, fixed by the merge (parent patch P1, I21): the
    # strict xfail mark is gone, so this now runs as a plain regression test.
    jobdir = tmp_path / 'job'
    jobdir.mkdir()
    lock = os.path.realpath(jobdir / job_shim.LOCK)
    holder = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    before = set(open_descriptors())
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)  # stands in for the live shim's lock
        handle = job_shim.attach(jobdir)
        assert all(handle.alive() for _ in range(5))
        assert [fd for fd in open_descriptors() if fd not in before] == []
    finally:
        for fd in set(open_descriptors()) - before:
            if descriptor_path(fd) == lock:  # only what alive() left on this test's own lock file
                os.close(fd)
        os.close(holder)


def read_exactly(fd, count, seconds=5):
    data, end = b'', time.monotonic() + seconds
    while len(data) < count and time.monotonic() < end:
        try:
            data += os.read(fd, count - len(data))
        except BlockingIOError:
            time.sleep(0.002)
    return data


def test_shim_pty_write_waits_for_room_above_1024(tmp_path, high):
    # The PTY case: macOS poll() does not support devices, so this proves kqueue waits on a PTY master.
    shim = make_shim(tmp_path, pty=True)
    shim.master, slave = os.openpty()
    reader = None
    try:
        os.set_blocking(shim.master, False)
        os.set_blocking(slave, False)
        tty.setraw(slave)  # no echo or line editing: input queues until the job reads it
        assert min(shim.master, slave) >= FD_SETSIZE
        data = (b'pebble-' * 1200)[:8192]  # several times the terminal's input queue
        started = time.monotonic()
        written = shim._write_master(data)  # nobody reads: it fills the queue, then waits for room to its deadline
        assert 0 < written < len(data) and time.monotonic() - started >= job_shim.INPUT_WRITE_SECONDS * 0.9
        started = time.monotonic()
        assert job_shim._ready([shim.master], selectors.EVENT_WRITE, 0.2) == []  # full: the wait times out
        assert time.monotonic() - started >= 0.15
        assert read_exactly(slave, written) == data[:written]
        assert job_shim._ready([shim.master], selectors.EVENT_WRITE, 1) == [shim.master]  # room again
        received = []
        reader = threading.Thread(target=lambda: received.append(read_exactly(slave, len(data))), daemon=True)
        reader.start()
        assert shim._write_master(data) == len(data)  # each wait woke when the job read
        reader.join(5)
        assert received == [data]
    finally:
        if reader is not None:
            reader.join(5)
        close_all(slave)
        shim._close()


# ---- a child provider's non-blocking connect ------------------------------------------------------------------

class InProgress(socket.socket):
    """Reports even an immediate loopback connect as in progress, so connect() always reaches its wait."""

    def connect_ex(self, address):
        error = super().connect_ex(address)
        return errno.EINPROGRESS if error == 0 else error


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = False  # server_close joins every handler, so nothing outlives the test

    def server_bind(self):
        # HTTPServer.server_bind would also call getfqdn(), whose resolver socket stays open for the process.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Hello(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b'connected'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_child_provider_connect_waits_above_1024(high, monkeypatch):
    server = LoopbackServer(('127.0.0.1', 0), Hello)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # A port nothing holds refuses a connect (macOS drops a SYN to a port bound without listen()).
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as vacated:
            vacated.bind(('127.0.0.1', 0))
            refused = vacated.getsockname()[1]
        waits, real = [], agent_provider._connect_settled
        monkeypatch.setattr(agent_provider, '_connect_settled',
                            lambda sock, timeout: waits.append(sock.fileno()) or real(sock, timeout))
        monkeypatch.setattr(agent_provider, 'socket', SimpleNamespace(**{**vars(socket), 'socket': InProgress}))
        owner = SimpleNamespace(_active_cancel=threading.Event(), _track_socket=lambda sock: None,
                                _addresses=lambda host, port, timeout: [
                                    (socket.AF_INET, socket.SOCK_STREAM, 0, '', ('127.0.0.1', port))])
        opener = PinnedProvider._opener(owner)  # the child's real Connection, owned by a stand-in provider
        with opener.open(f'http://127.0.0.1:{server.server_port}/', timeout=5) as response:
            assert response.read() == b'connected'
        assert waits and min(waits) >= FD_SETSIZE
        waits.clear()
        with pytest.raises(urllib.error.URLError) as failed:
            opener.open(f'http://127.0.0.1:{refused}/', timeout=5)
        assert failed.value.reason.errno == errno.ECONNREFUSED  # SO_ERROR after the wait, as before
        assert waits and min(waits) >= FD_SETSIZE
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
