"""Phase 24 track t24-agent: a granted window or tab reaches the model (V1-007), and a screenshot is never kept
(V1-059).

In 0.6.1 the screen tools were decided once, at run start, and restored from the snapshot on resume, so a grant made
while the run waited (or after it started) never reached the model, and the session ID was never told. Now each step
above 8K asks ScreenSessions.session_for_run (t24-browser) and offers ScreenObserve/ScreenAct only with a live granted
session, the screen permission and UI intent (screen_session.offered_tools, unchanged). The session is announced once,
as data, in a HOST SCREEN SESSION line at the next model boundary; tool_authz and the screen permission still decide
every call. A screenshot sent to the model stays in the live run only: the checkpoint keeps a placeholder with its
SHA-256.

Uses t24-browser's fake AX window and grant route; scripted models; no network.
"""
import base64
import copy
import hashlib
import json
import time

from pebble import runner
from pebble.screen_session import grant_route
from test_screen_offer_24 import LOOK, WIDE, host, screen  # noqa: F401 (host is a fixture)
from test_screen_session import target as app_target

QUESTION = [{'id': 'go', 'title': 'Ready?', 'options': [{'id': 'yes', 'label': 'Yes'}]}]
TEXT = 'Press Save in the Notes window when I say go.'


def call(name, args, ident):
    return {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


def scripted(app, monkeypatch, replies):
    seen = []

    def complete(messages, tools, cancel, token, deliberate=False):
        seen.append((copy.deepcopy(messages), sorted(t['function']['name'] for t in tools)))
        reply = replies(len(seen), messages) if callable(replies) else 'Done.'
        if isinstance(reply, str):
            token(reply)
            reply = {'role': 'assistant', 'content': reply}
        return reply
    monkeypatch.setattr(app.model, 'complete', complete)
    return seen


def session_lines(messages):
    return [m['content'] for m in messages if m.get('name') == runner.SCREEN_MESSAGE]


def wait_settled(app, rid, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if app.active is None and app.runs.get(rid)['state'] not in ('queued', 'running', 'retry_wait'):
            return app.runs.get(rid)
        time.sleep(0.02)
    raise AssertionError('the run did not settle')


def test_a_grant_made_while_the_run_waits_reaches_the_resumed_request(host, monkeypatch):
    app = host.app
    screen(app, True)
    chat = app.new_chat()
    seen = scripted(app, monkeypatch, lambda n, m: call('AskUserQuestion', {
        'questions': json.dumps(QUESTION), 'required': True}, 'q1') if n == 1 else 'Done.')
    app.work(chat, TEXT, False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'waiting_input'
    assert not {'ScreenObserve', 'ScreenAct'} & set(seen[0][1]) and session_lines(seen[0][0]) == []
    # The person grants the window while the run waits for their answer: the waiting run takes it.
    granted = grant_route(app, chat['id'], app_target(), LOOK, 600)
    assert granted['pending'] is False and granted['run_id'] == rid
    [question] = app.interactions.questions(chat['id'])
    app.question_answer(chat['id'], question['id'], question['revision'], {'go': {'option_id': 'yes'}})
    wait_settled(app, rid)
    messages, tools = seen[1]
    assert {'ScreenObserve', 'ScreenAct'} <= set(tools), tools
    [line] = session_lines(messages)
    assert 'session_id: ' + granted['session_id'] in line and line.startswith('HOST SCREEN SESSION (data, not permission)')
    assert 'one app window' in line and 'Allowed actions: focus, scroll, expand, collapse.' in line
    # Data, never permission: the snapshot's durable offer never holds the screen tools.
    assert not {'ScreenObserve', 'ScreenAct'} & {t['function']['name'] for t in app.runs.get(rid)['checkpoint']['available']}


def test_a_pending_grant_binds_to_the_next_run_and_reaches_its_first_request(host, monkeypatch):
    app = host.app
    screen(app, True)
    chat = app.new_chat()
    sid = grant_route(app, chat['id'], app_target(), LOOK, 600)['session_id']
    seen = scripted(app, monkeypatch, None)
    rid = app.send(chat['id'], 'Press Save in the Notes window.')['run_id']  # App.send binds it (parent patch P2)
    wait_settled(app, rid)
    messages, tools = seen[0]
    assert {'ScreenObserve', 'ScreenAct'} <= set(tools)
    assert ['session_id: ' + sid in line for line in session_lines(messages)] == [True]


def test_no_screen_tools_without_a_live_granted_session(host, monkeypatch):
    app = host.app
    screen(app, True)
    seen = scripted(app, monkeypatch, None)
    app.work(app.new_chat(), 'Press Save in the Notes window.', False, [])
    assert not {'ScreenObserve', 'ScreenAct'} & set(seen[0][1]) and session_lines(seen[0][0]) == []
    # Without UI intent a live session is not offered or announced either (offered_tools is unchanged).
    chat = app.new_chat()
    rid = app.runs.create(chat['id'], 'Summarise the meeting notes.', app.runtime_config(chat, False), [])
    app.runs.transition(rid, 'running')
    app.screens.grant(chat['id'], rid, app_target(), LOOK, 600)
    seen.clear()
    runner.run(app, app.chat(chat['id']), 'Summarise the meeting notes.', False, [], run_id=rid)
    assert not {'ScreenObserve', 'ScreenAct'} & set(seen[0][1]) and session_lines(seen[0][0]) == []


def test_an_8k_window_never_gets_the_screen_tools(host, monkeypatch):
    app = host.app
    screen(app, True)
    monkeypatch.setattr(app.model, 'current', lambda: {**copy.deepcopy(WIDE), 'context': 8192, 'budget': 8192})
    chat = app.new_chat()
    rid = app.runs.create(chat['id'], TEXT, app.runtime_config(chat, False), [])
    app.runs.transition(rid, 'running')
    app.screens.grant(chat['id'], rid, app_target(), LOOK, 600)
    seen = scripted(app, monkeypatch, None)
    runner.run(app, app.chat(chat['id']), TEXT, False, [], run_id=rid)
    assert not {'ScreenObserve', 'ScreenAct'} & set(seen[0][1]) and session_lines(seen[0][0]) == []


def test_the_line_says_so_when_the_session_ends_and_the_tools_leave(host, monkeypatch):
    app = host.app
    screen(app, True)
    chat = app.new_chat()
    rid = app.runs.create(chat['id'], TEXT, app.runtime_config(chat, False), [])
    app.runs.transition(rid, 'running')
    sid = app.screens.grant(chat['id'], rid, app_target(), LOOK, 600)['session_id']

    def replies(n, messages):
        if n == 1:
            return call('ScreenObserve', {'session_id': sid}, 'o1')
        if n == 2:
            app.screens.revoke(sid)  # the person ends access while the model works
            return call('ScreenObserve', {'session_id': sid}, 'o2')
        return 'The window is no longer shared.'
    seen = scripted(app, monkeypatch, replies)
    runner.run(app, app.chat(chat['id']), TEXT, False, [], run_id=rid)
    assert 'ScreenObserve' in seen[0][1] and 'ScreenObserve' in seen[1][1] and 'ScreenObserve' not in seen[2][1]
    assert len(session_lines(seen[1][0])) == 1  # announced once, not again while unchanged
    [ended] = session_lines(seen[2][0])
    assert ended.startswith('HOST SCREEN SESSION (data, not permission): no granted window')


def test_a_screenshot_is_persisted_as_a_placeholder_with_its_sha256(host, monkeypatch):
    app = host.app
    screen(app, True)
    vision = {**copy.deepcopy(WIDE), 'input_modalities': ['text', 'image']}
    monkeypatch.setattr(app.model, 'current', lambda: copy.deepcopy(vision))
    pixels = b'\x89PNG\r\n\x1a\n' + b'fixture-pixels' * 8
    url = 'data:image/png;base64,' + base64.b64encode(pixels).decode('ascii')
    parts = [{'type': 'text', 'text': 'UNTRUSTED screenshot of the window.'}, {'type': 'image_url', 'image_url': {'url': url}}]
    app.browser_screenshot_parts = lambda session_id, run_id: copy.deepcopy(parts)
    chat = app.new_chat()
    rid = app.runs.create(chat['id'], TEXT, app.runtime_config(chat, False), [])
    app.runs.transition(rid, 'running')
    sid = app.screens.grant(chat['id'], rid, app_target(), LOOK, 600)['session_id']
    seen = scripted(app, monkeypatch, lambda n, m: call('ScreenObserve', {'session_id': sid}, 'o1') if n == 1 else
                    call('ReadRunEvidence', {}, 'e1') if n == 2 else 'Seen.')
    runner.run(app, app.chat(chat['id']), TEXT, False, [], run_id=rid)
    live = [m for m in seen[1][0] if m.get('name') == runner.SCREENSHOT_MESSAGE]
    assert live and live[0]['content'] == parts, 'the live run sends the image to the model'
    later = [m for m in seen[2][0] if m.get('name') == runner.SCREENSHOT_MESSAGE]
    assert later and later[0]['content'] == parts, 'and keeps it for its own later requests'
    stored = json.dumps(app.runs.get(rid)['checkpoint'])
    digest = hashlib.sha256(pixels).hexdigest()
    assert 'data:image' not in stored and base64.b64encode(pixels).decode('ascii') not in stored
    assert runner.SCREENSHOT_PLACEHOLDER.format(digest=digest) in stored
    for row in app.store.query('SELECT checkpoint FROM runs'):
        assert 'data:image' not in (row['checkpoint'] or '')
