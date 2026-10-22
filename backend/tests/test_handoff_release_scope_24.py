"""Phase 24 (parent; t24-sec-recheck-arch-02, reproduced on the final tree): a reviewed handoff's release of 'device' covers
what the person reviewed, not the whole device class for the handoff conversation's future.

Before the fix, data_mirror.route_view sent every later on-device turn of a handoff conversation to the cloud, with no
withheld notice, and recall_for_route recalled on-device memories from any conversation there. These tests reuse the
privacy suite's fixtures (the real App, a scripted on-device model, the cloud route's exact request bodies). No network,
no model call, no Keychain.
"""
from test_model_route_privacy_24 import CANARY, app, device_history, finished_run, send, settle  # noqa: F401

LATER = 'PIN-5521-after-the-review'


def handoff(app):
    cid, device_run, _ = device_history(app)
    preview = app.model_handoff_preview(cid, device_run, 'luna', release=['device'])
    app.model_handoff_approve(cid, preview['plan_fingerprint'])
    started = app.model_handoff_start(cid, preview['plan_fingerprint'])
    assert settle(app, started['run_id']) == 'completed'
    return cid, started


def test_the_reviewed_messages_still_reach_the_destination(app):
    _, started = handoff(app)
    first = app.model.http.requests[-1]['body']
    assert CANARY['turn'].encode() in first, 'the person released the reviewed on-device turn'
    scope = app.store.setting('model_handoff_chats')[started['chat_id']]
    assert scope['released_messages'] and set(scope['released_messages']) <= {m['id'] for m in app.chat(started['chat_id'])['messages']}


def test_a_turn_made_on_this_mac_after_the_handoff_is_withheld_with_the_notice(app):
    _, started = handoff(app)
    handed = started['chat_id']
    send(app, handed, 'Between us: my PIN is ' + LATER + '.', 'device')  # on this Mac, after the review
    cloud = send(app, handed, 'Thanks, carry on.', 'luna')
    body = app.model.http.requests[-1]['body']
    assert LATER.encode() not in body, 'a later on-device turn was never reviewed'
    assert CANARY['turn'].encode() in body, 'the reviewed turn stays released'
    assert app.store.query("SELECT 1 FROM run_events WHERE run_id=? AND kind='route_history_withheld'", (cloud,))


def test_an_on_device_memory_from_another_conversation_is_not_released(app):
    source, started = handoff(app)
    handed = started['chat_id']
    elsewhere = app.new_chat()['id']  # answered on this Mac, never part of the review
    other_run = finished_run(app, elsewhere, 'device')
    secret = 'SAFE-9931-unrelated'
    learned = app.memory.put('episodic', 'The safe code is ' + secret + '.', 'global', elsewhere,
                             origin='conversation', ident='episode-' + other_run)
    handed_run = finished_run(app, handed, 'luna')
    assert app.recall_for_route(handed, [{'id': learned, 'content': 'x'}], handed_run) == []
    send(app, handed, 'What is the safe code?', 'luna')
    assert secret.encode() not in app.model.http.requests[-1]['body']
    # The reviewed source conversation's on-device record is still released (test_phase15_security g5 keeps this).
    source_record = 'episode-' + app.store.query("SELECT id FROM runs WHERE chat_id=? ORDER BY created LIMIT 1", (source,))[0]['id']
    assert [i['id'] for i in app.recall_for_route(handed, [{'id': source_record, 'content': 'x'}], handed_run)] == [source_record]


def test_control_the_same_later_turn_is_withheld_in_an_ordinary_conversation(app):
    cid = app.new_chat()['id']
    send(app, cid, 'Plan a trip to Lisbon.', 'luna')
    send(app, cid, 'Between us: my PIN is ' + LATER + '.', 'device')
    send(app, cid, 'Thanks, carry on.', 'luna')
    assert LATER.encode() not in app.model.http.requests[-1]['body']


# ---- t24-sec-recheck-arch-03 (the verification follow-up found it after the first fix): the release is bound to the runs
# reviewed, not to the source conversation; a project shares learned records across its conversations ----

def test_a_later_on_device_run_in_the_source_conversation_is_not_released(app):
    source, started = handoff(app)
    handed = started['chat_id']
    assert set(app.store.setting('model_handoff_chats')[handed]['released_runs']) == {
        r['id'] for r in app.store.query('SELECT id FROM runs WHERE chat_id=?', (source,))}
    later_run = finished_run(app, source, 'device')  # back in the source conversation, on this Mac, after the review
    secret = 'VAULT-8820-after-review'
    learned = app.memory.put('episodic', 'The vault code is ' + secret + '.', 'global', source,
                             origin='conversation', ident='episode-' + later_run)
    handed_run = finished_run(app, handed, 'luna')
    assert app.recall_for_route(handed, [{'id': learned, 'content': 'x'}], handed_run) == []
    send(app, handed, 'What is the vault code?', 'luna')
    assert secret.encode() not in app.model.http.requests[-1]['body']


def test_in_a_project_the_episode_of_a_later_source_turn_does_not_reach_the_cloud(app, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    source = app.new_chat()['id']
    app.set_workspace(source, str(project))
    first = send(app, source, 'Plan the garden beds.', 'device')
    preview = app.model_handoff_preview(source, first, 'luna', release=['device'])
    app.model_handoff_approve(source, preview['plan_fingerprint'])
    started = app.model_handoff_start(source, preview['plan_fingerprint'])
    assert settle(app, started['run_id']) == 'completed'
    handed = started['chat_id']
    assert app.chat(handed)['workspace'] == app.chat(source)['workspace']
    send(app, source, 'Note for later: the vault code is VAULT-7710-after-review.', 'device')
    question = 'What did I note for later about the vault code?'
    send(app, handed, question, 'luna')
    assert b'VAULT-7710-after-review' not in app.model.http.requests[-1]['body']
    # Control: an ordinary cloud conversation in the same project withholds the same record (V1-002).
    ordinary = app.new_chat()['id']
    app.set_workspace(ordinary, str(project))
    send(app, ordinary, question, 'luna')
    assert b'VAULT-7710-after-review' not in app.model.http.requests[-1]['body']


def test_a_handoff_of_a_handoff_does_not_widen_to_the_first_source(app):
    source, started = handoff(app)
    h1 = started['chat_id']
    device_run = app.store.query('SELECT id FROM runs WHERE chat_id=? ORDER BY created LIMIT 1', (source,))[0]['id']
    h1_device = send(app, h1, 'An aside on this Mac in H1.', 'device')
    preview = app.model_handoff_preview(h1, h1_device, 'luna', release=['device'])
    app.model_handoff_approve(h1, preview['plan_fingerprint'])
    second = app.model_handoff_start(h1, preview['plan_fingerprint'])
    assert settle(app, second['run_id']) == 'completed'
    h2 = second['chat_id']
    h2_run = finished_run(app, h2, 'luna')
    assert app.recall_for_route(h2, [{'id': 'episode-' + device_run, 'content': 'x'}], h2_run) == []
    send(app, h2, 'Between us, in H2 on this Mac: CHAIN-4471.', 'device')
    send(app, h2, 'Carry on.', 'luna')
    assert b'CHAIN-4471' not in app.model.http.requests[-1]['body']
