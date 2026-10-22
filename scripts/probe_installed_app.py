"""Read-only probes of a Pebble app bundle and its data home, and their reports (Phase 20, D20 section 4, T5).

  probe_installed_app.py CHECK --app PATH.app --source-snapshot FILE --report NEW [--home DIR] [--installed]
                         [--witness KEY=VALUE ...] [--evidence FILE ...] [check options]

Every run writes one new JSON report, created exclusively (an existing path is refused), bound to the
subject hashes the Phase 20 trust channel checks: app_bundle_sha256 (release_readiness.bundle_digest of the
whole .app), candidate_executable_sha256 (the bundled pebble-backend), candidate_bundle_sha256 (the bundled
backend folder, as verify_harness_candidate.candidate_manifest computes it), source_snapshot_sha256 (of the
file named by --source-snapshot) and the platform. A report holds the automated observations, any entries a
person made with --witness (recorded as witnessed, never as automated), the D20 pass criteria and a result:
passed, failed or needs_user. A check that needs a person stays needs_user until every entry of its
checklist is witnessed; a witnessed "no", or a number above its bound, fails it.

Automated checks: sidecar (resources equal to the stage manifest, the bundle's own o200k tokenizer, assets),
processes and webview_cpu (exact executable paths from libproc; ps for arguments and CPU time), logs (new
tracebacks in service.log and backend.log), data (migrations.inspect(home, deep=True), which is read-only),
banners, sleep_wake and timezone (rows read from a new read-only copy of pebble.sqlite: an SQLite online
backup into --scratch, then opened with mode=ro&immutable=1), login_item (the settings route's own output
files), cold_start (a frozen measure_resources report for this backend) and webview_store (a read-only listing
of WebKit's store). banners, login_item, sleep_wake, cold_start, webview_store, pet_gaze, frame_rate,
adhoc_replacement, containment, export_uninstall and accessibility also need a person's witness entries.

The probe never launches, quits or signals the app; it never writes into the app or the home, and never
uses the network, the Keychain, an account or a model. It refuses a home named com.pebble.local, and an app
in an installed location, unless --installed is given: pre-install checks run on the com.pebble.candidate
test bundle and its own home. Reading pebble.sqlite while its -wal or -shm exists uses SQLite's read-only
mode, which, like migrations.inspect, may create a missing -shm beside it. The --scratch copy holds that
home's data and is never deleted here; the parent keeps or removes it.

Exit codes: 0 passed, 10 needs_user, 20 failed, 2 refused or usage, 1 crash. The checklist, commands and
ordering are in docs/HARNESS_INSTALLED_VALIDATION.md.
"""

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform as host_platform
import plistlib
import re
import sqlite3
import stat
import sys
import tempfile
import time
import unicodedata
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'backend') not in sys.path:
    sys.path.insert(0, str(ROOT / 'backend'))

from pebble import migrations, token_budget  # noqa: E402
from pebble.release_readiness import (INSTALLED_PREFIXES, MAX_JSON_BYTES, EvidenceError, Missing, Root,  # noqa: E402
                                      Unsafe, bundle_digest, bundle_entries, parse_json, read_explicit, read_manifest,
                                      run_bounded, timestamp)

FORMAT = 'pebble.installed-probe/1'
PRODUCTION_ID = 'com.pebble.local'  # the test bundle is com.pebble.candidate (D20 section 1.6)
RESOURCES = 'Contents/Resources/resources'
BACKEND = 'backend/pebble-backend'
DATABASE = migrations.DATABASE
LOGS = ('service.log', 'backend.log')
MAX_LOG_BYTES, MAX_EVIDENCE_BYTES, MAX_SNAPSHOT_BYTES = 128_000_000, 128_000_000, 64_000_000
MAX_LISTED, MAX_ROUTE_BYTES = 50, 65_536
# The bundle's tokenizer must count this text exactly as o200k_base does (TokenBudget's exact path, never the
# one-token-per-byte estimate). PROBE_TOKENS is that count; the test recounts it with the checkout's bundle.
PROBE_TEXT = 'Pebble sidecar probe: the o200k_base ranks count this sentence exactly, never byte by byte. 12345'
PROBE_TOKENS = 25
COLD_START_P95_SECONDS = 5.0  # scripts/measure_resources.py THRESHOLDS['cold_start_p95_seconds'] (T8)
LOGIN_STATES = ('enabled', 'requires_approval', 'not_registered', 'not_found', 'unavailable')  # main.rs:151
TRACEBACK = b'Traceback (most recent call last):'
EXCEPTION = re.compile(rb'([A-Za-z_][\w.]{0,120})(?::|\r?\n?$)')
HELPER_FLAGS = ('--job-anchor', '--job-shim', '--job-canary', '--export-worker', '--hook-worker', '--mod-worker',
                '--mcp-http-worker', '--mcp-stdio-worker', '--mcp-schema-worker', '--mcp-media-image-worker',
                '--ax-worker', '--extract-pdf', '--extract-image', '--extract-file')  # backend/main.py
# WebKit's XPC services, by exact path: the system framework and the macOS 13+ Safari cryptex copy.
WEBKIT_ROOTS = ('/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/',
                '/System/Volumes/Preboot/Cryptexes/OS/System/Library/Frameworks/WebKit.framework/Versions/A/XPCServices/')
WEBKIT_SERVICES = ('com.apple.WebKit.WebContent', 'com.apple.WebKit.Networking', 'com.apple.WebKit.GPU')
WEBKIT_PATHS = frozenset(root + f'{name}.xpc/Contents/MacOS/{name}' for root in WEBKIT_ROOTS for name in WEBKIT_SERVICES)
EXIT = {'passed': 0, 'needs_user': 10, 'failed': 20}
OBSERVE = 'observe'  # a witness item answered yes or no and recorded either way; it never fails a check
MAX_STORE_ENTRIES = 20_000
LIMITS = (
    'A probe observes one bundle, one home and this host at one time; it is evidence for an operator attestation, '
    'not an attestation itself.',
    'Witness entries are a person\'s statements, recorded as entered; the probe cannot check them.',
    'Process paths come from libproc proc_pidpath for same-user processes; arguments come from ps and are only '
    'used to tell the service from helper processes of the same executable.',
    'Tracebacks are found by their header line. Logs carry no timestamps, so a traceback is new when it lies '
    'after a --baseline offset, old when the file was last modified before --since, and otherwise of unknown '
    'time, which fails the check.',
    'WebKit processes are attributed to Pebble only through --webkit-pid; otherwise every WebKit process on the '
    'host is summed as an upper bound.',
)


class Refused(Exception):
    """The probe cannot run on these inputs; nothing is written."""

    def __init__(self, code, detail=None):
        super().__init__(code if detail is None else f'{code}: {detail}')
        self.code, self.detail = code, detail


def sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def now_utc():
    return datetime.now(timezone.utc)


def subject_platform():
    """The subject's platform label, as the trust channel (T3 attestation.subject_platform) computes it."""
    label = f'{sys.platform}-{host_platform.machine()}'.lower()
    return label if re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,31}', label) else 'unknown'


def read_stage_manifest(data):
    """T1's C/stage/stage-manifest.json: release_readiness.read_manifest on its {path, bytes, sha256} entries and
    {path, target, kind} links. Extra per-entry keys (T1 adds mode and source) are set aside; modes are returned."""
    try:
        doc = parse_json(data, 'stage_manifest')
    except EvidenceError as error:
        raise Refused(error.code, 'stage_manifest') from None
    files, links = (doc.get('files'), doc.get('links', [])) if isinstance(doc, dict) else (None, None)
    if (not isinstance(files, list) or not isinstance(links, list)
            or not all(isinstance(item, dict) for item in files + links)):
        raise Refused('schema', 'stage_manifest')
    core = {'files': [{key: item.get(key) for key in ('path', 'bytes', 'sha256')} for item in files],
            'links': [{key: item.get(key) for key in ('path', 'target', 'kind')} for item in links],
            'bundle_sha256': doc.get('bundle_sha256')}
    try:
        manifest = read_manifest(json.dumps(core).encode())
    except EvidenceError as error:
        raise Refused(error.code, 'stage_manifest') from None
    modes = {item['path']: item['mode'] for item in files if 'mode' in item}
    if not modes and isinstance(doc.get('modes'), dict):
        # Merge (i20): T1 writes the modes as one path-keyed map beside `files`, not per entry.
        listed = {entry['path'] for entry in manifest['files']}
        modes = {path: mode for path, mode in doc['modes'].items() if path in listed}
    if any(not (isinstance(mode, str) and re.fullmatch(r'[0-7]{4}', mode)) for mode in modes.values()):
        raise Refused('schema', 'stage_manifest.mode')
    return manifest, modes


# ---- Native observation (injectable) ------------------------------------------------------------

_PIDPATH = []


def pid_path(pid):
    """The kernel's executable path for a same-user pid (libproc proc_pidpath), else None."""
    if not _PIDPATH:
        try:
            lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            function = lib.proc_pidpath
            function.restype, function.argtypes = ctypes.c_int, [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            _PIDPATH.append(function)
        except (OSError, AttributeError):
            _PIDPATH.append(None)
    function = _PIDPATH[0]
    if function is None:
        return None
    buffer = ctypes.create_string_buffer(4096)
    size = function(pid, buffer, 4096)
    return os.fsdecode(buffer.raw[:size]) if size > 0 else None


def list_processes():
    """Every process as {pid, ppid, path, args}; path is the exact executable path or None when unreadable."""
    out = run_bounded(['/bin/ps', '-axww', '-o', 'pid=,ppid=,args='], seconds=20, limit=16_000_000)
    if out['timed_out'] or out['truncated'] or out['exit'] != 0:
        raise OSError('ps did not complete')
    rows = []
    for line in out['text'].splitlines():
        match = re.match(r'\s*(\d+)\s+(\d+)\s?(.*)$', line)
        if match:
            pid = int(match.group(1))
            rows.append({'pid': pid, 'ppid': int(match.group(2)), 'args': match.group(3), 'path': pid_path(pid)})
    return rows


def parse_cputime(text):
    """Seconds from ps's time column: [dd-][hh:]mm:ss[.cc]."""
    match = re.fullmatch(r'\s*(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)\s*', text)
    if not match:
        raise ValueError('Unrecognized ps time.')
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def ps_cpu_times(pids):
    """{pid: cumulative user+system CPU seconds} from ps for the pids still alive."""
    out = run_bounded(['/bin/ps', '-o', 'pid=,time=', '-p', ','.join(str(pid) for pid in pids)], seconds=20,
                      limit=1_000_000)
    if out['timed_out'] or out['truncated'] or out['exit'] not in (0, 1):
        raise OSError('ps did not complete')
    times = {}
    for line in out['text'].splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            times[int(parts[0])] = parse_cputime(parts[1])
    return times


@dataclass
class Hooks:
    processes: object = list_processes   # () -> [{pid, ppid, path, args}]
    cpu_times: object = ps_cpu_times     # (pids) -> {pid: seconds}
    clock: object = time.monotonic
    sleep: object = time.sleep
    now: object = now_utc


# ---- Inputs ------------------------------------------------------------------------------------


def installed_location(path):
    home = os.path.realpath(os.path.expanduser('~'))
    prefixes = tuple(_fold(p) for p in INSTALLED_PREFIXES + (os.path.join(home, 'Applications') + '/',))
    return _fold(path.rstrip('/') + '/').startswith(prefixes)  # X20 F20-05: macOS volumes ignore case


def app_path(value, installed):
    text = os.path.abspath(value)
    if not text.endswith('.app'):
        raise Refused('app_not_bundle', value)
    # The lexical path is judged before anything at it is resolved or read.
    if not installed and (installed_location(text) or installed_location(os.path.realpath(text))):
        raise Refused('installed_app_needs_installed', 'An installed location is probed only with --installed.')
    real = os.path.realpath(text)
    try:
        info = os.lstat(text)
    except OSError:
        raise Refused('app_missing', value) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refused('app_not_bundle', value)
    return Path(real)


def bundle_info(app):
    try:
        plist = plistlib.loads(read_explicit(app / 'Contents/Info.plist', 4_000_000, 'info_plist'))
    except EvidenceError as error:
        raise Refused(error.code, 'Contents/Info.plist') from None
    except (plistlib.InvalidFileException, ValueError, OverflowError):
        raise Refused('info_plist_invalid') from None
    identifier, executable = plist.get('CFBundleIdentifier'), plist.get('CFBundleExecutable')
    if (not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9.-]{1,155}', identifier)
            or not isinstance(executable, str) or not re.fullmatch(r'[^/\x00]{1,255}', executable)
            or executable in ('.', '..')):
        raise Refused('info_plist_invalid')
    version = plist.get('CFBundleShortVersionString')
    build = plist.get('CFBundleVersion')
    return {'identifier': identifier, 'executable': executable,
            'version': version if isinstance(version, str) else None, 'build': build if isinstance(build, str) else None}


def home_path(value, identifier, installed):
    text = os.path.abspath(value)
    real = os.path.realpath(text)
    if PRODUCTION_ID in (os.path.basename(text), os.path.basename(real)) and not installed:
        raise Refused('production_home_needs_installed',
                      'The com.pebble.local home is probed only with --installed; use the candidate test home.')
    try:
        info = os.lstat(text)
    except OSError:
        raise Refused('home_missing', value) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refused('home_not_directory', value)
    if os.path.basename(real) != identifier:
        raise Refused('home_identifier_mismatch', 'The home folder must be named after the bundle identifier.')
    return Path(real)


def under(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def inside(path, root):
    return under(os.path.realpath(path), os.path.realpath(root))


def _fold(text):
    return unicodedata.normalize('NFC', text).casefold()


def kernel_under(path, root):
    """X20 F20-05: a kernel image path inside root on a case-insensitive volume, in either firmlink spelling."""
    folded, base = _fold(path), root.rstrip('/')
    forms = {_fold(base)}
    if not base.startswith('/System/Volumes/Data/'):
        forms.add(_fold('/System/Volumes/Data' + base))
    return any(folded == form or folded.startswith(form + '/') for form in forms)


def kernel_same(path, target):
    return _fold(path) in {_fold(target), _fold('/System/Volumes/Data' + target)}


def new_path(value, what, roots):
    path = Path(os.path.abspath(value))
    if os.path.lexists(path):
        raise Refused(what + '_exists', str(path))
    if not path.parent.is_dir():
        raise Refused(what + '_parent_missing', str(path.parent))
    for root in roots:
        if root is not None and inside(path.parent, root):
            raise Refused(what + '_inside_subject', 'Nothing is written into the app or its home.')
    return path


def since_time(value, now):
    when = timestamp(value)
    if when is None:
        raise Refused('since_invalid', 'Use an ISO 8601 time with a zone, such as 2026-10-13T09:00:00+00:00.')
    if (when - now).total_seconds() > 60:
        raise Refused('since_in_future', 'A future --since would hide new evidence.')
    return when


def parse_witness(check, items):
    bounds = {key: bound for key, bound, _ in check.checklist}
    out = {}
    for item in items:
        key, separator, value = item.partition('=')
        key, value = key.strip(), value.strip()
        if not separator or key not in bounds:
            raise Refused('witness_unknown_key', f'{check.name} accepts ' + (', '.join(bounds) or 'no witness entries'))
        if key in out:
            raise Refused('witness_duplicate', key)
        if bounds[key] is None or bounds[key] == OBSERVE:
            if value.lower() not in ('yes', 'no'):
                raise Refused('witness_value', f'{key} takes yes or no')
            out[key] = value.lower()
        else:
            try:
                number = float(value)
            except ValueError:
                number = math.nan
            if not math.isfinite(number) or number < 0:
                raise Refused('witness_value', f'{key} takes a nonnegative number')
            out[key] = number
    return out


def file_record(value, what, limit=MAX_EVIDENCE_BYTES):
    try:
        data = read_explicit(value, limit, what)
    except EvidenceError as error:
        raise Refused(error.code, f'{what}: {value}') from None
    return data, {'path': os.path.abspath(value), 'sha256': sha_bytes(data), 'bytes': len(data)}


# ---- Subject -----------------------------------------------------------------------------------


def walk(path, what):
    """release_readiness bundle entries of one real directory, else (None, None, problems)."""
    try:
        with Root(str(path), what) as root:
            return bundle_entries(root)
    except (EvidenceError, Unsafe) as error:
        return None, None, [(error.code, what)]


def compute_subject(app, snapshot_sha):
    entries, _, problems = walk(app, 'app')
    if entries is None or problems:
        raise Refused('app_bundle_unsafe', ', '.join(f'{code} {where}' for code, where in problems[:4]))
    executable = next((entry for entry in entries if entry['path'] == RESOURCES + '/' + BACKEND), None)
    if executable is None:
        raise Refused('bundled_backend_missing', RESOURCES + '/' + BACKEND)
    backend, _, problems = walk(app / RESOURCES / 'backend', 'bundled_backend')
    if backend is None or problems:
        raise Refused('bundled_backend_unsafe', ', '.join(f'{code} {where}' for code, where in problems[:4]))
    subject = {'source_snapshot_sha256': snapshot_sha, 'candidate_executable_sha256': executable['sha256'],
               'candidate_bundle_sha256': bundle_digest(backend), 'app_bundle_sha256': bundle_digest(entries),
               'platform': subject_platform()}
    return subject, {'files': len(entries), 'bytes': sum(entry['bytes'] for entry in entries),
                     'backend_files': len(backend)}


# ---- Checks ------------------------------------------------------------------------------------


@dataclass
class Context:
    args: object
    app: Path
    info: dict
    home: object
    subject: dict
    hooks: Hooks
    now: datetime
    evidence: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    scratch_record: object = None

    def finding(self, code, where=None):
        if len(self.findings) < 500:
            self.findings.append({'code': code, 'subject': where})

    def since(self):
        if not self.args.since:
            raise Refused('since_required', f'{self.args.check} needs --since.')
        return since_time(self.args.since, self.now)

    def shell_path(self):
        return str(self.app / 'Contents/MacOS' / self.info['executable'])

    def backend_path(self):
        return str(self.app / RESOURCES / BACKEND)


@dataclass(frozen=True)
class Check:
    name: str
    row: str
    star: bool                 # repeated on the installed app after install (D20 section 4)
    needs_home: bool
    criteria: tuple
    run: object = None         # (ctx) -> (state, observations); state: passed, failed, inconclusive or none
    checklist: tuple = ()      # (key, bound, prompt); bound None is yes/no, a number is an upper bound


def resource_modes(ctx, resources, links, modes, paths):
    """Paths whose permission bits differ from the stage manifest's (T1 records four octal digits per file)."""
    if not modes:
        return []
    linked, changed = {link['path'] for link in links}, []
    try:
        with Root(str(resources), 'app_resources') as root:
            for rel in sorted(path for path in paths if path in modes):
                try:
                    info = root.resolve(rel.split('/'))[1] if rel in linked else root.lstat(rel)
                except (Missing, Unsafe):
                    changed.append(rel)
                    continue
                if '%04o' % stat.S_IMODE(info.st_mode) != modes[rel]:
                    changed.append(rel)
    except EvidenceError:
        return []  # the resources walk already reported it
    for rel in changed:
        ctx.finding('resource_mode_differs', rel)
    return changed


def link_changes(ctx, expected, actual, files):
    """Links that differ from the stage. A file link the bundler flattened into a regular file with the same bytes,
    or a directory link dropped with nothing in its place, is recorded as flattened, as T1 accepts; any other
    difference is a finding."""
    want, have = {link['path']: link for link in expected}, {link['path']: link for link in actual}
    flattened = []
    for path in sorted(want.keys() | have.keys()):
        old, new = want.get(path), have.get(path)
        if old == new:
            continue
        if old and not new and ((old['kind'] == 'file' and path in files) or (
                old['kind'] == 'directory' and not any(p == path or p.startswith(path + '/') for p in files))):
            flattened.append(path)
            continue
        ctx.finding('resource_link_differs', path)
    return flattened


def check_sidecar(ctx):
    if not ctx.args.stage_manifest:
        raise Refused('stage_manifest_required', 'sidecar needs --stage-manifest C/stage/stage-manifest.json.')
    data, record = file_record(ctx.args.stage_manifest, 'stage_manifest', MAX_JSON_BYTES)
    manifest, modes = read_stage_manifest(data)
    expected = {entry['path']: entry for entry in manifest['files']}
    observed = {'stage_manifest': {**record, 'bundle_sha256': manifest['bundle_sha256'], 'files': len(expected)}}
    if bundle_digest(manifest['files']) != manifest['bundle_sha256']:
        ctx.finding('stage_manifest_digest_mismatch')
    resources = ctx.app / RESOURCES
    entries, links, problems = walk(resources, 'app_resources')
    for code, where in problems:
        ctx.finding('resources_' + code, where)
    actual = {entry['path']: entry for entry in entries or ()}
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    differing = sorted(path for path in set(expected) & set(actual)
                       if (expected[path]['sha256'], expected[path]['bytes']) != (actual[path]['sha256'], actual[path]['bytes']))
    for code, paths in (('resource_missing', missing), ('resource_unexpected', unexpected), ('resource_differs', differing)):
        for path in paths:
            ctx.finding(code, path)
    mode_changes = resource_modes(ctx, resources, links or [], modes, set(expected) & set(actual))
    flattened = link_changes(ctx, manifest.get('links') or [], links or [], actual) if entries is not None else []
    observed['resources'] = {
        'files': len(actual), 'bundle_sha256': bundle_digest(entries) if entries is not None else None,
        'equal_to_stage_manifest': entries is not None and not (problems or missing or unexpected or differing
                                                                 or mode_changes),
        'missing': missing[:MAX_LISTED], 'unexpected': unexpected[:MAX_LISTED], 'differing': differing[:MAX_LISTED],
        'mode_differs': mode_changes[:MAX_LISTED], 'flattened_links': flattened[:MAX_LISTED],
        'modes_compared': bool(modes),
        'counts': {'missing': len(missing), 'unexpected': len(unexpected), 'differing': len(differing),
                   'mode_differs': len(mode_changes)}}
    assets = {}
    for rel in (BACKEND, 'llama/llama-server'):
        entry = actual.get(rel)
        mode = None
        if entry is not None:
            try:
                mode = os.lstat(resources / rel).st_mode
            except OSError:
                pass
        assets[rel] = {'present': entry is not None,
                       'executable': mode is not None and stat.S_ISREG(mode) and bool(mode & 0o100)}
        if not assets[rel]['present']:
            ctx.finding('asset_missing', rel)
        elif not assets[rel]['executable']:
            ctx.finding('asset_not_executable', rel)
    observed['assets'] = assets
    observed['tokenizer'] = tokenizer_probe(ctx, resources, actual)
    return ('failed' if ctx.findings else 'passed'), observed


def tokenizer_probe(ctx, resources, actual):
    rel = 'tokenizer/' + token_budget.TOKENIZER_FILE
    entry = actual.get(rel)
    result = {'file': rel, 'present': entry is not None, 'sha256': entry and entry['sha256'],
              'published_sha256': token_budget.TOKENIZER_SHA256, 'probe_text_bytes': len(PROBE_TEXT.encode()),
              'expected_tokens': PROBE_TOKENS, 'counted_tokens': None, 'method': None}
    if entry is None:
        ctx.finding('tokenizer_missing', rel)
        return result
    if entry['sha256'] != token_budget.TOKENIZER_SHA256:
        ctx.finding('tokenizer_digest_mismatch', rel)
        return result
    try:
        encoding = token_budget._load(resources / 'tokenizer')  # the product's loader: never downloads
    except ImportError:
        ctx.finding('tokenizer_library_unavailable', 'tiktoken')
        return result
    if encoding is None:
        ctx.finding('tokenizer_not_loaded', rel)
        return result
    counted = len(encoding.encode(PROBE_TEXT, disallowed_special=()))
    result.update(counted_tokens=counted, method='o200k_bundled' if counted != result['probe_text_bytes'] else 'byte_estimate')
    if counted != PROBE_TOKENS:
        ctx.finding('tokenizer_count_mismatch', f'{counted} != {PROBE_TOKENS}')
    return result


def process_table(ctx):
    try:
        rows = ctx.hooks.processes()
    except OSError:
        ctx.finding('process_list_unavailable')
        return None
    if not isinstance(rows, list):
        ctx.finding('process_list_unavailable')
        return None
    return rows


def role(args, path):
    tokens = (args[len(path):] if args.startswith(path) else args).split()
    return 'helper' if any(token in HELPER_FLAGS for token in tokens) else 'service' if '--home' in tokens else 'other'


def check_processes(ctx, expect=None):
    rows = process_table(ctx)
    if rows is None:
        return 'failed', {}
    app, shell, backend = str(ctx.app), ctx.shell_path(), ctx.backend_path()
    names = {os.path.basename(shell), os.path.basename(backend)}
    unreadable = sum(1 for row in rows if not row.get('path'))
    # Kernel executable paths are already real and never resolved; X20 F20-05: compared casefolded, in either
    # firmlink spelling, because macOS volumes ignore case.
    ours = [row for row in rows if row.get('path') and kernel_under(row['path'], app)]
    shells = [row for row in ours if kernel_same(row['path'], shell)]
    backends = [row for row in ours if kernel_same(row['path'], backend)]
    services = [row for row in backends if role(row.get('args') or '', backend) == 'service']
    helpers = [row for row in backends if row not in services]
    others = [row for row in ours if row not in shells and row not in backends]
    # Observed by name only to report them; nothing here ever signals a process.
    foreign = [row for row in rows if row.get('path') and not kernel_under(row['path'], app)
               and os.path.basename(row['path']) in names and '/Contents/' in row['path']]
    expect = expect or ctx.args.expect or 'running'

    def brief(row):
        return {'pid': row['pid'], 'ppid': row['ppid'], 'path': row['path']}
    observed = {'expect': expect, 'shell_path': shell, 'backend_path': backend, 'processes_listed': len(rows),
                'paths_unreadable': unreadable, 'shell': [brief(row) for row in shells],
                'service': [brief(row) for row in services], 'helpers': [brief(row) for row in helpers],
                'other_from_app': [brief(row) for row in others][:MAX_LISTED],
                'other_copies': [brief(row) for row in foreign][:MAX_LISTED]}
    if expect == 'absent':
        for row in ours:
            ctx.finding('process_from_app_running', row['path'])
        return ('failed' if ours else 'passed'), observed
    if len(shells) != 1:
        ctx.finding('shell_count', str(len(shells)))
    if len(services) != 1:
        ctx.finding('service_count', str(len(services)))
    if len(shells) == 1 and len(services) == 1:
        if services[0]['ppid'] != shells[0]['pid']:
            ctx.finding('service_not_child_of_shell')
        if ctx.home is not None:
            args = services[0].get('args') or ''
            matches = any(f'--home {candidate} ' in args + ' ' for candidate in {str(ctx.home), os.path.abspath(ctx.args.home)})
            observed['service_home_matches'] = matches
            if not matches:
                ctx.finding('service_home_mismatch')
    # After install exactly one copy may run (section 2.6). Before install the user's own app may run beside the
    # test bundle, so other copies are only reported.
    if ctx.args.installed:
        for row in foreign:
            ctx.finding('other_pebble_copy_running', row['path'])
    return ('failed' if ctx.findings else 'passed'), observed


def tracebacks(data):
    """(byte offset, exception type) for each traceback header in data; messages are never kept."""
    found, lines, offset = [], data.splitlines(keepends=True), 0
    for index, line in enumerate(lines):
        if line.startswith(TRACEBACK):
            kind = None
            for later in lines[index + 1:index + 400]:
                if later.strip() and not later[:1].isspace():
                    match = EXCEPTION.match(later)
                    kind = match.group(1).decode('ascii') if match else None
                    break
            found.append((offset, kind))
        offset += len(line)
    return found


def read_baseline(ctx):
    if not ctx.args.baseline:
        return None, None
    data, record = file_record(ctx.args.baseline, 'baseline', MAX_JSON_BYTES)
    try:
        doc = parse_json(data, 'baseline')
    except EvidenceError as error:
        raise Refused(error.code, 'baseline') from None
    logs = ((doc.get('automated') or {}).get('logs') if isinstance(doc, dict) else None)
    if (not isinstance(doc, dict) or doc.get('format') != FORMAT or doc.get('check') != 'logs'
            or not isinstance(logs, dict) or not isinstance(doc.get('home'), dict)):
        raise Refused('baseline_not_logs_report')
    if doc['home'].get('path') != str(ctx.home):
        raise Refused('baseline_other_home')
    return logs, record


def check_logs(ctx):
    since = ctx.since().timestamp()
    baseline, record = read_baseline(ctx)
    observed = {'since': ctx.args.since, 'baseline': record, 'logs': {}}
    for name in LOGS:
        path = ctx.home / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            observed['logs'][name] = {'exists': False}
            continue
        except OSError:
            ctx.finding('log_unreadable', name)
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            ctx.finding('log_not_regular_file', name)
            continue
        try:
            data = read_explicit(path, MAX_LOG_BYTES, name)
        except EvidenceError as error:
            ctx.finding('log_' + error.code, name)
            continue
        cut = None
        base = (baseline or {}).get(name)
        if isinstance(base, dict) and base.get('exists'):
            size = base.get('bytes')
            if (type(size) is int and 0 <= size <= len(data) and base.get('inode') == info.st_ino
                    and sha_bytes(data[:size]) == base.get('sha256')):
                cut = size
            else:
                ctx.finding('log_baseline_mismatch', name)
        elif isinstance(base, dict):
            cut = 0  # the log did not exist at the baseline, so all of it is new
        counts, new_types = {'total': 0, 'new': 0, 'old': 0, 'unknown': 0}, []
        for offset, kind in tracebacks(data):
            counts['total'] += 1
            if cut is not None:
                state = 'new' if offset >= cut else 'old'
            else:
                state = 'old' if info.st_mtime < since else 'unknown'
            counts[state] += 1
            if state != 'old':
                new_types.append(kind or 'unrecognized')
                ctx.finding('new_traceback' if state == 'new' else 'traceback_time_unknown', f'{name}: {kind or "unrecognized"}')
        observed['logs'][name] = {'exists': True, 'bytes': len(data), 'sha256': sha_bytes(data), 'inode': info.st_ino,
                                  'modified': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                                  'modified_after_since': info.st_mtime >= since,
                                  'judged_by': 'baseline_offset' if cut is not None else 'modification_time',
                                  'tracebacks': counts, 'new_exception_types': new_types[:MAX_LISTED]}
    return ('failed' if ctx.findings else 'passed'), observed


def expected_rows(ctx):
    if not ctx.args.expect_rows:
        return None, None
    data, record = file_record(ctx.args.expect_rows, 'expect_rows', MAX_JSON_BYTES)
    try:
        doc = parse_json(data, 'expect_rows')
    except EvidenceError as error:
        raise Refused(error.code, 'expect_rows') from None
    rows = None
    if isinstance(doc, dict):
        for path in (('rows',), ('database', 'rows'), ('inspect', 'database', 'rows')):
            value = doc
            for key in path:
                value = value.get(key) if isinstance(value, dict) else None
            if value is not None:
                rows = value
                break
    if (not isinstance(rows, dict) or not rows
            or not all(isinstance(key, str) and type(count) is int and count >= 0 for key, count in rows.items())):
        raise Refused('expect_rows_invalid', 'Give {"rows": {...}}, an inspect report or a backup record holding one.')
    return rows, record


def check_data(ctx):
    expected, record = expected_rows(ctx)
    report = migrations.inspect(ctx.home, deep=True)
    database = report.get('database') or {}
    backups = report.get('backups') or []
    if report.get('state') != 'current':
        ctx.finding('state_not_current', str(report.get('state')))
    if database.get('user_version') != migrations.SCHEMA_EPOCH:
        ctx.finding('epoch_mismatch', str(database.get('user_version')))
    # inspect counts only backup-bound upgrades: a fresh test home has none, an upgraded installed home at least one.
    if ctx.args.upgraded and not (database.get('upgrade_history') or 0) >= 1:
        ctx.finding('upgrade_history_missing')
    if ctx.args.upgraded and not any(backup.get('verified') is True for backup in backups):
        ctx.finding('no_verified_backup')
    if database.get('integrity') != 'ok':
        ctx.finding('integrity_not_ok', str(database.get('integrity')))
    if database.get('refusal'):
        ctx.finding('refusal', str(database['refusal']))
    for backup in backups:
        if backup.get('verified') is False or not backup.get('data_present'):
            ctx.finding('backup_unverified', backup.get('id'))
    comparison = None
    if expected is not None:
        rows = database.get('rows') or {}
        changed = sorted(table for table, count in expected.items() if table in rows and rows[table] != count)
        absent = sorted(table for table in expected if table not in rows)
        for table in changed:
            ctx.finding('row_count_changed', table)
        for table in absent:
            ctx.finding('table_missing', table)
        comparison = {'source': record, 'tables': len(expected), 'changed': changed[:MAX_LISTED],
                      'missing': absent[:MAX_LISTED]}
    return ('failed' if ctx.findings else 'passed'), {'inspect': report, 'expected_rows': comparison}


def database_copy(ctx):
    """A new read-only copy of pebble.sqlite in --scratch, opened with mode=ro&immutable=1."""
    source = ctx.home / DATABASE
    try:
        info = os.lstat(source)
    except OSError:
        ctx.finding('database_missing', DATABASE)
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        ctx.finding('database_not_regular_file', DATABASE)
        return None
    if ctx.args.scratch:
        scratch = Path(ctx.args.scratch)
        scratch.mkdir(mode=0o700)
    else:
        scratch = Path(tempfile.mkdtemp(prefix='pebble-p20-probe-', dir='/private/tmp' if os.path.isdir('/private/tmp') else None))
    target = scratch / ('copy-' + DATABASE)
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600))
    live = any(os.path.lexists(str(source) + suffix) for suffix in migrations.SIDECARS)
    uri = source.as_uri() + ('?mode=ro' if live else '?mode=ro&immutable=1')
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as reader, closing(sqlite3.connect(target)) as copy:
        reader.backup(copy)
    os.chmod(target, 0o400)
    ctx.scratch_record = {'path': str(target), 'sha256': sha_bytes(target.read_bytes()), 'bytes': target.stat().st_size,
                          'source_mode': 'read_only' if live else 'immutable', 'holds_home_data': True}
    return sqlite3.connect(target.as_uri() + '?mode=ro&immutable=1', uri=True)


def query(ctx, db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.Error:
        ctx.finding('query_failed', sql.split(' FROM ')[-1].split()[0])
        return None


def with_copy(ctx, read):
    db = database_copy(ctx)
    if db is None:
        return 'failed', {}
    with closing(db):
        state, observed = read(db)
    observed['database_copy'] = ctx.scratch_record
    return state, observed


def check_banners(ctx):
    since = ctx.since().timestamp()

    def read(db):
        banners = query(ctx, db, 'SELECT scope,state,count(*) FROM native_banners WHERE opened>=? GROUP BY scope,state', (since,))
        deliveries = query(ctx, db, 'SELECT state,count(*) FROM native_deliveries WHERE created>=? GROUP BY state', (since,))
        notices = query(ctx, db, 'SELECT kind,count(*) FROM job_notifications WHERE created>=? GROUP BY kind', (since,))
        if None in (banners, deliveries, notices):
            return 'failed', {}
        delivered = sum(count for scope, state, count in banners if state == 'delivered')
        failed = sum(count for scope, state, count in banners if state == 'failed')
        if not delivered:
            ctx.finding('no_delivered_banner')
        if failed:
            ctx.finding('banner_failed', str(failed))
        observed = {'since': ctx.args.since,
                    'banners': [{'scope': scope, 'state': state, 'count': count} for scope, state, count in banners],
                    'deliveries': dict(deliveries), 'job_notices': dict(notices)}
        return ('failed' if ctx.findings else 'passed'), observed
    return with_copy(ctx, read)


def check_sleep_wake(ctx):
    since = ctx.since().timestamp()
    minimum = ctx.args.min_sleep_seconds

    def read(db):
        rows = query(ctx, db, 'SELECT kind,source,count(*),max(seconds) FROM lifecycle_observations WHERE observed>=? '
                              'GROUP BY kind,source', (since,))
        wakes = query(ctx, db, "SELECT count(*) FROM host_notices WHERE kind='wake_summary' AND created>=?", (since,))
        catch_up = query(ctx, db, 'SELECT job_id,count(*) FROM job_fires WHERE claimed>=? AND missed>0 GROUP BY job_id', (since,))
        if None in (rows, wakes, catch_up):
            return 'failed', {}
        kinds = {}
        for kind, source, count, longest in rows:
            item = kinds.setdefault(kind, {'count': 0, 'longest_seconds': 0.0, 'sources': []})
            item['count'] += count
            item['longest_seconds'] = max(item['longest_seconds'], longest or 0.0)
            item['sources'].append(source)
        for kind in ('will_sleep', 'did_wake', 'sleep'):
            if kind not in kinds:
                ctx.finding('lifecycle_row_missing', kind)
        if 'sleep' in kinds and kinds['sleep']['longest_seconds'] < minimum:
            ctx.finding('sleep_too_short', str(round(kinds['sleep']['longest_seconds'], 1)))
        if wakes[0][0] > 1:
            ctx.finding('wake_summary_repeated', str(wakes[0][0]))
        for job, count in catch_up:
            if count > 1:
                ctx.finding('catch_up_repeated', f'{count} fires')
        observed = {'since': ctx.args.since, 'minimum_sleep_seconds': minimum, 'observations': kinds,
                    'wake_summaries': wakes[0][0], 'catch_up_fires_per_job': sorted(count for _, count in catch_up)}
        return ('failed' if ctx.findings else 'passed'), observed
    return with_copy(ctx, read)


def check_timezone(ctx):
    since = ctx.since().timestamp()

    def read(db):
        rows = query(ctx, db, "SELECT zone,source FROM lifecycle_observations WHERE kind='timezone_changed' AND observed>=? "
                              'ORDER BY rowid', (since,))
        if rows is None:
            return 'failed', {}
        if len(rows) != 1:
            ctx.finding('timezone_rows', str(len(rows)))
        elif ctx.args.expect_zone and rows[0][0] != ctx.args.expect_zone:
            ctx.finding('timezone_unexpected', rows[0][0])
        observed = {'since': ctx.args.since, 'rows': [{'zone': zone, 'source': source} for zone, source in rows][:MAX_LISTED],
                    'expect_zone': ctx.args.expect_zone}
        return ('failed' if ctx.findings else 'passed'), observed
    return with_copy(ctx, read)


def check_login_item(ctx):
    outputs = []
    for value in ctx.args.route_output or ():
        data, record = file_record(value, 'route_output', MAX_ROUTE_BYTES)
        try:
            doc = parse_json(data, 'route_output')
        except EvidenceError as error:
            raise Refused(error.code, 'route_output') from None
        if not isinstance(doc, dict) or set(doc) != {'status'} or doc['status'] not in LOGIN_STATES:
            raise Refused('route_output_invalid', 'Give the login_item_status or login_item_set output: {"status": ...}.')
        outputs.append({**record, 'status': doc['status']})
    statuses = [item['status'] for item in outputs]
    observed = {'route_outputs': outputs, 'origin': 'supplied output of the app\'s own settings route; the probe '
                                                  'does not call the route'}
    for status in statuses:
        if status in ('not_found', 'unavailable'):
            ctx.finding('login_item_' + status)
    if ctx.findings:
        return 'failed', observed
    enabled = statuses.index('enabled') if 'enabled' in statuses else None
    complete = enabled is not None and 'not_registered' in statuses[enabled + 1:]
    observed['sequence_complete'] = complete
    return ('passed' if complete else 'inconclusive'), observed


def check_webview_cpu(ctx):
    if ctx.args.max_cpu_percent is None:
        raise Refused('max_cpu_percent_required', 'webview_cpu judges only against a budget fixed before the run.')
    seconds, interval = ctx.args.seconds, ctx.args.interval
    if not 1 <= seconds <= 600 or not 0.5 <= interval <= 60:
        raise Refused('window_invalid', 'Use 1-600 seconds and a 0.5-60 second interval.')
    rows = process_table(ctx)
    if rows is None:
        return 'failed', {}
    shells = [row for row in rows if row.get('path') == ctx.shell_path()]
    webkit = [row for row in rows if row.get('path') in WEBKIT_PATHS]
    attributed = bool(ctx.args.webkit_pid)
    if attributed:
        by_pid = {row['pid']: row for row in webkit}
        missing = [pid for pid in ctx.args.webkit_pid if pid not in by_pid]
        for pid in missing:
            ctx.finding('webkit_pid_not_webkit', str(pid))
        webkit = [by_pid[pid] for pid in ctx.args.webkit_pid if pid in by_pid]
    observed = {'state': ctx.args.state, 'window_seconds': seconds, 'interval_seconds': interval,
                'max_cpu_percent': ctx.args.max_cpu_percent, 'shell': [row['pid'] for row in shells],
                'webkit': [{'pid': row['pid'], 'path': row['path']} for row in webkit][:MAX_LISTED],
                'webkit_attribution': 'given_pids' if attributed else 'all_webkit_processes_upper_bound'}
    if len(shells) != 1:
        ctx.finding('shell_count', str(len(shells)))
    if ctx.findings:
        return 'failed', observed
    pids = [shells[0]['pid']] + [row['pid'] for row in webkit]
    try:
        start, first = ctx.hooks.clock(), ctx.hooks.cpu_times(pids)
        samples, last, ended = [], dict(first), start
        while ended - start < seconds:
            ctx.hooks.sleep(interval)
            now, current = ctx.hooks.clock(), ctx.hooks.cpu_times(pids)
            share = {pid: round(100 * (current[pid] - last[pid]) / max(now - ended, 1e-9), 3)
                     for pid in pids if pid in current and pid in last}
            samples.append({'t': round(now - start, 3), 'cpu_percent': share})
            last.update(current)
            ended = now
    except (OSError, ValueError, KeyError):
        ctx.finding('cpu_samples_unavailable')
        return 'failed', observed
    if shells[0]['pid'] not in first:
        ctx.finding('shell_not_sampled')
        return 'failed', observed
    window = max(ended - start, 1e-9)
    usage = {pid: round(100 * (last[pid] - first[pid]) / window, 3) for pid in pids if pid in first and pid in last}
    shell = usage[shells[0]['pid']]
    web = round(sum(value for pid, value in usage.items() if pid != shells[0]['pid']), 3)
    observed.update(samples=samples[:600], shell_cpu_percent=shell, webkit_cpu_percent=web,
                    total_cpu_percent=round(shell + web, 3), measured_seconds=round(window, 3))
    if shell + web <= ctx.args.max_cpu_percent:
        return 'passed', observed
    if attributed or not webkit:
        ctx.finding('cpu_over_budget', str(round(shell + web, 3)))
        return 'failed', observed
    ctx.finding('webkit_unattributed_over_budget', str(round(shell + web, 3)))
    return 'inconclusive', observed


def check_cold_start(ctx):
    reports = [item for item in ctx.evidence if item['kind'] == 'pebble.resources/1']
    if not reports:
        return 'inconclusive', {'resources_report': None}
    item = reports[-1]
    doc = item['document']
    row = ((doc.get('thresholds') or {}).get('T8_cold_start_p95_seconds') or {})
    observed = {'resources_report': {key: item[key] for key in ('path', 'sha256')}, 'kind': doc.get('kind'),
                'backend_sha256': doc.get('backend_sha256'), 'p95_seconds': row.get('measured'),
                'limit_seconds': row.get('limit'), 'met': row.get('met')}
    if doc.get('kind') != 'frozen':
        ctx.finding('resources_report_not_frozen')
    if doc.get('backend_sha256') != ctx.subject['candidate_executable_sha256']:
        ctx.finding('resources_report_other_backend')
    if row.get('met') is False:
        ctx.finding('cold_start_over_threshold', str(row.get('measured')))
    if ctx.findings:
        return 'failed', observed
    return ('passed' if row.get('met') is True else 'inconclusive'), observed


def check_export_uninstall(ctx):
    return check_processes(ctx, 'absent')


def check_webview_store(ctx):
    """D20 open check 3: names, counts and sizes in WebKit's store for this bundle identifier; never contents."""
    if not ctx.args.webview_store:
        return 'inconclusive', {'store': None}
    text = os.path.abspath(ctx.args.webview_store)
    real = os.path.realpath(text)
    if PRODUCTION_ID in (os.path.basename(text), os.path.basename(real)) and not ctx.args.installed:
        raise Refused('production_store_needs_installed', 'The com.pebble.local store is listed only with --installed.')
    if os.path.basename(real) != ctx.info['identifier']:
        raise Refused('store_identifier_mismatch', 'WebKit keeps one store per bundle identifier; name it.')
    try:
        info = os.lstat(text)
    except FileNotFoundError:
        return 'inconclusive', {'store': {'path': real, 'exists': False}}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        ctx.finding('store_not_directory')
        return 'failed', {'store': {'path': real, 'exists': True}}
    files, size, top, truncated = 0, 0, [], False
    for current, folders, names in os.walk(real):  # os.walk never follows links by default
        if current == real:
            top = sorted(folders + names)
        for name in names:
            try:
                entry = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            if stat.S_ISREG(entry.st_mode):
                files, size = files + 1, size + entry.st_size
        if files > MAX_STORE_ENTRIES:
            truncated = True
            break
    return 'passed', {'store': {'path': real, 'exists': True, 'files': files, 'bytes': size,
                                'top_level': top[:MAX_LISTED], 'truncated': truncated}}


def no_automation(ctx):
    return 'none', {}


YES = None
CHECKS = {check.name: check for check in (
    Check('sidecar', 'Sidecar, tokenizer and assets', True, False,
          ('Contents/Resources/resources equals C/stage/stage-manifest.json file for file',
           'the bundled tokenizer has the published SHA-256 and gives the exact o200k count, never the byte estimate',
           'backend/pebble-backend and llama/llama-server are present and executable'), check_sidecar),
    Check('processes', 'Readback (section 2.6): processes', True, False,
          ('exactly one shell process at the bundle\'s exact executable path',
           'exactly one service pebble-backend at the bundle\'s exact path, a child of that shell, with --home this home',
           'no Pebble executable running from another copy; with --expect absent, nothing runs from the bundle'),
          check_processes),
    Check('logs', 'Readback (section 2.6): logs', True, True,
          ('no traceback in service.log or backend.log after the --baseline offset or, without one, in a log '
           'modified after --since',), check_logs),
    Check('data', 'Readback (section 2.6): data', True, True,
          ('inspect(home, deep=True) state current, epoch ' + str(migrations.SCHEMA_EPOCH) + ', integrity ok, no '
           'refusal and every backup verified',
           'with --upgraded (the installed home after its first launch), upgrade_history at least 1 and a verified '
           'upgrade backup', 'with --expect-rows, every pre-existing table keeps its row count'),
          check_data),
    Check('banners', 'Native banners', True, True,
          ('a delivered native banner since --since and no failed banner',
           'the person saw one banner, Open opened the conversation, and after service_restart no duplicate'),
          check_banners,
          (('banner_seen', YES, 'One banner appeared for the one-minute schedule on the synthetic provider.'),
           ('open_opened_conversation', YES, 'Clicking Open showed that schedule\'s conversation.'),
           ('service_restarted', YES, 'The service was restarted (service_restart) after the banner.'),
           ('no_duplicate_after_restart', YES, 'No second banner for the same notice appeared after the restart.'))),
    Check('login_item', 'Login item', True, False,
          ('the settings route reported enabled (possibly after requires_approval) and, after toggling off, not_registered',
           'never not_found or unavailable', 'the person logged out and in and Pebble started at login'),
          check_login_item,
          (('logged_out_and_in', YES, 'With the login item enabled, the person logged out of macOS and back in.'),
           ('started_at_login', YES, 'Pebble started by itself at login.'))),
    Check('sleep_wake', 'Real sleep/wake', True, True,
          ('will_sleep, did_wake and sleep rows since --since, the sleep at least --min-sleep-seconds',
           'at most one wake summary and at most one catch-up fire per job',
           'the person slept the Mac for at least two minutes, woke it, and saw no duplicate banner'),
          check_sleep_wake,
          (('slept_two_minutes', YES, 'Apple menu > Sleep (or pmset sleepnow) for at least two minutes with a one-minute schedule.'),
           ('woke_by_person', YES, 'The person woke the Mac.'),
           ('one_catch_up', YES, 'One coalesced catch-up appeared for the missed minutes.'),
           ('no_duplicate_banner', YES, 'No banner appeared twice.'))),
    Check('timezone', 'Time-zone change', False, True,
          ('exactly one timezone_changed row since --since, with --expect-zone when given',), check_timezone),
    Check('pet_gaze', 'Pet gaze', True, False,
          ('eyes follow the pointer in all four quadrants on each display, including a second monitor',
           'no TCC prompt appears', 'the eyes stay still under reduced motion'), no_automation,
          (('main_display_quadrants', YES, 'With the pointer setting on, the eyes followed the cursor in all four '
                                          'quadrants around the pet on the main display.'),
           ('second_display_quadrants', YES, 'The same on a second display (move the pet there).'),
           ('no_tcc_prompt', YES, 'No Accessibility, Screen Recording or Input Monitoring prompt appeared.'),
           ('no_privacy_entry', YES, 'System Settings > Privacy & Security lists this bundle under none of '
                                     'Accessibility, Input Monitoring and Screen Recording (D20 open check 6).'),
           ('still_under_reduced_motion', YES, 'With Reduce motion on, the eyes did not move.'))),
    Check('webview_store', 'Webview store (D20 open check 3)', True, False,
          ('WebKit\'s store for this bundle identifier exists at --webview-store (listed read-only: names, counts, '
           'sizes)', 'drafts survive a relaunch, and losing the store loses only the pet\'s last chat and quiz drafts'),
          check_webview_store,
          (('drafts_restored_after_relaunch', YES, 'A pet chat draft and a quiz draft typed before quitting were back '
                                                   'after relaunching.'),
           ('store_moved_aside', OBSERVE, 'On the test bundle only, with the app quit, the store was moved aside '
                                          '(never deleted) and the app relaunched.'),
           ('only_drafts_lost', YES, 'With the store moved aside, chats, memories, settings and schedules were all '
                                     'there; only the webview drafts were gone. The store was then moved back.'))),
    Check('adhoc_replacement', 'Ad-hoc replacement (D20 open check 5)', True, False,
          ('a second ad-hoc build with the same identifier replaced the first and launched',
           'what macOS asked for again is recorded: Keychain, TCC, login item and notification authorization',
           'after answering, provider sign-in status reads back without a run and nothing was lost'), no_automation,
          (('replaced_and_launched', YES, 'A second ad-hoc build replaced the first and was launched.'),
           ('keychain_prompted', OBSERVE, 'A Keychain access prompt appeared on the first provider access.'),
           ('provider_status_read_back', YES, 'After any prompt, the provider sign-in status was read without starting '
                                             'a run and no account was lost.'),
           ('tcc_regrant_needed', OBSERVE, 'Accessibility or Screen Recording had to be granted again.'),
           ('login_item_reapproval_needed', OBSERVE, 'The login item needed approval again, or reported '
                                                     'requires_approval or not_found (attach the route output).'),
           ('notifications_still_authorized', OBSERVE, 'Notification authorization was still granted.'))),
    Check('webview_cpu', 'Webview CPU budget', False, False,
          ('the shell plus its WebKit processes use at most --max-cpu-percent of one core over the window, '
           'sampled after the 8-second hold',), check_webview_cpu),
    Check('frame_rate', 'Webview frame budget', False, False,
          ('at most 30 fps, 15 fps in the pet\'s low-power mode, and no frames after the 8-second hold',),
          no_automation,
          (('fps_active', 30.0, 'Peak frames per second while animating, from a devtools-enabled test bundle.'),
           ('fps_low_power', 15.0, 'Peak frames per second in the pet\'s low-power mode.'),
           ('no_frames_after_hold', YES, 'No frames were drawn after the 8-second hold while idle.'))),
    Check('cold_start', 'Cold start', False, False,
          ('a frozen measure_resources r3 report for this bundle\'s backend meets T8 (p95 within '
           + str(COLD_START_P95_SECONDS) + ' s)', 'five timed shell launches until the pet is visible, each within '
           + str(COLD_START_P95_SECONDS) + ' s (nearest-rank p95 of five is the maximum)'), check_cold_start,
          tuple((f'launch_{n}_seconds', COLD_START_P95_SECONDS, f'Seconds from open to the visible pet, launch {n}.')
                for n in range(1, 6))),
    Check('containment', 'Containment in the bundle', False, False,
          ('a reviewed async command from the test bundle re-execs pebble-backend --job-anchor, Phase 14 receipts '
           'are recorded, and nothing escapes',), no_automation,
          (('reviewed_command_ran', YES, 'A reviewed async command ran from the test bundle.'),
           ('receipts_recorded', YES, 'Its Phase 14 receipts are recorded (attach them with --evidence).'),
           ('nothing_escaped', YES, 'No process or file effect escaped its grant.'))),
    Check('export_uninstall', 'Export and uninstall', False, False,
          ('exports open, the login item is unregistered, the app is quit and nothing runs from the bundle',
           'no launch agent is left and the person was shown the Keychain items'), check_export_uninstall,
          (('memory_archive_opens', YES, 'The memory archive export opened.'),
           ('artifact_save_opens', YES, 'An artifact saved with Save opened.'),
           ('telemetry_export_opens', YES, 'The telemetry export opened.'),
           ('login_item_unregistered', YES, 'The settings route reported not_registered.'),
           ('no_launch_agent_left', YES, 'No Pebble entry in ~/Library/LaunchAgents or Login Items.'),
           ('keychain_items_shown', YES, 'The person was shown the com.pebble.* Keychain items.'))),
    Check('accessibility', 'Accessibility (E06)', False, False,
          ('VoiceOver reaches and labels every control in the main window, forms and the quiz',), no_automation,
          (('voiceover_main_window', YES, 'Every main-window control was reached and announced with a label.'),
           ('voiceover_forms', YES, 'Every form field and button was reachable and labelled.'),
           ('voiceover_quiz', YES, 'The quiz could be answered with VoiceOver alone.'))),
)}


def judge(state, check, witnessed):
    failing = sorted(key for key, bound, _ in check.checklist if key in witnessed and bound != OBSERVE
                     and (witnessed[key] == 'no' if bound is None else witnessed[key] > bound))
    missing = [key for key, _, _ in check.checklist if key not in witnessed]
    if state == 'failed' or failing:
        return 'failed', failing, missing
    if missing or state == 'inconclusive' or (state == 'none' and not check.checklist):
        return 'needs_user', failing, missing
    return 'passed', failing, missing


# ---- Report ------------------------------------------------------------------------------------


def write_new(path, report):
    """Create path exclusively; an existing file or link is never replaced."""
    data = (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    except FileExistsError:
        raise Refused('report_exists', str(path)) from None
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def read_evidence(values):
    items = []
    for value in values or ():
        data, record = file_record(value, 'evidence')
        kind, document = None, None
        try:
            document = parse_json(data, 'evidence')
            if isinstance(document, dict) and isinstance(document.get('schema'), str):
                kind = document['schema']
        except EvidenceError:
            pass
        items.append({**record, 'kind': kind, 'document': document if isinstance(document, dict) else {}})
    return items


def probe(args, hooks):
    check = CHECKS[args.check]
    now = hooks.now()
    app = app_path(args.app, args.installed)
    info = bundle_info(app)
    home = home_path(args.home, info['identifier'], args.installed) if args.home else None
    if check.needs_home and home is None:
        raise Refused('home_required', f'{check.name} needs --home.')
    report_path = new_path(args.report, 'report', (app, home))
    if args.scratch:
        if args.installed and not os.path.isabs(args.scratch):
            raise Refused('scratch_not_absolute')
        new_path(args.scratch, 'scratch', (app, home))
    elif args.installed and check.run in (check_banners, check_sleep_wake, check_timezone):
        raise Refused('scratch_required', 'A copy of the production database goes only where --scratch names.')
    witnessed = parse_witness(check, args.witness or [])
    _, snapshot = file_record(args.source_snapshot, 'source_snapshot', MAX_SNAPSHOT_BYTES)
    evidence = read_evidence(args.evidence)
    subject, summary = compute_subject(app, snapshot['sha256'])
    ctx = Context(args, app, info, home, subject, hooks, now, evidence)
    state, observed = check.run(ctx)
    if args.expect_app_sha256 and args.expect_app_sha256 != subject['app_bundle_sha256']:
        ctx.finding('app_bundle_unexpected')
        state = 'failed'
    result, failing, missing = judge(state, check, witnessed)
    return report_path, {
        'format': FORMAT, 'check': check.name, 'check_version': 1, 'd20_row': check.row,
        'repeat_after_install': check.star, 'created': now.isoformat(), 'subject': subject,
        'app': {'path': str(app), 'bundle_identifier': info['identifier'], 'executable': info['executable'],
                'version': info['version'], 'build': info['build'], **summary,
                'expected_app_sha256': args.expect_app_sha256},
        'home': None if home is None else {'path': str(home), 'installed': bool(args.installed)},
        'installed': bool(args.installed), 'source_snapshot': snapshot,
        'evidence': [{key: item[key] for key in ('path', 'sha256', 'bytes', 'kind')} for item in evidence],
        'automated': observed, 'automated_state': state,
        'witnessed': {'origin': 'person', 'entered_with': '--witness', 'entries': witnessed, 'failing': failing,
                      'note': 'Statements a person made; recorded as witnessed, never as automated observations.'},
        'checklist': [{'key': key, 'answer': 'yes or no' if bound is None else 'yes or no, recorded either way'
                       if bound == OBSERVE else f'number at most {bound}', 'prompt': prompt}
                      for key, bound, prompt in check.checklist],
        'missing_witness': missing, 'criteria': list(check.criteria), 'findings': ctx.findings, 'result': result,
        'probe_sha256': sha_bytes(Path(__file__).read_bytes()), 'limits': list(LIMITS)}


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('check', choices=list(CHECKS))
    p.add_argument('--app', required=True, help='The .app bundle to probe (never launched by this tool).')
    p.add_argument('--home', help='Its data home; its folder name must equal the bundle identifier.')
    p.add_argument('--installed', action='store_true',
                   help='Allow an installed location and the com.pebble.local home (post-install rows only).')
    p.add_argument('--source-snapshot', required=True, help='The tested source snapshot JSON bound into the subject.')
    p.add_argument('--report', required=True, help='A new report file; an existing path is refused.')
    p.add_argument('--witness', action='append', metavar='KEY=VALUE', help='A person\'s observation (repeatable).')
    p.add_argument('--evidence', action='append', metavar='FILE', help='A file to hash into the report (repeatable).')
    p.add_argument('--expect-app-sha256', help='Fail unless the bundle digest equals this (C/app-manifest.json).')
    p.add_argument('--stage-manifest', help='sidecar: C/stage/stage-manifest.json.')
    p.add_argument('--since', help='logs, banners, sleep_wake, timezone: ISO 8601 time with a zone.')
    p.add_argument('--baseline', help='logs: an earlier logs report of this home; later bytes are new.')
    p.add_argument('--expect', choices=('running', 'absent'), help='processes: running (default) or absent.')
    p.add_argument('--expect-rows', help='data: {"rows": {...}}, an inspect report or a backup record holding one.')
    p.add_argument('--upgraded', action='store_true',
                   help='data: the home was upgraded on this launch; require upgrade_history and a verified backup.')
    p.add_argument('--scratch', help='banners, sleep_wake, timezone: a new folder for the read-only database copy.')
    p.add_argument('--min-sleep-seconds', type=float, default=120.0, help='sleep_wake: shortest accepted sleep.')
    p.add_argument('--expect-zone', help='timezone: the IANA zone the person chose.')
    p.add_argument('--route-output', action='append', metavar='FILE',
                   help='login_item: {"status": ...} from login_item_status/login_item_set, in order (repeatable).')
    p.add_argument('--max-cpu-percent', type=float, help='webview_cpu: the budget, fixed before the run.')
    p.add_argument('--seconds', type=float, default=60.0, help='webview_cpu: window length (1-600).')
    p.add_argument('--interval', type=float, default=1.0, help='webview_cpu: sample interval (0.5-60).')
    p.add_argument('--state', default='idle', choices=('idle', 'hidden', 'working'), help='webview_cpu: label.')
    p.add_argument('--webkit-pid', type=int, action='append', help='webview_cpu: a WebKit pid known to be Pebble\'s.')
    p.add_argument('--webview-store', help='webview_store: WebKit\'s folder for this bundle identifier, listed read-only '
                                          '(normally ~/Library/WebKit/<identifier>).')
    return p


def main(argv=None, hooks=None):
    args = parser().parse_args(argv)
    try:
        path, report = probe(args, hooks or Hooks())
        write_new(path, report)
    except Refused as refusal:
        print(json.dumps({'check': args.check, 'refused': refusal.code, 'detail': refusal.detail}), file=sys.stderr)
        return 2
    print(json.dumps({'check': report['check'], 'result': report['result'], 'findings': len(report['findings']),
                      'missing_witness': report['missing_witness'], 'report': str(path)}))
    return EXIT[report['result']]


if __name__ == '__main__':
    sys.exit(main())
