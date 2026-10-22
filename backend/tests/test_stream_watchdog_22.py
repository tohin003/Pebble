"""Phase 22 (t22-stream): the stream stall watchdog on the Responses route.

The live record (docs/HARNESS_PHASE22_PLAN.md): "do you know my name" took 131.7 s because the first ChatGPT request
got no byte or event for 120.1 s before its retry. The root provider opened that request through urllib, whose
socket.create_connection gives each resolved address the whole 60 s timeout (two addresses: 120 s), then raised a
ProviderFailure with no status. The watchdog bounds the time to the stream's first line (default 20 s) and between
lines (60 s), abandons a silent request as a retryable failure, and leaves the retry to complete_with_retry's caps.
A model request never runs a tool, so an abandoned attempt has made no tool call or effect, and nothing it streamed is
used.

Scripted transports only: no network, model or credential. The App tests drive the real App, runner, Providers,
complete_responses and stream journal with only Providers.http scripted.
"""

import copy
import json
import re
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from pebble import runner
from pebble.agent_provider import PinnedProvider, _selected_profile
from pebble.app import App
from pebble.recovery import ProviderFailure, StreamBudget, StreamStalled, StreamWatchdog
from pebble.responses import complete_responses

PROFILE = {'id': 'luna', 'name': 'Luna', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
           'model': 'gpt-6-luna', 'context': 1050000, 'budget': 32768, 'tools': True}
LUNA = {**PROFILE, 'context_mode': 'custom', 'budget_version': 2, 'context_version': 1, 'connection_id': 'luna'}
MESSAGES = [{'role': 'user', 'content': 'do you know my name', '_pebble_current': True}]
USAGE = {'input_tokens': 900, 'output_tokens': 12, 'input_tokens_details': {'cached_tokens': 512}}
ARGS = json.dumps({'steps': '[]'})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('These tests must not open network connections.')
        return connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(AssertionError('network')))


def sse(events):
    return [b'data: ' + json.dumps(event).encode() + b'\n' for event in events]


def text_events(text, usage=USAGE):
    item = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text}]}
    return [{'type': 'response.created', 'response': {'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'status': 'in_progress', 'content': []}},
            {'type': 'response.output_text.delta', 'output_index': 0, 'item_id': 'msg_1', 'delta': text},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
            {'type': 'response.completed', 'response': {'status': 'completed', 'output': [], 'usage': usage}}]


def call_events(name='UpdatePlan', arguments=ARGS):
    """A finished function call, as the ChatGPT backend streams one (no response.completed yet)."""
    item = {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1', 'name': name, 'arguments': arguments,
            'status': 'completed'}
    return [{'type': 'response.created', 'response': {'status': 'in_progress', 'output': []}},
            {'type': 'response.output_item.added', 'output_index': 0, 'item': {**item, 'status': 'in_progress', 'arguments': ''}},
            {'type': 'response.function_call_arguments.done', 'output_index': 0, 'item_id': 'fc_1', 'arguments': arguments},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': item}]


class Stream:
    """An HTTP response as complete_responses reads it. With `hold`, readline waits after its lines (a silent stream)
    until abort() (the watchdog's) or the test releases it; `reads` and `closed` show what happened to it."""

    def __init__(self, lines=(), hold=False, pause=None):
        self.lines, self.hold, self.pause = list(lines), hold, pause
        self.reads, self.closed, self.aborted, self.gate = 0, False, False, threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def close(self):
        self.closed = True

    def abort(self):
        self.aborted = True
        self.gate.set()

    def readline(self, limit=-1):
        self.reads += 1
        if self.pause:
            time.sleep(self.pause)
        if self.lines:
            return self.lines.pop(0)
        if self.hold:
            self.gate.wait(20)
        return b''


class Provider:
    """The provider surface complete_responses uses, with a scripted transport (`respond(n)` per request)."""

    def __init__(self, respond, budget=None, watched=True):
        self.respond, self.requests, self.response = respond, [], None
        self.stream_budget, self.watched_open = budget, watched
        self.last_usage = {'sentinel': True}

    def key(self, profile):
        return 'scripted-token'

    def headers(self, kind, key):
        return {'Authorization': 'Bearer ' + key}

    def request(self, url, headers, body, timeout=None):
        self.requests.append(copy.deepcopy(body))
        return self.respond(len(self.requests))

    def project_context(self, messages, tools, budget, counter=None, method=None):
        return messages


def until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# -- the budgets ---------------------------------------------------------------------------------------------------

def test_the_default_budgets_and_their_bounds():
    assert StreamBudget.parse() == StreamBudget(20.0, None) == StreamBudget()
    # The idle deadline follows the request's effort: 90 s up to medium, 180 s for high and above.
    assert [StreamBudget().resolved(e).idle for e in ('none', 'low', 'medium', 'high', 'xhigh', 'max', None)] == [
        90.0, 90.0, 90.0, 180.0, 180.0, 180.0, 90.0]
    assert StreamBudget.parse({'first_event_seconds': 5}).as_dict() == {'first_event_seconds': 5.0}
    assert StreamBudget.parse({'first_event_seconds': 1, 'idle_seconds': 10}) == StreamBudget(1.0, 10.0)
    assert StreamBudget.parse({'idle_seconds': 300}).resolved('high').idle == 300.0  # an explicit budget wins
    for value in ({'first_event_seconds': 61}, {'idle_seconds': 301}, {'first_event_seconds': 0.5}, {'idle_seconds': 9},
                  {'first_event_seconds': True}, {'first_event_seconds': float('nan')}, {'first': 5}, {}, [20]):
        with pytest.raises(ValueError):
            StreamBudget.parse(value)


def test_the_watchdog_runs_its_abort_once_and_names_the_budget():
    calls = []
    watchdog = StreamWatchdog(StreamBudget(0.15, 0.15))
    watchdog.arm(lambda: calls.append('abort'))
    assert until(lambda: watchdog.expired == 'first_event') and until(lambda: calls == ['abort'])
    failure = watchdog.failure()
    assert isinstance(failure, StreamStalled) and isinstance(failure, ProviderFailure)
    assert failure.retryable is True and failure.status is None and failure.phase == 'first_event'
    assert 'No tools were executed' in str(failure)
    watchdog.close()
    beating = StreamWatchdog(StreamBudget(5, 0.15))
    beating.beat()
    assert until(lambda: beating.expired == 'idle') and beating.failure().phase == 'idle'
    beating.close()
    closed = StreamWatchdog(StreamBudget(0.1, 0.1))
    closed.close()
    time.sleep(0.25)
    assert closed.expired is None  # a closed watchdog never fires


# -- complete_responses with a scripted transport -------------------------------------------------------------------

def test_a_request_with_no_response_byte_is_abandoned_at_the_first_event_budget():
    release, late, tokens = threading.Event(), Stream(sse(text_events('late'))), []

    def respond(number):
        release.wait(20)  # the stalled connect: no header, no byte
        return late

    provider = Provider(respond, StreamBudget(0.3, 60))
    began = time.monotonic()
    with pytest.raises(StreamStalled) as caught:
        complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), tokens.append, 'medium')
    waited = time.monotonic() - began
    assert caught.value.phase == 'first_event' and caught.value.retryable and 0.25 <= waited < 3
    assert provider.last_timing['stall'] == 'first_event' and provider.last_timing['first_event'] is None
    assert provider.last_timing['headers'] is None
    assert caught.value.unsent is False  # a scripted transport is never assumed to have sent nothing
    assert provider.response is None and provider.last_usage == {'sentinel': True} and tokens == []
    release.set()
    # The abandoned open's late response is closed unread: it cannot reach this or a later attempt.
    assert until(lambda: late.closed) and late.reads == 0


def test_a_stream_that_goes_silent_is_aborted_at_the_idle_budget():
    stream, tokens = Stream(sse(text_events('Hel')[:3]), hold=True), []
    provider = Provider(lambda number: stream, StreamBudget(5, 0.3))
    with pytest.raises(StreamStalled) as caught:
        complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), tokens.append, 'medium')
    assert caught.value.phase == 'idle' and stream.aborted and stream.closed
    assert provider.last_timing['stall'] == 'idle' and provider.last_timing['first_event'] is not None
    assert provider.last_timing['headers'] <= provider.last_timing['first_event']
    assert provider.last_usage == {'sentinel': True}  # nothing from the abandoned stream is published as usage


def test_keep_alive_lines_keep_a_slow_stream_alive():
    lines = [b': keep-alive\n'] * 6 + sse(text_events('Yes.'))
    stream, tokens = Stream(lines, pause=0.1), []
    provider = Provider(lambda number: stream, StreamBudget(5, 0.35))
    result = complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), tokens.append, 'medium')
    assert result['content'] == 'Yes.' and tokens == ['Yes.'] and provider.last_timing['stall'] is None
    assert provider.last_usage['input_tokens'] == 900


def test_any_event_is_liveness_so_a_long_reasoning_phase_with_summaries_is_not_cut():
    # response.created, in_progress and reasoning-summary events every 0.1 s for 1.2 s, with a 0.35 s idle deadline:
    # the scaled form of "summary deltas every 30 s for 3 minutes at high effort are not retried" (study §6.3, 2).
    shell = {'status': 'in_progress', 'output': []}
    progress = [{'type': 'response.created', 'response': shell}, {'type': 'response.in_progress', 'response': shell}]
    progress += [{'type': 'response.reasoning_summary_text.delta', 'item_id': 'rs_1', 'output_index': 0,
                  'summary_index': 0, 'delta': 'still thinking '} for _ in range(10)]
    stream = Stream(sse(progress + text_events('Done.')[1:]), pause=0.1)
    provider = Provider(lambda number: stream, StreamBudget(5, 0.35))
    result = complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), [].append, 'high')
    assert result['content'] == 'Done.' and provider.last_timing['stall'] is None


class Socket:
    """The response socket's timeout, as the watchdog sets it after the headers."""

    def __init__(self):
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


def test_the_socket_read_timeout_follows_the_effort_scaled_idle_deadline():
    from pebble.capabilities import capabilities
    from pebble.recovery import READ_MARGIN, idle_for
    seen = {}
    for effort in ('low', 'medium', 'high', 'xhigh', 'default'):
        stream = Stream(sse(text_events('Ok.')))
        stream.fp = SimpleNamespace(raw=SimpleNamespace(_sock=Socket()))
        provider = Provider(lambda number, stream=stream: stream)
        complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), [].append, effort)
        seen[effort] = stream.fp.raw._sock.timeouts
    assert seen['low'] == seen['medium'] == [90.0 + READ_MARGIN]
    assert seen['high'] == seen['xhigh'] == [180.0 + READ_MARGIN]
    assert seen['default'] == [idle_for(capabilities(PROFILE)['default_effort']) + READ_MARGIN]


def test_the_person_stopping_a_stalled_open_returns_at_once():
    release, cancel = threading.Event(), threading.Event()

    def respond(number):
        release.wait(20)
        return Stream()

    provider = Provider(respond)  # the default 20 s budget: only the stop ends this wait
    threading.Timer(0.2, cancel.set).start()
    began = time.monotonic()
    with pytest.raises(InterruptedError):
        complete_responses(provider, PROFILE, MESSAGES, [], cancel, [].append, 'medium')
    assert time.monotonic() - began < 1.5
    release.set()


def test_a_provider_that_opens_on_its_own_thread_is_still_abandoned_before_any_line_is_used():
    # A child's pinned transport (watched_open False) opens on the request's thread; an open slower than the budget
    # is aborted as soon as the response exists, before any of its lines is parsed.
    stream, tokens = Stream(sse(text_events('too late'))), []

    def respond(number):
        time.sleep(0.4)
        return stream

    provider = Provider(respond, StreamBudget(0.2, 60), watched=False)
    with pytest.raises(StreamStalled) as caught:
        complete_responses(provider, PROFILE, MESSAGES, [], threading.Event(), tokens.append, 'medium')
    assert caught.value.phase == 'first_event' and stream.aborted and tokens == []


def test_a_loopback_server_keeps_the_phase21_bound_before_its_first_line():
    # A local server may read a long prompt before its first line; only the person's explicit budget is cut there.
    stream = Stream(sse(text_events('Local reply.')))
    local = {**PROFILE, 'kind': 'chatgpt', 'url': 'http://127.0.0.1:8080/v1'}

    def respond(number):
        time.sleep(0.4)
        return stream

    provider = Provider(respond, StreamBudget(0.2, 60))
    result = complete_responses(provider, local, MESSAGES, [], threading.Event(), [].append, 'medium')
    assert result['content'] == 'Local reply.'


# -- the real App: the live failure, retried promptly through the existing path -------------------------------------

class Http:
    """Providers.http: each model request goes through the real Providers.request with this opener."""

    def __init__(self, respond):
        self.respond, self.bodies = respond, []

    def open(self, req, timeout=None):
        self.bodies.append(json.loads(req.data))
        return self.respond(req, len(self.bodies))


def start(tmp_path, respond):
    events = []
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(copy.deepcopy(args)))
    app.store.set_setting('model_path', '')
    app.store.set_setting('model_profiles', [dict(LUNA)])
    app.model.select('luna')
    app.model.auth = SimpleNamespace(access=lambda pid: 'scripted-token')
    app.model.http = Http(respond)
    return app, events


def settle(app, rid, timeout=30):
    assert until(lambda: app.active is None and app.runs.get(rid)['state'] not in ('queued', 'running', 'retry_wait'),
                 timeout), app.runs.get(rid)['state']
    return app.runs.get(rid)


def notes(app, rid, kind):
    return [json.loads(r['payload']) for r in app.store.query(
        'SELECT payload FROM run_events WHERE run_id=? AND kind=? ORDER BY sequence', (rid, kind))]


def close(app):
    app.active = app.active_run = None
    app.close()


def test_the_live_stall_is_retried_after_the_first_event_budget_not_after_two_minutes(tmp_path):
    release, late = threading.Event(), Stream(sse(text_events('late text')))

    def respond(req, number):
        if number == 1:
            release.wait(20)  # attempt 1 of the live record: the request was sent and nothing came back
            return late
        return Stream(sse(text_events('Yes: you told me your name is Asha.')))

    app, events = start(tmp_path, respond)
    try:
        cid = app.new_chat(None)['id']
        began = time.monotonic()
        rid = app.send(cid, 'do you know my name', watchdog={'first_event_seconds': 2})['run_id']
        run = settle(app, rid)
        took = time.monotonic() - began
        assert run['state'] == 'completed' and took < 10, (run['state'], took)
        assert app.chat(cid)['messages'][-1]['content'] == 'Yes: you told me your name is Asha.'
        assert run['config']['stream_watchdog'] == {'first_event_seconds': 2.0}  # the idle deadline stays by effort
        bodies = app.model.http.bodies
        assert len(bodies) == 2 and bodies[0] == bodies[1]  # the same request, regenerated once
        # The abandoned attempt is recorded exactly as attempt 1 of the live record was, and retried once.
        assert notes(app, rid, 'model_attempt_failed') == [{'attempt': 1, 'status': None, 'type': 'StreamStalled'}]
        assert [r['attempt'] for r in notes(app, rid, 'model_retry')] == [2]
        attempts = notes(app, rid, 'model_attempt')
        assert [(a['attempt'], a['outcome'], a.get('stall')) for a in attempts] == [(1, 'stalled', 'first_event'),
                                                                                     (2, 'completed', None)]
        assert attempts[1]['headers'] <= attempts[1]['first_event'] <= attempts[1]['first_token']
        assert 'headers' not in attempts[0] and 'first_event' not in attempts[0]  # attempt 1 never got headers
        # It streamed live, so its (empty) presentation was settled `discarded`, and the retry's committed.
        assert [r['state'] for r in app.store.query(
            'SELECT state FROM run_stream_attempts WHERE run_id=? ORDER BY ordinal', (rid,))] == ['discarded', 'committed']
        # The activity timeline: the stalled attempt, the retry, the answered attempt.
        items = app.dispatch('run_activity', {'run_id': rid})['items']
        model = [(i['label'], i['status']) for i in items if i['kind'] == 'model']
        # Attempt 1 was still connecting after a second, so it also has a connect item that never connected.
        assert model == [('No response from ChatGPT', 'failed'), ('Could not connect to ChatGPT', 'failed'),
                         ('ChatGPT responded (attempt 2)', 'done')]
        answered = [i for i in items if i['label'] == 'ChatGPT responded (attempt 2)'][0]
        assert answered['headers_ms'] == round(attempts[1]['headers'] * 1000)
        assert answered['first_event_ms'] == round(attempts[1]['first_event'] * 1000)
        assert [i['label'] for name, d in events if name == 'activity' for i in [d['item']]
                if i['status'] == 'running' and i['label'] == 'Connecting to ChatGPT…']  # shown while it connected
        [retry] = [i for i in items if i['kind'] == 'retry']
        assert retry['label'] == 'Reconnecting to ChatGPT (attempt 2)' and 'first-event budget' in retry['detail']
        live = [d['item'] for name, d in events if name == 'activity' and d['item']['kind'] == 'retry']
        assert live and live[-1] == retry  # the live event and the route carry the same item
        release.set()
        assert until(lambda: late.closed) and late.reads == 0
    finally:
        release.set()
        close(app)


class Fast(StreamBudget):
    """Sub-second budgets for the App tests (the run config refuses them; the bound logic is the same)."""

    @classmethod
    def parse(cls, value=None):
        return StreamBudget(0.3, 0.3)


def test_a_finished_call_in_an_abandoned_attempt_never_runs(tmp_path, monkeypatch):
    # Attempt 1 streams a complete UpdatePlan call, then goes silent; no tool may run from it. Attempt 2 answers.
    monkeypatch.setattr(runner, 'StreamBudget', Fast)
    silent = Stream(sse(call_events()), hold=True)
    app, events = start(tmp_path, lambda req, number: silent if number == 1 else Stream(sse(text_events('Done.'))))
    try:
        cid = app.new_chat(None)['id']
        rid = app.send(cid, 'Plan my week')['run_id']
        run = settle(app, rid)
        assert run['state'] == 'completed' and silent.aborted
        assert app.runs.tools(rid) == []  # no call from the abandoned attempt was recorded, admitted or executed
        assert app.interactions.plan(rid) is None
        committed = [r['response'] for r in notes(app, rid, 'response_committed')]
        assert committed == [{'role': 'assistant', 'content': 'Done.', '_responses_output': [
            {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
             'content': [{'type': 'output_text', 'text': 'Done.'}]}]}]
        assert [a.get('stall') for a in notes(app, rid, 'model_attempt')] == ['idle', None]
    finally:
        close(app)


def test_repeated_stalls_stop_at_the_existing_retry_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'StreamBudget', Fast)
    release, opened = threading.Event(), []

    def respond(req, number):
        stream = Stream(sse(text_events('never read')))
        opened.append(stream)
        release.wait(20)
        return stream

    app, events = start(tmp_path, respond)
    try:
        cid = app.new_chat(None)['id']
        rid = app.send(cid, 'do you know my name')['run_id']
        run = settle(app, rid)
        assert run['state'] == 'failed' and 'sent no response' in run['error']
        assert len(app.model.http.bodies) == 3 and len(notes(app, rid, 'model_retry')) == 2
        assert [f['type'] for f in notes(app, rid, 'model_attempt_failed')] == ['StreamStalled'] * 3
        release.set()
        assert until(lambda: all(s.closed for s in opened)) and all(s.reads == 0 for s in opened)
    finally:
        release.set()
        close(app)


def test_stop_during_a_stalled_open_settles_promptly(tmp_path):
    release = threading.Event()

    def respond(req, number):
        release.wait(20)
        return Stream()

    app, events = start(tmp_path, respond)
    try:
        cid = app.new_chat(None)['id']
        rid = app.send(cid, 'do you know my name')['run_id']
        assert until(lambda: len(app.model.http.bodies) == 1)
        began = time.monotonic()
        app.stop(cid)
        run = settle(app, rid, timeout=5)
        assert run['state'] == 'stopped' and time.monotonic() - began < 3
    finally:
        release.set()
        close(app)


def test_the_run_config_carries_an_explicit_budget_only_and_resume_rebuilds_it(tmp_path):
    app, events = start(tmp_path, lambda req, number: Stream())
    try:
        chat = app.new_chat(None)
        default = app.runtime_config(chat, False)
        assert 'stream_watchdog' not in default  # every earlier run's config, and its resume check, is unchanged
        custom = app.runtime_config(chat, False, None, {'idle_seconds': 30})
        assert custom == {**default, 'stream_watchdog': {'first_event_seconds': 20.0, 'idle_seconds': 30.0}}
        # run_resume rebuilds the config from the stored one; an explicit budget compares equal.
        assert app.runtime_config(chat, False, custom['limits'], custom['stream_watchdog']) == custom
        with pytest.raises(ValueError, match='first-event budget'):
            app.send(chat['id'], 'hi', watchdog={'first_event_seconds': 120})
        assert app.runs.list(chat['id']) == [] and app.active is None  # refused before any run exists
    finally:
        close(app)


# -- the connect phase: Pebble's own connections, a fake resolver and fake sockets -----------------------------------

class Wire:
    """Addresses the fake resolver returns: `good` answers (one end of a local socketpair, served by a scripted HTTP
    responder), every other address is a black hole whose connect waits out its timeout. AF_UNIX pairs only."""

    def __init__(self, addresses, good, text='Yes: you told me your name is Asha.'):
        self.addresses, self.good, self.text, self.connects, self.requests = addresses, good, text, [], []

    def resolve(self, host, port, family=0, kind=0, *rest):
        return [(fam, socket.SOCK_STREAM, 6, '', addr) for fam, addr in self.addresses()]

    def factory(self, family, kind=socket.SOCK_STREAM, proto=0):
        return FakeSocket(self)

    def serve(self, server):
        with server:
            data = b''
            while b'\r\n\r\n' not in data:
                data += server.recv(65536)
            head, _, body = data.partition(b'\r\n\r\n')
            length = int(re.search(rb'content-length: *(\d+)', head, re.I).group(1))
            while len(body) < length:
                body += server.recv(65536)
            self.requests.append((head.decode(), json.loads(body)))
            server.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n'
                           + b''.join(line + b'\n' for line in sse(text_events(self.text))))


class FakeSocket:
    def __init__(self, wire):
        self.wire, self.timeout, self.inner = wire, None, None

    def settimeout(self, value):
        self.timeout = value
        if self.inner is not None:
            self.inner.settimeout(value)

    def bind(self, address):
        pass

    def setsockopt(self, *args):
        pass

    def connect(self, address):
        self.wire.connects.append((address[0], self.timeout, time.monotonic()))
        if address[0] != self.wire.good:
            time.sleep(self.timeout)  # a black hole: no answer, so the connect times out
            raise TimeoutError('timed out')
        self.inner, server = socket.socketpair()
        self.inner.settimeout(self.timeout)
        threading.Thread(target=self.wire.serve, args=(server,), daemon=True).start()

    def close(self):
        if self.inner is not None:
            self.inner.close()

    def __getattr__(self, name):  # sendall, makefile, shutdown, ...: the connected pair end
        return getattr(self.inner, name)


V6A, V6B, V4A, V4B = '2001:db8::1', '2001:db8::2', '192.0.2.1', '192.0.2.7'
ADDRESSES = [(socket.AF_INET6, (V6A, 80, 0, 0)), (socket.AF_INET6, (V6B, 80, 0, 0)),
             (socket.AF_INET, (V4A, 80)), (socket.AF_INET, (V4B, 80))]
LOCAL = {**LUNA, 'url': 'http://localhost/backend-api/codex'}  # plain HTTP over the fake wire; TLS is the stdlib's


def wired(tmp_path, wire):
    events = []
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(copy.deepcopy(args)))
    app.store.set_setting('model_path', '')
    app.store.set_setting('model_profiles', [dict(LOCAL)])
    app.model.select('luna')
    app.model.auth = SimpleNamespace(access=lambda pid: 'scripted-token')
    app.model.resolve, app.model.socket_factory = wire.resolve, wire.factory  # app.model.http stays Pebble's own
    return app, events


def test_two_dead_addresses_cost_seconds_each_and_the_turn_starts_within_ten_seconds(tmp_path):
    from pebble.recovery import CONNECT_ATTEMPT
    from pebble import VERSION
    wire = Wire(lambda: ADDRESSES, good=V6B)  # interleaved: V6A (dead), V4A (dead), then V6B answers
    app, events = wired(tmp_path, wire)
    try:
        cid = app.new_chat(None)['id']
        began = time.monotonic()
        rid = app.send(cid, 'do you know my name')['run_id']
        run = settle(app, rid)
        assert run['state'] == 'completed'
        [(head, body)] = wire.requests
        started = wire.connects[-1][2] - began
        assert started < 10, started  # was 120 s with each address given the whole 60 s timeout
        assert [(a, t) for a, t, _ in wire.connects] == [(V6A, CONNECT_ATTEMPT), (V4A, CONNECT_ATTEMPT),
                                                         (V6B, CONNECT_ATTEMPT)]
        assert re.search(r'^user-agent: Pebble/' + re.escape(VERSION) + r'\r?$', head, re.I | re.M)
        assert body['reasoning']['summary'] == 'auto'
        [attempt] = notes(app, rid, 'model_attempt')
        assert attempt['outcome'] == 'completed' and attempt['sent'] is True and attempt['headers'] >= 2 * CONNECT_ATTEMPT
        items = app.dispatch('run_activity', {'run_id': rid})['items']
        [connect] = [i for i in items if i['label'].startswith('Connected to ChatGPT in ')]
        assert connect['status'] == 'done' and connect['headers_ms'] == round(attempt['headers'] * 1000)
        assert any(d['item']['label'] == 'Connecting to ChatGPT…' and d['item']['status'] == 'running'
                   for name, d in events if name == 'activity')
        assert app.chat(cid)['messages'][-1]['usage']['input_tokens'] == 900
    finally:
        close(app)


def test_the_first_event_deadline_bounds_the_whole_connect_phase_and_an_abandoned_open_never_sends(tmp_path, monkeypatch):
    from pebble import responses
    monkeypatch.setattr(responses, 'LOOPBACK', set())  # the test host is localhost; apply the deadline as for ChatGPT
    calls = []

    def addresses():
        calls.append(True)
        return ADDRESSES[:1] * 6 if len(calls) == 1 else ADDRESSES[1:2]  # attempt 1: only dead addresses (24 s)

    wire = Wire(addresses, good=V6B)
    app, events = wired(tmp_path, wire)
    try:
        cid = app.new_chat(None)['id']
        began = time.monotonic()
        rid = app.send(cid, 'do you know my name', watchdog={'first_event_seconds': 2})['run_id']
        run = settle(app, rid)
        assert run['state'] == 'completed' and time.monotonic() - began < 8
        assert len(wire.requests) == 1  # the abandoned attempt never sent its request
        failed = notes(app, rid, 'model_attempt_failed')
        assert failed == [{'attempt': 1, 'status': None, 'type': 'StreamStalled'}]
        first, second = notes(app, rid, 'model_attempt')
        assert (first['outcome'], first['stall'], first['sent']) == ('stalled', 'first_event', False)
        # It used no tokens, so the message's totals stay known: the existing usage fields show real numbers.
        usage = app.chat(cid)['messages'][-1]['usage']
        assert usage['input_tokens'] == 900 and usage['cached_tokens'] == 512 and 'usage_incomplete' not in usage
        assert usage['failed_attempts'] == 1 and usage['unsent_attempts'] == 1
        items = app.dispatch('run_activity', {'run_id': rid})['items']
        [retry] = [i for i in items if i['kind'] == 'retry']
        assert 'Nothing was sent' in retry['detail']
        [lost] = [i for i in items if i['label'] == 'Could not connect to ChatGPT']
        assert lost['status'] == 'failed' and lost['detail'] == 'Nothing was sent.'
        # The abandoned helper tries no further address once its current connect ends.
        assert until(lambda: len([c for c in wire.connects if c[0] == V6A]) == 1 and time.monotonic() - began > 4.5, 8)
        time.sleep(0.2)
        assert len([c for c in wire.connects if c[0] == V6A]) == 1
    finally:
        close(app)


def test_the_provider_user_agent_names_the_running_version(tmp_path):
    from pebble import VERSION
    app, events = start(tmp_path, lambda req, number: Stream())
    try:
        assert app.model.headers('responses', 'key')['User-Agent'] == 'Pebble/' + VERSION
    finally:
        close(app)


def test_only_the_root_opens_on_a_helper_thread(tmp_path):
    app, events = start(tmp_path, lambda req, number: Stream())
    try:
        assert app.model.watched_open is True
        child = PinnedProvider(app, _selected_profile(app), 'child-1', app.agents.lane)
        assert child._backend.watched_open is False and child._backend.stream_budget is None
        child.close()
    finally:
        close(app)
