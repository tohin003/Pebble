"""Phase 16 F2 probe: a confined stdio MCP server that reports what its environment and network allow.

Launched only by scripts/validate_phase16.py, as a reviewed local MCP server under
Pebble's stdio sandbox, with the system Python (-s -B). Standard library only.

argv: <credential variable> <base-URL variable, or - for an environment grant> <loopback port to try, or 0>

Its one tool, probe, reports facts without content: the environment variable
names, whether the credential value equals the route placeholder, the base URL,
and the broker's HTTP status for an unknown route, a wrong method and a path
outside the reviewed prefix (none of which reaches upstream). With an
environment grant (no base URL) it echoes the credential value, so the host's
redaction proves the real value arrived; with a route it never echoes it.
"""

import http.client
import json
import os
import resource
import socket
import sys
from urllib.parse import urlsplit

NAME, BASE_NAME, DIRECT = sys.argv[1], sys.argv[2], int(sys.argv[3])
PLACEHOLDER = 'pebble-route-placeholder'
SPEC = {'name': 'probe', 'description': 'Report the Phase 16 F2 probe facts.',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}


def status(base, method, path):
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        # In route mode this header carries the placeholder; the broker drops it.
        connection.request(method, path, headers={'Authorization': 'Bearer ' + os.environ.get(NAME, '')})
        response = connection.getresponse()
        response.read(4096)
        return response.status
    except Exception as exc:
        return type(exc).__name__
    finally:
        connection.close()


def facts():
    value = os.environ.get(NAME)
    base = os.environ.get(BASE_NAME) if BASE_NAME != '-' else None
    out = {'names': sorted(os.environ), 'credential_present': value is not None,
           'credential_is_placeholder': value == PLACEHOLDER, 'base': base,
           'core_limit': list(resource.getrlimit(resource.RLIMIT_CORE)), 'pid': os.getpid()}
    if BASE_NAME == '-':
        out['credential_echo'] = value  # environment grant: the host must redact this echo
    if base:
        path = urlsplit(base).path
        out['unknown_route'] = status(base, 'GET', '/r/phase16-unknown/v1/items')
        out['wrong_method'] = status(base, 'DELETE', path + '/v1/items')
        out['outside_prefix'] = status(base, 'GET', path + '/admin')
    if DIRECT:
        try:
            socket.create_connection(('127.0.0.1', DIRECT), timeout=3).close()
            out['direct'] = 'connected'
        except OSError as exc:
            out['direct'] = exc.errno
    return out


for line in sys.stdin:
    try:
        body = json.loads(line)
    except ValueError:
        continue
    if not isinstance(body, dict) or 'id' not in body:
        continue
    if body.get('method') == 'initialize':
        result = {'protocolVersion': '2025-11-25', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'phase16-stdio-probe', 'version': '1.0.0'}}
    elif body.get('method') == 'tools/list':
        result = {'tools': [SPEC]}
    elif body.get('method') == 'tools/call':
        result = {'content': [{'type': 'text', 'text': json.dumps(facts(), sort_keys=True)}], 'isError': False}
    else:
        print(json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32601, 'message': 'unsupported'}}),
              flush=True)
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}), flush=True)
