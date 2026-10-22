"""Opt-in lifecycle manager for pinned language servers run under host confinement.

Nothing starts unless the host enables a server id and supplies ``confine``, a
callback that launches a reviewed plan under the Phase 14 sandbox profile and
returns a live process handle (``stdin``, ``stdout``, ``poll``, ``wait``, ``kill``).
This module never spawns a process itself, and it never signals a stored PID:
the only control is ``handle.kill()`` on the live handle, and only while the
handle reports no exit.

Allowed servers form a fixed registry of absolute executables. A plan pins the
executable's identity with ``recipes.pin``, plus the delegate a ``/usr/bin``
shim runs when one is listed, and ``verify`` checks it again immediately before
launch. Today the registry holds only ``/usr/bin/clangd`` and
``/usr/bin/sourcekit-lsp``.

The protocol is JSON-RPC with Content-Length framing over pipes. Headers,
messages, the session's total input, pending requests and stored diagnostics
are all bounded. A request that outlives its timeout gets ``$/cancelRequest``
and reports ``timeout``. Shutdown sends ``shutdown``, then ``exit``, waits,
and only then kills.

Documents are opened from ``Workspace.snapshot`` text, so every answer names
the file hash and document version it describes. An answer whose document
changed, on disk or in the session, before it arrived is ``stale`` and carries
no locations. Diagnostics for any version other than the current one are
dropped.

Returned URIs must be ``file:`` paths inside the selected root that pass
``Workspace.path``: no private names, no symbolic links, nothing outside the
root. Anything else is dropped and counted. Positions are converted between
LSP's 0-based lines and UTF-16 columns and the splitlines lines and 1-based
character columns used by Read. Server requests to edit, show documents or open
URLs are refused. Results are untrusted evidence that grants no Read witness or
edit permission.
"""

import json
import os
import queue
import re
import threading
import time
from collections import Counter, namedtuple
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from . import recipes
from .code_intelligence import _SPLIT_BREAK
from .policy import MAX_FILE, PolicyError, digest

VERSION = 'lsp-manager-1'
HEADER_LIMIT = 4_096  # bytes of headers per message
MAX_MESSAGE = 1024 * 1024  # bytes of one inbound body
MAX_OUTBOUND = 2 * 1024 * 1024  # bytes of one outbound body (didOpen carries at most MAX_FILE of text)
SESSION_INPUT = 64 * 1024 * 1024  # total inbound bytes before the session is closed
MAX_PENDING = 8
MAX_DOCUMENTS = 64
MAX_LOCATIONS = 200
MAX_DIAGNOSTICS = 200
MESSAGE_CHARS = 300
MAX_SESSIONS = 2
INIT_TIMEOUT = 20.0
REQUEST_TIMEOUT = 10.0
CANCEL_GRACE = 0.5  # seconds a cancelled request may still answer
SHUTDOWN_TIMEOUT = 2.0
EXIT_TIMEOUT = 2.0
KILL_TIMEOUT = 2.0
REQUEST_CANCELLED, METHOD_NOT_FOUND = -32800, -32601
NOTE = ('Untrusted evidence from a language server about the named file versions. It grants no Read witness '
        'or edit permission; Read a file before changing it.')

Server = namedtuple('Server', 'id path args suffixes languages delegate')
CLT = '/Library/Developer/CommandLineTools/usr/bin'
REGISTRY = {
    # The /usr/bin entries are xcrun shims; the Command Line Tools binary they exec is pinned as the delegate.
    # --background-index=false keeps clangd from writing an index cache into the project.
    'clangd': Server('clangd', '/usr/bin/clangd', ('--background-index=false', '--log=error', '-j=1'),
                     ('.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hh', '.hxx', '.m', '.mm'),
                     {'.c': 'c', '.h': 'c', '.m': 'objective-c', '.mm': 'objective-cpp'}, f'{CLT}/clangd'),
    'sourcekit-lsp': Server('sourcekit-lsp', '/usr/bin/sourcekit-lsp', (), ('.swift',), {'.swift': 'swift'},
                            f'{CLT}/sourcekit-lsp'),
}
ENV = {'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8'}
_REFUSED = {'workspace/applyEdit': {'applied': False, 'failureReason': 'Pebble language servers are read-only.'},
            'window/showDocument': {'success': False}}
_IGNORED = frozenset({'window/logMessage', 'window/showMessage', '$/progress', 'telemetry/event', '$/logTrace',
                      'window/workDoneProgress/cancel'})


class LspError(PolicyError):
    """A language server request that could not be honored."""


def _dict(value):
    return value if isinstance(value, dict) else {}


# Plans ---------------------------------------------------------------------------------------------


def plan(server_id, ws, registry=None):
    """A pinned launch plan for one registered server; nothing is started."""
    registry = REGISTRY if registry is None else registry
    server = registry.get(server_id) if isinstance(server_id, str) else None
    if server is None:
        raise PolicyError('Choose a registered language server.')
    if not os.path.isabs(server.path):
        raise PolicyError('Language servers must be absolute executables.')
    identity = recipes.pin(server.path, ws.root)
    arguments = []
    for argument in server.args:
        if not isinstance(argument, str) or '\x00' in argument:
            raise PolicyError('Language server arguments are fixed host strings.')
        arguments.append(argument)
    entrypoints = [recipes.pin(argument, ws.root, executable=False) for argument in arguments
                   if os.path.isabs(argument) and os.path.isfile(argument)]
    delegate = None
    if server.delegate and os.path.exists(server.delegate):
        delegate = recipes.pin(server.delegate, ws.root)
    return {'server': server.id, 'version': VERSION, 'argv': [server.path, *arguments], 'cwd': str(ws.root),
            'root_identity': list(ws.identity), 'env': dict(ENV), 'executable': identity,
            'entrypoints': entrypoints, 'delegate': delegate, 'network': False, 'writable': [],
            'stdin': 'pipe', 'stdout': 'pipe', 'stderr': 'discard'}


def verify(launch):
    """Re-pin every identity in a plan immediately before launch; any drift refuses."""
    root = launch['cwd']
    checks = [(launch['executable'], True)] + [(item, False) for item in launch['entrypoints']]
    if launch['delegate']:
        checks.append((launch['delegate'], True))
    for expected, executable in checks:
        current = recipes.pin(expected['path'], root, executable)
        changed = [key for key in recipes.IDENTITY if current.get(key) != expected.get(key)]
        if changed:
            raise PolicyError(f'The pinned language server changed after review ({", ".join(changed)}). '
                              'Enable it again.')
    return launch


def inspect(registry=None, project=None):
    """Which registered servers exist and verify under pinning; never launches anything."""
    registry = REGISTRY if registry is None else registry
    report = {}
    for server in registry.values():
        entry = {'path': server.path, 'suffixes': list(server.suffixes)}
        try:
            identity = recipes.pin(server.path, project)
        except PolicyError as exc:
            entry.update(status='missing' if not os.path.lexists(server.path) else 'refused', reason=str(exc))
        else:
            entry.update(status='verified', sha256=identity['sha256'], size=identity['size'])
            if server.delegate:
                entry['delegate'] = server.delegate
                entry['delegate_present'] = os.path.exists(server.delegate)
        report[server.id] = entry
    return report


# Text positions -----------------------------------------------------------------------------------


class _Text:
    """Map between Read positions (splitlines lines, 1-based characters) and LSP UTF-16 positions."""

    _LSP_BREAK = re.compile(r'\r\n|\r|\n')

    def __init__(self, text):
        self.text = text
        self.read_starts = [0] + [found.end() for found in _SPLIT_BREAK.finditer(text)]
        self.lsp_starts = [0] + [found.end() for found in self._LSP_BREAK.finditer(text)]

    @staticmethod
    def _line(starts, offset):
        low, high = 0, len(starts) - 1
        while low < high:
            middle = (low + high + 1) // 2
            if starts[middle] <= offset:
                low = middle
            else:
                high = middle - 1
        return low

    def to_lsp(self, line, column):
        if type(line) is not int or type(column) is not int or column < 1 or not 1 <= line <= len(self.read_starts):
            raise LspError('That line is outside the opened document.')
        offset = self.read_starts[line - 1] + column - 1
        limit = self.read_starts[line] if line < len(self.read_starts) else len(self.text)
        if offset > limit:
            raise LspError('That column is outside the opened document.')
        index = self._line(self.lsp_starts, offset)
        units = sum(2 if ord(char) > 0xFFFF else 1 for char in self.text[self.lsp_starts[index]:offset])
        return {'line': index, 'character': units}

    def from_lsp(self, position):
        line, character = _dict(position).get('line'), _dict(position).get('character')
        if type(line) is not int or type(character) is not int or line < 0 or character < 0:
            return None
        if line >= len(self.lsp_starts):
            return None
        offset, units = self.lsp_starts[line], 0
        limit = self.lsp_starts[line + 1] if line + 1 < len(self.lsp_starts) else len(self.text)
        while offset < limit and units < character:
            units += 2 if ord(self.text[offset]) > 0xFFFF else 1
            offset += 1
        number = self._line(self.read_starts, offset)
        return number + 1, offset - self.read_starts[number] + 1


# Sessions -----------------------------------------------------------------------------------------


_Document = namedtuple('_Document', 'path uri version file_hash text')


class LspSession:
    """One running server for one workspace root, driven only through its live handle."""

    def __init__(self, server, launch, handle, ws, *, request_timeout=None, init_timeout=None):
        self.server, self.plan, self.handle, self.ws = server, launch, handle, ws
        self.request_timeout = REQUEST_TIMEOUT if request_timeout is None else request_timeout
        self.root_uri = Path(ws.root).as_uri()
        self.state = 'starting'
        self.reason = None
        self.outcome = None
        self.capabilities = {}
        self.counts = Counter()
        self._lock = threading.Lock()
        self._closing = threading.Lock()
        self._next = 0
        self._pending = {}  # id -> [event, message]
        self._documents = {}  # path -> _Document
        self._diagnostics = {}  # uri -> (version, list)
        self._diagnostic_event = threading.Condition(self._lock)
        self._outbox = queue.Queue()
        self._received = 0
        self._reader = threading.Thread(target=self._read_loop, name=f'lsp-read-{server.id}', daemon=True)
        self._writer = threading.Thread(target=self._write_loop, name=f'lsp-write-{server.id}', daemon=True)
        self._reader.start()
        self._writer.start()
        self._initialize(INIT_TIMEOUT if init_timeout is None else init_timeout)

    # Transport

    def _write_loop(self):
        while True:
            data = self._outbox.get()
            if data is None:
                return
            try:
                self.handle.stdin.write(data)
                self.handle.stdin.flush()
            except (OSError, ValueError):
                self._fail('closed', 'The language server stopped reading its input.')
                return

    def _send(self, message):
        body = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(body) > MAX_OUTBOUND:
            raise LspError('That request exceeds the language server message limit.')
        if self.state in ('closed', 'failed'):
            raise LspError(f'The language server session is {self.state}: {self.reason or "closed"}.')
        self._outbox.put(b'Content-Length: %d\r\n\r\n' % len(body) + body)

    def _read_message(self, stream):
        length, headers = None, 0
        while True:
            line = stream.readline(HEADER_LIMIT + 1)
            if not line:
                return None  # end of output
            headers += len(line)
            if headers > HEADER_LIMIT or not line.endswith(b'\n'):
                raise ValueError('oversized_header')
            text = line.decode('ascii', 'replace').strip()
            if not text:
                break
            name, _, value = text.partition(':')
            if name.strip().lower() == 'content-length':
                if not value.strip().isdigit():
                    raise ValueError('malformed_header')
                length = int(value.strip())
        if length is None:
            raise ValueError('missing_content_length')
        if length > MAX_MESSAGE:
            raise ValueError('oversized_message')
        if self._received + length > SESSION_INPUT:
            raise ValueError('session_input_limit')
        body = stream.read(length)
        if len(body) != length:
            return None
        self._received += length + headers
        return json.loads(body.decode('utf-8'))

    def _read_loop(self):
        stream = self.handle.stdout
        while True:
            try:
                message = self._read_message(stream)
            except (ValueError, UnicodeDecodeError) as exc:  # json.JSONDecodeError is a ValueError
                reason = str(exc) if str(exc) in ('oversized_header', 'malformed_header', 'missing_content_length',
                                                  'oversized_message', 'session_input_limit') else 'protocol_error'
                self.counts[reason] += 1
                self._stop_process()  # before waking callers, so a reported failure is already contained
                self._fail('failed', reason)
                return
            except OSError:
                message = None
            if message is None:
                self._fail('crashed' if self.state not in ('closing', 'closed') else 'closed',
                           'The language server exited.')
                return
            try:
                self._dispatch(message)
            except (LspError, TypeError, ValueError):
                self.counts['malformed_message'] += 1

    def _dispatch(self, message):
        if not isinstance(message, dict):
            self.counts['malformed_message'] += 1
            return
        method, ident = message.get('method'), message.get('id')
        if method is None:
            with self._lock:
                slot = self._pending.get(ident) if type(ident) is int else None
                if slot is None:
                    self.counts['stray_responses'] += 1
                    return
                slot[1] = message
                slot[0].set()
            return
        if not isinstance(method, str):
            self.counts['malformed_message'] += 1
            return
        if ident is not None:  # a request from the server: answer without acting on anything
            self.counts['server_requests'] += 1
            if method in _REFUSED:
                self._send({'jsonrpc': '2.0', 'id': ident, 'result': _REFUSED[method]})
            elif method == 'workspace/configuration':
                items = _dict(message.get('params')).get('items')
                self._send({'jsonrpc': '2.0', 'id': ident,
                            'result': [None] * min(len(items) if isinstance(items, list) else 0, 64)})
            elif method in ('window/workDoneProgress/create', 'client/registerCapability',
                            'client/unregisterCapability'):
                self._send({'jsonrpc': '2.0', 'id': ident, 'result': None})
            elif method == 'workspace/workspaceFolders':
                self._send({'jsonrpc': '2.0', 'id': ident,
                            'result': [{'uri': self.root_uri, 'name': Path(self.ws.root).name}]})
            else:
                self._send({'jsonrpc': '2.0', 'id': ident,
                            'error': {'code': METHOD_NOT_FOUND, 'message': 'Not supported by Pebble.'}})
            return
        if method == 'textDocument/publishDiagnostics':
            self._publish(_dict(message.get('params')))
        elif method in _IGNORED:
            self.counts['ignored_notifications'] += 1
        else:
            self.counts['unknown_notifications'] += 1

    def _publish(self, params):
        uri, version, items = params.get('uri'), params.get('version'), params.get('diagnostics')
        if not isinstance(uri, str) or not isinstance(items, list):
            self.counts['malformed_message'] += 1
            return
        with self._lock:
            document = next((doc for doc in self._documents.values() if doc.uri == uri), None)
            if document is None:
                self.counts['diagnostics_unopened'] += 1
                return
            if version is not None and version != document.version:
                self.counts['diagnostics_stale_dropped'] += 1  # describes another version of the text
                return
            self._diagnostics[uri] = (document.version, document.file_hash, items[:MAX_DIAGNOSTICS + 1],
                                      version is None)
            self._diagnostic_event.notify_all()

    def _fail(self, state, reason):
        with self._lock:
            if self.state not in ('closed', 'failed', 'crashed'):
                self.state, self.reason = state, reason
            for slot in self._pending.values():
                slot[0].set()
            self._diagnostic_event.notify_all()

    def _stop_process(self):
        """Kill through the live handle only, and only while it has not exited."""
        try:
            if self.handle.poll() is None:
                self.handle.kill()
                self.counts['killed'] += 1
        except OSError:
            pass

    # Requests

    def _request(self, method, params, timeout=None, cancel=None):
        timeout = self.request_timeout if timeout is None else timeout
        with self._lock:
            if self.state not in ('starting', 'ready', 'closing'):
                raise LspError(f'The language server session is {self.state}: {self.reason or "closed"}.')
            if len(self._pending) >= MAX_PENDING:
                raise LspError('Too many language server requests are pending.')
            self._next += 1
            ident = self._next
            slot = [threading.Event(), None]
            self._pending[ident] = slot
        try:
            self._send({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params})
            deadline, interrupted = time.monotonic() + timeout, None
            while not slot[0].wait(min(0.05, max(0.0, deadline - time.monotonic()))):
                stopped = cancel is not None and (cancel() if callable(cancel) else cancel.is_set())
                if stopped or time.monotonic() >= deadline:
                    interrupted = 'cancelled' if stopped else 'timeout'
                    self.counts['cancel_sent'] += 1
                    try:
                        self._send({'jsonrpc': '2.0', 'method': '$/cancelRequest', 'params': {'id': ident}})
                    except LspError:
                        pass
                    slot[0].wait(CANCEL_GRACE)  # a server may still answer, usually with RequestCancelled
                    break
            message = slot[1]
            if message is None:
                if interrupted:
                    return interrupted, None
                return self.state if self.state in ('crashed', 'failed', 'closed') else 'closed', None
            error = message.get('error')
            if error is not None:
                code = _dict(error).get('code')
                status = interrupted or ('cancelled' if code == REQUEST_CANCELLED else 'error')
                return status, {'code': code if type(code) is int else None,
                                'message': ' '.join(str(_dict(error).get('message', '')).split())[:MESSAGE_CHARS]}
            return 'ok', message.get('result')
        finally:
            with self._lock:
                self._pending.pop(ident, None)

    def _initialize(self, timeout):
        capabilities = {
            'general': {'positionEncodings': ['utf-16']},
            'textDocument': {'synchronization': {'didSave': False, 'dynamicRegistration': False},
                             'definition': {'linkSupport': True}, 'references': {},
                             'publishDiagnostics': {'versionSupport': True, 'relatedInformation': False}},
            'workspace': {'configuration': False, 'workspaceFolders': False, 'applyEdit': False},
            'window': {'workDoneProgress': False, 'showDocument': {'support': False}},
        }
        status, result = self._request('initialize', {
            'processId': None, 'clientInfo': {'name': 'Pebble', 'version': VERSION},
            'rootUri': self.root_uri, 'rootPath': str(self.ws.root), 'capabilities': capabilities,
            'workspaceFolders': [{'uri': self.root_uri, 'name': Path(self.ws.root).name}],
            'initializationOptions': {}, 'trace': 'off'}, timeout=timeout)
        if status != 'ok' or not isinstance(result, dict):
            reason = self.reason or f'initialize {status}'
            self.close()
            raise LspError(f'The language server did not initialize ({reason}).')
        encoding = _dict(result.get('capabilities')).get('positionEncoding', 'utf-16')
        if encoding != 'utf-16':
            self.close()
            raise LspError('The language server chose a position encoding Pebble did not offer.')
        self.capabilities = _dict(result.get('capabilities'))
        self._send({'jsonrpc': '2.0', 'method': 'initialized', 'params': {}})
        with self._lock:
            if self.state == 'starting':
                self.state = 'ready'

    # Documents

    def _language(self, path):
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in self.server.suffixes:
            raise LspError(f'{self.server.id} does not handle {suffix or "files without a suffix"}.')
        return self.server.languages.get(suffix, 'cpp' if self.server.id == 'clangd' else suffix.lstrip('.'))

    def _snapshot(self, path):
        data = self.ws.snapshot(path)  # policy, private paths, symlinks and the 96 KB bound
        try:
            return data.decode('utf-8'), digest(data)
        except UnicodeDecodeError:
            raise LspError('Language servers read UTF-8 text only.') from None

    def sync(self, path):
        """Open ``path`` from its current snapshot, or send the full new text if it changed."""
        language = self._language(path)
        text, file_hash = self._snapshot(path)
        uri = (Path(self.ws.root) / path).as_uri()
        with self._lock:
            current = self._documents.get(path)
            if current is not None and current.file_hash == file_hash:
                return current
            if current is None and len(self._documents) >= MAX_DOCUMENTS:
                raise LspError('Too many documents are open in this language server session.')
            version = 1 if current is None else current.version + 1
            document = _Document(path, uri, version, file_hash, text)
            self._documents[path] = document
            self._diagnostics.pop(uri, None)
        if current is None:
            self._send({'jsonrpc': '2.0', 'method': 'textDocument/didOpen', 'params': {'textDocument': {
                'uri': uri, 'languageId': language, 'version': version, 'text': text}}})
        else:
            self._send({'jsonrpc': '2.0', 'method': 'textDocument/didChange', 'params': {
                'textDocument': {'uri': uri, 'version': version}, 'contentChanges': [{'text': text}]}})
        return document

    def close_document(self, path):
        with self._lock:
            document = self._documents.pop(path, None)
            if document is not None:
                self._diagnostics.pop(document.uri, None)
        if document is not None:
            self._send({'jsonrpc': '2.0', 'method': 'textDocument/didClose',
                        'params': {'textDocument': {'uri': document.uri}}})

    def _fresh(self, document):
        """Whether the session and disk still hold exactly the text this answer describes."""
        with self._lock:
            if self._documents.get(document.path) != document:
                return False
        try:
            return self._snapshot(document.path)[1] == document.file_hash
        except (OSError, PolicyError):
            return False

    # Queries

    def definition(self, path, line, column, *, timeout=None, cancel=None):
        return self._locations('textDocument/definition', path, line, column, {}, timeout, cancel)

    def references(self, path, line, column, *, include_declaration=True, timeout=None, cancel=None):
        return self._locations('textDocument/references', path, line, column,
                               {'context': {'includeDeclaration': bool(include_declaration)}}, timeout, cancel)

    def _locations(self, method, path, line, column, extra, timeout, cancel):
        document = self.sync(path)
        position = _Text(document.text).to_lsp(line, column)
        status, result = self._request(method, {'textDocument': {'uri': document.uri}, 'position': position,
                                                **extra}, timeout, cancel)
        answer = {'server': self.server.id, 'status': status,
                  'document': {'path': path, 'file_hash': document.file_hash, 'version': document.version},
                  'untrusted_locations': [], 'dropped': {}, 'note': NOTE}
        if status != 'ok':
            if isinstance(result, dict):
                answer['error'] = result
            return answer
        if not self._fresh(document):
            answer['status'] = 'stale'  # the text changed before the answer arrived
            return answer
        items = [result] if isinstance(result, dict) else result if isinstance(result, list) else []
        rows, dropped, texts = [], Counter(), {}
        for item in items:
            if len(rows) >= MAX_LOCATIONS:
                dropped['limit'] += 1
                continue
            row, reason = self._location(_dict(item), texts)
            if row is None:
                dropped[reason] += 1
            elif row not in rows:
                rows.append(row)
        answer['untrusted_locations'] = rows
        answer['dropped'] = dict(sorted(dropped.items()))
        return answer

    def _location(self, item, texts):
        uri = item.get('targetUri', item.get('uri'))
        span = _dict(item.get('targetSelectionRange', item.get('targetRange', item.get('range'))))
        relative, reason = self.relative(uri)
        if relative is None:
            return None, reason
        if relative not in texts:
            try:
                texts[relative] = (*self._snapshot(relative), )
            except (OSError, PolicyError, LspError):
                texts[relative] = None
        if texts[relative] is None:
            return None, 'unreadable'
        text, file_hash = texts[relative]
        mapping = _Text(text)
        start, end = mapping.from_lsp(span.get('start')), mapping.from_lsp(span.get('end'))
        if start is None or end is None:
            return None, 'invalid_range'
        return {'path': relative, 'line': start[0], 'column': start[1], 'end_line': end[0], 'end_column': end[1],
                'file_hash': file_hash, 'basis': 'disk'}, None

    def relative(self, uri):
        """(relative path, None) for an in-root, policy-eligible file URI, else (None, reason)."""
        if not isinstance(uri, str) or len(uri) > 4096:
            return None, 'malformed_uri'
        parts = urlsplit(uri)
        if parts.scheme != 'file' or parts.netloc not in ('', 'localhost') or parts.query or parts.fragment:
            return None, 'unsupported_uri'
        path = unquote(parts.path)
        if '\x00' in path or not path.startswith('/'):
            return None, 'malformed_uri'
        normal = os.path.normpath(path)
        root = str(self.ws.root)
        if normal != root and not normal.startswith(root.rstrip('/') + '/'):
            return None, 'outside_root'
        relative = normal[len(root.rstrip('/')) + 1:]
        if not relative:
            return None, 'outside_root'
        try:
            target = self.ws.path(relative)  # private names, symbolic links, escapes, non-regular files
            if not target.is_file():
                return None, 'not_a_file'
        except PolicyError:
            return None, 'private_or_link'
        except OSError:
            return None, 'unreadable'
        return relative, None

    def diagnostics(self, path, *, timeout=None):
        """Diagnostics the server published for the current version of ``path``."""
        document = self.sync(path)
        deadline = time.monotonic() + (self.request_timeout if timeout is None else timeout)
        with self._lock:
            while True:
                found = self._diagnostics.get(document.uri)
                if found is not None and found[0] == document.version:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.state not in ('ready', 'starting'):
                    found = None
                    break
                self._diagnostic_event.wait(min(remaining, 0.1))
        answer = {'server': self.server.id,
                  'document': {'path': path, 'file_hash': document.file_hash, 'version': document.version},
                  'diagnostics': [], 'note': NOTE}
        if found is None:
            answer['status'] = 'timeout' if self.state in ('ready', 'starting') else self.state
            return answer
        if not self._fresh(document):
            answer['status'] = 'stale'
            return answer
        mapping, rows = _Text(document.text), []
        for item in found[2][:MAX_DIAGNOSTICS]:
            item = _dict(item)
            span = _dict(item.get('range'))
            start, end = mapping.from_lsp(span.get('start')), mapping.from_lsp(span.get('end'))
            if start is None or end is None:
                continue
            severity = item.get('severity')
            rows.append({'line': start[0], 'column': start[1], 'end_line': end[0], 'end_column': end[1],
                         'severity': {1: 'error', 2: 'warning', 3: 'information', 4: 'hint'}.get(severity, 'unknown'),
                         'code': str(item.get('code'))[:64] if item.get('code') is not None else None,
                         'source': str(item.get('source'))[:64] if item.get('source') is not None else None,
                         'message': ' '.join(str(item.get('message', '')).split())[:MESSAGE_CHARS]})
        answer.update(status='ok', diagnostics=rows, truncated=len(found[2]) > MAX_DIAGNOSTICS)
        if found[3]:
            answer['unversioned'] = True  # the server did not name a version; the hash still matched
        return answer

    # Lifecycle

    def alive(self):
        return self.state == 'ready' and self.handle.poll() is None

    def close(self):
        """shutdown, exit, wait, then kill through the live handle; reports what happened."""
        with self._closing:
            if self.outcome is not None:
                return dict(self.outcome)
            return self._close()

    def _close(self):
        with self._lock:
            was = self.state
            self.state = 'closing' if was in ('ready', 'starting') else was
        outcome = {'requested': was}
        if was in ('ready', 'starting') and self.handle.poll() is None:
            status, _result = self._request('shutdown', None, timeout=SHUTDOWN_TIMEOUT)
            outcome['shutdown'] = status
            try:
                self._send({'jsonrpc': '2.0', 'method': 'exit'})
            except LspError:
                pass
        deadline = time.monotonic() + EXIT_TIMEOUT
        while self.handle.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.handle.poll() is None:
            self._stop_process()
            outcome['killed'] = True
            try:
                self.handle.wait(KILL_TIMEOUT)
            except Exception:  # noqa: BLE001 - reported below as an unknown exit
                pass
        code = self.handle.poll()
        outcome['exit_code'] = code
        outcome['exited'] = code is not None
        self._outbox.put(None)
        # A pipe is closed only after its thread finished: closing a stream another thread is
        # blocked on could hang here. A descendant still holding the pipe leaves it detached.
        for thread, stream, label in ((self._writer, self.handle.stdin, 'writer'),
                                      (self._reader, self.handle.stdout, 'reader')):
            thread.join(1.0)
            if thread.is_alive():
                outcome[label + '_detached'] = True
                continue
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        with self._lock:
            self.state, self.reason = 'closed', self.reason or 'closed by host'
            for slot in self._pending.values():
                slot[0].set()
        outcome['counts'] = dict(sorted(self.counts.items()))
        self.outcome = outcome
        return dict(outcome)


class LanguageServers:
    """Opt-in sessions for enabled servers, launched only through the host's confinement callback."""

    def __init__(self, confine, *, enabled=(), registry=None, request_timeout=None, init_timeout=None):
        if not callable(confine):
            raise PolicyError('Language servers run only through the host confinement callback.')
        self.confine, self.registry = confine, REGISTRY if registry is None else registry
        self.enabled = frozenset(enabled)
        unknown = self.enabled - set(self.registry)
        if unknown:
            raise PolicyError(f'Unknown language servers: {", ".join(sorted(unknown))}.')
        self.request_timeout, self.init_timeout = request_timeout, init_timeout
        self._sessions = {}  # (server id, root, identity) -> LspSession
        self._lock = threading.Lock()

    def inspect(self, project=None):
        return inspect(self.registry, project)

    def server_for(self, path):
        suffix = PurePosixPath(path).suffix.lower()
        return next((server.id for server in self.registry.values()
                     if server.id in self.enabled and suffix in server.suffixes), None)

    def session(self, server_id, ws):
        """The live session for this server and root, starting one through ``confine`` if needed."""
        if server_id not in self.enabled:
            raise PolicyError(f'The {server_id} language server is not enabled. Enable it in settings first.')
        key = (server_id, str(ws.root), tuple(ws.identity))
        with self._lock:
            current = self._sessions.get(key)
            if current is not None and current.alive():
                return current
            if current is not None:
                current.close()
                self._sessions.pop(key, None)
            if len(self._sessions) >= MAX_SESSIONS:
                raise LspError('Close another language server session first.')
            launch = verify(plan(server_id, ws, self.registry))
            handle = self.confine(launch)
            if handle is None or getattr(handle, 'stdin', None) is None or getattr(handle, 'stdout', None) is None:
                raise LspError('The confinement callback did not return a live piped process.')
            session = LspSession(self.registry[server_id], launch, handle, ws,
                                 request_timeout=self.request_timeout, init_timeout=self.init_timeout)
            self._sessions[key] = session
            return session

    def close(self, server_id=None, ws=None):
        """Close matching sessions (all with no arguments); returns their outcomes."""
        with self._lock:
            keys = [key for key in self._sessions if (server_id is None or key[0] == server_id)
                    and (ws is None or key[1:] == (str(ws.root), tuple(ws.identity)))]
            sessions = [self._sessions.pop(key) for key in keys]
        return {key[0]: session.close() for key, session in zip(keys, sessions)}

    def close_all(self):
        return self.close()
