"""Managed dependency environments: lockfile policy, reviewed installs, mounts and removal.

Every registry is a local HTTPS fixture inside pytest's temporary folder, with a
fixture CA made by /usr/bin/openssl. Installers run for real under sandbox-exec
and reach the fixture only through the EgressBroker (loopback_targets plus a
test-only ca_file). Fixture setup runs openssl and serves HTTP unconfined; the
code under test confines every installer process.
"""

import base64
import gzip
import hashlib
import io
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from pebble import environments
from pebble.environments import Environments, InstallOutcomeUncertain, tree
from pebble.policy import PolicyError, Workspace
from pebble.store import Store

MAC = sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file()
WHEEL = 'pebblefixture-1.0.0-py3-none-any.whl'
TARBALL = 'pebble-npm-fixture-1.0.0.tgz'


def require_mac():
    if not MAC:
        raise AssertionError('Managed environments are a macOS sandbox-exec product surface; this case is not skipped.')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def b64(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()


def build_wheel(blob=b''):
    dist = 'pebblefixture-1.0.0.dist-info'
    files = {'pebblefixture/__init__.py': b"VALUE = 'pebble-fixture-ok'\n",
             f'{dist}/METADATA': b'Metadata-Version: 2.1\nName: pebblefixture\nVersion: 1.0.0\n',
             f'{dist}/WHEEL': b'Wheel-Version: 1.0\nGenerator: pebble-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
    if blob:
        files['pebblefixture/blob.bin'] = blob
    record = ''.join(f'{path},sha256={b64(data)},{len(data)}\n' for path, data in files.items()) + f'{dist}/RECORD,,\n'
    files[f'{dist}/RECORD'] = record.encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for path, data in files.items():
            info = zipfile.ZipInfo(path, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, data, zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


def build_tarball():
    files = {'package/package.json': json.dumps({
                 'name': 'pebble-npm-fixture', 'version': '1.0.0', 'main': 'index.js',
                 # Lifecycle scripts must never run: this would leave a marker beside the package.
                 'scripts': {'postinstall': 'node -e "require(\'fs\').writeFileSync(\'postinstall-ran\', \'x\')"'}}).encode(),
             'package/index.js': b"module.exports = 'pebble-npm-ok';\n"}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for path, data in files.items():
            info = tarfile.TarInfo(path)
            info.size, info.mode, info.mtime = len(data), 0o644, 1577836800
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def integrity(data):
    return 'sha512-' + base64.b64encode(hashlib.sha512(data).digest()).decode()


@pytest.fixture(scope='session')
def tls(tmp_path_factory):
    folder = tmp_path_factory.mktemp('tls').resolve()
    (folder / 'ca.cnf').write_text('[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = Pebble Fixture CA\n'
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
                 [openssl, 'x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-set_serial', '4242',
                  '-days', '2', '-sha256', '-extfile', 'leaf.cnf', '-extensions', 'v3_leaf', '-out', 'leaf.pem']):
        result = subprocess.run(argv, cwd=folder, capture_output=True, text=True, timeout=60,
                                env={'PATH': '/usr/bin:/bin', 'HOME': str(folder), 'RANDFILE': str(folder / '.rnd')})
        assert result.returncode == 0, result.stderr
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(folder / 'leaf.pem', folder / 'leaf.key')
    return SimpleNamespace(ca=str(folder / 'ca.pem'), context=context)


class TLSServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, routes, context):
        self.routes, self.context, self.requests = routes, context, []
        super().__init__(('127.0.0.1', 0), Handler)

    def finish_request(self, request, client_address):
        request.settimeout(10)
        try:
            secure = self.context.wrap_socket(request, server_side=True)
        except (OSError, ssl.SSLError):
            return
        try:
            self.RequestHandlerClass(secure, client_address, self)
        finally:
            secure.close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        self.server.requests.append(path)
        found = self.server.routes.get(path)
        if found is None:
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        kind, body = found
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Stall:
    """Accepts TCP connections and never answers (a hung registry)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self.port, self.accepted, self.held = self.sock.getsockname()[1], 0, []
        self.stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.accepted += 1
            self.held.append(conn)

    def close(self):
        self.stop.set()
        for conn in self.held:
            conn.close()
        self.sock.close()


@pytest.fixture
def servers():
    made = []

    def serve(routes=None, context=None, stall=False):
        if stall:
            made.append(Stall())
        else:
            made.append(TLSServer(routes, context))
            threading.Thread(target=made[-1].serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        return made[-1]
    yield serve
    for server in made:
        if isinstance(server, Stall):
            server.close()
        else:
            server.shutdown()
            server.server_close()


def pypi(tls, serve, wheel=None):
    wheel = wheel or build_wheel()
    page = f'<!DOCTYPE html><html><body><a href="/packages/{WHEEL}#sha256={sha(wheel)}">{WHEEL}</a></body></html>'
    server = serve({'/simple/pebblefixture/': ('text/html', page.encode()),
                    f'/packages/{WHEEL}': ('application/octet-stream', wheel)}, tls.context)
    return server, wheel


@pytest.fixture
def host(tmp_path):
    require_mac()
    home = tmp_path / 'home'
    store = Store(home)
    app = SimpleNamespace(home=home, store=store)
    project = tmp_path / 'project'
    project.mkdir()
    chat = store.new_chat(str(project.resolve()))
    envs = Environments(app)
    yield SimpleNamespace(app=app, store=store, envs=envs, project=project.resolve(), chat=chat, tmp=tmp_path.resolve())
    envs.close()
    store.close()


def rows(host):
    return host.store.query('SELECT * FROM project_environments ORDER BY created')


def requirements(host, text):
    (host.project / 'requirements.txt').write_text(text)
    return Workspace(host.project)


def pinned(wheel):
    return f'pebblefixture==1.0.0 \\\n    --hash=sha256:{sha(wheel)}\n    # via fixture\n'


def npm_project(host, entries, version=3, manifest=None):
    (host.project / 'package.json').write_text(json.dumps(manifest or {'name': 'fixture-app', 'version': '1.0.0'}))
    lock = {'name': 'fixture-app', 'version': '1.0.0', 'lockfileVersion': version, 'requires': True,
            'packages': {'': {'name': 'fixture-app', 'version': '1.0.0'}, **entries}}
    (host.project / 'package-lock.json').write_text(json.dumps(lock))
    return Workspace(host.project)


GOOD = integrity(b'fixture')


def registry_entry(name, resolved=None, sri=GOOD):
    entry = {'version': '1.0.0', 'resolved': resolved or f'https://registry.npmjs.org/{name}/-/{name}-1.0.0.tgz'}
    if sri is not None:
        entry['integrity'] = sri
    return entry


def payload_keys():
    return {'operation', 'kind', 'environment_id', 'chat_id', 'root', 'root_identity', 'manifest_digest', 'lock_digest',
            'toolchain', 'registries', 'packages', 'size_limit', 'timeout', 'requires_review', 'network', 'effects',
            'summary'}


# Plan policy (data only)

def test_npm_plan_accepts_registry_lockfile_and_pins_toolchain_without_effects(host):
    ws = npm_project(host, {'node_modules/left-pad': registry_entry('left-pad'),
                            'node_modules/@scope/util': registry_entry('@scope/util'),
                            'node_modules/left-pad/node_modules/nested': registry_entry('nested')})
    plan = host.envs.plan(host.chat, ws, 'npm-ci')
    assert set(plan) == payload_keys()
    assert plan['operation'] == 'install_environment' and plan['kind'] == 'npm-ci' and plan['packages'] == 3
    assert plan['registries'] == [['registry.npmjs.org', 443]] and plan['requires_review'] is True
    assert plan['network'] == 'allowlisted_registry_only' and plan['root'] == str(host.project)
    assert plan['root_identity'] == list(ws.identity) and plan['chat_id'] == host.chat
    assert plan['lock_digest'] == sha((host.project / 'package-lock.json').read_bytes())
    assert plan['manifest_digest'] == sha((host.project / 'package.json').read_bytes())
    assert plan['size_limit'] == 1 << 30 and plan['timeout'] == 600
    assert len(plan['environment_id']) == 32 and plan['effects'] and 'npm' in plan['summary']
    node = plan['toolchain']['node']
    assert set(node) == {'path', 'realpath', 'sha256', 'size', 'mtime_ns', 'dev', 'ino', 'uid', 'mode'}
    assert node['sha256'] == sha(Path(node['realpath']).read_bytes())
    assert Path(plan['toolchain']['npm_cli']['realpath']).name == 'npm-cli.js'
    assert Path(plan['toolchain']['npm_cli']['realpath']).parent.parent.parent.parent.parent == Path(node['realpath']).parent.parent
    assert plan['toolchain']['npm_package']['files'] > 100 and plan['toolchain']['npm_package']['version']
    json.dumps(plan)
    # Planning has no effects: no row and no storage folder.
    assert rows(host) == [] and not (host.app.home / 'environments').exists()


@pytest.mark.parametrize('entry, category, name', [
    (registry_entry('evil-git', 'git+ssh://git@github.com/evil/repo.git#abc123'), 'git sources', 'evil-git'),
    (registry_entry('gh-short', 'github:evil/repo'), 'git sources', 'gh-short'),
    (registry_entry('local-file', 'file:../vendor/local-file-1.0.0.tgz'), 'file sources', 'local-file'),
    ({'resolved': 'packages/linked', 'link': True}, 'links or workspaces', 'linked'),
    (registry_entry('plain-http', 'http://registry.npmjs.org/plain-http/-/plain-http-1.0.0.tgz'), 'plain http', 'plain-http'),
    (registry_entry('foreign', 'https://evil.example/foreign/-/foreign-1.0.0.tgz'), 'another registry host', 'foreign'),
    (registry_entry('creds', 'https://user:secret@registry.npmjs.org/creds/-/creds-1.0.0.tgz'), 'another registry host', 'creds'),
    (registry_entry('ported', 'https://registry.npmjs.org:8443/ported/-/ported-1.0.0.tgz'), 'another registry host', 'ported'),
    ({'version': '1.0.0', 'integrity': GOOD}, 'no resolved URL', 'unresolved'),
    (registry_entry('no-integrity', sri=None), 'no sha512 integrity', 'no-integrity'),
    (registry_entry('sha1-only', sri='sha1-' + base64.b64encode(hashlib.sha1(b'x').digest()).decode()), 'no sha512 integrity', 'sha1-only'),
    (registry_entry('short-sri', sri='sha512-AAAA'), 'no sha512 integrity', 'short-sri'),
])
def test_npm_plan_refuses_non_registry_sources_with_names_only(host, entry, category, name):
    ws = npm_project(host, {'node_modules/fine': registry_entry('fine'), f'node_modules/{name}': entry})
    with pytest.raises(PolicyError) as caught:
        host.envs.plan(host.chat, ws, 'npm-ci')
    text = str(caught.value)
    assert f'{category}: {name}' in text and 'fine' not in text
    for leak in ('github.com', 'evil.example', 'secret', '://', 'vendor', '8443'):
        assert leak not in text
    assert rows(host) == []


def test_npm_plan_refuses_workspace_entries_and_legacy_sources(host):
    ws = npm_project(host, {'packages/app': {'version': '1.0.0'}, 'node_modules/fine': registry_entry('fine')})
    with pytest.raises(PolicyError, match='links or workspaces'):
        host.envs.plan(host.chat, ws, 'npm-ci')
    ws = npm_project(host, {'node_modules/fine': registry_entry('fine')}, version=2)
    lock = json.loads((host.project / 'package-lock.json').read_text())
    lock['dependencies'] = {'fine': {'version': '1.0.0', 'dependencies': {'sneaky': {'version': 'git+https://example.com/s.git'}}}}
    (host.project / 'package-lock.json').write_text(json.dumps(lock))
    with pytest.raises(PolicyError, match='git sources: sneaky'):
        host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')


def test_npm_plan_refuses_bad_lockfiles(host):
    entries = {'node_modules/fine': registry_entry('fine')}
    npm_project(host, entries, version=1)
    with pytest.raises(PolicyError, match='lockfileVersion 2 or 3'):
        host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')
    for version in ('3', True, 4, None):
        npm_project(host, entries, version=version)
        with pytest.raises(PolicyError, match='lockfileVersion 2 or 3'):
            host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')
    lock = host.project / 'package-lock.json'
    lock.write_text('{"lockfileVersion": 3, "packages": {')
    with pytest.raises(PolicyError, match='not valid UTF-8 JSON'):
        host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')
    lock.write_text('[' * 100_000 + ']' * 100_000)
    with pytest.raises(PolicyError, match='not valid UTF-8 JSON'):
        host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')
    lock.write_bytes(b'{"lockfileVersion": 3, "padding": "' + b'a' * 8_000_001 + b'"}')
    with pytest.raises(PolicyError, match='at most 8 MB'):
        host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')
    lock.write_text(json.dumps({'lockfileVersion': 3, 'packages': {'node_modules/x': registry_entry('x')}}))
    with pytest.raises(PolicyError, match='root entry'):
        host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')
    lock.unlink()
    with pytest.raises(PolicyError, match='package-lock.json is required'):
        host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')
    elsewhere = host.tmp / 'elsewhere.json'
    elsewhere.write_text(json.dumps({'lockfileVersion': 3, 'packages': {'': {}, 'node_modules/x': registry_entry('x')}}))
    lock.symlink_to(elsewhere)
    with pytest.raises(PolicyError, match='not a symbolic link'):
        host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')
    lock.unlink()
    npm_project(host, {})
    with pytest.raises(PolicyError, match='no packages'):
        host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')
    npm_project(host, entries)
    (host.project / 'package.json').write_text('[]')
    with pytest.raises(PolicyError, match='package.json must be a JSON object'):
        host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')
    assert rows(host) == []


HASH = '0' * 64


@pytest.mark.parametrize('text, reason', [
    ('requests==2.31.0\n', 'needs --hash'),
    (f'requests==2.31.0 --hash=sha256:{"AB" * 32}\n', 'needs --hash'),
    (f'requests==2.31.0 --hash=sha1:{"0" * 40}\n', 'needs --hash'),
    (f'requests==2.31.0 --hash=sha256:{HASH} --no-binary=:all:\n', 'needs --hash'),
    (f'requests @ https://evil.example/requests.whl --hash=sha256:{HASH}\n', 'exact name==version'),
    (f'https://evil.example/requests-2.31.0-py3-none-any.whl --hash=sha256:{HASH}\n', 'URL requirements'),
    (f'git+https://evil.example/requests.git --hash=sha256:{HASH}\n', 'URL requirements'),
    (f'-e . --hash=sha256:{HASH}\n', 'editable'),
    ('--editable=./src\n', 'editable'),
    ('-r other.txt\n', 'includes'),
    ('--constraint c.txt\n', 'includes'),
    ('--index-url https://evil.example/simple\n', 'index and source'),
    ('-i https://evil.example/simple\n', 'index and source'),
    ('--extra-index-url https://evil.example/simple\n', 'index and source'),
    ('--find-links /tmp/wheels\n', 'index and source'),
    ('--trusted-host evil.example\n', 'index and source'),
    ('--pre\n', 'options other than --hash'),
    (f'./vendor/requests.whl --hash=sha256:{HASH}\n', 'path requirements'),
    (f'requests>=2.31.0 --hash=sha256:{HASH}\n', 'exact name==version'),
    (f'requests[socks]==2.31.0 --hash=sha256:{HASH}\n', 'exact name==version'),
    (f'requests==2.31.0; python_version > "3" --hash=sha256:{HASH}\n', 'exact name==version'),
    (f'requests==${{VERSION}} --hash=sha256:{HASH}\n', 'exact name==version'),
    (f'requests==2.31.0 --hash=sha256:{HASH}\nRequests==2.31.0 --hash=sha256:{HASH}\n', 'duplicate'),
    ('# only a comment\n\n', 'no hash-pinned packages'),
])
def test_pip_plan_refuses_unpinned_or_redirected_requirements(host, text, reason):
    ws = requirements(host, text)
    with pytest.raises(PolicyError) as caught:
        host.envs.plan(host.chat, ws, 'pip-hashes')
    assert reason in str(caught.value)
    assert 'evil.example' not in str(caught.value)
    assert rows(host) == []


def test_pip_plan_accepts_hash_pinned_continuations_and_comments(host):
    ws = requirements(host, f'# compiled\nalpha==1.0 \\\n    --hash=sha256:{HASH} \\\n    --hash=sha256:{"1" * 64}\n'
                            f'    # via -r requirements.in\nbeta_pkg==2.0.post1 --hash=sha256:{"2" * 64}  # trailing\n')
    plan = host.envs.plan(host.chat, ws, 'pip-hashes')
    assert plan['packages'] == 2 and plan['manifest_digest'] == plan['lock_digest']
    assert plan['registries'] == [['pypi.org', 443], ['files.pythonhosted.org', 443]]
    assert set(plan['toolchain']) == {'python', 'pip_wheel'}
    assert plan['toolchain']['python']['realpath'].endswith('/bin/python3.14')
    assert Path(plan['toolchain']['pip_wheel']['realpath']).name.startswith('pip-')
    assert 'ca_file' not in plan and 'loopback_targets' not in plan


def test_plan_validates_registries_limits_scope_and_permissions(host, tmp_path):
    ws = requirements(host, f'alpha==1.0 --hash=sha256:{HASH}\n')
    for registries in ([('pypi.org', 8443)], [('127.0.0.1', 443)], [('*.pypi.org', 443)], [('xn--pypi-abc.org', 443)],
                       [('a.example', 443)] * 0, [(f'r{i}.example', 443) for i in range(5)], 'pypi.org'):
        with pytest.raises(PolicyError):
            host.envs.plan(host.chat, ws, 'pip-hashes', registries)
    # Fixture registries may use loopback names and other ports only with loopback_targets.
    plan = host.envs.plan(host.chat, ws, 'pip-hashes', [('localhost', 8443)], loopback_targets=True)
    assert plan['registries'] == [['localhost', 8443]] and plan['loopback_targets'] is True
    for kwargs in ({'size_limit': (1 << 30) + 1}, {'size_limit': 10}, {'timeout': 601}, {'timeout': 0},
                   {'ca_file': 'relative.pem'}, {'ca_file': str(tmp_path / 'missing.pem')}, {'loopback_targets': 1}):
        with pytest.raises(PolicyError):
            host.envs.plan(host.chat, ws, 'pip-hashes', **kwargs)
    for kind in ('pip', 'npm-install', None):
        with pytest.raises(PolicyError, match='npm-ci or pip-hashes'):
            host.envs.plan(host.chat, ws, kind)
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(PolicyError, match="selected project folder"):
        host.envs.plan(host.chat, Workspace(other), 'pip-hashes')
    with pytest.raises(PolicyError, match='Conversation not found'):
        host.envs.plan('missing', ws, 'pip-hashes')
    with pytest.raises(PolicyError, match='verified project workspace'):
        host.envs.plan(host.chat, str(host.project), 'pip-hashes')
    policy = {'mode': 'ask', 'tools': {**environments.DEFAULT['tools'], 'web': False}}
    host.store.set_setting('permissions', policy)
    with pytest.raises(PolicyError, match='Permissions'):
        host.envs.plan(host.chat, ws, 'pip-hashes')
    assert Environments.required_permissions == ('files', 'tests', 'web')


def test_storage_overlapping_the_project_or_shared_is_refused(host):
    anchor = host.app.home / 'environments'
    nested = anchor / 'project'
    nested.mkdir(parents=True)
    anchor.chmod(0o700)
    (nested / 'requirements.txt').write_text(f'alpha==1.0 --hash=sha256:{HASH}\n')
    chat = host.store.new_chat(str(nested.resolve()))
    with pytest.raises(PolicyError, match='overlaps'):
        host.envs.plan(chat, Workspace(nested), 'pip-hashes')
    ws = requirements(host, f'alpha==1.0 --hash=sha256:{HASH}\n')
    anchor.chmod(0o755)
    with pytest.raises(PolicyError, match='shared or replaced'):
        host.envs.plan(host.chat, ws, 'pip-hashes')
    anchor.chmod(0o700)
    host.envs.plan(host.chat, ws, 'pip-hashes')


def insert(host, state, ident=None, chat=None, evidence=None):
    ident = ident or os.urandom(16).hex()
    now = time.time()
    host.store.query('INSERT INTO project_environments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (ident, chat or host.chat, str(host.project), '[1,2]', 'pip-hashes', HASH, HASH, '{}', '[]', state,
                      None, None, json.dumps(evidence or {}), now, now))
    return ident


def test_live_environment_limits(host):
    ws = requirements(host, f'alpha==1.0 --hash=sha256:{HASH}\n')
    for state in ('ready', 'uncertain', 'installing', 'failed', 'removed', 'ready'):
        insert(host, state)
    with pytest.raises(PolicyError, match='more than four'):
        host.envs.plan(host.chat, ws, 'pip-hashes')
    other = host.store.new_chat(str(host.project))
    for _ in range(12):
        insert(host, 'ready', chat='elsewhere')
    with pytest.raises(PolicyError, match='more than sixteen'):
        host.envs.plan(other, ws, 'pip-hashes')


# Payload, identity and lifecycle checks before any effect

def test_install_refuses_tampered_payloads_without_effects(host):
    ws = requirements(host, f'alpha==1.0 --hash=sha256:{HASH}\n')
    plan = host.envs.plan(host.chat, ws, 'pip-hashes')
    never = threading.Event()
    bad = [{**plan, 'requires_review': False}, {**plan, 'network': 'any'}, {**plan, 'argv': ['sh']},
           {key: value for key, value in plan.items() if key != 'toolchain'}, {**plan, 'environment_id': '../x'},
           {**plan, 'registries': [['pypi.org', 8443]]}, {**plan, 'timeout': 601}, {**plan, 'kind': 'brew'},
           {**plan, 'operation': 'remove'}, {**plan, 'lock_digest': 'abc'}, {**plan, 'loopback_targets': 'yes'},
           {**plan, 'permission_mode': 'root'}, {**plan, 'kind': ['pip-hashes']}, {**plan, 'chat_id': None}, 'x']
    for payload in bad:
        with pytest.raises(PolicyError, match='malformed|timeout|443'):
            host.envs.install(payload, never)
    changed = json.loads(json.dumps(plan))
    changed['toolchain']['python']['sha256'] = 'f' * 64
    with pytest.raises(PolicyError, match='toolchain changed'):
        host.envs.install(changed, never)
    (host.project / 'requirements.txt').write_text(f'alpha==1.0 --hash=sha256:{"3" * 64}\n')
    with pytest.raises(PolicyError, match='changed after review'):
        host.envs.install(plan, never)
    requirements(host, f'alpha==1.0 --hash=sha256:{HASH}\n')
    stop = threading.Event()
    stop.set()
    with pytest.raises(PolicyError, match='Stopped before'):
        host.envs.install(plan, stop)
    assert rows(host) == [] and not (host.app.home / 'environments' / plan['environment_id']).exists()


def test_install_refuses_a_project_root_replaced_after_plan(host):
    ws = requirements(host, f'alpha==1.0 --hash=sha256:{HASH}\n')
    plan = host.envs.plan(host.chat, ws, 'pip-hashes')
    data = (host.project / 'requirements.txt').read_bytes()
    host.project.rename(host.tmp / 'project-original')
    host.project.mkdir()
    (host.project / 'requirements.txt').write_bytes(data)
    with pytest.raises(PolicyError, match='replaced'):
        host.envs.install(plan, threading.Event())
    assert rows(host) == []
    assert not (host.app.home / 'environments' / plan['environment_id']).exists()


def test_restart_turns_installing_into_uncertain_without_retry(host):
    ident = insert(host, 'installing', evidence={'phase': 'folder created; installer not started', 'dir_identity': [1, 2],
                                                 'output': 'dropped'})
    ready = insert(host, 'ready', evidence={'tree_digest': HASH})
    again = Environments(host.app)
    row = {r['id']: r for r in rows(host)}
    assert row[ident]['state'] == 'uncertain' and row[ready]['state'] == 'ready'
    evidence = json.loads(row[ident]['evidence'])
    assert evidence['phase'] == 'interrupted' and evidence['interrupted'] == 'installing'
    assert evidence['dir_identity'] == [1, 2] and 'output' not in evidence
    Environments(host.app)
    assert json.loads(rows(host)[0]['evidence']) == evidence
    with pytest.raises(PolicyError, match='Remove them before deleting'):
        again.assert_chat_deletable(host.chat)
    listed = again.list(host.chat)['items']
    assert {item['environment_id']: item['state'] for item in listed} == {ident: 'uncertain', ready: 'ready'}
    again.close()


def test_chat_deletion_forgets_only_settled_records_without_folders(host):
    failed, removed = insert(host, 'failed'), insert(host, 'removed')
    assert host.envs.assert_chat_deletable(host.chat)['deletable'] is True
    stray = insert(host, 'failed')
    anchor = host.app.home / 'environments'
    anchor.mkdir(mode=0o700)
    (anchor / stray).mkdir()
    with pytest.raises(PolicyError, match='still on disk'):
        host.envs.forget_chat_records(host.chat)
    (anchor / stray).rmdir()
    assert sorted(host.envs.forget_chat_records(host.chat)['forgotten']) == sorted([failed, removed, stray])
    assert rows(host) == []


def test_remove_refuses_unverified_or_live_folders(host):
    anchor = host.app.home / 'environments'
    anchor.mkdir(mode=0o700)
    ident = insert(host, 'ready', evidence={'dir_identity': [0, 0]})
    (anchor / ident).mkdir()
    (anchor / ident / 'keep.txt').write_text('user data')
    with pytest.raises(PolicyError, match='never deleted'):
        host.envs.remove(host.chat, ident)
    assert (anchor / ident / 'keep.txt').read_text() == 'user data'
    info = os.lstat(anchor / ident)
    link = insert(host, 'uncertain', evidence={'dir_identity': [info.st_dev, info.st_ino]})
    (anchor / link).symlink_to(anchor / ident)
    with pytest.raises(PolicyError, match='never deleted'):
        host.envs.remove(host.chat, link)
    assert (anchor / ident / 'keep.txt').exists()
    live = insert(host, 'installing')
    with pytest.raises(PolicyError, match='Only ready, failed or uncertain'):
        host.envs.remove(host.chat, live)
    gone = insert(host, 'failed')
    assert host.envs.remove(host.chat, gone) == {'environment_id': gone, 'state': 'removed', 'deleted': False,
                                                 'authorization': 'none'}
    for bad in ('../x', 'A' * 32, ident + 'x'):
        with pytest.raises(PolicyError, match='environment ID'):
            host.envs.remove(host.chat, bad)
    with pytest.raises(PolicyError, match='not found'):
        host.envs.remove('other-chat', ident)


def test_tree_digest_covers_files_and_links_and_labels_partial_trees(tmp_path):
    root = tmp_path / 'tree'
    (root / 'a' / 'b').mkdir(parents=True)
    (root / 'a' / 'b' / 'x.txt').write_text('x')
    (root / 'y.txt').write_text('yy')
    (root / 'link').symlink_to('y.txt')
    first = tree(root)
    assert (first['files'], first['bytes'], first['partial']) == (3, 3, False)
    assert first['links'] == [['link', 'y.txt']]
    (root / 'y.txt').write_text('yz')
    assert tree(root)['digest'] != first['digest']
    partial = tree(root, full_files=2, partial_bytes=1)
    assert partial['partial'] is True and partial['digest'] != tree(root)['digest']
    with pytest.raises(PolicyError, match='more than 2 files'):
        tree(root, max_files=2)
    os.mkfifo(root / 'fifo')
    with pytest.raises(PolicyError, match='special file'):
        tree(root)


def test_installer_steps_use_pinned_tools_isolated_env_and_fixed_flags(host, tmp_path):
    ws = requirements(host, f'alpha==1.0 --hash=sha256:{HASH}\n')
    path, broker = host.app.home / 'environments' / ('a' * 32), SimpleNamespace(url='http://127.0.0.1:4242')
    plan = host.envs.plan(host.chat, ws, 'pip-hashes')
    venv, pip = list(host.envs._steps(plan, path, plan['toolchain'], broker))
    assert venv[1] == [plan['toolchain']['python']['realpath'], '-I', '-m', 'venv', '--without-pip', str(path / 'venv')]
    assert pip[1][:3] == [str(path / 'venv' / 'bin' / 'python3'), '-I', plan['toolchain']['pip_wheel']['realpath'] + '/pip']
    for flag in ('--require-hashes', '--no-deps', '--only-binary=:all:', '--no-input', '--disable-pip-version-check'):
        assert flag in pip[1]
    assert '--cert' not in pip[1] and pip[1][-2:] == ['-r', str(path / 'work' / 'requirements.txt')]
    env = pip[2]
    assert env['HOME'] == str(path / 'home') and env['TMPDIR'] == str(path / 'tmp')
    assert env['HTTPS_PROXY'] == env['https_proxy'] == broker.url and env['PIP_CONFIG_FILE'] == os.devnull
    assert env['PIP_INDEX_URL'] == 'https://pypi.org/simple/' and 'HTTP_PROXY' not in env
    assert not {key for key in env if 'TOKEN' in key or 'KEY' in key or key.startswith(('AWS', 'GITHUB', 'OPENAI'))}
    assert pip[3] == [str(environments.FRAMEWORK)] and pip[4] == [str(environments.FRAMEWORK)]
    npm_project(host, {'node_modules/left-pad': registry_entry('left-pad')})
    plan = host.envs.plan(host.chat, Workspace(host.project), 'npm-ci')
    [(label, argv, env, reads, executables)] = list(host.envs._steps(plan, path, plan['toolchain'], broker))
    assert argv == [plan['toolchain']['node']['realpath'], plan['toolchain']['npm_cli']['realpath'], 'ci',
                    '--ignore-scripts', '--no-audit', '--no-fund', '--no-update-notifier', '--cache', str(path / 'cache')]
    assert env['npm_config_registry'] == 'https://registry.npmjs.org/' and env['npm_config_ignore_scripts'] == 'true'
    assert env['npm_config_https_proxy'] == broker.url and env['npm_config_globalconfig'] == '/dev/null'
    assert env['npm_config_userconfig'] == str(path / 'home' / '.npmrc') and 'npm_config_cafile' not in env
    assert executables == [plan['toolchain']['node']['realpath']]
    profile = host.envs._profile(path, reads, executables, 4242)
    assert profile.startswith('(version 1) (deny default)') and 'network*' not in profile
    assert '(allow network-outbound (remote tcp "localhost:4242"))' in profile
    assert f'(allow file-write* (subpath {json.dumps(str(path))}) (literal "/dev/null"))' in profile
    assert str(host.project) not in profile and '(allow mach-lookup)' not in profile


CONFINED = r'''
import json, socket, subprocess, sys
out = {}
def attempt(name, action):
    try:
        action()
        out[name] = 'ok'
    except OSError as exc:
        out[name] = exc.errno
attempt('read_project', lambda: open(sys.argv[1]).read())
attempt('write_outside', lambda: open(sys.argv[2], 'w').write('x'))
attempt('write_inside', lambda: open(sys.argv[3], 'w').write('x'))
attempt('connect_other', lambda: socket.create_connection(('127.0.0.1', int(sys.argv[4])), timeout=3).close())
attempt('connect_broker', lambda: socket.create_connection(('127.0.0.1', int(sys.argv[5])), timeout=3).close())
attempt('exec_other', lambda: subprocess.run(['/bin/echo', 'escaped'], check=True))
print(json.dumps(out))
'''


def test_install_profile_confines_reads_writes_exec_and_network(host):
    secret = host.project / 'secret.txt'
    secret.write_text('project data')
    path = host.tmp / 'env'
    path.mkdir()
    other = socket.socket()
    other.bind(('127.0.0.1', 0))
    other.listen(4)
    broker = environments.EgressBroker([('localhost', 443)], loopback_targets=True).start()
    try:
        framework = str(environments.FRAMEWORK)
        profile = host.envs._profile(path, [framework], [framework], broker.port)
        python = os.path.realpath(environments.PYTHON)
        result = host.envs._run([python, '-I', '-c', CONFINED, str(secret), str(host.tmp / 'outside.txt'),
                                 str(path / 'inside.txt'), str(other.getsockname()[1]), str(broker.port)],
                                path, {'PATH': '/usr/bin:/bin', 'HOME': str(path), 'TMPDIR': str(path)}, profile,
                                threading.Event(), time.monotonic() + 60, path, 10_000_000)
    finally:
        broker.close()
        other.close()
    assert result['code'] == 0, result['output']
    out = json.loads(result['output'].strip().splitlines()[-1])
    assert out['read_project'] == 1 and out['write_outside'] == 1 and out['connect_other'] == 1
    assert out['write_inside'] == 'ok' and out['connect_broker'] == 'ok' and out['exec_other'] in (1, 13)
    assert not (host.tmp / 'outside.txt').exists() and (path / 'inside.txt').read_text() == 'x'
    assert host.envs.processes == set()


def test_output_is_bounded_and_labelled():
    text, cut = environments._text(b'a' * 20_000 + b'\x00\x1b[31m\xff tail')
    assert cut is True and len(text) == environments.OUTPUT_CHARS and text.endswith(' [31m� tail')
    assert environments._text(b'short\n') == ('short\n', False)


# End-to-end installs through the broker

MOUNT_KEYS = {'kind', 'root', 'reads', 'links', 'executable', 'env', 'environment_id', 'tree_digest'}


def runner_accepts(spec, project):
    """Cross-check with the recipe runner's own mount validator (W3, imported lazily)."""
    from pebble import recipes
    assert set(spec) <= recipes.MOUNT_KEYS and set(spec['env']) <= recipes.MOUNT_ENV
    normalized = recipes._mount(spec, SimpleNamespace(needs=spec['kind']), project)
    assert normalized['root'] == spec['root'] and normalized['links'] == spec['links']
    if spec['kind'] == 'python-env':
        # The runner re-pins the interpreter path and requires an identical identity.
        assert recipes.pin(spec['executable']['path'], project) == spec['executable']


def sandboxed(argv, reads, executables, tmp, env):
    quote = json.dumps
    ancestors = set()
    for item in [*reads, *executables, tmp]:
        ancestors.update(Path(item).parents)
    handles = ' '.join('(literal %s)' % quote(str(p)) for p in sorted(ancestors, key=str))
    profile = f'''(version 1) (deny default)
      (allow process-fork) (allow process-exec {' '.join('(subpath %s)' % quote(str(p)) for p in executables)})
      (allow sysctl-read) (allow file-read-metadata)
      (allow file-read* {handles} {' '.join('(subpath %s)' % quote(str(p)) for p in [*reads, *executables, tmp])}
        (subpath "/System") (subpath "/usr/lib") (subpath "/usr/share") (subpath "/private/var/db/dyld")
        (literal "/dev/null") (literal "/dev/urandom"))
      (allow file-write* (literal "/dev/null"))'''
    return subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, *argv], capture_output=True, text=True, timeout=60,
                          env=env, cwd=str(tmp))


def test_pip_install_end_to_end_through_the_broker_then_mount_and_remove(host, tls, servers):
    server, wheel = pypi(tls, servers)
    ws = requirements(host, pinned(wheel))
    plan = host.envs.plan(host.chat, ws, 'pip-hashes', [('localhost', server.server_address[1])],
                          loopback_targets=True, ca_file=tls.ca)
    assert plan['ca_file'] == tls.ca and plan['loopback_targets'] is True
    # Installed exactly as the broker stores it: JSON round trip plus App.propose's mode annotation.
    receipt = host.envs.install(json.loads(json.dumps({**plan, 'permission_mode': 'ask'})), threading.Event())
    assert receipt['state'] == 'ready', receipt['output']
    assert receipt['exit_code'] == 0 and receipt['tree_partial'] is False and receipt['authorization'] == 'none'
    env = host.app.home / 'environments' / plan['environment_id']
    assert receipt['path'] == str(env) and env.is_dir() and stat.S_IMODE(env.stat().st_mode) == 0o700
    assert stat.S_IMODE((host.app.home / 'environments').stat().st_mode) == 0o700
    assert receipt['size_bytes'] > len(b"VALUE = 'pebble-fixture-ok'\n") and receipt['file_count'] >= 8
    # pip ran from the pinned wheel; it was never copied into the environment.
    assert not list((env / 'venv').rglob('pip/__init__.py')) and (env / 'venv' / 'pyvenv.cfg').is_file()
    # The only network path was the broker, and only to the reviewed registry.
    tunnels = [r for r in receipt['egress_log'] if r['allowed']]
    assert tunnels and all(r['host'] == 'localhost' and r['port'] == server.server_address[1] for r in tunnels)
    assert sum(r['bytes_down'] for r in tunnels) > len(wheel) and not [r for r in receipt['egress_log'] if not r['allowed']]
    assert '/simple/pebblefixture/' in server.requests and f'/packages/{WHEEL}' in server.requests
    assert sorted(os.listdir(env / 'work')) == ['requirements.txt']
    row = rows(host)[0]
    evidence = json.loads(row['evidence'])
    assert row['state'] == 'ready' and row['size_bytes'] == receipt['size_bytes'] and row['file_count'] == receipt['file_count']
    assert evidence['tree_digest'] == receipt['tree_digest'] and evidence['egress_log'] == receipt['egress_log']
    assert len(evidence['output']) <= 16_000 and evidence['exit_code'] == 0
    # Mount: verified, read-only data for the test runner.
    spec = host.envs.mount_spec(host.chat, plan['environment_id'], Workspace(host.project))
    assert set(spec) == MOUNT_KEYS and spec['kind'] == 'python-env' and spec['root'] == str(env)
    assert spec['reads'] == [str(env), '/Library/Frameworks/Python.framework'] and spec['links'] == {}
    assert spec['executable']['path'] == str(env / 'venv' / 'bin' / 'python3') and spec['tree_digest'] == receipt['tree_digest']
    assert spec['executable']['realpath'] == plan['toolchain']['python']['realpath']
    assert spec['env'] == {'VIRTUAL_ENV': str(env / 'venv')} and spec['environment_id'] == plan['environment_id']
    runner_accepts(spec, host.project)
    config = (env / 'venv' / 'pyvenv.cfg').read_text()
    assert f'home = {Path(plan["toolchain"]["python"]["realpath"]).parent}' in config
    scratch = host.tmp / 'scratch'
    scratch.mkdir()
    # Reads from the spec and exec of the framework alone suffice: the venv link resolves there.
    ran = sandboxed([spec['executable']['path'], '-I', '-c', 'import pebblefixture; print(pebblefixture.VALUE, pebblefixture.__file__)'],
                    spec['reads'], ['/Library/Frameworks/Python.framework'], scratch,
                    {'PATH': '/usr/bin:/bin', 'HOME': str(scratch), **spec['env']})
    assert ran.returncode == 0, ran.stderr[-2000:]
    value, origin = ran.stdout.split()
    assert value == 'pebble-fixture-ok' and origin.startswith(str(env / 'venv' / 'lib'))
    # Any change inside the environment refuses the mount until it is restored.
    (env / 'venv' / 'injected.py').write_text('x = 1\n')
    with pytest.raises(PolicyError, match='changed after install'):
        host.envs.mount_spec(host.chat, plan['environment_id'], Workspace(host.project))
    (env / 'venv' / 'injected.py').unlink()
    host.envs.mount_spec(host.chat, plan['environment_id'], Workspace(host.project))
    # A stale lockfile refuses the mount.
    original = (host.project / 'requirements.txt').read_bytes()
    (host.project / 'requirements.txt').write_bytes(original + b'# edited\n')
    with pytest.raises(PolicyError, match='changed after this environment was installed'):
        host.envs.mount_spec(host.chat, plan['environment_id'], Workspace(host.project))
    (host.project / 'requirements.txt').write_bytes(original)
    host.envs.mount_spec(host.chat, plan['environment_id'], Workspace(host.project))
    # The same reviewed payload is never installed twice.
    with pytest.raises(PolicyError, match='already attempted'):
        host.envs.install(plan, threading.Event())
    # A project folder replaced at the same path refuses the mount.
    host.project.rename(host.tmp / 'moved')
    host.project.mkdir()
    (host.project / 'requirements.txt').write_bytes(original)
    with pytest.raises(PolicyError, match='different or replaced'):
        host.envs.mount_spec(host.chat, plan['environment_id'], Workspace(host.project))
    with pytest.raises(PolicyError, match='Remove them before deleting'):
        host.envs.assert_chat_deletable(host.chat)
    assert host.envs.remove(host.chat, plan['environment_id'])['state'] == 'removed'
    assert not env.exists() and rows(host)[0]['state'] == 'removed'
    with pytest.raises(PolicyError, match='Only a ready'):
        host.envs.mount_spec(host.chat, plan['environment_id'], Workspace(host.project))
    assert host.envs.forget_chat_records(host.chat)['forgotten'] == [plan['environment_id']]


def test_pip_wrong_hash_fails_and_removes_only_its_folder(host, tls, servers):
    server, wheel = pypi(tls, servers)
    ws = requirements(host, f'pebblefixture==1.0.0 --hash=sha256:{sha(wheel + b"tampered")}\n')
    plan = host.envs.plan(host.chat, ws, 'pip-hashes', [('localhost', server.server_address[1])],
                          loopback_targets=True, ca_file=tls.ca)
    receipt = host.envs.install(plan, threading.Event())
    assert receipt['state'] == 'failed' and receipt['reason'] == 'installer_failed' and receipt['exit_code'] != 0
    assert 'HASHES' in receipt['output'] and receipt['folder_removed'] is True
    assert f'/packages/{WHEEL}' in server.requests
    assert [r['host'] for r in receipt['egress_log'] if r['allowed']]
    assert not (host.app.home / 'environments' / plan['environment_id']).exists()
    assert (host.app.home / 'environments').is_dir()
    row = rows(host)[0]
    assert row['state'] == 'failed' and json.loads(row['evidence'])['reason'] == 'installer_failed'
    with pytest.raises(PolicyError, match='Only a ready'):
        host.envs.mount_spec(host.chat, plan['environment_id'], Workspace(host.project))
    assert host.envs.forget_chat_records(host.chat)['forgotten'] == [plan['environment_id']]


def test_tls_is_verified_without_the_test_only_trust_anchor(host, tls, servers):
    server, wheel = pypi(tls, servers)
    ws = requirements(host, pinned(wheel))
    plan = host.envs.plan(host.chat, ws, 'pip-hashes', [('localhost', server.server_address[1])], loopback_targets=True)
    assert 'ca_file' not in plan
    receipt = host.envs.install(plan, threading.Event())
    assert receipt['state'] == 'failed' and receipt['exit_code'] != 0 and 'CERTIFICATE_VERIFY_FAILED' in receipt['output']
    assert [r for r in receipt['egress_log'] if r['allowed']] and server.requests == []


def test_unlisted_registry_host_is_refused_by_the_broker(host, tls, servers):
    server, wheel = pypi(tls, servers)
    ws = requirements(host, pinned(wheel))
    # The index page points the wheel at a host outside the reviewed registries.
    other = servers({'/simple/pebblefixture/': ('text/html', (
        f'<a href="https://127.0.0.1:{server.server_address[1]}/packages/{WHEEL}#sha256={sha(wheel)}">{WHEEL}</a>').encode())},
        tls.context)
    plan = host.envs.plan(host.chat, ws, 'pip-hashes', [('localhost', other.server_address[1])],
                          loopback_targets=True, ca_file=tls.ca)
    receipt = host.envs.install(plan, threading.Event())
    assert receipt['state'] == 'failed' and receipt['exit_code'] != 0
    refused = [r for r in receipt['egress_log'] if not r['allowed']]
    assert refused and all(r['reason'] == 'not_allowlisted' and r['port'] == server.server_address[1] for r in refused)
    assert f'/packages/{WHEEL}' not in server.requests


def test_size_limit_overflow_fails_and_removes_the_folder(host, tls, servers):
    server, wheel = pypi(tls, servers, build_wheel(os.urandom(1_500_000)))
    ws = requirements(host, pinned(wheel))
    plan = host.envs.plan(host.chat, ws, 'pip-hashes', [('localhost', server.server_address[1])],
                          loopback_targets=True, ca_file=tls.ca, size_limit=1_000_000)
    receipt = host.envs.install(plan, threading.Event())
    assert receipt['state'] == 'failed' and receipt['reason'] == 'size_limit' and receipt['folder_removed'] is True
    assert receipt['exit_code'] is None or receipt['size_bytes'] > 1_000_000
    assert not (host.app.home / 'environments' / plan['environment_id']).exists()
    assert rows(host)[0]['state'] == 'failed'


def test_cancellation_kills_the_installer_group_and_settles_uncertain(host, tls, servers):
    stall = servers(stall=True)
    ws = requirements(host, f'pebblefixture==1.0.0 --hash=sha256:{HASH}\n')
    plan = host.envs.plan(host.chat, ws, 'pip-hashes', [('localhost', stall.port)], loopback_targets=True, ca_file=tls.ca)
    cancel, outcome, seen = threading.Event(), {}, []

    def run():
        try:
            outcome['receipt'] = host.envs.install(plan, cancel)
        except Exception as exc:
            outcome['error'] = exc
    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 120
    while stall.accepted == 0 and worker.is_alive() and time.monotonic() < deadline:
        with host.envs.lock:
            seen.extend(p.pid for p in host.envs.processes if p.pid not in seen)
        time.sleep(0.05)
    assert stall.accepted >= 1, outcome
    with host.envs.lock:
        live = [p.pid for p in host.envs.processes]
    assert live
    cancel.set()
    worker.join(30)
    assert not worker.is_alive()
    assert isinstance(outcome.get('error'), InstallOutcomeUncertain)
    for pid in set(seen + live):
        with pytest.raises(ProcessLookupError):
            os.killpg(pid, 0)
    row = rows(host)[0]
    evidence = json.loads(row['evidence'])
    assert row['state'] == 'uncertain' and evidence['reason'] == 'cancelled'
    assert [r for r in evidence['egress_log'] if r['allowed']]
    env = host.app.home / 'environments' / plan['environment_id']
    assert env.is_dir()
    accepted = stall.accepted
    time.sleep(1)
    assert stall.accepted == accepted and host.envs.processes == set()
    with pytest.raises(PolicyError, match='Only a ready'):
        host.envs.mount_spec(host.chat, plan['environment_id'], Workspace(host.project))
    assert host.envs.remove(host.chat, plan['environment_id'])['deleted'] is True
    assert not env.exists()


def test_close_during_install_kills_the_group_and_settles_uncertain(host, tls, servers):
    stall = servers(stall=True)
    ws = requirements(host, f'pebblefixture==1.0.0 --hash=sha256:{HASH}\n')
    plan = host.envs.plan(host.chat, ws, 'pip-hashes', [('localhost', stall.port)], loopback_targets=True, ca_file=tls.ca)
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.update(error=pytest.raises(InstallOutcomeUncertain, host.envs.install,
                                                                                  plan, threading.Event())))
    worker.start()
    deadline = time.monotonic() + 120
    while stall.accepted == 0 and worker.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    with host.envs.lock:
        live = [p.pid for p in host.envs.processes]
    assert stall.accepted >= 1 and live
    host.envs.close()
    worker.join(30)
    assert not worker.is_alive() and 'error' in outcome
    for pid in live:
        with pytest.raises(ProcessLookupError):
            os.killpg(pid, 0)
    assert rows(host)[0]['state'] == 'uncertain' and host.envs.brokers == set()
    assert json.loads(rows(host)[0]['evidence'])['reason'] == 'service_closed'
    assert (host.app.home / 'environments' / plan['environment_id']).is_dir()
    with pytest.raises(PolicyError, match='Stopped before'):
        host.envs.install(host.envs.plan(host.chat, ws, 'pip-hashes'), threading.Event())


def test_timeout_stops_the_installer_and_settles_uncertain(host, tls, servers):
    stall = servers(stall=True)
    ws = requirements(host, f'pebblefixture==1.0.0 --hash=sha256:{HASH}\n')
    plan = host.envs.plan(host.chat, ws, 'pip-hashes', [('localhost', stall.port)], loopback_targets=True,
                          ca_file=tls.ca, timeout=2)
    started = time.monotonic()
    with pytest.raises(InstallOutcomeUncertain, match='timeout'):
        host.envs.install(plan, threading.Event())
    assert time.monotonic() - started < 30
    assert rows(host)[0]['state'] == 'uncertain' and host.envs.processes == set()
    assert json.loads(rows(host)[0]['evidence'])['reason'] == 'timeout'


def test_npm_ci_end_to_end_through_the_broker_without_lifecycle_scripts(host, tls, servers):
    tarball = build_tarball()
    sri = integrity(tarball)
    routes = {}
    server = servers(routes, tls.context)
    port = server.server_address[1]
    url = f'https://localhost:{port}/pebble-npm-fixture/-/{TARBALL}'
    routes['/pebble-npm-fixture/-/' + TARBALL] = ('application/octet-stream', tarball)
    routes['/pebble-npm-fixture'] = ('application/json', json.dumps({
        'name': 'pebble-npm-fixture', 'dist-tags': {'latest': '1.0.0'},
        'versions': {'1.0.0': {'name': 'pebble-npm-fixture', 'version': '1.0.0',
                               'dist': {'tarball': url, 'integrity': sri}}}}).encode())
    manifest = {'name': 'fixture-app', 'version': '1.0.0', 'dependencies': {'pebble-npm-fixture': '1.0.0'}}
    (host.project / 'package.json').write_text(json.dumps(manifest))
    (host.project / 'package-lock.json').write_text(json.dumps({
        'name': 'fixture-app', 'version': '1.0.0', 'lockfileVersion': 3, 'requires': True,
        'packages': {'': {'name': 'fixture-app', 'version': '1.0.0', 'dependencies': {'pebble-npm-fixture': '1.0.0'}},
                     'node_modules/pebble-npm-fixture': {'version': '1.0.0', 'resolved': url, 'integrity': sri,
                                                         'hasInstallScript': True}}}))
    plan = host.envs.plan(host.chat, Workspace(host.project), 'npm-ci', [('localhost', port)],
                          loopback_targets=True, ca_file=tls.ca)
    receipt = host.envs.install(plan, threading.Event())
    assert receipt['state'] == 'ready', receipt['output']
    env = host.app.home / 'environments' / plan['environment_id']
    package = env / 'work' / 'node_modules' / 'pebble-npm-fixture'
    assert (package / 'index.js').read_bytes() == b"module.exports = 'pebble-npm-ok';\n"
    assert not list(env.rglob('postinstall-ran'))
    assert f'/pebble-npm-fixture/-/{TARBALL}' in server.requests
    tunnels = [r for r in receipt['egress_log'] if r['allowed']]
    assert tunnels and all(r['host'] == 'localhost' and r['port'] == port for r in tunnels)
    spec = host.envs.mount_spec(host.chat, plan['environment_id'], Workspace(host.project))
    assert set(spec) == MOUNT_KEYS and spec['kind'] == 'node_modules'
    assert spec['links'] == {'node_modules': str(env / 'work' / 'node_modules')} and spec['env'] == {'NODE_PATH': str(env / 'work' / 'node_modules')}
    assert spec['reads'] == [str(env)] and spec['executable'] is None and spec['tree_digest'] == receipt['tree_digest']
    runner_accepts(spec, host.project)
    scratch = host.tmp / 'node-scratch'
    scratch.mkdir()
    (scratch / 'node_modules').symlink_to(spec['links']['node_modules'])
    node = plan['toolchain']['node']['realpath']
    ran = sandboxed([node, '-e', "console.log(require('pebble-npm-fixture'))"], spec['reads'], [node], scratch,
                    {'PATH': '/usr/bin:/bin', 'HOME': str(scratch)})
    assert ran.returncode == 0, ran.stderr[-2000:]
    assert ran.stdout.strip() == 'pebble-npm-ok'
    lock = host.project / 'package-lock.json'
    lock.write_bytes(lock.read_bytes() + b'\n')
    with pytest.raises(PolicyError, match='package-lock.json changed'):
        host.envs.mount_spec(host.chat, plan['environment_id'], Workspace(host.project))
    assert host.envs.remove(host.chat, plan['environment_id'])['deleted'] is True
