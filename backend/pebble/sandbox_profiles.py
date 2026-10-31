"""One SBPL builder for confined command execution on macOS.

build() returns the text for `sandbox-exec -p`. It replaces the ad hoc
profiles in terminal.py, async_terminal.py, testing.py and environments.py:

- deny by default; fork only for process_model 'tree';
- exec limited to the reviewed executable, the listed exec paths and the
  system tool folders. The temporary folder and writable paths never gain
  exec unless an exec path names them;
- signals and process information only inside the same sandbox *instance*,
  so a job can stop its own children and the containment anchor's
  kill(-1, SIGKILL) reaches exactly this job (Phase 14 E1/E1d);
- a mach-lookup allowlist (no DNS, keychain, LaunchServices or AppleEvents);
- reads of the system set, the listed folders, /dev/urandom and /dev/random
  (CLT Python needs both) and the dyld ancestor directory handles;
- writes to the temporary folder, the writable paths and /dev/null only;
- the private-path deny rules (Git metadata, .env, keys, credentials);
- network: none, the job's broker port, or the legacy open grant;
- an interactive job's own PTY slave literal plus /dev/tty (ioctl, read and
  write), never a ttys regex: E8 showed a regex lets a job type into the
  user's other terminals;
- a per-job marker literal, so two jobs never share profile text.

Inputs are host data. Paths must be absolute and already resolved; anything
else is refused rather than normalised, so the text says exactly what was
reviewed.

The broker port and the PTY slave exist only once the job's shim has started
its broker or allocated its terminal. build() can therefore emit one
DEFERRED slot line for each, and bind() replaces every slot exactly once,
refusing a missing, repeated or leftover slot. An unbound slot is an SBPL
comment, so it grants nothing.
"""

import json
import re
from pathlib import Path

from .egress import network_profile
from .policy import PolicyError

CLT = Path('/Library/Developer/CommandLineTools')
# Real toolchains exec through these: /usr/bin shims re-exec CLT tools via
# xcrun, make runs recipes through /bin/sh, clang runs ld, git runs helpers.
SYSTEM_EXEC = (Path('/bin'), Path('/usr/bin'), CLT / 'usr/bin', CLT / 'usr/libexec')
SYSTEM_READ = (
    Path('/System'), Path('/usr'), Path('/bin'), Path('/sbin'), Path('/private/etc'), Path('/private/var/db/dyld'),
    Path('/private/var/db/timezone'), Path('/private/var/select'), CLT,
)
SYSTEM_LITERALS = (
    Path('/dev/null'), Path('/dev/urandom'), Path('/dev/random'), Path('/private/var/db/xcode_select_link'),
)
# Determined with real python, pytest, node, git, make and clang runs under
# this profile (test_sandbox_profiles.py). libinfo serves getpwuid and
# friends; notification_center serves notify(3), which libSystem uses for
# time zone and configuration changes.
MACH_SERVICES = ('com.apple.system.opendirectoryd.libinfo', 'com.apple.system.notification_center')
# Name resolution, only with the legacy open network grant. A broker job
# never resolves names: the broker does, so mDNSResponder stays denied.
DNS_SERVICES = ('com.apple.dnssd.service', 'com.apple.mDNSResponder', 'com.apple.SystemConfiguration.DNSConfiguration',
                'com.apple.SystemConfiguration.configd', 'com.apple.trustd', 'com.apple.trustd.agent')
NETWORK_MODES = ('none', 'broker', 'open')
PROCESS_MODELS = ('tree', 'single')
MARKER = re.compile(r'[0-9a-f]{32}')
PATH_LIMIT = 1024
LIST_LIMIT = 64
DENY_LIMIT = 256  # Phase 40: the Shell's protected list (D92) is longer than any job's own path lists
PRIVATE_DENY = (
    '(regex #"(^|/)(\\.git|\\.env[^/]*|\\.ssh|\\.aws|\\.gnupg|\\.netrc|\\.npmrc|\\.pypirc|credentials|secrets)(/|$)")'
    ' (regex #"\\.(pem|key|p12|pfx|gguf)$")'
)
# A PTY slave allocated by openpty on macOS; anything else is refused.
TTY = re.compile(r'/dev/ttys[0-9]{1,6}')
CONTROLLING_TTY = Path('/dev/tty')
BROKER_SLOT = ';; pebble-slot: broker-port'
TTY_SLOT = ';; pebble-slot: pty-slave'
# Phase 46 (1.9.29, golden held-out recovery: 6 honest stops of 15 on both models; D212: the one failed command printed
# a lock with "Operation not permitted", a traceback, and the run stopped): Python's multiprocessing locks, queues and
# pools, and so concurrent.futures.ProcessPoolExecutor, are POSIX named semaphores (SemLock, "/mp-<random>"), which
# (deny default) refused: "PermissionError: [Errno 1] Operation not permitted" from a project's own script that runs in
# the person's terminal, in Codex (whose base profile allows POSIX semaphores for Python multiprocessing) and in the
# golden checkers. Only the names Python's multiprocessing makes; a semaphore holds a count, never data, so the worst
# another program's /mp- semaphore could suffer is a stuck or early lock. Unix sockets, shared memory and every network
# stay closed.
PYTHON_SEMAPHORES = '(allow ipc-posix-sem (ipc-posix-name-prefix "/mp-"))'


class _Deferred:
    """Pass as broker_port or tty to emit a slot that bind() fills exactly once."""

    def __repr__(self):
        return 'sandbox_profiles.DEFERRED'


DEFERRED = _Deferred()


def tty_rule(slave):
    """The only terminal grant: ioctl, read and write on the job's own slave literal and /dev/tty."""
    if not isinstance(slave, str) or not TTY.fullmatch(slave):
        raise PolicyError('A job terminal is one /dev/ttysN slave allocated for this job.')
    return f'(allow file-ioctl file-read* file-write* (literal {_quote(slave)}) (literal {_quote(CONTROLLING_TTY)}))'


def bind(profile, *, broker_port=None, tty=None):
    """Fill the DEFERRED slots: each given value replaces its slot line exactly once.

    A slot without a value, a value without its slot, a repeated slot or any
    slot text left over is refused, so the bound text grants exactly the
    broker port network_profile(port) and the job's own slave.
    """
    if not isinstance(profile, str) or not profile:
        raise PolicyError('A sandbox profile is required.')
    lines = profile.split('\n')
    for slot, value, rule in ((BROKER_SLOT, broker_port, network_profile), (TTY_SLOT, tty, tty_rule)):
        present = profile.count(slot)
        if value is None:
            if present:
                raise PolicyError('The sandbox profile has a slot that was not bound.')
            continue
        if present != 1 or lines.count(slot) != 1:
            raise PolicyError('The sandbox profile must hold each slot exactly once.')
        lines[lines.index(slot)] = rule(value)
    text = '\n'.join(lines)
    if 'pebble-slot:' in text:
        raise PolicyError('The sandbox profile still holds an unbound slot.')
    return text


def build(*, executable, exec_paths, read_paths, project_root, temp, writable_paths=(), network='none',
          broker_port=None, private_deny=True, job_marker, process_model='tree', executables=(), tty=None,
          deny_paths=(), deny_names=None, keep_paths=(), undeletable_paths=(), write_denied=None):
    """SBPL text for one job; refuses anything it cannot state exactly.

    executables are further pinned files the job may exec (a pipeline's other
    steps). tty is None (no terminal access), a /dev/ttysN slave, or DEFERRED.
    broker_port may be DEFERRED in broker mode; bind() then fills it.
    Phase 40 (1.6.0, D92): deny_paths (absolute, resolved) and deny_names (one SBPL regex) are denied for reading and
    writing after every allow, so they win inside the readable home folder; both default to nothing, which leaves
    every earlier profile byte for byte as it was.
    Phase 46 (1.9.28, b-new G2): keep_paths are denied every write and undeletable_paths deletion and renaming
    (file-write-unlink), after every allow; reads stay. Both default to nothing, leaving every profile as it was.
    Phase 46 (1.9.28, the w13b security review F1): write_denied (one SBPL regex) is denied every write after every
    allow, reads stay (confined.write_denied: the folders and bytecode files where later runs load code); none by
    default.
    """
    executable = _path(executable, 'executable')
    pinned = _paths(executables, 'executable')
    exec_paths = _paths(exec_paths, 'exec')
    read_paths = _paths(read_paths, 'read')
    root = _path(project_root, 'project')
    temp = _path(temp, 'temporary')
    writable = _paths(writable_paths, 'writable')
    if network not in NETWORK_MODES:
        raise PolicyError('Choose a sandbox network mode: none, broker or open.')
    if network == 'broker':
        broker = BROKER_SLOT if broker_port is DEFERRED else network_profile(broker_port)
    elif broker_port is not None:
        raise PolicyError('A broker port is only valid with broker network mode.')
    terminal = None if tty is None else TTY_SLOT if tty is DEFERRED else tty_rule(tty)
    denied = _paths(deny_paths, 'deny', limit=DENY_LIMIT) if deny_paths else ()
    kept = _paths(keep_paths, 'kept', limit=DENY_LIMIT) if keep_paths else ()
    undeletable = _paths(undeletable_paths, 'kept', limit=DENY_LIMIT) if undeletable_paths else ()
    if deny_names is not None and (type(deny_names) is not str or not deny_names or '"' in deny_names
                                   or any(ord(c) < 32 for c in deny_names) or len(deny_names) > 1024):
        raise PolicyError('The sandbox deny-names rule is one short regular expression.')
    if write_denied is not None and (type(write_denied) is not str or not write_denied or '"' in write_denied
                                     or any(ord(c) < 32 for c in write_denied) or len(write_denied) > 2048):
        raise PolicyError('The sandbox write-deny rule is one short regular expression.')
    if type(private_deny) is not bool:
        raise PolicyError('The private-path deny switch must be a boolean.')
    if type(job_marker) is not str or not MARKER.fullmatch(job_marker):
        raise PolicyError('A sandbox job marker is 32 lowercase hexadecimal characters.')
    if process_model not in PROCESS_MODELS:
        raise PolicyError('Choose a process model: tree or single.')
    if temp in (Path('/'), Path.home()) or root in (Path('/'), Path.home()):
        raise PolicyError('The sandbox temporary and project folders must be dedicated folders.')
    for path in exec_paths:
        if path == Path('/') or path == Path.home() or temp != path and temp.is_relative_to(path):
            raise PolicyError('An exec path may not contain the home folder or the temporary folder.')
    q = _quote
    # Exec from temp or a writable folder is denied unless an exec path names
    # it. Later SBPL rules win, so listed exec paths inside a denied folder are
    # re-allowed after the deny.
    blocked = [path for path in (temp, *writable) if path not in exec_paths]
    relisted = [path for path in exec_paths if any(path.is_relative_to(block) for block in blocked)]
    exec_rules = ' '.join(f'(subpath {q(p)})' for p in (*SYSTEM_EXEC, *exec_paths))
    exec_literals = ' '.join(f'(literal {q(p)})' for p in (executable, *pinned))
    deny_exec = ('(deny process-exec* ' + ' '.join(f'(subpath {q(p)})' for p in blocked) + ')') if blocked else ''
    allow_again = ('(allow process-exec* ' + ' '.join(f'(subpath {q(p)})' for p in relisted) + ')') if relisted else ''
    # dyld opens every ancestor directory of what it maps; grant the handles,
    # not the files under those ancestors.
    reads = (executable, *pinned, *exec_paths, *read_paths, root, temp, *writable)
    handles = sorted({ancestor for path in reads for ancestor in path.parents}, key=str)
    mach = MACH_SERVICES + (DNS_SERVICES if network == 'open' else ())
    lines = [
        '(version 1) (deny default)',
        f'(deny file-write* (literal "/private/var/empty/pebble-job-{job_marker}"))',
        '' if process_model == 'single' else '(allow process-fork)',
        f'(allow process-exec* {exec_literals} {exec_rules})',
        deny_exec,
        allow_again,
        '(allow signal (target same-sandbox))',
        '(allow process-info* (target same-sandbox))',
        '(allow sysctl-read) (allow file-read-metadata)',
        '(allow mach-lookup ' + ' '.join(f'(global-name {q(name)})' for name in mach) + ')',
        PYTHON_SEMAPHORES,  # Phase 46 (1.9.29)
        '(allow file-read* ' + ' '.join(f'(literal {q(p)})' for p in (*handles, *SYSTEM_LITERALS, executable, *pinned))
        + ' ' + ' '.join(f'(subpath {q(p)})' for p in (*SYSTEM_READ, *exec_paths, *read_paths, root, temp, *writable))
        + ')',
        '(allow file-write* ' + ' '.join(f'(subpath {q(p)})' for p in (temp, *writable)) + ' (literal "/dev/null"))',
        # Before the private-path deny, so that deny still wins inside a path re-allowed here.
        _protected_rules((root, *writable), files=(executable, *pinned), reads=(*exec_paths, *read_paths, temp, *writable),
                         writes=(temp, *writable)),
        (f'(deny file-read-data file-write* (subpath {q(root / ".git")}) {PRIVATE_DENY})' if private_deny else ''),
        ('(deny file-read-data file-write* ' + ' '.join(f'(subpath {q(p)})' for p in denied) + ')') if denied else '',
        f'(deny file-read-data file-write* (regex #"{deny_names}"))' if deny_names else '',
        ('(deny file-write* ' + ' '.join(f'(subpath {q(p)})' for p in kept) + ')') if kept else '',
        ('(deny file-write-unlink ' + ' '.join(f'(subpath {q(p)})' for p in undeletable) + ')') if undeletable else '',
        f'(deny file-write* (regex #"{write_denied}"))' if write_denied else '',
        terminal or '',
    ]
    if network == 'broker':
        lines.append(broker)
    elif network == 'open':
        lines.append(';; Legacy: unrestricted network kept only for the pre-broker approval path.\n(allow network*)')
    return '\n'.join(line for line in lines if line)


def _protected_rules(folders, *, files=(), reads=(), writes=()):
    """Phase 24 (V1-003, t24-data; exact at merge): Pebble's own folders below the project or a writable folder (a
    project at ~/Documents holds the Documents copies; a custom PEBBLE_HOME may lie inside a project) are denied for
    reading and writing, always. Later SBPL rules win, so a path this job was granted that lies inside such a folder
    (its temporary folder in <data>/jobs, a managed environment) then gets back exactly what it was granted: `files`
    (the executable and pinned files) as literals and `reads` as subpaths, `writes` as subpaths for file-write*.
    Nothing else inside the folder is re-allowed. The read re-allow names file-read-data, the operation the deny took
    away (metadata and xattr reads were never denied): measured with sandbox-exec, a later file-read* allow does not
    override an earlier file-read-data deny, while a later file-read-data allow does."""
    from .data_home import protected_paths_below
    below = sorted({str(p) for folder in folders for p in protected_paths_below(folder)})
    if not below:
        return ''
    folded = [p.casefold().rstrip('/') for p in below]  # APFS compares names case-insensitively, as data_home does

    def inside(path):
        text = str(path).casefold()
        return any(text == base or text.startswith(base + '/') for base in folded)
    lines = ['(deny file-read-data file-write* ' + ' '.join(f'(subpath {_quote(Path(p))})' for p in below) + ')']
    granted = [f'(literal {_quote(p)})' for p in dict.fromkeys(files) if inside(p)]
    granted += [f'(subpath {_quote(p)})' for p in dict.fromkeys(reads) if inside(p)]
    if granted:
        lines.append('(allow file-read-data ' + ' '.join(granted) + ')')
    written = [f'(subpath {_quote(p)})' for p in dict.fromkeys(writes) if inside(p)]
    if written:
        lines.append('(allow file-write* ' + ' '.join(written) + ')')
    return '\n'.join(lines)


def _path(value, label):
    if not isinstance(value, (str, Path)):
        raise PolicyError(f'The sandbox {label} path must be a path.')
    text = str(value)
    if not text or len(text) > PATH_LIMIT or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise PolicyError(f'The sandbox {label} path is invalid.')
    path = Path(text)
    if not path.is_absolute() or str(path) != text or '/../' in text + '/' or '/./' in text + '/':
        raise PolicyError(f'The sandbox {label} path must be absolute and normalised.')
    try:
        real = path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise PolicyError(f'The sandbox {label} path cannot be resolved.') from None
    if real != path:
        raise PolicyError(f'The sandbox {label} path must already be resolved (no symlinks).')
    return path


def _paths(values, label, limit=LIST_LIMIT):
    if isinstance(values, (str, Path)) or not isinstance(values, (list, tuple)) or len(values) > limit:
        raise PolicyError(f'The sandbox {label} paths must be a short list.')
    found = []
    for value in values:
        path = _path(value, label)
        if path not in found:
            found.append(path)
    return tuple(found)


def _quote(path):
    # SBPL string syntax, not shell interpolation; control characters were refused.
    return json.dumps(str(path), ensure_ascii=False)
