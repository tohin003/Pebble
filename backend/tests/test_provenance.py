"""File evidence must survive recovery without becoming approval or stale edits."""

import copy
import json
import os

import pytest

from pebble.app import App
from pebble.policy import MAX_FILE, PolicyError, Workspace, digest
from pebble import provenance
from pebble.runtime_commands import command


@pytest.fixture
def owned(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    chat = app.new_chat(str(root))
    rid = app.runs.create(chat['id'], 'Inspect and change source', app.runtime_config(chat, False))
    execution, token = app.controller.enter(app.runs.get(rid))
    app.active, app.active_run = chat['id'], rid
    app.runs.transition(rid, 'running')
    ws = Workspace(root)
    yield app, chat, rid, ws
    app.active = app.active_run = None
    app.controller.leave(execution, token)
    app.close()


def read(owned, path='a.txt', ident='read'):
    app, chat, _, ws = owned
    return app.execute('Read', {'file_path': path}, ws, chat['id'], call_id=ident)


def write(owned, content='new', path='a.txt', ident='write'):
    app, chat, _, ws = owned
    return app.execute('Write', {'file_path': path, 'content': content}, ws, chat['id'], call_id=ident)


def test_file_view_uses_raw_utf8_hash_including_crlf(owned):
    app, _, rid, ws = owned
    raw = '漢字\r\nsecond\r\n'.encode('utf-8')
    (ws.root / 'a.txt').write_bytes(raw)
    result = read(owned)
    assert result['file_hash'] == digest(raw) == result['source']['file_hash']
    assert app.provenance.records(rid)[0]['basis'] == 'disk'
    assert ws.draft('a.txt', 'x')['baseline'] == digest(raw)


@pytest.mark.parametrize('change,status', [('change', 'changed'), ('delete', 'deleted'), ('symlink', 'unreadable')])
def test_changed_sources_cannot_admit_an_edit(owned, tmp_path, change, status):
    app, chat, rid, ws = owned
    target = ws.root / 'a.txt'
    target.write_text('old')
    read(owned)
    if change == 'change':
        target.write_text('external')
    else:
        target.unlink()
        if change == 'symlink':
            external = tmp_path / 'outside'
            external.write_text('secret')
            target.symlink_to(external)
    changes = app.provenance.audit(rid, ws)
    assert changes[0]['status'] == status
    if change != 'delete':
        with pytest.raises(PolicyError):
            write(owned)
        assert not app.store.actions(chat['id'])
    assert app.provenance.records(rid)[0]['status'] == status


def test_existing_write_requires_current_read_but_new_file_does_not(owned):
    app, chat, rid, ws = owned
    (ws.root / 'a.txt').write_text('old')
    with pytest.raises(PolicyError, match='Read it again'):
        write(owned, ident='missing-view')
    assert not app.store.actions(chat['id'])
    read(owned)
    proposal = write(owned, ident='fresh-write')
    assert proposal['status'] == 'awaiting_user_approval'
    assert app.provenance.latest(rid)[0]['basis'] == 'draft'
    assert (ws.root / 'a.txt').read_text() == 'old'
    assert write(owned, path='brand-new.txt', ident='new-file')['status'] == 'awaiting_user_approval'


def test_fresh_read_after_external_change_authorizes_only_new_view(owned):
    app, _, rid, ws = owned
    target = ws.root / 'a.txt'
    target.write_text('old')
    read(owned)
    target.write_text('external')
    with pytest.raises(PolicyError):
        write(owned, ident='stale')
    assert read(owned, ident='refresh')['source']['status'] == 'current'
    write(owned, ident='new-view')
    assert app.provenance.records(rid)[0]['status'] == 'changed'


def test_failed_file_tool_cannot_establish_a_model_view(owned):
    app, _, rid, ws = owned
    (ws.root / 'a.txt').write_text('old')
    read(owned)
    app.runs.end_tool(rid, 'read', {'error': 'post-read delivery failed'})
    with pytest.raises(PolicyError, match='Read it again'):
        write(owned)


def test_staged_draft_can_be_extended_without_claiming_disk_effect(owned):
    app, chat, rid, ws = owned
    proposal = write(owned, path='./new.txt', content='a')
    extended = app.execute('Append', {'file_path': './new.txt', 'content': 'b'}, ws, chat['id'], call_id='append')
    actions = {a['id']: a for a in app.store.actions(chat['id'])}
    assert actions[proposal['action_id']]['status'] == 'superseded'
    assert actions[extended['action_id']]['payload']['after'] == 'ab'
    assert not (ws.root / 'new.txt').exists()
    assert app.provenance.latest(rid)[0]['status'] == 'current'
    assert app.provenance.latest(rid)[0]['basis'] == 'draft'


@pytest.mark.parametrize('older_owner', [False, True])
def test_new_draft_preserves_same_project_pending_actions_it_does_not_own(owned, older_owner):
    app, chat, rid, ws = owned
    payload = Workspace(str(ws.root)).draft('new.txt', 'older proposed content')
    payload['permission_mode'] = 'ask'
    if older_owner:
        app.runs.transition(rid, 'waiting_input')
        previous = app.runs.create(chat['id'], 'Earlier review', app.runs.get(rid)['config'])
        app.runs.transition(previous, 'waiting_approval')
        app.runs.begin_tool(previous, 'old-write', 'Write', {'file_path': 'new.txt', 'content': payload['after']})
        old_action = app.store.action(chat['id'], 'edit', payload)
        app.runs.bind_action(previous, 'old-write', old_action, 'edit')
        app.runs.transition(rid, 'running')
    else:
        old_action = app.store.action(chat['id'], 'edit', payload)
    result = write(owned, path='new.txt', content='current proposed content', ident='current-write')
    rows = {row['id']: row for row in app.store.actions(chat['id'])}
    assert rows[old_action]['status'] == 'pending'
    assert rows[result['action_id']]['status'] == 'pending'
    assert rows[old_action]['payload']['after'] == 'older proposed content'
    assert not (ws.root / 'new.txt').exists()


def test_applied_readback_updates_only_owning_witness(owned):
    app, chat, rid, ws = owned
    proposal = write(owned, path='./new.txt', content='saved')
    app.runs.transition(rid, 'paused')
    app.active = app.active_run = None
    app.approve(chat['id'], proposal['action_id'])
    witness = app.provenance.latest(rid)[0]
    assert witness['basis'] == 'disk' and witness['status'] == 'current'
    assert witness['disk_hash'] == digest(b'saved')
    assert app.runs.tools(rid)[0]['result']['status'] == 'applied'
    assert not app.provenance.audit(rid, ws)


def test_draft_recovery_is_scoped_and_refuses_changed_baseline(owned):
    app, chat, rid, ws = owned
    write(owned, path='own.txt', content='draft')
    app.propose(chat['id'], 'edit', ws.draft('unowned.txt', 'unowned'))
    # No tool owner for this unrelated broker proposal.
    fresh = Workspace(ws.root)
    assert app.provenance.restore_drafts(rid, fresh) == 1
    assert set(fresh.drafts) == {'own.txt'} and fresh.read('own.txt') == 'draft'
    (ws.root / 'own.txt').write_text('external')
    assert app.provenance.restore_drafts(rid, Workspace(ws.root)) == 0


def test_root_replacement_does_not_silently_refresh_evidence(owned):
    app, _, rid, ws = owned
    (ws.root / 'a.txt').write_text('old')
    read(owned)
    old = ws.root.with_name('replaced')
    ws.root.rename(old)
    ws.root.mkdir()
    (ws.root / 'a.txt').write_text('old')
    assert app.provenance.audit(rid, Workspace(ws.root))[0]['status'] == 'unreadable'
    with pytest.raises(PolicyError, match='replaced'):
        ws.snapshot('a.txt')


def test_replaced_root_cannot_apply_a_previously_reviewed_draft(owned):
    _, _, _, ws = owned
    (ws.root / 'a.txt').write_text('old')
    data = ws.draft('a.txt', 'new')
    ws.root.rename(ws.root.with_name('replaced'))
    ws.root.mkdir()
    (ws.root / 'a.txt').write_text('old')
    with pytest.raises(PolicyError, match='replaced'):
        Workspace.apply(data)
    assert (ws.root / 'a.txt').read_text() == 'old'


def test_source_ledger_requires_admitted_receipt_and_selected_root(owned, tmp_path):
    app, _, rid, ws = owned
    (ws.root / 'a.txt').write_text('x')
    with pytest.raises(PolicyError, match='admitted tool'):
        app.provenance.observe(rid, 'fake', ws, 'a.txt', 'x')
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(PolicyError, match='selected workspace'):
        app.provenance.observe(rid, 'fake', Workspace(other), 'a.txt', 'x')


@pytest.mark.parametrize('limit', ['MAX_PATHS', 'MAX_WITNESSES'])
def test_bounded_source_ledger_fails_before_edit_proposal(owned, monkeypatch, limit):
    app, chat, _, _ = owned
    write(owned, path='first.txt')
    monkeypatch.setattr(provenance, limit, 1)
    with pytest.raises(PolicyError, match='bounded source ledger'):
        write(owned, path='second.txt', ident='second')
    assert len(app.store.actions(chat['id'])) == 1


def test_snapshot_rejects_large_files_private_paths_and_fifo(owned):
    _, _, _, ws = owned
    (ws.root / 'large').write_bytes(b'x' * (MAX_FILE + 1))
    with pytest.raises(PolicyError):
        ws.snapshot('large')
    with pytest.raises(PolicyError):
        ws.snapshot('.env', missing_ok=True)
    if hasattr(os, 'mkfifo'):
        os.mkfifo(ws.root / 'pipe')
        with pytest.raises(PolicyError):
            ws.snapshot('pipe')


def test_audit_metadata_has_no_source_body_and_updates_checked_time(owned):
    app, _, rid, ws = owned
    (ws.root / 'a.txt').write_text('PRIVATE SOURCE CONTENT')
    read(owned)
    before = app.provenance.records(rid)[0]['checked']
    assert not app.provenance.audit(rid, ws)
    records = app.provenance.records(rid)
    assert records[0]['checked'] >= before
    assert 'PRIVATE SOURCE CONTENT' not in json.dumps(records)
    assert 'root_ino' not in records[0]


def test_context_command_is_scoped_and_audits_sources(owned):
    app, chat, rid, ws = owned
    (ws.root / 'a.txt').write_text('old')
    read(owned)
    (ws.root / 'a.txt').write_text('changed')
    assert '1 need a fresh read' in command(app, chat['id'], '/context ' + rid)
    other = app.new_chat()
    with pytest.raises(ValueError, match='conversation'):
        command(app, other['id'], '/context ' + rid)
    with pytest.raises(ValueError, match='/context'):
        command(app, chat['id'], '/context too many')


def test_source_records_survive_restart_and_forget_cascades(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'a.txt').write_text('x')
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    chat = app.new_chat(str(root))
    def complete(messages, tools, cancel, token, effort):
        if len(app.runs.tools(app.active_run)) == 0:
            return {'role': 'assistant', 'tool_calls': [{'id': 'read', 'type': 'function', 'function': {'name': 'Read', 'arguments': json.dumps({'file_path': 'a.txt'})}}]}
        return {'role': 'assistant', 'content': 'Read the file.'}
    app.model.complete = complete
    app.work(chat, 'Inspect a.txt', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    before = app.provenance.records(rid)
    app.close()
    restarted = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        assert restarted.provenance.records(rid) == before
        restarted.delete_chat(chat['id'])
        assert not restarted.provenance.records(rid)
    finally:
        restarted.close()


def test_model_boundary_receives_freshness_status_after_external_change(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    target = root / 'a.txt'
    target.write_text('old')
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    attempts = []
    try:
        chat = app.new_chat(str(root))
        def complete(messages, tools, cancel, token, effort):
            attempts.append(copy.deepcopy(messages))
            if len(attempts) == 1:
                return {'role': 'assistant', 'tool_calls': [{'id': 'read', 'type': 'function', 'function': {'name': 'Read', 'arguments': json.dumps({'file_path': 'a.txt'})}}]}
            return {'role': 'assistant', 'content': 'External change requires a new read.'}
        def emit(event, data):
            if event == 'progress' and data.get('kind') == 'done' and data.get('tool') == 'Read':
                target.write_text('external')
        app.model.complete, app.emit = complete, emit
        app.work(chat, 'Inspect a.txt', False, [])
        audit = next(m for m in attempts[-1] if m.get('name') == 'pebble_source_status')
        assert 'changed' in audit['content'] and audit['_pebble_checkpoint']
        assert app.run_get(chat['id'], app.runs.list(chat['id'])[0]['id'])['sources'][0]['status'] == 'changed'
    finally:
        app.close()


def test_same_response_read_then_changed_edit_has_no_effect(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    target = root / 'a.txt'
    target.write_text('old')
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    attempts = []
    try:
        chat = app.new_chat(str(root))
        def complete(messages, tools, cancel, token, effort):
            attempts.append(True)
            if len(attempts) > 1:
                return {'role': 'assistant', 'content': 'The source needs to be refreshed.'}
            return {'role': 'assistant', 'tool_calls': [
                {'id': 'read', 'type': 'function', 'function': {'name': 'Read', 'arguments': json.dumps({'file_path': 'a.txt'})}},
                {'id': 'edit', 'type': 'function', 'function': {'name': 'Edit', 'arguments': json.dumps({'file_path': 'a.txt', 'old_string': 'old', 'new_string': 'new'})}}]}
        def emit(event, data):
            if event == 'progress' and data.get('kind') == 'done' and data.get('tool') == 'Read':
                target.write_text('external')
        app.model.complete, app.emit = complete, emit
        app.work(chat, 'Read and edit a.txt', False, [])
        rid = app.runs.list(chat['id'])[0]['id']
        assert not app.store.actions(chat['id']) and target.read_text() == 'external'
        rejected = next(t for t in app.runs.tools(rid) if t['name'] == 'Edit')
        assert 'Read it again' in rejected['result']['error'] and rejected['state'] == 'completed'
    finally:
        app.close()
