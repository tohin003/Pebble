"""Pinned installed-app identity and honest launch receipts (Phase 16, D03).

pin() reads the identity a review pins for one installed bundle: its path,
display name, CFBundleIdentifier, version and code-signing team. launch()
re-reads that identity at the reviewed path immediately before asking macOS
to open it, refuses any drift in bundle ID or team, and then reads the
running state back for up to LAUNCH_READBACK_SECONDS. The receipt only says
what was observed:

    launch_observed  True   a process whose executable lies inside the pinned
                            bundle was running within the readback window
                     False  none was observed within the window
                     None   the running state could not be read on this host

A launch receipt never shows that the app was operated or that the person's
task was done: `proves` is always 'launch_request_only'. Operating an app
needs a screen session (screen_session.py) with its own readback.

open runs as `open -a <pinned path>`, not `open -b <bundle id>`: LaunchServices
resolves a bundle ID to whichever registered copy it prefers (a download, a
mounted image), while the path is the copy whose identity was just re-checked.
The readback still reports another copy honestly as not observed.

Team identity comes from the embedded signature (`codesign -d`). That read
does not validate the signature; Gatekeeper and LaunchServices validate at
launch. Unsigned and ad-hoc bundles have team_id None, and a pinned None must
stay None. The running-state readback uses libproc (proc_listallpids,
proc_pidpath, proc_pidinfo): it reads process paths and start times only,
never windows, and needs no Accessibility or Screen Recording grant.
"""

import ctypes
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .policy import PolicyError


LAUNCH_READBACK_SECONDS = 5.0   # E-N1 proposal; tunable
READBACK_INTERVAL = 0.25
OPEN = '/usr/bin/open'
CODESIGN = '/usr/bin/codesign'
PROVES = 'launch_request_only'
MAX_PLIST = 1_048_576
CACHE_LIMIT = 512
BUNDLE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]{0,254}')
TEAM_ID = re.compile(r'[A-Z0-9]{10}')
SIGNATURES = ('team', 'unsigned', 'unknown')


def default_roots():
    return (Path('/Applications'), Path('/System/Applications'), Path('/System/Applications/Utilities'),
            Path('/System/Library/CoreServices'), Path('/System/Library/CoreServices/Applications'),
            Path.home() / 'Applications')


class Unavailable(Exception):
    """The running state cannot be read on this host."""


# ---- identity ---------------------------------------------------------------

_cache, _cache_lock = {}, threading.Lock()


def _stamp(path):
    """A cache key that changes when the bundle's plist or main executable changes."""
    info = os.stat(path / 'Contents/Info.plist')
    stamp = [str(path.resolve()), info.st_ino, info.st_mtime_ns, info.st_size]
    macos = path / 'Contents/MacOS'
    try:
        with os.scandir(macos) as entries:
            for entry in sorted(entries, key=lambda e: e.name)[:8]:
                st = entry.stat(follow_symlinks=False)
                stamp += [entry.name, st.st_ino, st.st_mtime_ns, st.st_size]
    except OSError:
        stamp.append(None)
    return tuple(stamp)


def codesign_team(path):
    """('team', id) | ('unsigned', None) | ('unknown', None) from the embedded signature."""
    try:
        done = subprocess.run([CODESIGN, '-d', '--verbose=2', str(path)], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return 'unknown', None
    text = done.stderr.decode('utf-8', 'replace')[:65_536]
    if done.returncode != 0:
        return ('unsigned', None) if 'not signed' in text else ('unknown', None)
    match = re.search(r'^TeamIdentifier=(.+)$', text, re.M)
    team = match.group(1).strip() if match else 'not set'
    if team == 'not set':
        return 'unsigned', None
    return ('team', team) if TEAM_ID.fullmatch(team) else ('unknown', None)


def _unread(path):
    return 'unknown', None


def _read_info(path):
    raw = (path / 'Contents/Info.plist').read_bytes()
    if len(raw) > MAX_PLIST:
        raise ValueError('Info.plist is too large.')
    info = plistlib.loads(raw)
    if not isinstance(info, dict):
        raise ValueError('Info.plist is not a dictionary.')
    return info


def identity(path, *, signer=None):
    """{'id','name','bundle_id','version','team_id','signature'} read now from the bundle at path.

    Raises PolicyError when the bundle is unreadable or has no valid bundle ID.
    """
    path = Path(path)
    signer = signer or codesign_team
    try:
        if path.suffix != '.app' or not path.is_dir():
            raise ValueError('not a bundle')
        key = (_stamp(path), signer)
        with _cache_lock:
            if key in _cache:
                return dict(_cache[key])
        info = _read_info(path)
    except (OSError, ValueError, plistlib.InvalidFileException):
        raise PolicyError('That app bundle cannot be read.') from None
    bundle_id = info.get('CFBundleIdentifier')
    if not isinstance(bundle_id, str) or not BUNDLE_ID.fullmatch(bundle_id):
        raise PolicyError('That app bundle has no valid bundle identifier.')
    name = info.get('CFBundleDisplayName') or info.get('CFBundleName') or path.stem
    version = info.get('CFBundleShortVersionString') or info.get('CFBundleVersion')
    signature, team = signer(path)
    if signature not in SIGNATURES or (signature == 'team') != (isinstance(team, str) and bool(TEAM_ID.fullmatch(team))):
        signature, team = 'unknown', None
    entry = {'id': str(path), 'name': name[:128] if isinstance(name, str) else path.stem,
             'bundle_id': bundle_id, 'version': version[:64] if isinstance(version, str) else None,
             'team_id': team, 'signature': signature, 'background_only': info.get('LSBackgroundOnly') is True}
    with _cache_lock:
        if len(_cache) >= CACHE_LIMIT:
            _cache.clear()
        _cache[key] = dict(entry)
    return entry


def _installed(path, roots):
    path = Path(path)
    roots = [Path(r) for r in roots]
    if path.suffix != '.app' or path.parent not in roots:
        return False
    return path.parent != Path('/System/Library/CoreServices') or path.stem == 'Finder'


def pin(path, *, roots=None, signer=None):
    """The identity a review pins for one installed app, or PolicyError."""
    if not isinstance(path, str) or not _installed(path, roots or default_roots()):
        raise PolicyError('App is not in the installed app inventory.')
    entry = identity(path, signer=signer)
    if entry.pop('background_only'):
        raise PolicyError('App is not in the installed app inventory.')
    return entry


def inventory(*, roots=None, signer=None, team=True):
    """Installed apps with pinned identity fields, sorted by name.

    team=False skips the signature read (no codesign subprocess); entries then
    have signature 'unknown' and team_id None and cannot be launched until
    pin() reads them.
    """
    if sys.platform != 'darwin' and roots is None:
        return []
    result = {}
    for root in [Path(r) for r in (roots or default_roots())]:
        try:
            paths = sorted(root.glob('*.app'))
        except OSError:
            continue
        for path in paths:
            if not _installed(str(path), [root]):
                continue
            try:
                entry = identity(path, signer=signer if team else _unread)
            except PolicyError:
                continue
            if not entry.pop('background_only'):
                result[entry['id']] = entry
    return sorted(result.values(), key=lambda e: e['name'].lower())


# ---- running-state readback (libproc) ---------------------------------------

PROC_PIDTBSDINFO = 3
BSDINFO_SIZE = 136              # struct proc_bsdinfo (XNU bsd/sys/proc_info.h)
BSDINFO_START = 120             # pbi_start_tvsec, then pbi_start_tvusec
PATH_MAX_SIZE = 4096
_libproc, _libproc_lock = None, threading.Lock()


def _proc():
    global _libproc
    with _libproc_lock:
        if _libproc is None:
            if sys.platform != 'darwin':
                raise Unavailable('libproc is macOS only.')
            try:
                lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
                lib.proc_listallpids.restype = ctypes.c_int
                lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
                lib.proc_pidpath.restype = ctypes.c_int
                lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
                lib.proc_pidinfo.restype = ctypes.c_int
                lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            except (OSError, AttributeError) as exc:
                raise Unavailable(type(exc).__name__) from None
            _libproc = lib
        return _libproc


def pid_path(pid):
    """The executable path of pid, or None when it cannot be read."""
    buffer = ctypes.create_string_buffer(PATH_MAX_SIZE)
    size = _proc().proc_pidpath(pid, buffer, PATH_MAX_SIZE)
    return buffer.raw[:size].decode('utf-8', 'surrogateescape') if size > 0 else None


def process_start(pid):
    """The start time of pid in epoch seconds, or None when it cannot be read."""
    buffer = ctypes.create_string_buffer(BSDINFO_SIZE)
    if _proc().proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buffer, BSDINFO_SIZE) != BSDINFO_SIZE:
        return None
    seconds = int.from_bytes(buffer.raw[BSDINFO_START:BSDINFO_START + 8], sys.byteorder)
    micros = int.from_bytes(buffer.raw[BSDINFO_START + 8:BSDINFO_START + 16], sys.byteorder)
    return seconds + micros / 1e6 if seconds > 0 else None


def _pids():
    lib = _proc()
    count = lib.proc_listallpids(None, 0)
    if count <= 0:
        raise Unavailable('The process list cannot be read.')
    capacity = count + 256
    buffer = (ctypes.c_int * capacity)()
    written = lib.proc_listallpids(buffer, ctypes.sizeof(buffer))
    if written <= 0:
        raise Unavailable('The process list cannot be read.')
    return [pid for pid in buffer[:min(written, capacity)] if pid > 0]


def _bundle_of(executable):
    index = executable.rfind('.app/Contents/MacOS/')
    return executable[:index + 4] if index > 0 else None


def processes():
    """[{'pid', 'start', 'executable', 'bundle_path'}] for running app-bundle executables.

    Each PID is transient lookup data for this call only; callers re-resolve on
    every use and never store one as a capability.
    """
    rows = []
    for pid in _pids():
        path = pid_path(pid)
        bundle = _bundle_of(path) if path else None
        start = process_start(pid) if bundle else None
        if bundle and start:
            rows.append({'pid': pid, 'start': start, 'executable': path, 'bundle_path': bundle})
    return rows


def running(bundle_path, *, lister=None):
    """Running instances whose executable lies inside bundle_path: [{'pid','start'}].

    Raises Unavailable when the process list cannot be read.
    """
    try:
        root = os.path.realpath(bundle_path)
    except (OSError, ValueError):
        raise Unavailable('The bundle path cannot be resolved.') from None
    found = []
    for row in (lister or processes)():
        try:
            if os.path.realpath(row['bundle_path']) == root:
                found.append({'pid': row['pid'], 'start': row['start']})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return found


def instances(bundle_id, *, lister=None, signer=None):
    """Running instances of bundle_id: [{'pid','start','bundle_id','team_id','signature','path'}].

    Used by screen sessions to re-resolve a granted app on every call.
    """
    found = []
    for row in (lister or processes)():
        try:
            if identity(row['bundle_path'], signer=_unread)['bundle_id'] != bundle_id:
                continue  # read signatures only for matching bundles
            entry = identity(row['bundle_path'], signer=signer)
        except (PolicyError, KeyError, TypeError):
            continue
        found.append({'pid': row['pid'], 'start': row['start'], 'bundle_id': bundle_id,
                      'team_id': entry['team_id'], 'signature': entry['signature'], 'path': entry['id']})
    return found


# ---- launch -----------------------------------------------------------------

def _opener(argv):
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        return None
    return done.returncode


def launch_argv(entry):
    return [OPEN, '-a', entry['id']]


def launch(entry, *, roots=None, signer=None, opener=None, lister=None, clock=time.monotonic, wall=time.time,
           sleep=time.sleep, readback_seconds=LAUNCH_READBACK_SECONDS):
    """Re-check the pinned identity, ask macOS to open the app, read the running state back.

    Returns {'requested','bundle_id','team_id','version','path','launch_observed','observed_at',
    'already_running','readback','proves','authorization'}. Raises PolicyError, with
    nothing opened, when the pin is missing or the bundle changed after review.
    """
    if not isinstance(entry, dict) or not isinstance(entry.get('id'), str):
        raise PolicyError('Select an installed app.')
    if 'bundle_id' not in entry or 'team_id' not in entry or 'signature' not in entry:
        raise PolicyError('This app approval predates identity pinning, so it was not opened. Propose opening '
                          'the app again.')
    current = pin(entry['id'], roots=roots, signer=signer)
    if current['signature'] == 'unknown' or entry['signature'] == 'unknown':
        raise PolicyError("The app's signing identity could not be read, so it was not opened.")
    if (current['bundle_id'], current['team_id'], current['signature']) != (
            entry['bundle_id'], entry['team_id'], entry['signature']):
        raise PolicyError('The app at the reviewed path changed after review (its bundle or signing team '
                          'differs), so it was not opened.')
    name = entry.get('name') if isinstance(entry.get('name'), str) else current['name']
    try:
        before = running(entry['id'], lister=lister)
    except Unavailable:
        before = None
    code = (opener or _opener)(launch_argv(entry))
    if code is None:
        raise PolicyError(f'The request to open {name} did not finish; the app may or may not be opening.')
    if code != 0:
        raise PolicyError(f'macOS did not accept the request to open {name}.')
    observed, observed_at, deadline = False, None, clock() + readback_seconds
    while True:
        try:
            found = running(entry['id'], lister=lister)
        except Unavailable:
            observed = None
            break
        if found:
            observed, observed_at = True, wall()
            break
        if clock() >= deadline:
            break
        sleep(READBACK_INTERVAL)
    return {'requested': name, 'bundle_id': current['bundle_id'], 'team_id': current['team_id'],
            'version': current['version'], 'version_changed': current['version'] != entry.get('version'),
            'path': entry['id'], 'launch_observed': observed,
            'observed_at': observed_at, 'already_running': None if before is None else bool(before),
            'readback': {True: 'running', False: 'not_running', None: 'unavailable'}[observed],
            'proves': PROVES, 'authorization': 'none'}


def describe(result):
    """The action summary. Never says the app was operated or the task done."""
    if not isinstance(result, dict):
        return 'macOS was asked to open an app (running: unknown).'
    if 'requested' not in result and isinstance(result.get('opened'), str):  # receipts from before Phase 16
        return f"macOS accepted the request to open {result['opened']} (running: unknown)."
    state = {True: 'observed', False: 'not observed'}.get(result.get('launch_observed'), 'unknown')
    return f"macOS accepted the request to open {result.get('requested') or 'the app'} (running: {state})."


OPERATE = re.compile(
    r'\b(open|launch|start)\b.{0,80}?\b(and|then)\b.{0,40}?\b(type|write|send|create|fill|click|press|play|search|'
    r'add|edit|draft|compose|make|set|change|delete|save|book|order|post|reply|share|enter|select)\b', re.I | re.S)


def operation_issue(request_text, launch_receipts, screen_evidence=()):
    """A completion issue when the person asked for work inside an app and only a launch is evidenced.

    Heuristic. screen_evidence is ScreenSessions.evidence(run_id); an input or
    commit with verdict 'occurred' counts as operating the app. A launch
    receipt never does.
    """
    if not launch_receipts or not isinstance(request_text, str) or not OPERATE.search(request_text):
        return None
    if any(e.get('verdict') == 'occurred' and e.get('class') in ('input', 'commit') for e in screen_evidence or ()):
        return None
    return ('The app was only opened (a launch request); nothing shows it was operated. Do not claim the task '
            'inside the app was done.')
