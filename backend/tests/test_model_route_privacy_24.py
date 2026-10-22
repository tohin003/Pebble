"""Phase 24 (t24-data, V1-002): local history reaches a cloud route only through the reviewed model handoff.

In 0.6.1, choosing a cloud model in a conversation that ran on this Mac sent the earlier on-device exchange with the
next message, and memories learned in on-device runs were recalled into cloud requests. Now the send goes ahead, and
the cloud request leaves out what came from this Mac (parent patches in App.work and App.recall_for_route, with
data_mirror.route_view; one runner line for action evidence):
- every on-device turn (C10's route classes, and turns stored before classes were recorded that an on-device run
  answered or started), the recorded results of actions an on-device run took, and memories learned on this Mac;
- the run carries one plain notice, with what the window needs to open the existing reviewed handoff, where the person
  can release those turns deliberately;
- every caller gets it (the window's send, the controller that scheduled jobs use, resumes, handoff starts).
The canary checks read the exact HTTP request body the cloud route was sent (Providers.http scripted). The device route
is a loopback profile answered by a scripted model. No network, no model call, no Keychain.
"""

import inspect
import json
import time
from types import SimpleNamespace

import pytest

from pebble import data_mirror
from pebble.app import App

CANARY = {'turn': 'locker-7Q4Z-turn', 'reply': 'locker-7Q4Z-reply', 'action': 'locker-7Q4Z-action',
          'memory': 'locker-7Q4Z-memory'}
DEVICE = {'id': 'device', 'kind': 'openai', 'name': 'On this Mac', 'url': 'http://127.0.0.1:9/v1', 'model': 'local',
          'context': 32768, 'budget': 32768, 'budget_version': 2, 'context_mode': 'full', 'tools': True}
LUNA = {'id': 'luna', 'name': 'Luna', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
        'model': 'gpt-6-luna', 'context': 32768, 'budget': 32768, 'tools': True, 'context_mode': 'custom',
        'budget_version': 2, 'context_version': 1, 'connection_id': 'luna'}
NOVA = {**LUNA, 'id': 'nova', 'name': 'Nova', 'url': 'https://other.invalid/backend-api/codex', 'connection_id': 'nova'}
LUNA_CLASS = 'cloud:https://chatgpt.invalid'
SETTLED = {'completed', 'failed', 'stopped', 'budget_exhausted', 'paused', 'interrupted', 'waiting_input',
           'waiting_approval', 'needs_reconciliation', 'verification_required'}
NOTICE = 'Earlier on-device messages were not shared with Luna. Review to include them.'


def _source(function):
    return inspect.getsource(function) + (function.__doc__ or '')


PATCHED_WORK = 'route_view' in _source(App.work)
PATCHED_RECALL = 'DEVICE' in _source(App.recall_for_route)
PATCHED_WORKING = "'working'" in _source(App.recall_for_route)


def needs(flag, what):
    return pytest.mark.xfail(not flag, strict=True, reason=f"needs the parent's t24-data app.py patch ({what})")


class Lines:
    def __init__(self, events):
        self.lines = [b'data: ' + json.dumps(e).encode() + b'\n' for e in events]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def readline(self, limit=-1):
        return self.lines.pop(0) if self.lines else b''


def reply_events(text):
    item = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text}]}
    shell = {'status': 'in_progress', 'output': []}
    return [{'type': 'response.created', 'response': shell}, {'type': 'response.in_progress', 'response': shell},
            {'type': 'response.output_text.delta', 'output_index': 0, 'item_id': 'msg_1', 'delta': text},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            {'type': 'response.completed', 'response': {'status': 'completed', 'output': [],
                                                        'usage': {'input_tokens': 100, 'output_tokens': 5}}}]


class Http:
    """The cloud route's transport: every request body exactly as it would leave the Mac."""

    def __init__(self):
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append({'url': req.full_url, 'body': bytes(req.data)})
        return Lines(reply_events('Done.'))


@pytest.fixture
def app(tmp_path, monkeypatch):
    events = []
    value = App(tmp_path / 'data', tmp_path / 'resources', lambda *event: events.append(event))
    value.store.set_setting('model_path', '')
    value.store.set_setting('model_profiles', [dict(DEVICE), dict(LUNA), dict(NOVA)])
    value.model.auth = SimpleNamespace(access=lambda pid: 'scripted-token')
    value.model.http = Http()
    cloud = value.model.complete

    def complete(messages, tools, cancel, token, effort):
        if value.model.current()['id'] == 'device':  # the on-device model, scripted
            token('Noted ' + CANARY['reply'] + '.')
            return {'role': 'assistant', 'content': 'Noted ' + CANARY['reply'] + '.'}
        return cloud(messages, tools, cancel, token, effort)
    monkeypatch.setattr(value.model, 'complete', complete)
    value.events = events
    yield value
    deadline = time.monotonic() + 10
    while value.active and time.monotonic() < deadline:
        time.sleep(0.02)
    value.close()


def settle(app, rid, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if app.runs.get(rid)['state'] in SETTLED and not app.active:
            return app.runs.get(rid)['state']
        time.sleep(0.02)
    raise AssertionError('the run did not settle: ' + app.runs.get(rid)['state'])


def send(app, cid, text, profile):
    app.model_select(profile)
    rid = app.send(cid, text)['run_id']
    assert settle(app, rid) == 'completed', app.runs.get(rid)
    return rid


def device_history(app):
    """A conversation answered on this Mac, with an action result and a memory learned there, all carrying canaries."""
    cid = app.new_chat()['id']
    rid = send(app, cid, 'My locker code is ' + CANARY['turn'] + '.', 'device')
    aid = app.store.action(cid, 'command', {'command': 'cat notes.txt', 'result': {'stdout': CANARY['action']}})
    app.store.query("UPDATE actions SET status='applied' WHERE id=?", (aid,))
    app.store.query('INSERT INTO run_action_owners VALUES(?,?,?,?)', (aid, rid, 'call-1', 'command'))
    app.memory.put('episodic', 'The locker code is ' + CANARY['memory'] + '.', 'global', cid, origin='conversation',
                   ident='episode-' + rid)
    return cid, rid, aid


def leaked(body):
    return sorted(name for name, canary in CANARY.items() if canary.encode() in body)


def finished_run(app, cid, profile):
    app.model_select(profile)
    rid = app.runs.create(cid, 'A task.', app.runtime_config(app.chat(cid), False))
    app.runs.transition(rid, 'running')
    app.runs.transition(rid, 'completed')
    return rid


# ---- which turns a request may carry (data_mirror.route_view; no App patch needed) --------------------------------

def test_route_view_leaves_out_what_came_from_this_mac(app):
    cid, rid, aid = device_history(app)
    messages = app.chat(cid)['messages']
    view = data_mirror.route_view(app.store, cid, messages, LUNA)
    assert len(messages) == 2 and view['messages'] == [] and view['withheld'] == [m['id'] for m in messages]
    assert view['actions'] == [aid] and view['source_run_id'] == rid and view['destination'] == LUNA_CLASS
    for route in (DEVICE, None):
        assert data_mirror.route_view(app.store, cid, messages, route)['messages'] == messages, 'this Mac sees all'
    # Turns stored before route classes were recorded: an on-device run's reply (its checkpoint names it) and the
    # request that started it (its goal) count as on-device too; another old turn stays.
    legacy = app.new_chat()['id']
    old = finished_run(app, legacy, 'device')
    for mid, role, text in (('m-goal', 'user', 'A task.'), ('m-reply', 'assistant', 'old reply'),
                            ('m-other', 'user', 'unrelated old turn')):
        app.store.query('INSERT INTO messages VALUES(?,?,?,?,?)', (mid, legacy, role, text, time.time()))
    app.store.query('UPDATE runs SET checkpoint=? WHERE id=?', (json.dumps({'message_id': 'm-reply'}), old))
    view = data_mirror.route_view(app.store, legacy, app.chat(legacy)['messages'], LUNA)
    assert view['withheld'] == ['m-goal', 'm-reply'] and [m['id'] for m in view['messages']] == ['m-other']


def test_route_view_keeps_cloud_history_commands_and_released_turns(app):
    cid = app.new_chat()['id']
    send(app, cid, 'Plan a trip to Lisbon.', 'luna')
    messages = app.chat(cid)['messages']
    for route in (LUNA, NOVA):
        view = data_mirror.route_view(app.store, cid, messages, route)
        assert view['messages'] == messages and view['withheld'] == [] and view['actions'] == []
    commands = app.new_chat()['id']
    app.model_select('luna')
    app.send(commands, '/help')  # 0.6.1 records a slash command's exchange as 'device'; no model produced it
    assert data_mirror.route_view(app.store, commands, app.chat(commands)['messages'], LUNA)['withheld'] == []
    handed, _, _ = device_history(app)
    app.store.set_setting('model_handoff_chats', {handed: {'allowed': [LUNA_CLASS, 'device'], 'source_chat': cid}})
    released = app.chat(handed)['messages']
    assert data_mirror.route_view(app.store, handed, released, LUNA)['messages'] == released, 'released in the review'
    assert data_mirror.route_view(app.store, handed, released, NOVA)['messages'] == [], 'only toward its destination'


# ---- the request, through App.work (parent patch) -------------------------------------------------------------------

@needs(PATCHED_WORK, 'App.work withholding')
def test_a_cloud_send_after_on_device_turns_goes_ahead_without_them(app):
    cid, device_run, _ = device_history(app)
    before = [(m['id'], m['content']) for m in app.chat(cid)['messages']]
    cloud_run = send(app, cid, 'Summarise what we discussed.', 'luna')
    [request] = app.model.http.requests
    assert b'Summarise what we discussed.' in request['body'] and leaked(request['body']) == []
    # The conversation keeps its on-device turns, each with the route class it had; the new turn is the cloud's.
    after = app.chat(cid)['messages']
    assert [(m['id'], m['content']) for m in after][:2] == before
    routes = {r['message_id']: r['route_class'] for r in app.store.query(
        'SELECT message_id,route_class FROM message_routes WHERE chat_id=?', (cid,))}
    assert [routes[m['id']] for m in after] == ['device', 'device', LUNA_CLASS, LUNA_CLASS]
    # One plain notice, with what the window needs to open the reviewed handoff.
    [note] = [json.loads(e['payload']) for e in app.store.query(
        "SELECT payload FROM run_events WHERE run_id=? AND kind='route_history_withheld'", (cloud_run,))]
    assert note == {'source_run_id': device_run, 'profile_id': 'luna', 'withheld': 2, 'actions': 1,
                    'destination_class': LUNA_CLASS, 'label': NOTICE}
    shown = [d for name, d in app.events if name == 'progress' and d.get('run_id') == cloud_run and d.get('handoff')]
    assert [d['label'] for d in shown] == [NOTICE] and shown[0]['kind'] == 'note'
    assert not any(CANARY['turn'] in json.dumps(d) for d in shown)


def test_the_reviewed_handoff_is_how_the_person_includes_them(app):  # a guard: holds before and after the patch
    cid, device_run, _ = device_history(app)
    send(app, cid, 'Summarise what we discussed.', 'luna')
    preview = app.model_handoff_preview(cid, device_run, 'luna', release=['device'])
    assert preview['released_classes'] == ['device'] and preview['send_count'] >= 2
    app.model_handoff_approve(cid, preview['plan_fingerprint'])
    started = app.model_handoff_start(cid, preview['plan_fingerprint'])
    assert settle(app, started['run_id']) == 'completed'
    body = app.model.http.requests[-1]['body']
    assert CANARY['turn'].encode() in body and CANARY['reply'].encode() in body, 'released deliberately, so sent'
    assert not app.store.query("SELECT 1 FROM run_events WHERE run_id=? AND kind='route_history_withheld'",
                               (started['run_id'],)), 'nothing is withheld that the person released'


@needs(PATCHED_WORK, 'App.work withholding')
def test_scheduled_and_internal_runs_withhold_the_same(app):
    cid, _, _ = device_history(app)
    app.model_select('luna')
    chat = app.chat(cid)
    rid = app.runs.create(cid, 'Summarise the notes.', app.runtime_config(chat, False))
    app.controller.launch(chat, 'Summarise the notes.', False, [], rid)  # what a scheduled job's fire calls
    assert settle(app, rid) == 'completed'
    [request] = app.model.http.requests
    assert b'Summarise the notes.' in request['body'] and leaked(request['body']) == []


def test_same_route_sends_and_on_device_destinations_are_unchanged(app):  # a guard: holds before and after the patch
    cid = app.new_chat()['id']
    first = send(app, cid, 'Plan a trip to Lisbon.', 'luna')
    second = send(app, cid, 'And Porto?', 'luna')
    assert b'Plan a trip to Lisbon.' in app.model.http.requests[-1]['body'], 'same-route history is sent as before'
    send(app, cid, 'Summarise it here on this Mac.', 'device')
    assert not app.store.query("SELECT 1 FROM run_events WHERE kind='route_history_withheld'")
    assert first != second


# ---- recall (parent patch) ------------------------------------------------------------------------------------------

@needs(PATCHED_RECALL, 'recall_for_route')
def test_memories_from_device_runs_are_withheld_from_cloud_recall(app):
    local = app.new_chat()['id']
    device_run = finished_run(app, local, 'device')
    learned = app.memory.put('episodic', 'The locker code is ' + CANARY['memory'] + '.', 'global', local,
                             origin='conversation', ident='episode-' + device_run)
    saved = app.memory.put('profile', 'I prefer short answers.', 'global', None, origin='user')
    other = app.new_chat()['id']
    cloud_run = finished_run(app, other, 'luna')
    items = [{'id': learned, 'content': 'x'}, {'id': saved, 'content': 'I prefer short answers.'}]
    assert [i['id'] for i in app.recall_for_route(other, items, cloud_run)] == [saved]
    events = app.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='handoff_recall_withheld'", (cloud_run,))
    assert json.loads(events[-1]['payload'])['count'] == 1 and CANARY['memory'] not in json.dumps(events)
    third = app.new_chat()['id']
    device_later = finished_run(app, third, 'device')
    assert [i['id'] for i in app.recall_for_route(third, items, device_later)] == [learned, saved], 'on this Mac all stay'
    # A record learned on one cloud route reaches another as before (test_phase15_routes pins this for ordinary
    # conversations); only what was learned on this Mac is held back.
    cloud_chat = app.new_chat()['id']
    cloud_learned = app.memory.put('episodic', 'The ferry leaves at noon.', 'global', cloud_chat, origin='conversation',
                                   ident='episode-' + finished_run(app, cloud_chat, 'luna'))
    nova_run = finished_run(app, app.new_chat()['id'], 'nova')
    assert [i['id'] for i in app.recall_for_route(other, [{'id': cloud_learned, 'content': 'x'}], nova_run)] == [cloud_learned]
    # End to end: the record is recalled for the question, and the cloud request body never carries it.
    recalled = app.memory.recall('What is the locker code?', other, None, window=32768)
    assert learned in [i['id'] for i in recalled['items']], 'positive control: recall finds the record'
    send(app, other, 'What is the locker code?', 'luna')
    assert CANARY['memory'].encode() not in app.model.http.requests[-1]['body']


@needs(PATCHED_WORKING, 'recall_for_route, the working record')
def test_an_on_device_runs_working_record_is_withheld_from_cloud_recall(app, monkeypatch):
    cid, device_run, _ = device_history(app)
    assert data_mirror.working_origin(app.store, cid) == device_run, "the on-device run's working record"
    assert data_mirror.working_origin(app.store, app.new_chat()['id']) is None
    # A later run without start-time consent for the working layer writes no record of its own, so recall pins the
    # on-device run's record, whose goal is the on-device turn.
    consent = app.learning.allowed_run
    monkeypatch.setattr(app.learning, 'allowed_run', lambda rid, kind=None: kind != 'working' and consent(rid, kind))
    pinned = app.memory._working_item(cid, 32768, 'Summarise what we discussed.')
    assert CANARY['turn'] in pinned['content'], 'positive control: recall would carry the on-device goal'
    cloud_run = send(app, cid, 'Summarise what we discussed.', 'luna')
    body = app.model.http.requests[-1]['body']
    assert b'Summarise what we discussed.' in body and leaked(body) == []
    [event] = app.store.query("SELECT payload FROM run_events WHERE run_id=? AND kind='handoff_recall_withheld'",
                              (cloud_run,))
    assert json.loads(event['payload'])['count'] >= 1
    # For a run on this Mac the same record stays.
    assert app.recall_for_route(cid, [pinned], finished_run(app, cid, 'device')) == [pinned]
