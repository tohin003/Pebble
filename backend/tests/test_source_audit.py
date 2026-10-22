"""Settlement source audit: bounded, content-free, latest read only."""

import hashlib
import json
import re

import pytest

from pebble import source_audit
from pebble.handoff import HandoffIntegrityError, make_handoff
from pebble.source_audit import (
    OVER_BUDGET, SourceAuditError, audit, completion_issues, guard_summary, path_id, reads_from_witnesses,
    validate,
)

RUN = 'run-audit'
ROOT = (16777220, 4242)
SECRET_PATH = 'src/PATH-SENTINEL-secret.py'
SECRET_TEXT = b'CONTENT-SENTINEL def handler(): pass'


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Disk:
    """A fake confined file view keyed by path ID, recording every call."""

    def __init__(self, files):
        self.files, self.calls = dict(files), []

    def __call__(self, read, remaining):
        self.calls.append((read['path_id'], remaining))
        value = self.files.get(read['path_id'], 'missing')
        if value == 'missing':
            return None
        if isinstance(value, Exception):
            raise value
        if len(value) > remaining:
            return OVER_BUDGET
        return digest(value), len(value)


def read(name, data, sequence, root=ROOT):
    return {'path_id': path_id(RUN, root, name), 'root': list(root),
            'hash': None if data is None else digest(data), 'sequence': sequence}


def states(result):
    return {e['path_id']: e['state'] for e in result['entries']}


def test_changed_deleted_unreadable_and_unchanged_files_are_classified():
    reads = [read('a.py', b'one', 1), read('b.py', b'two', 2), read('c.py', SECRET_TEXT, 3),
             read('d.py', b'four', 4), read('new.py', None, 5)]
    ids = [r['path_id'] for r in reads]
    disk = Disk({ids[0]: b'one', ids[1]: b'two changed', ids[3]: PermissionError('denied')})
    result = audit(reads, disk, max_files=10, max_bytes=1000)
    assert states(result) == {ids[0]: 'unchanged', ids[1]: 'changed', ids[2]: 'deleted',
                              ids[3]: 'unreadable', ids[4]: 'unchanged'}
    assert result['counts'] == {'unchanged': 2, 'changed': 1, 'deleted': 1, 'unreadable': 1, 'skipped_budget': 0}
    assert result['complete'] is True and result['checked'] == 5 and result['authorization'] == 'none'
    assert sorted(result['stale']) == sorted([ids[1], ids[2], ids[3]])
    assert [c[0] for c in disk.calls] == ids[::-1]  # Newest read first.


def test_results_are_content_free_ids_only():
    reads = [read(SECRET_PATH, SECRET_TEXT, 1)]
    result = audit(reads, Disk({reads[0]['path_id']: SECRET_TEXT + b'!'}), max_files=4, max_bytes=1000)
    text = json.dumps(result)
    for leak in ('PATH-SENTINEL', 'CONTENT-SENTINEL', digest(SECRET_TEXT), digest(SECRET_TEXT + b'!'), RUN):
        assert leak not in text
    assert set(result['entries'][0]) == {'path_id', 'state'}
    assert result['entries'][0]['path_id'].startswith('p:') and len(result['entries'][0]['path_id']) == 26
    assert all(e['state'] in source_audit.STATES for e in result['entries'])

    def labels(value):
        if isinstance(value, dict):
            return all(labels(k) and labels(v) for k, v in value.items())
        if isinstance(value, list):
            return all(labels(v) for v in value)
        if isinstance(value, str):
            return bool(re.fullmatch(r'p:[0-9a-f]{24}|[a-z_]+', value))
        return value is None or isinstance(value, (bool, int))
    assert labels(result)


def test_file_and_byte_budgets_skip_explicitly_and_never_claim_fresh():
    reads = [read(f'f{i}.py', b'x' * 100, i) for i in range(1, 6)]
    files = {r['path_id']: b'x' * 100 for r in reads}
    by_files = audit(reads, Disk(files), max_files=2, max_bytes=10_000)
    assert by_files['counts']['skipped_budget'] == 3 and by_files['checked'] == 2 and not by_files['complete']
    assert by_files['counts']['unchanged'] == 2
    disk = Disk(files)
    by_bytes = audit(reads, disk, max_files=10, max_bytes=250)
    assert by_bytes['counts'] == {'unchanged': 2, 'changed': 0, 'deleted': 0, 'unreadable': 0, 'skipped_budget': 3}
    # An over-budget file is not read; smaller later files may still fit, and
    # every callback call counts against max_files.
    assert by_bytes['bytes_hashed'] == 200 and [c[1] for c in disk.calls] == [250, 150, 50, 50, 50]
    assert by_bytes['checked'] == 5
    # A callback that reads beyond its allowance is not trusted.
    greedy = audit(reads[:1], lambda r, remaining: (digest(b'x' * 100), 10_000), max_files=1, max_bytes=50)
    assert greedy['counts']['skipped_budget'] == 1
    assert guard_summary(by_bytes)['unverified_sources'] == 3 and not guard_summary(by_bytes)['blocking']
    assert completion_issues(by_bytes) == []


def test_a_later_read_clears_a_stale_entry():
    first = read('a.py', b'v1', 1)
    files = {first['path_id']: b'v2'}
    assert states(audit([first], Disk(files), max_files=4, max_bytes=100)) == {first['path_id']: 'changed'}
    again = read('a.py', b'v2', 7)
    result = audit([first, again], Disk(files), max_files=4, max_bytes=100)
    assert states(result) == {first['path_id']: 'unchanged'} and result['stale'] == []
    assert result['checked'] == 1  # Only the latest read of the path is checked.


def test_a_replaced_root_is_unreadable_without_rehashing():
    reads = [read('a.py', b'one', 1), read('b.py', b'two', 2, root=(1, 2))]
    disk = Disk({reads[0]['path_id']: b'one', reads[1]['path_id']: b'two'})
    result = audit(reads, disk, max_files=4, max_bytes=100, root=list(ROOT))
    assert states(result) == {reads[0]['path_id']: 'unchanged', reads[1]['path_id']: 'unreadable'}
    assert [c[0] for c in disk.calls] == [reads[0]['path_id']]
    assert completion_issues(result) and 'b.py' not in ' '.join(completion_issues(result))


def test_malformed_callback_results_and_cancellation():
    reads = [read('a.py', b'one', 1)]
    for bogus in ('abc', ('not-a-hash', 3), (digest(b'one'), -1), [digest(b'one'), 3]):
        assert audit(reads, lambda r, n, value=bogus: value, max_files=1, max_bytes=100)['counts']['unreadable'] == 1

    def cancelled(r, n):
        raise InterruptedError('Stopped by you.')
    with pytest.raises(InterruptedError):
        audit(reads, cancelled, max_files=1, max_bytes=100)


@pytest.mark.parametrize('reads, reason', [
    ('x', 'not_list'),
    ([{'path_id': 'src/a.py', 'root': [1, 2], 'hash': None, 'sequence': 1}], 'bad_path_id'),
    ([{'path_id': 'p:' + 'a' * 24, 'root': [1, 2], 'hash': 'nothex', 'sequence': 1}], 'bad_hash'),
    ([{'path_id': 'p:' + 'a' * 24, 'root': [1, 2], 'hash': None, 'sequence': True}], 'bad_sequence'),
    ([{'path_id': 'p:' + 'a' * 24, 'root': '/tmp', 'hash': None, 'sequence': 1}], 'bad_root'),
    ([{'path_id': 'p:' + 'a' * 24, 'root': [1, 2], 'hash': None, 'sequence': 1, 'path': 'a.py'}], 'bad_read'),
    ([{'path_id': 'p:' + 'a' * 24, 'root': [1, 2], 'hash': None, 'sequence': 1},
      {'path_id': 'p:' + 'b' * 24, 'root': [1, 2], 'hash': None, 'sequence': 1}], 'bad_sequence'),
])
def test_malformed_reads_are_refused_without_paths(reads, reason):
    with pytest.raises(SourceAuditError) as caught:
        audit(reads, Disk({}), max_files=1, max_bytes=1)
    assert caught.value.reason == reason and 'a.py' not in str(caught.value)


def test_limits_and_callback_are_validated():
    with pytest.raises(TypeError):
        audit([], None)
    for bad in ({'max_files': -1}, {'max_files': source_audit.MAX_FILES + 1}, {'max_bytes': True},
                {'max_bytes': source_audit.MAX_BYTES + 1}):
        with pytest.raises(ValueError):
            audit([], Disk({}), **bad)
    empty = audit([], Disk({}))
    assert empty['entries'] == [] and empty['complete'] is True


def test_witness_rows_map_to_reads_and_validation_rejects_forgery():
    rows = [{'path': 'a.py', 'root_dev': 1, 'root_ino': 2, 'disk_hash': digest(b'a'), 'ordinal': 1},
            {'path': 'new.py', 'root_dev': 1, 'root_ino': 2, 'disk_hash': None, 'ordinal': 2}]
    reads = reads_from_witnesses(rows, RUN)
    assert reads == [{'path_id': path_id(RUN, (1, 2), 'a.py'), 'root': [1, 2], 'hash': digest(b'a'), 'sequence': 1},
                     {'path_id': path_id(RUN, (1, 2), 'new.py'), 'root': [1, 2], 'hash': None, 'sequence': 2}]
    assert path_id(RUN, (1, 2), 'a.py') != path_id('other-run', (1, 2), 'a.py') != path_id(RUN, (1, 3), 'a.py')
    result = audit(reads, Disk({reads[0]['path_id']: b'b'}), max_files=4, max_bytes=100)
    assert validate(result)['stale'] == [reads[0]['path_id']]
    forged = json.loads(json.dumps(result))
    forged['entries'][0]['path'] = 'a.py'
    with pytest.raises(SourceAuditError):
        validate(forged)
    lying = json.loads(json.dumps(result))
    lying['entries'][0]['state'] = 'unchanged'
    with pytest.raises(SourceAuditError, match='bad_counts'):
        validate(lying)
    assert guard_summary(result) == {'kind': 'source_audit', 'authorization': 'none', 'stale_sources': 1,
                                     'unverified_sources': 0, 'complete': True, 'blocking': True,
                                     'counts': result['counts']}


def test_audit_enters_the_handoff_stale_sources_and_manifest():
    run = {'id': RUN, 'chat_id': 'chat-a', 'goal': 'Fix it', 'state': 'running', 'steps': 2, 'elapsed': 1.0,
           'config': {'limits': {'max_steps': 64, 'max_seconds': 900.0}}}
    reads = [read('a.py', b'one', 1), read('b.py', b'two', 2), read('c.py', b'three', 3)]
    result = audit(reads, Disk({reads[0]['path_id']: b'one!', reads[2]['path_id']: b'three'}),
                   max_files=2, max_bytes=100)
    packet = make_handoff(run, [], audit=result)
    stale = packet.data['unresolved']['stale_sources']
    # Newest first: c.py unchanged, b.py deleted, a.py skipped by the two-file
    # budget. Skipped files stay unverified and are not called stale or fresh.
    assert stale == [{'path_id': reads[1]['path_id'], 'status': 'deleted', 'basis': 'settlement_audit'}]
    assert result['counts']['skipped_budget'] == 1 and states(result)[reads[0]['path_id']] == 'skipped_budget'
    assert packet.data['source_audit']['counts'] == result['counts']
    assert packet.data['source_audit']['complete'] is False
    assert packet.data['source_audit']['semantics'] == 'skipped_budget_is_unverified_not_fresh'
    assert packet.manifest['counts']['source_audit'] == result['counts']
    assert packet.manifest['unresolved']['stale_sources'] == 1
    assert 'b.py' not in json.dumps(packet.data) and reads[1]['path_id'] not in json.dumps(packet.manifest)
    without = make_handoff(run, [])
    assert 'source_audit' not in without.data and 'source_audit' not in without.manifest['counts']
    with pytest.raises(HandoffIntegrityError) as caught:
        make_handoff(run, [], audit={'kind': 'source_audit', 'entries': [{'path': 'a.py'}]})
    assert caught.value.section == 'source_audit'
