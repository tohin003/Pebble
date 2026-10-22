"""Bounded, unauthenticated OAuth metadata and reviewed public-client registration.

The injected request(packet) owns DNS pinning, TLS, deadlines, cancellation,
redirect/proxy denial and response framing. It MUST NOT attach any credential.
This module owns no browser, callback, approval UI, vault or durable effect log.
"""
import copy
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit, urlunsplit

from .policy import PolicyError


MAX_DOCUMENT_BYTES = 64_000
MAX_CHALLENGE_BYTES = 8_192
MAX_URL_BYTES = 2_000
MAX_SCOPES = 32
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_TOKEN68 = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
_SCOPE = re.compile(r'[\x21\x23-\x5b\x5d-\x7e]{1,160}')


class OAuthMetadataError(PolicyError):
    """Safe local diagnosis; never embeds remote bodies or exception details."""


class MetadataUnavailable(OAuthMetadataError):
    """A read-only discovery candidate is unavailable; a fallback may be tried."""


class RequestNotDispatched(OAuthMetadataError):
    """Trusted callback guarantees admission stopped before the HTTP effect."""


class RegistrationRejected(OAuthMetadataError):
    outcome = 'rejected'


class RegistrationUnknown(OAuthMetadataError):
    """The registration may exist. Never automatically repeat its POST."""
    outcome = 'unknown'


@dataclass(frozen=True)
class AuthenticationChallenge:
    scheme: str
    parameters: dict
    token68: str | None = None


@dataclass(frozen=True)
class RegistrationResult:
    """Only public is suitable for IPC/receipts; private is vault input only."""
    public: dict
    private: dict = field(repr=False)


def _packed(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_packed(value).encode()).hexdigest()


def _text(value, limit=160):
    if (not isinstance(value, str) or not 1 <= len(value) <= limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise OAuthMetadataError('OAuth metadata contains invalid bounded text.')
    return value


def parse_www_authenticate(headers):
    """Parse multiple RFC 9110 challenges without splitting quoted commas.

    Input is a string or a list of individual field values. Duplicate parameters
    are rejected case-insensitively. Selection rejects multiple Bearer challenges
    rather than choosing whichever attacker-controlled challenge comes first.
    """
    if headers is None or headers == '' or headers == []:
        return ()
    if isinstance(headers, str):
        headers = [headers]
    if (not isinstance(headers, (list, tuple)) or not 1 <= len(headers) <= 8
            or any(not isinstance(h, str) or not h for h in headers)
            or any(any(ord(c) > 126 or ord(c) < 32 and c != '\t' for c in h) for h in headers)
            or sum(len(h) for h in headers) > MAX_CHALLENGE_BYTES
            or any(not h.strip(' \t') for h in headers)):
        raise OAuthMetadataError('Invalid or excessive WWW-Authenticate challenges.')
    result = []
    for header in headers:
        pos, size = 0, len(header)

        def ows():
            nonlocal pos
            while pos < size and header[pos] in ' \t':
                pos += 1

        def token():
            nonlocal pos
            match = _TOKEN.match(header, pos)
            if not match:
                raise OAuthMetadataError('Malformed WWW-Authenticate challenge.')
            pos = match.end()
            return match.group()

        ows()
        while pos < size:
            scheme = token().lower()
            parameters, encoded = {}, None
            if pos < size and header[pos] not in ' \t,':
                raise OAuthMetadataError('Malformed WWW-Authenticate challenge.')
            if pos < size and header[pos] in ' \t':
                ows()
                # token68 has only trailing '='; auth-param values cannot be empty.
                end = header.find(',', pos)
                end = size if end == -1 else end
                candidate = header[pos:end].rstrip(' \t')
                if candidate and _TOKEN68.fullmatch(candidate):
                    encoded = candidate
                    pos = end
                elif pos < size and header[pos] != ',':
                    while True:
                        name = token().lower()
                        if len(parameters) >= 16 or name in parameters:
                            raise OAuthMetadataError('Duplicate or excessive challenge parameters.')
                        ows()
                        if pos >= size or header[pos] != '=':
                            raise OAuthMetadataError('Malformed WWW-Authenticate parameter.')
                        pos += 1
                        ows()
                        if pos < size and header[pos] == '"':
                            pos += 1
                            value = []
                            while pos < size and header[pos] != '"':
                                if header[pos] == '\\':
                                    pos += 1
                                    if pos >= size:
                                        raise OAuthMetadataError('Malformed quoted challenge parameter.')
                                value.append(header[pos])
                                pos += 1
                            if pos >= size:
                                raise OAuthMetadataError('Unterminated quoted challenge parameter.')
                            pos += 1
                            value = ''.join(value)
                        else:
                            value = token()
                        if len(value) > 2_048:
                            raise OAuthMetadataError('Challenge parameter exceeded its limit.')
                        parameters[name] = value
                        ows()
                        if pos == size:
                            break
                        if header[pos] != ',':
                            raise OAuthMetadataError('Malformed WWW-Authenticate separator.')
                        next_pos = pos + 1
                        while next_pos < size and header[next_pos] in ' \t':
                            next_pos += 1
                        match = _TOKEN.match(header, next_pos)
                        if not match:
                            raise OAuthMetadataError('Malformed WWW-Authenticate separator.')
                        check = match.end()
                        while check < size and header[check] in ' \t':
                            check += 1
                        if check == size or header[check] != '=':
                            break
                        pos = next_pos
            result.append(AuthenticationChallenge(scheme, parameters, encoded))
            if len(result) > 8:
                raise OAuthMetadataError('Too many authentication challenges.')
            ows()
            if pos < size:
                if header[pos] != ',':
                    raise OAuthMetadataError('Malformed WWW-Authenticate separator.')
                pos += 1
                ows()
                if pos == size:
                    raise OAuthMetadataError('Trailing WWW-Authenticate separator.')
    return tuple(result)


def bearer_challenge(headers):
    matches = [c for c in parse_www_authenticate(headers) if c.scheme == 'bearer']
    if len(matches) > 1 or matches and matches[0].token68 is not None:
        raise OAuthMetadataError('Ambiguous or invalid Bearer metadata challenge.')
    return matches[0] if matches else None


def _origin(parsed):
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


def _loopback(host):
    return host in {'127.0.0.1', '::1', 'localhost'}


def validate_url(url, *, resource=None, https_only=False):
    """Lexical same-loopback/public policy. The HTTP callback must pin DNS too.

    Resource/issuer/metadata URLs are deliberately query-free for compatibility
    with the owned connector transport. URL normalization never changes identity.
    """
    try:
        if (not isinstance(url, str) or not 1 <= len(url.encode('utf-8')) <= MAX_URL_BYTES
                or any(ord(c) <= 32 or ord(c) > 126 for c in url) or '\\' in url):
            raise ValueError()
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
        if (not host or parsed.scheme not in {'http', 'https'} or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or '?' in url or '#' in url or port == 0 or '%' in parsed.netloc):
            raise ValueError()
        if https_only and parsed.scheme != 'https':
            raise ValueError()
        local = _loopback(host)
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if not local:
            if parsed.scheme != 'https' or address is not None and not address.is_global:
                raise ValueError()
            if address is None:
                # DNS classification remains in the checked-address transport.
                labels = host.split('.')
                if (len(labels) < 2 or host.endswith(('.localhost', '.local', '.internal', '.home', '.lan'))
                        or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
                    raise ValueError()
                # Reject ambiguous legacy numeric host forms interpreted as IPs.
                if all(re.fullmatch(r'(?:[0-9]+|0x[0-9a-f]+)', label) for label in labels):
                    raise ValueError()
        elif resource is not None:
            anchor = validate_url(resource)
            if _origin(parsed) != _origin(anchor) or not _loopback(anchor.hostname):
                raise ValueError()
        if parsed.scheme == 'http' and not local:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise OAuthMetadataError('OAuth destination must be public HTTPS or the exact reviewed loopback origin.') from None


def validate_resolved_addresses(url, addresses, *, resource=None):
    """Transport utility: reject mixed public/private DNS and rebinding targets.

    The transport must connect to one of these exact addresses without resolving
    again, validate TLS against the URL host, and deny proxies/redirects.
    """
    parsed = validate_url(url, resource=resource)
    try:
        if not isinstance(addresses, (list, tuple, set)) or not 1 <= len(addresses) <= 32:
            raise ValueError()
        checked = {ipaddress.ip_address(a) for a in addresses}
        if any(not (a.is_loopback if _loopback(parsed.hostname) else a.is_global) for a in checked):
            raise ValueError()
        return tuple(sorted(str(a) for a in checked))
    except (TypeError, ValueError):
        raise OAuthMetadataError('OAuth destination resolved outside its public/loopback boundary.') from None


def _duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OAuthMetadataError('Duplicate JSON metadata fields.')
        result[key] = value
    return result


def metadata_document(value):
    """Validate bounded JSON; raw inputs preserve duplicate-field detection."""
    try:
        if isinstance(value, (str, bytes)):
            raw = value.encode('utf-8') if isinstance(value, str) else value
            if len(raw) > MAX_DOCUMENT_BYTES:
                raise ValueError()
            value = json.loads(raw, object_pairs_hook=_duplicates,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict) or len(_packed(value).encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError()
        pending = [(value, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > 24 or count > 4_096:
                raise ValueError()
            if isinstance(item, dict):
                if any(not isinstance(k, str) or len(k) > 256 for k in item):
                    raise ValueError()
                pending.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                pending.extend((v, depth + 1) for v in item)
            elif item is not None and type(item) not in {str, bool, int, float}:
                raise ValueError()
        return copy.deepcopy(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise OAuthMetadataError('OAuth metadata is invalid or exceeded its JSON boundary.') from None


def _scopes(value, *, allow_empty=True):
    if (not isinstance(value, list) or not (0 if allow_empty else 1) <= len(value) <= MAX_SCOPES
            or any(not isinstance(s, str) or not _SCOPE.fullmatch(s) for s in value)
            or len(set(value)) != len(value)):
        raise OAuthMetadataError('OAuth scopes must be bounded and distinct.')
    return sorted(value)


def _scope_string(value):
    if not isinstance(value, str) or len(value) > 4_096 or not value or value.strip() != value or '  ' in value:
        raise OAuthMetadataError('Invalid OAuth scope challenge.')
    return _scopes(value.split(' '), allow_empty=False)


def _strings(value, *, maximum=32, length=200):
    if (not isinstance(value, list) or len(value) > maximum
            or any(not isinstance(v, str) or not 1 <= len(v) <= length
                   or any(ord(c) < 32 or ord(c) > 126 for c in v) for v in value)
            or len(set(value)) != len(value)):
        raise OAuthMetadataError('Invalid OAuth capability list.')
    return list(value)


def resource_metadata_urls(resource):
    parsed = validate_url(resource)
    path = parsed.path.rstrip('/')
    values = [urlunsplit((parsed.scheme, parsed.netloc, '/.well-known/oauth-protected-resource' + path, '', '')),
              urlunsplit((parsed.scheme, parsed.netloc, '/.well-known/oauth-protected-resource', '', ''))]
    return tuple(dict.fromkeys(values))


def issuer_metadata_urls(issuer, *, resource=None):
    parsed = validate_url(issuer, resource=resource)
    path = parsed.path.rstrip('/')
    values = [urlunsplit((parsed.scheme, parsed.netloc, '/.well-known/oauth-authorization-server' + path, '', '')),
              urlunsplit((parsed.scheme, parsed.netloc, '/.well-known/openid-configuration' + path, '', ''))]
    if path:
        values.append(urlunsplit((parsed.scheme, parsed.netloc, path + '/.well-known/openid-configuration', '', '')))
    return tuple(dict.fromkeys(values))


def _redirect(url):
    parsed = validate_url(url)
    # The caller, not this validator, must own the callback socket.
    if parsed.scheme == 'http' and (parsed.hostname not in {'127.0.0.1', '::1'} or parsed.port is None):
        raise OAuthMetadataError('Native registration requires an owned literal loopback callback with an explicit port.')
    if not parsed.path or parsed.path == '/':
        raise OAuthMetadataError('Registration callback requires an explicit path.')
    return url


def validate_client_id_metadata(client_id, document=None, *, redirect_uri=None):
    """Validate a supplied HTTPS reference and optionally its supplied document.

    This does not publish or fetch the document and cannot verify public hosting.
    The authorization server remains responsible for resolving this reference.
    """
    parsed = validate_url(client_id, https_only=True)
    if _loopback(parsed.hostname) or not parsed.path or parsed.path == '/':
        raise OAuthMetadataError('Client ID metadata requires a public HTTPS document URL with a path.')
    if any(unquote(segment) in {'.', '..'} for segment in parsed.path.split('/')):
        raise OAuthMetadataError('Client ID metadata URLs must not contain dot path segments.')
    result = {'client_id': client_id, 'document_url': client_id,
              'document_supplied': document is not None, 'hosting_verified': False}
    if document is None:
        if redirect_uri is not None:
            _redirect(redirect_uri)
        return result
    value = metadata_document(document)
    if value.get('client_id') != client_id:
        raise OAuthMetadataError('Client ID metadata must match its exact supplied document URL.')
    name = _text(value.get('client_name'))
    redirects = _strings(value.get('redirect_uris'), maximum=4, length=MAX_URL_BYTES)
    if not redirects:
        raise OAuthMetadataError('Client ID metadata needs at least one redirect URI.')
    for redirect in redirects:
        _redirect(redirect)
    if redirect_uri is not None and _redirect(redirect_uri) not in redirects:
        raise OAuthMetadataError('Owned callback is absent from client ID metadata.')
    if (value.get('token_endpoint_auth_method', 'none') != 'none'
            or any(k in value for k in ('client_secret', 'client_secret_expires_at', 'jwks', 'jwks_uri',
                                        'registration_access_token', 'access_token', 'refresh_token', 'id_token'))):
        raise OAuthMetadataError('Only public client ID metadata without private authentication is supported.')
    grant_types = _strings(value.get('grant_types', ['authorization_code']), maximum=2)
    if 'authorization_code' not in grant_types or set(grant_types) - {'authorization_code', 'refresh_token'}:
        raise OAuthMetadataError('Client ID metadata requires the public authorization-code flow.')
    response_types = _strings(value.get('response_types', ['code']), maximum=1)
    if response_types != ['code']:
        raise OAuthMetadataError('Client ID metadata requires the code response type.')
    if value.get('application_type', 'native') != 'native':
        raise OAuthMetadataError('This native client requires native client ID metadata.')
    metadata = {'client_id': client_id, 'client_name': name, 'redirect_uris': redirects,
                'token_endpoint_auth_method': 'none', 'grant_types': grant_types,
                'response_types': response_types, 'application_type': 'native'}
    if 'scope' in value:
        metadata['scope'] = ' '.join(_scope_string(value['scope']))
    for key in ('client_uri', 'logo_uri', 'tos_uri', 'policy_uri'):
        if key in value:
            validate_url(value[key], https_only=True)
            if _loopback(urlsplit(value[key]).hostname):
                raise OAuthMetadataError('Public client metadata references must be public HTTPS.')
            metadata[key] = value[key]
    return {**result, 'client_metadata': metadata}


class OAuthMetadata:
    """Read-only discovery and bounded, single-use reviewed registration plans."""
    def __init__(self, request, *, clock=time.monotonic, plan_ttl=300, max_plans=4):
        if (not callable(request) or type(plan_ttl) is not int or type(max_plans) is not int
                or not 1 <= plan_ttl <= 300 or not 1 <= max_plans <= 4):
            raise ValueError('Invalid bounded OAuth metadata helper configuration.')
        self.request, self.clock = request, clock
        self.plan_ttl, self.max_plans = plan_ttl, max_plans
        self._lock = threading.RLock()
        self._plans = {}

    def _get(self, url, resource):
        validate_url(url, resource=resource)
        packet = {'url': url, 'method': 'GET', 'body': None,
                  'headers': {'Accept': 'application/json'}, 'purpose': 'oauth-json'}
        try:
            reply = self.request(packet)
        except Exception:
            raise MetadataUnavailable('OAuth metadata request is unavailable.') from None
        if not isinstance(reply, dict):
            raise OAuthMetadataError('Invalid OAuth transport envelope.')
        if reply.get('transport_error'):
            if reply['transport_error'] in {'OAuthMetadataError', 'ValueError', 'JSONDecodeError', 'UnicodeDecodeError', 'PolicyError'}:
                raise OAuthMetadataError('OAuth metadata or its destination failed validation.')
            raise MetadataUnavailable('OAuth metadata request is unavailable.')
        status = reply.get('http_error', reply.get('status', 200))
        if type(status) is not int:
            raise OAuthMetadataError('Invalid OAuth metadata HTTP status.')
        if status in {404, 410}:
            raise MetadataUnavailable('OAuth metadata candidate was not found.')
        if status != 200:
            raise OAuthMetadataError('OAuth metadata HTTP response was rejected.')
        return metadata_document(reply.get('document', reply.get('raw_document')))

    def discover_resource(self, resource, www_authenticate=None):
        validate_url(resource)
        challenge = bearer_challenge(www_authenticate)
        challenged = None
        advertised = challenge.parameters.get('resource_metadata') if challenge else None
        if challenge and 'scope' in challenge.parameters:
            challenged = _scope_string(challenge.parameters['scope'])
        if advertised is not None:
            validate_url(advertised, resource=resource)
            candidates = (advertised,)
        else:
            candidates = resource_metadata_urls(resource)
        document, source = None, None
        for candidate in candidates:
            try:
                document, source = self._get(candidate, resource), candidate
                break
            except MetadataUnavailable:
                continue
        if document is None:
            raise MetadataUnavailable('Protected resource metadata could not be discovered.')
        if document.get('resource') != resource:
            raise OAuthMetadataError('Protected resource metadata must match the exact reviewed resource.')
        issuers = _strings(document.get('authorization_servers'), maximum=4, length=MAX_URL_BYTES)
        if not issuers:
            raise OAuthMetadataError('Protected resource metadata needs an authorization server.')
        for issuer in issuers:
            validate_url(issuer, resource=resource)
        supported = _scopes(document.get('scopes_supported', []))
        return {'resource': resource, 'authorization_servers': issuers, 'metadata_url': source,
                'scopes_supported': supported, 'challenge_scopes': challenged,
                'suggested_scopes': challenged if challenged is not None else supported,
                'provenance': 'untrusted_server_metadata', 'requires_native_review': True}

    def discover_issuer(self, issuer, *, resource):
        validate_url(resource)
        document, source = None, None
        for candidate in issuer_metadata_urls(issuer, resource=resource):
            try:
                document, source = self._get(candidate, resource), candidate
                break
            except MetadataUnavailable:
                continue
        if document is None:
            raise MetadataUnavailable('Authorization server metadata could not be discovered.')
        if document.get('issuer') != issuer:
            raise OAuthMetadataError('Authorization server metadata must match the exact selected issuer.')
        methods = _strings(document.get('code_challenge_methods_supported', []))
        if 'S256' not in methods:
            raise OAuthMetadataError('Authorization server must advertise S256 PKCE.')
        auth_methods = _strings(document.get('token_endpoint_auth_methods_supported', ['client_secret_basic']))
        if 'none' not in auth_methods:
            raise OAuthMetadataError('Authorization server requires confidential-client authentication.')
        for key, wanted in (('grant_types_supported', 'authorization_code'), ('response_types_supported', 'code')):
            if key in document and wanted not in _strings(document[key]):
                raise OAuthMetadataError('Authorization server does not support the public code flow.')
        endpoints = {}
        for key in ('authorization_endpoint', 'token_endpoint', 'revocation_endpoint', 'registration_endpoint'):
            if key not in document and key in {'revocation_endpoint', 'registration_endpoint'}:
                continue
            validate_url(document.get(key), resource=resource)
            endpoints[key] = document[key]
        for key in ('authorization_response_iss_parameter_supported', 'client_id_metadata_document_supported'):
            if key in document and type(document[key]) is not bool:
                raise OAuthMetadataError('Invalid authorization server capability flag.')
        issuer_origin = _origin(validate_url(issuer, resource=resource))
        cross_origin = [key for key, url in endpoints.items() if _origin(validate_url(url, resource=resource)) != issuer_origin]
        return {'issuer': issuer, 'resource': resource, 'metadata_url': source,
                'endpoints': endpoints, 'cross_origin_endpoints': cross_origin,
                'scopes_supported': _scopes(document.get('scopes_supported', [])),
                'code_challenge_methods_supported': methods,
                'token_endpoint_auth_methods_supported': auth_methods,
                'require_iss': document.get('authorization_response_iss_parameter_supported', False),
                'client_id_metadata_document_supported': document.get('client_id_metadata_document_supported', False),
                'provenance': 'untrusted_server_metadata', 'requires_native_review': True}

    def read_client_id_metadata(self, client_id, *, resource, document=None):
        """Read an existing supplied reference, or validate a supplied document.

        hosting_verified only records this successful GET; it is no assurance of
        future availability or of the authorization server accepting the URL.
        """
        validate_client_id_metadata(client_id)
        fetched = document is None
        value = self._get(client_id, resource) if fetched else document
        result = validate_client_id_metadata(client_id, value)
        result['hosting_verified'] = fetched
        return result

    def preview_registration(self, issuer_metadata, *, resource, redirect_uri,
                             client_name='Pebble', scopes=None, binding='', refresh=False):
        """Plan only. Native caller must already own the exact redirect socket."""
        value = metadata_document(issuer_metadata)
        validate_url(resource)
        issuer = value.get('issuer')
        validate_url(issuer, resource=resource)
        if value.get('resource') != resource or 'S256' not in _strings(value.get('code_challenge_methods_supported', [])):
            raise OAuthMetadataError('Registration requires resource-bound S256 issuer metadata.')
        if 'none' not in _strings(value.get('token_endpoint_auth_methods_supported', [])):
            raise OAuthMetadataError('Registration requires a public client without confidential authentication.')
        endpoints = value.get('endpoints')
        if not isinstance(endpoints, dict) or not {'authorization_endpoint', 'token_endpoint', 'registration_endpoint'} <= endpoints.keys():
            raise OAuthMetadataError('Authorization server did not advertise a registration endpoint.')
        checked = {}
        for key in ('authorization_endpoint', 'token_endpoint', 'registration_endpoint', 'revocation_endpoint'):
            if key in endpoints:
                validate_url(endpoints[key], resource=resource)
                checked[key] = endpoints[key]
        requested = _scopes([] if scopes is None else scopes)
        _redirect(redirect_uri)
        if not isinstance(binding, str):
            raise OAuthMetadataError('Registration binding must be bounded text.')
        if binding:
            _text(binding, 256)
        if type(refresh) is not bool:
            raise OAuthMetadataError('Invalid registration refresh capability.')
        body = {'client_name': _text(client_name), 'redirect_uris': [redirect_uri],
                'token_endpoint_auth_method': 'none', 'application_type': 'native',
                'grant_types': ['authorization_code'] + (['refresh_token'] if refresh else []),
                'response_types': ['code']}
        if requested:
            body['scope'] = ' '.join(requested)
        public = {'plan_id': secrets.token_hex(16), 'nonce': secrets.token_urlsafe(32),
                  'resource': resource, 'issuer': issuer, 'endpoints': checked,
                  'registration_endpoint': checked['registration_endpoint'],
                  'redirect_uri': redirect_uri, 'request_body': body, 'scopes': requested,
                  'binding': binding, 'requires_native_review': True}
        public['digest'] = _digest(public)
        with self._lock:
            now = self.clock()
            self._plans = {key: plan for key, plan in self._plans.items() if plan['deadline'] > now}
            if len(self._plans) >= self.max_plans:
                raise OAuthMetadataError('Too many pending registration reviews.')
            self._plans[public['plan_id']] = {'public': copy.deepcopy(public), 'deadline': now + self.plan_ttl}
        return {**copy.deepcopy(public), 'expires_in': self.plan_ttl}

    def cancel_registration(self, plan_id):
        with self._lock:
            return self._plans.pop(plan_id, None) is not None

    def register(self, plan_id, approved_digest, *, binding=''):
        """Consume one exact native approval before a single effectful POST.

        Caller must write durable admission evidence BEFORE calling this method.
        A crashed process loses this RAM registry; durable host evidence prevents
        resubmitting the old plan after restart. No automatic registration retry.
        """
        with self._lock:
            plan = self._plans.get(plan_id) if isinstance(plan_id, str) else None
            if (not plan or plan['deadline'] <= self.clock()
                    or not isinstance(approved_digest, str) or not re.fullmatch('[a-f0-9]{64}', approved_digest)
                    or not hmac.compare_digest(plan['public']['digest'], approved_digest)
                    or plan['public']['binding'] != binding):
                raise OAuthMetadataError('Registration approval expired, changed or was already consumed.')
            self._plans.pop(plan_id)
            public = plan['public']
        packet = {'url': public['registration_endpoint'], 'method': 'POST',
                  'body': copy.deepcopy(public['request_body']),
                  'headers': {'Accept': 'application/json', 'Content-Type': 'application/json'},
                  'purpose': 'oauth-json'}
        try:
            reply = self.request(packet)
        except RequestNotDispatched:
            raise RequestNotDispatched('Registration stopped before HTTP admission; its review was consumed.') from None
        except Exception:
            raise RegistrationUnknown('Registration outcome is unknown; reconcile provider state before a new review.') from None
        if not isinstance(reply, dict) or reply.get('transport_error'):
            raise RegistrationUnknown('Registration outcome is unknown; do not repeat the request.')
        status = reply.get('http_error', reply.get('status'))
        if type(status) is int and status in {400, 401, 403, 404, 405, 409, 410, 415, 422}:
            raise RegistrationRejected('The server rejected public-client registration; review its requirements.')
        if type(status) is not int or status != 201:
            raise RegistrationUnknown('Registration did not return a usable confirmed result; do not repeat it.')
        try:
            document = metadata_document(reply.get('document', reply.get('raw_document')))
            return self._registration_result(public, document)
        except Exception:
            raise RegistrationUnknown('Registration returned unusable public-client metadata; reconcile provider state before a new review.') from None

    def _registration_result(self, plan, document):
        body = plan['request_body']
        if any(key in document for key in ('access_token', 'refresh_token', 'id_token')):
            raise OAuthMetadataError('Registration returned unsupported authorization credentials.')
        client_id = _text(document.get('client_id'), 200)
        if any(ord(c) <= 32 or ord(c) > 126 for c in client_id):
            raise OAuthMetadataError('Invalid public client ID.')
        # Omission defaults to confidential authentication under RFC 7591.
        if document.get('token_endpoint_auth_method') != 'none':
            raise OAuthMetadataError('Registration requires confidential-client authentication.')
        if document.get('redirect_uris') != body['redirect_uris']:
            raise OAuthMetadataError('Registration changed the reviewed callbacks.')
        if document.get('application_type', 'native') != 'native':
            raise OAuthMetadataError('Registration changed the native client type.')
        grants = _strings(document.get('grant_types', body['grant_types']), maximum=2)
        if 'authorization_code' not in grants or not set(grants) <= set(body['grant_types']):
            raise OAuthMetadataError('Registration widened the reviewed grant types.')
        if document.get('response_types', ['code']) != ['code']:
            raise OAuthMetadataError('Registration changed the code flow.')
        issued = _scope_string(document['scope']) if 'scope' in document else plan['scopes']
        if not set(issued) <= set(plan['scopes']):
            raise OAuthMetadataError('Registration widened the reviewed scopes.')
        private = {}
        for key in ('client_secret', 'registration_access_token'):
            if key in document:
                value = _text(document[key], 8192)
                if any(ord(c) <= 32 or ord(c) > 126 for c in value):
                    raise OAuthMetadataError('Invalid private registration material.')
                private[key] = value
        if 'registration_client_uri' in document:
            validate_url(document['registration_client_uri'], resource=plan['resource'])
            private['registration_client_uri'] = document['registration_client_uri']
        for key in ('client_secret_expires_at', 'client_id_issued_at'):
            if key in document:
                if type(document[key]) is not int or not 0 <= document[key] <= 4_102_444_800:
                    raise OAuthMetadataError('Invalid registration timestamp.')
                private[key] = document[key]
        result = {'client_id': client_id, 'resource': plan['resource'], 'issuer': plan['issuer'],
                  'registration_endpoint': plan['registration_endpoint'],
                  'redirect_uris': list(body['redirect_uris']), 'client_name': body['client_name'],
                  'token_endpoint_auth_method': 'none', 'scopes': issued,
                  'grant_types': grants, 'plan_digest': plan['digest']}
        serialized = _packed(result)
        if any(isinstance(value, str) and value in serialized for value in private.values()):
            raise OAuthMetadataError('Private registration material contaminated public metadata.')
        return RegistrationResult(result, private)
