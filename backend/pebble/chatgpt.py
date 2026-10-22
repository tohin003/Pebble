"""OpenAI's public-client PKCE flow. Credentials belong only to Pebble.

Phase 24 (t24-auth; docs/PEBBLE_V1_INSPECTION.md V1-001, -008, -010, -070, -071, -157, -158):
- Every auth request names Pebble (User-Agent Pebble/<version>, Accept: application/json) and goes through the
  root's opener (Providers.open_url: no redirect, certifi trust, and the system proxy once after a connect failure).
- A failed request is a SignInError with a content-free `code` (offline, connect, timeout, tls, http_<status>,
  redirect, invalid_response, reconnect_required) and one plain message per class that ends "Your current sign-in is
  unchanged." (reconnect_required, which only a refresh or a refused model request raises, says how to reconnect).
  Each failed request logs one WARNING with only its method, endpoint path, code and exception class: never a body,
  an authorization code, a token or a query.
- The discovery document is validated and kept by this process: reused for DISCOVERY_SECONDS, then refetched, and
  the last good copy serves when a fetch fails. Discovery and JWKS GETs are retried twice (GET_RETRIES); a POST (the
  one-time code exchange, a refresh, a revocation) is sent once, so a code or a refresh token is never presented twice.
- A refresh that got no answer (no response, 408, 429 or 5xx) is a retryable ProviderFailure, so the model request
  asks again; invalid_grant and its kin end the sign-in (reconnect_required). A renewed record the vault could not
  save is kept in memory, used and saved on the next access, so its consumed predecessor is never presented again.
- start() and callback() make their network calls outside ChatGPT.lock, which access() needs.
"""
import base64
import hashlib
import hmac
import json
import logging
import math
import secrets
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlsplit, parse_qs
import jwt
from . import VERSION
from .recovery import CONNECT_WORDS, NETWORK, ProviderFailure, failure_class, failure_detail

ISSUER = 'https://auth.openai.com'
RESOURCE = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
TIMEOUT = 20                # seconds per socket operation of one auth request
DISCOVERY_SECONDS = 3600.0  # a validated discovery document is reused this long, then kept as the last good copy
GET_RETRIES = (1.0, 3.0)    # waits before the second and third try of a discovery or JWKS GET; POSTs are sent once
LEEWAY = 120                # seconds an id_token's iat or nbf may lie ahead of this Mac's clock
UNCHANGED = 'Your current sign-in is unchanged.'
RECONNECT = 'ChatGPT ended this sign-in (it expired or was revoked). Choose Continue with ChatGPT to sign in again.'
REUSED = ('ChatGPT ended this sign-in because it was renewed somewhere else (another copy of Pebble may share it). '
          'Choose Continue with ChatGPT to sign in again.')
# OAuth error codes that end a grant. Only these fixed words are ever kept from an error body; the body is not.
ENDED = frozenset({'invalid_grant', 'refresh_token_reused', 'refresh_token_expired', 'refresh_token_invalidated',
                   'token_revoked', 'invalid_client', 'unauthorized_client'})
MESSAGES = {
    'offline': 'Pebble could not reach OpenAI: this Mac seems to be offline (the sign-in address could not be '
               'looked up). Check the connection, then try again.',
    'connect': "Pebble could not connect to OpenAI's sign-in service: the connection was refused or dropped. If this "
               'Mac uses a VPN or a proxy, check it, then try again.',
    'timeout': "OpenAI's sign-in service did not answer in time. Try again in a moment.",
    'tls': "Pebble could not verify OpenAI's secure connection. A network filter, a proxy or this Mac's date and time "
           'may be interfering.',
    'redirect': "OpenAI's sign-in service answered with a redirect, which Pebble does not follow. Pebble may need an "
                'update.',
    'invalid_response': "OpenAI's sign-in service sent a reply Pebble could not read. Try again later.",
}
log = logging.getLogger(__name__)


def describe(code, status=None, grant=None):
    """The plain message for a failure class."""
    if code == 'reconnect_required':
        return REUSED if grant == 'refresh_token_reused' else RECONNECT
    if code in MESSAGES:
        text = MESSAGES[code]
        if code == 'redirect' and type(status) is int:
            text = text.replace('a redirect,', f'a redirect (HTTP {status}),')
    elif code == 'http_400' and grant:
        text = 'OpenAI did not accept this sign-in (it may have expired or been used already). Try again.'
    elif code == 'http_403':
        text = ("OpenAI's sign-in service refused the request (HTTP 403). Try again later; if it keeps happening, a "
                'network filter may be blocking Pebble.')
    elif code == 'http_408':
        text = "OpenAI's sign-in service did not answer in time (HTTP 408). Try again in a moment."
    elif code == 'http_429':
        text = "OpenAI's sign-in service is busy (HTTP 429). Wait a minute, then try again."
    elif code.startswith('http_5'):
        text = f"OpenAI's sign-in service is having a problem (HTTP {code[5:]}). Try again in a few minutes."
    else:
        text = (f"OpenAI's sign-in service refused the request (HTTP {code[5:]}). Try again; if it keeps happening, "
                'Pebble may need an update.')
    return text + ' ' + UNCHANGED


class SignInError(ValueError):
    """A ChatGPT sign-in request that failed: `code` is its content-free class, the message its plain words."""

    def __init__(self, code, message=None, *, status=None, grant=None):
        super().__init__(message or describe(code, status, grant))
        self.code, self.status, self.grant = code, status, grant


def _again(code):
    """A class worth asking again: no HTTP response, or 408, 429 and 5xx."""
    return code in NETWORK or code in ('http_408', 'http_429') or code.startswith('http_5')


def _grant_error(body):
    """The OAuth error code of an error body, when it is one of ENDED (nothing else of the body is kept)."""
    if not isinstance(body, dict):
        return None
    error = body.get('error')
    for value in (error, error.get('code') if isinstance(error, dict) else None, body.get('code')):
        if isinstance(value, str) and value in ENDED:
            return value
    return None


def _http_error(exc):
    status, body = exc.code, None
    try:
        if 400 <= status < 500:
            body = json.loads(exc.read(8192))
    except (ValueError, OSError):
        body = None
    finally:
        try:
            exc.close()
        except Exception:
            pass
    code = 'redirect' if 300 <= status < 400 else f'http_{status}'
    return SignInError(code, status=status, grant=_grant_error(body))


def _renewal_failure(error):
    """A refresh's SignInError as the model request sees it: retryable when nothing answered, the end of the sign-in
    for invalid_grant and its kin (or HTTP 401), else the error itself."""
    if error.code == 'reconnect_required':
        return error
    if error.code == 'http_401' or (error.code == 'http_400' and error.grant):
        return SignInError('reconnect_required', status=error.status, grant=error.grant)
    if _again(error.code):
        words = CONNECT_WORDS.get(error.code) or f"OpenAI's sign-in service answered HTTP {error.status}"
        return ProviderFailure(f'Could not connect to the model provider (ChatGPT) to renew the sign-in: {words}. '
                               'Your sign-in is unchanged.', retryable=True, code=error.code,
                               network=error.code in NETWORK)
    return error


def _close(server):
    if server:
        server.shutdown()
        server.server_close()


class SecretRecords:
    """Chunk OS-vault records for Windows' credential size limit; publish last.

    Phase 24 (V1-009): a vault that offers set_many (the vault helper's one item) writes a whole record, or deletes
    one, in a single item update; any other vault is written chunk by chunk as before."""
    def __init__(self, vault):
        self.vault = vault

    def _batch(self, changes):
        batch = getattr(self.vault, 'set_many', None)
        return callable(batch) and batch(changes) is True

    def get(self, name):
        manifest = self.vault.get(name + ':manifest')
        if not manifest:
            raise ValueError('Continue with ChatGPT to reconnect this account.')
        info = json.loads(manifest)
        return json.loads(''.join(self.vault.get(f'{name}:{info["generation"]}:{i}') for i in range(info['count'])))

    def set(self, name, value):
        data = json.dumps(value, ensure_ascii=True)
        if len(data) > 60000:
            raise ValueError('OAuth credential record exceeds its size limit.')
        old = self.vault.get(name + ':manifest')
        generation = uuid.uuid4().hex
        parts = [data[i:i+1800] for i in range(0, len(data), 1800)]
        changes = {f'{name}:{generation}:{i}': part for i, part in enumerate(parts)}
        changes[name + ':manifest'] = json.dumps({'generation': generation, 'count': len(parts)})
        if old:
            info = json.loads(old)
            changes.update({f'{name}:{info["generation"]}:{i}': None for i in range(info['count'])})
        if self._batch(changes):
            return
        written = []
        try:
            for i, part in enumerate(parts):
                key = f'{name}:{generation}:{i}'
                self.vault.set(key, part)
                written.append(key)
            self.vault.set(name + ':manifest', json.dumps({'generation': generation, 'count': len(parts)}))
        except Exception:
            for key in written:
                self.vault.delete(key)
            raise
        if old:
            info = json.loads(old)
            for i in range(info['count']):
                self.vault.delete(f'{name}:{info["generation"]}:{i}')

    def delete(self, name):
        old = self.vault.get(name + ':manifest')
        if old:
            info = json.loads(old)
            changes = {name + ':manifest': None}
            changes.update({f'{name}:{info["generation"]}:{i}': None for i in range(info['count'])})
            if self._batch(changes):
                return
            self.vault.delete(name + ':manifest')
            for i in range(info['count']):
                self.vault.delete(f'{name}:{info["generation"]}:{i}')


class ChatGPT:
    def __init__(self, providers):
        self.providers = providers
        self.store = providers.store
        self.records = SecretRecords(providers.vault)
        self.pending = None
        self.result = {'state': 'idle'}
        self.lock = threading.RLock()
        self.server = None
        self.can_activate = lambda: True
        # Phase 24: the validated discovery document (fetched at, document), its own lock (never ChatGPT.lock), the
        # renewed records the vault has not saved yet, and the clock and sleep the retries use (seams for tests).
        self.discovery, self.discovery_lock = None, threading.Lock()
        self.unsaved = {}
        self.clock, self.sleep = time.monotonic, time.sleep

    def http_json(self, url, form=None):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'auth.openai.com' or parsed.username or parsed.query:
            raise ValueError('Invalid OpenAI authentication endpoint.')
        headers = {'Accept': 'application/json', 'User-Agent': 'Pebble/' + VERSION}
        if form is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        req = urllib.request.Request(url, data=urlencode(form).encode() if form is not None else None, headers=headers)
        opened = getattr(self.providers, 'open_url', None)
        try:
            response = opened(req, TIMEOUT) if callable(opened) else self.providers.http.open(req, timeout=TIMEOUT)
            with response as res:
                raw = res.read(200001)
        except urllib.error.HTTPError as exc:
            error, detail = _http_error(exc), f'HTTP {exc.code}'
        except Exception as exc:
            error, detail = SignInError(failure_class(exc) or 'invalid_response'), failure_detail(exc)
        else:
            value = None
            if len(raw) <= 200000:
                try:
                    value = json.loads(raw) if raw else {}
                except ValueError:
                    value = None
            if isinstance(value, dict):
                return value
            error, detail = SignInError('invalid_response'), 'too large' if len(raw) > 200000 else 'not a JSON object'
        # One content-free line per failed request (V1-001/V1-010): the method, the fixed endpoint path and the class.
        log.warning('ChatGPT sign-in request failed: %s %s -> %s (%s).', 'GET' if form is None else 'POST',
                    parsed.path[:120], error.code, detail)
        raise error

    def _get(self, url):
        """A GET (discovery, JWKS), tried again after GET_RETRIES when nothing answered or the service was busy."""
        for wait in (*GET_RETRIES, None):
            try:
                return self.http_json(url)
            except SignInError as exc:
                if wait is None or not _again(exc.code):
                    raise
            self.sleep(wait)

    def config(self):
        with self.discovery_lock:
            kept = self.discovery
            if kept is not None and self.clock() - kept[0] < DISCOVERY_SECONDS:
                return kept[1]
            try:
                c = self._get(ISSUER + '/.well-known/openid-configuration')
            except SignInError as exc:
                if kept is None:
                    raise
                log.warning('ChatGPT sign-in uses the last good discovery document (%s).', exc.code)
                return kept[1]
            if c.get('issuer') != ISSUER:
                raise ValueError('OpenAI identity issuer did not match.')
            for key in ('authorization_endpoint', 'token_endpoint', 'jwks_uri'):
                p = urlsplit(c.get(key, ''))
                if p.scheme != 'https' or p.hostname != 'auth.openai.com' or p.username or p.query:
                    raise ValueError('OpenAI identity configuration did not match.')
            self.discovery = (self.clock(), c)
            return c

    def prefetch(self):
        """The validated discovery document, fetched (or reused) with no lock held. App.dispatch calls this before
        task_lock for chatgpt_start, so no sign-in network wait holds task_lock or ChatGPT.lock. A failure cancels a
        sign-in in progress, as start() always has, and raises its SignInError."""
        try:
            return self.config()
        except Exception:
            with self.lock:
                self.cancel()
            raise

    def start(self, profile_id=None):
        config = self.prefetch()  # outside ChatGPT.lock (V1-157): a cached copy when the parent prefetched it
        with self.lock:
            self.cancel()
            profiles = self.providers.profiles()
            old = next((p for p in profiles if p['id'] == profile_id and p['kind'] == 'chatgpt'), None)
            if profile_id and not old:
                raise ValueError('ChatGPT account not found.')
            host = self.store.setting('chatgpt_host_id') or 'urn:uuid:' + str(uuid.uuid4())
            self.store.set_setting('chatgpt_host_id', host)
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            client = old['client_id'] if old else 'dynamic_agent_client'
            owner = self
            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass  # Callback codes, state and query strings are never logged.
                def do_GET(self):
                    if len(self.path) > 8192 or urlsplit(self.path).path != '/auth/callback':
                        self.send_error(404)
                        return
                    q = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    status, message = owner.callback(q)
                    self.send_response(status)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Security-Policy', "default-src 'none'")
                    self.end_headers()
                    self.wfile.write(message.encode())
            self.server = ThreadingHTTPServer(('127.0.0.1', 0), Callback)
            self.server.daemon_threads = True
            redirect = f'http://127.0.0.1:{self.server.server_port}/auth/callback'
            self.pending = {'state': state, 'nonce': nonce, 'verifier': verifier, 'redirect': redirect,
                            'client_id': client, 'old': old, 'config': config, 'deadline': time.time()+300}
            self.result = {'state': 'waiting'}
            threading.Thread(target=self.server.serve_forever, daemon=True).start()
            timer = threading.Timer(300, self.expire, args=(state,))
            timer.daemon = True
            timer.start()
            query = {'client_id': client, 'ext_agent_host_id': host, 'response_type': 'code',
                     'redirect_uri': redirect, 'scope': SCOPES, 'resource': RESOURCE, 'state': state,
                     'nonce': nonce, 'code_challenge_method': 'S256',
                     'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')}
            if not old:
                query['agent_name_hint'] = 'Pebble'
            return {'url': config['authorization_endpoint'] + '?' + urlencode(query), 'state': 'waiting'}

    def expire(self, state):
        with self.lock:
            if self.pending and self.pending['state'] == state:
                self.cancel()
                self.result = {'state': 'failed', 'error': 'Sign-in expired. Try again.'}

    def callback(self, q):
        with self.lock:
            p = self.pending
            if not p or time.time() > p['deadline'] or any(len(v) != 1 for v in q.values()) or not hmac.compare_digest(q.get('state', [''])[0], p['state']):
                return 400, 'Invalid or expired sign-in callback.'
            if self.result['state'] != 'waiting':
                return 400, 'This sign-in has already been handled.'
            self.result = {'state': 'validating'}
        try:
            # The network calls run outside ChatGPT.lock (V1-157): the code exchange (sent once: a one-time code is
            # never re-posted), the identity keys and the account's model catalogue. 'validating' refuses a second
            # callback meanwhile, and only the flow that is still current may write.
            if q.get('error'):
                raise ValueError('ChatGPT sign-in was declined. Your previous account is unchanged.')
            client = q.get('client_id', [p['client_id']])[0]
            if p['old'] and client != p['client_id']:
                raise ValueError('ChatGPT registration changed unexpectedly.')
            if not client.startswith('oaiapp_') or len(client) > 200 or not q.get('code'):
                raise ValueError('ChatGPT registration did not return an issued client ID and code.')
            tokens = self.http_json(p['config']['token_endpoint'], {'grant_type': 'authorization_code', 'client_id': client,
                        'code': q['code'][0], 'code_verifier': p['verifier'], 'redirect_uri': p['redirect'], 'resource': RESOURCE})
            claims = self.validate_identity(tokens.get('id_token', ''), client, p['nonce'], p['config'])
            if self.pending is not p:
                raise ValueError('Sign-in was cancelled.')
            if p['old'] and claims['sub'] != p['old']['subject']:
                raise ValueError('The signed-in ChatGPT identity differs from the selected account.')
            scopes = self.validate_tokens(tokens)
            # Select from this account's actual eligible catalogue. Account
            # identity does not imply access to a hard-coded model.
            models = self.providers.discover(RESOURCE, 'chatgpt', tokens['access_token'])
            preferred = p['old']['model'] if p['old'] else 'gpt-6-luna'
            selected = next((m for m in models if m['id'] == preferred), models[0])
            pid = p['old'].get('connection_id',p['old']['id']) if p['old'] else uuid.uuid4().hex
            record = {**tokens, 'client_id': client, 'expires_at': time.time()+tokens['expires_in'], 'scopes': scopes}
            with self.lock:
                if self.pending is not p:
                    raise ValueError('Sign-in was cancelled.')
                self.records.set('oauth:' + pid, record)
                self.unsaved.pop(pid, None)  # this sign-in replaces any renewed record still waiting to be saved
                email = str(claims.get('email', 'ChatGPT account'))[:120]
                profile = {'id': pid, 'name': email + ' · ChatGPT', 'kind': 'chatgpt', 'url': RESOURCE,
                           'model': selected['id'], 'context': p['old']['context'] if p['old'] and selected['id'] == preferred else selected.get('context') or 32768,
                           'budget': selected.get('context') or 32768, 'tools': True, 'has_key': False, 'client_id': client, 'subject': claims['sub'], 'email': email, 'context_version': 1,'budget_version':2,'context_mode':'full','connection_id':pid,'provider_name':email+' · ChatGPT',
                           **({'effort':p['old'].get('effort','default')} if p['old'] else {})}
                profile['budget'] = min(profile['context'], profile['budget'])
                existing=[x for x in self.providers.profiles() if x.get('connection_id',x['id'])==pid]
                connected=existing if existing else [profile]
                for item in connected:
                    item.update(client_id=client,subject=claims['sub'],email=email,connection_id=pid)
                    meta=next((m for m in models if m['id']==item['model']),None)
                    if meta:
                        for key in ['context','context_source','advertised_max_context','extended_context_available','reasoning_capabilities','max_output_tokens','input_modalities']:
                            if key in meta:item[key]=meta[key]
                    if item.get('context_mode')=='full':item['budget']=item['context']
                self.store.set_setting('model_profiles', [x for x in self.providers.profiles() if x.get('connection_id',x['id']) != pid] + connected)
                selected_id=p['old']['id'] if p['old'] else pid
                if self.can_activate():
                    self.providers.select(selected_id)
                self.result = {'state': 'connected', 'profile_id': selected_id, 'email': email}
                self.pending = None
                server, self.server = self.server, None
            threading.Thread(target=_close, args=(server,), daemon=True).start()
            return 200, 'Pebble is connected to your ChatGPT account. Return to Pebble to select an available model.'
        except Exception as exc:
            # Never expose token endpoint bodies, signed tokens or callback values.
            error = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, jwt.PyJWTError) else 'ChatGPT identity verification failed.'
            with self.lock:
                server = None
                if self.pending is p:  # a newer sign-in or a cancel owns the state otherwise
                    self.result = {'state': 'failed', 'error': error[:250]}
                    self.pending = None
                    server, self.server = self.server, None
            threading.Thread(target=_close, args=(server,), daemon=True).start()
            return 400, 'Sign-in could not be completed. Return to Pebble for details.'

    def validate_identity(self, token, client, nonce, config):
        jwks = self._get(config['jwks_uri'])
        header = jwt.get_unverified_header(token)
        if header.get('alg') != 'RS256':
            raise ValueError('Unsupported identity signature algorithm.')
        key = next((k for k in jwks.get('keys', []) if k.get('kid') == header.get('kid') and k.get('kty') == 'RSA'), None)
        if not key:
            raise ValueError('OpenAI identity signing key was not found.')
        # Phase 24 (V1-158): a Mac clock behind OpenAI's by up to LEEWAY seconds still accepts a fresh token (iat, nbf);
        # exp keeps no leeway.
        claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=['RS256'], audience=client, issuer=ISSUER,
                            options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'nonce'],
                                     'verify_iat': False, 'verify_nbf': False})
        now = time.time()
        for name in ('iat', 'nbf'):
            value = claims.get(name)
            if name == 'nbf' and value is None:
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value > now + LEEWAY:
                raise jwt.ImmatureSignatureError(f'The token is not yet valid ({name}).')
        if not hmac.compare_digest(str(claims['nonce']), nonce) or not isinstance(claims['sub'], str) or not claims['sub']:
            raise ValueError('ChatGPT identity nonce did not match.')
        return claims

    @staticmethod
    def validate_tokens(tokens):
        scopes = str(tokens.get('scope', '')).split()
        if 'chatgpt.tokens.use.direct' not in scopes or 'resource.invoke' not in scopes:
            raise ValueError('ChatGPT plan usage was not authorized. Reconnect and allow plan usage.')
        if tokens.get('token_type', '').lower() != 'bearer' or not tokens.get('access_token') or type(tokens.get('expires_in')) is not int or not 0 < tokens['expires_in'] <= 86400:
            raise ValueError('OpenAI returned an invalid credential response.')
        return scopes

    def _record(self, pid):
        """The connection's record: a renewed one still waiting in memory first (saved now when the vault takes it),
        else the vault's."""
        record = self.unsaved.get(pid)
        if record is None:
            return self.records.get('oauth:' + pid)
        try:
            self.records.set('oauth:' + pid, record)
        except Exception as exc:
            log.warning('The renewed ChatGPT sign-in is still not saved (%s); Pebble keeps using it from memory.',
                        type(exc).__name__)
        else:
            self.unsaved.pop(pid, None)
        return record

    def _keep(self, pid, record):
        """Save a renewed record; when the vault refuses, keep it in memory (V1-071): its predecessor's refresh token
        was consumed, so presenting that one again would end the sign-in."""
        try:
            self.records.set('oauth:' + pid, record)
        except Exception as exc:
            self.unsaved[pid] = record
            log.warning('The renewed ChatGPT sign-in could not be saved (%s); Pebble keeps it in memory and saves it '
                        'on the next use.', type(exc).__name__)
        else:
            self.unsaved.pop(pid, None)

    def access(self, pid, rejected=None):
        """The connection's access token, refreshed within 60 s of expiry. `rejected` is a token the model route
        refused (HTTP 401): it is refreshed once, unless another request already replaced it (V1-070)."""
        with self.lock:
            record = self._record(pid)
            if record['expires_at'] > time.time()+60 and (rejected is None or record.get('access_token') != rejected):
                return record['access_token']
            if not record.get('refresh_token'):
                raise SignInError('reconnect_required', 'Continue with ChatGPT to renew this account.')
            try:
                c = self.config()
                tokens = self.http_json(c['token_endpoint'], {'grant_type': 'refresh_token', 'client_id': record['client_id'],
                                        'refresh_token': record['refresh_token'], 'resource': RESOURCE})
            except SignInError as exc:
                raise _renewal_failure(exc) from None
            scopes = self.validate_tokens(tokens)
            if not tokens.get('refresh_token'):
                raise ValueError('ChatGPT refresh did not rotate its credential. Reconnect.')
            record = {**record, **tokens, 'expires_at': time.time()+tokens['expires_in'], 'scopes': scopes}
            self._keep(pid, record)
            return record['access_token']

    def sign_out(self, pid):
        with self.lock:
            confirmed = False
            try:
                r = self.unsaved.pop(pid, None) or self.records.get('oauth:' + pid)
                config = self.config()
                if r.get('refresh_token') and config.get('revocation_endpoint'):
                    self.http_json(config['revocation_endpoint'], {'token': r['refresh_token'], 'token_type_hint': 'refresh_token', 'client_id': r['client_id']})
                    confirmed = True
            except ValueError:
                pass
            self.records.delete('oauth:' + pid)
            return {'revoked': confirmed, 'message': 'Signed out.' if confirmed else 'Signed out locally. Remote revocation was not confirmed; disconnect Pebble in ChatGPT Settings.'}

    def flush(self):
        """Save every renewed record still held in memory (best effort, for App.close); the count still unsaved."""
        with self.lock:
            for pid in list(self.unsaved):
                self._record(pid)
            return len(self.unsaved)

    def stop_server(self):
        server, self.server = self.server, None
        _close(server)

    def cancel(self):
        self.pending = None
        self.result = {'state': 'idle'}
        self.stop_server()

    def status(self):
        return dict(self.result)
