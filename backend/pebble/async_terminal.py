"""Owned streaming terminal execution for one reviewed command.

run_async_command() runs inside a manager-owned thread. It re-admits the
reviewed payload with terminal.inspect_command, builds the job's SBPL with the
unified sandbox_profiles.build, streams bounded raw output and returns only
once the run was observed stopped. Whatever it cannot observe is ``unknown``.

Two supervisors sit behind the same interface:
- launcher given: the Phase 14 shim+anchor path (job_shim.py, containment.py).
  The anchor sweeps the whole sandbox instance, so setsid/double-fork
  escapees are contained, output survives backend death in the shim's spool,
  and outcome_uncertain is False only after a recorded contained sweep.
- launcher None (today's default, kept so existing callers and tests keep
  their behavior): the owned process-group path below.

Typed command plans (command_plans v1) run only on the shim+anchor path, through
run_plan(): linear pipelines, per-step cwd and env, the exact reviewed writable
subpaths, process_model 'single', the reviewed resources as governors,
destination-scoped network through a per-job broker in the shim, and an
interactive plan's PTY. run_plan_command() is the synchronous entry with
terminal.run_command's result shape. A payload carrying a plan never runs on the
owned-group path unless the plan is the simple shape that path honours.

Owned-group path ownership rules:
- Only this module reaps the leader. Until it does, the leader's PID, and so
  its process-group ID, cannot be reused, so killpg() reaches only this
  command's group. Nothing is signalled after the reap, and nothing here
  signals a recorded PID.
- kqueue NOTE_EXIT observes the leader's exit without reaping it. It proves the
  leader exited, not that every descendant did, so the group is torn down and
  its death is proven separately.
- killpg() EPERM is ambiguous on XNU (bsd/kern/kern_sig.c killpg1): nfound is
  the count of members the caller is *authorised* to signal and ignores
  zombies, so EPERM (nfound == 0) means either no live member exists OR live
  members exist that we may not signal. Group death is therefore never inferred
  from EPERM; it is proven by enumerating live group members through libproc
  (proc_listpids(PROC_PGRP_ONLY) + proc_pidinfo(PROC_PIDT_SHORTBSDINFO)).
  Enumeration that is truncated, denied, partial or otherwise incomplete is
  treated as unknown, never as absence. Enumeration reads only pid/ppid/pgid/
  status; process names, argv and other foreign data are never published.
- A descendant that leaves the group (setsid/setpgid) is beyond killpg. If it
  keeps the output pipe open the run is ``unknown``; if it also closes the
  pipe it cannot be detected here.
- The service must not reap children elsewhere (waitpid(-1), SA_NOCLDWAIT).
- macOS has no parent-death signal: after a hard service crash the group runs
  on, still sandboxed but unsupervised. The manager must record such runs as
  unknown and never restart them.
"""

import ctypes
import errno
import hashlib
import math
import os
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .policy import PolicyError
from .terminal import inspect_command, resolve_executable

SANDBOX = Path('/usr/bin/sandbox-exec')
CHUNK_BYTES = 2048
OUTPUT_LIMIT = 192_000
TIMEOUT_LIMIT = 900
POLL_SECONDS = 0.05
STOP_POLL_SECONDS = 0.01
SETTLE_SECONDS = 0.005
TEARDOWN_SECONDS = 5.0
DRAIN_SECONDS = 2.0
LATE_REAP_SECONDS = 0.25
READS_PER_WAKE = 32

# libproc group-liveness enumeration (XNU bsd/sys/proc_info.h, libproc.h).
PROC_PGRP_ONLY = 2
PROC_PIDT_SHORTBSDINFO = 13
SHORT_BSD_INFO_SIZE = 64  # sizeof(struct proc_bsdshortinfo), ABI64.
SZOMB = 5  # p_stat of a process awaiting reap; see bsd/sys/proc.h.
_PID_SIZE = 4  # sizeof(pid_t).
_GROUP_LIST_CAPACITY = 64  # Starting PID-list capacity; grown on truncation.
_GROUP_LIST_RETRIES = 5

# Lifecycle seams: production uses the os calls; tests inject faults here.
_killpg = os.killpg
_waitpid = os.waitpid
_late_reapers = set()
_late_lock = threading.Lock()
_libproc = None
_libproc_probed = False
_libproc_lock = threading.Lock()


def run_async_command(payload, cancel, on_started, on_output, *, timeout=300, max_output=OUTPUT_LIMIT,
                      jobdir=None, launcher=None, on_disconnect='terminate', fetch=None, input_token=None):
    """Run one reviewed command to an observed end.

    on_started({'pid', 'isolation'}) is called once, right after spawn and
    before any output; the PID is manager control state and must never reach a
    model. on_output(bytes) receives chunks of at most CHUNK_BYTES. Both run in
    the calling thread and must not block. Failures before spawn raise
    PolicyError, and a cancellation seen before spawn returns ``cancelled``
    without calling on_started. Otherwise the result is
    {'state', 'exit_code', 'isolation', 'outcome_uncertain'}.

    With ``launcher`` set (the backend entry prefix, e.g. containment.launcher's
    argv) the command runs under the shim+anchor containment of job_shim.py:
    the reported PID is the anchor (evidence only) and outcome_uncertain is
    False only when the shim recorded a contained sweep and a certain exit. With
    ``launcher`` None the legacy owned-group path runs unchanged, so existing
    callers and tests keep today's behavior.

    A payload with a typed plan ('plan', 'plan_hash', bound by process_jobs)
    runs through run_plan() when a launcher is given; fetch(ref) -> bytearray
    supplies its credential references and input_token owns an interactive
    plan's terminal input. Without a launcher only the simple plan shape the
    owned-group path honours is accepted.
    """
    _limits(timeout, max_output)
    if not (callable(on_started) and callable(on_output) and callable(getattr(cancel, 'is_set', None))):
        raise PolicyError('Command callbacks and a cancellation signal are required.')
    if on_disconnect not in ('terminate', 'continue'):
        raise PolicyError('on_disconnect must be terminate or continue.')
    plan = None
    if isinstance(payload, dict) and payload.get('plan') is not None:
        plan = bound_plan(payload)
        if launcher is not None:
            return run_plan(plan, cancel, on_started, on_output, launcher=launcher, timeout=timeout,
                            max_output=max_output, jobdir=jobdir, on_disconnect=on_disconnect, fetch=fetch,
                            input_token=input_token)
        if not _simple(plan, payload):
            raise PolicyError('This command plan needs the contained plan worker. Nothing was run.')
    checked, writable = _admit(payload)
    if not _supported():
        raise PolicyError(
            'Confined terminal execution currently requires macOS. Windows terminal remains disabled until its OS sandbox is available.'
        )
    exe = resolve_executable(checked['argv'][0])
    if plan is not None:
        # Run the reviewed pin, re-checked now, never a fresh PATH lookup (terminal.run_command's rule).
        exe = Path(_verify_plan(plan, False)['steps'][0]['executable']['realpath'])
    toolchain = _toolchain(exe)
    root = Path(checked['root'])
    identity = tuple(checked['root_identity'])
    if launcher is not None:
        return _run_on_shim(checked, writable, exe, toolchain, root, identity, cancel, on_started, on_output,
                            timeout, max_output, jobdir, launcher, on_disconnect)
    isolation = 'macOS sandbox · selected project · ' + ('network approved' if checked['network'] else 'network denied')
    output = _Output(on_output, max_output)
    if cancel.is_set():
        return _result('cancelled', None, isolation, False)
    with tempfile.TemporaryDirectory(prefix='pebble-command-', ignore_cleanup_errors=True) as temp:
        temp = Path(temp).resolve()
        profile = _profile(exe, toolchain, root, temp, writable, checked['network'])
        if _identity(root) != identity:
            raise PolicyError('The reviewed project folder was replaced before terminal execution.')
        try:
            process = subprocess.Popen(
                [str(SANDBOX), '-p', profile, str(exe), *checked['argv'][1:]],
                cwd=root,
                env=_environment(exe, temp),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PolicyError('The confined command could not start; nothing ran.') from exc
        deadline = time.monotonic() + timeout
        return _OwnedGroup(process, output).supervise(cancel, deadline, on_started, isolation, root, identity)


def _limits(timeout, max_output):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= TIMEOUT_LIMIT:
        raise PolicyError('Choose a command timeout from 1 to 900 seconds.')
    if type(max_output) is not int or not 1 <= max_output <= OUTPUT_LIMIT:
        raise PolicyError('Choose a command output limit from 1 to 192000 bytes.')


def _admit(payload):
    """Re-inspect the reviewed command; any drift from the review fails closed."""
    if not isinstance(payload, dict):
        raise PolicyError('A reviewed command is required.')
    network, writable = payload.get('network', False), payload.get('writable', False)
    if type(network) is not bool or type(writable) is not bool:
        raise PolicyError('Command network and write scope must be explicit.')
    try:
        checked = inspect_command(payload.get('command'), payload.get('root'), network)
    except PolicyError:
        raise
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise PolicyError('The reviewed project folder is unavailable.') from exc
    identity = payload.get('root_identity')
    if (
        not isinstance(identity, (list, tuple))
        or len(identity) != 2
        or any(type(part) is not int for part in identity)
    ):
        raise PolicyError('The reviewed project identity is missing.')
    if payload.get('root') != checked['root'] or list(identity) != checked['root_identity']:
        raise PolicyError('The reviewed project folder was replaced before terminal execution.')
    if payload.get('argv') != checked['argv'] or payload.get('risk') != checked['risk']:
        raise PolicyError('Command changed after review.')
    if writable and checked['risk'] == 'read-only':
        raise PolicyError('Command write scope changed after review.')
    return checked, writable


def _supported():
    # libproc is required to prove group death without trusting killpg's EPERM.
    return (
        sys.platform == 'darwin'
        and SANDBOX.is_file()
        and hasattr(select, 'kqueue')
        and signal.getsignal(signal.SIGCHLD) != signal.SIG_IGN
        and _libproc_handle() is not None
    )


def _toolchain(exe):
    # Same runtime grant as terminal.run_command.
    toolchain = exe.parent.parent
    if exe.is_relative_to('/usr/bin') or exe.is_relative_to('/bin'):
        toolchain = Path('/Library/Developer/CommandLineTools')
    elif 'Python.framework' in str(exe):
        toolchain = Path('/Library/Frameworks/Python.framework')
    if toolchain == Path('/') or toolchain == Path.home():
        raise PolicyError('Cannot establish a confined command runtime.')
    return toolchain


def _profile(exe, toolchain, root, temp, writable, network):
    # The unified builder (sandbox_profiles.build) replaces the four ad hoc
    # profiles. It narrows mach-lookup, exec and process-info beyond the old
    # (allow process*) text and adds (allow signal (target same-sandbox)); the
    # owned-group teardown still runs killpg from the backend, outside the box.
    from . import sandbox_profiles
    return sandbox_profiles.build(
        executable=exe, exec_paths=[toolchain], read_paths=[toolchain], project_root=root, temp=temp,
        writable_paths=[root] if writable else (), network='open' if network else 'none',
        job_marker=uuid.uuid4().hex, process_model='tree')


def _environment(exe, temp):
    # Nothing is inherited; HOME and TMPDIR are this run's temporary folder.
    return {
        'PATH': str(exe.parent) + ':/usr/bin:/bin:/opt/homebrew/bin',
        'HOME': str(temp),
        'TMPDIR': str(temp),
        'LANG': 'en_US.UTF-8',
        'PYTHONNOUSERSITE': '1',
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_TERMINAL_PROMPT': '0',
    }


# --- shim+anchor path (job_shim.py containment) -----------------------------

# Reasons the shim records map to the caller-visible result states.
SHIM_STATES = {
    'command_exit': None,  # decided by exit code + contained
    'anchor_spawn_failed': None,  # the command's exec failed (code 127): certain, nothing of it ran
    # A supervisor that stopped its job because the service went away (crash or shutdown) leaves an
    # unknown outcome, never an owned cancellation: the Phase 12 crash contract.
    'stopped': 'cancelled', 'host_disconnect': 'unknown', 'lifeline_eof': 'cancelled',
    'timed_out': 'timed_out', 'deadline': 'timed_out',
    'output_limited': 'output_limited',
    'containment_lost': 'unknown', 'anchor_died': 'unknown', 'unknown': 'unknown',
}
SHIM_SPAWN_SECONDS = 15.0
SHIM_POLL_SECONDS = 0.03


def _run_on_shim(checked, writable, exe, toolchain, root, identity, cancel, on_started, on_output,
                 timeout, max_output, jobdir, launcher, on_disconnect):
    import secrets as _secrets

    from . import job_shim, sandbox_profiles

    prefix, launcher_reads = launcher
    launcher_reads = [Path(p) for p in launcher_reads]
    network = 'open' if checked['network'] else 'none'
    isolation = ('macOS sandbox · shim+anchor · selected project · '
                 + ('network approved' if checked['network'] else 'network denied'))
    if cancel.is_set():
        return _result('cancelled', None, isolation, False)
    owned_dir = jobdir is None
    # The job's HOME is a sibling of the jobdir, never inside it: dyld needs a
    # literal read on every ancestor of a granted path, and the jobdir holds
    # plan.json (with the control secret) and control.sock.
    base = Path(tempfile.mkdtemp(prefix='pebble-command-')) if owned_dir else None
    jobpath = base / 'job' if owned_dir else Path(jobdir)
    home = base / 'home' if owned_dir else jobpath.with_name(jobpath.name + '-home')
    try:
        home.mkdir(mode=0o700, parents=True, exist_ok=not owned_dir)
        home = home.resolve()
        if _identity(root) != identity:
            raise PolicyError('The reviewed project folder was replaced before terminal execution.')
        profile = sandbox_profiles.build(
            executable=Path(prefix[0]).resolve(), exec_paths=[toolchain, *launcher_reads],
            read_paths=[toolchain, *launcher_reads], project_root=root, temp=home,
            writable_paths=[root] if writable else [home], network=network, job_marker=uuid.uuid4().hex,
            process_model='tree')
        plan = {
            'version': 1, 'argv': [str(exe), *checked['argv'][1:]], 'cwd': str(root),
            'env': {**_environment(exe, home), 'PYTHONPATH': str(Path(__file__).resolve().parents[1])},
            'command_env': _environment(exe, home), 'profile': profile, 'launcher': list(prefix),
            'limits': {'timeout': timeout, 'output_limit': max_output, 'nofile': 4096, 'nproc': 512},
            'on_disconnect': on_disconnect, 'secret': _secrets.token_hex(16), 'isolation': isolation,
        }
        job_shim.write_plan(jobpath, plan)
        return _supervise_shim(jobpath, prefix, cancel, on_started, on_output, isolation, max_output, root,
                               identity)
    finally:
        # Never delete a jobdir under a live shim: its records are the evidence.
        if owned_dir and not job_shim.attach(jobpath).alive():
            _cleanup(base)


def _supervise_shim(jobpath, prefix, cancel, on_started, on_output, isolation, max_output, root, identity):
    lifeline_r, lifeline_w = os.pipe()  # pass_fds makes it inheritable in the child only
    env = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
           'PEBBLE_SHIM_LIFELINE_FD': str(lifeline_r)}
    try:
        shim = subprocess.Popen([*prefix, '--job-shim', str(jobpath)], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                pass_fds=(lifeline_r,), close_fds=True, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        os.close(lifeline_r)
        os.close(lifeline_w)
        raise PolicyError('The confined command could not start; nothing ran.') from exc
    os.close(lifeline_r)
    from . import job_shim
    handle = job_shim.attach(jobpath)
    output = _Output(on_output, max_output)
    started = stop_sent = drifted = False
    offset = 0
    spawn_deadline = time.monotonic() + SHIM_SPAWN_SECONDS
    try:
        while True:
            records = handle.records()
            kinds = {r.get('record') for r in records}
            if not started:
                spawned = next((r for r in records if r.get('record') == 'spawned'), None)
                if spawned is not None:
                    started = True
                    _call(output, on_started, {'pid': spawned.get('anchor_pid'), 'isolation': isolation})
                    # The sandbox grants paths; a folder swapped in during spawn was never reviewed.
                    drifted = _identity(root) != identity
                elif _never_ran(records):
                    raise PolicyError('The confined command could not start; nothing ran.')
                elif shim.poll() is not None or time.monotonic() > spawn_deadline:
                    # Spawning was recorded (or the shim vanished) without proof
                    # of either outcome: something may have run. Never "nothing ran".
                    handle.stop()
                    break
            chunk = handle.read_spool(offset, max_output)
            if chunk:
                offset += len(chunk)
                output.accept(chunk)
            if (cancel.is_set() or drifted or output.failure is not None) and not stop_sent:
                handle.stop()
                stop_sent = True
            if 'teardown' in kinds or (shim.poll() is not None and not handle.alive()):
                break
            time.sleep(SHIM_POLL_SECONDS)
        # Drain the final spool bytes after teardown.
        while True:
            chunk = handle.read_spool(offset, max_output)
            if not chunk:
                break
            offset += len(chunk)
            output.accept(chunk)
        output.flush()
    finally:
        try:
            os.close(lifeline_w)
        except OSError:
            pass
        try:
            shim.wait(timeout=job_shim.GRACE_SECONDS + 10)
        except Exception:
            pass
    failure = output.failure
    if failure is not None and not isinstance(failure, Exception):
        raise failure
    return _shim_result(handle, isolation, started, output, failure is not None or drifted)


def _never_ran(records):
    """True only when the shim proved its sandbox-exec never executed (exec-status pipe)."""
    kinds = [r.get('record') for r in records]
    exited = next((r for r in records if r.get('record') == 'exited'), None)
    return ('spawned' not in kinds and exited is not None
            and str(exited.get('reason', '')).startswith('anchor_spawn_failed'))


def _shim_result(handle, isolation, started, output, tainted):
    """Map the shim's exited record to today's states; anything unproven is unknown.

    outcome_uncertain is False only for a contained sweep with a certain exit
    (succeeded/failed) and no callback failure or project drift (tainted).
    """
    exited = next((r for r in reversed(handle.records()) if r.get('record') == 'exited'), None)
    if not started or exited is None:
        return _result('unknown', None, isolation, True)
    code = exited.get('exit_code')
    contained = bool(exited.get('contained'))
    reason = exited.get('reason') or 'unknown'
    mapped = SHIM_STATES.get(reason, 'unknown')
    if mapped is None:  # a natural command exit
        if contained and type(code) is int:
            state = 'succeeded' if code == 0 else 'failed'
        else:
            state = 'unknown'
    else:
        state = mapped
        if output.limited and state not in ('unknown',):
            state = 'output_limited'
    if tainted:
        state = 'unknown'
    uncertain = tainted or not (contained and state in ('succeeded', 'failed'))
    return _result(state, code if type(code) is int else None, isolation, uncertain)


# --- typed plans under shim+anchor (Phase 14f: X03, X04, R07) ----------------

PLAN_START_SECONDS = 45.0   # shim start, credential delivery (the shim waits 20 s), broker, digest re-check
SYNC_OUTPUT = 16_000        # terminal.run_command's displayed prefix
CREDENTIAL_VALUE_LIMIT = 8192
ROUTE = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?')
NEVER_RAN = {
    'credentials_unavailable': 'A reviewed credential could not be fetched or granted. Nothing ran.',
    'broker_unavailable': "The job's network broker could not start. Nothing ran.",
    'profile_unbound': "The job's sandbox profile could not be bound to its broker or terminal. Nothing ran.",
    'terminal_unavailable': "The job's terminal could not be allocated. Nothing ran.",
}


def bound_plan(payload):
    """The validated plan a reviewed payload carries, bound to its projection fields; else PolicyError."""
    from . import command_plans
    changed = PolicyError('The process command changed after review. Nothing was started.')
    try:
        plan = command_plans.validate(payload['plan'])
    except PolicyError:
        raise
    except Exception:
        raise changed from None
    step = plan['steps'][0]
    if (payload.get('plan_hash') != plan['plan_hash'] or payload.get('root') != plan['root']
            or payload.get('root_identity') != plan['root_identity'] or payload.get('argv') != step['argv']
            or payload.get('risk') != plan['risk'] or payload.get('network') is not (plan['network']['mode'] != 'none')
            or bool(plan['writable']) and payload.get('writable') is not True):
        raise changed
    return plan


def needs_plan_executor(payload):
    """True when a reviewed payload's plan needs run_plan/run_plan_command: terminal.run_command refuses it."""
    return isinstance(payload, dict) and payload.get('plan') is not None and not _simple(bound_plan(payload), payload)


def _simple(plan, payload):
    """The only plan shape the owned-group path honours (terminal.run_command's rule)."""
    step = plan['steps'][0]
    return (plan['pipeline'] == 'none' and plan['network']['mode'] == 'none' and plan['writable'] in ([], ['.'])
            and bool(plan['writable']) == bool(payload.get('writable', False)) and plan['process_model'] == 'tree'
            and not plan['interactive'] and step['cwd'] == '.' and not step['env'])


def _verify_plan(plan, loopback):
    """command_plans.verify right before launch: re-pins every executable, the root, cwd and writable paths."""
    from . import command_plans
    try:
        return command_plans.verify(plan, plan['root'], loopback=loopback)
    except PolicyError as exc:
        raise PolicyError(' '.join(str(exc).split()) + ' Nothing ran.') from None
    except Exception:
        raise PolicyError('The command plan could not be verified before its start. Nothing ran.') from None


def route_names(credentials):
    """One reverse-proxy route name per credential reference, in plan order (PEBBLE_ROUTE_<NAME>)."""
    names = []
    for index, item in enumerate(credentials):
        base = re.sub(r'[^a-z0-9]+', '-', item['ref'].lower()).strip('-')[:29].strip('-') or 'route'
        name = base if base not in names else f'{base[:27].strip("-")}-{index}'
        if not ROUTE.fullmatch(name) or name in names:
            raise PolicyError('The credential references cannot be named as distinct network routes.')
        names.append(name)
    return names


def _network_spec(plan, timeout, loopback, ca_file):
    """The shim's broker plan: CONNECT destinations and one credential route each. No credential values."""
    from . import egress
    network = plan['network']
    if network['mode'] != 'broker':
        return None
    routes = []
    for name, item in zip(route_names(network['credentials']), network['credentials']):
        host, port = item['destination']
        host = f'[{host}]' if ':' in host else host
        routes.append({'name': name, 'origin': egress.origin(f'https://{host}:{port}', loopback),
                       'methods': list(item['methods']), 'path_prefix': item['path_prefix'], 'ref': item['ref']})
    return {'allow': [list(pair) for pair in network['destinations']], 'routes': routes,
            'refs': sorted({item['ref'] for item in network['credentials']}),
            'lifetime': float(min(3600, timeout + 60)), 'loopback': loopback,
            'ca_file': ca_file if loopback and routes else None}


def run_plan(plan, cancel, on_started, on_output, *, launcher, timeout=None, max_output=OUTPUT_LIMIT, jobdir=None,
             on_disconnect=None, fetch=None, input_token=None, receipt=None, loopback=False, ca_file=None):
    """Run one reviewed typed plan to an observed end under shim+anchor containment.

    The plan is re-verified (command_plans.verify) immediately before launch
    and the anchor re-checks every pinned executable before any step starts;
    drift refuses with nothing run. The profile comes from
    command_plans.profile_inputs: exactly the reviewed writable paths, exec
    only of the pinned executables and their toolchains, the job's broker port
    and, for an interactive plan, its own PTY slave. The reviewed resources
    become the shim's governors. fetch(ref) -> bytearray supplies credential
    values, sent to the shim over a pipe after its launch and zeroed here.
    input_token (interactive plans) is the secret the input owner presents;
    only its SHA-256 reaches the jobdir. receipt, a dict, receives the
    content-free 'egress_log', 'step_codes' and 'records' before an owned
    jobdir is removed. loopback and ca_file are test-only fixtures.

    on_started({'pid', 'isolation'}) runs once the first step exec'd. A
    PolicyError means nothing of the command ran. Otherwise the result is
    {'state', 'exit_code', 'isolation', 'outcome_uncertain'}.
    """
    from . import job_shim, sandbox_profiles
    from . import command_plans
    if type(loopback) is not bool or ca_file is not None and not loopback:
        raise PolicyError('Loopback destinations and a fixture CA are for tests only.')
    if timeout is None:
        timeout = plan['resources']['timeout'] if isinstance(plan, dict) and isinstance(plan.get('resources'), dict) else 0
    _limits(timeout, max_output)
    if not (callable(on_started) and callable(on_output) and callable(getattr(cancel, 'is_set', None))):
        raise PolicyError('Command callbacks and a cancellation signal are required.')
    if launcher is None:
        raise PolicyError('Typed command plans run only under the containment supervisor. Nothing ran.')
    if not _supported():
        raise PolicyError(
            'Confined terminal execution currently requires macOS. Windows terminal remains disabled until its OS sandbox is available.'
        )
    plan = _verify_plan(plan, loopback)
    if on_disconnect is None:
        on_disconnect = plan['on_disconnect']
    if on_disconnect not in ('terminate', 'continue') or (on_disconnect == 'continue') > (plan['on_disconnect'] == 'continue'):
        raise PolicyError('on_disconnect may only narrow the reviewed plan. Nothing ran.')
    resources = plan['resources']
    timeout = min(timeout, resources['timeout'])
    max_output = min(max_output, resources['output_bytes'])
    network = _network_spec(plan, timeout, loopback, ca_file)
    if network and network['refs'] and not callable(fetch):
        raise PolicyError('This plan uses vault credentials, but no credential source is available. Nothing ran.')
    if plan['interactive'] and input_token is not None and (
            not isinstance(input_token, str) or not re.fullmatch(r'[0-9a-f]{32,128}', input_token)):
        raise PolicyError('The terminal input owner token is malformed. Nothing ran.')
    prefix, launcher_reads = launcher
    launcher_reads = [Path(p) for p in launcher_reads]
    isolation = ('macOS sandbox · shim+anchor · typed plan · '
                 + ('reviewed destinations only' if network else 'network denied'))
    if cancel.is_set():
        return _result('cancelled', None, isolation, False)
    root = Path(plan['root'])
    identity = tuple(plan['root_identity'])
    owned_dir = jobdir is None
    base = Path(tempfile.mkdtemp(prefix='pebble-command-')) if owned_dir else None
    jobpath = base / 'job' if owned_dir else Path(jobdir)
    home = base / 'home' if owned_dir else jobpath.with_name(jobpath.name + '-home')
    try:
        home.mkdir(mode=0o700, parents=True, exist_ok=not owned_dir)
        home = home.resolve()
        inputs = command_plans.profile_inputs(plan, home, uuid.uuid4().hex, loopback=loopback)
        if _identity(root) != identity:
            raise PolicyError('The reviewed project folder was replaced before terminal execution.')
        pinned = [step['executable']['realpath'] for step in plan['steps']]
        profile = sandbox_profiles.build(
            executable=Path(prefix[0]).resolve(), executables=pinned,
            exec_paths=[*inputs['exec_paths'], *launcher_reads], read_paths=[*inputs['read_paths'], *launcher_reads],
            project_root=inputs['project_root'], temp=inputs['temp'], writable_paths=inputs['writable_paths'],
            network=inputs['network'], broker_port=sandbox_profiles.DEFERRED if network else None,
            job_marker=uuid.uuid4().hex, process_model='tree',  # the anchor forks; 'single' is RLIMIT_NPROC=1
            tty=sandbox_profiles.DEFERRED if plan['interactive'] else None)
        steps = []
        for step in plan['steps']:
            real = Path(step['executable']['realpath'])
            env = {**_environment(real, home), **step['env']}
            if plan['interactive']:
                env.setdefault('TERM', 'dumb')
            steps.append({'argv': [str(real), *step['argv'][1:]], 'cwd': str(root / step['cwd']), 'env': env,
                          'pin': {key: step['executable'][key] for key in ('realpath', 'sha256', 'size')}})
        limits = {'timeout': timeout, 'output_limit': max_output, 'cpu_seconds': resources['cpu_seconds'],
                  'fsize_bytes': resources['fsize_mb'] << 20, 'nofile': resources['nofile'],
                  'nproc': resources['nproc'], 'memory_bytes': resources['memory_mb'] << 20}
        import secrets as _secrets
        job = {
            'version': 2, 'argv': steps[0]['argv'], 'cwd': str(root),
            'env': {**_environment(Path(pinned[0]), home), 'PYTHONPATH': str(Path(__file__).resolve().parents[1])},
            'command_env': steps[0]['env'], 'profile': profile, 'launcher': list(prefix), 'limits': limits,
            'on_disconnect': on_disconnect, 'secret': _secrets.token_hex(16), 'isolation': isolation,
            'plan_hash': plan['plan_hash'], 'steps': steps, 'single': plan['process_model'] == 'single',
            'pty': plan['interactive'], 'network': network,
            'input_owner': (hashlib.sha256(input_token.encode()).hexdigest()
                            if plan['interactive'] and input_token is not None else None),
        }
        job_shim.write_plan(jobpath, job)
        refs = network['refs'] if network else []
        return _supervise_plan(jobpath, prefix, cancel, on_started, on_output, isolation, max_output, root, identity,
                               refs, fetch, receipt)
    finally:
        # Never delete a jobdir under a live shim: its records are the evidence.
        if owned_dir and not job_shim.attach(jobpath).alive():
            _cleanup(base)


def run_plan_command(payload, cancel, *, launcher=None, fetch=None):
    """Synchronous entry for a reviewed plan terminal.run_command cannot honour.

    Returns terminal.run_command's shape plus 'plan_hash', 'step_codes' and
    'egress_log' (route receipts: no path, no credential). Raises PolicyError
    when nothing ran and terminal.CommandOutcomeUncertain when the outcome was
    not a certain contained exit. Interactive plans need the background path.
    """
    from .terminal import CommandOutcomeUncertain
    from .terminal_text import sanitize
    plan = bound_plan(payload)
    if plan['interactive']:
        raise PolicyError('An interactive command plan runs only as a background process. Nothing was run.')
    if launcher is None:
        from .containment import launcher as default_launcher
        launcher = default_launcher(bool(getattr(sys, 'frozen', False)))
    kept, receipt = bytearray(), {}

    def collect(chunk):
        kept.extend(chunk[:max(0, SYNC_OUTPUT - len(kept))])

    result = run_plan(plan, cancel, lambda info: None, collect, launcher=launcher, fetch=fetch, receipt=receipt)
    if result['outcome_uncertain'] or result['state'] not in ('succeeded', 'failed'):
        raise CommandOutcomeUncertain('Command stopped, timed out or exceeded the output limit after starting. Its '
                                      'outcome is uncertain; inspect it before retrying.')
    raw = bytes(kept)
    shown, removed, _ = sanitize(raw, final=True)
    return {'exit_code': result['exit_code'], 'output': shown, 'output_projection': 'terminal_text/1',
            'controls_removed': removed, 'raw_output_sha256': hashlib.sha256(raw).hexdigest(),
            'command': payload['command'], 'isolation': result['isolation'], 'plan_hash': plan['plan_hash'],
            'step_codes': receipt.get('step_codes'), 'egress_log': receipt.get('egress_log') or []}


def _deliver(fd, refs, fetch):
    """Send fetch(ref) values to the shim's credential pipe once, then zero every buffer. True on success."""
    from .job_shim import CREDENTIAL_MAGIC
    values, frame, ok = [], None, False
    try:
        for ref in refs:
            value = fetch(ref)
            if isinstance(value, str):
                value = bytearray(value, 'ascii')
            elif isinstance(value, bytes):
                value = bytearray(value)
            if isinstance(value, bytearray):
                values.append(value)  # tracked before any check, so a refused value is zeroed too
            if not isinstance(value, bytearray) or not 1 <= len(value) <= CREDENTIAL_VALUE_LIMIT:
                raise ValueError('unusable value')
        names = [ref.encode('ascii') for ref in refs]
        # One allocation, filled in place, so no resized copy of a value is left behind.
        frame = bytearray(len(CREDENTIAL_MAGIC) + sum(2 + len(n) + 4 + len(v) for n, v in zip(names, values)) + 2)
        at = len(CREDENTIAL_MAGIC)
        frame[:at] = CREDENTIAL_MAGIC
        for name, value in zip(names, values):
            struct.pack_into('>H', frame, at, len(name))
            frame[at + 2:at + 2 + len(name)] = name
            at += 2 + len(name)
            struct.pack_into('>I', frame, at, len(value))
            frame[at + 4:at + 4 + len(value)] = value
            at += 4 + len(value)
        view = memoryview(frame)
        try:
            while view:
                view = view[os.write(fd, view):]
        finally:
            view.release()
        ok = True
    except Exception:
        ok = False  # never chained: a vault error may quote its value
    finally:
        for value in values:
            value[:] = bytes(len(value))
        if frame is not None:
            frame[:] = bytes(len(frame))
        try:
            os.close(fd)
        except OSError:
            pass
    return ok


def _plan_never_ran(records):
    """(True, message) when the records prove nothing of the plan's command ran."""
    kinds = {r.get('record') for r in records}
    exited = next((r for r in records if r.get('record') == 'exited'), None)
    if exited is None:
        return False, None
    reason = str(exited.get('reason') or '')
    if 'spawned' not in kinds and reason.startswith('anchor_spawn_failed'):
        return True, NEVER_RAN.get(reason.partition(':')[2], 'The confined command could not start; nothing ran.')
    if 'started' not in kinds and reason.startswith('plan_refused') and exited.get('contained') is True:
        detail = reason.partition(':')[2]
        if detail.endswith('.exec'):
            return True, 'The confined command could not start; nothing ran.'
        return True, f'The command plan changed after review ({detail}). Propose it again. Nothing ran.'
    return False, None


def _supervise_plan(jobpath, prefix, cancel, on_started, on_output, isolation, max_output, root, identity, refs,
                    fetch, receipt):
    from . import job_shim
    lifeline_r, lifeline_w = os.pipe()  # pass_fds makes it inheritable in the child only
    env = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(Path(__file__).resolve().parents[1]),
           'PEBBLE_SHIM_LIFELINE_FD': str(lifeline_r)}
    passed = [lifeline_r]
    credential_w = None
    if refs:
        credential_r, credential_w = os.pipe()  # inheritable in the shim only (pass_fds)
        env['PEBBLE_SHIM_CREDENTIAL_FD'] = str(credential_r)
        passed.append(credential_r)
    try:
        shim = subprocess.Popen([*prefix, '--job-shim', str(jobpath)], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=tuple(passed),
                                close_fds=True, start_new_session=True)
    except (OSError, subprocess.SubprocessError) as exc:
        for fd in (*passed, lifeline_w) + ((credential_w,) if credential_w is not None else ()):
            os.close(fd)
        raise PolicyError('The confined command could not start; nothing ran.') from exc
    for fd in passed:
        os.close(fd)
    # Credentials travel only now, over the dedicated pipe: never argv, environment or the jobdir.
    fetched = _deliver(credential_w, refs, fetch) if credential_w is not None else True
    handle = job_shim.attach(jobpath)
    output = _Output(on_output, max_output)
    started = stop_sent = drifted = False
    offset = 0
    start_deadline = time.monotonic() + PLAN_START_SECONDS
    try:
        while True:
            records = handle.records()
            kinds = {r.get('record') for r in records}
            if not started:
                spawned = next((r for r in records if r.get('record') == 'spawned'), None)
                never, message = _plan_never_ran(records)
                if 'started' in kinds and spawned is not None:
                    started = True
                    _call(output, on_started, {'pid': spawned.get('anchor_pid'), 'isolation': isolation})
                    drifted = _identity(root) != identity
                elif never:
                    if not fetched:
                        message = NEVER_RAN['credentials_unavailable']
                    raise PolicyError(message)
                elif shim.poll() is not None and not handle.alive() and 'teardown' not in kinds:
                    break  # the shim is gone without proof either way: unknown
                elif time.monotonic() > start_deadline and not stop_sent:
                    handle.stop()  # no proof of either outcome yet: stop, and never call it "nothing ran"
                    stop_sent = True
            chunk = handle.read_spool(offset, max_output)
            if chunk:
                offset += len(chunk)
                output.accept(chunk)
            if (cancel.is_set() or drifted or output.failure is not None) and not stop_sent:
                handle.stop()
                stop_sent = True
            if 'teardown' in kinds or (shim.poll() is not None and not handle.alive()):
                break
            time.sleep(SHIM_POLL_SECONDS)
        while True:
            chunk = handle.read_spool(offset, max_output)
            if not chunk:
                break
            offset += len(chunk)
            output.accept(chunk)
        output.flush()
    finally:
        try:
            os.close(lifeline_w)
        except OSError:
            pass
        try:
            shim.wait(timeout=job_shim.GRACE_SECONDS + 10)
        except Exception:
            pass
        if isinstance(receipt, dict):
            records = handle.records()
            exited = next((r for r in reversed(records) if r.get('record') == 'exited'), None)
            receipt.update(egress_log=handle.egress_record(), records=[r.get('record') for r in records],
                           step_codes=exited.get('step_codes') if exited else None)
    failure = output.failure
    if failure is not None and not isinstance(failure, Exception):
        raise failure
    return _shim_result(handle, isolation, started, output, failure is not None or drifted)


def _call(output, callback, arg):
    try:
        callback(arg)
    except BaseException as exc:  # surfaced only after teardown
        output.failure = exc


def _cleanup(path):
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _identity(root):
    try:
        status = os.stat(root, follow_symlinks=False)
    except OSError:
        return None
    return status.st_dev, status.st_ino


def _result(state, exit_code, isolation, uncertain):
    return {'state': state, 'exit_code': exit_code, 'isolation': isolation, 'outcome_uncertain': uncertain}


def _exit_code(status):
    try:
        return os.waitstatus_to_exitcode(status)
    except ValueError:
        return None


def _partial_utf8(data):
    """Length of a trailing UTF-8 sequence still awaiting continuation bytes."""
    for back in range(1, min(3, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 == 0x80:
            continue
        need = 2 if byte & 0xE0 == 0xC0 else 3 if byte & 0xF0 == 0xE0 else 4 if byte & 0xF8 == 0xF0 else 1
        return back if need > back else 0
    return 0


def _watch_exit(queue, pid):
    """Watch for the leader's exit without reaping it; True if it already exited."""
    change = select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD, select.KQ_NOTE_EXIT)
    try:
        events = queue.control([change], 1, 0)
    except ProcessLookupError:
        # Already exited. It is still unreaped, so the PID remains ours.
        return True
    for event in events:
        if event.flags & select.KQ_EV_ERROR and event.data:
            if event.data == errno.ESRCH:
                return True
            raise OSError(event.data, os.strerror(event.data))
        if event.filter == select.KQ_FILTER_PROC and event.fflags & select.KQ_NOTE_EXIT:
            return True
    return False


def _reap_later(process):
    """Hand an unreaped leader to one thread that only kills before it reaps."""

    def collect():
        try:
            while True:
                try:
                    _killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    pid, status = _waitpid(process.pid, os.WNOHANG)
                except ChildProcessError:
                    process.returncode = -1
                    return
                if pid == process.pid:
                    code = _exit_code(status)
                    process.returncode = -1 if code is None else code
                    return
                time.sleep(LATE_REAP_SECONDS)
        finally:
            with _late_lock:
                _late_reapers.discard(threading.current_thread())

    thread = threading.Thread(target=collect, name='pebble-command-reaper', daemon=True)
    with _late_lock:
        _late_reapers.add(thread)
    try:
        thread.start()
    except RuntimeError:
        # Popen keeps an unreaped child in subprocess._active and collects it later.
        with _late_lock:
            _late_reapers.discard(thread)


def _libproc_handle():
    """Bind libproc once for group-liveness enumeration; None if unavailable."""
    global _libproc, _libproc_probed
    with _libproc_lock:
        if not _libproc_probed:
            try:
                lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
                lib.proc_listpids.restype = ctypes.c_int
                lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
                lib.proc_pidinfo.restype = ctypes.c_int
                lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            except (OSError, AttributeError):
                lib = None
            _libproc = lib
            _libproc_probed = True
        return _libproc


def _group_pids(pgid):
    """List the PIDs in process group pgid; None if truncated or unavailable.

    A snapshot exactly filling the buffer may have been truncated, so the
    buffer is grown and retried; a snapshot that never comes back short is
    reported as None (unknown), never as a shorter list.
    """
    libproc = _libproc_handle()
    if libproc is None:
        return None
    capacity = _GROUP_LIST_CAPACITY
    for _ in range(_GROUP_LIST_RETRIES):
        size = capacity * _PID_SIZE
        buffer = (ctypes.c_int32 * capacity)()
        ctypes.set_errno(0)
        written = libproc.proc_listpids(PROC_PGRP_ONLY, pgid, buffer, size)
        if written < 0 or written % _PID_SIZE != 0 or ctypes.get_errno() != 0:
            return None
        if written == 0:
            # Empty group, or an error reported as zero bytes; errno decides.
            return [] if ctypes.get_errno() == 0 else None
        if written >= size:
            capacity *= 4  # Possibly truncated; grow for a complete snapshot.
            continue
        members = [buffer[index] for index in range(written // _PID_SIZE)]
        if any(pid <= 0 for pid in members) or len(set(members)) != len(members):
            return None
        return members
    return None  # Never obtained a provably complete snapshot.


def _parse_short_bsd(data, written, pid, pgid):
    """Interpret PROC_PIDT_SHORTBSDINFO; None on any ABI/size surprise.

    Only pid/ppid/pgid/status are read. The process name and every other field
    are ignored, so no foreign data is published.
    """
    if written != SHORT_BSD_INFO_SIZE or len(data) != SHORT_BSD_INFO_SIZE:
        return None
    read_pid, _ppid, read_pgid, status = struct.unpack_from('<IIII', data, 0)
    if read_pid != pid:
        return None  # An identity/ABI mismatch cannot prove absence.
    if read_pgid != pgid:
        return 0  # This exact observed process moved out of the group.
    if not 1 <= status <= SZOMB:
        return None  # Impossible p_stat: fail closed to unknown.
    return status


def _short_bsd_status(pid, pgid):
    """p_stat of pid if it belongs to pgid; 0 if gone/moved; None if unreadable."""
    libproc = _libproc_handle()
    if libproc is None:
        return None
    buffer = (ctypes.c_byte * SHORT_BSD_INFO_SIZE)()
    ctypes.set_errno(0)
    written = libproc.proc_pidinfo(pid, PROC_PIDT_SHORTBSDINFO, 0, buffer, SHORT_BSD_INFO_SIZE)
    if written == 0:
        # A vanished process (ESRCH) is simply gone; anything else is unknown.
        return 0 if ctypes.get_errno() == errno.ESRCH else None
    if ctypes.get_errno() != 0:
        return None
    return _parse_short_bsd(bytes(buffer), written, pid, pgid)


def _live_group_members(pgid, exclude=()):
    """Count non-zombie members of owned group pgid.

    Returns 0 only when enumeration completed and every member is a zombie, or
    the group is empty: a proof of no live member. Returns a positive count
    when live members remain. Returns None when enumeration is truncated,
    denied, partial or otherwise incomplete; callers must treat None as unknown
    and never as absence. ``exclude`` drops a pid known to have exited (the
    owned leader after NOTE_EXIT), so a momentary zombie-transition read cannot
    miscount it as live; a still-unreaped leader also shows up as a zombie and
    is not counted anyway.
    """
    pids = _group_pids(pgid)
    if pids is None:
        return None
    skip = {exclude} if isinstance(exclude, int) else set(exclude)
    live = 0
    for pid in pids:
        if pid in skip:
            continue
        status = _short_bsd_status(pid, pgid)
        if status is None:
            return None  # Incomplete information: fail closed to unknown.
        if status and status != SZOMB:
            live += 1
    return live


class _Output:
    """Bounded byte transport for one run.

    Chunks hold at most CHUNK_BYTES and end on a UTF-8 boundary whenever the
    stream is valid UTF-8; only the final flush or the output limit can leave a
    trailing partial sequence. Delivered bytes are always an exact prefix of
    what the command wrote.
    """

    def __init__(self, on_output, limit):
        self.on_output = on_output
        self.limit = limit
        self.received = 0
        self.held = b''
        self.limited = False
        self.failure = None

    def room(self):
        return CHUNK_BYTES - len(self.held)

    def accept(self, block):
        while block and not self.limited:
            room = self.room()
            piece, block = block[:room], block[room:]
            allowed = self.limit - self.received
            if len(piece) > allowed:
                piece, self.limited = piece[:allowed], True
            self.received += len(piece)
            data = self.held + piece
            keep = 0 if self.limited else _partial_utf8(data)
            self.held = data[len(data) - keep:]
            self.emit(data[:len(data) - keep])

    def flush(self):
        held, self.held = self.held, b''
        self.emit(held)

    def emit(self, data):
        if data and self.failure is None:
            try:
                self.on_output(bytes(data))
            except BaseException as exc:  # Surfaced only after teardown.
                self.failure = exc


class _OwnedGroup:
    """One spawned leader and its process group.

    The group is signalled only while the leader is unreaped: its PID then
    still names this group, so no unrelated process can receive the signal.
    """

    def __init__(self, process, output):
        self.process = process
        self.pid = process.pid
        self.stream = process.stdout
        self.output = output
        self.queue = None
        self.readable = False
        self.eof = False
        self.exited = False  # NOTE_EXIT seen; the leader is still unreaped.
        self.collected = False  # The leader was reaped; never signal again.
        self.exit_code = None
        self.lingering = False

    def supervise(self, cancel, deadline, on_started, isolation, root, identity):
        reason, confirmed, escaped = 'unknown', False, True
        try:
            try:
                try:
                    on_started({'pid': self.pid, 'isolation': isolation})
                except BaseException as exc:  # Surfaced only after teardown.
                    self.output.failure = exc
                self.watch()
                # The sandbox grants paths; a folder swapped in during spawn was never reviewed.
                if self.output.failure is None and _identity(root) == identity:
                    reason = self.follow(cancel, deadline)
            except Exception:
                reason = 'unknown'
            try:
                if reason is None:
                    self.lingering = self.lingered()
                confirmed = self.stop()
                if self.exited:
                    self.reap()
                if confirmed:
                    escaped = not self.drain()
                else:
                    self.read()
            except Exception:
                confirmed = False
        finally:
            if not self.collected:
                _reap_later(self.process)
            self.close()
        failure = self.output.failure
        if failure is not None and not isinstance(failure, Exception):
            raise failure
        if failure is not None or reason == 'unknown' or not confirmed or escaped or self.exit_code is None:
            state = 'unknown'
        elif reason:
            state = reason
        elif self.output.limited:
            state = 'output_limited'
        else:
            state = 'succeeded' if self.exit_code == 0 else 'failed'
        uncertain = state not in ('succeeded', 'failed') or self.lingering
        return _result(state, self.exit_code, isolation, uncertain)

    def watch(self):
        os.set_blocking(self.stream.fileno(), False)
        self.readable = True
        self.queue = select.kqueue()
        try:
            self.exited = _watch_exit(self.queue, self.pid) or self.exited
        except OSError:
            self.queue.close()
            self.queue = None
            raise
        self.queue.control([select.kevent(self.stream.fileno(), select.KQ_FILTER_READ, select.KQ_EV_ADD)], 0)

    def follow(self, cancel, deadline):
        """Stream until the leader exits or a stop reason arises; None is a natural exit."""
        while True:
            if self.output.failure is not None:
                return 'unknown'
            if self.output.limited:
                return 'output_limited'
            if self.exited:
                return None
            if cancel.is_set():
                return 'cancelled'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 'timed_out'
            self.pump(min(POLL_SECONDS, remaining))

    def pump(self, timeout):
        """Wait briefly for output or the leader's exit, then drain. Never reaps."""
        events = ()
        if self.queue is not None:
            try:
                events = self.queue.control(None, 4, timeout)
            except OSError:
                self.queue.close()
                self.queue = None
        if self.queue is None:
            time.sleep(timeout)
        for event in events:
            if event.filter == select.KQ_FILTER_PROC and event.fflags & select.KQ_NOTE_EXIT:
                self.exited = True
        self.read()

    def read(self):
        """Drain what is readable now, bounded per wake; never blocks."""
        for _ in range(READS_PER_WAKE):
            if self.eof or not self.readable:
                return
            discard = self.output.limited
            try:
                block = os.read(self.stream.fileno(), CHUNK_BYTES if discard else self.output.room())
            except BlockingIOError:
                return
            except OSError:
                self.readable = False
                return
            if not block:
                self.eof = True
                self.output.flush()
                self._close_stream()
                return
            if not discard:
                self.output.accept(block)
                if self.output.limited:
                    return  # Stop the writers before reading any further.

    def lingered(self):
        """After a natural leader exit, True if live group members remain.

        Liveness is read directly from libproc, not inferred from killpg's
        ambiguous EPERM, and an unknown (None) enumeration is reported as
        lingering so the outcome stays uncertain.
        """
        until = time.monotonic() + SETTLE_SECONDS
        # Let the exited leader finish becoming a zombie before probing.
        while time.monotonic() < until:
            self.pump(max(0.0, until - time.monotonic()))
        # The leader is known to have exited; only descendants count as live.
        return _live_group_members(self.pid, exclude=self.pid) != 0  # >0, or None.

    def stop(self):
        """SIGKILL the group until no live member remains; True once confirmed."""
        if not self.exited and self.queue is None:
            # Exit cannot be observed without reaping; the late reaper takes over.
            try:
                _killpg(self.pid, signal.SIGKILL)
            except OSError:
                pass
            return False
        until = time.monotonic() + TEARDOWN_SECONDS
        while True:
            dead = self._kill_group()
            if dead and self.exited:
                return True
            if time.monotonic() >= until:
                return False
            self.pump(STOP_POLL_SECONDS)

    def reap(self):
        """Collect the exited leader once; nothing is signalled afterwards."""
        until = time.monotonic() + TEARDOWN_SECONDS
        while not self.collected:
            try:
                pid, status = _waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                # Another reaper took the leader; its status was not observed.
                self._collect(None)
                return
            if pid == self.pid:
                self._collect(status)
                return
            if time.monotonic() >= until:
                return
            time.sleep(STOP_POLL_SECONDS)

    def drain(self):
        """Read to EOF; a writer that survives the group's teardown escaped it."""
        until = time.monotonic() + DRAIN_SECONDS
        while not self.eof and self.readable and time.monotonic() < until:
            self.pump(POLL_SECONDS)
        return self.eof

    def close(self):
        if self.queue is not None:
            self.queue.close()
            self.queue = None
        self._close_stream()

    def _close_stream(self):
        try:
            self.stream.close()
        except OSError:
            pass

    def _kill_group(self):
        """SIGKILL the owned group; True only when no live member is proven gone.

        XNU's killpg answers EPERM both for an all-zombie group and for a live
        group the caller may not signal, so absence is never inferred from it:
        on EPERM the group is enumerated directly through libproc, and only a
        proven-empty live set (0) counts as death. A successful killpg means a
        live member just accepted SIGKILL, so the group is not yet dead.
        """
        if self.collected:
            raise RuntimeError('An owned process group is never signalled after its leader is reaped.')
        try:
            _killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True  # ESRCH: the group has no members at all.
        except PermissionError:
            # Exclude our leader only after NOTE_EXIT actually observed it.
            # Permission failure itself cannot establish that observation.
            return _live_group_members(self.pid, exclude=self.pid if self.exited else ()) == 0
        except OSError:
            return False  # Transient failure: not proven dead.
        return False  # A live member accepted SIGKILL; not dead yet.

    def _collect(self, status):
        self.collected = True
        # Popen must never wait on this PID again: it may now name another child.
        self.process.returncode = -1
        if status is not None:
            self.exit_code = _exit_code(status)
            if self.exit_code is not None:
                self.process.returncode = self.exit_code
