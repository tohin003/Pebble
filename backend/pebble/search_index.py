"""Bounded incremental literal search for one selected workspace.

The SQLite tables below are a cache, never an authority. Every query walks the
eligible tree again without following links, reuses a cached body only when the
root identity and the file's dev/ino/size/mtime_ns/ctime_ns all match, reads
anything else through ``Workspace.snapshot`` and overlays the workspace's staged
drafts in memory. Matches are untrusted evidence; scope and Read permission stay
with the host.

``search`` ranks files lexically (``search_ranking``) and ``symbols`` finds
Python definitions (``code_intelligence``) and, by file extension, ts/tsx/js/go/rust
declarations (``ts_symbols``, when its optional grammars are installed) over the
same refreshed view. Their outlines live in a bounded in-memory cache keyed by
root namespace, content hash and parser fingerprint, so the persisted schema is
unchanged and every result names the exact disk or draft version it describes.
"""

import base64
import fnmatch
import hashlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
import weakref
from bisect import bisect_right
from collections import Counter

from . import code_intelligence, search_ranking, ts_symbols
from .code_intelligence import SUFFIXES
from .policy import EXCLUDED, MAX_FILE, PRIVATE, PolicyError, digest

SCHEMA = 1
MAX_ENTRIES = 50_000  # file and directory entries examined per walk
MAX_DEPTH = 64
TEXT_BUDGET = 64 * 1024 * 1024  # indexed UTF-8 bytes kept per root
READ_BUDGET = 64 * 1024 * 1024  # source bytes one refresh may read
MAX_ROOTS = 4  # cached root namespaces; least recently used are evicted
RACY_NS = 2_000_000_000  # bodies cached this soon after a change are re-verified
MAX_PATTERN = 256
MAX_GLOB = 512
GLOB_LIMIT = 500
GREP_LIMIT = 100
TEXT_CHARS = 250
MAX_CURSOR = 4096
MAX_SCOPE = 1024
CONTENT = ('text', 'binary', 'denied')  # read outcomes cached under file metadata
SEARCH_LIMIT = 25
SYMBOL_LIMIT = 100
RANK_LIMIT = 500  # ranked files or definitions a cursor can reach
PARSE_BUDGET = 8 * 1024 * 1024  # uncached Python source bytes one query may parse
PAGE_CHARS = 4_400  # serialized rows per ranked page, so a whole page fits the model wire budget
ROW_CHARS = 4_000  # one serialized row, so even a lone first row fits a page
BATCH = 64  # cached bodies fetched per store round trip
_WIDE, _NARROW = code_intelligence.IDENTIFIER_CHARS, code_intelligence.IDENTIFIER_FLOOR
# Display steps tried in order until a row fits ROW_CHARS: (identifier chars, excerpt chars, spans).
SEARCH_STEPS = ((_WIDE, search_ranking.SPAN_CHARS, search_ranking.MAX_SPANS),
                (_NARROW, search_ranking.SPAN_CHARS, search_ranking.MAX_SPANS),
                (_NARROW, 24, search_ranking.MAX_SPANS), (_NARROW, 24, 1), (_NARROW, 24, 0))
# (identifier chars, excerpt chars, keep the docstring line)
SYMBOL_STEPS = ((_WIDE, search_ranking.SPAN_CHARS, True), (_NARROW, search_ranking.SPAN_CHARS, False),
                (_NARROW, 24, False), (_NARROW, 0, False))

GLOB_SCOPE = ('eligible project files; hidden/excluded directories, symbolic links and '
              'private paths omitted; staged new files included')
GREP_SCOPE = ('case-sensitive literal within one line of eligible UTF-8 project files up to '
              '96 KB; hidden/excluded directories, symbolic links and private paths omitted; '
              'staged drafts searched instead of disk')
SEARCH_SCOPE = ('eligible project files ranked by relative path/name; contents of UTF-8 files up to 96 KB '
                'and staged drafts; Python definitions via ast; hidden/excluded directories, symbolic links '
                'and private paths omitted')
# No longer than the Phase 11 Python-only text: every symbols page carries it on the 4K wire.
SYMBOL_SCOPE = ('definitions in eligible UTF-8 files up to 96 KB and staged drafts: Python functions, classes, '
                'methods, type aliases and module/class assignments via ast; ts/tsx/js/go/rust via tree-sitter '
                'if installed; nothing imported or executed; no references or inference')
NOTE = ('Untrusted evidence ranked by lexical signals. It grants no Read witness or edit permission; '
        'Read a file before changing it.')
STALE = ('Project files or staged drafts changed since this cursor was issued. '
         'Repeat the search without a cursor.')
RANK_STALE = ('Project files, staged drafts or parse coverage changed since this cursor was issued. '
              'Repeat the search without a cursor.')

DDL = '''
CREATE TABLE IF NOT EXISTS search_index_meta(key TEXT PRIMARY KEY, value);
CREATE TABLE IF NOT EXISTS search_index_roots(ns TEXT PRIMARY KEY, root BLOB NOT NULL,
  generation INTEGER NOT NULL, state TEXT NOT NULL, stats TEXT NOT NULL, used REAL NOT NULL);
CREATE TABLE IF NOT EXISTS search_index_files(ns TEXT NOT NULL, path TEXT NOT NULL,
  meta TEXT NOT NULL, eligible INTEGER NOT NULL, status TEXT, hash TEXT, checked INTEGER,
  body BLOB, PRIMARY KEY(ns, path));
'''
PUT = 'INSERT OR REPLACE INTO search_index_files(ns, path, meta, eligible) VALUES(?,?,?,?)'
SAVE = 'UPDATE search_index_files SET status=?, hash=?, body=?, checked=? WHERE ns=? AND path=?'
TOUCH = 'UPDATE search_index_files SET checked=? WHERE ns=? AND path=?'
CLEAR = ('UPDATE search_index_files SET status=NULL, hash=NULL, body=NULL, checked=NULL '
         'WHERE ns=? AND path=?')
DROP = 'DELETE FROM search_index_files WHERE ns=? AND path=?'


class CursorError(PolicyError):
    """A page cursor that this search cannot honor."""


_SERIAL = weakref.WeakKeyDictionary()
_SERIAL_GUARD = threading.Lock()


def _serial(store):
    # One query per store at a time keeps a refresh and the page read from it consistent.
    with _SERIAL_GUARD:
        return _SERIAL.setdefault(store, threading.Lock())


def _feed(h, *parts):
    for part in parts:
        data = part if isinstance(part, bytes) else str(part).encode('utf-8', 'surrogatepass')
        h.update(len(data).to_bytes(8, 'big'))
        h.update(data)
    return h


def _hash(*parts):
    return _feed(hashlib.sha256(), *parts).hexdigest()[:32]


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _unb64(text):
    return base64.urlsafe_b64decode(text.encode('ascii') + b'=' * (-len(text) % 4))


def _stop(cancel):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise InterruptedError('Stopped by you.')


def _namespace(ws):
    from . import policy
    # A cached eligible body must not outlive a tighter path policy after an
    # upgrade. Sets and the checker bytecode/constants are stable across restart.
    checker = policy.Workspace.path.__code__
    rules = _hash('policy', repr(sorted(policy.PRIVATE)), repr(sorted(policy.EXCLUDED)),
        policy.MAX_FILE, checker.co_code, repr(checker.co_consts))
    return _hash('root', os.fsencode(ws.root), *ws.identity, rules)


def _meta(info):
    return f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}'


def _linked(entry):
    return entry.is_symlink() or bool(getattr(entry, 'is_junction', lambda: False)())


def _walked(name):
    """Whether the walk reaches this posix path's directory (used for staged drafts)."""
    parts = name.split('/')
    return len(parts) - 1 <= MAX_DEPTH and not any(
        part in EXCLUDED or part.startswith('.') or part.lower() in PRIVATE for part in parts[:-1])


def _limit(limit, ceiling):
    if type(limit) is not int or not 1 <= limit <= ceiling:
        raise PolicyError(f'Choose a result limit from 1 to {ceiling}.')


def _cancel(cancel):
    if cancel is not None and not callable(cancel) and not callable(getattr(cancel, 'is_set', None)):
        raise TypeError('cancel must be None, a callable or an Event.')


def _scope(scope):
    items = [scope] if isinstance(scope, str) else list(scope) if isinstance(scope, (list, tuple)) else []
    if (any(item is not None and type(item) not in (str, int) for item in items)
            or all(item in (None, '') for item in items)):
        raise PolicyError('Search needs the host conversation and run scope.')
    text = json.dumps(items)
    if len(text) > MAX_SCOPE:
        raise PolicyError('The search scope is too long.')
    return _hash('scope', text)


def _literal(pattern):
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= MAX_PATTERN:
        raise PolicyError(f'Grep needs a nonempty literal of at most {MAX_PATTERN} characters.')
    if pattern.splitlines() != [pattern]:
        raise PolicyError('Grep matches a literal within one line; remove line breaks from the pattern.')
    try:
        return pattern.encode('utf-8')
    except UnicodeEncodeError:
        raise PolicyError('Grep patterns must be valid Unicode text.') from None


def _glob_pattern(pattern):
    if not isinstance(pattern, str) or not 1 <= len(pattern) <= MAX_GLOB:
        raise PolicyError(f'Glob needs a nonempty pattern of at most {MAX_GLOB} characters.')
    try:
        pattern.encode('utf-8')
    except UnicodeEncodeError:
        raise PolicyError('Glob patterns must be valid Unicode text.') from None


def _path_filter(path):
    """(predicate, canonical text) for an optional relative file/directory prefix or glob."""
    if path is None:
        return (lambda _name: True), ''
    if not isinstance(path, str) or not 1 <= len(path) <= MAX_GLOB or path.splitlines() != [path]:
        raise PolicyError(f'Narrow a search with one relative path or glob of at most {MAX_GLOB} characters.')
    try:
        path.encode('utf-8')
    except UnicodeEncodeError:
        raise PolicyError('Search paths must be valid Unicode text.') from None
    text = path.strip()
    while text.startswith('./'):
        text = text[2:]
    if text.startswith('/') or '..' in text.split('/'):
        raise PolicyError('Search paths stay relative to the selected project.')
    if any(char in text for char in '*?['):
        patterns = {text, text.removeprefix('**/')}
        return (lambda name: any(fnmatch.fnmatch(name, item) for item in patterns)), 'glob:' + text
    prefix = text.rstrip('/')
    if prefix in ('', '.'):
        return (lambda _name: True), ''
    return (lambda name: name == prefix or name.startswith(prefix + '/')), 'path:' + prefix


def _outline_language(path):
    """'python', a ts_symbols grammar name, or None when symbols mode does not outline the file."""
    return 'python' if path.endswith(SUFFIXES) else ts_symbols.language_for(path)


def _chars(row):
    return len(json.dumps(row, ensure_ascii=False, separators=(',', ':')))


def _fit(rows):
    """The longest prefix of rows within PAGE_CHARS (at least one); later rows stay for the cursor."""
    used = 0
    for count, row in enumerate(rows):
        used += _chars(row) + 1
        if count and used > PAGE_CHARS:
            return rows[:count]
    return rows


def _evidence(path, number, line, column, file_hash, basis):
    # A bounded window that keeps the match visible; Read returns the full line.
    start = max(0, min(column - TEXT_CHARS // 5, len(line) - TEXT_CHARS))
    return {'path': path, 'line': number, 'column': column + 1,
            'text': line[start:start + TEXT_CHARS], 'text_start': start + 1,
            'text_truncated': len(line) > TEXT_CHARS, 'file_hash': file_hash, 'basis': basis}


def _purge(db, ns):
    db.execute('DELETE FROM search_index_files WHERE ns=?', (ns,))
    db.execute('DELETE FROM search_index_roots WHERE ns=?', (ns,))


class SearchIndex:
    """Literal Grep and fnmatch Glob over an incrementally refreshed per-root cache."""

    def __init__(self, store):
        self.store = store
        self._serial = _serial(store)
        self._outlines = code_intelligence.OutlineCache()
        with store.lock:
            db = store.db
            if db.in_transaction:
                raise PolicyError('Create the search cache outside an application transaction.')
            db.executescript(DDL)
            row = db.execute("SELECT value FROM search_index_meta WHERE key='schema'").fetchone()
            if row is not None and row[0] != SCHEMA:
                # Only this cache's own tables are rebuilt; application tables are untouched.
                db.executescript('DROP TABLE search_index_files; DROP TABLE search_index_roots; '
                                 'DELETE FROM search_index_meta;' + DDL)
            db.execute("INSERT OR IGNORE INTO search_index_meta VALUES('schema', ?)", (SCHEMA,))
            db.execute("INSERT OR IGNORE INTO search_index_meta VALUES('cursor_key', ?)",
                       (secrets.token_bytes(32),))
            self._key = db.execute(
                "SELECT value FROM search_index_meta WHERE key='cursor_key'").fetchone()[0]
            db.commit()

    def glob(self, ws, pattern, *, scope, cursor=None, limit=100, cancel=None):
        """Page eligible paths matching ``pattern`` (fnmatch), ordered by relative path."""
        _glob_pattern(pattern)
        _limit(limit, GLOB_LIMIT)
        _cancel(cancel)
        ns = _namespace(ws)
        bound = (ns, _scope(scope), _hash('glob', pattern))
        after = self._open(cursor, 'glob', *bound)
        with self._serial:
            view = self._refresh(ws, ns, cancel, read=False)
            overlay = self._overlay(ws)
            token = self._token(view, overlay, content=False)
            if after is not None and (after['g'], after['w']) != (view['generation'], token):
                raise CursorError(STALE)
            disk = [entry[0] for entry in view['files']]
            staged = set(overlay).difference(disk)
            names = sorted(disk + list(staged))
            patterns = {pattern, pattern.removeprefix('**/')}
            if after and after['p'][0] >= len(names):
                raise CursorError(STALE)
            start = after['p'][0] + 1 if after else 0
            found = []
            for count, name in enumerate(names[start:]):
                if count % 1024 == 0:
                    _stop(cancel)
                if any(fnmatch.fnmatch(name, item) for item in patterns):
                    found.append(name)
                    if len(found) > limit:
                        break
        page, more = found[:limit], len(found) > limit
        scan_complete = not (view['truncated'] or view['errors'])
        coverage = {
            'scope': GLOB_SCOPE, 'inventoried': len(names),
            'inventory_truncated': view['truncated'], 'scan_errors': view['errors'],
            'entry_limit': MAX_ENTRIES, 'depth_limit': MAX_DEPTH,
            'match_limit': limit, 'matches_returned': len(page), 'matches_truncated': more,
            'staged': [name for name in page if name in staged], 'drafts_overlaid': len(overlay),
            'resumed': after is not None, 'scan_complete': scan_complete,
            'complete': scan_complete and not more,
            'cache': {'generation': view['generation'], 'reused': view['reused'], 'read': view['read']},
        }
        next_cursor = self._seal('glob', *bound, view['generation'], token,
            [bisect_right(names, page[-1]) - 1]) if more else None
        return {'files': page, 'coverage': coverage, 'cursor': next_cursor}

    def grep(self, ws, pattern, *, scope, cursor=None, limit=30, cancel=None):
        """Page literal line matches, ordered by relative path then line."""
        needle = _literal(pattern)
        _limit(limit, GREP_LIMIT)
        _cancel(cancel)
        ns = _namespace(ws)
        bound = (ns, _scope(scope), _hash('grep', pattern))
        after = self._open(cursor, 'grep', *bound)
        with self._serial:
            view = self._refresh(ws, ns, cancel, read=True)
            overlay = self._overlay(ws)
            token = self._token(view, overlay, content=True)
            if after is not None and (after['g'], after['w']) != (view['generation'], token):
                raise CursorError(STALE)
            # The authenticated view token binds an ordinal to this exact sorted
            # inventory. Long paths therefore cannot produce oversized cursors.
            names = sorted({entry[0] for entry in view['files']} | set(overlay))
            if after and after['p'][0] >= len(names):
                raise CursorError(STALE)
            start_path, start_line = (names[after['p'][0]], after['p'][1]) if after else ('', 0)
            # instr() on UTF-8 blobs is an exact byte-substring test, so it equals a
            # code-point literal match and only narrows which files are split into lines.
            with self.store.lock:
                disk = [row[0] for row in self.store.db.execute(
                    "SELECT path FROM search_index_files WHERE ns=? AND status='text' AND path>=? "
                    'AND instr(body, ?)>0 ORDER BY path', (ns, start_path, needle)).fetchall()]
            candidates = sorted({path for path in disk if path not in overlay}
                                | {path for path, text in overlay.items()
                                   if path >= start_path and pattern in text})
            matches = []
            for path in candidates:
                _stop(cancel)
                text, file_hash, basis = self._source(ns, path, overlay)
                first = start_line if path == start_path else 0
                for number, line in enumerate(text.splitlines()[first:], first + 1):
                    column = line.find(pattern)
                    if column >= 0:
                        matches.append(_evidence(path, number, line, column, file_hash, basis))
                        if len(matches) > limit:
                            break
                if len(matches) > limit:
                    break
        page, more = matches[:limit], len(matches) > limit
        files = [entry for entry in view['files'] if entry[0] not in overlay]
        skipped = Counter(status for _path, _size, status, _hash in files if status != 'text')
        disk_paths = {entry[0] for entry in view['files']}
        scan_complete = not (view['truncated'] or view['errors'] or skipped)
        coverage = {
            'scope': GREP_SCOPE,
            'inventoried': len(view['files']) + len(set(overlay) - disk_paths),
            'inventory_truncated': view['truncated'], 'inventory_errors': view['errors'],
            'entry_limit': MAX_ENTRIES, 'depth_limit': MAX_DEPTH,
            'searched': sum(entry[2] == 'text' for entry in files) + len(overlay),
            'skipped': sum(skipped.values()), 'skip_reasons': dict(sorted(skipped.items())),
            'file_size_limit': MAX_FILE, 'indexed_bytes': view['used'], 'text_budget': TEXT_BUDGET,
            'read_budget': READ_BUDGET, 'drafts_overlaid': len(overlay),
            'match_limit': limit, 'matches_returned': len(page), 'matches_truncated': more,
            'resumed': after is not None, 'scan_complete': scan_complete,
            'complete': scan_complete and not more,
            'cache': {'generation': view['generation'], 'reused': view['reused'], 'read': view['read']},
        }
        position = [bisect_right(names, page[-1]['path']) - 1, page[-1]['line']] if more else None
        next_cursor = self._seal('grep', *bound, view['generation'], token, position) if more else None
        return {'untrusted_matches': page, 'coverage': coverage, 'cursor': next_cursor}

    def search(self, ws, query, *, scope, path=None, cursor=None, limit=5, cancel=None):
        """Page files ranked by lexical relevance, with hash-pinned untrusted evidence spans.

        Order is descending integer score, then relative path. ``path`` narrows
        to a relative file/directory prefix or glob. A cursor binds the query,
        path filter, ranking settings, conversation/run scope, root identity and
        policy, and the exact disk, draft and Python parse state.
        """
        plan = search_ranking.plan(query)
        included, where = _path_filter(path)
        _limit(limit, SEARCH_LIMIT)
        _cancel(cancel)
        ns = _namespace(ws)
        settings = (search_ranking.fingerprint(), code_intelligence.fingerprint(), RANK_LIMIT)
        bound = (ns, _scope(scope), _hash('search', plan.text, where, *settings))
        after = self._open(cursor, 'search', *bound)
        with self._serial:
            view = self._refresh(ws, ns, cancel, read=True, kind='search')
            overlay = self._overlay(ws)
            docs, outside = self._documents(view, overlay, included)
            ranked, counts, state = self._rank(ns, docs, overlay, plan, cancel)
            token = _hash(self._token(view, overlay, content=True), state)
            start = self._resume(after, view, token, len(ranked))
            page = ranked[start:start + limit]
            results = []
            for rank, item in enumerate(page, start + 1):
                _stop(cancel)
                results.append(self._hit(ns, overlay, plan, item, rank))
        results = _fit(results)
        more = start + len(results) < len(ranked)
        skipped = Counter(doc[1] for doc in docs if doc[1] != 'text')
        coverage = {
            'scope': SEARCH_SCOPE, 'ranking': search_ranking.DESCRIPTION,
            **self._extent(view, overlay, where, outside),
            'in_scope': len(docs), 'searched': counts['searched'], 'path_only': sum(skipped.values()),
            'skip_reasons': dict(sorted(skipped.items())), 'file_size_limit': MAX_FILE,
            'indexed_bytes': view['used'], **self._parsing(counts),
        }
        coverage.update(self._paging(counts, view, overlay, limit, results, more, after, skipped,
                                     clipped=len(results) < len(page)))
        next_cursor = self._seal('search', *bound, view['generation'], token,
                                 [start + len(results) - 1]) if more else None
        return {'untrusted_results': results, 'coverage': coverage, 'cursor': next_cursor, 'note': NOTE}

    def symbols(self, ws, query, *, scope, path=None, cursor=None, limit=10, cancel=None):
        """Page definitions matching ``query``, or every definition under ``path``.

        Python files are outlined with ``ast``; ts/tsx/js/go/rust files, chosen by
        extension, with ``ts_symbols`` when its grammars are installed (otherwise
        they count as unavailable, never as files without definitions).
        Matches order by tier (exact, equivalent, prefix, substring, words), then
        definitions before assignments, shallower nesting, path, line and column.
        An empty query with a path lists that scope's outline in source order.
        Locations come from parsing the exact cached or draft text whose hash is
        returned; nothing is imported or executed.
        """
        probe = code_intelligence.probe(query, outline=path is not None)
        included, where = _path_filter(path)
        _limit(limit, SYMBOL_LIMIT)
        _cancel(cancel)
        ns = _namespace(ws)
        bound = (ns, _scope(scope), _hash('symbols', probe.text if probe else '', where,
                                          code_intelligence.fingerprint(), RANK_LIMIT))
        after = self._open(cursor, 'symbols', *bound)
        with self._serial:
            view = self._refresh(ws, ns, cancel, read=True, kind='symbols')
            overlay = self._overlay(ws)
            docs, outside = self._documents(view, overlay, included)
            outlined = [doc for doc in docs if _outline_language(doc[0])]
            python = [doc for doc in outlined if doc[0].endswith(SUFFIXES)]
            found, counts, state = self._definitions(ns, outlined, overlay, probe, cancel)
            token = _hash(self._token(view, overlay, content=True), state)
            start = self._resume(after, view, token, len(found))
            page = found[start:start + limit]
            results = _fit(self._symbol_rows(ns, overlay, page, start, probe, cancel))
        more = start + len(results) < len(found)
        skipped = Counter(doc[1] for doc in outlined if doc[1] != 'text')
        coverage = {
            'scope': SYMBOL_SCOPE, **self._extent(view, overlay, where, outside),
            'python_files': len(python), 'searched': counts['searched'], 'skipped': sum(skipped.values()),
            'skip_reasons': dict(sorted(skipped.items())), 'file_size_limit': MAX_FILE,
            'prefiltered': counts['prefiltered'], **self._parsing(counts),
        }
        if len(outlined) > len(python):  # absent for Python-only scopes, so their pages stay as small as before
            coverage['tree_sitter'] = self._tree_sitter(counts, outlined)
        coverage.update(self._paging(counts, view, overlay, limit, results, more, after, skipped,
                                     clipped=len(results) < len(page)))
        next_cursor = self._seal('symbols', *bound, view['generation'], token,
                                 [start + len(results) - 1]) if more else None
        return {'untrusted_symbols': results, 'coverage': coverage, 'cursor': next_cursor, 'note': NOTE}

    @staticmethod
    def _documents(view, overlay, included):
        """In-scope (path, status, hash, basis) rows in path order; drafts replace disk views."""
        docs, outside = [], 0
        for path, _size, status, file_hash in view['files']:
            if path in overlay:
                continue
            if included(path):
                docs.append((path, status, file_hash, 'disk'))
            else:
                outside += 1
        for path, text in overlay.items():
            if included(path):
                docs.append((path, 'text', digest(text.encode('utf-8')), 'draft'))
            else:
                outside += 1
        docs.sort()
        return docs, outside

    def _texts(self, ns, docs, overlay, cancel):
        """Yield (doc, UTF-8 body) for searchable rows, fetching cached bodies in bounded batches."""
        texts = [doc for doc in docs if doc[1] == 'text']
        for first in range(0, len(texts), BATCH):
            _stop(cancel)
            chunk = texts[first:first + BATCH]
            wanted = [doc[0] for doc in chunk if doc[3] == 'disk']
            rows = {}
            if wanted:
                with self.store.lock:
                    rows = {row[0]: row[1:] for row in self.store.db.execute(
                        "SELECT path, hash, body FROM search_index_files WHERE ns=? AND status='text' "
                        f"AND path IN ({','.join('?' * len(wanted))})", (ns, *wanted)).fetchall()}
            for doc in chunk:
                if doc[3] == 'draft':
                    yield doc, overlay[doc[0]].encode('utf-8')
                    continue
                row = rows.get(doc[0])
                if row is None or row[0] != doc[2] or row[1] is None:
                    raise PolicyError('The search index changed during this query. Search again.')
                yield doc, row[1]

    def _outline(self, key, body, budget, counts, language='python'):
        """A cached or newly parsed outline within the parse budget, else None (deferred).

        Python outcomes count under their status; tree-sitter outcomes under
        ``ts:`` keys, so the python_* coverage fields keep their meaning.
        """
        prefix = '' if language == 'python' else 'ts:'
        found = self._outlines.get(key)
        if found is not None:
            counts['outlines_reused'] += 1
        elif len(body) > budget:
            counts[prefix + 'deferred'] += 1
            return None, budget
        else:
            text = body.decode('utf-8')
            found = (code_intelligence.outline(text) if language == 'python'
                     else ts_symbols.outline(text, language))
            if not self._outlines.put(key, found):
                counts['outlines_not_cached'] += 1  # larger than a whole cache bound; parsed again next time
            budget -= len(body)
            counts['outlines_parsed'] += 1
        counts[prefix + found.status] += 1
        if found.issue:
            counts[prefix + 'issue:' + found.issue] += 1
        return found, budget

    def _rank(self, ns, docs, overlay, plan, cancel):
        """Score every in-scope file; return the bounded ranking, counters and parse-state digest."""
        counts, state = Counter(), hashlib.sha256()
        budget, settings = PARSE_BUDGET, code_intelligence.fingerprint()
        paths = {doc[0]: search_ranking.path_signals(doc[0], plan) for doc in docs}
        content, length = {}, 0
        for doc, body in self._texts(ns, docs, overlay, cancel):
            counts['searched'] += 1
            length += len(body)
            tf, phrase = search_ranking.content_signals(body.lower(), plan)
            if not (any(tf) or phrase):
                continue  # no definition name can contain a term the text lacks
            python, outline, symbol, exact = doc[0].endswith(SUFFIXES), None, None, False
            if python:
                _stop(cancel)
                outline, budget = self._outline((ns, doc[2], settings), body, budget, counts)
                _feed(state, doc[0], outline.status if outline else 'deferred')
                if outline is not None:
                    symbol, exact = search_ranking.symbol_signals(outline.symbols, plan)
            content[doc[0]] = (tf, phrase, symbol, exact, len(body), python, outline)
        df = [0] * len(plan.terms)
        for doc in docs:
            name, directory, _exact, _phrase = paths[doc[0]]
            tf = content[doc[0]][0] if doc[0] in content else None
            for index in range(len(plan.terms)):
                df[index] += bool(name[index] or directory[index] or tf and tf[index])
        idf = search_ranking.rarity(df, len(docs))
        average = length / counts['searched'] if counts['searched'] else 0
        ranked = []
        for doc in docs:
            name, directory, exact_name, path_phrase = paths[doc[0]]
            tf, phrase, symbol, exact, size, python, outline = content.get(
                doc[0], (None, 0, None, False, 0, False, None))
            signals = search_ranking.Signals(tf, phrase, name, directory, exact_name, path_phrase,
                                             symbol, exact, size, doc[1] == 'text')
            points, matched = search_ranking.score(signals, idf, average, plan)
            if points:
                parse = (outline.status if outline else 'deferred') if python else None
                ranked.append((-points, doc[0], doc, signals, matched, parse, outline))
        ranked.sort(key=lambda item: item[:2])
        counts['candidates'] = len(ranked)
        return ranked[:RANK_LIMIT], counts, state.hexdigest()[:32]

    def _hit(self, ns, overlay, plan, item, rank):
        negative, path, doc, signals, matched, parse, outline = item
        entry = {'rank': rank, 'path': path, 'score': -negative, 'basis': doc[3], 'file_hash': doc[2],
                 'content_searched': doc[1] == 'text', 'matched_terms': matched,
                 'signals': search_ranking.explain(signals)}
        if doc[1] != 'text':
            entry['skip_reason'] = doc[1]  # ranked by path alone; contents were not searched
            return entry
        if parse:
            entry['parse'] = parse
        text = self._current(ns, path, overlay, doc[2])
        entry['total_lines'] = len(text.splitlines())
        symbols, available = outline.symbols if outline else (), None
        for step, (identifiers, chars, count) in enumerate(SEARCH_STEPS):
            def label(symbol, limit=identifiers):
                return code_intelligence.display_qualname(symbols, symbol, limit)
            entry['spans'] = search_ranking.spans(text, plan, symbols, limit=count, chars=chars, label=label)
            available = len(entry['spans']) if available is None else available
            for key in ('spans_omitted', 'shortened_to_fit'):
                entry.pop(key, None)
            if len(entry['spans']) < available:
                entry['spans_omitted'] = available - len(entry['spans'])
            if step:
                entry['shortened_to_fit'] = True  # identifiers/excerpts/spans reduced to bound the row
            if _chars(entry) <= ROW_CHARS:
                break
        return entry

    def _current(self, ns, path, overlay, file_hash):
        """Source text of a ranked row, refusing anything but the exact version that was ranked."""
        text, current, _basis = self._source(ns, path, overlay)
        if current != file_hash:
            raise PolicyError('The search index changed during this query. Search again.')
        return text

    def _definitions(self, ns, docs, overlay, probe, cancel):
        """Matching definitions in bounded ranked order, counters and parse-state digest."""
        counts, state = Counter(), hashlib.sha256()
        budget, settings, found = PARSE_BUDGET, {'python': code_intelligence.fingerprint()}, []
        for doc, body in self._texts(ns, docs, overlay, cancel):
            counts['searched'] += 1
            language = _outline_language(doc[0])
            if not code_intelligence.possible(probe, body.lower()):
                counts['prefiltered'] += 1  # exact: this text cannot define a match
                continue
            _stop(cancel)
            if language not in settings:  # grammar availability is probed only when such a file is in scope
                settings[language] = ts_symbols.fingerprint(language)
            outline, budget = self._outline((ns, doc[2], settings[language]), body, budget, counts, language)
            # The fingerprint is part of the parse state, so cursors go stale if a grammar appears or changes.
            _feed(state, doc[0], outline.status if outline else 'deferred',
                  *(() if language == 'python' else (settings[language],)))
            for position, symbol in enumerate(outline.symbols if outline else ()):
                tier = code_intelligence.match(symbol, probe, outline.symbols)
                if tier is None:
                    continue
                counts['candidates'] += 1
                # (path, position) is unique, so keys never compare the symbols themselves.
                key = ((doc[0], symbol.line, symbol.column, position) if probe is None else
                       (tier, code_intelligence.KIND_ORDER[symbol.kind], symbol.depth, doc[0],
                        symbol.line, symbol.column, position))
                found.append((key, tier, doc, symbol, outline))
                if len(found) >= 4 * RANK_LIMIT:  # keep memory bounded; keys are unique
                    found.sort(key=lambda item: item[0])
                    del found[RANK_LIMIT:]
        found.sort(key=lambda item: item[0])
        return found[:RANK_LIMIT], counts, state.hexdigest()[:32]

    def _symbol_rows(self, ns, overlay, page, start, probe, cancel):
        lines, rows = {}, []
        for rank, (_key, tier, doc, symbol, outline) in enumerate(page, start + 1):
            path, _status, file_hash, basis = doc
            if path not in lines:
                _stop(cancel)
                lines[path] = self._current(ns, path, overlay, file_hash).splitlines()
            source = lines[path]
            text = source[symbol.line - 1] if 0 < symbol.line <= len(source) else ''
            match = code_intelligence.TIERS[tier] if probe else 'outline'
            for step, (identifiers, chars, keep_doc) in enumerate(SYMBOL_STEPS):
                row = self._symbol_row(rank, doc, symbol, outline, match, text, identifiers, chars, keep_doc)
                if step:
                    row['shortened_to_fit'] = True  # identifiers/excerpt/docstring reduced to bound the row
                if _chars(row) <= ROW_CHARS:
                    break
            rows.append(row)
        return rows

    @staticmethod
    def _symbol_row(rank, doc, symbol, outline, match, text, identifiers, chars, keep_doc):
        path, _status, file_hash, basis = doc
        qualified, short_qualified = code_intelligence.display_qualname(outline.symbols, symbol, identifiers)
        name, short_name = code_intelligence.display_name(symbol.name, identifiers)
        row = {'rank': rank, 'path': path, 'qualname': qualified, 'name': name,
               'kind': symbol.kind, 'line': symbol.line, 'end_line': symbol.end_line,
               'column': symbol.column, 'match': match, 'file_hash': file_hash, 'basis': basis,
               'parse': outline.status, **search_ranking.excerpt(text, symbol.column - 1, chars)}
        # Display forms only: path, line, column and file_hash remain the definition's identity.
        if short_qualified:
            row['qualname_truncated'] = True
        if short_name:
            row['name_truncated'] = True
        if symbol.start_line != symbol.line:
            row['start_line'] = symbol.start_line  # first decorator
        if symbol.doc:
            row['doc' if keep_doc else 'doc_omitted'] = symbol.doc if keep_doc else True
        if symbol.uncertain:
            row['end_line_uncertain'] = True
        return row

    @staticmethod
    def _resume(after, view, token, available):
        """First ordinal to return; a cursor must match this exact view, drafts and parse state."""
        if after is None:
            return 0
        if (after['g'], after['w']) != (view['generation'], token) or after['p'][0] + 1 >= available:
            raise CursorError(RANK_STALE)
        return after['p'][0] + 1

    @staticmethod
    def _extent(view, overlay, where, outside):
        disk = {entry[0] for entry in view['files']}
        return {'inventoried': len(view['files']) + len(set(overlay) - disk),
                'inventory_truncated': view['truncated'], 'inventory_errors': view['errors'],
                'entry_limit': MAX_ENTRIES, 'depth_limit': MAX_DEPTH,
                'path_filter': where or None, 'outside_filter': outside}

    @staticmethod
    def _parsing(counts):
        issues = {key.removeprefix('issue:'): value for key, value in sorted(counts.items())
                  if key.startswith('issue:')}
        return {'python_parsed': counts['parsed'], 'python_partial': counts['partial'],
                'python_failed': counts['failed'], 'python_deferred': counts['deferred'],
                'parse_issues': issues, 'parse_budget': PARSE_BUDGET}

    @staticmethod
    def _tree_sitter(counts, docs):
        """tree-sitter outline coverage; ``unavailable`` files had no grammar and were not outlined."""
        languages = Counter(_outline_language(doc[0]) for doc in docs)
        languages.pop('python', None)
        issues = {key.removeprefix('ts:issue:'): value for key, value in sorted(counts.items())
                  if key.startswith('ts:issue:')}
        return {'files': sum(languages.values()), 'languages': dict(sorted(languages.items())),
                'parsed': counts['ts:parsed'], 'partial': counts['ts:partial'], 'failed': counts['ts:failed'],
                'deferred': counts['ts:deferred'], 'issues': issues}

    @staticmethod
    def _paging(counts, view, overlay, limit, rows, more, after, skipped, clipped):
        truncated = counts['candidates'] > RANK_LIMIT
        scan = not (view['truncated'] or view['errors'] or skipped)
        parse = not (counts['partial'] or counts['failed'] or counts['deferred']
                     or counts['ts:partial'] or counts['ts:failed'] or counts['ts:deferred'])
        return {
            'candidates': counts['candidates'], 'rank_limit': RANK_LIMIT, 'ranked_truncated': truncated,
            'drafts_overlaid': len(overlay), 'match_limit': limit, 'matches_returned': len(rows),
            'matches_truncated': more, 'page_char_limit': PAGE_CHARS, 'page_char_clipped': clipped,
            'row_char_limit': ROW_CHARS,
            'rows_shortened_to_fit': sum(1 for row in rows if row.get('shortened_to_fit')),
            # Paths and hashes are never cut; only their JSON escaping could leave a row above the bound.
            'rows_over_char_limit': sum(1 for row in rows if _chars(row) > ROW_CHARS),
            'resumed': after is not None, 'scan_complete': scan,
            'parse_complete': parse, 'complete': scan and parse and not truncated and not more,
            'cache': {'generation': view['generation'], 'reused': view['reused'], 'read': view['read'],
                      'outlines_parsed': counts['outlines_parsed'],
                      'outlines_reused': counts['outlines_reused'],
                      'outlines_not_cached': counts['outlines_not_cached']},
        }

    def describe(self, ws):
        """Content-free metadata for this root's cache, or None when none is current."""
        try:
            info = os.stat(ws.root, follow_symlinks=False)
        except OSError:
            return None
        if (info.st_dev, info.st_ino) != ws.identity:
            return None
        ns = _namespace(ws)
        with self.store.lock:
            root = self.store.db.execute(
                'SELECT generation, stats, used FROM search_index_roots WHERE ns=?', (ns,)).fetchone()
            counts = self.store.db.execute(
                'SELECT status, count(*), coalesce(sum(length(body)), 0) FROM search_index_files '
                'WHERE ns=? AND eligible=1 GROUP BY status', (ns,)).fetchall()
        if root is None:
            return None
        stats = json.loads(root[1])
        return {
            'namespace': ns, 'schema': SCHEMA, 'generation': root[0], 'last_used': root[2],
            'refreshed': stats['refreshed'], 'last_query': stats['kind'],
            'inventoried': stats['inventoried'], 'inventory_truncated': stats['inventory_truncated'],
            'scan_errors': stats['scan_errors'],
            'status_counts': {status or 'unindexed': count for status, count, _size in counts},
            'indexed_bytes': sum(size for status, _count, size in counts if status == 'text'),
            'limits': {'entries': MAX_ENTRIES, 'depth': MAX_DEPTH, 'text_bytes': TEXT_BUDGET,
                       'read_bytes': READ_BUDGET, 'file_bytes': MAX_FILE, 'roots': MAX_ROOTS},
        }

    def _refresh(self, ws, ns, cancel, read, kind=None):
        """Walk, reuse or reread, then atomically record one consistent view."""
        _stop(cancel)
        started = time.time_ns()
        with self.store.lock:
            cached = {row[0]: row[1:] for row in self.store.db.execute(
                'SELECT path, meta, eligible, status, hash, checked FROM search_index_files '
                'WHERE ns=?', (ns,)).fetchall()}
        walk = self._walk(ws, ns, cancel)
        writes, files, errors = [], [], walk['errors']
        used = spent = reads = reused = 0
        state, content = hashlib.sha256(), hashlib.sha256()
        for count, (path, meta, size, newest) in enumerate(walk['entries']):
            if count % 256 == 0:
                _stop(cancel)
            row = cached.pop(path, None)
            same = row is not None and row[0] == meta
            eligible = row[1] if same else self._eligible(ws, path)
            if eligible is None:  # vanished or raced during the policy check
                errors += 1
                if row is not None:
                    writes.append((DROP, (ns, path)))
                continue
            if not same:
                writes.append((PUT, (ns, path, meta, eligible)))
            if not eligible:
                continue
            kept = same and row[2] in CONTENT and row[4] is not None
            # Coarse timestamps can hide a rewrite made just after indexing; re-verify those.
            trusted = kept and row[4] - newest > RACY_NS
            current = trusted or kept and not read
            status, file_hash = (row[2], row[3]) if current else (None, None)
            if size > MAX_FILE:
                status = 'large'
            elif read and not trusted:
                if used + size > TEXT_BUDGET:
                    status = 'budget'
                elif spent + size > READ_BUDGET:
                    status = 'deferred'
                else:
                    _stop(cancel)
                    spent += size
                    reads += 1
                    status, file_hash, body = self._read(ws, ns, path, meta, size)
                    if status in CONTENT:
                        current = True
                        if kept and (status, file_hash) == (row[2], row[3]):
                            writes.append((TOUCH, (started, ns, path)))
                        else:
                            writes.append((SAVE, (status, file_hash, body, started, ns, path)))
            if kept and not current:
                writes.append((CLEAR, (ns, path)))
            if status == 'text':
                if used + size > TEXT_BUDGET:
                    status = 'budget'
                    writes.append((CLEAR, (ns, path)))
                else:
                    used += size
            reused += trusted
            files.append((path, size, status, file_hash))
            _feed(state, path, meta)
            _feed(content, path, status, file_hash)
        writes.extend((DROP, (ns, path)) for path in cached)
        self._check_root(ws, ns)  # never record a view of a folder replaced mid-refresh
        _stop(cancel)
        state = _feed(state, 'walk', walk['truncated'], errors).hexdigest()[:32]
        stats = {'inventoried': len(files), 'inventory_truncated': walk['truncated'],
                 'scan_errors': errors, 'refreshed': time.time(),
                 'kind': kind or ('grep' if read else 'glob')}
        generation = self._commit(ws, ns, writes, state, stats)
        return {'files': files, 'truncated': walk['truncated'], 'errors': errors, 'state': state,
                'content': content.hexdigest()[:32], 'generation': generation, 'used': used,
                'read': reads, 'reused': reused}

    def _commit(self, ws, ns, writes, state, stats):
        db, root = self.store.db, os.fsencode(ws.root)
        with self.store.lock:
            if db.in_transaction:
                raise PolicyError('Refresh search outside an application transaction.')
            try:
                db.execute('BEGIN IMMEDIATE')
                for sql, args in writes:
                    db.execute(sql, args)
                row = db.execute('SELECT generation, state FROM search_index_roots WHERE ns=?',
                                 (ns,)).fetchone()
                generation = 1 if row is None else row[0] + (row[1] != state)
                db.execute('INSERT OR REPLACE INTO search_index_roots VALUES(?,?,?,?,?,?)',
                           (ns, root, generation, state, json.dumps(stats), time.time()))
                # Another folder now at this path, or roots beyond the LRU bound.
                stale = {other for (other,) in db.execute(
                    'SELECT ns FROM search_index_roots WHERE root=? AND ns<>?', (root, ns)).fetchall()}
                stale.update(other for (other,) in db.execute(
                    'SELECT ns FROM search_index_roots WHERE ns<>? ORDER BY used DESC LIMIT -1 OFFSET ?',
                    (ns, max(MAX_ROOTS - 1, 0))).fetchall())
                for other in stale:
                    _purge(db, other)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        for other in stale:
            self._outlines.drop(other)
        return generation

    def _walk(self, ws, ns, cancel):
        """List regular files under eligible directories without following links."""
        anchored = (os.scandir in os.supports_fd and os.open in os.supports_dir_fd
                    and hasattr(os, 'O_DIRECTORY') and hasattr(os, 'O_NOFOLLOW'))
        walk = {'entries': [], 'seen': 0, 'pending': 0, 'errors': 0, 'truncated': False,
                'anchored': anchored, 'cancel': cancel}
        if not anchored:
            self._check_root(ws, ns)
            self._visit(walk, str(ws.root), '', 0)
        else:
            try:
                root = os.open(ws.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError:
                self._check_root(ws, ns)
                raise PolicyError('The selected project folder could not be opened for search.') from None
            try:
                info = os.fstat(root)
                if (info.st_dev, info.st_ino) != ws.identity:
                    raise self._lost(ws, ns)
                self._visit(walk, root, '', 0)
            finally:
                os.close(root)
        walk['entries'].sort()
        return walk

    def _visit(self, walk, handle, prefix, depth):
        # Listed-but-unvisited entries are reserved against the budget, bounding memory.
        try:
            with os.scandir(handle) as listing:
                room, entries = MAX_ENTRIES - walk['seen'] - walk['pending'], []
                for entry in listing:
                    if len(entries) >= room:
                        walk['truncated'] = True
                        break
                    entries.append(entry)
        except OSError:
            walk['errors'] += 1
            return
        walk['pending'] += len(entries)
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            walk['pending'] -= 1
            walk['seen'] += 1
            if walk['seen'] % 256 == 0:
                _stop(walk['cancel'])
            try:
                self._entry(walk, handle, prefix, depth, entry)
            except InterruptedError:
                raise
            except (OSError, UnicodeError):
                walk['errors'] += 1

    def _entry(self, walk, handle, prefix, depth, entry):
        name = entry.name
        if _linked(entry):
            return
        if entry.is_dir(follow_symlinks=False):
            if name in EXCLUDED or name.startswith('.') or name.lower() in PRIVATE:
                return
            if depth >= MAX_DEPTH:
                walk['truncated'] = True
            elif not walk['anchored']:
                self._visit(walk, entry.path, f'{prefix}{name}/', depth + 1)
            else:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handle)
                try:
                    self._visit(walk, child, f'{prefix}{name}/', depth + 1)
                finally:
                    os.close(child)
        elif entry.is_file(follow_symlinks=False):
            info = (entry.stat(follow_symlinks=False) if walk['anchored']
                    else os.stat(entry.path, follow_symlinks=False))
            path = prefix + name
            path.encode('utf-8')  # undecodable names can be neither stored nor requested
            walk['entries'].append((path, _meta(info), info.st_size,
                                    max(info.st_mtime_ns, info.st_ctime_ns)))

    @staticmethod
    def _eligible(ws, path):
        try:
            ws.path(path)
            return 1
        except PolicyError:
            return 0
        except OSError:
            return None

    def _read(self, ws, ns, path, meta, size):
        """Read one file via Workspace.snapshot; keep nothing that no longer matches its metadata."""
        try:
            data = ws.snapshot(path)
        except PermissionError:
            return 'denied', None, None
        except (FileNotFoundError, PolicyError):
            self._check_root(ws, ns)  # a replaced root aborts the whole query
            return 'changed', None, None
        except OSError:
            return 'unreadable', None, None
        try:
            after = _meta(os.stat(ws.root / path, follow_symlinks=False))
        except OSError:
            return 'changed', None, None
        if len(data) != size or after != meta:
            return 'changed', None, None
        try:
            data.decode('utf-8')
        except UnicodeDecodeError:
            return 'binary', digest(data), None
        return 'text', digest(data), data

    @staticmethod
    def _overlay(ws):
        """Current staged drafts, validated through Workspace.read and never stored."""
        if len(ws.drafts) > MAX_ENTRIES:
            raise PolicyError('Staged search views exceed the declared entry budget.')
        overlay, used = {}, 0
        for name in sorted(ws.drafts):
            try:
                text = ws.read(name)  # drops drafts whose disk baseline moved
            except (OSError, UnicodeError, PolicyError):
                continue
            draft = ws.drafts.get(name)
            if draft is not None and draft['after'] == text and _walked(name):
                used += len(text.encode('utf-8'))
                if used > TEXT_BUDGET:
                    raise PolicyError('Staged search views exceed the declared text budget.')
                overlay[name] = text
        return overlay

    def _source(self, ns, path, overlay):
        if path in overlay:
            text = overlay[path]
            return text, digest(text.encode('utf-8')), 'draft'
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT hash, body FROM search_index_files WHERE ns=? AND path=? AND status='text'",
                (ns, path)).fetchone()
        if row is None:
            raise PolicyError('The search index changed during this query. Search again.')
        return row[1].decode('utf-8'), row[0], 'disk'

    def _check_root(self, ws, ns):
        try:
            info = os.stat(ws.root, follow_symlinks=False)
        except OSError:
            info = None
        if info is None or not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != ws.identity:
            raise self._lost(ws, ns)

    def _lost(self, ws, ns):
        """Forget a root that is gone or replaced; its cache describes nothing current."""
        message = ('The selected workspace was replaced. Start a fresh task.'
                   if os.path.lexists(ws.root) else
                   'The selected project folder no longer exists. Choose a project folder again.')
        self._outlines.drop(ns)
        with self.store.lock:
            if self.store.db.in_transaction:
                return PolicyError(message)
            try:
                _purge(self.store.db, ns)
                self.store.db.commit()
            except BaseException:
                self.store.db.rollback()
                raise
        return PolicyError(message)

    @staticmethod
    def _token(view, overlay, content):
        h = _feed(hashlib.sha256(), view['state'], view['content'] if content else 'paths')
        for name in sorted(overlay):
            _feed(h, name, digest(overlay[name].encode('utf-8')))
        return h.hexdigest()[:32]

    def _mac(self, body):
        return hmac.new(self._key, b'pebble-search-cursor\0' + body, hashlib.sha256).digest()

    def _seal(self, kind, ns, scope_id, query, generation, token, position):
        body = json.dumps({'v': 2, 'k': kind, 'ns': ns, 's': scope_id, 'q': query,
                           'g': generation, 'w': token, 'p': position},
                          ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')
        return f'{_b64(body)}.{_b64(self._mac(body))}'

    def _open(self, cursor, kind, ns, scope_id, query):
        """Authenticate and bind a cursor before any refresh; freshness is checked after."""
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not 0 < len(cursor) <= MAX_CURSOR:
            raise CursorError('Search cursors are the opaque strings returned with a previous page.')
        try:
            body, tag = (_unb64(part) for part in cursor.split('.'))
            if not hmac.compare_digest(tag, self._mac(body)):
                raise ValueError
            data = json.loads(body)
        except ValueError:
            raise CursorError('This search cursor was altered or was not issued by this search index.') from None
        position = data.get('p') if isinstance(data, dict) else None
        if not (isinstance(data, dict) and data.get('v') == 2 and isinstance(position, list)
                and type(data.get('g')) is int and isinstance(data.get('w'), str)):
            raise CursorError('This search cursor is malformed.')
        if data.get('k') != kind:
            raise CursorError('This cursor belongs to a different kind of search.')
        if data.get('ns') != ns:
            raise CursorError('This cursor belongs to another project folder, or the folder was replaced.')
        if data.get('s') != scope_id:
            raise CursorError('This cursor belongs to another conversation or run.')
        if data.get('q') != query:
            raise CursorError('This cursor belongs to a different pattern.')
        shape = (int, int) if kind == 'grep' else (int,)  # glob/search/symbols keep one ordinal
        ceiling = MAX_ENTRIES * 2 if kind in ('glob', 'grep') else RANK_LIMIT
        if (len(position) != len(shape) or any(type(value) is not expected for value, expected in zip(position, shape))
                or not 0 <= position[0] < ceiling or (kind == 'grep' and position[1] < 1)):
            raise CursorError('This search cursor is malformed.')
        return data
