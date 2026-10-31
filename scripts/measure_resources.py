"""E06 resource measurement of Pebble's backend, model-free (Phase 18; design study section 5).

Scenarios run against a backend launched as the native shell launches it: the
src-tauri/src/main.rs environment allowlist, an owned temporary --home outside
this checkout and this checkout's --resources. --backend selects a frozen
candidate (a regular file owned by this user, not group or world writable);
without it, backend/main.py runs from this checkout and the report says source.

  run        R1 idle: cold start, then --idle-seconds (default 600) with no request.
             R2 scripted load: --runs (default 50) runs over JSON-RPC with an owned
             loopback fake provider: coding (Read, Edit, native approval), research
             from local documents (the frozen service cannot take an injected
             FixtureWeb, so no web tool is used) and memory recall over seeded notes.
             R3 cold start: --trials (default 10) spawns, each in a fresh home, timed
             to the first JSON-RPC response.
  watch      R4: samples an existing same-user process tree, for example the
             in-process source-mode driver during claim blocks. Labelled driver plus
             App; never a frozen result.
  self-test  The sampler on short-lived children this script starts: tree walk,
             footprint, disk bytes and the CPU time unit against wait4 (X4).

Measurements, with no new dependency: libproc proc_pid_rusage(RUSAGE_INFO_V2)
over the process tree every second (phys_footprint, CPU time converted with
mach_timebase_info, wakeups, ri_diskio bytes); ru_maxrss of the reaped backend
from wait4; home size as the sum of st_blocks*512 plus DB and WAL sizes; lsof
snapshots of the tree's internet sockets (only loopback is expected); nettop
bytes (X2) and top's power column (X3, an energy proxy, not battery) when they
work without root, otherwise 'blocked'. Battery needs `sudo powermetrics`, which
this script never runs, so battery is always 'blocked' here (user step U3).
Percentiles are nearest rank.

Egress tripwire: every backend also gets proxy variables pointing at an owned
loopback recorder that refuses all requests (NO_PROXY covers loopback), so a
library that honours proxy settings cannot reach the network unseen between
lsof snapshots; refused attempts count as non-loopback connections. Pebble's
provider clients ignore proxy variables. The report records whether --resources
holds the bundled tokenizer (without it tiktoken tries to download one).

One new report file is written (created exclusively, never overwritten). No
model, non-loopback connection, credential, profile, vault or user data is used.
"""

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import queue
import re
import select
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'pebble.resources/1'
RESOURCES = ROOT / 'src-tauri/resources'
ENV_ALLOWLIST = ('PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR',
                 '__CF_USER_TEXT_ENCODING',
                 # Phase 45 (F164): layer 4's two switches, Pebble's own and secret-free
                 'PEBBLE_SCREEN_CAPTURE', 'PEBBLE_FOREGROUND_INPUT')  # exactly src-tauri/src/main.rs
TOOLS = {'lsof': '/usr/sbin/lsof', 'nettop': '/usr/bin/nettop', 'top': '/usr/bin/top', 'pmset': '/usr/bin/pmset'}
TOOL_SECONDS = 20
SAMPLE_SECONDS = 1.0
DEFAULT_IDLE_SECONDS, DEFAULT_RUNS, DEFAULT_TRIALS = 600, 50, 10
LSOF_EVERY_SECONDS, LSOF_EVERY_RUNS = 30, 10
FIRST_RPC_SECONDS, RPC_SECONDS, SETTLE_SECONDS, EXIT_SECONDS = 45, 30, 60, 20
MB = 1 << 20
MARK = '\N{SECTION SIGN}'
TAG = re.compile(MARK + r'r2-([a-z]+)-(\d+)')
FAMILIES = ('coding', 'research', 'memory')
TRANSIENT = frozenset({'queued', 'running', 'retry_wait'})
LIMITS = {'max_steps': 12, 'max_seconds': 120}
PERMISSIONS = {'mode': 'ask', 'tools': {'files': True, 'edits': True, 'tests': False, 'apps': False, 'web': False,
                                        'terminal': False, 'plugins': False, 'views': False, 'screen': False}}
GOALS = {'coding': 'Change value to 2 in notes.py.',
         'research': 'Using only a.md and b.md in this project, what is the measured ferry capacity?',
         'memory': 'What is the fixture value for item 3 that I asked you to remember?'}
MEMORY_SEEDS = 10
THRESHOLDS = {  # design study section 4, T8 and T11; MB is 2**20 bytes
    'idle_footprint_mb': 300, 'peak_footprint_mb': 1024, 'idle_cpu_percent': 1.0,
    'home_growth_mb_per_100_runs': 20, 'cold_start_p95_seconds': 5.0, 'cold_start_blocking_seconds': 10.0,
    'non_loopback_connections': 0, 'telemetry_bytes_default': 0}
BATTERY = {'status': 'blocked', 'reason': 'powermetrics needs sudo; this script never runs sudo (user step U3)',
           'user_step': 'sudo powermetrics --samplers tasks,battery --show-process-energy -i 1000 -n 600 '
                        '> NEW_FILE  (or an unplugged `pmset -g batt` before and after R1)'}

# --- libproc (read-only sampling of one process tree) -----------------------------------------

PROC_PPID_ONLY = 6
RUSAGE_INFO_V2 = 2
RUSAGE_BUFFER = 512
TREE_LIMIT = 4096
# struct rusage_info_v2 (sys/resource.h): ri_uuid[16], then uint64 fields in this order: user_time,
# system_time, pkg_idle_wkups, interrupt_wkups, pageins, wired_size, resident_size, phys_footprint,
# proc_start_abstime, proc_exit_abstime, child_user_time, child_system_time, child_pkg_idle_wkups,
# child_interrupt_wkups, child_pageins, child_elapsed_abstime, diskio_bytesread, diskio_byteswritten.
OFFSETS = {'user_time': 16, 'system_time': 24, 'pkg_idle_wkups': 32, 'interrupt_wkups': 40, 'resident_size': 64,
           'phys_footprint': 72, 'proc_start_abstime': 80, 'diskio_bytesread': 144, 'diskio_byteswritten': 152}
COUNTERS = ('user_time_ns', 'system_time_ns', 'pkg_idle_wkups', 'interrupt_wkups', 'diskio_bytesread',
            'diskio_byteswritten')


class _Timebase(ctypes.Structure):
    _fields_ = [('numer', ctypes.c_uint32), ('denom', ctypes.c_uint32)]


class LibProc:
    """proc_pid_rusage and proc_listpids through ctypes; CPU times are converted to nanoseconds."""

    def __init__(self):
        self.lib, self.timebase, self.error = None, None, None
        try:
            lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            lib.proc_pid_rusage.restype = ctypes.c_int
            lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
            lib.proc_listpids.restype = ctypes.c_int
            lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
            libc = ctypes.CDLL(None, use_errno=True)
            info = _Timebase()
            if libc.mach_timebase_info(ctypes.byref(info)) != 0 or not info.numer or not info.denom:
                raise OSError('mach_timebase_info failed')
            self.lib, self.timebase = lib, (info.numer, info.denom)
        except (OSError, AttributeError) as exc:
            self.error = type(exc).__name__

    @property
    def available(self):
        return self.lib is not None

    def rusage(self, pid):
        """Counters for one pid, or None when it cannot be read (gone, not ours, no libproc)."""
        if self.lib is None:
            return None
        buf = (ctypes.c_byte * RUSAGE_BUFFER)()
        if self.lib.proc_pid_rusage(pid, RUSAGE_INFO_V2, buf) != 0:
            return None
        raw = bytes(buf)
        values = {name: struct.unpack_from('<Q', raw, offset)[0] for name, offset in OFFSETS.items()}
        numer, denom = self.timebase
        for key in ('user_time', 'system_time'):
            values[key + '_ns'] = values.pop(key) * numer // denom
        return values

    def children(self, pid):
        if self.lib is None:
            return None
        capacity = 256
        for _ in range(4):
            buf = (ctypes.c_int32 * capacity)()
            written = self.lib.proc_listpids(PROC_PPID_ONLY, pid, buf, ctypes.sizeof(buf))
            if written < 0:
                return None
            if written < ctypes.sizeof(buf):
                return [buf[i] for i in range(written // 4) if buf[i] > 0]
            capacity *= 4
        return None

    def tree(self, root):
        """The root and its descendants, walked by parent pid; None when unreadable or too large.

        A member reparented to launchd (setsid plus double fork) leaves this view.
        """
        members, frontier = [root], [root]
        while frontier:
            kids = self.children(frontier.pop())
            if kids is None:
                return None
            for kid in kids:
                if kid not in members:
                    members.append(kid)
                    frontier.append(kid)
            if len(members) > TREE_LIMIT:
                return None
        return members


def nearest_rank(values, share):
    """Nearest-rank percentile (as scripts/benchmark_code_search.py); None for no values."""
    if not 0 < share <= 1:
        raise ValueError('Use a share in (0, 1].')
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def latency(values):
    values = [v for v in values if v is not None]
    if not values:
        return {'n': 0}
    return {'n': len(values), 'p50': round(nearest_rank(values, 0.5), 4), 'p95': round(nearest_rank(values, 0.95), 4),
            'max': round(max(values), 4)}


class Sampler:
    """Samples one process tree every `interval` seconds in a daemon thread.

    Counters are kept per (pid, start time), so a member that exits keeps its last
    observed CPU, wakeups and disk bytes in the totals, and a reused pid is a new
    member. A member that starts and exits between two samples is never seen.
    """

    MAX_SAMPLES = 100_000

    def __init__(self, pid, *, interval=SAMPLE_SECONDS, libproc=None, clock=time.monotonic):
        self.pid, self.interval, self.clock = pid, interval, clock
        self.libproc = libproc or LibProc()
        self.lock = threading.Lock()
        self.samples, self.members, self.marks = [], {}, []
        self.unreadable = 0
        self.started = None
        self.stopping = threading.Event()
        self.thread = None

    def start(self):
        self.started = self.clock()
        self.sample()
        self.thread = threading.Thread(target=self._loop, name='resource-sampler', daemon=True)
        self.thread.start()
        return self

    def _loop(self):
        while not self.stopping.wait(self.interval):
            self.sample()

    def sample(self):
        tree = self.libproc.tree(self.pid)
        now = self.clock()
        if tree is None:
            with self.lock:
                self.unreadable += 1
            return None
        footprint = resident = seen = 0
        observed = {}
        for pid in tree:
            usage = self.libproc.rusage(pid)
            if usage is None:
                continue
            seen += 1
            observed[(pid, usage['proc_start_abstime'])] = {c: usage[c] for c in COUNTERS}
            footprint += usage['phys_footprint']
            resident += usage['resident_size']
        with self.lock:
            if not seen:
                self.unreadable += 1
                return None
            self.members.update(observed)
            record = {'t': round(now - self.started, 3), 'members': seen, 'footprint': footprint,
                      'resident': resident, **self._totals()}
            if len(self.samples) < self.MAX_SAMPLES:
                self.samples.append(record)
            return record

    def _totals(self):
        return {c: sum(m[c] for m in self.members.values()) for c in COUNTERS}

    def mark(self, label):
        record = self.sample() or {}
        with self.lock:
            self.marks.append({'label': label, 't': record.get('t'), **{c: record.get(c) for c in COUNTERS}})

    def stop(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=10)
        self.sample()
        return self.summary()

    def keys(self):
        with self.lock:
            return list(self.members)

    def alive(self, keys=None):
        """Members seen earlier that still run (same pid and start time). Never signals anything."""
        alive = []
        for pid, start in (keys if keys is not None else self.keys()):
            usage = self.libproc.rusage(pid)
            if usage is not None and usage['proc_start_abstime'] == start:
                alive.append(pid)
        return alive

    def summary(self):
        with self.lock:
            return summarize(list(self.samples), self.unreadable)


def summarize(samples, unreadable=0):
    if not samples:
        return {'samples': 0, 'unreadable_samples': unreadable}
    first, last = samples[0], samples[-1]
    window = last['t'] - first['t']
    cpu = ((last['user_time_ns'] + last['system_time_ns']) - (first['user_time_ns'] + first['system_time_ns'])) / 1e9
    wakeups = ((last['pkg_idle_wkups'] + last['interrupt_wkups']) - (first['pkg_idle_wkups'] + first['interrupt_wkups']))
    footprints = [s['footprint'] for s in samples]
    return {
        'samples': len(samples), 'unreadable_samples': unreadable, 'window_seconds': round(window, 3),
        'members_max': max(s['members'] for s in samples),
        'footprint_bytes': {'p50': nearest_rank(footprints, 0.5), 'p95': nearest_rank(footprints, 0.95),
                            'peak': max(footprints), 'last': footprints[-1]},
        'resident_peak_bytes': max(s['resident'] for s in samples),
        'cpu_seconds': round(cpu, 4),
        'cpu_percent_of_one_core': round(100 * cpu / window, 4) if window > 0 else None,
        'wakeups': wakeups, 'wakeups_per_second': round(wakeups / window, 3) if window > 0 else None,
        'disk_read_bytes': last['diskio_bytesread'] - first['diskio_bytesread'],
        'disk_written_bytes': last['diskio_byteswritten'] - first['diskio_byteswritten'],
        'cpu_time_unit': 'mach_timebase_info converted to nanoseconds',
    }


def reap(process, seconds=EXIT_SECONDS):
    """wait4 our own child (its ru_maxrss); SIGKILL only the still-unreaped leader after `seconds`.

    Never Popen.poll/wait here: they could reap the child and lose its rusage.
    """
    deadline, killed = time.monotonic() + seconds, False
    while True:
        pid, status, usage = os.wait4(process.pid, os.WNOHANG)
        if pid == process.pid:
            break
        if time.monotonic() > deadline:
            if killed:
                raise RuntimeError('The backend did not exit after SIGKILL; its scratch is kept.')
            os.kill(process.pid, 9)  # unreaped, so this pid still names our child
            killed, deadline = True, time.monotonic() + 10
        time.sleep(0.05)
    process.returncode = os.waitstatus_to_exitcode(status)
    scale = 1 if sys.platform == 'darwin' else 1024  # ru_maxrss: bytes on macOS, KiB on Linux
    return {'exit_code': process.returncode, 'killed_after_seconds': seconds if killed else None,
            'maxrss_bytes': usage.ru_maxrss * scale, 'user_seconds': round(usage.ru_utime, 4),
            'system_seconds': round(usage.ru_stime, 4)}


# --- stock macOS tools (no root) -----------------------------------------------------------------

def _tool(name, args):
    path = TOOLS[name]
    if not os.path.isfile(path):
        return None, 'not installed at ' + path
    try:
        done = subprocess.run([path, *args], stdin=subprocess.DEVNULL, capture_output=True, timeout=TOOL_SECONDS,
                              env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'C'})
    except subprocess.TimeoutExpired:
        return None, 'timed out after %ss' % TOOL_SECONDS
    except OSError as exc:
        return None, type(exc).__name__
    return done, None


def _host(endpoint):
    if endpoint.startswith('['):
        return endpoint[1:endpoint.find(']')] if ']' in endpoint else endpoint
    return endpoint.rsplit(':', 1)[0] if ':' in endpoint else endpoint


def _loopback(host):
    host = host.lower()
    return host in ('localhost', '::1') or host.startswith('127.') or host.startswith('::ffff:127.')


def parse_lsof(text):
    """lsof -F pPn field output -> connection counts; a wildcard bind counts as non-loopback."""
    entries, pid, protocol = [], None, None
    for line in text.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field == 'p':
            pid = int(value) if value.isdigit() else None
        elif field == 'f':
            protocol = None
        elif field == 'P':
            protocol = value
        elif field == 'n':
            endpoints = value.split('->')
            hosts = [_host(e) for e in endpoints]
            wildcard = any(h == '*' for h in hosts)
            loopback = not wildcard and all(_loopback(h) for h in hosts)
            entries.append({'pid': pid, 'protocol': protocol, 'name': value, 'loopback': loopback,
                            'wildcard': wildcard})
    outside = [e for e in entries if not e['loopback']]
    return {'connections': len(entries), 'loopback': len(entries) - len(outside), 'non_loopback': len(outside),
            'wildcard': sum(1 for e in entries if e['wildcard']),
            'non_loopback_names': sorted({e['protocol'] + ' ' + e['name'] for e in outside if e['protocol']})[:8]}


def lsof_snapshot(pids, label=None):
    """Internet sockets of these pids. lsof exits 1 with no output when there are none."""
    pids = [p for p in pids or [] if isinstance(p, int) and p > 0]
    if not pids:
        return {'status': 'blocked', 'reason': 'no process to inspect', 'label': label}
    done, problem = _tool('lsof', ['-nP', '-a', '-i', '-p', ','.join(map(str, pids)), '-F', 'pPn'])
    if done is None:
        return {'status': 'blocked', 'reason': problem, 'label': label}
    if done.returncode not in (0, 1) or (done.returncode == 1 and done.stdout.strip()):
        return {'status': 'blocked', 'reason': 'lsof exit %d' % done.returncode, 'label': label}
    return {'status': 'measured', 'label': label, 'pids': len(pids),
            **parse_lsof(done.stdout.decode('utf-8', 'replace'))}


def parse_nettop(text, pids):
    """nettop -L CSV -> bytes for rows whose name ends in .<pid>; None when no header is found."""
    lines = [line.split(',') for line in text.splitlines() if line.strip()]
    header = next((cells for cells in lines if 'bytes_in' in cells and 'bytes_out' in cells), None)
    if header is None:
        return None
    index_in, index_out = header.index('bytes_in'), header.index('bytes_out')
    total_in = total_out = rows = 0
    wanted = {str(p) for p in pids}
    for cells in lines:
        if cells is header or len(cells) <= max(index_in, index_out):
            continue
        # The process cell is name.pid; a time cell such as 12:00:00.1 has only digits, colons and dots.
        owner = next((c for c in cells if re.fullmatch(r'.*[^\d:.].*\.\d+', c)), None)
        if owner is None or owner.rsplit('.', 1)[1] not in wanted:
            continue
        try:
            total_in += int(cells[index_in])
            total_out += int(cells[index_out])
        except ValueError:
            return None
        rows += 1
    return {'bytes_in': total_in, 'bytes_out': total_out, 'rows': rows}


def nettop_bytes(pids):
    """Per-process bytes since each socket opened, all interfaces including loopback (X2)."""
    args = ['-P', '-L', '1', '-n', '-x', '-J', 'bytes_in,bytes_out']
    for pid in pids:
        args += ['-p', str(pid)]
    done, problem = _tool('nettop', args)
    if done is None:
        return {'status': 'blocked', 'reason': problem}
    parsed = parse_nettop(done.stdout.decode('utf-8', 'replace'), pids) if done.returncode == 0 else None
    if parsed is None:
        return {'status': 'blocked', 'reason': 'nettop exit %d or unparsed output' % done.returncode}
    # No row is not zero bytes: nettop lists a process only while it has sockets open.
    return {'status': 'measured' if parsed['rows'] else 'inconclusive', 'scope': 'all interfaces including loopback',
            **parsed}


def parse_top_power(text, pid):
    """The POWER column for `pid` in the last sample of `top -l 2 -stats pid,power`."""
    value = None
    for line in text.splitlines():
        cells = line.split()
        if len(cells) == 2 and cells[0] == str(pid):
            try:
                value = float(cells[1])
            except ValueError:
                value = None
    return value


def top_power(pid):
    done, problem = _tool('top', ['-l', '2', '-s', '1', '-stats', 'pid,power', '-pid', str(pid)])
    if done is None:
        return {'status': 'blocked', 'reason': problem}
    value = parse_top_power(done.stdout.decode('utf-8', 'replace'), pid) if done.returncode == 0 else None
    if value is None:
        return {'status': 'blocked', 'reason': 'top exit %d or no power value' % done.returncode}
    return {'status': 'measured', 'power': value, 'label': 'energy proxy (top POWER), not battery'}


def power_source():
    done, problem = _tool('pmset', ['-g', 'batt'])
    if done is None or done.returncode:
        return {'status': 'blocked', 'reason': problem or 'pmset exit %d' % done.returncode}
    match = re.search(r"drawing from '([^']{1,40})'", done.stdout.decode('utf-8', 'replace'))
    return {'status': 'measured', 'source': match.group(1) if match else 'unknown'}


# --- disk ----------------------------------------------------------------------------------------

def disk_usage(path):
    """Allocated bytes (sum of st_blocks*512) and apparent bytes under `path`, never following links."""
    allocated = apparent = files = unreadable = 0
    stack = [str(path)]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            unreadable += 1
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                unreadable += 1
                continue
            allocated += info.st_blocks * 512
            apparent += info.st_size
            files += 1
            if stat.S_ISDIR(info.st_mode):
                stack.append(entry.path)
    return {'allocated_bytes': allocated, 'apparent_bytes': apparent, 'entries': files, 'unreadable': unreadable}


def home_usage(home):
    home = Path(home)
    sizes = {}
    for name in ('pebble.sqlite', 'pebble.sqlite-wal', 'pebble.sqlite-shm'):
        try:
            sizes[name] = os.lstat(home / name).st_size
        except OSError:
            sizes[name] = None
    return {**disk_usage(home), 'database_bytes': sizes, 'telemetry': telemetry_footprint(home)}


def telemetry_footprint(home):
    folder = Path(home) / 'telemetry'
    if not os.path.lexists(folder):
        return {'exists': False, 'bytes': 0}
    usage = disk_usage(folder) if folder.is_dir() and not folder.is_symlink() else {'apparent_bytes': 1}
    return {'exists': True, 'bytes': usage['apparent_bytes']}


# --- egress tripwire -----------------------------------------------------------------------------

PROXY_NAMES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')
LOOPBACK_NAMES = '127.0.0.1,localhost,::1'


class EgressTrap:
    """An owned loopback HTTP proxy that refuses every request and records only its host and port.

    The backend runs with the proxy variables pointing here and NO_PROXY covering
    loopback, so a library that honours proxy settings (urllib's default opener,
    requests, httpx; for example tiktoken fetching a missing tokenizer) is refused
    here instead of reaching the network. Pebble's provider clients use
    ProxyHandler({}) and ignore these variables. A raw socket that ignores proxy
    settings is visible only to the lsof snapshots.
    """

    LIMIT = 100

    def __init__(self):
        self.lock = threading.Lock()
        self.attempts = []
        trap = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def refuse(self):
                target = self.path if self.command == 'CONNECT' else '%s:%s' % (
                    urlsplit(self.path).hostname, urlsplit(self.path).port or 80)
                with trap.lock:
                    if len(trap.attempts) < trap.LIMIT:
                        trap.attempts.append({'method': self.command[:16], 'target': str(target)[:200]})
                try:
                    self.send_error(403, 'Egress is refused during this measurement.')
                except OSError:
                    pass
            do_CONNECT = do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = do_PATCH = do_OPTIONS = refuse

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name='egress-trap', daemon=True)

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % self.server.server_port

    def environment(self):
        return {**dict.fromkeys(PROXY_NAMES, self.url), 'NO_PROXY': LOOPBACK_NAMES, 'no_proxy': LOOPBACK_NAMES}

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def report(self):
        with self.lock:
            attempts = list(self.attempts)
        return {'attempts': len(attempts), 'targets': sorted({a['target'] for a in attempts})[:10],
                'label': 'proxy-honouring egress refused and recorded on loopback; raw sockets only via lsof'}


def resources_preflight(resources=RESOURCES):
    """Whether --resources holds the bundled tokenizer; without it tiktoken would try to download one."""
    folder = Path(resources) / 'tokenizer'
    files = [p for p in folder.iterdir() if p.is_file()] if folder.is_dir() else []
    return {'tokenizer_files': len(files), 'complete': bool(files),
            'note': None if files else ('src-tauri/resources/tokenizer is missing: the first run needs a warm '
                                        '$TMPDIR/data-gym-cache, or tiktoken tries the network (the trap refuses it).')}


# --- the backend over JSON-RPC -------------------------------------------------------------------

class Backend:
    """One backend process on an owned home, launched from the main.rs environment allowlist.

    With `trap`, the proxy variables point at the egress tripwire (the only additions to the allowlist).
    """

    def __init__(self, command, home, *, resources=RESOURCES, trap=None):
        env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
        if trap is not None:
            env.update(trap.environment())
        self.lines, self.events, self.event_seq = queue.Queue(), [], 0   # events: the last 2000 (seq, item)
        self.event_lock = threading.Condition()
        self.stderr = tempfile.TemporaryFile()
        self.launched = time.monotonic()
        self.first_response_seconds = None
        self.exit = None
        self.process = subprocess.Popen([*command, '--home', str(home), '--resources', str(resources)], cwd=str(home),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, env=env)
        threading.Thread(target=self._read, name='backend-reader', daemon=True).start()

    @property
    def pid(self):
        return self.process.pid

    def _read(self):
        try:
            for line in self.process.stdout:
                item = json.loads(line)
                if isinstance(item, dict) and item.get('event'):
                    with self.event_lock:
                        self.event_seq += 1
                        self.events.append((self.event_seq, item))
                        del self.events[:-2000]
                        self.event_lock.notify_all()
                else:
                    self.lines.put(item)
        except (ValueError, OSError) as exc:
            self.lines.put({'protocol_error': type(exc).__name__})
        finally:
            self.lines.put(None)

    def rpc(self, method, *, seconds=None, **params):
        ident = os.urandom(8).hex()
        self.process.stdin.write((json.dumps({'id': ident, 'method': method, 'params': params}) + '\n').encode())
        self.process.stdin.flush()
        first = self.first_response_seconds is None
        deadline = time.monotonic() + (seconds or (FIRST_RPC_SECONDS if first else RPC_SECONDS))
        while True:
            try:
                item = self.lines.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError('The backend timed out during ' + method + '.') from None
            if item is None:
                raise RuntimeError('The backend stopped during ' + method + '.')
            if item.get('protocol_error'):
                raise RuntimeError('Backend protocol error: ' + item['protocol_error'])
            if item.get('id') == ident:
                if first:
                    self.first_response_seconds = time.monotonic() - self.launched
                if 'error' in item:
                    raise RuntimeError(str(item['error'])[:500])
                return item.get('result')

    def mark(self):
        with self.event_lock:
            return self.event_seq

    def wait_event(self, predicate, seconds, after=0):
        """True once an event numbered above `after` matches; False after `seconds`."""
        deadline = time.monotonic() + seconds
        with self.event_lock:
            while True:
                if any(seq > after and predicate(item) for seq, item in self.events):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.event_lock.wait(min(remaining, 0.5))

    def stderr_tail(self, limit=3000):
        try:
            self.stderr.seek(0)
            return self.stderr.read()[-limit:].decode('utf-8', 'replace')
        except (OSError, ValueError):
            return ''

    def close(self):
        """EOF on stdin (the shell's graceful stop), then wait4."""
        if self.exit is None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            self.exit = reap(self.process)
            try:
                self.process.stdout.close()
            except OSError:
                pass
        return self.exit


class Provider:
    """Owned loopback fake chat-completions provider (SSE). Replies depend only on the request."""

    def __init__(self, scripts=None):
        self.lock = threading.Lock()
        self.records, self.errors = [], []
        self.scripts = {'coding': script_coding, 'research': script_research, 'memory': script_memory,
                        **(scripts or {})}
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def do_POST(self):
                started = time.monotonic()
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    tag, delta = provider.reply(body)
                    offered = sorted({str((t.get('function') or {}).get('name')) for t in body.get('tools') or []
                                      if isinstance(t, dict)})
                except Exception as exc:
                    with provider.lock:
                        provider.errors.append(type(exc).__name__)
                    try:
                        self.send_error(500)
                    except OSError:
                        pass
                    return
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    self.wfile.write(b'data: ' + json.dumps({'choices': [{'delta': delta}]}).encode() + b'\n\n')
                    usage = {'prompt_tokens': 400, 'completion_tokens': 20}
                    self.wfile.write(b'data: ' + json.dumps({'choices': [], 'usage': usage}).encode() + b'\n\n')
                    self.wfile.write(b'data: [DONE]\n\n')
                except OSError:
                    pass
                with provider.lock:
                    provider.records.append({'tag': tag, 'at': started, 'seconds': time.monotonic() - started,
                                             'offered': offered, 'messages': len(body.get('messages') or [])})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name='fake-provider', daemon=True)

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % self.server.server_port

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def reply(self, body):
        messages = body.get('messages') or []
        tag = None
        for message in messages:
            if message.get('role') == 'user':
                match = TAG.search(message.get('content') if isinstance(message.get('content'), str)
                                   else json.dumps(message.get('content')))
                if match:
                    tag = match.group(0)
                    break
        family = TAG.fullmatch(tag).group(1) if tag else None
        script = self.scripts.get(family, script_memory)
        return tag, script(messages)

    def seconds(self, tag):
        with self.lock:
            return sum(r['seconds'] for r in self.records if r['tag'] == tag)


def tool_call(name, arguments, ident):
    return {'tool_calls': [{'index': 0, 'id': ident, 'type': 'function',
                            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


def tool_results(messages):
    return sum(1 for m in messages if m.get('role') == 'tool')


def script_coding(messages):
    done = tool_results(messages)
    if done == 0:
        return tool_call('Read', {'file_path': 'notes.py'}, 'r2-read')
    if done == 1:
        return tool_call('Edit', {'file_path': 'notes.py', 'old_string': 'value = 1', 'new_string': 'value = 2'},
                         'r2-edit')
    return {'content': 'The edit to notes.py is recorded by the host. FINAL: value = 2'}


def script_research(messages):
    done = tool_results(messages)
    if done < 2:
        return tool_call('Read', {'file_path': ('a.md', 'b.md')[done]}, 'r2-doc-%d' % done)
    return {'content': 'FINAL: 410 passengers (a.md, confirmed by b.md).'}


def script_memory(messages):
    return {'content': 'FINAL: 21 (from your saved notes).'}


def settled_state(backend, cid, rid, seen=None):
    """The run's state once it rests with the service idle, else None.

    waiting_approval with nothing pending is not rest: a native approval was just
    applied and the controller has not resumed the run yet. `seen` records what
    was observed, for a timeout diagnosis.
    """
    run = backend.rpc('run_get', id=cid, run_id=rid)
    active = backend.rpc('state').get('active')
    if seen is not None:
        seen.update(state=run['state'], active=bool(active))
    if run['state'] in TRANSIENT or active:
        return None
    if run['state'] == 'waiting_approval':
        statuses = [a['status'] for a in backend.rpc('get_chat', id=cid)['actions']]
        if seen is not None:
            seen['actions'] = statuses
        if not any(s in ('pending', 'running') for s in statuses):
            return None
    return run['state']


def settle(backend, cid, rid, *, after, seconds=SETTLE_SECONDS, seen=None):
    """Wait until this run rests with the service idle; returns its state, or 'timeout' (then cancelled).

    The runner and a native approval emit `idle` with the run ID once they release
    the task lane. Each such event numbered above `after` (or, should one be
    missed, every 2 s) is followed by one confirming read, so the driver adds no
    polling load while the service works.
    """
    deadline = time.monotonic() + seconds

    def idle(item):
        data = item.get('data') or {}
        return item.get('event') == 'idle' and data.get('run_id') == rid
    while time.monotonic() < deadline:
        if backend.wait_event(idle, min(2.0, max(0.0, deadline - time.monotonic())), after=after):
            after = backend.mark()  # a later wait needs a newer event
        state = settled_state(backend, cid, rid, seen)
        if state:
            return state
    try:
        backend.rpc('cancel', id=cid)
    except RuntimeError:
        pass
    return 'timeout'


def project(scratch, family, index):
    root = Path(scratch) / 'r2-projects' / ('%s-%03d' % (family, index))
    root.mkdir(parents=True)
    if family == 'coding':
        (root / 'notes.py').write_text('value = 1\n')
    elif family == 'research':
        (root / 'a.md').write_text('# Harbour ferry\n\nMeasured capacity: 410 passengers (survey, 2026).\n')
        (root / 'b.md').write_text('# Operator note\n\nThe 2026 survey counted 410 passengers per crossing.\n')
    else:
        return None
    return root.resolve()


def tool_latencies(backend, cid, rid):
    """Seconds from tool_started to tool_finished per tool name, from the run journal's timestamps."""
    started, names, durations, after = {}, {}, {}, 0
    for _ in range(50):
        page = backend.rpc('run_events', id=cid, run_id=rid, after=after, limit=200)
        if not page:
            break
        for event in page:
            after = max(after, event['sequence'])
            payload = event.get('payload') or {}
            if event['kind'] == 'tool_started':
                started[payload.get('call_id')] = event['created']
                names[payload.get('call_id')] = payload.get('tool')
            elif event['kind'] == 'tool_finished' and payload.get('call_id') in started:
                call = payload['call_id']
                durations.setdefault(str(names.get(call)), []).append(event['created'] - started.pop(call))
        if len(page) < 200:
            break
    return durations


def drive_run(backend, provider, family, index, workspace, *, goal=None, settle_seconds=SETTLE_SECONDS):
    """One scripted run, with the native approval a person gives for each pending edit."""
    cid = backend.rpc('new_chat')['id']
    if workspace is not None:
        backend.rpc('set_workspace', id=cid, path=str(workspace))
    tag = MARK + 'r2-' + family + '-' + str(index)
    started, mark = time.monotonic(), backend.mark()
    rid = backend.rpc('send', id=cid, text=(goal or GOALS[family]) + ' ' + tag, limits=LIMITS)['run_id']
    phases, seen = {'send': round(time.monotonic() - started, 4)}, {}
    state, approvals = settle(backend, cid, rid, after=mark, seconds=settle_seconds, seen=seen), 0
    phases['first_settle'] = round(time.monotonic() - started, 4)
    with provider.lock:
        served = [r['at'] for r in provider.records if r['tag'] == tag]
    phases['provider_requests_before_approval'] = len(served)
    # Host time before each provider request and after the last one, up to settlement.
    phases['host_gaps'] = [round(b - a, 4) for a, b in zip([started] + served, served + [started + phases['first_settle']])]
    for _ in range(3):
        if state != 'waiting_approval':
            break
        pending = [a for a in backend.rpc('get_chat', id=cid)['actions'] if a['status'] == 'pending']
        if not pending:
            break
        mark, approving = backend.mark(), time.monotonic()
        for action in pending:
            backend.rpc('approve', id=cid, action_id=action['id'])
            approvals += 1
        phases['approve_%d' % approvals] = round(time.monotonic() - approving, 4)
        state = settle(backend, cid, rid, after=mark, seconds=settle_seconds, seen=seen)
        phases['settle_after_approval_%d' % approvals] = round(time.monotonic() - approving, 4)
    if state == 'timeout':
        phases['last_seen'] = dict(seen)
    wall = time.monotonic() - started
    provider_seconds = provider.seconds(tag)
    return {'family': family, 'chat_id': cid, 'run_id': rid, 'state': state, 'approvals': approvals,
            'wall_seconds': wall, 'provider_seconds': provider_seconds,
            'host_overhead_seconds': max(0.0, wall - provider_seconds), 'phases': phases,
            'tools': tool_latencies(backend, cid, rid)}


# --- scenarios -----------------------------------------------------------------------------------

def private_folder(path):
    path.mkdir(mode=0o700)
    return path


def failure(exc):
    return (type(exc).__name__ + ': ' + str(exc))[:600]


def teardown(result, backend, sampler):
    """Stop the backend (EOF, wait4) and count earlier tree members that still run. Never signals them."""
    keys = sampler.keys() if sampler else []
    result['exit'] = backend.close()
    result['still_running_after_exit'] = len(sampler.alive(keys)) if sampler else None
    if result['exit']['exit_code'] != 0:
        result['stderr_tail'] = backend.stderr_tail()


def scenario_r1(command, scratch, idle_seconds, libproc, trap=None):
    home = private_folder(Path(scratch) / 'r1-home')
    result = {'label': 'R1 idle: cold start, then no request', 'model_calls': 0, 'planned_idle_seconds': idle_seconds}
    backend, sampler = Backend(command, home, trap=trap), None
    try:
        backend.rpc('command_catalogue')
        result['first_response_seconds'] = round(backend.first_response_seconds, 4)
        result['home_after_start'] = home_usage(home)
        sampler = Sampler(backend.pid, libproc=libproc).start()
        snapshots = result.setdefault('network', {})['lsof'] = [lsof_snapshot(libproc.tree(backend.pid), 'idle 0s')]
        started = time.monotonic()
        while time.monotonic() - started < idle_seconds:
            time.sleep(min(LSOF_EVERY_SECONDS, max(0.0, idle_seconds - (time.monotonic() - started))))
            snapshots.append(lsof_snapshot(libproc.tree(backend.pid), 'idle %ds' % round(time.monotonic() - started)))
        result['idle'] = sampler.stop()
        result['network']['nettop'] = nettop_bytes(libproc.tree(backend.pid) or [backend.pid])
        result['energy_proxy'] = top_power(backend.pid)
        result['home_before_exit'] = home_usage(home)
    except Exception as exc:
        result['error'] = failure(exc)
    finally:
        teardown(result, backend, sampler)
        result['home_after_exit'] = home_usage(home)
    return result


def scenario_r2(command, scratch, runs, libproc, trap=None):
    home = private_folder(Path(scratch) / 'r2-home')
    result = {'label': 'R2 scripted load over JSON-RPC with an owned loopback fake provider', 'model_calls': 0,
              'planned_runs': runs, 'families': list(FAMILIES),
              'note': 'Setup runs in its own session; home growth compares the home after each session closed.'}
    provider = Provider().start()
    records, before, sampler = [], None, None
    setup = Backend(command, home, trap=trap)
    try:
        setup.rpc('model_save', name='Resource fixture', url=provider.url, model='resource-fixture', context=32768)
        setup.rpc('setting', key='permissions', value=PERMISSIONS)
        for index in range(MEMORY_SEEDS):
            setup.rpc('memory_save', kind='semantic',
                      content='Fixture value for item %d is %d (synthetic resource note).' % (index, index * 7))
    except Exception as exc:
        result['error'] = failure(exc)
    finally:
        result['setup_exit'] = setup.close()
    if 'error' in result:
        provider.stop()
        result['runs'] = 0
        return result
    result['home_after_setup'] = before = home_usage(home)
    backend = Backend(command, home, trap=trap)
    try:
        backend.rpc('command_catalogue')
        sampler = Sampler(backend.pid, libproc=libproc).start()
        snapshots = result.setdefault('network', {})['lsof'] = [lsof_snapshot(libproc.tree(backend.pid), 'run 0')]
        for index in range(runs):
            family = FAMILIES[index % len(FAMILIES)]
            records.append(drive_run(backend, provider, family, index, project(scratch, family, index)))
            if (index + 1) % LSOF_EVERY_RUNS == 0 or index + 1 == runs:
                snapshots.append(lsof_snapshot(libproc.tree(backend.pid), 'run %d' % (index + 1)))
        result['load'] = sampler.stop()
        result['network']['nettop'] = nettop_bytes(libproc.tree(backend.pid) or [backend.pid])
    except Exception as exc:
        result['error'] = failure(exc)
    finally:
        try:
            teardown(result, backend, sampler)
        finally:
            provider.stop()
        result['home_after_exit'] = after = home_usage(home)
    done = len(records)
    result['runs'] = done
    states, tools = {}, {}
    for record in records:
        states[record['state']] = states.get(record['state'], 0) + 1
        for name, values in record['tools'].items():
            tools.setdefault(name, []).extend(values)
    result['states'] = states
    result['approvals'] = sum(r['approvals'] for r in records)
    result['latency_seconds'] = {
        'wall': latency([r['wall_seconds'] for r in records]),
        'provider': latency([r['provider_seconds'] for r in records]),
        'host_overhead': latency([r['host_overhead_seconds'] for r in records]),
        'per_tool': {name: latency(values) for name, values in sorted(tools.items())},
    }
    load = result.get('load') or {}
    result['cpu_seconds_per_run'] = round(load['cpu_seconds'] / done, 4) if done and 'cpu_seconds' in load else None
    growth = after['allocated_bytes'] - before['allocated_bytes'] if before else None
    result['home_growth_bytes'] = growth
    result['home_growth_mb_per_100_runs'] = round(growth / MB / done * 100, 3) if done and growth is not None else None
    result['provider_errors'] = list(provider.errors)
    return result


def scenario_r3(command, scratch, trials, libproc, trap=None):
    result = {'label': 'R3 cold start: spawn to first JSON-RPC response, a fresh home per trial', 'model_calls': 0,
              'planned_trials': trials, 'note': 'Trial 1 may include a cold OS file cache; later trials are warmer.'}
    rows = result['trials'] = []
    for trial in range(trials):
        home = private_folder(Path(scratch) / ('r3-home-%02d' % trial))
        backend = Backend(command, home, trap=trap)
        row = {'trial': trial + 1}
        try:
            backend.rpc('command_catalogue')
            row['first_response_seconds'] = round(backend.first_response_seconds, 4)
            row['lsof'] = lsof_snapshot(libproc.tree(backend.pid), 'trial %d' % (trial + 1))
        except Exception as exc:
            row['error'] = failure(exc)
        finally:
            rows.append(row)
            row['exit'] = backend.close()
            row['telemetry'] = telemetry_footprint(home)
    errors = [r['trial'] for r in rows if 'error' in r]
    if errors:
        result['error'] = 'trials without a first response: ' + ','.join(map(str, errors))
    result['first_response_seconds'] = latency([r.get('first_response_seconds') for r in rows])
    result['maxrss_bytes'] = max((r['exit']['maxrss_bytes'] for r in rows), default=None)
    return result


def self_test(scratch, libproc=None, interval=0.1):
    """Prove the sampler on children this function starts and reaps (X4). Returns named checks."""
    libproc = libproc or LibProc()
    checks = {}
    if not libproc.available:
        return {'passed': False, 'checks': {'libproc_available': False}, 'error': libproc.error}
    target = Path(scratch) / 'sampler-self-test.bin'
    child = subprocess.Popen([sys.executable, '-I', '-c', SELF_TEST_CHILD, str(target)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    sampler = Sampler(child.pid, interval=interval, libproc=libproc)
    try:
        ready = select.select([child.stdout], [], [], 30)[0]
        line = child.stdout.readline().decode().split() if ready else []
        grandchild = int(line[1]) if len(line) == 2 and line[0] == 'ready' and line[1].isdigit() else None
        sampler.start()
        time.sleep(interval * 4)
        summary = sampler.stop()
        root_usage = libproc.rusage(child.pid)
    finally:
        try:
            child.stdin.close()
        except OSError:
            pass
        exit_record = reap(child, 30)
        child.stdout.close()
    with sampler.lock:
        members = dict(sampler.members)
    sampled_cpu = sum(m['user_time_ns'] + m['system_time_ns'] for m in members.values()) / 1e9
    waited_cpu = exit_record['user_seconds'] + exit_record['system_seconds']
    ratio = sampled_cpu / waited_cpu if waited_cpu > 0 else None
    written = max((m['diskio_byteswritten'] for m in members.values()), default=0)
    checks['libproc_available'] = True
    checks['tree_walk_sees_grandchild'] = grandchild is not None and summary.get('members_max', 0) >= 2
    checks['footprint_at_least_48_mb'] = (summary.get('footprint_bytes') or {}).get('peak', 0) >= 48 * MB
    checks['disk_written_at_least_1_mb'] = written >= MB
    checks['cpu_time_unit_matches_wait4'] = ratio is not None and 0.75 <= ratio <= 1.33 and waited_cpu >= 0.2
    checks['maxrss_at_least_48_mb'] = exit_record['maxrss_bytes'] >= 48 * MB
    checks['child_exited_cleanly'] = exit_record['exit_code'] == 0 and exit_record['killed_after_seconds'] is None
    checks['root_readable_while_alive'] = root_usage is not None
    try:
        target.unlink()
    except OSError:
        pass
    return {'passed': all(checks.values()), 'checks': checks,
            'evidence': {'sampled_cpu_seconds': round(sampled_cpu, 4), 'wait4_cpu_seconds': round(waited_cpu, 4),
                         'cpu_ratio': round(ratio, 4) if ratio else None, 'timebase': list(libproc.timebase),
                         'members_max': summary.get('members_max'), 'disk_written_bytes': written,
                         'footprint_peak_bytes': (summary.get('footprint_bytes') or {}).get('peak'),
                         'maxrss_bytes': exit_record['maxrss_bytes'], 'samples': summary.get('samples')}}


SELF_TEST_CHILD = r'''
import os, subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, '-I', '-c', 'import sys; sys.stdin.read()'], stdin=subprocess.PIPE)
block = bytearray(64 * 1024 * 1024)
for index in range(0, len(block), 4096):
    block[index] = 1
with open(sys.argv[1], 'wb') as stream:
    chunk = os.urandom(1 << 20)
    for _ in range(4):
        stream.write(chunk)
    stream.flush()
    os.fsync(stream.fileno())
deadline, spins = time.process_time() + 0.4, 0
while time.process_time() < deadline:
    spins += 1
sys.stdout.write('ready %d\n' % grandchild.pid)
sys.stdout.flush()
sys.stdin.read()
grandchild.stdin.close()
grandchild.wait()
'''


# --- report --------------------------------------------------------------------------------------

def write_new(path, report):
    """Create `path` exclusively; an existing file or link is never replaced."""
    data = (json.dumps(report, indent=2, sort_keys=True) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def checkout_digests():
    paths = ['backend/main.py', *[p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'backend/pebble').glob('*.py'))],
             'scripts/measure_resources.py', 'src-tauri/src/main.rs']
    digests = {}
    for path in paths:
        try:
            digests[path] = sha(ROOT / path)
        except OSError:
            digests[path] = None
    return digests


def owned_backend(parser, value):
    given = Path(value)
    try:
        info = os.lstat(given)
    except OSError as exc:
        parser.error('--backend is not readable: ' + type(exc).__name__)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        parser.error('--backend must be a regular file, not a symbolic link.')
    if info.st_uid != os.getuid():
        parser.error('--backend must be owned by the current user.')
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        parser.error('--backend must not be group or world writable.')
    if not os.access(given, os.X_OK):
        parser.error('--backend must be executable.')
    return given.resolve()


def machine():
    try:
        memory = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except (ValueError, OSError):
        memory = None
    return {'platform': sys.platform, 'macos': platform.mac_ver()[0] or None, 'arch': platform.machine(),
            'cpu_count': os.cpu_count(), 'memory_bytes': memory, 'python': platform.python_version()}


def thresholds(scenarios, egress=None):
    """Design study T8 and T11 against what was measured; None where a value is missing or blocked.

    Egress attempts the trap refused count as non-loopback connections, even with every lsof snapshot clean.
    """
    r1, r2, r3 = scenarios.get('r1_idle') or {}, scenarios.get('r2_scripted') or {}, scenarios.get('r3_cold_start') or {}
    idle = r1.get('idle') or {}
    footprint = (idle.get('footprint_bytes') or {}).get('p50')
    peaks = [s.get('footprint_bytes', {}).get('peak') for s in (idle, r2.get('load') or {}) if s.get('footprint_bytes')]
    snapshots = [s for name in ('r1_idle', 'r2_scripted') for s in ((scenarios.get(name) or {}).get('network') or {}).get('lsof', [])]
    snapshots += [row['lsof'] for row in r3.get('trials', []) if 'lsof' in row]
    blocked = [s for s in snapshots if s.get('status') != 'measured']
    trapped = (egress or {}).get('attempts', 0)
    outside = sum(s.get('non_loopback', 0) for s in snapshots if s.get('status') == 'measured') + trapped
    homes = [r1.get('home_after_exit'), r2.get('home_after_exit')] + [{'telemetry': row.get('telemetry')}
                                                                      for row in r3.get('trials', [])]
    telemetry = [h['telemetry']['bytes'] for h in homes if h and h.get('telemetry')]
    cold = (r3.get('first_response_seconds') or {}).get('p95')

    def row(measured, limit, blocking=False, compare=lambda m, l: m <= l, **extra):
        return {'measured': measured, 'limit': limit, 'met': None if measured is None else compare(measured, limit),
                'blocking': blocking, **extra}
    return {
        'T11_idle_footprint_mb_p50': row(None if footprint is None else round(footprint / MB, 2),
                                         THRESHOLDS['idle_footprint_mb'],
                                         planned_idle_seconds=DEFAULT_IDLE_SECONDS,
                                         idle_seconds=idle.get('window_seconds')),
        'T11_peak_footprint_mb': row(round(max(peaks) / MB, 2) if peaks else None, THRESHOLDS['peak_footprint_mb']),
        'T11_idle_cpu_percent_of_one_core': row(idle.get('cpu_percent_of_one_core'), THRESHOLDS['idle_cpu_percent'],
                                                planned_idle_seconds=DEFAULT_IDLE_SECONDS,
                                                idle_seconds=idle.get('window_seconds')),
        'T11_home_growth_mb_per_100_runs': row(r2.get('home_growth_mb_per_100_runs'),
                                               THRESHOLDS['home_growth_mb_per_100_runs'], runs=r2.get('runs')),
        'T11_non_loopback_connections': row(outside if trapped else None if blocked or not snapshots else outside,
                                            THRESHOLDS['non_loopback_connections'], blocking=True,
                                            snapshots=len(snapshots), blocked_snapshots=len(blocked),
                                            egress_attempts_refused=trapped),
        'T11_telemetry_bytes_by_default': row(sum(telemetry) if telemetry else None,
                                              THRESHOLDS['telemetry_bytes_default'], blocking=True, homes=len(telemetry)),
        'T8_cold_start_p95_seconds': row(cold, THRESHOLDS['cold_start_p95_seconds'],
                                         blocking_above=THRESHOLDS['cold_start_blocking_seconds'],
                                         trials=(r3.get('first_response_seconds') or {}).get('n')),
    }


def run_command(args, parser):
    backend = owned_backend(parser, args.backend) if args.backend else None
    command = [str(backend)] if backend else [sys.executable, str(ROOT / 'backend/main.py')]
    chosen = [s.strip() for s in args.scenarios.split(',') if s.strip()]
    if not chosen or set(chosen) - {'r1', 'r2', 'r3'} or len(set(chosen)) != len(chosen):
        parser.error('--scenarios takes a unique comma list of r1, r2 and r3.')
    if not 1 <= args.idle_seconds <= 7200 or not 1 <= args.runs <= 1000 or not 1 <= args.trials <= 100:
        parser.error('Use 1-7200 idle seconds, 1-1000 runs and 1-100 trials.')
    if args.scratch:
        scratch = Path(args.scratch).resolve()
        if scratch.is_relative_to(ROOT) or os.path.lexists(scratch) or not scratch.parent.is_dir():
            parser.error('--scratch must be a new folder, outside this checkout, in an existing folder.')
        scratch.mkdir(mode=0o700)
    else:
        scratch = Path(tempfile.mkdtemp(prefix='pebble-resources-',
                                        dir='/private/tmp' if os.path.isdir('/private/tmp') else None)).resolve()
        if scratch.is_relative_to(ROOT):
            parser.error('The temporary folder is inside this checkout; choose --scratch.')
    libproc = LibProc()
    backend_sha = sha(backend) if backend else None
    sources = checkout_digests()
    scenarios, errors = {}, []
    started = time.monotonic()
    trap = EgressTrap().start()
    try:
        for name, run in (('r1', lambda: scenario_r1(command, scratch, args.idle_seconds, libproc, trap)),
                          ('r2', lambda: scenario_r2(command, scratch, args.runs, libproc, trap)),
                          ('r3', lambda: scenario_r3(command, scratch, args.trials, libproc, trap))):
            if name not in chosen:
                continue
            key = {'r1': 'r1_idle', 'r2': 'r2_scripted', 'r3': 'r3_cold_start'}[name]
            try:
                scenarios[key] = run()
            except Exception as exc:
                errors.append({'scenario': key, 'error': (type(exc).__name__ + ': ' + str(exc))[:600]})
    finally:
        trap.stop()
    stable = checkout_digests() == sources and (backend is None or sha(backend) == backend_sha)
    egress = trap.report()
    limits = thresholds(scenarios, egress)
    privacy = [limits['T11_non_loopback_connections'], limits['T11_telemetry_bytes_by_default']]
    complete = (not errors and len(scenarios) == len(chosen)
                and not any('error' in s for s in scenarios.values())
                and all(s.get('runs') == s['planned_runs'] for s in scenarios.values() if 'planned_runs' in s))
    lingering = sum(s.get('still_running_after_exit') or 0 for s in scenarios.values())
    report = {
        'schema': SCHEMA, 'created_at': datetime.now(timezone.utc).isoformat(),
        'kind': 'frozen' if backend else 'source', 'label': (args.label or '')[:120] or None,
        'backend_path': str(backend) if backend else None, 'backend_sha256': backend_sha,
        'source_sha256': sources, 'inputs_stable': stable,
        'machine': machine(), 'power_source': power_source(), 'battery': BATTERY,
        'libproc': {'available': libproc.available, 'timebase': list(libproc.timebase or ()), 'error': libproc.error},
        'resources': resources_preflight(), 'egress_trap': egress,
        'scenarios': scenarios, 'errors': errors, 'complete': complete,
        'thresholds': limits,
        'privacy_passed': all(p['met'] is True for p in privacy),
        'duration_seconds': round(time.monotonic() - started, 1),
        'scratch_preserved': None,
        'limits': ('Model-free scenarios on this machine. Memory is the libproc tree sum of phys_footprint sampled '
                   'every second (members that live less than a sample interval are missed; members reparented '
                   'to launchd leave the tree); ru_maxrss is the reaped backend only. lsof shows sockets open at '
                   'each snapshot, not every connection made between snapshots; the egress trap covers clients that '
                   'honour proxy variables, not raw sockets. nettop and top need no root but '
                   'may be blocked; top POWER is an energy proxy, not battery; battery is blocked without the '
                   "user's sudo powermetrics run. R2 research uses local documents because the frozen service "
                   'cannot take an injected FixtureWeb; its host overhead is a scripted proxy, not the T8 claim '
                   'value. Source mode measures backend/main.py under this Python, never a frozen result.'),
    }
    keep = args.keep_scratch or errors or not complete or lingering
    if keep:
        report['scratch_preserved'] = str(scratch)
    write_new(args.report, report)
    if not keep:
        shutil.rmtree(scratch, ignore_errors=True)
    print(json.dumps({'kind': report['kind'], 'complete': complete, 'privacy_passed': report['privacy_passed'],
                      'errors': errors, 'thresholds': {k: v['met'] for k, v in limits.items()},
                      'report': str(args.report)}, indent=2))
    return 0 if complete and report['privacy_passed'] and stable else 1


def watch_command(args, parser):
    libproc = LibProc()
    try:
        os.kill(args.pid, 0)
    except PermissionError:
        parser.error('--pid must be a process owned by the current user.')
    except ProcessLookupError:
        parser.error('--pid is not running.')
    if libproc.rusage(args.pid) is None:
        parser.error('--pid cannot be sampled with libproc.')
    sampler = Sampler(args.pid, libproc=libproc).start()
    snapshots, started, reason = [lsof_snapshot(libproc.tree(args.pid), 'watch 0s')], time.monotonic(), 'max_seconds'
    last_lsof = started
    while time.monotonic() - started < args.max_seconds:
        time.sleep(SAMPLE_SECONDS)
        if libproc.rusage(args.pid) is None:
            reason = 'exited'
            break
        if time.monotonic() - last_lsof >= 60:
            last_lsof = time.monotonic()
            snapshots.append(lsof_snapshot(libproc.tree(args.pid), 'watch %ds' % round(last_lsof - started)))
    summary = sampler.stop()
    report = {
        'schema': SCHEMA, 'created_at': datetime.now(timezone.utc).isoformat(), 'kind': 'watch',
        'label': 'R4 live (labelled): driver plus App in source mode; never a frozen result',
        'note': (args.label or '')[:120] or None, 'ended': reason, 'max_seconds': args.max_seconds,
        'machine': machine(), 'power_source': power_source(), 'battery': BATTERY,
        'sampled': summary, 'network': {'lsof': snapshots},
        'limits': 'A live campaign makes model calls on the authorised route; this sampler makes none.',
    }
    write_new(args.report, report)
    print(json.dumps({'ended': reason, 'samples': summary.get('samples'), 'report': str(args.report)}))
    return 0


def self_test_command(args, parser):
    with tempfile.TemporaryDirectory(prefix='pebble-sampler-') as scratch:
        result = self_test(scratch)
    report = {'schema': SCHEMA, 'created_at': datetime.now(timezone.utc).isoformat(), 'kind': 'self_test',
              'machine': machine(), **result}
    write_new(args.report, report)
    print(json.dumps({'passed': result['passed'], 'checks': result['checks']}, indent=2))
    return 0 if result['passed'] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='R1-R3 against a backend')
    run.add_argument('--report', type=Path, required=True, help='new JSON report path (must not exist)')
    run.add_argument('--backend', help='frozen executable owned by the current user (default: source mode)')
    run.add_argument('--scenarios', default='r1,r2,r3')
    run.add_argument('--idle-seconds', type=int, default=DEFAULT_IDLE_SECONDS)
    run.add_argument('--runs', type=int, default=DEFAULT_RUNS)
    run.add_argument('--trials', type=int, default=DEFAULT_TRIALS)
    run.add_argument('--scratch', help='new folder for owned homes and projects (default: /private/tmp)')
    run.add_argument('--keep-scratch', action='store_true')
    run.add_argument('--label', help='short label for the subject, e.g. the candidate name')
    watch = commands.add_parser('watch', help='R4: sample an existing same-user process tree')
    watch.add_argument('--pid', type=int, required=True)
    watch.add_argument('--report', type=Path, required=True)
    watch.add_argument('--max-seconds', type=int, default=7200)
    watch.add_argument('--label')
    test = commands.add_parser('self-test', help='prove the sampler on owned short-lived children')
    test.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    if os.path.lexists(args.report):
        parser.error('%s already exists; choose a new report path.' % args.report)
    if not args.report.parent.is_dir():
        parser.error('%s is not an existing directory.' % args.report.parent)
    if args.command == 'run':
        return run_command(args, parser)
    if args.command == 'watch':
        if not 1 <= args.max_seconds <= 86400:
            parser.error('Use 1-86400 --max-seconds.')
        return watch_command(args, parser)
    return self_test_command(args, parser)


if __name__ == '__main__':
    sys.exit(main())
