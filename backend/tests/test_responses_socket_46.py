"""Phase 46 (D154): the Responses WebSocket mode on the ChatGPT route, against a local fake service.

Measured before the change (golden dev d-cb-01 on gpt-6.1-sol): every HTTP request waited 2.7-4.0 s for its response
headers, and turns with under 100 output tokens took 7.5-9 s where Codex took 2.6-3.6 s. OpenAI's WebSocket mode keeps
one connection per run and lets a turn continue the previous response with only its new input items. The fake service
here speaks RFC 6455 over loopback TCP (no TLS, no network, no account); the provider stub hands complete_responses its
connections, so the real request, pass and pool code runs.
"""

import base64
import copy
import hashlib
import json
import socket
import struct
import threading
import time

import pytest

from pebble import responses_socket as rs
from pebble.recovery import ProviderFailure
from pebble.responses import complete_responses, input_items
from pebble.responses_socket import ResponsesSocket, SocketClosed, SocketPool, SocketRefused
from test_responses_stream import TOOLS, USAGE, Stream, call, call_events, done, added, message

PROFILE = {'id': 'p46', 'name': 'Sign-in', 'kind': 'chatgpt', 'url': 'https://api.openai.com/v1',
           'model': 'gpt-6-luna', 'context': 1050000, 'budget': 32768, 'tools': True}
ASK = [{'role': 'user', 'content': 'Read a.py and summarize it.', '_pebble_current': True}]
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'  # RFC 6455; never imported from the module under test


# -- the fake service ----------------------------------------------------------------------------------------------

def frame(opcode, data, final=True):
    head = bytearray([(0x80 if final else 0) | opcode])
    if len(data) < 126:
        head.append(len(data))
    elif len(data) < 65536:
        head += bytes([126]) + struct.pack('!H', len(data))
    else:
        head += bytes([127]) + struct.pack('!Q', len(data))
    return bytes(head) + data


def read_exact(conn, size):
    data = b''
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def client_frame(conn):
    first, second = read_exact(conn, 2)
    assert second & 0x80, 'a client frame must be masked'
    size = second & 0x7F
    if size == 126:
        size = struct.unpack('!H', read_exact(conn, 2))[0]
    elif size == 127:
        size = struct.unpack('!Q', read_exact(conn, 8))[0]
    mask = read_exact(conn, 4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(read_exact(conn, size)))
    return first & 0x0F, data


def created(rid):
    return {'type': 'response.created', 'response': {'id': rid, 'status': 'in_progress', 'output': []}}


def finished(rid, output):
    return {'type': 'response.completed', 'response': {'id': rid, 'status': 'completed', 'output': list(output),
                                                       'usage': USAGE}}


def call_turn(rid, item):
    return lambda payload: [created(rid), *call_events(0, item), finished(rid, [item])]


def text_turn(rid, text):
    item = message(text)
    return lambda payload: [created(rid), added(0, item),
                            {'type': 'response.output_text.delta', 'output_index': 0, 'item_id': item['id'],
                             'delta': text},
                            done(0, item), finished(rid, [item])]


class Service:
    """Loopback WebSocket service: each connection answers the upgrade with the next of `statuses` (101 once they run
    out), then answers each `response.create` with the next turn's actions (events, or ('ping', data), ('fragments',
    event), 'close')."""

    def __init__(self, turns, statuses=()):
        self.turns, self.statuses = list(turns), list(statuses)
        self.handshakes, self.received, self.pongs, self.closed = [], [], [], 0
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        threading.Thread(target=self._accept, daemon=True).start()

    def connect(self):
        return socket.create_connection(self.listener.getsockname(), timeout=10)

    def _accept(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            head = b''
            while b'\r\n\r\n' not in head:
                head += conn.recv(4096)
            lines = head.split(b'\r\n\r\n')[0].decode().split('\r\n')
            headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(':') for line in lines[1:])}
            self.handshakes.append({'path': lines[0].split()[1], **headers})
            status = self.statuses.pop(0) if self.statuses else 101
            if status != 101:
                body = json.dumps({'error': {'message': 'Refused.', 'code': 'refused'}}).encode()
                conn.sendall(f'HTTP/1.1 {status} Refused\r\nContent-Type: application/json\r\n'
                             f'Content-Length: {len(body)}\r\n\r\n'.encode() + body)
                return
            accept = base64.b64encode(hashlib.sha1((headers['sec-websocket-key'] + GUID).encode()).digest()).decode()
            conn.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                          f'Sec-WebSocket-Accept: {accept}\r\n\r\n').encode())
            while True:
                opcode, data = client_frame(conn)
                if opcode == 0x8:
                    self.closed += 1
                    return
                if opcode == 0xA:
                    self.pongs.append(data)
                    continue
                payload = json.loads(data)
                self.received.append(payload)
                for action in self.turns.pop(0)(payload):
                    if action == 'close':
                        return
                    if isinstance(action, tuple) and action[0] == 'ping':
                        conn.sendall(frame(0x9, action[1]))
                    elif isinstance(action, tuple) and action[0] == 'fragments':
                        text = json.dumps(action[1]).encode()
                        conn.sendall(frame(0x1, text[:5], False) + frame(0x0, text[5:9], False) + frame(0x0, text[9:]))
                    else:
                        conn.sendall(frame(0x1, json.dumps(action).encode()))
        except (EOFError, OSError):
            pass
        finally:
            conn.close()

    def stop(self):
        self.listener.close()


class Provider:
    """The provider surface complete_responses uses, with its WebSocket connections opened to `service` and an HTTP
    fallback that serves `http_events` as an SSE stream."""
    watched_open = True

    def __init__(self, service, http_events=(), cache_key='run-0001'):
        self.service, self.http_events = service, list(http_events)
        self.prompt_cache_key, self.socket_pool, self.refused = cache_key, SocketPool(), {}
        self.requests, self.keys, self.response, self.last_usage, self.last_timing = [], ['k1', 'k2'], None, None, None

    def key(self, profile):
        return self.keys[0]

    def renew_key(self, profile, rejected):
        self.keys.pop(0)
        return self.keys[0]

    def headers(self, kind, key):
        return {'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'Pebble/test',
                'Authorization': 'Bearer ' + key}

    def project_context(self, messages, tools, budget, counter=None, method=None):
        return messages

    def socket_route(self, profile):
        return (profile['id'], profile['model']) not in self.refused

    def refuse_sockets(self, profile, status):
        self.refused[(profile['id'], profile['model'])] = status

    def open_response_socket(self, url, headers, timeout):
        sock = self.service.connect()
        try:
            return ResponsesSocket.handshake(sock, 'api.openai.com', '/v1/responses', headers)
        except BaseException:
            sock.close()
            raise

    def request(self, url, headers, body, timeout=None):
        self.requests.append(copy.deepcopy(body))
        lines = []
        for event in self.http_events:
            lines += [b'data: ' + json.dumps(event).encode() + b'\n', b'\n']
        return Stream(lines, None)


def ask(provider, messages, cancel=None, tokens=None):
    return complete_responses(provider, PROFILE, messages, TOOLS, cancel or threading.Event(),
                              (tokens if tokens is not None else []).append, 'medium')


def after(messages, result, output='print(1)\n'):
    """The run's next messages: the turn's reply and its tool's result, as the runner appends them."""
    return messages + [result, {'role': 'tool', 'tool_call_id': result['tool_calls'][0]['id'], 'content': output}]


@pytest.fixture
def service():
    made = []

    def make(turns, statuses=()):
        made.append(Service(turns, statuses))
        return made[-1]
    yield make
    for each in made:
        each.stop()


# -- the turns ------------------------------------------------------------------------------------------------------

def test_a_run_keeps_one_connection_and_continues_its_response_with_only_the_new_items(service):
    svc = service([call_turn('resp_1', call()), text_turn('resp_2', 'a.py prints 1.')])
    provider = Provider(svc)
    first = ask(provider, ASK)
    assert first['tool_calls'][0]['function']['name'] == 'Read'
    assert provider.last_timing['transport'] == 'socket' and provider.last_timing['continued'] is False
    second_messages = after(ASK, first)
    tokens = []
    second = ask(provider, second_messages, tokens=tokens)
    assert second['content'] == 'a.py prints 1.' and tokens == ['a.py prints 1.']
    assert provider.last_timing['transport'] == 'socket_reused' and provider.last_timing['continued'] is True
    assert len(svc.handshakes) == 1 and provider.requests == []
    one, two = svc.received
    assert one['type'] == two['type'] == 'response.create' and 'stream' not in one
    assert one['input'] == input_items(ASK) and 'previous_response_id' not in one
    assert two['previous_response_id'] == 'resp_1'
    assert two['input'] == [{'type': 'function_call_output', 'call_id': 'call_1', 'output': 'print(1)\n'}]
    assert two['prompt_cache_key'] == 'run-0001' and two['tools'] == one['tools'] and two['store'] is False
    shook = svc.handshakes[0]
    assert shook['path'] == '/v1/responses' and shook['authorization'] == 'Bearer k1'
    assert shook['session_id'] == 'run-0001' and 'content-type' not in shook and 'accept' not in shook
    assert shook['openai-beta'] == 'responses_websockets=2026-02-06'  # as Codex's open-source client sends it


def test_a_rewritten_history_is_sent_whole_on_the_same_connection(service):
    svc = service([call_turn('resp_1', call()), text_turn('resp_2', 'Done.')])
    provider = Provider(svc)
    first = ask(provider, ASK)
    changed = after([{**ASK[0], 'content': 'Read a.py and summarise it briefly.'}], first)
    ask(provider, changed)
    two = svc.received[1]
    assert 'previous_response_id' not in two and two['input'] == input_items(changed)
    assert len(svc.handshakes) == 1 and provider.last_timing['continued'] is False


def test_a_refused_upgrade_keeps_http_for_the_profile(service):
    svc = service([], statuses=[404])
    http = [created('resp_h'), *call_events(0, call()), finished('resp_h', [call()])]
    provider = Provider(svc, http_events=http)
    result = ask(provider, ASK)
    assert result['tool_calls'][0]['id'] == 'call_1' and len(provider.requests) == 1
    assert provider.refused == {('p46', 'gpt-6-luna'): 404} and provider.last_timing.get('transport') is None
    ask(provider, after(ASK, result))  # HTTP again, no second upgrade
    assert len(svc.handshakes) == 1 and len(provider.requests) == 2


def test_an_upgrade_refused_with_401_renews_the_sign_in_once(service):
    svc = service([text_turn('resp_1', 'Hello.')], statuses=[401])
    provider = Provider(svc)
    assert ask(provider, ASK)['content'] == 'Hello.'
    assert [h['authorization'] for h in svc.handshakes] == ['Bearer k1', 'Bearer k2'] and not provider.refused


def test_a_lost_continuation_is_asked_again_whole_on_a_new_connection(service):
    lost = {'type': 'error', 'status': 400, 'error': {'type': 'invalid_request_error', 'code': 'previous_response_not_found',
                                                      'message': 'Previous response not found.', 'param': 'previous_response_id'}}
    svc = service([call_turn('resp_1', call()), lambda payload: [lost], text_turn('resp_3', 'Done.')])
    provider = Provider(svc)
    first = ask(provider, ASK)
    messages = after(ASK, first)
    assert ask(provider, messages)['content'] == 'Done.'
    assert svc.received[1]['previous_response_id'] == 'resp_1'
    assert 'previous_response_id' not in svc.received[2] and svc.received[2]['input'] == input_items(messages)
    assert len(svc.handshakes) == 2


def test_a_connection_dropped_mid_turn_fails_as_a_transient_error_and_the_next_turn_starts_over(service):
    svc = service([call_turn('resp_1', call()), lambda payload: [created('resp_2'), 'close'],
                   text_turn('resp_3', 'Done.')])
    provider = Provider(svc)
    first = ask(provider, ASK)
    messages = after(ASK, first)
    with pytest.raises(SocketClosed):
        ask(provider, messages)
    assert isinstance(SocketClosed('x'), ConnectionError)  # recovery.transient retries it
    assert ask(provider, messages)['content'] == 'Done.'
    assert len(svc.handshakes) == 2 and 'previous_response_id' not in svc.received[2]


def test_pings_are_answered_and_fragmented_events_are_joined(service):
    item = message('Hi there.')
    svc = service([lambda payload: [created('resp_1'), ('ping', b'are-you-there'), added(0, item),
                                     ('fragments', {'type': 'response.output_text.delta', 'output_index': 0,
                                                    'item_id': item['id'], 'delta': 'Hi there.'}),
                                     done(0, item), finished('resp_1', [item])]])
    provider = Provider(svc)
    assert ask(provider, ASK)['content'] == 'Hi there.'
    deadline = time.monotonic() + 5
    while not svc.pongs and time.monotonic() < deadline:
        time.sleep(0.01)
    assert svc.pongs == [b'are-you-there']


def test_a_turn_that_begins_mid_response_is_refused_and_its_connection_closed(service):
    item = message('Stale.')
    svc = service([lambda payload: [{'type': 'response.output_text.delta', 'output_index': 0, 'item_id': 'm',
                                     'delta': 'Stale.'}, done(0, item), finished('resp_1', [item])]])
    provider = Provider(svc)
    with pytest.raises(ProviderFailure, match='began mid-response'):
        ask(provider, ASK)
    assert provider.socket_pool.take('run-0001') is None


def test_stopping_closes_the_connection(service):
    item = message('Long answer.')
    cancel = threading.Event()
    svc = service([lambda payload: [created('resp_1'), added(0, item), 'close']])
    provider = Provider(svc)
    tokens = []
    cancel.set()
    with pytest.raises(InterruptedError):
        complete_responses(provider, PROFILE, ASK, TOOLS, cancel, tokens.append, 'medium')
    assert provider.socket_pool.take('run-0001') is None


def test_other_routes_and_small_windows_keep_http(service):
    from pebble.responses import socket_pool
    provider = Provider(service([]))
    body = {'prompt_cache_key': 'run-0001'}
    assert socket_pool(provider, PROFILE, body) is provider.socket_pool
    assert socket_pool(provider, {**PROFILE, 'kind': 'responses'}, body) is None
    assert socket_pool(provider, {**PROFILE, 'url': 'https://chatgpt.invalid/backend-api/codex'}, body) is None
    assert socket_pool(provider, PROFILE, {}) is None  # no cache key: at or below the 4K budget
    provider.watched_open = False
    assert socket_pool(provider, PROFILE, body) is None


def test_the_providers_switch_and_seams(tmp_path, monkeypatch):
    from pebble.app import App
    from pebble import providers
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *event: None)
    try:
        model = app.model
        monkeypatch.setenv('PEBBLE_RESPONSES_SOCKET', '1')
        monkeypatch.setattr(model, 'getproxies', lambda: {})
        assert model.socket_route(PROFILE) is True
        model.refuse_sockets(PROFILE, 404)
        assert model.socket_route(PROFILE) is False
        model.sockets_refused_at[(PROFILE.get('id'), PROFILE.get('model'))] -= providers.SOCKET_RETRY + 1
        assert model.socket_route(PROFILE) is False  # an HTTP refusal holds until Pebble restarts
        model.refuse_sockets(PROFILE, 0)  # a broken upgrade (may be transient) ...
        assert model.socket_route(PROFILE) is False
        model.sockets_refused_at[(PROFILE.get('id'), PROFILE.get('model'))] -= providers.SOCKET_RETRY + 1
        assert model.socket_route(PROFILE) is True  # ... is tried again after SOCKET_RETRY
        model.sockets_refused.clear()
        monkeypatch.setattr(model, 'request', lambda *a, **k: None)  # a test's or a child's transport keeps HTTP
        assert model.socket_route(PROFILE) is False
        monkeypatch.undo()
        monkeypatch.setenv('PEBBLE_RESPONSES_SOCKET', '0')
        assert model.socket_route(PROFILE) is False
        assert providers.SOCKET_DEFAULT in {'0', '1'}
    finally:
        app.close()


# -- frames and the pool --------------------------------------------------------------------------------------------

@pytest.mark.parametrize('size', [0, 1, 3, 4, 125, 126, 127, 65535, 65536, 70001])
def test_masking_and_frame_lengths_round_trip(size):
    data = bytes(range(256)) * (size // 256 + 1)
    data = data[:size]
    mask = b'\x01\x7f\x80\xff'
    assert rs._mask(data, mask) == bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    left, right = socket.socketpair()
    try:
        sender = threading.Thread(target=ResponsesSocket(left)._send, args=(0x1, data))
        sender.start()  # a large frame fills the pair's buffer until this side reads it
        opcode, received = client_frame(right)
        sender.join(10)
        assert opcode == 0x1 and received == data
    finally:
        left.close()
        right.close()


def test_a_close_frame_and_a_binary_message_end_the_connection():
    left, right = socket.socketpair()
    try:
        conn = ResponsesSocket(left)
        right.sendall(frame(0x2, b'\x00\x01'))
        with pytest.raises(SocketClosed, match='binary'):
            conn.receive()
        right.sendall(frame(0x8, struct.pack('!H', 1011)))
        with pytest.raises(SocketClosed, match='code 1011') as closed:
            ResponsesSocket(left).receive()
        assert closed.value.code == 1011
    finally:
        left.close()
        right.close()


def test_an_oversized_message_is_refused_before_it_is_read():
    left, right = socket.socketpair()
    try:
        right.sendall(bytes([0x81, 127]) + struct.pack('!Q', rs.MAX_MESSAGE + 1))
        with pytest.raises(ValueError, match='response limit'):
            ResponsesSocket(left).receive()
    finally:
        left.close()
        right.close()


class Fake:
    def __init__(self, clock):
        self.clock = clock
        self.opened = self.used = clock()
        self.broken = self.busy = False
        self.closed = False

    def reusable(self, now=None):
        return ResponsesSocket.reusable(self, now)

    def close(self):
        self.closed = True


def test_the_pool_keeps_one_connection_per_run_and_closes_stale_ones():
    now = [1000.0]
    clock = lambda: now[0]  # noqa: E731
    pool = SocketPool(clock)
    conns = {f'run-{i}': Fake(clock) for i in range(rs.POOL + 1)}
    for name, conn in conns.items():
        now[0] += 1
        pool.keep(name, conn)
    assert conns['run-0'].closed and pool.take('run-0') is None  # the least recently used beyond POOL
    taken = pool.take('run-1')
    assert taken is conns['run-1'] and taken.busy and pool.take('run-1') is None  # busy: one turn at a time
    pool.keep('run-1', taken)
    now[0] += rs.IDLE
    assert pool.take('run-1') is None and conns['run-1'].closed  # idle too long
    fresh = Fake(clock)
    pool.keep('run-9', fresh)
    now[0] += rs.LIFETIME
    assert pool.take('run-9') is None and fresh.closed  # too old


def test_a_connection_notice_before_the_response_is_skipped(service):
    item = message('Fine.')
    svc = service([lambda payload: [{'type': 'rate_limits.updated', 'rate_limits': []}, created('resp_1'), added(0, item),
                                    {'type': 'response.output_text.delta', 'output_index': 0, 'item_id': item['id'],
                                     'delta': 'Fine.'}, done(0, item), finished('resp_1', [item])]])
    provider = Provider(svc)
    assert ask(provider, ASK)['content'] == 'Fine.' and provider.last_timing['transport'] == 'socket'


def test_the_upgrade_answer_is_checked_with_rfc_6455s_own_example():
    # Measured 21:03 on the real service: the module's GUID was wrong, so every genuine 101 answer was refused, and the
    # loopback service (which copied the same constant) agreed. RFC 6455 section 1.3's example settles it.
    from pebble.responses_socket import _accept
    assert _accept('dGhlIHNhbXBsZSBub25jZQ==') == 's3pPLMBiTxaQ9kYGzzhZRbK+xOo='
