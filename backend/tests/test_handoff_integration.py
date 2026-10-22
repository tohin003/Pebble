"""Exercise the pure packet builder against real runtime record shapes."""

import json

import pytest

from pebble.app import App
from pebble import handoff
from pebble.runtime_commands import command
from pebble.policy import digest


def calls(*specs):
    return {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
        for ident, name, args in specs]}


@pytest.fixture
def app(tmp_path):
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    yield value
    value.close()


def pending(app, tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'a.txt').write_text('old')
    chat = app.new_chat(str(root))
    count = []
    def complete(*args):
        count.append(True)
        if len(count) == 1:
            return calls(('read', 'Read', {'file_path': 'a.txt'}))
        return calls(('write', 'Write', {'file_path': 'a.txt', 'content': 'unapplied new content'}),
            ('ask', 'AskUserQuestion', {'required': True, 'questions': json.dumps([
                {'id': 'scope', 'title': 'Which scope?', 'options': []}])}))
    app.model.complete = complete
    app.work(chat, 'Read, update a.txt and ask for scope. Preserve 漢字.', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    return chat, rid, root


def test_settlement_saves_exact_goal_unresolved_effect_and_native_question(app, tmp_path):
    chat, rid, root = pending(app, tmp_path)
    packet = app.run_handoff(chat['id'], rid, refresh=False)
    assert packet and packet['manifest']['status'] == 'complete'
    assert packet['data']['goal']['text'] == app.runs.get(rid)['goal']
    assert len(packet['data']['unresolved']['effects']) == 1
    assert len(packet['data']['unresolved']['waiting_questions']) == 1
    assert packet['data']['authority']['tool_replay'] == 'never'
    assert (root / 'a.txt').read_text() == 'old'
    assert 'unapplied new content' not in json.dumps(packet)
    assert 'Which scope?' not in json.dumps(packet['manifest'])
    assert app.run_get(chat['id'], rid)['handoff']['manifest'] == packet['manifest']


def test_refresh_reaudits_sources_and_retains_exact_pending_steering(app, tmp_path):
    chat, rid, root = pending(app, tmp_path)
    app.interactions.steer(rid, 'Keep the 漢字 constraints\nDo not publish.', 'followup')
    (root / 'a.txt').write_text('external')
    packet = app.run_handoff(chat['id'], rid)
    assert packet['data']['inputs'][0]['payload'] == {'text': 'Keep the 漢字 constraints\nDo not publish.'}
    assert packet['data']['inputs'][0]['origin'] == 'human'
    assert packet['data']['inputs'][0]['status'] == 'pending'
    assert packet['manifest']['counts']['sources']['stale'] == 2


def test_handoff_scope_and_host_command_never_infer(app, tmp_path):
    chat, rid, _ = pending(app, tmp_path)
    app.model.complete = lambda *args: pytest.fail('Inspection attempted inference')
    assert 'grants no permission' in command(app, chat['id'], '/handoff ' + rid)
    other = app.new_chat()
    with pytest.raises(ValueError, match='conversation'):
        app.run_handoff(other['id'], rid)
    with pytest.raises(ValueError, match='conversation'):
        command(app, other['id'], '/handoff ' + rid)
    with pytest.raises(ValueError, match='boolean'):
        app.run_handoff(chat['id'], rid, refresh='false')


def test_missing_workspace_marks_sources_unreadable_without_losing_packet(app, tmp_path):
    chat, rid, root = pending(app, tmp_path)
    root.rename(root.with_name('moved-away'))
    packet = app.run_handoff(chat['id'], rid)
    assert all(s['status'] == 'unreadable' for s in packet['data']['sources'])
    assert packet['data']['goal']['text'] == app.runs.get(rid)['goal']


def test_handoff_failure_cannot_overturn_committed_tool_or_run(app, monkeypatch):
    def fail(*args, **kwargs):
        raise handoff.HandoffIntegrityError('bad_field', 'sources')
    monkeypatch.setattr(handoff, 'make_handoff', fail)
    chat = app.new_chat()
    attempts = []
    def complete(*args):
        attempts.append(True)
        if len(attempts) == 1:
            return calls(('diagram', 'UIRender', {'title': 'Fixture', 'kind': 'diagram',
                'data': json.dumps({'nodes': [{'id': 'a', 'label': 'A'}], 'edges': []})}))
        return {'role': 'assistant', 'content': 'Done.'}
    app.model.complete = complete
    app.work(chat, 'Draw a diagram', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'completed'
    assert app.runs.tools(rid)[0]['state'] == 'completed' and len(app.artifacts.list(chat['id'])) == 1
    assert any(e['kind'] == 'handoff_failed' and e['payload']['reason'] == 'bad_field' for e in app.runs.events(rid))


def test_packet_restart_is_independent_of_optional_memory_and_delete_cascades(tmp_path):
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    value.store.set_setting('memory_enabled', False)
    chat, rid, _ = pending(value, tmp_path)
    packet = value.run_handoff(chat['id'], rid, refresh=False)
    value.close()
    restarted = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    try:
        assert restarted.run_handoff(chat['id'], rid, refresh=False) == packet
        assert not restarted.memory.enabled()
        restarted.delete_chat(chat['id'])
        assert not restarted.runs.handoff(rid) and not restarted.provenance.records(rid)
    finally:
        restarted.close()


@pytest.mark.parametrize('baseline,current', [(digest(b'old'), digest(b'old')), (None, None)])
def test_current_draft_witness_refers_to_baseline_not_unapplied_file_hash(app, tmp_path, baseline, current):
    chat, rid, _ = pending(app, tmp_path)
    witness = app.provenance.latest(rid)[0]
    witness.update(basis='draft', status='current', disk_hash=baseline, current_hash=current)
    packet = handoff.make_handoff(app.runs.get(rid), app.runs.tools(rid), sources=[witness])
    assert packet.data['sources'][0]['file_hash'] != baseline
    assert packet.data['sources'][0]['status'] == 'current'
    witness['current_hash'] = digest(b'changed')
    with pytest.raises(handoff.HandoffIntegrityError, match='freshness'):
        handoff.make_handoff(app.runs.get(rid), app.runs.tools(rid), sources=[witness])


def test_source_packet_rejects_duplicate_witness_and_nonfile_receipt(app, tmp_path):
    _, rid, _ = pending(app, tmp_path)
    witness = app.provenance.latest(rid)[0]
    with pytest.raises(handoff.HandoffIntegrityError) as exc:
        handoff.make_handoff(app.runs.get(rid), app.runs.tools(rid), sources=[witness, witness])
    assert exc.value.reason == 'duplicate_id'
    witness['receipt_id'] = '2:ask'
    with pytest.raises(handoff.HandoffIntegrityError) as exc:
        handoff.make_handoff(app.runs.get(rid), app.runs.tools(rid), sources=[witness])
    assert exc.value.reason == 'impossible_source'
