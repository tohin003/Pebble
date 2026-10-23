"""Compatibility and failure bounds for the real disposable schema worker."""
import copy
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pebble import mcp_transport
from pebble.mcp_schema import header_routes, header_value, inspect, parameter_headers
from pebble.mcp_transport import valid_arguments, valid_schema
from pebble.policy import PolicyError


@pytest.mark.parametrize('schema,good,bad', [
    ({'type': 'string', 'minLength': 2}, 'ok', 'x'),
    ({'type': 'integer', 'minimum': 2, 'maximum': 5}, 3, True),
    ({'type': ['string', 'null']}, None, 1),
    ({'type': 'array', 'prefixItems': [{'const': 'first'}], 'items': {'type': 'integer'}}, ['first', 2], ['first', 'bad']),
    ({'type': 'array', 'contains': {'const': 3}, 'minContains': 1, 'maxContains': 1}, [1, 3], [3, 3]),
    ({'anyOf': [{'type': 'string'}, {'type': 'number'}]}, 1.5, False),
    ({'oneOf': [{'type': 'integer'}, {'minimum': 5}]}, 2, 8),
    ({'allOf': [{'type': 'integer'}, {'not': {'const': 0}}]}, 1, 0),
    ({'type': 'object', 'dependentRequired': {'a': ['b']}}, {'a': 1, 'b': 2}, {'a': 1}),
    ({'if': {'const': 'special'}, 'then': {'maxLength': 6}, 'else': {'type': 'integer'}}, 5, 'special'),
    ({'$defs': {'positive': {'type': 'integer', 'minimum': 1}}, 'type': 'object',
      'properties': {'count': {'$ref': '#/$defs/positive'}}, 'required': ['count'], 'additionalProperties': False}, {'count': 2}, {'count': -1}),
    ({'$schema': 'http://json-schema.org/draft-07/schema#', 'type': 'array',
      'items': [{'type': 'string'}, {'type': 'integer'}], 'additionalItems': False}, ['x', 1], ['x', 1, 2]),
])
def test_real_worker_supported_local_schema_contract(schema, good, bad):
    assert valid_schema(schema)
    assert valid_arguments(schema, good) is True
    assert valid_arguments(schema, bad) is False


@pytest.mark.parametrize('schema,result', [(True, True), (False, False)])
def test_boolean_schemas_are_valid_and_meaningful(schema, result):
    assert valid_schema(schema)
    assert valid_arguments(schema, {'untrusted': 'value'}) is result


@pytest.mark.parametrize('schema', [
    {'$ref': '#'}, {'$id': 'https://unreviewed.example/schema'},
    {'$ref': 'https://unreviewed.example/schema'}, {'$dynamicRef': 'file:///private/secret'},
    {'$schema': 'https://unknown.example/dialect'}, {'pattern': '['}, {'pattern': 'a' * 513},
    {'anyOf': [{}] * 9}, {'patternProperties': {str(n): {} for n in range(17)}},
    {'type': 'never-a-json-type'}, {'properties': []}, {'required': ['same', 'same']},
])
def test_real_worker_invalid_or_unsupported_schema_refused(schema):
    assert valid_schema(schema) is False
    assert valid_arguments(schema, {'value': 1}) is False


def test_reference_validation_cannot_fetch_loopback_network():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"type":"string"}')
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: http.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{http.server_port}/private-schema'
        for schema in ({'$ref': url}, {'properties': {'field': {'$ref': url}}}, {'$dynamicRef': url}):
            assert valid_schema(schema) is False
            assert valid_arguments(schema, {'field': 'value'}) is False
        assert requests == []
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def test_schema_byte_complexity_and_alternative_bounds():
    with pytest.raises(PolicyError, match='byte boundary'):
        inspect({'description': 'x' * 20001})
    schema = {}
    for _ in range(21):
        schema = {'properties': {'nested': schema}}
    with pytest.raises(PolicyError, match='complexity'):
        inspect(schema)
    with pytest.raises(PolicyError, match='complexity'):
        inspect({'enum': list(range(1001))})
    with pytest.raises(PolicyError, match='alternatives'):
        inspect({'oneOf': [{}] * 9})
    with pytest.raises(PolicyError, match='isolated budget'):
        valid_arguments(True, 'x' * 48001)


def test_actual_regex_timeout_is_bounded_and_worker_reaped(monkeypatch):
    processes = []
    launch = mcp_transport.subprocess.Popen
    def record(*args, **kwargs):
        process = launch(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(mcp_transport.subprocess, 'Popen', record)
    started = time.monotonic()
    with pytest.raises(PolicyError, match='isolated budget'):
        valid_arguments({'type': 'string', 'pattern': '^(a+)+$'}, 'a' * 12000 + '!')
    assert 1.5 <= time.monotonic() - started < 5
    assert processes and processes[0].poll() is not None


def test_recursive_local_reference_cannot_exceed_isolated_budget():
    schema = {'$defs': {'loop': {'$ref': '#/$defs/loop'}}, '$ref': '#/$defs/loop'}
    assert valid_schema(schema)
    started = time.monotonic()
    try:
        result = valid_arguments(schema, {'value': 1})
    except PolicyError:
        result = False
    assert result is False and time.monotonic() - started < 5


def test_cancel_before_schema_worker_launch(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(mcp_transport.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Cancelled validation launched a worker'))
    with pytest.raises(PolicyError, match='isolated budget'):
        valid_arguments(True, {'value': 1}, cancel)


def test_schema_worker_gets_private_environment_without_inherited_secrets(monkeypatch):
    monkeypatch.setenv('MCP_SCHEMA_TEST_TOKEN', 'synthetic-sentinel')
    monkeypatch.setenv('HTTPS_PROXY', 'http://invalid.fixture')
    launch = mcp_transport.subprocess.Popen
    seen = []
    def record(*args, **kwargs):
        assert 'MCP_SCHEMA_TEST_TOKEN' not in kwargs['env']
        assert 'HTTPS_PROXY' not in kwargs['env'] and 'PYTHONPATH' not in kwargs['env']
        assert kwargs['start_new_session']
        seen.append(kwargs['cwd'])
        return launch(*args, **kwargs)
    monkeypatch.setattr(mcp_transport.subprocess, 'Popen', record)
    assert valid_arguments({'type': 'string'}, 'safe')
    assert seen


def test_static_parameter_header_paths_and_primitive_encoding():
    schema = {'type': 'object', 'properties': {
        'query': {'type': 'string', 'x-mcp-header': 'Query'},
        'nested': {'type': 'object', 'properties': {'enabled': {'type': 'boolean', 'x-mcp-header': 'Enabled'}}},
        'optional': {'type': 'integer', 'x-mcp-header': 'Count'}}}
    before = copy.deepcopy(schema)
    assert header_routes(schema) == {'Query': ('query',), 'Enabled': ('nested', 'enabled'), 'Count': ('optional',)}
    assert parameter_headers(schema, {'query': 'héllo\nworld', 'nested': {'enabled': False}}) == {
        'Mcp-Param-Query': '=?base64?aMOpbGxvCndvcmxk?=', 'Mcp-Param-Enabled': 'false'}
    assert schema == before
    assert parameter_headers(schema, {}) == {}


@pytest.mark.parametrize('value,expected', [('plain', 'plain'), (True, 'true'), (1, '1'),
    ('\r\nInjected: yes', '=?base64?DQpJbmplY3RlZDogeWVz?='), (' padded ', '=?base64?IHBhZGRlZCA=?='),
    ('', '=?base64??='), ('=?base64?x?=', '=?base64?PT9iYXNlNjQ/eD89?=')])
def test_header_encoding_never_emits_control_characters(value, expected):
    assert header_value(value) == expected


@pytest.mark.parametrize('schema', [
    {'properties': {'a': {'type': 'string', 'x-mcp-header': 'bad\r\nheader'}}},
    {'properties': {'a': {'type': 'string', 'x-mcp-header': 'duplicate'}, 'b': {'type': 'integer', 'x-mcp-header': 'Duplicate'}}},
    {'properties': {'a': {'type': 'object', 'x-mcp-header': 'Object'}}},
    {'anyOf': [{'properties': {'a': {'type': 'string', 'x-mcp-header': 'Conditional'}}}]},
    {'$defs': {'a': {'type': 'string', 'x-mcp-header': 'Referenced'}}},
    {'items': {'type': 'string', 'x-mcp-header': 'ArrayItem'}},
])
def test_ambiguous_or_invalid_parameter_header_annotations_refused(schema):
    with pytest.raises(PolicyError):
        header_routes(schema)


@pytest.mark.parametrize('value', [2**53, {'not': 'primitive'}, ['not primitive'], 1.5])
def test_header_parameter_requires_safe_primitive(value):
    with pytest.raises(PolicyError, match='safe primitive'):
        parameter_headers({'properties': {'x': {'type': 'integer', 'x-mcp-header': 'Value'}}}, {'x': value})


@pytest.mark.parametrize('failure', ['ImportError', 'MemoryError', 'OSError'])
def test_a_failed_checker_is_not_a_verdict_on_the_arguments(monkeypatch, failure):
    """Phase 32: the isolated checker failing (import, memory, files) says validation could not complete; it never
    reports valid arguments as not matching the schema."""
    monkeypatch.setattr(mcp_transport, 'isolated_http', lambda *a, **k: {'valid': False, 'checker_failed': failure})
    with pytest.raises(PolicyError, match='could not be validated: the isolated checker failed \\(' + failure + '\\)'):
        valid_arguments({'type': 'string'}, 'safe')
    with pytest.raises(PolicyError, match='MCP schema could not be validated'):
        valid_schema({'type': 'string'})
    monkeypatch.setattr(mcp_transport, 'isolated_http', lambda *a, **k: {'valid': False})
    assert valid_arguments({'type': 'string'}, 1) is False  # a refusal stays a refusal


@pytest.mark.parametrize('error, expected', [(ImportError('jsonschema'), {'valid': False, 'checker_failed': 'ImportError'}),
                                             (MemoryError(), {'valid': False, 'checker_failed': 'MemoryError'}),
                                             (PolicyError('too complex'), {'valid': False}),
                                             (ValueError('bad pattern'), {'valid': False})])
def test_the_worker_tells_a_checker_failure_from_a_refusal(monkeypatch, capsysbinary, error, expected):
    import io, json, sys
    from pebble import mcp_schema

    def broken(*args, **kwargs):
        raise error
    monkeypatch.setattr(mcp_schema, 'worker', broken)
    monkeypatch.setattr(sys, 'stdin', type('In', (), {'buffer': io.BytesIO(json.dumps({'schema': True}).encode())})())
    mcp_transport.schema_worker_main()
    assert json.loads(capsysbinary.readouterr().out) == expected
