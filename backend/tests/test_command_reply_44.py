"""Phase 44 (1.9.1): what a window needs to show a runtime command's reply.

On 2026-10-25 the main window showed the person's command and never the host's reply. app.send answers a command at
once: it stores the command and the reply, emits only `complete` and starts no task, so no token, run or idle event
reaches a window. The window now reads the conversation back after a send the host marks as a command and adds what
the host stored after the command (src/runEvents.ts addStoredAfter). These tests pin that contract: the send result
names the stored command, the reply is stored right after it and readable through get_chat at once, the one event is a
`complete` for the reply, and a refused command stores nothing.
"""

import pytest

from pebble.app import App
from pebble.goals import Goals

# The events a window uses to bring a reply in; a command emits none of them but `complete`.
REPLY_EVENTS = {'token', 'run', 'idle', 'complete', 'progress'}


@pytest.fixture
def recorded(tmp_path):
    events = []
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args, **kwargs: events.append(args))
    app.goals = Goals(app)
    app.model.complete = lambda *args: pytest.fail('A host command must not run inference')
    yield app, events
    app.active = app.active_run = None
    app.close()


def sent(app, events, cid, text):
    del events[:]
    before = len(app.dispatch('get_chat', {'id': cid})['messages'])
    result = app.send(cid, text)
    messages = app.dispatch('get_chat', {'id': cid})['messages']
    return result, messages[before:], [e for e in events if e and e[0] in REPLY_EVENTS]


@pytest.mark.parametrize('text, answer', [
    ('/status', 'This conversation has no tasks yet.'),
    ('/jobs', 'This conversation has no schedules. Create one under Details › Tools.'),
    ('/memory status', 'Personalized memory: '),
])
def test_the_reply_is_stored_right_after_the_command_and_readable_at_once(recorded, text, answer):
    app, events = recorded
    cid = app.new_chat()['id']
    result, added, seen = sent(app, events, cid, text)
    assert set(result) == {'message_id', 'command'} and result['command'] is True
    assert [m['role'] for m in added] == ['user', 'assistant'], 'the command, then its reply, and nothing else'
    command, reply = added
    assert command['id'] == result['message_id'] and command['content'] == text
    assert reply['content'].startswith(answer) and reply['id'] != command['id']
    assert seen == [('complete', {'chat_id': cid, 'id': reply['id'], 'seconds': 0})], 'one complete, for the reply'
    assert not app.runs.list(cid), 'no task starts'


def test_a_command_during_a_task_is_still_answered_at_once(recorded):
    app, events = recorded
    cid = app.new_chat()['id']
    rid = app.runs.create(cid, 'Plan a trip', app.runtime_config(app.chat(cid), False))
    app.runs.transition(rid, 'running')
    app.active, app.active_run = cid, rid
    result, added, seen = sent(app, events, cid, '/status')
    assert result == {'message_id': added[0]['id'], 'command': True}, 'a command, not a follow-up to the task'
    assert [m['role'] for m in added] == ['user', 'assistant'] and 'Latest task, “Plan a trip”: **running**' in added[1]['content']
    assert seen == [('complete', {'chat_id': cid, 'id': added[1]['id'], 'seconds': 0})]


def test_a_refused_command_stores_and_emits_nothing(recorded):
    app, events = recorded
    cid = app.new_chat()['id']
    del events[:]
    with pytest.raises(ValueError, match=r'Use /memory \[status\|candidates\|maintain\]\.'):
        app.send(cid, '/memory everything')
    assert app.dispatch('get_chat', {'id': cid})['messages'] == []
    assert [e for e in events if e and e[0] in REPLY_EVENTS] == []
