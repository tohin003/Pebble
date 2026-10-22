"""Phase 22 (t22-stream): reasoning summaries on the Responses route, published as display data only.

On the ChatGPT sign-in route above a 4,096-token budget the request asks for `reasoning.summary`; the summary deltas
reach `token.reasoning`, and the runner publishes them as `reasoning` events and a `reasoning` activity item. They
are never the response, never model context (returned reasoning items carry an empty summary), never stored (not in
the database, a memory layer, a checkpoint or the stream journal), and a step whose text is held keeps its summary
held the same way. Request bodies at or below 4,096 tokens, and on API-key Responses routes, are unchanged.

Scripted transports and a scripted root model only: no network, model or credential.
"""

import copy
import io
import json
import socket
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

from pebble.app import App
from pebble.permissions import DEFAULT
from pebble.providers import Providers
from pebble.recovery import ProviderFailure, RetryPolicy, complete_with_retry
from pebble.responses import complete_responses, input_items
from test_high_fd_waits_21 import close_guard_databases

PROFILE = {'id': 'luna', 'name': 'Luna', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
           'model': 'gpt-6-luna', 'context': 1050000, 'budget': 32768, 'tools': True}
MESSAGES = [{'role': 'user', 'content': 'do you know my name', '_pebble_current': True}]
USAGE = {'input_tokens': 900, 'output_tokens': 40, 'output_tokens_details': {'reasoning_tokens': 28}}
FAST = RetryPolicy(initial_delay=0, max_delay=0)
SOURCE = 'def add(a, b):\n    return a + b\n'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('These tests must not open network connections.')
        return connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))


# -- scripted Responses streams -------------------------------------------------------------------------------------

def added(index, item):
    started = {**item, 'status': 'in_progress'}
    if item['type'] == 'message':
        started['content'] = []
    if item['type'] == 'reasoning':
        started['summary'] = []
        started.pop('encrypted_content', None)
    return {'type': 'response.output_item.added', 'output_index': index, 'item': started}


def done(index, item):
    return {'type': 'response.output_item.done', 'output_index': index, 'item': item}


def summary_events(parts, deltas=True, finished=True, item_id='rs_1'):
    """A reasoning item whose summary parts stream as the ChatGPT backend sends them."""
    item = {'type': 'reasoning', 'id': item_id, 'summary': [], 'encrypted_content': 'opaque-' + item_id}
    events = [added(0, item)]
    for index, text in enumerate(parts):
        where = {'item_id': item_id, 'output_index': 0, 'summary_index': index}
        events.append({'type': 'response.reasoning_summary_part.added', **where, 'part': {'type': 'summary_text', 'text': ''}})
        if deltas:
            half = len(text) // 2
            events += [{'type': 'response.reasoning_summary_text.delta', **where, 'delta': text[:half]},
                       {'type': 'response.reasoning_summary_text.delta', **where, 'delta': text[half:]}]
        if finished:
            events.append({'type': 'response.reasoning_summary_text.done', **where, 'text': text})
        events.append({'type': 'response.reasoning_summary_part.done', **where,
                       'part': {'type': 'summary_text', 'text': text}})
    final = {**item, 'summary': [{'type': 'summary_text', 'text': text} for text in parts]}
    return events + [done(0, final)], final


def message_events(text, index=1):
    item = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text}]}
    return [added(index, item), {'type': 'response.output_text.delta', 'output_index': index, 'item_id': 'msg_1',
                                 'delta': text}, done(index, item)]


def stream(parts, text='Your name is Asha.', output=(), **options):
    events, _ = summary_events(parts, **options)
    return ([{'type': 'response.created', 'response': {'status': 'in_progress', 'output': []}}] + events
            + message_events(text) + [{'type': 'response.completed',
                                       'response': {'status': 'completed', 'output': list(output), 'usage': USAGE}}])


class Response:
    def __init__(self, events):
        self.lines = [b'data: ' + json.dumps(e).encode() + b'\n' for e in events]
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def readline(self, limit=-1):
        return self.lines.pop(0) if self.lines else b''


class Provider:
    def __init__(self, respond):
        self.respond, self.requests, self.response, self.last_usage = respond, [], None, {}

    def key(self, profile):
        return 'scripted-token'

    def headers(self, kind, key):
        return {'Authorization': 'Bearer ' + key}

    def request(self, url, headers, body, timeout=None):
        self.requests.append(copy.deepcopy(body))
        return self.respond(len(self.requests))

    def project_context(self, messages, tools, budget, counter=None, method=None):
        return messages


class Sink(list):
    """A `token` callable with the reasoning channel complete_with_retry attaches."""

    def __init__(self):
        super().__init__()
        self.summaries = []

    def __call__(self, part):
        self.append(part)

    def reasoning(self, part):
        self.summaries.append(part)


def run(events, profile=PROFILE, effort='medium', sink=None):
    provider, sink = Provider(lambda number: Response(events)), sink if sink is not None else Sink()
    result = complete_responses(provider, profile, MESSAGES, [], threading.Event(), sink, effort)
    return result, provider, sink


# -- the request ----------------------------------------------------------------------------------------------------

def test_the_chatgpt_route_above_4k_asks_for_summaries_and_nothing_else_changes():
    _, large, _ = run(stream([]))
    _, small, _ = run(stream([]), {**PROFILE, 'budget': 4096})
    body, phase21 = large.requests[0], small.requests[0]
    assert body['reasoning'] == {'effort': 'medium', 'summary': 'auto'}
    # At a 4,096-token budget the body is the Phase 21 body; above it only reasoning.summary is added.
    assert phase21['reasoning'] == {'effort': 'medium'} and 'summary' not in json.dumps(phase21)
    assert {**body, 'reasoning': {'effort': 'medium'}} == phase21
    assert body['include'] == ['reasoning.encrypted_content'] and body['store'] is False
    _, default, _ = run(stream([]), effort='default')
    assert default.requests[0]['reasoning'] == {'summary': 'auto'}


def test_an_api_key_responses_route_keeps_its_phase21_request():
    keyed = {**PROFILE, 'kind': 'responses', 'url': 'https://api.example.invalid/v1',
             'reasoning_capabilities': {'parameter': 'reasoning', 'levels': ['low', 'medium'], 'default': 'medium'}}
    _, provider, _ = run(stream([]), keyed)
    assert provider.requests[0]['reasoning'] == {'effort': 'medium'}


# -- parsing and publication ----------------------------------------------------------------------------------------

def test_summary_deltas_reach_only_the_reasoning_channel_and_never_the_reply_or_its_replay():
    parts = ['Looking at what you told me before.', 'Checking the saved profile.']
    result, provider, sink = run(stream(parts))
    halves = [(p[:len(p) // 2], p[len(p) // 2:]) for p in parts]
    assert sink.summaries == [halves[0][0], halves[0][1], '\n\n' + halves[1][0], halves[1][1]]
    assert ''.join(sink.summaries) == '\n\n'.join(parts)
    assert list(sink) == ['Your name is Asha.'] and result['content'] == 'Your name is Asha.'
    # The returned reasoning item keeps its encrypted content and an empty summary: the text never returns to the model.
    [reasoning, message] = result['_responses_output']
    assert reasoning == {'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'encrypted_content': 'opaque-rs_1'}
    replay = json.dumps(input_items([result, {'role': 'user', 'content': 'and my city?'}]))
    assert 'Checking the saved profile' not in json.dumps(result) and 'Looking at' not in replay
    timing = provider.last_timing
    assert timing['summaries'] is True and timing['reasoning_chars'] == sum(len(p) for p in parts)
    assert 0 <= timing['reasoning_first'] <= timing['reasoning_last'] and timing['first_token'] >= timing['reasoning_last']
    assert provider.last_usage['reasoning_tokens'] == 28


def test_a_route_that_sends_only_finished_parts_shows_each_once():
    result, _, sink = run(stream(['Thinking about it.', 'Done thinking.'], deltas=False))
    assert sink.summaries == ['Thinking about it.', '\n\nDone thinking.']
    _, _, both = run(stream(['Said once.']))  # deltas then .done: the finished text is not repeated
    assert ''.join(both.summaries) == 'Said once.'


def test_an_authoritative_terminal_output_with_summary_text_is_replayed_without_it():
    _, final = summary_events(['A private summary.'])
    message = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
               'content': [{'type': 'output_text', 'text': 'Your name is Asha.'}]}
    result, _, _ = run(stream(['A private summary.'], output=[final, message]))
    assert result['_responses_output'][0]['summary'] == [] and 'private summary' not in json.dumps(result)


def test_a_plain_token_callback_ignores_summaries():
    tokens = []
    provider = Provider(lambda number: Response(stream(['Unseen.'])))
    result = complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), tokens.append, 'medium')
    assert tokens == ['Your name is Asha.'] and result['content'] == 'Your name is Asha.'


def test_a_route_that_refuses_summaries_is_asked_once_more_without_them():
    def respond(number):
        if number == 1:
            raise ProviderFailure('Provider HTTP 400: Your organization must be verified to generate reasoning '
                                  'summaries. (parameter: reasoning.summary)', status=400)
        return Response(stream([]))

    provider, sink = Provider(respond), Sink()
    result = complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), sink, 'medium')
    assert result['content'] == 'Your name is Asha.' and len(provider.requests) == 2
    first, second = provider.requests
    assert first['reasoning'] == {'effort': 'medium', 'summary': 'auto'} and second == {**first, 'reasoning': {'effort': 'medium'}}
    assert provider.summaries_refused == {('luna', 'gpt-6-luna')} and provider.last_timing['summaries'] is False
    complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), sink, 'medium')
    assert len(provider.requests) == 3 and provider.requests[2] == second  # not asked again on this route


def test_a_refusal_inside_the_stream_before_any_output_is_also_asked_again_without_summaries():
    refusal = {'type': 'error', 'code': 'unsupported_parameter', 'param': 'reasoning.summary',
               'message': 'Reasoning summaries are not available for this account.'}
    responses = [Response([{'type': 'response.created', 'response': {'status': 'in_progress', 'output': []}}, refusal]),
                 Response(stream([]))]
    provider, sink = Provider(lambda number: responses[number - 1]), Sink()
    result = complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), sink, 'medium')
    assert result['content'] == 'Your name is Asha.' and list(sink) == ['Your name is Asha.']
    assert [r['reasoning'] for r in provider.requests] == [{'effort': 'medium', 'summary': 'auto'}, {'effort': 'medium'}]
    # After output was shown, the same error fails the request as before (nothing is shown twice).
    late = [{'type': 'response.output_text.delta', 'output_index': 0, 'item_id': 'm', 'delta': 'Partial'}, refusal]
    provider = Provider(lambda number: Response(late))
    with pytest.raises(ValueError, match='Reasoning summaries are not available'):
        complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), Sink(), 'medium')
    assert len(provider.requests) == 1


def test_a_refusal_is_remembered_for_the_profile_across_a_restart(tmp_path):
    from pebble.providers import SUMMARY_REFUSALS
    luna = {**PROFILE, 'context_mode': 'custom', 'budget_version': 2, 'context_version': 1, 'connection_id': 'luna'}
    bodies = []

    class Http:
        def open(self, req, timeout=None):
            bodies.append(json.loads(req.data))
            if len(bodies) == 1:
                raise urllib.error.HTTPError(req.full_url, 400, 'bad', {}, io.BytesIO(json.dumps({'error': {
                    'message': 'Your organization must be verified to generate reasoning summaries.',
                    'param': 'reasoning.summary'}}).encode()))
            return Response(stream([]))

    for leg in range(2):
        app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
        try:
            app.store.set_setting('model_path', '')
            app.store.set_setting('model_profiles', [dict(luna)])
            app.model.select('luna')
            app.model.auth = SimpleNamespace(access=lambda pid: 'scripted-token')
            app.model.http = Http()
            chat = app.new_chat(None)
            app.work(chat, 'do you know my name', False, [])
            assert app.chat(chat['id'])['messages'][-1]['content'] == 'Your name is Asha.'
            assert app.store.setting(SUMMARY_REFUSALS) == [['luna', 'gpt-6-luna']]
            # The profile itself, and so every run's route pin, is unchanged by the remembered refusal.
            assert app.store.setting('model_profiles') == [luna]
        finally:
            app.active = app.active_run = None
            app.close()
    assert ['summary' in b.get('reasoning', {}) for b in bodies] == [True, False, False]  # the restart never asks


def test_another_400_is_not_retried_without_summaries():
    def respond(number):
        raise ProviderFailure('Provider HTTP 400: Your input exceeds the context window. (parameter: input)', status=400)

    provider = Provider(respond)
    with pytest.raises(ProviderFailure, match='context window'):
        complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), Sink(), 'medium')
    assert len(provider.requests) == 1


# -- the retry boundary -----------------------------------------------------------------------------------------------

def test_summaries_are_bound_to_their_open_attempt_and_never_become_the_reply():
    seen, output, kept = [], [], []

    def complete(messages, tools, cancel, token, effort):
        kept.append(token)
        token.reasoning('thinking %d' % len(kept))
        if len(kept) == 1:
            token('abandoned text')
            raise ProviderFailure('connection lost', retryable=True)
        kept[0].reasoning('late from attempt 1')  # a finished attempt's channel is closed
        token('Committed.')
        return {'role': 'assistant', 'content': 'Committed.'}

    result = complete_with_retry(complete, MESSAGES, [], threading.Event(), output.append, False, policy=FAST,
                                 on_reasoning=lambda attempt, text: seen.append((attempt, text)))
    assert result['content'] == 'Committed.' and output == ['Committed.']
    assert seen == [(1, 'thinking 1'), (2, 'thinking 2')]


def test_a_reasoning_display_error_never_fails_or_retries_the_request():
    calls = []

    def complete(messages, tools, cancel, token, effort):
        calls.append(True)
        token.reasoning('one')
        token.reasoning('two')
        token('Answer.')
        return {'role': 'assistant', 'content': 'Answer.'}

    def broken(attempt, text):
        raise RuntimeError('display failed')

    assert complete_with_retry(complete, MESSAGES, [], threading.Event(), [].append, False, policy=FAST,
                               on_reasoning=broken)['content'] == 'Answer.'
    assert calls == [True]
    with pytest.raises(TypeError):
        complete_with_retry(complete, MESSAGES, [], threading.Event(), [].append, False, on_reasoning='no')


def test_summaries_after_the_person_stops_are_dropped():
    cancel, seen = threading.Event(), []

    def complete(messages, tools, cancel_event, token, effort):
        token.reasoning('before')
        cancel.set()
        token.reasoning('after')
        return {'role': 'assistant', 'content': 'x'}

    with pytest.raises(InterruptedError):
        complete_with_retry(complete, MESSAGES, [], cancel, [].append, False, policy=FAST,
                            on_reasoning=lambda attempt, text: seen.append(text))
    assert seen == ['before']


# -- the runner: live, held, released and discarded summaries -------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'calc.py').write_text(SOURCE)
    value = SimpleNamespace(project=project, script=[], events=[], apps=[], during=[])

    def complete(provider, messages, tools, cancel, token, effort=False):
        assert value.script, 'Unscripted root model request.'
        sent, marks, relay = time.monotonic(), [], getattr(token, 'reasoning', None)
        if relay is not None:
            def reasoning(part):
                marks.append(round(time.monotonic() - sent, 4))
                relay(part)
            token.reasoning = reasoning
        try:
            return value.script.pop(0)(token, value)
        finally:
            if marks:  # the content-free timing complete_responses records for a request with a summary
                provider.last_timing = {'reasoning_first': marks[0], 'reasoning_last': marks[-1], 'summaries': True}

    monkeypatch.setattr(Providers, 'complete', complete)

    def start():
        app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: value.events.append(copy.deepcopy(args)))
        value.apps.append(app)
        app.store.set_setting('permissions', {**copy.deepcopy(DEFAULT), 'mode': 'ask'})
        app.model_save(name='Summary fixture', url='http://127.0.0.1:9', model='summary-fixture', context=32768)
        return app

    value.start = start
    yield value
    for app in value.apps:
        if not app.closed:
            app.active = app.active_run = None
            app.close()
    close_guard_databases(tmp_path)


def answer(text, summaries=(), calls=None):
    def step(token, env):
        for part in summaries:
            token.reasoning(part)
        env.during.append(published(env))  # what the person could see while this attempt ran
        if text:
            token(text)
        return {'role': 'assistant', 'content': text, **({'tool_calls': calls} if calls else {})}
    return step


def failing(summaries):
    def step(token, env):
        for part in summaries:
            token.reasoning(part)
        raise ProviderFailure('connection lost', retryable=True)
    return step


def tool(name, args, ident):
    return [{'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]


def published(env):
    return ''.join(d['delta'] for name, d in env.events if name == 'reasoning')


def stored_in(app, marker):
    """Every table of the database that holds `marker` anywhere (none, for display-only text)."""
    found = []
    with app.store.lock:
        names = [r[0] for r in app.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for name in names:
            if any(marker in repr(row) for row in app.store.db.execute('SELECT * FROM "%s"' % name.replace('"', '""'))):
                found.append(name)
    return found


def settle(app, rid, state, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if app.runs.get(rid)['state'] == state and app.active is None:
            return
        time.sleep(0.02)
    pytest.fail('Run did not settle as %s: %s' % (state, app.runs.get(rid)['state']))


def test_a_live_steps_summary_streams_as_reasoning_events_and_is_never_stored(env):
    app = env.start()
    chat = app.new_chat(None)
    env.script = [answer('Your name is Asha.', ['Looking at what you said. ', 'SUMMARY-MARKER-LIVE'])]
    app.work(chat, 'do you know my name', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    events = [d for name, d in env.events if name == 'reasoning']
    [attempt] = [r['id'] for r in app.store.query('SELECT id FROM run_stream_attempts WHERE run_id=?', (rid,))]
    assert [(d['run_id'], d['attempt_id'], d['delta']) for d in events] == [
        (rid, attempt, 'Looking at what you said. '), (rid, attempt, 'SUMMARY-MARKER-LIVE')]
    assert env.during == ['Looking at what you said. SUMMARY-MARKER-LIVE']  # shown while the attempt ran
    activity = app.dispatch('run_activity', {'run_id': rid})
    assert activity['reasoning'] == {'available': True, 'text': 'Looking at what you said. SUMMARY-MARKER-LIVE'}
    [item] = [i for i in activity['items'] if i['kind'] == 'reasoning']
    assert item['label'].startswith('Thought for') and item['status'] == 'done' and item['detail'] is None
    # Display data only: not in any table (run records, checkpoint, stream journal, memories, metrics), the reply,
    # the transcript or any activity item.
    assert stored_in(app, 'SUMMARY-MARKER') == []
    assert all('SUMMARY-MARKER' not in m['content'] for m in app.chat(chat['id'])['messages'])
    assert 'SUMMARY-MARKER' not in json.dumps(activity['items'])
    assert all('SUMMARY-MARKER' not in json.dumps(d) for name, d in env.events if name != 'reasoning')


def test_a_held_turns_summary_is_held_like_its_text_and_the_reply_after_the_decision_is_shown(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    early, after = 'notes.md is staged.\nFINAL: written', 'You declined, so notes.md was not written.\nFINAL: declined'
    env.script = [answer(None, calls=tool('Write', {'file_path': 'notes.md', 'content': '# Notes\n'}, 'write')),
                  answer(early, ['HELD-SUMMARY-MARKER']), answer(after, ['SHOWN-AFTER-DECISION'])]
    app.work(chat, 'Write notes.md describing add in calc.py. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    assert app.runs.get(rid)['state'] == 'waiting_approval'
    assert 'HELD-SUMMARY-MARKER' not in published(env) and 'HELD-SUMMARY-MARKER' not in env.during[-1]
    held = app.dispatch('run_activity', {'run_id': rid})
    assert 'HELD-SUMMARY-MARKER' not in held['reasoning']['text'] and not [i for i in held['items'] if i['kind'] == 'reasoning']
    action = next(a for a in app.store.actions(cid) if a['status'] == 'pending')
    app.reject(cid, action['id'])
    settle(app, rid, 'completed')
    assert 'SHOWN-AFTER-DECISION' in published(env) and 'HELD-SUMMARY-MARKER' not in published(env)
    text = app.dispatch('run_activity', {'run_id': rid})['reasoning']['text']
    assert text == 'SHOWN-AFTER-DECISION'
    assert stored_in(app, 'SUMMARY-MARKER') == [] and stored_in(app, 'SHOWN-AFTER-DECISION') == []


def test_a_buffered_steps_summary_is_released_with_its_text_never_before(env):
    app = env.start()
    chat = app.new_chat(str(env.project))
    cid = chat['id']
    env.script = [answer(None, calls=tool('Write', {'file_path': 'notes.md', 'content': '# Notes\n'}, 'write')),
                  # The edit awaits a decision, so this step is buffered; its text comes with a call, so it is shown.
                  answer('Requesting the tests too.', ['RELEASED-SUMMARY'],
                         calls=tool('RunTests', {'recipe': 'python-unittest'}, 'tests')),
                  answer('Both are staged.\nFINAL: staged', ['HELD-SUMMARY-MARKER'])]
    (env.project / 'test_calc.py').write_text('import unittest\n')
    app.work(chat, 'Write notes.md and run the tests. End with `FINAL: <word>`.', False, [])
    rid = app.runs.list(cid)[0]['id']
    assert app.runs.get(rid)['state'] == 'waiting_approval'
    assert 'RELEASED-SUMMARY' not in env.during[1]  # nothing was shown while the buffered attempt ran
    names = [name for name, d in env.events if name in ('reasoning', 'token')]
    texts = [d.get('delta', d.get('text')) for name, d in env.events if name in ('reasoning', 'token')]
    released, shown = texts.index('RELEASED-SUMMARY'), next(i for i, t in enumerate(texts) if 'Requesting the tests' in t)
    assert names[released] == 'reasoning' and released < shown  # released with its text, just before it
    assert 'HELD-SUMMARY-MARKER' not in published(env)
    assert app.dispatch('run_activity', {'run_id': rid})['reasoning']['text'] == 'RELEASED-SUMMARY'


def test_a_discarded_attempts_summary_leaves_the_text_and_its_item_settles_as_failed(env):
    app = env.start()
    chat = app.new_chat(None)
    env.script = [failing(['DISCARDED-SUMMARY']), answer('Your name is Asha.', ['KEPT-SUMMARY'])]
    app.work(chat, 'do you know my name', False, [])
    rid = app.runs.list(chat['id'])[0]['id']
    assert app.runs.get(rid)['state'] == 'completed'
    activity = app.dispatch('run_activity', {'run_id': rid})
    assert activity['reasoning']['text'] == 'KEPT-SUMMARY'
    reasoning = [(i['status']) for i in activity['items'] if i['kind'] == 'reasoning']
    assert reasoning == ['failed', 'done']
    live = {}
    for name, data in env.events:
        if name == 'activity' and data['item']['kind'] == 'reasoning':
            live[data['item']['seq']] = data['item']['status']
    assert sorted(live.values()) == ['done', 'failed']  # the UI saw the discarded one settle, not hang as running
