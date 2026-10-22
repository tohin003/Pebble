"""Language server manager: opt-in, pinned, confined, bounded, versioned, cancellable - via a fake LSP."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pebble import language_servers
from pebble.language_servers import LanguageServers, LspError, Server
from pebble.policy import PolicyError, Workspace

# A scripted language server. It speaks Content-Length framed JSON-RPC on stdin/stdout and logs
# what it receives to <folder>/lsp-<mode>.jsonl. Only the standard library; run with -I.
FAKE = r'''
import json, os, sys, time

mode, folder = sys.argv[1], sys.argv[2]
log_path = os.path.join(folder, 'lsp-%s.jsonl' % mode)
stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
root = None
docs = {}


def log(entry):
    with open(log_path, 'a') as handle:
        handle.write(json.dumps(entry) + '\n')


def read():
    length = None
    while True:
        line = stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode().partition(':')
        if name.lower() == 'content-length':
            length = int(value)
    return json.loads(stdin.read(length))


def send(message):
    body = json.dumps(message).encode()
    stdout.write(b'Content-Length: %d\r\n\r\n' % len(body) + body)
    stdout.flush()


def units(text):
    return len(text.encode('utf-16-le')) // 2


def where(text, word):
    for index, line in enumerate(text.replace('\r\n', '\n').replace('\r', '\n').split('\n')):
        column = line.find(word)
        if column >= 0:
            return {'line': index, 'character': units(line[:column])}


def diagnose(uri, version, label):
    send({'jsonrpc': '2.0', 'method': 'textDocument/publishDiagnostics', 'params': {
        'uri': uri, 'version': version, 'diagnostics': [{
            'range': {'start': {'line': 0, 'character': 0}, 'end': {'line': 0, 'character': 2}},
            'severity': 2, 'code': 'fake', 'source': 'fake', 'message': label}]}})


while True:
    message = read()
    if message is None:
        log({'event': 'eof'})
        sys.exit(0)
    method, ident, params = message.get('method'), message.get('id'), message.get('params') or {}
    log({'method': method, 'id': ident, 'params': params if method != 'textDocument/didOpen' else None,
         'result': message.get('result'), 'error': message.get('error')})
    if method == 'initialize':
        root = params['rootUri']
        if mode == 'hang_init':
            continue
        send({'jsonrpc': '2.0', 'id': ident, 'result': {'capabilities': {
            'definitionProvider': True, 'referencesProvider': True,
            'positionEncoding': 'utf-8' if mode == 'encoding' else 'utf-16'}}})
    elif method == 'initialized':
        if mode == 'server_requests':
            send({'jsonrpc': '2.0', 'id': 'edit', 'method': 'workspace/applyEdit', 'params': {'edit': {
                'changes': {root + '/main.c': [{'range': {'start': {'line': 0, 'character': 0},
                                                          'end': {'line': 0, 'character': 0}}, 'newText': 'X'}]}}}})
            send({'jsonrpc': '2.0', 'id': 'show', 'method': 'window/showDocument',
                  'params': {'uri': 'https://example.com', 'external': True}})
            send({'jsonrpc': '2.0', 'id': 'config', 'method': 'workspace/configuration',
                  'params': {'items': [{'section': 'a'}, {'section': 'b'}]}})
            send({'jsonrpc': '2.0', 'id': 'other', 'method': 'workspace/executeCommand', 'params': {}})
    elif method in ('textDocument/didOpen', 'textDocument/didChange'):
        document = params['textDocument']
        text = document.get('text') if method == 'textDocument/didOpen' else params['contentChanges'][0]['text']
        docs[document['uri']] = text
        if mode == 'stale':
            diagnose(document['uri'], document['version'] - 1, 'old version')
        diagnose(document['uri'], document['version'], 'current version %d' % document['version'])
    elif method in ('textDocument/definition', 'textDocument/references'):
        if mode == 'hang':
            continue
        if mode == 'crash':
            sys.exit(3)
        if mode == 'oversized':
            stdout.write(b'Content-Length: 50000000\r\n\r\n' + b'[' * 4096)
            stdout.flush()
            time.sleep(30)
        if mode == 'garbage':
            stdout.write(b'Content-Length: 9\r\n\r\nnot json!')
            stdout.flush()
            time.sleep(30)
        if mode == 'cancel':
            cancel = read()
            log({'method': cancel.get('method'), 'params': cancel.get('params')})
            send({'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32800, 'message': 'cancelled'}})
            continue
        if mode == 'flood':
            for index in range(400):
                send({'jsonrpc': '2.0', 'method': 'window/logMessage', 'params': {'type': 4, 'message': 'x' * 200}})
        if mode == 'gate':
            open(os.path.join(folder, 'received'), 'w').close()
            deadline = time.time() + 10
            while not os.path.exists(os.path.join(folder, 'go')) and time.time() < deadline:
                time.sleep(0.01)
        uri = params['textDocument']['uri']
        start = where(docs[uri], 'target')
        span = {'start': start, 'end': {'line': start['line'], 'character': start['character'] + 6}}
        other = {'start': {'line': 0, 'character': 4}, 'end': {'line': 0, 'character': 9}}
        send({'jsonrpc': '2.0', 'id': ident, 'result': [
            {'uri': uri, 'range': span},
            {'uri': uri, 'range': span},  # duplicates collapse
            {'targetUri': root + '/other.c', 'targetRange': other, 'targetSelectionRange': other},
            {'uri': 'file:///etc/hosts', 'range': other},
            {'uri': root + '/%2e%2e/escape.c', 'range': other},
            {'uri': root + '/.env.c', 'range': other},
            {'uri': root + '/secrets/key.c', 'range': other},
            {'uri': root + '/link.c', 'range': other},
            {'uri': 'https://example.com/x.c', 'range': other},
            {'uri': root + '/other.c', 'range': {'start': {'line': 99, 'character': 0},
                                                  'end': {'line': 99, 'character': 1}}},
        ]})
    elif method == 'shutdown':
        if mode == 'hang':
            continue
        send({'jsonrpc': '2.0', 'id': ident, 'result': None})
    elif method == 'exit':
        if mode == 'hang':
            continue
        log({'event': 'exit'})
        sys.exit(0)
'''

SOURCE = '// head\x0cer\n/* \U0001f4a1 */ int target = 1;\nint use(void) { return target; }\n'


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(language_servers, 'SHUTDOWN_TIMEOUT', 0.5)
    monkeypatch.setattr(language_servers, 'EXIT_TIMEOUT', 0.5)
    monkeypatch.setattr(language_servers, 'CANCEL_GRACE', 0.2)


@pytest.fixture
def env(tmp_path, fast):
    root = tmp_path / 'project'
    root.mkdir()
    (root / 'main.c').write_text(SOURCE)
    (root / 'other.c').write_text('int other_thing;\n')
    (root / 'secrets').mkdir()
    (root / 'secrets/key.c').write_text('int key;\n')
    (root / '.env.c').write_text('int env;\n')
    outside = tmp_path / 'outside.c'
    outside.write_text('int outside;\n')
    (root / 'link.c').symlink_to(outside)
    tools = tmp_path / 'tools'
    tools.mkdir()
    script = tools / 'fake_lsp.py'
    script.write_text(FAKE)
    logs = tmp_path / 'logs'
    logs.mkdir()
    return root, script, logs


def registry(script, logs, *modes):
    return {f'fake-{mode}': Server(f'fake-{mode}', sys.executable, ('-I', str(script), mode, str(logs)),
                                   ('.c', '.h'), {'.c': 'c', '.h': 'c'}, None) for mode in modes}


class Launcher:
    """The parent's confinement callback stands in here: it launches the plan's argv directly."""

    def __init__(self):
        self.plans, self.handles = [], []

    def __call__(self, plan):
        self.plans.append(plan)
        handle = subprocess.Popen(plan['argv'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, cwd=plan['cwd'], env=plan['env'], start_new_session=True)
        self.handles.append(handle)
        return handle

    def reap(self):
        for handle in self.handles:
            if handle.poll() is None:
                handle.kill()
            handle.wait(5)


@pytest.fixture
def launcher():
    found = Launcher()
    yield found
    found.reap()


def events(logs, mode):
    path = logs / f'lsp-{mode}.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def manager(launcher, script, logs, *modes, **options):
    return LanguageServers(launcher, enabled=[f'fake-{mode}' for mode in modes],
                           registry=registry(script, logs, *modes), **options)


def test_registry_is_fixed_absolute_and_nothing_runs_without_opt_in_and_confinement(env, launcher):
    root, script, logs = env
    assert set(language_servers.REGISTRY) == {'clangd', 'sourcekit-lsp'}
    assert [s.path for s in language_servers.REGISTRY.values()] == ['/usr/bin/clangd', '/usr/bin/sourcekit-lsp']
    assert all(os.path.isabs(s.path) and '--background-index=false' in s.args
               for s in language_servers.REGISTRY.values() if s.id == 'clangd')
    with pytest.raises(PolicyError, match='confinement callback'):
        LanguageServers(None)
    with pytest.raises(PolicyError, match='Unknown language servers'):
        LanguageServers(launcher, enabled=['pyright'])
    servers = LanguageServers(launcher)  # nothing enabled by default
    with pytest.raises(PolicyError, match='not enabled'):
        servers.session('clangd', Workspace(root))
    off = LanguageServers(launcher, registry=registry(script, logs, 'normal'))
    with pytest.raises(PolicyError, match='not enabled'):
        off.session('fake-normal', Workspace(root))
    assert launcher.plans == [] and off.server_for('main.c') is None


def test_inspect_pins_installed_servers_without_launching(env, launcher):
    root, script, logs = env
    report = language_servers.inspect()
    assert set(report) == {'clangd', 'sourcekit-lsp'}
    for entry in report.values():
        assert entry['status'] in ('verified', 'missing', 'refused')
        if entry['status'] == 'verified':
            assert len(entry['sha256']) == 64 and entry['size'] > 0
    absent = {'clangd': Server('clangd', '/nonexistent/pebble/clangd', (), ('.c',), {}, None)}
    assert language_servers.inspect(absent)['clangd']['status'] == 'missing'
    servers = LanguageServers(launcher, enabled=['clangd'], registry=absent)
    with pytest.raises(PolicyError, match='unavailable'):
        servers.session('clangd', Workspace(root))
    assert launcher.plans == []  # an absent server degrades without any launch attempt


def test_plans_pin_identities_and_verify_refuses_drift(env, launcher):
    root, script, logs = env
    ws = Workspace(root)
    plan = language_servers.plan('fake-normal', ws, registry(script, logs, 'normal'))
    assert plan['argv'] == [sys.executable, '-I', str(script), 'normal', str(logs)]
    assert plan['network'] is False and plan['writable'] == [] and plan['stderr'] == 'discard'
    assert plan['cwd'] == str(ws.root) and plan['root_identity'] == list(ws.identity)
    assert len(plan['executable']['sha256']) == 64
    assert [item['path'] for item in plan['entrypoints']] == [str(script)]
    assert set(plan['env']) == {'PATH', 'LANG', 'LC_ALL'}
    language_servers.verify(plan)
    script.write_text(FAKE + '\n# changed after review\n')
    with pytest.raises(PolicyError, match='changed after review'):
        language_servers.verify(plan)
    with pytest.raises(PolicyError, match='registered'):
        language_servers.plan('clangd', ws, registry(script, logs, 'normal'))


def test_definition_references_and_diagnostics_are_scoped_versioned_and_hash_pinned(env, launcher):
    root, script, logs = env
    ws = Workspace(root)
    servers = manager(launcher, script, logs, 'normal')
    assert servers.server_for('src/x.c') == 'fake-normal' and servers.server_for('x.py') is None
    session = servers.session('fake-normal', ws)
    assert servers.session('fake-normal', ws) is session and len(launcher.plans) == 1
    import hashlib
    file_hash = hashlib.sha256(SOURCE.encode()).hexdigest()
    found = session.definition('main.c', 4, 24)  # `target` in `return target;`
    assert found['status'] == 'ok'
    assert found['document'] == {'path': 'main.c', 'file_hash': file_hash, 'version': 1}
    # The form feed is a line break for Read but not for LSP, and the emoji is two UTF-16 units.
    sent = next(e for e in events(logs, 'normal') if e['method'] == 'textDocument/definition')
    assert sent['params']['position'] == {'line': 2, 'character': 23}
    other_hash = hashlib.sha256(b'int other_thing;\n').hexdigest()
    assert found['untrusted_locations'] == [
        {'path': 'main.c', 'line': 3, 'column': 13, 'end_line': 3, 'end_column': 19, 'file_hash': file_hash,
         'basis': 'disk'},
        {'path': 'other.c', 'line': 1, 'column': 5, 'end_line': 1, 'end_column': 10, 'file_hash': other_hash,
         'basis': 'disk'},
    ]
    assert found['dropped'] == {'invalid_range': 1, 'outside_root': 2, 'private_or_link': 3, 'unsupported_uri': 1}
    assert '/etc/hosts' not in json.dumps(found) and 'outside.c' not in json.dumps(found)
    refs = session.references('main.c', 4, 24)
    assert refs['status'] == 'ok' and [r['path'] for r in refs['untrusted_locations']] == ['main.c', 'other.c']
    diagnostics = session.diagnostics('main.c', timeout=5)
    assert diagnostics['status'] == 'ok' and diagnostics['document']['version'] == 1
    assert [(d['line'], d['column'], d['severity'], d['message']) for d in diagnostics['diagnostics']] == [
        (1, 1, 'warning', 'current version 1')]
    (root / 'main.c').write_text(SOURCE + '// edited\n')
    changed = session.diagnostics('main.c', timeout=5)
    assert changed['document']['version'] == 2 and changed['diagnostics'][0]['message'] == 'current version 2'
    change = [e for e in events(logs, 'normal') if e['method'] == 'textDocument/didChange']
    assert change[0]['params']['textDocument']['version'] == 2
    outcome = servers.close_all()['fake-normal']
    assert outcome['shutdown'] == 'ok' and outcome['exited'] and outcome['exit_code'] == 0
    assert 'killed' not in outcome
    order = [e.get('method') or e.get('event') for e in events(logs, 'normal')]
    assert order[-3:] == ['shutdown', 'exit', 'exit']  # request, notification, then the fake's own exit record
    with pytest.raises(LspError):
        session.definition('main.c', 4, 24)


def test_hang_times_out_sends_cancel_then_shutdown_exit_and_kill(env, launcher):
    root, script, logs = env
    servers = manager(launcher, script, logs, 'hang', request_timeout=0.3)
    session = servers.session('fake-hang', Workspace(root))
    started = time.monotonic()
    found = session.definition('main.c', 4, 24)
    assert found['status'] == 'timeout' and found['untrusted_locations'] == []
    assert time.monotonic() - started < 3
    request = next(e for e in events(logs, 'hang') if e['method'] == 'textDocument/definition')
    cancels = [e for e in events(logs, 'hang') if e['method'] == '$/cancelRequest']
    assert cancels and cancels[0]['params'] == {'id': request['id']}
    outcome = session.close()
    assert outcome['shutdown'] == 'timeout' and outcome['killed'] and outcome['exited']
    assert outcome['exit_code'] == -9 and session.close() == outcome  # idempotent
    methods = [e['method'] for e in events(logs, 'hang')]
    assert methods.index('shutdown') < methods.index('exit')


def test_host_cancellation_and_server_cancel_replies(env, launcher):
    root, script, logs = env
    servers = manager(launcher, script, logs, 'cancel', 'hang', request_timeout=5)
    session = servers.session('fake-cancel', Workspace(root))
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    found = session.definition('main.c', 4, 24, cancel=stop)
    assert found['status'] == 'cancelled' and found['error']['code'] == language_servers.REQUEST_CANCELLED
    assert [e['method'] for e in events(logs, 'cancel')].count('$/cancelRequest') == 1
    hung = servers.session('fake-hang', Workspace(root))
    assert hung.definition('main.c', 4, 24, cancel=lambda: True)['status'] == 'cancelled'
    servers.close_all()


def test_crash_mid_request_is_reported_and_later_requests_refuse(env, launcher):
    root, script, logs = env
    servers = manager(launcher, script, logs, 'crash')
    session = servers.session('fake-crash', Workspace(root))
    found = session.definition('main.c', 4, 24)
    assert found['status'] == 'crashed' and found['untrusted_locations'] == []
    assert not session.alive()
    with pytest.raises(LspError, match='crashed'):
        session.references('main.c', 4, 24)
    outcome = session.close()
    assert outcome['exit_code'] == 3 and 'killed' not in outcome and 'shutdown' not in outcome
    # A crashed session is replaced, never resurrected.
    assert servers.session('fake-crash', Workspace(root)) is not session
    servers.close_all()


@pytest.mark.parametrize('mode, reason', [('oversized', 'oversized_message'), ('garbage', 'protocol_error')])
def test_oversized_or_malformed_output_closes_the_session_and_kills(env, launcher, mode, reason):
    root, script, logs = env
    servers = manager(launcher, script, logs, mode)
    session = servers.session(f'fake-{mode}', Workspace(root))
    found = session.definition('main.c', 4, 24)
    assert found['status'] == 'failed' and session.reason == reason
    assert session.counts[reason] == 1 and session.counts['killed'] == 1
    launcher.handles[-1].wait(5)
    assert launcher.handles[-1].returncode == -9
    outcome = session.close()
    assert outcome['exited'] and 'shutdown' not in outcome


def test_session_input_is_bounded(env, launcher, monkeypatch):
    root, script, logs = env
    monkeypatch.setattr(language_servers, 'SESSION_INPUT', 20_000)
    servers = manager(launcher, script, logs, 'flood')
    session = servers.session('fake-flood', Workspace(root))
    assert session.definition('main.c', 4, 24)['status'] == 'failed'
    assert session.reason == 'session_input_limit'
    servers.close_all()


def test_stale_versions_and_changed_files_never_yield_answers(env, launcher):
    root, script, logs = env
    servers = manager(launcher, script, logs, 'stale', 'gate')
    session = servers.session('fake-stale', Workspace(root))
    current = session.diagnostics('main.c', timeout=5)
    assert [d['message'] for d in current['diagnostics']] == ['current version 1']
    assert session.counts['diagnostics_stale_dropped'] >= 1
    gate = servers.session('fake-gate', Workspace(root))
    result = {}
    worker = threading.Thread(target=lambda: result.update(gate.definition('main.c', 4, 24)))
    worker.start()
    deadline = time.monotonic() + 10
    while not (logs / 'received').exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    (root / 'main.c').write_text(SOURCE.replace('target', 'renamed'))  # the file changes mid-request
    (logs / 'go').touch()
    worker.join(10)
    assert result['status'] == 'stale' and result['untrusted_locations'] == []
    servers.close_all()


def test_server_requests_are_answered_without_acting(env, launcher):
    root, script, logs = env
    before = (root / 'main.c').read_text()
    servers = manager(launcher, script, logs, 'server_requests')
    session = servers.session('fake-server_requests', Workspace(root))
    session.diagnostics('main.c', timeout=5)
    deadline = time.monotonic() + 5
    while len([e for e in events(logs, 'server_requests') if e['id'] in ('edit', 'show', 'config', 'other')]) < 4:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    replies = {e['id']: (e['result'], e['error']) for e in events(logs, 'server_requests')
               if e['id'] in ('edit', 'show', 'config', 'other')}
    assert replies['edit'][0]['applied'] is False and replies['show'][0] == {'success': False}
    assert replies['config'] == ([None, None], None) and replies['other'][1]['code'] == -32601
    assert (root / 'main.c').read_text() == before and session.counts['server_requests'] == 4
    servers.close_all()


def test_startup_failures_are_refused_and_cleaned_up(env, launcher):
    root, script, logs = env
    servers = manager(launcher, script, logs, 'encoding', 'hang_init', init_timeout=0.5)
    with pytest.raises(LspError, match='position encoding'):
        servers.session('fake-encoding', Workspace(root))
    with pytest.raises(LspError, match='did not initialize'):
        servers.session('fake-hang_init', Workspace(root))
    for handle in launcher.handles:
        handle.wait(5)
        assert handle.returncode is not None
    # The server that never initialized still honoured shutdown then exit, so it was not killed.
    assert launcher.handles[1].returncode == 0
    assert [e.get('method') or e.get('event') for e in events(logs, 'hang_init')][-3:] == ['shutdown', 'exit', 'exit']


def test_documents_and_positions_stay_inside_policy(env, launcher):
    root, script, logs = env
    (root / 'notes.txt').write_text('text\n')
    servers = manager(launcher, script, logs, 'normal')
    session = servers.session('fake-normal', Workspace(root))
    with pytest.raises(PolicyError):
        session.definition('link.c', 1, 1)  # symbolic link
    with pytest.raises(PolicyError):
        session.definition('.env.c', 1, 1)  # private name
    with pytest.raises(PolicyError):
        session.definition('../outside.c', 1, 1)
    with pytest.raises(LspError, match='does not handle'):
        session.definition('notes.txt', 1, 1)
    for line, column in ((0, 1), (99, 1), (1, 500), (1, 0)):
        with pytest.raises(LspError):
            session.definition('main.c', line, column)
    with pytest.raises(LspError, match='Close another'):
        two = manager(launcher, script, logs, 'normal', 'crash', 'hang')
        two.session('fake-normal', Workspace(root))
        two.session('fake-crash', Workspace(root))
        try:
            two.session('fake-hang', Workspace(root))
        finally:
            two.close_all()
    servers.close_all()


@pytest.mark.parametrize('server', ['clangd', 'sourcekit-lsp'])
def test_installed_servers_verify_under_pinning_when_present(server):
    path = Path(language_servers.REGISTRY[server].path)
    if not path.exists():
        pytest.skip(f'{path} is not installed')
    identity = language_servers.recipes.pin(str(path))
    assert identity['path'] == str(path) and len(identity['sha256']) == 64
    assert language_servers.inspect()[server]['status'] == 'verified'
