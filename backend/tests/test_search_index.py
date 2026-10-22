"""SearchIndex: bounded incremental literal Grep/Glob over the shared store."""

import hashlib
import json
import os
import shutil
import threading
import time

import pytest

from pebble import search_index
from pebble.policy import PolicyError, Workspace
from pebble.search_index import CursorError, SearchIndex
from pebble.store import Store

SCOPE = ('chat-a', 'run-1')


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Production re-verifies bodies changed within 2 s of indexing; tests settle past 10 ms.
    monkeypatch.setattr(search_index, 'RACY_NS', 10_000_000)
    root = tmp_path / 'project'
    root.mkdir()
    store = Store(tmp_path / 'data')
    yield root, store, SearchIndex(store)
    store.close()


def settle():
    time.sleep(0.05)


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def reads(monkeypatch, ws):
    seen = []
    original = ws.snapshot

    def snapshot(name, missing_ok=False):
        seen.append(name)
        return original(name, missing_ok=missing_ok)

    monkeypatch.setattr(ws, 'snapshot', snapshot)
    return seen


def never():
    raise AssertionError('refresh started before the request was validated')


def lines(result):
    return [(m['path'], m['line']) for m in result['untrusted_matches']]


def test_grep_and_glob_reach_past_old_500_and_1500_file_prefixes(env):
    root, _store, index = env
    for i in range(1601):
        (root / f'{i:04d}.txt').write_text('needle\n' if i in (600, 1550) else 'ordinary\n')
    ws = Workspace(root)
    result = index.grep(ws, 'needle', scope=SCOPE)
    assert lines(result) == [('0600.txt', 1), ('1550.txt', 1)]
    coverage = result['coverage']
    assert coverage['inventoried'] == 1601 and coverage['searched'] == 1601
    assert coverage['scan_complete'] and coverage['complete'] and result['cursor'] is None
    found = index.glob(ws, '*1550*', scope=SCOPE)
    assert found['files'] == ['1550.txt'] and found['coverage']['complete']


def test_glob_pages_cover_every_file_once_in_path_order(env):
    root, _store, index = env
    (root / 'src').mkdir()
    for i in range(1203):
        (root / 'src' / f'm{i:04d}.py').write_text('x')
    ws = Workspace(root)
    pages, cursor = [], None
    while True:
        result = index.glob(ws, '**/*.py', scope=SCOPE, cursor=cursor, limit=500)
        pages.append(result['files'])
        cursor = result['cursor']
        if cursor is None:
            break
        assert result['coverage']['scan_complete'] and not result['coverage']['complete']
    flat = [name for page in pages for name in page]
    assert [len(page) for page in pages] == [500, 500, 203]
    assert flat == sorted(flat) and len(set(flat)) == 1203
    assert result['coverage']['complete'] and not result['coverage']['inventory_truncated']


def test_grep_cursor_resumes_inside_a_file_without_skipping_lines(env):
    root, _store, index = env
    (root / 'many.txt').write_text(''.join(f'needle {n}\nfiller\n' for n in range(75)))
    (root / 'next.txt').write_text('needle tail\n')
    ws = Workspace(root)
    first = index.grep(ws, 'needle', scope=SCOPE)
    coverage = first['coverage']
    assert len(first['untrusted_matches']) == 30 and first['cursor']
    # The file scan finished; only the output page is partial.
    assert coverage['scan_complete'] and coverage['matches_truncated'] and not coverage['complete']
    seen, cursor = list(first['untrusted_matches']), first['cursor']
    while cursor:
        page = index.grep(ws, 'needle', scope=SCOPE, cursor=cursor)
        seen += page['untrusted_matches']
        cursor = page['cursor']
    assert [(m['path'], m['line']) for m in seen] == (
        [('many.txt', 2 * n + 1) for n in range(75)] + [('next.txt', 1)])
    assert page['coverage']['complete'] and page['coverage']['resumed']


def test_replaying_a_page_returns_identical_evidence(env):
    root, _store, index = env
    (root / 'a.txt').write_text('needle\n' * 5)
    ws = Workspace(root)
    first = index.grep(ws, 'needle', scope=SCOPE, limit=2)
    second = index.grep(ws, 'needle', scope=SCOPE, cursor=first['cursor'], limit=2)
    replay = index.grep(ws, 'needle', scope=SCOPE, cursor=first['cursor'], limit=2)
    assert replay['untrusted_matches'] == second['untrusted_matches']
    assert replay['cursor'] == second['cursor']
    assert [m['line'] for m in second['untrusted_matches']] == [3, 4]
    assert {(m['basis'], m['file_hash']) for m in second['untrusted_matches']} == {
        ('disk', sha('needle\n' * 5))}


def test_tampered_or_malformed_cursors_are_rejected_before_refresh(env):
    root, _store, index = env
    (root / 'a.txt').write_text('needle\n' * 3)
    ws = Workspace(root)
    cursor = index.grep(ws, 'needle', scope=SCOPE, limit=1)['cursor']
    body, tag = cursor.split('.')
    middle = len(body) // 2
    flipped = body[:middle] + ('A' if body[middle] != 'A' else 'B') + body[middle + 1:]
    for bad in (f'{flipped}.{tag}', f'{body}.{tag[::-1]}', 'garbage', '.', '', 'x' * 5000, 'é.é',
                7, b'bytes', ['list']):
        with pytest.raises(CursorError):
            index.grep(ws, 'needle', scope=SCOPE, cursor=bad, cancel=never)
    assert index.grep(ws, 'needle', scope=SCOPE, cursor=cursor, limit=1)['untrusted_matches'][0]['line'] == 2


def test_cursor_is_bound_to_scope_pattern_kind_root_and_store_key(env, tmp_path):
    root, _store, index = env
    (root / 'a.txt').write_text('needle\n' * 3)
    ws = Workspace(root)
    cursor = index.grep(ws, 'needle', scope=SCOPE, limit=1)['cursor']
    for scope in (('chat-b', 'run-1'), ('chat-a', 'run-2')):
        with pytest.raises(CursorError, match='another conversation or run'):
            index.grep(ws, 'needle', scope=scope, cursor=cursor, cancel=never)
    with pytest.raises(CursorError, match='different pattern'):
        index.grep(ws, 'needl', scope=SCOPE, cursor=cursor, cancel=never)
    with pytest.raises(CursorError, match='different kind'):
        index.glob(ws, 'needle', scope=SCOPE, cursor=cursor, cancel=never)
    other_root = tmp_path / 'other'
    other_root.mkdir()
    (other_root / 'a.txt').write_text('needle\n' * 3)
    with pytest.raises(CursorError, match='another project folder'):
        index.grep(Workspace(other_root), 'needle', scope=SCOPE, cursor=cursor, cancel=never)
    other_store = Store(tmp_path / 'other-data')
    try:
        with pytest.raises(CursorError, match='altered'):
            SearchIndex(other_store).grep(ws, 'needle', scope=SCOPE, cursor=cursor, cancel=never)
    finally:
        other_store.close()


def test_filesystem_and_draft_changes_make_cursors_stale(env):
    root, _store, index = env
    (root / 'a.txt').write_text('needle\n' * 3)
    (root / 'b.txt').write_text('other\n')
    ws = Workspace(root)
    cursor = index.grep(ws, 'needle', scope=SCOPE, limit=1)['cursor']
    (root / 'b.txt').write_text('changed elsewhere\n')
    with pytest.raises(CursorError, match='changed since'):
        index.grep(ws, 'needle', scope=SCOPE, cursor=cursor, limit=1)
    cursor = index.grep(ws, 'needle', scope=SCOPE, limit=1)['cursor']
    ws.draft('c.txt', 'staged\n')
    with pytest.raises(CursorError, match='changed since'):
        index.grep(ws, 'needle', scope=SCOPE, cursor=cursor, limit=1)
    glob_cursor = index.glob(ws, '*.txt', scope=SCOPE, limit=1)['cursor']
    (root / 'd.txt').write_text('new file\n')
    with pytest.raises(CursorError, match='changed since'):
        index.glob(ws, '*.txt', scope=SCOPE, cursor=glob_cursor, limit=1)


def test_same_size_and_mtime_but_new_ctime_is_reread(env, monkeypatch):
    root, _store, index = env
    target = root / 'config.py'
    target.write_bytes(b'value = 1\n')
    settle()
    ws = Workspace(root)
    assert index.grep(ws, 'value = 2', scope=SCOPE)['untrusted_matches'] == []
    before = os.stat(target)
    settle()
    with open(target, 'r+b') as handle:  # same inode and size
        handle.write(b'value = 2\n')
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(target)
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert after.st_ctime_ns != before.st_ctime_ns
    settle()
    seen = reads(monkeypatch, ws)
    result = index.grep(ws, 'value = 2', scope=SCOPE)
    assert lines(result) == [('config.py', 1)] and seen == ['config.py']
    assert result['untrusted_matches'][0]['file_hash'] == sha(b'value = 2\n')


def test_bodies_indexed_right_after_a_change_are_reverified(env, monkeypatch):
    root, _store, index = env
    monkeypatch.setattr(search_index, 'RACY_NS', 60_000_000_000)  # everything counts as recent
    (root / 'a.txt').write_text('needle\n' * 3)
    ws = Workspace(root)
    seen = reads(monkeypatch, ws)
    first = index.grep(ws, 'needle', scope=SCOPE, limit=1)
    second = index.grep(ws, 'needle', scope=SCOPE, cursor=first['cursor'], limit=1)
    assert seen == ['a.txt', 'a.txt']
    # Verified-unchanged content keeps the generation and the cursor valid.
    assert second['untrusted_matches'][0]['line'] == 2
    assert second['coverage']['cache']['generation'] == first['coverage']['cache']['generation']


def test_warm_refresh_does_not_reread_unchanged_sources(env, monkeypatch):
    root, _store, index = env
    for i in range(50):
        (root / f'{i:02d}.py').write_text(f'value_{i} = {i}\n')
    settle()
    ws = Workspace(root)
    seen = reads(monkeypatch, ws)
    cold = index.grep(ws, 'value_49', scope=SCOPE)
    assert len(seen) == 50 and cold['coverage']['cache']['read'] == 50
    seen.clear()
    warm = index.grep(ws, 'value_49', scope=SCOPE)
    assert seen == [] and warm['coverage']['cache'] == {'generation': 1, 'reused': 50, 'read': 0}
    assert warm['untrusted_matches'] == cold['untrusted_matches']
    assert index.glob(ws, '*.py', scope=SCOPE)['coverage']['complete'] and seen == []
    (root / '07.py').write_text('value_7 = "changed"\n')
    settle()
    changed = index.grep(ws, 'changed', scope=SCOPE)
    assert seen == ['07.py'] and lines(changed) == [('07.py', 1)]
    assert changed['coverage']['cache']['generation'] == 2


def test_restart_reuses_persisted_index_and_cursor_key(env, tmp_path, monkeypatch):
    root, store, index = env
    (root / 'a.txt').write_text('needle\n' * 3)
    settle()
    ws = Workspace(root)
    first = index.grep(ws, 'needle', scope=SCOPE, limit=1)
    store.close()
    store = Store(tmp_path / 'data')  # normal service startup over the existing database
    try:
        assert store.query('SELECT count(*) AS n FROM chats')[0]['n'] == 0
        restarted = SearchIndex(store)
        assert restarted.describe(ws)['generation'] == first['coverage']['cache']['generation']
        seen = reads(monkeypatch, ws)
        resumed = restarted.grep(ws, 'needle', scope=SCOPE, cursor=first['cursor'], limit=1)
        assert seen == [] and lines(resumed) == [('a.txt', 2)]
    finally:
        store.close()


def test_replaced_root_is_rejected_and_old_cursors_do_not_cross_roots(env, tmp_path):
    root, store, index = env
    (root / 'a.txt').write_text('needle\n' * 3)
    ws = Workspace(root)
    cursor = index.grep(ws, 'needle', scope=SCOPE, limit=1)['cursor']
    root.rename(tmp_path / 'moved')
    root.mkdir()
    (root / 'a.txt').write_text('needle\n' * 3)
    with pytest.raises(PolicyError, match='replaced'):
        index.grep(ws, 'needle', scope=SCOPE, cursor=cursor, limit=1)
    assert index.describe(ws) is None
    fresh = Workspace(root)
    with pytest.raises(CursorError, match='another project folder'):
        index.grep(fresh, 'needle', scope=SCOPE, cursor=cursor, limit=1)
    assert len(index.grep(fresh, 'needle', scope=SCOPE)['untrusted_matches']) == 3
    assert store.query('SELECT count(*) AS n FROM search_index_roots')[0]['n'] == 1


def test_deleted_root_fails_honestly_and_purges_its_cache(env):
    root, store, index = env
    (root / 'a.txt').write_text('needle\n')
    ws = Workspace(root)
    index.grep(ws, 'needle', scope=SCOPE)
    assert index.describe(ws)['inventoried'] == 1
    shutil.rmtree(root)
    with pytest.raises(PolicyError, match='no longer exists'):
        index.glob(ws, '*', scope=SCOPE)
    assert index.describe(ws) is None
    assert store.query('SELECT count(*) AS n FROM search_index_files')[0]['n'] == 0


def test_staged_drafts_are_overlaid_per_query_and_never_stored(env):
    root, store, index = env
    (root / 'app.py').write_text('old = 1\n')
    ws = Workspace(root)
    index.grep(ws, 'needle', scope=SCOPE)  # warm the shared index with the disk view
    ws.draft('app.py', 'needle = 1\n')
    ws.draft('pkg/new.py', 'needle = 2\n')
    result = index.grep(ws, 'needle', scope=SCOPE)
    assert [(m['path'], m['basis'], m['file_hash']) for m in result['untrusted_matches']] == [
        ('app.py', 'draft', sha('needle = 1\n')), ('pkg/new.py', 'draft', sha('needle = 2\n'))]
    assert result['coverage']['drafts_overlaid'] == 2 and result['coverage']['complete']
    found = index.glob(ws, '**/*.py', scope=SCOPE)
    assert found['files'] == ['app.py', 'pkg/new.py'] and found['coverage']['staged'] == ['pkg/new.py']
    assert store.query('SELECT count(*) AS n FROM search_index_files WHERE instr(body, ?) > 0',
                       (b'needle',))[0]['n'] == 0
    assert store.query("SELECT count(*) AS n FROM search_index_files WHERE path='pkg/new.py'")[0]['n'] == 0
    other = Workspace(root)  # another conversation on the same root sees only disk
    plain = index.grep(other, 'needle', scope=('chat-b', 'run-1'))
    assert plain['untrusted_matches'] == [] and plain['coverage']['complete']
    assert index.glob(other, '**/*.py', scope=('chat-b', 'run-1'))['files'] == ['app.py']


def test_draft_with_stale_baseline_is_dropped_through_workspace_read(env):
    root, _store, index = env
    (root / 'app.py').write_text('old = 1\n')
    ws = Workspace(root)
    ws.draft('app.py', 'needle = 1\n')
    (root / 'app.py').write_text('disk = needle\n')
    result = index.grep(ws, 'needle', scope=SCOPE)
    assert [(m['text'], m['basis']) for m in result['untrusted_matches']] == [('disk = needle', 'disk')]
    assert 'app.py' not in ws.drafts and result['coverage']['drafts_overlaid'] == 0


def test_private_linked_binary_and_large_files_are_never_presented_as_searched(env, tmp_path, monkeypatch):
    root, store, index = env
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'leak.txt').write_text('needle outside\n')
    (root / 'ok.txt').write_text('needle\n')
    for name in ('.env', 'id.pem', 'weights.gguf'):
        (root / name).write_text('needle SECRET\n')
    for folder in ('secrets', 'models', 'node_modules', '.git'):
        (root / folder).mkdir()
        (root / folder / 'x.txt').write_text('needle SECRET\n')
    (root / 'link.txt').symlink_to(outside / 'leak.txt')
    (root / 'linkdir').symlink_to(outside, target_is_directory=True)
    (root / 'blob.bin').write_bytes(b'\xff\xfe needle')
    (root / 'big.txt').write_text('needle\n' + 'x' * 100_000)
    ws = Workspace(root)
    seen = reads(monkeypatch, ws)
    result = index.grep(ws, 'needle', scope=SCOPE)
    assert lines(result) == [('ok.txt', 1)] and sorted(seen) == ['blob.bin', 'ok.txt']
    coverage = result['coverage']
    assert coverage['skip_reasons'] == {'binary': 1, 'large': 1} and coverage['skipped'] == 2
    assert coverage['scan_complete'] is False and coverage['complete'] is False
    assert 'private paths omitted' in coverage['scope']
    assert index.glob(ws, '*', scope=SCOPE)['files'] == ['big.txt', 'blob.bin', 'ok.txt']
    for marker in (b'SECRET', b'outside'):
        assert store.query('SELECT count(*) AS n FROM search_index_files WHERE instr(body, ?) > 0',
                           (marker,))[0]['n'] == 0


def test_cancelled_refresh_writes_nothing_and_cannot_bless_old_data(env):
    root, _store, index = env
    for i in range(5):
        (root / f'{i}.txt').write_text(f'line {i}\n')
    ws = Workspace(root)
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        index.grep(ws, 'line', scope=SCOPE, cancel=stop)
    calls = []

    def later():
        calls.append(1)
        return len(calls) > 3  # stops after the refresh has started reading

    with pytest.raises(InterruptedError):
        index.grep(ws, 'line', scope=SCOPE, cancel=later)
    assert index.describe(ws) is None
    settle()
    assert len(index.grep(ws, 'line', scope=SCOPE)['untrusted_matches']) == 5
    generation = index.describe(ws)['generation']
    (root / '3.txt').write_text('fresh 3\n')
    calls.clear()
    with pytest.raises(InterruptedError):
        index.grep(ws, 'fresh', scope=SCOPE, cancel=later)
    assert index.describe(ws)['generation'] == generation
    result = index.grep(ws, 'fresh', scope=SCOPE)
    assert lines(result) == [('3.txt', 1)] and result['coverage']['cache']['generation'] == generation + 1


def test_entry_limit_reports_partial_traversal_without_claiming_absence(env, monkeypatch):
    root, _store, index = env
    monkeypatch.setattr(search_index, 'MAX_ENTRIES', 5)
    for i in range(10):
        (root / f'{i}.txt').write_text('needle\n')
    ws = Workspace(root)
    result = index.grep(ws, 'needle', scope=SCOPE)
    coverage = result['coverage']
    assert len(result['untrusted_matches']) == 5 and result['cursor'] is None
    assert coverage['inventory_truncated'] and coverage['inventoried'] == 5
    assert not coverage['scan_complete'] and not coverage['complete']
    absent = index.grep(ws, 'absent', scope=SCOPE)
    assert absent['untrusted_matches'] == [] and absent['coverage']['complete'] is False
    found = index.glob(ws, '*.txt', scope=SCOPE)
    assert len(found['files']) == 5 and found['cursor'] is None and not found['coverage']['complete']


def test_text_and_read_budgets_are_reported_and_progress_is_kept(env, monkeypatch):
    root, _store, index = env
    for name in 'abcdef':
        (root / f'{name}.txt').write_text(f'needle {name}\n' + 'x' * 991)  # 1000 bytes each
    settle()
    ws = Workspace(root)
    monkeypatch.setattr(search_index, 'TEXT_BUDGET', 2500)
    limited = index.grep(ws, 'needle', scope=SCOPE)
    assert [m['path'] for m in limited['untrusted_matches']] == ['a.txt', 'b.txt']
    assert limited['coverage']['skip_reasons'] == {'budget': 4} and limited['coverage']['indexed_bytes'] == 2000
    assert not limited['coverage']['complete']
    monkeypatch.setattr(search_index, 'TEXT_BUDGET', 64 * 1024 * 1024)
    monkeypatch.setattr(search_index, 'READ_BUDGET', 2500)
    first = index.grep(ws, 'needle', scope=SCOPE)
    assert [m['path'] for m in first['untrusted_matches']] == ['a.txt', 'b.txt', 'c.txt', 'd.txt']
    assert first['coverage']['skip_reasons'] == {'deferred': 2} and not first['coverage']['complete']
    second = index.grep(ws, 'needle', scope=SCOPE)
    assert len(second['untrusted_matches']) == 6 and second['coverage']['complete']
    assert second['coverage']['cache']['read'] == 2


def test_unicode_and_crlf_are_matched_literally_and_preserved(env):
    root, store, index = env
    raw = 'naïve café\r\nGrüße 👋 wave\r\nplain\r\n'.encode()
    (root / 'u.txt').write_bytes(raw)
    ws = Workspace(root)
    hit = index.grep(ws, 'café', scope=SCOPE)['untrusted_matches']
    assert [(m['line'], m['column'], m['text']) for m in hit] == [(1, 7, 'naïve café')]
    assert hit[0]['file_hash'] == sha(raw)
    assert lines(index.grep(ws, 'ü', scope=SCOPE)) == [('u.txt', 2)]
    assert index.grep(ws, '👋', scope=SCOPE)['untrusted_matches'][0]['column'] == 7
    for absent in ('CAFÉ', 'café', 'cafe'):
        result = index.grep(ws, absent, scope=SCOPE)
        assert result['untrusted_matches'] == [] and result['coverage']['complete']
    with pytest.raises(PolicyError):
        index.grep(ws, 'café\r', scope=SCOPE)
    assert store.query("SELECT body FROM search_index_files WHERE path='u.txt'")[0]['body'] == raw


def test_long_lines_return_a_bounded_window_around_the_match(env):
    root, _store, index = env
    (root / 'min.js').write_text('a' * 5000 + 'needle' + 'b' * 5000 + '\n')
    match = index.grep(Workspace(root), 'needle', scope=SCOPE)['untrusted_matches'][0]
    assert len(match['text']) == 250 and 'needle' in match['text'] and match['text_truncated']
    assert match['column'] == 5001 and match['text'][match['column'] - match['text_start']:].startswith('needle')


def test_matches_are_ordered_by_relative_path_then_line(env):
    root, _store, index = env
    (root / 'a').mkdir()
    for name in ('a/b.txt', 'a-b.txt', 'a.txt', 'B.txt'):
        (root / name).write_text('needle 1\nskip\nneedle 3\n')
    result = index.grep(Workspace(root), 'needle', scope=SCOPE)
    assert lines(result) == [(path, n) for path in ('B.txt', 'a-b.txt', 'a.txt', 'a/b.txt') for n in (1, 3)]


def test_request_bounds_are_validated_before_any_refresh(env):
    root, _store, index = env
    ws = Workspace(root)
    for pattern in ('', 'a\nb', 'x' * 257, 7, None, '\ud800'):
        with pytest.raises(PolicyError):
            index.grep(ws, pattern, scope=SCOPE, cancel=never)
    for limit in (0, 101, True, '5', 2.0):
        with pytest.raises(PolicyError):
            index.grep(ws, 'needle', scope=SCOPE, limit=limit, cancel=never)
    for limit in (0, 501):
        with pytest.raises(PolicyError):
            index.glob(ws, '*', scope=SCOPE, limit=limit, cancel=never)
    for scope in (None, '', [], ('', None), (1.5,), {'chat': 'a'}, 'x' * 2000):
        with pytest.raises(PolicyError):
            index.grep(ws, 'needle', scope=scope, cancel=never)
    with pytest.raises(PolicyError):
        index.glob(ws, '', scope=SCOPE, cancel=never)
    with pytest.raises(TypeError):
        index.grep(ws, 'needle', scope=SCOPE, cancel='stop')
    assert index.describe(ws) is None


def test_describe_reports_content_free_metadata(env):
    root, _store, index = env
    body = 'token = "BODY_MARKER"\n'
    (root / 'secret_name_marker.py').write_text(body)
    ws = Workspace(root)
    assert index.describe(ws) is None
    index.grep(ws, 'BODY', scope=SCOPE)
    info = index.describe(ws)
    text = json.dumps(info)
    assert 'BODY_MARKER' not in text and 'secret_name_marker' not in text and str(root) not in text
    assert info['inventoried'] == 1 and info['indexed_bytes'] == len(body) and info['generation'] == 1


def test_cached_roots_are_bounded_least_recently_used(env, tmp_path, monkeypatch):
    _root, _store, index = env
    monkeypatch.setattr(search_index, 'MAX_ROOTS', 2)
    spaces = []
    for name in ('one', 'two', 'three'):
        folder = tmp_path / name
        folder.mkdir()
        (folder / 'a.txt').write_text(name)
        spaces.append(Workspace(folder))
        index.glob(spaces[-1], '*', scope=SCOPE)
    assert index.describe(spaces[0]) is None
    assert index.describe(spaces[1]) is not None and index.describe(spaces[2]) is not None


# Phase 15 C05/C06: scripts/benchmark_code_search.py over supplied roots, read-only.

def _benchmark():
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'benchmark_code_search.py'
    spec = importlib.util.spec_from_file_location('benchmark_code_search_under_test', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state(*roots):
    return {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
            for root in roots for path in sorted(root.rglob('*')) if path.is_file()}


def test_code_search_benchmark_is_read_only_and_scores_generated_queries(tmp_path):
    bench = _benchmark()
    checkout, packages = tmp_path / 'checkout', tmp_path / 'packages' / 'widgets'
    for i in range(45):
        (checkout / 'pkg').mkdir(parents=True, exist_ok=True)
        (checkout / f'pkg/mod_{i:02d}.py').write_text(
            f'"""Module {i}."""\n\n\ndef ledger_total_{i:02d}(value):\n    return value + {i}\n\n\n'
            f'class ReportBuilder{i:02d}:\n    pass\n')
    (checkout / 'notes').mkdir()
    for i in range(470):
        (checkout / f'notes/f{i:03d}.txt').write_text(f'fillertoken{i:03d}xx plain words\n')
    (checkout / '.env').write_text('SECRET=ledger_total_00\n')
    (checkout / 'secrets').mkdir()
    (checkout / 'secrets/key.txt').write_text('ledger_total_00\n')
    packages.mkdir(parents=True)
    (packages / 'index.ts').write_text('export class LedgerWidget {}\nexport function renderWidget() {}\n')
    (packages / 'node_modules').mkdir()
    (packages / 'node_modules/nested.ts').write_text('export class NestedHidden {}\n')
    before = _state(checkout, packages)
    out, scratch = tmp_path / 'reports' / 'code-search.json', tmp_path / 'scratch'
    out.parent.mkdir()
    args = ['--root', str(checkout), '--root', str(packages), '--out', str(out), '--query-dir', 'pkg',
            '--queries', '60', '--cold', '1', '--warm', '1', '--restarts', '2', '--beyond-samples', '4',
            '--scratch', str(scratch)]
    report = bench.main(args)
    assert json.loads(out.read_text()) == json.loads(json.dumps(report))
    assert _state(checkout, packages) == before and not scratch.exists()  # roots untouched, scratch removed
    assert report['schema'] == 'pebble-code-search-benchmark/1' and report['passed']
    quality = report['quality']
    assert quality['queries'] == 60 and quality['unique_candidates'] == 90
    assert quality['symbols']['mrr_at_5'] == 1.0 and quality['symbols']['recall_at_5'] == 1.0
    assert quality['files']['recall_at_5'] >= 0.9
    first, second = report['roots']
    assert first['inventory'] == 515 and first['private_path_violations'] == 0
    for key in ('cold_search', 'cold_symbols', 'revalidation_search', 'warm_search', 'warm_symbols', 'warm_grep'):
        assert first['latency_ms'][key]['samples'] >= 1 and first['latency_ms'][key]['p95_ms'] >= 0
    assert first['latency_ms']['revalidation_search']['samples'] == 2
    assert all(work['read'] == 0 for work in first['revalidation_work'])  # persisted bodies reused after restart
    beyond = first['beyond_500']
    assert beyond['beyond_threshold'] == 15 and beyond['sampled'] == 4
    assert beyond['glob_found'] == 4 and beyond['grep_found'] == beyond['grep_probed'] == 4
    assert all(item['position'] > 500 for item in beyond['details'])
    assert first['completeness']['scan_complete'] and first['completeness']['inventory_truncated'] is False
    assert second['inventory'] == 1  # node_modules inside a root stays excluded by policy
    assert 'fewer than 501' in second['beyond_500']['note']
    if report['tree_sitter']['available']:
        assert report['ts_quality'][0]['symbols']['recall_at_5'] == 1.0
    with pytest.raises(SystemExit, match='new file'):
        bench.main(args)  # never overwrites a report
    with pytest.raises(SystemExit, match='outside every measured root'):
        bench.main(['--root', str(checkout), '--out', str(checkout / 'report.json'), '--query-dir', 'pkg'])
    with pytest.raises(SystemExit, match='scratch'):
        bench.main(['--root', str(checkout), '--out', str(tmp_path / 'other.json'), '--query-dir', 'pkg',
                    '--scratch', str(checkout / 'cache')])
    assert _state(checkout, packages) == before
