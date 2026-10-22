"""Read-only code search benchmark over supplied real roots: latency, completeness and ranking quality.

Each --root (this checkout, node_modules package trees, ...) is searched in
place and never written. The search cache lives in a scratch home, a new
directory outside every root, removed afterwards unless --keep-scratch. The
JSON report is written only to --out, which must name a new file outside every
root.

Measured per root:
- cold: first SearchFiles-style query on an empty cache (fresh store), repeated
  --cold times, for files and symbols mode;
- revalidation: first query after a restart over the persisted cache (metadata
  walk, persisted bodies reused, outlines parsed again), repeated --restarts times;
- warm: every generated query again on the live index;
- completeness: inventory, skips, parse coverage and complete flags exactly as
  the index reports them, plus private-path violations (must be zero);
- beyond 500 (C05): files after the 500th eligible path, located by a literal
  token from their own text through paged Grep and an exact Glob.

Quality (C06) uses a query set generated mechanically from --query-dir in the
first root. It takes top-level classes and functions whose names have at least
two identifier words and are unique in that directory, spaced evenly by
(path, line). Symbols mode queries the exact name and expects that definition.
Files mode queries the words of the name and expects that file. Both report
MRR@5 and Recall@5. When ts/js definitions exist in the other roots and the
grammars are installed, a symbols-mode set is generated from them too.

Limits: one machine, a few samples and lexical ranking only. No model, task
success or comparison with another tool is measured or implied.
"""

import argparse
import json
import math
import os
import platform
import re
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pebble import code_intelligence, search_index, search_ranking, ts_symbols  # noqa: E402
from pebble.policy import EXCLUDED, MAX_FILE, PRIVATE, PolicyError, Workspace  # noqa: E402
from pebble.search_index import SearchIndex  # noqa: E402
from pebble.store import Store  # noqa: E402

SCHEMA = 'pebble-code-search-benchmark/1'
SCOPE = ('benchmark', 'code-search')
MIN_QUERIES = 40
TOP = 5
BEYOND = 500
TOKEN = re.compile(r'[A-Za-z_][A-Za-z0-9_]{7,63}')
DEFINITION_KINDS = frozenset({'class', 'function', 'async_function'})
TS_KINDS = frozenset({'class', 'function', 'interface', 'struct', 'trait', 'enum', 'type_alias'})


def nearest_rank(values, share):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def summary(samples):
    if not samples:
        return {'samples': 0}
    return {'samples': len(samples), 'p50_ms': round(statistics.median(samples), 3),
            'p95_ms': round(nearest_rank(samples, 0.95), 3), 'max_ms': round(max(samples), 3)}


def timed(call):
    started = time.perf_counter()
    result = call()
    return (time.perf_counter() - started) * 1000, result


def private(path):
    for part in path.split('/'):
        lower = part.lower()
        if lower in PRIVATE or lower.startswith('.env') or lower.endswith(('.pem', '.key', '.p12', '.pfx', '.gguf')):
            return True
    return False


def eligible_files(root, start=None):
    """Relative paths under ``start`` the index could see: no links, hidden, excluded or private folders."""
    base = start or root
    for folder, folders, files in os.walk(base, followlinks=False):
        folders[:] = sorted(name for name in folders if not (name in EXCLUDED or name.startswith('.')
                                                            or name.lower() in PRIVATE)
                            and not os.path.islink(os.path.join(folder, name)))
        for name in sorted(files):
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, root).replace(os.sep, '/')
            if not os.path.islink(path) and not private(relative):
                yield relative


def glob_literal(path):
    """An fnmatch pattern matching exactly ``path``."""
    return ''.join(f'[{char}]' if char in '*?[' else char for char in path)


def returned_paths(result):
    rows = []
    for key in ('untrusted_results', 'untrusted_symbols', 'untrusted_matches'):
        rows.extend(row['path'] for row in result.get(key, []))
    rows.extend(result.get('files', []))
    return rows


# Query generation ----------------------------------------------------------------------------------


def python_queries(ws, query_dir, limit):
    """Evenly spaced unique top-level definitions under ``query_dir`` (relative to the root)."""
    base = (ws.root / query_dir) if query_dir else ws.root
    if not base.is_dir() or base.is_symlink():
        raise SystemExit(f'--query-dir {query_dir!r} is not a folder in the first root')
    seen, candidates = {}, []
    for relative in eligible_files(ws.root, base):
        if not relative.endswith('.py'):
            continue
        try:
            text = ws.snapshot(relative).decode('utf-8')
        except (OSError, UnicodeDecodeError, PolicyError):
            continue
        for symbol in code_intelligence.outline(text).symbols:
            seen[symbol.name] = seen.get(symbol.name, 0) + 1
            if (symbol.depth == 0 and symbol.kind in DEFINITION_KINDS and len(symbol.name) >= 6
                    and not symbol.name.startswith('_') and len(search_ranking.subwords(symbol.name)) >= 2):
                candidates.append((relative, symbol.line, symbol.name, symbol.kind))
    unique = [item for item in candidates if seen[item[2]] == 1]
    return spaced(unique, limit), len(candidates), len(unique)


def ts_queries(ws, limit):
    """Unique top-level ts/js/go/rust declarations in one root, from the same index view."""
    seen, candidates = {}, []
    for relative in eligible_files(ws.root):
        language = ts_symbols.language_for(relative)
        if language is None:
            continue
        try:
            text = ws.snapshot(relative).decode('utf-8')
        except (OSError, UnicodeDecodeError, PolicyError):
            continue
        for symbol in ts_symbols.outline(text, language).symbols:
            seen[symbol.name] = seen.get(symbol.name, 0) + 1
            if symbol.depth == 0 and symbol.kind in TS_KINDS and len(symbol.name) >= 6:
                candidates.append((relative, symbol.line, symbol.name, symbol.kind))
        if len(candidates) > 50 * limit:
            break  # enough to space a sample; the walk below still measures the whole root
    unique = [item for item in candidates if seen[item[2]] == 1]
    return spaced(unique, limit), len(candidates), len(unique)


def spaced(items, limit):
    if len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(index * step)] for index in range(limit)]


def score(index, ws, queries, mode):
    reciprocal, hits, misses = [], 0, []
    for path, line, name, _kind in queries:
        if mode == 'symbols':
            rows = index.symbols(ws, name, scope=SCOPE, limit=TOP)['untrusted_symbols']
            rank = next((row['rank'] for row in rows if row['path'] == path and row['line'] == line), None)
        else:
            query = ' '.join(search_ranking.subwords(name))
            rows = index.search(ws, query, scope=SCOPE, limit=TOP)['untrusted_results']
            rank = next((row['rank'] for row in rows if row['path'] == path), None)
        reciprocal.append(1 / rank if rank and rank <= TOP else 0.0)
        hits += bool(rank and rank <= TOP)
        if not rank and len(misses) < 10:
            misses.append(name)
    count = len(queries) or 1
    return {'queries': len(queries), 'mrr_at_5': round(sum(reciprocal) / count, 4),
            'recall_at_5': round(hits / count, 4), 'first_misses': misses}


# Per-root measurement ------------------------------------------------------------------------------


def inventory(index, ws):
    names, cursor = [], None
    while True:
        page = index.glob(ws, '**', scope=SCOPE, cursor=cursor, limit=search_index.GLOB_LIMIT)
        names.extend(page['files'])
        cursor = page['cursor']
        if cursor is None:
            return names, page['coverage']


def token_of(ws, path):
    """A distinctive literal from the file's own text: its longest identifier-like word of 8+ chars."""
    try:
        text = ws.snapshot(path).decode('utf-8')
    except (OSError, UnicodeDecodeError, PolicyError):
        return None
    best = ''
    for line in text.splitlines()[:400]:
        for word in TOKEN.findall(line):  # a literal that really occurs in one line of the file
            if len(word) > len(best):
                best = word
    return best or None


def beyond_500(index, ws, names, samples):
    eligible = sorted(names)
    tail = eligible[BEYOND:]
    report = {'eligible_paths': len(eligible), 'threshold': BEYOND, 'beyond_threshold': len(tail)}
    if not tail:
        report['note'] = f'fewer than {BEYOND + 1} eligible files; nothing beyond the threshold to find'
        return report
    picks = spaced(tail, samples)
    grep_found = glob_found = probed = 0
    details = []
    for path in picks:
        glob = index.glob(ws, glob_literal(path), scope=SCOPE, limit=1)
        glob_found += glob['files'] == [path]
        token = token_of(ws, path)
        if token is None:
            details.append({'path': path, 'probe': 'no_token'})
            continue
        probed += 1
        found, cursor, pages = False, None, 0
        while not found and pages < 50:
            page = index.grep(ws, token, scope=SCOPE, cursor=cursor, limit=search_index.GREP_LIMIT)
            found = any(match['path'] == path for match in page['untrusted_matches'])
            cursor, pages = page['cursor'], pages + 1
            if cursor is None:
                break
        grep_found += found
        details.append({'path': path, 'position': eligible.index(path) + 1, 'grep_found': found, 'pages': pages})
    report.update(sampled=len(picks), glob_found=glob_found, grep_probed=probed, grep_found=grep_found,
                  details=details)
    return report


def measure(root, scratch, label, args, queries):
    ws = Workspace(root)
    data = scratch / f'{label}-data'
    sample = [name for _path, _line, name, _kind in queries[:max(1, args.cold)]] or ['main']
    words = [' '.join(search_ranking.subwords(name)) or name for name in sample]
    cold_search, cold_symbols, violations = [], [], 0
    for run in range(args.cold):
        for mode, bucket in (('search', cold_search), ('symbols', cold_symbols)):
            store = Store(scratch / f'{label}-cold-{mode}-{run}')
            try:
                index = SearchIndex(store)
                call = (lambda: index.search(ws, words[run % len(words)], scope=SCOPE, limit=TOP)) if mode == 'search' \
                    else (lambda: index.symbols(ws, sample[run % len(sample)], scope=SCOPE, limit=TOP))
                elapsed, result = timed(call)
                bucket.append(elapsed)
                violations += sum(private(path) for path in returned_paths(result))
            finally:
                store.close()
            shutil.rmtree(scratch / f'{label}-cold-{mode}-{run}', ignore_errors=True)
    store = Store(data)
    try:
        index = SearchIndex(store)
        names, glob_coverage = inventory(index, ws)
        violations += sum(private(path) for path in names)
        first = index.search(ws, words[0], scope=SCOPE, limit=TOP)
        symbols_first = index.symbols(ws, sample[0], scope=SCOPE, limit=TOP)
        warm_search, warm_symbols, warm_grep = [], [], []
        for _round in range(args.warm):
            for name in sample + [item[2] for item in queries[len(sample):]]:
                text = ' '.join(search_ranking.subwords(name)) or name
                elapsed, result = timed(lambda: index.search(ws, text, scope=SCOPE, limit=TOP))
                warm_search.append(elapsed)
                violations += sum(private(path) for path in returned_paths(result))
                elapsed, result = timed(lambda: index.symbols(ws, name, scope=SCOPE, limit=TOP))
                warm_symbols.append(elapsed)
                violations += sum(private(path) for path in returned_paths(result))
            elapsed, result = timed(lambda: index.grep(ws, sample[0], scope=SCOPE, limit=search_index.GREP_LIMIT))
            warm_grep.append(elapsed)
        last = index.search(ws, words[0], scope=SCOPE, limit=TOP)
        beyond = beyond_500(index, ws, names, args.beyond_samples)
    finally:
        store.close()
    revalidation, restart_work = [], []
    for _restart in range(args.restarts):
        store = Store(data)
        try:
            index = SearchIndex(store)
            elapsed, result = timed(lambda: index.search(ws, words[0], scope=SCOPE, limit=TOP))
            revalidation.append(elapsed)
            restart_work.append({key: result['coverage']['cache'][key] for key in ('read', 'reused', 'outlines_parsed')})
        finally:
            store.close()
    coverage, symbol_coverage = first['coverage'], symbols_first['coverage']
    completeness = {
        'inventoried': coverage['inventoried'], 'inventory_truncated': coverage['inventory_truncated'],
        'inventory_errors': coverage['inventory_errors'], 'entry_limit': coverage['entry_limit'],
        'searched': coverage['searched'], 'path_only': coverage['path_only'], 'skip_reasons': coverage['skip_reasons'],
        'file_size_limit': MAX_FILE, 'python_parsed': coverage['python_parsed'],
        'python_partial': coverage['python_partial'], 'python_failed': coverage['python_failed'],
        'python_deferred': coverage['python_deferred'], 'scan_complete': coverage['scan_complete'],
        'parse_complete': coverage['parse_complete'],
        'symbols': {key: symbol_coverage.get(key) for key in ('python_files', 'searched', 'skipped', 'prefiltered',
                                                               'parse_complete', 'scan_complete', 'tree_sitter')},
        'glob_complete': glob_coverage['complete'],
        'warm_body_reads_after_cold': last['coverage']['cache']['read'],
    }
    return {
        'label': label, 'path': str(root), 'inventory': len(names), 'completeness': completeness,
        'latency_ms': {'cold_search': summary(cold_search), 'cold_symbols': summary(cold_symbols),
                       'revalidation_search': summary(revalidation), 'warm_search': summary(warm_search),
                       'warm_symbols': summary(warm_symbols), 'warm_grep': summary(warm_grep)},
        'revalidation_work': restart_work, 'beyond_500': beyond, 'private_path_violations': violations,
    }


def guard_paths(roots, out, scratch):
    for root in roots:
        if out == root or out.is_relative_to(root):
            raise SystemExit('--out must be outside every measured root (the benchmark is read-only)')
        if scratch is not None and (scratch == root or scratch.is_relative_to(root) or root.is_relative_to(scratch)):
            raise SystemExit('--scratch must be outside every measured root and must not contain one')
    if out.exists() or out.is_symlink():
        raise SystemExit('--out must name a new file')
    if not out.parent.is_dir():
        raise SystemExit('--out must be in an existing folder')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', action='append', type=Path, required=True,
                        help='folder to search in place (repeatable; the first one supplies --query-dir)')
    parser.add_argument('--out', type=Path, required=True, help='new JSON report file, outside every root')
    parser.add_argument('--query-dir', default='backend/pebble', help='folder in the first root for queries')
    parser.add_argument('--queries', type=int, default=60, help='maximum generated queries (>= 40 to pass)')
    parser.add_argument('--cold', type=int, default=3, help='cold samples per mode (>= 1)')
    parser.add_argument('--warm', type=int, default=2, help='warm rounds over every query (>= 1)')
    parser.add_argument('--restarts', type=int, default=3, help='restart revalidation samples (>= 1)')
    parser.add_argument('--beyond-samples', type=int, default=20, help='files probed beyond the 500th path')
    parser.add_argument('--scratch', type=Path, help='new or empty scratch home (default: a new temp folder)')
    parser.add_argument('--keep-scratch', action='store_true', help='keep the scratch home afterwards')
    args = parser.parse_args(argv)
    if min(args.cold, args.warm, args.restarts) < 1 or args.queries < 1 or args.beyond_samples < 1:
        parser.error('use --cold, --warm, --restarts, --queries and --beyond-samples of at least 1')
    roots = []
    for root in args.root:
        resolved = root.expanduser().resolve(strict=True)
        if not resolved.is_dir():
            parser.error(f'{root} is not a folder')
        roots.append(resolved)
    out = args.out.expanduser().absolute()
    out = out.parent.resolve() / out.name
    scratch = args.scratch.expanduser().resolve() if args.scratch else None
    guard_paths(roots, out, scratch)
    created = scratch is None or not scratch.exists()
    if scratch is None:
        scratch = Path(tempfile.mkdtemp(prefix='pebble-code-bench-')).resolve()
        guard_paths(roots, out, scratch)
    elif scratch.exists() and any(scratch.iterdir()):
        parser.error('--scratch must be a new or empty folder')
    scratch.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        first = Workspace(roots[0])
        queries, candidates, unique = python_queries(first, args.query_dir, args.queries)
        generated = {}  # other roots: ts/js/go/rust definitions, when the grammars are installed
        if ts_symbols.available():
            for number, root in enumerate(roots[1:], 2):
                generated[number] = ts_queries(Workspace(root), args.queries)
        measured = []
        for number, root in enumerate(roots, 1):
            root_queries = queries if number == 1 else generated.get(number, ([], 0, 0))[0]
            measured.append(measure(root, scratch, f'root-{number}', args, root_queries))
        store = Store(scratch / 'quality-data')
        try:
            index = SearchIndex(store)
            quality = {'root': 'root-1', 'query_dir': args.query_dir, 'candidates': candidates,
                       'unique_candidates': unique, 'queries': len(queries),
                       'rules': ('top-level class/function, name >= 6 chars, >= 2 identifier words, not private, '
                                 'unique under the query dir; evenly spaced by (path, line)'),
                       'symbols': score(index, first, queries, 'symbols'),
                       'files': score(index, first, queries, 'files')}
        finally:
            store.close()
        ts_quality = []
        for number, (found, total, distinct) in sorted(generated.items()):
            if not found:
                continue
            store = Store(scratch / f'ts-quality-{number}')
            try:
                ts_quality.append({'root': f'root-{number}', 'candidates_seen': total, 'unique_candidates': distinct,
                                   'symbols': score(SearchIndex(store), Workspace(roots[number - 1]), found,
                                                    'symbols')})
            finally:
                store.close()
    finally:
        if created and not args.keep_scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    probe = ts_symbols.probe()
    beyond_ok = all(item['beyond_500'].get('grep_found', 0) == item['beyond_500'].get('grep_probed', 0)
                    and item['beyond_500'].get('glob_found', 0) == item['beyond_500'].get('sampled', 0)
                    for item in measured)
    report = {
        'schema': SCHEMA, 'date': datetime.now(timezone.utc).isoformat(),
        'host': {'platform': platform.platform(), 'python': platform.python_version()},
        'settings': {'cold': args.cold, 'warm_rounds': args.warm, 'restarts': args.restarts,
                     'beyond_samples': args.beyond_samples, 'top': TOP, 'entry_limit': search_index.MAX_ENTRIES,
                     'file_size_limit': MAX_FILE, 'parse_budget': search_index.PARSE_BUDGET,
                     'rank_limit': search_index.RANK_LIMIT, 'ranking': search_ranking.fingerprint(),
                     'python_outline': code_intelligence.fingerprint()},
        'tree_sitter': {'available': probe.available, 'package': probe.package, 'tree_sitter': probe.tree_sitter,
                        'languages': probe.languages, 'reason': probe.reason},
        'roots': measured, 'quality': quality, 'ts_quality': ts_quality,
        'elapsed_s': round(time.perf_counter() - started, 3),
        'passed': (len(queries) >= MIN_QUERIES and beyond_ok
                   and all(item['private_path_violations'] == 0 for item in measured)),
        'definitions': {
            'cold': 'first query on a fresh store in the scratch home (walk, read and parse everything needed)',
            'revalidation': 'first query after reopening the persisted store: metadata walk, persisted bodies '
                            'reused, outlines parsed again (the in-memory outline cache starts empty)',
            'warm': 'repeated queries on the live index', 'p95': 'nearest rank'},
        'limits': ('Read-only measurements on this machine with few samples; lexical and outline ranking only. '
                   'Excluded folders (node_modules, dist, build, hidden, private) inside a root are not searched by '
                   'design. No model, task success or comparison with any other tool is measured or implied.'),
    }
    with open(out, 'x', encoding='utf-8') as handle:  # never overwrite
        json.dump(report, handle, indent=2)
        handle.write('\n')
    print(json.dumps({'out': str(out), 'passed': report['passed'], 'queries': len(queries),
                      'symbols_mrr_at_5': quality['symbols']['mrr_at_5'],
                      'files_mrr_at_5': quality['files']['mrr_at_5']}))
    return report


if __name__ == '__main__':
    main()
