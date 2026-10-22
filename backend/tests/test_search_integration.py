"""Index evidence and paging remain independent of authorization and text trim."""

import json

import pytest

from pebble.app import App
from pebble.context import encode_result
from pebble.policy import PolicyError, Workspace
from pebble import policy, search_index


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    chat = app.new_chat(str(root))
    yield app, chat, Workspace(root)
    app.close()


def test_model_pages_keep_every_match_with_exact_wire_cursor(project):
    app, chat, ws = project
    (ws.root / 'a.txt').write_text('needle\n' * 7)
    cursor, matches = None, []
    while True:
        result = app.execute('Grep', {'pattern': 'needle', 'limit': 30, **({'cursor': cursor} if cursor else {})}, ws, chat['id'])
        wire = json.loads(encode_result(result))
        assert wire['cursor'] == result['cursor']
        matches += [m['line'] for m in wire['untrusted_matches']]
        cursor = wire['cursor']
        if cursor is None:
            break
    assert matches == list(range(1, 8))


def test_long_nested_paths_have_compact_replayable_cursors(project):
    app, chat, ws = project
    directory = ws.root
    for i in range(4):
        directory /= str(i) + '漢' * 55
        directory.mkdir()
    for name in ('a.txt', 'b.txt'):
        (directory / name).write_text('needle\n')
    first = app.search.grep(ws, 'needle', scope=chat['id'], limit=1)
    path = first['untrusted_matches'][0]['path']
    assert len(path) <= 512 and len(path.encode('utf-8')) > 512
    assert len(first['cursor']) < 500
    second = app.search.grep(ws, 'needle', scope=chat['id'], limit=1, cursor=first['cursor'])
    assert second['untrusted_matches'][0]['path'].endswith('/b.txt') and second['cursor'] is None
    first = app.search.glob(ws, '**/*.txt', scope=chat['id'], limit=1)
    second = app.search.glob(ws, '**/*.txt', scope=chat['id'], limit=1, cursor=first['cursor'])
    assert second['files'][0].endswith('/b.txt') and second['cursor'] is None


def test_foreign_chat_or_run_cannot_use_a_cursor(project):
    app, chat, ws = project
    (ws.root / 'a.txt').write_text('needle\n' * 4)
    first = app.execute('Grep', {'pattern': 'needle'}, ws, chat['id'])
    other = app.new_chat(str(ws.root))
    with pytest.raises(PolicyError, match='conversation or run'):
        app.execute('Grep', {'pattern': 'needle', 'cursor': first['cursor']}, ws, other['id'])
    app.active, app.active_run = chat['id'], 'new-run'
    try:
        with pytest.raises(PolicyError, match='conversation or run'):
            app.search.grep(ws, 'needle', scope=(chat['id'], 'new-run'), cursor=first['cursor'])
    finally:
        app.active = app.active_run = None


def test_cached_matches_do_not_authorize_an_existing_file_write(project):
    app, chat, ws = project
    (ws.root / 'a.txt').write_text('needle')
    rid = app.runs.create(chat['id'], 'Find and inspect source', app.runtime_config(chat, False))
    execution, token = app.controller.enter(app.runs.get(rid))
    app.active, app.active_run = chat['id'], rid
    try:
        assert app.execute('Grep', {'pattern': 'needle'}, ws, chat['id'], call_id='find')['untrusted_matches']
        assert not app.provenance.records(rid)
        with pytest.raises(PolicyError, match='Read it again'):
            app.execute('Write', {'file_path': 'a.txt', 'content': 'overwrite'}, ws, chat['id'], call_id='write')
        assert not app.store.actions(chat['id'])
    finally:
        app.active = app.active_run = None
        app.controller.leave(execution, token)


def test_tightened_policy_invalidates_cached_eligible_body(project, monkeypatch):
    app, chat, ws = project
    (ws.root / 'new-private.txt').write_text('private-sentinel')
    assert app.execute('Grep', {'pattern': 'private-sentinel'}, ws, chat['id'])['untrusted_matches']
    monkeypatch.setattr(policy, 'PRIVATE', policy.PRIVATE | {'new-private.txt'})
    result = app.execute('Grep', {'pattern': 'private-sentinel'}, ws, chat['id'])
    assert not result['untrusted_matches']
    assert not app.store.query("SELECT 1 FROM search_index_files WHERE path='new-private.txt' AND body IS NOT NULL")


def test_search_cannot_commit_or_rollback_an_application_transaction(project):
    app, chat, ws = project
    (ws.root / 'a.txt').write_text('needle')
    db = app.store.db
    with app.store.lock:
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO settings VALUES('uncommitted-sentinel','true')")
        with pytest.raises(PolicyError, match='application transaction'):
            app.search.grep(ws, 'needle', scope=chat['id'])
        assert db.in_transaction and db.execute("SELECT value FROM settings WHERE key='uncommitted-sentinel'").fetchone()
        db.rollback()
        assert not db.execute("SELECT 1 FROM settings WHERE key='uncommitted-sentinel'").fetchone()


def test_glob_keeps_recent_cached_bodies_and_drafts_stay_unstored(project, monkeypatch):
    app, chat, ws = project
    (ws.root / 'a.txt').write_text('needle')
    app.execute('Grep', {'pattern': 'needle'}, ws, chat['id'])
    app.execute('Glob', {'pattern': '*.txt'}, ws, chat['id'])
    assert app.store.query("SELECT body FROM search_index_files WHERE path='a.txt'")[0]['body'] == b'needle'
    ws.draft('draft.txt', 'staged-sentinel')
    assert app.execute('Grep', {'pattern': 'staged-sentinel'}, ws, chat['id'])['untrusted_matches'][0]['basis'] == 'draft'
    assert not app.store.query("SELECT 1 FROM search_index_files WHERE body=?", (b'staged-sentinel',))
    monkeypatch.setattr(search_index, 'TEXT_BUDGET', 3)
    with pytest.raises(PolicyError, match='Staged search views'):
        app.search.glob(ws, '*.txt', scope=chat['id'])


def test_serialization_never_drops_rows_but_advances_the_page():
    result = {'untrusted_matches': [{'text': 'x' * 200} for _ in range(30)], 'cursor': 'next-page', 'receipt_id': '1:grep'}
    encoded = json.loads(encode_result(result, limit=500))
    assert encoded['page_not_shown'] and encoded['status'] == 'evidence_too_large'
    assert 'cursor' not in encoded and 'untrusted_matches' not in encoded
    assert encoded['receipt_id'] == '1:grep' and 'ORIGINAL' in encoded['context_note']


def test_cache_inspection_is_content_free_and_conversation_scoped(project):
    app, chat, ws = project
    (ws.root / 'a.txt').write_text('private-sentinel')
    app.execute('Grep', {'pattern': 'private-sentinel'}, ws, chat['id'])
    metadata = app.search_stats(chat['id'])
    assert metadata['inventoried'] == 1 and 'private-sentinel' not in json.dumps(metadata)
    assert app.search_stats(app.new_chat()['id']) is None
