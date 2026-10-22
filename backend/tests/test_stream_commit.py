"""Canonical response and presentation settlement must commit together."""
import hashlib

import pytest

from pebble.runtime import RunJournal
from pebble.store import Store
from pebble.stream_journal import StreamJournal


def test_response_and_stream_commit_share_one_transaction(tmp_path):
    store = Store(tmp_path)
    runs = RunJournal(store, tmp_path)
    streams = StreamJournal(store, runs)
    cid = store.new_chat()
    rid = runs.create(cid, 'goal', {'limits': {'max_steps': 3, 'max_seconds': 30}})
    runs.transition(rid, 'running')
    aid = streams.begin(rid, 1, 1)
    streams.append(aid, 'observed answer')
    response = {'role': 'assistant', 'content': 'observed answer'}
    snapshot = {'phase': 'tools', 'messages': [response]}
    try:
        with pytest.raises(ValueError, match='does not match'):
            runs.response(rid, response, snapshot, steps=1, elapsed=1,
                presentation_commit=lambda db: streams.settle_in(db, aid, 'committed', expect_sha256='0' * 64))
        assert runs.get(rid)['steps'] == 0
        assert not any(e['kind'] == 'response_committed' for e in runs.events(rid))
        assert streams.read(cid, rid)['events'][-1]['kind'] == 'delta'
        expected = hashlib.sha256(b'observed answer').hexdigest()
        runs.response(rid, response, snapshot, steps=1, elapsed=1,
            presentation_commit=lambda db: streams.settle_in(db, aid, 'committed', expect_sha256=expected))
        assert runs.get(rid)['steps'] == 1
        assert streams.read(cid, rid)['events'][-1]['state'] == 'committed'
    finally:
        runs.close()
        store.close()


def test_discarded_stream_cannot_commit_a_response(tmp_path):
    store = Store(tmp_path)
    runs = RunJournal(store, tmp_path)
    streams = StreamJournal(store, runs)
    cid = store.new_chat()
    rid = runs.create(cid, 'goal', {'limits': {'max_steps': 3, 'max_seconds': 30}})
    runs.transition(rid, 'running')
    aid = streams.begin(rid, 1, 1)
    streams.append(aid, 'partial')
    streams.settle(aid, 'discarded')
    try:
        with pytest.raises(ValueError, match='differently'):
            runs.response(rid, {'role': 'assistant', 'content': 'partial'}, {}, steps=1, elapsed=1,
                presentation_commit=lambda db: streams.settle_in(db, aid, 'committed'))
        assert runs.get(rid)['steps'] == 0
        assert streams.read(cid, rid)['events'][-1]['state'] == 'discarded'
    finally:
        runs.close()
        store.close()
