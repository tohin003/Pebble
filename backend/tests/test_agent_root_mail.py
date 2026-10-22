"""Root mailbox reads: ordered full-body pages, receipt-bound acknowledgement.

The App, root tool journal, durable tree and manager are the checked-in code;
the scheduler thread is disabled and no provider, model HTTP, credential,
shell or plugin operation is used.
"""

import copy
import json
from types import SimpleNamespace

import pytest

from pebble.agent_manager import Agents
from pebble.app import App
from pebble.policy import PolicyError, Workspace


PROFILE = {'id': 'fixture-cloud', 'kind': 'openai', 'name': 'Fixture provider',
           'url': 'https://unused.invalid', 'model': 'fixture', 'context': 8192,
           'budget': 4096, 'tools': True}
BODY = ''.join('line %04d: ordinary delegated finding.\n' % i for i in range(60))


def boot(tmp_path, monkeypatch):
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)

    def root_complete(*args, **kwargs):
        raise AssertionError('Root mail fixtures must not make a root model request.')

    monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(PROFILE))
    monkeypatch.setattr(app.model, 'complete', root_complete)
    monkeypatch.setattr(app.guard, 'handle', lambda *args, **kwargs: {'action': 'allow', 'reason': 'Fixture Guard admission'})
    return app


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(Agents, 'start', lambda self: None)
    project = tmp_path / 'project'
    project.mkdir()
    app = boot(tmp_path, monkeypatch)
    try:
        yield SimpleNamespace(app=app, project=project, tree=app.agents.tree)
    finally:
        app.active = app.active_run = None
        app.close()


def start_root(app, project):
    for live in app.runs.list():
        if live['state'] in {'queued', 'running', 'retry_wait'}:
            app.runs.transition(live['id'], 'waiting_input')
    chat = app.new_chat(str(project))
    config = app.runtime_config(chat, False, {'max_steps': 64, 'max_seconds': 180})
    rid = app.runs.create(chat['id'], 'Read delegated mail in full before reporting.', config)
    app.runs.transition(rid, 'running')
    app.active, app.active_run = chat['id'], rid
    app.task_policy = copy.deepcopy(config['permissions'])
    return chat['id'], rid


def root_tool(app, project, cid, rid, name, args, ident):
    app.active, app.active_run = cid, rid
    return app.execute(name, args, Workspace(project), cid, call_id=ident)


def claimed(app, cid, rid, name, tools):
    node = app.agents.tree.spawn(cid, rid, None, 'Report scoped findings.', name, list(tools),
                                 {'max_steps': 2, 'max_seconds': 10, 'model_tokens': 16384})
    owner = 'worker-' + name
    app.agents.tree.claim(cid, rid, node['id'], 1, owner)
    return SimpleNamespace(id=node['id'], owner=owner)


def mail_state(app, message_id):
    acked = app.store.query('SELECT acked FROM agent_messages WHERE id=?', (message_id,))[0]['acked']
    rows = app.store.query('SELECT cursor FROM agent_mail_reads WHERE message_id=?', (message_id,))
    return acked, rows[0]['cursor'] if rows else 0


def journal(app, rid, call_id):
    return next(t for t in app.runs.tools(rid) if t['call_id'] == call_id)


def test_root_reads_full_mail_in_order_and_acks_only_with_the_final_receipt(host):
    app, project, tree = host.app, host.project, host.tree
    cid, rid = start_root(app, project)
    child = claimed(app, cid, rid, 'reporter', ['Read', 'SendAgentMessage'])
    mid = tree.send(cid, rid, child.id, rid, BODY, generation=1, owner=child.owner)['id']
    assert 'ReadAgentMessage' in app.agents.root_tools(cid, rid)
    # A bounded preview and a wake-up never consume the message.
    view = root_tool(app, project, cid, rid, 'ReadAgentTree', {}, 'tree')
    assert [(m['id'], len(m['preview'])) for m in view['mailbox']['messages']] == [(mid, 160)]
    assert root_tool(app, project, cid, rid, 'WaitForAgents', {'agent_ids': json.dumps([child.id])}, 'wait-unread')['message_ids'] == [mid]
    assert mail_state(app, mid) == (0, 0)

    first = root_tool(app, project, cid, rid, 'ReadAgentMessage', {'message_id': mid}, 'read-1')
    assert first['offset'] == 0 and first['next_offset'] == len(first['untrusted_body']) < len(BODY)
    assert (first['acknowledged'], first['origin'], first['authorization']) == (False, 'agent', 'none')
    assert journal(app, rid, 'read-1')['result'] == first
    assert mail_state(app, first['message_id']) == (0, first['next_offset'])
    # Out-of-order offsets fail without moving the cursor; no skip to the end.
    for ident, offset in (('behind', 0), ('ahead', first['next_offset'] + 1), ('end', len(BODY))):
        with pytest.raises(PolicyError, match='continue this message at offset'):
            root_tool(app, project, cid, rid, 'ReadAgentMessage', {'message_id': mid, 'offset': offset}, ident)
        assert 'error' in journal(app, rid, ident)['result']
    # The cached receipt replays without advancing again.
    assert root_tool(app, project, cid, rid, 'ReadAgentMessage', {'message_id': mid}, 'read-1') == first
    assert mail_state(app, mid) == (0, first['next_offset'])

    pages = [first]
    while not pages[-1]['acknowledged']:
        assert mail_state(app, mid)[0] == 0
        ident = 'read-' + str(len(pages) + 1)
        pages.append(root_tool(app, project, cid, rid, 'ReadAgentMessage', {'message_id': mid, 'offset': pages[-1]['next_offset']}, ident))
        assert journal(app, rid, ident)['result'] == pages[-1]
    assert len(pages) > 2 and ''.join(p['untrusted_body'] for p in pages) == BODY
    assert pages[-1]['next_offset'] is None and mail_state(app, mid) == (1, len(BODY))
    with pytest.raises(PolicyError, match='already read'):
        root_tool(app, project, cid, rid, 'ReadAgentMessage', {'message_id': mid}, 'read-again')
    assert 'ReadAgentMessage' not in app.agents.root_tools(cid, rid)
    # A fresh root wait no longer wakes on consumed mail.
    consumed = root_tool(app, project, cid, rid, 'WaitForAgents', {'agent_ids': json.dumps([child.id])}, 'wait-consumed')
    assert consumed['status'] == 'awaiting_agent_results' and consumed['required'] is True
    assert not app.store.query("SELECT * FROM run_inbox WHERE run_id=? AND origin='human'", (rid,))
    assert not app.store.query("SELECT * FROM agent_inputs WHERE origin='human'")


def test_completed_reviewed_child_mail_still_blocks_completion_until_committed_read(host):
    app, project, tree = host.app, host.project, host.tree
    cid, rid = start_root(app, project)
    child = claimed(app, cid, rid, 'finished-reporter', ['SendAgentMessage'])
    args = {'agent_id': rid, 'text': 'A final scoped finding.'}
    tree.begin_tool(cid, rid, child.id, 1, child.owner, 'send-finding', 'SendAgentMessage', args)
    sent = tree.send(cid, rid, child.id, rid, args['text'], generation=1, owner=child.owner)
    mid = sent['id']
    tree.end_tool(cid, rid, child.id, 1, child.owner, 'send-finding', sent)
    tree.settle(cid, rid, child.id, 1, 'completed', {'text': 'Report sent.'}, ['send-finding'], owner=child.owner)
    tree.review(cid, rid, rid, child.id, 1, ['send-finding'])
    gate = app.agents.completion(cid, rid)
    assert gate['state'] == 'verification_required' and 'Unread delegated mail' in gate['issues'][0]
    raw = tree.read_mail(cid, rid, mid)
    assert raw['next_offset'] is None and raw['acknowledged'] is False and mail_state(app, mid) == (0, 0)
    read = root_tool(app, project, cid, rid, 'ReadAgentMessage', {'message_id': mid}, 'read-final-finding')
    assert read['acknowledged'] is True
    assert app.agents.completion(cid, rid) == {'state': 'completed', 'issues': [], 'repairable': False}


def test_foreign_child_revoked_and_child_actor_mail_reads_are_rejected(host):
    app, project, tree = host.app, host.project, host.tree
    other_cid, other_rid = start_root(app, project)
    other = claimed(app, other_cid, other_rid, 'other', ['SendAgentMessage'])
    foreign = tree.send(other_cid, other_rid, other.id, other_rid, 'Other tree report.', generation=1, owner=other.owner)['id']
    cid, rid = start_root(app, project)
    child = claimed(app, cid, rid, 'sender', ['SendAgentMessage'])
    peer = claimed(app, cid, rid, 'peer', ['SendAgentMessage'])
    to_child = tree.send(cid, rid, rid, child.id, 'Root note for the child.')['id']
    peer_mail = tree.send(cid, rid, peer.id, child.id, 'Peer note.', generation=1, owner=peer.owner)['id']
    assert 'ReadAgentMessage' not in app.agents.root_tools(cid, rid)
    for ident, message in (('foreign', foreign), ('to-child', to_child), ('peer', peer_mail), ('unknown', 'missing-message')):
        with pytest.raises(PolicyError, match='root mailbox'):
            root_tool(app, project, cid, rid, 'ReadAgentMessage', {'message_id': message}, 'read-' + ident)
    # Child delivery is unchanged: the child still receives its own unread mail.
    assert {m['id'] for m in tree.deliver(cid, rid, child.id)['messages']} == {to_child, peer_mail}
    with pytest.raises(ValueError, match='conversation'):
        tree.read_mail(other_cid, rid, to_child)
    # Children can neither hold nor call the root mail tool.
    worker = {'cid': cid, 'rid': rid, 'id': peer.id, 'generation': 1, 'owner': peer.owner}
    with pytest.raises(PolicyError, match='Only the root'):
        app.agents.tool(cid, rid, peer.id, 'ReadAgentMessage', {'message_id': to_child}, 'child-read', worker=worker)
    with pytest.raises(PolicyError, match='outside child capabilities'):
        app.agents.child_tool(worker, 'ReadAgentMessage', {'message_id': to_child}, 'child-tool')
    with pytest.raises(ValueError, match='subset'):
        tree.spawn(cid, rid, None, 'Read root mail.', 'mail-reader', ['ReadAgentMessage'], {'max_steps': 1, 'max_seconds': 5, 'model_tokens': 4096})

    revoked = tree.send(cid, rid, child.id, rid, 'Report before stop.', generation=1, owner=child.owner)['id']
    assert 'ReadAgentMessage' in app.agents.root_tools(cid, rid)
    root_tool(app, project, cid, rid, 'StopAgent', {'agent_id': child.id}, 'stop')
    with pytest.raises(PolicyError, match='root mailbox'):
        root_tool(app, project, cid, rid, 'ReadAgentMessage', {'message_id': revoked}, 'read-revoked')
    assert 'ReadAgentMessage' not in app.agents.root_tools(cid, rid)
    assert not app.store.query('SELECT * FROM agent_mail_reads')
    assert mail_state(app, foreign) == (0, 0)


def test_stale_uncertain_forged_or_revoked_receipts_never_advance_or_acknowledge(host):
    app, project, tree = host.app, host.project, host.tree
    cid, rid = start_root(app, project)
    child = claimed(app, cid, rid, 'sender', ['SendAgentMessage'])
    leaving = claimed(app, cid, rid, 'leaving', ['SendAgentMessage'])

    def page(call_id, message_id, name='ReadAgentMessage'):
        # The root path minus hooks: journal marker, tool result, then end_tool.
        app.runs.begin_tool(rid, call_id, name, {'message_id': message_id})
        return {**app.agents.tool(cid, rid, rid, 'ReadAgentMessage', {'message_id': message_id}, call_id), 'receipt_id': call_id}

    short = tree.send(cid, rid, child.id, rid, 'Short final report.', generation=1, owner=child.owner)['id']
    first, duplicate = page('first', short), page('duplicate', short)
    app.runs.end_tool(rid, 'first', first)
    assert first['acknowledged'] is True and mail_state(app, short) == (1, len('Short final report.'))
    app.runs.end_tool(rid, 'duplicate', duplicate)
    assert duplicate['status'] == 'stale' and duplicate['untrusted_body'] is None and duplicate['acknowledged'] is False
    assert journal(app, rid, 'duplicate')['result']['status'] == 'stale'
    assert app.store.query('SELECT call_id FROM agent_mail_reads WHERE message_id=?', (short,)) == [{'call_id': 'first'}]

    second = tree.send(cid, rid, child.id, rid, 'Second report.', generation=1, owner=child.owner)['id']
    uncertain = page('uncertain', second)
    app.runs.end_tool(rid, 'uncertain', uncertain, uncertain=True)
    assert journal(app, rid, 'uncertain')['state'] == 'uncertain' and mail_state(app, second) == (0, 0)
    tampered = page('tampered', second)
    tampered['untrusted_body'] = 'Different text.'
    app.runs.end_tool(rid, 'tampered', tampered)
    assert tampered['status'] == 'stale' and mail_state(app, second) == (0, 0)
    # A mail-shaped result under another tool's receipt is never an acknowledgement.
    shaped = page('preview', second, name='ReadAgentTree')
    app.runs.end_tool(rid, 'preview', shaped)
    assert mail_state(app, second) == (0, 0)

    late = tree.send(cid, rid, leaving.id, rid, 'Report before stop.', generation=1, owner=leaving.owner)['id']
    pending = page('revoked', late)
    tree.cancel(cid, rid, rid, leaving.id)
    app.runs.end_tool(rid, 'revoked', pending)
    assert pending['status'] == 'stale'
    assert app.store.query('SELECT acked,revoked FROM agent_messages WHERE id=?', (late,)) == [{'acked': 0, 'revoked': 1}]
    # The real root path still reads the untouched message from its start.
    read = root_tool(app, project, cid, rid, 'ReadAgentMessage', {'message_id': second}, 'second')
    assert (read['offset'], read['untrusted_body'], read['acknowledged']) == (0, 'Second report.', True)
    assert mail_state(app, second) == (1, len('Second report.'))


def test_restart_keeps_committed_cursor_and_replays_cached_page_without_advancing(tmp_path, monkeypatch):
    monkeypatch.setattr(Agents, 'start', lambda self: None)
    project = tmp_path / 'project'
    project.mkdir()
    app = boot(tmp_path, monkeypatch)
    try:
        cid, rid = start_root(app, project)
        child = claimed(app, cid, rid, 'reporter', ['SendAgentMessage'])
        mid = app.agents.tree.send(cid, rid, child.id, rid, BODY, generation=1, owner=child.owner)['id']
        first = root_tool(app, project, cid, rid, 'ReadAgentMessage', {'message_id': mid}, 'read-1')
        # The service stops after a page was produced but before its receipt committed.
        app.runs.begin_tool(rid, 'read-crash', 'ReadAgentMessage', {'message_id': mid})
        app.agents.tool(cid, rid, rid, 'ReadAgentMessage', {'message_id': mid}, 'read-crash')
    finally:
        app.active = app.active_run = None
        app.close()
    restarted = boot(tmp_path, monkeypatch)
    try:
        assert journal(restarted, rid, 'read-crash')['state'] == 'uncertain'
        assert mail_state(restarted, mid) == (0, first['next_offset'])
        restarted.runs.transition(rid, 'running')
        assert root_tool(restarted, project, cid, rid, 'ReadAgentMessage', {'message_id': mid}, 'read-1') == first
        assert mail_state(restarted, mid) == (0, first['next_offset'])
        second = root_tool(restarted, project, cid, rid, 'ReadAgentMessage', {'message_id': mid}, 'read-2')
        end = second['offset'] + len(second['untrusted_body'])
        assert second['offset'] == first['next_offset'] and second['untrusted_body'] == BODY[second['offset']:end]
        assert mail_state(restarted, mid) == (0, end)
    finally:
        restarted.active = restarted.active_run = None
        restarted.close()
