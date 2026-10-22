"""Anchor-side launcher for the steps of a typed command plan.

The containment anchor (containment.py) runs this inside the job's sandbox
instance for a spec in mode 'plan'. Before any step starts, check() re-reads
every step's pinned executable (realpath, size and SHA-256 through a
no-follow descriptor) and re-resolves its working folder. Drift refuses the
whole plan with nothing started. start() then forks the steps left to right
as the anchor's children, with no shell:

- step i's stdout feeds step i+1's stdin through an anchor-created pipe;
- every step's stderr and the last step's stdout go to the job output;
- the first step reads /dev/null, or, for an interactive plan (one step), the
  job's PTY slave becomes stdin, stdout, stderr and its controlling terminal
  (setsid, then TIOCSCTTY);
- SIGPIPE and SIGXFSZ get their default action back, as under a shell.

process_model 'single': the anchor must stay in the instance as its killer,
so it still forks the step, but each step gets RLIMIT_NPROC soft and hard 1
before exec. An unprivileged process cannot raise a hard limit, and XNU's
fork1 (fork, vfork and posix_spawn) refuses a new process to a real UID that
already has one, so the command can never create a descendant. A sandbox
'(deny process-fork)' would also refuse the anchor's own fork, which is why
it is not used here.

Nothing here signals anything: the anchor's sandbox-instance sweep does every
stop. Exit statuses follow pipefail: the rightmost nonzero status, else 0.
"""

import fcntl
import hashlib
import os
import resource
import signal
import stat
import termios

MAX_STEPS = 4
MAX_ARGV = 256
HASH_LIMIT = 512 * 1024 * 1024
FD_CEILING = 65536
STEP_KEYS = frozenset({'argv', 'cwd', 'env', 'pin'})
PIN_KEYS = frozenset({'realpath', 'sha256', 'size'})


class Refused(Exception):
    """The plan drifted or cannot start; no step was started. str() names the field."""


class StepFailed(Exception):
    """Step `index` could not exec; steps before it are running."""

    def __init__(self, index, detail):
        super().__init__(detail)
        self.index = index


def valid(spec):
    """Shape check for the anchor's spec parser: True only for a well-formed plan spec."""
    steps = spec.get('steps')
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return False
    if type(spec.get('single')) is not bool or type(spec.get('pty')) is not bool or spec['pty'] and len(steps) != 1:
        return False
    for step in steps:
        if not isinstance(step, dict) or set(step) != STEP_KEYS:
            return False
        argv, env, pin = step['argv'], step['env'], step['pin']
        if (not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV
                or not all(isinstance(a, str) and '\x00' not in a for a in argv)
                or not isinstance(step['cwd'], str) or not step['cwd'].startswith('/')
                or not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
                or not isinstance(pin, dict) or set(pin) != PIN_KEYS or pin['realpath'] != argv[0]
                or type(pin['size']) is not int or not isinstance(pin['sha256'], str)):
            return False
    return True


def check(spec):
    """Re-verify every step right before any exec. Raises Refused naming the first drifted field."""
    for index, step in enumerate(spec['steps']):
        field = f'steps[{index}]'
        pin = step['pin']
        path = pin['realpath']
        try:
            if os.path.realpath(path) != path:
                raise Refused(f'{field}.executable: realpath')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise Refused(f'{field}.executable: unavailable') from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o002:
                raise Refused(f'{field}.executable: mode')  # recipes.pin refuses world-writable too
            if info.st_size != pin['size']:
                raise Refused(f'{field}.executable: size')
            digest, total = hashlib.sha256(), 0
            while chunk := os.read(fd, 1 << 20):
                digest.update(chunk)
                total += len(chunk)
                if total > HASH_LIMIT:
                    raise Refused(f'{field}.executable: size')
            if total != pin['size'] or digest.hexdigest() != pin['sha256']:
                raise Refused(f'{field}.executable: sha256')
        except OSError:
            raise Refused(f'{field}.executable: unavailable') from None
        finally:
            os.close(fd)
        cwd = step['cwd']
        try:
            if os.path.realpath(cwd) != cwd or not stat.S_ISDIR(os.stat(cwd).st_mode):
                raise Refused(f'{field}.cwd')
        except OSError:
            raise Refused(f'{field}.cwd') from None


def start(spec, out_fd, tty_fd, pids, on_first=None):
    """Fork every step; append each PID to `pids` as it is forked.

    on_first() runs once the first step has exec'd (something of the command
    now runs). Raises StepFailed when a step cannot exec. The anchor closes
    every pipe end it created before returning.
    """
    steps = spec['steps']
    pipes = [os.pipe() for _ in range(len(steps) - 1)]
    try:
        for index, step in enumerate(steps):
            stdin = pipes[index - 1][0] if index else None
            stdout = pipes[index][1] if index < len(steps) - 1 else None
            _fork(step, index, stdin, stdout, out_fd, tty_fd if spec['pty'] else None, spec['single'], pids)
            if index == 0 and on_first is not None:
                on_first()
    finally:
        for pair in pipes:
            for fd in pair:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _fork(step, index, stdin, stdout, out_fd, tty_fd, single, pids):
    read, write = os.pipe()  # close-on-exec: EOF without data proves the exec succeeded
    pid = os.fork()
    if pid == 0:  # the step
        try:
            os.close(read)
            # Default dispositions for the step: an ignored SIGINT/SIGQUIT inherited from whatever started the
            # backend (a background shell ignores both) would make the terminal's Ctrl-C and Ctrl-\ do nothing.
            for sig in (signal.SIGPIPE, signal.SIGXFSZ, signal.SIGINT, signal.SIGQUIT):
                signal.signal(sig, signal.SIG_DFL)
            os.setsid()
            if tty_fd is not None:
                for target in (0, 1, 2):
                    os.dup2(tty_fd, target)
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)
            else:
                os.dup2(stdin if stdin is not None else os.open(os.devnull, os.O_RDONLY), 0)
                os.dup2(stdout if stdout is not None else out_fd, 1)
                os.dup2(out_fd, 2)
            if single:
                resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
            ceiling = os.sysconf('SC_OPEN_MAX')
            ceiling = FD_CEILING if not 0 < ceiling <= FD_CEILING else ceiling
            os.closerange(3, write)
            os.closerange(write + 1, ceiling)
            os.chdir(step['cwd'])
            os.execve(step['argv'][0], step['argv'], step['env'])
        except BaseException as exc:
            try:
                os.write(write, ('exec_error %s' % type(exc).__name__).encode()[:200])
            except OSError:
                pass
            os._exit(127)
    pids.append(pid)  # known before the exec verdict, so the sweep reaps its status
    os.close(write)
    detail = b''
    while True:
        try:
            piece = os.read(read, 200)
        except OSError:
            break
        if not piece:
            break
        detail += piece
    os.close(read)
    if detail:
        raise StepFailed(index, detail.decode('ascii', 'replace'))


def exit_code(codes):
    """Pipefail: the rightmost nonzero status, else 0; None when any status is unobserved."""
    if not codes or any(type(code) is not int for code in codes):
        return None
    return next((code for code in reversed(codes) if code != 0), 0)
