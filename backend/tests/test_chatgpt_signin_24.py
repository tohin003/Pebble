"""Phase 24 (t24-auth): ChatGPT sign-in failures explain themselves, a refresh survives the network, a 401 refreshes
once, a refused vault write keeps the renewed sign-in, and sign-in network waits never hold the lock access() needs.

V1-001, V1-008, V1-010 (the sign-in log line), V1-070, V1-071, V1-157 and V1-158 in docs/PEBBLE_V1_INSPECTION.md.
Loopback servers (plain HTTP standing in for auth.openai.com, and one TLS server with a self-signed certificate),
scripted transports and fake vaults only: no external network, no model, no Keychain.
"""
import copy
import datetime
import email.message
import http.client
import io
import json
import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID

from pebble import VERSION, chatgpt
from pebble.app import App
from pebble.chatgpt import ChatGPT, ISSUER, RECONNECT, REUSED, UNCHANGED, SignInError
from pebble.providers import NoRedirect, Providers
from pebble.recovery import ProviderFailure, transient, waiting_for_network
from pebble.store import Store

DISCOVERY = {'issuer': ISSUER, 'authorization_endpoint': ISSUER + '/oauth/authorize',
             'token_endpoint': ISSUER + '/oauth/token', 'jwks_uri': ISSUER + '/.well-known/jwks.json',
             'revocation_endpoint': ISSUER + '/oauth/revoke'}
DISCOVERY_PATH = '/.well-known/openid-configuration'
CANARY = 'canary-body-3f9e1c'  # in every error body: it must never reach a message or a log line
SCOPE = 'resource.invoke chatgpt.tokens.use.direct'
LUNA = {'id': 'luna', 'name': 'Luna', 'kind': 'chatgpt', 'url': 'https://chatgpt.invalid/backend-api/codex',
        'model': 'gpt-6-luna', 'context': 1050000, 'budget': 32768, 'tools': True, 'context_mode': 'custom',
        'budget_version': 2, 'context_version': 1, 'connection_id': 'luna'}
MESSAGES = [{'role': 'user', 'content': 'do you know my name', '_pebble_current': True}]


def tokens(access, refresh):
    return {'access_token': access, 'refresh_token': refresh, 'token_type': 'Bearer', 'expires_in': 3600,
            'scope': SCOPE}


def record(access='valid-access', refresh='RT1', expires=None):
    return {'access_token': access, 'refresh_token': refresh, 'client_id': 'oaiapp_fixture',
            'expires_at': time.time() + 3600 if expires is None else expires, 'scopes': SCOPE.split()}


@pytest.fixture(autouse=True)
def loopback_only(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ('127.0.0.1', '::1'):
            raise AssertionError('These tests open loopback connections only.')
        return connect(self, address)
    monkeypatch.setattr(socket.socket, 'connect', guarded)


class FakeVault:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key, '')

    def set(self, key, value):
        assert len(value) <= 2400
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


class MemoryRecords:
    """chatgpt.SecretRecords' surface over one dict; set() fails while `failing`."""

    def __init__(self, **records):
        self.values, self.failing, self.sets = {'oauth:' + k: copy.deepcopy(v) for k, v in records.items()}, False, 0

    def get(self, name):
        if name not in self.values:
            raise ValueError('Continue with ChatGPT to reconnect this account.')
        return copy.deepcopy(self.values[name])

    def set(self, name, value):
        self.sets += 1
        if self.failing:
            raise ValueError('Keychain could not save the key.')
        self.values[name] = copy.deepcopy(value)

    def delete(self, name):
        self.values.pop(name, None)


class AuthServer:
    """A loopback stand-in for auth.openai.com: each path answers from its route; every request is recorded."""

    def __init__(self, context=None):
        self.routes, self.requests = {}, []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else b''
                owner.requests.append({'method': self.command, 'path': self.path, 'body': body,
                                       'user_agent': self.headers.get('User-Agent'),
                                       'accept': self.headers.get('Accept'),
                                       'content_type': self.headers.get('Content-Type')})
                status, payload, headers, delay = owner.routes.get(self.path, (404, b'{}', {}, 0))
                time.sleep(delay)
                try:
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass
            do_GET = do_POST = answer

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        if context is not None:
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server.server_address[1]

    def route(self, path, status=200, payload=None, headers=None, delay=0):
        data = payload if isinstance(payload, bytes) else json.dumps(payload if payload is not None else {}).encode()
        self.routes[path] = (status, data, headers or {}, delay)

    def of(self, method):
        return [r for r in self.requests if r['method'] == method]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def servers():
    made = []

    def make(context=None):
        made.append(AuthServer(context))
        return made[-1]
    yield make
    for server in made:
        server.close()


def to_loopback(port):
    """An opener like Pebble's (no proxy, no redirect) whose https://auth.openai.com is the loopback server."""
    class Handler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(lambda host, timeout=None, **kw: http.client.HTTPConnection('127.0.0.1', port,
                                                                                            timeout=timeout), req)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), Handler())


def tls_context(folder):
    """A server context with a self-signed certificate for auth.openai.com, which certifi does not trust."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'auth.openai.com')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('auth.openai.com')]), critical=False)
            .sign(key, hashes.SHA256()))
    (folder / 'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / 'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / 'cert.pem', folder / 'key.pem')
    return context


@pytest.fixture
def provider(tmp_path):
    store = Store(tmp_path)
    p = Providers(tmp_path, Path('missing'), store, lambda *args: None)
    p.vault = FakeVault()
    p.getproxies = lambda: {}  # no system proxy on these routes
    yield p
    store.close()


def lines_of(caplog):
    return [r for r in caplog.records if r.name == 'pebble.chatgpt']


# -- V1-001: eight failure classes, eight codes and messages, one content-free WARNING each ---------------------------

def test_eight_failure_classes_give_distinct_codes_messages_and_one_content_free_warning(
        provider, servers, tmp_path, monkeypatch, caplog):
    server, tls = servers(), servers(tls_context(tmp_path))
    monkeypatch.setattr(chatgpt, 'TIMEOUT', 0.4)

    class Refusing:
        """Pebble's own connection (connect_any) on a socket whose connect is refused, as a closed port answers."""
        def settimeout(self, value):
            pass

        def connect(self, address):
            raise ConnectionRefusedError(61, 'Connection refused')

        def close(self):
            pass
    real_factory = provider.socket_factory
    body = {'error': CANARY, 'access_token': CANARY}
    server.route('/slow', 200, DISCOVERY, delay=1.5)
    for path, status in (('/forbidden', 403), ('/busy', 429), ('/down', 503)):
        server.route(path, status, body)
    server.route('/moved', 302, body, headers={'Location': ISSUER + '/elsewhere'})
    own, loop = provider.http, to_loopback(server.port)

    def resolve_to(port):
        return lambda host, *rest: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]

    def no_name(host, *rest):
        raise socket.gaierror(socket.EAI_NONAME, 'nodename nor servname provided, or not known')

    cases = [('timeout', loop, None, '/slow'), ('offline', own, no_name, DISCOVERY_PATH),
             ('connect', own, resolve_to(9), DISCOVERY_PATH),
             ('tls', own, resolve_to(tls.port), DISCOVERY_PATH), ('http_403', loop, None, '/forbidden'),
             ('http_429', loop, None, '/busy'), ('http_503', loop, None, '/down'), ('redirect', loop, None, '/moved')]
    auth, messages = ChatGPT(provider), {}
    for code, opener, resolve, path in cases:
        provider.http = opener
        if resolve is not None:
            provider.resolve = resolve
        provider.socket_factory = (lambda *args: Refusing()) if code == 'connect' else real_factory
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger='pebble.chatgpt'):
            with pytest.raises(SignInError) as caught:
                auth.http_json(ISSUER + path)
        error = caught.value
        assert error.code == code, (code, error.code)
        assert str(error).endswith(UNCHANGED) and 'Start a fresh sign-in' not in str(error)
        assert CANARY not in str(error) and error.__cause__ is None
        [line] = lines_of(caplog)
        text = line.getMessage()
        assert line.levelno == logging.WARNING and f'GET {path} -> {code} (' in text, text
        assert CANARY not in text and 'Bearer' not in text
        messages[code] = str(error)
    assert len(messages) == 8 and len(set(messages.values())) == 8  # one plain message per class
    assert 'HTTP 403' in messages['http_403'] and 'HTTP 503' in messages['http_503'] and 'offline' in messages['offline']
    assert 'HTTP 302' in messages['redirect']


def test_a_failed_code_exchange_logs_no_code_verifier_or_body(provider, servers, caplog):
    server = servers()
    server.route('/oauth/token', 400, {'error': 'invalid_grant', 'error_description': CANARY})
    provider.http = to_loopback(server.port)
    with caplog.at_level(logging.DEBUG, logger='pebble.chatgpt'):
        with pytest.raises(SignInError) as caught:
            ChatGPT(provider).http_json(ISSUER + '/oauth/token', {'grant_type': 'authorization_code',
                                                                  'code': 'one-use-code-7d1', 'code_verifier': 'v-9a2'})
    assert caught.value.code == 'http_400' and caught.value.grant == 'invalid_grant'
    assert str(caught.value).endswith(UNCHANGED) and 'expired or been used' in str(caught.value)
    text = ' '.join(r.getMessage() for r in lines_of(caplog))
    assert 'POST /oauth/token -> http_400' in text
    assert not any(secret in text + str(caught.value) for secret in ('one-use-code-7d1', 'v-9a2', CANARY))


# -- V1-001: every auth request names Pebble ----------------------------------------------------------------------------

def test_every_auth_request_carries_the_pebble_user_agent_and_asks_for_json(provider, servers):
    server = servers()
    server.route(DISCOVERY_PATH, 200, DISCOVERY)
    server.route('/.well-known/jwks.json', 200, {'keys': []})
    server.route('/oauth/token', 200, tokens('a', 'r'))
    server.route('/oauth/revoke', 200, {})
    provider.http = to_loopback(server.port)
    auth = ChatGPT(provider)
    started = auth.start()  # the sign-in's discovery GET
    assert started['url'].startswith(ISSUER + '/oauth/authorize?')
    auth.cancel()
    auth.http_json(ISSUER + '/oauth/token', {'grant_type': 'refresh_token', 'refresh_token': 'r'})
    auth._get(ISSUER + '/.well-known/jwks.json')
    auth.records.set('oauth:conn', record())
    assert auth.sign_out('conn')['revoked'] is True  # the revocation POST
    seen = {(r['method'], r['path']) for r in server.requests}
    assert seen == {('GET', DISCOVERY_PATH), ('POST', '/oauth/token'), ('GET', '/.well-known/jwks.json'),
                    ('POST', '/oauth/revoke')}
    for request in server.requests:
        assert request['user_agent'] == 'Pebble/' + VERSION and request['accept'] == 'application/json', request
        assert request['content_type'] == ('application/x-www-form-urlencoded' if request['method'] == 'POST' else None)


# -- V1-001/V1-008: the discovery document is kept; only GETs are retried; a code is posted once --------------------------

def test_discovery_is_cached_its_last_good_copy_serves_and_only_gets_are_retried(provider, servers, caplog):
    server = servers()
    server.route(DISCOVERY_PATH, 200, DISCOVERY)
    server.route('/.well-known/jwks.json', 503, {'error': CANARY})
    provider.http = to_loopback(server.port)
    now, waits = [1000.0], []
    auth = ChatGPT(provider)
    auth.clock, auth.sleep = (lambda: now[0]), waits.append
    first = auth.config()
    assert auth.config() == first and len(server.of('GET')) == 1  # cached for this process
    now[0] += chatgpt.DISCOVERY_SECONDS + 1
    server.route(DISCOVERY_PATH, 503, {'error': CANARY})
    with caplog.at_level(logging.WARNING, logger='pebble.chatgpt'):
        assert auth.config() == first  # the fetch failed: the last good copy serves
    assert len(server.of('GET')) == 4 and waits == [1.0, 3.0]  # retried twice, after 1 s and 3 s
    assert any('last good discovery document (http_503)' in r.getMessage() for r in lines_of(caplog))
    fresh = ChatGPT(provider)
    fresh.sleep = waits.append
    with pytest.raises(SignInError) as caught:
        fresh.config()  # no copy yet: the classified error, after the same three tries
    assert caught.value.code == 'http_503' and len(server.of('GET')) == 7 and waits == [1.0, 3.0] * 2
    with pytest.raises(SignInError):
        fresh._get(ISSUER + '/.well-known/jwks.json')  # the JWKS GET is retried the same way
    assert len(server.of('GET')) == 10
    server.route(DISCOVERY_PATH, 403, {'error': CANARY})
    with pytest.raises(SignInError) as caught:
        fresh.config()  # a refusal is not asked again
    assert caught.value.code == 'http_403' and len(server.of('GET')) == 11


@pytest.mark.parametrize('status,delay,code', [(503, 0, 'http_503'), (200, 1.5, 'timeout')])
def test_the_code_exchange_post_is_sent_exactly_once(provider, servers, monkeypatch, caplog, status, delay, code):
    monkeypatch.setattr(chatgpt, 'TIMEOUT', 0.4)
    server = servers()
    server.route(DISCOVERY_PATH, 200, DISCOVERY)
    server.route('/oauth/token', status, tokens('a', 'r') if status == 200 else {'error': CANARY}, delay=delay)
    provider.http = to_loopback(server.port)
    auth = ChatGPT(provider)
    auth.sleep = lambda seconds: None
    auth.start()
    pending = auth.pending
    with caplog.at_level(logging.WARNING, logger='pebble.chatgpt'):
        answer = auth.callback({'state': [pending['state']], 'client_id': ['oaiapp_fixture'],
                                'code': ['one-use-code-7d1']})
    assert answer[0] == 400 and auth.status()['state'] == 'failed'
    assert auth.status()['error'].endswith(UNCHANGED) and SignInError(code).args[0] == auth.status()['error']
    assert len(server.of('POST')) == 1  # never re-posted, whatever the failure
    assert 'one-use-code-7d1' not in caplog.text and CANARY not in caplog.text
    assert auth.pending is None and auth.server is None


# -- V1-008: a refresh that got no answer is retried; a revoked one says reconnect --------------------------------------

def test_a_refresh_transport_failure_is_retryable_and_leaves_the_sign_in_unchanged(provider, servers):
    server = servers()
    server.route('/oauth/token', 503, {'error': CANARY})
    provider.http = to_loopback(server.port)
    auth = ChatGPT(provider)
    auth.config = lambda: DISCOVERY
    auth.records = MemoryRecords(conn=record(expires=0))
    with pytest.raises(ProviderFailure) as caught:
        auth.access('conn')
    busy = caught.value
    assert busy.retryable and transient(busy) and not waiting_for_network(busy) and busy.code == 'http_503'
    assert len(server.of('POST')) == 1  # one presentation per attempt; the model request decides to ask again
    provider.http = provider._own_http

    def no_name(host, *rest):
        raise socket.gaierror(socket.EAI_NONAME, 'nodename nor servname provided, or not known')
    provider.resolve = no_name
    with pytest.raises(ProviderFailure) as caught:
        auth.access('conn')
    offline = caught.value
    assert offline.retryable and waiting_for_network(offline) and offline.code == 'offline'
    for failure in (busy, offline):
        text = str(failure)
        assert text.startswith('Could not connect to the model provider (ChatGPT) to renew the sign-in')
        assert 'reconnect' not in text.lower() and 'fresh sign-in' not in text and CANARY not in text
    assert auth.records.get('oauth:conn')['refresh_token'] == 'RT1' and auth.records.sets == 0


@pytest.mark.parametrize('status,body,message', [
    (400, {'error': 'invalid_grant', 'error_description': CANARY}, RECONNECT),
    (400, {'error': {'code': 'refresh_token_reused', 'message': CANARY}}, REUSED),
    # m24a merge (t24-bench's V1-015 request): the RFC 6749 section 5.2 form says who renewed it too.
    (400, {'error': 'refresh_token_reused', 'error_description': CANARY}, REUSED),
    (401, {'error': {'code': 'refresh_token_expired', 'message': CANARY}}, RECONNECT),
    (401, {'detail': CANARY}, RECONNECT),
])
def test_a_revoked_refresh_gives_the_reconnect_message(provider, servers, status, body, message):
    server = servers()
    server.route('/oauth/token', status, body)
    provider.http = to_loopback(server.port)
    auth = ChatGPT(provider)
    auth.config = lambda: DISCOVERY
    auth.records = MemoryRecords(conn=record(expires=0))
    with pytest.raises(SignInError) as caught:
        auth.access('conn')
    assert caught.value.code == 'reconnect_required' and str(caught.value) == message
    assert not transient(caught.value) and 'Continue with ChatGPT' in message and CANARY not in message


class Stream:
    def __init__(self, text):
        item = {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': text}]}
        events = [{'type': 'response.output_text.delta', 'output_index': 0, 'item_id': 'msg_1', 'delta': text},
                  {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                  {'type': 'response.completed', 'response': {'status': 'completed', 'output': [],
                                                              'usage': {'input_tokens': 9, 'output_tokens': 2}}}]
        self.lines = [b'data: ' + json.dumps(e).encode() + b'\n' for e in events]
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def close(self):
        self.closed = True

    def readline(self, limit=-1):
        return self.lines.pop(0) if self.lines else b''


class Http:
    """Providers.http: each model request through the real Providers.request; `respond(n)` answers request n."""

    def __init__(self, respond):
        self.respond, self.authorizations = respond, []

    def open(self, req, timeout=None):
        self.authorizations.append(req.headers.get('Authorization'))
        return self.respond(req, len(self.authorizations))


def refused(req):
    headers = email.message.Message()
    return urllib.error.HTTPError(req.full_url, 401, 'Unauthorized', headers, io.BytesIO(b'{"error":{"message":"x"}}'))


def until(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


# Merge (m24b): the runner now marks a retry after a network failure (t24-agent applied this track's request), so the
# offline refresh shows "Waiting for the network", as chatgpt._renewal_failure's network=True intends; a refresh that
# got an HTTP 503 keeps "Reconnecting to ChatGPT". Both runs complete.
@pytest.mark.parametrize('failure, label', [(SignInError('offline'), 'Waiting for the network (attempt 2)'),
                                            (SignInError('http_503', status=503), 'Reconnecting to ChatGPT (attempt 2)')])
def test_a_refresh_that_got_no_answer_shows_reconnecting_and_the_run_completes(tmp_path, failure, label):
    events = []
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: events.append(copy.deepcopy(args)))
    try:
        app.store.set_setting('model_path', '')
        app.store.set_setting('model_profiles', [dict(LUNA)])
        app.model.select('luna')
        auth = ChatGPT(app.model)
        auth.config = lambda: DISCOVERY
        auth.records = MemoryRecords(luna=record(expires=0))
        presented = []

        def refresh(url, form=None):
            presented.append(form['refresh_token'])
            if len(presented) == 1:
                raise failure  # a name lookup failure (nothing was sent), or a 503 from the token endpoint
            return tokens('fresh-access', 'RT2')
        auth.http_json = refresh
        app.model.auth = auth
        app.model.http = Http(lambda req, number: Stream('Yes: you told me your name is Asha.'))
        cid = app.new_chat(None)['id']
        rid = app.send(cid, 'do you know my name')['run_id']
        assert until(lambda: app.active is None and app.runs.get(rid)['state'] not in ('queued', 'running', 'retry_wait'))
        run = app.runs.get(rid)
        assert run['state'] == 'completed', (run['state'], run.get('error'))
        assert presented == ['RT1', 'RT1']  # the first was never sent; RT2 was saved after the second
        assert auth.records.get('oauth:luna')['refresh_token'] == 'RT2'
        assert app.model.http.authorizations == ['Bearer fresh-access']  # no model request was made without a token
        items = app.dispatch('run_activity', {'run_id': rid})['items']
        [retry] = [i for i in items if i['kind'] == 'retry']
        assert retry['label'] == label
    finally:
        app.active = app.active_run = None
        app.close()


# -- V1-070: a 401 on the model route refreshes once ------------------------------------------------------------------

def model_provider(tmp_path, respond, presented):
    store = Store(tmp_path)
    p = Providers(tmp_path, Path('missing'), store, lambda *args: None)
    store.set_setting('model_profiles', [dict(LUNA)])
    store.set_setting('active_profile', 'luna')
    auth = ChatGPT(p)
    auth.config = lambda: DISCOVERY
    auth.records = MemoryRecords(luna=record('old-access', 'RT1'))

    def refresh(url, form=None):
        presented.append(form['refresh_token'])
        return tokens('new-access', 'RT2' if len(presented) == 1 else 'RT3')
    auth.http_json = refresh
    p.auth, p.http = auth, Http(respond)
    return p, store


def test_a_model_route_401_forces_one_refresh_and_one_retry(tmp_path):
    def respond(req, number):
        if number == 1:
            raise refused(req)
        return Stream('Hello again.')
    presented, shown = [], []
    p, store = model_provider(tmp_path, respond, presented)
    try:
        result = p.complete(MESSAGES, [], threading.Event(), shown.append)
        assert result['content'] == 'Hello again.' and shown == ['Hello again.']
        assert presented == ['RT1'] and p.http.authorizations == ['Bearer old-access', 'Bearer new-access']
        assert p.auth.records.get('oauth:luna')['refresh_token'] == 'RT2'
    finally:
        store.close()


def test_a_second_model_route_401_gives_the_reconnect_message(tmp_path):
    presented = []
    p, store = model_provider(tmp_path, lambda req, number: (_ for _ in ()).throw(refused(req)), presented)
    try:
        with pytest.raises(SignInError) as caught:
            p.complete(MESSAGES, [], threading.Event(), [].append)
        assert caught.value.code == 'reconnect_required' and str(caught.value) == RECONNECT
        assert caught.value.status == 401 and not transient(caught.value)
        assert presented == ['RT1'] and len(p.http.authorizations) == 2  # one refresh, one retry, then it stops
    finally:
        store.close()


def test_other_routes_never_refresh_or_retry_a_401(tmp_path):
    presented = []
    p, store = model_provider(tmp_path, lambda req, number: (_ for _ in ()).throw(refused(req)), presented)
    try:
        assert p.renew_key({**LUNA, 'kind': 'responses'}, 'k') is None
        store.set_setting('model_profiles', [{**LUNA, 'kind': 'responses', 'url': 'https://llm.example.test/v1',
                                              'has_key': False}])
        with pytest.raises(ProviderFailure) as caught:
            p.complete(MESSAGES, [], threading.Event(), [].append)
        assert caught.value.status == 401 and presented == [] and len(p.http.authorizations) == 1
    finally:
        store.close()


# -- V1-071: a renewed record the vault refused stays in memory ---------------------------------------------------------

def test_a_renewed_record_survives_a_failed_vault_write_and_is_saved_on_the_next_access(provider, caplog):
    auth = ChatGPT(provider)
    auth.config = lambda: DISCOVERY
    auth.records = MemoryRecords(conn=record('old-access', 'RT1', expires=0))
    auth.records.failing = True
    presented = []

    def refresh(url, form=None):
        presented.append(form['refresh_token'])
        return tokens('access-%d' % len(presented), 'RT%d' % (len(presented) + 1))
    auth.http_json = refresh
    with caplog.at_level(logging.WARNING, logger='pebble.chatgpt'):
        assert auth.access('conn') == 'access-1'
        assert auth.records.values['oauth:conn']['refresh_token'] == 'RT1' and 'conn' in auth.unsaved
        assert auth.access('conn') == 'access-1'  # from memory: RT1 was consumed, so it is never presented again
    assert presented == ['RT1'] and 'conn' in auth.unsaved
    text = caplog.text
    assert 'could not be saved (ValueError)' in text and 'still not saved' in text
    assert 'access-1' not in text and 'RT2' not in text
    assert auth.flush() == 1 and 'conn' in auth.unsaved  # at quit: still refused, so still held
    auth.records.failing = False
    assert auth.access('conn') == 'access-1' and presented == ['RT1']  # saved now, still not refreshed again
    assert auth.flush() == 0
    assert auth.records.values['oauth:conn']['refresh_token'] == 'RT2' and auth.unsaved == {}
    auth.records.values['oauth:conn']['expires_at'] = 0
    assert auth.access('conn') == 'access-2' and presented == ['RT1', 'RT2']  # the next renewal presents RT2


# -- V1-158: clock leeway; V1-157: no network wait under ChatGPT.lock --------------------------------------------------

def test_an_id_token_issued_seconds_ahead_of_this_clock_is_accepted(provider):
    auth = ChatGPT(provider)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk['kid'] = 'fixture'
    auth.http_json = lambda *args: {'keys': [jwk]}
    now = int(time.time())
    claims = {'iss': ISSUER, 'aud': 'oaiapp_fixture', 'exp': now + 600, 'iat': now + 5, 'sub': 'account',
              'nonce': 'nonce'}

    def signed(changes):
        return jwt.encode({**claims, **changes}, private, algorithm='RS256', headers={'kid': 'fixture'})
    config = {'jwks_uri': ISSUER + '/.well-known/jwks.json'}
    assert auth.validate_identity(signed({}), 'oaiapp_fixture', 'nonce', config)['sub'] == 'account'
    assert auth.validate_identity(signed({'nbf': now + 5}), 'oaiapp_fixture', 'nonce', config)['sub'] == 'account'
    for changes in ({'iat': now + chatgpt.LEEWAY + 60}, {'nbf': now + chatgpt.LEEWAY + 60}, {'exp': now - 100},
                    {'iat': 'soon'}):
        with pytest.raises((ValueError, jwt.PyJWTError)):
            auth.validate_identity(signed(changes), 'oaiapp_fixture', 'nonce', config)


def test_sign_in_network_waits_never_hold_the_lock_access_needs(provider):
    auth = ChatGPT(provider)
    auth.records.set('oauth:conn', record('valid-access'))
    waiting, release, answers = threading.Event(), threading.Event(), []

    def slow(url, form=None):
        if url.endswith(DISCOVERY_PATH) or url.endswith('/oauth/token'):
            waiting.set()
            assert release.wait(10)
            return DISCOVERY if form is None else {**tokens('signed-in', 'RT9'), 'id_token': 'x'}
        raise AssertionError(url)
    auth.http_json = slow
    auth.validate_identity = lambda *args: {'sub': 'account', 'email': 'fixture@example.test'}
    provider.discover = lambda *args, **kwargs: [{'id': 'gpt-6-luna', 'context': 272000}]

    def measured():
        began = time.monotonic()
        assert auth.access('conn') == 'valid-access'
        return time.monotonic() - began

    starting = threading.Thread(target=lambda: answers.append(auth.start()))
    starting.start()
    assert waiting.wait(5)
    assert measured() < 0.1  # the discovery fetch is waiting, outside ChatGPT.lock
    release.set()
    starting.join(5)
    assert answers and answers[0]['state'] == 'waiting'
    pending, = [auth.pending]
    waiting.clear()
    release.clear()
    query = {'state': [pending['state']], 'client_id': ['oaiapp_fixture'], 'code': ['one-use-code']}
    calling = threading.Thread(target=lambda: answers.append(auth.callback(query)))
    calling.start()
    assert waiting.wait(5)
    assert measured() < 0.1 and auth.status() == {'state': 'validating'}  # the code exchange is waiting
    assert auth.callback(query)[0] == 400  # a second callback meanwhile is refused at once
    release.set()
    calling.join(5)
    assert answers[-1][0] == 200 and auth.status()['state'] == 'connected'
    assert any(p['kind'] == 'chatgpt' for p in provider.profiles())
