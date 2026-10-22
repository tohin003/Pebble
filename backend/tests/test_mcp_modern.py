"""Explicit modern mode against counted, real loopback HTTP MCP responses."""
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pebble.extensions import Extensions
from pebble.mcp_transport import MCP, MODERN, MCPUncertain
from pebble.policy import PolicyError
from pebble.store import Store

SPEC = {'name': 'lookup', 'description': 'Find fixture results', 'inputSchema': {'type': 'object',
    'properties': {'query': {'type': 'string', 'x-mcp-header': 'Query'},
        'nested': {'type': 'object', 'properties': {'enabled': {'type': 'boolean', 'x-mcp-header': 'Enabled'}}}},
    'required': ['query'], 'additionalProperties': False}, 'outputSchema': {'type': 'integer'}}


@pytest.fixture
def server():
    state = {'calls': [], 'headers': [], 'specs': [copy.deepcopy(SPEC)], 'modern': None,
        'value': 7, 'result_type': 'complete', 'catalogue': {'ttlMs': 5000, 'cacheScope': 'private'},
        'versions': [MODERN], 'session': False, 'deletes': 0,
        'discovery_cache': {'ttlMs': 5000, 'cacheScope': 'private'},
        'resource_cache': {'ttlMs': 5000, 'cacheScope': 'private'}}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_DELETE(self):
            state['deletes'] += 1
            self.send_response(204)
            self.end_headers()
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['calls'].append(body)
            state['headers'].append(dict(self.headers))
            method = body['method']
            if method in {'initialize', 'server/discover'}:
                state['modern'] = method == 'server/discover'
            if 'id' not in body:
                self.send_response(202)
                self.end_headers()
                return
            if method == 'server/discover':
                result = {'supportedVersions': state['versions'], 'capabilities': {'tools': {}, 'resources': {}, 'prompts': {}}}
                result.update(state['discovery_cache'])
            elif method == 'initialize':
                result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
            elif method.endswith('/list'):
                result = {method.split('/')[0]: state['specs'] if method == 'tools/list' else []}
                if state['modern']:
                    result.update(state['catalogue'])
            elif method == 'tools/call':
                result = {'content': [{'type': 'text', 'text': 'fixture output'}], 'isError': False}
                if not state.get('omit_structured'):
                    result['structuredContent'] = state['value']
            else:
                result = {'contents': [], 'messages': []}
                if method == 'resources/read' and state['modern']:
                    result.update(state['resource_cache'])
            if state['modern']:
                result_type = state['result_type'] if method in {'tools/call', 'resources/read', 'prompts/get'} else state.get('discovery_result_type', 'complete')
                if result_type is not None:
                    result['resultType'] = result_type
                if result_type == 'input_required':
                    result['inputRequests'] = [{'type': 'elicitation', 'message': 'untrusted request'}]
                elif result_type == 'task':
                    result = {'resultType': 'task', 'taskId': 'untrusted-task', 'status': 'working',
                        'createdAt': '2026-10-10T00:00:00Z', 'lastUpdatedAt': '2026-10-10T00:00:00Z',
                        'ttlMs': 60000, 'pollIntervalMs': 1000}
            event = {'jsonrpc': '2.0', 'id': body['id'], 'result': result}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            if state['session']:
                self.send_header('MCP-Session-Id', 'server-session-ignored-in-modern')
            self.end_headers()
            self.wfile.write(json.dumps(event).encode())
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: http.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{http.server_port}/mcp', state
    http.shutdown()
    http.server_close()
    thread.join()


@pytest.fixture
def extensions(tmp_path):
    store = Store(tmp_path / 'data')
    instance = Extensions(store)
    yield instance
    instance.close()
    store.close()


def client(server, modern=True):
    return MCP(config={'transport': 'http', 'url': server[0], 'protocol_era': 'modern' if modern else 'legacy',
        'schemas': copy.deepcopy(server[1]['specs'])})


def install(extensions, server, modern=True):
    identifier = extensions.add('plugin', 'Modern fixture', json.dumps({'tools': ['lookup'],
        'read_only': True, 'protocol_era': 'modern' if modern else 'legacy'}), server[0])
    extensions.toggle(identifier, True)
    return identifier, extensions.get(identifier)['config']['catalogue_digest']


def effect_count(state):
    return sum(call['method'] == 'tools/call' for call in state['calls'])


def test_modern_discovery_meta_headers_and_pinned_parameter_annotations(server):
    instance = client(server)
    try:
        assert instance.tools() == server[1]['specs']
        instance.call('lookup', {'query': 'héllo\nworld', 'nested': {'enabled': True}})
        assert instance.protocol == MODERN and instance.modern
        methods = [b['method'] for b in server[1]['calls']]
        assert methods == ['server/discover', 'tools/list', 'tools/call']
        for body, headers in zip(server[1]['calls'], server[1]['headers']):
            assert body['params']['_meta'] == {'io.modelcontextprotocol/protocolVersion': MODERN,
                'io.modelcontextprotocol/clientCapabilities': {},
                'io.modelcontextprotocol/clientInfo': {'name': 'Pebble', 'version': '0.4.2'}}
            normalized_headers = {key.lower(): value for key, value in headers.items()}
            assert normalized_headers['mcp-method'] == body['method'] and normalized_headers['mcp-protocol-version'] == MODERN
        headers = server[1]['headers'][-1]
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        assert normalized_headers['mcp-name'] == 'lookup'
        assert normalized_headers['mcp-param-query'] == '=?base64?aMOpbGxvCndvcmxk?='
        assert normalized_headers['mcp-param-enabled'] == 'true'
        assert server[1]['calls'][-1]['params']['arguments']['query'] == 'héllo\nworld'
    finally:
        instance.close()
    assert effect_count(server[1]) == 1 and server[1]['deletes'] == 0


@pytest.mark.parametrize('value', [7, 'scalar string', True, None, [1, {'nested': 'value'}]])
def test_modern_structured_json_scalars_arrays_and_null_keep_presence(server, value):
    server[1]['value'] = value
    instance = client(server)
    try:
        result = instance.call('lookup', {'query': 'q'})
    finally:
        instance.close()
    assert result['untrusted_structured'] == value and result['has_structured'] is True
    assert result['authorization'] == 'none' and result['untrusted_plugin_output'] == 'fixture output'
    assert effect_count(server[1]) == 1


def test_modern_output_schema_is_checked_with_run_owned_route(extensions, server):
    identifier, digest = install(extensions, server)
    extensions.load_plugin_tool(identifier, 'lookup', digest, 'run-a')
    payload = extensions.plugin_payload(identifier, 'lookup', '{"query":"q"}', 'run-a', digest)
    result = extensions.plugin_call(payload, 'run-a')
    assert result['untrusted_structured'] == 7 and result['route_digest'] == digest and result['protocol'] == MODERN
    server[1]['value'] = 'wrong scalar'
    with pytest.raises(MCPUncertain, match='output schema'):
        extensions.plugin_call(payload, 'run-a')
    assert effect_count(server[1]) == 2 and not extensions.clients


def test_missing_structured_output_fails_even_for_nullable_schema(extensions, server):
    server[1]['specs'][0]['outputSchema'] = {'type': ['integer', 'null']}
    identifier, digest = install(extensions, server)
    payload = extensions.plugin_payload(identifier, 'lookup', '{"query":"q"}')
    server[1]['omit_structured'] = True
    with pytest.raises(MCPUncertain, match='output schema'):
        extensions.plugin_call(payload)
    assert effect_count(server[1]) == 1 and not extensions.clients


@pytest.mark.parametrize('result_type', [None, 'input_required', 'task', 'unknown', True])
def test_dispatched_modern_missing_or_extension_result_is_uncertain_without_replay(server, result_type):
    instance = client(server)
    instance.tools()
    server[1]['result_type'] = result_type
    with pytest.raises(MCPUncertain, match='unknown remote outcome'):
        instance.call('lookup', {'query': 'q'})
    instance.close()
    methods = [b['method'] for b in server[1]['calls']]
    assert effect_count(server[1]) == 1
    assert 'initialize' not in methods and 'notifications/cancelled' not in methods
    assert not any(method.startswith('tasks/') for method in methods)
    assert server[1]['calls'][-1]['params']['_meta']['io.modelcontextprotocol/clientCapabilities'] == {}
    assert server[1]['deletes'] == 0


@pytest.mark.parametrize('catalogue', [{}, {'ttlMs': 100}, {'cacheScope': 'private'},
    {'ttlMs': True, 'cacheScope': 'private'}, {'ttlMs': -1, 'cacheScope': 'private'},
    {'ttlMs': 604800001, 'cacheScope': 'public'}, {'ttlMs': 1, 'cacheScope': 'unknown'}])
def test_modern_catalogue_cache_metadata_required_before_effect(server, catalogue):
    server[1]['catalogue'] = catalogue
    instance = client(server)
    try:
        with pytest.raises(PolicyError, match='cache metadata'):
            instance.tools()
    finally:
        instance.close()
    assert effect_count(server[1]) == 0


@pytest.mark.parametrize('cache', [{}, {'ttlMs': 1}, {'cacheScope': 'private'},
    {'ttlMs': True, 'cacheScope': 'private'}, {'ttlMs': -1, 'cacheScope': 'private'},
    {'ttlMs': 604800001, 'cacheScope': 'public'}, {'ttlMs': 1, 'cacheScope': 'unknown'}])
def test_modern_discovery_cache_metadata_required_without_fallback(server, cache):
    server[1]['discovery_cache'] = cache
    instance = client(server)
    try:
        with pytest.raises(PolicyError, match='cache metadata'):
            instance.tools()
    finally:
        instance.close()
    assert [b['method'] for b in server[1]['calls']] == ['server/discover']


@pytest.mark.parametrize('cache', [{}, {'ttlMs': 1}, {'cacheScope': 'private'},
    {'ttlMs': True, 'cacheScope': 'private'}, {'ttlMs': -1, 'cacheScope': 'private'},
    {'ttlMs': 604800001, 'cacheScope': 'public'}, {'ttlMs': 1, 'cacheScope': 'unknown'}])
def test_modern_resource_read_bad_cache_is_uncertain_and_never_replayed(server, cache):
    server[1]['resource_cache'] = cache
    instance = client(server)
    try:
        instance.tools()
        with pytest.raises(MCPUncertain, match='unknown remote outcome'):
            instance.request('resources/read', {'uri': 'fixture://selected'})
    finally:
        instance.close()
    methods = [b['method'] for b in server[1]['calls']]
    assert methods.count('resources/read') == 1 and 'initialize' not in methods
    assert 'notifications/cancelled' not in methods


def test_modern_resource_read_valid_cache_and_untrusted_name_header(server):
    instance = client(server)
    try:
        instance.tools()
        result = instance.request('resources/read', {'uri': 'fixture://selected'})
        assert result['resultType'] == 'complete' and result['cacheScope'] == 'private'
        assert result['ttlMs'] == 5000
        headers = {key.lower(): value for key, value in server[1]['headers'][-1].items()}
        assert headers['mcp-method'] == 'resources/read' and headers['mcp-name'] == 'fixture://selected'
    finally:
        instance.close()


@pytest.mark.parametrize('method,params', [('resources/read', {'uri': 'fixture://selected'}),
    ('prompts/get', {'name': 'summary', 'arguments': {}})])
def test_modern_data_input_required_never_becomes_host_question_or_replay(server, method, params):
    instance = client(server)
    try:
        instance.tools()
        server[1]['result_type'] = 'input_required'
        with pytest.raises(MCPUncertain):
            instance.request(method, params)
    finally:
        instance.close()
    methods = [b['method'] for b in server[1]['calls']]
    assert methods.count(method) == 1 and 'initialize' not in methods
    assert not any(name.startswith('elicitation') or name.startswith('sampling') for name in methods)


@pytest.mark.parametrize('change', ['missing_type', 'input_required', 'wrong_version'])
def test_modern_discovery_never_falls_back_to_legacy(server, change):
    if change == 'missing_type':
        server[1]['discovery_result_type'] = None
    elif change == 'input_required':
        server[1]['discovery_result_type'] = 'input_required'
    else:
        server[1]['versions'] = ['2025-11-25']
    instance = client(server)
    try:
        with pytest.raises(PolicyError):
            instance.tools()
    finally:
        instance.close()
    assert [b['method'] for b in server[1]['calls']] == ['server/discover']


def test_modern_parameter_annotation_drift_is_blocked_before_call(extensions, server):
    identifier, digest = install(extensions, server)
    payload = extensions.plugin_payload(identifier, 'lookup', '{"query":"q"}')
    server[1]['specs'][0]['inputSchema']['properties']['query']['x-mcp-header'] = 'Changed'
    with pytest.raises(PolicyError, match='drifted'):
        extensions.plugin_call(payload)
    assert effect_count(server[1]) == 0 and not extensions.clients
    assert extensions.get(identifier)['config']['catalogue_digest'] == digest


def test_modern_call_requires_pinned_schema_before_http_dispatch(server):
    instance = MCP(config={'transport': 'http', 'url': server[0], 'protocol_era': 'modern'})
    try:
        instance.tools()
        with pytest.raises(PolicyError, match='reviewed full schema'):
            instance.call('lookup', {'query': 'q'})
    finally:
        instance.close()
    assert effect_count(server[1]) == 0


def test_modern_stateless_ignores_server_session_without_delete(server):
    server[1]['session'] = True
    instance = client(server)
    try:
        instance.tools()
        instance.tools()
        instance.call('lookup', {'query': 'q'})
        assert instance.session is None
    finally:
        instance.close()
    assert sum(b['method'] == 'server/discover' for b in server[1]['calls']) == 1
    assert not any('MCP-Session-Id' in headers for headers in server[1]['headers'])
    assert server[1]['deletes'] == 0


def test_default_legacy_handshake_and_object_structured_output_preserved(server):
    server[1]['value'] = {'count': 7}
    instance = MCP(server[0])
    try:
        instance.tools()
        result = instance.call('lookup', {'query': 'q'})
        assert instance.protocol == '2025-11-25' and not instance.modern
    finally:
        instance.close()
    methods = [b['method'] for b in server[1]['calls']]
    assert methods == ['initialize', 'notifications/initialized', 'tools/list', 'tools/call']
    assert result['untrusted_structured'] == {'count': 7}
    assert not any('_meta' in b['params'] for b in server[1]['calls'])
    assert not any('Mcp-Method' in h or 'Mcp-Name' in h or 'Mcp-Param-Query' in h for h in server[1]['headers'])


def test_legacy_scalar_response_still_fails_without_modern_opt_in(server):
    instance = MCP(server[0])
    try:
        with pytest.raises(MCPUncertain, match='Legacy structured MCP output'):
            instance.call('lookup', {'query': 'q'})
    finally:
        instance.close()
    assert effect_count(server[1]) == 1 and not any(b['method'] == 'server/discover' for b in server[1]['calls'])


def test_explicit_protocol_mode_is_digest_pinned_and_invalid_values_rejected(extensions, server):
    with pytest.raises(ValueError, match='protocol mode'):
        extensions.add('plugin', 'Bad era', '{"tools":["lookup"],"protocol_era":"auto"}', server[0])
    identifier, _ = install(extensions, server)
    config = extensions.get(identifier)['config']
    extensions.store.query('UPDATE extensions SET config=? WHERE id=?', (json.dumps({**config, 'protocol_era': 'legacy'}), identifier))
    with pytest.raises(PolicyError, match='fresh native review'):
        extensions.checked_plugin(identifier)
    assert effect_count(server[1]) == 0
