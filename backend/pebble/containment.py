"""Sandbox-instance containment: the job anchor and its startup canary.

The anchor runs *inside* the job's sandbox instance as the job leader. It
starts the reviewed command as its only child and holds a lifeline to the
outside shim. On the command's exit, lifeline EOF, a stop byte, its own
deadline or an unignorable kill it performs a *sweep*: it broadcasts SIGKILL
to the whole sandbox instance and keeps going until its own subtree is gone.

Why a broadcast and not a process group: a descendant that calls setsid or
double-forks leaves the group and is reparented to launchd, but it keeps the
sandbox membership it inherited. Phase 14 E1/E1d proved that, inside an
instance whose profile has (allow signal (target same-sandbox)), kill(-1)
reaches every member of *that instance* and nothing outside it, regardless of
process group. E1d used os.kill(-1) (POSIX semantics) and the killer died in
its own broadcast. This module instead issues the raw __kill(-1, sig, 0) with
the BSD `posix=0` flag, which excludes the caller: verified on this Mac, the
anchor survives, reaps its own subtree and reports a completed sweep, while the
member and a setsid/double-fork escapee both die and an outside canary lives.

The anchor never touches a PID outside its own instance, and it never reads a
stored PID: kill(-1) is instance-scoped by the kernel, not by any list we keep.
"""

import ctypes
import errno
import json
import os
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # the project root; ROOT/backend holds the package
# Catchable signals the anchor refuses during a sweep, so a late SIGTERM/SIGHUP
# from the shim or a tty does not stop it mid-teardown. SIGKILL cannot be caught
# and is excluded from the anchor only by the posix=0 broadcast flag.
SWEEP_IGNORE = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGPIPE,
                signal.SIGURG, signal.SIGTTIN, signal.SIGTTOU, signal.SIGTSTP)
SWEEP_ROUND = 0.01          # pause between broadcast rounds
SWEEP_SETTLE_ROUNDS = 3     # consecutive ECHILD rounds that prove the subtree is gone
SWEEP_DEADLINE = 8.0        # hard cap; a sweep that cannot finish reports thorough=False
POLL_SECONDS = 0.05
SPEC_LIMIT = 256 * 1024

_libc = None


def _libc_kill():
    """Bind the raw __kill(pid, sig, posix) once; posix=0 is BSD broadcast semantics."""
    global _libc
    if _libc is None:
        lib = ctypes.CDLL(None, use_errno=True)
        lib.__kill.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.__kill.restype = ctypes.c_int
        _libc = lib
    return _libc


def broadcast(sig, *, include_self):
    """kill(-1, sig): reach every process this one may signal. Returns errno (0 on success).

    include_self=False uses the BSD posix=0 flag, which excludes the caller, so
    the anchor stays alive to prove the sweep finished. The kernel restricts the
    reach to the sandbox instance; this call never consults a PID list.
    """
    lib = _libc_kill()
    ctypes.set_errno(0)
    result = lib.__kill(-1, int(sig), 0 if not include_self else 1)
    return 0 if result == 0 else ctypes.get_errno()


def _reap_own(on_reap=None):
    """Reap the anchor's exited children; True once none remain (ECHILD)."""
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            return False
        if on_reap is not None:
            on_reap(pid, status)


def sweep(*, interlock=None, deadline=None, on_reap=None):
    """Broadcast SIGKILL across the instance until this subtree is gone.

    interlock(), when given, must return True before any SIGKILL is sent; a
    False aborts the sweep (used by the startup canary and tests to confirm,
    with SIGURG, that an outside canary is not in reach). on_reap(pid, status)
    sees each of the anchor's own children as it is reaped. Returns a dict with
    `thorough` True only when the anchor's own subtree was fully reaped.
    """
    for sig in SWEEP_IGNORE:
        try:
            signal.signal(sig, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
    if interlock is not None and not interlock():
        return {'thorough': False, 'reason': 'interlock_refused', 'rounds': 0}
    end = time.monotonic() + (SWEEP_DEADLINE if deadline is None else deadline)
    rounds, settled = 0, 0
    while True:
        code = broadcast(signal.SIGKILL, include_self=False)
        rounds += 1
        empty = _reap_own(on_reap)
        if code == errno.ESRCH:
            return {'thorough': True, 'reason': 'esrch', 'rounds': rounds}
        settled = settled + 1 if empty else 0
        if settled >= SWEEP_SETTLE_ROUNDS:
            return {'thorough': True, 'reason': 'subtree_reaped', 'rounds': rounds}
        if time.monotonic() >= end:
            return {'thorough': False, 'reason': 'deadline', 'rounds': rounds}
        time.sleep(SWEEP_ROUND)


# --- anchor process ---------------------------------------------------------

def anchor_main(argv):
    """Entry for `pebble-backend --job-anchor`. Runs inside the job's sandbox.

    FD numbers arrive in the environment (no secrets, no stored PIDs):
      PEBBLE_ANCHOR_SPEC_FD    shim->anchor: a length-prefixed JSON spec, then
                               the lifeline (a stop byte, or EOF if the shim dies)
      PEBBLE_ANCHOR_STATUS_FD  anchor->shim: JSON lines (started, exit, sweep)
      PEBBLE_ANCHOR_OUT_FD     the reviewed command's stdout+stderr sink
      PEBBLE_ANCHOR_TTY_FD     optional: an interactive plan's PTY slave
    """
    try:
        spec_fd = int(os.environ['PEBBLE_ANCHOR_SPEC_FD'])
        status_fd = int(os.environ['PEBBLE_ANCHOR_STATUS_FD'])
        out_fd = int(os.environ['PEBBLE_ANCHOR_OUT_FD'])
        tty_fd = int(os.environ['PEBBLE_ANCHOR_TTY_FD']) if 'PEBBLE_ANCHOR_TTY_FD' in os.environ else None
    except (KeyError, ValueError):
        return 2
    spec = _read_spec(spec_fd)
    if spec is None or bool(spec.get('pty')) != (tty_fd is not None):
        return 2
    anchor = _Anchor(spec, spec_fd, status_fd, out_fd, tty_fd)
    return anchor.run()


def _read_spec(fd):
    header = b''
    while b'\n' not in header:
        try:
            byte = os.read(fd, 1)
        except OSError:
            return None
        if not byte:
            return None
        header += byte
        if len(header) > 16:
            return None
    try:
        length = int(header.strip())
    except ValueError:
        return None
    if not 0 < length <= SPEC_LIMIT:
        return None
    body = b''
    while len(body) < length:
        chunk = os.read(fd, length - len(body))
        if not chunk:
            return None
        body += chunk
    try:
        spec = json.loads(body)
    except ValueError:
        return None
    if isinstance(spec, dict) and spec.get('mode') == 'plan':
        from . import plan_runner
        return spec if plan_runner.valid(spec) else None
    if not isinstance(spec, dict) or not isinstance(spec.get('argv'), list) or not spec['argv']:
        return None
    return spec


class _Anchor:
    def __init__(self, spec, lifeline_fd, status_fd, out_fd, tty_fd=None):
        self.spec = spec
        self.lifeline_fd = lifeline_fd
        self.status_fd = status_fd
        self.out_fd = out_fd
        self.tty_fd = tty_fd
        self.child = None
        self.exit_status = None
        self.steps = []     # plan mode: step PIDs in pipeline order (own children; reaped here only)
        self.codes = {}     # plan mode: pid -> exit code once reaped

    def run(self):
        os.set_blocking(self.lifeline_fd, False)
        deadline = None
        timeout = self.spec.get('timeout')
        if isinstance(timeout, (int, float)) and timeout > 0:
            deadline = time.monotonic() + float(timeout)
        if self.spec.get('mode') == 'plan':
            return self._run_plan(deadline)
        try:
            self._spawn()
        except OSError:
            self._status({'event': 'exit', 'code': None, 'reason': 'anchor_spawn_failed'})
            self._run_sweep('spawn_failed')
            return 0
        reason = self._supervise(deadline)
        self._run_sweep(reason)
        return 0

    def _spawn(self):
        """Fork+exec the reviewed command (or, in self-test mode, run a worker function)."""
        # The exec-status pipe stays close-on-exec: a successful execve closes
        # the write end, so the parent reads EOF and knows the exec succeeded.
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            try:
                os.close(read)
                os.setsid()
                devnull = os.open(os.devnull, os.O_RDONLY)
                os.dup2(devnull, 0)
                os.dup2(self.out_fd, 1)
                os.dup2(self.out_fd, 2)
                for fd in (self.lifeline_fd, self.status_fd, self.out_fd, devnull):
                    if fd > 2:
                        os.close(fd)
                if self.spec.get('mode') == 'selftest':
                    os.close(write)
                    _selftest_worker(self.spec)
                    os._exit(0)
                cwd = self.spec.get('cwd') or os.getcwd()
                env = self.spec.get('env') or {}
                os.chdir(cwd)
                os.execve(self.spec['argv'][0], self.spec['argv'], env)
            except BaseException as exc:
                try:
                    os.write(write, ('exec_error %r' % exc).encode()[:200])
                except OSError:
                    pass
                os._exit(127)
        self.child = pid  # known before the exec verdict, so the sweep can reap its status
        os.close(write)
        # Block until exec closes the write end (success) or writes an error.
        data = b''
        while True:
            try:
                piece = os.read(read, 200)
            except OSError:
                break
            if not piece:
                break
            data += piece
        os.close(read)
        if data:
            raise OSError(data.decode('ascii', 'replace'))

    def _run_plan(self, deadline):
        """Plan mode: re-check every step, start the pipeline, supervise it, then sweep.

        A refusal before the first exec ('plan_refused') means nothing of the
        command ran; a later step that cannot exec ('step_spawn_failed') leaves
        earlier steps' effects possible. Either way the sweep still runs.
        """
        from . import plan_runner
        try:
            try:
                plan_runner.check(self.spec)
                plan_runner.start(self.spec, self.out_fd, self.tty_fd, self.steps,
                                  on_first=lambda: self._status({'event': 'started', 'steps': len(self.spec['steps'])}))
            finally:
                if self.tty_fd is not None:
                    os.close(self.tty_fd)  # the step holds the terminal; the master sees EOF when it ends
                    self.tty_fd = None
        except plan_runner.Refused as exc:
            self._status({'event': 'exit', 'code': None, 'reason': 'plan_refused', 'detail': str(exc)[:120]})
            self._run_sweep('plan_refused')
            return 0
        except plan_runner.StepFailed as exc:
            reason = 'plan_refused' if exc.index == 0 else 'step_spawn_failed'
            self._status({'event': 'exit', 'code': None, 'reason': reason, 'detail': f'steps[{exc.index}].exec'})
            self._run_sweep(reason)
            return 0
        except Exception as exc:
            reason = 'step_spawn_failed' if self.steps else 'plan_refused'
            self._status({'event': 'exit', 'code': None, 'reason': reason, 'detail': type(exc).__name__})
            self._run_sweep(reason)
            return 0
        reason = self._supervise(deadline)
        self._run_sweep(reason)
        return 0

    def _steps_exited(self):
        """Plan mode: (True, pipefail code) once every step was reaped; never waits on anything else."""
        from . import plan_runner
        for pid in self.steps:
            if pid in self.codes:
                continue
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                self.codes[pid] = None  # gone without an observed status
                continue
            if done == pid:
                self.codes[pid] = _exit_code(status)
        if len(self.codes) < len(self.steps):
            return False, None
        return True, plan_runner.exit_code([self.codes[pid] for pid in self.steps])

    def _supervise(self, deadline):
        """Wait for the command's exit, a lifeline stop/EOF or the deadline."""
        while True:
            if self.steps:
                exited, code = self._steps_exited()
                if exited:
                    self.exit_status = code
                    self._status({'event': 'exit', 'code': code, 'reason': 'command_exit',
                                  'step_codes': [self.codes[pid] for pid in self.steps]})
                    return 'command_exit'
                exited = False
            else:
                exited, status = self._child_exited()
            if exited:
                self.exit_status = status
                self._status({'event': 'exit', 'code': _exit_code(status), 'reason': 'command_exit'})
                return 'command_exit'
            signal_stop = self._lifeline()
            if signal_stop is not None:
                self._status({'event': 'exit', 'code': None, 'reason': signal_stop})
                return signal_stop
            if deadline is not None and time.monotonic() >= deadline:
                self._status({'event': 'exit', 'code': None, 'reason': 'deadline'})
                return 'deadline'
            time.sleep(POLL_SECONDS)

    def _child_exited(self):
        """(True, status) once the reviewed command exits; (False, None) while it runs."""
        try:
            pid, status = os.waitpid(self.child, os.WNOHANG)
        except ChildProcessError:
            return True, None  # already gone; outcome unobservable
        if pid == self.child:
            return True, status
        return False, None

    def _lifeline(self):
        """Return a stop reason if the shim asked to stop or died, else None."""
        try:
            data = os.read(self.lifeline_fd, 64)
        except BlockingIOError:
            return None
        except OSError:
            return 'lifeline_error'
        if data == b'':
            return 'lifeline_eof'  # the shim is gone
        return 'stop_requested'

    def _run_sweep(self, reason):
        self._status({'event': 'sweep', 'phase': 'starting', 'reason': reason})
        interlock = None
        abort_path = self.spec.get('interlock_abort_path')
        if abort_path:
            # Self-test / tests only: SIGURG first, abort if an outside canary is reached.
            def interlock():
                broadcast(signal.SIGURG, include_self=False)
                time.sleep(0.5)
                return not os.path.exists(abort_path)
        swept = {}
        natural = self.exit_status is not None

        def on_reap(pid, status):
            if pid == self.child and self.exit_status is None:
                swept['code'] = _exit_code(status)  # the command, stopped by this sweep
            if pid in self.steps and pid not in self.codes:
                self.codes[pid] = _exit_code(status)  # a plan step, stopped by this sweep

        result = sweep(interlock=interlock, on_reap=on_reap)
        if self.steps and not natural:
            from . import plan_runner
            codes = [self.codes.get(pid) for pid in self.steps]
            swept['step_codes'] = codes
            if plan_runner.exit_code(codes) is not None:
                swept['code'] = plan_runner.exit_code(codes)
        self._status({'event': 'sweep', 'phase': 'complete', **result, **swept})

    def _status(self, message):
        try:
            os.write(self.status_fd, (json.dumps(message) + '\n').encode())
        except OSError:
            pass


def _exit_code(status):
    if status is None:
        return None
    try:
        return os.waitstatus_to_exitcode(status)
    except ValueError:
        return None


def _selftest_worker(spec):
    """A leader for self_test: one in-group member and one setsid/double-fork escapee."""
    base = Path(spec['heartbeat_dir'])
    _beat_child(base / 'member')
    if os.fork() == 0:
        os.setsid()
        if os.fork() == 0:
            _beat(base / 'escapee')
            os._exit(0)
        os._exit(0)
    os.wait()
    # Let the anchor drive the sweep; just keep the member/escapee alive meanwhile.
    _beat(base / 'leader')


def _beat_child(path):
    if os.fork() == 0:
        _beat(path)
        os._exit(0)


def _beat(path):
    end = time.time() + 20
    while time.time() < end:
        try:  # write then rename: a reader never sees a truncated heartbeat
            Path(str(path) + '.t').write_text(str(time.time()))
            os.replace(str(path) + '.t', str(path) + '.beat')
        except OSError:
            pass
        time.sleep(0.05)


# --- launcher ---------------------------------------------------------------

def launcher(frozen):
    """(argv_prefix, read_paths) for running the anchor/shim dispatch.

    Frozen: the bundled pebble-backend binary (sys.executable); its bundle
    folder must be readable. Source: the current interpreter plus backend/main.py.
    The returned read_paths are folders the job profile must allow reading so
    the launcher can start inside the sandbox.
    """
    if frozen:
        executable = Path(sys.executable).resolve()
        bundle = Path(getattr(sys, '_MEIPASS', executable.parent)).resolve()
        return [str(executable)], [executable.parent, bundle]
    main = ROOT / 'backend' / 'main.py'
    # argv keeps sys.executable as-is; the caller resolves it for the profile's
    # exec rule. Reads cover the backend package and the interpreter runtime.
    reads = [ROOT / 'backend', Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]
    return [sys.executable, str(main)], reads


# --- startup canary ---------------------------------------------------------

def self_test(home):
    """Fail-closed startup canary: does a real sweep contain a real escapee?

    Inside a fresh sandbox instance it starts a child and a setsid/double-fork
    escapee, then confirms the sweep leaves neither alive (by heartbeat file)
    while an outside canary stays fresh. Returns {'contained', 'reason',
    'elapsed'}. Any error is reported as not contained.
    """
    import subprocess
    import tempfile
    import uuid
    from . import sandbox_profiles

    started = time.monotonic()
    try:
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
            return _verdict(False, 'unsupported_platform', started)
        frozen = bool(getattr(sys, 'frozen', False))
        prefix, launcher_reads = launcher(frozen)
        with tempfile.TemporaryDirectory(dir=home, prefix='pebble-selftest-') as work:
            work = Path(work).resolve()
            beats = work / 'beats'
            beats.mkdir()
            canary_beat = work / 'canary'
            abort_path = beats / 'canary.urg'
            canary = _start_canary(canary_beat, abort_path)
            try:
                marker = uuid.uuid4().hex
                profile = sandbox_profiles.build(
                    executable=Path(prefix[0]).resolve(), exec_paths=launcher_reads, read_paths=launcher_reads,
                    project_root=work, temp=beats, writable_paths=[beats], job_marker=marker,
                    private_deny=False)
                spec = {'mode': 'selftest', 'argv': ['selftest'], 'heartbeat_dir': str(beats),
                        'interlock_abort_path': str(abort_path), 'timeout': 2.0}
                code, detail = _run_anchor(profile, prefix, spec, beats)
                if code != 0:
                    return _verdict(False, 'anchor_exit_%s:%s' % (code, detail), started)
                time.sleep(1.0)
                now = time.time()
                ages = {name: _age(beats / (name + '.beat'), now) for name in ('member', 'escapee')}
                canary_age = _age(Path(str(canary_beat) + '.beat'), now)
                if abort_path.exists():
                    return _verdict(False, 'interlock_canary_reached', started)
                if canary_age is None or canary_age > 1.0:
                    return _verdict(False, 'canary_not_fresh', started)
                for name, age in ages.items():
                    if age is None:
                        return _verdict(False, '%s_never_ran' % name, started)
                    if age < 1.0:
                        return _verdict(False, '%s_survived' % name, started)
                return _verdict(True, 'escapees_contained', started)
            finally:
                _stop_canary(canary)
    except Exception as exc:
        return _verdict(False, 'error:%s' % type(exc).__name__, started)


def _run_anchor(profile, prefix, spec, beats):
    import subprocess

    lifeline_r, lifeline_w = os.pipe()
    status_r, status_w = os.pipe()
    out_fd = os.open(os.devnull, os.O_WRONLY)  # pass_fds makes these inheritable in the anchor only
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(beats), 'TMPDIR': str(beats), 'LANG': 'en_US.UTF-8',
           'PEBBLE_ANCHOR_SPEC_FD': str(lifeline_r), 'PEBBLE_ANCHOR_STATUS_FD': str(status_w),
           'PEBBLE_ANCHOR_OUT_FD': str(out_fd)}
    if not getattr(sys, 'frozen', False):
        # Source mode: let the launcher import the pebble package from the checkout.
        env['PYTHONPATH'] = str(ROOT / 'backend')
    argv = ['/usr/bin/sandbox-exec', '-p', profile, *prefix, '--job-anchor', str(beats)]
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, pass_fds=(lifeline_r, status_w, out_fd),
                            start_new_session=True, close_fds=True)
    os.close(lifeline_r)
    os.close(status_w)
    os.close(out_fd)
    body = json.dumps(spec).encode()
    try:
        os.write(lifeline_w, ('%d\n' % len(body)).encode() + body)
    except OSError:
        pass
    try:
        proc.wait(timeout=SWEEP_DEADLINE + 8)
    except Exception:
        proc.kill()
        proc.wait()
    os.close(lifeline_w)
    os.close(status_r)
    err = proc.stderr.read(2000).decode('ascii', 'replace') if proc.stderr else ''
    return proc.returncode, err.strip()


def _start_canary(beat, abort_path):
    """An unsandboxed outside process, started through the launcher so the frozen app needs no interpreter."""
    import subprocess

    prefix, _ = launcher(bool(getattr(sys, 'frozen', False)))
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}
    if not getattr(sys, 'frozen', False):
        env['PYTHONPATH'] = str(ROOT / 'backend')
    return subprocess.Popen([*prefix, '--job-canary', str(beat), str(abort_path)], env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def canary_main(argv):
    """`--job-canary <beat> <urg>`: beat for 30 s; record any SIGURG that reaches this outside process."""
    import signal

    if len(argv) != 4:
        return 2
    beat, urg = argv[2], argv[3]
    signal.signal(signal.SIGURG, lambda *_: open(urg, 'a').write('x'))
    end = time.time() + 30
    while time.time() < end:
        try:  # write then rename: a reader never sees a truncated heartbeat
            Path(beat + '.t').write_text(str(time.time()))
            os.replace(beat + '.t', beat + '.beat')
        except OSError:
            pass
        time.sleep(0.05)
    return 0


def _stop_canary(canary):
    try:
        canary.terminate()
        canary.wait(timeout=5)
    except Exception:
        try:
            canary.kill()
        except OSError:
            pass


def _age(path, now):
    try:
        return round(now - float(path.read_text()), 2)
    except (OSError, ValueError):
        return None


def _verdict(contained, reason, started):
    return {'contained': bool(contained), 'reason': reason, 'elapsed': round(time.monotonic() - started, 3)}
