"""Bounded, owned MCP transports. Discovery is never an authority grant."""
import hashlib
import http.client
import ipaddress
import json
import os
import re
import selectors
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from urllib.parse import urlencode

import certifi
# The MCP clientInfo version is a protocol identity pinned by the historical Phase 6 and Phase 16 contracts
# (validate_phase6.py's fixture server answers anything else with HTTP 500). It stays at the 0.4.2 contract value while
# the app version (pebble.VERSION) moves; changing it needs the person's decision on those historical graders.
MCP_CLIENT_VERSION = '0.4.2'
from .policy import PolicyError
from .providers import NoRedirect

VERSIONS = {'2025-11-25', '2025-06-18', '2025-03-26'}
FRAME = 500_000
MODERN = '2026-07-28'
# F2 option A: an opt-in reviewed route. The server environment holds a placeholder
# and a loopback base URL; the supervisor's in-memory broker adds the real header.
ENV_NAME = re.compile(r'[A-Z][A-Z0-9_]{0,63}')
SECRET_SUFFIXES = ('TOKEN', 'API_KEY', 'SECRET', 'PASSWORD')
URL_SUFFIXES = ('URL', 'URI', 'BASE', 'ENDPOINT', 'HOST', 'ORIGIN', 'SERVER', 'API')
RUNTIME_NAMES = frozenset({'PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'SHELL', 'USER', 'LOGNAME', 'PWD', 'IFS',
                           'ENV', 'TERM', 'EDITOR', 'ZDOTDIR'})
RUNTIME_PREFIXES = ('DYLD_', 'LD_', 'PYTHON', 'NODE_', 'NPM_', 'RUBY', 'GEM_', 'PERL', 'JAVA_', 'JDK_', 'BASH_', 'LC_',
                    'SSL_', 'REQUESTS_', 'CURL_', 'GIT_', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                    'PEBBLE_', 'SANDBOX', 'MALLOC')
ROUTE_KEYS = frozenset({'base_url_env', 'origin', 'methods', 'path_prefix', 'header', 'scheme'})
PLACEHOLDER = 'pebble-route-placeholder'
ROUTE_SECONDS = 300  # the supervisor's own deadline


class MCPUncertain(RuntimeError):
    """The remote tool may have executed. The host must reconcile, never replay."""


class MCPNotDispatched(PolicyError):
    """Cancellation or rejection before a tool request was sent; no effect occurred."""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()


def endpoint(url):
    try:
        p = urlsplit(url)
        port = p.port
    except (TypeError, ValueError):
        raise ValueError('Enter a valid MCP endpoint.') from None
    if (not isinstance(url, str) or len(url) > 2000 or any(ord(c) <= 32 for c in url)
            or not p.hostname or p.username or p.password or p.query or p.fragment
            or p.scheme not in {'http', 'https'} or port == 0
            or p.scheme == 'http' and p.hostname not in {'localhost', '127.0.0.1', '::1'}):
        raise ValueError('MCP requires HTTPS or literal loopback HTTP without embedded credentials.')
    return p


def resolve_destination(url):
    p = endpoint(url)
    try:
        addresses = {r[4][0] for r in socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)}
    except OSError:
        raise PolicyError('MCP destination could not be resolved.') from None
    local = p.hostname in {'localhost', '127.0.0.1', '::1'}
    if not addresses or any(not (ipaddress.ip_address(a).is_loopback if local else ipaddress.ip_address(a).is_global) for a in addresses):
        raise PolicyError('MCP destination resolved outside its reviewed public/loopback scope.')
    return p, sorted(addresses)[0]


def http_worker(packet):
    """Executed only in a disposable child, including DNS and slow socket reads."""
    p, address = resolve_destination(packet['url'])
    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((address, self.port), self.timeout)
    class TLSConnection(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection((address, self.port), self.timeout)
            try:
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            except BaseException:
                sock.close()
                raise
    class HTTP(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(Connection, req)
    class HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(TLSConnection, req, context=self._context)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), HTTP(),
        HTTPS(context=ssl.create_default_context(cafile=certifi.where())))
    data = (urlencode(packet['form']).encode() if packet.get('form') is not None else encoded(packet['body']) if packet.get('body') is not None else None)
    request = urllib.request.Request(packet['url'], data=data,
        headers=packet['headers'], method=packet.get('method', 'POST'))
    try:
        with opener.open(request, timeout=packet['timeout']) as response:
            session = response.headers.get('MCP-Session-Id')
            content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
            if packet.get('purpose') == 'oauth-revoke':
                # RFC 7009 successful revocation is HTTP 200, often an empty body.
                if len(response.read(64_001))>64_000:raise ValueError('Revocation response exceeded its limit.')
                return {'status':response.status}
            if packet.get('purpose') == 'oauth-json':
                raw = response.read(64_001)
                if len(raw) > 64_000 or content_type != 'application/json':
                    raise ValueError('Authentication metadata/response exceeded its JSON boundary.')
                from .oauth_metadata import metadata_document
                return {'document': metadata_document(raw), 'status':response.status}
            if packet.get('notification') or packet.get('method') == 'DELETE':
                return {'session': session, 'status': response.status}
            if content_type == 'text/event-stream':
                size, payload, events, notices = 0, [], 0, []
                for line in response:
                    size += len(line)
                    if size > FRAME:
                        raise ValueError('MCP stream exceeded its byte limit.')
                    if line.startswith(b'data:'):
                        payload.append(line[5:].strip())
                    elif not line.strip() and payload:
                        raw = b'\n'.join(payload); payload = []
                        if not raw:
                            continue
                        event = json.loads(raw); events += 1
                        if events > 64 or not isinstance(event, dict):
                            raise ValueError('Invalid or excessive MCP stream frames.')
                        if event.get('id') == packet['body']['id'] and 'method' not in event:
                            return {'session': session, 'event': event, 'notifications': notices}
                        if event.get('method') in {'notifications/tools/list_changed','notifications/resources/list_changed','notifications/prompts/list_changed'}:
                            notices.append(event['method'])
                        elif 'id' in event:
                            raise ValueError('Server requests are unsupported; no sampling or elicitation was granted.')
                raise ValueError('MCP stream ended without its response; resumable streams are not supported.')
            if content_type != 'application/json':
                raise ValueError('MCP response must be JSON or SSE.')
            raw = response.read(FRAME + 1)
            if len(raw) > FRAME:
                raise ValueError('MCP response exceeded its byte limit.')
            return {'session': session, 'event': json.loads(raw)}
    except urllib.error.HTTPError as exc:
        challenge=', '.join(exc.headers.get_all('WWW-Authenticate',[]))
        return {'http_error': exc.code,'auth_challenge':challenge if len(challenge)<=2048 else '',
            'auth_challenge_invalid':len(challenge)>2048}


def worker_main():
    try:
        raw = sys.stdin.buffer.read(48_001)
        if len(raw) > 48_000:
            raise ValueError('MCP request exceeded its input limit.')
        result = http_worker(json.loads(raw))
    except Exception as exc:
        # Never expose remote response bodies, local paths, or credentials in diagnostics.
        result = {'transport_error': type(exc).__name__}
    sys.stdout.buffer.write(encoded(result))


def python_command(flag, framework=False):
    if getattr(sys, 'frozen', False):
        return [sys.executable, flag]
    executable = Path(sys.base_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
    return [str(executable) if framework and executable.is_file() else sys.executable, '-s', str(Path(__file__).resolve().parents[1] / 'main.py'), flag]


def terminate(proc):
    if proc.poll() is None:
        try:
            if os.name == 'nt':
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def clean_env(folder):
    return {'HOME': str(folder), 'TMPDIR': str(folder), 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8', 'PYTHONNOUSERSITE': '1'}


def isolated_http(packet, cancel, timeout, flag='--mcp-http-worker'):
    if cancel.is_set():
        raise MCPNotDispatched('MCP request cancelled before admission.')
    raw=encoded({**packet,'timeout':timeout})
    if len(raw)>48000:raise PolicyError('MCP/authentication request exceeded its private pipe limit.')
    with tempfile.TemporaryDirectory(prefix='pebble-mcp-http-') as folder:
        proc=subprocess.Popen(python_command(flag),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env=clean_env(folder),cwd=folder,start_new_session=True)
        out,err,sent=bytearray(),bytearray(),0
        try:
            for stream in (proc.stdin,proc.stdout,proc.stderr):os.set_blocking(stream.fileno(),False)
            deadline=time.monotonic()+timeout
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdin,selectors.EVENT_WRITE,'in')
                selector.register(proc.stdout,selectors.EVENT_READ,'out')
                selector.register(proc.stderr,selectors.EVENT_READ,'err')
                while selector.get_map():
                    if cancel.is_set():raise InterruptedError('MCP request cancelled after dispatch.')
                    if time.monotonic()>=deadline:raise TimeoutError('MCP request exceeded its wall-time limit.')
                    for key,_ in selector.select(.02):
                        if key.data=='in':
                            sent+=os.write(key.fileobj.fileno(),raw[sent:])
                            if sent==len(raw):selector.unregister(key.fileobj);key.fileobj.close()
                        else:
                            block=os.read(key.fileobj.fileno(),65536)
                            if not block:selector.unregister(key.fileobj);continue
                            (out if key.data=='out' else err).extend(block)
                            if len(out)+len(err)>FRAME+40000:raise ValueError('MCP child output exceeded its limit.')
            proc.wait(timeout=max(.01,deadline-time.monotonic()))
            if proc.returncode:raise ValueError('MCP worker failed within its process boundary.')
            return json.loads(out)
        finally:
            terminate(proc)
            for stream in (proc.stdin,proc.stdout,proc.stderr):stream.close()


# Phase 32: a failure of the checker itself (it could not import, allocate, or reach a file) is no verdict on the
# schema or the arguments. Phase 32's parallel measurement once saw a valid call refused as "Plugin arguments do not
# match the reviewed tool schema" on a machine short of memory; such a failure now says validation could not
# complete, and names the exception class. Every other exception stays a refusal of the schema or arguments.
CHECKER_FAILURES = (ImportError, MemoryError, OSError, SystemError)


def schema_worker_main():
    from .mcp_schema import worker
    raw = sys.stdin.buffer.read(48_001)
    packet = json.loads(raw) if len(raw) <= 48_000 else {}
    try:result={'valid':worker(packet['schema'],packet.get('arguments'),packet.get('check') is True)} if packet else {'valid':False}
    except CHECKER_FAILURES as exc:result={'valid':False,'checker_failed':type(exc).__name__}
    except Exception:result={'valid':False}
    sys.stdout.buffer.write(encoded(result))


def _verdict(result, what):
    failed = result.get('checker_failed')
    if failed:
        raise PolicyError(what + ' could not be validated: the isolated checker failed (' + str(failed)[:40] + ').')
    return result.get('valid') is True


def valid_arguments(schema, arguments, cancel=None):
    try:
        result = isolated_http({'schema': schema, 'arguments': arguments}, cancel or threading.Event(), 2, '--mcp-schema-worker')
    except (ValueError, OSError):
        raise PolicyError('MCP arguments could not be validated within the isolated budget.') from None
    return _verdict(result, 'MCP arguments')


def valid_schema(schema):
    try:result=isolated_http({'schema':schema,'check':True},threading.Event(),2,'--mcp-schema-worker')
    except (ValueError,OSError):raise PolicyError('MCP schema could not be checked within its isolated budget.') from None
    return _verdict(result,'MCP schema')


def file_digest(path):
    p = Path(path)
    if not p.is_absolute() or any(c.is_symlink() for c in (p, *p.parents)) or not p.is_file() or p.stat().st_size > 100_000_000:
        raise ValueError('MCP launch grants require regular absolute files without symlinks, under 100 MB.')
    digest = hashlib.sha256()
    with p.open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()


def stdio_config(config):
    if sys.platform != 'darwin' or not shutil.which('sandbox-exec'):
        raise PolicyError('Local MCP workers currently require verified macOS confinement.')
    if (set(config) - {'tools', 'resources', 'prompts', 'protocol_era', 'read_only', 'transport', 'command', 'args', 'read_files', 'effects'}
            or not isinstance(config.get('command'), str)
            or not isinstance(config.get('args', []), list) or len(config.get('args', [])) > 16
            or any(not isinstance(x, str) or len(x) > 1000 or '\x00' in x for x in config.get('args', []))
            or not isinstance(config.get('read_files', []), list) or len(config.get('read_files', [])) > 8
            or any(not isinstance(x, str) for x in config.get('read_files', []))):
        raise ValueError('Local MCP requires an absolute command, at most 16 literal args and 8 pinned read files.')
    if Path(config['command']).name in {'sh', 'bash', 'zsh', 'fish', 'env', 'sudo', 'osascript'}:
        raise ValueError('Shell/interpreter dispatch wrappers are not supported for MCP launch.')
    grants = [config['command'], *config.get('read_files', [])]
    if len(set(grants)) != len(grants):
        raise ValueError('Duplicate MCP launch grants.')
    for grant in grants:
        if any(part.lower().startswith('.env') or part.lower() in {'.ssh', '.aws', '.gnupg', '.config', '.pebble', 'credentials', 'secrets'} for part in Path(grant).parts) or Path(grant).suffix.lower() in {'.pem', '.key', '.p12'}:
            raise ValueError('Credential files cannot be MCP launch grants.')
    return {**config, 'transport': 'stdio', 'launch_files': {p: file_digest(p) for p in grants}}


def credential_name(value):
    """The environment-grant rule: one explicit secret-suffixed name, so runtime variables never qualify."""
    return isinstance(value, str) and bool(ENV_NAME.fullmatch(value)) and value.endswith(SECRET_SUFFIXES)


def base_url_name(value, name):
    if (not isinstance(value, str) or not ENV_NAME.fullmatch(value) or value == name or value.endswith(SECRET_SUFFIXES)
            or value in RUNTIME_NAMES or value.startswith(RUNTIME_PREFIXES) or not value.endswith(URL_SUFFIXES)):
        raise PolicyError('Name the base-URL variable the server reads, ending URL, URI, BASE, ENDPOINT, HOST, ORIGIN, '
                          'SERVER or API. It cannot be the credential name, secret-suffixed or a runtime variable.')
    return value


def route_spec(route, environment, loopback=False):
    """The normalized reviewed route for one environment credential, or PolicyError. loopback is test-only."""
    from . import egress
    from .credential_broker import FRAMING, HEADER, SCHEME
    if (not isinstance(route, dict) or set(route) != ROUTE_KEYS or not isinstance(environment, dict)
            or len(environment) != 1 or not credential_name(next(iter(environment)))):
        raise PolicyError('A credential route needs exactly one environment credential and its reviewed route.')
    name = next(iter(environment))
    origin = egress.origin(route['origin'], loopback)
    host = origin[len('https://'):].rpartition(':')[0]
    if not loopback and (host == 'localhost' or host.endswith('.localhost')):
        raise PolicyError('Route origins are remote HTTPS services, never loopback.')
    header, scheme = route['header'], route['scheme']
    if not isinstance(header, str) or not HEADER.fullmatch(header) or header.lower() in FRAMING:
        raise PolicyError('Choose a credential header name that is not an HTTP framing or proxy field.')
    if scheme is not None and (not isinstance(scheme, str) or not SCHEME.fullmatch(scheme)):
        raise PolicyError('A credential scheme is a short token such as Bearer, or none.')
    return {'base_url_env': base_url_name(route['base_url_env'], name), 'origin': origin,
            'methods': list(egress.route_methods(route['methods'])), 'path_prefix': egress.path_prefix(route['path_prefix']),
            'header': header, 'scheme': scheme}


def route_broker(route, environment, loopback=False, ca_file=None):
    """(credentials, broker, server environment). The held token is zeroed once granted (Phase 14 shim pattern)."""
    from .credential_broker import CredentialBroker
    from .egress import EgressBroker
    name = next(iter(environment))
    held = {'api': bytearray(environment[name], 'ascii')}
    credentials = CredentialBroker(lambda ref: held[ref])
    broker = None
    try:
        grant = credentials.grant('api', route['origin'], route['methods'], route['path_prefix'], time.time() + ROUTE_SECONDS,
                                  header=route['header'], scheme=route['scheme'])
        for value in held.values():
            value[:] = bytes(len(value))
        broker = EgressBroker(None, routes={'api': {'origin': route['origin'], 'methods': route['methods'],
                                                    'path_prefix': route['path_prefix'], 'grant': grant}},
                              credentials=credentials, loopback_targets=loopback, ca_file=ca_file, max_connections=4,
                              lifetime_seconds=ROUTE_SECONDS)
        broker.start()
    except BaseException:
        if broker is not None:
            broker.close()
        credentials.close()
        raise
    finally:
        for value in held.values():
            value[:] = bytes(len(value))
    return credentials, broker, {name: PLACEHOLDER, route['base_url_env']: f'{broker.url}/r/api'}


class Stdio:
    def __init__(self, config, direct=False, environment=None, route=None, broker_port=None, *, loopback=False, ca_file=None):
        self.config, self.proc, self.folder = config, None, None
        self.direct = direct
        self.environment = environment or {}
        # route travels to the supervisor over the private pipe; broker_port is the supervisor's own broker.
        # loopback and ca_file are test-only fixtures, as in async_terminal.
        self.route, self.broker_port, self.loopback, self.ca_file = route, broker_port, loopback, ca_file
        self.buffer = b''
    def start(self):
        if self.proc:
            return
        for path, digest in self.config['launch_files'].items():
            if file_digest(path) != digest:
                raise PolicyError('MCP executable or source changed; install and review a new launch grant.')
        if sys.platform != 'darwin':
            raise PolicyError('Local MCP confinement is unavailable on this platform.')
        self.folder = tempfile.TemporaryDirectory(prefix='pebble-mcp-stdio-')
        if not self.direct:
            try:
                self.proc = subprocess.Popen(python_command('--mcp-stdio-worker'), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.folder.name,
                    env=clean_env(self.folder.name), start_new_session=True)
                packet = {'config':self.config,'environment':self.environment}
                if self.route is not None:packet['route'] = self.route
                if self.loopback:packet['loopback'] = True
                if self.ca_file is not None:packet['ca_file'] = self.ca_file
                self.proc.stdin.write(encoded(packet)+b'\n'); self.proc.stdin.flush()
                for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                    os.set_blocking(stream.fileno(), False)
                return
            except BaseException:
                self.close()
                raise
        temp = Path(self.folder.name).resolve()
        quote = lambda s: json.dumps(str(s))
        literals = ' '.join('(literal '+quote(p)+')' for p in self.config['launch_files'])
        # Only the host Python runtime is supported as an interpreter. Other reviewed
        # binaries may use system libraries; arbitrary package managers are not launched.
        runtime = ''
        command = self.config['command']
        handles = ' '.join('(literal '+quote(p)+')' for p in {*Path(command).parents, *temp.parents})
        if Path(command).name.lower().startswith('python'):
            paths = {sys.base_prefix, sys.prefix}
            if command.endswith('/Resources/Python.app/Contents/MacOS/Python'):
                paths.add(str(Path(command).parents[4]))
            runtime = ' '.join('(subpath '+quote(p)+')' for p in paths)
        # With a reviewed route, outbound TCP reaches the supervisor's broker port alone.
        if self.broker_port is None:
            network = '(deny network*)'
        else:
            from .egress import network_profile
            network = network_profile(self.broker_port)
        profile = ('(version 1)(deny default)(allow process-exec (literal '+quote(command)+'))'
            '(deny process-fork)(allow sysctl-read)(allow file-read-metadata)'
            '(allow file-read* '+literals+' '+handles+' '+runtime+' (subpath "/System") (subpath "/usr/lib") (subpath "/private/var/db/dyld") (literal "/dev/null") (literal "/dev/urandom"))'
            '(allow file-read* file-write* (subpath '+quote(temp)+'))'+network+
            r'(deny file-read-data file-write* (regex #"(^|/)(\.env[^/]*|\.ssh|\.aws|\.gnupg|\.pebble|credentials|secrets)(/|$)"))')
        try:
            self.proc = subprocess.Popen(['/usr/bin/sandbox-exec', '-p', profile, command, *self.config.get('args', [])],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.folder.name,
                env={**clean_env(self.folder.name),**self.environment}, start_new_session=False)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                os.set_blocking(stream.fileno(), False)
        except BaseException:
            self.close()
            raise
    def exchange(self, body, cancel, timeout, notification=False):
        if cancel.is_set():
            raise MCPNotDispatched('MCP request cancelled before admission.')
        self.start()
        deadline, sent, stderr, frames, notices = time.monotonic() + timeout, 0, 0, 0, []
        raw = encoded(body) + b'\n'
        if len(raw) > 48_000:
            raise PolicyError('MCP request exceeds its frame limit.')
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdin, selectors.EVENT_WRITE, 'in')
            selector.register(self.proc.stdout, selectors.EVENT_READ, 'out')
            selector.register(self.proc.stderr, selectors.EVENT_READ, 'err')
            while True:
                if cancel.is_set():
                    raise InterruptedError('MCP request cancelled after dispatch.')
                if time.monotonic() >= deadline:
                    raise TimeoutError('MCP worker exceeded its wall-time limit.')
                while b'\n' in self.buffer:
                    line, self.buffer = self.buffer.split(b'\n', 1); frames += 1
                    if len(line) > FRAME or frames > 64:
                        raise ValueError('MCP frame budget exceeded.')
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError('MCP frame must be an object.')
                    if event.get('id') == body.get('id') and 'id' in event and 'method' not in event:
                        return {'event': event, 'notifications': notices}
                    if event.get('method') == 'notifications/tools/list_changed':
                        notices.append(event['method'])
                    elif 'id' in event:
                        raise ValueError('MCP server requests are unsupported.')
                for key, _ in selector.select(.02):
                    if key.data == 'in':
                        sent += os.write(key.fileobj.fileno(), raw[sent:])
                        if sent == len(raw):
                            selector.unregister(key.fileobj)
                            if notification:
                                return {}
                    else:
                        block = os.read(key.fileobj.fileno(), 65536)
                        if not block:
                            selector.unregister(key.fileobj)
                            if key.data == 'out':
                                raise EOFError('MCP worker closed without a response.')
                        elif key.data == 'out':
                            self.buffer += block
                            if len(self.buffer) > FRAME:
                                raise ValueError('MCP stdout exceeded its frame limit.')
                        else:
                            stderr += len(block)
                            if stderr > 32768:
                                raise ValueError('MCP stderr exceeded its limit.')
    def close(self):
        if self.proc:
            if self.direct:
                if self.proc.poll() is None:
                    self.proc.kill()
                self.proc.wait(timeout=2)
            else:
                terminate(self.proc)
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                stream.close()
            self.proc = None
        if self.folder:
            self.folder.cleanup(); self.folder = None


def stdio_supervisor():
    """A backend EOF kills even a server which ignores its own input EOF.

    With a reviewed route the supervisor keeps the credential in its own broker:
    the server environment holds only a placeholder and the broker's loopback base
    URL, and the server profile admits outbound TCP to that port alone.
    """
    import resource
    try:
        # This process and the server it starts may hold a credential: never dump core.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError):
        return
    # Read the preamble without buffered read-ahead stealing a protocol frame.
    raw = bytearray()
    while len(raw) <= 48_000 and not raw.endswith(b'\n'):
        byte = os.read(sys.stdin.fileno(), 1)
        if not byte:
            return
        raw += byte
    if len(raw) > 48_000 or not raw.endswith(b'\n'):
        return
    try:
        packet = json.loads(raw)
    except ValueError:
        return
    finally:
        raw[:] = bytes(len(raw))
    if not isinstance(packet, dict):
        return
    # Popped, so only `environment` refers to the credential (a str cannot be zeroed; see the F2 residuals).
    environment=packet.pop('environment',{})
    config = packet.get('config',packet)
    if not isinstance(environment,dict) or len(environment)>1 or any(not isinstance(k,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',k) or not k.endswith(('TOKEN','API_KEY','SECRET','PASSWORD')) or not isinstance(v,str) or len(v)>8192 for k,v in environment.items()):return
    route, loopback, ca_file = packet.get('route'), packet.get('loopback', False), packet.get('ca_file')
    if type(loopback) is not bool or ca_file is not None and (not loopback or not isinstance(ca_file, str)):
        return
    if route is not None:
        try:
            route = route_spec(route, environment, loopback)
        except (ValueError, TypeError):
            return
    server = broker = credentials = None
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        resource.setrlimit(resource.RLIMIT_FSIZE, (16_000_000, 16_000_000))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        if route is not None:
            # The base-URL variable joins only after the suffix check above, which is unchanged.
            credentials, broker, environment = route_broker(route, environment, loopback, ca_file)
        server = Stdio(config, direct=True, environment=environment, broker_port=broker.port if broker else None)
        server.start()
        sources = {sys.stdin.buffer: server.proc.stdin, server.proc.stdout: sys.stdout.buffer, server.proc.stderr: sys.stderr.buffer}
        buffers, stderr_size = {}, 0
        for stream in [*sources, *sources.values()]:
            os.set_blocking(stream.fileno(), False)
        deadline, next_audit = time.monotonic()+300, 0
        with selectors.DefaultSelector() as selector:
            for source in sources:
                selector.register(source, selectors.EVENT_READ, 'read')
            while time.monotonic() < deadline:
                if time.monotonic() >= next_audit:
                    total, entries = 0, 0
                    for root, dirs, files in os.walk(server.folder.name, followlinks=False):
                        entries += len(dirs)+len(files)
                        if entries > 64:
                            return
                        for name in files:
                            total += os.lstat(os.path.join(root, name)).st_size
                            if total > 16_000_000:
                                return
                    next_audit = time.monotonic()+.1
                for key, _ in selector.select(.05):
                    stream = key.fileobj
                    if key.data == 'read':
                        block = os.read(stream.fileno(), 65536)
                        if not block:
                            return
                        target = sources[stream]
                        if stream is server.proc.stderr:
                            stderr_size += len(block)
                            if stderr_size > 32768:
                                return
                        buffers[target] = buffers.get(target, b'')+block
                        if len(buffers[target]) > FRAME:
                            return
                        try:
                            selector.get_key(target)
                        except KeyError:
                            selector.register(target, selectors.EVENT_WRITE, 'write')
                    else:
                        count = os.write(stream.fileno(), buffers[stream])
                        buffers[stream] = buffers[stream][count:]
                        if not buffers[stream]:
                            selector.unregister(stream)
    except (OSError, ValueError):
        pass
    finally:
        # The server first, then the broker, then the credentials.
        for owned in (server, broker, credentials):
            if owned is not None:
                try:
                    owned.close()
                except Exception:
                    pass


class MCP:
    def __init__(self, url=None, *, config=None, cancel=None, timeout=20, auth=None, plugin_id=None):
        self.config = config or {'transport': 'http', 'url': url}
        self.url = self.config.get('url')
        if self.url:
            endpoint(self.url)
        self.session, self.protocol, self.initialized = None, '2025-11-25', False
        self.modern = self.config.get('protocol_era') == 'modern'
        if self.modern:self.protocol = MODERN
        self.sequence, self.lock = 0, threading.RLock()
        self.cancel, self.timeout = cancel or threading.Event(), timeout
        self.auth, self.plugin_id = auth, plugin_id
        grant = self.auth.credential(plugin_id,self.config,self.cancel) if auth else None
        self.stdio = Stdio(self.config, environment=(grant or {}).get('environment'), route=(grant or {}).get('route')) if self.config.get('transport') == 'stdio' else None
        self.dirty = False
        self.capabilities = {}
        # True once the current tools/call was handed to its transport: an effect may then have occurred.
        self.call_dispatched = False
    def request(self, method, params=None, notification=False):
        if self.cancel.is_set():
            raise MCPNotDispatched('MCP request cancelled before admission.')
        self.sequence += 1
        body = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if self.modern:
            body['params']={**body['params'],'_meta':{'io.modelcontextprotocol/protocolVersion':self.protocol,
                'io.modelcontextprotocol/clientCapabilities':{},'io.modelcontextprotocol/clientInfo':{'name':'Pebble','version':MCP_CLIENT_VERSION}}}
        if not notification:
            body['id'] = self.sequence
        try:
            if self.stdio:
                if method == 'tools/call':self.call_dispatched = True
                reply = self.stdio.exchange(body, self.cancel, self.timeout, notification)
            else:
                headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': self.protocol}
                if self.modern:
                    from .mcp_schema import header_value,parameter_headers
                    headers['Mcp-Method']=method
                    label=(params or {}).get('name',(params or {}).get('uri'))
                    if label is not None:headers['Mcp-Name']=header_value(label)
                    if method=='tools/call':
                        spec=next((s for s in self.config.get('schemas',[]) if s['name']==label),None)
                        if not spec:raise PolicyError('Modern MCP calls require the reviewed full schema.')
                        headers.update(parameter_headers(spec['inputSchema'],params['arguments']))
                if self.auth:
                    grant=self.auth.credential(self.plugin_id,self.config,self.cancel)
                    headers.update((grant or {}).get('headers',{}))
                if self.session:
                    headers['MCP-Session-Id'] = self.session
                if method == 'tools/call':self.call_dispatched = True
                reply = isolated_http({'url': self.url, 'body': body, 'headers': headers, 'notification': notification}, self.cancel, self.timeout)
                if reply.get('http_error'):
                    if reply['http_error'] in {401,403} and self.auth and self.config.get('auth_binding'):
                        self.auth.rejected(self.plugin_id)
                    if reply['http_error'] == 404:
                        self.initialized, self.session = False, None
                    raise ValueError('MCP endpoint returned HTTP '+str(reply['http_error'])+'.')
                if reply.get('transport_error'):
                    raise ValueError('MCP transport failed: '+reply['transport_error'])
                session = reply.get('session')
                if session is not None and not self.modern:
                    if method != 'initialize' and session != self.session or not isinstance(session, str) or not 1 <= len(session) <= 300 or any(not 0x21 <= ord(c) <= 0x7e for c in session):
                        raise ValueError('Invalid or changed MCP session identifier.')
                    self.session = session
            if reply.get('notifications'):
                self.dirty = True
            if notification:
                return {}
            event = reply.get('event')
            if self.auth:
                event=self.auth.redact(self.plugin_id,event)
            if (not isinstance(event, dict) or event.get('jsonrpc') != '2.0' or type(event.get('id')) is not int
                    or event['id'] != body['id'] or 'error' in event or set(event) - {'jsonrpc', 'id', 'result'}
                    or not isinstance(event.get('result'), dict)):
                raise ValueError('Invalid, errored or mismatched MCP response.')
            if self.modern and event['result'].get('resultType')!='complete':
                raise ValueError('Modern MCP returned an undeclared input/extension result; it was not replayed.')
            if self.modern and method in {'server/discover','resources/read','resources/list','prompts/list','tools/list','resources/templates/list'}:
                result=event['result']
                if type(result.get('ttlMs')) is not int or not 0<=result['ttlMs']<=604800000 or result.get('cacheScope') not in {'public','private'}:
                    raise ValueError('Modern MCP result lacks valid cache metadata.')
            return event['result']
        except PolicyError:
            raise
        except (OSError, ValueError, EOFError, TimeoutError, InterruptedError) as exc:
            if method in {'tools/call','resources/read','prompts/get'}:
                self._cancel_request(body.get('id'))
                self.close()
                raise MCPUncertain('MCP call has an unknown remote outcome; inspect it before resuming. '+type(exc).__name__) from None
            self.close()
            if isinstance(exc, InterruptedError):
                raise MCPNotDispatched('MCP discovery was cancelled before tool dispatch.') from None
            raise PolicyError('MCP request failed before any tool call: '+str(exc)) from None
    def _cancel_request(self, ident):
        if self.modern and not self.stdio:return
        try:
            body = {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': ident, 'reason': 'Host cancelled or lost the tool response'}}
            if self.stdio:
                self.stdio.exchange(body, threading.Event(), .2, True)
            else:
                headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': self.protocol}
                if self.session:
                    headers['MCP-Session-Id'] = self.session
                if self.auth:
                    grant=self.auth.credential(self.plugin_id,self.config)
                    headers.update((grant or {}).get('headers',{}))
                isolated_http({'url': self.url, 'body': body, 'headers': headers, 'notification': True}, threading.Event(), .5)
        except Exception:
            pass
    def initialize(self):
        if self.modern:
            result=self.request('server/discover')
            if not isinstance(result.get('supportedVersions'),list) or MODERN not in result['supportedVersions'] or not isinstance(result.get('capabilities'),dict) or not set(result['capabilities']) & {'tools','resources','prompts'}:raise PolicyError('Modern MCP discovery is incompatible. Review a supported protocol mode; calls are not downgraded or replayed.')
            self.capabilities=result['capabilities'];self.initialized=True
            return
        result = self.request('initialize', {'protocolVersion': self.protocol, 'capabilities': {}, 'clientInfo': {'name': 'Pebble', 'version': MCP_CLIENT_VERSION}})
        if result.get('protocolVersion') not in VERSIONS or not isinstance(result.get('capabilities'), dict) or not set(result['capabilities']) & {'tools','resources','prompts'}:
            self.close()
            raise PolicyError('MCP server must negotiate a supported version and tools capability.')
        self.protocol = result['protocolVersion']
        self.capabilities = result['capabilities']
        self.request('notifications/initialized', notification=True)
        self.initialized = True
    def tools(self):
        return self.catalogue('tools')
    def catalogue(self, kind):
        if kind not in {'tools','resources','prompts'}:raise PolicyError('Unknown MCP catalogue.')
        with self.lock:
            if not self.initialized:
                self.initialize()
            if kind not in self.capabilities:raise PolicyError('MCP server did not declare the requested catalogue capability.')
            tools, cursor, seen = [], None, set()
            for _ in range(5):
                result = self.request(kind+'/list', {'cursor': cursor} if cursor else {})
                if self.modern and (type(result.get('ttlMs')) is not int or not 0<=result['ttlMs']<=604800000 or result.get('cacheScope') not in {'public','private'}):raise PolicyError('Modern MCP catalogue lacks valid cache metadata.')
                page = result.get(kind)
                if not isinstance(page, list) or len(tools) + len(page) > 100 or any(not isinstance(t, dict) for t in page):
                    raise PolicyError('MCP catalogue exceeds its bounded tool limit.')
                tools += page
                cursor = result.get('nextCursor')
                if not cursor:
                    self.dirty = False
                    return tools
                if not isinstance(cursor, str) or len(cursor) > 1000 or cursor in seen:
                    raise PolicyError('MCP catalogue has invalid or repeated pagination.')
                seen.add(cursor)
            raise PolicyError('MCP catalogue is incomplete after five pages.')
    def call(self, name, arguments, capture=None):
        with self.lock:
            self.call_dispatched = False
            if not self.initialized:
                self.initialize()
            result = self.request('tools/call', {'name': name, 'arguments': arguments})
            blocks = result.get('content')
            if not isinstance(blocks, list) or len(blocks) > 100 or any(not isinstance(b, dict) for b in blocks) or type(result.get('isError', False)) is not bool:
                self.close()
                raise MCPUncertain('MCP call returned invalid content; inspect its remote outcome.')
            texts = [b.get('text', '') for b in blocks if b.get('type') == 'text']
            if any(not isinstance(t, str) for t in texts):
                raise MCPUncertain('MCP call returned invalid text; inspect its remote outcome.')
            from .mcp_content import normalize
            normalized=capture(result) if capture else normalize(result,modern=self.modern)
            return {'untrusted_plugin_output': '\n'.join(texts)[:12000], 'is_error': result.get('isError', False), 'tool': name,
                **normalized,
                'unsupported_content_blocks': sum(b.get('type') != 'text' for b in blocks), 'authorization': 'none'}
    def close(self):
        if self.stdio:
            self.stdio.close()
        elif self.session:
            try:
                headers={'MCP-Session-Id':self.session,'MCP-Protocol-Version':self.protocol}
                if self.auth:
                    grant=self.auth.credential(self.plugin_id,self.config)
                    headers.update((grant or {}).get('headers',{}))
                isolated_http({'url': self.url, 'body': None, 'method': 'DELETE', 'headers': headers}, threading.Event(), .5)
            except Exception:
                pass
        self.initialized, self.session = False, None
