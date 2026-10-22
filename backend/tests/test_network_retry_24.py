"""Phase 24 (t24-auth): model requests ride out a short outage, say so in plain words, notice a sleep, try the system
proxy after a connect failure, close their HTTP errors and carry the run's prompt_cache_key on the ChatGPT route.

V1-066, V1-068, V1-072, V1-078 and V1-168 in docs/PEBBLE_V1_INSPECTION.md. Scripted transports, fake clocks and
loopback servers only: no external network, model or credential.
"""
import email.message
import io
import json
import socket
import ssl
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import VERSION, recovery
from pebble.lifecycle import Lifecycle
from pebble.providers import Providers
from pebble.recovery import (NETWORK_WAIT, ProviderFailure, RetryPolicy, StreamBudget, StreamStalled, StreamWatchdog,
                             complete_with_retry, retry_label, waiting_for_network)
from pebble.responses import complete_responses
from pebble.store import Store

LUNA = {'id': 'luna', 'name': 'Luna', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
        'model': 'gpt-6-luna', 'context': 1050000, 'budget': 32768, 'tools': True, 'connection_id': 'luna'}
MESSAGES = [{'role': 'user', 'content': 'do you know my name', '_pebble_current': True}]


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ('127.0.0.1', '::1'):
            raise AssertionError('These tests open loopback connections only.')
        return connect(self, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)


def offline():
    return ProviderFailure(recovery.connect_message('offline'), retryable=True, code='offline', network=True)


class Waits:
    """The cancel event: wait() records each delay and moves the fake clock, never sleeping."""

    def __init__(self, now):
        self.now, self.delays = now, []

    def is_set(self):
        return False

    def wait(self, delay):
        self.delays.append(delay)
        self.now[0] += delay
        return False


def retried(complete, now, **kwargs):
    cancel = Waits(now)
    retries = []
    result = complete_with_retry(complete, MESSAGES, [], cancel, [].append, 'medium', clock=lambda: now[0],
                                 on_retry=lambda *args: retries.append(args), **kwargs)
    return result, cancel.delays, retries


# -- V1-066: the retry window ---------------------------------------------------------------------------------------------

def test_transport_failures_wait_about_1_2_4_8_seconds_for_the_network():
    now, calls = [0.0], []

    def complete(*args):
        calls.append(now[0])
        if len(calls) < 6:
            raise offline()
        return {'role': 'assistant', 'content': 'back'}
    result, delays, retries = retried(complete, now, deadline=60.0)
    assert result['content'] == 'back' and len(calls) == 6
    for delay, base in zip(delays, (1, 2, 4, 8, 8)):
        assert base * 0.8 <= delay <= base * 1.2, (delay, base)
    assert [r[0] for r in retries] == [1, 2, 3, 4, 5] and all(r[2] is None for r in retries)
    assert waiting_for_network(offline()) and retry_label(offline()) == NETWORK_WAIT == 'Waiting for the network'
    assert retry_label(ProviderFailure('busy', retryable=True, status=503)) == 'Reconnecting to the selected model'
    calls.clear()
    with pytest.raises(ProviderFailure):
        retried(lambda *args: calls.append(1) or (_ for _ in ()).throw(offline()), [0.0], deadline=600.0)
    assert len(calls) == RetryPolicy().network_attempts == 6


def test_a_four_second_outage_is_ridden_out_within_the_run_deadline():
    now = [0.0]

    def complete(*args):
        if now[0] < 4.0:  # the Wi-Fi handover: no route for four seconds
            raise offline()
        return {'role': 'assistant', 'content': 'answered'}
    result, delays, _ = retried(complete, now, deadline=120.0)
    assert result['content'] == 'answered' and 4.0 <= now[0] < 12.0 and len(delays) == 3
    # The run deadline still bounds the wait: with 10 s left, the 8 s wait is not started.
    now = [0.0]
    with pytest.raises(TimeoutError, match='while reconnecting'):
        retried(lambda *args: (_ for _ in ()).throw(offline()), now, deadline=10.0)
    assert now[0] < 10.0


def test_status_failures_and_stalls_keep_three_attempts():
    for failure in (ProviderFailure('Provider HTTP 503: busy', retryable=True, status=503),
                    StreamStalled('first_event', 20.0)):
        calls = []
        with pytest.raises(ProviderFailure):
            retried(lambda *args: calls.append(1) or (_ for _ in ()).throw(failure), [0.0], deadline=600.0)
        assert len(calls) == RetryPolicy().attempts == 3
    now, delays = [0.0], None

    def busy(*args):
        raise ProviderFailure('Provider HTTP 503: busy', retryable=True, status=503)
    with pytest.raises(ProviderFailure):
        _, delays, _ = retried(busy, now, deadline=600.0)
    assert 1.2 <= now[0] <= 1.8  # 0.5 s and 1 s, as the Phase 20 fault grader allows
    # A stall that leads into a network outage (a wake) keeps waiting for the network.
    calls = []

    def woke(*args):
        calls.append(1)
        if len(calls) == 1:
            raise StreamStalled('sleep', recovery.WAKE_GRACE)
        if len(calls) < 5:
            raise offline()
        return {'role': 'assistant', 'content': 'ok'}
    assert retried(woke, [0.0], deadline=600.0)[0]['content'] == 'ok' and len(calls) == 5


def chatgpt_provider(tmp_path, error, profile=LUNA):
    store = Store(tmp_path)
    p = Providers(tmp_path, Path('missing'), store, lambda *args: None)
    store.set_setting('model_profiles', [dict(profile)])
    store.set_setting('active_profile', profile['id'])
    p.auth = SimpleNamespace(access=lambda pid, rejected=None: 'scripted-token')
    p.http = SimpleNamespace(open=lambda req, timeout=None: (_ for _ in ()).throw(error))
    return p, store


def test_the_chatgpt_route_never_says_check_its_url(tmp_path):
    lookup = urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, 'nodename nor servname provided, or not known'))
    p, store = chatgpt_provider(tmp_path / 'a', lookup)
    try:
        with pytest.raises(ProviderFailure) as caught:
            p.complete(MESSAGES, [], threading.Event(), [].append)
        text = str(caught.value)
        assert 'Check its URL' not in text and text.startswith('Could not connect to the model provider (ChatGPT): ')
        assert 'offline' in text and caught.value.code == 'offline' and waiting_for_network(caught.value)
    finally:
        store.close()
    custom = {**LUNA, 'kind': 'responses', 'url': 'https://llm.example.test/v1'}
    p, store = chatgpt_provider(tmp_path / 'b', urllib.error.URLError(ConnectionRefusedError(61, 'refused')), custom)
    try:
        with pytest.raises(ProviderFailure) as caught:
            p.complete(MESSAGES, [], threading.Event(), [].append)
        assert str(caught.value) == ('Could not connect to the model provider: the connection was refused or dropped. '
                                     'Check its URL and network connection.')  # a URL the person typed: advice kept
    finally:
        store.close()
    tls = urllib.error.URLError(ssl.SSLCertVerificationError(1, 'certificate verify failed'))
    p, store = chatgpt_provider(tmp_path / 'c', tls)
    try:
        with pytest.raises(ProviderFailure) as caught:
            p.complete(MESSAGES, [], threading.Event(), [].append)
        assert caught.value.code == 'tls' and not caught.value.retryable and not waiting_for_network(caught.value)
        assert 'secure connection could not be verified' in str(caught.value)
    finally:
        store.close()


def test_an_http_error_response_is_closed(tmp_path):
    body = io.BytesIO(b'{"error":{"message":"busy"}}')
    error = urllib.error.HTTPError('https://llm.example.test/v1/responses', 503, 'busy', email.message.Message(), body)
    p, store = chatgpt_provider(tmp_path, error)
    try:
        with pytest.raises(ProviderFailure) as caught:
            p.request('https://llm.example.test/v1/responses', p.headers('responses', 'k'), {})
        assert caught.value.status == 503 and body.closed  # V1-168: no ResourceWarning for an unclosed HTTPError
    finally:
        store.close()


# -- V1-068: sleep in the middle of a stream -----------------------------------------------------------------------------

class Clock:
    """(awake, total): awake stops while 'asleep', total keeps counting."""

    def __init__(self):
        self.awake = self.total = 100.0

    def __call__(self):
        return self.awake, self.total

    def sleep(self, seconds):
        self.total += seconds

    def run(self, seconds):
        self.awake += seconds
        self.total += seconds


def until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def fast_sleep(monkeypatch):
    monkeypatch.setattr(recovery, 'SLEEP_POLL', 0.02)
    monkeypatch.setattr(recovery, 'WAKE_GRACE', 0.5)


def test_a_sleep_counting_clock_abandons_a_stream_silent_since_the_sleep(fast_sleep):
    clock, aborted = Clock(), []
    watchdog = StreamWatchdog(StreamBudget(20.0, 180.0), clock=clock)
    watchdog.arm(lambda: aborted.append(True))
    watchdog.beat()  # the stream was alive before the Mac slept
    clock.sleep(600)  # ten minutes asleep: the awake clock did not move
    assert until(lambda: watchdog.woke is not None)
    time.sleep(0.1)
    assert watchdog.expired is None  # within the grace, nothing is abandoned yet
    clock.run(0.6)
    assert until(lambda: watchdog.expired == 'sleep') and until(lambda: aborted == [True])
    failure = watchdog.failure()
    assert isinstance(failure, StreamStalled) and failure.retryable and failure.phase == 'sleep'
    assert 'slept' in str(failure) and 'No tools were executed' in str(failure)
    watchdog.close()
    # A line after the sleep keeps the stream: it is alive.
    clock = Clock()
    alive = StreamWatchdog(StreamBudget(20.0, 180.0), clock=clock)
    alive.beat()
    clock.sleep(600)
    assert until(lambda: alive.woke is not None)
    alive.beat()
    clock.run(5)
    time.sleep(0.15)
    assert alive.expired is None and alive.woke is None
    alive.close()


def test_a_line_right_after_wake_absorbs_the_sleep(monkeypatch):
    monkeypatch.setattr(recovery, 'SLEEP_POLL', 30.0)  # the watcher does not look during this test; _now() is its look
    clock = Clock()
    watchdog = StreamWatchdog(StreamBudget(20.0, 180.0), clock=clock)
    try:
        watchdog.beat()
        clock.sleep(600)
        watchdog.beat()  # the stream sent a line before the watcher noticed the sleep: it is alive
        watchdog._now()
        assert watchdog.woke is None and watchdog.expired is None
    finally:
        watchdog.close()


def test_did_wake_reaches_open_streams(tmp_path, fast_sleep):
    store = Store(tmp_path)
    try:
        lifecycle = Lifecycle(SimpleNamespace(store=store, emit=lambda *args: None, jobs=None),
                              clocks=lambda: (time.time(), 5.0, 5.0))
        clock = Clock()  # no clock gap: only the native hint reports this sleep
        watchdog = StreamWatchdog(StreamBudget(20.0, 180.0), clock=clock)
        watchdog.beat()
        assert lifecycle.event('did_wake')['accepted'] is True
        assert watchdog.woke is not None
        clock.run(0.6)
        assert until(lambda: watchdog.expired == 'sleep')
        watchdog.close()
    finally:
        store.close()


class HeldStream:
    """A response that sends its lines, then (held) nothing until aborted, or (not held) ends."""

    def __init__(self, events=({'type': 'response.created', 'response': {'status': 'in_progress', 'output': []}},),
                 hold=True):
        self.lines = [b'data: ' + json.dumps(event).encode() + b'\n' for event in events]
        self.reads, self.gate, self.hold = 0, threading.Event(), hold

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def abort(self):
        self.gate.set()

    def readline(self, limit=-1):
        self.reads += 1
        if self.lines:
            return self.lines.pop(0)
        if self.hold:
            self.gate.wait(10)
        return b''


class Scripted:
    """The provider surface complete_responses uses, answering every request with `respond()`."""

    def __init__(self, respond, cache_key=None):
        self.respond, self.bodies, self.response, self.stream_budget = respond, [], None, None
        self.watched_open, self.last_usage = True, {}
        if cache_key is not None:
            self.prompt_cache_key = cache_key

    def key(self, profile):
        return 'scripted-token'

    def headers(self, kind, key):
        return {'Authorization': 'Bearer ' + key}

    def request(self, url, headers, body, timeout=None):
        self.bodies.append(json.loads(json.dumps(body)))
        return self.respond()

    def project_context(self, messages, tools, budget, counter=None, method=None):
        return messages


def test_a_stream_that_died_during_sleep_is_abandoned_soon_after_wake(monkeypatch, fast_sleep):
    clock = Clock()
    monkeypatch.setattr(recovery, 'clocks', clock)
    stream = HeldStream()
    provider = Scripted(lambda: stream)
    outcome = {}

    def run():
        try:
            complete_responses(provider, LUNA, MESSAGES, [], threading.Event(), [].append, 'high')
        except StreamStalled as exc:
            outcome['stall'] = exc
    worker = threading.Thread(target=run)
    worker.start()
    assert until(lambda: stream.reads >= 2)  # the first line came, then silence
    clock.sleep(900)
    time.sleep(0.1)
    clock.run(0.6)  # awake again; the idle budget (180 s) is nowhere near, the grace is over
    worker.join(5)
    assert outcome['stall'].phase == 'sleep' and provider.last_timing['stall'] == 'sleep'


# -- V1-072: the system proxy after a connect failure --------------------------------------------------------------------

class Proxy:
    """A loopback HTTP proxy: CONNECT is refused (no tunnel is ever opened); an absolute-URI GET is answered."""

    def __init__(self):
        self.seen = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                owner.seen.append(('CONNECT', self.path, None))
                self.send_error(403)

            def do_GET(self):
                owner.seen.append(('GET', self.path, self.headers.get('User-Agent')))
                payload = json.dumps({'data': [{'id': 'proxied-model'}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def proxied(tmp_path):
    store = Store(tmp_path)
    p = Providers(tmp_path, Path('missing'), store, lambda *args: None)
    proxy = Proxy()
    lookups = []

    def resolve(host, port, *rest):
        lookups.append(host)
        if host == '127.0.0.1':
            return socket.getaddrinfo(host, port, *rest)
        raise socket.gaierror(socket.EAI_NONAME, 'nodename nor servname provided, or not known')
    p.resolve = resolve
    p.getproxies = lambda: {'http': proxy.url, 'https': proxy.url}
    p.proxy_bypass = lambda host: False
    yield SimpleNamespace(p=p, proxy=proxy, lookups=lookups)
    proxy.close()
    store.close()


def test_a_connect_failure_is_retried_once_through_the_system_proxy(proxied):
    p, proxy = proxied.p, proxied.proxy
    with pytest.raises(ProviderFailure) as caught:
        p.request('https://api.openai.com/v1/models', p.headers('responses', 'k'))
    assert proxy.seen == [('CONNECT', 'api.openai.com:443', None)]  # HTTPS goes through a CONNECT tunnel
    assert caught.value.code == 'connect' and p.via_proxy is False
    with p.request('http://models.example.test/v1/models', p.headers('openai', '')) as response:
        assert json.loads(response.read())['data'][0]['id'] == 'proxied-model'
    assert proxy.seen[-1] == ('GET', 'http://models.example.test/v1/models', 'Pebble/' + VERSION)
    assert p.via_proxy is True
    proxied.lookups.clear()
    with p.request('http://models.example.test/v1/models', p.headers('openai', '')) as response:
        response.read()
    assert proxied.lookups == ['127.0.0.1']  # the route that worked goes first: no direct lookup this time


def test_local_hosts_and_a_proxy_free_mac_are_never_proxied(proxied):
    p, proxy = proxied.p, proxied.proxy
    for url in ('https://10.0.0.5/v1/models', 'https://studio.local/v1/models', 'https://0.0.0.0:9/v1/models'):
        with pytest.raises(ProviderFailure):
            p.request(url, p.headers('openai', ''))
    p.getproxies = lambda: {}
    with pytest.raises(ProviderFailure):
        p.request('https://api.openai.com/v1/models', p.headers('responses', 'k'))
    p.getproxies = lambda: {'https': 'http://user:secret@127.0.0.1:1'}  # a proxy with credentials is not used
    with pytest.raises(ProviderFailure):
        p.request('https://api.openai.com/v1/models', p.headers('responses', 'k'))
    assert proxy.seen == []
    replaced = SimpleNamespace(open=lambda req, timeout=None: (_ for _ in ()).throw(
        urllib.error.URLError(socket.gaierror(8, 'nodename'))))
    p.http, p.getproxies = replaced, (lambda: {'https': proxy.url})
    with pytest.raises(ProviderFailure):
        p.request('https://api.openai.com/v1/models', p.headers('responses', 'k'))
    assert proxy.seen == []  # a replaced transport (a child's, a test's) is never proxied


# -- V1-078: prompt_cache_key on the ChatGPT route -----------------------------------------------------------------------

def completed():
    item = {'type': 'message', 'id': 'm', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': 'ok'}]}
    return HeldStream([{'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                       {'type': 'response.completed', 'response': {'status': 'completed', 'output': [], 'usage': {}}}],
                      hold=False)


@pytest.mark.parametrize('profile,key,sent', [
    (LUNA, 'run-4f2a9c', True),
    ({**LUNA, 'budget': 4096}, 'run-4f2a9c', False),  # at or below 4,096 the body is unchanged
    ({**LUNA, 'kind': 'responses', 'url': 'https://llm.example.test/v1'}, 'run-4f2a9c', False),
    (LUNA, 'not a run id', False),
    (LUNA, None, False),
])
def test_the_chatgpt_route_sends_the_run_id_as_prompt_cache_key(profile, key, sent):
    provider = Scripted(completed, key)
    complete_responses(provider, profile, MESSAGES, [], threading.Event(), [].append, 'medium')
    body = provider.bodies[0]
    assert ('prompt_cache_key' in body) is sent and (not sent or body['prompt_cache_key'] == key)


def test_the_cache_key_is_the_same_on_every_attempt_of_one_run(tmp_path):
    provider = Scripted(completed, 'run-4f2a9c')
    for _ in range(2):
        complete_responses(provider, LUNA, MESSAGES, [], threading.Event(), [].append, 'medium')
    assert [b['prompt_cache_key'] for b in provider.bodies] == ['run-4f2a9c'] * 2
    store = Store(tmp_path)
    try:  # the root provider's slot is unset until the runner names its run, so every existing body is unchanged
        assert Providers(tmp_path, Path('missing'), store, lambda *args: None).prompt_cache_key is None
    finally:
        store.close()
