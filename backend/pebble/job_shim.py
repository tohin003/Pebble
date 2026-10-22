"""The per-job shim: a detached supervisor that lives outside the sandbox.

Topology per job:   shim (here) -> sandbox-exec P_job -> anchor -> command

The shim is single-threaded and event-driven. It:
- holds an exclusive flock on jobdir/shim.lock for its whole life, so a free
  lock proves it is gone without consulting any PID;
- forks a child that sets rlimits and background priority, then execs
  sandbox-exec with the job profile and the launcher's --job-anchor dispatch;
- writes fsynced records (spawning, spawned, exited, teardown);
- streams the command's stdout/stderr into an append-only bounded spool, so a
  build's output survives a backend crash;
- enforces the deadline, the output limit and a measured memory budget by
  asking the anchor to sweep (never by signalling a PID it read from disk);
- serves jobdir/control.sock (status, stop, detach_ok) behind a per-job secret;
- watches a lifeline pipe from the host and applies on_disconnect on EOF.

Containment is the anchor's job (see containment.py). The shim only observes
it: an anchor that reports a completed sweep settles the job as contained; an
anchor that dies without that report is containment_lost and stays uncertain.
The shim never signals a stored PID; stop flows through the anchor's lifeline,
and the only PID it ever touches is its own live child, for sampling only.

Typed plans (plan.json 'steps') add, all decided by the host before launch:
- pipelines, per-step cwd and env and process_model 'single', run by the
  anchor's plan mode (plan_runner.py), which re-checks every pinned
  executable right before any step starts;
- a per-job EgressBroker plus CredentialBroker in this process for
  destination-scoped network. Credential values arrive only over the
  PEBBLE_SHIM_CREDENTIAL_FD pipe after launch, are granted and then zeroed;
  never through environment, argv, the jobdir or records. The job learns
  route URLs from non-secret PEBBLE_ROUTE_<NAME> variables. The route log
  (no path, no credential) becomes the 'egress' record;
- an interactive job's PTY, allocated here. Its slave path and the broker
  port are bound into the profile's slots (sandbox_profiles.bind) only after
  the pre-sandbox child was forked and before it execs, so the child never
  inherits broker threads or a credential. Input arrives through the 'input'
  control op, re-validated by pty_input, refused for origin 'model' while the
  terminal's ECHO is off, and recorded by length and SHA-256 only. Stop hangs
  the terminal up (SIGHUP to the session) before the anchor sweeps.

Any failure before the anchor execs (no credential, no broker, an unbound
profile) leaves the job unstarted: the child exits before sandbox-exec and the
exited record says anchor_spawn_failed, with no spawned record.
"""

import ctypes
import fcntl
import hashlib
import hmac
import json
import os
import resource
import selectors
import socket
import struct
import sys
import termios
import time
from pathlib import Path

PLAN = 'plan.json'
LOCK = 'shim.lock'
RECORDS = 'records.jsonl'
SPOOL = 'output.spool'
CONTROL = 'control.sock'
SOCKET_LIMIT = 103  # macOS sun_path bytes, excluding the terminating NUL


def control_path(jobdir):
    """The job's control socket, in a per-user private folder in the user temp directory.

    macOS limits AF_UNIX paths to 103 bytes; a job folder under the app's data
    directory can exceed that. The name is derived from the jobdir, so a
    restarted host finds the same socket; the per-job secret still gates use."""
    import hashlib
    import stat as _stat
    # The per-user temporary folder from confstr(_CS_DARWIN_USER_TEMP_DIR) does
    # not depend on environment variables, so the host and the shim (which runs
    # with a controlled environment) always agree on the socket path.
    buffer = ctypes.create_string_buffer(1024)
    size = ctypes.CDLL(None).confstr(65537, buffer, len(buffer))
    if not 0 < size <= len(buffer):
        raise RuntimeError('The per-user temporary folder is unavailable.')
    base = Path(buffer.value.decode()).resolve() / 'pebble-control'
    try:
        os.mkdir(base, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(base)
    if not _stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('The private control-socket folder is missing, shared or replaced.')
    path = base / (hashlib.sha256(str(Path(jobdir).resolve()).encode()).hexdigest()[:16] + '.sock')
    if len(str(path).encode()) > SOCKET_LIMIT:
        raise RuntimeError('The control-socket path exceeds the macOS limit.')
    return path
SAMPLE_SECONDS = 0.15
GRACE_SECONDS = 8.0            # how long to wait for the anchor's sweep to complete
SPEC_LIMIT = 256 * 1024
CONTROL_BACKLOG = 4
REQUEST_LIMIT = 4096
PLAN_LIMIT = 1024 * 1024
MAX_CONNECTIONS = 8            # simultaneous control connections; more are closed at once
REPLY_LIMIT = 64 * 1024
SEND_SECONDS = 1.0             # a reply blocks the loop at most this long
PROFILE_LIMIT = 512 * 1024
# Credentials from the host: b'PCRED1\n', then per reference a 2-byte length and the ASCII
# reference, a 4-byte length and the value; a zero reference length ends the frame.
CREDENTIAL_MAGIC = b'PCRED1\n'
CREDENTIAL_LIMIT = 8 * (2 + 128 + 4 + 8192) + 16
CREDENTIAL_SECONDS = 20.0
WINDOW = (24, 80)              # fixed rows and columns of an interactive job's terminal
HANGUP_SECONDS = 0.2           # after the hang-up, before the anchor is asked to sweep
INPUT_WRITE_SECONDS = 0.5
EGRESS_KEYS = frozenset({'route', 'method', 'path_hash', 'status', 'bytes', 'credential_used', 'allowed', 'reason',
                         'started', 'ended', 'host', 'port', 'bytes_up', 'bytes_down'})

# libproc / rusage for the measured memory governor (read-only sampling of
# this job's own process tree; nothing outside it is listed or read).
PROC_RUID_ONLY = 5
PROC_PPID_ONLY = 6
RUSAGE_INFO_V2 = 2
# struct rusage_info_v2: ri_uuid[16], then uint64 user_time, system_time,
# pkg_idle_wkups, interrupt_wkups, pageins, wired_size, resident_size,
# phys_footprint -> offset 16 + 7 * 8.
RI_PHYS_FOOTPRINT_OFFSET = 72
TREE_LIMIT = 4096              # members sampled per tick; more is reported as over budget
STOP_GRACE_SECONDS = 16.0      # stop requested -> anchor must be reaped within this

_libproc = None


class _Unstarted(Exception):
    """A failure before the anchor execs; str() is a fixed reason code, never secret text."""


def _read_frame(fd, limit):
    """b'<len>\\n<body>' from a blocking descriptor; ValueError if absent, short or too large."""
    header = b''
    while not header.endswith(b'\n'):
        byte = os.read(fd, 1)
        if not byte or len(header) > 16:
            raise ValueError('no frame')
        header += byte
    length = int(header)
    if not 0 < length <= limit:
        raise ValueError('frame size')
    body = bytearray()
    while len(body) < length:
        chunk = os.read(fd, length - len(body))
        if not chunk:
            raise ValueError('short frame')
        body += chunk
    return bytes(body)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _ready(objects, events, timeout):
    """The objects (descriptors or sockets) ready for events within timeout, in their given order.

    Not select(): it refuses descriptors numbered 1024 (FD_SETSIZE) or above, and the shim keeps the host's
    descriptor numbers (pass_fds), so a busy host hands it such descriptors. selectors uses kqueue on macOS,
    which also waits on the PTY master (macOS poll() does not support devices).
    """
    with selectors.DefaultSelector() as selector:
        for obj in objects:
            if obj not in selector.get_map():  # select() accepted a repeated entry; register() refuses one
                selector.register(obj, events)
        ready = {key.fileobj for key, _ in selector.select(timeout)}
    return [obj for obj in objects if obj in ready]


def _credentials(view, size):
    """Parse a complete credential frame from view[:size]: {ref: bytearray}, or None while incomplete.

    Values are copied straight from the receive buffer into fresh bytearrays;
    partial results are zeroed. A malformed frame raises _Unstarted.
    """
    found, ok = {}, False
    try:
        if size < len(CREDENTIAL_MAGIC):
            return None
        if bytes(view[:len(CREDENTIAL_MAGIC)]) != CREDENTIAL_MAGIC:
            raise _Unstarted('credentials_unavailable')
        at = len(CREDENTIAL_MAGIC)
        while True:
            if at + 2 > size:
                return None
            length = struct.unpack_from('>H', view, at)[0]
            at += 2
            if length == 0:
                ok = True
                return found
            if length > 128 or len(found) >= 8 or at + length + 4 > size:
                if length > 128 or len(found) >= 8:
                    raise _Unstarted('credentials_unavailable')
                return None
            ref = bytes(view[at:at + length]).decode('ascii', 'strict')
            at += length
            value_length = struct.unpack_from('>I', view, at)[0]
            at += 4
            if not 1 <= value_length <= 8192 or ref in found:
                raise _Unstarted('credentials_unavailable')
            if at + value_length > size:
                return None
            found[ref] = bytearray(view[at:at + value_length])
            at += value_length
    except UnicodeDecodeError:
        raise _Unstarted('credentials_unavailable') from None
    finally:
        if not ok:
            for value in found.values():
                value[:] = bytes(len(value))


def _egress_records(log):
    """The broker log as receipts: fixed keys only (route records carry a path hash, never a path)."""
    out = []
    for record in log[:256]:
        if isinstance(record, dict):
            out.append({key: record[key] for key in sorted(record) if key in EGRESS_KEYS})
    return out


# --- records ----------------------------------------------------------------

def _write_record(handle, record):
    handle.write((json.dumps({**record, 'ts': time.time()}) + '\n').encode())
    handle.flush()
    os.fsync(handle.fileno())


def read_records(jobdir):
    path = Path(jobdir) / RECORDS
    try:
        text = path.read_text()
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


# --- plan -------------------------------------------------------------------

def write_plan(jobdir, plan):
    """Write an immutable 0600 plan into a 0700 jobdir. Returns the jobdir path."""
    jobdir = Path(jobdir)
    jobdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(jobdir, 0o700)
    body = json.dumps(plan).encode()
    if len(body) > PLAN_LIMIT:
        raise ValueError('plan too large')
    path = jobdir / PLAN
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return jobdir


def _read_plan(jobdir):
    path = Path(jobdir) / PLAN
    data = path.read_bytes()
    if len(data) > PLAN_LIMIT:
        raise ValueError('plan too large')
    plan = json.loads(data)
    if not isinstance(plan, dict):
        raise ValueError('plan must be an object')
    for key in ('argv', 'env', 'cwd', 'profile', 'launcher', 'limits', 'secret'):
        if key not in plan:
            raise ValueError('plan missing ' + key)
    return plan


# --- shim process -----------------------------------------------------------

def main(argv):
    """Entry for `pebble-backend --job-shim <jobdir>`. Returns a process exit code."""
    args = [a for a in argv[1:] if a != '--job-shim']
    if not args:
        return 2
    # This process may hold credentials and a broker: never write a core image.
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError):
        return 2
    # Host descriptors stay in the shim; the anchor and the job never inherit them.
    for name in ('PEBBLE_SHIM_LIFELINE_FD', 'PEBBLE_SHIM_CREDENTIAL_FD'):
        try:
            os.set_inheritable(int(os.environ[name]), False)
        except (KeyError, ValueError, OSError):
            pass
    jobdir = Path(args[0])
    try:
        plan = _read_plan(jobdir)
    except (OSError, ValueError):
        return 2
    lock_fd = os.open(jobdir / LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        return 3  # another shim already owns this job
    os.umask(0o077)
    try:
        return _Shim(jobdir, plan, lock_fd).run()
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)


class _Shim:
    def __init__(self, jobdir, plan, lock_fd):
        self.jobdir = Path(jobdir)
        self.plan = plan
        self.lock_fd = lock_fd
        self.limits = plan.get('limits') or {}
        self.secret = plan['secret']
        self.on_disconnect = plan.get('on_disconnect', 'terminate')
        self.records = open(self.jobdir / RECORDS, 'ab')
        self.spool = open(self.jobdir / SPOOL, 'ab')
        self.spool_bytes = self.spool.tell()
        self.output_limit = int(self.limits.get('output_limit', 192_000))
        self.deadline = None
        self.anchor_pid = None
        self.anchor_lifeline = None   # shim->anchor (stop byte / EOF)
        self.status_r = None          # anchor->shim status messages
        self.out_r = None             # anchor->shim command output
        self.host_lifeline = None     # host->shim; EOF means the backend is gone
        self.control = None
        self.conns = {}
        self.status_buf = b''
        self.exit_code = None
        self.exit_seen = False
        self.sweep_started = False
        self.contained = False
        self.reason = None
        self.stopping = False
        self.stop_at = None
        self.dropped = 0
        self.output_full = False
        self.peak_memory = 0
        self.finished = False
        # Typed plans (all optional; a legacy plan has none of these keys).
        self.steps = plan.get('steps')
        self.network = plan.get('network')
        self.pty = plan.get('pty') is True
        self.started = False          # the anchor reported the first step exec'd
        self.step_codes = None
        self.broker = None
        self.credentials = None
        self.egress_log = []
        self.master = None            # interactive: the PTY master (the shim's end)
        self.slave_path = None
        self.hangup_at = None         # stop: when the terminal was hung up
        self.stop_sent = False
        self.input_owner = plan.get('input_owner')
        self.input_bytes = 0
        self.inputs = None
        self.profile_sha256 = None

    def run(self):
        self._record({'record': 'spawning'})
        self._open_control()
        self._open_host_lifeline()
        timeout = self.limits.get('timeout')
        if isinstance(timeout, (int, float)) and timeout > 0:
            self.deadline = time.monotonic() + float(timeout)
        try:
            self._spawn()
        except OSError as exc:
            self.reason = 'anchor_spawn_failed'
            self._close_network()
            self._record({'record': 'exited', 'exit_code': None, 'contained': False,
                          'reason': 'anchor_spawn_failed:%s' % exc})
            self._record({'record': 'teardown'})
            self._close()
            return 0
        self._record({'record': 'spawned', 'anchor_pid': self.anchor_pid, 'profile_sha256': self.profile_sha256})
        self._loop()
        self._settle()
        self._close()
        return 0

    # -- launch --

    def _spawn(self):
        lifeline_r, lifeline_w = os.pipe()   # shim -> anchor
        status_r, status_w = os.pipe()        # anchor -> shim
        out_r, out_w = os.pipe()              # command output -> shim
        exec_r, exec_w = os.pipe()            # close-on-exec: confirms exec
        profile_r, profile_w = os.pipe()      # shim -> pre-sandbox child: the bound profile
        launcher = list(self.plan['launcher'])
        env = dict(self.plan['env'])
        env['PEBBLE_ANCHOR_SPEC_FD'] = str(lifeline_r)
        env['PEBBLE_ANCHOR_STATUS_FD'] = str(status_w)
        env['PEBBLE_ANCHOR_OUT_FD'] = str(out_w)
        keep = [lifeline_r, status_w, out_w]
        slave = None
        try:
            if self.pty:
                self.master, slave = os.openpty()  # both close-on-exec until the child marks the slave
                self.slave_path = os.ttyname(slave)
                fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', WINDOW[0], WINDOW[1], 0, 0))
                os.set_blocking(self.master, False)
                env['PEBBLE_ANCHOR_TTY_FD'] = str(slave)
                keep.append(slave)
        except OSError:
            self._close_master()
            for fd in (lifeline_r, lifeline_w, status_r, status_w, out_r, out_w, exec_r, exec_w, profile_r, profile_w,
                       slave):
                if fd is not None:
                    os.close(fd)
            raise OSError('terminal_unavailable') from None
        # Fork while this process is still single-threaded: the broker's threads start
        # only afterwards, and the child waits for the bound profile before it execs.
        pid = os.fork()
        if pid == 0:
            try:
                os.close(exec_r)
                for fd in (lifeline_w, status_r, out_r, profile_w):
                    os.close(fd)
                if self.master is not None:
                    os.close(self.master)
                _apply_governors(self.limits)
                os.setpgid(0, 0)
                devnull = os.open(os.devnull, os.O_RDONLY)
                os.dup2(devnull, 0)
                sink = os.open(os.devnull, os.O_WRONLY)
                os.dup2(sink, 1)
                os.dup2(sink, 2)
                for fd in keep:
                    os.set_inheritable(fd, True)
                os.chdir(self.plan.get('cwd') or '/')
                profile = _read_frame(profile_r, PROFILE_LIMIT).decode('utf-8')
                argv = ['/usr/bin/sandbox-exec', '-p', profile, *launcher, '--job-anchor', str(self.jobdir)]
                os.execve(argv[0], argv, env)
            except BaseException as exc:
                try:
                    os.write(exec_w, ('exec_error %r' % exc).encode()[:200])
                except OSError:
                    pass
                os._exit(127)
        self.anchor_pid = pid
        os.close(exec_w)
        os.close(profile_r)
        for fd in keep:
            os.close(fd)
        failure = None
        try:
            profile = self._bound_profile().encode('utf-8')
            self.profile_sha256 = hashlib.sha256(profile).hexdigest()  # the exact text the job runs under
            _write_all(profile_w, ('%d\n' % len(profile)).encode() + profile)
        except _Unstarted as exc:
            failure = str(exc)
        except Exception:
            failure = 'profile_unavailable'
        finally:
            os.close(profile_w)  # without a profile the child reads EOF and exits before sandbox-exec
        detail = b''
        while True:
            chunk = os.read(exec_r, 200)
            if not chunk:
                break
            detail += chunk
        os.close(exec_r)
        if failure or detail:
            try:
                os.waitpid(pid, 0)  # our own child, which exits at once without exec
            except ChildProcessError:
                pass
            self.anchor_pid = None
            os.close(lifeline_w)
            os.close(status_r)
            os.close(out_r)
            raise OSError(failure or detail.decode('ascii', 'replace'))
        self.anchor_lifeline = lifeline_w
        self.status_r = status_r
        self.out_r = out_r
        os.set_blocking(self.status_r, False)
        os.set_blocking(self.out_r, False)
        # Hand the anchor its spec, then keep the lifeline open.
        if self.steps is not None:
            extra = self._network_env()
            spec = {'mode': 'plan', 'steps': [dict(step, env={**step['env'], **extra}) for step in self.steps],
                    'single': self.plan.get('single') is True, 'pty': self.pty, 'timeout': self.limits.get('timeout')}
        else:
            spec = {'mode': 'command', 'argv': self.plan['argv'], 'cwd': self.plan.get('cwd'),
                    'env': self.plan.get('command_env', self.plan['env']), 'timeout': self.limits.get('timeout')}
        if self.plan.get('interlock_abort_path'):
            # Tests only: the anchor proves SIGURG scoping before any SIGKILL.
            spec['interlock_abort_path'] = self.plan['interlock_abort_path']
        body = json.dumps(spec).encode()
        _write_all(self.anchor_lifeline, ('%d\n' % len(body)).encode() + body)

    def _bound_profile(self):
        """The plan's profile with its slots bound to this job's broker port and PTY slave."""
        from . import sandbox_profiles
        from .policy import PolicyError
        port = self._start_network() if self.network is not None else None
        try:
            return sandbox_profiles.bind(self.plan['profile'], broker_port=port, tty=self.slave_path)
        except PolicyError:
            raise _Unstarted('profile_unbound') from None

    # -- destination-scoped network (X04) --

    def _start_network(self):
        """Receive the credential values, grant them, zero the buffers, start the broker. Returns its port."""
        from .credential_broker import CredentialBroker
        from .egress import EgressBroker
        from .policy import PolicyError
        net = self.network
        refs = list(net.get('refs') or [])
        received = self._read_credentials(refs) if refs else {}
        try:
            grants = {}
            if refs:
                self.credentials = CredentialBroker(lambda ref: received[ref])
                expires = time.time() + float(net['lifetime'])
                for route in net['routes']:
                    if route.get('ref'):
                        grants[route['name']] = self.credentials.grant(route['ref'], route['origin'], route['methods'],
                                                                       route['path_prefix'], expires)
        except (PolicyError, KeyError, TypeError, ValueError):
            raise _Unstarted('credentials_unavailable') from None
        finally:
            for value in received.values():
                value[:] = bytes(len(value))
            received.clear()
        try:
            routes = {route['name']: {'origin': route['origin'], 'methods': route['methods'],
                                      'path_prefix': route['path_prefix'], 'grant': grants.get(route['name'])}
                      for route in net['routes']}
            self.broker = EgressBroker([tuple(pair) for pair in net['allow']] or None, routes=routes or None,
                                       credentials=self.credentials if routes and self.credentials else None,
                                       loopback_targets=net.get('loopback') is True,
                                       ca_file=net.get('ca_file') if routes else None,
                                       lifetime_seconds=float(net['lifetime']))
            self.broker.start()
        except (PolicyError, OSError, KeyError, TypeError, ValueError):
            raise _Unstarted('broker_unavailable') from None
        return self.broker.port

    def _read_credentials(self, refs):
        """{ref: bytearray} from the host's credential pipe; anything incomplete or extra is refused."""
        try:
            fd = int(os.environ['PEBBLE_SHIM_CREDENTIAL_FD'])
        except (KeyError, ValueError):
            raise _Unstarted('credentials_unavailable') from None
        buffer = bytearray(CREDENTIAL_LIMIT)
        view = memoryview(buffer)
        size, found = 0, None
        end = time.monotonic() + CREDENTIAL_SECONDS
        try:
            while found is None:
                left = end - time.monotonic()
                if left <= 0 or size == len(buffer):
                    break
                if not _ready([fd], selectors.EVENT_READ, left):
                    break
                count = os.readv(fd, [view[size:]])
                if count == 0:
                    break
                size += count
                found = _credentials(view, size)
        except OSError:
            found = None
        finally:
            view.release()
            buffer[:] = bytes(len(buffer))
            try:
                os.close(fd)
            except OSError:
                pass
        if found is None or sorted(found) != sorted(set(refs)):
            for value in (found or {}).values():
                value[:] = bytes(len(value))
            raise _Unstarted('credentials_unavailable')
        return found

    def _network_env(self):
        """Non-secret variables naming the broker: one route URL per credential route, and the CONNECT proxy."""
        if self.broker is None:
            return {}
        env = {}
        for route in self.network['routes']:
            env['PEBBLE_ROUTE_' + route['name'].upper().replace('-', '_')] = f'{self.broker.url}/r/{route["name"]}'
        if self.network.get('allow'):
            env['HTTPS_PROXY'] = env['https_proxy'] = self.broker.url
        return env

    def _close_network(self):
        """Teardown order: the broker first, then the credentials. Keeps the content-free route log."""
        if self.broker is not None:
            try:
                self.broker.close()
            except Exception:
                pass
            try:
                self.egress_log = _egress_records(self.broker.log())
            except Exception:
                self.egress_log = []
            self.broker = None
        if self.credentials is not None:
            try:
                self.credentials.close()
            except Exception:
                pass
            self.credentials = None

    # -- event loop --

    def _loop(self):
        last_sample = 0.0
        while not self.finished:
            rlist = [self.status_r, self.control] + ([] if self.output_full else [self.out_r])
            if self.master is not None and not self.output_full:
                rlist.append(self.master)
            if self.host_lifeline is not None:
                rlist.append(self.host_lifeline)
            rlist.extend(self.conns)
            try:
                ready = _ready(rlist, selectors.EVENT_READ, SAMPLE_SECONDS)
            except (OSError, ValueError):
                ready = []
            for fd in ready:
                if fd == self.out_r:
                    self._drain_output()
                elif self.master is not None and fd == self.master:
                    self._drain_output(self.master)
                elif fd == self.status_r:
                    self._drain_status()
                elif fd == self.control:
                    self._accept()
                elif fd == self.host_lifeline:
                    self._host_readable()
                elif fd in self.conns:
                    self._serve(fd)
            now = time.monotonic()
            if self.deadline is not None and now >= self.deadline:
                self._request_stop('timed_out')
            if self.stopping and not self.stop_sent and now - self.hangup_at >= HANGUP_SECONDS:
                self._send_stop()
            if now - last_sample >= SAMPLE_SECONDS:
                last_sample = now
                self._sample_memory()
            if self._complete():
                self.finished = True
            elif self.stop_at is not None and now - self.stop_at > STOP_GRACE_SECONDS:
                # The anchor never finished its sweep. Do not signal anything:
                # leave, and let the record say the containment is unproven.
                self.reason = 'containment_lost'
                self.finished = True

    def _complete(self):
        # Done when the anchor's output pipe closed and the anchor was reaped.
        if self.anchor_pid is None:
            return True
        try:
            pid, status = os.waitpid(self.anchor_pid, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == self.anchor_pid:
            self._drain_output()
            self._drain_status()
            self.anchor_reaped = True
            if not self.exit_seen:
                self.reason = self.reason or 'anchor_died'
            self.anchor_pid = None
            return True
        return False

    def _drain_output(self, fd=None):
        # Once the limit is reached the shim stops reading: the writer blocks on
        # the full pipe until the sweep kills it, so it cannot run on unseen.
        fd = self.out_r if fd is None else fd
        while not self.output_full and fd is not None:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                return
            except OSError:
                chunk = b''  # a PTY master reads EIO once every slave descriptor is closed
            if not chunk:
                if fd == self.master:
                    self._close_master()
                return
            room = self.output_limit - self.spool_bytes
            if len(chunk) > room:  # exactly filling the limit is not yet a loss
                self.dropped += len(chunk) - room  # an observed lower bound
                chunk = chunk[:room]
                self.output_full = True
            self.spool.write(chunk)
            self.spool.flush()
            os.fsync(self.spool.fileno())
            self.spool_bytes += len(chunk)
            if self.output_full:
                self._request_stop('output_limited')

    def _drain_status(self):
        try:
            data = os.read(self.status_r, 65536)
        except (BlockingIOError, OSError):
            return
        if not data:
            return
        self.status_buf += data
        while b'\n' in self.status_buf:
            line, self.status_buf = self.status_buf.split(b'\n', 1)
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self._handle_status(msg)

    def _handle_status(self, msg):
        event = msg.get('event')
        if event == 'started' and not self.started:
            self.started = True
            self._record({'record': 'started', 'steps': msg.get('steps') if type(msg.get('steps')) is int else None})
        elif event == 'exit':
            self.exit_seen = True
            self.exit_code = msg.get('code')
            reason = msg.get('reason')
            detail = msg.get('detail')
            if reason in ('plan_refused', 'step_spawn_failed') and isinstance(detail, str):
                reason = reason + ':' + ''.join(c for c in detail[:120] if c.isprintable())
            self.reason = self.reason or reason
            self._codes(msg.get('step_codes'))
        elif event == 'sweep' and msg.get('phase') == 'starting':
            self.sweep_started = True
        elif event == 'sweep' and msg.get('phase') == 'complete':
            self.contained = bool(msg.get('thorough'))
            if self.exit_code is None and type(msg.get('code')) is int:
                self.exit_code = msg['code']  # the command's status after the sweep stopped it
            if self.step_codes is None:
                self._codes(msg.get('step_codes'))

    def _codes(self, codes):
        if isinstance(codes, list) and len(codes) <= 4 and all(c is None or type(c) is int for c in codes):
            self.step_codes = codes

    # -- control --

    def _request_stop(self, reason):
        if self.stopping:
            return
        self.stopping = True
        self.stop_at = time.monotonic()
        self.reason = self.reason or reason
        # An interactive job is hung up first: closing the master sends SIGHUP to its
        # session. The anchor's sweep follows HANGUP_SECONDS later (see _loop).
        self.hangup_at = self.stop_at
        if self.master is not None:
            self._close_master()
            self._record({'record': 'hangup'})
        else:
            self._send_stop()

    def _send_stop(self):
        self.stop_sent = True
        if self.anchor_lifeline is not None:
            try:
                os.write(self.anchor_lifeline, b'\x00')
            except OSError:
                pass

    def _close_master(self):
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None

    def _open_control(self):
        path = control_path(self.jobdir)
        try:
            os.unlink(path)
        except OSError:
            pass
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.control.bind(str(path))
        os.chmod(path, 0o600)
        self.control.listen(CONTROL_BACKLOG)
        self.control.setblocking(False)

    def _accept(self):
        try:
            conn, _ = self.control.accept()
        except OSError:
            return
        if len(self.conns) >= MAX_CONNECTIONS:
            conn.close()  # bounded: a flood of idle connections cannot starve the loop
            return
        conn.setblocking(False)
        self.conns[conn] = b''

    def _serve(self, conn):
        try:
            data = conn.recv(REQUEST_LIMIT)
        except (BlockingIOError, OSError):
            return
        if not data:
            self._drop(conn)
            return
        self.conns[conn] += data
        if len(self.conns[conn]) > REQUEST_LIMIT or b'\n' in self.conns[conn]:
            line = self.conns[conn].split(b'\n', 1)[0]
            self._reply(conn, line)
            self._drop(conn)

    def _reply(self, conn, line):
        try:
            req = json.loads(line or b'{}')
        except ValueError:
            req = {}
        secret = req.get('secret') if isinstance(req, dict) else None
        if not isinstance(secret, str) or not hmac.compare_digest(secret.encode(), str(self.secret).encode()):
            self._send(conn, {'ok': False, 'error': 'unauthorized'})
            return
        op = req.get('op')
        if op == 'status':
            reply = {'ok': True, 'exit_seen': self.exit_seen, 'exit_code': self.exit_code,
                     'contained': self.contained, 'output_bytes': self.spool_bytes,
                     'reason': self.reason, 'stopping': self.stopping}
            if self.steps is not None:
                reply['started'] = self.started
            if self.pty:
                reply.update(echo=self._echo(), input_bytes=self.input_bytes, owned=self._owned())
            self._send(conn, reply)
        elif op == 'stop':
            self._request_stop('stopped')
            self._send(conn, {'ok': True})
        elif op == 'detach_ok':
            self._send(conn, {'ok': True})
        elif op == 'input':
            self._send(conn, self._input(req))
        elif op == 'claim':
            self._send(conn, self._claim(req))
        elif op == 'egress':
            log = _egress_records(self.broker.log()) if self.broker is not None else list(self.egress_log)
            self._send(conn, {'ok': True, 'log': log[-32:]})  # the newest; the full log is the egress record
        else:
            self._send(conn, {'ok': False, 'error': 'bad_op'})

    # -- interactive terminal (R07) --

    def _echo(self):
        """The terminal's ECHO bit read from the master; None when unreadable."""
        if self.master is None:
            return None
        try:
            return bool(termios.tcgetattr(self.master)[3] & termios.ECHO)
        except (termios.error, OSError):
            return None

    def _owned(self):
        return isinstance(self.input_owner, str) and len(self.input_owner) == 64

    def _claim(self, req):
        """A restarted host proves ownership: the secret, the launching host's lifeline gone, a fresh token."""
        if not self.pty:
            return {'ok': False, 'error': 'no_terminal'}
        if self.host_lifeline is not None:
            return {'ok': False, 'error': 'host_attached'}  # the launching host is still alive
        owner = req.get('owner')
        if not isinstance(owner, str) or not 32 <= len(owner) <= 128 or not all(c in '0123456789abcdef' for c in owner):
            return {'ok': False, 'error': 'bad_owner'}
        self.input_owner = hashlib.sha256(owner.encode()).hexdigest()
        self._record({'record': 'claim'})  # the token itself is never written
        return {'ok': True, 'echo': self._echo()}

    def _input(self, req):
        """Relay one validated write to the master. Every refusal leaves the terminal untouched."""
        from . import pty_input
        from .policy import PolicyError
        if not self.pty:
            return {'ok': False, 'error': 'no_terminal'}
        if self.stopping or self.exit_seen or self.master is None:
            return {'ok': False, 'error': 'stopped'}
        if not self.started:
            return {'ok': False, 'error': 'not_started'}
        owner = req.get('owner')
        if (not self._owned() or not isinstance(owner, str) or not hmac.compare_digest(
                hashlib.sha256(owner.encode()).hexdigest(), self.input_owner)):
            return {'ok': False, 'error': 'not_owner'}
        origin, kind, text = req.get('origin'), req.get('kind'), req.get('data')
        if origin not in pty_input.ORIGINS:
            return {'ok': False, 'error': 'bad_origin'}
        try:
            data = pty_input.check(kind, bytes.fromhex(text) if isinstance(text, str) else None)
        except (PolicyError, ValueError):
            return {'ok': False, 'error': 'bad_input'}
        if self.inputs is None:
            self.inputs = pty_input.Limiter()
        refusal = self.inputs.refusal(len(data))
        if refusal is not None:
            return {'ok': False, 'error': refusal}
        echo = self._echo()
        if echo is None:
            return {'ok': False, 'error': 'terminal_unavailable'}
        if not echo and origin != 'human':
            return {'ok': False, 'error': 'echo_off', 'echo': False}
        self.inputs.spend(len(data))
        written = self._write_master(data)
        self.input_bytes += written
        receipt = pty_input.receipt(origin, kind, data[:written])
        self._record({'record': 'input', **receipt, 'echo': echo})
        if written != len(data):
            return {'ok': False, 'error': 'busy', 'written': written, 'echo': echo}
        return {'ok': True, 'written': written, 'echo': echo}

    def _write_master(self, data):
        view, written = memoryview(data), 0
        end = time.monotonic() + INPUT_WRITE_SECONDS
        while written < len(data) and self.master is not None:
            try:
                written += os.write(self.master, view[written:])
            except BlockingIOError:
                left = end - time.monotonic()
                if left <= 0:
                    break
                try:
                    _ready([self.master], selectors.EVENT_WRITE, min(left, 0.05))
                except (OSError, ValueError):
                    break  # a wait that cannot run reports the bytes written so far ('busy'); it must not end the shim
            except OSError:
                break
        return written

    def _send(self, conn, obj):
        try:
            conn.settimeout(SEND_SECONDS)
            conn.sendall((json.dumps(obj) + '\n').encode())
        except OSError:
            pass

    def _drop(self, conn):
        self.conns.pop(conn, None)
        try:
            conn.close()
        except OSError:
            pass

    # -- host lifeline --

    def _open_host_lifeline(self):
        fd = os.environ.get('PEBBLE_SHIM_LIFELINE_FD')
        if fd is None:
            return
        try:
            self.host_lifeline = int(fd)
            os.set_blocking(self.host_lifeline, False)
        except (ValueError, OSError):
            self.host_lifeline = None

    def _host_readable(self):
        try:
            data = os.read(self.host_lifeline, 64)
        except (BlockingIOError, OSError):
            return
        if data == b'':  # the backend is gone
            try:
                os.close(self.host_lifeline)
            except OSError:
                pass
            self.host_lifeline = None
            if self.on_disconnect == 'continue':
                self._record({'record': 'host_disconnect', 'action': 'continue'})
            else:
                self._record({'record': 'host_disconnect', 'action': 'terminate'})
                self._request_stop('host_disconnect')

    # -- memory governor --

    def _sample_memory(self):
        budget = self.limits.get('memory_bytes')
        if not budget or self.anchor_pid is None:
            return
        used = _footprint_tree(self.anchor_pid)
        if used is None:
            return  # unobservable this tick; the deadline still bounds the job
        if used != float('inf'):
            self.peak_memory = max(self.peak_memory, int(used))
        if used > budget:
            self._request_stop('memory')

    # -- settle --

    def _settle(self):
        # Wait briefly for the anchor's sweep to complete, then reap it.
        end = time.monotonic() + GRACE_SECONDS
        while self.anchor_pid is not None and time.monotonic() < end:
            self._drain_status()
            try:
                pid, _ = os.waitpid(self.anchor_pid, os.WNOHANG)
            except ChildProcessError:
                self.anchor_pid = None
                break
            if pid == self.anchor_pid:
                self.anchor_pid = None
                break
            time.sleep(0.02)
        self._drain_output()
        if self.master is not None:
            self._drain_output(self.master)
        self._drain_status()
        contained = self.contained and self.sweep_started
        reason = self.reason or ('command_exit' if self.exit_seen else 'unknown')
        if not self.exit_seen and not self.sweep_started:
            reason = 'containment_lost'
        if self.network is not None:
            self._close_network()  # the broker, then the credentials
            self._record({'record': 'egress', 'log': self.egress_log})
        exited = {'record': 'exited', 'exit_code': self.exit_code, 'contained': bool(contained),
                  'reason': reason, 'output_bytes': self.spool_bytes, 'dropped_bytes': self.dropped,
                  'peak_memory_bytes': self.peak_memory}
        if self.steps is not None:
            exited['step_codes'] = self.step_codes
        self._record(exited)
        self._record({'record': 'teardown'})

    def _record(self, record):
        _write_record(self.records, record)

    def _close(self):
        self._close_master()
        self._close_network()
        for fd in (self.anchor_lifeline, self.status_r, self.out_r):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        for conn in list(self.conns):
            self._drop(conn)
        if self.control is not None:
            try:
                self.control.close()
            except OSError:
                pass
            try:
                os.unlink(control_path(self.jobdir))
            except (OSError, RuntimeError):
                pass
        for handle in (self.spool, self.records):
            try:
                handle.close()
            except OSError:
                pass


# --- governors --------------------------------------------------------------

def _apply_governors(limits):
    """Best-effort rlimits and background priority in the shim's child before exec."""
    def setlimit(which, value):
        if value is None:
            return
        try:
            soft, hard = resource.getrlimit(which)
            cap = value if hard == resource.RLIM_INFINITY else min(value, hard)
            resource.setrlimit(which, (cap, cap if hard == resource.RLIM_INFINITY else hard))
        except (OSError, ValueError, OverflowError):
            pass

    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError):
        pass
    cpu = limits.get('cpu_seconds')
    if cpu:
        setlimit(resource.RLIMIT_CPU, int(cpu))
    setlimit(resource.RLIMIT_FSIZE, limits.get('fsize_bytes'))
    setlimit(resource.RLIMIT_NOFILE, limits.get('nofile'))
    nproc = limits.get('nproc')
    if nproc:
        base = _count_pids()
        setlimit(resource.RLIMIT_NPROC, (base + int(nproc)) if base is not None else int(nproc))
    _background_priority()


def _background_priority():
    # PRIO_DARWIN_PROCESS=4, PRIO_DARWIN_BG=0x1000; background CPU and I/O.
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.setpriority(4, 0, 0x1000)
    except (OSError, AttributeError):
        pass


# --- libproc sampling (read-only) ------------------------------------------

def _libproc_handle():
    global _libproc
    if _libproc is None:
        try:
            lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            lib.proc_pid_rusage.restype = ctypes.c_int
            lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            lib.proc_listpids.restype = ctypes.c_int
            lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
            _libproc = lib
        except (OSError, AttributeError):
            _libproc = False
    return _libproc or None


def _count_pids():
    """Count of this real UID's processes, read from the kernel's filtered pid list.

    A size query (NULL buffer) ignores the filter and answers the whole system's count plus 20, which gave
    an `nproc` limit every other user's processes as extra headroom (Phase 19 trial 3: about 287 for 8)."""
    lib = _libproc_handle()
    if lib is None:
        return None
    capacity = 1024
    for _ in range(4):
        buf = (ctypes.c_int32 * capacity)()
        written = lib.proc_listpids(PROC_RUID_ONLY, os.getuid(), buf, ctypes.sizeof(buf))
        if written <= 0:
            return None
        if written < ctypes.sizeof(buf):
            return sum(1 for i in range(written // 4) if buf[i] > 0)
        capacity *= 4
    return None


def _children(pid):
    """Direct children of one pid in this job's tree; None if unreadable."""
    lib = _libproc_handle()
    if lib is None:
        return None
    capacity = 256
    for _ in range(4):
        buf = (ctypes.c_int32 * capacity)()
        written = lib.proc_listpids(PROC_PPID_ONLY, pid, buf, ctypes.sizeof(buf))
        if written < 0:
            return None
        if written < ctypes.sizeof(buf):
            return [buf[i] for i in range(written // 4) if buf[i] > 0]
        capacity *= 4
    return None


def _footprint(pid):
    lib = _libproc_handle()
    if lib is None:
        return 0
    buf = (ctypes.c_byte * 512)()
    if lib.proc_pid_rusage(pid, RUSAGE_INFO_V2, buf) != 0:
        return 0
    return struct.unpack_from('<Q', bytes(buf), RI_PHYS_FOOTPRINT_OFFSET)[0]


def _footprint_tree(root):
    """Sum ri_phys_footprint over the anchor and its descendants, walked by parent.

    Only this job's own tree is listed (PROC_PPID_ONLY from the anchor down).
    A member reparented to launchd (setsid + double fork) leaves this view, so
    the sum underestimates escapees; see the note. Returns None when the walk
    cannot be read, and infinity when the tree exceeds TREE_LIMIT.
    """
    members, frontier = [root], [root]
    while frontier:
        kids = _children(frontier.pop())
        if kids is None:
            return None
        for kid in kids:
            if kid not in members:
                members.append(kid)
                frontier.append(kid)
        if len(members) > TREE_LIMIT:
            return float('inf')
    return sum(_footprint(pid) for pid in members)


# --- reattach ---------------------------------------------------------------

def attach(jobdir):
    """Reattach to a shim's job. Never signals a stored PID."""
    return JobHandle(Path(jobdir))


class JobHandle:
    def __init__(self, jobdir):
        self.jobdir = Path(jobdir)
        try:
            self.secret = _read_plan(self.jobdir)['secret']
        except (OSError, ValueError):
            self.secret = None

    def alive(self):
        """True if a shim still holds the lock. A free lock proves it is gone."""
        path = self.jobdir / LOCK
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True  # someone holds it: the shim is alive
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)  # F21-fd: a live job's probe kept its descriptor open (one per poll)

    def records(self):
        return read_records(self.jobdir)

    def read_spool(self, offset=0, limit=65536):
        path = self.jobdir / SPOOL
        try:
            with open(path, 'rb') as handle:
                handle.seek(max(0, int(offset)))
                return handle.read(max(0, int(limit)))
        except OSError:
            return b''

    def status(self):
        """Live status over the control socket; None if the shim is gone."""
        reply = self._control('status')
        return reply

    def stop(self):
        return self._control('stop')

    def input(self, data, *, origin, kind, owner):
        """Relay pty_input-encoded bytes to an interactive job; the shim re-validates everything."""
        return self._control('input', origin=origin, kind=kind, data=bytes(data).hex(), owner=owner)

    def claim(self, owner):
        """After a host restart: become the job's input owner (refused while the launching host lives)."""
        return self._control('claim', owner=owner)

    def egress(self):
        """The live route log (no path, no credential), or None if the shim is gone."""
        reply = self._control('egress')
        return reply.get('log') if isinstance(reply, dict) and reply.get('ok') else None

    def egress_record(self):
        """The route log the shim recorded at teardown, or None."""
        return next((r.get('log') for r in reversed(self.records()) if r.get('record') == 'egress'), None)

    def settlement(self):
        """('contained', code) once the shim recorded a contained exit; else ('unknown', code)."""
        code = None
        for record in self.records():
            if record.get('record') == 'exited':
                code = record.get('exit_code')
                if record.get('contained'):
                    return ('contained', code)
                return ('unknown', code)
        return ('unknown', code)

    def _control(self, op, **fields):
        if self.secret is None:
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(str(control_path(self.jobdir)))
        except (OSError, RuntimeError):
            return None
        try:
            sock.sendall((json.dumps({**fields, 'secret': self.secret, 'op': op}) + '\n').encode())
            data = b''
            while b'\n' not in data and len(data) < REPLY_LIMIT:
                chunk = sock.recv(REQUEST_LIMIT)
                if not chunk:
                    break
                data += chunk
            return json.loads(data.split(b'\n', 1)[0] or b'{}')
        except (OSError, ValueError):
            return None
        finally:
            sock.close()
