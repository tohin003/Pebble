"""F2 option A: the reviewed reverse-proxy route for MCP stdio credentials.

The environment grant keeps its exact behaviour (validate_connector_auth.py
depends on it). The route is a new opt-in kind: the server environment holds a
placeholder and a loopback base URL, the supervisor's in-memory CredentialBroker
and EgressBroker add the real header, and the server profile admits outbound
TCP to the broker port alone. Real confined processes and a loopback TLS
upstream with a fixture CA (test-only loopback/ca_file); no network.
"""
import copy
import ctypes
import json
import os
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pebble.app import App
from pebble.connector_auth import COMPUTED, EXPOSURE, Records, digest
from pebble.mcp_transport import (MCP, PLACEHOLDER, Stdio, base_url_name, python_command, route_spec,
                                  stdio_config)
from pebble.policy import PolicyError

MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
macos = pytest.mark.skipif(not MAC, reason='Confined MCP stdio workers are a macOS sandbox-exec product surface.')
TOKEN = 'route-fixture-' + secrets.token_hex(16)
GOOD = {'name': 'SERVICE_TOKEN', 'base_url_env': 'SERVICE_API_URL', 'origin': 'https://api.example.com',
        'methods': ['GET'], 'path_prefix': '/v1'}


class FakeVault:
    """Records every access, so a refusal can prove the vault was never touched."""
    def __init__(self): self.data, self.calls = {}, []
    def get(self, key): self.calls.append(('get', key)); return self.data.get(key, '')
    def set(self, key, value): self.calls.append(('set', key)); self.data[key] = value
    def delete(self, key): self.calls.append(('delete', key)); self.data.pop(key, None)


@pytest.fixture
def app(tmp_path):
    app = App(tmp_path / 'data', tmp_path / 'resources', lambda *args: None)
    app.connector_auth.records = Records(FakeVault())
    yield app
    app.close()


def save_route(app, **params):
    """The native grant route. App.connector_operation gains 'save_environment_route' in the parent patch."""
    with app.task_lock:
        return app.connector_auth.save_environment_route(**params)


def stdio_plugin(app, tmp_path, server_source='import sys\n', tools=('lookup',)):
    source = tmp_path / 'server.py'
    source.write_text(server_source)
    config = {'transport': 'stdio', 'command': python_command('--unused', framework=True)[0],
              'args': ['-s', '-B', str(source)], 'read_files': [str(source)], 'tools': list(tools)}
    return app.extension_add(kind='plugin', name='Local route fixture', content=json.dumps(config))


ECHO_SERVER = '''import json,sys,os
for line in sys.stdin:
 b=json.loads(line)
 if 'id' not in b:continue
 if b['method']=='initialize':r={'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
 elif b['method']=='tools/list':r={'tools':[{'name':'lookup','inputSchema':{'type':'object'}}]}
 else:r={'content':[{'type':'text','text':os.getenv('API_KEY','missing')+':'+str('OTHER_KEY_SECRET' in os.environ)}]}
 print(json.dumps({'jsonrpc':'2.0','id':b['id'],'result':r}),flush=True)
'''


@macos
def test_environment_grant_is_unchanged_and_labelled(app, tmp_path, monkeypatch):
    monkeypatch.setenv('OTHER_KEY_SECRET', 'foreign')
    ident = stdio_plugin(app, tmp_path, ECHO_SERVER)
    meta = app.connector_operation('save_environment', id=ident, name='API_KEY', token=TOKEN)
    assert meta['kind'] == 'environment' and meta['credential_exposure'] == 'environment_readable_by_local_commands'
    config = app.extensions.get(ident)['config']
    assert app.connector_auth.credential(ident, config) == {'environment': {'API_KEY': TOKEN}}
    app.extension_toggle(ident, True)
    result = app.extensions.plugin_call(app.extensions.plugin_payload(ident, 'lookup', '{}'))
    # The grader's shape: the server reads the real value, and its echo is redacted.
    assert result['untrusted_plugin_output'] == '[connector credential redacted]:False'
    assert TOKEN not in json.dumps(app.extensions.list()) and TOKEN not in json.dumps(meta)


BAD = [
    ('name', 'PATH'), ('name', 'api_key'), ('name', 'SERVICE_URL'), ('name', 'X' * 70 + '_TOKEN'),
    ('base_url_env', 'SERVICE_TOKEN'), ('base_url_env', 'PATH'), ('base_url_env', 'DYLD_INSERT_URL'),
    ('base_url_env', 'PYTHONSTARTUP_URL'), ('base_url_env', 'OTHER_API_KEY'), ('base_url_env', 'service_api_url'),
    ('base_url_env', 'NODE_OPTIONS_URL'), ('base_url_env', 'HTTPS_PROXY'), ('base_url_env', 'SERVICE_ACCOUNT'),
    ('base_url_env', 'A' * 64 + '_URL'), ('base_url_env', None),
    ('origin', 'http://api.example.com'), ('origin', 'https://127.0.0.1'), ('origin', 'https://localhost:8443'),
    ('origin', 'https://api.localhost'), ('origin', 'https://user@api.example.com'), ('origin', 'https://api.example.com/v1'),
    ('origin', 'https://API.example.com'), ('origin', 'https://xn--bcher-kva.example'), ('origin', 'https://[::1]'),
    ('origin', 'https://api.example.com:0'), ('origin', 7),
    ('methods', ['CONNECT']), ('methods', []), ('methods', ['GET', 'GET']), ('methods', 'GET'), ('methods', ['TRACE']),
    ('path_prefix', 'v1'), ('path_prefix', '/v1/../admin'), ('path_prefix', '/v1?x=1'), ('path_prefix', '/v1/%2e%2e'),
    ('path_prefix', ''), ('path_prefix', '/v1//x'),
    ('header', 'Host'), ('header', 'Content-Length'), ('header', 'Proxy-Authorization'), ('header', 'Bad Header'),
    ('header', ''), ('scheme', 'Bearer token'), ('scheme', ''), ('scheme', 7),
    ('token', 'has space'), ('token', ''), ('token', 'x' * 8193), ('token', 'line\nbreak'), ('token', None),
]


@macos
@pytest.mark.parametrize('field,value', BAD, ids=[f'{f}-{i}' for i, (f, _) in enumerate(BAD)])
def test_bad_route_shapes_are_refused_before_the_vault(app, tmp_path, field, value):
    ident = stdio_plugin(app, tmp_path)
    vault = app.connector_auth.records.vault
    params = {**GOOD, 'token': TOKEN, field: value}
    with pytest.raises(PolicyError):
        save_route(app, id=ident, **params)
    assert vault.calls == [] and not app.store.query('SELECT * FROM connector_grants')
    assert app.connector_auth.metadata(ident)['state'] == 'disconnected'
    # Positive control: the unchanged good shape is accepted, so each refusal above is the changed field.
    assert save_route(app, id=ident, **GOOD, token=TOKEN)['kind'] == 'environment_route' and vault.calls


def test_route_requires_a_local_stdio_plugin(app):
    ident = app.extension_add(kind='plugin', name='Remote', content='{"tools":["lookup"]}', url='https://mcp.example.com/mcp')
    with pytest.raises(PolicyError, match='Local grants'):
        save_route(app, id=ident, token=TOKEN, **GOOD)
    assert app.connector_auth.records.vault.calls == []


@macos
def test_route_grant_digest_is_stable_and_route_edits_force_a_reconnect(app, tmp_path):
    ident = stdio_plugin(app, tmp_path)
    meta = save_route(app, id=ident, token=TOKEN, **GOOD)
    route = {'base_url_env': 'SERVICE_API_URL', 'origin': 'https://api.example.com:443', 'methods': ['GET'],
             'path_prefix': '/v1', 'header': 'Authorization', 'scheme': 'Bearer'}
    assert meta['kind'] == 'environment_route' and meta['credential_exposure'] == 'reviewed_route_placeholder_only'
    assert meta['resource'] == route['origin'] and meta['endpoints'] == {'credential_route': route}
    assert meta['environment_names'] == ['SERVICE_TOKEN'] and not app.extensions.get(ident)['enabled']
    config = app.extensions.get(ident)['config']
    assert app.connector_auth.credential(ident, config) == {'environment': {'SERVICE_TOKEN': TOKEN}, 'route': route}
    assert app.connector_auth.credential(ident, config) == app.connector_auth.credential(ident, config)
    stored = json.loads(app.store.query('SELECT metadata FROM connector_grants')[0]['metadata'])
    assert set(stored) == {'kind', 'resource', 'issuer', 'client_id', 'scopes', 'environment_names', 'endpoints', 'base_digest'}
    assert config['auth_binding']['digest'] == digest(stored)
    assert TOKEN not in (app.home / 'pebble.sqlite').read_bytes().decode(errors='ignore') and TOKEN not in json.dumps(meta)
    # Editing the stored route breaks the reviewed digest: the old binding needs a reconnect.
    edited = copy.deepcopy(stored)
    edited['endpoints']['credential_route']['path_prefix'] = '/'
    app.store.query('UPDATE connector_grants SET metadata=?', (json.dumps(edited),))
    with pytest.raises(PolicyError, match='reconnect'):
        app.connector_auth.credential(ident, config)
    app.store.query('UPDATE connector_grants SET metadata=?', (json.dumps(stored),))
    # A reviewed route change is a new grant: a new revision, the plugin disabled for review, the old binding refused.
    changed = save_route(app, id=ident, token=TOKEN, **{**GOOD, 'path_prefix': '/v2'})
    assert changed['revision'] == meta['revision'] + 1 and changed['grant_id'] != meta['grant_id']
    assert not app.extensions.get(ident)['enabled']
    with pytest.raises(PolicyError):
        app.connector_auth.credential(ident, config)
    assert app.connector_auth.credential(ident, app.extensions.get(ident)['config'])['route']['path_prefix'] == '/v2'
    app.connector_operation('disconnect', id=ident)
    assert not app.connector_auth.records.vault.data


@macos
def test_exposure_label_is_computed_and_leaves_existing_grants_working(app, tmp_path):
    assert COMPUTED >= {'credential_exposure', 'plugin_id', 'state', 'grant_id', 'revision', 'storage_cleanup_pending'}
    ident = stdio_plugin(app, tmp_path)
    app.connector_operation('save_environment', id=ident, name='API_KEY', token=TOKEN)
    config = app.extensions.get(ident)['config']
    stored = json.loads(app.store.query('SELECT metadata FROM connector_grants')[0]['metadata'])
    # A grant written before the label existed has exactly this stored shape and digest.
    assert 'credential_exposure' not in stored and config['auth_binding']['digest'] == digest(stored)
    meta = app.connector_auth.metadata(ident)
    assert digest({k: v for k, v in meta.items() if k not in COMPUTED}) == config['auth_binding']['digest']
    assert digest({k: v for k, v in meta.items() if k not in COMPUTED - {'credential_exposure'}}) != config['auth_binding']['digest']
    assert app.connector_auth.credential(ident, config) == {'environment': {'API_KEY': TOKEN}}
    assert EXPOSURE == {'environment': 'environment_readable_by_local_commands',
                        'environment_route': 'reviewed_route_placeholder_only', 'bearer': 'host_only', 'oauth': 'host_only'}


def test_route_spec_rules():
    good = {'base_url_env': 'SERVICE_API_URL', 'origin': 'https://api.example.com', 'methods': ['POST', 'GET'],
            'path_prefix': '/v1', 'header': 'X-Api-Key', 'scheme': None}
    spec = route_spec(good, {'SERVICE_TOKEN': ''})
    assert spec == {**good, 'origin': 'https://api.example.com:443', 'methods': ['GET', 'POST']}
    assert route_spec(spec, {'SERVICE_TOKEN': ''}) == spec  # normalized routes are stable
    for environment in ({}, {'SERVICE_TOKEN': '', 'OTHER_TOKEN': ''}, {'PATH': ''}, []):
        with pytest.raises(PolicyError):
            route_spec(good, environment)
    for change in ({'extra': 1}, {'origin': 'https://localhost:9'}):
        with pytest.raises(PolicyError):
            route_spec({**good, **change}, {'SERVICE_TOKEN': ''})
    # Loopback is only a test fixture switch, as async_terminal's.
    assert route_spec({**good, 'origin': 'https://localhost:9'}, {'SERVICE_TOKEN': ''}, loopback=True)['origin'] == 'https://localhost:9'
    assert base_url_name('GITHUB_API_URL', 'GITHUB_TOKEN') == 'GITHUB_API_URL'


class FakeAuth:
    def __init__(self, grant): self.grant = grant
    def credential(self, plugin_id, config, cancel=None): return self.grant


@macos
def test_mcp_hands_the_grant_route_to_its_stdio_transport(tmp_path):
    source = tmp_path / 'server.py'
    source.write_text('import sys\n')
    config = stdio_config({'transport': 'stdio', 'tools': ['lookup'], 'command': python_command('--unused', framework=True)[0],
                           'args': ['-s', '-B', str(source)], 'read_files': [str(source)]})
    route = route_spec({**{k: GOOD[k] for k in ('base_url_env', 'origin', 'methods', 'path_prefix')},
                        'header': 'Authorization', 'scheme': 'Bearer'}, {'SERVICE_TOKEN': ''})
    client = MCP(config=config, auth=FakeAuth({'environment': {'SERVICE_TOKEN': TOKEN}, 'route': route}), plugin_id='p')
    assert client.stdio.route == route and client.stdio.environment == {'SERVICE_TOKEN': TOKEN}
    assert client.stdio.broker_port is None and client.stdio.loopback is False and client.stdio.proc is None
    legacy = MCP(config=config, auth=FakeAuth({'environment': {'SERVICE_TOKEN': TOKEN}}), plugin_id='p')
    assert legacy.stdio.route is None


# The supervisor, end to end

@pytest.fixture(scope='module')
def tls(tmp_path_factory):
    folder = tmp_path_factory.mktemp('route-tls')
    (folder / 'ca.cnf').write_text('[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = Pebble T5 Fixture CA\n'
                                   '[v3_ca]\nbasicConstraints = critical,CA:TRUE\nkeyUsage = critical,keyCertSign,cRLSign\n'
                                   'subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid:always\n')
    (folder / 'leaf.cnf').write_text('[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = localhost\n'
                                     '[v3_leaf]\nbasicConstraints = critical,CA:FALSE\n'
                                     'keyUsage = critical,digitalSignature,keyEncipherment\nextendedKeyUsage = serverAuth\n'
                                     'subjectAltName = DNS:localhost,IP:127.0.0.1\nsubjectKeyIdentifier = hash\n'
                                     'authorityKeyIdentifier = keyid\n')
    openssl = '/usr/bin/openssl'
    for argv in ([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '2', '-keyout', 'ca.key',
                  '-out', 'ca.pem', '-config', 'ca.cnf', '-extensions', 'v3_ca'],
                 [openssl, 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'leaf.key', '-out', 'leaf.csr',
                  '-config', 'leaf.cnf'],
                 [openssl, 'x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-set_serial', '5151',
                  '-days', '2', '-sha256', '-extfile', 'leaf.cnf', '-extensions', 'v3_leaf', '-out', 'leaf.pem']):
        result = subprocess.run(argv, cwd=folder, capture_output=True, text=True, timeout=60,
                                env={'PATH': '/usr/bin:/bin', 'HOME': str(folder), 'RANDFILE': str(folder / '.rnd')})
        assert result.returncode == 0, result.stderr
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / 'leaf.pem', folder / 'leaf.key')
    return {'ca': str(folder / 'ca.pem'), 'context': context}


@pytest.fixture
def upstream(tls):
    """A loopback HTTPS API that accepts only the real token. /v1/echo reflects the credential header."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_GET(self):
            seen.append({'path': self.path, 'authorization': self.headers.get_all('Authorization') or []})
            if self.path == '/v1/echo':
                status, body = 200, (self.headers.get('Authorization') or '').encode()
            elif self.headers.get('Authorization') != 'Bearer ' + TOKEN:
                status, body = 401, b'{}'
            else:
                status, body = 200, b'{"items": [1, 2]}'
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    http.daemon_threads = True
    http.socket = tls['context'].wrap_socket(http.socket, server_side=True)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield http.server_address[1], seen
    http.shutdown()
    http.server_close()


PROBE_SERVER = '''import json,os,sys,socket,resource
import http.client
from urllib.parse import urlsplit
BASE=os.environ.get('SERVICE_API_URL','')
def request(method,path):
    p=urlsplit(BASE)
    c=http.client.HTTPConnection(p.hostname,p.port,timeout=10)
    try:
        c.request(method,path,headers={'Authorization':'Bearer '+os.environ.get('SERVICE_TOKEN','missing')})
        r=c.getresponse()
        return [r.status,r.read().decode('latin-1')]
    except Exception as e:
        return [0,type(e).__name__]
    finally:
        c.close()
for line in sys.stdin:
    b=json.loads(line)
    if 'id' not in b:continue
    if b['method']=='initialize':r={'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
    elif b['method']=='tools/list':r={'tools':[{'name':'probe','inputSchema':{'type':'object'}}]}
    else:
        base=urlsplit(BASE).path
        out={'fetch':request('GET',base+'/v1/items'),'unknown_route':request('GET','/r/other/v1/items'),
             'wrong_method':request('DELETE',base+'/v1/items'),'outside_prefix':request('GET',base+'/admin'),
             'reflected':request('GET',base+'/v1/echo'),
             'token':os.environ.get('SERVICE_TOKEN'),'base':BASE,'names':sorted(os.environ),
             'core':list(resource.getrlimit(resource.RLIMIT_CORE)),'pid':os.getpid(),'ppid':os.getppid()}
        try:
            socket.create_connection(('127.0.0.1',UPSTREAM),timeout=3).close();out['direct']='connected'
        except OSError as e:
            out['direct']=e.errno
        r={'content':[{'type':'text','text':json.dumps(out)}]}
    print(json.dumps({'jsonrpc':'2.0','id':b['id'],'result':r}),flush=True)
'''


def procargs(pid):
    """The initial argv and environment any same-user process can read (KERN_PROCARGS2)."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                            ctypes.c_void_p, ctypes.c_size_t]
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(262144)
    buffer = ctypes.create_string_buffer(262144)
    assert libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) == 0, ctypes.get_errno()
    return buffer.raw[:size.value]


def files_holding(root, needle):
    found = []
    for base, _, names in os.walk(root):
        for name in names:
            path = Path(base) / name
            try:
                if needle in path.read_bytes():
                    found.append(str(path))
            except OSError:
                pass
    return found


def routed_client(tmp_path, port, ca, route_change=None, server=PROBE_SERVER, loopback=True):
    source = tmp_path / 'server.py'
    source.write_text(server.replace('UPSTREAM', str(port)))
    config = stdio_config({'transport': 'stdio', 'tools': ['probe'], 'command': python_command('--unused', framework=True)[0],
                           'args': ['-s', '-B', str(source)], 'read_files': [str(source)]})
    route = {'base_url_env': 'SERVICE_API_URL', 'origin': f'https://localhost:{port}', 'methods': ['GET'],
             'path_prefix': '/v1', 'header': 'Authorization', 'scheme': 'Bearer', **(route_change or {})}
    client = MCP(config=config, timeout=30)
    # Test-only fixtures reach the loopback upstream with the fixture CA (as async_terminal's loopback/ca_file).
    client.stdio = Stdio(config, environment={'SERVICE_TOKEN': TOKEN}, route=route, loopback=loopback, ca_file=ca)
    return client


@macos
def test_supervisor_routes_the_credential_and_the_server_sees_only_a_placeholder(tmp_path, tls, upstream, monkeypatch):
    port, seen = upstream
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setattr(tempfile, 'tempdir', str(scratch))  # Both stdio folders live under tmp_path.
    client = routed_client(tmp_path, port, tls['ca'])
    token = TOKEN.encode()
    try:
        client.tools()
        out = json.loads(client.call('probe', {})['untrusted_plugin_output'])
        supervisor = client.stdio.proc.pid
        server_args, supervisor_args = procargs(out['pid']), procargs(supervisor)
        held = files_holding(tmp_path, token)
    finally:
        client.close()
    # A confined server fetches through the route from the loopback TLS upstream.
    assert out['fetch'] == [200, '{"items": [1, 2]}']
    assert [r['authorization'] for r in seen if r['path'] == '/v1/items'] == [['Bearer ' + TOKEN]]
    assert all(PLACEHOLDER not in ' '.join(r['authorization']) for r in seen)
    # The broker refuses without reaching upstream: unknown route, wrong method, outside the prefix.
    assert out['unknown_route'][0] == 404 and out['wrong_method'][0] == 405 and out['outside_prefix'][0] == 403
    assert [r['path'] for r in seen].count('/admin') == 0
    # An upstream that reflects the credential in its body is cut off before any of it reaches the server.
    assert out['reflected'] == [0, 'IncompleteRead'] and TOKEN not in json.dumps(out)
    # The server environment holds only the placeholder and the base URL; it cannot reach upstream itself.
    assert out['token'] == PLACEHOLDER and out['base'].startswith('http://127.0.0.1:') and out['base'].endswith('/r/api')
    assert 'SERVICE_TOKEN' in out['names'] and 'SERVICE_API_URL' in out['names']
    assert out['direct'] == 1  # EPERM from the profile: only the broker port is reachable.
    assert out['core'] == [0, 0] and out['ppid'] == supervisor
    # KERN_PROCARGS2 of the server shows the placeholder, never the token; the supervisor got it over stdin.
    assert PLACEHOLDER.encode() in server_args and b'SERVICE_API_URL=' in server_args and token not in server_args
    assert b'--mcp-stdio-worker' in supervisor_args and token not in supervisor_args
    assert held == [] and files_holding(tmp_path, token) == []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(out['pid'], 0)
        except ProcessLookupError:
            break
        time.sleep(.05)
    else:
        raise AssertionError('The routed server outlived its supervisor.')


@macos
def test_backend_eof_tears_down_server_then_broker(tmp_path, tls, upstream):
    port, seen = upstream
    client = routed_client(tmp_path, port, tls['ca'])
    try:
        client.tools()
        out = json.loads(client.call('probe', {})['untrusted_plugin_output'])
        broker_port = int(out['base'].split(':')[2].split('/')[0])
        supervisor = client.stdio.proc
        supervisor.stdin.close()  # What an abrupt backend exit does to the owned pipe.
        assert supervisor.wait(timeout=10) == 0
        with pytest.raises(ProcessLookupError):
            os.kill(out['pid'], 0)
        with pytest.raises(OSError):
            socket.create_connection(('127.0.0.1', broker_port), timeout=2).close()
    finally:
        client.close()


@macos
@pytest.mark.parametrize('change', [{'base_url_env': 'PATH'}, {'origin': 'http://localhost:1'}, {'methods': ['CONNECT']},
                                    {'header': 'Host'}, {'extra': True}, 'no_loopback', 'ca_without_loopback'])
def test_supervisor_refuses_a_malformed_route_before_starting_the_server(tmp_path, tls, upstream, change):
    port, seen = upstream
    client = routed_client(tmp_path, port, tls['ca'], route_change=change if isinstance(change, dict) else None,
                           loopback=change not in ('no_loopback', 'ca_without_loopback'))
    if change == 'no_loopback':
        client.stdio.ca_file = None  # A loopback origin without the test switch is refused like any loopback.
    try:
        with pytest.raises(PolicyError):
            client.tools()
    finally:
        client.close()
    assert seen == []


@macos
def test_server_that_ignores_the_base_url_fails_visibly(tmp_path, tls, upstream):
    port, seen = upstream
    ignoring = PROBE_SERVER.replace("BASE=os.environ.get('SERVICE_API_URL','')", "BASE='https://localhost:UPSTREAM'")
    client = routed_client(tmp_path, port, tls['ca'], server=ignoring)
    try:
        client.tools()
        out = json.loads(client.call('probe', {})['untrusted_plugin_output'])
    finally:
        client.close()
    assert out['fetch'][0] == 0 and out['direct'] == 1 and seen == []
