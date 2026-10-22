"""Revision ownership, atomic persistence, migration and declarative boundaries."""

import copy
import json
import sqlite3
import threading

import pytest

from pebble.artifact_schema import validate_form_input
from pebble.artifacts import Artifacts, validate
from pebble.runtime import RunJournal
from pebble.store import Store


DIAGRAM = {'nodes': [{'id': 'a', 'label': 'A'}], 'edges': []}
FORM = {'fields': [{'id': 'scope', 'label': 'Which scope?', 'type': 'choice',
                    'options': [{'id': 'small', 'label': 'Small'}, {'id': 'large', 'label': 'Large'}]}]}
TABLE = {'columns': [{'id': 'name', 'label': 'Name'}, {'id': 'score', 'label': 'Score'}],
         'rows': [{'name': 'Alpha', 'score': 3}]}
CHART = {'type': 'bar', 'x_key': 'name', 'y_keys': ['score'], 'rows': [{'name': 'Alpha', 'score': 3}]}


@pytest.fixture
def service(tmp_path):
    store = Store(tmp_path)
    artifacts = Artifacts(store)
    journal = RunJournal(store, tmp_path)
    cid, foreign = store.new_chat(), store.new_chat()
    runs = []
    for chat in (cid, cid, foreign):
        rid = journal.create(chat, 'Make a view', {'limits': {'max_steps': 10, 'max_seconds': 60}})
        journal.transition(rid, 'paused')
        runs.append(rid)
    journal.transition(runs[0], 'running')
    yield store, artifacts, journal, cid, foreign, runs
    journal.close()
    store.close()


def receipt(store, rid, ident, state='completed', result=None):
    store.query('INSERT INTO run_tools VALUES(?,?,?,?,?,?)',
                (rid, ident, 'Read', '{}', state, json.dumps(result or {'content': 'Verified data'})))


def test_revisions_survive_restart_and_keep_created_and_history(service, tmp_path):
    store, artifacts, journal, cid, _, (rid, _, _) = service
    first = artifacts.render(cid, 'Original', 'table', TABLE, rid)
    second = artifacts.update(cid, first['id'], 1, 'Revised', 'chart', CHART, rid)
    assert second['revision'] == 2 and second['created'] == first['created']
    assert second['data_hash'] != first['data_hash'] and second['owner_run_id'] == rid
    assert artifacts.get(cid, first['id'], 1)['data'] == TABLE
    assert artifacts.list(cid)[0]['data'] == CHART
    journal.close()
    reopened = Store(tmp_path)
    try:
        restored = Artifacts(reopened)
        assert restored.get(cid, first['id']) == second
        assert [row['title'] for row in restored.history(cid, first['id'])] == ['Original', 'Revised']
    finally:
        reopened.close()


@pytest.mark.parametrize('attempt', ['stale', 'foreign_chat', 'foreign_run', 'missing_run', 'paused_owner'])
def test_foreign_stale_and_inactive_updates_do_not_change_history(service, attempt):
    store, artifacts, journal, cid, foreign, (rid, other, _) = service
    first = artifacts.render(cid, 'Owned', 'diagram', DIAGRAM, rid)
    chat, owner, revision = cid, rid, 1
    if attempt == 'stale':
        revision = 2
    elif attempt == 'foreign_chat':
        chat = foreign
    elif attempt == 'foreign_run':
        owner = other
    elif attempt == 'missing_run':
        owner = None
    else:
        journal.transition(rid, 'paused')
    with pytest.raises(ValueError):
        artifacts.update(chat, first['id'], revision, 'Changed', 'diagram', DIAGRAM, owner)
    assert len(artifacts.history(cid, first['id'])) == 1
    assert artifacts.get(cid, first['id'])['title'] == 'Owned'
    with pytest.raises(ValueError):
        artifacts.get(foreign, first['id'], 1)


def test_concurrent_cas_across_connections_has_one_winner(service, tmp_path):
    store, artifacts, _, cid, _, (rid, _, _) = service
    first = artifacts.render(cid, 'Start', 'diagram', DIAGRAM, rid)
    second_store = Store(tmp_path)
    second_artifacts = Artifacts(second_store)
    barrier, results = threading.Barrier(2), []

    def change(instance, title):
        barrier.wait()
        try:
            results.append(instance.update(cid, first['id'], 1, title, 'diagram', DIAGRAM, rid))
        except ValueError as error:
            results.append(str(error))

    workers = [threading.Thread(target=change, args=(instance, title)) for instance, title in
               ((artifacts, 'One'), (second_artifacts, 'Two'))]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        assert len([r for r in results if isinstance(r, dict)]) == 1
        assert len([r for r in results if isinstance(r, str) and 'stale' in r]) == 1
        assert [r['revision'] for r in artifacts.history(cid, first['id'])] == [1, 2]
    finally:
        second_store.close()


@pytest.mark.parametrize('operation', ['create', 'update', 'stop'])
def test_revision_write_failure_rolls_back_materialized_data_and_head(service, operation):
    store, artifacts, _, cid, _, (rid, _, _) = service
    first = artifacts.render(cid, 'Start', 'diagram', DIAGRAM, rid)
    before = artifacts.get(cid, first['id'])
    store.query("CREATE TRIGGER fail_revision BEFORE INSERT ON artifact_revisions BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='disk failure'):
        if operation == 'create':
            artifacts.render(cid, 'Failed create', 'diagram', DIAGRAM, rid)
        elif operation == 'update':
            artifacts.update(cid, first['id'], 1, 'Failed update', 'table', TABLE, rid)
        else:
            artifacts.stop(cid, first['id'], 1)
    assert artifacts.list(cid) == [before]
    assert artifacts.history(cid, first['id']) == [before]
    assert not store.db.in_transaction


@pytest.mark.parametrize('operation', ['render', 'update', 'get', 'history', 'stop', 'forget', 'list', 'migrate'])
def test_outside_transaction_guards_never_commit_callers_work(service, operation):
    store, artifacts, _, cid, _, (rid, _, _) = service
    first = artifacts.render(cid, 'Start', 'diagram', DIAGRAM, rid)
    with store.lock:
        store.db.execute("INSERT INTO settings VALUES('uncommitted','true')")
        with pytest.raises(ValueError, match='outside transaction'):
            if operation == 'render':
                artifacts.render(cid, 'Another', 'diagram', DIAGRAM, rid)
            elif operation == 'update':
                artifacts.update(cid, first['id'], 1, 'Changed', 'diagram', DIAGRAM, rid)
            elif operation == 'get':
                artifacts.get(cid, first['id'])
            elif operation == 'history':
                artifacts.history(cid, first['id'])
            elif operation == 'stop':
                artifacts.stop(cid, first['id'], 1)
            elif operation == 'forget':
                artifacts.forget(cid)
            elif operation == 'list':
                artifacts.list(cid)
            else:
                Artifacts(store)
        assert store.db.in_transaction
        store.db.rollback()
    assert not store.query("SELECT 1 FROM settings WHERE key='uncommitted'")
    assert artifacts.get(cid, first['id'])['revision'] == 1


def test_original_six_column_rows_and_late_host_media_migrate_idempotently(tmp_path):
    store = Store(tmp_path)
    try:
        store.query('CREATE TABLE artifacts(id TEXT PRIMARY KEY,chat_id TEXT,title TEXT,kind TEXT,data TEXT,created REAL)')
        store.query('INSERT INTO artifacts VALUES(?,?,?,?,?,?)', ('legacy', 'chat', 'Legacy', 'diagram', json.dumps(DIAGRAM), 123.5))
        artifacts = Artifacts(store)
        assert artifacts.get('chat', 'legacy')['created'] == 123.5
        assert artifacts.get('chat', 'legacy')['owner_run_id'] is None
        assert len(Artifacts(store).history('chat', 'legacy')) == 1
        media = {'run_id': 'host-run', 'snapshot_id': 'snapshot'}
        store.query('INSERT INTO artifacts VALUES(?,?,?,?,?,?)', ('media', 'chat', 'Connector image', 'mcp_media', json.dumps(media), 123.5))
        assert [row['id'] for row in artifacts.list('chat')] == ['legacy', 'media']
        row = artifacts.get('chat', 'media')
        assert row['owner_run_id'] == 'host-run' and row['status'] == 'ready' and row['data'] == media
        assert len(Artifacts(store).history('chat', 'media')) == 1
        with pytest.raises(ValueError):
            artifacts.render('chat', 'Spoofed media', 'mcp_media', media)
        with pytest.raises(ValueError, match='owning run'):
            artifacts.update('chat', 'media', 1, 'Overwrite host data', 'diagram', DIAGRAM, 'host-run')
        with pytest.raises(ValueError):
            artifacts.update('chat', 'legacy', 1, 'Claim legacy', 'diagram', DIAGRAM, 'host-run')
        assert len(store.query('PRAGMA table_info(artifacts)')) == 6
    finally:
        store.close()


def test_failed_legacy_migration_rolls_back_schema_and_preserves_rows(tmp_path):
    store = Store(tmp_path)
    try:
        store.query('CREATE TABLE artifacts(id TEXT PRIMARY KEY,chat_id TEXT,title TEXT,kind TEXT,data TEXT,created REAL)')
        store.query('INSERT INTO artifacts VALUES(?,?,?,?,?,?)', ('a', 'chat', 'Valid', 'diagram', json.dumps(DIAGRAM), 1))
        store.query('INSERT INTO artifacts VALUES(?,?,?,?,?,?)', ('b', 'chat', 'Broken', 'diagram', '{', 2))
        with pytest.raises(ValueError):
            Artifacts(store)
        assert not store.query("SELECT 1 FROM sqlite_master WHERE name='artifact_revisions'")
        assert len(store.query('SELECT * FROM artifacts')) == 2
        assert not store.db.in_transaction
    finally:
        store.close()


@pytest.mark.parametrize('tamper', [
    "UPDATE artifacts SET data='{}' WHERE id=?",
    "UPDATE artifact_revisions SET data='{}' WHERE artifact_id=? AND revision=1",
    "UPDATE artifact_revisions SET owner_run_id='attacker' WHERE artifact_id=? AND revision=1",
    "UPDATE artifact_revisions SET status='ready' WHERE artifact_id=? AND revision=2",
    "DELETE FROM artifact_revisions WHERE artifact_id=? AND revision=1",
    "DELETE FROM artifact_heads WHERE artifact_id=?",
    "UPDATE artifact_heads SET record_hash='forged' WHERE artifact_id=?",
])
def test_tampering_rejects_current_historical_and_restart_reads(service, tamper):
    store, artifacts, _, cid, _, (rid, _, _) = service
    first = artifacts.render(cid, 'Start', 'diagram', DIAGRAM, rid)
    artifacts.stop(cid, first['id'], 1)
    store.query(tamper, (first['id'],))
    for operation in (lambda: artifacts.get(cid, first['id']), lambda: artifacts.get(cid, first['id'], 1),
                      lambda: artifacts.list(cid), lambda: artifacts.history(cid, first['id']), lambda: Artifacts(store)):
        with pytest.raises(ValueError):
            operation()


def test_deleting_head_and_all_revisions_does_not_reimport_existing_artifact(service):
    store, artifacts, _, cid, _, (rid, _, _) = service
    first = artifacts.render(cid, 'Owned', 'diagram', DIAGRAM, rid)
    store.query('DELETE FROM artifact_revisions WHERE artifact_id=?', (first['id'],))
    store.query('DELETE FROM artifact_heads WHERE artifact_id=?', (first['id'],))
    with pytest.raises(ValueError, match='history'):
        artifacts.get(cid, first['id'])


def test_revision_limit_keeps_native_stop_available_and_forget_is_scoped(service):
    store, artifacts, _, cid, foreign, (rid, _, _) = service
    first = artifacts.render(cid, 'Start', 'diagram', DIAGRAM, rid)
    foreign_view = artifacts.render(foreign, 'Other conversation', 'diagram', DIAGRAM)
    for revision in range(1, 15):
        artifacts.update(cid, first['id'], revision, f'Revision {revision + 1}', 'diagram', DIAGRAM, rid)
    with pytest.raises(ValueError, match='content revision limit'):
        artifacts.update(cid, first['id'], 15, 'Too many', 'diagram', DIAGRAM, rid)
    stopped = artifacts.stop(cid, first['id'], 15)
    assert stopped['revision'] == 16 and stopped['status'] == 'stopped'
    assert artifacts.stop(cid, first['id'], 16) == stopped
    assert len(artifacts.history(cid, first['id'])) == 16
    with pytest.raises(ValueError, match='stale'):
        artifacts.stop(cid, first['id'], 15)
    with pytest.raises(ValueError, match='no longer'):
        artifacts.update(cid, first['id'], 16, 'Restart stopped', 'diagram', DIAGRAM, rid)
    artifacts.forget(cid)
    assert not artifacts.list(cid)
    assert not store.query('SELECT * FROM artifact_revisions WHERE chat_id=?', (cid,))
    assert not store.query('SELECT * FROM artifact_heads WHERE artifact_id=?', (first['id'],))
    assert not store.query('SELECT * FROM artifact_registry WHERE chat_id=?', (cid,))
    assert artifacts.get(foreign, foreign_view['id'])['title'] == 'Other conversation'


def test_expiration_keeps_historical_inspection_and_blocks_updates(service):
    _, artifacts, _, cid, _, (rid, _, _) = service
    first = artifacts.render(cid, 'Custom', 'html', {'html': '<p>Review me</p>', 'script': 'console.log(1)'}, rid)
    assert first['status'] == 'review'
    expired = artifacts.expire(cid, first['id'], 1)
    assert expired['status'] == 'expired' and artifacts.get(cid, first['id'], 1)['status'] == 'review'
    with pytest.raises(ValueError, match='no longer'):
        artifacts.update(cid, first['id'], 2, 'Custom again', 'html', {'html': '<p>Changed</p>'}, rid)


def test_chat_artifact_cap_counts_items_and_not_revisions(service):
    _, artifacts, _, cid, _, (rid, _, _) = service
    first = artifacts.render(cid, 'First', 'diagram', DIAGRAM, rid)
    for index in range(29):
        artifacts.render(cid, str(index), 'diagram', DIAGRAM, rid)
    artifacts.update(cid, first['id'], 1, 'Updated within cap', 'table', TABLE, rid)
    with pytest.raises(ValueError, match='30 views'):
        artifacts.render(cid, 'Overflow', 'diagram', DIAGRAM, rid)
    assert len(artifacts.list(cid)) == 30


@pytest.mark.parametrize('state,result', [
    ('planned', {'content': 'not started'}), ('running', {'content': 'uncommitted'}),
    ('uncertain', {'content': 'unknown'}), ('completed', {'error': 'failed'}),
    ('completed', {'status': 'awaiting_user_approval'}), ('completed', {'status': 'user_reported'}),
    ('completed', {'status': 'failed'}), ('completed', {'result': {'exit_code': 1}}),
    ('completed', {'content': float('nan')}),
])
def test_unresolved_evidence_is_rejected_without_partial_artifact(service, state, result):
    store, artifacts, _, cid, _, (rid, _, _) = service
    receipt(store, rid, '1:read', state, result)
    with pytest.raises(ValueError, match='resolved tool receipts'):
        artifacts.render(cid, 'Evidence', 'evidence', {'items': [{'id': 'one', 'label': 'Read', 'evidence_id': '1:read'}]}, rid)
    assert artifacts.list(cid) == []


def test_evidence_is_linked_to_own_resolved_receipts_and_completed_steps(service):
    store, artifacts, _, cid, _, (rid, other, _) = service
    receipt(store, rid, '1:read')
    receipt(store, other, '1:foreign')
    plan = {'steps': [{'id': 'checked', 'title': 'Checked source', 'status': 'completed', 'evidence_ids': ['1:read']}]}
    first = artifacts.render(cid, 'Verified plan', 'plan', plan, rid)
    assert first['evidence_ids'] == ['1:read']
    with pytest.raises(ValueError, match='resolved tool receipts'):
        artifacts.update(cid, first['id'], 1, 'Foreign evidence', 'plan', plan, rid, ['1:foreign'])
    assert len(artifacts.history(cid, first['id'])) == 1
    with pytest.raises(ValueError, match='owning run'):
        artifacts.render(cid, 'Unowned evidence', 'plan', plan)


@pytest.mark.parametrize('kind,data', [('form', FORM), ('table', TABLE), ('chart', CHART),
                                      ('plan', {'steps': [{'id': 'one', 'title': 'Check', 'status': 'pending'}]}),
                                      ('evidence', {'items': [{'id': 'one', 'label': 'Source', 'evidence_id': '1:read'}]})])
@pytest.mark.parametrize('key,value', [('origin', 'human'), ('on_submit', 'nativeRpc'), ('actions', ['approve']),
                                      ('script', 'alert(1)'), ('permission', 'granted')])
def test_native_declarative_data_rejects_executable_and_authority_fields(kind, data, key, value):
    with pytest.raises(ValueError):
        validate(kind, {**copy.deepcopy(data), key: value})


@pytest.mark.parametrize('data', [
    {'fields': [{'id': 'password', 'label': 'Password', 'type': 'text'}]},
    {'fields': [{'id': 'value', 'label': 'Paste your access token', 'type': 'text'}]},
    {'fields': [{'id': '__proto__', 'label': 'Name', 'type': 'text'}]},
    {'fields': [{'id': 'one', 'label': 'One', 'type': 'text', 'required': False}]},
    {'fields': [{'id': str(i), 'label': 'Scope', 'type': 'text'} for i in range(4)]},
    {'fields': [{'id': 'scope', 'label': 'Scope', 'type': 'choice', 'options': [{'id': 'one', 'label': 'One'}, {'id': 'one', 'label': 'Again'}]}]},
    {'fields': [{'id': 'scope', 'label': 'Scope', 'type': ['text']}]},
])
def test_form_schema_rejects_credential_prompts_optional_or_unstable_fields(data):
    with pytest.raises(ValueError):
        validate('form', data)


def test_form_title_cannot_hide_a_credential_request(service):
    _, artifacts, _, cid, _, (rid, _, _) = service
    with pytest.raises(ValueError, match='Credentials'):
        artifacts.render(cid, 'Enter your API key', 'form', {'fields': [{'id': 'value', 'label': 'Value', 'type': 'text'}]}, rid)
    assert artifacts.list(cid) == []


def test_form_values_are_data_and_cannot_set_origin_or_native_permissions():
    normalized = validate('form', {'fields': [{'id': 'approval', 'label': 'Approval note', 'type': 'text'}]})
    assert normalized['fields'][0]['required'] is True
    assert validate_form_input(normalized, {'approval': 'approve'}) == {'approval': 'approve'}
    for values in ({}, {'approval': 'yes', 'origin': 'human'}, {'approval': {'approved': True}}, {'approval': 'sk-' + 'a' * 30}):
        with pytest.raises(ValueError):
            validate_form_input(normalized, values)
    assert validate_form_input(FORM, {'scope': 'small'}) == {'scope': 'small'}
    with pytest.raises(ValueError):
        validate_form_input(FORM, {'scope': 'not_an_option'})


@pytest.mark.parametrize('kind,data', [
    ('table', {**TABLE, 'rows': [{'name': 'Alpha', 'score': {'html': '<script>'}}]}),
    ('table', {**TABLE, 'rows': [{'name': 'Alpha'}]}),
    ('table', {**TABLE, 'rows': [{'name': 'sk-' + 'a' * 30, 'score': 1}]}),
    ('chart', {**CHART, 'rows': [{'name': 'Alpha', 'score': True}]}),
    ('chart', {**CHART, 'rows': [{'name': 'Alpha', 'score': float('inf')}]}),
    ('chart', {**CHART, 'type': ['bar']}),
    ('chart', {**CHART, 'y_keys': ['name']}),
    ('plan', {'steps': [{'id': 'one', 'title': 'Done', 'status': 'completed'}]}),
    ('plan', {'steps': [{'id': 'one', 'title': 'A', 'status': 'in_progress'}, {'id': 'two', 'title': 'B', 'status': 'in_progress'}]}),
    ('evidence', {'items': [{'id': 'one', 'label': 'Evidence', 'evidence_id': ''}]}),
])
def test_native_types_reject_invalid_cells_series_secrets_and_evidence(kind, data):
    with pytest.raises(ValueError):
        validate(kind, data)


@pytest.mark.parametrize('data', ['{"html":"ok","score":NaN}', {'html': 'x' * 96001},
                                {'html': 'ok', 'nested': [[[[[[[[['too deep']]]]]]]]]},
                                {'html': 'ok', 'foreign': object()}])
def test_json_boundary_rejects_nonfinite_oversize_deep_and_non_json_values(data):
    with pytest.raises(ValueError):
        validate('html', data)
