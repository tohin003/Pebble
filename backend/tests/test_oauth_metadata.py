"""OAuth interop boundaries using injected replies and an owned loopback server."""
import copy
import http.client
import json
import socket
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pebble.oauth_metadata import (
    MAX_DOCUMENT_BYTES, MetadataUnavailable, OAuthMetadata, OAuthMetadataError,
    RegistrationRejected, RegistrationUnknown, RequestNotDispatched,
    bearer_challenge, issuer_metadata_urls, metadata_document,
    parse_www_authenticate, resource_metadata_urls, validate_client_id_metadata,
    validate_resolved_addresses, validate_url,
)
from pebble.app import App
from pebble.connector_auth import ConnectorAuth, Records
from pebble.policy import PolicyError

RESOURCE = 'https://mcp.example.com/team/mcp'
ISSUER = 'https://auth.example.com/tenant'
REDIRECT = 'http://127.0.0.1:34567/connector/callback'
REGISTRATION_TOKEN = 'fixture-private-registration-token-0123456789'
CLIENT_SECRET = 'fixture-unused-public-client-secret-0123456789'
ACCESS_TOKEN = 'fixture-native-interop-access-0123456789'
REFRESH_TOKEN = 'fixture-native-interop-refresh-0123456789'


def issuer_document(issuer=ISSUER):
    return {'issuer': issuer, 'authorization_endpoint': 'https://login.example.com/authorize',
            'token_endpoint': 'https://tokens.example.com/token',
            'registration_endpoint': 'https://registration.example.com/clients',
            'revocation_endpoint': 'https://tokens.example.com/revoke',
            'code_challenge_methods_supported': ['S256'],
            'token_endpoint_auth_methods_supported': ['none', 'client_secret_basic'],
            'client_id_metadata_document_supported': True,
            'authorization_response_iss_parameter_supported': True}


class FixtureTransport:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, packet):
        with self.lock:
            self.calls.append(copy.deepcopy(packet))
        assert set(packet['headers']) <= {'Accept', 'Content-Type'}
        assert packet['purpose'] == 'oauth-json' and 'form' not in packet
        response = self.routes.get(packet['url'], {'http_error': 404})
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(packet)
        return copy.deepcopy(response)


def helper_fixture():
    transport = FixtureTransport({issuer_metadata_urls(ISSUER)[0]: {'document': issuer_document()}})
    helper = OAuthMetadata(transport)
    metadata = helper.discover_issuer(ISSUER, resource=RESOURCE)
    return helper, metadata, transport


def registration_document(packet):
    return {'status': 201, 'document': {'client_id': 'registered-public-client', **packet['body'],
            'client_secret': CLIENT_SECRET, 'registration_access_token': REGISTRATION_TOKEN,
            'registration_client_uri': 'https://registration.example.com/clients/registered-public-client',
            'client_id_issued_at': 1_750_000_000, 'client_secret_expires_at': 0,
            'provider_untrusted_extra': {'secret': 'discarded'}}}


def registration_plan(helper, metadata, **kwargs):
    return helper.preview_registration(metadata, resource=RESOURCE, redirect_uri=REDIRECT,
                                       scopes=['read'], binding='plugin-base-digest', **kwargs)


def test_challenges_preserve_escaped_quotes_backslashes_and_commas():
    challenges = parse_www_authenticate([
        'Digest realm="x,y", nonce="a\\\"b\\\\c", algorithm=SHA-256, Basic Zm9vOmJhcg==',
        'Bearer realm="mcp, team", resource_metadata="https://metadata.example.com/resource.json", scope="files:read files:write"',
    ])
    assert [challenge.scheme for challenge in challenges] == ['digest', 'basic', 'bearer']
    assert challenges[0].parameters == {'realm': 'x,y', 'nonce': 'a"b\\c', 'algorithm': 'SHA-256'}
    assert challenges[1].token68 == 'Zm9vOmJhcg=='
    assert challenges[2].parameters['realm'] == 'mcp, team'
    assert bearer_challenge('Basic realm="other", bEaReR scope="read"').parameters == {'scope': 'read'}


@pytest.mark.parametrize('header', [
    'Bearer scope="read", Scope="write"', 'Bearer realm="unterminated',
    'Bearer scope="read" trailing', 'Bearer scope="read",', 'Bearer scope="read",, Basic realm="x"',
    'Bearer scope="read"\r\nX-Poison: x', 'Bearer scope="read\x00"', 'Bearer realm="x\\',
    'Bearer @bad=x', 'Bearer scope="read",bad=', 'Bearer=bad', ' ', '\t',
    'Bearer realm="' + 'x' * 2_049 + '"', 'Bearer ' + 'x' * 8_192,
    ['Basic realm="x"'] * 9, 'Bearer realm="é"', 'Bearer realm="\ud800"',
])
def test_challenge_poison_is_rejected(header):
    with pytest.raises(OAuthMetadataError):
        parse_www_authenticate(header)


@pytest.mark.parametrize('header', ['Bearer scope="read", Bearer scope="write"',
                                 ['Bearer scope="read"', 'Bearer scope="read"'], 'Bearer abc=='])
def test_ambiguous_bearer_challenges_are_not_selected(header):
    with pytest.raises(OAuthMetadataError):
        bearer_challenge(header)


def test_empty_and_other_auth_schemes_have_no_bearer():
    assert parse_www_authenticate(None) == ()
    assert parse_www_authenticate('') == ()
    assert bearer_challenge('Basic realm="login"') is None


@pytest.mark.parametrize('destination', [
    'http://public.example.com/metadata', 'https://169.254.169.254/latest/meta-data',
    'https://10.0.0.1/metadata', 'https://[::ffff:127.0.0.1]/metadata', 'https://[fe80::1]/metadata',
    'https://127.0.0.1/metadata', 'https://localhost/metadata', 'https://service.internal/metadata',
    'https://evil.localhost/metadata', 'https://user:password@public.example.com/metadata',
    'https://public.example.com/metadata#fragment', 'https://public.example.com/metadata?access_token=poison',
    'https://127.1/metadata', 'https://2130706433/metadata', 'https://0x7f.0.0.1/metadata',
    'https://public.example.com:0/metadata', 'https://public.example.com\\@127.0.0.1/metadata',
    'https://public.example.com./metadata', 'file:///private/credential', '//public.example.com/metadata',
])
def test_advertised_metadata_ssrf_stops_before_transport(destination):
    transport = FixtureTransport()
    with pytest.raises(OAuthMetadataError):
        OAuthMetadata(transport).discover_resource(RESOURCE, f'Bearer resource_metadata="{destination}"')
    assert not transport.calls


@pytest.mark.parametrize('addresses', [[], ['10.0.0.1'], ['169.254.169.254'],
                                     ['8.8.8.8', '127.0.0.1'], ['::ffff:127.0.0.1'], ['bad-address']])
def test_dns_pin_policy_rejects_private_or_mixed_public_answers(addresses):
    with pytest.raises(OAuthMetadataError):
        validate_resolved_addresses('https://public.example.com/metadata', addresses, resource=RESOURCE)


def test_dns_pin_policy_does_not_allow_public_answers_for_loopback_host():
    local = 'http://127.0.0.1:34567/mcp'
    assert validate_resolved_addresses(local, ['127.0.0.1'], resource=local) == ('127.0.0.1',)
    assert validate_resolved_addresses('https://public.example.com/metadata', ['8.8.8.8']) == ('8.8.8.8',)
    with pytest.raises(OAuthMetadataError):
        validate_resolved_addresses(local, ['8.8.8.8'], resource=local)
    with pytest.raises(OAuthMetadataError):
        validate_url('http://127.0.0.1:34568/metadata', resource=local)
    with pytest.raises(OAuthMetadataError):
        validate_url('http://localhost:34567/metadata', resource=local)


def test_challenge_metadata_priority_and_authoritative_scopes():
    advertised = 'https://metadata.example.com/resource.json'
    transport = FixtureTransport({advertised: {'document': {'resource': RESOURCE,
        'authorization_servers': [ISSUER], 'scopes_supported': ['default:read']}}})
    result = OAuthMetadata(transport).discover_resource(RESOURCE,
        f'Basic realm="irrelevant", Bearer realm="team, x", resource_metadata="{advertised}", scope="files:write"')
    assert [call['url'] for call in transport.calls] == [advertised]
    assert result['resource'] == RESOURCE and result['metadata_url'] == advertised
    assert result['suggested_scopes'] == result['challenge_scopes'] == ['files:write']
    assert result['scopes_supported'] == ['default:read'] and result['requires_native_review']


def test_advertised_metadata_failure_does_not_fall_back_to_unadvertised_documents():
    transport = FixtureTransport()
    with pytest.raises(MetadataUnavailable):
        OAuthMetadata(transport).discover_resource(RESOURCE, 'Bearer resource_metadata="https://metadata.example.com/missing"')
    assert len(transport.calls) == 1


def test_resource_path_then_root_discovery_and_no_credential_packets():
    candidates = resource_metadata_urls(RESOURCE)
    assert candidates == ('https://mcp.example.com/.well-known/oauth-protected-resource/team/mcp',
                          'https://mcp.example.com/.well-known/oauth-protected-resource')
    transport = FixtureTransport({candidates[1]: {'document': {'resource': RESOURCE, 'authorization_servers': [ISSUER]}}})
    result = OAuthMetadata(transport).discover_resource(RESOURCE)
    assert result['suggested_scopes'] == [] and result['challenge_scopes'] is None
    assert [call['url'] for call in transport.calls] == list(candidates)
    assert all(call['method'] == 'GET' and call['body'] is None for call in transport.calls)


@pytest.mark.parametrize('change', [
    {'resource': RESOURCE + '/'}, {'authorization_servers': [ISSUER, ISSUER]},
    {'authorization_servers': []}, {'authorization_servers': ['https://10.0.0.1/issuer']},
    {'scopes_supported': ['read', 'read']}, {'scopes_supported': ['has space']},
])
def test_wrong_resource_or_poisoned_resource_document_does_not_fall_back(change):
    value = {'resource': RESOURCE, 'authorization_servers': [ISSUER], **change}
    transport = FixtureTransport({resource_metadata_urls(RESOURCE)[0]: {'document': value}})
    with pytest.raises(OAuthMetadataError):
        OAuthMetadata(transport).discover_resource(RESOURCE)
    assert len(transport.calls) == 1


def test_all_issuer_discovery_fallbacks_preserve_exact_issuer():
    candidates = issuer_metadata_urls(ISSUER)
    assert candidates == ('https://auth.example.com/.well-known/oauth-authorization-server/tenant',
                          'https://auth.example.com/.well-known/openid-configuration/tenant',
                          'https://auth.example.com/tenant/.well-known/openid-configuration')
    transport = FixtureTransport({candidates[-1]: {'document': issuer_document()}})
    result = OAuthMetadata(transport).discover_issuer(ISSUER, resource=RESOURCE)
    assert [call['url'] for call in transport.calls] == list(candidates)
    assert result['issuer'] == ISSUER and result['metadata_url'] == candidates[-1]
    assert result['cross_origin_endpoints'] == ['authorization_endpoint', 'token_endpoint', 'revocation_endpoint', 'registration_endpoint']
    assert result['require_iss'] and result['client_id_metadata_document_supported']
    assert 'token_endpoint' in result['endpoints'] and all(call['method'] == 'GET' for call in transport.calls)
    assert len(issuer_metadata_urls('https://auth.example.com')) == 2


@pytest.mark.parametrize('change', [
    {'issuer': ISSUER + '/'}, {'issuer': 'https://AUTH.example.com/tenant'},
    {'issuer': 'https://other.example.com/tenant'}, {'code_challenge_methods_supported': ['plain']},
    {'code_challenge_methods_supported': 'S256'}, {'token_endpoint_auth_methods_supported': ['client_secret_basic']},
    {'authorization_endpoint': 'https://10.0.0.1/login'}, {'token_endpoint': 'http://tokens.example.com/token'},
    {'registration_endpoint': 'https://localhost/clients'}, {'revocation_endpoint': '//tokens.example.com/revoke'},
    {'authorization_response_iss_parameter_supported': 'true'}, {'client_id_metadata_document_supported': 1},
    {'grant_types_supported': ['client_credentials']}, {'response_types_supported': ['token']},
])
def test_wrong_issuer_or_invalid_endpoints_fail_closed_without_fallback(change):
    transport = FixtureTransport({issuer_metadata_urls(ISSUER)[0]: {'document': {**issuer_document(), **change}}})
    with pytest.raises(OAuthMetadataError):
        OAuthMetadata(transport).discover_issuer(ISSUER, resource=RESOURCE)
    assert len(transport.calls) == 1


@pytest.mark.parametrize('reply', [{'http_error': 302}, {'status': 401}, {'status': '200'},
                                 {'document': []}, {'raw_document': '{"issuer":"one","issuer":"two"}'},
                                 {'transport_error': 'OAuthMetadataError'}, {'transport_error': 'PolicyError'}])
def test_malformed_success_and_redirects_do_not_trigger_metadata_fallback(reply):
    transport = FixtureTransport({issuer_metadata_urls(ISSUER)[0]: reply})
    with pytest.raises(OAuthMetadataError):
        OAuthMetadata(transport).discover_issuer(ISSUER, resource=RESOURCE)
    assert len(transport.calls) == 1


@pytest.mark.parametrize('document', [
    '{"resource":"one","resource":"two"}', '{"nested":{"scope":"read","scope":"write"}}',
    '{"value":NaN}', {'value': float('inf')}, b'{}' + b' ' * MAX_DOCUMENT_BYTES,
    {'value': 'x' * MAX_DOCUMENT_BYTES}, {'value': [[[['x']]]] * 4_096},
])
def test_json_duplicates_and_boundaries_are_rejected(document):
    with pytest.raises(OAuthMetadataError):
        metadata_document(document)


def test_registration_preview_is_exact_and_has_no_http_effect():
    helper, metadata, transport = helper_fixture()
    count = len(transport.calls)
    plan = registration_plan(helper, metadata, refresh=True)
    assert len(transport.calls) == count
    assert plan['request_body'] == {'client_name': 'Pebble', 'redirect_uris': [REDIRECT],
        'application_type': 'native', 'token_endpoint_auth_method': 'none',
        'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'], 'scope': 'read'}
    assert len(plan['digest']) == 64 and plan['nonce'] and plan['expires_in'] == 300
    assert plan['resource'] == RESOURCE and plan['endpoints'] == metadata['endpoints']
    assert plan['registration_endpoint'] == metadata['endpoints']['registration_endpoint']


def test_registration_result_separates_private_material_and_ignores_unknown_fields():
    helper, metadata, transport = helper_fixture()
    transport.routes[metadata['endpoints']['registration_endpoint']] = registration_document
    plan = registration_plan(helper, metadata)
    # Mutating a returned preview cannot change the stored approved request.
    plan['request_body']['scope'] = 'read write'
    plan['endpoints']['registration_endpoint'] = 'https://attacker.example.com/clients'
    result = helper.register(plan['plan_id'], plan['digest'], binding='plugin-base-digest')
    assert result.public['client_id'] == 'registered-public-client' and result.public['scopes'] == ['read']
    assert result.private['registration_access_token'] == REGISTRATION_TOKEN
    assert result.private['client_secret'] == CLIENT_SECRET
    assert 'provider_untrusted_extra' not in result.public and 'client_secret' not in result.public
    assert CLIENT_SECRET not in repr(result) and REGISTRATION_TOKEN not in json.dumps(result.public)
    assert transport.calls[-1]['body']['scope'] == 'read' and transport.calls[-1]['method'] == 'POST'
    with pytest.raises(OAuthMetadataError):
        helper.register(plan['plan_id'], plan['digest'], binding='plugin-base-digest')
    assert sum(call['method'] == 'POST' for call in transport.calls) == 1


@pytest.mark.parametrize('change', [
    {'scope': 'read write'}, {'scope': ['read']}, {'scope': ''},
    {'token_endpoint_auth_method': 'client_secret_basic'}, {'token_endpoint_auth_method': None},
    {'redirect_uris': [REDIRECT, 'http://127.0.0.1:34568/connector/callback']},
    {'application_type': 'web'}, {'response_types': ['token']}, {'grant_types': ['client_credentials']},
    {'grant_types': ['authorization_code', 'refresh_token']}, {'client_id': REGISTRATION_TOKEN},
    {'client_name': REGISTRATION_TOKEN, 'client_id': REGISTRATION_TOKEN},
    {'registration_client_uri': 'https://169.254.169.254/clients'}, {'access_token': REGISTRATION_TOKEN},
    {'client_secret_expires_at': True}, {'registration_access_token': '\nprivate'},
])
def test_unusable_registration_results_never_publish_or_retry(change):
    helper, metadata, transport = helper_fixture()
    endpoint = metadata['endpoints']['registration_endpoint']
    transport.routes[endpoint] = lambda packet: {'status': 201,
        'document': {**registration_document(packet)['document'], **change}}
    plan = registration_plan(helper, metadata)
    with pytest.raises(RegistrationUnknown) as captured:
        helper.register(plan['plan_id'], plan['digest'], binding='plugin-base-digest')
    assert REGISTRATION_TOKEN not in str(captured.value) and CLIENT_SECRET not in str(captured.value)
    assert captured.value.outcome == 'unknown'
    with pytest.raises(OAuthMetadataError):
        helper.register(plan['plan_id'], plan['digest'], binding='plugin-base-digest')
    assert sum(call['method'] == 'POST' for call in transport.calls) == 1


@pytest.mark.parametrize('reply', [
    RuntimeError(REGISTRATION_TOKEN), {'transport_error': REGISTRATION_TOKEN}, {'status': 500},
    {'status': 408}, {'status': 429}, {'status': 200, 'document': {}}, {'document': {}},
    {'status': 201, 'raw_document': '{"client_id":"one","client_id":"two"}'},
])
def test_lost_or_ambiguous_registration_response_consumes_approval(reply):
    helper, metadata, transport = helper_fixture()
    transport.routes[metadata['endpoints']['registration_endpoint']] = reply
    plan = registration_plan(helper, metadata)
    with pytest.raises(RegistrationUnknown) as captured:
        helper.register(plan['plan_id'], plan['digest'], binding='plugin-base-digest')
    assert REGISTRATION_TOKEN not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None or captured.value.__suppress_context__
    with pytest.raises(OAuthMetadataError):
        helper.register(plan['plan_id'], plan['digest'], binding='plugin-base-digest')
    assert sum(call['method'] == 'POST' for call in transport.calls) == 1


def test_known_rejection_and_proven_pre_dispatch_stop_are_consumed_and_safe():
    helper, metadata, transport = helper_fixture()
    endpoint = metadata['endpoints']['registration_endpoint']
    transport.routes[endpoint] = {'http_error': 400, 'document': {'error_description': REGISTRATION_TOKEN}}
    plan = registration_plan(helper, metadata)
    with pytest.raises(RegistrationRejected) as captured:
        helper.register(plan['plan_id'], plan['digest'], binding='plugin-base-digest')
    assert captured.value.outcome == 'rejected' and REGISTRATION_TOKEN not in str(captured.value)
    transport.routes[endpoint] = RequestNotDispatched(REGISTRATION_TOKEN)
    second = registration_plan(helper, metadata)
    with pytest.raises(RequestNotDispatched) as captured:
        helper.register(second['plan_id'], second['digest'], binding='plugin-base-digest')
    assert REGISTRATION_TOKEN not in str(captured.value)
    with pytest.raises(OAuthMetadataError):
        helper.register(second['plan_id'], second['digest'], binding='plugin-base-digest')


def test_concurrent_registration_approval_dispatches_exactly_once():
    helper, metadata, transport = helper_fixture()
    entered, release = threading.Event(), threading.Event()

    def response(packet):
        entered.set()
        assert release.wait(3)
        return registration_document(packet)

    transport.routes[metadata['endpoints']['registration_endpoint']] = response
    plan = registration_plan(helper, metadata)
    with ThreadPoolExecutor(max_workers=6) as pool:
        first = pool.submit(helper.register, plan['plan_id'], plan['digest'], binding='plugin-base-digest')
        assert entered.wait(3)
        others = [pool.submit(helper.register, plan['plan_id'], plan['digest'], binding='plugin-base-digest') for _ in range(5)]
        for future in others:
            with pytest.raises(OAuthMetadataError):
                future.result(timeout=3)
        release.set()
        assert first.result(timeout=3).public['client_id'] == 'registered-public-client'
    assert sum(call['method'] == 'POST' for call in transport.calls) == 1


def test_approval_digest_binding_expiry_and_cancellation_stop_before_dispatch():
    helper, metadata, transport = helper_fixture()
    now = [10]
    helper.clock = lambda: now[0]
    plan = registration_plan(helper, metadata)
    for digest, binding in [('0' * 64, 'plugin-base-digest'), ('é' * 64, 'plugin-base-digest'),
                            (plan['digest'], 'changed-plugin'), (None, 'plugin-base-digest')]:
        with pytest.raises(OAuthMetadataError):
            helper.register(plan['plan_id'], digest, binding=binding)
    now[0] = 311
    with pytest.raises(OAuthMetadataError):
        helper.register(plan['plan_id'], plan['digest'], binding='plugin-base-digest')
    second = registration_plan(helper, metadata)
    assert helper.cancel_registration(second['plan_id'])
    with pytest.raises(OAuthMetadataError):
        helper.register(second['plan_id'], second['digest'], binding='plugin-base-digest')
    assert not any(call['method'] == 'POST' for call in transport.calls)


def test_registration_registry_and_restart_do_not_restore_approval_authority():
    helper, metadata, transport = helper_fixture()
    plans = [registration_plan(helper, metadata) for _ in range(4)]
    with pytest.raises(OAuthMetadataError):
        registration_plan(helper, metadata)
    fresh = OAuthMetadata(transport)
    with pytest.raises(OAuthMetadataError):
        fresh.register(plans[0]['plan_id'], plans[0]['digest'], binding='plugin-base-digest')
    assert not any(call['method'] == 'POST' for call in transport.calls)


def client_id_document(client_id='https://pebble.example.com/oauth/client.json'):
    return {'client_id': client_id, 'client_name': 'Pebble', 'redirect_uris': [REDIRECT],
            'token_endpoint_auth_method': 'none'}


def test_supplied_client_id_metadata_validates_reference_without_claiming_hosting():
    client_id = 'https://pebble.example.com/oauth/client.json'
    reference = validate_client_id_metadata(client_id)
    assert not reference['document_supplied'] and not reference['hosting_verified']
    result = validate_client_id_metadata(client_id, client_id_document(), redirect_uri=REDIRECT)
    assert result['client_metadata']['client_id'] == client_id
    assert result['client_metadata']['grant_types'] == ['authorization_code']
    assert result['document_supplied'] and not result['hosting_verified']


@pytest.mark.parametrize('client_id,change', [
    ('http://pebble.example.com/client.json', {}), ('https://pebble.example.com', {}),
    ('https://127.0.0.1/client.json', {}), ('https://10.0.0.1/client.json', {}),
    ('https://pebble.example.com/client.json', {'client_id': 'https://pebble.example.com/client.json/'}),
    ('https://pebble.example.com/oauth/client.json', {'client_name': None}),
    ('https://pebble.example.com/oauth/client.json', {'redirect_uris': []}),
    ('https://pebble.example.com/oauth/client.json', {'redirect_uris': ['http://localhost:34567/callback']}),
    ('https://pebble.example.com/oauth/client.json', {'redirect_uris': [REDIRECT + '#fragment']}),
    ('https://pebble.example.com/oauth/client.json', {'token_endpoint_auth_method': 'private_key_jwt'}),
    ('https://pebble.example.com/oauth/client.json', {'client_secret': CLIENT_SECRET}),
    ('https://pebble.example.com/oauth/client.json', {'client_secret_expires_at': 0}),
    ('https://pebble.example.com/oauth/client.json', {'registration_access_token': REGISTRATION_TOKEN}),
    ('https://pebble.example.com/oauth/client.json', {'jwks_uri': 'https://pebble.example.com/keys'}),
    ('https://pebble.example.com/oauth/client.json', {'grant_types': ['client_credentials']}),
    ('https://pebble.example.com/oauth/client.json', {'response_types': ['token']}),
])
def test_invalid_client_id_metadata_is_not_adopted(client_id, change):
    with pytest.raises(OAuthMetadataError):
        validate_client_id_metadata(client_id, {**client_id_document(), **change}, redirect_uri=REDIRECT)


@pytest.mark.parametrize('client_id', ['https://pebble.example.com/./client.json',
                                     'https://pebble.example.com/oauth/../client.json',
                                     'https://pebble.example.com/oauth/%2e%2E/client.json'])
def test_client_id_metadata_references_do_not_normalize_dot_segments(client_id):
    with pytest.raises(OAuthMetadataError):
        validate_client_id_metadata(client_id)


def test_missing_public_auth_capability_is_not_treated_as_none():
    document = issuer_document()
    document.pop('token_endpoint_auth_methods_supported')
    transport = FixtureTransport({issuer_metadata_urls(ISSUER)[0]: {'document': document}})
    with pytest.raises(OAuthMetadataError, match='confidential'):
        OAuthMetadata(transport).discover_issuer(ISSUER, resource=RESOURCE)


@pytest.fixture
def loopback():
    state = {'base': '', 'posts': [], 'gets': [], 'mode': 'normal', 'admission': None, 'challenge': None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_document(self, value, status=200):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            state['gets'].append(self.path)
            if self.path == '/mcp':
                self.send_response(401)
                self.send_header('Content-Type', 'application/json')
                self.send_header('WWW-Authenticate', state['challenge'] or f'Basic realm="other, realm", Bearer resource_metadata="{state["base"]}/.well-known/oauth-protected-resource/mcp", scope="read"')
                self.end_headers()
                self.wfile.write(b'{}')
            elif self.path == '/.well-known/oauth-protected-resource/mcp':
                self.send_document({'resource': state['base'] + '/mcp', 'authorization_servers': [state['base']],
                                    'scopes_supported': ['read']})
            elif self.path == '/.well-known/oauth-authorization-server':
                self.send_document({'issuer': state['base'], 'authorization_endpoint': state['base'] + '/authorize',
                    'token_endpoint': state['base'] + '/token', 'registration_endpoint': state['base'] + '/register',
                    'revocation_endpoint': state['base'] + '/revoke', 'authorization_response_iss_parameter_supported': True,
                    'client_id_metadata_document_supported': True,
                    'code_challenge_methods_supported': ['S256'], 'token_endpoint_auth_methods_supported': ['none']})
            else:
                self.send_document({}, 404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            body = ({key: value[0] for key,value in parse_qs(raw.decode()).items()}
                    if self.path in {'/token', '/revoke'} else json.loads(raw))
            state['posts'].append({'path': self.path, 'body': body, 'authorization': self.headers.get('Authorization')})
            if self.path == '/mcp':
                self.send_response(401)
                self.send_header('Content-Type', 'application/json')
                self.send_header('WWW-Authenticate', f'Basic realm="other, realm", Bearer resource_metadata="{state["base"]}/.well-known/oauth-protected-resource/mcp", scope="read"')
                self.end_headers()
                self.wfile.write(b'{}')
                return
            if self.path == '/token':
                self.send_document({'access_token': ACCESS_TOKEN, 'refresh_token': REFRESH_TOKEN,
                    'token_type': 'Bearer', 'expires_in': 3600, 'scope': 'read'})
                return
            if self.path == '/revoke':
                self.send_response(200)
                self.end_headers()
                return
            if state['admission']:
                state['admission']()
            if state['mode'] == 'lost_registration':
                self.connection.shutdown(2)
                self.connection.close()
                return
            if state['mode'] == 'rejected':
                self.send_document({'error_description': REGISTRATION_TOKEN}, 400)
                return
            if state['mode'] == 'wait':
                state['entered'].set()
                assert state['release'].wait(5)
            if state['mode'] == 'widen':
                body['scope'] = 'read write'
            self.send_document({'client_id': 'loopback-public-client', **body,
                                'client_secret': CLIENT_SECRET,
                                'registration_access_token': REGISTRATION_TOKEN}, 201)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state['base'] = f'http://127.0.0.1:{server.server_port}'

    def request(packet):
        parsed = validate_url(packet['url'], resource=state['base'] + '/mcp')
        address, = validate_resolved_addresses(packet['url'], ['127.0.0.1'], resource=state['base'] + '/mcp')
        connection = http.client.HTTPConnection(address, parsed.port, timeout=2)
        try:
            raw = json.dumps(packet['body']).encode() if packet['body'] is not None else None
            connection.request(packet['method'], parsed.path, body=raw, headers=packet['headers'])
            response = connection.getresponse()
            return {'status': response.status, 'raw_document': response.read(MAX_DOCUMENT_BYTES + 1)}
        finally:
            connection.close()

    try:
        yield state, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_owned_loopback_discovery_and_registration_once_without_credentials(loopback):
    state, request = loopback
    helper = OAuthMetadata(request)
    resource = state['base'] + '/mcp'
    discovered = helper.discover_resource(resource, f'Basic realm="a,b", Bearer scope="read"')
    issuer = discovered['authorization_servers'][0]
    metadata = helper.discover_issuer(issuer, resource=resource)
    plan = helper.preview_registration(metadata, resource=resource, redirect_uri=REDIRECT,
                                       scopes=['read'], binding='owned-loopback-plugin')
    result = helper.register(plan['plan_id'], plan['digest'], binding='owned-loopback-plugin')
    assert result.public['client_id'] == 'loopback-public-client'
    assert result.private['registration_access_token'] == REGISTRATION_TOKEN
    assert len(state['posts']) == 1 and state['posts'][0]['authorization'] is None
    assert state['posts'][0]['body']['redirect_uris'] == [REDIRECT]
    assert state['gets'] == ['/.well-known/oauth-protected-resource/mcp', '/.well-known/oauth-authorization-server']
    with pytest.raises(OAuthMetadataError):
        helper.register(plan['plan_id'], plan['digest'], binding='owned-loopback-plugin')
    assert len(state['posts']) == 1


class NativeFakeVault:
    def __init__(self):
        self.data = {}
        self.fail_set = self.fail_delete = False

    def get(self, key):
        return self.data.get(key, '')

    def set(self, key, value):
        if self.fail_set:
            raise OSError(REGISTRATION_TOKEN)
        self.data[key] = value

    def delete(self, key):
        if self.fail_delete:
            raise OSError(REGISTRATION_TOKEN)
        self.data.pop(key, None)


@pytest.fixture
def native_app(tmp_path):
    app = App(tmp_path/'data', tmp_path/'resources', lambda *args: None)
    app.connector_auth.records = Records(NativeFakeVault())
    try:
        yield app
    finally:
        app.close()


def native_install(app, state):
    return app.extension_add(kind='plugin', name='Native interop fixture', content='{"tools":["lookup"]}', url=state['base']+'/mcp')


def native_preview(app, state, id, **kwargs):
    return app.connector_operation('preview_registration', id=id, issuer=state['base'], requested_scopes=['read'], **kwargs)


def post_count(state, path='/register'):
    return sum(value['path'] == path for value in state['posts'])


def test_native_registration_admits_before_post_then_reuses_real_callback_and_cleans_private(native_app, loopback):
    app, state = native_app, loopback[0]
    id = native_install(app, state)
    discovery = app.connector_operation('discover_interop', id=id)
    assert discovery['registration_methods'] == ['pre_registered', 'client_id_metadata', 'dynamic_registration']
    assert not state['posts']
    preview = native_preview(app, state, id)
    assert not state['posts'] and preview['redirect_uri'] == app.connector_auth.registration_pending['redirect']
    # The reserved callback is active, but cannot accept a code before PKCE start.
    with pytest.raises(urllib.error.HTTPError) as captured:
        urllib.request.urlopen(preview['redirect_uri']+'?state=poison&code=poison', timeout=2)
    assert captured.value.code == 400 and not post_count(state, '/token')
    admissions = []
    state['admission'] = lambda: admissions.extend(app.store.query("SELECT state,plan_digest FROM connector_registrations WHERE plugin_id=?", (id,)))
    registered = app.connector_operation('register_client', id=id, preview_id=preview['preview_id'], digest=preview['digest'])
    assert admissions == [{'state': 'admitted', 'plan_digest': preview['digest']}]
    assert post_count(state) == 1 and registered['client_id'] == 'loopback-public-client'
    rows = app.store.query('SELECT * FROM connector_registrations WHERE plugin_id=?', (id,))
    assert rows[0]['state'] == 'registered' and rows[0]['grant_id']
    private = app.connector_auth.records.get(rows[0]['grant_id'])
    assert private['values'] == {'registration_access_token': REGISTRATION_TOKEN}
    assert CLIENT_SECRET not in json.dumps(app.connector_auth.records.vault.data)
    assert REGISTRATION_TOKEN not in json.dumps(rows) and REGISTRATION_TOKEN not in json.dumps(registered)
    started = app.connector_operation('start', id=id, preview_id=registered['preview_id'])
    query = parse_qs(urlsplit(started['url']).query)
    assert query['redirect_uri'] == [preview['redirect_uri']] and query['resource'] == [state['base']+'/mcp']
    assert query['client_id'] == ['loopback-public-client'] and query['code_challenge_method'] == ['S256']
    with urllib.request.urlopen(query['redirect_uri'][0]+'?'+urlencode({'state': query['state'][0], 'code': 'owned-code', 'iss': state['base']}), timeout=10) as response:
        assert response.status == 200
    assert app.connector_auth.metadata(id)['state'] == 'connected'
    assert not app.extensions.get(id)['enabled']
    token_post = next(value for value in state['posts'] if value['path'] == '/token')
    assert token_post['body']['resource'] == state['base']+'/mcp' and 'client_secret' not in token_post['body']
    for path in app.home.rglob('*'):
        if path.is_file():
            assert all(token.encode() not in path.read_bytes() for token in (REGISTRATION_TOKEN, CLIENT_SECRET, ACCESS_TOKEN, REFRESH_TOKEN))
    result = app.connector_operation('disconnect', id=id)
    assert not result['storage_cleanup_pending'] and result['remote_revocation_confirmed']
    assert not app.connector_auth.records.vault.data
    assert app.store.query('SELECT grant_id FROM connector_registrations WHERE plugin_id=?', (id,))[0]['grant_id'] is None
    assert post_count(state) == 1


@pytest.mark.parametrize('mode', ['lost_registration', 'widen', 'vault_failure'])
def test_native_unknown_registration_blocks_retry_across_restart(native_app, loopback, mode):
    app, state = native_app, loopback[0]
    id = native_install(app, state)
    preview = native_preview(app, state, id)
    if mode == 'vault_failure':
        app.connector_auth.records.vault.fail_set = True
    else:
        state['mode'] = mode
    with pytest.raises(PolicyError) as captured:
        app.connector_operation('register_client', id=id, preview_id=preview['preview_id'], digest=preview['digest'])
    assert REGISTRATION_TOKEN not in str(captured.value) and CLIENT_SECRET not in str(captured.value)
    assert app.store.query('SELECT state FROM connector_registrations WHERE plugin_id=?', (id,))[0]['state'] == 'unknown'
    assert app.connector_auth.registration_pending is None and not app.connector_auth.previews
    assert post_count(state) == 1
    vault = app.connector_auth.records.vault
    app.connector_auth.close()
    app.connector_auth = ConnectorAuth(app, vault=vault)
    app.extensions.auth = app.connector_auth
    with pytest.raises(PolicyError, match='unknown'):
        native_preview(app, state, id)
    assert post_count(state) == 1
    discovery = app.connector_operation('discover_interop', id=id)
    assert discovery['unresolved_registrations'][0]['state'] == 'unknown'
    vault.fail_set = False
    assert not app.connector_operation('disconnect', id=id)['storage_cleanup_pending']


def test_native_admitted_crash_marker_is_never_replayed(native_app, loopback):
    app, state = native_app, loopback[0]
    id = native_install(app, state)
    app.store.query('INSERT INTO connector_registrations VALUES(?,?,?,?,?,?,?,?,?,?)',
        ('crash-fixture', id, state['base'], state['base']+'/mcp', 'base', 'digest', 'admitted', None, None, 0))
    app.connector_auth.close()
    app.connector_auth = ConnectorAuth(app, vault=NativeFakeVault())
    app.extensions.auth = app.connector_auth
    assert app.store.query('SELECT state FROM connector_registrations')[0]['state'] == 'unknown'
    with pytest.raises(PolicyError, match='unknown'):
        native_preview(app, state, id)
    assert not post_count(state)


def test_native_replay_changed_config_and_outer_transactions_cannot_register(native_app, loopback):
    app, state = native_app, loopback[0]
    id = native_install(app, state)
    preview = native_preview(app, state, id)
    with pytest.raises(PolicyError):
        app.connector_operation('register_client', id=id, preview_id=preview['preview_id'], digest='0'*64)
    with app.store.db:
        app.store.db.execute('UPDATE extensions SET name=? WHERE id=?', ('Renamed fixture', id))
        with pytest.raises(PolicyError):
            app.connector_operation('register_client', id=id, preview_id=preview['preview_id'], digest=preview['digest'])
    assert not post_count(state)
    app.store.query('UPDATE extensions SET config=? WHERE id=?', (json.dumps({**app.extensions.get(id)['config'], 'url': state['base']+'/changed'}), id))
    with pytest.raises(PolicyError):
        app.connector_operation('register_client', id=id, preview_id=preview['preview_id'], digest=preview['digest'])
    assert not post_count(state)


def test_native_private_registration_cleanup_failure_blocks_removal_before_oauth_grant(native_app, loopback):
    app, state = native_app, loopback[0]
    id = native_install(app, state)
    preview = native_preview(app, state, id)
    app.connector_operation('register_client', id=id, preview_id=preview['preview_id'], digest=preview['digest'])
    vault = app.connector_auth.records.vault
    vault.fail_delete = True
    assert app.connector_operation('disconnect', id=id)['storage_cleanup_pending']
    assert app.connector_auth.metadata(id)['storage_cleanup_pending']
    with pytest.raises(PolicyError):
        app.extension_remove(id)
    vault.fail_delete = False
    assert not app.connector_operation('disconnect', id=id)['storage_cleanup_pending']
    assert not vault.data


def test_native_cancel_during_registration_prevents_late_client_publication(native_app, loopback):
    app, state = native_app, loopback[0]
    id = native_install(app, state)
    preview = native_preview(app, state, id)
    state.update(mode='wait', entered=threading.Event(), release=threading.Event())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(app.connector_operation, 'register_client', id=id, preview_id=preview['preview_id'], digest=preview['digest'])
        assert state['entered'].wait(5)
        app.connector_auth.cancel()
        state['release'].set()
        with pytest.raises(PolicyError):
            future.result(timeout=10)
    assert not app.connector_auth.previews and not app.connector_auth.records.vault.data
    assert app.store.query('SELECT state FROM connector_registrations')[0]['state'] == 'unknown'
    assert post_count(state) == 1


def test_native_preregistered_modern_review_sends_no_dynamic_registration(native_app, loopback):
    app, state = native_app, loopback[0]
    id = native_install(app, state)
    preview = native_preview(app, state, id, registration_method='pre_registered', client_id='already-registered')
    result = app.connector_operation('register_client', id=id, preview_id=preview['preview_id'], digest=preview['digest'])
    started = app.connector_operation('start', id=id, preview_id=result['preview_id'])
    assert parse_qs(urlsplit(started['url']).query)['client_id'] == ['already-registered']
    assert not post_count(state) and not app.store.query('SELECT * FROM connector_registrations')


@pytest.mark.parametrize('limit', ['plugin', 'global'])
def test_native_admission_limits_block_before_post_and_preserve_tombstones(native_app, loopback, limit):
    app, state = native_app, loopback[0]
    id = native_install(app, state)
    count = 16 if limit == 'plugin' else 800
    plugin = id if limit == 'plugin' else 'other-fixture-plugin'
    with app.store.db:
        app.store.db.executemany('INSERT INTO connector_registrations VALUES(?,?,?,?,?,?,?,?,?,?)',
            [(f'receipt-{index}', plugin, state['base'], state['base']+'/mcp', 'base', 'digest', 'registered', None, None, index) for index in range(count)])
    with pytest.raises(PolicyError, match='limit'):
        native_preview(app, state, id)
    assert not post_count(state) and len(app.store.query('SELECT * FROM connector_registrations')) == count


def test_native_oversized_auth_challenge_does_not_silently_select_fallback(native_app, loopback):
    app, state = native_app, loopback[0]
    id = native_install(app, state)
    state['challenge'] = 'Bearer realm="'+'x'*3_000+'"'
    with pytest.raises(PolicyError, match='challenge'):
        app.connector_operation('discover_interop', id=id)
    assert state['gets'] == ['/mcp'] and not state['posts']


def test_native_budget_is_rechecked_after_review_before_admission(native_app, loopback):
    app, state = native_app, loopback[0]
    id = native_install(app, state)
    preview = native_preview(app, state, id)
    with app.store.db:
        app.store.db.executemany('INSERT INTO connector_registrations VALUES(?,?,?,?,?,?,?,?,?,?)',
            [(f'receipt-{index}', id, state['base'], state['base']+'/mcp', 'base', 'digest', 'registered', None, None, index) for index in range(16)])
    with pytest.raises(PolicyError):
        app.connector_operation('register_client', id=id, preview_id=preview['preview_id'], digest=preview['digest'])
    assert not post_count(state) and len(app.store.query('SELECT * FROM connector_registrations')) == 16


def test_native_supplied_client_id_document_reserves_its_exact_callback_without_hosting(native_app, loopback):
    app, state = native_app, loopback[0]
    id = native_install(app, state)
    with socket.socket() as socket_fixture:
        socket_fixture.bind(('127.0.0.1', 0))
        port = socket_fixture.getsockname()[1]
    redirect = f'http://127.0.0.1:{port}/connector/callback'
    client_id = 'https://pebble.example.com/oauth/client.json'
    document = {'client_id': client_id, 'client_name': 'Pebble', 'redirect_uris': [redirect], 'token_endpoint_auth_method': 'none'}
    preview = native_preview(app, state, id, registration_method='client_id_metadata', client_id=client_id, client_metadata=document)
    assert preview['redirect_uri'] == redirect and not preview['client_metadata']['hosting_verified']
    result = app.connector_operation('register_client', id=id, preview_id=preview['preview_id'], digest=preview['digest'])
    started = app.connector_operation('start', id=id, preview_id=result['preview_id'])
    assert parse_qs(urlsplit(started['url']).query)['client_id'] == [client_id]
    assert not post_count(state) and not app.store.query('SELECT * FROM connector_registrations')


def test_existing_client_id_metadata_reference_is_fetched_once_and_exactly_validated():
    client_id = 'https://pebble.example.com/oauth/client.json'
    transport = FixtureTransport({client_id: {'status': 200, 'document': client_id_document()}})
    helper = OAuthMetadata(transport)
    result = helper.read_client_id_metadata(client_id, resource=RESOURCE)
    assert result['hosting_verified'] and len(transport.calls) == 1
    transport.routes[client_id]['document']['client_id'] = client_id + '/'
    with pytest.raises(OAuthMetadataError):
        helper.read_client_id_metadata(client_id, resource=RESOURCE)
