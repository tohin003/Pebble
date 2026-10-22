"""Fail-closed release evidence contract and versioned readiness gates.

Only explicitly supplied roots and files are read. Local source and candidate
readiness is re-derived from bytes this module hashes itself through
descriptor-anchored, no-follow opens; a review's `passed` flags and counts are
cross-checked against the files they summarize, never trusted alone.
Production gates (Developer ID signing with notarization, installed routing,
signed updates, independent human audit, other-platform containment and
real-model quality) never pass from a JSON claim: a scripted task, a synthetic
service suite or an ad-hoc signature cannot satisfy them. Results are metadata
only (statuses, counts, digests, reason codes and safe relative paths), never
file content, check text, prompts, signing identities or credentials.
The input contract is documented in docs/HARNESS_RELEASE_GATES.md.
"""

import fnmatch
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import PurePosixPath

from .policy import PRIVATE

CONTRACT, CONTRACT_VERSION = 'pebble.release-readiness', 1
# Contract v2 (Phase 20): a review that declares readiness_contract pebble.release-readiness/2 has its
# attestation gates verified through backend/pebble/attestation.py (docs/HARNESS_TRUST_CHANNEL.md).
# Every other review keeps contract v1, byte for byte.
CONTRACT_V2_KEY, CONTRACT_V2 = 'readiness_contract', 'pebble.release-readiness/2'
PLATFORM = sys.platform
EXIT_PRODUCTION_READY, EXIT_LOCAL_READY, EXIT_NOT_READY, EXIT_MALFORMED = 0, 10, 20, 30

SOURCE_GATES = ('evidence_integrity', 'backend_tests', 'source_freshness', 'source_services')
CANDIDATE_GATES = ('candidate_bundle', 'frozen_services')
LOCAL_GATES = SOURCE_GATES + CANDIDATE_GATES
PRODUCTION_GATES = ('macos_distribution_signing', 'installed_application', 'signed_updates',
                    'independent_human_audit', 'other_platform_containment', 'real_model_quality')
# Bump a gate's version whenever its pass criteria change so stored results stay comparable.
GATE_VERSIONS = dict.fromkeys(LOCAL_GATES + PRODUCTION_GATES, 1)
# Phase 24 (V1-136): release hygiene gates, evaluated only by release_gates() so readiness results keep their shape.
RELEASE_GATES = ('third_party_notices', 'dependency_audit', 'secret_scan', 'machine_paths')
RELEASE_GATE_VERSIONS = dict.fromkeys(RELEASE_GATES, 1)
RELEASE_FORMAT, TRIAGE_FORMAT, INVENTORY_FORMAT = 'pebble.release-gates/1', 'pebble.audit-triage/1', 'pebble.release-inventory/1'
TRIAGE_VERDICTS = ('not_affected', 'fixed_in_lock', 'accepted_risk')
PASSED = {'local': 'Re-derived from supplied files that this validator hashed itself.',
          'production': 'Verified by read-only native checks that this validator ran itself.',
          'release': 'Every supplied release report was read by this validator and holds no open finding.'}

HEX64 = re.compile(r'[0-9a-f]{64}')
CONTROL = re.compile(r'[\x00-\x1f\x7f]')
SUITE = re.compile(r'[a-z0-9][a-z0-9._-]{0,119}')
LABEL = re.compile(r'[A-Za-z0-9_.-]{1,120}')
PATTERN = re.compile(r'[A-Za-z0-9_.*?-]{1,64}')
TEAM = re.compile(r'[A-Z0-9]{10}')
MAX_JSON_BYTES, MAX_JSON_DEPTH, MAX_JSON_NODES = 8_000_000, 48, 1_000_000
MAX_PATH, MAX_PARTS = 512, 48
MAX_REPORTS, MAX_CHECKS, MAX_CHECK_CHARS = 512, 4000, 2000
MAX_SOURCE_FILES, MAX_SOURCE_FILE_BYTES, MAX_SOURCE_BYTES = 40_000, 64_000_000, 2_000_000_000
MAX_BUNDLE_FILES, MAX_BUNDLE_FILE_BYTES, MAX_BUNDLE_BYTES = 50_000, 2_000_000_000, 8_000_000_000
MAX_WALK_ENTRIES, MAX_LINK_HOPS = 200_000, 40
MAX_ARTIFACTS, MAX_ARTIFACT_BYTES, MAX_LOG_BYTES = 4000, 1_000_000_000, 128_000_000
MAX_EVALUATION_REPORTS, MAX_EVALUATION_CASES = 32, 10_000
MAX_SUBJECTS, MAX_MALFORMED, CHUNK = 12, 48, 1 << 20
NATIVE_SECONDS, MAX_NATIVE_OUTPUT, MAX_NATIVE_ARGS, MAX_APP_PATH = 60, 64_000, 8, 1024

PRIVATE_NAMES = frozenset(name.casefold() for name in PRIVATE) | {
    '.docker', '.kube', 'keychains', '.git-credentials', '.pgpass', 'id_rsa', 'id_ecdsa', 'id_ed25519'}
# Key material is refused everywhere. Source snapshots also refuse .pem, but frozen
# Python bundles legitimately ship public CA bundles such as certifi/cacert.pem.
KEY_SUFFIXES = ('.key', '.p12', '.pfx', '.p8', '.keychain', '.keychain-db', '.mobileprovision', '.provisionprofile')

# The Phase 9 tested-source convention, used when a review declares no source_coverage.
DEFAULT_COVERAGE = {
    'name': 'pebble-default-v1',
    'include': (('backend', ('*.py',)), ('src', ('*.vue', '*.ts', '*.mjs', '*.css')), ('scripts', ('validate_*.py',))),
    'fixed': ('backend/requirements.txt', 'package.json', 'package-lock.json', 'src-tauri/src/main.rs',
              'src-tauri/Cargo.toml', 'src-tauri/Cargo.lock', 'src-tauri/tauri.conf.json', '.runtime/pebble-backend.spec'),
    'skip_dirs': ('__pycache__',),
}
DEFAULT_EXECUTABLE, DEFAULT_SOURCE_ENTRY = 'pebble-backend', 'backend/main.py'
BUILD_CONFIG = 'src-tauri/tauri.conf.json'
OUTCOME_KEYS = ('collected', 'passed', 'skipped', 'xfailed', 'xpassed')
FAILURE_KEYS = ('failures', 'failed_checks', 'failed', 'errors')
# Keys that would assert production authority. They are reported as ignored, never trusted.
CLAIM_KEYS = frozenset({
    'production_ready', 'release_ready', 'signed', 'notarized', 'stapled', 'developer_id', 'signature', 'signing',
    'notarization', 'human_audit', 'independent_audit', 'audit', 'audit_passed', 'security_audit', 'quality',
    'quality_passed', 'score', 'scores', 'real_model_evaluation', 'real_model_quality', 'installed',
    'installed_verified', 'other_platform', 'other_platform_verified', 'signed_updates', 'update_signature',
    'frontier_superiority_established', 'attestation', 'operator_attestation'})
# Per-arm reports written by backend/pebble/evaluation.py. Comparison reports carry no per-case outcomes.
EVALUATION_SCHEMA = 'pebble.harness_evaluation/1'
EVALUATION_EVIDENCE = {'live_model': 'live_model_recorded', 'scripted_fixture': 'scripted_fixture'}
EVALUATION_OUTCOMES = frozenset({'passed', 'failed', 'error', 'timeout', 'cancelled', 'unsupported', 'not_run',
                                 'budget_exhausted', 'solo_violation', 'non_delegated'})

TOOL_PATHS = {
    'codesign': ('/usr/bin/codesign',),
    'spctl': ('/usr/sbin/spctl',),
    # Called directly: the /usr/bin/xcrun shim can prompt to download developer tools.
    'stapler': ('/Library/Developer/CommandLineTools/usr/bin/stapler',
                '/Applications/Xcode.app/Contents/Developer/usr/bin/stapler'),
}
NATIVE_ENV = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'C', 'LC_ALL': 'C'}
INSTALLED_PREFIXES = ('/Applications/', '/System/', '/Library/', '/usr/', '/bin/', '/sbin/', '/opt/', '/private/var/db/')

UNAUTHENTICATED = {
    'installed_application': (
        'This validator never installs, launches or reads installed resources, and contract v1 cannot authenticate an installed-routing probe.',
        ('installed-application probe bound to the candidate executable, with an authenticated attestation',)),
    'signed_updates': (
        'Contract v1 has no pinned updater key or signed update artifact to verify.',
        ('pinned updater public key', 'signed update artifact and signature', 'update channel manifest')),
    'independent_human_audit': (
        'An independent human audit cannot be authenticated from repository files; JSON claims are ignored.',
        ('authenticated independent audit attestation',)),
    'other_platform_containment': (
        'Only the local macOS candidate is evaluated; other-platform containment needs evidence produced and authenticated on those platforms.',
        ('other-platform service and containment evidence with an authenticated attestation',)),
}
INTERNAL_CODES = frozenset({'review_not_passed', 'review_digest_disagrees_with_snapshot', 'service_check_total_mismatch',
                            'snapshot_digest_mismatch', 'snapshot_content_mismatch', 'snapshot_digest_unreproducible'})
REPORT_CODES = frozenset({'report_missing', 'report_digest_mismatch', 'report_check_count_mismatch', 'report_date_mismatch',
                          'report_after_review', 'report_suite_unknown', 'suite_mode_duplicated'})
ARTIFACT_CODES = frozenset({'artifact_missing', 'artifact_modified'})
MANIFEST_CODES = frozenset({'manifest_digest_mismatch', 'review_bundle_mismatch', 'review_file_count_mismatch',
                            'review_byte_count_mismatch', 'manifest_executable_missing', 'review_executable_mismatch',
                            'manifest_executable_disagrees'})
LIMITS = (
    'A passed local gate shows only that the supplied files hash to the recorded evidence at check time; it is not a reproducible-build proof that the frozen executable was compiled from the snapshot.',
    'Synthetic or scripted service suites and evaluations validate plumbing, never model quality.',
    'Production gates without an authenticated attestation channel stay unverified; JSON claims are ignored.',
    'Native signing checks cover only the explicit candidate app on this host, not installed copies, update channels or other platforms.',
    'Descriptor-anchored no-follow reads narrow, but do not eliminate, check-to-use races with a concurrent writer.',
)


class EvidenceError(ValueError):
    """Malformed or unsafe evidence that cannot be evaluated at all."""

    def __init__(self, code, subject=None):
        super().__init__(code if subject is None else f'{code}: {subject}')
        self.code, self.subject = code, subject


class UnsupportedPlatform(Exception):
    """Descriptor-anchored, no-follow file access is unavailable on this host."""


class Missing(Exception):
    """A named entry does not exist below its root."""


class Unsafe(Exception):
    """A link, special file, changed entry or exceeded bound where a regular file was required."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def redact(value):
    """A stable, content-free label for a value that must not be echoed."""
    data = value if isinstance(value, bytes) else str(value).encode('utf-8', 'surrogatepass')
    return 'sha256:' + sha256(data)[:16]


def _hex(value):
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def _count(value):
    return type(value) is int and value >= 0


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    return isinstance(value, (str, list, dict)) and len(value) > 0


def _identity(info):
    return info.st_dev, info.st_ino


def timestamp(value):
    """A timezone-aware ISO 8601 timestamp, else None."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# ---- Strict JSON ------------------------------------------------------------------------------


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise EvidenceError('duplicate_json_key')
        out[key] = value
    return out


def _non_finite(token):
    raise EvidenceError('non_finite_json_number')


def parse_json(data, what):
    """Strict bounded JSON: UTF-8 without BOM, no duplicate keys, NaN or Infinity, bounded size and depth."""
    if not isinstance(data, bytes) or len(data) > MAX_JSON_BYTES:
        raise EvidenceError('json_too_large', what)
    if data.startswith(b'\xef\xbb\xbf'):
        raise EvidenceError('json_byte_order_mark', what)
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_non_finite)
    except EvidenceError as error:
        raise EvidenceError(error.code, what) from None
    except (ValueError, RecursionError):
        raise EvidenceError('json_invalid', what) from None
    nodes, stack = 0, [(value, 1)]
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            raise EvidenceError('json_too_complex', what)
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
    return value


# ---- Paths ------------------------------------------------------------------------------------


def path_problem(value, bundle=False):
    """None for an exact, normalized, non-private relative POSIX path, else a reason code."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_PATH:
        return 'unsafe_path'
    if CONTROL.search(value) or '\\' in value or value.startswith('/') or re.match(r'[A-Za-z]:', value):
        return 'unsafe_path'
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return 'unsafe_path'
    parts = value.split('/')
    if len(parts) > MAX_PARTS or any(part in ('', '.', '..') for part in parts):
        return 'unsafe_path'
    for part in parts:
        lower = part.casefold()
        if lower in PRIVATE_NAMES or lower.startswith('.env'):
            return 'private_path'
    last = parts[-1].casefold()
    if last.endswith(KEY_SUFFIXES) or (not bundle and last.endswith('.pem')):
        return 'private_path'
    return None


def safe_relative(value, what, bundle=False):
    problem = path_problem(value, bundle)
    if problem:
        raise EvidenceError(problem, what)
    return value


def collision_key(path):
    """Paths that would collide on a case- and normalization-insensitive file system."""
    return unicodedata.normalize('NFC', path).casefold()


def subject(path):
    """A path that is safe to show, else a content-free label."""
    return path if path_problem(path, bundle=True) in (None, 'private_path') else redact(path)


def digest_map(value, what, limit, bundle=False):
    """A {safe relative path: sha256} map without colliding entries."""
    if not isinstance(value, dict) or len(value) > limit:
        raise EvidenceError('schema', what)
    seen = set()
    for path, digest in value.items():
        safe_relative(path, what, bundle)
        if not _hex(digest):
            raise EvidenceError('schema', what)
        key = collision_key(path)
        if key in seen:
            raise EvidenceError('duplicate_entry', what)
        seen.add(key)
    return value


# ---- Descriptor-anchored file access ----------------------------------------------------------

_READ = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)


def anchored_io_supported():
    return (bool(_NOFOLLOW and _DIRECTORY) and {os.open, os.stat, os.readlink} <= os.supports_dir_fd
            and os.scandir in os.supports_fd and os.stat in os.supports_follow_symlinks)


def _consume(fd, expected, limit, keep):
    if expected > limit:
        raise Unsafe('too_large')
    digest, size, chunks = hashlib.sha256(), 0, []
    while True:
        try:
            chunk = os.read(fd, CHUNK)
        except OSError:
            raise Unsafe('unreadable') from None
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise Unsafe('too_large')
        digest.update(chunk)
        if keep:
            chunks.append(chunk)
    if size != expected:
        raise Unsafe('changed_during_read')
    return digest.hexdigest(), size, b''.join(chunks)


def read_explicit(path, limit, what):
    """Read one explicitly named regular file without following a final symlink."""
    try:
        text = os.fspath(path)
        info = os.lstat(text)
    except (OSError, TypeError, ValueError):
        raise EvidenceError('input_missing', what) from None
    if stat.S_ISLNK(info.st_mode):
        raise EvidenceError('input_is_symlink', what)
    if not stat.S_ISREG(info.st_mode):
        raise EvidenceError('input_not_regular_file', what)
    try:
        fd = os.open(text, _READ | _NOFOLLOW | _NONBLOCK)
    except OSError:
        raise EvidenceError('input_unreadable', what) from None
    try:
        if _identity(os.fstat(fd)) != _identity(info):
            raise EvidenceError('input_changed', what)
        try:
            return _consume(fd, info.st_size, limit, True)[2]
        except Unsafe as error:
            raise EvidenceError('input_' + error.code, what) from None
    finally:
        os.close(fd)


class Root:
    """One explicitly supplied directory. Every entry below it is reached from this
    descriptor one component at a time with O_NOFOLLOW and an identity re-check, so
    no symlink is traversed implicitly and a swapped entry is refused."""

    def __init__(self, path, what):
        if not anchored_io_supported():
            raise UnsupportedPlatform(what)
        self.what, self.fd = what, None
        try:
            info = os.lstat(path)
        except (OSError, TypeError, ValueError):
            raise EvidenceError('root_missing', what) from None
        if stat.S_ISLNK(info.st_mode):
            raise EvidenceError('root_is_symlink', what)
        if not stat.S_ISDIR(info.st_mode):
            raise EvidenceError('root_not_directory', what)
        try:
            fd = os.open(path, _READ | _DIRECTORY | _NOFOLLOW)
        except OSError:
            raise EvidenceError('root_unreadable', what) from None
        if _identity(os.fstat(fd)) != _identity(info):
            os.close(fd)
            raise EvidenceError('root_changed', what)
        self.fd, self.device = fd, info.st_dev

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()

    @staticmethod
    def _lstat(dfd, name):
        try:
            return os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            raise Missing() from None
        except OSError:
            raise Unsafe('unreadable') from None

    def directory(self, parts, expect=None):
        """A new descriptor, which the caller closes, for the real directory at parts."""
        fd = os.dup(self.fd)
        try:
            for part in parts:
                info = self._lstat(fd, part)
                if stat.S_ISLNK(info.st_mode):
                    raise Unsafe('symlink')
                if not stat.S_ISDIR(info.st_mode):
                    raise Missing()
                if info.st_dev != self.device:
                    raise Unsafe('mount_point')
                try:
                    child = os.open(part, _READ | _DIRECTORY | _NOFOLLOW, dir_fd=fd)
                except OSError:
                    raise Unsafe('changed_during_read') from None
                os.close(fd)
                fd = child
                if _identity(os.fstat(fd)) != _identity(info):
                    raise Unsafe('changed_during_read')
            if expect is not None and _identity(os.fstat(fd)) != expect:
                raise Unsafe('changed_during_read')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def lstat(self, rel):
        parts = rel.split('/')
        dfd = self.directory(parts[:-1])
        try:
            return self._lstat(dfd, parts[-1])
        finally:
            os.close(dfd)

    def _open(self, rel):
        parts = rel.split('/')
        dfd = self.directory(parts[:-1])
        try:
            info = self._lstat(dfd, parts[-1])
            if stat.S_ISLNK(info.st_mode):
                raise Unsafe('symlink')
            if not stat.S_ISREG(info.st_mode):
                raise Unsafe('not_regular_file')
            try:
                fd = os.open(parts[-1], _READ | _NOFOLLOW | _NONBLOCK, dir_fd=dfd)
            except OSError:
                raise Unsafe('changed_during_read') from None
        finally:
            os.close(dfd)
        if _identity(os.fstat(fd)) != _identity(info):
            os.close(fd)
            raise Unsafe('changed_during_read')
        return fd, info

    def digest(self, rel, limit):
        fd, info = self._open(rel)
        try:
            return _consume(fd, info.st_size, limit, False)[:2]
        finally:
            os.close(fd)

    def read(self, rel, limit):
        fd, info = self._open(rel)
        try:
            return _consume(fd, info.st_size, limit, True)[2]
        finally:
            os.close(fd)

    def listing(self, parts=(), skip=frozenset()):
        """Every entry below parts as (relative parts, lstat). Directories are
        descended and links never are. None when parts does not exist."""
        parts = tuple(parts)
        try:
            os.close(self.directory(parts))
        except Missing:
            return None
        out, pending = [], [(parts, None)]
        while pending:
            prefix, expect = pending.pop()
            try:
                fd = self.directory(prefix, expect)
            except Missing:
                raise Unsafe('changed_during_read') from None
            try:
                try:
                    with os.scandir(fd) as found:
                        names = sorted(entry.name for entry in found)
                except OSError:
                    raise Unsafe('unreadable') from None
                for name in names:
                    try:
                        info = self._lstat(fd, name)
                    except Missing:
                        raise Unsafe('changed_during_read') from None
                    out.append((prefix + (name,), info))
                    if len(out) > MAX_WALK_ENTRIES:
                        raise Unsafe('too_many_entries')
                    if stat.S_ISDIR(info.st_mode) and name not in skip:
                        pending.append((prefix + (name,), _identity(info)))
            finally:
                os.close(fd)
        return out

    def resolve(self, parts):
        """Resolve a symlink chain strictly inside the root: relative targets only,
        never above the root, at most MAX_LINK_HOPS links. Returns the real parts
        and lstat of the final entry."""
        pending, resolved, hops = list(parts), [], 0
        while pending:
            part = pending.pop(0)
            if part in ('', '.'):
                continue
            if part == '..':
                if not resolved:
                    raise Unsafe('link_escapes_root')
                resolved.pop()
                continue
            dfd = self.directory(resolved)
            try:
                info = self._lstat(dfd, part)
                if stat.S_ISLNK(info.st_mode):
                    hops += 1
                    if hops > MAX_LINK_HOPS:
                        raise Unsafe('link_loop')
                    try:
                        target = os.readlink(part, dir_fd=dfd)
                    except OSError:
                        raise Unsafe('unreadable') from None
                    if not target or target.startswith('/'):
                        raise Unsafe('link_absolute')
                    pending[:0] = target.split('/')
                    continue
            finally:
                os.close(dfd)
            if pending and not stat.S_ISDIR(info.st_mode):
                raise Missing()
            resolved.append(part)
        if not resolved:
            raise Unsafe('link_escapes_root')
        return resolved, self.lstat('/'.join(resolved))


# ---- Candidate bundle -------------------------------------------------------------------------


def bundle_digest(entries):
    """The Phase 8/9 complete-bundle digest: SHA256 of sorted-key compact JSON entries."""
    return sha256(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode())


def _path_order(path):
    # pathlib orders paths component by component; the legacy digest depends on that order.
    return PurePosixPath(path).parts


def bundle_entries(root):
    """Legacy-compatible complete-bundle entries for a candidate root.

    Each regular file, and each relative file alias that resolves inside the root,
    becomes {path, bytes, sha256} of the bytes it names; Phase 9 recorded
    PyInstaller's Python.framework aliases this way. Directory aliases are listed
    in links but not descended, matching the Phase 9 walk. Absolute, escaping,
    dangling or looping links, special files, colliding names and private paths are
    problems; private files are never read.
    """
    entries, links, problems, digests, seen, total = [], [], [], {}, set(), 0
    for parts, info in root.listing() or ():
        rel = '/'.join(parts)
        problem = path_problem(rel, bundle=True)
        if problem:
            problems.append((problem, subject(rel)))
            continue
        key = collision_key(rel)
        if key in seen:
            problems.append(('path_collision', rel))
            continue
        seen.add(key)
        if stat.S_ISDIR(info.st_mode):
            continue
        target = rel
        if stat.S_ISLNK(info.st_mode):
            try:
                real, target_info = root.resolve(parts)
            except Missing:
                problems.append(('dangling_link', rel))
                continue
            except Unsafe as error:
                problems.append((error.code, rel))
                continue
            target = '/'.join(real)
            if stat.S_ISDIR(target_info.st_mode):
                kind = 'directory'
            elif stat.S_ISREG(target_info.st_mode):
                kind = 'file'
            else:
                problems.append(('link_to_special_file', rel))
                continue
            if path_problem(target, bundle=True):
                problems.append(('link_to_private_path', rel))
                continue
            links.append({'path': rel, 'target': target, 'kind': kind})
            if kind == 'directory':
                continue
        elif not stat.S_ISREG(info.st_mode):
            problems.append(('special_file', rel))
            continue
        if len(entries) >= MAX_BUNDLE_FILES:
            problems.append(('too_many_files', None))
            break
        if target not in digests:
            try:
                digests[target] = root.digest(target, MAX_BUNDLE_FILE_BYTES)
            except Missing:
                problems.append(('changed_during_read', rel))
                continue
            except Unsafe as error:
                problems.append((error.code, rel))
                continue
        hexdigest, size = digests[target]
        total += size
        if total > MAX_BUNDLE_BYTES:
            problems.append(('too_many_bytes', None))
            break
        entries.append({'path': rel, 'bytes': size, 'sha256': hexdigest})
    entries.sort(key=lambda entry: _path_order(entry['path']))
    links.sort(key=lambda link: _path_order(link['path']))
    return entries, links, problems


def read_manifest(data):
    """A complete candidate manifest: exact unique entries and a bundle digest."""
    manifest = parse_json(data, 'candidate_manifest')
    if (not isinstance(manifest, dict) or not isinstance(manifest.get('files'), list)
            or not 0 < len(manifest['files']) <= MAX_BUNDLE_FILES or not _hex(manifest.get('bundle_sha256'))):
        raise EvidenceError('schema', 'candidate_manifest')
    seen = set()
    for entry in manifest['files']:
        if not isinstance(entry, dict) or set(entry) != {'path', 'bytes', 'sha256'}:
            raise EvidenceError('schema', 'candidate_manifest')
        safe_relative(entry['path'], 'candidate_manifest', bundle=True)
        if not _count(entry['bytes']) or entry['bytes'] > MAX_BUNDLE_FILE_BYTES or not _hex(entry['sha256']):
            raise EvidenceError('schema', 'candidate_manifest')
        key = collision_key(entry['path'])
        if key in seen:
            raise EvidenceError('duplicate_entry', 'candidate_manifest')
        seen.add(key)
    if 'executable_sha256' in manifest and not _hex(manifest['executable_sha256']):
        raise EvidenceError('schema', 'candidate_manifest')
    links = manifest.get('links')
    if links is not None:
        if not isinstance(links, list) or len(links) > MAX_BUNDLE_FILES:
            raise EvidenceError('schema', 'candidate_manifest.links')
        for link in links:
            if (not isinstance(link, dict) or set(link) != {'path', 'target', 'kind'}
                    or link['kind'] not in ('file', 'directory')):
                raise EvidenceError('schema', 'candidate_manifest.links')
            safe_relative(link['path'], 'candidate_manifest.links', bundle=True)
            safe_relative(link['target'], 'candidate_manifest.links', bundle=True)
    return manifest


def candidate_executable(candidate, override=None):
    """The candidate executable relative to the candidate root."""
    if override is not None:
        return safe_relative(override, 'executable', bundle=True)
    if 'executable' in candidate:
        return safe_relative(candidate['executable'], 'review.candidate.executable', bundle=True)
    path = candidate.get('path')
    if isinstance(path, str) and path.strip('/'):
        # Phase 9 recorded the repository path of the executable; only its name is used.
        return safe_relative(path.rstrip('/').rsplit('/', 1)[-1], 'review.candidate.path', bundle=True)
    return DEFAULT_EXECUTABLE


# ---- Review and source coverage ---------------------------------------------------------------


def read_review(data):
    """Parse an integrated review strictly: the Phase 9 shape plus optional Phase 10 fields."""
    review = parse_json(data, 'review')
    if not isinstance(review, dict):
        raise EvidenceError('schema', 'review')

    def field(key, valid):
        if key not in review or not valid(review[key]):
            raise EvidenceError('schema', 'review.' + key)
        return review[key]

    field('date', lambda value: timestamp(value) is not None)
    field('passed', lambda value: type(value) is bool)
    outcomes = field('backend_outcomes', lambda value: isinstance(value, dict))
    if (not all(_count(outcomes.get(key)) for key in OUTCOME_KEYS)
            or not all(_count(outcomes[key]) for key in ('failed', 'errors') if key in outcomes)):
        raise EvidenceError('schema', 'review.backend_outcomes')
    ids = outcomes.get('skipped_ids', [])
    if not isinstance(ids, list) or len(ids) > 1000 or not all(isinstance(item, str) for item in ids):
        raise EvidenceError('schema', 'review.backend_outcomes')
    reports = field('service_reports', lambda value: isinstance(value, dict) and 0 < len(value) <= MAX_REPORTS)
    seen = set()
    for name, entry in reports.items():
        safe_relative(name, 'review.service_reports')
        if not name.endswith('.json'):
            raise EvidenceError('schema', 'review.service_reports')
        if collision_key(name) in seen:
            raise EvidenceError('duplicate_entry', 'review.service_reports')
        seen.add(collision_key(name))
        if (not isinstance(entry, dict) or type(entry.get('passed')) is not bool or not _count(entry.get('checks'))
                or entry.get('kind') not in ('source', 'frozen') or timestamp(entry.get('date')) is None
                or not _hex(entry.get('sha256'))
                or ('suite' in entry and not (isinstance(entry['suite'], str) and SUITE.fullmatch(entry['suite'])))):
            raise EvidenceError('schema', 'review.service_reports')
    candidate = field('candidate', lambda value: isinstance(value, dict))
    if not (_hex(candidate.get('executable_sha256')) and _hex(candidate.get('bundle_sha256'))
            and _count(candidate.get('files')) and _count(candidate.get('bytes'))):
        raise EvidenceError('schema', 'review.candidate')
    digest_map(field('source_sha256', lambda value: isinstance(value, dict) and len(value) > 0),
               'review.source_sha256', MAX_SOURCE_FILES)
    field('source_snapshot_sha256', _hex)
    if 'verification_sha256' in review:
        digest_map(review['verification_sha256'], 'review.verification_sha256', MAX_ARTIFACTS)
    for key in ('backend_tests', 'backend_tests_skipped', 'service_checks'):
        if key in review:
            field(key, _count)
    if 'source_entry' in review:
        safe_relative(review['source_entry'], 'review.source_entry')
    for key, value in review.items():
        if re.fullmatch(r'phase\d+_suite_passes', key) and not (
                isinstance(value, dict) and len(value) <= 1000
                and all(isinstance(name, str) and _count(passes) for name, passes in value.items())):
            raise EvidenceError('schema', 'review.' + key)
    if CONTRACT_V2_KEY in review and review[CONTRACT_V2_KEY] != CONTRACT_V2:
        raise EvidenceError('schema', 'review.' + CONTRACT_V2_KEY)
    return review


def read_coverage(value):
    """The review's declared source coverage, or the Phase 9 default."""
    if value is None:
        return DEFAULT_COVERAGE
    what = 'review.source_coverage'
    if not isinstance(value, dict) or set(value) - {'name', 'include', 'fixed', 'skip_dirs'}:
        raise EvidenceError('schema', what)
    include = value.get('include')
    if not isinstance(include, list) or not 0 < len(include) <= 32:
        raise EvidenceError('schema', what)
    folders = []
    for item in include:
        if not (isinstance(item, list) and len(item) == 2 and isinstance(item[1], list) and 0 < len(item[1]) <= 16
                and all(isinstance(pattern, str) and PATTERN.fullmatch(pattern) for pattern in item[1])):
            raise EvidenceError('schema', what)
        folders.append((safe_relative(item[0], what), tuple(item[1])))
    fixed = value.get('fixed', [])
    skip = value.get('skip_dirs', ['__pycache__'])
    if (not isinstance(fixed, list) or len(fixed) > 256 or not isinstance(skip, list) or len(skip) > 16
            or not all(isinstance(name, str) and LABEL.fullmatch(name) and name not in ('.', '..') for name in skip)):
        raise EvidenceError('schema', what)
    return {'name': 'declared', 'include': tuple(folders),
            'fixed': tuple(safe_relative(path, what) for path in fixed), 'skip_dirs': tuple(skip)}


def covered_sources(root, coverage):
    """Current covered inputs below the source root and any coverage problems.

    A link anywhere under a covered folder is a problem: a linked file is not the
    snapshot's bytes and a linked directory could hide inputs. Declare skip_dirs
    for directories such as virtual environments that must stay out of coverage.
    """
    found, problems = set(), []
    skip = frozenset(coverage['skip_dirs'])
    for folder, patterns in coverage['include']:
        try:
            listing = root.listing(folder.split('/'), skip)
        except Unsafe as error:
            problems.append(('source_' + error.code, folder))
            continue
        for parts, info in listing or ():
            rel = '/'.join(parts)
            if stat.S_ISDIR(info.st_mode):
                continue
            if stat.S_ISLNK(info.st_mode):
                problems.append(('source_symlink', subject(rel)))
                continue
            if not any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in patterns):
                continue
            if not stat.S_ISREG(info.st_mode):
                problems.append(('source_not_regular_file', subject(rel)))
            elif path_problem(rel):
                problems.append(('source_' + path_problem(rel), subject(rel)))
            else:
                found.add(rel)
    for rel in coverage['fixed']:
        try:
            info = root.lstat(rel)
        except Missing:
            continue
        except Unsafe as error:
            problems.append(('source_' + error.code, rel))
            continue
        if stat.S_ISLNK(info.st_mode):
            problems.append(('source_symlink', rel))
        elif stat.S_ISREG(info.st_mode):
            found.add(rel)
    return found, problems


def build_config(root, snapshot):
    """Metadata-only view of the tested Tauri build configuration; it never passes a gate."""
    if root is None or BUILD_CONFIG not in snapshot:
        return {'state': 'not_supplied'}
    try:
        data = root.read(BUILD_CONFIG, 1_000_000)
        config = parse_json(data, 'build_config')
    except (Missing, Unsafe, EvidenceError):
        return {'state': 'unreadable'}
    if sha256(data) != snapshot[BUILD_CONFIG] or not isinstance(config, dict):
        return {'state': 'stale'}
    bundle = config.get('bundle')
    macos = bundle.get('macOS') if isinstance(bundle, dict) else None
    identity = macos.get('signingIdentity') if isinstance(macos, dict) else None
    plugins = config.get('plugins')
    updater = plugins.get('updater') if isinstance(plugins, dict) else None
    return {'state': 'read',
            'configured_signing': 'adhoc' if identity == '-' else 'unset' if not identity else 'named_identity',
            'updater_configured': isinstance(updater, dict),
            'updater_public_key_present': isinstance(updater, dict) and isinstance(updater.get('pubkey'), str)
            and bool(updater['pubkey'].strip())}


# ---- Test and evaluation evidence -------------------------------------------------------------

_SUMMARY = re.compile(r'\bin \d+(?:\.\d+)?s\b')
_OUTCOME_WORDS = re.compile(r'(\d+) (passed|failed|skipped|xfailed|xpassed|errors?|deselected)\b')


def parse_test_log(data):
    """Outcome counts from a full pytest log's final summary, or None."""
    text = data.decode('utf-8', 'replace')
    summaries = [line for line in text.splitlines() if _SUMMARY.search(line) and _OUTCOME_WORDS.search(line)]
    collected = re.findall(r'\bcollected (\d+) items?\b', text)
    if not summaries or not collected:
        return None
    counts = Counter()
    for number, word in _OUTCOME_WORDS.findall(summaries[-1]):
        counts['errors' if word.startswith('error') else word] += int(number)
    return {'collected': int(collected[-1]), 'passed': counts['passed'], 'failed': counts['failed'],
            'skipped': counts['skipped'], 'xfailed': counts['xfailed'], 'xpassed': counts['xpassed'],
            'errors': counts['errors'], 'deselected': counts['deselected'],
            'failure_lines': len(re.findall(r'^(?:FAILED|ERROR) ', text, re.M))}


def evaluation_summary(doc):
    """Observe one evaluation.py per-arm report. Its evidence label, outcomes and
    ledger signature are recorded but not authenticated here, so no evaluation
    passes the quality gate in contract v1."""
    if (not isinstance(doc, dict) or doc.get('schema') != EVALUATION_SCHEMA
            or doc.get('evidence') not in EVALUATION_EVIDENCE):
        return 'unrecognized', None
    cases, summary = doc.get('cases'), doc.get('summary')
    if not isinstance(cases, list) or len(cases) > MAX_EVALUATION_CASES or not isinstance(summary, dict):
        return 'unrecognized', None
    counts = Counter()
    for case in cases:
        outcome = case.get('outcome') if isinstance(case, dict) else None
        if outcome not in EVALUATION_OUTCOMES:
            return 'unrecognized', None
        counts[outcome] += 1
    passed, recorded = counts['passed'], summary.get('outcomes')

    def agrees(key, expected, required):
        return (key not in summary and not required) or (_count(summary.get(key)) and summary[key] == expected)

    consistent = (agrees('selected', len(cases), True) and agrees('passed', passed, True)
                  and agrees('not_passed', len(cases) - passed, False)
                  and (recorded is None or (isinstance(recorded, dict) and all(_count(value) for value in recorded.values())
                                            and recorded == dict(counts))))
    return EVALUATION_EVIDENCE[doc['evidence']], {
        'cases': len(cases), 'outcomes': dict(sorted(counts.items())), 'consistent': bool(consistent),
        'ledger_signature_present': doc.get('signature') is not None}


# ---- Native signing evidence (macOS, read-only) -----------------------------------------------


def owned_app_path(value):
    """The real path of an explicit candidate .app outside installed locations."""
    try:
        text = os.fspath(value)
    except TypeError:
        raise EvidenceError('native_app_invalid') from None
    if not isinstance(text, str) or not text or len(text) > MAX_APP_PATH or CONTROL.search(text):
        raise EvidenceError('native_app_invalid')
    path = os.path.abspath(text)
    real = os.path.realpath(path)
    home = os.path.realpath(os.path.expanduser('~'))
    prefixes = INSTALLED_PREFIXES + (os.path.join(home, 'Applications') + '/',)
    if any((candidate.rstrip('/') + '/').startswith(prefixes) for candidate in (path, real)):
        raise EvidenceError('native_app_installed_location')
    if not path.endswith('.app'):
        raise EvidenceError('native_app_not_bundle')
    try:
        info = os.lstat(path)
    except OSError:
        raise EvidenceError('native_app_missing') from None
    if stat.S_ISLNK(info.st_mode):
        raise EvidenceError('native_app_is_symlink')
    if not stat.S_ISDIR(info.st_mode):
        raise EvidenceError('native_app_not_bundle')
    try:
        plist = os.lstat(os.path.join(real, 'Contents', 'Info.plist'))
    except OSError:
        raise EvidenceError('native_app_not_bundle') from None
    if not stat.S_ISREG(plist.st_mode):
        raise EvidenceError('native_app_not_bundle')
    return real


def trusted_tool(path):
    """A root-owned regular executable that only root can modify."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
            and bool(info.st_mode & 0o111))


def locate_tools():
    return {name: next((path for path in paths if trusted_tool(path)), None) for name, paths in TOOL_PATHS.items()}


def native_argv(tools, app):
    """The only command lines this module runs: verification, display, assessment
    and ticket validation. None of them signs, staples, installs or launches."""
    def argv(tool, *arguments):
        return [tools[tool], *arguments, app] if tools.get(tool) else None
    return {'verify': argv('codesign', '--verify', '--deep', '--strict', '--verbose=2'),
            'display': argv('codesign', '--display', '--verbose=4'),
            'assess': argv('spctl', '--assess', '--type', 'execute', '--verbose=4'),
            'stapled_ticket': argv('stapler', 'validate')}


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def run_bounded(argv, seconds=NATIVE_SECONDS, limit=MAX_NATIVE_OUTPUT):
    """Run one fixed read-only tool with no shell or stdin, a minimal environment,
    its own process group, a wall-clock deadline and an output cap. The output is
    returned for parsing only and is never written to a result."""
    if (not 2 <= len(argv) <= MAX_NATIVE_ARGS or not isinstance(argv[0], str) or not argv[0].startswith('/')
            or not all(isinstance(item, str) and item and '\x00' not in item and len(item) <= MAX_APP_PATH
                       for item in argv)):
        raise ValueError('Refusing an unbounded native command line.')
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=dict(NATIVE_ENV), cwd='/', close_fds=True, start_new_session=True)
    output, timed_out, truncated, code = bytearray(), False, False, None
    deadline = time.monotonic() + seconds
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if not selector.select(min(remaining, 1.0)):
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                room = limit - len(output)
                output += chunk[:room]
                if len(chunk) > room:
                    truncated = True
                    break
        if timed_out or truncated:
            _kill_group(process)
        try:
            code = process.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(process)
            process.wait(timeout=5)
    finally:
        process.stdout.close()
        if process.poll() is None:
            _kill_group(process)
            process.wait(timeout=5)
    return {'exit': None if timed_out else code, 'timed_out': timed_out, 'truncated': truncated,
            'text': output.decode('utf-8', 'replace')}


def parse_codesign_display(text, code):
    authorities, fields, flags = [], {}, set()
    for line in text.splitlines()[:500]:
        key, separator, value = line.partition('=')
        if not separator:
            continue
        key, value = key.strip(), value.strip()
        if key == 'Authority':
            authorities.append(value)
        elif key in ('Signature', 'TeamIdentifier', 'Timestamp'):
            fields[key] = value
        elif key == 'CodeDirectory v':
            match = re.search(r'flags=0x[0-9a-fA-F]+\(([^)]*)\)', value)
            if match:
                flags.update(flag.strip() for flag in match.group(1).split(','))
    first = authorities[0] if authorities else ''
    if code != 0 and not authorities and 'not signed' in text:
        kind = 'unsigned'
    elif fields.get('Signature') == 'adhoc' or 'adhoc' in flags:
        kind = 'adhoc'
    elif first.startswith('Developer ID Application:'):
        kind = 'developer_id_application'
    elif first.startswith(('Apple Development:', 'Mac Developer:')):
        kind = 'development'
    elif first.startswith(('Apple Distribution:', '3rd Party Mac Developer Application:')):
        kind = 'app_store_distribution'
    else:
        kind = 'unrecognized'
    return {'signature_kind': kind,
            'team_identifier_present': TEAM.fullmatch(fields.get('TeamIdentifier', '')) is not None,
            'hardened_runtime': 'runtime' in flags,
            'secure_timestamp': bool(fields.get('Timestamp')),
            'apple_root_chain': authorities[1:] == ['Developer ID Certification Authority', 'Apple Root CA']}


def parse_spctl(text, code):
    accepted = code == 0 and re.search(r':\s*accepted\s*$', text, re.M) is not None
    match = re.search(r'^source=(.*)$', text, re.M)
    source = match.group(1).strip() if match else ''
    kinds = {'Notarized Developer ID': 'notarized_developer_id', 'Developer ID': 'developer_id',
             'Apple System': 'apple_system', 'Mac App Store': 'mac_app_store'}
    return {'assessment': 'accepted' if accepted else 'rejected' if code is not None else 'incomplete',
            'source_kind': kinds.get(source, 'unrecognized' if source else 'none')}


def summarize_native(check, outcome):
    """Metadata from one native result; tool text, names and identities are dropped."""
    text, code = outcome.get('text') or '', outcome.get('exit')
    summary = {'exit': code if type(code) is int else None, 'timed_out': bool(outcome.get('timed_out')),
               'truncated': bool(outcome.get('truncated'))}
    if check == 'display':
        summary.update(parse_codesign_display(text, code))
    elif check == 'assess':
        summary.update(parse_spctl(text, code))
    elif check == 'stapled_ticket':
        summary['stapled'] = code == 0 and 'The validate action worked' in text
    return summary


def native_evidence(app, runner=None, tools=None):
    tools = locate_tools() if tools is None else tools
    runner = run_bounded if runner is None else runner
    checks = {}
    for check, argv in native_argv(tools, app).items():
        if argv is None:
            checks[check] = None
            continue
        try:
            checks[check] = summarize_native(check, runner(argv))
        except OSError:
            checks[check] = None
    return {'tools': {name: bool(tools.get(name)) for name in TOOL_PATHS}, 'checks': checks}


def app_embeds(app, digest, size):
    """Whether the app contains a regular file with exactly the candidate executable's bytes."""
    with Root(app, 'native_app') as root:
        try:
            listing = root.listing()
        except Unsafe:
            return None
        for parts, info in listing or ():
            if stat.S_ISREG(info.st_mode) and info.st_size == size:
                try:
                    found, _ = root.digest('/'.join(parts), MAX_BUNDLE_FILE_BYTES)
                except (Missing, Unsafe):
                    continue
                if found == digest:
                    return True
    return False


def judge_signing(gate, evidence):
    """Developer ID, hardened runtime, secure timestamp, notarized Gatekeeper source,
    stapled ticket and the validated candidate inside the app are all required."""
    checks = evidence['checks']
    gate.observed.update(tools=evidence['tools'], checks=checks, embeds_candidate=evidence.get('embeds_candidate'))
    for name in ('verify', 'display', 'assess', 'stapled_ticket'):
        result = checks.get(name)
        if result is None:
            gate.unverified('A required native tool is unavailable or untrusted.', f'trusted {name} tool')
        elif result['timed_out'] or result['truncated']:
            gate.unverified('A native check did not complete within its bounds.', f'complete {name} result')
    usable = {name: result for name, result in checks.items()
              if result is not None and not result['timed_out'] and not result['truncated']}
    if 'verify' in usable and usable['verify']['exit'] != 0:
        gate.issue('codesign_verification_failed')
    display = usable.get('display')
    if display:
        kind = display['signature_kind']
        if kind == 'adhoc':
            gate.issue('adhoc_signature')
        elif kind == 'unsigned':
            gate.issue('unsigned')
        elif kind != 'developer_id_application':
            gate.issue('not_developer_id_application')
        else:
            for key, code in (('team_identifier_present', 'team_identifier_missing'),
                              ('hardened_runtime', 'hardened_runtime_missing'),
                              ('secure_timestamp', 'secure_timestamp_missing'),
                              ('apple_root_chain', 'developer_id_chain_unrecognized')):
                if not display[key]:
                    gate.issue(code)
    assess = usable.get('assess')
    if assess:
        if assess['assessment'] != 'accepted':
            gate.issue('gatekeeper_rejected')
        elif assess['source_kind'] != 'notarized_developer_id':
            gate.issue('not_notarized')
    if 'stapled_ticket' in usable and not usable['stapled_ticket']['stapled']:
        gate.issue('ticket_not_stapled')
    embeds = evidence.get('embeds_candidate')
    if embeds is False:
        gate.issue('app_does_not_embed_candidate')
    elif embeds is None:
        gate.unverified('The app was not bound to a validated candidate executable.',
                        'validated candidate (--candidate-root and --candidate-manifest)')


# ---- Gates and assessment ---------------------------------------------------------------------


class Gate:
    """One versioned gate. It passes only with no problem, no missing input and no
    unverified reason; any contradiction fails it."""

    def __init__(self, gate_id, scope):
        self.id, self.scope = gate_id, scope
        self.problems, self.missing, self.reason, self.observed = {}, [], None, {}

    def issue(self, code, where=None):
        count, subjects = self.problems.get(code, (0, set()))
        if where is not None:
            subjects.add(where)
        self.problems[code] = (count + 1, subjects)

    def unverified(self, reason, *missing):
        self.reason = self.reason or reason
        self.missing.extend(item for item in missing if item not in self.missing)

    def render(self):
        issues = [{'code': code, 'count': count, 'subjects': sorted(subjects)[:MAX_SUBJECTS]}
                  for code, (count, subjects) in sorted(self.problems.items())]
        if issues:
            status = 'failed'
            reason = 'Evidence contradicts this gate: ' + ', '.join(item['code'] for item in issues[:6]) + '.'
        elif self.reason or self.missing:
            status, reason = 'unverified', self.reason or 'Required inputs are missing.'
        else:
            status, reason = 'passed', PASSED[self.scope]
        version = GATE_VERSIONS[self.id] if self.id in GATE_VERSIONS else RELEASE_GATE_VERSIONS[self.id]
        return {'version': version, 'scope': self.scope, 'status': status, 'reason': reason,
                'missing_inputs': sorted(self.missing), 'issues': issues, 'observed': self.observed}


def suite_of(name, kind):
    """The suite a report belongs to, from a name ending in -source.json or -frozen.json."""
    stem = name.rsplit('/', 1)[-1][:-len('.json')]
    suffix = '-' + kind
    if stem.endswith(suffix) and len(stem) > len(suffix) and SUITE.fullmatch(stem[:-len(suffix)]):
        return stem[:-len(suffix)]
    return None


class Assessment:
    """One evaluation of explicit inputs. Each step records malformed input instead of
    aborting the others, so one result carries every diagnosis."""

    def __init__(self, fixture, platform):
        self.fixture, self.platform = fixture, platform
        self.gates = {gate_id: Gate(gate_id, 'local') for gate_id in LOCAL_GATES}
        self.gates.update({gate_id: Gate(gate_id, 'production') for gate_id in PRODUCTION_GATES})
        self.malformed, self.unsupported, self.checked = set(), False, set()
        self.inputs, self.claims, self.roots = {}, set(), []
        self.source, self.review_date = None, None
        for gate_id, (reason, missing) in UNAUTHENTICATED.items():
            self.gates[gate_id].unverified(reason, *missing)

    def guard(self, step, *args):
        try:
            return step(*args)
        except EvidenceError as error:
            self.malformed.add((error.code, error.subject or ''))
        except UnsupportedPlatform:
            self.unsupported = True
        return None

    def root(self, path, what):
        root = Root(path, what)
        self.roots.append(root)
        return root

    def claims_from(self, document, value):
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if key in CLAIM_KEYS and _truthy(item):
                self.claims.add((document, key))
            if isinstance(item, dict):
                for inner, nested in item.items():
                    if inner in CLAIM_KEYS and _truthy(nested):
                        self.claims.add((document, f'{key}.{inner}' if LABEL.fullmatch(key) else inner))

    # -- review, snapshot and backend outcomes --

    def load_review(self, path):
        data = read_explicit(path, MAX_JSON_BYTES, 'review')
        self.inputs['review_sha256'] = sha256(data)
        review = read_review(data)
        self.review_date = timestamp(review['date'])
        self.claims_from('review', review)
        return review

    def check_snapshot(self, review, path):
        gate = self.gates['evidence_integrity']
        recorded = review['source_snapshot_sha256']
        if path is not None:
            data = read_explicit(path, MAX_JSON_BYTES, 'source_snapshot')
            self.inputs['source_snapshot_sha256'] = sha256(data)
            snapshot = digest_map(parse_json(data, 'source_snapshot'), 'source_snapshot', MAX_SOURCE_FILES)
            if sha256(data) != recorded:
                gate.issue('snapshot_digest_mismatch')
            if snapshot != review['source_sha256']:
                gate.issue('snapshot_content_mismatch')
        elif sha256((json.dumps(review['source_sha256'], indent=2) + '\n').encode()) != recorded:
            # Phase 9 wrote the snapshot as indented JSON; supply --source-snapshot for any other format.
            gate.issue('snapshot_digest_unreproducible')

    def check_internal(self, review):
        gate = self.gates['evidence_integrity']
        if review['passed'] is not True:
            gate.issue('review_not_passed')
        snapshot = review['source_sha256']
        maps = [(key, value) for key, value in review.items()
                if key == 'validator_sha256' or re.fullmatch(r'phase\d+_modules', key)]
        ui = review.get('ui')
        if isinstance(ui, dict) and 'source_sha256' in ui:
            maps.append(('ui.source_sha256', ui['source_sha256']))
        for label, mapping in sorted(maps, key=lambda item: item[0]):
            digest_map(mapping, 'review.' + label, MAX_SOURCE_FILES)
            if any(snapshot.get(path) != digest for path, digest in mapping.items()):
                gate.issue('review_digest_disagrees_with_snapshot', label)
        total = sum(entry['checks'] for entry in review['service_reports'].values())
        if 'service_checks' in review and review['service_checks'] != total:
            gate.issue('service_check_total_mismatch')
        self.checked.add('review')

    def load_test_log(self, path):
        data = read_explicit(path, MAX_LOG_BYTES, 'test_log')
        self.inputs['test_log_sha256'] = sha256(data)
        return data

    def check_backend_tests(self, review, log, minimum):
        gate = self.gates['backend_tests']
        outcomes = review['backend_outcomes']
        values = {key: outcomes.get(key, 0) for key in (*OUTCOME_KEYS, 'failed', 'errors')}
        collected, passed = values['collected'], values['passed']
        if collected == 0:
            gate.issue('no_tests_collected')
        if collected != sum(values[key] for key in values if key != 'collected'):
            gate.issue('outcomes_do_not_cover_collection')
        for key in ('skipped', 'xfailed', 'xpassed', 'failed', 'errors'):
            if values[key]:
                gate.issue('tests_' + key)
        ids = outcomes.get('skipped_ids', [])
        if len(ids) != min(values['skipped'], 50) and len(ids) != values['skipped']:
            gate.issue('skipped_ids_inconsistent')
        if review.get('backend_tests', passed) != passed:
            gate.issue('backend_test_total_mismatch')
        if review.get('backend_tests_skipped', values['skipped']) != values['skipped']:
            gate.issue('backend_skip_total_mismatch')
        suites = 0
        for key in sorted(review):
            if re.fullmatch(r'phase\d+_suite_passes', key):
                for name, count in sorted(review[key].items()):
                    suites += 1
                    if count == 0:
                        gate.issue('suite_without_passes', name if LABEL.fullmatch(name) else redact(name))
        if minimum is not None and passed < minimum:
            gate.issue('below_required_minimum')
        gate.observed.update(values, suites_with_pass_counts=suites, test_log='not_supplied')
        if log is None:
            return
        gate.observed['test_log'] = 'cross_checked'
        parsed = parse_test_log(log)
        if parsed is None:
            gate.issue('test_log_unparseable')
        else:
            for key in values:
                if parsed[key] != values[key]:
                    gate.issue('test_log_outcome_mismatch', key)
            if parsed['deselected']:
                gate.issue('test_log_deselected')
            if parsed['failure_lines']:
                gate.issue('test_log_lists_failures')
        if sha256(log) not in review.get('verification_sha256', {}).values():
            gate.issue('test_log_not_bound_to_review')

    # -- service reports --

    def check_reports(self, review, path, require_suites):
        root = self.root(path, 'reports_root')
        integrity = self.gates['evidence_integrity']
        suites, synthetic, unbound, checks_per_mode = {}, Counter(), [], Counter()
        for name in sorted(review['service_reports']):
            entry = review['service_reports'][name]
            kind = entry['kind']
            gate = self.gates[kind + '_services']
            try:
                data = root.read(name, MAX_JSON_BYTES)
            except Missing:
                integrity.issue('report_missing', name)
                gate.issue('report_missing', name)
                continue
            except Unsafe as error:
                raise EvidenceError('report_' + error.code, name) from None
            if sha256(data) != entry['sha256']:
                integrity.issue('report_digest_mismatch', name)
            report = parse_json(data, name)
            if not isinstance(report, dict):
                raise EvidenceError('schema', name)
            self.claims_from('service_report', report)
            checks = self.report_content(report, entry, name, gate)
            if checks is not None:
                checks_per_mode[kind] += len(checks)
            if not self.report_binding(report, kind, name, gate, review) and kind == 'source':
                unbound.append(name)
            if type(report.get('synthetic_provider_attempts')) is int:
                synthetic[kind] += 1
            suite = entry.get('suite') or suite_of(name, kind)
            if suite is None:
                integrity.issue('report_suite_unknown', name)
                continue
            modes = suites.setdefault(suite, {})
            if kind in modes:
                integrity.issue('suite_mode_duplicated', suite)
                continue
            modes[kind] = (checks, report.get('approved_source_sha256'))
        self.pair_suites(suites, require_suites)
        for kind in ('source', 'frozen'):
            self.gates[kind + '_services'].observed.update(
                reports=sum(1 for entry in review['service_reports'].values() if entry['kind'] == kind),
                checks=checks_per_mode[kind], synthetic_provider_reports=synthetic[kind], suites=len(suites))
        source = self.gates['source_services']
        source.observed['reports_without_source_binding'] = len(unbound)
        if unbound:
            # Historical source reports often record no digest; they cannot prove that the current source ran.
            source.observed['unbound_source_reports'] = unbound[:MAX_SUBJECTS]
            source.unverified('Source reports without a source_sha256 binding cannot prove that the tested source ran.',
                              'source-bound report (source_sha256 consistent with the snapshot) for every source suite')
        self.checked.add('reports')

    def report_content(self, report, entry, name, gate):
        integrity = self.gates['evidence_integrity']
        if report.get('passed') is not True or entry['passed'] is not True:
            gate.issue('report_not_passed', name)
        if report.get('kind') != entry['kind']:
            gate.issue('report_kind_mismatch', name)
        if any(_truthy(report.get(key)) for key in FAILURE_KEYS):
            gate.issue('report_lists_failures', name)
        when = timestamp(report.get('date'))
        if when is None:
            gate.issue('report_date_invalid', name)
        else:
            if report['date'] != entry['date']:
                integrity.issue('report_date_mismatch', name)
            if when > self.review_date:
                integrity.issue('report_after_review', name)
        checks = report.get('checks')
        if not (isinstance(checks, list) and 0 < len(checks) <= MAX_CHECKS
                and all(isinstance(check, str) and check.strip() and len(check) <= MAX_CHECK_CHARS for check in checks)):
            gate.issue('report_checks_invalid', name)
            return None
        if len(set(checks)) != len(checks):
            gate.issue('report_duplicate_checks', name)
        if len(checks) != entry['checks']:
            integrity.issue('report_check_count_mismatch', name)
        return checks

    @staticmethod
    def report_binding(report, kind, name, gate, review):
        """Bind a report to the candidate executable (frozen) and the tested source.
        Returns whether it carries a non-empty source_sha256 consistent with the
        snapshot; a backend digest alone binds only the entry point. A
        parent_invocation record, when present, must name this snapshot and
        candidate and an unchanged, successful run."""
        snapshot, executable = review['source_sha256'], review['candidate']['executable_sha256']
        entry_point = review.get('source_entry', DEFAULT_SOURCE_ENTRY)
        backend = report.get('backend_sha256')
        if kind == 'frozen':
            if not _hex(backend):
                gate.issue('frozen_backend_missing', name)
            elif backend != executable:
                gate.issue('frozen_backend_mismatch', name)
        elif backend is not None:
            if not _hex(backend):
                gate.issue('source_backend_invalid', name)
            elif backend == executable:
                gate.issue('source_report_ran_candidate', name)
            elif backend != snapshot.get(entry_point):
                gate.issue('source_backend_stale', name)
        sources = report.get('source_sha256')
        if sources is not None:
            digest_map(sources, name, MAX_SOURCE_FILES)
            if any(path not in snapshot for path in sources):
                gate.issue('report_source_unlisted', name)
            if any(path in snapshot and snapshot[path] != digest for path, digest in sources.items()):
                gate.issue('report_source_stale', name)
        invocation = report.get('parent_invocation')
        if invocation is not None:
            if not isinstance(invocation, dict):
                raise EvidenceError('schema', name)
            if invocation.get('kind', kind) != kind:
                gate.issue('report_invocation_kind_mismatch', name)
            if invocation.get('snapshot_sha256', review['source_snapshot_sha256']) != review['source_snapshot_sha256']:
                gate.issue('report_invocation_snapshot_stale', name)
            if invocation.get('candidate_bundle_sha256', review['candidate']['bundle_sha256']) != review['candidate']['bundle_sha256']:
                gate.issue('report_invocation_candidate_stale', name)
            if invocation.get('inputs_unchanged_before_and_after') is not True:
                gate.issue('report_inputs_changed_during_run', name)
            if invocation.get('exit_code', 0) != 0:
                gate.issue('report_invocation_failed', name)
        return bool(sources)

    def pair_suites(self, suites, require_suites):
        source, frozen = self.gates['source_services'], self.gates['frozen_services']
        for suite in sorted(suites):
            modes = suites[suite]
            if 'source' not in modes:
                source.issue('suite_missing_source_mode', suite)
            if 'frozen' not in modes:
                frozen.issue('suite_missing_frozen_mode', suite)
            if len(modes) == 2:
                (source_checks, source_approved), (frozen_checks, frozen_approved) = modes['source'], modes['frozen']
                if source_checks is not None and frozen_checks is not None and sorted(source_checks) != sorted(frozen_checks):
                    frozen.issue('suite_checks_differ_between_modes', suite)
                if (source_approved or frozen_approved) and source_approved != frozen_approved:
                    frozen.issue('suite_approved_source_differs', suite)
        for suite in sorted(set(require_suites) - set(suites)):
            source.issue('required_suite_missing', suite)
            frozen.issue('required_suite_missing', suite)

    # -- artifacts, source and candidate --

    def check_artifacts(self, review, path):
        artifacts = review.get('verification_sha256')
        if not artifacts:
            return
        gate = self.gates['evidence_integrity']
        if path is None:
            gate.unverified('The review lists verification artifacts but no artifact root was supplied.', '--artifact-root')
            return
        root = self.root(path, 'artifact_root')
        for rel in sorted(artifacts):
            try:
                digest, _ = root.digest(rel, MAX_ARTIFACT_BYTES)
            except Missing:
                gate.issue('artifact_missing', rel)
                continue
            except Unsafe as error:
                raise EvidenceError('artifact_' + error.code, rel) from None
            if digest != artifacts[rel]:
                gate.issue('artifact_modified', rel)
        gate.observed['artifacts'] = len(artifacts)
        self.checked.add('artifacts')

    def check_source(self, review, path, coverage):
        gate = self.gates['source_freshness']
        gate.observed['coverage'] = coverage['name']
        if path is None:
            gate.unverified('Current source was not supplied, so freshness against the tested snapshot is unproven.',
                            '--source-root')
            return
        root = self.source = self.root(path, 'source_root')
        snapshot, total = review['source_sha256'], 0
        for rel in sorted(snapshot):
            try:
                digest, size = root.digest(rel, MAX_SOURCE_FILE_BYTES)
            except Missing:
                gate.issue('source_missing', rel)
                continue
            except Unsafe as error:
                gate.issue('source_' + error.code, rel)
                continue
            total += size
            if total > MAX_SOURCE_BYTES:
                gate.issue('source_too_large')
                break
            if digest != snapshot[rel]:
                gate.issue('source_modified', rel)
        found, problems = covered_sources(root, coverage)
        for code, rel in problems:
            gate.issue(code, rel)
        for rel in sorted(found - set(snapshot)):
            gate.issue('source_unlisted', rel)
        gate.observed.update(files=len(snapshot), bytes=total, covered_files=len(found))
        self.checked.add('source')

    def check_candidate(self, review, root_path, manifest_path, executable):
        gate = self.gates['candidate_bundle']
        claimed = review['candidate']
        exe = candidate_executable(claimed, executable)
        manifest, listed = None, {}
        if manifest_path is None:
            gate.unverified('No complete candidate manifest was supplied.', '--candidate-manifest')
        else:
            data = read_explicit(manifest_path, MAX_JSON_BYTES, 'candidate_manifest')
            self.inputs['candidate_manifest_sha256'] = sha256(data)
            manifest = read_manifest(data)
            self.claims_from('candidate_manifest', manifest)
            files = manifest['files']
            listed = {entry['path']: entry for entry in files}
            if bundle_digest(files) != manifest['bundle_sha256']:
                gate.issue('manifest_digest_mismatch')
            if manifest['bundle_sha256'] != claimed['bundle_sha256']:
                gate.issue('review_bundle_mismatch')
            if len(files) != claimed['files']:
                gate.issue('review_file_count_mismatch')
            if sum(entry['bytes'] for entry in files) != claimed['bytes']:
                gate.issue('review_byte_count_mismatch')
            if exe not in listed:
                gate.issue('manifest_executable_missing', exe)
            elif listed[exe]['sha256'] != claimed['executable_sha256']:
                gate.issue('review_executable_mismatch', exe)
            if manifest.get('executable_sha256', claimed['executable_sha256']) != claimed['executable_sha256']:
                gate.issue('manifest_executable_disagrees')
            self.checked.add('manifest')
        if root_path is None:
            gate.unverified('No candidate root was supplied, so the bundle bytes are unproven.', '--candidate-root')
            return None
        root = self.root(root_path, 'candidate_root')
        try:
            entries, links, problems = bundle_entries(root)
        except Unsafe as error:
            gate.issue('candidate_' + error.code)
            return None
        for code, rel in problems:
            gate.issue('candidate_' + code, rel)
        actual = bundle_digest(entries)
        found = {entry['path']: entry for entry in entries}
        if manifest is not None:
            for rel in sorted(set(listed) - set(found)):
                gate.issue('candidate_file_missing', rel)
            for rel in sorted(set(found) - set(listed)):
                gate.issue('candidate_file_unlisted', rel)
            for rel in sorted(set(listed) & set(found)):
                if listed[rel] != found[rel]:
                    gate.issue('candidate_file_modified', rel)
            if actual != manifest['bundle_sha256']:
                gate.issue('candidate_bundle_differs_from_manifest')
            declared = manifest.get('links')
            if declared is not None and sorted(declared, key=lambda link: _path_order(link['path'])) != links:
                gate.issue('candidate_links_differ')
        if actual != claimed['bundle_sha256']:
            gate.issue('candidate_bundle_differs_from_review')
        gate.observed.update(files=len(entries), bytes=sum(entry['bytes'] for entry in entries), bundle_sha256=actual,
                             links=len(links), directory_links=sum(link['kind'] == 'directory' for link in links),
                             links_sha256=sha256(json.dumps(links, sort_keys=True, separators=(',', ':')).encode()))
        self.checked.add('candidate')
        entry = found.get(exe)
        if entry is None:
            gate.issue('candidate_executable_missing', exe)
            return None
        gate.observed['executable_sha256'] = entry['sha256']
        try:
            info = root.lstat(exe)
        except (Missing, Unsafe):
            gate.issue('candidate_executable_changed', exe)
            return None
        if not stat.S_ISREG(info.st_mode):
            gate.issue('candidate_executable_not_regular', exe)
        elif not info.st_mode & 0o111:
            gate.issue('candidate_executable_not_executable', exe)
        if entry['sha256'] != claimed['executable_sha256']:
            gate.issue('candidate_executable_mismatch', exe)
            return None
        return entry['sha256'], entry['bytes']

    # -- production evidence --

    def check_evaluations(self, paths):
        gate = self.gates['real_model_quality']
        if len(paths) > MAX_EVALUATION_REPORTS:
            raise EvidenceError('too_many_evaluation_reports')
        kinds, outcomes, signed, digests = Counter(), Counter(), 0, []
        for index, path in enumerate(paths):
            label = f'evaluation[{index}]'
            data = read_explicit(path, MAX_JSON_BYTES, label)
            digests.append(sha256(data))
            doc = parse_json(data, label)
            self.claims_from('evaluation_report', doc)
            kind, summary = evaluation_summary(doc)
            kinds[kind] += 1
            if summary:
                outcomes.update(summary['outcomes'])
                signed += summary['ledger_signature_present']
                if not summary['consistent']:
                    gate.issue('evaluation_counts_inconsistent', label)
        if digests:
            self.inputs['evaluation_report_sha256'] = digests
        if kinds['live_model_recorded']:
            gate.unverified('Live-model results are recorded, but contract v1 cannot authenticate the report or its ledger '
                            'signature and defines no release quality threshold.',
                            'authenticated live-model evaluation attestation', 'release quality threshold')
        elif kinds['scripted_fixture']:
            gate.unverified('Only scripted fixture evaluations were supplied; they validate plumbing, never model quality.',
                            'live-model evaluation report')
        elif paths:
            gate.unverified(f'No supplied report is a {EVALUATION_SCHEMA} per-arm report with recognized evidence and outcomes.',
                            'live-model evaluation report')
        else:
            gate.unverified('No evaluation report was supplied.', 'live-model evaluation report')
        gate.observed.update(reports=len(paths), kinds=dict(sorted(kinds.items())), cases=sum(outcomes.values()),
                             passed_cases=outcomes['passed'], outcomes=dict(sorted(outcomes.items())),
                             ledger_signatures_present_unverified=signed)

    def check_native(self, app, candidate, runner, tools):
        gate = self.gates['macos_distribution_signing']
        if app is None:
            gate.unverified('Native signing checks were not requested for an explicit candidate app.',
                            '--native-app (an owned candidate .app, checked on macOS)')
            return
        if self.platform != 'darwin':
            gate.observed['platform_supported'] = False
            gate.unverified('Native signing checks need macOS; this host is unsupported.',
                            'macOS host with codesign, spctl and stapler')
            return
        real = owned_app_path(app)
        evidence = native_evidence(real, runner=runner, tools=tools)
        evidence['embeds_candidate'] = app_embeds(real, *candidate) if candidate else None
        judge_signing(gate, evidence)

    def observe_build(self, review):
        build = build_config(self.source, review['source_sha256']) if review else {'state': 'not_supplied'}
        self.gates['macos_distribution_signing'].observed['build_config'] = {
            key: build[key] for key in ('state', 'configured_signing') if key in build}
        self.gates['signed_updates'].observed['build_config'] = {
            key: build[key] for key in ('state', 'updater_configured', 'updater_public_key_present') if key in build}

    # -- result --

    def result(self):
        gates = {}
        for gate_id in LOCAL_GATES + PRODUCTION_GATES:
            gate = self.gates[gate_id]
            if gate.scope == 'local' and not gate.problems:
                if self.malformed:
                    gate.issue('malformed_evidence')
                elif self.unsupported:
                    gate.unverified('Descriptor-anchored no-follow file access is unavailable on this host.',
                                    'supported host (macOS or Linux)')
            gates[gate_id] = gate.render()

        def passed(ids):
            return all(gates[gate_id]['status'] == 'passed' for gate_id in ids)

        def clean(gate_id, codes, step):
            return None if step not in self.checked else not any(code in self.gates[gate_id].problems for code in codes)

        local_source = not self.malformed and passed(SOURCE_GATES)
        local_candidate = local_source and passed(CANDIDATE_GATES)
        production = local_candidate and passed(PRODUCTION_GATES) and not self.fixture
        code = (EXIT_MALFORMED if self.malformed else EXIT_PRODUCTION_READY if production
                else EXIT_LOCAL_READY if local_candidate else EXIT_NOT_READY)
        candidate_problems = [code for code in self.gates['candidate_bundle'].problems if code.startswith('candidate_')]
        return {
            'contract': CONTRACT, 'contract_version': CONTRACT_VERSION, 'gate_versions': dict(GATE_VERSIONS),
            'fixture': self.fixture,
            'evidence': 'malformed' if self.malformed else 'well_formed',
            'malformed': [{'code': code, 'subject': where or None} for code, where in sorted(self.malformed)][:MAX_MALFORMED],
            'readiness': {'local_source': local_source, 'local_candidate': local_candidate, 'production': production},
            'production_ready': production,
            'exit_code': code,
            'gates': gates,
            # Historical integrity: the review agrees with the files it summarizes. It is not current-source proof.
            'history': {'review_internally_consistent': clean('evidence_integrity', INTERNAL_CODES, 'review'),
                        'reports_match_review': clean('evidence_integrity', REPORT_CODES, 'reports'),
                        'artifacts_match_review': clean('evidence_integrity', ARTIFACT_CODES, 'artifacts'),
                        'manifest_matches_review': clean('candidate_bundle', MANIFEST_CODES, 'manifest')},
            'freshness': {'source_matches_snapshot': None if 'source' not in self.checked
                          else not self.gates['source_freshness'].problems,
                          'candidate_matches_evidence': None if 'candidate' not in self.checked else not candidate_problems},
            'inputs': dict(sorted(self.inputs.items())),
            'ignored_claims': [{'document': document, 'key': key} for document, key in sorted(self.claims)],
            'host': {'platform': self.platform},
            'limits': list(LIMITS),
        }


def attestation_step(run, review, attestations, ledger, record, trust, now, app):
    """Contract v2 only: verify (gate, envelope, evidence) triples against the pinned trust roots."""
    if not (isinstance(review, dict) and review.get(CONTRACT_V2_KEY) == CONTRACT_V2):
        if attestations:
            raise EvidenceError('attestation_requires_contract_v2')
        return None
    from . import attestation  # imported here: attestation imports this module, and v1 runs never load it
    return attestation.readiness_attestations(run, review, attestations, ledger, record, trust, now, app)


def check_release(*, review, reports_root, source_root=None, source_snapshot=None, candidate_root=None,
                  candidate_manifest=None, artifact_root=None, test_log=None, evaluation_reports=(),
                  native_app=None, executable=None, require_suites=(), min_backend_tests=None,
                  signing_identity_status='unknown', fixture=False, platform=None, runner=None, tools=None,
                  attestations=(), attestation_ledger=None, record_attestations=False, trust=None, now=None):
    """Evaluate explicit release evidence; return the deterministic, metadata-only result.

    Only the named files and roots are read. Native tools run only when native_app
    names an owned candidate .app on macOS. signing_identity_status is a
    parent-obtained summary that is recorded and never used to pass a gate.

    A review that declares readiness_contract pebble.release-readiness/2 also has each
    (gate, envelope path, evidence path) in attestations verified through
    backend/pebble/attestation.py against the pinned trust roots; trust and now are for
    tests only. Every other review keeps contract v1.
    """
    if signing_identity_status not in ('available', 'absent', 'unknown'):
        raise ValueError('signing_identity_status must be available, absent or unknown.')
    if min_backend_tests is not None and not _count(min_backend_tests):
        raise ValueError('min_backend_tests must be a non-negative integer.')
    run = Assessment(bool(fixture), PLATFORM if platform is None else platform)
    run.gates['macos_distribution_signing'].observed['operator_reported_identity'] = signing_identity_status
    review_doc = outcome = None
    try:
        for suite in require_suites:
            if not (isinstance(suite, str) and SUITE.fullmatch(suite)):
                raise EvidenceError('required_suite_invalid')
        review_doc = run.guard(run.load_review, review)
        candidate = None
        if review_doc is not None:
            coverage = run.guard(read_coverage, review_doc.get('source_coverage'))
            run.guard(run.check_snapshot, review_doc, source_snapshot)
            run.guard(run.check_internal, review_doc)
            log = run.guard(run.load_test_log, test_log) if test_log is not None else None
            run.guard(run.check_backend_tests, review_doc, log, min_backend_tests)
            run.guard(run.check_reports, review_doc, reports_root, tuple(require_suites))
            run.guard(run.check_artifacts, review_doc, artifact_root)
            if coverage is not None:
                run.guard(run.check_source, review_doc, source_root, coverage)
            candidate = run.guard(run.check_candidate, review_doc, candidate_root, candidate_manifest, executable)
        run.guard(run.check_evaluations, list(evaluation_reports))
        run.guard(run.check_native, native_app, candidate, runner, tools)
        run.guard(run.observe_build, review_doc)
        outcome = run.guard(attestation_step, run, review_doc, list(attestations), attestation_ledger,
                            record_attestations, trust, now, native_app)
    except EvidenceError as error:
        run.malformed.add((error.code, error.subject or ''))
    finally:
        for root in run.roots:
            root.close()
    result = run.result()
    if review_doc is not None and review_doc.get(CONTRACT_V2_KEY) == CONTRACT_V2:
        from . import attestation
        result = attestation.apply_contract_v2(result, outcome)
    return result


# ---- Release hygiene gates (Phase 24, V1-136) ---------------------------------------------------


def _release_json(gate, path, what):
    """A supplied report, parsed; a missing or malformed one fails the gate (never a pass)."""
    try:
        return parse_json(read_explicit(path, MAX_JSON_BYTES, what), what)
    except EvidenceError as error:
        gate.issue(what + '_unreadable', error.code)
        return None


def _audit_findings(doc):
    """[(package, version, id, aliases)] from pip-audit's JSON (-f json) or osv-scanner's (--format json)."""
    found = []
    if isinstance(doc, dict) and isinstance(doc.get('dependencies'), list):  # pip-audit
        for dep in doc['dependencies']:
            for vuln in (dep.get('vulns') or []) if isinstance(dep, dict) else ():
                if isinstance(vuln, dict) and isinstance(vuln.get('id'), str):
                    found.append((str(dep.get('name')), str(dep.get('version')), vuln['id'],
                                  tuple(a for a in vuln.get('aliases') or () if isinstance(a, str))))
        return found
    if isinstance(doc, dict) and isinstance(doc.get('results'), list):  # osv-scanner
        for result in doc['results']:
            for item in (result.get('packages') or []) if isinstance(result, dict) else ():
                if not isinstance(item, dict):
                    continue
                package = item.get('package') if isinstance(item.get('package'), dict) else {}
                for vuln in item.get('vulnerabilities') or ():
                    if isinstance(vuln, dict) and isinstance(vuln.get('id'), str):
                        found.append((str(package.get('name')), str(package.get('version')), vuln['id'],
                                      tuple(a for a in vuln.get('aliases') or () if isinstance(a, str))))
        return found
    return None


def release_gates(*, inventory_report=None, lock=None, audit_reports=(), audit_triage=None, gitleaks_report=None,
                  stage_manifest=None, app_manifest=None):
    """The four release hygiene gates over explicit reports; content-free like every readiness result.

    - third_party_notices: scripts/release_inventory.py's report is 'complete' with no finding, and its notices file
      was generated from `lock` (V1-046).
    - dependency_audit: every vulnerability that pip-audit or osv-scanner reports for the lock has an entry in the
      triage file (pebble.audit-triage/1: lock_sha256 and {id: {verdict, reason}}) written for this very lock.
    - secret_scan: gitleaks' JSON report over the staged tree lists nothing (its allowlist is reviewed separately).
    - machine_paths: the candidate build's stage manifest and app manifest record a scan with no path of the build
      machine in any shipped binary (V1-023).

    A gate whose input is not supplied is unverified; the result passes only when all four pass."""
    gates = {gate_id: Gate(gate_id, 'release') for gate_id in RELEASE_GATES}
    lock_sha = None
    if lock is not None:
        try:
            lock_sha = sha256(read_explicit(lock, MAX_JSON_BYTES, 'lock'))
        except EvidenceError as error:
            for gate_id in ('third_party_notices', 'dependency_audit'):
                gates[gate_id].issue('lock_unreadable', error.code)

    gate = gates['third_party_notices']
    if inventory_report is None:
        gate.unverified('No release inventory report was supplied.', 'scripts/release_inventory.py report')
    else:
        doc = _release_json(gate, inventory_report, 'inventory')
        if doc is not None:
            if not isinstance(doc, dict) or doc.get('format') != INVENTORY_FORMAT:
                gate.issue('inventory_format')
            else:
                findings = doc.get('findings')
                if doc.get('result') != 'complete' or not isinstance(findings, list) or findings:
                    gate.issue('inventory_not_complete')
                    for finding in (findings if isinstance(findings, list) else [])[:MAX_SUBJECTS * 4]:
                        if isinstance(finding, dict) and isinstance(finding.get('code'), str):
                            gate.issue('inventory_' + finding['code'])
                notices = ((doc.get('inputs') or {}).get('third_party_notices') or {})
                recorded = (notices.get('inputs') or {}).get('requirements.lock') if isinstance(notices, dict) else None
                if lock_sha is not None and recorded != lock_sha:
                    gate.issue('notices_not_from_this_lock')
                summary = doc.get('summary') if isinstance(doc.get('summary'), dict) else {}
                gate.observed.update(components=summary.get('components'),
                                     by_ecosystem={k: v.get('components') for k, v in (summary.get('by_ecosystem') or {}).items()
                                                   if isinstance(v, dict)})

    gate = gates['dependency_audit']
    if not audit_reports:
        gate.unverified('No pip-audit or osv-scanner report of the lock was supplied.', 'audit report of the lock')
    if audit_triage is None:
        gate.unverified('No audit triage file was supplied.', 'audit triage (' + TRIAGE_FORMAT + ')')
    findings = []
    for path in audit_reports:
        doc = _release_json(gate, path, 'audit')
        found = _audit_findings(doc) if doc is not None else []
        if found is None:
            gate.issue('audit_format')
        else:
            findings += found
    triage = _release_json(gate, audit_triage, 'triage') if audit_triage is not None else None
    entries = {}
    if triage is not None:
        if not isinstance(triage, dict) or triage.get('format') != TRIAGE_FORMAT or not isinstance(triage.get('entries'), dict):
            gate.issue('triage_format')
        else:
            entries = triage['entries']
            if lock_sha is not None and triage.get('lock_sha256') != lock_sha:
                gate.issue('triage_for_another_lock')
    verdicts = Counter()
    for package, version, ident, aliases in findings:
        entry = next((entries[key] for key in (ident, *aliases) if isinstance(entries.get(key), dict)), None)
        if (entry is None or entry.get('verdict') not in TRIAGE_VERDICTS or not isinstance(entry.get('reason'), str)
                or not entry['reason'].strip()):
            gate.issue('untriaged_vulnerability', f'{package}@{version}:{ident}')
        else:
            verdicts[entry['verdict']] += 1
    gate.observed.update(reports=len(audit_reports), vulnerabilities=len(findings), triaged=dict(sorted(verdicts.items())))

    gate = gates['secret_scan']
    if gitleaks_report is None:
        gate.unverified('No gitleaks report of the staged tree was supplied.', 'gitleaks JSON report')
    else:
        doc = _release_json(gate, gitleaks_report, 'gitleaks')
        if doc is not None and not isinstance(doc, list):
            gate.issue('gitleaks_format')
        for leak in doc if isinstance(doc, list) else ():
            # Rule and file name only: a secret, its match, its line or a full path never enter the result.
            gate.issue('secret_found', '%s:%s' % (leak.get('RuleID'), os.path.basename(str(leak.get('File'))))
                       if isinstance(leak, dict) else None)
        gate.observed['findings'] = len(doc) if isinstance(doc, list) else None

    gate = gates['machine_paths']
    if stage_manifest is None or app_manifest is None:
        gate.unverified('The candidate build\'s stage and app manifests were not both supplied.',
                        'C/stage/stage-manifest.json', 'C/app-manifest.json')
    else:
        scans = []
        stage = _release_json(gate, stage_manifest, 'stage_manifest')
        if stage is not None:
            scans.append(('stage', (stage.get('machine_paths') if isinstance(stage, dict) else None)))
        app = _release_json(gate, app_manifest, 'app_manifest')
        if app is not None:
            apps = app.get('apps') if isinstance(app, dict) else None
            for kind, bundle in sorted(apps.items()) if isinstance(apps, dict) else ():
                scans.append((kind, bundle.get('shell_machine_paths') if isinstance(bundle, dict) else None))
            if not isinstance(apps, dict) or not apps:
                gate.issue('machine_scan_missing', 'apps')
        for where, scan in scans:
            if not isinstance(scan, dict) or not _count(scan.get('files_scanned')) or not scan['files_scanned']:
                gate.issue('machine_scan_missing', where)
            elif scan.get('machine_specific') != 0:
                gate.issue('machine_paths_present', where)
        gate.observed['scans'] = {where: {key: scan.get(key) for key in ('files_scanned', 'machine_specific', 'users_prefix')}
                                  for where, scan in scans if isinstance(scan, dict)}

    rendered = {gate_id: gates[gate_id].render() for gate_id in RELEASE_GATES}
    return {'format': RELEASE_FORMAT, 'gates': rendered,
            'passed': all(item['status'] == 'passed' for item in rendered.values())}


# ---- Output -----------------------------------------------------------------------------------


def summary(result):
    """The short console view of a result."""
    return {'exit_code': result['exit_code'], 'evidence': result['evidence'], 'fixture': result['fixture'],
            'readiness': result['readiness'], 'production_ready': result['production_ready'],
            'gates': {gate_id: gate['status'] for gate_id, gate in result['gates'].items()},
            'malformed': result['malformed']}


def write_report(path, result, protected_trees=()):
    """Write a result atomically. It never follows a final symlink, never writes
    inside a protected tree (candidate or app) and only replaces an earlier
    readiness result, so an evidence input can never be overwritten."""
    target = os.path.abspath(os.fspath(path))
    parent, name = os.path.split(target)
    if not name.endswith('.json') or name.startswith(('harness-phase9-', '.')):
        raise EvidenceError('report_target_refused')
    # Parent directories may be system aliases such as /tmp; the target itself is never followed.
    if not os.path.isdir(parent):
        raise EvidenceError('report_target_refused')
    real_parent = os.path.realpath(parent)
    for tree in protected_trees:
        if tree is not None:
            protected = os.path.realpath(os.fspath(tree))
            if os.path.commonpath([real_parent, protected]) == protected:
                raise EvidenceError('report_target_refused')
    if os.path.lexists(target):
        try:
            previous = parse_json(read_explicit(target, MAX_JSON_BYTES, 'report_target'), 'report_target')
        except EvidenceError:
            raise EvidenceError('report_target_refused') from None
        if not isinstance(previous, dict) or previous.get('contract') != CONTRACT:
            raise EvidenceError('report_target_refused')
    data = (json.dumps(result, indent=2, sort_keys=True) + '\n').encode()
    handle, temporary = tempfile.mkstemp(prefix='.' + name + '.', suffix='.tmp', dir=real_parent)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, os.path.join(real_parent, name))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    try:
        directory = os.open(real_parent, _READ | _DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        pass


# ---- Synthetic fixture ------------------------------------------------------------------------

FIXTURE_DATE, FIXTURE_REPORT_DATE = '2026-01-01T12:00:00+00:00', '2026-01-01T11:00:00+00:00'


def legacy_entries(package):
    """The Phase 9 finalizer's bundle walk (regular files plus followed file links,
    directory links not descended), reproduced without this module's walker."""
    found = []
    for folder, directories, files in os.walk(package):
        for name in directories + files:
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                found.append(os.path.relpath(path, package).replace(os.sep, '/'))
    entries = []
    for rel in sorted(found, key=_path_order):
        with open(os.path.join(package, rel), 'rb') as stream:
            data = stream.read()
        entries.append({'path': rel, 'bytes': len(data), 'sha256': sha256(data)})
    return entries


def build_fixture(base):
    """Write a small synthetic evidence tree below base (a new, private directory) and
    return the explicit arguments that validate it. Nothing is real: the
    'executable' is inert bytes and every signing, audit and quality claim is fake,
    so production readiness must stay false."""
    base = os.fspath(base)

    def write(rel, data, mode=0o644):
        path = os.path.join(base, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as stream:
            stream.write(data)
        os.chmod(path, mode)
        return path

    def dump(value):
        return (json.dumps(value, indent=2) + '\n').encode()

    sources = {'backend/main.py': b'from pebble import core\n', 'backend/pebble/__init__.py': b'"""Fixture."""\n',
               'backend/pebble/core.py': b'VALUE = 42\n', 'backend/requirements.txt': b'synthetic==0\n',
               'scripts/validate_fixture.py': b'print("synthetic")\n', 'scripts/fixture_tool.py': b'print("tool")\n',
               'src/App.vue': b'<template><main /></template>\n'}
    for rel, data in sources.items():
        write('source/' + rel, data)
    snapshot = {rel: sha256(data) for rel, data in sorted(sources.items())}
    snapshot_sha256 = sha256(dump(snapshot))
    executable = b'\xcf\xfa\xed\xfe' + bytes(range(256)) * 16
    write('candidate/pebble-backend', executable, 0o755)
    write('candidate/_internal/base_library.zip', bytes(reversed(range(256))) * 32)
    write('candidate/_internal/data/catalogue.json', b'{"synthetic": true}\n')
    write('candidate/_internal/certifi/cacert.pem', b'-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----\n')
    write('candidate/_internal/Fixture.framework/Versions/1/Fixture', b'\x00\x01synthetic library\x00' * 64)
    framework = os.path.join(base, 'candidate', '_internal', 'Fixture.framework')
    try:
        os.symlink('1', os.path.join(framework, 'Versions', 'Current'))
        os.symlink('Versions/Current/Fixture', os.path.join(framework, 'Fixture'))
        os.symlink('Fixture.framework/Versions/1/Fixture', os.path.join(base, 'candidate', '_internal', 'Fixture'))
    except (AttributeError, NotImplementedError, OSError):
        pass  # Aliases are optional; the remaining tree is still complete evidence.
    entries = legacy_entries(os.path.join(base, 'candidate'))
    bundle = bundle_digest(entries)
    write('manifest.json', dump({'bundle_sha256': bundle, 'algorithm': 'SHA256 of sorted-key compact JSON file entries in relative-path order',
                                 'files': entries}))
    checks = ['synthetic plumbing check one', 'synthetic plumbing check two']
    reports = {}
    for kind in ('source', 'frozen'):
        # Shaped like the parent's source-bound reports: the complete snapshot plus an invocation record.
        report = {'date': FIXTURE_REPORT_DATE, 'kind': kind, 'passed': True, 'checks': checks, 'synthetic_provider_attempts': 1,
                  'source_sha256': snapshot, 'backend_sha256': sha256(executable) if kind == 'frozen' else None,
                  'parent_invocation': {'schema': 1, 'suite': 'fixture-suite', 'kind': kind, 'snapshot_sha256': snapshot_sha256,
                                        'candidate_bundle_sha256': bundle, 'exit_code': 0,
                                        'inputs_unchanged_before_and_after': True},
                  'limits': 'Synthetic fixture only; no real model, signing or installed proof.'}
        data = dump(report)
        write(f'reports/fixture-suite-{kind}.json', data)
        reports[f'fixture-suite-{kind}.json'] = {'passed': True, 'checks': len(checks), 'kind': kind,
                                                 'date': FIXTURE_REPORT_DATE, 'sha256': sha256(data)}
    log = (b'collecting ... collected 3 items\n\n'
           + b''.join(b'backend/tests/test_fixture.py::test_case_%d PASSED [%3d%%]\n' % (n, 33 * (n + 1)) for n in range(3))
           + b'\n============================== 3 passed in 0.01s ==============================\n')
    write('logs/tests.log', log)
    review = {
        'date': FIXTURE_DATE, 'scope': 'Synthetic release-readiness fixture; nothing here is real.', 'passed': True,
        'backend_tests': 3, 'backend_tests_skipped': 0,
        'backend_outcomes': {'collected': 3, 'passed': 3, 'skipped': 0, 'xfailed': 0, 'xpassed': 0, 'skipped_ids': []},
        'phase0_suite_passes': {'test_fixture': 3},
        'service_checks': 2 * len(checks), 'service_reports': reports,
        'candidate': {'path': 'candidate/pebble-backend', 'executable_sha256': sha256(executable), 'bundle_sha256': bundle,
                      'files': len(entries), 'bytes': sum(entry['bytes'] for entry in entries)},
        'source_sha256': snapshot, 'source_snapshot_sha256': snapshot_sha256,
        'source_coverage': {'include': [['backend', ['*.py']], ['src', ['*.vue', '*.ts', '*.mjs', '*.css']], ['scripts', ['*.py']]],
                            'fixed': list(DEFAULT_COVERAGE['fixed']), 'skip_dirs': ['__pycache__']},
        'verification_sha256': {'logs/tests.log': sha256(log)},
        # Deliberately fake production claims: the validator must ignore every one.
        'signed': True, 'notarized': True, 'production_ready': True, 'real_model_evaluation': True, 'score': 1.0,
        'human_audit': {'passed': True},
    }
    write('review.json', dump(review))
    write('evaluation.json', dump({'schema': EVALUATION_SCHEMA, 'evidence': 'scripted_fixture', 'arm': 'solo',
                                   'cases': [{'case_id': 'synthetic-1', 'outcome': 'passed', 'success': True}],
                                   'summary': {'selected': 1, 'passed': 1, 'not_passed': 0, 'outcomes': {'passed': 1}},
                                   'score': 1.0, 'signature': None}))
    join = lambda *parts: os.path.join(base, *parts)  # noqa: E731
    return {'review': join('review.json'), 'reports_root': join('reports'), 'source_root': join('source'),
            'candidate_root': join('candidate'), 'candidate_manifest': join('manifest.json'), 'artifact_root': base,
            'test_log': join('logs', 'tests.log'), 'evaluation_reports': [join('evaluation.json')]}


def run_fixture():
    """Validate a freshly generated synthetic evidence tree in a private temporary
    directory, which is removed afterwards. Expected: local gates pass, production
    gates stay unverified and production_ready is false."""
    with tempfile.TemporaryDirectory(prefix='pebble-release-fixture-') as scratch:
        return check_release(**build_fixture(scratch), fixture=True)
