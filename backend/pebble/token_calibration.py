"""Calibrate local input-token estimates against provider-reported counts.

API::

    route_fingerprint(route) -> str
    Calibration(load, append).record(route, estimated, reported) -> dict
    Calibration(load, append).multiplier(route) -> float
    Calibration(load, append).apply(route, estimate) -> int
    calibrated_counter(counter, calibration, route) -> callable
    schema_cost(tools) -> int
    profile_tools(names, tools=None) -> list

Samples are ``(estimated, reported)`` pairs per route fingerprint, where
``reported`` is the provider's input-token usage for the exact request whose
local estimate was ``estimated``. Unknown (zero or missing) usage is not a
sample. Below ``MIN_SAMPLES`` the multiplier is exactly 1.0; from then on it
is the nearest-rank p95 of ``reported / estimated`` over the newest
``WINDOW`` samples, clamped to [1.0, 2.0]. A multiplier only ever enlarges
estimates, so calibration cannot admit more context than the local estimate.

Storage is a parent callback pair: ``load(route) -> [(estimated, reported)]``
oldest first, and ``append(route, estimated, reported)``. ``DDL`` and the
statements below are the proposed epoch-6 table. ``MemoryStore`` serves tests.
Nothing here reads files, the network or a clock.
"""

import hashlib
import json
import math
import re

from .context_manifest import BASE_TOKENS, estimate_tokens

__all__ = [
    'Calibration', 'DDL', 'MAX_MULTIPLIER', 'MIN_MULTIPLIER', 'MIN_SAMPLES', 'MemoryStore',
    'PROFILE_4K_CODING', 'calibrated_counter', 'profile_tools', 'route_fingerprint', 'schema_cost',
]

MIN_SAMPLES = 20
WINDOW = 200
PERCENTILE = 0.95
MIN_MULTIPLIER, MAX_MULTIPLIER = 1.0, 2.0
MAX_TOKENS = 10_000_000  # As context_manifest.MAX_CONTEXT.
ROUTE = re.compile(r'sha256:[0-9a-f]{64}')

DDL = '''
CREATE TABLE IF NOT EXISTS token_calibration(
  route TEXT NOT NULL CHECK(length(route)=71),
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  estimated INTEGER NOT NULL CHECK(estimated BETWEEN 1 AND 10000000),
  reported INTEGER NOT NULL CHECK(reported BETWEEN 1 AND 10000000),
  created REAL NOT NULL,
  PRIMARY KEY(route, sequence));
'''
INSERT_SQL = ('INSERT INTO token_calibration VALUES(?,'
              '(SELECT coalesce(max(sequence),0)+1 FROM token_calibration WHERE route=?),?,?,?)')
SELECT_SQL = ('SELECT estimated,reported FROM (SELECT estimated,reported,sequence FROM token_calibration '
              'WHERE route=? ORDER BY sequence DESC LIMIT 200) ORDER BY sequence')
PRUNE_SQL = ('DELETE FROM token_calibration WHERE route=? AND sequence<='
             '(SELECT coalesce(max(sequence),0)-200 FROM token_calibration WHERE route=?)')

# Schemas the runner offers a 4K coding request in a selected, writable
# workspace that mentions tests (runner.py tool selection, budget <= 4096):
# core tools without agents or memory, the coding set and recipe listing.
PROFILE_4K_CODING = ('AskUserQuestion', 'UpdatePlan', 'ReadRunEvidence', 'Glob', 'Read', 'Grep', 'SearchFiles',
                     'RunTests', 'Write', 'Append', 'Edit', 'RunCommand', 'ListProjectRecipes')


def route_fingerprint(route):
    """Tokenizer-relevant route identity: kind, endpoint and model, never credentials."""
    if not isinstance(route, dict):
        raise ValueError('A route must be a profile mapping.')
    identity = {k: route.get(k) for k in ('kind', 'url', 'model', 'path')}
    if isinstance(identity['url'], str):
        identity['url'] = re.sub(r'//[^/@]*@', '//', identity['url'])  # Drop embedded user information.
    text = json.dumps(identity, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    return 'sha256:' + hashlib.sha256(text.encode('ascii')).hexdigest()


def _route(value):
    if type(value) is not str or not ROUTE.fullmatch(value):
        raise ValueError('Calibration needs a route fingerprint.')
    return value


def _count(value, low=1):
    return type(value) is int and low <= value <= MAX_TOKENS


def _p95(ratios):
    ordered = sorted(ratios)
    return ordered[max(0, math.ceil(PERCENTILE * len(ordered)) - 1)]


class MemoryStore:
    """An in-memory stand-in for the parent's SQLite callbacks."""

    def __init__(self):
        self.rows = {}

    def load(self, route):
        return list(self.rows.get(route, []))[-WINDOW:]

    def append(self, route, estimated, reported):
        self.rows.setdefault(route, []).append((estimated, reported))


class Calibration:
    def __init__(self, load, append):
        if not callable(load) or not callable(append):
            raise TypeError('Calibration storage needs load and append callbacks.')
        self.load, self.append = load, append

    def samples(self, route):
        rows = list(self.load(_route(route)))[-WINDOW:]
        if not all(isinstance(r, (list, tuple)) and len(r) == 2 and _count(r[0]) and _count(r[1]) for r in rows):
            raise ValueError('Stored calibration samples are malformed.')
        return [(int(e), int(r)) for e, r in rows]

    def record(self, route, estimated, reported):
        """Store one sample; unknown provider usage is skipped, not guessed."""
        route = _route(route)
        if not _count(estimated):
            raise ValueError('The local estimate must be a positive token count.')
        if reported is None or reported == 0:
            return {'status': 'skipped', 'reason': 'usage_unknown'}
        if not _count(reported):
            raise ValueError('Provider input usage must be a positive token count.')
        self.append(route, estimated, reported)
        return {'status': 'recorded', **self.describe(route)}

    def multiplier(self, route):
        rows = self.samples(route)
        if len(rows) < MIN_SAMPLES:
            return 1.0
        return min(MAX_MULTIPLIER, max(MIN_MULTIPLIER, _p95([r / e for e, r in rows])))

    def apply(self, route, estimate):
        if not _count(estimate, 0):
            raise ValueError('An estimate must be a non-negative token count.')
        return math.ceil(estimate * self.multiplier(route))

    def describe(self, route):
        """Content-free state for manifests and the native inspector."""
        rows = self.samples(route)
        raw = _p95([r / e for e, r in rows]) if len(rows) >= MIN_SAMPLES else None
        return {'samples': len(rows), 'min_samples': MIN_SAMPLES, 'window': WINDOW,
                'percentile': PERCENTILE, 'raw_ratio': None if raw is None else round(raw, 6),
                'multiplier': self.multiplier(route), 'clamp': [MIN_MULTIPLIER, MAX_MULTIPLIER],
                'active': len(rows) >= MIN_SAMPLES}


def calibrated_counter(counter, calibration, route):
    """Wrap an assemble_context counter so its window counts are calibrated."""
    if not callable(counter):
        raise TypeError('The token counter must be callable.')
    factor = calibration.multiplier(route)

    def count(messages, tools):
        return math.ceil(counter(messages, tools) * factor)
    return count


def schema_cost(tools):
    """Local-estimate tokens the tool schemas add to a window (no window framing)."""
    return estimate_tokens([], tools) - BASE_TOKENS


def profile_tools(names, tools=None):
    """The catalogue schemas for ``names``; a missing name is an error, not a skip."""
    if tools is None:
        from .tool_catalogue import TOOLS as tools
    by_name = {t['function']['name']: t for t in tools}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise KeyError('Catalogue lacks profile tools: ' + ', '.join(missing))
    return [by_name[n] for n in names]
