"""Bounded settlement audit of files this run read; content-free results.

API::

    path_id(run_id, root_identity, path) -> str
    reads_from_witnesses(rows, run_id) -> list
    audit(reads, rehash, *, max_files=DEFAULT_FILES, max_bytes=DEFAULT_BYTES, root=None) -> dict
    validate(result) -> dict
    completion_issues(result) -> list
    guard_summary(result) -> dict

A read is ``{'path_id', 'root', 'hash', 'sequence'}``: an opaque path ID,
the workspace root identity at read time, the content hash the run observed
(None if the file was absent) and the host ledger order. Only the latest read
of each path ID is audited, so a later read clears an earlier stale view.

``rehash(read, remaining_bytes)`` is a parent callback that looks the path up
by its ID, under the current confinement, and returns ``(hash, size)`` for a
present file, ``None`` for an absent one or ``OVER_BUDGET`` when the file is
larger than ``remaining_bytes`` (without reading it). Any exception means the
file is unreadable. When ``root`` is given and a read's root identity differs,
the file is ``unreadable`` without calling the callback: a replaced root is
never treated as the same source.

The result holds IDs, states, counts and limits only: no paths, hashes or
content. States are ``unchanged``, ``changed``, ``deleted``, ``unreadable``
and ``skipped_budget``; the last one means "not checked", never "fresh". The
audit is evidence for the completion record, the handoff and Guard; it grants
nothing and does not promise freshness after the check.
"""

import hashlib
import json
import re

__all__ = [
    'OVER_BUDGET', 'SourceAuditError', 'STATES', 'audit', 'completion_issues',
    'guard_summary', 'path_id', 'reads_from_witnesses', 'validate',
]

VERSION = 1
STATES = ('unchanged', 'changed', 'deleted', 'unreadable', 'skipped_budget')
STALE = ('changed', 'deleted', 'unreadable')
DEFAULT_FILES = 64
DEFAULT_BYTES = 8_000_000
MAX_FILES = 4096  # As provenance.MAX_WITNESSES.
MAX_BYTES = 64_000_000
MAX_READS = 4096
ID = re.compile(r'p:[0-9a-f]{24}')
HASH = re.compile(r'(?:sha256:)?[0-9A-Fa-f]{16,128}')
OVER_BUDGET = object()


class SourceAuditError(ValueError):
    """The reads or the result are malformed. Messages never contain paths."""

    def __init__(self, reason, index=None):
        self.reason, self.index = reason, index
        where = '' if index is None else f' (read {index})'
        super().__init__(f'The source audit refused its records: {reason}{where}.')


def path_id(run_id, root_identity, path):
    """An opaque, run-scoped ID for a path under one root identity."""
    value = json.dumps([run_id, list(root_identity), path], ensure_ascii=True, separators=(',', ':'))
    return 'p:' + hashlib.sha256(value.encode('ascii')).hexdigest()[:24]


def _root(value):
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(type(v) is int for v in value):
        return tuple(value)
    raise SourceAuditError('bad_root')


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return a.removeprefix('sha256:').lower() == b.removeprefix('sha256:').lower()


def reads_from_witnesses(rows, run_id):
    """Map ``run_sources`` rows (with ``root_dev``/``root_ino``/``ordinal``) to reads.

    ``disk_hash`` is what the file held when observed: the read text for a disk
    view, the unchanged baseline for a staged draft, refreshed after an
    authoritative applied readback."""
    reads = []
    for row in rows:
        root = (row['root_dev'], row['root_ino'])
        reads.append({'path_id': path_id(run_id, root, row['path']), 'root': list(root),
                      'hash': row['disk_hash'], 'sequence': row['ordinal']})
    return reads


def _latest(reads):
    if not isinstance(reads, (list, tuple)):
        raise SourceAuditError('not_list')
    if len(reads) > MAX_READS:
        raise SourceAuditError('too_many')
    latest, sequences = {}, set()
    for index, read in enumerate(reads):
        if not isinstance(read, dict) or set(read) - {'path_id', 'root', 'hash', 'sequence'}:
            raise SourceAuditError('bad_read', index)
        ident, digest, sequence = read.get('path_id'), read.get('hash'), read.get('sequence')
        if type(ident) is not str or not ID.fullmatch(ident):
            raise SourceAuditError('bad_path_id', index)
        if digest is not None and (type(digest) is not str or not HASH.fullmatch(digest)):
            raise SourceAuditError('bad_hash', index)
        if type(sequence) is not int or sequence < 0 or sequence in sequences:
            raise SourceAuditError('bad_sequence', index)
        sequences.add(sequence)
        record = {'path_id': ident, 'root': _root(read.get('root')), 'hash': digest, 'sequence': sequence}
        if ident not in latest or latest[ident]['sequence'] < sequence:
            latest[ident] = record
    return latest


def audit(reads, rehash, *, max_files=DEFAULT_FILES, max_bytes=DEFAULT_BYTES, root=None):
    """Check each path's latest read once, newest first, within file and byte bounds."""
    if not callable(rehash):
        raise TypeError('rehash must be a callable supplied by the host.')
    if type(max_files) is not int or not 0 <= max_files <= MAX_FILES:
        raise ValueError(f'max_files must be an integer from 0 to {MAX_FILES}.')
    if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_BYTES:
        raise ValueError(f'max_bytes must be an integer from 0 to {MAX_BYTES}.')
    current_root = None if root is None else _root(root)
    latest = sorted(_latest(reads).values(), key=lambda r: -r['sequence'])
    entries, checked, used = [], 0, 0
    for read in latest:
        state = None
        if current_root is not None and read['root'] != current_root:
            state = 'unreadable'
        elif checked >= max_files or used >= max_bytes:
            state = 'skipped_budget'
        else:
            checked += 1
            try:
                observed = rehash({'path_id': read['path_id'], 'root': list(read['root'])}, max_bytes - used)
            except InterruptedError:
                raise  # Cancellation is not a file state.
            except Exception:
                observed, state = None, 'unreadable'
            if state is None:
                if observed is OVER_BUDGET:
                    state = 'skipped_budget'
                elif observed is None:
                    state = 'unchanged' if read['hash'] is None else 'deleted'
                elif (isinstance(observed, tuple) and len(observed) == 2 and type(observed[0]) is str
                      and HASH.fullmatch(observed[0]) and type(observed[1]) is int and observed[1] >= 0):
                    used += observed[1]
                    if used > max_bytes:
                        # The callback read beyond its allowance; the result is not trusted.
                        state = 'skipped_budget'
                    else:
                        state = 'unchanged' if _same(observed[0], read['hash']) else 'changed'
                else:
                    state = 'unreadable'
        entries.append({'path_id': read['path_id'], 'state': state})
    entries.sort(key=lambda e: e['path_id'])
    counts = {s: sum(e['state'] == s for e in entries) for s in STATES}
    return {'version': VERSION, 'kind': 'source_audit', 'authorization': 'none',
            'entries': entries, 'counts': counts, 'checked': checked, 'bytes_hashed': min(used, max_bytes),
            'limits': {'max_files': max_files, 'max_bytes': max_bytes},
            'complete': counts['skipped_budget'] == 0,
            'stale': [e['path_id'] for e in entries if e['state'] in STALE],
            'not_established': ['freshness_after_check', 'source_truth', 'permissions']}


def validate(result):
    """Accept only the content-free shape ``audit`` produces; return a copy."""
    if not isinstance(result, dict) or result.get('kind') != 'source_audit' or result.get('version') != VERSION:
        raise SourceAuditError('bad_result')
    entries = result.get('entries')
    if not isinstance(entries, list) or len(entries) > MAX_READS:
        raise SourceAuditError('bad_result')
    clean, seen = [], set()
    for index, entry in enumerate(entries):
        if (not isinstance(entry, dict) or set(entry) != {'path_id', 'state'} or type(entry['path_id']) is not str
                or not ID.fullmatch(entry['path_id']) or entry['state'] not in STATES or entry['path_id'] in seen):
            raise SourceAuditError('bad_entry', index)
        seen.add(entry['path_id'])
        clean.append({'path_id': entry['path_id'], 'state': entry['state']})
    counts = {s: sum(e['state'] == s for e in clean) for s in STATES}
    if result.get('counts') != counts:
        raise SourceAuditError('bad_counts')
    return {'version': VERSION, 'kind': 'source_audit', 'authorization': 'none', 'entries': clean,
            'counts': counts, 'complete': counts['skipped_budget'] == 0,
            'stale': [e['path_id'] for e in clean if e['state'] in STALE]}


def completion_issues(result):
    """Host completion-check lines; content-free, naming counts only."""
    counts = validate(result)['counts']
    issues = []
    if counts['changed'] or counts['deleted']:
        issues.append(f"{counts['changed'] + counts['deleted']} source file(s) read in this run changed or were "
                      'deleted afterwards. Read them again before relying on them or claiming completion.')
    if counts['unreadable']:
        issues.append(f"{counts['unreadable']} source file(s) read in this run can no longer be checked under the "
                      'same project root. Their earlier views cannot support completion.')
    return issues


def guard_summary(result):
    """Counts for AgentGuard and the native inspector; never paths or hashes."""
    checked = validate(result)
    counts = checked['counts']
    return {'kind': 'source_audit', 'authorization': 'none',
            'stale_sources': counts['changed'] + counts['deleted'] + counts['unreadable'],
            'unverified_sources': counts['skipped_budget'], 'complete': checked['complete'],
            'blocking': bool(counts['changed'] or counts['deleted'] or counts['unreadable']), 'counts': counts}
